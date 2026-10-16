"""Per-Admin EXE builds (SuperAdmin "Build EXE" button).

The PyInstaller run itself is not exercised here - it takes minutes and needs
a full Windows toolchain. What is covered: filename safety, what gets baked
into the build, the job lifecycle, single-flight serialisation, and the
routes' auth/error behaviour.
"""

import json
from pathlib import Path

import pytest

from superadmin_panel import config, exe_builder


@pytest.fixture(autouse=True)
def _clean_jobs():
    exe_builder.reset_jobs_for_tests()
    yield
    exe_builder.reset_jobs_for_tests()


# ----------------------------------------------------------------- filenames

@pytest.mark.parametrize(
    "given,expected",
    [
        ("test one", "test one"),
        ("ABC Trading", "ABC Trading"),
        ("ABC/Trading", "ABC_Trading"),
        ("a<b>c:d|e?f*g", "a_b_c_d_e_f_g"),
        ("  spaced  out  ", "spaced out"),
        ("", "StakBridge"),
        ("...", "StakBridge"),
    ],
)
def test_safe_exe_stem(given, expected):
    assert exe_builder.safe_exe_stem(given) == expected


def test_safe_exe_stem_blocks_path_traversal():
    stem = exe_builder.safe_exe_stem("../../evil")
    assert "/" not in stem and "\\" not in stem and ".." not in stem


def test_safe_exe_stem_is_length_capped():
    assert len(exe_builder.safe_exe_stem("x" * 300)) <= 60


# ------------------------------------------------------------ baked settings

def test_built_settings_contain_server_url_and_no_secrets(monkeypatch, tmp_path):
    """The EXE must carry the endpoint and cosmetic identity - never a
    credential."""
    captured = {}

    def fake_popen(command, **kwargs):
        settings_path = kwargs["env"]["STAKBRIDGE_BUILD_SETTINGS"]
        captured["settings"] = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        captured["exe_name"] = kwargs["env"]["STAKBRIDGE_EXE_NAME"]
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(exe_builder, "preflight", lambda: [])
    monkeypatch.setattr(exe_builder.subprocess, "Popen", fake_popen)

    job = exe_builder.BuildJob(id="j1", admin_id="a1", admin_name="Test One")
    with pytest.raises(RuntimeError):
        exe_builder._do_build(
            job,
            {
                "id": "a1",
                "name": "Test One",
                "username": "test_admin",
                "password_hash": "$argon2id$SECRET",
            },
            "https://license.example.com/",
        )

    assert captured["exe_name"] == "Test One"
    assert captured["settings"]["server_url"] == "https://license.example.com"
    assert captured["settings"]["admin_username"] == "test_admin"
    blob = json.dumps(captured["settings"])
    assert "argon2" not in blob and "SECRET" not in blob
    assert "password_hash" not in captured["settings"]


# --------------------------------------------------------------- job records

def test_job_tracks_admin_and_serialises():
    job = exe_builder.BuildJob(id="j1", admin_id="a1", admin_name="Test One")
    payload = job.to_dict()
    assert payload["status"] == "queued"
    assert payload["done"] is False
    job.status = "success"
    assert job.to_dict()["done"] is True


def test_log_is_capped():
    job = exe_builder.BuildJob(id="j1", admin_id="a1", admin_name="Test One")
    for i in range(exe_builder.MAX_LOG_LINES + 120):
        job.append_log(f"line {i}")
    assert len(job.log) == exe_builder.MAX_LOG_LINES
    assert job.log[-1] == f"line {exe_builder.MAX_LOG_LINES + 119}"


def test_output_paths_are_isolated_per_admin(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "EXE_OUTPUT_DIR", str(tmp_path))
    a = exe_builder.built_exe_path("admin-a", "Test One")
    b = exe_builder.built_exe_path("admin-b", "Test One")
    assert a != b
    assert a.parent.name == "admin-a"
    assert b.parent.name == "admin-b"


def test_find_existing_exe_returns_none_when_never_built(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "EXE_OUTPUT_DIR", str(tmp_path))
    assert exe_builder.find_existing_exe("admin-a") is None


def test_find_existing_exe_finds_a_built_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "EXE_OUTPUT_DIR", str(tmp_path))
    target = exe_builder.built_exe_path("admin-a", "Test One")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"MZ")
    assert exe_builder.find_existing_exe("admin-a") == target


# ---------------------------------------------------------------- preflight

