"""원인 모드 찾기 v2: the Bayesian search engine (mhwmm.bisect_engine), answered by synthetic "games"."""

import json
import math
import os
import random
import subprocess
import sys
from itertools import combinations
from pathlib import Path

import pytest

from mhwmm import bisect_engine as be
from mhwmm.bisect_engine import Engine, Obs, Params, Prior, simulate

NAMED = ("found", "together", "each")


def ids(n, prefix="u"):
    return [f"{prefix}{i}" for i in range(n)]


def setup(n):
    """REFramework and n mods that need it."""
    us = ids(n)
    return us, ["refw", *us], {u: ["refw"] for u in us}


def named(d) -> set:
    return set(d.mods) if d.kind in NAMED else set()


def det(cause, kind="and"):
    cause = set(cause)
    if kind == "or":
        return lambda on: bool(cause & on)
    if kind == "none":
        return lambda on: True
    return lambda on: cause <= on


def run(units, req, truth, **kw):
    """The search answered exactly by ``truth(on)``: (final decision, tests)."""
    return simulate(units, req, lambda on, k: truth(set(on)), **kw)


def test_one_culprit_anywhere():
    for n in range(1, 13):
        us, units, req = setup(n)
        for c in us:
            d, steps = run(units, req, lambda on, c=c: c in on)
            assert d.kind == "found" and d.mods == (c,), (n, c, d)
            assert len(steps) <= math.ceil(math.log2(n + 1)) + 4
            assert d.pivots >= 2 and d.p >= 0.95
            for s in steps:  # REFramework off only when everything that needs it is off
                assert "refw" in s.on or not (set(us) & s.on)


def test_two_mods_that_crash_only_together():
    for n in range(2, 7):
        us, units, req = setup(n)
        for a, b in combinations(us, 2):
            d, steps = run(units, req, lambda on, a=a, b=b: a in on and b in on)
            assert d.kind == "together" and d.mods == (a, b), (n, a, b, d)
            assert len(steps) <= 2 * (math.ceil(math.log2(n + 1)) + 2) + 6


def test_three_mods_that_crash_only_together():
    for n in range(3, 6):
        us, units, req = setup(n)
        for trio in combinations(us, 3):
            d, steps = run(units, req, lambda on, t=set(trio): t <= on)
            assert d.kind == "together" and d.mods == trio, (n, trio, d)
            assert len(steps) <= 20


def test_two_mods_that_each_crash():
    for n in range(3, 8):
        us, units, req = setup(n)
        for a, b in list(combinations(us, 2))[:6]:
            d, steps = run(units, req, lambda on, a=a, b=b: a in on or b in on)
            assert d.kind == "each" and d.mods == (a, b), (n, a, b, d)


def test_a_library_refw_itself_not_a_mod_and_no_crash():
    units = ["refw", "lib", "u0", "u1", "u2"]
    req = {"lib": ["refw"], "u0": ["refw", "lib"], "u1": ["refw", "lib"], "u2": ["refw"]}
    d, steps = run(units, req, lambda on: "lib" in on)
    assert d.kind == "found" and d.mods == ("lib",)
    assert all("refw" in s.on for s in steps if "lib" in s.on)
    d, _steps = run(units, req, lambda on: "refw" in on)
    assert d.kind == "found" and d.mods == ("refw",)
    d, steps = run(units, req, lambda on: True)  # it crashes whatever is on
    assert d.kind == "not_mods" and d.p >= 0.95 and sum(1 for s in steps if not s.on) >= 2
    d, steps = run(units, req, lambda on: False)  # the crash never comes back
    assert d.kind == "no_repro" and len(steps) <= 6 and not named(d)
    assert [len(s.on) for s in steps[-2:]] == [5, 5]  # the last two: everything on, as reported


def bad_naming(d, cause, kind) -> bool:
    got = named(d)
    return bool((got and not got <= set(cause)) or (d.kind == "not_mods" and kind != "none")
                or (len(cause) == 1 and d.kind in ("together", "each")) or (kind == "or" and d.kind == "together"))


