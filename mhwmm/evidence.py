"""What is known before 원인 모드 찾기 launches the game: how suspect each mod is, and earlier runs.

:func:`build` turns the manager's records into an :class:`Evidence` for the search (bisect): a weight
per mod (a product of a few multipliers with plain reasons), a share of the "one mod" probability for
the three strongest pointers (a crash inside a mod's own plugin or a file it placed, a script that was
loading when the game stopped), pairs of mods that may crash only together, how likely "not the mods"
is, and the earlier runs as observations - the crash record itself (which mods were on, how long the
game ran, which content mods' files the game never asked for) and the runs in ``launches.json``.

Every number here is an educated guess. The search never names a mod on them alone: its stop rule
needs launches that show the mod crashing and the game fine without it, so a misleading record only
costs launches. Each source is read on its own: one that fails is logged and left out.
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Iterable

from . import accessed, crashes, crashlog, gamewatch, precheck
from .gamewatch import options_digest

log = logging.getLogger(__name__)

SHARE = 0.5  # a strong pointer's share of the one-mod probability
SHARE_MAX = 0.85  # all shares together (the rest stays spread by the weights)
WEIGHT_MIN, WEIGHT_MAX = 0.05, 50.0
REASONS_MAX = 3
RECORD_DAYS = 3  # without a record chosen: the newest crash this recent (and of this game version)
HISTORY_ROWS = 20
WHEN_DAYS = 14  # crashes this recent say when the game crashes
START_SECONDS = 150.0  # every crash this soon after the start: at start-up or loading
RHO_NORMAL = 0.6  # an earlier normal run did what triggers the crash (soft: a crash report may end with code 0)
E_FATAL = 0.005  # a REFramework fatal exception is a crash for sure
E_DEFAULT = 0.02
# an older crash in launches.json kept for looking like the record's (code and category): one at the same place
# (module and offset) is likely the same crash, one known only that far may well be another (rows are crashes by
# selection, not answers: the search never names a mod on them, see bisect_engine)
E_HISTORY_EXACT = 0.05
E_HISTORY_LOOSE = 0.25
EQUIP_BOOST = 10.0  # two mods that change the same equipment: "only together" is likelier
EQUIP_MAX_MODS = 8  # an item more mods change says little (a body base, shared textures)
PLUGIN_PAIR = 3.0
SCRIPT_PAIR = 2.0
MAX_PAIRS = 2000
RECENT_HOURS = 24
RECENT_DAYS = 7
CONTENT = frozenset({"loose", "pak"})
CRASH_PLACES = ("plugin", "reframework", "game", "gpu", "root_dll", "system", "other", "unknown", "named",
                "corrupted_pak")
# graphics device errors the game's own crash report names (crashlog.GameReport.device_error) that point at the
# graphics driver or settings more than at mods (the graphics card stopped answering)
DRIVER_ERRORS = ("DEVICE_HUNG", "DEVICE_RESET", "DRIVER_INTERNAL_ERROR")


@dataclass
class Evidence:
    """What the search starts from (bisect_engine reads these attributes; ``Evidence()`` means no evidence)."""
    weights: dict[str, float] = field(default_factory=dict)  # mod id -> multiplier (product of factors)
    shares: dict[str, float] = field(default_factory=dict)  # mod id -> share of the one-mod mass from strong pointers
    pairs: list[tuple[str, str, float]] = field(default_factory=list)  # (a, b, boost) for "only together"
    none_scale: float = 1.0  # "not the mods" prior multiplier
    or_scale: float = 1.0  # "two broken mods" family multiplier (2 after a game update)
    passive: list[dict] = field(default_factory=list)  # earlier runs as observations (see _item)
    crash_times: list[float] = field(default_factory=list)  # seconds from the game's start to a crash
    reasons: dict[str, list[str]] = field(default_factory=dict)  # mod id -> 근거 lines, strongest first, at most 3
    none_reasons: list[str] = field(default_factory=list)  # lines for "모드 문제가 아님"
    when_hint: str = ""  # "" | "start" | "same" | "time" | "rare"
    minutes_hint: float | None = None  # typical minutes until the crash (records)
    equipment: list[tuple[str, str, list[str]]] = field(default_factory=list)  # (key, label, mod ids) for the picker
    record: str = ""  # crashes.Session id used ("" none)
    record_title: str = ""  # crashes.Session.title() of it
    notes: list[str] = field(default_factory=list)  # what was used, for report.txt
    device_error: str = ""  # the record's graphics device error (the game's own crash report: "DEVICE_HUNG" ...)

    @property
    def informative(self) -> bool:
        """Something points at particular mods or runs (a reason, a share, an earlier run, a hint that it is not
        the mods); False when only the kind of each mod (a plugin, textures...) set the weights."""
        return bool(self.reasons or self.shares or self.passive or self.none_reasons)


@dataclass
class ModInfo:
    """One installed mod as the evidence needs it (taken on the GUI thread)."""
    id: str
    name: str
    enabled: bool
    stamp: str  # f"{installed_at}|{updated_at}", as in the crash records' context
    digest: str  # options_digest of the chosen options
    kinds: frozenset  # analyzer.classify of the files it applies
    files: tuple[str, ...]  # game paths it applies (manager.mod_files)
    refw: bool  # the mod that provides REFramework
    changed_at: float | None  # its last install or update (epoch)
    updated: bool = False  # that was an update (not the first install)


@dataclass
class Inputs:
    """The manager's state for :func:`build` (plain data: gathered on the GUI thread, used on any other)."""
    crash_dir: Path
    game_dir: Path | None
    data_dir: Path
    game_version: str
    record_id: str  # the crash record the user chose ("" for the newest recent one)
    mods: list[ModInfo]  # in load order
    index: crashes.ModIndex  # manager.crash_index()
    owners: dict[str, list[str]]  # manager.conflicts().owners: file key -> mods having it, the winner last
    precheck: precheck.Facts | None  # for "REFramework older than the game"
    last_good: dict | None  # gamewatch.load_last_good()
    appdata: str | None = None


