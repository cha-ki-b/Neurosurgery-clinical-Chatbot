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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .capabilities import registry as capability_registry
from .conversation import MAX_REPAIRS, PendingAction, PendingOperation, TaskFrame, store
from .dialogue import validation
from .dialogue.references import (
    FIELD_CHOICE,
    attempted_value,
    is_correction,
    is_repair,
    looks_like_a_person_name,
    readable_field,
    resolve_field,
    value_for_field,
)
from .nlu.base import (
    INTENT_CANCEL,
    INTENT_CONFIRM,
    INTENT_TASK,
    INTENT_UNSUPPORTED,
    TASK_LIST_PATIENTS,
    TASK_SEARCH_PATIENT,
    TASK_UPDATE_PATIENT,
    Interpretation,
)
from .nlu.rules import (
    BARE_IDENTIFIER_RE as _BARE_IDENTIFIER_RE,
    DELETION_REFUSAL,
    RuleBasedNlu,
    classify_answer,
    extract_slots,
    identifier_shaped as _identifier_shaped,
    impossible_dates_in,
    matches_a_task,
    reads_as_deletion,
)
from .openmrs_client import ApiResult, OpenmrsClient, OpenmrsUnavailable, explain_failure
from .phi import safe, safe_path, safe_slots
from .security import ActingUser
from .telemetry import telemetry
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

        self._tools = tool_registry or build_registry(settings.patientview_tools_enabled)
        self._nlu = nlu if nlu is not None else _build_nlu(self._tools)

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
        # One turn at a time per conversation. Everything below reads and writes the frame, and a
        # second turn arriving mid-flight would otherwise plan from a half-updated one.
        async with state.lock:
            return await self._handle_turn_locked(prompt, delegated_token, user, state, context)

    async def _handle_turn_locked(
        self,
        prompt: str,
        delegated_token: str,
        user: ActingUser,
        state,
        context: Dict[str, Any],
    ) -> TurnResult:
        if context.get("patient_uuid"):
            # The chart the clinician has open. No label comes with it, and none is invented: the
            # label is filled in when the patient is actually read, and until then a summary says
            # the uuid rather than a blank.
            state.remember_patient(context["patient_uuid"])

        if state.pending is not None:
            return await self._handle_pending_answer(prompt, delegated_token, user, state)

        # Deletion is refused by name, and refused *first*. Two reasons it cannot wait: the generic
        # fallback told the clinician their perfectly clear sentence was not understood, and an
        # unrecognised turn is indistinguishable from an answer to a pending question - so
        # "supprime tous les patients", typed while a create was half-finished, was absorbed into it
        # and never answered at all.
        if reads_as_deletion(prompt):
            state.close_frame()
            return TurnResult(reply=DELETION_REFUSAL, state=STATE_UNSUPPORTED)

        interpretation = await self._interpret_with_carryover(prompt, state)

        if interpretation.intent == INTENT_UNSUPPORTED or interpretation.task is None:
            state.close_frame()
            return TurnResult(
                # The interpreter is expected to always produce a contextual reply for this case
                # (a welcome for small talk, a reasoned refusal otherwise) - this generic text is
                # only the last-resort safety net for the rare turn where it produced none at all.
                reply=interpretation.clarification or (
                    "Je peux rechercher un patient, afficher son dossier, en creer ou mettre a "
                    "jour un, noter un score neurologique, ou programmer un rendez-vous. Que "
                    "souhaitez-vous faire ?"
                ),
                state=STATE_UNSUPPORTED,
            )

        # Availability is settled before any question is asked. Asking a clinician for an appointment
        # date, and then refusing the booking a turn later because this installation has no
        # Appointment resource at all, wastes their time and reads as the assistant changing its mind.
        # The refusal used to fire correctly for exactly this case; it stopped once the interpreter
        # started producing a clarification for that phrasing, because the clarification was returned
        # first.
        unavailable = self._unavailable_reason(interpretation.task)
        if unavailable is not None:
            state.close_frame()
            return TurnResult(reply=unavailable, state=STATE_UNSUPPORTED, task_type=interpretation.task)

        if interpretation.needs_clarification:
            # Hold on to what was understood so the answer to the question completes the request
            # instead of restarting it. Which slot the answer belongs in is recorded too, when it
            # can be worked out from the tool's own required slots - this clarification is asked
            # before the request is ever handed to the tool layer, so this is the only place that
            # knows both the task and what it is still missing (Finding 29).
            frame = state.open_frame(interpretation.task, interpretation.slots)
            frame.awaiting = self._primary_missing_slot(interpretation.task, frame.slots)
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
            state.close_frame()
            return TurnResult(reply="C'est annule, rien n'a ete enregistre.", state=STATE_CANCELLED,
                              task_type=pending.task_type)

        if answer != INTENT_CONFIRM:
            # A deletion request typed while a confirmation is outstanding is still a deletion
            # request, and answering it with "repondez oui ou non" makes a perfectly clear sentence
            # disappear. Nothing is cancelled - the pending write is left exactly as it was and
            # shown again - because a refusal is not a reason to discard something already approved
            # in words.
            if reads_as_deletion(prompt):
                return TurnResult(
                    reply=DELETION_REFUSAL + "\n\nVotre demande precedente est toujours en attente :\n"
                          + pending.summary,
                    state=STATE_AWAITING_CONFIRMATION,
                    task_type=pending.task_type,
                    pending_action=_describe_pending(pending),
                )

            # "en fait, mets plutot 0666777888" revises the write that is waiting rather than
            # answering yes or no to it. Before this, the only readings available were yes, no and
            # "je n'ai pas compris votre reponse", so a correction at the last moment forced the
            # clinician to cancel and retype the whole request.
            amendment = self._pending_amendment(prompt, state, pending)
            if amendment:
                store.clear_pending(state.conversation_id)
                frame = state.frame
                frame.slots.update(amendment)
                frame.awaiting = None
                log.info("Amending the pending %s: %s", pending.task_type, sorted(amendment))
                # Re-planned from scratch, not patched: the new values go through the same
                # validation and produce a fresh summary, which is shown and waited on again.
                # An amendment can never be the thing that executes a write.
                return await self._plan_and_maybe_execute(
                    prompt, delegated_token, user, state,
                    Interpretation(intent=INTENT_TASK, task=frame.task, slots=dict(frame.slots)),
                )

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
        result = await self._execute(
            [
                PlannedOperation(op.method, op.path, op.body, op.summary, op.body_from_results)
                for op in pending.operations
            ],
            delegated_token,
            state.conversation_id,
            pending.task_type,
            prompt,
            success_prefix="C'est enregistre.",
            state=state,
            verification=pending.verification,
        )
        if result.state == STATE_ANSWERED:
            # The request is done, so it stops being the request in progress. Left open, a stray
            # value in the next turn ("0666") would be absorbed into the finished task and planned
            # as a second write - it would still need confirming, so nothing unsafe, but it is not
            # what the clinician asked for. A *failed* write keeps its frame, so a correction costs
            # one turn instead of retyping the whole thing.
            state.close_frame()
        return result

    def _pending_amendment(self, prompt: str, state, pending) -> Optional[Dict[str, Any]]:
        """The values this turn revises in the write that is waiting, or None.

        Deliberately narrow: only a value the turn actually supplies, for a field the pending task
        already has or can write. A turn that merely mentions something is not an amendment, and
        an amendment never executes anything - it re-opens the confirmation gate with a new
        summary, which the clinician still has to approve.
        """
        frame = state.frame
        if frame is None or frame.task != pending.task_type:
            return None
        tool = self._tool_for(frame.task)
        if tool is None:
            return None

        changed: Dict[str, Any] = {}

        if tool.updatable_fields:
            named = resolve_field(prompt, tool.updatable_fields)
            if named.resolved:
                value = value_for_field(prompt, named.field)
                if value is not None:
                    changed[named.field] = value

        if not changed and frame.active_field:
            value = value_for_field(prompt, frame.active_field)
            if value is not None:
                changed[frame.active_field] = value

        if not changed:
            # A slot the frame already holds, restated with a different value: "non, 07/11/1965".
            for slot, value in extract_slots(prompt).items():
                if slot in frame.slots and frame.slots[slot] != value:
                    changed[slot] = value

        return changed or None

    # ------------------------------------------------------------------ planning

    def _unavailable_reason(self, task: Optional[str]) -> Optional[str]:
        """The reason this deployment cannot do the task, or None if it can.

        Read from the live capability statement, never from a list written down here (ADR-10).
        """
        if task is None:
            return None
        availability = self._tools.for_task(task, capability_registry.current)
        if availability is None:
            return "Cette action ne fait pas partie de ce que je sais faire."
        if not availability.available:
            return f"Je ne peux pas effectuer cette action ici. {availability.reason}"
        return None

    def _primary_missing_slot(self, task: Optional[str], slots: Dict[str, Any]) -> Optional[str]:
        """The one slot a reply to this clarification most likely fills, or None if that is unknowable.

        Reuses the tool's own ``required_slots`` - the same list the gap-check further down the
        pipeline asks against - rather than a second, hand-maintained notion of what a task needs.
        None when the task itself is what is ambiguous (two task families matched) or every
        required slot is already filled and something else prompted the question: there is no
        single slot to aim a bare reply at, so none is recorded rather than guessed.
        """
        if task is None:
            return None
        availability = self._tools.for_task(task, capability_registry.current)
        if availability is None:
            return None
        gaps = missing_slots(availability.tool, slots)
        return gaps[0] if gaps else None

    def _interpreter_context(self, state) -> Dict[str, Any]:
        """What the interpreter is told about the conversation so far.

        This argument existed on :class:`app.nlu.base.NluEngine` from the start and was passed an
        empty dict on every single turn, so the interpreter - rules or model - has never once seen
        the conversation it is interpreting. Every follow-up was read as if it were the first thing
        the clinician had said.

        It is context, not authority. Nothing here can fill a slot or advance a task on its own:
        the frame does that, in code, below.
        """
        frame = state.frame
        return {
            "active_task": frame.task if frame else None,
            "active_field": frame.active_field if frame else None,
            "awaiting": frame.awaiting if frame else None,
            "known_slots": dict(frame.slots) if frame else {},
            "active_patient": state.last_patient_label or state.last_patient_uuid,
        }

    async def _interpret_with_carryover(self, prompt: str, state) -> Interpretation:
        # An engine that talks to a model exposes ``ainterpret``; calling its sync ``interpret``
        # would block the event loop for the length of a GPU call. The rules engine has only the
        # sync form, and needs no thread for a handful of regexes.
        context = self._interpreter_context(state)
        if hasattr(self._nlu, "ainterpret"):
            interpretation = await self._nlu.ainterpret(prompt, context)
        else:
            interpretation = self._nlu.interpret(prompt, context)

        frame = state.frame
        if frame is None:
            return interpretation

        # A turn that carries the vocabulary of a *different* task family really is a new request,
        # and takes over. The deterministic matcher decides this, not the model: asked with no
        # memory of the question, a model reads a bare name ("Nadia Belkacem") as a confident fresh
        # search and silently abandons a half-finished create (Finding 30).
        if (
            interpretation.intent == INTENT_TASK
            and interpretation.task
            and interpretation.task != frame.task
            and matches_a_task(prompt)
        ):
            log.info("Switching from %s to %s: the turn names a different task (%s)",
                     frame.task, interpretation.task, safe(prompt))
            state.close_frame()
            return interpretation

        return self._absorb_into_frame(prompt, state, frame, interpretation)

    # ------------------------------------------------------------------ slot filling

    def _absorb_into_frame(self, prompt: str, state, frame: TaskFrame, interpretation: Interpretation) -> Interpretation:
        """Reads this turn as a contribution to the request already in progress.

        This is where the redesign lives. The old code had one question - "does this turn fill the
        exact slot I asked about?" - and threw the whole request away whenever the answer was no.
        Three ordinary things a clinician says answer "no" to that question and are not failures:
        naming a field without its value ("le telephone"), supplying a value for a field named a
        turn ago ("change it to 06564565"), and saying the question was already answered.

        So the turn is offered to each of those in turn, and only a turn that contributes nothing
        at all - and is not conversational repair - abandons anything.
        """
        tool = self._tool_for(frame.task)
        filled: List[str] = []

        # 1. The answer to "which field should I change?". Naming a field is a complete answer;
        #    the value is a separate question, asked next.
        if frame.awaiting == FIELD_CHOICE and tool is not None:
            resolution = resolve_field(prompt, tool.updatable_fields)
            if resolution.ambiguous:
                return Interpretation(
                    intent=INTENT_TASK,
                    task=frame.task,
                    slots=dict(frame.slots),
                    clarification=(
                        "Vous parlez de "
                        + " ou de ".join(readable_field(name) for name in resolution.ambiguous)
                        + " ? Precisez lequel."
                    ),
                )
            if resolution.resolved:
                frame.active_field = resolution.field
                frame.awaiting = None
                filled.append(resolution.field)

        # 2. A value for the field that is active. A different field named in this turn wins over
        #    the active one - "actually change the name instead" must not write to the phone.
        if frame.active_field and tool is not None:
            named = resolve_field(prompt, tool.updatable_fields)
            target = named.field or frame.active_field
            value = value_for_field(prompt, target, whole_turn_is_value=(frame.awaiting == target))
            if value is not None:
                frame.slots[target] = value
                frame.active_field = target
                if frame.awaiting == target:
                    frame.awaiting = None
                filled.append(target)
            elif named.resolved and named.field != frame.active_field:
                # The field was changed but no value came with it: ask for that field's value
                # rather than carrying the old field's.
                frame.slots.pop(frame.active_field, None)
                frame.active_field = named.field
                filled.append(named.field)

        # 3. The answer to a specific slot question. Kept from the original design, including its
        #    right to *replace* a value already present: a clarification asked to narrow a name
        #    down to an identifier can only be answered if the stale name stops winning.
        if not filled and frame.awaiting and frame.awaiting != FIELD_CHOICE:
            merged = dict(frame.slots)
            merged.update(extract_slots(prompt))
            slot, answer = _bare_slot_answer(frame.awaiting, prompt, merged)
            if answer is not None:
                frame.slots[slot] = answer
                if slot == "identifier":
                    # Searching by identifier is exact; leaving the ambiguous name would reproduce
                    # the ambiguity that prompted the question.
                    frame.slots.pop("name", None)
                frame.awaiting = None
                filled.append(slot)

        # 4. Anything else the turn plainly states, filling gaps only. A value already established
        #    is not overwritten by a passing mention - that is what step 3 is for.
        for slot, value in extract_slots(prompt).items():
            if slot not in frame.slots or not frame.slots.get(slot):
                frame.slots[slot] = value
                filled.append(slot)

        # 5. The interpreter's own reading, for the same task, filling gaps only. Its name is
        #    checked against the sentence: a pronoun is not a patient (Finding 38).
        if interpretation.intent == INTENT_TASK and interpretation.task == frame.task:
            for slot, value in interpretation.slots.items():
                if slot == "name" and not looks_like_a_person_name(value):
                    continue
                if slot not in frame.slots or not frame.slots.get(slot):
                    frame.slots[slot] = value
                    filled.append(slot)

        if filled:
            frame.note_answer()
            log.info("Frame %s advanced: filled %s, now holding %s",
                     frame.task, sorted(set(filled)), safe_slots(frame.slots))
            return Interpretation(intent=INTENT_TASK, task=frame.task, slots=dict(frame.slots))

        # --- nothing was filled ---------------------------------------------------------------

        # A date-shaped token that names no real day is a *given* answer, not a missing one. Saying
        # so beats repeating the question unchanged, which reads as not having listened (Finding 37).
        # A value the clinician evidently *did* give, which simply cannot be used. Explaining why
        # keeps the request alive and costs one turn; abandoning it costs everything typed so far.
        if frame.awaiting and frame.awaiting != FIELD_CHOICE:
            attempt = attempted_value(prompt, frame.awaiting)
            if attempt is not None:
                problem = validation.check_slot(frame.awaiting, attempt)
                if problem is not None:
                    frame.note_non_answer()
                    return Interpretation(
                        intent=INTENT_TASK,
                        task=frame.task,
                        slots=dict(frame.slots),
                        clarification=problem.message,
                    )

        impossible = impossible_dates_in(prompt)
        if impossible and frame.awaiting in ("birthdate", "dates"):
            frame.note_non_answer()
            return Interpretation(
                intent=INTENT_TASK,
                task=frame.task,
                slots=dict(frame.slots),
                clarification=(
                    f"« {impossible[0]} » n'est pas une date valide. Donnez-la au format "
                    "JJ/MM/AAAA, par exemple 20/09/2008."
                ),
            )

        # Conversational repair: the clinician is telling us the question was already answered or
        # was not understood. Repeating it verbatim is what made the assistant feel deaf; saying
        # what is already held, and asking only for what is left, answers the actual complaint.
        if is_repair(prompt) or is_correction(prompt):
            telemetry.record("frame.repair")
            if frame.note_non_answer() <= MAX_REPAIRS:
                return Interpretation(
                    intent=INTENT_TASK,
                    task=frame.task,
                    slots=dict(frame.slots),
                    clarification=self._repair_question(frame, tool),
                )

        log.info("Abandoning %s: the turn (%s) does not answer the pending question",
                 frame.task, safe(prompt))
        telemetry.record("frame.abandoned")
        telemetry.record(f"frame.abandoned.{frame.task}")
        state.close_frame()
        return Interpretation(
            intent=INTENT_UNSUPPORTED,
            clarification=(
                "J'abandonne la demande precedente, votre message ne repondait pas a ma "
                "question. " + (interpretation.clarification or
                                "Reformulez ce que vous souhaitez faire.")
            ),
        )

    def _repair_question(self, frame: TaskFrame, tool: Optional[ToolSpec]) -> str:
        """What to say when the clinician tells us we already have the answer.

        States what is held, then asks for exactly one missing thing - never the whole request
        again, and never the same sentence twice in a row.
        """
        known = frame.known_summary()
        preface = f"Voici ce que j'ai deja note - {known}. " if known else ""

        if frame.awaiting and frame.awaiting != FIELD_CHOICE and tool is not None:
            question = tool.slot_questions.get(frame.awaiting, f"Il me manque : {frame.awaiting}.")
            return preface + "Il me manque encore une chose. " + question
        if frame.awaiting == FIELD_CHOICE and tool is not None:
            return preface + self._field_question(tool)
        if tool is not None:
            gaps = missing_slots(tool, frame.slots)
            if gaps:
                return preface + tool.slot_questions.get(gaps[0], f"Il me manque : {gaps[0]}.")
        return preface + "Que souhaitez-vous que je fasse exactement ?"

    def _field_question(self, tool: ToolSpec) -> str:
        """"What should I change?", listing only the fields this tool can actually write."""
        options = " ou ".join(readable_field(name) for name in tool.updatable_fields)
        return f"Que faut-il modifier : {options} ?" if options else "Que faut-il modifier exactement ?"

    def _tool_for(self, task: Optional[str]) -> Optional[ToolSpec]:
        if task is None:
            return None
        availability = self._tools.for_task(task, capability_registry.current)
        return availability.tool if availability is not None else None

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
        # The frame is the working copy from here on. Opening it (rather than passing a loose dict
        # around) is what lets a question asked below be answered on the *next* turn without the
        # request having to be reassembled from scratch.
        frame = state.open_frame(tool.task, interpretation.slots)
        slots = frame.slots
        tool_context: Dict[str, Any] = {}
        client = OpenmrsClient(delegated_token, state.conversation_id, tool.task, prompt)

        # A write the user cannot have executed is refused before anything is planned, so no
        # summary is ever shown for an action that could not have happened anyway.
        if tool.writes and not user.may_write:
            state.close_frame()
            return TurnResult(
                reply="Votre compte permet uniquement les consultations. Je ne peux rien enregistrer "
                      "en votre nom.",
                state=STATE_FAILED,
                task_type=tool.task,
            )

        if tool.requires_patient:
            # The slot being changed holds the *new* value, never a way to find the patient. Without
            # this, "modifie le patient X" / "le nom" / "Walter Black" searched OpenMRS for a patient
            # called Walter Black and reported that no such patient exists - Finding 28's collision
            # in the opposite direction, and invisible until a rename was tried end to end.
            patient_slots = dict(slots)
            if frame.active_field:
                patient_slots.pop(frame.active_field, None)
            resolution = await self._resolve_patient(patient_slots, state, client)
            if resolution.get("clarification"):
                frame.awaiting = resolution.get("awaiting_slot")
                return TurnResult(reply=resolution["clarification"], state=STATE_AWAITING_CLARIFICATION,
                                  task_type=tool.task)
            if resolution.get("error"):
                return TurnResult(reply=resolution["error"], state=STATE_FAILED, task_type=tool.task)
            tool_context.update(resolution["context"])
            state.remember_patient(tool_context["patient_uuid"], tool_context.get("patient_label"))
            # The label the summary will show comes from the frame, so a patient identified three
            # turns ago is still named by name when the write is finally confirmed.
            tool_context["patient_label"] = tool_context.get("patient_label") or frame.patient_label or ""

            if (
                tool.task == TASK_UPDATE_PATIENT
                and slots.get("name")
                and not slots.get("identifier")
                and not _identifier_shaped(slots.get("name"))
                and frame.active_field != "name"
            ):
                # The name found *which* patient this is about; update_patient_demographics reads
                # that very same slot as "the new name to write" downstream, and nothing before
                # this point distinguishes the two. That collision is the structural cause of
                # Finding 28: a reply that only said who to update ("nom", or a name search that
                # had to fall back to disambiguation) was, once, indistinguishable from an
                # instruction to rename them. It has now done its job of finding the patient, so
                # it is dropped before it can also be read as a value to write.
                #
                # The active field is now the exception that makes a rename possible at all: when
                # the clinician has explicitly said "change the name" and then given one, the value
                # is a new name by construction, not a search term.
                slots.pop("name", None)

        gaps = missing_slots(tool, slots)
        if gaps:
            frame.awaiting = gaps[0]
            question = tool.slot_questions.get(gaps[0], f"Il me manque : {gaps[0]}.")
            return TurnResult(reply=question, state=STATE_AWAITING_CLARIFICATION, task_type=tool.task)

        # A field-changing tool needs to know *which* field before it needs a value, and those are
        # two separate questions. Asking for both at once - "precisez le champ et la nouvelle
        # valeur ensemble" - is what made "le telephone" an unanswerable reply and destroyed the
        # request (Finding 39). The vocabulary offered here and the values accepted both come from
        # the tool's own ``updatable_fields``, so no field is ever offered that cannot be written.
        if tool.updatable_fields:
            current = await self._read_current_patient(tool_context["patient_uuid"], client)
            if current is None:
                return TurnResult(
                    reply="Je n'ai pas pu relire la fiche du patient, je prefere ne rien modifier.",
                    state=STATE_FAILED,
                    task_type=tool.task,
                )
            tool_context["current_patient"] = current

            if not tool_context.get("patient_label"):
                # The patient came from the open chart, which carries a uuid and no name. The
                # record has just been read, so the name is in hand - and a confirmation summary
                # for a write must name the person it will change.
                label = _patient_label(current)
                if label and label != "(sans nom)":
                    tool_context["patient_label"] = label
                    state.remember_patient(tool_context["patient_uuid"], label)

            changing = [name for name in tool.updatable_fields if slots.get(name)]
            if not changing:
                if frame.active_field:
                    # The field is settled; only its value is outstanding. Ask for exactly that.
                    frame.awaiting = frame.active_field
                    question = tool.slot_questions.get(
                        frame.active_field,
                        f"Quelle est la nouvelle valeur pour {readable_field(frame.active_field)} ?",
                    )
                    return TurnResult(reply=question, state=STATE_AWAITING_CLARIFICATION, task_type=tool.task)
                frame.awaiting = FIELD_CHOICE
                return TurnResult(
                    reply=self._field_question(tool),
                    state=STATE_AWAITING_CLARIFICATION,
                    task_type=tool.task,
                )
            if frame.active_field is None and len(changing) == 1:
                frame.active_field = changing[0]

        # Values are checked here, before a summary exists - so the clinician is never shown, and
        # never approves, something the database cannot hold. The frame survives the question, so
        # correcting one bad value costs one turn instead of re-entering the whole request
        # (Finding 37).
        problem = validation.first_problem(slots)
        if problem is not None:
            slots.pop(problem.slot, None)
            frame.awaiting = problem.slot
            log.info("Rejecting slot %s for %s: it failed validation", problem.slot, tool.task)
            telemetry.record(f"slot.rejected.{problem.slot}")
            return TurnResult(reply=problem.message, state=STATE_AWAITING_CLARIFICATION, task_type=tool.task)

        operations = tool.build(dict(slots), tool_context)

        if not tool.writes:
            read_slots = dict(slots)
            state.close_frame()
            return await self._execute(
                operations, delegated_token, state.conversation_id, tool.task, prompt,
                state=state, slots=read_slots,
            )

        summary = tool.summarise(dict(slots), tool_context)
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
            verification=tool.verify(dict(slots), tool_context) if tool.verify else None,
        )
        state.pending = pending
        # The frame is deliberately *not* closed while a confirmation is outstanding: "en fait,
        # mets plutot 0666777888" amends the pending write instead of restarting the request, and
        # a write OpenMRS refuses leaves everything already established still in hand.
        frame.awaiting = None
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
                # The label travels with the uuid. Returning "" here is what produced "Je vais
                # MODIFIER la fiche du patient  :" - a change approved without the clinician being
                # shown whose record it lands in (Finding 40).
                return {
                    "context": {
                        "patient_uuid": state.last_patient_uuid,
                        "patient_label": state.last_patient_label or "",
                    }
                }
            return {
                "clarification": "De quel patient s'agit-il ? Donnez son nom ou son identifiant.",
                # Either form is acceptable here, so which slot the answer fills depends on its
                # shape. Recording it as "patient" rather than "name" is what lets a clinician
                # answer with an identifier - the obvious thing to do once a name proved ambiguous.
                "awaiting_slot": "patient",
            }

        # A token that looks like a record number is searched as an identifier, not as a name. The
        # clinician typing "le patient 1000C6" means the identifier, and FHIR's `name` parameter can
        # never match one - it returned nothing and the assistant reported the patient as unknown.
        identifier = slots.get("identifier") or _identifier_shaped(slots.get("name"))
        query = f"identifier={identifier}" if identifier else f"name={slots['name']}"
        try:
            result = await client.call("GET", f"/ws/fhir2/R4/Patient?{query}&_count=10")
        except OpenmrsUnavailable:
            return {"error": "OpenMRS n'a pas repondu. Reessayez dans un instant."}

        if not result.ok:
            return {"error": explain_failure(result.status, result.body, searching=True)}

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
        state: Optional[Any] = None,
        slots: Optional[Dict[str, Any]] = None,
        verification: Optional[Any] = None,
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
                log.warning("Could not build the body for %s %s: %s", operation.method,
                            safe_path(operation.path), type(exc).__name__)
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
                searching = operation.method.upper() == "GET" and "?" in operation.path
                return TurnResult(
                    reply="Echec : " + explain_failure(result.status, result.body, searching=searching),
                    state=STATE_FAILED,
                    task_type=task_type,
                    executed=executed,
                )
            results.append(result)

        if state is not None and task_type == TASK_SEARCH_PATIENT and results:
            # A search that lands on exactly one patient is the natural antecedent for "son
            # dossier" or "son telephone" in the very next turn. Without this, search_patient's
            # requires_patient=False meant last_patient_uuid was never set by a search at all, so
            # the clinician's most natural follow-up always asked "de quel patient s'agit-il ?"
            # (Finding 32).
            matches = _bundle_entries(results[0].body)
            if len(matches) == 1:
                state.remember_patient(matches[0].get("id"), _patient_label(matches[0]))

        if verification is not None:
            outcome = await self._verify_write(verification, client, executed, [r.body for r in results])
            if outcome is not None:
                message, verdict = outcome
                if verdict == STATE_FAILED:
                    return TurnResult(reply=message, state=STATE_FAILED, task_type=task_type, executed=executed)
                # A warning rides along with the ordinary success report: what was done, then what
                # to check about it.
                reply = _render_results(task_type, results, slots or {})
                if success_prefix:
                    reply = f"{success_prefix} {reply}".strip()
                return TurnResult(reply=f"{reply}\n\n{message}", state=STATE_ANSWERED,
                                  task_type=task_type, executed=executed)

        reply = _render_results(task_type, results, slots or {})
        if success_prefix:
            reply = f"{success_prefix} {reply}".strip()
        return TurnResult(reply=reply, state=STATE_ANSWERED, task_type=task_type, executed=executed)

    async def _verify_write(self, verification, client, executed, bodies) -> Optional[Tuple[str, str]]:
        """Reads the record back and reports what is actually in it, or None if all is well.

        Three outcomes, and the middle one is the reason this exists at all:

        * the value is there - say nothing, the caller reports success as before;
        * OpenMRS accepted the write and the value is **not** there - a silent no-op, reported as
          the failure it is rather than as "c'est enregistre";
        * the record could not be re-read - the write may well have worked, so this neither claims
          success nor invents a failure; it says plainly that it could not check.
        """
        operation = verification.plan(bodies)
        if operation is None:
            # The write returned nothing that identifies a record to re-read. Nothing is claimed
            # about verification either way.
            return None

        try:
            result = await client.call(operation.method, operation.path)
        except OpenmrsUnavailable:
            result = None

        if result is None or not result.ok:
            return (
                "OpenMRS a accepte la modification, mais je n'ai pas pu relire la fiche pour le "
                "verifier. Verifiez la valeur dans le dossier avant de vous y fier.",
                STATE_ANSWERED,
            )

        executed.append({"method": operation.method, "path": operation.path, "status": result.status})

        reason = verification.confirm(result.body)
        if reason is None:
            return None

        if verification.on_mismatch == "warn":
            # ``reason`` is written for the clinician and names the values - "la date de naissance
            # enregistree est 2008-09-19, et non 2008-09-20" - so it is patient data and only its
            # shape goes to the log. The clinician still gets the whole sentence in the reply,
            # which goes to an authenticated person rather than to a file on this host.
            log.warning("Write applied but a value differs (%s)", safe(reason))
            telemetry.record("write.value_differs")
            return (
                f"ATTENTION : {reason}. Le dossier existe bien - ne le recreez pas - mais corrigez "
                "cette valeur dans OpenMRS.",
                STATE_ANSWERED,
            )

        log.warning("Write accepted but not applied (%s)", safe(reason))
        telemetry.record("write.accepted_but_not_applied")
        return (
            f"Echec : OpenMRS a accepte la demande mais {reason}. Rien n'a change ; "
            "signalez-le a l'administrateur plutot que de reessayer.",
            STATE_FAILED,
        )


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




