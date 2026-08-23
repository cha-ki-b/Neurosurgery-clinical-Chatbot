# Clinical chatbot for the CHU Blida neurosurgery department — Phase 2

This delivers **Phase 2** of `documentation/Chatbot_and_report_architecture.md` §9: the agent
foundation, plus a working prototype of the assistant on top of it.

Phase 2 as written is "build `agentgateway` and exercise it with a stub caller". The stub here is
not a stub in the throwaway sense — it is the real Clinical Agent Service with a deterministic
interpreter in place of the LLM. It genuinely searches, reads, creates, updates and books against
OpenMRS's own APIs, under the requesting clinician's own privileges, with confirmation before
every write. That means the security and audit model is proven against real traffic now, and
Phase 3 replaces one class (`app/nlu/rules.py`) rather than the plumbing.

Two components:

| | Where it runs | What it is |
|---|---|---|
| `openmrs-module-agentgateway/` | Server 1, inside OpenMRS | The entire OpenMRS-side footprint: chat relay, delegated-token minting and verification, the audit filter, `agentgateway_operation_log`, and admin review/rollback. No clinical logic, no neurosurgery-specific logic. |
| `clinical-agent-service/` | Server 2 | FastAPI. One endpoint, `POST /chat`. Holds no OpenMRS account and no durable store. |

