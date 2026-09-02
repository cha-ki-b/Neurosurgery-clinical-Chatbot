"""The Clinical Agent Service - one process, one externally reachable endpoint.

`POST /chat` is the entire surface OpenMRS talks to, which keeps the Server 1 to Server 2 boundary
a single thing to firewall, monitor and reason about. `/health` and `/capabilities` are
operational endpoints for the administrator, not part of the clinical path.

This service holds no OpenMRS credentials of its own and no durable store. Everything it can do,
it does as the clinician whose signed token arrived with the turn.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .capabilities import registry as capability_registry
from .config import settings
from .orchestrator import Orchestrator
from .security import ChannelAuthError, TokenError, verify_channel_secret, verify_delegated_token
from .telemetry import telemetry
from .tools.catalog import build_registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
log = logging.getLogger("clinical-agent")

# httpx logs every request line at INFO, and a patient search URL *is* patient data:
# `GET /ws/fhir2/R4/Patient?name=Zoubir%20Belkacemi` puts the name a clinician typed straight into
# this container's logs. Our own log lines were audited and redacted for exactly this (Finding 44);
# leaving the HTTP client to publish the same values one line later would have made that pointless.
# Raised to WARNING unless an operator has explicitly turned prompt logging on, in which case they
# have already accepted PHI in the logs for the length of a debugging session.
logging.getLogger("httpx").setLevel(logging.INFO if settings.log_prompts else logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

app = FastAPI(
    title="Clinical Agent Service",
    version="1.0.0",
    description="Conversational access to OpenMRS for the CHU Blida neurosurgery department.",
)

orchestrator = Orchestrator()
tool_registry = build_registry(settings.patientview_tools_enabled)


class ChatContext(BaseModel):
    patient_uuid: Optional[str] = None
    locale: str = "fr"


class ChatRequest(BaseModel):
    """What agentgateway relays.

    Note what is absent: any user id. Identity comes from the signed token and nowhere else
    (ADR-13). A second, unsigned assertion of who the caller is would be one more thing that could
    be believed by mistake.
    """

    conversation_id: str = Field(..., min_length=1, max_length=64)
    prompt: str = Field(..., min_length=1, max_length=4000)
    delegated_token: str = Field(..., min_length=1)
    context: ChatContext = Field(default_factory=ChatContext)


@app.on_event("startup")
async def on_startup() -> None:
    settings.validate()
    # Best-effort: the capability statement is read again on the first turn if OpenMRS was not up
    # yet. Failing to read it disables the FHIR-backed tools with an explicit reason rather than
    # letting them fail obscurely later.
    await capability_registry.refresh(force=True)
    log.info(
        "Clinical Agent Service ready. OpenMRS: %s. Neurosurgery-specific tools: %s.",
        settings.openmrs_base_url,
        "enabled" if settings.patientview_tools_enabled else "disabled",
    )
    if settings.log_prompts:
        # Said once, loudly, at the only moment an operator is watching. LOG_PROMPTS is a
        # debugging switch and it writes patient data into this container's logs, where it is kept
        # by the docker log rotation rather than by the hospital's retention policy. The audit
        # trail on the OpenMRS side already records every prompt under proper access control.
        log.warning(
            "LOG_PROMPTS is ON: clinicians' prompts and the values read from them are being "
            "written to this container's logs. That is patient data outside the OpenMRS audit "
            "trail. Turn it off once the current diagnosis is finished."
        )


@app.get("/health")
async def health() -> Dict[str, Any]:
    capabilities = capability_registry.current
    return {
        "status": "ok",
        "openmrs_base_url": settings.openmrs_base_url,
        "fhir_capabilities_known": capabilities.known,
        "fhir_capabilities_error": capabilities.error,
    }


@app.get("/metrics")
async def metrics(
    channel_key: Optional[str] = Header(default=None, alias="X-Agent-Channel-Key"),
) -> Dict[str, Any]:
    """What the assistant has been doing, in counts with no patient data in them.

    Gated on the channel secret for the same reason ``/capabilities`` is: it describes this
    hospital's use of the system, which is not something to hand to anything that can reach the
    port.
    """
    _require_channel(channel_key)
    return telemetry.snapshot()


@app.get("/capabilities")
async def capabilities(
    channel_key: Optional[str] = Header(default=None, alias="X-Agent-Channel-Key"),
) -> Dict[str, Any]:
    """What the assistant can and cannot do on this installation, and why.

    Gated on the channel secret: it describes the hospital's OpenMRS configuration, which is not
    something to hand out to anything that can reach the port.
    """
    _require_channel(channel_key)
    current = capability_registry.current
    return {"fhir": current.describe(), "tools": tool_registry.describe(current)}


@app.post("/chat")
async def chat(
    payload: ChatRequest,
    request: Request,
    channel_key: Optional[str] = Header(default=None, alias="X-Agent-Channel-Key"),
) -> Dict[str, Any]:
    _require_channel(channel_key)

    try:
        user = verify_delegated_token(payload.delegated_token)
    except TokenError as exc:
        log.warning("Rejected a turn from %s: %s", request.client.host if request.client else "?", exc)
        raise HTTPException(status_code=401, detail="The delegated token could not be trusted") from exc

    # The conversation id is only ever a grouping key. It is never trusted to carry identity or
    # permission - both of those are re-derived from the token on every single turn.
    conversation_id = payload.conversation_id or str(uuid.uuid4())

    if settings.log_prompts:
        log.info("[%s] %s: %s", conversation_id, user.username, payload.prompt)
    else:
        log.info("[%s] turn from %s (%d chars)", conversation_id, user.username, len(payload.prompt))

    # Re-read the capability statement when it has gone stale, or was never readable at startup.
    await capability_registry.refresh(delegated_token=payload.delegated_token)

    started = time.monotonic()
    result = await orchestrator.handle_turn(
        prompt=payload.prompt,
        delegated_token=payload.delegated_token,
        user=user,
        conversation_id=conversation_id,
        context=payload.context.model_dump(),
    )
    elapsed_ms = (time.monotonic() - started) * 1000
    telemetry.record_turn(result.task_type, result.state, elapsed_ms)

    log.info("[%s] -> %s (%s) in %dms", conversation_id, result.state, result.task_type, elapsed_ms)
    return result.to_response(conversation_id)


def _require_channel(channel_key: Optional[str]) -> None:
    try:
        verify_channel_secret(channel_key)
    except ChannelAuthError as exc:
        raise HTTPException(status_code=403, detail="Unrecognised caller") from exc
