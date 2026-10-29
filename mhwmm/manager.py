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
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timedelta
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
from .equip import retarget as equip_retarget
from .equip import scan as equip_scan
from .equip import selftest as equip_selftest
from .equip import tex as equip_tex
from .equip import userdata as equip_userdata
from .equip.model import ARMOR_PARTS, BODIES, PART_LABELS, WEAPON_PARTS, Target, fallback_label
from .equip.paths import normalize as normalize_path
from .matfix import MatfixError, rsz
from .matfix.donors import candidate_paths
from .matfix.game import GameIndex, ShaderIndex, game_fingerprint
from .matfix.mdf2 import detect_version
from .matfix.pak import PakFile, path_hash
from .matfix.repair import CURRENT_VERSION_HINT, MMTR_VERSIONS, RSZ_RE, Aliases, FileResult, RszContext, check_mod, \
    is_material_file, rsz_sources, shader_file
from .modedit import EDITS_DIR, EDITS_PREFIX, EDITS_RECORD, EditError, GameCopyChanged
from .modedit import build as edit_build
from .modedit import engines as edit_engines
from .modedit import model as edit_model
from .modedit import pakedit

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
# 다른 장비로 옮기기 of an armor key without its body ("004/001"): its recipe could not be read afterwards
RETARGET_BODY = "방어구는 체형까지 적어 주세요 (예: armor/f/087/000)"
RETARGET_NAMED = 3  # the name of a mod moved part by part to more pieces of equipment says "<the first> 외 N부위"
MAX_ARCHIVE_DIRS = 8
# Version of the check/repair rules: records made with older rules count as unchecked, so every
# mod is checked again (and repairs offered again) after an update. Raise it whenever a change
# can give a file another result. 2: .user/.pfb/.scn checked too; 3: RSZ rules of v1.11.1-1.11.3;
# 4: a donor picked from others no longer decides alone (stale when no current material of its shader has
# all the mod's slots; the copied material still decides), "null" never; paks the game does not read left out;
# 5: the shaders the game does not have of materials left as shipped are recorded (shader requirements);
# 6: repairs made for an older game version that fit the current one too stay in use as its own (the records
# v2.7.0 made after a game update called every repaired mod stale).
MATFIX_FORMAT = 6
SHADERS_FORMAT = 5  # records from this format on list those shaders (FileResult.shaders)
SHADER_FILE_RE = re.compile(r"^natives/stm/(.+\.mmtr)\.(\d+)$", re.IGNORECASE)  # a shader file a mod puts in
PAK_ENTRY_LABEL_RE = re.compile(r"#([0-9a-f]{16})\.[a-z]+$", re.IGNORECASE)  # check_mod's label of a PAK's RSZ entry
REPORT_SELFTEST_ITEMS = 6  # game parts the diagnostics zip's 3D preview self-test looks at
REPORT_SELFTEST_SECONDS = 60.0  # ... and for at most this long
REGISTRY_RETRY = 3 * 86400  # how long to wait before looking for newer .user/.pfb type data again
VERSIONED_RE = re.compile(r"\.(mdf2|user|pfb|scn)\.(\d+)$", re.IGNORECASE)  # the game reads one version number


class InstallError(Exception):
    pass


def shader_key(mmtr: str) -> str:
    """How shaders are compared: as materials name them (under natives/STM/, without the version number)."""
    return mmtr.replace("\\", "/").strip("/").lower()


def _part_targets(kind: str, to: dict) -> dict[str, equip_retarget.Slot]:
    """part -> the equipment it went to, of a recipe whose "to" is a map (부위마다 다른 장비로, format 2), in
    ARMOR_PARTS order. ValueError for one that does not read (not armor, not a part, a key without its body)."""
    if kind != "armor" or not to:
        raise ValueError("parts")
    targets = {}
    for part, key in to.items():
        slot = equip_retarget.Slot.of(Target(kind, str(key)))
        if part not in ARMOR_PARTS or slot is None:
            raise ValueError(f"{part}: {key}")
        targets[part] = slot
    return {part: targets[part] for part in ARMOR_PARTS if part in targets}


def _recipe_targets(recipe: dict, parts=()) -> dict[str, equip_retarget.Slot]:
    """part -> the equipment a recipe's move took it to: its "to" map, or its one "to" for each of its
    "parts" (``parts`` when it lists none)."""
    to = recipe["to"]
    if isinstance(to, dict):
        return _part_targets(recipe["kind"], to)
    slot = equip_retarget.Slot.of(Target(recipe["kind"], str(to)))
    if slot is None:
        raise ValueError("equipment")
    return {str(part): slot for part in recipe.get("parts") or parts}


def _recipe_to_text(recipe: dict) -> str:
    """A recipe's "to" as the diagnostics report says it: "f/004/000" or "머리 f/045/002 · 몸통 f/087/000"."""
    to = recipe.get("to")
    if isinstance(to, dict):
        return " · ".join(f"{PART_LABELS.get(part, part)} {key}" for part, key in to.items())
    return str(to)


def retarget_aliases(recipe: dict) -> Aliases:
    """What the material check needs of a mod create_retarget made (Mod.retarget): its moved files are
    compared with the game's files at their old paths, and an RSZ file is looked at with the old paths
    and gets the new ones again (equip.retarget's same-length rewrite; a mod whose parts went to different
    equipment, "to" a part -> target map, with each part's own). ValueError (Korean) for a recipe that does
    not read: checked like any other mod, a repair could put the original equipment's paths back."""
    try:
        kind = recipe["kind"]
        source = equip_retarget.Slot.of(Target(kind, str(recipe["from"])))
        if source is None:
            raise ValueError("equipment")
        if isinstance(recipe["to"], dict):  # 부위마다 다른 장비로 (format 2)
            targets = _part_targets(kind, recipe["to"])
            forward = equip_retarget.renamer_of(source, targets)
            back = equip_retarget.renamer_of(source, targets, reverse=True)
        else:  # every part to one piece of equipment (and every recipe of format 1)
            target = equip_retarget.Slot.of(Target(kind, str(recipe["to"])))
            if target is None:
                raise ValueError("equipment")
            parts = recipe.get("parts") if kind == "armor" else None
            forward = equip_retarget.Renamer(source, target, parts)
            back = equip_retarget.Renamer(target, source, parts)
        moves = [(str(new), str(old)) for new, old in recipe.get("moves") or ()]
        hashes = {int(new): int(old) for new, old in recipe.get("pak_moves") or ()}
    except (KeyError, TypeError, ValueError, AttributeError, equip_retarget.RetargetError) as exc:
        raise ValueError(f"다른 장비로 옮긴 기록(mod.json)을 읽지 못해 점검하지 않습니다 ({exc})") from None
    to_new = equip_retarget.mover(moves, forward)
    to_old = equip_retarget.mover([(old, new) for new, old in moves], back)
    return Aliases({new.replace("\\", "/").lower(): old for new, old in moves}, hashes,
                   lambda data, name, moved: equip_retarget.rewrite(data, name, to_old, back if moved else None)[0],
                   lambda data, name, moved: equip_retarget.rewrite(data, name, to_new, forward if moved else None)[0])


def _chained_recipe(first: dict, then: dict, kept=()) -> dict:
    """The recipe of a mod made by moving a mod that was itself moved (``first``: that one's recipe): one
    move from where ``first``'s files came from, so that the material check compares with the game files
    the mod was made for. A file of the first move is found whatever its spelling now (a repair may have
    given it another version number) and gets the original equipment's path in that spelling.
    ``kept``: the files ``then`` put in as they were (loose game paths, pak entry hashes, a named pak
    entry by both); those the first move put where they are stay its moves (a part it took elsewhere than ``then`` moved from).
    The parts: those ``then`` took from where the first move put them, to ``then``'s targets, and those
    the first move took elsewhere, to theirs (their files and names are still there); one the first move
    did not take has the original's names still, one ``then`` took back to the original equipment is a
    file like any other mod's: {} when no file is a moved one. ``then`` alone when ``first`` does not read
    or did not move any part to where ``then`` moved them from (or the parts ``then`` moved without the
    first one went to several pieces of equipment: one recipe cannot say so)."""
    try:
        kind = then["kind"]
        if first["kind"] != kind:
            raise ValueError(first["kind"])
        origin = Target(kind, str(first["from"]))
        a, middle = equip_retarget.Slot.of(origin), equip_retarget.Slot.of(Target(kind, str(then["from"])))
        before, after = _recipe_targets(first, then["parts"]), _recipe_targets(then)
        if a is None or middle is None or middle not in before.values():
            raise ValueError("equipment")
        keys = {}  # each target -> its key as the recipes spell it
        for recipe, targets in ((then, after), (first, before)):
            if isinstance(recipe["to"], dict):
                for part, slot in targets.items():
                    keys.setdefault(slot, str(recipe["to"][part]))
            else:
                slot = equip_retarget.Slot.of(Target(kind, str(recipe["to"])))
                if slot is None:
                    raise ValueError("equipment")
                keys.setdefault(slot, str(recipe["to"]))
        ends = {after[part] for part in after} if isinstance(then["to"], dict) else \
            {equip_retarget.Slot.of(Target(kind, str(then["to"])))}
        back = equip_retarget.Renamer(middle, a)  # the middle equipment's ids -> the original's
        firsts = {normalize_path(str(new)): str(old) for new, old in first.get("moves") or ()}
        hashes = {int(new): int(old) for new, old in first.get("pak_moves") or ()}
    except (KeyError, TypeError, ValueError, AttributeError, equip_retarget.RetargetError):
        return then
    elsewhere = {part: slot for part, slot in before.items() if slot != middle}
    if ends == {a} and not elsewhere:  # back where they came from
        return {}
    chained = {part: slot for part, slot in after.items() if before.get(part) == middle and slot != a}
    chained.update((part, slot) for part, slot in elsewhere.items() if part not in chained)
    order = ARMOR_PARTS + WEAPON_PARTS
    chained = {part: chained[part] for part in sorted(chained, key=lambda p: order.index(p) if p in order else
                                                       len(order))}
    if len(set(chained.values())) > 1:
        to = {part: keys[slot] for part, slot in chained.items()}
    elif len(set(chained.values()) or ends - {a}) == 1:
        to = keys[next(iter(set(chained.values()) or ends - {a}))]
    else:
        return then

    def old_path(path: str) -> str:
        was = firsts.get(normalize_path(path))
        if was is None:  # not one of the first move's files: it came from where it is
            return path
        try:
            renamed = back.rename(path, all_parts=True)
        except equip_retarget.RetargetError:
            renamed = None
        return renamed if renamed is not None and normalize_path(renamed) == normalize_path(was) else was

    moves = [[new, old_path(old)] for new, old in then["moves"]]
    pak_moves = [[new, hashes.get(old, old)] for new, old in then["pak_moves"]]
    for file in kept:  # (where the first move put it)
        if isinstance(file, int) and file in hashes:
            pak_moves.append([file, hashes[file]])
        elif isinstance(file, str) and normalize_path(file) in firsts:
            moves.append([file, old_path(file)])
    moves = [[new, old] for new, old in moves if normalize_path(new) != normalize_path(old)]
    pak_moves = [[new, old] for new, old in pak_moves if new != old]
    if not moves and not pak_moves:  # every file back where it came from (or never moved)
        return {}
    return {**then, "from": origin.key, "to": to, "parts": list(chained), "moves": moves, "pak_moves": pak_moves}


def _retarget_target(source: Target, target, parts=None):
    """``target`` as plan_retarget, create_retarget and retarget_name take it -> (target, parts, errors,
    the parts the errors name): one piece of equipment (a Target, ``parts`` as given), or for armor parts
    moved to different ones (부위마다 다른 장비로) a part -> Target map in ARMOR_PARTS order (``parts`` as
    given: equip.retarget.plan wants None or the map's parts).
    A map of every part to one piece of equipment is that one with the map's parts (a weapon's main and sub
    to one weapon: that weapon). The errors (Korean) of a map are those equip.retarget.plan does not say:
    a value that is not a Target, an armor key without its body or with another body than ``source``'s
    (the recipe keeps the keys)."""
    if not isinstance(target, Mapping):
        return target, parts, [], set()
    mapping = dict(target)
    wrong = [value for value in mapping.values() if not isinstance(value, Target)]
    if wrong:
        return mapping, parts, [f"옮길 수 없는 장비입니다 ({wrong[0]})"], set()
    order = ARMOR_PARTS if source.kind == "armor" else WEAPON_PARTS
    mapping = {part: mapping[part].item  # (sorted is stable: what is not a part stays as given, at the end)
               for part in sorted(mapping, key=lambda p: order.index(p) if p in order else len(order))}
    items = set(mapping.values())
    if source.kind == "weapon":
        if len(items) == 1 and set(mapping) == set(WEAPON_PARTS):
            return next(iter(items)), None, [], set()
        return mapping, parts, [], set()  # (plan says why not)
    if source.kind != "armor" or any(part not in ARMOR_PARTS for part in mapping) or (
            parts is not None and set(parts) != set(mapping)):
        return mapping, parts, [], set()
    if any(t.kind == "armor" and equip_retarget.Slot.of(t) is None and
           equip_retarget.Slot(t.kind, t.key).valid for t in (source, *items)):
        return mapping, parts, [RETARGET_BODY], set()
    body = source.key.partition("/")[0]
    errors, bad = [], set()
    for part, t in mapping.items():
        other = t.key.partition("/")[0]
        if t.kind == "armor" and body in BODIES and other in BODIES and other != body:
            errors.append(f"{PART_LABELS[part]}: 원래 장비와 체형이 다른 장비입니다 ({t.kind}/{t.key})")
            bad.add(part)
    if errors:
        return mapping, parts, errors, bad
    if len(items) == 1:
        return next(iter(items)), list(mapping), [], set()
    return mapping, parts, [], set()


def _children(folder: Path) -> list[Path]:
    """The entries of a folder (none when it is missing or cannot be read)."""
    try:
        with os.scandir(folder) as it:
            return [Path(e.path) for e in it]
    except OSError:
        return []


