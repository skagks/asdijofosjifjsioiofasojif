"""Small filesystem helpers that behave on Windows (long paths, read-only files)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
PART_SUFFIX = ".mhwmm-part"


def longpath(path) -> str:
    """Return a path string usable beyond MAX_PATH on Windows."""
    s = os.fspath(path)
    if not IS_WINDOWS:
        return s
    s = os.path.abspath(s)
    if s.startswith("\\\\?\\") or len(s) < 240:
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def stat_or_none(path) -> os.stat_result | None:
    try:
        return os.stat(longpath(path))
    except (FileNotFoundError, NotADirectoryError):
        return None


def exists(path) -> bool:
    return os.path.exists(longpath(path))


def is_file(path) -> bool:
    return os.path.isfile(longpath(path))


def same_path(a, b) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(a))) == os.path.normcase(
        os.path.abspath(os.fspath(b))
    )


def is_within(path, base) -> bool:
    """True if ``path`` is strictly inside ``base``."""
    p = os.path.normcase(os.path.abspath(os.fspath(path)))
    b = os.path.normcase(os.path.abspath(os.fspath(base))).rstrip("\\/")
    return p != b and p.startswith(b + os.sep)


def _make_writable(path) -> None:
    try:
        os.chmod(longpath(path), stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def makedirs(path) -> None:
    os.makedirs(longpath(path), exist_ok=True)


def remove_file(path) -> None:
    p = longpath(path)
    try:
        os.remove(p)
    except FileNotFoundError:
        return
    except PermissionError:
        _make_writable(path)
        os.remove(p)


def replace_file(src, dst) -> None:
    try:
        os.replace(longpath(src), longpath(dst))
    except PermissionError:
        if not exists(dst):
            raise
        _make_writable(dst)
        os.replace(longpath(src), longpath(dst))


def copy_file(src, dst) -> None:
    """Copy ``src`` to ``dst`` through a temporary file so ``dst`` is never half written."""
    makedirs(os.path.dirname(os.path.abspath(os.fspath(dst))))
    tmp = os.fspath(dst) + PART_SUFFIX
    try:
        shutil.copy2(longpath(src), longpath(tmp))
        replace_file(tmp, dst)
    except BaseException:
        try:
            remove_file(tmp)
        except OSError:
            pass
        raise


def move_file(src, dst) -> None:
    makedirs(os.path.dirname(os.path.abspath(os.fspath(dst))))
    try:
        replace_file(src, dst)
    except OSError:
        # Different volume: fall back to copy + delete.
        copy_file(src, dst)
        remove_file(src)


def rmtree(path) -> None:
    if not os.path.lexists(longpath(path)):
        return

    def onexc(func, p, _exc):
        _make_writable(p)
        func(p)

    if sys.version_info >= (3, 12):
        shutil.rmtree(longpath(path), onexc=onexc)
    else:  # pragma: no cover - older Pythons
        shutil.rmtree(longpath(path), onerror=lambda func, p, info: onexc(func, p, info[1]))


def prune_empty_dirs(path, stop) -> None:
    """Remove ``path`` and its parents while they are empty, never touching ``stop``."""
    path = Path(path)
    while is_within(path, stop):
        try:
            os.rmdir(longpath(path))
        except OSError:
            return
        path = path.parent


def read_json(path, default):
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeDecodeError) as exc:
        broken = path.with_name(f"{path.name}.broken-{time.strftime('%Y%m%d-%H%M%S')}")
        log.error("%s 파일이 손상되어 %s(으)로 옮겼습니다: %s", path.name, broken.name, exc)
        try:
            os.replace(path, broken)
        except OSError:
            pass
        return default


def atomic_write_json(path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def open_in_explorer(path) -> None:
    import subprocess

    path = os.fspath(path)
    if IS_WINDOWS:
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