def _epoch_iso(text: str) -> float | None:
    try:
        return datetime.fromisoformat(text).timestamp() if text else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _when(epoch: float, now: float) -> str:
    t = datetime.fromtimestamp(epoch)
    return t.strftime("%H:%M") if t.date() == datetime.fromtimestamp(now).date() else f"{t.month}월 {t.day}일 {t:%H:%M}"


def gather(manager, record_id: str = "", owners: dict | None = None) -> Inputs:
    """Inputs of a manager.ModManager (GUI thread; what its lists compute anyway). ``owners``:
    manager.conflicts().owners when the caller has it."""
    conflicts = None
    if owners is None:
        conflicts = manager.conflicts()
        owners = conflicts.owners
    refw = manager.refw_mod()
    mods = []
    for m in manager.mods:
        installed, updated = _epoch_iso(m.installed_at), _epoch_iso(m.updated_at)
        changed = max((t for t in (installed, updated) if t is not None), default=None)
        mods.append(ModInfo(m.id, m.name, m.enabled, f"{m.installed_at}|{m.updated_at}",
                            options_digest(o["name"] for o in m.options if o["id"] in m.selected),
                            frozenset(manager.mod_kinds(m.id)), tuple(manager.mod_files(m.id)),
                            refw is not None and refw.id == m.id, changed,
                            updated is not None and (installed is None or updated > installed)))
    facts = None
    try:
        facts = precheck.gather(manager, conflicts=conflicts, verify=False)
    except Exception as exc:  # only one source of the evidence
        log.warning("실행 전 점검 정보를 읽지 못했습니다: %s", exc)
    try:
        last_good = gamewatch.load_last_good(manager.crash_dir)
    except Exception as exc:
        log.warning("정상 종료 기록을 읽지 못했습니다: %s", exc)
        last_good = None
    version = facts.game_version if facts is not None else manager.game_version()
    return Inputs(manager.crash_dir, manager.game_dir, manager.data_dir, version, record_id, mods,
                  manager.crash_index(), {k: list(v) for k, v in owners.items()}, facts, last_good)


