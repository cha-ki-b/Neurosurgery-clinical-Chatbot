# Chatbot audit and conversational-state redesign

**Date:** 2026-08-30 · **Scope:** `clinical-agent-service` (Python) + `openmrs-module-agentgateway` (Java)
**Status:** implemented, 172 automated tests green, verified end-to-end against the live MedGemma
weights with a mock OpenMRS. **Not yet deployed to the running container.**

Findings are numbered continuing from `IMPLEMENTATION-LOG.md`, which ends at Finding 36.

---

## 1. Current architecture

There is no Claude/Anthropic integration anywhere in this project. The interpreter is **MedGemma
4B**, served by vLLM on the private docker network, reached over an OpenAI-compatible HTTP API.
`NLU_ENGINE=medgemma` in `server2-stack/.env`; the deterministic rules engine is the fallback.

```
browser (chat.gsp / agent-chat.js, jQuery)
  │  POST /module/agentgateway/chat.form   {message, conversationId, patientUuid}
  ▼
ChatRelayController.java  ── mints an RS256 delegated token for the logged-in clinician
  │  POST https://agent.../chat  + X-Agent-Channel-Key
  ▼
main.py  /chat  ── verify_channel_secret → verify_delegated_token → ActingUser
  ▼
Orchestrator.handle_turn
  ├── pending confirmation? → yes/no
  ├── deletion? → refuse by name
  ├── interpret  (MedGemma, JSON-schema-constrained → Interpretation{intent,task,slots,clarification})
  ├── availability gate  (live FHIR CapabilityStatement)
  ├── clarification gate
  ├── patient resolution → FHIR Patient search
  ├── slot gap check → ToolSpec.required_slots
  ├── tool.build() → [PlannedOperation]
  ├── writes → summary → PendingAction → wait for "oui"
  └── OpenmrsClient → agentgateway relay → OpenMRS, under the clinician's own token
```

**What was already right, and is untouched.** Two independent trust checks per turn; identity read
only from the signed token; RS256 pinned; the confirmation gate on every write; tools declared in a
registry so the model can only name a registered task; availability read from the live capability
statement; OpenMRS as the sole authority on privilege. None of this was weakened.

**Where state lived.** `ConversationState` held `pending`, `last_patient_uuid`, and three loose
fields — `draft_task`, `draft_slots`, `awaiting_slot`. That was the entire memory of the
conversation.

---

## 2. Root causes

### RC-1 — the frame was destroyed by any turn that was not a direct answer

`orchestrator._interpret_with_carryover` had exactly one question: *does this turn fill the slot I
asked about?* Every "no" ran `draft_task = None; draft_slots = {}` — four separate abandon paths.
Three ordinary clinician turns answer "no" and are not failures: naming a field without its value,
supplying a value for a field named a turn earlier, and saying the question was already answered.

### RC-2 — the interpreter never saw the conversation

`NluEngine.interpret(prompt, context)` has had a `context` argument since Phase 2. The orchestrator
passed `{}` on **every** turn. MedGemma read each sentence as if it were the first thing said, so
"change it to 06564565" arrived with no antecedent for "it". All follow-up understanding was
therefore done by regex, and the `matches_a_task()` gate forced the model's answer to `unsupported`
whenever a frame was open.

### RC-3 — "which field" and "what value" were one unanswerable question

The update flow asked *"Que faut-il modifier exactement ?"* and stored `awaiting_slot="update_field"`
— a sentinel that is not a slot. Its handler required the field **and** the value in the same turn;
anything else abandoned the task. Naming a field alone was structurally unrepresentable.

### RC-4 — no validation layer anywhere

The first component in the whole chain that consults a calendar was OpenMRS's Java date parser.

### RC-5 — grammatical words could become patient names

`_NAME_STOPWORDS` covered English pronouns but not French object pronouns or English determiners,
and MedGemma's name was trusted whenever it appeared as a substring of the prompt — which a pronoun
the clinician typed always does.

---

## 3. Capability map (from the code, not the docs)

