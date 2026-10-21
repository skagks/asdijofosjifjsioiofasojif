"""원인 모드 찾기: finding the mod that makes the game crash by running it with some mods off.

The user's profile is never edited: the search works in a temporary profile (a copy of the
current one) and each step only turns mods of that copy on or off; the caller applies it and the
user plays until the crash would come, then answers.  At the end the original profile comes
back (manager.switch_profile saves the temporary state into the temporary profile, which is then
deleted).  The search is kept in ``<data>/bisect.json`` and goes on after a restart.

Units are "a mod plus the mods it requires": REFramework and every mod another enabled mod needs
(Nexus requirements, Lua libraries) stay on, so a step never breaks a mod by taking its library
away.  Mods that need an option first (nothing to apply) and files the manager did not place are
left out and listed.

The search runs on prefixes of the suspects in a fixed order (unchanged since the last normal
exit first, then the changed ones, so testing the last good set is the first launch):

1. find the smallest prefix that crashes (a binary search, ceil(log2(n+1)) launches); the unit
   that ends it is needed for the crash;
2. with it on, look whether it crashes alone (one launch) - else find the next needed unit among
   the units before it the same way: two mods that crash only together are found as a pair;
3. when even REFramework and the needed mods alone crash, try with everything off: still a crash
   means it is probably not a mod; else the same search runs inside that group.

A result rests on crashes seen in this search: when only the user's report (everything on
crashes) supports it, everything is tried again, and before looking for a second mod the crash
is tried once more (a crash that came once may not come back).  No result rests on one answer,
so one wrong answer (a crash that took longer than that play, a mis-click) cannot name a mod
that is not the cause: the set it crashed with and a set without it that ran fine are each seen
twice (once more when needed), and so is "nothing on still crashes".  Answers that contradict
each other (the same mods crashed once and not again, or more mods ran fine where fewer had
crashed) end the search as "intermittent".  The search is a pure function of the answers so far
(:func:`next_step`), so resuming only needs them.
"""

from __future__ import annotations

import copy
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import fsutil, requirements
from .constants import PAK_MODS_DIR, REFRAMEWORK_DLL

log = logging.getLogger(__name__)

SESSION_FILE = "bisect.json"  # in the data folder
SESSION_FORMAT = 1
TEMP_PROFILE = "원인 찾기 (임시)"
MAX_UNMANAGED_SCAN = 20000  # entries looked at when listing files the manager did not place
MAX_UNMANAGED_LIST = 200
ESTIMATE_SAMPLES = 40  # culprit positions simulated for the "about N launches" estimate

TITLE = "원인 모드 찾기"  # the window and the ☰ menu item
INTRO = ("켜져 있는 모드 {count}개 중 게임을 튕기게 하는 모드를 찾습니다. 절반씩 끄고 실행해 보는 방식이라 {runs} "
         "실행하면 됩니다. 지금 프로필은 그대로 두고 임시 프로필을 씁니다. REFramework와 다른 모드에 필요한 모드는 계속 "
         "켜 둡니다. 테스트 중에는 가능하면 게임을 저장하지 마세요.")
ASK = "또 튕겼나요?"
ANSWER_CRASHED = "튕겼어요"
ANSWER_FINE = "괜찮았어요"
ANSWER_RETRY = "잘 모르겠어요 (이 단계 다시)"
BUTTON_LAUNCH = "게임 실행"
BUTTON_STOP = "그만두기"
RESULT_DISABLE = "이 모드를 끄고 원래 프로필로"
RESULT_DISABLE_MANY = "이 모드들을 끄고 원래 프로필로"
RESULT_RESTORE = "원래 프로필로"
RESULT_REPORT = "진단 파일 저장"
BLOCKED = "원인 모드 찾기 중에는 할 수 없습니다. 원인 찾기를 마치거나 그만둔 뒤 다시 하세요."
PLAY_AGAIN = "튕기던 상황을 다시 해 보세요."
OUTDATED_PAKS = ("예전 방식(패치 번호)으로 넣은 PAK이 게임 폴더에 남아 있습니다. 먼저 '게임에 적용'으로 pak_mods 폴더로 "
                 "옮긴 뒤 다시 시작하세요.")

KINDS = ("half", "base", "last_good", "confirm", "partner", "again", "full", "everything_off", "core")
RESULTS = ("found", "together", "core", "not_mods", "intermittent", "no_repro", "aborted")


class BisectError(Exception):
    pass


# ---- the search -------------------------------------------------------------------------------
@dataclass
class Step:
    """One launch of the search: the session's mods in ``on`` on, the rest of them off."""
    number: int  # 1 for the first launch
    total: int  # about how many launches in all (it may still change)
    kind: str  # see KINDS
    on: list[str]  # ids, in load order
    off: list[str]
    found: list[str] = field(default_factory=list)  # mods already shown to be needed for the crash
    phase: str = "main"  # "core": inside REFramework and the mods others need


@dataclass
class Result:
    kind: str  # see RESULTS
    mods: list[str] = field(default_factory=list)  # ids: the cause, in load order
    runs: int = 0  # launches answered
    reason: str = ""  # why it was aborted

    @property
    def can_disable(self) -> bool:
        """The result names mods the user may turn off in the original profile."""
        return self.kind in ("found", "together", "core") and bool(self.mods)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "mods": list(self.mods), "runs": self.runs, "reason": self.reason}

    @classmethod
    def from_dict(cls, data) -> Result | None:
        if not isinstance(data, dict) or data.get("kind") not in RESULTS:
            return None
        mods = [m for m in data.get("mods") or [] if isinstance(m, str)]
        runs = data.get("runs") if isinstance(data.get("runs"), int) else 0
        return cls(data["kind"], mods, runs, str(data.get("reason") or ""))


class _Need(Exception):
    def __init__(self, on: frozenset, kind: str, left: int):
        super().__init__(kind)
        self.on, self.kind, self.left = on, kind, left


class _Done(Exception):
    def __init__(self, result: Result):
        super().__init__(result.kind)
        self.result = result


