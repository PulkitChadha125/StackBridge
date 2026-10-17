"""FastAPI app: REST endpoints under /v1 and the web UI at /."""

import dataclasses
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic.alias_generators import to_camel

from . import __version__
from .applog import AppLog
from .config import Settings, load_settings
from .copier import Copier
from .db import Database
from .errors import ApiError, RateLimiter
from .licensing import LicenseError, Licensing, paths as licence_paths
from .licensing.client import Transport
from .models import (AccountIn, AccountPatch, CopierIn, ForgotIn, LoginIn, PasswordChangeIn, PasswordSetupIn,
                     PreferencesIn)
from .terminals import find_terminals
from .security import PasswordCipher, hash_password, hash_token, new_session_token, verify_password
from .workers import WorkerGone, WorkerManager
from .workers.backends import TERMINAL_EXE, normalize_terminal_path, terminal_path_problem

log = logging.getLogger("stakbridge.api")


class NoCacheStaticFiles(StaticFiles):
    """The web page files. "no-cache" makes the browser ask every time whether a file changed (a cheap
    304 answer when it didn't), so after an update it never mixes an old page with a new script."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response

STATIC_DIR = Path(__file__).parent / "static"
# Fields that require the worker to log in again when they change.
CONNECTION_FIELDS = {"login", "server", "password", "terminal_path", "enabled"}

PASSWORD_KEY = "ui_password"
PREFERENCES_KEY = "ui_preferences"
SESSION_COOKIE = "stakbridge_session"
SESSION_LIFETIME = timedelta(days=30)
UI_HEADER = "X-StakBridge"
LOGIN_ATTEMPTS_PER_MINUTE = 10
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
LAUNCH_TOKEN_LIFETIME = 120.0  # seconds: the desktop window opens with a one-time sign-in link


@dataclasses.dataclass
class LicenceOptions:
    """Where the licence server is and where this PC keeps its licence files and Admin folders."""
    server_url: str
    folder: Path
    public_key: Path
    transport: Transport | None = None  # tests talk to the panel without a network
    fingerprint: Callable[[], str] | None = None


def licence_from_environment() -> LicenceOptions | None:
    url = licence_paths.server_url()
    if not url:
        return None
    return LicenceOptions(url, licence_paths.device_root(), licence_paths.public_key_path())


def client_count(accounts: list) -> int:
    """Client accounts for the licence limit: every account except one, which is the master's place."""
    return max(0, len(accounts) - 1)


class Core:
    """One StakBridge on one data folder: database, broker workers and copier. Without a licence there
    is one, on the data folder. With a licence each Admin has their own, started when they sign in."""

    def __init__(self, settings: Settings, license_ok: Callable[[], bool] = lambda: True):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.db = Database(settings.db_path)
        self.applog = AppLog(self.db)
        self.cipher = PasswordCipher(settings.key_path)
        self.workers = WorkerManager(settings)
        self.copier = Copier(self.db, self.workers, settings, license_ok=license_ok)

    def sync_worker(self, account: dict) -> None:
        if account["enabled"]:
            # The copier's master is watched: its worker reports every position change.
            self.workers.ensure(account, self.cipher.decrypt(account["password_enc"]),
                                watch=account["id"] == self.copier.master_id)
        else:
            self.workers.remove(account["id"])

    def start(self) -> None:
        self.applog.start()
        self.workers.start()
        self.copier.start()
        for account in self.db.list_accounts():
            fixed = normalize_terminal_path(account["terminal_path"])
            if fixed != account["terminal_path"]:  # e.g. a folder saved before folders were accepted
                self.db.update_account(account["id"], {"terminal_path": fixed})
                account["terminal_path"] = fixed
                log.info("terminal path of %s corrected to %s", account["id"], fixed, extra={"account": account["id"]})
            try:
                self.sync_worker(account)
            except InvalidToken:
                log.error("cannot decrypt the password of %s (secret key changed?), re-enter it in the UI", account["id"])

    def stop(self) -> None:
        self.copier.stop()
        self.workers.stop_all()
        self.applog.stop()