def test_one_wrong_answer_never_names_an_innocent_mod():
    runs = 0
    for n in range(1, 6):
        us = ids(n)
        units = ["refw", "lib", *us]
        req = {"lib": ["refw"], **{u: ["refw"] for u in us}}
        req["u0"] = ["refw", "lib"]
        causes = [((u,), "and") for u in us] + [(("refw",), "and"), (("lib",), "and")]
        causes += [(c, "and") for c in combinations(us, 2)] + [(c, "or") for c in list(combinations(us, 2))[:3]]
        causes += [(c, "and") for c in list(combinations(us, 3))[:1]] + [((), "none")]
        for cause, kind in causes:
            truth = det(cause, kind)
            _d, steps = run(units, req, truth)
            for i in range(len(steps) + 2):  # one wrong answer at each launch in turn (and none)
                d, _s = simulate(units, req, lambda on, k, i=i: truth(set(on)) != (k == i))
                runs += 1
                assert not bad_naming(d, cause, kind), (n, cause, kind, i, d.kind, d.mods)
    assert runs > 600


def noisy_runs(r, params=None, n=12, cases=40, seed=11):
    us, units, req = setup(n)
    rnd = random.Random(seed)
    right = innocent = 0
    for k in range(cases):
        c = us[k % n]
        local = random.Random(rnd.random())

        def answer(on, kk, c=c, local=local):
            real = (c in on and local.random() < r) or local.random() < 0.02
            return real != (local.random() < 0.02)

        d, _steps = simulate(units, req, answer, params=params)
        got = named(d)
        right += got == {c}
        innocent += bool(got) and not got <= {c}
    return right, innocent


def test_noisy_answers():
    right, innocent = noisy_runs(0.95)
    assert right >= 36 and innocent <= 1
    _right, innocent = noisy_runs(0.7, Params(r_prior=be.R_PRIORS["rare"]))
    assert innocent <= 2


def test_a_pointer_from_the_crash_record():
    us, units, req = setup(20)
    right, none, wrong = [], [], []
    for i, c in enumerate(us):
        d, steps = run(units, req, lambda on, c=c: c in on, prior=Prior(shares={c: 0.5}))
        assert d.mods == (c,)
        right.append(len(steps))
        if i % 2:
            continue
        d, steps = run(units, req, lambda on, c=c: c in on)
        none.append(len(steps))
        innocent = us[(i + 7) % 20]  # a record pointing at another mod slows it down, never names it
        d, steps = run(units, req, lambda on, c=c: c in on, prior=Prior(shares={innocent: 0.5}))
        assert d.kind == "found" and d.mods == (c,)
        wrong.append(len(steps))
    assert sum(right) / len(right) <= 3.6
    assert sum(wrong) / len(wrong) <= sum(none) / len(none) + 2.5


def test_the_last_normal_exit():
    us, units, req = setup(20)
    rnd = random.Random(5)
    launches = []
    for c in us:
        changed = {rnd.choice([u for u in us if u != c]), c}
        good = frozenset(u for u in units if u not in changed)
        report = Obs(frozenset(units), True, e=0.02, source="report")
        d, steps = run(units, req, lambda on, c=c: c in on,
                       passive=[report, Obs(good, False, rho=0.8, e=0.02, source="last_good")])
        assert d.mods == (c,)
        launches.append(len(steps))
        # after a game update an unchanged mod may crash now: the old exit counts little, it is still found
        good = frozenset(u for u in units if u not in rnd.sample([u for u in us if u != c], 2))
        d, steps = run(units, req, lambda on, c=c: c in on,
                       passive=[report, Obs(good, False, rho=0.15, e=0.02, source="last_good")])
        assert d.kind == "found" and d.mods == (c,)
    # (5 launches and, the search being this short, one with every mod off before naming it: Params.none_controls)
    assert sum(launches) / len(launches) <= 6.0


def test_a_short_play_counts_little():
    us, units, req = setup(6)
    tops = {}
    for seconds in (None, 120, 1500):
        eng = Engine(units, req, prior=Prior(shares={"u0": 0.5}), crash_times=[600])
        eng.add(Obs(frozenset(units), True, source="report"))
        first = eng.decide()
        if seconds is not None:  # only it on, and the game ran fine for that long
            eng.add(Obs(frozenset(["refw", "u0"]), False, seconds=seconds))
        d = eng.decide()
        tops[seconds] = (d.ranking[0], d.on, first.on)
    assert Engine(units, req, crash_times=[600]).g(120) < 0.01 and Engine(units, req, crash_times=[600]).g(1500) > 0.9
    top, on, first = tops[120]  # 2 minutes, the crash took 10: it stays the top suspect, tested next as before
    assert top.mods == ("u0",) and top.p > 0.4 and on == first
    top, _on, _first = tops[1500]  # 25 minutes: cleared
    assert top.p < 0.25 and top.p < tops[120][0].p / 2
    assert Engine(units, req).g(30) == 1.0  # no crash times known: every answer counts in full


