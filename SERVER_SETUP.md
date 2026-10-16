# StakBridge licence server — setup, step by step

This guide puts the **StakBridge SuperAdmin server** on a Windows VPS (or any Windows PC that stays on).

## What the server is

StakBridge is sold as a desktop program (an `.exe`). The licence server is the website **you** run, which decides who may use that program:

| Who | What they use |
| --- | --- |
| **You** (SuperAdmin) | The SuperAdmin website: create customers, set expiry dates and limits, build their `.exe`, see who is online |
| **Your customer** (Admin) | The StakBridge `.exe` on their PC. It signs in with the username and password you give them |
| **Anyone interested** | The free-trial page `/trial`: they enter their email, get a code, and download a 7-day trial |

The customer's MetaTrader logins and trades **never** go to your server. It only learns counts (how many accounts, whether copying is running) and a short list of recent trades (symbol, side, lots, status).

**Time needed:** about 30 minutes, plus a few minutes for the first `.exe` build.

---

## Before you start — have these ready

| Thing | Why | Example |
| --- | --- | --- |
| A Windows Server / VPS that stays on | Everything runs here | any provider |
| Its public IP address | How you and your customers reach it | `203.0.113.45` |
| An email mailbox for sending | Trial codes, welcome emails and expiry reminders come from it | `hello@thinkstak.com` on Hostinger |
| That mailbox's password | The server needs it to send | — |
| Your own email address | Where you get "new trial signup" alerts and reminder copies | `you@gmail.com` |

---

## Step 1 — Make a clean copy of the project

On **your own PC**, double-click **`make_server_package.bat`** in the StakBridge folder.

It creates `server_package\StakBridgeServer\` (and `StakBridgeServer.zip`, the same thing zipped). It contains the whole project **minus everything that belongs to your PC**: your StakBridge data (`data\`, `data_fake\` with your MetaTrader passwords), any local licence database, keys, logins, `.venv` and caches.

> **Why the whole project?** The **Build EXE** button builds the customer's `.exe` on the server, so the app's source must be there too. Customers only ever get the finished `.exe`.

> **Never paste your project folder itself** onto the server. Always use the package.

## Step 2 — Put it on the server

1. Copy `StakBridgeServer.zip` to the server (Remote Desktop copy-paste works).
2. Unzip it so the files are in **`C:\StakBridgeServer\`**.
3. Install **Python 3.12** from https://www.python.org/downloads/ and tick **"Add python.exe to PATH"** during the install. (Not the Microsoft Store Python.)

## Step 3 — Run the setup

Double-click **`C:\StakBridgeServer\setup_server.bat`** and click **Yes** on the administrator prompt. Answer its questions:

| Question | What to type |
| --- | --- |
| SuperAdmin username / password | Your own login for the website (password at least 8 characters, typed twice) |
| Port for the panel | Press Enter for **5070**, or type another free port such as **5050** (only if nothing else uses it any more). 5000, 5001, 8000 and 8010 are refused. |
| Public server address | Press Enter to accept `http://YOUR_IP:5070`, or type your HTTPS address if you already have one, e.g. `https://stakbridge.thinkstak.com` |
| Email settings | Already filled in from `email_defaults.env` (`hello@thinkstak.com` on Hostinger, alerts to your Gmail). Answer **y** to "Keep these email settings?" |

The script then installs everything, saves your settings in `server_settings.env` (only Administrators can read it), opens the port in Windows Firewall, makes the panel start at every boot, starts it and checks that it answers.

**Also open port 5070 in your VPS provider's own firewall panel** (their website), otherwise nobody outside can reach it.

You can run `setup_server.bat` again at any time to change a setting.

Other helpers in the same folder:

| File | Use it to |
| --- | --- |
| `restart_server.bat` | Restart the panel, e.g. after pasting an update or editing `server_settings.env` |
| `stop_server.bat` | Stop the panel (it starts again at the next reboot) |
| `setup_https.bat` | Put the panel on your own domain with HTTPS (see below) |
| `logs\server.log` | Read what went wrong if the panel does not start |

