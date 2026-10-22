"""Watching the game process: how it ended (게임 종료 확인).

The game is started through Steam (``steam://rungameid/``), which gives no process handle, so
after a launch (and when the manager starts while the game runs) the watcher looks for
MonsterHunterWilds.exe with a Toolhelp snapshot, checks its path (QueryFullProcessImageNameW),
opens it with SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION and waits in a daemon thread; the
exit code (GetExitCodeProcess) and times (GetProcessTimes) are then posted to the GUI through its
events queue (a copy in the game folder is preferred to one elsewhere).  When the process cannot
be opened (a game run as administrator) it only polls whether the process is still there (no exit
code then); without Toolhelp ``tasklist`` lists it.

With REFramework's crash handler the exit code is the exception code (0xC0000005...), but the
code of a normal quit is not known for sure, so every exit is written to manager.log, and only a
code >= 0xC0000000 (or 0x80000003) or a new REFramework crash record counts as a crash.  An exit
with code 0 after more than NORMAL_SECONDS and no new record is a normal one: the mods it ran
with are kept as the last good set (``crashes/last_good.json``) for 원인 모드 찾기, and every watched run
is added to ``crashes/launches.json`` (how it ended, how long it ran and with which mods), which 원인 모드
찾기 weighs as earlier runs of the same setup.

Everything Windows-only is guarded: on other systems (or when an API is missing) nothing is
watched, and no API failure reaches the caller.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import crashes, crashlog, fsutil
from .constants import GAME_EXE

log = logging.getLogger(__name__)

FIND_SECONDS = 180.0  # after a launch the game is looked for this long (Steam may update it first)
POLL_SECONDS = 2.0
WAIT_MS = 1000  # one wait on the process; stop() is noticed between waits
RELAUNCH_SECONDS = 15.0  # a game that ends this soon may have been restarted (by Steam): follow the new one
RELAUNCH_WAIT = 30.0
NORMAL_SECONDS = 180.0  # an exit with code 0 after this long (and no crash record) is a normal one
LAUNCH_SECONDS = crashes.LAUNCH_SECONDS  # a process starting this soon after a launch from the manager is its
LAST_GOOD_FILE = "last_good.json"  # in the crash records folder
LAST_GOOD_FORMAT = 1
LAUNCHES_FILE = "launches.json"  # in the crash records folder: every watched run, the newest last
LAUNCHES_FORMAT = 1
MAX_LAUNCHES = 100
LAUNCH_DAYS = 90
OUTCOMES = ("crash", "normal", "unknown")

STILL_ACTIVE = 259
CRASH_CODE_MIN = 0xC0000000  # NTSTATUS errors (and C++ exceptions, 0xE06D7363)
BREAKPOINT = 0x80000003
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x2
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
_EPOCH_AS_FILETIME = 116444736000000000  # 1970-01-01 in 100 ns since 1601

DETECTED = "비정상 종료를 확인했습니다 ({code})"
DETECTED_NO_CODE = "비정상 종료를 확인했습니다"


def is_crash_code(code: int | None) -> bool:
    """An exit code that means the game crashed (an unhandled exception or a breakpoint)."""
    if code is None:
        return False
    code &= 0xFFFFFFFF
    return code >= CRASH_CODE_MIN or code == BREAKPOINT


def code_text(code: int | None) -> str:
    """``0xC0000005 (메모리 접근 오류)``; small codes as numbers ("0", "1"); "" when unknown."""
    if code is None:
        return ""
    code &= 0xFFFFFFFF
    if code < 0x10000:
        return str(code)
    return crashlog.exception_label(code)


def filetime(value: int) -> float | None:
    """A FILETIME (100 ns since 1601, UTC) as epoch seconds (None for 0)."""
    if not value:
        return None
    return (value - _EPOCH_AS_FILETIME) / 1e7


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}초"
    if seconds < 3600:
        return f"{seconds // 60}분 {seconds % 60}초"
    return f"{seconds // 3600}시간 {seconds % 3600 // 60}분"


@dataclass
class GameExit:
    """How one game process ended."""
    pid: int
    code: int | None  # exit code (None: not known: the process could only be watched from outside)
    ended: float  # epoch seconds
    started: float | None = None  # the process's creation time
    seen: float | None = None  # when the watcher found it
    path: str = ""
    method: str = "handle"  # "handle" (waited on it) | "poll" (only saw it go away)

    @property
    def seconds(self) -> float | None:
        """How long it ran (from when it was found, if its start is not known)."""
        start = self.started if self.started is not None else self.seen
        return max(0.0, self.ended - start) if start is not None else None

    @property
    def crashed(self) -> bool:
        return is_crash_code(self.code)

    def describe(self) -> str:
        """For manager.log: ``종료 코드 0xC0000005 (메모리 접근 오류), 9분 33초 실행, pid 1234``."""
        parts = [f"종료 코드 {code_text(self.code)}" if self.code is not None
                 else "종료 코드 모름(프로세스를 열 수 없어 끝난 것만 확인)"]
        if self.seconds is not None:
            parts.append(f"{_duration(self.seconds)} 실행" + ("" if self.started is not None else " 이상"))
        parts.append(f"pid {self.pid}")
        return ", ".join(parts)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data) -> GameExit | None:
        if not isinstance(data, dict) or not isinstance(data.get("pid"), int) or \
                not isinstance(data.get("ended"), (int, float)):
            return None

        def number(key):
            value = data.get(key)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        code = data.get("code")
        return cls(data["pid"], code if isinstance(code, int) and not isinstance(code, bool) else None,
                   float(data["ended"]), number("started"), number("seen"), str(data.get("path") or ""),
                   str(data.get("method") or "handle"))


# ---- the system calls ------------------------------------------------------------------------------
class ProcessApi:
    """What the watcher asks the system (a fake one in tests). Handles are opaque."""

    def find(self, exe_name: str) -> list[int] | None:
        """Ids of the running processes with this file name (None: could not be listed)."""
        raise NotImplementedError

    def open(self, pid: int):
        """A handle to wait on, or None (e.g. the game runs as administrator)."""
        return None

    def image_path(self, handle) -> str:
        return ""

    def times(self, handle) -> tuple[float | None, float | None]:
        """(created, exited) as epoch seconds."""
        return None, None

    def wait(self, handle, ms: int) -> bool | None:
        """True when the process has ended, False when it still runs after ``ms``, None on a failure."""
        return None

    def exit_code(self, handle) -> int | None:
        return None

    def close(self, handle) -> None:
        pass


def parse_tasklist(text: str, exe_name: str = GAME_EXE) -> list[int]:
    """Process ids from ``tasklist /FO CSV /NH`` output (other lines, such as "INFO: ...", are skipped)."""
    out = []
    for row in csv.reader(io.StringIO(text or "")):
        if len(row) >= 2 and row[0].strip().casefold() == exe_name.casefold() and row[1].strip().isdigit():
            out.append(int(row[1].strip()))
    return out


class TasklistApi(ProcessApi):
    """Only finding processes, with tasklist (when Toolhelp is not available)."""

    def find(self, exe_name: str) -> list[int] | None:
        if os.name != "nt":
            return None
        try:
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH", "/FO", "CSV"],
                                 capture_output=True, text=True, timeout=10, errors="replace",
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        return parse_tasklist(out, exe_name)


class WinApi(ProcessApi):
    """kernel32 through ctypes (Windows only: building it elsewhere raises)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", wintypes.WCHAR * 260)]

        self._ctypes, self._wintypes, self._entry = ctypes, wintypes, PROCESSENTRY32W
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # its own function objects: no shared prototypes
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32FirstW.restype = k32.Process32NextW.restype = wintypes.BOOL
        k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.OpenProcess.restype = wintypes.HANDLE  # a HANDLE is 64 bits: the default int would cut it
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k32 = k32
        self._invalid = ctypes.c_void_p(-1).value
        self._tasklist = TasklistApi()

    def find(self, exe_name: str) -> list[int] | None:
        ctypes = self._ctypes
        try:
            snap = self.k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        except OSError:
            snap = None
        if not snap or snap == self._invalid:
            return self._tasklist.find(exe_name)
        pids = []
        try:
            entry = self._entry()
            entry.dwSize = ctypes.sizeof(entry)
            ok = self.k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                if entry.szExeFile.casefold() == exe_name.casefold():
                    pids.append(int(entry.th32ProcessID))
                ok = self.k32.Process32NextW(snap, ctypes.byref(entry))
        except OSError:
            return self._tasklist.find(exe_name)
        finally:
            self.k32.CloseHandle(snap)
        return pids

    def open(self, pid: int):
        try:
            handle = self.k32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except OSError:
            return None
        return handle or None

    def image_path(self, handle) -> str:
        ctypes, wintypes = self._ctypes, self._wintypes
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if self.k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
        except OSError:
            pass
        return ""

    def times(self, handle) -> tuple[float | None, float | None]:
        ctypes, wintypes = self._ctypes, self._wintypes
        try:
            t = [wintypes.FILETIME() for _ in range(4)]
            if self.k32.GetProcessTimes(handle, *[ctypes.byref(x) for x in t]):
                value = [(x.dwHighDateTime << 32) | x.dwLowDateTime for x in t[:2]]
                return filetime(value[0]), filetime(value[1])
        except OSError:
            pass
        return None, None

    def wait(self, handle, ms: int) -> bool | None:
        try:
            r = self.k32.WaitForSingleObject(handle, int(ms))
        except OSError:
            return None
        return True if r == WAIT_OBJECT_0 else False if r == WAIT_TIMEOUT else None

    def exit_code(self, handle) -> int | None:
        ctypes, wintypes = self._ctypes, self._wintypes
        try:
            code = wintypes.DWORD()
            if self.k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return int(code.value)
        except OSError:
            pass
        return None

    def close(self, handle) -> None:
        try:
            self.k32.CloseHandle(handle)
        except OSError:
            pass


