"""One helper, for one rule: patient data does not go to this server's logs.

The rule is not new - ``LOG_PROMPTS`` exists precisely because prompts are patient data and the
default is off. What was new on inspection is that the flag governed two log lines out of fifteen.
Thirteen others interpolated the clinician's sentence, or a name, phone number or date of birth
extracted from it, at INFO level unconditionally: every dropped slot, every abandoned frame, every
task switch. With the flag off, and believed to be protective, container logs still accumulated
PHI (Finding 44).

Where PHI *is* meant to live is already settled and unchanged: ``agentgateway_operation_log`` on
the OpenMRS side, which stores ``raw_prompt``, ``request_body`` and ``previous_state`` under the
hospital's own access control, on the system of record. This server holds no data and keeps no
record; its logs are for diagnosing behaviour, and behaviour can be diagnosed from shapes.
"""

from __future__ import annotations

from typing import Any

from .config import settings

REDACTED = "<redacted>"


def safe(value: Any) -> str:
    """A log-safe rendering of something that may contain patient data.

    Returns the real value only when an operator has explicitly turned prompt logging on for a
    debugging session. Otherwise it returns the value's *shape* - enough to tell "the model
    returned an empty name" from "the model returned a long one", which is what a log line about a
    dropped slot is actually for.
    """
    if settings.log_prompts:
        return repr(value)
    if value is None:
        return "<none>"
    text = str(value)
    return f"<{len(text)} chars>" if text else "<empty>"


# Query parameters whose values say nothing about a patient. Everything else in a query string is
# either something the clinician typed or something that identifies a record.
_SAFE_QUERY_PARAMS = {"_count", "_sort"}


def safe_path(path: Any) -> str:
    """A request path with its query values redacted, keeping the endpoint and parameter names.

    ``main.py`` raises the httpx logger to WARNING precisely because a search URL *is* patient data
    - ``GET /ws/fhir2/R4/Patient?name=Zoubir%20Belkacemi``. Silencing the HTTP client achieved
    nothing while the application logged the same URL itself, one line later, on every non-2xx and
    every timeout (Finding 56). Those are not rare paths: a 403 for a clinician without the
    privilege, a 404 while the relay filter is misconfigured, or a slow Server 1 all reach them.

    What survives redaction is what a diagnosis actually needs - which endpoint, which parameters
    were sent, how many were asked for - none of which identifies anybody.
    """
    if settings.log_prompts:
        return str(path)
    text = str(path)
    head, separator, query = text.partition("?")
    if not separator:
        return head
    parts = []
    for pair in query.split("&"):
        key, has_value, _ = pair.partition("=")
        if not has_value:
            parts.append(pair)
        elif key in _SAFE_QUERY_PARAMS:
            parts.append(pair)
        else:
            parts.append(f"{key}={REDACTED}")
    return f"{head}?{'&'.join(parts)}"


def safe_slots(slots: Any) -> str:
    """Which slots are filled, never what is in them.

    A slot *name* is not patient data; a slot value almost always is. This is the form that
    belongs in an always-on log line.
    """
    if not isinstance(slots, dict):
        return REDACTED
    return "{" + ", ".join(sorted(key for key, value in slots.items() if value not in (None, "", []))) + "}"
