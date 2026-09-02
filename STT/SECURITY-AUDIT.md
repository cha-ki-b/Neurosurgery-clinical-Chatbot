# Security audit — dictation feature

Conducted 2026-09-02, after phases 1–3 and 5. Covers `stt-service`, the compose/nginx layer on
Server 2, and `agentgateway` 1.2.0's dictation path on Server 1.

**Three findings. All three fixed and re-verified.** One was critical and silently undid a property
this project had built, tested, documented and demonstrated.

---

## Finding 1 — CRITICAL: the two services held each other's channel secrets

**What was wrong.** `docker-compose.stt.yml` had `env_file: .env`, and so did `docker-compose.yml`.
`env_file` hands a container the *whole file*. `.env` held both `AGENT_CHANNEL_SECRET` and
`STT_CHANNEL_SECRET`, so:

```
stt-gateway    had AGENT_CHANNEL_SECRET
clinical-agent had STT_CHANNEL_SECRET
```

`stt-gateway` also held every OpenMRS clinical setting — base URL, patient identifier type UUID,
idgen source UUID, identifier location UUID, phone attribute type UUID — none of which it has any
use for.

**Why it matters.** The whole reason the two services have separate secrets (STT-PLAN.md §3/Q2) is
that the dictation service decodes attacker-shaped binary audio through a model runtime, which is a
larger attack surface than the agent's JSON — so *a compromise there must not reach `/chat`*. I
tested that property at the protocol layer and it passed: presenting the agent's secret to the
dictation service returns 403, and vice versa. It passed because the **request** was rejected. It
said nothing about whether the process could simply read the other secret out of its own
environment. It could.

**What saved it from being exploitable today** — and why that is not reassurance: `stt-gateway` sits
on `stt_net` alone and cannot reach `clinical-agent` at all, and the agent's vhost only accepts
Server 1's address. So an attacker holding the secret still had no path to use it *from that
container*. But defence in depth is supposed to be layers that each hold on their own. This one was
being held up entirely by the layer beneath it, and an attacker who could exfiltrate the value could
use it from anywhere else with a route to Server 1.

**Fix.** A separate `server2-stack/.env.stt`, containing only what the gateway needs. `.env` keeps
just the six non-secret STT variables docker compose must interpolate (`STT_SERVER_NAME`,
`STT_VLLM_TAG`, `STT_MODEL_PATH`, `STT_MODEL`, `STT_GPU_FRACTION`, `STT_PROXY_READ_TIMEOUT`).
Both files are `0600`.

**Re-verified after the fix**, both directions:

```
✅ stt-gateway    does NOT have AGENT_CHANNEL_SECRET
✅ clinical-agent does NOT have STT_CHANNEL_SECRET
   OpenMRS clinical config vars visible to stt-gateway: 0
```

**The lesson, which generalises past this bug.** I verified the boundary by testing what the service
*answered* and never checked what it *held*. `docker exec <container> printenv` would have found it
in one command at any point. When §12's imaging service is built on this pattern, that check belongs
in its acceptance list.

---

## Finding 2 — MEDIUM: `lang` was concatenated into a URL unvalidated

**What was wrong.** `TranscribeRelayController` built its target as:

```java
sttServiceUrl + "/v1/transcribe" + (isNotBlank(lang) ? "?lang=" + lang.trim() : "")
```

`lang` is a request parameter — browser-controlled, and reachable by anything that can reach
`transcribe.form` in an authenticated session. It went on to the gateway, which passed it
unvalidated into the form data sent to vLLM.

**Impact.** Bounded but real: extra query parameters, garbage in nginx's access log where a URL
should be, and reliance on Java's URL parser to reject control characters rather than on our own
check. Not remote code execution; the kind of thing that is cheap to close and awkward to explain
later.

**Fix.** A whitelist on both sides — `[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?` — because the set of valid
language tags is tiny and known, so anything outside it is a mistake or an attempt and both deserve
the same answer. Anything off-shape is dropped and the configured default (`fr`) applies. Covered by
nine new gateway tests including `fr&extra=1`, `../../etc/passwd`, CRLF, and a 200-character string.

*One of those tests was itself wrong first:* passing raw CRLF made `httpx` refuse to build the
request, so the test exercised `httpx` rather than the service. Percent-encoded now, as a real
client would send it.

---

## Finding 3 — LOW: file permissions and missing gitignores

`server2-stack/.env` was `0644` — world-readable on a shared host, holding two channel secrets. Now
`0600`, as is the new `.env.stt`. `certs/agent.key` was already `0600`.

