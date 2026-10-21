"""Watching the game process (a fake process table and clock; a real child process only on Windows)
and what an exit means, with synthetic REFramework records."""

import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from mhwmm import crashes, gamewatch
from mhwmm.gamewatch import GameExit, GameWatcher

from conftest import write

GAME = Path("C:/Games/MonsterHunterWilds")
EXE = str(GAME / "MonsterHunterWilds.exe")


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class Proc:
    def __init__(self, pid, appears, ends, code=0, path=EXE, openable=True, created=None):
        self.pid, self.appears, self.ends, self.code, self.path = pid, appears, ends, code, path
        self.openable, self.created = openable, appears if created is None else created


class FakeApi(gamewatch.ProcessApi):
    """A process table that changes with the fake clock."""

    def __init__(self, clock, procs, fail_wait=False):
        self.clock, self.procs, self.fail_wait = clock, list(procs), fail_wait
        self.opened, self.closed = [], []

    def _alive(self, p):
        return p.appears <= self.clock.t < p.ends

    def find(self, exe_name):
        assert exe_name == "MonsterHunterWilds.exe"
        return [p.pid for p in self.procs if self._alive(p)]

    def open(self, pid):
        p = next(p for p in self.procs if p.pid == pid)
        if not p.openable:
            return None
        self.opened.append(pid)
        return p

    def image_path(self, h):
        return h.path

    def times(self, h):
        return h.created, (h.ends if self.clock.t >= h.ends else None)

    def wait(self, h, ms):
        if self.fail_wait:
            return None
        if self.clock.t < h.ends:
            self.clock.t = min(h.ends, self.clock.t + ms / 1000)
        return self.clock.t >= h.ends

    def exit_code(self, h):
        return h.code if self.clock.t >= h.ends else gamewatch.STILL_ACTIVE

    def close(self, h):
        self.closed.append(h.pid)


def watcher(clock, api, **kw):
    kw.setdefault("game_dir", GAME)
    return GameWatcher(lambda e: None, api=api, clock=clock, sleep=clock.sleep, **kw)


# ---- exit codes ----------------------------------------------------------------------------------
def test_exit_codes():
    assert gamewatch.is_crash_code(0xC0000005) and gamewatch.is_crash_code(0xE06D7363)
    assert gamewatch.is_crash_code(0x80000003) and gamewatch.is_crash_code(-1073741819)  # signed 0xC0000005
    assert not any(gamewatch.is_crash_code(c) for c in (0, 1, 259, 0x40010004, None))
    assert gamewatch.code_text(0xC0000005) == "0xC0000005 (메모리 접근 오류)"
    assert gamewatch.code_text(0) == "0" and gamewatch.code_text(1) == "1" and gamewatch.code_text(None) == ""
    t = datetime(2026, 10, 16, 6, 39, 22).timestamp()
    assert gamewatch.filetime(int(t * 1e7) + 116444736000000000) == pytest.approx(t)
    assert gamewatch.filetime(0) is None


def test_tasklist_output():
    text = ('"MonsterHunterWilds.exe","1234","Console","1","3,456,789 K"\r\n'
            '"monsterhunterwilds.exe","99","Console","1","12 K"\r\n'
            '"steam.exe","5","Console","1","1 K"\r\n')
    assert gamewatch.parse_tasklist(text) == [1234, 99]
    assert gamewatch.parse_tasklist("정보: 지정된 조건에 맞는 작업이 없습니다.\r\n") == []
    assert gamewatch.parse_tasklist("") == []


def test_an_exit_as_text_and_back():
    e = GameExit(1234, 0xC0000005, 2000.0, started=1427.0, seen=1440.0, path=EXE)
    assert e.crashed and e.seconds == 573.0
    assert e.describe() == "종료 코드 0xC0000005 (메모리 접근 오류), 9분 33초 실행, pid 1234"
    assert GameExit.from_dict(e.to_dict()) == e
    polled = GameExit(7, None, 100.0, seen=40.0, method="poll")
    assert not polled.crashed and polled.seconds == 60.0 and "종료 코드 모름" in polled.describe()
    assert "1분 0초 실행 이상" in polled.describe()
    assert GameExit.from_dict({"pid": "x"}) is None and GameExit.from_dict(None) is None


# ---- the watcher -------------------------------------------------------------------------------------
def test_the_game_is_found_after_a_launch_and_its_exit_code_read(caplog):
    clock = Clock()
    launched = clock.t
    api = FakeApi(clock, [Proc(42, launched + 25, launched + 600, code=0xC0000005)])
    started = []
    w = watcher(clock, api, on_start=started.append)
    with caplog.at_level("INFO", logger="mhwmm.gamewatch"):
        result = w.run(launched)
    assert result.pid == 42 and result.code == 0xC0000005 and result.crashed and result.method == "handle"
    assert result.started == launched + 25 and result.ended == launched + 600 and result.path == EXE
    assert started == [42] and api.closed == [42]
    assert "종료 코드 0xC0000005 (메모리 접근 오류)" in caplog.text  # every exit is logged


