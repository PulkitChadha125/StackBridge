"""The desktop app (the EXE): StakBridge's web server plus its own window, no browser needed.

Closing the window stops copying, the broker workers and the server, then the program ends.
When the window cannot open (no .NET Framework 4.7.2+ or WebView2 Runtime), the page opens in the web
browser instead and a message box stands in for the window: OK on it stops StakBridge.
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


NETFX_472 = 461808  # .NET Framework 4.7.2 "Release" number; pythonnet (the window's .NET part) needs at least this
NETFX_URL = "https://dotnet.microsoft.com/download/dotnet-framework/net48"
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"  # Microsoft's WebView2 Runtime installer


def _netfx_release() -> int:
    """The installed .NET Framework 4.x "Release" number, 0 when there is none."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full") as key:
            return int(winreg.QueryValueEx(key, "Release")[0])
    except (OSError, ValueError):
        return 0


def _window_problem() -> tuple[str, str] | None:
    """Why StakBridge's own window cannot open on this PC (what is missing, its download page), or None.

    The window needs the .NET Framework 4.7.2+ and the Microsoft Edge WebView2 Runtime. Some Windows Server
    and VPS images lack one of them, or have a broken .NET: loading it then fails with a RuntimeError
    ("Failed to create a .NET runtime"), which pywebview does not catch."""
    if sys.platform != "win32":
        return None
    if _netfx_release() < NETFX_472:
        return "Microsoft .NET Framework 4.8", NETFX_URL
    try:
        from webview.platforms.winforms import _is_chromium  # loads .NET (pythonnet)
    except Exception:
        log.exception("the window's .NET part could not load")
        return "a working Microsoft .NET Framework 4.8 (repair or reinstall it)", NETFX_URL
    if not _is_chromium():  # pywebview would fall back to Internet Explorer, which shows only the header
        return "the Microsoft Edge WebView2 Runtime", WEBVIEW2_URL
    return None


def _run_in_browser(url: str, missing: str, download: str) -> None:
    """Shows StakBridge in the PC's web browser instead of its own window; OK on the message stops it."""
    import webbrowser

    log.error("own window cannot open (needs %s): using the web browser", missing)
    webbrowser.open(download)
    webbrowser.open(url)
    _message_box(f"StakBridge's own window cannot open on this computer: it needs {missing}.\n\n"
                 "StakBridge is running and opened in your web browser instead. Your browser also shows the "
                 "download page: install it and open StakBridge again to get its own window back.\n\n"
                 "Keep this message open while you use StakBridge. Clicking OK stops StakBridge and copying.")


_UNWATCHED = ("AddWebResourceRequestedFilter", "WebResourceRequested", "WebResourceResponseReceived")


class _Ignored:
    """Stands in for a WebView2 call or event pywebview should not use: calls and `+= handler` do nothing."""

    def __call__(self, *args, **kwargs):
        return None

    def __iadd__(self, handler):
        return self


class _WithoutRequestEvents:
    """pywebview's WebView2 control (sender.CoreWebView2) without the request/response events; all else passes."""

    def __init__(self, core):
        object.__setattr__(self, "_core", core)

    def __getattr__(self, name):
        return _Ignored() if name in _UNWATCHED else getattr(self._core, name)

    def __setattr__(self, name, value):
        if name not in _UNWATCHED:
            setattr(self._core, name, value)


class _SenderWithoutRequestEvents:
    def __init__(self, sender):
        self._sender = sender
        self.CoreWebView2 = _WithoutRequestEvents(sender.CoreWebView2)

    def __getattr__(self, name):
        return getattr(self._sender, name)


def _stop_request_events() -> None:
    """Keeps pywebview from watching every request and response of the page (its request_sent and
    response_received events, which StakBridge does not use). With them WebView2 hands each answer of the
    1-second refresh to the host and the "WebView2: StakBridge" process grows by ~1 GB a day although the page
    itself is flat (measured 2026-10-31, see RAM_BUG_FIX.md). Without them it stays at ~40 MB."""
    try:
        from webview.platforms.edgechromium import EdgeChrome
    except Exception:
        return  # not the WebView2 window: nothing to change
    if getattr(EdgeChrome.on_webview_ready, "_stakbridge", False):
        return
    original = EdgeChrome.on_webview_ready

    def on_webview_ready(self, sender, args):
        if not args.IsSuccess:
            return original(self, sender, args)
        try:
            wrapped = _SenderWithoutRequestEvents(sender)
        except Exception:
            log.exception("could not switch off the window's request events")
            return original(self, sender, args)
        return original(self, wrapped, args)

    on_webview_ready._stakbridge = True
    EdgeChrome.on_webview_ready = on_webview_ready


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

    def launch_url() -> str:
        return f"http://127.0.0.1:{port}{app.state.make_launch_link()}"

    problem = _window_problem()
    if problem is None:
        try:
            import webview

            webview.settings["ALLOW_DOWNLOADS"] = True
            _stop_request_events()
            webview.create_window(APP_NAME, launch_url(),
                                  width=1440, height=900, min_size=(900, 560), text_select=True)
            webview.start()  # returns when the window is closed
        except Exception:
            log.exception("the window failed")
            problem = "a working Microsoft .NET Framework 4.8 (repair or reinstall it)", NETFX_URL
    if problem is not None:
        _run_in_browser(launch_url(), *problem)
    log.info("stopping")
    server.should_exit = True  # the server's shutdown stops the copier, workers and licence checks
    thread.join(timeout=30)
    logging.shutdown()
    os._exit(0)  # a stuck broker call must not keep a windowless StakBridge running


if __name__ == "__main__":
    sys.exit(run())