class _Search:
    """One replay of the search over the answers so far (``seen``: effective set -> what each launch
    with it gave). ``oracle`` answers sets not seen yet (for estimates and tests); without one they
    raise _Need."""

    def __init__(self, units, base, core, requires, split, seen, oracle=None, order=None):
        self.units, self.base, self.core = list(units), list(base), list(core)
        ids = set(self.units) | set(self.base) | set(self.core)
        self.requires = {k: [r for r in v if r in ids and r != k] for k, v in (requires or {}).items() if k in ids}
        self.split = split if isinstance(split, int) and 0 < split < len(self.units) else None
        self.seen: dict[frozenset, list[bool]] = {k: list(v) for k, v in dict(seen).items()}
        self.oracle = oracle
        self.launches = 0  # answered by the oracle
        self.full = self.eff(self.base + self.units)
        self.base_set = self.eff(self.base)
        self.found: list[str] = []
        self.phase = "main"
        self.position = {i: n for n, i in enumerate(order or [*self.base, *self.units])}

    def eff(self, ids: Iterable[str]) -> frozenset:
        """The mods on when ``ids`` are: with everything they require."""
        out: set[str] = set()
        todo = list(ids)
        while todo:
            i = todo.pop()
            if i not in out:
                out.add(i)
                todo.extend(self.requires.get(i, ()))
        return frozenset(out)

    def crashes(self, ids, kind: str, left: int, times: int = 0) -> bool:
        """Whether the game crashes with ``ids`` on: from the answers, else a launch is needed. The
        set with everything on crashes by the user's report unless ``times`` asks for that many
        launches with it (a report counts as one)."""
        s = self.eff(ids)
        got = self.seen.get(s, [])
        need = max(times - (1 if s == self.full else 0), 1 if times else 0)
        if got and len(got) >= need:
            return got[-1]
        if not got and not need and s == self.full:
            return True  # what the user reported: with everything on it crashes
        if self.oracle is not None:
            self.launches += 1
            self.seen.setdefault(s, []).append(bool(self.oracle(s)))
            self.check()
            return self.seen[s][-1]
        if got and got[-1]:
            kind = "again"  # (a set that ran fine is tried again as the caller says)
        elif not s:
            kind = "everything_off"
        elif s == self.full:
            kind = "full"
        elif s == self.base_set and self.phase == "main":
            kind = "base"
        raise _Need(s, kind, max(0, left))

    def smallest(self, f: Callable[[int], list], lo: int, hi: int, first: int | None, first_kind: str, kind: str,
                 extra: int, first_left: int | None = None, top: int = 1) -> int:
        """The smallest j in [lo, hi] for which f(j) crashes, f(hi) crashing; ``first`` is tried first.
        ``extra``: launches expected after this search; ``first_left``: after the first one (as it is
        expected to go). ``top``: how many launches must have shown f(hi) crash (the user's report
        counts as one), checked after a first one that did not crash, before searching further."""
        if first is not None and lo <= first < hi:
            left = (hi - first - 1).bit_length() + extra if first_left is None else first_left
            if self.crashes(f(first), first_kind, left):
                hi = first
            else:
                lo = first + 1
                self.confirm(f(hi), (hi - lo).bit_length() + extra, top)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.crashes(f(mid), kind, (hi - lo).bit_length() - 1 + extra):
                hi = mid
            else:
                lo = mid + 1
        self.confirm(f(hi), extra, 1 if top else 0)
        return hi

    def confirm(self, ids, left: int, times: int) -> None:
        """``ids`` crashed in ``times`` launches (no crash: nothing crashed at all, or it did not come back)."""
        if times and not self.crashes(ids, "full", left, times):
            self.check()
            raise _Done(Result("no_repro"))

    def minimize(self, order: list[str], base: list[str], k: int) -> list[str]:
        """``order[k-1]`` is needed (``base`` and ``order[:k]`` crash, without it they did not): add
        the units before it that are needed too, until the found ones crash on their own."""
        self.found = [order[k - 1]]
        limit = k - 1
        top = 2  # the first time it does not crash alone, the crash is tried again before looking further
        if self.eff(base + self.found) == self.eff(base + order[:k]):
            # it alone (with what it needs) is the set seen crashing, once so far: once more before blaming it
            self.confirm(base + self.found, 1, 2)
        while True:
            found = list(self.found)
            # alone first (one mod is the usual cause); then one more needed one among those before it
            j = self.smallest(lambda j: base + found + order[:j], 0, limit, 0, "confirm", "partner", 1, 0, top)
            if j == 0:
                if len(self.found) > 1:  # the set they crash with: seen twice (the last partner launch may be all)
                    self.confirm(base + self.found, 1, 2)
                self.needed(base)
                return sorted(self.found, key=lambda i: self.position.get(i, 0))
            self.found.append(order[j - 1])
            limit, top = j - 1, 1

    def needed(self, base: list[str]) -> None:
        """Each found mod is needed for the crash: with the others on and it off, the game ran fine in
        two launches (any with those on counts), so one wrong 괜찮았어요 cannot blame a mod. Else it is
        tried that way once more; a crash then contradicts the answers before."""
        found = list(self.found)
        for n, x in enumerate(found):
            others = base + [i for i in found if i != x]
            rest = self.eff(others) | (self.eff([x]) - {x})  # everything on with it but itself
            if x in rest:  # (needed by another found one: never off without it)
                continue
            others = sorted(rest, key=lambda i: self.position.get(i, 0))
            while sum(got.count(False) for s, got in self.seen.items() if x not in s and rest <= s) < 2:
                if self.crashes(others, "confirm", len(found) - n - 1, len(self.seen.get(rest, [])) + 1):
                    self.check()
                    raise _Done(Result("intermittent"))

    def check(self) -> None:
        """Answers that contradict each other end the search: the same mods gave a crash and none,
        or more mods on ran fine where fewer had crashed."""
        crashed = [s for s, got in self.seen.items() if any(got)]
        fine = [s for s, got in self.seen.items() if not all(got)]
        if any(c <= f for c in crashed for f in fine):
            raise _Done(Result("intermittent"))

    def run(self) -> Result:
        self.check()
        n = len(self.units)
        # (the answer may rest on the user's report alone: the mod found is then tried alone first)
        k = self.smallest(lambda j: self.base + self.units[:j], 0, n, self.split, "last_good", "half", 1, top=0)
        if k > 0:
            found = self.minimize(self.units, self.base, k)
            return Result("found" if len(found) == 1 else "together", found)
        # REFramework and the mods others need crash on their own: everything off, then inside them
        self.phase = "core"
        k = self.smallest(lambda j: self.core[:j], 0, len(self.core), 0, "everything_off", "core", 1)
        if k == 0:
            self.confirm([], 0, 2)  # (the one launch that says so: once more)
            return Result("not_mods")
        return Result("core", self.minimize(self.core, [], k))


def next_step(units: Sequence[str], base: Sequence[str], core: Sequence[str] = (), requires: dict | None = None,
              split: int | None = None, answers: Iterable[tuple[Iterable[str], bool]] = (),
              order: Sequence[str] | None = None, estimate: int = 0) -> Step | Result:
    """The next launch, or the result, of the search after ``answers`` ((ids on, crashed) of the
    launches so far, in order). ``units``: the suspects in search order; ``base``: always on;
    ``core``: ``base`` with requirements first (REFramework first); ``split``: how many of the
    units are unchanged since the last normal exit (tested together first)."""
    seen: dict[frozenset, list[bool]] = {}
    count = 0
    for on, crashed in answers:
        seen.setdefault(frozenset(on), []).append(bool(crashed))
        count += 1
    search = _Search(units, base, core or base, requires, split, seen, order=order)
    try:
        result = search.run()
    except _Need as need:
        number = count + 1
        on = sorted(need.on, key=lambda i: search.position.get(i, 0))
        off = [i for i in sorted(search.full | set(search.core), key=lambda i: search.position.get(i, 0))
               if i not in need.on]
        return Step(number, max(estimate, number + need.left), need.kind, on, off,
                    sorted(search.found, key=lambda i: search.position.get(i, 0)), search.phase)
    except _Done as done:
        result = done.result
    result.runs = count
    return result


