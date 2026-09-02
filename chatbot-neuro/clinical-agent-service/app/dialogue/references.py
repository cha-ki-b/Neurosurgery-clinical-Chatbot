"""Resolving what a short turn refers to, against the frame that is already open.

The failure this exists to end: a clinician who has already said which patient and which field is
being changed types "change it to 06564565", and the assistant asks - for the second time - which
field they mean. Nothing in the pipeline could represent "the field is telephone, the value is
still to come", so naming a field on its own was not an answer to any question and destroyed the
half-finished request instead of advancing it.

Two rules do most of the work here, and neither needs a pronoun list:

*A field can be named without a value.* "le telephone", "the phone", "son numero" identify a slot.
That is a complete answer to "what should I change?", and the next question is for the value.

*When a field is already active and the turn names no other field, any value in the turn belongs
to that field.* This resolves "it", "that", "make it 42" and a bare "06564565" identically, and it
does so without guessing: the antecedent is not inferred from the words, it is read from the
frame. If the turn names a different field, the frame no longer decides and the named field wins.

Values are extracted here without the cue word ``app.nlu.rules`` requires ("tel 0555..."), for the
same reason ``_bare_slot_answer`` already drops that requirement when answering a question: the
question - or the active field - *is* the cue. Applying that reasoning only to a bare reply and
not to "change it to 06564565" is what made the two turns behave differently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

from ..nlu.rules import (
    BARE_IDENTIFIER_RE,
    _GENDER_F_RE,
    _GENDER_M_RE,
    _NAME_STOPWORDS,
    _TIME_RE,
    _extract_dates,
    normalise,
)

# The sentinel the frame uses for "I asked which field, not for a slot's value". It is not a slot
# name and must never reach a tool: it names a *question*, and the answer to it fills
# ``TaskFrame.active_field``.
FIELD_CHOICE = "__field__"


# --------------------------------------------------------------------------- field vocabulary

# How a clinician names each slot, in either language. Only slots the tools actually declare
# appear here: a field the application cannot change must not be recognisable, or the assistant
# would accept "change his address", ask for a value, and then have nowhere to put it.
FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "phone": (
        "numero de telephone", "numero de tel", "telephone", "tel", "portable", "mobile",
        "numero", "phone number", "phone", "number", "contact",
    ),
    "name": (
        "nom complet", "nom de famille", "prenom", "nom", "full name", "family name",
        "first name", "last name", "name",
    ),
    "birthdate": (
        "date de naissance", "naissance", "date of birth", "birth date", "birthdate", "dob",
    ),
    "gender": ("sexe", "genre", "gender", "sex"),
    "identifier": (
        "numero de dossier", "identifiant", "matricule", "identifier", "record number", "id",
    ),
    "dates": ("date du rendez-vous", "date", "appointment date", "day"),
    "time": ("heure", "time", "hour"),
    "gcs_total": ("score de glasgow", "glasgow", "gcs"),
    "karnofsky": ("indice de karnofsky", "karnofsky"),
}


@dataclass
class FieldResolution:
    """Which field a turn names, or which ones it could not be told apart between."""

    field: Optional[str] = None
    ambiguous: Tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.field is not None


def resolve_field(prompt: str, allowed: Sequence[str]) -> FieldResolution:
    """The field this turn names, out of the ones the tool actually offers.

    The longest alias wins, so "numero de telephone" is a phone number rather than an identifier
    ("numero"). Two *different* fields named in one turn is ambiguity, not a preference order:
    it is reported rather than resolved, because picking one would write to a field nobody chose.
    """
    text = normalise(prompt)
    best: Dict[str, int] = {}

    for field_name in allowed:
        for alias in FIELD_ALIASES.get(field_name, ()):
            if re.search(rf"\b{re.escape(alias)}\b", text):
                best[field_name] = max(best.get(field_name, 0), len(alias))

    if not best:
        return FieldResolution()
    if len(best) == 1:
        return FieldResolution(field=next(iter(best)))

    # Several fields matched. A longer alias beats a shorter one outright - "numero de telephone"
    # contains "numero", and that overlap is not a real ambiguity. Only a genuine tie is reported.
    ranked = sorted(best.items(), key=lambda item: item[1], reverse=True)
    if ranked[0][1] > ranked[1][1]:
        return FieldResolution(field=ranked[0][0])
    return FieldResolution(ambiguous=tuple(name for name, _ in ranked))


def readable_field(field_name: str) -> str:
    """The field's name in the words the clinician used to ask for it."""
    return {
        "phone": "le numero de telephone",
        "name": "le nom",
        "birthdate": "la date de naissance",
        "gender": "le sexe",
        "identifier": "l'identifiant",
        "dates": "la date",
        "time": "l'heure",
        "gcs_total": "le score de Glasgow",
        "karnofsky": "l'indice de Karnofsky",
    }.get(field_name, field_name)