def test_preflight_reports_missing_source(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "EXE_BUILD_SOURCE_ROOT", str(tmp_path / "nope"))
    problems = exe_builder.preflight()
    assert problems and "source not found" in problems[0].lower()


def test_preflight_passes_on_a_real_checkout():
    """This repo IS a valid build source, so preflight should be clean here
    (on Windows, with pyinstaller installed via requirements-dev.txt)."""
    assert exe_builder.preflight() == []


# ------------------------------------------------------------------- routes

def _bootstrap_and_login(client):
    from superadmin_panel import security

    config.save_superadmin_credentials("owner", security.hash_password("S3cret!123"))
    client.post("/login", data={"username": "owner", "password": "S3cret!123"})


def _make_admin(sa_app):
    from datetime import date, timedelta

    from superadmin_panel import db, repositories as repo, security

    with sa_app.app_context():
        return repo.create_admin(
            db.get_db(),
            name="Test One",
            username="test_admin",
            password_hash=security.hash_password("Temp-Pass-1"),
            expiry_date=(date.today() + timedelta(days=30)).isoformat(),
            allowed_installations=1,
        )


def test_build_routes_require_login(client, sa_app):
    admin = _make_admin(sa_app)
    for resp in (
        client.post(f"/admins/{admin['id']}/build-exe"),
        client.get(f"/admins/{admin['id']}/build-exe/status"),
        client.get(f"/admins/{admin['id']}/build-exe/download"),
    ):
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


def test_build_button_appears_on_admin_page(client, sa_app):
    _bootstrap_and_login(client)
    admin = _make_admin(sa_app)
    resp = client.get(f"/admins/{admin['id']}")
    assert resp.status_code == 200
    assert b"Build EXE" in resp.data
    # The name it will produce is shown to the operator before they click.
    assert b"Test One.exe" in resp.data


def test_build_rejected_for_unknown_admin(client):
    _bootstrap_and_login(client)
    resp = client.post("/admins/does-not-exist/build-exe")
    assert resp.status_code == 404


def test_status_reports_none_before_any_build(client, sa_app):
    _bootstrap_and_login(client)
    admin = _make_admin(sa_app)
    resp = client.get(f"/admins/{admin['id']}/build-exe/status")
    assert resp.get_json()["status"] == "none"


def test_download_redirects_when_nothing_built(client, sa_app, monkeypatch, tmp_path):
    _bootstrap_and_login(client)
    monkeypatch.setattr(config, "EXE_OUTPUT_DIR", str(tmp_path))
    admin = _make_admin(sa_app)
    resp = client.get(f"/admins/{admin['id']}/build-exe/download", follow_redirects=False)
    assert resp.status_code == 302


def test_second_build_is_rejected_while_one_runs(client, sa_app, monkeypatch):
    _bootstrap_and_login(client)
    admin = _make_admin(sa_app)
    monkeypatch.setattr(exe_builder, "preflight", lambda: [])
    monkeypatch.setattr(exe_builder, "is_build_in_progress", lambda: True)
    resp = client.post(f"/admins/{admin['id']}/build-exe")
    assert resp.status_code == 409
    assert "one at a time" in resp.get_json()["message"]


def test_build_blocked_with_reason_when_preflight_fails(client, sa_app, monkeypatch):
    _bootstrap_and_login(client)
    admin = _make_admin(sa_app)
    monkeypatch.setattr(exe_builder, "preflight", lambda: ["PyInstaller is not installed"])
    resp = client.post(f"/admins/{admin['id']}/build-exe")
    assert resp.status_code == 400
    assert "PyInstaller" in resp.get_json()["message"]


def test_build_starts_and_reports_a_job(client, sa_app, monkeypatch):
    _bootstrap_and_login(client)
    admin = _make_admin(sa_app)
    monkeypatch.setattr(exe_builder, "preflight", lambda: [])
    started = {}

    def fake_start(admin_row, server_url):
        started["server_url"] = server_url
        started["name"] = admin_row["name"]
        return exe_builder.BuildJob(id="job-1", admin_id=admin_row["id"], admin_name=admin_row["name"])

    monkeypatch.setattr(exe_builder, "start_build", fake_start)
    resp = client.post(f"/admins/{admin['id']}/build-exe")
    assert resp.status_code == 202
    assert resp.get_json()["id"] == "job-1"
    assert started["name"] == "Test One"
    assert started["server_url"].startswith("http")


