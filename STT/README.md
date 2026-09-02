# STT — speech input for the clinical assistant

A microphone button in the assistant's chat box: click it, speak one sentence in French, click again,
and the text appears in the input box as an editable draft. The clinician then sends it as a normal chat turn, and everything
downstream — the clinical agent, the confirmation gate, the audit log — works exactly as it does
today.

**Status: phases 1, 2, 3 and 5 done.** The dictation service is live on Server 2 and module
`agentgateway 1.2.0` is deployed on Server 1. **Three configuration steps remain** before a
clinician sees a microphone — see [`phase5/RESULTS.md`](phase5/RESULTS.md). Phase 4 (the model
bake-off) is deferred with Q-D.

The service code lives at **`~/stt-service/`**, a sibling of `chatbot-neuro/` and `server2-stack/`.

---

## Start here

| Document | Read it when |
|---|---|
| **[`STT-PLAN.md`](STT-PLAN.md)** | you want the design: architecture, security, model choice, VRAM budget, Docker layout, delivery phases |
| **[`PHASE-1-SPIKE.md`](PHASE-1-SPIKE.md)** | **you are about to do the work.** Step by step, with what you should see after each step and what to do when it fails |
| [`phase1/SCORESHEET.md`](phase1/SCORESHEET.md) | you have run the spike and need to decide pass or fail |
| [`phase1/results-multimed.txt`](phase1/results-multimed.txt) | what the model actually produced on real French clinical audio, 2026-09-01 |
| **[`phase2/RESULTS.md`](phase2/RESULTS.md)** | the FP8 evaluation: what passed, what it freed, and what is blocked |
| **[`phase3/RESULTS.md`](phase3/RESULTS.md)** | the dictation service: acceptance checks, the isolation proof, and end-to-end timings |
| **[`phase5/RESULTS.md`](phase5/RESULTS.md)** | the OpenMRS module: what shipped, two deploy failures, and the three steps left |

If you only read one thing before touching the machine, read **`PHASE-1-SPIKE.md`**.

---

## The design in six lines

- Click to start, speak, click to stop → **one** request. No streaming, no WebSocket, no partial text.
- The browser talks to **OpenMRS**, never to Server 2 — same trust boundary as the chat (ADR-12).
- Audio is raw 16 kHz PCM, so the service needs **no audio codec** — no ffmpeg anywhere.
- The model is **`Qwen3-ASR-0.6B`** on vLLM, behind an OpenAI-compatible interface, so swapping it is
  one environment variable.
- **Separate channel secret** from the clinical agent, **separate Docker network**. A compromised STT
  service can transcribe audio and nothing else.
- The transcript **never auto-sends** — you edit it, then send. Confirmation is **never** by voice.

---

## Where things run

| | Address | Runs |
|---|---|---|
| Server 1 | `10.0.211.249` | OpenMRS + `agentgateway`, Orthanc, the viewer, the `hospitalCA` authority |
| **Server 2** | `10.0.211.250` | **this machine** — the GPU, the clinical agent, and the STT service |

Everything in this folder is done on **Server 2**. Phase 1 never touches Server 1.

---

## Layout

```
STT/
  README.md            this file
  STT-PLAN.md          the design
  PHASE-1-SPIKE.md     step-by-step for phase 1, and how to validate it
  phase3/
    RESULTS.md         acceptance checks and what went wrong
  phase5/
    RESULTS.md         module 1.2.0: what shipped and what is left
  phase2/
    RESULTS.md         evaluation-window findings and the promotion decision
    run-arm1.sh        MedGemma 1.5 arm, end to end, restores vllm on exit
    compare.py         applies the promotion gate mechanically
    vision-probe.sh    checks a served model can still read an image
    results/           BASELINE.md + raw output per arm
  phase1/
    sentences-fr.txt   the 10 French sentences to read aloud
    record.sh          ./record.sh 3   → records sentence 3 into audio/03.wav
    transcribe.sh      ./transcribe.sh → sends them all to the model, prints what it heard
    SCORESHEET.md      the table that decides pass or fail
    eval-dataset.py    no microphone? measures the model on public French audio
    Dockerfile.spike   vLLM image + the audio libraries it ships without
    audio/             your recordings land here
    dataset-audio/     public clips fetched by eval-dataset.py
    results*.txt       written by transcribe.sh / eval-dataset.py
```

---

## Delivery phases

Full table with pass/fail checks in [`STT-PLAN.md` §7](STT-PLAN.md).

| # | What | Status |
|---|---|---|
| 1 | Spike — does Qwen3-ASR-0.6B transcribe our French correctly? | **model runs; French verified on public audio. Own recordings still needed** |
| 2 | Evaluation window — promote on `eval_nlu` UNSAFE = 0 | **done. MedGemma 1 + FP8 promoted; 1.5 failed the gate ([results](phase2/RESULTS.md))** |
| 3 | `stt-service` + compose overlay + nginx vhost | **done — live, reachable only from Server 1 ([results](phase3/RESULTS.md))** |
| 4 | Model bake-off on a recorded clinician corpus | **deferred — Q-D answered "later"** |
| 5 | `agentgateway` 1.2.0 — the microphone button | **deployed. 3 config steps left ([results](phase5/RESULTS.md))** |
| 6 | End to end, a real clinician, a real utterance | **next, after the 3 config steps** |
| 7 | Per-user quota + concurrent load test | blocked on 6 |

---

## Two things worth knowing before you touch anything

**The recordings are the only irreplaceable thing here.** Every container, image and config file in
this project can be recreated by running a command. Ten clinicians' voices saying real clinical
sentences cannot. `phase1/audio/` is the first ten items of the corpus phase 4 needs — do not delete
it.

**The model endpoint has no authentication of any kind.** In phase 1 it is published on
`127.0.0.1:8100`, which means only this machine can reach it. If that ever becomes `-p 8100:8000`, it
is open to the whole hospital network. The same rule governs the real deployment: `expose:`, never
`ports:`.

---

## Still open

Tracked in [`STT-PLAN.md` §10](STT-PLAN.md):

- **Q-C** — does the *service code* live at `STT/stt-service/`, and does any of this go into git?
- **Q-D** — can we record the corpus: 50–100 utterances, 3–5 clinicians, in the department?
- **Q-E** — which browsers do the clinicians actually use?
- **Q-G** — what is the imaging use case, concretely? It decides how much of MedGemma 1.5 is needed.
