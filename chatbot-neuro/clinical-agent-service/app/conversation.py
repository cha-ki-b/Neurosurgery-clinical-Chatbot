"""The short-lived conversation buffer.

It exists for exactly two things: remembering what the assistant asked a clarifying question
about, and holding a pending write between the moment its summary is shown and the moment the
clinician confirms it. Nothing is persisted, entries expire, and the durable record of what
actually happened is written on the OpenMRS side (§1.3) - this process is not a system of record
and must not become one.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional  # noqa: F401  (List used in PendingAction)

from .config import settings


@dataclass
class PendingOperation:
    """One concrete call that has been described to the clinician and is awaiting a yes."""

    method: str
    path: str
    body: Optional[Dict[str, Any]]
    summary: str
    task_type: str
    # Carried through the wait for confirmation, not just the body. A plan whose later call needs a
    # value from an earlier one (creating a patient reserves an identifier first) would otherwise
    # lose that builder while parked here and be sent with an empty body once the clinician says
    # yes - a write that fails after they approved it, which is the worst moment to fail.
    body_from_results: Optional[Callable[[List[Any]], Dict[str, Any]]] = None


@dataclass
class PendingAction:
    summary: str
    operations: List[PendingOperation]
    task_type: str
    # The user this was summarised for. A pending action is only ever executable by the same
    # clinician who was shown it, even if someone else guesses the conversation id.
    username: str
    # The read that proves the write landed, built at the same moment as the write itself so it
    # checks exactly what the clinician approved rather than whatever the frame holds a turn later.
    verification: Optional[Any] = None


@dataclass
class TaskFrame:
    """The request currently being assembled, and everything already established about it.

    This is the structured state the redesign turns on. Before it, "what are we doing" lived in
    three loose fields that any turn failing to answer the pending question would clear - so a
    clinician who typed "le telephone", naming the field but not yet its value, destroyed a
    request that was one turn from complete, and a clinician who typed "je te l'ai deja dit" threw
    away three turns of a create.

    The frame is owned by application code and is the only authority on task, slots and field.
    The interpreter may propose values for it; it never *is* it.
    """

    task: str
    slots: Dict[str, Any] = field(default_factory=dict)
    # The slot the last question was about, so its answer lands there and may *replace* what is
    # already present. ``references.FIELD_CHOICE`` means the question was "which field?", whose
    # answer fills ``active_field`` rather than a slot.
    awaiting: Optional[str] = None
    # For tasks that change one field of an existing record: which field. Naming a field is a
    # complete answer on its own - the value is then asked for separately - and it is what "it",
    # "that" and "make it 42" resolve against on the following turn.
    active_field: Optional[str] = None
    # Which patient this request is about, resolved once and carried. The label is kept alongside
    # the uuid because a confirmation summary that names no patient ("Je vais MODIFIER la fiche du
    # patient  :") is a write approved blind.
    patient_uuid: Optional[str] = None
    patient_label: Optional[str] = None
    # Consecutive turns that answered nothing. Bounded so a misunderstanding cannot loop forever,
    # but non-zero so one confused turn does not discard an almost-complete request.
    repairs: int = 0

    def note_answer(self) -> None:
        self.repairs = 0

    def note_non_answer(self) -> int:
        self.repairs += 1
        return self.repairs

    def known_summary(self) -> str:
        """What is already established, in the clinician's words - for a repair turn.

        Answering "je te l'ai deja dit" by repeating the question unchanged is what made the
        assistant feel like it was not listening. Saying what it holds proves it was.
        """
        parts = []
        if self.patient_label:
            parts.append(f"patient : {self.patient_label}")
        for slot, value in sorted(self.slots.items()):
            if value in (None, "", []):
                continue
            parts.append(f"{slot} : {value}")
        return ", ".join(parts)


MAX_REPAIRS = 2


@dataclass
class ConversationState:
    conversation_id: str
    username: str
    pending: Optional[PendingAction] = None
    last_patient_uuid: Optional[str] = None
    last_patient_label: Optional[str] = None
    # The request in progress, or None when the assistant is not in the middle of anything.
    frame: Optional[TaskFrame] = None
    updated_at: float = field(default_factory=time.time)
    # Held for the length of a turn. The store's own lock protects the *dictionary*; it is released
    # the moment ``get`` returns, and every mutation of the frame happened after that with nothing
    # holding anything. A turn is not short - it can wait 25 seconds on the model - so two turns on
    # one conversation had ample room to interleave and leave the frame describing neither of them.
    # The UI serialises turns today, which is why this had never been seen; a double-submit, a
    # second tab, or the reconnect-and-retry any future client will have is all it would take.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)

    # -- the three legacy fields, now views onto the frame -------------------------------------
    # Kept as properties rather than deleted so nothing that reads them silently sees a stale copy
    # of state that has moved. There is one source of truth; these are windows onto it.

    @property
    def draft_task(self) -> Optional[str]:
        return self.frame.task if self.frame else None

    @property
    def draft_slots(self) -> Dict[str, Any]:
        return self.frame.slots if self.frame else {}

    @property
    def awaiting_slot(self) -> Optional[str]:
        return self.frame.awaiting if self.frame else None

    def open_frame(self, task: str, slots: Optional[Dict[str, Any]] = None) -> "TaskFrame":
        """Starts, or switches to, a request.

        The frame does **not** inherit the last patient. Continuity across turns already works
        without it: ``_resolve_patient`` falls back to ``last_patient_uuid`` for any task that
        actually needs a patient, which is what makes "affiche son dossier" work after a search.
        Copying it onto the frame as well meant a task with no patient at all carried one anyway -
        a create started after a search told the clinician "patient : Benali Amine" while creating
        Karim Saidi, which reads as though the two are related. The frame's patient is set by
        ``remember_patient`` when one is genuinely resolved *for this request*.
        """
        if self.frame is not None and self.frame.task == task:
            if slots:
                self.frame.slots.update(slots)
            return self.frame
        self.frame = TaskFrame(task=task, slots=dict(slots or {}))
        return self.frame

    def close_frame(self) -> None:
        self.frame = None

    def remember_patient(self, uuid: Optional[str], label: Optional[str] = None) -> None:
        """Records which patient the conversation is about, and never lets the two disagree.

        The label and the uuid must describe the same person or the summary is a lie. An earlier
        version kept a known label when a new uuid arrived without one, reasoning that a blank
        summary is bad. It is - but a *wrong* one is far worse, and that is what it produced: with
        Cherif's chart open on the dashboard and Benali searched a turn earlier, the confirmation
        read "Je vais MODIFIER la fiche du patient Benali Amine" and the write landed on Cherif.
        The clinician approves one record and a different one changes (Finding 57).

        So a uuid that changes clears any label that came with the old one. The caller is expected
        to supply the right label, or to look it up - which ``_plan_and_maybe_execute`` now does
        from the record it already reads before an update.
        """
        if not uuid:
            return
        changed = uuid != self.last_patient_uuid
        self.last_patient_uuid = uuid
        if label:
            self.last_patient_label = label
        elif changed:
            self.last_patient_label = None
        if self.frame is not None:
            self.frame.patient_uuid = uuid
            if label:
                self.frame.patient_label = label
            elif changed:
                self.frame.patient_label = None


class ConversationStore:
    """In-memory, TTL-bounded, size-bounded, thread-safe.

    Process-local by construction, and deliberately so: this service is not a system of record and
    a conversation is worth less than the complexity of sharing it. The consequence to know is that
    it assumes **one replica**. Two instances behind the proxy would each hold half the
    conversations and a clinician's follow-up would land on whichever one the load balancer chose,
    losing the frame roughly half the time. Scaling this service out means either sticky sessions
    keyed on ``conversation_id`` or moving this store to something shared - not adding a second
    container.
    """

    def __init__(self, ttl_seconds: Optional[int] = None, max_entries: Optional[int] = None) -> None:
        self._ttl = ttl_seconds if ttl_seconds is not None else settings.conversation_ttl_seconds
        self._max = max_entries if max_entries is not None else settings.max_conversations
        self._entries: Dict[str, ConversationState] = {}
        self._lock = threading.Lock()

    def get(self, conversation_id: str, username: str) -> ConversationState:
        """Returns this conversation, creating it if needed.

        A conversation is keyed by id *and* owner: if the id is already in use by a different
        clinician the caller gets a fresh state rather than the other person's, so a guessed or
        leaked conversation id can never surface someone else's pending action or patient context.
        """
        now = time.time()
        with self._lock:
            self._evict_expired(now)
            state = self._entries.get(conversation_id)
            if state is None or state.username != username:
                state = ConversationState(conversation_id=conversation_id, username=username)
                self._entries[conversation_id] = state
            state.updated_at = now
            self._evict_overflow()
            return state

    def clear_pending(self, conversation_id: str) -> None:
        with self._lock:
            state = self._entries.get(conversation_id)
            if state is not None:
                state.pending = None

    def forget(self, conversation_id: str) -> None:
        with self._lock:
            self._entries.pop(conversation_id, None)

    def _evict_expired(self, now: float) -> None:
        expired = [key for key, state in self._entries.items() if now - state.updated_at > self._ttl]
        for key in expired:
            del self._entries[key]

    def _evict_overflow(self) -> None:
        while len(self._entries) > self._max:
            oldest = min(self._entries.items(), key=lambda item: item[1].updated_at)[0]
            del self._entries[oldest]


store = ConversationStore()
