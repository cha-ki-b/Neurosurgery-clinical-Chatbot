# Speech-to-text for the clinical assistant — design

**Status: design agreed, no code written, nothing on the machine changed.**
Revised 2026-09-01 after three decisions were taken (§0). Supersedes the first proposal.

Written after reading `chatbot-neuro/` (README, HANDOFF, server2-stack README, the module source and
the chat widget), `server2-stack/` (compose, nginx, `.env`),
`system_report_Cerist-Neurochir_20260831_222707.txt`, and `candidate-models`.

Everything asserted about this machine was read off it directly; the commands are in §1. Everything
asserted about a model comes from its own card or repository, cited. Where I could not verify
something, it says so.

---

## 0. Decisions taken

| | Decision | What it removes |
|---|---|---|
| **Latency model** | **Instant-on-stop.** Nothing appears while speaking; the final transcript lands in the input box ~0.3–0.5 s after recording stops. | Per-frame streaming, server-side session buffers, partial decodes, the coalescing throttle, session-ownership checks. The service becomes **stateless**. |
| **OpenMRS side** | **Extend `agentgateway` → 1.2.0**, own package, own controller, own privilege, own settings. | A second `.omod`, a duplicated token/HTTP layer, an inter-module GSP dependency. |
| **Languages** | **French only in v1.** Arabic deferred; English kept as a free by-product of the model choice, not a v1 commitment. | Auto language identification, a language selector, and a promise that would only have held for read-aloud MSA. |
| **STT model** | **`Qwen/Qwen3-ASR-0.6B`**, served by vLLM. Redeploy behind the same seam if it underperforms. | A model bake-off *before* anything works. Phase 4 still measures; phase 1 no longer blocks on it. |
| **LLM target** | ~~MedGemma 1.5 + FP8~~ → **MedGemma 1 + FP8**, promoted 2026-09-02. 1.5 failed phase 2's gate: it reads *"que peux-tu faire ?"* as `list_patients` and asks for clarification measurably less often. Deferred pending prompt re-tuning — `phase2/RESULTS.md`. | ~4.1 GB of VRAM, with no behavioural change at all. |
| **Interaction** | **Editable draft, Telegram-style.** Click to start, speak, click to stop; the text lands in the compose box and the clinician edits it before sending. Never auto-sends; voice never confirms. | The idea that ASR accuracy is a *safety* property. It is a quality property — §6.1. |
| **Environment** | **Development.** Scheduling is not a constraint; a container may be stopped whenever it is convenient. | Maintenance windows and "name a time" gating. **The quality gates are unchanged** — `eval_nlu.py` UNSAFE = 0 is a correctness bar, not a scheduling one. |

