"""원인 모드 찾기: the search over answers, its plan, and a search run with a real ModManager
(synthetic mods; the "game" crashes when a test's predicate says so)."""

import itertools
import json
import math
import random
import time

import pytest

from mhwmm import bisect, gamewatch
from mhwmm.bisect import ModFacts, Result, Step

from conftest import make_zip, touch_later, write


def run(units, base=(), crash=None, core=(), requires=None, split=None):
    """The search answered by ``crash`` (set of ids on -> crashed): (result, steps)."""
    return bisect.simulate(list(units), list(base), list(core), requires, split, crash)


def ids(n, prefix="u"):
    return [f"{prefix}{i}" for i in range(n)]


def bound(n):
    """Launches for one mod: halving, then one to see it crash alone and, when that rests on one
    answer, one more to see the game run fine without it."""
    return math.ceil(math.log2(n + 1)) + 2


# ---- the search ------------------------------------------------------------------------------
@pytest.mark.parametrize("n", range(1, 21))
def test_one_mod_is_found_wherever_it_is(n):
    units = ids(n)
    estimate = bisect.estimate_runs(units, ["refw"])
    assert estimate <= bound(n)
    for culprit in units:
        result, steps = run(units, ["refw"], lambda s, c=culprit: c in s)
        assert result.kind == "found" and result.mods == [culprit] and result.runs == len(steps)
        assert len(steps) <= estimate
        assert all("refw" in s.on for s in steps)  # REFramework always stays on
        # it rests on what was seen: with it the game crashed, and without it (and REFramework on) it did not
        answers = [(set(s.on), culprit in s.on) for s in steps]
        assert any(on == {"refw", culprit} or (culprit in on and crashed) for on, crashed in answers)
        assert any(not crashed and "refw" in on and culprit not in on for on, crashed in answers)


@pytest.mark.parametrize("n", range(2, 9))
def test_two_mods_that_crash_only_together(n):
    units = ids(n)
    for a, b in itertools.combinations(units, 2):
        result, steps = run(units, ["refw"], lambda s, a=a, b=b: a in s and b in s)
        assert result.kind == "together" and result.mods == [a, b]
        assert len(steps) <= 2 * bound(n)
        assert any(s.kind == "partner" or s.kind == "confirm" for s in steps)


@pytest.mark.parametrize("n", range(3, 8))
def test_three_mods_that_crash_only_together(n):
    units = ids(n)
    for trio in itertools.combinations(units, 3):
        result, _steps = run(units, ["refw"], lambda s, t=trio: set(t) <= s)
        assert result.kind == "together" and result.mods == list(trio)


def test_the_result_lists_mods_in_load_order():
    order = ["refw", "z", "a", "m"]
    answers = []
    while True:
        nxt = bisect.next_step(["z", "a", "m"], ["refw"], answers=answers, order=order)
        if isinstance(nxt, Result):
            break
        assert nxt.on == [i for i in order if i in nxt.on] and set(nxt.off) == set(order) - set(nxt.on)
        answers.append((nxt.on, {"m", "z"} <= set(nxt.on)))
    assert nxt.mods == ["z", "m"]


def test_a_library_every_mod_needs_crashes():
    # "lib" is needed by u1 (so it stays on), and itself needs REFramework
    requires = {"u1": ["lib"], "lib": ["refw"]}
    result, steps = run(ids(6), ["refw", "lib"], lambda s: "lib" in s, core=["refw", "lib"], requires=requires)
    assert result.kind == "core" and result.mods == ["lib"]
    kinds = [s.kind for s in steps]
    assert "base" in kinds and "everything_off" in kinds  # the kept group alone, then nothing at all
    for s in steps:
        if "lib" in s.on:
            assert "refw" in s.on  # never without what it needs
        if "u1" in s.on:
            assert "lib" in s.on


def test_refw_itself_and_not_a_mod_at_all():
    result, steps = run(ids(5), ["refw"], lambda s: "refw" in s)
    assert result.kind == "core" and result.mods == ["refw"]
    # REFramework alone crashed and nothing on did not, each seen twice
    assert [(s.kind, s.on) for s in steps][-4:] == [("base", ["refw"]), ("everything_off", []), ("again", ["refw"]),
                                                   ("everything_off", [])]
    result, steps = run(ids(5), ["refw"], lambda s: True)
    assert result.kind == "not_mods" and [s.on for s in steps][-2:] == [[], []]
    result, steps = run([], [], lambda s: True)  # nothing on at all: one launch with nothing
    assert result.kind == "not_mods" and [s.kind for s in steps] == ["everything_off"]


def test_a_crash_that_does_not_come_back():
    # never again: everything is tried once more before giving up, and nobody is blamed
    result, steps = run(ids(8), ["refw"], lambda s: False)
    assert result.kind == "no_repro" and result.mods == [] and steps[-1].kind == "full"
    assert len(steps[-1].on) == 9
    # once, then never: the answers contradict each other
    seen = []

    def once(s):
        seen.append(s)
        return len(seen) == 1

    result, _steps = run(ids(8), ["refw"], once)
    assert result.kind == "intermittent" and result.mods == []


def answered_with(units, base, truth, wrong, core=(), requires=None):
    """The search answered like ``truth`` except the launches numbered in ``wrong`` (0-based)."""
    answers, steps = [], []
    while True:
        nxt = bisect.next_step(units, base, core or base, requires, None, answers)
        if isinstance(nxt, Result):
            return nxt, steps
        steps.append(nxt)
        answers.append((nxt.on, truth(set(nxt.on)) != (len(steps) - 1 in wrong)))


def test_one_wrong_answer_never_blames_a_mod_that_is_not_the_cause():
    # a crash that did not come back in time (괜찮았어요) or a mis-click (튕겼어요), at any launch: the
    # search may stop (intermittent), but what it names is (part of) the real cause
    requires = {"lib": ["refw"], "u0": ["lib"]}
    for n in range(1, 11):
        units = ids(n)
        causes = [(u,) for u in units] + [("refw",), ("lib",)] + list(itertools.combinations(units, 2))[:12]
        for cause in causes:
            truth = lambda on, c=set(cause): c <= on  # noqa: E731
            right, steps = answered_with(units, ["refw", "lib"], truth, set(), ["refw", "lib"], requires)
            assert set(right.mods) == set(cause)
            for i in range(len(steps) + 2):
                result, _steps = answered_with(units, ["refw", "lib"], truth, {i}, ["refw", "lib"], requires)
                if result.can_disable:
                    assert set(result.mods) <= set(cause), (n, cause, i, result)
                    assert len(cause) > 1 or result.kind != "together"


def test_the_traces_of_one_wrong_answer():
    names = list("ABCDEFGH")
    # the crash took longer than this play (A..D answered 괜찮았어요): not "A and E together"
    result, steps = answered_with(names, ["refw"], lambda on: "A" in on, {0})
    assert steps[0].on == ["refw", *"ABCD"] and result.kind == "intermittent"
    # a mis-click on the first launch with the first suspect (B is the cause): not "A"
    result, steps = answered_with(list("ABC"), ["refw"], lambda on: "B" in on, {0})
    assert steps[0].on == ["refw", "A"] and result.kind == "intermittent"
    # REFramework itself crashes, and one launch was answered 괜찮았어요: no mod is blamed
    for i in range(4):
        result, steps = answered_with(list("ABC"), ["refw"], lambda on: "refw" in on, {i})
        assert not result.can_disable or result.mods == ["refw"]
    # nothing on crashed once (a mis-click): asked again, not "모드 문제가 아닐 수 있습니다"
    _right, steps = answered_with(list("ABC"), ["refw"], lambda on: "refw" in on, set())
    off = [s.kind for s in steps].index("everything_off")
    result, steps = answered_with(list("ABC"), ["refw"], lambda on: "refw" in on, {off})
    assert result.kind == "intermittent" and [s.on for s in steps].count([]) == 2


def test_random_answers_always_end():
    for seed in range(300):
        rnd = random.Random(seed)
        n = rnd.randint(0, 14)
        base = ["refw"] if rnd.random() < 0.8 else []
        result, steps = run(ids(n), base, lambda s: rnd.random() < 0.5, core=base)
        assert result.kind in bisect.RESULTS and len(steps) <= 3 * bound(n) + 4
        if result.kind in ("found", "together"):
            # a crash was seen with them on, and each is needed (more mods on without it ran fine)
            on_crash = [set(s.on) for s in steps if set(result.mods) <= set(s.on)]
            assert on_crash


def test_the_last_good_set_is_tried_first():
    units = ids(24)
    estimate = bisect.estimate_runs(units, ["refw"], split=20)
    for culprit in units[20:]:
        result, steps = run(units, ["refw"], lambda s, c=culprit: c in s, split=20)
        assert result.mods == [culprit] and steps[0].kind == "last_good" and len(steps[0].on) == 21
        assert len(steps) <= estimate <= 5
    # an old mod broken by a game update is still found
    result, steps = run(units, ["refw"], lambda s: "u5" in s, split=20)
    assert result.mods == ["u5"] and steps[0].kind == "last_good"
    # the start window says so: the estimate holds when a changed mod is the cause, else it takes more
    everywhere = bisect.estimate_runs(units, ["refw"], split=20, everywhere=True)
    assert len(steps) <= everywhere and everywhere > estimate
    plan = bisect.Plan(units, ["refw"], ["refw"], {}, 20, {}, ["refw", *units], runs=estimate, runs_all=everywhere)
    assert f"바뀐 모드가 원인이면 약 {estimate}번, 아니면 약 {everywhere}번 실행하면 됩니다" in plan.intro()


def test_unsure_answers_repeat_the_step():
    answers = []
    first = bisect.next_step(ids(4), ["refw"], answers=answers)
    assert bisect.next_step(ids(4), ["refw"], answers=answers) == first
    answers.append((first.on, True))
    assert bisect.next_step(ids(4), ["refw"], answers=answers).number == 2


# ---- requirements and the plan -------------------------------------------------------------------
def facts(**kw):
    base = dict(enabled=True, has_files=True)
    base.update(kw)
    return ModFacts(**base)


def test_requirements_as_mod_ids():
    mods = [
        facts(id="r", name="REFramework", refw=True),
        facts(id="lib", name="Lua Bone System", provides=frozenset({"bones", "lib/bones"}), needs_refw=True),
        facts(id="lib-off", name="Lua Bone System (2)", enabled=False, provides=frozenset({"bones"})),
        facts(id="cat", name="_CatLib", nexus_id=77, provides=frozenset({"_catlib"}), needs_refw=True),
        facts(id="app", name="App", needs_refw=True, lua_requires=["Bones", "missing.module"],
              requirements=[{"name": "REFramework"}, {"name": "Cat Library", "nexus_id": 77},
                            {"name": "LuaBoneSystem v2"}, {"name": "Elsewhere", "external": True},
                            {"name": "Bad", "nexus_id": "x"}]),
        facts(id="tex", name="Texture"),
    ]
    req = bisect.requirement_ids(mods)
    assert req["app"] == ["r", "cat", "lib"]  # by name, by mod number, by Lua module; REFramework once
    assert req["lib"] == ["r"] and req["tex"] == [] and req["r"] == []
    mods[0].enabled = False  # REFramework off (or not a mod): not needed by anything here
    assert "r" not in bisect.requirement_ids(mods)["app"]


def test_a_shader_pack_is_turned_on_with_the_costume_that_uses_its_shaders():
    skin = "materialshader/variation/ef_skin_npr.mmtr"
    mods = [facts(id="pack-off", name="Old pack", enabled=False, shaders_provided=frozenset({skin})),
            facts(id="pack", name="Endfield Style Shader Pack", shaders_provided=frozenset({skin})),
            facts(id="rossi", name="Rossi", shaders_needed=frozenset({skin, "materialshader/nobody.mmtr"}))]
    req = bisect.requirement_ids(mods)
    assert req["rossi"] == ["pack"] and req["pack"] == []  # (no mod has the other shader: left out)


def test_the_plan_turns_what_others_need_on_with_them():
    mods = [
        facts(id="r", name="REFramework", refw=True),
        facts(id="lib", name="Lib", provides=frozenset({"lib"}), needs_refw=True),
        facts(id="opt", name="Options", has_files=False, needs_option=True),
        facts(id="app", name="App", lua_requires=["lib"], needs_refw=True),
        facts(id="off", name="Off", enabled=False),
        facts(id="tex", name="Texture"),
        facts(id="lost", name="Lost", hidden=True),
    ]
    plan = bisect.make_plan(mods, unmanaged=(["natives/STM/x.tex"], 1), profile="기본")
    # every mod that reaches the game is a suspect, REFramework and the library too
    assert plan.units == ["r", "lib", "app", "tex"] and plan.base == ["r", "lib"] and plan.excluded == ["opt"]
    assert plan.hidden == ["lost"] and plan.core == ["r", "lib"] and plan.count == 4 and plan.split is None
    assert plan.problem == "" and plan.v2 and isinstance(plan.first, Step) and plan.first.number == 1
    lo, hi = plan.estimate
    assert 1 <= lo <= hi and plan.runs == hi and "켜져 있는 모드 4개" in plan.intro() and "예상: 게임을 약 " in plan.intro()
    lines = "\n".join(plan.lines())
    assert "다른 모드에 필요한 모드(그 모드를 켤 때 함께 켬): REFramework, Lib" in lines and "Options" in lines
    assert "natives/STM/x.tex" in lines and "파일이 모두 밀려 게임에 들어가지 않는 모드(빼고 찾음, 덮어쓰는 모드를 끌 때 함께 끔): Lost" in lines
    assert "계속 켜 둘 모드" not in lines
    assert bisect.make_plan([facts(id="x", name="X", enabled=False)]).problem


