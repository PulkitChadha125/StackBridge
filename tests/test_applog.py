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


def test_rejected_copy_is_logged(bridge):
    bridge.add_account("acct_m", 1111)
    bridge.add_account("acct_x", 2222)
    bridge.wait_connected("acct_m", "acct_x")
    assert bridge.put("/copier", {"masterId": "acct_m", "clients": [
        {"accountId": "acct_x", "multiplier": 0.01}]}).status_code == 200
    wait_until(lambda: bridge.get("/copier").json()["watching"])
    bridge.open_by_hand("acct_m", volume=0.1)  # x0.01 = 0.001 lot, below the broker minimum of 0.01
    wait_until(lambda: any("VOLUME_BELOW_BROKER_MIN" in e["message"] and e["account"] == "acct_x"
                           for e in logs(bridge, "warning")))


def test_folder_and_quoted_terminal_paths_are_cleaned(tmp_path):
    (tmp_path / "terminal64.exe").write_bytes(b"")
    exe = str(tmp_path / "terminal64.exe")
    assert normalize_terminal_path(str(tmp_path)) == exe          # folder -> program inside it
    assert normalize_terminal_path(f'  "{exe}" ') == exe           # "Copy as path" adds quotes
    assert normalize_terminal_path(f"{exe}.") == exe                # a full stop pasted from a sentence
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


def test_log_downloads_as_csv_and_text(bridge):
    import csv
    import io

    bridge.add_account("acct_x", 1111)
    bridge.wait_connected("acct_x")
    bridge.post("/kill")  # a warning
    wait_until(lambda: any("kill switch" in e["message"] for e in logs(bridge, "warning")))

    res = bridge.client.get("/v1/logs/download?format=csv&level=info")
    assert res.status_code == 200
    assert 'attachment; filename="stakbridge-log_' in res.headers["content-disposition"]
    assert res.headers["content-disposition"].endswith('.csv"')
    assert res.text.startswith("\ufeff")  # Excel reads it as UTF-8
    rows = list(csv.reader(io.StringIO(res.text.lstrip("\ufeff"))))
    assert rows[0] == ["Time (this PC)", "Time (UTC)", "Level", "Account", "Account name", "Part", "Message"]
    connected = next(r for r in rows[1:] if r[3] == "acct_x" and "connected" in r[6])
    name = next(a["name"] for a in bridge.get("/accounts").json()["accounts"] if a["id"] == "acct_x")
    assert connected[4] == name and connected[2] == "info"  # the account's name is filled in
    times = [r[1] for r in rows[1:]]
    assert times == sorted(times)  # oldest first

    warnings = list(csv.reader(io.StringIO(bridge.client.get("/v1/logs/download?level=warning").text.lstrip("\ufeff"))))
    assert len(warnings) > 1 and all(r[2] in ("warning", "error", "critical") for r in warnings[1:])

    text = bridge.client.get("/v1/logs/download?format=txt").text
    assert text.startswith("StakBridge log - ") and "WARNING" in text and "kill switch" in text
    assert bridge.client.get("/v1/logs/download?format=pdf").status_code == 400


def test_csv_cells_are_never_excel_formulas():
    from stakbridge.applog import export_csv

    text = export_csv([{"at": "2026-10-24T10:00:00.000+00:00", "level": "info", "account": None, "source": "app",
                        "message": "=HYPERLINK(\"x\")"}], {})
    assert "'=HYPERLINK" in text