def default_api() -> ProcessApi | None:
    """The system's API (None where the game cannot run: nothing is watched there)."""
    if os.name != "nt":
        return None
    try:
        return WinApi()
    except Exception as exc:  # an old or odd system: only tasklist
        log.warning("게임 프로세스를 지켜보는 기능을 쓸 수 없어 tasklist로 확인합니다: %s", exc)
        return TasklistApi()


# ---- the watcher ---------------------------------------------------------------------------------
@dataclass
class _Found:
    pid: int
    handle: object = None
    path: str = ""
    created: float | None = None
    seen: float | None = None
    listed: frozenset = frozenset()  # every process of that name running when it was found


class GameWatcher:
    """Finds the game after a launch (or running at startup) and reports how it ended.

    ``on_exit(GameExit)`` (and ``on_start(pid)``) are called on the watcher's thread, or, with
    ``events`` (the GUI's queue), posted to it as ``("call", on_exit, game_exit)``. ``on_lost(launched)``:
    a watch after a launch ended without an exit to report (the game was not found in time, or
    watching failed). ``game_dir``: a process started from there is preferred (when its path can be read)."""

    def __init__(self, on_exit: Callable[[GameExit], None], events=None, api: ProcessApi | None = None,
                 game_dir=None, exe_name: str = GAME_EXE, on_start: Callable[[int], None] | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] | None = None,
                 poll: float = POLL_SECONDS, relaunch_seconds: float = RELAUNCH_SECONDS,
                 relaunch_wait: float = RELAUNCH_WAIT, on_lost: Callable[[float | None], None] | None = None):
        self.on_exit, self.on_start, self.on_lost, self.events = on_exit, on_start, on_lost, events
        self.api = api if api is not None else default_api()
        self.game_dir = Path(game_dir) if game_dir else None
        self.exe_name = exe_name
        self.clock = clock
        self._stop = threading.Event()
        self._sleep = sleep if sleep is not None else self._stop.wait
        self.poll, self.relaunch_seconds, self.relaunch_wait = poll, relaunch_seconds, relaunch_wait
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.pid: int | None = None  # the process watched now

    @property
    def available(self) -> bool:
        return self.api is not None

    @property
    def busy(self) -> bool:
        """Looking for the game or waiting for it to end."""
        return self._thread is not None and self._thread.is_alive()

    def watch(self, launched: float | None = None, seconds: float = FIND_SECONDS) -> bool:
        """Look for the game for ``seconds`` (0: only now) and wait for it to end, in the background.
        ``launched``: when the manager started it (a process started after it is preferred). False when
        nothing can be watched here or a watch is already running."""
        if self.api is None:
            return False
        with self._lock:
            if self.busy:
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._thread_main, args=(launched, seconds), daemon=True,
                                            name="game-watch")
            self._thread.start()
        return True

    def attach(self) -> bool:
        """Watch the game if it runs now (at the manager's start)."""
        return self.watch(None, 0)

    def stop(self) -> None:
        """Stop watching (the manager closes); nothing is reported then."""
        self._stop.set()

    def _post(self, func, *args) -> None:
        if func is None:
            return
        if self.events is not None:
            self.events.put(("call", func, *args))
        else:
            func(*args)

    def _thread_main(self, launched, seconds) -> None:
        try:
            result = self.run(launched, seconds)
        except Exception:  # never in the user's way
            log.exception("게임 종료를 확인하지 못했습니다.")
            result = None
        self.pid = None
        if self._stop.is_set():
            return
        if result is not None:
            self._post(self.on_exit, result)
        elif seconds:  # after a launch: nothing will be reported for it
            self._post(self.on_lost, launched)

    # ---- the work (on the watcher's thread; tests call it directly)
    def run(self, launched: float | None = None, seconds: float = FIND_SECONDS) -> GameExit | None:
        found = self._look(launched, seconds)
        if found is None:
            if seconds:
                log.info("게임 프로세스(%s)를 찾지 못해 종료를 확인하지 않습니다.", self.exe_name)
            return None
        result = self._wait(found)
        self.pid = None
        # Steam may restart the game at once (started from its exe, or after an update): follow the new
        # one - a process that was not there before and was created after the one that ended. A crash
        # (even a quick one) is reported at once: the next run is the user's, not Steam's.
        while result is not None and not result.crashed and result.seconds is not None \
                and result.seconds < self.relaunch_seconds:
            newer = result.started if result.started is not None else result.ended - self.relaunch_seconds
            again = self._look(None, self.relaunch_wait, skip=found.listed | {result.pid}, newer=newer)
            if again is None:
                break
            log.info("게임이 바로 다시 시작되었습니다 (%s); 새 프로세스를 지켜봅니다.", result.describe())
            found = again
            result = self._wait(again)
            self.pid = None
        if result is not None:
            log.info("게임이 끝났습니다: %s", result.describe())
        return result

    def _look(self, launched, seconds, skip=(), newer: float | None = None) -> _Found | None:
        """Poll for the game for ``seconds``; ``newer``: only a process created after that (a restart)."""
        deadline = self.clock() + seconds
        while not self._stop.is_set():
            found = self._find(launched, skip, newer)
            if found is not None:
                self.pid = found.pid
                if self.on_start is not None:
                    self._post(self.on_start, found.pid)
                return found
            if self.clock() >= deadline:
                return None
            self._sleep(self.poll)
        return None

    def _find(self, launched, skip, newer=None) -> _Found | None:
        try:
            pids = self.api.find(self.exe_name) or []
        except Exception as exc:
            log.debug("프로세스 목록을 읽지 못했습니다: %s", exc)
            return None
        candidates = []
        for pid in pids:
            if pid in skip:
                continue
            item = _Found(pid, seen=self.clock())
            try:
                item.handle = self.api.open(pid)
                if item.handle is not None:
                    item.path = self.api.image_path(item.handle) or ""
                    item.created = self.api.times(item.handle)[0]
            except Exception as exc:
                log.debug("게임 프로세스를 열지 못했습니다 (pid %s): %s", pid, exc)
            if newer is not None and item.created is not None and item.created <= newer:
                self._close(item)  # was already running: not a restart
                continue
            candidates.append(item)
        if not candidates:
            return None
        for c in candidates:
            c.listed = frozenset(pids)
        # the game folder's copy (a path through a junction may differ), started after the launch, the newest
        after = launched - 5 if launched is not None else None
        best = max(candidates, key=lambda c: (
            not c.path or self.game_dir is None or _in_folder(c.path, self.game_dir),
            after is None or (c.created is not None and c.created >= after), c.created or 0.0))
        if best.path and self.game_dir is not None and not _in_folder(best.path, self.game_dir):
            log.info("게임 폴더가 아닌 곳의 게임을 지켜봅니다: %s", best.path)
        for c in candidates:
            if c is not best:
                self._close(c)
        return best

    def _close(self, item: _Found) -> None:
        if item.handle is not None:
            try:
                self.api.close(item.handle)
            except Exception:
                pass
            item.handle = None

    def _wait(self, found: _Found) -> GameExit | None:
        if found.handle is not None:
            try:
                while not self._stop.is_set():
                    done = self.api.wait(found.handle, WAIT_MS)
                    if done is None:
                        break  # the wait failed: watch from outside
                    if done:
                        code = self.api.exit_code(found.handle)
                        created, exited = self.api.times(found.handle)
                        if code == STILL_ACTIVE:
                            code = None
                        return GameExit(found.pid, code, exited or self.clock(), created or found.created,
                                        found.seen, found.path, "handle")
                else:
                    return None
            except Exception as exc:
                log.debug("게임 프로세스를 기다리지 못했습니다: %s", exc)
            finally:
                self._close(found)
        # no handle (a game run as administrator): only see when it is gone
        log.info("게임 프로세스를 열 수 없어 종료 코드 없이 끝나는 것만 확인합니다 (pid %s).", found.pid)
        misses = 0
        while not self._stop.is_set():
            try:
                pids = self.api.find(self.exe_name)
            except Exception:
                pids = None
            if pids is None:
                misses += 1
                if misses >= 30:  # the list cannot be read any more
                    return None
            elif found.pid not in pids:
                return GameExit(found.pid, None, self.clock(), found.created, found.seen, found.path, "poll")
            self._sleep(self.poll)
        return None


