# How to test StakBridge on your PC

This guide walks you through testing StakBridge with the 3 MetaTrader 5 programs on your computer. Follow the parts in order. You don't need to know any code.

**What you will test**

- **The copier.** You trade on one account (the **master**), and StakBridge copies every trade to the other two accounts (the **clients**). When you close on the master, the clients close too.
- **The licence** (optional, Part 8). The licence server, a customer's `.exe`, sign-in, trial, limits.

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
3. Wait until it stops. At the bottom you should see **`354 passed`**.
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

Then, in each program, open **Tools → Options → Expert Advisors** and **untick**:
- "Disable algorithmic trading when the account has been changed"
- "Disable algorithmic trading via external Python API" (if your version has it)

Press **OK**. Otherwise MetaTrader can switch Algo Trading off by itself when StakBridge logs in.

If you skip this, the accounts can't trade through StakBridge.

### Step 4: Find each broker's name for gold

Different brokers can give the same thing different names. In each program, look at the **Market Watch** list on the left (press **Ctrl+M** if you don't see it) and find gold. If it doesn't appear, right-click Market Watch → **Show All**.

Write down **exactly** how each program spells it. Capitals and dots count. You'll see one of these:

- **`XAUUSD`**: the plain name. Nothing to do.
- **`XAUUSD` plus something extra**, like `XAUUSD.m`, `XAUUSDm` or `XAUUSD.x`: the extra part (`.m`, `m`, `.x`) is that account's **suffix**. You'll type it in Part 4.
- **A completely different name**, like `GOLD` or `GOLD.pro`: you'll tell the copier the name in Part 5, step 5 (**symbol mapping**).

> If you forget a suffix, StakBridge still finds the right symbol by itself in most cases: when the client's broker has no symbol with the master's name, it uses the one with the same name minus the ending (master `XAUUSD.C` → client `XAUUSD`). Setting the suffix is still the surest way.

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
2. Click **+ Add account** at the bottom. A pop-up opens.
3. Fill it in for **acct_a**:

   | Box | What to type |
   | --- | --- |
   | Role | **Master account** |
   | Name | `Master A` |
   | MT5 login | the login number you wrote down |
   | Password | that account's password |
   | Server | the server, exactly as you wrote it down |
   | Symbol suffix | the suffix from Step 4, or leave it empty |
   | Terminal path | `C:\Program Files\MetaTrader 5\terminal64.exe` (typing just the folder `C:\Program Files\MetaTrader 5` works too) |

4. Press **Add account**. The pop-up closes. (**Cancel**, **×** or the **Esc** key close it without saving. **Edit** on an account opens the same pop-up.)
5. Do the same for the other two, with Role **Client account**:
   - **acct_b**: Name `Client B`, Terminal path `C:\Program Files\STARTRADER Financial MetaTrader 5\terminal64.exe`
   - **acct_c**: Name `Client C`, Terminal path `C:\Program Files\MetaTrader 5 - C\terminal64.exe`
6. Wait up to a minute. Each account's **Status** turns into a green dot with **Connected**, and its balance appears.
   The master is shown on its own at the top (**Master account**), the clients below it (**Client accounts**).
7. Each account has a red **○ OFF** button under **Trading**. Click it on all 3. It shows **Saving…**, then turns green **● ON**. Accounts always start OFF for safety.
8. Each time you switch Trading ON, StakBridge asks that MetaTrader for the account's numbers right away and shows them under the account: **Balance, Equity, Open P/L, Free margin, Leverage, Open positions, Ping to broker** and how long the answer took.
   - Green box **Connection OK**: the account is ready.
   - Yellow box: connected, but there's a problem, for example Algo Trading is off. The box says what to do.
   - Red box **Check failed**: MetaTrader didn't answer. Check the Status column and the Log tab.

   You can press **Check** on any account at any time to read the numbers again.
9. The **Balance**, **Equity** and **Open P/L** columns update by themselves about once a second. Open P/L is green when in profit and red when in loss.
10. **Open MT5** brings up that account's MetaTrader window (it starts it if it isn't running).

> Every account that isn't the master is a client automatically: there's nothing to tick. No master yet? Press **Make master** on the account you trade on.

**If an account doesn't show Connected:**