> If the Forex CopyTrader licence server runs on the same VPS, the two stay apart: different folder, different port (choose one other than the CopyTrader's 5050), different Windows task and firewall rule. Stopping or restarting one never touches the other.

## Step 4 — HTTPS (recommended)

Without HTTPS, passwords travel over the internet unencrypted. Pick **one**:

**A. IIS + win-acme** (what the Forex CopyTrader server uses):
1. In your DNS (Hostinger: hPanel → Domains → DNS), add an `A` record, e.g. name `stakbridge`, pointing to the VPS IP.
2. In IIS, add a new site for `stakbridge.thinkstak.com` (folder e.g. `C:\inetpub\stakbridge\`) whose `web.config` forwards (URL Rewrite / ARR reverse proxy) to `http://127.0.0.1:5070`. Run `iisreset`.
3. Run win-acme for that site to get the free certificate.
4. Re-run `setup_server.bat` and type `https://stakbridge.thinkstak.com` as the public server address.

**B. Caddy** (`setup_https.bat`, simplest if IIS is not already using ports 80/443):
1. Add the DNS `A` record as above and allow TCP **80** and **443** in your VPS provider's firewall.
2. Double-click `setup_https.bat`. It checks the DNS, gets the certificate, updates the server address and starts everything at every boot.

After switching to HTTPS, **rebuild every customer's `.exe`** (each `.exe` keeps the address it was built with). The trial `.exe` rebuilds itself.

## Step 5 — First sign-in and Settings

Open `http://YOUR_IP:5070` (or your HTTPS address) and sign in with the SuperAdmin login from Step 3. Click **Settings**:

1. **Server Address** — must be the address customers' PCs will use, e.g. `https://stakbridge.thinkstak.com`. Fix it **before** building any `.exe`; an `.exe` with a wrong address cannot sign in.
2. **Email** — should say it is sending via your SMTP host. If it says "SMTP is not configured", re-run `setup_server.bat` and check the email answers.
3. **Your notification email** — where trial alerts and expiry-reminder copies go. Save.
4. **Support contact** — the email and WhatsApp link customers see when they reach their account limit.
5. **Free Trial Signups** — shows the public link (`/trial`) and whether the trial `.exe` is ready. It builds itself the first time someone opens `/trial`; click **Rebuild Trial EXE** after every new StakBridge version.

## Step 6 — Give StakBridge to a paying customer

1. **Create Admin**: their name, email, expiry date, how many PCs they may use (installations) and how many client accounts (or Unlimited).
2. Copy the **temporary password** shown. It is shown **once only**.
3. On their page click **Build EXE**, wait a few minutes, then **Download**. You get `<Their Name>.exe`.
4. Send them the `.exe`, their username and the temporary password. At their first sign-in StakBridge makes them choose their own password.

**To renew**, just change their expiry date and save. Never send a new `.exe` for a renewal: their StakBridge asks your server every 30 minutes and picks up the new date.

On a customer's page you can also: revoke or reactivate them, reset or set their password, free a PC slot (**Revoke device**), see their client-account usage and last check-in, and **request their app log** for up to 7 days (it downloads as a `.txt` once their StakBridge has sent it, usually within a minute).

**Delete Admin** removes them completely: their StakBridge signs out, and their local StakBridge data on their PC is erased the next time it checks in.

## Step 7 — Test like a customer

1. On your phone, open `http://YOUR_IP:5070/trial` (or your HTTPS address + `/trial`).
2. Enter a real email you can read. You get a 6-digit code; type it in.
3. You see a **Download** button and get an email with the download link, a username and a temporary password. You also get a "New trial signup" email.
4. Clean up: on the dashboard open that test Admin and click **Delete Admin**.

> One trial per email and one trial per PC. To let someone try again: delete their trial Admin, or **Settings → PCs that have used a free trial → Allow new trial**.

## Updating the server later

1. On your PC, double-click `make_server_package.bat`.
2. Copy the zip to the server and paste the `StakBridgeServer` folder over `C:\StakBridgeServer` (replace files).
3. Double-click `restart_server.bat`.
4. In **Settings**, click **Rebuild Trial EXE**, and rebuild customers' `.exe` files if the app changed.

The package never contains the server's database, login or keys, so pasting it cannot overwrite them. It does contain `email_defaults.env` (the default mailbox and its password), so keep the zip private.

## Back up these regularly (together)

```
C:\StakBridgeServer\superadmin_panel\superadmin.db        all your customers and trials
C:\StakBridgeServer\superadmin_panel\keys\                your licence signing keys
C:\StakBridgeServer\superadmin_panel\superadmin_config.json   your SuperAdmin login
C:\StakBridgeServer\superadmin_panel\flask_secret_key.txt
C:\StakBridgeServer\server_settings.env                   your settings and email password
```

**Losing `keys\` means every installed StakBridge stops trusting your server** (customers then work only while online, and every `.exe` must be rebuilt).

## If something goes wrong

| What you see | Fix |
| --- | --- |
| Your phone can't open the page, but the server can | Open port 5070 in the VPS provider's firewall panel |
| `python is not recognized` | Reinstall Python with "Add to PATH" ticked |
| The panel does not start | Read `logs\server.log`; run `restart_server.bat` |
| "Port 5070 is already in use" | Another program uses it. Re-run `setup_server.bat` and choose another port (and update IIS's `web.config` to match) |
| Settings says SMTP is not configured | Re-run `setup_server.bat` and fill in the email questions |
| No trial email arrives | Wrong mailbox password, or port 465 blocked: try port `587` with `STARTTLS` |
| **Build EXE** shows a list of problems | The list says what is missing; usually only `superadmin_panel\` was copied. Use the package from Step 1 |
| Customer gets a connection error at sign-in | Their `.exe` has a wrong address: fix **Settings → Server Address**, rebuild, resend |
| "This device has already used a free trial" | **Settings → PCs that have used a free trial → Allow new trial** |
| IIS shows "502 invalid response" | The panel is stopped or on another port than `web.config` says. Run `restart_server.bat`; make both ports the same; `iisreset` |
| Forgot the SuperAdmin password | Use **Forgot password** on the sign-in page (code goes to your notification email), or re-run `setup_server.bat` and answer **n** to "Keep it?" |

## Quick reference

| | |
| --- | --- |
| SuperAdmin website | `http://YOUR_IP:5070` or your HTTPS address |
| Free-trial link to share | `<that address>/trial` |
| Server folder | `C:\StakBridgeServer\` |
| Restart | `restart_server.bat` |
| Log | `logs\server.log` |