def test_files_the_game_never_requested():
    us, units, req = setup(6)
    inv = {}
    for unexposed in (frozenset(), frozenset({"u0"})):
        eng = Engine(units, req, content=us)
        eng.add(Obs(frozenset(units), True, source="report"))
        before = eng.decide().involvement["u0"]
        eng.add(Obs(frozenset({"refw", "u0", "u1", "u2"}), False, unexposed=unexposed))
        inv[bool(unexposed)] = eng.decide().involvement["u0"]
    assert inv[True] >= before and inv[False] < 0.6 * before  # never loaded: not cleared by the fine run
    eng = Engine(units, req, content=us)
    eng.add(Obs(frozenset(units), True, source="report", unexposed=frozenset({"u3"})))
    eng.add(Obs(frozenset({"refw", "u0", "u3"}), True, unexposed=frozenset({"u3"})))
    eng.add(Obs(frozenset({"refw", "u1", "u3"}), True, unexposed=frozenset({"u3"})))
    assert eng.decide().involvement["u3"] < 0.05  # crashed twice without the game reading it
    # (the log is only about content mods: a plugin's "unexposed" is ignored)
    eng = Engine(units, req, content=["u1"])
    eng.add(Obs(frozenset(units), True, unexposed=frozenset({"u0"}), source="report"))
    assert eng.pivots("u0") == 0 and eng.obs[0].unexposed == 0


def test_masking_keeps_a_loser_with_its_winner():
    rnd = random.Random(3)
    us = ids(8)
    units = ["refw", *us]
    req = {u: ["refw"] for u in us}
    winners = {"u1": "u4", "u2": "u4", "u6": "u7"}  # u1's files lose to u4's...
    for loser, winner in winners.items():
        req[loser] = req[loser] + [winner]
    for seed in range(6):
        local = random.Random(seed)
        _d, steps = simulate(units, req, lambda on, k: local.random() < 0.5, extra=0)
        for s in steps:
            for loser, winner in winners.items():
                assert loser not in s.on or winner in s.on
    eng = Engine(units, req)
    assert eng.without(["u4"]) == frozenset(units) - {"u1", "u2", "u4"}
    assert eng.close(["u1"]) == frozenset({"u1", "u4", "refw"})
    assert eng.hypothesis("and", ["u1", "u4"]) is None  # the same as u1 alone: no such pair
    assert rnd  # (seeded)


def test_strict_needs_three_pivot_pairs():
    us, units, req = setup(8)
    found = None
    for c in us:
        d, steps = run(units, req, lambda on, c=c: c in on)
        if d.pivots == 2 and d.can_confirm:
            found = (c, steps)
            break
    assert found is not None
    c, steps = found
    eng = Engine(units, req, strict=True)
    eng.add(Obs(frozenset(units), True, e=0.02, source="report"))
    for s in steps:
        eng.add(Obs(s.on, c in s.on))
    for _ in range(10):
        d = eng.decide()
        if d.kind != "test":
            break
        eng.add(Obs(d.on, c in d.on))
    assert d.kind == "found" and d.mods == (c,) and d.pivots >= 3


def test_random_answers_end_within_the_budget():
    for seed in range(300):
        rnd = random.Random(seed)
        n = rnd.randint(0, 14)
        refw = rnd.random() < 0.8
        units = (["refw"] if refw else []) + ids(n)
        req = {u: ["refw"] for u in ids(n)} if refw else {}
        d, steps = simulate(units, req, lambda on, k: rnd.random() < 0.5)
        assert d.kind in ("found", "together", "each", "not_mods", "no_repro", "unsure")
        assert len(steps) <= be.budget(len(units)), (seed, n, len(steps))
        assert sum(s.p for s in d.ranking) <= 1 + 1e-9
        if d.kind == "unsure":
            assert d.ranking and all(s.family != "rest" for s in d.ranking)
    assert be.budget(10) == min(40, 3 * math.ceil(math.log2(12)) + 12) and be.budget(10, 6) == be.budget(10) + 6


DETERMINISM = """
import json, sys
from mhwmm.bisect_engine import simulate
units = ["refw"] + [f"u{i}" for i in range(12)]
req = {u: ["refw"] for u in units[1:]}
d, steps = simulate(units, req, lambda on, k: "u7" in on or ("u3" in on and k % 3 == 0))
print(json.dumps([sorted(s.on) for s in steps] + [d.kind, list(d.mods)]))
"""


