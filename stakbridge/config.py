"""Settings, read from environment variables with safe defaults."""

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    # "mt5" talks to real MetaTrader 5 terminals, "fake" simulates them (tests, UI work without MT5).
    backend: str = "mt5"
    # Seconds to wait for a terminal's answer to an order before showing it as UNKNOWN.
    trade_timeout: float = 10.0
    # Seconds between connection checks inside each worker. Balance, equity and open P/L are read then too.
    status_interval: float = 1.0
    # Max price slippage in points for market orders.
    deviation_points: int = 20
    # Copier: milliseconds between position checks on the master account.
    copy_poll_ms: float = 2.0
    # Copier: a master position found after a restart is copied only if it is at most this many seconds old.
    copy_max_age: float = 30.0
    # Copier: seconds between retries of client closes or SL/TP changes that did not go through.
    copy_retry_interval: float = 2.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "stakbridge.db"

    @property
    def key_path(self) -> Path:
        return self.data_dir / "secret.key"


def load_settings(**overrides) -> Settings:
    values = dict(
        data_dir=Path(os.environ.get("STAKBRIDGE_DATA_DIR", Path.cwd() / "data")),
        backend=os.environ.get("STAKBRIDGE_BACKEND", "mt5"),
        trade_timeout=float(os.environ.get("STAKBRIDGE_TRADE_TIMEOUT", "10")),
        status_interval=float(os.environ.get("STAKBRIDGE_STATUS_INTERVAL", "1")),
        deviation_points=int(os.environ.get("STAKBRIDGE_DEVIATION_POINTS", "20")),
        copy_poll_ms=float(os.environ.get("STAKBRIDGE_COPY_POLL_MS", "2")),
        copy_max_age=float(os.environ.get("STAKBRIDGE_COPY_MAX_AGE", "30")),
    )
    values.update(overrides)
    if values["backend"] not in {"mt5", "fake"}:
        raise ValueError("STAKBRIDGE_BACKEND must be 'mt5' or 'fake'")
    settings = Settings(**values)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
