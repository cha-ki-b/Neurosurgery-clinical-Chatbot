"""Every way a request can fail to prove who it is, and that each one is refused.

Mirrors `DelegatedTokenTest` on the OpenMRS side and the agent service's own
`test_security.py`: a tampered payload, another key's signature, `alg: none`, the wrong
audience, the wrong issuer, a missing expiry, an unknown purpose, and expiry itself.
"""

from __future__ import annotations

import jwt
import pytest

from tests.conftest import (
    AUDIENCE, CHANNEL_SECRET, ISSUER, OTHER_PEM, PUBLIC_B64, make_token, pcm,
)


def post(client, token=None, key=CHANNEL_SECRET, body=None):
    headers = {"Content-Type": "application/octet-stream"}
    if key is not None:
        headers["X-Stt-Channel-Key"] = key
    if token is not None:
        headers["X-OpenMRS-Agent-Token"] = token
    return client.post("/v1/transcribe", content=body if body is not None else pcm(), headers=headers)


# --- channel trust -------------------------------------------------------------------

def test_no_channel_key_is_403(client):
    assert post(client, token=make_token(), key=None).status_code == 403


def test_wrong_channel_key_is_403(client):
    assert post(client, token=make_token(), key="not-the-secret").status_code == 403


def test_channel_key_is_checked_before_the_token(client):
    """A bad key must not reveal whether the token would have been accepted."""
    r = post(client, token="garbage", key="also-garbage")
    assert r.status_code == 403


def test_the_agents_secret_does_not_work_here(client):
    """The whole point of §3/Q2: a compromise of one service is not a compromise of both."""
    assert post(client, token=make_token(), key="a-different-secret-entirely").status_code == 403


# --- user trust ----------------------------------------------------------------------

def test_missing_token_is_401(client):
    assert post(client, token=None).status_code == 401


def test_token_signed_by_another_key_is_refused(client):
    assert post(client, token=make_token(key=OTHER_PEM)).status_code == 401


def test_alg_none_token_is_refused(client):
    """The classic downgrade. PyJWT will not mint one, so it is built by hand."""
    import base64, json
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    import time
    now = int(time.time())
    forged = ".".join([
        seg({"alg": "none", "typ": "JWT"}),
        seg({"sub": "attacker", "purpose": "stt", "aud": AUDIENCE, "iss": ISSUER,
             "iat": now, "exp": now + 300}),
        "",
    ])
    assert post(client, token=forged).status_code == 401


def test_hs256_signed_with_the_public_key_is_refused(client):
    """Algorithm confusion: the public key used as an HMAC secret."""
    import time
    now = int(time.time())
    forged = jwt.encode(
        {"sub": "attacker", "purpose": "stt", "aud": AUDIENCE, "iss": ISSUER,
         "iat": now, "exp": now + 300},
        PUBLIC_B64, algorithm="HS256",
    )
    assert post(client, token=forged).status_code == 401


def test_tampered_payload_is_refused(client):
    token = make_token()
    head, payload, sig = token.split(".")
    other = make_token(sub="someone.else")
    assert post(client, token=f"{head}.{other.split('.')[1]}.{sig}").status_code == 401


def test_expired_token_is_refused(client):
    assert post(client, token=make_token(expires_in=-10)).status_code == 401


def test_wrong_audience_is_refused(client):
    """A token minted for the clinical agent must not drive the GPU."""
    assert post(client, token=make_token(audience="clinical-agent-service")).status_code == 401


def test_wrong_issuer_is_refused(client):
    assert post(client, token=make_token(issuer="somebody-else")).status_code == 401


@pytest.mark.parametrize("purpose", ["chat", "rollback", "read", "", "STT"])
def test_wrong_purpose_is_refused(client, purpose):
    """A chat token cannot be replayed here. §3/Q2."""
    assert post(client, token=make_token(purpose=purpose)).status_code == 401


@pytest.mark.parametrize("field", ["exp", "iat", "sub", "aud", "iss"])
def test_missing_required_claim_is_refused(client, field):
    assert post(client, token=make_token(omit=(field,))).status_code == 401


def test_blank_subject_is_refused(client):
    assert post(client, token=make_token(sub="   ")).status_code == 401


# --- the endpoints that should not exist ---------------------------------------------

@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_documentation_is_not_served(client, path):
    """It describes the hospital's clinical API surface. nginx 404s it too."""
    assert client.get(path).status_code == 404
