// StakBridge UI: plain JavaScript, no build step. Talks only to the /v1 REST API.
// The browser signs in with a password and gets a session cookie. API tokens are for programs.

const REFRESH_MS = 3000;
const COPY_MAGIC = 990000;  // magic number the copier puts on every copied position
const THEME_KEY = "stakbridge-theme";
const LOG_SEEN_KEY = "stakbridge-log-seen";  // newest error id the user has already seen in the Log tab
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
  copier: null,             // saved copier settings, status and recent copies
  copierDraft: null,        // unsaved edits in the Copier tab: {masterId, clients: {id: {on, mult, symbols: [{from, to}]}}}
  copierSymbolsOpen: null,  // client account id whose symbol mapping is expanded
  logs: [],
  logLevel: "info",         // info = everything, warning = warnings and errors, error = errors only
  newestErrorId: 0,
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
    const [accounts, strategies, trades, health, copier, errors] = await Promise.all([
      api("GET", "/accounts"), api("GET", "/strategies"), api("GET", "/trades?limit=200"), api("GET", "/health"),
      api("GET", "/copier"), api("GET", "/logs?level=error&limit=1"),
    ]);
    state.newestErrorId = errors.logs[0]?.id ?? 0;
    if (state.tab === "log") state.logs = (await api("GET", `/logs?level=${state.logLevel}&limit=500`)).logs;
    state.copier = copier;
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
  // Do not redraw while the user is typing in a text or number box, or choosing in a drop-down, inside the view.
  const active = document.activeElement;
  const typing = active && view.contains(active)
    && ((active.tagName === "INPUT" && active.type !== "checkbox") || active.tagName === "SELECT");
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
  if (state.tab === "copier") view.innerHTML = renderCopier();
  if (state.tab === "log") { view.innerHTML = renderLog(); markErrorsSeen(); }
  updateLogBadge();

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
  const magicTag = (magic) => magic === COPY_MAGIC ? "copier" : state.strategies.find((s) => s.magic === magic)?.tag ?? `magic ${magic}`;
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
      <td>${esc(a.name)}${copierBadge(a.id)}<div class="sub">${id}${a.symbolSuffix ? ` · suffix ${esc(a.symbolSuffix)}` : ""}</div></td>
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

// ---------- copier ----------

function copierBadge(accountId) {
  const c = state.copier;
  if (!c) return "";
  if (c.masterId === accountId) return `<span class="badge" title="Copier master">master</span>`;
  const client = c.clients.find((x) => x.accountId === accountId);
  return client ? `<span class="badge" title="Copier client">client ×${esc(client.multiplier)}</span>` : "";
}

const NEW_CLIENT = { on: false, mult: "1", symbols: [] };

function savedCopierForm() {
  const c = state.copier || { masterId: null, clients: [] };
  return {
    masterId: c.masterId,
    clients: Object.fromEntries(c.clients.map((x) => [x.accountId, {
      on: true, mult: String(x.multiplier),
      symbols: Object.entries(x.symbols || {}).map(([from, to]) => ({ from, to })),
    }])),
  };
}

function draftClient(draft, id) {
  draft.clients[id] = { ...NEW_CLIENT, ...draft.clients[id] };
  return draft.clients[id];
}

