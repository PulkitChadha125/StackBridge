"""FastAPI app: REST endpoints under /v1 and the web UI at /."""

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Settings, load_settings
from .db import Database
from .models import AccountIn, AccountPatch, StrategyAccounts, StrategyIn, StrategyPatch, TradeIn
from .security import PasswordCipher, hash_token
from .trading import ApiError, RateLimiter, TradeService
from .workers import WorkerGone, WorkerManager

log = logging.getLogger("stakbridge.api")
STATIC_DIR = Path(__file__).parent / "static"
# Fields that require the worker to log in again when they change.
CONNECTION_FIELDS = {"login", "server", "password", "terminal_path", "enabled"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    cipher = PasswordCipher(settings.key_path)
    workers = WorkerManager(settings)
    trades = TradeService(db, workers, settings.trade_timeout)
    limiter = RateLimiter(settings.trade_rate_limit)

    def sync_worker(account: dict) -> None:
        if account["enabled"]:
            workers.ensure(account, cipher.decrypt(account["password_enc"]))
        else:
            workers.remove(account["id"])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        workers.start()
        for account in db.list_accounts():
            sync_worker(account)
        log.info("StakBridge %s started with backend=%s", __version__, settings.backend)
        yield
        workers.stop_all()

    app = FastAPI(title="StakBridge", version=__version__, lifespan=lifespan)
    app.state.settings, app.state.db, app.state.workers = settings, db, workers

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(RequestValidationError)
    async def bad_payload(_: Request, exc: RequestValidationError):
        # Drop the echoed input so a password in a bad request never comes back in the response.
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]), "problem": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=400, content={"error": {
            "code": "BAD_PAYLOAD", "message": "the request body is not valid", "details": details}})

    def require_token(authorization: str | None = Header(None)) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise ApiError(401, "UNAUTHORIZED", "missing bearer token")
        token_hash = hash_token(authorization[7:].strip())
        if not db.token_exists(token_hash):
            raise ApiError(401, "UNAUTHORIZED", "invalid token")
        return token_hash

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])

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
    def post_trade(trade: TradeIn, token_hash: str = Depends(require_token)):
        if not limiter.allow(token_hash):
            raise ApiError(429, "RATE_LIMITED", f"more than {settings.trade_rate_limit} trade requests per minute")
        return trades.execute(trade)

    @v1.get("/trades")
    def list_trades(limit: int = 100, strategy: str | None = None):
        return {"trades": db.list_trades(limit=max(1, min(limit, 1000)), strategy=strategy)}

    @v1.get("/trades/{request_id}")
    def get_trade(request_id: str):
        trade = db.get_trade(request_id)
        if trade is None:
            raise ApiError(404, "TRADE_NOT_FOUND", f"no request with id '{request_id}'")
        return trade

    @v1.post("/kill")
    def kill():
        db.set_kill_switch(True)
        log.warning("kill switch turned ON")
        return {"killSwitch": True}

    @v1.delete("/kill")
    def release_kill():
        db.set_kill_switch(False)
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
        sync_worker(account)
        return account_out(account)

    @v1.patch("/accounts/{account_id}")
    def edit_account(account_id: str, body: AccountPatch):
        load_account(account_id)
        changes = body.model_dump(exclude_none=True)
        reconnect = bool(CONNECTION_FIELDS & changes.keys())
        if "terminal_path" in changes:
            check_terminal_free(changes["terminal_path"], exclude_id=account_id)
        if "password" in changes:
            changes["password_enc"] = cipher.encrypt(changes.pop("password"))
        db.update_account(account_id, changes)
        account = db.get_account(account_id)
        if reconnect:
            sync_worker(account)
        return account_out(account)

    @v1.delete("/accounts/{account_id}")
    def remove_account(account_id: str):
        load_account(account_id)
        workers.remove(account_id)
        db.delete_account(account_id)
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

    app.include_router(v1)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app
