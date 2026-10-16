# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for StakBridge.exe: one file, its own window, no console.

Normally the SuperAdmin panel's "Build EXE" button runs this for each Admin. By hand: build.bat.

Set by the panel's builder (all optional for a hand build):
    STAKBRIDGE_EXE_NAME        file name, e.g. "John Smith" -> "John Smith.exe"
    STAKBRIDGE_BUILD_SETTINGS  build_settings.json to put inside: licence server address, Admin name,
                               username. Without it the EXE refuses to start (it has no licence server),
                               unless STAKBRIDGE_LICENSE_SERVER is set on the PC running it.
    STAKBRIDGE_PUBLIC_KEY      the licence server's license_public_key.pem, so the EXE trusts exactly
                               the server that built it (only the public key: it can check, never sign)

The licence panel (superadmin_panel) and the tests are never put in the EXE.
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)  # noqa: F821 - given by PyInstaller

EXE_NAME = os.environ.get("STAKBRIDGE_EXE_NAME") or "StakBridge"
BUILD_SETTINGS = os.environ.get("STAKBRIDGE_BUILD_SETTINGS")
PUBLIC_KEY = os.environ.get("STAKBRIDGE_PUBLIC_KEY") or str(ROOT / "license_public_key.pem")

datas = [(str(ROOT / "stakbridge" / "static"), "stakbridge/static")]
if Path(PUBLIC_KEY).is_file():
    if Path(PUBLIC_KEY).name != "license_public_key.pem":
        raise SystemExit(f"STAKBRIDGE_PUBLIC_KEY must be a file named license_public_key.pem: {PUBLIC_KEY}")
    datas.append((PUBLIC_KEY, "."))
elif os.environ.get("STAKBRIDGE_PUBLIC_KEY"):
    raise SystemExit(f"STAKBRIDGE_PUBLIC_KEY does not exist: {PUBLIC_KEY}")
else:
    print("WARNING: no license_public_key.pem: this EXE cannot use its offline licence (72 hours without the server).")
if BUILD_SETTINGS:
    if not Path(BUILD_SETTINGS).is_file():
        raise SystemExit(f"STAKBRIDGE_BUILD_SETTINGS does not exist: {BUILD_SETTINGS}")
    datas.append((BUILD_SETTINGS, "."))  # read as build_settings.json at the root of the EXE

hidden_imports = [
    *collect_submodules("uvicorn"),  # uvicorn picks its loop and protocol parts at run time
    "MetaTrader5",  # imported only inside the worker, where it is needed
    "win32crypt",
    "win32timezone",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "desktop_launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["superadmin_panel", "tests", "argon2", "argon2_cffi", "pytest", "flask", "waitress", "PyInstaller"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(ROOT / "packaging" / "version_info.txt"),
    icon=str(ROOT / "packaging" / "stakbridge.ico"),  # also the window and taskbar icon
)
