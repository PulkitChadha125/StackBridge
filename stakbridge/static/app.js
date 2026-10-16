// StakBridge UI: plain JavaScript, no build step. Talks only to the /v1 REST API.
// The browser signs in (with a licence: username and password checked by the licence server) and gets a session cookie.

const REFRESH_MS = 1000;  // balance, equity and open P/L follow MetaTrader about once a second
const COPY_MAGIC = 990000;  // magic number the copier puts on every copied position
const THEME_KEY = "stakbridge-theme";
const LOG_SEEN_KEY = "stakbridge-log-seen";  // newest error id the user has already seen in the Log tab
// Required on every browser request that changes something. Other websites cannot add it.
const UI_HEADERS = { "X-StakBridge": "1" };
let signedIn = false;
let licensed = false;  // StakBridge signs in with a licence (the EXE): see /v1/session

const state = {
  tab: "accounts",
  accounts: [],
  killSwitch: false,
  openPositions: null,      // account id whose positions are expanded
  positionsData: null,      // {positions, orders} or {error}
  editingAccount: null,     // account id being edited in the form, null when adding
  copier: null,             // saved copier settings, status and recent copies
  copierDraft: null,        // unsaved edits in the Copier tab: {masterId, clients: {id: {mult, symbols: [{from, to}]}}}
  copierSymbolsOpen: null,  // client account id whose symbol mapping is expanded
  logs: [],
  logLevel: "info",         // info = everything, warning = warnings and errors, error = errors only
  newestErrorId: 0,
  orders: [],               // master trades for the Trades tab, newest first, each with its client copies
  openOrder: null,          // "masterId:ticket" of the trade shown in the pop-up
  orderLogs: null,          // log entries of the expanded order
  checks: {},               // account id -> {loading} | {data} | {error}: the live check under the account
  busy: {},                 // account id -> true while its Trading switch is being saved
  licence: null,            // licence status and client limit (only with a licence)
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
  if (res.status === 401) {
    const session = licensed ? await fetch("/v1/session", { credentials: "same-origin" }).then((r) => r.json()).catch(() => null) : null;
    const why = session && ["admin_deleted", "invalid_session"].includes(session.licence?.reason) ? session.licence.message : "";
    showSignIn(why || "Your session ended. Please sign in again.");
    throw new Unauthorized();
  }
  if (!res.ok) {
    const err = data.error || {};
    if (err.code === "PASSWORD_CHANGE_REQUIRED") { showPasswordChange(true); throw new Unauthorized(); }
    const details = Array.isArray(err.details) ? err.details.map((d) => `${d.field}: ${d.problem}`).join("; ") : "";
    const error = new Error(`${err.message || res.statusText}${details ? ` (${details})` : ""}`);
    error.code = err.code;
    error.details = Array.isArray(err.details) ? null : err.details;
    throw error;
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
    const [accounts, health, copier, errors, licence] = await Promise.all([
      api("GET", "/accounts"), api("GET", "/health"), api("GET", "/copier"), api("GET", "/logs?level=error&limit=1"),
      licensed ? api("GET", "/license") : null,
    ]);
    state.licence = licence;
    state.newestErrorId = errors.logs[0]?.id ?? 0;
    if (state.tab === "log") state.logs = (await api("GET", `/logs?level=${state.logLevel}&limit=500`)).logs;
    if (state.tab === "trades") {
      state.orders = (await api("GET", "/orders?limit=200")).orders;
      const open = state.orders.find((o) => orderKey(o) === state.openOrder);
      state.orderLogs = open ? (await api("GET", `/orders/${open.ticket}/logs`).catch(() => ({ logs: [] }))).logs : null;
    }
    state.copier = copier;
    state.accounts = accounts.accounts;
    state.killSwitch = health.killSwitch;
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

// ---------- licence ----------

// The badge at the top, and the red bar while the licence is not valid (no new trades are copied then).
function renderLicence() {
  const l = state.licence;
  const badge = $("#licence-badge");
  badge.hidden = !l;
  $("#licence-bar").hidden = !l || l.valid;
  if (!l) return;
  const who = l.name || l.username || "";
  if (!l.valid) {
    badge.textContent = "Licence not active";
    badge.className = "licence-badge bad";
    $("#licence-bar-text").textContent = `${l.message || ""} Closes and SL/TP changes from the master are still copied.`;
  } else if (l.reason === "offline_grace") {
    badge.textContent = `${who} · licence server unreachable, working offline until ${fmtTime(l.graceUntil)}`;
    badge.className = "licence-badge warn";
  } else {
    badge.textContent = `${who} · licence until ${l.expiryDate}`;
    badge.className = "licence-badge";
  }
}

// "Client accounts: 2 of 5" under the Client accounts heading.
function clientLimitLine() {
  const c = state.licence?.clients;
  if (!c || !c.limitKnown) return "";
  if (c.unlimited) return `<p class="client-limit">Client accounts: ${esc(c.thisPc)} (no limit)</p>`;
  const used = c.total ?? c.thisPc;
  const elsewhere = used > c.thisPc ? ` (${esc(used - c.thisPc)} on your other PCs)` : "";
  return `<p class="client-limit${used >= c.limit ? " full" : ""}">Client accounts: ${esc(used)} of ${esc(c.limit)} used${elsewhere}.
    The master account does not count.</p>`;
}

function showLimitModal(details) {
  const d = details || state.licence?.clients || {};
  const support = d.support || {};
  $("#limit-text").textContent = `Your licence allows ${d.limit ?? "no more"} client account${d.limit === 1 ? "" : "s"}`
    + `${d.total != null ? ` and ${d.total} ${d.total === 1 ? "is" : "are"} in use on your PCs` : ""}.`;
  $("#limit-email").textContent = support.email || "";
  $("#limit-email").href = support.email ? `mailto:${support.email}` : "#";
  $("#limit-whatsapp").hidden = !support.whatsapp_url;
  $("#limit-whatsapp").href = support.whatsapp_url || "#";
  $("#limit-modal").hidden = false;
}

$("#limit-modal").addEventListener("click", (e) => {
  if (e.target.id === "limit-modal" || e.target.closest("[data-limit-close]")) $("#limit-modal").hidden = true;
});
$("#licence-bar-check").addEventListener("click", () => act(async () => {
  state.licence = await api("GET", "/license?refresh=true");
  if (!state.licence.valid) throw new Error(state.licence.message || "The licence is still not active.");
}, "Licence is active again"));

// ---------- rendering ----------

function render(keepTyped = false) {
  renderLicence();
  // Keep what the user typed (but did not submit) across the automatic refresh.
  const typed = {};
  if (keepTyped) view.querySelectorAll("input[name]").forEach((i) => { if (i.type !== "checkbox" && i.value) typed[i.name] = i.value; });

  $("#kill-banner").hidden = !state.killSwitch;
  $("#stop-bar").hidden = !state.killSwitch;
  const killBtn = $("#kill-btn");
  killBtn.textContent = state.killSwitch ? "Resume trading" : "Stop all";
  killBtn.className = state.killSwitch ? "primary" : "danger";
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === state.tab));
  $("#add-account").hidden = state.tab !== "accounts";
  if (!state.editingAccount) updateRoleField();

  if (state.tab === "accounts") view.innerHTML = renderAccounts();
  if (state.tab === "trades") view.innerHTML = renderTrades();
  if (state.tab === "copier") view.innerHTML = renderCopier();
  if (state.tab === "log") { view.innerHTML = renderLog(); markErrorsSeen(); }
  updateLogBadge();
  renderModal();

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
  const source = (magic) => magic === COPY_MAGIC ? "copier" : magic ? `EA (magic ${magic})` : "by hand";
  const positions = data.positions.map((p) => `<tr>
      <td>${esc(p.ticket)}</td><td>position</td><td>${esc(p.symbol)}</td><td>${esc(p.side)}</td><td>${esc(p.volume)}</td>
      <td>${esc(p.price)}</td><td>${esc(p.sl || "")}</td><td>${esc(p.tp || "")}</td><td class="num">${pl(p.profit)}</td>
      <td>${esc(source(p.magic))}</td><td>${esc(p.comment)}</td></tr>`);
  const orders = data.orders.map((o) => `<tr>
      <td>${esc(o.ticket)}</td><td>waiting ${esc(o.type ?? "")} order</td><td>${esc(o.symbol)}</td><td>${esc(o.side ?? "")}</td><td>${esc(o.volume)}</td>
      <td>${esc(o.price)}</td><td>${esc(o.sl || "")}</td><td>${esc(o.tp || "")}</td><td></td><td>${esc(source(o.magic))}</td><td>${esc(o.comment)}</td></tr>`);
  return `<table>
    <thead><tr><th>Ticket</th><th>Kind</th><th>Symbol</th><th>Side</th><th>Volume</th><th>Price</th><th>SL</th><th>TP</th><th>P/L</th><th>Placed</th><th>Comment</th></tr></thead>
    <tbody>${positions.join("")}${orders.join("")}</tbody></table>`;
}

