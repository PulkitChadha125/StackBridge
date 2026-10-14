"""FastAPI app: REST endpoints under /v1 and the web UI at /."""

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic.alias_generators import to_camel

from . import __version__
from .applog import AppLog
from .config import Settings, load_settings
from .copier import Copier
from .db import Database
from .models import (AccountIn, AccountPatch, CopierIn, LoginIn, PasswordSetupIn, StrategyAccounts, StrategyIn,
                     StrategyPatch, TradeIn)
from .security import PasswordCipher, hash_password, hash_token, new_session_token, verify_password
from .trading import ApiError, RateLimiter, TradeService
from .workers import WorkerGone, WorkerManager
from .workers.backends import normalize_terminal_path, terminal_path_problem

log = logging.getLogger("stakbridge.api")
STATIC_DIR = Path(__file__).parent / "static"
# Fields that require the worker to log in again when they change.
CONNECTION_FIELDS = {"login", "server", "password", "terminal_path", "enabled"}

PASSWORD_KEY = "ui_password"
SESSION_COOKIE = "stakbridge_session"
SESSION_LIFETIME = timedelta(days=30)
UI_HEADER = "X-StakBridge"
LOGIN_ATTEMPTS_PER_MINUTE = 10
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    applog = AppLog(db)
    cipher = PasswordCipher(settings.key_path)
    workers = WorkerManager(settings)
    trades = TradeService(db, workers, settings.trade_timeout)
    copier = Copier(db, workers, settings)
    limiter = RateLimiter(settings.trade_rate_limit)
    login_limiter = RateLimiter(LOGIN_ATTEMPTS_PER_MINUTE)

    def sync_worker(account: dict) -> None:
        if account["enabled"]:
            # The copier's master is watched: its worker reports every position change.
            workers.ensure(account, cipher.decrypt(account["password_enc"]), watch=account["id"] == copier.master_id)
        else:
            workers.remove(account["id"])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        applog.start()
        workers.start()
        copier.start()
        for account in db.list_accounts():
            fixed = normalize_terminal_path(account["terminal_path"])
            if fixed != account["terminal_path"]:  # e.g. a folder saved before folders were accepted
                db.update_account(account["id"], {"terminal_path": fixed})
                account["terminal_path"] = fixed
                log.info("terminal path of %s corrected to %s", account["id"], fixed, extra={"account": account["id"]})
            try:
                sync_worker(account)
            except InvalidToken:
                log.error("cannot decrypt the password of %s (secret key changed?), re-enter it in the UI", account["id"])
        log.info("StakBridge %s started with backend=%s", __version__, settings.backend)
        yield
        copier.stop()
        workers.stop_all()
        applog.stop()

    app = FastAPI(title="StakBridge", version=__version__, lifespan=lifespan)
    app.state.settings, app.state.db, app.state.workers, app.state.copier = settings, db, workers, copier

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(RequestValidationError)
    async def bad_payload(_: Request, exc: RequestValidationError):
        # Drop the echoed input so a password in a bad request never comes back in the response.
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]) or "body", "problem": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=400, content={"error": {
            "code": "BAD_PAYLOAD", "message": "the request body is not valid", "details": details}})

    def require_auth(request: Request, authorization: str | None = Header(None)) -> str:
        """Programs send the API token. The web UI sends its session cookie after a password login.

        Returns a key identifying the caller (used for rate limiting)."""
        if authorization:
            if not authorization.lower().startswith("bearer "):
                raise ApiError(401, "UNAUTHORIZED", "use 'Authorization: Bearer <token>'")
            token_hash = hash_token(authorization[7:].strip())
            if not db.token_exists(token_hash):
                raise ApiError(401, "UNAUTHORIZED", "invalid token")
            return token_hash
        session = request.cookies.get(SESSION_COOKIE)
        if session and db.session_valid(hash_token(session)):
            # Another website cannot add this header, so it cannot ride on the browser's cookie.
            if request.method not in ("GET", "HEAD") and request.headers.get(UI_HEADER) != "1":
                raise ApiError(403, "FORBIDDEN", f"browser requests must send the {UI_HEADER} header")
            return "web-ui"
        raise ApiError(401, "UNAUTHORIZED", "sign in to the web UI, or send an API token")

    def start_session(request: Request, response: Response) -> None:
        token = new_session_token()
        db.add_session(hash_token(token), datetime.now(timezone.utc) + SESSION_LIFETIME)
        response.set_cookie(
            SESSION_COOKIE, token, max_age=int(SESSION_LIFETIME.total_seconds()),
            httponly=True, samesite="strict", secure=request.url.scheme == "https",
        )

    def is_local_request(request: Request) -> bool:
        # Behind a reverse proxy every request looks local, so a forwarded request never counts.
        forwarded = any(h in request.headers for h in ("x-forwarded-for", "forwarded", "x-real-ip"))
        return not forwarded and request.client is not None and request.client.host in LOCAL_HOSTS

    # ---- web UI password login (no authentication needed to reach these) ----

    session_api = APIRouter(prefix="/v1/session")

    @session_api.get("")
    def session_status(request: Request):
        session = request.cookies.get(SESSION_COOKIE)
        return {
            "passwordSet": db.get_state(PASSWORD_KEY) is not None,
            "loggedIn": bool(session) and db.session_valid(hash_token(session)),
            "setupAllowedHere": is_local_request(request),
        }

    @session_api.post("/setup")
    def setup_password(body: PasswordSetupIn, request: Request, response: Response):
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
        if session:
            db.delete_session(hash_token(session))
        response.delete_cookie(SESSION_COOKIE)
        return {"loggedIn": False}

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
            "strategies": account["strategies"],
            "status": state["status"],
            "statusReason": state["reason"],
            "info": state["info"],
        }

    def strategy_out(strategy: dict) -> dict:
        last = db.list_trades(limit=1, strategy=strategy["tag"])
        return {
            "tag": strategy["tag"],
            "magic": strategy["magic"],
            "active": strategy["active"],
            "maxVolume": strategy["max_volume"],
            "accountIds": strategy["account_ids"],
            "lastRequest": last[0] if last else None,
        }

    def load_account(account_id: str) -> dict:
        account = db.get_account(account_id)
        if account is None:
            raise ApiError(404, "ACCOUNT_NOT_FOUND", f"account '{account_id}' does not exist")
        return account

    def load_strategy(tag: str) -> dict:
        strategy = db.get_strategy(tag)
        if strategy is None:
            raise ApiError(404, "STRATEGY_NOT_FOUND", f"strategy '{tag}' does not exist")
        return strategy

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
                raise ApiError(400, "TERMINAL_IN_USE", f"{where} is already used by account '{other['id']}'")

    # ---- trading ----

    @v1.post("/trade")
    def post_trade(trade: TradeIn, caller: str = Depends(require_auth)):
        if not limiter.allow(caller):
            raise ApiError(429, "RATE_LIMITED", f"more than {settings.trade_rate_limit} trade requests per minute")
        return trades.execute(trade)

    @v1.get("/trades")
    def list_trades(limit: int = 100, strategy: str | None = None):
        return {"trades": db.list_trades(limit=max(1, min(limit, 1000)), strategy=strategy)}

    @v1.get("/trades/{request_id:path}")  # :path so ids containing "/" still work
    def get_trade(request_id: str):
        trade = db.get_trade(request_id)
        if trade is None:
            raise ApiError(404, "TRADE_NOT_FOUND", f"no request with id '{request_id}'")
        return trade

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
                config.update(masterId=None, active=False)
            config["clients"] = [c for c in config["clients"] if c["accountId"] != account_id]
            db.set_copier(config)
            copier.configure(config)
        workers.remove(account_id)
        db.delete_account(account_id)
        copier.reload()
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

    # ---- strategies ----

    @v1.get("/strategies")
    def list_strategies():
        return {"strategies": [strategy_out(s) for s in db.list_strategies()]}

    @v1.post("/strategies", status_code=201)
    def add_strategy(body: StrategyIn):
        if db.get_strategy(body.tag) is not None:
            raise ApiError(409, "STRATEGY_EXISTS", f"strategy '{body.tag}' already exists")
        db.insert_strategy(body.tag, body.max_volume, body.active)
        return strategy_out(db.get_strategy(body.tag))

    @v1.patch("/strategies/{tag}")
    def edit_strategy(tag: str, body: StrategyPatch):
        load_strategy(tag)
        db.update_strategy(tag, body.model_dump(exclude_none=True))
        return strategy_out(db.get_strategy(tag))

    @v1.put("/strategies/{tag}/accounts")
    def set_strategy_accounts(tag: str, body: StrategyAccounts):
        load_strategy(tag)
        known = {a["id"] for a in db.list_accounts()}
        unknown = [a for a in body.account_ids if a not in known]
        if unknown:
            raise ApiError(400, "UNKNOWN_ACCOUNT", f"unknown account ids: {', '.join(unknown)}")
        db.set_strategy_accounts(tag, body.account_ids)
        return strategy_out(db.get_strategy(tag))

    # ---- app log ----

    @v1.get("/logs")
    def list_logs(limit: int = 200, level: str = "info"):
        return {"logs": db.list_logs(limit=max(1, min(limit, 2000)), level=level)}

    @v1.delete("/logs")
    def clear_logs():
        db.clear_logs()
        log.info("log cleared")
        return {"cleared": True}

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
        if body.active and not body.master_id:
            raise ApiError(400, "NO_MASTER", "choose a master account before switching the copier on")

        old_master = copier.master_id
        config = {"masterId": body.master_id, "active": body.active,
                  "clients": [{"accountId": c.account_id, "multiplier": c.multiplier, "symbols": c.symbols}
                              for c in body.clients]}
        db.set_copier(config)
        copier.configure(config)
        if old_master != body.master_id:
            if old_master:
                workers.set_watch(old_master, False)
            if body.master_id:
                workers.set_watch(body.master_id, True)
        log.warning("copier set: master=%s active=%s clients=%s", body.master_id, body.active, client_ids)
        return copier_out()

    app.include_router(session_api)
    app.include_router(v1)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app