def _build_nlu(registry: ToolRegistry):
    """The interpreter this deployment is configured for.

    Defaults to the deterministic engine. A deployment that has not stood up a GPU keeps working
    rather than failing every turn, and switching is one environment variable - which also makes
    "is it the model?" answerable in one restart when a turn is read wrongly.
    """
    from .config import settings

    if settings.nlu_engine == "medgemma":
        from .nlu.medgemma import MedGemmaNlu

        log.info("Interpretation: MedGemma at %s (falling back to rules on failure)", settings.llm_base_url)
        return MedGemmaNlu(registry, fallback=RuleBasedNlu())

    if settings.nlu_engine not in ("rules", ""):
        log.warning("Unknown NLU_ENGINE %r; using the rules engine", settings.nlu_engine)
    log.info("Interpretation: deterministic rules engine")
    return RuleBasedNlu()


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
    if asked == "birthdate" and already.get("dates"):
        # The extractor only fills `birthdate` when the sentence carries a birth cue ("ne le") -
        # right for an ordinary sentence, wrong here, because the question just asked *is* that
        # cue. A bare date in reply to "quelle est la date de naissance ?" needs no cue of its
        # own; the question already supplied it (Finding 30).
        return asked, already["dates"][0]
    # Any other slot (a date, a gender, a phone number) has its own extractor, and guessing from
    # free text would be worse than asking the question again.
    return asked, None


