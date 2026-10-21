"""Mod library: installed mods, load order, settings and deployment."""

from __future__ import annotations

import copy
import hashlib
import io
import itertools
import json
import logging
import logging.handlers
import lzma
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import zipfile
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

from . import crashes, fsutil, matfix, nexus, precheck, requirements, steam
from .analyzer import Variant, analyze, classify, list_files, package_name, raw_variant
from .archive import extract, is_archive
from .constants import GAME_EXE, NEXUS_SEARCH_URL, REFRAMEWORK_DLL, REFRAMEWORK_URL, REFW_ORIGIN
from .deploy import Deployer, DeployError, DeployReport, ModSource, ProgressFn, is_root_pak, path_key
from .equip import EquipError
from .equip import catalog as equip_catalog
from .equip import icons as equip_icons
from .equip import look as equip_look
from .equip import overview as equip_overview
from .equip import pathlist as equip_pathlist
from .equip import scan as equip_scan
from .equip import selftest as equip_selftest
from .equip import tex as equip_tex
from .equip import userdata as equip_userdata
from .equip.model import PART_LABELS
from .matfix import MatfixError, rsz
from .matfix.game import GameIndex, ShaderIndex, game_fingerprint
from .matfix.repair import RSZ_RE, FileResult, RszContext, check_mod, is_material_file, rsz_sources

log = logging.getLogger(__name__)

STATE_VERSION = 1
SINGLE_FILE_EXTS = (".pak", ".lua", ".dll", ".asi")
STALE_TEMP_SECONDS = 2 * 3600
DATA_DIR_NAME = "MHWildsModManager_data"  # next to the exe
DEFAULT_PROFILE = "기본"
MAX_PROFILE_NAME = 40
MATFIX_RECORD = "matfix.json"  # per mod: what the material check found and which repairs are in use
MATFIX_DIR = "matfix"  # per mod: the repaired files (the mod's own files stay as they are)
OPTION_PREVIEWS = "previews"  # per mod: the pictures of its options, named after the option id
MAX_ARCHIVE_DIRS = 8
# Version of the check/repair rules: records made with older rules count as unchecked, so every
# mod is checked again (and repairs offered again) after an update. Raise it whenever a change
# can give a file another result. 2: .user/.pfb/.scn checked too; 3: RSZ rules of v1.11.1-1.11.3.
MATFIX_FORMAT = 3
REPORT_SELFTEST_ITEMS = 6  # game parts the diagnostics zip's 3D preview self-test looks at
REPORT_SELFTEST_SECONDS = 60.0  # ... and for at most this long
REGISTRY_RETRY = 3 * 86400  # how long to wait before looking for newer .user/.pfb type data again
VERSIONED_RE = re.compile(r"\.(mdf2|user|pfb|scn)\.(\d+)$", re.IGNORECASE)  # the game reads one version number


class InstallError(Exception):
    pass


def app_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    """Per-user data folder (the default before v1.3.0, and the fallback)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "MHWildsModManager"


def _dir_writable(path: Path) -> bool:
    probe = path / f".mhwmm-write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _has_data(path: Path) -> bool:
    return (path / "state.json").is_file() or (path / "mods").is_dir()


def resolve_data_dir() -> tuple[Path, Path | None]:
    """(data folder to use, older data folder to move into it first or None).

    The packaged exe keeps its data in ``MHWildsModManager_data`` next to itself
    unless that folder cannot be written; running from source uses the user folder."""
    env = os.environ.get("MHWMM_DATA_DIR")
    if env:
        return Path(env), None
    app_dir = app_directory()
    if (app_dir / "portable.txt").exists():  # portable mode from older versions
        return app_dir / "data", None
    legacy = user_data_dir()
    if not getattr(sys, "frozen", False):
        return legacy, None
    target = app_dir / DATA_DIR_NAME
    if target.is_dir():
        return target, None
    if not _dir_writable(app_dir):
        return legacy, None
    return target, (legacy if _has_data(legacy) else None)


def default_data_dir() -> Path:
    return resolve_data_dir()[0]


def _rename_dir(src: Path, dst: Path) -> None:
    os.replace(fsutil.longpath(src), fsutil.longpath(dst))


def migrate_data_dir(src: Path, dst: Path, progress: ProgressFn | None = None) -> None:
    """Move the whole data folder ``src`` to ``dst`` (which must not exist yet).

    A rename is used when both are on the same drive; otherwise the files are copied
    to a temporary folder first, so a failure leaves ``src`` untouched."""
    progress = progress or (lambda done, total, msg: None)
    try:
        _rename_dir(src, dst)
        return
    except OSError:
        pass  # different drive or in use: copy instead
    files = []
    for dirpath, _dirnames, filenames in os.walk(fsutil.longpath(src)):
        for name in filenames:
            full = os.path.join(dirpath, name)
            files.append((full, os.path.relpath(full, fsutil.longpath(src)), os.path.getsize(full)))
    total = sum(size for _, _, size in files) or 1
    partial = dst.with_name(dst.name + ".partial")
    fsutil.rmtree(partial)
    done = 0
    try:
        for full, rel, size in files:
            fsutil.copy_file(full, partial / rel)  # keeps modification times, so nothing is redeployed
            done += size
            progress(done, total, f"데이터 폴더 옮기는 중... {done * 100 // total}%")
        _rename_dir(partial, dst)
    except BaseException:
        fsutil.rmtree(partial)
        raise
    try:
        fsutil.rmtree(src)
    except OSError as exc:
        log.warning("이전 데이터 폴더를 지우지 못했습니다: %s (%s)", src, exc)


def prepare_data_dir(explicit=None, progress: ProgressFn | None = None) -> tuple[Path, str | None]:
    """Pick the data folder, moving data from an older location if needed.

    Returns (folder, problem): ``problem`` explains why an older folder is still used."""
    if explicit:
        return Path(explicit), None
    target, legacy = resolve_data_dir()
    if legacy is None:
        return target, None
    probe = acquire_instance_lock(legacy)
    if probe is None:  # an older version is running from there; let the caller report it
        return legacy, None
    probe.close()  # the lock file moves along with the folder
    try:
        migrate_data_dir(legacy, target, progress)
    except OSError as exc:
        return legacy, f"데이터 폴더를 {target}(으)로 옮기지 못해 이전 위치({legacy})를 계속 씁니다: {exc}"
    return target, None


def setup_logging(data_dir: Path, extra: logging.Handler | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            data_dir / "manager.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
    except OSError:
        pass
    if extra:
        root.addHandler(extra)


def acquire_instance_lock(data_dir: Path):
    """Hold an exclusive lock so only one manager edits a data folder at a time.

    Returns an open file object to keep alive, or None if another instance holds it."""
    data_dir.mkdir(parents=True, exist_ok=True)
    f = open(data_dir / "instance.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def is_game_running() -> bool:
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return GAME_EXE.lower() in (out or "").lower()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _from_dict(cls, data: dict):
    if not isinstance(data, dict):
        return cls() if cls is Settings else None
    names = {f.name: f for f in fields(cls)}
    kwargs = {}
    for k, v in data.items():
        f = names.get(k)
        if f is None:
            continue
        default = f.default
        if isinstance(default, bool) and not isinstance(v, bool):
            continue
        if isinstance(default, str) and not isinstance(v, str):
            continue
        if isinstance(default, int) and not isinstance(default, bool) and (isinstance(v, bool) or not isinstance(v, int)):
            continue
        if isinstance(default, float):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            v = float(v)
        if f.default_factory is list and not isinstance(v, list):
            continue
        if f.default_factory is dict and not isinstance(v, dict):
            continue
        kwargs[k] = v
    return cls(**kwargs)


@dataclass
class Settings:
    theme: str = "dark"
    auto_apply: bool = True
    enable_on_install: bool = True
    check_game_running: bool = True
    check_updates: bool = True
    check_refw_updates: bool = True
    nexus_api_key: str = ""
    check_mod_updates: bool = True
    last_mod_check: float = 0.0
    confirm_remove: bool = True
    apply_before_launch: bool = True
    minimize_on_launch: bool = False
    remember_window: bool = True
    window_geometry: str = ""
    window_zoomed: bool = False
    sash_ratio: float = 0.58
    log_visible: bool = True
    check_materials: bool = True  # look for outdated materials at startup and after installs
    archive_dirs: list = field(default_factory=list)  # folders mods were installed from, the latest first


@dataclass
class Mod:
    id: str = ""
    name: str = ""
    enabled: bool = False
    version: str = ""
    author: str = ""
    description: str = ""
    category: str = ""
    source: str = ""
    installed_at: str = ""
    updated_at: str = ""
    screenshot: str = ""
    # Archives with several variants become one mod with options: [{"id": "o1", "name": ...}].
    # Only the options listed in ``selected`` are deployed (none right after install).
    options: list = field(default_factory=list)
    selected: list = field(default_factory=list)
    origin: str = ""  # "reframework" for the REFramework installed by the REFramework updater
    # Nexus Mods page of the mod and the upload time of the installed file (0 = unknown)
    nexus_id: int = 0
    nexus_file_time: int = 0
    nexus_exact: bool = True  # nexus_file_time is to the second (not only the minute)
    nexus_checked: int = 0  # last time this mod was looked up on Nexus
    nexus_unlinked: bool = False  # the user removed the link: don't guess it from the file name again
    update: dict = field(default_factory=dict)  # newer file found on Nexus: {"version", "file", "uploaded"}
    # Requirements listed on the Nexus page: [{"name", "url", "nexus_id", "notes", "external"}]
    requirements: list = field(default_factory=list)
    requirements_checked: int = 0  # when they were last read from Nexus (0 = never)
    requirements_format: int = 0  # nexus.REQUIREMENTS_FORMAT they were read with
    lua_requires: list = field(default_factory=list)  # Lua modules the mod's scripts need from other mods
    lua_scanned: bool = False
    # the archive's option pictures were looked for (installs since 1.12.0 and read_option_pictures)
    pictures_read: bool = False

    @property
    def nexus_url(self) -> str:
        return nexus.MOD_URL.format(self.nexus_id) if self.nexus_id else ""

    def __post_init__(self):
        self.options = [o for o in self.options
                        if isinstance(o, dict) and isinstance(o.get("id"), str) and isinstance(o.get("name"), str)]
        ids = [o["id"] for o in self.options]
        self.selected = [i for i in ids if i in self.selected]
        self.requirements = [r for r in self.requirements if isinstance(r, dict) and isinstance(r.get("name"), str)]
        self.lua_requires = [r for r in self.lua_requires if isinstance(r, str)]

    @property
    def needs_option(self) -> bool:
        """True for a mod with options of which none is selected yet."""
        return bool(self.options) and not self.selected

    def to_dict(self) -> dict:
        return asdict(self)


class Conflicts:
    """Loose files provided by more than one enabled mod."""

    def __init__(self, owners: dict[str, list[str]], paths: dict[str, str]):
        self.owners = owners  # key -> mod ids in load order (last one wins)
        self.paths = paths

    def winner(self, key: str) -> str | None:
        ids = self.owners.get(key)
        return ids[-1] if ids else None

    def for_mod(self, mod_id: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """(files this mod overrides per other mod, files overriding this mod per other mod)."""
        beats: dict[str, list[str]] = {}
        beaten: dict[str, list[str]] = {}
        for key, ids in self.owners.items():
            if mod_id not in ids:
                continue
            winner = ids[-1]
            path = self.paths[key]
            if winner == mod_id:
                for other in ids[:-1]:
                    beats.setdefault(other, []).append(path)
            else:
                beaten.setdefault(winner, []).append(path)
        return beats, beaten

    def pairs(self) -> list[tuple[str, str, list[str]]]:
        """(winning mod, overridden mod, files) for every two mods that change the same files, most files first."""
        found: dict[tuple[str, str], list[str]] = {}
        for key, ids in self.owners.items():
            for other in ids[:-1]:
                found.setdefault((ids[-1], other), []).append(self.paths[key])
        return sorted(((w, l, sorted(f, key=str.casefold)) for (w, l), f in found.items()), key=lambda p: -len(p[2]))

    def counts(self, mod_id: str) -> tuple[int, int]:
        wins = losses = 0
        for ids in self.owners.values():
            if mod_id in ids:
                if ids[-1] == mod_id:
                    wins += 1
                else:
                    losses += 1
        return wins, losses


@dataclass
class MaterialJob:
    """One mod for a material check or repair, taken on the caller's thread."""
    mod_id: str
    name: str
    stamp: str  # changes when the mod is reinstalled
    files: list  # [(source key, game path, original file)]
    provided: set = field(default_factory=set)  # names of all the mod's files (no version number), lower case
    stage: Path | None = None  # where the repaired files are written (repairs only)