def test_what_changed_since_the_last_normal_exit():
    mods = [facts(id="a", name="A", stamp="1|"), facts(id="b", name="B", stamp="2|2"),
            facts(id="c", name="C", stamp="3|", options=["Blue"]), facts(id="d", name="D", stamp="4|"),
            facts(id="r", name="REFramework", refw=True, stamp="9|"),
            facts(id="o", name="O", has_files=False, needs_option=True)]
    last_good = {"time": 1760000000.0, "context": {"game_version": "old", "mods": [
        {"id": "a", "name": "A", "enabled": True, "stamp": "1|"},
        {"id": "b", "name": "B", "enabled": True, "stamp": "2|"},
        {"id": "c", "name": "C", "enabled": True, "stamp": "3|", "options": ["Red"]},
        {"id": "gone", "name": "지운 모드", "enabled": True, "stamp": "x"},
        {"id": "o", "name": "O", "enabled": True, "stamp": ""},
        {"id": "r", "name": "REFramework", "enabled": True, "stamp": "9|"}]}}
    changes = bisect.changes_since(last_good, mods, "new")
    assert changes.game_updated and changes.added == ["d"] and changes.updated == ["b"] and changes.options == ["c"]
    assert changes.removed == ["지운 모드"] and changes.changed == ["d", "b", "c"]
    text = changes.text({m.id: m.name for m in mods})
    assert "게임 업데이트" in text and "새로 켠 모드 1개(D)" in text and "업데이트한 모드 1개(B)" in text
    plan = bisect.make_plan(mods, last_good, "new")
    assert plan.units == ["a", "b", "c", "d", "r"] and plan.split is None
    # the last normal exit is an observation: unchanged mods on, changed ones unknown, a new one off; it says
    # little after a game update
    (good,) = [o for o in plan.passive if o.source == "last_good"]
    assert good.on == {"a", "r"} and good.unknown == {"b", "c"} and not good.crashed and good.rho == 0.15
    assert "바뀐 모드가 원인이면" not in plan.intro()
    assert bisect.last_good_obs(last_good, mods, plan.units, "old").rho == 0.8
    assert bisect.last_good_obs({"context": {"mods": []}}, mods, plan.units) is None
    assert bisect.changes_since(None, mods) is None
    same = {"context": {"mods": [{"id": m.id, "enabled": True, "stamp": m.stamp} for m in mods]}}
    assert bisect.changes_since(same, mods).text({}).endswith("바뀐 모드가 없습니다.")


def test_dependency_order():
    order = bisect.dependency_order(["b", "a", "r", "c"], {"a": ["r"], "b": ["a"], "c": ["c2"]}, first="r")
    assert order == ["r", "a", "b", "c"]
    assert sorted(bisect.dependency_order(["x", "y"], {"x": ["y"], "y": ["x"]})) == ["x", "y"]  # a cycle


def test_files_the_manager_did_not_place(tmp_path):
    game = tmp_path / "game"
    for rel in ("natives/STM/a.tex.241106027", "natives/STM/mine.tex.241106027", "pak_mods/001_x.pak",
                "pak_mods/other.pak", "pak_mods/notes.txt", "pak_mods/upper.PAK", "reframework/plugins/Foo.dll",
                "reframework/autorun/bar.lua", "reframework/autorun/lib/util.lua", "reframework/data/x.json",
                "dxgi.dll", "dinput8.dll", "loader.asi", "MonsterHunterWilds.exe", "steam_api64.dll",
                "natives/STM/x.tex.mhwmm-tmp"):
        write(game / rel)
    deployed = {"natives/stm/mine.tex.241106027": {"mod": "m"}, "pak_mods/001_x.pak": {"mod": "m"},
                "dinput8.dll": {"mod": "r"}}
    found, total = bisect.unmanaged_files(game, deployed)
    assert found == ["dxgi.dll", "loader.asi", "natives/STM/a.tex.241106027", "pak_mods/other.pak",
                     "reframework/autorun/bar.lua", "reframework/autorun/lib/util.lua", "reframework/plugins/Foo.dll"]
    assert total == 7
    assert bisect.unmanaged_files(game, deployed, limit=2) == (found[:2], 7)
    assert bisect.unmanaged_files(tmp_path / "missing", {}) == ([], 0)
    assert bisect.unmanaged_files(None, {}) == ([], 0)
    # files placed by hand that a mod's file covers now come back when it is off
    backups = {"dinput8.dll": {"path": "dinput8.dll", "backup": "dinput8.dll"},
               "natives/stm/old.tex": {"path": "natives/STM/old.tex", "backup": "natives/STM/old.tex"},
               "amd_fidelityfx_dx12.dll": {"path": "amd_fidelityfx_dx12.dll", "backup": "amd_fidelityfx_dx12.dll"}}
    found, total = bisect.unmanaged_files(game, deployed, backups=backups)
    assert total == 9 and "dinput8.dll" + bisect.BACKUP_MARK in found and "natives/STM/old.tex" + bisect.BACKUP_MARK in found
    assert bisect.backed_up_refw(backups) and not bisect.backed_up_refw({}) and not bisect.backed_up_refw(None)


def test_a_big_natives_folder_does_not_hide_plugins_and_paks(tmp_path):
    game = tmp_path / "game"
    for i in range(300):
        write(game / f"natives/STM/big/f{i:03d}.tex")
    for rel in ("pak_mods/999_mine.pak", "reframework/plugins/Evil.dll", "reframework/autorun/foo.lua", "dxgi.dll"):
        write(game / rel)
    deployed = {f"natives/stm/big/f{i:03d}.tex": {"mod": "m"} for i in range(300)}
    assert bisect.unmanaged_files(game, deployed)[1] == 4
    found, _total = bisect.unmanaged_files(game, deployed, scan=50)  # (the budget runs out inside natives\)
    assert found == ["dxgi.dll", "pak_mods/999_mine.pak", "reframework/autorun/foo.lua", "reframework/plugins/Evil.dll"]


def test_texts():
    names = {"a": "A", "b": "B", "r": "REFramework"}
    step = Step(2, 5, "half", ["r", "a"], ["b"])
    assert bisect.step_text(step, names) == ("원인 찾기 2/5단계: 모드 2개를 켜고 적용했습니다. 튕기던 상황을 다시 해 보세요.")
    assert "모든 모드를 끄고" in bisect.step_text(Step(3, 5, "everything_off", [], ["a"]), names)
    assert "'A'만 켜고" in bisect.step_text(Step(4, 5, "confirm", ["r", "a"], ["b"], ["a"]), names)
    assert "'B'만 켜고" in bisect.step_text(Step(4, 5, "confirm", ["r", "b"], ["a"], ["a", "b"]), names)
    assert "'A'만 끄고(모드 1개 켬)" in bisect.step_text(Step(4, 5, "confirm", ["r"], ["a"], ["a"]), names)
    assert "'A' 모드와 함께" in bisect.step_text(Step(5, 6, "partner", ["r", "a", "b"], [], ["a"]), names)
    assert "바뀌지 않은 모드만 켜고(계속 켜 두는 모드 포함 2개)" in \
        bisect.step_text(Step(1, 3, "last_good", ["r", "a"], ["b"]), names)
    # not in the game folder yet (a deploy runs, or failed)
    assert bisect.step_text(step, names, applied=False) == (
        "원인 찾기 2/5단계: 모드 2개를 켜 두었습니다. 게임을 실행하면 먼저 게임 폴더에 적용합니다. 튕기던 상황을 다시 해 보세요.")
    assert "모든 모드를 꺼 두었습니다" in bisect.step_text(Step(3, 5, "everything_off", [], ["a"]), names, applied=False)
    assert "직접 넣어 두었던 dinput8.dll" in bisect.step_text(Step(3, 5, "everything_off", [], ["a"]), names,
                                                         refw_backup=True)
    assert bisect.result_title(Result("found", ["a"]), names) == "원인으로 보이는 모드: A"
    assert bisect.result_title(Result("together", ["a", "b"]), names) == "함께 켜면 튕기는 모드: A, B"
    assert "VRAM" in bisect.result_title(Result("intermittent"), names)
    assert "모드 문제가 아닐 수" in bisect.result_title(Result("not_mods"), names)
    assert "둘 중 하나를" in bisect.result_lines(Result("together", ["a", "b"]), names)[0]
    assert "이 중 하나를" in bisect.result_lines(Result("together", ["a", "b", "r"]), names)[0]
    lines = bisect.result_lines(Result("core", ["r"]), names, refw="r", requires={"a": ["r"], "b": ["r"], "r": []})
    assert "업데이트" in lines[0] and "모두 동작하지 않습니다(켜져 있는 모드 중 2개)" in lines[1]
    lines = bisect.result_lines(Result("core", ["a"]), names, refw="r", requires={"b": ["a"]})
    assert "'B'도 동작하지 않습니다" in lines[0]
    assert "3개" in bisect.result_lines(Result("not_mods"), names, unmanaged=3)[-1]
    lines = bisect.result_lines(Result("not_mods"), names, refw_backup=True)
    assert "직접 넣어 두었던 dinput8.dll" in lines[0] and "무결성" in lines[1]
    assert Result("found", ["a"]).can_disable and not Result("not_mods").can_disable
    assert Result.from_dict(Result("aborted", reason="x").to_dict()) == Result("aborted", reason="x")
    assert Result.from_dict({"kind": "nonsense"}) is None


# ---- with a ModManager -------------------------------------------------------------------------------
def install(manager, tmp_path, name, files):
    with manager.prepare_install(make_zip(tmp_path / "zips" / f"{name}.zip", files)) as s:
        return manager.install_variant(s, s.variants[0])


@pytest.fixture
def mods(manager, tmp_path, game_dir):
    """REFramework, a Lua library, a script needing it, three plain mods (one off) and one with options."""
    write(game_dir / "re_chunk_000.pak.sub_000.pak.patch_002.pak", "official-2")
    refw = install(manager, tmp_path, "REFramework", {"dinput8.dll": "MZ"})
    lib = install(manager, tmp_path, "_CatLib", {"reframework/autorun/_CatLib.lua": "return {}"})
    app = install(manager, tmp_path, "App", {"reframework/autorun/app.lua": 'require("_CatLib")'})
    a = install(manager, tmp_path, "Armor A", {"natives/STM/a.tex": "a"})
    b = install(manager, tmp_path, "Armor B", {"natives/STM/b.tex": "b"})
    c = install(manager, tmp_path, "Armor C", {"natives/STM/c.tex": "c"})
    with manager.prepare_install(make_zip(tmp_path / "zips" / "Opts.zip", {
            "Opts/One/natives/STM/o.tex": "1", "Opts/Two/natives/STM/o.tex": "2"})) as s:
        opts = manager.install_package(s, s.variants, None, [0])
    manager.set_enabled(c.id, False)
    manager.set_enabled(opts.id, True)
    manager.set_options(opts.id, [])  # on, but no option chosen: nothing to apply
    manager.deploy()
    return {"refw": refw.id, "lib": lib.id, "app": app.id, "a": a.id, "b": b.id, "c": c.id, "opts": opts.id}


def state(manager):
    return ([m.id for m in manager.mods], sorted(m.id for m in manager.mods if m.enabled),
            {m.id: list(m.selected) for m in manager.mods})


def drive(manager, session, step, crash):
    """Answer the search like a user whose game crashes when ``crash(on)``, deploying each step."""
    while isinstance(step, Step):
        manager.deploy()
        on = {m.id for m in manager.mods if m.enabled}
        assert set(step.on) <= on and not (set(step.off) & on)
        session.mark_launched()
        step = bisect.answer(manager, session, crash(on), detected="비정상 종료를 확인했습니다 (0xC0000005)")
    return step


def test_a_search_with_the_manager_leaves_the_user_s_profile_alone(manager, mods, game_dir):
    before = state(manager)
    original_snapshot = json.loads(json.dumps(manager.profiles["기본"]))
    plan = bisect.prepare(manager)
    assert plan.base == [mods["refw"], mods["lib"]]
    assert plan.units == [mods["refw"], mods["lib"], mods["app"], mods["a"], mods["b"]]
    assert plan.excluded == [mods["opts"]] and plan.profile == "기본" and plan.game_version

    session, step = bisect.start(manager, plan, record="20261016-153922")
    assert manager.profile == bisect.TEMP_PROFILE and bisect.active(manager.data_dir)
    assert session.record_id == "20261016-153922" and session.launched is None and not session.awaiting_answer
    assert manager.profiles["기본"] == original_snapshot and isinstance(step, Step) and manager.dirty
    with pytest.raises(bisect.BisectError):
        bisect.start(manager, plan)

    result = drive(manager, session, step, lambda on: mods["a"] in on)
    assert result.kind == "found" and result.mods == [mods["a"]] and result.p >= 0.95 and result.pivots >= 2
    assert manager.profiles["기본"] == original_snapshot  # still never touched
    lines = "\n".join(session.report_lines())
    assert "결과: 원인으로 의심되는 모드: Armor A (가능성 약 " in lines and "튕김" in lines
    assert "이 모드를 끈 실행" in lines and "언제 튕기나요: 같은 장소·행동에서 매번" in lines

    bisect.finish(manager, session)
    assert state(manager) == before and manager.profile == "기본" and list(manager.profiles) == ["기본"]
    assert not bisect.active(manager.data_dir) and bisect.load(manager.data_dir) is None and manager.dirty
    manager.deploy()
    assert (game_dir / "natives/STM/a.tex").read_text() == "a"  # everything as it was


