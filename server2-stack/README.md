# Server 2 stack — TLS reverse proxy + the clinical assistant

Everything that runs on the GPU host, behind one Nginx that terminates TLS and is the only
container publishing a port.

## The two machines

| | Address | Hostnames | Runs |
|---|---|---|---|
| **Server 1** | `10.0.211.249` | `openmrs.hospital.lan`, `orthanc.hospital.lan`, `viewer.hospital.lan` | OpenMRS + agentgateway, Orthanc, the viewer, and **hospitalCA** — the authority that signs every certificate in the hospital |
| **Server 2** | `10.0.211.250` | `agent.hospital.lan` | this stack: Nginx + the Clinical Agent Service |

```
                          Server 2 — 10.0.211.250
  ┌──────────────────────────────────────────────────────────────┐
  │  nginx  :443 :80   ← the only published ports                │
  │    ├── agent.hospital.lan   → clinical-agent:8000            │
  │    │      allow 10.0.211.249/32 · deny all                   │
  │    └── anything else        → 444, connection closed         │
  │                                                              │
  │  clinical-agent :8000   ─── server2_net, no published port   │
  └──────────────────────────────────────────────────────────────┘
              ▲                                    │
              │ https, only from Server 1          │ https to openmrs.hospital.lan
              │                                    ▼
                          Server 1 — 10.0.211.249
```

## One authority, not two

`hospitalCA` on Server 1 already signs `openmrs`, `orthanc` and `viewer`. `agent.hospital.lan`
becomes a fourth certificate from that same authority — issued by the two-step flow below, where
the CA's private key never leaves Server 1 and only a signing request travels.

That matters because trust has to work in **both directions**:

- the agent calls `https://openmrs.hospital.lan`, so it must trust hospitalCA;
- OpenMRS calls `https://agent.hospital.lan`, so the **JVM inside the OpenMRS container** must
  trust hospitalCA too.

The second one is the step everybody forgets. Java does not use the operating system's trust
store — it has its own, and it starts out knowing nothing about your hospital. Skip that import
and the chat says "assistant indisponible" forever while the real cause is an
`SSLHandshakeException` in the Tomcat log.

## Why a proxy, and why the agent publishes no port

If the agent published `8000` itself, the proxy would be **optional**: anything that could reach
`10.0.211.250` could skip TLS, skip the Server-1-only rule and skip the rate limit by talking to
the raw port. Publishing only Nginx makes the proxy the single way in.

This is also why the URL is `https://agent.hospital.lan` and not `...:8000`. Port 8000 is the
*application's* port and is never exposed; TLS lives on 443.

---

## Setting it up

### 1. DNS — Server 1 must be able to find Server 2

On **Server 1**:

```bash
echo "10.0.211.250  agent.hospital.lan" | sudo tee -a /etc/hosts
```

If OpenMRS runs in Docker, the *container* needs it too. Either add to the OpenMRS service in its
compose file:

```yaml
extra_hosts:
  - "agent.hospital.lan:10.0.211.250"
```

or, to check quickly without a restart:

```bash
docker exec openmrs-app getent hosts agent.hospital.lan
```

### 2. Configuration

On **Server 2**:

```bash
cd server2-stack
cp .env.example .env
```

The defaults already match this hospital. Fill in the two secrets from OpenMRS
(**Administration → Settings → Agentgateway**):

| Variable | Where it comes from |
|---|---|
| `AGENT_CHANNEL_SECRET` | the **Channel Secret** field, copied exactly |
| `OPENMRS_JWT_PUBLIC_KEY` | the **Signing Public Key** field, as **one single line** |

Creating patients needs four more, none of which can be guessed — all four were discovered by
failing against the live instance, and each failure named the wrong thing (see Findings 8–10):

| Variable | Where it comes from | What breaks without it |
|---|---|---|
| `OPENMRS_PATIENT_IDENTIFIER_TYPE` | Administration → Manage Identifier Types, the name (`OpenMRS ID` here) | nothing today; kept for the FHIR path |
| `OPENMRS_PATIENT_IDENTIFIER_TYPE_UUID` | the same type's uuid | the create is refused: no identifier type |
| `OPENMRS_IDGEN_SOURCE_UUID` | Administration → Manage Patient Identifier Sources | the assistant has to ask the clinician for an identifier, because the type validates a check digit and a value cannot be invented |
| `OPENMRS_IDENTIFIER_LOCATION_UUID` | any Location's uuid (`Registration Desk` here) | `Identifier Location cannot be null for <identifier>` |

