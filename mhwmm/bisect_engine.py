"""원인 모드 찾기 v2: a Bayesian search for what makes the game crash (pure: no I/O, no clock).

Units are the mods the search turns on and off (REFramework and libraries included); a launch
turns a set S of them on, closed under ``requires`` (a unit on -> everything it requires on;
requirements and "a mod whose files another mod overwrites needs that mod", so a test never loads
a copy of a file the user's own setup never loads).  The unknown cause is one of these crash
functions f(S):

  none      f = 1 for every S (not the mods: the game, a driver, files the manager did not place)
  one {x}   f = [x in S]
  and {x,y} f = [x in S and y in S]        (two mods that crash only together)
  or  {x,y} f = [x in S or y in S]         (two broken mods, each crashes on its own)
  and3      f = [C <= S], |C| = 3           (only for n <= Params.and3_max_n)
  rest      f = x^3 / 2 + (1 - (1 - x)^3) / 2, x = |S| / n (a soft catch-all)

each with an unknown reproduction rate r on a small grid.  One launch (an observation) with S on:

  q = 1 - (1 - b)(1 - r * rho * gamma * f(S))      P(answer = crash) = (1 - e) q + e (1 - q)

b: a crash from something else; e: a wrong answer (smaller when the answer agrees with what the
manager detected); rho: the session did what triggers the crash (1 in the search, less for the last
normal exit and older launches); gamma = 1 for a crash answer, and for a fine one g(seconds) = the
chance that a crash would have come within that play, from the crash times known before it.  f is
LEAK instead of 1 when the game never requested a member's files in that session (the access log),
and an observation says nothing about a hypothesis whose members were changed since (``unknown``).

The posterior over (cause, r) is a pure function of the prior and the observations in order, so a
search resumes from its stored answers.  The next launch maximises the expected information gain
about the cause, plus a small bonus for launches that add an independent pivot pair for a top
suspect; among launches within ``near_best`` of the best, the one with the most mods on.

Naming rule.  A cause C is named only when (1) every member x has P(x is part of the cause) >= tau,
(2) every member has a pivot matching of size >= 2 (3 when E[r] < r_confident, or after 한 번 더
확인), (3) turning C off is expected to fix the crash (else the others are tested with C off) and
the kind of cause is clear (else one more launch), unless the budget is spent.

A pivot pair for x is a crash observation c with x on (not unknown, not unexposed) and a fine
observation f with x off, long enough (rho * g >= fine_weight), such that everything else on in c
(and what x requires) is on in f.  The crash comes from the crash record or a launch of the search;
the fine run only from a launch of the search: an earlier ordinary session (the last normal exit,
launches.json) may never have reached what triggers the crash - and when a user never goes there,
none of their sessions do, so those are not independent answers - and an earlier crash is kept for
looking like the record's, which says little about its cause.  Earlier sessions still move the
probabilities (the prior and the posterior), so they speed the search up; they never name a mod.

Theorem.  For a monotone cause, if both answers of a pivot pair for x are right, then x is in every
minimal crashing set inside S_c.  Proof: f(S_c) = 1, and f(S_c minus x) <= f(S_f) = 0 because
S_c minus x is a subset of S_f.  By Konig's theorem a matching of m pivot pairs (no observation used
twice) survives any m - 1 wrong answers.  The rule does not read the prior, so misleading evidence
can only slow the search down; masking edges keep tests monotone for file conflicts.

A crash the game reports itself as a graphics device error (DEVICE_HUNG ...) is usually not the mods
and comes and goes; such a search starts with the "rare" reproduction prior and names a mod only
after ``controls`` launches with every mod off ran fine (Params.controls).

Everything here is deterministic: sets of strings are never iterated in a decision path (units go
in their index order), and ties go to the higher score rounded to 1e-9, then more mods on, then the
earlier candidate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from itertools import combinations
from typing import Callable, Iterable, Mapping, Sequence

ENGINE_FORMAT = 1
FAMILIES = ("none", "one", "and", "or", "and3", "rest")
REASONS = ("split", "full", "everything_off", "confirm_crash", "confirm_fine", "others", "kind", "dry", "control")
CRASH_SOURCES = ("report", "search")  # the crash half of a pivot pair (see the module docstring)
FINE_SOURCES = ("search",)  # its fine half
R_PRIORS = {"sharp": (0.62, 0.20, 0.12, 0.06), "middle": (0.45, 0.25, 0.18, 0.12), "rare": (0.30, 0.30, 0.25, 0.15)}
WHEN_PRIOR = {"start": "sharp", "same": "middle", "equip": "middle", "time": "middle", "rare": "rare", "": "middle"}

NONE, ONE, AND, OR, AND3, REST = range(6)
WEAK = 1e-12  # hypotheses lighter than this are left out of the gains
_SKIP = 3  # class of a hypothesis an observation says nothing about
_SQRT2 = math.sqrt(2.0)


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# ---- parameters ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class Params:
    """The model and the decision rule (stored with a search at its start)."""
    r_grid: tuple = (0.95, 0.8, 0.55, 0.3)
    r_prior: tuple = R_PRIORS["middle"]
    b: float = 0.02  # background crash per launch (not the units)
    e: float = 0.02  # a wrong answer
    e_agrees: float = 0.005  # ... when the answer agrees with what the manager detected
    leak: float = 0.2  # f of a cause whose files were never requested (logging gaps)
    p_none: float = 0.03
    p_one: float = 0.80
    p_and: float = 0.07
    p_or: float = 0.06
    p_and3: float = 0.02
    p_rest: float = 0.02
    and3_max_n: int = 24
    tau: float = 0.95  # P(part of the cause) of every named member
    tau_fix: float = 0.95  # P(turning the named mods off fixes it)
    tau_exact: float = 0.80  # P(the named hypothesis itself): else one more launch for the kind
    confirm_at: float = 0.80  # from this involvement, launches aimed at the missing pivot pairs
    r_confident: float = 0.85  # E[r] below this: one more pivot pair
    fine_weight: float = 0.5  # a fine answer is direct evidence when rho * g >= this
    prune: float = 25.0  # nats below the best: left out
    min_gain: float = 0.005  # bits: nothing left to learn below this
    split_points: int = 48  # prefixes evaluated per order for large n
    max_launches: int = 40
    more_launches: int = 6  # per 계속 찾기
    dry_spell: int = 3  # no crash in this many launches: the full set next
    pivot_bonus: float = 0.15  # bits per expected new independent pivot pair of a top suspect
    near_best: float = 0.95  # among launches this close to the best, the one with the most mods on
    clear_mass: float = 0.1  # a unit is cleared below this share of an even split of the mass
    g_sigma: float = 0.6  # spread of ln(crash time)
    weight_min: float = 0.05
    weight_max: float = 50.0
    share_max: float = 0.85
    none_scale_max: float = 6.0
    # the remaining-launch estimate (fitted with tools/bisect_sim.py): x = ceil(log2 n_eff) + the pivot pairs the
    # top single suspect still lacks + est_shift; (lo, hi) = (x - est_lo, x + est_hi), widened by est_noisy_lo/hi
    # once E[r] < r_confident after est_noisy_after launches
    est_shift: int = -1
    est_lo: int = 1
    est_hi: int = 1
    est_noisy_lo: int = 2
    est_noisy_hi: int = 5
    est_noisy_after: int = 2
    # 언제 튕기나요: a fine launch of the search counts by its play time against the usual time to crash (g) only
    # when the crash comes "after a while" or "sometimes"; at a place, an action or a piece of equipment (or at
    # start-up) the user goes there, and the play time says nothing
    timed_fine: bool = True
    passive_times: bool = True  # the record's and older crashes' seconds join the crash times (not when the user typed them)
    controls: int = 0  # fine launches with every mod off before a mod is named (graphics device errors: 2)

    def to_dict(self) -> dict:
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            out[f.name] = list(v) if isinstance(v, tuple) else v
        return out

    @classmethod
    def from_dict(cls, data) -> Params:
        """Unknown keys are ignored, missing ones and values of the wrong type or range default."""
        default = cls()
        if not isinstance(data, Mapping):
            return default
        kw: dict = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value, d = data[f.name], getattr(default, f.name)
            if isinstance(d, tuple):
                if isinstance(value, (list, tuple)) and value and all(_num(x) and 0 <= x <= 1 for x in value):
                    kw[f.name] = tuple(float(x) for x in value)
            elif isinstance(d, bool):
                if isinstance(value, bool):
                    kw[f.name] = value
            elif isinstance(d, int):
                if isinstance(value, int) and not isinstance(value, bool) and (value >= 0 or f.name == "est_shift"):
                    kw[f.name] = value
            elif _num(value) and value >= 0 and (f.name not in _UNIT_FIELDS or value <= 1):
                kw[f.name] = float(value)
        grid, prior = kw.get("r_grid", default.r_grid), kw.get("r_prior", default.r_prior)
        if len(grid) != len(prior) or sum(prior) <= 0 or min(grid) <= 0:
            kw.pop("r_grid", None)
            kw.pop("r_prior", None)
        if kw.get("g_sigma", 1.0) <= 0:
            kw.pop("g_sigma")
        if kw.get("weight_min", 1.0) <= 0 or kw.get("weight_max", default.weight_max) < kw.get("weight_min", 0):
            kw.pop("weight_min", None)
            kw.pop("weight_max", None)
        return cls(**kw)


_UNIT_FIELDS = {"b", "e", "e_agrees", "leak", "p_none", "p_one", "p_and", "p_or", "p_and3", "p_rest", "tau", "tau_fix",
                "tau_exact", "confirm_at", "r_confident", "fine_weight", "near_best", "clear_mass", "share_max"}


def budget(n_units: int, extra: int = 0, params: Params | None = None) -> int:
    """The most launches a search makes before it stops unsure (``extra``: added by 계속 찾기)."""
    p = params or Params()
    return min(p.max_launches, 3 * math.ceil(math.log2(max(0, n_units) + 2)) + 12) + max(0, int(extra))


# ---- the prior from the evidence ----------------------------------------------------------------
@dataclass
class Prior:
    """Starting suspicion: ``weights`` multiply a mod's share of the one-mod mass, ``shares`` give a
    mod a fixed share of it (strong pointers), ``pairs`` boost "only together" pairs."""
    weights: dict = field(default_factory=dict)
    shares: dict = field(default_factory=dict)
    pairs: list = field(default_factory=list)
    none_scale: float = 1.0
    or_scale: float = 1.0

    def to_dict(self) -> dict:
        return {"weights": {str(k): float(v) for k, v in self.weights.items()},
                "shares": {str(k): float(v) for k, v in self.shares.items()},
                "pairs": [[str(a), str(b), float(x)] for a, b, x in self.pairs],
                "none_scale": float(self.none_scale), "or_scale": float(self.or_scale)}

    @classmethod
    def from_dict(cls, data) -> Prior:
        if not isinstance(data, Mapping):
            return cls()

        def numbers(value) -> dict:
            if not isinstance(value, Mapping):
                return {}
            return {k: float(v) for k, v in value.items() if isinstance(k, str) and _num(v) and v >= 0}

        pairs = []
        for item in data.get("pairs") or []:
            if isinstance(item, (list, tuple)) and len(item) == 3 and isinstance(item[0], str) \
                    and isinstance(item[1], str) and _num(item[2]) and item[2] > 0:
                pairs.append((item[0], item[1], float(item[2])))
        scale = {k: float(data[k]) if _num(data.get(k)) and data[k] > 0 else 1.0 for k in ("none_scale", "or_scale")}
        return cls(numbers(data.get("weights")), numbers(data.get("shares")), pairs, scale["none_scale"],
                   scale["or_scale"])

    @property
    def empty(self) -> bool:
        """No evidence at all (every weight 1, no shares, no pairs, scales 1)."""
        return (all(abs(v - 1.0) < 1e-12 for v in self.weights.values()) and not any(self.shares.values())
                and not self.pairs and self.none_scale == 1.0 and self.or_scale == 1.0)


# ---- observations and decisions -------------------------------------------------------------------
@dataclass(frozen=True)
class Obs:
    """One session of the game: the units ``on``, whether it crashed, and how much it says."""
    on: frozenset
    crashed: bool
    rho: float = 1.0
    seconds: float | None = None
    e: float | None = None
    unknown: frozenset = frozenset()
    unexposed: frozenset = frozenset()
    source: str = "search"  # "report" | "last_good" | "history" | "search"

    def to_dict(self, order: Sequence[str] | None = None) -> dict:
        def listed(ids) -> list[str]:
            if order is not None:
                inside = set(ids)
                return [i for i in order if i in inside] + sorted(i for i in ids if i not in set(order))
            return sorted(ids)

        return {"on": listed(self.on), "crashed": bool(self.crashed), "rho": float(self.rho), "seconds": self.seconds,
                "e": self.e, "unknown": listed(self.unknown), "unexposed": listed(self.unexposed),
                "source": self.source}

    @classmethod
    def from_dict(cls, data) -> Obs | None:
        if not isinstance(data, Mapping) or not isinstance(data.get("crashed"), bool) \
                or not isinstance(data.get("on"), (list, tuple)):
            return None

        def ids(key) -> frozenset:
            value = data.get(key)
            return frozenset(i for i in value if isinstance(i, str)) if isinstance(value, (list, tuple)) else frozenset()

        rho = float(data["rho"]) if _num(data.get("rho")) and 0 <= data["rho"] <= 1 else 1.0
        seconds = float(data["seconds"]) if _num(data.get("seconds")) and data["seconds"] >= 0 else None
        e = float(data["e"]) if _num(data.get("e")) and 0 <= data["e"] <= 0.5 else None
        source = data.get("source") if data.get("source") in ("report", "last_good", "history", "search") else "search"
        return cls(ids("on"), data["crashed"], rho, seconds, e, ids("unknown"), ids("unexposed"), source)


@dataclass(frozen=True)
class Suspect:
    family: str  # FAMILIES
    mods: tuple  # unit ids in unit order (empty for none and rest)
    p: float


@dataclass
class Decision:
    kind: str  # "test" | "found" | "together" | "each" | "not_mods" | "no_repro" | "unsure"
    on: frozenset = frozenset()  # kind "test": the units on
    reason: str = ""  # REASONS (kind "test")
    mods: tuple = ()  # named units (a test: the top hypothesis's), in unit order
    p: float = 0.0  # found/together/each: min involvement; not_mods: P(none); test/unsure: P(top)
    pivots: int = 0  # matching of the weakest named member (capped at 3)
    fix: float = 0.0
    can_confirm: bool = False  # named with pivots < 3 and launches that would add one exist (see _confirm_sequence)
    ranking: list = field(default_factory=list)  # top 5 Suspects by P (rest left out, p >= 0.01)
    involvement: dict = field(default_factory=dict)  # unit -> P(part of the cause)
    suspects: int = 0  # units with involvement >= 0.2 / n
    r_mean: float = 1.0
    remaining: tuple = (0, 0)  # launches from now on, this one included (a test)
    entropy: float = 0.0  # bits, over the causes
    family: str = ""  # the top hypothesis's family
    sure: float = 0.0  # min involvement of the top hypothesis's members (P(none) for none)


def hb(p: float) -> float:
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


def _bits(mask: int):
    """Indexes of the set bits, lowest first."""
    while mask:
        low = mask & -mask
        yield low.bit_length() - 1
        mask ^= low


def _popcount(x: int) -> int:
    return bin(x).count("1")


class _Rec:
    """An observation as masks."""
    __slots__ = ("on", "crashed", "rho", "seconds", "e", "unknown", "unexposed", "source", "g", "w")

    def __init__(self, on, crashed, rho, seconds, e, unknown, unexposed, source, g):
        self.on, self.crashed, self.rho, self.seconds, self.e = on, crashed, rho, seconds, e
        self.unknown, self.unexposed, self.source, self.g = unknown, unexposed, source, g
        self.w = rho * (1.0 if crashed else g)


class _Post:
    """The posterior: P(h) over the hypotheses within ``prune`` of the best, P(r) and P(r | h)."""
    __slots__ = ("alive", "ph", "pr", "prh", "r_mean", "top", "ptop", "inv", "mass")


# the class of a pair (and / or) by the states of its members: 0 off, 1 on, 2 on but unexposed, 3 unknown
_AND_CLASS = [[0, 0, 0, _SKIP], [0, 1, 2, _SKIP], [0, 2, 2, _SKIP], [_SKIP] * 4]
_OR_CLASS = [[0, 1, 2, _SKIP], [1, 1, 1, _SKIP], [2, 1, 2, _SKIP], [_SKIP] * 4]


# ---- the engine -------------------------------------------------------------------------------------
class Engine:
    """The search over ``units`` (in their order); ``requires``: unit -> units it needs on."""

    def __init__(self, units: Sequence[str], requires: Mapping[str, Sequence[str]] | None = None, *,
                 content: Iterable[str] = (), prior: Prior | None = None, params: Params | None = None,
                 crash_times: Sequence[float] = (), strict: bool = False):
        self.p = params if isinstance(params, Params) else Params()
        self.units = list(dict.fromkeys(u for u in units if isinstance(u, str)))
        n = self.n = len(self.units)
        self.index = {u: i for i, u in enumerate(self.units)}
        self.full = (1 << n) - 1
        req: list[list[int]] = [[] for _ in range(n)]
        for k, v in (requires or {}).items():
            i = self.index.get(k)
            if i is None or not isinstance(v, (list, tuple)):
                continue
            for r in v:
                j = self.index.get(r)
                if j is not None and j != i and j not in req[i]:
                    req[i].append(j)
        self.req = req
        self.req_closure = [self._closure(i, req) for i in range(n)]  # i and what it needs
        rev: list[list[int]] = [[] for _ in range(n)]
        for i in range(n):
            for j in req[i]:
                rev[j].append(i)
        self.dep_closure = [self._closure(i, rev) for i in range(n)]  # i and what needs it
        self.content = self.mask(content)
        self.strict = bool(strict)
        self.prior = prior if isinstance(prior, Prior) else Prior()
        self.times0 = [float(t) for t in crash_times if _num(t) and t > 0]
        self.times = list(self.times0)  # and the seconds of every crashed observation, in order
        self.obs: list[_Rec] = []
        grid, prior_r = self.p.r_grid, self.p.r_prior
        if len(grid) != len(prior_r) or sum(prior_r) <= 0:
            grid, prior_r = Params().r_grid, Params().r_prior
        self.grid = tuple(grid)
        z = sum(prior_r)
        self.pi_r = tuple(x / z for x in prior_r)
        self.R = len(self.grid)
        self.last_ph: dict[int, float] = {}
        self._build()

    # ---- structure
    @staticmethod
    def _closure(i: int, edges) -> int:
        out, todo = 0, [i]
        while todo:
            j = todo.pop()
            if not out >> j & 1:
                out |= 1 << j
                todo.extend(edges[j])
        return out

    def mask(self, ids: Iterable[str]) -> int:
        out = 0
        for u in ids:
            i = self.index.get(u) if isinstance(u, str) else None
            if i is not None:
                out |= 1 << i
        return out

    def ids(self, mask: int) -> frozenset:
        return frozenset(self.units[i] for i in _bits(mask & self.full))

    def ordered(self, mask: int) -> tuple:
        return tuple(self.units[i] for i in _bits(mask & self.full))

    def _close(self, mask: int) -> int:
        out = 0
        for i in _bits(mask):
            out |= self.req_closure[i]
        return out

    def _without(self, mask: int) -> int:
        off = 0
        for i in _bits(mask):
            off |= self.dep_closure[i]
        return self.full & ~off

    def close(self, ids: Iterable[str]) -> frozenset:
        """``ids`` with what they require."""
        return self.ids(self._close(self.mask(ids)))

    def without(self, ids: Iterable[str]) -> frozenset:
        """Every unit but ``ids`` and what needs them."""
        return self.ids(self._without(self.mask(ids)))

    def _build(self) -> None:
        p, n, prior = self.p, self.n, self.prior
        w = []
        for u in self.units:
            x = prior.weights.get(u, 1.0)
            w.append(min(p.weight_max, max(p.weight_min, float(x))) if _num(x) else 1.0)
        shares = []
        for u in self.units:
            x = prior.shares.get(u, 0.0)
            shares.append(min(1.0, max(0.0, float(x))) if _num(x) else 0.0)
        total = sum(shares)
        if total > p.share_max:
            shares = [s * p.share_max / total for s in shares]
            total = p.share_max
        wsum = sum(w) or 1.0
        pi_one = [shares[i] + (1.0 - total) * w[i] / wsum for i in range(n)]
        zs = sum(pi_one) or 1.0
        weff = [n * x / zs for x in pi_one]  # effective weights for pairs and triples
        rc = self.req_closure
        boost: dict[tuple[int, int], float] = {}
        for item in prior.pairs:
            try:
                a, b, x = item
            except (TypeError, ValueError):
                continue
            i, j = self.index.get(a), self.index.get(b)
            if i is None or j is None or i == j or not _num(x) or x <= 0:
                continue
            key = (min(i, j), max(i, j))
            boost[key] = max(boost.get(key, 0.0), min(p.weight_max, max(p.weight_min, float(x))))

        kinds: list[int] = []
        masks: list[int] = []
        members: list[tuple] = []
        mass: list[float] = []
        self.ranges = [(0, 0)] * 6

        def family(code, items, family_mass):
            start = len(kinds)
            total_w = sum(x for _m, _mem, x in items)
            if items and family_mass > 0 and total_w > 0:
                for m, mem, x in items:
                    kinds.append(code)
                    masks.append(m)
                    members.append(mem)
                    mass.append(family_mass * x / total_w)
            self.ranges[code] = (start, len(kinds))

        def free(*mem) -> bool:  # no member needs another (else the set means the same as a smaller one)
            return all(not (rc[a] >> b & 1) for a in mem for b in mem if a != b)

        none_scale = min(p.none_scale_max, max(p.weight_min, prior.none_scale if _num(prior.none_scale) else 1.0))
        or_scale = min(p.none_scale_max, max(p.weight_min, prior.or_scale if _num(prior.or_scale) else 1.0))
        family(NONE, [(0, (), 1.0)], p.p_none * none_scale)
        family(ONE, [(1 << i, (i,), pi_one[i]) for i in range(n)], p.p_one)
        self.pairs = [(i, j) for i, j in combinations(range(n), 2) if free(i, j)]
        family(AND, [((1 << i) | (1 << j), (i, j), math.sqrt(weff[i] * weff[j]) * boost.get((i, j), 1.0))
                     for i, j in self.pairs], p.p_and)
        family(OR, [((1 << i) | (1 << j), (i, j), math.sqrt(weff[i] * weff[j])) for i, j in self.pairs],
               p.p_or * or_scale)
        triples = 3 <= n <= p.and3_max_n
        if triples:
            family(AND3, [((1 << i) | (1 << j) | (1 << k), (i, j, k), (weff[i] * weff[j] * weff[k]) ** (1 / 3))
                          for i, j, k in combinations(range(n), 3) if free(i, j, k)], p.p_and3)
        else:
            self.ranges[AND3] = (len(kinds), len(kinds))
        family(REST, [(0, (), 1.0)], p.p_rest + (0.0 if triples else p.p_and3))
        z = sum(mass) or 1.0
        self.kinds, self.masks, self.members = kinds, masks, members
        self.H = len(kinds)
        logp = [math.log(x / z) if x > 0 else -1e9 for x in mass]
        self.L = [[lp + math.log(pr) if pr > 0 else lp - 1e9 for lp in logp] for pr in self.pi_r]
        self.base = [[0.0] * self.R for _ in range(6)]  # per family, added to every member (pair updates)
        pidx = [[-1] * n for _ in range(n)]
        for k, (i, j) in enumerate(self.pairs):
            pidx[i][j] = pidx[j][i] = k
        self.pidx = pidx

    # ---- the model
    def f(self, h: int, on: int, unexposed: int = 0) -> float:
        kind, m = self.kinds[h], self.masks[h]
        if kind == NONE:
            return 1.0
        if kind == REST:
            x = _popcount(on) / self.n if self.n else 0.0
            return 0.5 * x ** 3 + 0.5 * (1 - (1 - x) ** 3)
        if kind == OR:
            hit = m & on
            if not hit:
                return 0.0
            return 1.0 if hit & ~unexposed else self.p.leak
        if m & on != m:
            return 0.0
        return self.p.leak if m & unexposed else 1.0

    def _lik(self, crashed: bool, e: float, drive_scale: float, fv: float, r: float) -> float:
        b = self.p.b
        pc = 1 - (1 - b) * (1 - r * drive_scale * fv)
        pa = (1 - e) * pc + e * (1 - pc)
        return pa if crashed else 1 - pa

    def _terms(self, crashed: bool, e: float, scale: float, fv: float) -> list[float]:
        return [math.log(max(1e-300, self._lik(crashed, e, scale, fv, r))) for r in self.grid]

    def _gfun(self, seconds: float | None, times: Sequence[float]) -> float:
        if seconds is None or not times:
            return 1.0
        d = math.log(max(float(seconds), 1.0))
        s = self.p.g_sigma * _SQRT2
        return sum(0.5 * (1 + math.erf((d - math.log(max(t, 1.0))) / s)) for t in times) / len(times)

    def _times_from(self, o: _Rec) -> bool:
        """The observation's play time is a crash time (a crash with its seconds known; the record's and older
        ones only with Params.passive_times)."""
        return o.crashed and o.seconds is not None and (o.source == "search" or self.p.passive_times)

    def g(self, seconds: float | None, before: int | None = None) -> float:
        """P(a crash that comes would have come within ``seconds``), from the crash times known before
        observation ``before`` (None: all known now)."""
        if before is None:
            return self._gfun(seconds, self.times)
        times = list(self.times0)
        for o in self.obs[:max(0, before)]:
            if self._times_from(o):
                times.append(o.seconds)
        return self._gfun(seconds, times)

    def add(self, obs: Obs) -> None:
        """Take one observation into the posterior (in order: g uses the crash times before it)."""
        p, n, R = self.p, self.n, self.R
        unknown = self.mask(obs.unknown)
        on = self.mask(obs.on) & ~unknown
        unexposed = self.mask(obs.unexposed) & on & self.content
        crashed = bool(obs.crashed)
        rho = float(obs.rho) if _num(obs.rho) else 1.0
        rho = min(1.0, max(0.0, rho))
        seconds = float(obs.seconds) if _num(obs.seconds) and obs.seconds >= 0 else None
        e = p.e if obs.e is None or not _num(obs.e) else min(0.5, max(0.0, float(obs.e)))
        timed = not crashed and (obs.source != "search" or p.timed_fine)
        g = self._gfun(seconds, self.times) if timed else 1.0
        rec = _Rec(on, crashed, rho, seconds, e, unknown, unexposed, obs.source, g)
        self.obs.append(rec)
        if self._times_from(rec):
            self.times.append(seconds)
        scale = rho * (1.0 if crashed else g)
        t = [self._terms(crashed, e, scale, fv) for fv in (0.0, 1.0, p.leak)]
        t.append([0.0] * R)  # _SKIP
        L = self.L
        # none: f = 1 (nothing it says when something was unknown)
        s, end = self.ranges[NONE]
        if end > s and not unknown:
            for r in range(R):
                L[r][s] += t[1][r]
        # one {x}
        s, end = self.ranges[ONE]
        if end > s:
            for i in range(n):
                bit = 1 << i
                c = _SKIP if unknown & bit else (0 if not on & bit else 2 if unexposed & bit else 1)
                if c != _SKIP:
                    tc = t[c]
                    for r in range(R):
                        L[r][s + i] += tc[r]
        # pairs: by blocks of member states, against a baseline per family
        if self.pairs:
            self._pair_update(on, unexposed, unknown, t)
        # triples (small n only)
        s, end = self.ranges[AND3]
        for h in range(s, end):
            m = self.masks[h]
            if m & unknown:
                continue
            c = (2 if m & unexposed else 1) if m & on == m else 0
            tc = t[c]
            for r in range(R):
                L[r][h] += tc[r]
        # the catch-all
        s, end = self.ranges[REST]
        if end > s:
            x = _popcount(on) / n if n else 0.0
            fv = 0.5 * x ** 3 + 0.5 * (1 - (1 - x) ** 3)
            tr = self._terms(crashed, e, scale, fv)
            for r in range(R):
                L[r][s] += tr[r]

    def _pair_update(self, on: int, unexposed: int, unknown: int, t: list[list[float]]) -> None:
        groups: list[list[int]] = [[], [], [], []]
        for i in range(self.n):
            bit = 1 << i
            groups[3 if unknown & bit else 0 if not on & bit else 2 if unexposed & bit else 1].append(i)
        sizes = [len(g) for g in groups]
        blocks = [(a, b) for a in range(4) for b in range(a, 4)]
        count = {(a, b): (sizes[a] * (sizes[a] - 1) // 2 if a == b else sizes[a] * sizes[b]) for a, b in blocks}
        cache: dict[tuple[int, int], list[int]] = {}
        pidx = self.pidx

        def pair_ids(a: int, b: int) -> list[int]:
            got = cache.get((a, b))
            if got is None:
                got = []
                A, B = groups[a], groups[b]
                if a == b:
                    for x, i in enumerate(A):
                        row = pidx[i]
                        got.extend([k for k in map(row.__getitem__, A[x + 1:]) if k >= 0])
                else:
                    for i in A:
                        row = pidx[i]
                        got.extend([k for k in map(row.__getitem__, B) if k >= 0])
                cache[(a, b)] = got
            return got

        R, L = self.R, self.L
        for code, table in ((AND, _AND_CLASS), (OR, _OR_CLASS)):
            s, end = self.ranges[code]
            if end <= s:
                continue
            per_class = [0, 0, 0, 0]
            for a, b in blocks:
                per_class[table[a][b]] += count[(a, b)]
            baseline = max(range(4), key=lambda c: (per_class[c], -c))
            tb = t[baseline]
            row_base = self.base[code]
            for r in range(R):
                row_base[r] += tb[r]
            for a, b in blocks:
                c = table[a][b]
                if c == baseline or not count[(a, b)]:
                    continue
                ids = pair_ids(a, b)
                if not ids:
                    continue
                tc = t[c]
                for r in range(R):
                    d = tc[r] - tb[r]
                    if d:
                        Lr = L[r]
                        for k in ids:
                            Lr[s + k] += d

    # ---- the posterior
    def _posterior(self) -> _Post:
        p, R = self.p, self.R
        rows = []
        for r in range(R):
            Lr = self.L[r]
            eff: list[float] = []
            for code in range(6):
                s, end = self.ranges[code]
                if end <= s:
                    continue
                b = self.base[code][r]
                eff.extend([x + b for x in Lr[s:end]] if b else Lr[s:end])
            rows.append(eff)
        mx = list(map(max, *rows)) if R > 1 else list(rows[0])
        best = max(mx) if mx else 0.0
        cut = best - p.prune
        alive = [h for h, m in enumerate(mx) if m >= cut]
        ph: dict[int, float] = {}
        prh: dict[int, list[float]] = {}
        pr = [0.0] * R
        z = 0.0
        exp = math.exp
        for h in alive:
            ws = [exp(rows[r][h] - best) for r in range(R)]
            s = sum(ws)
            ph[h] = s
            prh[h] = [x / s for x in ws] if s > 0 else [1.0 / R] * R
            for r in range(R):
                pr[r] += ws[r]
            z += s
        z = z or 1.0
        out = _Post()
        out.alive = alive
        out.ph = {h: v / z for h, v in ph.items()}
        out.pr = [x / z for x in pr]
        out.prh = prh
        out.r_mean = sum(q * r for q, r in zip(out.pr, self.grid))
        top, ptop = (alive[0], 0.0) if alive else (0, 0.0)
        for h in alive:
            if out.ph[h] > ptop:
                top, ptop = h, out.ph[h]
        out.top, out.ptop = top, ptop
        inv = [0.0] * self.n
        mass = [0.0] * self.n
        kinds, members = self.kinds, self.members
        for h, v in out.ph.items():
            if kinds[h] in (NONE, REST):
                continue
            mem = members[h]
            share = v / len(mem)
            light = v < WEAK
            for i in mem:
                inv[i] += v
                if not light:
                    mass[i] += share
        out.inv, out.mass = inv, mass
        self.last_ph = out.ph
        return out

    # ---- pivot pairs
    def _pivot_parts(self, x: int):
        """(crash nodes, fine nodes, edges) of unit x over the observations so far."""
        bit = 1 << x
        need = self.req_closure[x] & ~bit
        crashes, fines = [], []
        fw = self.p.fine_weight
        for k, o in enumerate(self.obs):
            if o.unknown & bit:
                continue
            if o.crashed:
                if o.on & bit and not o.unexposed & bit and o.source in CRASH_SOURCES:
                    crashes.append((k, (o.on | o.unknown) & ~bit))
            elif not o.on & bit and o.w >= fw and o.source in FINE_SOURCES:
                fines.append((k, o.on & ~o.unknown))
        edges = [(c, f) for c, sc in crashes for f, sf in fines if (sc | need) & ~sf == 0]
        return crashes, fines, edges, need

    def _pivot_extra(self, x: int, parts, extras: Sequence[tuple[int, bool]]) -> list[tuple[int, int]]:
        """The edges with hypothetical launches added: (``mask`` on, answered ``crashed``) each, in order."""
        crashes, fines, edges, need = parts
        bit = 1 << x
        if not extras:
            return edges
        crashes, fines, out = list(crashes), list(fines), list(edges)
        for k, (mask, crashed) in enumerate(extras):
            new = -1 - k  # (its observation number)
            if crashed:
                if mask & bit:
                    sc = mask & ~bit
                    out += [(new, f) for f, sf in fines if (sc | need) & ~sf == 0]
                    crashes.append((new, sc))
            elif not mask & bit:
                out += [(c, new) for c, sc in crashes if (sc | need) & ~mask == 0]
                fines.append((new, mask))
        return out

    @staticmethod
    def matching(edges: Sequence[tuple[int, int]], cap: int = 3) -> int:
        """The size of a maximum matching of pivot pairs (no observation twice), capped at ``cap``."""
        adj: dict[int, list[int]] = {}
        for c, f in edges:
            adj.setdefault(c, []).append(f)
        match: dict[int, int] = {}

        def augment(c, seen) -> bool:
            for f in adj[c]:
                if f in seen:
                    continue
                seen.add(f)
                if f not in match or augment(match[f], seen):
                    match[f] = c
                    return True
            return False

        size = 0
        for c in adj:
            if size >= cap:
                break
            if augment(c, set()):
                size += 1
        return size

    def pivots(self, unit: str, cap: int = 3) -> int:
        """Independent pivot pairs of ``unit`` over the observations so far (capped)."""
        i = self.index.get(unit)
        if i is None:
            return 0
        return self.matching(self._pivot_parts(i)[2], cap)

    def _none_evidence(self, extras: Sequence[tuple[int, bool]] = ()) -> int:
        count = sum(1 for o in self.obs if o.crashed and not o.on and not o.unknown and o.source in CRASH_SOURCES)
        return count + sum(1 for mask, crashed in extras if mask == 0 and crashed)

    def _evidence(self, h: int, cap: int = 3, parts: dict | None = None,
                  extras: Sequence[tuple[int, bool]] = ()) -> int:
        """How many wrong answers the naming of h survives, plus one (capped; its weakest member), with the
        hypothetical launches ``extras`` ((mask, crashed) each) added."""
        kind = self.kinds[h]
        if kind == REST:
            return 0
        if kind == NONE:
            return min(cap, self._none_evidence(extras))
        out = cap
        for i in self.members[h]:
            got = parts.get(i) if parts is not None else None
            if got is None:
                got = self._pivot_parts(i)
                if parts is not None:
                    parts[i] = got
            edges = self._pivot_extra(i, got, extras) if extras else got[2]
            out = min(out, self.matching(edges, cap))
            if not out:
                break
        return out

    def _controls(self) -> int:
        """Fine launches of the search with every mod off (long enough to count)."""
        fw = self.p.fine_weight
        return sum(1 for o in self.obs if o.source == "search" and not o.on and not o.crashed and o.w >= fw)

    def _need(self, r_mean: float) -> int:
        return 3 if self.strict or r_mean < self.p.r_confident else 2

    # ---- choosing the next launch
    def _candidates(self, post: _Post, top: int | None) -> list[tuple[int, str]]:
        """Launches worth looking at, in order (first one wins ties): prefixes of the units by
        suspicion (with what they need), their complements, prefixes with the cleared units on, the
        full set, nothing on, and launches around the top hypothesis."""
        n = self.n
        mass = post.mass
        order = sorted(range(n), key=lambda i: (-mass[i], i))
        out: list[tuple[int, str]] = []
        seen: set[int] = set()

        def add(m: int, why: str) -> None:
            if m not in seen:
                seen.add(m)
                out.append((m, why))

        for m, why in self._sequences(post, order):
            add(m, why)
        add(self.full, "full")
        add(0, "everything_off")
        if top is not None and self.kinds[top] not in (NONE, REST):
            m = self.masks[top]
            add(self._close(m), "confirm_crash")
            for i in self.members[top]:
                add(self._without(1 << i), "confirm_fine")
                if self.kinds[top] == OR:
                    add(self._close(1 << i), "confirm_crash")
            add(self._without(m), "confirm_fine")
        return out

    def _ks(self, post: _Post, order: list[int]) -> list[int]:
        n = self.n
        if n <= self.p.split_points:
            return list(range(1, n + 1))
        mass = post.mass
        acc, want, ks = 0.0, 1, list(range(1, 9))
        total = sum(mass) or 1.0
        for k, i in enumerate(order, 1):
            acc += mass[i]
            if acc >= want * total / self.p.split_points:
                ks.append(k)
                want = int(acc * self.p.split_points / total) + 1
        return sorted(set(k for k in ks if 1 <= k <= n))

    def _sequences(self, post: _Post, order: list[int]) -> list[tuple[int, str]]:
        """The prefix candidates: (top_k, rest_k) for every k, then top_k with the cleared units."""
        n = self.n
        ks = self._ks(post, order)
        tops, rests, cleared_tops = [], [], []
        total = sum(post.mass) or 1.0
        cleared = 0
        for i in range(n):
            if post.mass[i] < self.p.clear_mass * total / n:
                cleared |= 1 << i
        prefix, pos = 0, 0
        closed, off = 0, 0
        for k in ks:
            while pos < k:
                i = order[pos]
                prefix |= 1 << i
                closed |= self.req_closure[i]
                off |= self.dep_closure[i]
                pos += 1
            tops.append(closed)
            rests.append(self.full & ~off)
            cleared_tops.append(self._close(prefix | cleared))
        self._seq = (tops, rests, cleared_tops)
        out = []
        for a, b in zip(tops, rests):
            out.append((a, "top"))
            out.append((b, "rest"))
        out.extend((m, "top+cleared") for m in cleared_tops)
        return out

    def _gains(self, post: _Post, cands: list[tuple[int, str]]) -> dict[int, tuple[float, float]]:
        """Expected information gain (bits) about the cause and P(crash) of each candidate launch."""
        p, n = self.p, self.n
        e, b = p.e, p.b
        c0 = (1 - e) * b + e * (1 - b)
        hc0 = hb(c0)
        lik1 = [self._lik(True, e, 1.0, 1.0, r) for r in self.grid]
        kinds, masks = self.kinds, self.masks
        inc_and: list[list] = [[] for _ in range(n)]
        inc_or: list[list] = [[] for _ in range(n)]
        a_none = b_none = 0.0
        a_all = b_all = 0.0
        w_other = 0.0
        w_rest, prh_rest = 0.0, None
        for h, wh in post.ph.items():
            if wh < WEAK:
                continue
            kind = kinds[h]
            if kind == REST:
                w_rest, prh_rest = wh, post.prh[h]
                continue
            w_other += wh
            ph_r = post.prh[h]
            a1 = sum(q * x for q, x in zip(ph_r, lik1))
            alpha = wh * (a1 - c0)
            beta = wh * (hb(a1) - hc0)
            a_all += alpha
            b_all += beta
            if kind == NONE:
                a_none += alpha
                b_none += beta
                continue
            item = (masks[h], alpha, beta)
            target = inc_or if kind == OR else inc_and
            for i in self.members[h]:
                target[i].append(item)

        class State:
            __slots__ = ("s", "a", "b")

        def grow(st, u: int) -> None:
            old = st.s
            new = old | (1 << u)
            a, bb = st.a, st.b
            for m, x, y in inc_and[u]:
                if m & new == m:
                    a += x
                    bb += y
            for m, x, y in inc_or[u]:
                if not m & old:
                    a += x
                    bb += y
            st.s, st.a, st.b = new, a, bb

        def shrink(st, u: int) -> None:
            old = st.s
            new = old & ~(1 << u)
            a, bb = st.a, st.b
            for m, x, y in inc_and[u]:
                if m & old == m:
                    a -= x
                    bb -= y
            for m, x, y in inc_or[u]:
                if not m & new:
                    a -= x
                    bb -= y
            st.s, st.a, st.b = new, a, bb

        def empty_state():
            st = State()
            st.s, st.a, st.b = 0, a_none, b_none
            return st

        values: dict[int, tuple[float, float]] = {0: (a_none, b_none), self.full: (a_all, b_all)}

        def run(seq: Sequence[int]) -> None:
            st = empty_state()
            for target in seq:
                if target & st.s != st.s:  # (not growing: start again)
                    st = empty_state()
                for u in _bits(target & ~st.s):
                    grow(st, u)
                values.setdefault(target, (st.a, st.b))

        tops, rests, cleared = getattr(self, "_seq", ([], [], []))
        run(tops)
        run(list(reversed(rests)))
        run(cleared)
        for m, _why in cands:
            if m in values:
                continue
            if _popcount(m) * 2 <= n:
                st = empty_state()
                for u in _bits(m):
                    grow(st, u)
            else:
                st = State()
                st.s, st.a, st.b = self.full, a_all, b_all
                for u in _bits(self.full & ~m):
                    shrink(st, u)
            values[m] = (st.a, st.b)
        out: dict[int, tuple[float, float]] = {}
        for m, _why in cands:
            a, bb = values[m]
            pc = c0 * w_other + a
            cond = hc0 * w_other + bb
            if prh_rest is not None:
                x = _popcount(m) / n if n else 0.0
                fv = 0.5 * x ** 3 + 0.5 * (1 - (1 - x) ** 3)
                q = sum(pr * self._lik(True, e, 1.0, fv, r) for pr, r in zip(prh_rest, self.grid))
                pc += w_rest * q
                cond += w_rest * hb(q)
            out[m] = (hb(pc) - cond, pc)
        return out

    def _fix(self, post: _Post, mask: int) -> float:
        """P(the game runs fine with ``mask`` (and what needs it) off and everything else on)."""
        s = self._without(mask)
        return sum(w * (1.0 - self.f(h, s)) for h, w in post.ph.items())

    # ---- the decision
    def _suspect(self, h: int, p: float) -> Suspect:
        return Suspect(FAMILIES[self.kinds[h]], tuple(self.units[i] for i in self.members[h]), p)

    def _summary(self, post: _Post, d: Decision) -> Decision:
        ranked = sorted(((v, h) for h, v in post.ph.items() if self.kinds[h] != REST and v >= 0.01),
                        key=lambda x: (-x[0], x[1]))
        d.ranking = [self._suspect(h, v) for v, h in ranked[:5]]
        d.involvement = {u: post.inv[i] for i, u in enumerate(self.units)}
        d.suspects = sum(1 for x in post.inv if self.n and x >= 0.2 / self.n)
        d.r_mean = post.r_mean
        d.entropy = -sum(v * math.log2(v) for v in post.ph.values() if v > 0)
        top = post.top
        d.family = FAMILIES[self.kinds[top]] if self.H else ""
        if self.H and self.kinds[top] == NONE:
            d.sure = post.ptop
        elif self.H and self.kinds[top] != REST:
            d.sure = min(post.inv[i] for i in self.members[top])
        if not d.mods and self.H and self.kinds[top] not in (NONE, REST):
            d.mods = tuple(self.units[i] for i in self.members[top])
        return d

    def _remaining(self, post: _Post, need: int, launches: int, limit: int, skip: int | None = None) -> tuple[int, int]:
        """About how many launches are left (this one included), as (lo, hi); ``skip``: a unit known
        not to be the cause (its one-mod mass left out)."""
        s, end = self.ranges[ONE]
        probs = [post.ph.get(h, 0.0) for h in range(s, end)]
        if skip is not None and 0 <= skip < len(probs):
            probs[skip] = 0.0
        top_one = max(range(len(probs)), key=lambda i: (probs[i], -i)) if probs else None
        s0, e0 = self.ranges[NONE]
        if e0 > s0:
            probs.append(post.ph.get(s0, 0.0))
        z = sum(probs)
        n_eff = 1.0 / sum((x / z) ** 2 for x in probs) if z > 0 else 1.0
        piv = self.matching(self._pivot_parts(top_one)[2], need) if top_one is not None else 0
        p = self.p
        x = max(1, math.ceil(math.log2(max(1.0, n_eff)) - 1e-9) + (need - min(piv, need)) + p.est_shift)
        x += max(0, p.controls - self._controls())
        noisy = post.r_mean < p.r_confident and launches >= p.est_noisy_after
        lo = max(1, x - p.est_lo - (p.est_noisy_lo if noisy else 0))
        hi = x + p.est_hi + (p.est_noisy_hi if noisy else 0)
        left = max(1, limit - launches)
        return min(lo, left), max(min(hi, left), min(lo, left))

    def estimate(self, without: str | None = None, extra: int = 0) -> tuple[int, int]:
        """About how many launches are left now (lo, hi); ``without``: if that unit is not the cause."""
        post = self._posterior()
        launches = sum(1 for o in self.obs if o.source == "search")
        return self._remaining(post, self._need(post.r_mean), launches, budget(self.n, extra, self.p),
                               self.index.get(without) if without is not None else None)

    def summary(self) -> Decision:
        """The posterior as a Decision of kind "unsure" (no launch chosen): ranking, involvement and
        the top hypothesis (``mods``, ``sure``). Cheap next to decide()."""
        post = self._posterior()
        d = Decision("unsure", p=post.ptop)
        return self._summary(post, d)

    def p_of(self, family: str, mods: Sequence[str]) -> float:
        """P of a hypothesis in the posterior of the last decide()/summary() (0 if not alive)."""
        h = self.hypothesis(family, mods)
        return self.last_ph.get(h, 0.0) if h is not None else 0.0

    def hypothesis(self, family: str, mods: Sequence[str]) -> int | None:
        if family not in FAMILIES:
            return None
        code = FAMILIES.index(family)
        s, end = self.ranges[code]
        if end <= s:
            return None
        if code in (NONE, REST):
            return s
        idx = sorted(self.index[m] for m in mods if m in self.index)
        if code == ONE:
            return s + idx[0] if len(idx) == 1 else None
        if code in (AND, OR):
            if len(idx) != 2:
                return None
            k = self.pidx[idx[0]][idx[1]]
            return s + k if k >= 0 else None
        want = tuple(idx)
        for h in range(s, end):
            if self.members[h] == want:
                return h
        return None

    def decide(self, extra: int = 0) -> Decision:
        """What to do now: the next launch ("test") or the result."""
        p = self.p
        limit = budget(self.n, extra, p)
        search = [o for o in self.obs if o.source == "search"]
        launches = len(search)
        post = self._posterior()
        top, ptop = post.top, post.ptop
        kind = self.kinds[top] if self.H else REST
        nameable = kind != REST
        members = self.members[top] if nameable else ()
        parts: dict = {}
        robust = self._evidence(top, 3, parts) if nameable else 0
        need = self._need(post.r_mean)

        def finish(d: Decision) -> Decision:
            self._summary(post, d)
            if d.kind == "test":
                d.remaining = self._remaining(post, need, launches, limit)
            if not d.pivots and nameable:
                d.pivots = robust
            return d

        crashed_any = any(o.crashed for o in search)
        if p.dry_spell and search and not crashed_any:
            fulls = sum(1 for o in search if o.on == self.full and o.w >= p.fine_weight)
            if fulls >= 2 or launches >= limit:
                return finish(Decision("no_repro", p=ptop))
            if launches >= p.dry_spell:
                return finish(Decision("test", self.ids(self.full), "dry", p=ptop))
        if kind == NONE:
            sure = ptop
        else:
            sure = min(post.inv[i] for i in members) if members else 0.0
        named: Decision | None = None
        if nameable and sure >= p.tau and robust >= need:
            if kind == NONE:
                return finish(Decision("not_mods", p=ptop, pivots=robust))
            fix = self._fix(post, self.masks[top])
            named = Decision({ONE: "found", AND: "together", AND3: "together", OR: "each"}[kind],
                             mods=tuple(self.units[i] for i in members), p=sure, pivots=robust, fix=fix)
            if p.controls and self._controls() < p.controls and launches < limit:
                # (a graphics device error: is the game fine with every mod off? the crash may come and go)
                return finish(Decision("test", frozenset(), "control", mods=named.mods, p=ptop, pivots=robust,
                                       fix=fix))
            if (fix >= p.tau_fix and ptop >= p.tau_exact) or launches >= limit:
                named.can_confirm = robust < 3 and self._can_confirm(post, top, parts, robust)
                return finish(named)
        if named is None and launches >= limit:
            return finish(Decision("unsure" if crashed_any else "no_repro", p=ptop))
        cands = self._candidates(post, top if nameable and kind not in (NONE, REST) else None)
        gains = self._gains(post, cands)
        bonus = self._bonuses(post, cands)
        scored = [(gains[m][0] + bonus.get(m, 0.0), why, m) for m, why in cands]

        def best_of(items):
            return max(items, key=lambda s: (round(s[0], 9), _popcount(s[2])))

        if named is not None:
            if named.fix < p.tau_fix:  # something else may crash too: look at the rest with it off
                aimed = [s for s in scored if not s[2] & self.masks[top]]
                why = "others"
            else:  # which kind (alone, with a partner) is not clear yet
                aimed = scored
                why = "kind"
            if aimed:
                g, _w, m = best_of(aimed)
                if g >= p.min_gain:
                    test = Decision("test", self.ids(m), why, mods=named.mods, p=ptop, pivots=robust, fix=named.fix)
                    return finish(test)
            named.can_confirm = robust < 3 and self._can_confirm(post, top, parts, robust)
            return finish(named)
        confirming = nameable and sure >= p.confirm_at and robust < need
        if confirming:
            # likely, but it rests on too few independent answers: the launch that adds one
            aimed = []
            for s in scored:
                crash = self.f(top, s[2]) >= 0.5
                if self._evidence(top, 3, parts, [(s[2], crash)]) > robust:
                    aimed.append((s, crash))
            if aimed:
                (g, _w, m), crash = max(aimed, key=lambda a: (round(a[0][0], 9), _popcount(a[0][2])))
                return finish(Decision("test", self.ids(m), "confirm_crash" if crash else "confirm_fine", p=ptop))
            if self.strict:  # (한 번 더 확인: no one launch adds a pair, a few do - the first of them)
                test = self._sequence_test(top, parts, robust, ptop)
                if test is not None:
                    return finish(test)
        g, why, m = best_of(scored)
        if p.near_best < 1.0:
            g, why, m = max((s for s in scored if s[0] >= p.near_best * g), key=lambda s: (_popcount(s[2]), round(s[0], 9)))
        if g < p.min_gain:
            # nothing left to learn: the pairs a few launches add (a single unit that crashes now and then needs
            # three pairs, and no one launch adds the third), before giving up
            test = self._sequence_test(top, parts, robust, ptop) if confirming else None
            if test is not None:
                return finish(test)
            return finish(Decision("unsure" if crashed_any else "no_repro", p=ptop))
        reason = {"top": "split", "rest": "split", "top+cleared": "split"}.get(why, why)
        return finish(Decision("test", self.ids(m), reason, p=ptop))

    def _sequence_test(self, top: int, parts: dict, robust: int, ptop: float) -> Decision | None:
        """The first launch of _confirm_sequence as a test (None when there is none)."""
        seq = self._confirm_sequence(top, parts, robust)
        if not seq:
            return None
        m, crash = seq[0]
        return Decision("test", self.ids(m), "confirm_crash" if crash else "confirm_fine", p=ptop)

    def _bonuses(self, post: _Post, cands: list[tuple[int, str]]) -> dict[int, float]:
        """The pivot bonus of each candidate: for the top single suspects, the pivot pairs it adds if
        the answer comes as predicted (crash with the suspect on, fine with it off)."""
        p = self.p
        if not p.pivot_bonus:
            return {}
        s, end = self.ranges[ONE]
        ones = sorted(((post.ph[h], self.members[h][0]) for h in range(s, end) if h in post.ph), reverse=True)[:3]
        suspects = []
        for w, x in ones:
            if w < 0.1:
                continue
            parts = self._pivot_parts(x)
            now = self.matching(parts[2], 2)
            if now < 2:
                suspects.append((w, x, parts, now))
        out: dict[int, float] = {}
        if not suspects:
            return out
        for m, _why in cands:
            total = 0.0
            for w, x, parts, now in suspects:
                later = self.matching(self._pivot_extra(x, parts, [(m, bool(m >> x & 1))]), 2)
                total += p.pivot_bonus * w * (later - now)
            if total:
                out[m] = total
        return out

    def _can_confirm(self, post: _Post, top: int, parts: dict, robust: int) -> bool:
        """Launches exist that would add an independent pivot pair to the named hypothesis (one launch, or
        the few of _confirm_sequence: a strict search makes them)."""
        for m, _why in self._candidates(post, top):
            if self._evidence(top, 3, parts, [(m, self.f(top, m) >= 0.5)]) > robust:
                return True
        return self._confirm_sequence(top, parts, robust) is not None

    def _confirm_sequence(self, top: int, parts: dict, robust: int) -> list[tuple[int, bool]] | None:
        """Launches (mask, predicted crash) that together add a pivot pair to every member of hypothesis
        ``top`` when no single launch does, from where the search is now: for each member still at ``robust``
        pairs, its fine run alone when a crash it can pair with is there already (from the observations or the
        launches before it in the sequence), its crash alone when such a fine run is, else a crash and then its
        fine run. The crash is the members on with what they need, the fine run each member off with everything
        else on (for "each": each member alone on, then only what it needs). None when the hypothesis does not
        predict them so."""
        kind, mem = self.kinds[top], self.members[top]
        if kind not in (ONE, AND, AND3, OR):
            return None
        want = robust + 1
        seq: list[tuple[int, bool]] = []
        for i in mem:
            got = parts.get(i)
            if got is None:
                got = parts[i] = self._pivot_parts(i)
            if kind == OR:
                crash, fine = self._close(1 << i), self.req_closure[i] & ~(1 << i)
            else:
                crash, fine = self._close(self.masks[top]), self._without(1 << i)
            for more in ([], [(fine, False)], [(crash, True)], [(crash, True), (fine, False)]):
                if self.matching(self._pivot_extra(i, got, [*seq, *more]), want) >= want:
                    seq += more
                    break
            else:
                return None
        if not seq or any((self.f(top, m) >= 0.5) != crash for m, crash in seq):
            return None
        return seq if self._evidence(top, 3, parts, seq) > robust else None

    def confirm_launches(self) -> int:
        """How many launches adding a pivot pair to every member of the top hypothesis takes when the answers
        come as it predicts (one when a single launch does it, else the _confirm_sequence; 0: none do)."""
        post = self._posterior()
        top = post.top
        if not self.H or self.kinds[top] in (NONE, REST):
            return 0
        parts: dict = {}
        robust = self._evidence(top, 3, parts)
        for m, _why in self._candidates(post, top):
            if self._evidence(top, 3, parts, [(m, self.f(top, m) >= 0.5)]) > robust:
                return 1
        seq = self._confirm_sequence(top, parts, robust)
        return len(seq) if seq else 0


# ---- simulation ---------------------------------------------------------------------------------------
def simulate(units: Sequence[str], requires: Mapping[str, Sequence[str]] | None = None,
             crashes_with: Callable | None = None, *, content: Iterable[str] = (), prior: Prior | None = None,
             params: Params | None = None, passive: Sequence[Obs] = (), crash_times: Sequence[float] = (),
             strict: bool = False, limit: int = 80, extra: int = 0) -> tuple[Decision, list[Decision]]:
    """Run a search answered by ``crashes_with(on, k)`` (the units on, the launch number from 0) ->
    crashed, or (crashed, seconds or None, unexposed units). Without a report in ``passive`` the
    user's report (everything on crashed) comes first. Returns the final decision and the tests."""
    engine = Engine(units, requires, content=content, prior=prior, params=params, crash_times=crash_times,
                    strict=strict)
    if not any(o.source == "report" for o in passive):
        engine.add(Obs(frozenset(engine.units), True, e=0.02, source="report"))
    for o in passive:
        engine.add(o)
    steps: list[Decision] = []
    while len(steps) < limit:
        d = engine.decide(extra)
        if d.kind != "test":
            return d, steps
        got = crashes_with(d.on, len(steps)) if crashes_with is not None else False
        seconds, unexposed = None, frozenset()
        if isinstance(got, tuple):
            crashed = bool(got[0])
            seconds = got[1] if len(got) > 1 else None
            unexposed = frozenset(got[2]) if len(got) > 2 and got[2] else frozenset()
        else:
            crashed = bool(got)
        steps.append(d)
        engine.add(Obs(d.on, crashed, seconds=seconds, unexposed=unexposed))
    raise RuntimeError("the search did not end")