function renderSymbolMap(account, client, masterSuffix) {
  const id = esc(account.id);
  const example = `master <code>XAUUSD${esc(masterSuffix)}</code> becomes <code>XAUUSD${esc(account.symbolSuffix)}</code> here`;
  const rows = client.symbols.map((m, i) => `<tr>
      <td><input data-copier="sym-from" data-id="${id}" data-index="${i}" value="${esc(m.from)}" placeholder="e.g. GOLD" maxlength="32" aria-label="Master symbol"></td>
      <td class="arrow">→</td>
      <td><input data-copier="sym-to" data-id="${id}" data-index="${i}" value="${esc(m.to)}" placeholder="e.g. XAUUSD${esc(account.symbolSuffix)}" maxlength="32" aria-label="Symbol on ${esc(account.name)}"></td>
      <td><button class="small" data-action="copier-sym-remove" data-id="${id}" data-index="${i}">Remove</button></td>
    </tr>`).join("");
  return `<tr class="detail"><td colspan="6"><div class="symbol-map">
      <p class="hint">Only needed when ${esc(account.name)}'s broker names a symbol differently from the master's broker.
        Symbols without a row here use the suffix: ${example}.</p>
      ${rows ? `<table><thead><tr><th>Master symbol</th><th></th><th>Symbol on ${esc(account.name)}</th><th></th></tr></thead><tbody>${rows}</tbody></table>` : ""}
      <button class="small" data-action="copier-sym-add" data-id="${id}">+ Add symbol</button>
    </div></td></tr>`;
}

// What the copier form shows: the unsaved draft if there is one, otherwise the saved settings.
function copierForm() {
  return state.copierDraft || savedCopierForm();
}

function editCopier(change) {
  if (!state.copierDraft) state.copierDraft = structuredClone(savedCopierForm());
  change(state.copierDraft);
}

