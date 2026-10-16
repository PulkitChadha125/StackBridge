"""Server-side configuration for the SuperAdmin Control Plane.

All secrets (SuperAdmin login credentials, Flask session secret key) come
from environment variables or a local, git-ignored config file - never
hardcoded in source, and never sent to/embedded in the Admin EXE.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

CONFIG_PATH = Path(
    os.environ.get("SUPERADMIN_CONFIG_PATH", str(PACKAGE_DIR / "superadmin_config.json"))
)
SECRET_KEY_PATH = Path(
    os.environ.get("SUPERADMIN_SECRET_KEY_PATH", str(PACKAGE_DIR / "flask_secret_key.txt"))
)

SESSION_TTL_SECONDS = int(os.environ.get("SUPERADMIN_SESSION_TTL_SECONDS", str(24 * 3600)))
ADMIN_SESSION_TTL_SECONDS = int(os.environ.get("ADMIN_SESSION_TTL_SECONDS", str(24 * 3600)))
OFFLINE_GRACE_HOURS = int(os.environ.get("ADMIN_OFFLINE_GRACE_HOURS", "72"))

# Brute-force protection on POST /api/auth/login (Phase 13).
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = int(os.environ.get("SUPERADMIN_LOGIN_RATE_LIMIT_MAX_ATTEMPTS", "10"))
LOGIN_RATE_LIMIT_WINDOW_SECONDS = float(os.environ.get("SUPERADMIN_LOGIN_RATE_LIMIT_WINDOW_SECONDS", "300"))

# Abuse protection on the public POST /trial signup form - deliberately
# tighter than the login limiter above, since this one creates a real DB
# row and sends a real email on every successful attempt.
TRIAL_SIGNUP_RATE_LIMIT_MAX_ATTEMPTS = int(os.environ.get("TRIAL_SIGNUP_RATE_LIMIT_MAX_ATTEMPTS", "5"))
TRIAL_SIGNUP_RATE_LIMIT_WINDOW_SECONDS = float(
    os.environ.get("TRIAL_SIGNUP_RATE_LIMIT_WINDOW_SECONDS", "3600")
)

# Length of a self-serve trial, in days.
TRIAL_LENGTH_DAYS = int(os.environ.get("TRIAL_LENGTH_DAYS", "7"))

# Client accounts a self-serve trial may add, and the pre-filled value on the
# Create Admin form.
TRIAL_MAX_CLIENT_ACCOUNTS = int(os.environ.get("TRIAL_MAX_CLIENT_ACCOUNTS", "5"))
DEFAULT_MAX_CLIENT_ACCOUNTS = 5

# Shown to Admins in the app's "client limit reached" pop-up. The Settings
# page overrides both.
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "hello@thinkstak.com")
SUPPORT_WHATSAPP_URL = os.environ.get("SUPPORT_WHATSAPP_URL", "https://wa.me/918273366104")

# Email verification code sent before a trial is created.
TRIAL_CODE_TTL_SECONDS = int(os.environ.get("TRIAL_CODE_TTL_SECONDS", "600"))
TRIAL_CODE_MAX_ATTEMPTS = int(os.environ.get("TRIAL_CODE_MAX_ATTEMPTS", "5"))
TRIAL_CODE_RESEND_COOLDOWN_SECONDS = int(os.environ.get("TRIAL_CODE_RESEND_COOLDOWN_SECONDS", "60"))

# "Forgot password" email codes (SuperAdmin panel and Admin app). The TTL
# also bounds how long the "set new password" step stays open after the code
# is confirmed.
PASSWORD_RESET_CODE_TTL_SECONDS = int(os.environ.get("PASSWORD_RESET_CODE_TTL_SECONDS", "600"))
PASSWORD_RESET_CODE_MAX_ATTEMPTS = int(os.environ.get("PASSWORD_RESET_CODE_MAX_ATTEMPTS", "5"))
PASSWORD_RESET_RESEND_COOLDOWN_SECONDS = int(os.environ.get("PASSWORD_RESET_RESEND_COOLDOWN_SECONDS", "60"))
PASSWORD_RESET_RATE_LIMIT_MAX_ATTEMPTS = int(os.environ.get("PASSWORD_RESET_RATE_LIMIT_MAX_ATTEMPTS", "10"))
PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS = float(
    os.environ.get("PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS", "900")
)


# --------------------------------------------------------------------------
# Outgoing email (expiry reminders) - credentials only ever come from env
# vars, never stored in the DB or source, matching this file's own rule.
# --------------------------------------------------------------------------

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
# "SSL" (port 465, connect already encrypted) or "STARTTLS" (port 587, plain
# connect then upgrade). Hostinger's smtp.hostinger.com supports both.
SMTP_SECURITY = os.environ.get("SMTP_SECURITY", "SSL").upper()
SMTP_FROM_ADDRESS = os.environ.get("SMTP_FROM_ADDRESS") or SMTP_USERNAME

# Fallback owner address if nothing is saved on the Settings page. The
# Settings-page value (see repositories.SETTING_OWNER_NOTIFICATION_EMAIL)
# takes precedence so it can be changed without touching the server.
OWNER_NOTIFICATION_EMAIL = os.environ.get("OWNER_NOTIFICATION_EMAIL")

# How often the background thread checks for admins expiring in ~24h.
EXPIRY_REMINDER_CHECK_INTERVAL_SECONDS = int(
    os.environ.get("EXPIRY_REMINDER_CHECK_INTERVAL_SECONDS", str(3600))
)


def smtp_is_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD)


# --------------------------------------------------------------------------
# Per-Admin EXE builds (see exe_builder.py)
# --------------------------------------------------------------------------

# Folder containing the Admin app source (desktop_launcher.py + the .spec).
# Defaults to this package's parent, which is correct when the server runs
# from a full checkout; override when superadmin_panel/ is deployed alone.
EXE_BUILD_SOURCE_ROOT = os.environ.get("EXE_BUILD_SOURCE_ROOT")

# Where finished per-Admin EXEs are kept, one subfolder per admin_id.
EXE_OUTPUT_DIR = os.environ.get("EXE_OUTPUT_DIR")

# Admin App Logs uploaded on the SuperAdmin's request (see log_requests.py).
LOG_UPLOAD_DIR = os.environ.get("LOG_UPLOAD_DIR")


def log_upload_dir() -> Path:
    path = Path(LOG_UPLOAD_DIR) if LOG_UPLOAD_DIR else (PACKAGE_DIR / "log_uploads")
    path.mkdir(parents=True, exist_ok=True)
    return path


# The URL that gets compiled into a built EXE, i.e. the public HTTPS address
# Admins' machines will call. Falls back to the request's own host when unset,
# which is right for local testing and wrong for production - set it.
PUBLIC_SERVER_URL = os.environ.get("PUBLIC_SERVER_URL")


def exe_build_source_root() -> Path:
    if EXE_BUILD_SOURCE_ROOT:
        return Path(EXE_BUILD_SOURCE_ROOT)
    return PACKAGE_DIR.parent


def exe_output_dir() -> Path:
    path = Path(EXE_OUTPUT_DIR) if EXE_OUTPUT_DIR else (PACKAGE_DIR / "built_exes")
    path.mkdir(parents=True, exist_ok=True)
    return path


def public_server_url(fallback: str | None = None) -> str:
    """URL to bake into a built EXE. Explicit config wins; otherwise use the
    address the SuperAdmin's own browser reached this server on."""
    if PUBLIC_SERVER_URL:
        return PUBLIC_SERVER_URL.rstrip("/")
    return (fallback or "http://127.0.0.1:5070").rstrip("/")