The first decision is the one that reshapes the design. Because the whole utterance is transcribed in
one request after the clinician lets go, **Server 2 holds no session state at all** — audio exists only
for the lifetime of one HTTP request. That is a materially stronger position on §1.3 ("Server 2 keeps
nothing") than the streaming design would have been, and it removes most of the moving parts.

---

## 1. What is actually on this machine

Read-only, 2026-09-01:

| | Measured | Source |
|---|---|---|
| GPU | RTX 5070 Ti, **16303 MiB total, 12926 used, 2894 free** | `nvidia-smi` |
| GPU occupant | `VLLM::EngineCore` pid 11411 holding **12500 MiB** | `nvidia-smi --query-compute-apps` |
| vLLM args | `--max-model-len 4096 --gpu-memory-utilization 0.80 --max-num-seqs 8` | `docker inspect vllm` |
| MedGemma weights | **8.1 GB** at `/home/cerist/models/medgemma-4b-it` (bf16) | `du -sh` |
| CPU | Core Ultra 7 265KF, 20 cores / 20 threads, AVX2 + AVX-VNNI, **no AVX-512** | `lscpu` |
| RAM | 32 GB, ~22 GB available | `free` |
| Disk | 716 GB free on `/` | `df` |
| **NPU** | Arrow Lake NPU `8086:ad1d`, `/dev/accel/accel0` present, `intel_vpu` loaded | `lspci`, `lsmod` |
| **NPU usage to date** | `npu_busy_time_us = 0` — it has never executed anything | sysfs |
| **NPU userspace** | **not installed** — no level-zero, no `intel-driver-compiler-npu`, no OpenVINO | `dpkg -l` |
| iGPU | **none.** 265K**F** has no graphics; `/dev/dri/renderD128` is the NVIDIA card | `/sys/class/drm/renderD128/device/uevent` |
| Outbound net | huggingface.co 200, github.com 200 — weights can be pulled here | `curl` |
| Containers | `vllm`, `clinical-agent`, `server2-proxy` on `server2_net`; only nginx publishes ports | `docker ps` |
| **Deployed LLM** | `medgemma-4b-it` — **MedGemma 1**, `Gemma3ForConditionalGeneration`, bf16. **Not MedGemma 1.5** | `config.json`, `transformers_version 4.54.0.dev0` |
| **Vision tower** | **already loaded and resident.** `mm_tokens_per_image: 256`; vLLM: *"Encoder cache will be initialized with a budget of 2048 tokens, profiled with 7 image items"* | `docker logs vllm` |
| **KV cache** | **2.94 GiB = 21,984 tokens** at `util 0.80` | `docker logs vllm` |
| **KV actually used** | **4.3–4.7 %** (~1,000 tokens) in production | `docker logs vllm` |

Two of these change the design before anything else does:

- **There are 2.9 GB of VRAM free today, not 16.** Any plan that assumes "16 GB, minus MedGemma" has
  to say how it gets the room back. §5 does.
- **The NPU has no software stack on this box at all.** Choosing it is not "flip a flag", it is
  installing and validating a second inference toolchain that has never run here. §4/Q3 weighs that
  against what it buys.

---

## 2. Architecture

### 2.1 The path the audio takes

```
Clinician's browser  (openmrs.hospital.lan — existing OpenMRS session, HTTPS)
  │  click the microphone button to start; click again to stop
  │  AudioWorklet → 16 kHz mono Int16 PCM, accumulated client-side
  │  level meter animates while recording  (client-side only, zero server cost)
  │  on release → ONE request
  │
  │  POST /openmrs/module/agentgateway/transcribe.form?lang=fr
  │       Content-Type: application/octet-stream, body = raw Int16LE PCM
  ▼
TranscribeRelayController  (Server 1, inside OpenMRS)  ── checks App: agentgateway.voice.use
  │                                                    ── mints RS256 token: sub, purpose=stt, exp=+5min
  │  POST https://stt.hospital.lan/v1/transcribe  + X-Stt-Channel-Key   (STT's OWN secret)
  ▼
nginx (Server 2)     ── TLS; allow 10.0.211.249/32 only; deny all      [same proxy, new vhost]
  ▼
stt-gateway          ── verifies channel secret, verifies token (aud=stt-service, purpose=stt)
  (FastAPI, CPU)     ── per-user quota, duration cap, PCM → 44-byte WAV header
  │                  ── STATELESS: audio lives only for this request
  │  POST http://stt-engine:8000/v1/audio/transcriptions   (OpenAI-compatible, stt_net only)
  ▼
stt-engine           ── vLLM serving the ASR model on the GPU. No auth, no published port.
  │                     Batches concurrent requests — this is where "many users at once" is solved.
  ▼
  transcript ────────────────────────────────► back up the same path, into the input box
                                               clinician presses send → existing chat.form flow
```

Nothing downstream of the input box changes. The clinical agent, the delegated-token chat path, the
audit filter, the confirmation gate, the operation log — all untouched. That is deliberate: the chat
pipeline is live and validated (69 module tests, 142 agent tests, `eval_nlu` UNSAFE = 0), and this work
must not re-open any of it.

### 2.2 Why the browser does not talk to Server 2 directly

The reflex design is a WebSocket from the browser straight to Server 2. With instant-on-release there
is nothing left to stream, so the question mostly answers itself — but for the record:

1. **ADR-12 exists and this would be the first hole in it.** Today exactly one address may reach
   Server 2 (`allow 10.0.211.249/32; deny all`), proved live (check D in the server2 README).
2. **One request per utterance is not a streaming workload.** A 15-second dictation is ~480 KB of raw
   PCM — about 4 ms on a gigabit LAN. The extra hop through Server 1 is sub-millisecond against a
   ~200 ms decode.
3. **No WebSocket support is needed inside OpenMRS**, which on Tomcat 7 + UI Framework would have been
   genuinely unpleasant.

### 2.3 Why raw PCM, and why octet-stream rather than base64 in a form field

`MediaRecorder` is the reflex choice and is wrong here: it produces WebM/Ogg containers, which puts
**ffmpeg and a codec stack into the service** — the largest single dependency in the design, with a
long CVE history, fed attacker-controlled bytes.

`AudioWorklet` → downsample to 16 kHz mono → Int16 is self-describing and needs no decoder. The
gateway's only transformation is prepending a **44-byte WAV header** — no library, no codec. That is
the biggest "minimal dependency" win available here.

Sending it as `application/octet-stream` rather than base64 in a form parameter saves 33 % of the
bytes and sidesteps Tomcat's `maxPostSize` and Spring's form-binding limits entirely; the controller
reads `request.getInputStream()`. At the 30-second cap that is 960 KB rather than 1.28 MB.

`AudioWorklet` and `getUserMedia` both require a secure context — OpenMRS is already HTTPS, so this is
satisfied. (Browser inventory is Q-E in §10.)

> **Confirmed by measurement, 2026-09-01 — and it is stricter than "efficient".** vLLM's
> `/v1/audio/transcriptions` **rejects** anything that is not mono 16 kHz, with
> `{"error":{"message":"Invalid or unsupported audio file.","code":400}}` — even though `librosa`
> inside the same container decodes the file happily. A 48 kHz stereo WAV 400s; the identical audio
> converted to mono 16 kHz transcribes perfectly.
>
> So downsampling in the browser is **mandatory**, not an optimisation. The good news is that §2.3
> already specified exactly that, so the architecture needs no change — but a browser implementation
> that skips the resample will fail with an error that points at the *file* rather than at the
> sample rate, and will cost an afternoon. Same trap caught `eval-dataset.py` in phase 1.

### 2.4 The engine seam — how "easy to change model" is made real

`stt-gateway` speaks exactly one thing to the engine: **OpenAI's `/v1/audio/transcriptions`**. That is
what vLLM, faster-whisper-server, whisper.cpp's server and essentially every other serving stack
already expose. Consequences:

- swapping model = change the image tag and `STT_MODEL` in one overlay file, restart one container;
- swapping runtime (GPU → CPU → NPU) = point `STT_ENGINE_URL` somewhere else;
- the gateway has no ML dependency at all — `fastapi`, `httpx`, `pyjwt`. That is the whole
  requirements file. It is a security component and it stays one.

A `Transcriber` protocol with a single implementation (`OpenAiCompatibleTranscriber`) is the seam,
mirroring how `app/nlu/base.py` is the seam the agent service swaps engines behind.

---

## 3. The four questions, answered

### Q1 — `agentgateway`, or a new module? → **extend `agentgateway`, 1.2.0**

The separation that matters is between the two Server-2 services, and that is total: separate
container, separate network, separate secret, separate process, separate model. The OpenMRS side goes
the other way:

- **The button has to live inside `chatWidget.gsp` and `chat.gsp`, which belong to `agentgateway`.**
  A second module cannot inject there without agentgateway including its fragment — an edit to
  agentgateway anyway, plus a hard inter-module dependency and a start-order problem. You would pay
  for two modules and get none of the isolation.
- **Everything the relay needs already exists there**: `RsaJwt`, `DelegatedTokenService`,
  `HttpJsonClient`, the privilege helper, the settings mechanism. A second module means a second copy —
  and this codebase has already had a production incident from duplicated libraries in a module
  classloader (the Jackson note in the README).
- **Module deployment on this box is expensive** (rsync to Server 1, Maven in a container, `docker cp`
  into `openmrs-app`, restart). Doubling that per change is a real recurring cost.
- `agentgateway`'s charter — "no clinical logic, no neurosurgery-specific logic, reusable by an
  unrelated department unchanged" — is **not** violated. A voice front-end to its own chat box is the
  same kind of thing as the chat box.

Footprint, small and additive:

```
api/  …/stt/SttConfig.java                         new global properties
omod/ …/web/controller/TranscribeRelayController.java   new: /module/agentgateway/transcribe.form
      …/webapp/resources/scripts/agent-voice.js    new: capture + one POST, ~120 lines
      …/webapp/fragments/chatWidget.gsp            +1 button, +1 script include
      …/webapp/pages/chat.gsp                      +1 button, +1 script include
      …/resources/config.xml                       +1 privilege, +3 global properties
```

`AgentAuditFilter`, the existing token purposes, the rollback engine and the operation log are **not
touched**.

### Q2 — Shared channel secret and keys? → **separate secret; shared public key; distinct purpose/audience**

| Credential | Shared? | Why |
|---|---|---|
| Channel secret | **NO — generate a second one** | The one that matters. The STT service's input is attacker-shapeable binary audio decoded by a model runtime — a materially larger surface than the agent's JSON. If it is compromised and holds the *agent's* secret, the attacker can call `/chat` and drive PHI reads and confirmed writes. Separate secrets mean a compromised STT service can transcribe audio and nothing else. `openssl rand -base64 48` → new GP `agentgateway.sttChannelSecret`, new `STT_CHANNEL_SECRET` on Server 2. |
| RSA **private** signing key | never leaves OpenMRS — unchanged | Already correct. STT changes nothing about it. |
| RSA **public** key | **yes, share it** | It is public. One key lifecycle instead of two. |
| Token `purpose` / `aud` | **NO — distinct** | `purpose: "stt"`, `aud: "stt-service"`. Exactly the mechanism the module already uses to keep chat, rollback and read tokens from being replayed into each other. A stolen STT token cannot open a chat turn; a stolen chat token cannot drive the GPU. One `if` in the existing verifier. |
| TLS certificate | **yes — one cert, two SANs** | `agent.hospital.lan` + `stt.hospital.lan`, one nginx, one host, same hospitalCA. Steps 3–5 of the server2 README, re-run listing both names. Split only if STT ever moves hosts. |
| Docker network | **NO — separate `stt_net`** | Today `clinical-agent` and `vllm` share `server2_net`. Putting STT there would let the clinical agent reach the ASR engine and the STT gateway reach MedGemma. A separate network makes "completely decoupled" true at the packet level. nginx is the only container on both. |

**Does the STT service still need a delegated token**, now that it is stateless and the channel secret
already proves the caller is OpenMRS? Yes — for one reason that survives the simplification:

- **Per-clinician rate limiting.** The server2 README already notes nginx's limit is keyed on Server 1's
  single address, so it limits *the hospital*, not a user. For chat that is a backstop; for STT, where
  one request is a GPU workload, per-user quota is the actual defence against one person or one stuck
  button starving everyone else. It needs a subject to key on.
- Secondarily, attribution if a dictation ever has to be investigated.

(Session ownership, which was the third reason in the streaming design, no longer applies — there are
no sessions.)

### Q3 — Which model, and NPU or GPU? → **`Qwen/Qwen3-ASR-0.6B`, on the GPU**

#### The three candidates in `candidate-models`

I read all three. None is deployable as the production model:

| Candidate | What it actually is | Verdict |
|---|---|---|
| [`StephaneBah/Whisper-AfroRad-FR`](https://huggingface.co/StephaneBah/Whisper-AfroRad-FR) | whisper-**small** (0.2B) + **LoRA**, trained on **4.5 hours** (562 files) of Afro-French radiology. Card reports **WER 20.93 %** on its own 75-file test set. Apache-2.0. | Not usable. ~21 % WER is roughly one word in five wrong, on the model's *own* test set. For utterances carrying patient names, identifiers and numbers that is a safety problem, not a quality one. |
| [`v4nn4/whisper-medical`](https://github.com/v4nn4/whisper-medical) | whisper-**tiny**, fine-tuned on **~1,200 synthetic samples (~45 min)** of urgent-care phrases, 100 steps on a Colab T4. Evaluation "to be completed"; **no released checkpoint**. | Not usable. It is a teaching example of the fine-tuning procedure — and an honest one, it says so itself. |
| [`leduckhai/MultiMed-ST`](https://github.com/leduckhai/MultiMed-ST) | EMNLP-2025 **dataset** (290k samples) + Whisper fine-tunes for medical speech *translation*. Vietnamese, English, German, French, Chinese. | Genuinely useful — as an **evaluation corpus** and a source of French medical audio, which is how §4 uses it. Not the production model. |

All three try to buy medical specialisation through a small fine-tune, and all three end up **worse**
than a strong general model, because they trade the general model's acoustic robustness for a
vocabulary gain available another way.

#### The other way: context biasing

Qwen3-ASR takes free-form text context to steer decoding — the card's phrasing is that you can "prompt
the model with texture context in any format to obtain customized ASR results". Whisper has the same
capability as `initial_prompt`. So the neurosurgery lexicon — *Glasgow, Karnofsky, hydrocéphalie,
craniotomie, méningiome, dérivation ventriculo-péritonéale*, the ward's drug list, and **the names of
patients currently in the department** — can be injected per request, at zero training cost,
changeable by editing a text file.

That last item is what actually matters. In this system the transcript's job is to name a patient, a
number and a verb. Biasing on the current inpatient list is worth more than fine-tuning on generic
medical French, it is free to keep current, and it disappears when the request ends.

#### Why Qwen3-ASR-0.6B

[`Qwen/Qwen3-ASR-0.6B`](https://huggingface.co/Qwen/Qwen3-ASR-0.6B) · [repo](https://github.com/QwenLM/Qwen3-ASR)

- **Apache-2.0**, no gating step (unlike MedGemma, which needed licence acceptance on HF). This
  machine has HF access.
- **vLLM support with an OpenAI-compatible `/v1/audio/transcriptions`.** The practical argument: vLLM
  is the one serving stack already proven on this Blackwell card, where the compose file's own comment
  records the sm_120 kernel trap that cost time last round. Batching, memory accounting and the
  CUDA-arch question are all already answered here.
- **Small** — 0.6B / ~900M params, fits the budget in §5 with margin.
- **French is in the supported set**, and so are English and Arabic — which keeps the deferred-Arabic
  door open at no cost, and keeps English working as it does in the typed chat today.
- The 1.7B sibling reports 1.63 / 3.38 % WER on LibriSpeech clean/other against Whisper-large-v3's
  1.51 / 3.97 %, and 3.35 % on Fleurs-en against 4.08 % — competitive with large-v3 at a fraction of
  the size.

**What I could not verify:** the card publishes **no per-language WER for French**, only English and
Chinese. So this is the strongest available prior, not a measurement on your task. §4 turns it into
one, and that step is not optional.

**Consequence of deferring Arabic, stated plainly:** the multilingual requirement was the main thing
ruling out French-only models. With Arabic out of v1, a French specialist such as
`bofenghuang/whisper-large-v3-french-distil-dec16` becomes a **first-class candidate**, not just a
diagnostic. I still recommend starting with Qwen3-ASR-0.6B — it keeps English and keeps Arabic
reachable later without a model migration — but if the bake-off shows the French specialist clearly
ahead on entity accuracy, that is a real decision to take on the numbers, not a foregone conclusion.

**Streaming is not used.** Qwen3-ASR's native streaming mode exists only through the in-process vLLM
Python API (`streaming_transcribe(seg, state)`, `chunk_size_sec=2.0`), not the HTTP server, and it
costs accuracy (average WER 2.69 % offline → 3.33 % streaming on the 1.7B). Instant-on-release needs
none of it: every decode is a full offline decode at full accuracy, over the stock OpenAI interface.

#### GPU, not NPU

| | GPU (RTX 5070 Ti) | NPU (Arrow Lake, ~13 TOPS) |
|---|---|---|
| Software present today | vLLM running, sm_120 trap already solved | **nothing** — no level-zero, no NPU compiler, no OpenVINO |
| Models available | Qwen3-ASR, Whisper any size, anything vLLM serves | OpenVINO `WhisperPipeline` documents **whisper-base / small / medium** on NPU. Not large-v3, not turbo, **not Qwen3-ASR** |
| Best achievable quality | current SOTA | whisper-**small** is the base model of the AfroRad candidate already rejected above; medium is better but still well behind |
| Concurrency | vLLM batches many requests on one device | one accelerator, effectively serial — fails "many users at once" hardest |
| Interface | OpenAI-compatible, drop-in | bespoke pipeline plus an offline model-conversion step per model |
| Container plumbing | already working | `--device /dev/accel/accel0`, `group_add: render`, NPU userspace in the image, firmware coupling |
| Power | ~300 W card, idle most of the time | 2–3 W — genuinely excellent, and irrelevant on a mains-powered desktop |

The NPU's real strength is battery-life-per-inference on a laptop. On a mains-powered tower with a
16 GB Blackwell card sitting at 1 % utilisation it buys nothing, and it costs the quality ceiling —
because the model tier it can serve is precisely the tier this project already rejected as unsafe.

**Not proposing we install the NPU stack.** The seam in §2.4 means an OpenVINO NPU engine can be added
later as a third backend and measured against the others without touching the gateway. That is the
right time to spend the effort — once §4's corpus exists to measure it on.

**Fallback:**

- *Engine unreachable or GPU busy* → the gateway returns a clean "dictée indisponible", the mic button
  greys out, and the composer stays a normal text box. The clinician types. This mirrors the agent's
  existing MedGemma→rules philosophy: a dead GPU narrows what the assistant offers, it never takes the
  chat offline.
- *Optional CPU engine* → a second container running whisper.cpp or a CPU-mode server behind the same
  OpenAI interface, on 20 Arrow Lake cores. Seconds rather than sub-second, but real. **Not in phase
  1**: build the graceful degradation first, measure whether the GPU is ever actually unavailable, add
  the CPU engine only if the answer is yes. One container and one env var whenever you want it.

### Q4 — Docker configuration inside `server2-stack`

Follows the "adding another service" contract in the server2 README: **one overlay, one vhost
template.**

```
server2-stack/
  docker-compose.stt.yml                  new
  nginx/templates-stt/stt.conf.template   new
  nginx/nginx.conf                        +1 line (limit_req_zone — see below)
  .env                                    new keys appended
  certs/agent.crt                         re-signed with a second SAN (stt.hospital.lan)
```

```bash
docker compose -f docker-compose.yml -f docker-compose.vllm.yml -f docker-compose.stt.yml up -d
```

#### `docker-compose.stt.yml` (proposed)

```yaml
# Speech-to-text: a security gateway and an ASR engine, on their own network.
#
# stt_net is not server2_net on purpose. The clinical agent must not be able to reach the ASR
# engine, and the STT gateway must not be able to reach MedGemma. "Decoupled" should be true at
# the packet level, not only on the diagram. nginx is the only container on both.

services:
  nginx:
    environment:
      # The base file filters to ^(AGENT_|OPENMRS_). Extended here rather than edited there, so the
      # base stack stays untouched. Without STT_ in the filter, ${STT_SERVER_NAME} renders empty and
      # the vhost silently answers for nothing.
      NGINX_ENVSUBST_FILTER: "^(AGENT_|OPENMRS_|STT_)"
      STT_SERVER_NAME: ${STT_SERVER_NAME:?set STT_SERVER_NAME, e.g. stt.hospital.lan}
      STT_PROXY_READ_TIMEOUT: ${STT_PROXY_READ_TIMEOUT:-30s}
    volumes:
      - ./nginx/templates-stt/stt.conf.template:/etc/nginx/templates/stt.conf.template:ro
    networks:
      - server2_net
      - stt_net
    depends_on:
      stt-gateway:
        condition: service_healthy

  stt-gateway:
    build:
      context: ../stt-service          # layout is Q-C in §10
    image: chu-blida/stt-gateway:0.1.0
    container_name: stt-gateway
    restart: unless-stopped
    env_file: .env
    environment:
      STT_ENGINE_URL: http://stt-engine:8000/v1
      STT_MODEL: ${STT_MODEL:-Qwen3-ASR-0.6B}
    expose: ["8000"]                   # never "ports:"
    networks: [stt_net]
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    logging: { driver: json-file, options: { max-size: "10m", max-file: "5" } }

  stt-engine:
    # Pinned, never :latest — same rule as the MedGemma overlay. A newer tag than v0.11.0 is
    # REQUIRED: Qwen3-ASR postdates that release. Exact tag is fixed in the phase-1 spike.
    image: vllm/vllm-openai:${STT_VLLM_TAG:?pin the vLLM tag verified for Qwen3-ASR}
    container_name: stt-engine
    restart: unless-stopped
    command:
      - --model
      - ${STT_MODEL_PATH:?e.g. /models/Qwen3-ASR-0.6B}
      - --served-model-name
      - ${STT_MODEL:-Qwen3-ASR-0.6B}
      - --gpu-memory-utilization
      - ${STT_GPU_FRACTION:?fraction of TOTAL VRAM, not of free — see the budget}
      - --max-num-seqs
      - "16"
    volumes:
      - ${LLM_MODEL_DIR:-/home/cerist/models}:/models:ro    # same host dir the MedGemma weights use
    expose: ["8000"]                   # no auth on this endpoint — never publish it
    networks: [stt_net]
    deploy:
      resources:
        reservations:
          devices: [{ driver: nvidia, count: 1, capabilities: [gpu] }]
    healthcheck:
      test: ["CMD-SHELL", "python3 -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\""]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 600s
    logging: { driver: json-file, options: { max-size: "10m", max-file: "5" } }

networks:
  stt_net:
    name: stt_net
```

#### `nginx/templates-stt/stt.conf.template` (proposed)

Started from `templates/agent.conf.template` — same posture: server-to-server, IP-allowlisted, TLS,
`return 404` outside the endpoints it means to expose.

```nginx
server {
    listen 443 ssl;
    server_name ${STT_SERVER_NAME};

    include /etc/nginx/snippets/tls.conf;
    include /etc/nginx/snippets/security-headers.conf;
    access_log /var/log/nginx/stt.access.log server2;

    allow ${OPENMRS_SERVER_CIDR};
    deny all;

    # One whole utterance of raw 16 kHz mono Int16 PCM. The 30 s cap is 960 KB; 2m leaves margin
    # without letting an unbounded body through. The gateway enforces the duration cap itself —
    # this is the outer backstop, not the rule.
    client_max_body_size 2m;

    location = /v1/transcribe {
        limit_req zone=stt_transcribe burst=10 nodelay;
        proxy_pass http://stt-gateway:8000/v1/transcribe;
        include /etc/nginx/snippets/proxy.conf;
        proxy_read_timeout ${STT_PROXY_READ_TIMEOUT};
        proxy_request_buffering on;    # bounded body; buffering keeps a slow client off the GPU
    }

    location = /health {
        access_log off;
        proxy_pass http://stt-gateway:8000/health;
        include /etc/nginx/snippets/proxy.conf;
    }

    location / { return 404; }
}

server {
    listen 80;
    server_name ${STT_SERVER_NAME};
    return 444;    # plain HTTP here is a misconfiguration, not something to redirect
}
```

**The one place this touches a file the running proxy already uses:** `limit_req_zone` must be
declared in `http{}`, so `nginx/nginx.conf` needs **one added line**:

```nginx
limit_req_zone $binary_remote_addr zone=stt_transcribe:10m rate=120r/m;
```

120 r/m ≈ two dictations per second aggregate. Every request arrives from Server 1's single address,
so — as the README already notes for `/chat` — this limits the hospital, not a user. Real per-user
limiting happens in `stt-gateway`, keyed on the token subject. The edit is additive (a new zone name)
and `nginx -t` is already the container's healthcheck.

#### New `.env` keys

```ini
STT_SERVER_NAME=stt.hospital.lan
STT_PROXY_READ_TIMEOUT=30s
STT_CHANNEL_SECRET=          # openssl rand -base64 48 — a DIFFERENT value from AGENT_CHANNEL_SECRET
STT_JWT_AUDIENCE=stt-service
STT_MODEL=Qwen3-ASR-0.6B
STT_MODEL_PATH=/models/Qwen3-ASR-0.6B
STT_VLLM_TAG=                # pinned in the phase-1 spike
STT_GPU_FRACTION=            # set from the phase-2 measurement
STT_LANGUAGE=fr              # pinned, not auto-detected — see §6.3
STT_MAX_UTTERANCE_SECONDS=30
STT_MAX_CONCURRENT_PER_USER=1
STT_BIAS_LEXICON_PATH=/etc/stt/lexicon-neurochir.txt
LOG_TRANSCRIPTS=false        # mirrors LOG_PROMPTS, and for the same reason
# OPENMRS_JWT_PUBLIC_KEY is reused as-is — it is public.
```

---

## 4. Choosing the model by measurement, not by leaderboard

No public benchmark answers "which ASR is best on Algerian neurosurgeons dictating French clinical
requests". The choice above is a starting bet; phase 4 confirms or overturns it.

**The metric is not WER, and — since §6.1 — it is not "was it perfect" either.** The clinician edits
the draft before sending, so the question is not *"is every word right"* but *"how much work did this
save, and how often does it produce an error the clinician will not notice"*. Measure:

| Metric | Why |
|---|---|
| **Edit rate** — characters changed between transcript and what was sent | The honest measure of value. A transcript needing two fixed digits out of a 60-character sentence is still a large win over typing it |
| **Plausible-error rate** — wrong digits, wrong name spellings, wrong dates that still *read* correctly | §6.1: the one class editing does not catch. This is the number that should drive the model choice |
| **Obvious-error rate** — garbled words, wrong language, invented text | Annoying, self-evident, cheap to fix. Track it, do not weight it heavily |
| **End-to-end UNSAFE count**, audio → transcript → `eval_nlu` | The bar is UNSAFE = 0. Voice must not move it — but note this is now a *third* layer, not the only one |
| Substitution rate (not raw WER) | See the MultiMed-ST caveat below — raw WER is dominated by dataset artefacts |
| Time from button release to text in the box | The felt experience. Measured at ~0.14 s for 3 s of audio |

**Edit rate is measurable for free once phase 5 is live, with no PHI exposure**: log the scalar
Levenshtein distance between the transcript and the text actually sent — **the number only, never the
strings**. That gives a continuous quality signal that satisfies §6.4 and costs nothing.

**Corpus.** 50–100 utterances, read by 3–5 actual clinicians from the department, in the room they
work in. `tests/eval_nlu.py`'s existing corpus is the obvious script to read aloud — that makes the
voice path directly comparable to the typed path on the same sentences. This corpus is the most
valuable artefact this project can produce and it outlives every model decision. It is also the one
thing I cannot produce for you (Q-D in §10).

**Bake-off entrants**, all behind the same OpenAI interface, all with the same lexicon biasing:

1. `Qwen3-ASR-0.6B` — the proposed default
2. `Qwen3-ASR-1.7B` — the accuracy ceiling; keep if §5's budget allows and the gain is real
3. `whisper-large-v3-turbo` — the well-understood multilingual baseline, and the check that the Qwen
   numbers are not an artefact
4. `bofenghuang/whisper-large-v3-french-distil-dec16` — now a **serious contender**, not a diagnostic,
   since Arabic is deferred. If it wins clearly on entity accuracy, the trade is French quality now
   against a model migration when Arabic returns

`MultiMed-ST`'s French medical audio goes in as extra evaluation material, not as a model — **with a
caveat found by using it** (2026-09-01, `phase1/results-multimed.txt`):

> Its French `corrected.test` references are **misaligned with the audio**. The clips are overlapping
> sliding windows and the reference covers only part of what is spoken, so a correct transcript scores
> as insertion errors. One sample's reference implies a speaking rate of **51 words/minute** — against
> ~150–200 for conversational French — while the model's transcript of the same clip implies 226.
> Consecutive rows also overlap: row 7's transcript ends where row 8's reference begins.
>
> Consequence: **raw WER against this dataset is not a usable score.** Report the
> substitution/deletion/insertion split instead — substitutions are the model's real errors, and
> edge insertions are the dataset's. Roughly a quarter of the split is clinical at all; the rest is
> YouTube channel intros and sign-offs.

**Considered and ruled out: [`google/medasr`](https://developers.google.com/health-ai-developer-foundations/medasr/model-card).**
Released alongside MedGemma 1.5, 105M-parameter Conformer, trained on ~5,000 hours of physician
dictation, and it posts the best medical-dictation numbers of anything here — 5.2 % WER against
Whisper large-v3's 12.5 % on chest X-ray dictation, and 5.2 % against 28.2 % on an internal dictation
benchmark. It is exactly this problem domain. But the model card is unambiguous: **"English-only: All
training data is in English"**, and primarily US native speakers. For a French-first deployment it is
not a candidate. Recorded here so it does not get re-proposed on the strength of those headline
numbers.

---

## 5. Where the VRAM comes from — measured, and my first estimate was wrong

Today: **12926 of 16303 MiB used, 2894 free.** Room has to be made deliberately.

**Mechanism note: vLLM's `--gpu-memory-utilization` is a fraction of TOTAL VRAM, not of free VRAM.**
Two vLLM containers on one card means the fractions plus everything else must stay under 1.0, and
getting it wrong shows up as an allocation failure at model load, not a warning.

### 5.1 What the running engine actually reports

`docker logs vllm` gives the numbers directly, so this no longer needs estimating:

```
Available KV cache memory: 2.94 GiB
GPU KV cache size: 21,984 tokens
GPU KV cache usage: 4.3% … 4.7%          ← in production
```

The process holds **12500 MiB = 12.21 GiB**. Subtracting the 2.94 GiB KV cache leaves
**9.27 GiB of non-KV overhead** — weights (8.1 GiB), the vision tower, CUDA graphs, activations and
the 2048-token encoder cache. That figure is a **floor**: it does not shrink when you lower the
utilisation, only the KV cache does.

**This kills the 0.60 figure in my earlier draft.** 0.60 × 16303 = 9782 MiB = 9.55 GiB, which is
barely above the 9.27 GiB floor — leaving ~0.28 GiB of KV, about **2,150 tokens**. The system prompt
alone is 2,138 tokens. It would have served roughly one chat turn at a time, or failed to start.
Measuring first is the whole reason phase 2 exists; this is what it caught.

| `--gpu-memory-utilization` | Budget | KV left | ≈ tokens | Verdict |
|---|---|---|---|---|
| 0.80 (today) | 12.21 GiB | 2.94 GiB | 21,984 | ~20× more than production uses |
| 0.68 | 10.83 GiB | 1.55 GiB | ~11,600 | comfortable — ~5 concurrent turns |
| 0.66 | 10.51 GiB | 1.24 GiB | ~9,270 | workable — ~4 concurrent turns |
| **0.60** | 9.55 GiB | **0.28 GiB** | **~2,150** | **breaks. Do not use** |

So the honest bf16 floor is **~0.66**, not 0.60.

### 5.2 The target: MedGemma 1.5 + FP8

At bf16, 0.68 for MedGemma leaves 0.30 for everything else — enough for the STT engine at 0.22 with
~1.6 GB of headroom, and **nothing at all** for the image-classification workload. FP8 changes that.

The target model is **MedGemma 1.5 4B**, not MedGemma 1. It is the same Gemma 3 family (decoder-only,
GQA, `transformers ≥ 4.50.0`, same chat template, same tokenizer), so the plumbing risk is small and
the arithmetic below carries over unchanged — but it must be **re-measured**, not assumed, because the
non-KV floor is read off the running engine, not calculated. Going straight there skips one
quantisation exercise and one reload; §7 phase 2 is the evaluation that earns the right to do so.

Split the quantisation: **language model in FP8, vision tower and projector left in bf16.**

| | bf16 | FP8 (LM only) |
|---|---|---|
| Language model (3.88B) | 7.76 GB | **3.88 GB** |
| SigLIP vision tower + projector (~0.42B) | 0.84 GB | 0.84 GB — *unquantised, deliberately* |
| Total weights | ~8.6 GB | **~4.7 GB** |
| Non-KV floor | 9.27 GiB | **~5.56 GiB** |

The vision tower is excluded because SigLIP encoders are markedly more precision-sensitive than the
decoder, and preserving image accuracy is an explicit requirement. `llm-compressor` supports this
directly via `ignore=["vision_tower.*", "multi_modal_projector.*", "lm_head"]`.

FP8 rather than INT4/AWQ because Blackwell has **native FP8 tensor cores** — it is faster as well as
smaller — and W8A8 FP8 is typically near-lossless, whereas 4-bit measurably degrades vision-language
tasks. That is precisely the trade being ruled out.

**Revised budget:**

| Consumer | Fraction | ≈ MiB | Note |
|---|---|---|---|
| MedGemma, FP8 | **0.50** | ~8150 | KV ≈ 2.40 GiB ≈ 17,900 tokens — *better* than today's effective capacity |
| `stt-engine` (Qwen3-ASR-0.6B) | **0.25** | ~4080 | comfortable with batching |
| Desktop / X / misc | — | ~300 | measured ~145 MiB, but it moves |
| **Headroom** | — | **~3800** | the room image classification will need |

### 5.3 The fallback, if 1.5 + FP8 does not pass its gate

If phase 2's evaluation shows MedGemma 1.5 + FP8 failing `eval_nlu.py`, fall back in this order:

1. **MedGemma 1 + FP8** — keeps the model whose prompt calibration is already proven, still frees
   ~3.7 GB. Imaging then waits for a separate 1.5 migration.
2. **MedGemma 1 bf16 at `util 0.68`** — no numerics change at all, no re-validation beyond a smoke
   test. Ships STT, leaves no room for imaging.

**Not recommended: replacing MedGemma with a smaller interpreter.** It re-opens a component validated
at UNSAFE = 0 across several prompt revisions to solve a problem FP8 already solves.

Because this is a development environment, any of these can be tried whenever convenient — stopping
`vllm` degrades the chat to the `rules` engine, which is a tested path, not an outage.

---

## 6. Details that will otherwise be got wrong

### 6.1 The transcript is a draft the clinician edits — confirmed, and it changes the risk model

**Settled 2026-09-02.** The interaction is the one everybody already knows from Telegram or
Messenger: hold the button, speak, release, and the words appear **in the compose box as editable
text**. The clinician reads them, fixes anything wrong, and presses send. Dictation replaces typing;
it does not replace judgement.

Two rules follow, and they are now decisions rather than proposals:

- **The transcript never auto-sends.** It is inserted into `#agentInput` and nothing else happens.
- **Confirmation stays on the buttons.** `Confirmer` / `Annuler` are already buttons in both GSPs, and
  `agent-voice.js` has no path to `agentConfirm()`. Voice cannot produce the *oui* that authorises a
  write (CA5, ADR-2).

**What this does to the risk model — including a correction.** An earlier draft of this plan called a
descriptive sentence transcribed as an imperative "the most serious failure possible". That
overstated it. With an editable draft there are now three independent layers between a
mis-transcription and a write:

1. the clinician reads the text before sending it;
2. the interpreter refuses to turn descriptive phrasing into a write (`eval_nlu` UNSAFE = 0);
3. the confirmation gate summarises every write in plain French and waits for an explicit *oui*.

So ASR accuracy is **a quality and usability property, not a safety property**. The safety properties
were already built and tested; voice input does not weaken them. That reframing matters because it is
what stops us over-engineering the model choice — see the recalibrated criteria in §4.

**The one failure mode editing does *not* catch** is a *plausible* error: `0666777889` for
`0666777888`, or `Benali` for `Benhali`. A clinician skims text they just dictated and sees what they
expected to say. Obvious errors are free to catch; plausible ones are not. This is why §4 measures
entity accuracy specifically rather than trusting overall WER, and it is the one place where model
quality still carries real weight.

**Interaction details this settles:**

| | Behaviour |
|---|---|
| Existing text in the box | **append**, do not replace. Dictate, type, dictate again all compose |
| Cursor | left at the end of the inserted text, so typing continues naturally |
| Repeat dictation | allowed, any number of times, into the same box |
| Cancel while recording | `Escape` discards — nothing is sent, nothing is inserted |
| Undo a bad transcript | one click clears **only what the last dictation inserted**, not the whole box |
| Start/stop | **click to start, click again to stop** — decided 2026-09-02. Not hold-to-record: holding a mouse button for twenty seconds while reading a chart is awkward, which is the problem Telegram solves with slide-to-lock. A visible recording indicator and elapsed timer matter more with a toggle, since there is no button-held cue |

### 6.2 Latency budget, end to end

For a 15-second utterance, from button release to text in the box:

| Step | Estimate |
|---|---|
| Upload 480 KB over the LAN, browser → Server 1 | ~5 ms |
| OpenMRS relay: privilege check, token mint (RSA sign), forward | ~10–20 ms |
| Server 1 → Server 2, TLS, 480 KB | ~5 ms |
| Gateway: verify secret + token, WAV header, forward | ~5 ms |
| **Decode, Qwen3-ASR-0.6B, 15 s audio** | **~150 ms** at a pessimistic 100× realtime |
| Return path | ~10 ms |
| **Total** | **≈ 200 ms, call it 0.3–0.5 s with queueing** |

The decode dominates and everything else is noise — which is the argument in §2.2 restated as
arithmetic. Concurrency is bounded by vLLM's batching (`--max-num-seqs 16`) plus
`STT_MAX_CONCURRENT_PER_USER=1` and the 30-second duration cap, so a stuck button costs one decode,
not a runaway.

### 6.3 Language is pinned to French, not auto-detected

Automatic language identification on a three-second utterance is unreliable, and a wrong guess produces
confident nonsense. Pinning improves accuracy measurably. `STT_LANGUAGE=fr` in v1. The `lang` query
parameter exists in the API from day one so that adding a selector later is configuration, not a
redesign — but there is no selector in v1 and no LID.

### 6.4 PHI on Server 2

Server 2 keeps nothing (§1.3), and instant-on-release makes that easy to hold:

- audio exists **only for the lifetime of one HTTP request** — no session store, no disk, no TTL to
  get wrong;
- **audio is never logged**, at any level — there is deliberately no audio equivalent of `LOG_PROMPTS`;
- transcripts logged only under `LOG_TRANSCRIPTS`, defaulting false, mirroring the existing convention
  and its reasoning;
- the reviewable record stays where it already is — `agentgateway_operation_log` on Server 1, which
  captures the chat turn the transcript became.

### 6.5 Three workloads, one GPU — the contention nobody has hit yet

Today the card runs one workload. The plan adds a second (STT), and the imaging ambition adds a third.
They have very different shapes:

| Workload | Shape | Latency sensitivity |
|---|---|---|
| Chat NLU | ~2,200-token prefill, ~50 output tokens | **high** — `agentgateway.agentTimeoutMillis` is 30 s, the proxy cuts at 60 s |
| STT | one audio encode, short decode, ~150 ms | medium — the clinician is waiting, but on its own engine |
| Image classification | heavy vision-tower forward, 256 tokens/image, possibly batched | **low** — nobody watches a classification run |

STT is insulated: separate container, separate vLLM instance, separate network. **Image
classification is not** — it would share the *same* engine as the chat NLU, because that is the same
model. With `--max-num-seqs 8`, a batch of images occupies the scheduler and every chat turn queues
behind it. The failure mode is a clinician's chat timing out at 30 s because someone else started a
classification job.

Cheapest mitigation, and the one I would take: **cap imaging concurrency at 1 in the application
layer** and let chat turns interleave. A second vLLM instance for imaging would isolate it properly
but there is not enough VRAM for a third pool. Worth measuring before assuming it is a problem — but
worth knowing it is the shape of the problem when it appears.

### 6.6 Silence must never reach the model — measured, not theoretical

**Added 2026-09-01 after testing it directly on this deployment.** Fed near-silence with
`language=fr` pinned, Qwen3-ASR-0.6B returns confident, fluent French that nobody said:

| Input (3 s, 16 kHz mono) | `language=fr` | auto-detect |
|---|---|---|
| digital silence | `Ah.` | `Ahora.` |
| very quiet room (±8 LSB dither) | `Je suis un peu en colère.` | `嗯。` |
| room / fan noise (±200 LSB) | `Il est possible de faire un travail de révision.` | *(empty)* |
| 10 s digital silence | `Ah, c'est ça.` | `Ahora sí.` |

Two things follow, and both were surprises:

1. **This model is not immune.** §4 records a secondary source claiming Qwen3-ASR does not have the
   silence-hallucination problem that Whisper has. On this deployment, at this size, **that is
   false.** Treat it as a property of the workload, not of the model family.
2. **Pinning the language makes it worse.** §6.3 pins `fr` for accuracy — and pinning is exactly what
   removes the model's option to return nothing. Every single pinned case fabricated text; the only
   empty result came from auto-detect. The two settings pull against each other, and accuracy on real
   speech is the more important of the two, so the pin stays and the guard below is what pays for it.

**The guard, in two places:**

- **Browser** — compute RMS over the captured buffer before sending. Below threshold, or shorter than
  ~300 ms, do not send at all: clear the button and say nothing. Saves the round trip.
- **`stt-gateway` — authoritative.** Same check server-side, on the decoded PCM. Below threshold,
  return an empty transcript **without calling the engine**. The browser check is a convenience; this
  one is the rule.

Cheap to build — RMS over Int16 samples is a few lines and needs no library, which keeps §2.3's
no-dependency property intact. If a plain energy threshold proves too blunt in a noisy ward, Silero
VAD is the next step, but it is a dependency and should not be reached for first.

**Phase 4 must measure this explicitly.** Add silent and near-silent clips to the corpus as their own
test cases: *"button pressed, nothing said"* is a thing clinicians will do by accident, and the
correct behaviour is an empty box, not a sentence.

**Severity, stated accurately.** Because the transcript is an editable draft (§6.1), a fabricated
sentence appears in the compose box where the clinician plainly sees it and deletes it. It is
confusing and unprofessional, not dangerous — a UX defect. The RMS guard is worth building because it
is a few lines and it removes a baffling behaviour ("I said nothing and it wrote something"), not
because it closes a safety hole. There was no safety hole.

### 6.7 Things that will waste a day if nobody says them

- **`NGINX_ENVSUBST_FILTER` must be extended to include `STT_`**, or `${STT_SERVER_NAME}` renders empty
  and the vhost answers for nothing while looking fine. The server2 README already warns about this and
  it is still the easiest mistake here.
- **Use the port after the arrow.** `stt-gateway` is `8000` on `stt_net`. Same trap as
  `ohif-viewer` / `orthanc-cors-proxy` in the root `CLAUDE.md`.
- **`stt.hospital.lan` must resolve from Server 1 *and* from inside `openmrs-app`** — `/etc/hosts` plus
  `extra_hosts:`, exactly as `agent.hospital.lan` needed. Verify with
  `docker exec openmrs-app getent hosts stt.hospital.lan`. Server 2 itself cannot resolve
  `*.hospital.lan`; test with `curl --resolve`, per the root `CLAUDE.md`.
- **Re-signing the certificate with a second SAN replaces `agent.crt`**, which the running agent vhost
  also serves. Back it up timestamped into `backup files/` first, and reload nginx rather than
  recreating it.
- **`faster-whisper` / CTranslate2 int8 is broken on sm_120.** RTX 50-series hits
  `CUBLAS_STATUS_NOT_SUPPORTED` with int8; float16 works. Same class of trap as the vLLM sm_120 note
  already in the compose file — and a reason not to reach for the reflexive `faster-whisper` answer on
  this specific card.
- **Test in a private window.** Microphone permission is per-origin and sticky; a cached grant makes a
  broken permission flow look like it works — exactly as the root `CLAUDE.md` says about cached
  credentials and the viewer.
- **`getUserMedia` needs a secure context.** OpenMRS is HTTPS, so this holds — but anyone testing
  against a plain-HTTP dev instance will get a silent `undefined` on `navigator.mediaDevices` and no
  useful error.

---

## 7. Delivery, one reviewable step at a time

Each phase has a pass/fail check. Nothing in a later phase starts before the previous one passes.
Phases 1, 3 and 4 change **nothing** that is currently serving clinicians.

| # | What | Touches production? | Verified by |
|---|---|---|---|
| **1** | **Spike, off to one side.** Pull Qwen3-ASR-0.6B; find the vLLM tag that serves it; run it in a throwaway container; transcribe 10 French clinical sentences you read aloud. **Full step-by-step: [`PHASE-1-SPIKE.md`](PHASE-1-SPIKE.md).** | no — throwaway container, nothing rewired | a correct French transcript, and a **pinned** `STT_VLLM_TAG` |
| **2** | **Evaluation window, then make the room.** Test three arms back to back on the free card (§7.1), promote the winner, set `util` and `STT_GPU_FRACTION`. | yes — recreates `vllm`. Dev environment, so no scheduling constraint | **`tests/eval_nlu.py` UNSAFE still 0** and `tests/explore.py`'s 44 scenarios unchanged — the gate, not a formality. Plus `nvidia-smi` showing §5.2's headroom |
| **3** | **`stt-service` + compose overlay + vhost**, no OpenMRS side yet. New cert SAN. | no — additive containers, new vhost | checks A–F from the server2 README re-run for `stt.hospital.lan`: health OK, Server 1 can reach it, **nobody else can (403)**, bare IP gives nothing |
| **4** | **Bake-off** on the recorded corpus (§4). Confirm or overturn the model choice. | no | a table: entity accuracy, task-verb accuracy, UNSAFE count, latency, per model |
| **5** | **Module 1.2.0** — privilege, settings, `TranscribeRelayController`, `agent-voice.js`, the button in both GSPs. Built and tested off-line first. | **yes** — module deploy on Server 1 | module tests green including a `ModuleWiringTest` case for the new privilege; the chat still works with the mic untouched |
| **6** | **End-to-end**, private window, a clinician, a real utterance | yes | a spoken French sentence becomes the right chat turn; `agentgateway_operation_log` shows the resulting operation attributed correctly |
| **7** | **Per-user quota + load test**, 5–10 concurrent dictations | no | latency inside target; no VRAM growth; MedGemma unaffected |

**Rollback at every stage.** Phases 1, 3, 4 roll back by stopping a container. Phase 2 rolls back by
pointing `--model` back at `/models/medgemma-4b-it` and restoring `--gpu-memory-utilization 0.80` —
**keep the MedGemma 1 weights on disk until phase 2 has passed its gate.** Phase 5 rolls back by
restoring the previous `.omod`, backed up *outside* the modules directory, per HANDOFF. The mic button
is additionally gated on `App: agentgateway.voice.use`, so revoking one privilege makes it disappear
hospital-wide without a redeploy.

### 7.1 Phase 2 in detail — the evaluation window

Stop `vllm` outright. The chat falls back to the deterministic `rules` engine, which is a tested,
documented degradation. That frees the whole card, so candidates can be measured at full size without
contorting the budget. Run these back to back:

| Arm | What it answers |
|---|---|
| **MedGemma 1.5 + FP8** | the target. If it passes, take it and skip everything below |
| MedGemma 1 + FP8 | the low-risk baseline — does FP8 alone hold UNSAFE = 0? |
| MedGemma 1.5 bf16 | only if arm 1 fails: separates "1.5 broke it" from "FP8 broke it" |

Gate on every arm, no exceptions: **`tests/eval_nlu.py` UNSAFE = 0** and `tests/explore.py`'s 44
scenarios unchanged. HANDOFF has the exact invocations. Record the per-arm numbers in
`IMPLEMENTATION-LOG.md` whichever way it goes — a failed arm is evidence worth keeping.

Two practical notes:

- **MedGemma 1.5 is gated on Hugging Face** under the Health AI Developer Foundations terms — the same
  acceptance step MedGemma 1 needed. Do it before the window, not during it.
- **Check what vLLM's on-the-fly `--quantization fp8` actually quantises.** It is one flag, but it
  gives little control over *which* modules it touches. If it quantises the SigLIP tower, image
  accuracy is the thing being traded away. Verify first; fall back to an offline `llm-compressor`
  checkpoint with `ignore=["vision_tower.*", "multi_modal_projector.*", "lm_head"]` if it does.

---

## 8. What this design deliberately does not do

- **No streaming, no WebSocket, no partial decodes.** §0, §2.2.
- **No audio codec on the server.** §2.3. No ffmpeg, no libopus, no container parsing.
- **No fine-tuning in v1.** Context biasing first; revisit only if §4 shows biasing is not enough, and
  then with a real corpus rather than 4.5 hours.
- **No NPU stack installed.** §3/Q3. The seam keeps it addable.
- **No second OpenMRS module.** §3/Q1.
- **No voice-driven confirmation, and no auto-send.** §6.1.
- **No language detection.** §6.3.
- **No changes to the clinical agent service at all.** It keeps receiving text on `/chat` and never
  learns that voice exists.

---

## 9. Cost of being wrong

| If this turns out wrong | Cost to change |
|---|---|
| Qwen3-ASR is worse than Whisper on our French | one env var + one image tag. The seam is the point |
| The French specialist wins the bake-off | same — one env var, at the cost of English and a future Arabic migration |
| 0.3–0.5 s still feels slow | partials become worth building; the gateway grows a session buffer, nothing else moves |
| The NPU would have been better | add a third engine container; gateway untouched |
| Arabic comes back into scope | Qwen3-ASR already covers `ar` — a config change, unless the answer is Derja, which is a project |
| Extending `agentgateway` was the wrong call | the new code is in its own package and controller; extracting it into a `voicegateway` module is a move, not a rewrite |

---

## 10. Still open — needed soon, none of it blocking the design

- **Q-C · ~~Where does the code live?~~** **Answered 2026-09-02: `~/stt-service/`**, a sibling of
  `chatbot-neuro/` and `server2-stack/`. Documentation stays in `~/STT/`. Still open: does any of it
  go into git, given that `origin/main` holds only a README?
- **Q-D · ~~Record the corpus?~~** **Deferred 2026-09-02** — not now. Consequence, stated plainly:
  **phase 4 cannot run**, so the model choice stays an argument rather than a measurement, and
  Algerian-accented French remains untested. Qwen3-ASR-0.6B ships on the strength of the public-audio
  smoke test in phase 1 (7.7 % substitution rate on conversational French medical speech). The seam
  makes swapping it cheap if it disappoints in real use — but "in real use" is now the first place we
  will find out.
- **Q-E · ~~Which browsers?~~** **Answered: Chrome, Firefox, Edge.** All three have supported
  `AudioWorklet` and `getUserMedia` for years, and OpenMRS is already HTTPS, so the secure-context
  requirement is met. `agent-voice.js` still feature-detects and hides the button rather than throwing,
  because one locked-down profile on one ward is cheaper to degrade than to debug.
- **Q-F · ~~The two rules in §6.1~~** — **answered 2026-09-02.** Editable draft, Telegram-style:
  transcript never auto-sends, confirmation never by voice. Folded into §0 and §6.1.
- **Q-H · ~~Hold-to-record or toggle?~~** — **answered 2026-09-02: click to start, click to stop.**
  Folded into §0 and §6.1. Consequence: the recording indicator and elapsed-time display carry more
  weight, because there is no held button to tell the clinician they are still recording.
- **Q-G · ~~The imaging use case?~~** **Answered 2026-09-02: an independent service that classifies
  medical images for diagnosis and studies — and it is the end goal of the whole project.** Not now;
  the field is being prepared. What that changes today, and what it does not, is §12.

**No longer blocking:** phase 2 recreates the `vllm` container. In a development environment that is
free to do whenever convenient — the chat degrades to the `rules` engine, which is tested. The
*quality* gate (§7.1) still stands: nothing is promoted without UNSAFE = 0.

---

## 11. Appendix — MedGemma: what is deployed, and what quantising costs

Added 2026-09-01 in response to the question about redeploying a quantised multimodal MedGemma.

### Two corrections to the premise

**The deployed model is MedGemma 1, not 1.5.** `/home/cerist/models/medgemma-4b-it/config.json` gives
`architectures: ["Gemma3ForConditionalGeneration"]`, `transformers_version: 4.54.0.dev0`, downloaded
19 Aug 2026. [MedGemma 1.5](https://developers.google.com/health-ai-developer-foundations/medgemma/model-card)
was released 13 Jan 2026 and is a different model.

**It is already multimodal, and the vision tower is already resident.** `mm_tokens_per_image: 256`,
`image_token_index`, `boi_token_index`/`eoi_token_index` are all in the config, and the vLLM startup
log reads *"Encoder cache will be initialized with a budget of 2048 tokens, and profiled with 7 image
items of the maximum feature size."* The image path is loaded and paid for in VRAM **today** — you
are simply not sending it images. Enabling image classification is application work (a tool, a
DICOM→image path from Orthanc on Server 1, a safety framing), not a redeployment.

### Does redeploying cancel the prompt engineering?

**No. Nothing is stored in the weights.** No fine-tuning was ever done. Everything lives in
`clinical-agent-service/app/nlu/medgemma.py` (790 lines): `SYSTEM_PROMPT` at line 101, `FEW_SHOT` at
line 190, tool descriptions generated from the registry at line 302, and the JSON schema in
`schema.py` constraining output through xgrammar. Those are source files. They survive any model
change untouched.

**But the artefacts surviving is not the same as the validation surviving.** The prompt is *calibrated*
to this model's quirks, and HANDOFF records what that cost: MedGemma has no `system` role because
Gemma 3's template drops it (2,178 characters became 4 tokens); explaining the surrounding system to a
4B model makes it refuse; the prompt is 2,138 of 4,096 tokens; the few-shot block is what actually
drives behaviour at this size.

So the risk depends entirely on *which* change:

| Change | Prompt file | Risk | Re-validation needed |
|---|---|---|---|
| **Quantise the same model to FP8** | byte-identical | **Low.** Same weights semantically, same chat template, same tokenizer, same token count, same few-shot. Only logits shift, and only at the margin | `eval_nlu.py` (**UNSAFE must stay 0**) + `explore.py`'s 44 scenarios. Hours, not days |
| **Swap to MedGemma 1.5** | survives, calibration may not | **High.** Different model. Its own card notes prompt-sensitivity differences ("less optimized for the SLAKE Q&A format"). Refusal behaviour, template handling and few-shot sensitivity would all need re-deriving | everything in HANDOFF's "things that will waste your time" list, from scratch |

**Decision taken: go straight to MedGemma 1.5 + FP8**, and settle the risk by measurement rather than
by sequencing. Most of what made a model swap look expensive turns out to be plumbing that does not
actually change:

| | MedGemma 1 (deployed) | MedGemma 1.5 (target) | Risk |
|---|---|---|---|
| Base | Gemma 3 | Gemma 3 | none |
| Chat template | no `system` role | no `system` role | **none** — HANDOFF's costliest finding carries over |
| Tokenizer | Gemma 3, 262k vocab | same | none — the 2,138-token budget holds |
| vLLM | serves it today | `vllm serve` documented | low |
| HF gating | HAI-DEF acceptance | same acceptance step | operational only |
| **Few-shot behaviour** | measured, UNSAFE = 0 | **unknown** | **the only real risk** |

That last row is one `eval_nlu.py` run away from being a fact, which is what §7.1's evaluation window
does. If 1.5 + FP8 passes, the intermediate FP8-on-MedGemma-1 step never happens.

**What 1.5 buys, and what it does not.** MedGemma 1 handles 2D images only; 1.5 adds 3D CT/MRI
volumes, longitudinal series and gigapixel pathology — for a department whose Orthanc is full of CT
and MRI, the difference between classifying a screenshot and reading the study. But its card is
explicit that CT, MRI and whole-slide images **"require some pre-processing"**. The volumetric support
is training plus a preprocessing convention on the same 2D SigLIP encoder, not an architecture that
eats DICOM. **The Orthanc → preprocessed-representation pipeline has to be built either way**, so 1.5
does not by itself unlock imaging. Also note 1.5 is **4B-only** — the 27B variants exist for
MedGemma 1 only, which is moot at 16 GB.

---

## 12. Preparing the field for the imaging service

Added 2026-09-02, answering Q-G: the end goal of this project is **an independent service that
classifies medical images for diagnosis and studies**. Not now. This section records what the STT
work has already bought it, and the three things that will actually be hard — so that decisions taken
now do not have to be unpicked later.

### What is already built for it

The dictation service was deliberately built as a *pattern*, not a one-off. An imaging service is the
same shape, and every piece transfers:

| Piece | Reuse |
|---|---|
| Separate Docker network (`stt_net`) | make `imaging_net` the same way. Proven: neither service can reach the other's containers |
| Its own channel secret | `IMAGING_CHANNEL_SECRET`, generated separately. Same reasoning as §3/Q2, more forcefully — an imaging service ingesting DICOM has a *larger* attack surface than one ingesting PCM |
| `purpose` / `aud` on the delegated token | `purpose=imaging`, `aud=imaging-service`. The mechanism already separates chat / rollback / read / stt; a fifth costs one `if` |
| One more SAN on the same certificate | `imaging.hospital.lan`, same hospitalCA, same two-step flow |
| Overlay + vhost, nothing existing edited | `docker-compose.imaging.yml` + `templates-imaging/` |
| The gateway shape itself | `stt-service/app/` is ~350 lines: config, security, bounds, an engine seam, one endpoint. Copy the skeleton, change what it validates |
| A new privilege | `App: agentgateway.imaging.use`, gated exactly like `voice.use` |

**The single most valuable thing to preserve is the seam** (§2.4): the gateway speaks one narrow
protocol to the model container and knows nothing about the model. That is what makes swapping
Qwen3-ASR cheap, and it is what will make swapping imaging models cheap.

### The three hard parts, none of which are model choice

**1. VRAM is already the binding constraint.** After MedGemma FP8 (0.50) and Qwen3-ASR (0.30), about
**2.9 GB** is free. That is not enough for a third resident model of any consequence. Three honest
options, in the order I would try them:

- run the imaging model **on demand** rather than resident — nobody watches a classification job, so
  a 3-minute cold start is acceptable where it would not be for chat or dictation;
- shrink MedGemma further, or move image work *into* MedGemma, which is already multimodal and
  already resident (§11) — its vision tower is loaded and paid for today;
- a second GPU, which is the honest answer if imaging is to be resident alongside everything else.

**2. The DICOM path crosses machines.** Orthanc is on **Server 1**; the GPU is on **Server 2**. Every
study has to travel, or the imaging service has to live on Server 1 without a GPU. The server2 README
already worked through the mirror image of this for OHIF and concluded the viewer belongs next to
Orthanc. Imaging inference concludes the opposite — it belongs next to the GPU — so the study moves.
That is a decision to take deliberately, with its own PHI-in-transit answer.

**3. Pre-processing, not the model, is the work.** MedGemma 1.5's own card says CT, MRI and
whole-slide images *"require some pre-processing"*. So even the model chosen for its 3D support does
not ingest DICOM: windowing, slice selection, resampling and normalisation all have to be built and —
harder — **validated**, because a wrong window is a wrong diagnosis with a confident tone.

### Two things to hold onto

**The safety framing changes completely.** Dictation is a draft a clinician edits (§6.1), and the
existing confirmation gate stands behind it. **A diagnostic classification has no equivalent.**
MedGemma's own card states its outputs must not directly inform clinical decisions and require
independent verification. An imaging service therefore needs its own answer to "what stops this being
treated as a diagnosis", and that answer is a governance question before it is an engineering one. It
should be designed in from the first line, not retrofitted.

**Do not let it drift into `agentgateway`.** The chat module should gain a relay and a privilege for
imaging — nothing more — exactly as it will for voice. Clinical image interpretation is not gateway
logic, and `agentgateway`'s charter ("no clinical logic… reusable by an unrelated department
unchanged") is worth defending precisely when it is inconvenient.