const money = (value, currency = "") =>
  typeof value === "number" ? `${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${currency ? ` ${esc(currency)}` : ""}` : "–";

// Profit or loss, green above zero and red below.
function pl(value, currency = "") {
  if (typeof value !== "number") return `<span class="sub">–</span>`;
  const cls = value > 0 ? "pl-up" : value < 0 ? "pl-down" : "";
  return `<span class="${cls}">${value > 0 ? "+" : ""}${money(value, currency)}</span>`;
}

function tradingButton(a) {
  if (state.busy[a.id]) return `<button class="toggle busy" disabled>Saving…</button>`;
  return `<button class="toggle ${a.tradingOn ? "on" : "off"}" data-action="trading" data-id="${esc(a.id)}" data-on="${a.tradingOn}"
    title="${a.tradingOn ? "Trading is ON. Click to switch it OFF." : "Trading is OFF. Click to switch it ON and check the account live."}">${a.tradingOn ? "● ON" : "○ OFF"}</button>`;
}

// The live check: balance and connection details read from MetaTrader at the moment of the click.
function renderCheck(a) {
  const c = state.checks[a.id];
  if (!c) return "";
  const id = esc(a.id);
  if (c.loading) return `<div class="check"><span class="dot connecting"></span>Checking ${esc(a.name)} in MetaTrader…</div>`;
  if (c.error) {
    return `<div class="check bad"><div class="check-head"><b><span class="dot error"></span>Check failed: ${esc(a.name)} did not answer</b>
      <span class="row-actions"><button class="small" data-action="check" data-id="${id}">Try again</button>
      <button class="small" data-action="check-hide" data-id="${id}">Hide</button></span></div>
      <div>${esc(c.error)}</div></div>`;
  }
  const d = c.data;
  const warnings = [
    !d.brokerConnected && "MetaTrader has no connection to the broker's server.",
    !d.algoTrading && "Algo Trading is OFF in this MetaTrader. Click the Algo Trading button so it turns green, or every order is refused.",
    !d.accountTradeAllowed && "The broker does not allow trading on this account (read-only/investor password?).",
  ].filter(Boolean);
  const item = (label, value) => `<div><div class="label">${label}</div><div class="value">${value}</div></div>`;
  return `<div class="check ${warnings.length ? "warn" : "ok"}">
    <div class="check-head"><b><span class="dot ${warnings.length ? "connecting" : "connected"}"></span>${warnings.length ? "Connected, with a problem" : "Connection OK"}:
      ${esc(d.name || a.name)} · ${esc(d.login)} · ${esc(d.server)}${d.company ? ` · ${esc(d.company)}` : ""}${d.demo ? "" : ` · <span class="status-rejected">REAL MONEY</span>`}</b>
      <span class="row-actions"><span class="sub">checked ${esc(new Date(d.checkedAt).toLocaleTimeString())}</span>
      <button class="small" data-action="check" data-id="${id}">Check again</button>
      <button class="small" data-action="check-hide" data-id="${id}">Hide</button></span></div>
    <div class="check-grid">
      ${item("Balance", money(d.balance, d.currency))}
      ${item("Equity", money(d.equity, d.currency))}
      ${item("Open P/L", money(d.profit, d.currency))}
      ${item("Free margin", money(d.freeMargin, d.currency))}
      ${item("Leverage", d.leverage ? `1:${esc(d.leverage)}` : "–")}
      ${item("Open positions", esc(d.openPositions ?? "–"))}
      ${item("Ping to broker", d.pingMs != null ? `${esc(d.pingMs)} ms` : "–")}
      ${item("Answered in", `${esc(d.roundTripMs)} ms`)}
    </div>
    ${warnings.map((w) => `<div class="status-rejected">${esc(w)}</div>`).join("")}
  </div>`;
}

async function runCheck(accountId) {
  state.checks[accountId] = { loading: true };
  render();
  try {
    state.checks[accountId] = { data: await api("GET", `/accounts/${encodeURIComponent(accountId)}/check`) };
  } catch (e) {
    if (e instanceof Unauthorized) return;
    state.checks[accountId] = { error: e.message };
  }
  render();
}

