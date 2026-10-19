"""Staff login for the CRM: password hashing and signed access tokens.

Standard library only:
- Passwords: scrypt with a random salt, stored as "scrypt$n$r$p$salt$hash".
- Tokens: base64url(JSON payload) + "." + base64url(HMAC-SHA256 signature),
  signed with AUTH_SECRET. They carry the user id, username, role and expiry.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

logger = logging.getLogger("arul-motors-auth")

TOKEN_TTL_SECS = int(os.getenv("AUTH_TOKEN_TTL_HOURS", "12")) * 3600
MIN_PASSWORD_LENGTH = 8

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
_fallback_secret: str | None = None


def _secret() -> bytes:
    """AUTH_SECRET from the environment. Without it, a random per-process
    secret is used, so logins only last until the API restarts."""
    global _fallback_secret
    secret = os.getenv("AUTH_SECRET")
    if secret:
        return secret.encode()
    if _fallback_secret is None:
        _fallback_secret = secrets.token_hex(32)
        logger.warning("AUTH_SECRET is not set: staff logins will reset when the API restarts.")
    return _fallback_secret.encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(candidate, _unb64(digest))
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str | None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def create_token(user: dict, ttl_secs: int = TOKEN_TTL_SECS) -> str:
    payload = {
        "sub": user["id"],
        "username": user["username"],
        "role": user["role"],
        "exp": int(time.time()) + ttl_secs,
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signature = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def read_token(token: str) -> dict | None:
    """The token's payload if the signature is valid and it hasn't expired."""
    try:
        body, signature = token.split(".")
        expected = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload
