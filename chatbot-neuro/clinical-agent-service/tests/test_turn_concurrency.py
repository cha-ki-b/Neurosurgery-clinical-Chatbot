"""Two turns on one conversation are serialised, not interleaved.

The store's lock protected the dictionary and was released the moment ``get`` returned. Every
mutation of the frame - the task, the slots, the field being changed, the pending write - happened
after that, unsynchronised, for the whole length of a turn. A turn is not short: it can spend 25
seconds waiting on the model. Two turns arriving in that window could each plan from a frame the
other was halfway through rewriting.

Nobody had seen it because the chat panel disables its own input until a reply comes back. That is
a property of one client, not of the service, and it is one double-submit or one second browser tab
away from not holding.
"""

from __future__ import annotations

import asyncio

import pytest

from app.nlu.base import INTENT_UNSUPPORTED, Interpretation
from app.orchestrator import Orchestrator
from app.security import ActingUser


class SlowNlu:
    """Records when each turn enters and leaves interpretation."""

    def __init__(self, trace):
        self._trace = trace

    async def ainterpret(self, prompt, context):
        self._trace.append(f"enter:{prompt}")
        # Yields control. Without a lock the second turn starts here, every time.
        await asyncio.sleep(0.02)
        self._trace.append(f"leave:{prompt}")
        return Interpretation(intent=INTENT_UNSUPPORTED, clarification="rien a faire")


USER = ActingUser(username="dr.benali", user_uuid="u", conversation_id="c", may_write=True, purpose="chat")


@pytest.mark.asyncio
async def test_concurrent_turns_on_one_conversation_do_not_interleave():
    trace = []
    orchestrator = Orchestrator(nlu=SlowNlu(trace))

    await asyncio.gather(
        orchestrator.handle_turn("un", "token", USER, "same-conversation", {}),
        orchestrator.handle_turn("deux", "token", USER, "same-conversation", {}),
    )

    # Whichever ran first, it must have finished before the other started.
    assert trace in (
        ["enter:un", "leave:un", "enter:deux", "leave:deux"],
        ["enter:deux", "leave:deux", "enter:un", "leave:un"],
    ), f"turns interleaved: {trace}"


@pytest.mark.asyncio
async def test_different_conversations_still_run_concurrently():
    """Serialising per conversation must not serialise the whole ward."""
    trace = []
    orchestrator = Orchestrator(nlu=SlowNlu(trace))

    await asyncio.gather(
        orchestrator.handle_turn("un", "token", USER, "conversation-a", {}),
        orchestrator.handle_turn("deux", "token", USER, "conversation-b", {}),
    )

    assert trace[0].startswith("enter") and trace[1].startswith("enter"), \
        f"independent conversations were serialised: {trace}"


@pytest.mark.asyncio
async def test_a_frame_survives_a_concurrent_turn_intact():
    """The failure the lock prevents, stated as state rather than as ordering."""
    from app.conversation import store

    trace = []
    orchestrator = Orchestrator(nlu=SlowNlu(trace))

    await asyncio.gather(
        *[orchestrator.handle_turn(f"turn-{i}", "token", USER, "shared", {}) for i in range(6)]
    )

    state = store.get("shared", USER.username)
    # Every turn here is unsupported, so the frame must end closed - not half-written by whichever
    # coroutine happened to be resumed last.
    assert state.frame is None