function accountActions(a, extraButton = "") {
  const id = esc(a.id);
  return `<td class="row-actions">${extraButton}
    <button class="small" data-action="open-mt5" data-id="${id}" title="Open this account's MetaTrader window on this PC">Open MT5</button>
    <button class="small" data-action="check" data-id="${id}" title="Read balance and connection details from MetaTrader now">Check</button>
    <button class="small" data-action="positions" data-id="${id}">${state.openPositions === a.id ? "Hide" : "Positions"}</button>
    <button class="small" data-action="edit" data-id="${id}">Edit</button>
    <button class="small" data-action="enable" data-id="${id}" data-on="${a.enabled}">${a.enabled ? "Disable" : "Enable"}</button>
    <button class="small" data-action="remove" data-id="${id}">Remove</button>
  </td>`;
}

function accountName(a, extra = "") {
  return `<td>${esc(a.name)}${extra}${a.symbolSuffix ? `<div class="sub">suffix ${esc(a.symbolSuffix)}</div>` : ""}</td>`;
}

// Balance, equity and open P/L, read from MetaTrader about once a second while connected.
function moneyCells(a) {
  const info = a.status === "connected" ? a.info || {} : {};
  const cell = (value) => `<td class="num">${typeof value === "number" ? money(value) : `<span class="sub">–</span>`}</td>`;
  return `${cell(info.balance)}${cell(info.equity)}<td class="num">${pl(info.profit)}</td>`;
}

const loginCell = (a) => `<td>${esc(a.login)}<div class="sub">${esc(a.server)}</div></td>`;

// The check and the positions open under the account they belong to.
function accountExtras(a, colspan) {
  const parts = [renderCheck(a), state.openPositions === a.id ? renderPositions() : ""].filter(Boolean);
  return parts.length ? `<tr class="detail"><td colspan="${colspan}">${parts.join("")}</td></tr>` : "";
}

function renderAccounts() {
  if (!state.accounts.length) return `<div class="empty">No accounts yet. Add your master account below (Role: Master), then your client accounts.</div>`;
  const c = state.copier || { masterId: null, clients: [] };
  const master = state.accounts.find((a) => a.id === c.masterId);
  const currency = (master || state.accounts[0]).info?.currency;
  const head = `<tr><th>Name</th><th>Login · Server</th><th>Status</th><th>Balance</th><th>Equity</th><th>Open P/L${currency ? ` <span class="sub">${esc(currency)}</span>` : ""}</th><th>Trading</th><th></th></tr>`;
  const COLS = 8;

  const masterBody = master
    ? `<table><thead>${head}</thead><tbody><tr>
        ${accountName(master)}${loginCell(master)}<td>${statusCell(master)}</td>
        ${moneyCells(master)}<td>${tradingButton(master)}</td>${accountActions(master)}
      </tr>${accountExtras(master, COLS)}</tbody></table>`
    : `<div class="empty">No master account yet. Add one below with Role <b>Master</b>, or press <b>Make master</b> on an account under Client accounts.</div>`;
  const copyState = !master ? "" : master.tradingOn
    ? `<span class="badge on">copying ON</span>` : `<span class="badge off">copying OFF: master Trading is OFF</span>`;

  const clients = state.accounts.filter((a) => a.id !== c.masterId);
  const rows = clients.map((a) => {
    const multiplier = c.clients.find((x) => x.accountId === a.id)?.multiplier ?? 1;
    const badge = master ? ` <span class="badge" title="Lots = master lots × ${esc(multiplier)}. Change it in the Copier tab.">×${esc(multiplier)}</span>` : "";
    const extraButton = master ? "" : `<button class="small" data-action="make-master" data-id="${esc(a.id)}">Make master</button>`;
    return `<tr>
      ${accountName(a, badge)}${loginCell(a)}<td>${statusCell(a)}</td>
      ${moneyCells(a)}<td>${tradingButton(a)}</td>${accountActions(a, extraButton)}
    </tr>${accountExtras(a, COLS)}`;
  }).join("");

  return `<div class="accounts">
    <section class="role master-role">
      <h3 class="section">Master account ${copyState}</h3>
      <p class="hint">You trade here in MetaTrader. Every trade opened, changed or closed on it is copied to all client accounts. Its Trading switch turns copying of new trades on or off for everyone.</p>
      ${masterBody}
    </section>
    <section class="role">
      <h3 class="section">Client accounts</h3>
      ${clientLimitLine()}
      <p class="hint">Each copies every master trade while its own Trading switch is ON. Switching one OFF stops new copies only on that account. Lot multipliers and symbol names are in the Copier tab.</p>
      ${rows ? `<table><thead>${head}</thead><tbody>${rows}</tbody></table>` : `<div class="empty">No client accounts yet. Add one below with Role <b>Client</b>.</div>`}
    </section>
  </div>`;
}

// ---------- orders (master orders and what happened on each client) ----------

const orderKey = (o) => `${o.masterId}:${o.ticket}`;

// Plain-language meaning of the reasons a copy can show.
const REASONS = [
  ["TRADING_OFF", "the Trading switch of this account was OFF"],
  ["MASTER_TRADING_OFF", "the master account's Trading switch was OFF, so no account copied it"],
  ["COPIER_PAUSED", "the copier was OFF"],
  ["WRONG_ACCOUNT_IN_TERMINAL", "this MetaTrader was logged into a different account; give each account its own Terminal path"],
  ["LICENSE_INACTIVE", "the StakBridge licence was not active, so no new trades were copied"],
  ["KILL_SWITCH_ON", "Stop all was on (for a waiting order that filled: this account's order was deleted, no trade opened)"],
  ["DISCONNECTED", "this account was not connected"],
  ["ACCOUNT_REMOVED", "this account was removed"],
  ["TOO_OLD_AFTER_RESTART", "the master trade was opened while StakBridge was off, too long ago to copy safely"],
  ["UNKNOWN_SYMBOL", "this broker has no symbol with that name: set the account's suffix or add a symbol mapping in the Copier tab"],
  ["VOLUME_BELOW_BROKER_MIN", "master lots × multiplier is below this broker's minimum lot size"],
  ["VOLUME_ABOVE_BROKER_MAX", "master lots × multiplier is above this broker's maximum lot size"],
  ["NO_PRICE", "MetaTrader had no price for the symbol (market closed, or symbol hidden)"],
  ["OPENED_WITHOUT_SLTP", "the broker refused the master's SL/TP, so the copy was opened without them"],
  ["FOUND_AFTER_LOST_ANSWER", "the answer was lost, and the copy was found on the account afterwards"],
  ["NOT_FOUND_ON_CLIENT", "the answer was lost, and no copy was found on the account"],
  ["ALREADY_CLOSED_ON_CLIENT", "the copy had already closed by itself (its own SL or TP)"],
  ["CLOSE_WAITING", "the master closed but this account cannot be reached; it is closed as soon as it is back"],
  ["CLOSE_FAILED", "closing failed; StakBridge keeps retrying"],
  ["CANCEL_WAITING", "the master deleted its order but this account cannot be reached; its order is deleted as soon as it is back"],
  ["FILL_WAITING", "the master's order filled but this account cannot be reached or its Trading is OFF; it gets the trade as soon as that changes"],
  ["CANCEL_FAILED", "deleting this account's order failed; StakBridge keeps retrying"],
  ["FILL_FAILED", "the master's order filled but this account's order could not be swapped for the trade; StakBridge keeps retrying"],
  ["ORDER_CHANGE_FAILED", "moving this account's order to the master's new price or SL/TP did not go through"],
  ["ALREADY_GONE_ON_CLIENT", "this account's order was already gone (deleted by hand, or it expired)"],
  ["PLACED_WITHOUT_SLTP", "the broker refused the master's SL/TP, so the order was placed without them"],
  ["ORDER_NOT_FOUND", "this account's order was not found"],
  ["SLTP_FAILED", "the SL/TP change did not go through"],
  ["TIMEOUT", "no answer from MetaTrader in time; StakBridge never sends it twice"],
  ["BROKER_10004", "requote: the price moved"],
  ["BROKER_10006", "the broker rejected the order"],
  ["BROKER_10014", "invalid volume for this broker"],
  ["BROKER_10015", "invalid price"],
  ["BROKER_10016", "invalid SL/TP (too close to the price)"],
  ["BROKER_10017", "trading is disabled for this account"],
  ["BROKER_10018", "the market is closed"],
  ["BROKER_10019", "not enough money"],
  ["BROKER_10021", "no prices at the moment"],
  ["BROKER_10024", "too many requests"],
  ["BROKER_10026", "the broker's server has auto trading disabled"],
  ["BROKER_10027", "Algo Trading is OFF in this MetaTrader: click the Algo Trading button so it turns green"],
  ["BROKER_10030", "the broker does not support this order filling type"],
  ["BROKER_10031", "MetaTrader has no connection to the broker"],
];

