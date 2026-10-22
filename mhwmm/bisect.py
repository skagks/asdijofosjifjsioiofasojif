"""원인 모드 찾기: finding the mod that makes the game crash by running it with some mods off.

The user's profile is never edited: the search works in a temporary profile (a copy of the
current one) and each step only turns mods of that copy on or off; the caller applies it and the
user plays until the crash would come, then answers.  At the end the original profile comes
back (manager.switch_profile saves the temporary state into the temporary profile, which is then
deleted).  The search is kept in ``<data>/bisect.json`` and goes on after a restart.

Since v2.2.4 (bisect.json format 2) the search is Bayesian (:mod:`bisect_engine`): every possible
cause (one mod, two that crash only together, two that each crash, three together, not the mods)
gets a probability from the evidence (the crash record, older launches, the last normal exit, the
material state: :mod:`evidence`, through the plain attributes of ``evidence.Evidence``), each
launch is the one that tells the most, and answers are weighed (a crash may need longer than that
play, the game may not have loaded the mod's files, an answer may be wrong).  A mod is named only
with P >= 95% and at least two independent pivot pairs (a crash with it on - a launch of the search
or the crash record - and a long enough fine launch of the search with it off and everything else of
that crash on; three when crashes look intermittent or after 한 번 더 확인), so one wrong answer
never names a mod that is not the cause, whatever the evidence says (older sessions only move the
probabilities).  Units are every enabled mod with files that reaches the game (REFramework and the
libraries too); a unit on brings what it requires, and a mod whose files another overwrites brings
that mod (a test never loads a copy of a file the user's own setup never loads).  Mods that need
an option first, mods whose files all lose to others (on only with every mod covering them) and
files the manager did not place are left out and listed.  Before a mod is named, the game runs with
every mod off while "not the mods" keeps some probability (up to five fine launches: a crash that
comes and goes whatever is on can otherwise look like any mod; Params.none_controls).  A record of a
graphics error the game reported itself (DEVICE_HUNG ...) is a crash like any other: it raises the
suspicion of mods with render data (evidence), not of "not the mods" (a search started by v2.2.5
from such a record keeps the two launches with every mod off it asked for first, a search started
before this release keeps its rules: the parameters stored with it).  Every result is a suspicion
with a probability and its evidence.

A search started by v2.2.2/v2.2.3 (format 1) goes on with the search it started with (``_Search``
below: a fixed halving, kept unchanged for those sessions only).
"""

from __future__ import annotations

import copy
import logging
import math
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from . import bisect_engine as engine_mod
from . import fsutil, requirements
from .bisect_engine import Decision, Engine, Obs, Params, Prior, Suspect
from .constants import PAK_MODS_DIR, REFRAMEWORK_DLL

log = logging.getLogger(__name__)

SESSION_FILE = "bisect.json"  # in the data folder
SESSION_FORMAT = 2
LEGACY_FORMAT = 1  # v2.2.2/v2.2.3: the fixed halving search
TEMP_PROFILE = "원인 찾기 (임시)"
MAX_UNMANAGED_SCAN = 20000  # entries looked at when listing files the manager did not place
MAX_UNMANAGED_LIST = 200
ESTIMATE_SAMPLES = 40  # culprit positions simulated for the "about N launches" estimate (format 1)
MORE_LAUNCHES = engine_mod.Params().more_launches  # 계속 찾기
CONFIRM_LAUNCHES = 3  # 한 번 더 확인 has at least this many launches left (more when its pairs need more)
EQUIP_WEIGHT = 10.0  # the mods of the equipment the user picked
TOP_SIMULATED = 8  # launches simulated at most for "가장 의심되는 모드가 원인이면 약 N번"

TITLE = "원인 모드 찾기"  # the window and the ☰ menu item
INTRO = ("켜져 있는 모드 {count}개 중 게임을 튕기게 하는 모드를 찾습니다. 절반씩 끄고 실행해 보는 방식이라 {runs} "
         "실행하면 됩니다. 지금 프로필은 그대로 두고 임시 프로필을 씁니다. REFramework와 다른 모드에 필요한 모드는 계속 "
         "켜 둡니다. 테스트 중에는 가능하면 게임을 저장하지 마세요.")
INTRO_V2 = ("켜져 있는 모드 {count}개 중 게임을 튕기게 하는 모드를 찾습니다. 기록으로 의심되는 정도를 매긴 뒤, 실행할 때마다 "
            "가장 많이 좁힐 수 있는 조합으로 모드를 켜고 끕니다. 예상: 게임을 {runs} 실행합니다. 지금 프로필은 그대로 두고 "
            "임시 프로필을 씁니다. 다른 모드에 필요한 모드는 그 모드를 켤 때 함께 켭니다. 테스트 중에는 가능하면 게임을 "
            "저장하지 마세요.")
ASK = "또 튕겼나요?"
ANSWER_CRASHED = "튕겼어요"
ANSWER_FINE = "괜찮았어요"
ANSWER_RETRY = "잘 모르겠어요 (같은 구성으로 다시)"
BUTTON_LAUNCH = "게임 실행"
BUTTON_STOP = "그만두기"
RESULT_DISABLE = "이 모드를 끄고 원래 프로필로"
RESULT_DISABLE_MANY = "이 모드들을 끄고 원래 프로필로"
RESULT_DISABLE_UNSURE = "'{name}'(약 {pct})을(를) 끄고 원래 프로필로"
RESULT_RESTORE = "원래 프로필로"
RESULT_REPORT = "진단 파일 저장"
RESULT_MORE = "계속 찾기"
RESULT_CONFIRM = "한 번 더 확인"
BLOCKED = "원인 모드 찾기 중에는 할 수 없습니다. 원인 찾기를 마치거나 그만둔 뒤 다시 하세요."
PLAY_AGAIN = "튕기던 상황을 다시 해 보세요."
OUTDATED_PAKS = ("예전 방식(패치 번호)으로 넣은 PAK이 게임 폴더에 남아 있습니다. 먼저 '게임에 적용'으로 pak_mods 폴더로 "
                 "옮긴 뒤 다시 시작하세요.")
RANKING_HEADING = "가장 의심되는 모드 (기록으로 매긴 것이고, 확실하다는 뜻은 아닙니다)"
NO_EVIDENCE = "기록에서 특별히 의심되는 모드가 없습니다. 모든 모드를 같은 정도로 의심하고 시작합니다."
NO_EVIDENCE_KINDS = ("기록에서 특별히 의심되는 모드가 없습니다. 모드 종류(플러그인·스크립트 등)로만 의심되는 정도를 조금씩 "
                     "다르게 매겼습니다.")
CONFIRM_NOTE = "한 번 더 실행해 확인하면 더 확실해집니다 (답 하나가 틀렸더라도 결과가 바뀌지 않도록 이미 확인했습니다)."
WEAK_ANSWER = " · 이번 답은 약하게만 반영했습니다(지난번 튕긴 시점보다 짧게 실행)"
UNAPPLIED = " 게임을 실행하면 먼저 게임 폴더에 적용합니다."

KINDS = ("half", "base", "last_good", "confirm", "partner", "again", "full", "everything_off", "core",
         "off_some", "on_some", "split")
RESULTS = ("found", "together", "core", "not_mods", "intermittent", "no_repro", "aborted", "each", "unsure")
WHEN = ("start", "same", "equip", "time", "rare")
WHEN_TEXT = {"start": "게임을 켜거나 불러오는 중에 (타이틀·로딩 화면)", "same": "같은 장소·행동에서 매번",
             "equip": "특정 장비를 입거나 볼 때", "time": "한동안 하다 보면 (몇 분~몇십 분 뒤)",
             "rare": "가끔, 언제인지 모르겠어요"}
PLAY = {"start": "타이틀 화면을 지나 저장 데이터를 불러올 때까지 해 보세요.", "same": "튕기던 장소·행동을 다시 해 보세요.",
        "equip": "'{label}'을(를) 입거나 보는 등 튕기던 상황을 다시 해 보세요.", "time": "{minutes}분 넘게 해 보세요.",
        "rare": "평소처럼 오래 해 보세요. 오래 할수록 결과가 정확해집니다."}
REASON_TEXT = {"split": "나눠 보기", "full": "모두 켜기", "everything_off": "모두 끄기", "confirm_crash": "확인(튕기는지)",
               "confirm_fine": "확인(괜찮은지)", "others": "다른 원인 확인", "kind": "원인의 종류 확인",
               "dry": "다시 모두 켜기", "control": "모드 없이 확인"}
# a launch with every mod off before a mod is named (Params.none_controls; Params.controls of a v2.2.5 search)
CONTROL_TEXT = "모드 없이도 튕기는지 한 번 확인합니다. "
CONTROL_AGAIN = "모드 없이도 튕기는지 한 번 더 확인합니다. "  # (after launches with every mod off, all fine)
# (after launches with every mod off of which some crashed - off_crash_text: a crash there only raises "not the mods")
CONTROL_AFTER_CRASH = "{crashes} 모드 문제가 아닐 수도 있어 모드 없이 한 번 더 확인합니다. "
CONTROL_FINE = "모든 모드를 끈 실행 {fine}번은 괜찮았습니다."  # (a named result's 근거)
CONTROL_SOME = "모든 모드를 끈 실행 {count}번 중 {fine}번 괜찮았습니다."
CONTROL_NONE_FINE = "모든 모드를 끈 실행 {count}번 모두 튕겼습니다."
OFF_CRASHED = "모든 모드를 꺼도 튕겼습니다"  # (the line after such an answer: how "not the mods" moved)
# (a named result after a launch with every mod off crashed: what to look at if turning the mods off is not enough)
OFF_CRASH_NOTE = ("모든 모드를 끈 실행에서도 {count}번 튕겼습니다. 결과대로 모드를 끈 뒤에도 가끔 튕기면 모드 문제가 아닐 수 "
                  "있으니 Steam의 '게임 파일 무결성 확인', 그래픽 드라이버 업데이트, 오버레이·녹화 프로그램 끄기를 해 보세요.")
NAMED = ("found", "together", "each", "core")


class BisectError(Exception):
    pass


# ---- the search ------------------------------------------------------------------------------
@dataclass
class Step:
    """One launch of the search: the session's mods in ``on`` on, the rest of them off. A step of
    the v2 engine has ``remaining`` set (a format-1 step leaves it None and uses the old texts)."""
    number: int  # 1 for the first launch
    total: int  # about how many launches in all (it may still change; v2: number - 1 + remaining[1])
    kind: str  # see KINDS
    on: list[str]  # ids, in load order
    off: list[str]
    found: list[str] = field(default_factory=list)  # mods already shown (named) to be needed for the crash
    phase: str = "main"  # "core": inside REFramework and the mods others need (format 1)
    reason: str = ""  # bisect_engine.REASONS
    focus: list[str] = field(default_factory=list)  # the mods the text names (turned off, or the few turned on)
    ranking: list = field(default_factory=list)  # bisect_engine.Suspect, most likely first
    suspects: int = 0  # mods still suspected
    p: float = 0.0  # P of the top suspect
    remaining: tuple | None = None  # (lo, hi) launches from now on, this one included
    minutes: int = 0  # recommended play time (0: none)
    crash_minutes: float | None = None  # when the latest crash came (minutes; the search's own first), timed answers only
    play: str = ""  # what to do in the game (texts)
    off_suspects: int = 0  # suspected mods turned off in this step
    control: int = 0  # reason "control": the launches with every mod off before this one (0: the first)
    control_crashed: int = 0  # ... of them the ones that crashed

    @property
    def engine(self) -> bool:
        """A step of the v2 engine (not of a format-1 session)."""
        return self.remaining is not None


