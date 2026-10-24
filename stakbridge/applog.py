"""App log: StakBridge's own events (info, warnings, errors) kept in the database for the Log tab.

Records go through a queue and are written by a background thread, so logging never
slows down an order. Pass extra={"account": "<id>"} to tie an entry to an account.
"""

import csv
import io
import logging
import queue
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener

from .db import Database

LOGGER = "stakbridge"


class _DatabaseHandler(logging.Handler):
    def __init__(self, db: Database):
        super().__init__(logging.INFO)
        self.db = db

    def emit(self, record: logging.LogRecord) -> None:
        try:
            source = record.name.removeprefix(f"{LOGGER}.") if record.name != LOGGER else "app"
            # QueueHandler already merged the arguments and any traceback into msg.
            self.db.add_log(record.levelname.lower(), source, getattr(record, "account", None), str(record.msg))
        except Exception:
            self.handleError(record)


class AppLog:
    def __init__(self, db: Database):
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._handler = QueueHandler(self._queue)
        self._handler.setLevel(logging.INFO)
        self._listener = QueueListener(self._queue, _DatabaseHandler(db), respect_handler_level=True)

    def start(self) -> None:
        logger = logging.getLogger(LOGGER)
        if logger.getEffectiveLevel() > logging.INFO:
            logger.setLevel(logging.INFO)  # keep info entries even when the console shows only warnings
        logger.addHandler(self._handler)
        self._listener.start()

    def stop(self) -> None:
        logging.getLogger(LOGGER).removeHandler(self._handler)
        self._listener.stop()  # writes what is still queued


# ---- downloads from the Log tab ----

CSV_COLUMNS = ("Time (this PC)", "Time (UTC)", "Level", "Account", "Account name", "Part", "Message")


def _local_time(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except ValueError:
        return stamp


def _cell(value) -> str:
    """Text Excel shows as typed: a message starting with = + - or @ would otherwise be run as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def export_csv(entries: list[dict], names: dict[str, str]) -> str:
    """Log entries as CSV for Excel: a byte order mark so it reads the text as UTF-8."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for e in entries:
        account = e.get("account") or ""
        writer.writerow([_cell(v) for v in (_local_time(e["at"]), e["at"], e["level"], account,
                                             names.get(account, ""), e["source"], e["message"])])
    return "\ufeff" + out.getvalue()


def export_txt(entries: list[dict], names: dict[str, str]) -> str:
    lines = [f"StakBridge log - {len(entries)} entries, oldest first, times are this PC's local time", ""]
    for e in entries:
        account = e.get("account") or ""
        who = f" {account} ({names[account]})" if account in names else f" {account}" if account else ""
        lines.append(f"{_local_time(e['at'])} {e['level'].upper():8} {e['source']}{who}: {e['message']}")
    return "\r\n".join(lines) + "\r\n"