| You see | What it means | What to do |
| --- | --- | --- |
| `LOGIN_FAILED` | Wrong login, password or server, or the program at that Terminal path is logged into a different account | Click **Edit** on the account and check each box |
| `TERMINAL_PATH_INVALID` | There's no MetaTrader program at the Terminal path | Click **Edit** and use the path from the table at the top of this guide |
| `TERMINAL_NOT_STARTED` | StakBridge couldn't start that MetaTrader | Double-click that MetaTrader yourself. If it opens, log in, then wait: StakBridge retries every 10 seconds. If it doesn't open, reinstall it. |
| `TERMINAL_NOT_REACHED` | That MetaTrader is open but not answering | Close that MetaTrader. StakBridge starts it again within about 10 seconds. |
| `WRONG_ACCOUNT_IN_TERMINAL` | Two accounts use the same MetaTrader, so they keep logging each other out. StakBridge stops trading on it so no order lands on the wrong account. | Click **Edit** and give each account its own Terminal path (see the table at the top). An **empty** Terminal path means "the default MetaTrader", which is usually acct_a's. |
| `NETTING_NOT_SUPPORTED` | It's not a hedging account | Open a different demo account |
| `ALGO_TRADING_OFF_IN_TERMINAL` | Algo Trading is off in that program | Click **Algo Trading** in MetaTrader so it turns green |
| `Connecting` for a long time | MetaTrader is still starting | Wait 1 minute. Check the program's connection in its bottom-right corner. |

---

## Part 5: Set up the copier

1. Click the **Copier** tab.
2. Under **Master account**, **Master A (acct_a)** is already chosen (from its Role in Part 4).
3. Under **Client accounts**, **Client B** and **Client C** are listed. Every account except the master copies the master; there is nothing to tick.
4. In the **Lot multiplier** box, leave Client B at `1` and change Client C to `2`. Client C will then always trade twice the master's size.
5. **Only if a client's broker uses a different name** (from Part 2, Step 4), set up symbol mapping for that client:
   1. In that client's row, click **Map symbols**.
   2. Click **+ Add symbol**.
   3. Left box (**Master symbol**): the name exactly as acct_a's program shows it, for example `XAUUSD`.
   4. Right box: the name exactly as **this client's** program shows it, for example `GOLD`.
   5. Repeat for each symbol whose name is different. Click **Hide** when you're done.

   Example: master acct_a calls gold `XAUUSD`, but Client C's broker calls it `GOLD`. On Client C's row, add `XAUUSD` → `GOLD`. Now a gold trade on the master opens `GOLD` on Client C.

   > You don't need a mapping when only the suffix differs (`XAUUSD` vs `XAUUSD.m`). The **Symbol suffix** from Part 4 already handles that, and if a suffix is missing StakBridge matches the name by itself. A mapping, when you add one, is used instead of the suffix for that symbol. You do need one for a completely different name (`GOLD`), or when a broker has two versions of a symbol (`XAUUSD` and `XAUUSD.pro`) both in Market Watch.

6. Press **Save changes**.
7. Under the title you should now see a green dot and **Watching acct_a**. If it says the master has Trading OFF, switch acct_a's Trading **ON** in the Accounts tab.

> There is no separate copier ON/OFF switch. Copying is controlled by the **Trading** switches: the master's switch for everyone, each client's switch for that client only.

> Trades that were already open on acct_a before this point are **not** copied. Only new trades are.

---

## Part 6: Test the copier

In each test, you trade in the **acct_a MetaTrader program** (`C:\Program Files\MetaTrader 5`) just as you normally would. Then you check the client programs and the StakBridge **Trades** tab.

