"""API token hashing and broker password encryption."""

import hashlib
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet


def new_api_token() -> str:
    return "sb_" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