function explainReason(reason) {
  if (!reason) return "";
  const match = REASONS.find(([code]) => reason.includes(code));
  return `${esc(reason)}${match ? `<div class="sub">${esc(match[1])}</div>` : ""}`;
}

function ms(value, broker) {
  if (value == null) return "";
  return `${fmtMs(value)} ms${broker != null ? `<div class="sub">broker ${fmtMs(broker)} ms</div>` : ""}`;
}

// Reasons on a copy that still needs attention.
const PROBLEM = /CLOSE_|SLTP_FAILED|TIMEOUT|CANCEL_|FILL_|ORDER_CHANGE_FAILED/;

// "Buy", or "Buy Limit" for a waiting order.
const sideName = (o) => {
  const side = o.side === "buy" ? "Buy" : "Sell";
  return o.orderType && o.orderType !== "market" ? `${side} ${o.orderType[0].toUpperCase()}${o.orderType.slice(1)}` : side;
};

function orderSummary(o) {
  const n = o.copies.length;
  if (!n) return `<span class="sub">no clients</span>`;
  const copied = o.copies.filter((c) => c.clientTicket != null && c.status !== "rejected").length;
  const failed = o.copies.filter((c) => c.status === "rejected" || c.status === "unknown" || PROBLEM.test(c.reason || "")).length;
  const skipped = o.copies.filter((c) => c.status === "skipped").length;
  const waiting = o.copies.filter((c) => c.status === "pending").length;
  const parts = [`<span class="${copied === n ? "status-open" : ""}">${copied} of ${n} copied</span>`];
  if (failed) parts.push(`<span class="status-rejected">${failed} problem${failed === 1 ? "" : "s"}</span>`);
  if (skipped) parts.push(`<span class="status-skipped">${skipped} skipped</span>`);
  if (waiting) parts.push(`<span class="status-unknown">${waiting} sending</span>`);
  return parts.join(" · ");
}

// One word for what happened to a trade on one account.
function copyResult(c) {
  if (c.status === "pending") return `<span class="result-wait">Sending…</span>`;
  if (c.status === "skipped") return `<span class="result-skip">Not copied</span>`;
  if (c.status === "rejected") return `<span class="result-bad">Failed</span>`;
  if (c.status === "unknown") return `<span class="result-wait">No answer yet</span>`;
  const problem = PROBLEM.test(c.reason || "");
  if (c.status === "closed") return `<span class="result-ok">Copied, closed</span>`;
  if (c.status === "cancelled") return `<span class="result-skip">Order deleted</span>`;
  if (c.status === "placed") return `<span class="${problem ? "result-wait" : "result-ok"}">Order placed, waiting${problem ? ", needs attention" : ""}</span>`;
  return `<span class="${problem ? "result-wait" : "result-ok"}">Copied${problem ? ", needs attention" : ""}</span>`;
}

function renderOrderDetail(o) {
  const name = (id) => state.accounts.find((a) => a.id === id)?.name ?? id;
  const field = (label, value) => `<div><div class="label">${label}</div><div class="value">${value}</div></div>`;
  const details = `<div class="check-grid order-grid">
    ${field("Master account", `${esc(name(o.masterId))}`)}
    ${field("Master ticket", esc(o.ticket))}
    ${o.orderType && o.orderType !== "market" ? field("Order type", esc(sideName(o))) : ""}
    ${field(o.status === "waiting" || o.status === "cancelled" ? "Order price" : "Open price", esc(o.price ?? "–"))}
    ${field("Stop loss", esc(o.sl || "none"))}
    ${field("Take profit", esc(o.tp || "none"))}
    ${field("Seen at", esc(fmtTime(o.openedAt)))}
    ${field(o.status === "cancelled" ? "Deleted at" : "Closed at", o.closedAt ? esc(fmtTime(o.closedAt))
      : `<span class="status-open">${o.status === "waiting" ? "waiting to fill" : "still open"}</span>`)}
  </div>`;
  const history = o.events.map((e) => `<li><span class="sub">${esc(fmtTime(e.at))}</span> ${esc(e.text)}</li>`).join("");
  const clients = o.copies.map((c) => `<tr>
      <td>${esc(name(c.clientId))}</td>
      <td>${copyResult(c)}</td>
      <td>${esc(c.symbol)}</td>
      <td class="num">${esc(c.volume ?? "")}</td>
      <td class="num">${esc(c.clientTicket ?? "")}</td>
      <td class="num">${esc(c.openPrice ?? "")}</td>
      <td class="num">${ms(c.openMs, c.brokerOpenMs)}</td>
      <td class="num">${esc(c.closePrice ?? "")}</td>
      <td class="num">${ms(c.closeMs, c.brokerCloseMs)}</td>
      <td class="reason">${explainReason(c.reason)}</td>
    </tr>`).join("");
  const logs = (state.orderLogs || []).map((e) => `<tr>
      <td class="num">${esc(fmtTime(e.at))}</td><td class="level-${esc(e.level)}">${esc(e.level)}</td>
      <td>${esc(e.account ?? "")}</td><td class="log-message">${esc(e.message)}</td></tr>`).join("");
  const copied = o.copies.filter((c) => c.clientTicket != null && c.status !== "rejected").length;
  return `<div class="order-detail">
    <h2 id="modal-title">${esc(sideName(o))} ${esc(o.volume)} ${esc(o.symbol)}${o.status === "waiting" ? ` @ ${esc(o.price)}` : ""}
      <span class="sub">· ${o.copies.length ? `copied to ${copied} of ${o.copies.length} accounts` : "no client accounts"}</span></h2>
    <h4>Client accounts</h4>
    ${clients ? `<table><thead><tr><th>Account</th><th>Result</th><th>Symbol</th><th>Lots</th><th>Ticket</th><th>Open price</th><th>Open latency</th><th>Close price</th><th>Close latency</th><th>Error / reason</th></tr></thead><tbody>${clients}</tbody></table>
      <p class="hint">Latency: time from StakBridge seeing the change on the master until that account's broker confirmed it. "broker": the part spent waiting for the broker's server.</p>`
      : `<p class="sub">There were no client accounts when this trade was placed.</p>`}
    <h4>Trade on the master</h4>${details}
    <h4>History</h4><ul class="history">${history}</ul>
    ${logs ? `<h4>Log entries for this trade</h4><table><tbody>${logs}</tbody></table>` : ""}
  </div>`;
}