@dataclass
class Result:
    kind: str  # see RESULTS
    mods: list[str] = field(default_factory=list)  # ids: the cause, in load order
    runs: int = 0  # launches answered
    reason: str = ""  # why it was aborted
    p: float | None = None  # its probability (None: a format-1 result)
    pivots: int = 0  # independent pivot pairs of its weakest mod (capped at 3)
    can_confirm: bool = False  # 한 번 더 확인 can add one
    ranking: list = field(default_factory=list)  # bisect_engine.Suspect
    evidence: list[str] = field(default_factory=list)  # 근거 lines
    r_mean: float | None = None  # how reliably the crash came back (E[r])

    @property
    def can_disable(self) -> bool:
        """The result names mods the user may turn off in the original profile (an unsure one only
        when its top suspect has P >= 50%)."""
        return self.kind in ("found", "together", "core", "each", "unsure") and bool(self.mods)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "mods": list(self.mods), "runs": self.runs, "reason": self.reason, "p": self.p,
                "pivots": self.pivots, "can_confirm": self.can_confirm,
                "ranking": [_suspect_dict(s) for s in self.ranking], "evidence": list(self.evidence),
                "r_mean": self.r_mean}

    @classmethod
    def from_dict(cls, data) -> Result | None:
        if not isinstance(data, dict) or data.get("kind") not in RESULTS:
            return None
        mods = [m for m in data.get("mods") or [] if isinstance(m, str)]
        runs = data.get("runs") if isinstance(data.get("runs"), int) else 0
        p = float(data["p"]) if _num(data.get("p")) else None
        pivots = data.get("pivots") if isinstance(data.get("pivots"), int) and not isinstance(data.get("pivots"), bool) else 0
        ranking = [s for s in (_suspect_of(x) for x in data.get("ranking") or []) if s is not None]
        evidence = [str(x) for x in data.get("evidence") or [] if isinstance(x, str)]
        r_mean = float(data["r_mean"]) if _num(data.get("r_mean")) else None
        return cls(data["kind"], mods, runs, str(data.get("reason") or ""), p, pivots, data.get("can_confirm") is True,
                   ranking, evidence, r_mean)


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _suspect_dict(s: Suspect) -> dict:
    return {"family": s.family, "mods": list(s.mods), "p": float(s.p)}


def _suspect_of(data) -> Suspect | None:
    if not isinstance(data, dict) or data.get("family") not in engine_mod.FAMILIES or not _num(data.get("p")):
        return None
    return Suspect(data["family"], tuple(m for m in data.get("mods") or [] if isinstance(m, str)), float(data["p"]))


# (v2.2.2 search: only format-1 sessions) ----------------------------------------------------
class _Need(Exception):
    def __init__(self, on: frozenset, kind: str, left: int):
        super().__init__(kind)
        self.on, self.kind, self.left = on, kind, left


class _Done(Exception):
    def __init__(self, result: Result):
        super().__init__(result.kind)
        self.result = result


class _Search:
    """v2.2.2 search: only format-1 sessions. One replay of the search over the answers so far (``seen``: effective set -> what each launch
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
    """v2.2.2 search: only format-1 sessions. The next launch, or the result, of the search after ``answers`` ((ids on, crashed) of the
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
    """v2.2.2 search: only format-1 sessions. Run the search with answers from ``crashes_with`` (set of ids on -> crashed): the result and
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
    """v2.2.2 search: only format-1 sessions. About how many launches finding one mod takes (the most over where it may be: among the
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
    kinds: frozenset = frozenset()  # manager.mod_kinds: "loose", "pak", "script", "reframework", "dll", "other"
    hidden: bool = False  # every file loses to another mod: nothing of it reaches the game (manager.hidden_mods)


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


# ---- file conflicts: a mod whose files another overwrites needs that mod on -------------------------
def conflict_owners(manager) -> dict[str, list[str]]:
    """manager.conflicts().owners: file key -> enabled mod ids in load order (the last one wins). GUI thread."""
    return {k: list(v) for k, v in manager.conflicts().owners.items()}


def masking(owners: Mapping[str, Sequence[str]] | None, ids: Iterable[str],
            requires: Mapping[str, Sequence[str]] | None = None) -> dict[str, list[str]]:
    """Loser -> [winners] among ``ids`` (in their order): a unit some of whose files another unit
    overwrites is only turned on with that unit, so a step never loads a copy the user's setup never
    loads. Load order is a line and the last owner wins, so these edges alone have no cycles; an edge that
    would close one with ``requires`` (unit -> the units it needs) and the edges kept before it is left out:
    a patch that needs its base and overwrites some of its files would else always be on with it, and
    neither could ever be named. The base on with the patch off is what turning the patch off gives."""
    order = list(dict.fromkeys(ids))
    pos = {i: k for k, i in enumerate(order)}
    edges: dict[str, dict[str, bool]] = {}
    for owner_ids in (owners or {}).values():
        if not isinstance(owner_ids, (list, tuple)) or not owner_ids:
            continue
        winner = owner_ids[-1]
        if winner not in pos:
            continue
        for loser in owner_ids[:-1]:
            if loser in pos and loser != winner:
                edges.setdefault(loser, {})[winner] = True
    graph = {k: list(v) for k, v in (requires or {}).items() if isinstance(v, (list, tuple))}
    out: dict[str, list[str]] = {}
    for loser, won in sorted(edges.items(), key=lambda kv: pos[kv[0]]):
        kept = [w for w in sorted(won, key=pos.__getitem__) if loser not in _closure([w], graph)]
        if kept:
            out[loser] = kept
            graph[loser] = [*graph.get(loser, []), *kept]
    return out


def hidden_winners(owners: Mapping[str, Sequence[str]] | None, hidden: Iterable[str],
                   units: Sequence[str]) -> dict[str, list[str]]:
    """Hidden mod (every file loses to another mod) -> the units whose copies of its files the game reads. A
    hidden mod is on in a step only when all of them are, so its files never reach the game (they would when
    the mod covering them is off: a copy the user's setup never loads). Not known: every unit (on only when
    every mod is)."""
    pos = {u: k for k, u in enumerate(units)}
    out: dict[str, list[str]] = {}
    for h in dict.fromkeys(hidden):
        won: dict[str, bool] = {}
        for ids in (owners or {}).values():
            if isinstance(ids, (list, tuple)) and ids and h in ids and ids[-1] != h:
                won[ids[-1]] = True
        winners = [w for w in won if w in pos]
        if not won or len(winners) != len(won):
            winners = list(units)
        out[h] = sorted(winners, key=pos.__getitem__)
    return out


def last_good_obs(last_good: dict | None, mods: Sequence[ModFacts], units: Sequence[str],
                  game_version: str = "") -> Obs | None:
    """The last normal exit as an observation: the units on then with the same install and options
    are on, those changed since are unknown, the rest off (None without one, or with nothing on)."""
    if not isinstance(last_good, dict) or not isinstance(last_good.get("context"), dict):
        return None
    context = last_good["context"]
    then = {m["id"]: m for m in context.get("mods") or [] if isinstance(m, dict) and isinstance(m.get("id"), str)
            and m.get("enabled")}
    placed = context.get("placed")
    if isinstance(placed, list):  # (a mod turned on but not applied then was not in the game)
        inside = {str(p) for p in placed}
        then = {i: m for i, m in then.items() if i in inside}
    known = {m.id: m for m in mods}
    on, unknown = [], []
    for u in units:
        old, now = then.get(u), known.get(u)
        if old is None or now is None:
            continue
        same = not isinstance(old.get("stamp"), str) or old["stamp"] == now.stamp
        if isinstance(old.get("options"), list) and sorted(map(str, old["options"])) != sorted(now.options):
            same = False
        (on if same else unknown).append(u)
    if not on:
        return None
    old_game = context.get("game_version") if isinstance(context.get("game_version"), str) else ""
    updated = bool(old_game and game_version and old_game != game_version)
    seconds = float(last_good["seconds"]) if _num(last_good.get("seconds")) and last_good["seconds"] >= 0 else None
    return Obs(frozenset(on), False, rho=0.15 if updated else 0.8, seconds=seconds, e=0.02, unknown=frozenset(unknown),
               source="last_good")


def _evidence_obs(item, units: Sequence[str], content: Sequence[str],
                  cover: Mapping[str, Sequence[str]] | None = None) -> Obs | None:
    """One of Evidence.passive (a dict, or an Obs) on the search's units. A crash with a mod on that the search
    does not have now (removed, turned off, without files: it may have been that mod) is left out - except a
    hidden mod whose files lost then too (``cover``: hidden_winners) - a fine run drops such mods safely."""
    obs = item if isinstance(item, Obs) else Obs.from_dict(item)
    if obs is None or obs.source not in ("report", "history"):
        return None
    inside, content_set = set(units), set(content)
    if obs.crashed:
        cover = cover or {}
        for i in obs.on | obs.unknown:
            if i not in inside and not (i in cover and all(w in obs.on for w in cover[i])):
                return None
    unknown = frozenset(i for i in obs.unknown if i in inside)
    on = frozenset(i for i in obs.on if i in inside and i not in unknown)
    unexposed = frozenset(i for i in obs.unexposed if i in on and i in content_set)
    return Obs(on, obs.crashed, obs.rho, obs.seconds, obs.e, unknown, unexposed, obs.source)


def _initial_times(times: Sequence[float], passive: Sequence[Obs], when: str, minutes,
                   typed: bool = False) -> tuple[list[float], bool]:
    """The crash times the engine starts with (the seconds of crashed observations are added by the
    engine itself, so they are taken out once here), and whether they are only a default. ``typed``: the user
    gave the minutes for "time" (then they replace the records' times: Params.passive_times)."""
    if when == "time" and typed and _num(minutes) and minutes > 0:
        return [60.0 * float(minutes)], True
    out = [float(t) for t in times if _num(t) and t > 0]
    seen = [o.seconds for o in passive if o.crashed and o.seconds is not None]
    for s in seen:
        j = next((k for k, t in enumerate(out) if abs(t - s) < 0.5), None)
        if j is not None:
            out.pop(j)
    if out or seen:
        return out, False
    if when == "start":
        return [90.0], True
    if when == "time" and _num(minutes) and minutes > 0:
        return [60.0 * float(minutes)], True
    return [], False


# ---- the plan shown before starting ----------------------------------------------------------------
@dataclass
class Plan:
    """The search as it would start now (the start dialog)."""
    units: list[str]  # the mods turned on and off, in load order (format 1: the suspects, in search order)
    base: list[str]  # REFramework and the mods other mods need (format 1: always on)
    core: list[str]  # base, requirements first
    requires: dict[str, list[str]]  # unit -> the units it needs (requirements)
    split: int | None  # format 1: units unchanged since the last normal exit (tested first)
    names: dict[str, str]
    order: list[str]  # the session's mods in load order
    excluded: list[str] = field(default_factory=list)  # ids of enabled mods with nothing to apply
    unmanaged: list[str] = field(default_factory=list)  # game paths (the first ones)
    unmanaged_total: int = 0
    changes: Changes | None = None
    game_version: str = ""
    profile: str = ""
    runs: int = 0  # about how many launches (the estimate's upper end)
    runs_all: int = 0  # format 1: with split, when the cause is any mod
    refw_backup: bool = False  # a dinput8.dll placed by hand comes back when the REFramework mod is off
    revision: int | None = None  # manager.revision the plan was made at (start() refuses a stale plan)
    hidden: list[str] = field(default_factory=list)  # enabled mods whose files all lose to others (left out)
    masking: dict[str, list[str]] = field(default_factory=dict)  # loser -> winners (file conflicts)
    content: list[str] = field(default_factory=list)  # units with only loose files and PAKs
    evidence: object | None = None  # the evidence.Evidence it was made with
    prior: Prior = field(default_factory=Prior)
    passive: list[Obs] = field(default_factory=list)  # report, last normal exit, older launches
    crash_times: list[float] = field(default_factory=list)  # seconds from start to crash (the engine's first ones)
    when: str = "same"  # 언제 튕기나요 (WHEN)
    minutes: float | None = None  # with "time": about when (minutes)
    estimate: tuple = (0, 0)  # (lo, hi) launches
    estimate_top: int | None = None  # launches when the top suspect is the cause (when it stands out)
    estimate_other: tuple | None = None  # (lo, hi) launches when it is not
    ranking: list = field(default_factory=list)  # bisect_engine.Suspect
    involvement: dict[str, float] = field(default_factory=dict)
    first: object | None = None  # the first Step (or Result), computed with the plan
    singles: list = field(default_factory=list)  # (unit, P(only it)) most likely first
    families: dict[str, float] = field(default_factory=dict)  # P of each family of causes
    data: dict = field(default_factory=dict, repr=False)  # what a session starts with (Session.new)

    @property
    def count(self) -> int:
        """Enabled mods taking part."""
        return len(dict.fromkeys([*self.units, *self.base]))

    @property
    def problem(self) -> str:
        """Why the search cannot start ("" when it can)."""
        if not self.units and not self.base:
            return "켜져 있는 모드가 없어 원인 모드를 찾을 수 없습니다."
        return ""

    @property
    def v2(self) -> bool:
        return bool(self.data)

    @property
    def uniform(self) -> bool:
        """No evidence at all: every mod is suspected the same at the start."""
        return self.prior.empty and all(o.source == "report" for o in self.passive) and \
            not any(o.unknown or o.unexposed or o.on != frozenset(self.units) or o.seconds is not None
                    for o in self.passive)

    @property
    def informative(self) -> bool:
        """Something points at particular mods or launches (evidence.Evidence.informative when it has it:
        the kind of each mod alone is not)."""
        if self.uniform:
            return False
        flag = getattr(self.evidence, "informative", None)
        return flag if isinstance(flag, bool) else True

    @property
    def dominant(self) -> str | None:
        """The top single mod when it has P >= 50%."""
        return self.singles[0][0] if self.singles and self.singles[0][1] >= 0.5 else None

    def intro(self) -> str:
        if not self.v2:
            runs = f"약 {self.runs}번"
            if self.split and self.runs_all > self.runs:
                runs = f"마지막 정상 종료 뒤로 바뀐 모드가 원인이면 약 {self.runs}번, 아니면 약 {self.runs_all}번"
            return INTRO.format(count=self.count, runs=runs)
        lo, hi = self.estimate
        runs = launches_text(lo, hi)
        other = self.estimate_other
        if self.estimate_top is not None and other is not None and self.estimate_top < other[0]:
            runs = f"가장 의심되는 모드가 원인이면 약 {self.estimate_top}번, 아니면 {launches_text(*other)}"
        return INTRO_V2.format(count=self.count, runs=runs)

    def lines(self) -> list[str]:
        """Details under the introduction."""
        out = []
        if self.base:
            names = ", ".join(self.names.get(i, i) for i in self.base)
            out.append(f"다른 모드에 필요한 모드(그 모드를 켤 때 함께 켬): {names}" if self.v2 else f"계속 켜 둘 모드: {names}")
        if self.excluded:
            out.append("옵션을 고르지 않아 적용되지 않는 모드(빼고 찾음): "
                       + ", ".join(self.names.get(i, i) for i in self.excluded))
        if self.hidden:
            out.append("다른 모드에 파일이 모두 밀려 게임에 들어가지 않는 모드(빼고 찾음, 덮어쓰는 모드를 끌 때 함께 끔): "
                       + ", ".join(self.names.get(i, i) for i in self.hidden))
        pairs = sum(len(v) for v in self.masking.values())
        if pairs:
            out.append(f"한 모드가 다른 모드의 파일을 덮어쓰는 경우, 덮어쓰는 모드를 끌 때 덮이는 모드도 함께 끕니다 ({pairs}쌍).")
        if self.unmanaged_total:
            shown = ", ".join(self.unmanaged[:3]) + (" 등" if self.unmanaged_total > 3 else "")
            out.append(f"매니저가 넣지 않은 파일 {self.unmanaged_total}개는 켜고 끌 수 없어 그대로 둡니다: {shown}")
        if self.refw_backup:
            out.append("REFramework 모드를 끄면 직접 넣어 두었던 dinput8.dll이 되돌아와, 모든 모드를 끈 단계에서도 "
                       "그 파일(REFramework 등)은 그대로 남습니다.")
        if self.changes is not None:
            text = self.changes.text(self.names)
            if self.v2:
                used = any(o.source == "last_good" for o in self.passive)
                out.append(text + (" → 그때 그대로인 모드는 덜 의심합니다." if used and self.changes.changed else ""))
            else:
                out.append(text + (" → 바뀐 모드부터 확인합니다." if self.split else ""))
        return out

    def ranking_rows(self, names: Mapping[str, str] | None = None) -> list[tuple[str, float, list[str]]]:
        """(label, P, 근거 lines) of the most suspected single mods (P >= 3%, at most 5), for the start window."""
        names = self.names if names is None else names
        reasons = self.data.get("reasons") if isinstance(self.data.get("reasons"), dict) else {}
        out = []
        for unit, p in self.singles[:5]:
            if p < 0.03:
                break
            why = [str(x) for x in reasons.get(unit) or []][:3]
            out.append((suspect_label(Suspect("one", (unit,), p), names), p, why))
        return out

    def ranking_note(self, names: Mapping[str, str] | None = None) -> str:
        """The line after the rows: the rest summed up, or (no evidence at all) that every mod is
        suspected the same."""
        if self.uniform:
            return NO_EVIDENCE
        if not self.informative:
            return NO_EVIDENCE_KINDS
        shown = {u for u, _p in self.singles[:5] if _p >= 0.03}
        others = [(u, p) for u, p in self.singles if u not in shown]
        fam = self.families
        parts = []
        if others:
            parts.append(f"나머지 모드 {len(others)}개 합쳐서 약 {percent(sum(p for _u, p in others))}")
        parts.append(f"두 모드 이상이 함께 원인 약 {percent(fam.get('and', 0) + fam.get('and3', 0) + fam.get('or', 0))}")
        parts.append(f"모드 문제가 아님 약 {percent(fam.get('none', 0))}")
        return "그 밖: " + " · ".join(parts)


def facts(manager, owners: dict | None = None) -> list[ModFacts]:
    """ModFacts of every installed mod of a manager.ModManager (GUI thread). ``owners``: conflict_owners()."""
    from .manager import Conflicts

    refw = manager.refw_mod()
    try:
        conflicts = Conflicts(owners, {}) if owners is not None else manager.conflicts()
        hidden = {m.id for m in manager.hidden_mods(conflicts)}
    except Exception as exc:  # only a help: then every mod takes part
        log.debug("파일이 밀린 모드를 확인하지 못했습니다: %s", exc)
        hidden = set()
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
            options=[o["name"] for o in m.options if o["id"] in m.selected], kinds=frozenset(kinds),
            hidden=m.id in hidden))
    return out