def test_the_same_answers_give_the_same_steps_in_any_process():
    root = Path(__file__).resolve().parent.parent
    out = []
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(root))
        got = subprocess.run([sys.executable, "-c", DETERMINISM], env=env, capture_output=True, text=True, timeout=120,
                             cwd=str(root))
        assert got.returncode == 0, got.stderr
        out.append(json.loads(got.stdout))
    assert out[0] == out[1] and len(out[0]) > 3


def test_params_and_prior_round_trip():
    p = Params(r_prior=be.R_PRIORS["rare"], tau=0.97, est_shift=-2)
    assert Params.from_dict(json.loads(json.dumps(p.to_dict()))) == p
    assert Params.from_dict({"tau": "x", "r_grid": [0.9], "b": -1, "e": 2.0, "max_launches": 3.5, "nope": 1,
                             "est_shift": True, "g_sigma": 0}) == Params()
    assert Params.from_dict(None) == Params() and Params.from_dict([1]) == Params()
    assert Params.from_dict({"r_grid": [0.9, 0.5], "r_prior": [0.5, 0.5]}).r_grid == (0.9, 0.5)
    prior = Prior({"a": 3.0}, {"b": 0.5}, [("a", "b", 10.0)], 2.0, 0.5)
    assert Prior.from_dict(json.loads(json.dumps(prior.to_dict()))) == prior
    assert Prior.from_dict({"weights": {"a": "x", "b": -1, 3: 2}, "shares": [], "pairs": [["a"], ["a", "b", "x"]],
                            "none_scale": "big", "or_scale": None}) == Prior()
    assert Prior.from_dict("junk") == Prior() and Prior().empty and not prior.empty
    obs = Obs(frozenset({"b", "a"}), True, 0.6, 12.5, 0.005, frozenset({"c"}), frozenset({"a"}), "history")
    data = obs.to_dict(["b", "a", "c"])
    assert data["on"] == ["b", "a"] and Obs.from_dict(json.loads(json.dumps(data))) == obs
    assert Obs.from_dict({"on": "x", "crashed": True}) is None and Obs.from_dict({"on": [], "crashed": 1}) is None
    junk = Obs.from_dict({"on": ["a", 3], "crashed": False, "rho": 7, "seconds": -1, "e": 0.9, "source": "x"})
    assert junk == Obs(frozenset({"a"}), False)


def test_strong_pointers_are_capped_and_weights_clamped():
    us, units, req = setup(10)
    eng = Engine(units, req, prior=Prior(weights={"u1": 1e9, "u2": 0.0}, shares={"u3": 0.7, "u4": 0.7, "zz": 0.9}))
    eng.add(Obs(frozenset(units), True, source="report"))
    d = eng.decide()
    one = {s.mods[0]: s.p for s in d.ranking if s.family == "one"}
    assert one["u3"] == pytest.approx(one["u4"]) and one["u3"] < 0.45  # 0.85 between them at most
    assert eng.p_of("one", ["u1"]) < one["u3"] and 0 < eng.p_of("one", ["u2"]) < eng.p_of("one", ["u5"])
    assert d.suspects >= 2 and 0 < d.r_mean < 1 and d.entropy > 0 and d.remaining[0] >= 1


def test_speed_is_reasonable_at_200_mods():
    # not a timing assert: only that a large search works (tools/bisect_sim.py --timing measures it)
    us, units, req = setup(200)
    eng = Engine(units, req)
    eng.add(Obs(frozenset(units), True, source="report"))
    d = eng.decide()
    assert d.kind == "test" and 0 < len(d.on) < len(units) and eng.H > 2 * 199 * 100


def test_the_fast_update_equals_the_plain_likelihood():
    # add() updates pairs by blocks of member states against a baseline per family; this is the textbook sum
    worst = 0.0
    for trial in range(25):
        rnd = random.Random(trial)
        n = rnd.randint(2, 8)
        units = ids(n)
        req = {u: [units[0]] for u in units[1:] if rnd.random() < 0.5}
        content = [u for u in units if rnd.random() < 0.6]
        eng = Engine(units, req, content=content, crash_times=[300.0])
        fresh = Engine(units, req, content=content, crash_times=[300.0])
        for k in range(rnd.randint(1, 7)):
            on = frozenset(u for u in units if rnd.random() < 0.5)
            eng.add(Obs(on, rnd.random() < 0.5, rho=rnd.choice([1.0, 0.6, 0.15]), seconds=rnd.choice([None, 60.0, 600.0]),
                        e=rnd.choice([None, 0.005]), unknown=frozenset(u for u in units if rnd.random() < 0.15),
                        unexposed=frozenset(u for u in on if rnd.random() < 0.3), source="history" if k == 0 else "search"))
        for code in range(6):
            s, end = eng.ranges[code]
            for h in range(s, end):
                want = [fresh.L[r][h] for r in range(eng.R)]
                for rec in eng.obs:
                    if (eng.masks[h] & rec.unknown) or (eng.kinds[h] == be.NONE and rec.unknown):
                        continue
                    fv = eng.f(h, rec.on, rec.unexposed)
                    scale = rec.rho * (1.0 if rec.crashed else rec.g)
                    for r, rv in enumerate(eng.grid):
                        want[r] += math.log(max(1e-300, eng._lik(rec.crashed, rec.e, scale, fv, rv)))
                for r in range(eng.R):
                    worst = max(worst, abs(eng.L[r][h] + eng.base[code][r] - want[r]))
    assert worst < 1e-9


