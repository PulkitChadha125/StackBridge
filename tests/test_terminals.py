"""The Terminal path drop-down: MetaTrader 5 terminals found on this PC."""

from stakbridge import terminals


def install(folder):
    folder.mkdir(parents=True)
    (folder / "terminal64.exe").write_bytes(b"MZ")
    return folder / "terminal64.exe"


def fake_pc(tmp_path, monkeypatch):
    """Two MT5s under Program Files, one elsewhere that only %APPDATA% knows about, and an MT4."""
    program_files = tmp_path / "Program Files"
    a = install(program_files / "MetaTrader 5")
    b = install(program_files / "Broker MetaTrader 5")
    c = install(tmp_path / "Trading" / "MT5 Second")
    (program_files / "Some MT4").mkdir()
    (program_files / "Some MT4" / "terminal.exe").write_bytes(b"MZ")
    data = tmp_path / "AppData" / "MetaQuotes" / "Terminal" / "ABC123"
    data.mkdir(parents=True)
    (data / "origin.txt").write_text(str(c.parent), encoding="utf-16")
    gone = tmp_path / "AppData" / "MetaQuotes" / "Terminal" / "OLD"
    gone.mkdir()
    (gone / "origin.txt").write_text(str(tmp_path / "Uninstalled MT5"), encoding="utf-16")
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        monkeypatch.setenv(variable, str(program_files))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr(terminals, "_from_registry", lambda: [])
    return a, b, c


def test_finds_each_installed_mt5_once(tmp_path, monkeypatch):
    a, b, c = fake_pc(tmp_path, monkeypatch)
    found = terminals.find_terminals()
    assert sorted(t["path"] for t in found) == sorted(str(p) for p in (a, b, c))
    assert {t["name"] for t in found} == {"MetaTrader 5", "Broker MetaTrader 5", "MT5 Second"}


def test_includes_an_accounts_own_path_in_an_unusual_folder(tmp_path, monkeypatch):
    fake_pc(tmp_path, monkeypatch)
    odd = install(tmp_path / "D-drive" / "My MT5")
    assert str(odd) in [t["path"] for t in terminals.find_terminals([str(odd)])]


def test_api_marks_which_account_uses_each_terminal(bridge, tmp_path, monkeypatch):
    a, b, _ = fake_pc(tmp_path, monkeypatch)
    bridge.add_account("acct_a", 1001, trading_on=False)
    assert bridge.patch("/accounts/acct_a", {"terminalPath": str(a)}).status_code == 200
    res = bridge.get("/terminals")
    assert res.status_code == 200
    by_path = {t["path"]: t for t in res.json()["terminals"]}
    assert by_path[str(a)]["usedById"] == "acct_a" and by_path[str(a)]["usedByName"] == "ACCT_A"
    assert by_path[str(b)]["usedById"] is None