class _Current:
    """Stands for one part (db, workers, copier, ...) of the Core in use, so the endpoints below always
    reach the signed-in Admin's own database and accounts."""

    def __init__(self, get):
        object.__setattr__(self, "_get", get)

    def __getattr__(self, name):
        return getattr(self._get(), name)


class _Sessions:
    """Signed-in browsers. Without a licence they are kept in the database (they survive a restart);
    with a licence in memory, and all of them end when the Admin signs out or another one signs in."""

    def __init__(self, db_of: Callable[[], Database] | None):
        self._db_of = db_of
        self._memory: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def add(self, token_hash: str, expires: datetime) -> None:
        if self._db_of:
            self._db_of().add_session(token_hash, expires)
        else:
            with self._lock:
                self._memory[token_hash] = expires

    def valid(self, token_hash: str) -> bool:
        if self._db_of:
            return self._db_of().session_valid(token_hash)
        with self._lock:
            expires = self._memory.get(token_hash)
        return expires is not None and expires > datetime.now(timezone.utc)

    def delete(self, token_hash: str) -> None:
        if self._db_of:
            self._db_of().delete_session(token_hash)
        else:
            with self._lock:
                self._memory.pop(token_hash, None)

    def clear(self) -> None:
        with self._lock:
            self._memory.clear()