def _sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(fsutil.longpath(path), "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _edit_file_kind(rel: str) -> str:
    """What a stored file is, as 파일 고르기 names it (텍스처, 재질, 데이터...; PAK for a root .pak)."""
    if is_root_pak(rel):
        return "PAK"
    parts = rel.rsplit("/", 1)[-1].lower().split(".")[1:]
    return next((kind for ext, kind in EDIT_FILE_KINDS if ext in parts), "기타")


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
    bisect_log_files: bool = False  # 원인 모드 찾기: the last choice of "게임이 읽는 파일을 기록해 더 정확하게 찾기"


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
    # 다른 장비로 옮기기: how create_retarget made this mod from another one (empty for every other mod):
    # {"format", "kind", "from", "to" (Target keys; "from" is where the files first came from; "to" of armor
    #  parts moved to different equipment, format 2: {part: key}), "parts" (the parts moved), "source_mod",
    #  "source_name", "moves": [[new game path, old one]], "pak_moves": [[new pak entry hash, old one]]}
    retarget: dict = field(default_factory=dict)
    edited_at: str = ""  # the last 모드 수정 saved or applied again (edits.json): "" for a mod never edited

    @property
    def files_stamp(self) -> str:
        """Changes with every install of other files (the stored files: what the material check reads)."""
        return f"{self.installed_at}|{self.updated_at}"

    @property
    def stamp(self) -> str:
        """Changes with the stored files and with every 모드 수정 (what the game gets); the same as
        files_stamp for a mod never edited, as older crash records and bisect.json have it."""
        return self.files_stamp + (f"|{self.edited_at}" if self.edited_at else "")

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
    retarget: dict = field(default_factory=dict)  # Mod.retarget: compared with the original equipment's files
    excluded: str = ""  # _exclusion_digest of the files 모드 수정 left out (kept in the record: another set is unchecked)
    left_out: frozenset = frozenset()  # their source keys (their repairs stay: the check does not look at them)
    # the repairs in use (repaired_materials, without those of the files left out) and the game version they were
    # made for: a check against another one looks at whether they fit it too (MaterialRun.carried)
    repaired: dict = field(default_factory=dict)
    repaired_for: str = ""
    carried: bool = False  # they were carried over from an older game version (the record's "carried"): looked at too


@dataclass
class MaterialRun:
    game: str  # fingerprint of the game files checked against
    repair: bool
    results: dict = field(default_factory=dict)  # mod id -> [FileResult]
    errors: dict = field(default_factory=dict)  # mod id -> why it could not be checked
    registry: str = ""  # date of the .user/.pfb type data used
    registry_updated: str = ""  # newer type data downloaded during the run (read by it when that was before its
    # jobs, else from the next check)
    # checks: mod id -> {source key: its repair in use, made for another game version (or carried over to this
    # one: MaterialJob.carried), fits this one} (only mods with files that are still stale; finish_materials keeps
    # those repairs as this version's, or no longer when carried over ones do not fit)
    carried: dict = field(default_factory=dict)


@dataclass
class EquipJob:
    """One mod for the equipment window, taken on the caller's thread."""
    mod_id: str
    name: str
    enabled: bool
    order: int  # place in the load order (later mods win)
    stamp: str  # changes when the mod's files change
    files: dict  # game path -> stored file (selected options, repaired materials)


# ---- 모드 수정 (mhwmm/modedit): the overlay in mods/<id>/edits.json and edits/ -------------------------------
EDIT_WAS_CHANGED = "모드 수정 기록이 그사이 바뀌었습니다. 창을 다시 열어 주세요."
EDIT_MOD_CHANGED = "모드가 그사이 바뀌어 저장하지 않았습니다. 창을 다시 열어 고쳐 주세요."
EDIT_OPTIONS_CHANGED = "옵션 구성이 바뀌었습니다"
# the carry-over of an update (install_package): what a member that could not be applied again says
CARRY_STALE = "지금 게임과 형식이 달라 다시 적용하지 못했습니다. 재질 점검에서 복구하면 다시 적용합니다."
CARRY_FAILED = "업데이트 뒤 다시 적용하지 못했습니다"
CARRY_UNREADABLE = "수정 기록을 읽지 못해 다시 적용하지 않았습니다"
# a loose data file whose copy in the game folder changed since it was placed (I11; config files: textconfig's text)
GAME_COPY_DATA = ("게임 폴더의 이 파일은 매니저가 넣은 뒤에 바뀌었습니다. 여기서 값을 바꾸면 게임 폴더에서 바뀐 내용이 "
                  "사라지므로 바꿀 수 없습니다.")
_MEMBER_LABEL = re.compile(r"#([0-9A-Fa-f]{16})(?![0-9A-Fa-f])")  # the entry of a PAK in matfix's labels
EDIT_FILE_KINDS = (  # EditFile.kind by the file's extensions (the first that matches)
    ("tex", "텍스처"), ("mdf2", "재질"), ("mmtr", "재질"), ("mesh", "모델"), ("user", "데이터"), ("pfb", "프리팹"),
    ("scn", "프리팹"), ("efx", "이펙트"), ("sbnk", "소리"), ("spck", "소리"), ("bnk", "소리"), ("pck", "소리"),
    ("wem", "소리"), ("msg", "글"), ("lua", "스크립트"), ("json", "설정"), ("ini", "설정"), ("toml", "설정"),
    ("cfg", "설정"), ("dll", "플러그인"), ("asi", "플러그인"),
)


@dataclass(frozen=True)
class OutRef:
    """An output of the record as mod_entries uses it: only while its base is still the effective base."""
    path: Path
    base_key: str
    base_size: int
    base_mtime_ns: int
    size: int

    def fits(self, eff: tuple[Path, str]) -> bool:
        """``eff`` (effective base file, its source key) is what the output was built from and the output is
        there with its recorded size (I4)."""
        if eff[1] != self.base_key:
            return False
        st = fsutil.stat_or_none(eff[0])
        if st is None or (st.st_size, st.st_mtime_ns) != (self.base_size, self.base_mtime_ns):
            return False
        out = fsutil.stat_or_none(self.path)
        return out is not None and out.st_size == self.size


@dataclass
class EditOverlay:
    """A mod's edits as mod_entries applies them, resolved to today's option ids (_edit_overlay)."""
    excluded: set = field(default_factory=set)  # source keys left out
    outputs: dict = field(default_factory=dict)  # source key -> OutRef
    error: str = ""  # an unreadable record: nothing of it is used


@dataclass
class EditJob:
    """One mod's save (``commit``, from an EditPlan) or re-apply: taken on the GUI thread (edit_job,
    reapply_job), built on a worker into a temporary stage (build_edit), written by finish_edit."""
    mod_id: str
    name: str
    stamp: str
    rev: int
    mode: str  # "commit" | "reapply"
    record: edit_model.Record  # desired, normalized
    inputs: list
    prev: edit_model.Record
    force: bool = False
    dry_run: bool = False
    stage: Path | None = None
    built: list = field(default_factory=list)
    exclusion_changed: bool = False
    lua_changed: bool = False
    notes: list = field(default_factory=list)
    guarded: list = field(default_factory=list)  # [(source key, game path, "values" | "revert")]: I11, checked again
    next: int = 0  # the output counter after build_edit


@dataclass
class EditResult:
    mod_id: str
    changed: bool
    deploy: bool  # what the enabled mod puts into the game changed: apply again
    materials: bool  # the files left out changed and the mod has materials: check them again
    equipment: bool  # a natives/ file or a root PAK was involved
    lua: bool  # a .lua file was left out or put back (lua_requires computed again)
    summary: str
    conflicts: int
    pending: int
    notes: list
    skipped: bool = False  # a re-apply whose mod changed meanwhile: dropped (edits_pending still lists it)


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
        self._edit_records: dict[str, edit_model.Record] = {}  # mod id -> its 모드 수정 record (edits.json)
        self._edits_cache: dict[str, EditOverlay] = {}  # mod id -> the record resolved for mod_entries
        self._pending_cache: tuple[int, list[str]] | None = None  # (revision, edits_pending())
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
        for m in mods:  # outputs of 모드 수정 no record refers to any more (no worker holds one at startup)
            if (self.mod_dir(m.id) / EDITS_DIR).is_dir():
                self._gc_edits(m.id)
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
        self._edit_records.pop(mod_id, None)
        self._edits_cache.pop(mod_id, None)
        self._pending_cache = None
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
        the place of the mod's own files (under the game path the current game uses).

        모드 수정 (edits.json) applies in the order stored file → (files left out) → matfix
        repair → value edit: a file left out is skipped before the later-wins rule (so an
        earlier option's copy wins then), and a value edit's output (source key
        ``edits/<key>``) takes the place of the effective base only while it was built from it."""
        entries = self._entries_cache.get(mod_id)
        if entries is not None:
            return entries
        mod = self.get(mod_id)
        repaired = self.repaired_materials(mod_id)
        overlay = self._edit_overlay(mod_id)

        def entry(base: Path, rel: str, key: str) -> tuple[str, tuple[Path, str]] | None:
            if key in overlay.excluded:
                return None
            if key in repaired:
                new_rel, path = repaired[key]
                eff = (path, f"{MATFIX_DIR}/{key}")
            else:
                new_rel, eff = rel, (base / rel, key)
            out = overlay.outputs.get(key)
            if out is not None and out.fits(eff):
                return new_rel, (out.path, f"{EDITS_PREFIX}{key}")
            return new_rel, eff

        entries = {}
        if not mod.options:
            base = self.files_dir(mod_id)
            entries = dict(e for e in (entry(base, rel, rel) for rel in list_files(base)) if e is not None)
        else:
            by_key: dict[str, str] = {}
            for opt in mod.options:
                if opt["id"] not in mod.selected:
                    continue
                base = self.option_dir(mod_id, opt["id"])
                for rel in self.option_files(mod_id, opt["id"]):
                    found = entry(base, rel, f"{opt['id']}/{rel}")
                    if found is None:  # (an earlier option's copy stays)
                        continue
                    rel, item = found
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
        """Enabled mods whose files all lose to other mods, so nothing of them reaches the game (never a mod with
        a root .pak: that goes to pak_mods and is always loaded)."""
        conflicts = conflicts or self.conflicts()
        hidden = []
        for m in self.mods:
            if not m.enabled:
                continue
            wins, losses = conflicts.counts(m.id)
            files = self.mod_files(m.id)
            if losses and not wins and not any(is_root_pak(f) for f in files) and losses == len(files):
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
                        selected: list[int] | None = None, progress: ProgressFn | None = None) -> Mod:
        """Install ``variants`` as one mod. Several variants become options of that mod.

        ``selected`` lists the indexes of options to switch on; by default none are, and
        an updated mod keeps the options (by name) that were on before. An update carries
        the mod's 모드 수정 over to the new files (_carry_edits: re-applied, what could not be
        is left to check; the record's ``notice`` says what happened)."""
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
            old_options: list[dict] = []
            if replace_id:
                mod = self.get(replace_id)
                old_options = copy.deepcopy(mod.options)  # (before anything of the mod changes: edits name them)
                mod.updated_at = now
                old_selected = {o["name"] for o in mod.options if o["id"] in mod.selected}
                if mod.retarget:  # the files are the archive's now, at the paths it has them
                    log.info("%s: 압축 파일의 파일로 업데이트되어 다른 장비로 옮긴 기록을 지웠습니다", mod.name)
                    mod.retarget = {}
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
            carried = None
            if replace_id and (self.mod_dir(mod_id) / EDITS_RECORD).exists():  # (still the old folder)
                _report, carried = self._carry_edits(mod_id, self.mod_dir(mod_id), stage, old_options, options,
                                                     progress)
            mod.lua_requires = requirements.required_modules(self._effective_stage_files(stage, options, carried))
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
        the game reads one of them. The files 모드 수정 left out are not among them; value edits do not
        count (the stored files are checked: a value edit keeps a file's layout, so its verdict too)."""
        cached = self._material_cache.get(mod_id)
        if cached is not None:
            return cached
        mod = self.get(mod_id)
        if mod.options:
            scopes = [(f"{o['id']}/", self.option_dir(mod_id, o["id"]), self.option_files(mod_id, o["id"]))
                      for o in mod.options]
        else:
            scopes = [("", self.files_dir(mod_id), list_files(self.files_dir(mod_id)))]
        excluded = self._edit_overlay(mod_id).excluded
        found = []
        for prefix, base, rels in scopes:
            newest: dict[tuple[str, str], tuple[int, str]] = {}
            for rel in rels:
                if not is_material_file(rel) or prefix + rel in excluded:
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
        if record.get("excluded", "") != self._exclusion_digest(mod_id):
            return "unchecked"  # checked with other files left out (모드 수정); records without the key: none
        results = self.material_results(mod_id)
        if any(r.stuck for r in results) and record.get("registry", "") < self.registry_date():
            return "unchecked"  # newer .user/.pfb type data may repair what could not be repaired
        carried = record.get("carried")
        if isinstance(carried, dict) and str(carried.get("registry") or "") < self.registry_date() \
                and any(RSZ_RE.search(rel) or is_root_pak(rel) for _key, rel, _path in self.material_files(mod_id)):
            return "unchecked"  # repairs carried over a game update with older type data: it may find them stale
        fixable = {r.key for r in results if r.state == "fixable"}
        if not fixable:
            return "current"
        if record.get("applied_game") == game and fixable <= set(self.repaired_materials(mod_id)):
            return "repaired"
        return "declined" if record.get("declined") else "stale"

    def material_repairs_outdated(self, mod_id: str) -> bool:
        """Whether a "stale" mod is so after a game update: a file that is still stale has a repair in use made for
        an older game version (not carried over to this one, finish_materials), which stays in use until the mod is
        repaired again. False for any other mod, and for a stale one whose stale files were never repaired."""
        if self.material_state(mod_id) != "stale":
            return False
        made_for = self._material_record(mod_id).get("applied_game")
        if not made_for or made_for == self.game_version():
            return False
        excluded = self._edit_overlay(mod_id).excluded
        repaired = {k for k in self.repaired_materials(mod_id) if k not in excluded}
        return any(r.key in repaired for r in self.material_results(mod_id) if r.state == "fixable")

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
                            for rel in self._effective_stored_files(mod_id)}
                if mod.retarget:  # (its RSZ files are looked at as the original shipped them)
                    provided |= {re.sub(r"\.\d+$", "", Path(str(move[1])).name).lower()
                                 for move in mod.retarget.get("moves") or () if isinstance(move, list) and len(move) == 2}
                left_out = frozenset(self._edit_overlay(mod_id).excluded)
                record = self._material_record(mod_id)
                made_for = record.get("applied_game")
                jobs.append(MaterialJob(mod_id, mod.name, mod.files_stamp, list(files), provided,
                                        retarget=copy.deepcopy(mod.retarget), excluded=self._exclusion_digest(mod_id),
                                        left_out=left_out,
                                        repaired={k: v for k, v in self.repaired_materials(mod_id).items()
                                                  if k not in left_out},
                                        repaired_for=made_for if isinstance(made_for, str) else "",
                                        carried=isinstance(record.get("carried"), dict)))
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
        game = self._game_index(progress)
        shaders = ShaderIndex(game, progress)
        run = MaterialRun(game.fingerprint, repair)
        verb = "복구" if repair else "점검"
        context = RszContext(game)
        try:
            if any(RSZ_RE.search(rel) or is_root_pak(rel) for job in jobs for _key, rel, _path in job.files):
                self._update_registry(game, context, run, progress, only_if_outdated=True)
                if run.registry_updated:  # (the run reads the new type data, not the one it compared the game with)
                    context = RszContext(game)
            # the date of the type data this run reads (모드 수정 may hold an older one: what is stuck with it is
            # checked again with the newer one, material_state)
            run.registry = rsz.TypeRegistry.date_in_use()
            self._run_jobs(jobs, game, shaders, run, verb, progress, context)
            if (context.game_unreadable or context.registry_outdated) and not run.registry_updated:
                self._update_registry(game, context, run, progress)
        finally:
            rsz.TypeRegistry.release()
        progress(len(jobs), len(jobs), f"재질 {verb}을 마쳤습니다")
        return run

    def _game_index(self, progress: ProgressFn | None = None) -> GameIndex:
        """The game's pak index (재질 점검, the equipment window, 실행 전 점검): the paks the game reads, without
        the PAKs an older version put into the patch chain (outdated_paks: mods' files, which the game does
        not read either)."""
        return GameIndex(self.game_dir, self.data_dir / "cache", progress, exclude=self.outdated_paks())

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
            rsz.TypeRegistry.reload()  # (even while 모드 수정 holds the old one: the next load reads the new file)
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
            marks = self._type_marks(context)  # (what this mod's check finds of the type data: _types_lag)
            try:
                aliases = retarget_aliases(job.retarget) if job.retarget else None
                results = run.results[job.mod_id] = check_mod(job.files, game, shaders, out, provided=job.provided,
                                                              rszs=context, aliases=aliases)
            except Exception as exc:  # one broken mod must not stop the others
                log.warning("%s: 재질을 %s하지 못했습니다: %s", job.name, verb, exc)
                run.errors[job.mod_id] = str(exc)
                continue
            # repairs made for another game version, or carried over to this one (checked again: newer type data
            # may find them stale)
            if not run.repair and job.repaired and (job.repaired_for != run.game or job.carried) \
                    and any(r.state == "fixable" for r in results):
                try:
                    run.carried[job.mod_id] = self._repairs_fit(job, results, game, shaders, context, aliases, marks)
                except Exception as exc:  # (then they are not known to fit: the mod stays stale)
                    log.warning("%s: 전에 복구한 파일을 점검하지 못했습니다: %s", job.name, exc)

    @staticmethod
    def _repairs_fit(job: MaterialJob, results: list[FileResult], game, shaders, context,
                     aliases: Aliases | None, marks: tuple[int, int] | None = None) -> dict[str, bool]:
        """Whether each repair in use (``job.repaired``, made for another game version or carried over to this one)
        fits this one. The mod's files are checked again as the game gets them, the repaired copies in place of its
        own files (all of them, read-only: a donor may come from the current copy of another of its files, as in the
        check of its own). One fits when a repair would not change it: it reads (a PAK: every entry the check of the
        mod's own PAK reads), nothing in it is stale, and it has no material left as shipped that the mod's own file
        (``results``) does not have too. An entry stuck in it fits only when the old repair left it as shipped, the
        mod's own file has it stuck too and the type data is this game version's (not _types_lag since ``marks``,
        taken before the check of the mod's own files: stuck because the data is behind, newer data may find the
        entry stale, and a repair then would change it). A repaired copy of a loose .user/.pfb is one the old
        repair rewrote: stuck, it is not known to fit."""
        own_files = {key: path for key, _rel, path in job.files}
        files = [(key, *job.repaired[key]) if key in job.repaired else (key, rel, path) for key, rel, path in job.files]
        if aliases is not None:  # a repaired copy with another version number is compared with the same old file
            renamed = {job.repaired[key][0].replace("\\", "/").lower(): aliases.old_path(rel)
                       for key, rel, _path in job.files if key in job.repaired and aliases.old_path(rel)}
            aliases = replace(aliases, paths={**aliases.paths, **renamed})
        seen = {r.key: r for r in check_mod(files, game, shaders, None, provided=job.provided, rszs=context,
                                            aliases=aliases)}
        own = {r.key: r for r in results}
        checked = {key: (rel, path) for key, rel, path in files}
        lagging = []  # (_types_lag, once it is needed)

        def types_lag() -> bool:
            if not lagging:
                lagging.append(ModManager._types_lag(context, marks))
            return lagging[0]

        def fits(key: str) -> bool:
            if key not in checked:
                return False  # (not one of the files looked at)
            r, before = seen.get(key), own.get(key)
            rel, path = checked[key]
            pak = is_root_pak(rel)
            if pak and not ModManager._pak_reads_alike(own_files[key], path):
                return False  # (check_mod passes over an entry it cannot read: a damaged copy would look current)
            if r is None:  # a PAK without materials or RSZ files to look at; a loose file always has a result
                return pak
            if RSZ_RE.search(rel) and r.state == "current" and not r.rsz:  # (check_mod: "not an RSZ file after all")
                try:
                    if rsz.parse(path.read_bytes()) is None:
                        return False
                except Exception:  # (one that cannot be read does not fit)
                    return False
            if r.stuck and not (pak and ModManager._pak_entries_unchanged(own_files[key], path, r.stuck)
                                and not types_lag()):
                return False
            return r.state not in ("fixable", "error") and before is not None \
                and set(r.unresolved) <= set(before.unresolved) \
                and {i.get("label") for i in r.stuck} <= {i.get("label") for i in before.stuck}

        return {key: fits(key) for key in job.repaired}

    @staticmethod
    def _type_marks(context) -> tuple[int, int]:
        """RszContext's counts of the game files its type data could not read and of those with CRCs it does not
        have as current (they only grow: compared before and after a check, _types_lag)."""
        return (context.game_unreadable, context.registry_outdated) if context is not None else (0, 0)

    @staticmethod
    def _types_lag(context, marks: tuple[int, int] | None) -> bool:
        """Whether the .user/.pfb type data of a check (``context``, an RszContext) is known to be behind the game
        for what was checked since ``marks`` (_type_marks; None: since the run began): a game file it could not
        read or whose CRCs it does not have, or the game's sample .pfb (RszContext.matches_game) says so."""
        if context is None:
            return False
        if ModManager._type_marks(context) != (marks if marks is not None else (0, 0)):
            return True
        try:
            return context.matches_game() is False
        except Exception:  # (not known to be this version's)
            return True

    @staticmethod
    def _pak_reads_alike(own: Path, copy: Path) -> bool:
        """Whether every material and .user/.pfb/.scn entry of the mod's own PAK that a check reads is in the
        repaired copy too, of the same kind, and reads there as well as in the mod's own PAK."""
        def reads(head: bytes, data: bytes) -> bool:
            try:
                if head in rsz.MAGICS:
                    return rsz.parse(data) is not None
                return detect_version(data, prefer=CURRENT_VERSION_HINT) is not None  # (as check_mod reads it)
            except Exception:
                return False

        try:
            mine, theirs = PakFile(own), PakFile(copy)
        except Exception:
            return False
        for i, h in enumerate(mine.hashes):
            try:
                head = mine.head(i)
                if head != b"MDF\x00" and head not in rsz.MAGICS:
                    continue  # (textures and meshes: the check does not read them)
                readable = reads(head, mine.read(i))
            except Exception:  # (the check of the mod's own PAK cannot read it either)
                continue
            j = theirs.find(h)
            try:
                if j is None or theirs.head(j) != head:
                    return False
                data = theirs.read(j)
            except Exception:
                return False
            if readable and not reads(head, data):
                return False
        return True

    @staticmethod
    def _pak_entries_unchanged(own: Path, copy: Path, items: list[dict]) -> bool:
        """Whether the PAK entries of ``items`` (check_mod's RSZ items, labelled "<pak>#<hash>.<kind>") are in the
        repaired copy exactly as in the mod's own PAK (the old repair left them as shipped)."""
        try:
            mine, theirs = PakFile(own), PakFile(copy)
            for item in items:
                m = PAK_ENTRY_LABEL_RE.search(str(item.get("label") or ""))
                data = mine.read_hash(int(m.group(1), 16)) if m else None
                if data is None or theirs.read_hash(int(m.group(1), 16)) != data:
                    return False
        except Exception:
            return False
        return True

    def export_material_report(self, job: MaterialJob, target: Path, progress: ProgressFn | None = None) -> int:
        """Write a zip with the mod's .user/.pfb/.scn files, the current game's copies of them, the
        repaired versions in use and what the last check found - to find out why one could not be
        repaired. A mod made by 다른 장비로 옮기기 is paired as its check pairs it: a moved file with the
        original equipment's game file, and what the check looked at (the old paths put back) as
        ``restored/``; report.json says so. Thread-safe (``job`` from material_jobs). Returns the
        number of files compared."""
        from . import __version__

        progress = progress or (lambda done, total, msg: None)
        if not self.game_dir or not self.game_dir.is_dir():
            raise MatfixError("게임 폴더가 설정되지 않았습니다.")
        if not matfix.available():
            raise MatfixError("게임 파일의 압축(zstd)을 풀 수 있는 모듈이 없습니다.")
        game = self._game_index(progress)
        record = fsutil.read_json(self.mod_dir(job.mod_id) / MATFIX_RECORD, {})
        registry_meta = {}
        try:
            registry_meta = rsz.TypeRegistry.load().meta
        except (OSError, ValueError) as exc:
            registry_meta = {"error": str(exc)}
        finally:
            rsz.TypeRegistry.release()
        aliases, moved = None, {}
        if job.retarget:
            recipe = job.retarget
            try:
                aliases = retarget_aliases(recipe)
            except ValueError as exc:  # (the check did not look at it either)
                moved = {"error": str(exc)}
            else:
                moved = {"note": f"다른 장비로 옮긴 모드입니다 ({recipe.get('from')} → {_recipe_to_text(recipe)}). "
                                 "점검은 옮긴 파일을 원래 장비의 게임 파일(game/)과 비교하고, 안의 경로를 원래 장비의 "
                                 "경로로 되돌린 내용(restored/)으로 봅니다"}
            moved["recipe"] = {k: v for k, v in recipe.items() if k not in ("moves", "pak_moves")}
        count, compared = 0, {}
        partial = target.with_name(target.name + ".partial")
        try:
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
                for label, kind, mod_bytes, donor_path, donor_bytes, seen in rsz_sources(job.files, game, aliases):
                    progress(count, 0, f"진단 파일 만드는 중... {label.rsplit('/', 1)[-1]}")
                    name = label.replace("#", "_")
                    zf.writestr(f"mod/{name}", mod_bytes)
                    if seen is not None:
                        zf.writestr(f"restored/{name}", seen)
                    if donor_bytes is not None:
                        zf.writestr(f"game/{name}", donor_bytes)
                    compared[label] = donor_path if donor_bytes is not None else None
                    count += 1
                for key, (rel, path) in self.repaired_materials(job.mod_id).items():
                    if not rel.lower().endswith(".pak"):
                        zf.write(path, f"repaired/{rel}")
                zf.writestr("report.json", json.dumps({
                    "app_version": __version__, "mod": job.name, "game": game.fingerprint,
                    "game_paks": [p.name for p in game.paks], "game_paks_left_out": sorted(game.exclude),
                    "registry": registry_meta, "record": record, "compared": compared,
                    **({"retarget": moved} if moved else {}),
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
                if mod is None or results is None or mod.files_stamp != job.stamp:
                    continue  # removed or reinstalled meanwhile, or could not be read (a value edit changes no input)
                record = dict(self._material_record(mod.id))
                mark = record.get("carried") if isinstance(record.get("carried"), dict) else None
                # files left out or put back by 모드 수정 since the jobs were taken: the record is for the files the
                # run looked at, so the mod is unchecked again (material_state) and its repairs do not carry over
                # (the repair of a file put back is in use again: it must not go)
                exclusions_changed = self._exclusion_digest(mod.id) != job.excluded
                if record.get("excluded", "") != job.excluded:  # other files left out: a stale one may be new
                    record.pop("offered", None)
                record.update(game=run.game, format=MATFIX_FORMAT, registry=run.registry, checked=now,
                              results=[r.to_dict() for r in results], excluded=job.excluded)
                repaired = self.repaired_materials(mod.id)
                had = bool(repaired)
                target = self.mod_dir(mod.id) / MATFIX_DIR
                # the repairs of the files 모드 수정 left out stay in use (the run did not look at them): put back,
                # those files are not the old layout again (§2.7)
                kept = {k: dict(record["applied"][k]) for k in repaired if k in job.left_out}
                if run.repair:
                    applied = {r.key: {"rel": r.new_rel, "file": f"{MATFIX_DIR}/{r.file.name}"}
                               for r in results if r.state == "fixable" and r.file is not None}
                    # (else they go: made for another version, or carried over to this one and not looked at since)
                    if kept and job.stage is not None and record.get("applied_game") == run.game and mark is None:
                        applied.update(self._keep_repairs(kept, repaired, job.stage))
                    fsutil.rmtree(target)
                    if applied:
                        os.replace(job.stage, target)
                    record.update(applied=applied, applied_game=run.game if applied else "", declined=False)
                    record.pop("carried", None)  # (made for this version now)
                    if applied or had:
                        changed.append(mod)
                    if applied:
                        log.info("재질 복구: %s (%d개 파일)", mod.name, len(applied))
                elif had and not any(r.state == "fixable" for r in results):
                    # the mod's own files fit this game version: the repaired copies are not needed now
                    if kept:
                        for key, (_rel, path) in repaired.items():
                            if key not in kept:
                                fsutil.remove_file(path)
                    else:
                        fsutil.rmtree(target)
                    record.update(applied=kept, applied_game=record.get("applied_game", "") if kept else "")
                    if not kept:
                        record.pop("carried", None)
                    if len(kept) < len(repaired):
                        changed.append(mod)
                elif had and not exclusions_changed and (record.get("applied_game") != run.game or mark is not None):
                    fit = self._repairs_carry(job, run, record, repaired)
                    made_for = record.get("applied_game")
                    if fit and made_for != run.game:
                        # they fit the updated game too: this version's repairs now, as they are (the deployed files
                        # and the 모드 수정 built on them stay). Those of the files left out were not looked at: they
                        # go (nothing of them is deployed: they are still left out, else exclusions_changed; put back
                        # later, the file is checked again and offered for repair). The record says so ("carried":
                        # every check looks at them again, newer type data checks the mod again, material_state).
                        for key in kept:
                            fsutil.remove_file(repaired[key][1])
                        record.update(applied={k: dict(record["applied"][k]) for k in repaired if k not in kept},
                                      applied_game=run.game,
                                      carried={"from": made_for if isinstance(made_for, str) else "",
                                               "registry": run.registry})
                        log.info("재질 복구: '%s'은(는) 게임 업데이트 뒤에도 전에 복구한 파일이 그대로 맞아 계속 씁니다",
                                 mod.name)
                    elif fit is False and mark is not None and made_for == run.game:
                        # carried over to this version, checked again (newer type data, ...): one does not fit it.
                        # Made for the version before again: stale, to repair again (material_repairs_outdated); in
                        # use until then
                        before = mark.get("from")
                        record.update(applied_game=before if isinstance(before, str) and before != run.game else "")
                        record.pop("carried")
                        record.pop("offered", None)  # (stale for another reason than when it was offered)
                        log.info("재질 복구: '%s'의 게임 업데이트 뒤에도 그대로 쓰던 전에 복구한 파일이 현재 게임과 "
                                 "맞지 않아 다시 복구해야 합니다", mod.name)
                if isinstance(record.get("carried"), dict):  # (checked with this run's type data)
                    record["carried"] = {**record["carried"], "registry": run.registry}
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

    @staticmethod
    def _repairs_carry(job: MaterialJob, run: MaterialRun, record: dict, repaired: dict) -> bool | None:
        """Whether a check keeps the repairs in use, made for another game version or carried over to this one, as
        this one's: every one of them fits this version (run.carried). False when one does not, or they could not be
        looked at; None when the run did not look at them as they are now (repaired, restored or carried over
        meanwhile). A file the update made stale that has no repair leaves the mod stale (material_state), not its
        repairs outdated."""
        made_for = record.get("applied_game")
        if (made_for if isinstance(made_for, str) else "") != job.repaired_for \
                or {k: v for k, v in repaired.items() if k not in job.left_out} != job.repaired \
                or job.carried != isinstance(record.get("carried"), dict):
            return None
        fits = run.carried.get(job.mod_id)
        return bool(fits) and all(fits.values())

    @staticmethod
    def _keep_repairs(kept: dict, repaired: dict, stage: Path) -> dict:
        """The repairs in use ``kept`` (source key -> applied entry) copied into a repair run's ``stage`` (under
        names its own files do not use), as its applied entries."""
        fsutil.makedirs(stage)
        out = {}
        for key, entry in kept.items():
            path = repaired[key][1]
            tail = path.name.partition("_")[2] or path.name
            name = next(n for n in (f"{i:03d}_{tail}" for i in itertools.count(1)) if not (stage / n).exists())
            fsutil.copy_file(path, stage / name)
            out[key] = {**entry, "file": f"{MATFIX_DIR}/{name}"}
        return out

    def restore_materials(self, mod_id: str) -> bool:
        """Use the mod's own material files again (it no longer counts as a problem).
        Returns True when the deployed files change."""
        mod = self.get(mod_id)
        record = dict(self._material_record(mod_id))
        had = bool(self.repaired_materials(mod_id))
        fsutil.rmtree(self.mod_dir(mod_id) / MATFIX_DIR)
        record.update(applied={}, applied_game="", declined=True)
        record.pop("carried", None)
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
        stamp = f"{mod.stamp}|{','.join(mod.selected)}|{len(files)}"
        return EquipJob(mod.id, mod.name, mod.enabled, order, stamp, files)

    def _equip_game(self, progress: ProgressFn | None = None) -> GameIndex | None:
        """The game's pak index (None without a readable game)."""
        if not self.game_dir or not self.game_dir.is_dir() or not matfix.available():
            return None
        with self._equip_lock:
            version = self.game_version()
            index = self._equip_game_index
            if index is None or index.fingerprint != version or not fsutil.same_path(index.game_dir, self.game_dir):
                # (the PAKs left out change only with the paks themselves: moved out of the chain, or overwritten)
                self._equip_game_index = self._game_index(progress)
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

    # ------------------------------------------------------------- 다른 장비로 옮기기
    def retarget_sources(self, mod_id: str, files: dict | None = None) -> list[Target]:
        """The armor (one Target per body) and weapons a mod changes the look of that it can be moved away
        from (equip.retarget), from the equipment scan of what it puts into the game. ``files``: those
        files ({game path: stored file or (stored file, key)}, e.g. equipment_job(mod_id).files) taken on
        the GUI thread - then they are scanned on this thread, without the mod's state or the caches;
        None: the mod's files now (equipment_job, so on the GUI thread) and the cached scan."""
        if files is None:
            job = self.equipment_job(mod_id)
            scan = self.equipment_scans([job])[mod_id]
        else:
            with self._equip_lock:
                if self._equip_paths is None and any(is_root_pak(rel) for rel in files):
                    self._equip_paths = equip_pathlist.PathIndex(self.data_dir / "cache")
                index = self._equip_paths
            scan = equip_scan.scan_files(files, index)
        return equip_retarget.sources_of(scan.targets)

    def retarget_choices(self, source: Target, progress: ProgressFn | None = None) -> list[tuple[Target, str]]:
        """(equipment, name) a mod of ``source`` can be moved to, by name: the game's equipment of the same
        kind (armor: the same body; weapons: the same type) from the catalog, all its names in one (without
        a game, the bundled path list's with numbers). Thread-safe; the catalog may take a while the
        first time."""
        slot = equip_retarget.Slot.of(source)
        if slot is None:
            return []
        catalog = self._retarget_catalog(progress)
        items = catalog.data.get("items") if catalog is not None else None
        keys = items.get(source.kind) if isinstance(items, dict) else None
        if not isinstance(keys, dict) or not keys:
            keys = {t.key for t in equip_look.game_files().by_item if t.kind == source.kind}
        body = source.key.partition("/")[0]
        out = []
        for key in keys:
            if not isinstance(key, str) or (source.kind == "armor" and key.partition("/")[0] != body):
                continue
            target = Target(source.kind, key)
            other = equip_retarget.Slot.of(target)
            if other is not None and equip_retarget.why_not(slot, other) is None:
                names = list(dict.fromkeys(catalog.names(target))) if catalog is not None else []
                out.append((target, " · ".join(names) or fallback_label(target)))
        return sorted(out, key=lambda choice: (choice[1].casefold(), choice[0]))

    def plan_retarget(self, mod_id: str, source: Target, target: Target | Mapping[str, Target], parts=None,
                      files: dict | None = None) -> equip_retarget.Plan:
        """What moving mod ``mod_id`` from ``source`` to ``target`` would do (equip.retarget.plan; ``parts``:
        the armor parts to move, None: all). ``target``: one piece of equipment, or for armor a part ->
        Target map (부위마다 다른 장비로: each part to its own, the parts not in it stay; ``parts`` then None
        or its parts; each Target of the source's body; a weapon's main and sub to one weapon is a move to
        it). Checked against the game's own files of the targets when the game can be read. ``files``: the
        mod's files ({game path: stored file or (stored file, key)}) taken on the GUI thread
        (equipment_job(mod_id).files) - None: mod_entries now, so on the GUI thread. With ``files``
        thread-safe: reads those files, the path list and the game; changes nothing."""
        return self._plan_retarget(mod_id, source, target, parts, files)[0]

    def _plan_retarget(self, mod_id: str, source: Target, target, parts, files: dict | None = None):
        def slot(t: Target) -> equip_retarget.Slot:  # (one that cannot be moved gets why_not's reason)
            return equip_retarget.Slot.of(t) or equip_retarget.Slot(t.kind, t.key)

        target, parts, problems, bad = _retarget_target(source, target, parts)
        a = slot(source)
        if isinstance(target, dict):  # each part to its own (plan names the part of a target it refuses)
            b = {part: slot(t) if isinstance(t, Target) else t for part, t in target.items()}
            first = next((s for s in b.values() if isinstance(s, equip_retarget.Slot)), a)

            def refused(errors: list[str]) -> equip_retarget.Plan:
                return equip_retarget.Plan(a, first, parts=tuple(b), errors=list(errors), bad_parts=set(bad),
                                           targets={p: s for p, s in b.items() if isinstance(s, equip_retarget.Slot)})
            problem = None
        else:
            b = slot(target)
            bodiless = any(t.kind == "armor" and equip_retarget.Slot.of(t) is None and slot(t).valid
                           for t in (source, target))
            problem = RETARGET_BODY if bodiless else equip_retarget.why_not(a, b)

            def refused(errors: list[str]) -> equip_retarget.Plan:
                return equip_retarget.Plan(a, b, errors=list(errors))
        if problems or problem:
            return refused(problems or [problem]), []
        entries = self.mod_entries(mod_id) if files is None else files
        index = None
        if any(is_root_pak(rel) for rel in entries):
            with self._equip_lock:
                if self._equip_paths is None:
                    self._equip_paths = equip_pathlist.PathIndex(self.data_dir / "cache")
                index = self._equip_paths
        try:
            items = equip_retarget.items_of(entries, index)
        except equip_retarget.RetargetError as exc:
            return refused([str(exc)]), []
        try:
            game = self._equip_game()
        except Exception as exc:  # the target is then not checked (the plan says so)
            log.warning("게임 파일을 읽지 못해 옮길 장비를 확인하지 않습니다: %s", exc)
            game = None
        return equip_retarget.plan(items, a, b, parts, game), items

    def _retarget_catalog(self, progress: ProgressFn | None = None):
        try:
            return self.equipment_catalog(progress)
        except Exception as exc:  # names are only a help: numbers then
            log.warning("장비 이름을 읽지 못했습니다: %s", exc)
            return None

    def retarget_name(self, mod_id: str, target: Target | Mapping[str, Target],
                      progress: ProgressFn | None = None) -> str:
        """The name create_retarget gives the mod it makes of mod ``mod_id`` for ``target``: "<mod> →
        <the target's first name>" (by number without the game's names); for parts moved to different
        equipment (a part -> Target map) "<mod> → 머리 호프 · 몸통 고어" (the parts going to one piece
        together, "머리·팔 호프", in ARMOR_PARTS order; of more than RETARGET_NAMED pieces only the first, "외
        N부위"); with a number when that is taken. Reads the catalog as retarget_choices does: quick once
        that has run (before, on a worker thread)."""
        catalog = self._retarget_catalog(progress)

        def label(t: Target) -> str:
            t = t.item
            return (catalog.name(t) if catalog is not None else "") or fallback_label(t)

        if isinstance(target, Mapping):
            order = ARMOR_PARTS + WEAPON_PARTS
            groups: dict[Target, list[str]] = {}
            for part in sorted(target, key=lambda p: order.index(p) if p in order else len(order)):
                groups.setdefault(target[part].item, []).append(part)
            if len(groups) == 1:  # every part to one: named as a move to it
                target = next(iter(groups))
            else:
                named = [f"{'·'.join(PART_LABELS.get(p, str(p)) for p in parts)} {label(t)}"
                         for t, parts in groups.items()]
                text = " · ".join(named)
                if len(groups) > RETARGET_NAMED:
                    text = f"{named[0]} 외 {len(target) - len(next(iter(groups.values())))}부위"
                return self._unique_name(f"{self.get(mod_id).name} → {text}")
        return self._unique_name(f"{self.get(mod_id).name} → {label(target)}")

    def create_retarget(self, mod_id: str, source: Target, target: Target | Mapping[str, Target], parts=None,
                        disable_original: bool = True, enable: bool = True,
                        progress: ProgressFn | None = None) -> Mod:
        """Make a new mod that changes ``target`` the way mod ``mod_id`` changes ``source`` (plan_retarget,
        planned again; ``target`` as there: one piece of equipment, or a part -> Target map for armor parts
        moved to different ones): its files are built in a new folder under temp and installed as one mod
        without options named as retarget_name says, with the original's author, description, version,
        category and picture (not its Nexus link or update), right after the original in the load order;
        ``enable`` switches it on (whether the original is on or not), ``disable_original`` switches the
        original off. Its recipe (Mod.retarget; a map of every part to one piece of equipment kept as a move
        to it) makes the material check compare it with the original equipment's game files (for a mod
        that was itself moved, those its files first came from). InstallError with the plan's errors. The
        callers check bisect.BLOCKED first; like install_package it may run on a worker thread while
        nothing else changes the mods."""
        progress = progress or (lambda done, total, msg: None)
        original = self.get(mod_id)
        progress(0, 0, "옮길 파일 확인 중...")
        plan, items = self._plan_retarget(mod_id, source, target, parts)
        if not plan.ok:
            raise InstallError("\n".join(plan.errors) or "옮길 파일이 없습니다.")
        target = _retarget_target(source, target, parts)[0]  # (the uniform form of a map of every part to one)
        to = {part: t.key for part, t in target.items()} if isinstance(target, dict) else target.item.key
        name = self.retarget_name(mod_id, target, progress)
        new_id = uuid.uuid4().hex[:10]
        self.temp_root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="retarget-", dir=self.temp_root))
        try:
            try:
                built = equip_retarget.build(plan, items, stage / "files", progress)
            except equip_retarget.RetargetError as exc:
                raise InstallError(str(exc)) from None
            mod = Mod(id=new_id, name=name, enabled=enable, version=original.version, author=original.author,
                      description=original.description, category=original.category, installed_at=_now(),
                      pictures_read=True)
            picture = self.mod_picture(mod_id)
            if picture is not None:
                mod.screenshot = "preview" + picture.suffix.lower()
                fsutil.copy_file(picture, stage / mod.screenshot)
            mod.lua_requires = requirements.required_modules(self._stored_files(stage))
            mod.lua_scanned = True
            mod.retarget = {
                "format": equip_retarget.RETARGET_FORMAT, "kind": plan.source.kind, "from": source.item.key,
                "to": to, "parts": list(plan.parts), "source_mod": original.id,
                "source_name": original.name, "moves": [[new, old] for new, old in built.moves],
                "pak_moves": [[new, old] for new, old in built.pak_moves]}
            if original.retarget:  # moved again: from where the files first came from
                names = set(plan.kept)  # (as Plan.kept names them)
                kept = []  # (a named pak entry by its name too: the recipe's moves rename its references)
                for item in items:
                    if (item.path if item.path is not None else f"{item.pak}#{item.hash64:016X}") not in names:
                        continue
                    if item.pak is not None and item.hash64 is not None:
                        kept.append(item.hash64)
                    if item.path is not None:
                        kept.append(item.path)
                mod.retarget = _chained_recipe(original.retarget, mod.retarget, kept)
            self._write_mod_meta(mod, stage)
            os.replace(stage, self.mod_dir(new_id))
        except BaseException:
            fsutil.rmtree(stage)
            raise
        self._invalidate(new_id)
        ids = [m.id for m in self.mods]
        self.mods.insert(ids.index(original.id) + 1 if original.id in ids else len(ids), mod)
        switched_off = disable_original and original.enabled
        if switched_off:
            original.enabled = False
        self.revision += 1
        self.dirty = self.dirty or mod.enabled or switched_off
        self.save()
        log.info("다른 장비로 옮긴 모드를 만들었습니다: %s (%d개 파일, %s%s)", mod.name, len(built.files),
                 "켜짐" if mod.enabled else "꺼짐", ", 원래 모드는 끔" if switched_off else "")
        return mod

    # ------------------------------------------------------------- 모드 수정
    # The overlay in mods/<id>/: edits.json (the recipe: files left out, values changed) and edits/ (the files
    # built from it). mod_entries applies it (stored file → files left out → matfix repair → value edit), so the
    # mod's own files and folders are never written. The window and the command line ask for an EditPlan;
    # edit_job (GUI thread) checks it, build_edit (any thread) builds the outputs into a temporary stage and
    # finish_edit (GUI thread) moves them in and writes the record, all or nothing.
    @staticmethod
    def source_key(key: str) -> str:
        """The stored file's source key of a mod_entries key: without ``matfix/`` (a repair in use) or
        ``edits/`` (a value edit's output)."""
        for prefix in (EDITS_PREFIX, MATFIX_DIR + "/"):
            if key.startswith(prefix):
                return key[len(prefix):]
        return key

    def edit_record(self, mod_id: str) -> edit_model.Record:
        """The mod's 모드 수정 record (an empty one without edits.json; ``error`` when it cannot be read).
        Cached: callers copy it before changing anything (GUI thread)."""
        record = self._edit_records.get(mod_id)
        if record is None:
            record = self._edit_records[mod_id] = edit_model.load(self.mod_dir(mod_id))
        return record

    def _scopes(self, mod: Mod) -> list[tuple[str, Path, list[str]]]:
        """(option id, "" without options; folder; its game paths) of every stored scope of a mod, in option order."""
        if not mod.options:
            base = self.files_dir(mod.id)
            return [("", base, list_files(base))]
        return [(o["id"], self.option_dir(mod.id, o["id"]), self.option_files(mod.id, o["id"])) for o in mod.options]

    def _stored_path(self, mod: Mod, key: str) -> tuple[str, Path]:
        """(game path, stored file) of a source key."""
        if mod.options:
            oid, _, rel = key.partition("/")
            return rel, self.option_dir(mod.id, oid) / rel
        return key, self.files_dir(mod.id) / key

    def _edit_overlay(self, mod_id: str) -> EditOverlay:
        """The record as mod_entries applies it, resolved to today's option ids (cached; an item whose option
        or file is gone, an item conflict, is not used; an unreadable record not at all)."""
        cached = self._edits_cache.get(mod_id)
        if cached is not None:
            return cached
        record = self.edit_record(mod_id)
        overlay = EditOverlay(error=record.error)
        if not record.error and record.items:
            mod = self.get(mod_id)
            folder = self.mod_dir(mod_id)
            for item in record.items:
                key = None if item.conflict else edit_model.key_of(mod.options, item.source)
                if key is None:
                    continue
                if item.exclude:
                    overlay.excluded.add(key)  # (its values wait until it is put back)
                elif item.out is not None and item.base is not None:
                    overlay.outputs[key] = OutRef(folder / item.out.file, item.base.key, item.base.size,
                                                  item.base.mtime_ns, item.out.size)
        self._edits_cache[mod_id] = overlay
        return overlay

    def _exclusion_digest(self, mod_id: str) -> str:
        """The files 모드 수정 leaves out of the mod, as the material record keeps it ("" for none)."""
        return edit_model.exclusion_digest(self._edit_overlay(mod_id).excluded)

    def _effective_base(self, mod: Mod, key: str, rel: str, stored: Path, repaired: dict | None = None
                        ) -> edit_model.BaseRef:
        """What a value edit of the stored file ``key`` is built on: the matfix repair in use, else the stored
        file. ``repaired``: repaired_materials(mod.id) when the caller has it."""
        repaired = self.repaired_materials(mod.id) if repaired is None else repaired
        if key in repaired:
            new_rel, path = repaired[key]
            return edit_model.BaseRef(path, f"{MATFIX_DIR}/{key}", "repair", new_rel)
        return edit_model.BaseRef(stored, key, "stored", rel)

    def _effective_stored_files(self, mod_id: str) -> dict[str, Path]:
        """Game path -> stored file for every file of a mod, all options included, without the files 모드 수정
        leaves out (the Lua modules its scripts need, the files it provides for the material check)."""
        mod = self.get(mod_id)
        excluded = self._edit_overlay(mod_id).excluded
        out: dict[str, Path] = {}
        for oid, base, rels in self._scopes(mod):
            out.update((rel, base / rel) for rel in rels if (f"{oid}/{rel}" if oid else rel) not in excluded)
        return out

    @staticmethod
    def _effective_stage_files(stage: Path, options: list[dict], record: edit_model.Record | None) -> dict[str, Path]:
        """_effective_stored_files of a mod being installed into ``stage`` with ``options``, without the files
        the carried-over record (``record``, None: no edits) leaves out."""
        excluded = set()
        for item in (record.items if record is not None and not record.error else ()):
            key = None if item.conflict or not item.exclude else edit_model.key_of(options, item.source)
            if key is not None:
                excluded.add(key)
        scopes = [("", stage / "files")] if not options else [(o["id"], stage / "options" / o["id"]) for o in options]
        out: dict[str, Path] = {}
        for oid, base in scopes:
            if base.is_dir():
                out.update((rel, base / rel) for rel in list_files(base)
                           if (f"{oid}/{rel}" if oid else rel) not in excluded)
        return out

    def _excluded_keys(self, mod: Mod, record: edit_model.Record) -> set[str]:
        """The source keys ``record`` leaves out of the mod, with today's option ids."""
        if record.error:
            return set()
        keys = (None if i.conflict or not i.exclude else edit_model.key_of(mod.options, i.source) for i in record.items)
        return {k for k in keys if k is not None}

    def _item_conflict(self, mod: Mod, item: edit_model.Item) -> str:
        """Why an item is not used at all ("" when it is): its option or file went with an update, or its
        option does not resolve (the mod gained or lost its options)."""
        if item.conflict:
            return item.conflict
        if edit_model.key_of(mod.options, item.source) is not None:
            return ""
        if bool(mod.options) != bool(item.source.option):
            return EDIT_OPTIONS_CHANGED
        return f"옵션 '{item.source.option}'이(가) 없습니다"

    def _needs_build(self, mod: Mod, key: str, item: edit_model.Item, repaired: dict | None = None) -> bool:
        """An item with values whose output is not what the game should get (§2.13): never built, its base is
        not the effective base any more (an update, a repair accepted or declined, a hand edit), or its output
        is gone. One whose members all wait (pending) on an unchanged base does not."""
        if item.conflict or not any(m.changes or m.conflicts for m in item.members):
            return False
        rel, stored = self._stored_path(mod, key)
        if item.base is None:
            return fsutil.is_file(stored)
        eff = self._effective_base(mod, key, rel, stored, repaired)
        st = fsutil.stat_or_none(eff.path)
        if st is None:
            return False  # nothing to build on (edit_issues says so)
        if item.base.key != eff.key or (st.st_size, st.st_mtime_ns) != (item.base.size, item.base.mtime_ns):
            return True
        if item.out is not None:
            out = fsutil.stat_or_none(self.mod_dir(mod.id) / item.out.file)
            return out is None or out.st_size != item.out.size
        return False

    def _pending_keys(self, mod: Mod) -> list[str]:
        """Source keys of the mod's items that need building again."""
        record = self.edit_record(mod.id)
        if record.error or not any(i.members for i in record.items):
            return []
        repaired = self.repaired_materials(mod.id)
        out = []
        for item in record.items:
            key = None if item.conflict or not item.members else edit_model.key_of(mod.options, item.source)
            if key is not None and self._needs_build(mod, key, item, repaired):
                out.append(key)
        return out

    def edits_waiting(self, mod_id: str) -> list[str]:
        """Source keys of the mod's files whose edits wait to be built again (the files tab says so; mod_entries
        gives them without the edits meanwhile). GUI thread."""
        return self._pending_keys(self.get(mod_id))

    def edits_pending(self) -> list[str]:
        """Ids of the mods with edits to build again (re-apply: §2.13). Cheap (a stat or two per edited file),
        cached per revision (GUI thread)."""
        if self._pending_cache is not None and self._pending_cache[0] == self.revision:
            return list(self._pending_cache[1])
        found = [m.id for m in self.mods if self._pending_keys(m)]
        self._pending_cache = (self.revision, found)
        return list(found)

    def edit_issues(self, mod_id: str) -> list[str]:
        """What of the mod's edits does not reach the game now, one line each (§2.14): an unreadable or newer
        record, an item whose option or file is gone, values to check, files that could not be built (pending),
        files waiting to be built again. The game gets the effective base for those (GUI thread)."""
        mod = self.get(mod_id)
        record = self.edit_record(mod_id)
        if record.error:
            if record.newer:
                return ["모드 수정 기록이 더 새로운 버전의 매니저에서 만든 것이라 쓰지 않습니다"]
            return [f"모드 수정 기록을 읽지 못해 쓰지 않고 있습니다: {record.error}"]
        out, reasons = [], []
        conflicts = waiting = 0
        repaired = self.repaired_materials(mod_id) if any(i.members for i in record.items) else {}
        for item in record.items:
            why = self._item_conflict(mod, item)
            if why:
                out.append(f"{item.source.label()}: {why}")
                continue
            if not item.members:
                continue
            key = edit_model.key_of(mod.options, item.source)
            if not fsutil.is_file(self._stored_path(mod, key)[1]):
                out.append(f"{item.source.label()}: 모드 폴더에 파일이 없어 수정을 쓰지 않습니다")
                continue
            conflicts += item.conflicts()
            reasons += [m.pending for m in item.members if m.pending]
            waiting += self._needs_build(mod, key, item, repaired)
        if conflicts:
            out.append(f"확인할 값 {conflicts}개")
        if reasons:
            out.append(f"다시 적용하지 못한 파일 {len(reasons)}개: {reasons[0]}")
        if waiting:
            out.append(f"다시 적용 대기 {waiting}개")
        return out

    def _summary(self, mod: Mod, record: edit_model.Record, waiting: bool = True) -> edit_model.EditSummary:
        if record.error:
            return edit_model.EditSummary(0, 0, 0, 0, 0, 0, record.error)
        excluded = values = files = conflicts = pending = wait = 0
        repaired = self.repaired_materials(mod.id) if waiting and any(i.members for i in record.items) else {}
        for item in record.items:
            if self._item_conflict(mod, item):
                conflicts += 1  # (to check in 수정 내용, which offers 지우기)
                continue
            excluded += item.exclude
            n = item.changes()
            values += n
            files += bool(n)
            conflicts += item.conflicts()
            pending += sum(1 for m in item.members if m.pending)
            if waiting and item.members:
                wait += self._needs_build(mod, edit_model.key_of(mod.options, item.source), item, repaired)
        return edit_model.EditSummary(excluded, values, files, conflicts, pending, wait, "")

    def edit_summary(self, mod_id: str) -> edit_model.EditSummary:
        """How many files the mod's edits leave out and values they change, and what needs the user (GUI thread)."""
        return self._summary(self.get(mod_id), self.edit_record(mod_id))

    def edit_items(self, mod_id: str) -> list[edit_model.EditItemView]:
        """The lines of 수정 내용 (and of the command line's ``edit status``): one per file left out and one per
        file (or PAK entry) with values, with its state (GUI thread)."""
        mod = self.get(mod_id)
        record = self.edit_record(mod_id)
        if record.error:
            return []
        repaired = self.repaired_materials(mod_id) if any(i.members for i in record.items) else {}
        out = []
        for item in record.items:
            why = self._item_conflict(mod, item)
            key = None if why else edit_model.key_of(mod.options, item.source)
            selected = not mod.options or (key is not None and key.partition("/")[0] in mod.selected)
            if item.exclude:
                state = "item_conflict" if why else "active" if selected else "inactive_option"
                out.append(edit_model.EditItemView("exclude", item.source, "", item.source.rel, (), (), state, why))
            waiting = key is not None and self._needs_build(mod, key, item, repaired)
            name = item.source.rel.rsplit("/", 1)[-1]
            for m in item.members:
                state, reason = "active", ""
                if why:
                    state, reason = "item_conflict", why
                elif item.exclude:
                    state = "inactive_excluded"
                elif m.pending:
                    state, reason = "pending", m.pending
                elif waiting:
                    state = "waiting"
                elif m.conflicts:
                    state = "conflict"
                elif not selected:
                    state = "inactive_option"
                label = m.label or (f"{name} {m.member}" if m.member else name)
                out.append(edit_model.EditItemView(edit_model.engine_tool(m.engine) or "data", item.source, m.member,
                                                   label, tuple(m.changes), tuple(m.conflicts), state, reason))
        return out

    def excluded_files(self, mod_id: str) -> list[str]:
        """The game paths 모드 수정 leaves out of what the mod puts into the game now (the files tab lists them
        muted; a path another chosen option still puts in is not one)."""
        mod = self.get(mod_id)
        excluded = self._edit_overlay(mod_id).excluded
        if not excluded:
            return []
        shown = {path_key(rel) for rel in self.mod_entries(mod_id)}
        out: dict[str, str] = {}
        for oid, _base, rels in self._scopes(mod):
            if oid and oid not in mod.selected:
                continue
            for rel in rels:
                if (f"{oid}/{rel}" if oid else rel) in excluded and path_key(rel) not in shown:
                    out.setdefault(path_key(rel), rel)
        return sorted(out.values(), key=str.casefold)

    def drop_edit_notice(self, mod_id: str) -> None:
        """Forget the last update's report on the edits (Record.notice: shown once); the rev stays."""
        record = self.edit_record(mod_id)
        if record.error or not record.notice:
            return
        new = record.copy()
        new.notice = {}
        edit_model.save(self.mod_dir(mod_id), new)
        self._edit_records.pop(mod_id, None)

    def game_files(self, progress: ProgressFn | None = None) -> GameIndex | None:
        """The game's pak index shared with the equipment window (None without a readable game). Any thread."""
        return self._equip_game(progress)

    # ---- what the window shows (GUI thread: the snapshot; worker threads read only the snapshot)
    def _placed_states(self, mod: Mod, keys: Mapping[str, str]) -> dict[str, str]:
        """Source key -> Deployer.placed state of the game copy that key's file is placed as now (``keys``:
        source key -> its game path). A key that is not what the chosen options put at its path has no copy
        there ("none"), unless a kept copy of the mod is in edited\\ for it ("stash"): it comes back only for
        the same file."""
        if not self.game_dir or not keys:
            return {}
        at = {self.source_key(k): rel for rel, (_path, k) in self.mod_entries(mod.id).items()}
        ask = {at.get(key, rel) for key, rel in keys.items()} | set(keys.values())
        try:
            states = self.deployer().placed(mod.id, sorted(ask))
        except (OSError, DeployError) as exc:
            log.warning("게임 폴더에 넣은 파일을 확인하지 못했습니다: %s", exc)
            return {}
        out = {}
        for key, rel in keys.items():
            if key in at:
                out[key] = states.get(at[key], "none")
            else:
                out[key] = "stash" if states.get(rel) == "stash" else "none"
        return out

    def edit_snapshot(self, mod_id: str, conflicts: Conflicts | None = None) -> edit_model.EditSnapshot:
        """Everything the 모드 수정 pages and their workers read of a mod (GUI thread): its options, every stored
        file with its state, the record (a copy), the effective bases, the game copies' states (Deployer.placed,
        read once), matfix's stuck files, the paths another enabled mod wins (``conflicts``: conflicts() when
        the caller has it) and what waits to be built again."""
        mod = self.get(mod_id)
        record = self.edit_record(mod_id)
        overlay = self._edit_overlay(mod_id)
        repaired = self.repaired_materials(mod_id)
        options, seen = [], {}
        for o in mod.options:
            n = seen.get(o["name"], 0)
            seen[o["name"]] = n + 1
            options.append(edit_model.OptionRef(o["id"], o["name"], n, o["id"] in mod.selected))
        refs = {o.id: o for o in options}
        scopes = self._scopes(mod)
        winner, having = {}, {}  # path -> the key the chosen options put there; path -> the chosen options having it
        for oid, _base, rels in scopes:
            if oid and oid not in mod.selected:
                continue
            for rel in rels:
                key = f"{oid}/{rel}" if oid else rel
                if oid:
                    having.setdefault(path_key(rel), []).append(refs[oid].name)
                if key not in overlay.excluded:
                    winner[path_key(rel)] = key
        keys = {f"{oid}/{rel}" if oid else rel: rel for oid, _base, rels in scopes for rel in rels}
        placed = self._placed_states(mod, keys)
        stuck: dict[str, str] = {}
        if self.material_state(mod_id) not in ("none", "unchecked"):
            for r in self.material_results(mod_id):
                for s in r.stuck:  # (a PAK's entry is labelled "<pak>#<16 hex digits>.<user|pfb|scn>")
                    found = _MEMBER_LABEL.search(str(s.get("label", "")))
                    stuck.setdefault(f"{r.key}#{found.group(1).upper()}" if found else r.key, str(s.get("reason", "")))
        conflicts = conflicts if conflicts is not None else self.conflicts()
        names = {m.id: m.name for m in self.mods}
        overlaps = {k: names.get(ids[-1], ids[-1]) for k, ids in conflicts.owners.items()
                    if mod_id in ids and ids[-1] != mod_id}
        files, bases = [], {}
        for oid, base, rels in scopes:
            by_path = {path_key(rel): rel for rel in rels}
            for rel in rels:
                key = f"{oid}/{rel}" if oid else rel
                ref = refs.get(oid)
                source = edit_model.Source(ref.name, ref.n, rel) if ref else edit_model.Source("", 0, rel)
                item = None if record.error else record.item(source)
                pk = path_key(rel)
                over = ""
                if item is not None and item.conflict:
                    state = "conflict"
                elif key in overlay.excluded:
                    state = "excluded"
                elif mod.options and not mod.selected:
                    state = "needs_option"
                elif oid and oid not in mod.selected:
                    state = "option_off"
                elif winner.get(pk, key) != key:
                    state = "overridden"
                    over = refs[winner[pk].partition("/")[0]].name
                else:
                    state = "in"
                twin = equip_pathlist.streaming_twin(rel)
                twin = by_path.get(path_key(twin)) if twin else None
                if twin is None and pk.startswith(equip_pathlist.PREFIX + equip_pathlist.STREAMING):
                    plain = pk[:len(equip_pathlist.PREFIX)] + pk[len(equip_pathlist.PREFIX + equip_pathlist.STREAMING):]
                    twin = by_path.get(plain) if equip_pathlist.streaming_twin(plain) == pk else None
                st = fsutil.stat_or_none(base / rel)
                files.append(edit_model.EditFile(
                    source, oid or None, key, rel, base / rel, st.st_size if st else 0, _edit_file_kind(rel), state,
                    over, tuple(having.get(pk, ())) if oid else (), item.changes() if item else 0, key in repaired,
                    placed.get(key, "none"), twin or ""))
                bases[key] = self._effective_base(mod, key, rel, base / rel, repaired)
        return edit_model.EditSnapshot(
            mod.id, mod.name, mod.enabled, mod.stamp, record.rev, options, files, record.copy(), bases, placed,
            stuck, overlaps, frozenset(self._pending_keys(mod)), self.edit_issues(mod_id), self.game_dir,
            copy.deepcopy(mod.retarget), self.data_dir)

    @staticmethod
    def _copy_readonly(state: str, tool: str) -> str:
        """Why a loose file's values cannot be changed because of its game copy (I11), "" when they can."""
        if state not in ("changed", "stash"):
            return ""
        if tool == "config":
            from .modedit import textconfig

            return textconfig.GAME_COPY_CHANGED
        return GAME_COPY_DATA

    def edit_targets(self, snap: edit_model.EditSnapshot, tool: str,
                     progress: ProgressFn | None = None) -> list[edit_model.EditTarget]:
        """The files (and PAK entries) the 게임 수치 (``tool`` "data": .user files under natives/, the .user
        entries of the mod's root PAKs) or 스크립트 설정 page ("config": textconfig.listed) can open, of every
        option. Worker thread: reads the snapshot, the PAKs (on their effective base) and the path list."""
        progress = progress or (lambda done, total, msg: None)
        record = snap.record

        def edited(source, member: str) -> bool:
            item = None if record.error else record.item(source)
            m = item.member(member) if item is not None else None
            return m is not None and bool(m.changes or m.conflicts)

        def readonly(f: edit_model.EditFile, kind: str) -> str:
            if f.state == "conflict":
                item = record.item(f.source)
                return f"수정 기록에 확인할 문제가 있어 바꿀 수 없습니다: {item.conflict if item else ''}"
            return self._copy_readonly(f.placed, kind)

        def note(f: edit_model.EditFile) -> str:
            if f.state == "option_off":
                return f"옵션 '{f.source.option}'이(가) 꺼져 있어 바꾼 값은 그 옵션을 켜야 게임에 들어갑니다."
            if f.state == "overridden":
                return f"아래쪽 옵션 '{f.overridden_by}'의 파일이 이 파일을 덮어써 바꾼 값이 게임에 들어가지 않습니다."
            if f.state == "excluded":
                return "게임에 넣지 않는 파일이라 바꾼 값은 이 파일을 다시 넣어야 게임에 들어갑니다."
            return ""

        def label(f: edit_model.EditFile) -> str:
            folder, _, name = f.rel.rpartition("/")
            text = f"{name} · {folder}" if folder else name
            return text + (f" (옵션 {f.source.option})" if f.source.option else "")

        out: list[edit_model.EditTarget] = []
        if tool == "config":
            from .modedit import textconfig

            for f in snap.files:
                name = textconfig.listed(f.rel, f.size)
                if name:
                    out.append(edit_model.EditTarget(f.source, f.key, "", name, "config", label(f), f.size,
                                                     edited(f.source, ""), readonly(f, "config"), note(f)))
            return out
        if tool != "data":
            raise ValueError(tool)
        paks = [f for f in snap.files if is_root_pak(f.rel)]
        index = reg = None
        if paks:
            from .modedit import rszdata

            with self._equip_lock:
                if self._equip_paths is None:
                    self._equip_paths = equip_pathlist.PathIndex(self.data_dir / "cache")
                index = self._equip_paths
            reg = rszdata.registry()
        try:
            done = 0
            for f in snap.files:
                if edit_model.is_data_rel(f.rel):
                    out.append(edit_model.EditTarget(f.source, f.key, "", "rsz", "data", label(f), f.size,
                                                     edited(f.source, ""), readonly(f, "data"), note(f)))
                    continue
                if not is_root_pak(f.rel):
                    continue
                progress(done, len(paks), f"PAK 안을 읽는 중... {f.rel}")
                done += 1
                base = snap.bases.get(f.key)
                path = base.path if base is not None else f.path
                why = pakedit.check_rebuildable(path)
                locked = f"이 PAK은 다시 만들 수 없는 형식이라 값을 바꿀 수 없습니다 ({why})" if why else ""
                size = fsutil.stat_or_none(path)
                extra = note(f)
                if size is not None and size.st_size > pakedit.BIG_PAK:
                    extra = " ".join(x for x in (extra, f"이 PAK({pakedit.size_text(size.st_size)})은 고치면 같은 크기의 "
                                                        "복사본을 하나 더 저장합니다.") if x)
                try:
                    members = pakedit.members(path, names=index.lookup if index is not None else None,
                                              type_of=lambda data: rszdata.root_type(data, reg))
                except Exception as exc:  # (EditError; OSError or a table that does not read: the PAK is listed so)
                    if not isinstance(exc, EditError):
                        log.warning("모드 수정: %s 안을 읽지 못했습니다: %s", f.rel, exc, exc_info=not isinstance(exc, OSError))
                    why = str(exc) if isinstance(exc, EditError) else f"PAK을 읽지 못했습니다: {exc}"
                    out.append(edit_model.EditTarget(f.source, f.key, "", "rsz", "data", label(f), f.size, False,
                                                     why, extra))
                    continue
                for pm in members:
                    root = pm.type_name.rsplit(".", 1)[-1] if pm.type_name else "데이터"
                    tail = pm.name.rsplit("/", 1)[-1] if pm.name else pm.key[:9]
                    text = f"{f.rel} · {root} ({tail})" + (f" (옵션 {f.source.option})" if f.source.option else "")
                    out.append(edit_model.EditTarget(f.source, f.key, pm.key, "rsz", "data", text, pm.size,
                                                     edited(f.source, pm.key), locked or readonly(f, "data"), extra))
        finally:
            if reg is not None:
                rszdata.release()
        return out

    def _game_copy(self, snap: edit_model.EditSnapshot, target: edit_model.EditTarget,
                   base: edit_model.BaseRef) -> bytes | None:
        """The game's own copy of a data file (a moved mod's at the original equipment's path), None without one."""
        try:
            game = self.game_files()
            if game is None:
                return None
            aliases = None
            if snap.aliases:
                try:
                    aliases = retarget_aliases(snap.aliases)
                except ValueError:
                    aliases = None
            if target.member:
                h = pakedit.member_hash(target.member)
                return game.read_hash(aliases.hashes.get(h, h) if aliases is not None else h)
            path = (aliases.old_path(target.source.rel) if aliases is not None else None) or base.rel
            m = RSZ_RE.search(path)
            if m is None:
                return None
            for candidate in candidate_paths(path[:m.start()]):
                found = game.read_versioned(candidate, m.group(1).lower(), prefer=(int(m.group(2)),))
                if found is not None:
                    return found[1]
        except Exception as exc:  # display only: the page shows every value then
            log.info("모드 수정: 게임 파일을 읽지 못했습니다 (%s): %s", target.label, exc)
        return None

    def open_values(self, snap: edit_model.EditSnapshot, target: edit_model.EditTarget,
                    progress: ProgressFn | None = None) -> edit_model.ValueView:
        """The values of one file (or PAK entry) on its effective base, with the mod's own value, the game's
        (data files: rszdata.compare with the game's copy, and which of them came from rows paired by a key:
        ``keyed``), the hint and the record's change, conflict and rule of each. A file that cannot be changed
        or read (anything that goes wrong while reading it) comes back with ``readonly`` and no rows. Worker
        thread: reads the snapshot, the file, the game's pak index (shared, locked) and the type registry
        (held)."""
        progress = progress or (lambda done, total, msg: None)
        from .modedit import StaleError

        base = snap.bases[target.key]

        def view(rows=(), readonly="", notes=(), total=0, compared=False, truncated=False, data_files=(),
                 added=frozenset(), keyed=frozenset()):
            return edit_model.ValueView(target, list(rows), total, compared, truncated, list(notes), readonly, base,
                                        list(data_files), frozenset(added), frozenset(keyed))

        if target.readonly:
            return view(readonly=target.readonly)
        progress(0, 0, f"읽는 중... {target.label}")
        data_tool = target.tool == "data"
        rszdata = reg = None
        try:
            if data_tool:
                from .modedit import rszdata

                reg = rszdata.registry()
            try:
                if target.member:
                    h = pakedit.member_hash(target.member)
                    data, name = pakedit.read_member(base.path, h), f"{base.rel}#{h:016X}"
                else:
                    with open(fsutil.longpath(base.path), "rb") as f:
                        data, name = f.read(), base.rel
                engine = edit_engines.get(target.engine)
                doc = engine.open(data, name)
                locked = getattr(doc, "readonly", "")
                values = [] if locked else list(doc.values())
            except KeyError:
                return view(readonly="PAK 안에서 이 파일을 찾지 못했습니다.")
            except StaleError as exc:
                why = str(exc)
                if f"{target.key}{target.member}" in snap.stuck and rszdata is not None:
                    why += rszdata.STUCK_SUFFIX  # (matfix found no way to repair it: a repair PAK holds it as it was)
                return view(readonly=why)
            except EditError as exc:
                return view(readonly=str(exc))
            except Exception as exc:  # (OSError, or a file that does not read as its engine expects)
                log.warning("모드 수정: %s을(를) 읽지 못했습니다: %s", target.label, exc,
                            exc_info=not isinstance(exc, OSError))
                return view(readonly=f"파일을 읽지 못했습니다: {exc}")
            if locked:
                return view(readonly=locked)
            notes = [rszdata.REPAIR_NOTE] if data_tool and base.via == "repair" else []
            notes += list(doc.notes)
            game = None
            if data_tool:
                progress(0, 0, f"게임 파일과 비교하는 중... {target.label}")
                try:
                    game = rszdata.compare(data, self._game_copy(snap, target, base), reg, doc=doc)
                except Exception as exc:  # display only: the values without the game's then
                    log.warning("모드 수정: %s을(를) 게임 파일과 비교하지 못했습니다: %s", target.label, exc, exc_info=True)
                else:
                    notes += game.notes()
            rows = self._value_rows(snap, target, engine, values, game, doc)
            compared = bool(game and game.compared)
            return view(rows, "", notes, len(values), compared, bool(getattr(doc, "truncated", False)),
                        getattr(doc, "data_files", ()), game.added if compared else (), game.keyed if compared else ())
        finally:
            if reg is not None:
                rszdata.release()

    @staticmethod
    def _value_rows(snap: edit_model.EditSnapshot, target: edit_model.EditTarget, engine, values, game, doc=None
                    ) -> list[edit_model.ValueRow]:
        """The page's rows: each value with the record's change, conflict and rule of it. A change keeps the
        address it was made at until its file is built again (a re-apply that waits, an update not applied
        yet), so the record's addresses are matched as this file spells them (``doc``: Doc.get / expand)."""
        record = snap.record
        item = None if record.error else record.item(target.source)
        member = item.member(target.member) if item is not None else None

        def spelled(c: edit_model.Change) -> str:
            return c.at if doc is None else ModManager._spelled_at(doc, c)

        singles, rules, conflicts = {}, [], {}
        for c in member.changes if member else ():  # (of two changes to one value the later one counts)
            if c.rule:
                rules.append((spelled(c), c))
            else:
                singles[spelled(c)] = c
        for x in member.conflicts if member else ():
            if not x.change.rule:
                conflicts[spelled(x.change)] = x
        rows = []
        for v in values:
            rule = None
            if rules and v.rule_at:
                rule = next((c for at, c in rules if at == v.rule_at and engine.same(v, v.text, c.before)), None)
            rows.append(edit_model.ValueRow(v, v.text, game.values.get(v.at) if game is not None else None,
                                            game.hint(v.rule_at) if game is not None else "", singles.get(v.at),
                                            conflicts.get(v.at), rule))
        return rows

    @staticmethod
    def _spelled_at(doc, c: edit_model.Change) -> str:
        """The address of ``c`` as ``doc`` names its value now (a row is named by values of its table, which an
        update can change: #8): Doc.get's, for a rule the rule address of the rows Doc.expand finds; its own
        when the file cannot name it."""
        try:
            if c.rule:
                return next((v.rule_at for v in doc.expand(c.at, c.before) if v.rule_at), c.at)
            found = doc.get(c.at)
        except Exception as exc:  # (an address this file cannot name: matched as it is)
            log.debug("모드 수정: 주소를 찾지 못했습니다 (%s): %s", c.at, exc)
            return c.at
        return found.at if found is not None and found.at else c.at

    @classmethod
    def _respell(cls, doc, member: edit_model.Member) -> None:
        """``member``'s changes and conflicts spelled as ``doc`` names their values, one per value as a build
        keeps them (engines.apply): of two changes the later one, and a change rather than a conflict."""
        changes: dict = {}
        for c in member.changes:
            at = cls._spelled_at(doc, c)
            c = c if at == c.at else replace(c, at=at)
            changes.pop(c.ident, None)
            changes[c.ident] = c
        conflicts: dict = {}
        for x in member.conflicts:
            at = cls._spelled_at(doc, x.change)
            x = x if at == x.change.at else replace(x, change=replace(x.change, at=at))
            if x.ident not in changes:
                conflicts.pop(x.ident, None)
                conflicts[x.ident] = x
        member.changes, member.conflicts = list(changes.values()), list(conflicts.values())

    @classmethod
    def _respell_waiting(cls, inp, item: edit_model.Item) -> None:
        """The members of a built item that wait (pending) keep their changes as they were given, named the way
        the file they were made on named its rows. The 게임 수치 page and the command line find the record's
        changes by address, so these are named the way the file names them now when it opens (#8; a member
        that is built comes back so from engines.apply). One that does not open stays as it is. Worker thread
        (the type registry held for data files)."""
        waiting = [m for m in item.members if m.pending and (m.changes or m.conflicts)]
        if not waiting:
            return
        rel = inp.base.rel or inp.rel
        loose = None
        for m in waiting:
            try:
                if is_root_pak(inp.rel):
                    h = pakedit.member_hash(m.member)
                    data, name = pakedit.read_member(inp.base.path, h), f"{rel}#{h:016X}"
                else:
                    if loose is None:
                        with open(fsutil.longpath(inp.base.path), "rb") as f:
                            loose = f.read()
                    data, name = loose, rel
                doc = edit_engines.get(m.engine).open(data, name)
            except Exception as exc:  # (what made it wait, most likely)
                log.debug("모드 수정: 기다리는 %s%s의 주소를 다시 읽지 못했습니다: %s", inp.rel, m.member, exc)
                continue
            if not getattr(doc, "readonly", ""):
                cls._respell(doc, m)

    # ---- saving (edit_job on the GUI thread → build_edit on a worker → finish_edit on the GUI thread)
    def _guard_list(self, mod: Mod, prev: edit_model.Record, desired: edit_model.Record,
                    reapply=frozenset()) -> list[tuple[str, str, str]]:
        """The loose files whose output a save changes: (source key, game path, "values" when a value is set
        or changed, "revert" when values only go). A changed copy of one in the game folder would be lost or
        hidden (I11). Files left out or put back need no question, also when they make another selected
        option's copy the one the mod puts there (쓸 옵션, 모두 원래대로): deploy keeps a changed copy aside
        when another file of the mod takes its place and puts it back with its own file, as when the options
        are changed (#0)."""
        def values(record: edit_model.Record, source) -> dict:
            item = None if record.error else record.item(source)
            if item is None or item.conflict:
                return {}
            return {m.member: list(m.changes) for m in item.members if m.changes}

        sources = {i.source for r in (prev, desired) if not r.error for i in r.items if i.members} | set(reapply)
        out = []
        for source in sorted(sources, key=lambda s: (s.option, s.n, s.rel)):
            key = None if is_root_pak(source.rel) else edit_model.key_of(mod.options, source)
            if key is None:
                continue
            a, b = values(prev, source), values(desired, source)
            if a == b and source not in reapply:
                continue
            added = any(c not in a.get(m, ()) for m, cs in b.items() for c in cs)
            out.append((key, source.rel, "values" if added else "revert"))
        return out

    def _check_guard(self, mod: Mod, guarded: list, force: bool) -> None:
        """I11: refuse values on a loose file whose game copy changed (or is kept in edited\\), and ask before
        putting it back to the mod's own file (GameCopyChanged unless ``force``)."""
        if not guarded:
            return
        states = self._placed_states(mod, {key: rel for key, rel, _kind in guarded})
        hit = [(rel, kind) for key, rel, kind in guarded if states.get(key) in ("changed", "stash")]
        values = [rel for rel, kind in hit if kind == "values"]
        if values:
            tool = "config" if edit_model.config_engine(values[0]) else "data"
            raise EditError(f"{values[0]}: {self._copy_readonly('changed', tool)}")
        if hit and not force:
            rels = list(dict.fromkeys(rel for rel, _kind in hit))
            raise GameCopyChanged("게임 폴더의 다음 파일은 매니저가 넣은 뒤에 바뀌었습니다: " + ", ".join(rels), rels)

    def edit_job(self, plan: edit_model.EditPlan, force: bool = False) -> EditJob:
        """Check an EditPlan against the mod as it is now and take what building it needs (GUI thread, or the
        command line's caller): the record ``plan`` was made on (stamp, rev), its files, the rules of the record
        (model.merge), the game copies of the loose files whose values change (I11: ``force`` to put back one
        that changed) and the effective bases of the items to build. EditError (Korean) when it cannot be
        saved; GameCopyChanged when that needs ``force``."""
        try:
            mod = self.get(plan.mod_id)
        except KeyError:
            raise EditError("모드를 찾지 못했습니다. 창을 다시 열어 주세요.") from None
        record = self.edit_record(mod.id)
        if (mod.stamp, record.rev) != (plan.stamp, plan.rev):
            raise EditError(EDIT_WAS_CHANGED)
        if record.error and not (plan.clear_bad or plan.reset):
            raise EditError(f"모드 수정 기록을 읽지 못해 고칠 수 없습니다 ({record.error}). 수정 내용에서 기록을 "
                            "지우거나 모두 되돌리세요.")
        for source in [*(s for s, flag in plan.exclude.items() if flag), *(s for s, _m in plan.members), *plan.reapply]:
            key = edit_model.key_of(mod.options, source)
            if key is None:
                raise EditError(f"옵션을 찾지 못했습니다: {source.label()}")
            if not fsutil.is_file(self._stored_path(mod, key)[1]):
                raise EditError(f"모드에 없는 파일입니다: {source.label()}")
        desired = edit_model.merge(record, plan)
        prev = record.copy()
        old = {} if prev.error else {i.source: i for i in prev.items}
        repaired = self.repaired_materials(mod.id)
        inputs = []
        for index, item in enumerate(desired.items):
            key = None if item.conflict or not item.members else edit_model.key_of(mod.options, item.source)
            if key is None:
                continue
            was = old.get(item.source)
            before = {m.member: list(m.changes) for m in was.members if m.changes} if was is not None else {}
            now = {m.member: list(m.changes) for m in item.members if m.changes}
            if before != now:
                mode = "commit"  # (only conflicts taken away: the output stays as it is)
            elif item.source in plan.reapply or self._needs_build(mod, key, item, repaired):
                mode = "reapply"  # (a file that does not open waits instead of stopping the save)
            else:
                continue
            rel, stored = self._stored_path(mod, key)
            if not fsutil.is_file(stored):
                if mode == "commit":
                    raise EditError(f"모드에 없는 파일입니다: {item.source.label()}")
                continue
            new = frozenset((m.member, c.at) for m in item.members for c in m.changes
                            if c not in before.get(m.member, ()))
            inputs.append(edit_build.BuildInput(index, copy.deepcopy(item), key, rel,
                                                self._effective_base(mod, key, rel, stored, repaired), stored, mode, new))
        guarded = self._guard_list(mod, prev, desired, plan.reapply)
        self._check_guard(mod, guarded, force)
        flipped = self._excluded_keys(mod, prev) ^ self._excluded_keys(mod, desired)
        return EditJob(mod.id, mod.name, mod.stamp, record.rev, "commit", desired, inputs, prev, force=force,
                       exclusion_changed=bool(flipped), lua_changed=any(k.lower().endswith(".lua") for k in flipped),
                       guarded=guarded)

    def reapply_job(self, mod_id: str, sources=None) -> EditJob | None:
        """A re-apply of the mod's items that need building again (or of ``sources``, the Sources asked for,
        even when nothing changed): None when there is none or the record is unreadable (GUI thread)."""
        mod = self.get(mod_id)
        record = self.edit_record(mod_id)
        if record.error:
            return None
        wanted = None if sources is None else set(sources)
        repaired = self.repaired_materials(mod_id)
        inputs = []
        for index, item in enumerate(record.items):
            key = None if item.conflict or not item.members else edit_model.key_of(mod.options, item.source)
            if key is None or (wanted is not None and item.source not in wanted):
                continue
            if wanted is None and not self._needs_build(mod, key, item, repaired):
                continue
            rel, stored = self._stored_path(mod, key)
            if fsutil.is_file(stored):
                inputs.append(edit_build.BuildInput(index, copy.deepcopy(item), key, rel,
                                                    self._effective_base(mod, key, rel, stored, repaired), stored,
                                                    "reapply"))
        if not inputs:
            return None
        return EditJob(mod.id, mod.name, mod.stamp, record.rev, "reapply", record.copy(), inputs, record.copy())

    @classmethod
    def _failed_build(cls, inp, why: str) -> edit_build.Built:
        """An item of a re-apply that could not be built: every member waits with ``why``, on the base as it
        is now (so it is not tried again until that changes or the user asks), named as that names its values
        (_respell_waiting)."""
        item = copy.deepcopy(inp.item)
        for m in item.members:
            m.pending = why
        cls._respell_waiting(inp, item)
        item.out = None
        st = fsutil.stat_or_none(inp.base.path)
        if st is not None:
            item.base = edit_model.Base(inp.base.key, st.st_size, st.st_mtime_ns, "", "")
        return edit_build.Built(inp.index, item, None, [f"{inp.rel}: {why}"])

    def build_edit(self, job: EditJob, progress: ProgressFn | None = None) -> EditJob:
        """Build the job's outputs into a new folder under temp (any thread: reads only the job, the stored
        files and the type registry, which it holds while it works; writes nothing else). A commit raises on
        the first item that cannot be built (the stage is deleted); a re-apply lets that item wait (pending)."""
        progress = progress or (lambda done, total, msg: None)
        self.temp_root.mkdir(parents=True, exist_ok=True)
        stage = self.temp_root / f"modedit-{job.mod_id}-{uuid.uuid4().hex[:8]}"
        stage.mkdir()
        job.stage, job.built, job.notes = stage, [], list(job.notes)
        rszdata = None
        verb = "저장" if job.mode == "commit" else "다시 적용"
        try:
            if any(edit_model.engine_tool(m.engine) == "data" for inp in job.inputs for m in inp.item.members):
                from .modedit import rszdata as module

                module.registry()
                rszdata = module
            folder = self.mod_dir(job.mod_id)
            number = max(edit_model.number_from(folder, job.prev), edit_model.number_from(folder, job.record))
            for n, inp in enumerate(job.inputs):
                progress(n, len(job.inputs), f"모드 수정 {verb} 중... {inp.rel.rsplit('/', 1)[-1]}")
                try:
                    built = edit_build.build_item(inp, stage, number, progress)
                except Exception as exc:
                    if inp.mode == "commit":
                        raise
                    log.warning("%s: %s을(를) 다시 적용하지 못했습니다: %s", job.name, inp.rel, exc,
                                exc_info=not isinstance(exc, (EditError, OSError)))
                    built = self._failed_build(inp, f"다시 적용하지 못했습니다: {exc}")
                else:
                    self._respell_waiting(inp, built.item)
                if built.out_path is not None:
                    number += 1
                job.built.append(built)
                job.notes += built.notes
            job.next = number
        except BaseException:
            fsutil.rmtree(stage)
            job.stage = None
            raise
        finally:
            if rszdata is not None:
                rszdata.release()
        progress(len(job.inputs), len(job.inputs), f"모드 수정 {verb}을 마쳤습니다")
        return job

    def _base_still(self, mod: Mod, inp, base: edit_model.Base | None, repaired: dict) -> bool:
        """The effective base of a built item is still the one it was built on."""
        if base is None:
            return False
        eff = self._effective_base(mod, inp.key, inp.rel, inp.stored_path, repaired)
        st = fsutil.stat_or_none(eff.path)
        return eff.key == base.key and st is not None and (st.st_size, st.st_mtime_ns) == (base.size, base.mtime_ns)

    def _post_check(self, mod: Mod, record: edit_model.Record, written=frozenset()) -> None:
        """F1: mod_entries as the saved record says - no file left out in it, every output with a fitting base
        used under its game path, every output written now (``written``: edits/ names) there with its size (an
        older one that went missing only makes its item wait). EditError when not."""
        entries = self.mod_entries(mod.id)
        live = {self.source_key(k): (path, k) for path, k in entries.values()}
        put_in = self._excluded_keys(mod, record) & live.keys()
        if put_in:
            raise EditError(f"뺀 파일이 아직 들어갑니다 ({sorted(put_in)[0]})")
        folder = self.mod_dir(mod.id)
        repaired = self.repaired_materials(mod.id)
        for item in record.items:
            key = None if item.conflict or item.out is None else edit_model.key_of(mod.options, item.source)
            if key is None:
                continue
            path = folder / item.out.file
            st = fsutil.stat_or_none(path)
            if item.out.file in written and (st is None or st.st_size != item.out.size):
                raise EditError(f"수정 파일이 없거나 크기가 다릅니다 ({item.out.file})")
            if item.exclude or key not in live or item.base is None:
                continue
            rel, stored = self._stored_path(mod, key)
            eff = self._effective_base(mod, key, rel, stored, repaired)
            ref = OutRef(path, item.base.key, item.base.size, item.base.mtime_ns, item.out.size)
            used, used_key = live[key]
            if ref.fits((eff.path, eff.key)) and (used_key != EDITS_PREFIX + key or not fsutil.same_path(used, path)):
                raise EditError(f"수정한 파일이 쓰이지 않습니다 ({item.source.label()})")

    @staticmethod
    def _edit_time(old: str) -> str:
        """Mod.edited_at for a save now: later than ``old`` even within the same second (the stamps must change)."""
        now = _now()
        if old and now <= old:
            try:
                now = (datetime.fromisoformat(old) + timedelta(seconds=1)).isoformat(timespec="seconds")
            except ValueError:
                pass
        return now

    def _edit_result(self, job: EditJob, mod: Mod | None, record: edit_model.Record, changed: bool = False,
                     deploy: bool = False, flipped=(), skipped: bool = False) -> EditResult:
        rels = {inp.rel for inp in job.inputs} | {k.partition("/")[2] if mod and mod.options else k for k in flipped}
        summary = self._summary(mod, record, waiting=changed) if mod is not None else None
        materials = bool(flipped) and mod is not None and (
            bool(self.material_files(mod.id)) or any(is_material_file(r) for r in rels))
        return EditResult(
            job.mod_id, changed, deploy, materials,
            any(r.lower().startswith("natives/") or is_root_pak(r) for r in rels), job.lua_changed and changed,
            summary.text() if summary is not None else "", 0 if record.error else sum(i.conflicts() for i in record.items),
            0 if record.error else sum(1 for i in record.items for m in i.members if m.pending), list(job.notes),
            skipped)

    def finish_edit(self, job: EditJob) -> EditResult:
        """Write what build_edit built (GUI thread, while nothing else changes the mods): the mod and its record
        must still be those the job was made of (a commit raises EditError, a re-apply is dropped: ``skipped``),
        and each built item's base the effective base; the game copies are checked again (I11). The outputs
        are moved into edits/, the record written (rev + 1), mod_entries checked against it (F1) and all of
        it undone when that fails (F2). Then the outputs no record refers to go (one generation kept),
        Mod.edited_at changes (and with it Mod.stamp), and the state is saved. ``dry_run``: nothing is written."""
        commit = job.mode == "commit"
        try:
            try:
                mod = self.get(job.mod_id)
            except KeyError:
                mod = None
            record = self.edit_record(job.mod_id) if mod is not None else None
            if mod is None or (mod.stamp, record.rev) != (job.stamp, job.rev):
                if commit:
                    raise EditError(EDIT_MOD_CHANGED)
                log.info("모드 수정 다시 적용: '%s'이(가) 그사이 바뀌어 다음에 다시 합니다", job.name)
                return self._edit_result(job, mod, job.record, skipped=True)
            final = job.record.copy()
            inputs = {inp.index: inp for inp in job.inputs}
            repaired = self.repaired_materials(mod.id)
            used = []
            for built in job.built:
                inp = inputs[built.index]
                if not self._base_still(mod, inp, built.item.base, repaired):
                    if inp.mode == "commit":
                        raise EditError(EDIT_MOD_CHANGED)
                    log.info("모드 수정 다시 적용: %s의 바탕 파일이 그사이 바뀌어 다음에 다시 합니다", inp.rel)
                    continue
                final.items[built.index] = built.item
                if built.out_path is not None:
                    used.append(built)
            if commit:
                self._check_guard(mod, job.guarded, job.force)
            final = edit_model.normalize(final)
            flipped = self._excluded_keys(mod, job.prev) ^ self._excluded_keys(mod, final)
            if job.dry_run:
                return self._edit_result(job, mod, final, flipped=flipped)
            if not used and not job.prev.error and final.to_dict() == job.prev.to_dict():
                return self._edit_result(job, mod, final)  # nothing changes
            folder = self.mod_dir(mod.id)
            old_entries = dict(self.mod_entries(mod.id))
            path = folder / EDITS_RECORD
            try:
                old_bytes = path.read_bytes()
            except FileNotFoundError:
                old_bytes = None
            moved = []
            try:
                for built in used:
                    target = folder / built.item.out.file
                    if fsutil.exists(target):
                        raise EditError(f"같은 이름의 수정 파일이 이미 있습니다 ({built.item.out.file})")
                    fsutil.move_file(built.out_path, target)
                    moved.append(target)
                final.rev = job.prev.rev + 1
                final.next = max(final.next, job.next)
                if job.prev.error:  # (기록 지우기 / 모두 원래대로 of an unreadable record: kept aside, never overwritten)
                    edit_model.set_aside(folder)
                edit_model.save(folder, final)
                self._invalidate(mod.id)
                self._post_check(mod, final, {b.item.out.file for b in used})
            except Exception as exc:
                try:  # F2
                    if old_bytes is None:
                        fsutil.remove_file(path)
                    elif not fsutil.exists(path) or path.read_bytes() != old_bytes:
                        back = path.with_name(path.name + ".restore")
                        back.write_bytes(old_bytes)
                        os.replace(back, path)
                    for target in moved:
                        fsutil.remove_file(target)
                except OSError as undo:
                    log.error("%s: 모드 수정을 되돌리지 못했습니다: %s", mod.name, undo)
                self._invalidate(mod.id)
                log.warning("%s: 모드 수정을 저장하지 못해 되돌렸습니다: %s", mod.name, exc,
                            exc_info=not isinstance(exc, (EditError, OSError)))
                raise EditError(f"수정을 저장한 뒤 확인하지 못해 되돌렸습니다: {exc}") from exc
            self._gc_edits(mod.id, keep=job.prev.outputs())
            mod.edited_at = self._edit_time(mod.edited_at)
            if job.lua_changed:
                mod.lua_requires = requirements.required_modules(self._effective_stored_files(mod.id))
            self._write_mod_meta(mod)
            deploy = mod.enabled and dict(self.mod_entries(mod.id)) != old_entries
            self.revision += 1
            self.dirty = self.dirty or deploy
            self.save()
            summary = self._summary(mod, final)
            log.info("모드 수정%s: %s (뺀 파일 %d개 · 바꾼 값 %d개)", "" if commit else " 다시 적용", mod.name,
                     summary.excluded, summary.values)
            return self._edit_result(job, mod, final, changed=True, deploy=deploy, flipped=flipped)
        finally:
            if job.stage is not None:
                fsutil.rmtree(job.stage)
                job.stage = None

    def apply_edit_plan(self, plan: edit_model.EditPlan, force: bool = False, dry_run: bool = False,
                        progress: ProgressFn | None = None) -> EditResult:
        """edit_job → build_edit → finish_edit on the caller's thread (the command line). ``dry_run``: built and
        checked, nothing written."""
        job = self.edit_job(plan, force)
        job.dry_run = dry_run
        self.build_edit(job, progress)
        return self.finish_edit(job)

    def reapply_edits(self, mod_ids=None, progress: ProgressFn | None = None) -> list[EditResult]:
        """Build the edits that need it again (of ``mod_ids``, None: edits_pending()) on the caller's thread (the
        command line after a material check, repair or restore). A mod that fails is logged and left waiting."""
        results = []
        for mod_id in list(mod_ids) if mod_ids is not None else self.edits_pending():
            try:
                job = self.reapply_job(mod_id)
            except KeyError:
                continue
            if job is None:
                continue
            try:
                self.build_edit(job, progress)
                results.append(self.finish_edit(job))
            except Exception as exc:
                log.warning("%s: 모드 수정을 다시 적용하지 못했습니다: %s", job.name, exc,
                            exc_info=not isinstance(exc, (EditError, OSError)))
        return results

    def _gc_edits(self, mod_id: str, keep=frozenset()) -> None:
        """Delete the outputs in edits/ that neither the record nor ``keep`` (``edits/...`` names: the previous
        record's, for workers that still hold its snapshot) refers to; nothing for an unreadable record."""
        folder = self.mod_dir(mod_id)
        try:
            edit_build.collect(folder, edit_model.load(folder), keep)
            fsutil.prune_empty_dirs(folder / EDITS_DIR, folder)
        except OSError as exc:
            log.warning("지난 모드 수정 파일을 지우지 못했습니다 (%s): %s", mod_id, exc)

    # ---- updates (install_package with replace_id): the edits carried over to the new files
    def _carry_edits(self, mod_id: str, old_dir: Path, stage: Path, old_options: list[dict], new_options: list[dict],
                     progress: ProgressFn | None = None) -> tuple[edit_model.CarryReport, edit_model.Record | None]:
        """Carry the mod's record over to an update's files in ``stage`` (the install worker, before the folder
        swap; writes only into the stage): each item resolved by option name and n against ``new_options``, an
        exclusion kept (dropped with its file), values copied with their output when the stored file did not
        change, else built again on the new file (build_item "carry": conflicts left to check, a file that does
        not open waits). stage/edits.json gets rev + 1 and the report as its ``notice``. Never raises: when that
        fails, the old record is put in with every value waiting (nothing unchecked is applied)."""
        old_dir, stage = Path(old_dir), Path(stage)
        try:
            return self._carry(old_dir, stage, old_options, new_options, progress or (lambda done, total, msg: None))
        except Exception as exc:
            log.warning("모드 수정을 업데이트한 파일에 다시 적용하지 못했습니다 (%s): %s", mod_id, exc, exc_info=True)
        try:
            fsutil.rmtree(stage / EDITS_DIR)
            record = edit_model.load(old_dir)
            if record.error:
                return self._carry_unreadable(old_dir, stage, record)
            for item in record.items:
                for m in item.members:
                    m.pending = CARRY_FAILED
                item.out = None
            record.rev += 1
            report = edit_model.CarryReport(pending=sum(len(i.members) for i in record.items))
            record.notice = report.notice(_now())
            edit_model.save(stage, record)
            return report, record
        except Exception as exc:
            log.error("모드 수정 기록을 업데이트한 모드에 옮기지 못했습니다 (%s): %s", mod_id, exc, exc_info=True)
            return edit_model.CarryReport(), None

    @staticmethod
    def _carry_unreadable(old_dir: Path, stage: Path, record: edit_model.Record):
        """An unreadable record is kept aside in the new folder (never applied), with a notice that says so."""
        from .modedit import BAD_SUFFIX

        bad = stage / f"{EDITS_RECORD}{BAD_SUFFIX}{time.strftime('%Y%m%d-%H%M%S')}"
        fsutil.copy_file(old_dir / EDITS_RECORD, bad)
        log.warning("%s: %s (%s)", CARRY_UNREADABLE, record.error, bad.name)
        report = edit_model.CarryReport(error=f"{CARRY_UNREADABLE}: {record.error}")
        note = edit_model.Record(notice=report.notice(_now()))
        edit_model.save(stage, note)
        return report, note

    def _carry(self, old_dir: Path, stage: Path, old_options: list[dict], new_options: list[dict], progress):
        record = edit_model.load(old_dir)
        if record.error:
            return self._carry_unreadable(old_dir, stage, record)
        new = edit_model.Record(rev=record.rev + 1, next=record.next)
        report = edit_model.CarryReport()
        number = edit_model.number_from(old_dir, record)
        listed: dict[Path, tuple[set, dict]] = {}  # scope -> (its files, path_key -> file) as the update lists them
        rszdata = None
        try:
            if any(edit_model.engine_tool(m.engine) == "data" for i in record.items for m in i.members):
                from .modedit import rszdata as module

                module.registry()
                rszdata = module
            for n, old in enumerate(record.items):
                progress(n, len(record.items), f"모드 수정 다시 적용 중... {old.source.rel.rsplit('/', 1)[-1]}")
                item = copy.deepcopy(old)
                src = item.source
                values = item.changes() + int(item.exclude)
                was, item.conflict = item.conflict, ""  # (inactive since an earlier update: its option or file may be back)
                oid = edit_model.resolve_option(new_options, src.option, src.n)
                if oid is None:
                    item.conflict = (f"업데이트에서 옵션 '{src.option}'이(가) 없어졌습니다" if src.option and new_options
                                     else EDIT_OPTIONS_CHANGED)
                    item.out = None
                    report.conflicts += 0 if was else values
                    new.items.append(item)
                    continue
                scope = stage / "options" / oid if oid else stage / "files"
                if scope not in listed:
                    names = set(list_files(scope)) if scope.is_dir() else set()
                    listed[scope] = (names, {path_key(rel): rel for rel in sorted(names)})
                names, by_key = listed[scope]
                # the update's own spelling: one that changed only the letter case of a folder or file is the same
                # file (Windows finds it under the old spelling too, but every key of the mod has the new one)
                rel = src.rel if src.rel in names else by_key.get(path_key(src.rel))
                respelled = edit_model.Source(src.option, src.n, rel or "")
                if rel is not None and rel != src.rel and record.item(respelled) is None and new.item(respelled) is None:
                    item.source = src = respelled  # (not when the record has an item of that spelling too)
                key = f"{oid}/{src.rel}" if oid else src.rel
                path = scope / src.rel
                if rel != src.rel:
                    if not item.members:
                        report.dropped.append(src.label())  # (an exclusion of a file the update removed)
                        continue
                    item.conflict, item.out = "업데이트에서 파일이 없어졌습니다", None
                    report.conflicts += 0 if was else values
                    new.items.append(item)
                    continue
                if not item.members:  # an exclusion: kept as it is
                    report.kept += 1
                    new.items.append(item)
                    continue
                if not was and not any(m.pending for m in item.members) and \
                        self._carry_copy(old_dir, stage, item, key, path):  # (one that waits is tried again)
                    report.kept += values
                    new.items.append(item)
                    continue
                inp = edit_build.BuildInput(len(new.items), item, key, src.rel,
                                            edit_model.BaseRef(path, key, "stored", src.rel), path, "carry")
                try:
                    built = edit_build.build_item(inp, stage, number, progress)
                except Exception as exc:  # one item must not stop the update
                    log.warning("업데이트한 파일에 모드 수정을 다시 적용하지 못했습니다 (%s): %s", src.label(), exc,
                                exc_info=True)
                    built = self._failed_build(inp, f"{CARRY_FAILED}: {exc}")
                else:
                    self._respell_waiting(inp, built.item)
                if built.out_path is not None:
                    number += 1
                given, item = item, built.item
                for m in item.members:
                    if m.pending and rszdata is not None and m.pending.startswith(rszdata.STALE):
                        m.pending = CARRY_STALE
                report.reapplied += item.changes()
                report.conflicts += self._made_conflicts(given, item)
                report.pending += sum(1 for m in item.members if m.pending)
                new.items.append(item)
        finally:
            if rszdata is not None:
                rszdata.release()
        new.next = max(new.next, number)
        new.notice = report.notice(_now())
        edit_model.save(stage, new)
        log.info("모드 수정을 업데이트한 파일에 옮겼습니다: %s", report.text())
        return report, new

    @staticmethod
    def _made_conflicts(given: edit_model.Item, built: edit_model.Item) -> int:
        """The values to check that a build of ``given`` made (``built``): the changes that became conflicts,
        and earlier conflicts that hold another value now (or whose value went). Not the difference of the
        counts: an earlier conflict that heals (its value back to what the change was made on) must not hide a
        new one (#1). An earlier conflict the file only names another way now (its row key changed) is not
        new."""
        made = 0
        for m in built.members:
            old = given.member(m.member)
            left = list(old.conflicts) if old is not None else []
            mine = {c.at for c in old.changes} if old is not None else set()
            for x in m.conflicts:
                if x in left:
                    left.remove(x)
                    continue
                same = None if x.change.at in mine else next(
                    (o for o in left if (o.now, o.why, o.note) == (x.now, x.why, x.note)
                     and replace(o.change, at=x.change.at) == x.change), None)
                if same is not None:
                    left.remove(same)
                else:
                    made += 1
        return made

    @staticmethod
    def _carry_copy(old_dir: Path, stage: Path, item: edit_model.Item, key: str, path: Path) -> bool:
        """The cheap path of a carry-over: the item was built on the stored file and the update did not change
        it, so its output is copied as it is (same name and time) and its base points at the new file."""
        base = item.base
        if base is None or base.key.startswith(MATFIX_DIR + "/") or not base.stored_sha1:
            return False
        if _sha1_file(path) != base.stored_sha1:
            return False
        if item.out is not None:
            old = old_dir / item.out.file
            st = fsutil.stat_or_none(old)
            if st is None or st.st_size != item.out.size:
                return False
            fsutil.copy_file(old, stage / item.out.file)
        st = path.stat()
        item.base = edit_model.Base(key, st.st_size, st.st_mtime_ns, base.sha1, base.stored_sha1)
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
            mod.lua_requires = requirements.required_modules(self._effective_stored_files(mod.id))
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

    # ---- shaders other mods put in (shader packs) ----------------------------------------------------
    def _shader_sources(self, mod: Mod) -> list[tuple[str | None, str, Path]]:
        """(option id or None, game path, stored file) of the mod's loose shader files and PAKs, every option."""
        if not mod.options:
            items = [(None, rel, path) for rel, (path, _key) in self.mod_entries(mod.id).items()]
        else:
            excluded = self._edit_overlay(mod.id).excluded  # (모드 수정: left out of every option)
            items = [(o["id"], rel, self.option_dir(mod.id, o["id"]) / rel)
                     for o in mod.options for rel in self.option_files(mod.id, o["id"])
                     if f"{o['id']}/{rel}" not in excluded]
        return [(opt, rel, path) for opt, rel, path in items if SHADER_FILE_RE.match(rel) or is_root_pak(rel)]

    def _pak_hashes(self, path: Path) -> frozenset:
        """The file hashes of a PAK's entry table (cached per file version; empty when it cannot be read)."""
        st = fsutil.stat_or_none(path)
        if st is None:
            return frozenset()
        cache = self.__dict__.setdefault("_pak_hash_cache", {})
        key = (str(path), st.st_size, st.st_mtime_ns)
        if key not in cache:
            try:
                cache[key] = frozenset(PakFile(path).hashes)
            except Exception as exc:  # (a damaged PAK: nothing of it counts, the rest still does)
                log.debug("PAK의 파일 목록을 읽지 못했습니다 (%s): %s", path.name, exc)
                cache[key] = frozenset()
        return cache[key]

    def _shader_index(self):
        """(shader key -> [(mod id, option id or None)] of the loose shader files, [(mod id, option id or None,
        file hashes)] of the PAKs, the version numbers to look for in PAKs) of every installed mod, all options
        included (cached until a mod is installed, updated, edited or removed)."""
        key = tuple((m.id, m.stamp, len(m.options)) for m in self.mods)
        cached = self.__dict__.get("_shader_cache")
        if cached and cached[0] == key:
            return cached[1]
        loose: dict[str, list[tuple[str, str | None]]] = {}
        paks: list[tuple[str, str | None, frozenset]] = []
        versions = set(MMTR_VERSIONS)
        for m in self.mods:
            for opt, rel, path in self._shader_sources(m):
                match = SHADER_FILE_RE.match(rel)
                if match:
                    loose.setdefault(shader_key(match.group(1)), []).append((m.id, opt))
                    versions.add(int(match.group(2)))  # (the numbering shader packs use now)
                else:
                    hashes = self._pak_hashes(path)
                    if hashes:
                        paks.append((m.id, opt, hashes))
        index = (loose, paks, tuple(sorted(versions)))
        self._shader_cache = (key, index)
        return index

    def _shader_providers(self, key: str) -> list[tuple[str, str | None]]:
        """(mod id, option id or None) of the installed mods with the shader ``key`` (loose or in a PAK)."""
        loose, paks, versions = self._shader_index()
        hashes = {path_hash(shader_file(key, v)) for v in versions}
        found = list(loose.get(key, [])) + [(mod_id, opt) for mod_id, opt, have in paks if hashes & have]
        return list(dict.fromkeys(found))

    def _game_folder_shader(self, key: str) -> bool:
        """The game folder has the shader the manager did not put there: loose in natives\\ or in a PAK of
        pak_mods\\ (another tool's)."""
        if not self.game_dir or not self.game_dir.is_dir():
            return False
        folder: Path | None = self.game_dir / "natives" / "STM"
        *dirs, name = key.split("/")
        for part in dirs:  # (names as Windows compares them)
            folder = next((c for c in _children(folder) if c.name.lower() == part and c.is_dir()), None)
            if folder is None:
                break
        prefix = name + "."
        if folder is not None and any(c.name.lower().startswith(prefix) and c.name[len(prefix):].isdigit()
                                      and c.is_file() for c in _children(folder)):
            return True
        _loose, _paks, versions = self._shader_index()
        hashes = {path_hash(shader_file(key, v)) for v in versions}
        return any(c.suffix.lower() == ".pak" and hashes & self._pak_hashes(c)
                   for c in _children(self.game_dir / "pak_mods"))

    def foreign_shaders(self, mod_id: str) -> dict[str, list[str]]:
        """Shaders (as the mod's materials name them) the current game does not have -> the materials using
        them, of the files the mod puts into the game now (mod_entries: the options chosen): the last material check left
        those materials as shipped; their shader comes from another mod (a shader pack), from an option of
        the mod itself, or is missing. Shaders the mod puts in itself (with the options chosen now) are left
        out. Empty before a check against this game version with the rules of SHADERS_FORMAT."""
        record = self._material_record(mod_id)
        if record.get("format", 1) < SHADERS_FORMAT or record.get("game") != self.game_version():
            return {}
        mod = self.get(mod_id)
        live = {self.source_key(key) for _path, key in self.mod_entries(mod_id).values()}
        need: dict[str, list[str]] = {}
        for r in self.material_results(mod_id):
            if r.key not in live:
                continue  # (an option not chosen, or a file a later chosen option replaces: not in the game)
            for name, mmtr in r.shaders:
                names = need.setdefault(mmtr, [])
                if name not in names:
                    names.append(name)
        return {mmtr: names for mmtr, names in need.items()
                if not any(provider == mod_id and (opt is None or opt in mod.selected)
                           for provider, opt in self._shader_providers(shader_key(mmtr)))}

    def shader_needs(self, mod_id: str) -> set[str]:
        """Keys (shader_key) of the mod's foreign_shaders."""
        return {shader_key(mmtr) for mmtr in self.foreign_shaders(mod_id)}

    def shaders_provided(self, mod_id: str, keys) -> set[str]:
        """Which of the shader ``keys`` the mod puts into the game (with the options chosen now)."""
        mod = self.get(mod_id)
        return {k for k in keys if any(provider == mod_id and (opt is None or opt in mod.selected)
                                       for provider, opt in self._shader_providers(k))}

    def shader_requirements(self, mod_id: str) -> list[dict]:
        """requirement_status items (kind "shader") for the mod's foreign_shaders, one per mod that has them
        (or one for those no installed mod has): ok when that mod is on with the option that has them; off
        when it is off ("mod_off") or that option is not chosen ("option": its name; the mod itself too,
        when its own option has them); missing when no installed mod has them (nor the game folder: then ok,
        by "게임 폴더"). Extra keys: "option", "mod_off", "shaders", "materials"."""
        groups: dict[tuple, dict] = {}
        for mmtr, materials in self.foreign_shaders(mod_id).items():
            key = shader_key(mmtr)
            ranked = []
            for provider, opt in self._shader_providers(key):
                try:
                    mod = self.get(provider)
                except KeyError:
                    continue
                chosen = opt is None or opt in mod.selected
                option = "" if chosen else next((o["name"] for o in mod.options if o["id"] == opt), "")
                # (the fewest steps first: on, then the option to choose, then the mod to turn on, then both)
                rank = (0 if chosen else 1) if mod.enabled else (2 if chosen else 3)
                ranked.append((rank, "ok" if rank == 0 else "off", mod.name, option, not mod.enabled))
            if ranked:
                _rank, state, by, option, mod_off = min(ranked, key=lambda r: r[0])
            elif self._game_folder_shader(key):
                state, by, option, mod_off = "ok", "게임 폴더", "", False
            else:
                state, by, option, mod_off = "missing", "", "", False
            item = groups.setdefault((state, by, option, mod_off), {
                "state": state, "by": by, "option": option, "mod_off": mod_off, "shaders": [], "materials": []})
            item["shaders"].append(mmtr)
            item["materials"] += [n for n in materials if n not in item["materials"]]
        out = []
        for item in groups.values():
            names = [s.replace("\\", "/").rsplit("/", 1)[-1] for s in item["shaders"]]
            name = "셰이더 " + names[0] + (f" 외 {len(names) - 1}개" if len(names) > 1 else "")
            out.append({"name": name, "url": "", "notes": "", "kind": "shader", **item})
        return out

    def requirement_status(self, mod_id: str) -> list[dict]:
        """What the mod needs and whether it is there.

        Each item: {"name", "url", "notes", "kind": "nexus" | "lua" | "shader", "state", "by"} where state is
        "ok" (installed and on), "off" (installed but off), "missing" or "unknown" (cannot be checked).
        Shader items (shader_requirements) also have "option", "mod_off", "shaders" and "materials"."""
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
        try:
            out += self.shader_requirements(mod_id)
        except Exception as exc:  # only a help: the other requirements still count
            log.warning("'%s'에 필요한 셰이더를 확인하지 못했습니다: %s", mod.name, exc)
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
                "mods": [{"id": m.id, "name": m.name, "enabled": m.enabled, "stamp": m.stamp,
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
