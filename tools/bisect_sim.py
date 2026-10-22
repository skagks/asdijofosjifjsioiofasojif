"""원인 모드 찾기: simulated searches with the v2 engine (mhwmm.bisect_engine) against the shipped v2.2.3
search (mhwmm.bisect.simulate), for the acceptance scenarios of the v2.2.4 design. Not run by CI.

    python tools/bisect_sim.py --quick     S1 and S4 (and the remaining-launch estimate), a few minutes
    python tools/bisect_sim.py --all       every scenario
    python tools/bisect_sim.py --timing    how long decide() and a replay take (n = 50, 100, 200)
    python tools/bisect_sim.py S6 S7       only these scenarios

Everything is seeded: the same command prints the same numbers. "innocent" means a named set that is
not part of the real cause; launches are means. The searches use the rules of a search started now
(Params.current, and bisect_engine.WHEN_RULES for 가끔); "stored" rows use the field defaults, the rules of
a search stored before the launches with every mod off while "not the mods" keeps some probability.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from itertools import combinations
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mhwmm import bisect  # noqa: E402
from mhwmm.bisect_engine import Engine, Obs, Params, Prior, R_PRIORS, WHEN_RULES, budget, simulate  # noqa: E402

NAMED = ("found", "together", "each")
RARE = Params.current(r_prior=R_PRIORS["rare"], **WHEN_RULES["rare"])  # what bisect._settle_data uses for 가끔
STORED = Params()  # the rules of a search stored before Params.none_controls (no launches with every mod off)


def ids(n, prefix="u"):
    return [f"{prefix}{i}" for i in range(n)]


def det(cause, kind="and"):
    cause = set(cause)
    if kind == "or":
        return lambda on: bool(cause & set(on))
    if kind == "none":
        return lambda on: True
    return lambda on: cause <= set(on)


def named(d) -> set:
    return set(d.mods) if d.kind in NAMED else set()


def outcome(got: set, cause) -> str:
    if got == set(cause):
        return "right"
    if got and not got <= set(cause):
        return "innocent"
    return "part" if got else "none"


def run_old(units, base, answer, requires=None, split=None, core=None):
    """The shipped search answered by ``answer(k, on)``: (result, launches)."""
    answers, steps = [], []
    while True:
        nxt = bisect.next_step(units, base, core or base, requires, split, answers)
        if isinstance(nxt, bisect.Result):
            return nxt, len(steps)
        crashed = answer(len(steps), set(nxt.on))
        steps.append(nxt)
        answers.append((nxt.on, crashed))
        if len(steps) > 200:
            raise RuntimeError("the shipped search did not end")


def gfun(d, times, s=0.6):
    if d is None or not times:
        return 1.0
    return sum(0.5 * (1 + math.erf((math.log(max(d, 1.0)) - math.log(t)) / (s * math.sqrt(2)))) for t in times) / len(times)


def stats(rows):
    """rows: (launches, outcome) -> "mean 8.13 (p90 11, max 25), right 100.0%, innocent 0.0%"."""
    ls = sorted(r[0] for r in rows)
    res = [r[1] for r in rows]
    n = len(rows)
    return (f"{sum(ls) / n:.2f} launches (p90 {ls[int(0.9 * n)] if n > 1 else ls[0]}, max {ls[-1]}), "
            f"right {res.count('right') / n:.1%}, innocent {res.count('innocent') / n:.1%}")


# ---- S1-S3: exact answers -----------------------------------------------------------------------------
def exact_causes(n, kind, count=30, seed=0):
    us = ids(n)
    if n == 100 and kind == "one":
        return [(c,) for c in random.Random(100).sample(us, 20)]
    rnd = random.Random(seed + n)
    causes = [(u,) for u in us] if kind == "one" else list(combinations(us, 2))
    rnd.shuffle(causes)
    return causes[:count]


def exact_case(args):
    n, kind, c = args
    us = ids(n)
    units, req = ["refw", *us], {u: ["refw"] for u in us}
    truth = det(c, "or" if kind == "or" else "and")
    d, steps = simulate(units, req, lambda on, k: truth(on))
    res, launches = run_old(us, ["refw"], lambda k, on: truth(on), requires=req)
    return (len(steps), outcome(named(d), c)), (launches, outcome(set(res.mods) if res.can_disable else set(), c))


def scenario_exact(pool, sid, kind, ns, targets):
    lines = []
    for n in ns:
        rows = pool.map(exact_case, [(n, kind, c) for c in exact_causes(n, kind)], chunksize=1)
        new, old = [r[0] for r in rows], [r[1] for r in rows]
        mean = sum(r[0] for r in new) / len(new)
        lines.append(f"{sid} exact n={n} {kind}: engine {stats(new)} | shipped {stats(old)} "
                     f"[target {targets[n]}]" + _flag(mean, targets[n]))
    return lines


def _flag(mean, target_text):
    """" OK"/" MISS" for a "mean <= X" target at the start of target_text."""
    try:
        limit = float(target_text.split("mean <= ")[1].split()[0].rstrip(",;"))
    except (IndexError, ValueError):
        return ""
    return " OK" if mean <= limit + 1e-9 else " MISS"


# ---- S4: noisy answers -----------------------------------------------------------------------------------
def noisy_case(args):
    seed2, n, r_true, cause, params = args
    us = ids(n)
    units, req = ["refw", *us], {u: ["refw"] for u in us}
    truth = det(cause)

    def make(local):
        def answer(k, on):
            real = (truth(on) and local.random() < r_true) or local.random() < 0.02
            return real != (local.random() < 0.02)
        return answer

    a = make(random.Random(seed2))
    d, steps = simulate(units, req, lambda on, k: a(k, on), params=params)
    new = (len(steps), outcome(named(d), cause))
    a = make(random.Random(seed2))
    res, launches = run_old(us, ["refw"], lambda k, on: a(k, on), requires=req)
    old = (launches, outcome(set(res.mods) if res.can_disable else set(), cause))
    return new, old


def noisy(pool, n, r_true, cases=150, seed=7, params=None):
    rnd = random.Random(seed)
    us = ids(n)
    jobs = []
    for _ in range(cases):
        cause = (rnd.choice(us),)
        jobs.append((rnd.random(), n, r_true, cause, params))
    rows = pool.map(noisy_case, jobs, chunksize=4)
    return [r[0] for r in rows], [r[1] for r in rows]


# ---- S5: one wrong answer ----------------------------------------------------------------------------------
def safety_case(args):
    units, req, cause, kind = args
    truth = det(cause, kind)
    _d, steps = simulate(units, req, lambda on, k: truth(on))
    bad, runs = [], 0
    for i in range(len(steps) + 2):
        d, _s = simulate(units, req, lambda on, k, i=i: truth(on) != (k == i))
        runs += 1
        if _bad(d, cause, kind):
            bad.append((cause, kind, i, d.kind, sorted(named(d))))
    return bad, runs


def _bad(d, cause, kind) -> bool:
    got = named(d)
    return bool((got and not got <= set(cause)) or (d.kind == "not_mods" and kind != "none")
                or (len(cause) == 1 and d.kind in ("together", "each")) or (kind == "or" and d.kind == "together"))


def safety_jobs(max_n=6):
    jobs = []
    for n in range(1, max_n + 1):
        us = ids(n)
        units = ["refw", "lib", *us]
        req = {"lib": ["refw"], **{u: ["refw"] for u in us}}
        req["u0"] = ["refw", "lib"]
        causes = [((u,), "and") for u in us] + [(("refw",), "and"), (("lib",), "and")]
        causes += [(c, "and") for c in list(combinations(us, 2))[:12]]
        causes += [(c, "or") for c in list(combinations(us, 2))[:6]]
        causes += [(c, "and") for c in list(combinations(us, 3))[:4]]
        causes += [((), "none")]
        jobs += [(units, req, c, k) for c, k in causes]
    return jobs


def sampled_case(args):
    seed = args
    rnd = random.Random(seed)
    us = ids(20)
    units = ["refw", "lib", *us]
    req = {"lib": ["refw"], **{u: ["refw"] for u in us}}
    for u in us[:4]:
        req[u] = ["refw", "lib"]
    x = rnd.random()
    if x < 0.5:
        cause, kind = (rnd.choice(us),), "and"
    elif x < 0.7:
        cause, kind = tuple(rnd.sample(us, 2)), "and"
    elif x < 0.85:
        cause, kind = tuple(rnd.sample(us, 2)), "or"
    elif x < 0.9:
        cause, kind = ("lib",), "and"
    elif x < 0.95:
        cause, kind = tuple(rnd.sample(us, 3)), "and"
    else:
        cause, kind = (), "none"
    truth = det(cause, kind)
    _d, steps = simulate(units, req, lambda on, k: truth(on))
    bad, runs = [], 0
    for i in sorted(rnd.sample(range(len(steps) + 1), min(4, len(steps) + 1))):
        d, _s = simulate(units, req, lambda on, k, i=i: truth(on) != (k == i))
        runs += 1
        if _bad(d, cause, kind):
            bad.append((cause, kind, i, d.kind, sorted(named(d))))
    return bad, runs


# ---- S6, S7: one kind of evidence at a time (n = 30) -----------------------------------------------------------
def pointer_case(args):
    seed, scenario, noise = args
    rnd = random.Random(seed)
    n = 30
    us = ids(n)
    units = ["refw", *us]
    req = {u: ["refw"] for u in us}
    c = rnd.choice(us)
    innocent = rnd.choice([u for u in us if u != c])
    prior, passive = None, []
    if scenario == "pointer_right":
        prior = Prior(shares={c: 0.5})
    elif scenario == "pointer_wrong":
        prior = Prior(shares={innocent: 0.5})
    elif scenario in ("last_good_changed", "last_good_unchanged"):
        changed = set(rnd.sample([u for u in us if u != c], 2))
        changed.add(c if scenario == "last_good_changed" else innocent)
        good = frozenset(u for u in units if u not in changed)
        rho = 0.8 if scenario == "last_good_changed" else 0.15  # (unchanged culprit: a game update since)
        passive = [Obs(frozenset(units), True, e=0.02, source="report"), Obs(good, False, rho=rho, source="last_good")]
    elif scenario == "stale_three":
        flagged = rnd.sample([u for u in us if u != c], 2) + [c]
        prior = Prior(weights={u: 4.0 for u in flagged})
    ans_seed = rnd.random()
    local = random.Random(ans_seed)

    def answer(on, k):
        if noise >= 1:
            return c in on
        real = (c in on and local.random() < noise) or local.random() < 0.02
        return real != (local.random() < 0.02)

    d, steps = simulate(units, req, answer, prior=prior, passive=passive)
    return len(steps), outcome(named(d), (c,))


def pointer100_case(args):
    seed, scenario, r = args
    rnd = random.Random(seed)
    n = 100
    us = ids(n)
    units = ["refw", *us]
    req = {u: ["refw"] for u in us}
    c = rnd.choice(us)
    innocent = rnd.choice([u for u in us if u != c])
    target = c if scenario == "right" else innocent if scenario == "wrong" else None
    prior = Prior(shares={target: 0.5}) if target else None
    local = random.Random(rnd.random())

    def answer(on, k):
        return ((c in on and local.random() < r) or local.random() < 0.02) != (local.random() < 0.02)

    d, steps = simulate(units, req, answer, prior=prior)
    return len(steps), outcome(named(d), (c,))


# ---- S8: mixed realistic setups (port of the engine designer's evidence_sim) -------------------------------------
KINDS = (("plugin", 0.06), ("script", 0.14), ("pak", 0.15), ("rsz", 0.20), ("tex", 0.45))


def mixed_case(args):
    seed, n, misleading, logging, use = args
    rnd = random.Random(seed)
    units, kind, requires = ["refw", "lib"], {"refw": "refw", "lib": "lib"}, {"lib": ["refw"]}
    for i in range(n):
        u = f"m{i}"
        x, acc = rnd.random(), 0.0
        for k, p in KINDS:
            acc += p
            if x <= acc:
                break
        units.append(u)
        kind[u] = k
        requires[u] = ["refw"] + (["lib"] if k == "script" and rnd.random() < 0.4 else [])
    mods = units[2:]
    content = [u for u in mods if kind[u] in ("pak", "rsz", "tex")]
    x = rnd.random()
    game_update = rnd.random() < 0.3
    if x < 0.04:
        cause, fam = (), "none"
    elif x < 0.09:
        cause, fam = ("refw",), "and"
    elif x < 0.17:
        cause, fam = tuple(rnd.sample(mods, 2)), "and"
    elif x < 0.24:
        cause, fam = tuple(rnd.sample(mods, 2)), "or"
    else:
        if game_update and rnd.random() < 0.6:
            pool = [u for u in mods if kind[u] in ("rsz", "plugin", "script")] or mods
        else:
            pool = mods
        cause, fam = (rnd.choice(pool),), "and"
    truth = det(cause, fam)
    weights: dict[str, float] = {}

    def mult(u, lr):
        weights[u] = min(50.0, weights.get(u, 1.0) * lr)

    culprits = set(cause)
    for c in sorted(culprits):
        if kind.get(c) == "plugin" and rnd.random() < 0.7:
            mult(c, 20)
        if kind.get(c) == "script" and rnd.random() < 0.3:
            mult(c, 8)
    if rnd.random() < misleading:
        mult(rnd.choice([u for u in mods if u not in culprits]), 20)
    for u in mods:
        if kind[u] != "rsz":
            continue
        if (u in culprits and rnd.random() < 0.7) or (game_update and rnd.random() < 0.15) or rnd.random() < 0.03:
            mult(u, 4)
    passive = []
    if rnd.random() < 0.6:
        k = rnd.randint(1, 5)
        if fam != "none" and "refw" not in culprits and not game_update and rnd.random() < 0.85:
            changed = set(rnd.sample([u for u in mods if u not in culprits], max(0, k - len(culprits)))) | culprits
        else:
            changed = set(rnd.sample(mods, k))
        good = [u for u in units if u not in changed]
        if game_update or not truth(set(good)):
            passive.append(Obs(frozenset(good), False, rho=0.15 if game_update else 0.8, source="last_good"))
    unexposed = set()
    if rnd.random() < logging and fam != "none":
        for u in content:
            if not rnd.random() < (0.97 if u in culprits else 0.4):
                unexposed.add(u)
        for c in sorted(culprits):
            if c in content and c not in unexposed and rnd.random() < 0.6:
                mult(c, 6)
        for u in content:
            if u not in culprits and u not in unexposed and rnd.random() < 0.03:
                mult(u, 6)
    ans_seed = rnd.random()

    def answerer():
        local = random.Random(ans_seed)

        def answer(on, k):
            real = (truth(on) and local.random() < 0.95) or local.random() < 0.02
            return real != (local.random() < 0.02)
        return answer

    if use == "old":
        a = answerer()
        res, launches = run_old(mods, ["refw", "lib"], lambda k, on: a(on, k), requires=requires, core=["refw", "lib"])
        got = set(res.mods) if res.can_disable else set()
        if fam == "none":
            return launches, "right" if res.kind == "not_mods" else "innocent" if got else "none"
        return launches, outcome(got, cause) if res.kind != "not_mods" else "innocent"
    report = Obs(frozenset(units), True, e=0.02, unexposed=frozenset(unexposed) if use == "new" else frozenset(),
                 source="report")
    prior = Prior(weights=weights) if use == "new" else None
    d, steps = simulate(units, requires, answerer(), content=content, prior=prior,
                        passive=[report] + (passive if use == "new" else []))
    got = named(d)
    if fam == "none":
        return len(steps), "right" if d.kind == "not_mods" else "innocent" if got else "none"
    return len(steps), outcome(got, cause) if d.kind != "not_mods" else "innocent"


# ---- S9, S11: crash times and older launches (port of time_sim) ------------------------------------------------------
def lognorm(rnd, median, s):
    return math.exp(math.log(median) + s * rnd.gauss(0, 1))


def time_case(args):
    seed, n, mode, use = args
    rnd = random.Random(seed)
    us = ids(n)
    units = ["refw", *us]
    req = {u: ["refw"] for u in us}
    culprit = rnd.choice(us)
    ans_seed = rnd.random()
    hist_seed = rnd.random()
    t0 = lognorm(random.Random(seed + 1), 480, 0.5)

    def launch(on, local, times):
        cause_on = culprit in on
        if mode == "times":
            T, d = lognorm(local, 480, 0.5), lognorm(local, 600, 0.6)
            crashed = (cause_on and T <= d) or local.random() < 0.02
            crashed = crashed != (local.random() < 0.02)
            if crashed:
                times.append(min(T, d))
                return crashed, min(T, d)
            return crashed, d
        crashed = (cause_on and local.random() < 0.95) or local.random() < 0.02
        return crashed != (local.random() < 0.02), None

    local = random.Random(ans_seed)
    old_times = [t0]
    res, launches = run_old(us, ["refw"], lambda k, on: launch(set(on), local, old_times)[0], requires=req)
    got = set(res.mods) if res.can_disable else set()
    shipped = (launches, "right" if got == {culprit} else "innocent" if got else "none")
    passive = []
    if mode == "history" and use:
        h = random.Random(hist_seed)
        for _ in range(6):
            off = set(h.sample(us, h.randint(0, int(0.3 * n))))
            on = set(units) - off
            crashed = (culprit in on and h.random() < 0.6) or h.random() < 0.02
            passive.append(Obs(frozenset(on), crashed, rho=0.6, source="history"))
    local = random.Random(ans_seed)
    times = [t0]

    def answer(on, k):
        crashed, seconds = launch(set(on), local, times)
        return crashed, (seconds if mode == "times" and use else None), frozenset()

    d, steps = simulate(units, req, answer, passive=passive,
                        crash_times=[t0] if mode == "times" and use else ())
    got = named(d)
    return shipped, (len(steps), "right" if got == {culprit} else "innocent" if got else "none")


# ---- S10: the access log (port of lead_expo_mid) -------------------------------------------------------------------
def expo_case(args):
    seed, n, reach, use, innocent_reach = args
    rnd = random.Random(seed)
    us = ids(n)
    units = ["refw", *us]
    req = {u: ["refw"] for u in us}
    culprit = rnd.choice(us)
    content = set(rnd.sample(us, int(0.7 * n))) | {culprit}
    local = random.Random(rnd.random())
    report_unexposed = frozenset()
    if use:
        report_unexposed = frozenset(u for u in sorted(content) if u != culprit and local.random() > innocent_reach)
    # (the prototype drew the report's exposure from the same stream as the answers)
    report = Obs(frozenset(units), True, e=0.02, unexposed=report_unexposed, source="report")

    def answer(on, k):
        unexposed, effective = set(), set(on)
        for u in sorted(content):
            if u in on:
                reached = local.random() < (reach if u == culprit else innocent_reach)
                logged = reached and not (u == culprit and local.random() < 0.03)
                if not logged:
                    unexposed.add(u)
                if u == culprit and not reached:
                    effective.discard(culprit)
        crashed = (culprit in effective and local.random() < 0.95) or local.random() < 0.02
        crashed = crashed != (local.random() < 0.02)
        return crashed, None, frozenset(unexposed) if use else frozenset()

    d, steps = simulate(units, req, answer, content=sorted(content), passive=[report])
    got = named(d)
    return len(steps), "right" if got == {culprit} else "innocent" if got else "none"


# ---- S14: a graphics error record (DEVICE_HUNG ...) -------------------------------------------------------------------
# v2.2.5 read it as "usually not the mods": the "rare" prior, two fine launches with every mod off before a mod is named
# (Params.controls) and "not the mods" x4 (W9, W9b), render data x1.5, scripts and plugins x0.7. v2.2.6 reads it as a
# crash like any other (in a modded game the "badly formed commands" behind it often come from a mod's render data):
# the prior of 언제 튕기나요 ("same" -> middle, 가끔 -> rare), no controls, no "not the mods" weight, render data x2.5,
# Lua scripts and data files x0.8 (evidence.graphics), on top of the kind of mod (W17)
V225 = Params(r_prior=R_PRIORS["rare"], controls=2)  # what v2.2.5's bisect._settle_data stored for such a record
DEVICE_KINDS = ["render"] * 12 + ["script"] * 4 + ["plugin"] * 2 + ["data"] * 2  # 20 mods (and REFramework)
DEVICE_SETUPS = {  # (params, weight by kind, "not the mods" scale)
    "v2.2.5": (V225, {"render": 1.5, "script": 0.7 * 1.3, "plugin": 0.7 * 2.0, "data": 1.5}, 4.0),
    "v2.2.6 same": (None, {"render": 2.5, "script": 0.8 * 1.3, "plugin": 2.0, "data": 0.8 * 1.5}, 1.0),
    "v2.2.6 rare": (RARE, {"render": 2.5, "script": 0.8 * 1.3, "plugin": 2.0, "data": 0.8 * 1.5}, 1.0)}
# (v2.2.6 rows: the current rules - launches with every mod off while "not the mods" keeps some probability; S17)


def device_case(args):
    """refw + 20 mods (DEVICE_KINDS) after a graphics error record; the cause one mod of ``cause``'s kind, or "none"
    (the game crashes whatever is on); crashes with rate r (1.0: exact answers). Returns (launches, outcome)."""
    seed, r, cause, setup = args
    params, weights, none = DEVICE_SETUPS[setup]
    rnd = random.Random(seed)
    us = ids(len(DEVICE_KINDS))
    units, req = ["refw", *us], {u: ["refw"] for u in us}
    kind = dict(zip(us, DEVICE_KINDS))
    prior = Prior(weights={u: weights[kind[u]] for u in us}, none_scale=none)
    c = None if cause == "none" else rnd.choice([u for u in us if kind[u] == cause])
    local = random.Random(rnd.random())

    def answer(on, k):
        if r >= 1:
            return c is None or c in on
        real = ((c is None or c in on) and local.random() < r) or local.random() < 0.02
        return real != (local.random() < 0.02)

    d, steps = simulate(units, req, answer, params=params, prior=prior)
    if c is None:
        return len(steps), "right" if d.kind == "not_mods" else "innocent" if named(d) else "none"
    return len(steps), outcome(named(d), (c,))


# ---- S17: not the mods, crashing now and then whatever is on (v2.2.6) ------------------------------------------------
# refw + 20 mods (DEVICE_KINDS). Starts: norec - no crash record (the user's report only, every mod suspected the same);
# ord - an ordinary record (weights by the kind of mod, W17); mis - a misleading record (W17 and a strong pointer, share
# 0.5, at a mod that is not the cause); S14 - a graphics error record as v2.2.6 reads it; S14r - the same and 가끔.
# A crash that comes and goes whatever is on can look like any mod: the current rule asks for launches with every mod
# off while "not the mods" keeps P >= Params.none_controls (up to max_controls fine ones) before a mod is named
W17 = {"render": 1.0, "script": 1.3, "plugin": 2.0, "data": 1.5}
STARTS = ("norec", "ord", "mis", "S14", "S14r")


def notmods_case(args):
    """Returns (launches, outcome, control launches): the cause is "none" (outcome right = 'not the mods') or one mod."""
    seed, r, start, cause, rule = args
    rnd = random.Random(seed)
    us = ids(len(DEVICE_KINDS))
    kind = dict(zip(us, DEVICE_KINDS))
    c = None if cause == "none" else rnd.choice(us)
    pointed = rnd.choice([u for u in us if u != c])
    rare = start == "S14r"
    if start == "norec":
        prior = None
    elif start in ("ord", "mis"):
        prior = Prior(weights={u: W17[kind[u]] for u in us}, shares={pointed: 0.5} if start == "mis" else {})
    else:
        prior = Prior(weights={u: DEVICE_SETUPS["v2.2.6 same"][1][kind[u]] for u in us})
    if rule == "stored":
        params = Params(r_prior=R_PRIORS["rare"]) if rare else STORED
    else:
        params = RARE if rare else Params.current()
    local = random.Random(rnd.random())

    def answer(on, k):
        if r >= 1:
            return c is None or c in on
        real = ((c is None or c in on) and local.random() < r) or local.random() < 0.02
        return real != (local.random() < 0.02)

    d, steps = simulate(["refw", *us], {u: ["refw"] for u in us}, answer, params=params, prior=prior)
    got = named(d)
    controls = sum(1 for s in steps if s.reason == "control")
    if c is None:
        return len(steps), "right" if d.kind == "not_mods" else "innocent" if got else d.kind, controls
    return len(steps), outcome(got, (c,)) if d.kind != "not_mods" else "not_mods", controls


def notmods_stats(rows):
    n = len(rows)
    res = [r[1] for r in rows]
    return (f"{sum(r[0] for r in rows) / n:.2f} launches ({sum(r[2] for r in rows) / n:.2f} with every mod off), "
            f"right {res.count('right') / n:.1%}, innocent {res.count('innocent') / n:.1%}, "
            f"unsure {res.count('unsure') / n:.1%}, no_repro {res.count('no_repro') / n:.1%}")


# ---- S15, S16: earlier sessions never make a pivot pair (v2.2.5) ------------------------------------------------------
def ordinary_play_case(args):
    """refw + 12 mods; 8 normal sessions (rho 0.6, an hour each) with u1 (installed lately: x2) off and the culprit u0 on
    but its content never reached; one wrong answer at each launch in turn. Returns (bad, runs)."""
    n, rows, seed = args
    rnd = random.Random(seed)
    us = ids(n)
    units, req = ["refw", *us], {u: ["refw"] for u in us}
    passive = [Obs(frozenset(units), True, e=0.005, seconds=600.0, source="report")]
    for _ in range(rows):
        off = {"u1"} | set(rnd.sample(us[2:], rnd.randint(0, 3)))
        passive.append(Obs(frozenset(units) - off, False, rho=0.6, seconds=3600.0, source="history"))
    prior = Prior(weights={"u1": 2.0})
    truth = det(("u0",))
    _d, steps = simulate(units, req, lambda on, k: truth(on), prior=prior, passive=passive, crash_times=[600.0])
    bad, runs = [], 0
    for i in range(-1, len(steps) + 2):
        d, _s = simulate(units, req, lambda on, k, i=i: truth(on) != (k == i), prior=prior, passive=passive,
                         crash_times=[600.0])
        runs += 1
        if named(d) and not named(d) <= {"u0"}:
            bad.append((rows, i, d.kind, sorted(named(d))))
    return bad, runs


def old_crash_case(args):
    """refw + 12 mods, the culprit u11 installed after k older crashes of the same code (all other mods on): exact
    answers. Returns (launches, outcome)."""
    k, e, seed = args
    rnd = random.Random(seed)
    us = ids(12)
    units, req = ["refw", *us], {u: ["refw"] for u in us}
    c = us[rnd.randrange(12)]
    passive = [Obs(frozenset(units), True, e=0.005, source="report")]
    passive += [Obs(frozenset(units) - {c}, True, e=e, source="history") for _ in range(k)]
    d, steps = simulate(units, req, lambda on, kk: c in on, passive=passive)
    return len(steps), outcome(named(d), (c,))


# ---- S12, S13 -----------------------------------------------------------------------------------------------------
def never(n):
    us = ids(n)
    d, steps = simulate(["refw", *us], {u: ["refw"] for u in us}, lambda on, k: False)
    res, launches = run_old(us, ["refw"], lambda k, on: False, requires={u: ["refw"] for u in us})
    return f"n={n}: engine {len(steps)} -> {d.kind} | shipped {launches} -> {res.kind}", len(steps), d.kind


def random_answers(seed):
    rnd = random.Random(seed)
    n = rnd.randint(0, 14)
    us = ids(n)
    refw = rnd.random() < 0.8
    units = (["refw"] if refw else []) + us
    req = {u: ["refw"] for u in us} if refw else {}
    d, steps = simulate(units, req, lambda on, k: rnd.random() < 0.5)
    total = sum(s.p for s in d.ranking)
    return len(steps) <= budget(len(units)), n, len(steps), d.kind, total <= 1 + 1e-9


# ---- the remaining-launch estimate ------------------------------------------------------------------------------------
def estimate_case(args):
    n, culprit, r_true, seed, *rest = args  # (rest: "rare" - the parameters of 가끔)
    us = ids(n)
    units = ["refw", *us]
    req = {u: ["refw"] for u in us}
    local = random.Random(seed)

    def answer(on, k):
        if r_true >= 1:
            return culprit in on
        real = (culprit in on and local.random() < r_true) or local.random() < 0.02
        return real != (local.random() < 0.02)

    d, steps = simulate(units, req, answer, params=RARE if rest and rest[0] == "rare" else None)
    total = len(steps)
    out = []
    for k, s in enumerate(steps):
        actual = total - k  # this launch and the ones after it
        lo, hi = s.remaining
        out.append((lo <= actual <= hi, abs((lo + hi) / 2 - actual)))
    return out


def scenario_estimate(pool):
    lines = []
    for label, jobs, err in (
            ("S1 n=10", [(10, f"u{i}", 1.0, i) for i in range(10)], 2.0),
            ("S1 n=30", [(30, f"u{i}", 1.0, i) for i in range(0, 30, 2)], 2.0),
            ("S4 n=20 r=0.95", [(20, f"u{i % 20}", 0.95, 100 + i) for i in range(60)], 2.0),
            # (a per-bucket median oracle gets about 3.9 there: the answers are noisy)
            ("S4 n=20 r=0.7", [(20, f"u{i % 20}", 0.7, 200 + i) for i in range(60)], None),
            # 가끔 (RARE): launches with every mod off come before nearly every naming (Params.none_controls 0.002);
            # at r=0.95 the rare prior's E[r] widens the range early (the stored rule, no such launches: 2.56)
            ("가끔 n=20 exact", [(20, f"u{i % 20}", 1.0, 300 + i, "rare") for i in range(40)], 2.0),
            ("가끔 n=20 r=0.95", [(20, f"u{i % 20}", 0.95, 400 + i, "rare") for i in range(60)], 3.0)):
        rows = [x for r in pool.map(estimate_case, jobs, chunksize=2) for x in r]
        inside = sum(1 for ok, _d in rows if ok) / len(rows)
        mean = sum(d for _ok, d in rows) / len(rows)
        lines.append(f"estimate {label}: actual in [lo, hi] for {inside:.0%} of {len(rows)} steps, "
                     f"mean |mid - actual| {mean:.2f} [target >= 60%"
                     + (f", <= {err:g}]" if err is not None else "; an error target is out of reach with these inputs]")
                     + (" OK" if inside >= 0.6 and (err is None or mean <= err) else " MISS"))
    return lines


# ---- timing ----------------------------------------------------------------------------------------------------------
def timing() -> list[str]:
    lines = []
    for n in (50, 100, 200):
        us = ids(n)
        units, req = ["refw", *us], {u: ["refw"] for u in us}
        c = f"u{n * 2 // 3}"
        t = time.perf_counter()
        eng = Engine(units, req)
        build = time.perf_counter() - t
        eng.add(Obs(frozenset(units), True, e=0.02, source="report"))
        decide, add = [], []
        while True:
            t = time.perf_counter()
            d = eng.decide()
            decide.append(time.perf_counter() - t)
            if d.kind != "test":
                break
            o = Obs(d.on, c in d.on)
            t = time.perf_counter()
            eng.add(o)
            add.append(time.perf_counter() - t)
        rnd = random.Random(1)
        obs = [Obs(frozenset(units), True, source="report")] + [
            Obs(frozenset(u for u in units if rnd.random() < 0.5) | {"refw"}, rnd.random() < 0.5) for _ in range(20)]
        t = time.perf_counter()
        e2 = Engine(units, req)
        for o in obs:
            e2.add(o)
        replay = time.perf_counter() - t
        t = time.perf_counter()
        e2.decide()
        after = time.perf_counter() - t
        lines.append(f"T n={n}: {eng.H} causes, build {build:.2f}s, decide max {max(decide):.2f}s mean "
                     f"{sum(decide) / len(decide):.2f}s, add max {max(add, default=0):.4f}s, replay of 20 {replay:.2f}s, "
                     f"decide after it {after:.2f}s -> {d.kind} {list(d.mods)} "
                     f"[target decide <= {0.4 if n == 100 else 2.5 if n == 200 else '-'}s, replay <= 0.2s at n=200; "
                     f"Python {sys.version.split()[0]}]")
    return lines


# ---- the scenarios ------------------------------------------------------------------------------------------------------
def run(names: list[str], workers: int) -> None:
    with Pool(workers) as pool:
        def show(*lines):
            for line in lines:
                print(line, flush=True)

        t0 = time.time()
        # (v2.2.6: the targets of searches that end short include the launches with every mod off asked for before a
        # mod is named while "not the mods" keeps P >= Params.none_controls - about +0.5 on average, see S17)
        if "S1" in names:
            show(*scenario_exact(pool, "S1", "one", (10, 30, 100), {
                10: "mean <= 6.1, max <= 7, right 100%", 30: "mean <= 7.7, max <= 9, right 100%",
                100: "mean <= 9.9, max <= 11, right 100%"}))
        if "S2" in names:
            show(*scenario_exact(pool, "S2", "and", (10, 30), {
                10: "mean <= 16.0, right >= 95%, innocent 0", 30: "mean <= 19.0, right >= 95%, innocent 0"}))
        if "S3" in names:
            show(*scenario_exact(pool, "S3", "or", (10, 30), {
                10: "mean <= 13.2, right >= 95%, innocent 0", 30: "mean <= 17.0, right >= 93%, innocent 0"}))
        if "S4" in names:
            for r, target in ((0.95, "right >= 97%, innocent <= 1.0%, mean <= 8.9"),
                              (0.7, "right >= 88%, innocent <= 2.0%, mean <= 13.3"),
                              (0.5, "right >= 58%, innocent <= 5.0%, mean <= 18.0")):
                new, old = noisy(pool, 20, r)
                show(f"S4 noisy n=20 r={r}: engine {stats(new)} | shipped {stats(old)} [target {target}]")
        if "S4r" in names:
            for r, target in ((0.7, "innocent <= 1.5%"), (0.5, "innocent <= 3.5%")):
                new, _old = noisy(pool, 20, r, params=RARE)
                show(f"S4r noisy n=20 r={r}, rare prior: engine {stats(new)} [target {target}]")
        if "S5" in names:
            got = pool.map(safety_case, safety_jobs(6), chunksize=2)
            bad = [b for r in got for b in r[0]]
            runs = sum(r[1] for r in got)
            got2 = pool.map(sampled_case, range(3000, 3060))
            bad2 = [b for r in got2 for b in r[0]]
            runs2 = sum(r[1] for r in got2)
            show(f"S5 one wrong answer: exhaustive n<=6 {len(bad)} bad / {runs} runs; sampled n=20 {len(bad2)} bad / "
                 f"{runs2} runs [target 0]" + (f" e.g. {(bad + bad2)[:3]}" if bad or bad2 else " OK"))
        if "S6" in names:
            res = {}
            for scenario in ("none", "pointer_right", "pointer_wrong"):
                res[scenario] = pool.map(pointer_case, [(i, scenario, 1.0) for i in range(60)])
            mean = {k: sum(r[0] for r in v) / len(v) for k, v in res.items()}
            show(f"S6 pointer share 0.5, n=30, exact: none {stats(res['none'])} | right {stats(res['pointer_right'])} | "
                 f"wrong {stats(res['pointer_wrong'])} [target right mean <= 3.5, wrong <= none + 2.0 = "
                 f"{mean['none'] + 2:.2f}, innocent 0]"
                 + (" OK" if mean["pointer_right"] <= 3.5 and mean["pointer_wrong"] <= mean["none"] + 2 else " MISS"))
        if "S6b" in names:
            res = {}
            for scenario in ("none", "right", "wrong"):
                res[scenario] = pool.map(pointer100_case, [(i, scenario, 0.95) for i in range(80)])
            mean = {k: sum(r[0] for r in v) / len(v) for k, v in res.items()}
            innocent = sum(1 for v in res.values() for r in v if r[1] == "innocent")
            show(f"S6b pointer share 0.5, n=100, r=0.95: none {stats(res['none'])} | right {stats(res['right'])} | "
                 f"wrong {stats(res['wrong'])} [target right mean <= 3.8, wrong <= none + 1.5 = {mean['none'] + 1.5:.2f}, "
                 f"innocent <= 1 case]"
                 + (" OK" if mean["right"] <= 3.8 and mean["wrong"] <= mean["none"] + 1.5 and innocent <= 1 else " MISS"))
        if "S7" in names:
            a = pool.map(pointer_case, [(i, "last_good_changed", 1.0) for i in range(60)])
            b = pool.map(pointer_case, [(i, "last_good_unchanged", 1.0) for i in range(60)])
            ma, mb = sum(r[0] for r in a) / 60, sum(r[0] for r in b) / 60
            show(f"S7 last_good n=30 exact: culprit changed {stats(a)} | unchanged after a game update {stats(b)} "
                 f"[target mean <= 7.0 / <= 7.8, right 100%]" + (" OK" if ma <= 7.0 and mb <= 7.8 else " MISS"))
        if "S8" in names:
            for mis, target in ((0.1, 9.4), (0.4, 9.7)):
                parts = []
                for use in ("old", "new", "plain"):
                    rows = pool.map(mixed_case, [(1000 + i, 30, mis, 0.5, use) for i in range(160)], chunksize=4)
                    parts.append(f"{ {'old': 'shipped', 'new': 'engine', 'plain': 'engine without evidence'}[use]} "
                                 f"{stats(rows)}")
                show(f"S8 mixed n=30 misleading={mis}: " + " | ".join(parts)
                     + f" [target with evidence mean <= {target}, right >= 96%, innocent <= 0.7%; without mean <= 11.4]")
        if "S9" in names:
            for use in (True, False):
                rows = pool.map(time_case, [(5000 + i, 20, "times", use) for i in range(150)], chunksize=4)
                show(f"S9 crash ~8 min in, play ~10 min, {'with' if use else 'without'} g: engine "
                     f"{stats([r[1] for r in rows])} | shipped {stats([r[0] for r in rows])}"
                     + (" [target right >= 92%, innocent <= 2.0%]" if use else ""))
        if "S10" in names:
            for reach in (1.0, 0.8, 0.5):
                parts = []
                means = {}
                for use in (True, False):
                    rows = pool.map(expo_case, [(9000 + i, 20, reach, use, 0.5) for i in range(150)], chunksize=4)
                    means[use] = sum(r[0] for r in rows) / len(rows)
                    parts.append(f"log {'on' if use else 'off'} {stats(rows)}")
                target = {1.0: "S10b: see below", 0.8: "S10: right >= 95%, innocent <= 1.0%, mean <= 10.0",
                          0.5: "-"}[reach]
                show(f"S10 access log, culprit reached {reach:.0%} (innocent content 50%): " + " | ".join(parts)
                     + f" [{target}]")
            # S10b on 600 cases (150 vary by about +-0.3 launches from seed to seed)
            means = {}
            for use in (True, False):
                rows = pool.map(expo_case, [(base + i, 20, 1.0, use, 0.5) for base in (9000, 19000, 29000, 39000)
                                            for i in range(150)], chunksize=8)
                means[use] = sum(r[0] for r in rows) / len(rows)
            show(f"S10b culprit always reached, 600 cases: log on {means[True]:.2f} | log off {means[False]:.2f} "
                 f"[target log-on mean <= log-off + 1.0 = {means[False] + 1:.2f}]"
                 + (" OK" if means[True] <= means[False] + 1 else " MISS"))
        if "S11" in names:
            for use in (True, False):
                rows = pool.map(time_case, [(5000 + i, 20, "history", use) for i in range(150)], chunksize=4)
                show(f"S11 history, {'6 rows' if use else 'none'}: engine {stats([r[1] for r in rows])} | shipped "
                     f"{stats([r[0] for r in rows])}" + (" [target mean <= 7.3, right >= 99%]" if use else ""))
        if "S12" in names:
            got = [never(n) for n in (5, 20, 60)]
            ok = all(k <= 6 and kind == "no_repro" for _l, k, kind in got)
            show("S12 never crashes: " + " ; ".join(line for line, _k, _d in got) + " [target no_repro within 6]"
                 + (" OK" if ok else " MISS"))
        if "S13" in names:
            got = pool.map(random_answers, range(300), chunksize=10)
            over = [g for g in got if not g[0]]
            sums = [g for g in got if not g[4]]
            show(f"S13 random answers, 300 seeds, n=0..14: {len(over)} over budget, rankings summing over 1: {len(sums)} "
                 f"[target 0]" + (" OK" if not over and not sums else f" MISS {over[:3]}"))
        if "S14" in names:
            for cause, rates, what in (("render", (1.0, 0.95, 0.7), "a mod with render data is the cause"),
                                       ("script", (1.0, 0.7), "a Lua script is the cause (the record misleads)"),
                                       ("none", (0.95, 0.7, 0.5), "not the mods: crashes whatever is on")):
                for r in rates:
                    parts = []
                    for setup in DEVICE_SETUPS:
                        rows = pool.map(device_case, [(7000 + i, r, cause, setup) for i in range(100)], chunksize=4)
                        parts.append(f"{setup} {stats(rows)}")
                    show(f"S14 graphics error record, n=20, {what}, r={r}: " + " | ".join(parts)
                         + (" [right = 'not the mods']" if cause == "none" else ""))
        if "S15" in names:
            got = pool.map(ordinary_play_case, [(12, rows, seed) for rows in (6, 8, 12) for seed in range(8)])
            bad = [b for r in got for b in r[0]]
            show(f"S15 normal sessions with the culprit unreached, one wrong answer: {len(bad)} bad / "
                 f"{sum(r[1] for r in got)} runs [target 0]" + (f" e.g. {bad[:3]}" if bad else " OK"))
        if "S16" in names:
            for k in (1, 3, 5):
                parts = []
                for e in (0.02, 0.25):
                    rows = pool.map(old_crash_case, [(k, e, 8000 + i) for i in range(60)])
                    parts.append(f"e={e} {stats(rows)}")
                show(f"S16 {k} older crashes like the record's with the culprit not installed yet, exact: "
                     + " | ".join(parts) + " [target innocent 0]")
        if "S17" in names:
            for cause, rates in (("none", (0.95, 0.8, 0.7, 0.5)), ("one", (1.0, 0.95, 0.7, 0.5))):
                for r in rates:
                    means = {"stored": [], "current": []}
                    for start in STARTS:
                        parts = []
                        for rule in ("stored", "current"):
                            base = 7000 if cause == "none" else 17000
                            rows = pool.map(notmods_case, [(base + i, r, start, cause, rule) for i in range(300)],
                                            chunksize=4)
                            parts.append(f"{rule} {notmods_stats(rows)}")
                            means[rule].append((sum(x[0] for x in rows) / len(rows),
                                                sum(x[1] == "right" for x in rows) / len(rows),
                                                sum(x[1] == "innocent" for x in rows) / len(rows)))
                        what = "not the mods" if cause == "none" else "one mod"
                        show(f"S17 {what}, r={r}, {start}: " + " | ".join(parts))
                    avg = {k: [sum(v[i] for v in rows) / len(rows) for i in range(3)] for k, rows in means.items()}
                    line = (f"S17 {what}, r={r}, mean of the {len(STARTS)} starts: stored {avg['stored'][0]:.2f} launches, "
                            f"right {avg['stored'][1]:.1%}, innocent {avg['stored'][2]:.1%} | current "
                            f"{avg['current'][0]:.2f} ({avg['current'][0] - avg['stored'][0]:+.2f}), right "
                            f"{avg['current'][1]:.1%}, innocent {avg['current'][2]:.1%}")
                    if cause == "none" and r == 0.7:
                        line += " [target current innocent <= 11%]" + (" OK" if avg["current"][2] <= 0.11 else " MISS")
                    if cause == "one" and r == 1.0:
                        line += " [target current launches <= stored + 1.0]" + (
                            " OK" if avg["current"][0] <= avg["stored"][0] + 1.0 else " MISS")
                    if cause == "one" and r in (0.7, 0.5):
                        line += " [target current right >= stored - 1 point]" + (
                            " OK" if avg["current"][1] >= avg["stored"][1] - 0.01 else " MISS")
                    show(line)
        if "EST" in names:
            show(*scenario_estimate(pool))
        if "T" in names:
            show(*timing())
        print(f"({time.time() - t0:.0f}s)")


ALL = ["S1", "S2", "S3", "S4", "S4r", "S5", "S6", "S6b", "S7", "S8", "S9", "S10", "S11", "S12", "S13", "S14", "S15", "S16",
       "S17", "EST", "T"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scenarios", nargs="*", help="scenario ids (" + " ".join(ALL) + ")")
    ap.add_argument("--quick", action="store_true", help="S1, S4 and the estimate")
    ap.add_argument("--all", action="store_true", help="every scenario")
    ap.add_argument("--timing", action="store_true", help="only the timing")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)
    names = list(args.scenarios)
    if args.quick:
        names += ["S1", "S4", "EST"]
    if args.all:
        names += ALL
    if args.timing:
        names += ["T"]
    if not names:
        ap.print_help()
        return 1
    run(names, args.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