def test_nothing_found_in_time():
    clock = Clock()
    api = FakeApi(clock, [Proc(42, clock.t + 500, clock.t + 900)])
    assert watcher(clock, api).run(clock.t, seconds=180) is None
    assert watcher(clock, FakeApi(clock, [])).run(None, seconds=0) is None  # attach: only now


def test_a_game_that_cannot_be_opened_is_watched_from_outside():
    clock = Clock()
    t = clock.t
    api = FakeApi(clock, [Proc(42, t, t + 300, openable=False)])
    result = watcher(clock, api).run(t)
    assert result.code is None and result.method == "poll" and not result.crashed
    assert result.started is None and result.seen == t and result.seconds == pytest.approx(300, abs=2)
    # a wait that fails falls back the same way
    clock2 = Clock()
    api2 = FakeApi(clock2, [Proc(43, clock2.t, clock2.t + 50)], fail_wait=True)
    result = watcher(clock2, api2).run(clock2.t)
    assert result.method == "poll" and result.pid == 43 and api2.closed == [43]


def test_a_game_restarted_at_once_is_followed():
    clock = Clock()
    t = clock.t
    api = FakeApi(clock, [Proc(1, t + 2, t + 6, code=0), Proc(2, t + 10, t + 400, code=0xC0000409)])
    result = watcher(clock, api).run(t)
    assert result.pid == 2 and result.code == 0xC0000409
    # a process with that name that was running before is no restart (it would be waited on for ever)
    clock = Clock()
    t = clock.t
    api = FakeApi(clock, [Proc(9, t - 500, t + 10_000, path="D:/Other/MonsterHunterWilds.exe"),
                          Proc(1, t + 2, t + 6, code=0xC0000005)])
    clock.t = t + 3
    result = watcher(clock, api).run(t)
    assert result.pid == 1 and result.code == 0xC0000005 and clock.t < t + 60
    # ... also when it cannot be opened (its start is not known)
    clock = Clock()
    t = clock.t
    api = FakeApi(clock, [Proc(9, t - 500, t + 10_000, openable=False), Proc(1, t + 2, t + 6, code=0xC0000005)])
    clock.t = t + 3
    result = watcher(clock, api).run(t)
    assert result.pid == 1 and clock.t < t + 60


def test_a_quick_crash_is_reported_not_followed():
    # a crash before REFramework's handler (no record of it), then the user starts the game again
    # (or 게임 다시 실행): the crash is what counts, at once
    clock = Clock()
    t = clock.t
    api = FakeApi(clock, [Proc(1, t + 2, t + 10, code=0xC0000135), Proc(2, t + 35, t + 1235, code=0)])
    result = watcher(clock, api).run(t)
    assert result.pid == 1 and result.code == 0xC0000135 and result.crashed and clock.t < t + 20
    # while waiting for a relaunch after a clean quick exit, no process is being watched
    clock = Clock()
    t = clock.t
    w = None
    pids = []

    class Seen(FakeApi):
        def find(self, exe_name):
            if self.clock.t >= t + 6:
                pids.append(w.pid)
            return super().find(exe_name)

    w = watcher(clock, Seen(clock, [Proc(1, t + 2, t + 6, code=0)]))
    assert w.run(t).pid == 1 and pids and set(pids) == {None} and w.pid is None


def test_a_game_never_found_is_told():
    clock = Clock()
    events = queue.Queue()
    exits, lost = [], []
    w = GameWatcher(exits.append, events=events, api=FakeApi(clock, []), game_dir=GAME, clock=clock,
                    sleep=clock.sleep, on_lost=lost.append)
    launched = clock.t
    assert w.watch(launched)
    kind, func, arg = events.get(timeout=10)
    assert kind == "call" and func == lost.append and arg == launched
    for _ in range(100):
        if not w.busy:
            break
        time.sleep(0.01)
    assert w.attach()  # at the manager's start nothing is promised: nothing is told
    for _ in range(100):
        if not w.busy:
            break
        time.sleep(0.01)
    assert not w.busy and events.empty() and not exits and not lost


