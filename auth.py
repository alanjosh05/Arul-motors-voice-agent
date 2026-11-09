"""Staff login for the CRM: password hashing and signed access tokens.

Standard library only:
- Passwords: scrypt with a random salt, stored as "scrypt$n$r$p$salt$hash".
- Tokens: base64url(JSON payload) + "." + base64url(HMAC-SHA256 signature),
  signed with AUTH_SECRET. They carry the user id, username, role, expiry and
  the user's token version: bumping it (password change, "sign out
  everywhere") makes every token issued before it invalid.
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
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 128

# Passwords guessed first in real attacks (and obvious ones for this showroom).
# Compared case-insensitively with digits and symbols at the end removed, so
# "Password@123" and "arulmotors2026" are caught too.
COMMON_PASSWORDS = {
    "password", "passw0rd", "qwerty", "qwertyuiop", "asdfgh", "zxcvbn", "abc", "abcd", "abcdef",
    "letmein", "welcome", "admin", "administrator", "root", "user", "login", "test", "guest",
    "iloveyou", "monkey", "dragon", "master", "sunshine", "princess", "football", "cricket",
    "india", "chennai", "tamilnadu", "yamaha", "arul", "arulmotors", "motors", "nazareth",
    "thoothukudi", "tuticorin", "showroom", "service", "changeme", "secret",
}

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


# A real hash of a random password: a sign-in for a username that doesn't
# exist is checked against it, so it takes as long as a wrong password and
# the reply time doesn't reveal which usernames exist.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def verify_password_or_dummy(password: str, stored: str | None) -> bool:
    """verify_password, spending the same time when there's no such user."""
    if stored is None:
        verify_password(password, _DUMMY_HASH)
        return False
    return verify_password(password, stored)


def password_problem(password: str, username: str | None = None) -> str | None:
    """Why a new password isn't acceptable, or None."""
    password = password or ""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
    lowered = password.lower()
    core = lowered.rstrip("0123456789!@#$%^&*._-+= ")
    if len(set(lowered)) < 4 or lowered.isdigit():
        return "Password is too simple. Mix words, numbers and symbols."
    if core in COMMON_PASSWORDS or lowered in COMMON_PASSWORDS:
        return "That password is too common. Choose one that's hard to guess."
    if username and len(username) >= 3 and username.strip().lower() in lowered:
        return "Password must not contain the username."
    return None


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def create_token(user: dict, ttl_secs: int = TOKEN_TTL_SECS) -> str:
    payload = {
        "sub": user["id"],
        "username": user["username"],
        "role": user["role"],
        "ver": int(user.get("token_version") or 0),
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
