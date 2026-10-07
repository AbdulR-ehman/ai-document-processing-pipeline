"""JWT access tokens.

The signing algorithm is pinned: the ``alg`` header in a client-supplied token
is never trusted (so ``alg=none`` and HS/RS confusion attacks fail).
"""

from __future__ import annotations

import hmac

import jwt

from ..config import Settings
from ..utils.helpers import new_id, utcnow

ALGORITHM = "HS256"


class TokenError(Exception):
    """Raised for any invalid, expired or tampered token."""


def create_access_token(
    subject: int | str,
    settings: Settings,
    expires_minutes: int | None = None,
) -> str:
    """Create a signed access token.

    The role is deliberately *not* embedded: it is re-read from the database on
    every request so a stale or forged claim can never escalate privileges.
    """
    now = utcnow()
    minutes = expires_minutes if expires_minutes is not None else settings.access_token_expire_minutes
    payload = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + minutes * 60,
        "jti": new_id(),
        "typ": "access",
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str, settings: Settings) -> dict:
    """Validate a token and return its claims, or raise :class:`TokenError`."""
    if not token or not isinstance(token, str):
        raise TokenError("missing token")
    try:
        return jwt.decode(
            token,
            settings.secret_key,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "iat", "sub"], "verify_exp": True, "verify_iat": True},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid token") from exc


def constant_time_equals(left: str, right: str) -> bool:
    """Timing-safe string comparison for secrets."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