function renderTrades() {
  const c = state.copier;
  const intro = `<p class="hint">Every trade and limit/stop order placed on the master account${c?.masterId ? ` (<b>${esc(accountLabel(c.masterId))}</b>)` : ""} while StakBridge is running, newest first.
    Click a trade to see on which accounts it was copied, the latency and any error.</p>`;
  if (!c?.masterId) return `${intro}<div class="empty">No master account yet. Set one in the Accounts tab.</div>`;
  if (!state.orders.length) return `${intro}<div class="empty">No trades yet. Place a trade in the master's MetaTrader and it appears here within a second.</div>`;
  const rows = state.orders.map((o) => {
    const key = orderKey(o);
    const open = state.openOrder === key;
    const opens = o.copies.map((x) => x.openMs).filter((x) => x != null);
    return `<tr class="clickable${open ? " open" : ""}" data-action="order" data-id="${esc(key)}">
      <td class="num">${esc(fmtTime(o.openedAt))}</td>
      <td class="num">${esc(o.ticket)}</td>
      <td>${esc(o.symbol)}</td>
      <td>${esc(sideName(o))}</td>
      <td class="num">${esc(o.volume)}</td>
      <td class="num">${esc(o.price ?? "")}</td>
      <td class="num">${esc(o.sl || "–")} / ${esc(o.tp || "–")}</td>
      <td class="status-${o.status === "open" || o.status === "waiting" ? esc(o.status) : "skipped"}">${esc(o.status)}</td>
      <td>${orderSummary(o)}</td>
      <td class="num">${opens.length ? `${fmtMs(Math.max(...opens))} ms` : ""}</td>
    </tr>`;
  }).join("");
  return `${intro}<table>
    <thead><tr><th>Time</th><th>Ticket</th><th>Symbol</th><th>Side</th><th>Lots</th><th>Price</th><th>SL / TP</th><th>Master</th><th>Clients</th><th>Slowest copy</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

// The pop-up of the clicked trade. Redrawn on every refresh, so results and latencies fill in live.
function renderModal() {
  const order = state.tab === "trades" && state.openOrder ? state.orders.find((o) => orderKey(o) === state.openOrder) : null;
  const modal = $("#modal");
  if (!order) { modal.hidden = true; return; }
  const body = $("#modal-body");
  const scroll = modal.scrollTop;
  body.innerHTML = renderOrderDetail(order);
  modal.hidden = false;
  modal.scrollTop = scroll;
}

function closeModal() {
  state.openOrder = null;
  state.orderLogs = null;
  renderModal();
  render();
}

$("#modal").addEventListener("click", (e) => {
  if (e.target.id === "modal" || e.target.closest("[data-close]")) closeModal();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.openOrder) closeModal(); });

// ---------- copier ----------

const NEW_CLIENT = { mult: "1", symbols: [] };

function savedCopierForm() {
  const c = state.copier || { masterId: null, clients: [] };
  return {
    masterId: c.masterId,
    clients: Object.fromEntries(c.clients.map((x) => [x.accountId, {
      mult: String(x.multiplier),
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
  return `<tr class="detail"><td colspan="5"><div class="symbol-map">
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

  const masterAccount = state.accounts.find((a) => a.id === c.masterId);
  let watch = `<span class="dot"></span>No master chosen yet`;
  if (masterAccount && !masterAccount.tradingOn) watch = `<span class="dot error"></span>Master <b>${esc(accountLabel(c.masterId))}</b> has Trading OFF: new trades are not copied. Switch it ON in Accounts.`;
  else if (c.masterId && c.watching) watch = `<span class="dot connected"></span>Watching <b>${esc(accountLabel(c.masterId))}</b> · ${esc(c.masterPositions)} open position${c.masterPositions === 1 ? "" : "s"}`;
  else if (c.masterId) watch = `<span class="dot connecting"></span>Waiting for <b>${esc(accountLabel(c.masterId))}</b> to connect`;

  const options = [`<option value="">Choose the master account</option>`, ...state.accounts.map((a) =>
    `<option value="${esc(a.id)}" ${a.id === form.masterId ? "selected" : ""}>${esc(a.name)}</option>`)].join("");
  const masterSuffix = state.accounts.find((a) => a.id === form.masterId)?.symbolSuffix ?? "";
  const clients = state.accounts.filter((a) => a.id !== form.masterId).map((a) => {
    const client = { ...NEW_CLIENT, ...form.clients[a.id] };
    const mapped = client.symbols.filter((m) => m.from.trim() && m.to.trim()).length;
    const open = state.copierSymbolsOpen === a.id;
    const row = `<tr class="${a.tradingOn ? "" : "off"}">
      <td>${esc(a.name)}${a.symbolSuffix ? `<div class="sub">suffix ${esc(a.symbolSuffix)}</div>` : ""}</td>
      <td>${statusCell(a)}</td>
      <td>${a.tradingOn ? "ON" : `<span class="status-rejected">OFF</span><div class="sub">switch it on in Accounts</div>`}</td>
      <td class="num"><input type="number" min="0.01" max="100" step="0.01" data-copier="mult" data-id="${esc(a.id)}" value="${esc(client.mult)}" aria-label="Lot multiplier for ${esc(a.name)}"> ×</td>
      <td><button class="small" data-action="copier-symbols" data-id="${esc(a.id)}">${open ? "Hide" : mapped ? `${mapped} mapped` : "Map symbols"}</button></td>
    </tr>`;
    return open ? row + renderSymbolMap(a, client, masterSuffix) : row;
  }).join("");
  const idleHint = "Client lots = master lots × multiplier (1 = same lots as the master), rounded down to each broker's lot step.";

  const settings = `<div class="card">
    <h2>Trade copier</h2>
    <div class="meta"><span>${watch}</span></div>
    <h3>Master account</h3>
    <select data-copier="master" aria-label="Master account">${options}</select>
    <p class="hint">Every position opened, changed or closed on the master is copied to every other account whose Trading switch is ON. Trades you place yourself on a client are never touched.</p>
    <h3>Client accounts</h3>
    <table><thead><tr><th>Account</th><th>Status</th><th>Trading</th><th>Lot multiplier</th><th>Symbols</th></tr></thead>
      <tbody>${clients || `<tr><td colspan="5" class="sub">No other accounts.</td></tr>`}</tbody></table>
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

function saveCopier(form) {
  const clients = [];
  for (const [accountId, client] of Object.entries(form.clients)) {
    if (accountId === form.masterId || !state.accounts.some((a) => a.id === accountId)) continue;
    const multiplier = parseFloat(client.mult);
    if (!(multiplier > 0 && multiplier <= 100)) throw new Error(`Lot multiplier for ${accountLabel(accountId)} must be above 0 and at most 100.`);
    const symbols = {};
    for (const { from, to } of client.symbols || []) {
      const [master, own] = [from.trim(), to.trim()];
      if (!master && !own) continue;  // an empty row is simply ignored
      if (!master || !own) throw new Error(`Symbol mapping for ${accountLabel(accountId)}: fill in both the master symbol and the client symbol, or remove the row.`);
      if (master in symbols) throw new Error(`Symbol mapping for ${accountLabel(accountId)}: ${master} is listed twice.`);
      symbols[master] = own;
    }
    clients.push({ accountId, multiplier, symbols });
  }
  return api("PUT", "/copier", { masterId: form.masterId || null, clients });
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

// Role is only asked when adding. There is one master, so "Master" is offered only while there is none.
function updateRoleField() {
  const select = accountForm.elements.role;
  const hasMaster = Boolean(state.copier?.masterId && state.accounts.some((a) => a.id === state.copier.masterId));
  const masterOption = select.querySelector('option[value="master"]');
  masterOption.disabled = hasMaster;
  masterOption.textContent = hasMaster
    ? `Master account: already set (${state.copier.masterId})`
    : "Master account: you trade here, its trades are copied";
  if (!select.dataset.touched) select.value = hasMaster ? "client" : "master";
  if (hasMaster && select.value === "master") select.value = "client";
}

accountForm.elements.role.addEventListener("change", (e) => { e.target.dataset.touched = "1"; });

// Makes a newly added account the master. Every other account is a client by itself (lot multiplier 1).
async function assignRole(accountId, role) {
  if (role === "master") await saveCopier({ ...savedCopierForm(), masterId: accountId });
}

const accountLabel = (id) => state.accounts.find((a) => a.id === id)?.name || id;

function openAccountModal() {
  $("#account-modal").hidden = false;
  fillTerminalSelect(state.terminals || [], accountForm.elements.terminalPath.value);
  loadTerminals();
  accountForm.elements.name.focus();
}

function closeAccountModal() {
  $("#account-modal").hidden = true;
  resetAccountForm();
}

$("#add-account").addEventListener("click", () => { resetAccountForm(); openAccountModal(); });
$("#account-modal").addEventListener("click", (e) => {
  if (e.target.id === "account-modal" || e.target.closest("[data-account-close]")) closeAccountModal();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#account-modal").hidden) closeAccountModal(); });

// ---- Terminal path: a drop-down of the MetaTrader 5 terminals found on this PC ----

const OTHER_TERMINAL = "__other";
const samePath = (a, b) => (a || "").trim().toLowerCase() === (b || "").trim().toLowerCase();

function fillTerminalSelect(terminals, current) {
  const select = $("#terminal-select");
  const editing = state.editingAccount;
  const options = [terminals.length
    ? '<option value="">Choose a MetaTrader 5...</option>'
    : '<option value="">No MetaTrader 5 found on this PC: install one, then click Refresh</option>'];
  for (const t of terminals) {
    const mine = editing && t.usedById === editing;
    const other = t.usedById && !mine;
    const note = mine ? "this account" : other ? `used by ${t.usedByName}` : "new, not used yet";
    const cls = other ? "term-used" : mine ? "" : "term-new";
    options.push(`<option value="${esc(t.path)}" class="${cls}"${other ? " disabled" : ""}>${esc(t.name)} (${esc(note)}): ${esc(t.path)}</option>`);
  }
  options.push(`<option value="${OTHER_TERMINAL}">Other: type the path myself...</option>`);
  select.innerHTML = options.join("");
  const found = terminals.find((t) => samePath(t.path, current));
  select.value = found ? found.path : current ? OTHER_TERMINAL : "";
  showTerminalChoice(false);
}

function showTerminalChoice(focus = true) {
  const select = $("#terminal-select");
  const input = accountForm.elements.terminalPath;
  const other = select.value === OTHER_TERMINAL;
  input.hidden = !other;
  if (!other) input.value = select.value;
  select.className = select.selectedOptions[0]?.className || "";
  if (other && focus) input.focus();
}

async function loadTerminals() {
  const button = $("#terminal-refresh");
  button.classList.add("spinning");
  try {
    const before = state.terminals ? new Set(state.terminals.map((t) => t.path.toLowerCase())) : null;
    const { terminals } = await api("GET", "/terminals");
    state.terminals = terminals;
    fillTerminalSelect(terminals, accountForm.elements.terminalPath.value);
    const added = before ? terminals.filter((t) => !before.has(t.path.toLowerCase())) : [];
    if (added.length) flash(`Found new MetaTrader 5: ${added.map((t) => t.name).join(", ")}`, true);
  } catch (e) {
    flash(`Could not look for MetaTrader on this PC: ${e.message}`);
  } finally {
    button.classList.remove("spinning");
  }
}

$("#terminal-select").addEventListener("change", () => showTerminalChoice());
$("#terminal-refresh").addEventListener("click", loadTerminals);

function startEdit(accountId) {
  const a = state.accounts.find((x) => x.id === accountId);
  if (!a) return;
  state.editingAccount = a.id;
  const f = accountForm.elements;
  f.name.value = a.name;
  f.login.value = a.login;
  f.password.value = "";
  f.password.required = false;
  f.password.placeholder = "leave empty to keep the current password";
  f.server.value = a.server;
  f.symbolSuffix.value = a.symbolSuffix;
  f.terminalPath.value = a.terminalPath;
  $("#role-field").hidden = true;
  $("#account-form-title").textContent = `Edit ${a.name}`;
  $("#account-submit").textContent = "Save changes";
  $("#account-hint").textContent = "Changing login, password, server or terminal path reconnects the account.";
  openAccountModal();
}

function resetAccountForm() {
  state.editingAccount = null;
  accountForm.reset();
  const f = accountForm.elements;
  f.password.required = true;
  f.password.placeholder = "";
  $("#role-field").hidden = false;
  delete f.role.dataset.touched;
  updateRoleField();
  $("#account-form-title").textContent = "Add account";
  $("#account-submit").textContent = "Add account";
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
    const name = body.name;
    act(async () => { await api("PATCH", `/accounts/${encodeURIComponent(editing)}`, body); closeAccountModal(); },
      `${name} saved`);
  } else {
    const role = f.role.value;
    act(async () => {
      let added;
      try {
        added = await api("POST", "/accounts", body);
      } catch (e) {
        if (e.code !== "CLIENT_LIMIT_REACHED") throw e;
        showLimitModal(e.details);
        return;
      }
      await assignRole(added.id, role);
      closeAccountModal();
      flash(`${role === "master" ? "Master" : "Client"} account ${added.name} added, connecting... Switch its Trading ON once it shows Connected.`, true);
    });
  }
});

$("#account-cancel").addEventListener("click", () => closeAccountModal());

// The switch shows "Saving…" until the server answers. Switching ON also checks the account live,
// so the balance shown proves MetaTrader really answers.
async function toggleTrading(accountId, turnOn) {
  state.busy[accountId] = true;
  render();
  try {
    await api("PATCH", `/accounts/${encodeURIComponent(accountId)}`, { tradingOn: turnOn });
    flash(turnOn ? `Trading ON for ${accountLabel(accountId)}. Checking the account…` : `Trading OFF for ${accountLabel(accountId)}: no new trades are placed on it.`, true);
  } catch (e) {
    if (!(e instanceof Unauthorized)) flash(e.message);
    delete state.busy[accountId];
    await refresh(true);
    return;
  }
  delete state.busy[accountId];
  if (!turnOn) delete state.checks[accountId];
  await refresh(true);
  if (turnOn) await runCheck(accountId);
}

// ---------- view events ----------

view.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action]");
  if (!el) return;
  const { action, id } = el.dataset;
  const on = el.dataset.on === "true";
  const path = (p) => encodeURIComponent(p);

  if (action === "trading") toggleTrading(id, !on);
  if (action === "check") runCheck(id);
  if (action === "check-hide") { delete state.checks[id]; render(); }
  if (action === "make-master") {
    act(() => saveCopier({ ...savedCopierForm(), masterId: id }),
      `${id} is now the master account. Its trades are copied to every account whose Trading is ON.`);
  }
  if (action === "open-mt5") {
    api("POST", `/accounts/${path(id)}/open-terminal`)
      .then(() => flash(`Opening MetaTrader for ${id}…`, true))
      .catch((err) => { if (!(err instanceof Unauthorized)) flash(err.message); });
  }
  if (action === "order") {
    state.openOrder = id;
    state.orderLogs = null;
    render();
    refresh(true);  // loads the log entries of the trade in the pop-up
  }
  if (action === "enable") act(() => api("PATCH", `/accounts/${path(id)}`, { enabled: !on }), `${accountLabel(id)} ${on ? "disabled" : "enabled"}`);
  if (action === "edit") startEdit(id);
  if (action === "remove" && window.confirm(`Remove ${accountLabel(id)}? Its open positions stay at the broker.`)) {
    const name = accountLabel(id);
    if (state.editingAccount === id) closeAccountModal();
    act(() => api("DELETE", `/accounts/${path(id)}`), `${name} removed`);
  }
  if (action === "positions") {
    if (state.openPositions === id) { state.openPositions = null; state.positionsData = null; render(); return; }
    state.openPositions = id;
    state.positionsData = null;
    render();
    refresh(true);  // loads the positions of the open account
  }
  if (action === "copier-save") {
    act(async () => { await saveCopier(state.copierDraft); state.copierDraft = null; }, "Copier settings saved");
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
});

view.addEventListener("change", (e) => {
  const { copier, id } = e.target.dataset;
  if (copier === "master") {
    editCopier((d) => { d.masterId = e.target.value || null; });
    render();
    return;
  }
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

// ---------- header ----------

$("#tabs").addEventListener("click", (e) => {
  const tab = e.target.closest("button")?.dataset.tab;
  if (!tab) return;
  if (tab !== state.tab) {  // a short fade-in of the new tab (restarted by taking the class off and on)
    view.classList.remove("tab-enter");
    void view.offsetWidth;
    view.classList.add("tab-enter");
  }
  state.tab = tab;
  if (tab === "log" || tab === "trades") { refresh(true); return; }  // loads the entries, then draws
  render();
});

$("#stop-bar-resume").addEventListener("click", () => act(() => api("DELETE", "/kill"), "Trading resumed: new master trades are copied again"));

$("#kill-btn").addEventListener("click", () => {
  if (!state.killSwitch && !window.confirm("Stop all?\n\nNo new master trades will be copied to ANY account until you press Resume trading. Closes are still copied.")) return;
  if (state.killSwitch) {
    act(() => api("DELETE", "/kill"), "Trading resumed: new master trades are copied again");
  } else {
    act(() => api("POST", "/kill"), "No new trades are copied to any account. Closes still are.");
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
  const details = Array.isArray(err.details) ? err.details.map((d) => d.problem).join("; ") : "";
  return { ok: res.ok, code: err.code, retryable: err.retryable, data,
           message: details || err.message || res.statusText };
}

const UNREACHABLE = { ok: false, message: "Cannot reach StakBridge. Is its window still open?" };

// Shows one of the sign-in screens (or none) and the app header controls only when signed in.
function showScreen(screen) {
  ["login", "setup", "setup-remote", "password-change", "forgot"].forEach((id) => { $(`#${id}`).hidden = id !== screen; });
  const inApp = screen === null;
  signedIn = inApp;
  $("#app").hidden = !inApp;
  $("#tabs").hidden = !inApp;
  $("#kill-btn").hidden = !inApp;
  $("#logout-btn").hidden = !inApp && screen !== "password-change";
  $("#password-btn").hidden = !inApp || !licensed;
  if (!inApp) {
    $("#kill-banner").hidden = true; $("#stop-bar").hidden = true;
    $("#licence-badge").hidden = true; $("#licence-bar").hidden = true; $("#limit-modal").hidden = true;
  }
}