def test_the_estimate_if_the_top_suspect_is_not_the_cause():
    us, units, req = setup(12)
    eng = Engine(units, req, prior=Prior(shares={"u3": 0.5}))
    eng.add(Obs(frozenset(units), True, source="report"))
    plain = Engine(units, req)
    plain.add(Obs(frozenset(units), True, source="report"))
    lo, hi = eng.estimate()
    assert 1 <= lo <= hi < plain.estimate()[1]  # the pointer shortens it...
    assert eng.estimate(without="u3") == plain.estimate()  # ...unless it points at the wrong mod


def test_earlier_sessions_never_make_a_pivot_pair():
    # ordinary sessions (rho 0.6) and the last normal exit with u1 off and the culprit u0 on - its content never reached
    # (a user who never goes there never does) - and u1 a little more suspected (installed lately): one wrong answer at
    # any launch never names u1, nor anyone but u0
    us, units, req = setup(12)
    rnd = random.Random(4)
    passive = [Obs(frozenset(units), True, e=0.005, seconds=600.0, source="report")]
    for _ in range(8):
        off = {"u1"} | set(rnd.sample(us[2:], rnd.randint(0, 3)))
        passive.append(Obs(frozenset(units) - off, False, rho=0.6, seconds=3600.0, source="history"))
    passive.append(Obs(frozenset(units) - {"u1"}, False, rho=0.8, seconds=3600.0, source="last_good"))
    prior = Prior(weights={"u1": 2.0})
    eng = Engine(units, req, prior=prior, crash_times=[600.0])
    for o in passive:
        eng.add(o)
    assert eng.pivots("u1") == 0 and eng.decide().ranking[0].mods == ("u1",)  # (suspected first, no pair yet)
    _d, steps = simulate(units, req, lambda on, k: "u0" in on, prior=prior, passive=passive, crash_times=[600.0])
    for i in range(-1, len(steps) + 2):
        d, _s = simulate(units, req, lambda on, k, i=i: ("u0" in on) != (k == i), prior=prior, passive=passive,
                         crash_times=[600.0])
        assert named(d) <= {"u0"}, (i, d.kind, d.mods)
        if i == -1:
            assert d.kind == "found" and d.mods == ("u0",)
    # an older crash is kept for looking like the record's: never the crash half of a pair (the record is)
    for source, pairs in (("history", 0), ("report", 1)):
        eng = Engine(units, req)
        eng.add(Obs(frozenset({"refw", "u3"}), True, source=source))
        eng.add(Obs(frozenset({"refw"}), False))
        assert eng.pivots("u3") == pairs


def test_older_crashes_from_before_the_culprit_never_name_another_mod():
    # crashes like the record's from before the culprit was installed (every other mod on): they slow the search down;
    # a place known only vaguely (e = 0.25) much less than one at the same offset (0.05, here even 0.02)
    us, units, req = setup(12)
    launches = {}
    for k in (1, 3, 5):
        for e in (0.02, 0.25):
            for c in ("u2", "u11"):
                passive = [Obs(frozenset(units), True, e=0.005, source="report")]
                passive += [Obs(frozenset(units) - {c}, True, e=e, source="history")] * k
                d, steps = run(units, req, lambda on, c=c: c in on, passive=passive)
                assert named(d) <= {c}, (k, e, c, d.kind, d.mods)
                if e == 0.25:
                    assert d.kind == "found" and d.mods == (c,)
                launches[(k, e)] = len(steps)
    assert all(launches[(k, 0.25)] < launches[(k, 0.02)] for k in (1, 3, 5))