def simulate(units, base, core=(), requires=None, split=None, crashes_with: Callable[[frozenset], bool] = None,
             limit: int = 200) -> tuple[Result, list[Step]]:
    """Run the search with answers from ``crashes_with`` (set of ids on -> crashed): the result and
    the launches it took."""
    answers: list[tuple[list[str], bool]] = []
    steps: list[Step] = []
    while len(steps) < limit:
        nxt = next_step(units, base, core, requires, split, answers)
        if isinstance(nxt, Result):
            return nxt, steps
        steps.append(nxt)
        answers.append((nxt.on, bool(crashes_with(frozenset(nxt.on)))))
    raise BisectError("원인 찾기가 끝나지 않습니다.")


def estimate_runs(units, base, core=(), requires=None, split=None, everywhere: bool = False) -> int:
    """About how many launches finding one mod takes (the most over where it may be: among the
    changed ones when ``split`` is given, unless ``everywhere``)."""
    units = list(units)
    if not units:
        return 2 if base else 1
    start = split if isinstance(split, int) and 0 < split < len(units) and not everywhere else 0
    positions = list(range(start, len(units)))
    if len(positions) > ESTIMATE_SAMPLES:
        step = len(positions) / ESTIMATE_SAMPLES
        positions = sorted({positions[int(i * step)] for i in range(ESTIMATE_SAMPLES)} | {positions[-1]})
    most = 0
    for p in positions:
        culprit = units[p]
        search = _Search(units, base, core or base, requires, split, {}, oracle=lambda s, c=culprit: c in s)
        try:
            search.run()
        except _Done:
            pass
        most = max(most, search.launches)
    return most


# ---- what is known of the mods ------------------------------------------------------------------
@dataclass
class ModFacts:
    """What the search needs to know of one installed mod (manager.Mod and its files)."""
    id: str
    name: str
    enabled: bool
    has_files: bool = True  # something to apply (False: it needs an option chosen first, or is empty)
    needs_option: bool = False
    nexus_id: int = 0
    requirements: Sequence[dict] = ()  # Mod.requirements (from its Nexus page)
    lua_requires: Sequence[str] = ()  # Lua modules its scripts need
    provides: frozenset = frozenset()  # Lua modules it provides (requirements.provided_modules)
    needs_refw: bool = False  # loose files, scripts or PAKs: only work with REFramework
    refw: bool = False  # it is REFramework (manager.refw_mod())
    stamp: str = ""  # installed_at|updated_at: changes with every install of another version
    options: Sequence[str] = ()  # names of the chosen options


def requirement_ids(mods: Sequence[ModFacts]) -> dict[str, list[str]]:
    """Mod id -> ids of the enabled mods it needs, matched as manager.requirement_status matches
    them (Nexus requirements by mod number or name, Lua libraries by the mods that provide them,
    REFramework for mods that need it); a requirement no enabled mod meets is left out."""
    refw = next((m for m in mods if m.refw), None)
    out: dict[str, list[str]] = {}

    def best(candidates: list[ModFacts]) -> ModFacts | None:
        return next((m for m in candidates if m.enabled), None)

    for mod in mods:
        others = [m for m in mods if m.id != mod.id]
        need: list[str] = []
        for req in mod.requirements:
            if not isinstance(req, dict):
                continue
            name = str(req.get("name") or "")
            if requirements.is_reframework(name):
                found = refw if refw is not None and refw.id != mod.id and refw.enabled else None
            else:
                try:
                    nexus_id = int(req.get("nexus_id") or 0)
                except (TypeError, ValueError):
                    nexus_id = 0
                matches = [m for m in others if nexus_id and m.nexus_id == nexus_id] or \
                    [m for m in others if requirements.names_match(name, m.name)]
                found = best(matches)
            if found is not None:
                need.append(found.id)
        for module in mod.lua_requires:
            key = requirements.module_key(module)
            found = best([m for m in others if key in m.provides])
            if found is not None:
                need.append(found.id)
        if mod.needs_refw and refw is not None and refw.id != mod.id and refw.enabled:
            need.append(refw.id)
        out[mod.id] = list(dict.fromkeys(need))
    return out


def _closure(ids: Iterable[str], requires: dict[str, list[str]]) -> set[str]:
    out: set[str] = set()
    todo = list(ids)
    while todo:
        i = todo.pop()
        for r in requires.get(i, ()):
            if r not in out:
                out.add(r)
                todo.append(r)
    return out


def dependency_order(ids: Sequence[str], requires: dict[str, list[str]], first: str | None = None) -> list[str]:
    """``ids`` with the mods each one requires before it (else in the given order; ``first`` leads)."""
    ids = list(ids)
    if first in ids:
        ids.remove(first)
        ids.insert(0, first)
    inside = set(ids)
    out: list[str] = []
    placed: set[str] = set()
    while len(out) < len(ids):
        ready = next((i for i in ids if i not in placed
                      and all(r in placed or r not in inside for r in requires.get(i, ()))), None)
        if ready is None:  # a cycle: the rest in the given order
            ready = next(i for i in ids if i not in placed)
        out.append(ready)
        placed.add(ready)
    return out


# ---- what changed since the game last ended normally ---------------------------------------------------
@dataclass
class Changes:
    """The mods on now against those of the last normal exit (gamewatch's last_good.json)."""
    when: float = 0.0  # that exit
    game_updated: bool = False
    added: list[str] = field(default_factory=list)  # ids on now, not then
    updated: list[str] = field(default_factory=list)  # on both times, another install since
    options: list[str] = field(default_factory=list)  # other options chosen
    removed: list[str] = field(default_factory=list)  # names of mods on then, off or gone now

    @property
    def changed(self) -> list[str]:
        return list(dict.fromkeys([*self.added, *self.updated, *self.options]))

    def text(self, names: dict[str, str]) -> str:
        def some(ids: Sequence[str], label: str) -> str:
            shown = ", ".join(names.get(i, i) for i in ids[:3]) + (" 등" if len(ids) > 3 else "")
            return f"{label} {len(ids)}개({shown})"

        parts = ["게임 업데이트"] if self.game_updated else []
        for ids, label in ((self.added, "새로 켠 모드"), (self.updated, "업데이트한 모드"), (self.options, "옵션을 바꾼 모드")):
            if ids:
                parts.append(some(ids, label))
        if self.removed:
            more = " 등" if len(self.removed) > 3 else ""
            parts.append(f"끈 모드 {len(self.removed)}개({', '.join(self.removed[:3])}{more})")
        when = f"마지막 정상 종료({_when(self.when)})" if self.when else "마지막 정상 종료"
        if not parts:
            return f"{when} 이후 바뀐 모드가 없습니다."
        return f"{when} 이후 바뀐 것: " + ", ".join(parts)