def _clean_evidence(evidence) -> dict:
    """The attributes of an evidence.Evidence (read with defaults: any object, or None)."""
    def get(name, default, kind):
        value = getattr(evidence, name, default) if evidence is not None else default
        return value if isinstance(value, kind) else default

    reasons = {k: [str(x) for x in v][:3] for k, v in get("reasons", {}, dict).items()
               if isinstance(k, str) and isinstance(v, (list, tuple))}
    pairs = []
    for item in get("pairs", [], (list, tuple)):
        if isinstance(item, (list, tuple)) and len(item) == 3 and isinstance(item[0], str) and isinstance(item[1], str) \
                and _num(item[2]):
            pairs.append((item[0], item[1], float(item[2])))
    equipment = []
    for item in get("equipment", [], (list, tuple)):
        if isinstance(item, (list, tuple)) and len(item) == 3 and isinstance(item[0], str) and isinstance(item[1], str) \
                and isinstance(item[2], (list, tuple)):
            equipment.append((item[0], item[1], [i for i in item[2] if isinstance(i, str)]))
    minutes = get("minutes_hint", None, (int, float))

    def scale(name) -> float:
        value = get(name, 1.0, (int, float))
        return float(value) if _num(value) and value > 0 else 1.0

    return {"weights": {k: float(v) for k, v in get("weights", {}, dict).items() if isinstance(k, str) and _num(v)},
            "shares": {k: float(v) for k, v in get("shares", {}, dict).items() if isinstance(k, str) and _num(v)},
            "pairs": pairs, "none_scale": scale("none_scale"), "or_scale": scale("or_scale"),
            "passive": list(get("passive", [], (list, tuple))),
            "crash_times": [float(t) for t in get("crash_times", [], (list, tuple)) if _num(t) and t > 0],
            "reasons": reasons, "none_reasons": [str(x) for x in get("none_reasons", [], (list, tuple))][:3],
            "when_hint": get("when_hint", "", str), "minutes_hint": float(minutes) if _num(minutes) and minutes > 0 else None,
            "equipment": equipment, "record": get("record", "", str), "record_title": get("record_title", "", str),
            "notes": [str(x) for x in get("notes", [], (list, tuple))]}


def make_plan(mods: Sequence[ModFacts], last_good: dict | None = None, game_version: str = "",
              unmanaged: tuple[list[str], int] = ((), 0), profile: str = "", refw_backup: bool = False,
              revision: int | None = None, *, owners: Mapping[str, Sequence[str]] | None = None, evidence=None,
              when: str | None = None, minutes: float | None = None) -> Plan:
    """The search over the enabled mods, with its first step (pure, any thread: see :func:`prepare`
    for a manager). ``owners``: conflict_owners(); ``evidence``: an evidence.Evidence (or None)."""
    requires = requirement_ids(mods)
    active = [m for m in mods if m.enabled and m.has_files]
    excluded = [m.id for m in mods if m.enabled and not m.has_files]
    hidden = [m.id for m in active if m.hidden]
    units = [m.id for m in active if not m.hidden]
    inside = set(units)
    refw = next((m.id for m in active if m.refw and m.id in inside), None)
    req = {i: [r for r in requires.get(i, []) if r in inside] for i in units}
    kept = _closure(units, req) & inside
    if refw is not None:
        kept.add(refw)
    base = [i for i in units if i in kept]
    masks = masking(owners, units, req)
    content = [m.id for m in active if m.id in inside and m.kinds and set(m.kinds) <= {"loose", "pak"}]
    cover = hidden_winners(owners, hidden, units)
    ev = _clean_evidence(evidence)
    when = when if when in WHEN else ev["when_hint"] if ev["when_hint"] in WHEN else "same"
    typed = _num(minutes) and minutes > 0
    minutes = float(minutes) if typed else ev["minutes_hint"]
    items = []  # (observation, label) from the evidence
    for x in ev["passive"]:
        o = _evidence_obs(x, units, content, cover)
        if o is not None:
            items.append((o, x.get("label") if isinstance(x, dict) and isinstance(x.get("label"), str) else ""))
    labelled = [(o, label) for o, label in items if o.source == "report"][:1]
    if not labelled:
        labelled = [(Obs(frozenset(units), True, rho=1.0, e=0.02, source="report"), "")]
    good = last_good_obs(last_good, mods, units, game_version)
    if good is not None:
        when_good = last_good.get("time") if _num(last_good.get("time")) else None
        labelled.append((good, f"마지막 정상 종료 {_when(when_good)}" if when_good else "마지막 정상 종료"))
    labelled += [(o, label) for o, label in items if o.source == "history"]
    passive = [o for o, _label in labelled]
    data = {
        "units": list(units), "base": list(base), "requires": {k: list(v) for k, v in req.items()},
        "masking": masks, "content": content, "hidden": list(hidden), "hidden_winners": cover,
        "prior": Prior(ev["weights"], ev["shares"], ev["pairs"], ev["none_scale"], ev["or_scale"]).to_dict(),
        "passive": [dict(o.to_dict(units), label=label) for o, label in labelled],
        "evidence_times": list(ev["crash_times"]),
        "reasons": ev["reasons"], "none_reasons": ev["none_reasons"], "evidence_notes": ev["notes"],
        "equipment": [[k, label, list(ids)] for k, label, ids in ev["equipment"]], "names": {m.id: m.name for m in mods},
        "when": when, "minutes": minutes, "minutes_typed": bool(typed), "equip": "", "equip_label": "", "extra": 0,
        "strict": False,
        "record_title": ev["record_title"]}
    _settle_data(data)
    changes = changes_since(last_good, mods, game_version)
    core = dependency_order(base, req, refw)
    plan = Plan(units, base, core, req, None, {m.id: m.name for m in mods}, list(units), excluded,
                list(unmanaged[0]), int(unmanaged[1]), changes, game_version, profile, refw_backup=refw_backup,
                revision=revision, hidden=hidden, masking=masks, content=content, evidence=evidence,
                prior=Prior.from_dict(data["prior"]), passive=passive, crash_times=list(data["crash_times"]),
                when=when, minutes=minutes, data=data)
    view = _View(data)
    eng = view.engine([])
    decision = eng.decide(0)
    plan.first = view.next_of(decision, eng, [])
    plan.ranking, plan.involvement = list(decision.ranking), dict(decision.involvement)
    plan.singles, plan.families = _breakdown(eng)
    plan.estimate = tuple(decision.remaining) if decision.kind == "test" else (0, 0)
    plan.runs = plan.estimate[1]
    plan.estimate_top = _estimate_top(view, plan) if decision.kind == "test" else None
    if plan.estimate_top is not None:
        plan.estimate_other = eng.estimate(without=plan.singles[0][0])
    return plan


