"""Password hashing (Argon2id), opaque token generation, and token hashing.

Session tokens are stored in the DB only as a SHA-256 hash of the raw
token, never the raw value - the raw token is returned to the caller once
(at login/refresh time) and never persisted anywhere in plaintext, so a
database leak alone does not hand out usable sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

# argon2-cffi's PasswordHasher defaults to the Argon2id variant.
_password_hasher = PasswordHasher()

_TEMP_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHash):
        return False
    except Exception:
        # Malformed/foreign hash format - treat as a verification failure,
        # not a server error.
        return False


def generate_temp_password(length: int = 12) -> str:
    return "".join(secrets.choice(_TEMP_PASSWORD_ALPHABET) for _ in range(length))


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def is_plausible_email(value: str) -> bool:
    # Deliberately loose - just enough to catch typos before they end up
    # silently swallowing an expiry reminder or a trial signup. Real
    # validation is "did the email arrive", which this can't check.
    return "@" in value and "." in value.split("@")[-1] and " " not in value
