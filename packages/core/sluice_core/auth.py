from __future__ import annotations

import time

import jwt

ISSUER = "sluice-autoscaler"
AUDIENCE = "sluice-gateway"
ALGORITHM = "HS256"


class TokenError(Exception):
    """A worker token is missing, expired, malformed, or fails verification."""


def mint_worker_token(*, app: str, worker_id: str, key: str, ttl_s: int = 21600, now: int | None = None) -> str:
    """Mint an app-scoped worker JWT (HS256). Default TTL is 6h."""
    issued = int(now if now is not None else time.time())
    payload = {
        "app": app,
        "worker_id": worker_id,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": issued,
        "exp": issued + ttl_s,
    }
    return jwt.encode(payload, key, algorithm=ALGORITHM)


def verify_worker_token(token: str, *, key: str) -> dict:
    """Verify a worker JWT and return its claims, or raise TokenError."""
    try:
        return jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "app", "worker_id"]},
        )
    except jwt.InvalidTokenError as e:
        raise TokenError(str(e)) from e