function showSignIn(message = "", good = false) {
  showScreen("login");
  $("#login-error").textContent = message;
  $("#login-error").classList.toggle("good", good);
  (licensed && !$("#login-username").value ? $("#login-username") : $("#login-password")).focus();
}

// Licence sign-in: username + password, "forgot password", and which Admin this EXE was built for.
function setUpLicensedSignIn(session) {
  licensed = true;
  $("#login-username-field").hidden = false;
  $("#login-username").required = true;
  if (session.username && !$("#login-username").value) $("#login-username").value = session.username;
  $("#login-built-for").hidden = !session.builtFor;
  $("#login-built-for").textContent = session.builtFor ? `StakBridge for ${session.builtFor}` : "";
  $("#login-hint").hidden = true;
  $("#login-forgot").hidden = false;
}

function showPasswordChange(forced) {
  showScreen("password-change");
  $("#password-change-form").reset();
  $("#password-change-error").textContent = "";
  $("#password-change-intro").textContent = forced
    ? "This is your first sign-in with the password from your provider. Choose your own password to continue."
    : "Type your current password, then the new one. You stay signed in on this PC.";
  $("#pc-cancel").hidden = forced;
  $("#pc-current").focus();
}

const SIGN_IN_ERRORS = {
  INVALID_CREDENTIALS: "Wrong username or password.",
  LICENSE_SERVER_UNREACHABLE: "Can't reach the licence server. Check this PC's internet connection and try again.",
};