def _settle_data(data: dict) -> None:
    """The parameters and first crash times that follow from ``when`` and ``minutes``: the rules of a search started
    now (Params.current: launches with every mod off before a mod is named while "not the mods" keeps some
    probability, whatever the record - a graphics error record is a crash like any other, its reproduction prior the
    user's 언제 튕기나요). A search started earlier keeps the parameters it stored (v2.2.5: two control launches after
    a graphics error record; before this release: none)."""
    when = data.get("when") if data.get("when") in WHEN else "same"
    typed = data.get("minutes_typed") is True and when == "time"
    params = Params.current(r_prior=engine_mod.R_PRIORS[engine_mod.WHEN_PRIOR.get(when, "middle")],
                            timed_fine=when in ("time", "rare"), passive_times=not typed,
                            **engine_mod.WHEN_RULES.get(when, {}))
    data["params"] = params.to_dict()
    data["r_prior"] = list(params.r_prior)
    passive = [o for o in (Obs.from_dict(x) for x in data.get("passive") or []) if o is not None]
    times, default = _initial_times(data.get("evidence_times") or [], passive, when, data.get("minutes"), typed)
    data["crash_times"] = times
    data["times_default"] = default


def _breakdown(eng: Engine) -> tuple[list[tuple[str, float]], dict[str, float]]:
    """(unit, P(only it)) most likely first, and P of each family, from the last posterior."""
    ph = eng.last_ph
    s, end = eng.ranges[engine_mod.ONE]
    singles = sorted(((eng.units[h - s], ph.get(h, 0.0)) for h in range(s, end)), key=lambda x: (-x[1], eng.index[x[0]]))
    families = {}
    for code, name in enumerate(engine_mod.FAMILIES):
        a, b = eng.ranges[code]
        families[name] = sum(ph.get(h, 0.0) for h in range(a, b))
    return singles, families


def _estimate_top(view: "_View", plan: Plan) -> int | None:
    """Launches until the result when the top single mod is the cause (it has P >= 30%), simulated
    with exact answers."""
    if not plan.singles or plan.singles[0][1] < 0.3:
        return None
    culprit = plan.singles[0][0]
    eng = view.engine([])
    for k in range(TOP_SIMULATED):
        d = eng.decide(0)
        if d.kind != "test":
            return k if d.kind in NAMED and culprit in d.mods else None
        eng.add(Obs(d.on, culprit in d.on))
    return None


def prepare(manager, last_good: dict | None = None, scan: bool = True, evidence=None,
            owners: dict | None = None) -> Plan:
    """The plan for a manager.ModManager: its enabled mods, the last normal exit (gamewatch) and the
    files it did not place (``scan``). GUI thread; reads the game folder a little. ``owners``:
    conflict_owners() when the caller has it (the evidence needs it too)."""
    from . import gamewatch

    if last_good is None:
        last_good = gamewatch.load_last_good(manager.crash_dir)
    if owners is None:
        owners = conflict_owners(manager)
    mods, revision = facts(manager, owners), manager.revision
    backups = manager.deployed_backups()
    unmanaged: tuple[list[str], int] = ([], 0)
    if scan and manager.game_dir:
        try:
            unmanaged = unmanaged_files(manager.game_dir, manager.deployed_files(), backups=backups)
        except Exception as exc:  # only a list for the user
            log.warning("매니저가 넣지 않은 파일을 찾지 못했습니다: %s", exc)
    return make_plan(mods, last_good, manager.game_version(refresh=True), unmanaged, manager.profile,
                     backed_up_refw(backups), revision, owners=owners, evidence=evidence)


# ---- a v2 search's data: the engine, its steps and results ---------------------------------------------
class _View:
    """What a format-2 search starts with (``data``: Plan.data, or a session's bisect.json) and how
    its engine's decisions become steps and results."""

    def __init__(self, data: dict):
        self.data = data

    def ids(self, key: str) -> list[str]:
        return _ids(self.data.get(key))

    @property
    def names(self) -> dict[str, str]:
        names = self.data.get("names")
        return names if isinstance(names, dict) else {}

    @property
    def requires(self) -> dict[str, list[str]]:
        req = self.data.get("requires")
        return {k: _ids(v) for k, v in req.items() if isinstance(k, str)} if isinstance(req, dict) else {}

    @property
    def requires_all(self) -> dict[str, list[str]]:
        """Requirements and masking edges together (what the engine closes sets under)."""
        out = {k: list(v) for k, v in self.requires.items()}
        mask = self.data.get("masking") if isinstance(self.data.get("masking"), dict) else {}
        for loser, winners in mask.items():
            if isinstance(loser, str):
                out[loser] = list(dict.fromkeys([*out.get(loser, []), *_ids(winners)]))
        return out

    @property
    def params(self) -> Params:
        return Params.from_dict(self.data.get("params"))

    @property
    def prior(self) -> Prior:
        return Prior.from_dict(self.data.get("prior"))

    @property
    def passive(self) -> list[Obs]:
        return [o for o in (Obs.from_dict(x) for x in self.data.get("passive") or []) if o is not None]

    @property
    def crash_times(self) -> list[float]:
        return [float(t) for t in self.data.get("crash_times") or [] if _num(t) and t > 0]

    @property
    def when(self) -> str:
        return self.data.get("when") if self.data.get("when") in WHEN else "same"

    @property
    def minutes(self) -> float | None:
        m = self.data.get("minutes")
        return float(m) if _num(m) and m > 0 else None

    @property
    def extra(self) -> int:
        x = self.data.get("extra")
        return x if isinstance(x, int) and not isinstance(x, bool) and x > 0 else 0

    @property
    def strict(self) -> bool:
        return self.data.get("strict") is True

    @property
    def reasons(self) -> dict[str, list[str]]:
        r = self.data.get("reasons")
        return {k: [str(x) for x in v] for k, v in r.items() if isinstance(k, str) and isinstance(v, list)} \
            if isinstance(r, dict) else {}

    def step_obs(self, s: dict, params: Params) -> Obs:
        """An answered step as an observation (the answer counts more when the manager saw the same)."""
        crashed = bool(s.get("crashed"))
        auto = s.get("auto")
        agrees = (auto == "crash" and crashed) or (auto == "normal" and not crashed)
        seconds = float(s["seconds"]) if _num(s.get("seconds")) and s["seconds"] >= 0 else None
        return Obs(frozenset(_ids(s.get("on"))), crashed, 1.0, seconds, params.e_agrees if agrees else None,
                   frozenset(), frozenset(_ids(s.get("unexposed"))), "search")

    def engine(self, steps: Sequence[dict]) -> Engine:
        params = self.params
        eng = Engine(self.ids("units"), self.requires_all, content=self.ids("content"), prior=self.prior,
                     params=params, crash_times=self.crash_times, strict=self.strict)
        for o in self.passive:
            eng.add(o)
        for s in steps:
            eng.add(self.step_obs(s, params))
        return eng

    # ---- decisions as steps and results
    def shape(self, on: Sequence[str]) -> tuple[str, list[str], list[str]]:
        """(kind, focus, off) of a step with ``on`` on (KINDS)."""
        units, base = self.ids("units"), self.ids("base")
        inside = set(on)
        off = [u for u in units if u not in inside]
        req = self.requires_all
        if not on:
            return "everything_off", [], off
        if not off:
            return "full", [], off
        if inside == set(base):
            return "base", [], off
        roots = [u for u in off if all(r in inside for r in req.get(u, []))]
        base_set = set(base)
        extra = [u for u in on if u not in base_set]
        needed = {r for u in extra for r in req.get(u, []) if r != u}
        tops = [u for u in extra if u not in needed]
        choices = []
        if len(roots) <= 3:
            choices.append((len(roots), 0, "off_some", roots))
        if tops and len(tops) <= 3:
            choices.append((len(tops), 1, "on_some", tops))
        if choices:
            _n, _k, kind, focus = min(choices, key=lambda c: (c[0], c[1]))
            return kind, focus, off
        return "split", [], off

    def play(self, minutes: int, crash_minutes: float | None) -> str:
        when = self.when
        if when == "time":
            m = minutes or int(math.ceil(self.minutes or 10))
            text = PLAY["time"].format(minutes=m)
        elif when == "equip":
            text = PLAY["equip"].format(label=str(self.data.get("equip_label") or "그 장비"))
        else:
            text = PLAY[when]
        if crash_minutes is not None and crash_minutes * 60 > 150:
            text += f" (지난번에는 실행 후 약 {max(1, round(crash_minutes))}분 만에 튕겼습니다)"
        return text

    def times(self, eng: Engine) -> tuple[int, float | None]:
        """(recommended play minutes or 0, when the latest crash came in minutes or None). Only when the play
        time counts (언제 튕기나요: 한동안 / 가끔, Params.timed_fine): at a place, an action, a piece of
        equipment or at start-up the user goes there, however long it takes."""
        if not eng.p.timed_fine:
            return 0, None
        all_times = list(eng.times)
        minutes = 0
        if all_times and statistics.median(all_times) > 150:
            minutes = int(math.ceil(2.2 * statistics.median(all_times) / 60 - 1e-9))
        crashes = [o.seconds for o in eng.obs if o.crashed and o.seconds is not None and o.source == "search"]
        latest = crashes[-1] if crashes else None
        if latest is None and eng.p.passive_times:  # (the record first, then the newest older crash)
            latest = next((o.seconds for o in eng.obs if o.crashed and o.seconds is not None
                           and o.source in ("report", "history")), None)
        if latest is None:
            real = eng.times0[1:] if self.data.get("times_default") else eng.times0
            latest = real[0] if real else None
        return minutes, latest / 60 if latest is not None else None

    def next_of(self, d: Decision, eng: Engine, steps: Sequence[dict]) -> Step | Result:
        if d.kind == "test":
            return self.step_of(d, eng, len(steps) + 1)
        return self.result_of(d, eng, steps)

    def step_of(self, d: Decision, eng: Engine, number: int) -> Step:
        units = self.ids("units")
        on = [u for u in units if u in d.on]
        kind, focus, off = self.shape(on)
        lo, hi = d.remaining if d.remaining != (0, 0) else (1, 1)
        minutes, crash_minutes = self.times(eng)
        suspected = {u for u, x in d.involvement.items() if units and x >= 0.2 / len(units)}
        named = list(d.mods) if d.reason in ("others", "kind") else []
        bare = [o for o in eng.obs if o.source == "search" and not o.on and not o.unknown] \
            if d.reason == "control" else []
        return Step(number, number - 1 + hi, kind, on, off, named, "main", d.reason, focus, list(d.ranking), d.suspects,
                    d.ranking[0].p if d.ranking else 0.0, (lo, hi), minutes, crash_minutes,
                    self.play(minutes, crash_minutes), sum(1 for u in off if u in suspected), len(bare),
                    sum(1 for o in bare if o.crashed))

    def result_of(self, d: Decision, eng: Engine, steps: Sequence[dict]) -> Result:
        base = set(self.ids("base"))
        kind = d.kind
        mods: list[str] = []
        units = self.ids("units")
        if kind in ("found", "together", "each"):
            mods = [u for u in units if u in d.mods]
            if mods and all(m in base for m in mods):
                kind = "core"
        elif kind == "unsure" and d.sure >= 0.5 and d.family not in ("none", "rest"):
            mods = [u for u in units if u in d.mods]
        p = d.p if kind != "unsure" or not mods else d.sure
        result = Result(kind, mods, len(steps), p=p, pivots=d.pivots, can_confirm=d.can_confirm and kind in NAMED,
                        ranking=list(d.ranking), r_mean=d.r_mean)
        result.evidence = self.evidence_lines(result, d, eng)
        return result

    def evidence_lines(self, result: Result, d: Decision, eng: Engine) -> list[str]:
        """The 근거 of a result: the search's launches (and the crash record) first, then what the earlier
        sessions (the record, the last normal exit, launches.json) showed, apart."""
        names = self.names
        search = [o for o in eng.obs if o.source == "search"]
        crashes = [o for o in eng.obs if o.crashed and o.source in engine_mod.CRASH_SOURCES]  # (the record and the search)
        earlier = [o for o in eng.obs if o.source != "search"]

        def label(i):
            return names.get(i, i)

        if result.kind == "unsure":
            return [f"{suspect_label(s, names)} 약 {percent(s.p)}" for s in result.ranking[:3]]
        bare = [o for o in search if not o.on and not o.unknown]  # (the search's launches with every mod off)
        bare_crashed = sum(1 for o in bare if o.crashed)
        if result.kind == "not_mods":
            if bare_crashed:  # (each of them, the fine ones too)
                out = [CONTROL_NONE_FINE.format(count=len(bare)) if bare_crashed == len(bare)
                       else f"모든 모드를 끈 실행 {len(bare)}번 중 {bare_crashed}번 튕겼습니다."]
            else:  # (the record's crash with every mod off)
                off = [o for o in crashes if not o.on and not o.unknown]
                out = [f"모든 모드를 끈 실행 {len(off)}번 모두 튕겼습니다." if off else "모든 모드를 끈 실행에서도 튕겼습니다."]
            out += [f"기록: {x}" for x in _ids(self.data.get("none_reasons"))[:2]]
            return out
        if result.kind == "no_repro":
            fulls = [o for o in search if o.on == eng.full and not o.crashed]
            return [f"처음처럼 모든 모드를 켠 실행 {len(fulls)}번 모두 괜찮았습니다."] if fulls else []
        out: list[str] = []
        mods = result.mods[:2]
        many = len(result.mods) > 1
        for x in mods:
            bit = eng.mask([x])
            head = f"'{label(x)}': " if many else ""
            with_on = [o for o in search if o.on & bit]
            crashed = [o for o in with_on if o.crashed]
            if with_on:
                fast = [o.seconds for o in crashed if o.seconds is not None]
                out.append(f"{head}원인 찾기에서 이 모드를 켠 실행 {len(with_on)}번 중 {len(crashed)}번 튕겼습니다"
                           + (f" (가장 빨리 실행 후 {_minutes(min(fast))}분)" if fast else "") + ".")
            with_off = [o for o in search if not o.on & bit]
            fine = [o for o in with_off if not o.crashed]
            if with_off:
                long = [o.seconds for o in fine if o.seconds is not None]
                text = (f"원인 찾기에서 이 모드를 끈 실행 {len(with_off)}번은 모두 괜찮았습니다" if len(fine) == len(with_off)
                        else f"원인 찾기에서 이 모드를 끈 실행 {len(with_off)}번 중 {len(fine)}번 괜찮았습니다")
                out.append(f"{head}{text}" + (f" (가장 길게 {_minutes(max(long))}분)" if long else "") + ".")
            seen_on = [o for o in earlier if o.crashed and o.on & bit and not o.unknown & bit]
            seen_off = [o for o in earlier if not o.crashed and not o.on & bit and not o.unknown & bit]
            if seen_on or seen_off:
                parts = ([f"이 모드를 켜고 튕김 {len(seen_on)}번"] if seen_on else []) + \
                        ([f"이 모드를 끄고 괜찮음 {len(seen_off)}번"] if seen_off else [])
                out.append(f"{head}원인 찾기 전의 기록(튕김 기록·이전 실행): " + " · ".join(parts) + ".")
        if result.kind in ("found", "core") and len(result.mods) == 1:
            alone = eng.mask(eng.close(result.mods))
            if any(o.on and not o.on & ~alone for o in crashes):
                out.append("이 모드(와 필요한 모드)만 켰을 때도 튕겼습니다.")
        if bare:
            bare_fine = len(bare) - bare_crashed
            out.append(CONTROL_FINE.format(fine=bare_fine) if not bare_crashed
                       else CONTROL_SOME.format(count=len(bare), fine=bare_fine) if bare_fine
                       else CONTROL_NONE_FINE.format(count=len(bare)))
        bits = [eng.mask([m]) for m in result.mods]
        if d.family in ("and", "and3") and len(bits) > 1:
            each_fine = all(any(not o.crashed and o.on & b and any(not o.on & c for c in bits if c != b) for o in search)
                            for b in bits)
            if each_fine:
                out.append("하나씩 켰을 때는 괜찮았고 함께 켰을 때만 튕겼습니다.")
        if d.family == "or" and len(bits) == 2:
            a, b = bits
            if any(o.on & a and not o.on & b for o in crashes) or any(o.on & b and not o.on & a for o in crashes):
                out.append("한쪽만 켜도 튕겼습니다.")
        reasons = self.reasons
        for x in mods:
            for line in reasons.get(x, [])[:2]:
                out.append(f"기록: {line}")
        named = (d.family, tuple(d.mods))
        others = [s for s in result.ranking if (s.family, tuple(s.mods)) != named][:2]
        if others:
            out.append("다른 가능성: " + " · ".join(f"{suspect_label(s, names)} {percent(s.p)}" for s in others))
        if result.can_confirm:
            out.append(CONFIRM_NOTE)
        return out