function median(values) {
  const s = [...values].sort((a, b) => a - b);
  if (!s.length) return null;
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

const fmtMs = (ms) => (ms == null ? "–" : ms < 10 ? ms.toFixed(2) : ms.toFixed(0));

function latencyStat(label, values, brokerValues) {
  if (!values.length) return `<div class="stat"><div class="label">${label}</div><div class="value">–</div><div class="sub">no copies yet</div></div>`;
  return `<div class="stat"><div class="label">${label}</div>
    <div class="value">${fmtMs(median(values))} <small>ms median</small></div>
    <div class="sub">max ${fmtMs(Math.max(...values))} ms · broker ${fmtMs(median(brokerValues))} ms</div></div>`;
}

function renderCopier() {
  const c = state.copier;
  if (!c) return `<div class="empty">Loading...</div>`;
  if (state.accounts.length < 2) return `<div class="empty">Add at least two accounts in the Accounts tab: one master and one or more clients.</div>`;
  const form = copierForm();
  const dirty = state.copierDraft !== null;

  let watch = `<span class="dot"></span>No master chosen yet`;
  if (c.masterId && c.watching) watch = `<span class="dot connected"></span>Watching <b>${esc(c.masterId)}</b> · ${esc(c.masterPositions)} open position${c.masterPositions === 1 ? "" : "s"}`;
  else if (c.masterId) watch = `<span class="dot connecting"></span>Waiting for <b>${esc(c.masterId)}</b> to connect`;

  const options = [`<option value="">Choose the master account</option>`, ...state.accounts.map((a) =>
    `<option value="${esc(a.id)}" ${a.id === form.masterId ? "selected" : ""}>${esc(a.name)} (${esc(a.id)})</option>`)].join("");
  const masterSuffix = state.accounts.find((a) => a.id === form.masterId)?.symbolSuffix ?? "";
  const clients = state.accounts.filter((a) => a.id !== form.masterId).map((a) => {
    const client = { ...NEW_CLIENT, ...form.clients[a.id] };
    const mapped = client.symbols.filter((m) => m.from.trim() && m.to.trim()).length;
    const open = state.copierSymbolsOpen === a.id;
    const row = `<tr class="${client.on ? "" : "off"}">
      <td><input type="checkbox" data-copier="client" data-id="${esc(a.id)}" ${client.on ? "checked" : ""} aria-label="Copy to ${esc(a.name)}"></td>
      <td>${esc(a.name)}<div class="sub">${esc(a.id)}${a.symbolSuffix ? ` · suffix ${esc(a.symbolSuffix)}` : ""}</div></td>
      <td>${statusCell(a)}</td>
      <td>${a.tradingOn ? "ON" : `<span class="status-rejected">OFF</span><div class="sub">switch it on in Accounts</div>`}</td>
      <td class="num"><input type="number" min="0.01" max="100" step="0.01" data-copier="mult" data-id="${esc(a.id)}" value="${esc(client.mult)}" aria-label="Lot multiplier for ${esc(a.name)}"> ×</td>
      <td><button class="small" data-action="copier-symbols" data-id="${esc(a.id)}">${open ? "Hide" : mapped ? `${mapped} mapped` : "Map symbols"}</button></td>
    </tr>`;
    return open ? row + renderSymbolMap(a, client, masterSuffix) : row;
  }).join("");
  const idleHint = c.active
    ? "Client lots = master lots × multiplier, rounded down to each broker's lot step."
    : "Copier is OFF: new master trades are not copied. Closes and SL/TP changes still are.";

  const settings = `<div class="card">
    <h2>Trade copier <button class="toggle ${c.active ? "on" : ""}" data-action="copier-active" data-on="${c.active}" ${c.masterId ? "" : "disabled"} title="Switch copying of new trades on or off">${c.active ? "ON" : "OFF"}</button></h2>
    <div class="meta"><span>${watch}</span></div>
    <h3>Master account</h3>
    <select data-copier="master" aria-label="Master account">${options}</select>
    <p class="hint">Every position opened, changed or closed on the master is mirrored on the ticked clients. Trades you place yourself on a client are never touched.</p>
    <h3>Client accounts</h3>
    <table><thead><tr><th></th><th>Account</th><th>Status</th><th>Trading</th><th>Lot multiplier</th><th>Symbols</th></tr></thead>
      <tbody>${clients || `<tr><td colspan="6" class="sub">No other accounts.</td></tr>`}</tbody></table>
    <div class="actions">
      <button class="primary" id="copier-save" data-action="copier-save" ${dirty ? "" : "disabled"}>Save changes</button>
      <button data-action="copier-discard" ${dirty ? "" : "hidden"}>Discard</button>
      <span class="hint">${dirty ? "Unsaved changes" : idleHint}</span>
    </div></div>`;

  const copies = c.copies || [];
  const opened = copies.filter((x) => x.openMs != null);
  const closed = copies.filter((x) => x.closeMs != null);
  const problems = copies.filter((x) => x.status === "rejected" || x.status === "unknown" || (x.status === "open" && x.reason)).length;
  const stats = `<div class="stats">
    ${latencyStat("Copy open", opened.map((x) => x.openMs), opened.map((x) => x.brokerOpenMs).filter((x) => x != null))}
    ${latencyStat("Copy close", closed.map((x) => x.closeMs), closed.map((x) => x.brokerCloseMs).filter((x) => x != null))}
    <div class="stat"><div class="label">Open copies</div><div class="value">${copies.filter((x) => x.status === "open").length}</div><div class="sub">on all clients</div></div>
    <div class="stat"><div class="label">Need attention</div><div class="value ${problems ? "status-rejected" : ""}">${problems}</div><div class="sub">rejected, unknown or waiting</div></div>
  </div>`;

  const rows = copies.slice(0, 100).map((x) => `<tr>
      <td class="num">${esc(fmtTime(x.createdAt))}</td>
      <td class="num">${esc(x.masterTicket)}</td>
      <td>${esc(x.clientId)}</td>
      <td>${esc(x.symbol)}</td>
      <td>${esc(x.side)}</td>
      <td class="num">${esc(x.masterVolume)} → ${esc(x.volume ?? "")}</td>
      <td class="status-${esc(x.status)}">${esc(x.status)}</td>
      <td class="num">${esc(x.clientTicket ?? "")}</td>
      <td class="num">${x.openMs != null ? `${fmtMs(x.openMs)} ms<div class="sub">broker ${fmtMs(x.brokerOpenMs)}</div>` : ""}</td>
      <td class="num">${x.closeMs != null ? `${fmtMs(x.closeMs)} ms<div class="sub">broker ${fmtMs(x.brokerCloseMs)}</div>` : ""}</td>
      <td>${esc(x.reason ?? "")}</td>
    </tr>`).join("");
  const table = copies.length
    ? `<table><thead><tr><th>Time</th><th>Master ticket</th><th>Client</th><th>Symbol</th><th>Side</th><th>Lots</th><th>Status</th><th>Client ticket</th><th>Open</th><th>Close</th><th>Reason</th></tr></thead><tbody>${rows}</tbody></table>
       <p class="hint">Open and Close: time from the master change being seen to the client's broker confirming. Broker: the part spent waiting for the broker.</p>`
    : `<div class="empty">No copies yet. Open a trade on the master account and it appears here.</div>`;

  return `<div class="copier">${settings}${stats}<h3 class="section">Recent copies</h3>${table}</div>`;
}

function saveCopier(form, active) {
  const clients = [];
  for (const [accountId, client] of Object.entries(form.clients)) {
    if (!client.on || accountId === form.masterId || !state.accounts.some((a) => a.id === accountId)) continue;
    const multiplier = parseFloat(client.mult);
    if (!(multiplier > 0 && multiplier <= 100)) throw new Error(`Lot multiplier for ${accountId} must be above 0 and at most 100.`);
    const symbols = {};
    for (const { from, to } of client.symbols || []) {
      const [master, own] = [from.trim(), to.trim()];
      if (!master && !own) continue;  // an empty row is simply ignored
      if (!master || !own) throw new Error(`Symbol mapping for ${accountId}: fill in both the master symbol and the client symbol, or remove the row.`);
      if (master in symbols) throw new Error(`Symbol mapping for ${accountId}: ${master} is listed twice.`);
      symbols[master] = own;
    }
    clients.push({ accountId, multiplier, symbols });
  }
  return api("PUT", "/copier", { masterId: form.masterId || null, active: Boolean(active && form.masterId), clients });
}

// ---------- log ----------

function seenErrorId() {
  try { return Number(localStorage.getItem(LOG_SEEN_KEY)) || 0; } catch { return 0; }
}

function markErrorsSeen() {
  try { localStorage.setItem(LOG_SEEN_KEY, String(state.newestErrorId)); } catch { /* storage blocked */ }
}

// A dot on the Log tab when an error arrived that the user has not looked at yet.
function updateLogBadge() {
  const badge = $("#log-badge");
  const unseen = state.tab !== "log" && state.newestErrorId > seenErrorId();
  badge.hidden = !unseen;
  badge.textContent = unseen ? "!" : "";
  badge.title = unseen ? "New errors" : "";
}

const LOG_FILTERS = [["info", "Everything"], ["warning", "Warnings and errors"], ["error", "Errors only"]];

function renderLog() {
  const filters = LOG_FILTERS.map(([level, label]) =>
    `<button class="small ${state.logLevel === level ? "primary" : ""}" data-action="log-level" data-level="${level}">${label}</button>`).join("");
  const rows = state.logs.map((e) => `<tr>
      <td class="num">${esc(fmtTime(e.at))}</td>
      <td class="level-${esc(e.level)}">${esc(e.level)}</td>
      <td>${esc(e.account ?? "")}</td>
      <td>${esc(e.source)}</td>
      <td class="log-message">${esc(e.message)}</td>
    </tr>`).join("");
  const table = rows
    ? `<table><thead><tr><th>Time</th><th>Level</th><th>Account</th><th>Part</th><th>Message</th></tr></thead><tbody>${rows}</tbody></table>`
    : `<div class="empty">Nothing logged${state.logLevel === "info" ? "" : " at this level"} yet.</div>`;
  return `<div class="log">
    <div class="actions">
      <div class="presets">${filters}</div>
      <span class="hint">Newest first. Connection problems, refused orders, copier problems and settings changes are kept here (the newest 5000).</span>
      <button class="small" data-action="log-clear">Clear log</button>
    </div>
    ${table}</div>`;
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
  if (action === "copier-save") {
    act(async () => { await saveCopier(state.copierDraft, state.copier.active); state.copierDraft = null; }, "Copier settings saved");
  }
  if (action === "copier-discard") { state.copierDraft = null; render(); }
  if (action === "log-level") { state.logLevel = el.dataset.level; refresh(true); }
  if (action === "log-clear" && window.confirm("Delete every entry in the log?")) act(() => api("DELETE", "/logs"), "Log cleared");
  if (action === "copier-symbols") { state.copierSymbolsOpen = state.copierSymbolsOpen === id ? null : id; render(); }
  if (action === "copier-sym-add") {
    editCopier((d) => { const client = draftClient(d, id); client.symbols = [...client.symbols, { from: "", to: "" }]; });
    render();
    const inputs = view.querySelectorAll(`input[data-copier="sym-from"][data-id="${CSS.escape(id)}"]`);
    inputs[inputs.length - 1]?.focus();
  }
  if (action === "copier-sym-remove") {
    editCopier((d) => { const client = draftClient(d, id); client.symbols = client.symbols.filter((_, i) => i !== Number(el.dataset.index)); });
    render();
  }
  // ON/OFF uses the saved settings, so unsaved edits stay in the form.
  if (action === "copier-active") act(() => saveCopier(savedCopierForm(), !on), on ? "Copier OFF: new master trades are not copied" : "Copier ON");
  if (action === "trade") {
    if (state.openTrade === id) { state.openTrade = null; state.openTradeDetail = null; render(); return; }
    state.openTrade = id;
    state.openTradeDetail = null;
    refresh(true);  // loads the per-account results of the open request
  }
});

