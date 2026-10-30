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
