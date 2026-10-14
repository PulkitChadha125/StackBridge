# How to test StakBridge on your PC

This guide walks you through testing StakBridge with the 3 MetaTrader 5 programs on your computer. Follow the parts in order. You don't need to know any code.

**What you will test**

- **The copier.** You trade on one account (the **master**), and StakBridge copies every trade to the other two accounts (the **clients**). When you close on the master, the clients close too.
- **Strategy trading** (optional, Part 8). Another program sends a trade to StakBridge, and StakBridge places it on several accounts at once.

**Time needed:** about 1 hour the first time. Do it while the market is open (Monday to Friday). At the weekend every order is refused.

---

## Your 3 MetaTrader 5 programs

These are already installed on your PC. Each one is logged into its own account:

| StakBridge name | Role | MetaTrader 5 program (Terminal path) |
| --- | --- | --- |
| **acct_a** | Master: you trade here | `C:\Program Files\MetaTrader 5\terminal64.exe` |
| **acct_b** | Client 1: gets copies | `C:\Program Files\STARTRADER Financial MetaTrader 5\terminal64.exe` |
| **acct_c** | Client 2: gets copies | `C:\Program Files\MetaTrader 5 - C\terminal64.exe` |

> **Rule:** one MT5 program = one account. Never log two StakBridge accounts into the same program.

---

## Part 1: Quick check (2 minutes)

1. Open the folder `D:\Desktop\python projects\StackBridge`.
2. Double-click **`run_tests.bat`**. A black window opens. The first time, it installs things, which takes a few minutes.
3. Wait until it stops. At the bottom you should see **`48 passed`**.
4. Press any key to close the window.

If you see `failed` instead, stop here and send me what the window shows.

---

## Part 2: Get the 3 MetaTrader programs ready (one time, about 15 minutes)

### Step 1: Get 3 demo accounts

You need **3 different demo accounts** (practice money, not real money).

- In the **STARTRADER** program, open a STARTRADER demo account.
- In the other two MetaTrader programs, open a demo account on any broker. `MetaQuotes-Demo` works.

To open a demo account in any MetaTrader program: **File → Open an Account**, pick the broker, choose **Open a demo account**, and follow the steps.

For each account, **write down**:

| | acct_a | acct_b | acct_c |
| --- | --- | --- | --- |
| Login (a number) | | | |
| Password | | | |
| Server (exactly as written, e.g. `MetaQuotes-Demo`) | | | |
| Gold symbol name (see Step 4) | | | |

> The accounts must be **"hedging"** accounts. Most demo accounts are. If an account isn't, StakBridge will tell you with `NETTING_NOT_SUPPORTED`, and you'll need to open another one.

### Step 2: Log each program into its account

Open each MetaTrader program from the table at the top and log it into **its own** account: **File → Login to Trade Account**, then enter the login, password and server.

You know it worked when the bottom-right corner of MetaTrader shows a speed number like `45 / 3 kb` instead of `No connection`.

### Step 3: Turn on Algo Trading in all 3

In each MetaTrader program, click the **Algo Trading** button on the top toolbar so it turns **green**.

If you skip this, the accounts can't trade through StakBridge.

### Step 4: Find each broker's name for gold