def _in_folder(path: str, folder: Path) -> bool:
    p = crashlog.norm_path(path)
    return p.rsplit("/", 1)[0] == crashlog.norm_path(str(folder)).rstrip("/")


# ---- what an exit means ------------------------------------------------------------------------------
@dataclass
class Detection:
    """How a game session ended, from its exit code and the crash records collected after it."""
    crashed: bool | None  # True: a crash; False: a clean exit (code 0, no record); None: not known
    code: str = ""  # exception code, 8 hex digits ("" if none)
    exit: GameExit | None = None
    record: crashes.Session | None = None  # a crash record written since the launch
    session: crashes.Session | None = None  # the record of this run, crash or not (detect)

    @property
    def normal(self) -> bool:
        """A normal quit (code 0, a long enough run, no crash record): its mods ran fine."""
        return (self.crashed is False and self.exit is not None and self.exit.code == 0
                and (self.exit.seconds or 0) >= NORMAL_SECONDS)

    @property
    def label(self) -> str:
        """"비정상 종료를 확인했습니다 (0xC0000005)" for a crash ("" otherwise)."""
        if not self.crashed:
            return ""
        return DETECTED.format(code=f"0x{self.code.upper()}") if self.code else DETECTED_NO_CODE

    @property
    def alarm(self) -> bool:
        """Worth telling the user about without being asked."""
        return self.crashed is True