**Status: in deployment against the live instance.** Both servers are up and talking; the
module fix in 1.1.1 was required before a single chat turn could complete. See
[`IMPLEMENTATION-LOG.md`](IMPLEMENTATION-LOG.md) for what was found, what changed and what is
still outstanding, and [What still needs doing](#what-still-needs-doing) below.

---

## The security model in one page

Everything else follows from these five properties.

**1. The browser never talks to Server 2.** The channel secret would have to be readable by page
JavaScript, which means it would not be a secret. Browser → OpenMRS (same origin, existing
session) → `agentgateway` → agent service. ADR-12.

**2. Two independent trust boundaries.** A shared *channel secret* proves a request came from this
hospital's OpenMRS and not from anything else that can reach the port. A short-lived *delegated
token* says which clinician the turn is for. Neither substitutes for the other. ADR-9.

**3. Identity comes from one place only.** `agentgateway` signs the token; the agent service reads
the username out of its verified payload. There is no plaintext user id anywhere on the wire, and
no code path that accepts an identity asserted any other way. ADR-13.

**4. Every call runs as the clinician.** The audit filter authenticates the request as that
OpenMRS user, and OpenMRS's ordinary privilege checks then apply unchanged. The agent has no
account of its own and cannot widen anyone's access. On top of that sits an extra gate,
`App: agentgateway.chat.write`, so an administrator can switch off the chat's write capability
hospital-wide without touching anybody's clinical permissions. CA7, §6.

**5. Nothing is written without an explicit yes.** Every create, update and booking — not only the
ambiguous ones — is summarised in plain French and waits for confirmation. CA5, ADR-2.

### Request flow

```
Clinician's browser
  │  POST /openmrs/module/agentgateway/chat.form         (existing OpenMRS session)
  ▼
ChatRelayController          ── checks App: agentgateway.chat.use
  │                          ── mints an RS256 token: sub, may_write, purpose=chat, exp=+5min
  │  POST https://agent.hospital.lan/chat   + X-Agent-Channel-Key
  ▼
nginx (Server 2)             ── TLS; allow Server 1's address only, deny all
  │                          ── the agent's own port 8000 is never published
  ▼
Clinical Agent Service       ── verifies channel secret, verifies token signature
  │                          ── interprets, checks the tool registry, gates writes on a yes
  │  GET/POST /ws/fhir2/R4/…       + X-OpenMRS-Agent-Token
  ▼
AgentAuditFilter (Server 1)  ── path allowed? token valid? purpose right? privilege held?
  │                          ── authenticates as that clinician for this request only
  │                          ── captures a before-image for updates
  ▼
webservices.rest / fhir2     ── normal OpenMRS privilege checks, as that user
  │
  ▼
agentgateway_operation_log   ── append-only: who, what, before, after, reversible?
```

---

## Where this diverges from the architecture document

Each of these is a deliberate decision, not an oversight. Reviewers should push back on any they
disagree with.

**Asymmetric signing (RS256) rather than a shared symmetric secret.** ADR-13 requires the agent to
read identity from a token whose signature it has *verified*, which means it needs a verification
key. With a symmetric secret, the key that verifies is also the key that mints — so a compromised
agent service could forge a token for any user in the hospital. Signing with a private key that
never leaves OpenMRS and publishing only the public half removes that escalation path entirely.

**A per-request `UserContext` rather than overriding the platform authentication scheme.** ADR-9
suggests `openmrs-module-oauth2login`; §7 allows "an equivalent minimal filter using the same
approach". OpenMRS resolves a *single* global `AuthenticationScheme` bean, so taking that route
would put this module on the critical path of every login in the hospital — including the
administrator login someone would need to fix it — for a module whose stated requirement is never
to sit on the critical path of existing workflows. Instead the filter builds a throwaway
`UserContext` around the delegation scheme for one request and restores the previous one
afterwards. Same SPI, same result; a failure here can only break the chat.

**A `purpose` claim on the token.** Three things mint tokens: a chat turn, an administrator's
rollback, and this module reading a record's state back before overwriting it. Giving each its own
purpose means an administrator does not need chat-write access to reverse somebody's mistake, a
clinician's chat token cannot be replayed into the rollback path, and a read-only token cannot
write. The filter enforces the matching privilege per purpose.

**The audit filter is mapped on `/*`, not on the two API prefixes.** The prefixes it acts on are a
global property an administrator can extend at runtime (to add `/ws/rest/v1/patientview/`, for
instance); a filter mapping is fixed when the module loads. The cost is one `getHeader` call per
request, which is the first line of `doFilter`.

**No JWT library, no HTTP client library in the module.** The module mints and verifies tokens it
issued itself, in one pinned algorithm, and makes three outbound call shapes. A JWT library would
put a second copy of Jackson and its transitive tree into the module classloader of a deployment
that has already had a production incident from exactly that duplication. The crypto is the JDK's
(`SHA256withRSA`); what is hand-written is base64url and JSON framing, with the algorithm pinned
rather than read from the token header.

**A rule-based interpreter, per the build order.** Phase 2 is explicitly "no LLM yet". Rules make
the security model testable without a GPU, and every rule is enumerable, so a failing test points
at a rule rather than a sampling temperature.

Two behaviours in the interpreter are **not** phase-2 stopgaps and should survive the Phase 3
swap:

- *Descriptive phrasing is never an instruction.* "Le GCS s'est aggravé à 6" describes a course;
  "enregistre un GCS à 6" requests a write. Anything hedged, reported or interrogative produces a
  clarifying question. This is the exact risk §0 of the architecture document flags, and it is
  covered by tests.
- *A turn matching two task families is ambiguous, full stop.* Asking costs one turn; guessing
  wrong writes to the wrong place.

---

## Building and deploying

**[DEPLOYMENT-GUIDE.md](DEPLOYMENT-GUIDE.md) is the step-by-step version**, written for someone
following it without prior context, with a pass/fail check after every stage. This section is the
summary for someone who already knows the system.

The hospital: **Server 1 `10.0.211.249`** (`openmrs` / `orthanc` / `viewer`.hospital.lan, and the
`hospitalCA` authority), **Server 2 `10.0.211.250`** (`agent.hospital.lan`, this assistant).

### 1. Build and install the module

```bash
cd openmrs-module-agentgateway && mvn clean package
```

Produces `omod/target/agentgateway-1.1.0.omod`. Java 8 and Maven, matching the deployment
(OpenMRS 2.12.2 / platform 2.5.9, WAR 2.4.3, Tomcat 7).

Install through **Administration → Manage Modules → Add or Upgrade Module**. Liquibase creates
`agentgateway_operation_log` on start and the activator generates the signing key pair.

> **If you installed 1.0.0, upgrading is required, not optional.** In 1.0.0 the app-framework
> definitions were in a file named `agentgateway_app.json`. OpenMRS keys that loader on the file
> *name*: `*app.json` is parsed as `AppDescriptor`, `*extension.json` as `Extension`. Those are
> different classes, the file held extensions, and appframework's Jackson 1.x rejects unknown
> properties — so it was dropped at load with one line in the log. Everything else worked, which
> is exactly why it was confusing: the module started, its settings appeared, its endpoints
> answered, and no UI rendered anywhere. `ModuleWiringTest` now fails the build if that naming
> rule is ever broken again.

### 2. Configure OpenMRS

**Administration → Settings → Agentgateway**:

| Setting | Value |
|---|---|
| `agentgateway.agentServiceUrl` | `https://agent.hospital.lan` — no port. TLS is on 443 and the agent's own 8000 is never published |
| `agentgateway.channelSecret` | `openssl rand -base64 48`. The chat refuses to start while empty |
| `agentgateway.agentTimeoutMillis` | `30000` — must stay below the proxy's 60s read timeout |
| `agentgateway.selfBaseUrl` | `http://localhost:8080/openmrs` — loopback, never leaves the machine |
| `agentgateway.auditedPathPrefixes` | `/ws/rest/v1/,/ws/fhir2/R4/` (default) |
| `agentgateway.captureBeforeState` | `true` — off makes updates non-reversible, and the log says so |

Copy `agentgateway.signingPublicKey` (one line, no wrapping) for step 4. Never copy
`agentgateway.signingPrivateKey`: anyone holding it can impersonate any user.

### 3. Assign the privileges

**Administration → Manage Roles**, per §6:

| Privilege | Roles |
|---|---|
| `App: agentgateway.chat.use` | Surgeon, OR Nurse, Radiologist/Technician, Admissions Staff |
| `App: agentgateway.chat.write` | Surgeon, Admissions Staff — the roles already allowed to create/edit patients by hand |
| `App: agentgateway.rollback` | System Administrator only |

**Nothing appears in the UI until this is done** — every entry point is privilege-gated.

### 4. Deploy the agent service

Use **[`../server2-stack/`](../server2-stack/README.md)**: Nginx terminating TLS, the agent
container publishing nothing, and the assistant's vhost closed to everything but `10.0.211.249`.

```bash
cd ../server2-stack
cp .env.example .env      # add AGENT_CHANNEL_SECRET and OPENMRS_JWT_PUBLIC_KEY
./1-make-agent-csr.sh     # then sign on Server 1 with 2-sign-agent-csr.sh
docker compose up -d
```

The certificate is issued by the **existing hospitalCA on Server 1** rather than a second
authority — the CA key never travels, only the signing request does.

The step that is easy to miss and impossible to diagnose from the chat: **import hospitalCA into
the OpenMRS container's Java trust store**. Java keeps its own list, the hospital CA is not on it,
and without the import every relay fails as `SSLHandshakeException` while the clinician just sees
"assistant indisponible". `2-sign-agent-csr.sh` prints the exact commands.

`clinical-agent-service/docker-compose.yml` remains for single-machine development — no TLS,
loopback only. Do not run it on Server 2: it publishes the raw port, which makes the proxy's
access rules optional.

### 5. Check it came up

```bash
curl -fsS --cacert certs/hospitalCA.crt https://agent.hospital.lan/health
```

`fhir_capabilities_known: true` means it read the deployed `fhir2` module's capability statement.
Then, from Server 1:

```bash
curl --cacert ~/certificates/hospitalCA.crt -H "X-Agent-Channel-Key: $AGENT_CHANNEL_SECRET" https://agent.hospital.lan/capabilities
```

Every tool reports `available: true` or `false` **with a reason** — a task whose FHIR resource
this `fhir2` version does not advertise is declared unavailable rather than failing obscurely
mid-conversation, which is the point of reading `/ws/fhir2/R4/metadata` live instead of hardcoding
a list (ADR-10, §8 #5). **Re-check after every `fhir2` upgrade.**

### 6. Where the assistant appears

Four entry points, all gated on `App: agentgateway.chat.use`:

| Where | What |
|---|---|
| **Patient dashboard, right column** | an **"ASSISTANT CLINIQUE"** chat box, embedded via `patientDashboard.secondColumnFragments` — the patient is already in context, so the clinician never has to say who they mean |
| Patient dashboard actions | "Assistant clinique" link → full-screen chat for that patient |
| Home page | "Assistant clinique" link → full-screen chat, no patient |
| Direct URL | `/openmrs/agentgateway/chat.page` — works regardless of extension points, useful for diagnosing |

Administrators additionally get "Opérations de l'assistant clinique" under system administration,
or `/openmrs/agentgateway/operationLog.page`.

**One gap worth knowing about.** The embedded widget attaches to the *standard* OpenMRS patient
dashboard. Neurosurgeons spend their day on `patientview`'s dashboard instead, and extension
points do not carry across to it. Putting the assistant there is a single line added to
`patientview`'s `patient.gsp`:

```groovy
${ ui.includeFragment("agentgateway", "chatWidget") }
```

That works unchanged — the fragment takes the patient from the `patientId` request parameter,
which `patientview`'s pages already carry. It is left out of this delivery because it is an edit
to a different module, and this one is meant to stay independent of it (ADR-5).

---

## Verifying

```bash
cd openmrs-module-agentgateway && mvn test          # 58 tests
cd clinical-agent-service && python -m pytest       # 81 tests
```

Both suites currently pass. What they actually cover:

- **`DelegatedTokenTest`** — a tampered payload, a token signed by another key, an `alg: none`
  token, a wrong audience or issuer, a missing expiry, an unknown purpose, and expiry itself are
  each refused.
- **`RollbackEngineTest`** — the coherence rule, including every case where the engine lacks
  information (no before-image, unreadable state, a resource type it cannot interrogate for
  dependents) resolving to "a human has to do this" rather than an attempted reversal.
- **`ModuleWiringTest`** — every endpoint gates itself on a privilege; every privilege is
  `App:`-prefixed (an unprefixed one is auto-granted to every role on a Reference Application
  install and gates nothing); mapping files declared are shipped; the filter is mapped widely
  enough to cover prefixes added later; every CSS class is `agent-` prefixed; and every
  app-framework JSON file is *named* for what it contains — the check that reproduces the 1.0.0
  UI bug and fails the build on it.
- **`test_chat_end_to_end.py`** — over real HTTP against a mock OpenMRS that enforces the same
  gates: nothing is written on the turn that asks for it, a read-only clinician cannot get a write
  even planned, a privilege revoked mid-conversation stops a pending write, a pending action
  cannot be confirmed by a different clinician, an unclear answer to "shall I save this?" is not
  read as yes, an unsupported task is declared rather than attempted, and an update preserves the
  fields nobody mentioned.

Docker is not required for either suite.

---

## What the assistant can do today

French and English, conversational, with the clarification loop carrying context across turns.

| Task | API | Write? |
|---|---|---|
| Rechercher un patient | `GET /ws/fhir2/R4/Patient?name=…` | no |
| Afficher un dossier et les derniers passages | `GET Patient/{id}`, `GET Encounter?patient=…` | no |
| Créer un patient | `POST /ws/fhir2/R4/Patient` | **yes** — confirmation + duplicate warning |
| Mettre à jour les données administratives | `PUT /ws/fhir2/R4/Patient/{id}` | **yes** — confirmation |
| Programmer un rendez-vous | `POST /ws/fhir2/R4/Appointment` | **yes** — only if `fhir2` advertises Appointment |
| Enregistrer un GCS / Karnofsky | `POST /ws/rest/v1/patientview/neuroassessment` | **yes** — see the prerequisite below |

A worked example:

> **Clinicien** — crée un patient nommé "Amine Benali", sexe masculin, né le 03/04/1978
>
> **Assistant** — ATTENTION : des dossiers portant un nom proche existent déjà :
> — Benali Amine - 1978-04-03
> Vérifiez qu'il ne s'agit pas d'un doublon avant de confirmer.
>
> Je vais CRÉER un nouveau dossier patient avec les informations suivantes :
> — Nom : Amine Benali — Sexe : masculin — Date de naissance : 1978-04-03
> Aucun dossier existant ne sera modifié. Confirmez-vous la création ?
>
> **Clinicien** — oui, je confirme
>
> **Assistant** — C'est enregistré. Le dossier a été créé : Benali Amine - 1978-04-03.

---

## Rollback

`agentgateway_operation_log` records every agent-originated call, successful or not, with the
acting user, the endpoint, the request, the response, and — for updates — the record's state
beforehand.

An administrator opens an entry, runs the check, and only then gets a button. The check asks
questions about *this* operation's aftermath, not about its resource type:

- Has the record been edited since we wrote it? → **manual** (reversing would discard that edit)
- Has the agent written to it again since? → **manual** (roll that one back first)
- For a create: does anything now depend on it — a visit, an encounter, an observation? →
  **manual** (voiding it would orphan them)
- For a create of a type we cannot interrogate for dependents? → **manual**, always. There is no
  generic way to ask OpenMRS "what refers to this", and guessing is how you orphan data
- For an update: is there a before-image, and can every field the agent set be restored? →
  reversible, restoring **only** the fields the agent changed
- A void? → **manual**. Un-voiding is not exposed over the REST API

A rollback is itself an ordinary logged operation, attributed to the administrator, pointing back
at what it reversed. Nothing is ever edited out of the trail.

---

## Current state

Working live against the hospital's OpenMRS as of 2026-08-18: searching for a patient, reading a
record, and **creating a patient from a French sentence** — with a duplicate warning, an explicit
confirmation, an OpenMRS-issued identifier and a full audit row. Updating is implemented but not yet
exercised live. Booking an appointment and recording a neuro score are blocked for reasons that are
not code — see [`IMPLEMENTATION-LOG.md`](IMPLEMENTATION-LOG.md) Findings 6 and 11, and §4.3.

Getting there took ten findings, most of them properties of this deployment rather than of the code:
they are all recorded in the implementation log, with the evidence for each.

## What still needs doing

### Before this can be used on the live instance

1. **Deploy and smoke-test it.** Module 1.1.1 is built but not yet installed; the Server 2
   stack is running. Confirm that module filters run after `OpenmrsFilter` on this Tomcat build —
   the design relies on a `UserContext` already existing when the audit filter runs, and it is
   worth confirming rather than assuming.

   Two faults found during deployment are recorded in
   [`IMPLEMENTATION-LOG.md`](IMPLEMENTATION-LOG.md): tokens could not be minted for accounts
   without a username (fixed in 1.1.1), and Server 2's `.env` held the signing *private* key
   where the public key belongs (being rotated). Neither was visible from the test suite, because
   both are properties of the deployment rather than of the code under test.

   Note also that `openmrs-app` runs with **no volume mounts**: the installed module and the
   hospitalCA truststore import live in the container's writable layer, so recreating the
   container discards both. `docker restart` is safe; `docker compose down && up` is not.
2. **Check `POST /ws/fhir2/R4/Patient` against the real instance.** Creating a patient needs an
   identifier of a type this hospital issues. Set `OPENMRS_PATIENT_IDENTIFIER_SYSTEM` accordingly
   and confirm the created patient is complete, not just accepted.
3. **Dry-run a rollback** on a throwaway patient before letting anyone use the write path, per §9
   phase 4.
4. **Confirm the `Rapport_3` privilege question** (§8 #7) if `medreport.imaging.*` ships alongside.

### Prerequisite for the neurosurgery-specific tools (architecture §4.3)

The GCS/Karnofsky tool is registered but reports itself unavailable, because it cannot work yet:
`patientview`'s existing endpoints (`/module/patientview/addNeuroAssessment.form`) key on the
internal **numeric** `patient_id`, which the REST API does not expose — so nothing outside OpenMRS
can address a patient there. `patientview` needs to expose its `PatientviewService` methods as
ordinary `webservices.rest` custom resources under `/ws/rest/v1/patientview/…`, keyed on UUID,
exactly as §4.3 prescribes.

Once that lands: set `PATIENTVIEW_TOOLS_ENABLED=true` **and** add `/ws/rest/v1/patientview/` to
`agentgateway.auditedPathPrefixes`. Both, or the tool stays unreachable.

This is additive to `patientview` — new REST classes, no change to existing pages or services —
and is what any external integration would need, chatbot or not.

### Phase 3 (the model) — code complete, awaiting the GPU

`MedGemmaNlu` (`app/nlu/medgemma.py`) implements the same `NluEngine` interface as the rules engine
and is selected with `NLU_ENGINE=medgemma`. Its output is constrained to a JSON schema **generated
from the tool registry** (`app/nlu/schema.py`), so it cannot name a task that does not exist. Three
checks do not trust it: descriptive phrasing never becomes a write, slot values it reports but the
sentence does not contain are dropped on writes, and an unknown task is refused rather than repaired.
Any model failure falls back to the rules engine for that turn.

vLLM is a compose overlay (`../server2-stack/docker-compose.vllm.yml`), not part of the base stack:
with `NLU_ENGINE=rules` the assistant runs with no GPU at all.

Outstanding: the weights are not downloaded and the model has never been served. §8 #1 — whether
tool selection is reliable at 4B on real French clinical phrasing — remains an open empirical
question, and the answer is a measurement, not an opinion. The plan for taking it from here, with the
exact commands, configuration and tests, is [`MEDGEMMA-PLAN.md`](MEDGEMMA-PLAN.md).

### Phase 4 (agent, writes)

The write path is implemented and tested here, so Phase 4 is largely validation on the real
instance rather than new code: the confirmation gate, the audit trail and rollback all exist.

### Known limitations

- **A token is replayable within its lifetime.** Mitigated by a five-minute expiry, TLS, and the
  channel secret; there is no `jti` replay cache, because the same token is legitimately used for
  several calls within one turn. A per-turn nonce would need the filter to share state across
  requests. Worth revisiting if the token lifetime is ever raised.
- **The conversation buffer is per-process.** Running more than one replica needs Redis behind
  `app/conversation.py`, or sticky sessions. One replica is enough for one department.
- **Response bodies are recorded up to `agentgateway.maxLoggedBodyChars`** (20,000 by default) and
  marked truncated beyond it. Request bodies over 1 MB are streamed through unbuffered and
  recorded as omitted rather than held in memory.
- **The audit log contains PHI**, unavoidably — the prompt and the data it produced are the only
  way an administrator can review or reverse anything. It lives on Server 1, inside the existing
  system of record and its access controls. Server 2 keeps nothing (§1.3).
- **`LOG_PROMPTS` defaults to false** for the same reason. Turn it on only while debugging.

---

## Layout

```
openmrs-module-agentgateway/
  api/  … security/       RsaJwt, DelegatedTokenService, DelegatedAuthenticationScheme
        … rollback/       OperationTarget, RollbackEngine — the coherence rule
        … api/            AgentGatewayService, the DAO, AgentOperationLog
        … resources/      liquibase.xml, the Hibernate mapping, Spring wiring
  omod/ … web/filter/     AgentAuditFilter — the single enforcement point
        … web/controller/ chat relay, admin log + rollback, public key
        … webapp/         chat.gsp, operationLog.gsp, agent- prefixed CSS and JS

clinical-agent-service/
  app/  security.py       channel + token verification
        orchestrator.py   the §4.2 pipeline and both gates
        tools/            the registry and the catalog — FHIR and patientview families
        nlu/              base.py is the seam Phase 3 replaces
        capabilities.py   reads /ws/fhir2/R4/metadata live
  tests/mock_openmrs.py   an OpenMRS that enforces the same gates as the real one
```