def equipment_choices(manager, ids: Iterable[str]) -> list[tuple[str, str, list[str]]]:
    """(key, label, mod ids) of the equipment the mods ``ids`` change, for "특정 장비를 입거나 볼 때": from the
    equipment window's kept results only (its scans and the game's equipment names), [] when there are none.
    Thread-safe; never raises."""
    try:
        from .equip import overview as equip_overview

        wanted = set(ids)
        catalog = manager.equipment_catalog(build=False)
        if catalog is None or not wanted:
            return []
        lock = manager._equip_lock  # (the kept scans only: scanning the mods again takes a while)
        if not lock.acquire(timeout=10):
            return []
        try:
            kept = dict(manager._load_equip_scans())
        finally:
            lock.release()
        rows = []
        for order, m in enumerate(list(manager.mods)):
            cached = kept.get(m.id) if m.id in wanted else None
            if cached is not None and cached[0] == manager.equipment_job(m.id).stamp:
                rows.append((m.id, m.name, m.enabled, order, cached[1]))
        out = []
        for item in equip_overview.build(rows, catalog).items if rows else ():
            mods = [u.mod_id for u in item.uses if u.mod_id in wanted]
            if mods:
                label = item.name + (f" ({item.subtitle})" if item.subtitle else "")
                out.append((f"{item.target.kind}:{item.target.key}", label, mods))
        return out
    except Exception as exc:  # only a help for the start window
        log.warning("모드가 바꾸는 장비 목록을 만들지 못했습니다: %s", exc)
        return []


def _is_plugin(info: ModInfo) -> bool:
    return any(f.replace("\\", "/").lower().startswith("reframework/plugins/") and f.lower().endswith(".dll")
               for f in info.files)


def _is_script(info: ModInfo) -> bool:
    return any(f.replace("\\", "/").lower().startswith("reframework/") and f.lower().endswith(".lua")
               for f in info.files)


def _types(info: ModInfo) -> set[str]:
    """File types of a mod's loose files (tex, mesh, user...)."""
    out = set()
    for f in info.files:
        if f.replace("\\", "/").lower().startswith("natives/"):
            name = crashlog.resource_key(f).rsplit("/", 1)[-1]
            out.add(name.rsplit(".", 1)[-1] if "." in name else "")
    return out


def _faulty_name(evidence: list[str]) -> str:
    """The file name in a faulty finding's evidence (``"Invalid file" "<path>"`` lines), with "등" for more."""
    if not evidence:
        return ""
    path = evidence[0].rstrip().rstrip('"').rsplit('"', 1)[-1]
    return path.replace("\\", "/").rsplit("/", 1)[-1] + (" 등" if len(evidence) > 1 else "")


def _quoted(text: str) -> str:
    """The first quoted name in a finding's text ("" for none)."""
    start = text.find("'")
    end = text.find("'", start + 1) if start >= 0 else -1
    return text[start + 1:end] if end > start else ""