`stt-service/` and `STT/` had no `.gitignore`. Added, before anything is pushed anywhere.

---

## What was checked and found correct

Recorded so the next audit does not redo it, and so the claims below are falsifiable.

| Area | Finding |
|---|---|
| **Secret comparison** | `hmac.compare_digest` — constant-time, cannot be used to guess the secret one byte at a time |
| **Check order** | Channel trust is verified *before* the token, and 403 carries no detail. Probing the port reveals nothing about whether a token would have been accepted |
| **Token algorithm** | RS256 pinned. `alg: none` and HS256-signed-with-the-public-key both refused — the classic algorithm-confusion pair, tested explicitly |
| **Audience separation** | A chat token cannot drive the GPU; a dictation token cannot open a chat turn. And `AgentAuditFilter` still verifies only `clinical-agent-service`, so **a dictation token can never authenticate an OpenMRS API call** — that one came free from the existing design |
| **Write capability** | Dictation tokens carry `may_write: false` unconditionally. There is no branch that could make it true |
| **Network isolation** | `clinical-agent ↔ stt-engine`, `clinical-agent ↔ stt-gateway`, `stt-gateway ↔ vllm`, `stt-gateway ↔ clinical-agent`: all unreachable. Verified, not assumed |
| **Published ports** | Only nginx. Neither STT container publishes anything; the model endpoint has no authentication and is reachable only over `stt_net` |
| **API documentation** | `/docs`, `/redoc`, `/openapi.json` disabled in FastAPI *and* 404'd by nginx |
| **Audio logging** | Never, at any level. There is deliberately no setting for it |
| **Transcript logging** | Off by default (`LOG_TRANSCRIPTS=false`), same convention and reason as the agent's `LOG_PROMPTS` |
| **Statelessness** | Audio exists for the lifetime of one request. No session store, no disk, no TTL to get wrong |
| **XSS via transcript** | The transcript reaches the DOM only through jQuery `.val()`, which sets a value property, not HTML |
| **Concurrency slot leak** | The per-user counter is released in a `finally`, tested by forcing an engine failure and confirming the next request succeeds |
| **Malformed audio** | Raw PCM is wrapped in a header this service writes, declaring mono/16-bit/16 kHz. There is no container to parse — which is the security argument for §2.3's design, not just the dependency one |
| **Secrets in documentation** | Exact-match scan of every doc and source file against the two live secrets and the HF token: clean. No private key material outside `certs/` |

---

## Known and accepted

Not defects — decisions, recorded so they are decisions rather than oversights.

**Tokens are replayable within their five-minute lifetime.** No `jti` cache, exactly as the chat path
already documents. Mitigated by the short expiry, TLS, the channel secret and the IP allowlist. Worth
revisiting only if the token lifetime is ever raised.

**`transcribe.form` has no CSRF token**, consistent with the existing `chat.form`. A cross-site
request could make an authenticated clinician's browser spend GPU time; it could not read the
transcript, because the same-origin policy stops the attacker reading the response. Low impact, and
fixing it belongs with `chat.form` rather than separately.

**nginx is the one container on both networks.** That is inherent to it being the proxy, and the
vhosts control what is reachable — but it does mean a careless `location` block in any future vhost
could bridge `server2_net` and `stt_net`. Worth remembering when §12's imaging vhost is written.

**Rate limiting is coarse**, as for `/chat`: every request arrives from Server 1's single address, so
nginx's 120/min bounds the hospital rather than a user. Real per-clinician limiting is
`STT_MAX_CONCURRENT_PER_USER` inside the gateway, keyed on the token subject.

**The model container has no authentication.** By design, and the reason it must never gain a
`ports:` entry.

---

## Is the STT service decoupled?

Yes, on every axis — and now genuinely, rather than nearly.

| Axis | Status |
|---|---|
| Process | separate container |
| Network | separate docker network; mutual unreachability verified |
| Secret | separate channel secret — **and, since finding 1, separate environments** |
| Identity | separate token audience and purpose |
| Code | separate repository directory, `~/stt-service/`, no shared imports |
| Model | separate vLLM instance, separate weights |
| Failure | the gateway returns 503 and the composer stays a plain text box; the chat is unaffected |
| Removal | `docker compose` without `-f docker-compose.stt.yml`, and revoke one privilege. Nothing else changes |

The one thing they share is the **RSA public key** used to verify tokens — which is public, and
sharing it avoids a second key lifecycle for no security gain. The private key remains where it has
always been: inside OpenMRS, never transmitted.
