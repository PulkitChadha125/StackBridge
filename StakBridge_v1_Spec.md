# StakBridge v1: Product and Feature Spec

14 October 2026. StakBridge is a working name, not yet confirmed.

> **In one line:** send one REST request with a strategy tag, and the trade is placed at the same moment on every connected MT5 account assigned to that tag. Accounts that are not assigned to the tag never receive it.

This builds on the architecture doc: one gateway, one worker and one MT5 terminal per account. The new parts are the strategy layer, the routing rules, the trade API and a small web UI.

## 1. What v1 does

- **Connects many MT5 terminals at once.** Each account runs in its own terminal and stays connected in the background.
- **Places trades on several accounts at once.** One REST call fans out to all eligible accounts in parallel.
- **Strategy tags decide who trades.** A strategy has a tag. Only accounts assigned to that tag receive its trades. The tag comes in the request payload.
- **Minimal web UI.** Three tabs: Accounts, Strategies, Trades. No charts, no clutter.

Out of scope for v1: MT4, copy trading from a master account, a risk engine, billing, user signup, and live broker accounts. Live accounts need your explicit approval first.

## 2. How strategy-tag routing works

1. Each strategy has a unique tag, for example `gold_scalper`.
2. Each account can be assigned to one or more strategies (assumption, see section 8).
3. Every trade request must carry a `strategy` tag in its payload.
4. The app sends the trade to every account that is assigned to that tag and passes the checks below.
5. Accounts that are not assigned to the tag are never contacted.
6. A missing or unknown tag rejects the whole request. The app never falls back to "all accounts".
7. One account failing does not stop the others. Every account gets its own result.

### Checks run for each request

| Check | If it fails |
| --- | --- |
| Account is assigned to the strategy tag | Account is not part of the trade and is not listed |
| Strategy is Active | Whole request rejected, code `STRATEGY_PAUSED` |
| Volume is within the strategy max volume | Whole request rejected, code `VOLUME_TOO_LARGE` |
| Account is enabled and its terminal is Connected | Account skipped, reason `DISCONNECTED` |
| Account trading switch is on | Account skipped, reason `TRADING_OFF` |
| Broker accepts the order | Account marked rejected, with the broker reason |

## 3. REST API

All calls use `Authorization: Bearer <token>` over HTTPS. Names are proposals and can change when coding starts.

| Method and path | Purpose |
| --- | --- |
| `POST /v1/trade` | Open or close trades for a strategy tag on all its assigned accounts |
| `GET /v1/trades/{requestId}` | Stored result of one request, per account |
| `GET, POST /v1/accounts` | List accounts, add an account |
| `PATCH, DELETE /v1/accounts/{id}` | Edit, switch trading on or off, remove |
| `GET, POST /v1/strategies` | List strategies, create a strategy tag |
| `PATCH /v1/strategies/{tag}` | Change active flag, max volume |
| `PUT /v1/strategies/{tag}/accounts` | Set which accounts are assigned to the tag |
| `POST /v1/kill` | Stop all trading at once |
| `GET /v1/health` | Gateway and per-account status |

### Open a trade

```json
POST /v1/trade
{
  "requestId": "sig-0001",
  "strategy":  "gold_scalper",
  "action":    "open",
  "symbol":    "XAUUSD",
  "side":      "buy",
  "type":      "market",
  "volume":    0.10,
  "sl":        2310.50,
  "tp":        2340.00,
  "comment":   "gs-0001"
}
```

| Field | Needed | Notes |
| --- | --- | --- |
| `requestId` | Yes | Your own unique id. Sending it twice never places the order twice. |
| `strategy` | Yes | Strategy tag. Must exist and be active. |
| `action` | Yes | `open` or `close`. |
| `symbol` | Yes | Base symbol such as XAUUSD. The app adds each account's own suffix. |
| `side`, `type`, `volume` | On open | buy or sell; market, limit or stop; lots. Same volume on every account in v1. |
| `price` | Limit and stop | Entry price for pending orders. |
| `sl`, `tp` | No | Stop loss and take profit price levels. |
| `comment` | No | Short text kept on the order. |

**Close by strategy:** with `"action": "close"` the app closes positions that belong to that strategy on its assigned accounts. If symbol is left out, all of the strategy's positions close. Positions are matched through a magic number that each strategy gets when it is created.

### Response (sample values, for shape only)

```json
{
  "requestId": "sig-0001",
  "strategy":  "gold_scalper",
  "results": [
    {"accountId": "acct_a", "status": "filled",
     "ticket": 123456, "price": 2321.45},
    {"accountId": "acct_b", "status": "rejected",
     "reason": "INVALID_VOLUME"},
    {"accountId": "acct_c", "status": "skipped",
     "reason": "DISCONNECTED"}
  ]
}
```

| Code | Meaning |
| --- | --- |
| 200 | Request processed. Read each account result, some may be rejected or skipped. |
| 400 | Bad payload, unknown strategy tag, or volume above the strategy max. |
| 401 | Missing or invalid token. |
| 409 | Strategy paused, or no eligible account for this tag. |
| 429 | Rate limit reached. |

