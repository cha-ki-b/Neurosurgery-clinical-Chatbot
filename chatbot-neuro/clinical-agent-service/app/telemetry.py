"""Counters for what the assistant actually did, with no patient data in them.

Item 7 of the improvement list assumed the corpus of real clinician phrasings could be mined from
``agentgateway_operation_log``. Measured, it cannot - and the reason matters more than the
conclusion. That log records the HTTP calls the agent *made to OpenMRS*, and a turn the assistant
did not understand makes none: it is answered from the interpreter and returns before any client
exists. Four such turns in a row leave the audit trail completely empty.

So the turns most worth learning from - the ones that ended in "je n'ai pas compris", the
clarifications that were asked twice, the frames abandoned - are exactly the ones invisible to the
system of record. They can only be counted here.

What is counted is shape, never content: which task, which outcome, how long, how often the model
was unreachable. That is enough to answer "is the 4B model holding up", "which task do clinicians
abandon", and "did that change help", none of which anything could answer before. The sentences
themselves are captured separately, under supervision, with ``LOG_PROMPTS`` - see ``app.phi``.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Any, Dict, Optional

# Bucket boundaries in milliseconds. A clinician notices the difference between "instant" and "a
# second"; nobody needs a percentile.
_LATENCY_BUCKETS = ((500, "under_500ms"), (2000, "under_2s"), (10000, "under_10s"))


class Telemetry:
    """Process-local, lock-guarded, unbounded in nothing but integers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter = Counter()
        self._started = time.time()

    def record(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[key] += amount

    def record_turn(self, task_type: Optional[str], state: str, elapsed_ms: float) -> None:
        with self._lock:
            self._counts["turns.total"] += 1
            self._counts[f"turns.state.{state}"] += 1
            self._counts[f"turns.task.{task_type or 'none'}"] += 1
            self._counts[f"turns.latency.{_bucket(elapsed_ms)}"] += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            counts = dict(sorted(self._counts.items()))
        return {"uptime_seconds": round(time.time() - self._started), "counters": counts}

    def reset(self) -> None:
        """Only for tests. There is no operational reason to lose the numbers."""
        with self._lock:
            self._counts.clear()


def _bucket(elapsed_ms: float) -> str:
    for threshold, name in _LATENCY_BUCKETS:
        if elapsed_ms < threshold:
            return name
    return "over_10s"


telemetry = Telemetry()
