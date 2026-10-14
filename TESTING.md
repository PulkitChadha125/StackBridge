# Testing StakBridge v1

The spec says v1 is done when the 9 acceptance tests in section 7 pass on demo accounts. Work through the stages in order. Each stage only needs the one before it.

| Stage | What it proves | Needs MT5? | Time |
| --- | --- | --- | --- |
| [1. Automated tests](#stage-1-automated-tests) | The code follows the spec rules | No | 1 min |
| [2. Simulated broker walkthrough](#stage-2-simulated-broker-walkthrough) | The UI and API work end to end | No | 15 min |
| [3. Real MT5 demo accounts](#stage-3-real-mt5-demo-accounts) | Real orders reach real terminals | Yes, 3 demo accounts | 45 min |

---

## Stage 1: Automated tests

Double-click **`run_tests.bat`**. The first run creates `.venv` and installs the packages.

**Pass:** the last line reads `29 passed`.

- `tests/test_acceptance.py`: one test for each of the 9 acceptance tests (`test_1_...` to `test_9_...`).
- `tests/test_safety.py`: token check, trading off by default, passwords never returned, paused strategy, and the other rules from section 6.
- `tests/test_login.py`: web page password, sign in and out, the first password only from this PC, and password reset.

---

## Stage 2: Simulated broker walkthrough

The simulated broker behaves like a hedging demo account, but nothing leaves your PC. XAUUSD always fills at 2321.45.

### Start

1. Double-click **`run_demo.bat`**. A black window opens. Keep it open, because closing it stops the app.
2. The browser opens http://127.0.0.1:8000.
   - **First time:** you see **Create your password**. Type a password of at least 8 characters twice and press **Save and sign in**.
   - **After that:** you see **Sign in**. The browser stays signed in for 30 days, even after a restart.

> The demo and the real app have **separate passwords**, because they keep separate data. Forgot one? Run `run.bat password fake` for the demo, or `run.bat password` for the real app.

> The demo stores its data in `data_fake\`, separate from real accounts. To start the demo over, close the window and delete `data_fake\`.

### Set up (same as the spec's example)

1. **Accounts** tab, **+ Add account**. Add three accounts. In demo mode, any password and server will do.

   | Name | Account id | MT5 login | Symbol suffix |
   | --- | --- | --- | --- |
   | Acct A | acct_a | 5012345 | |
   | Acct B | acct_b | 5012346 | .m |
   | Acct C | acct_c | 7788120 | |

2. Wait a few seconds until all three show a green **Connected** dot.
3. Click each **OFF** button so it turns green **ON**. New accounts always start OFF.
4. **Strategies** tab. Create `gold_scalper` with max lots `1`, then `swing_eur` with max lots `2`.
5. Select **gold_scalper** and tick **Acct A** and **Acct B**. Select **swing_eur** and tick **Acct B** and **Acct C**.

### Sending a trade

On the **Trades** tab, open **Send a trade request**. The preset buttons fill in the JSON:

- **Open market**: buy 0.01 XAUUSD for the selected strategy, with a fresh `requestId`.
- **Open limit**: a pending buy limit far below the price.
- **Close strategy**: closes everything that strategy opened.

You can edit the JSON before you press **Send**. The response appears below with its HTTP code, and the trade's per-account results open in the table above.

> **Important:** clicking a preset creates a new `requestId`. Pressing **Send** again *without* a new preset resends the same id on purpose. That is acceptance test 5.

To check positions, go to the **Accounts** tab and click **Positions** on an account.

### The 9 acceptance tests

| # | Do this | Pass when |
| --- | --- | --- |
| 1 | Look at the Accounts tab | All three show **Connected** |
| 2 | Trades tab: select `gold_scalper` (Strategies tab), then **Open market** and **Send** | HTTP 200. Results list **only** acct_a and acct_b, both `filled`. **Positions** shows the trade on A and B (B as `XAUUSD.m`). Acct C has nothing. |
| 3 | Change `"strategy"` to `"nope"` and send | HTTP 400 `UNKNOWN_STRATEGY`. No new positions anywhere. Then delete the whole `"strategy"` line and send: HTTP 400 `BAD_PAYLOAD`. |
| 4 | Accounts tab: click **Disable** on Acct B. Then send a new **Open market** for gold_scalper | Acct A `filled`, Acct B `skipped` with reason `DISCONNECTED`. Click **Enable** afterwards. |
| 5 | Send a new **Open market**, then press **Send** again without changing anything | Second response has `"replayed": true` and the same tickets. Positions grows by one only. Now change `"volume"` to `0.02` and send again: HTTP 409 `REQUEST_ID_REUSED`. |
| 6 | New **Open market**, set `"volume": 5` | HTTP 400 `VOLUME_TOO_LARGE`. Nothing placed. |
| 7 | Press **Stop all** (top right). Send a new **Open market** | HTTP 409 `KILL_SWITCH_ON`, nothing placed. The header shows **Trading stopped**. Then press **Close strategy** and send: it still works. Press **Resume trading** at the end. |
| 8 | Strategies tab: untick Acct A on `gold_scalper`, tick Acct C. Send a new **Open market** | Results list acct_b and acct_c only |
| 9 | Make sure gold_scalper and swing_eur both have open positions on Acct B (send one trade to each; for swing_eur set `"symbol": "EURUSD"`). Then select gold_scalper and send **Close strategy** | Acct B shows `closed` with `closedPositions` ≥ 1. **Positions** on Acct B still shows the `EURUSD.m` swing_eur position. |

### Extra checks

- **Pause a strategy:** on the Strategies tab, click **Active** to make it **Paused**, then send an open. Expect HTTP 409 `STRATEGY_PAUSED`. A close still works.
- **Trading switch:** turn an account's Trading **OFF** and send. That account shows `skipped`, `TRADING_OFF`.
- **No accounts on a tag:** create a strategy with no ticked accounts and send to it. Expect HTTP 409 `NO_ELIGIBLE_ACCOUNT`.
- **Sign out and in:** click **Sign out** (top right). Type a wrong password: you see "Wrong password". Type the right one: you are back in.
- **Password reset:** close the window, run `run.bat password fake`, and choose a new password. Start `run_demo.bat` again. The old password no longer works and the new one does.
- **Restart:** close the window and start `run_demo.bat` again. Accounts, strategies, trade history and the Stop all state are all still there. Positions are not, because the simulated broker forgets them on restart. Real MT5 keeps them.

---

## Stage 3: Real MT5 demo accounts

### Prepare (one time)

1. **Three demo accounts.** They must be **hedging** accounts. Most brokers' standard MT5 demos are. A netting account shows `NETTING_NOT_SUPPORTED` and is refused on purpose.
2. **One terminal folder per account.** MT5 can only be logged into one account at a time, so each account needs its own copy:
   ```
   C:\MT5\acct_a\   <- copy of the whole "C:\Program Files\MetaTrader 5" folder
   C:\MT5\acct_b\
   C:\MT5\acct_c\
   ```
3. **Log in once by hand** in each copy (run its `terminal64.exe`, File > Login to Trade Account). Note the **server name exactly** as the login window shows it, for example `MetaQuotes-Demo`.
4. In each terminal, turn on **Algo Trading** (the toolbar button turns green). If you skip this, StakBridge shows `ALGO_TRADING_OFF_IN_TERMINAL` and the broker rejects orders.
5. Check the broker's gold symbol name in Market Watch. If it is `XAUUSD.m`, set the account's **Symbol suffix** to `.m`. If the broker calls gold something else entirely, such as `GOLD`, use that name as `"symbol"` in your test requests.

### Start

Double-click **`run.bat`**. This is real MT5 mode, with data in `data\`. Create the web page password on first start, the same way as in Stage 2.

Add each account with its real **login, password, server** and the **Terminal path**, for example `C:\MT5\acct_a\terminal64.exe`. StakBridge starts the terminal itself if it is not running.

> Live (real money) accounts are refused with `LIVE_ACCOUNT_BLOCKED`. That is the spec's "demo only" rule.

### Run the 9 tests

Use the same table as Stage 2, with these differences:

- **Use 0.01 lots.** The presets already do.
- **Check in MT5 as well.** In each terminal, open the **Toolbox > Trade** tab. The order comment is the `requestId` (for example `test-20261014...`), so you can match orders to requests.
- **Test 4:** **Disable** is the reliable way. If you close a terminal instead, StakBridge restarts it within about 10 seconds, so send the trade straight after closing it.
- **Test 9:** the swing_eur position must be on an account that also has gold_scalper (Acct B in the setup).
- **Prices and fills are real.** A trade can be `rejected` for a broker reason, for example the market is closed at the weekend. Rejected is the correct result in that case. Run the tests while the market is open.

### Send a trade from outside the UI

This is how a real signal source calls StakBridge. Programs don't use the web page password. They use an **API token**:

1. Create a token: open a Command Prompt in the project folder and run `run.bat token`. Copy the `sb_...` value it prints, because it is shown only once. You can create one token per program.
2. Send a trade in PowerShell:

```powershell
$token = "sb_your_token_here"
$body = @{
  requestId = "ps-0001"; strategy = "gold_scalper"; action = "open"
  symbol = "XAUUSD"; side = "buy"; type = "market"; volume = 0.01
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/trade `
  -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" `
  -Body $body -SkipHttpErrorCheck -StatusCodeVariable code
"HTTP $code"
```

Close everything for the strategy:

```powershell
$body = @{ requestId = "ps-0002"; strategy = "gold_scalper"; action = "close" } | ConvertTo-Json
```

### Measure the timeout (spec section 3)

The spec says to set the trade timeout from real measurements. While testing:

1. Send 10 market trades to a tag with all 3 accounts.
2. Note how long each response takes. PowerShell: `Measure-Command { Invoke-RestMethod ... }`.
3. Set `STAKBRIDGE_TRADE_TIMEOUT` to about 3 times the slowest one, with a minimum of 5 seconds, before starting `run.bat`:
   ```bat
   set STAKBRIDGE_TRADE_TIMEOUT=6
   run.bat
   ```
   An account that does not answer in time is shown as `unknown` and is **never** resent. When its late answer arrives, the result updates itself and shows `lateReply` in the detail column.

---

## What the statuses mean

**Account status (Accounts tab)**

| Status | Meaning | What to do |
| --- | --- | --- |
| Connected | Logged in and talking to the broker | Nothing |
| Starting / Connecting | Worker is starting or logging in (MT5 can take up to a minute) | Wait |
| Disconnected | Terminal or broker connection lost. StakBridge retries every 10 s | Check internet and the terminal |
| Error `LOGIN_FAILED` | Wrong login, password or server | **Edit** the account |
| Error `NETTING_NOT_SUPPORTED` | Netting account | Use a hedging demo account |
| Error `LIVE_ACCOUNT_BLOCKED` | Real-money account | Use a demo account |
| Connected + `ALGO_TRADING_OFF_IN_TERMINAL` | Algo Trading button is off in MT5 | Turn it on in the terminal |
| Disabled | You clicked Disable | Click Enable |

**Trade result per account (Trades tab)**

| Status | Meaning |
| --- | --- |
| filled | Market order executed |
| placed | Pending (limit or stop) order placed |
| closed | Close action done. `detail` shows the counts. |
| rejected | The broker or the account's lot rules refused it. `reason` says why. |
| skipped | Not sent at all: `DISCONNECTED` or `TRADING_OFF` |
| unknown | No answer within the timeout. Never resent. Check that terminal. |

## Test record

Copy this list and tick each line as it passes.

```
Stage 1  [ ] 29 automated tests pass
Stage 2  [ ] 1  [ ] 2  [ ] 3  [ ] 4  [ ] 5  [ ] 6  [ ] 7  [ ] 8  [ ] 9
Stage 3  [ ] 1  [ ] 2  [ ] 3  [ ] 4  [ ] 5  [ ] 6  [ ] 7  [ ] 8  [ ] 9
         [ ] timeout measured and set: ____ s
```