def test_the_game_folder_s_copy_and_the_new_process_come_first():
    clock = Clock()
    t = clock.t
    other = Proc(5, t - 10, t + 900, path="D:/Other/MonsterHunterWilds.exe")
    old = Proc(6, t - 1000, t + 900, created=t - 1000)
    new = Proc(7, t + 1, t + 300, code=0)
    api = FakeApi(clock, [other, old, new])
    clock.t = t + 2
    result = watcher(clock, api).run(t)
    assert result.pid == 7 and sorted(api.closed) == [5, 6, 7]
    # only a copy elsewhere (a junction, another library): still watched
    clock = Clock()
    api = FakeApi(clock, [Proc(8, clock.t, clock.t + 100, path="D:/Other/MonsterHunterWilds.exe")])
    assert watcher(clock, api).run(clock.t).pid == 8


def test_stopping_reports_nothing():
    clock = Clock()
    api = FakeApi(clock, [Proc(42, clock.t, clock.t + 10_000)])
    w = watcher(clock, api)
    real_wait = api.wait

    def wait(h, ms):
        w.stop()
        return real_wait(h, ms)

    api.wait = wait
    assert w.run(clock.t) is None and api.closed == [42]


def test_the_result_is_posted_to_the_gui_queue():
    clock = Clock()
    lock = threading.Lock()

    class Locked(FakeApi):
        def find(self, exe_name):
            with lock:
                return super().find(exe_name)

    api = Locked(clock, [Proc(42, clock.t, clock.t + 5000, code=0)])
    events = queue.Queue()
    got = []
    w = GameWatcher(got.append, events=events, api=api, game_dir=GAME, clock=clock, sleep=clock.sleep,
                    on_start=got.append)
    assert w.available and w.watch(clock.t)
    kind, func, result = events.get(timeout=10)[:3]  # on_start first
    assert kind == "call" and result == 42
    kind, func, result = events.get(timeout=10)
    assert kind == "call" and func == got.append and result.pid == 42 and result.code == 0
    for _ in range(100):
        if not w.busy:
            break
        time.sleep(0.01)
    assert not w.busy and w.pid is None and got == []  # posted, not called on the watcher's thread


def test_nothing_is_watched_without_the_system_api():
    w = GameWatcher(lambda e: None, api=None)
    w.api = None
    assert not w.available and not w.watch() and not w.attach()
    if os.name != "nt":
        assert gamewatch.default_api() is None


