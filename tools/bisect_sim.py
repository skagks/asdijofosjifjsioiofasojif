"""원인 모드 찾기: simulated searches with the v2 engine (mhwmm.bisect_engine) against the shipped v2.2.3
search (mhwmm.bisect.simulate), for the acceptance scenarios of the v2.2.4 design. Not run by CI.

    python tools/bisect_sim.py --quick     S1 and S4 (and the remaining-launch estimate), a few minutes
    python tools/bisect_sim.py --all       every scenario
    python tools/bisect_sim.py --timing    how long decide() and a replay take (n = 50, 100, 200)
    python tools/bisect_sim.py S6 S7       only these scenarios

Everything is seeded: the same command prints the same numbers. "innocent" means a named set that is
not part of the real cause; launches are means.
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
from mhwmm.bisect_engine import Engine, Obs, Params, Prior, R_PRIORS, budget, simulate  # noqa: E402

NAMED = ("found", "together", "each")
RARE = Params(r_prior=R_PRIORS["rare"])


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


# ---- S14: a graphics device error that is not the mods (v2.2.5) --------------------------------------------------------
DEVICE = Params(r_prior=R_PRIORS["rare"], controls=2)  # what bisect._settle_data uses for DEVICE_HUNG ... records


def device_case(args):
    seed, n, r, cause_none, params = args
    rnd = random.Random(seed)
    us = ids(n)
    units, req = ["refw", *us], {u: ["refw"] for u in us}
    c = rnd.choice(us)
    local = random.Random(rnd.random())

    def answer(on, k):
        real = (local.random() < r if cause_none else (c in on and local.random() < r)) or local.random() < 0.02
        return real != (local.random() < 0.02)

    d, steps = simulate(units, req, answer, params=params, prior=Prior(none_scale=4.0))  # (W9 x2, W9b x2)
    if cause_none:
        return len(steps), "right" if d.kind == "not_mods" else "innocent" if named(d) else "none"
    return len(steps), outcome(named(d), (c,))


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
    n, culprit, r_true, seed = args
    us = ids(n)
    units = ["refw", *us]
    req = {u: ["refw"] for u in us}
    local = random.Random(seed)

    def answer(on, k):
        if r_true >= 1:
            return culprit in on
        real = (culprit in on and local.random() < r_true) or local.random() < 0.02
        return real != (local.random() < 0.02)

    d, steps = simulate(units, req, answer)
    total = len(steps)
    out = []
    for k, s in enumerate(steps):
        actual = total - k  # this launch and the ones after it
        lo, hi = s.remaining
        out.append((lo <= actual <= hi, abs((lo + hi) / 2 - actual)))
    return out


def scenario_estimate(pool):
    lines = []
    for label, jobs in (
            ("S1 n=10", [(10, f"u{i}", 1.0, i) for i in range(10)]),
            ("S1 n=30", [(30, f"u{i}", 1.0, i) for i in range(0, 30, 2)]),
            ("S4 n=20 r=0.95", [(20, f"u{i % 20}", 0.95, 100 + i) for i in range(60)]),
            ("S4 n=20 r=0.7", [(20, f"u{i % 20}", 0.7, 200 + i) for i in range(60)])):
        rows = [x for r in pool.map(estimate_case, jobs, chunksize=2) for x in r]
        inside = sum(1 for ok, _d in rows if ok) / len(rows)
        mean = sum(d for _ok, d in rows) / len(rows)
        reachable = "r=0.7" not in label  # (a per-bucket median oracle gets about 3.9 there: the answers are noisy)
        lines.append(f"estimate {label}: actual in [lo, hi] for {inside:.0%} of {len(rows)} steps, "
                     f"mean |mid - actual| {mean:.2f} [target >= 60%, <= 2"
                     + ("]" if reachable else "; the error target is out of reach with these inputs]")
                     + (" OK" if inside >= 0.6 and (mean <= 2 or not reachable) else " MISS"))
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
        if "S1" in names:
            show(*scenario_exact(pool, "S1", "one", (10, 30, 100), {
                10: "mean <= 5.6, max <= 7, right 100%", 30: "mean <= 7.2, max <= 8, right 100%",
                100: "mean <= 9.4, max <= 11, right 100%"}))
        if "S2" in names:
            show(*scenario_exact(pool, "S2", "and", (10, 30), {
                10: "mean <= 16.0, right >= 95%, innocent 0", 30: "mean <= 19.0, right >= 95%, innocent 0"}))
        if "S3" in names:
            show(*scenario_exact(pool, "S3", "or", (10, 30), {
                10: "mean <= 13.0, right >= 95%, innocent 0", 30: "mean <= 17.0, right >= 93%, innocent 0"}))
        if "S4" in names:
            for r, target in ((0.95, "right >= 97%, innocent <= 1.0%, mean <= 8.6"),
                              (0.7, "right >= 88%, innocent <= 2.0%, mean <= 13.0"),
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
                 f"[target mean <= 5.8 / <= 7.2, right 100%]" + (" OK" if ma <= 5.8 and mb <= 7.2 else " MISS"))
        if "S8" in names:
            for mis, target in ((0.1, 8.8), (0.4, 9.0)):
                parts = []
                for use in ("old", "new", "plain"):
                    rows = pool.map(mixed_case, [(1000 + i, 30, mis, 0.5, use) for i in range(160)], chunksize=4)
                    parts.append(f"{ {'old': 'shipped', 'new': 'engine', 'plain': 'engine without evidence'}[use]} "
                                 f"{stats(rows)}")
                show(f"S8 mixed n=30 misleading={mis}: " + " | ".join(parts)
                     + f" [target with evidence mean <= {target}, right >= 96%, innocent <= 0.7%; without mean <= 11.0]")
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
                     f"{stats([r[0] for r in rows])}" + (" [target mean <= 7.0, right >= 99%]" if use else ""))
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
            for r in (0.95, 0.7, 0.5):
                parts = []
                for label, params in (("middle", None), ("rare+2 controls", DEVICE)):
                    rows = pool.map(device_case, [(7000 + i, 20, r, True, params) for i in range(100)], chunksize=4)
                    parts.append(f"{label} {stats(rows)}")
                show(f"S14 not the mods (device error, none x4), n=20, crashes r={r} whatever is on: " + " | ".join(parts)
                     + (" [target with controls: innocent <= 10% at r=0.7]" if r == 0.7 else ""))
            for label, params in (("middle", None), ("rare+2 controls", DEVICE)):
                rows = pool.map(device_case, [(7000 + i, 20, 1.0, False, params) for i in range(100)], chunksize=4)
                show(f"S14 one mod after a device error record, n=20, exact: {label} {stats(rows)}")
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
        if "EST" in names:
            show(*scenario_estimate(pool))
        if "T" in names:
            show(*timing())
        print(f"({time.time() - t0:.0f}s)")


ALL = ["S1", "S2", "S3", "S4", "S4r", "S5", "S6", "S6b", "S7", "S8", "S9", "S10", "S11", "S12", "S13", "S14", "S15", "S16",
       "EST", "T"]


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