# --------------------------------------------------------------------------- value extraction

_BARE_PHONE_RE = re.compile(r"(\+?\d[\d\s.\-]{4,17}\d)")
_BARE_INT_RE = re.compile(r"\b(\d{1,3})\b")
# Lead-ins a value arrives behind when the field is already known. Stripped so the *name* field
# gets "Walter Black" out of "change it to Walter Black" rather than the whole sentence.
_VALUE_LEAD_IN_RE = re.compile(
    r"^\s*(?:"
    r"(?:c'?est|il est|elle est|its?|it'?s|the (?:new )?(?:value|one) is)"
    r"|(?:mets?|met|mettre|mettez|change[rz]?|changer|corrige[rz]?|remplace[rz]?|update|set|make|put)"
    r"(?:\s+(?:le|la|les|l'|son|sa|ses|leur|the|his|her|their|it|that|this|ca|cela))*"
    r")\b[\s:,]*(?:a|à|to|en|par|with|avec|=)?[\s:,]*",
    re.IGNORECASE,
)


# Words that carry the correction, not the value. Without them, "en fait plutot le nom" - a
# clinician *switching* which field to change - was read as an instruction to rename the patient
# to "en fait plutot", which the update tool would have written.
_VALUE_FILLERS = {
    "en", "fait", "plutot", "alors", "donc", "bon", "bien", "du", "coup", "mais", "et",
    "actually", "instead", "rather", "well", "so", "then", "ok", "okay",
    "oui", "non", "yes", "no", "merci", "thanks", "please", "svp", "stp",
}

# An explicit assignment: "... a 0555", "... to Walter Black", "le nom est X". Free-text values
# need one of these unless the assistant has just asked for that exact field, because without it
# there is nothing to distinguish a value from the rest of the sentence.
_ASSIGNMENT_RE = re.compile(r"\b(?:a|à|to|est|is|sera|devient|=|:)\s+(?P<value>.+)$", re.IGNORECASE)


def value_for_field(prompt: str, field_name: str, *, whole_turn_is_value: bool = False) -> Optional[Any]:
    """The value this turn supplies for a field that is already known, or None.

    None means "this turn does not answer for that field" - never a guess. The caller asks again
    rather than filling a patient record with something that merely appeared in the sentence.

    ``whole_turn_is_value`` is set when the assistant has just asked for this exact field, which is
    what licenses reading a whole free-text turn as the value. A number is self-evidencing and
    needs no such licence; a name is not, and taking one without it is how a correction became a
    rename.
    """
    original = prompt.strip()
    text = normalise(original)

    if field_name == "phone":
        match = _BARE_PHONE_RE.search(original)
        if not match:
            return None
        digits = re.sub(r"[\s.\-]", "", match.group(1))
        return digits if len(re.sub(r"\D", "", digits)) >= 6 else None

    if field_name in ("birthdate", "dates"):
        found = _extract_dates(original, text)
        if not found:
            return None
        return found[0] if field_name == "birthdate" else found

    if field_name == "time":
        match = _TIME_RE.search(text)
        if not match:
            return None
        hour, minute = match.groups()
        return f"{int(hour):02d}:{minute or '00'}"

    if field_name == "gender":
        if _GENDER_M_RE.search(text):
            return "M"
        if _GENDER_F_RE.search(text):
            return "F"
        return None

    if field_name == "identifier":
        candidate = _strip_lead_in(original)
        if BARE_IDENTIFIER_RE.match(candidate) and any(char.isdigit() for char in candidate):
            return candidate.upper()
        return None

    if field_name in ("gcs_total", "karnofsky"):
        # The question - or the active field - already named the score, so a bare integer needs no
        # cue of its own. Range is enforced by the validator, not here: a value out of range was
        # still plainly *given*, and saying "15 is the maximum" beats "I did not understand".
        match = _BARE_INT_RE.search(text)
        return int(match.group(1)) if match else None

    if field_name == "name":
        candidate = _strip_lead_in(original)
        if not whole_turn_is_value and candidate.strip().lower() == original.strip().lower():
            # Nothing was stripped, so the turn carried no instruction to assign anything. Look for
            # an explicit assignment; without one, this turn does not supply a name.
            assignment = _ASSIGNMENT_RE.search(original)
            if not assignment:
                return None
            candidate = assignment.group("value")
        return _trim_value_to_name(candidate)

    return None