def judge(exit: GameExit | None, record: crashes.Session | None = None, reported: bool = False) -> Detection:
    """Detection from an exit (None: not watched), a crash record since the launch and whether the
    game's own crash reporter wrote a report since then (``reported``: the game then ends with exit
    code 0 although it crashed)."""
    code = ""
    if exit is not None and exit.crashed:
        code = crashlog.normalize_code(exit.code)
    elif record is not None:
        code = crashlog.normalize_code(record.summary.get("crash") or "")
    if (exit is not None and exit.crashed) or record is not None or reported:
        crashed = True
    elif exit is not None and exit.code == 0:
        crashed = False
    else:
        crashed = None  # killed, frozen and closed, an unknown code, or not watched: ask
    return Detection(crashed, code, exit, record)


def detect(crash_dir, since: float | None, exit: GameExit | None = None, game_dir=None, context: dict | None = None,
           appdata=None) -> Detection:
    """Collect REFramework's records of the session (with ``game_dir``) and judge how it ended; the record of
    this run, crash or not, is ``Detection.session``. ``since``: when the game was launched (records ending
    after it count). Reads files: run it off the GUI thread."""
    detection = _judge_records(crash_dir, since, exit, game_dir, context, appdata)
    detection.session = run_session(crash_dir, since, exit)
    return detection


