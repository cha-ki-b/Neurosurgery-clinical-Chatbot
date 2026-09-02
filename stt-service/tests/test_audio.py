"""The WAV header, the silence guard, and the bounds - the parts with no network."""

from __future__ import annotations

import io
import struct
import wave

import pytest

from app.audio import is_silent, rms, to_wav, wav_header
from tests.conftest import pcm, silence


# --- the 44 bytes --------------------------------------------------------------------

def test_header_is_44_bytes():
    assert len(wav_header(1000, 16000)) == 44


def test_the_result_is_a_wav_python_can_read():
    """The real test: hand it to the stdlib and see if it agrees."""
    data = pcm(seconds=0.5)
    with wave.open(io.BytesIO(to_wav(data, 16000)), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000
        assert w.getnframes() == len(data) // 2
        assert w.readframes(w.getnframes()) == data


def test_sizes_in_the_header_match_the_payload():
    data = pcm(seconds=0.25)
    header = to_wav(data, 16000)[:44]
    assert struct.unpack("<I", header[4:8])[0] == 36 + len(data)
    assert struct.unpack("<I", header[40:44])[0] == len(data)


def test_a_different_sample_rate_is_carried_through():
    with wave.open(io.BytesIO(to_wav(pcm(0.1, rate=8000), 8000)), "rb") as w:
        assert w.getframerate() == 8000
        assert w.getparams().framerate * 2 == 16000  # byte rate field consistency


# --- RMS -----------------------------------------------------------------------------

def test_rms_of_digital_silence_is_zero():
    assert rms(silence()) == 0.0


def test_rms_of_an_empty_buffer_is_zero():
    assert rms(b"") == 0.0
    assert rms(b"\x00") == 0.0


def test_rms_of_a_tone_is_about_amplitude_over_root_two():
    """A sine at amplitude A has RMS A/sqrt(2). Confirms the maths, not just non-zero."""
    import math
    got = rms(pcm(seconds=1.0, amplitude=10000))
    assert math.isclose(got, 10000 / math.sqrt(2), rel_tol=0.02)


def test_an_odd_trailing_byte_does_not_raise():
    """A truncated frame should not reject the whole utterance."""
    assert rms(pcm(0.1) + b"\x01") > 0


# --- the guard -----------------------------------------------------------------------

def test_digital_silence_is_silent():
    assert is_silent(silence(), 200) is True


def test_speech_level_audio_is_not_silent():
    assert is_silent(pcm(amplitude=4000), 200) is False


def test_quiet_room_noise_is_still_silent():
    """STT-PLAN.md §6.6: dither at this level made the model produce fluent French."""
    import array
    import random
    random.seed(1)
    noise = array.array("h", [random.randint(-8, 8) for _ in range(16000)]).tobytes()
    assert is_silent(noise, 200) is True


def test_the_threshold_is_the_boundary():
    import array
    just_under = array.array("h", [150] * 16000).tobytes()
    just_over = array.array("h", [250] * 16000).tobytes()
    assert is_silent(just_under, 200) is True
    assert is_silent(just_over, 200) is False