def _minutes(seconds: float) -> int:
    return max(1, round(seconds / 60))


# ---- the session (bisect.json) ----------------------------------------------------------------------
def _ids(value) -> list[str]:
    return [i for i in value if isinstance(i, str)] if isinstance(value, list) else []


def _exit_seconds(exit_info) -> float | None:
    """How long the game ran, from a gamewatch.GameExit.to_dict() (None if not known)."""
    if not isinstance(exit_info, dict) or not _num(exit_info.get("ended")):
        return None
    start = exit_info.get("started") if _num(exit_info.get("started")) else exit_info.get("seen")
    return max(0.0, float(exit_info["ended"]) - float(start)) if _num(start) else None


def _clean_run(run) -> dict:
    """The run dict of accessed.step_run (what is kept of it)."""
    if not isinstance(run, dict):
        return {}
    out = {"seconds": float(run["seconds"]) if _num(run.get("seconds")) and run["seconds"] >= 0 else None,
           "log": run.get("log") is True, "unexposed": _ids(list(run.get("unexposed") or [])),
           "tail": _ids(list(run.get("tail") or [])), "record": str(run.get("record") or ""),
           "auto": run.get("auto") if run.get("auto") in ("crash", "normal") else ""}
    return out


class Session:
    """A search in progress, kept in ``<data>/bisect.json``. Format 2 is the v2 engine's; a format-1
    search (v2.2.2/v2.2.3) goes on with the search it started with (``legacy``). Thread-safe: the GUI
    thread answers (add_answer, fast) and a worker computes what comes next (settle)."""

    def __init__(self, path: Path, data: dict):
        self.path = Path(path)
        self.data = data
        self._next: Step | Result | None = None
        self._lock = threading.RLock()
        self._engine: Engine | None = None
        self._engine_steps = -1  # answered steps the engine holds
        self._before: dict | None = None  # P of each cause before the last answer
        self._answered = False  # an answer was added since what comes next was last worked out
        self._change = ""
        self._removed = False  # finished: a worker still settling must not write bisect.json again

    @classmethod
    def new(cls, data_dir, plan: Plan, original: str, original_snapshot: dict, temp: str, record: str = "", *,
            when: str | None = None, minutes: float | None = None, equip: str | None = None,
            refw_restore: dict | None = None) -> Session:
        data = {"format": SESSION_FORMAT if plan.v2 else LEGACY_FORMAT, "started": time.time(),
                "original_profile": original, "original_snapshot": copy.deepcopy(original_snapshot),
                "temp_profile": temp, "game_version": plan.game_version, "record": record, "names": dict(plan.names),
                "order": list(plan.order), "units": list(plan.units), "base": list(plan.base),
                "core": list(plan.core), "requires": {k: list(v) for k, v in plan.requires.items()},
                "split": plan.split, "estimate": plan.runs, "excluded": list(plan.excluded),
                "unmanaged": {"total": plan.unmanaged_total, "paths": list(plan.unmanaged[:20])},
                "refw_backup": bool(plan.refw_backup),
                "changes": plan.changes.text(plan.names) if plan.changes is not None else "",
                "steps": [], "current": None, "result": None}
        same = True
        if plan.v2:
            core = copy.deepcopy(plan.data)
            if when in WHEN and when != core.get("when"):
                core["when"], same = when, False
            if _num(minutes) and minutes > 0 and minutes != core.get("minutes"):
                core["minutes"], core["minutes_typed"], same = float(minutes), True, False
            entry = next((e for e in core.get("equipment") or [] if isinstance(e, list) and e and e[0] == equip), None)
            if equip and entry is not None:
                _key, label, ids = entry
                prior = core["prior"]
                inside = set(core.get("units") or [])
                for i in ids:
                    if i in inside:
                        prior["weights"][i] = float(prior["weights"].get(i, 1.0)) * EQUIP_WEIGHT
                        reasons = core["reasons"].setdefault(i, [])
                        reasons.insert(0, f"고른 장비 '{label}'을(를) 바꾸는 모드입니다")
                        del reasons[3:]
                core["equip"], core["equip_label"], same = equip, label, False
            _settle_data(core)
            data.update(core)
            data.update({"engine": engine_mod.ENGINE_FORMAT, "hidden": list(plan.hidden),
                         "refw_restore": copy.deepcopy(refw_restore) if isinstance(refw_restore, dict) else None})
        session = cls(Path(data_dir) / SESSION_FILE, data)
        if plan.v2 and same and plan.first is not None:
            session._next = plan.first
            data["start_ranking"] = [[u, p] for u, p in plan.singles[:5] if p >= 0.03]
        return session

    @classmethod
    def load(cls, data_dir) -> Session | None:
        path = Path(data_dir) / SESSION_FILE
        data = fsutil.read_json(path, None)
        if not isinstance(data, dict) or data.get("format") not in (SESSION_FORMAT, LEGACY_FORMAT):
            if data is not None:
                log.warning("원인 찾기 기록을 읽지 못해 무시합니다: %s", path)
            return None
        if not isinstance(data.get("original_profile"), str) or not isinstance(data.get("temp_profile"), str):
            return None
        if data["format"] == SESSION_FORMAT and not isinstance(data.get("units"), list):
            log.warning("원인 찾기 기록을 읽지 못해 무시합니다: %s", path)
            return None
        data["steps"] = [s for s in data.get("steps") or [] if isinstance(s, dict) and isinstance(s.get("on"), list)
                         and isinstance(s.get("crashed"), bool)]
        if not isinstance(data.get("current"), dict):
            data["current"] = None
        if data["format"] == SESSION_FORMAT and data.get("engine") != engine_mod.ENGINE_FORMAT:
            log.info("다른 버전에서 시작한 원인 찾기를 그때의 설정으로 이어 갑니다 (엔진 형식 %s).", data.get("engine"))
        return cls(path, data)

    def save(self) -> None:
        with self._lock:
            if not self._removed:
                fsutil.atomic_write_json(self.path, self.data)

    def remove(self) -> None:
        """The search is over: delete bisect.json (after a worker's settle() running now, which then
        cannot write it again)."""
        with self._lock:
            self._removed = True
            fsutil.remove_file(self.path)

    # ---- what it holds
    @property
    def legacy(self) -> bool:
        """Started by v2.2.2/v2.2.3: the fixed halving search goes on."""
        return self.data.get("format") == LEGACY_FORMAT

    @property
    def _view(self) -> _View:
        return _View(self.data)

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
        if self.legacy:
            return list(dict.fromkeys([*_ids(self.data.get("base")), *_ids(self.data.get("units"))]))
        return _ids(self.data.get("units"))

    @property
    def content(self) -> list[str]:
        """Units with only loose files and PAKs (whose files the access log can show were never read)."""
        return _ids(self.data.get("content"))

    @property
    def steps(self) -> list[dict]:
        """The launches answered: {"on", "crashed", "kind", "number", "time", "exit", "detected"} (and with
        format 2 "reason", "focus", "seconds", "unexposed", "log", "record", "auto", "tail")."""
        return self.data["steps"]

    @property
    def current(self) -> dict | None:
        """The step applied now: {"on", "applied", "launched", "exit", "retries", "run"} (None: not applied yet)."""
        return self.data.get("current")

    @property
    def result(self) -> Result | None:
        return Result.from_dict(self.data.get("result"))

    @property
    def excluded(self) -> list[str]:
        return _ids(self.data.get("excluded"))

    @property
    def hidden(self) -> list[str]:
        return _ids(self.data.get("hidden"))

    @property
    def hidden_winners(self) -> dict[str, list[str]]:
        """Format 2: hidden mod -> the units it is on with (hidden_winners); {} for format 1 (left as they are)."""
        if self.legacy:
            return {}
        got = self.data.get("hidden_winners")
        units = _ids(self.data.get("units"))
        got = got if isinstance(got, dict) else {}
        return {h: (_ids(got[h]) if isinstance(got.get(h), list) else units) for h in self.hidden}

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

    @property
    def when(self) -> str:
        return self._view.when

    @property
    def minutes(self) -> float | None:
        return self._view.minutes

    @property
    def strict(self) -> bool:
        return self.data.get("strict") is True

    @property
    def extra(self) -> int:
        return self._view.extra

    @property
    def refw_restore(self) -> dict | None:
        """What accessed.enable_logging changed at the start (restored after finish), or None."""
        r = self.data.get("refw_restore")
        return r if isinstance(r, dict) else None

    @property
    def ready(self) -> bool:
        """What comes next is known (next() returns at once)."""
        return self.legacy or self._next is not None or self.data.get("result") is not None

    def _engine_now(self) -> Engine:
        with self._lock:
            if self._engine is None or self._engine_steps != len(self.steps):
                self._engine = self._view.engine(self.steps)
                self._engine_steps = len(self.steps)
            self._engine.strict = self.strict
            return self._engine

    def next(self) -> Step | Result:
        """The step to run now, or the result (a stored result wins: an aborted search). Format 2:
        the engine's decision (cached; may take a moment the first time: see ``ready``)."""
        with self._lock:
            stored = self.result
            if stored is not None:
                return stored
            if self._next is None:
                d = self.data
                if self.legacy:
                    split = d.get("split") if isinstance(d.get("split"), int) else None
                    self._next = next_step(_ids(d.get("units")), _ids(d.get("base")), _ids(d.get("core")),
                                           self.requires, split, [(s["on"], s["crashed"]) for s in self.steps],
                                           _ids(d.get("order")),
                                           d.get("estimate") if isinstance(d.get("estimate"), int) else 0)
                else:
                    eng = self._engine_now()
                    decision = eng.decide(self.extra)
                    self._next = self._view.next_of(decision, eng, self.steps)
                    if not self.steps and "start_ranking" not in d:
                        d["start_ranking"] = [[u, p] for u, p in _breakdown(eng)[0][:5] if p >= 0.03]
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
        """The current step's game was started and not answered for yet (never computes what comes next:
        a step applied and not answered is what next() gives, unless a result was stored meanwhile)."""
        if not (self.current and self.current.get("launched")) or self.data.get("result") is not None:
            return False
        return isinstance(self.next(), Step) if self.ready else True

    # ---- changes
    def mark_applied(self, step: Step) -> None:
        with self._lock:
            cur = self.current
            if cur is None or sorted(cur.get("on") or []) != sorted(step.on):
                self.data["current"] = {"on": list(step.on), "applied": time.time(), "launched": None, "exit": None,
                                        "retries": 0, "run": None}
            else:
                cur["applied"] = time.time()
            self.save()

    def mark_launched(self, when: float | None = None) -> None:
        """The game was started for the current step (before it, the crash records are collected)."""
        with self._lock:
            if self.current is not None:
                self.current["launched"] = when if when is not None else time.time()
                self.current["exit"] = None
                self.current["run"] = None
                self.save()

    def note_exit(self, exit_info: dict | None) -> None:
        """How the game of the current step ended (gamewatch.GameExit.to_dict()), kept for a restart."""
        with self._lock:
            if self.current is not None:
                self.current["exit"] = exit_info
                self.save()

    def note_run(self, run: dict | None) -> None:
        """What accessed.step_run found of the current step's game (kept for a restart, like note_exit)."""
        with self._lock:
            if self.current is not None:
                self.current["run"] = _clean_run(run) if isinstance(run, dict) else None
                self.save()

    def add_answer(self, crashed: bool | None, detected: str = "", run: dict | None = None) -> None:
        """The user's answer for the current step: True (튕겼어요), False (괜찮았어요) or None (잘
        모르겠어요: the same step again). Fast: what comes next is computed by settle() (or next())."""
        with self._lock:
            step = self.next()
            if not isinstance(step, Step):
                return
            cur = self.current or {}
            self._change = ""
            if crashed is None:
                if self.current is not None:
                    self.current["retries"] = int(cur.get("retries") or 0) + 1
                    self.current["launched"] = None
                    self.current["exit"] = None
                    self.current["run"] = None
                self.save()
                return
            entry = {"on": list(step.on), "crashed": bool(crashed), "kind": step.kind, "number": step.number,
                     "time": time.time(), "launched": cur.get("launched"), "exit": cur.get("exit"),
                     "detected": detected}
            if not self.legacy:
                info = _clean_run(run) if isinstance(run, dict) else _clean_run(cur.get("run"))
                seconds = info.get("seconds")
                if seconds is None:
                    seconds = _exit_seconds(cur.get("exit"))
                auto = info.get("auto") or ("crash" if detected and crashed else "")
                content, on = set(self.content), set(step.on)
                entry.update({"reason": step.reason, "focus": list(step.focus), "seconds": seconds,
                              "unexposed": [u for u in info.get("unexposed", []) if u in content and u in on],
                              "log": bool(info.get("log")), "record": info.get("record", ""), "auto": auto,
                              "tail": list(info.get("tail", []))})
            self.steps.append(entry)
            self.data["current"] = None
            self._next = None
            self._before = None
            self._answered = True
            if not self.legacy and self._engine is not None and self._engine_steps == len(self.steps) - 1:
                self._before = self._engine.last_ph
                self._engine.add(self._view.step_obs(entry, self._engine.p))
                self._engine_steps += 1
            self.save()

    def settle(self) -> Step | Result:
        """What comes after the last answer (a result is stored); call from a worker after add_answer()."""
        with self._lock:
            nxt = self.next()
            if isinstance(nxt, Result) and self.data.get("result") is None:
                self.data["result"] = nxt.to_dict()
                log.info("원인 찾기 결과: %s", result_title(nxt, self.names))
                self.save()
            if not self.legacy:
                self._change = self._last_change(nxt)
            return nxt

    def record(self, crashed: bool | None, detected: str = "", run: dict | None = None) -> Step | Result:
        """add_answer() and settle(): returns what comes next (not applied yet)."""
        with self._lock:
            self.add_answer(crashed, detected, run)
            return self.settle()

    def abort(self, reason: str) -> Result:
        with self._lock:
            result = Result("aborted", runs=len(self.steps), reason=reason)
            self.data["result"] = result.to_dict()
            self._next = None
            self.save()
            return result

    def more(self) -> Step | Result:
        """계속 찾기 (unsure: 6 more launches) or 한 번 더 확인 (a result that can add a pivot pair:
        three are needed from now on). Clears the result and settles (a worker's job)."""
        with self._lock:
            result = self.result
            if result is None or self.legacy:
                return self.next()
            if result.kind == "unsure":
                self.data["extra"] = self.extra + MORE_LAUNCHES
            elif result.kind in NAMED and result.can_confirm:
                self.data["strict"] = True
                eng = self._engine_now()
                want = max(CONFIRM_LAUNCHES, eng.confirm_launches())
                left = engine_mod.budget(len(self.mod_ids), self.extra, eng.p) - len(self.steps)
                if left < want:  # (the pairs it adds may take a few launches: never "unsure" for want of them)
                    self.data["extra"] = self.extra + want - left
            else:
                return result
            self.data["result"] = None
            self._next = None
            self._change = ""
            log.info("원인 찾기를 이어 갑니다 (%s).", RESULT_MORE if result.kind == "unsure" else RESULT_CONFIRM)
            nxt = self.settle()
            self.save()
            return nxt

    def interim(self) -> Result:
        """The best guess so far as an unsure Result (not stored), for 그만두기."""
        with self._lock:
            if self.legacy:
                return Result("unsure", runs=len(self.steps))
            eng = self._engine_now()
            s = eng.summary()
            mods = [u for u in _ids(self.data.get("units")) if u in s.mods] \
                if s.sure >= 0.5 and s.family not in ("none", "rest") else []
            result = Result("unsure", mods, len(self.steps), p=s.sure if mods else s.p, ranking=list(s.ranking),
                            r_mean=s.r_mean)
            result.evidence = [f"{suspect_label(x, self.names)} 약 {percent(x.p)}" for x in s.ranking[:3]]
            return result

    def interim_note(self) -> str:
        """For 그만두기 before a result: " 지금까지 가장 의심되는 모드는 'X'(약 45%)입니다." ("" for a format-1
        search or when nothing stands out yet)."""
        if self.legacy:
            return ""
        guess = self.interim()
        top = guess.ranking[0] if guess.ranking else None
        if top is None or top.p < 0.01:
            return ""
        tied = tied_top(guess.ranking)
        if len(tied) > 1:  # (none of them stands out: naming the first would blame it for its place in the list)
            if len(tied) <= 3 and all(s.mods and s.family not in ("none", "rest") for s in tied):
                return (f" 지금까지 가장 의심되는 것은 {', '.join(suspect_label(s, self.names) for s in tied)}"
                        f"(각각 약 {percent(top.p)})입니다.")
            return f" 아직 두드러지게 의심되는 모드는 없습니다({tie_text(tied, guess.ranking)})."
        if top.family == "none":
            return f" 지금까지는 모드 문제가 아닐 가능성이 가장 높습니다(약 {percent(top.p)})."
        if top.family == "rest" or not top.mods:
            return ""
        return f" 지금까지 가장 의심되는 모드는 {suspect_label(top, self.names)}(약 {percent(top.p)})입니다."

    def last_change(self) -> str:
        """After an answer: how the top suspect's probability moved ("" if not known)."""
        return self._change

    def _last_change(self, nxt) -> str:
        if not self._answered:  # (계속 찾기 / 한 번 더 확인: no new answer)
            return ""
        self._answered = False
        eng = self._engine
        if eng is None or not self.steps or not getattr(nxt, "ranking", None):
            return ""
        before_ph = self._before
        if before_ph is None:  # (the first decision came with the plan: the engine before the answer is made now)
            earlier = self._view.engine(self.steps[:-1])
            earlier.summary()
            before_ph = earlier.last_ph
        tied = tied_top(nxt.ranking)
        befores = []
        for s in tied:
            h = eng.hypothesis(s.family, s.mods)
            befores.append(percent(before_ph.get(h, 0.0) if h is not None else 0.0))
        if len(tied) > 3:  # (none stands out: naming the first would blame it for its place in the list)
            same = set(befores)
            text = f"원인 찾기: 아직 두드러지게 의심되는 모드는 없습니다" \
                   f"({tie_text(tied, nxt.ranking, befores[0] if len(same) == 1 else '')})"
        else:  # (the top one, or each of the few as suspected as it)
            text = "원인 찾기: " + " · ".join(f"{suspect_label(s, self.names)} {b} → {percent(s.p)}"
                                          for s, b in zip(tied, befores))
        last = self.steps[-1]
        if last.get("crashed") and not last.get("on"):  # (it only raises "not the mods": say how much)
            text += f" · {OFF_CRASHED}"
            h = eng.hypothesis("none", ())
            if h is not None and not any(s.family == "none" for s in tied):
                text += f"(모드 문제가 아님 {percent(before_ph.get(h, 0.0))} → {percent(eng.last_ph.get(h, 0.0))})"
        if not last.get("crashed") and eng.obs and eng.obs[-1].source == "search" and eng.obs[-1].g < 0.5:
            text += WEAK_ANSWER  # (how the engine weighed it: play time counts only for 한동안 / 가끔)
        return text

    # ---- texts
    def banner(self, step: Step, applied: bool = True) -> str:
        """The step's line (``applied``: it is in the game folder already)."""
        return step_text(step, self.names, applied, self.refw_backup)

    def result_lines(self, result: Result, refw: str | None = None) -> list[str]:
        """What the result means (``refw``: the REFramework mod's id)."""
        off = 0 if self.legacy else sum(1 for s in self.steps if s.get("crashed") is True and not s.get("on"))
        return result_lines(result, self.names, refw, self.requires, self.unmanaged[1], self.refw_backup, off)

    def step_lines(self) -> list[str]:
        """The answered launches, a line each (the result window): "2번째 · 'X'만 끄고 실행 → 튕김 (0xC0000005) · 12분"."""
        from . import gamewatch

        out = []
        names = self.names
        for s in self.steps:
            on = _ids(s.get("on"))
            code = ""
            if isinstance(s.get("exit"), dict) and isinstance(s["exit"].get("code"), int) \
                    and gamewatch.is_crash_code(s["exit"]["code"]):
                code = f" ({gamewatch.code_text(s['exit']['code'])})"
            answer = "튕김" if s.get("crashed") else "괜찮음"
            if self.legacy:
                out.append(f"{s.get('number', '?')}단계 · 모드 {len(on)}개 켬 → {answer}{code}")
                continue
            what = _summary(str(s.get("kind") or ""), _ids(s.get("focus")), on, names)
            seconds = s.get("seconds") if _num(s.get("seconds")) else None
            out.append(f"{s.get('number', '?')}번째 · {what} → {answer}{code}"
                       + (f" · {_minutes(seconds)}분" if seconds is not None else ""))
        return out

    def report_lines(self) -> list[str]:
        """The search for report.txt of the support zip."""
        if self.legacy:
            return self._legacy_report_lines()
        names = self.names
        view = self._view
        out = [f"[{TITLE}]", f"원래 프로필: {self.original_profile}, 임시 프로필: {self.temp_profile}",
               f"켜고 끈 모드 {len(self.mod_ids)}개, 다른 모드에 필요한 모드: "
               + (", ".join(self.name(i) for i in _ids(self.data.get("base"))) or "-")]
        when = f"언제 튕기나요: {WHEN_TEXT.get(view.when, view.when)}"
        if view.when == "time" and view.minutes:
            when += f" (약 {round(view.minutes)}분)"
        if self.data.get("equip_label"):
            when += f" · 고른 장비: {self.data['equip_label']}"
        out.append(when)
        if self.data.get("changes"):
            out.append(str(self.data["changes"]))
        if self.excluded:
            out.append("뺀 모드(옵션 선택 필요): " + ", ".join(self.name(i) for i in self.excluded))
        if self.hidden:
            out.append("뺀 모드(파일이 모두 밀림): " + ", ".join(self.name(i) for i in self.hidden))
        paths, total = self.unmanaged
        if total:
            out.append(f"매니저가 넣지 않은 파일 {total}개: " + ", ".join(paths[:10]))
        start = self.data.get("start_ranking") if isinstance(self.data.get("start_ranking"), list) else []
        reasons = view.reasons
        if start:
            out.append("시작할 때 의심한 정도:")
            for item in start:
                if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str) and _num(item[1]):
                    why = " / ".join(reasons.get(item[0], [])[:3])
                    out.append(f"  약 {percent(item[1])}  {self.name(item[0])}" + (f"  (근거: {why})" if why else ""))
        for note in _ids(self.data.get("evidence_notes")):
            out.append(f"사용한 기록: {note}")
        for item in self.data.get("passive") or []:
            if isinstance(item, dict) and item.get("label"):
                out.append(f"함께 본 실행: {item['label']} → {'튕김' if item.get('crashed') else '괜찮음'}")
        for s in self.steps:
            on = _ids(s.get("on"))
            parts = [_summary(str(s.get("kind") or ""), _ids(s.get("focus")), on, names),
                     "튕김" if s["crashed"] else "괜찮음"]
            line = f"{s.get('number', '?')}번째 ({REASON_TEXT.get(s.get('reason'), s.get('reason') or '-')}): " \
                   f"{parts[0]} → {parts[1]}"
            if isinstance(s.get("exit"), dict) and s["exit"].get("code") is not None:
                line += f", 종료 코드 0x{int(s['exit']['code']) & 0xFFFFFFFF:08X}"
            if _num(s.get("seconds")):
                line += f", 플레이 {_minutes(s['seconds'])}분"
            if s.get("unexposed"):
                line += f", 파일을 읽지 않은 모드 {len(_ids(s['unexposed']))}개"
            if s.get("log"):
                line += ", 파일 기록 사용"
            if s.get("detected"):
                line += f", {s['detected']}"
            out.append(line)
        nxt = self.next()
        if isinstance(nxt, Result):
            title = result_title(nxt, names)
            out.append("결과: " + title)
            out += [f"  {line}" for line in nxt.evidence]
        else:
            out.append(f"진행 중: {nxt.number}번째 실행 ({progress_text(nxt, names)})")
        return out

    def _legacy_report_lines(self) -> list[str]:
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
def _quoted(ids: Sequence[str], names: Mapping[str, str]) -> str:
    return ", ".join(f"'{names.get(i, i)}'" for i in ids)