def run_session(crash_dir, since: float | None, exit: GameExit | None = None) -> crashes.Session | None:
    """The kept record of the run launched at ``since`` (or the process ``exit`` describes); None if not known.
    Without the exit (not watched: the command line, a restart of the manager) the first run since the launch:
    the game may have been started again since."""
    known = [t for t in ((exit.started, exit.seen) if exit is not None else ()) if t is not None]
    start = min([*known, *([since] if since is not None else [])], default=None)
    if start is None:
        return None
    try:
        return crashes.session_since(crash_dir, start, exit.ended if exit is not None else None, first=exit is None)
    except Exception as exc:
        log.warning("게임 기록을 읽지 못했습니다: %s", exc)
        return None


def _judge_records(crash_dir, since: float | None, exit: GameExit | None = None, game_dir=None,
                   context: dict | None = None, appdata=None) -> Detection:
    """detect without the run's record (Detection.session)."""
    if game_dir:
        try:
            crashes.collect(crash_dir, game_dir, context, appdata)
        except Exception as exc:  # the exit code still says something
            log.warning("게임 기록을 모으지 못했습니다: %s", exc)
    start = since
    if exit is not None:
        known = [t for t in (exit.started, exit.seen) if t is not None]
        start = min([*known, *([since] if since is not None else [])], default=None)
    record = None
    reported = False
    if start is not None:
        try:
            record = crashes.crashed_since(crash_dir, start - 5, first=exit is None,  # (not watched: see run_session)
                                           until=exit.ended if exit is not None and exit.ended else None)
        except Exception as exc:
            log.warning("게임 기록을 읽지 못했습니다: %s", exc)
        reported = record is None and game_reported_since(game_dir, start - 5,
                                                          exit.ended if exit is not None and exit.ended else None)
        if reported:
            log.warning("게임이 스스로 오류 보고서(CrashReport)를 만들었습니다: 종료 코드와 관계없이 튕긴 것으로 봅니다.")
    return judge(exit, record, reported)