def test_build_bundles_this_servers_public_key(monkeypatch, tmp_path):
    """The EXE must verify licenses with the key of the server that built it,
    even if the source tree's license_public_key.pem is from somewhere else
    (e.g. keys/ was not copied to a fresh server, which then made new ones)."""
    from superadmin_panel import license_signing

    license_signing.set_key_paths_override(tmp_path / "priv.pem", tmp_path / "pub.pem")
    try:
        server_public_key = license_signing.get_public_key_pem()
        captured = {}

        def fake_popen(command, **kwargs):
            key_path = Path(kwargs["env"]["STAKBRIDGE_PUBLIC_KEY"])
            captured["name"] = key_path.name
            captured["key"] = key_path.read_bytes()
            raise RuntimeError("stop after capture")

        monkeypatch.setattr(exe_builder, "preflight", lambda: [])
        monkeypatch.setattr(exe_builder.subprocess, "Popen", fake_popen)

        job = exe_builder.BuildJob(id="j2", admin_id="a2", admin_name="Key Test")
        with pytest.raises(RuntimeError):
            exe_builder._do_build(job, {"id": "a2", "name": "Key Test", "username": "k"}, "https://x.example")

        assert captured["name"] == "license_public_key.pem"
        assert captured["key"] == server_public_key
    finally:
        license_signing.set_key_paths_override(None, None)


# ------------------------------------------------------- shared trial EXE

def _register(job):
    with exe_builder._jobs_lock:
        exe_builder._jobs[job.id] = job
    return job


def test_trial_build_bakes_no_admin_identity(monkeypatch):
    """Many prospects get the same file - it must not show or prefill any
    one person's name/username."""
    captured = {}

    def fake_popen(command, **kwargs):
        captured["settings"] = json.loads(
            Path(kwargs["env"]["STAKBRIDGE_BUILD_SETTINGS"]).read_text(encoding="utf-8")
        )
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(exe_builder, "preflight", lambda: [])
    monkeypatch.setattr(exe_builder.subprocess, "Popen", fake_popen)
    job = exe_builder.BuildJob(id="t", admin_id=exe_builder.TRIAL_BUILD_ID, admin_name="x")
    with pytest.raises(RuntimeError):
        exe_builder._do_build(
            job,
            {"id": exe_builder.TRIAL_BUILD_ID, "name": exe_builder.TRIAL_EXE_STEM, "username": ""},
            "https://license.example.com",
        )
    assert captured["settings"]["server_url"] == "https://license.example.com"
    assert captured["settings"]["admin_name"] == ""
    assert captured["settings"]["admin_username"] == ""


def test_ensure_trial_exe_does_not_start_a_second_build(monkeypatch, build_calls):
    monkeypatch.setattr(exe_builder, "preflight", lambda: [])
    running = _register(
        exe_builder.BuildJob(
            id="r", admin_id=exe_builder.TRIAL_BUILD_ID, admin_name="t", status="running",
            started_at="2026-10-03T00:00:00+00:00",
        )
    )
    assert exe_builder.ensure_trial_exe("http://x", force=True) is running
    assert build_calls == []


def test_ensure_trial_exe_backs_off_after_a_recent_failure(monkeypatch, build_calls):
    from datetime import datetime, timezone

    monkeypatch.setattr(exe_builder, "preflight", lambda: [])
    _register(
        exe_builder.BuildJob(
            id="f", admin_id=exe_builder.TRIAL_BUILD_ID, admin_name="t", status="failed",
            started_at="2026-10-03T00:00:00+00:00",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    )
    assert exe_builder.ensure_trial_exe("http://x") is None
    assert build_calls == []
    # The operator's explicit Rebuild ignores the back-off.
    exe_builder.ensure_trial_exe("http://x", force=True)
    assert len(build_calls) == 1


def test_ensure_trial_exe_skips_when_server_cannot_build(monkeypatch, build_calls):
    monkeypatch.setattr(exe_builder, "preflight", lambda: ["no PyInstaller"])
    assert exe_builder.ensure_trial_exe("http://x") is None
    assert build_calls == []


def test_find_trial_exe_matches_server_url(monkeypatch):
    trial_dir = config.exe_output_dir() / exe_builder.TRIAL_BUILD_ID
    trial_dir.mkdir(parents=True)
    (trial_dir / "T.exe").write_bytes(b"MZ")
    (trial_dir / exe_builder.BUILD_INFO_FILENAME).write_text(json.dumps({"server_url": "http://a"}))
    assert exe_builder.find_trial_exe("http://a/") is not None
    assert exe_builder.find_trial_exe("http://b") is None
    assert exe_builder.find_trial_exe() is not None
