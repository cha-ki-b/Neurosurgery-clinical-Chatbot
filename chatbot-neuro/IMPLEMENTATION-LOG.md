# Implementation log — bringing the clinical assistant into service

A running, factual record of what was inspected, what was changed and why, with the evidence for
each finding. Newest phase last. Written so that someone who was not present can tell what state
the system is in and why each decision was made.

Companion documents: [`chatbot_archi.md`](chatbot_archi.md) is the design; [`README.md`](README.md)
describes the two components; [`DEPLOYMENT-GUIDE.md`](DEPLOYMENT-GUIDE.md) is the step-by-step
install. This file is the history — it does not replace any of them.

| | |
|---|---|
| Started | 2026-08-18 |
| Server 1 | `10.0.211.249` — OpenMRS, Orthanc, OHIF, MySQL, PostgreSQL, Nginx Proxy Manager, hospitalCA |
| Server 2 | `10.0.211.250` — Nginx + Clinical Agent Service, RTX 5070 Ti 16 GB |

---

## Phase 0 — Survey: what was actually deployed

The starting belief was "everything is built, something in the connection is wrong". The survey
found that the two servers do talk, and that the chat had **never once completed a turn** — for a
reason on Server 1 that nobody had looked for, plus a second, independent fault on Server 2 that
had not yet been reached.

### Confirmed working

| Check | Evidence |
|---|---|
| Nginx up on Server 2 | `:443` and `:80` listening; `server2-proxy` workers running since Aug 17 |
| Agent service up | `uvicorn app.main:app --host 0.0.0.0 --port 8000` running in-container, **no published port** |
| TLS certificate correct | clean handshake to `https://agent.hospital.lan/health` — no certificate error |
| Server-1-only allowlist works | that request returns **403** from Server 2's own address, as designed |
| Bare IP refuses | `https://10.0.211.250/health` → connection closed (the `return 444` default server) |
| OpenMRS reachable from Server 2 | `/openmrs/` → 200, `/ws/fhir2/R4/metadata` → 200, over hospitalCA |
| `agentgateway` module loaded | `POST`-only `chat.form` answers `HttpRequestMethodNotSupportedException` for `GET` |
| hospitalCA in the JVM truststore | `hospital-ca, trustedCertEntry` present in `/etc/ssl/certs/java/cacerts` |
| All ten global properties correct | `agentServiceUrl=https://agent.hospital.lan`, `agentTimeoutMillis=30000`, `tokenTtlSeconds=300`, `selfBaseUrl` on loopback, `captureBeforeState=true`, prefixes as shipped |
| Channel secret matches | Server 1's `agentgateway.channelSecret` equals Server 2's `AGENT_CHANNEL_SECRET` |
| `App: agentgateway.chat.use` assigned | the failing code path had already passed `requireChatUse()` |

So the README's caveat that "the Nginx configuration has never been started" was out of date, and
three of the planned Server 1 configuration steps turned out to be already done.

### Finding 1 — the chat fails on Server 1, before the request leaves (BLOCKER)

```
ERROR - ChatRelayController.chat(88) agentgateway: could not mint a delegated token
org.openmrs.module.agentgateway.security.TokenException:
        Cannot mint a delegated token without a username
```

Five attempts in the Tomcat log, all identical — 2026-08-17T14:55:58, 15:02:57, and
2026-08-18T10:29:41, 10:29:52, 10:29:54. Nothing else. **The relay has never succeeded.**

Cause: the token subject was minted from `user.getUsername()` alone, and resolved back with
`ContextDAO.getUserByUsername()`. OpenMRS does not require a username — an account can
authenticate by `system_id` alone, which is what the Reference Application's own user-creation
flow produces. So the module worked only for accounts that happen to have a username (like
`admin`) and failed for the accounts real clinicians have.

Fixed in module 1.1.1 — see Phase 1.

### Finding 2 — Server 2 holds the signing PRIVATE key, not the public one (BLOCKER)

`OPENMRS_JWT_PUBLIC_KEY` in `server2-stack/.env` was 1624 characters beginning `MIIEvAIBAD`, which
parses as a 2048-bit RSA **private** key. The correct public key is 392 characters beginning
`MIIBIjANBg`. Someone copied the "Signing Private Key" field instead of "Signing Public Key" —
the exact mistake `DEPLOYMENT-GUIDE.md` §A4 warns about.

Two consequences:

1. **Functional.** `_public_key()` in `app/security.py` calls `load_der_public_key`, which raises
   `ValueError` on private-key DER. `/chat` catches only `TokenError`, so this would surface as a
   **500 on every turn** — not the 401 it should be. Never observed in practice only because
   Finding 1 stopped every request before it left Server 1.
2. **Security.** The key that signs tokens for *any* user in the hospital was readable on
   Server 2, in `.env`, in the container's environment, and in `Secrets.txt`.

### Finding 3 — the signing private key is also in the Tomcat log

OpenMRS's `LoggingAdvice` logs every global-property save with its value, so
`docker logs openmrs-app` contains `agentgateway.signingPrivateKey` in full cleartext. A third
exposure route, independent of the other two, and readable by anyone in Server 1's `docker` group.

This is OpenMRS platform behaviour, not something the module chose, and it will happen again when
the replacement key is saved. Recorded as a known limitation; it does not block anything.

### Finding 4 — `OPENMRS_PATIENT_IDENTIFIER_SYSTEM` is empty

`app/tools/catalog.py` omits `identifier.system` when this is unset, so a created FHIR Patient
would carry an identifier with no type. Needs the identifier type this hospital actually issues.

### Finding 5 — the OpenMRS data directory is not on a volume

`docker inspect openmrs-app` reports **no mounts**. The module directory
(`/usr/local/tomcat/.OpenMRS/modules/`) and the JVM truststore both live in the container's
writable layer. Recreating that container — `docker compose down && up`, an image bump — silently
discards the installed module *and* the hospitalCA import. `docker restart` is safe; recreation is
not. This explains the truststore warning in `server2-stack/README.md` and deserves to be fixed
properly by mounting a volume, which is out of scope here but is filed under "known risks".

### A note on method

Server 1's global properties were read from the **application log**, not by querying MySQL
directly, even though SSH access made that possible. §1.2 of the architecture rules out direct
cross-server database access in either direction; a diagnostic session is not a reason to be the
first thing to break it.

---

## Phase 1 — Module fix: token subject and user resolution

**Module version 1.1.0 → 1.1.1.** Bugfix only; no schema change, no new privilege, no new global
property, no API change.

### What changed

| File | Change |
|---|---|
| `security/DelegatedTokenService.java` | new `subjectFor(User)`: username, else `system_id`, else a `TokenException` naming the account |
| `security/DelegatedCredentials.java` | now carries the user uuid alongside the subject label |
| `security/DelegatedAuthenticationScheme.java` | resolves by **uuid** first (`ContextDAO.getUserByUuid`), falling back to the subject via `getUserByUsername` |
| `api/impl/AgentGatewayServiceImpl.java` | all three mint sites use `subjectFor(...)` — chat, rollback, and the read-only before-state caller |
| `api/src/test/.../DelegatedSubjectTest.java` | **new**, 6 tests |

### Why resolve by uuid rather than teach the resolver about system ids

The token already carried a `user_uuid` claim that nothing was using. Resolving against it means
the subject never has to be *interpreted* — it stays a label for the audit trail and the agent's
logs, and there is no code that has to guess whether a given string is a username or a system id.
A resolver that tried both would also have to decide what to do when a username and a system id
collide across two different accounts; anchoring on the uuid makes that question disappear.

Both values arrive inside the same signed token, so this is not a change in what is trusted — only
in which field identifies a row.

`getContextDAO().getUserByUuid` was confirmed to exist on this platform by inspecting
`openmrs-api` rather than assumed from documentation.

### Backward compatibility

A token minted by 1.1.0 carries no `user_uuid` for accounts that reached the mint at all; the
resolver falls back to the subject lookup for those. Covered by
`credentialsTolerateATokenWithNoUuidClaim`.