function showApp() {
  showScreen(null);
  refresh(true);
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#login-password");
  const button = e.target.querySelector("button");
  button.disabled = true;
  $("#login-error").textContent = licensed ? "Checking your licence…" : "";
  $("#login-error").classList.add("good");
  const body = licensed ? { username: $("#login-username").value.trim(), password: input.value } : { password: input.value };
  const result = await postJson("/session", body).catch(() => UNREACHABLE);
  button.disabled = false;
  input.value = "";
  if (result.ok) {
    $("#login-error").textContent = "";
    if (result.data.mustChangePassword) showPasswordChange(true); else showApp();
    return;
  }
  $("#login-error").classList.remove("good");
  $("#login-error").textContent = result.code === "WRONG_PASSWORD" ? "Wrong password, try again."
    : SIGN_IN_ERRORS[result.code] || result.message;
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
  if (licensed && !window.confirm("Sign out?\n\nCopying stops on this PC until you sign in again.")) return;
  await fetch("/v1/session", { method: "DELETE", credentials: "same-origin", headers: UI_HEADERS }).catch(() => {});
  showSignIn(licensed ? "You are signed out. Copying is stopped on this PC until you sign in again." : "You are signed out.", true);
});

$("#password-btn").addEventListener("click", () => showPasswordChange(false));
$("#pc-cancel").addEventListener("click", () => showApp());