def test_launches_with_every_mod_off_before_a_mod_is_named():
    # Params.controls: two fine launches with every mod off before a mod is named, with the "rare" prior - what v2.2.5
    # stored for a graphics error record (new searches use 0); a search it started goes on with them
    us, units, req = setup(10)
    p = Params(r_prior=be.R_PRIORS["rare"], controls=2)
    for c in us:
        d, steps = run(units, req, lambda on, c=c: c in on, params=p)
        assert d.kind == "found" and d.mods == (c,)
        assert sum(1 for s in steps if not s.on) >= 2 and any(s.reason == "control" for s in steps)
        assert all(not s.on for s in steps if s.reason == "control")
    d, steps = run(units, req, lambda on: True, params=p)  # (the controls crash too: not the mods)
    assert d.kind == "not_mods"
    counts = []
    for params in (Params(), p):  # not the mods, crashing 70% of the time whatever is on
        rnd = random.Random(21)
        got = 0
        for _ in range(40):
            local = random.Random(rnd.random())
            d, _s = simulate(units, req, lambda on, k, local=local: (local.random() < 0.7 or local.random() < 0.02)
                             != (local.random() < 0.02), params=params, prior=Prior(none_scale=4.0))
            got += d.kind in NAMED
        counts.append(got)
    assert counts[1] <= 4 and counts[1] < counts[0]
    assert Params.from_dict(json.loads(json.dumps(p.to_dict()))) == p


def not_the_mods(local, r=0.7):
    """Answers of a game that crashes r of the time whatever is on (2% other crashes, 2% wrong answers)."""
    return lambda on, k: (local.random() < r or local.random() < 0.02) != (local.random() < 0.02)


def test_not_the_mods_coming_and_going_is_checked_with_every_mod_off():
    # a crash that comes and goes whatever is on can look like any mod (the search converges on one and its pivot pairs
    # come by chance). A search started now runs the game with every mod off before naming a cause while "not the
    # mods" keeps P >= none_controls (Params.current): it names an innocent mod far less often than the rule of a
    # search stored without it (the field defaults), whatever the record says
    us, units, req = setup(12)
    named_by = {}
    for label, params in (("stored", Params()), ("current", Params.current())):
        rnd = random.Random(31)
        got = 0
        for k in range(60):
            prior = None if k % 2 else Prior(shares={us[k % 12]: 0.5})  # (half of them with a misleading record)
            d, _steps = simulate(units, req, not_the_mods(random.Random(rnd.random())), params=params, prior=prior)
            got += d.kind in NAMED
        named_by[label] = got
    assert named_by["current"] <= 6 and named_by["current"] <= 0.5 * named_by["stored"], named_by  # (13 -> 5)
    assert Params().none_controls == 0 and Params().max_controls == 0  # (the stored rule: none of these launches)


def test_launches_with_every_mod_off_only_while_not_the_mods_keeps_some_probability():
    # exact answers: a launch with every mod off ("control") comes only when a cause could be named and P(not the
    # mods) >= none_controls; the culprit is named once it is under it (or after max_controls fine ones), on average
    # with less than one such launch
    p = Params.current()
    assert p.none_controls > 0 and p.max_controls >= 2
    us, units, req = setup(20)
    controls = []
    for c in us:
        eng = Engine(units, req)
        eng.add(Obs(frozenset(units), True, e=0.02, source="report"))
        asked = 0
        while True:
            d = eng.decide()
            if d.kind != "test":
                break
            if d.reason == "control":
                asked += 1
                assert not d.on and d.mods == (c,) and eng.p_of("none", ()) >= p.none_controls
            eng.add(Obs(d.on, c in d.on))
        assert d.kind == "found" and d.mods == (c,)
        assert eng.p_of("none", ()) < p.none_controls or asked == p.max_controls
        assert asked <= p.max_controls
        controls.append(asked)
    assert any(controls) and sum(controls) / len(controls) < 1.0