### Build

Server 1 has Maven 3.8.7 but **no `javac`** (JRE only), so the build ran in a
`maven:3.9-eclipse-temurin-8` container with the host's `~/.m2` mounted. This also builds against
a real JDK 8 rather than asking JDK 21 to target a deprecated release.

```
Tests run: 9   OperationTargetTest
Tests run: 21  RollbackEngineTest
Tests run: 6   DelegatedSubjectTest      <- new
Tests run: 12  DelegatedTokenTest
Tests run: 16  ModuleWiringTest
BUILD SUCCESS   ->  omod/target/agentgateway-1.1.1.omod
```

64 tests, 0 failures, 0 errors.

### Deployment status

Artifact staged on Server 1 at
`~/agentgateway-build/openmrs-module-agentgateway/omod/target/agentgateway-1.1.1.omod`.

**Not yet installed** — installing means writing into the live OpenMRS container and restarting it,
which is a short outage of the hospital's EMR. Handed to the operator as a manual step rather than
done unattended. See "Pending manual steps" below.

### Deployed — 2026-08-18 ~15:34

Module 1.1.1 installed by the operator: `docker cp` into
`/usr/local/tomcat/.OpenMRS/modules/`, old 1.1.0 removed (backed up to
`~/agentgateway-backup/` on Server 1), `docker restart openmrs-app`.

Verified afterwards: `agentgateway-1.1.1.omod` is the only copy present, OpenMRS returns 200, and
no `ERROR.*agentgateway` lines appear since the restart.

---

## Phase 2 — Key rotation and the guard that makes Finding 2 impossible again

### Rotation

Performed by the operator in Administration → Settings → Agentgateway: both signing key properties
cleared, and a fresh `agentgateway.channelSecret` generated with `openssl rand -base64 48`.

Clearing rather than replacing by hand is deliberate — `DelegatedTokenService.ensureKeyPair()`
regenerates whenever either property is blank, so OpenMRS produces the new pair itself and the
private half never has to be handled by a person. It runs lazily on first use, not only at module
start, so no second restart was needed.

Confirmed from Server 2:

| Check | Result |
|---|---|
| Old channel secret | **rejected** (`403 Not authorised`) — the secret really did change |
| New public key | 392 chars, begins `MIIBIjANBg`, parses as a 2048-bit RSA **public** key |
| Different from the old key | yes |

The public key was fetched over `module/agentgateway/publicKey.form`, which is gated on the channel
secret rather than a privilege — so the correct value reached `.env` without a person copying key
material between two windows, which is how the original mistake was made.

`server2-stack/.env` updated (`AGENT_CHANNEL_SECRET`, `OPENMRS_JWT_PUBLIC_KEY`); previous file kept
as `.env.bak.20260818`.

### The guard

Rotating fixes today's deployment. These two changes stop the same mistake from costing a week
again:

| File | Change |
|---|---|
| `app/config.py` | `Settings.validate()` now parses the key with `load_der_public_key` at startup and refuses to start otherwise. The message names the actual mistake — copy "Signing Public Key", ~390 chars, begins `MIIBIjANBg`; a value beginning `MIIEvA` is the private key — rather than reporting a DER decoding failure. |
| `app/security.py` | `_public_key()` raises `TokenError` instead of letting `ValueError` escape, so a process whose key goes bad refuses turns (401, with the cause logged) rather than answering every one with a 500. |

Why both: the startup check is what an operator sees, and it turns a week of confusion into one
line at boot. The `TokenError` conversion covers the case where configuration changes under a
running process, and removes the misleading 500 that hid the cause in the first place.

### Tests

Five added to `tests/test_security.py`:

- startup refuses a private key pasted into the public-key field, a non-base64 value, and base64
  that is not a key at all;
- the private-key case specifically names the right field and the `MIIBIjANBg` prefix, because a
  message that only says "could not deserialize" is what left this undiagnosed;
- an unusable key at request time raises `TokenError`, not `ValueError`.

Run in a `python:3.11-slim` container on Server 1 — Server 2's host Python is 3.14 with no
`ensurepip` and no `python3-venv`, and installing either needs root:

```
86 passed in 1.00s
```

### Pending

The agent container has **not** yet been rebuilt, so it is still running the old image and the old
`.env`. Both the code change and the new `.env` need a rebuild-and-recreate, which requires Docker
on Server 2 — the `cerist` account is not in the `docker` group and `sudo` needs interactive
authentication, so this is an operator step.

### Rebuilt and verified — 2026-08-18

`docker compose up -d --build` on Server 2 (via `sudo` — the `cerist` account has no Docker access
and the host has no `docker` group at all, the socket being `root:root`). Container reports
healthy.

```
GET /health   ->  {"status":"ok",
                   "openmrs_base_url":"https://openmrs.hospital.lan/openmrs",
                   "fhir_capabilities_known":true,
                   "fhir_capabilities_error":null}
```

Two things this proves beyond reachability: the service **started**, which means the new startup
guard parsed `OPENMRS_JWT_PUBLIC_KEY` successfully, and `fhir_capabilities_known: true` means it
authenticated to `/ws/fhir2/R4/metadata` and read 17 resource types back.

`GET /capabilities`, presented with the new channel secret:

| Tool | State |
|---|---|
| `search_patient` | available |
| `get_patient_summary` | available |
| `create_patient` | available |
| `update_patient_demographics` | available |
| `book_appointment` | **unavailable** — `fhir2` on this installation does not expose `Appointment` (create) |
| `record_neuro_assessment` | unavailable — neurosurgery fields not yet exposed over REST (architecture §4.3) |

### Finding 6 — `book_appointment` cannot work over FHIR on this installation

The architecture lists "book an appointment" as one of the four task families (CA1), and ADR-10
assumed `fhir2` would cover it. Read live from this deployment's own capability statement, it does
not: `Appointment` is absent from the 17 resources `fhir2` exposes here.

Appointments *are* available — the `appointmentscheduling` and `appointmentschedulingui` modules
are installed and configured (`defaultTimeSlotDuration`, `defaultVisitType` and others are set) —
but they are exposed under `/ws/rest/v1/appointmentscheduling/...`, not as a FHIR resource. So the
capability exists; the tool is pointed at the wrong surface.

This is exactly the failure mode ADR-10's "no hardcoded `fhir2` coverage" rule was written to
catch, and it caught it: the tool reports itself unavailable with a specific reason instead of
failing obscurely at the moment a clinician tries to book something. Retargeting the tool at the
`appointmentscheduling` REST resources is a change to `app/tools/catalog.py` plus adding that
prefix to `agentgateway.auditedPathPrefixes`. Not yet done, not a blocker for the other three
families.

---

## Phase 3 — Checkpoint 0 reached, and Finding 7

### The relay works

First real chat turn, 2026-08-18 15:52, logged in as `admin`:

```
[1db54b87…] turn from admin (31 chars)
[1db54b87…] -> awaiting_clarification (search_patient)
[1db54b87…] turn from admin (12 chars)
GET .../ws/fhir2/R4/Patient?name=walter%20white&_count=10  "HTTP/1.1 401 Unauthorized"
[1db54b87…] -> failed (search_patient)
```

Everything up to the final hop now works: the browser reached `agentgateway`, a token was minted
(no `could not mint a delegated token` since 1.1.1 — the last occurrence is from before the fix),
the channel and token checks passed on Server 2, the interpreter classified the task, and the agent
issued a real FHIR call. The Server 1 blocker from Finding 1 is closed.

The clinician saw *"Vous n'avez pas les droits necessaires…"*, which is `explain_failure`'s wording
for both 401 and 403 — so the chat text alone could not distinguish a permission problem from an
authentication one. The agent log gave the answer: **401**.

### Finding 7 — `fhir2` rejects agent calls before the audit filter can authenticate them (BLOCKER)

`fhir2` declares its own `AuthenticationFilter` on `/ws/fhir2/*`, `/ws/fhir2`, `/ms/fhir2Servlet/*`.
Its logic, read from the 1.2.2 source rather than inferred:

