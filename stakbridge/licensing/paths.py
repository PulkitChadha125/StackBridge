"""Where licence files and each Admin's data live, and where bundled files are found.

Device folder (%LOCALAPPDATA%\\StakBridge): session.bin, license_cache.json, logs\\, and one
folder per Admin under admins\\<admin id>\\ with that Admin's database and key. Two Admins
signing in on one PC therefore never see each other's accounts.
"""

import json
import os
import re
import sys
from pathlib import Path

APP_DIR_NAME = "StakBridge"
ADMINS_DIR = "admins"
BUILD_SETTINGS = "build_settings.json"
PUBLIC_KEY = "license_public_key.pem"

REPO_ROOT = Path(__file__).resolve().parents[2]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Read-only files shipped with the app: inside the EXE when frozen, the project folder otherwise."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", REPO_ROOT))
    return REPO_ROOT


def device_root() -> Path:
    override = os.environ.get("STAKBRIDGE_DEVICE_DIR")
    if override:
        root = Path(override)
    elif os.environ.get("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"]) / APP_DIR_NAME
    else:
        root = Path.home() / f".{APP_DIR_NAME.lower()}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def admin_dir(root: Path, admin_id: str) -> Path:
    """The Admin id comes from the server but ends up in a path, so only safe characters are kept."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", admin_id.strip()).strip(".")[:64] or "unknown"
    return root / ADMINS_DIR / safe


def build_settings() -> dict:
    """Written into the EXE by the panel's Build EXE: server address, Admin name and username. No secrets."""
    try:
        data = json.loads((bundle_root() / BUILD_SETTINGS).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def server_url() -> str | None:
    """The licence server's address, or None when there is none (licence off)."""
    url = os.environ.get("STAKBRIDGE_LICENSE_SERVER") or build_settings().get("server_url")
    return url.strip().rstrip("/") if isinstance(url, str) and url.strip() else None


def public_key_path() -> Path:
    """The licence server's public key, to check the signed offline licence. Inside the EXE it is the key
    of the server that built it. From source it can come from a licence server run on this PC."""
    override = os.environ.get("STAKBRIDGE_LICENSE_PUBLIC_KEY")
    if override:
        return Path(override)
    bundled = bundle_root() / PUBLIC_KEY
    local_server = REPO_ROOT / "superadmin_panel" / "keys" / "license_signing_public.pem"
    if not bundled.exists() and not is_frozen() and local_server.exists():
        return local_server
    return bundled
