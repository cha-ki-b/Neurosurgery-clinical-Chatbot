# Phase 1 — the spike, step by step

**Goal:** prove that `Qwen3-ASR-0.6B` can turn your spoken French into correct text on this machine,
before we build anything around it.

**Time:** about an hour, most of it waiting for a download.

**What this touches:** one throwaway container, and the `vllm` container gets stopped and started
again. Nothing else. No files in `chatbot-neuro/` or `server2-stack/` are edited. Nothing is
installed on the host except — if you choose to — one small audio recording tool.

**Where everything runs:** on **Server 2**, which is *this* machine (`10.0.211.250`). You never touch
Server 1 in this phase. Commands marked **[host]** run in your normal terminal. Commands marked
**[container]** run *inside* a container and always start with `docker exec` or `docker run`.

---

## The idea, in plain words

A speech-to-text model is a program that takes a sound file and gives back the words that were said.

We want to check three things, in this order:

1. Can we get the model onto this machine at all?
2. Does the software that runs models (vLLM) know how to run *this* model?
3. When you say a real sentence from your daily work, does it write down the right words —
   **especially the numbers and the patient names**?

If the answer to all three is yes, we carry on with the plan. If the answer to number 3 is no, we
have saved ourselves from building a whole service around a model that cannot do the job.

Everything below is written so you can copy one block at a time, run it, and check what you see
against what the document says you should see.

---

## Step 0 — Open a terminal and go to the right place

**[host]**

```bash
cd /home/cerist/STT/phase1
```

Everything in this phase happens in this folder. It already exists, and it already contains:

| File | What it is |
|---|---|
| `sentences-fr.txt` | the 10 French sentences you are going to read out loud |
| `record.sh` | records one sentence into a sound file |
| `transcribe.sh` | sends all your sound files to the model and prints what it heard |
| `SCORESHEET.md` | the table you fill in to decide pass or fail |
| `audio/` | empty folder where your recordings will go |

---

## Step 1 — Check the machine can hear you

**Classification: READ-ONLY.** Nothing changes.

**[host]**

```bash
arecord -l
```

**What you should see:** a list with a line like

```
card 1: PCH [HDA Intel PCH], device 0: ALC897 Analog [ALC897 Analog]
```

That is the sound chip on the motherboard. It means the machine *has* a microphone socket. It does
**not** yet prove a microphone is plugged in.

Now actually test it. Plug a microphone or a headset into the **pink** socket on the back of the
computer, then run:

**[host]**

```bash
arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 3 /tmp/mic-test.wav
aplay /tmp/mic-test.wav
```

Say "un, deux, trois" while the first command runs. It stops on its own after 3 seconds. The second
command plays it back.

**What you should see and hear:** the recording command prints a couple of lines and exits without an
error, and then you hear your own voice.

**If it fails:**

- `arecord: command not found` → install it: `sudo apt install alsa-utils`
- The file plays but is silent → the microphone is not plugged in, is muted, or is in the wrong
  socket. Open the Ubuntu **Settings → Sound → Input** panel, pick the right input, and check the
  input level bar moves when you speak.
- `device or resource busy` → something else is using the microphone. Close any video-call app.

> **Why 16000?** The model wants sound sampled 16,000 times a second, mono (one channel). Recording it
> that way from the start means nobody has to convert anything later. `plughw:1,0` means "card 1,
> device 0, and convert the format for me if you need to".

**Do not carry on until you can hear your own voice.** Everything after this depends on it.

---

## Step 2 — Give the graphics card some free memory

**Classification: SERVICE-AFFECTING (development environment — safe here).**

Right now the graphics card is almost full. The MedGemma model is using 12.5 GB of the 16 GB.

**[host]**

```bash
nvidia-smi --query-gpu=memory.total,memory.used,memory.free --format=csv
```

**What you should see:** something close to `16303 MiB, 12926 MiB, 2894 MiB`. Only about 2.9 GB is
free, which is not comfortable.