def percent(p: float) -> str:
    """"1% 미만", "45%", "99% 이상"."""
    if p < 0.01:
        return "1% 미만"
    if p >= 0.995:
        return "99% 이상"
    return f"{round(100 * p)}%"


def launches_text(lo: int, hi: int) -> str:
    """"약 6~9번" (or "약 6번")."""
    return f"약 {lo}번" if lo >= hi else f"약 {lo}~{hi}번"


def suspect_label(s: Suspect, names: Mapping[str, str]) -> str:
    """A cause for the user: "'A'", "'A'+'B' (함께 켰을 때만)", "'A', 'B' (각각)", "모드 문제가 아님 (...)"."""
    q = [f"'{names.get(i, i)}'" for i in s.mods]
    if s.family == "one" and q:
        return q[0]
    if s.family in ("and", "and3"):
        return "+".join(q) + " (함께 켰을 때만)"
    if s.family == "or":
        return ", ".join(q) + " (각각)"
    if s.family == "none":
        return "모드 문제가 아님 (게임·드라이버·매니저가 넣지 않은 파일)"
    return "그 밖의 경우"


def _summary(kind: str, focus: Sequence[str], on: Sequence[str], names: Mapping[str, str]) -> str:
    if kind == "everything_off" or not on:
        return "모든 모드를 끄고 실행"
    if kind == "full":
        return "처음처럼 모두 켜고 실행"
    if kind == "off_some" and focus:
        return f"{_quoted(focus, names)}만 끄고 실행"
    if kind == "on_some" and focus:
        return f"{_quoted(focus, names)}만 켜고 실행"
    return f"모드 {len(on)}개를 켜고 실행"


