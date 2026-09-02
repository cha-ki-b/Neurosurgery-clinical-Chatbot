"""The request path, with the engine stubbed - bounds, the guard, quota, and failure."""

from __future__ import annotations

import pytest

from tests.conftest import CHANNEL_SECRET, make_token, pcm, silence


def post(client, body, token=None, params=""):
    return client.post(
        "/v1/transcribe" + params,
        content=body,
        headers={
            "Content-Type": "application/octet-stream",
            "X-Stt-Channel-Key": CHANNEL_SECRET,
            "X-OpenMRS-Agent-Token": token or make_token(),
        },
    )


@pytest.fixture
def stub_engine(monkeypatch):
    """Replace the engine with something that records what it was handed."""
    calls = []

    async def fake(pcm_bytes, language, prompt):
        calls.append({"bytes": len(pcm_bytes), "language": language, "prompt": prompt})
        return "cherche le patient Kaced Amine"

    import app.main
    monkeypatch.setattr(app.main.transcriber, "transcribe", fake)
    return calls


# --- the happy path ------------------------------------------------------------------

def test_a_normal_utterance_returns_text(client, stub_engine):
    r = post(client, pcm(seconds=2.0))
    assert r.status_code == 200
    assert r.json()["text"] == "cherche le patient Kaced Amine"
    assert len(stub_engine) == 1


def test_the_language_is_pinned_to_french_by_default(client, stub_engine):
    """§6.3: auto-detection on a three-second utterance is unreliable."""
    post(client, pcm(seconds=1.0))
    assert stub_engine[0]["language"] == "fr"


def test_the_language_can_be_overridden_per_request(client, stub_engine):
    """The parameter exists from day one so adding a selector later is config, not redesign."""
    post(client, pcm(seconds=1.0), params="?lang=en")
    assert stub_engine[0]["language"] == "en"


# --- the silence guard ---------------------------------------------------------------

def test_silence_returns_empty_text_and_never_reaches_the_engine(client, stub_engine):
    """§6.6. The engine must not see it: that is where the invented French comes from."""
    r = post(client, silence(seconds=2.0))
    assert r.status_code == 200
    assert r.json()["text"] == ""
    assert r.json()["reason"] == "silence"
    assert stub_engine == []


def test_quiet_room_noise_is_also_refused(client, stub_engine):
    import array, random
    random.seed(7)
    noise = array.array("h", [random.randint(-8, 8) for _ in range(32000)]).tobytes()
    assert post(client, noise).json()["text"] == ""
    assert stub_engine == []


# --- bounds --------------------------------------------------------------------------

def test_an_utterance_over_the_cap_is_413(client, stub_engine):
    r = post(client, pcm(seconds=31.0))
    assert r.status_code == 413
    assert stub_engine == []


def test_an_utterance_at_the_cap_is_accepted(client, stub_engine):
    assert post(client, pcm(seconds=29.9)).status_code == 200


def test_a_too_short_utterance_returns_empty_rather_than_an_error(client, stub_engine):
    """A stray click should leave the compose box alone, not show an error."""
    r = post(client, pcm(seconds=0.1))
    assert r.status_code == 200
    assert r.json()["text"] == ""
    assert r.json()["reason"] == "too_short"
    assert stub_engine == []


def test_an_empty_body_is_not_an_error(client, stub_engine):
    assert post(client, b"").json()["text"] == ""
    assert stub_engine == []


# --- engine failure ------------------------------------------------------------------

def test_an_unreachable_engine_is_503_not_500(client, monkeypatch):
    """The clinician should see "dictée indisponible" and keep typing."""
    from app.engine import TranscriptionError
    import app.main

    async def boom(*_a, **_k):
        raise TranscriptionError("connection refused")

    monkeypatch.setattr(app.main.transcriber, "transcribe", boom)
    r = post(client, pcm(seconds=1.0))
    assert r.status_code == 503
    assert r.json()["error"] == "unavailable"


def test_a_failed_request_releases_the_users_slot(client, monkeypatch, stub_engine):
    """Otherwise one GPU hiccup locks a clinician out until the process restarts."""
    from app.engine import TranscriptionError
    import app.main

    async def boom(*_a, **_k):
        raise TranscriptionError("nope")

    monkeypatch.setattr(app.main.transcriber, "transcribe", boom)
    assert post(client, pcm(seconds=1.0)).status_code == 503

    # restore a working stub and confirm the slot was released, not leaked
    async def ok(pcm_bytes, language, prompt):
        return "ok"
    monkeypatch.setattr(app.main.transcriber, "transcribe", ok)
    assert post(client, pcm(seconds=1.0)).status_code == 200


# --- vocabulary biasing --------------------------------------------------------------

def test_a_missing_lexicon_file_does_not_break_dictation(client, stub_engine):
    """Biasing is an improvement, not a dependency. A bad path must not take it offline."""
    r = post(client, pcm(seconds=1.0))
    assert r.status_code == 200
    assert stub_engine[0]["prompt"] == ""


def test_the_lexicon_is_passed_to_the_engine_when_present(client, stub_engine, tmp_path, monkeypatch):
    lex = tmp_path / "lexicon.txt"
    lex.write_text("# neurochirurgie\nGlasgow\nKarnofsky\nhydrocéphalie\n", encoding="utf-8")
    from app.config import settings
    monkeypatch.setattr(settings, "bias_lexicon_path", str(lex))
    post(client, pcm(seconds=1.0))
    assert stub_engine[0]["prompt"] == "Glasgow, Karnofsky, hydrocéphalie"


def test_comments_and_blank_lines_are_stripped_from_the_lexicon(tmp_path, monkeypatch):
    lex = tmp_path / "l.txt"
    lex.write_text("# a comment\n\nGlasgow\n\n  Karnofsky  \n", encoding="utf-8")
    from app.config import settings
    monkeypatch.setattr(settings, "bias_lexicon_path", str(lex))
    assert settings.bias_prompt() == "Glasgow, Karnofsky"


# --- health --------------------------------------------------------------------------

def test_health_does_not_touch_the_engine(client, stub_engine):
    """A health check that fails when the GPU is busy takes the service out of rotation
    exactly when it is working hardest."""
    assert client.get("/health").json()["status"] == "ok"
    assert stub_engine == []


# --- language validation -------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "fr&extra=1", "fr HTTP/1.1", "../../etc/passwd", "fr\r\nX-Injected: 1",
    "a" * 200, "<script>", "", "   ", "français",
])
def test_a_malformed_language_falls_back_to_the_default(client, stub_engine, bad):
    """Browser-supplied and therefore not trusted. Anything off-shape is ignored rather
    than passed on - it would otherwise reach a URL on the OpenMRS side and a form field
    here.

    Percent-encoded, as any real client would send it. Passing raw CRLF makes httpx
    refuse to build the request at all, which tests httpx rather than this service.
    """
    from urllib.parse import quote
    post(client, pcm(seconds=1.0), params="?lang=" + quote(bad, safe=""))
    assert stub_engine[0]["language"] == "fr"


@pytest.mark.parametrize("good,expected", [("en", "en"), ("ar", "ar"), ("ar-DZ", "ar-DZ"), ("fr", "fr")])
def test_a_real_language_tag_is_honoured(client, stub_engine, good, expected):
    post(client, pcm(seconds=1.0), params="?lang=" + good)
    assert stub_engine[0]["language"] == expected