```
PATIENT
├── search_patient            read   name | identifier            → FHIR Patient?search
├── list_patients             read   [gender]                     → FHIR Patient?_count=50
├── get_patient_summary       read   patient                      → FHIR Patient + Encounter
├── create_patient            WRITE  name, gender, birthdate       → idgen + webservices.rest
│                                     (+identifier if no idgen source)
└── update_patient_demographics WRITE patient, field, value        → webservices.rest person/*
                                      fields: phone, name  ← only these two

CLINICAL
└── record_neuro_assessment   WRITE  patient, gcs|karnofsky|EVM   → patientview REST
                                      DISABLED (PATIENTVIEW_TOOLS_ENABLED=false)

SCHEDULING
└── book_appointment          WRITE  patient, dates, [time]       → FHIR Appointment
                                      DISABLED (fhir2 1.2.2 advertises no Appointment)

REFUSED BY NAME
└── any deletion
```

**Capability gaps to state plainly:** there is no address field, no consultation retrieval, no
MRI/imaging retrieval, no lab results, no report generation, and no filter by creation date. The
screenshots show clinicians asking for reports and for "patients created today"; the assistant
cannot do either, and §5 makes it say so rather than answer with something else.

---

## 4. The reported failure, traced

Reproduced exactly, word for word, before any change:

| turn | before | code responsible |
|---|---|---|
| `modifie le patient …` | *"Que faut-il modifier exactement…?"* | `orchestrator.py` update block, `awaiting_slot="update_field"` |
| `le telephone` | *"Je n'ai pas compris quel champ modifier…"* — **frame destroyed** | `_interpret_with_carryover`, `update_field` branch: required phone **or** name to be *extracted*; `extract_slots("le telephone")` = `{}` |
| `change it to 06564565` | *"Que faut-il modifier exactement…?"* | frame already gone; `_PHONE_RE` needs a cue word, so no phone extracted either; fell back to `last_patient_uuid` and re-asked |

Two independent bugs compounding: a field name could not be stored, and a value could not be read
without a cue word the previous turn had already supplied.

---

## 5. What changed

### New: `app/dialogue/` — deterministic conversation machinery

**`state` (in `conversation.py`) — `TaskFrame`**

```
task, slots, awaiting, active_field, patient_uuid, patient_label, repairs
```

Owned by application code. The interpreter proposes; the frame decides. `draft_task`,
`draft_slots`, `awaiting_slot` survive as read-only properties onto it, so nothing reads a stale
copy.

**`references.py` — reference resolution.** Two rules, no pronoun list needed:

1. *A field can be named without a value.* `FIELD_ALIASES` maps FR/EN vocabulary onto slots, keyed
   on the tool's own `updatable_fields`, so a field the app cannot write is not recognisable.
2. *When a field is active and the turn names no other field, any value in the turn belongs to it.*
   This resolves "it", "that", "make it 42" and a bare number identically — the antecedent is read
   from the frame, never inferred.

Free-text values (a name) additionally require positive evidence — either the assistant just asked
for that exact field, or an explicit assignment ("… **à** X", "… **to** X"). Without this, "en fait
plutôt le nom" — a clinician *switching* field — was read as an instruction to rename the patient
to "en fait plutôt". Caught by the new test suite, not in review.

**`validation.py` — deterministic slot checks**, run *before* the summary exists, so no clinician
is ever shown something that cannot be written: calendar-valid dates, birthdate not in the future
and within 130 years, phone 6–15 digits, GCS 3–15, Karnofsky 0–100 step 10, EVM component ranges.

### Rewired

- `_absorb_into_frame` replaces the abandon-on-non-answer logic. A turn is offered to: field
  resolution → value-for-active-field → the awaited slot → gap-filling extraction → the
  interpreter's reading. Only a turn that contributes nothing **and** is not conversational repair
  abandons anything, bounded by `MAX_REPAIRS = 2`.
- A repair turn ("je te l'ai déjà dit") is answered with **what is already held**, then the one
  question that remains.
- The interpreter now receives a real context block (active task, patient, field, known slots), and
  the system prompt gained rule 6: follow-ups keep the task, pronouns refer to the context, a
  pronoun is never a name, never re-ask for what the context lists.
