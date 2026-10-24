"""SQLite storage. One short-lived connection per call keeps it safe across threads."""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

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
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ui_sessions (
    token_hash TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS copies (
    master_id       TEXT NOT NULL,
    master_ticket   INTEGER NOT NULL,
    client_id       TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    master_volume   REAL NOT NULL,
    volume          REAL,
    sl              REAL NOT NULL DEFAULT 0,
    tp              REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,
    reason          TEXT,
    client_ticket   INTEGER,
    open_price      REAL,
    open_ms         REAL,
    broker_open_ms  REAL,
    close_price     REAL,
    close_ms        REAL,
    broker_close_ms REAL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (master_id, master_ticket, client_id)
);
CREATE TABLE IF NOT EXISTS master_orders (
    master_id  TEXT NOT NULL,
    ticket     INTEGER NOT NULL,
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL,
    volume     REAL NOT NULL,
    price      REAL,
    sl         REAL NOT NULL DEFAULT 0,
    tp         REAL NOT NULL DEFAULT 0,
    order_type TEXT NOT NULL DEFAULT 'market',
    status     TEXT NOT NULL,
    opened_at  TEXT NOT NULL,
    closed_at  TEXT,
    events     TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (master_id, ticket)
);
CREATE TABLE IF NOT EXISTS app_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    level   TEXT NOT NULL,
    source  TEXT NOT NULL,
    account TEXT,
    message TEXT NOT NULL
);
"""

# Tables of the removed strategy-tag trading, dropped from older databases.
OLD_TABLES = ("account_strategies", "trade_results", "trade_requests", "strategies", "api_tokens")

LOG_KEEP = 5000  # newest log entries kept; older ones are deleted
LOG_LEVELS = {"info": ("info", "warning", "error", "critical"), "warning": ("warning", "error", "critical"),
              "error": ("error", "critical")}

COPY_COLUMNS = (
    "master_id", "master_ticket", "client_id", "symbol", "side", "master_volume", "volume", "sl", "tp", "status",
    "reason", "client_ticket", "open_price", "open_ms", "broker_open_ms", "close_price", "close_ms",
    "broker_close_ms", "created_at",
)
# Copies the copier still has to look after: open or a waiting order on the client, or not known yet.
LIVE_COPY_STATUSES = ("pending", "placed", "open", "unknown")
# Master orders still on the master: a waiting limit/stop order, or an open position.
LIVE_MASTER_STATUSES = ("waiting", "open")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(master_orders)")}
            if "order_type" not in columns:  # databases from before limit orders were copied
                conn.execute("ALTER TABLE master_orders ADD COLUMN order_type TEXT NOT NULL DEFAULT 'market'")
            for table in OLD_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")

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

    # ---- app state (kill switch, UI password) ----

    def get_state(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_kill_switch(self) -> bool:
        return self.get_state("kill_switch") == "1"

    def set_kill_switch(self, on: bool) -> None:
        self.set_state("kill_switch", "1" if on else "0")

    # ---- copier ----

    def get_copier(self) -> dict:
        stored = self.get_state("copier")
        if stored is None:
            return {"masterId": None, "clients": []}
        config = json.loads(stored)
        config.pop("active", None)  # the copier ON/OFF switch was removed: each account's Trading switch decides
        return config

    def set_copier(self, config: dict) -> None:
        self.set_state("copier", json.dumps(config, sort_keys=True))

    def get_seen_tickets(self, master_id: str) -> set[int] | None:
        """Master tickets open at the last check, or None if this master was never watched."""
        stored = self.get_state(f"copier_seen:{master_id}")
        return None if stored is None else set(json.loads(stored))

    def set_seen_tickets(self, master_id: str, tickets) -> None:
        self.set_state(f"copier_seen:{master_id}", json.dumps(sorted(tickets)))

    def save_copy(self, row: dict) -> None:
        """Inserts or updates one copy (one master position on one client)."""
        self.save_copies([row])

    def save_copies(self, rows: list[dict]) -> None:
        """Inserts or updates copies in one transaction: one disk write for a whole burst."""
        placeholders = ", ".join("?" for _ in COPY_COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in COPY_COLUMNS if c != "created_at")
        stamp = now_iso()
        with self._connect() as conn:
            conn.executemany(
                f"INSERT INTO copies ({', '.join(COPY_COLUMNS)}, updated_at) VALUES ({placeholders}, ?) "
                f"ON CONFLICT(master_id, master_ticket, client_id) DO UPDATE SET {updates}, updated_at = excluded.updated_at",
                [(*(row.get(c) for c in COPY_COLUMNS), stamp) for row in rows],
            )

    def live_copies(self, master_id: str) -> list[dict]:
        marks = ", ".join("?" for _ in LIVE_COPY_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM copies WHERE master_id = ? AND status IN ({marks})", (master_id, *LIVE_COPY_STATUSES)
            ).fetchall()
        return [dict(row) for row in rows]

    def find_copy(self, master_id: str, client_id: str, master_ticket: int | None = None,
                  client_ticket: int | None = None) -> dict | None:
        """The copy of this master ticket on this client, or the one with this client ticket (newest first)."""
        column, value = ("master_ticket", master_ticket) if master_ticket is not None else ("client_ticket", client_ticket)
        with self._connect() as conn:
            row = conn.execute(f"SELECT * FROM copies WHERE master_id = ? AND client_id = ? AND {column} = ? "
                               "ORDER BY created_at DESC LIMIT 1", (master_id, client_id, value)).fetchone()
        return None if row is None else dict(row)

    def list_copies(self, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM copies ORDER BY created_at DESC, client_id LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # ---- master orders (the Orders tab) ----

    def master_order_opened(self, master_id: str, position: dict, note: str) -> None:
        stamp = now_iso()
        event = {"at": stamp, "kind": "open", "text": note}
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO master_orders (master_id, ticket, symbol, side, volume, price, sl, tp, "
                "order_type, status, opened_at, events) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (master_id, position["ticket"], position["symbol"], position["side"], position["volume"],
                 position.get("price"), position["sl"] or 0.0, position["tp"] or 0.0, position.get("type", "market"),
                 "waiting" if position.get("pending") else "open", stamp, json.dumps([event])),
            )

    def master_order_changed(self, master_id: str, ticket: int, kind: str, text: str, **values) -> None:
        """Adds an event ("modify", "fill", "cancel" or "close") to a master order and stores its new values."""
        stamp = now_iso()
        with self._connect() as conn:
            row = conn.execute("SELECT events FROM master_orders WHERE master_id = ? AND ticket = ?",
                               (master_id, ticket)).fetchone()
            if row is None:
                return  # opened before the copier started watching: not in the order log
            events = json.loads(row["events"]) + [{"at": stamp, "kind": kind, "text": text}]
            if kind in ("close", "cancel"):
                values.update(status="closed" if kind == "close" else "cancelled", closed_at=stamp)
            elif kind == "fill":
                values["status"] = "open"
            assignments = "".join(f", {column} = ?" for column in values)
            conn.execute(f"UPDATE master_orders SET events = ?{assignments} WHERE master_id = ? AND ticket = ?",
                         (json.dumps(events), *values.values(), master_id, ticket))

    def open_master_tickets(self, master_id: str) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT ticket FROM master_orders WHERE master_id = ? AND status IN (?, ?)",
                                (master_id, *LIVE_MASTER_STATUSES)).fetchall()
        return [row["ticket"] for row in rows]

    def list_master_orders(self, limit: int = 200, since: str = "", until: str = "~") -> list[dict]:
        """The newest master orders with each client's copy. since/until: ISO times (UTC) of opened_at."""
        chosen = "SELECT * FROM master_orders WHERE opened_at >= ? AND opened_at < ? ORDER BY opened_at DESC LIMIT ?"
        with self._connect() as conn:
            orders = [dict(row) for row in conn.execute(chosen, (since, until, limit)).fetchall()]
            copies = conn.execute(
                f"SELECT c.* FROM copies c JOIN ({chosen.replace('*', 'master_id, ticket', 1)}) o "
                "ON c.master_id = o.master_id AND c.master_ticket = o.ticket ORDER BY c.client_id",
                (since, until, limit)).fetchall()
        by_order: dict[tuple, list[dict]] = {}
        for row in copies:
            by_order.setdefault((row["master_id"], row["master_ticket"]), []).append(dict(row))
        for order in orders:
            order["events"] = json.loads(order["events"])
            order["copies"] = by_order.get((order["master_id"], order["ticket"]), [])
        return orders

    def master_order_logs(self, ticket: int, limit: int = 50) -> list[dict]:
        """Log entries that mention this master ticket."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM app_log WHERE message LIKE ? ORDER BY id DESC LIMIT ?",
                                (f"%{ticket}%", limit * 4)).fetchall()
        pattern = re.compile(rf"(?<!\d){ticket}(?!\d)")
        return [dict(row) for row in rows if pattern.search(row["message"])][:limit]

    # ---- app log ----

    def add_log(self, level: str, source: str, account: str | None, message: str) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO app_log (at, level, source, account, message) VALUES (?, ?, ?, ?, ?)",
                (now_iso(), level, source, account, message),
            )
            conn.execute("DELETE FROM app_log WHERE id <= ?", (cursor.lastrowid - LOG_KEEP,))

    def list_logs(self, limit: int = 200, level: str = "info") -> list[dict]:
        levels = LOG_LEVELS.get(level, LOG_LEVELS["info"])
        marks = ", ".join("?" for _ in levels)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM app_log WHERE level IN ({marks}) ORDER BY id DESC LIMIT ?", (*levels, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_logs(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM app_log")

    # ---- web UI sessions ----

    def add_session(self, token_hash: str, expires_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM ui_sessions WHERE expires_at < ?", (now_iso(),))
            conn.execute(
                "INSERT INTO ui_sessions (token_hash, expires_at) VALUES (?, ?)",
                (token_hash, expires_at.isoformat(timespec="milliseconds")),
            )

    def session_valid(self, token_hash: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM ui_sessions WHERE token_hash = ? AND expires_at > ?", (token_hash, now_iso())
            ).fetchone()
        return row is not None

    def delete_session(self, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM ui_sessions WHERE token_hash = ?", (token_hash,))

    def delete_all_sessions(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM ui_sessions")

    # ---- accounts ----

    def list_accounts(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()
        return [_account(row) for row in rows]

    def get_account(self, account_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return None if row is None else _account(row)

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


def _account(row: sqlite3.Row) -> dict:
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
    }

