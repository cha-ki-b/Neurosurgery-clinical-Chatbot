"""The Clinical Dictation Service - one endpoint, no state, no OpenMRS account.

    POST /v1/transcribe   raw Int16LE PCM body -> {"text": "..."}

Called only by OpenMRS on Server 1, over TLS, from one allowed address. Never by a
browser: the channel secret would have to be readable from page JavaScript, which means
it would not be a secret (ADR-12, STT-PLAN.md §2.2).

Holds nothing. The audio exists for the lifetime of one request and is never written to
disk and never logged. The reviewable record of what a clinician asked for lives where it
already does - `agentgateway_operation_log` on Server 1.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

import re

from .audio import is_silent
from .config import settings
from .engine import OpenAiCompatibleTranscriber, TranscriptionError
from .security import ChannelAuthError, TokenError, verify_channel_secret, verify_delegated_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stt")

@asynccontextmanager
async def lifespan(_app: "FastAPI"):
    settings.validate()
    log.info(
        "dictation service ready: engine=%s model=%s language=%s max=%ss",
        settings.engine_url, settings.model, settings.language, settings.max_utterance_seconds,
    )
    yield


app = FastAPI(
    title="Clinical Dictation Service",
    lifespan=lifespan,
    # FastAPI's own docs describe the hospital's API surface and have no business being
    # reachable, even from Server 1. nginx returns 404 for them too; belt and braces.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

transcriber = OpenAiCompatibleTranscriber()

# A whitelist, not a sanitiser. The set of valid language tags is tiny and known, so
# anything outside it is a mistake or an attempt and both get the same answer: ignored,
# and the configured default applies. OpenMRS validates the same shape before it ever
# reaches here - this is the second lock, for the same door.
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$")


def _language(requested: str | None) -> str:
    if requested and _LANGUAGE_TAG.match(requested.strip()):
        return requested.strip()
    return settings.language

# Per-clinician concurrency. nginx's rate limit is keyed on the source address, and every
# request arrives from Server 1's single address - so it limits the hospital, not a user
# (the server2 README says the same of /chat). One dictation is a GPU workload, so the
# real per-user bound has to live here, keyed on the token's subject.
_in_flight: dict[str, int] = defaultdict(int)
_in_flight_lock = asyncio.Lock()


@app.get("/health")
async def health() -> dict:
    """Liveness only - deliberately does not touch the engine.

    A health check that fails when the GPU is busy would take the service out of the
    proxy's rotation exactly when it is working hardest. Engine reachability surfaces
    per request instead, as a 503 the clinician sees as "dictée indisponible".
    """
    return {
        "status": "ok",
        "engine_url": settings.engine_url,
        "model": settings.model,
        "language": settings.language,
    }


@app.post("/v1/transcribe")
async def transcribe(
    request: Request,
    x_stt_channel_key: str | None = Header(default=None),
    x_openmrs_agent_token: str | None = Header(default=None),
    lang: str | None = None,
) -> Response:
    # --- channel trust, then user trust. Neither substitutes for the other. -------------
    try:
        verify_channel_secret(x_stt_channel_key)
    except ChannelAuthError:
        # No detail: an attacker probing the port learns nothing about why.
        return JSONResponse({"error": "forbidden"}, status_code=403)

    try:
        user = verify_delegated_token(x_openmrs_agent_token)
    except TokenError as exc:
        log.warning("dictation refused: %s", exc)
        return JSONResponse({"error": "unauthorised"}, status_code=401)

    # --- bounds, before anything expensive ---------------------------------------------
    pcm = await request.body()

    if len(pcm) > settings.max_pcm_bytes:
        return JSONResponse(
            {"error": "too_long",
             "detail": f"maximum {settings.max_utterance_seconds}s"},
            status_code=413,
        )
    if len(pcm) < settings.min_pcm_bytes:
        # Too short to be speech. Not an error the clinician needs to see - an empty
        # transcript leaves their compose box exactly as it was.
        return JSONResponse({"text": "", "reason": "too_short"})

    # STT-PLAN.md §6.6. Measured: with the language pinned, this model answers silence
    # with confident invented French, every time. Refuse before the engine sees it.
    if is_silent(pcm, settings.silence_rms_threshold):
        return JSONResponse({"text": "", "reason": "silence"})

    # --- per-user concurrency ----------------------------------------------------------
    async with _in_flight_lock:
        if _in_flight[user.username] >= settings.max_concurrent_per_user:
            return JSONResponse(
                {"error": "busy", "detail": "a dictation is already in progress"},
                status_code=429,
            )
        _in_flight[user.username] += 1

    started = time.monotonic()
    try:
        text = await transcriber.transcribe(
            pcm,
            language=_language(lang),
            prompt=settings.bias_prompt(),
        )
    except TranscriptionError as exc:
        log.warning("transcription failed: %s", exc)
        # 503, not 500: the clinician should see "dictée indisponible" and carry on
        # typing. A dead GPU narrows what the assistant offers; it never blocks the chat.
        return JSONResponse({"error": "unavailable"}, status_code=503)
    finally:
        async with _in_flight_lock:
            _in_flight[user.username] -= 1
            if _in_flight[user.username] <= 0:
                del _in_flight[user.username]

    elapsed = time.monotonic() - started
    audio_seconds = len(pcm) / (settings.sample_rate * 2)

    # The transcript is PHI. Off by default, same convention and same reason as the
    # agent's LOG_PROMPTS. Audio is never logged at any level - there is no setting.
    if settings.log_transcripts:
        log.info("dictation for %s: %r", user.username, text)
    else:
        log.info(
            "dictation for %s: %.1fs audio in %.2fs, %d chars",
            user.username, audio_seconds, elapsed, len(text),
        )

    return JSONResponse({"text": text})
