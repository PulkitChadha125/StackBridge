"""The Trades tab as text: each master trade with every client's result. Added to the logs the
SuperAdmin downloads (licence log requests), so they show which clients got a trade and why one failed.

A client line that needs a look (refused, not known, skipped, failed close...) starts with PROBLEM_MARK,
so the SuperAdmin's "errors only" download can keep just those trades.
"""

from datetime import date, datetime, time, timedelta, timezone

HEADER = "==== Trades: each master trade and what every client got ===="
TRADE_START = "Master trade"
PROBLEM_MARK = "  !! "
OK_MARK = "  -> "
PROBLEM_STATUSES = ("rejected", "unknown", "skipped")
# Reasons that only explain what happened, not a problem.
INFO_REASONS = ("ALREADY_CLOSED_ON_CLIENT", "ALREADY_GONE_ON_CLIENT", "FOUND_AFTER_LOST_ANSWER")
MAX_ORDERS = 5000


def _utc_iso(day: date) -> str:
    """Local midnight of this day, as the UTC time stored in the database."""
    return datetime.combine(day, time.min).astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _local(stamp: str | None) -> str:
    if not stamp:
        return ""
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except ValueError:
        return stamp


def is_problem(copy: dict) -> bool:
    reason = copy.get("reason")
    return copy["status"] in PROBLEM_STATUSES or bool(reason and reason not in INFO_REASONS)


def _client_line(copy: dict, names: dict) -> str:
    client = copy["client_id"]
    name = names.get(client)
    parts = [f"{name} ({client})" if name and name != client else client, copy["status"].upper()]
    if copy.get("volume") is not None:
        parts.append(f"{copy['volume']} lots {copy['symbol']}")
    if copy.get("client_ticket"):
        parts.append(f"ticket {copy['client_ticket']}")
    if copy.get("open_price") is not None:
        took = f" in {copy['open_ms']} ms" if copy.get("open_ms") is not None else ""
        parts.append(f"opened at {copy['open_price']}{took}")
    if copy.get("close_price") is not None:
        took = f" in {copy['close_ms']} ms" if copy.get("close_ms") is not None else ""
        parts.append(f"closed at {copy['close_price']}{took}")
    if copy.get("reason"):
        parts.append(f"reason: {copy['reason']}")
    return (PROBLEM_MARK if is_problem(copy) else OK_MARK) + " - ".join(parts)


def report(db, first: date, last: date) -> list[str]:
    """Lines for the master trades opened on the local days first..last (inclusive), oldest first."""
    orders = db.list_master_orders(limit=MAX_ORDERS, since=_utc_iso(first), until=_utc_iso(last + timedelta(days=1)))
    if not orders:
        return ["", HEADER, "No master trades on these dates."]
    names = {a["id"]: a["name"] for a in db.list_accounts()}
    lines = ["", HEADER, "Times are this PC's local time. Lines starting with !! need a look."]
    for order in reversed(orders):
        lines += ["", f"{TRADE_START} #{order['ticket']} on {order['master_id']}: {order['side']} {order['order_type']} "
                      f"{order['volume']} {order['symbol']} - now {order['status']} (opened {_local(order['opened_at'])})"]
        lines += [f"     {_local(e['at'])} {e['text']}" for e in order["events"]]
        lines += [_client_line(copy, names) for copy in order["copies"]] or ["  -- not sent to any client"]
    return lines