def _when(epoch: float) -> str:
    t = datetime.fromtimestamp(epoch)
    return t.strftime("%H:%M") if t.date() == datetime.now().date() else f"{t.month}월 {t.day}일 {t:%H:%M}"


def changes_since(last_good: dict | None, mods: Sequence[ModFacts], game_version: str = "") -> Changes | None:
    """What changed since the last normal exit (None without one). ``last_good``: gamewatch.load_last_good()."""
    if not isinstance(last_good, dict) or not isinstance(last_good.get("context"), dict):
        return None
    context = last_good["context"]
    then = {m["id"]: m for m in context.get("mods") or [] if isinstance(m, dict) and isinstance(m.get("id"), str)
            and m.get("enabled")}
    out = Changes(when=float(last_good.get("time") or 0) if isinstance(last_good.get("time"), (int, float)) else 0.0)
    old_game = context.get("game_version") if isinstance(context.get("game_version"), str) else ""
    out.game_updated = bool(old_game and game_version and old_game != game_version)
    now = {m.id for m in mods if m.enabled and m.has_files}
    for m in mods:
        if m.id not in now:
            continue
        old = then.get(m.id)
        if old is None:
            out.added.append(m.id)
        elif isinstance(old.get("stamp"), str) and old["stamp"] != m.stamp:
            out.updated.append(m.id)
        elif isinstance(old.get("options"), list) and sorted(map(str, old["options"])) != sorted(m.options):
            out.options.append(m.id)
    on = {m.id for m in mods if m.enabled}  # (one on with no option chosen is not "turned off")
    out.removed = [str(o.get("name") or i) for i, o in then.items() if i not in on]
    return out


# ---- files the manager cannot turn off -----------------------------------------------------------------
BACKUP_MARK = " (모드를 끄면 되돌아옴)"  # after a file placed by hand that a mod's file covers now


def _mod_file(rel: str) -> bool:
    """A game path the game or REFramework loads as a mod: proxy DLLs and .asi in the game folder,
    natives\\, pak_mods\\*.pak, REFramework plugins and scripts (what unmanaged_files looks at)."""
    from .crashlog import PROXY_DLLS

    parts = str(rel).replace("\\", "/").strip("/").split("/")
    low = [p.casefold() for p in parts]
    if len(parts) == 1:
        return low[0] in PROXY_DLLS or low[0] == REFRAMEWORK_DLL or low[0].endswith(".asi")
    if low[0] == "natives":
        return not parts[-1].endswith(".mhwmm-tmp")
    if low[0] == PAK_MODS_DIR:
        return parts[-1].endswith(".pak")  # REFramework's test is case-sensitive
    if low[0] == "reframework" and len(parts) >= 3:
        return (low[1] == "plugins" and low[-1].endswith(".dll")) or (low[1] == "autorun" and low[-1].endswith(".lua"))
    return False


def backed_up_refw(backups: dict | None) -> bool:
    """A dinput8.dll placed by hand (REFramework, or another loader) is kept aside under the REFramework
    mod's: it comes back, and loads, whenever that mod is off. ``backups``: deployed_backups()."""
    return any(isinstance(b, dict) and str(b.get("path") or "").casefold() == REFRAMEWORK_DLL
               for b in (backups or {}).values())