- exempt: `/.well-known` and **`/metadata`**;
- otherwise pass only if `Context.isAuthenticated()` is already true, or HTTP Basic credentials are
  present and valid;
- **a valid session alone does not pass** — 401.

Both `fhir2AuthenticationFilter` and `agentgatewayAuditFilter` are module filters, run by web.xml's
single `ModuleFilter` in **module start order**. `fhir2` is a *bundled* module
(`WEB-INF/bundledModules/fhir2-1.2.2.omod`); `agentgateway` lives in `.OpenMRS/modules`. Bundled
modules start first, so fhir2's filter is registered first and runs first — it returns 401 before
`agentgateway` has had the chance to authenticate the delegated user.

Two observations confirm this rather than leaving it as a theory:

1. **The audit filter logged nothing.** Every rejection path it has for a GET writes a warning; the
   log has none. It never ran.
2. **`/metadata` worked and nothing else did.** Capability discovery succeeded — 17 resources read —
   because `/metadata` is on fhir2's exemption list. The one call that bypasses that filter is the
   one call that worked.

This invalidates an assumption in the architecture, not just a setting. §3's note that
"`agentgateway`'s job on that leg is to register an in-process filter that verifies the delegated
JWT and logs the call, not to proxy the traffic" only holds if that filter runs before the API's own
authentication. On this deployment it cannot.

**It also breaks the module's own loopback calls** for the same reason: `DelegatedApiCaller` reads a
record's before-state and issues reversing calls over HTTP to `/ws/fhir2/R4/...`, so before-state
capture (CA9) and rollback (CA10) would fail identically once a write path is reachable.

Options are recorded in the message accompanying this entry; the decision is the operator's, since
it changes a documented design position. `webservices.rest`'s `AuthorizationFilter` on `/ws/rest/*`
poses the same question, though start order there may happen to favour `agentgateway` — untested,
because every patient tool currently targets `fhir2`.

---

## Phase 4 — The relay: fixing Finding 7

Decision taken: route agent calls through a path `fhir2` does not guard, authenticate there, and
**forward** to the real servlet. The two alternatives were rejected on the evidence:

- **Reordering the filters.** `fhir2` has no module dependencies while `agentgateway` requires
  uiframework/appui/coreapps, so the dependency sort will keep starting fhir2 first. Stopping and
  restarting fhir2 at runtime does re-register its filter behind ours, but the original order
  returns on the next OpenMRS restart. A fix that silently reverts on reboot is worse than none.
- **HTTP Basic from the agent.** It means one shared service account, which is exactly what CA7 and
  ADR-9 exist to prevent, and every audit row would name the same user.

### Mechanics, each verified against this deployment rather than assumed

| Question | Answer | How it was established |
|---|---|---|
| Do module filters run on a FORWARD? | **No** — `ModuleFilter` declares no `<dispatcher>`, so it defaults to REQUEST | read from the deployed `web.xml` |
| Does `OpenmrsFilter` run on a FORWARD? | **Yes** — it declares ERROR, FORWARD, REQUEST, INCLUDE | same |
| Where does `fhir2` really serve R4? | `/ms/fhir2Servlet` | `FhirConstants.SERVLET_PATH_R4` read out of the deployed `fhir2-api-1.2.2.jar` |
| What does fhir2's `ForwardingFilter` do? | rewrites `<ctx>/ws/fhir2/R4` → `/ms/fhir2Servlet`, dropping the version segment | 1.2.2 source |
| What does fhir2's `AuthenticationFilter` accept? | an already-authenticated context, or HTTP Basic. A valid session alone does **not** pass. `/metadata` and `/.well-known` are exempt | 1.2.2 source |
| What serves `/ws/*` and `/ms/*`? | OpenMRS's `DispatcherServlet` and `module_servlet` respectively | deployed `web.xml` |

The second row is the one that would have quietly broken this: `OpenmrsFilter` re-reads the user
context from the HTTP session on the forward, so it would have replaced the delegated context with a
fresh unauthenticated one and the call would have failed exactly as before. Hence seeding the
session with the delegated context before forwarding, and invalidating it afterwards — one session
per chat turn, never invalidated, is a leak that grows with use.

### Changes

| File | Change |
|---|---|
| `AgentGatewayConstants` | `RELAY_PATH_PREFIX`, `FHIR2_R4_SERVLET_PATH`, `FHIR2_R3_SERVLET_PATH` |
| `AgentAuditFilter` | strips the relay prefix to get the real target; seeds the session; forwards instead of continuing the chain; `dispatchTarget()` does the R4/R3 rewrite |
| `DelegatedApiCaller` | loopback calls (before-state, rollback) go through the relay too |
| `app/openmrs_client.py` | delegated calls prefixed with the relay path; `/metadata` deliberately left direct, since it is exempt and startup has no token yet |
| `tests/mock_openmrs.py` | strips the relay prefix like the real filter, so tests exercise the paths the agent really sends |
| `tests/test_security.py` | pins the prefix — dropping it would silently lose every FHIR call again |

### Build

- module **1.1.2** — 64 tests, 0 failures
- agent service — **87 tests**, 0 failures

Both must be deployed together: the agent addresses the relay prefix, and only 1.1.2 understands it.

### Pending deployment

Neither side is live yet. Server 1 needs the new `.omod` and a restart; Server 2 needs the agent
container rebuilt.

### Deployed and working — 2026-08-18 ~17:30

Module 1.1.2 on Server 1, agent container rebuilt on Server 2. First successful end-to-end turn:

```
cherche le patient walter white
  -> Quel est le nom du patient a rechercher ?
walter white
  -> 1 patient trouve :
     - white walter - 10002T - 1960-01-02
```

**Checkpoint 0 is met.** Every link now works: browser → OpenMRS session → `agentgateway` →
delegated token → TLS → Server 2 → channel and token verification → interpretation → relay →
forward → `fhir2` → a real patient, reported in French. Finding 7 is closed.

Note for anyone reading the health endpoint straight after a deployment: the agent restarted while
OpenMRS was still booting, so `fhir_capabilities_known` was `false` for a few minutes. That is
self-healing — `fetched_at is None` counts as stale and `/chat` refreshes before each turn — and the
first real turn re-read the statement before searching.

### Still open, in priority order

1. `OPENMRS_PATIENT_IDENTIFIER_SYSTEM` for `create_patient` — see Finding 8 below.
2. `book_appointment` retarget onto `/ws/rest/v1/appointmentscheduling/` (Finding 6).
3. Checkpoint 1: create a patient, dry-run a rollback, confirm a read-only user is refused.
4. Phase 2: MedGemma on vLLM.
5. Cosmetic: the `null null` panel header; the name-extraction gap that made the search take two
   turns instead of one.

---

## Phase 5 — Finding 8, and two fixes from the first live turn

### Finding 8 — `OPENMRS_PATIENT_IDENTIFIER_SYSTEM` was the wrong setting entirely

Read from the fhir2 1.2.2 source, not inferred:

```java
// FhirPatientServiceImpl.getPatientIdentifierTypeByIdentifier
if (identifier.getType() == null || StringUtils.isBlank(identifier.getType().getText())) {
    return null;
}
return dao.getPatientIdentifierTypeByNameOrUuid(identifier.getType().getText(), null);
```

fhir2 resolves the patient identifier type from **`identifier.type.text` and nothing else** — it
never looks at `identifier.system` — and a null type fails the whole conversion. The agent wrote the
configured value into `identifier.system`, so **creating a patient could not have worked whatever
was configured there**. Setting that variable, as every earlier note in this project said to do,
would have produced the same failure and sent us looking in the wrong place.

Two settings now exist. `OPENMRS_PATIENT_IDENTIFIER_TYPE` is the one that matters and goes into
`identifier.type.text`; `OPENMRS_PATIENT_IDENTIFIER_SYSTEM` is still sent when set, because it is
correct FHIR and later fhir2 versions consult a system-to-type mapping.

### The identifier value cannot be invented