def _strip_lead_in(prompt: str) -> str:
    """Drops the instruction wrapping a value: "change it to X" -> "X"."""
    stripped = _VALUE_LEAD_IN_RE.sub("", prompt.strip(), count=1)
    return stripped.strip().strip("\"'«»“”.,;:").strip()


def _trim_value_to_name(candidate: str) -> Optional[str]:
    """A person's name out of a turn whose field is already known to be the name.

    Stricter than the general extractor on one point and looser on another. Looser: no trigger
    word is needed, because the frame supplied the context. Stricter: anything made only of
    stopwords, pronouns or digits is rejected outright - "it", "him" and "0655123456" are the
    three things that have actually reached a patient's name field in this deployment.
    """
    tokens = [token for token in candidate.split() if token]
    kept = [
        token for token in tokens
        if normalise(token).strip("'’-") not in _NAME_STOPWORDS
        and normalise(token).strip("'’-") not in _VALUE_FILLERS
    ]
    if not kept:
        return None
    value = " ".join(kept)
    if not any(char.isalpha() for char in value):
        return None
    return value


def looks_like_a_person_name(value: Optional[str]) -> bool:
    """Whether a value can be a patient's name at all.

    Two measured failures, one check. A pronoun: "generate a report for him" produced a search for
    a patient called "him", which came back empty and was reported to the clinician as "no patient
    matches" - a false clinical fact produced by a grammatical word. And a bare number: "mets a
    jour son telephone a 0666777888" carries no name at all, yet came back as
    slots={"name": "0666777888"}, which passes any substring corroboration by definition because
    the clinician did type those digits.

    Both are the same mistake - taking a word's presence in the sentence as evidence that it was
    *meant* as a name - so both are refused here rather than in two places.
    """
    if not value:
        return False
    text = str(value).strip()
    if not any(char.isalpha() for char in text):
        return False
    tokens = [normalise(token).strip("'’-") for token in text.split() if token]
    return any(token and token not in _NAME_STOPWORDS for token in tokens)


# --------------------------------------------------------------------------- turn classification

# Conversational repair, not a new request: the clinician is telling us our question was wrong,
# already answered, or repeated. These must not abandon the frame - abandoning is what turned
# "je te l'ai deja dit" into a three-turn create thrown away - but they carry no value either, so
# the frame answers by saying what it already holds and asking again for precisely what is left.
_REPAIR_RE = re.compile(
    r"\b(deja dit|deja donne|je (te |vous )?l'?ai deja|already (told|said|gave)|"
    r"je viens de (le |vous )?dire|tu (te )?repetes?|vous (vous )?repetez|"
    r"you are repeating|you're repeating|same (thing|as before)|comme (avant|tout a l'heure)|"
    r"c'est (ce que|deja) |encore la meme|quoi ?\?|hein|attends?|wait|"
    r"je (ne )?comprends pas|i don'?t understand|what\?)\b"
)

# An amendment to something already stated, rather than a fresh request. "actually, make it 42"
# revises the pending value; restarting the whole task would lose everything else.
_CORRECTION_RE = re.compile(
    r"\b(en fait|plutot|plutot que|au lieu de|non[, ]+(mets?|change|c'est)|"
    r"actually|instead|rather|no[, ]+(make|change|set) it|correction|erreur|je me suis trompe)\b"
)


# Slots whose value is a number, so an attempt at one is recognisable even when it is wrong. A
# clinician who answers "quel est le numero ?" with "12" has plainly answered; saying "12 is not a
# phone number" is the useful reply, and abandoning the whole request is not.
_NUMERIC_SLOTS = {"phone", "gcs_total", "karnofsky", "time"}


def attempted_value(prompt: str, field_name: str) -> Optional[Any]:
    """What this turn was evidently *trying* to supply for a numeric field, valid or not.

    Only ever fed to the validator, never to a tool: its whole purpose is to turn "I did not
    understand" into "that is not a valid phone number", which is the difference between a
    conversation that recovers and one that starts over.
    """
    if field_name not in _NUMERIC_SLOTS:
        return None
    digits = re.findall(r"\d+", prompt)
    if not digits:
        return None
    if field_name == "phone":
        return "".join(digits)
    if field_name == "time":
        return prompt.strip()
    try:
        return int(digits[0])
    except ValueError:
        return None


def is_repair(prompt: str) -> bool:
    return bool(_REPAIR_RE.search(normalise(prompt)))


def is_correction(prompt: str) -> bool:
    return bool(_CORRECTION_RE.search(normalise(prompt)))
