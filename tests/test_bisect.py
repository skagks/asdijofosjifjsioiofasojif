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


def test_the_plan_keeps_what_others_need_on():
    mods = [
        facts(id="r", name="REFramework", refw=True),
        facts(id="lib", name="Lib", provides=frozenset({"lib"}), needs_refw=True),
        facts(id="opt", name="Options", has_files=False, needs_option=True),
        facts(id="app", name="App", lua_requires=["lib"], needs_refw=True),
        facts(id="off", name="Off", enabled=False),
        facts(id="tex", name="Texture"),
    ]
    plan = bisect.make_plan(mods, unmanaged=(["natives/STM/x.tex"], 1), profile="기본")
    assert plan.base == ["r", "lib"] and plan.units == ["app", "tex"] and plan.excluded == ["opt"]
    assert plan.core == ["r", "lib"] and plan.count == 4 and plan.split is None and plan.problem == ""
    assert "켜져 있는 모드 4개" in plan.intro() and f"방식이라 약 {plan.runs}번 실행하면" in plan.intro()
    lines = "\n".join(plan.lines())
    assert "계속 켜 둘 모드: REFramework, Lib" in lines and "Options" in lines and "natives/STM/x.tex" in lines
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
    assert plan.units == ["a", "b", "c", "d"] and plan.split == 1  # the unchanged one first
    assert plan.runs_all >= plan.runs
    first = bisect.next_step(plan.units, plan.base, plan.core, plan.requires, plan.split)
    assert first.kind == "last_good" and set(first.on) == {"a", "r"}
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
    assert plan.base == [mods["refw"], mods["lib"]] and plan.units == [mods["app"], mods["a"], mods["b"]]
    assert plan.excluded == [mods["opts"]] and plan.profile == "기본" and plan.game_version

    session, step = bisect.start(manager, plan, record="20261016-153922")
    assert manager.profile == bisect.TEMP_PROFILE and bisect.active(manager.data_dir)
    assert session.record_id == "20261016-153922" and session.launched is None and not session.awaiting_answer
    assert manager.profiles["기본"] == original_snapshot and isinstance(step, Step) and manager.dirty
    with pytest.raises(bisect.BisectError):
        bisect.start(manager, plan)

    result = drive(manager, session, step, lambda on: mods["a"] in on)
    assert result.kind == "found" and result.mods == [mods["a"]]
    assert manager.profiles["기본"] == original_snapshot  # still never touched
    lines = "\n".join(session.report_lines())
    assert "결과: 원인으로 보이는 모드: Armor A" in lines and "튕김" in lines

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
    assert all(mods["lib"] in s["on"] for s in session.steps)  # the library stays on with the script
    bisect.finish(manager, session)

    session, step = bisect.start(manager, bisect.prepare(manager))
    result = drive(manager, session, step, lambda on: mods["lib"] in on)
    assert result.kind == "core" and result.mods == [mods["lib"]]
    assert any(s["on"] == [] for s in session.steps)  # tried with everything off
    lines = bisect.result_lines(result, session.names, mods["refw"], session.data["requires"])
    assert "'App'도 동작하지 않습니다" in "\n".join(lines)
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
    assert plan.split == len(plan.units) - 1
    session, step = bisect.start(manager, plan)
    assert step.kind == "last_good"
    result = drive(manager, session, step, lambda on: new.id in on)
    assert result.mods == [new.id] and len(session.steps) <= 3
    bisect.finish(manager, session)


def test_the_search_from_the_command_line(manager, mods, game_dir, capsys, monkeypatch):
    from mhwmm.cli import main
    from mhwmm.manager import ModManager

    monkeypatch.setenv("APPDATA", "")  # REFramework's fallback folder of this PC is not part of the test
    data = str(manager.data_dir)
    assert main(["--data-dir", data, "bisect"]) == 0
    out = capsys.readouterr().out
    assert "진행 중인 원인 찾기가 없습니다" in out and "계속 켜 둘 모드: REFramework, _CatLib" in out
    assert main(["--data-dir", data, "bisect", "fine"]) == 1  # nothing to answer for
    assert main(["--data-dir", data, "bisect", "start"]) == 0
    out = capsys.readouterr().out
    assert "켜져 있는 모드 5개 중" in out and "원인 찾기 1/" in out and "bisect crashed" in out
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
        if "원인으로 보이는 모드" in out:
            break
    assert "원인으로 보이는 모드: Armor B" in out and "bisect stop --disable" in out
    assert main(["--data-dir", data, "bisect"]) == 0
    assert "결과: 원인으로 보이는 모드: Armor B" in capsys.readouterr().out

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
    assert "원인 찾기 1/" in capsys.readouterr().out
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
