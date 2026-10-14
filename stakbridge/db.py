"""SQLite storage. One short-lived connection per call keeps it safe across threads."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

FIRST_MAGIC = 1001

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    login         INTEGER NOT NULL,
    server        TEXT NOT NULL,
    password_enc  BLOB NOT NULL,
    terminal_path TEXT NOT NULL DEFAULT '',
    symbol_suffix TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    trading_on    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategies (
    tag        TEXT PRIMARY KEY,
    magic      INTEGER NOT NULL UNIQUE,
    active     INTEGER NOT NULL DEFAULT 1,
    max_volume REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_strategies (
    account_id   TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    strategy_tag TEXT NOT NULL REFERENCES strategies(tag) ON DELETE CASCADE,
    PRIMARY KEY (account_id, strategy_tag)
);
CREATE TABLE IF NOT EXISTS trade_requests (
    request_id   TEXT PRIMARY KEY,
    strategy_tag TEXT NOT NULL,
    action       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    received_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_results (
    request_id TEXT NOT NULL REFERENCES trade_requests(request_id),
    account_id TEXT NOT NULL,
    status     TEXT NOT NULL,
    reason     TEXT,
    ticket     INTEGER,
    price      REAL,
    volume     REAL,
    detail     TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (request_id, account_id)
);
CREATE TABLE IF NOT EXISTS api_tokens (
    token_hash TEXT PRIMARY KEY,
    label      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class DuplicateRequest(Exception):
    """The requestId is already stored."""


class Database:
    def __init__(self, path: Path):
        self.path = path
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---- api tokens ----

    def add_token(self, token_hash: str, label: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO api_tokens (token_hash, label, created_at) VALUES (?, ?, ?)",
                (token_hash, label, now_iso()),
            )

    def token_exists(self, token_hash: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM api_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
        return row is not None

    # ---- kill switch ----

    def get_kill_switch(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_state WHERE key = 'kill_switch'").fetchone()
        return row is not None and row["value"] == "1"

    def set_kill_switch(self, on: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO app_state (key, value) VALUES ('kill_switch', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("1" if on else "0",),
            )

    # ---- accounts ----

    def list_accounts(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()
            links = conn.execute("SELECT account_id, strategy_tag FROM account_strategies ORDER BY strategy_tag").fetchall()
        tags: dict[str, list[str]] = {}
        for link in links:
            tags.setdefault(link["account_id"], []).append(link["strategy_tag"])
        return [_account(row, tags.get(row["id"], [])) for row in rows]

    def get_account(self, account_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
            if row is None:
                return None
            links = conn.execute(
                "SELECT strategy_tag FROM account_strategies WHERE account_id = ? ORDER BY strategy_tag", (account_id,)
            ).fetchall()
        return _account(row, [link["strategy_tag"] for link in links])

    def insert_account(self, values: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO accounts (id, name, login, server, password_enc, terminal_path, symbol_suffix, "
                "enabled, trading_on, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    values["id"], values["name"], values["login"], values["server"], values["password_enc"],
                    values["terminal_path"], values["symbol_suffix"], int(values["enabled"]), now_iso(),
                ),
            )

    def update_account(self, account_id: str, values: dict) -> None:
        columns = {"name", "login", "server", "password_enc", "terminal_path", "symbol_suffix", "enabled", "trading_on"}
        changes = {k: (int(v) if isinstance(v, bool) else v) for k, v in values.items() if k in columns}
        if not changes:
            return
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self._connect() as conn:
            conn.execute(f"UPDATE accounts SET {assignments} WHERE id = ?", (*changes.values(), account_id))

    def delete_account(self, account_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    # ---- strategies ----

    def list_strategies(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM strategies ORDER BY created_at").fetchall()
            links = conn.execute("SELECT account_id, strategy_tag FROM account_strategies").fetchall()
        accounts: dict[str, list[str]] = {}
        for link in links:
            accounts.setdefault(link["strategy_tag"], []).append(link["account_id"])
        return [_strategy(row, accounts.get(row["tag"], [])) for row in rows]

    def get_strategy(self, tag: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM strategies WHERE tag = ?", (tag,)).fetchone()
            if row is None:
                return None
            links = conn.execute("SELECT account_id FROM account_strategies WHERE strategy_tag = ?", (tag,)).fetchall()
        return _strategy(row, [link["account_id"] for link in links])

    def insert_strategy(self, tag: str, max_volume: float, active: bool) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(magic) AS m FROM strategies").fetchone()
            magic = max(FIRST_MAGIC, (row["m"] or 0) + 1)
            conn.execute(
                "INSERT INTO strategies (tag, magic, active, max_volume, created_at) VALUES (?, ?, ?, ?, ?)",
                (tag, magic, int(active), max_volume, now_iso()),
            )

    def update_strategy(self, tag: str, values: dict) -> None:
        changes = {k: (int(v) if isinstance(v, bool) else v) for k, v in values.items() if k in {"active", "max_volume"}}
        if not changes:
            return
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self._connect() as conn:
            conn.execute(f"UPDATE strategies SET {assignments} WHERE tag = ?", (*changes.values(), tag))

    def set_strategy_accounts(self, tag: str, account_ids: list[str]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM account_strategies WHERE strategy_tag = ?", (tag,))
            conn.executemany(
                "INSERT INTO account_strategies (account_id, strategy_tag) VALUES (?, ?)",
                [(account_id, tag) for account_id in dict.fromkeys(account_ids)],
            )

    # ---- trades ----

    def create_trade_request(self, request_id: str, tag: str, action: str, payload: dict, account_ids: list[str]) -> None:
        """Stores the request and a 'pending' row per account in one transaction.

        Raises DuplicateRequest if the requestId was stored before, which is what makes a
        resent request safe: the second one can never reach the workers.
        """
        stamp = now_iso()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO trade_requests (request_id, strategy_tag, action, payload, received_at) VALUES (?, ?, ?, ?, ?)",
                    (request_id, tag, action, json.dumps(payload, sort_keys=True), stamp),
                )
                conn.executemany(
                    "INSERT INTO trade_results (request_id, account_id, status, updated_at) VALUES (?, ?, 'pending', ?)",
                    [(request_id, account_id, stamp) for account_id in account_ids],
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateRequest(request_id) from exc

    def save_result(self, request_id: str, account_id: str, result: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE trade_results SET status = ?, reason = ?, ticket = ?, price = ?, volume = ?, detail = ?, updated_at = ? "
                "WHERE request_id = ? AND account_id = ?",
                (
                    result["status"], result.get("reason"), result.get("ticket"), result.get("price"),
                    result.get("volume"), json.dumps(result["detail"]) if result.get("detail") else None,
                    now_iso(), request_id, account_id,
                ),
            )

    def get_trade(self, request_id: str) -> dict | None:
        with self._connect() as conn:
            req = conn.execute("SELECT * FROM trade_requests WHERE request_id = ?", (request_id,)).fetchone()
            if req is None:
                return None
            rows = conn.execute(
                "SELECT * FROM trade_results WHERE request_id = ? ORDER BY account_id", (request_id,)
            ).fetchall()
        return {
            "requestId": req["request_id"],
            "strategy": req["strategy_tag"],
            "action": req["action"],
            "payload": json.loads(req["payload"]),
            "receivedAt": req["received_at"],
            "results": [_result(row) for row in rows],
        }

    def list_trades(self, limit: int = 100, strategy: str | None = None) -> list[dict]:
        where = "WHERE r.strategy_tag = ? " if strategy else ""
        params = (strategy, limit) if strategy else (limit,)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.request_id, r.strategy_tag, r.action, r.payload, r.received_at, "
                "SUM(t.status IN ('filled', 'placed', 'closed')) AS ok, COUNT(t.account_id) AS total "
                "FROM trade_requests r LEFT JOIN trade_results t ON t.request_id = r.request_id "
                f"{where}GROUP BY r.request_id ORDER BY r.received_at DESC LIMIT ?",
                params,
            ).fetchall()
        trades = []
        for row in rows:
            payload = json.loads(row["payload"])
            trades.append({
                "requestId": row["request_id"],
                "strategy": row["strategy_tag"],
                "action": row["action"],
                "symbol": payload.get("symbol"),
                "side": payload.get("side"),
                "volume": payload.get("volume"),
                "receivedAt": row["received_at"],
                "succeeded": row["ok"] or 0,
                "accounts": row["total"],
            })
        return trades


def _account(row: sqlite3.Row, strategies: list[str]) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "login": row["login"],
        "server": row["server"],
        "password_enc": row["password_enc"],
        "terminal_path": row["terminal_path"],
        "symbol_suffix": row["symbol_suffix"],
        "enabled": bool(row["enabled"]),
        "trading_on": bool(row["trading_on"]),
        "strategies": strategies,
    }


def _strategy(row: sqlite3.Row, account_ids: list[str]) -> dict:
    return {
        "tag": row["tag"],
        "magic": row["magic"],
        "active": bool(row["active"]),
        "max_volume": row["max_volume"],
        "account_ids": sorted(account_ids),
    }


def _result(row: sqlite3.Row) -> dict:
    result = {"accountId": row["account_id"], "status": row["status"]}
    for key, column in (("reason", "reason"), ("ticket", "ticket"), ("price", "price"), ("volume", "volume")):
        if row[column] is not None:
            result[key] = row[column]
    if row["detail"]:
        result["detail"] = json.loads(row["detail"])
    return result