def _bundle_total(body: Any, shown: int) -> Optional[int]:
    """How many records actually matched, as FHIR reports it - not how many came back.

    A bundle is a *page*. The renderer used to count the entries it received and present that as
    the answer, so a query capped at ``_count=50`` reported "50 patients trouves" for a database
    holding any number more than fifty. A count a clinician cannot rely on is worse than no count.
    """
    if isinstance(body, dict) and isinstance(body.get("total"), int):
        return body["total"]
    return None


def _count_line(total: Optional[int], shown: int) -> str:
    if total is None or total == shown:
        plural = "s" if shown > 1 else ""
        return f"{shown} patient{plural} trouve{plural} :"
    plural = "s" if total > 1 else ""
    return f"{total} patient{plural} trouve{plural}, les {shown} premiers :"


def _applied_filter(slots: Dict[str, Any]) -> str:
    """What the query actually filtered on, so an answer cannot be read as narrower than it is.

    Measured: "how many patients got created today" produced the whole patient list with no
    qualification at all, which reads as an answer to the question that was asked. No tool here can
    filter by creation date, so the honest reply names the filter that *was* applied and lets the
    clinician see it is not the one they meant.
    """
    applied = []
    if slots.get("gender"):
        applied.append("sexe " + {"M": "masculin", "F": "feminin"}.get(slots["gender"], slots["gender"]))
    if slots.get("since"):
        # Deliberately "modifies", not "crees". OpenMRS offers no creation-date search parameter,
        # and answering a question about creation with a count of modifications - silently - is the
        # failure this whole disclosure exists to prevent.
        applied.append(f"dossiers modifies depuis le {slots['since']} (OpenMRS ne permet pas de "
                       "filtrer sur la date de creation)")
    if applied:
        return " (filtre : " + ", ".join(applied) + ")"
    return " (aucun filtre : la liste complete)"


def _render_results(task_type: str, results: List[ApiResult], slots: Optional[Dict[str, Any]] = None) -> str:
    slots = slots or {}
    if not results:
        return "Operation effectuee."

    if task_type == "search_patient":
        matches = _bundle_entries(results[0].body)
        if not matches:
            return "Aucun patient ne correspond a cette recherche."
        listing = "\n".join(f"  - {_patient_label(entry)}" for entry in matches[:10])
        shown = min(len(matches), 10)
        return f"{_count_line(_bundle_total(results[0].body, len(matches)), shown)}\n{listing}"

    if task_type == TASK_LIST_PATIENTS:
        matches = _bundle_entries(results[0].body)
        if not matches:
            return "Aucun patient ne correspond a ce filtre."
        listing = "\n".join(f"  - {_patient_label(entry)}" for entry in matches[:20])
        shown = min(len(matches), 20)
        header = _count_line(_bundle_total(results[0].body, len(matches)), shown)
        return f"{header[:-1]}{_applied_filter(slots)} :\n{listing}"

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