$("#password-change-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const error = $("#password-change-error");
  if ($("#pc-new").value !== $("#pc-confirm").value) { error.textContent = "The two new passwords are different."; return; }
  if ($("#pc-new").value.length < 8) { error.textContent = "Use at least 8 characters."; return; }
  error.textContent = "Saving…";
  const result = await postJson("/session/password", { currentPassword: $("#pc-current").value, newPassword: $("#pc-new").value })
    .catch(() => UNREACHABLE);
  if (result.ok) { e.target.reset(); error.textContent = ""; showApp(); flash("Password changed.", true); return; }
  if (result.code === "UNAUTHORIZED") { showSignIn("Your session ended. Please sign in again."); return; }
  error.textContent = result.code === "INVALID_CREDENTIALS" ? "The current password is wrong." : result.message;
});

// ---------- forgot password: email -> code -> new password (checked by the licence server) ----------

function forgotStep(step) {
  ["email", "code", "reset"].forEach((name) => { $(`#forgot-${name}-form`).hidden = name !== step; });
}

$("#forgot-link").addEventListener("click", (e) => {
  e.preventDefault();
  showScreen("forgot");
  forgotStep("email");
  $("#forgot-error").textContent = "";
  $("#forgot-email").focus();
});
$("#forgot-back").addEventListener("click", (e) => { e.preventDefault(); showSignIn(); });

async function sendForgotCode() {
  const error = $("#forgot-error");
  error.textContent = "Sending…";
  const result = await postJson("/session/forgot/request", { email: $("#forgot-email").value.trim() }).catch(() => UNREACHABLE);
  if (!result.ok) { error.textContent = result.message; return; }
  error.textContent = "";
  const wait = result.data.resend_wait;
  $("#forgot-code-intro").textContent = `${result.data.message || "If an account uses this email, a code has been sent."}`
    + `${wait ? ` You can ask for a new code in ${wait} seconds.` : ""}`;
  forgotStep("code");
  $("#forgot-code").value = "";
  $("#forgot-code").focus();
}

$("#forgot-email-form").addEventListener("submit", (e) => { e.preventDefault(); sendForgotCode(); });
$("#forgot-resend").addEventListener("click", () => sendForgotCode());

let resetToken = null;
$("#forgot-code-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const error = $("#forgot-error");
  error.textContent = "Checking…";
  const result = await postJson("/session/forgot/verify", { email: $("#forgot-email").value.trim(), code: $("#forgot-code").value.trim() })
    .catch(() => UNREACHABLE);
  if (!result.ok) {
    error.textContent = result.message;
    if (!result.retryable) $("#forgot-code").value = "";
    return;
  }
  resetToken = result.data.reset_token;
  error.textContent = "";
  forgotStep("reset");
  $("#forgot-new").focus();
});

$("#forgot-reset-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const error = $("#forgot-error");
  if ($("#forgot-new").value !== $("#forgot-confirm").value) { error.textContent = "The two passwords are different."; return; }
  const result = await postJson("/session/forgot/reset", {
    email: $("#forgot-email").value.trim(), resetToken, newPassword: $("#forgot-new").value, confirmPassword: $("#forgot-confirm").value,
  }).catch(() => UNREACHABLE);
  if (!result.ok) {
    error.textContent = result.message;
    if (!result.retryable) forgotStep("email");
    return;
  }
  e.target.reset();
  showSignIn("Password changed. Sign in with your new password.", true);
});

async function start() {
  try {
    const session = await (await fetch("/v1/session", { credentials: "same-origin" })).json();
    if (session.licensed) {
      setUpLicensedSignIn(session);
      if (session.loggedIn && session.licence.mustChangePassword) showPasswordChange(true);
      else if (session.loggedIn) showApp();
      else showSignIn(["admin_deleted", "invalid_session"].includes(session.licence.reason) ? session.licence.message : "");
      return;
    }
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
