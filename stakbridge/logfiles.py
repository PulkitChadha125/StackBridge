"""Daily log files (logs\\stakbridge-YYYY-MM-DD.log), kept for 30 days.

Everything StakBridge logs goes here as well as to the Log tab. The EXE has no console window,
so its print output and errors are sent here too. The SuperAdmin can ask for a range of these
days (licence log requests).
"""

import logging
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

KEEP_DAYS = 30
PREFIX = "stakbridge-"
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _file_for(folder: Path, day: date) -> Path:
    return folder / f"{PREFIX}{day.isoformat()}.log"


class DailyFileHandler(logging.Handler):
    """Writes each record to the file of its own day, opening a new file after midnight."""

    def __init__(self, folder: Path):
        super().__init__(logging.INFO)
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.setFormatter(logging.Formatter(FORMAT))
        self._day: date | None = None
        self._stream = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            self.write(line + "\n")
        except Exception:
            self.handleError(record)

    def write(self, text: str) -> None:
        with self.lock:
            today = date.today()
            if today != self._day:
                if self._stream:
                    self._stream.close()
                self._stream = open(_file_for(self.folder, today), "a", encoding="utf-8")
                self._day = today
                threading.Thread(target=delete_old, args=(self.folder,), daemon=True).start()
            self._stream.write(text)
            self._stream.flush()

    def close(self) -> None:
        with self.lock:
            if self._stream:
                self._stream.close()
                self._stream = None
        super().close()


class _ToLog:
    """Stands in for stdout/stderr in the windowed EXE, so prints and tracebacks are kept."""

    def __init__(self, handler: DailyFileHandler, label: str):
        self.handler, self.label = handler, label

    def write(self, text: str) -> int:
        if text.strip():
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
            self.handler.write("".join(f"{stamp} {self.label} {line}\n" for line in text.rstrip("\n").splitlines()))
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def install(folder: Path, capture_output: bool = False) -> DailyFileHandler:
    handler = DailyFileHandler(folder)
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    if capture_output:
        sys.stdout = _ToLog(handler, "OUTPUT")
        sys.stderr = _ToLog(handler, "ERROR")
    return handler


def delete_old(folder: Path) -> None:
    cutoff = date.today() - timedelta(days=KEEP_DAYS)
    for path in folder.glob(f"{PREFIX}*.log"):
        try:
            if date.fromisoformat(path.stem.removeprefix(PREFIX)) < cutoff:
                path.unlink()
        except (ValueError, OSError):
            pass


def read_range(folder: Path, first: date, last: date) -> Iterator[str]:
    """The lines of the days first..last (inclusive), oldest first."""
    day = first
    while day <= last:
        try:
            with open(_file_for(folder, day), encoding="utf-8", errors="replace") as file:
                for line in file:
                    yield line.rstrip("\n")
        except OSError:
            pass
        day += timedelta(days=1)
