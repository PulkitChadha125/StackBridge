// StakBridge UI: plain JavaScript, no build step. Talks only to the /v1 REST API.
// The browser signs in with a password and gets a session cookie. API tokens are for programs.

const REFRESH_MS = 3000;
// Required on every browser request that changes something. Other websites cannot add it.
const UI_HEADERS = { "X-StakBridge": "1" };
let signedIn = false;

const state = {
  tab: "accounts",
  accounts: [],
  strategies: [],
  trades: [],
  killSwitch: false,
  selectedStrategy: null,
  openTrade: null,          // requestId whose per-account results are expanded
  openTradeDetail: null,
  openPositions: null,      // account id whose positions are expanded
  positionsData: null,      // {positions, orders} or {error}
  editingAccount: null,     // account id being edited in the form, null when adding
};

const $ = (sel) => document.querySelector(sel);
const view = $("#view");

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTime(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleString();
}

class Unauthorized extends Error {}

async function api(method, path, body) {
  const res = await fetch(`/v1${path}`, {
    method,
    credentials: "same-origin",
    headers: { ...UI_HEADERS, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { showSignIn("Your session ended. Please sign in again."); throw new Unauthorized(); }
  if (!res.ok) {
    const err = data.error || {};
    const details = (err.details || []).map((d) => `${d.field}: ${d.problem}`).join("; ");
    throw new Error(`${err.message || res.statusText}${details ? ` (${details})` : ""}`);
  }
  return data;
}

function flash(text, ok = false) {
  const el = $("#message");
  el.textContent = text;
  el.className = ok ? "message ok" : "message";
  el.hidden = false;
  clearTimeout(flash.timer);
  flash.timer = setTimeout(() => { el.hidden = true; }, ok ? 4000 : 8000);
}

async function act(fn, okText) {
  try {
    await fn();
    if (okText) flash(okText, true);
  } catch (e) {
    if (!(e instanceof Unauthorized)) flash(e.message);
  }
  await refresh(true);
}

// ---------- data ----------

let refreshing = false;

async function refresh(force = false) {
  if (!signedIn || (refreshing && !force)) return;
  refreshing = true;
  try {
    const [accounts, strategies, trades, health] = await Promise.all([
      api("GET", "/accounts"), api("GET", "/strategies"), api("GET", "/trades?limit=200"), api("GET", "/health"),
    ]);
    state.accounts = accounts.accounts;
    state.strategies = strategies.strategies;
    state.trades = trades.trades;
    state.killSwitch = health.killSwitch;
    if (state.openTrade) {
      state.openTradeDetail = await api("GET", `/trades/${encodeURIComponent(state.openTrade)}`).catch(() => null);
    }
    if (state.openPositions) await loadPositions(state.openPositions);
  } catch (e) {
    if (!(e instanceof Unauthorized)) flash(`Cannot reach the gateway: ${e.message}`);
    return;
  } finally {
    refreshing = false;
  }
  // Do not redraw while the user is typing in a text or number box inside the view.
  const active = document.activeElement;
  const typing = active && view.contains(active) && active.tagName === "INPUT" && active.type !== "checkbox";
  if (!force && typing) return;
  render(!force);
}

async function loadPositions(accountId) {
  try {
    state.positionsData = await api("GET", `/accounts/${encodeURIComponent(accountId)}/positions`);
  } catch (e) {
    if (e instanceof Unauthorized) throw e;
    state.positionsData = { error: e.message };
  }
}

// ---------- rendering ----------

function render(keepTyped = false) {
  // Keep what the user typed (but did not submit) across the automatic refresh.
  const typed = {};
  if (keepTyped) view.querySelectorAll("input[name]").forEach((i) => { if (i.type !== "checkbox" && i.value) typed[i.name] = i.value; });

  $("#kill-banner").hidden = !state.killSwitch;
  const killBtn = $("#kill-btn");
  killBtn.textContent = state.killSwitch ? "Resume trading" : "Stop all";
  killBtn.className = state.killSwitch ? "primary" : "danger";
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab));
  $("#add-account").hidden = state.tab !== "accounts";
  $("#send-trade").hidden = state.tab !== "trades";

  if (state.tab === "accounts") view.innerHTML = renderAccounts();
  if (state.tab === "strategies") view.innerHTML = renderStrategies();
  if (state.tab === "trades") view.innerHTML = renderTrades();

  Object.entries(typed).forEach(([name, value]) => {
    const input = view.querySelector(`input[name="${name}"]`);
    if (input && !input.value) input.value = value;
  });
}

const STATUS_LABELS = {
  connected: "Connected", connecting: "Connecting", starting: "Starting", disconnected: "Disconnected",
  error: "Error", disabled: "Disabled", stopped: "Stopped",
};

function statusCell(a) {
  const reason = a.statusReason ? `<div class="sub">${esc(a.statusReason)}</div>` : "";
  return `<span class="dot ${esc(a.status)}"></span>${esc(STATUS_LABELS[a.status] || a.status)}${reason}`;
}

function renderPositions() {
  const data = state.positionsData;
  if (!data) return `<p class="sub">Loading...</p>`;
  if (data.error) return `<p class="sub">${esc(data.error)}</p>`;
  if (!data.positions.length && !data.orders.length) return `<p class="sub">No open positions or pending orders.</p>`;
  const magicTag = (magic) => state.strategies.find((s) => s.magic === magic)?.tag ?? `magic ${magic}`;
  const positions = data.positions.map((p) => `<tr>
      <td>${esc(p.ticket)}</td><td>position</td><td>${esc(p.symbol)}</td><td>${esc(p.side)}</td><td>${esc(p.volume)}</td>
      <td>${esc(p.price)}</td><td>${esc(p.sl || "")}</td><td>${esc(p.tp || "")}</td><td>${esc(magicTag(p.magic))}</td><td>${esc(p.comment)}</td></tr>`);
  const orders = data.orders.map((o) => `<tr>
      <td>${esc(o.ticket)}</td><td>pending</td><td>${esc(o.symbol)}</td><td>${esc(o.side ?? "")}</td><td>${esc(o.volume)}</td>
      <td>${esc(o.price)}</td><td></td><td></td><td>${esc(magicTag(o.magic))}</td><td>${esc(o.comment)}</td></tr>`);
  return `<table>
    <thead><tr><th>Ticket</th><th>Kind</th><th>Symbol</th><th>Side</th><th>Volume</th><th>Price</th><th>SL</th><th>TP</th><th>Strategy</th><th>Comment</th></tr></thead>
    <tbody>${positions.join("")}${orders.join("")}</tbody></table>`;
}

function renderAccounts() {
  if (!state.accounts.length) return `<div class="empty">No accounts yet. Add one below.</div>`;
  const rows = state.accounts.map((a) => {
    const id = esc(a.id);
    const balance = a.info && typeof a.info.balance === "number" ? `${a.info.balance.toFixed(2)} ${esc(a.info.currency)}` : `<span class="sub">-</span>`;
    const row = `<tr>
      <td>${esc(a.name)}<div class="sub">${id}${a.symbolSuffix ? ` · suffix ${esc(a.symbolSuffix)}` : ""}</div></td>
      <td>${esc(a.login)}</td>
      <td>${esc(a.server)}</td>
      <td>${statusCell(a)}</td>
      <td>${balance}</td>
      <td>${a.strategies.length ? a.strategies.map(esc).join("<br>") : "<span class='sub'>none</span>"}</td>
      <td><button class="toggle ${a.tradingOn ? "on" : ""}" data-action="trading" data-id="${id}" data-on="${a.tradingOn}" title="Switch trading on or off for this account">${a.tradingOn ? "ON" : "OFF"}</button></td>
      <td class="row-actions">
        <button class="small" data-action="positions" data-id="${id}">${state.openPositions === a.id ? "Hide" : "Positions"}</button>
        <button class="small" data-action="edit" data-id="${id}">Edit</button>
        <button class="small" data-action="enable" data-id="${id}" data-on="${a.enabled}">${a.enabled ? "Disable" : "Enable"}</button>
        <button class="small" data-action="remove" data-id="${id}">Remove</button>
      </td>
    </tr>`;
    if (state.openPositions !== a.id) return row;
    return `${row}<tr class="detail"><td colspan="8">${renderPositions()}</td></tr>`;
  }).join("");
  return `<table>
    <thead><tr><th>Name</th><th>Login</th><th>Server</th><th>Status</th><th>Balance</th><th>Strategies</th><th>Trading</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function renderStrategies() {
  if (!state.selectedStrategy || !state.strategies.some((s) => s.tag === state.selectedStrategy)) {
    state.selectedStrategy = state.strategies[0]?.tag ?? null;
  }
  const list = state.strategies.map((s) =>
    `<button data-action="pick" data-tag="${esc(s.tag)}" class="${s.tag === state.selectedStrategy ? "selected" : ""}">${esc(s.tag)}${s.active ? "" : " <span class='sub'>(paused)</span>"}</button>`
  ).join("");
  const newForm = `<form id="strategy-form" autocomplete="off">
      <input name="tag" placeholder="new_tag" pattern="[A-Za-z0-9_\\-]{1,40}" title="Letters, digits, _ and - only" required>
      <input name="maxVolume" type="number" step="0.01" min="0.01" placeholder="max lots" required>
      <button type="submit" class="small primary">+ New strategy</button>
    </form>`;

  const s = state.strategies.find((x) => x.tag === state.selectedStrategy);
  let detail = `<div class="card empty">Create a strategy tag on the left.</div>`;
  if (s) {
    const checks = state.accounts.length ? state.accounts.map((a) => `
      <label><input type="checkbox" data-action="assign" data-id="${esc(a.id)}" ${s.accountIds.includes(a.id) ? "checked" : ""}>
        ${esc(a.name)} <span class="sub">${esc(a.id)}</span>
        <span class="sub"><span class="dot ${esc(a.status)}"></span>${esc(STATUS_LABELS[a.status] || a.status)}${a.tradingOn ? "" : ", trading off"}</span></label>`).join("")
      : `<p class="sub">No accounts yet. Add them in the Accounts tab.</p>`;
    const last = s.lastRequest
      ? `${esc(s.lastRequest.requestId)}: ${esc(s.lastRequest.succeeded)} of ${esc(s.lastRequest.accounts)} succeeded <span class="sub">${esc(fmtTime(s.lastRequest.receivedAt))}</span>`
      : `<span class="sub">none yet</span>`;
    detail = `<div class="card">
      <h2>${esc(s.tag)} <button class="toggle ${s.active ? "on" : ""}" data-action="active" data-tag="${esc(s.tag)}" data-on="${s.active}">${s.active ? "Active" : "Paused"}</button></h2>
      <div class="meta">
        <span>Magic number: <b>${esc(s.magic)}</b></span>
        <span>Max volume per trade:
          <input name="maxVolumeEdit" id="max-volume" type="number" step="0.01" min="0.01" value="${esc(s.maxVolume)}"> lot
          <button class="small" data-action="max-volume" data-tag="${esc(s.tag)}">Save</button></span>
      </div>
      <h3>Accounts that receive this strategy</h3>
      <div class="checks">${checks}</div>
      <h3>Last request</h3>
      <div>${last}</div>
    </div>`;
  }
  return `<div class="split"><div class="list">${list}${newForm}</div>${detail}</div>`;
}

function renderTrades() {
  if (!state.trades.length) return `<div class="empty">No trade requests yet. Send one to POST /v1/trade.</div>`;
  const rows = state.trades.map((t) => {
    const open = state.openTrade === t.requestId;
    const main = `<tr class="clickable${open ? " open" : ""}" data-action="trade" data-id="${esc(t.requestId)}">
      <td>${esc(fmtTime(t.receivedAt))}</td>
      <td>${esc(t.requestId)}</td>
      <td>${esc(t.strategy)}</td>
      <td>${esc(t.action)}</td>
      <td>${esc(t.symbol ?? "all")}</td>
      <td>${esc(t.side ?? "")}</td>
      <td>${esc(t.volume ?? "")}</td>
      <td>${esc(t.succeeded)} / ${esc(t.accounts)}</td>
    </tr>`;
    if (!open || !state.openTradeDetail) return main;
    const results = state.openTradeDetail.results.map((r) => `<tr>
        <td>${esc(r.accountId)}</td><td class="status-${esc(r.status)}">${esc(r.status)}</td>
        <td>${esc(r.reason ?? "")}</td><td>${esc(r.ticket ?? "")}</td><td>${esc(r.price ?? "")}</td><td>${esc(r.volume ?? "")}</td>
        <td>${r.detail ? `<code>${esc(JSON.stringify(r.detail))}</code>` : ""}</td></tr>`).join("");
    return `${main}<tr class="detail"><td colspan="8"><table>
      <thead><tr><th>Account</th><th>Status</th><th>Reason</th><th>Ticket</th><th>Price</th><th>Volume</th><th>Detail</th></tr></thead>
      <tbody>${results}</tbody></table></td></tr>`;
  }).join("");
  return `<table>
    <thead><tr><th>Time</th><th>Request id</th><th>Strategy</th><th>Action</th><th>Symbol</th><th>Side</th><th>Volume</th><th>Succeeded</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

// ---------- account form (add and edit) ----------

const accountForm = $("#account-form");

function startEdit(accountId) {
  const a = state.accounts.find((x) => x.id === accountId);
  if (!a) return;
  state.editingAccount = a.id;
  const f = accountForm.elements;
  f.name.value = a.name;
  f.id.value = a.id;
  f.id.disabled = true;
  f.login.value = a.login;
  f.password.value = "";
  f.password.required = false;
  f.password.placeholder = "leave empty to keep the current password";
  f.server.value = a.server;
  f.symbolSuffix.value = a.symbolSuffix;
  f.terminalPath.value = a.terminalPath;
  $("#account-form-title").textContent = `Edit account ${a.id}`;
  $("#account-submit").textContent = "Save changes";
  $("#account-cancel").hidden = false;
  $("#account-hint").textContent = "Changing login, password, server or terminal path reconnects the account.";
  $("#add-account").open = true;
  $("#add-account").scrollIntoView({ behavior: "smooth", block: "start" });
}

function resetAccountForm() {
  state.editingAccount = null;
  accountForm.reset();
  const f = accountForm.elements;
  f.id.disabled = false;
  f.password.required = true;
  f.password.placeholder = "";
  $("#account-form-title").textContent = "+ Add account";
  $("#account-submit").textContent = "Add account";
  $("#account-cancel").hidden = true;
  $("#account-hint").textContent = "Trading starts OFF. Switch it on once the account shows Connected.";
}

accountForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const f = accountForm.elements;
  const login = parseInt(f.login.value, 10);
  if (!Number.isInteger(login) || login < 1) { flash("MT5 login must be a whole number."); return; }
  const body = {
    name: f.name.value.trim(),
    login,
    server: f.server.value.trim(),
    symbolSuffix: f.symbolSuffix.value.trim(),
    terminalPath: f.terminalPath.value.trim(),
  };
  if (f.password.value) body.password = f.password.value;

  const editing = state.editingAccount;
  if (editing) {
    act(async () => { await api("PATCH", `/accounts/${encodeURIComponent(editing)}`, body); resetAccountForm(); $("#add-account").open = false; },
      `Account ${editing} saved`);
  } else {
    const id = f.id.value.trim();
    if (id) body.id = id;
    act(async () => { await api("POST", "/accounts", body); resetAccountForm(); $("#add-account").open = false; },
      "Account added, connecting...");
  }
});

