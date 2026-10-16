"""The licence of this PC: sign-in, licence checks, client limit, telemetry and log requests.

Licence check (at start, when Trading is switched ON, and every 30 minutes):
    1. Server reachable and happy: use its answer and keep the freshly signed offline licence.
    2. Server reachable and says no (revoked, expired, deleted, signed out): that wins, always.
    3. Server unreachable: use the signed offline licence until its grace time (72 hours).
    4. Nothing usable: not valid.
While the licence is not valid StakBridge copies no NEW trades. Closes and SL/TP changes from the
master still go through, so clients are never left stuck in a trade.

When the SuperAdmin deletes this Admin, the next check signs out and erases this Admin's folder.
"""

import gzip
import logging
import os
import shutil
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from .. import logfiles
from .client import LicenseClient, Transport
from .paths import admin_dir
from .store import LicenseCache, SessionFile, device_fingerprint

log = logging.getLogger("stakbridge.licence")


def _env_seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


CHECK_SECONDS = _env_seconds("STAKBRIDGE_LICENSE_CHECK_SECONDS", 1800)
TELEMETRY_SECONDS = _env_seconds("STAKBRIDGE_TELEMETRY_SECONDS", 900)
LOG_POLL_SECONDS = _env_seconds("STAKBRIDGE_LOG_REQUEST_POLL_SECONDS", 30)
DEFAULT_SUPPORT = {"email": "hello@thinkstak.com", "whatsapp_url": "https://wa.me/918273366104"}
LOG_MAX_BYTES = 16 * 1024 * 1024
UNREACHABLE = ("Can't reach the licence server right now, and adding client accounts needs it. "
               "Check this PC's internet connection and try again.")

# Kept from the server's answers (and saved in session.bin, which only this Windows user can read).
STATE_FIELDS = ("token", "admin_id", "device_id", "name", "username", "status", "expiry_date",
                "allowed_installations", "max_client_accounts", "client_usage_total", "support_contact",
                "must_change_password", "license_valid")