def game_reported_since(game_dir, since: float, until: float | None = None) -> bool:
    """Whether the game's own crash reporter wrote a report in <game>\\CrashReport for a crash between ``since``
    and ``until`` (default: now): the crash's minute in its name (2026_1022_0346.zip, or a temporary file while
    it is still being written), else a zip's time. An unpacked or copied earlier report is not this run's."""
    folder = crashes.game_report_dir(game_dir)
    until = (until if until is not None else time.time()) + 60
    try:
        entries = list(folder.iterdir()) if folder is not None and folder.is_dir() else []
    except OSError:
        return False
    for p in entries:
        named = crashes.report_name_time(p.name)
        if named is not None:
            if since - 60 <= named <= until:
                return True
            continue
        try:
            if p.suffix.lower() == ".zip" and since <= p.stat().st_mtime <= until:
                return True
        except OSError:
            continue
    return False


# ---- the last normal exit ---------------------------------------------------------------------------
def launch_context(crash_dir, started: float | None) -> dict | None:
    """The mods of the launch from the manager a process started at ``started`` came from (launch.json)."""
    data = fsutil.read_json(Path(crash_dir) / crashes.LAUNCH_FILE, None)
    if not isinstance(data, dict) or not isinstance(data.get("time"), (int, float)) or started is None:
        return None
    context = data.get("context")
    if not isinstance(context, dict) or not -60 <= started - data["time"] <= LAUNCH_SECONDS:
        return None
    return context


def save_last_good(crash_dir, detection: Detection, context: dict | None = None) -> bool:
    """Keep the mods of a normal exit as the last good set (``context``: the manager's crash_context
    when the game was not launched from it). Returns whether it was saved."""
    if not detection.normal:
        return False
    exit = detection.exit
    launched = launch_context(crash_dir, exit.started or exit.seen)
    if isinstance(launched, dict) and launched.get("bisect"):
        # (not the profile in use now either: the game ran with the step's mods)
        log.info("원인 찾기 단계로 실행한 게임이라 정상 종료 기록으로 남기지 않습니다.")
        return False
    context = launched or context
    if not isinstance(context, dict) or not isinstance(context.get("mods"), list):
        return False
    try:
        fsutil.atomic_write_json(Path(crash_dir) / LAST_GOOD_FILE, {
            "format": LAST_GOOD_FORMAT, "time": exit.ended, "started": exit.started, "seconds": exit.seconds,
            "code": exit.code, "context": context})
    except OSError as exc:
        log.warning("정상 종료 기록을 저장하지 못했습니다: %s", exc)
        return False
    log.info("정상 종료된 모드 구성을 기억합니다 (모드 %d개 켜짐).",
             sum(1 for m in context["mods"] if isinstance(m, dict) and m.get("enabled")))
    return True


def load_last_good(crash_dir) -> dict | None:
    data = fsutil.read_json(Path(crash_dir) / LAST_GOOD_FILE, None)
    if not isinstance(data, dict) or data.get("format") != LAST_GOOD_FORMAT or \
            not isinstance(data.get("context"), dict):
        return None
    if _crashed_during(crash_dir, data):
        return None
    return data