$("#account-cancel").addEventListener("click", () => { resetAccountForm(); $("#add-account").open = false; });

// ---------- view events ----------

view.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action]");
  if (!el || el.dataset.action === "assign") return;
  const { action, id, tag } = el.dataset;
  const on = el.dataset.on === "true";
  const path = (p) => encodeURIComponent(p);

  if (action === "trading") act(() => api("PATCH", `/accounts/${path(id)}`, { tradingOn: !on }), `Trading ${on ? "OFF" : "ON"} for ${id}`);
  if (action === "enable") act(() => api("PATCH", `/accounts/${path(id)}`, { enabled: !on }), `${id} ${on ? "disabled" : "enabled"}`);
  if (action === "edit") startEdit(id);
  if (action === "remove" && window.confirm(`Remove account ${id}? Its open positions stay at the broker.`)) {
    if (state.editingAccount === id) resetAccountForm();
    act(() => api("DELETE", `/accounts/${path(id)}`), `${id} removed`);
  }
  if (action === "positions") {
    if (state.openPositions === id) { state.openPositions = null; state.positionsData = null; render(); return; }
    state.openPositions = id;
    state.positionsData = null;
    render();
    refresh(true);  // loads the positions of the open account
  }
  if (action === "pick") { state.selectedStrategy = tag; render(); }
  if (action === "active") act(() => api("PATCH", `/strategies/${path(tag)}`, { active: !on }), `${tag} is now ${on ? "paused" : "active"}`);
  if (action === "max-volume") {
    const value = parseFloat($("#max-volume").value);
    if (!(value > 0)) { flash("Max volume must be a number above 0."); return; }
    act(() => api("PATCH", `/strategies/${path(tag)}`, { maxVolume: value }), `Max volume for ${tag} set to ${value} lot`);
  }
  if (action === "trade") {
    if (state.openTrade === id) { state.openTrade = null; state.openTradeDetail = null; render(); return; }
    state.openTrade = id;
    state.openTradeDetail = null;
    refresh(true);  // loads the per-account results of the open request
  }
});

