# StakBridge v1

Send one REST request with a strategy tag, and the trade is placed at the same time on every connected MT5 account assigned to that tag. Full spec: [StakBridge_v1_Spec.md](StakBridge_v1_Spec.md).

## Quick start (Windows)

| Double-click | What it does |
| --- | --- |
| `run.bat` | Starts StakBridge with real MT5 terminals. Data goes in `data\`. |
| `run_demo.bat` | Starts StakBridge with a simulated broker, so nothing real is traded. Data goes in `data_fake\`. |
| `run_tests.bat` | Runs the automated tests |

The first run sets up `.venv` and installs `requirements.txt`. The browser then opens http://127.0.0.1:8000.

**Two ways in:**

| Who | How they sign in | How to get it |
| --- | --- | --- |
| You, in the browser | A **password** | Created on the first visit. Only possible from this PC. Reset it with `run.bat password`. Stays signed in for 30 days. |
| Programs that send trades | An **API token** in `Authorization: Bearer <token>` | `run.bat token` (shown once, stored only as a hash) |

Add `fake` to `run.bat password` or `run.bat token` to change the demo instead, for example `run.bat password fake`.

**How to test everything step by step: [TESTING.md](TESTING.md).**

Manual equivalent:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m stakbridge serve             # UI and API on http://127.0.0.1:8000
.\.venv\Scripts\python.exe -m stakbridge set-password      # set or reset the web page password
.\.venv\Scripts\python.exe -m stakbridge create-token      # API token for a signal program
```

**One terminal per account.** Copy an MT5 install folder once per account (for example `C:\MT5\acct_a\`) and put that folder's `terminal64.exe` in the account's *Terminal path*. In each terminal, turn on *Algo Trading*.

To try everything without MT5, set `$env:STAKBRIDGE_BACKEND = "fake"` before `serve`. This uses a simulated broker.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

`tests/test_acceptance.py` holds the 9 acceptance tests from section 7 of the spec. `tests/test_safety.py` covers the safety rules. Both run against the simulated broker.

## Settings (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `STAKBRIDGE_DATA_DIR` | `./data` | Database and encryption key location |
| `STAKBRIDGE_BACKEND` | `mt5` | `mt5` or `fake` |
| `STAKBRIDGE_TRADE_TIMEOUT` | `10` | Seconds to wait for all accounts before reporting `unknown` |
| `STAKBRIDGE_TRADE_RATE_LIMIT` | `120` | Trade requests per token per minute |
| `STAKBRIDGE_DEVIATION_POINTS` | `20` | Max slippage for market orders |
| `STAKBRIDGE_ALLOW_LIVE` | off | Live accounts are refused unless this is `1` |
| `STAKBRIDGE_SECRET_KEY` | key file | Password encryption key. If not set, `data/secret.key` is created. Back it up. |
| `STAKBRIDGE_COPY_POLL_MS` | `2` | Copier: how often the master's positions are checked, in milliseconds |
| `STAKBRIDGE_COPY_MAX_AGE` | `30` | Copier: after a restart, a master position StakBridge missed is copied only if it is at most this many seconds old |

For HTTPS, pass `--ssl-certfile` and `--ssl-keyfile` to `serve`, or put it behind a reverse proxy.

## Decisions made on the open questions

| Question | Decision |
| --- | --- |
| Netting accounts | Refused. The account shows `error: NETTING_NOT_SUPPORTED`. Only hedging accounts keep strategies apart. |
| Different broker lot rules | Volume is rounded **down** to each broker's lot step. Below the broker min or above its max, that account is `rejected`. The filled volume is in the result. |
| All assigned accounts offline | `200`, every account `skipped / DISCONNECTED`. `409 NO_ELIGIBLE_ACCOUNT` only when no account is assigned to the tag. |
| Same requestId, different body | `409 REQUEST_ID_REUSED`. An identical resend returns the stored result with `"replayed": true` and never trades again. |
| Stop all | Saved in the database, so it survives restarts. Blocks new **opens** only. **Closes are still allowed** so you can flatten. Release it with `DELETE /v1/kill` or *Resume trading*. |
| Paused strategy | Same rule: opens refused, closes allowed. |
| Account trading switch OFF | The account is skipped for both opens and closes, so it is not touched at all. |
| Pending orders on close | `close` closes the strategy's positions **and** cancels its pending orders. |
| Timeout | An account that does not answer in time is `unknown`. It is never resent. When the late answer arrives, the stored result is updated (`detail.lateReply`). |
| Demo only | Live accounts are blocked unless `STAKBRIDGE_ALLOW_LIVE=1`. |

## Additions to the spec

- `DELETE /v1/kill`: releases Stop all.
- `GET /v1/trades`: the trade log used by the Trades tab.
- `GET /v1/accounts/{id}/positions`: open positions and pending orders of one account (the **Positions** button in the UI).
- **Trade copier** (Copier tab): mirrors one master account onto client accounts, with a lot multiplier per client. Opens, SL/TP changes and closes are copied. Each client can map symbols that its broker names differently (for example master `XAUUSD` → client `GOLD`). Unmapped symbols use the account suffixes. `GET /v1/copier` returns the settings and recent copies; `PUT /v1/copier` with `{"masterId", "active", "clients": [{"accountId", "multiplier", "symbols": {"XAUUSD": "GOLD"}}]}` changes them. How to test it: [TESTING.md, Part 6](TESTING.md#part-6-test-the-copier).
- Appearance: **System**, **Light** or **Dark**, top right. Remembered per browser.
- **Log** tab: StakBridge's own events, kept in the database (newest 5000): connects and disconnects, connection errors in plain words, refused orders, copier problems and settings changes. `GET /v1/logs?level=info|warning|error` and `DELETE /v1/logs`.
- Terminal path: a MetaTrader folder or a quoted path is accepted and turned into the `terminal64.exe` path. In real MT5 mode a path with no program behind it is refused when you save it (`TERMINAL_PATH_INVALID`).
- Trades tab: a collapsed **Send a trade request** panel for testing without curl.
- `GET/POST/DELETE /v1/session` and `POST /v1/session/setup`: web page password sign-in. The browser gets a 30-day session cookie (HttpOnly, SameSite=Strict). Every browser request that changes something must also send the `X-StakBridge: 1` header, so other websites can't misuse the cookie. Sign-in is limited to 10 attempts per minute.
- Errors use the shape `{"error": {"code", "message"}}`. `409 KILL_SWITCH_ON` is returned for opens while stopped.
- The `accounts` table has an extra `terminal_path` column. Live connection status is kept in memory, not in the database.

## Layout

```
stakbridge/
  api.py              REST endpoints and UI serving
  trading.py          strategy-tag routing and parallel fan-out
  copier.py           trade copier: mirrors the master's positions onto the clients
  db.py               SQLite storage
  models.py           request validation
  security.py         token hashing, password encryption
  workers/manager.py  starts and supervises one process per account
  workers/process.py  the worker loop (one MT5 terminal each)
  workers/backends.py real MT5 calls + simulated broker
  static/             the web UI (plain HTML/JS/CSS)
tests/
```