Different brokers can give the same thing different names. In each program, look at the **Market Watch** list on the left (press **Ctrl+M** if you don't see it) and find gold. If it doesn't appear, right-click Market Watch → **Show All**.

Write down **exactly** how each program spells it. Capitals and dots count. You'll see one of these:

- **`XAUUSD`**: the plain name. Nothing to do.
- **`XAUUSD` plus something extra**, like `XAUUSD.m`, `XAUUSDm` or `XAUUSD.x`: the extra part (`.m`, `m`, `.x`) is that account's **suffix**. You'll type it in Part 4.
- **A completely different name**, like `GOLD` or `GOLD.pro`: you'll tell the copier the name in Part 5, step 5 (**symbol mapping**).

Do the same for any other symbol you plan to copy, for example EURUSD.

### Step 5: Leave all 3 programs open

Keep all 3 MetaTrader programs open and logged in while you test. If one closes, StakBridge reopens it by itself within about 10 seconds.

---

## Part 3: Start StakBridge

1. In the StakBridge folder, double-click **`run.bat`**.
2. A black window opens. **Keep it open the whole time.** Closing it stops StakBridge.
3. After a few seconds your browser opens the StakBridge page. If it doesn't, type `http://127.0.0.1:8000` in the browser.
4. You see **Create your password**, because StakBridge starts with empty data. Choose a password (at least 8 characters), type it twice, and press **Save and sign in**.

> Forgot the password later? Close the black window, open a Command Prompt in the StakBridge folder and type `run.bat password`.

**Appearance:** at the top right you can pick **System**, **Light** or **Dark**. **System** follows your Windows setting.

---

## Part 4: Add your 3 accounts

1. Click the **Accounts** tab.
2. Click **+ Add account** at the bottom.
3. Fill in the form for **acct_a**:

   | Box | What to type |
   | --- | --- |
   | Name | `Master A` |
   | Account id | `acct_a` |
   | MT5 login | the login number you wrote down |
   | Password | that account's password |
   | Server | the server, exactly as you wrote it down |
   | Symbol suffix | the suffix from Step 4, or leave it empty |
   | Terminal path | `C:\Program Files\MetaTrader 5\terminal64.exe` (typing just the folder `C:\Program Files\MetaTrader 5` works too) |

4. Press **Add account**.
5. Do the same for the other two:
   - **acct_b**: Name `Client B`, Terminal path `C:\Program Files\STARTRADER Financial MetaTrader 5\terminal64.exe`
   - **acct_c**: Name `Client C`, Terminal path `C:\Program Files\MetaTrader 5 - C\terminal64.exe`
6. Wait up to a minute. Each account's **Status** turns into a green dot with **Connected**, and its balance appears.
7. Each account has a **Trading** button showing **OFF**. Click it on all 3 so it turns green **ON**. Accounts always start OFF for safety.

**If an account doesn't show Connected:**

| You see | What it means | What to do |
| --- | --- | --- |
| `LOGIN_FAILED` | Wrong login, password or server, or the program at that Terminal path is logged into a different account | Click **Edit** on the account and check each box |
| `TERMINAL_PATH_INVALID` | There's no MetaTrader program at the Terminal path | Click **Edit** and use the path from the table at the top of this guide |
| `TERMINAL_NOT_STARTED` | StakBridge couldn't start that MetaTrader | Double-click that MetaTrader yourself. If it opens, log in, then wait: StakBridge retries every 10 seconds. If it doesn't open, reinstall it. |
| `TERMINAL_NOT_REACHED` | That MetaTrader is open but not answering | Close that MetaTrader. StakBridge starts it again within about 10 seconds. |
| `NETTING_NOT_SUPPORTED` | It's not a hedging account | Open a different demo account |
| `LIVE_ACCOUNT_BLOCKED` | It's a real-money account | Use a demo account |
| `ALGO_TRADING_OFF_IN_TERMINAL` | Algo Trading is off in that program | Click **Algo Trading** in MetaTrader so it turns green |
| `Connecting` for a long time | MetaTrader is still starting | Wait 1 minute. Check the program's connection in its bottom-right corner. |

---

## Part 5: Set up the copier

1. Click the **Copier** tab.
2. Under **Master account**, choose **Master A (acct_a)**.
3. Under **Client accounts**, tick **Client B** and **Client C**.
4. In the **Lot multiplier** box, leave Client B at `1` and change Client C to `2`. Client C will then always trade twice the master's size.
5. **Only if a client's broker uses a different name** (from Part 2, Step 4), set up symbol mapping for that client:
   1. In that client's row, click **Map symbols**.
   2. Click **+ Add symbol**.
   3. Left box (**Master symbol**): the name exactly as acct_a's program shows it, for example `XAUUSD`.
   4. Right box: the name exactly as **this client's** program shows it, for example `GOLD`.
   5. Repeat for each symbol whose name is different. Click **Hide** when you're done.

   Example: master acct_a calls gold `XAUUSD`, but Client C's broker calls it `GOLD`. On Client C's row, add `XAUUSD` → `GOLD`. Now a gold trade on the master opens `GOLD` on Client C.

   > You don't need a mapping when only the suffix differs (`XAUUSD` vs `XAUUSD.m`). The **Symbol suffix** from Part 4 already handles that. A mapping, when you add one, is used instead of the suffix for that symbol.

6. Press **Save changes**.
7. Click the **OFF** button next to "Trade copier" so it turns green **ON**.
8. Under the title you should now see a green dot and **Watching acct_a**.

> Trades that were already open on acct_a before this point are **not** copied. Only new trades are.

---

## Part 6: Test the copier

In each test, you trade in the **acct_a MetaTrader program** (`C:\Program Files\MetaTrader 5`) just as you normally would. Then you check the client programs and the StakBridge **Copier** tab.

**Useful MetaTrader actions**
- **New trade:** press **F9** (New Order), choose the symbol, type the volume, and click **Buy by Market** or **Sell by Market**.
- **See open trades:** press **Ctrl+T** to open the Toolbox, then the **Trade** tab.
- **Change SL/TP:** in the Trade tab, right-click the trade → **Modify or Delete**, change Stop Loss or Take Profit, and click **Modify**.
- **Close a trade:** in the Trade tab, click the small **X** at the end of the trade's row.

### C1: Opening is copied

**Do:** in acct_a, buy **0.01** lots of gold. Set a **Stop Loss** about 20 dollars below the price and a **Take Profit** about 20 dollars above it.

**You should see:**
- **acct_b program** (Trade tab): a new buy of **0.01** lots, with the same SL and TP.
- **acct_c program:** a new buy of **0.02** lots (×2), with the same SL and TP.
- Each copy's **Comment** says `copy` followed by the master's ticket number.
- StakBridge **Copier** tab: two rows with status **open**.

### C2: SL/TP changes are copied

**Do:** in acct_a, change the Stop Loss of that trade to a new price.

**You should see:** within a second, the copies in acct_b and acct_c show the same new Stop Loss.

### C3: Closing is copied

**Do:** in acct_a, close the trade (the **X**).

**You should see:** the copies disappear from the acct_b and acct_c programs. In the Copier tab both rows say **closed**, and the **Close** column shows how long it took.

### C4: Sell trades work too

**Do:** in acct_a, **sell** 0.01 gold. Wait for the copies. Then close it.

**You should see:** a 0.01 sell in acct_b and a 0.02 sell in acct_c, and both close when you close the master.

### C5: Your own trades on a client are left alone

**Do:**
1. In the **acct_b** program, open a trade yourself, for example buy 0.01 EURUSD.
2. In acct_a, buy 0.01 gold, wait for the copies, then close it.

**You should see:** the gold copy on acct_b closes, but **your own EURUSD trade stays open**. Close it yourself afterwards.

### C6: Switching the copier off

**Do:**
1. In the Copier tab, click **ON** so it turns **OFF**.
2. In acct_a, buy 0.01 gold.

**You should see:** nothing is copied. The Copier tab shows the two rows as **skipped** with reason `COPIER_PAUSED`.

3. Close that trade on acct_a. Click **OFF** so it turns **ON** again.

> Good to know: while the copier is OFF, new trades aren't copied, but **closes and SL/TP changes still are**. That way you can always get your clients out of a trade.

### C7: A client that goes offline

**Do:**
1. In acct_a, buy 0.01 gold and wait for the copies.
2. In StakBridge, **Accounts** tab: click **Disable** on **Client C**.
3. In acct_a, close the trade.

**You should see:** the copy on acct_b closes at once. The copy on acct_c **stays open**, and the Copier tab shows `CLOSE_WAITING: DISCONNECTED`.

4. Click **Enable** on Client C and wait until it shows **Connected**.

**You should see:** the acct_c copy closes by itself within a few seconds.

### C8: Two trades quickly

**Do:** in acct_a, buy 0.01 gold, and immediately (within a second or two) buy another 0.01 gold. Then close both.

**You should see:** two copies on each client, and both close when you close them on the master.

### C9: Symbol mapping (only if you set one up in Part 5)

**Do:** in acct_a, buy 0.01 of the symbol you mapped, for example `XAUUSD`. Then close it.

**You should see:** the client with the mapping opens the **mapped name**, for example `GOLD`. The other client opens the usual name. Both close when you close the master. In the Copier tab, the **Symbol** column shows the name each client used.

> **Practice it even if all your brokers use the same name:** on Client B's row, map `XAUUSD` to the name Client B already uses, for example `XAUUSD.m`. The trade copies normally. Then change the right box to a name that doesn't exist, like `NOTREAL`. A new copy is then **rejected** with `UNKNOWN_SYMBOL`, which proves the mapping is used. Put the right name back and press **Save changes**.

---

## Part 7: Check the speed

In the **Copier** tab, look at the boxes above the table:

- **Copy open:** how long it took from the moment StakBridge saw your master trade until the client's broker confirmed the copy. It shows the median (the typical time) and the max (the slowest).
- **Copy close:** the same, for closing.
- **broker:** the part of that time spent waiting for the broker's server.

**What's normal:**
- StakBridge's own part (the total minus "broker") should be a few milliseconds.
- The **broker** part is usually **30 to 300 ms**. It depends on how far the broker's server is from your PC. StakBridge can't make it faster.
- **Goal:** everything under **1000 ms** (1 second). Write down your numbers in the checklist at the end.

> Want it faster? Run StakBridge and MetaTrader on a VPS (a rented server) in the same city as the broker's servers. The broker part then drops to a few milliseconds.

---

## Part 8 (optional): Strategy trading tests

These test the other half of StakBridge: a program sends a trade to StakBridge, and StakBridge places it on the accounts linked to a **strategy**.

**Before you start:** go to the **Copier** tab and switch the copier **OFF**. Otherwise anything placed on acct_a here would also be copied, which makes the results confusing. Switch it back ON afterwards.

**Set up:**
1. **Strategies** tab. In the box at the bottom left, type `gold_scalper`, max lots `1`, and press **+ New strategy**. Do the same for `swing_eur` with max lots `2`.
2. Click **gold_scalper** and tick **Master A** and **Client B**. Click **swing_eur** and tick **Client B** and **Client C**.

**How to send a trade:** **Trades** tab → open **Send a trade request**. The buttons fill in the request for you:
- **Open market:** buys 0.01 gold for the strategy selected in the Strategies tab.
- **Close strategy:** closes everything that strategy opened.

Press **Send**. The answer appears below. Each click on a button creates a new request id, and pressing **Send** twice without clicking a button sends the same request again (that's test 5).

| # | Do this | It works if |
| --- | --- | --- |
| 1 | Look at the Accounts tab | All 3 show **Connected** |
| 2 | Select `gold_scalper`, click **Open market**, **Send** | `HTTP 200`. Only acct_a and acct_b get a trade, acct_c gets nothing |
| 3 | Change `"strategy"` to `"nope"` and **Send** | `HTTP 400` `UNKNOWN_STRATEGY`, no trade anywhere |
| 4 | Accounts tab: **Disable** Client B. Send a new **Open market** | acct_a filled, acct_b `skipped` `DISCONNECTED`. **Enable** it again afterwards. |
| 5 | Click **Open market**, **Send**, then **Send** again without clicking anything else | The second answer says `"replayed": true` and **no second trade** is placed |
| 6 | **Open market**, change `"volume"` to `5`, **Send** | `HTTP 400` `VOLUME_TOO_LARGE`, nothing placed |
| 7 | Press **Stop all** (top right), then **Open market**, **Send** | `HTTP 409` `KILL_SWITCH_ON`. **Close strategy** still works. Press **Resume trading** afterwards. |
| 8 | Strategies tab: untick Master A on gold_scalper, tick Client C. **Open market**, **Send** | Only acct_b and acct_c get the trade |
| 9 | Make sure Client B has a gold_scalper trade **and** a swing_eur trade (for swing_eur, change `"symbol"` to `"EURUSD"`). Select gold_scalper, **Close strategy**, **Send** | On acct_b only the gold_scalper trade closes. The EURUSD trade stays. |

In MetaTrader, the **Comment** of each trade is its request id, so you can match trades to requests. Close all test trades when you're done.

---

## If something looks wrong

**Copier tab: what each status means**

| Status | Meaning |
| --- | --- |
| **open** | Copied. The copy is open on the client. |
| **closed** | The copy was closed. `ALREADY_CLOSED_ON_CLIENT` means it had already closed by itself, for example its own SL was hit first. |
| **skipped** | Not copied, on purpose. The reason says why: `COPIER_PAUSED` (copier OFF), `KILL_SWITCH_ON` (Stop all pressed), `TRADING_OFF` (that account's Trading button is OFF), `DISCONNECTED` (that account is offline), `TOO_OLD_AFTER_RESTART` (the master trade was opened while StakBridge was closed, more than 30 seconds earlier). |
| **rejected** | The client's broker refused it. `VOLUME_BELOW_BROKER_MIN` means master lots × multiplier is smaller than that broker allows. `UNKNOWN_SYMBOL` means that broker doesn't know the name: check the Symbol column, then fix the account's **Symbol suffix** or add a **symbol mapping** (Copier tab → **Map symbols**). |
| **unknown** | No answer from the client yet. StakBridge never sends it twice. If the answer got lost, it finds the copy on the client by itself. |
| `CLOSE_WAITING` / `CLOSE_FAILED` | The master closed, but the client copy is still open. StakBridge keeps retrying until it closes. |
| `OPENED_WITHOUT_SLTP` | The client's broker refused the master's SL/TP, so the copy was opened without them. Set them by hand in that program. |
| `SLTP_FAILED` | An SL/TP change didn't go through on that client |

**Not copied in this version**
- Pending orders (Buy Limit, Sell Stop and so on). Only trades that are actually open are copied.
- Closing **part** of a trade on the master. The client copy closes fully when the master closes fully.

**The Log tab**
Click **Log** at the top to see what happened and when: accounts connecting or disconnecting, connection errors, orders a broker refused, copier problems and settings changes. The newest entries are at the top. Use **Errors only** to see only problems. A red **!** appears on the tab when a new error arrives that you haven't seen yet.

When you ask for help, send a screenshot of the Log tab. It usually shows the cause.

**StakBridge page says "Cannot reach the gateway"**
The black `run.bat` window was closed. Start `run.bat` again.

**Start completely fresh**
Close the black window and delete the `data` folder inside the StakBridge folder. Then start `run.bat` again and repeat Parts 3 to 5.

---

## Real-money accounts

StakBridge refuses real-money accounts on purpose (`LIVE_ACCOUNT_BLOCKED`). Only switch this on **after every test above passes on demo accounts**. Open a Command Prompt in the StakBridge folder and start it like this:

```bat
set STAKBRIDGE_ALLOW_LIVE=1
run.bat
```

Start with the smallest lot size and watch the first trades closely.

---

## Practice without MetaTrader (optional)

Double-click **`run_demo.bat`** instead of `run.bat`. It uses a pretend broker: nothing real is traded, gold always costs 2321.45, and any login works. Its data is kept separately in the `data_fake` folder, with its own password. In this mode you can't trade "by hand" on the master, so use the **Trades** tab with a strategy that only has Master A ticked to place master trades.

---

## Checklist

Copy this and tick each line as it passes.

```
Part 1   [ ] 48 passed
Part 4   [ ] all 3 accounts Connected, Trading ON
Part 5   [ ] Copier shows "Watching acct_a"
Part 6   [ ] C1  [ ] C2  [ ] C3  [ ] C4  [ ] C5  [ ] C6  [ ] C7  [ ] C8  [ ] C9
Part 7   Copy open: ____ ms median, ____ ms max (broker ____ ms)
         Copy close: ____ ms median, ____ ms max (broker ____ ms)
Part 8   [ ] 1  [ ] 2  [ ] 3  [ ] 4  [ ] 5  [ ] 6  [ ] 7  [ ] 8  [ ] 9   (optional)
```