def test_short_launches_with_every_mod_off_count_toward_max_controls():
    # 한동안 / 가끔 (timed fine answers): a launch with every mod off played far shorter than the crash takes barely
    # lowers P(not the mods), yet it counts toward max_controls however long it ran - otherwise the same launch came
    # back again and again up to the budget. A crash there only raises P(not the mods): never more than max_controls
    # fine ones before a cause is named, and none asked for once there are that many
    us, units, req = setup(20)
    capped = 0
    for rules in ({}, be.WHEN_RULES["rare"]):
        p = Params.current(r_prior=be.R_PRIORS["rare" if rules else "middle"], timed_fine=True, **rules)
        for c in us[::3]:
            for crash_first in (False, True):
                eng = Engine(units, req, params=p, crash_times=[1200.0])
                eng.add(Obs(frozenset(units), True, e=0.02, seconds=1200.0, source="report"))
                fine_off = crashed_off = launches = 0
                while True:
                    d = eng.decide()
                    if d.kind != "test":
                        break
                    launches += 1
                    if d.reason == "control":
                        assert not d.on and fine_off < p.max_controls
                    if not d.on:  # (5 minutes against a crash at 20: g about 0.01)
                        crashed = crash_first and not crashed_off and d.reason == "control"
                        crashed_off += crashed
                        fine_off += not crashed
                        eng.add(Obs(d.on, crashed, seconds=1200.0 if crashed else 300.0))
                    else:
                        crashed = c in d.on
                        eng.add(Obs(d.on, crashed, seconds=1200.0 if crashed else 3600.0))
                assert fine_off <= p.max_controls and launches <= be.budget(len(units), 0, p)
                if not crash_first:
                    assert d.kind == "found" and d.mods == (c,)
                capped += fine_off == p.max_controls
    assert capped  # (the cap is what ended them: P(not the mods) stayed above none_controls)


def test_launches_with_every_mod_off_after_the_launches_about_the_kind():
    # the other place a cause is named: after the launches about its kind (or the other causes) when none of them is
    # worth making any more - the launches with every mod off come first there too. (With the shipped thresholds a
    # naming seldom ends that way; tau_exact 1 and min_gain 1e9 send every naming there.)
    us, units, req = setup(6)
    rest = frozenset(units) - {"u0"}
    obs = [Obs(frozenset(units), True, e=0.02, source="report"), Obs(frozenset({"refw", "u0"}), True), Obs(rest, False),
           Obs(frozenset({"refw", "u0", "u1", "u2"}), True), Obs(frozenset({"refw", "u1", "u2"}), False)]
    for params, controls in ((Params.current(tau_exact=1.0, min_gain=1e9, none_controls=1e-9, max_controls=2), 2),
                             (Params(tau_exact=1.0, min_gain=1e9), 0)):  # (the stored rule: named at once)
        eng = Engine(units, req, params=params)
        for o in obs:
            eng.add(o)
        for _ in range(controls):
            d = eng.decide()
            assert d.kind == "test" and d.reason == "control" and not d.on and d.mods == ("u0",)
            eng.add(Obs(d.on, False))
        d = eng.decide()
        assert d.kind == "found" and d.mods == ("u0",)


@pytest.mark.parametrize("n", [12, 20])
def test_the_estimate_counts_the_launches_with_every_mod_off_still_to_come(n):
    # until a suspect is near being named, the launches with every mod off a naming may still need are in the upper
    # bound of the remaining-launch estimate (with 가끔 they come before nearly every naming): an exact search always
    # ends within the range shown at each step ("앞으로 약 N~M번")
    us, units, req = setup(n)
    for params in (Params.current(), Params.current(r_prior=be.R_PRIORS["rare"], **be.WHEN_RULES["rare"])):
        controls = 0
        for c in us:
            d, steps = run(units, req, lambda on, c=c: c in on, params=params)
            assert d.kind == "found" and d.mods == (c,)
            controls += sum(1 for s in steps if s.reason == "control")
            for k, s in enumerate(steps):
                lo, hi = s.remaining
                assert lo <= len(steps) - k <= hi, (params.none_controls, c, k, s.remaining, len(steps))
        assert controls


def test_a_search_stored_before_the_launches_with_every_mod_off_keeps_its_rule():
    # parameters stored without none_controls / max_controls (v2.2.5, v2.2.6 before them) read back as the field
    # defaults: the same launches as before, none asked for "not the mods"; a search started now round-trips its own
    us, units, req = setup(12)
    stored = Params().to_dict()
    del stored["none_controls"], stored["max_controls"]
    old = Params.from_dict(json.loads(json.dumps(stored)))
    assert old == Params() and old.none_controls == 0 and old.max_controls == 0
    assert Params.from_dict(json.loads(json.dumps(Params.current().to_dict()))) == Params.current()
    assert Params.from_dict({**stored, "none_controls": 2.0, "max_controls": -1}) == Params()  # (out of range)
    rnd = random.Random(8)
    for _ in range(12):
        seed = rnd.random()
        d, steps = simulate(units, req, not_the_mods(random.Random(seed)), params=old)
        assert not any(s.reason == "control" for s in steps)
        again, steps2 = simulate(units, req, not_the_mods(random.Random(seed)), params=Params())
        assert [s.on for s in steps] == [s.on for s in steps2] and (d.kind, d.mods) == (again.kind, again.mods)


