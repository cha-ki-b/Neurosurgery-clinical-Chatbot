"""The pipeline from section 4.2, in order: identify, scope, gate, map, construct, execute, report.

The two gates are the whole point of this file.

*The clarification gate* stops a turn that is ambiguous or outside the declared capability list
and asks instead of guessing (CA3).

*The confirmation gate* stops **every** create, update or booking - not only the ambiguous ones -
shows the clinician a plain-language summary of exactly what will be written, and waits for an
explicit yes (CA5, ADR-2). A confident misreading is still a misreading, and the cost of one turn
of friction is nothing against the cost of a wrong value in a patient record.

Nothing here decides whether the clinician is *allowed* to do what they asked. That is settled by
OpenMRS, under their own privileges, when the call actually lands (CA7).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .capabilities import registry as capability_registry
from .conversation import PendingAction, PendingOperation, store
from .nlu.base import (
    INTENT_CANCEL,
    INTENT_CONFIRM,
    INTENT_UNSUPPORTED,
    Interpretation,
)
from .nlu.rules import RuleBasedNlu, classify_answer, extract_slots
from .openmrs_client import ApiResult, OpenmrsClient, OpenmrsUnavailable, explain_failure
from .security import ActingUser
from .tools.registry import PlannedOperation, ToolRegistry, ToolSpec, missing_slots

log = logging.getLogger(__name__)

STATE_ANSWERED = "answered"
STATE_AWAITING_CLARIFICATION = "awaiting_clarification"
STATE_AWAITING_CONFIRMATION = "awaiting_confirmation"
STATE_CANCELLED = "cancelled"
STATE_FAILED = "failed"
STATE_UNSUPPORTED = "unsupported"


@dataclass
class TurnResult:
    reply: str
    state: str
    task_type: Optional[str] = None
    pending_action: Optional[Dict[str, Any]] = None
    # Not sent to the browser; used by tests and by the server log.
    executed: List[Dict[str, Any]] = field(default_factory=list)

    def to_response(self, conversation_id: str) -> Dict[str, Any]:
        return {
            "conversation_id": conversation_id,
            "reply": self.reply,
            "state": self.state,
            "task_type": self.task_type,
            "pending_action": self.pending_action,
        }


class Orchestrator:
    def __init__(self, nlu=None, tool_registry: Optional[ToolRegistry] = None) -> None:
        from .config import settings
        from .tools.catalog import build_registry

        self._nlu = nlu or RuleBasedNlu()
        self._tools = tool_registry or build_registry(settings.patientview_tools_enabled)

    # ------------------------------------------------------------------ entry point

    async def handle_turn(
        self,
        prompt: str,
        delegated_token: str,
        user: ActingUser,
        conversation_id: str,
        context: Dict[str, Any],
    ) -> TurnResult:
        state = store.get(conversation_id, user.username)
        if context.get("patient_uuid"):
            state.last_patient_uuid = context["patient_uuid"]

        if state.pending is not None:
            return await self._handle_pending_answer(prompt, delegated_token, user, state)

        interpretation = self._interpret_with_carryover(prompt, state)

        if interpretation.intent == INTENT_UNSUPPORTED or interpretation.task is None:
            state.draft_task = None
            state.draft_slots = {}
            return TurnResult(
                reply=interpretation.clarification or "Je ne peux pas traiter cette demande.",
                state=STATE_UNSUPPORTED,
            )

        if interpretation.needs_clarification:
            # Hold on to what was understood so the answer to the question completes the request
            # instead of restarting it.
            state.draft_task = interpretation.task
            state.draft_slots = dict(interpretation.slots)
            return TurnResult(
                reply=interpretation.clarification,
                state=STATE_AWAITING_CLARIFICATION,
                task_type=interpretation.task,
            )

        return await self._plan_and_maybe_execute(prompt, delegated_token, user, state, interpretation)

    # ------------------------------------------------------------------ confirmation loop

    async def _handle_pending_answer(self, prompt, delegated_token, user, state) -> TurnResult:
        pending = state.pending
        answer = classify_answer(prompt)

        if answer == INTENT_CANCEL:
            store.clear_pending(state.conversation_id)
            return TurnResult(reply="C'est annule, rien n'a ete enregistre.", state=STATE_CANCELLED,
                              task_type=pending.task_type)

        if answer != INTENT_CONFIRM:
            return TurnResult(
                reply="Je n'ai pas compris votre reponse. Repondez « oui » pour enregistrer, "
                      "ou « non » pour annuler.",
                state=STATE_AWAITING_CONFIRMATION,
                task_type=pending.task_type,
                pending_action=_describe_pending(pending),
            )

        # Two things are re-checked at the moment of the yes rather than trusted from when the
        # summary was produced: that this is the same clinician, and that they still hold the
        # chat-write privilege. A privilege revoked mid-conversation takes effect immediately.
        if pending.username != user.username:
            store.clear_pending(state.conversation_id)
            return TurnResult(reply="Cette action ne vous appartient pas et a ete annulee.", state=STATE_FAILED)

        if not user.may_write:
            store.clear_pending(state.conversation_id)
            return TurnResult(
                reply="Votre compte n'autorise pas l'enregistrement via l'assistant. Rien n'a ete enregistre.",
                state=STATE_FAILED,
                task_type=pending.task_type,
            )

        store.clear_pending(state.conversation_id)
        return await self._execute(
            [
                PlannedOperation(op.method, op.path, op.body, op.summary, op.body_from_results)
                for op in pending.operations
            ],
            delegated_token,
            state.conversation_id,
            pending.task_type,
            prompt,
            success_prefix="C'est enregistre.",
        )

    # ------------------------------------------------------------------ planning

    def _interpret_with_carryover(self, prompt: str, state) -> Interpretation:
        interpretation = self._nlu.interpret(prompt, {})

        draft_task = getattr(state, "draft_task", None)
        awaiting = getattr(state, "awaiting_slot", None)

        if draft_task and interpretation.intent == INTENT_UNSUPPORTED:
            # The turn does not name a task because it is the answer to a question we asked.
            merged = dict(getattr(state, "draft_slots", {}))
            merged.update(extract_slots(prompt))

            if awaiting:
                # The answer belongs in the slot that was asked about, and it must be allowed to
                # *replace* what is there. Merging it in as a default is what made the loop
                # inescapable: asked to narrow a name down to an identifier, the stale name kept
                # winning and the same question came back forever.
                slot, answer = _bare_slot_answer(awaiting, prompt, merged)
                if answer is not None:
                    merged[slot] = answer
                    if slot == "identifier":
                        # Searching by identifier is exact. Leaving the ambiguous name in place
                        # would just reproduce the ambiguity that prompted the question.
                        merged.pop("name", None)
                state.awaiting_slot = None
            else:
                merged.setdefault("name", prompt.strip())

            return Interpretation(intent="task", task=draft_task, slots=merged)

        if draft_task and interpretation.task == draft_task:
            merged = dict(getattr(state, "draft_slots", {}))
            merged.update(interpretation.slots)
            interpretation.slots = merged

        return interpretation

    async def _plan_and_maybe_execute(self, prompt, delegated_token, user, state, interpretation) -> TurnResult:
        availability = self._tools.for_task(interpretation.task, capability_registry.current)
        if availability is None:
            return TurnResult(reply="Cette action ne fait pas partie de ce que je sais faire.",
                              state=STATE_UNSUPPORTED, task_type=interpretation.task)
        if not availability.available:
            return TurnResult(
                reply=f"Je ne peux pas effectuer cette action ici. {availability.reason}",
                state=STATE_UNSUPPORTED,
                task_type=interpretation.task,
            )

        tool: ToolSpec = availability.tool
        slots = dict(interpretation.slots)
        tool_context: Dict[str, Any] = {}
        client = OpenmrsClient(delegated_token, state.conversation_id, tool.task, prompt)

        # A write the user cannot have executed is refused before anything is planned, so no
        # summary is ever shown for an action that could not have happened anyway.
        if tool.writes and not user.may_write:
            return TurnResult(
                reply="Votre compte permet uniquement les consultations. Je ne peux rien enregistrer "
                      "en votre nom.",
                state=STATE_FAILED,
                task_type=tool.task,
            )

        if tool.requires_patient:
            resolution = await self._resolve_patient(slots, state, client)
            if resolution.get("clarification"):
                state.draft_task = tool.task
                state.draft_slots = slots
                state.awaiting_slot = resolution.get("awaiting_slot")
                return TurnResult(reply=resolution["clarification"], state=STATE_AWAITING_CLARIFICATION,
                                  task_type=tool.task)
            if resolution.get("error"):
                return TurnResult(reply=resolution["error"], state=STATE_FAILED, task_type=tool.task)
            tool_context.update(resolution["context"])
            state.last_patient_uuid = tool_context["patient_uuid"]

        gaps = missing_slots(tool, slots)
        if gaps:
            state.draft_task = tool.task
            state.draft_slots = slots
            state.awaiting_slot = gaps[0]
            question = tool.slot_questions.get(gaps[0], f"Il me manque : {gaps[0]}.")
            return TurnResult(reply=question, state=STATE_AWAITING_CLARIFICATION, task_type=tool.task)

        if tool.task == "update_patient_demographics":
            current = await self._read_current_patient(tool_context["patient_uuid"], client)
            if current is None:
                return TurnResult(
                    reply="Je n'ai pas pu relire la fiche du patient, je prefere ne rien modifier.",
                    state=STATE_FAILED,
                    task_type=tool.task,
                )
            tool_context["current_patient"] = current
            if not any(slots.get(field) for field in ("phone", "name")):
                state.draft_task = tool.task
                state.draft_slots = slots
                return TurnResult(
                    reply="Que faut-il modifier exactement (par exemple le telephone ou le nom) ?",
                    state=STATE_AWAITING_CLARIFICATION,
                    task_type=tool.task,
                )

        state.draft_task = None
        state.draft_slots = {}
        operations = tool.build(slots, tool_context)

        if not tool.writes:
            return await self._execute(operations, delegated_token, state.conversation_id, tool.task, prompt)

        summary = tool.summarise(slots, tool_context)
        if tool.task == "create_patient":
            warning = await self._duplicate_warning(slots, client)
            if warning:
                summary = warning + "\n\n" + summary

        pending = PendingAction(
            summary=summary,
            operations=[
                PendingOperation(op.method, op.path, op.body, op.summary, tool.task, op.body_from_results)
                for op in operations
            ],
            task_type=tool.task,
            username=user.username,
        )
        state.pending = pending
        return TurnResult(
            reply=summary,
            state=STATE_AWAITING_CONFIRMATION,
            task_type=tool.task,
            pending_action=_describe_pending(pending),
        )

    # ------------------------------------------------------------------ patient resolution

    async def _resolve_patient(self, slots, state, client) -> Dict[str, Any]:
        """Works out which patient a request is about, and refuses to pick when it cannot tell.

        A patient named in this turn wins over the one carried from the open dashboard or an
        earlier turn. The other order would let "affiche le dossier de Cherif", typed while
        Benali's chart is open, quietly answer about Benali.
        """
        if not slots.get("name") and not slots.get("identifier"):
            if state.last_patient_uuid:
                return {"context": {"patient_uuid": state.last_patient_uuid, "patient_label": ""}}
            return {
                "clarification": "De quel patient s'agit-il ? Donnez son nom ou son identifiant.",
                # Either form is acceptable here, so which slot the answer fills depends on its
                # shape. Recording it as "patient" rather than "name" is what lets a clinician
                # answer with an identifier - the obvious thing to do once a name proved ambiguous.
                "awaiting_slot": "patient",
            }

        query = (
            f"identifier={slots['identifier']}" if slots.get("identifier") else f"name={slots['name']}"
        )
        try:
            result = await client.call("GET", f"/ws/fhir2/R4/Patient?{query}&_count=10")
        except OpenmrsUnavailable:
            return {"error": "OpenMRS n'a pas repondu. Reessayez dans un instant."}

        if not result.ok:
            return {"error": explain_failure(result.status, result.body)}

        matches = _bundle_entries(result.body)
        if not matches:
            return {"error": "Aucun patient ne correspond a cette recherche dans OpenMRS."}
        if len(matches) > 1:
            listing = "\n".join(f"  - {_patient_label(entry)}" for entry in matches[:8])
            return {
                "clarification": "Plusieurs patients correspondent :\n" + listing
                + "\nPrecisez l'identifiant du patient concerne.",
                "awaiting_slot": "identifier",
            }

        patient = matches[0]
        return {"context": {"patient_uuid": patient.get("id"), "patient_label": _patient_label(patient)}}

    async def _read_current_patient(self, patient_uuid: str, client) -> Optional[Dict[str, Any]]:
        try:
            result = await client.call("GET", f"/ws/fhir2/R4/Patient/{patient_uuid}")
        except OpenmrsUnavailable:
            return None
        return result.body if result.ok and isinstance(result.body, dict) else None

    async def _duplicate_warning(self, slots, client) -> Optional[str]:
        """Surfaces possible duplicates before a create is confirmed (section 1.4).

        A warning, not a block: only the clinician can tell a genuine namesake from a duplicate.
        A failed search is not allowed to hold up the creation - it just means no warning.
        """
        try:
            result = await client.call("GET", f"/ws/fhir2/R4/Patient?name={slots['name']}&_count=5")
        except OpenmrsUnavailable:
            return None
        if not result.ok:
            return None
        matches = _bundle_entries(result.body)
        if not matches:
            return None
        listing = "\n".join(f"  - {_patient_label(entry)}" for entry in matches[:5])
        return (
            "ATTENTION : des dossiers portant un nom proche existent deja :\n"
            + listing
            + "\nVerifiez qu'il ne s'agit pas d'un doublon avant de confirmer."
        )

    # ------------------------------------------------------------------ execution

    async def _execute(
        self,
        operations: List[PlannedOperation],
        delegated_token: str,
        conversation_id: str,
        task_type: str,
        prompt: str,
        success_prefix: str = "",
    ) -> TurnResult:
        client = OpenmrsClient(delegated_token, conversation_id, task_type, prompt)
        results: List[ApiResult] = []
        executed: List[Dict[str, Any]] = []

        for operation in operations:
            try:
                body = operation.resolved_body([r.body for r in results])
            except Exception as exc:
                # A plan whose later step cannot be built from what the earlier step returned. The
                # write has not been sent, so say so rather than leaving the clinician guessing
                # whether something was half-saved.
                log.warning("Could not build the body for %s %s: %s", operation.method, operation.path, exc)
                return TurnResult(
                    reply="Echec : la reponse d'OpenMRS n'a pas permis de preparer l'operation suivante. "
                          "Rien n'a ete enregistre.",
                    state=STATE_FAILED,
                    task_type=task_type,
                    executed=executed,
                )

            try:
                result = await client.call(operation.method, operation.path, body)
            except OpenmrsUnavailable:
                return TurnResult(
                    reply="Echec : OpenMRS n'a pas repondu dans le delai imparti. "
                          + ("Rien n'a ete enregistre." if operation.writes else ""),
                    state=STATE_FAILED,
                    task_type=task_type,
                    executed=executed,
                )

            executed.append({"method": operation.method, "path": operation.path, "status": result.status})
            if not result.ok:
                # Stop at the first failure rather than pressing on: a later call in the same
                # plan usually depends on the one that just failed.
                return TurnResult(
                    reply="Echec : " + explain_failure(result.status, result.body),
                    state=STATE_FAILED,
                    task_type=task_type,
                    executed=executed,
                )
            results.append(result)

        reply = _render_results(task_type, results)
        if success_prefix:
            reply = f"{success_prefix} {reply}".strip()
        return TurnResult(reply=reply, state=STATE_ANSWERED, task_type=task_type, executed=executed)


# --------------------------------------------------------------------------- rendering


def _describe_pending(pending: PendingAction) -> Dict[str, Any]:
    return {
        "summary": pending.summary,
        "task_type": pending.task_type,
        "operations": [
            {"method": op.method, "path": op.path, "summary": op.summary} for op in pending.operations
        ],
    }


def _bundle_entries(body: Any) -> List[Dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    if body.get("resourceType") == "Patient":
        return [body]
    return [
        entry["resource"]
        for entry in body.get("entry", []) or []
        if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)
    ]


def _patient_label(patient: Dict[str, Any]) -> str:
    names = patient.get("name") or []
    label = ""
    if names:
        first = names[0]
        given = " ".join(first.get("given") or [])
        label = f"{first.get('family', '')} {given}".strip()
    identifiers = patient.get("identifier") or []
    identifier = identifiers[0].get("value") if identifiers else None
    birth = patient.get("birthDate")
    parts = [part for part in (label or "(sans nom)", identifier, birth) if part]
    return " - ".join(parts)


_BARE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{2,19}$")


def _bare_slot_answer(asked: str, prompt: str, already: Dict[str, Any]) -> Tuple[str, Optional[Any]]:
    """The value a one-line answer supplies for the slot that was asked about.

    ``extract_slots`` needs a cue word - "identifiant 10007F" - because in a full sentence a bare
    token is more likely to be a word than a value. In an answer to "Precisez l'identifiant" that
    reasoning inverts: the whole turn *is* the value. Prefer what extraction already found, and
    otherwise take the turn itself when it has the right shape.
    """
    answer = prompt.strip()

    if asked == "patient":
        # The question accepted either form ("Donnez son nom ou son identifiant"), so the shape of
        # the answer decides. Digits are what distinguishes "10007F" from a one-word surname.
        if already.get("identifier"):
            return "identifier", already["identifier"]
        if _BARE_IDENTIFIER_RE.match(answer) and any(char.isdigit() for char in answer):
            return "identifier", answer.upper()
        return "name", answer or None

    extracted = already.get(asked)
    if extracted:
        return asked, extracted
    if not answer:
        return asked, None
    if asked == "identifier":
        return asked, answer.upper() if _BARE_IDENTIFIER_RE.match(answer) else None
    if asked == "name":
        return asked, answer
    # Any other slot (a date, a gender, a phone number) has its own extractor, and guessing from
    # free text would be worse than asking the question again.
    return asked, None


def _render_results(task_type: str, results: List[ApiResult]) -> str:
    if not results:
        return "Operation effectuee."

    if task_type == "search_patient":
        matches = _bundle_entries(results[0].body)
        if not matches:
            return "Aucun patient ne correspond a cette recherche."
        listing = "\n".join(f"  - {_patient_label(entry)}" for entry in matches[:10])
        plural = "s" if len(matches) > 1 else ""
        return f"{len(matches)} patient{plural} trouve{plural} :\n{listing}"

    if task_type == "get_patient_summary":
        patient = results[0].body if isinstance(results[0].body, dict) else {}
        lines = [f"Patient : {_patient_label(patient)}"]
        gender = {"male": "masculin", "female": "feminin"}.get(patient.get("gender", ""), "non precise")
        lines.append(f"Sexe : {gender}")
        if patient.get("birthDate"):
            lines.append(f"Date de naissance : {patient['birthDate']}")
        if len(results) > 1:
            encounters = _bundle_entries(results[1].body)
            if encounters:
                lines.append(f"Derniers passages ({len(encounters)}) :")
                for encounter in encounters[:5]:
                    period = (encounter.get("period") or {}).get("start", "date inconnue")
                    kind = (encounter.get("type") or [{}])[0].get("text", "passage")
                    lines.append(f"  - {period} : {kind}")
            else:
                lines.append("Aucun passage enregistre.")
        return "\n".join(lines)

    if task_type == "create_patient":
        created = results[-1].body if isinstance(results[-1].body, dict) else {}
        # webservices.rest answers a create with {"uuid": ..., "display": "<identifier> - <name>"},
        # not a FHIR Patient - so the FHIR label reader finds no name and reports "(sans nom)" for a
        # patient that was created perfectly well. Prefer the display string, which already carries
        # the identifier the clinician needs in order to find the record again.
        display = created.get("display")
        if isinstance(display, str) and display.strip():
            return f"Le dossier a ete cree : {display.strip()}."
        label = _patient_label(created)
        if label and label != "(sans nom)":
            return f"Le dossier a ete cree : {label}."
        # Created, but the response said nothing usable about it. Say that rather than inventing a
        # name or implying the record is unidentifiable.
        return "Le dossier a ete cree."

    if task_type == "book_appointment":
        return "Le rendez-vous est enregistre dans OpenMRS."

    if task_type == "record_neuro_assessment":
        return "Le score a ete ajoute a l'historique du patient."

    if task_type == "update_patient_demographics":
        return "La fiche du patient a ete mise a jour."

    return "Operation effectuee."