def load_superadmin_credentials() -> tuple[str, str] | None:
    """Returns (username, password_hash) or None if not configured yet.

    Checked in order: SUPERADMIN_USERNAME/SUPERADMIN_PASSWORD_HASH env vars,
    then the local config file written by bootstrap_superadmin.py.
    """
    env_username = os.environ.get("SUPERADMIN_USERNAME")
    env_hash = os.environ.get("SUPERADMIN_PASSWORD_HASH")
    if env_username and env_hash:
        return env_username, env_hash

    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        username = data.get("username")
        password_hash = data.get("password_hash")
        if username and password_hash:
            return username, password_hash

    return None


def superadmin_credentials_from_env() -> bool:
    """True when the login comes from env vars, which this app can't rewrite."""
    return bool(os.environ.get("SUPERADMIN_USERNAME") and os.environ.get("SUPERADMIN_PASSWORD_HASH"))


def save_superadmin_credentials(username: str, password_hash: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps({"username": username, "password_hash": password_hash}, indent=2),
        encoding="utf-8",
    )


def get_flask_secret_key() -> str:
    env_key = os.environ.get("SUPERADMIN_SECRET_KEY")
    if env_key:
        return env_key

    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_text(encoding="utf-8").strip()

    generated = secrets.token_hex(32)
    SECRET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SECRET_KEY_PATH.write_text(generated, encoding="utf-8")
    return generated
