"""The EXE's window check: a PC without a working .NET Framework or WebView2 gets the browser, not a crash."""

import builtins
import sys

import pytest

from stakbridge import desktop

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the window check is for Windows")


def test_old_or_missing_dotnet_framework_is_reported_before_loading_it(monkeypatch):
    monkeypatch.setattr(desktop, "_netfx_release", lambda: 394802)  # 4.6.2
    assert desktop._window_problem() == ("Microsoft .NET Framework 4.8", desktop.NETFX_URL)


def test_dotnet_that_fails_to_load_is_reported_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(desktop, "_netfx_release", lambda: 528040)  # 4.8
    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "webview.platforms.winforms":
            raise RuntimeError("Failed to create a .NET runtime (coreclr) using the parameters {}.")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "webview.platforms.winforms", raising=False)
    monkeypatch.setattr(builtins, "__import__", failing_import)
    missing, download = desktop._window_problem()
    assert "NET Framework" in missing and download == desktop.NETFX_URL


def test_this_pc_reads_its_dotnet_version():
    assert desktop._netfx_release() >= desktop.NETFX_472  # Windows 10/11 ship with 4.8


class _Event:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def __iadd__(self, handler):
        self.log.append(self.name)
        return self

    def __getattr__(self, method):  # e.g. CookieManager.DeleteAllCookies()
        return lambda *a: None

    def __call__(self, *args):  # a method of the control, e.g. NavigateToString()
        return None


class _Core:
    """Records what pywebview subscribes to on the WebView2 control."""

    def __init__(self):
        object.__setattr__(self, "log", [])
        object.__setattr__(self, "Settings", type("S", (), {})())

    def __getattr__(self, name):
        if name == "AddWebResourceRequestedFilter":
            return lambda *a: self.log.append(name)
        return _Event(self.log, name)

    def __setattr__(self, name, value):
        pass  # what `core.Event += handler` writes back


def test_window_does_not_watch_every_request_but_keeps_its_other_events(monkeypatch):
    """pywebview's request/response events made WebView2 grow by ~1 GB a day (RAM_BUG_FIX.md, 2026-10-31)."""
    edgechromium = pytest.importorskip("webview.platforms.edgechromium")
    monkeypatch.setattr(edgechromium.EdgeChrome, "on_webview_ready", edgechromium.EdgeChrome.on_webview_ready)
    core = _Core()
    sender = type("Sender", (), {"CoreWebView2": core})()
    browser = edgechromium.EdgeChrome.__new__(edgechromium.EdgeChrome)  # without starting a real WebView2
    browser.webview = sender
    browser.pywebview_window = type("W", (), {"real_url": None, "html": None})()
    args = type("A", (), {"IsSuccess": True})()

    desktop._stop_request_events()
    desktop._stop_request_events()  # twice is harmless
    edgechromium.EdgeChrome.on_webview_ready(browser, sender, args)

    assert not {"AddWebResourceRequestedFilter", "WebResourceRequested", "WebResourceResponseReceived"} & set(core.log)
    assert {"NewWindowRequested", "DownloadStarting", "SourceChanged"} <= set(core.log)
