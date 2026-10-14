"""App log (Log tab), Terminal path cleaning and readable MT5 connection errors."""

from stakbridge.workers.backends import connect_error, normalize_terminal_path, terminal_path_problem

from conftest import wait_until


def logs(bridge, level="info"):
    return bridge.get(f"/logs?level={level}").json()["logs"]


def test_connects_and_problems_are_logged_once(bridge):
    bridge.add_account("acct_x", 1111)
    bridge.wait_connected("acct_x")
    wait_until(lambda: any(e["account"] == "acct_x" and "connected" in e["message"] for e in logs(bridge)))

    bridge.set_offline("acct_x")
    wait_until(lambda: bridge.status("acct_x") == "disconnected")
    wait_until(lambda: any(e["level"] == "warning" and "disconnected" in e["message"] for e in logs(bridge)))
    # The worker keeps retrying every few seconds, but the same problem is written only once.
    assert sum("disconnected" in e["message"] for e in logs(bridge)) == 1


def test_level_filter_and_clear(bridge):
    bridge.post("/kill")  # a warning
    bridge.add_account("acct_x", 1111)
    bridge.wait_connected("acct_x")
    wait_until(lambda: any(e["level"] == "info" for e in logs(bridge)))
    wait_until(lambda: any("kill switch" in e["message"] for e in logs(bridge, "warning")))
    assert all(e["level"] in ("warning", "error", "critical") for e in logs(bridge, "warning"))

    assert bridge.delete("/logs").status_code == 200
    wait_until(lambda: [e["message"] for e in logs(bridge)] == ["log cleared"])


def test_rejected_trade_is_logged(bridge):
    bridge.add_account("acct_x", 1111)
    bridge.wait_connected("acct_x")
    bridge.add_strategy("s1", max_volume=5, accounts=["acct_x"])
    bridge.trade("t-1", strategy="s1", volume=0.001)  # below the broker minimum of 0.01
    wait_until(lambda: any("VOLUME_BELOW_BROKER_MIN" in e["message"] and e["account"] == "acct_x"
                           for e in logs(bridge, "warning")))


def test_folder_and_quoted_terminal_paths_are_cleaned(tmp_path):
    (tmp_path / "terminal64.exe").write_bytes(b"")
    exe = str(tmp_path / "terminal64.exe")
    assert normalize_terminal_path(str(tmp_path)) == exe          # folder -> program inside it
    assert normalize_terminal_path(f'  "{exe}" ') == exe           # "Copy as path" adds quotes
    assert normalize_terminal_path("") == ""
    assert terminal_path_problem(exe) is None
    assert terminal_path_problem("") is None                       # empty: the default MT5
    assert "nothing found" in terminal_path_problem(str(tmp_path / "missing.exe"))
    (tmp_path / "notes.txt").write_text("x")
    assert "not a program" in terminal_path_problem(str(tmp_path / "notes.txt"))


def test_mt5_connection_errors_are_explained():
    assert connect_error(-10003, "IPC initialize failed").startswith("TERMINAL_NOT_STARTED: MetaTrader could not be started")
    assert connect_error(-6, "Terminal: Authorization failed").startswith("LOGIN_FAILED: wrong login, password or server")
    assert connect_error(-1, "something new") == "LOGIN_FAILED: -1 something new"
