"""Deterministic checks on a slot's value, run before anything is summarised or sent.

Measured on the live deployment: a clinician answered "quelle est la date de naissance ?" with
"20-99-2008". The extractor turned it into ``2008-99-20`` because its pattern reads three numbers
separated by dashes and never asks whether they name a day that exists. That value went into a
confirmation summary the clinician approved, and OpenMRS - the first component in the chain to
apply a calendar - rejected the create with a Java parser message about ``monthOfYear``. The
clinician then had to re-enter the name, the sex and the date, because the failure also threw the
frame away.

Every rule here is a value a patient record cannot hold. They live in application code rather than
in the model's prompt or the tool's build function for the reason the whole redesign turns on:
a check that is only ever asked of a language model is not a check. And they run *before* the
summary, so the clinician is never shown, and never approves, something that cannot be written.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

# A person alive today. Wider than any plausible patient on purpose: the check exists to catch a
# typo that inverts a century ("1878"), not to adjudicate longevity.
MAX_AGE_YEARS = 130

# ITU-T E.164 caps a subscriber number at 15 digits; six is below any real one, and is what
# distinguishes a phone number from a stray year.
MIN_PHONE_DIGITS = 6
MAX_PHONE_DIGITS = 15

MAX_NAME_CHARS = 100


@dataclass
class SlotProblem:
    """One value that cannot be written, and the question that gets a usable one instead."""

    slot: str
    message: str


def _parse_iso(value: Any) -> Optional[_dt.date]:
    if not isinstance(value, str):
        return None
    try:
        return _dt.date.fromisoformat(value)
    except ValueError:
        return None


def _today() -> _dt.date:
    return _dt.date.today()


def check_slot(slot: str, value: Any) -> Optional[SlotProblem]:
    """The reason this value cannot be used, or None."""
    if value is None or value == "":
        return None

    if slot == "birthdate":
        parsed = _parse_iso(value)
        if parsed is None:
            return SlotProblem(
                slot,
                f"« {value} » n'est pas une date valide. Donnez la date de naissance au format "
                "JJ/MM/AAAA, par exemple 20/09/2008.",
            )
        today = _today()
        if parsed > today:
            return SlotProblem(
                slot,
                f"La date de naissance {parsed.isoformat()} est dans le futur. "
                "Quelle est la date de naissance du patient (JJ/MM/AAAA) ?",
            )
        if parsed.year < today.year - MAX_AGE_YEARS:
            return SlotProblem(
                slot,
                f"La date de naissance {parsed.isoformat()} donnerait un age de plus de "
                f"{MAX_AGE_YEARS} ans. Verifiez l'annee et redonnez la date (JJ/MM/AAAA).",
            )
        return None

    if slot == "dates":
        values = value if isinstance(value, list) else [value]
        for entry in values:
            if _parse_iso(entry) is None:
                return SlotProblem(
                    slot,
                    f"« {entry} » n'est pas une date valide. Donnez la date au format JJ/MM/AAAA.",
                )
        return None

    if slot == "time":
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(value)):
            return SlotProblem(slot, f"« {value} » n'est pas une heure valide. Donnez-la sous la forme 14h30.")
        return None

    if slot == "phone":
        digits = re.sub(r"\D", "", str(value))
        if not (MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS):
            return SlotProblem(
                slot,
                f"« {value} » ne ressemble pas a un numero de telephone "
                f"({MIN_PHONE_DIGITS} a {MAX_PHONE_DIGITS} chiffres). Quel est le numero ?",
            )
        return None

    if slot == "gcs_total":
        number = _as_int(value)
        if number is None or not 3 <= number <= 15:
            return SlotProblem(
                slot,
                f"Un score de Glasgow va de 3 a 15 ; « {value} » n'en est pas un. Quel est le score ?",
            )
        return None

    if slot == "karnofsky":
        number = _as_int(value)
        if number is None or not 0 <= number <= 100 or number % 10 != 0:
            return SlotProblem(
                slot,
                f"L'indice de Karnofsky va de 0 a 100, par pas de 10 ; « {value} » n'en est pas un. "
                "Quel est l'indice ?",
            )
        return None

    if slot in ("eye_response", "verbal_response", "motor_response"):
        bounds = {"eye_response": (1, 4), "verbal_response": (1, 5), "motor_response": (1, 6)}[slot]
        number = _as_int(value)
        if number is None or not bounds[0] <= number <= bounds[1]:
            return SlotProblem(
                slot,
                f"Cette composante du score de Glasgow va de {bounds[0]} a {bounds[1]} ; "
                f"« {value} » n'en est pas une.",
            )
        return None

    if slot == "gender":
        if str(value).upper() not in ("M", "F"):
            return SlotProblem(slot, "Quel est le sexe du patient (masculin ou feminin) ?")
        return None

    if slot == "name":
        text = str(value).strip()
        if not any(char.isalpha() for char in text):
            return SlotProblem(slot, f"« {text} » ne peut pas etre un nom de patient. Quel est le nom ?")
        if len(text) > MAX_NAME_CHARS:
            return SlotProblem(slot, "Ce nom est anormalement long. Donnez le nom du patient seul.")
        return None

    return None


def first_problem(slots: Dict[str, Any]) -> Optional[SlotProblem]:
    """The first unusable value in a set of slots, checked in a stable order.

    Stable so that a turn supplying two bad values asks about the same one every time rather than
    alternating between them, which reads as the assistant changing its mind.
    """
    for slot in sorted(slots):
        problem = check_slot(slot, slots[slot])
        if problem is not None:
            return problem
    return None


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
