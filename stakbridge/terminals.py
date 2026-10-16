"""Finds the MetaTrader 5 terminals installed on this PC, for the Terminal path drop-down."""

from __future__ import annotations

import glob
import os

from .workers.backends import TERMINAL_EXE

try:
    import winreg
except ImportError:  # not Windows
    winreg = None

_UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"


def _from_registry() -> list[tuple[str, str]]:
    """(folder, name) from the Windows list of installed programs: every MT5 setup registers there."""
    found = []
    if winreg is None:
        return found
    for hive, flags in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                        (winreg.HKEY_CURRENT_USER, 0)):
        try:
            root = winreg.OpenKey(hive, _UNINSTALL, 0, winreg.KEY_READ | flags)
        except OSError:
            continue
        with root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                        values = {}
                        for field in ("DisplayName", "Publisher", "InstallLocation", "DisplayIcon"):
                            try:
                                values[field] = str(winreg.QueryValueEx(key, field)[0])
                            except OSError:
                                values[field] = ""
                except OSError:
                    continue
                if "metaquotes" not in values["Publisher"].lower() and "metatrader" not in values["DisplayName"].lower():
                    continue
                folder = values["InstallLocation"] or os.path.dirname(values["DisplayIcon"].split(",")[0].strip('"'))
                if folder:
                    found.append((folder, values["DisplayName"]))
    return found


def _from_terminal_data() -> list[tuple[str, str]]:
    """(folder, "") from %APPDATA%\\MetaQuotes\\Terminal\\*\\origin.txt: each MT5 that has run once leaves one."""
    found = []
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return found
    for origin in glob.glob(os.path.join(appdata, "MetaQuotes", "Terminal", "*", "origin.txt")):
        for encoding in ("utf-16", "utf-8"):
            try:
                with open(origin, encoding=encoding) as f:
                    folder = f.read().strip().strip("﻿")
                break
            except (OSError, UnicodeError):
                folder = ""
        if folder:
            found.append((folder, ""))
    return found


def _from_program_files() -> list[tuple[str, str]]:
    """(folder, "") for any folder directly under Program Files that holds terminal64.exe."""
    found = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(variable)
        if base:
            found += [(os.path.dirname(p), "") for p in glob.glob(os.path.join(base, "*", TERMINAL_EXE))]
    return found


def find_terminals(extra_paths: list[str] = ()) -> list[dict]:
    """Every terminal64.exe found, as {"path", "name"}, sorted by name. extra_paths (the accounts'
    own paths) are included too when they exist, so a terminal in an unusual folder still shows."""
    candidates = _from_registry() + _from_terminal_data() + _from_program_files()
    candidates += [(os.path.dirname(p), "") for p in extra_paths if p]
    terminals: dict[str, dict] = {}
    for folder, name in candidates:
        path = os.path.normpath(os.path.join(folder.strip().strip('"'), TERMINAL_EXE))
        if not os.path.isfile(path):
            continue
        key = os.path.normcase(path)
        name = name.strip() or os.path.basename(os.path.dirname(path))
        if key not in terminals or len(name) > len(terminals[key]["name"]):
            terminals[key] = {"path": path, "name": name}
    return sorted(terminals.values(), key=lambda t: (t["name"].lower(), t["path"].lower()))