def step_summary(step: Step, names: Mapping[str, str]) -> str:
    """A step in a few words (the 또 튕겼나요? subtitle, the list of launches): "'X'만 끄고 실행"."""
    return _summary(step.kind if step.engine else "", step.focus, step.on, names)


def tied_top(ranking: Sequence[Suspect]) -> list[Suspect]:
    """The causes as likely as the top one (the top one alone when it stands out; [] for no ranking)."""
    if not ranking:
        return []
    top = ranking[0].p
    return [s for s in ranking if s.p >= top - 1e-9]


def tie_text(tied: Sequence[Suspect], ranking: Sequence[Suspect], before: str = "") -> str:
    """"같은 정도 2개, 각각 약 37%" ("5개 이상" when every one of a full ranking - the top five - is tied;
    ``before``: "각각 24% → 37%")."""
    more = " 이상" if len(tied) == len(ranking) >= 5 else ""
    now = f"{before} → {percent(tied[0].p)}" if before else f"약 {percent(tied[0].p)}"
    return f"같은 정도 {len(tied)}개{more}, 각각 {now}"


def progress_text(step: Step, names: Mapping[str, str]) -> str:
    """The bar's second line: suspects, the top one and about how many launches are left."""
    parts = [f"의심되는 모드 {step.suspects}개"]
    tied = tied_top(step.ranking)
    if len(tied) > 1:
        parts.append(f"가장 의심: 아직 없음({tie_text(tied, step.ranking)})")
    elif step.ranking:
        parts.append(f"가장 의심: {suspect_label(step.ranking[0], names)} 약 {percent(step.ranking[0].p)}")
    if step.remaining is not None:
        parts.append(f"앞으로 {launches_text(*step.remaining)}")
    return " · ".join(parts)


RETRY_HINT = "'잘 모르겠어요'를 고르세요"
CLI_RETRY = "다음부터는 답 대신 'bisect retry'(같은 구성으로 다시)를 쓰세요"  # (the command line takes the answer at once)


def run_note(step: Step, seconds: float | None, retry: str = RETRY_HINT) -> str:
    """Under 또 튕겼나요?: a play much shorter than recommended, or longer than the crash took before
    (for a fine answer; ``retry``: what to pick instead)."""
    if seconds is None or not _num(seconds):
        return ""
    m = _minutes(seconds)
    if step.minutes and seconds < 0.5 * step.minutes * 60:
        return f"이번에는 {m}분만 실행했습니다. 튕기던 상황까지 가지 못했다면 {retry}."
    if step.crash_minutes and seconds > step.crash_minutes * 60:
        return f"이번 실행 {m}분 · 지난번 튕긴 시점(약 {max(1, round(step.crash_minutes))}분)보다 오래 했습니다."
    return ""


def exposure_note(step: Step, unexposed: Iterable[str], names: Mapping[str, str], retry: str = RETRY_HINT) -> str:
    """Under 또 튕겼나요?: the game never requested the top suspect's files in this launch."""
    if not step.ranking:
        return ""
    gone = set(unexposed)
    top = [i for i in step.ranking[0].mods if i in gone and i in step.on]
    if not top:
        return ""
    return (f"이번 실행에서 게임이 가장 의심되는 {_quoted(top, names)}의 파일을 한 번도 읽지 않았습니다. "
            f"그 장비·장소까지 가지 않았다면 {retry}.")


