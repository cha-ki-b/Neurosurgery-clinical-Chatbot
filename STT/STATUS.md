# Where this stands — 2026-09-02

A point-in-time summary: what exists, what is left, whether it is production-ready, and what belongs
in git.

---

## What was built

| Phase | Outcome |
|---|---|
| **1 — Spike** | Qwen3-ASR-0.6B runs on this GPU. 7.7 % substitution rate on real French medical speech; **0.013× realtime** (17 s of audio in 0.23 s) |
| **2 — Evaluation window** | MedGemma 1 + **FP8** promoted: **~4.1 GB freed**, `eval_nlu` UNSAFE = 0, explore.py distribution *identical* to baseline, vision output byte-identical. **MedGemma 1.5 failed the gate** and was not promoted |
| **3 — Dictation service** | `stt-gateway` + `stt-engine` live on Server 2, own network, own secret, own vhost, reachable only from Server 1. 66 tests |
| **4 — Model bake-off** | **Deferred** with Q-D |
| **5 — Module** | `agentgateway 1.2.0` deployed on Server 1. 73 tests |
| **Audit** | 3 findings, all fixed. One critical |

**Running now.** Server 2: `vllm` (MedGemma FP8), `clinical-agent`, `stt-gateway`, `stt-engine`,
`server2-proxy` — all healthy, 2.7 GB VRAM free. Server 1: `openmrs-app` on `agentgateway-1.2.0`,
zero validation errors.

---

## Is it production-ready?

**No — three configuration steps remain, and one honest gap.**

### The three steps (from `phase5/RESULTS.md`)

1. **Assign `App: agentgateway.voice.use`** — Administration → Manage Roles. Nothing appears until
   this is done.
2. **Set `agentgateway.sttChannelSecret`** — Administration → Settings → Agentgateway, to the
   `STT_CHANNEL_SECRET` value in `server2-stack/.env.stt`. **Through the UI, not SQL**: global
   properties are cached in memory and a direct `UPDATE` leaves the running instance on the old
   value. I have not set it myself — it is a credential and does not need to pass through a shell.
3. **Make `stt.hospital.lan` resolve inside `openmrs-app`** — needs `sudo` on Server 1, which this
   session does not have:
   ```bash
   echo "10.0.211.250  stt.hospital.lan" | sudo tee -a /etc/hosts
   ```

### The gap that is not a step

**No clinician has spoken to it yet, and no corpus exists.** Q-D is deferred, so:

- the model choice rests on a **public-audio smoke test**, not on your clinicians' speech;
- **Algerian-accented French is completely untested** — the phase-1 corpus is metropolitan speakers;
- the ten command sentences (imperatives packed with `0666777888`, `10002T`) have never been
  transcribed by anyone.

That is a real risk, and it is not mitigated by any amount of engineering. What *is* mitigated is the
consequence: the transcript is an editable draft, voice cannot confirm a write, and the interpreter
still refuses to turn descriptive phrasing into a write. So the failure mode is *irritation* — a
clinician retyping — rather than a wrong record.

**Phase 6 is therefore not a demo. It is the first measurement.** Treat a disappointing result as
information about the model, not as a broken deployment: the seam makes swapping it one environment
variable.

---

## Is the STT service decoupled?

**Yes, on every axis** — and genuinely so only since the audit. Full table in
[`SECURITY-AUDIT.md`](SECURITY-AUDIT.md).

Separate container, separate network (mutual unreachability verified), separate channel secret,
separate environment, separate token audience and purpose, separate code tree, separate model.
Removing it is `docker compose` without one `-f` flag, plus revoking one privilege.

The only shared item is the **RSA public key** used to verify tokens. It is public; sharing it avoids
a second key lifecycle for no security gain. The private key never leaves OpenMRS.

---

## What to push to GitHub, and what must stay here

`origin/main` currently holds only a README and an old submodule pointer, so this is close to a
first push. Nothing here is a git repository yet.

### Push

| Path | Why |
|---|---|
| `stt-service/` | the service: `app/`, `tests/`, `Dockerfile`, `requirements.txt`, `.gitignore` |
| `STT/*.md` | `README`, `STT-PLAN`, `PHASE-1-SPIKE`, `SECURITY-AUDIT`, `STATUS`, and each `phase*/RESULTS.md` |
| `STT/phase1/` | `sentences-fr.txt`, `record.sh`, `transcribe.sh`, `eval-dataset.py`, `SCORESHEET.md`, `Dockerfile.spike` |
| `STT/phase2/` | `run-arm1.sh`, `compare.py`, `vision-probe.sh`, `results/BASELINE.md` |
| `server2-stack/` | `docker-compose.stt.yml`, `nginx/templates-stt/`, `nginx/nginx.conf`, `stt/lexicon-neurochir.txt`, `.env.example` |
| `chatbot-neuro/openmrs-module-agentgateway/` | the module source and `CHANGELOG.md` |

### Never push

| Path | Why |
|---|---|
| `server2-stack/.env` | `AGENT_CHANNEL_SECRET` and every OpenMRS UUID |
| **`server2-stack/.env.stt`** | **`STT_CHANNEL_SECRET`. New file — the existing `.gitignore` does not match it** |
| `server2-stack/certs/` | `agent.key`. Already ignored |
| `backup files/` | contains `.env` copies with live secrets |
| `~/.cache/huggingface/token` | outside these trees, but do not let it wander in |
| `STT/phase1/audio/`, `phase1/dataset-audio/` | 21 MB of audio; the clinician recordings, when they exist, are PHI |
| `STT/phase1/results*.txt`, `phase2/results/*.txt` | may contain transcribed speech |
| `*/target/`, `__pycache__/` | build output |

**One thing to fix before the first push:** `server2-stack/.gitignore` ignores `.env` but **not
`.env.stt`**. Add it, and add an `.env.stt.example` with the value blank, mirroring `.env.example`.

Both files are `0600`. Nothing else on disk holds a live secret — verified by exact-match scan of
every document and source file against both channel secrets and the HF token.

---

## Pending work, in the order I would take it

1. **The three configuration steps** above. Small, and nothing works without them.
2. **`.gitignore` for `.env.stt`**, then the first push.
3. **Phase 6** — a clinician, a real sentence, a private window (microphone permission is per-origin
   and sticky; a cached grant makes a broken permission flow look like it works).
4. **Phase 7** — per-user quota under 5–10 concurrent dictations. Untested at concurrency so far.
5. **Q-D, whenever it becomes possible.** Everything about model quality is provisional until then.
6. **MedGemma 1.5** — deferred, not abandoned. Needs prompt re-tuning to restore MedGemma 1's caution
   level, then a re-run of phase 2's gate. Self-contained; nothing blocks it.
7. **The imaging service** — §12 of the plan records what the dictation work already bought it and
   the three hard parts (VRAM is already the binding constraint; the DICOM path crosses machines;
   pre-processing is the work, not the model).

---

## Two things worth carrying forward

**The audit's critical finding came from checking what a container *held*, not what it *answered*.**
The isolation tests passed the whole time — presenting the wrong secret returned 403 in both
directions — while `stt-gateway` quietly had `AGENT_CHANNEL_SECRET` in its environment because
`env_file:` hands over the whole file. `docker exec <c> printenv` would have found it in one command.
When the imaging service is built on this pattern, put that in its acceptance list.

**Three separate mistakes this session were caught by disagreement between two numbers**, not by a
test failing: `compare.py` reporting no new failures while the counts said otherwise; a privilege
description accepted at build time and rejected at module start; a validation rule that turned out to
apply to privileges but not global properties. Each was settled by reading the database rather than
the report. That habit is worth more here than any individual fix.
