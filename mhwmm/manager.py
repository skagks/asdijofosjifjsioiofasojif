"""Mod library: installed mods, load order, settings and deployment."""

from __future__ import annotations

import copy
import logging
import logging.handlers
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

from . import fsutil, nexus, steam
from .analyzer import Variant, analyze, classify, list_files, package_name, raw_variant
from .archive import extract, is_archive
from .constants import DEFAULT_PAK_CHAIN, GAME_EXE, PAK_CHAIN_RE, REFRAMEWORK_DLL, REFW_ORIGIN
from .deploy import Deployer, DeployError, DeployReport, ModSource, ProgressFn, is_root_pak, path_key

log = logging.getLogger(__name__)

STATE_VERSION = 1
SINGLE_FILE_EXTS = (".pak", ".lua", ".dll", ".asi")
STALE_TEMP_SECONDS = 2 * 3600
DATA_DIR_NAME = "MHWildsModManager_data"  # next to the exe
DEFAULT_PROFILE = "기본"
MAX_PROFILE_NAME = 40


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
    pak_chain: str = DEFAULT_PAK_CHAIN
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

    @property
    def nexus_url(self) -> str:
        return nexus.MOD_URL.format(self.nexus_id) if self.nexus_id else ""

    def __post_init__(self):
        self.options = [o for o in self.options
                        if isinstance(o, dict) and isinstance(o.get("id"), str) and isinstance(o.get("name"), str)]
        ids = [o["id"] for o in self.options]
        self.selected = [i for i in ids if i in self.selected]

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

    def counts(self, mod_id: str) -> tuple[int, int]:
        wins = losses = 0
        for ids in self.owners.values():
            if mod_id in ids:
                if ids[-1] == mod_id:
                    wins += 1
                else:
                    losses += 1
        return wins, losses


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
        self.data_dir.mkdir(parents=True, exist_ok=True)
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
        return Deployer(game, self.data_dir, self.settings.pak_chain)

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
                report = Deployer(old, self.data_dir, self.settings.pak_chain).run([], progress)
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
        return any(self.mod_kinds(m.id) & {"loose", "script"} for m in self.mods if m.enabled)

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
        options contain the same path the later option wins."""
        entries = self._entries_cache.get(mod_id)
        if entries is not None:
            return entries
        mod = self.get(mod_id)
        entries = {}
        if not mod.options:
            base = self.files_dir(mod_id)
            entries = {rel: (base / rel, rel) for rel in list_files(base)}
        else:
            by_key: dict[str, str] = {}
            for opt in mod.options:
                if opt["id"] not in mod.selected:
                    continue
                base = self.option_dir(mod_id, opt["id"])
                for rel in self.option_files(mod_id, opt["id"]):
                    k = path_key(rel)
                    if k in by_key:
                        del entries[by_key[k]]
                    by_key[k] = rel
                    entries[rel] = (base / rel, f"{opt['id']}/{rel}")
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
        chain = changes.get("pak_chain")
        if chain is not None and not PAK_CHAIN_RE.match(chain):
            raise ValueError(f"올바르지 않은 PAK 체인 이름입니다: {chain}")
        affects_deploy = chain is not None and chain.lower() != self.settings.pak_chain.lower()
        for k, v in changes.items():
            if hasattr(self.settings, k):
                setattr(self.settings, k, v)
        if affects_deploy:
            self._changed()
        else:
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
            screenshot = ""
            shot = next((v.screenshot for v in variants
                         if v.screenshot and (session.base / v.screenshot).is_file()), None)
            if shot:
                screenshot = "preview" + Path(shot).suffix.lower()
                fsutil.copy_file(session.base / shot, stage / screenshot)

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
        self.save()
        if mod.options:
            log.info("설치 완료: %s (옵션 %d개, 사용할 옵션을 골라 주세요)", mod.name, len(mod.options))
        else:
            log.info("설치 완료: %s (%d개 파일)", mod.name, len(self.mod_files(mod_id)))
        return mod

    # ------------------------------------------------------------- Nexus
    def link_nexus(self, mod_id: str, text: str) -> int:
        """Connect a mod to its Nexus page (link or mod number). Returns the Nexus mod id."""
        mod = self.get(mod_id)
        nexus_id = nexus.parse_mod_ref(text)
        if nexus_id != mod.nexus_id:
            mod.nexus_id, mod.nexus_file_time, mod.nexus_checked, mod.update = nexus_id, 0, 0, {}
            mod.nexus_unlinked = False
            self._write_mod_meta(mod)
            self.save()
        return nexus_id

    def unlink_nexus(self, mod_id: str) -> None:
        mod = self.get(mod_id)
        mod.nexus_id, mod.nexus_file_time, mod.nexus_checked, mod.update = 0, 0, 0, {}
        mod.nexus_unlinked = True
        self._write_mod_meta(mod)
        self.save()

    def nexus_items(self) -> list[nexus.CheckItem]:
        """Mods connected to Nexus (reading older installs' original file names first)."""
        changed = False
        for m in self.mods:
            if not m.nexus_id and not m.nexus_unlinked and m.origin != REFW_ORIGIN:
                ref = nexus.parse_source_name(m.source)
                if ref:
                    m.nexus_id, m.nexus_file_time, m.nexus_exact = ref.mod_id, ref.uploaded, ref.exact
                    changed = True
        if changed:
            self.save()
        return [nexus.CheckItem(m.id, m.nexus_id, m.nexus_file_time, m.nexus_exact, m.version, m.nexus_checked)
                for m in self.mods if m.nexus_id]

    def find_existing(self, name: str, source: str = "") -> Mod | None:
        """The installed mod a new download probably updates: same name, or else the same Nexus mod."""
        mod = self.find_by_name(name)
        ref = nexus.parse_source_name(source) if mod is None else None
        if ref:
            same = [m for m in self.mods if m.nexus_id == ref.mod_id and m.origin != REFW_ORIGIN]
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
        if self.settings.check_game_running and is_game_running():
            raise DeployError("게임이 실행 중입니다. 게임을 종료한 뒤 다시 시도하세요.")
        return deployer.run(sources, progress)

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

    def verify(self) -> list[str]:
        if not self.game_dir:
            return []
        return self.deployer().verify()

    def deployed_files(self) -> dict:
        """Manifest entries of the current deployment, keyed by lower-cased game path."""
        if not self.game_dir:
            return {}
        return self.deployer().load_manifest()["files"]