### Placing trades at the same time

- The gateway sends the command to all eligible workers together, not one after another.
- Each account has its own worker and terminal, so a slow or failed account never blocks the rest.
- The response is returned when every account has replied or a timeout passes. The timeout value is set after real measurements in the test phase.
- An account that does not reply in time is reported as `UNKNOWN`. The app checks the terminal before anything is ever resent, so a trade is never sent twice.

## 4. Minimal UI

One page, three tabs, tables instead of cards, one accent colour, a status dot for connection state. The Stop all button is always visible in the top right.

### Accounts tab (layout only, sample data)

```text
 Accounts   Strategies   Trades                                [ Stop all ]
 ---------------------------------------------------------------------------
 Name    Login    Server          Status          Strategies            Trading
 Acct A  5012345  Demo-Server-1   (o) Connected   gold_scalper          [ON ]
 Acct B  5012346  Demo-Server-1   (o) Connected   gold_scalper,         [ON ]
                                                  swing_eur
 Acct C  7788120  Demo-Server-2   (x) Disconnected swing_eur            [OFF]

 [ + Add account ]
```

### Strategies tab (layout only, sample data)

```text
 Accounts   Strategies   Trades                                [ Stop all ]
 ---------------------------------------------------------------------------
 > gold_scalper  | gold_scalper                                  Active [ON]
   swing_eur     | Magic number: 1001      Max volume per trade: 1.00 lot
   + New strategy|
                 | Accounts that receive this strategy
                 |   [x] Acct A   (o) Connected
                 |   [x] Acct B   (o) Connected
                 |   [ ] Acct C   (x) Disconnected
                 |
                 | Last request: sig-0001, 2 of 2 filled
```

Pick a strategy on the left, tick the accounts that should receive it on the right. The change applies to the next request.

| Tab | What it holds |
| --- | --- |
| Accounts | Add, edit and remove accounts. Shows connection status and the strategies of each account. Trading switch per account. |
| Strategies | Create a strategy tag, set its max volume, pause it, and choose the accounts that receive it. |
| Trades | Log of requests: time, request id, strategy, symbol, side, volume, and how many accounts filled. Click a row for the result of each account. |

## 5. Data model

| Table | Main fields |
| --- | --- |
| `accounts` | id, name, MT5 login, server, encrypted password, symbol suffix, enabled, trading on, status |
| `strategies` | tag (unique), magic number, active, max volume |
| `account_strategies` | account id, strategy tag (an account can have several tags) |
| `trade_requests` | request id (unique), strategy tag, action, full payload, received time |
| `trade_results` | request id, account id, status, reason, ticket, fill price, time |

## 6. Safety rules

- Demo accounts only until you approve live use.
- Every account has a trading switch, off by default.
- An unknown or missing strategy tag rejects the request. It never broadcasts.
- Each strategy has a max volume per trade, checked before anything is sent.
- The Stop all button and `POST /v1/kill` block all new trades.
- Broker passwords are encrypted in the database and never appear in logs or API responses.
- API token required on every call. Tokens are stored as hashes.

## 7. Acceptance tests

All on demo accounts. v1 is done when each of these passes.

| # | Test | Pass when |
| --- | --- | --- |
| 1 | Connect 3 demo terminals | All three show Connected in the UI |
| 2 | Send a trade for a tag with 2 of 3 accounts assigned | The trade appears on exactly those 2 accounts. The third has nothing. |
| 3 | Send a trade with an unknown tag | 400 returned and no order placed anywhere |
| 4 | Disconnect one assigned terminal, then send a trade | Other accounts fill. Result shows the lost one as skipped, DISCONNECTED. |
| 5 | Send the same requestId twice | Only one set of orders exists |
| 6 | Send a volume above the strategy max | 400 returned and no order placed |
| 7 | Press Stop all, then send a trade | Request is refused and nothing is placed |
| 8 | Change an account assignment in the UI | The next request follows the new assignment |
| 9 | Close by strategy tag | Only that strategy's positions close, other strategies stay open |

## 8. Open decisions

Each line is what I assumed. Tell me if any is wrong.

| Question | Assumption in this spec |
| --- | --- |
| Can one account belong to several strategies? | Yes. It receives trades from each of its tags. |
| Same volume on every account, or a multiplier per account? | Same volume in v1. A per-account multiplier is a small later addition. |
| Brokers name symbols differently (XAUUSD vs XAUUSD.m) | One symbol suffix per account. |
| Close action: by strategy tag using a magic number? | Yes, as described in section 3. |
| Does the UI need user logins? | No. One owner, protected by the API token or a single password. |
| UI technology | Plain HTML and JavaScript served by the gateway, no build step. Next.js later if needed. |
| Is the "strategy tab" a tab for managing strategies? | Yes, the Strategies tab above. Tell me if you meant one tab per strategy. |
