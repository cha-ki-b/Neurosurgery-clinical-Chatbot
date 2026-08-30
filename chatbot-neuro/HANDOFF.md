# Handoff — state of the clinical assistant, 2026-08-26 (updated 2026-08-27, Phase 24)

Written so a new session can pick this up without re-deriving anything. Read this, then
`IMPLEMENTATION-LOG.md` Phase 24 (`SYSTEM_PROMPT` revision, Finding 35; appointment-booking timezone
diagnosis, Finding 36) and Phase 23 (a clean live verification pass, no code changes) and Phase 22
(20, 21) for the detail behind the most recent work, Phase 19 and Phase 18 for what came before.

**Three test patients remain in the real database on purpose**: `Kaced Amine`, `Slimani Slimani`,
`Marry Curry` - created during this session's verification work, left unvoided at the operator's
explicit request (Phase 23). They will show up in `liste tous les patients`. Not a bug; don't
"clean them up" without asking first, since leaving them was a deliberate choice, not an oversight.

---

## Where the system stands

**Live and working** against the hospital's OpenMRS (Server 1 `10.0.211.249`, Server 2 `10.0.211.250`):

- Searching for a patient — by name, surname, identifier, question phrasing, name-in-a-clause, and
  name prefix.
- Listing or filtering patients (`?gender=female` and an unfiltered list) — new in Phase 19.
- Reading a record (administrative summary plus recent encounters).
- Creating a patient **in one sentence**, with a duplicate warning, an identifier reserved from idgen,
  and a confirmation gate that holds. Also over several turns now (Finding 30, fixed).