view.addEventListener("change", (e) => {
  if (e.target.dataset.action !== "assign") return;
  const ids = [...view.querySelectorAll("[data-action=assign]:checked")].map((c) => c.dataset.id);
  const tag = state.selectedStrategy;
  act(() => api("PUT", `/strategies/${encodeURIComponent(tag)}/accounts`, { accountIds: ids }),
    `Accounts for ${tag} saved. The next request uses them.`);
});

view.addEventListener("submit", (e) => {
  if (e.target.id !== "strategy-form") return;
  e.preventDefault();
  const form = new FormData(e.target);
  const tag = form.get("tag").trim();
  const maxVolume = parseFloat(form.get("maxVolume"));
  if (!(maxVolume > 0)) { flash("Max volume must be a number above 0."); return; }
  act(async () => {
    await api("POST", "/strategies", { tag, maxVolume });
    state.selectedStrategy = tag;
    e.target.reset();
  }, `Strategy ${tag} created`);
});

// ---------- send a trade request (testing aid) ----------

function tradePreset(kind) {
  const strategy = state.selectedStrategy || state.strategies[0]?.tag || "gold_scalper";
  const requestId = `test-${new Date().toISOString().replace(/\D/g, "").slice(0, 14)}`;
  const presets = {
    open: { requestId, strategy, action: "open", symbol: "XAUUSD", side: "buy", type: "market", volume: 0.01 },
    limit: { requestId, strategy, action: "open", symbol: "XAUUSD", side: "buy", type: "limit", volume: 0.01, price: 1000.0 },
    close: { requestId, strategy, action: "close" },
  };
  $("#trade-json").value = JSON.stringify(presets[kind], null, 2);
}