- `_handle_pending_answer` gained an **amendment** path: "en fait mets plutôt 0666777888" re-plans
  and re-summarises instead of forcing a cancel-and-retype. An amendment can never execute — it
  re-opens the confirmation gate.
- A deletion typed during a confirmation is refused by name **and** the pending write is shown
  again, not silently discarded.
- A failed write no longer clears the frame.

### New findings fixed

| # | Finding |
|---|---|
| 37 | An impossible date (`20-99-2008` → `2008-99-20`) passed extraction, passed the confirmation summary a clinician approved, and was rejected by OpenMRS's Java date parser — and the failure also discarded the frame, so the name, sex and date all had to be retyped. |
| 38 | A pronoun became a patient name. `generate a report for him` searched OpenMRS for a patient called "him"; `pour lui` did the same in French. The empty result was reported as *"aucun patient ne correspond"* — a false clinical fact manufactured from a grammatical word. |
| 39 | Naming a field without a value was unrepresentable, so it destroyed the request (the reported failure). |
| 40 | An update confirmation read *"Je vais MODIFIER la fiche du patient  :"* whenever the patient came from the open chart or an earlier turn — a write approved without the clinician being told whose record it lands in. |
| 41 | `mets son telephone a 0555123456` matched no task family in the rules engine at all; the fallback answered *"je n'ai pas compris"* to a plain instruction. |
| 42 | A patient list reported the page size as the total: `_count=50` produced *"50 patients trouvés"* regardless of how many matched. Now reports the FHIR `total` and says how many are shown. |
| 43 | An unfiltered list answered a filtered question. *"how many patients got created today"* returned every patient with no qualification. The reply now names the filter actually applied — *"(aucun filtre : la liste complète)"* — so the clinician can see it is not the one they asked for. |

---

## 6. Responsibility split

| Layer | Owns |
|---|---|
| **MedGemma** | reading one sentence: which registered task, which slots. Proposes only. |
| **`dialogue/`** | the frame, reference resolution, slot validation. Deterministic. |
| **Orchestrator** | gates: availability → clarification → patient → slots → validation → confirmation |
| **Tool registry** | what exists, what it needs, what it can write, what it depends on |
| **agentgateway (Java)** | channel secret, token minting, relay, audit log, rollback |
| **OpenMRS** | authorisation. Final word, always, under the clinician's own privileges. |
| **Frontend** | rendering. Holds no task knowledge; the confirmation gate is server-side. |

---

## 7. Verification

| Suite | Result |
|---|---|
| Existing tests (142) | **142 passed** — no regressions, no API contract changed |
| New `tests/test_conversation_state.py` (30) | **30 passed** |
| `tests/eval_nlu.py` — rules | 13 correct / 1 wrong / 3 slot / **UNSAFE 0** — matches recorded baseline exactly |
| `tests/eval_nlu.py` — medgemma (live weights) | 13 correct / 1 wrong / 1 slot / **UNSAFE 0** — matches recorded baseline exactly |
| `tests/live_model_check.py` (4, real MedGemma) | **4 passed** — reported conversation, mixed EN/FR, invalid date + correction, pronoun reference |
| pyflakes over `app/` | clean |

The 30 scenarios cover: search, selection, single- and multi-turn update, follow-ups, pronoun
resolution, "it"/"that", corrections, changing one's mind, ambiguous patient, ambiguous field,
missing value, invalid value, tool failure, patient not found, multiple matches, read vs. write,
confirm, cancel, yes, no, mixed EN/FR, very short turns, several values in one turn, switching
patient, switching task, out of scope, and refusing to invent.

---

## 8. Remaining risks

1. **Not deployed.** The running `clinical-agent` container still has the old code. It serves the
   hospital's real OpenMRS; restarting it is a deliberate act, not a side effect of this work.
2. **Birthdate off by one.** A patient created with `2008-09-20` reads back as `2008-09-19`. This
   is downstream of the agent — a timezone conversion in the OpenMRS REST layer or container `TZ` —
   and was **not** fixed here. It needs confirming against the database before anything is changed.
