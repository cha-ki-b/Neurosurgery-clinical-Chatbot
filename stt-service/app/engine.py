"""The seam a different model is swapped in behind.

The gateway speaks exactly one protocol to the engine: OpenAI's
``/v1/audio/transcriptions``. vLLM, faster-whisper-server, whisper.cpp's server and
essentially every other serving stack expose it, so changing model or runtime is an
environment variable and a container - never a change in here (STT-PLAN.md §2.4).

There is one implementation. A second one is only worth writing when there is a second
engine that does not speak this protocol; until then a Protocol plus one class is the
honest amount of abstraction.
"""

from __future__ import annotations

from typing import Optional, Protocol

import httpx

from .audio import to_wav
from .config import settings


class TranscriptionError(Exception):
    """The engine could not be reached, or refused the audio."""


class Transcriber(Protocol):
    async def transcribe(self, pcm: bytes, language: str, prompt: str) -> str: ...


class OpenAiCompatibleTranscriber:
    """Posts to /v1/audio/transcriptions and returns the text."""

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None):
        self._base_url = (base_url or settings.engine_url).rstrip("/")
        self._model = model or settings.model

    async def transcribe(self, pcm: bytes, language: str, prompt: str) -> str:
        wav = to_wav(pcm, settings.sample_rate)
        data = {"model": self._model, "language": language, "response_format": "json"}
        if prompt:
            # Vocabulary biasing. Named `prompt` because that is what the OpenAI
            # transcription API calls it, and what vLLM and whisper.cpp both accept.
            data["prompt"] = prompt

        try:
            async with httpx.AsyncClient(timeout=settings.engine_timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/audio/transcriptions",
                    files={"file": ("utterance.wav", wav, "audio/wav")},
                    data=data,
                )
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"the transcription engine could not be reached: {exc}") from exc

        if response.status_code != 200:
            # The body can carry the engine's own reason (a sample-rate refusal, say).
            # Worth keeping in the service log; it contains no audio and no transcript.
            raise TranscriptionError(
                f"the transcription engine answered HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            return (response.json().get("text") or "").strip()
        except ValueError as exc:
            raise TranscriptionError(f"the engine's reply was not JSON: {exc}") from exc
