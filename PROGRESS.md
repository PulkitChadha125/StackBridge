# StakBridge progress notes

## 17 October 2026: version 1 finished, next is version 2

Version 1 (branch `main`: StakBridge runs one MetaTrader per account on the same PC or server) is complete. Done today:
- **Automatic symbol matching** (`match_symbol` / `symbol_core` in `stakbridge/workers/backends.py`): when the client's broker has no symbol with the name the copier asks for, it uses its own symbol with the same core name (`XAUUSD.C` → `XAUUSD`, `XAUUSD` → `XAUUSDm`). Mapping and suffix still come first; several matches → the one in Market Watch, otherwise refused with the names listed. The copy row stores the client's real symbol. Tests: `tests/test_symbols.py`.
- **Zoom** (Ctrl + Plus / Minus / 0, Ctrl + wheel) and **light/dark** remembered through `GET/PUT /v1/preferences` (stored in `app_state`), because the EXE window (pywebview, private mode) wipes browser storage on close and ignores the browser's zoom keys.
- **Check panel:** hides itself after 8 s when all is OK, ✕ to close, click the account row to show the last check again; fades in and out.
- **Layout:** full width at any zoom, one-line account rows; narrow window or zoomed in → `.narrow` (set by app.js) lets the buttons wrap.
- **Minimal redesign** of `style.css`: neutral palette, plain-text row buttons, ON/OFF pills, subtle animations (tab underline, fades), reduced-motion respected.
- 354 tests pass. Tried in Chrome against the simulated broker; **not yet inside the real EXE window or with real MetaTrader** (symbol matching).

Still to try on real MetaTrader for version 1: limit/stop order copying, symbol matching (master `XAUUSD.C` with an empty suffix → client `XAUUSD`), the new screens in the EXE. Then **version 2** (branch `v2`): the EA hub and the MQL5 client EA, which still needs the same symbol matching written in MQL5.

## 16 October 2026 (night): licence system (selling StakBridge)