def test_turning_the_culprit_off_in_the_original_profile(manager, mods):
    session, step = bisect.start(manager, bisect.prepare(manager))
    result = drive(manager, session, step, lambda on: mods["b"] in on)
    assert result.mods == [mods["b"]] and result.can_disable
    bisect.finish(manager, session, disable=result.mods)
    assert manager.profile == "기본" and not manager.get(mods["b"]).enabled and manager.get(mods["a"]).enabled
    assert manager.get(mods["opts"]).enabled  # what the search left out is as it was


def test_a_mod_with_its_library(manager, mods):
    session, step = bisect.start(manager, bisect.prepare(manager))
    result = drive(manager, session, step, lambda on: mods["app"] in on)
    assert result.kind == "found" and result.mods == [mods["app"]]
    # the library is on whenever the script is, REFramework whenever anything that needs it is
    assert all(mods["lib"] in s["on"] for s in session.steps if mods["app"] in s["on"])
    assert all(mods["refw"] in s["on"] for s in session.steps if set(s["on"]) - {mods["refw"]})
    bisect.finish(manager, session)

    session, step = bisect.start(manager, bisect.prepare(manager))
    result = drive(manager, session, step, lambda on: mods["lib"] in on)
    assert result.kind == "core" and result.mods == [mods["lib"]]
    lines = bisect.result_lines(result, session.names, mods["refw"], session.data["requires"])
    assert "'App'도 동작하지 않습니다" in "\n".join(lines) and "계속 켜 두었던" not in "\n".join(lines)
    bisect.finish(manager, session)


def test_the_search_goes_on_after_a_restart(manager, mods):
    from mhwmm.manager import ModManager

    session, step = bisect.start(manager, bisect.prepare(manager))
    manager.deploy()
    session.mark_launched(1234.0)
    session.note_exit({"pid": 7, "code": 0xC0000005, "ended": 1300.0})
    assert session.launched == 1234.0 and session.record_id == ""

    again = ModManager(manager.data_dir)
    resumed = bisect.load(again.data_dir)
    assert resumed is not None and again.profile == resumed.temp_profile and bisect.problem(again, resumed) == ""
    assert resumed.next() == step and resumed.awaiting_answer
    assert gamewatch.GameExit.from_dict(resumed.current["exit"]).crashed
    assert bisect.apply_step(again, resumed) == step and not again.dirty  # nothing to change again
    nxt = bisect.answer(again, resumed, None)  # 잘 모르겠어요: the same step again
    assert nxt == step and not resumed.awaiting_answer and resumed.current["retries"] == 1
    result = drive(again, resumed, nxt, lambda on: mods["b"] in on)
    assert result.mods == [mods["b"]]
    assert resumed.steps[0]["exit"] is None  # the exit noted before the "unsure" answer is not kept
    bisect.finish(again, resumed)
    assert again.profile == "기본" and bisect.TEMP_PROFILE not in again.profiles


def test_reasons_to_stop(manager, mods, game_dir):
    session, _step = bisect.start(manager, bisect.prepare(manager))
    assert bisect.problem(manager, session) == ""
    write(game_dir / "re_chunk_000.pak.sub_000.pak.patch_003.pak", "update")  # a game update
    assert "업데이트" in bisect.problem(manager, session)
    result = session.abort(bisect.problem(manager, session))
    assert result.kind == "aborted" and session.next() == result and bisect.load(manager.data_dir).result == result
    bisect.finish(manager, session)

    session, _step = bisect.start(manager, bisect.prepare(manager))
    manager.switch_profile("기본")
    assert "다른 프로필" in bisect.problem(manager, session)
    with pytest.raises(bisect.BisectError):
        bisect.apply_step(manager, session)
    bisect.finish(manager, session)
    assert manager.profile == "기본" and list(manager.profiles) == ["기본"]

    session, _step = bisect.start(manager, bisect.prepare(manager))
    manager.remove(mods["b"])
    assert "지워져" in bisect.problem(manager, session)
    bisect.finish(manager, session)


def test_a_missing_original_profile_comes_back_from_the_copy(manager, mods):
    before = state(manager)
    session, step = bisect.start(manager, bisect.prepare(manager))
    drive(manager, session, step, lambda on: mods["a"] in on)
    del manager.profiles["기본"]  # (a profile change the search should have blocked)
    bisect.finish(manager, session)
    assert manager.profile == "기본" and state(manager) == before


def test_the_temporary_profile_name_is_free(manager, mods):
    manager.create_profile(bisect.TEMP_PROFILE)
    manager.switch_profile("기본")
    session, _step = bisect.start(manager, bisect.prepare(manager))
    assert session.temp_profile == "원인 찾기 (임시 2)" and manager.profile == session.temp_profile
    bisect.finish(manager, session)
    assert sorted(manager.profiles) == sorted(["기본", bisect.TEMP_PROFILE])


def test_a_broken_session_file_is_ignored(manager):
    write(manager.data_dir / bisect.SESSION_FILE, json.dumps({"format": 99}))  # (a newer version's)
    assert bisect.load(manager.data_dir) is None and not bisect.active(manager.data_dir)
    write(manager.data_dir / bisect.SESSION_FILE, "{not json")
    assert bisect.load(manager.data_dir) is None and not bisect.active(manager.data_dir)


def test_the_last_normal_exit_speeds_the_search_up(manager, mods, tmp_path):
    context = manager.crash_context()
    gamewatch.save_last_good(manager.crash_dir, gamewatch.Detection(
        False, exit=gamewatch.GameExit(1, 0, 10_000.0, started=1_000.0)), context)
    new = install(manager, tmp_path, "New Armor", {"natives/STM/n.tex": "n"})
    plan = bisect.prepare(manager)
    assert plan.changes is not None and plan.changes.added == [new.id] and plan.units[-1] == new.id
    (good,) = [o for o in plan.passive if o.source == "last_good"]
    assert new.id not in good.on and good.on == set(plan.units) - {new.id} and good.seconds == 9000.0
    assert plan.singles[0][0] == new.id  # the only mod not seen running fine
    assert any("덜 의심합니다" in line for line in plan.lines())
    session, step = bisect.start(manager, plan)
    result = drive(manager, session, step, lambda on: new.id in on)
    assert result.mods == [new.id] and len(session.steps) <= 4
    bisect.finish(manager, session)
    # without it the same search takes longer
    gamewatch_file = manager.crash_dir / gamewatch.LAST_GOOD_FILE
    gamewatch_file.unlink()
    session, step = bisect.start(manager, bisect.prepare(manager))
    drive(manager, session, step, lambda on: new.id in on)
    assert len(session.steps) > 4
    bisect.finish(manager, session)


def test_the_search_from_the_command_line(manager, mods, game_dir, capsys, monkeypatch):
    from mhwmm.cli import main
    from mhwmm.manager import ModManager

    monkeypatch.setenv("APPDATA", "")  # REFramework's fallback folder of this PC is not part of the test
    data = str(manager.data_dir)
    assert main(["--data-dir", data, "bisect"]) == 0
    out = capsys.readouterr().out
    assert "진행 중인 원인 찾기가 없습니다" in out and "다른 모드에 필요한 모드(그 모드를 켤 때 함께 켬): REFramework, _CatLib" in out
    assert "(바꾸려면: bisect start --when " in out  # (nothing runs yet: start takes them)
    assert main(["--data-dir", data, "bisect", "fine"]) == 1  # nothing to answer for
    assert main(["--data-dir", data, "bisect", "start"]) == 0
    out = capsys.readouterr().out
    assert "켜져 있는 모드 5개 중" in out and "원인 찾기 1번째 실행: " in out and "bisect crashed" in out
    assert main(["--data-dir", data, "bisect", "start"]) == 1

    # while it runs, what would change the mods it turns on and off waits
    capsys.readouterr()
    for args in (["disable", "Armor A"], ["profile", "기본"], ["install", str(game_dir)], ["options", "Opts", "--set", "1"],
                 ["materials", "repair"], ["move", "Armor A", "1"]):
        assert main(["--data-dir", data, *args]) == 1
        assert bisect.BLOCKED in capsys.readouterr().err
    assert main(["--data-dir", data, "profile"]) == 0 and bisect.TEMP_PROFILE in capsys.readouterr().out

    # answered like a user whose game crashes with Armor B; each step is in the game folder
    for _ in range(12):
        on = {m.id for m in ModManager(manager.data_dir).mods if m.enabled}
        assert (game_dir / "natives/STM/b.tex").exists() == (mods["b"] in on)
        assert main(["--data-dir", data, "bisect", "crashed" if mods["b"] in on else "fine"]) == 0
        out = capsys.readouterr().out
        if "원인으로 의심되는 모드" in out:
            break
    assert "원인으로 의심되는 모드: Armor B (가능성 약 " in out and "bisect stop --disable" in out
    assert main(["--data-dir", data, "bisect"]) == 0
    assert "결과: 원인으로 의심되는 모드: Armor B" in capsys.readouterr().out

    assert main(["--data-dir", data, "bisect", "stop", "--disable"]) == 0
    assert "원래 프로필로 돌아왔습니다: 기본 (Armor B 끔)" in capsys.readouterr().out
    after = ModManager(manager.data_dir)
    assert after.profile == "기본" and list(after.profiles) == ["기본"] and not bisect.active(after.data_dir)
    assert not after.get(mods["b"]).enabled and after.get(mods["a"]).enabled and after.get(mods["opts"]).enabled
    assert not (game_dir / "natives/STM/b.tex").exists() and (game_dir / "natives/STM/a.tex").exists()
    assert main(["--data-dir", data, "disable", "Armor A"]) == 0  # nothing waits any more


def test_stopping_from_the_command_line_before_a_result(manager, mods, game_dir, capsys):
    from mhwmm.cli import main
    from mhwmm.manager import ModManager

    data = str(manager.data_dir)
    assert main(["--data-dir", data, "bisect", "start"]) == 0
    assert main(["--data-dir", data, "bisect", "retry"]) == 0  # 잘 모르겠어요: the same step
    assert "원인 찾기 1번째 실행: " in capsys.readouterr().out
    assert main(["--data-dir", data, "bisect", "stop", "--disable"]) == 1  # nothing found to turn off yet
    assert main(["--data-dir", data, "bisect", "stop"]) == 0
    after = ModManager(manager.data_dir)
    assert after.profile == "기본" and all(after.get(i).enabled for i in (mods["a"], mods["b"]))
    assert (game_dir / "natives/STM/a.tex").exists() and (game_dir / "natives/STM/b.tex").exists()


def test_a_plan_made_before_a_change_is_refused(manager, mods):
    plan = bisect.prepare(manager)
    manager.set_enabled(mods["c"], True)  # (while the start window was open, say)
    with pytest.raises(bisect.BisectError, match="준비하는 동안"):
        bisect.start(manager, plan)
    manager.set_enabled(mods["c"], False)
    with pytest.raises(bisect.BisectError):  # the same mods on, but something changed meanwhile
        bisect.start(manager, plan)
    assert manager.profile == "기본" and not bisect.active(manager.data_dir)
    session, _step = bisect.start(manager, bisect.prepare(manager))
    bisect.finish(manager, session)


def plant_chain_pak(manager, game_dir, tmp_path):
    """A PAK mod deployed the way versions before 1.7.0 did: renamed into the game's patch chain."""
    install(manager, tmp_path, "Pak Mod", {"x.pak": "PAKDATA"})
    manager.deploy()
    path = manager.data_dir / "deployment.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    key = next(k for k, e in manifest["files"].items() if e.get("pak"))
    entry = manifest["files"].pop(key)
    chain = "re_chunk_000.pak.sub_000.pak.patch_003.pak"
    (game_dir / entry["path"]).replace(game_dir / chain)
    st = (game_dir / chain).stat()
    entry.update(path=chain, size=st.st_size, mtime=st.st_mtime_ns)
    manifest["files"][chain] = entry
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert manager.outdated_paks() == [chain]


def test_old_patch_chain_paks_are_moved_before_a_search(manager, mods, game_dir, tmp_path, capsys):
    from mhwmm.cli import main

    plant_chain_pak(manager, game_dir, tmp_path)
    # moving them changes the game's fingerprint: the search would stop at once as after a game update
    with pytest.raises(bisect.BisectError, match="예전 방식"):
        bisect.start(manager, bisect.prepare(manager))
    assert main(["--data-dir", str(manager.data_dir), "bisect", "start"]) == 0  # the command line moves them first
    assert "예전 방식(패치 번호)으로 넣은 PAK을 먼저" in capsys.readouterr().out
    session = bisect.load(manager.data_dir)
    assert not list(game_dir.glob("*.patch_003.pak")) and bisect.problem(manager.__class__(manager.data_dir), session) == ""