3. **`update_patient_demographics` writes only `phone` and `name`.** Address and other demographics
   have no build path. The assistant now declines them honestly rather than accepting and losing
   the value, but the gap is real.
4. **Field aliases are a curated list.** `FIELD_ALIASES` covers the FR/EN vocabulary observed so
   far. A phrasing outside it falls through to a clarifying question — safe, but it is friction
   that only real clinician transcripts will find.
5. **`MAX_REPAIRS = 2` is a judgement call**, not a measured value.
6. **The corpus is still ours, not the department's.** `eval_nlu.py` says this already and it
   remains true: 28 sentences written by the people building the thing.

---

# Round 2 — the thirteen items, one by one

**Date:** 2026-08-31 · **Tests:** 215 python (was 142 before any of this work) + 69 java, all green
· **NLU eval:** UNSAFE 0 on both engines, every other column matching the recorded baseline exactly.

**Deployment state, checked rather than assumed.**

- **Round 2 was deployed on 2026-09-01** to `clinical-agent` (image `39485ce89f49`), together with
  `LOG_PROMPTS=false`. Verified after the restart: container healthy, MedGemma engine loaded, FHIR
  capabilities read, `/metrics` present and gated, redaction active, security chain returning 403
  without a channel key and 401 on a bad token, and `book_appointment` / `record_neuro_assessment`
  still correctly reported unavailable.
- **Rollback path:** the previous image is tagged `chu-blida/clinical-agent:rollback-round1`
  (`6f233b9e9c51`); the previous configuration is `server2-stack/.env.bak-20260901-103236`. To go
  back: `docker tag chu-blida/clinical-agent:rollback-round1 chu-blida/clinical-agent:1.0.0`,
  restore the `.env`, then `docker compose ... up -d --no-deps clinical-agent`.
- **Not deployed:** the agentgateway module on Server 1 is still 1.1.4.
  `agentgateway-1.1.5.omod` is built and tested (69/69) and waiting, so the two chat-panel fixes
  (waiting indicator, conversation surviving a reload) are not yet live.
- **No clinician turn has yet run against round 2.** Minting a delegated token requires OpenMRS's
  private key, so it cannot be driven from this host; the first real turn is the remaining
  verification.

## Tier 1 — soundness

### 1. Writes are no longer believed because OpenMRS said 200 · **DONE**

`ToolSpec.verify` returns a `WriteVerification`: a read that proves the write landed, plus the
check that reads its answer. It runs after every operation succeeds and before any success is
reported. Three outcomes, and the middle one is why it exists:

| read-back says | reported as |
| --- | --- |
| the value is there | `C'est enregistre.` — as before |
| accepted, value **not** there | `Echec : … Rien n'a change` — a silent no-op named as one |
| record could not be re-read | neither claimed nor denied: "je n'ai pas pu relire la fiche" |