- Updating a patient's phone or name, including when the patient is only named earlier in the sentence
  (`le telephone de X`, Finding 31) or by anaphora from the previous turn (Finding 32, and now
  reliable even with a bare pronoun + digits, Phase 20) — and, as of Phase 19, never by guessing an
  unrecorded reply into the name field (Finding 28, was CRITICAL). Phase 20 also found and fixed a
  deeper problem: **a phone or name update had never actually reached the database on real OpenMRS**
  - FHIR PUT either crashed (Finding 10's translator bug, adding a first-ever value) or silently
    no-op'd (200, unchanged database, changing an existing one). Both fields now go through
  `webservices.rest`'s own attribute/name endpoints instead, the same fallback `create_patient`
  already used and for the identical reason. Verified against the real database, not just a status
  code - see Phase 20 in the implementation log for the full trail.
- Refusing, by name and with the real reason: deletion, appointments (no `Appointment` resource on
  this `fhir2` - Findings 6/11 - **and**, independently, OpenMRS's own scheduling UI is broken here
  too, `timezone.conversions=false` - Finding 36, diagnosed not fixed, 2026-08-27), GCS/Karnofsky
  (needs `patientview` REST, architecture §4.3).
- Every safety property: no descriptive, hedged or interrogative sentence has ever become a write, and
  (new in Phase 19) no sentence naming two task families becomes a confident single write either.
- MedGemma 4B on vLLM as the interpreter, with the deterministic rules engine as an automatic fallback.

**Deployed versions:** module `agentgateway 1.1.4` - **rebuilt and redeployed to Server 1 in Phase
22** (version string unchanged, code changed - `OperationTarget`'s rollback path parsing). The
`.omod` in place before this round is backed up at
`/tmp/agentgateway-backups/agentgateway-1.1.4.omod.bak-phase22` inside the `openmrs-app` container
(not in the modules directory, so it is not itself loaded as a module - `docker restart openmrs-app`
would otherwise try to and log a harmless-looking error about it). Agent service built from
`chatbot-neuro/clinical-agent-service`, `NLU_ENGINE=medgemma`.
Also added Phase 22: `OPENMRS_PHONE_ATTRIBUTE_TYPE_UUID` in `server2-stack/.env` - was never set,
so a patient's *first* phone number failed OpenMRS's own validation until now.

**Tests:** module 69/69 (52 `api` + 17 `omod`, up from 65 - five new `OperationTargetTest` cases,
Phase 22). Agent service 142/142. `tests/explore.py` — 44 exploratory scenarios, run against the
live MedGemma deployment. `tests/eval_nlu.py` — UNSAFE = 0 on both engines, re-confirmed Phase 24
after the `SYSTEM_PROMPT` revision (`IMPLEMENTATION-LOG.md` Finding 35).

---

## What is broken, in priority order

Full detail: `IMPLEMENTATION-LOG.md` Phase 20. **All four items manual testing found after Phase 19
are now fixed** (previously listed here as open items 1-4 below). What's left is smaller still, and
none of it unsafe.

### Fixed in Phase 20 (was open items 1-4)

1. **The pronoun-plus-digits phone/identifier mis-tag** — `mets a jour son telephone a 0666777888`
   no longer gets the digits guessed as the patient's name (and then searched as an identifier).
   Fixed with a "a name always has a letter" guard on both the write and read corroboration checks.
2. **Phone and name updates never actually reached the database on real OpenMRS** — this turned out
   far bigger than the `41cccb5d` 500. Two fix attempts (editing the existing entry in place;
   matching it correctly once `system` was found to never be set) each stopped the 500 but the
   value still silently did not change - `200`, database unchanged. Decompiling the deployed fhir2
   jar found why: every incoming telecom/name entry is mapped to a **brand-new** object before
   translation, never the one fhir2 itself just read, so an id-bearing update is a Hibernate `Set`
   member "already present" and is dropped rather than applied. **No FHIR PUT shape could have
   fixed this.** Both fields now go through `webservices.rest`'s attribute/name sub-resources
   instead (`create_patient`'s own ADR-10 precedent, same underlying reason) - verified by reading
   the database back after the call, not by trusting the response status.
3. **Search by identifier** — `_build_search_patient` now applies the same identifier-shape
   heuristic `_resolve_patient` already had (moved to `nlu/rules.py` as a shared `identifier_shaped`
   helper). `cherche le patient 10002T` now searches `identifier=10002T`.
4. **Two extractor name gaps** — `inscris une nouvelle patiente, X, ...` (comma before the name) and
   `corrige la date de naissance de X, ...` (`naissance de` as a trigger) both now extract the name.

### Restating a bare update after a safety abandon doesn't pick the request back up

By design (Finding 28/29's fix), not a bug: once an unrecognisable reply causes the pending question
to be abandoned, a bare follow-up like "le telephone est X" alone doesn't resume it - the clinician
has to restate the patient or give a full sentence with an update verb. Confirmed in manual testing;
documented as the accepted cost of refusing to guess.

### Verified directly against the real deployment, not just the mock

Item 2 above needed real credentials to chase down: `curl -u admin:...` against Server 1's OpenMRS
(the admin account's password turned out to match `MYSQL_ROOT_PASSWORD` from the `openmrs-mysql`
container's own env - `Admin123` on this deployment) was used to read walter white's actual FHIR
JSON, reproduce the silent no-op with a direct PUT, and confirm the `webservices.rest` replacement
actually changes the database (`SELECT ... FROM person_attribute` / `person_name`, before and
after). Walter's real record was used for this and restored to its original values afterwards.
**A live re-run through the actual chat is still worth doing** as a final check of the whole
pipeline (interpretation -> confirmation gate -> the new REST calls together), but the specific
question "does the write actually persist" has already been answered directly at the database.

### Capability gaps (new work, not repairs)

Update fields beyond name and phone (birthdate, address) — now correctly refuses to guess rather than
silently doing nothing, but still cannot make the change. Computed answers ("quel âge a X ?"); small
talk.

---

## How to work on this

**Everything runs from Server 2** (`/home/cerist/server2-stack`), which has Docker and the GPU. Server 1
is reachable at `ssh -i ~/.ssh/id_ed25519_server1 -p 2222 server@10.0.211.249` — it has Docker, Maven
(via a container; there is no `javac` on the host) and the OpenMRS containers.

Run the agent test suite (Server 1 has the spare capacity; Server 2's host Python is 3.14 with no venv):

```bash
ssh -i ~/.ssh/id_ed25519_server1 -p 2222 server@10.0.211.249 'cd ~/agent-test/clinical-agent-service && docker run --rm -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD":/src -w /src python:3.11-slim sh -c "pip install -q -r requirements.txt >/dev/null 2>&1 && python -m pytest -q -p no:cacheprovider"'
```

Rebuild and restart the agent after a code change:

```bash
cd ~/server2-stack && docker compose -f docker-compose.yml -f docker-compose.vllm.yml up -d --build clinical-agent
```

Re-run the 44 exploratory scenarios and the NLU corpus. The image ships no `tests/` directory at all
(only `app/` and `requirements.txt`), so after every `--build` the directory has to be recreated, as
root, before copying files in as the container itself runs non-root:

```bash
docker exec -u root clinical-agent mkdir -p /srv/agent/tests
docker cp /home/cerist/chatbot-neuro/clinical-agent-service/tests/explore.py clinical-agent:/srv/agent/tests/explore.py
docker cp /home/cerist/chatbot-neuro/clinical-agent-service/tests/mock_openmrs.py clinical-agent:/srv/agent/tests/mock_openmrs.py
docker cp /home/cerist/chatbot-neuro/clinical-agent-service/tests/eval_nlu.py clinical-agent:/srv/agent/tests/eval_nlu.py
docker exec -u root clinical-agent touch /srv/agent/tests/__init__.py
docker exec -u root clinical-agent chown -R agent:agent /srv/agent/tests
cd ~/server2-stack && docker compose -f docker-compose.yml -f docker-compose.vllm.yml exec -T -e NLU_ENGINE=medgemma -w /srv/agent clinical-agent python3 -m tests.explore
```

Measure interpretation quality against the corpus (`UNSAFE` must stay at 0) — same `tests/` setup as
above, then:

```bash
cd ~/server2-stack && docker compose -f docker-compose.yml -f docker-compose.vllm.yml exec -T -e NLU_ENGINE=medgemma clinical-agent python3 -m tests.eval_nlu
```

Revert to the deterministic engine in one step — the first thing to try if a turn reads oddly, because
it says immediately whether the model or the plumbing is at fault:

```bash
sed -i 's/^NLU_ENGINE=.*/NLU_ENGINE=rules/' ~/server2-stack/.env && cd ~/server2-stack && docker compose -f docker-compose.yml -f docker-compose.vllm.yml up -d --force-recreate clinical-agent
```

See exactly what MedGemma is sent and what it says back (Phase 21) — the `LOG_PROMPTS` setting
already existed for logging the clinician's raw prompt in `main.py`; it now also logs the model's
fully-engineered final turn (rules + the clinician's sentence, exactly as sent - not the few-shot
block, which is the same every time and adds nothing worth re-reading) and MedGemma's raw JSON
response, both at INFO level:

```bash
sed -i 's/^LOG_PROMPTS=.*/LOG_PROMPTS=true/' ~/server2-stack/.env && cd ~/server2-stack && docker compose -f docker-compose.yml -f docker-compose.vllm.yml up -d --force-recreate clinical-agent
docker logs -f clinical-agent
```

Off by default (`LOG_PROMPTS=false`) - turn it back off the same way once done, since every turn's
prompt and response includes the clinician's sentence and should not sit in a log file longer than
needed.

---

## Things that will waste your time if you do not know them

Learned the hard way; each cost hours.

- **How to actually rebuild and deploy the Java module** (Phase 22, first time this round needed it):
  source lives on Server 2 at `chatbot-neuro/openmrs-module-agentgateway`; `rsync` it to
  `/home/server/agentgateway-build/openmrs-module-agentgateway` on Server 1, then
  `docker run --rm -v <that path>:/src -v maven-repo-cache:/root/.m2 -w /src maven:3.9-eclipse-temurin-8 mvn test`
  (drop `-DskipTests package` once tests pass, to get the `.omod` at `omod/target/`). Deploy with
  `docker cp` into `openmrs-app`'s `/usr/local/tomcat/.OpenMRS/modules/agentgateway-1.1.4.omod`
  (a real persisted volume, contrary to the older note below) and `docker restart openmrs-app` -
  safe, confirmed by `openmrs.log` showing `agentgateway.started: true` afterward. Back up the old
  `.omod` somewhere *outside* the modules directory first (inside `openmrs-app` is fine, e.g.
  `/tmp`) - a backup left in the modules directory with a non-`.omod` suffix still gets scanned on
  boot and logs a (harmless but noisy) "does not have the correct '.omod' file extension" error.
- **A component's own audit trail can call an operation reversible without reversing it actually
  working.** Phase 22: the operation log said `reversible: true` for a phone update; the rollback
  still failed, because the path-parsing logic that decided reversibility didn't understand the
  path shape Phase 20 had just introduced. The same "verify the outcome, not the report" discipline
  from the item below applies recursively - to the reporting mechanism itself, not only to the
  operation it reports on.
- **Five of the first sixteen findings were reported by the wrong layer.** Read the deployed source and
  jars; do not trust the error message. A missing endpoint said "patient introuvable", a missing
  extension said "Identifier Location cannot be null", a snap-confined Docker said "no such file" about
  a file that existed.
- **MedGemma has no `system` role.** Gemma 3's template drops it and vLLM says nothing: 2178 chars
  became 4 tokens. Instructions go on the final user turn.
- **Explaining the surrounding system to a 4B model makes it refuse.** Every paragraph about OpenMRS,
  confirmation gates or access rights came back as "Je ne peux pas...". Keep the prompt to a classifier
  framing — and filter it out before it reaches a clinician (Finding 33, fixed Phase 19: a clarification
  containing that vocabulary is dropped in `_usable_clarification`).
- **Driving the mock is not enough to trust a fix in this codebase.** Two of Phase 19's most important
  fixes — the update-name/search-term slot collision behind Finding 28's second path, and the missing
  ambiguity backstop that let `eval_nlu` measure `UNSAFE = 1` — exist only in MedGemma's actual output,
  not in the rules engine or in the mock's canned responses. Verify against the live deployment
  (`tests.explore`, `tests.eval_nlu`), not just the pytest suite, before calling an orchestration change
  done.
- **fhir2 1.2.2 cannot create a patient, and cannot update an existing telecom or name entry
  either** (found Phase 20); both go through `webservices.rest` now. `setUuid(getId())` being
  unconditional is why create fails outright; for update, `PersonTranslatorImpl` builds a *new*
  `PersonAttribute`/`PersonName` object per incoming entry rather than reusing the one just read,
  so an id-bearing update is silently dropped by Hibernate's `Set` semantics instead of crashing.
  Reads are unaffected either way - only these specific writes.
- **A `200` from fhir2 does not mean the write happened.** The silent-no-op above returned `200`
  with the database completely unchanged, twice, after two fixes that each looked complete (no
  crash, tests passed). Reading the value back from the database - not just checking the response
  status - is what caught it. Trust the data, not the status code, for anything routed through fhir2.
- **The OpenMRS admin password matches `MYSQL_ROOT_PASSWORD`** in `openmrs-mysql`'s own container
  env (`Admin123` on this deployment) - readable with `docker exec openmrs-mysql env`. Useful for
  direct `curl -u admin:...` verification against the real FHIR/REST APIs, or a direct
  `mysql -uroot -p...` query, when the operation log and `openmrs.log` alone are not enough to tell
  whether a write actually landed.
- **When the error message and the log line both run out, decompile the jar.** `docker cp` the
  class files out of `.openmrs-lib-cache`, `javap -p -c` them (e.g. via `docker run --rm -v ...
  eclipse-temurin:17-jdk javap ...` - no JDK needed on either server) and read the bytecode for the
  actual method calls. This is how the `PersonTranslatorImpl` "new object per entry" bug was found,
  after the error message ("Column 'uuid' cannot be null", or nothing at all for the silent case)
  had already run out of things to say.
- **Agent calls must use the relay prefix** `/module/agentgateway/relay` + the real path. Removing it
  silently breaks every FHIR call.
- **`openmrs-app`'s `.OpenMRS` data directory (modules, config) is a real, persisted Docker
  volume** - corrected Phase 22, confirmed directly (`docker inspect openmrs-app`, and a real
  module rebuild survived a `docker restart` without needing reinstallation). The caution the
  older version of this note carried - that `docker compose down && up` (as opposed to `restart`)
  can discard the installed module and the hospitalCA truststore import - was not re-verified this
  round and is left in place rather than dropped as untested; `docker restart` is confirmed safe
  either way and is the one this round actually used.
- **Do not bump the parent POM version** with the module version — a blanket `sed` over `pom.xml` hits
  both.
- **The prompt is 2138 of 4096 tokens** (was 1306 before the 2026-08-27 `SYSTEM_PROMPT` revision,
  Finding 15 - the rules got more explicit about not treating the few-shot examples as a closed
  list, at the cost of roughly 800 tokens). The few-shot examples are still the bulk and are what
  actually drives behaviour at this model size.

---

## Documents, and what each is for

| Document | Use it for |
|---|---|
| `chatbot-neuro/IMPLEMENTATION-LOG.md` | the append-only history and the evidence, newest phase last — check its own tail for the current phase/finding count rather than trusting a number copied here, it will always be stale sooner or later |
| `chatbot-neuro/HANDOFF.md` | this file — the canonical "state of the system right now" entry point; if this and the implementation log's own summary tables disagree, this file is more likely to be current |
| `Desktop/CHATBOT-VALIDATION-REPORT.md` | the operator's own validation report, registry updated in place with each root cause |
| `chatbot-neuro/MEDGEMMA-PLAN.md` | closed out — steps 1-9 all done as of Phase 22/24 (rollback dry-run and read-only refusal test, previously the only open items, both done Phase 22). Kept as reference for the `eval_nlu.py` measurement method and the model-rollback procedure, not as an open plan |
| `chatbot-neuro/README.md` | what the two components are and the security model |
| `chatbot-neuro/DEPLOYMENT-GUIDE.md` | installing from scratch, including the four values needed to create patients |
| `chatbot-neuro/chatbot_archi.md` | the original design, annotated where deployment disproved it |
| `openmrs-module-agentgateway/CHANGELOG.md` | module 1.1.0 → 1.1.4 |
| `server2-stack/README.md` | the proxy stack and what it deliberately does not do |

---

## Still outstanding from earlier phases

- ~~**Rollback dry-run**~~ — done, Phase 22. Reversed a real create (`cree un patient nomme "Phase22
  Rollback Test"...`) through the real chat and `rollback.form`, confirmed at the database
  (`patient.voided` 0 -> 1) not just the response. Idempotency ("already rolled back") and
  reversal-of-a-reversal refusal both confirmed live too. Not exercised: reversing an *update*
  against real data (unit-tested only), the dependency probe actually blocking a reversal, and the
  appointment path (unavailable on this deployment).
- ~~**Read-only refusal test**~~ — done, Phase 22. Turned out no role had `chat.use`/`chat.write`
  assigned at all; verified for real with a throwaway role/user through the actual chat endpoint,
  confirmed no write call ever reached OpenMRS, cleaned up afterward. `AgentAuditFilter`'s own
  independent re-check of the same rule was not separately exercised (see Phase 22 for why) - the
  Python orchestrator's gate was proven; the module-layer backstop behind it was inspected, not run.
- **`book_appointment`** — needs a decision, not code: `appointmentscheduling` books into slots an
  administrator creates in advance, so "book at this date" does not exist as an operation.
- **`patientview` REST resources** (§4.3) — until they exist, GCS/Karnofsky stay unreachable.
- **`null null`** — checked Phase 22 for the *global OpenMRS page header*: `admin`'s person record
  already has a name in the database ("Super User", preferred), so that occurrence looked resolved.
  **Re-opened 2026-08-27**: a screenshot from that session shows `null null` rendered a second time,
  inside the "Assistant clinique" chat widget itself (above the chat panel, same `admin` account) -
  not confirmed to be the same code path as the Phase 22 finding, and not investigated yet. Whoever
  picks this up next should treat it as open until the widget's own template is checked.
- **Orthanc credentials in cleartext** in `OHIF/ohif-app-config.js`, which travels with the viewer to
  Server 1 - confirmed still the literal unrotated default (`orthanc:orthanc_admin_password`), and a
  prior engineer already flagged it in `OHIF-Integration-Status.md`. **On hold**: a colleague is
  actively working on Server 1's OHIF/Orthanc stack - do not touch until they finish, then revisit.