class LicenseError(Exception):
    """The licence server (or the way to it) said no. code is the server's error code."""

    def __init__(self, status: int, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.status, self.code, self.message, self.retryable = status, code, message, retryable


def _error(result: dict) -> LicenseError:
    if result.get("network_error"):
        return LicenseError(503, "license_server_unreachable", result["message"])
    return LicenseError(result.get("status") or 400, result["error"], result["message"],
                        bool((result.get("body") or {}).get("retryable")))


class Licensing:
    def __init__(self, server_url: str, folder: Path, public_key: Path, *, transport: Transport | None = None,
                 on_admin: Callable[[str | None], None] = lambda admin_id: None,
                 snapshot: Callable[[], dict] | None = None, fingerprint: Callable[[], str] = device_fingerprint):
        self.server_url = server_url
        self.folder = folder
        self.client = LicenseClient(server_url, transport)
        self.session_file = SessionFile(folder)
        self.cache = LicenseCache(folder, public_key)
        self.logs_folder = folder / "logs"
        self.fingerprint = fingerprint
        self.on_admin = on_admin  # called with the Admin id after sign-in, None after sign-out
        self.snapshot = snapshot  # telemetry: counts and recent trades, never secrets
        self._state: dict | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.log_poll_seconds = LOG_POLL_SECONDS  # the server may ask for another pace
        self.last_check: dict = {"valid": False, "reason": "not_signed_in", "message": "Sign in to StakBridge."}

    # ---- state ----

    @property
    def signed_in(self) -> bool:
        return self._state is not None

    @property
    def admin_id(self) -> str | None:
        return self._state.get("admin_id") if self._state else None

    @property
    def token(self) -> str | None:
        return self._state.get("token") if self._state else None

    def license_ok(self) -> bool:
        return bool(self._state and self._state.get("license_valid"))

    @property
    def session_ended(self) -> bool:
        """The server ended this sign-in (account revoked, password reset): the page must sign in again.
        This Admin's copier keeps running meanwhile, so clients still follow the master's closes."""
        return bool(self._state and self._state.get("session_ended"))

    def must_change_password(self) -> bool:
        return bool(self._state and self._state.get("must_change_password"))

    def data_dir(self) -> Path | None:
        return admin_dir(self.folder, self.admin_id) if self.admin_id else None

    def status(self) -> dict:
        state = self._state or {}
        return {
            "signedIn": self.signed_in, "valid": self.license_ok(), "reason": self.last_check.get("reason"),
            "message": self.last_check.get("message"), "graceUntil": self.last_check.get("grace_until"),
            "name": state.get("name"), "username": state.get("username"), "expiryDate": state.get("expiry_date"),
            "status": state.get("status"), "mustChangePassword": self.must_change_password(),
            "server": self.server_url,
        }

    def _remember(self, data: dict, *, replace: bool = False) -> None:
        with self._lock:
            fields = {k: data[k] for k in STATE_FIELDS if k in data}
            if replace or self._state is None:
                self._state = fields
            else:
                self._state.update(fields)
            if "max_client_accounts" in data:
                self._state["client_limit_known"] = True

    def _save(self) -> None:
        with self._lock:
            if self._state is not None:
                self.session_file.save(self._state)

    def _keep_cache(self, data: dict) -> None:
        if data.get("license_cache"):
            self.cache.save(data["license_cache"])

    # ---- sign in / out ----

    def restore(self) -> None:
        """Signs in again with the session saved at the last run, if it is still readable here."""
        saved = self.session_file.load()
        if not saved or not saved.get("token"):
            return
        self._remember(saved, replace=True)
        self.on_admin(self.admin_id)
        self.check()
        self._save()

    def login(self, username: str, password: str) -> dict:
        result = self.client.login(username, password, self.fingerprint())
        if not result["ok"]:
            raise _error(result)
        data = result["data"]
        previous = self.admin_id
        self._remember(data, replace=True)
        self._state.setdefault("license_valid", True)
        self._keep_cache(data)
        self._save()
        self.last_check = {"valid": self.license_ok(), "reason": "live",
                           "message": None if self.license_ok() else "Your licence is not active."}
        if previous != self.admin_id or previous is None:
            self.on_admin(self.admin_id)
        log.warning("signed in to the licence server as %s", self._state.get("username"))
        threading.Thread(target=self.sync_telemetry, daemon=True, name="telemetry-now").start()
        return self.status()

    def logout(self) -> None:
        token = self.token
        if token:
            self.client.logout(token)
        self._forget()
        log.warning("signed out of the licence")

    def _forget(self) -> None:
        with self._lock:
            self._state = None
        self.session_file.clear()
        self.last_check = {"valid": False, "reason": "not_signed_in", "message": "Sign in to StakBridge."}
        self.on_admin(None)

    def change_password(self, current: str, new: str) -> dict:
        if not self.token:
            raise LicenseError(401, "not_signed_in", "Sign in first.")
        result = self.client.change_password(self.token, current, new)
        if not result["ok"]:
            raise _error(result)
        self._remember(result["data"])  # a new token: the old sessions were all ended
        self._keep_cache(result["data"])
        self._save()
        log.warning("licence password changed")
        return self.status()

    def forgot(self, step: str, **fields) -> dict:
        call = {"request": self.client.forgot_request, "verify": self.client.forgot_verify,
                "reset": self.client.forgot_reset}[step]
        result = call(**fields)
        if not result["ok"]:
            raise _error(result)
        return result["data"]

    # ---- licence check ----

    def check(self) -> dict:
        """Asks the server now (see the module notes for the rules). Returns {"valid", "reason", "message"}."""
        token = self.token
        if not token:
            return self.last_check
        result = self.client.refresh(token, self.admin_id)
        if result["ok"]:
            data = result["data"]
            self._keep_cache(data)
            self._remember({**data, "license_valid": bool(data.get("license_valid"))})
            if self.license_ok():
                check = {"valid": True, "reason": "live", "message": None}
            elif data.get("status") == "revoked":
                check = {"valid": False, "reason": "admin_revoked",
                         "message": "This account has been revoked. Contact your provider."}
            else:
                check = {"valid": False, "reason": "license_expired",
                         "message": f"Your licence expired on {data.get('expiry_date')}. Contact your provider."}
        elif result.get("network_error"):
            check = self._offline_check()
        else:
            if result["error"] == "admin_deleted":
                self._erase_deleted_admin()
                check = {"valid": False, "reason": "admin_deleted", "message": result["message"]}
                self.last_check = check
                return check
            self._remember({"license_valid": False})
            check = {"valid": False, "reason": result["error"], "message": result["message"]}
            if result["error"] == "invalid_session":
                with self._lock:
                    self._state["session_ended"] = True
                check["message"] = ("Your sign-in has ended: your provider revoked the account or reset its password. "
                                    "Sign in again.")
        if self.last_check.get("valid") and not check["valid"]:
            log.error("licence not valid any more (%s): %s New trades are not copied until it is.",
                      check["reason"], check["message"])
        self.last_check = check
        self._save()
        return check

    def _offline_check(self) -> dict:
        payload = self.cache.load()
        if payload is None or payload.get("admin_id") != self.admin_id:
            self._remember({"license_valid": False})
            return {"valid": False, "reason": "no_cache",
                    "message": "The licence server can't be reached and there is no offline licence on this PC."}
        grace = datetime.fromisoformat(payload["grace_until"])
        if grace.tzinfo is None:
            grace = grace.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > grace:
            self._remember({"license_valid": False})
            return {"valid": False, "reason": "grace_expired", "grace_until": payload["grace_until"],
                    "message": "The licence server has been unreachable for too long. Reconnect to the internet."}
        valid = bool(payload.get("license_valid"))
        fields = {"license_valid": valid, "expiry_date": payload.get("expiry_date")}
        if "max_client_accounts" in payload:  # signed by the server, so it can be trusted offline
            fields["max_client_accounts"] = payload["max_client_accounts"]
        self._remember(fields)
        return {"valid": valid, "reason": "offline_grace", "grace_until": payload["grace_until"],
                "message": None if valid else "Your saved licence is not active."}

    def _erase_deleted_admin(self) -> None:
        """The SuperAdmin deleted this Admin: sign out and erase their folder (accounts, trades, keys)."""
        folder = self.data_dir()
        self._forget()  # stops copying and closes this Admin's database first
        self.cache.clear()
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
        log.error("this Admin was deleted by the licence provider: signed out and local data erased")

    # ---- client limit ----

    def client_summary(self, this_pc: int) -> dict:
        state = self._state or {}
        known = bool(state.get("client_limit_known"))
        limit = state.get("max_client_accounts") if known else None
        return {"thisPc": this_pc, "limitKnown": known, "unlimited": known and limit is None, "limit": limit,
                "total": state.get("client_usage_total"), "support": state.get("support_contact") or DEFAULT_SUPPORT}

    def reserve(self, new_count: int, this_pc: int) -> None:
        """Asks the server before this PC holds new_count client accounts. Raises LicenseError when not allowed."""
        result = self.client.reserve_clients(self.token, new_count)
        if result["ok"]:
            self._remember(result["data"])
            return
        if result.get("network_error"):
            raise LicenseError(503, "license_server_unreachable", UNREACHABLE)
        if result.get("status") == 404:
            return  # a server from before client limits
        if result["error"] == "client_limit_reached":
            self._remember(result.get("body") or {})
        raise _error(result)

    def note_removed(self, removed: int, new_count: int) -> None:
        """Clients were removed here: show the freed slots now, tell the server in the background
        (if that fails, the next telemetry report carries the right count anyway)."""
        with self._lock:
            if self._state and isinstance(self._state.get("client_usage_total"), int):
                self._state["client_usage_total"] = max(0, self._state["client_usage_total"] - removed)

        def report():
            result = self.client.reserve_clients(self.token, new_count)
            if result["ok"]:
                self._remember(result["data"])

        if self.token:
            threading.Thread(target=report, daemon=True, name="client-count").start()

    # ---- telemetry and log requests ----

    def sync_telemetry(self) -> dict:
        if not self.token or self.snapshot is None:
            return {"ok": False, "error": "not_signed_in"}
        try:
            snapshot = self.snapshot()
        except Exception as exc:  # the app is switching Admins
            return {"ok": False, "error": str(exc)}
        result = self.client.telemetry(self.token, snapshot)
        if result["ok"]:
            self._remember(result["data"])
        return result

    def check_log_requests(self) -> dict:
        token = self.token
        if not token:
            return {"ok": False, "error": "not_signed_in"}
        result = self.client.pending_log_request(token)
        if not result["ok"]:
            return result
        data = result["data"] or {}
        try:
            self.log_poll_seconds = min(300.0, max(10.0, float(data.get("poll_seconds") or LOG_POLL_SECONDS)))
        except (TypeError, ValueError):
            pass
        item = data.get("request")
        if not item:
            return result
        request_id = str(item.get("id") or "")
        try:
            first, last = date.fromisoformat(str(item["date_from"])), date.fromisoformat(str(item["date_to"]))
            content, lines, truncated = self._pack_logs(first, last, int(item.get("max_bytes") or LOG_MAX_BYTES))
        except Exception as exc:  # reported to the SuperAdmin instead
            return self.client.upload_logs(token, request_id, None, error=str(exc) or type(exc).__name__)
        log.info("sending %d log lines (%s to %s) to the licence provider%s", lines, first, last,
                 ", newest only" if truncated else "")
        return self.client.upload_logs(token, request_id, content, lines, truncated)

    def _pack_logs(self, first: date, last: date, max_bytes: int) -> tuple[bytes, int, bool]:
        lines = list(logfiles.read_range(self.logs_folder, first, last))
        truncated = False
        while True:
            content = gzip.compress(("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"))
            if len(content) <= max_bytes or not lines:
                return content, len(lines), truncated
            truncated = True  # too big: keep the newest lines
            keep = int(len(lines) * max_bytes / len(content) * 0.9)
            lines = lines[-keep:] if keep > 0 else []

    # ---- background ----

    def start(self) -> None:
        """Licence check, telemetry and log requests, each on its own thread so one slow call never holds up another."""
        self._stop.clear()
        if self.signed_in:
            threading.Thread(target=self.sync_telemetry, daemon=True, name="telemetry-now").start()
        jobs = (("licence-check", lambda: CHECK_SECONDS, self.check),
                ("telemetry", lambda: TELEMETRY_SECONDS, self.sync_telemetry),
                ("log-requests", lambda: self.log_poll_seconds, self.check_log_requests))
        for name, interval, job in jobs:
            thread = threading.Thread(target=self._loop, args=(interval, job), daemon=True, name=name)
            thread.start()
            self._threads.append(thread)

    def _loop(self, interval: Callable[[], float], job: Callable[[], object]) -> None:
        while not self._stop.wait(interval()):
            if self.signed_in:
                try:
                    job()
                except Exception:  # never let the thread die
                    log.exception("licence background job failed")

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()