Neither uuid is visible in the OpenMRS admin screens. Both were read out of
`/ws/rest/v1/idgen/identifiersource` and `/ws/rest/v1/location` — which **return HTTP 500 on this
deployment**, because its XStream/XML marshaller cannot initialise and a browser asks for XML. The
data is in the exception message. That 500 affects any REST client asking for XML or HTML and is
unrelated to this project; the agent is unaffected because it sends `Accept: application/json`.

The public key box in OpenMRS wraps the text across several lines to fit. That wrapping is not
part of the key — paste it into `.env` with no spaces and no line breaks. Never copy **Signing
Private Key**: anyone holding it can impersonate any user in the hospital.

### 3. Certificate — request here, sign on Server 1

On **Server 2**:

```bash
./1-make-agent-csr.sh
```

Copy the request to Server 1, into the folder that already holds `hospitalCA.crt` and
`hospitalCA.key`:

```bash
scp certs/agent.csr certs/agent-san.cnf 2-sign-agent-csr.sh user@10.0.211.249:~/certificates/
```

On **Server 1**, in that folder:

```bash
./2-sign-agent-csr.sh
```

It prints the certificate's names and verifies the chain before it hands anything back.

### 4. Teach the OpenMRS container to trust hospitalCA

Still on **Server 1** — the step that is invisible until it bites:

```bash
docker cp hospitalCA.crt openmrs-app:/tmp/hospitalCA.crt
docker exec -u 0 openmrs-app keytool -importcert -noprompt -alias hospital-ca -file /tmp/hospitalCA.crt -keystore $JAVA_HOME/jre/lib/security/cacerts -storepass changeit
docker restart openmrs-app
```

A container rebuild wipes the trust store, so put this in whatever provisions Server 1.

### 5. Bring the certificate back and start

On **Server 2**:

```bash
scp user@10.0.211.249:~/certificates/agent.crt user@10.0.211.249:~/certificates/hospitalCA.crt certs/
docker compose up -d
```

`hospitalCA.crt` is needed here too — the compose file mounts it into the agent container so it
can verify Server 1 rather than trusting it blindly.

### 6. Point OpenMRS at the assistant

**Administration → Settings → Agentgateway**:

| Setting | Value |
|---|---|
| `agentgateway.agentServiceUrl` | `https://agent.hospital.lan` — no port |
| `agentgateway.agentTimeoutMillis` | `30000` — must stay **below** `AGENT_PROXY_READ_TIMEOUT` (60s) |
| `agentgateway.selfBaseUrl` | `http://localhost:8080/openmrs` — plain HTTP on loopback is correct here, it never leaves the machine |

Then assign the privileges under **Administration → Manage Roles**:

| Privilege | Who |
|---|---|
| `App: agentgateway.chat.use` | Surgeon, OR Nurse, Radiologist/Technician, Admissions Staff |
| `App: agentgateway.chat.write` | Surgeon, Admissions Staff |
| `App: agentgateway.rollback` | System Administrator only |

**Nothing appears in the interface until these are assigned.** The links are privilege-gated by
design, and a role with none of them sees no assistant at all.

---

## Checking it works

Run these in order. Each one isolates a different link in the chain, so the first failure tells
you where the problem is.

**A. The proxy is up and the certificate is right** — on Server 2:

```bash
curl -fsS --cacert certs/hospitalCA.crt https://agent.hospital.lan/health
```

Expect `{"status":"ok",...}`. A certificate complaint means the name in the certificate does not
match; `Connection refused` means the container is not running.

**B. `fhir_capabilities_known` is true** — same output as above. If it is `false`, the agent
could not read `https://openmrs.hospital.lan/ws/fhir2/R4/metadata`: check DNS, and check that
`hospitalCA.crt` really is mounted (`docker compose logs clinical-agent`).

**C. Server 1 can reach it** — on Server 1:

```bash
curl -fsS --cacert ~/certificates/hospitalCA.crt https://agent.hospital.lan/health
```

**D. Nobody else can** — from any other machine on the LAN. This **must** fail with 403:

```bash
curl -k -sS -o /dev/null -w '%{http_code}\n' https://agent.hospital.lan/health
```

**E. The bare IP gives nothing** — no response at all is the correct result:

```bash
curl -k -sS https://10.0.211.250/health
```

**F. What the assistant can actually do here** — on Server 1:

```bash
curl -fsS --cacert ~/certificates/hospitalCA.crt -H "X-Agent-Channel-Key: PASTE_CHANNEL_SECRET" https://agent.hospital.lan/capabilities
```

Every tool comes back `available: true` or `false` **with a reason**. Re-run this after every
`fhir2` upgrade — coverage is read from the deployed module's own capability statement, never
assumed.

---

## Adding another service later

