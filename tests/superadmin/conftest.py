import pytest

from superadmin_panel import config, db, exe_builder, license_signing, rate_limit


@pytest.fixture(autouse=True)
def _sandbox_superadmin_state(tmp_path, monkeypatch):
    """Hermetic per-test SuperAdmin DB + config - never touches real files."""
    db.set_db_path_override(tmp_path / "superadmin_test.db")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "superadmin_config.json")
    monkeypatch.setattr(config, "SECRET_KEY_PATH", tmp_path / "flask_secret_key.txt")
    monkeypatch.setattr(config, "EXE_OUTPUT_DIR", str(tmp_path / "built_exes"))
    monkeypatch.setattr(config, "LOG_UPLOAD_DIR", str(tmp_path / "log_uploads"))
    # Throwaway signing keys: a test run must never create (or replace) the server's real keypair.
    license_signing.set_key_paths_override(tmp_path / "keys" / "private.pem", tmp_path / "keys" / "public.pem")
    rate_limit.reset_all()
    exe_builder.reset_jobs_for_tests()
    yield
    db.set_db_path_override(None)
    license_signing.set_key_paths_override(None, None)
    rate_limit.reset_all()
    exe_builder.reset_jobs_for_tests()


@pytest.fixture(autouse=True)
def build_calls(monkeypatch):
    """Pages now start the shared trial EXE build on their own (signup page,
    saving the server address). On a Windows dev box preflight() passes, so
    without this every such test would launch a real multi-minute PyInstaller
    run. Records the calls instead; tests that care override start_build."""
    calls = []

    def _record(admin, server_url):
        calls.append({"admin_id": admin["id"], "server_url": server_url})
        return exe_builder.BuildJob(id=f"stub-{len(calls)}", admin_id=admin["id"], admin_name=admin["name"])

    monkeypatch.setattr(exe_builder, "start_build", _record)
    return calls


@pytest.fixture
def sa_app(_sandbox_superadmin_state):
    from superadmin_panel.app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    return app


@pytest.fixture
def client(sa_app):
    return sa_app.test_client()


@pytest.fixture
def conn(_sandbox_superadmin_state):
    connection = db.get_connection()
    db.init_db()
    yield connection
    connection.close()
