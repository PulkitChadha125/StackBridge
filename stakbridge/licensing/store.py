"""Files kept on this PC: the sign-in (session.bin), the signed offline licence (license_cache.json),
and the PC's fingerprint.

session.bin is encrypted with Windows DPAPI: only the same Windows user on the same PC can read
it, so copying it to another PC just means signing in again. license_cache.json is signed by the
licence server; this app only has the public key, so it can check the signature but never make
one, and an edited file is ignored.
"""

import base64
import hashlib
import json
import logging
import platform
import uuid
from pathlib import Path

log = logging.getLogger("stakbridge.licence")

SESSION_FILE = "session.bin"
CACHE_FILE = "license_cache.json"
_PLAIN_MARK = b"STAKBRIDGE-UNENCRYPTED-DEV-ONLY\n"
_DPAPI_NAME = "StakBridge licence session"

try:
    import win32crypt  # pywin32
except ImportError:  # not Windows: development only
    win32crypt = None


def device_fingerprint() -> str:
    """SHA-256 of the Windows MachineGuid (stable across reboots, users and renames). The server never
    sees the GUID itself."""
    raw = None
    if platform.system() == "Windows":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                raw = (winreg.QueryValueEx(key, "MachineGuid")[0] or "").strip() or None
        except OSError:
            raw = None
    raw = raw or f"fallback-node-{uuid.getnode()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SessionFile:
    def __init__(self, folder: Path):
        self.path = folder / SESSION_FILE

    def save(self, data: dict) -> None:
        payload = json.dumps(data).encode("utf-8")
        if win32crypt is not None:
            self.path.write_bytes(win32crypt.CryptProtectData(payload, _DPAPI_NAME, None, None, None, 0))
        else:
            log.warning("DPAPI is not available: the licence session is saved unencrypted (development only)")
            self.path.write_bytes(_PLAIN_MARK + payload)

    def load(self) -> dict | None:
        try:
            raw = self.path.read_bytes()
        except OSError:
            return None
        try:
            if raw.startswith(_PLAIN_MARK):
                return None if win32crypt is not None else json.loads(raw[len(_PLAIN_MARK):])
            if win32crypt is None:
                return None
            return json.loads(win32crypt.CryptUnprotectData(raw, None, None, None, 0)[1])
        except Exception:  # another user or PC, damaged file: sign in again
            return None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class LicenseCache:
    def __init__(self, folder: Path, public_key: Path):
        self.path = folder / CACHE_FILE
        self.public_key = public_key

    def save(self, signed: dict) -> None:
        self.path.write_text(json.dumps(signed), encoding="utf-8")

    def load(self) -> dict | None:
        """The cached licence if its signature is right, else None."""
        try:
            signed = json.loads(self.path.read_text(encoding="utf-8"))
            payload, signature = signed["payload"], base64.b64decode(signed["signature"])
            from cryptography.hazmat.primitives import serialization

            key = serialization.load_pem_public_key(self.public_key.read_bytes())
            key.verify(signature, json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            return payload
        except Exception:  # missing, damaged, edited or signed by another server
            return None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