Ported from the ThinkStak Forex CopyTrader licensing (reference: `Suresh Copy Trader`):
- `superadmin_panel/`: the licence server (SuperAdmin panel), unchanged in behaviour, renamed StakBridge, default port **5070** (5060 is blocked by Chrome). Server scripts `setup_server.bat` etc. and **SERVER_SETUP.md**. `run_panel.bat` runs it on this PC for trying out.
- `stakbridge/licensing/`: sign-in with username/password, forced password change, forgot password, licence checks with 72 h offline grace (signed, Ed25519), client limit (all accounts except the master's place), telemetry, log requests, one data folder per Admin, deleted Admin erased.
- No valid licence = no NEW copies (`LICENSE_INACTIVE`); closes and SL/TP still follow.
- `StakBridge.exe`: `desktop_launcher.py` + `stakbridge/desktop.py` (own window, pywebview), `StakBridge.spec`, `build.bat`, icon in `packaging/`.
- Without a licence server (run.bat from source) nothing changes: local page password as before.
- 322 tests pass (43 StakBridge, 14 licence end-to-end against the real panel, 265 panel).
- Tried for real on this PC: panel in Chrome, Create Admin, **Build EXE** (real PyInstaller), the EXE signing in, password change, client limit pop-up, telemetry, log download, revoke, expiry bar, delete + erase, restart signed in, single copy, offline grace, trial with emailed codes (emails captured, not sent), one trial per PC, forgot password.
- Not tried: real email sending (needs your SMTP settings), the VPS scripts (`setup_server.bat` needs admin rights on the server), HTTPS.

## 16 October 2026 (evening): limit and stop orders are copied

- A waiting Buy/Sell Limit or Stop on the master is placed on every client (same price, SL/TP, lots x multiplier), moved when the master's is moved, and deleted when it is deleted or expires.
- When the master's order fills, each client keeps its own fill, or its order is deleted and the trade opened at market. Stop all / master Trading OFF at that moment deletes the client orders instead.
- Trades tab shows "Buy Limit" etc., master status waiting / open / closed / cancelled. Red "Stop all is ON" bar on every tab.
- 43 tests pass. Not yet tried on real MT5.

## 16 October 2026 (afternoon): copier-only StakBridge

- **Strategy tags removed completely**: Strategies tab, `/v1/trade`, `/v1/strategies`, API tokens (`run.bat token`), `trading.py`. Old tables are dropped from the database on start.
- **Every account except the master is a client.** No client list, no copier ON/OFF switch. The master's Trading switch turns copying on/off for all; a client's Trading switch only for that client (`MASTER_TRADING_OFF`, `TRADING_OFF`).
- **Live Balance / Equity / Open P/L** per account, about once a second (`STAKBRIDGE_STATUS_INTERVAL` now 1 s).
- **Open MT5** button per account (`POST /v1/accounts/{id}/open-terminal`).
- **Trades tab** (was Orders): click a trade for a pop-up with each account's result, latency and error.
- Safety: a MetaTrader logged into a different account than expected now shows `WRONG_ACCOUNT_IN_TERMINAL` and is not traded on (two accounts sharing one MetaTrader).
- 39 tests pass. Not yet tried on real MT5.
- Note: in your current `data`, acct_2 (Client1) has an **empty Terminal path**, so it likely shares acct_a's MetaTrader. Give it its own path.

The notes below are from 14 October.


Last updated: 14 October 2026, end of day. Continue from **"Tomorrow: start here"**.

## Where we are

| Area | Status |
| --- | --- |
| StakBridge v1 (strategy-tag trading, 9 acceptance tests) | Built before this session, tests pass |
| **Trade copier** (master → clients) | Built and tested with the simulated broker. **Not yet tested on real MT5 demo accounts.** |
| Copier tab in the web page | Built, tested in the browser |
| Symbol mapping per client (e.g. master `XAUUSD` → client `GOLD`) | Built, tested in the browser |
| Appearance: System / Light / Dark | Built, tested in the browser |
| Log tab (errors, warnings, events) | Built, tested in the browser |
| Terminal path fix (folder accepted, bad path refused, plain-English MT5 errors) | Built, tested. Your acct_a fixes itself on the next start. |
| Automated tests | **48 of 48 pass** (`run_tests.bat`) |
| Git | **Nothing committed yet.** Everything from this session is uncommitted changes. |

## Tomorrow: start here

1. **Restart StakBridge** so it runs the new code. Close the black `run.bat` window if it's still open, then double-click `run.bat`.
2. Sign in with the web page password you created today. There is no user ID. If you've forgotten it: close the window, open a Command Prompt in this folder, and run `run.bat password`.
3. **Accounts** tab: acct_a (`Master A`, login 5057389188, MetaQuotes-Demo) is already added. After the restart its Terminal path is corrected to `C:\Program Files\MetaTrader 5\terminal64.exe` and it should show **Connected**. If not, open the **Log** tab.
4. Add the two clients, then set up the copier and run the tests. Follow **[TESTING.md](TESTING.md)** from **Part 4**:
   - Part 4: add acct_b and acct_c, switch Trading **ON** for all 3
   - Part 5: Copier tab: master acct_a, tick both clients, multipliers, symbol mapping if needed, **Save**, switch **ON**
   - Part 6: tests C1–C9 (trade by hand in acct_a's MetaTrader)
   - Part 7: write down the delay numbers
   - Part 8 (optional): the 9 strategy-trading tests
5. Test Monday to Friday while the market is open.
6. When the tests pass, **commit** the work (ask Claude: "commit our progress").

## Your PC

| StakBridge name | Role | Terminal path |
| --- | --- | --- |
| acct_a | Master (you trade here) | `C:\Program Files\MetaTrader 5\terminal64.exe` |
| acct_b | Client 1 | `C:\Program Files\STARTRADER Financial MetaTrader 5\terminal64.exe` |
| acct_c | Client 2 | `C:\Program Files\MetaTrader 5 - C\terminal64.exe` |

- Project folder: `D:\Desktop\python projects\StackBridge`
- Web page: http://127.0.0.1:8000
- App data (database, password key): the `data` folder. Delete it to start completely fresh (everything has to be re-added).

## What was built this session

**Trade copier** (`stakbridge/copier.py`)
- The master's worker checks its positions every 2 ms. Opens, closes and SL/TP changes are sent to all clients at the same moment, and each answer is handled as it arrives. A slow client never holds up the others.
- Client lots = master lots × multiplier, rounded down to the broker's lot step.
- Copies carry magic `990000` and the comment `copy <master ticket>`. Only those are touched. Manual client trades are left alone.
- Copier OFF, Stop all, or a client's Trading OFF block **new** copies. Closes and SL/TP changes still go through, except to a client whose Trading is OFF.
- Failed closes and SL/TP changes are retried with growing waits (2 s up to 60 s). An offline client is closed as soon as it reconnects.
- An order whose answer was lost (crash or restart) is found on the client by its comment.
- After a restart, a master trade opened while StakBridge was off is copied only if it's at most 30 s old (`STAKBRIDGE_COPY_MAX_AGE`).
- If a broker refuses the master's SL/TP, the copy opens without them (`OPENED_WITHOUT_SLTP`).
- Symbol mapping per client wins over the suffix rule.

**Speed** (simulated broker, so the broker's own time isn't included): about 0.6 ms median and under 2 ms worst, from "master change seen" to "client confirmed". Real brokers add their own 30–300 ms. The target is under 1 s in total.

**Other changes**
- Worker loop: on Windows, waiting on a pipe with a timeout takes at least 15 ms. The master now checks the pipe without waiting and sleeps precisely instead.
- Delays are measured with `time.perf_counter` (precise across processes). `time.time` on Python 3.11 / Windows only updates every 15.6 ms.
- The page no longer redraws while a drop-down is open.
- Log tab: database table `app_log`, the newest 5000 entries kept. An account that keeps failing is logged once, not on every retry.

**New API**
- `GET /v1/copier`, `PUT /v1/copier` with `{masterId, active, clients: [{accountId, multiplier, symbols: {"XAUUSD": "GOLD"}}]}`
- `GET /v1/logs?level=info|warning|error`, `DELETE /v1/logs`

**New settings:** `STAKBRIDGE_COPY_POLL_MS` (2), `STAKBRIDGE_COPY_MAX_AGE` (30)

## Files

| File | What it is |
| --- | --- |
| `stakbridge/copier.py` | The copier (new) |
| `stakbridge/applog.py` | Log tab storage (new) |
| `stakbridge/workers/process.py` | Worker loop: watches the master's positions |
| `stakbridge/workers/backends.py` | MT5 calls: close or modify one position by ticket, read positions, path checks, readable errors |
| `stakbridge/workers/manager.py` | Starts workers, turns master watching on and off, logs status changes |
| `stakbridge/db.py` | Tables `copies` and `app_log`, copier settings |
| `stakbridge/api.py` | `/v1/copier` and `/v1/logs`, path cleaning, start-up path fix |
| `stakbridge/static/*` | Copier tab, Log tab, symbol mapping editor, theme switch |
| `tests/test_copier.py`, `tests/test_applog.py` | New tests (14 + 5) |
| `TESTING.md` | Step-by-step guide for testing on this PC |

## Not done yet / known limits

- **Real MT5 test of the copier.** It's only been tested with the simulated broker. Real-MT5 parts still unchecked: modifying SL/TP, closing by ticket, finding a lost copy by its comment.
- Partial closes on the master aren't mirrored. The copy closes in full when the master closes in full.
- Pending orders aren't copied (by choice for v1).
- Switching to a different master stops following the old master's copies until you switch back.
- Symbol mapping is for the copier only. Strategy trades still use only each account's suffix.
- Phone-width layout wasn't checked.
- Idea for later, if the real delay is too high: an MT5 EA on the master for instant detection, and/or a VPS near the broker's servers.
