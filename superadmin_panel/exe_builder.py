"""Per-Admin EXE builds, triggered from the SuperAdmin panel.

The delivered binary is functionally the same product for every Admin -
licensing is enforced server-side, so nothing secret or security-relevant is
compiled in. What a per-Admin build *does* give you is a file that is already
pointed at your license server and named after the customer, so delivery is
one file with nothing for the Admin to configure and nothing for you to
remember to attach.

Baked in (all non-secret): the server URL, the Admin's display name, and
their username for prefilling the login box. Never a password, never a
session token, never the license *signing* key - the EXE only ever carries
the public verification key.

Builds are slow (minutes) and PyInstaller is not safe to run concurrently
against one source tree, so this module runs at most one build at a time in a
background thread and exposes a pollable job record. Jobs live in memory
only: a server restart forgets them, which is fine - the built EXE files
themselves persist on disk.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, license_signing

# One build at a time. PyInstaller writes into shared build/ and __pycache__
# directories inside the source tree, so parallel runs corrupt each other.
_build_lock = threading.Lock()

_jobs: dict[str, "BuildJob"] = {}
_jobs_lock = threading.Lock()

MAX_LOG_LINES = 400

# The shared EXE every trial signup downloads. It belongs to no Admin - the
# binary is the same product for everyone and identity comes from the
# credentials emailed at signup - so it lives under a reserved build id that
# can never collide with an Admin's UUID.
TRIAL_BUILD_ID = "_trial"
TRIAL_EXE_STEM = "StakBridge"

# Written next to each finished EXE so we can tell which server URL it
# points at, and rebuild when that address changes.
BUILD_INFO_FILENAME = "build_info.json"

# After a failed automatic trial build, wait this long before trying again,
# so a broken build environment isn't retried on every public page view.
TRIAL_RETRY_COOLDOWN_SECONDS = 600

# ensure_trial_exe is check-then-start, reachable from concurrent public
# requests - without this two visitors could each queue a trial build.
_trial_ensure_lock = threading.Lock()


@dataclass
class BuildJob:
    id: str
    admin_id: str
    admin_name: str
    status: str = "queued"  # queued | running | success | failed
    message: str = ""
    exe_path: str | None = None
    exe_name: str | None = None
    log: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "admin_id": self.admin_id,
            "admin_name": self.admin_name,
            "status": self.status,
            "message": self.message,
            "exe_name": self.exe_name,
            "log": self.log[-40:],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "done": self.status in ("success", "failed"),
        }

    def append_log(self, line: str) -> None:
        self.log.append(line)
        if len(self.log) > MAX_LOG_LINES:
            del self.log[: len(self.log) - MAX_LOG_LINES]


def safe_exe_stem(name: str) -> str:
    """Turn an Admin's display name into a safe Windows filename stem.

    "test one" -> "test one". Spaces are kept (they read better on a file the
    customer receives); characters Windows forbids in filenames, path
    separators, and leading/trailing dots or spaces are not.
    """
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "")
    # Strip first, so a name that is nothing but dots/spaces falls through to
    # the product-name default rather than collapsing to a bare "_".
    cleaned = cleaned.strip(" .")
    # Then collapse dot runs, so nothing traversal-looking ("..") survives
    # into a filename - the separators are already gone by this point.
    cleaned = re.sub(r"\.{2,}", "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    if not cleaned:
        cleaned = "StakBridge"
    return cleaned[:60]


def get_job(job_id: str) -> BuildJob | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def get_latest_job_for_admin(admin_id: str) -> BuildJob | None:
    with _jobs_lock:
        jobs = [job for job in _jobs.values() if job.admin_id == admin_id]
    if not jobs:
        return None
    return sorted(jobs, key=lambda item: item.started_at)[-1]


def is_build_in_progress() -> bool:
    with _jobs_lock:
        return any(job.status in ("queued", "running") for job in _jobs.values())


def built_exe_path(admin_id: str, exe_stem: str) -> Path:
    """Where a finished build is kept. One directory per Admin so a rebuild
    replaces that Admin's file rather than piling up copies."""
    return config.exe_output_dir() / admin_id / f"{exe_stem}.exe"