**The Trades tab** lists every trade you open on the master, newest first. Each row shows the symbol, side, lots, price, SL/TP, whether the master trade is still open, and how many clients got it (for example `2 of 2 copied`). **Click a row** and a pop-up opens with:
- each client account: **Copied** / **Not copied** / **Failed**, client ticket, lots, open and close price, **open and close latency** (how long the copy took, and the broker's part of it), and the **error or reason** in plain words
- the trade on the master (ticket, price, SL, TP, when it was seen, when it closed) and its history
- log entries about this trade

The pop-up updates by itself. Close it with the **×**, the **Esc** key, or a click outside it.

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
- StakBridge **Trades** tab: the trade, with `2 of 2 copied`. Click it: both clients show **Copied** with their latency.

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

### C5b: Limit and stop orders are copied

**Do:**
1. In acct_a, place a **Buy Limit** of 0.01 gold a few dollars **below** the current price, with an SL and a TP (right-click the chart → **Trading** → **Buy Limit**, or **New Order** → Type **Pending Order**).
2. Wait a second, then move that order's price (drag its line on the chart, or right-click it in the Trade tab → **Modify or Delete**).
3. Delete the order (right-click → **Modify or Delete** → **Delete**).
4. Place another Buy Limit just below the price and wait until the price comes down and it fills.

**You should see:**
1. acct_b and acct_c each show a waiting **Buy Limit** at the same price, SL and TP (0.01 and 0.02 lots). Trades tab: Master **waiting**, clients **Order placed, waiting**.
2. The client orders move to the new price.
3. The client orders disappear. Trades tab: Master **cancelled**, clients **Order deleted**.
4. When the master's order fills, each client gets the trade within a second: its own order if it filled already, otherwise its order is deleted and the trade is opened at market. From then on SL/TP changes and the close are copied as usual.

> Stop-limit orders are not copied while waiting; the trade they open is copied at market. If **Stop all** is on when a master order fills, the clients' orders are deleted and no trade is opened (`KILL_SWITCH_ON`).

### C6: Trading OFF on one client only

**Do:**
1. In the Accounts tab, switch **Client C**'s Trading **OFF**.
2. In acct_a, buy 0.01 gold.

**You should see:** Client B gets the copy as usual. Client C gets nothing. In the Trades tab pop-up, Client C shows **Not copied** with `TRADING_OFF`.

3. Close that trade on acct_a. Switch Client C's Trading **ON** again.

**Also try:** switch the **master's** Trading OFF and buy on acct_a. Then **no** client copies it (`MASTER_TRADING_OFF`). Close it and switch the master back ON.

> Good to know: while Trading is OFF, new trades aren't copied, but **closes and SL/TP changes from the master still are** (except to a client whose own switch is OFF). That way you can always get your clients out of a trade.

### C7: A client that goes offline

**Do:**
1. In acct_a, buy 0.01 gold and wait for the copies.
2. In StakBridge, **Accounts** tab: click **Disable** on **Client C**.
3. In acct_a, close the trade.

**You should see:** the copy on acct_b closes at once. The copy on acct_c **stays open**, and the Trades tab pop-up shows `CLOSE_WAITING: DISCONNECTED` for it.

4. Click **Enable** on Client C and wait until it shows **Connected**.

**You should see:** the acct_c copy closes by itself within a few seconds.

### C8: Two trades quickly

**Do:** in acct_a, buy 0.01 gold, and immediately (within a second or two) buy another 0.01 gold. Then close both.

**You should see:** two copies on each client, and both close when you close them on the master.

### C9: Symbol mapping (only if you set one up in Part 5)

**Do:** in acct_a, buy 0.01 of the symbol you mapped, for example `XAUUSD`. Then close it.

**You should see:** the client with the mapping opens the **mapped name**, for example `GOLD`. The other client opens the usual name. Both close when you close the master. In the Copier tab, the **Symbol** column shows the name each client used.

> **Practice C9 even if all your brokers use the same name:** on Client B's row, map `XAUUSD` to the name Client B already uses, for example `XAUUSD.m`. The trade copies normally. Then change the right box to a name that doesn't exist, like `NOTREAL`. A new copy is then **rejected** with `UNKNOWN_SYMBOL`, which proves the mapping is used. Put the right name back and press **Save changes**.

### C10: Automatic symbol matching (master with a suffix, client without)

**Set up:** this needs a master whose broker adds an ending to gold, for example `XAUUSD.C`, and a client whose broker calls it plain `XAUUSD`. Leave **both** accounts' **Symbol suffix** empty and add **no** mapping for gold.

**Do:** in the master, buy 0.01 `XAUUSD.C`. Then close it.

**You should see:** the client opens **`XAUUSD`** and closes with the master. In the Trades pop-up, the client's row shows the symbol `XAUUSD`. If the client's broker has both `XAUUSD` and, say, `XAUUSD.pro` in Market Watch, the copy is instead **rejected** with a message naming both: add a mapping then.

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

## If something looks wrong

**Copier tab: what each status means**

| Status | Meaning |
| --- | --- |
| **open** | Copied. The copy is open on the client. |
| **closed** | The copy was closed. `ALREADY_CLOSED_ON_CLIENT` means it had already closed by itself, for example its own SL was hit first. |
| **skipped** | Not copied, on purpose. The reason says why: `MASTER_TRADING_OFF` (the master's Trading button is OFF), `KILL_SWITCH_ON` (Stop all pressed), `TRADING_OFF` (that account's Trading button is OFF), `DISCONNECTED` (that account is offline), `TOO_OLD_AFTER_RESTART` (the master trade was opened while StakBridge was closed, more than 30 seconds earlier). |
| **rejected** | The client's broker refused it. `VOLUME_BELOW_BROKER_MIN` means master lots × multiplier is smaller than that broker allows. `UNKNOWN_SYMBOL` means that broker doesn't know the name: check the Symbol column, then fix the account's **Symbol suffix** or add a **symbol mapping** (Copier tab → **Map symbols**). |
| **unknown** | No answer from the client yet. StakBridge never sends it twice. If the answer got lost, it finds the copy on the client by itself. |
| `CLOSE_WAITING` / `CLOSE_FAILED` | The master closed, but the client copy is still open. StakBridge keeps retrying until it closes. |
| `OPENED_WITHOUT_SLTP` | The client's broker refused the master's SL/TP, so the copy was opened without them. Set them by hand in that program. |
| `SLTP_FAILED` | An SL/TP change didn't go through on that client |
| `ORDER_CHANGE_FAILED` | Moving a waiting order on that client to the master's new price/SL/TP didn't go through |
| `FILL_WAITING` / `CANCEL_WAITING` | The master's order filled / was deleted while that client was offline or its Trading was OFF. It is done as soon as that changes. |
| `FILL_FAILED` / `CANCEL_FAILED` | Swapping the client's order for the trade, or deleting it, failed. StakBridge keeps retrying. |

**Not copied in this version**
- Closing **part** of a trade on the master. The client copy closes fully when the master closes fully.
- Stop-limit orders while they wait. The trade they open is copied at market.

**The Log tab**
Click **Log** at the top to see what happened and when: accounts connecting or disconnecting, connection errors, orders a broker refused, copier problems and settings changes. The newest entries are at the top. Use **Errors only** to see only problems. A red **!** appears on the tab when a new error arrives that you haven't seen yet.

When you ask for help, send a screenshot of the Log tab. It usually shows the cause.

**StakBridge page says "Cannot reach the gateway"**
The black `run.bat` window was closed. Start `run.bat` again.

**Start completely fresh**
Close the black window and delete the `data` folder inside the StakBridge folder. Then start `run.bat` again and repeat Parts 3 to 5.

---

## Real-money accounts

StakBridge works with real-money accounts the same way as demo accounts, with no setting to change. Add them only **after every test above passes on demo accounts**. Start with the smallest lot size and watch the first trades closely.

---

## Part 8 (optional): The licence and the customer's EXE

This tries the selling side on your own PC: your licence server (the SuperAdmin panel) and a customer's `StakBridge.exe`. Nothing here touches your real accounts in `data\`. About 30 minutes.

### L1: Start the licence server

1. Double-click **`run_panel.bat`**. The first time it installs a few things, then asks you to choose a **SuperAdmin login** (username and password). That is *your* login for the panel.
2. The browser opens **http://127.0.0.1:5070**. Sign in with that login.

**You should see:** the **Admins** page, empty.

### L2: Create a customer and build their EXE

1. Click **Create Admin**. Fill in a name (e.g. `Test Customer`), a username, an expiry date a month ahead, **Max Client Accounts = 1**. Click **Create Admin**.
2. **Write down the temporary password** shown. It is shown only once.
3. Click **Continue to Admin detail**, then **Build EXE**. A log scrolls; it takes 3 to 5 minutes.
4. Click **Download Test Customer.exe** when it appears. Save it on your Desktop.

### L3: First sign-in

1. Close `run.bat` if it is open (both use port 8000).
2. Double-click **Test Customer.exe**. A StakBridge window opens (no browser).

**You should see:** "StakBridge for Test Customer" and the username already filled in.

3. Type the temporary password and click **Sign in**.

**You should see:** "Choose a new password". Type the temporary one, then a new one twice. You land on the Accounts tab, and at the top right: `Test Customer · licence until <date>`.

### L4: Client limit

1. Add a master account and one client account (any of your demo accounts; or see the tip below).
2. Try to add a second client account.

**You should see:** a pop-up "Client account limit reached" with the contact email and WhatsApp button. Under **Client accounts**: `Client accounts: 1 of 1 used`.

> Tip: to try this without touching MetaTrader, close the EXE and start it from a black window (Command Prompt) with the simulated broker:
> `set STAKBRIDGE_BACKEND=fake` then `"%USERPROFILE%\Desktop\Test Customer.exe"`. Any login number works then.

### L5: The panel sees the PC

In the panel, open the customer (click their name).

**You should see:** under **Registered Devices** one PC, under **Telemetry** the app version and `1 client(s)` (it reports every 15 minutes, and right after sign-in). Under **App Logs**, click **Get Logs**: within about 30 seconds a log file downloads.

### L6: Closing and reopening

1. Close the StakBridge window.
2. Open Test Customer.exe again.

**You should see:** it opens **already signed in**, with the same accounts. A second double-click while it is open only shows "StakBridge is already running".

### L7: Revoke, expiry and offline

1. In the panel, click **Revoke Admin** (and confirm).
2. In StakBridge, wait up to 30 minutes, or close and reopen it.

**You should see:** the sign-in screen with "Your sign-in has ended…". Signing in now says "This account has been revoked". In the panel click **Reactivate** and sign in again: everything is back.

3. Close `run_panel.bat`'s window (the licence server stops). In StakBridge, click **Check again** if a bar shows, or reopen it.

**You should see:** at the top right, in yellow: "licence server unreachable, working offline until …" (72 hours). Copying keeps working. Start `run_panel.bat` again afterwards.

> While the licence is not valid (expired or revoked), a red bar says **"Your licence is not active: no new trades are copied"**. Closes and SL/TP changes from the master are still copied, so no client is left in a trade.

### L8: Delete

In the panel, open the customer and click **Delete Admin**. Within 30 minutes (or at the next start) StakBridge signs out with "This account has been deleted by your provider", and that customer's accounts are erased from the PC.

### Free trial and "Forgot password"

These send emails, so they work once the email settings are in place on your real server (see **SERVER_SETUP.md**, Step 2). Then:
- Open `http://YOUR_SERVER/trial` on your phone, enter a name and email, type the 6-digit code from the email: you get the download link and a username and password by email. A second trial with the same email, or with a new email on the same PC, is refused.
- On the StakBridge sign-in screen, **Forgot your password?** sends a code to the customer's email and lets them choose a new password.

---

## Part 9: The screen (2 minutes)

These work the same in `run.bat` (browser) and in `StakBridge.exe`.

- **U1 Zoom:** press **Ctrl + Plus** twice: the page grows and a note says "Zoom 125%". **Ctrl + Minus** shrinks it, **Ctrl + 0** goes back to 100%. **Ctrl + mouse wheel** works too. Close and reopen StakBridge: the zoom is the same as before. Do the same with the light/dark switch.
- **U2 Full width:** zoom out to 80%: the tables still fill the window from side to side and each account stays on one line.
- **U3 Check panel:** press **Check** on an account. A "Connection OK" panel slides in under it and disappears by itself after about 8 seconds. Click the account's row (not a button): the last check shows again and stays. Click **✕**: it fades away. A check that shows a problem (for example Algo Trading OFF) stays until you close it.

---

## Practice without MetaTrader (optional)

Double-click **`run_demo.bat`** instead of `run.bat`. It uses a pretend broker: nothing real is traded, gold always costs 2321.45, and any login works. Its data is kept separately in the `data_fake` folder, with its own password. In this mode there's no MetaTrader to trade in, so you can look around the screens but not place master trades.

---

## Checklist

Copy this and tick each line as it passes.

```
Part 1   [ ] 354 passed
Part 4   [ ] all 3 accounts Connected, Trading ON
Part 5   [ ] Copier shows "Watching acct_a"
Part 6   [ ] C1  [ ] C2  [ ] C3  [ ] C4  [ ] C5  [ ] C5b  [ ] C6  [ ] C7  [ ] C8  [ ] C9  [ ] C10
Part 7   Copy open: ____ ms median, ____ ms max (broker ____ ms)
         Copy close: ____ ms median, ____ ms max (broker ____ ms)
Part 8   [ ] L1  [ ] L2  [ ] L3  [ ] L4  [ ] L5  [ ] L6  [ ] L7  [ ] L8   (optional)
Part 9   [ ] U1  [ ] U2  [ ] U3
```
