# Phase 5 — `agentgateway` 1.2.0: built, tested, deployed

2026-09-02. Module **1.2.0 is running on Server 1**. `73 tests` green (52 api + 21 omod, up from 69).

**Three steps remain before a clinician sees a microphone**, and none of them are code — §"What is
left" below. Two need your hands; one needs `sudo` on Server 1, which I do not have.

---

## What was added

| | |
|---|---|
| `App: agentgateway.voice.use` | separate from `chat.use`, so dictation can be switched off without withdrawing the assistant |
| `agentgateway.sttServiceUrl` / `sttChannelSecret` / `sttTimeoutMillis` | three new settings |
| `POST /module/agentgateway/transcribe.form` | relays raw PCM, returns the transcript. Gated on `voice.use`, bounded at 30 s |
| `purpose=stt` tokens for audience `stt-service` | never carry a write capability |
| `agent-voice.js` | click-to-start / click-to-stop capture, 16 kHz mono PCM, client-side silence check |
| microphone button in both templates | dashboard widget and full-screen page |

`AgentAuditFilter`, the operation log, the rollback engine and the chat path are **untouched**. The
filter still verifies only `clinical-agent-service`, so a dictation token can never authenticate an
OpenMRS API call — a property that came free from the existing design.

### The two safety rules are enforced by the build

`agent-voice.js` never sends and never confirms. A new `ModuleWiringTest` case strips comments and
then fails the build if the file mentions `agentSend(`, `agentPost(` or `agentConfirm(` **in code**.

That check earned its place immediately: it failed on first run — against the file's own
documentation, which names those calls while explaining they are forbidden. A check that punished
the file for documenting the rule would have pushed the next person to delete the explanation, so it
strips comments first. The rule is worth having precisely because it is one careless convenience
away from being lost.

---

## Two failures during the deploy

### A privilege description over 250 characters is rejected at module start, not at build time

`App: agentgateway.voice.use` packaged cleanly, deployed cleanly, and then threw on a running
instance:

```
ValidationException: 'App: agentgateway.voice.use' failed to validate with reason:
description: This value exceeds the maximum length of 250 permitted for this field.
```

Repeated once per retry. **The privilege was never created** — so the feature would have been
invisible with nothing obviously wrong, which is the expensive kind of failure.

Now caught by `everyPrivilegeDescriptionSurvivesOpenmrsValidation`.

### The first version of that test was wrong

It checked privileges **and** global properties, and immediately failed on a 355-character
`sttChannelSecret` description. That looked like a second catch. It was a false positive:

| | Evidence |
|---|---|
| Both columns are `TEXT` | `information_schema` — so 250 is a validator rule, not a schema one |
| The 355-char **global property** was written successfully | present in `global_property`, length 355 |
| The **privilege** was not | absent from `privilege` in the same deploy |

So the limit applies to privileges only. Scoped the test accordingly and recorded the evidence in
its comment, rather than truncating a useful description to satisfy a rule that does not exist.

**Both mistakes are mine, and both were found by checking the database rather than trusting the
report.** That is the discipline HANDOFF already asks for, arriving twice in one afternoon.

---

## Verified after deployment

```
App: agentgateway.chat.use      82
App: agentgateway.chat.write   183
App: agentgateway.rollback     101
App: agentgateway.voice.use    244   <-- created
agentgateway.sttChannelSecret  (set, length 0)
agentgateway.sttServiceUrl     (set, length 24)
agentgateway.sttTimeoutMillis  (set, length 5)
validation errors this boot:   0
```

The old `agentgateway-1.1.5.omod` is backed up **outside** the modules directory — inside
`openmrs-app` at `/tmp/agentgateway-backups/`, and on Server 1's host filesystem at
`~/agentgateway-1.1.5.omod.bak-20260902-140716`. A backup left *in* the modules directory is still
scanned on boot and logs a confusing error.

---

## What is left — three steps, none of them code

**1. Assign the privilege.** Administration → Manage Roles → `App: agentgateway.voice.use`, to the
roles that should dictate. Nothing appears until this is done; the button is privilege-gated by
design.

**2. Set the channel secret.** Administration → Settings → Agentgateway →
`agentgateway.sttChannelSecret`, to the `STT_CHANNEL_SECRET` value in `server2-stack/.env`.

**Through the UI, not SQL.** Global properties are cached in memory and a direct `UPDATE` leaves the
running instance serving the old value. I have deliberately not set it myself — it is a credential,
and it does not need to pass through a shell or a database client to get where it is going.

Until it is set, `isDictationConfigured()` is false and the microphone is not rendered at all. That
is intended: a button that cannot work is worse than no button.

**3. Make `stt.hospital.lan` resolve from inside `openmrs-app`** — needs `sudo` on Server 1, which
this session does not have.

```bash
echo "10.0.211.250  stt.hospital.lan" | sudo tee -a /etc/hosts
```

On this deployment the container resolves through Docker's embedded DNS → the host's
`systemd-resolved` → Server 1's `/etc/hosts`, which is exactly how `agent.hospital.lan` already
works. No container recreate needed. Verify with:

```bash
docker exec openmrs-app getent hosts stt.hospital.lan
```

The alternative is adding the name to the hospital's internal DNS, which is the tidier answer if
whoever runs it is available — `agent`, `viewer`, `orthanc` and `openmrs` are all there already,
and `stt` is the odd one out.

---

## Then: phase 6

Once those three are done, dictation is end-to-end and phase 6 is a clinician saying one French
sentence into the chat box, **in a private window** — microphone permission is per-origin and
sticky, so a cached grant makes a broken permission flow look like it works.

Worth remembering what is still unmeasured: **Q-D is deferred**, so no corpus exists and the model
choice rests on the phase-1 public-audio smoke test. Algerian-accented French is untested. Phase 6
will be the first time this system hears the people who will actually use it — treat it as
measurement, not just a demo.
