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
