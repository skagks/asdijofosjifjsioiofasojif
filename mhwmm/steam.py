"""Locate the Monster Hunter Wilds install through Steam."""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Iterable

from .constants import GAME_EXE, STEAM_APP_ID, STEAM_INSTALL_DIR

log = logging.getLogger(__name__)

_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|(//[^\n]*)|([^\s{}"]+)')


def parse_vdf(text: str) -> dict:
    """Parse Valve's KeyValues text format (libraryfolders.vdf, appmanifest_*.acf)."""
    stack: list[dict] = [{}]
    key: str | None = None
    for m in _VDF_TOKEN.finditer(text):
        quoted, brace, comment, bare = m.groups()
        if comment is not None:
            continue
        if brace == "{":
            child: dict = {}
            if key is not None:
                stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        else:
            token = re.sub(r"\\(.)", r"\1", quoted) if quoted is not None else bare
            if key is None:
                key = token
            else:
                stack[-1][key] = token
                key = None
    return stack[0]


def _get(d: dict, key: str):
    """Case-insensitive dictionary lookup (Steam is inconsistent about key case)."""
    if not isinstance(d, dict):
        return None
    lk = key.lower()
    for k, v in d.items():
        if k.lower() == lk:
            return v
    return None


def is_game_dir(path) -> bool:
    return bool(path) and (Path(path) / GAME_EXE).is_file()


def _read_vdf(path: Path) -> dict:
    try:
        return parse_vdf(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def library_paths(steam_root: Path) -> list[Path]:
    """All Steam library folders known to a Steam installation (including itself)."""
    steam_root = Path(steam_root)
    libs = [steam_root]
    for vdf in (steam_root / "steamapps" / "libraryfolders.vdf", steam_root / "config" / "libraryfolders.vdf"):
        if not vdf.is_file():
            continue
        folders = _get(_read_vdf(vdf), "libraryfolders") or {}
        for k, v in folders.items():
            if isinstance(v, dict):
                p = _get(v, "path")
            elif k.isdigit():  # old format: "1" "D:\\SteamLibrary"
                p = v
            else:
                p = None
            if p:
                libs.append(Path(p))
    out: list[Path] = []
    seen = set()
    for lib in libs:
        k = os.path.normcase(os.path.abspath(lib))
        if k not in seen:
            seen.add(k)
            out.append(lib)
    return out


def game_dir_in_library(library: Path) -> Path | None:
    steamapps = Path(library) / "steamapps"
    names = []
    acf = steamapps / f"appmanifest_{STEAM_APP_ID}.acf"
    if acf.is_file():
        installdir = _get(_get(_read_vdf(acf), "AppState") or {}, "installdir")
        if installdir:
            names.append(installdir)
    names.append(STEAM_INSTALL_DIR)
    for name in names:
        candidate = steamapps / "common" / name
        if is_game_dir(candidate):
            return candidate
    return None


def _windows_registry_paths() -> tuple[list[Path], list[Path]]:
    """(steam roots, direct game locations) read from the Windows registry."""
    roots: list[Path] = []
    games: list[Path] = []
    try:
        import winreg
    except ImportError:
        return roots, games
    queries = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath", roots),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath", roots),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath", roots),
    ]
    uninstall = rf"Microsoft\Windows\CurrentVersion\Uninstall\Steam App {STEAM_APP_ID}"
    queries += [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\{uninstall}", "InstallLocation", games),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\{uninstall}", "InstallLocation", games),
    ]
    for hive, key, value, target in queries:
        try:
            with winreg.OpenKey(hive, key) as k:
                v, _ = winreg.QueryValueEx(k, value)
                if v:
                    target.append(Path(v))
        except OSError:
            pass
    return roots, games


def _fixed_drives() -> list[str]:
    if os.name != "nt":
        return []
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        mask = kernel32.GetLogicalDrives()
        drives = []
        for i in range(26):
            if mask & (1 << i):
                root = f"{chr(65 + i)}:\\"
                if kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                    drives.append(root)
        return drives
    except Exception:  # pragma: no cover - defensive
        return ["C:\\"]


def steam_roots() -> list[Path]:
    roots: list[Path] = []
    if os.name == "nt":
        reg_roots, _ = _windows_registry_paths()
        roots += reg_roots
        for drive in _fixed_drives():
            for sub in ("Program Files (x86)\\Steam", "Program Files\\Steam", "Steam", "SteamLibrary",
                        "Games\\Steam", "Games\\SteamLibrary"):
                roots.append(Path(drive) / sub)
    else:
        home = Path.home()
        roots += [
            home / ".steam" / "steam",
            home / ".local" / "share" / "Steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        ]
        if sys.platform == "darwin":
            roots.append(home / "Library" / "Application Support" / "Steam")
    return [r for r in roots if r.is_dir()]


def find_game_dir(extra_roots: Iterable[Path] = (), use_system: bool = True) -> Path | None:
    """Best effort search for the Monster Hunter Wilds install folder."""
    roots = list(extra_roots)
    if use_system:
        if os.name == "nt":
            _, games = _windows_registry_paths()
            for g in games:
                if is_game_dir(g):
                    return g
        roots += steam_roots()
    seen = set()
    for root in roots:
        for lib in library_paths(root):
            k = os.path.normcase(os.path.abspath(lib))
            if k in seen:
                continue
            seen.add(k)
            found = game_dir_in_library(lib)
            if found:
                log.info("게임 폴더를 찾았습니다: %s", found)
                return found
    return None