def _crashed_during(crash_dir, data: dict) -> bool:
    """A crash was recorded during the run kept as the last good one (e.g. the game's own crash report,
    collected only later, of a run that ended with exit code 0): forget that run."""
    ended = data.get("time")
    started = data.get("started") or (ended - data["seconds"] if isinstance(ended, (int, float))
                                      and isinstance(data.get("seconds"), (int, float)) else None)
    if not isinstance(ended, (int, float)) or not isinstance(started, (int, float)):
        return False
    try:
        crashed = any(s.problem() == "crash" and started - 5 <= float(s.summary.get("crash_time") or 0) <= ended + 5
                      for s in crashes.sessions(crash_dir))
    except Exception as exc:
        log.debug("게임 기록을 읽지 못했습니다: %s", exc)
        return False
    if crashed:
        log.warning("정상 종료로 기억했던 실행에서 튕긴 기록이 나와 그 기억을 지웁니다.")
        try:
            (Path(crash_dir) / LAST_GOOD_FILE).unlink()
        except OSError:
            pass
    return crashed


def after_exit(crash_dir, exit: GameExit | None, launched: float | None = None, game_dir=None,
               context: dict | None = None, keep_good: bool = True, appdata=None) -> Detection:
    """Everything to do when the game ended (off the GUI thread): collect the records, judge the
    exit and, for a normal one, keep its mods as the last good set (``keep_good``: False while
    원인 모드 찾기 runs, whose steps are not the user's own setup)."""
    detection = detect(crash_dir, launched, exit, game_dir, context, appdata)
    if detection.crashed:
        log.warning("게임이 비정상적으로 종료되었습니다%s.", f" ({crashlog.exception_label(detection.code)})"
                    if detection.code else "")
    if keep_good and detection.normal:
        save_last_good(crash_dir, detection, context)
    record_launch(crash_dir, detection, context, bisect=not keep_good)
    return detection


# ---- every run (launches.json) -----------------------------------------------------------------------
_launches_lock = threading.Lock()


def options_digest(names) -> str:
    """A mod's chosen options in 8 characters: the start of the sha1 of their names (sorted, one per line);
    "" for none."""
    names = sorted(str(n) for n in names or ())
    return hashlib.sha1("\n".join(names).encode("utf-8")).hexdigest()[:8] if names else ""


def launch_mods(context) -> list[list[str]] | None:
    """[[id, stamp, options digest]] of the mods enabled and applied in a launch context (crash_context;
    None when it does not say)."""
    mods = context.get("mods") if isinstance(context, dict) else None
    if not isinstance(mods, list):
        return None
    placed = context.get("placed")
    placed = {i for i in placed if isinstance(i, str)} if isinstance(placed, list) else None
    out = []
    for m in mods:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m.get("enabled"):
            continue
        if placed is None or m["id"] in placed:
            options = m.get("options") if isinstance(m.get("options"), list) else ()
            out.append([m["id"], str(m.get("stamp") or ""), options_digest(options)])
    return out