def create_app(settings: Settings | None = None, licence: LicenceOptions | None | str = "auto") -> FastAPI:
    """licence: "auto" uses the licence server baked into the EXE or set in STAKBRIDGE_LICENSE_SERVER, if any."""
    settings = settings or load_settings()
    if licence == "auto":
        licence = licence_from_environment()
    login_limiter = RateLimiter(LOGIN_ATTEMPTS_PER_MINUTE)
    current: dict[str, Core | None] = {"core": None}
    switching = threading.RLock()
    launch_tokens: dict[str, float] = {}

    def core() -> Core:
        found = current["core"]
        if found is None:
            raise ApiError(401, "UNAUTHORIZED", "sign in to StakBridge")
        return found

    db, workers, copier, cipher = (_Current(lambda part=part: getattr(core(), part))
                                   for part in ("db", "workers", "copier", "cipher"))

    def sync_worker(account: dict) -> None:
        core().sync_worker(account)

    def publish(app_: FastAPI, found: Core | None) -> None:
        current["core"] = found
        app_.state.db = found.db if found else None
        app_.state.workers = found.workers if found else None
        app_.state.copier = found.copier if found else None

    licensing: Licensing | None = None
    if licence is None:
        sessions = _Sessions(lambda: core().db)
    else:
        sessions = _Sessions(None)

        def switch_admin(admin_id: str | None) -> None:
            """A licence sign-in or sign-out: stop the previous Admin's copier and workers, start this one's."""
            with switching:
                old = current["core"]
                publish(app, None)
                sessions.clear()
                if old is not None:
                    old.stop()
                if admin_id:
                    found = Core(dataclasses.replace(settings, data_dir=licensing.data_dir()), licensing.license_ok)
                    found.start()
                    publish(app, found)
                    log.info("StakBridge %s ready for %s", __version__, licensing.status()["username"])

        def telemetry_snapshot() -> dict:
            """Counts and the last trades for the licence provider. Never passwords, logins or servers."""
            found = core()
            accounts = found.db.list_accounts()
            master = found.copier.master_id
            watching = found.copier.status()["watching"]
            recent = [{"symbol": o["symbol"], "action": o["order_type"], "side": o["side"], "quantity": o["volume"],
                       "status": o["status"], "timestamp": o["opened_at"]} for o in found.db.list_master_orders(limit=20)]
            enabled = sum(1 for a in accounts if a["enabled"] and a["id"] != master)
            return {
                "app_version": __version__,
                "copier_status": f"watching {master}" if watching else ("master offline" if master else "no master"),
                "copier_running": watching,
                "client_count": client_count(accounts),
                "enabled_client_count": min(enabled, client_count(accounts)),
                "recent_trades": recent,
            }

        licensing = Licensing(licence.server_url, licence.folder, licence.public_key, transport=licence.transport,
                              on_admin=switch_admin, snapshot=telemetry_snapshot,
                              **({"fingerprint": licence.fingerprint} if licence.fingerprint else {}))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if licensing is None:
            current["core"].start()
        else:
            licensing.restore()
            licensing.start()
        log.info("StakBridge %s started with backend=%s%s", __version__, settings.backend,
                 f", licence server {licensing.server_url}" if licensing else "")
        yield
        if licensing is not None:
            licensing.stop()
        with switching:
            if current["core"] is not None:
                current["core"].stop()

    app = FastAPI(title="StakBridge", version=__version__, lifespan=lifespan)
    app.state.settings, app.state.licensing = settings, licensing
    if licensing is None:
        publish(app, Core(settings))
    else:
        publish(app, None)

    def make_launch_link() -> str:
        """A one-time link that signs the desktop window in (the EXE's own window, opened by this process)."""
        token = new_session_token()
        launch_tokens[hash_token(token)] = time.monotonic() + LAUNCH_TOKEN_LIFETIME
        return f"/launch?token={token}"

    app.state.make_launch_link = make_launch_link

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError):
        error = {"code": exc.code, "message": exc.message}
        if exc.details:
            error["details"] = exc.details
        if exc.retryable:
            error["retryable"] = True
        return JSONResponse(status_code=exc.status, content={"error": error})

    @app.exception_handler(RequestValidationError)
    async def bad_payload(_: Request, exc: RequestValidationError):
        # Drop the echoed input so a password in a bad request never comes back in the response.
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]) or "body", "problem": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=400, content={"error": {
            "code": "BAD_PAYLOAD", "message": "the request body is not valid", "details": details}})

    def signed_in(request: Request) -> bool:
        session = request.cookies.get(SESSION_COOKIE)
        if not session:
            return False
        if licensing is not None and (not licensing.signed_in or licensing.session_ended or current["core"] is None):
            return False
        return sessions.valid(hash_token(session))

    def check_header(request: Request) -> None:
        # Another website cannot add this header, so it cannot ride on the browser's cookie.
        if request.method not in ("GET", "HEAD") and request.headers.get(UI_HEADER) != "1":
            raise ApiError(403, "FORBIDDEN", f"browser requests must send the {UI_HEADER} header")

    def require_auth(request: Request) -> None:
        """The web UI signs in with its password and then sends its session cookie."""
        if not signed_in(request):
            raise ApiError(401, "UNAUTHORIZED", "sign in to the web UI")
        check_header(request)
        if licensing is not None and licensing.must_change_password() and not request.url.path.startswith("/v1/license"):
            raise ApiError(403, "PASSWORD_CHANGE_REQUIRED", "choose a new password first")

    def start_session(request: Request, response: Response) -> None:
        token = new_session_token()
        sessions.add(hash_token(token), datetime.now(timezone.utc) + SESSION_LIFETIME)
        response.set_cookie(
            SESSION_COOKIE, token, max_age=int(SESSION_LIFETIME.total_seconds()),
            httponly=True, samesite="strict", secure=request.url.scheme == "https",
        )

    def licence_error(exc: LicenseError) -> ApiError:
        error = ApiError(exc.status, exc.code.upper(), exc.message)
        error.retryable = exc.retryable
        return error

    def is_local_request(request: Request) -> bool:
        # Behind a reverse proxy every request looks local, so a forwarded request never counts.
        forwarded = any(h in request.headers for h in ("x-forwarded-for", "forwarded", "x-real-ip"))
        return not forwarded and request.client is not None and request.client.host in LOCAL_HOSTS

    # ---- web UI password login (no authentication needed to reach these) ----

    session_api = APIRouter(prefix="/v1/session")

    @session_api.get("")
    def session_status(request: Request):
        if licensing is not None:
            build = licence_paths.build_settings()
            return {"licensed": True, "loggedIn": signed_in(request), "licence": licensing.status(),
                    "builtFor": build.get("admin_name"), "username": build.get("admin_username")}
        return {
            "licensed": False,
            "passwordSet": db.get_state(PASSWORD_KEY) is not None,
            "loggedIn": signed_in(request),
            "setupAllowedHere": is_local_request(request),
        }

    @session_api.post("/setup")
    def setup_password(body: PasswordSetupIn, request: Request, response: Response):
        if licensing is not None:
            raise ApiError(409, "LICENSED", "sign in with your StakBridge username and password")
        if db.get_state(PASSWORD_KEY) is not None:
            raise ApiError(409, "PASSWORD_ALREADY_SET", "a password exists already, sign in instead")
        if not is_local_request(request):
            raise ApiError(403, "SETUP_LOCAL_ONLY",
                           "set the first password on the PC running StakBridge, or run: run.bat password")
        db.set_state(PASSWORD_KEY, hash_password(body.password))
        log.warning("web UI password created")
        start_session(request, response)
        return {"loggedIn": True}

    @session_api.post("")
    def login(body: LoginIn, request: Request, response: Response):
        client = request.client.host if request.client else "unknown"
        if not login_limiter.allow(client):
            raise ApiError(429, "TOO_MANY_ATTEMPTS", "too many sign-in attempts, wait a minute")
        if licensing is not None:
            check_header(request)
            if not (body.username or "").strip():
                raise ApiError(400, "USERNAME_MISSING", "enter your username")
            try:
                status = licensing.login(body.username.strip(), body.password)
            except LicenseError as exc:
                raise licence_error(exc) from None
            start_session(request, response)
            return {"loggedIn": True, "mustChangePassword": status["mustChangePassword"]}
        stored = db.get_state(PASSWORD_KEY)
        if stored is None:
            raise ApiError(409, "PASSWORD_NOT_SET", "no password yet, create one first")
        if not verify_password(body.password, stored):
            log.warning("wrong web UI password from %s", client)
            raise ApiError(401, "WRONG_PASSWORD", "wrong password")
        start_session(request, response)
        return {"loggedIn": True}

    @session_api.delete("")
    def logout(request: Request, response: Response):
        session = request.cookies.get(SESSION_COOKIE)
        if licensing is not None:
            if signed_in(request):
                check_header(request)
                licensing.logout()  # stops copying on this PC until someone signs in again
        elif session:
            sessions.delete(hash_token(session))
        response.delete_cookie(SESSION_COOKIE)
        return {"loggedIn": False}

    @session_api.post("/password")
    def change_password(body: PasswordChangeIn, request: Request):
        if licensing is None:
            raise ApiError(409, "NOT_LICENSED", "use run.bat password to change the page password")
        if not signed_in(request):
            raise ApiError(401, "UNAUTHORIZED", "sign in first")
        check_header(request)
        try:
            return licensing.change_password(body.current_password, body.new_password)
        except LicenseError as exc:
            raise licence_error(exc) from None

    @session_api.post("/forgot/{step}")
    def forgot_password(step: str, body: ForgotIn, request: Request):
        """Forgot password: email -> code -> new password. The licence server does all the checking."""
        if licensing is None or step not in ("request", "verify", "reset"):
            raise ApiError(404, "NOT_FOUND", "not available")
        check_header(request)
        fields = {"email": body.email.strip()}
        if step == "verify":
            fields["code"] = (body.code or "").strip()
        elif step == "reset":
            fields.update(reset_token=body.reset_token or "", new=body.new_password or "",
                          confirm=body.confirm_password or "")
        try:
            return licensing.forgot(step, **fields)
        except LicenseError as exc:
            raise licence_error(exc) from None

    @app.get("/launch", include_in_schema=False)
    def launch(token: str = ""):
        """The desktop window's one-time sign-in link."""
        response = RedirectResponse("/", status_code=303)
        deadline = launch_tokens.pop(hash_token(token), None) if token else None
        if deadline is not None and time.monotonic() < deadline and (licensing is None or licensing.signed_in):
            session = new_session_token()
            sessions.add(hash_token(session), datetime.now(timezone.utc) + SESSION_LIFETIME)
            response.set_cookie(SESSION_COOKIE, session, max_age=int(SESSION_LIFETIME.total_seconds()),
                                httponly=True, samesite="strict")
        return response

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])

    # ---- helpers ----

    def account_out(account: dict) -> dict:
        state = workers.status(account["id"]) if account["enabled"] else {"status": "disabled", "reason": None, "info": {}}
        return {
            "id": account["id"],
            "name": account["name"],
            "login": account["login"],
            "server": account["server"],
            "terminalPath": account["terminal_path"],
            "symbolSuffix": account["symbol_suffix"],
            "enabled": account["enabled"],
            "tradingOn": account["trading_on"],
            "status": state["status"],
            "statusReason": state["reason"],
            "info": state["info"],
        }

    def load_account(account_id: str) -> dict:
        account = db.get_account(account_id)
        if account is None:
            raise ApiError(404, "ACCOUNT_NOT_FOUND", f"account '{account_id}' does not exist")
        return account

    def clean_terminal_path(terminal_path: str) -> str:
        path = normalize_terminal_path(terminal_path)
        problem = terminal_path_problem(path) if settings.backend == "mt5" else None
        if problem:
            raise ApiError(400, "TERMINAL_PATH_INVALID",
                           f"Terminal path: {problem}. Example: C:\\Program Files\\MetaTrader 5\\terminal64.exe")
        return path

    def check_terminal_free(terminal_path: str, exclude_id: str | None = None) -> None:
        # One terminal can only be logged into one account, so two accounts must not share it.
        if settings.backend != "mt5":
            return
        wanted = terminal_path.strip().lower()
        for other in db.list_accounts():
            if other["id"] != exclude_id and other["terminal_path"].strip().lower() == wanted:
                where = f"'{terminal_path}'" if wanted else "the default terminal (empty path)"
                raise ApiError(400, "TERMINAL_IN_USE", f"{where} is already used by {other['name']}")

    # ---- licence ----

    def licence_out(found: Core) -> dict:
        return {**licensing.status(), "clients": licensing.client_summary(client_count(found.db.list_accounts()))}

    @v1.get("/license")
    def licence_status(refresh: bool = False):
        if licensing is None:
            return {"licensed": False}
        if refresh:
            licensing.check()
        return {"licensed": True, **licence_out(core())}

    # ---- stop all ----

    @v1.post("/kill")
    def kill():
        db.set_kill_switch(True)
        copier.reload()
        log.warning("kill switch turned ON")
        return {"killSwitch": True}

    @v1.delete("/kill")
    def release_kill():
        db.set_kill_switch(False)
        copier.reload()
        log.warning("kill switch released")
        return {"killSwitch": False}

    @v1.get("/health")
    def health():
        accounts = [account_out(a) for a in db.list_accounts()]
        return {
            "gateway": "ok",
            "version": __version__,
            "backend": settings.backend,
            "killSwitch": db.get_kill_switch(),
            "accounts": [{"id": a["id"], "name": a["name"], "status": a["status"], "reason": a["statusReason"]}
                         for a in accounts],
        }

    # ---- accounts ----

    @v1.get("/accounts")
    def list_accounts():
        return {"accounts": [account_out(a) for a in db.list_accounts()]}

    @v1.post("/accounts", status_code=201)
    def add_account(body: AccountIn):
        account_id = body.id or f"acct_{uuid.uuid4().hex[:8]}"
        if db.get_account(account_id) is not None:
            raise ApiError(409, "ACCOUNT_EXISTS", f"account '{account_id}' already exists")
        body.terminal_path = clean_terminal_path(body.terminal_path)
        check_terminal_free(body.terminal_path)
        if licensing is not None:
            existing = db.list_accounts()
            if client_count(existing + [None]) > client_count(existing):
                try:
                    licensing.reserve(client_count(existing + [None]), client_count(existing))
                except LicenseError as exc:
                    error = licence_error(exc)
                    if exc.code == "client_limit_reached":
                        error.details = licensing.client_summary(client_count(existing))
                    raise error from None
        db.insert_account({
            "id": account_id,
            "name": body.name,
            "login": body.login,
            "server": body.server,
            "password_enc": cipher.encrypt(body.password),
            "terminal_path": body.terminal_path,
            "symbol_suffix": body.symbol_suffix,
            "enabled": body.enabled,
        })
        account = db.get_account(account_id)
        copier.reload()
        sync_worker(account)
        return account_out(account)

    @v1.patch("/accounts/{account_id}")
    def edit_account(account_id: str, body: AccountPatch):
        load_account(account_id)
        changes = body.model_dump(exclude_none=True)
        if licensing is not None and changes.get("trading_on"):
            check = licensing.check()  # a live licence check before copying is switched on
            if not check["valid"]:
                raise ApiError(403, "LICENSE_INACTIVE", check.get("message") or "your licence is not active")
        reconnect = bool(CONNECTION_FIELDS & changes.keys())
        if "terminal_path" in changes:
            changes["terminal_path"] = clean_terminal_path(changes["terminal_path"])
            check_terminal_free(changes["terminal_path"], exclude_id=account_id)
        if "password" in changes:
            changes["password_enc"] = cipher.encrypt(changes.pop("password"))
        db.update_account(account_id, changes)
        account = db.get_account(account_id)
        copier.reload()
        if reconnect:
            sync_worker(account)
        return account_out(account)

    @v1.delete("/accounts/{account_id}")
    def remove_account(account_id: str):
        load_account(account_id)
        config = db.get_copier()
        if account_id == config["masterId"] or any(c["accountId"] == account_id for c in config["clients"]):
            if account_id == config["masterId"]:
                config["masterId"] = None
            config["clients"] = [c for c in config["clients"] if c["accountId"] != account_id]
            db.set_copier(config)
            copier.configure(config)
        workers.remove(account_id)
        before = client_count(db.list_accounts())
        db.delete_account(account_id)
        copier.reload()
        if licensing is not None:
            after = client_count(db.list_accounts())
            if after < before:
                licensing.note_removed(before - after, after)
        return {"deleted": account_id}

    @v1.get("/accounts/{account_id}/positions")
    def account_positions(account_id: str):
        load_account(account_id)
        handle = workers.get(account_id)
        if handle is None or workers.status(account_id)["status"] != "connected":
            raise ApiError(409, "DISCONNECTED", "account is not connected")
        try:
            result = handle.send("positions", magic=None).result(timeout=settings.trade_timeout)
        except (TimeoutError, WorkerGone):
            raise ApiError(504, "NO_REPLY", "the account did not answer in time") from None
        if result.get("status") != "ok":
            raise ApiError(409, result.get("reason") or "DISCONNECTED", "could not read positions")
        return {"positions": result["positions"], "orders": result["orders"]}

    @v1.get("/accounts/{account_id}/check")
    def check_account(account_id: str):
        """Reads balance, equity and connection details from the account's terminal right now."""
        load_account(account_id)
        handle = workers.get(account_id)
        if handle is None or workers.status(account_id)["status"] != "connected":
            state = workers.status(account_id)
            reason = state["reason"] or state["status"]
            raise ApiError(409, "DISCONNECTED", f"account is not connected ({reason})")
        started = time.perf_counter()
        try:
            result = handle.send("account_info").result(timeout=settings.trade_timeout)
        except (TimeoutError, WorkerGone):
            raise ApiError(504, "NO_REPLY", "the account did not answer in time") from None
        if result.get("status") != "ok":
            raise ApiError(409, "CHECK_FAILED", result.get("reason") or "the terminal did not answer")
        return {**result, "roundTripMs": round((time.perf_counter() - started) * 1000, 1),
                "checkedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}

    @v1.post("/accounts/{account_id}/open-terminal")
    def open_terminal(account_id: str):
        """Opens the account's MetaTrader window on this PC. If it is already running, MT5 shows that window."""
        account = load_account(account_id)
        if settings.backend != "mt5":
            raise ApiError(409, "SIMULATED_BROKER", "there is no MetaTrader to open with the simulated broker")
        # An empty Terminal path means the default MetaTrader: the running worker knows where it is.
        path = normalize_terminal_path(account["terminal_path"]) or workers.status(account_id)["info"].get("terminalPath", "")
        if not path:
            raise ApiError(409, "NO_TERMINAL_PATH",
                           f"no Terminal path is set for {account['name']}. Click Edit and fill in the path to {TERMINAL_EXE}")
        problem = terminal_path_problem(path)
        if problem:
            raise ApiError(409, "TERMINAL_PATH_INVALID", f"Terminal path: {problem}")
        try:
            subprocess.Popen([path], cwd=os.path.dirname(path), close_fds=True,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        except OSError as exc:
            raise ApiError(500, "CANNOT_START_TERMINAL", f"could not start {path}: {exc}") from None
        log.info("MetaTrader of %s opened from the web page", account_id, extra={"account": account_id})
        return {"opened": path}

    @v1.get("/terminals")
    def list_terminals():
        """The MetaTrader 5 terminals installed on this PC, and which account uses each one."""
        accounts = db.list_accounts()
        used_by = {os.path.normcase(os.path.normpath(a["terminal_path"])): a for a in accounts if a["terminal_path"]}
        out = []
        for terminal in find_terminals([a["terminal_path"] for a in accounts]):
            account = used_by.get(os.path.normcase(terminal["path"]))
            out.append({**terminal, "usedById": account["id"] if account else None,
                        "usedByName": account["name"] if account else None})
        return {"terminals": out}

    # ---- master trades (Trades tab) ----

    def order_out(order: dict) -> dict:
        out = {to_camel(k): v for k, v in order.items() if k != "copies"}
        out["copies"] = [{to_camel(k): v for k, v in row.items()} for row in order.get("copies", [])]
        return out

    @v1.get("/orders")
    def list_orders(limit: int = 200):
        return {"orders": [order_out(o) for o in db.list_master_orders(limit=max(1, min(limit, 1000)))]}

    @v1.get("/orders/{ticket}/logs")
    def order_logs(ticket: int):
        return {"logs": db.master_order_logs(ticket)}

    # ---- app log ----

    @v1.get("/logs")
    def list_logs(limit: int = 200, level: str = "info"):
        return {"logs": db.list_logs(limit=max(1, min(limit, 2000)), level=level)}

    @v1.delete("/logs")
    def clear_logs():
        db.clear_logs()
        log.info("log cleared")
        return {"cleared": True}

    # ---- page preferences (zoom, theme) ----

    def preferences() -> dict:
        try:
            return json.loads(db.get_state(PREFERENCES_KEY) or "{}")
        except ValueError:
            return {}

    @v1.get("/preferences")
    def get_preferences():
        return preferences()

    @v1.put("/preferences")
    def set_preferences(body: PreferencesIn):
        saved = {**preferences(), **body.model_dump(exclude_none=True)}
        db.set_state(PREFERENCES_KEY, json.dumps(saved))
        return saved

    # ---- copier ----

    def copier_out() -> dict:
        copies = [{to_camel(k): v for k, v in row.items()} for row in db.list_copies()]
        return {**db.get_copier(), **copier.status(), "copies": copies}

    @v1.get("/copier")
    def get_copier():
        return copier_out()

    @v1.put("/copier")
    def set_copier(body: CopierIn):
        accounts = {a["id"]: a for a in db.list_accounts()}
        client_ids = [c.account_id for c in body.clients]
        unknown = [a for a in [body.master_id, *client_ids] if a and a not in accounts]
        if unknown:
            raise ApiError(400, "UNKNOWN_ACCOUNT", f"unknown account ids: {', '.join(unknown)}")
        if body.master_id in client_ids:
            raise ApiError(400, "MASTER_IS_CLIENT", "the master account cannot also be a client")
        if len(set(client_ids)) != len(client_ids):
            raise ApiError(400, "DUPLICATE_CLIENT", "each client account can be listed only once")

        old_master = copier.master_id
        config = {"masterId": body.master_id,
                  "clients": [{"accountId": c.account_id, "multiplier": c.multiplier, "symbols": c.symbols}
                              for c in body.clients]}
        db.set_copier(config)
        copier.configure(config)
        if old_master != body.master_id:
            if old_master:
                workers.set_watch(old_master, False)
            if body.master_id:
                workers.set_watch(body.master_id, True)
        log.warning("copier set: master=%s clients=%s", body.master_id,
                    ", ".join(f"{c.account_id} x{c.multiplier}" for c in body.clients) or "all at x1")
        return copier_out()

    app.include_router(session_api)
    app.include_router(v1)
    app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app
