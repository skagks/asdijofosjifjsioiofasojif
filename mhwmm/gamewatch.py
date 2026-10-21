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
with are kept as the last good set (``crashes/last_good.json``) for 원인 모드 찾기.

Everything Windows-only is guarded: on other systems (or when an API is missing) nothing is
watched, and no API failure reaches the caller.
"""

from __future__ import annotations

import csv
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


def judge(exit: GameExit | None, record: crashes.Session | None = None) -> Detection:
    """Detection from an exit (None: not watched) and a crash record since the launch."""
    code = ""
    if exit is not None and exit.crashed:
        code = crashlog.normalize_code(exit.code)
    elif record is not None:
        code = crashlog.normalize_code(record.summary.get("crash") or "")
    if (exit is not None and exit.crashed) or record is not None:
        crashed = True
    elif exit is not None and exit.code == 0:
        crashed = False
    else:
        crashed = None  # killed, frozen and closed, an unknown code, or not watched: ask
    return Detection(crashed, code, exit, record)


def detect(crash_dir, since: float | None, exit: GameExit | None = None, game_dir=None, context: dict | None = None,
           appdata=None) -> Detection:
    """Collect REFramework's records of the session (with ``game_dir``) and judge how it ended.
    ``since``: when the game was launched (records ending after it count). Reads files: run it
    off the GUI thread."""
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
    if start is not None:
        try:
            record = crashes.crashed_since(crash_dir, start - 5)
        except Exception as exc:
            log.warning("게임 기록을 읽지 못했습니다: %s", exc)
    return judge(exit, record)


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
    return data


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
    return detection
