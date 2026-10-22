"""원인 모드 찾기 with everything together: a crash record (evidence) -> the plan -> the search with a real
ModManager, what the record of the files the game read says of a launch, the game's own crash report and the
REFramework setting put back at the end (synthetic logs, records and mods; no real user data)."""

import time
from datetime import datetime

import pytest

from mhwmm import accessed, bisect, crashes, evidence, gamewatch
from mhwmm.bisect import Step

from conftest import make_zip, write
from test_crashes import game_report_text, report_zip


def stamp(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def rec(t: float, msg: str, level: str = "info") -> str:
    return f"[{stamp(t)}] [REFramework] [{level}] {msg}"


def refw_log(game, start: float, crash_in=None, end: float = 600) -> str:
    """REFramework's log of a session; with ``crash_in`` (a module path) a fatal exception ``end`` s in."""
    lines = [rec(start, "REFramework entry"), rec(start, "Commit hash: d052e8fd3fd41d0a1a33394fcf6e6c19be39b1ee"),
             rec(start, "Tag: v1.5.9.1"), rec(start, f"Current game path: {game}"),
             rec(start + 2, "VM initialized"), rec(start + 30, "Hooked DirectX 12")]
    if crash_in is not None:
        t = start + end
        lines += [rec(t, "Exception occurred: c0000005", "error"), rec(t, "RIP (exception record): 7ff600001000", "error"),
                  rec(t, "Call stack:", "error"), rec(t, "", "error"), " Unknown symbol", f"  {crash_in} + 0x1234", "",
                  rec(t, f"Module: 7ff600000000 {crash_in}", "error")]
    else:
        lines.append(rec(start + end, "Hooked DirectX 12"))
    return "\r\n".join(lines) + "\r\n"


def access_log(start: float, entries) -> str:
    """reframework_accessed_files.txt: constructed 3 s after ``start``, then (offset, path) requests."""
    line = lambda t, msg: f"[{stamp(t)}] [LooseFileLoader] [info] {msg}"  # noqa: E731
    return "\n".join([line(start + 3, accessed.CONSTRUCTED), *(line(start + dt, p) for dt, p in entries)]) + "\n"


def install(manager, tmp_path, name, files):
    with manager.prepare_install(make_zip(tmp_path / "zips" / f"{name}.zip", files)) as s:
        return manager.install_variant(s, s.variants[0])


@pytest.fixture
def setup(manager, tmp_path, game_dir, monkeypatch):
    """REFramework, a plugin mod and eight loose armor mods, applied."""
    monkeypatch.setenv("APPDATA", "")
    ids = {"refw": install(manager, tmp_path, "REFramework", {"dinput8.dll": "MZ"}).id,
           "plugin": install(manager, tmp_path, "Artian QoL", {"reframework/plugins/ArtianQoL.dll": "MZ"}).id}
    for i in range(8):
        ids[f"a{i}"] = install(manager, tmp_path, f"Armor {i}", {f"natives/STM/art/armor{i}.tex": "t"}).id
    manager.deploy()
    return ids


def drive(manager, session, step, crash, run=None):
    """Answer like a user whose game crashes when ``crash(on)``, deploying each step: (result, launches)."""
    launches = 0
    while isinstance(step, Step):
        manager.deploy()
        session.mark_launched()
        launches += 1
        step = bisect.answer(manager, session, crash(set(step.on)), run=run(step) if run else None)
    return step, launches


def test_a_plugin_crash_record_finds_its_mod_in_a_few_launches(manager, setup, game_dir):
    ids = setup
    start = time.time() - 3600
    write(game_dir / "re2_framework_log.txt", refw_log(game_dir, start, game_dir / "reframework/plugins/ArtianQoL.dll"))
    record, = crashes.collect(manager.crash_dir, game_dir, manager.crash_context(), appdata="")
    assert record.problem() == "crash"

    owners = bisect.conflict_owners(manager)
    found = evidence.build(evidence.gather(manager, "", owners))  # (the newest recent crash is the record)
    assert found.record == record.id and found.shares == {ids["plugin"]: 0.5}
    plan = bisect.prepare(manager, evidence=found, owners=owners)
    label, p, why = plan.ranking_rows()[0]
    assert label == "'Artian QoL'" and 0.35 <= p <= 0.55 and why[0].startswith("튕긴 곳이 이 모드의 플러그인입니다")
    assert plan.informative and plan.estimate_top is not None and "가장 의심되는 모드가 원인이면 약 " in plan.intro()
    first = plan.first
    assert first.kind in ("off_some", "on_some") and first.focus == [ids["plugin"]]  # it names the suspect

    session, step = bisect.start(manager, plan, found.record)
    result, launches = drive(manager, session, step, lambda on: ids["plugin"] in on)
    assert result.kind == "found" and result.mods == [ids["plugin"]] and launches <= 4 and result.p >= 0.95
    assert any(line.startswith("기록: 튕긴 곳이 이 모드의 플러그인입니다") for line in result.evidence)
    report = "\n".join(session.report_lines())
    assert "사용한 기록: 튕김 기록 " in report and "시작할 때 의심한 정도:" in report
    # the fastest results rest on two pivot pairs (the record's crash and one of the search's): 한 번 더 확인 is
    # offered and adds a third (a crash and a fine run: two launches)
    assert result.pivots == 2 and result.can_confirm and bisect.CONFIRM_NOTE in result.evidence
    nxt = session.more()
    assert isinstance(nxt, Step) and nxt.reason.startswith("confirm") and session.strict
    step = bisect.apply_step(manager, session)
    again, more = drive(manager, session, step, lambda on: ids["plugin"] in on)
    assert again.kind == "found" and again.mods == [ids["plugin"]] and again.pivots >= 3 and more <= 3
    assert not again.can_confirm
    bisect.finish(manager, session)
    assert manager.profile == "기본" and all(m.enabled for m in manager.mods)


def test_a_mod_whose_files_the_game_never_read_stays_suspected(manager, setup, game_dir, tmp_path):
    ids = setup
    loose = [ids[f"a{i}"] for i in range(8)]
    plan = bisect.prepare(manager)
    session, step = bisect.start(manager, plan)
    while len([u for u in step.on if u in loose]) < 2:  # (a launch with two armors at least)
        session.mark_launched()
        step = bisect.answer(manager, session, True)
    manager.deploy()
    on = [u for u in step.on if u in loose]
    quiet, read = on[-1], on[:-1]
    # the launch's records: REFramework's log and the record of the files the game asked for (every armor
    # that was on, but not ``quiet``'s)
    t0 = time.time() - 1200
    write(game_dir / "re2_framework_log.txt", refw_log(game_dir, t0, end=1100))
    names = {u: f"natives/STM/art/armor{loose.index(u)}.tex" for u in loose}
    write(game_dir / accessed.ACCESSED_FILE, access_log(t0, [(60 + 10 * k, names[u]) for k, u in enumerate(read)]
                                                     + [(400, "natives/STM/gui/ui000.tex")]))
    crashes.collect(manager.crash_dir, game_dir, manager.crash_context(), appdata="")
    exit_ = gamewatch.GameExit(1, 0, t0 + 1110, t0)
    detection = gamewatch.detect(manager.crash_dir, t0 - 1, exit_)
    assert detection.session is not None and detection.session.accessed() is not None
    run = accessed.step_run(detection, step.on, manager.crash_index(), session.content)
    assert run["log"] and run["unexposed"] == [quiet] and run["auto"] == "normal" and run["seconds"] == 1110.0

    def p_after(run_):
        copy = bisect.Session(tmp_path / f"copy-{len(run_['unexposed'])}.json", __import__("copy").deepcopy(session.data))
        copy.note_run(run_)
        copy.record(False)
        involvement = copy._engine_now().decide(copy.extra).involvement
        return involvement.get(quiet, 0.0), involvement.get(read[0], 0.0)

    kept, cleared = p_after(run)
    seen, _ = p_after(dict(run, unexposed=[]))
    # a fine launch clears what it ran, but not a mod whose files the game never asked for
    assert kept > 2 * seen and kept > 2 * cleared
    session.note_run(run)
    nxt = bisect.answer(manager, session, False)
    assert session.steps[-1]["unexposed"] == [quiet] and session.steps[-1]["log"] is True
    assert isinstance(nxt, Step)
    bisect.finish(manager, session)


def test_the_game_s_own_crash_report_is_a_crash_and_the_last_seconds_line_up(manager, setup, game_dir):
    ids = setup
    t0 = time.time() - 3600
    # REFramework saw nothing (the game caught a graphics device error itself and ended with exit code 0)
    write(game_dir / "re2_framework_log.txt", refw_log(game_dir, t0, end=280))
    write(game_dir / accessed.ACCESSED_FILE, access_log(t0, [
        (60, "natives/STM/art/armor0.tex"), (100, "natives/STM/art/armor1.tex"),
        (288, "natives/STM/art/armor2.tex"), (291, "natives/STM/art/armor2.tex.241106027"),
        (298, "natives/STM/art/armor3.tex")]))  # (after the crash: not part of what led to it)
    crashed_at = t0 + 295
    report_zip(game_dir / "CrashReport" / datetime.fromtimestamp(crashed_at).strftime("%Y_%m%d_%H%M.zip"),
               game_report_text(game_dir), crashed_at, crashed_at + 40, started=t0 - 1)
    exit_ = gamewatch.GameExit(1, 0, crashed_at + 45, t0 - 1)
    context = manager.crash_context()
    detection = gamewatch.after_exit(manager.crash_dir, exit_, t0 - 1, game_dir, context, keep_good=False, appdata="")
    assert detection.crashed and detection.record is not None and not detection.normal
    assert detection.record.summary["device_error"] == "DEVICE_HUNG"
    assert accessed.crash_time(detection.session) == int(crashed_at)  # the minidump's time
    run = accessed.step_run(detection, [ids["refw"], *(ids[f"a{i}"] for i in range(8))], manager.crash_index(),
                            [ids[f"a{i}"] for i in range(8)])
    assert run["auto"] == "crash" and run["tail"] == [ids["a2"]]  # the 10 s before the report's crash time
    row, = gamewatch.load_launches(manager.crash_dir)
    assert row["outcome"] == "crash" and row["category"] == "gpu" and row["bisect"]
    assert gamewatch.load_last_good(manager.crash_dir) is None

    # a graphics error is a crash like any other: no "not the mods" weight; the mods with render data up (here every
    # armor mod's texture), no box telling the user to check the drivers first
    found = evidence.build(evidence.gather(manager, detection.record.id))
    assert found.device_error == "DEVICE_HUNG" and found.none_scale == 1.0 and found.none_reasons == []
    assert found.weights[ids["a0"]] == pytest.approx(2.5) and found.weights[ids["plugin"]] == pytest.approx(2.0)
    assert found.reasons[ids["a0"]] == ["게임이 그래픽 오류(DEVICE_HUNG)로 멈췄고 이 모드에 텍스처 파일이 있습니다"]
    assert "그래픽 오류 (DEVICE_HUNG)" in found.record_title
    plan = bisect.prepare(manager, evidence=found)
    assert not plan.problem and not hasattr(plan, "device_note")
    # the search starts as for any other crash: the reproduction prior of 언제 튕기나요 (here "after a while", from the
    # record's ~5 minutes), no two launches with every mod off asked for up front (v2.2.5) - only the rule of every
    # search started now (one while "not the mods" keeps some probability) - and no driver note in the result
    params = plan.data["params"]
    assert plan.when == "time" and params["controls"] == 0
    assert params["none_controls"] == bisect.engine_mod.CURRENT["none_controls"]
    assert params["r_prior"] == list(bisect.engine_mod.R_PRIORS[bisect.engine_mod.WHEN_PRIOR[plan.when]])
    session, step = bisect.start(manager, plan, detection.record.id)
    result, launches = drive(manager, session, step, lambda on: ids["a5"] in on)
    assert result.kind == "found" and result.mods == [ids["a5"]]
    controls = [s for s in session.steps if s["reason"] == "control"]
    assert len(controls) <= 1 and all(not s["on"] for s in controls)
    assert not any("그래픽카드" in line or "드라이버" in line for line in session.result_lines(result))
    bisect.finish(manager, session)


def test_the_file_record_setting_comes_back_after_the_search(manager, setup, game_dir, monkeypatch):
    import mhwmm.manager

    config = write(game_dir / "re2_fw_config.txt", "FontSize=16\r\nLooseFileLoader_Enabled=true\r\n")
    offer = accessed.manager_offer(manager)
    assert offer.available and not offer.already and offer.changes == {accessed.LOG_KEY: "true"}
    restore = accessed.enable_logging(manager.set_refw_option, offer)
    session, _step = bisect.start(manager, bisect.prepare(manager), refw_restore=restore)
    assert "LooseFileLoader_LogAccessedFiles=true" in config.read_text()
    session = bisect.load(manager.data_dir)  # (kept with the search: a restart does not lose it)
    assert session.refw_restore == restore

    # the game still runs when the search ends: put back once it is closed
    monkeypatch.setattr(mhwmm.manager, "is_game_running", lambda: True)
    restore = session.refw_restore
    bisect.finish(manager, session)
    assert accessed.finish_restore(manager, restore) == accessed.RESTORE_PENDING
    assert "LooseFileLoader_LogAccessedFiles=true" in config.read_text() and accessed.pending(manager.data_dir)
    monkeypatch.setattr(mhwmm.manager, "is_game_running", lambda: False)
    # a new search that found the setting on keeps it while it runs
    again, _step = bisect.start(manager, bisect.prepare(manager))
    assert accessed.apply_pending(manager, searching=bisect.active(manager.data_dir)) == ""
    assert "LooseFileLoader_LogAccessedFiles=true" in config.read_text()
    bisect.finish(manager, again)
    assert accessed.apply_pending(manager, searching=bisect.active(manager.data_dir)) == accessed.RESTORED
    assert config.read_bytes() == b"FontSize=16\r\nLooseFileLoader_Enabled=true\r\nLooseFileLoader_LogAccessedFiles=false\r\n"
    assert accessed.pending(manager.data_dir) is None


def test_the_launch_history_and_the_last_normal_exit_shorten_the_search(manager, setup, game_dir):
    ids = setup
    now = time.time()
    # earlier ordinary play: long normal runs with everything but the new armor, then the crash with it
    context = manager.crash_context()
    gamewatch.save_last_good(manager.crash_dir, gamewatch.Detection(
        False, exit=gamewatch.GameExit(1, 0, now - 7200, now - 7200 - 3000)), context)
    new = install(manager, game_dir.parent, "New Armor", {"natives/STM/art/new.tex": "n"})
    manager.deploy()
    start = now - 1200
    write(game_dir / "re2_framework_log.txt", refw_log(game_dir, start, game_dir / "MonsterHunterWilds.exe", end=500))
    crashes.collect(manager.crash_dir, game_dir, manager.crash_context(), appdata="")
    owners = bisect.conflict_owners(manager)
    found = evidence.build(evidence.gather(manager, "", owners))
    plan = bisect.prepare(manager, evidence=found, owners=owners)
    # (the plugin, a likelier kind of culprit that also ran in the normal exit, comes close)
    assert [o.source for o in plan.passive][:2] == ["report", "last_good"] and new.id in [u for u, _p in plan.singles[:2]]
    good = plan.passive[1]
    assert new.id not in good.on and good.on == set(plan.units) - {new.id} and good.seconds == 3000.0
    assert plan.crash_times == [] and plan.passive[0].seconds == 500.0  # (the record's own time)
    session, step = bisect.start(manager, plan, found.record)
    result, launches = drive(manager, session, step, lambda on: new.id in on)
    assert result.mods == [new.id] and launches <= 6

    def searched(session) -> int:  # (launches but those with every mod off asked for before naming a mod)
        return sum(1 for s in session.steps if s["reason"] != "control")

    core = searched(session)
    assert core <= 5
    bisect.finish(manager, session)
    # without them the same search takes longer (a search this short may add a launch with every mod off before it
    # names the mod - "not the mods" still has some probability - which a longer one does not need)
    (manager.crash_dir / gamewatch.LAST_GOOD_FILE).unlink()
    session, step = bisect.start(manager, bisect.prepare(manager, owners=owners))
    _result, without = drive(manager, session, step, lambda on: new.id in on)
    assert searched(session) > core and without >= launches
    bisect.finish(manager, session)
    assert ids