def find_existing_exe(admin_id: str) -> Path | None:
    directory = config.exe_output_dir() / admin_id
    if not directory.is_dir():
        return None
    candidates = sorted(directory.glob("*.exe"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def read_build_info(admin_id: str) -> dict[str, Any]:
    info_path = config.exe_output_dir() / admin_id / BUILD_INFO_FILENAME
    try:
        return json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def find_trial_exe(server_url: str | None = None) -> Path | None:
    """The shared trial EXE, or None. With server_url, only an EXE built for
    that exact address counts - one pointing at an old address would hand
    every prospect a binary that can't reach the license server."""
    path = find_existing_exe(TRIAL_BUILD_ID)
    if path is None or server_url is None:
        return path
    built_for = (read_build_info(TRIAL_BUILD_ID).get("server_url") or "").rstrip("/")
    return path if built_for == server_url.rstrip("/") else None


def ensure_trial_exe(server_url: str, *, force: bool = False) -> BuildJob | None:
    """Start building the shared trial EXE if it is missing or points at a
    different server URL. Returns the in-flight job, or None when the EXE is
    already current (or can't be built here - see preflight()).

    Safe to call on every request: it never starts a second trial build
    while one is queued/running, and backs off after a recent failure
    unless force=True (the operator's explicit "Rebuild" button).
    """
    with _trial_ensure_lock:
        latest = get_latest_job_for_admin(TRIAL_BUILD_ID)
        if latest is not None and latest.status in ("queued", "running"):
            return latest
        if not force and find_trial_exe(server_url) is not None:
            return None
        if preflight():
            return None
        if not force and latest is not None and latest.status == "failed" and latest.finished_at:
            finished = datetime.fromisoformat(latest.finished_at)
            if (datetime.now(timezone.utc) - finished).total_seconds() < TRIAL_RETRY_COOLDOWN_SECONDS:
                return None
        return start_build(
            {"id": TRIAL_BUILD_ID, "name": TRIAL_EXE_STEM, "username": ""}, server_url
        )


def preflight() -> list[str]:
    """Reasons a build cannot run right now, as human-readable strings.

    Checked before offering the button so the failure is explained up front
    rather than three minutes into a doomed PyInstaller run.
    """
    problems: list[str] = []

    source_root = config.exe_build_source_root()
    if not source_root.is_dir():
        problems.append(
            f"Application source not found at {source_root}. Set EXE_BUILD_SOURCE_ROOT "
            "to the folder containing desktop_launcher.py."
        )
        return problems

    spec = source_root / "StakBridge.spec"
    if not spec.is_file():
        problems.append(f"Build spec not found: {spec}")
    if not (source_root / "desktop_launcher.py").is_file():
        problems.append(f"desktop_launcher.py not found in {source_root}")

    if shutil.which("pyinstaller") is None and not _pyinstaller_module_available():
        problems.append(
            "PyInstaller is not installed in this server's Python environment "
            "(pip install pyinstaller)."
        )

    if not sys.platform.startswith("win"):
        problems.append(
            "EXE builds must run on Windows - PyInstaller cannot cross-compile "
            "a Windows binary from this platform."
        )

    return problems


def _pyinstaller_module_available() -> bool:
    try:
        import PyInstaller  # noqa: F401

        return True
    except ImportError:
        return False


def start_build(admin: dict[str, Any], server_url: str) -> BuildJob:
    """Queue a build and return its job record immediately."""
    job = BuildJob(
        id=str(uuid.uuid4()),
        admin_id=admin["id"],
        admin_name=admin.get("name") or admin.get("username") or "Admin",
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    with _jobs_lock:
        _jobs[job.id] = job

    thread = threading.Thread(
        target=_run_build,
        args=(job, dict(admin), server_url),
        daemon=True,
        name=f"exe-build-{job.id[:8]}",
    )
    thread.start()
    return job


def _run_build(job: BuildJob, admin: dict[str, Any], server_url: str) -> None:
    # Serialise builds; a queued job simply waits its turn here.
    with _build_lock:
        job.status = "running"
        job.append_log(f"Building EXE for '{job.admin_name}' ...")
        try:
            _do_build(job, admin, server_url)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator via the job
            job.status = "failed"
            job.message = str(exc)
            job.append_log(f"ERROR: {exc}")
        finally:
            job.finished_at = datetime.now(timezone.utc).isoformat()


def _do_build(job: BuildJob, admin: dict[str, Any], server_url: str) -> None:
    problems = preflight()
    if problems:
        raise RuntimeError(problems[0])

    source_root = config.exe_build_source_root()
    exe_stem = safe_exe_stem(admin.get("name") or admin.get("username") or "")
    job.exe_name = f"{exe_stem}.exe"

    # The trial EXE goes to many people, so it carries no name or username
    # to show on (or prefill into) the login screen.
    is_trial = admin.get("id") == TRIAL_BUILD_ID
    settings = {
        "server_url": server_url.rstrip("/"),
        "admin_name": "" if is_trial else (admin.get("name") or ""),
        "admin_username": "" if is_trial else (admin.get("username") or ""),
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    job.append_log(f"Server URL baked in: {settings['server_url']}")

    with tempfile.TemporaryDirectory(prefix="stakbridge-build-") as tmp:
        tmp_path = Path(tmp)
        settings_path = tmp_path / "build_settings.json"
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

        # Bundle THIS server's verification key, not whatever
        # license_public_key.pem sits in the source tree. If the two ever
        # differ (e.g. keys/ was not copied to a new server, so it generated
        # a fresh pair), every license this server signs would be rejected
        # by the EXE - and nothing would say why.
        public_key_dir = tmp_path / "public_key"
        public_key_dir.mkdir()
        public_key_path = public_key_dir / "license_public_key.pem"
        public_key_path.write_bytes(license_signing.get_public_key_pem())

        dist_dir = tmp_path / "dist"
        work_dir = tmp_path / "work"

        env = os.environ.copy()
        env["STAKBRIDGE_EXE_NAME"] = exe_stem
        env["STAKBRIDGE_BUILD_SETTINGS"] = str(settings_path)
        env["STAKBRIDGE_PUBLIC_KEY"] = str(public_key_path)

        command = _pyinstaller_command() + [
            "StakBridge.spec",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(dist_dir),
            "--workpath",
            str(work_dir),
        ]
        job.append_log("Running PyInstaller (this takes a few minutes) ...")

        process = subprocess.Popen(
            command,
            cwd=str(source_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            if line:
                job.append_log(line)
        return_code = process.wait()

        if return_code != 0:
            raise RuntimeError(
                f"PyInstaller exited with code {return_code}. See the log above for the cause."
            )

        produced = dist_dir / f"{exe_stem}.exe"
        if not produced.is_file():
            raise RuntimeError(f"Build reported success but {produced.name} was not produced.")

        destination = built_exe_path(job.admin_id, exe_stem)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Clear older builds for this Admin so a rename doesn't leave a stale
        # EXE alongside the current one.
        for stale in destination.parent.glob("*.exe"):
            if stale != destination:
                stale.unlink(missing_ok=True)
        shutil.copy2(produced, destination)
        (destination.parent / BUILD_INFO_FILENAME).write_text(
            json.dumps({"server_url": settings["server_url"], "built_at": settings["built_at"]}),
            encoding="utf-8",
        )

        job.exe_path = str(destination)
        job.status = "success"
        size_mb = destination.stat().st_size / (1024 * 1024)
        job.message = f"{destination.name} built successfully ({size_mb:.1f} MB)."
        job.append_log(job.message)


def _pyinstaller_command() -> list[str]:
    """Prefer the current interpreter's PyInstaller module over a PATH lookup,
    so the build uses the same virtualenv the server is running in."""
    if _pyinstaller_module_available():
        return [sys.executable, "-m", "PyInstaller"]
    return ["pyinstaller"]


def reset_jobs_for_tests() -> None:
    with _jobs_lock:
        _jobs.clear()