class _Builder:
    def __init__(self, inputs: Inputs, equipment, now: float | None):
        self.inputs = inputs
        self.now = time.time() if now is None else now
        self.ev = Evidence()
        self.ev.equipment = [(str(k), str(label), [str(i) for i in mods]) for k, label, mods in equipment or ()]
        self.mods = {m.id: m for m in inputs.mods}
        self.on = [m.id for m in inputs.mods if m.enabled and m.files]  # what the search may suspect
        self.on_set = set(self.on)
        self.refw = next((m.id for m in inputs.mods if m.refw and m.id in self.on_set), "")
        self.factors: dict[str, dict[str, float]] = {}  # mod -> row -> factor (each row once per mod)
        self.share_parts: dict[str, dict[str, float]] = {}
        self.reason_items: dict[str, list[tuple[float, int, str]]] = {}
        self.none_factors: dict[str, float] = {}
        self.session: crashes.Session | None = None
        self.explanation: crashes.Explanation | None = None
        self.exposure: accessed.Exposure | None = None
        self.signature: tuple[str, str] | None = None  # (code, category) of the record's crash
        self.where = ""  # crashes.crash_where of the record
        self.crash_at: float | None = None
        self.refw_old = False
        self.history_crashes: list[dict] = []

    # ---- recording the effects
    def _reason(self, mod: str, strength: float, text: str) -> None:
        items = self.reason_items.setdefault(mod, [])
        if text and all(t != text for _s, _n, t in items):
            items.append((strength, len(items), text))

    def weight(self, mod: str, row: str, factor: float, text: str = "") -> None:
        if mod not in self.on_set:
            return
        rows = self.factors.setdefault(mod, {})
        if row not in rows:
            rows[row] = factor
        if text:
            self._reason(mod, abs(math.log(factor)), text)

    def share(self, mod: str, row: str, text: str) -> None:
        if mod not in self.on_set:
            return
        self.share_parts.setdefault(mod, {}).setdefault(row, SHARE)
        self._reason(mod, 10.0 + SHARE, text)

    def none(self, row: str, factor: float, text: str) -> None:
        self.none_factors.setdefault(row, factor)
        if text and text not in self.ev.none_reasons:
            self.ev.none_reasons.append(text)

    def changed(self, then: dict[str, tuple[str, str | None]], ids: Iterable[str]) -> list[str]:
        """Of ``ids`` (on in an earlier run), the mods installed again or with other options since: what ran then
        is not what would run now."""
        out = []
        for i in ids:
            info = self.mods.get(i)
            if info is None or i not in then:
                continue
            stamp, digest = then[i]
            if stamp != info.stamp or (digest is not None and digest != info.digest):
                out.append(i)
        return out

    # ---- the sources
    def run(self) -> None:
        for step in (self.record, self.report, self.findings, self.access, self.materials, self.kinds, self.recent,
                     self.refw_age, self.pairs, self.scales, self.history, self.timing):
            try:
                step()
            except Exception as exc:  # one source that fails must not stop the others (nor the search)
                log.warning("원인 찾기 근거 일부를 읽지 못했습니다 (%s): %s", step.__name__, exc)

    def record(self) -> None:
        """The crash record: the one chosen, else the newest recent crash of this game version."""
        inputs = self.inputs
        if inputs.record_id:
            s = crashes.find_session(inputs.crash_dir, inputs.record_id)
        else:
            s = crashes.last_fatal(inputs.crash_dir)
            if s is not None:
                ended = max(s.end_time, float(s.summary.get("crash_time") or 0))
                version = s.context.get("game_version")
                if self.now - ended > RECORD_DAYS * 86400 or (
                        isinstance(version, str) and version and inputs.game_version and version != inputs.game_version):
                    s = None
        if s is None:
            return
        self.session = s
        self.ev.record, self.ev.record_title = s.id, s.title(self.now)
        self.ev.device_error = str(s.summary.get("device_error") or "")
        t = s.summary.get("crash_time")
        self.crash_at = float(t) if isinstance(t, (int, float)) and t else None
        self.explanation = crashes.explain(s, inputs.index, now=self.now)
        self.signature = (crashlog.normalize_code(s.summary.get("crash") or ""),
                          self.explanation.category or str(s.summary.get("category") or ""))
        self.where = crashes.crash_where(s)

    def report(self) -> None:
        """The crash record as an observation: the mods on then crashed (after how long, with which content
        mods' files never asked for)."""
        s = self.session
        if s is None:
            return
        mods = s.context.get("mods")
        if not isinstance(mods, list):
            self.ev.notes.append(f"튕김 기록 {self.ev.record}: 켜져 있던 모드를 몰라 실행 결과로는 쓰지 않음")
            return
        then = {m["id"]: (str(m.get("stamp") or ""), options_digest(m["options"])
                          if isinstance(m.get("options"), list) else None)
                for m in mods if isinstance(m, dict) and isinstance(m.get("id"), str)}
        on = [i for i in accessed.session_on(s) or [] if i in then]
        unknown = self.changed(then, on)
        seconds = None
        if self.crash_at is not None:
            began = s.time if s.started else s.launched
            if began and self.crash_at > began:
                seconds = self.crash_at - began
        content = [m.id for m in self.inputs.mods if m.kinds and m.kinds <= CONTENT]
        self.exposure = accessed.session_exposure(s, self.inputs.index, content, on,
                                                  names=accessed.PakNames(Path(self.inputs.data_dir) / "cache"))
        unexposed = list(self.exposure.unexposed) if self.exposure is not None and self.exposure.log else []
        label = f"튕김 기록 {_when(self.crash_at or s.end_time, self.now)}"
        gone = [i for i in on if i not in self.on_set]
        if gone:  # (it may have been that mod: as a launch the record would blame the mods on now)
            names = {m["id"]: str(m.get("name") or m["id"]) for m in mods if isinstance(m, dict) and isinstance(m.get("id"), str)}
            shown = ", ".join(f"'{self.mods[i].name if i in self.mods else names.get(i, i)}'" for i in gone[:3])
            self.ev.notes.append(f"{label}: 그때 켜져 있던 모드 {shown}{' 등' if len(gone) > 3 else ''}이(가) 지금은 꺼져 있거나 "
                                 "없어 실행 결과로는 쓰지 않음")
            return
        self.ev.passive.append(_item("report", on, True, 1.0, seconds, unknown, unexposed,
                                     E_FATAL if s.crash is not None else E_DEFAULT, label))
        if seconds is not None:
            self.ev.crash_times.append(seconds)
        note = label
        if self.exposure is not None and self.exposure.log:
            note += f" · 파일 기록 사용(모드 파일 {self.exposure.files}개)"
        elif self.exposure is not None and self.exposure.note:
            note += f" · 파일 기록: {self.exposure.note}"
        self.ev.notes.append(note)

    def findings(self) -> None:
        """What the record points at (crashes.explain's findings and where it crashed)."""
        e = self.explanation
        if e is None:
            return
        for f in e.findings:
            mods = [m for m in dict.fromkeys(f.mods) if m]
            if f.kind == "plugin" and len(mods) == 1:
                where = next((x.split(": ", 1)[1] for x in f.evidence if x.startswith("오류 위치: ")), "") \
                    or _quoted(f.text)
                self.share(mods[0], "S1", f"튕긴 곳이 이 모드의 플러그인입니다 ({where})" if where
                           else "튕긴 곳이 이 모드의 플러그인입니다")
            elif f.kind in CRASH_PLACES and f.level == "high" and len(mods) == 1:
                self.share(mods[0], "S2", f"튕긴 곳이 이 모드가 넣은 파일 '{_quoted(f.text)}'입니다")
            elif f.kind == "script" and mods:
                text = f"이 모드의 스크립트 '{_quoted(f.text)}'를 읽다가 멈췄습니다"
                for x in mods:
                    if f.level == "high" and len(mods) == 1:
                        self.share(x, "S3", text)
                    else:
                        self.weight(x, "S3b", 3.0, text + " (확실하지 않음)")
            elif f.kind == "faulty":
                name = _faulty_name(f.evidence)
                for x in mods:
                    if f.level == "high":
                        self.weight(x, "W1", 6.0 if f.action == "matfix" else 5.0,
                                    f"REFramework가 이 모드의 파일 {name}을(를) 읽지 못했습니다")
                    elif f.level == "low":
                        self.weight(x, "W2", 1.5, f"REFramework가 이 모드의 파일 {name}을(를) 읽지 못한 파일로 기록했지만 "
                                    "재질 점검에서는 맞는 파일입니다")
            elif f.kind == "stack":
                for x in mods:
                    self.weight(x, "W3", 2.0, "튕긴 호출 기록에 이 모드의 플러그인이 있습니다")
            elif f.kind == "lua":
                for x in mods:
                    self.weight(x, "W4", 1.5, "이 모드의 스크립트에서 오류가 기록되었습니다")
            elif f.kind == "plugin_load":
                error = crashlog.plugin_error(f.evidence[0]) if f.evidence else None
                reason = error.reason if error is not None else ""
                for x in mods:
                    if reason in ("load", "major", "minor", "game"):
                        self.weight(x, "W5", 0.3, "이 모드의 플러그인은 시작하지 못해 실행되지 않았습니다")
                    elif reason in ("init", "exception"):
                        self.weight(x, "W5b", 1.5, "이 모드의 플러그인이 시작하다가 오류를 냈습니다")
            elif f.kind == "pak_failed":
                for x in mods:
                    self.weight(x, "W6", 0.3, "이 모드의 PAK은 게임에 적용되지 않았습니다")
            elif f.kind == "root_dll" and not mods and f.level in ("high", "medium"):
                self.none("W12", 3.0, "튕긴 곳이 매니저가 넣지 않은 파일입니다")
            elif f.kind in ("faulty_loose", "faulty_pak"):
                self.none("W13", 1.5, "매니저가 넣지 않은 파일을 REFramework가 읽지 못했습니다")
            elif f.kind == "init" and self.refw:
                self.weight(self.refw, "W15", 8.0, "REFramework가 이 게임 버전에서 제대로 시작하지 못했습니다")
            elif f.kind == "hooks":
                for x in self.on:
                    if _is_script(self.mods[x]):
                        self.weight(x, "W16", 1.5, "Lua 스크립트가 게임에 없는 함수를 고치려 했습니다")
        category = e.category
        if category == "reframework":
            if self.refw:
                self.weight(self.refw, "W8", 6.0, "튕긴 곳이 REFramework(dinput8.dll) 안입니다")
            for x in self.on:
                if _is_script(self.mods[x]):
                    self.weight(x, "W8", 1.5, "튕긴 곳이 REFramework(dinput8.dll) 안입니다 (Lua 스크립트가 그 안에서 실행됨)")
        elif category == "gpu":
            self.none("W9", 2.0, "그래픽(D3D12) 쪽에서 튕겨 모드 문제가 아닐 수도 있습니다")
            if self.ev.device_error in DRIVER_ERRORS:  # (the game's own report: the graphics card stopped answering)
                self.none("W9b", 2.0, f"그래픽카드가 응답하지 않는 오류({self.ev.device_error})는 대개 그래픽 드라이버·설정 "
                          "문제입니다")
            for x in self.on:
                info = self.mods[x]
                if _types(info) & {"tex", "mesh"}:
                    self.weight(x, "W9", 1.5, "그래픽(D3D12) 쪽에서 튕겼고 이 모드에 텍스처·모델 파일이 있습니다")
                elif _is_plugin(info) or _is_script(info):
                    self.weight(x, "W9", 0.7)
        elif category == "unknown":
            for x in self.on:
                if _is_plugin(self.mods[x]) or _is_script(self.mods[x]):
                    self.weight(x, "W10", 1.5, "튕긴 곳이 어느 파일에도 속하지 않는 코드입니다 (플러그인·스크립트가 바꾼 게임 코드일 수 "
                                "있음)")
        elif category == "system":
            self.none("W11", 1.5, "Windows 시스템 파일에서 튕겼습니다")
        elif category == "other":
            self.none("W12", 3.0, "튕긴 곳이 매니저가 넣지 않은 파일입니다")

    def access(self) -> None:
        """The mods whose files the game asked for in the last seconds before the crash."""
        found = self.exposure
        if found is None or not found.tail:
            return
        if found.exclusive:
            k = found.tail[found.exclusive]
            self.weight(found.exclusive, "S4", 5.0, f"튕기기 직전 {accessed.TAIL_SECONDS:.0f}초 동안 게임이 읽은 모드 파일 {k}개가 "
                        "모두 이 모드의 것입니다")
            return
        k = len(found.tail)
        if k == 1:  # (one file of one mod: the game reads a file of the player's own armor mod now and then)
            (x, files), = found.tail.items()
            self.weight(x, "W14", 1.5, f"튕기기 직전에 게임이 이 모드의 파일 {files}개를 읽었습니다")
            return
        factor = min(4.0, 1.0 + 6.0 / (k + 1))
        for x, files in found.tail.items():
            self.weight(x, "W14", factor, f"튕기기 직전에 게임이 이 모드의 파일 {files}개를 읽었습니다")

    def materials(self) -> None:
        """Materials and data files that do not fit this game version (재질 점검)."""
        flagged = 0
        for ref in self.inputs.index.mods.values():
            if not ref.enabled or ref.id not in self.on_set:
                continue
            if ref.stuck:
                self.weight(ref.id, "W7b", 4.0, f"이 게임 버전에 맞게 고치지 못한 파일이 {len(ref.stuck)}개 있습니다")
            elif ref.material_state == "stale":
                self.weight(ref.id, "W7", 3.0, "이 게임 버전과 맞지 않는 재질·데이터 파일이 있습니다")
            else:
                continue
            flagged += 1
        if flagged:
            self.ev.notes.append(f"재질 점검: 이 게임 버전과 맞지 않는 파일이 있는 모드 {flagged}개")

    def kinds(self) -> None:
        """What kind of mod it is (plugins crash the game more often than textures)."""
        for x in self.on:
            info = self.mods[x]
            types = _types(info)
            if _is_plugin(info):
                self.weight(x, "W17", 2.0)
            elif _is_script(info):
                self.weight(x, "W17", 1.3)
            elif types & {"user", "pfb", "scn"}:
                self.weight(x, "W17", 1.5)
            elif types == {"tex"} and info.kinds == {"loose"}:
                self.weight(x, "W17", 0.6)

    def recent(self) -> None:
        """Mods installed or updated shortly before the crash (only without a last normal exit: that one says
        exactly what changed)."""
        if self.inputs.last_good is not None:
            return
        at = self.crash_at if self.crash_at is not None else self.now
        count = 0
        for x in self.on:
            info = self.mods[x]
            if info.changed_at is None or not 0 <= at - info.changed_at <= RECENT_DAYS * 86400:
                continue
            hours = (at - info.changed_at) / 3600
            ago = (f"{max(1, round(hours * 60))}분" if hours < 1 else f"{round(hours)}시간" if hours < 48
                   else f"{round(hours / 24)}일")
            before = f"튕기기 {ago} 전에" if self.crash_at is not None else f"{ago} 전에"
            text = before + (" 업데이트했습니다" if info.updated else " 설치했습니다")
            self.weight(x, "W18", 2.0 if hours <= RECENT_HOURS else 1.3, text)
            count += 1
        if count:
            self.ev.notes.append(f"최근 설치·업데이트한 모드 {count}개")

    def refw_age(self) -> None:
        """REFramework older than the game (실행 전 점검's refw_old)."""
        facts = self.inputs.precheck
        if facts is None or not facts.game_dir:
            return
        issues = precheck.refw_issues(facts, precheck.users(facts), precheck.refw_present(facts))
        if any(i.kind == "refw_old" for i in issues):
            self.refw_old = True
            if self.refw:
                self.weight(self.refw, "W15", 8.0, "게임 업데이트 뒤로 REFramework가 업데이트되지 않았습니다")
            self.ev.notes.append("게임 업데이트 뒤로 REFramework가 업데이트되지 않았습니다")

    def pairs(self) -> None:
        """Two mods that may crash only together: the same equipment, two plugins, two scripts."""
        masked = set()
        for ids in self.inputs.owners.values():
            for loser in ids[:-1]:
                if loser != ids[-1]:
                    masked.add(frozenset((loser, ids[-1])))
        rank = {m: n for n, m in enumerate(self.on)}
        found: dict[tuple[str, str], float] = {}

        def add(ids: Iterable[str], boost: float) -> bool:
            for a, b in combinations(sorted(set(ids) & self.on_set, key=rank.get), 2):
                if frozenset((a, b)) in masked:
                    continue  # (one's files cover the other's: "both on" is the winner's setup)
                if (a, b) not in found and len(found) >= MAX_PAIRS:
                    return False
                found[(a, b)] = max(found.get((a, b), 0.0), boost)
            return True

        for _key, _label, ids in self.ev.equipment:
            if 2 <= len(set(ids) & self.on_set) <= EQUIP_MAX_MODS and not add(ids, EQUIP_BOOST):
                break
        add([x for x in self.on if _is_plugin(self.mods[x])], PLUGIN_PAIR)
        add([x for x in self.on if _is_script(self.mods[x])], SCRIPT_PAIR)
        self.ev.pairs = [(a, b, boost) for (a, b), boost in found.items()]

    def scales(self) -> None:
        good = self.inputs.last_good
        context = good.get("context") if isinstance(good, dict) else None
        then = context.get("game_version") if isinstance(context, dict) else ""
        if (isinstance(then, str) and then and self.inputs.game_version and then != self.inputs.game_version) \
                or self.refw_old:
            self.ev.or_scale = 2.0

    def history(self) -> None:
        """Earlier runs of this game version (launches.json): normal exits, and crashes like the record's."""
        inputs = self.inputs
        rows = gamewatch.load_launches(inputs.crash_dir, inputs.game_version, self.now)
        good = inputs.last_good.get("time") if isinstance(inputs.last_good, dict) else None
        used = crashed = 0
        for r in rows:
            if used >= HISTORY_ROWS:
                break
            if r["bisect"] or r["outcome"] == "unknown" or (self.ev.record and r["record"] == self.ev.record):
                continue
            if isinstance(good, (int, float)) and abs(r["time"] - good) < 2:
                continue  # (the search takes the last normal exit on its own)
            then = {i: (stamp, digest) for i, stamp, digest in r["mods"]}
            ok = r["outcome"] == "normal"
            strength = "" if ok else self._same_crash(r)
            if strength is None:
                continue
            if not ok and any(i not in self.on_set for i in then):
                continue  # (a mod on then is gone or off now: that crash may have been its)
            on = list(then)
            minutes = f" {round(r['seconds'] / 60)}분" if r["seconds"] else ""
            label = f"실행 기록 {_when(r['time'], self.now)} ({'정상 종료' if ok else '튕김'}{minutes})"
            e = None if ok else E_HISTORY_EXACT if strength == "exact" else E_HISTORY_LOOSE
            self.ev.passive.append(_item("history", on, not ok, RHO_NORMAL if ok else 1.0, r["seconds"],
                                         self.changed(then, on), [], e, label))
            if not ok:
                crashed += 1
                if strength == "exact":  # (only a crash at the same place says when the game crashes)
                    self.history_crashes.append(r)
                    if r["seconds"]:
                        self.ev.crash_times.append(r["seconds"])
            used += 1
        if used:
            self.ev.notes.append(f"이전 실행 기록 {used}개" + (f" (그중 같은 방식으로 튕김 {crashed}번)" if crashed else ""))

    def _same_crash(self, row: dict) -> str | None:
        """How much an older crash looks like the record's: "exact" (the same code, category and place - module
        and offset), "loose" (the same code and category, the place not known on one side), None (another
        crash: a different code, category, module or offset)."""
        if self.signature is None:
            return None
        code, category = self.signature
        if crashlog.normalize_code(row["code"]) != code or (category and row["category"] and row["category"] != category):
            return None
        mine, theirs = self.where, str(row.get("where") or "")
        if not mine or not theirs:
            return "loose"
        if mine.split("+", 1)[0] != theirs.split("+", 1)[0]:
            return None  # (another module)
        if "+" in mine and "+" in theirs:
            return "exact" if mine == theirs else None
        return "loose"

    def timing(self) -> None:
        """When the game crashes: at start-up, or after a while (the report and recent crashes like it)."""
        times = []
        report = next((p for p in self.ev.passive if p["source"] == "report"), None)
        if report is not None and report["seconds"] and self.crash_at is not None \
                and self.now - self.crash_at <= WHEN_DAYS * 86400:
            times.append(report["seconds"])
        times += [r["seconds"] for r in self.history_crashes
                  if r["seconds"] and self.now - r["time"] <= WHEN_DAYS * 86400]
        if not times:
            return
        if all(t <= START_SECONDS for t in times):
            self.ev.when_hint = "start"
        else:
            self.ev.when_hint = "time"
            self.ev.minutes_hint = round(statistics.median(times) / 60, 1)

    # ---- the result
    def result(self) -> Evidence:
        ev = self.ev
        for mod in self.on:
            rows = self.factors.get(mod)
            if rows:
                w = min(max(math.prod(rows.values()), WEIGHT_MIN), WEIGHT_MAX)
                if w != 1.0:
                    ev.weights[mod] = w
            parts = self.share_parts.get(mod)
            if parts:
                ev.shares[mod] = 1.0 - math.prod(1.0 - s for s in parts.values())
            items = self.reason_items.get(mod)
            if items:
                ev.reasons[mod] = [t for _s, _n, t in sorted(items, key=lambda x: (-x[0], x[1]))][:REASONS_MAX]
        total = sum(ev.shares.values())
        if total > SHARE_MAX:
            ev.shares = {k: v * SHARE_MAX / total for k, v in ev.shares.items()}
        ev.none_scale = math.prod(self.none_factors.values()) if self.none_factors else 1.0
        return ev


def _item(source: str, on: Iterable[str], crashed: bool, rho: float, seconds, unknown: Iterable[str],
          unexposed: Iterable[str], e: float | None, label: str) -> dict:
    """One earlier run as an observation (Evidence.passive; JSON-able)."""
    return {"source": source, "on": list(on), "crashed": bool(crashed), "rho": float(rho),
            "seconds": float(seconds) if isinstance(seconds, (int, float)) and seconds > 0 else None,
            "unknown": list(unknown), "unexposed": list(unexposed), "e": e, "label": label}


def build(inputs: Inputs, equipment=None, now: float | None = None) -> Evidence:
    """The Evidence of ``inputs`` (``equipment``: equipment_choices for the picker and the same-equipment pairs).
    Any thread (reads the crash record, its copy of the files the game read and pak tables); never raises."""
    try:
        builder = _Builder(inputs, equipment, now)
        builder.run()
        return builder.result()
    except Exception as exc:
        log.warning("원인 찾기 근거를 만들지 못했습니다: %s", exc)
        return Evidence()