@dataclass
class MaterialRun:
    game: str  # fingerprint of the game files checked against
    repair: bool
    results: dict = field(default_factory=dict)  # mod id -> [FileResult]
    errors: dict = field(default_factory=dict)  # mod id -> why it could not be checked
    registry: str = ""  # date of the .user/.pfb type data used
    registry_updated: str = ""  # newer type data downloaded during the run (used from the next check)


@dataclass
class EquipJob:
    """One mod for the equipment window, taken on the caller's thread."""
    mod_id: str
    name: str
    enabled: bool
    order: int  # place in the load order (later mods win)
    stamp: str  # changes when the mod's files change
    files: dict  # game path -> stored file (selected options, repaired materials)


class InstallSession:
    """An extracted archive (or folder) waiting for the user to pick variants."""

    def __init__(self, source: Path, base: Path, temp_dir: Path | None, variants: list[Variant]):
        self.source = source
        self.base = base
        self.temp_dir = temp_dir
        self.variants = variants

    @property
    def display_name(self) -> str:
        return self.source.stem if self.source.is_file() else self.source.name

    @property
    def owns_files(self) -> bool:
        return self.temp_dir is not None

    def raw_variant(self) -> Variant:
        return raw_variant(self.base, self.display_name)

    def cleanup(self) -> None:
        if self.temp_dir is not None:
            try:
                fsutil.rmtree(self.temp_dir)
            except OSError as exc:
                log.warning("임시 폴더를 지우지 못했습니다: %s (%s)", self.temp_dir, exc)
            self.temp_dir = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()


