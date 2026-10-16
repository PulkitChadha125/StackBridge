"""Web UI password and sessions, and broker password encryption."""

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


# Web UI password. scrypt is deliberately slow, so a stolen database cannot be brute-forced quickly.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)


class PasswordCipher:
    """Encrypts broker passwords at rest.

    The key comes from STAKBRIDGE_SECRET_KEY, or from a key file created in the data
    directory on first run. Losing the key means re-entering every broker password.
    """

    def __init__(self, key_path: Path):
        key = os.environ.get("STAKBRIDGE_SECRET_KEY")
        if key:
            key_bytes = key.encode("ascii")
        elif key_path.exists():
            key_bytes = key_path.read_bytes().strip()
        else:
            key_bytes = Fernet.generate_key()
            key_path.write_bytes(key_bytes)
        self._fernet = Fernet(key_bytes)

    def encrypt(self, plain: str) -> bytes:
        return self._fernet.encrypt(plain.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        return self._fernet.decrypt(token).decode("utf-8")