`10002T`, the identifier of the patient the first successful search returned, is a check-digit
format. OpenMRS's usual identifier types validate that digit, so the assistant cannot make a value
up — and it has no business doing so anyway. Creating a patient is therefore two calls:

1. `POST /ws/rest/v1/idgen/identifiersource/{uuid}/identifier` — reserve the next identifier
   (idgen 4.7.0 is installed and exposes REST resources);
2. `POST /ws/fhir2/R4/Patient` — create, with that identifier and its type text.

`/ws/rest/v1/` is already in `agentgateway.auditedPathPrefixes`, so no configuration change is
needed for the extra call.

Where no identifier source is configured, `create_patient` **asks** the clinician for an identifier
instead — an extra required slot rather than a silent guess. The confirmation summary now says
`Identifiant : attribue automatiquement par OpenMRS` when one will be reserved, because the
identifier is part of what the clinician is approving.

This needed a small, deliberately narrow addition: `PlannedOperation.body_from_results`, a callable
that builds one call's body from the results of the earlier ones. The plan stays fixed and
inspectable before anything is sent, and the summary the clinician approves is still written by the
tool. It also had to be carried through `PendingOperation` — the first version lost it while the plan
waited for confirmation, so the create fired with an empty body after the clinician said yes. The
test suite caught that, which is exactly where it should be caught.

### The name-extraction gap

`cherche le patient walter white` asked for a name that was already on screen, because the name
pattern required capitalised words. Case cannot be what decides whether a name was given, so a
case-blind fallback was added, guarded by a stopword list so that `le patient avec un GCS bas` does
not yield a patient called "avec un". Four tests cover it, including the trailing-clinical-words case.

### `null null` in the panel header

Not this module's markup — `chat.gsp` already guards against it, and the text appears *above* our
heading. It comes from appui's `standardEmrPage` decorator rendering the logged-in user's person
name, and the `admin` account's person record has no given or family name. It will show on every
page of the application, not only the assistant. The fix is to give that account a name; no code
change here.

### Tests

Agent service: **93 passed**. The mock OpenMRS now rejects a patient created without
`identifier.type.text`, the way fhir2 does, and implements idgen's reserve call — so the two-call
shape is exercised rather than assumed. A mock that accepted a patient with no identifier type is
what let Finding 8 survive this long.

### Pending

`OPENMRS_PATIENT_IDENTIFIER_TYPE` and `OPENMRS_IDGEN_SOURCE_UUID` are declared in `.env` but empty —
both are per-deployment values that have to be read out of the OpenMRS administration screens.
Until the type is set, `create_patient` will be refused by fhir2. The agent container also needs
rebuilding to pick up this phase's code.

### Configured — 2026-08-18

```
OPENMRS_PATIENT_IDENTIFIER_TYPE=OpenMRS ID
OPENMRS_IDGEN_SOURCE_UUID=691eed12-c0f1-11e2-94be-8c13b969e334
```

The type name comes from Administration → Manage Identifier Types, where "OpenMRS ID" is described
as *"OpenMRS patient identifier, with check-digit"* — confirming directly why the assistant reserves
an identifier instead of composing one. The name is used rather than the type's uuid because fhir2
passes the text as the **name** argument (`getPatientIdentifierTypeByNameOrUuid(text, null)`), so a
uuid in that field would not match.

The source uuid came out of an error page rather than a working response — see below.

### Two incidental observations

**This deployment's XML marshaller is broken, independently of anything here.** Opening
`/ws/rest/v1/idgen/identifiersource` in a browser returns HTTP 500:

```
HttpMessageNotWritableException: Could not marshal ...
  InitializationException: Could not instantiate mapper : com.thoughtworks.xstream.mapper.EnumMapper
  NoSuchMethodException: EnumMapper.<init>(com.thoughtworks.xstream.mapper.Mapper)
```

A browser's `Accept` header makes Spring choose the XML converter, and XStream cannot initialise on
this classpath — a version mismatch in the deployed webapp. Any REST client asking for XML or HTML
gets a 500 from every endpoint. It does not affect the assistant, which sends
`Accept: application/json` explicitly, and it is not caused by this project. Worth knowing before
someone spends an afternoon on it: the data was in the exception message all along, which is how the
source uuid was obtained.

**The real filter order, confirmed from that stack trace.** Outermost first:

```
CharacterEncodingFilter -> StartupFilter -> OpenSessionInViewFilter -> OpenmrsFilter
  -> ModuleFilter -> SpaFilter -> OwaFilter -> AgentAuditFilter
  -> ShallowEtagHeaderFilter -> ContentTypeFilter -> AuthorizationFilter (webservices.rest)
  -> GZIPFilter -> ForcePasswordChangeFilter -> servlet
```

Two things follow. `AgentAuditFilter` runs **before** `webservices.rest`'s `AuthorizationFilter`, so
`/ws/rest/v1/*` would in fact work without the relay — the start-order problem is specific to
`fhir2`, which is a bundled module and starts earlier than either. And `OpenmrsFilter` sits outside
`ModuleFilter`, which is what makes seeding the session the correct way to carry the delegated
context across the forward. Both were reasoned about earlier from `web.xml`; this is the running
system agreeing.

---

## Phase 6 — Finding 9: the identifier needs a location

The create flow now runs end to end and fails at the last validation:

```
Echec : Les informations fournies ont ete refusees par OpenMRS :
  'Patient#null' failed to validate with reason: Identifier Location cannot be null for 10005K.
```