def test_one_more_check_after_a_record_that_pointed_at_the_culprit():
    # the record's crash plus one crash and two fine runs of the search: no single launch adds a third pair (it needs
    # a crash and a fine run), yet 한 번 더 확인 is offered and a strict search makes both
    for n in (8, 20):
        us, units, req = setup(n)
        for c in us[::3]:
            prior = Prior(shares={c: 0.5})
            d, steps = run(units, req, lambda on, c=c: c in on, prior=prior)
            assert d.kind == "found" and d.mods == (c,) and d.pivots == 2 and d.can_confirm, (n, c)
            eng = Engine(units, req, prior=prior, strict=True)
            eng.add(Obs(frozenset(units), True, e=0.02, source="report"))
            for s in steps:
                eng.add(Obs(s.on, c in s.on))
            reasons = []
            for _ in range(6):
                d = eng.decide(3)
                if d.kind != "test":
                    break
                reasons.append(d.reason)
                eng.add(Obs(d.on, c in d.on))
            assert d.kind == "found" and d.mods == (c,) and d.pivots >= 3 and not d.can_confirm
            assert reasons and all(r.startswith("confirm") for r in reasons)


@pytest.mark.parametrize("n", [2, 4, 6])
def test_one_more_check_of_two_mods_that_each_crash_never_repeats_a_launch(n):
    # 'each' (two mods that are both broken): no single launch adds a pair to both members, so 한 번 더 확인 plans a
    # few from where the search is now - a crash with one member alone on is not asked for again once it came
    units = ids(n)
    checked = 0
    for a, b in combinations(units, 2):
        for prior in (Prior(), Prior(shares={a: 0.3, b: 0.3}), Prior(shares={a: 0.5, b: 0.2}, or_scale=2.0)):
            truth = det({a, b}, "or")
            d, steps = run(units, {}, truth, prior=prior)
            if not (d.kind == "each" and d.can_confirm):
                continue
            assert d.pivots == 2
            eng = Engine(units, prior=prior, strict=True)
            eng.add(Obs(frozenset(units), True, e=0.02, source="report"))
            for s in steps:
                eng.add(Obs(s.on, truth(set(s.on))))
            want = eng.confirm_launches()
            assert 1 <= want <= 4
            extra = max(0, len(steps) + want - be.budget(n))
            seen = []
            for _ in range(want):
                d = eng.decide(extra)
                if d.kind != "test":
                    break
                assert d.on not in seen and d.reason.startswith("confirm")
                seen.append(d.on)
                eng.add(Obs(d.on, truth(set(d.on))))
            d = eng.decide(extra)
            assert d.kind == "each" and set(d.mods) == {a, b} and d.pivots >= 3, (n, a, b, prior, seen)
            checked += 1
    assert checked


def test_a_single_mod_that_crashes_now_and_then_is_named():
    # one unit and the "rare" prior: three pairs are needed and no one launch adds the third (it takes a crash and a
    # fine run), so the search plans both rather than stopping unsure with nothing left to learn (also with the
    # controls of a search v2.2.5 started from a graphics error record)
    for params in (Params(r_prior=be.R_PRIORS["rare"]), Params(r_prior=be.R_PRIORS["rare"], controls=2)):
        d, steps = run(["x"], {}, lambda on: "x" in on, params=params)
        assert d.kind == "found" and d.mods == ("x",), (params.controls, [s.on for s in steps])
        assert len(steps) <= 5


def test_play_time_counts_only_when_the_crash_comes_after_a_while():
    # 같은 장소·행동 (timed_fine False): a 2-minute fine run counts in full although crashes took 10 minutes
    us, units, req = setup(6)
    for timed, weak in ((True, True), (False, False)):
        eng = Engine(units, req, params=Params(timed_fine=timed), crash_times=[600])
        eng.add(Obs(frozenset(units), True, seconds=600.0, source="report"))
        eng.add(Obs(frozenset({"refw", "u0"}), False, seconds=120.0))
        assert (eng.obs[-1].g < 0.5) == weak and eng.obs[-1].seconds == 120.0
    # the minutes the user typed replace the record's crash times (passive_times False)
    eng = Engine(units, req, params=Params(passive_times=False), crash_times=[600])
    eng.add(Obs(frozenset(units), True, seconds=2700.0, source="report"))
    eng.add(Obs(frozenset({"refw", "u0"}), True, seconds=300.0))
    assert eng.times == [600, 300.0] and eng.g(1000) == pytest.approx(eng._gfun(1000, [600, 300.0]))