The simplest thing is to switch MedGemma off for the duration of the spike:

**[host]**

```bash
docker stop vllm
```

**What happens to the chatbot:** it keeps working. The clinical agent notices the model is gone and
automatically falls back to its `rules` engine, which understands fewer phrasings but is not broken.
This is a designed, tested behaviour, not a crash. We put MedGemma back in Step 7.

Check the memory came back:

**[host]**

```bash
nvidia-smi --query-gpu=memory.free --format=csv
```

**What you should see:** roughly `16000 MiB` free (a couple of hundred MB stay used by the desktop).

---

## Step 3 — Start the speech model

This is the step that either works or teaches us something. It does two things at once: it downloads
the model (about 2 GB, once) and then starts it.

**Classification: SAFE CHANGE.** A new throwaway container. Nothing existing is modified.

> **This step was run for real on 2026-09-01 and both of its traps were hit.** The commands below are
> the corrected ones. If you followed an earlier version of this document and it failed, that was a
> bug in the instructions, not something you did. See [Step 3 — what went wrong the first
> time](#step-3--what-went-wrong-the-first-time) at the end for the detail.

### 3a. Build the image (once)

vLLM's published image does **not** include the audio libraries, so the transcription endpoint
rejects every file with *"Invalid or unsupported audio file."* This adds them:

**[host]**

```bash
cd /home/cerist/STT/phase1
docker build -f Dockerfile.spike -t vllm-audio-spike:v0.28.0 .
```

`Dockerfile.spike` is already in the folder. It is three lines: start from `vllm/vllm-openai:v0.28.0`,
`pip install librosa soundfile`. Takes about 30 seconds.

**What you should see:** ends with `naming to docker.io/library/vllm-audio-spike:v0.28.0`.

### 3b. Start it

**[host]**

```bash
mkdir -p /home/cerist/models/hf-cache

docker run -d --name qwen-asr-spike \
  --gpus all \
  -p 127.0.0.1:8100:8000 \
  -v /home/cerist/models/hf-cache:/root/.cache/huggingface \
  vllm-audio-spike:v0.28.0 \
  --model Qwen/Qwen3-ASR-0.6B \
  --served-model-name qwen3-asr \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.35 \
  --max-num-seqs 8
```

Line by line, because every part matters:

| Part | What it means |
|---|---|
| `-d` | run in the background so you get your terminal back |
| `--name qwen-asr-spike` | a name we can use to stop and delete it later |
| `--gpus all` | let the container use the graphics card |
| `-p 127.0.0.1:8100:8000` | **only this machine** can talk to it, on port 8100. See the warning below |
| `-v .../hf-cache:...` | download the model onto the hard disk here, so a restart does not download it again |
| `--model Qwen/Qwen3-ASR-0.6B` | which model to fetch and run |
| `--served-model-name qwen3-asr` | the short name we use when asking it to transcribe |
| `--max-model-len 8192` | **do not leave this out.** The model's own default is 65,536, and vLLM refuses to start unless it can reserve enough memory for one sentence that long — 7 GB. We will never send more than about 30 seconds of speech, so 8192 is generous |
| `--gpu-memory-utilization 0.35` | use at most 35 % of the card, about 5.4 GB. Measured actual use: 5.5 GB |

> ### ⚠️ The one security thing in this step
>
> `-p 127.0.0.1:8100:8000` starts with `127.0.0.1` on purpose. **The model has no password of any
> kind.** Anyone who can reach its port can use the hospital's graphics card for whatever they like.
> `127.0.0.1` means "only programs running on this very machine can reach it". If you write
> `-p 8100:8000` instead, you have just opened it to the whole hospital network. Do not do that.

Now watch it start:

**[host]**

```bash
docker logs -f qwen-asr-spike
```

Press `Ctrl+C` to stop watching (this stops *watching*, not the container).

**What you should see, in order:**

1. Download progress bars — a few minutes the first time, nothing on later runs.
2. `Resolved architecture: Qwen3ASRForConditionalGeneration` — vLLM recognises the model. ✔ confirmed
3. `Capturing CUDA graphs` — normal, takes about 45 seconds
4. `Supported tasks: ['generate', 'transcription']` — **this is the one that matters.** `transcription`
   means the endpoint we need is live
5. Finally: **`Application startup complete`**

Total time from `docker run` to ready is about **3 minutes** (measured), most of it CUDA graph capture.
Be patient — it is not stuck.

That last line is the success signal. Confirm it is really answering:

**[host]**

```bash
curl -sS http://127.0.0.1:8100/v1/models
```

**What you should see:** a line of JSON containing `"id":"qwen3-asr"`.

### If Step 3 fails

This is the step most likely to need a second try, and that is fine — *finding the right image tag is
one of the things phase 1 exists to do.* Read the last 30 lines:

```bash
docker logs --tail 30 qwen-asr-spike
```

| What the log says | What it means | What to do |
|---|---|---|
| `To serve at least one request with the model's max seq len (65536)` | **you left out `--max-model-len`** | add `--max-model-len 8192`. This is the trap that bit us first time |
| `Invalid or unsupported audio file` *(at Step 5, not startup)* | you started the **stock** vLLM image instead of `vllm-audio-spike` | redo step 3a, then 3b with the right image name |
| `unsupported architecture` | this vLLM version is too old | try the other repo (below), then a newer tag. *Not expected — v0.28.0 is confirmed working* |
| `no kernel image is available` | wrong CUDA build for this graphics card | try tag `nightly`. *Not expected on v0.28.0* |
| `CUDA out of memory` | not enough free memory | check Step 2 really stopped `vllm`; lower `0.35` to `0.25` |

**To retry anything, first remove the old container** — the name cannot be reused:

```bash
docker rm -f qwen-asr-spike
```

**Alternative repo to try.** Qwen publishes two versions of the same model. If the first is not
recognised, swap this one line and run the `docker run` block again:

```
  --model Qwen/Qwen3-ASR-0.6B-hf \
```

**Already answered for you:** `vllm/vllm-openai:v0.28.0` + `Qwen/Qwen3-ASR-0.6B` (the plain repo,
not `-hf`) + the audio libraries is a confirmed-working combination on this machine. That is
`STT_VLLM_TAG` settled.

---

## Step 4 — No microphone? Do this instead

**Skip to Step 4b if you have a microphone.** If you do not, this gets you a real measurement today,
using real French speech that already comes with a written reference — so nothing has to be recorded.

**Classification: READ-ONLY** — downloads public audio, asks the model questions.

**[host]**

```bash
cd /home/cerist/STT/phase1
python3 eval-dataset.py --dataset multimed --n 10 --medical-only --pool 60
```

It fetches real doctor–patient French consultations from the public `MultiMed-ST` dataset, converts
each clip to mono 16 kHz, sends it to the model, and prints the reference next to the transcript with
a word error rate. Results are saved to `results-multimed.txt`.

**What you should see:** ten blocks like

```
─── 003 ── WER  20.0%  (25 words, 0.13s)
  ref : madame joselin votre consultation s'est bien passé? très bien. votre docteur est très gentille.
  hyp : Madame Jocelyn, votre consultation s'est bien passée ? Très bien, le docteur est très gentil.
```

### How to read the number — carefully

**The headline WER on this dataset is misleading and you should not act on it.** When this was run on
2026-09-01 it reported an aggregate of **37.9 %**, which looks like a failing model. It is not. The
breakdown:

| | Count | Share of reference words |
|---|---|---|
| Substitutions — the model wrote a *different* word | 22 | **7.7 %** |
| Deletions | 23 | 8.1 % |
| Insertions — the model wrote words the reference does not have | 63 | 22.1 % |

The insertions are the dataset's fault, not the model's. The clips are overlapping windows and each
reference covers only the middle of what is spoken, so a *correct* transcript of the whole clip is
scored as errors. One sample makes it obvious: its reference has 11 words for 13 seconds of audio —
a speaking rate of **51 words per minute**, when conversational French runs 150–200. The model's
transcript of the same clip implies 226 wpm. The reference is truncated; the model was right.

**So judge it on substitutions.** 7.7 % — and several of those are not errors either: `1er` written
as `premier`, `36` written as `trente six`. Genuine mistakes were `bronchite` → `bonne chic` and the
name `Joselin` → `Jocelyn`.

**Verdict from that run: the model handles conversational French medical speech well.** It also
transcribed 96 seconds of audio in 1.4 seconds — a realtime factor of **0.014×**, about 70× faster
than speech.

### What this does *not* tell you

Be clear about the limits, because it is tempting to treat this as done:

- ❌ **Algerian-accented French.** The dataset is metropolitan speakers. This is the single biggest
  open question and this test cannot touch it.
- ❌ **Your ward's acoustics** — fans, corridors, distance from the microphone.
- ❌ **The ten command sentences.** Those are imperatives packed with names, identifiers and digits
  (`0666777888`, `10002T`). Conversational speech does not exercise them.
- ❌ **Sentence 8's descriptive/imperative trap.**

**This can falsify the model, not green-light it.** A bad result here would mean stop. A good result
means the model has cleared a floor. The corpus in `STT-PLAN.md` Q-D is still required, and phase 5
should not start without it.

---

## Step 4b — Record the ten sentences

**Classification: SAFE — creates files in `phase1/audio/` only.**

Open the list and read it once before recording anything:

**[host]**

```bash
cat sentences-fr.txt
```

These are not random sentences. They are the things the assistant already knows how to do, and they
deliberately contain the hard parts: **patient names, a phone number, an identifier, a date, and
scores**. Sentence 8 is a trap on purpose — more on that in Step 6.

Read the list first (`cat sentences-fr.txt`), then record them one at a time:

**[host]**

```bash
./record.sh 1
```

It counts down, records for up to 15 seconds, and stops when you press `Ctrl+C` — or you can let it
run out. Speak **normally**: your usual pace, your usual voice, at your usual distance from the
microphone. Do not enunciate like a newsreader. We are testing how it copes with you, not with a
performance.

Do that for all ten:

```bash
./record.sh 2
./record.sh 3
# … through to …
./record.sh 10
```

Listen back to any you are unsure about:

```bash
aplay audio/03.wav
```

If one is bad, just record it again — `./record.sh 3` overwrites it.

**What you should see when done:**

```bash
ls -l audio/
```

Ten files, `01.wav` to `10.wav`, each a few hundred kilobytes. **A file of 44 bytes is empty** — that
recording failed, do it again.

---

## Step 5 — Let the model listen

**Classification: READ-ONLY** as far as the system is concerned — it just asks the model questions.

**[host]**

```bash
./transcribe.sh
```

**What you should see:** for each of the ten, the sentence you were supposed to say, then what the
model actually heard, then how long it took. Something like:

```
─── 01 ───────────────────────────────────────────
  expected : Cherche le patient Kaced Amine.
  heard    : Cherche le patient Kaced Amine.
  time     : 0.31 s
```

It also writes everything to `results.txt` so you can read it again later without re-running.

**If it fails:**

| Message | Meaning | Fix |
|---|---|---|
| `Connection refused` | the model container is not running | `docker ps` — is `qwen-asr-spike` there? Go back to Step 3 |
| `404` | wrong model name | check `curl -sS http://127.0.0.1:8100/v1/models` says `qwen3-asr` |
| empty `heard` | the sound file is silent | re-record that one |

---

## Step 6 — Review it: how to decide pass or fail

This is the part that actually matters, so do not rush it.

**The transcript is a draft you edit before sending** — Telegram-style, `STT-PLAN.md` §6.1. Nothing
is sent, and nothing is written, until you read the text and press send. So the question is not
"was it perfect" but **"how much typing did it save, and did it slip a mistake past me?"**

That splits mistakes into two kinds that matter very differently:

- **Obvious** — a garbled word, wrong language, invented text. You see it, you fix it, two seconds
  lost. Worth tracking, not worth weighting.
- **Plausible** — `0666777889` instead of `0666777888`. `Benali` instead of `Benhali`. It reads
  perfectly and you skim past it, because it is what you expected to say. **This is the kind that
  matters**, and it is what the model choice should turn on.

A wrong small word (*le* instead of *la*) is neither — the interpreter understands it anyway.

Open `SCORESHEET.md` and fill in one row per sentence. For each one ask three questions:

**Question 1 — did it get the important bits *exactly* right?**
The names, the numbers, the date, the identifier. Character for character. If one is wrong, mark it
**P** or **O**: would you have *noticed*, reading the draft back? `0666777889` for `0666777888` is a
**P** — it reads fine and you would have sent it. A mangled `zéro six six six...` is an **O**.

**Question 2 — did it keep the action word?**
`cherche`, `montre`, `crée`, `mets à jour`, `enregistre`, `programme`. If the verb changed into a
different verb, that row **fails**.

**Question 3 — did it invent anything?**
Look for extra sentences you did not say, or the same phrase repeated over and over.

> ### ⚠️ Known: this model invents text when it hears silence
>
> Tested directly on 2026-09-01. Fed three seconds of an empty room with `language=fr`, it returned
> confident French sentences that nobody said:
>
> | Input | Returned |
> |---|---|
> | digital silence | `Ah.` |
> | very quiet room | `Je suis un peu en colère.` |
> | room / fan noise | `Il est possible de faire un travail de révision.` |
>
> It happens **every time** the language is pinned to French. Left on auto-detect it sometimes
> returns nothing, but then it guesses the wrong language instead.
>
> **This is a real finding and it changes the design, not the spike.** The service will need an
> energy check that refuses to send silent audio to the model at all — see `STT-PLAN.md` §6.6. It is
> a cheap fix and it is now written into the plan.
>
> **For your purposes today:** just make sure each recording actually contains you speaking. If a
> transcript looks like a sentence from another conversation, check the recording is not empty
> (`aplay audio/NN.wav`) before you mark the row as a failure.

Genuine invention on top of real speech is still worth recording — that is what question 3 is for.

### Sentence 8 is a deliberate trap

Sentence 8 is **"Le GCS s'est aggravé à 6 depuis ce matin."** That describes something that happened.
It is *not* an instruction to save anything. The model must write it down **as you said it**.

If it comes back as *"Enregistre un GCS à 6"* — a command — note it and tell me.

*Corrected 2026-09-02:* an earlier version of this document called that "the single most serious
failure possible". That was overstated, and the correction matters because it changes how you weight
the row. Three independent layers sit between a mis-transcription and a write: you read the draft
before sending, the interpreter refuses to turn descriptive phrasing into a write (`eval_nlu`
UNSAFE = 0), and the confirmation gate summarises every write and waits for an explicit *oui*. So
this is a strong signal about the **model**, not a safety incident.

### The verdict

**PASS — carry on to phase 2** if all of these hold:

- [ ] All 10 came back as recognisable French
- [ ] **Zero plausible errors** — nothing wrong that you would have skimmed past
- [ ] Any remaining fixes were quick and obvious
- [ ] **10 / 10** kept the correct action word
- [ ] Every response took **under 1 second**

**BORDERLINE — worth a second look** if there is **one** plausible error, or several obvious ones
sharing a pattern (say, both are phone numbers). That usually means a fixable setting rather than a
bad model — the next things to try are passing the neurosurgery vocabulary as biasing context, or
re-recording away from a noisy fan.

**FAIL — tell me before building anything** if there are **two or more plausible errors**, or if
correcting the drafts would have taken longer than typing them. We would go to the phase-4 bake-off
early and test Whisper instead. That is not a disaster; it is exactly what a spike is for.

---

## Step 7 — Put everything back

**Classification: SAFE CHANGE — restores the previous state.**

**[host]**

```bash
docker rm -f qwen-asr-spike
docker start vllm
```

Wait about a minute for MedGemma to load, then check it is healthy:

**[host]**

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}'
```

**What you should see:** `vllm` showing `Up ... (healthy)`. If it says `(health: starting)`, wait
another minute — loading the model takes time.

Then confirm the chatbot really is back on MedGemma and not still on the fallback:

**[host]**

```bash
docker logs --tail 5 vllm
```

**What you should see:** the engine logging throughput lines again.

Your recordings and results stay in `phase1/` — **do not delete them**. They are the first ten items
of the evaluation corpus that phase 4 needs, and they are the only thing in this whole project that
cannot be regenerated by running a command.

---

## What to send me when you are done

1. `results.txt`
2. Your filled-in `SCORESHEET.md`
3. The exact image tag and model repo that worked (from Step 3)
4. Anything that surprised you

That is everything I need to either green-light phase 2 or change the model choice.

---

## Step 3 — what went wrong the first time

Kept because the plan says a failed arm is evidence worth keeping, and because both traps will
reappear in phase 3 when the real `stt-engine` container is written.

**Trap 1 — the missing `--max-model-len`.** My original command omitted it. Qwen3-ASR's own default
context is **65,536 tokens**, and vLLM refuses to start unless it can reserve enough KV cache for one
request that long:

```
ValueError: To serve at least one request with the model's max seq len (65536),
7.0 GiB KV cache is needed, which is larger than the available KV cache memory (2.28 GiB).
```

It reads like "not enough memory", which sends you off lowering `--gpu-memory-utilization` — the
opposite of the fix. The real problem is a context length we will never use. The existing MedGemma
container has carried `--max-model-len 4096` since day one for exactly this reason; I simply failed
to carry it across.

**Trap 2 — no audio libraries in the published image.** `vllm/vllm-openai:v0.28.0` starts perfectly,
reports `Supported tasks: ['generate', 'transcription']`, and then rejects every upload:

```json
{"error":{"message":"Invalid or unsupported audio file.","type":"BadRequestError","code":400}}
```

in 28 milliseconds — too fast to be the model. `librosa` and `soundfile` are simply absent
(`torchaudio` is present, which is why it starts at all). vLLM's own docs mention installing
`vllm[audio]`; the Docker image does not include those extras. Hence `Dockerfile.spike`.

**What the failure did prove**, before it failed:

- `vllm/vllm-openai:v0.28.0` **does** resolve `Qwen3ASRForConditionalGeneration` — no nightly needed
- the plain `Qwen/Qwen3-ASR-0.6B` repo works; `-hf` is not required
- the weights download and load cleanly on this Blackwell card — no sm_120 kernel problem

### Measured numbers, for phase 3

From the successful start at `--gpu-memory-utilization 0.35`:

| | Measured |
|---|---|
| Weights + non-torch | 2.17 GiB |
| Peak activation | 1.05 GiB |
| CUDA graphs | 0.09 GiB |
| KV cache allocated | 2.18 GiB |
| **Total** | **~5.5 GiB** |
| Startup, `docker run` → ready | ~3 minutes |
| Transcribe 3 s of audio | **0.139 s** (~22× realtime, concurrency 1) |

vLLM's own suggestion in the log was that 1.95 GiB of KV cache would fit the request. So the floor for
this model is roughly **3.8 GiB ≈ `--gpu-memory-utilization 0.24`**, which confirms the **0.25**
budgeted for `stt-engine` in `STT-PLAN.md` §5.2 — that number is now measured, not estimated.
