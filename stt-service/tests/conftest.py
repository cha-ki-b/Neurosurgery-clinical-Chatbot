"""Test fixtures: a throwaway signing key, and an environment configured before import.

`app.config.settings` is built at import time, so every environment variable has to be in
place before `app` is imported anywhere. Hence the module-level setup rather than a
fixture.
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any, Dict, Optional

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# --- a signing key that exists only for this test run --------------------------------
_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _PRIVATE.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
PUBLIC_DER = _PRIVATE.public_key().public_bytes(
    encoding=serialization.Encoding.DER,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)
PUBLIC_B64 = base64.b64encode(PUBLIC_DER).decode()

# A second key, for "signed by someone else" tests.
_OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_PEM = _OTHER.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)

CHANNEL_SECRET = "test-channel-secret-not-the-agents"
ISSUER = "openmrs-agentgateway"
AUDIENCE = "stt-service"

os.environ.update({
    "STT_CHANNEL_SECRET": CHANNEL_SECRET,
    "AGENT_CHANNEL_SECRET": "a-different-secret-entirely",
    "OPENMRS_JWT_PUBLIC_KEY": PUBLIC_B64,
    "JWT_ISSUER": ISSUER,
    "STT_JWT_AUDIENCE": AUDIENCE,
    "STT_ENGINE_URL": "http://engine.invalid/v1",
    "STT_MAX_UTTERANCE_SECONDS": "30",
    "STT_SAMPLE_RATE": "16000",
    "STT_BIAS_LEXICON_PATH": "/nonexistent/lexicon.txt",
})


def make_token(
    sub: str = "dr.benali",
    purpose: str = "stt",
    audience: str = AUDIENCE,
    issuer: str = ISSUER,
    expires_in: int = 300,
    key: Optional[bytes] = None,
    algorithm: str = "RS256",
    omit: tuple = (),
) -> str:
    now = int(time.time())
    claims: Dict[str, Any] = {
        "sub": sub,
        "purpose": purpose,
        "aud": audience,
        "iss": issuer,
        "iat": now,
        "exp": now + expires_in,
        "user_uuid": "uuid-1234",
    }
    for field in omit:
        claims.pop(field, None)
    return jwt.encode(claims, key or PRIVATE_PEM, algorithm=algorithm)


def pcm(seconds: float = 1.0, amplitude: int = 4000, rate: int = 16000) -> bytes:
    """Int16LE mono samples loud enough to pass the silence guard."""
    import array
    import math
    n = int(rate * seconds)
    samples = array.array(
        "h", (int(amplitude * math.sin(2 * math.pi * 440 * i / rate)) for i in range(n))
    )
    return samples.tobytes()


def silence(seconds: float = 1.0, rate: int = 16000) -> bytes:
    import array
    return array.array("h", [0] * int(rate * seconds)).tobytes()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c
