from __future__ import annotations

import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mhwmm.constants import GAME_EXE  # noqa: E402


def write(path: Path, data: str | bytes = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


def make_tree(base: Path, files: dict) -> Path:
    for rel, data in files.items():
        write(base / rel, data)
    return base


def make_zip(path: Path, files: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for rel, data in files.items():
            zf.writestr(rel, data)
    return path


def touch_later(path: Path, data: str | bytes) -> None:
    """Rewrite a file and make sure its mtime moves (coarse filesystems)."""
    st = path.stat()
    write(path, data)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


@pytest.fixture
def game_dir(tmp_path: Path) -> Path:
    game = tmp_path / "MonsterHunterWilds"
    write(game / GAME_EXE, "exe")
    write(game / "re_chunk_000.pak", "base")
    write(game / "re_chunk_000.pak.sub_000.pak", "sub")
    write(game / "re_chunk_000.pak.sub_000.pak.patch_001.pak", "official-1")
    return game


@pytest.fixture
def manager(tmp_path: Path, game_dir: Path):
    from mhwmm.manager import ModManager

    m = ModManager(tmp_path / "data")
    m.settings.check_game_running = False
    m.settings.check_updates = False  # no network in tests
    m.settings.check_refw_updates = False
    m.set_game_dir(game_dir)
    return m


@pytest.fixture
def now():
    return time.time()
