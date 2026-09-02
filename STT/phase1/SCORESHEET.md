# Phase 1 scoresheet

Fill this in after running `./transcribe.sh`. Read step 6 of
[`../PHASE-1-SPIKE.md`](../PHASE-1-SPIKE.md) first — it explains *why* these particular
columns, and not word error rate.

**Date:** ______________  **Who recorded:** ______________
**Image tag that worked:** ______________  **Model repo:** ______________

---

## First, what you are actually judging

The transcript is a **draft you edit before sending** (`STT-PLAN.md` §6.1) — Telegram-style. So the
question is not "was it perfect" but **"how much typing did it save, and did it make a mistake I
would not have noticed?"**

That splits errors into two very different kinds:

- **Obvious** — a garbled word, wrong language, invented text. You see it, you fix it, it cost you
  two seconds. Track these; do not weight them heavily.
- **Plausible** — `0666777889` instead of `0666777888`. `Benali` instead of `Benhali`. Reads
  perfectly, and you skim past it because it is what you expected to say. **This is the only kind
  that really matters**, and it is what the model choice should turn on.

## The three questions, per sentence

- **Entities exact?** — names, digits, date, identifier, character for character. If wrong, mark it
  **P** (plausible — would have slipped past you) or **O** (obvious — you would have caught it).
- **Action word kept?** — `cherche` / `montre` / `crée` / `mets à jour` / `enregistre` /
  `programme`. A verb that became a *different* verb is a ✗.
- **Nothing invented?** — extra sentences you did not say, or a phrase repeating itself.

| # | What matters in this one | Entities exact? (or P/O) | Action word kept? | Nothing invented? | Time | Notes |
|---|---|---|---|---|---|---|
| 01 | `Kaced Amine` | ☐ | ☐ | ☐ | ___ s | |
| 02 | `10002T` — final letter counts | ☐ | ☐ | ☐ | ___ s | |
| 03 | `Slimani Slimani` | ☐ | ☐ | ☐ | ___ s | |
| 04 | `Amine Benali` · `3 avril 1978` | ☐ | ☐ | ☐ | ___ s | |
| 05 | `0666777888` — all ten digits | ☐ | ☐ | ☐ | ___ s | |
| 06 | `Marry Curry` → `Marie Curie` | ☐ | ☐ | ☐ | ___ s | |
| 07 | `féminin` | ☐ | ☐ | ☐ | ___ s | |
| 08 | **the trap — see below** | ☐ | ☐ | ☐ | ___ s | |
| 09 | `lundi prochain` · `dix heures` | ☐ | ☐ | ☐ | ___ s | |
| 10 | `Karnofsky` · `70` | ☐ | ☐ | ☐ | ___ s | |

**Totals:** entities ___/10 · action words ___/10 · nothing invented ___/10

---

## Sentence 08 — check this one on its own

Said: **"Le GCS s'est aggravé à 6 depuis ce matin."** — a *description*, not an instruction.

Transcribed as: ______________________________________________________________

- ☐ It stayed descriptive. **Good.**
- ☐ It became an instruction (e.g. *"Enregistre un GCS à 6"*). **Note it and tell me.**

*Corrected 2026-09-02:* an earlier version of this sheet called this "the most serious failure
possible". That was overstated. Three independent layers sit between this and a write — you read
the draft before sending it, the interpreter refuses to turn descriptive phrasing into a write
(`eval_nlu` UNSAFE = 0), and the confirmation gate demands an explicit *oui*. It is a strong signal
about the **model**, not a safety incident.

---

## Verdict

Judge on **plausible** errors and on how much editing each sentence needed — not on perfection.

**PASS** — **zero plausible errors (P)**, and any remaining fixes are quick and obvious.
→ Carry on to phase 2. A couple of O-marked rows is fine; you would have caught them.

- ☐ PASS — plausible errors: ____ / 10

**BORDERLINE** — **one** plausible error, or several obvious ones sharing a pattern (e.g. both are
phone numbers). Usually a fixable setting rather than a bad model: try passing the neurosurgery
vocabulary as biasing context, or re-record away from the fan.

- ☐ BORDERLINE — what failed, and did it have a pattern? ______________________________

**FAIL** — **two or more plausible errors**, or so much editing needed that typing would have been
faster.
→ Do not build on it. We bring the phase-4 bake-off forward and test Whisper instead.

- ☐ FAIL

---

## Anything that surprised you

_Worth more than the table. Odd behaviour, a word it never gets right, a sentence it handled
better than expected, background noise that mattered — write it here._

```




```