The read-back deliberately goes through FHIR while the write goes through `webservices.rest`, so it
cannot merely echo what was just sent. This is not hypothetical on this deployment: a FHIR PUT
replacing an existing telecom or name returns 200 and changes nothing (fhir2 1.2.2 maps each
incoming entry to a new object that Hibernate's `Set` discards). Five tests, including one that
simulates exactly that.

### 2. Patient data no longer reaches this server's logs · **DONE**

`LOG_PROMPTS` was believed to control this. It controlled **two log lines out of fifteen**. Thirteen
others wrote the clinician's sentence, or a name, phone number or date of birth taken from it, at
INFO level regardless of the flag — every dropped slot, every abandoned frame, every task switch.
And `httpx` logs every request line, so `GET /ws/fhir2/R4/Patient?name=…` published the name a
clinician typed one line later.

`app/phi.py` renders anything that may carry patient data as its *shape* unless an operator has
explicitly turned prompt logging on; the `httpx` logger is raised to WARNING on the same condition;
and turning the flag on now logs a warning saying, in words, that it writes patient data to
container logs. Four tests, one of which drives a whole conversation with a distinctive name and
asserts it appears nowhere.

**Measured, so as not to overstate it:** the current container's log holds **zero chat turns** —
only healthcheck traffic. The exposure is real and latent, not a breach that has already happened.

### 3. One turn at a time per conversation · **DONE**

The store's lock protected the dictionary and was released the moment `get` returned; every
mutation of the frame happened after that, unsynchronised, for the whole length of a turn — up to
25 seconds waiting on the model. Turns are now serialised per conversation, and independent
conversations still run concurrently. Verified by removing the lock and watching the test fail.

The store remains process-local and now says so: **it assumes one replica.** Scaling out needs
sticky sessions or a shared store, not a second container.

## Tier 2 — capability

### 4. `record_neuro_assessment` · **BLOCKED, not agent code**

The agent side is complete: the tool is registered, builds a UUID-keyed POST, and reports itself
unavailable with a reason. The blocker is `openmrs-module-patientview`, which is **not in this
repository and not on this machine**. Its existing endpoints key on the internal numeric
`patient_id`, which the REST API never hands out; it needs new `webservices.rest` resources keyed
on patient UUID. Then `PATIENTVIEW_TOOLS_ENABLED=true` is the whole agent-side change.

### 5. `book_appointment` · **BLOCKED, not agent code — but its root cause was actionable**

Confirmed by probing the live server: the deployed fhir2 advertises 17 resources and `Appointment`
is **not** among them. Finding 36's root cause is the OpenMRS global property
`timezone.conversions = false`, which needs an admin session on Server 1.

That property governs **every** date the REST layer serialises — including patient birthdates. So
what could be done from the agent side was done: see item 5b.

### 5b. A created record's stored values are now checked · **DONE**

A patient created with `2008-09-20` was observed listed afterwards as `2008-09-19`. Nothing in the
chain ever looked at the stored value. `create_patient` now re-reads the record it just created and
compares the birthdate. A mismatch is reported as a **warning, not a failure** — the patient really
was created, and a clinician told "echec" would reasonably create them a second time:

> ATTENTION : la date de naissance enregistree est 2008-09-19, et non 2008-09-20 comme demande. Le
> dossier existe bien - ne le recreez pas - mais corrigez cette valeur dans OpenMRS.

### 6. Address and date filtering · **date filtering DONE, address BLOCKED**

A read-only probe of the live CapabilityStatement settled what the repo could not: `Patient`
advertises `_lastUpdated` and `birthdate` as search parameters, and nothing resembling a creation
date. So:

- `list_patients` accepts a window — "aujourd'hui", "hier", "cette semaine", "ce mois", "les N
  derniers jours", "depuis le JJ/MM/AAAA" — resolved against the clock in application code, never
  by the model.
- `combien de patients …` / `how many patients …` now matches a task at all. It previously matched
  none and was answered "je n'ai pas compris" — the exact phrasing from the screenshots.
- The reply says **"dossiers modifies depuis le …"**, never "crees", and adds "(OpenMRS ne permet
  pas de filtrer sur la date de creation)". Answering a question about creation with a count of
  modifications is acceptable only if the answer says that is what it did.
- The mock now honours `_lastUpdated` and **rejects** an unsupported prefix. Without that, FastAPI
  would have silently dropped the parameter and every test of the filter would have passed while
  proving nothing.

**Address writes are not shipped, on purpose.** Two blockers: nobody has ever called
`POST /ws/rest/v1/person/{uuid}/address/{id}` against real OpenMRS, so the body shape is unverified;
and `OperationTarget.PERSON_SUB_RESOURCES` lists only `attribute` and `name`, so an address edit
would be logged as a CREATE with no before-image and **could not be rolled back**. Shipping an
unreversible write path into a hospital record to save a probe is the wrong trade.

## Tier 3 — measurability

### 7. Real clinician transcripts · **RE-SCOPED, with evidence**

The premise was wrong, and measuring it is what showed why. `agentgateway_operation_log` records
the calls the agent makes *to OpenMRS*. A turn the assistant did not understand makes none — it is
answered from the interpreter and returns before a client exists. **Measured: four misunderstood
turns in a row produce zero audit rows.**