Everything before this worked, and the message says so: `10005K` is a real identifier, which means
idgen reserved one (Finding 8's two-call flow works) and fhir2 resolved the identifier type from
`type.text` (Finding 8's fix works). The duplicate-name warning fired correctly too, surfacing an
existing "TEST test - 10001V" before asking for confirmation.

**Cause.** A stock "OpenMRS ID" has location behaviour REQUIRED, so OpenMRS refuses a patient whose
identifier has no location. **FHIR has no field for this.** fhir2 reads it from its own extension:

```java
// PatientIdentifierTranslatorImpl.toOpenmrsType
if (identifier.hasExtension(FhirConstants.OPENMRS_FHIR_EXT_PATIENT_IDENTIFIER_LOCATION)) {
    ... getReferenceId((Reference) value).map(uuid -> locationDao.get(uuid))
            .ifPresent(patientIdentifier::setLocation);
}
```

The extension URL, read out of the deployed `fhir2-api-1.2.2.jar`, is
`http://fhir.openmrs.org/ext/patient/identifier#location`, and the value is a `Reference` of the form
`Location/<uuid>`.

So a resource can be entirely valid FHIR and still be refused. Worth noting how the error reads: it
names the *identifier* (`...for 10005K`), which points a reader at idgen rather than at a missing
extension. That is the third time in this deployment that the message named the wrong layer.

**Change.** `OPENMRS_IDENTIFIER_LOCATION_UUID` is added; when set, `_identifier_block` attaches the
extension. The mock now refuses an identifier without it, with OpenMRS's own wording, so it cannot be
dropped unnoticed. **94 tests pass.**

Also fixed while here: the `mock_state` fixture did not reset `generated_identifiers` between tests,
so a test asserting that an identifier had been reserved could pass on one left behind by an earlier
test.

### Pending

`OPENMRS_IDENTIFIER_LOCATION_UUID` is empty — it needs a Location uuid from the deployment, then a
rebuild of the agent container.

---

## Phase 7 — Finding 10: fhir2 1.2.2 cannot create a patient at all

With the location extension in place the create got past validation and died in the database:

```
ERROR SqlExceptionHelper - Column 'uuid' cannot be null
ca.uhn.fhir.rest.server.exceptions.InternalErrorException: Failed to call access method:
  org.hibernate.exception.ConstraintViolationException: could not execute statement
```

**Cause**, read from the 1.2.2 source. Three translators each do this, none of them guarded:

```java
currentPatient.setUuid(patient.getId());        // PatientTranslatorImpl
personName.setUuid(name.getId());               // PersonNameTranslatorImpl
patientIdentifier.setUuid(identifier.getId());  // PatientIdentifierTranslatorImpl
```

`BaseOpenmrsObject` generates a uuid in its constructor; these calls overwrite it with the FHIR
element's id. **A FHIR create carries no ids** — that is what create *means* — so all three become
null and MySQL refuses the insert.

This is not configurable around. The client would have to invent ids for the patient, the name and
the identifier, and FHIR forbids sending an `id` on create; a server that honoured one would be
letting clients choose primary keys.

It also explains the shape of everything before it: **searching worked from the first day and only
creating ever failed**, because a FHIR *update* sends back a resource that was fetched, so its ids
are already present. The defect is specific to create.

### Decision — create through `webservices.rest`, keep everything else on FHIR

`create_patient` now posts to `/ws/rest/v1/patient` with `person` and `identifiers` as ordinary named
fields. That takes all three contested values — identifier type (by uuid), assignment location, and
the identifier itself — without extensions, without type-text lookup, and without any translator
touching a uuid.

This is the second documented departure from ADR-10's "FHIR where OpenMRS exposes it". The
justification is narrow and specific: on *this* fhir2 version, FHIR create is not merely awkward, it
cannot succeed. Reads, searches and updates are unchanged and still FHIR. If fhir2 is upgraded, the
`CREATE_VIA_REST` note in `app/tools/catalog.py` records exactly what to re-test before reverting.

`/ws/rest/v1/` is already in `agentgateway.auditedPathPrefixes`, so the audit trail and the
privilege gate cover the new path with no configuration change — and the stack trace in Phase 6
confirmed `AgentAuditFilter` runs ahead of `webservices.rest`'s `AuthorizationFilter`.

The create tool is also no longer gated on the FHIR capability statement, since it no longer depends
on it.

### Configuration added

```
OPENMRS_PATIENT_IDENTIFIER_TYPE_UUID=05a29f94-c0ed-11e2-94be-8c13b969e334   # "OpenMRS ID"
```

Taken from the idgen source listing, which reported
`identifierType={uuid=05a29f94-c0ed-11e2-94be-8c13b969e334, display=OpenMRS ID}`.

### Tests

The mock now implements `POST /ws/rest/v1/patient` and enforces what OpenMRS enforces: a family
name, a birthdate, a gender of M/F/U, an identifier, an identifier type, and a location — the last
with OpenMRS's own wording. **94 passed.**

### A pattern worth recording

Four findings in a row were reported by the wrong layer: a private key pasted into a public-key field
surfaced as a generic 500 on every turn; fhir2's filter ordering surfaced as "you do not have
permission"; a missing proprietary extension surfaced as a complaint about an identifier; and an
unguarded `setUuid` surfaced as a database constraint. In each case the message named the symptom's
location, not the cause's. Reading the deployed source and jars was faster than trusting any of them.

---

## Phase 8 — It works

2026-08-18. From a French sentence in the OpenMRS interface:

```
cree un patient nomme "Test Neurochir", sexe masculin, ne le 15/03/1980
  -> ATTENTION : des dossiers portant un nom proche existent deja :
     - TEST test - 10001V - 2003-02-05
     Verifiez qu'il ne s'agit pas d'un doublon avant de confirmer.
     Je vais CREER un nouveau dossier patient ...
     - Identifiant : attribue automatiquement par OpenMRS
     Confirmez-vous la creation ?
oui
  -> C'est enregistre. Le dossier a ete cree.
```

Confirmed in OpenMRS's own patient search, independently of the assistant:

```
10007F | Test Neurochir | M | 46 | 15 mars 1980
```

**The original goal is met**: a clinician types a request in natural language in the OpenMRS
interface, and a patient is created — under their own privileges, after an explicit confirmation,
with a duplicate warning first, and with an identifier OpenMRS itself issued.

### One fix from that run

The reply read `Le dossier a ete cree : (sans nom).` The record was correct; the *message* was wrong.
`_render_results` read the response as a FHIR Patient, but `webservices.rest` answers a create with
`{"uuid": ..., "display": "<identifier> - <name>"}`. Reading FHIR fields off that finds no name.

Worth fixing properly rather than cosmetically: for an operation that cannot be undone by retrying,
"(sans nom)" invites the clinician to wonder whether the record is damaged, and it withheld the
identifier they need to find the patient again. The renderer now prefers `display`, falls back to the
FHIR label, and if neither is usable says plainly that the record was created without inventing a
name. **95 tests pass.**

### State of the four task families

| Family | State |
|---|---|
| Search a patient | working, live |
| Show a record | working (same FHIR read path) |
| Create a patient | **working, live** |
| Update a patient | implemented, FHIR PUT, not yet exercised live |
| Book an appointment | blocked — `fhir2` here has no `Appointment` resource (Finding 6); needs retargeting onto `/ws/rest/v1/appointmentscheduling/` |
| Record a neuro score | blocked — needs `patientview` REST resources (architecture §4.3) |

### Still to do

1. Finish Checkpoint 1: exercise an update, dry-run a rollback on the throwaway patient
   (`Test Neurochir`, 10007F), and confirm a `chat.use`-only account is refused a write.
2. Check the audit trail: `agentgateway_operation_log` should hold both calls of the create, with
   `using_agent = true` and the acting user.
3. Retarget `book_appointment` (Finding 6).
4. Phase 2: MedGemma on vLLM — needs `nvidia-container-toolkit` (root) and the model licence
   accepted.
5. Cosmetic: `null null` in the page header, which is appui rendering the `admin` account's empty
   person name, not this module.

---

## Phase 9 — Finding 11: booking is not a code problem

Retargeting `book_appointment` off FHIR (Finding 6) turns out not to be a matter of changing a URL.

`appointmentscheduling-1.16.0` is installed and does expose REST resources, including
`AppointmentResource1_9`. Its writable properties, read from the deployed omod:

```
patient, timeSlot, appointmentType, status, reason, cancelReason, visit
```

**An appointment belongs to a `timeSlot`**, and a TimeSlot belongs to an AppointmentBlock — a
provider's session at a location, created in advance by an administrator. There is no "book at this
date and time" operation: a clinician can only be given a slot that already exists.

So CA1's "book an appointment" assumes a scheduling model this module does not have. Booking through
the assistant would mean: read the appointment types, find slots available on the requested day,
choose one, and create the appointment in it — three reads before the write, and it fails outright if
no blocks have been configured for that day.

**Before writing any of that, two things need answering, and neither is a coding question:**

1. Does this deployment have appointment types and appointment blocks configured at all? If provider
   schedules were never set up, booking cannot work regardless of what the assistant sends.
2. Does the department actually want a clinician to book into a pre-existing slot from the chat, or
   is "book an appointment" in the brief really a different operation — a request or a referral —
   than what `appointmentscheduling` models?

Recorded rather than guessed. Building a three-read booking flow against a module the hospital may
not use would be speculative work, and inventing time slots on a clinician's behalf would be worse:
it would create schedule entries nobody staffed.

The tool continues to report itself unavailable with a specific reason, which is the correct
behaviour until this is settled.

---

## Phase 10 — The audit trail, and two bugs it exposed

### The audit trail works

The operation log page shows 13 rows for the work so far, every call the assistant made, successful
or not:

| # | Task | Call | Status | Reversible |
|---|---|---|---|---|
| 13 | create_patient | `POST /ws/rest/v1/patient` | 201 | **oui** |
| 12 | create_patient | `POST /ws/rest/v1/idgen/.../identifier` | 201 | non |
| 11 | create_patient | `GET /ws/fhir2/R4/Patient?name=Test%20Neurochir` | 200 | non |
| 10 | create_patient | `POST /ws/fhir2/R4/Patient` | **500** | non |
| 9–1 | … | … | 201/200/422 | non |

Everything CA9 asks for is there: the successful create is recorded and marked reversible, the
duplicate-check read that preceded it is recorded, and **the failed attempts are recorded too** —
including the 500 from Finding 10 and the two 422s from Finding 9. An administrator can reconstruct
exactly what was tried.

### Finding 12 — the "Utilisateur" column was blank on every row

`AgentLogController` read `User.getUsername()`. That is null for an account that authenticates by
system id, and **this installation's `admin` account is one**. The log had recorded who acted; it
could not display it.

This is Finding 1 again, surfacing somewhere else — and it retrospectively explains that finding
completely. The original `Cannot mint a delegated token without a username` failures happened while
logged in as `admin`, which had looked odd: admin is the one account you would expect to have a
username. It does not. Fixed the same way, and the fallback order now matches
`subjectFor` (username → system id → uuid).

Dates were also rendered as raw epoch milliseconds; they are now formatted server-side. On a page
whose purpose is to help an administrator decide whether to reverse something, `1787072923000` is
not a date.

Module **1.1.3**, 64 tests, 0 failures.

### Finding 13 — the clarification loop could not be escaped (BLOCKER for updates)

The live update attempt:

```
mets a jour le telephone de Test Neurochir a 0555123456
  -> De quel patient s'agit-il ? Donnez son nom ou son identifiant.
Test Neurochir a 0555123456
  -> Plusieurs patients correspondent : TEST test - 10001V / Neurochir Test - 10007F
     Precisez l'identifiant du patient concerne.
10007F
  -> Plusieurs patients correspondent : ... (identical, forever)
```

Three separate defects, each individually plausible, together making updates impossible:

1. **The answer was discarded.** `_interpret_with_carryover` merged a clarification answer with
   `merged.setdefault("name", prompt)`. `name` was already set, so `10007F` was thrown away and the
   same ambiguous search ran again. A question whose answer *refines* an existing slot could never
   be answered.
2. **A bare identifier was not recognised as one.** `extract_slots` requires a cue word
   ("identifiant 10007F") because inside a sentence a bare token is more likely a word than a value.
   Answering a direct question inverts that: the whole turn *is* the value.
3. **The phone number was never extracted.** `_PHONE_RE` allowed 12 characters between the cue word
   and the digits; "telephone **de Test Neurochir a** 0555123456" has 21. So the assistant asked for
   something the clinician had already given.

Fixes: the conversation now records *which slot* a question was about (`awaiting_slot`), an answer
lands in that slot and is allowed to **replace** what is there, an identifier answer clears the
ambiguous name rather than competing with it, and the "which patient?" question accepts either form —
deciding by shape, since answering it with an identifier is the obvious move once a name has proved
ambiguous. The phone window was widened.

**98 agent tests pass**, including the live sequence turn for turn.

Worth noting what this says about Phase 2: none of these three would have been fixed by a better
model. The model picks the task and fills slots; carrying an answer into the right slot, and letting
it override, is the orchestrator's job either way. A model on top of a loop that cannot be escaped
would still not be able to escape it.

## Phase 11 — MedGemma integration (code complete, awaiting the GPU)

Written while the GPU toolkit install was outstanding, since none of it depends on the GPU being
reachable.

### What was added

| File | What it is |
|---|---|
| `app/nlu/schema.py` | the JSON schema the model is constrained to, **generated from the tool registry** |
| `app/nlu/medgemma.py` | `MedGemmaNlu`, implementing the same `NluEngine` protocol as the rules engine |
| `tests/test_medgemma.py` | 14 tests against a fake vLLM — no GPU needed |
| `server2-stack/docker-compose.vllm.yml` | the inference server, as an overlay |
| `.env` / `.env.example` | `NLU_ENGINE`, `LLM_*`, `LLM_MODEL_DIR`, `LLM_MODEL_PATH` |

Nothing downstream changed. The orchestrator, the confirmation gate, the tool registry and the audit
trail all still work from an `Interpretation`, exactly as `base.py` was written to allow.

### The schema is generated, not written

`build_interpretation_schema` reads the registry: the `task` enum *is* the registry's task names, and
the slot properties are the union of what the tools declare. A tool added tomorrow is offered to the
model automatically; a tool removed stops being representable.

That is the property worth having. With vLLM's structured output the model **cannot emit a task name
outside the enum**, so "the model invented an endpoint" is not a failure mode that exists here — it is
excluded by construction rather than caught downstream. What remains possible is misjudging *which*
real task a sentence means, or which slot a value belongs to. Those are what the confirmation gate and
the post-checks are for.

### Three checks that do not trust the model

1. **Descriptive phrasing is never a write.** `reads_as_description` is re-applied after the model
   answers. "Le GCS s'est aggrave a 6" becomes a clarifying question even when the model returned a
   write task with the slot filled. Tested with the model actively getting it wrong.
2. **Fabricated slots are dropped, on writes only.** A value the model reports is kept if the
   deterministic extractor also found it, or if it appears verbatim in the sentence; otherwise it is
   discarded and the clinician is asked. A hallucinated identifier or date of birth is the most
   damaging output this component could produce. Reads are *not* filtered — understanding phrasings
   the extractor cannot parse is the whole reason for having a model.
3. **A task outside the registry is refused**, not repaired. If structured output ever lets one
   through, the constraint is not doing what this design assumes, and guessing would be the wrong
   response.

### Degradation, not failure

Any model failure — unreachable, timed out, unparseable, self-contradictory — falls back to the rules
engine for that turn, with a log line saying so. A stopped GPU narrows the assistant's understanding
of language; it does not take the chat offline. `NLU_ENGINE` switches engines in one restart, which
also makes "is it the model or the plumbing?" answerable in one step — worth having, given how many
findings in this deployment were reported by the wrong layer.

A plain "oui"/"non" is settled by rule without calling the model: it is unambiguous, it is the most
common turn in any confirmation flow, and a GPU round-trip would only add latency.

### Deliberately not folded into the base stack

`docker-compose.vllm.yml` is an overlay, so `NLU_ENGINE=rules` plus the base stack remains a working
system. The agent does **not** `depend_on` vllm being healthy: a model that fails to load should
leave a working assistant with narrower language understanding, not an assistant that will not start.

`expose: 8000`, never `ports:` — the model endpoint has no authentication at all, so anything that
could reach a published port could drive the hospital's GPU and read clinical prompts.

### Notes for when the GPU is available

- The RTX 5070 Ti is Blackwell, compute capability **12.0**. It needs a vLLM built against CUDA 12.8
  or newer; an older image has no `sm_120` kernel and fails at model load with an error that reads
  like a broken model rather than a wrong image. `vllm/vllm-openai:v0.11.0` is pinned for that reason.
- `--max-model-len 4096`, `--gpu-memory-utilization 0.80`: the weights are ~8.6 GB of 16 GB, and the
  rest is KV cache. Headroom goes to stability rather than throughput nobody is waiting on.
- `--guided-decoding-backend xgrammar` is what enforces the schema. Without it the constraint is
  advisory and the "cannot name a task that does not exist" property is lost.

**110 tests pass** (96 existing + 14 new).

### Still outstanding

The GPU toolkit is not installed (`nvidia-container-toolkit` is not in Ubuntu 26.04's default
repositories; NVIDIA's own repository has to be added first), and MedGemma's licence has not been
accepted, so the weights cannot be fetched. Until then `NLU_ENGINE` stays `rules` and the assistant
behaves exactly as it does today.

---

## Phase 12 — Finding 14: Docker on Server 2 was a snap, and could not pass through the GPU

Installing `nvidia-container-toolkit` succeeded, then the GPU test failed:

```
docker: Error response from daemon: failed to create task for container:
  ... error during container init: failed to fulfil mount request:
  open /usr/bin/nvidia-cuda-mps-control: no such file or directory
```

The file **exists** on the host. Two things were wrong, both explained by one fact: Docker on Server 2
was the Canonical **snap** (`snap.docker.dockerd.service`, docker 29.6.1), not Docker Engine.

- A snap-confined `dockerd` does not see the host's `/usr/bin`, so the toolkit's mount of the NVIDIA
  userspace binaries fails — reported as a missing file, which sends a reader looking for a broken
  driver install that was never broken.
- `nvidia-ctk` wrote `/etc/docker/daemon.json`. The snap reads
  `/var/snap/docker/current/config/daemon.json`, so the runtime registration had no effect either.

The same fact explains two earlier puzzles: `docker.service` did not exist (hence
`Failed to restart docker.service`), and `/var/run/docker.sock` was `root:root` with **no `docker`
group at all** — which is why every container command on Server 2 had to be handed to the operator
with `sudo` while Server 1 needed none.

The snap's only GPU-related interface is `gpu-2404` → `mesa-2404`, which is Mesa graphics, not CUDA
compute. There is no supported path to CUDA passthrough there.

### Resolution — replaced with Docker Engine from Ubuntu's repositories

```
docker.io 29.1.3 + docker-compose-v2 + docker-buildx
```

Safe to do because the whole stack is declarative and every input lives on the host filesystem:
compose file, `.env`, `certs/`, the nginx config and the agent's source. Only built images were lost,
and `up -d --build` restored them in about a minute. No configuration, certificates or data were
involved. Server 1 was untouched.

### Verified after the migration

| Check | Result |
|---|---|
| Docker flavour | Docker Engine 29.1.3, `systemctl is-active docker` → active |
| nvidia runtime registered | `/etc/docker/daemon.json` contains the `nvidia` runtime |
| `docker` group | now exists; `cerist` is a member |
| Stack restored | 443 and 80 listening |
| Assistant healthy from Server 1 | `status: ok`, `fhir_capabilities_known: true` |

Three problems solved by one change: CUDA passthrough is now possible, the daemon reads the config
the toolkit writes, and container commands no longer need `sudo`.

Fourteenth finding, and the fourth in a row whose error message named the wrong layer — a missing host
file that was present all along.

---

## Phase 13 — The evaluation harness, and Finding 15

`tests/eval_nlu.py` runs one corpus of 25 French sentences through both interpreters and scores them
the same way, so "is the model good enough" becomes a table rather than an impression. It is a script,
not a test: it needs a live model and has no place in CI.

The corpus has three parts — plain phrasing both engines should handle, phrasing the rules engine is
*expected* to miss (the delta the model exists to close), and **safety rows** where the only correct
behaviour is to ask.

The column that decides go/no-go is **UNSAFE**: a sentence that describes, hedges or asks, for which
the engine produced a write plan. Wrong-task and spurious-clarification counts are quality — friction,
a wasted turn. UNSAFE is not quality, and any value above zero blocks regardless of the rest.

### Finding 15 — every date became a birth date

Found by writing the harness, before the model was involved at all.

```python
birth = re.search(r"\bnee?\s+le\s+", text)
slots["birthdate"] = dates[0] if birth or len(dates) == 1 else dates[0]
```

Both branches return `dates[0]`. The condition reads as though it tests for a birth cue and tests
nothing, so **any** date in a sentence filled `birthdate`. Confirmed against the extractor:

```
'programme un rendez-vous pour Ahmed Ziani le 12/09/2026 a 10h'
     {'name': 'Ahmed Ziani', 'dates': ['2026-09-12'], 'birthdate': '2026-09-12', 'time': '10:00'}
```

An appointment date recorded as a date of birth. Harmless for booking, which reads `dates` — but
`update_patient` builds its body from the slots, so a sentence like *"mets a jour ... le 03/04/1978"*
could have written an unrelated date into a real patient's date of birth. A wrong date of birth is not
a cosmetic error in a hospital: it changes age-based dosing and it is how records get merged wrongly.

Fixed: `birthdate` is filled only on an explicit cue — `ne/nee le`, `date de naissance`, `naissance`,
`born`. `ne` alone is not accepted, being the French negation. Tools that want a plain date read
`dates`. Four tests pin it.

Worth noting how this was found. It was not found by the test suite, which had 110 passing tests, nor
by using the assistant. It was found by writing down what each sentence *should* produce and comparing
— which is the argument for the harness existing at all.

### The rules-engine baseline, before the model

```
                                                 rules
cases                                               25
task correct                                        13
task wrong                                           1
slot wrong or missing                                8
slot invented                                        0
asked when it should                                 11
asked when it should not                             0
UNSAFE (wrote when it should have asked)             0
```

Read that as: **safe but narrow.** Every describing, hedged or interrogative sentence produced a
question — zero unsafe rows, and nothing invented. What it cannot do is find a name it has no pattern
for: eight of the nine failures are a missing or mangled `name`, including
`'recherche Benali'` and `"je cherche le monsieur qui s'appelle white"`.

That is the gap MedGemma has to close, and the shape of the target is now explicit: **keep UNSAFE at
zero while turning those eight slot misses into hits.** An engine that reads more sentences correctly
but writes one thing nobody asked for is worse than the engine we have.

**114 tests pass.**

---

## Phase 14 — Standing up the model: steps 1 and 2

Following `MEDGEMMA-PLAN.md`.

### Step 1 — GPU passthrough: PASSED

The gate the whole phase depended on, and the step most likely to have cost a day.

```
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi

NVIDIA-SMI 595.84    Driver Version: 595.84    CUDA Version: 13.2
NVIDIA GeForce RTX 5070 Ti    851MiB / 16303MiB
```

Blackwell (`sm_120`) passthrough works, and the driver exposes **CUDA 13.2** — comfortably ahead of the
12.8 minimum, so the "no kernel image available" failure the plan warned about is unlikely. 851 MiB is
already in use by the desktop, leaving ~15.4 GB.

This also confirms Phase 12's diagnosis was right: nothing about the driver or the toolkit was ever
broken. Replacing the snap Docker with Docker Engine was the entire fix.

### Step 2 — weights: 8.1 GB on disk

```
/home/cerist/models/medgemma-4b-it   8.1G
  model-00001-of-00002.safetensors   4.96 GB
  model-00002-of-00002.safetensors   3.64 GB
  config.json, tokenizer.json, tokenizer.model, ...
```

Two deviations from the plan as written, both deliberate:

- **`/home/cerist/models`, not `/opt/models`.** `/opt` needs root to create; the home directory does
  not, and the bind mount into the container is read-only either way. One less privileged step in the
  install.
- **`snapshot_download` from the Python API, not the `hf` CLI.** The first attempt failed with
  `hf: command not found`: `pip install` as a non-root user with `HOME=/tmp` puts the entry point in
  `/tmp/.local/bin`, which is not on `PATH`. Calling the library directly avoids the question. Worth
  noting the failure was silent — `docker run ... | tail` reported exit code 0 because the pipeline's
  status is the last command's, so the download "succeeded" while producing nothing. `set -o pipefail`
  is in the retry.

The weights sit on the host and are mounted read-only. A 9 GB model inside an image layer would make
every rebuild enormous, and this way the weights survive container recreation — the failure mode
Finding 5 recorded on Server 1.

### Step 2b — the vLLM image

`vllm/vllm-openai:v0.11.0`, pulled in parallel with the weights. Still in progress at the time of
writing; it is a large image.

### Next

Steps 3 to 5: start the server, prove guided decoding works with nothing else in the path, then switch
`NLU_ENGINE`. Step 4 is a hard gate — if the model can return a task outside the schema's enum, the
"cannot name a task that does not exist" property is not real, and that property is what makes the
rest safe.

---

## Phase 15 — MedGemma live: steps 3 to 7

### Step 3 — vLLM started

`vllm/vllm-openai:v0.11.0` (38.5 GB image), MedGemma 4B loaded from disk, `Application startup
complete`, `/health` 200. **No Blackwell kernel problem** — the plan's most-expected failure did not
happen, because the driver exposes CUDA 13.2. GPU settles at **13.2 GB of 16.3 GB** with
`--gpu-memory-utilization 0.80` and `--max-model-len 4096`.

### Step 4 — guided decoding: PASSED

The hard gate. Constrained to a two-value enum with nothing else in the path:

```
CONTENT: {"task": "search_patient"}
```

Valid JSON, a value from the enum, and the right one. The property the whole design leans on — the
model *cannot* name a task that does not exist — is real and is enforced by the server, not the prompt.

### Steps 5 to 7 — three measured iterations

The first run was the point of building the harness. Every number below is measured, not estimated.

| | rules | run 1 | run 2 | run 3 (live) |
|---|---|---|---|---|
| task correct | 13 | **0** | 9 | **12** |
| task wrong | 1 | 0 | 0 | 1 |
| slot wrong or missing | 8 | 0 | 3 | **0** |
| slot invented | 0 | 0 | 1 | 0 |
| asked when it should not | 0 | **14** | 5 | 1 |
| read an unclear sentence | 0 | 0 | 2 | 2 |
| **UNSAFE** | 0 | 0 | **2→0** | **0** |

**Run 1 — unusable, and instructive.** Zero tasks correct; it asked a clarifying question for all 25
cases. But the replies showed it had understood everything:

```
'cherche le patient walter white' -> clarification: 'Rechercher le patient Walter White.'
```

That is a restatement, not a question. The model was using `clarification` as a general message field.
Not a comprehension failure — a format failure. Two fixes: **six few-shot examples** (a 4B model
follows a demonstration far better than a paragraph) and a code rule that a "clarification" which is
not a question, offered alongside a task already identified, is not a clarification.

**Run 2 — better, and it exposed two real bugs of mine.**

- `gender='M'` was invented from the first name "Ahmed" in a sentence about a GCS score, and the
  fabrication filter waved it through, because `"m"` is a substring of almost any French sentence. Now
  coded slots (`gender`, `gcs_total`, `karnofsky`) may only come from the deterministic extractor, and
  substring corroboration needs at least three characters.
- The model overlooked the phone number, the GCS and the time in sentences stating all three plainly.
  Fixed by merging the extractor's findings with the model's — each is good at a different half of the
  job.

It also showed **the harness itself was wrong**. Its UNSAFE column counted any unclarified case,
including two where the chosen task was a *read*. CA4 executes lookups without confirmation by design
and nothing in the record changes, so a read on an unclear sentence is a quality miss, not a safety
one. A go/no-go column that also counts harmless reads cannot be used to decide anything, so reads now
have their own bucket.

**A regression the metric caught.** Suppressing redundant questions — the fix for "asks for values the
sentence already gave" — sent **UNSAFE from 0 to 7**. The descriptive-phrasing check declines to add a
question when the model has already asked one; suppression then ran afterwards and dropped that
question, because the extractor had filled every slot. "Le GCS s'est aggrave a 6" became a write plan.
Descriptive phrasing is now decided **last** and returns directly, so nothing downstream can undo it,
and a test pins it.

That sequence is the argument for the whole harness. Seven ways to write something nobody asked for,
introduced by a change that made four other numbers better, caught in one run.

**Run 3 — better than the rules engine where it counts.** Zero slot errors against the rules engine's
eight, zero fabrications, zero unsafe. What remains is quality: one wrong task
(`search_patient` where `get_patient_summary` was expected — both reads), one question asked
needlessly, and two unclear sentences answered with a read rather than a question.

### Live

`NLU_ENGINE=medgemma`. Confirmed from the running container, not from the file:

```
Interpretation: MedGemma at http://vllm:8000/v1 (falling back to rules on failure)
```

**123 tests pass.**

### Finding 16 — the documented rollback did not work

`docker-compose.vllm.yml` set `NLU_ENGINE: medgemma` outright. A compose `environment` value overrides
`env_file`, so editing `.env` — the rollback written into `MEDGEMMA-PLAN.md` and
`DEPLOYMENT-GUIDE.md` — changed nothing while the overlay was applied. Found by using it: the revert
was performed, reported success, and the log still said MedGemma.

A rollback that appears to work and does not is worse than none. Now `${NLU_ENGINE:-medgemma}`, so the
overlay supplies a default and `.env` wins.

### Honest limits of this measurement

- **25 cases, written by us, not by clinicians.** The corpus should be replaced with phrasings
  collected from the department; until then these numbers are provisional.
- **Iterating the prompt against this corpus risks fitting it.** The safety rows are the ones that
  matter, and those are enforced in code rather than by the prompt, which is what makes them worth
  trusting.
- **Nothing here measures latency**, which a clinician will feel immediately.
- **The four task families have not been exercised through the model in the UI.** Step 6 of the plan is
  still outstanding and is the operator's to run.

---

# Documentation index

Kept last on purpose: this log grows by appending, and an index the document keeps growing past is
worse than none.

## The documents

| Document | Purpose |
|---|---|
| `IMPLEMENTATION-LOG.md` | this file — history and proof: every check with its evidence, all 14 findings, every decision and why |
| `MEDGEMMA-PLAN.md` | the plan for the remaining work: exact commands, configuration, tests and rollback |
| `README.md` | what the two components are, the security model, current state, outstanding work |
| `DEPLOYMENT-GUIDE.md` | step-by-step install for an operator |
| `chatbot_archi.md` | the original design, annotated where deployment disproved it |
| `openmrs-module-agentgateway/CHANGELOG.md` | module releases 1.1.0 → 1.1.3 |
| `../server2-stack/README.md` | the Server 2 stack: proxy, certificates, what it deliberately does not do |
| `../server2-stack/.env.example` | every setting, with what breaks without it |

## The code, and what each part is for

| Path | Role |
|---|---|
| `openmrs-module-agentgateway/` | Server 1: chat relay, delegated tokens, the audit filter, the operation log, rollback |
| `clinical-agent-service/app/orchestrator.py` | the pipeline and the two gates (clarification, confirmation) |
| `clinical-agent-service/app/tools/catalog.py` | the tools: which call each task family makes |
| `clinical-agent-service/app/nlu/rules.py` | the deterministic interpreter (default) |
| `clinical-agent-service/app/nlu/medgemma.py` | the model interpreter (`NLU_ENGINE=medgemma`) |
| `clinical-agent-service/app/nlu/schema.py` | the JSON schema, generated from the tool registry |
| `../server2-stack/docker-compose.yml` | proxy + agent |
| `../server2-stack/docker-compose.vllm.yml` | the model server, as an overlay |

## Test counts at the time of writing

| Suite | Tests |
|---|---|
| `openmrs-module-agentgateway` | 64 |
| `clinical-agent-service` | 110 |

## The fourteen findings, in one place

| # | Finding | Where it surfaced |
|---|---|---|
| 1 | tokens could not be minted for accounts without a username | "could not mint a delegated token" |
| 2 | the signing **private** key was configured where the public key belongs | a 500 on every turn |
| 3 | the private key is also written to the Tomcat log in cleartext | — |
| 4 | the patient identifier setting was empty | — |
| 5 | `openmrs-app` has no volume mounts; recreating it discards the module and truststore | — |
| 6 | `book_appointment` cannot work over FHIR: no `Appointment` resource here | tool self-reported unavailable |
| 7 | `fhir2`'s own filter rejects agent calls before the audit filter can authenticate them | "you do not have permission" |
| 8 | `identifier.system` is ignored by fhir2; only `identifier.type.text` resolves the type | — |
| 9 | the identifier needs an assignment location, which FHIR has no field for | "Identifier Location cannot be null" |
| 10 | fhir2 1.2.2 cannot create a patient at all: `setUuid(getId())` is unconditional | `Column 'uuid' cannot be null` |
| 11 | booking is a scheduling-model question, not a code one | — |
| 12 | the audit log could not display who acted | blank column |
| 13 | the clarification loop could not be escaped | the same question forever |
| 14 | Docker on Server 2 was a snap and could not pass through the GPU | "no such file or directory" for a file that exists |

Five of these were reported by the wrong layer. That is the single most useful thing to know before
working on this system: read the deployed source and jars, do not trust the message.

## What is deliberately not documented

Secrets. The channel secret and signing keys live only in OpenMRS's settings and Server 2's `.env`.
`Secrets.txt` was deleted and the keys it held were rotated.

The **retired** signing private key appears in this project's conversation transcript, because OpenMRS
logs every global-property save with its value and the Tomcat log was read during diagnosis. It has
been rotated and is useless, but if that transcript is stored anywhere, treat it as containing a
retired credential.
