"""The two independent checks every request must pass.

Lifted deliberately from the clinical agent's `app/security.py` rather than reinvented, so
the two services fail the same way for the same reasons. Two differences, both intentional:

* the channel secret is **this service's own** (STT-PLAN.md §3/Q2);
* the token's ``purpose`` must be ``stt`` and its audience ``stt-service``, so a chat token
  cannot drive the GPU and an STT token cannot open a chat turn.

There is no code path here that accepts an identity asserted any other way - not a request
field, not a header, not a query parameter.
"""

from __future__ import annotations

import base64
import hmac
from dataclasses import dataclass
from typing import Any, Dict, Optional

import jwt
from cryptography.hazmat.primitives.serialization import load_der_public_key

from .config import settings


class ChannelAuthError(Exception):
    """The caller did not prove it is the hospital's OpenMRS instance."""


class TokenError(Exception):
    """The delegated token is missing, malformed, unsigned by OpenMRS, or expired."""


@dataclass(frozen=True)
class ActingUser:
    """Who this dictation runs as. Only ever built from a signature-verified token."""

    username: str
    user_uuid: Optional[str]
    purpose: str


def verify_channel_secret(presented: Optional[str]) -> None:
    """Constant-time, so the endpoint cannot be used to guess the secret."""
    if not presented or not hmac.compare_digest(presented, settings.channel_secret):
        raise ChannelAuthError("The caller did not present a valid channel key")


def _public_key():
    try:
        return load_der_public_key(base64.b64decode(settings.jwt_public_key_b64))
    except Exception as exc:
        raise TokenError(f"The token verification key is unusable: {type(exc).__name__}: {exc}") from exc


def verify_delegated_token(token: Optional[str]) -> ActingUser:
    """Verifies the token and returns the clinician it names.

    RS256 is pinned. Passing a public key while allowing a symmetric algorithm would let
    anyone holding that (public!) key sign their own tokens - the classic JWT
    algorithm-confusion attack.
    """
    if not token:
        raise TokenError("No delegated token was presented")

    try:
        claims: Dict[str, Any] = jwt.decode(
            token,
            _public_key(),
            algorithms=["RS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("The delegated token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"The delegated token could not be trusted: {exc}") from exc

    username = claims.get("sub")
    if not isinstance(username, str) or not username.strip():
        raise TokenError("The delegated token carries no usable subject")

    purpose = claims.get("purpose")
    if purpose != settings.token_purpose:
        raise TokenError(
            f"The delegated token was minted for {purpose!r}, not for dictation"
        )

    return ActingUser(
        username=username.strip(),
        user_uuid=claims.get("user_uuid"),
        purpose=purpose,
    )
