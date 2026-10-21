"""The manager's game-session records: collecting, matching to mods, notices and the support zip
(synthetic logs, dumps, faulty lists and paks written here; no real user data)."""

import hashlib
import os
import struct
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from mhwmm import crashes, crashlog
from mhwmm.crashes import ModIndex, ModRef
from mhwmm.matfix.pak import path_hash, write_pak

from conftest import write

T0 = datetime(2026, 10, 16, 15, 39, 22).timestamp()  # local time, as REFramework writes it


def stamp(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def rec(t: float, msg: str, level: str = "info") -> str:
    return f"[{stamp(t)}] [REFramework] [{level}] {msg}"


def log_text(game: Path, start: float, body: list[tuple[float, str, str]] = (), crash: str | None = None,
             frames: list[str] = (), end: float | None = None) -> str:
    """A REFramework log: the header, ``body`` records (offset, message, level) and, with ``crash`` (the
    module path), the fatal block ``end`` seconds after the start."""
    lines = [rec(start, "REFramework entry"), rec(start, "Commit hash: d052e8fd3fd41d0a1a33394fcf6e6c19be39b1ee"),
             rec(start, "Tag: v1.5.9.1"), rec(start, "Commits past tag: 514"), rec(start, "Build date: 11.10.2026"),
             rec(start, f"Current game path: {game}")]
    lines += [rec(start + dt, msg, level) for dt, msg, level in body]
    if crash is not None:
        t = start + (end or 600)
        lines += [rec(t, "Exception occurred: c0000005", "error"), rec(t, "RIP (exception record): 7ff600001000", "error"),
                  rec(t, "Call stack:", "error"), rec(t, "", "error"),
                  " KiUserExceptionDispatcher", r"  C:\WINDOWS\SYSTEM32\ntdll.dll + 0x165dee", ""]
        for frame in [crash, *frames]:  # code in no module is logged as its address alone
            lines += [" Unknown symbol", f"  {frame} + 0x1234" if frame else "  0x2c3944f30", ""]
        lines += [rec(t, f"Module: 7ff600000000 {crash}" if crash else "Module: Unknown", "error"),
                  rec(t, f"Attempting to write dump to {game / 'reframework_crash.dmp'}", "error")]
    elif end:
        lines.append(rec(start + end, "Hooked DirectX 12"))
    return "\r\n".join(lines) + "\r\n"


def put(path: Path, data, mtime: float) -> Path:
    write(path, data)
    os.utime(path, (mtime, mtime))
    return path


def dump_bytes(t: float) -> bytes:
    return b"MDMP" + struct.pack("<IIIII", 0xA793, 3, 32, 0, int(t)) + bytes(8) + b"\x00" * 64


def snapshot(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): (p.stat().st_mtime_ns, hashlib.sha1(p.read_bytes()).hexdigest())
            for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture
def game(tmp_path) -> Path:
    game = tmp_path / "MonsterHunterWilds"
    write(game / "MonsterHunterWilds.exe", "exe")
    write(game / "dinput8.dll", "refw")
    return game


@pytest.fixture
def crash_dir(tmp_path) -> Path:
    return tmp_path / "data" / "crashes"


def collect(crash_dir, game, context=None):
    """crashes.collect; without ``context`` the sessions do not say which mods were on (they are explained
    with the mods as they are now)."""
    return crashes.collect(crash_dir, game, context or {"profile": "기본"}, appdata="")


def test_collect_copies_each_session_once_and_never_changes_the_game_folder(game, crash_dir, tmp_path):
    plugin = game / "reframework" / "plugins" / "ArtianQoL.dll"
    put(game / "re2_framework_log.txt", log_text(game, T0, crash=str(plugin)), T0 + 600)
    put(game / "re2_framework_log.prev.txt", log_text(game, T0 - 3600, end=1200), T0 - 2400)
    put(game / "reframework_crash.dmp", dump_bytes(T0 + 600), T0 + 601)
    put(game / "reframework_faulty_files.txt", '"error" 2 "Invalid file" "GameDesign/x.user"\r\n', T0 + 300)
    put(game / "re2_fw_config.txt", "ScriptRunner_LogToDisk=false\r\n", T0 - 5000)
    before = snapshot(game)

    changed = collect(crash_dir, game)
    assert [s.id for s in changed] == ["20261016-153922", "20261016-143922"]
    new, old = changed
    assert sorted(new.info["files"]) == ["re2_framework_log.txt", "re2_fw_config.txt", "reframework_crash.dmp",
                                         "reframework_faulty_files.txt"]
    assert sorted(old.info["files"]) == ["re2_framework_log.txt", "re2_fw_config.txt"]
    assert new.problem() == "crash" and old.problem() == ""
    assert new.summary["crash"] == "c0000005" and new.summary["category"] == "plugin"
    assert new.summary["module_name"] == "ArtianQoL.dll" and new.summary["faulty"] == 1
    assert new.title(now=T0) == "15:49 · 오류 코드 0xC0000005 (메모리 접근 오류) · 읽지 못한 파일 1개"
    assert old.title(now=T0 + 86400 * 3) == "10월 16일 14:59 · 오류 기록 없음"
    assert new.crash.module == str(plugin) and new.faulty()[0].name == "x.user"
    assert new.dump_path.read_bytes() == (game / "reframework_crash.dmp").read_bytes()
    assert new.context == {"profile": "기본"} and new.launched is None
    assert old.log().ended == stamp(T0 - 2400)

    assert collect(crash_dir, game) == []  # nothing new
    assert snapshot(game) == before  # only copies: nothing in the game folder changed
    # the running session's log grew (it was collected while the game ran): copied again
    with open(game / "re2_framework_log.txt", "a", encoding="utf-8", newline="") as f:
        f.write(rec(T0 + 700, "Late line") + "\r\n")
    os.utime(game / "re2_framework_log.txt", (T0 + 700, T0 + 700))
    again = collect(crash_dir, game)
    assert [s.id for s in again] == [new.id]
    assert len(crashes.sessions(crash_dir)) == 2


def test_appdata_folder_and_a_dump_whose_log_is_gone(game, crash_dir, tmp_path):
    appdata = tmp_path / "AppData"
    folder = appdata / "REFramework" / "MonsterHunterWilds"
    # the game folder was not writable: REFramework wrote to %APPDATA%
    put(folder / "re2_framework_log.txt", log_text(game, T0, end=60), T0 + 60)
    # an older crash whose log a later start overwrote (builds before 2026-10-11): only its dump is left
    put(game / "reframework_crash.dmp", dump_bytes(T0 - 7200), T0 - 7200)
    changed = crashes.collect(crash_dir, game, {}, appdata=str(appdata))
    assert sorted(s.id for s in changed) == ["20261016-133922-dump", "20261016-153922"]
    dump = crashes.find_session(crash_dir, "20261016-133922")
    assert dump.dump_only and dump.problem() == "dump" and dump.log() is None
    assert dump.title(now=T0) == "13:39 · 크래시 덤프만 남음"
    assert crashes.find_session(crash_dir, "2").id == dump.id and crashes.find_session(crash_dir, "1").id != dump.id
    assert crashes.find_session(crash_dir, "2026") is None  # ambiguous
    assert crashes.last_fatal(crash_dir).id == dump.id
    assert crashes.crashed_since(crash_dir, T0 - 7300).id == dump.id and crashes.crashed_since(crash_dir, T0) is None
    assert collect(crash_dir, game) == []
    assert crashes.evidence_dirs(game, str(appdata)) == [game, folder]
    assert crashes.appdata_dir("") is None


def test_a_dump_is_matched_to_its_crash_even_after_a_time_zone_change(game, crash_dir):
    put(game / "re2_framework_log.txt", log_text(game, T0, crash=str(game / "MonsterHunterWilds.exe")), T0 + 600)
    put(game / "reframework_crash.dmp", dump_bytes(T0 + 600 + 3600), T0 + 4200)  # an hour off: another time zone
    (session,) = collect(crash_dir, game)
    assert session.dump_path is not None and not session.dump_only


def test_a_later_dump_never_replaces_the_dump_of_an_earlier_crash(game, crash_dir):
    put(game / "re2_framework_log.txt", log_text(game, T0, crash=str(game / "MonsterHunterWilds.exe")), T0 + 600)
    put(game / "reframework_crash.dmp", dump_bytes(T0 + 600), T0 + 601)
    (first,) = collect(crash_dir, game)
    own = first.dump_path.read_bytes()
    crashes.mark_notified(crash_dir, first)
    # another crash 45.5 minutes later (close to whole quarter hours after the first): the next start
    # overwrote its log, only its dump is left
    put(game / "re2_framework_log.txt", log_text(game, T0 + 3600, end=60), T0 + 3660)
    put(game / "reframework_crash.dmp", dump_bytes(T0 + 600 + 2730) + b"later", T0 + 3330)
    changed = collect(crash_dir, game)
    assert crashes.find_session(crash_dir, first.id).dump_path.read_bytes() == own
    (alone,) = [s for s in changed if s.dump_only]
    assert alone.id.endswith("-dump") and crashes.pending_notice(crash_dir, now=T0 + 3700).id == alone.id
    # a crash without a dump yet does not take one that is off by more than seconds from whole quarter hours
    put(game / "re2_framework_log.txt", log_text(game, T0 + 7200, crash=str(game / "MonsterHunterWilds.exe")),
        T0 + 7800)
    put(game / "reframework_crash.dmp", dump_bytes(T0 + 7800 + 960), T0 + 8760)
    changed = collect(crash_dir, game)
    assert [s.dump_only for s in sorted(changed, key=lambda s: s.time)] == [False, True]
    # crashing at every start: the next crash's dump comes within minutes of this one (its log is gone)
    put(game / "re2_framework_log.txt", log_text(game, T0 + 9000, crash=str(game / "MonsterHunterWilds.exe"), end=30),
        T0 + 9030)
    put(game / "reframework_crash.dmp", dump_bytes(T0 + 9030), T0 + 9031)
    (looping,) = collect(crash_dir, game)
    put(game / "reframework_crash.dmp", dump_bytes(T0 + 9150) + b"next", T0 + 9151)
    (alone,) = collect(crash_dir, game)
    assert alone.dump_only and looping.dump_path.read_bytes() == dump_bytes(T0 + 9030)


def test_the_launch_from_the_manager_gives_the_session_its_mods(game, crash_dir):
    launch = {"profile": "테스트", "mods": [{"id": "m1", "name": "A", "enabled": True, "stamp": "x|"}]}
    crashes.note_launch(crash_dir, launch, now=T0 - 20)
    put(game / "re2_framework_log.txt", log_text(game, T0, end=30), T0 + 30)
    put(game / "re2_framework_log.prev.txt", log_text(game, T0 - 86400, end=30), T0 - 86400 + 30)
    new, old = collect(crash_dir, game, {"profile": "기본", "mods": []})
    assert new.context == launch and new.launched == T0 - 20 and new.info["context_from"] == "launch"
    assert old.context == {"profile": "기본", "mods": []} and old.launched is None


def test_a_launch_from_the_manager_goes_to_one_session(game, crash_dir):
    crashes.note_launch(crash_dir, {"mods": [{"id": "x", "name": "X", "enabled": True}]}, now=T0 - 20)
    put(game / "re2_framework_log.txt", log_text(game, T0, crash=str(game / "MonsterHunterWilds.exe"), end=180),
        T0 + 180)
    off = {"mods": [{"id": "x", "name": "X", "enabled": False}]}
    (first,) = collect(crash_dir, game, off)
    assert first.launched == T0 - 20 and first.context["mods"][0]["enabled"]
    # X turned off in the manager, then the game started again from Steam six minutes later
    put(game / "re2_framework_log.txt", log_text(game, T0 + 360, end=60), T0 + 420)
    (again,) = collect(crash_dir, game, off)
    assert again.launched is None and again.info["context_from"] == "collect" and again.context == off


def test_a_log_that_becomes_the_next_one_while_it_is_copied(game, crash_dir, monkeypatch):
    log = game / "re2_framework_log.txt"
    put(log, log_text(game, T0, crash=str(game / "MonsterHunterWilds.exe")), T0 + 600)
    real, started = crashes._copy, []

    def game_starts_now(src, dst, limit=None):  # REFramework keeps the old log as .prev.txt and starts a new one
        if src == log and not started:
            started.append(True)
            os.replace(log, game / "re2_framework_log.prev.txt")
            put(log, log_text(game, T0 + 3600, end=10), T0 + 3610)
        return real(src, dst, limit)

    monkeypatch.setattr(crashes, "_copy", game_starts_now)
    assert collect(crash_dir, game) == [] and not any(crash_dir.iterdir())
    monkeypatch.setattr(crashes, "_copy", real)
    found = {s.started: s for s in collect(crash_dir, game)}
    assert set(found) == {stamp(T0), stamp(T0 + 3600)}
    assert found[stamp(T0)].crash is not None and found[stamp(T0 + 3600)].crash is None


def test_only_the_last_sessions_are_kept_and_old_ones_are_not_copied_again(game, crash_dir):
    times = [T0 + i * 1000 for i in range(12)]
    for i, t in enumerate(times):
        crash = str(game / "MonsterHunterWilds.exe") if i < 2 else None
        put(game / "re2_framework_log.txt", log_text(game, t, crash=crash, end=60), t + 60)
        collect(crash_dir, game)
    kept = crashes.sessions(crash_dir)
    assert len(kept) == crashes.KEEP_SESSIONS
    # the two crashes are kept; the oldest sessions without a problem went first
    assert {s.started for s in kept} == {stamp(t) for t in times[:2] + times[4:]}
    # one that was let go is not copied again while it is still in the game folder
    put(game / "re2_framework_log.prev.txt", log_text(game, times[2], end=60), times[2] + 60)
    assert collect(crash_dir, game) == []
    # folders a crash of the manager left half made go after a day
    stray = crash_dir / "stray"
    stray.mkdir()
    os.utime(stray, (T0 - 3 * 86400, T0 - 3 * 86400))
    crashes.prune(crash_dir)
    assert not stray.exists() and len(crashes.sessions(crash_dir)) == crashes.KEEP_SESSIONS


def test_a_crash_outlives_sessions_that_only_list_unreadable_files(game, crash_dir):
    put(game / "re2_framework_log.txt", log_text(game, T0, crash=str(game / "MonsterHunterWilds.exe")), T0 + 600)
    put(game / "reframework_crash.dmp", dump_bytes(T0 + 600), T0 + 601)
    collect(crash_dir, game)
    for i in range(1, 13):  # REFramework lists some of the game's own files in every session (#1677)
        t = T0 + i * 3600
        put(game / "re2_framework_log.txt", log_text(game, t, end=60), t + 60)
        put(game / "reframework_faulty_files.txt", '"error" 2 "Invalid file" "GameDesign/Vanilla.user"\r\n', t + 30)
        collect(crash_dir, game)
    assert len(crashes.sessions(crash_dir)) == crashes.KEEP_SESSIONS
    assert crashes.last_fatal(crash_dir).started == stamp(T0) and crashes.last_fatal(crash_dir).dump_path is not None


def test_the_startup_notice_comes_once(game, crash_dir):
    put(game / "re2_framework_log.txt", log_text(game, T0, crash=str(game / "MonsterHunterWilds.exe")), T0 + 600)
    put(game / "reframework_faulty_files.txt", '"error" 2 "Invalid file" "GameDesign/x.user"\r\n', T0 + 30)
    collect(crash_dir, game)
    now = T0 + 3600
    first = crashes.pending_notice(crash_dir, now=now)
    assert first is not None and crashes.notice_text(first) == crashes.NOTICE_CRASH
    crashes.mark_notified(crash_dir, first)
    assert crashes.pending_notice(crash_dir, now=now) is None
    # the next session has the same unreadable file and no crash: nothing new to say
    later = T0 + 7200
    put(game / "re2_framework_log.txt", log_text(game, later, end=60), later + 60)
    put(game / "reframework_faulty_files.txt", '"error" 2 "Invalid file" "GameDesign/x.user"\r\n', later + 30)
    collect(crash_dir, game)
    assert crashes.pending_notice(crash_dir, now=later + 100) is None
    # ... but a new one is
    put(game / "reframework_faulty_files.txt", '"error" 2 "Invalid file" "GameDesign/x.user"\r\n'
                                              '"error" 1 "Missing file" "Art/y.tex"\r\n', later + 40)
    collect(crash_dir, game)
    second = crashes.pending_notice(crash_dir, now=later + 100)
    assert second is not None and crashes.notice_text(second) == crashes.NOTICE_FAULTY
    # old records are listed, not announced
    crashes.mark_notified(crash_dir, second)
    put(game / "re2_framework_log.txt", log_text(game, later + 100, crash=str(game / "MonsterHunterWilds.exe")),
        later + 700)
    collect(crash_dir, game)
    assert crashes.pending_notice(crash_dir, now=later + 10 * 86400) is None


def test_an_older_crash_is_not_told_about_after_a_newer_one(game, crash_dir):
    exe = str(game / "MonsterHunterWilds.exe")
    put(game / "re2_framework_log.prev.txt", log_text(game, T0 - 7200, crash=exe), T0 - 6600)
    put(game / "re2_framework_log.txt", log_text(game, T0, crash=exe), T0 + 600)
    collect(crash_dir, game)  # two crashes in a row
    newer = crashes.pending_notice(crash_dir, now=T0 + 3600)
    assert newer.started == stamp(T0)
    crashes.mark_notified(crash_dir, newer)
    assert crashes.pending_notice(crash_dir, now=T0 + 3600) is None  # nor later in the same run
    put(game / "re2_framework_log.txt", log_text(game, T0 + 7200, end=60), T0 + 7260)  # a game without a problem
    collect(crash_dir, game)
    assert crashes.pending_notice(crash_dir, now=T0 + 7300) is None
    put(game / "re2_framework_log.txt", log_text(game, T0 + 9000, crash=exe), T0 + 9600)
    collect(crash_dir, game)
    assert crashes.pending_notice(crash_dir, now=T0 + 9700).started == stamp(T0 + 9000)


def test_the_running_game_s_session_is_left_for_later(game, crash_dir):
    exe = str(game / "MonsterHunterWilds.exe")
    put(game / "re2_framework_log.prev.txt", log_text(game, T0 - 3600, crash=exe), T0 - 3000)  # crashed...
    put(game / "re2_framework_log.txt", log_text(game, T0, end=60), T0 + 60)  # ... and runs again now
    put(game / "reframework_faulty_files.txt", '"error" 2 "Invalid file" "GameDesign/x.user"\r\n', T0 + 30)
    collect(crash_dir, game)
    live = crashes.live_started(game, appdata="")
    assert live == stamp(T0) and crashes.pending_notice(crash_dir, now=T0 + 100).started == live
    assert crashes.pending_notice(crash_dir, now=T0 + 100, skip_started=live).started == stamp(T0 - 3600)
    running = crashes.find_session(crash_dir, "1")
    e = crashes.explain(running, ModIndex(game), now=T0 + 100, running=True)
    assert e.findings[0].kind == "running" and "게임 실행 중" in e.summary and "강제로" not in e.findings[0].text
    assert "게임 실행 중" in running.title(running=True) and "오류 기록 없음" in running.title()
    assert crashes.live_started(game.parent / "nowhere", appdata="") == ""


def test_unreadable_files_are_told_about_only_when_a_mod_has_them(game, crash_dir):
    put(game / "re2_framework_log.txt", log_text(game, T0, end=60), T0 + 60)
    vanilla = '"error" 2 "Invalid file" "GameDesign/Vanilla.user"\r\n'
    put(game / "reframework_faulty_files.txt", vanilla, T0 + 30)
    collect(crash_dir, game)
    index = ModIndex(game, deployed(("natives/STM/GameDesign/Armor.user.3", "arm")), [ModRef("arm", "방어구")])
    assert crashes.pending_notice(crash_dir, now=T0 + 100, index=index) is None  # the game's own file
    assert crashes.pending_notice(crash_dir, now=T0 + 100) is not None  # (without the mods: every new entry)
    put(game / "reframework_faulty_files.txt", vanilla + '"error" 2 "Invalid file" "GameDesign/Armor.user"\r\n', T0 + 40)
    collect(crash_dir, game)
    told = crashes.pending_notice(crash_dir, now=T0 + 100, index=index)
    assert told is not None and crashes.notice_text(told) == crashes.NOTICE_FAULTY


# ---- explaining -------------------------------------------------------------------------------------
def deployed(*entries: tuple[str, str], pak: bool = False) -> dict:
    return {p.replace("\\", "/").casefold(): {"path": p, "mod": m, "pak": pak} for p, m in entries}


def session_for(crash_dir, game, text: str, faulty: str | None = None):
    put(game / "re2_framework_log.txt", text, T0 + 600)
    if faulty is not None:
        put(game / "reframework_faulty_files.txt", faulty, T0 + 30)
    (s, *_rest) = collect(crash_dir, game)
    return s


class Build:
    commit, label = "d052e8fd", "nightly 01432"


def test_a_plugin_crash_names_its_mod(game, crash_dir):
    plugin = game / "reframework" / "plugins" / "ArtianQoL.dll"
    other = game / "reframework" / "plugins" / "Helper.dll"
    body = [(5, "[ScriptRunner] Initializing Lua state for the first time...", "info"),
            (6, f"[ScriptState] Running script {game / 'reframework' / 'autorun' / 'ok.lua'}...", "info"),
            (7, "[ScriptRunner] Lua state initialized.", "info")]
    s = session_for(crash_dir, game, log_text(game, T0, body, crash=str(plugin), frames=[str(other)]))
    index = ModIndex(game, deployed(("reframework/plugins/ArtianQoL.dll", "m1"), ("reframework/plugins/Helper.dll", "m3")),
                     [ModRef("m1", "ArtianQoL"), ModRef("m3", "도우미")])
    e = crashes.explain(s, index, builds=[Build()], now=T0)
    assert e.summary == "15:49 종료 · 오류 코드 0xC0000005 (메모리 접근 오류) · REFramework nightly 01432"
    assert e.crashed and e.code == "c0000005" and e.category == "plugin" and e.has_log
    top = e.findings[0]
    assert (top.kind, top.level, top.mods) == ("plugin", "high", ["m1"])
    assert top.text == "플러그인 'ArtianQoL.dll'에서 오류가 났습니다 → 의심되는 모드 'ArtianQoL'"
    assert top.evidence[0] == "Exception occurred: c0000005" and top.evidence[1].startswith("Module: 7ff600000000 ")
    stack = next(f for f in e.findings if f.kind == "stack")
    assert stack.mods == ["m3"] and stack.level == "low"
    assert e.suspects == ["m1", "m3"]
    lines = e.lines()
    assert lines[0] == e.summary and "• " + top.text in lines and any(x.startswith("    근거: ") for x in lines)
    # without the manager knowing it
    unmanaged = crashes.explain(s, ModIndex(game))
    assert "매니저가 설치하지 않은 플러그인" in unmanaged.findings[0].text and unmanaged.suspects == []


@pytest.mark.parametrize("module, kind, level, action, words", [
    ("dxgi.dll", "root_dll", "high", "", "직접 넣은 'dxgi.dll'"),
    # not on the list of the game's own files (one install): perhaps a file a game update added
    ("_storage_/something_new.dll", "root_dll", "medium", "", "게임 업데이트로 생긴 파일일 수도"),
    ("_storage_/libxess_fg.dll", "gpu", "info", "bisect", "그래픽(D3D12) 쪽에서"),
    ("_storage_/dinput8.dll", "reframework", "medium", "update_refw", "REFramework(dinput8.dll)"),
    ("MonsterHunterWilds.exe", "game", "info", "bisect", "게임 본체에서"),
    ("D3D12/D3D12Core.dll", "gpu", "info", "bisect", "그래픽(D3D12) 쪽에서"),
    (r"C:\WINDOWS\SYSTEM32\ntdll.dll", "system", "info", "bisect", "Windows 시스템 파일 'ntdll.dll'"),
    (r"C:\Program Files\Avast\aswhook.dll", "other", "medium", "", "(Avast·AVG·Norton 백신)"),
    ("", "unknown", "low", "bisect", "어느 파일에도 속하지 않는"),
])
def test_where_it_crashed(game, crash_dir, module, kind, level, action, words):
    path = module if ":" in module or not module else str(game / module)
    s = session_for(crash_dir, game, log_text(game, T0, crash=path))
    e = crashes.explain(s, ModIndex(game))
    assert (e.findings[0].kind, e.findings[0].level, e.findings[0].action) == (kind, level, action)
    assert words in e.findings[0].text and e.findings[0].mods == []


def test_a_dll_a_mod_put_into_the_game_folder(game, crash_dir):
    s = session_for(crash_dir, game, log_text(game, T0, crash=str(game / "_storage_" / "dxgi.dll")))
    e = crashes.explain(s, ModIndex(game, deployed(("dxgi.dll", "rs")), [ModRef("rs", "ReShade 프리셋")]))
    assert e.findings[0].text == "게임 폴더의 'dxgi.dll'에서 오류가 났습니다 → 의심되는 모드 'ReShade 프리셋'"
    assert e.suspects == ["rs"]


def test_game_code_shows_the_game_method_only_as_a_hint(game, crash_dir):
    text = log_text(game, T0, crash=str(game / "MonsterHunterWilds.exe"))
    text = text.replace(" Unknown symbol\r\n  " + str(game / "MonsterHunterWilds.exe"),
                        " Ordinal398 [TDB: app.cEnemy.update]\r\n  " + str(game / "MonsterHunterWilds.exe"), 1)
    e = crashes.explain(session_for(crash_dir, game, text), ModIndex(game))
    assert e.findings[0].evidence[-1] == "게임 함수(참고용, 정확하지 않을 수 있음): app.cEnemy.update"


def test_scripts_plugins_and_paks_in_the_log(game, crash_dir):
    autorun = game / "reframework" / "autorun"
    body = [(5, "[ScriptRunner] Initializing Lua state for the first time...", "info"),
            (6, f"[ScriptState] Running script {autorun / 'Foo.lua'}...", "info"),
            (7, r"sol: runtime error: ...ers\x\MonsterHunterWilds\reframework\autorun\lib\util.lua:3: boom", "error"),
            (8, r"sol: runtime error: ...ers\x\MonsterHunterWilds\reframework\autorun\lib\util.lua:4: boom", "error"),
            (9, "[PluginLoader] Plugin Old requires a different major version", "error"),
            (10, f"[IntegrityCheckBypass]: pak_load_check_function_hook returned false for string: "
                 f"{game / 'pak_mods' / '002_Armor.pak'}", "warning"),
            (11, "[IntegrityCheckBypass]: 3 custom pak(s) were cached but NONE were injected - no archive", "error"),
            (12, "[IntegrityCheckBypass]: Pak directory loading is disabled, skipping it.", "info"),
            (13, "[LooseFileLoader] Failed to find path_to_hash candidate, cannot continue", "error"),
            (14, "Timed out waiting for VM to initialize.", "error"),
            (15, "[HookManager] Cannot add nullptr function", "error"),
            (16, "Present failed: -7785fffb", "error")]
    s = session_for(crash_dir, game, log_text(game, T0, body, end=20))
    index = ModIndex(game, {**deployed(("reframework/autorun/Foo.lua", "foo"), ("reframework/autorun/lib/util.lua", "lib"),
                                       ("reframework/plugins/Old.dll", "old"),
                                       ("natives/STM/x.tex.241106027", "tex")),
                            **deployed(("pak_mods/002_Armor.pak", "armor"), pak=True)},
                     [ModRef("foo", "Foo"), ModRef("lib", "LuaLib"), ModRef("old", "OldPlugin"), ModRef("armor", "갑옷")])
    e = crashes.explain(s, index)
    by_kind = {f.kind: f for f in e.findings}
    assert e.findings[0].kind == "no_crash" and not e.crashed
    assert by_kind["script"].text == "스크립트 'Foo.lua'를 읽는 중에 멈췄습니다 → 의심되는 모드 'Foo'"
    assert by_kind["script"].level == "high" and by_kind["script"].mods == ["foo"]
    assert by_kind["lua"].text == "스크립트 'util.lua'에서 오류가 2번 났습니다 → 의심되는 모드 'LuaLib'"
    assert by_kind["plugin_load"].mods == ["old"] and by_kind["plugin_load"].action == "update_refw"
    assert by_kind["pak_failed"].mods == ["armor"] and "002_Armor.pak" in by_kind["pak_failed"].text
    assert {"pak_none", "pak_disabled", "loose_failed", "init", "hooks", "present"} <= set(by_kind)
    assert "DEVICE_REMOVED" in by_kind["present"].text
    assert e.suspects[:2] == ["foo", "armor"]  # the strongest first
    # nothing about pak_mods or loose files when no mod uses them
    plain = {f.kind for f in crashes.explain(s, ModIndex(game)).findings}
    assert "pak_disabled" not in plain and "loose_failed" not in plain


def test_files_the_game_could_not_load_are_matched_to_mods(game, crash_dir):
    user = "GameDesign/Equip/_Prefab/Armor/Male/003/001/003_001_avp.user"
    tex = "Art/Model/Character/ch03/001/001/2/ch03_001_0012_ALBD.tex"
    # a .pak mod in pak_mods and one REFramework reads although no mod put it there
    write_pak([(path_hash(f"natives/STM/{tex}.241106027"), b"tex", 3, 0)], write(game / "pak_mods" / "001_Pak.pak", b""))
    write_pak([(path_hash("natives/STM/GameDesign/Other.user.3"), b"u", 1, 0)],
              write(game / "pak_mods" / "extra" / "Mine.pak", b""))
    write(game / "pak_mods" / "Ignored.PAK", b"not read: REFramework wants .pak")
    # a loose file in the game folder no mod placed
    write(game / "natives" / "STM" / "GameDesign" / "Loose" / "Left.user.3", b"x")
    faulty = "\r\n".join(f'"error" 2 "Invalid file" "{p}"' for p in (
        user, tex, "GameDesign/Other.user", "GameDesign/loose/left.user", "GameDesign/Vanilla.user",
        "Art/Enabled/Only.mdf2")) + "\r\n"
    s = session_for(crash_dir, game, log_text(game, T0, end=30), faulty)
    index = ModIndex(game, {**deployed((f"natives/STM/{user}.3", "armor")), **deployed(("pak_mods/001_Pak.pak", "pak"),
                                                                                        pak=True)},
                     [ModRef("armor", "방어구 A"), ModRef("pak", "텍스처 팩"),
                      ModRef("new", "새 모드", files=["natives/STM/Art/Enabled/Only.mdf2.45"]),
                      ModRef("off", "꺼진 모드", enabled=False, files=["natives/STM/GameDesign/Vanilla.user.3"])])
    e = crashes.explain(s, index)
    by_mod = {f.mods[0]: f for f in e.findings if f.kind == "faulty"}
    assert by_mod["armor"].text == "REFramework가 읽지 못한 파일: '방어구 A'의 003_001_avp.user → 재질 점검·복구를 해 보세요"
    assert by_mod["armor"].action == "matfix" and by_mod["armor"].level == "high"
    assert by_mod["pak"].text.endswith("ch03_001_0012_ALBD.tex → 모드를 최신 버전으로 업데이트하거나 꺼 보세요")
    assert by_mod["new"].action == "matfix"  # enabled, not applied yet
    kinds = {f.kind: f for f in e.findings}
    assert "'pak_mods/extra/Mine.pak'" in kinds["faulty_pak"].text and kinds["faulty_pak"].mods == []
    assert "natives/STM/GameDesign/Loose/Left.user.3" in kinds["faulty_loose"].text
    assert kinds["faulty_game"].text.startswith("REFramework가 읽지 못한 게임 파일 1개(Vanilla.user 등)")
    assert kinds["faulty_game"].level == "low"
    assert set(e.suspects) == {"armor", "pak", "new"}
    many = ModIndex(game, deployed(*((f"natives/STM/GameDesign/F{i}.user.3", "armor") for i in range(3))),
                    [ModRef("armor", "방어구 A")])
    match = many.match_faulty(crashlog.parse_faulty("\n".join(f'"error" 2 "Invalid file" "GameDesign/f{i}.user"'
                                                              for i in range(3))))
    assert len(match.by_mod["armor"]) == 3
    text = crashes._faulty_findings(match, many)[0].text
    assert text == "REFramework가 읽지 못한 파일: '방어구 A'의 f0.user 외 2개 → 재질 점검·복구를 해 보세요"
    assert index.pak_candidates("natives/STM/a/b.user.2") == ["natives/STM/a/b.user.2"]
    assert "natives/STM/streaming/a/b.tex.241106027" in index.pak_candidates("a/b.tex")


def test_broken_paks_and_long_lists_do_not_stop_the_matching(game, monkeypatch):
    # a pak cut right after its header (a chunked one: the parser fails on its chunk table)
    write(game / "pak_mods" / "broken.pak", struct.pack("<IBBhii", 0x414B504B, 4, 0, 40, 0, 0))
    index = ModIndex(game)
    entries = crashlog.parse_faulty("".join(f'"error" 2 "Invalid file" "GameDesign/V{i}.user"\n' for i in range(3)))
    monkeypatch.setattr(crashes, "MAX_FAULTY_MATCH", 2)
    match = index.match_faulty(entries)
    assert len(match.unknown) == 2 and len(match.unchecked) == 1 and match.unreadable == ["pak_mods/broken.pak"]
    kinds = {f.kind: f for f in crashes._faulty_findings(match, index)}
    assert kinds["faulty_game"].text.startswith("REFramework가 읽지 못한 게임 파일 2개")
    assert "1개는 PAK과 게임 폴더에서 찾아보지 않았습니다" in kinds["faulty_more"].text
    assert "'pak_mods/broken.pak'을 읽지 못해" in kinds["faulty_unread"].text


def test_materials_add_reasons_and_no_record_says_so(game, crash_dir):
    mods = [ModRef("a", "A", material_state="stale"), ModRef("b", "B", stuck=["x.user", "y.pfb", "z.scn"]),
            ModRef("c", "C", enabled=False, material_state="stale")]
    e = crashes.explain(None, ModIndex(game, {}, mods))
    assert e.summary == "" and e.findings[0].text == crashes.NO_RECORD and not e.has_log and not e.crashed
    assert [f.kind for f in e.findings[1:]] == ["material", "stuck"]
    assert e.findings[2].text.startswith("'B'의 파일 3개(x.user, y.pfb 외 1개)는")
    assert e.suspects == ["a", "b"]
    put(game / "reframework_crash.dmp", dump_bytes(T0), T0)
    (dump,) = collect(crash_dir, game)
    e = crashes.explain(dump, ModIndex(game), now=T0)
    assert e.findings[0].kind == "dump_only" and e.crashed and e.summary == "15:39 종료 · 크래시 덤프만 남음"


def test_a_record_is_explained_with_the_mods_of_its_session(game, crash_dir):
    """Mods installed, turned off, removed or renumbered after the game ran do not change what its record says."""
    user = "GameDesign/Equip/_Prefab/Armor/Male/003/001/003_001_avp.user"
    plugin = game / "reframework" / "plugins" / "ArtianQoL.dll"
    crashes.note_launch(crash_dir, {"mods": [{"id": "a", "name": "ArtianQoL", "enabled": True},
                                             {"id": "p", "name": "갑옷 PAK", "enabled": True}],
                                    "owners": {"reframework/plugins/ArtianQoL.dll": "a", "pak_mods/003_Armor.pak": "p"}},
                        now=T0 - 10)
    body = [(10, "[IntegrityCheckBypass]: pak_load_check_function_hook returned false for string: "
                 f"{game / 'pak_mods' / '003_Armor.pak'}", "warning")]
    s = session_for(crash_dir, game, log_text(game, T0, body, crash=str(plugin)), f'"error" 2 "Invalid file" "{user}"\r\n')
    # since then: ArtianQoL removed, the PAK renumbered (a mod before it went), ModB installed, which has the file
    now = ModIndex(game, {**deployed((f"natives/STM/{user}.3", "b")), **deployed(("pak_mods/002_Armor.pak", "p"), pak=True)},
                   [ModRef("p", "갑옷 PAK"), ModRef("b", "ModB", files=[f"natives/STM/{user}.3"])])
    e = crashes.explain(s, now)
    assert e.findings[0].text == "플러그인 'ArtianQoL.dll'에서 오류가 났습니다 → 의심되는 모드 'ArtianQoL'"
    by_kind = {f.kind: f for f in e.findings}
    assert by_kind["pak_failed"].mods == ["p"] and "faulty" not in by_kind and "faulty_game" in by_kind
    assert e.suspects == ["a", "p"]
    assert "b" in now.match_faulty(s.faulty()).by_mod  # (the mods as they are now would blame ModB)
    # an older record (no files kept with it), its mods turned off since: still named, and said to be off now
    plain = {"mods": [{"id": "a", "name": "ArtianQoL", "enabled": True}, {"id": "arm", "name": "방어구", "enabled": True}]}
    crashes.note_launch(crash_dir, plain, now=T0 + 3590)
    put(game / "re2_framework_log.txt", log_text(game, T0 + 3600, crash=str(plugin)), T0 + 4200)
    put(game / "reframework_faulty_files.txt", f'"error" 2 "Invalid file" "{user}"\r\n', T0 + 3630)
    (later,) = collect(crash_dir, game)
    off = ModIndex(game, {}, [ModRef("a", "ArtianQoL", enabled=False, files=["reframework/plugins/ArtianQoL.dll"]),
                              ModRef("arm", "방어구", enabled=False, files=[f"natives/STM/{user}.3"])])
    e = crashes.explain(later, off)
    assert e.findings[0].text.endswith("→ 의심되는 모드 'ArtianQoL' (지금은 꺼져 있음)")
    faulty = next(f for f in e.findings if f.kind == "faulty")
    assert faulty.mods == ["arm"] and "'방어구' (지금은 꺼져 있음)의 003_001_avp.user" in faulty.text
    assert e.suspects == ["a", "arm"]


def faulty_lines(*paths) -> str:
    return "".join(f'"error" 2 "Invalid file" "{p}"\r\n' for p in paths)


def test_the_copy_the_game_read_is_the_one_blamed(game, crash_dir):
    """Of the mods that had a file, the one whose copy was in the game folder in the session: the last
    one in load order (not the one placed since), and never a mod that was on but not applied."""
    user = "GameDesign/Equip/_Prefab/Armor/Male/003/001/003_001_avp.user"
    rel = f"natives/STM/{user}.3"
    crashes.note_launch(crash_dir, {"mods": [{"id": "a", "name": "Armor A", "enabled": True},
                                             {"id": "b", "name": "Armor B", "enabled": True},
                                             {"id": "c", "name": "Armor C", "enabled": True}],
                                    "placed": ["a", "b"], "dirty": True}, now=T0 - 10)
    put(game / "re2_fw_config.txt", "LooseFileLoader_Enabled=true\r\n", T0 - 100)
    s = session_for(crash_dir, game, log_text(game, T0, end=30), faulty_lines(user))
    # since then B was turned off and applied: A's copy is in the game folder now; C (on, not applied then) too
    mods = [ModRef("a", "Armor A", files=[rel]), ModRef("b", "Armor B", enabled=False, files=[rel]),
            ModRef("c", "Armor C", files=[rel])]
    index = ModIndex(game, deployed((rel, "a")), mods)
    e = crashes.explain(s, index)
    (found,) = [f for f in e.findings if f.kind == "faulty"]
    assert found.mods == ["b"] and "'Armor B' (지금은 꺼져 있음)의 003_001_avp.user" in found.text
    assert list(index.for_session(s).match_faulty(s.faulty()).by_mod) == ["b"]
    # C came last but had no file in the game folder then (turned on without applying); with B removed since,
    # A is the last one known, but whether B had that file is not (later_gone: precheck words it as unsure)
    later = ModIndex(game, deployed((rel, "c")), [ModRef("a", "Armor A", files=[rel]), ModRef("c", "Armor C", files=[rel])])
    assert list(later.for_session(s).match_faulty(s.faulty()).by_mod) == ["a"]
    assert later.for_session(s).later_gone("a") and not index.for_session(s).later_gone("a")
    # as things are now (no session): the copy placed now
    assert list(index.match_faulty(s.faulty()).by_mod) == ["a"]


def test_the_pak_the_game_read_wins(game):
    user = "GameDesign/Equip/_Prefab/Armor/Male/003/001/003_001_avp.user"
    entry = [(path_hash(f"natives/STM/{user}.3"), b"u", 1, 0)]
    for rel in ("pak_mods/001_Old Armor.pak", "pak_mods/002_New Armor.pak"):
        write_pak(entry, write(game / rel, b""))
    paks = {**deployed(("pak_mods/001_Old Armor.pak", "old"), pak=True),
            **deployed(("pak_mods/002_New Armor.pak", "new"), pak=True)}
    index = ModIndex(game, paks, [ModRef("old", "Old Armor"), ModRef("new", "New Armor")])
    faulty = crashlog.parse_faulty(faulty_lines(user))
    assert list(index.match_faulty(faulty).by_mod) == ["new"]  # (REFramework numbers them in folder order)
    # a pak named as a patch of the game takes that number; a subfolder's paks come where the subfolder is
    write_pak(entry, write(game / "pak_mods" / "re_chunk_000.pak.sub_000.pak.patch_090.pak", b""))
    assert list(ModIndex(game, paks, []).match_faulty(faulty).paks) == [
        "pak_mods/re_chunk_000.pak.sub_000.pak.patch_090.pak"]
    (game / "pak_mods" / "re_chunk_000.pak.sub_000.pak.patch_090.pak").unlink()
    write_pak(entry, write(game / "pak_mods" / "zz" / "x.pak", b""))
    write_pak(entry, write(game / "pak_mods" / "zz x.pak", b""))
    assert list(ModIndex(game, paks, []).match_faulty(faulty).paks) == ["pak_mods/zz x.pak"]


def test_a_loose_file_is_read_before_any_pak(game):
    user = "GameDesign/Equip/_Prefab/Armor/Male/003/001/003_001_avp.user"
    write_pak([(path_hash(f"natives/STM/{user}.3"), b"u", 1, 0)], write(game / "pak_mods" / "001_Armor.pak", b""))
    write(game / f"natives/STM/{user}.3", b"by hand")
    index = ModIndex(game, deployed(("pak_mods/001_Armor.pak", "p"), pak=True), [ModRef("p", "갑옷 팩")])
    faulty = crashlog.parse_faulty(faulty_lines(user))
    match = index.match_faulty(faulty)
    assert match.by_mod == {} and [rel for _f, rel in match.loose] == [f"natives/STM/{user}.3"]
    # loose file loading off: that file was not read, the pak was
    match = index.match_faulty(faulty, loose=False)
    assert list(match.by_mod) == ["p"] and match.loose == []
    # nor a mod's loose file
    loose = ModIndex(game, deployed((f"natives/STM/{user}.3", "a")), [ModRef("a", "Armor A")])
    assert list(loose.match_faulty(faulty).by_mod) == ["a"] and loose.match_faulty(faulty, loose=False).by_mod == {}


def test_the_settings_of_the_session_say_whether_loose_files_were_read(game, crash_dir, tmp_path):
    appdata = tmp_path / "AppData"
    folder = appdata / "REFramework" / "MonsterHunterWilds"
    put(game / "re2_fw_config.txt", "LooseFileLoader_Enabled=true\r\n", T0 - 100)
    put(folder / "re2_fw_config.txt", "LooseFileLoader_Enabled=false\r\n", T0 - 100)
    # REFramework could not write to the game folder: its log and its settings are in %APPDATA%
    put(folder / "re2_framework_log.txt", log_text(game, T0, end=30), T0 + 600)
    (s,) = crashes.collect(crash_dir, game, {}, appdata=str(appdata))
    assert s.info["source"] == str(folder) and crashes.session_loose(s) is False
    assert s.info["files"]["re2_fw_config.txt"]["from"] == str(folder / "re2_fw_config.txt")
    assert crashes.session_loose(None) is None


def test_session_mods_name_removed_mods(game, crash_dir):
    crashes.note_launch(crash_dir, {"mods": [{"id": "gone", "name": "지운 모드", "enabled": True}]}, now=T0)
    plugin = game / "reframework" / "plugins" / "Gone.dll"
    s = session_for(crash_dir, game, log_text(game, T0, crash=str(plugin)))
    e = crashes.explain(s, ModIndex(game, deployed(("reframework/plugins/Gone.dll", "gone"))))
    assert e.findings[0].text.endswith("→ 의심되는 모드 '지운 모드'")


def test_pasted_error_text(game, crash_dir):
    body = [(5, f"[IntegrityCheckBypass]: injecting {game / 'pak_mods' / '001_Armor.pak'} as native-form "
                "'re_chunk_000.pak.sub_000.pak.patch_016.pak' (a6=16, slot=31, auto-assigned)", "info")]
    s = session_for(crash_dir, game, log_text(game, T0, body, end=30))
    index = ModIndex(game, {**deployed(("pak_mods/001_Armor.pak", "armor"), pak=True),
                            **deployed(("reframework/plugins/Artian.dll", "artian"))},
                     [ModRef("armor", "갑옷"), ModRef("artian", "Artian")])
    found = crashes.explain_text("RE ENGINE - Aborted\nfile looks corrupted: re_chunk_000.pak.sub_000.pak.patch_016.pak",
                                 index, s)
    assert [(f.kind, f.mods, f.level) for f in found] == [("corrupted_pak", ["armor"], "high")]
    assert found[0].text == "게임이 PAK '001_Armor.pak'을 손상된 파일로 보았습니다 → 의심되는 모드 '갑옷'"
    unknown = crashes.explain_text("file looks corrupted: re_chunk_000.pak.patch_009.pak", index)
    assert unknown[0].action == "update_refw" and unknown[0].mods == []
    oom = crashes.explain_text("Fatal D3D error (6, E_OUTOFMEMORY, 0x8007000e)", index)
    assert oom[0].kind == "d3d" and "VRAM" in oom[0].text
    event = crashes.explain_text(f"Faulting module path: {game / 'reframework' / 'plugins' / 'Artian.dll'}\n"
                                 "Exception code: 0xc0000005", index)
    assert event[0].kind == "plugin" and event[0].mods == ["artian"]
    assert "예외 코드: 0xC0000005 (메모리 접근 오류)" in event[0].evidence
    assert crashes.explain_text("Exception code: 0xc00000fd", index)[0].kind == "code"
    assert crashes.explain_text("hello", index) == []
    assert crashes.explain_text("RE ENGINE - Aborted", index)[0].action == "bisect"


def test_modules_an_error_window_names_without_a_folder(game):
    index = ModIndex(game, deployed(("reframework/plugins/Artian.dll", "artian")), [ModRef("artian", "Artian")])

    def lead(text):
        return crashes.explain_text(text, index)[0]

    plugin = lead("Faulting module name: Artian.dll, version: 0.0.0.0")
    assert (plugin.kind, plugin.mods, plugin.level) == ("plugin", ["artian"], "high")
    assert lead("Faulting module name: dinput8.dll").kind == "reframework"
    assert lead("Faulting module name: KERNELBASE.dll, version: 10.0.26100.1").kind == "system"
    assert lead("Faulting module name: MonsterHunterWilds.exe").kind == "game"
    named = lead("Faulting module name: Mystery.dll")
    assert named.kind == "named" and "다른 프로그램" not in named.text
    # Reliability Monitor: no folder, the code without 0x
    monitor = lead("Fault Module Name:\tKERNELBASE.dll\r\nException Code:\te06d7363")
    assert monitor.kind == "system" and "예외 코드: 0xE06D7363 (C++ 예외)" in monitor.evidence
    assert lead("오류 모듈 이름:\tntdll.dll\r\n예외 코드:\tc0000374").kind == "system"


def test_script_paths_match_only_whole_file_names(game):
    index = ModIndex(game, deployed(("reframework/autorun/barfoo.lua", "bar"), ("reframework/autorun/foo.lua", "foo")))
    assert index.owners_by_tail("foo.lua") == ["foo"] and index.owners_by_tail("oo.lua") == []
    assert index.owners_by_tail("...work/autorun/foo.lua") == ["foo"]  # Lua cut it inside a folder name
    assert index.owners_by_tail("...ork/autorun/barfoo.lua") == ["bar"]
    assert index.owners_by_tail(r"G:\Games\MonsterHunterWilds\reframework\autorun\foo.lua") == ["foo"]
    only_bar = ModIndex(game, deployed(("reframework/autorun/barfoo.lua", "bar")), [ModRef("bar", "BarFoo")])
    found = crashes.explain_text("foo.lua:12: attempt to index a nil value", only_bar)
    assert [(f.kind, f.mods) for f in found] == [("lua", [])]
    cut = crashes.explain_text(r"sol: runtime error: ...work\autorun\barfoo.lua:3: boom", only_bar)
    assert cut[0].mods == ["bar"] and cut[0].evidence == [r"...work\autorun\barfoo.lua:3"]


def test_code_in_no_module_does_not_blame_the_plugin_that_called_it(game, crash_dir):
    helper = game / "reframework" / "plugins" / "Helper.dll"
    s = session_for(crash_dir, game, log_text(game, T0, crash="", frames=[str(helper)]))
    e = crashes.explain(s, ModIndex(game, deployed(("reframework/plugins/Helper.dll", "h")), [ModRef("h", "Helper")]))
    assert (e.findings[0].kind, e.findings[0].mods) == ("unknown", []) and "Module: Unknown" in e.findings[0].evidence
    stack = next(f for f in e.findings if f.kind == "stack")
    assert (stack.mods, stack.level) == (["h"], "low") and e.category == "unknown"


def test_a_log_without_the_build_lines(game, crash_dir):
    text = "\r\n".join([rec(T0, f"Current game path: {game}"), rec(T0 + 60, "Exception occurred: c0000005", "error"),
                        rec(T0 + 60, "Module: Unknown", "error")]) + "\r\n"
    e = crashes.explain(session_for(crash_dir, game, text), ModIndex(game), now=T0)
    assert e.summary == "15:40 종료 · 오류 코드 0xC0000005 (메모리 접근 오류)" and e.findings[0].kind == "unknown"


# ---- the support zip, REFramework's settings, Steam --------------------------------------------------
def test_the_support_zip(game, crash_dir, tmp_path):
    user_game = r"C:\Users\김철수\Games\MonsterHunterWilds"
    text = log_text(game, T0, [(1, rf"[PluginLoader] Loaded {user_game}\reframework\plugins\A.dll", "info")],
                    crash=str(game / "MonsterHunterWilds.exe"))
    s = session_for(crash_dir, game, text)
    put(game / "reframework_crash.dmp", dump_bytes(T0 + 600), T0 + 600)
    collect(crash_dir, game)
    data = tmp_path / "data"
    write(data / "state.json", '{"settings": {"nexus_api_key": "SECRET"}}')
    write(data / "manager.log", "\n".join(f"line {i} C:\\Users\\김철수\\x" for i in range(2500)))
    mods = [crashes.ReportMod("A", True, "DLL", "1.0", 12, ["옵션 1"], "stale", ["Lib"]), crashes.ReportMod("B", False)]
    e = crashes.explain(s, ModIndex(game))
    report = crashes.report_text(app_version="9.9.9", game_dir=user_game, game_version="abc", steam={"buildid": "1"},
                                 refw="nightly 01432 (최신)", profile="기본", mods=mods, verify=["없어진 파일: x"],
                                 session=crashes.find_session(crash_dir, s.id), explanation=e)
    assert "  1. [켜짐] A  v1.0  DLL  Nexus 12  옵션: 옵션 1  재질: stale  없는 필수 모드: Lib" in report
    assert "  2. [꺼짐] B" in report and "- 없어진 파일: x" in report and e.summary in report
    target = tmp_path / "out" / "report.zip"
    target.parent.mkdir()
    files = deployed(("reframework/plugins/A.dll", "a"))
    crashes.write_report(target, report, crashes.sessions(crash_dir), manager_log=data / "manager.log",
                         deployed=files, names={"a": "A"})
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
        assert "report.txt" in names and f"{s.id}/re2_framework_log.txt" in names and f"{s.id}/session.json" in names
        assert f"{s.id}/reframework_crash.dmp" not in names  # only when asked
        assert not any(n.endswith("state.json") for n in names)
        everything = "".join(zf.read(n).decode("utf-8") for n in names)
        assert "김철수" not in everything and "SECRET" not in everything and r"C:\Users\<사용자>" in everything
        log_lines = zf.read("manager.log").decode().splitlines()
        assert len(log_lines) == crashes.REPORT_LOG_LINES and log_lines[-1].startswith("line 2499")
        assert zf.read("deployed.txt").decode() == "reframework/plugins/A.dll\tA\n"
    crashes.write_report(target, report, crashes.sessions(crash_dir), include_dump=True)
    with zipfile.ZipFile(target) as zf:
        assert zf.read(f"{s.id}/reframework_crash.dmp")[:4] == b"MDMP"
    assert not list(target.parent.glob("*.partial"))


def test_refw_options(game, tmp_path):
    assert not crashes.lua_logging(game, appdata="")  # no config yet: REFramework's default
    config = put(game / "re2_fw_config.txt", "FontSize=16\r\nScriptRunner_LogToDisk=false\r\n", T0)
    assert crashes.refw_option(game, crashlog.LUA_LOG_KEY, appdata="") == "false"
    assert crashes.set_refw_option(game, crashlog.LUA_LOG_KEY, True, appdata="")
    assert crashes.lua_logging(game, appdata="")
    assert config.read_bytes() == b"FontSize=16\r\nScriptRunner_LogToDisk=true\r\n"
    assert not crashes.set_refw_option(game, crashlog.LUA_LOG_KEY, True, appdata="")
    bare = tmp_path / "NoRefw"
    bare.mkdir()
    with pytest.raises(ValueError):
        crashes.set_refw_option(bare, crashlog.LUA_LOG_KEY, True, appdata="")
    assert not (bare / "re2_fw_config.txt").exists()
    # REFramework fell back to %APPDATA%: its config there is the one in use
    appdata = tmp_path / "AppData"
    folder = put(appdata / "REFramework" / "MonsterHunterWilds" / "re2_fw_config.txt", "A=1\r\n", T0).parent
    assert crashes.config_path(bare, str(appdata)) == folder / "re2_fw_config.txt"
    assert crashes.set_refw_option(bare, "B", "2", appdata=str(appdata))
    assert (folder / "re2_fw_config.txt").read_bytes() == b"A=1\r\nB=2\r\n"
    # REFramework picks its folder at every start: the folder its last log came from, file or not
    assert crashes.config_path(game, str(appdata), fallback=False) == game / "re2_fw_config.txt"
    fresh = tmp_path / "Fresh"
    target = crashes.config_path(game, str(fresh), fallback=True)
    assert target == fresh / "REFramework" / "MonsterHunterWilds" / "re2_fw_config.txt"
    assert crashes.set_refw_option(game, crashlog.LOOSE_FILES_KEY, "true", path=target)  # (its folder is made)
    assert target.read_bytes() == b"LooseFileLoader_Enabled=true\r\n"
    assert crashes.config_paths(game, str(fresh)) == [game / "re2_fw_config.txt", target]


def test_steam_build_from_the_app_manifest(tmp_path):
    game = tmp_path / "steamapps" / "common" / "MonsterHunterWilds"
    game.mkdir(parents=True)
    write(tmp_path / "steamapps" / "appmanifest_2246340.acf",
          '"AppState"\n{\n\t"appid"\t\t"2246340"\n\t"buildid"\t\t"20519522"\n\t"LastUpdated"\t\t"1792000000"\n}\n')
    info = crashes.steam_info(game)
    assert info["buildid"] == "20519522"
    assert info["LastUpdated"] == datetime.fromtimestamp(1792000000).strftime("%Y-%m-%d %H:%M")
    assert crashes.steam_info(tmp_path / "elsewhere") == {} and crashes.steam_info(None) == {}
    assert crashes.steam_updated(game) == 1792000000 and crashes.steam_updated(tmp_path / "elsewhere") is None


def test_a_file_that_cannot_be_copied_is_skipped(game, crash_dir, monkeypatch):
    put(game / "re2_framework_log.txt", log_text(game, T0, end=30), T0 + 30)
    put(game / "re2_framework_log.prev.txt", log_text(game, T0 - 86400, end=30), T0 - 86400 + 30)
    real = crashes._copy

    def flaky(src, dst, limit=None):
        if src.name == "re2_framework_log.txt":
            raise PermissionError("locked")
        return real(src, dst, limit)

    monkeypatch.setattr(crashes, "_copy", flaky)
    assert [s.started for s in collect(crash_dir, game)] == [stamp(T0 - 86400)]
    assert sorted(p.name for p in crash_dir.iterdir()) == ["20261015-153922"]  # no half-made folder
    monkeypatch.setattr(crashes, "_copy", real)
    assert [s.started for s in collect(crash_dir, game)] == [stamp(T0)]  # the next try gets it
    assert crashes.collect(crash_dir, None) == [] and crashes.collect(crash_dir, game.parent / "nowhere", appdata="") == []


def test_an_old_faulty_list_is_not_given_to_a_session_that_did_not_write_it(game, crash_dir):
    # kept: a session from yesterday; the list is from a session in between whose log is gone, and the
    # current session (the detector turned off) did not empty it
    put(game / "re2_framework_log.txt", log_text(game, T0 - 86400, end=60), T0 - 86400 + 60)
    collect(crash_dir, game)
    put(game / "re2_framework_log.txt", log_text(game, T0, end=60), T0 + 60)
    put(game / "reframework_faulty_files.txt", '"error" 2 "Invalid file" "GameDesign/x.user"\r\n', T0 - 3600)
    (current,) = collect(crash_dir, game)
    assert current.started == stamp(T0) and current.faulty() == []
    assert all(s.faulty() == [] for s in crashes.sessions(crash_dir))