document.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => tradePreset(b.dataset.preset)));
$("#send-trade").addEventListener("toggle", () => { if ($("#send-trade").open && !$("#trade-json").value) tradePreset("open"); });

$("#trade-send").addEventListener("click", async () => {
  const out = $("#trade-response");
  const status = $("#trade-status");
  let body;
  try {
    body = JSON.parse($("#trade-json").value);
  } catch (e) {
    status.textContent = `Not valid JSON: ${e.message}`;
    return;
  }
  const button = $("#trade-send");
  button.disabled = true;
  status.textContent = "Sending...";
  try {
    const res = await fetch("/v1/trade", {
      method: "POST",
      credentials: "same-origin",
      headers: { ...UI_HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.status === 401) { showSignIn("Your session ended. Please sign in again."); return; }
    const text = await res.text();
    let pretty = text;
    try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch { /* not JSON */ }
    status.textContent = `HTTP ${res.status}`;
    out.textContent = pretty;
    out.hidden = false;
    if (res.ok) { state.openTrade = body.requestId; state.openTradeDetail = null; }
  } catch (e) {
    status.textContent = `Request failed: ${e.message}`;
  } finally {
    button.disabled = false;
  }
  await refresh(true);
});

// ---------- header ----------

$("#tabs").addEventListener("click", (e) => {
  const tab = e.target.closest("button")?.dataset.tab;
  if (!tab) return;
  state.tab = tab;
  render();
});

$("#kill-btn").addEventListener("click", () => {
  if (state.killSwitch) {
    act(() => api("DELETE", "/kill"), "Trading resumed");
  } else {
    act(() => api("POST", "/kill"), "All new trades are blocked. Closing positions still works.");
  }
});

$("#message").addEventListener("click", () => { $("#message").hidden = true; });

// ---------- sign in, first-time password, sign out ----------

async function postJson(path, body) {
  const res = await fetch(`/v1${path}`, {
    method: "POST",
    credentials: "same-origin",
    headers: { ...UI_HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  const err = data.error || {};
  const details = (err.details || []).map((d) => d.problem).join("; ");
  return { ok: res.ok, code: err.code, message: details || err.message || res.statusText };
}

// Shows one of the sign-in screens (or none) and the app header controls only when signed in.
function showScreen(screen) {
  ["login", "setup", "setup-remote"].forEach((id) => { $(`#${id}`).hidden = id !== screen; });
  const inApp = screen === null;
  signedIn = inApp;
  $("#app").hidden = !inApp;
  $("#tabs").hidden = !inApp;
  $("#kill-btn").hidden = !inApp;
  $("#logout-btn").hidden = !inApp;
  if (!inApp) $("#kill-banner").hidden = true;
}

function showSignIn(message = "") {
  showScreen("login");
  $("#login-error").textContent = message;
  $("#login-password").focus();
}

function showApp() {
  showScreen(null);
  refresh(true);
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#login-password");
  const button = e.target.querySelector("button");
  button.disabled = true;
  const result = await postJson("/session", { password: input.value }).catch(() => ({ ok: false, message: "Cannot reach StakBridge." }));
  button.disabled = false;
  input.value = "";
  if (result.ok) { $("#login-error").textContent = ""; showApp(); return; }
  $("#login-error").textContent = result.code === "WRONG_PASSWORD" ? "Wrong password, try again." : result.message;
  input.focus();
});

$("#setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const password = $("#setup-password").value;
  if (password !== $("#setup-confirm").value) { $("#setup-error").textContent = "The two passwords are different."; return; }
  if (password.length < 8) { $("#setup-error").textContent = "Use at least 8 characters."; return; }
  const result = await postJson("/session/setup", { password }).catch(() => ({ ok: false, message: "Cannot reach StakBridge." }));
  if (result.ok) { e.target.reset(); $("#setup-error").textContent = ""; showApp(); return; }
  if (result.code === "PASSWORD_ALREADY_SET") { showSignIn("A password already exists. Sign in with it."); return; }
  $("#setup-error").textContent = result.message;
});

$("#logout-btn").addEventListener("click", async () => {
  await fetch("/v1/session", { method: "DELETE", credentials: "same-origin", headers: UI_HEADERS }).catch(() => {});
  showSignIn("You are signed out.");
});

async function start() {
  try {
    const session = await (await fetch("/v1/session", { credentials: "same-origin" })).json();
    if (session.loggedIn) showApp();
    else if (session.passwordSet) showSignIn();
    else showScreen(session.setupAllowedHere ? "setup" : "setup-remote");
    if (!session.passwordSet && session.setupAllowedHere) $("#setup-password").focus();
  } catch {
    showSignIn("Cannot reach StakBridge. Is its window still open?");
  }
}

start();
setInterval(() => refresh(), REFRESH_MS);