def _row(data) -> dict | None:
    """A launches.json row as written, or None for a damaged one."""
    if not isinstance(data, dict) or not isinstance(data.get("time"), (int, float)) or \
            data.get("outcome") not in OUTCOMES or not isinstance(data.get("mods"), list):
        return None
    mods = [m for m in data["mods"] if isinstance(m, list) and len(m) == 3 and all(isinstance(x, str) for x in m)]
    number = (lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
    return {"time": float(data["time"]), "started": number(data.get("started")), "seconds": number(data.get("seconds")),
            "outcome": data["outcome"], "code": str(data.get("code") or ""), "category": str(data.get("category") or ""),
            "game_version": str(data.get("game_version") or ""), "bisect": bool(data.get("bisect")),
            "record": str(data.get("record") or ""), "where": str(data.get("where") or ""), "mods": mods}


def _launch_rows(crash_dir) -> list[dict]:
    data = fsutil.read_json(Path(crash_dir) / LAUNCHES_FILE, None)
    if not isinstance(data, dict) or data.get("format") != LAUNCHES_FORMAT or not isinstance(data.get("rows"), list):
        return []
    return [r for r in map(_row, data["rows"]) if r is not None]


def record_launch(crash_dir, detection: Detection, context: dict | None = None, bisect: bool = False,
                  now: float | None = None) -> bool:
    """Add one watched run to launches.json: how it ended ("crash", "normal" or "unknown"), when, how long it ran
    and the mods it ran with (those of the launch from the manager it came from, else ``context``). ``bisect``:
    a step of 원인 모드 찾기 (not the user's own setup). Keeps the last MAX_LAUNCHES of LAUNCH_DAYS. Returns
    whether it was written; never raises."""
    try:
        exit_ = detection.exit
        if exit_ is None:
            return False
        launched = launch_context(crash_dir, exit_.started or exit_.seen)
        ctx = launched if isinstance(launched, dict) else context
        mods = launch_mods(ctx)
        if mods is None:
            return False
        record = detection.record if detection.record is not None else detection.session
        code = detection.code
        if not code and exit_.code is not None and (not detection.crashed or is_crash_code(exit_.code)):
            code = crashlog.normalize_code(exit_.code)  # (not the 0 of a crash the game reported itself)
        row = {"time": float(exit_.ended), "started": exit_.started if exit_.started is not None else exit_.seen,
               "seconds": exit_.seconds,
               "outcome": "crash" if detection.crashed else "normal" if detection.normal else "unknown",
               "code": code, "category": str(detection.record.summary.get("category") or "")
               if detection.record is not None else "", "game_version": str(ctx.get("game_version") or ""),
               "bisect": bool(bisect or ctx.get("bisect")), "record": record.id if record is not None else "",
               "where": crashes.crash_where(detection.record) if detection.record is not None else "", "mods": mods}
        now = time.time() if now is None else now
        with _launches_lock:
            rows = [r for r in _launch_rows(crash_dir) if not (abs(r["time"] - row["time"]) < 1
                                                               and r["started"] == row["started"])]
            rows.append(row)
            rows = sorted((r for r in rows if now - r["time"] <= LAUNCH_DAYS * 86400), key=lambda r: r["time"])
            fsutil.atomic_write_json(Path(crash_dir) / LAUNCHES_FILE,
                                     {"format": LAUNCHES_FORMAT, "rows": rows[-MAX_LAUNCHES:]})
        return True
    except Exception as exc:  # only a help for later searches
        log.warning("게임 실행 기록을 저장하지 못했습니다: %s", exc)
        return False


def load_launches(crash_dir, game_version: str = "", now: float | None = None) -> list[dict]:
    """The runs in launches.json of the last LAUNCH_DAYS, the newest first (of ``game_version`` when given), as
    the crash records kept now tell them (_reconciled)."""
    now = time.time() if now is None else now
    rows = [r for r in _launch_rows(crash_dir) if now - r["time"] <= LAUNCH_DAYS * 86400
            and (not game_version or r["game_version"] == game_version)]
    return sorted(_reconciled(crash_dir, rows), key=lambda r: r["time"], reverse=True)


def _reconciled(crash_dir, rows: list[dict]) -> list[dict]:
    """Rows of runs whose crash was recorded only after the row was written - the game's own crash report, collected
    later, of a run that ended with exit code 0 (as _crashed_during for the last normal exit), or one still being
    written then (a crash without its code): crashes, with that record's code, category and place."""
    pending = {id(r) for r in rows if r["outcome"] != "crash" or crashlog.normalize_code(r["code"]) in ("", "00000000")}
    if not pending:
        return rows
    try:
        kept = [s for s in crashes.sessions(crash_dir) if s.problem() == "crash"]
    except Exception as exc:  # (the rows as written)
        log.debug("게임 기록을 읽지 못했습니다: %s", exc)
        return rows
    if not kept:
        return rows

    def crashed_at(s) -> float | None:
        t = s.summary.get("crash_time")
        return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) and t else None

    out = []
    for r in rows:
        start = r["started"] if r["started"] is not None else r["time"] - r["seconds"] if r["seconds"] is not None \
            else None
        found = None if id(r) not in pending else next(
            (s for s in kept if (r["record"] and s.id == r["record"])
             or (start is not None and (at := crashed_at(s)) is not None and start - 5 <= at <= r["time"] + 5)), None)
        if found is None:
            out.append(r)
            continue
        try:
            where = crashes.crash_where(found)
        except Exception as exc:
            log.debug("튕긴 곳을 읽지 못했습니다 (%s): %s", found.id, exc)
            where = ""
        out.append({**r, "outcome": "crash", "code": crashlog.normalize_code(found.summary.get("crash") or ""),
                    "category": str(found.summary.get("category") or ""), "where": where, "record": found.id})
    return out
