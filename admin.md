# Admin / Licensing System — Feature Reference (for porting)

This document lists **everything the licensing side of ThinkStak Forex CopyTrader does** — the SuperAdmin panel, Admin accounts, EXE creation, the 7‑day free trial, email OTP verification, "one trial per computer", emails, offline licensing, client limits, telemetry and log requests — and **which file implements each piece**, so the same functionality can be copied into another copy trader.

None of this is tied to the trading logic. The copy engine (MetaApi / MT5) only touches it in three places: the login gate, the "license valid?" check before **Start**, and the client‑count limit on **Add / Import client**.

---

## 1. The three roles

| Role | What they are | Where they work |
|---|---|---|
| **SuperAdmin** (you / the owner) | Sells and controls licenses | Web panel `superadmin_panel/` (Flask, port 5050) hosted on the VPS |
| **Admin** (your customer) | Runs the copy trader | Desktop EXE (`desktop_launcher.py` → `app.py`, local port 5077) |
| **Client** (Admin's trading accounts) | MetaApi accounts the Admin copies into | **No login, no dashboard** — just rows in the Admin's app |

Key rule: **client data (MetaApi tokens, order logs) never leaves the Admin's PC.** The server only ever learns counts.

---

## 2. Architecture at a glance

```
          ┌───────────────────────────── VPS ─────────────────────────────┐
Browser → │ SuperAdmin panel (session cookie)   /login, /admins/..., /settings
Prospect→ │ Public trial signup                 /trial, /trial/verify, /trial/download
          │ Admin‑app JSON API (opaque token)   /api/auth/*, /api/clients/*,
          │                                     /api/telemetry/sync, /api/logs/*
          │ SQLite superadmin.db  +  keys/ (Ed25519 private key)  +  built_exes/
          └───────────────────────────────────────────────────────────────┘
                              ▲  outbound HTTPS only (server never calls the PC)
          ┌─────────── Admin PC / VPS ───────────┐
          │ <Name>.exe  (PyInstaller onefile)    │
          │  pywebview window → Flask 127.0.0.1  │
          │  %LOCALAPPDATA%\ThinkStakForexCopyTrader\
          │     session.bin (DPAPI encrypted)    │
          │     license_cache.json (signed)      │
          │     admins\<admin_id>\  (clients, logs, mappings)
          └──────────────────────────────────────┘
```

- **Push model only**: the EXE calls the server (login, refresh, telemetry, log‑request polling). The server never opens a connection to the Admin's machine, so NAT / dynamic IP / firewalls don't matter.
- Production hosting: `waitress-serve --listen=0.0.0.0:5050 superadmin_panel.app:app`, run as a Windows service via NSSM, behind IIS reverse proxy + win‑acme for HTTPS (see `SETUP.md`).

---

## 3. SuperAdmin panel features (`superadmin_panel/superadmin_web.py`)

### 3.1 SuperAdmin login
- Single SuperAdmin account. Created with:
  `python -m superadmin_panel.bootstrap_superadmin --username owner --password "..."`
- Only the **Argon2id hash** is stored (`superadmin_config.json`, git‑ignored), or provided via env vars `SUPERADMIN_USERNAME` / `SUPERADMIN_PASSWORD_HASH`.
- Flask session cookie (HttpOnly, SameSite=Lax), `@login_required` decorator on every page.
- Login is **rate limited** per IP (10 attempts / 5 min — `rate_limit.py`, in‑memory sliding window).
- **Forgot password** for the SuperAdmin too (email OTP to the "owner notification email" from Settings — see §6.3).

### 3.2 Dashboard (`/`)
Table of all Admins: Name, Username, Email, Status, Expiry, Allowed Installs, **Clients (used / allowed)**, Created, **Last Seen** ("3 min ago", from telemetry).

### 3.3 Create Admin (`/admins/new`)
Fields: name, username (unique), email (optional, used for reminders + forgot password), expiry date (YYYY‑MM‑DD), **allowed installations** (default 1), **max client accounts** (default 5, or tick **Unlimited**), notes.
- Server generates a random **temporary password** (12 chars, no look‑alike characters).
- The temp password is shown **exactly once** on `/admins/<id>/created` (stored only in the flask session, popped on view). Never saved in plain text.
- New Admin has `must_change_password = 1` → the app forces a password change at first login.

### 3.4 Admin detail page (`/admins/<id>`)
| Section | What it does |
|---|---|
| **Details / Edit** | Change name, email, expiry date, allowed installations, max client accounts / unlimited, notes |
| **Revoke / Reactivate** | Revoke sets `status='revoked'` **and kills every session immediately**. Reactivate sets it back to active |
| **Reset password** | Generates a new temp password (shown once), forces change on next login, kills all sessions |
| **Set password** | SuperAdmin types a specific new password (min 8, confirm), optional "must change on next login", kills all sessions |
| **Delete** | Full reset — see §3.6 |
| **Build EXE** | Builds `<Admin name>.exe` for this customer — see §5 |
| **Registered devices** | List of PCs (fingerprint, first/last seen, status) with "x / allowed" slots. **Revoke device** frees a slot and kills its session; **Reactivate** only if a slot is free |
| **Client usage** | Used / allowed, broken down per device, with "updated X ago" |
| **App Logs** | Request the Admin's app log for a date range (max 7 days) and download it — see §9 |
| **Telemetry** | Last check‑in: app version, copier running/status, client count, enabled count, last 20 trades (symbol/side/qty/status only) |

### 3.5 Settings page (`/settings`)
- **Server address** (`public_server_url`) — the URL baked into every EXE built from now on. Precedence: Settings value → `PUBLIC_SERVER_URL` env var → the address the browser used. Saving it **auto‑rebuilds the trial EXE**.
- **Owner notification email** — receives expiry‑reminder copies and "new trial signup" alerts; also the SuperAdmin's forgot‑password email.
- **SMTP status** — shows whether SMTP env vars are configured (host / from address).
- **Support contact** — email + WhatsApp link shown in the app's "limit reached" pop‑up (defaults `hello@thinkstak.com`, `https://wa.me/918273366104`). Apps pick it up on next license check.
- **Free trial signups** — public signup URL (`/trial`), trial length, trial EXE status (built for which URL, when), **Rebuild trial EXE** button.
- **PCs that have used a free trial** — list of device fingerprints with an **"Allow new trial"** button to clear one PC.

### 3.6 Delete Admin = full reset
`repositories.delete_admin()`:
1. Deletes their rows in `trial_devices` and `trial_emails` → **that email and PC can take a new trial again**.
2. Writes a tombstone in `deleted_admins`.
3. Deletes the admin row → devices, sessions, telemetry, client_usage, log_requests cascade.

On the Admin's PC: the next `/api/auth/refresh` (startup, every 30 min, before Start, client‑limit refresh) sends its `admin_id`; the server sees the tombstone and returns `admin_deleted`. The app then **stops the copier, logs out, clears session + license cache, and deletes `admins\<admin_id>\`** (clients, order logs, ticket/symbol mappings) — `app.py::_erase_if_admin_deleted`.

---

## 4. Admin accounts & authentication

### 4.1 Data model (SQLite, `superadmin_panel/db.py`)
| Table | Purpose |
|---|---|
| `admins` | id (uuid), name, username UNIQUE, password_hash (Argon2id), must_change_password, expiry_date, status (`active`/`revoked`), allowed_installations, max_client_accounts (NULL = unlimited), email, notes, signup_source (`trial_signup` or NULL), last_expiry_reminder_sent_for, created_at |
| `devices` | one row per (admin, device_fingerprint); status active/revoked; first/last seen |
| `sessions` | opaque token **SHA‑256 hash** only, admin_id, device_id, issued/expires, revoked |
| `telemetry` | last check‑in per Admin |
| `client_usage` | client count per (admin, device) — the limit pool |
| `settings` | key/value (server URL, owner email, support contact) |
| `trial_emails` | emails that have used a trial |
| `trial_devices` | PC fingerprints that have used a trial |
| `trial_pending_signups` | email OTP waiting to be verified (hashed code) |
| `password_reset_codes` | forgot‑password OTPs + one‑time reset tokens |
| `deleted_admins` | tombstones so the app learns "you were deleted" |
| `log_requests` | SuperAdmin → app log‑upload requests |

`init_db()` creates tables and runs **additive column migrations** (`ALTER TABLE ... ADD COLUMN`) for older DBs.

### 4.2 Sessions — opaque tokens, not JWT
- `security.generate_token()` = `secrets.token_urlsafe(32)`; the DB stores only `sha256(token)`.
- Revocation is one DB write (revoke admin / reset password / revoke device / delete) — **instant**, no blocklist.
- TTL 24 h (`ADMIN_SESSION_TTL_SECONDS`), **extended on every refresh**.
- Changing password revokes **all** sessions and issues a fresh one.

### 4.3 Admin‑app JSON API (`superadmin_panel/auth_api.py`, prefix `/api/auth`)
| Endpoint | Body | Notes |
|---|---|---|
| `POST /login` | username, password, device_fingerprint | Rate limited per IP. Checks in order: credentials → revoked → expired → device revoked → **installation limit** → **trial already used on this PC** → registers device → issues session + signed license cache |
| `POST /refresh` | token, admin_id | Extends session, touches device, returns fresh license fields + signed cache; `admin_deleted` if tombstoned |
| `POST /logout` | token | Idempotent |
| `POST /change-password` | token, current_password, new_password (≥8) | Rotates all sessions |
| `POST /forgot-password` | email | Sends OTP |
| `POST /forgot-password/verify` | email, code | Returns one‑time `reset_token` |
| `POST /forgot-password/reset` | email, reset_token, new_password, confirm_password | Sets password, kills sessions |

Error codes returned (the app shows `message` as‑is): `invalid_credentials`, `admin_revoked`, `license_expired`, `device_revoked`, `installation_limit_reached`, `trial_already_used`, `invalid_session`, `admin_deleted`, `rate_limited`.

Security details worth copying:
- Unknown username still runs a dummy Argon2 verify (**timing parity** — can't probe usernames).
- Same "Invalid username or password." for wrong user and wrong password.
- Expiry is **inclusive of the expiry date** (`is_admin_expired`: expired when today > expiry_date, UTC).

### 4.4 Admin app side
| File | Role |
|---|---|
| `auth_client.py` | HTTP client; every call returns `{"ok", "data"}` or `{"ok": False, "error", "message", "network_error"}` — `network_error` separates "server unreachable" from "server said no" |
| `server_config.py` | Server URL: `COPYTRADER_SERVER_URL` env → baked `build_settings.json` → `http://127.0.0.1:5050`. `post_json()` keeps POST across 301/302 redirects (HTTP→HTTPS) |
| `auth_state.py` | In‑memory login state for this process |
| `session_store.py` | Saves the session to `session.bin` encrypted with **Windows DPAPI** — copying the file to another PC/user can't be decrypted, forces login |
| `app.py` login routes | `/login`, `/logout`, `/change-password` (forced when `must_change_password`), `/forgot-password` (+ `/request`, `/verify`, `/reset` relays). `@before_request require_login` guards everything else |
| `restore_session_on_startup()` | Reopens the app logged in if `session.bin` decrypts; runs a license refresh immediately |
| `runtime_paths.py` | `%LOCALAPPDATA%\ThinkStakForexCopyTrader\` = device root; `admins\<admin_id>\` = per‑Admin data, so two Admins on one PC never see each other's clients |
| Login screen | Shows the Admin's name (from the baked build settings) and pre‑fills username |

---

## 5. EXE creation

### 5.1 How the EXE is built
- **PyInstaller `--onefile`, windowed** (no console) from `ThinkStakForexCopyTrader.spec`, entry point `desktop_launcher.py`.
- `desktop_launcher.py`: starts the Flask app in a werkzeug thread on `127.0.0.1:5077` (`COPYTRADER_HOST` can make it `0.0.0.0` for VPS use) and opens a **pywebview** native window (WebView2). Window close → stops license checks, stops copier, shuts the server, `os._exit(0)`.
- **Single‑instance guard**: if port 5077 already answers, show "already running" message box and exit.
- Spec bundles `templates/`, `static/`, `license_public_key.pem`, optional `build_settings.json`; `collect_all` for MetaApi SDKs / socketio / engineio / aiohttp; hidden imports for pywebview backends; **excludes** `superadmin_panel`, `tests`, `argon2`, `pytest`. Icon `packaging/thinkstak.ico`, Windows version info `packaging/version_info.txt`.
- Manual build: `build.bat` → `pyinstaller ThinkStakForexCopyTrader.spec --clean --noconfirm` → `dist\ThinkStakForexCopyTrader.exe`.

### 5.2 Spec env vars (set by the server's builder)
| Env var | Effect |
|---|---|
| `COPYTRADER_EXE_NAME` | Output file name, e.g. `John Smith` → `John Smith.exe` |
| `COPYTRADER_BUILD_SETTINGS` | Path to a `build_settings.json` bundled at the root: `{server_url, admin_name, admin_username, built_at}` |
| `COPYTRADER_PUBLIC_KEY` | Path to the **server's own** `license_public_key.pem` to bundle (so the EXE always trusts the server that built it) |

### 5.3 "Build EXE" button in the SuperAdmin panel (`superadmin_panel/exe_builder.py`)
1. `preflight()` checks: source folder exists (`EXE_BUILD_SOURCE_ROOT`), `.spec` + `desktop_launcher.py` present, PyInstaller installed, running on Windows. Problems are shown **before** the button is used.
2. `POST /admins/<id>/build-exe` → starts a background thread, returns a job id (202). **Only one build at a time** (`threading.Lock`; PyInstaller can't run in parallel on one source tree) — a second request gets 409 "busy".
3. Builder writes `build_settings.json` + the public key into a temp dir, runs `python -m PyInstaller ThinkStakForexCopyTrader.spec --clean --noconfirm --distpath <tmp> --workpath <tmp>` with the env vars above, streaming output into the job log.
4. Panel polls `GET /admins/<id>/build-exe/status` (live log, last 40 lines).
5. Result copied to `built_exes/<admin_id>/<Name>.exe` (older EXEs for that Admin removed) + `build_info.json` (server URL, built_at).
6. `GET /admins/<id>/build-exe/download` sends the file.

What's baked in is **non‑secret only**: server URL, display name, username. Never a password, never a token, never the private key. The per‑Admin EXE is a convenience — sharing is still stopped by device fingerprint + installation limit.

### 5.4 Shared trial EXE
- Trials **do not** get a per‑person build. One shared EXE (`built_exes/_trial/ThinkStak Forex CopyTrader.exe`) with **no name/username baked in**.
- `ensure_trial_exe(server_url)` auto‑builds it when missing or when built for a different server URL; called when `/trial` is opened, on signup, on download, and when the server address changes. 10‑min back‑off after a failed build. Settings has a **Rebuild** button (force).
- `/trial/download`: serves the EXE; if a build is in progress shows a self‑refreshing "preparing" page (202); if impossible shows "unavailable" (503).
- Signup is refused ("Trials aren't available right now") if no trial EXE exists **and** the server can't build one.

---

## 6. Email functionality (`superadmin_panel/email_notifications.py`)

Plain `smtplib`, no third‑party library. Configured **only by env vars** (never DB/source):

| Env var | Example |
|---|---|
| `SMTP_HOST` | `smtp.hostinger.com` |
| `SMTP_PORT` | `465` (SSL) or `587` (STARTTLS) |
| `SMTP_SECURITY` | `SSL` or `STARTTLS` |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | mailbox login |
| `SMTP_FROM_ADDRESS` | defaults to username |
| `OWNER_NOTIFICATION_EMAIL` | fallback owner address (Settings value wins) |

Emails sent:

| Email | To | When |
|---|---|---|
| **Trial verification code** (6‑digit OTP) | Prospect | `/trial` form submitted |
| **Trial welcome** — download link, username, temp password, expiry date | Prospect | Code verified |
| **New trial signup** alert (no password) | Owner | Same time; failure here never fails the signup |
| **Password reset code** (6‑digit OTP) | Admin or SuperAdmin | Forgot password |
| **License expires tomorrow** reminder | Admin (if email on file) + owner | ~24 h before expiry |

### 6.1 Expiry reminder (`expiry_reminder.py`)
- Background daemon thread, checks every hour (`EXPIRY_REMINDER_CHECK_INTERVAL_SECONDS`).
- Enabled with `EXPIRY_REMINDER_ENABLED=1` under waitress (always on when run as `python -m superadmin_panel.app`). Idempotent start — never two loops.
- Picks active Admins whose `expiry_date == tomorrow` and `last_expiry_reminder_sent_for != expiry_date` → **changing the expiry date automatically re‑arms the reminder**. One bad address doesn't stop the batch.

### 6.2 OTP rules (shared by trial + password reset)
- 6 digits from `secrets.randbelow(10**6)`, stored **Argon2‑hashed**, never plain.
- Expires in **10 min** (`TRIAL_CODE_TTL_SECONDS` / `PASSWORD_RESET_CODE_TTL_SECONDS`).
- **5 wrong attempts** max, then the code is deleted ("request a new one"). Shows "N tries left".
- **60 s resend cooldown**; a double‑click within the cooldown doesn't send another email — the page shows a live countdown instead.
- If the email fails to send, the pending row is deleted and the user is told to retry.

### 6.3 Forgot password (`password_reset.py`) — 3 steps, both account types
1. `request_code(email)` — same "If an account uses this email, a code has been sent" answer for known and unknown emails (**no account enumeration**). Admin = `admins.email`; if several Admins share an email → refuse, SuperAdmin must reset. SuperAdmin = owner notification email.
2. `verify_code(email, code)` — right code is consumed and swapped for a one‑time **reset token** (hashed in DB).
3. `complete_reset(email, token, new, confirm)` — min 8 chars, must match. For an Admin: sets password, clears must_change, **kills all sessions**. For SuperAdmin: rewrites `superadmin_config.json` (refused if credentials come from env vars).
- Rate limited per IP: 10 / 15 min.
- In the Admin EXE, `/forgot-password` page walks email → code → new password, relaying to the server.

---

## 7. 7‑day free trial with email OTP (`superadmin_panel/trial_signup.py`)

Public, no login. The business model: owner configures nothing — prospects self‑serve.

**Step 1 — `GET/POST /trial`** (name + email)
- IP rate limit: **5 code emails per hour** (refused/failed attempts are "given back" and don't count).
- Validates name + plausible email.
- **Refuses if the email was ever used**: existing Admin with that email, or present in `trial_emails` → *"You have already used the trial from this particular email."* (one message for all cases so the form can't probe which emails exist).
- Saves `trial_pending_signups` (hashed code, expiry) and emails the OTP. Nothing else is created yet.

**Step 2 — `POST /trial/verify`** (email + code)
- Checks expiry, attempt count, code. Re‑checks the email refusal (could have changed while waiting).
- **Only now** creates the Admin:
  - username derived from the email's local part (sanitized, random 4‑digit suffix if taken, retries on race)
  - random temp password, `must_change_password = 1`
  - `expiry_date = today + 7 days` (`TRIAL_LENGTH_DAYS`) — counted from signup, not first login
  - `allowed_installations = 1`, `max_client_accounts = 5` (`TRIAL_MAX_CLIENT_ACCOUNTS`)
  - `signup_source = 'trial_signup'`
  - records the email in `trial_emails`
- Sends the welcome email (download link + credentials) and the owner alert.
- **If the welcome email fails**, the Admin and the trial_emails row are deleted (the password only travels in that email — otherwise the person is locked out of a trial they never received).
- Shows the "thanks" page with the download link.

**Step 3 — `GET /trial/download`** → shared trial EXE (§5.4).

**Converting a trial to paid**: SuperAdmin just edits the Admin — new expiry date, raise limits. (`signup_source` stays `trial_signup`, which only matters for the one‑PC rule below.)

---

## 8. "User cannot take a demo from the same computer again"

### 8.1 Device fingerprint (`device_fingerprint.py`)
- Reads Windows **`HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid`** (stable across reboots, users, hostname changes).
- Fallback: `uuid.getnode()` (MAC‑based) if the registry can't be read.
- Sent as **SHA‑256 hex** — the server never sees the raw GUID.

### 8.2 One trial per PC — enforced at **login** (`auth_api.py`)
Only for Admins with `signup_source == 'trial_signup'`, and only the first time a fingerprint logs into that Admin:
- If the fingerprint is already in `trial_devices` **under a different Admin** → `403 trial_already_used`: *"This device has already used a free trial. Contact us to continue."*
- Otherwise the fingerprint is recorded in `trial_devices` for this Admin.
- Paying Admins are **never** affected by this check.

Combined with §7: a person can't re‑trial with the **same email** (checked at signup) or with a **new email on the same PC** (checked at login).

### 8.3 Ways to allow a re‑trial
- **Delete the trial Admin** → its `trial_devices` / `trial_emails` rows are removed (full reset).
- **Settings → "PCs that have used a free trial" → Allow new trial** → clears just that PC.

### 8.4 Installation limit (all Admins, not just trials)
- `allowed_installations` per Admin (default 1, trials = 1).
- New fingerprint + active devices ≥ limit → `installation_limit_reached`.
- Revoked device → `device_revoked`. SuperAdmin revokes an old PC to free its slot.

---

## 9. Licensing & offline grace

### 9.1 Signed offline license cache (Ed25519)
- Server (`license_signing.py`) generates an **Ed25519 keypair** on first use in `superadmin_panel/keys/`. **Private key never leaves the server — back it up**; a new key = every existing EXE rejects the cache.
- Every login / refresh / change‑password returns `license_cache = {payload, signature}`. Payload: admin_id, device_id, status, license_valid, expiry_date, allowed_installations, max_client_accounts, verified_at, grace_until (now + **72 h**, `ADMIN_OFFLINE_GRACE_HOURS`), issued_at. Signed over canonical JSON (sorted keys, no spaces).
- EXE bundles only the **public key** (`license_signing_client.py`) — it can verify but never forge. Editing `license_cache.json` breaks the signature → ignored.

### 9.2 License decision (`license_state.refresh_license_state`)
Run at startup, **before every Start**, and every **30 min** in the background:
1. Server reachable + OK → use live result, save the new signed cache.
2. Server reachable + says no (revoked / expired / invalid / deleted) → **that wins**, a cached "valid" can never override it.
3. Server unreachable → use the verified cache **until `grace_until`** (72 h).
4. No token / no cache / grace over → not valid.

When invalid, the background checker **stops the copier** (never force‑closes open positions). Start is refused with the server's message.

---

## 10. Client account limit (per Admin)

- `max_client_accounts` set by SuperAdmin (default 5, trials 5, or **Unlimited** = NULL).
- **All saved clients count** (enabled or disabled); only deleting frees a slot.
- The limit is a **shared pool across all of the Admin's servers/devices** (e.g. 11 + 8 = 19 / 20). Revoking a dead device releases its slots.
- App (`client_limit.py`) calls `POST /api/clients/reserve {token, client_count}` **before** Add client / CSV import. Server (`client_usage_api.py` + `repositories.reserve_client_slots`, `BEGIN IMMEDIATE` lock) answers allowed / `409 client_limit_reached`. Shrinking is always allowed.
- Adding needs the server online; deleting works offline (reported later; telemetry always repairs the count).
- App shows **"used / limit"** on the Users tab and a **"limit reached — contact provider"** pop‑up with support email + WhatsApp link from Settings.
- Limit also travels inside the signed cache, so it's trusted offline.

---

## 11. Telemetry & remote log download

### 11.1 Telemetry (`telemetry_sync.py` → `POST /api/telemetry/sync`)
- Every **15 min** (+ immediately on login / startup restore). Same session token.
- Sends: app version, copier status/running, client count, enabled client count, last 20 trades (symbol/action/side/qty/status/time).
- **Never** sends MetaApi tokens, passwords, or full order payloads.
- Gives the dashboard "Last seen" and keeps client usage correct.

### 11.2 SuperAdmin downloads an Admin's app log (`log_requests.py` / `log_request_sync.py`)
- SuperAdmin picks a date range (**max 7 days**) on the Admin page → request is queued (`pending`).
- App polls `POST /api/logs/pending` every **30 s**, reads those dates from its log files, **gzips** (≤16 MB, keeps newest lines if too big) and uploads to `POST /api/logs/upload`.
- Status: pending → picked_up → ready / failed / expired (10‑min pickup timeout). File kept **72 h**, downloaded as `.txt`.

---

## 12. Server configuration (env vars, `superadmin_panel/config.py`)

| Variable | Default | Purpose |
|---|---|---|
| `SUPERADMIN_HOST` / `SUPERADMIN_PORT` | 127.0.0.1 / 5050 | Bind address (dev run) |
| `SUPERADMIN_USERNAME` / `SUPERADMIN_PASSWORD_HASH` | — | Optional env‑based SuperAdmin login |
| `SUPERADMIN_SECRET_KEY` | auto‑generated file | Flask cookie key |
| `SUPERADMIN_DB_PATH` | `superadmin_panel/superadmin.db` | SQLite file |
| `PUBLIC_SERVER_URL` | request host | URL baked into EXEs (Settings page overrides) |
| `EXE_BUILD_SOURCE_ROOT` | repo root | Where the app source + `.spec` live |
| `EXE_OUTPUT_DIR` | `superadmin_panel/built_exes` | Built EXEs |
| `LOG_UPLOAD_DIR` | `superadmin_panel/log_uploads` | Uploaded app logs |
| `ADMIN_SESSION_TTL_SECONDS` | 86400 | Admin token lifetime |
| `ADMIN_OFFLINE_GRACE_HOURS` | 72 | Offline grace |
| `TRIAL_LENGTH_DAYS` | 7 | Trial length |
| `TRIAL_MAX_CLIENT_ACCOUNTS` | 5 | Trial client limit |
| `TRIAL_CODE_TTL_SECONDS` / `_MAX_ATTEMPTS` / `_RESEND_COOLDOWN_SECONDS` | 600 / 5 / 60 | Trial OTP |
| `PASSWORD_RESET_CODE_*` | 600 / 5 / 60 | Reset OTP |
| `LOGIN_RATE_LIMIT_*` | 10 per 300 s | Login brute force |
| `TRIAL_SIGNUP_RATE_LIMIT_*` | 5 per 3600 s | Trial OTP emails per IP |
| `PASSWORD_RESET_RATE_LIMIT_*` | 10 per 900 s | Reset attempts per IP |
| `SMTP_*`, `OWNER_NOTIFICATION_EMAIL` | — | Email (§6) |
| `EXPIRY_REMINDER_ENABLED` / `_CHECK_INTERVAL_SECONDS` | off / 3600 | Reminder loop |
| `SUPPORT_EMAIL` / `SUPPORT_WHATSAPP_URL` | hello@thinkstak.com / wa.me link | Pop‑up contact defaults |

App‑side: `COPYTRADER_SERVER_URL`, `COPYTRADER_HOST`, `COPYTRADER_LOCAL_PORT` (5077), `COPYTRADER_LICENSE_CHECK_INTERVAL_SECONDS` (1800), `COPYTRADER_TELEMETRY_SYNC_INTERVAL_SECONDS` (900), `COPYTRADER_LOG_REQUEST_POLL_SECONDS` (30), `COPYTRADER_SERVER_TIMEOUT_SECONDS` (10).

**Back up**: `superadmin.db`, `superadmin_panel/keys/`, `superadmin_config.json`, `flask_secret_key.txt`, `server_settings.env`.

---

## 13. Server scripts — `setup_server.bat`, `stop_server.bat` and friends

You install the SuperAdmin server on the VPS with one double‑click. Each `.bat` is a thin wrapper: it **asks for Administrator rights** (`net session` check; if not admin it relaunches itself with `Start-Process -Verb RunAs`), then runs **`setup_server.ps1 -Action <X>`**, which holds all the logic.

| Script | Runs | What it is for |
|---|---|---|
| `setup_server.bat` | `setup_server.ps1 -Action Setup` | First install, and re‑run any time to change settings |
| `stop_server.bat` | `-Action Stop` | Stops the panel (it starts again at next reboot or with restart) |
| `restart_server.bat` | `-Action Restart` | Stop + start, e.g. after editing `server_settings.env` or pasting new code |
| `setup_https.bat` | `-Action Https` | Optional: own domain + HTTPS via Caddy (**our live server uses IIS + win‑acme instead, not Caddy**) |
| `start_server.bat` | (run by Task Scheduler) | The actual server process loop — you never run it yourself |
| `start_caddy.bat` | (run by Task Scheduler) | Caddy loop, only if `setup_https.bat` was used |
| `make_server_package.bat` | on your dev PC | Builds a clean `server_package\ThinkStakServer\` (+ `.zip`) to paste onto the server |

### 13.1 `setup_server.bat` → `-Action Setup` (7 steps, safe to re‑run)
Before step 1: checks the **whole project** was copied (`requirements-dev.txt`, `desktop_launcher.py`, the `.spec`, `superadmin_panel\app.py` — the server needs the app source to build EXEs). Warns if the folder is inside the user profile (recommends `C:\ThinkStakServer`, since the server runs as SYSTEM). Stops a running server first so `.venv` files aren't locked.

1. **Find Python** — tries `py -3` then `python`; needs **3.10+**; **skips the Microsoft Store Python** (`WindowsApps`, can't run as a service). Fails with "install Python 3.12 from python.org" if none.
2. **Virtual env** — creates `.venv`; if an existing one is broken (copied from another PC) it deletes and recreates it.
3. **Dependencies** — upgrades pip, installs `requirements-dev.txt` **+ `waitress`** (web server, email, PyInstaller for EXE builds).
4. **SuperAdmin login** — if `superadmin_config.json` exists asks "Keep it?"; otherwise asks username (default `owner`) + password twice (hidden input, min 8). Passes them to `bootstrap_superadmin` via **env vars, not the command line**, so the password never appears in the process list.
5. **Server settings** → written to **`server_settings.env`**:
   - `SERVER_PORT` (default **5050**, 1024–65535; refuses ports reserved for other services on this VPS: 5000, 5001, 8000, 8010)
   - `PUBLIC_SERVER_URL` — auto‑detects the public IP (`api.ipify.org`) and suggests `http://<ip>:<port>`; never overwrites an existing https domain address
   - Email: SMTP host (default `smtp.hostinger.com`), port (465/587), security (SSL/STARTTLS, auto from port), mailbox, mailbox password (blank = keep current), owner email. Offers "Keep these email settings?" on re‑run.
   - `EXPIRY_REMINDER_ENABLED` = 1 only if SMTP is fully set, else 0 with a warning.
   - File permissions locked with `icacls` to **Administrators + SYSTEM only** (it contains the mailbox password).
   - Also writes the URL straight into the panel DB's **Settings → Server Address** (a tiny Python script via `.venv`), because the DB value overrides the env var — otherwise new EXEs would still get the old address.
6. **Windows Firewall** — removes and re‑creates the inbound TCP rule `"ThinkStak SuperAdmin Panel"` for the port (skipped if `SERVER_BIND=127.0.0.1`, i.e. behind a proxy). Reminds you to also open the port in the **VPS provider's** firewall.
7. **Run at every boot** — registers Task Scheduler task **`"ThinkStak SuperAdmin Server"`**: runs `cmd /c start_server.bat` **as SYSTEM**, trigger **At startup**, no time limit, restart up to 999× every 1 min, ignore new instances. Then starts it and **polls `http://127.0.0.1:<port>/login` for up to 45 s**; on failure prints the last 25 lines of `logs\server.log`. Refuses to start if the port is already taken (shows which process).

Ends with a summary: panel URL, `/trial` URL, log path, and next steps (check Settings → Server Address, trial EXE builds itself, IIS `web.config` must forward to `127.0.0.1:<port>` then `iisreset`).

### 13.2 `start_server.bat` (what actually runs)
- `chcp 65001` (UTF‑8), creates `logs\`.
- Loads every `KEY=value` from `server_settings.env` into the environment (`for /f ... delims==`), defaults `SERVER_PORT=5050`, `SERVER_BIND=0.0.0.0`.
- **Infinite restart loop**: rolls `logs\server.log` over to `server.old.log` past ~10 MB, then runs
  `waitress-serve --listen=%SERVER_BIND%:%SERVER_PORT% --trusted-proxy=127.0.0.1 --trusted-proxy-count=1 "--trusted-proxy-headers=x-forwarded-for x-forwarded-host x-forwarded-proto" superadmin_panel.app:app >> logs\server.log 2>&1`
  — the trusted‑proxy flags give the app the **visitor's real IP** behind IIS/Caddy (otherwise everyone is 127.0.0.1 and the per‑IP rate limits block all users at once).
- If waitress exits, logs the exit code and restarts after 10 s (uses `ping -n 11 127.0.0.1` instead of `timeout`, because `timeout` fails without a console under Task Scheduler).

### 13.3 `stop_server.bat` → `-Action Stop` (`Stop-PanelServer`)
1. `Stop-ScheduledTask "ThinkStak SuperAdmin Server"`.
2. Stopping the task doesn't reliably kill its children, so it finds via `Win32_Process` and force‑kills:
   - the `cmd.exe` running **`start_server.bat`** (killed first, otherwise the loop relaunches the server), and
   - any process whose command line contains **`superadmin_panel.app:app`** (waitress).
3. Waits 2 s. Message: "Stopped. It will start again at the next reboot (or run restart_server.bat)."

`restart_server.bat` = `Stop-PanelServer` + `Start-PanelServer` (same 45 s health check as setup).

### 13.4 `setup_https.bat` → `-Action Https` (optional, Caddy)
Asks the domain → checks its DNS A record points to this server's public IP (prints exact Hostinger DNS instructions if not) → checks ports 80/443 are free → downloads `caddy.exe` → writes `caddy\Caddyfile` (`<domain> { reverse_proxy 127.0.0.1:<port> }`) → opens 80/443 → offers to close direct access (`SERVER_BIND=127.0.0.1`, deletes the port rule) → sets `PUBLIC_SERVER_URL=https://<domain>` (env file + DB) → registers boot task `"ThinkStak HTTPS Proxy"` (`start_caddy.bat`) → restarts panel + Caddy → waits up to 3 min for `https://<domain>/login`. Afterwards **rebuild the trial EXE and every Admin's EXE** (each EXE keeps the URL it was built with).

### 13.5 `make_server_package.bat` (updating the server)
`robocopy` the project into `server_package\ThinkStakServer\` + zip, **excluding** everything that belongs to the server or to your PC: `.venv`, `.git`, caches, `build`, `dist`, `logs`, `caddy`, `superadmin_panel\keys`, `built_exes`, `log_uploads`, `superadmin.db`, `superadmin_config.json`, `flask_secret_key.txt`, `server_settings.env`, and local client data (`meta_accounts.csv`, `order_logs*.json`, `ticket_mappings.csv`, `session.bin`, `license_cache.json`). Paste it over the server folder → run `restart_server.bat`. The server keeps its own DB, login, keys and email settings.

### 13.6 What to rename when copying to the other copy trader
In `setup_server.ps1`: `$TaskName`, `$FirewallRuleName`, `$CaddyTaskName`, `$CaddyFirewallRuleName`, `$DefaultPort` (use a **different port** if both servers run on the same VPS, and add 5050 to `$BlockedPorts`), the `.spec` file name in the required‑files check, banner text, and the default HTTPS domain. In `start_server.bat`: nothing except comments (it's relative to its own folder). Install the second product in its **own folder** (e.g. `C:\OtherCopyTraderServer`) so `.venv`, `server_settings.env`, DB and keys stay separate. If using IIS, add a second site/binding whose `web.config` forwards to the new port.

---

## 14. Dependencies

- Server: `Flask`, `argon2-cffi`, `cryptography`, `waitress` (production), `pyinstaller` (for Build EXE, same venv).
- App: `Flask`, `requests`, `cryptography`, `pywebview`, `pywin32` (DPAPI + MachineGuid), plus the trading SDKs.

---

## 15. Porting checklist for the other copy trader

**Copy as‑is (no trading code inside):**
- [ ] Whole `superadmin_panel/` folder (rename product strings in `email_notifications.py`, templates, `exe_builder.TRIAL_EXE_STEM`)
- [ ] `auth_client.py`, `auth_state.py`, `session_store.py`, `device_fingerprint.py`
- [ ] `license_state.py`, `license_cache.py`, `license_signing_client.py`, `license_public_key.pem`
- [ ] `server_config.py`, `runtime_paths.py` (change `APP_DIR_NAME` so the two products don't share `%LOCALAPPDATA%` folders)
- [ ] `client_limit.py`, `telemetry_client.py`, `telemetry_sync.py`, `log_request_client.py`, `log_request_sync.py`
- [ ] `templates/login.html`, `change_password.html`, `forgot_password.html` + `static/login.js`, `change_password.js`, `forgot_password.js`, `password_toggle.js`
- [ ] `desktop_launcher.py`, `.spec` (rename), `build.bat`, `packaging/` (new icon + version info), `version.py`
- [ ] Server scripts: `setup_server.bat`, `setup_server.ps1`, `start_server.bat`, `stop_server.bat`, `restart_server.bat`, `make_server_package.bat` (+ `setup_https.bat`, `start_caddy.bat` only if not using IIS) — rename task/firewall names and port per §13.6

**Wire into the new app's `app.py`:**
- [ ] `PUBLIC_ENDPOINTS` + `@before_request require_login`
- [ ] `/login`, `/logout`, `/change-password`, `/forgot-password/*` routes
- [ ] `restore_session_on_startup()` and `_apply_admin_scope()` (per‑Admin data folder)
- [ ] `_erase_if_admin_deleted()` called after every license refresh
- [ ] `license_state.start_periodic_checks(stop_copier_callback)`
- [ ] `license_state.refresh_license_state()` check inside the **Start** route
- [ ] `client_limit.reserve()` before Add / Import; `client_limit.note_removed()` after Delete; `/api/client-limit` for the UI counter + pop‑up
- [ ] `telemetry_sync.start_periodic_sync(snapshot_fn)` with the new app's status/client counts
- [ ] `log_request_sync.start_periodic_check()` + `app_log` with dated lines and rotation

**Decide for the new product:**
- [ ] Same SuperAdmin server for both products, or a separate one? (Separate is simpler: new DB, new keys, new port/domain, own trial EXE.) If shared, add a `product` column to `admins`/trial tables — otherwise one product's trial blocks the other's.
- [ ] Product name/branding in emails and EXE names
- [ ] Trial length / limits (env vars above)

**Server setup**: copy the whole project to e.g. `C:\OtherCopyTraderServer` → double‑click `setup_server.bat` (Python, venv, deps, login, settings, firewall, boot task — §13) → IIS + win‑acme for HTTPS forwarding to the new port → check Settings → Server Address in the panel → open `/trial` once so the trial EXE builds. Updates: `make_server_package.bat` → paste → `restart_server.bat`.

**Tests to port**: `tests/superadmin/*` (auth, trial signup, password reset, exe builder, client limit, email, expiry reminder, log requests) and `tests/test_auth_client.py`, `test_device_fingerprint.py`, `test_license_phase6_7.py`, `test_session_store.py`, `test_admin_data_isolation.py`, `test_client_limit_app.py`.
