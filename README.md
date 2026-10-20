# StakBridge

A trade copier for MetaTrader 5. You trade by hand (or with an EA) on one **master** account, and every trade opened, changed or closed there is copied at once to all **client** accounts.

## Quick start (Windows)

| Double-click | What it does |
| --- | --- |
| `run.bat` | Starts StakBridge with real MT5 terminals. Data goes in `data\`. |
| `run_demo.bat` | Starts StakBridge with a simulated broker, so nothing real is traded. Data goes in `data_fake\`. |
| `run_tests.bat` | Runs the automated tests |
| `run_panel.bat` | Runs the licence server (SuperAdmin panel) on this PC to try it out, on http://127.0.0.1:5070 |
| `build.bat` | Builds `dist\StakBridge.exe` by hand (normally the panel's **Build EXE** button does it) |

The first run sets up `.venv` and installs `requirements.txt`. The browser then opens http://127.0.0.1:8000.

You sign in with a **password**, created on the first visit (only possible from this PC). Reset it with `run.bat password`. You stay signed in for 30 days. Add `fake` to change the demo's password instead: `run.bat password fake`.

**How to test everything step by step: [TESTING.md](TESTING.md).**

Manual equivalent:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m stakbridge serve             # web page on http://127.0.0.1:8000
.\.venv\Scripts\python.exe -m stakbridge set-password      # set or reset the web page password
```

**One MetaTrader per account.** Each account needs its own MT5 installation (for example `C:\Program Files\MetaTrader 5 - B\terminal64.exe`) in its *Terminal path*. Two accounts sharing one MetaTrader keep logging each other out; StakBridge then shows `WRONG_ACCOUNT_IN_TERMINAL` and stops trading on that account. In each MetaTrader, turn on *Algo Trading*.

## Licence (selling StakBridge)

StakBridge is sold as a desktop program, `StakBridge.exe`, controlled by a licence server that you run: the **SuperAdmin panel** in `superadmin_panel/`. How to install it on your VPS: **[SERVER_SETUP.md](SERVER_SETUP.md)**.

| Who | Uses |
| --- | --- |
| You (SuperAdmin) | The panel: create customers (Admins) with an expiry date, number of PCs and number of client accounts, build each one's `.exe`, revoke, reset passwords, see who is online, download a PC's log |
| Your customer (Admin) | `StakBridge.exe`: signs in with the username and password you give them, changes the password at the first sign-in |
| Anyone | The free-trial page `/trial`: email, 6-digit code, a 7-day trial download. One trial per email and per PC |

