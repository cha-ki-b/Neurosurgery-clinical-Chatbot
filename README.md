# Clinical assistant — CHU Blida, neurosurgery

A conversational assistant inside OpenMRS. A clinician types — or dictates — a sentence in French,
and the assistant searches for a patient, reads a record, or creates and updates one. Every write is
summarised in plain French and waits for an explicit confirmation, and every call it makes runs under
the requesting clinician's own OpenMRS privileges.

Live against the hospital's real OpenMRS.

---

## What is here

| Directory | What it is |
|---|---|
| **`chatbot-neuro/`** | the assistant. `openmrs-module-agentgateway/` is the OpenMRS-side module (chat relay, delegated tokens, audit log, rollback); `clinical-agent-service/` is the FastAPI service that interprets a sentence and calls OpenMRS back |
| **`server2-stack/`** | the GPU host's deployment: one Nginx terminating TLS, and every other container reachable only through it |
| **`stt-service/`** | dictation. Turns speech into text and nothing else — no OpenMRS account, no model of its own |
| **`STT/`** | the dictation project's design, phase results and security audit |
| `neuro-patientview/`, `models/` | the department's patient dashboard, and downloaded model weights |

### Two machines

| | Address | Runs |
|---|---|---|
| **Server 1** | `10.0.211.249` | OpenMRS + the `agentgateway` module, Orthanc, the DICOM viewer, and `hospitalCA` — the authority that signs every certificate here |
| **Server 2** | `10.0.211.250` | the GPU: the clinical agent, the dictation service, and the models behind them |

The browser only ever talks to OpenMRS. Server 2 accepts connections from Server 1 and from nowhere
else — a rule that is enforced by the proxy, proved by a test, and the reason no channel secret ever
has to be readable by page JavaScript.

---

## Tech stack

**Server 1** — OpenMRS 2.12.2 (platform 2.5.9, Tomcat 7, Java 8), MySQL, the `agentgateway` module
built with Maven. Orthanc for DICOM, OHIF as the viewer, Nginx Proxy Manager in front.

**Server 2** — Docker Compose behind Nginx. Python 3.11 / FastAPI for both services. vLLM serving
**MedGemma 4B** (FP8, multimodal) as the interpreter and **Qwen3-ASR-0.6B** for dictation, on an
RTX 5070 Ti. Both models are quantised or sized to share one 16 GB card.

**Between them** — TLS from a single internal authority, a shared channel secret per service, and
short-lived RS256 tokens that carry the clinician's identity. Each service has its own secret, its
own token audience and its own Docker network; none of them can reach another's containers.

---

## Getting started

Read [`chatbot-neuro/HANDOFF.md`](chatbot-neuro/HANDOFF.md) first. It is the canonical "state of the
system right now" and will save you re-deriving things that cost days to learn.

**To work on the assistant** — [`chatbot-neuro/README.md`](chatbot-neuro/README.md) for the security
model, [`DEPLOYMENT-GUIDE.md`](chatbot-neuro/DEPLOYMENT-GUIDE.md) to install from scratch.

**To work on the deployment** — [`server2-stack/README.md`](server2-stack/README.md). Adding a service
is one compose overlay and one vhost template; nothing already running gets edited.

**To work on dictation** — [`STT/README.md`](STT/README.md), then
[`STT/STATUS.md`](STT/STATUS.md) for what is done and what is left.

Bring the GPU host up with:

```bash
cd server2-stack && docker compose -f docker-compose.yml -f docker-compose.vllm.yml -f docker-compose.stt.yml up -d
```

Copy `.env.example` to `.env` and `.env.stt.example` to `.env.stt` first, and fill in the secrets from
OpenMRS. Neither real file is in the repository, and they must not be: the two channel secrets are
deliberately different values, and a service that holds the other's secret undoes the reason they are
separate.

---

## Source control

**One repository, not four.** `server2-stack`'s compose files build from three sibling directories
(`../chatbot-neuro/clinical-agent-service`, `../stt-service`, `../STT/phase1`), so splitting them
apart means `docker compose up` only works if every clone lands in exactly the right layout — or
means submodules, which removed a directory from this project once already. The components are also
version-coupled: module 1.2.0 requires the dictation service to exist.

> ### ⚠️ The repository root is currently a home directory
>
> Beside the project sit `~/.ssh/id_ed25519_server1` — **the private key to the production OpenMRS
> server** — along with `~/.cache/huggingface/token`, `~/.gnupg`, `~/.bash_history`, and
> `backup files/`, which holds `.env` copies with live channel secrets and a copy of the TLS private
> key. A `git add -A` here would stage all of it.
>
> `/.gitignore` is therefore **deny-by-default**: it ignores everything, then names the four project
> directories explicitly. Adding a new one means adding a line — so the failure mode is "my folder
> isn't tracked", noticed in seconds, rather than "my private key is tracked", noticed far too late.
>
> **It is a seatbelt, not a fix.** The right shape is to move the four directories into a dedicated
> root so the repository contains only project content:
>
> ```
> ~/openmrs-orthanc-integration/{chatbot-neuro,server2-stack,stt-service,STT}
> ```
>
> They stay siblings, so every build context still resolves. The running containers hold absolute
> bind-mount paths, so they need recreating afterwards — which is why this has not been done for you.

---

## Before you change anything

[`CLAUDE.md`](CLAUDE.md) is the working protocol, and it is not style advice — every rule in it exists
because ignoring it broke something here. The short version:

- **This is a live hospital system.** One change at a time; read before you write; never claim
  something works without showing it.
- **Say where a command runs** — host or container, Server 1 or Server 2. That distinction causes more
  confusion here than anything else.
- **Git holds reads only.** The operational configuration is not in the repository, so
  `git checkout <file>` is not a rollback path. Back up with timestamped copies into `backup files/`.
- **Test viewers and microphones in a private window.** A cached credential or a sticky permission
  grant makes a broken flow look like a working one.
