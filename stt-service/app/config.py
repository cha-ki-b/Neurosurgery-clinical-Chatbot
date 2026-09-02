"""Settings, and the refusal to start without the ones that matter.

Mirrors the clinical agent's `app/config.py`: everything comes from the environment, the
security-critical values have no defaults, and `validate()` fails loudly at startup rather
than letting the first request discover the problem.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from typing import List


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- trust boundaries -------------------------------------------------------------
    # Deliberately NOT the clinical agent's AGENT_CHANNEL_SECRET. This service decodes
    # attacker-shaped binary audio through a model runtime, which is a larger attack
    # surface than the agent's JSON. Sharing the secret would mean a compromise here
    # hands the attacker the agent's /chat endpoint too. See STT-PLAN.md §3/Q2.
    channel_secret: str = field(default_factory=lambda: os.getenv("STT_CHANNEL_SECRET", "").strip())
    jwt_public_key_b64: str = field(
        default_factory=lambda: "".join(os.getenv("OPENMRS_JWT_PUBLIC_KEY", "").split())
    )
    jwt_issuer: str = field(default_factory=lambda: os.getenv("JWT_ISSUER", "openmrs-agentgateway"))
    # A chat token must not be replayable here, and an STT token must not open a chat
    # turn. Same mechanism the module already uses to separate chat/rollback/read.
    jwt_audience: str = field(default_factory=lambda: os.getenv("STT_JWT_AUDIENCE", "stt-service"))
    token_purpose: str = field(default_factory=lambda: os.getenv("STT_TOKEN_PURPOSE", "stt"))

    # --- engine -----------------------------------------------------------------------
    engine_url: str = field(
        default_factory=lambda: os.getenv("STT_ENGINE_URL", "http://stt-engine:8000/v1").rstrip("/")
    )
    model: str = field(default_factory=lambda: os.getenv("STT_MODEL", "Qwen3-ASR-0.6B"))
    language: str = field(default_factory=lambda: os.getenv("STT_LANGUAGE", "fr"))
    engine_timeout_seconds: int = field(default_factory=lambda: _int("STT_ENGINE_TIMEOUT_SECONDS", 30))

    # --- limits -----------------------------------------------------------------------
    sample_rate: int = field(default_factory=lambda: _int("STT_SAMPLE_RATE", 16000))
    max_utterance_seconds: int = field(default_factory=lambda: _int("STT_MAX_UTTERANCE_SECONDS", 30))
    min_utterance_ms: int = field(default_factory=lambda: _int("STT_MIN_UTTERANCE_MS", 300))
    max_concurrent_per_user: int = field(default_factory=lambda: _int("STT_MAX_CONCURRENT_PER_USER", 1))

    # RMS below this is treated as "nothing was said". Int16 full scale is 32767, so 200
    # is about -44 dBFS - quiet room noise, well under speech. STT-PLAN.md §6.6: this
    # model invents fluent French when handed silence, every time, with the language
    # pinned. The guard is what stops that reaching the clinician's compose box.
    silence_rms_threshold: int = field(default_factory=lambda: _int("STT_SILENCE_RMS_THRESHOLD", 200))

    # --- vocabulary biasing -----------------------------------------------------------
    # Free-form context steering decoding toward the department's own words. Worth more
    # than a fine-tune on a few hours of audio, costs nothing, and is edited as a text
    # file rather than retrained. STT-PLAN.md §3/Q3.
    bias_lexicon_path: str = field(
        default_factory=lambda: os.getenv("STT_BIAS_LEXICON_PATH", "/etc/stt/lexicon-neurochir.txt")
    )

    # --- logging ----------------------------------------------------------------------
    # Audio is NEVER logged, at any level - there is deliberately no setting for it.
    # Transcripts are PHI, so this mirrors the agent's LOG_PROMPTS convention and default.
    log_transcripts: bool = field(default_factory=lambda: _bool("LOG_TRANSCRIPTS", False))

    @property
    def max_pcm_bytes(self) -> int:
        """Two bytes per sample, mono."""
        return self.sample_rate * self.max_utterance_seconds * 2

    @property
    def min_pcm_bytes(self) -> int:
        return int(self.sample_rate * (self.min_utterance_ms / 1000.0)) * 2

    def bias_prompt(self) -> str:
        """The lexicon, read fresh each call so editing the file needs no restart.

        Missing or unreadable is not an error: biasing is an improvement, not a
        dependency, and a typo in a path must not take dictation offline.
        """
        try:
            with open(self.bias_lexicon_path, "r", encoding="utf-8") as fh:
                terms = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
        except OSError:
            return ""
        return ", ".join(terms)

    def validate(self) -> None:
        missing: List[str] = []
        if not self.channel_secret:
            missing.append("STT_CHANNEL_SECRET")
        if not self.jwt_public_key_b64:
            missing.append("OPENMRS_JWT_PUBLIC_KEY")
        if missing:
            raise RuntimeError(
                "refusing to start without: " + ", ".join(missing)
                + ". Without the channel secret this service cannot tell the hospital's OpenMRS "
                "from anything else that can reach the port."
            )
        if self.channel_secret == os.getenv("AGENT_CHANNEL_SECRET", "").strip():
            # Both live in the same .env, so a copy-paste is the likely way this happens.
            raise RuntimeError(
                "STT_CHANNEL_SECRET must not equal AGENT_CHANNEL_SECRET. Separate secrets are "
                "what stop a compromise of this service reaching the clinical agent's /chat "
                "(STT-PLAN.md §3/Q2). Generate a new one: openssl rand -base64 48"
            )
        try:
            base64.b64decode(self.jwt_public_key_b64, validate=True)
        except Exception as exc:
            raise RuntimeError(f"OPENMRS_JWT_PUBLIC_KEY is not valid base64: {exc}")


settings = Settings()