class ModManager:
    def __init__(self, data_dir=None):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.mods_dir = self.data_dir / "mods"
        self.temp_root = self.data_dir / "temp"
        self.state_path = self.data_dir / "state.json"
        self.mods: list[Mod] = []
        self.game_dir: Path | None = None
        self.settings = Settings()
        self.dirty = False
        self.revision = 0  # bumped on every change that affects deployment
        # Profiles remember which mods are on, their options and the load order.
        # The active profile's state lives in self.mods; the others are snapshots.
        self.profiles: dict[str, dict] = {}
        self.profile = DEFAULT_PROFILE
        self._entries_cache: dict[str, dict[str, tuple[Path, str]]] = {}
        self._option_cache: dict[tuple[str, str], list[str]] = {}
        self._size_cache: dict[str, int] = {}
        self._material_cache: dict[str, list[tuple[str, str, Path]]] = {}
        self._record_cache: dict[str, dict] = {}
        self._game_version: tuple[str, str] | None = None
        self._registry_date: str | None = None
        self._equip_lock = threading.RLock()
        self._equip_game_index = None  # GameIndex kept for the equipment window's pictures
        self._equip_catalog: tuple[str, object] | None = None  # (game version, Catalog)
        self._equip_paths = None  # PathIndex of the game's equipment paths (for .pak mods)
        self._equip_scans: dict[str, tuple[str, object]] | None = None  # mod id -> (stamp, ModScan)
        self._equip_failed: set[tuple[str, int]] = set()  # pictures that could not be read
        self._preview_failed: dict[tuple[str, str], str] = {}  # (cache folder, key) of failed 3D previews -> why
        self._preview_textures = None  # equip_look.Textures shared by the 3D previews
        self.data_dir.mkdir(parents=True, exist_ok=True)
        rsz.TypeRegistry.updates = self.data_dir / "cache"
        self.mods_dir.mkdir(exist_ok=True)
        self.load()
        self._cleanup_temp()

    # ------------------------------------------------------------- persistence
    def load(self) -> None:
        data = fsutil.read_json(self.state_path, {})
        if not isinstance(data, dict):
            data = {}
        self.settings = _from_dict(Settings, data.get("settings") or {})
        game = data.get("game_dir")
        self.game_dir = Path(game) if isinstance(game, str) and game else None
        self.dirty = bool(data.get("dirty", False))
        mods = []
        for raw in data.get("mods") or []:
            m = _from_dict(Mod, raw)
            if m and m.id and self._has_payload(self.mods_dir / m.id):
                mods.append(m)
            elif m and m.id:
                log.warning("모드 파일이 없어 목록에서 제외합니다: %s", m.name or m.id)
        known = {m.id for m in mods}
        for d in sorted(self.mods_dir.iterdir()) if self.mods_dir.is_dir() else []:
            if d.name.startswith(".") or d.name in known or not self._has_payload(d):
                continue
            m = _from_dict(Mod, fsutil.read_json(d / "mod.json", {})) or Mod()
            m.id = d.name
            m.enabled = False
            m.name = m.name or d.name
            mods.append(m)
            log.warning("목록에 없던 모드를 복구했습니다: %s", m.name)
        self.mods = mods
        raw_profiles = data.get("profiles")
        self.profiles = {k: v for k, v in raw_profiles.items()
                         if isinstance(k, str) and k.strip() and isinstance(v, dict)} if isinstance(raw_profiles, dict) else {}
        profile = data.get("profile")
        if not isinstance(profile, str) or profile not in self.profiles:
            profile = next(iter(self.profiles), DEFAULT_PROFILE)
        self.profile = profile
        self.profiles[profile] = self._snapshot()

    @staticmethod
    def _has_payload(mod_dir: Path) -> bool:
        return (mod_dir / "files").is_dir() or (mod_dir / "options").is_dir()

    def _invalidate(self, mod_id: str) -> None:
        self._entries_cache.pop(mod_id, None)
        self._size_cache.pop(mod_id, None)
        self._material_cache.pop(mod_id, None)
        self._record_cache.pop(mod_id, None)
        for key in [k for k in self._option_cache if k[0] == mod_id]:
            del self._option_cache[key]

    def save(self) -> None:
        self.profiles[self.profile] = self._snapshot()
        known = {m.id for m in self.mods}
        for snap in self.profiles.values():  # forget removed mods
            snap["order"] = [i for i in snap.get("order") or [] if i in known]
            snap["enabled"] = [i for i in snap.get("enabled") or [] if i in known]
            snap["options"] = {k: v for k, v in (snap.get("options") or {}).items() if k in known}
        fsutil.atomic_write_json(self.state_path, {
            "version": STATE_VERSION,
            "game_dir": str(self.game_dir) if self.game_dir else "",
            "dirty": self.dirty,
            "settings": asdict(self.settings),
            "profile": self.profile,
            "profiles": self.profiles,
            "mods": [m.to_dict() for m in self.mods],
        })

    # ------------------------------------------------------------- profiles
    def _snapshot(self) -> dict:
        """Enabled mods, chosen options (by name, so updates keep them) and load order."""
        return {
            "order": [m.id for m in self.mods],
            "enabled": [m.id for m in self.mods if m.enabled],
            "options": {m.id: [o["name"] for o in m.options if o["id"] in m.selected]
                        for m in self.mods if m.options},
        }

    def _restore(self, snap: dict) -> None:
        position = {mod_id: i for i, mod_id in enumerate(snap.get("order") or []) if isinstance(mod_id, str)}
        current = {m.id: i for i, m in enumerate(self.mods)}
        # mods the profile has not seen yet (installed later) go to the end, switched off
        self.mods.sort(key=lambda m: (0, position[m.id]) if m.id in position else (1, current[m.id]))
        enabled = set(snap.get("enabled") or [])
        options = snap.get("options") or {}
        for m in self.mods:
            m.enabled = m.id in enabled
            if m.options:
                names = options.get(m.id) or []
                m.selected = [o["id"] for o in m.options if o["name"] in names]
            self._invalidate(m.id)

    def _check_profile_name(self, name: str, keep: str | None = None) -> str:
        name = name.strip()
        if not name:
            raise ValueError("프로필 이름이 비어 있습니다.")
        if len(name) > MAX_PROFILE_NAME:
            raise ValueError(f"프로필 이름은 {MAX_PROFILE_NAME}자까지 쓸 수 있습니다.")
        for existing in self.profiles:
            if existing != keep and existing.casefold() == name.casefold():
                raise ValueError(f"'{existing}' 프로필이 이미 있습니다.")
        return name

    def switch_profile(self, name: str) -> None:
        if name not in self.profiles:
            raise KeyError(name)
        if name == self.profile:
            return
        self.profiles[self.profile] = self._snapshot()
        self.profile = name
        self._restore(self.profiles[name])
        self._changed()

    def create_profile(self, name: str, copy_current: bool = True) -> str:
        """Add a profile (a copy of the current one, or with every mod off) and switch to it."""
        name = self._check_profile_name(name)
        snap = self._snapshot()
        if not copy_current:
            snap = {"order": snap["order"], "enabled": [], "options": {}}
        self.profiles[name] = copy.deepcopy(snap)
        self.switch_profile(name)
        return name

    def rename_profile(self, old: str, new: str) -> str:
        if old not in self.profiles:
            raise KeyError(old)
        new = self._check_profile_name(new, keep=old)
        self.profiles = {(new if k == old else k): v for k, v in self.profiles.items()}
        if self.profile == old:
            self.profile = new
        self.save()
        return new

    def delete_profile(self, name: str) -> None:
        if name not in self.profiles:
            raise KeyError(name)
        if len(self.profiles) <= 1:
            raise ValueError("마지막 남은 프로필은 지울 수 없습니다.")
        if name == self.profile:
            self.switch_profile(next(n for n in self.profiles if n != name))
        del self.profiles[name]
        self.save()

    def _write_mod_meta(self, mod: Mod, mod_dir: Path | None = None) -> None:
        fsutil.atomic_write_json((mod_dir or self.mods_dir / mod.id) / "mod.json", mod.to_dict())

    def _cleanup_temp(self) -> None:
        """Remove leftovers of interrupted installs (old enough not to belong to a running one)."""
        cutoff = time.time() - STALE_TEMP_SECONDS
        leftovers = [d for d in self.mods_dir.iterdir() if d.name.startswith(".")]
        if self.temp_root.is_dir():
            leftovers += list(self.temp_root.iterdir())
        for d in leftovers:
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    fsutil.rmtree(d)
            except OSError as exc:
                log.warning("임시 폴더를 지우지 못했습니다: %s (%s)", d, exc)

    def _changed(self) -> None:
        self.revision += 1
        self.dirty = True
        self.save()

    # ------------------------------------------------------------- game folder
    def deployer(self, game_dir: Path | None = None) -> Deployer:
        game = game_dir or self.game_dir
        if not game:
            raise DeployError("게임 폴더가 설정되지 않았습니다.")
        return Deployer(game, self.data_dir)

    def detect_game_dir(self) -> Path | None:
        return steam.find_game_dir()

    def set_game_dir(self, path, progress: ProgressFn | None = None) -> DeployReport | None:
        """Switch to another game folder, removing mods from the old one first."""
        new = Path(path)
        report = None
        manifest = Deployer(new, self.data_dir).load_manifest()
        bound = Deployer.bound_game_dir(manifest)
        if bound and not fsutil.same_path(bound, new):
            old = Path(bound)
            if old.is_dir():
                log.info("이전 게임 폴더에서 모드를 해제합니다: %s", old)
                report = Deployer(old, self.data_dir).run([], progress)
                if report.errors:
                    raise DeployError("이전 게임 폴더에서 모드를 해제하지 못했습니다:\n" + "\n".join(report.errors[:5]))
            else:
                log.warning("이전 게임 폴더(%s)가 없어 배포 기록만 지웁니다.", old)
                fsutil.remove_file(self.data_dir / "deployment.json")
        self.game_dir = new
        self.revision += 1
        self.dirty = any(m.enabled for m in self.mods)
        self.save()
        return report

    def refw_installed(self) -> bool:
        return bool(self.game_dir) and (self.game_dir / REFRAMEWORK_DLL).is_file()

    def needs_refw(self) -> bool:
        """Enabled mods only work with REFramework: loose files, scripts and PAKs (loaded from pak_mods)."""
        return any(self.mod_kinds(m.id) & {"loose", "script", "pak"} for m in self.mods if m.enabled)

    # ------------------------------------------------------------- queries
    def get(self, mod_id: str) -> Mod:
        for m in self.mods:
            if m.id == mod_id:
                return m
        raise KeyError(mod_id)

    def find(self, ref: str) -> Mod | None:
        """Look a mod up by id, 1-based position or (case-insensitive) name."""
        for m in self.mods:
            if m.id == ref:
                return m
        if ref.isdigit() and 1 <= int(ref) <= len(self.mods):
            return self.mods[int(ref) - 1]
        return self.find_by_name(ref)

    def find_by_name(self, name: str) -> Mod | None:
        key = name.casefold().strip()
        for m in self.mods:
            if m.name.casefold().strip() == key:
                return m
        return None

    def mod_dir(self, mod_id: str) -> Path:
        return self.mods_dir / mod_id

    def files_dir(self, mod_id: str) -> Path:
        return self.mods_dir / mod_id / "files"

    def option_dir(self, mod_id: str, option_id: str) -> Path:
        return self.mods_dir / mod_id / "options" / option_id

    def option_files(self, mod_id: str, option_id: str) -> list[str]:
        key = (mod_id, option_id)
        if key not in self._option_cache:
            self._option_cache[key] = list_files(self.option_dir(mod_id, option_id))
        return self._option_cache[key]

    def mod_entries(self, mod_id: str) -> dict[str, tuple[Path, str]]:
        """Files the mod puts into the game: game path -> (stored file, source key).

        For a mod with options these are the files of the selected options; when two
        options contain the same path the later option wins. Repaired materials take
        the place of the mod's own files (under the game path the current game uses)."""
        entries = self._entries_cache.get(mod_id)
        if entries is not None:
            return entries
        mod = self.get(mod_id)
        repaired = self.repaired_materials(mod_id)

        def entry(base: Path, rel: str, key: str) -> tuple[str, tuple[Path, str]]:
            if key in repaired:
                new_rel, path = repaired[key]
                return new_rel, (path, f"{MATFIX_DIR}/{key}")
            return rel, (base / rel, key)

        entries = {}
        if not mod.options:
            base = self.files_dir(mod_id)
            entries = dict(entry(base, rel, rel) for rel in list_files(base))
        else:
            by_key: dict[str, str] = {}
            for opt in mod.options:
                if opt["id"] not in mod.selected:
                    continue
                base = self.option_dir(mod_id, opt["id"])
                for rel in self.option_files(mod_id, opt["id"]):
                    rel, item = entry(base, rel, f"{opt['id']}/{rel}")
                    k = path_key(rel)
                    if k in by_key:
                        del entries[by_key[k]]
                    by_key[k] = rel
                    entries[rel] = item
        self._entries_cache[mod_id] = entries
        return entries

    def mod_files(self, mod_id: str) -> list[str]:
        return list(self.mod_entries(mod_id))

    def mod_kinds(self, mod_id: str) -> set[str]:
        return classify(self.mod_files(mod_id))

    def mod_size(self, mod_id: str) -> int:
        if mod_id not in self._size_cache:
            total = 0
            for path, _ in self.mod_entries(mod_id).values():
                st = fsutil.stat_or_none(path)
                total += st.st_size if st else 0
            self._size_cache[mod_id] = total
        return self._size_cache[mod_id]

    def screenshot_path(self, mod: Mod) -> Path | None:
        if not mod.screenshot:
            return None
        p = self.mod_dir(mod.id) / mod.screenshot
        return p if p.is_file() else None

    def option_preview(self, mod_id: str, option: dict) -> Path | None:
        """The picture an option of a mod came with (Fluffy Mod Manager style), if any."""
        rel = option.get("preview")
        if not isinstance(rel, str) or not re.fullmatch(rf"{OPTION_PREVIEWS}/[\w.-]+", rel) or ".." in rel:
            return None
        p = self.mod_dir(mod_id) / rel
        return p if p.is_file() else None

    def mod_picture(self, mod_id: str) -> Path | None:
        """The picture that shows a mod as it is used: the first selected option's own picture, else the
        mod's (what the archive came with; None without one)."""
        mod = self.get(mod_id)
        for opt in mod.options:
            if opt["id"] in mod.selected:
                found = self.option_preview(mod_id, opt)
                if found is not None:
                    return found
        return self.screenshot_path(mod)

    def conflicts(self) -> Conflicts:
        owners: dict[str, list[str]] = {}
        paths: dict[str, str] = {}
        for m in self.mods:
            if not m.enabled:
                continue
            for rel in self.mod_files(m.id):
                if is_root_pak(rel):
                    continue
                key = path_key(rel)
                ids = owners.setdefault(key, [])
                if not ids or ids[-1] != m.id:
                    ids.append(m.id)
                paths.setdefault(key, rel)
        multi = {k: v for k, v in owners.items() if len(v) > 1}
        return Conflicts(multi, {k: paths[k] for k in multi})

    def hidden_mods(self, conflicts: Conflicts | None = None) -> list[Mod]:
        """Enabled mods whose files all lose to other mods, so nothing of them reaches the game."""
        conflicts = conflicts or self.conflicts()
        hidden = []
        for m in self.mods:
            if not m.enabled:
                continue
            wins, losses = conflicts.counts(m.id)
            if losses and not wins and losses == sum(1 for f in self.mod_files(m.id) if not is_root_pak(f)):
                hidden.append(m)
        return hidden

    # ------------------------------------------------------------- edits
    def set_enabled(self, mod_id: str, enabled: bool) -> None:
        mod = self.get(mod_id)
        if mod.enabled != enabled:
            mod.enabled = enabled
            self._changed()

    def set_options(self, mod_id: str, selected: list[str]) -> None:
        """Choose which options of a mod are used (kept in option order)."""
        mod = self.get(mod_id)
        wanted = set(selected)
        new = [o["id"] for o in mod.options if o["id"] in wanted]
        if new == mod.selected:
            return
        mod.selected = new
        self._invalidate(mod_id)
        self._write_mod_meta(mod)
        if mod.enabled:
            self._changed()
        else:
            self.revision += 1
            self.save()

    def move_to(self, mod_id: str, index: int) -> None:
        mod = self.get(mod_id)
        old = self.mods.index(mod)
        index = max(0, min(index, len(self.mods) - 1))
        if old == index:
            return
        self.mods.pop(old)
        self.mods.insert(index, mod)
        self._changed()

    def move(self, mod_id: str, delta: int) -> None:
        self.move_to(mod_id, self.mods.index(self.get(mod_id)) + delta)

    def rename(self, mod_id: str, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("이름이 비어 있습니다.")
        mod = self.get(mod_id)
        mod.name = name
        self._write_mod_meta(mod)
        self.save()

    def remove(self, mod_id: str) -> None:
        """Forget a mod and delete its stored files. Deployed copies go on the next deploy."""
        mod = self.get(mod_id)
        self.mods.remove(mod)
        self._invalidate(mod_id)
        self.revision += 1
        if mod.enabled:
            self.dirty = True
        self.save()
        trash = self.mods_dir / f".{mod_id}.removed-{uuid.uuid4().hex[:6]}"
        try:
            os.replace(self.mod_dir(mod_id), trash)
            fsutil.rmtree(trash)
        except OSError as exc:
            log.warning("모드 폴더를 완전히 지우지 못했습니다: %s", exc)

    def update_settings(self, **changes) -> None:
        for k, v in changes.items():
            if hasattr(self.settings, k):
                setattr(self.settings, k, v)
        self.save()

    # ------------------------------------------------------------- install
    def prepare_install(self, path) -> InstallSession:
        src = Path(path)
        if not src.exists():
            raise InstallError(f"파일을 찾을 수 없습니다: {src}")
        if src.is_dir():
            return InstallSession(src, src, None, analyze(src, src.name))
        ext = src.suffix.lower()
        if not is_archive(src) and ext not in SINGLE_FILE_EXTS:
            raise InstallError(f"지원하지 않는 파일 형식입니다: {src.name}")
        self.temp_root.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix="install-", dir=self.temp_root))
        base = temp / "content"
        try:
            if is_archive(src):
                extract(src, base)
            else:
                fsutil.copy_file(src, base / src.name)
            return InstallSession(src, base, temp, analyze(base, src.stem))
        except BaseException:
            fsutil.rmtree(temp)
            raise

    def install_variant(self, session: InstallSession, variant: Variant, replace_id: str | None = None) -> Mod:
        return self.install_package(session, [variant], replace_id)

    def install_package(self, session: InstallSession, variants: list[Variant], replace_id: str | None = None,
                        selected: list[int] | None = None) -> Mod:
        """Install ``variants`` as one mod. Several variants become options of that mod.

        ``selected`` lists the indexes of options to switch on; by default none are, and
        an updated mod keeps the options (by name) that were on before."""
        variants = [v for v in variants if v.files]
        if not variants:
            raise InstallError("설치할 파일이 없습니다.")
        multi = len(variants) > 1
        mod_id = replace_id or uuid.uuid4().hex[:10]
        stage = self.mods_dir / f".{mod_id}.staging-{uuid.uuid4().hex[:6]}"
        options = [{"id": f"o{i + 1}", "name": v.option or v.name} for i, v in enumerate(variants)] if multi else []
        try:
            for i, variant in enumerate(variants):
                target = stage / "options" / options[i]["id"] if multi else stage / "files"
                seen = set()
                for src_rel, dst_rel in variant.files:
                    key = path_key(dst_rel)
                    if key in seen:
                        log.warning("같은 경로의 파일이 여러 개라 마지막 것을 사용합니다: %s", dst_rel)
                    seen.add(key)
                    src = session.base / src_rel
                    dst = target / dst_rel
                    if session.owns_files:
                        fsutil.move_file(src, dst)
                    else:
                        fsutil.copy_file(src, dst)
                fsutil.makedirs(target)
            screenshot = self._store_pictures(session.base, [v.screenshot for v in variants], options, stage)

            info = next((v.info for v in variants if v.info), None)
            version = next((v.version for v in variants if v.version), "")
            now = _now()
            old_selected: set[str] = set()
            if replace_id:
                mod = self.get(replace_id)
                mod.updated_at = now
                old_selected = {o["name"] for o in mod.options if o["id"] in mod.selected}
            else:
                name = package_name(variants) if multi else variants[0].name
                mod = Mod(id=mod_id, name=self._unique_name(name),
                          enabled=self.settings.enable_on_install, installed_at=now)
            ref = nexus.parse_source_name(session.source.name)
            if ref and ref.mod_id != mod.nexus_id:
                mod.requirements, mod.requirements_checked = [], 0  # another Nexus mod now
            if ref:
                mod.nexus_id, mod.nexus_file_time, mod.nexus_exact = ref.mod_id, ref.uploaded, ref.exact
                mod.nexus_unlinked = False
            else:
                mod.nexus_file_time = 0  # keep a manually linked page, but the file is unknown
            mod.update, mod.nexus_checked = {}, 0
            mod.options = options
            if selected is not None:
                mod.selected = [options[i]["id"] for i in selected if 0 <= i < len(options)]
            else:
                mod.selected = [o["id"] for o in options if o["name"] in old_selected]
            mod.version = version or (mod.version if replace_id else "")
            mod.author = (info.author if info else "") or (mod.author if replace_id else "")
            mod.description = (info.description if info else "") or (mod.description if replace_id else "")
            mod.category = (info.category if info else "") or (mod.category if replace_id else "")
            mod.source = session.source.name
            mod.screenshot = screenshot
            mod.pictures_read = True
            mod.lua_requires = requirements.required_modules(self._stored_files(stage))
            mod.lua_scanned = True
            self._write_mod_meta(mod, stage)

            final = self.mod_dir(mod_id)
            if final.exists():
                old = self.mods_dir / f".{mod_id}.old-{uuid.uuid4().hex[:6]}"
                os.replace(final, old)
                os.replace(stage, final)
                fsutil.rmtree(old)
            else:
                os.replace(stage, final)
        except BaseException:
            fsutil.rmtree(stage)
            raise

        self._invalidate(mod_id)
        if not replace_id:
            self.mods.append(mod)
        if mod.enabled:
            self.revision += 1
            self.dirty = True
        self._remember_archive_dir(session.source)
        self.save()
        if mod.options:
            log.info("설치 완료: %s (옵션 %d개, 사용할 옵션을 골라 주세요)", mod.name, len(mod.options))
        else:
            log.info("설치 완료: %s (%d개 파일)", mod.name, len(self.mod_files(mod_id)))
        return mod

    @staticmethod
    def _store_pictures(base: Path, shots: list, options: list[dict], target: Path, main: bool = True) -> str:
        """Copy an extracted archive's pictures into a mod folder: the mod's own (the first of ``shots``
        that exists; not with ``main=False``) and, unless every option has the same one, each option's
        into previews/. ``shots``: archive paths (or None) in the order of ``options`` (one entry for a
        mod without options). Returns the mod's picture ("" without one or with ``main=False``)."""
        shots = [s if s and (base / s).is_file() else None for s in shots]
        shot = next((s for s in shots if s), None)
        if not shot:
            return ""
        screenshot = "preview" + Path(shot).suffix.lower() if main else ""
        if main:
            fsutil.copy_file(base / shot, target / screenshot)
        # options show their own picture, unless every option has the same one
        pictures = [hashlib.sha1((base / s).read_bytes()).digest() if s else None for s in shots] if options else []
        if len(set(pictures)) > 1:
            for opt, s in zip(options, shots):
                if s:
                    opt["preview"] = f"{OPTION_PREVIEWS}/{opt['id']}{Path(s).suffix.lower()}"
                    fsutil.copy_file(base / s, target / opt["preview"])
        return screenshot

    def _remember_archive_dir(self, source: Path) -> None:
        folder = os.path.abspath(Path(source).parent)
        if not self._user_folder(folder):
            return  # e.g. a REFramework download in the app's temp folder
        key = os.path.normcase(folder)
        dirs = [d for d in self.settings.archive_dirs if isinstance(d, str) and os.path.normcase(d) != key]
        self.settings.archive_dirs = [folder, *dirs][:MAX_ARCHIVE_DIRS]

    def _user_folder(self, folder) -> bool:
        """A folder of the user's (absolute, not the app's own data folder)."""
        return os.path.isabs(folder) and not (fsutil.is_within(folder, self.data_dir)
                                             or os.path.normcase(os.path.abspath(folder))
                                             == os.path.normcase(os.path.abspath(self.data_dir)))

    def archive_folders(self) -> list[Path]:
        """Where mod archives are probably kept: the folders mods were installed from, Downloads, Desktop."""
        home = Path.home()
        found, seen = [], set()
        for d in [*self.settings.archive_dirs, home / "Downloads", home / "Desktop"]:
            if not isinstance(d, (str, Path)) or not self._user_folder(d):
                continue
            p = Path(d)
            key = os.path.normcase(str(p))
            if key not in seen and p.is_dir():
                seen.add(key)
                found.append(p)
        return found

    def find_archive(self, mod_id: str, folders: list[Path] | None = None) -> Path | None:
        """The archive (or folder) a mod was installed from, if it is still in one of ``folders``
        (archive_folders() by default)."""
        name = self.get(mod_id).source
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            return None
        for folder in self.archive_folders() if folders is None else folders:
            p = folder / name
            if p.exists():
                return p
        return None

    def read_option_pictures(self, mod_id: str, source, progress=None) -> int:
        """Take the options' pictures from the archive a mod was installed from (for mods installed
        before options had pictures); its files stay as they are. Options are matched by name (by
        position when no name matches). Returns how many options got a picture of their own."""
        mod = self.get(mod_id)
        if not mod.options:
            raise InstallError("옵션이 있는 모드가 아닙니다.")
        if progress:
            progress(0, 1, f"압축 푸는 중: {Path(source).name}")
        with self.prepare_install(source) as session:
            variants = [v for v in session.variants if v.files]
            by_name = {}
            for v in variants:
                by_name.setdefault(v.option or v.name, v)
            matched = [by_name.get(o["name"]) for o in mod.options]
            if not any(matched) and len(variants) == len(mod.options):
                matched = variants
            if not any(matched):
                raise InstallError("이 압축 파일에서 모드의 옵션을 찾지 못했습니다. 이 모드를 설치한 압축 파일인지 확인해 주세요.")
            target = self.mod_dir(mod_id)
            options = copy.deepcopy(mod.options)
            for opt in options:
                opt.pop("preview", None)
            fsutil.rmtree(target / OPTION_PREVIEWS)
            keep = self.screenshot_path(mod) is not None  # the mod's own picture stays as it is
            screenshot = self._store_pictures(session.base, [v.screenshot if v else None for v in matched],
                                              options, target, main=not keep)
        mod.options = options
        if screenshot:
            mod.screenshot = screenshot
        mod.pictures_read = True
        self._write_mod_meta(mod)
        self._remember_archive_dir(Path(source))
        self.save()
        count = sum(1 for o in options if self.option_preview(mod_id, o))
        log.info("옵션 그림을 가져왔습니다: %s (%d개)", mod.name, count)
        return count

    # ------------------------------------------------------------- materials
    def game_version(self, refresh: bool = False) -> str:
        """Fingerprint of the game's pak files ("" without a game): it changes with every game update."""
        game = str(self.game_dir) if self.game_dir else ""
        if refresh or self._game_version is None or self._game_version[0] != game:
            try:
                found = game_fingerprint(self.game_dir) if self.game_dir and self.game_dir.is_dir() else ""
            except OSError:
                found = ""
            self._game_version = (game, found)
        return self._game_version[1]

    def material_files(self, mod_id: str) -> list[tuple[str, str, Path]]:
        """(source key, game path, original file) of the mod's materials (.mdf2), RSZ files (.user, .pfb,
        .scn) and PAKs, every option included. Of several versions of one file only the newest counts:
        the game reads one of them."""
        cached = self._material_cache.get(mod_id)
        if cached is not None:
            return cached
        mod = self.get(mod_id)
        if mod.options:
            scopes = [(f"{o['id']}/", self.option_dir(mod_id, o["id"]), self.option_files(mod_id, o["id"]))
                      for o in mod.options]
        else:
            scopes = [("", self.files_dir(mod_id), list_files(self.files_dir(mod_id)))]
        found = []
        for prefix, base, rels in scopes:
            newest: dict[tuple[str, str], tuple[int, str]] = {}
            for rel in rels:
                if not is_material_file(rel):
                    continue
                m = VERSIONED_RE.search(rel)
                if m is None:
                    found.append((prefix + rel, rel, base / rel))
                    continue
                stem = (path_key(rel[:m.start()]), m.group(1).lower())
                if stem not in newest or int(m.group(2)) > newest[stem][0]:
                    newest[stem] = (int(m.group(2)), rel)
            found += [(prefix + rel, rel, base / rel) for _, rel in newest.values()]
        self._material_cache[mod_id] = found
        return found

    def _material_record(self, mod_id: str) -> dict:
        record = self._record_cache.get(mod_id)
        if record is None:
            record = fsutil.read_json(self.mod_dir(mod_id) / MATFIX_RECORD, {})
            record = record if isinstance(record, dict) else {}
            self._record_cache[mod_id] = record
        return record

    def _save_material_record(self, mod_id: str, record: dict) -> None:
        fsutil.atomic_write_json(self.mod_dir(mod_id) / MATFIX_RECORD, record)
        self._invalidate(mod_id)

    def repaired_materials(self, mod_id: str) -> dict[str, tuple[str, Path]]:
        """Source key -> (game path, repaired file) of the repairs in use."""
        applied = self._material_record(mod_id).get("applied")
        out = {}
        for key, item in (applied.items() if isinstance(applied, dict) else ()):
            if isinstance(item, dict) and isinstance(item.get("rel"), str) and isinstance(item.get("file"), str) \
                    and item["file"].startswith(MATFIX_DIR + "/"):
                path = self.mod_dir(mod_id) / item["file"]
                if path.is_file():
                    out[key] = (item["rel"], path)
        return out

    def material_results(self, mod_id: str) -> list[FileResult]:
        """What the last check found, per file (an empty list before the first check)."""
        results = self._material_record(mod_id).get("results")
        found = [FileResult.from_dict(r) for r in results if isinstance(r, dict)] if isinstance(results, list) else []
        return [r for r in found if r is not None]

    def registry_date(self, refresh: bool = False) -> str:
        """Date of the .user/.pfb type data in use (the bundled one or a newer download)."""
        if refresh or self._registry_date is None:
            self._registry_date = rsz.registry_date(rsz.TypeRegistry.path())
        return self._registry_date

    def material_stuck(self, mod_id: str) -> list[dict]:
        """RSZ files (.user/.pfb/.scn) that no longer fit this game version and could not be repaired:
        [{"label", "kind", "reason"}] (empty unless checked against the current game)."""
        if self.material_state(mod_id) in ("none", "unchecked"):
            return []
        return [item for r in self.material_results(mod_id) for item in r.stuck]

    def material_checked_for(self, mod_id: str) -> str:
        """The game version (fingerprint) the mod's materials were last checked against ("" never)."""
        game = self._material_record(mod_id).get("game")
        return game if isinstance(game, str) else ""

    def material_state(self, mod_id: str) -> str:
        """"none" (no materials or PAKs), "unchecked" (not against this game version), "current",
        "stale" (outdated materials, not repaired), "repaired" or "declined" (the user undid the repair)."""
        if not self.material_files(mod_id):
            return "none"
        record = self._material_record(mod_id)
        game = self.game_version()
        if not game or record.get("game") != game or record.get("format", 1) < MATFIX_FORMAT:
            return "unchecked"
        results = self.material_results(mod_id)
        if any(r.stuck for r in results) and record.get("registry", "") < self.registry_date():
            return "unchecked"  # newer .user/.pfb type data may repair what could not be repaired
        fixable = {r.key for r in results if r.state == "fixable"}
        if not fixable:
            return "current"
        if record.get("applied_game") == game and fixable <= set(self.repaired_materials(mod_id)):
            return "repaired"
        return "declined" if record.get("declined") else "stale"

    def stale_material_mods(self, enabled_only: bool = False) -> list[Mod]:
        return [m for m in self.mods if (m.enabled or not enabled_only) and self.material_state(m.id) == "stale"]

    def _offer_key(self) -> str:
        return f"{self.game_version()}/{MATFIX_FORMAT}"

    def materials_to_offer(self) -> list[Mod]:
        """Stale mods not yet offered for repair for this game version (offered once, see mark_offered)."""
        key = self._offer_key()
        return [m for m in self.stale_material_mods() if self._material_record(m.id).get("offered") != key]

    def mark_materials_offered(self, mods: list[Mod]) -> None:
        key = self._offer_key()
        for m in mods:
            record = dict(self._material_record(m.id))
            if record.get("offered") != key:
                record["offered"] = key
                self._save_material_record(m.id, record)

    @staticmethod
    def _stamp(mod: Mod) -> str:
        return f"{mod.installed_at}|{mod.updated_at}"

    def material_jobs(self, mod_ids: list[str] | None = None, unchecked_only: bool = False) -> list[MaterialJob]:
        """Mods to check (or repair): the given ones, or all; with ``unchecked_only`` only those not
        checked against the current game yet."""
        jobs = []
        if unchecked_only:
            self.game_version(refresh=True)  # the game may have been updated since
        for mod_id in (mod_ids if mod_ids is not None else [m.id for m in self.mods]):
            mod = self.get(mod_id)
            files = self.material_files(mod_id)
            if files and not (unchecked_only and self.material_state(mod_id) != "unchecked"):
                provided = {re.sub(r"\.\d+$", "", Path(rel).name).lower()  # as resource paths name them
                            for rel in self._stored_files(self.mod_dir(mod_id))}
                jobs.append(MaterialJob(mod_id, mod.name, self._stamp(mod), list(files), provided))
        return jobs

    def run_materials(self, jobs: list[MaterialJob], repair: bool = False,
                      progress: ProgressFn | None = None) -> MaterialRun:
        """Check (or repair) the jobs' mods against the current game; finish_materials stores the outcome.

        Thread-safe: reads the game's paks and the mods' own files, and writes repaired files
        only to new folders under temp."""
        progress = progress or (lambda done, total, msg: None)
        if not self.game_dir or not self.game_dir.is_dir():
            raise MatfixError("게임 폴더가 설정되지 않았습니다.")
        if not matfix.available():
            raise MatfixError("게임 파일의 압축(zstd)을 풀 수 있는 모듈이 없어 재질을 점검할 수 없습니다.")
        game = GameIndex(self.game_dir, self.data_dir / "cache", progress)
        shaders = ShaderIndex(game, progress)
        run = MaterialRun(game.fingerprint, repair)
        verb = "복구" if repair else "점검"
        context = RszContext(game)
        try:
            if any(RSZ_RE.search(rel) or is_root_pak(rel) for job in jobs for _key, rel, _path in job.files):
                self._update_registry(game, context, run, progress, only_if_outdated=True)
            run.registry = rsz.registry_date(rsz.TypeRegistry.path())
            self._run_jobs(jobs, game, shaders, run, verb, progress, context)
            if (context.game_unreadable or context.registry_outdated) and not run.registry_updated:
                self._update_registry(game, context, run, progress)
        finally:
            rsz.TypeRegistry.release()
        progress(len(jobs), len(jobs), f"재질 {verb}을 마쳤습니다")
        return run

    def _update_registry(self, game: GameIndex, context: RszContext, run: MaterialRun, progress,
                         only_if_outdated: bool = False) -> None:
        """Download newer .user/.pfb type data when the bundled data does not match the game
        (at most once per game version every few days)."""
        cache = self.data_dir / "cache"
        state_file = cache / "rsz_update.json"
        state = fsutil.read_json(state_file, {})
        state = state if isinstance(state, dict) else {}
        if state.get("game") == game.fingerprint and time.time() - float(state.get("tried") or 0) < REGISTRY_RETRY:
            return
        try:
            if only_if_outdated and context.matches_game() is not False:
                return
        except (OSError, ValueError, EOFError) as exc:
            log.warning(".user·.pfb 형식 정보를 읽지 못했습니다: %s", exc)
            return
        fsutil.atomic_write_json(state_file, {"game": game.fingerprint, "tried": time.time()})
        progress(0, 0, "새 .user·.pfb 형식 정보를 찾는 중...")
        try:
            date = rsz.download_registry(cache)
        except (OSError, ValueError, EOFError, lzma.LZMAError) as exc:
            log.info("새 .user·.pfb 형식 정보를 받지 못했습니다: %s", exc)
            return
        if date:
            rsz.TypeRegistry.release()
            run.registry_updated = date
            log.info("새 .user·.pfb 형식 정보를 받았습니다 (%s 기준)", date)

    def _run_jobs(self, jobs, game, shaders, run, verb, progress, context) -> None:
        for n, job in enumerate(jobs):
            progress(n, len(jobs), f"재질 {verb} 중... {job.name}")
            out = None
            if run.repair:
                job.stage = self.temp_root / f"matfix-{job.mod_id}-{uuid.uuid4().hex[:6]}"
                numbers = itertools.count(1)
                out = lambda result, stage=job.stage, numbers=numbers: \
                    stage / f"{next(numbers):03d}_{result.new_rel.rsplit('/', 1)[-1]}"
            try:
                run.results[job.mod_id] = check_mod(job.files, game, shaders, out, provided=job.provided,
                                                    rszs=context)
            except Exception as exc:  # one broken mod must not stop the others
                log.warning("%s: 재질을 %s하지 못했습니다: %s", job.name, verb, exc)
                run.errors[job.mod_id] = str(exc)

    def export_material_report(self, job: MaterialJob, target: Path, progress: ProgressFn | None = None) -> int:
        """Write a zip with the mod's .user/.pfb/.scn files, the current game's copies of them, the
        repaired versions in use and what the last check found - to find out why one could not be
        repaired. Thread-safe (``job`` from material_jobs). Returns the number of files compared."""
        from . import __version__

        progress = progress or (lambda done, total, msg: None)
        if not self.game_dir or not self.game_dir.is_dir():
            raise MatfixError("게임 폴더가 설정되지 않았습니다.")
        if not matfix.available():
            raise MatfixError("게임 파일의 압축(zstd)을 풀 수 있는 모듈이 없습니다.")
        game = GameIndex(self.game_dir, self.data_dir / "cache", progress)
        record = fsutil.read_json(self.mod_dir(job.mod_id) / MATFIX_RECORD, {})
        registry_meta = {}
        try:
            registry_meta = rsz.TypeRegistry.load().meta
        except (OSError, ValueError) as exc:
            registry_meta = {"error": str(exc)}
        finally:
            rsz.TypeRegistry.release()
        count = 0
        partial = target.with_name(target.name + ".partial")
        try:
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
                for label, kind, mod_bytes, donor_path, donor_bytes in rsz_sources(job.files, game):
                    progress(count, 0, f"진단 파일 만드는 중... {label.rsplit('/', 1)[-1]}")
                    name = label.replace("#", "_")
                    zf.writestr(f"mod/{name}", mod_bytes)
                    if donor_bytes is not None:
                        zf.writestr(f"game/{name}", donor_bytes)
                    count += 1
                for key, (rel, path) in self.repaired_materials(job.mod_id).items():
                    if not rel.lower().endswith(".pak"):
                        zf.write(path, f"repaired/{rel}")
                zf.writestr("report.json", json.dumps({
                    "app_version": __version__, "mod": job.name, "game": game.fingerprint,
                    "game_paks": [p.name for p in game.paks], "registry": registry_meta, "record": record,
                }, ensure_ascii=False, indent=2))
            os.replace(partial, target)
        except BaseException:
            fsutil.remove_file(partial)
            raise
        return count

    def finish_materials(self, run: MaterialRun, jobs: list[MaterialJob]) -> list[Mod]:
        """Store what a run found; a repair also switches the mods to their repaired files.
        Returns the mods whose deployed files change."""
        changed = []
        now = int(time.time())
        self.game_version(refresh=True)
        self.registry_date(refresh=True)
        for job in jobs:
            try:
                mod = self.get(job.mod_id)
            except KeyError:
                mod = None
            results = run.results.get(job.mod_id)
            try:
                if mod is None or results is None or self._stamp(mod) != job.stamp:
                    continue  # removed or reinstalled meanwhile, or could not be read
                record = dict(self._material_record(mod.id))
                record.update(game=run.game, format=MATFIX_FORMAT, registry=run.registry, checked=now,
                              results=[r.to_dict() for r in results])
                had = bool(self.repaired_materials(mod.id))
                target = self.mod_dir(mod.id) / MATFIX_DIR
                if run.repair:
                    applied = {r.key: {"rel": r.new_rel, "file": f"{MATFIX_DIR}/{r.file.name}"}
                               for r in results if r.state == "fixable" and r.file is not None}
                    fsutil.rmtree(target)
                    if applied:
                        os.replace(job.stage, target)
                    record.update(applied=applied, applied_game=run.game if applied else "", declined=False)
                    if applied or had:
                        changed.append(mod)
                    if applied:
                        log.info("재질 복구: %s (%d개 파일)", mod.name, len(applied))
                elif had and not any(r.state == "fixable" for r in results):
                    # the mod's own files fit this game version: the repaired copies are not needed now
                    fsutil.rmtree(target)
                    record.update(applied={}, applied_game="")
                    changed.append(mod)
                self._save_material_record(mod.id, record)
            except OSError as exc:
                log.error("%s: 재질 복구 결과를 저장하지 못했습니다: %s", job.name, exc)
            finally:
                if job.stage is not None:
                    fsutil.rmtree(job.stage)
        if changed:
            self.revision += 1
            self.dirty = self.dirty or any(m.enabled for m in changed)
            self.save()
        return changed

    def restore_materials(self, mod_id: str) -> bool:
        """Use the mod's own material files again (it no longer counts as a problem).
        Returns True when the deployed files change."""
        mod = self.get(mod_id)
        record = dict(self._material_record(mod_id))
        had = bool(self.repaired_materials(mod_id))
        fsutil.rmtree(self.mod_dir(mod_id) / MATFIX_DIR)
        record.update(applied={}, applied_game="", declined=True)
        self._save_material_record(mod_id, record)
        if had:
            self.revision += 1
            self.dirty = self.dirty or mod.enabled
        self.save()
        return had

    # ------------------------------------------------------------- equipment
    def equipment_jobs(self) -> list[EquipJob]:
        """Every mod with the files it puts into the game, for equipment_scans (call on the GUI thread)."""
        return [self._equip_job(order, mod) for order, mod in enumerate(self.mods)]

    def equipment_job(self, mod_id: str) -> EquipJob:
        """One mod's EquipJob (for equipment_preview; call on the GUI thread). KeyError for an unknown mod."""
        mod = self.get(mod_id)
        return self._equip_job(self.mods.index(mod), mod)

    def _equip_job(self, order: int, mod: Mod) -> EquipJob:
        files = {rel: path for rel, (path, _key) in self.mod_entries(mod.id).items()}
        stamp = f"{self._stamp(mod)}|{','.join(mod.selected)}|{len(files)}"
        return EquipJob(mod.id, mod.name, mod.enabled, order, stamp, files)

    def _equip_game(self, progress: ProgressFn | None = None) -> GameIndex | None:
        """The game's pak index (None without a readable game)."""
        if not self.game_dir or not self.game_dir.is_dir() or not matfix.available():
            return None
        with self._equip_lock:
            version = self.game_version()
            index = self._equip_game_index
            if index is None or index.fingerprint != version or not fsutil.same_path(index.game_dir, self.game_dir):
                self._equip_game_index = GameIndex(self.game_dir, self.data_dir / "cache", progress)
                self._equip_failed.clear()  # pictures read through the old index
            return self._equip_game_index

    def equipment_catalog(self, progress: ProgressFn | None = None, rebuild: bool = False,
                          build: bool = True):
        """Names, rarity and preview pictures of the game's equipment, read from the game itself and
        cached per game version (None without a game). ``build=False``: only a cached one."""
        version = self.game_version(refresh=build)
        if not version:
            return None
        with self._equip_lock:
            if self._equip_catalog and self._equip_catalog[0] == version and not rebuild:
                return self._equip_catalog[1]
            # the catalog depends on the game, the build rules, the NPC files of the bundled path list and
            # the .user type data (a table it cannot read exactly has no names)
            cache = self.data_dir / "cache" / (f"equip-catalog-{version}-{equip_catalog.CATALOG_FORMAT}-"
                                               f"{equip_pathlist.list_version()[:8]}-{self.registry_date()}.json")
            if not rebuild and cache.is_file():
                try:
                    found = equip_catalog.Catalog.from_json(cache.read_text(encoding="utf-8"))
                except (OSError, ValueError, EquipError) as exc:
                    log.info("장비 정보 캐시를 다시 만듭니다: %s", exc)
                else:
                    self._equip_catalog = (version, found)
                    return found
            if not build:
                return None
            game = self._equip_game(progress)
            if game is None:
                return None
            if rebuild:  # read everything again, pictures and icons too
                self._equip_failed.clear()
                self._preview_failed.clear()  # e.g. a full disk since freed
                self._preview_textures = None
                folder = self._picture_dir()
                if folder is not None:
                    (folder / "ui.json").unlink(missing_ok=True)
            try:
                data = equip_catalog.build(game, rsz.TypeRegistry.load(), progress,
                                           npc_settings=equip_pathlist.npc_visual_settings())
            finally:
                rsz.TypeRegistry.release()
            found = equip_catalog.Catalog(data)
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                for old in cache.parent.glob("equip-catalog-*.json"):
                    old.unlink(missing_ok=True)  # from before a game update
                fsutil.atomic_write_json(cache, found.data)
            except OSError as exc:
                log.warning("장비 정보를 저장하지 못했습니다: %s", exc)
            self._equip_catalog = (version, found)
            return found

    def _equip_scan_file(self) -> Path:
        return self.data_dir / "cache" / "equip-scans.json"

    def _load_equip_scans(self) -> dict[str, tuple[str, object]]:
        if self._equip_scans is None:
            self._equip_scans = {}
            try:
                saved = json.loads(self._equip_scan_file().read_text(encoding="utf-8"))
                if saved.get("paths") == equip_pathlist.list_version():  # each scan carries its format
                    for mod_id, (stamp, scan) in saved.get("mods", {}).items():
                        found = equip_scan.ModScan.from_dict(scan)
                        if found is not None:
                            self._equip_scans[mod_id] = (str(stamp), found)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
        return self._equip_scans

    def equipment_scans(self, jobs: list[EquipJob], progress: ProgressFn | None = None) -> dict:
        """mod id -> ModScan: the equipment each job's files belong to (cached until the mod changes).
        Thread-safe: reads the mods' stored files only."""
        progress = progress or (lambda done, total, msg: None)
        with self._equip_lock:
            scans = self._load_equip_scans()
            out, changed = {}, False
            for i, job in enumerate(jobs):
                cached = scans.get(job.mod_id)
                if cached is not None and cached[0] == job.stamp:
                    out[job.mod_id] = cached[1]
                    continue
                if self._equip_paths is None and any(rel.lower().endswith(".pak") for rel in job.files):
                    progress(i, len(jobs), "게임 파일 이름 목록 준비 중...")
                    self._equip_paths = equip_pathlist.PathIndex(self.data_dir / "cache")
                progress(i, len(jobs), f"모드 파일 살펴보는 중... {job.name}")
                scan = equip_scan.scan_files(job.files, self._equip_paths)
                scans[job.mod_id] = (job.stamp, scan)
                out[job.mod_id] = scan
                changed = True
            known = {m.id for m in self.mods}
            for mod_id in [k for k in scans if k not in known]:
                del scans[mod_id]
                changed = True
            if changed:
                saved = {"paths": equip_pathlist.list_version(),
                         "mods": {k: [stamp, scan.to_dict()] for k, (stamp, scan) in scans.items()}}
                try:
                    fsutil.atomic_write_json(self._equip_scan_file(), saved)
                except OSError as exc:
                    log.debug("장비 검사 결과를 저장하지 못했습니다: %s", exc)
            return out

    def equipment_summary(self, mod_id: str) -> list[str]:
        """Short names of what a mod changes, from what was already worked out (never reads the game)."""
        mod = self.get(mod_id)
        if not self._equip_lock.acquire(blocking=False):
            return []  # being worked out right now (the GUI shows it once that is done)
        try:
            cached = self._load_equip_scans().get(mod_id)
            if not cached or cached[0] != self._equip_job(0, mod).stamp:
                return []
            catalog = self.equipment_catalog(build=False)
        finally:
            self._equip_lock.release()
        items = equip_overview.build([(mod_id, mod.name, mod.enabled, 0, cached[1])], catalog).items
        out = []
        for item in items:
            parts = [PART_LABELS.get(p, p) for p in item.changed if p]
            out.append(f"{item.name} ({item.subtitle}{': ' + '·'.join(parts) if parts else ''})")
        return out

    def _picture_dir(self) -> Path | None:
        version = self.game_version()
        if not version:
            return None
        base = self.data_dir / "cache" / "equip-pictures"
        here = base / f"{version[:16]}-{equip_icons.PICTURE_FORMAT}"
        if not here.is_dir():
            try:
                for old in base.glob("*"):  # from before a game update
                    fsutil.rmtree(old)
                here.mkdir(parents=True, exist_ok=True)
            except OSError:
                return None
        return here

    def equipment_thumbnail(self, tex_path: str, size: int) -> Path | None:
        """A game preview picture as a PNG of at most size x size (cached; None when it cannot be read
        or Pillow is missing). Safe to call from a worker thread."""
        key = (tex_path, size)
        if key in self._equip_failed:
            return None
        folder = self._picture_dir()
        if folder is None:
            return None
        target = folder / f"{hashlib.sha1(tex_path.lower().encode('utf-8')).hexdigest()[:20]}_{size}.png"
        if target.is_file():
            return target
        game = self._equip_game()
        data = game.read(tex_path) if game is not None else None
        try:
            if data is None:
                raise EquipError("게임에 없는 그림")
            image = equip_tex.decode(data, max_side=size)
            image.thumbnail((size, size))
            tmp = target.with_name(f"{target.name}.{os.getpid()}-{threading.get_ident()}.tmp")
            image.save(tmp, "PNG")
            tmp.replace(target)
            return target
        except (EquipError, OSError, ValueError) as exc:
            log.debug("장비 그림을 만들지 못했습니다 (%s): %s", tex_path, exc)
            self._equip_failed.add(key)
            return None

    # ------------------------------------------------------------- 3D previews
    def _preview_dir(self) -> Path | None:
        """cache/equip-previews/<game version>-<formats>/ ("nogame" without a game); the folders of other
        versions are removed. None when it cannot be made."""
        version = self.game_version() or "nogame"
        base = self.data_dir / "cache" / "equip-previews"
        here = base / f"{version[:16]}-{self.preview_formats()}"
        if not here.is_dir():
            with self._equip_lock:
                try:
                    for old in base.glob("*"):  # from before a game update or a new preview format
                        if old.is_dir() and old != here:
                            fsutil.rmtree(old)
                    here.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    log.warning("미리보기 폴더를 만들지 못했습니다: %s", exc)
                    return None
        return here

    @staticmethod
    def preview_formats() -> str:
        """What the 3D previews are made with besides the game: the preview rules (mesh.py, render.py,
        look.py), the texture decoding (tex.py, gdeflate.py) and the file rules (paths.py, scan.py)."""
        return f"{equip_look.PREVIEW_FORMAT}-{equip_icons.PICTURE_FORMAT}-{equip_scan.SCAN_FORMAT}"

    def _preview_key(self, job: EquipJob | None, target, size: int, compare: bool, extras: bool = True) -> str:
        if job is None:  # the game's own look: the part alone
            parts = ["game", target.kind, target.key, target.part, size]
        else:
            # the stamp changes with reinstalls and options, the file list with material repairs; a repair
            # made again (a new game, new rules) keeps the names of the repaired files, not their content
            repaired = self.mod_dir(job.mod_id) / MATFIX_DIR
            lines = []
            for rel, path in job.files.items():
                line = f"{rel}\0{path}"
                if Path(path).parent == repaired:
                    try:
                        st = Path(path).stat()
                        line += f"\0{st.st_size}\0{st.st_mtime_ns}"
                    except OSError:
                        line += "\0?"
                lines.append(line)
            listing = "\n".join(sorted(lines))
            parts = [job.mod_id, job.stamp, hashlib.sha1(listing.encode("utf-8", "replace")).hexdigest(),
                     target.kind, target.key, target.part, size, compare]
            if not extras:
                parts.append("no extras")
        parts.append(equip_pathlist.list_version()[:8])  # which meshes make up a part
        return hashlib.sha1(json.dumps(parts).encode("utf-8")).hexdigest()[:24]

    def equipment_preview(self, job: EquipJob | None, target, size: int = 256,
                          compare: bool = False, extras: bool = True) -> equip_look.Preview:
        """A small 3D picture of one equipment part (``target``, a Target with its part) with ``job``'s files
        applied - job None: the game's own look - as PNG files with a transparent background.  ``compare``
        (with a job) also draws the game's own look as ``before``, in the same camera and framing.
        ``extras=False``: without the texture swatches and colour chips (small pictures that show neither).

        Cached under cache/equip-previews/ per game version, mod files, options, size and compare.  Without
        a game the mod's own files are still drawn (the game's textures as flat colours).  Never raises: a
        broken mod gives a status and notes (Preview.text, Preview.notes).  Safe to call from a worker thread;
        the heavy work does not hold the equipment lock, and a kept picture does not wait for it."""
        try:
            size = max(32, min(1024, int(size)))
        except (TypeError, ValueError):
            size = 256
        compare = bool(compare) and job is not None
        extras = bool(extras) or job is None
        if not equip_tex.pillow():
            return equip_look.Preview("error", notes=["3D 미리보기를 그리려면 Pillow가 필요합니다"])
        folder = self._preview_dir()
        if folder is None:
            return equip_look.Preview("error", notes=["미리보기를 저장할 폴더를 만들지 못했습니다"])
        key = self._preview_key(job, target, size, compare, extras)
        failed = self._preview_failed.get((folder.name, key))  # written under the lock; a read needs none
        if failed is not None:
            return equip_look.Preview("error", notes=[failed])
        try:
            found = equip_look.Preview.from_dict(json.loads((folder / f"{key}.json").read_text(encoding="utf-8")),
                                                 folder)
        except (OSError, ValueError):
            found = None
        if found is not None:
            return found
        try:
            made, keep = self._make_preview(job, target, size, compare, folder, extras)
            return self._save_preview(folder, key, made, keep)
        except Exception as exc:  # noqa: BLE001 - a bug or a full disk must not stop the window's worker
            log.warning("3D 미리보기를 만들지 못했습니다 (%s %s %s): %s", target.kind, target.key, target.part, exc,
                        exc_info=not isinstance(exc, (OSError, EquipError)))
            note = f"{equip_look.STATUS['error']}: {exc}"
            with self._equip_lock:
                self._preview_failed[(folder.name, key)] = note  # tried again after a restart
            return equip_look.Preview("error", notes=[note])

    def _make_preview(self, job: EquipJob | None, target, size: int, compare: bool, folder: Path,
                      extras: bool = True):
        """(equip_look.Look, whether it may be kept: not when the game could not be read this time)."""
        notes = []
        try:
            game = self._equip_game()
        except (OSError, MatfixError) as exc:
            game = None
            notes.append(f"게임 파일 목록을 읽지 못했습니다: {exc}")
        mod, redirects = None, ()
        if job is not None:
            mod = equip_look.ModSource(job.files)
            try:
                scan = self._preview_scan(job)
                redirects = list(scan.redirects) if scan is not None else ()
            except Exception as exc:  # noqa: BLE001 - without the look swaps the mod's own part is drawn
                log.debug("모드 파일을 살펴보지 못했습니다 (%s): %s", job.name, exc)
        with self._equip_lock:
            textures = self._preview_textures
            if textures is None or textures.folder != folder / "textures":
                textures = self._preview_textures = equip_look.Textures(folder / "textures")
        look = equip_look.preview(target, mod, equip_look.GameSource(game) if game is not None else None, size,
                                  compare, redirects, textures, extras=extras)
        look.notes[:0] = notes
        return look, not notes

    def _preview_scan(self, job: EquipJob):
        """The job's ModScan (for its look swaps).  A job whose mod was scanned with other files since (an
        option changed while the window still draws the old pictures) is scanned for this picture only:
        it must not take the current scan's place."""
        with self._equip_lock:
            cached = self._load_equip_scans().get(job.mod_id)
            if cached is not None and cached[0] == job.stamp:
                return cached[1]
            if cached is None:  # never scanned (the command line): scanned and kept
                return self.equipment_scans([job]).get(job.mod_id)
            if self._equip_paths is None and any(rel.lower().endswith(".pak") for rel in job.files):
                self._equip_paths = equip_pathlist.PathIndex(self.data_dir / "cache")
            paths = self._equip_paths
        return equip_scan.scan_files(job.files, paths)  # the index is only read: no lock needed

    @staticmethod
    def _save_preview(folder: Path, key: str, look, keep: bool = True) -> equip_look.Preview:
        """The pictures of a Look as PNG files and, with ``keep``, its record as <key>.json (written last: a
        record is complete when it is there)."""
        def save(image, name: str) -> Path | None:
            if image is None:
                return None
            target = folder / name
            tmp = target.with_name(f"{name}.{os.getpid()}-{threading.get_ident()}.tmp")
            try:
                image.save(tmp, "PNG")
                tmp.replace(target)
            finally:
                tmp.unlink(missing_ok=True)  # left only when writing failed
            return target

        out = equip_look.Preview(look.status, save(look.image, f"{key}.png"), save(look.before, f"{key}-before.png"),
                                 list(look.notes), [], list(look.chips), look.triangles, look.changed)
        for i, (name, before, after, new) in enumerate(look.swatches):
            out.swatches.append(equip_look.Swatch(name, save(before, f"{key}-t{i}b.png"),
                                                  save(after, f"{key}-t{i}a.png"), new))
        if not keep:
            return out
        record = folder / f"{key}.json"
        tmp = record.with_name(f"{record.name}.{os.getpid()}-{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(out.to_dict(), ensure_ascii=False), encoding="utf-8")
            tmp.replace(record)
        finally:
            tmp.unlink(missing_ok=True)
        return out

    def equipment_self_test(self, items: int = equip_selftest.DEFAULT_ITEMS, seconds: float | None = None,
                            progress: ProgressFn | None = None) -> str:
        """The 3D preview's self-test on the installed game (equip/selftest.py): a text report on how
        ``items`` of the game's own equipment parts read and draw. Never raises; safe on a worker thread."""
        try:
            game = self._equip_game(progress)
        except Exception as exc:  # noqa: BLE001 - the report says it
            return f"preview self-test: 게임 파일 목록을 읽지 못했습니다 ({exc})\n"
        if game is None:
            return "preview self-test: 게임 폴더가 없거나 게임 파일을 읽을 수 없습니다 (zstandard 필요)\n"
        try:
            catalog = self.equipment_catalog(build=False)
        except Exception:  # noqa: BLE001 - without the catalog there is no thumbnail to compare with
            catalog = None

        def thumb(target):
            tex = catalog.thumb(target) if catalog is not None else None
            return self.equipment_thumbnail(tex, 64) if tex else None

        try:
            return equip_selftest.run(game, items, thumb=thumb, seconds=seconds, progress=progress)
        except Exception as exc:  # noqa: BLE001 - run() reports problems itself; this is a bug in it
            log.warning("3D 미리보기 점검이 실패했습니다: %s", exc, exc_info=True)
            return f"preview self-test failed: {type(exc).__name__}: {exc}\n"

    def equipment_ui(self) -> dict:
        """The game's rarity colours ({"1": "#rrggbb"...}), weapon-type icons ({type: PNG path}) and the
        icons of armor pieces, kinsects and palicos ({"helm"...: PNG path}), cached per game version; {}
        when the game's UI files cannot be read."""
        folder = self._picture_dir()
        if folder is None:
            return {}
        saved = folder / "ui.json"
        try:
            ui = json.loads(saved.read_text(encoding="utf-8"))
            icons = [*ui.get("weapon_icons", {}).values(), *ui.get("icons", {}).values()]
            if all(Path(p).is_file() for p in icons):
                return ui
        except (OSError, ValueError, AttributeError):
            pass
        game = self._equip_game()
        if game is None:
            return {}
        ui = {"rarity": {}, "weapon_icons": {}, "icons": {}}
        colors = equip_icons.rarity_colors(game.read)
        ui["rarity"] = {str(n): "#%02x%02x%02x" % tuple(rgb[:3]) for n, rgb in colors.items()}
        for key, icons, prefix in (("weapon_icons", equip_icons.weapon_type_icons(game.read, 48), "weapon-type-"),
                                   ("icons", equip_icons.other_icons(game.read, 48), "icon-")):
            for name, image in icons.items():
                path = folder / f"{prefix}{name}.png"
                try:
                    image.save(path, "PNG")
                    ui[key][str(name)] = str(path)
                except OSError:
                    pass
        try:
            fsutil.atomic_write_json(saved, ui)
        except OSError:
            pass
        return ui

    def export_equipment_report(self, target: Path, progress: ProgressFn | None = None) -> None:
        """A zip to find out why names or pictures are missing on a real game: the cached results, a
        check of the game files they come from, the 3D preview's self-test, the icon sheet and the log.
        Thread-safe."""
        from . import __version__

        lines = [f"app {__version__}", f"game {self.game_version()}", f"registry {self.registry_date()}",
                 f"game folder {self.game_dir}", f"zstd {matfix.available()}", f"pillow {equip_tex.pillow()}"]
        catalog = self.equipment_catalog(build=False)
        game = self._equip_game()
        if catalog is not None:
            lines.append("problems: " + "; ".join(catalog.problems))
            for kind in ("armor", "weapon", "kinsect", "palico", "seikret", "npc", "monster"):
                items = catalog.data.get("items", {}).get(kind, {})
                lines.append(f"{kind}: {len(items)} items, {sum(1 for e in items.values() if e.get('thumb'))} "
                             "with a picture")
        if game is not None:
            registry = rsz.TypeRegistry.load()
            try:
                for name in ("ArmorThumbnailTextureData", "WeaponThumbnailTextureData", "OtEquipThumnailTextureData"):
                    path = f"natives/STM/GameDesign/GUI/Common/_UserData/{name}.user.3"
                    data = game.read(path) if game.has(path) else None
                    root = equip_userdata.read_user(data, registry) if data else None
                    info = rsz.parse(data) if data else None
                    lines.append(f"{name}: {'missing' if data is None else len(data)} bytes, "
                                 f"read {'ok' if root is not None else 'FAILED'}, "
                                 f"types {sorted(registry.name(t) for t in info.types) if info else '-'}")
            finally:
                rsz.TypeRegistry.release()
            for prefab in (equip_catalog.item_thumb_prefab(2000083), equip_catalog.armor_thumb_prefab(30000510),
                           equip_catalog.palico_thumb_prefab(500100)):
                tex = equip_catalog.thumb_texture(prefab)
                lines.append(f"{tex}: {'found' if game.has(tex) else 'not in the game'}")
        selftest = self.equipment_self_test(REPORT_SELFTEST_ITEMS, REPORT_SELFTEST_SECONDS, progress)
        partial = target.with_name(target.name + ".partial")
        try:
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("report.txt", "\n".join(lines) + "\n")
                zf.writestr("preview-selftest.txt", selftest)  # text only: no game pictures
                cache = self.data_dir / "cache"
                for path in [*cache.glob("equip-catalog-*.json"), cache / "equip-scans.json"]:
                    if path.is_file():
                        zf.write(path, path.name)
                folder = self._picture_dir()
                if folder is not None and (folder / "ui.json").is_file():
                    zf.write(folder / "ui.json", "ui.json")
                sheet = equip_icons.icon_sheet(game.read) if game is not None else None
                if sheet is not None:
                    buffer = io.BytesIO()
                    sheet.save(buffer, "PNG")
                    zf.writestr("icons.png", buffer.getvalue())
                log_file = self.data_dir / "manager.log"
                if log_file.is_file():
                    zf.writestr("manager.log", "\n".join(log_file.read_text(encoding="utf-8", errors="replace")
                                                          .splitlines()[-2000:]))
            os.replace(partial, target)
        except BaseException:
            fsutil.remove_file(partial)
            raise

    def save_equipment_icon_sheet(self, target: Path, progress: ProgressFn | None = None) -> bool:
        """The game's icon atlas with a labelled grid (to confirm the weapon-icon positions)."""
        game = self._equip_game(progress)
        sheet = equip_icons.icon_sheet(game.read) if game is not None else None
        if sheet is None:
            return False
        sheet.save(target, "PNG")
        return True

    # ------------------------------------------------------------- requirements
    @staticmethod
    def _stored_files(mod_dir: Path) -> dict[str, Path]:
        """Game path -> stored file for every file of a mod, all options included."""
        out: dict[str, Path] = {}
        files = mod_dir / "files"
        if files.is_dir():
            out.update((rel, files / rel) for rel in list_files(files))
        options = mod_dir / "options"
        if options.is_dir():
            for option in sorted(options.iterdir()):
                if option.is_dir():
                    out.update((rel, option / rel) for rel in list_files(option))
        return out

    def _ensure_lua_scan(self, mod: Mod) -> None:
        """Mods installed by older versions are read once for the Lua modules they need."""
        if not mod.lua_scanned:
            mod.lua_requires = requirements.required_modules(self._stored_files(self.mod_dir(mod.id)))
            mod.lua_scanned = True
            self._write_mod_meta(mod)
            self.save()

    def _module_providers(self) -> dict[str, list[Mod]]:
        """Lua module -> installed mods that provide it (cached per change)."""
        key = (self.revision, tuple((m.id, tuple(m.selected)) for m in self.mods))
        cached = getattr(self, "_providers_cache", None)
        if cached and cached[0] == key:
            return cached[1]
        index: dict[str, list[Mod]] = {}
        for m in self.mods:
            for name in requirements.provided_modules(self.mod_files(m.id)):
                index.setdefault(name, []).append(m)
        self._providers_cache = (key, index)
        return index

    def requirement_status(self, mod_id: str) -> list[dict]:
        """What the mod needs and whether it is there.

        Each item: {"name", "url", "notes", "kind": "nexus" | "lua", "state", "by"} where state is
        "ok" (installed and on), "off" (installed but off), "missing" or "unknown" (cannot be checked)."""
        mod = self.get(mod_id)
        self._ensure_lua_scan(mod)
        others = [m for m in self.mods if m.id != mod.id]
        out = []

        def found(candidates: list[Mod]) -> tuple[str, str]:
            if not candidates:
                return "missing", ""
            best = next((m for m in candidates if m.enabled), candidates[0])
            return ("ok" if best.enabled else "off"), best.name

        for req in mod.requirements:
            name, url = req.get("name", ""), req.get("url", "")
            item = {"name": name, "url": url, "notes": req.get("notes", ""), "kind": "nexus", "by": ""}
            if requirements.is_reframework(name):
                refw = self.refw_mod()
                ok = self.refw_installed() and (refw is None or refw.enabled)
                item.update(state="ok" if ok else "missing", by="REFramework" if ok else "",
                            url=url or REFRAMEWORK_URL)
            else:
                nexus_id = int(req.get("nexus_id") or 0)
                matches = [m for m in others if nexus_id and m.nexus_id == nexus_id] or \
                    [m for m in others if requirements.names_match(name, m.name)]
                state, by = found(matches)
                if state == "missing" and req.get("external"):
                    state = "unknown"  # another site or game: cannot tell if it is installed
                item.update(state=state, by=by)
            out.append(item)

        providers = self._module_providers()
        autorun = self.game_dir / "reframework" / "autorun" if self.game_dir else None
        for module in mod.lua_requires:
            key = requirements.module_key(module)
            state, by = found([m for m in providers.get(key, []) if m.id != mod.id])
            if state == "missing" and autorun is not None and (
                    (autorun / f"{key}.lua").is_file() or (autorun / key / "init.lua").is_file()):
                state, by = "ok", "게임 폴더"
            out.append({"name": module, "url": NEXUS_SEARCH_URL.format(urllib.parse.quote(module)),
                        "notes": "", "kind": "lua", "state": state, "by": by})
        return out

    def missing_requirements(self, mod_id: str) -> list[dict]:
        return [r for r in self.requirement_status(mod_id) if r["state"] in ("missing", "off")]

    def requirement_targets(self, max_age: float | None = 7 * 86400) -> list[int]:
        """Nexus mods whose requirements were never read (or in an older format), or not for ``max_age``."""
        now = time.time()
        return sorted({m.nexus_id for m in self.mods if m.nexus_id and (
            not m.requirements_checked or m.requirements_format < nexus.REQUIREMENTS_FORMAT
            or (max_age is not None and now - m.requirements_checked > max_age))})

    def set_requirements(self, found: dict[int, list[dict]]) -> list[Mod]:
        """Store requirements read from Nexus. Returns the mods that got them."""
        now = int(time.time())
        changed = []
        for m in self.mods:
            if m.nexus_id in found:
                m.requirements = [dict(r) for r in found[m.nexus_id]]
                m.requirements_checked, m.requirements_format = now, nexus.REQUIREMENTS_FORMAT
                self._write_mod_meta(m)
                changed.append(m)
        if changed:
            self.save()
        return changed

    # ------------------------------------------------------------- Nexus
    def link_nexus(self, mod_id: str, text: str) -> int:
        """Connect a mod to its Nexus page (link or mod number). Returns the Nexus mod id."""
        mod = self.get(mod_id)
        nexus_id = nexus.parse_mod_ref(text)
        if nexus_id != mod.nexus_id:
            mod.nexus_id, mod.nexus_file_time, mod.nexus_checked, mod.update = nexus_id, 0, 0, {}
            mod.requirements, mod.requirements_checked = [], 0
            mod.nexus_unlinked = False
            self._write_mod_meta(mod)
            self.save()
        return nexus_id

    def unlink_nexus(self, mod_id: str) -> None:
        mod = self.get(mod_id)
        mod.nexus_id, mod.nexus_file_time, mod.nexus_checked, mod.update = 0, 0, 0, {}
        mod.requirements, mod.requirements_checked = [], 0
        mod.nexus_unlinked = True
        self._write_mod_meta(mod)
        self.save()

    def link_from_sources(self) -> list[Mod]:
        """Connect mods to Nexus from their original file names (installs of older versions, or names
        an older version could not read). Returns the mods that got linked."""
        linked = []
        for m in self.mods:
            if not m.nexus_id and not m.nexus_unlinked and m.origin != REFW_ORIGIN:
                ref = nexus.parse_source_name(m.source)
                if ref:
                    m.nexus_id, m.nexus_file_time, m.nexus_exact = ref.mod_id, ref.uploaded, ref.exact
                    linked.append(m)
        if linked:
            self.save()
        return linked

    def nexus_items(self) -> list[nexus.CheckItem]:
        """Mods connected to Nexus (reading older installs' original file names first)."""
        self.link_from_sources()
        return [nexus.CheckItem(m.id, m.nexus_id, m.nexus_file_time, m.nexus_exact, m.version, m.nexus_checked)
                for m in self.mods if m.nexus_id]

    def find_existing(self, name: str, source: str = "") -> Mod | None:
        """The installed mod a new download probably updates: same name, or else the same Nexus mod -
        but never another file of the same Nexus mod page (a page can offer several mods)."""
        mod = self.find_by_name(name)
        if mod is not None and source and nexus.other_file(mod.source, source):
            mod = None
        ref = nexus.parse_source_name(source) if mod is None else None
        if ref:
            same = [m for m in self.mods if m.nexus_id == ref.mod_id and m.origin != REFW_ORIGIN
                    and not nexus.other_file(m.source, source)]
            mod = same[0] if len(same) == 1 else None
        return mod

    def set_update_results(self, results: dict[str, dict | None]) -> None:
        """Store what a Nexus check found (None keeps the previous result: nothing new since)."""
        now = int(time.time())
        for mod_id, update in results.items():
            try:
                mod = self.get(mod_id)
            except KeyError:
                continue  # removed while checking
            if update is not None:
                mod.update = dict(update)
            mod.nexus_checked = now
        self.settings.last_mod_check = time.time()
        self.save()

    def refw_mod(self) -> Mod | None:
        """The mod that provides REFramework's dinput8.dll, if one is installed here."""
        for m in self.mods:
            if m.origin == REFW_ORIGIN:
                return m
        for m in self.mods:
            if any(path_key(f) == REFRAMEWORK_DLL for f in self.mod_files(m.id)):
                return m
        return None

    def install_refw(self, archive: Path, version: str, source: str) -> Mod:
        """Install or update REFramework from REFramework.zip and switch it on.

        Only dinput8.dll is used, as REFramework advises for players without VR."""
        with self.prepare_install(archive) as session:
            variant = next((v for v in session.variants
                            if any(dst.lower() == REFRAMEWORK_DLL for _, dst in v.files)), None)
            if variant is None:
                raise InstallError(f"{Path(archive).name}에 {REFRAMEWORK_DLL}이(가) 없습니다.")
            variant.files = [(src, dst) for src, dst in variant.files if dst.lower() == REFRAMEWORK_DLL]
            variant.name = variant.package = variant.option = "REFramework"
            variant.version = version
            existing = self.refw_mod()
            mod = self.install_package(session, [variant], existing.id if existing else None)
        mod.origin = REFW_ORIGIN
        mod.version = version
        mod.source = source
        mod.enabled = True
        if existing is None:  # keep REFramework at the top of the load order
            self.mods.remove(mod)
            self.mods.insert(0, mod)
        self._write_mod_meta(mod)
        self._changed()
        return mod

    def _unique_name(self, name: str) -> str:
        name = name.strip() or "이름 없는 모드"
        if not self.find_by_name(name):
            return name
        n = 2
        while self.find_by_name(f"{name} ({n})"):
            n += 1
        return f"{name} ({n})"

    # ------------------------------------------------------------- deploy
    def prepare_deploy(self) -> tuple[list[ModSource], int]:
        """Snapshot of what to deploy, taken on the caller's thread."""
        sources = []
        for m in self.mods:
            if m.enabled:
                entries = self.mod_entries(m.id)
                sources.append(ModSource(m.id, m.name, self.mod_dir(m.id), list(entries), dict(entries)))
        return sources, self.revision

    def run_deploy(self, sources: list[ModSource], progress: ProgressFn | None = None) -> DeployReport:
        """Thread-safe part of a deploy: only touches the game folder and the manifest."""
        deployer = self.deployer()
        installed = {m.id for m in list(self.mods)}  # (a mod installed meanwhile has nothing kept yet)
        if self.settings.check_game_running and is_game_running():
            raise DeployError("게임이 실행 중입니다. 게임을 종료한 뒤 다시 시도하세요.")
        return deployer.run(sources, progress, installed)

    def finish_deploy(self, report: DeployReport, revision: int) -> None:
        if not report.errors and revision == self.revision:
            self.dirty = False
        self.save()

    def deploy(self, progress: ProgressFn | None = None) -> DeployReport:
        sources, revision = self.prepare_deploy()
        report = self.run_deploy(sources, progress)
        self.finish_deploy(report, revision)
        return report

    def purge(self, progress: ProgressFn | None = None) -> DeployReport:
        """Remove every deployed file from the game folder (mods stay installed)."""
        report = self.run_deploy([], progress)
        self.dirty = any(m.enabled for m in self.mods) or bool(report.errors)
        self.save()
        return report

    def verify(self, include_outdated: bool = True) -> list[str]:
        if not self.game_dir:
            return []
        return self.deployer().verify(include_outdated)

    def outdated_paks(self) -> list[str]:
        """PAKs an older version put into the patch chain, which the game no longer reads."""
        if not self.game_dir or not self.game_dir.is_dir():
            return []
        return self.deployer().outdated_paks()

    def deployed_files(self) -> dict:
        """Manifest entries of the current deployment, keyed by lower-cased game path."""
        if not self.game_dir:
            return {}
        return self.deployer().load_manifest()["files"]

    def deployed_backups(self) -> dict:
        """Game files kept aside because a mod's file took their place ({key: {"path", "backup"}}); each
        comes back when no enabled mod places that file any more."""
        if not self.game_dir:
            return {}
        return self.deployer().load_manifest()["backups"]

    # ------------------------------------------------------------- game crash records (게임 튕김 기록)
    @property
    def crash_dir(self) -> Path:
        return self.data_dir / crashes.CRASH_DIR

    def crash_context(self, deployed: dict | None = None) -> dict:
        """What a game session runs with, kept with its records: versions, profile, mods and which mod
        placed each file outside natives\\ (plugins, scripts, PAKs: a crash record names them by path, and
        they may be renumbered or gone by the time the record is read), and which mods had files there
        at all (``placed``: a mod turned on but not applied yet was not in the game). The chosen options'
        names let 원인 모드 찾기 see what changed since the last normal exit. ``deployed``: deployed_files()."""
        from . import __version__

        deployed = self.deployed_files() if deployed is None else deployed
        return {"app": __version__, "game_version": self.game_version(), "profile": self.profile,
                "mods": [{"id": m.id, "name": m.name, "enabled": m.enabled, "stamp": self._stamp(m),
                          "options": [o["name"] for o in m.options if o["id"] in m.selected]}
                         for m in self.mods],
                "owners": {e.get("path") or key: e["mod"] for key, e in deployed.items()
                           if isinstance(e, dict) and e.get("mod") and not key.startswith("natives/")},
                "placed": sorted({str(e["mod"]) for e in deployed.values() if isinstance(e, dict) and e.get("mod")}),
                "dirty": bool(self.dirty)}

    def collect_crash_evidence(self, context: dict | None = None) -> list[crashes.Session]:
        """Copy REFramework's records of the last game sessions before a game start overwrites them.
        Thread-safe with ``context`` from crash_context (taken on the GUI thread)."""
        if not self.game_dir:
            return []
        return crashes.collect(self.crash_dir, self.game_dir, context if context is not None else self.crash_context())

    def note_launch(self) -> None:
        """The game is being started from here: its session gets the mods it runs with (a step of 원인
        모드 찾기 is marked: its mods are not the user's own setup, even if the search ends meanwhile)."""
        from . import bisect

        context = self.crash_context()
        if bisect.active(self.data_dir):
            context["bisect"] = True
        crashes.note_launch(self.crash_dir, context)

    def crash_sessions(self) -> list[crashes.Session]:
        return crashes.sessions(self.crash_dir)

    def crash_index(self, deployed: dict | None = None) -> crashes.ModIndex:
        """The mods and deployed files to match crash records to (crashes.explain may then run on
        another thread). Every mod's files are in it: a record is matched to the mods that were on when
        it was written, which may be off now. ``deployed``: deployed_files()."""
        mods = [crashes.ModRef(m.id, m.name, m.enabled, files=self.mod_files(m.id),
                               material_state=self.material_state(m.id) if m.enabled else "",
                               stuck=[i.get("label", "") for i in self.material_stuck(m.id)] if m.enabled else ())
                for m in self.mods]
        return crashes.ModIndex(self.game_dir, self.deployed_files() if deployed is None else deployed, mods)

    def set_refw_option(self, key: str, value, path=None) -> bool:
        """Change one REFramework setting (only while the game is closed: REFramework writes its whole
        config file again while it runs) in its config file, or in ``path`` (the game folder's or
        %APPDATA%'s: the one REFramework uses, see precheck.Issue.config). Returns False when it already
        had that value."""
        if is_game_running():
            raise DeployError("게임이 실행 중입니다. 게임을 종료한 뒤 다시 시도하세요.")
        if not self.game_dir:
            raise ValueError("게임 폴더가 설정되지 않았습니다.")
        if path and not any(fsutil.same_path(path, p) for p in crashes.config_paths(self.game_dir)):
            raise ValueError(f"REFramework 설정 파일이 아닙니다: {path}")
        if isinstance(value, bool):
            value = "true" if value else "false"  # as REFramework writes them
        return crashes.set_refw_option(self.game_dir, key, value, path=path or None)

    # ------------------------------------------------------------- 실행 전 점검
    def problems(self, conflicts: Conflicts | None = None, missing: dict | None = None) -> dict[str, list[str]]:
        """Why each enabled mod cannot fully work: {mod id: [reason]} (the list's ⚠ marks; the same as the
        mods' own issues of 실행 전 점검). ``conflicts`` and ``missing`` ({enabled mod id:
        missing_requirements}) are used when the caller has them already."""
        return precheck.mod_problems(precheck.mod_states(self, conflicts, missing))

    def precheck_background(self, facts: precheck.Facts, natives: bool = True, materials: bool = True,
                            progress: ProgressFn | None = None) -> precheck.Background:
        """The slow part of 실행 전 점검 (precheck.background, any thread; kept in cache\\): the last game's
        records and the loose files no mod placed, with the game's pak index shared with the equipment
        window."""
        return precheck.background(facts, self.crash_dir, self.data_dir / "cache", natives=natives,
                                   materials=materials, game=lambda: self._equip_game(progress), progress=progress)

    def prelaunch_check(self, refw_status=None, background: precheck.Background | None = None) -> precheck.Report:
        """실행 전 점검 as things are now: the fast checks with the slow part's last result (``background``,
        by default the one kept in cache\\)."""
        facts = precheck.gather(self, refw_status)
        if background is None:
            background = precheck.load_background(self.data_dir / "cache")
        return precheck.check(facts, background)

    def crash_report_mods(self, kind_text=None) -> list[crashes.ReportMod]:
        """The mods (in load order) for the support zip's report.txt."""
        kind_text = kind_text or (lambda kinds: "+".join(sorted(kinds)))
        rows = []
        for m in self.mods:
            options = [o["name"] for o in m.options if o["id"] in m.selected]
            missing = [r["name"] for r in self.missing_requirements(m.id)] if m.enabled else []
            rows.append(crashes.ReportMod(m.name, m.enabled, kind_text(self.mod_kinds(m.id)), m.version, m.nexus_id,
                                          options, self.material_state(m.id), missing))
        return rows

    def export_crash_report(self, target: Path, session: crashes.Session | None,
                            explanation: crashes.Explanation | None, mods: list[crashes.ReportMod], refw: str = "",
                            extra: list[str] = (), include_dump: bool = False) -> Path:
        """The zip for asking for help with a crash (crashes.write_report): report.txt, the session's
        records, the end of manager.log and the deployed files. User names are masked and state.json
        (the Nexus API key) is never in it. ``mods`` from crash_report_mods; thread-safe while the mods
        do not change."""
        from . import __version__

        text = crashes.report_text(
            app_version=__version__, game_dir=self.game_dir, game_version=self.game_version(),
            steam=crashes.steam_info(self.game_dir), refw=refw or ("설치됨" if self.refw_installed() else "없음"),
            profile=self.profile, mods=mods, verify=self.verify(), session=session, explanation=explanation,
            extra=extra)
        return crashes.write_report(target, text, [session] if session is not None else [],
                                    manager_log=self.data_dir / "manager.log", deployed=self.deployed_files(),
                                    names={m.id: m.name for m in self.mods}, include_dump=include_dump)