def test_a_hand_placed_refw_comes_back_with_its_mod_off(manager, tmp_path, game_dir):
    write(game_dir / "dinput8.dll", "REFramework placed by hand")
    refw = install(manager, tmp_path, "REFramework", {"dinput8.dll": "MZ"})
    install(manager, tmp_path, "Armor A", {"natives/STM/a.tex": "a"})
    manager.deploy()
    plan = bisect.prepare(manager)
    assert plan.refw_backup and "dinput8.dll" + bisect.BACKUP_MARK in plan.unmanaged
    assert any("직접 넣어 두었던 dinput8.dll" in line for line in plan.lines())
    session, step = bisect.start(manager, plan)
    assert session.refw_backup
    result = drive(manager, session, step, lambda on: True)  # (both builds crash)
    assert result.kind == "not_mods"
    off = [s for s in session.steps if s["kind"] == "everything_off"]
    assert off and (game_dir / "dinput8.dll").read_text() == "REFramework placed by hand"  # it was not "all off"
    assert "직접 넣어 두었던 dinput8.dll" in session.result_lines(result, refw.id)[0]
    bisect.finish(manager, session)
    manager.deploy()
    assert (game_dir / "dinput8.dll").read_text() == "MZ"


def test_a_step_s_game_ending_after_stopping_is_not_the_last_good_set(manager, mods, game_dir):
    session, _step = bisect.start(manager, bisect.prepare(manager))
    manager.deploy()
    launched = time.time()
    manager.note_launch()  # the step's game is started...
    session.mark_launched(launched)
    bisect.finish(manager, session)  # ...and the search stopped (그만두기) while it runs
    manager.deploy()
    exit = gamewatch.GameExit(1234, 0, launched + 400, launched + 2)  # (it quits normally later)
    detection = gamewatch.after_exit(manager.crash_dir, exit, launched, game_dir, manager.crash_context(),
                                     keep_good=not bisect.active(manager.data_dir))
    assert detection.normal and gamewatch.load_last_good(manager.crash_dir) is None
    # a launch outside a search is kept
    manager.note_launch()
    exit = gamewatch.GameExit(1235, 0, time.time() + 400, time.time() + 2)
    detection = gamewatch.after_exit(manager.crash_dir, exit, time.time(), game_dir, manager.crash_context())
    assert detection.normal and gamewatch.load_last_good(manager.crash_dir) is not None


def test_settings_saved_in_the_game_folder_survive_a_search(manager, tmp_path, game_dir):
    install(manager, tmp_path, "REFramework", {"dinput8.dll": "MZ"})
    install(manager, tmp_path, "Cfg Script", {"reframework/autorun/cfg.lua": "x",
                                              "reframework/data/CfgScript/config.json": '{"stock": 1}'})
    install(manager, tmp_path, "Armor A", {"natives/STM/a.tex": "a"})
    b = install(manager, tmp_path, "Armor B", {"natives/STM/b.tex": "b"})
    manager.deploy()
    config = game_dir / "reframework/data/CfgScript/config.json"
    touch_later(config, '{"user": "my settings"}')  # the script saved the user's settings
    session, step = bisect.start(manager, bisect.prepare(manager))
    result = drive(manager, session, step, lambda on: b.id in on)
    assert result.mods == [b.id]
    bisect.finish(manager, session)
    manager.deploy()
    assert config.read_text() == '{"user": "my settings"}'