So the turns most worth learning from are precisely the ones invisible to the system of record.
They can only be counted in the agent, which is item 8. Capturing the sentences themselves stays a
supervised activity using `LOG_PROMPTS`, which now announces what it is doing.

### 8. Interpretation-quality counters · **DONE**

`app/telemetry.py`, exposed at `GET /metrics` behind the channel secret. Counts turns by state and
task, latency in buckets, frames abandoned **by the task they lost**, repair turns, slots rejected
**by which slot**, model unreachable / unusable / retried, and writes that OpenMRS accepted but did
not apply. Shape only — a test asserts no counter key can contain patient data.

These are the measurements items 13 needs and nothing could previously answer.

### 9. Model-call retry · **DONE, and the first version was wrong**

One retry, and only for failures a retry can fix. Timeouts are excluded: spending fifty of a
clinician's seconds to reach the same fallback is worse than reaching it once.

The first version retried malformed JSON with identical parameters — which at temperature 0
reproduces the identical answer. The diagnostic added alongside it caught this immediately:
`finish_reason=length, 573 chars`. The failure was a **token-budget cutoff**, so the retry now
raises the budget rather than repeating the question. (On the one live case that still fails, the
model runs away past 1085 characters too and the rules fallback correctly takes over — the retry
does not rescue everything, and the log now says which kind of failure it was.)

## Tier 4 — friction

### 10. Streaming · **DECLINED, cheaper equivalent shipped**

Streaming is the wrong trade here. The reply is one short paragraph, and the path is
browser → OpenMRS → agent whose middle hop reads the whole body before returning; streaming means
rebuilding the Java relay as a chunked proxy for a payload measured in sentences. The problem was
the **silence** — the input greyed out with nothing on screen. Three pulsing dots now appear where
the reply will land. Honours `prefers-reduced-motion`.

### 11. Conversation survives a page reload · **DONE**

`sessionStorage`, not `localStorage`: its lifetime matches the agent's own conversation buffer, so
the id cannot outlive the state it refers to. Storage unavailable falls back to the old behaviour.

Both verified against the real `agent-chat.js` and `agentgateway.css` in a harness: indicator
appears while waiting, disappears on reply, and the id survives a reload.

### 12. Undo from the chat · **DECLINED on design grounds**

Not merely blocked — deliberately forbidden. `AgentLogController`'s own javadoc and ADR-11 state
that rollback is *"never self-service for the clinician who made the original request"*. Three
mechanisms enforce it: the audit filter refuses any relayed path outside
`auditedPathPrefixes`; `requireRollback()` demands a privilege a clinician's chat token does not
carry; and a rollback-purpose token can only be minted Java-side with a private key the agent
service does not hold (it has the public half and only verifies).

Separation of duties in a hospital system is not an obstacle to route around. The verification
messages added in item 1 point at the administrator instead.

### 13. Tuning `MAX_REPAIRS` and the field aliases · **BLOCKED on data, which now exists**

`frame.abandoned.*`, `frame.repair` and `slot.rejected.*` from item 8 are exactly the measurements
needed. Guessing new values now would replace one unmeasured number with another. Re-read after a
few weeks of real use.

## New findings

| # | Finding |
| --- | --- |
| 44 | A write was reported as saved on its HTTP status alone, on a deployment with a known endpoint that returns 200 and changes nothing. |
| 45 | The field being changed was used as a patient search term: "modifie X" / "le nom" / "Walter Black" searched for a patient called Walter Black and reported no such patient. Finding 28's collision, inverted — a rename was impossible. |
| 46 | Patient data reached container logs regardless of `LOG_PROMPTS`: 13 unconditional log lines plus every `httpx` request URL. |
| 47 | Turns on one conversation were unsynchronised for the whole length of a turn. |
| 48 | A created record's stored values were never read back; a birthdate silently off by a day stayed in the record. |
| 49 | "combien de patients …" matched no task family and was answered "je n'ai pas compris". |
| 50 | No date filter existed, though the deployed fhir2 advertises `_lastUpdated` and `birthdate`. |
| 51 | A date was accepted as a phone number — `07/11/1965` passed the substring corroboration and the digit-count validator alike. |
| 52 | Turns the assistant failed to understand are invisible to the audit trail, so the most important quality signal was unmeasurable. |
| 53 | A truncated model answer was retried with identical parameters at temperature 0, reproducing it exactly. |
| 54 | `eval_nlu.py` hard-coded `sys.path.insert(0, "/srv/agent")` — the path inside the built image — so it silently measured the image's baked-in copy instead of the code under test. Two "no regression" measurements were taken that way before it was noticed. |
| 55 | The chat panel forgot the conversation on reload, and said nothing while waiting. |

