"""실행 전 점검: what is worth a look before the game starts.

These are risk predictors, not proof: each issue says what was found and what may help, worded
like the rest of the app.  Levels: ``high`` (mods will not work, or the game is likely to crash
or hang), ``medium`` (a common cause, or an estimate), ``low`` and ``info`` (worth knowing).
Only new high and medium issues are put in front of the user before a launch (:func:`to_ask`);
"같은 내용이면 다시 묻지 않기" keeps them from being asked again until something changes
(:func:`remember`; after a game update everything is asked again).  Launching is never blocked.

Two parts:

- :func:`check` is fast (well under a second, offline, a few folder listings): the mods' own
  problems (what the main list marks: options, requirements, materials; also materials not checked
  against this game version yet), REFramework's settings against the mods (``re2_fw_config.txt``),
  pak_mods (count, PAKs the manager did not place, extension case), DLLs in the game folder
  (REFramework's VR files, proxies such as ReShade's dxgi.dll, others the game does not ship),
  REFramework plugins and scripts the manager did not place, REFramework missing or behind after a
  game update, and the deploy check (verify).
- :func:`background` is slow (off the GUI thread; its result is kept in ``cache\\precheck-scan.json``):
  the last game session's records (files REFramework could not load whose copy the game read then was
  an enabled mod's, REFramework failing to start) and loose files under natives\\ the manager
  did not place, with a read-only material check of them (재질 점검, ``check_mod(..., out=None)``).
  :func:`check` takes that result and keeps only what still holds (the mod still on and unchanged,
  the file still there and unmanaged).

Nothing here changes the game folder.  REFramework's settings are changed by the caller
(manager.set_refw_option: only while the game is closed, and only with the user's consent) with
the key and value in :attr:`Issue.fix`.  The game's own DLL list is from one install (October
2026) and the pak_mods limit is a reported estimate, so those are worded carefully.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from . import crashes, crashlog, fsutil
from .constants import GAME_EXE, PAK_MODS_DIR, REFRAMEWORK_DLL

log = logging.getLogger(__name__)

TITLE = "실행 전 점검"  # the ☰ menu item and the window
DIALOG_TITLE = "실행 전 점검: 확인할 것 {count}개"
RUN_ANYWAY = "그래도 실행"
CANCEL = "취소"
DONT_ASK = "같은 내용이면 다시 묻지 않기"
NOTHING = "확인할 것이 없습니다."
RUNNING = "게임이 실행 중이라 REFramework 설정을 바꿀 수 없습니다. 게임을 끈 뒤 다시 하세요."

LEVELS = ("high", "medium", "low", "info")
ASK_LEVELS = ("high", "medium")  # asked about before a launch
LEVEL_LABELS = {"high": "높음", "medium": "보통", "low": "낮음", "info": "참고"}
# Issue.action -> the button that may help
ACTIONS = {
    "fix_config": "설정 고치기", "matfix": "재질 복구", "matfix_check": "재질 점검", "update_refw": "REFramework 업데이트",
    "install_refw": "REFramework 설치", "enable_refw": "REFramework 켜기", "apply": "다시 적용",
    "crash_record": "튕김 기록 보기", "open_folder": "폴더 열기", "show_mods": "모드 보기",
}
KINDS = (
    # the mods' own problems (also the main list's ⚠ marks)
    "material", "stuck", "unchecked", "unchecked_new", "requirement", "option", "hidden",
    # the game folder
    "refw_missing", "refw_off", "refw_old", "config_loose", "config_pak", "config_faulty", "pak_count",
    "pak_unknown", "pak_case", "dll_vr", "dll_proxy", "dll_other", "plugin_unmanaged", "script_unmanaged",
    "verify",
    # from background(): the last session's records and the loose files the manager did not place
    "faulty", "faulty_loose", "faulty_pak", "refw_failed", "refw_appdata", "natives_unmanaged", "natives_stale",
)

FAULTY_KEY = "FaultyFileDetector_Enabled"
CONFIG_DEFAULTS = {crashlog.LOOSE_FILES_KEY: False, crashlog.PAK_DIRECTORY_KEY: True, FAULTY_KEY: True}
CONFIG_LABELS = {crashlog.LOOSE_FILES_KEY: "루스 파일 읽기(LooseFileLoader)",
                 crashlog.PAK_DIRECTORY_KEY: "pak_mods 읽기(LoadPakDirectory)",
                 FAULTY_KEY: "읽지 못한 파일 기록(FaultyFileDetector)"}
# Unverified: #1704 reports start-up crashes from about 80 (and 96, 130) PAKs on older builds, and the
# current code has no visible bound - the real limit is the engine's patch slots.
PAK_COUNT_WARN = 80
VR_DLLS = ("openvr_api.dll", "openxr_loader.dll")  # in REFramework.zip, for VR only
# files in the game folder that tell which program a proxy DLL belongs to
PROGRAM_HINTS = (("reshade.ini", "ReShade"), ("reshade.log", "ReShade"), ("reshade-shaders", "ReShade"),
                 ("optiscaler.ini", "OptiScaler"), ("optiscaler.log", "OptiScaler"), ("dxvk.conf", "DXVK"),
                 ("dlssg_to_fsr3", "DLSS→FSR3 프레임 생성"), ("specialk", "Special K"))
_MANAGER_PAK = re.compile(r"^\d{3}_[^.]+\.pak$")  # deploy.pak_mods_name
_MATERIAL = re.compile(r"\.(?:mdf2|user|pfb|scn)\.\d+$", re.IGNORECASE)
_RSZ = re.compile(r"\.(?:user|pfb|scn)$", re.IGNORECASE)
REFW_SLACK = 3600  # a game file written this soon after REFramework is the same install, not an update
SESSION_SLACK = 120  # a mod file placed this soon after a session ended was already there (clock, copy time)
SHOWN = 3  # names in one line
DETAIL = 10  # lines under an issue
MAX_SCAN = 5000  # entries looked at in pak_mods and REFramework's folders (fast part)
MAX_NATIVES_SCAN = 100_000  # entries looked at under natives\ (background)
NATIVES_SAMPLE = 20
MAX_MATERIAL_CHECK = 1000  # loose files checked in one background run (the rest the next time)

STATE_FILE = "precheck.json"  # in the data folder: what the user said not to be asked about again
STATE_FORMAT = 1
MAX_SEEN = 2000
BACKGROUND_FILE = "precheck-scan.json"  # in the cache folder
BACKGROUND_FORMAT = 3  # 3: material verdicts of matfix format 4 (results of older formats are dropped)
BACKGROUND_DAYS = 1  # an older background result is looked at again


# ---- what is found -------------------------------------------------------------------------------
@dataclass
class Issue:
    """One thing to look at. ``text`` is one Korean line (without the bullet); blame is worded as a
    possibility. ``items`` are stable keys of what it is about (file names, mod ids with their stamp):
    an issue is new while one of them was not seen (remembered) before."""
    kind: str  # see KINDS
    level: str  # see LEVELS
    text: str
    detail: list[str] = field(default_factory=list)  # file names, evidence, setting lines
    mods: list[str] = field(default_factory=list)  # ids of the mods it is about
    action: str = ""  # see ACTIONS ("" for none)
    fix: tuple[str, str] | None = None  # "fix_config": (REFramework setting, value) for set_refw_option
    config: str = ""  # "fix_config": the settings file it is about (set_refw_option's path)
    folder: str = ""  # "open_folder": game-relative folder ("" for the game folder itself)
    items: list[str] = field(default_factory=list)

    @property
    def keys(self) -> list[str]:
        return [f"{self.kind}:{item}" for item in (self.items or [""])]

    @property
    def action_label(self) -> str:
        return ACTIONS.get(self.action, "")


@dataclass
class ModState:
    """What the checks need to know of one installed mod (see mod_states)."""
    id: str
    name: str
    enabled: bool
    stamp: str = ""  # Mod.stamp (installed_at|updated_at[|edited_at]), as in the crash records' context
    kinds: frozenset = frozenset()  # analyzer.classify of the files it applies ("loose", "pak", "script"...)
    files: Sequence[str] = ()  # game paths it applies (manager.mod_files)
    needs_option: bool = False
    missing: Sequence[str] = ()  # names of requirements missing or off (enabled mods only)
    material_state: str = ""  # manager.material_state (enabled mods only)
    after_update: bool = False  # "unchecked" because the game was updated since its last check (not never checked)
    stuck: Sequence[dict] = ()  # manager.material_stuck: [{"label", "kind", "reason"}]
    hidden: bool = False  # every file loses to other mods
    refw: bool = False  # the mod that provides REFramework's dinput8.dll
    edit_issues: Sequence[str] = ()  # manager.edit_issues: what of its 모드 수정 does not reach the game (enabled mods)


@dataclass
class Facts:
    """The manager's state for :func:`check` and :func:`background` (plain data: gathered on the GUI
    thread, usable on any other)."""
    game_dir: Path | None
    mods: list[ModState] = field(default_factory=list)  # in load order
    deployed: dict = field(default_factory=dict)  # manager.deployed_files(): lower-cased game path -> entry
    backups: dict = field(default_factory=dict)  # manager.deployed_backups()
    verify: list[str] = field(default_factory=list)  # manager.verify()
    game_version: str = ""  # manager.game_version()
    refw_status: object = None  # refw.Status when it was looked up (None: not known)
    appdata: str | None = None  # stands in for %APPDATA% (tests); None: the real one
    matfix: bool = True  # the material check can run here (matfix.available())
    checking: bool = False  # the mods' own material check is running now (the GUI's)


def stuck_summary(stuck: Sequence[dict]) -> str:
    """``.user·.pfb 파일 3개``."""
    kinds = sorted({"." + i.get("kind", "") for i in stuck})
    return f"{'·'.join(kinds)} 파일 {len(stuck)}개"


def _names(names: Sequence[str], shown: int = SHOWN) -> str:
    """``'A', 'B', 'C' 외 2개``."""
    text = ", ".join(f"'{n}'" for n in names[:shown])
    return text + (f" 외 {len(names) - shown}개" if len(names) > shown else "")


def _file_names(paths: Sequence[str], shown: int = SHOWN) -> str:
    return _names([crashlog.module_name(p) for p in paths], shown)


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# ---- the mods' own problems ------------------------------------------------------------------------
def mod_problems(mods: Sequence[ModState]) -> dict[str, list[str]]:
    """Why each enabled mod cannot fully work: {mod id: [reason]} (the main list's ⚠ marks and tooltips)."""
    problems: dict[str, list[str]] = {}
    for mod in mods:
        if not mod.enabled:
            continue
        reasons = []
        if mod.needs_option:
            reasons.append("옵션을 하나도 켜지 않아 아무 파일도 적용되지 않습니다")
        if mod.missing:
            reasons.append("필요한 모드가 없거나 꺼져 있습니다: " + ", ".join(mod.missing))
        if mod.material_state == "stale":
            reasons.append("재질이나 .user·.pfb 파일이 현재 게임과 맞지 않습니다 (오른쪽 클릭 → 재질 복구)")
        if mod.stuck:
            reasons.append(f"현재 게임과 맞지 않지만 고칠 수 없는 {stuck_summary(mod.stuck)}이 있어 게임이 멈출 수 "
                           "있습니다 (정보 탭 참고)")
        if mod.edit_issues:
            reasons.append(f"모드 수정 내용 일부가 적용되지 않았습니다: {mod.edit_issues[0]} (모드 수정 → 수정 내용)")
        if reasons:
            problems[mod.id] = reasons
    for mod in mods:
        if mod.enabled and mod.hidden:
            problems.setdefault(mod.id, []).append("파일이 모두 다른 모드에 밀려 아무것도 적용되지 않습니다")
    return problems


def mod_issues(mods: Sequence[ModState], unchecked: bool = True, checking: bool = False) -> list[Issue]:
    """The mods' own problems as issues, one per kind of problem (``unchecked``: also the mods whose
    materials were not checked against this game version yet, when that check can run: asked about
    after a game update, worth knowing for a new mod; ``checking``: the mods' own check runs now)."""
    on = [m for m in mods if m.enabled]
    out = []

    def group(kind, level, found, one, many, action="show_mods", detail=None, item=None):
        if not found:
            return
        names = [m.name for m in found]
        text = one(found[0]) if len(found) == 1 else many(len(found), _names(names))
        out.append(Issue(kind, level, text, detail(found) if detail else [], [m.id for m in found], action,
                         items=[item(m) if item else f"{m.id}|{m.stamp}" for m in found]))

    group("material", "high", [m for m in on if m.material_state == "stale"],
          lambda m: f"'{m.name}'의 재질·.user·.pfb 파일이 현재 게임과 맞지 않습니다 → 재질 복구를 해 보세요. 그대로 두면 "
                    "텍스처가 깨지거나 게임이 검은 화면에서 멈출 수 있습니다.",
          lambda n, names: f"재질·.user·.pfb 파일이 현재 게임과 맞지 않는 모드 {n}개: {names} → 재질 복구를 해 보세요. "
                           "그대로 두면 텍스처가 깨지거나 게임이 검은 화면에서 멈출 수 있습니다.", "matfix")
    group("stuck", "high", [m for m in on if m.stuck],
          lambda m: f"'{m.name}'에 현재 게임과 맞지 않지만 고칠 수 없는 {stuck_summary(m.stuck)}이 있어 게임이 멈출 수 "
                    "있습니다 → 모드 업데이트를 확인하거나 이 모드를 꺼 보세요.",
          lambda n, names: f"현재 게임과 맞지 않지만 고칠 수 없는 파일이 있는 모드 {n}개: {names}. 게임이 멈출 수 있습니다 "
                           "→ 모드 업데이트를 확인하거나 꺼 보세요.",
          detail=lambda found: [f"'{m.name}': " + ", ".join(i.get("label", "?").rsplit("/", 1)[-1] for i in m.stuck[:3])
                                + (f" 외 {len(m.stuck) - 3}개" if len(m.stuck) > 3 else "") for m in found[:DETAIL]],
          item=lambda m: f"{m.id}|{m.stamp}|{len(m.stuck)}")
    not_checked = [m for m in on if m.material_state == "unchecked"] if unchecked else []
    if checking:  # (the mods' own check finds what does not fit; then they are stale and asked about)
        group("unchecked", "medium", [m for m in not_checked if m.after_update],
              lambda m: f"게임이 업데이트되어 '{m.name}'의 재질·.user·.pfb 파일을 점검하는 중입니다 → 점검이 끝난 뒤 "
                        "실행하면 맞지 않는 파일을 먼저 고칠 수 있습니다. 게임 업데이트 뒤에는 맞지 않는 파일 때문에 검은 "
                        "화면에서 멈추는 경우가 많습니다.",
              lambda n, names: f"게임이 업데이트되어 모드 {n}개({names})의 재질·.user·.pfb 파일을 점검하는 중입니다 → 점검이 "
                               "끝난 뒤 실행하면 맞지 않는 파일을 먼저 고칠 수 있습니다. 게임 업데이트 뒤에는 맞지 않는 파일 "
                               "때문에 검은 화면에서 멈추는 경우가 많습니다.")
    else:
        group("unchecked", "medium", [m for m in not_checked if m.after_update],
              lambda m: f"게임이 업데이트된 뒤 '{m.name}'의 재질·.user·.pfb 파일을 아직 점검하지 않았습니다 → 재질 점검을 "
                        "해 보세요. 게임 업데이트 뒤에는 맞지 않는 파일 때문에 검은 화면에서 멈추는 경우가 많습니다.",
              lambda n, names: f"게임이 업데이트된 뒤 재질·.user·.pfb 파일을 아직 점검하지 않은 모드 {n}개: {names} → 재질 "
                               "점검을 해 보세요. 게임 업데이트 뒤에는 맞지 않는 파일 때문에 검은 화면에서 멈추는 경우가 "
                               "많습니다.", "matfix_check")
        group("unchecked_new", "low", [m for m in not_checked if not m.after_update],
              lambda m: f"'{m.name}'의 재질·.user·.pfb 파일을 아직 지금 게임 버전에서 점검하지 않았습니다 → 게임 업데이트 "
                        "전에 만든 모드라면 재질 점검을 해 보세요.",
              lambda n, names: f"재질·.user·.pfb 파일을 아직 지금 게임 버전에서 점검하지 않은 모드 {n}개: {names} → 게임 "
                               "업데이트 전에 만든 모드라면 재질 점검을 해 보세요.", "matfix_check")
    group("requirement", "medium", [m for m in on if m.missing],
          lambda m: f"'{m.name}'에 필요한 모드가 없거나 꺼져 있습니다: {', '.join(m.missing)}",
          lambda n, names: f"필요한 모드가 없거나 꺼져 있는 모드 {n}개: {names}",
          detail=lambda found: [f"'{m.name}': {', '.join(m.missing)}" for m in found[:DETAIL]] if found[1:] else [],
          item=lambda m: f"{m.id}|{'|'.join(sorted(m.missing))}")
    group("option", "low", [m for m in on if m.needs_option],
          lambda m: f"'{m.name}'의 옵션을 하나도 켜지 않아 아무 파일도 적용되지 않습니다.",
          lambda n, names: f"옵션을 하나도 켜지 않아 아무 파일도 적용되지 않는 모드 {n}개: {names}")
    group("hidden", "low", [m for m in on if m.hidden],
          lambda m: f"'{m.name}'의 파일이 모두 다른 모드에 밀려 아무것도 적용되지 않습니다.",
          lambda n, names: f"파일이 모두 다른 모드에 밀려 아무것도 적용되지 않는 모드 {n}개: {names}")
    return out


# ---- REFramework and its settings --------------------------------------------------------------------
@dataclass
class RefwConfig:
    """``re2_fw_config.txt`` as REFramework reads it (key=value split at the first ``=``)."""
    path: Path | None = None
    exists: bool = False
    values: dict = field(default_factory=dict)
    appdata: bool = False  # %APPDATA%'s, as REFramework used that folder in the last session

    def value(self, key: str) -> str | None:
        return self.values.get(key)

    def flag(self, key: str, default: bool | None = None) -> bool:
        """The setting as REFramework uses it: exactly ``true`` or ``false``, anything else (or no line)
        its default."""
        value = self.values.get(key)
        default = CONFIG_DEFAULTS.get(key, False) if default is None else default
        return True if value == "true" else False if value == "false" else default


def read_config(game_dir, appdata=None, fallback: bool | None = None) -> RefwConfig:
    """REFramework's settings in use (none: every default). ``fallback``: REFramework used %APPDATA% in
    the last session (True), the game folder (False), or not known (None: the game folder's file, else
    %APPDATA%'s) - see crashes.config_path."""
    path = crashes.config_path(game_dir, appdata, fallback) if game_dir else None
    if path is None:
        return RefwConfig()
    used = bool(fallback) and not fsutil.same_path(path.parent, game_dir)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return RefwConfig(path, appdata=used)
    except OSError as exc:
        log.debug("REFramework 설정 파일을 읽지 못했습니다 (%s): %s", path, exc)
        return RefwConfig(path, appdata=used)
    return RefwConfig(path, True, crashlog.parse_config(text), used)


def users(facts: Facts) -> dict[str, list[str]]:
    """Ids of the mods that need REFramework's loose file loading ("loose"), pak_mods ("pak") or its
    script and plugin loading ("script"): the enabled ones and those with files in the game folder now."""
    out: dict[str, list[str]] = {"loose": [], "pak": [], "script": []}
    for m in facts.mods:
        if m.enabled and not m.refw:
            for kind, ids in out.items():
                if kind in m.kinds:
                    ids.append(m.id)
    refw = {m.id for m in facts.mods if m.refw}
    for key, entry in facts.deployed.items():
        mod = entry.get("mod") if isinstance(entry, dict) else None
        if not mod or mod in refw:
            continue
        kind = ("loose" if key.startswith("natives/") else "pak" if key.startswith(PAK_MODS_DIR + "/")
                else "script" if key.startswith("reframework/") else "")
        if kind and mod not in out[kind]:
            out[kind].append(mod)
    return out


def refw_present(facts: Facts) -> bool:
    """REFramework's dinput8.dll is (or after applying will be) in the game folder."""
    from .bisect import backed_up_refw

    game = Path(facts.game_dir) if facts.game_dir else None
    on_disk = game is not None and (game / REFRAMEWORK_DLL).is_file()
    refw = next((m for m in facts.mods if m.refw), None)
    if refw is None:
        return on_disk
    if refw.enabled:
        return True
    managed = REFRAMEWORK_DLL in facts.deployed
    return (on_disk and not managed) or backed_up_refw(facts.backups)


def config_issues(config: RefwConfig, need: dict[str, list[str]]) -> list[Issue]:
    """REFramework's settings that keep mods from loading (or crash records from being written)."""
    out = []
    shown = crashlog.mask_user(str(config.path)) if config.path else ""

    def line(key):
        value = config.value(key)
        return f"{key}={value}" if value is not None else f"{key} 줄 없음 (기본값 {str(CONFIG_DEFAULTS[key]).lower()})"

    def issue(kind, level, key, text, mods):
        detail = [line(key)] + ([f"설정 파일: {shown}" + ("" if config.exists else " (아직 없음)")]
                                if shown and (config.exists or config.appdata) else [])
        out.append(Issue(kind, level, text, detail, list(mods), "fix_config", (key, "true"),
                         config=str(config.path) if config.path else "", items=[f"{key}={config.value(key)}"]))

    loose = need.get("loose") or []
    if loose and not config.flag(crashlog.LOOSE_FILES_KEY):
        if config.exists:
            text = (f"REFramework 설정에서 루스 파일 읽기(LooseFileLoader)가 꺼져 있어 루스 파일(natives) 모드 "
                    f"{len(loose)}개가 적용되지 않습니다 → 설정을 켜세요.")
        else:
            text = (f"REFramework 설정 파일이 아직 없습니다. 처음에는 루스 파일 읽기(LooseFileLoader)가 꺼져 있어 루스 "
                    f"파일(natives) 모드 {len(loose)}개가 적용되지 않습니다 → 미리 켜 둘 수 있습니다.")
        issue("config_loose", "high", crashlog.LOOSE_FILES_KEY, text, loose)
    paks = need.get("pak") or []
    if paks and not config.flag(crashlog.PAK_DIRECTORY_KEY):
        issue("config_pak", "high", crashlog.PAK_DIRECTORY_KEY,
              f"REFramework 설정에서 pak_mods 읽기(LoadPakDirectory)가 꺼져 있어 PAK 모드 {len(paks)}개가 적용되지 "
              "않습니다 → 설정을 켜세요.", paks)
    if config.exists and not config.flag(FAULTY_KEY):
        issue("config_faulty", "low", FAULTY_KEY,
              "REFramework의 읽지 못한 파일 기록(FaultyFileDetector)이 꺼져 있어, 게임이 멈추거나 튕겨도 어떤 모드 파일을 "
              "읽지 못했는지 남지 않습니다 → 켜 두는 것이 좋습니다.", [])
    return out


def fix_question(issue: Issue) -> str:
    """The consent question for a "fix_config" issue."""
    key, value = issue.fix or ("", "")
    label = CONFIG_LABELS.get(key, key)
    what = "켭니다" if value == "true" else "끕니다" if value == "false" else f"{value}(으)로 바꿉니다"
    return (f"REFramework 설정에서 {label} 항목을 {what} (re2_fw_config.txt의 {key}={value}).\n"
            "REFramework는 게임을 끌 때 이 파일을 다시 쓰므로 게임이 꺼져 있을 때만 바꿀 수 있습니다. 바꿀까요?")


def game_updated(game_dir) -> float | None:
    """When the game was last updated: when its exe was written (a title update that changes the game's
    code, what REFramework depends on, rewrites it), else Steam's record of the last update, else when
    its base paks were written. Never the patch paks: other programs and old guides put some there."""
    game = Path(game_dir)
    st = fsutil.stat_or_none(game / GAME_EXE)
    if st is not None:
        return st.st_mtime
    steam = crashes.steam_updated(game)
    if steam is not None:
        return steam
    times = [st.st_mtime for st in (fsutil.stat_or_none(game / name) for name in ("re_chunk_000.pak", crashes.PAK_FAMILY))
             if st is not None]
    return max(times) if times else None


def _day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def refw_issues(facts: Facts, need: dict[str, list[str]], present: bool) -> list[Issue]:
    """REFramework missing (or off) while mods need it, or older than the game after an update (a hint:
    file times are local install times, so it is worded as one)."""
    out = []
    needing = list(dict.fromkeys(i for ids in need.values() for i in ids))
    refw = next((m for m in facts.mods if m.refw), None)
    if needing and not present:
        if refw is not None and not refw.enabled:
            out.append(Issue("refw_off", "medium", f"REFramework가 꺼져 있어 루스 파일·PAK·스크립트 모드 {len(needing)}개가 "
                             "작동하지 않습니다 → REFramework를 켜세요.", [f"REFramework 모드: '{refw.name}'"],
                             [refw.id] + needing, "enable_refw", items=[refw.id]))
        else:
            out.append(Issue("refw_missing", "medium", f"REFramework(dinput8.dll)가 없어 루스 파일·PAK·스크립트 모드 "
                             f"{len(needing)}개가 작동하지 않습니다 → REFramework를 설치하세요.", [], needing,
                             "install_refw", items=[""]))
        return out
    game = Path(facts.game_dir)
    st = fsutil.stat_or_none(game / REFRAMEWORK_DLL)
    updated = game_updated(game)
    if st is None or updated is None or updated <= st.st_mtime + REFW_SLACK:
        return out
    detail = [f"dinput8.dll: {_day(st.st_mtime)}", f"게임 업데이트: {_day(updated)}"]
    items = [f"{st.st_size}:{int(st.st_mtime)}"]
    status = facts.refw_status
    if status is not None and getattr(status, "installed", False):
        current = getattr(status, "current", None)
        if current is None:
            out.append(Issue("refw_old", "medium", "설치된 REFramework가 최근 빌드 목록에 없고(오래된 빌드이거나 REFramework가 "
                             "아닌 dinput8.dll), 그 뒤에 게임이 업데이트되었습니다 → 게임이 튕기거나 모드가 적용되지 않으면 "
                             "REFramework를 최신 빌드로 업데이트하세요.", detail, [], "update_refw", items=items))
        elif not status.up_to_date:
            out.append(Issue("refw_old", "medium", f"REFramework가 최신보다 {status.behind}개 이전 빌드({current.label})이고, "
                             "그 뒤에 게임이 업데이트되었습니다 → 게임이 튕기거나 모드가 적용되지 않으면 REFramework를 "
                             "업데이트하세요.", detail + [f"최신: {status.latest.label}"], [], "update_refw", items=items))
        return out
    if needing:
        out.append(Issue("refw_old", "low", "REFramework(dinput8.dll)를 넣은 뒤에 게임이 업데이트되었습니다. 게임이 시작하자마자 "
                         "튕기거나 모드가 적용되지 않으면 REFramework를 최신 빌드로 업데이트해 보세요.", detail, [],
                         "update_refw", items=items))
    return out


# ---- the game folder -----------------------------------------------------------------------------------
def _scandir(path) -> list:
    try:
        return sorted(os.scandir(fsutil.longpath(path)), key=lambda e: e.name.casefold())
    except OSError:
        return []


def _child(folder: Path, name: str) -> Path | None:
    """A subfolder by name, any case (as Windows finds it)."""
    low = name.casefold()
    for e in _scandir(folder):
        try:
            if e.name.casefold() == low and e.is_dir():
                return folder / e.name
        except OSError:
            continue
    return None


def _walk(game: Path, folder: Path, limit: int) -> tuple[list[str], bool]:
    """Game-relative paths ("/") of every file below ``folder``, looking at no more than ``limit``
    entries (True: the limit was reached)."""
    out, seen, stack = [], 0, [folder]
    base = len(str(game)) + 1
    while stack:
        current = stack.pop()
        for e in _scandir(current):
            seen += 1
            if seen > limit:
                return out, True
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(current / e.name)
                elif e.is_file():
                    out.append(str(current / e.name)[base:].replace("\\", "/"))
            except OSError:
                continue
    return out, False


@dataclass
class PakScan:
    loaded: list[str] = field(default_factory=list)  # game paths of the .pak files REFramework mounts
    unknown: list[str] = field(default_factory=list)  # of those, the ones the manager did not place
    wrong_case: list[str] = field(default_factory=list)  # ".PAK", ".Pak": REFramework's test is case-sensitive
    capped: bool = False


def scan_pak_mods(game_dir, deployed: dict) -> PakScan:
    """pak_mods as REFramework reads it: every file ending in ``.pak`` (exactly), subfolders included."""
    game = Path(game_dir)
    folder = game / PAK_MODS_DIR
    scan = PakScan()
    if not folder.is_dir():
        return scan
    files, scan.capped = _walk(game, folder, MAX_SCAN)
    for rel in sorted(files, key=str.casefold):
        if rel.endswith(".pak"):
            scan.loaded.append(rel)
            if rel.casefold() not in deployed:
                scan.unknown.append(rel)
        elif rel.lower().endswith(".pak"):
            scan.wrong_case.append(rel)
    return scan


def pak_issues(scan: PakScan) -> list[Issue]:
    out = []
    count = len(scan.loaded)
    if count >= PAK_COUNT_WARN:
        managed = count - len(scan.unknown)
        out.append(Issue("pak_count", "medium", f"pak_mods에 PAK이 {count}개{' 넘게' if scan.capped else ''} 있습니다. "
                         f"{PAK_COUNT_WARN}개쯤을 넘으면 게임이 시작할 때 튕긴다는 보고가 있습니다(정확한 한도는 알려지지 않은 "
                         "추정치입니다) → 시작할 때 튕기면 PAK 모드를 줄여 보세요.",
                         [f"매니저가 넣은 PAK {managed}개, 넣지 않은 PAK {len(scan.unknown)}개"], [], "open_folder",
                         folder=PAK_MODS_DIR, items=[str(count // 10)]))
    if scan.unknown:
        n = len(scan.unknown)
        where = f"'{crashlog.module_name(scan.unknown[0])}'이" if n == 1 else f"{n}개({_file_names(scan.unknown)})가"
        detail = scan.unknown[:DETAIL] + ([f"… 외 {n - DETAIL}개"] if n > DETAIL else [])
        if any(_MANAGER_PAK.match(crashlog.module_name(p)) for p in scan.unknown):
            detail.append("이름이 이 매니저의 방식(001_이름.pak)과 같은 PAK이 있습니다. 데이터 폴더를 옮기거나 지웠다면 예전에 "
                          "매니저가 넣은 것일 수 있습니다.")
        out.append(Issue("pak_unknown", "medium", f"pak_mods에 매니저가 넣지 않은 PAK {where} 있습니다. REFramework가 함께 "
                         "불러옵니다 → 직접 넣은 것이 아니거나 게임 업데이트 전에 넣은 것이면 빼 보세요.", detail, [],
                         "open_folder", folder=PAK_MODS_DIR, items=[p.casefold() for p in scan.unknown]))
    if scan.wrong_case:
        out.append(Issue("pak_case", "low", f"pak_mods의 {_file_names(scan.wrong_case)}은(는) 확장자가 소문자 '.pak'이 "
                         "아니라 REFramework가 읽지 않습니다 → 확장자를 .pak으로 바꾸거나 빼세요.", scan.wrong_case[:DETAIL],
                         [], "open_folder", folder=PAK_MODS_DIR, items=[p.casefold() for p in scan.wrong_case]))
    return out


@dataclass
class RootScan:
    vr: list[tuple[str, str]] = field(default_factory=list)  # (file name, id of the mod that placed it or "")
    proxies: list[str] = field(default_factory=list)  # DLLs Windows loads from here first, the manager did not place
    others: list[str] = field(default_factory=list)  # other .dll/.asi the manager did not place and the game lacks
    programs: list[str] = field(default_factory=list)  # programs recognised by their files (ReShade, OptiScaler...)


def scan_root(game_dir, deployed: dict) -> RootScan:
    """The DLLs (and .asi) directly in the game folder."""
    scan = RootScan()
    programs = []
    for e in _scandir(game_dir):
        name, low = e.name, e.name.casefold()
        for hint, program in PROGRAM_HINTS:
            if low.startswith(hint) and program not in programs:
                programs.append(program)
        try:
            if not e.is_file():
                continue
        except OSError:
            continue
        if not low.endswith((".dll", ".asi")):
            continue
        owner = deployed.get(low, {}).get("mod", "") if isinstance(deployed.get(low), dict) else ""
        if low in VR_DLLS:
            scan.vr.append((name, owner))
        elif low in deployed or low == REFRAMEWORK_DLL:
            continue  # placed by a mod, or REFramework put there by hand
        elif low in crashlog.PROXY_DLLS:
            scan.proxies.append(name)
        elif low.endswith(".asi") or crashlog.game_place(low)[0] == "root_dll":
            scan.others.append(name)  # (names of the game's upscalers count as the game's: updates add them)
    scan.programs = programs
    return scan


def root_issues(scan: RootScan, names: dict[str, str], refw: Iterable[str] = ()) -> list[Issue]:
    """``names``: mod id -> name; ``refw``: ids of the mods that provide REFramework."""
    out = []
    why = ("REFramework는 VR을 쓰지 않으면 dinput8.dll 말고는 넣지 말라고 안내합니다(게임이 불안정해지고 안티에일리어싱이 "
           "꺼질 수 있음)")
    refw = set(refw)
    loose = [n for n, o in scan.vr if not o]
    if loose:  # put in by hand
        out.append(Issue("dll_vr", "medium", f"게임 폴더에 REFramework의 VR 파일({', '.join(loose)})이 있습니다. {why} "
                         "→ VR을 쓰지 않으면 빼세요.", [], [], "open_folder", items=[n.casefold() for n in loose]))
    mine = [n for n, o in scan.vr if o and o in refw]
    if mine:  # the manager places them again at every apply: deleting them by hand does not last
        owners = list(dict.fromkeys(o for n, o in scan.vr if o in refw))
        out.append(Issue("dll_vr", "low", f"REFramework 모드 {_names([names.get(o, o) for o in owners])}에 VR 파일"
                         f"({', '.join(mine)})이 들어 있어 게임 폴더에 넣었습니다. {why} → VR을 쓰지 않으면 REFramework "
                         "창에서 다시 설치하세요(매니저가 받는 REFramework는 dinput8.dll만 넣습니다).", [], owners,
                         "update_refw", items=[n.casefold() for n in mine]))
    other = [(n, o) for n, o in scan.vr if o and o not in refw]
    if other:
        owners = list(dict.fromkeys(o for _n, o in other))
        out.append(Issue("dll_vr", "medium", f"모드 {_names([names.get(o, o) for o in owners])}이(가) 게임 폴더에 "
                         f"REFramework의 VR 파일({', '.join(n for n, _o in other)})을 넣습니다. {why} → VR을 쓰지 않으면 "
                         "그 모드를 끄거나 VR 파일 없이 다시 설치하세요(게임 폴더에서 지워도 다시 적용할 때 다시 들어갑니다).",
                         [], owners, "show_mods", items=[n.casefold() for n, _o in other]))
    if scan.proxies:
        who = f"{'·'.join(scan.programs)} 파일로 보임" if scan.programs else "ReShade·OptiScaler 같은 프로그램일 수 있음"
        n = len(scan.proxies)
        what = f"'{scan.proxies[0]}'이" if n == 1 else f"DLL {n}개({_names(scan.proxies)})가"
        out.append(Issue("dll_proxy", "medium", f"게임 폴더에 매니저가 넣지 않은 {what} 있습니다({who}). 게임과 함께 실행되는 "
                         "다른 프로그램의 파일입니다 → 게임이 튕기면 먼저 빼고 해 보세요.",
                         list(scan.proxies) if n > 1 else [], [], "open_folder",
                         items=[p.casefold() for p in scan.proxies]))
    if scan.others:
        out.append(Issue("dll_other", "low", f"게임 폴더에 게임에 들어 있지 않은 DLL·ASI {len(scan.others)}개가 있습니다"
                         f"({_names(scan.others)}). 매니저가 넣은 파일이 아닙니다. 게임 업데이트로 생긴 파일일 수도 있으니 "
                         "지우기 전에 확인하세요.", scan.others[:DETAIL], [], "open_folder",
                         items=[p.casefold() for p in scan.others]))
    return out


def scan_reframework(game_dir, deployed: dict) -> tuple[list[str], list[str]]:
    """(plugins, scripts) REFramework loads that the manager did not place: ``reframework\\plugins\\*.dll``
    and ``reframework\\autorun\\*.lua`` (REFramework reads only those folders themselves, not below)."""
    game = Path(game_dir)
    found: dict[str, list[str]] = {"plugins": [], "autorun": []}
    top = _child(game, "reframework")
    for sub, ext in (("plugins", ".dll"), ("autorun", ".lua")):
        folder = _child(top, sub) if top is not None else None
        if folder is None:
            continue
        for e in _scandir(folder)[:MAX_SCAN]:
            try:
                if e.is_file() and e.name.casefold().endswith(ext):
                    rel = f"{top.name}/{folder.name}/{e.name}"
                    if rel.casefold() not in deployed:
                        found[sub].append(rel)
            except OSError:
                continue
    return found["plugins"], found["autorun"]


def unmanaged_issues(plugins: Sequence[str], scripts: Sequence[str]) -> list[Issue]:
    out = []
    if plugins:
        out.append(Issue("plugin_unmanaged", "low", f"매니저가 넣지 않은 REFramework 플러그인 {len(plugins)}개가 있습니다"
                         f"({_file_names(plugins)}). 원인 모드 찾기로 켜고 끌 수 없고, 게임 업데이트 뒤에는 플러그인도 새 "
                         "버전이 필요한 경우가 많습니다.", list(plugins[:DETAIL]), [], "open_folder",
                         folder="reframework/plugins", items=[p.casefold() for p in plugins]))
    if scripts:
        out.append(Issue("script_unmanaged", "low", f"매니저가 넣지 않은 Lua 스크립트 {len(scripts)}개가 있습니다"
                         f"({_file_names(scripts)}). 원인 모드 찾기로 켜고 끌 수 없습니다.", list(scripts[:DETAIL]), [],
                         "open_folder", folder="reframework/autorun", items=[p.casefold() for p in scripts]))
    return out


def verify_issues(problems: Sequence[str]) -> list[Issue]:
    if not problems:
        return []
    more = f" 외 {len(problems) - 1}개" if len(problems) > 1 else ""
    return [Issue("verify", "medium", f"게임 폴더의 모드 파일에 문제가 있습니다: {problems[0]}{more} → 다시 적용하세요.",
                  list(problems[:DETAIL]), [], "apply", items=list(problems))]


# ---- the slow part ---------------------------------------------------------------------------------------
@dataclass
class Background:
    """What :func:`background` found (kept in the cache folder; :func:`check` keeps what still holds)."""
    game_version: str = ""
    made: float = 0.0
    matfix_format: int = 0
    session: str = ""  # the last game session looked at (crashes.Session.id)
    session_key: str = ""  # its id and copied files (a session that grew since is looked at again)
    session_end: float = 0.0
    session_game: str = ""  # the game version in it ("" when not known)
    session_log: bool = False  # it has REFramework's log (then ``appdata`` says which folder REFramework used)
    session_loose: bool | None = None  # REFramework's loose file loading was on in it (None: not known)
    # files REFramework could not load that an enabled mod has:
    # {"mod", "name", "stamp" (in the session, "" unknown), "on" (in the session: True, False, None unknown),
    #  "sure" (the mod's copy was the one in the game folder then: False when that is not known),
    #  "paths" (the mod's game paths they are in), "entries": [[label, path]]}
    faulty: list = field(default_factory=list)
    faulty_loose: list = field(default_factory=list)  # [[entry path, game path of a loose copy no mod placed]]
    faulty_paks: dict = field(default_factory=dict)  # PAK no mod placed -> entry paths
    refw_failed: list = field(default_factory=list)  # [[what, log line]]: REFramework did not start properly
    refw_commit: str = ""  # REFramework build of that session
    refw_same: bool | None = None  # the dinput8.dll there now is that build
    refw_dll: list = field(default_factory=list)  # [size, mtime_ns] of dinput8.dll when looked at
    appdata: bool = False  # REFramework used %APPDATA% in that session (the game folder was not writable)
    natives_made: float = 0.0  # when natives\ was looked at (0: not yet)
    natives_total: int = 0  # loose files under natives\ no mod placed
    natives_sample: list = field(default_factory=list)
    natives_capped: bool = False
    materials: dict = field(default_factory=dict)  # game path -> [size, mtime_ns, state, kind] of those checked
    matfix: str = ""  # why the material check did not run or failed ("" when it ran or had nothing to do)

    def to_dict(self) -> dict:
        return {"format": BACKGROUND_FORMAT, **{k: getattr(self, k) for k in self.__dataclass_fields__}}

    @classmethod
    def from_dict(cls, data) -> Background | None:
        if not isinstance(data, dict) or data.get("format") != BACKGROUND_FORMAT:
            return None
        out = cls()
        for key, default in cls().to_dict().items():
            value = data.get(key)
            if key == "format" or value is None:
                continue
            if key in ("refw_same", "session_loose"):
                ok = isinstance(value, bool)
            elif isinstance(default, float):
                ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            else:
                ok = isinstance(value, type(default))
            if ok:
                setattr(out, key, value)
        return out


def load_background(cache_dir) -> Background | None:
    if not cache_dir:
        return None
    try:
        return Background.from_dict(fsutil.read_json(Path(cache_dir) / BACKGROUND_FILE, None))
    except OSError:
        return None


def save_background(cache_dir, bg: Background) -> None:
    try:
        fsutil.atomic_write_json(Path(cache_dir) / BACKGROUND_FILE, bg.to_dict())
    except OSError as exc:
        log.warning("실행 전 점검 결과를 저장하지 못했습니다: %s", exc)


def background_due(facts: Facts, bg: Background | None, crash_dir=None, now: float | None = None) -> bool:
    """Whether :func:`background` should run again: none yet, another game version, a newer session
    collected, or natives\\ not looked at for BACKGROUND_DAYS (cheap: reads the sessions' small files)."""
    if bg is None or bg.game_version != facts.game_version:
        return True
    now = time.time() if now is None else now
    if not now - BACKGROUND_DAYS * 86400 < bg.natives_made < now + 60:
        return True
    try:
        last = _last_session(crash_dir) if crash_dir else None
    except OSError:
        return False
    return (_session_key(last) if last is not None else "") != bg.session_key


def _last_session(crash_dir):
    """The newest kept session with REFramework's log (not a crash dump left alone)."""
    return next((s for s in crashes.sessions(crash_dir) if s.started), None)


def _session_key(s) -> str:
    files = s.info.get("files") if isinstance(s.info.get("files"), dict) else {}
    return "|".join([s.id, *(f"{name}:{r.get('size')}:{r.get('mtime')}" for name, r in sorted(files.items())
                             if isinstance(r, dict))])


def _matfix_format() -> int:
    from .manager import MATFIX_FORMAT

    return MATFIX_FORMAT


def background(facts: Facts, crash_dir=None, cache_dir=None, *, natives: bool = True, materials: bool = True,
               game=None, progress=None, previous: Background | None = None) -> Background:
    """The slow checks (any thread): the last session's records against the mods as they are now, and
    the loose files under natives\\ no mod placed, with a read-only material check of them.

    ``natives`` False keeps what ``previous`` (by default the result kept in ``cache_dir``) found there
    instead of looking again (the session part alone is quick: for right before a launch);
    ``materials`` False looks at the folder but checks no new files (unchanged ones keep their
    result). The material check builds the game's pak index on first use, or takes ``game`` (a matfix
    GameIndex, or a function returning one, called only when there is something to check). The
    result is saved in ``cache_dir``. Never raises for what it reads: a part that fails is left out."""
    progress = progress or (lambda done, total, msg: None)
    if previous is None:
        previous = load_background(cache_dir)
    bg = Background(game_version=facts.game_version, made=time.time(), matfix_format=_matfix_format())
    if crash_dir:
        try:
            _session_part(bg, facts, crash_dir)
        except Exception as exc:  # the records are only a help here
            log.warning("지난 게임 기록을 점검하지 못했습니다: %s", exc)
    if not natives:
        # (material verdicts of older check rules are not kept: then natives\ is due to be looked at again)
        if previous is not None and previous.game_version == facts.game_version \
                and previous.matfix_format == bg.matfix_format:
            bg.natives_made, bg.natives_total, bg.natives_sample, bg.natives_capped = (
                previous.natives_made, previous.natives_total, previous.natives_sample, previous.natives_capped)
            bg.materials, bg.matfix = previous.materials, previous.matfix
    elif facts.game_dir and Path(facts.game_dir).is_dir():
        try:
            _natives_part(bg, facts, previous, materials, game, cache_dir, progress)
        except Exception as exc:
            log.warning("매니저가 넣지 않은 루스 파일을 점검하지 못했습니다: %s", exc)
            bg.matfix = bg.matfix or str(exc)
    if cache_dir:
        save_background(cache_dir, bg)
    return bg


def _dll_has(path: Path, commit: str, chunk: int = 8 * 1024 * 1024) -> bool | None:
    """The DLL holds this commit hash as text, as refw.identify finds builds (None: cannot tell)."""
    want = commit[:12].lower().encode("ascii", errors="replace")
    if len(want) < 7:
        return None
    try:
        with open(fsutil.longpath(path), "rb") as f:
            tail = b""
            while True:
                block = f.read(chunk)
                if not block:
                    return False
                if want in tail + block:
                    return True
                tail = block[-len(want):]
    except OSError:
        return None


def _session_part(bg: Background, facts: Facts, crash_dir) -> None:
    s = _last_session(crash_dir)
    if s is None:
        return
    bg.session, bg.session_key, bg.session_end = s.id, _session_key(s), s.end_time
    context = s.context
    bg.session_game = str(context.get("game_version") or "")
    listed = context.get("mods") if isinstance(context.get("mods"), list) else None
    ran = {m["id"]: m for m in listed or () if isinstance(m, dict) and isinstance(m.get("id"), str)}
    # which mods had files in the game folder then (a mod turned on but not applied had none), and whether
    # the game ran with changes not applied (then what was there may differ from the mods' files now)
    known = isinstance(context.get("placed"), list) and context.get("dirty") is False
    info = s.log()
    bg.session_log = info is not None
    bg.appdata = bool(info and info.appdata)
    bg.session_loose = crashes.session_loose(s)
    if bg.session_loose is None and facts.game_dir:  # (not copied with it: as the settings are now)
        bg.session_loose = read_config(facts.game_dir, facts.appdata, bg.appdata if info else None).flag(
            crashlog.LOOSE_FILES_KEY)
    faulty = s.faulty()
    if faulty and facts.game_dir:
        refs = [crashes.ModRef(m.id, m.name, m.enabled, files=list(m.files)) for m in facts.mods]
        index = crashes.ModIndex(facts.game_dir, facts.deployed, refs).for_session(s)
        match = index.match_faulty(faulty, bg.session_loose)
        mods = {m.id: m for m in facts.mods}
        for mod_id, entries in match.by_mod.items():
            mod = mods.get(mod_id)
            if mod is None or not mod.enabled:
                continue
            keys = {e.key for e in entries}
            paths = sorted({str(e.get("path") or k) for k, e in facts.deployed.items()
                            if isinstance(e, dict) and e.get("mod") == mod_id
                            and (e.get("pak") or crashlog.resource_key(k) in keys)})
            then = ran.get(mod_id)
            bg.faulty.append({"mod": mod_id, "name": mod.name,
                              "stamp": str(then.get("stamp") or "") if then else "",
                              "on": bool(then.get("enabled")) if then else (None if listed is None else False),
                              "sure": known and not index.later_gone(mod_id),
                              "paths": paths, "entries": [[e.label, e.path] for e in entries]})
        bg.faulty_loose = [[f.path, rel] for f, rel in match.loose]
        bg.faulty_paks = {rel: [f.path for f in items] for rel, items in match.paks.items()}
    if info is None:
        return
    bg.refw_commit = info.header.get("commit", "")
    failed = []
    if info.init_errors:
        failed.append(["init", info.init_errors[0]])
    if info.paks_none:
        failed.append(["pak_none", f"{info.paks_none} custom pak(s) were cached but NONE were injected"])
    if info.paks_out_of_range:
        failed.append(["pak_range", info.paks_out_of_range[0]])
    if info.loose_failed:
        failed.append(["loose", info.loose_failed])
    bg.refw_failed = failed
    dll = Path(facts.game_dir) / REFRAMEWORK_DLL if facts.game_dir else None
    st = fsutil.stat_or_none(dll) if dll is not None else None
    if failed and st is not None and bg.refw_commit:
        bg.refw_dll = [st.st_size, st.st_mtime_ns]
        bg.refw_same = _dll_has(dll, bg.refw_commit)


def _natives_part(bg: Background, facts: Facts, previous: Background | None, materials: bool, game, cache_dir,
                  progress) -> None:
    root = Path(facts.game_dir)
    bg.natives_made = time.time()
    folder = _child(root, "natives")
    if folder is None:
        return
    files, bg.natives_capped = _walk(root, folder, MAX_NATIVES_SCAN)
    unmanaged = sorted((rel for rel in files if rel.casefold() not in facts.deployed
                        and not rel.endswith((".mhwmm-tmp", fsutil.PART_SUFFIX))), key=str.casefold)
    bg.natives_total, bg.natives_sample = len(unmanaged), unmanaged[:NATIVES_SAMPLE]
    todo, kept = [], {}
    reuse = previous is not None and previous.game_version == facts.game_version \
        and previous.matfix_format == bg.matfix_format
    for rel in (r for r in unmanaged if _MATERIAL.search(r)):
        st = fsutil.stat_or_none(root / rel)
        if st is None:
            continue
        old = previous.materials.get(rel) if reuse else None
        if isinstance(old, list) and len(old) >= 3 and old[:2] == [st.st_size, st.st_mtime_ns]:
            kept[rel] = old
        else:
            todo.append((rel, st))
    bg.materials = kept
    if not todo or not materials:
        return
    from . import matfix

    if not matfix.available():
        bg.matfix = "게임 파일의 압축(zstd)을 풀 수 있는 모듈이 없어 재질을 점검하지 못했습니다."
        return
    todo = todo[:MAX_MATERIAL_CHECK]
    progress(0, len(todo), "매니저가 넣지 않은 루스 파일의 재질 점검 중...")
    results = check_materials(root, [(rel, root / rel) for rel, _st in todo], cache_dir, game, progress)
    for rel, st in todo:
        state, kind = results.get(rel, ("error", ""))
        bg.materials[rel] = [st.st_size, st.st_mtime_ns, state, kind]


def check_materials(game_dir, files: Sequence[tuple[str, Path]], cache_dir=None, game=None,
                    progress=None) -> dict[str, tuple[str, str]]:
    """Read-only material check (재질 점검) of loose files: game path -> (state, kind). States as in
    matfix (``current``, ``fixable``: no longer fits the game, ``stuck``, ``unresolved``, ``error``).
    Builds the game's pak index (``cache_dir``) unless ``game`` (a matfix GameIndex, or a function
    returning one) is given."""
    from .matfix import rsz
    from .matfix.game import GameIndex, ShaderIndex
    from .matfix.repair import RszContext, check_mod

    game = game() if callable(game) else game
    game = game if game is not None else GameIndex(game_dir, Path(cache_dir) if cache_dir else None, progress)
    provided = {re.sub(r"\.\d+$", "", Path(rel).name).lower() for rel, _ in files}
    try:
        results = check_mod([(rel, rel, Path(path)) for rel, path in files], game, ShaderIndex(game, progress),
                            out=None, progress=progress, provided=provided, rszs=RszContext(game))
    finally:
        rsz.TypeRegistry.release()
    out = {}
    for r in results:
        out[r.key] = ("stuck" if r.state != "fixable" and r.stuck else r.state, r.kind)
    return out


def _placed_after(deployed: dict, path: str, mod_id: str, when: float) -> bool:
    """The mod's file at ``path`` in the game folder now was placed from a source newer than ``when``
    (the mod was updated or repaired since)."""
    entry = deployed.get(path.replace("\\", "/").casefold())
    if not isinstance(entry, dict) or entry.get("mod") != mod_id:
        return False
    mtime = entry.get("src_mtime")
    return isinstance(mtime, (int, float)) and mtime / 1e9 > when + SESSION_SLACK


_REFW_FAILED = {"init": "필요한 게임 기능을 찾지 못함", "pak_none": "pak_mods의 PAK을 하나도 적용하지 못함",
                "pak_range": "pak_mods의 PAK 일부를 적용하지 못함", "loose": "루스 파일 읽기를 켜지 못함"}


def session_issues(facts: Facts, bg: Background, need: dict[str, list[str]], config: RefwConfig) -> list[Issue]:
    """What the last session's records say about the next start, as far as it still holds."""
    out = []
    game = Path(facts.game_dir)
    mods = {m.id: m for m in facts.mods}
    for record in bg.faulty:
        if not isinstance(record, dict):
            continue
        mod = mods.get(record.get("mod"))
        entries = [e for e in record.get("entries") or [] if isinstance(e, list) and len(e) == 2]
        if mod is None or not mod.enabled or record.get("on") is False or not entries:
            continue  # off now, or not on in that session (then those are the game's own files)
        if record.get("stamp") and record["stamp"] != mod.stamp:
            continue  # updated (or installed again) since
        if any(_placed_after(facts.deployed, p, mod.id, bg.session_end) for p in record.get("paths") or []):
            continue  # repaired or replaced since
        names = [crashlog.module_name(p) for _l, p in entries]
        rsz = any(_RSZ.search(crashlog.resource_key(p)) for _l, p in entries)
        only_rsz = all(_RSZ.search(crashlog.resource_key(p)) for _l, p in entries)
        more = f" 외 {len(entries) - 1}개" if len(entries) > 1 else ""
        level, action = "high", "matfix" if rsz else "crash_record"
        what = f"'{mod.name}'의 {names[0]}{more}"
        advice = " → 재질 복구를 해 보세요" if rsz else " → 모드를 최신 버전으로 업데이트하거나 꺼 보세요"
        if only_rsz and mod.material_state == "current":  # (REFramework lists some of the game's own files too: #1677)
            level, action = "low", "crash_record"
            text = (f"지난번 게임에서 REFramework가 {what}을(를) 읽지 못한 파일로 기록했지만, 재질 점검에서는 현재 게임과 맞는 "
                    "파일입니다. 바뀌지 않은 게임 파일도 이렇게 기록되는 경우가 있어 원인이 아닐 수 있습니다 → 게임이 멈추거나 "
                    "튕기면 모드를 업데이트하거나 꺼 보세요")
        elif only_rsz and mod.material_state == "repaired":  # (repaired before that game: it did not help)
            action = "crash_record"
            text = (f"지난번 게임에서 REFramework가 읽지 못한 파일: {what} → 재질 복구를 한 파일인데도 기록되었습니다. 모드를 "
                    "최신 버전으로 업데이트하거나 꺼 보세요")
        elif record.get("sure") is False:  # (which mod's copy was in the game folder then is not certain)
            level = "medium"
            text = (f"지난번 게임에서 REFramework가 읽지 못한 파일 {names[0]}{more}은(는) '{mod.name}'의 파일일 수 있습니다(그때 "
                    f"게임 폴더에 있던 것이 이 모드의 파일인지는 확실하지 않음){advice}")
        else:
            text = f"지난번 게임에서 REFramework가 읽지 못한 파일: {what}{advice}"
        out.append(Issue("faulty", level, text, [f'"{label or "?"}" "{path}"' for label, path in entries[:3]], [mod.id],
                         action, items=[f"{mod.id}|{mod.stamp}|" + _digest("|".join(sorted(crashlog.resource_key(p)
                                                                                          for _l, p in entries)))]))
    loose = [(e[0], e[1]) for e in bg.faulty_loose if isinstance(e, list) and len(e) == 2
             and bg.session_loose is not False  # (loose files were not read then)
             and str(e[1]).casefold() not in facts.deployed and (game / str(e[1])).is_file()]
    if loose:
        more = f" 외 {len(loose) - 1}개" if len(loose) > 1 else ""
        out.append(Issue("faulty_loose", "medium", f"지난번 게임에서 REFramework가 읽지 못한 파일: 매니저가 넣지 않은 "
                         f"'{loose[0][1]}'{more} → 직접 넣은 모드 파일이면 빼거나 매니저로 설치해 보세요.",
                         [rel for _p, rel in loose[:DETAIL]], [], "open_folder",
                         folder=loose[0][1].rsplit("/", 1)[0] if "/" in loose[0][1] else "",
                         items=[rel.casefold() for _p, rel in loose]))
    for rel, paths in sorted(bg.faulty_paks.items()):
        if not isinstance(paths, list) or not paths or rel.casefold() in facts.deployed or not (game / rel).is_file():
            continue
        more = f" 외 {len(paths) - 1}개" if len(paths) > 1 else ""
        out.append(Issue("faulty_pak", "medium", f"지난번 게임에서 REFramework가 읽지 못한 파일 "
                         f"{crashlog.module_name(paths[0])}{more}이(가) 매니저가 넣지 않은 PAK '{rel}'에 있습니다 → 직접 넣은 "
                         "PAK이면 빼고 다시 해 보세요.", [str(p) for p in paths[:3]], [], "open_folder",
                         folder=rel.rsplit("/", 1)[0] if "/" in rel else "", items=[rel.casefold()]))
    failed = [f for f in bg.refw_failed if isinstance(f, list) and len(f) == 2
              and (f[0] != "loose" or need.get("loose")) and (not f[0].startswith("pak") or need.get("pak"))]
    st = fsutil.stat_or_none(game / REFRAMEWORK_DLL)
    if failed and bg.refw_same and st is not None and bg.refw_dll == [st.st_size, st.st_mtime_ns]:
        what = ", ".join(dict.fromkeys(_REFW_FAILED.get(f[0], f[0]) for f in failed))
        when = ("게임 업데이트 전 지난번 게임에서" if bg.session_game and facts.game_version
                and bg.session_game != facts.game_version else "지난번 게임에서")
        out.append(Issue("refw_failed", "high", f"{when} REFramework가 제대로 시작하지 못했고({what}), 그 뒤로 REFramework가 "
                         "바뀌지 않았습니다 → REFramework를 최신 빌드로 업데이트하세요.", [str(f[1]) for f in failed[:3]], [],
                         "update_refw", items=[f"{bg.refw_commit[:12]}|{f[0]}" for f in failed]))
    if bg.appdata:
        evidence = ["Failed to write to current directory, falling back to appdata folder"]
        cause = "→ 게임 폴더가 읽기 전용이거나 백신(랜섬웨어 보호 등)이 막고 있는지 확인하세요."
        scripts = sorted(need.get("script") or [])
        others = sorted(set(need.get("loose") or []) | set(need.get("pak") or []))
        if scripts:  # (keyed on the mods it is about: every session has the line again)
            out.append(Issue("refw_appdata", "medium", "지난번 게임에서 REFramework가 게임 폴더에 쓸 수 없어 %APPDATA%\\"
                             "REFramework 폴더를 썼습니다. 이때는 설정도 그 폴더의 것을 쓰고, 매니저가 게임 폴더에 넣은 "
                             f"스크립트·플러그인은 읽지 않습니다 {cause}", evidence, scripts, items=scripts))
        elif others:
            out.append(Issue("refw_appdata", "low", "지난번 게임에서 REFramework가 게임 폴더에 쓸 수 없어 %APPDATA%\\"
                             "REFramework 폴더를 썼습니다. 이때는 REFramework 설정도 그 폴더의 re2_fw_config.txt를 씁니다 "
                             f"{cause}", evidence, others, items=["settings"]))
    if bg.game_version != facts.game_version:
        return out  # (the material results are of another game version)
    stale = []
    rules = bg.matfix_format == _matfix_format()  # (verdicts of older check rules are not used)
    for rel, record in sorted(bg.materials.items()) if rules else ():
        if not isinstance(record, list) or len(record) < 3 or record[2] not in ("fixable", "stuck"):
            continue
        st = fsutil.stat_or_none(game / rel)
        if st is not None and [st.st_size, st.st_mtime_ns] == record[:2] and rel.casefold() not in facts.deployed:
            stale.append(rel)
    if stale:
        on = config.flag(crashlog.LOOSE_FILES_KEY)
        note = "" if on else " 지금은 루스 파일 읽기가 꺼져 있어 읽히지 않습니다."
        out.append(Issue("natives_stale", "high" if on else "low", f"매니저가 넣지 않은 루스 파일 {len(stale)}개"
                         f"({_file_names(stale)})가 현재 게임과 맞지 "
                         "않습니다 → 직접 넣은 모드라면 빼거나, 매니저로 설치해 재질 복구를 해 보세요. 그대로 두면 텍스처가 "
                         f"깨지거나 게임이 검은 화면에서 멈출 수 있습니다.{note}", stale[:DETAIL], [], "open_folder",
                         folder="natives", items=[r.casefold() for r in stale]))
    rest = bg.natives_total - len(stale)
    if rest > 0:
        count = f"{bg.natives_total}개{' 넘게' if bg.natives_capped else ''}"
        out.append(Issue("natives_unmanaged", "low", f"매니저가 넣지 않은 루스 파일(natives) {count}가 있습니다"
                         f"({_file_names(bg.natives_sample)}). 직접 넣었거나 다른 모드 관리자가 넣은 파일로, 원인 모드 찾기로 "
                         "켜고 끌 수 없습니다.", list(bg.natives_sample[:DETAIL]), [], "open_folder", folder="natives",
                         items=[str(bg.natives_total // 100)]))
    return out


# ---- putting it together ---------------------------------------------------------------------------------
@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)  # the strongest first
    problems: dict = field(default_factory=dict)  # mod id -> reasons (mod_problems)
    game_version: str = ""
    background: str = "none"  # "current" | "outdated" (another game version: material results left out) | "none"

    @property
    def important(self) -> list[Issue]:
        """High and medium issues (the ones a launch asks about)."""
        return [i for i in self.issues if i.level in ASK_LEVELS]

    def title(self, issues: Sequence[Issue] | None = None) -> str:
        issues = self.important if issues is None else issues
        return DIALOG_TITLE.format(count=len(issues)) if issues else f"{TITLE}: {NOTHING}"

    def summary(self) -> str:
        """``실행 전 점검: 높음 1 · 보통 2 · 낮음 3`` for the log."""
        counts = [(LEVEL_LABELS[lv], sum(1 for i in self.issues if i.level == lv)) for lv in LEVELS]
        shown = " · ".join(f"{label} {n}" for label, n in counts if n)
        return f"{TITLE}: {shown or NOTHING}"

    def lines(self, low: bool = True) -> list[str]:
        """The report as text (the CLI and the diagnostics zip)."""
        out = [self.title()]
        for issue in self.issues:
            if issue.level not in ASK_LEVELS and not low:
                continue
            out.append(f"• [{LEVEL_LABELS.get(issue.level, issue.level)}] {issue.text}")
            out += [f"    - {d}" for d in issue.detail[:DETAIL]]
            if issue.fix:
                out.append(f"    ({ACTIONS['fix_config']}: {issue.fix[0]}={issue.fix[1]})")
        return [crashlog.mask_user(line) for line in out]

    def signature(self, issues: Sequence[Issue] | None = None) -> str:
        """A hash of the issue set (the important ones by default)."""
        issues = self.important if issues is None else issues
        return _digest("\n".join(sorted(k for i in issues for k in i.keys)))


def _safe(func, *args) -> list[Issue]:
    try:
        return func(*args)
    except Exception as exc:  # one check that fails must not stop the others (nor a launch)
        log.warning("실행 전 점검 중 일부를 하지 못했습니다 (%s): %s", getattr(func, "__name__", func), exc)
        return []


def check(facts: Facts, background: Background | None = None) -> Report:
    """Every check (fast: a few folder listings, no paks read), with the slow part's result if given.
    Never raises."""
    issues: list[Issue] = []  # what the last session showed first, then the mods, then the game folder
    mods = _safe(mod_issues, facts.mods, facts.matfix and bool(facts.game_version), facts.checking)
    state = "none"
    game = Path(facts.game_dir) if facts.game_dir else None
    if game is not None and game.is_dir():
        try:
            need = users(facts)
        except Exception as exc:
            log.warning("모드가 쓰는 REFramework 기능을 확인하지 못했습니다: %s", exc)
            need = {"loose": [], "pak": [], "script": []}
        try:
            present = refw_present(facts)
        except Exception as exc:
            log.warning("REFramework 설치 여부를 확인하지 못했습니다: %s", exc)
            present = True
        config = RefwConfig()
        # (REFramework reads its settings where it could write the last time: its log says which folder)
        fallback = background.appdata if background is not None and background.session_log else None
        if present:
            try:
                config = read_config(game, facts.appdata, fallback)
            except Exception as exc:
                log.warning("REFramework 설정을 확인하지 못했습니다: %s", exc)
        if background is not None:
            state = "current" if background.game_version == facts.game_version else "outdated"
            issues += _safe(session_issues, facts, background, need, config)
        issues += mods
        if present:
            issues += _safe(config_issues, config, need)
        issues += _safe(refw_issues, facts, need, present)
        issues += _safe(lambda: pak_issues(scan_pak_mods(game, facts.deployed)))
        names = {m.id: m.name for m in facts.mods}
        refw = [m.id for m in facts.mods if m.refw]
        issues += _safe(lambda: root_issues(scan_root(game, facts.deployed), names, refw))
        issues += _safe(lambda: unmanaged_issues(*scan_reframework(game, facts.deployed)))
        issues += _safe(verify_issues, facts.verify)
    else:
        issues = mods
    order = {level: n for n, level in enumerate(LEVELS)}
    issues = sorted(issues, key=lambda i: order.get(i.level, len(LEVELS)))  # (stable: the order above within a level)
    return Report(issues, _problems(facts.mods), facts.game_version, state)


def _problems(mods) -> dict:
    try:
        return mod_problems(mods)
    except Exception as exc:
        log.warning("모드 문제를 확인하지 못했습니다: %s", exc)
        return {}


# ---- "같은 내용이면 다시 묻지 않기" -------------------------------------------------------------------------
def _seen(data_dir, game_version: str) -> dict[str, float]:
    try:
        data = fsutil.read_json(Path(data_dir) / STATE_FILE, {})
    except OSError:
        return {}
    if not isinstance(data, dict) or data.get("format") != STATE_FORMAT or data.get("game") != game_version:
        return {}  # (a game update asks about everything again)
    seen = data.get("seen")
    return {k: float(v) for k, v in seen.items() if isinstance(k, str) and isinstance(v, (int, float))} \
        if isinstance(seen, dict) else {}


def is_new(issue: Issue, seen) -> bool:
    return any(_digest(k) not in seen for k in issue.keys)


def to_ask(report: Report, data_dir) -> list[Issue]:
    """The high and medium issues the user did not say not to be asked about again (empty: launch
    without asking). The dialog lists report.important and may mark these as new."""
    seen = _seen(data_dir, report.game_version)
    return [i for i in report.important if is_new(i, seen)]


def remember(data_dir, issues: Iterable[Issue], game_version: str, now: float | None = None) -> None:
    """The user ticked "같은 내용이면 다시 묻지 않기" for these issues (report.important)."""
    now = time.time() if now is None else now
    seen = _seen(data_dir, game_version)
    for issue in issues:
        for key in issue.keys:
            seen[_digest(key)] = now
    kept = dict(sorted(seen.items(), key=lambda kv: kv[1])[-MAX_SEEN:])
    try:
        fsutil.atomic_write_json(Path(data_dir) / STATE_FILE, {"format": STATE_FORMAT, "game": game_version,
                                                                "seen": kept})
    except OSError as exc:
        log.warning("실행 전 점검 상태를 저장하지 못했습니다: %s", exc)


# ---- from a manager --------------------------------------------------------------------------------------
def shader_state(req: dict) -> str:
    """Where a shader requirement (manager.shader_requirements) stands, in a few words."""
    by, option = req.get("by"), req.get("option")
    if req.get("state") == "ok":
        return f"있음 ({by})"
    if req.get("state") == "off" and option and req.get("mod_off"):
        return f"'{by}'과(와) 그 옵션 '{option}'이(가) 꺼져 있음"
    if req.get("state") == "off" and option:
        return f"'{by}'의 옵션 '{option}'이(가) 꺼져 있음"
    if req.get("state") == "off":
        return f"'{by}'이(가) 꺼져 있음"
    return "게임에도 설치한 모드에도 없음"


def requirement_label(req: dict) -> str:
    """How a missing requirement (manager.requirement_status) is named in the mods' problems: a shader
    (the materials use one the game does not have) with where it is."""
    name = str(req.get("name") or "?")
    if req.get("kind") != "shader":
        return name
    if req.get("state") == "missing":
        return f"{name} (재질이 쓰는 셰이더 파일이 {shader_state(req)})"
    return f"{name} ({shader_state(req)})"


def mod_states(manager, conflicts=None, missing: dict | None = None) -> list[ModState]:
    """ModStates of a manager.ModManager's mods (GUI thread; what its main list computes anyway).
    ``conflicts`` (manager.conflicts()) and ``missing`` ({enabled mod id: missing_requirements}) are
    used when the caller has them already."""
    conflicts = conflicts if conflicts is not None else manager.conflicts()
    hidden = {m.id for m in manager.hidden_mods(conflicts)}
    refw = manager.refw_mod()
    version = manager.game_version()
    out = []
    for m in manager.mods:
        reqs, state, stuck, updated, edits = [], "", [], False, []
        if m.enabled:
            reqs = missing.get(m.id, []) if missing is not None else manager.missing_requirements(m.id)
            state = manager.material_state(m.id)
            stuck = manager.material_stuck(m.id)
            checked = manager.material_checked_for(m.id) if state == "unchecked" else ""
            updated = bool(checked) and checked != version
            edits = manager.edit_issues(m.id)
        files = manager.mod_files(m.id)
        out.append(ModState(m.id, m.name, m.enabled, m.stamp,
                            frozenset(manager.mod_kinds(m.id)), list(files), m.needs_option,
                            [requirement_label(r) for r in reqs], state, updated, list(stuck), m.id in hidden,
                            refw is not None and refw.id == m.id, list(edits)))
    return out


def gather(manager, refw_status=None, conflicts=None, missing: dict | None = None, verify: bool = True,
           appdata=None, checking: bool = False) -> Facts:
    """Facts of a manager.ModManager (GUI thread; cheap: no folder walks, unless ``verify``: the deploy
    check stats every file the manager placed, which may take a while - the GUI runs it on another
    thread and sets Facts.verify). Looks at the game version again (Steam may have updated the game
    while the manager was open). ``refw_status``: the refw.Status the GUI has (None: not known);
    ``checking``: the mods' own material check is running."""
    game = manager.game_dir
    version = manager.game_version(refresh=True)
    deployed, backups = {}, {}
    if game:
        manifest = manager.deployer().load_manifest()
        deployed, backups = manifest["files"], manifest["backups"]
    problems = verify_files(manager) if verify else []
    from . import matfix

    return Facts(game, mod_states(manager, conflicts, missing), deployed, backups, problems, version, refw_status,
                 appdata, matfix.available(), checking)


def verify_files(manager) -> list[str]:
    """manager.verify(), never raising (any thread: reads the deploy manifest and stats the files)."""
    if not manager.game_dir or not Path(manager.game_dir).is_dir():
        return []
    try:
        return manager.verify()
    except Exception as exc:  # only a part of the check
        log.warning("적용 상태를 점검하지 못했습니다: %s", exc)
        return []