# ---- v2: the plan, the session, format 1, texts ---------------------------------------------------------
class Ev:
    """A stand-in for evidence.Evidence: the plan reads its attributes with defaults."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def twelve():
    us = [facts(id=f"m{i}", name=f"Mod {i}", needs_refw=True, kinds=frozenset({"loose"})) for i in range(12)]
    return [facts(id="r", name="REFramework", refw=True, kinds=frozenset({"reframework"})), *us,
            facts(id="h", name="Hidden", hidden=True, needs_refw=True)]


def test_the_plan_with_and_without_evidence():
    mods = twelve()
    owners = {"natives/stm/a.tex": ["m1", "m2"], "natives/stm/b.tex": ["m3", "h", "m4"], "natives/stm/c.tex": ["m9"]}
    plan = bisect.make_plan(mods, owners=owners, profile="기본")
    assert plan.units == ["r", *[f"m{i}" for i in range(12)]] and plan.hidden == ["h"] and plan.base == ["r"]
    assert plan.masking == {"m1": ["m2"], "m3": ["m4"]} and plan.content == [f"m{i}" for i in range(12)]
    assert "덮이는 모드도 함께 끕니다 (2쌍)" in "\n".join(plan.lines())
    assert all("m1" not in s.on or "m2" in s.on for s in [plan.first])
    rows = plan.ranking_rows()
    assert plan.uniform and len(rows) == 5 and max(p for _l, p, _r in rows) - min(p for _l, p, _r in rows) < 1e-9
    assert plan.ranking_note() == bisect.NO_EVIDENCE and plan.dominant is None and plan.estimate_top is None
    assert not plan.informative
    kinds = bisect.make_plan(mods, evidence=Ev(weights={"m1": 2.0}, informative=False))  # (only the kind of mod)
    assert not kinds.uniform and not kinds.informative and kinds.ranking_note() == bisect.NO_EVIDENCE_KINDS
    assert bisect.Plan.uniform.__doc__
    # a crash in a mod's plugin: that mod first, about 40-50%, and the first launch looks at it
    ev = Ev(shares={"m7": 0.5}, weights={"m2": 3.0, "x": 9.0}, reasons={"m7": ["튕긴 곳이 이 모드의 플러그인입니다 (m7.dll+0x10)"]},
            record_title="튕김 기록 10월 20일 21:43", notes=["이전 실행 기록 6개"])
    plan = bisect.make_plan(mods, owners=owners, evidence=ev)
    label, p, why = plan.ranking_rows()[0]
    assert label == "'Mod 7'" and 0.35 <= p <= 0.5 and why == ["튕긴 곳이 이 모드의 플러그인입니다 (m7.dll+0x10)"]
    assert not plan.uniform and plan.informative and plan.ranking_note().startswith("그 밖: 나머지 모드 ")
    assert "모드 문제가 아님 약 " in plan.ranking_note()
    assert plan.estimate_top == 3 and "가장 의심되는 모드가 원인이면 약 3번, 아니면 약 " in plan.intro()
    assert plan.first.kind in ("off_some", "on_some") and plan.first.focus == ["m7"]
    assert plan.ranking[0].mods == ("m7",) and plan.involvement["m7"] > 0.4 and plan.evidence is ev
    # the answer to 언제 튕기나요 picks how reliable a crash is thought to be, and the first crash time
    rare = bisect.make_plan(mods, owners=owners, evidence=ev, when="rare")
    assert rare.data["params"]["r_prior"] == list(bisect.engine_mod.R_PRIORS["rare"]) and rare.when == "rare"
    timed = bisect.make_plan(mods, evidence=Ev(when_hint="time", minutes_hint=12.0))
    assert timed.when == "time" and timed.minutes == 12.0 and timed.crash_times == [720.0]
    assert timed.first.minutes == 27 and timed.first.play.startswith("27분 넘게 해 보세요.")
    assert "지난번에는" not in timed.first.play  # (a default from the answer, not a record)
    report = {"source": "report", "on": [f"m{i}" for i in range(12)] + ["r", "h"], "crashed": True, "rho": 1.0,
              "seconds": 600.0, "label": "튕김 기록"}
    recorded = bisect.make_plan(mods, evidence=Ev(crash_times=[600.0], passive=[report], when_hint="time",
                                                  minutes_hint=10.0))
    assert recorded.crash_times == [] and recorded.passive[0].seconds == 600.0 and "h" not in recorded.passive[0].on
    assert "(지난번에는 실행 후 약 10분 만에 튕겼습니다)" in recorded.first.play and recorded.first.minutes == 22
    assert recorded.data["passive"][0]["label"] == "튕김 기록"
    # at a place, an action or a piece of equipment the play time says nothing: no minutes asked for, none weighed
    for when in ("same", "equip", "start"):
        at = bisect.make_plan(mods, evidence=Ev(crash_times=[600.0], passive=[report]), when=when)
        assert at.first.minutes == 0 and "지난번에는" not in at.first.play and not at.data["params"]["timed_fine"]
    # a crash with a mod on that the search does not have now (removed, off): not a launch of today's mods
    stale = bisect.make_plan(mods, evidence=Ev(passive=[dict(report, on=[*report["on"], "gone"]),
                                                        {**report, "source": "history", "on": ["m1", "off"]},
                                                        {**report, "source": "history", "on": ["m2", "h"]},
                                                        {**report, "source": "history", "on": ["m3", "gone"],
                                                         "crashed": False}]))
    assert [(o.source, o.crashed, sorted(o.on)) for o in stale.passive] == \
        [("report", True, sorted(plan.units)), ("history", False, ["m3"])]  # (the default report; a fine one is safe)


@pytest.mark.parametrize("culprit", ["A", "B"])
def test_a_patch_that_needs_its_base_and_covers_one_of_its_files(tmp_path, culprit):
    # the usual patch: B needs A (its Nexus requirement) and overwrites one texture of A. A masking edge A -> B
    # with the requirement B -> A would keep both always on together, and neither could ever be named
    mods = [facts(id="refw", name="REFramework", refw=True, kinds=frozenset({"reframework"})),
            facts(id="A", name="Armor X", nexus_id=100, needs_refw=True, kinds=frozenset({"loose"})),
            facts(id="B", name="Armor X - Color Patch", needs_refw=True, kinds=frozenset({"loose"}),
                  requirements=[{"name": "Armor X", "nexus_id": 100}]),
            *[facts(id=i, name=f"Mod {i}", needs_refw=True, kinds=frozenset({"loose"})) for i in "CDE"]]
    owners = {"natives/stm/armor/x/body_alb.tex": ["A", "B"], "natives/stm/c.tex": ["C", "D"]}
    plan = bisect.make_plan(mods, owners=owners)
    assert plan.requires["B"] == ["A", "refw"] and plan.masking == {"C": ["D"]}
    assert bisect.masking(owners, plan.units) == {"A": ["B"], "C": ["D"]}  # (without the requirements)
    session = bisect.Session.new(tmp_path, plan, "orig", {}, "temp")
    nxt = session.next()
    while isinstance(nxt, Step):
        assert "A" in nxt.on or "B" not in nxt.on
        nxt = session.record(culprit in nxt.on)
    assert nxt.kind == ("core" if culprit == "A" else "found") and nxt.mods == [culprit]  # (A: B needs it)


@pytest.mark.parametrize("culprit", ["New Armor", "Armor C"])
def test_hidden_mods_never_reach_the_game(manager, tmp_path, game_dir, culprit):
    # every file of Old Armor loses to New Armor: it is not a suspect, and it is on in a step only with New Armor
    # (else its a.tex - a copy the user's own setup never loads - would reach the game)
    install(manager, tmp_path, "REFramework", {"dinput8.dll": "MZ"})
    lost = install(manager, tmp_path, "Old Armor", {"natives/STM/a.tex": "old"})
    a = install(manager, tmp_path, "New Armor", {"natives/STM/a.tex": "new", "natives/STM/b.tex": "b"})
    c = install(manager, tmp_path, "Armor C", {"natives/STM/c.tex": "c"})
    manager.deploy()
    plan = bisect.prepare(manager)
    assert plan.hidden == [lost.id] and lost.id not in plan.units and plan.data["hidden_winners"] == {lost.id: [a.id]}
    session, step = bisect.start(manager, plan)
    cause = {"New Armor": a.id, "Armor C": c.id}[culprit]
    seen = []
    while isinstance(step, Step):
        manager.deploy()
        seen.append((a.id in step.on, manager.get(lost.id).enabled, (game_dir / "natives/STM/a.tex").read_text()
                     if (game_dir / "natives/STM/a.tex").exists() else None))
        session.mark_launched()
        step = bisect.answer(manager, session, cause in step.on)
    assert step.mods == [cause] and any(not new for new, _old, _tex in seen)
    assert all(old == new and tex in (("new",) if new else (None,)) for new, old, tex in seen)
    assert a.id in plan.units and lost.id not in session.mod_ids
    bisect.finish(manager, session)
    assert manager.get(lost.id).enabled  # (the user's profile: as it was)


def test_a_mod_with_a_root_pak_is_never_hidden(manager, tmp_path, game_dir):
    # Old Armor's loose texture loses to New Armor Tex, but its root .pak goes to pak_mods and is always loaded: it
    # is a unit like any other (on its own when its pak is what crashes), never left out as 'hidden'
    install(manager, tmp_path, "REFramework", {"dinput8.dll": "MZ"})
    old = install(manager, tmp_path, "Old Armor", {"re_chunk_000.pak.patch_005.pak": "PAKDATA",
                                                   "natives/STM/a.tex": "old"})
    new = install(manager, tmp_path, "New Armor Tex", {"natives/STM/a.tex": "new"})
    install(manager, tmp_path, "Armor C", {"natives/STM/c.tex": "c"})
    install(manager, tmp_path, "Armor D", {"natives/STM/d.tex": "d"})
    manager.deploy()
    assert list((game_dir / "pak_mods").glob("*.pak")) and manager.hidden_mods() == []
    plan = bisect.prepare(manager)
    assert plan.hidden == [] and old.id in plan.units and plan.masking == {old.id: [new.id]}
    session, step = bisect.start(manager, plan)
    while isinstance(step, Step):
        manager.deploy()
        session.mark_launched()
        step = bisect.answer(manager, session, any((game_dir / "pak_mods").glob("*.pak")))  # (the old pak crashes)
    assert step.kind == "found" and step.mods == [old.id]
    bisect.finish(manager, session)


def test_a_session_goes_on_after_a_restart_and_in_a_worker(manager, mods, tmp_path):
    import threading

    from mhwmm.manager import ModManager

    session, step = bisect.start(manager, bisect.prepare(manager))
    assert not session.legacy and session.ready and step.engine and step.remaining[0] >= 1
    assert session.data["format"] == 2 and session.data["engine"] == bisect.engine_mod.ENGINE_FORMAT
    nxt = bisect.answer(manager, session, mods["b"] in step.on)
    again = ModManager(manager.data_dir)
    resumed = bisect.load(again.data_dir)
    assert not resumed.ready and resumed.next() == nxt and resumed.ready  # the same step from the answers alone
    # what the manager saw of the step's game is kept for a restart
    resumed.mark_launched(1000.0)
    resumed.note_run({"seconds": 300.0, "auto": "normal", "log": True, "unexposed": [mods["a"], "nope"], "tail": [],
                      "record": ""})
    third = bisect.load(again.data_dir)
    assert third.current["run"]["seconds"] == 300.0 and third.current["run"]["unexposed"] == [mods["a"], "nope"]
    crashed = mods["b"] in third.next().on
    bisect.answer(again, third, crashed)
    last = third.steps[-1]
    assert last["seconds"] == 300.0 and last["auto"] == "normal" and last["log"] is True
    assert last["unexposed"] == ([mods["a"]] if mods["a"] in last["on"] else [])
    # an answer and what comes next from a worker give what record() gives
    copies = []
    for name in ("one", "two"):
        folder = tmp_path / name
        folder.mkdir()
        copies.append(bisect.Session(folder / bisect.SESSION_FILE, json.loads(json.dumps(third.data))))
    one, two = copies
    got_one = one.record(True)
    two.add_answer(True)
    assert not two.ready and two.steps[-1]["crashed"]
    box = []
    worker = threading.Thread(target=lambda: box.append(two.settle()))
    worker.start()
    worker.join(30)
    assert box == [got_one] and two.ready and two.last_change().startswith("원인 찾기: ")
    assert [{k: v for k, v in s.items() if k != "time"} for s in one.steps] == \
        [{k: v for k, v in s.items() if k != "time"} for s in two.steps]
    bisect.finish(again, third)


def v223_session(manager, mods, answers):
    """bisect.json exactly as v2.2.3 wrote it (format 1), with ``answers`` given."""
    units, base = [mods["app"], mods["a"], mods["b"]], [mods["refw"], mods["lib"]]
    order = [*base, *units]
    requires = {mods["refw"]: [], mods["lib"]: [mods["refw"]], mods["app"]: [mods["refw"], mods["lib"]],
                mods["a"]: [mods["refw"]], mods["b"]: [mods["refw"]]}
    manager.save()
    snapshot = json.loads(json.dumps(manager.profiles["기본"]))
    manager.create_profile(bisect.TEMP_PROFILE, copy_current=True)
    steps = []
    for number, crashed in enumerate(answers, 1):
        nxt = bisect.next_step(units, base, base, requires, None, [(s["on"], s["crashed"]) for s in steps], order)
        steps.append({"on": nxt.on, "crashed": crashed(set(nxt.on)), "kind": nxt.kind, "number": number,
                      "time": 1760000000.0 + number, "launched": 1760000000.0, "exit": None, "detected": ""})
    data = {"format": 1, "started": 1760000000.0, "original_profile": "기본", "original_snapshot": snapshot,
            "temp_profile": bisect.TEMP_PROFILE, "game_version": manager.game_version(), "record": "",
            "names": {m.id: m.name for m in manager.mods}, "order": order, "units": units, "base": base, "core": base,
            "requires": requires, "split": None, "estimate": 4, "excluded": [mods["opts"]],
            "unmanaged": {"total": 0, "paths": []}, "refw_backup": False, "changes": "", "steps": steps,
            "current": None, "result": None}
    write(manager.data_dir / bisect.SESSION_FILE, json.dumps(data, ensure_ascii=False))
    return units, base, requires, order


def test_a_search_started_by_v2_2_3_goes_on_as_it_started(manager, mods):
    crash = lambda on: mods["b"] in on  # noqa: E731
    units, base, requires, order = v223_session(manager, mods, [crash, crash])
    assert bisect.active(manager.data_dir)
    session = bisect.load(manager.data_dir)
    assert session.legacy and session.ready and session.mod_ids == [*base, *units]
    assert bisect.problem(manager, session) == ""
    step = bisect.apply_step(manager, session)
    while isinstance(step, Step):
        want = bisect.next_step(units, base, base, requires, None, [(s["on"], s["crashed"]) for s in session.steps],
                                order, 4)
        assert step == want and not step.engine and "단계: " in session.banner(step)
        step = bisect.answer(manager, session, crash(set(step.on)))
    assert step.kind == "found" and step.mods == [mods["b"]] and step.p is None
    assert bisect.result_title(step, session.names) == "원인으로 보이는 모드: Armor B"  # (the old texts)
    assert session.step_lines()[0].startswith("1단계 · ") and "결과: 원인으로 보이는 모드: Armor B" in session.report_lines()
    assert session.more() == step and session.interim().kind == "unsure"
    bisect.finish(manager, session)
    assert manager.profile == "기본" and list(manager.profiles) == ["기본"] and not bisect.active(manager.data_dir)


def test_unsure_more_and_interim(manager, mods, capsys):
    from mhwmm.cli import main

    session, step = bisect.start(manager, bisect.prepare(manager))
    session.data["params"]["max_launches"] = 3  # (a short budget)
    session._next = None
    step = bisect.apply_step(manager, session)
    flip = iter([True, False, True, False, True, False])
    while isinstance(step, Step):
        step = bisect.answer(manager, session, next(flip))
    assert step.kind == "unsure" and step.runs == 3 and step.ranking and step.r_mean is not None
    assert sum(s.p for s in step.ranking) <= 1 + 1e-9 and step.evidence[0].endswith("%") or "미만" in step.evidence[0]
    assert bisect.result_title(step, session.names) == "아직 하나로 좁히지 못했습니다."
    lines = session.result_lines(step)
    assert any("'계속 찾기'로 6번 더" in line for line in lines)
    guess = session.interim()
    assert guess.kind == "unsure" and guess.ranking and guess.p is not None and not session.data["result"] is None
    nxt = session.more()  # 계속 찾기: six more launches
    assert isinstance(nxt, Step) and session.extra == 6 and session.result is None and nxt.number == 4
    assert bisect.load(manager.data_dir).extra == 6
    # 그만두기 --disable: an unsure result names a mod only from 50%
    session.data["result"] = Result("unsure", [], 3, p=0.3, ranking=list(guess.ranking)).to_dict()
    session.save()
    assert main(["--data-dir", str(manager.data_dir), "bisect", "stop", "--disable"]) == 1
    session.data["result"] = Result("unsure", [mods["a"]], 3, p=0.62).to_dict()
    session.save()
    assert main(["--data-dir", str(manager.data_dir), "bisect", "stop", "--disable"]) == 0
    assert "Armor A 끔" in capsys.readouterr().out


def test_one_more_check_needs_a_third_pivot_pair(manager, mods):
    session, step = bisect.start(manager, bisect.prepare(manager))
    result = drive(manager, session, step, lambda on: mods["a"] in on)
    assert result.kind == "found" and result.pivots == 2 and result.can_confirm
    assert bisect.CONFIRM_NOTE in result.evidence
    nxt = session.more()  # 한 번 더 확인
    assert session.strict and isinstance(nxt, Step) and nxt.reason.startswith("confirm")
    step = bisect.apply_step(manager, session)
    again = drive(manager, session, step, lambda on: mods["a"] in on)
    assert again.kind == "found" and again.mods == [mods["a"]] and again.pivots >= 3 and not again.can_confirm
    bisect.finish(manager, session)


def test_one_more_check_of_two_mods_that_each_crash(tmp_path):
    # both mods broken ('each'): 한 번 더 확인 needs a crash with each one alone on and a fine run with it off - more
    # launches than a single mod, all given, none of them repeated
    mods = [ModFacts(f"m{i}", f"Mod {i}", True, kinds=frozenset({"loose"})) for i in range(2)]
    session = bisect.Session.new(tmp_path, bisect.make_plan(mods), "orig", {}, "temp")
    nxt = session.next()
    while isinstance(nxt, Step):
        nxt = session.record(bool(nxt.on))  # (either one on crashes)
    assert nxt.kind == "each" and nxt.pivots == 2 and nxt.can_confirm
    nxt = session.more()
    seen = []
    while isinstance(nxt, Step):
        assert nxt.on not in seen and nxt.reason.startswith("confirm")
        seen.append(nxt.on)
        nxt = bisect.Session.load(tmp_path).record(bool(nxt.on))  # (from bisect.json each time: a restart)
    assert nxt.kind == "each" and nxt.mods == ["m0", "m1"] and nxt.pivots >= 3 and seen


@pytest.mark.parametrize("record", ["rare", "device"])
def test_a_single_mod_that_crashes_now_and_then_is_named(tmp_path, record):
    # one managed mod, crashes that come and go ('가끔', also from a graphics error record): no one launch adds the
    # third pivot pair, the search makes the two that do instead of stopping unsure
    mods = [ModFacts("x", "Only Mod", True, kinds=frozenset({"pak"}))]
    ev = Ev(device_error="DEVICE_HUNG", weights={"x": 1.6}) if record == "device" else None
    plan = bisect.make_plan(mods, evidence=ev, when="rare")
    session = bisect.Session.new(tmp_path, plan, "orig", {}, "temp")
    nxt = session.next()
    while isinstance(nxt, Step):
        nxt = session.record("x" in nxt.on)
    assert nxt.kind == "found" and nxt.mods == ["x"] and nxt.runs <= 5


DEVICE_TITLE = "10월 21일 18:46 · 그래픽 오류 (DEVICE_HUNG)"


def test_a_graphics_error_record_starts_a_search_like_any_other(tmp_path):
    # a DEVICE_HUNG record (the graphics card could not finish the work the game gave it - in a modded game often a
    # mod's render data) is a crash like any other: the same rules as any search started now (launches with every mod
    # off only while "not the mods" keeps some probability, never two of them up front), the reproduction prior from
    # 언제 튕기나요, and nothing about graphics cards or drivers in the steps or the result of a named mod
    ev = Ev(device_error="DEVICE_HUNG", record_title=DEVICE_TITLE)
    for when, prior in (("same", "middle"), ("start", "sharp"), ("time", "middle"), ("rare", "rare")):
        plan = bisect.make_plan(twelve(), evidence=ev, when=when)
        params = plan.data["params"]
        assert params["controls"] == 0 and params["r_prior"] == list(bisect.engine_mod.R_PRIORS[prior]), when
        assert params == bisect.make_plan(twelve(), when=when).data["params"], when  # (as without the record)
        assert plan.data["record_title"] == DEVICE_TITLE and "device_error" not in plan.data
    session = bisect.Session.new(tmp_path, bisect.make_plan(twelve(), evidence=ev), "orig", {}, "temp")
    nxt = session.next()
    texts = []
    while isinstance(nxt, Step):
        texts.append(bisect.step_text(nxt, session.names))
        nxt = session.record("m4" in nxt.on)
    assert nxt.kind == "found" and nxt.mods == ["m4"] and nxt.runs <= 7
    assert not any("그래픽" in t for t in texts)
    assert session.result_lines(nxt) == ["모드를 최신 버전으로 업데이트하거나, 재질 점검·복구를 해 보거나, 만든 사람에게 알려 보세요."]


def test_a_search_v2_2_5_began_from_a_graphics_error_record_goes_on_as_it_began(tmp_path):
    # v2.2.5 stored the "rare" prior and two fine launches with every mod off before a mod is named (Params.controls)
    # for a DEVICE_HUNG record. Its bisect.json (the same formats: the engine's model did not change) goes on with
    # them after the update - its launches stay what they were - and says nothing about the graphics card
    from dataclasses import replace

    from mhwmm.bisect_engine import Params

    session = bisect.Session.new(tmp_path, bisect.make_plan(twelve(), evidence=Ev(device_error="DEVICE_HUNG")),
                                 "orig", {}, "temp")
    stored = replace(Params(), r_prior=bisect.engine_mod.R_PRIORS["rare"], timed_fine=False, controls=2).to_dict()
    del stored["none_controls"], stored["max_controls"]  # (keys of a later release)
    session.data.update(params=stored, r_prior=list(stored["r_prior"]), device_error="DEVICE_HUNG")
    session.save()
    assert json.loads((tmp_path / bisect.SESSION_FILE).read_text("utf-8"))["params"]["controls"] == 2
    steps = []
    nxt = bisect.Session.load(tmp_path).next()
    while isinstance(nxt, Step):
        steps.append(nxt)
        session = bisect.Session.load(tmp_path)  # (a restart each time)
        params = Params.from_dict(session.data["params"])
        assert params.controls == 2 and params.r_prior == bisect.engine_mod.R_PRIORS["rare"]
        assert params.none_controls == 0 and "none_controls" not in session.data["params"]
        nxt = session.record("m4" in nxt.on)
    assert nxt.kind == "found" and nxt.mods == ["m4"]
    controls = [s for s in steps if s.reason == "control"]
    assert len(controls) == 2 and all(not s.on for s in controls)  # (both fine: the culprit was off)
    texts = [bisect.step_text(s, session.names) for s in controls]
    assert bisect.CONTROL_TEXT in texts[0] and bisect.CONTROL_AGAIN in texts[1]
    assert not any("그래픽" in t for t in texts)
    assert not any("그래픽" in line for line in session.result_lines(nxt))
    assert bisect.CONTROL_FINE.format(fine=2) in nxt.evidence


def not_the_mods(seed, r=0.7):
    """A game that crashes r of the time whatever is on."""
    rnd = random.Random(seed)
    return lambda on: rnd.random() < r


def replay(path, answer):
    """Every step and the result of the search kept in ``path``, restarting the session before each answer."""
    steps = []
    nxt = bisect.Session.load(path).next()
    while isinstance(nxt, Step):
        steps.append(nxt)
        nxt = bisect.Session.load(path).record(answer(set(nxt.on)))
    return steps, nxt


def test_a_new_search_runs_the_game_with_every_mod_off_before_it_names_a_mod(tmp_path):
    # a crash that comes and goes whatever is on can look like any mod: a search started now (any record, any answer
    # to 언제 튕기나요; 가끔 more firmly) runs the game with every mod off while "not the mods" keeps some
    # probability before it names a mod. The step says so plainly, and the result counts those launches in its 근거
    engine_mod = bisect.engine_mod
    for when in ("same", "rare"):
        params = bisect.make_plan(twelve(), when=when).data["params"]
        for key, value in {**engine_mod.CURRENT, **engine_mod.WHEN_RULES.get(when, {})}.items():
            assert params[key] == value, (when, key)
    assert engine_mod.WHEN_RULES["rare"]["none_controls"] < engine_mod.CURRENT["none_controls"]
    seen = []
    for k, seed in enumerate(range(40)):
        path = tmp_path / str(k)
        bisect.Session.new(path, bisect.make_plan(twelve()), "orig", {}, "temp").save()
        steps, result = replay(path, not_the_mods(seed))
        controls = [s for s in steps if s.reason == "control"]
        assert all(not s.on and s.kind == "everything_off" for s in controls)
        if controls:
            seen.append((controls, result, bisect.Session.load(path)))
        if len(seen) >= 3:
            break
    assert seen
    for controls, result, session in seen:
        texts = [bisect.step_text(s, session.names) for s in controls]
        assert bisect.CONTROL_TEXT in texts[0]
        for s, t in zip(controls[1:], texts[1:]):  # (later ones: how the earlier ones went)
            assert s.control >= 1
            after = bisect.CONTROL_AFTER_CRASH.format(crashes=bisect.off_crash_text(s.control, s.control_crashed))
            assert (after if s.control_crashed else bisect.CONTROL_AGAIN) in t
        assert not any("그래픽" in t for t in texts) and "모든 모드를 끄고 적용했습니다." in texts[0]
        if result.kind in ("found", "together", "each"):
            assert any(line.startswith("모든 모드를 끈 실행 ") for line in result.evidence)


def test_a_crash_with_every_mod_off_is_said_after_the_answer_and_in_the_result(tmp_path):
    # the first launch with every mod off crashes (가끔, the culprit m4 otherwise exact): the line after the answer says
    # so and how "not the mods" moved, the next such launch says why it comes again, and the named result counts them
    # in its 근거 and says what to look at if turning the mod off is not enough (after a restart too)
    session = bisect.Session.new(tmp_path, bisect.make_plan(twelve(), when="rare"), "orig", {}, "temp")
    session.save()
    nxt, changes, controls, crashed = session.next(), [], [], 0
    while isinstance(nxt, Step):
        session = bisect.Session.load(tmp_path)
        if nxt.reason == "control":
            controls.append(nxt)
        off_crash = nxt.reason == "control" and not crashed
        crashed += off_crash
        nxt = session.record(True if off_crash else "m4" in nxt.on)
        changes.append((off_crash, session.last_change()))
    assert crashed == 1 and len(controls) >= 2
    change = next(text for off, text in changes if off)
    assert f" · {bisect.OFF_CRASHED}(모드 문제가 아님 " in change and change.startswith("원인 찾기: ")
    assert all(bisect.OFF_CRASHED not in text for off, text in changes if not off)
    texts = [bisect.step_text(s, session.names) for s in controls]
    assert bisect.CONTROL_TEXT in texts[0]
    assert (bisect.CONTROL_AFTER_CRASH.format(crashes="모든 모드를 끈 실행에서도 튕겼습니다.")) in texts[1]
    assert controls[1].control == 1 and controls[1].control_crashed == 1
    assert nxt.kind == "found" and nxt.mods == ["m4"]
    bare = [s for s in session.steps if not s["on"]]
    assert bisect.CONTROL_SOME.format(count=len(bare), fine=len(bare) - 1) in nxt.evidence
    assert not any(line.startswith(bisect.CONTROL_FINE.format(fine=len(bare) - 1)) for line in nxt.evidence)
    note = bisect.OFF_CRASH_NOTE.format(count=1)
    assert session.result_lines(nxt)[-1] == note and bisect.Session.load(tmp_path).result_lines(nxt)[-1] == note
    # (no such crash: no note, and the 근거 line counts them all fine; the range shown at each step held them)
    other = bisect.Session.new(tmp_path / "fine", bisect.make_plan(twelve(), when="rare"), "orig", {}, "temp")
    nxt, shown = other.next(), []
    while isinstance(nxt, Step):
        shown.append(nxt.remaining)
        nxt = other.record("m4" in nxt.on)
    assert all(lo <= len(shown) - k <= hi for k, (lo, hi) in enumerate(shown)), shown
    bare = [s for s in other.steps if not s["on"]]
    assert nxt.kind == "found" and bare and bisect.CONTROL_FINE.format(fine=len(bare)) in nxt.evidence
    assert all("모든 모드를 끈 실행에서도" not in line for line in other.result_lines(nxt))
    assert bisect.off_crash_text(3, 1) == "모든 모드를 끈 실행 3번 중 1번 튕겼습니다."
    assert bisect.off_crash_text(2, 2) == "모든 모드를 끈 실행 2번 모두 튕겼습니다."


def test_not_the_mods_counts_every_launch_with_every_mod_off(tmp_path):
    # the 근거 of "not the mods" counts the fine launches with every mod off too ("4번 중 3번"), and says "모두" only when
    # each of them crashed
    seen = set()
    for seed in range(30):
        path = tmp_path / str(seed)
        bisect.Session.new(path, bisect.make_plan(twelve()), "orig", {}, "temp").save()
        _steps, result = replay(path, not_the_mods(seed))
        if result.kind != "not_mods":
            continue
        bare = [s for s in bisect.Session.load(path).steps if not s["on"]]
        crashed = sum(1 for s in bare if s["crashed"])
        assert crashed >= 2
        if crashed < len(bare):
            assert result.evidence[0] == f"모든 모드를 끈 실행 {len(bare)}번 중 {crashed}번 튕겼습니다."
            seen.add("some")
        else:
            assert result.evidence[0] == bisect.CONTROL_NONE_FINE.format(count=len(bare))
            seen.add("all")
        if seen == {"some", "all"}:
            break
    assert seen == {"some", "all"}


def test_a_search_stored_before_the_launches_with_every_mod_off_goes_on_without_them(tmp_path):
    # bisect.json of a search started before this release (its params without none_controls / max_controls) goes on
    # with the rule it began with: launch for launch what the field defaults give, no launch asked for "not the mods",
    # and the file keeps its parameters; the same answers to a search started now do ask for such a launch
    differs = 0
    for seed in range(12):
        runs = {}
        for label in ("stored", "defaults", "current"):
            path = tmp_path / f"{label}{seed}"
            session = bisect.Session.new(path, bisect.make_plan(twelve()), "orig", {}, "temp")
            if label != "current":
                params = bisect.Params().to_dict()
                if label == "stored":
                    del params["none_controls"], params["max_controls"]
                session.data["params"] = params
            session.save()
            steps, result = replay(path, not_the_mods(seed))
            runs[label] = ([(s.on, s.reason) for s in steps], result.kind, result.mods)
            if label == "stored":
                assert "none_controls" not in json.loads((path / bisect.SESSION_FILE).read_text("utf-8"))["params"]
                assert not any(s.reason == "control" for s in steps)
        assert runs["stored"] == runs["defaults"], seed
        differs += runs["current"] != runs["stored"]
    assert differs


def test_suspects_as_likely_as_the_top_one_are_never_named_alone(tmp_path):
    # every mod suspected the same (no evidence): the bar, the line after an answer and 그만두기 never name the first
    # mod of the list as "the most suspected" - it would be blamed for its place in the load order
    session = bisect.Session.new(tmp_path, bisect.make_plan(twelve()), "orig", {}, "temp")
    step = session.next()
    assert "아직 없음(같은 정도 5개 이상, 각각 약 " in bisect.progress_text(step, session.names)
    note = session.interim_note()
    assert note.startswith(" 아직 두드러지게 의심되는 모드는 없습니다(같은 정도 5개 이상, 각각 약 ") and "'" not in note
    nxt = session.record("m5" in step.on)
    tied = bisect.tied_top(nxt.ranking)
    change = session.last_change()
    if len(tied) > 3:
        assert change.startswith("원인 찾기: 아직 두드러지게 의심되는 모드는 없습니다(같은 정도 ") and " → " in change
        assert "'" not in change
    else:
        assert all(bisect.suspect_label(s, session.names) in change for s in tied)
    # two as likely as each other: both, each with its own move
    two = [bisect.Suspect("one", (i,), p) for i, p in (("m1", 0.37), ("m2", 0.37), ("m3", 0.07))]
    session._answered, session._before = True, {}
    session._engine = session._engine_now()
    assert session._last_change(Step(2, 6, "split", [], [], ranking=two)) == \
        "원인 찾기: 'Mod 1' 1% 미만 → 37% · 'Mod 2' 1% 미만 → 37%"
    session.interim = lambda: Result("unsure", [], 1, p=0.37, ranking=two)
    assert session.interim_note() == " 지금까지 가장 의심되는 것은 'Mod 1', 'Mod 2'(각각 약 37%)입니다."


def engine_step(kind, on, off, **kw):
    base = dict(reason="split", focus=[], ranking=[bisect.Suspect("one", ("a",), 0.45)], suspects=4, p=0.45,
                remaining=(3, 5), minutes=0, crash_minutes=None, play=bisect.PLAY["same"])
    base.update(kw)
    return Step(2, 6, kind, on, off, **base)


def test_v2_texts():
    names = {"a": "A", "b": "B", "c": "C", "r": "REFramework"}
    assert [bisect.percent(p) for p in (0.004, 0.01, 0.5, 0.994, 0.996)] == ["1% 미만", "1%", "50%", "99%", "99% 이상"]
    assert bisect.launches_text(3, 5) == "약 3~5번" and bisect.launches_text(4, 4) == "약 4번"
    S = bisect.Suspect
    assert bisect.suspect_label(S("one", ("a",), 0.5), names) == "'A'"
    assert bisect.suspect_label(S("and", ("a", "b"), 0.5), names) == "'A'+'B' (함께 켰을 때만)"
    assert bisect.suspect_label(S("and3", ("a", "b", "c"), 0.5), names) == "'A'+'B'+'C' (함께 켰을 때만)"
    assert bisect.suspect_label(S("or", ("a", "b"), 0.5), names) == "'A', 'B' (각각)"
    assert bisect.suspect_label(S("none", (), 0.5), names).startswith("모드 문제가 아님")
    assert bisect.suspect_label(S("rest", (), 0.5), names) == "그 밖의 경우"
    play = " " + bisect.PLAY["same"]
    texts = {
        "everything_off": (engine_step("everything_off", [], ["r", "a", "b"]), "모든 모드를 끄고 적용했습니다."),
        "full": (engine_step("full", ["r", "a", "b"], []), "처음처럼 모드 3개를 모두 켜고 적용했습니다."),
        "base": (engine_step("base", ["r"], ["a", "b"]), "REFramework와 다른 모드에 필요한 모드 1개만 켜고 적용했습니다."),
        "off_some": (engine_step("off_some", ["r", "b"], ["a", "c"], focus=["a"]),
                     "'A'만 끄고 나머지는 그대로 적용했습니다. (그 모드가 필요한 모드 1개도 함께 껐습니다)"),
        "on_some": (engine_step("on_some", ["r", "a"], ["b", "c"], focus=["a"]), "'A'만 켜고(필요한 모드 포함 2개) 적용했습니다."),
        "split": (engine_step("split", ["r", "a"], ["b", "c"], off_suspects=2),
                  "아직 의심되는 모드 4개 중 2개를 끄고(모드 2개 켬) 적용했습니다."),
    }
    for kind, (step, body) in texts.items():
        assert bisect.step_text(step, names) == f"원인 찾기 2번째 실행: {body}{play}", kind
        assert bisect.step_text(step, names, applied=False).endswith(bisect.UNAPPLIED + play)
    assert "꺼 두었습니다" in bisect.step_text(texts["off_some"][0], names, applied=False)
    assert "직접 넣어 두었던 dinput8.dll" in bisect.step_text(texts["everything_off"][0], names, refw_backup=True)
    others = engine_step("split", ["r", "b"], ["a", "c"], reason="others", found=["a"], off_suspects=1)
    assert bisect.step_text(others, names).startswith("원인 찾기 2번째 실행: 'A'을(를) 끈 채로 다른 원인이 있는지 확인합니다. ")
    kind = engine_step("on_some", ["r", "a"], ["b"], reason="kind", focus=["a"])
    assert "한 모드만의 문제인지, 함께 켰을 때만인지 확인합니다. 'A'만 켜고" in bisect.step_text(kind, names)
    assert [bisect.step_summary(texts[k][0], names) for k in ("everything_off", "full", "off_some", "on_some", "split")] == \
        ["모든 모드를 끄고 실행", "처음처럼 모두 켜고 실행", "'A'만 끄고 실행", "'A'만 켜고 실행", "모드 2개를 켜고 실행"]
    assert bisect.progress_text(texts["split"][0], names) == "의심되는 모드 4개 · 가장 의심: 'A' 약 45% · 앞으로 약 3~5번"
    tie = engine_step("split", ["r"], ["a", "b"], ranking=[bisect.Suspect("one", ("r",), 0.2), bisect.Suspect("one", ("a",), 0.2)])
    assert "가장 의심: 아직 없음(같은 정도 2개, 각각 약 20%)" in bisect.progress_text(tie, names)
    five = engine_step("split", ["r"], ["a", "b"], ranking=[bisect.Suspect("one", (i,), 0.1) for i in "rabcd"])
    assert "가장 의심: 아직 없음(같은 정도 5개 이상, 각각 약 10%)" in bisect.progress_text(five, names)
    timed = engine_step("split", ["r", "a"], ["b"], minutes=22, crash_minutes=10.0)
    assert bisect.run_note(timed, 240) == "이번에는 4분만 실행했습니다. 튕기던 상황까지 가지 못했다면 '잘 모르겠어요'를 고르세요."
    assert bisect.run_note(timed, 900) == "이번 실행 15분 · 지난번 튕긴 시점(약 10분)보다 오래 했습니다."
    assert bisect.run_note(timed, None) == "" and bisect.run_note(engine_step("full", ["a"], []), 30) == ""
    assert "가장 의심되는 'A'의 파일을 한 번도 읽지 않았습니다" in bisect.exposure_note(texts["on_some"][0], ["a"], names)
    assert bisect.exposure_note(texts["on_some"][0], ["b"], names) == ""
    # results
    R = Result
    assert bisect.result_title(R("found", ["a"], p=0.97), names) == "원인으로 의심되는 모드: A (가능성 약 97%)"
    assert bisect.result_title(R("together", ["a", "b"], p=0.96), names) == \
        "함께 켜면 튕기는 것으로 의심되는 모드: A, B (가능성 약 96%)"
    assert bisect.result_title(R("each", ["a", "b"], p=0.95), names) == "각각 튕기게 하는 것으로 의심되는 모드: A, B (가능성 약 95%)"
    assert bisect.result_title(R("core", ["r"], p=0.999), names) == "원인으로 의심되는 모드: REFramework (가능성 약 99% 이상)"
    assert bisect.result_title(R("not_mods", p=0.96), names) == \
        "모드 문제가 아닐 가능성이 높습니다 (약 96%). 모든 모드를 꺼도 튕겼습니다."
    assert bisect.result_title(R("unsure", p=0.4), names) == "아직 하나로 좁히지 못했습니다."
    assert bisect.result_title(R("no_repro", p=0.1), names) == bisect.result_title(R("no_repro"), names)
    assert bisect.disable_unsure_text(R("unsure", ["a"], p=0.62), names) == "'A'(약 62%)을(를) 끄고 원래 프로필로"
    assert bisect.result_lines(R("each", ["a", "b"], p=0.95), names) == ["두 모드를 모두 끄거나 업데이트해 보세요."]
    noisy = bisect.result_lines(R("unsure", runs=9, p=0.3, r_mean=0.6), names)
    assert "튕기기도 하고 괜찮기도 했습니다" in noisy[0] and "'계속 찾기'로 6번 더" in noisy[1]
    assert bisect.result_lines(R("unsure", runs=9, p=0.3, r_mean=0.9), names)[0] == \
        "게임을 9번 실행했지만 아직 하나로 좁히지 못했습니다."
    assert R("unsure", ["a"], p=0.62).can_disable and not R("unsure", [], p=0.3).can_disable and R("each", ["a"]).can_disable
    full = R("found", ["a"], 7, "", 0.97, 2, True, [bisect.Suspect("one", ("a",), 0.97)], ["근거"], 0.93)
    assert R.from_dict(json.loads(json.dumps(full.to_dict()))) == full
    assert R.from_dict({"kind": "found", "mods": ["a"], "p": "x", "ranking": [{"family": "bad"}], "pivots": True}) == \
        R("found", ["a"])


def test_the_evidence_lines_of_a_result(manager, mods):
    session, step = bisect.start(manager, bisect.prepare(manager, evidence=Ev(
        reasons={mods["b"]: ["튕긴 곳이 이 모드가 넣은 파일 'b.tex'입니다"]})))
    result = drive(manager, session, step, lambda on: mods["b"] in on)
    lines = result.evidence
    assert result.kind == "found" and lines[0].startswith("원인 찾기에서 이 모드를 켠 실행 ") and "번 튕겼습니다." in lines[0]
    assert any(line.startswith("원인 찾기에서 이 모드를 끈 실행 ") and "모두 괜찮았습니다" in line for line in lines)
    assert "기록: 튕긴 곳이 이 모드가 넣은 파일 'b.tex'입니다" in lines
    assert any(line.startswith("다른 가능성: ") for line in lines)
    assert session.step_lines()[0].startswith("1번째 · ") and " → " in session.step_lines()[0]
    report = "\n".join(session.report_lines())
    assert "시작할 때 의심한 정도:" in report and "근거: 튕긴 곳이" in report and "1번째 (" in report
    bisect.finish(manager, session)


def older_launches(mods, x):
    """Earlier sessions (launches.json) with and without ``x``: they all point at it."""
    others = [mods["refw"], mods["lib"], mods["app"], *(m for m in (mods["a"], mods["b"]) if m != x)]
    passive = []
    for crashed, on in ((True, [mods["refw"], x]), (False, others), (True, [mods["refw"], *others[3:], x]),
                        (False, others), (True, [*others, x]), (False, others), (False, [mods["lib"], mods["refw"]]),
                        (False, [mods["refw"]]), (False, [])):
        passive.append({"source": "history", "on": on, "crashed": crashed, "rho": 1.0 if crashed else 0.6,
                        "seconds": 600.0 if crashed else 3600.0, "unknown": [], "unexposed": [], "e": 0.02,
                        "label": "이전 실행"})
    return passive


def test_older_launches_speed_the_search_up_but_never_name_a_mod(manager, mods):
    # earlier sessions may never have reached what triggers the crash (a user who never goes there never does), and
    # a crash kept for looking like the record's says little: a mod is named only on the search's own launches
    plan = bisect.prepare(manager, evidence=Ev(passive=older_launches(mods, mods["a"])))
    assert isinstance(plan.first, Step) and plan.singles[0][0] == mods["a"] and plan.singles[0][1] > 0.5
    session, step = bisect.start(manager, plan)
    result = drive(manager, session, step, lambda on: mods["a"] in on)
    assert result.kind == "found" and result.mods == [mods["a"]] and 2 <= result.runs <= 3
    assert any(line.startswith("원인 찾기 전의 기록(튕김 기록·이전 실행): 이 모드를 켜고 튕김 ") for line in result.evidence)
    bisect.finish(manager, session)
    # they point at A, B is the culprit, and the user gives one wrong answer anywhere: A is never named
    for flip in range(8):
        session, step = bisect.start(manager, bisect.prepare(manager, evidence=Ev(passive=older_launches(mods, mods["a"]))))
        k = 0
        while isinstance(step, Step):
            manager.deploy()
            crashed = (mods["b"] in step.on) != (k == flip)
            session.mark_launched()
            step = bisect.answer(manager, session, crashed)
            k += 1
        assert mods["a"] not in step.mods, (flip, step)
        bisect.finish(manager, session)


def test_odd_evidence_is_read_safely():
    mods = twelve()
    nan = float("nan")
    ev = Ev(weights={"m1": nan, "m2": "x", 3: 2.0}, shares="no", pairs=[("m1", "m2", 10.0), ("m1",), "x"],
            none_scale=nan, or_scale=-1, passive=[{"source": "report", "on": "x"}, 7, {"source": "other", "on": [],
                                                                                       "crashed": True}],
            crash_times=[nan, -5, 30.0], reasons={"m1": "not a list", "m2": ["a", "b", "c", "d"]}, when_hint="later",
            minutes_hint=-3, equipment=[("k", "투구", ["m1", 5]), ("bad",)], notes=None)
    plan = bisect.make_plan(mods, evidence=ev)
    assert plan.when == "same" and plan.minutes is None and plan.prior.none_scale == 1.0 and plan.prior.or_scale == 1.0
    assert [o.source for o in plan.passive] == ["report"] and plan.passive[0].on == frozenset(plan.units)
    assert plan.data["equipment"] == [["k", "투구", ["m1"]]] and plan.data["reasons"] == {"m2": ["a", "b", "c"]}
    assert plan.data["evidence_times"] == [30.0] and json.loads(json.dumps(plan.data)) == plan.data
    assert isinstance(plan.first, Step)


def answer_session(tmp_path, plan, crash, name):
    """A v2 session (no manager) answered by ``crash(on)``: the result and its steps."""
    folder = tmp_path / name
    folder.mkdir()
    session = bisect.Session.new(folder, plan, "기본", {}, bisect.TEMP_PROFILE)
    nxt = session.next()
    while isinstance(nxt, Step):
        session.mark_applied(nxt)
        nxt = session.record(crash(set(nxt.on)))
    return nxt, session


def test_refw_a_library_and_not_the_mods(tmp_path):
    mods = [facts(id="r", name="REFramework", refw=True), facts(id="lib", name="Lib", provides=frozenset({"lib"}),
                                                                 needs_refw=True),
            facts(id="app", name="App", lua_requires=["lib"], needs_refw=True),
            *[facts(id=f"m{i}", name=f"Mod {i}", needs_refw=True) for i in range(4)]]
    plan = bisect.make_plan(mods)
    result, session = answer_session(tmp_path, plan, lambda on: "r" in on, "refw")
    assert result.kind == "core" and result.mods == ["r"] and result.p >= 0.95
    assert "REFramework를 켰을 때 튕겼습니다" in session.result_lines(result, "r")[0]
    assert all("r" in s["on"] for s in session.steps if set(s["on"]) - {"r"})
    result, _session = answer_session(tmp_path, plan, lambda on: "lib" in on, "lib")
    assert result.kind == "core" and result.mods == ["lib"]
    result, session = answer_session(tmp_path, plan, lambda on: True, "none")
    assert result.kind == "not_mods" and result.p >= 0.95 and sum(1 for s in session.steps if not s["on"]) >= 2
    assert result.evidence[0].startswith("모든 모드를 끈 실행 ") and "모두 튕겼습니다" in result.evidence[0]
    result, session = answer_session(tmp_path, plan, lambda on: False, "never")
    assert result.kind == "no_repro" and not result.mods and len(session.steps) <= 6
    assert bisect.result_title(result, plan.names) == "처음처럼 모든 모드를 켜도 튕기지 않아 원인을 좁히지 못했습니다."
    result, session = answer_session(tmp_path, plan, lambda on: {"m1", "m2"} <= on, "pair")
    assert result.kind == "together" and result.mods == ["m1", "m2"]
    assert "하나씩 켰을 때는 괜찮았고 함께 켰을 때만 튕겼습니다." in result.evidence
    result, session = answer_session(tmp_path, plan, lambda on: bool({"m1", "m3"} & on), "each")
    assert result.kind == "each" and result.mods == ["m1", "m3"] and "한쪽만 켜도 튕겼습니다." in result.evidence
    assert result.can_disable and session.result_lines(result) == ["두 모드를 모두 끄거나 업데이트해 보세요."]


def test_the_command_line_with_v2_options(manager, mods, game_dir, capsys, monkeypatch):
    from mhwmm import accessed
    from mhwmm.cli import main

    monkeypatch.setenv("APPDATA", "")
    data = str(manager.data_dir)
    # the file record cannot be turned on: loose loading is off and natives\ has files (the armors)
    config = write(game_dir / "re2_fw_config.txt", "FontSize=16\r\n")
    assert main(["--data-dir", data, "bisect", "start", "--log-files"]) == 1
    assert accessed.LOG_UNAVAILABLE_LOOSE in capsys.readouterr().err and not bisect.active(manager.data_dir)
    # with loose loading on it only turns the record on, and puts it back at the end
    write(config, "FontSize=16\r\nLooseFileLoader_Enabled=true\r\n")
    assert main(["--data-dir", data, "bisect", "start", "--when", "rare", "--log-files"]) == 0
    out = capsys.readouterr().out
    assert "게임이 읽는 파일 기록을 켰습니다" in out and bisect.RANKING_HEADING in out and "원인 찾기 1번째 실행: " in out
    assert "의심되는 모드 " in out and "(바꾸려면: bisect stop 후 bisect start --when " in out  # (it runs already)
    assert config.read_bytes() == b"FontSize=16\r\nLooseFileLoader_Enabled=true\r\nLooseFileLoader_LogAccessedFiles=true\r\n"
    session = bisect.load(manager.data_dir)
    assert session.when == "rare" and session.data["params"]["r_prior"] == list(bisect.engine_mod.R_PRIORS["rare"])
    assert session.refw_restore["values"] == {accessed.LOG_KEY: None}
    assert main(["--data-dir", data, "bisect"]) == 0
    assert "의심 순위 (기록과 지금까지의 답으로 매긴 것이고" in capsys.readouterr().out

    # an answer without the game watched: the play time given counts as the run's length
    assert main(["--data-dir", data, "bisect", "fine", "--minutes", "3"]) == 0
    out = capsys.readouterr().out
    session = bisect.load(manager.data_dir)
    assert session.steps[0]["seconds"] == 180.0 and not session.steps[0]["crashed"] and "원인 찾기: " in out
    assert main(["--data-dir", data, "bisect", "more"]) == 1  # (nothing to go on with yet)
    assert "지금은 더 찾을 것이 없습니다" in capsys.readouterr().err

    # 아직 좁히지 못함: 계속 찾기 gives six more launches; 그만두기 --disable only from 50%
    guess = session.interim()
    session.data["result"] = Result("unsure", [], 1, p=0.3, ranking=list(guess.ranking)).to_dict()
    session.save()
    assert main(["--data-dir", data, "bisect"]) == 0
    assert "bisect more (6번 더)" in capsys.readouterr().out
    assert main(["--data-dir", data, "bisect", "stop", "--disable"]) == 1
    assert "50% 이상일 때만" in capsys.readouterr().err
    assert main(["--data-dir", data, "bisect", "more"]) == 0
    out = capsys.readouterr().out
    session = bisect.load(manager.data_dir)
    assert "계속 찾기: 원인 찾기를 이어 갑니다." in out and session.extra == 6 and session.result is None
    assert "원인 찾기 2번째 실행: " in out

    # stopping before a result says what is suspected most, and puts the setting back
    assert main(["--data-dir", data, "bisect", "stop"]) == 0
    out = capsys.readouterr().out
    assert "원래 프로필로 돌아왔습니다: 기본" in out and "지금까지 가장 의심되는 것은 '_CatLib', 'App'(각각 약 " in out
    assert accessed.RESTORED in out
    assert config.read_bytes() == b"FontSize=16\r\nLooseFileLoader_Enabled=true\r\nLooseFileLoader_LogAccessedFiles=false\r\n"
    assert not bisect.active(manager.data_dir)


def test_a_fine_run_shorter_than_the_crash_took_counts_weakly(tmp_path):
    mods = twelve()
    on = [f"m{i}" for i in range(12)] + ["r"]
    plan = bisect.make_plan(mods, evidence=Ev(crash_times=[600.0], when_hint="time", minutes_hint=10.0, passive=[
        {"source": "report", "on": on, "crashed": True, "rho": 1.0, "seconds": 600.0, "label": "튕김 기록"}]))
    for seconds, weak in ((120.0, True), (1500.0, False)):
        folder = tmp_path / f"s{int(seconds)}"
        folder.mkdir()
        session = bisect.Session.new(folder, plan, "기본", {}, bisect.TEMP_PROFILE)
        step = session.next()
        session.mark_applied(step)
        assert step.minutes == 22 and "(지난번에는 실행 후 약 10분 만에 튕겼습니다)" in step.play
        assert ("분만 실행했습니다" in bisect.run_note(step, seconds)) == weak
        assert ("지난번 튕긴 시점(약 10분)보다 오래 했습니다" in bisect.run_note(step, seconds)) == (not weak)
        session.record(False, run={"seconds": seconds, "auto": "normal"})
        change = session.last_change()
        assert change.startswith("원인 찾기: ") and " → " in change and (bisect.WEAK_ANSWER in change) == weak


def involvement_after(tmp_path, plan, name, answers):
    """A v2 session answered with ``answers`` [(crashed, seconds)]: P(part of the cause) of every unit after them."""
    folder = tmp_path / name
    folder.mkdir()
    session = bisect.Session.new(folder, plan, "기본", {}, bisect.TEMP_PROFILE)
    for crashed, seconds in answers:
        session.mark_applied(session.next())
        session.record(crashed, run={"seconds": seconds})
    return session, session._engine_now().summary().involvement


def test_the_play_time_of_a_fine_run_is_weighed_by_the_engine(tmp_path):
    # a 2-minute fine run against a 10-minute crash says little; a 25-minute one clears the mods that were on
    mods = twelve()
    on = [f"m{i}" for i in range(12)] + ["r"]
    plan = bisect.make_plan(mods, when="time", evidence=Ev(crash_times=[600.0], passive=[
        {"source": "report", "on": on, "crashed": True, "rho": 1.0, "seconds": 600.0}]))
    short, inv_short = involvement_after(tmp_path, plan, "short", [(False, 120.0)])
    long_, inv_long = involvement_after(tmp_path, plan, "long", [(False, 1500.0)])
    assert short._engine_now().obs[-1].g < 0.5 < long_._engine_now().obs[-1].g
    ms = [u for u in plan.units if u != "r"]
    assert max(inv_short[u] for u in ms) - min(inv_short[u] for u in ms) < 0.01  # (barely moved)
    assert all(inv_long[u] < 0.7 * inv_short[u] for u in plan.first.on)


def test_quick_reproductions_count_at_a_place_or_action(tmp_path):
    # 같은 장소·행동: the user walks straight to the spot (a crash 3 minutes in, fine runs of 4 minutes) although the
    # record crashed 9 minutes into a normal session - the culprit is found within the usual launches
    mods = twelve()
    on = [f"m{i}" for i in range(12)] + ["r"]
    plan = bisect.make_plan(mods, when="same", evidence=Ev(crash_times=[540.0], passive=[
        {"source": "report", "on": on, "crashed": True, "rho": 1.0, "seconds": 540.0}]))
    for culprit in ("m0", "m5", "m11"):
        folder = tmp_path / culprit
        folder.mkdir()
        session = bisect.Session.new(folder, plan, "기본", {}, bisect.TEMP_PROFILE)
        nxt = session.next()
        while isinstance(nxt, Step):
            assert nxt.minutes == 0 and "지난번에는" not in nxt.play
            session.mark_applied(nxt)
            crashed = culprit in nxt.on
            assert bisect.run_note(nxt, 240.0) == ""
            nxt = session.record(crashed, run={"seconds": 180.0 if crashed else 240.0})
            assert bisect.WEAK_ANSWER not in session.last_change()
        assert nxt.kind == "found" and nxt.mods == [culprit] and nxt.runs <= math.ceil(math.log2(14)) + 4


def test_minutes_typed_in_the_start_window_win_over_the_record(tmp_path):
    # the record crashed 45 minutes in; the user says "about 10 minutes": the bar asks for 22, not 100
    mods = twelve()
    on = [f"m{i}" for i in range(12)] + ["r"]
    plan = bisect.make_plan(mods, evidence=Ev(crash_times=[2700.0], when_hint="time", minutes_hint=45.0, passive=[
        {"source": "report", "on": on, "crashed": True, "rho": 1.0, "seconds": 2700.0}]))
    assert plan.first.minutes == 99 and "약 45분 만에" in plan.first.play
    session = bisect.Session.new(tmp_path, plan, "기본", {}, bisect.TEMP_PROFILE, when="time", minutes=10)
    step = session.next()
    assert step.minutes == 22 and step.play.startswith("22분 넘게 해 보세요.") and "45분" not in step.play
    assert session.data["minutes_typed"] and not session.data["params"]["passive_times"]
    session.mark_applied(step)
    assert "분만 실행했습니다" not in bisect.run_note(step, 12 * 60)
    session.record(False, run={"seconds": 12 * 60})
    assert session._engine_now().obs[-1].g > 0.5 and bisect.WEAK_ANSWER not in session.last_change()
    # the same minutes as the record's: the record's time stays
    same = bisect.Session.new(tmp_path / "same", plan, "기본", {}, bisect.TEMP_PROFILE, when="time", minutes=45.0)
    assert same.next().minutes == 99 and same.data["params"]["passive_times"]


def test_the_last_crash_time_is_the_latest_one(tmp_path):
    # the record crashed 9 minutes in, then the search's first launch 2 minutes in: "지난번에는 약 2분"
    mods = twelve()
    on = [f"m{i}" for i in range(12)] + ["r"]
    plan = bisect.make_plan(mods, evidence=Ev(crash_times=[540.0], when_hint="time", minutes_hint=9.0, passive=[
        {"source": "report", "on": on, "crashed": True, "rho": 1.0, "seconds": 540.0}]))
    assert "(지난번에는 실행 후 약 9분 만에 튕겼습니다)" in plan.first.play
    session = bisect.Session.new(tmp_path, plan, "기본", {}, bisect.TEMP_PROFILE)
    session.mark_applied(session.next())
    step = session.record(True, run={"seconds": 400.0})
    assert isinstance(step, Step) and step.crash_minutes == pytest.approx(400 / 60)
    assert "(지난번에는 실행 후 약 7분 만에 튕겼습니다)" in step.play


def test_an_unsure_result_names_a_mod_only_from_50_percent(tmp_path):
    # made by the engine (not by hand): a short budget and m4 crashing, with a quarter of the answers wrong
    mods = twelve()
    plan = bisect.make_plan(mods)
    named = []
    for seed in (0, 1):
        folder = tmp_path / f"s{seed}"
        folder.mkdir()
        session = bisect.Session.new(folder, plan, "기본", {}, bisect.TEMP_PROFILE)
        session.data["params"]["max_launches"] = 4
        session._next = None
        nxt = session.next()
        rnd = random.Random(seed)
        while isinstance(nxt, Step):
            session.mark_applied(nxt)
            nxt = session.record(("m4" in nxt.on) != (rnd.random() < 0.25))
        assert nxt.kind == "unsure"
        top = session._engine_now().summary()
        assert bool(nxt.mods) == (top.sure >= 0.5) and nxt.can_disable == bool(nxt.mods)
        if nxt.mods:
            assert nxt.p == pytest.approx(top.sure) and nxt.p >= 0.5
            assert bisect.disable_unsure_text(nxt, session.names) == f"'Mod 4'(약 {bisect.percent(nxt.p)})을(를) 끄고 원래 프로필로"
        named.append(bool(nxt.mods))
    assert named == [True, False]  # (both sides of the 50% rule)


def test_the_command_line_notes_and_a_pending_setting(manager, mods, game_dir, capsys, monkeypatch):
    import mhwmm.manager
    from mhwmm import accessed
    from mhwmm.cli import main

    monkeypatch.setenv("APPDATA", "")
    data = str(manager.data_dir)
    config = write(game_dir / "re2_fw_config.txt", "FontSize=16\r\nLooseFileLoader_Enabled=true\r\n")
    assert main(["--data-dir", data, "bisect", "start", "--when", "time", "--minutes", "10", "--log-files"]) == 0
    assert "22분 넘게 해 보세요." in capsys.readouterr().out
    # a crash 2 minutes in: no advice to play longer (the answer is taken at once)
    assert main(["--data-dir", data, "bisect", "crashed", "--minutes", "2"]) == 0
    out = capsys.readouterr().out
    assert "분만 실행했습니다" not in out and "원인 찾기: " in out
    # a short fine run: what to use next time instead, and the answer weighed weakly
    step = bisect.load(manager.data_dir).next()
    assert step.minutes >= 10
    assert main(["--data-dir", data, "bisect", "fine", "--minutes", "1"]) == 0
    out = capsys.readouterr().out
    assert f"이번에는 1분만 실행했습니다. 튕기던 상황까지 가지 못했다면 {bisect.CLI_RETRY}." in out
    assert bisect.WEAK_ANSWER in out
    # stopped while the game runs: the setting is put back by the next command once the game is closed
    monkeypatch.setattr(mhwmm.manager, "is_game_running", lambda: True)
    assert main(["--data-dir", data, "bisect", "stop"]) == 0
    assert accessed.RESTORE_PENDING in capsys.readouterr().out and "LogAccessedFiles=true" in config.read_text()
    assert main(["--data-dir", data, "list"]) == 0  # (still running: kept)
    assert accessed.RESTORED not in capsys.readouterr().out and accessed.pending(manager.data_dir)
    monkeypatch.setattr(mhwmm.manager, "is_game_running", lambda: False)
    assert main(["--data-dir", data, "list"]) == 0
    assert accessed.RESTORED in capsys.readouterr().out and accessed.pending(manager.data_dir) is None
    assert config.read_bytes() == b"FontSize=16\r\nLooseFileLoader_Enabled=true\r\nLooseFileLoader_LogAccessedFiles=false\r\n"


def test_facts_use_the_mod_stamp_with_its_edits(manager, tmp_path):
    """A mod never edited keeps the stamp older crash records and bisect.json have; a 모드 수정 changes it, so
    the last normal exit counts the mod as changed."""
    with manager.prepare_install(make_zip(tmp_path / "Edit.zip", {"natives/STM/a.tex": "a"})) as s:
        mod = manager.install_variant(s, s.variants[0])
    (found,) = bisect.facts(manager)
    assert found.stamp == f"{mod.installed_at}|{mod.updated_at}" == mod.files_stamp == mod.stamp
    last_good = {"context": {"mods": [{"id": mod.id, "enabled": True, "stamp": found.stamp}]}}
    assert bisect.changes_since(last_good, [found]).updated == []
    mod.edited_at = "2026-10-28T10:00:00"
    (edited,) = bisect.facts(manager)
    assert edited.stamp == f"{mod.installed_at}|{mod.updated_at}|2026-10-28T10:00:00" and mod.files_stamp == found.stamp
    assert bisect.changes_since(last_good, [edited]).updated == [mod.id]