## Remaining risks

1. **`agentgateway-1.1.5.omod` is built but not installed** on Server 1, so the two chat-panel
   fixes are not live. The agent service itself is fully deployed.
2. **No clinician turn has run against round 2 yet.** Everything testable from this host is
   verified; the full chain through OpenMRS's own token minting is not.
3. **`timezone.conversions = false`** remains, so birthdates may still be stored a day out. The
   agent now *reports* the discrepancy; it cannot fix it.
4. **Address, neuro scores and appointments** all remain blocked outside this repository.
5. **Rebuilding the module ships everything in the source tree**, not only the two frontend edits.
   The tree was at 1.1.4 and is now 1.1.5.
6. **The corpus is still ours, not the department's** — 28 sentences written by the builders. Item 8
   is the instrument; it needs real use to produce data.

## Adversarial review of this round's own changes

The change set was reviewed by independent agents, each finding then attacked by a separate
skeptic instructed to refute it. One lens (patient-data leakage) completed; the other four were
lost to a session limit and were done by hand afterwards. **Four real defects came out of it, two
of them mine from this round and one of them serious.**

| # | Finding |
| --- | --- |
| 56 | `OpenmrsClient.call` logged the request path verbatim on every non-2xx **and** every timeout. A patient search path *is* patient data. `main.py` raises the httpx logger to WARNING for exactly this reason - and the application then re-published the same URL one line later. Every test missed it because the mock answers every search with 200. |
| 57 | **A write named one patient and changed another.** With Cherif's chart open on the dashboard and Benali searched a turn earlier, the confirmation read *"Je vais MODIFIER la fiche du patient Benali Amine"* and the update landed on Cherif. Cause: `remember_patient` kept a known label when a new uuid arrived without one. The reasoning ("a blank summary is bad") was right about blanks and wrong about the trade - a *wrong* summary is far worse. A uuid that changes now clears the label that came with the old one, and an update derives the label from the record it already reads. |
| 58 | A repair question during a create announced a patient inherited from an unrelated earlier search - "patient : Benali Amine" while creating Karim Saidi. Frames no longer inherit a patient they have not resolved. |
| 59 | The verification mismatch message - which names the stored and requested values, e.g. two dates of birth - was logged in full. |

Two method lessons, both of which cost real defects:

- **A line-based grep cannot audit log calls.** The redaction pass in item 2 used one, and silently
  skipped a `log.info(` whose arguments were on the following line - which is how a raw prompt
  survived it. `tests/test_phi_logging.py` now walks the AST of every log call in `app/` and fails
  on any argument that is neither redacted nor on an explicitly reviewed allowlist.
- **A mock that only ever succeeds cannot test failure paths.** Finding 56 lived in two branches no
  test could reach, because the mock OpenMRS answers every seeded search with 200. Those branches
  now have tests that force a 403 and a timeout.

Each fix was verified by reverting it and watching the corresponding test fail.

## Honest limits of this round's verification

- **`_bundle_total` assumes the deployed fhir2 returns `total` on a searchset.** The mock does. The
  real server was not queried for it (it needs a delegated token this process cannot mint). If it
  is absent, a capped list under-reports rather than over-reports, and still discloses its filter.
- **The four hand-done review lenses are one person's reading**, not the independent adversarial
  pass the fifth got.
- **`tests/live_model_check.py` runs against the real weights but a mock OpenMRS.** No round-2 code
  has executed against the hospital's real data.
