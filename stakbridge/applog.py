"""App log: StakBridge's own events (info, warnings, errors) kept in the database for the Log tab.

Records go through a queue and are written by a background thread, so logging never
slows down an order. Pass extra={"account": "<id>"} to tie an entry to an account.
"""

import logging
import queue
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
