# Phase 3 — `stt-service` + compose overlay + vhost: complete

Built and verified 2026-09-02 on Server 2. **The dictation service is live and reachable only
from Server 1.** No OpenMRS-side change yet — that is phase 5.

---

## What was built

| | |
|---|---|
| `STT/stt-service/` | the gateway: FastAPI, ~350 lines, **53 tests passing** |
| `server2-stack/docker-compose.stt.yml` | `stt-gateway` + `stt-engine` on their own `stt_net` |
| `server2-stack/nginx/templates-stt/stt.conf.template` | the vhost — TLS, IP allowlist, `return 404` outside two endpoints |
| `server2-stack/nginx/nginx.conf` | **+1 line** — the `stt_transcribe` rate-limit zone must live in `http{}` |
| `server2-stack/stt/lexicon-neurochir.txt` | 53 neurosurgery terms, read fresh per request |
| `server2-stack/.env` | new keys, including a **freshly generated** `STT_CHANNEL_SECRET` |
| `certs/agent.crt` | reissued by hospitalCA with `stt.hospital.lan` added as a SAN |

The gateway's whole dependency list is `fastapi`, `uvicorn`, `httpx`, `PyJWT`, `cryptography`.
**No audio library, no numpy, no ML dependency** — the browser sends 16 kHz mono PCM and the only
transformation is a 44-byte WAV header (`struct`).

---

## Acceptance checks

Re-run from the server2 README's own list, for the new hostname.

| | Check | Result |
|---|---|---|
| **A** | TLS certificate valid for `stt.hospital.lan` | ✅ `Verify return code: 0 (ok)`, SAN carries both names |
| **A2** | `agent.hospital.lan` still works on the reissued cert | ✅ 200 — no regression from the swap |
| **C** | Server 1 **can** reach it | ✅ `{"status":"ok","model":"Qwen3-ASR-0.6B","language":"fr"}` |
| **D** | Nobody else can | ✅ 403 from Server 2's own address |
| **E** | Bare IP gets nothing | ✅ connection closed, no response (444) |
| **F** | Nothing else is exposed | ✅ `/docs`, `/redoc`, `/openapi.json`, `/v1/audio/transcriptions`, `/` → **404**. Plain HTTP → no response |

### The auth chain, through the real vhost

| Presented | Result |
|---|---|
| no channel key, no token | **403** |
| wrong channel key | **403** |
| correct channel key, no token | **401** |
| correct key, garbage token | **401** |

Channel trust is checked first and fails without detail, so probing the port reveals nothing about
whether a token would have been accepted.

### The isolation property (§3/Q2), proven

The reason the two services have separate secrets is that a compromise of one must not reach the
other. Tested in both directions:

| | Result |
|---|---|
| the **agent's** secret against the STT service | **403** |
| the **STT** secret against the clinical agent's `/chat` | **403** |
| the STT secret against the STT service *(control)* | **401** — channel passed, token refused |

And at the network layer, which is what makes "completely decoupled" true rather than a diagram:

```
  clinical-agent -> stt-engine   : unreachable
  clinical-agent -> stt-gateway  : unreachable
  stt-gateway    -> vllm         : unreachable
  stt-gateway    -> clinical-agent: unreachable
```

nginx is the only container on both networks. Neither STT container publishes a port.

---

## End to end, on real audio

OpenMRS cannot mint a `purpose=stt` token until phase 5, so the transcription path was proved with a
throwaway gateway holding a **test** keypair, pointed at the **real** engine and the **real** lexicon.
Input was real French clinical speech from the phase-1 corpus, header-stripped to raw Int16LE PCM —
byte-for-byte what the browser will send.

```
17.0s of audio -> 0.23s
transcript: Et je vais vous marquer donc les rendez-vous donc la prochaine fois
            si vous voulez venir lundi premier septembre parfait à dix heures
            parfait à l'air de bingo voilà.
```

Date and time both intact. **0.23 s for 17 s of audio** — a realtime factor of 0.013.

### The guards, against the real engine

| Input | RMS | Result |
|---|---|---|
| digital silence, 3 s | 0 | `reason=silence`, engine never called |
| quiet room noise, 3 s | ~5 | `reason=silence`, engine never called |
| tone below threshold | 106 | `reason=silence` |
| tone above threshold | 282 | reaches the engine |
| speech-level tone | 2828 | reaches the engine |
| 0.006 s click | — | `reason=too_short` |
| 31 s | — | **413** `{"error":"too_long"}` |

The §6.6 guard works and does not over-block: real speech passed, and the boundary sits where the
threshold says it does.

---

## Two things that went wrong, and what they cost

### `--gpu-memory-utilization 0.25` crash-looped

Twenty restarts before the cause was read:

```
ValueError: To serve at least one request with the model's max seq len (8192),
0.88 GiB KV cache is needed, which is larger than the available KV cache memory (0.61 GiB).
```

The 0.25 figure came from phase 1's measured floor — weights 2.17 + activation 1.05 + graphs 0.09
≈ 3.3 GiB. **That omitted the KV cache vLLM demands for one request at the full
`--max-model-len`**: 0.88 GiB at 8192, putting the real floor at ~4.2 GiB. Phase 1 measured at 0.35,
so the gap never showed.

Corrected to **0.30** (~4891 MiB), which starts cleanly with 1.39 GiB = 12,960 tokens of KV.

This is the *second* time the same class of error has bitten in this project — the phase-1 spike
failed for the same reason with a different number. The lesson is written into the compose file:
**`--max-model-len` is not free; it sets a floor on the memory fraction.**

### One of my own test cases was wrong, not the code

`reason=silence` came back for uniform noise at ±250, which looked like the guard over-blocking.
Uniform noise on ±250 has RMS ≈ 144 — below the 200 threshold. The code was right; the test case was
badly chosen. Re-tested with tones at known amplitudes, which is why the table above reports RMS
rather than a description.

---

## State of the machine

| Container | Network | Published | Status |
|---|---|---|---|
| `server2-proxy` | server2_net + stt_net | **:80, :443 only** | healthy |
| `clinical-agent` | server2_net | none | healthy |
| `vllm` (MedGemma FP8) | server2_net | none | healthy |
| `stt-gateway` | **stt_net** | none | healthy |
| `stt-engine` (Qwen3-ASR) | **stt_net** | none | healthy |

**VRAM: 12859 MiB used, 2962 MiB free** — MedGemma FP8 at 0.50 plus Qwen3-ASR at 0.30, both resident,
with room left.

Backups taken before every edit, in `backup files/`: the compose overlay, `nginx.conf`, `.env`, and
the previous certificate and key.

---

## Still to do before a clinician can use this

**Phase 5** is the whole remaining OpenMRS side, and nothing in phase 3 can be exercised by a human
until it lands:

- `agentgateway` 1.2.0 — a `purpose=stt` token, the `App: agentgateway.voice.use` privilege,
  `agentgateway.sttChannelSecret` and `agentgateway.sttServiceUrl` settings,
  `TranscribeRelayController`, and `agent-voice.js` with the click-to-start/click-to-stop button;
- `stt.hospital.lan` must resolve **from Server 1 and from inside the `openmrs-app` container** —
  `/etc/hosts` plus `extra_hosts:`, exactly as `agent.hospital.lan` needed. Verify with
  `docker exec openmrs-app getent hosts stt.hospital.lan`;
- the `STT_CHANNEL_SECRET` from `server2-stack/.env` copied into the new OpenMRS global property.

**Phase 4** (the model bake-off) still needs the clinician corpus — Q-D, unchanged and still the
critical path for judging model quality.