# ---- what an exit means -------------------------------------------------------------------------------
def stamp(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def crash_log(game: Path, start: float, crash_at: float) -> str:
    def rec(t, msg, level="info"):
        return f"[{stamp(t)}] [REFramework] [{level}] {msg}"

    lines = [rec(start, "REFramework entry"), rec(start, "Commit hash: d052e8fd3fd41d0a1a33394fcf6e6c19be39b1ee"),
             rec(start, f"Current game path: {game}"),
             rec(crash_at, "Exception occurred: c0000005", "error"), rec(crash_at, "Call stack:", "error"),
             rec(crash_at, "", "error"), " KiUserExceptionDispatcher", r"  C:\WINDOWS\SYSTEM32\ntdll.dll + 0x165dee",
             "", " Unknown symbol", f"  {game / 'MonsterHunterWilds.exe'} + 0x1234", "",
             rec(crash_at, f"Module: 7ff600000000 {game / 'MonsterHunterWilds.exe'}", "error")]
    return "\r\n".join(lines) + "\r\n"


@pytest.fixture
def game(tmp_path):
    game = tmp_path / "MonsterHunterWilds"
    write(game / "MonsterHunterWilds.exe", "exe")
    return game


def test_a_crash_record_since_the_launch(game, tmp_path):
    crash_dir = tmp_path / "data" / "crashes"
    launched = datetime(2026, 10, 16, 15, 39, 0).timestamp()
    write(game / "re2_framework_log.txt", crash_log(game, launched + 20, launched + 573))
    exit_ = GameExit(42, 0xC0000005, launched + 574, started=launched + 15)
    d = gamewatch.detect(crash_dir, launched, exit_, game, {"mods": []}, appdata="")
    assert d.crashed and d.code == "c0000005" and d.record is not None and d.alarm and not d.normal
    assert d.label == "비정상 종료를 확인했습니다 (0xC0000005)"
    # the record alone (the manager was closed meanwhile) says so too
    d = gamewatch.detect(crash_dir, launched, None, game, {}, appdata="")
    assert d.crashed and d.code == "c0000005" and d.exit is None
    # a record of a game before the launch does not count
    later = launched + 3600
    d = gamewatch.detect(crash_dir, later, GameExit(43, 0, later + 900, started=later + 5), game, {}, appdata="")
    assert d.crashed is False and d.record is None and d.normal and d.label == ""


def test_exits_without_a_record():
    assert gamewatch.judge(GameExit(1, 0xC0000409, 100.0, started=90.0)).crashed  # fail-fast: no REFramework record
    assert gamewatch.judge(GameExit(1, 0xC0000409, 100.0)).label == "비정상 종료를 확인했습니다 (0xC0000409)"
    short = gamewatch.judge(GameExit(1, 0, 100.0, started=90.0))
    assert short.crashed is False and not short.normal  # closed at once: not a test of the mods
    assert gamewatch.judge(GameExit(1, 1, 1000.0, started=0.0)).crashed is None  # killed, or an unknown code: ask
    assert gamewatch.judge(GameExit(1, None, 1000.0, started=0.0, method="poll")).crashed is None
    assert gamewatch.judge(None).crashed is None and not gamewatch.judge(None).alarm


def test_the_last_normal_exit_keeps_its_mods(game, tmp_path):
    crash_dir = tmp_path / "data" / "crashes"
    launched = datetime(2026, 10, 16, 15, 39, 0).timestamp()
    mods = {"game_version": "v1", "mods": [{"id": "a", "name": "A", "enabled": True, "stamp": "1|"}]}
    crashes.note_launch(crash_dir, mods, now=launched)
    normal = GameExit(42, 0, launched + 1200, started=launched + 30)
    d = gamewatch.after_exit(crash_dir, normal, launched, game, {"mods": []}, keep_good=False, appdata="")
    assert d.normal and gamewatch.load_last_good(crash_dir) is None  # (while 원인 모드 찾기 runs)
    d = gamewatch.after_exit(crash_dir, normal, launched, game, {"mods": []}, appdata="")
    good = gamewatch.load_last_good(crash_dir)
    assert good["context"] == mods and good["code"] == 0 and good["seconds"] == 1170.0  # the launch's mods
    # a game not started from the manager: the mods as they are now
    attached = GameExit(43, 0, launched + 90_000, started=launched + 80_000)
    gamewatch.after_exit(crash_dir, attached, None, game, {"mods": [{"id": "b", "enabled": True}]}, appdata="")
    assert gamewatch.load_last_good(crash_dir)["context"]["mods"][0]["id"] == "b"
    # a crash never replaces it
    write(game / "re2_framework_log.txt", crash_log(game, launched + 100_000, launched + 100_300))
    crashed = GameExit(44, 0, launched + 100_400, started=launched + 99_990)
    d = gamewatch.after_exit(crash_dir, crashed, launched + 99_980, game, {"mods": []}, appdata="")
    assert d.crashed and gamewatch.load_last_good(crash_dir)["context"]["mods"][0]["id"] == "b"
    write(crash_dir / gamewatch.LAST_GOOD_FILE, '{"format": 0}')
    assert gamewatch.load_last_good(crash_dir) is None


def test_the_launch_a_process_came_from(tmp_path):
    crash_dir = tmp_path / "crashes"
    crashes.note_launch(crash_dir, {"mods": [1]}, now=1000.0)
    assert gamewatch.launch_context(crash_dir, 1030.0) == {"mods": [1]}
    assert gamewatch.launch_context(crash_dir, 1000.0 + 16 * 60) is None
    assert gamewatch.launch_context(crash_dir, None) is None
    assert gamewatch.launch_context(tmp_path / "none", 1030.0) is None


# ---- a real process (Windows only) ---------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Windows process API")
def test_a_real_crashing_process_on_windows():
    api = gamewatch.WinApi()
    name = os.path.basename(sys.executable)
    launched = time.time()
    child = subprocess.Popen([sys.executable, "-c",
                              "import ctypes, time; time.sleep(1.5); ctypes.windll.kernel32.ExitProcess(0xC0000005)"])
    try:
        assert child.pid in (api.find(name) or [])
        handle = api.open(child.pid)
        assert handle
        assert os.path.basename(api.image_path(handle)).casefold() == name.casefold()
        created, _exited = api.times(handle)
        assert created is not None and abs(created - launched) < 60
        deadline = time.time() + 30
        while not api.wait(handle, 200):
            assert time.time() < deadline
        assert api.exit_code(handle) == 0xC0000005
        api.close(handle)
    finally:
        child.wait(timeout=30)
    assert child.returncode & 0xFFFFFFFF == 0xC0000005

    # the whole watcher, on a process started after the "launch"
    got = []
    w = GameWatcher(got.append, api=api, exe_name=name, relaunch_wait=0.5, poll=0.2)
    launched = time.time()
    child = subprocess.Popen([sys.executable, "-c",
                              "import ctypes, time; time.sleep(2); ctypes.windll.kernel32.ExitProcess(0xC0000005)"])
    try:
        result = w.run(launched, seconds=20)
    finally:
        child.wait(timeout=30)
    assert result is not None and result.code == 0xC0000005 and result.crashed and result.method == "handle"