def step_text(step: Step, names: dict[str, str], applied: bool = True, refw_backup: bool = False) -> str:
    """The banner line of a step (without its buttons). ``applied``: the step is in the game folder
    (else it is only set in the temporary profile); ``refw_backup``: a dinput8.dll placed by hand
    comes back when the REFramework mod is off (see :func:`backed_up_refw`)."""
    if step.engine:
        return _engine_step_text(step, names, applied, refw_backup)
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


def off_crash_text(count: int, crashed: int) -> str:
    """How the launches with every mod off went when some crashed: "모든 모드를 끈 실행에서도 튕겼습니다." (the one so
    far), "... 2번 모두 튕겼습니다.", "... 3번 중 1번 튕겼습니다."."""
    if crashed >= count:
        return "모든 모드를 끈 실행에서도 튕겼습니다." if count <= 1 else CONTROL_NONE_FINE.format(count=count)
    return f"모든 모드를 끈 실행 {count}번 중 {crashed}번 튕겼습니다."


def _engine_step_text(step: Step, names: Mapping[str, str], applied: bool, refw_backup: bool) -> str:
    head = f"원인 찾기 {step.number}번째 실행: "
    count = len(step.on)
    prefix = ""
    if step.reason == "others" and step.found:
        prefix = f"{_quoted(step.found, names)}을(를) 끈 채로 다른 원인이 있는지 확인합니다. "
    elif step.reason == "kind":
        prefix = "한 모드만의 문제인지, 함께 켰을 때만인지 확인합니다. "
    elif step.reason == "control":
        prefix = CONTROL_TEXT if not step.control else CONTROL_AGAIN if not step.control_crashed else \
            CONTROL_AFTER_CRASH.format(crashes=off_crash_text(step.control, step.control_crashed))
    q = _quoted(step.focus, names)
    extra = ""
    kind = step.kind if step.on else "everything_off"
    if kind == "everything_off":
        body = ("모든 모드를 끄고 적용했습니다.", "모든 모드를 꺼 두었습니다.")
        if refw_backup:
            extra = " 직접 넣어 두었던 dinput8.dll은 되돌아와 그대로 있습니다."
    elif kind == "full":
        body = (f"처음처럼 모드 {count}개를 모두 켜고 적용했습니다.", f"처음처럼 모드 {count}개를 모두 켜 두었습니다.")
    elif kind == "base":
        body = (f"REFramework와 다른 모드에 필요한 모드 {count}개만 켜고 적용했습니다.",
                f"REFramework와 다른 모드에 필요한 모드 {count}개만 켜 두었습니다.")
    elif kind == "off_some" and step.focus:
        body = (f"{q}만 끄고 나머지는 그대로 적용했습니다.", f"{q}만 꺼 두었습니다.")
        k = len(step.off) - len(step.focus)
        if k > 0:
            extra = f" (그 모드가 필요한 모드 {k}개도 함께 껐습니다)"
    elif kind == "on_some" and step.focus:
        body = (f"{q}만 켜고(필요한 모드 포함 {count}개) 적용했습니다.", f"{q}만 켜 두었습니다(필요한 모드 포함 {count}개).")
    elif step.off_suspects:
        body = (f"아직 의심되는 모드 {step.suspects}개 중 {step.off_suspects}개를 끄고(모드 {count}개 켬) 적용했습니다.",
                f"아직 의심되는 모드 {step.suspects}개 중 {step.off_suspects}개를 꺼 두었습니다(모드 {count}개 켬).")
    else:
        body = (f"모드 {len(step.off)}개를 끄고(모드 {count}개 켬) 적용했습니다.",
                f"모드 {len(step.off)}개를 꺼 두었습니다(모드 {count}개 켬).")
    text = head + prefix + (body[0] if applied else body[1]) + extra
    if not applied:
        text += UNAPPLIED
    return text + (" " + step.play if step.play else "")


def result_title(result: Result, names: dict[str, str]) -> str:
    """The result's headline."""
    if result.p is not None:
        pct = percent(result.p)
        listed = ", ".join(names.get(i, i) for i in result.mods)
        if result.kind == "found" and result.mods:
            return f"원인으로 의심되는 모드: {listed} (가능성 약 {pct})"
        if result.kind == "together":
            return f"함께 켜면 튕기는 것으로 의심되는 모드: {listed} (가능성 약 {pct})"
        if result.kind == "each":
            return f"각각 튕기게 하는 것으로 의심되는 모드: {listed} (가능성 약 {pct})"
        if result.kind == "core":
            return f"원인으로 의심되는 모드: {listed} (가능성 약 {pct})"
        if result.kind == "not_mods":
            return f"모드 문제가 아닐 가능성이 높습니다 (약 {pct}). 모든 모드를 꺼도 튕겼습니다."
        if result.kind == "unsure":
            return "아직 하나로 좁히지 못했습니다."
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
    if result.kind == "unsure":
        return "아직 하나로 좁히지 못했습니다."
    return result.reason or "원인 찾기를 그만두었습니다."


def disable_unsure_text(result: Result, names: Mapping[str, str]) -> str:
    """The button that turns an unsure result's top suspect off (RESULT_DISABLE_UNSURE), with how likely it is
    part of the cause (alone or with another mod)."""
    listed = ", ".join(names.get(i, i) for i in result.mods)
    return RESULT_DISABLE_UNSURE.format(name=listed, pct=percent(result.p or 0.0))


def result_lines(result: Result, names: dict[str, str], refw: str | None = None,
                 requires: dict[str, list[str]] | None = None, unmanaged: int = 0,
                 refw_backup: bool = False, off_crashes: int = 0) -> list[str]:
    """What the result means and what to try (under the headline and its 근거). ``refw``: the REFramework
    mod's id; ``refw_backup``: a dinput8.dll placed by hand came back whenever the REFramework mod was off;
    ``off_crashes``: launches of the search with every mod off that crashed (a named result: OFF_CRASH_NOTE)."""
    lines = _result_lines(result, names, refw, requires, unmanaged, refw_backup)
    if off_crashes > 0 and result.kind in NAMED and result.p is not None:
        lines.append(OFF_CRASH_NOTE.format(count=off_crashes))
    return lines


def _result_lines(result: Result, names: dict[str, str], refw: str | None, requires: dict[str, list[str]] | None,
                  unmanaged: int, refw_backup: bool) -> list[str]:
    v2 = result.p is not None
    if result.kind == "found":
        if v2:
            return ["모드를 최신 버전으로 업데이트하거나, 재질 점검·복구를 해 보거나, 만든 사람에게 알려 보세요."]
        return ["이 모드를 켰을 때만 튕겼습니다. 모드를 최신 버전으로 업데이트하거나, 재질 점검·복구를 해 보거나, 만든 "
                "사람에게 알려 보세요."]
    if result.kind == "together":
        which = "둘 중" if len(result.mods) == 2 else "이 중"
        if v2:
            return [f"같은 파일이나 기능을 건드리는 모드일 수 있습니다. {which} 하나를 끄거나 업데이트해 보세요."]
        return ["하나씩은 괜찮고 함께 켰을 때만 튕겼습니다. 같은 파일이나 기능을 건드리는 모드일 수 있습니다. "
                f"{which} 하나를 끄거나 업데이트해 보세요."]
    if result.kind == "each":
        return ["두 모드를 모두 끄거나 업데이트해 보세요."]
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
            kept = "다른 모드에 필요한 모드입니다." if v2 else "다른 모드에 필요한 모드라 계속 켜 두었던 모드입니다."
            out.append(f"{_quoted(others, names)}은(는) {kept}"
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
    if result.kind == "unsure":
        out = []
        if result.r_mean is not None and result.r_mean < 0.7:
            out.append("같은 구성에서 튕기기도 하고 괜찮기도 했습니다. 가끔 튕기는 경우(그래픽 드라이버, VRAM 부족 등)이거나 "
                       "튕기던 상황까지 가지 못한 실행이 있었을 수 있습니다.")
        else:
            out.append(f"게임을 {result.runs}번 실행했지만 아직 하나로 좁히지 못했습니다.")
        out.append(f"'{RESULT_MORE}'로 {MORE_LAUNCHES}번 더 해 보거나, 원래 프로필로 돌아갈 수 있습니다.")
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
    return isinstance(data, dict) and data.get("format") in (SESSION_FORMAT, LEGACY_FORMAT)


def _temp_name(profiles) -> str:
    taken = {p.casefold() for p in profiles}
    if TEMP_PROFILE.casefold() not in taken:
        return TEMP_PROFILE
    n = 2
    while f"{TEMP_PROFILE[:-1]} {n})".casefold() in taken:
        n += 1
    return f"{TEMP_PROFILE[:-1]} {n})"


def start(manager, plan: Plan, record: str = "", *, when: str | None = None, minutes: float | None = None,
          equip: str | None = None, refw_restore: dict | None = None) -> tuple[Session, Step | Result]:
    """Start a search: switch to a temporary copy of the current profile and turn the first step's
    mods on there. The caller then applies (deploys). ``record``: the crash record it started from;
    ``when``/``minutes``/``equip``: the start window's answers (else the plan's); ``refw_restore``:
    what accessed.enable_logging changed (kept with the search, restored after finish)."""
    if active(manager.data_dir):
        raise BisectError("이미 원인 찾기가 진행 중입니다.")
    if plan.problem:
        raise BisectError(plan.problem)
    enabled = {m.id for m in manager.mods if m.enabled}
    if manager.profile != plan.profile or enabled != {*plan.order, *plan.excluded, *plan.hidden} or \
            (plan.revision is not None and plan.revision != manager.revision):
        raise BisectError("준비하는 동안 모드 상태가 바뀌었습니다. 원인 모드 찾기를 다시 시작하세요.")
    if manager.outdated_paks():  # moving them changes the game's fingerprint: problem() would see an update
        raise BisectError(OUTDATED_PAKS)
    manager.save()  # the original profile's snapshot as it is now
    original = manager.profile
    temp = _temp_name(manager.profiles)
    session = Session.new(manager.data_dir, plan, original, manager.profiles.get(original) or {}, temp, record,
                          when=when, minutes=minutes, equip=equip, refw_restore=refw_restore)
    session.save()  # first: a temporary profile is never left without its search
    try:
        manager.create_profile(temp, copy_current=True)
    except BaseException:
        session.remove()
        raise
    if session.legacy:
        log.info("원인 찾기를 시작합니다: 모드 %d개 중에서 찾고 %d개는 계속 켜 둡니다 (임시 프로필 '%s').",
                 len(plan.units), len(plan.base), temp)
    else:
        log.info("원인 찾기를 시작합니다: 모드 %d개 (언제 튕기나요: %s, 임시 프로필 '%s').", len(session.mod_ids),
                 session.when, temp)
    nxt = apply_step(manager, session)
    if isinstance(nxt, Result):  # (the records alone were enough: kept like any result)
        nxt = session.settle()
    return session, nxt


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
    cover = session.hidden_winners  # (a hidden mod on only with every mod covering its files: never its own copies)
    changed = False
    for m in manager.mods:
        if m.id in inside:
            on = m.id in want
        elif m.id in cover:
            on = all(w in want for w in cover[m.id])
        else:
            continue
        if m.enabled != on:
            m.enabled = on
            changed = True
    if changed:
        manager.revision += 1
        manager.dirty = True
        manager.save()
    session.mark_applied(nxt)
    if nxt.engine:
        log.info("원인 찾기 %d번째 실행: 모드 %d개를 켭니다 (%s).", nxt.number, len(nxt.on),
                 REASON_TEXT.get(nxt.reason, nxt.reason))
    else:
        log.info("원인 찾기 %d단계: 모드 %d개를 켭니다.", nxt.number, len(nxt.on))
    return nxt


def answer(manager, session: Session, crashed: bool | None, detected: str = "", run: dict | None = None) -> Step | Result:
    """Record the answer for the current step and turn the next step's mods on (then the caller
    applies), or return the result (the temporary profile stays until :func:`finish`). ``run``: what
    accessed.step_run found of that launch (else the one noted with Session.note_run)."""
    nxt = session.record(crashed, detected, run)
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