def unmanaged_files(game_dir, deployed: dict | None, limit: int = MAX_UNMANAGED_LIST,
                    scan: int = MAX_UNMANAGED_SCAN, backups: dict | None = None) -> tuple[list[str], int]:
    """Mod files in the game folder the manager did not place, so turning mods off leaves them
    (natives\\, REFramework plugins and scripts, pak_mods, proxy DLLs and .asi in the game folder):
    up to ``limit`` game paths and how many were found (looking at no more than ``scan`` entries).
    ``backups`` (the deployment's, deployed_backups()): files placed by hand that a mod's file covers
    now come back when that mod is off; they are listed too, with BACKUP_MARK."""
    if not game_dir:
        return [], 0
    game = Path(game_dir)
    known = {str(k).replace("\\", "/").casefold() for k in (deployed or {})}
    found: list[str] = []
    seen = 0

    def walk(rel: str) -> None:
        nonlocal seen
        stack = [rel]
        while stack and seen < scan:
            folder = stack.pop()
            try:
                entries = sorted(os.scandir(fsutil.longpath(game / folder)), key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                seen += 1
                if seen > scan:
                    return
                path = f"{folder}/{e.name}"
                try:
                    if e.is_dir():
                        stack.append(path)
                    elif e.is_file() and _mod_file(path) and path.casefold() not in known:
                        found.append(path)
                except OSError:
                    continue

    try:
        root = sorted(os.scandir(fsutil.longpath(game)), key=lambda e: e.name)
    except OSError:
        return [], 0
    # natives\\ last: it may be large, and the budget is better spent on plugins and paks first
    for e in sorted(root, key=lambda e: e.name.casefold() == "natives"):
        low = e.name.casefold()
        try:
            if e.is_file() and _mod_file(e.name):
                if low not in known:
                    found.append(e.name)
            elif e.is_dir() and low in ("natives", PAK_MODS_DIR):
                walk(e.name)
            elif e.is_dir() and low == "reframework":
                for sub in ("plugins", "autorun"):
                    target = next((d.name for d in _dirs(game / e.name) if d.name.casefold() == sub), None)
                    if target:
                        walk(f"{e.name}/{target}")
        except OSError:
            continue
    for b in (backups or {}).values():
        rel = str(b.get("path") or "") if isinstance(b, dict) else ""
        if rel and _mod_file(rel):
            found.append(rel.replace("\\", "/") + BACKUP_MARK)
    found.sort(key=str.casefold)
    return found[:limit], len(found)


def _dirs(path: Path) -> list:
    try:
        return [e for e in os.scandir(fsutil.longpath(path)) if e.is_dir()]
    except OSError:
        return []


# ---- the plan shown before starting ----------------------------------------------------------------
@dataclass
class Plan:
    """The search as it would start now (the start dialog)."""
    units: list[str]  # suspects, in search order
    base: list[str]  # always on (REFramework first)
    core: list[str]  # base, requirements first
    requires: dict[str, list[str]]
    split: int | None  # units unchanged since the last normal exit (tested first)
    names: dict[str, str]
    order: list[str]  # the session's mods in load order
    excluded: list[str] = field(default_factory=list)  # ids of enabled mods with nothing to apply
    unmanaged: list[str] = field(default_factory=list)  # game paths (the first ones)
    unmanaged_total: int = 0
    changes: Changes | None = None
    game_version: str = ""
    profile: str = ""
    runs: int = 0  # about how many launches (with split: when one of the changed mods is the cause)
    runs_all: int = 0  # with split: about how many when the cause is any mod (the last good set crashes too)
    refw_backup: bool = False  # a dinput8.dll placed by hand comes back when the REFramework mod is off
    revision: int | None = None  # manager.revision the plan was made at (start() refuses a stale plan)

    @property
    def count(self) -> int:
        """Enabled mods taking part (suspects and the ones kept on)."""
        return len(self.units) + len(self.base)

    @property
    def problem(self) -> str:
        """Why the search cannot start ("" when it can)."""
        if not self.units and not self.base:
            return "켜져 있는 모드가 없어 원인 모드를 찾을 수 없습니다."
        return ""

    def intro(self) -> str:
        runs = f"약 {self.runs}번"
        if self.split and self.runs_all > self.runs:
            runs = f"마지막 정상 종료 뒤로 바뀐 모드가 원인이면 약 {self.runs}번, 아니면 약 {self.runs_all}번"
        return INTRO.format(count=self.count, runs=runs)

    def lines(self) -> list[str]:
        """Details under the introduction."""
        out = []
        if self.base:
            out.append("계속 켜 둘 모드: " + ", ".join(self.names.get(i, i) for i in self.base))
        if self.excluded:
            out.append("옵션을 고르지 않아 적용되지 않는 모드(빼고 찾음): "
                       + ", ".join(self.names.get(i, i) for i in self.excluded))
        if self.unmanaged_total:
            shown = ", ".join(self.unmanaged[:3]) + (" 등" if self.unmanaged_total > 3 else "")
            out.append(f"매니저가 넣지 않은 파일 {self.unmanaged_total}개는 켜고 끌 수 없어 그대로 둡니다: {shown}")
        if self.refw_backup:
            out.append("REFramework 모드를 끄면 직접 넣어 두었던 dinput8.dll이 되돌아와, 모든 모드를 끈 단계에서도 "
                       "그 파일(REFramework 등)은 그대로 남습니다.")
        if self.changes is not None:
            text = self.changes.text(self.names)
            out.append(text + (" → 바뀐 모드부터 확인합니다." if self.split else ""))
        return out


def facts(manager) -> list[ModFacts]:
    """ModFacts of every installed mod of a manager.ModManager (GUI thread)."""
    refw = manager.refw_mod()
    out = []
    for m in manager.mods:
        if m.enabled:
            try:
                manager.requirement_status(m.id)  # reads the Lua modules of mods installed by older versions
            except Exception as exc:  # requirements are only a help here
                log.debug("필수 모드를 확인하지 못했습니다 (%s): %s", m.name, exc)
        files = manager.mod_files(m.id)
        kinds = manager.mod_kinds(m.id)
        out.append(ModFacts(
            m.id, m.name, m.enabled, has_files=bool(files), needs_option=m.needs_option, nexus_id=m.nexus_id,
            requirements=list(m.requirements), lua_requires=list(m.lua_requires),
            provides=frozenset(requirements.provided_modules(files)),
            needs_refw=bool(kinds & {"loose", "script", "pak"}),
            refw=refw is not None and refw.id == m.id, stamp=f"{m.installed_at}|{m.updated_at}",
            options=[o["name"] for o in m.options if o["id"] in m.selected]))
    return out


def make_plan(mods: Sequence[ModFacts], last_good: dict | None = None, game_version: str = "",
              unmanaged: tuple[list[str], int] = ((), 0), profile: str = "", refw_backup: bool = False,
              revision: int | None = None) -> Plan:
    """The search over the enabled mods (pure: see :func:`prepare` for a manager)."""
    requires = requirement_ids(mods)
    active = [m for m in mods if m.enabled and m.has_files]
    excluded = [m.id for m in mods if m.enabled and not m.has_files]
    ids = [m.id for m in active]
    refw = next((m.id for m in active if m.refw), None)
    kept = _closure(ids, {i: requires.get(i, []) for i in ids}) & set(ids)
    if refw is not None:
        kept.add(refw)
    base = [i for i in ids if i in kept]
    rest = [i for i in ids if i not in kept]
    requires = {i: [r for r in requires.get(i, []) if r in set(ids)] for i in ids}
    changes = changes_since(last_good, mods, game_version)
    split = None
    if changes is not None:
        changed = set(changes.changed)
        same = [i for i in rest if i not in changed]
        rest = same + [i for i in rest if i in changed]
        split = len(same) if 0 < len(same) < len(rest) else None
    core = dependency_order(base, requires, refw)
    plan = Plan(rest, base, core, requires, split, {m.id: m.name for m in mods}, ids, excluded,
                list(unmanaged[0]), int(unmanaged[1]), changes, game_version, profile,
                refw_backup=refw_backup, revision=revision)
    plan.runs = estimate_runs(plan.units, plan.base, plan.core, plan.requires, plan.split)
    plan.runs_all = estimate_runs(plan.units, plan.base, plan.core, plan.requires, plan.split, everywhere=True) \
        if plan.split else plan.runs
    return plan


def prepare(manager, last_good: dict | None = None, scan: bool = True) -> Plan:
    """The plan for a manager.ModManager: its enabled mods, the last normal exit (gamewatch) and the
    files it did not place (``scan``). GUI thread; reads the game folder a little."""
    from . import gamewatch

    if last_good is None:
        last_good = gamewatch.load_last_good(manager.crash_dir)
    mods, revision = facts(manager), manager.revision
    backups = manager.deployed_backups()
    unmanaged: tuple[list[str], int] = ([], 0)
    if scan and manager.game_dir:
        try:
            unmanaged = unmanaged_files(manager.game_dir, manager.deployed_files(), backups=backups)
        except Exception as exc:  # only a list for the user
            log.warning("매니저가 넣지 않은 파일을 찾지 못했습니다: %s", exc)
    return make_plan(mods, last_good, manager.game_version(refresh=True), unmanaged, manager.profile,
                     backed_up_refw(backups), revision)


# ---- the session (bisect.json) ----------------------------------------------------------------------
def _ids(value) -> list[str]:
    return [i for i in value if isinstance(i, str)] if isinstance(value, list) else []


class Session:
    """A search in progress, kept in ``<data>/bisect.json``."""

    def __init__(self, path: Path, data: dict):
        self.path = Path(path)
        self.data = data
        self._next: Step | Result | None = None

    @classmethod
    def new(cls, data_dir, plan: Plan, original: str, original_snapshot: dict, temp: str, record: str = "") -> Session:
        data = {"format": SESSION_FORMAT, "started": time.time(), "original_profile": original,
                "original_snapshot": copy.deepcopy(original_snapshot), "temp_profile": temp,
                "game_version": plan.game_version, "record": record, "names": dict(plan.names),
                "order": list(plan.order), "units": list(plan.units), "base": list(plan.base), "core": list(plan.core),
                "requires": {k: list(v) for k, v in plan.requires.items()}, "split": plan.split,
                "estimate": plan.runs, "excluded": list(plan.excluded),
                "unmanaged": {"total": plan.unmanaged_total, "paths": list(plan.unmanaged[:20])},
                "refw_backup": bool(plan.refw_backup),
                "changes": plan.changes.text(plan.names) if plan.changes is not None else "",
                "steps": [], "current": None, "result": None}
        return cls(Path(data_dir) / SESSION_FILE, data)

    @classmethod
    def load(cls, data_dir) -> Session | None:
        path = Path(data_dir) / SESSION_FILE
        data = fsutil.read_json(path, None)
        if not isinstance(data, dict) or data.get("format") != SESSION_FORMAT:
            if data is not None:
                log.warning("원인 찾기 기록을 읽지 못해 무시합니다: %s", path)
            return None
        if not isinstance(data.get("original_profile"), str) or not isinstance(data.get("temp_profile"), str):
            return None
        data["steps"] = [s for s in data.get("steps") or [] if isinstance(s, dict) and isinstance(s.get("on"), list)
                         and isinstance(s.get("crashed"), bool)]
        if not isinstance(data.get("current"), dict):
            data["current"] = None
        return cls(path, data)

    def save(self) -> None:
        fsutil.atomic_write_json(self.path, self.data)

    def remove(self) -> None:
        fsutil.remove_file(self.path)

    # ---- what it holds
    @property
    def original_profile(self) -> str:
        return self.data["original_profile"]

    @property
    def temp_profile(self) -> str:
        return self.data["temp_profile"]

    @property
    def game_version(self) -> str:
        return self.data.get("game_version") or ""

    @property
    def names(self) -> dict[str, str]:
        names = self.data.get("names")
        return names if isinstance(names, dict) else {}

    def name(self, mod_id: str) -> str:
        return str(self.names.get(mod_id) or mod_id)

    @property
    def mod_ids(self) -> list[str]:
        """The mods the search turns on and off."""
        return list(dict.fromkeys([*_ids(self.data.get("base")), *_ids(self.data.get("units"))]))

    @property
    def steps(self) -> list[dict]:
        """The launches answered: {"on", "crashed", "kind", "number", "time", "exit", "detected"}."""
        return self.data["steps"]

    @property
    def current(self) -> dict | None:
        """The step applied now: {"on", "applied", "launched", "exit", "retries"} (None: not applied yet)."""
        return self.data.get("current")

    @property
    def result(self) -> Result | None:
        return Result.from_dict(self.data.get("result"))

    @property
    def excluded(self) -> list[str]:
        return _ids(self.data.get("excluded"))

    @property
    def unmanaged(self) -> tuple[list[str], int]:
        u = self.data.get("unmanaged") if isinstance(self.data.get("unmanaged"), dict) else {}
        return _ids(u.get("paths")), int(u.get("total") or 0) if isinstance(u.get("total"), int) else 0

    @property
    def refw_backup(self) -> bool:
        """A dinput8.dll placed by hand stayed in the game whenever the REFramework mod was off."""
        return self.data.get("refw_backup") is True

    @property
    def requires(self) -> dict[str, list[str]]:
        req = self.data.get("requires")
        return {k: _ids(v) for k, v in req.items() if isinstance(k, str)} if isinstance(req, dict) else {}

    def next(self) -> Step | Result:
        """The step to run now, or the result (a stored result wins: an aborted search)."""
        stored = self.result
        if stored is not None:
            return stored
        if self._next is None:
            d = self.data
            split = d.get("split") if isinstance(d.get("split"), int) else None
            self._next = next_step(_ids(d.get("units")), _ids(d.get("base")), _ids(d.get("core")), self.requires, split,
                                   [(s["on"], s["crashed"]) for s in self.steps], _ids(d.get("order")),
                                   d.get("estimate") if isinstance(d.get("estimate"), int) else 0)
        return self._next

    @property
    def launched(self) -> float | None:
        """When the game was started for the current step (for gamewatch.detect)."""
        t = (self.current or {}).get("launched")
        return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None

    @property
    def record_id(self) -> str:
        """The crash record (crashes.Session id) the search was started from ("" if none)."""
        return str(self.data.get("record") or "")

    @property
    def awaiting_answer(self) -> bool:
        """The current step's game was started and not answered for yet."""
        return bool(self.current and self.current.get("launched")) and isinstance(self.next(), Step)

    # ---- changes
    def mark_applied(self, step: Step) -> None:
        cur = self.current
        if cur is None or sorted(cur.get("on") or []) != sorted(step.on):
            self.data["current"] = {"on": list(step.on), "applied": time.time(), "launched": None, "exit": None,
                                    "retries": 0}
        else:
            cur["applied"] = time.time()
        self.save()

    def mark_launched(self, when: float | None = None) -> None:
        """The game was started for the current step (before it, the crash records are collected)."""
        if self.current is not None:
            self.current["launched"] = when if when is not None else time.time()
            self.current["exit"] = None
            self.save()

    def note_exit(self, exit_info: dict | None) -> None:
        """How the game of the current step ended (gamewatch.GameExit.to_dict()), kept for a restart."""
        if self.current is not None:
            self.current["exit"] = exit_info
            self.save()

    def record(self, crashed: bool | None, detected: str = "") -> Step | Result:
        """The user's answer for the current step: True (튕겼어요), False (괜찮았어요) or None (잘
        모르겠어요: the same step again). Returns what comes next (not applied yet)."""
        step = self.next()
        if not isinstance(step, Step):
            return step
        cur = self.current or {}
        if crashed is None:
            if self.current is not None:
                self.current["retries"] = int(cur.get("retries") or 0) + 1
                self.current["launched"] = None
                self.current["exit"] = None
            self.save()
            return step
        self.steps.append({"on": list(step.on), "crashed": bool(crashed), "kind": step.kind, "number": step.number,
                           "time": time.time(), "launched": cur.get("launched"), "exit": cur.get("exit"),
                           "detected": detected})
        self.data["current"] = None
        self._next = None
        nxt = self.next()
        if isinstance(nxt, Result):
            self.data["result"] = nxt.to_dict()
            log.info("원인 찾기 결과: %s", result_title(nxt, self.names))
        self.save()
        return nxt

    def abort(self, reason: str) -> Result:
        result = Result("aborted", runs=len(self.steps), reason=reason)
        self.data["result"] = result.to_dict()
        self._next = None
        self.save()
        return result

    # ---- texts
    def banner(self, step: Step, applied: bool = True) -> str:
        """The step's line (``applied``: it is in the game folder already)."""
        return step_text(step, self.names, applied, self.refw_backup)

    def result_lines(self, result: Result, refw: str | None = None) -> list[str]:
        """What the result means (``refw``: the REFramework mod's id)."""
        return result_lines(result, self.names, refw, self.requires, self.unmanaged[1], self.refw_backup)

    def report_lines(self) -> list[str]:
        """The search for report.txt of the support zip."""
        out = [f"[{TITLE}]", f"원래 프로필: {self.original_profile}, 임시 프로필: {self.temp_profile}",
               f"찾는 모드 {len(_ids(self.data.get('units')))}개, 계속 켜 둔 모드: "
               + (", ".join(self.name(i) for i in _ids(self.data.get("base"))) or "-")]
        if self.data.get("changes"):
            out.append(str(self.data["changes"]))
        if self.excluded:
            out.append("뺀 모드(옵션 선택 필요): " + ", ".join(self.name(i) for i in self.excluded))
        paths, total = self.unmanaged
        if total:
            out.append(f"매니저가 넣지 않은 파일 {total}개: " + ", ".join(paths[:10]))
        for s in self.steps:
            on = _ids(s.get("on"))
            exit_text = ""
            if isinstance(s.get("exit"), dict) and s["exit"].get("code") is not None:
                exit_text = f", 종료 코드 0x{int(s['exit']['code']) & 0xFFFFFFFF:08X}"
            out.append(f"{s.get('number', '?')}단계: 모드 {len(on)}개 켬 → {'튕김' if s['crashed'] else '괜찮음'}"
                       f"{exit_text}{', ' + s['detected'] if s.get('detected') else ''}")
        nxt = self.next()
        if isinstance(nxt, Result):
            out.append("결과: " + result_title(nxt, self.names))
        else:
            out.append(f"진행 중: {nxt.number}단계")
        return out


# ---- texts ---------------------------------------------------------------------------------------
def _quoted(ids: Sequence[str], names: dict[str, str]) -> str:
    return ", ".join(f"'{names.get(i, i)}'" for i in ids)


def step_text(step: Step, names: dict[str, str], applied: bool = True, refw_backup: bool = False) -> str:
    """The banner line of a step (without its buttons). ``applied``: the step is in the game folder
    (else it is only set in the temporary profile); ``refw_backup``: a dinput8.dll placed by hand
    comes back when the REFramework mod is off (see :func:`backed_up_refw`)."""
    head = f"원인 찾기 {step.number}/{step.total}단계: "
    count = len(step.on)
    verb = "켜"  # what was done to the mods: "켜고 적용했습니다" or "켜 두었습니다"
    note = ""  # after the verb: "(필요한 모드 포함 3개)"
    extra = ""
    if step.kind == "everything_off" or not step.on:
        lead, verb = "모든 모드를", "끄"
        if refw_backup:
            extra = " 직접 넣어 두었던 dinput8.dll은 되돌아와 그대로 있습니다."
    elif step.kind == "last_good":
        lead, note = "마지막 정상 종료 뒤로 바뀌지 않은 모드만", f"(계속 켜 두는 모드 포함 {count}개)"
    elif step.kind == "base":
        lead = f"REFramework와 다른 모드에 필요한 모드 {count}개만"
    elif step.kind == "confirm" and any(i in step.on for i in step.found):
        shown = [i for i in step.found if i in step.on]
        lead, note = f"확인을 위해 {_quoted(shown, names)}만", f"(필요한 모드 포함 {count}개)"
    elif step.kind == "confirm":  # the found one off, what it needs on
        lead, verb, note = f"확인을 위해 {_quoted(step.found, names)}만", "끄", f"(모드 {count}개 켬)"
    elif step.kind == "partner":
        lead = f"{_quoted(step.found, names)} 모드와 함께 튕기는 모드를 찾습니다. 모드 {count}개를"
    elif step.kind == "full":
        lead = f"결과를 확인하기 위해 처음처럼 모드 {count}개를 모두"
    elif step.kind == "again":
        lead = f"결과를 확인하기 위해 앞에서 튕겼던 구성(모드 {count}개)을 다시"
    else:
        lead = f"모드 {count}개를"
    if applied:
        body = f"{lead} {verb}고{note} 적용했습니다."
    else:
        done = {"켜": "켜", "끄": "꺼"}[verb]
        body = f"{lead} {done} 두었습니다{note}. 게임을 실행하면 먼저 게임 폴더에 적용합니다."
    return head + body + extra + " " + PLAY_AGAIN


def result_title(result: Result, names: dict[str, str]) -> str:
    """The result's headline."""
    if result.kind == "found":
        return f"원인으로 보이는 모드: {names.get(result.mods[0], result.mods[0])}"
    if result.kind == "together":
        return "함께 켜면 튕기는 모드: " + ", ".join(names.get(i, i) for i in result.mods)
    if result.kind == "core":
        return "원인으로 보이는 모드: " + ", ".join(names.get(i, i) for i in result.mods)
    if result.kind == "not_mods":
        return "모든 모드를 꺼도 튕겼습니다. 모드 문제가 아닐 수 있습니다."
    if result.kind == "intermittent":
        return ("켜고 끌 때마다 결과가 달라 원인을 좁히지 못했습니다. 가끔씩 튕기는 경우(그래픽 드라이버, VRAM 부족 등)일 "
                "수 있습니다.")
    if result.kind == "no_repro":
        return "처음처럼 모든 모드를 켜도 튕기지 않아 원인을 좁히지 못했습니다."
    return result.reason or "원인 찾기를 그만두었습니다."


def result_lines(result: Result, names: dict[str, str], refw: str | None = None,
                 requires: dict[str, list[str]] | None = None, unmanaged: int = 0,
                 refw_backup: bool = False) -> list[str]:
    """What the result means and what to try (under the headline). ``refw``: the REFramework mod's id;
    ``refw_backup``: a dinput8.dll placed by hand came back whenever the REFramework mod was off."""
    if result.kind == "found":
        return ["이 모드를 켰을 때만 튕겼습니다. 모드를 최신 버전으로 업데이트하거나, 재질 점검·복구를 해 보거나, 만든 "
                "사람에게 알려 보세요."]
    if result.kind == "together":
        which = "둘 중" if len(result.mods) == 2 else "이 중"
        return ["하나씩은 괜찮고 함께 켰을 때만 튕겼습니다. 같은 파일이나 기능을 건드리는 모드일 수 있습니다. "
                f"{which} 하나를 끄거나 업데이트해 보세요."]
    if result.kind == "core":
        out = []
        if refw is not None and refw in result.mods:
            out.append("REFramework를 켰을 때 튕겼습니다. REFramework를 최신 빌드로 업데이트해 보세요.")
            needing = sum(1 for i, reqs in (requires or {}).items() if refw in reqs and i not in result.mods)
            out.append("REFramework를 끄면 루스 파일·PAK·스크립트 모드가 모두 동작하지 않습니다"
                       + (f"(켜져 있는 모드 중 {needing}개)" if needing else "") + ".")
        users = sorted({i for i, reqs in (requires or {}).items() for r in reqs if r in result.mods and r != refw}
                       - set(result.mods))
        others = [i for i in result.mods if i != refw]
        if others:
            out.append(f"{_quoted(others, names)}은(는) 다른 모드에 필요한 모드라 계속 켜 두었던 모드입니다."
                       + (f" 끄면 {_quoted(users[:5], names)}도 동작하지 않습니다." if users else ""))
        return out
    if result.kind == "not_mods":
        out = []
        if refw_backup:
            out.append("REFramework 모드를 꺼도 직접 넣어 두었던 dinput8.dll(REFramework 등)이 되돌아와 계속 켜져 "
                       "있었습니다. 그 파일이 원인일 수 있습니다. REFramework를 최신 빌드로 바꾸거나 그 파일을 뺀 뒤 "
                       "다시 해 보세요.")
        out.append("Steam의 '게임 파일 무결성 확인', 그래픽 드라이버 업데이트, 오버레이·녹화 프로그램 끄기를 해 보세요.")
        if unmanaged:
            out.append(f"매니저가 넣지 않은 파일 {unmanaged}개는 그대로 있었습니다. 이 파일이 원인일 수도 있습니다.")
        return out
    if result.kind in ("intermittent", "no_repro"):
        return ["같은 상황에서 매번 튕기는지 확인한 뒤 다시 해 보세요. 게임 튕김 기록에 남은 오류도 함께 확인해 보세요."]
    return []


# ---- running a search with a manager.ModManager ---------------------------------------------------
def load(data_dir) -> Session | None:
    """The search in progress (None: none)."""
    return Session.load(data_dir)


def active(data_dir) -> bool:
    """A search is in progress: installs, turning mods on or off and profile changes wait (BLOCKED).
    (A file this version cannot read blocks nothing.)"""
    data = fsutil.read_json(Path(data_dir) / SESSION_FILE, None)
    return isinstance(data, dict) and data.get("format") == SESSION_FORMAT


def _temp_name(profiles) -> str:
    taken = {p.casefold() for p in profiles}
    if TEMP_PROFILE.casefold() not in taken:
        return TEMP_PROFILE
    n = 2
    while f"{TEMP_PROFILE[:-1]} {n})".casefold() in taken:
        n += 1
    return f"{TEMP_PROFILE[:-1]} {n})"


def start(manager, plan: Plan, record: str = "") -> tuple[Session, Step | Result]:
    """Start a search: switch to a temporary copy of the current profile and turn the first step's
    mods on there. The caller then applies (deploys). ``record``: the crash record it started from."""
    if active(manager.data_dir):
        raise BisectError("이미 원인 찾기가 진행 중입니다.")
    if plan.problem:
        raise BisectError(plan.problem)
    enabled = {m.id for m in manager.mods if m.enabled}
    if manager.profile != plan.profile or enabled != {*plan.order, *plan.excluded} or \
            (plan.revision is not None and plan.revision != manager.revision):
        raise BisectError("준비하는 동안 모드 상태가 바뀌었습니다. 원인 모드 찾기를 다시 시작하세요.")
    if manager.outdated_paks():  # moving them changes the game's fingerprint: problem() would see an update
        raise BisectError(OUTDATED_PAKS)
    manager.save()  # the original profile's snapshot as it is now
    original = manager.profile
    temp = _temp_name(manager.profiles)
    session = Session.new(manager.data_dir, plan, original, manager.profiles.get(original) or {}, temp, record)
    session.save()  # first: a temporary profile is never left without its search
    try:
        manager.create_profile(temp, copy_current=True)
    except BaseException:
        session.remove()
        raise
    log.info("원인 찾기를 시작합니다: 모드 %d개 중에서 찾고 %d개는 계속 켜 둡니다 (임시 프로필 '%s').",
             len(plan.units), len(plan.base), temp)
    return session, apply_step(manager, session)


def problem(manager, session: Session) -> str:
    """Why the search cannot go on ("" when it can): the game was updated, the temporary profile is
    not the one in use, or mods it turns on and off are gone."""
    if session.temp_profile not in manager.profiles:
        return "원인 찾기용 임시 프로필이 없어져 원인 찾기를 그만둡니다."
    if manager.profile != session.temp_profile:
        return "다른 프로필로 바뀌어 원인 찾기를 그만둡니다."
    known = {m.id for m in manager.mods}
    if any(i not in known for i in session.mod_ids):
        return "원인 찾기 중에 모드가 지워져 원인 찾기를 그만둡니다."
    version = manager.game_version(refresh=True)
    if session.game_version and version and version != session.game_version:
        return "게임이 업데이트되어 원인 찾기를 그만둡니다. 필요하면 다시 시작하세요."
    return ""


def apply_step(manager, session: Session) -> Step | Result:
    """Turn the current step's mods on (and the session's other mods off) in the temporary profile;
    the caller applies (deploys) when a Step comes back."""
    nxt = session.next()
    if not isinstance(nxt, Step):
        return nxt
    if manager.profile != session.temp_profile:
        raise BisectError("원인 찾기용 임시 프로필이 선택되어 있지 않습니다.")
    want = set(nxt.on)
    inside = set(session.mod_ids)
    changed = False
    for m in manager.mods:
        if m.id in inside and m.enabled != (m.id in want):
            m.enabled = m.id in want
            changed = True
    if changed:
        manager.revision += 1
        manager.dirty = True
        manager.save()
    session.mark_applied(nxt)
    log.info("원인 찾기 %d단계: 모드 %d개를 켭니다.", nxt.number, len(nxt.on))
    return nxt


def answer(manager, session: Session, crashed: bool | None, detected: str = "") -> Step | Result:
    """Record the answer for the current step and turn the next step's mods on (then the caller
    applies), or return the result (the temporary profile stays until :func:`finish`)."""
    nxt = session.record(crashed, detected)
    if isinstance(nxt, Step):
        return apply_step(manager, session)
    return nxt


def finish(manager, session: Session, disable: Sequence[str] = ()) -> None:
    """End the search: back to the original profile (made again from the copy kept at the start if
    it went missing), delete the temporary one, then turn ``disable`` off in the original profile
    (the user chose "이 모드를 끄고 원래 프로필로"). The caller then applies."""
    original, temp = session.original_profile, session.temp_profile
    if original not in manager.profiles:
        manager.profiles[original] = copy.deepcopy(session.data.get("original_snapshot") or {})
        log.warning("원래 프로필 '%s'이(가) 없어 원인 찾기를 시작할 때의 상태로 되살립니다.", original)
    if manager.profile != original:
        manager.switch_profile(original)
    if temp != original and temp in manager.profiles:
        manager.delete_profile(temp)
    for mod_id in disable:
        try:
            manager.set_enabled(mod_id, False)
        except KeyError:
            continue
    manager.dirty = True  # the game folder still has the last step's mods
    manager.save()
    session.remove()
    log.info("원인 찾기를 마치고 '%s' 프로필로 돌아왔습니다%s.", original,
             f" ({', '.join(session.name(i) for i in disable)} 끔)" if disable else "")
