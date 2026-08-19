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

---

## Documentation index

Everything produced or amended during this work, and what each is for.

| Document | Purpose | Current as of |
|---|---|---|
| [`IMPLEMENTATION-LOG.md`](IMPLEMENTATION-LOG.md) | this file — the history and the proof: every check with its evidence, all 13 findings, every decision and why | 2026-08-18 |
| [`README.md`](README.md) | what the two components are, the security model, current state, outstanding work | 2026-08-18 |
| [`DEPLOYMENT-GUIDE.md`](DEPLOYMENT-GUIDE.md) | step-by-step install for an operator | 2026-08-18 |
| [`chatbot_archi.md`](chatbot_archi.md) | the original design, with a revision note where deployment disproved §3 | 2026-08-18 |
| [`openmrs-module-agentgateway/CHANGELOG.md`](openmrs-module-agentgateway/CHANGELOG.md) | module releases 1.1.0 → 1.1.3, with operator notes | 2026-08-18 |
| `../server2-stack/README.md` | the Server 2 stack: proxy, certificates, what it deliberately does not do | 2026-08-18 |
| `../server2-stack/.env.example` | every setting, with what breaks without it | 2026-08-18 |

Amended in this pass, because they had gone stale and would have misled a fresh install:

- **`DEPLOYMENT-GUIDE.md`** — new step A5b (the four values needed to create patients, including the
  two uuids that are not visible in any admin screen and must be read out of a 500 error page);
  step B2 lists them; the private-key warning now gives the length and prefix of each key, since that
  mistake was made here and cost a week; the limitations section now covers appointments and
  `null null`.
- **`server2-stack/README.md`** — the "the Nginx configuration has never been started" caveat is
  struck through and dated, since checks A–F have now all been run against the live hospital; three
  new entries explain the relay prefix, why creates go through `webservices.rest`, and the missing
  volume mounts on `openmrs-app` — each being something a well-meaning reader would otherwise
  "simplify" and break.

### What is deliberately *not* documented anywhere

Secrets. The channel secret and the signing keys live only in OpenMRS's settings and Server 2's
`.env`. `Secrets.txt` was deleted, the keys it held have been rotated, and the previous `.env` is kept
as `.env.bak.20260818` on Server 2 only.

One caveat worth stating plainly: the **retired** signing private key appears in this project's
conversation transcript, because OpenMRS logs every global-property save with its value and the
Tomcat log was read during diagnosis. It has been rotated and is useless, but if that transcript is
stored anywhere, treat it as containing a retired credential.

---

## OHIF removal — 2026-08-18

Architectural decision: the DICOM viewer is deployed on **Server 1**, next to Orthanc, and Server 2
carries no browser-facing service at all. Every OHIF trace is gone from the Server 2 stack.

### Why Server 1 is the right home for it

OHIF runs in the clinician's browser and talks to Orthanc directly. Hosting it on a different machine
from Orthanc makes every study a cross-origin request — which is why Server 1 already runs an
`orthanc-cors-proxy` container. Serving the viewer from the GPU host would mean either widening those
CORS rules or proxying DICOM through Server 2, and both add a moving part to the imaging path in
exchange for nothing. Server 2 exists to hold the GPU and the assistant.

### Removed

| Path | Was |
|---|---|
| `server2-stack/docker-compose.ohif.yml` | the overlay adding an `ohif/app:v3.9.2` container and its vhost |
| `server2-stack/nginx/templates-ohif/ohif.conf.template` | the browser-facing vhost template |

Both were copied to the session scratchpad before deletion. Note that the overlay mounted
`../OHIF/ohif-app-config.js`, which its own comments flagged as still containing **plaintext Orthanc
credentials** — that file is outside this stack and is untouched here, but it remains a problem
wherever the viewer is finally served from, and is worth resolving on Server 1.

### Amended

| File | Change |
|---|---|
| `docker-compose.yml` | `NGINX_ENVSUBST_FILTER` is now `^(AGENT_\|OPENMRS_)`; header comments no longer describe an OHIF overlay |
| `.env`, `.env.example` | the `OHIF_SERVER_NAME` block removed |
| `README.md` | the "add another service" recipe no longer tells the reader to copy a template that does not exist; a new "Why there is no viewer here" section records the decision and its reason |

`.env.bak.20260818` still mentions OHIF and was deliberately left alone: it is a point-in-time backup
of the file as it stood before the key rotation, and editing a backup makes it useless as one.

### Verified not broken

| Check | Result |
|---|---|
| `docker-compose.yml` parses; services and nginx environment | 2 services (`nginx`, `clinical-agent`), no OHIF variables |
| nginx tree | `nginx.conf`, three snippets, `templates/agent.conf.template` — nothing dangling |
| every `${VAR}` in the templates and snippets | `AGENT_PROXY_READ_TIMEOUT`, `AGENT_SERVER_NAME`, `OPENMRS_SERVER_CIDR` — all still covered by the narrowed envsubst filter |
| variables the compose file requires (`:?`) | all present in `.env` |
| references to the deleted files | none outside the README's explanation of why they are gone |

Nothing in the running stack changes: the overlay was never enabled, so the live nginx configuration
never contained an OHIF vhost. The narrowed envsubst filter takes effect on the next container
recreate, and `AGENT_`/`OPENMRS_` remain covered.

Mentions of `viewer.hospital.lan` in `nginx/snippets/tls.conf`, `1-make-agent-csr.sh` and
`2-sign-agent-csr.sh` were **kept**: they describe the certificates hospitalCA signs on Server 1,
which is exactly where the viewer now lives. Removing them would have made those comments wrong.