Two files, and nothing already running is edited.

**1. An overlay** — `docker-compose.<service>.yml`:

```yaml
services:
  nginx:
    environment:
      MYSERVICE_SERVER_NAME: ${MYSERVICE_SERVER_NAME:?set MYSERVICE_SERVER_NAME}
    volumes:
      - ./nginx/templates-myservice/myservice.conf.template:/etc/nginx/templates/myservice.conf.template:ro
    depends_on:
      myservice:
        condition: service_started

  myservice:
    image: …
    expose: ["8080"]          # never "ports:"
    networks: [server2_net]
```

**2. A vhost template** — start from `templates/agent.conf.template`. It is a server-to-server
vhost: TLS, an IP allowlist, a rate limit, and `return 404` for anything outside the endpoints it
means to expose. A browser-facing service differs in three ways — no `allow`/`deny` (clinicians come
from all over the LAN), the security headers snippet matters because a browser will honour it, and
`try_files … /index.html` if it is a single-page app.

Then add the hostname to the certificate (steps 3–5 again, listing both names) and start it:

```bash
docker compose -f docker-compose.yml -f docker-compose.myservice.yml up -d
```

Two things that bite:

- **`expose:`, never `ports:`.** A published port bypasses everything the proxy enforces.
- **Substituted variables need a matching prefix.** `NGINX_ENVSUBST_FILTER` is
  `^(AGENT_|OPENMRS_)`; extend it to cover your own variables, or nginx's own `$host` and
  `$remote_addr` become fair game for substitution and get silently blanked.

### Why there is no viewer here

This stack used to carry an OHIF overlay as a worked example. It was removed on 2026-08-18: the
viewer belongs on **Server 1**, next to Orthanc.

That is not tidiness. OHIF in a browser talks to Orthanc directly, so hosting it on a different
machine from Orthanc makes every study a cross-origin request — which is why Server 1 runs an
`orthanc-cors-proxy` container at all. Putting the viewer on the GPU host would mean either widening
those CORS rules or proxying DICOM through Server 2, and both add a moving part to the imaging path
in exchange for nothing. Server 2 exists to hold the GPU and the assistant.

If a browser-facing service ever does belong here, the two-file mechanism above still applies; the
removed overlay is described in `../chatbot-neuro/IMPLEMENTATION-LOG.md` under "OHIF removal" if you
want to see what one looked like.

---

## What this does not do

**No mTLS.** ADR-9 offers a client certificate instead of the shared channel secret, and Nginx
would enforce it in one line. The blocker is on the OpenMRS side: the module's `HttpJsonClient`
uses `HttpURLConnection` with the default `SSLSocketFactory` and cannot present a client
certificate. Turning it on today would simply break the chat. It is a module code change, not a
proxy setting.

**~~The Nginx configuration has never been started.~~** *Superseded 2026-08-18.* The stack is
running against the hospital's OpenMRS. Checks A through F above were all run, including D and E —
a request from any address other than Server 1 is refused with 403, and the bare IP gets no response
at all. The `allow`/`deny` rule was proved rather than assumed.

**The agent does not call `/ws/fhir2/*` directly, and must not be "simplified" to do so.**
Every delegated call goes to `/module/agentgateway/relay` + the real path. `fhir2` registers its own
authentication filter on `/ws/fhir2/*` and, being a bundled module, starts — and registers it —
before `agentgateway`; module filters run in start order, so fhir2 answers **401** before the audit
filter can authenticate the clinician. The relay lands on a path fhir2 does not guard, where the
filter authenticates and then *forwards* to the real servlet (module filters are not mapped for
`FORWARD`). Removing the prefix silently breaks every FHIR call. See `IMPLEMENTATION-LOG.md`
Finding 7.

**Creating a patient goes through `webservices.rest`, not FHIR.** `fhir2` 1.2.2 calls
`setUuid(resource.getId())` unconditionally on the patient, the name and the identifier, and a FHIR
create carries no ids — so every uuid lands null and the insert is refused with
`Column 'uuid' cannot be null`. Reads, searches and updates are still FHIR; update is unaffected
because it sends back a resource that was fetched. See Finding 10.

**`openmrs-app` on Server 1 has no volume mounts.** The installed module and the hospitalCA
truststore import live in that container's writable layer, so `docker compose down && up` discards
both. Use `docker restart`. See Finding 5.

**Rate limiting is coarse.** 60 requests/minute on `/chat`, burst 10, keyed on source address —
and every turn arrives from Server 1's single address, so this limits the *hospital*, not a user.
It is a runaway-loop backstop. Per-clinician limiting would have to happen inside the agent,
keyed on the token's subject.
