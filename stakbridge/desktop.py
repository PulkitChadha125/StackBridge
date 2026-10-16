"""The desktop app (the EXE): StakBridge's web server plus its own window, no browser needed.

Closing the window stops copying, the broker workers and the server, then the program ends.
The window opens already signed in when this PC has a saved licence sign-in (one-time link).
Only one copy can run at a time: a second one shows a message and closes.

STAKBRIDGE_HOST=0.0.0.0 lets other PCs reach the page too (for a VPS); the port is
STAKBRIDGE_PORT (default 8000).
"""

import logging
import os
import socket
import sys
import threading
import time

from . import __version__, logfiles
from .licensing import paths

APP_NAME = "StakBridge"
log = logging.getLogger("stakbridge.desktop")


def _message_box(text: str, warning: bool = True) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x30 if warning else 0x40)
    except Exception:  # not Windows
        print(text)


def _port_in_use(port: int) -> bool:
    """Windows lets a second program bind a port the first one still listens on, so ask instead."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def run() -> None:
    host = os.environ.get("STAKBRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("STAKBRIDGE_PORT", "8000"))
    device = paths.device_root()
    logfiles.install(device / "logs", capture_output=paths.is_frozen())
    log.info("StakBridge %s desktop starting", __version__)

    if paths.is_frozen() and not paths.server_url():
        _message_box("This copy of StakBridge has no licence server in it.\n\n"
                     "Download StakBridge again from your provider.")
        return
    if _port_in_use(port):
        _message_box(f"StakBridge is already running on this computer (port {port}).\n\n"
                     "Close the other StakBridge window first. If none is open, end StakBridge in Task Manager "
                     "(Details tab) and open it again.")
        return

    import uvicorn

    from .api import create_app
    from .config import load_settings

    # With a licence the data lives in the signed-in Admin's own folder; this is only the default.
    settings = load_settings(data_dir=device / "data")
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, name="web-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        _message_box("StakBridge could not start. The details are in the log folder:\n" + str(device / "logs"))
        os._exit(1)

    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True
    window = webview.create_window(APP_NAME, f"http://127.0.0.1:{port}{app.state.make_launch_link()}",
                                   width=1440, height=900, min_size=(900, 560), text_select=True)

    def closed() -> None:
        log.info("window closed: stopping")
        server.should_exit = True  # the server's shutdown stops the copier, workers and licence checks

    window.events.closed += closed
    webview.start()
    thread.join(timeout=30)
    logging.shutdown()
    os._exit(0)  # a stuck broker call must not keep a windowless StakBridge running


if __name__ == "__main__":
    sys.exit(run())
