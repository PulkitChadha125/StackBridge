from superadmin_panel import config
from superadmin_panel.bootstrap_superadmin import main as bootstrap_main


def test_bootstrap_writes_hash_not_plaintext(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["bootstrap_superadmin", "--username", "owner", "--password", "S3cret!123"]
    )
    rc = bootstrap_main()
    assert rc == 0

    creds = config.load_superadmin_credentials()
    assert creds is not None
    username, password_hash = creds
    assert username == "owner"
    assert "S3cret!123" not in password_hash
    assert password_hash.startswith("$argon2id$")

    raw_file_contents = config.CONFIG_PATH.read_text(encoding="utf-8")
    assert "S3cret!123" not in raw_file_contents
