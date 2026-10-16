"""Asymmetric signing for the offline license cache (Phase 7).

The server generates (on first use) an Ed25519 keypair. The PRIVATE key
never leaves this file/server - it is used only here to sign license
payloads. The PUBLIC key is not secret; it is copied out to the Admin
app's source tree as license_public_key.pem (see sync_public_key.py) and
bundled into the shipped EXE, where it can verify a signed payload but
can never produce a new valid signature. This is what makes the offline
cache tamper-evident: an Admin editing their local cached expiry_date/
grace_until invalidates the signature, and the EXE refuses to trust it.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PACKAGE_DIR = Path(__file__).resolve().parent
KEYS_DIR = PACKAGE_DIR / "keys"
DEFAULT_PRIVATE_KEY_PATH = KEYS_DIR / "license_signing_private.pem"
DEFAULT_PUBLIC_KEY_PATH = KEYS_DIR / "license_signing_public.pem"

_private_key_path_override: Path | None = None
_public_key_path_override: Path | None = None


def set_key_paths_override(private_path, public_path) -> None:
    """Test hook: use throwaway key files instead of the real server keypair."""
    global _private_key_path_override, _public_key_path_override
    _private_key_path_override = Path(private_path) if private_path else None
    _public_key_path_override = Path(public_path) if public_path else None


def private_key_path() -> Path:
    return _private_key_path_override or DEFAULT_PRIVATE_KEY_PATH


def public_key_path() -> Path:
    return _public_key_path_override or DEFAULT_PUBLIC_KEY_PATH


def _generate_and_persist_keypair() -> Ed25519PrivateKey:
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    private_key_path().parent.mkdir(parents=True, exist_ok=True)
    private_key_path().write_bytes(private_bytes)
    public_key_path().parent.mkdir(parents=True, exist_ok=True)
    public_key_path().write_bytes(public_bytes)
    return private_key


def load_or_create_private_key() -> Ed25519PrivateKey:
    path = private_key_path()
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    return _generate_and_persist_keypair()


def get_public_key_pem() -> bytes:
    if not public_key_path().exists():
        load_or_create_private_key()  # generates both files together
    return public_key_path().read_bytes()


def canonical_json(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_license_payload(payload: dict) -> dict:
    private_key = load_or_create_private_key()
    signature = private_key.sign(canonical_json(payload))
    return {"payload": payload, "signature": base64.b64encode(signature).decode("ascii")}