view.addEventListener("change", (e) => {
  const { copier, id } = e.target.dataset;
  if (copier === "master") {
    editCopier((d) => { d.masterId = e.target.value || null; });
    render();
    return;
  }
  if (copier === "client") {
    editCopier((d) => { draftClient(d, id).on = e.target.checked; });
    render();
    return;
  }
  if (e.target.dataset.action !== "assign") return;
  const ids = [...view.querySelectorAll("[data-action=assign]:checked")].map((c) => c.dataset.id);
  const tag = state.selectedStrategy;
  act(() => api("PUT", `/strategies/${encodeURIComponent(tag)}/accounts`, { accountIds: ids }),
    `Accounts for ${tag} saved. The next request uses them.`);
});

view.addEventListener("input", (e) => {
  const { copier, id, index } = e.target.dataset;
  if (copier === "mult") editCopier((d) => { draftClient(d, id).mult = e.target.value; });
  else if (copier === "sym-from" || copier === "sym-to") {
    editCopier((d) => { draftClient(d, id).symbols[Number(index)][copier === "sym-from" ? "from" : "to"] = e.target.value; });
  } else return;
  // No redraw while typing (it would lose the cursor); just enable Save.
  const save = $("#copier-save");
  if (save) save.disabled = false;
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
  if (tab === "log") { refresh(true); return; }  // loads the log entries, then draws
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

// ---------- appearance: system, light or dark ----------

function savedTheme() {
  try { return localStorage.getItem(THEME_KEY) || "system"; } catch { return "system"; }
}

function applyTheme(choice) {
  if (choice === "light" || choice === "dark") document.documentElement.dataset.theme = choice;
  else delete document.documentElement.dataset.theme;  // follow the operating system
  try {
    if (choice === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, choice);
  } catch { /* storage blocked: the choice lasts until the page is closed */ }
  document.querySelectorAll("[data-theme-choice]").forEach((b) => b.classList.toggle("active", b.dataset.themeChoice === choice));
}

document.querySelectorAll("[data-theme-choice]").forEach((b) => b.addEventListener("click", () => applyTheme(b.dataset.themeChoice)));
applyTheme(savedTheme());

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