What the licence does in StakBridge:
- **Sign-in** with username and password, checked by the licence server. "Forgot password" sends a code by email. The sign-in is kept for the next start (encrypted for this Windows user, DPAPI), so the window opens already signed in.
- **Licence check** at start, when a Trading switch is turned ON, and every 30 minutes. While the licence is not valid (expired, revoked) **no new trades are copied**; closes and SL/TP changes still are, so clients are never stuck in a trade. Without internet the signed offline licence works for 72 hours.
- **Client limit**: every account except one (the master's place) counts. Adding one asks the server first; the limit is shared by all the customer's PCs.
- **Telemetry** every 15 minutes: version, whether copying runs, account counts and the last 20 trades (symbol, side, lots, status). Never MetaTrader logins, passwords or servers.
- **Log requests**: the SuperAdmin can ask for up to 7 days of this PC's log files (`logs\stakbridge-YYYY-MM-DD.log`).
- **Each Admin has their own data folder**: `%LOCALAPPDATA%\StakBridge\admins\<id>\`. If the SuperAdmin deletes the Admin, the next check signs out and erases that folder.
- **Signing out stops copying** on that PC until someone signs in again.

Without a licence server (running from source with `run.bat`, as before) none of this applies and the page keeps its own password. With `STAKBRIDGE_LICENSE_SERVER=http://...` set, `run.bat` signs in with the licence too. An `.exe` without a licence server inside refuses to start.

## Progress, 17 October 2026 (version 1 finished)

- **Symbol names are matched automatically.** If a client's broker has no symbol with the master's name, the client picks its own symbol with the same name minus the broker's ending: master `XAUUSD.C` → client `XAUUSD`, `XAUUSD` → `XAUUSDm`, `#XAUUSD` → `XAUUSD.r`. The account's **Symbol suffix** and the Copier tab's **symbol mapping** still come first. If the broker has several matches (say `XAUUSD` and `XAUUSD.pro`), it prefers the one in Market Watch; if that still leaves more than one, the copy is refused and names them, so you add a mapping. The Trades view shows the name the client really used.
- **Zoom:** Ctrl + Plus / Ctrl + Minus / Ctrl + 0 and Ctrl + mouse wheel, 50% to 200%. The level and the light/dark choice are kept by StakBridge, so the EXE remembers them (its window forgets browser storage when closed).
- **Account check panel:** a "Connection OK" check hides itself after 8 seconds, ✕ closes it, and clicking the account's row shows the last check again. A check with a problem stays until you close it.
- **Full-width page** that fills the window at any zoom; each account row is one line.
- **Minimal look:** neutral colours, plain-text row buttons that brighten on hover, soft ON/OFF pills, thin borders, and small animations (sliding tab underline, fades). Movement is switched off when Windows has animations off.
- 354 automated tests pass. The new screens were tried in Chrome against the simulated broker, not yet inside the real EXE window.

## Progress, 16 October 2026

- **Limit and stop orders are copied** while they wait (placed, moved, deleted), and the trade follows when the master's order fills.
- **Licence system added**: the SuperAdmin panel (licence server, port 5070), `StakBridge.exe` per customer, sign-in, 7-day free trial on `/trial`, client limits, telemetry, log requests. Tried for real on this PC: building an EXE, signing in, limits, revoke, delete, offline use, trial.
- **Add / Edit account** is a pop-up with one Name field (the internal id is made automatically).
- **Subtle animations** and **System / Light / Dark** appearance in both StakBridge and the panel.
- **ThinkStak logo** in the header of both StakBridge and the panel (white in dark mode, dark in light mode).
- **Email works**: the default mailbox (`hello@thinkstak.com` on Hostinger) is in `email_defaults.env` (kept out of git), used by `run_panel.bat` and `setup_server.bat`. A real test email and a "New trial signup" alert arrived in the owner's Gmail. The panel's **Settings** page has a **Send Test Email** button. Every trial signup emails the owner automatically.
- **Terminal path is a drop-down** of every MetaTrader 5 found on this PC (installed programs, `%APPDATA%\MetaQuotes\Terminal\*\origin.txt`, Program Files), with a **Refresh** button. Green = new, not used by any account yet; grey = already used by another account; "Other" lets you type a path.
- **New app icon** for `StakBridge.exe` and the page icons: the ThinkStak "S" on a dark tile (`packaging/make_icon.py`).
- 331 automated tests pass.
- Still to try: the VPS setup (`setup_server.bat` on the server), HTTPS, and limit-order copying on real MetaTrader.

## How copying works

- The account with Role **Master** is watched. **Every other account is a client** and copies the master, at the same lot size unless the Copier tab sets a lot multiplier.
- Each account has a **Trading** switch. A client with Trading OFF gets no new copies; the other clients still do. The master's Trading switch turns copying of new trades on or off for everyone.
- **Stop all** blocks new copies on every account. Closes and SL/TP changes still go through, so clients always follow the master out.
- **Limit and stop orders** are copied while they wait: placed, moved and deleted on the clients together with the master. When the master's order fills, each client gets the trade (its own order if it filled already, otherwise at market). Stop-limit orders are copied once they open a trade.
- Copies carry magic `990000` and the comment `copy <master ticket>`. Trades you place yourself on a client are never touched.
- **Symbol names:** a client uses, in this order, its symbol mapping (Copier tab), the suffix rule (master's suffix removed, the client's added), and if that name does not exist on its broker, the broker's symbol with the same core name (`XAUUSD.C` → `XAUUSD`).

## How many accounts on one server

Each account runs its own MetaTrader 5 and a small worker, so RAM usually runs out before CPU. Budget about **350 MB per account** plus 2 GB for Windows, with charts closed and only the traded symbols in Market Watch. Rough guide: 4 vCPU / 8 GB ≈ 12–15 accounts, 8 vCPU / 16 GB ≈ 25–30, 16 vCPU / 32 GB ≈ 50–60. Watch the copy times in the Trades tab while adding accounts and stop when the last client gets noticeably slower, especially during news. Version 2 (clients run the StakBridge EA in their own MetaTrader) removes this limit.

## Web page

| Tab | What it shows |
| --- | --- |
| Accounts | Master and clients with live **Balance, Equity and Open P/L** (about once a second), the Trading switch, and buttons: **Open MT5** (opens that account's MetaTrader window on this PC), Check, Positions, Edit, Disable, Remove. Click a row to show or hide its last check |
| Trades | Every master trade, newest first. Click one for a pop-up showing, per account, whether it was copied, the ticket, open/close latency and any error in plain words |
| Copier | Master choice, lot multiplier and symbol names per client, latency figures, recent copies |
| Log | StakBridge's own events: connects, errors, refused orders, settings changes |

Zoom with **Ctrl + Plus / Minus / 0** or **Ctrl + mouse wheel**; the level is remembered.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

All tests run against the simulated broker.

## Settings (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `STAKBRIDGE_DATA_DIR` | `./data` | Database and encryption key location |
| `STAKBRIDGE_BACKEND` | `mt5` | `mt5` or `fake` |
| `STAKBRIDGE_TRADE_TIMEOUT` | `10` | Seconds to wait for a terminal's answer before a copy shows `unknown` |
| `STAKBRIDGE_STATUS_INTERVAL` | `1` | Seconds between connection checks; balance, equity and open P/L are read then |
| `STAKBRIDGE_DEVIATION_POINTS` | `20` | Max slippage for market orders |
| `STAKBRIDGE_COPY_POLL_MS` | `2` | How often the master's positions are checked, in milliseconds |
| `STAKBRIDGE_COPY_MAX_AGE` | `30` | After a restart, a master position StakBridge missed is copied only if it is at most this many seconds old |
| `STAKBRIDGE_LICENSE_SERVER` | inside the EXE | Licence server address. Not set and not in the EXE: no licence (local password) |
| `STAKBRIDGE_DEVICE_DIR` | `%LOCALAPPDATA%\StakBridge` | Licence files, logs and each Admin's data folder |
| `STAKBRIDGE_LICENSE_PUBLIC_KEY` | inside the EXE | The licence server's public key, to check the offline licence |
| `STAKBRIDGE_PORT` / `STAKBRIDGE_HOST` | `8000` / `127.0.0.1` | The EXE's local web server. `0.0.0.0` lets other PCs open the page (VPS) |
| `STAKBRIDGE_LICENSE_CHECK_SECONDS` | `1800` | Licence check interval |
| `STAKBRIDGE_TELEMETRY_SECONDS` | `900` | Telemetry interval |

For HTTPS, pass `--ssl-certfile` and `--ssl-keyfile` to `serve`, or put it behind a reverse proxy.

## API (used by the web page)

All under `/v1`, signed in with the session cookie. Requests that change something must send `X-StakBridge: 1`. Errors look like `{"error": {"code", "message"}}`.

- `GET/POST /accounts`, `PATCH/DELETE /accounts/{id}`, `GET /accounts/{id}/positions`, `GET /accounts/{id}/check`, `POST /accounts/{id}/open-terminal`
- `GET /copier`, `PUT /copier` with `{"masterId", "clients": [{"accountId", "multiplier", "symbols": {"XAUUSD": "GOLD"}}]}`
- `GET /orders`, `GET /orders/{ticket}/logs`: master trades and what happened on each client
- `POST/DELETE /kill`: Stop all / resume
- `GET /logs?level=info|warning|error`, `DELETE /logs`, `GET /health`
- `GET/POST/DELETE /session`, `POST /session/setup`: sign-in (with a licence: `{"username", "password"}`)
- `GET/PUT /preferences`: how the page looks, `{"zoom": 0.5–2, "theme": "system"|"light"|"dark"}` (kept by StakBridge because the EXE window forgets browser storage)
- With a licence: `POST /session/password` (change), `POST /session/forgot/request|verify|reset`, `GET /license` (`?refresh=true` asks the server now), `GET /launch?token=` (the EXE window's one-time sign-in link)

## Layout

```
stakbridge/
  api.py              REST endpoints and UI serving
  copier.py           trade copier: mirrors the master's positions onto the clients
  desktop.py          the EXE: web server + its own window
  licensing/          sign-in to the licence server, licence checks, client limit, telemetry, log requests
  logfiles.py         daily log files
  db.py               SQLite storage
  errors.py           API errors, sign-in rate limit
  models.py           request validation
  security.py         password hashing and encryption
  workers/manager.py  starts and supervises one process per account
  workers/process.py  the worker loop (one MT5 terminal each)
  workers/backends.py real MT5 calls + simulated broker
  static/             the web page (plain HTML/JS/CSS)
desktop_launcher.py   start of the EXE (StakBridge.spec)
superadmin_panel/     the licence server (SuperAdmin panel), see SERVER_SETUP.md
tests/                StakBridge tests; tests/superadmin/ the licence server's
```
