"""Raw PCM in, WAV out, plus the check that stops silence reaching the model.

No codec, no ffmpeg, no container parsing. The browser sends 16 kHz mono Int16 because
that is what the engine demands - vLLM's /v1/audio/transcriptions rejects anything else
outright with "Invalid or unsupported audio file", even though librosa inside the same
container decodes it happily (STT-PLAN.md §2.3, measured 2026-09-01). So the only
transformation this service performs is prepending a 44-byte header.

That is the whole audio dependency: `struct`.
"""

from __future__ import annotations

import array
import math
import struct
import sys


def wav_header(pcm_len: int, sample_rate: int) -> bytes:
    """A 44-byte canonical WAV header for mono 16-bit PCM."""
    return (
        b"RIFF"
        + struct.pack("<I", 36 + pcm_len)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", pcm_len)
    )


def to_wav(pcm: bytes, sample_rate: int) -> bytes:
    return wav_header(len(pcm), sample_rate) + pcm


def rms(pcm: bytes) -> float:
    """Root-mean-square amplitude of Int16LE samples, 0.0 for an empty buffer.

    Used to decide whether anything was actually said. Cheap enough to run on every
    request: a 30-second utterance is 480,000 samples, which `array` sums in a few
    milliseconds without numpy.
    """
    if len(pcm) < 2:
        return 0.0
    samples = array.array("h")
    # Trailing odd byte would raise; drop it rather than reject the whole utterance.
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def is_silent(pcm: bytes, threshold: int) -> bool:
    """Whether this buffer should be refused before it reaches the model.

    STT-PLAN.md §6.6: handed silence with the language pinned to French, Qwen3-ASR-0.6B
    returns fluent invented sentences - "Je suis un peu en colère", "Ah, c'est ça" - every
    single time. Measured, not theoretical. The transcript is an editable draft so the
    clinician would see and delete it (§6.1), which makes this a usability defect rather
    than a safety hole; it is guarded because "I said nothing and it wrote something" is
    a baffling thing for a clinician to be shown.
    """
    return rms(pcm) < threshold
