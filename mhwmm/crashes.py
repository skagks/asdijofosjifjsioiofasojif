"""The manager's record of game sessions ("게임 튕김 기록").

REFramework's log is overwritten on the next game start (builds since 2026-10-11 keep one previous
session), so its records are copied out of the game folder - when the manager starts and right
before it starts the game - into ``<data>/crashes/<YYYYMMDD-HHMMSS>/``, one folder per session:

- ``re2_framework_log.txt`` (from the log or the .prev log), ``reframework_faulty_files.txt``,
  ``reframework_crash.dmp`` (matched to the session by its time) and ``re2_fw_config.txt``;
- ``reframework_accessed_files.txt.gz``: REFramework's record of every file the game asked for, when
  LooseFileLoader_LogAccessedFiles is on (compressed; see accessed.py);
- ``session.json``: where they came from, a short summary, and the manager's state (profile, mods)
  at the launch the session came from (or when it was collected).

Only copies are made: nothing in the game folder is moved or deleted.  The last ``KEEP_SESSIONS``
are kept (sessions without a problem go first).  :func:`explain` matches a session's evidence to
the mods as they were in it (on or off, the files the manager had placed), always as a suspicion
with the line it rests on: a crash location names a mod only for a plugin or a DLL the manager
placed, game files REFramework could not load name the mods that ship them.  :func:`write_report` makes the zip for asking for help (user names masked, never
state.json, which holds the Nexus API key).
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import struct
import threading
import time
import zipfile
import zlib
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from . import crashlog, fsutil
from .constants import PAK_MODS_DIR, REFRAMEWORK_DLL, STEAM_APP_ID
from .crashlog import (CONFIG_FILE, DUMP_FILE, FAULTY_FILE, LOG_FILE, PREV_LOG_FILE, Crash, FaultyFile, LogInfo,
                       exception_label, game_relative, mask_user, module_name, module_place, norm_path, parse_time,
                       refw_label, resource_key, split_version)

log = logging.getLogger(__name__)

CRASH_DIR = "crashes"  # in the data folder (not cache\: these cannot be made again)
SESSION_FILE = "session.json"
LAUNCH_FILE = "launch.json"
NOTICE_FILE = "notice.json"
PRUNED_FILE = "pruned.json"  # records already let go, so they are not copied again while still in the game folder
ACCESSED_COPY = crashlog.ACCESSED_FILE + ".gz"  # REFramework's record of the files the game read, compressed
ACCESSED_TAIL = "reframework_accessed_files_tail.txt"  # its last lines in the support zip
REPORT_ACCESSED_LINES = 300
SESSION_FORMAT = 1
KEEP_SESSIONS = 10
KEEP_RECENT = 3  # the newest ones are kept even without a problem
NOTICE_DAYS = 3  # older records are listed but not announced at startup
DUMP_SECONDS = 180  # a dump written this close to a session's crash (or end) belongs to it
DUMP_SHIFT_SECONDS = 30  # ... or this close to it plus whole quarter hours (the time zone changed since)
LAUNCH_SECONDS = 15 * 60  # a session starting this soon after a launch from the manager came from it
MAX_LOG_COPY = 64 * 1024 * 1024  # a larger log is copied in part (its start and its end)
MAX_DUMP_COPY = 512 * 1024 * 1024
REPORT_LOG_LINES = 2000
GAME_REPORT_FILE = "game_crashreport.txt"  # the text of the game's own crash report (CrashReport.txt)
GAME_REPORT_SECONDS = 15 * 60  # a game crash report this soon after a session's last log line belongs to it
MAX_GAME_REPORTS = 20  # the newest reports in <game>\CrashReport looked at
MAX_GAME_REPORT_TEXT = 4 * 1024 * 1024
DUMP_HEAD = 64 * 1024  # a minidump's first bytes read for its times (header, stream directory, MiscInfoStream)
GAME_REPORT_DAYS = 14  # an older report gets no session of its own (only a kept session it was written in takes it)
REPORT_LAUNCH_SECONDS = 12 * 3600  # a report-only session this soon after an unclaimed launch from the manager came from it
SAME_REPORT_SECONDS = 120  # reports this close together are the same crash (a copy, or an unpacked zip)
_REPORT_NAME = re.compile(r"^(\d{4})_(\d{2})(\d{2})_(\d{2})(\d{2})(?!\d)")  # 2026_1022_0346.zip: the crash's minute
MAX_FAULTY_MATCH = 5000  # faulty entries looked for in paks and the game folder (the rest only in the mods' files)
PAK_FAMILY = "re_chunk_000.pak.sub_000.pak"  # the paks REFramework adds pak_mods' paks to as numbered patches
RSZ_EXTS = (".user", ".pfb", ".scn", ".mdf2")  # files the material check and repair (재질 점검·복구) handle
# version suffixes when neither the mods nor the bundled path list know one
FALLBACK_VERSIONS = {"user": ("3",), "pfb": ("18",), "scn": ("21",), "mdf2": ("45",), "tex": ("241106027",),
                     "mesh": ("241111606",)}
TEXT_FILES = (LOG_FILE, FAULTY_FILE, CONFIG_FILE, SESSION_FILE, GAME_REPORT_FILE)

TITLE = "게임 튕김 기록"  # the window and the ☰ menu item
SUSPECTS_HEADER = "의심되는 원인"
REPORT_NOTE = ("모드 목록과 게임·REFramework 기록이 들어 있습니다. 사용자 이름은 가리고, "
               "Nexus API 키는 넣지 않습니다.")
DUMP_CHECKBOX = "크래시 덤프 포함 (메모리 일부가 들어 있어 사용자 이름을 가리지 못함)"
LUA_LOG_CHECKBOX = "Lua 스크립트 오류도 기록하기 (REFramework 설정 변경, 게임이 꺼져 있을 때만)"
NO_RECORD = "REFramework 기록이 없습니다. 오류 창이 떴다면 문구를 붙여 넣어 주세요."
NOTICE_CRASH = "지난번 게임이 비정상적으로 종료된 기록이 있습니다."
NOTICE_FAULTY = "지난번 게임에서 REFramework가 읽지 못한 파일이 있었습니다."

_lock = threading.Lock()


# ---- where REFramework writes ------------------------------------------------------------------
def appdata_dir(appdata=None) -> Path | None:
    """``%APPDATA%\\REFramework\\MonsterHunterWilds``: REFramework's folder when the game folder is not
    writable (``appdata`` replaces %APPDATA%; "" for none)."""
    base = os.environ.get("APPDATA", "") if appdata is None else appdata
    return Path(base, *crashlog.APPDATA_SUBDIR) if base else None


def evidence_dirs(game_dir, appdata=None) -> list[Path]:
    """The folders REFramework may have written its files to (existing ones only)."""
    out: list[Path] = []
    for d in (Path(game_dir) if game_dir else None, appdata_dir(appdata)):
        if d is not None and d.is_dir() and not any(fsutil.same_path(d, o) for o in out):
            out.append(d)
    return out


def config_path(game_dir, appdata=None, fallback: bool | None = None) -> Path | None:
    """REFramework's config file in use. REFramework picks its folder at every start by trying to write
    to the game folder, so ``fallback`` (its last log said it used %APPDATA%: True, the game folder:
    False) names that folder's file even before it exists; without it (None: not known) the game
    folder's file, else %APPDATA%'s, else the game folder's (made there)."""
    if fallback is not None and game_dir:
        folder = appdata_dir(appdata) if fallback else Path(game_dir)
        if folder is not None:
            return folder / CONFIG_FILE
    for d in evidence_dirs(game_dir, appdata):
        if (d / CONFIG_FILE).is_file():
            return d / CONFIG_FILE
    return Path(game_dir) / CONFIG_FILE if game_dir else None


def config_paths(game_dir, appdata=None) -> list[Path]:
    """The places REFramework's config file can be (the game folder's, %APPDATA%'s)."""
    folders = [Path(game_dir) if game_dir else None, appdata_dir(appdata)]
    return [d / CONFIG_FILE for d in folders if d is not None]


def refw_option(game_dir, key: str, appdata=None) -> str | None:
    path = config_path(game_dir, appdata)
    try:
        text = path.read_text(encoding="utf-8", errors="replace") if path else ""
    except OSError:
        return None
    return crashlog.parse_config(text).get(key)


def lua_logging(game_dir, appdata=None) -> bool:
    """REFramework writes Lua script errors to its log (ScriptRunner_LogToDisk; off unless set)."""
    return crashlog.config_bool(refw_option(game_dir, crashlog.LUA_LOG_KEY, appdata)) is True


def set_refw_option(game_dir, key: str, value, appdata=None, path=None) -> bool:
    """Change one REFramework option (see crashlog.set_config_value) in ``path`` (one of
    config_paths; by default config_path's). The caller makes sure the game is not running and the
    user agreed. Returns False when it already had that value."""
    path = Path(path) if path else config_path(game_dir, appdata)
    if path is None or (not path.is_file() and not (Path(game_dir) / REFRAMEWORK_DLL).is_file()):
        raise ValueError("REFramework가 설치되어 있지 않습니다.")
    changed = crashlog.set_config_value(path, key, value)
    if changed:
        log.info("REFramework 설정을 바꿨습니다: %s=%s (%s)", key, value, path)
    return changed


def dump_time(path) -> float | None:
    """When a minidump was written: its header's time stamp, else the file's mtime."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
        if len(head) >= 24 and head[:4] == b"MDMP":
            stamp = struct.unpack_from("<I", head, 20)[0]
            if stamp:
                return float(stamp)
        return os.stat(path).st_mtime
    except OSError:
        return None


def _steam_state(game_dir) -> dict:
    """``buildid`` and ``LastUpdated`` (as written) of the game in Steam's app manifest ({} if not found)."""
    if not game_dir:
        return {}
    from . import steam

    acf = Path(game_dir).parent.parent / f"appmanifest_{STEAM_APP_ID}.acf"
    try:
        state = steam._get(steam.parse_vdf(acf.read_text(encoding="utf-8", errors="replace")), "AppState") or {}
    except OSError:
        return {}
    out = {}
    for key in ("buildid", "LastUpdated"):
        value = steam._get(state, key)
        if isinstance(value, str) and value:
            out[key] = value
    return out


def steam_info(game_dir) -> dict:
    """``buildid`` and ``LastUpdated`` (local time) of the game in Steam's app manifest ({} if not found)."""
    out = _steam_state(game_dir)
    if out.get("LastUpdated", "").isdigit():
        out["LastUpdated"] = datetime.fromtimestamp(int(out["LastUpdated"])).strftime("%Y-%m-%d %H:%M")
    return out


def steam_updated(game_dir) -> float | None:
    """When Steam last updated the game (its app manifest's ``LastUpdated``; None if not known)."""
    value = _steam_state(game_dir).get("LastUpdated", "")
    return float(value) if value.isdigit() and int(value) > 0 else None


# ---- sessions ------------------------------------------------------------------------------------
def _epoch(stamp: str) -> float | None:
    t = parse_time(stamp) if stamp else None
    return t.timestamp() if t else None


def epoch(stamp: str) -> float | None:
    """A log time stamp (local time, as REFramework writes it) as epoch seconds (None if it is not one)."""
    return _epoch(stamp)


class Session:
    """One game session the manager keeps the records of (a folder in crashes\\)."""

    def __init__(self, folder: Path, info: dict):
        self.folder = Path(folder)
        self.info = info
        self._log: LogInfo | None = None
        self._faulty: list[FaultyFile] | None = None
        self._accessed = None  # accessed.AccessLog, read once

    def __repr__(self) -> str:
        return f"Session({self.id!r})"

    @property
    def id(self) -> str:
        return self.folder.name

    @property
    def started(self) -> str:
        """First log time stamp (local time, as REFramework writes it); "" for a dump without a log."""
        return self.info.get("started", "")

    @property
    def ended(self) -> str:
        return self.info.get("ended", "")

    @property
    def time(self) -> float:
        """Start (or for a dump alone the dump's time), for ordering."""
        return float(self.info.get("time") or 0)

    @property
    def end_time(self) -> float:
        return float(self.info.get("end_time") or self.time)

    @property
    def summary(self) -> dict:
        s = self.info.get("summary")
        return s if isinstance(s, dict) else {}

    @property
    def context(self) -> dict:
        """The manager's state for this session: {"app", "game_version", "profile", "mods": [{"id", "name",
        "enabled", "stamp"}]} as given to note_launch (``launched`` set) or collect."""
        c = self.info.get("context")
        return c if isinstance(c, dict) else {}

    @property
    def launched(self) -> float | None:
        """When the manager launched the game for this session (None: started some other way)."""
        t = self.info.get("launched")
        return float(t) if isinstance(t, (int, float)) and t else None

    def file(self, name: str) -> Path | None:
        path = self.folder / name
        return path if name in (self.info.get("files") or {}) and path.is_file() else None

    @property
    def dump_path(self) -> Path | None:
        return self.file(DUMP_FILE)

    @property
    def dump_only(self) -> bool:
        return not self.started and self.dump_path is not None

    def log(self) -> LogInfo | None:
        if self._log is None:
            path = self.file(LOG_FILE)
            if path is None:
                return None
            try:
                self._log = crashlog.read_log(path)
            except OSError as exc:
                log.warning("게임 기록을 읽지 못했습니다 (%s): %s", self.id, exc)
                return None
        return self._log

    def faulty(self) -> list[FaultyFile]:
        if self._faulty is None:
            path = self.file(FAULTY_FILE)
            try:
                self._faulty = crashlog.parse_faulty(path.read_text(encoding="utf-8", errors="replace")) if path else []
            except OSError:
                self._faulty = []
        return self._faulty

    def accessed(self):
        """REFramework's record of the files the game asked for in this session (accessed.AccessLog, from its
        copy; None without one). Reads the whole record: off the GUI thread."""
        if self._accessed is None:
            path = self.file(ACCESSED_COPY)
            if path is None:
                return None
            from . import accessed

            found = accessed.read(path)
            record = self.info["files"].get(ACCESSED_COPY)
            record = record if isinstance(record, dict) else {}
            found.truncated = found.truncated or bool(record.get("truncated"))
            # copied while the game still ran, and the game was started again (from Steam) before the manager
            # could copy the rest: the session ended (or crashed) after its copy was made
            copied = record.get("copied")
            crashed = self.summary.get("crash_time")
            crashed = float(crashed) if isinstance(crashed, (int, float)) and not isinstance(crashed, bool) and crashed \
                else None
            ends = [t for t in (self.end_time, crashed) if t]
            if isinstance(copied, (int, float)) and ends and max(ends) > copied + accessed.COPY_SLACK:
                found.partial = True
            # (or, whatever the clock said: nothing new asked for in the minutes before a crash - the game asks for
            # files all the time while it runs - the end is missing)
            if crashed is not None and found.last_time is not None and crashed - found.last_time > accessed.PARTIAL_GAP:
                found.partial = True
            self._accessed = found
        return self._accessed

    @property
    def crash(self) -> Crash | None:
        info = self.log()
        return info.crash if info else None

    def game_report(self) -> crashlog.GameReport | None:
        """The game's own crash report of this session (CrashReport.exe), if one was found."""
        path = self.file(GAME_REPORT_FILE)
        try:
            return crashlog.parse_game_report(path.read_text(encoding="utf-8", errors="replace")) if path else None
        except OSError:
            return None

    def problem(self) -> str:
        """"crash" (REFramework recorded a fatal exception), "dump" (only a crash dump is left), "faulty"
        (files the game could not load) or "" - from the summary, without reading the files."""
        s = self.summary
        if s.get("crash") or s.get("game_report"):
            return "crash"
        if self.dump_only:
            return "dump"
        if s.get("faulty"):
            return "faulty"
        return ""

    def title(self, now: float | None = None, running: bool = False) -> str:
        """One line for lists: ``10월 16일 15:48 · 오류 코드 0xC0000005 (메모리 접근 오류)`` (``running``: the
        session of the game running now, not finished)."""
        s = self.summary
        parts = [_when(s.get("crash_time") or self.end_time, now)]
        if s.get("device_error"):
            parts.append(f"그래픽 오류 ({s['device_error']})")
        elif s.get("crash"):
            parts.append(f"오류 코드 {exception_label(s['crash'])}")
        elif s.get("game_report"):
            parts.append("게임 오류 보고서")
        elif self.dump_only:
            parts.append("크래시 덤프만 남음")
        else:
            parts.append("게임 실행 중" if running else "오류 기록 없음")
        if s.get("faulty"):
            parts.append(f"읽지 못한 파일 {s['faulty']}개")
        return " · ".join(parts)


def session_loose(s: Session | None) -> bool | None:
    """Whether REFramework's loose file loading (LooseFileLoader) was on in the session: its settings
    file as copied with the session (REFramework writes it while it runs; no line: off, its default).
    None: not copied."""
    path = s.file(CONFIG_FILE) if s is not None else None
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return crashlog.parse_config(text).get(crashlog.LOOSE_FILES_KEY) == "true"  # (REFramework takes only "true")


def _when(epoch: float | None, now: float | None = None) -> str:
    if not epoch:
        return "시간 모름"
    t = datetime.fromtimestamp(epoch)
    today = datetime.fromtimestamp(now if now is not None else time.time()).date()
    return t.strftime("%H:%M") if t.date() == today else f"{t.month}월 {t.day}일 {t:%H:%M}"


def sessions(crash_dir) -> list[Session]:
    """Every kept session, the newest first."""
    out = []
    root = Path(crash_dir)
    for d in root.iterdir() if root.is_dir() else ():
        if not d.is_dir():
            continue
        info = fsutil.read_json(d / SESSION_FILE, None)
        if isinstance(info, dict) and isinstance(info.get("files"), dict):
            out.append(Session(d, info))
    return sorted(out, key=lambda s: (s.time, s.id), reverse=True)


def find_session(crash_dir, ref: str) -> Session | None:
    """By list number (1 = the newest) or folder name (a unique start of it is enough)."""
    found = sessions(crash_dir)
    ref = str(ref).strip()
    if ref.isdigit() and 0 < int(ref) <= len(found) and len(ref) < 4:
        return found[int(ref) - 1]
    exact = [s for s in found if s.id == ref]
    if exact:
        return exact[0]
    starts = [s for s in found if s.id.startswith(ref)]
    return starts[0] if len(starts) == 1 else None


def live_started(game_dir, appdata=None) -> str:
    """First time stamp of the log REFramework is writing now (the newest one): call it while the game
    runs to know which kept session is not finished yet."""
    logs = [(st.st_mtime, d / LOG_FILE) for d in evidence_dirs(game_dir, appdata) if (st := _stat(d / LOG_FILE))]
    return crashlog.first_time(max(logs)[1]) if logs else ""


def last_fatal(crash_dir) -> Session | None:
    """The newest session that ended in a crash (a fatal block, or a dump alone)."""
    return next((s for s in sessions(crash_dir) if s.problem() in ("crash", "dump")), None)


def crash_where(session: Session | None) -> str:
    """Where a session crashed, as exactly as its records say (to tell one crash from another of the same code):
    "<module>+0x<offset>" from REFramework's handler, else the module's file name (the game's own report, or a
    handler that wrote no address), lower case; "" when not known (or code in no module)."""
    if session is None:
        return ""
    info = session.log()
    crash = info.crash if info is not None else None
    if crash is not None:
        name = module_name(crash.place).lower() if crash.place else ""
        if not name:
            return ""
        try:
            offset = int(crash.rip, 16) - int(crash.module_base, 16) if crash.rip and crash.module_base else None
        except ValueError:
            offset = None
        return f"{name}+0x{offset:x}" if offset is not None and offset >= 0 else name
    name = str(session.summary.get("module_name") or "")
    return name.lower()


def crashed_since(crash_dir, since: float, first: bool = False, until: float | None = None) -> Session | None:
    """A crash recorded after ``since`` (epoch seconds), e.g. a launch: the newest (``first``: the oldest - the
    launch's own run when the game may have been started again since; ``until``: of a run that began before the
    game ended then). Run :func:`collect` first."""
    found = [s for s in sessions(crash_dir) if s.problem() in ("crash", "dump")
             and max(s.end_time, float(s.summary.get("crash_time") or 0)) >= since
             and (until is None or s.time <= until + 5)]
    return (found[-1] if first else found[0]) if found else None


def session_since(crash_dir, since: float, until: float | None = None, first: bool = False) -> Session | None:
    """The record of a game run that started at ``since`` (epoch; e.g. a launch), crash or not: the newest
    session starting after it (and, with ``until``, not after the run ended; ``first``: the oldest - the run the
    launch started, when the game may have been started again since). Run :func:`collect` first."""
    found = [s for s in sessions(crash_dir) if s.time >= since - 5 and (until is None or s.time <= until + 5)]
    return (found[-1] if first else found[0]) if found else None


# ---- collecting ------------------------------------------------------------------------------------
def note_launch(crash_dir, context: dict | None = None, now: float | None = None) -> None:
    """Remember that the manager is starting the game (and with which mods): the session it starts gets
    this context when it is collected."""
    try:
        fsutil.atomic_write_json(Path(crash_dir) / LAUNCH_FILE,
                                 {"time": now if now is not None else time.time(), "context": context or {}})
    except OSError as exc:
        log.warning("게임 실행 기록을 저장하지 못했습니다: %s", exc)


def collect(crash_dir, game_dir, context: dict | None = None, appdata=None, keep: int = KEEP_SESSIONS) -> list[Session]:
    """Copy REFramework's records of sessions not kept yet (or that grew since) into ``crash_dir``.
    ``context`` is the manager's state now, kept for sessions the manager did not launch. Returns the
    new or updated sessions, the newest first. Never changes anything in the game folder; a file that
    cannot be read is skipped."""
    if not game_dir:
        return []
    with _lock:
        changed = _collect(Path(crash_dir), game_dir, context or {}, appdata)
        if changed:
            prune(crash_dir, keep)
    for s in changed:
        log.info("게임 기록을 모았습니다: %s · %s%s", s.id, s.title(),
                 f" · {s.summary['module_name']}" if s.summary.get("module_name") else "")
    return [s for s in changed if s.folder.is_dir()]


def _stat(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return st if st.st_size >= 0 and path.is_file() else None


def _same(record, st) -> bool:
    return isinstance(record, dict) and record.get("size") == st.st_size and record.get("mtime") == st.st_mtime_ns


def _copy(src: Path, dst: Path, limit: int | None = None) -> None:
    size = src.stat().st_size
    if limit is None or size <= limit:
        fsutil.copy_file(src, dst)
        return
    text = crashlog.read_text(src, limit)  # the start and the end
    tmp = dst.with_name(dst.name + fsutil.PART_SUFFIX)
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, dst)


def _new_folder(crash_dir: Path, epoch: float, suffix: str = "") -> Path:
    base = datetime.fromtimestamp(epoch).strftime("%Y%m%d-%H%M%S") + suffix
    folder, n = crash_dir / base, 2
    while folder.exists():
        folder, n = crash_dir / f"{base}-{n}", n + 1
    folder.mkdir(parents=True)
    return folder


def _launch_for(crash_dir: Path, started: float | None) -> dict | None:
    """The launch from the manager a session starting at ``started`` came from (each launch starts one)."""
    data = fsutil.read_json(crash_dir / LAUNCH_FILE, None)
    if not isinstance(data, dict) or not isinstance(data.get("time"), (int, float)) or started is None:
        return None
    if data.get("claimed"):
        return None
    return data if -60 <= started - data["time"] <= LAUNCH_SECONDS else None


def _claim_launch(crash_dir: Path, launch: dict, session_id: str) -> None:
    try:
        fsutil.atomic_write_json(crash_dir / LAUNCH_FILE, {**launch, "claimed": session_id})
    except OSError as exc:
        log.warning("게임 실행 기록을 저장하지 못했습니다: %s", exc)


def _copy_log(src: Path, dst: Path, start: str) -> bool:
    """Copy a log only while it is still the one of the session starting at ``start`` (False: the game
    started meanwhile and REFramework began a new log under that name)."""
    tmp = dst.with_name(dst.name + ".new")
    _copy(src, tmp, MAX_LOG_COPY)
    if crashlog.first_time(tmp) != start:
        fsutil.remove_file(tmp)
        return False
    fsutil.replace_file(tmp, dst)
    return True


def _accessed_owner(path: Path, candidates: Iterable[Session]) -> tuple[Session | None, bytes]:
    """The kept session a record of the files the game read was written in, and the record's first line (None:
    no such session, or the record holds no path: logging was off)."""
    from . import accessed

    head = accessed.read_head(path)
    if head is None or not head[1].logging:
        return None, b""
    found = [s for s in candidates if s.started and accessed.belongs(head[1], s.time)]
    return max(found, key=lambda s: s.time, default=None), head[0]


def _copy_accessed(src: Path, dst: Path, first_line: bytes) -> tuple[int | None, bool]:
    """Copy the record gzip-compressed: (lines, whether only its first line and its last MAX_LOG_COPY bytes were
    kept); (None, False) when the game started a new record meanwhile (its first line is not ``first_line``)."""
    size = src.stat().st_size
    truncated = size > MAX_LOG_COPY
    tmp = dst.with_name(dst.name + fsutil.PART_SUFFIX)

    def first_of(f) -> bytes:
        return f.readline(64 * 1024).rstrip(b"\r\n")

    same, lines = False, 0
    try:
        with open(src, "rb") as f, gzip.open(tmp, "wb", compresslevel=6) as out:
            first = first_of(f)
            if first == first_line:
                out.write(first + b"\n")
                lines = 1
                if truncated:
                    f.seek(size - MAX_LOG_COPY)
                    f.readline()  # (a cut line)
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    out.write(chunk)
                    lines += chunk.count(b"\n")
                with open(src, "rb") as again:  # (a new start truncates the file and begins it again)
                    same = first_of(again) == first_line
        if not same:
            fsutil.remove_file(tmp)
            return None, False
        os.replace(tmp, dst)
    except BaseException:
        fsutil.remove_file(tmp)
        raise
    return lines, truncated


def _collect(crash_dir: Path, game_dir, context: dict, appdata) -> list[Session]:
    dirs = evidence_dirs(game_dir, appdata)
    if not dirs:
        return []
    known = sessions(crash_dir)
    by_start = {s.started: s for s in reversed(known) if s.started}
    pruned = set(_pruned(crash_dir))
    changed: dict[str, Session] = {}

    # logs: one session per first time stamp (the .prev log is the session before)
    logs: dict[str, tuple[Path, os.stat_result, Path]] = {}
    for d in dirs:
        for name in (LOG_FILE, PREV_LOG_FILE):
            path = d / name
            st = _stat(path)
            start = crashlog.first_time(path) if st else ""
            if start and (start not in logs or st.st_size > logs[start][1].st_size):
                logs[start] = (path, st, d)
    for start, (path, st, d) in sorted(logs.items()):
        s = by_start.get(start)
        if s is None and start in pruned or s is not None and _same(s.info["files"].get(LOG_FILE), st):
            continue
        made, launch = s is None, None
        try:
            if s is None:
                epoch = _epoch(start) or st.st_mtime
                s = Session(_new_folder(crash_dir, epoch), {"format": SESSION_FORMAT, "files": {}, "started": start,
                                                             "time": epoch, "notified": ""})
                launch = _launch_for(crash_dir, epoch)
                s.info["context"] = (launch or {}).get("context") or context
                s.info["context_from"] = "launch" if launch else "collect"
                s.info["launched"] = launch["time"] if launch else 0
            copied = _copy_log(path, s.folder / LOG_FILE, start)
            if not copied:
                log.info("게임이 새 기록을 시작해 다음에 다시 모읍니다: %s", path)
        except OSError as exc:
            log.warning("게임 기록을 복사하지 못했습니다 (%s): %s", path, exc)
            copied = False
        if not copied:
            if made and s is not None:
                fsutil.rmtree(s.folder)
            continue  # (a log that became the next session's is copied as .prev.txt next time)
        if launch is not None:
            _claim_launch(crash_dir, launch, s.id)
        s.info["files"][LOG_FILE] = {"from": str(path), "size": st.st_size, "mtime": st.st_mtime_ns}
        s.info["source"] = str(d)
        s._log = None
        _summarize(s)
        by_start[start] = s
        changed[s.id] = s
    everything = {s.id: s for s in [*known, *changed.values()]}

    # the faulty files list belongs to the session that was running when it was last written: the
    # current log's (REFramework empties the list at every start), or an older one that ended after it
    for d in dirs:
        path = d / FAULTY_FILE
        st = _stat(path)
        if st is None:
            continue
        current = by_start.get(crashlog.first_time(d / LOG_FILE))
        if current is not None and current.time <= st.st_mtime + 5:
            s = current
        else:
            owners = [x for x in everything.values() if x.started and x.time <= st.st_mtime + 5
                      and st.st_mtime <= x.end_time + DUMP_SECONDS]
            s = max(owners, key=lambda x: x.time, default=None)
        if s is None or _same(s.info["files"].get(FAULTY_FILE), st):
            continue
        try:
            fsutil.copy_file(path, s.folder / FAULTY_FILE)
        except OSError as exc:
            log.warning("읽지 못한 파일 목록을 복사하지 못했습니다 (%s): %s", path, exc)
            continue
        s.info["files"][FAULTY_FILE] = {"from": str(path), "size": st.st_size, "mtime": st.st_mtime_ns}
        s._faulty = None
        changed[s.id] = s

    # REFramework's record of the files the game asked for (LooseFileLoader_LogAccessedFiles): truncated at
    # every start, it belongs to the session REFramework began just before writing its first line; kept
    # compressed, and only when it holds paths (with logging off it has the first line alone)
    for d in dirs:
        path = d / crashlog.ACCESSED_FILE
        st = _stat(path)
        if st is None or any(_same(x.info["files"].get(ACCESSED_COPY), st) for x in everything.values()):
            continue
        s, first_line = _accessed_owner(path, everything.values())
        if s is None:
            continue
        try:
            lines, truncated = _copy_accessed(path, s.folder / ACCESSED_COPY, first_line)
        except OSError as exc:
            log.warning("게임이 읽은 파일 기록을 복사하지 못했습니다 (%s): %s", path, exc)
            continue
        if lines is None:
            log.info("게임이 새 파일 기록을 시작해 다음에 다시 모읍니다: %s", path)
            continue
        s.info["files"][ACCESSED_COPY] = {"from": str(path), "size": st.st_size, "mtime": st.st_mtime_ns,
                                          "lines": lines, "truncated": truncated, "copied": time.time()}
        s._accessed = None
        changed[s.id] = s

    # the crash dump goes with the session whose crash it records; a dump left alone (the log of its
    # session was overwritten by a later start) gets a session of its own
    for d in dirs:
        path = d / DUMP_FILE
        st = _stat(path)
        if st is None or st.st_size > MAX_DUMP_COPY:
            continue
        if _dump_key(st) in pruned or any(_same(s.info["files"].get(DUMP_FILE), st) for s in everything.values()):
            continue
        written = dump_time(path) or st.st_mtime
        s = _dump_owner(everything.values(), written)
        made = s is None
        try:
            if s is None:
                s = Session(_new_folder(crash_dir, written, "-dump"),
                            {"format": SESSION_FORMAT, "files": {}, "started": "", "time": written, "end_time": written,
                             "ended": datetime.fromtimestamp(written).strftime(crashlog.TIME_FORMAT)[:-3],
                             "context": context, "context_from": "collect", "launched": 0, "notified": "",
                             "source": str(d)})
            fsutil.copy_file(path, s.folder / DUMP_FILE)
        except OSError as exc:
            log.warning("크래시 덤프를 복사하지 못했습니다 (%s): %s", path, exc)
            if made and s is not None:
                fsutil.rmtree(s.folder)
            continue
        everything[s.id] = s
        s.info["files"][DUMP_FILE] = {"from": str(path), "size": st.st_size, "mtime": st.st_mtime_ns, "time": written}
        changed[s.id] = s

    # the game's own crash reports (CrashReport.exe; e.g. a graphics device error that REFramework never sees):
    # only their text is copied, to the session that was running then (or a session of its own)
    cutoff = time.time() - GAME_REPORT_DAYS * 86400
    oldest = min((s.time for s in everything.values()), default=cutoff)
    for path, st in _game_reports(game_dir):
        key = _report_key(path)
        if key in pruned or any(_report_record_key(s) == key for s in everything.values()):
            continue
        if st.st_mtime < min(cutoff, oldest):  # older than any kept session and too old for one of its own
            continue
        try:
            text, written, started, time_from = _read_game_report(path, st)
        except Exception as exc:  # one report never stops the others (nor the records above) being kept
            log.info("게임 오류 보고서를 읽지 못했습니다 (%s): %s", path, exc)
            continue
        if text is None or any(abs(_report_record_time(s) - written) <= SAME_REPORT_SECONDS
                               for s in everything.values() if _report_record_key(s)):
            continue
        launch_data = fsutil.read_json(crash_dir / LAUNCH_FILE, None)
        launched = launch_data.get("time") if isinstance(launch_data, dict) else None
        s = _report_owner(everything.values(), written, started,
                          launched if isinstance(launched, (int, float)) else None)
        if s is None and written < cutoff:
            continue
        made, launch = s is None, None
        try:
            if s is None:
                launch = _report_launch(launch_data, written, started)
                s = Session(_new_folder(crash_dir, written, "-report"),
                            {"format": SESSION_FORMAT, "files": {}, "started": "", "time": written, "end_time": written,
                             "ended": datetime.fromtimestamp(written).strftime(crashlog.TIME_FORMAT)[:-3],
                             "context": (launch or {}).get("context") or context,
                             "context_from": "launch" if launch else "collect",
                             "launched": launch["time"] if launch else 0, "notified": "", "source": str(game_dir)})
            fsutil.atomic_write_text(s.folder / GAME_REPORT_FILE, text)
        except OSError as exc:
            log.warning("게임 오류 보고서를 복사하지 못했습니다 (%s): %s", path, exc)
            if made and s is not None:
                fsutil.rmtree(s.folder)
            continue
        if launch is not None:
            _claim_launch(crash_dir, launch, s.id)
        everything[s.id] = s
        s.info["files"][GAME_REPORT_FILE] = {"from": str(path), "size": st.st_size, "mtime": st.st_mtime_ns,
                                             "time": written, "time_from": time_from, "key": key}
        changed[s.id] = s

    # REFramework's settings as they are now (Lua error logging, loose files, pak_mods): those of the folder
    # the session's records came from (REFramework keeps its settings where it writes its log)
    for s in changed.values():
        source = s.info.get("source")
        if source:
            config = Path(source) / CONFIG_FILE
            config = config if config.is_file() else None
        else:
            config = next((d / CONFIG_FILE for d in dirs if (d / CONFIG_FILE).is_file()), None)
        if config is not None:
            try:
                st = config.stat()
                fsutil.copy_file(config, s.folder / CONFIG_FILE)
                s.info["files"][CONFIG_FILE] = {"from": str(config), "size": st.st_size, "mtime": st.st_mtime_ns}
            except OSError as exc:
                log.warning("REFramework 설정 파일을 복사하지 못했습니다: %s", exc)
        _summarize(s)
        s.info["collected"] = datetime.now().isoformat(timespec="seconds")
        try:
            fsutil.atomic_write_json(s.folder / SESSION_FILE, s.info)
        except OSError as exc:
            log.warning("게임 기록을 저장하지 못했습니다 (%s): %s", s.id, exc)
    return sorted(changed.values(), key=lambda s: (s.time, s.id), reverse=True)


def _dump_key(st) -> str:
    return f"dump:{st.st_size}:{st.st_mtime_ns}"


def game_report_dir(game_dir) -> Path | None:
    return Path(game_dir) / crashlog.GAME_REPORT_DIR if game_dir else None


def _game_reports(game_dir) -> list[tuple[Path, os.stat_result]]:
    """The newest crash reports of the game: zips (or folders) in <game>\\CrashReport."""
    folder = game_report_dir(game_dir)
    found = []
    try:
        entries = list(folder.iterdir()) if folder is not None and folder.is_dir() else []
    except OSError:
        entries = []
    for p in entries:
        if p.is_dir():
            p = p / crashlog.GAME_REPORT_TEXT
        elif p.suffix.lower() != ".zip":
            continue
        st = _stat(p)
        if st is not None and p.is_file():
            found.append((p, st))
    return sorted(found, key=lambda x: x[1].st_mtime, reverse=True)[:MAX_GAME_REPORTS]


def report_name_time(name: str) -> float | None:
    """The crash's minute (local time) in a report's name (2026_1022_0346.zip, its unpacked folder, a temporary
    file); None for another name."""
    m = _REPORT_NAME.match(name)
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups())).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _report_key(path: Path) -> str:
    """The same for a report zip, its unpacked folder and a copy of it ("2026_1022_0346 - 복사본.zip")."""
    name = path.parent.name if path.name == crashlog.GAME_REPORT_TEXT else path.name
    m = _REPORT_NAME.match(name)
    return f"report:{m.group(0) if m else name.removesuffix('.zip').removesuffix('.ZIP')}"


def _report_record_time(s: Session) -> float:
    record = s.info["files"].get(GAME_REPORT_FILE)
    t = record.get("time") if isinstance(record, dict) else None
    return float(t) if isinstance(t, (int, float)) else 0.0


def _report_record_key(s: Session) -> str:
    record = s.info["files"].get(GAME_REPORT_FILE)
    return record.get("key", "") if isinstance(record, dict) else ""


def _read_game_report(path: Path, st) -> tuple[str | None, float, float | None, str]:
    """(CrashReport.txt's text, when the game crashed, when its process started, where that time came from) of a
    report zip or folder; (None, 0, None, "") if unreadable (any error: a damaged zip also raises zlib.error,
    EOFError ...). The crash time is the minidump's ("dump": written at the crash, in whole seconds), else the
    text's ("zip": its entry's time, 2 s), else the name's ("name": only the minute), else the file's ("mtime")."""
    head, stamp = b"", None
    try:
        if path.name != crashlog.GAME_REPORT_TEXT:
            with zipfile.ZipFile(path) as zf:
                by_name = {i.filename.replace("\\", "/").rsplit("/", 1)[-1].casefold(): i for i in zf.infolist()}
                info = by_name.get(crashlog.GAME_REPORT_TEXT.casefold())
                if info is None or info.file_size > MAX_GAME_REPORT_TEXT:
                    return None, 0, None, ""
                data = zf.read(info)
                dump = next((i for n, i in by_name.items() if n.endswith(".dmp")), None)
                if dump is not None:
                    try:
                        with zf.open(dump) as f:
                            head = f.read(DUMP_HEAD)
                    except Exception:  # (a damaged dump only loses the times)
                        head = b""
                try:
                    stamp = time.mktime((*info.date_time, 0, 0, -1))
                except (OverflowError, ValueError):
                    stamp = None
            name = path.name
        else:
            if st.st_size > MAX_GAME_REPORT_TEXT:
                return None, 0, None, ""
            data = path.read_bytes()
            dumps = sorted(path.parent.glob("*.dmp"))
            if dumps:
                try:
                    with open(dumps[0], "rb") as f:
                        head = f.read(DUMP_HEAD)
                except OSError:
                    head = b""
            name = path.parent.name
    except Exception as exc:
        log.info("게임 오류 보고서를 읽지 못했습니다 (%s): %s", path, exc)
        return None, 0, None, ""
    text = data.decode("utf-8", errors="replace")
    if crashlog.parse_game_report(text) is None:
        return None, 0, None, ""
    crashed, started = _dump_times(head)
    written, source = next(((t, source) for t, source in ((crashed, "dump"), (_plausible(stamp), "zip"),
                                                         (_plausible(report_name_time(name)), "name")) if t),
                           (st.st_mtime, "mtime"))
    return text, float(written), started, source


def _plausible(t: float | None) -> float | None:
    return float(t) if t and 1.5e9 < t < time.time() + 86400 else None


def _dump_times(head: bytes) -> tuple[float | None, float | None]:
    """(when it was written, when the process started) from a minidump's first bytes: its header and the
    MiscInfoStream (MINIDUMP_MISC_INFO with MINIDUMP_MISC1_PROCESS_TIMES)."""
    try:
        if len(head) < 32 or head[:4] != b"MDMP":
            return None, None
        count, rva, _sum, written = struct.unpack_from("<IIII", head, 8)
        started = None
        for i in range(min(count, 64)):
            at = rva + 12 * i
            if at + 12 > len(head):
                break
            kind, size, where = struct.unpack_from("<III", head, at)
            if kind == 15 and size >= 24 and where + 24 <= len(head):
                flags, _pid, created = struct.unpack_from("<III", head, where + 4)
                started = float(created) if flags & 2 else None
                break
    except struct.error:
        return None, None
    return _plausible(written), _plausible(started)


def _report_owner(candidates: Iterable[Session], written: float, started: float | None = None,
                  launched: float | None = None) -> Session | None:
    """The session that was running when the game wrote its crash report (one without a report yet). With
    the process start from the dump: the session REFramework began in that process (one game runs at a
    time). Else the newest running then that no other run (a kept session, or the last launch from the
    manager) started after, before the report - a run without REFramework writes no log, and its report is
    not the earlier run's."""
    candidates = list(candidates)
    if started is not None:
        spans = [s for s in candidates if s.started and started - 30 <= s.time <= written + 5]
    else:
        later = [t for t in (*(x.time for x in candidates), launched) if t]
        spans = [s for s in candidates if s.started and s.time - 5 <= written <= s.end_time + GAME_REPORT_SECONDS
                 and not any(s.end_time + 5 < t <= written for t in later)]
    spans = [s for s in spans if not _report_record_key(s)]
    return max(spans, key=lambda s: s.time) if spans else None


def _report_launch(data, written: float, started: float | None = None) -> dict | None:
    """The launch from the manager a report without a log came from (REFramework off: no session claimed it)."""
    if not isinstance(data, dict) or not isinstance(data.get("time"), (int, float)) or data.get("claimed"):
        return None
    if started is not None:
        return data if -60 <= started - data["time"] <= LAUNCH_SECONDS else None
    return data if -60 <= written - data["time"] <= REPORT_LAUNCH_SECONDS else None


def _pruned(crash_dir) -> list[str]:
    data = fsutil.read_json(Path(crash_dir) / PRUNED_FILE, {})
    items = data.get("records") if isinstance(data, dict) else None
    return [i for i in items if isinstance(i, str)] if isinstance(items, list) else []


def _dump_time(s: Session) -> float | None:
    """When the dump a session holds was written (None: it has none)."""
    record = s.info["files"].get(DUMP_FILE)
    if not isinstance(record, dict):
        return None
    return float(record["time"]) if isinstance(record.get("time"), (int, float)) else 0.0


def _dump_owner(candidates: Iterable[Session], written: float) -> Session | None:
    # a session keeps the dump it has: a later crash's dump never replaces it (it gets a session of its own)
    candidates = [s for s in candidates if (t := _dump_time(s)) is None or abs(t - written) <= 2]
    crashed = [(abs(written - float(s.summary["crash_time"])), s) for s in candidates
               if s.summary.get("crash_time") and abs(written - float(s.summary["crash_time"])) <= DUMP_SECONDS]
    if crashed:
        return min(crashed, key=lambda x: x[0])[1]
    spans = [s for s in candidates if s.started and s.time - 5 <= written <= s.end_time + DUMP_SECONDS]
    if spans:
        return max(spans, key=lambda s: s.time)
    # The log's times are local: when the time zone changed since, they are off by whole quarter hours.
    # A crash whose log says it wrote the dump, and that has none yet, still claims it.
    shifted = []
    for s in candidates:
        if s.summary.get("dump_written") and s.summary.get("crash_time") and _dump_time(s) is None:
            diff = written - float(s.summary["crash_time"])
            off = abs(diff - round(diff / 900) * 900)
            if 900 - DUMP_SHIFT_SECONDS <= abs(diff) <= 14 * 3600 and off <= DUMP_SHIFT_SECONDS:
                shifted.append((off, s))
    return min(shifted, key=lambda x: x[0])[1] if shifted else None


def _summarize(s: Session) -> None:
    info = s.log()
    summary: dict = {"faulty": len(s.faulty())}
    if info is not None:
        s.info["ended"] = info.ended
        s.info["end_time"] = _epoch(info.ended) or s.time
        bases = [b for b in (info.game_path, s.info.get("source", "")) if b]
        summary.update({"refw": refw_label(info.header), "commit": info.header.get("commit", ""),
                        "stuck_script": module_name(info.stuck_script) if info.stuck_script else "",
                        "lua_errors": len(info.lua_errors), "plugin_errors": len(info.plugin_errors),
                        "paks_failed": len(info.paks_failed)})
        crash = info.crash
        if crash is not None:
            category, _rel = module_place(crash.place, bases)
            summary.update({"crash": crash.code, "crash_time": _epoch(crash.time) or s.end_time,
                            "module": crash.place, "module_name": module_name(crash.place) if crash.place else "",
                            "category": category, "dump_written": bool(crash.dump) and not crash.dump_failed})
    report = s.game_report()
    if report is not None:
        record = s.info["files"].get(GAME_REPORT_FILE) or {}
        summary["game_report"] = True
        summary["device_error"] = report.device_error
        if not summary.get("crash"):
            top = report.modules[0] if report.modules else ""
            summary.update({"crash": report.code, "crash_from": "report",
                            "crash_time": float(record.get("time") or s.end_time),
                            "crash_time_from": str(record.get("time_from") or ""), "module": top,
                            "module_name": module_name(top) if top else "",
                            "category": "gpu" if report.device_error else module_place(top, [s.info.get("source", "")])[0]
                            if top else ""})
    s.info["summary"] = summary


def prune(crash_dir, keep: int = KEEP_SESSIONS, recent: int = KEEP_RECENT) -> None:
    """Keep ``keep`` sessions: the ``recent`` newest, then crashes, then sessions with files the game could
    not load (the list often has the game's own files in every session), then the rest."""
    found = sessions(crash_dir)
    excess = len(found) - keep
    if excess > 0:
        rank = {"crash": 2, "dump": 2, "faulty": 1}
        rest = sorted(found[recent:], key=lambda s: (rank.get(s.problem(), 0), s.time))
        let_go = []
        for s in rest[:excess]:
            let_go.append(s.started)
            dump = (s.info.get("files") or {}).get(DUMP_FILE)
            if isinstance(dump, dict):
                let_go.append(f"dump:{dump.get('size')}:{dump.get('mtime')}")
            if _report_record_key(s):
                let_go.append(_report_record_key(s))
            try:
                fsutil.rmtree(s.folder)
            except OSError as exc:
                log.warning("오래된 게임 기록을 지우지 못했습니다 (%s): %s", s.id, exc)
        records = list(dict.fromkeys([*_pruned(crash_dir), *(r for r in let_go if r)]))[-100:]
        try:
            fsutil.atomic_write_json(Path(crash_dir) / PRUNED_FILE, {"records": records})
        except OSError as exc:
            log.warning("게임 기록 정리 상태를 저장하지 못했습니다: %s", exc)
    root = Path(crash_dir)
    for d in root.iterdir() if root.is_dir() else ():  # folders a crash of the manager left half made
        if d.is_dir() and not (d / SESSION_FILE).is_file() and time.time() - d.stat().st_mtime > 86400:
            try:
                fsutil.rmtree(d)
            except OSError:
                pass


# ---- the startup notice ------------------------------------------------------------------------------
def _signature(s: Session) -> str:
    return f"{s.summary.get('crash', '')}|{s.summary.get('faulty', 0)}|{bool(s.dump_path)}"


def pending_notice(crash_dir, now: float | None = None, days: float = NOTICE_DAYS, index: ModIndex | None = None,
                   skip_started: str = "") -> Session | None:
    """The newest recent session to tell the user about, once: a crash, a dump alone, or files the game
    could not load that were not in an earlier notice (with ``index``: only those a mod or a file no mod
    placed has; the game's own files are listed even without mods). Nothing older than the last session
    told about; ``skip_started`` is the session of the game running now (not finished). Reads paks with
    ``index``: off the GUI thread."""
    now = now if now is not None else time.time()
    state = _notice_state(crash_dir)
    shown = set(state.get("faulty") or [])
    until = state.get("until") if isinstance(state.get("until"), (int, float)) else 0
    for s in sessions(crash_dir):
        if now - s.end_time > days * 86400 or s.time <= until:
            break
        if skip_started and s.started == skip_started or s.info.get("notified") == _signature(s):
            continue
        kind = s.problem()
        if kind in ("crash", "dump"):
            return s
        new = [f for f in s.faulty() if f.key not in shown] if kind == "faulty" else []
        if new and index is None:
            return s
        if new:
            match = index.for_session(s).match_faulty(new, session_loose(s))
            if match.by_mod or match.loose or match.paks:
                return s
    return None


def notice_text(s: Session) -> str:
    return NOTICE_FAULTY if s.problem() == "faulty" else NOTICE_CRASH


def mark_notified(crash_dir, s: Session) -> None:
    """The user was told about ``s``: neither it nor an older session is told about again."""
    s.info["notified"] = _signature(s)
    state = _notice_state(crash_dir)
    keys = list(dict.fromkeys([*(state.get("faulty") or []), *(f.key for f in s.faulty())]))[-5000:]
    until = state.get("until") if isinstance(state.get("until"), (int, float)) else 0
    try:
        fsutil.atomic_write_json(s.folder / SESSION_FILE, s.info)
        fsutil.atomic_write_json(Path(crash_dir) / NOTICE_FILE, {"faulty": keys, "until": max(until, s.time)})
    except OSError as exc:
        log.warning("게임 기록 알림 상태를 저장하지 못했습니다: %s", exc)


def _notice_state(crash_dir) -> dict:
    data = fsutil.read_json(Path(crash_dir) / NOTICE_FILE, {})
    return data if isinstance(data, dict) else {}


# ---- matching evidence to mods ---------------------------------------------------------------------
@dataclass
class ModRef:
    """What explain needs to know of one installed mod."""
    id: str
    name: str
    enabled: bool = True  # now
    files: Sequence[str] = ()  # game paths the mod puts into the game (manager.mod_files; also when it is off)
    material_state: str = ""  # manager.material_state
    stuck: Sequence[str] = ()  # labels of files the material repair could not fix (manager.material_stuck)


@dataclass
class FaultyMatch:
    by_mod: dict[str, list[FaultyFile]] = field(default_factory=dict)  # mod id -> its files the game could not load
    loose: list[tuple[FaultyFile, str]] = field(default_factory=list)  # (entry, game path of a file no mod placed)
    paks: dict[str, list[FaultyFile]] = field(default_factory=dict)  # pak no mod placed -> entries in it
    unknown: list[FaultyFile] = field(default_factory=list)  # in no mod (often the game's own files)
    unchecked: list[FaultyFile] = field(default_factory=list)  # past MAX_FAULTY_MATCH: not looked for in paks
    unreadable: list[str] = field(default_factory=list)  # paks that could not be read (game paths)


def _tail_match(key: str, tail: str, cut: bool = False) -> bool:
    """A game path ``key`` against the end of a logged path: the tail is the key, or holds the whole key
    after a folder of the game's path; a ``cut`` one (Lua's "..." and the last characters) may also
    start inside a folder name of the key, but never inside its file name."""
    return (key == tail or key.endswith("/" + tail) or tail.endswith("/" + key)
            or cut and "/" in tail and key.endswith(tail))


class ModIndex:
    """The manager's mods and deployed files, to match evidence to. ``deployed`` is the deploy
    manifest (manager.deployed_files(): lower-cased game path -> {"path", "mod", "pak", ...});
    ``names`` gives names for mods no longer installed (from a session's context). Reads the game
    folder only (pak tables, folder listings).

    A session is explained with :meth:`for_session`: then only the mods that were on in it count
    (``ran``, in load order; of those only the ones with files in the game folder then, ``present``,
    when the session says), and the files the manager had placed then (``owners``, outside natives\\)
    name their mods, whatever was installed, turned off or renumbered since. A loose file several of
    them have was the last one's in load order (the one the manager had placed), whoever placed it since."""

    def __init__(self, game_dir=None, deployed: dict | None = None, mods: Iterable[ModRef] = (),
                 names: dict[str, str] | None = None, ran: Iterable[str] | None = None, owners: dict | None = None,
                 present: Iterable[str] | None = None):
        self.game_dir = Path(game_dir) if game_dir else None
        self.deployed = {k: v for k, v in (deployed or {}).items() if isinstance(v, dict)}
        self.mods = {m.id: m for m in mods}
        self.names = dict(names or {})
        self.order = list(dict.fromkeys(ran)) if ran is not None else None  # ids of the mods on in the session
        self.ran = set(self.order) if self.order is not None else None  # (None: as now)
        self.present = set(present) if present is not None else None  # of those, the ones applied (None: not known)
        self.owners = ({norm_path(k): v for k, v in owners.items() if isinstance(k, str) and isinstance(v, str) and v}
                       if isinstance(owners, dict) else None)
        self.unreadable: list[str] = []
        self._placed: dict[str, str] | None = None
        self._keys: dict[str, set[str]] = {}
        self._loose: dict[str, list[str]] | None = None
        self._exact: dict[str, list[str]] | None = None
        self._paks: list[tuple[str, str]] | None = None
        self._pak_tables: dict[str, object] = {}
        self._listings: dict[Path, dict[str, str]] = {}
        self._versions: dict[str, list[str]] = {}
        self._ranks: dict[str, int] | None = None

    def for_session(self, session: Session | None) -> ModIndex:
        """This index as it was when ``session`` ran (its context: the mods on then and the files the
        manager had placed); itself for a session without one (records the manager did not know the mods of)."""
        context = session.context if session is not None else {}
        mods = context.get("mods")
        if not isinstance(mods, list):
            return self
        mods = [m for m in mods if isinstance(m, dict) and isinstance(m.get("id"), str)]
        names = {**{m["id"]: m["name"] for m in mods if isinstance(m.get("name"), str) and m["name"]}, **self.names}
        placed = context.get("placed")
        view = ModIndex(self.game_dir, self.deployed, self.mods.values(), names,
                        ran=[m["id"] for m in mods if m.get("enabled")], owners=context.get("owners"),
                        present=[i for i in placed if isinstance(i, str)] if isinstance(placed, list) else None)
        view.unreadable, view._pak_tables, view._listings, view._versions = (
            self.unreadable, self._pak_tables, self._listings, self._versions)
        return view

    def name(self, mod_id: str) -> str:
        mod = self.mods.get(mod_id)
        return mod.name if mod else self.names.get(mod_id) or mod_id

    def label(self, mod_id: str) -> str:
        """``'name'`` for a finding, saying so when the mod is installed but turned off now."""
        mod = self.mods.get(mod_id)
        return f"'{self.name(mod_id)}'" + (" (지금은 꺼져 있음)" if mod is not None and not mod.enabled else "")

    def was_on(self, mod: ModRef) -> bool:
        """The mod was on in the session explained (and applied, when the session says which mods were;
        without a session: it is on now)."""
        return self._in_session(mod.id) if self.ran is not None else mod.enabled

    def _in_session(self, mod_id: str) -> bool:
        """The mod's files were in the game folder in the session (as far as it says; without one: True)."""
        return (self.ran is None or mod_id in self.ran) and (self.present is None or mod_id in self.present)

    def _keys_of(self, mod: ModRef) -> set[str]:
        if mod.id not in self._keys:
            self._keys[mod.id] = {norm_path(f) for f in mod.files}
        return self._keys[mod.id]

    def placed(self) -> dict[str, str]:
        """Game path -> mod of the files the manager had placed in the game folder: the session's record
        of them (natives\\ from the deploy manifest), else the manifest's, of the mods that were on."""
        if self._placed is None:
            placed = {k: str(e["mod"]) for k, e in self.deployed.items()
                      if e.get("mod") and self._in_session(str(e["mod"]))}
            if self.owners is not None:
                placed = {k: m for k, m in placed.items() if k.startswith("natives/")}
                placed.update(self.owners)
            self._placed = placed
        return self._placed

    def owner(self, rel: str | None) -> str:
        """The mod that placed a game-relative path, or that was on and has it ("" if none)."""
        key = norm_path(rel) if rel else ""
        if not key:
            return ""
        return self.placed().get(key) or next(
            (m.id for m in self.mods.values() if self.was_on(m) and key in self._keys_of(m)), "")

    def placed_name(self, name: str) -> str:
        """The game path of a file the manager placed (or a mod that was on has) by this file name, for a
        module an error window names without its folder ("" if none)."""
        low = name.strip().casefold()

        def best(keys) -> str:
            found = [k for k in keys if k.rsplit("/", 1)[-1] == low and not k.startswith("natives/")]
            return min(found, key=lambda k: (not k.startswith("reframework/plugins/"), k.count("/"), k), default="")

        return best(self.placed()) or best(k for m in self.mods.values() if self.was_on(m) for k in self._keys_of(m))

    def has_paks(self) -> bool:
        return any(k.endswith(".pak") for k in self.placed())

    def has_loose(self) -> bool:
        return any(k.startswith("natives/") for k in self.placed())

    def owners_by_tail(self, tail: str) -> list[str]:
        """Mods with a placed file (or one that was on with a file) whose path ends like ``tail`` (Lua cuts
        long paths to "..." and their last characters)."""
        t = norm_path(tail)
        cut = t.startswith("...")
        t = t.removeprefix("...").lstrip("/")
        if not t:
            return []
        found = [m for k, m in self.placed().items() if _tail_match(k, t, cut)]
        if not found:
            found = [m.id for m in self.mods.values()
                     if self.was_on(m) and any(_tail_match(k, t, cut) for k in self._keys_of(m))]
        return list(dict.fromkeys(found))

    # ---- faulty files
    def _loose_index(self) -> dict[str, list[str]]:
        """Resource -> the mods whose loose file for it the game could read: the files in the game folder
        now and those of the mods that are on but not applied; for a session, the one of the mods that
        were on (and applied) with it that came last in load order (the manifest may name another one
        now: a mod turned off or moved since)."""
        if self._loose is None:
            index: dict[str, list[str]] = {}
            for key, mod in self.placed().items():  # the files in the game folder
                index.setdefault(resource_key(key), []).append(mod)
            placed = set(index)
            for m in self.mods.values():  # and those of the mods that were on (but not applied)
                if self.was_on(m):
                    for rel in m.files:
                        key = resource_key(rel)
                        if (key not in placed or self.order is not None) and m.id not in index.get(key, []):
                            index.setdefault(key, []).append(m.id)
            if self.order is not None:
                rank = {mod_id: n for n, mod_id in enumerate(self.order)}
                index = {key: [max(ids, key=lambda i: rank.get(i, -1))] for key, ids in index.items()}
            self._loose = index
        return self._loose

    def loose_owner(self, key: str) -> str:
        """The mod whose loose file the game reads for a resource (crashlog.resource_key): the one the manager
        placed, else the last in load order of the mods that were on ("" for none)."""
        owners = self._loose_index().get(key)
        return owners[-1] if owners else ""

    def loose_path_owner(self, path: str) -> str:
        """The mod whose loose file is at exactly this game path (natives\\..., with its folder - streaming\\ or
        not - and version suffix: REFramework's loose file loader serves only a file that is there), as
        loose_owner picks it ("" for none)."""
        if self._exact is None:
            index: dict[str, list[str]] = {}
            for key, mod in self.placed().items():
                if key.startswith("natives/"):
                    index.setdefault(key, []).append(mod)
            placed = set(index)
            for m in self.mods.values():
                if self.was_on(m):
                    for rel in m.files:
                        key = norm_path(rel)
                        if key.startswith("natives/") and (key not in placed or self.order is not None) \
                                and m.id not in index.get(key, []):
                            index.setdefault(key, []).append(m.id)
            if self.order is not None:
                rank = {mod_id: n for n, mod_id in enumerate(self.order)}
                index = {key: [max(ids, key=lambda i: rank.get(i, -1))] for key, ids in index.items()}
            self._exact = index
        owners = self._exact.get(norm_path(path))
        return owners[-1] if owners else ""

    def pak_paths(self) -> list[tuple[str, str, int]]:
        """(mod id or "" for a pak no mod placed, game path, patch number REFramework gives it: the highest
        wins) of the paks in pak_mods the game read."""
        ranks = self._pak_ranks()
        return [(mod_id, rel, ranks.get(rel, -1)) for mod_id, rel in self._pak_list()]

    def pak_file(self, rel: str):
        """The entry table of a pak in the game folder (matfix.pak.PakFile; None if it cannot be read)."""
        return self._pak_table(rel)

    def later_gone(self, mod_id: str) -> bool:
        """For a session: a mod that was on in it after this one in load order is no longer installed
        (its files are not known, so it may have been the one whose copy the game read)."""
        if self.order is None or mod_id not in self.order:
            return False
        return any(i not in self.mods and self._in_session(i) for i in self.order[self.order.index(mod_id) + 1:])

    def _pak_list(self) -> list[tuple[str, str]]:
        """(mod id or "" for a pak no mod placed, game path) of the paks REFramework or the game read."""
        if self._paks is None:
            managed = [(e.get("mod", ""), e["path"]) for e in self.deployed.values() if e.get("pak") and e.get("path")]
            # a pak of a mod that was not on (or not applied) came after the session (nor is it one no mod placed)
            paks = [(m, p) for m, p in managed if self._in_session(m)]
            if self.game_dir is not None:
                folder = self.game_dir / PAK_MODS_DIR
                placed = {norm_path(p) for _, p in managed}
                try:
                    found = sorted(folder.rglob("*.pak")) if folder.is_dir() else []
                except OSError:
                    found = []
                for path in found:  # REFramework reads every *.pak there (a case-sensitive test)
                    rel = path.relative_to(self.game_dir).as_posix()
                    if path.suffix == ".pak" and norm_path(rel) not in placed:
                        paks.append(("", rel))
            self._paks = paks
        return self._paks

    def _pak_table(self, rel: str):
        if rel not in self._pak_tables:
            from .matfix.pak import PakFile

            table = None
            if self.game_dir is not None:
                try:
                    table = PakFile(self.game_dir / rel)
                except Exception as exc:  # a broken or half-copied pak (any error of the parser): not checked
                    log.debug("PAK을 읽지 못했습니다 (%s): %s", rel, exc)
                    self.unreadable.append(rel)
            self._pak_tables[rel] = table
        return self._pak_tables[rel]

    def versions(self, ext: str) -> list[str]:
        """Version suffixes to try for a file type: those the mods use, then the game's (bundled list)."""
        ext = ext.lower()
        if ext not in self._versions:
            seen: Counter = Counter()
            rx = re.compile(rf"\.{re.escape(ext)}\.(\d+)$", re.IGNORECASE)
            paths = [e.get("path", "") for e in self.deployed.values()]
            paths += [f for m in self.mods.values() for f in m.files]
            for p in paths:
                m = rx.search(p)
                if m:
                    seen[m.group(1)] += 1
            found = [v for v, _n in seen.most_common()]
            try:
                from .equip import pathlist

                found += list(pathlist.versions().get(ext, ()))
            except Exception as exc:  # the bundled list is only a help
                log.debug("경로 목록을 읽지 못했습니다: %s", exc)
            found += list(FALLBACK_VERSIONS.get(ext, ()))
            self._versions[ext] = list(dict.fromkeys(found))
        return self._versions[ext]

    def pak_candidates(self, path: str) -> list[str]:
        """``natives/STM/<path>.<version>`` as a pak may store a resource REFramework names without them."""
        p = path.replace("\\", "/").strip().lstrip("@/")
        p = re.sub(r"^natives/(?:stm/)?", "", p, flags=re.IGNORECASE)
        base, version = split_version(p)
        file_name = base.rsplit("/", 1)[-1]
        if version:
            names = [p]
        elif "." in file_name:
            names = [f"{base}.{v}" for v in self.versions(file_name.rsplit(".", 1)[-1])]
        else:
            names = []
        out = []
        for name in names:
            out.append("natives/STM/" + name)
            if re.search(r"\.(tex|mesh)\.\d+", name, re.IGNORECASE):  # the game keeps a streaming/ copy of these
                out.append("natives/STM/streaming/" + name)
        return list(dict.fromkeys(out))

    def _listing(self, folder: Path) -> dict[str, str]:
        if folder not in self._listings:
            try:
                self._listings[folder] = {n.casefold(): n for n in os.listdir(fsutil.longpath(folder))}
            except OSError:
                self._listings[folder] = {}
        return self._listings[folder]

    def loose_copy(self, path: str) -> str | None:
        """A file for this resource in the game folder's natives\\STM\\ that no mod placed (game path)."""
        if self.game_dir is None:
            return None
        p = path.replace("\\", "/").strip().lstrip("@/")
        p = re.sub(r"^natives/(?:stm/)?", "", p, flags=re.IGNORECASE)
        parts = ["natives", "stm", *[x for x in p.split("/") if x]]
        folder, rel = self.game_dir, []
        for part in parts[:-1]:
            name = self._listing(folder).get(part.casefold())
            if name is None:
                return None
            folder, rel = folder / name, [*rel, name]
        want = resource_key(parts[-1])
        for low, name in sorted(self._listing(folder).items()):
            if resource_key(low) == want:
                game_path = "/".join([*rel, name])
                if norm_path(game_path) not in self.deployed and (folder / name).is_file():
                    return game_path
        return None

    def match_faulty(self, entries: Sequence[FaultyFile], loose: bool | None = None) -> FaultyMatch:
        """Where the game got the files REFramework could not load: a loose file in the game folder first
        (REFramework's loose file loading serves one before any pak), else the pak that wins. ``loose``:
        whether that loading was on (False: loose files were not read; None: not known, as if on)."""
        out = FaultyMatch()
        index = self._loose_index() if loose is not False else {}
        for i, entry in enumerate(entries):
            owners = index.get(entry.key)
            if owners:
                for mod_id in owners:
                    out.by_mod.setdefault(mod_id, []).append(entry)
                continue
            if i >= MAX_FAULTY_MATCH:
                out.unchecked.append(entry)
                continue
            copy = self.loose_copy(entry.path) if loose is not False else None
            if copy:
                out.loose.append((entry, copy))
                continue
            hits = self._pak_hits(entry.path)
            if hits:
                for mod_id, rel in hits:
                    if mod_id:
                        out.by_mod.setdefault(mod_id, []).append(entry)
                    else:
                        out.paks.setdefault(rel, []).append(entry)
                continue
            out.unknown.append(entry)
        out.unreadable = list(dict.fromkeys(self.unreadable))
        return out

    def _pak_hits(self, path: str) -> list[tuple[str, str]]:
        """The pak in pak_mods the game read this resource from: of those that have it, the one REFramework
        gave the highest patch number."""
        paks = self._pak_list()
        if not paks:
            return []
        from .matfix.pak import path_hash

        hashes = [path_hash(c) for c in self.pak_candidates(path)]
        hits = []
        for mod_id, rel in paks:
            table = self._pak_table(rel)
            if table is not None and any(table.find(h) is not None for h in hashes):
                hits.append((mod_id, rel))
        if len(hits) > 1:
            ranks = self._pak_ranks()
            hits = [max(hits, key=lambda hit: ranks.get(hit[1], -1))]
        return hits

    def _pak_ranks(self) -> dict[str, int]:
        """Game path -> the patch number REFramework gives a pak in pak_mods (the highest wins): a name like
        ``re_chunk_000.pak.sub_000.pak.patch_012.pak`` takes its number, the others the next free ones
        after the game's own patches, in the order Windows lists the folder (NTFS: by upper-cased name,
        a subfolder's paks where the subfolder is)."""
        if self._ranks is None:
            free = 1
            if self.game_dir is not None:
                listing = self._listing(self.game_dir)
                while free < 32 and f"{PAK_FAMILY}.patch_{free:03d}.pak".casefold() in listing:
                    free += 1
            ranks: dict[str, int] = {}
            auto = []
            for _mod, rel in self._pak_list():
                m = re.match(r"(.+)\.patch_(\d+)", module_name(rel))  # (at the last ".patch_", as REFramework)
                if m and m.group(1) == PAK_FAMILY:
                    ranks[rel] = int(m.group(2))
                else:
                    auto.append(rel)
            for n, rel in enumerate(sorted(auto, key=lambda r: tuple(p.upper() for p in r.split("/")))):
                ranks[rel] = free + n
            self._ranks = ranks
        return self._ranks


# ---- explaining a session -------------------------------------------------------------------------
@dataclass
class Finding:
    kind: str  # see KINDS
    text: str  # one Korean line (without the bullet); blame is always worded as a suspicion
    evidence: list[str] = field(default_factory=list)  # the record lines it rests on
    mods: list[str] = field(default_factory=list)  # ids of the suspected mods
    level: str = "info"  # "high" | "medium" | "low" | "info" (nothing to point at)
    action: str = ""  # what may help: "matfix" (재질 점검·복구), "bisect" (원인 모드 찾기), "update_refw"


KINDS = ("plugin", "reframework", "game", "gpu", "root_dll", "system", "other", "unknown", "stack", "suspicious",
         "accessed", "accessed_some",
         "script", "lua", "plugin_load", "pak_failed", "pak_slot", "pak_none", "pak_disabled", "loose_failed",
         "init", "hooks", "present", "faulty", "faulty_loose", "faulty_pak", "faulty_game", "faulty_more",
         "faulty_unread", "material", "stuck", "no_crash", "running", "no_record", "dump_only", "corrupted_pak", "d3d",
         "unhandled", "code", "named", "game_report")
_LEVELS = {"high": 0, "medium": 1, "low": 2, "info": 3}


@dataclass
class Explanation:
    summary: str  # "21:43 종료 · 오류 코드 0xC0000005 (메모리 접근 오류) · REFramework nightly 01432"
    findings: list[Finding] = field(default_factory=list)  # the strongest first
    suspects: list[str] = field(default_factory=list)  # mod ids, the most likely first
    crashed: bool = False  # REFramework recorded a fatal exception (or a dump is left)
    code: str = ""  # exception code, 8 hex digits
    category: str = ""  # where it crashed (crashlog.module_place)
    has_log: bool = False

    def lines(self) -> list[str]:
        out = [self.summary, "", f"{SUSPECTS_HEADER}:"] if self.summary else [f"{SUSPECTS_HEADER}:"]
        for f in self.findings:
            out.append(f"• {f.text}")
            out += [f"    근거: {e}" for e in f.evidence[:3]]
        if not self.findings:
            out.append("• 찾은 것이 없습니다.")
        return out


def _mods_text(index: ModIndex, mods: Sequence[str]) -> str:
    return ", ".join(index.label(m) for m in mods)


def _suspect(index: ModIndex, mods: Sequence[str], unmanaged: str = "") -> str:
    if mods:
        return f" → 의심되는 모드 {_mods_text(index, mods)}"
    return f" → {unmanaged}" if unmanaged else ""


def crash_finding(place: str, bases: Sequence[str], index: ModIndex, evidence: list[str]) -> Finding:
    """What the crashing module says (also used for a module pasted from the event log)."""
    category, rel = module_place(place, bases)
    name = module_name(place) if place else ""
    if name and name == place.strip() and (known := index.placed_name(name)):  # a file name alone: the mods' file
        category, rel = crashlog.game_place(known)
    owner = index.owner(rel) if rel else ""
    mods = [owner] if owner else []
    if category == "plugin":
        return Finding("plugin", f"플러그인 '{name}'에서 오류가 났습니다"
                       + _suspect(index, mods, "매니저가 설치하지 않은 플러그인입니다. 이 플러그인을 빼고 다시 해 보세요"),
                       evidence, mods, "high")
    if category == "reframework":
        return Finding("reframework", "REFramework(dinput8.dll) 안에서 오류가 났습니다. REFramework 자체나 Lua 스크립트 "
                       "문제일 수 있습니다 → REFramework를 최신 빌드로 업데이트해 보세요", evidence, [], "medium", "update_refw")
    if owner:  # a file a mod placed in the game folder (a vanilla DLL a mod replaced too)
        return Finding(category, f"게임 폴더의 '{name}'에서 오류가 났습니다" + _suspect(index, mods), evidence, mods, "high")
    if category == "game":
        if rel and rel != crashlog.GAME_EXE_NAME:
            return Finding("game", f"게임에 들어 있는 파일 '{name}'에서 오류가 났습니다. 기록만으로는 어떤 모드인지 알 수 없습니다.",
                           evidence, [], "info", "bisect")
        return Finding("game", "게임 본체에서 오류가 났습니다. 기록만으로는 어떤 모드인지 알 수 없습니다.", evidence, [], "info",
                       "bisect")
    if category == "gpu":
        return Finding("gpu", "그래픽(D3D12) 쪽에서 오류가 났습니다. 모드의 모델·텍스처·재질 파일이나 그래픽 플러그인이 "
                       "그래픽카드에 잘못된 작업을 보내도 이렇게 나고, 드라이버·오버레이·프레임 생성 설정 때문일 수도 있어 "
                       "기록만으로는 모드를 알 수 없습니다.", evidence, [], "info", "bisect")
    if category == "root_dll" and rel in crashlog.PROXY_DLLS:  # never a file of the game
        return Finding("root_dll", f"게임 폴더에 직접 넣은 '{name}'에서 오류가 났습니다. 매니저가 넣은 파일이 아닙니다"
                       "(ReShade·OptiScaler 같은 프로그램일 수 있음) → 이 파일을 빼고 다시 해 보세요", evidence, [], "high")
    if category == "root_dll":  # the list of the game's own files is from one install: an update may add some
        return Finding("root_dll", f"게임 폴더의 '{name}'(매니저가 넣지 않은 파일)에서 오류가 났습니다 → ReShade·OptiScaler"
                       "처럼 직접 넣은 프로그램이면 빼고 다시 해 보세요. 게임 업데이트로 생긴 파일일 수도 있으니 지우기 전에 "
                       "확인하세요(Steam의 '게임 파일 무결성 확인'으로 되돌릴 수 있음)", evidence, [], "medium")
    if category == "system":
        return Finding("system", f"Windows 시스템 파일 '{name}'에서 오류가 났습니다. 다른 곳의 문제가 여기서 드러나는 경우가 "
                       "많아 기록만으로는 어떤 모드인지 알 수 없습니다.", evidence, [], "info", "bisect")
    if category == "other":
        label = crashlog.KNOWN_PROGRAMS.get(name.casefold())
        if label:
            return Finding("other", f"다른 프로그램의 파일 '{name}'({label})에서 오류가 났습니다 → 그 프로그램을 끄거나 "
                           "게임을 예외로 두고 다시 해 보세요", evidence, [], "medium")
        return Finding("other", f"게임·모드가 아닌 다른 프로그램의 파일 '{name}'에서 오류가 났습니다 → 오버레이·백신·녹화 "
                       "프로그램 등을 끄고 다시 해 보세요", evidence, [], "medium")
    if category == "named":
        return Finding("named", f"'{name}'에서 오류가 났습니다. 파일 이름만으로는 어느 프로그램의 파일인지 알 수 없습니다 → "
                       "이벤트 뷰어의 '오류 있는 모듈 경로' 줄도 함께 붙여 넣어 주세요", evidence, [], "info")
    return Finding("unknown", "어느 파일에도 속하지 않는 코드에서 오류가 났습니다. REFramework 플러그인이나 Lua 스크립트가 바꾼 "
                   "게임 코드일 수 있습니다.", evidence, [], "low", "bisect")


def _crash_findings(crash: Crash, bases: Sequence[str], index: ModIndex) -> list[Finding]:
    evidence = [f"Exception occurred: {crash.code}"]
    if crash.module:
        evidence.append(f"Module: {crash.module_base} {crash.module}".replace("Module:  ", "Module: "))
    elif crash.module_logged:
        evidence.append("Module: Unknown")
    elif crash.place:
        evidence.append(f"첫 호출 기록: {crash.place}")
    top = crash.frames[0] if crash.frames else None
    if top is not None:
        evidence.append(f"오류 위치: {top.describe()}")
    out = [crash_finding(crash.place, bases, index, evidence)]
    if out[0].kind == "game" and any(f.tdb for f in crash.frames[:3]):
        tdb = next(f.tdb for f in crash.frames[:3] if f.tdb)
        out[0].evidence.append(f"게임 함수(참고용, 정확하지 않을 수 있음): {tdb}")
    # plugins further down the crashed thread's stack (weaker: a plugin hook the crash passed through)
    seen = set()
    for frame in crash.frames[1:]:
        category, rel = module_place(frame.module, bases)
        if category != "plugin" or rel in seen or module_place(crash.place, bases)[1] == rel:
            continue
        seen.add(rel)
        owner = index.owner(rel)
        out.append(Finding("stack", f"오류가 난 호출 기록에 플러그인 '{module_name(frame.module)}'이 있습니다"
                           + _suspect(index, [owner] if owner else []), [f"호출 기록: {frame.describe()}"],
                           [owner] if owner else [], "low"))
        if len(seen) >= 3:
            break
    if crash.suspicious:
        out.append(Finding("suspicious", "REFramework가 이 오류를 '의심스러운 예외'로 표시했습니다. 게임 보호 장치가 일부러 낸 "
                           "예외일 수 있습니다.", ["SUSPICIOUS EXCEPTION: _ReturnAddress() == nullptr"], [], "info"))
    return out


_RSZ_EXTS_RE = re.compile(r"\.(user|pfb|scn|mdf2)$", re.IGNORECASE)


def _faulty_findings(match: FaultyMatch, index: ModIndex) -> list[Finding]:
    out = []

    def sample(items: list[FaultyFile]) -> list[str]:
        return [f'"{f.label or "?"}" "{f.path}"' for f in items[:3]]

    def more(items) -> str:
        return f" 외 {len(items) - 1}개" if len(items) > 1 else ""

    for mod_id, items in match.by_mod.items():
        rsz = any(_RSZ_EXTS_RE.search(f.key) for f in items)
        mod = index.mods.get(mod_id)
        state = mod.material_state if mod is not None and all(_RSZ_EXTS_RE.search(f.key) for f in items) else ""
        if state == "current":  # (REFramework lists some of the game's own files too: #1677)
            out.append(Finding("faulty", f"REFramework가 {index.label(mod_id)}의 {items[0].name}{more(items)}을(를) 읽지 못한 "
                               "파일로 기록했지만, 재질 점검에서는 현재 게임과 맞는 파일입니다. 바뀌지 않은 게임 파일도 이렇게 "
                               "기록되는 경우가 있어 원인이 아닐 수 있습니다 → 게임이 멈추거나 튕기면 모드를 업데이트하거나 꺼 "
                               "보세요", sample(items), [mod_id], "low"))
            continue
        out.append(Finding("faulty", f"REFramework가 읽지 못한 파일: {index.label(mod_id)}의 {items[0].name}{more(items)}"
                           + (" → 재질 점검·복구를 해 보세요" if rsz else " → 모드를 최신 버전으로 업데이트하거나 꺼 보세요"),
                           sample(items), [mod_id], "high", "matfix" if rsz else ""))
    if match.loose:
        items = [f for f, _rel in match.loose]
        out.append(Finding("faulty_loose", f"REFramework가 읽지 못한 파일: 매니저가 넣지 않은 '{match.loose[0][1]}'{more(items)}"
                           " → 직접 넣은 모드 파일이면 빼거나 매니저로 설치해 보세요", sample(items), [], "medium"))
    for rel, items in match.paks.items():
        out.append(Finding("faulty_pak", f"REFramework가 읽지 못한 파일 {items[0].name}{more(items)}이(가) 매니저가 넣지 않은 PAK "
                           f"'{rel}'에 있습니다 → 직접 넣은 PAK이면 빼고 다시 해 보세요", sample(items), [], "medium"))
    if match.unknown:
        items = match.unknown
        out.append(Finding("faulty_game", f"REFramework가 읽지 못한 게임 파일 {len(items)}개({items[0].name} 등)는 켜져 있는 "
                           "모드에 없습니다. 모드 없이도 기록되는 경우가 있어 원인이 아닐 수 있습니다.", sample(items), [], "low"))
    if match.unchecked:
        out.append(Finding("faulty_more", f"REFramework가 읽지 못한 파일이 너무 많아 {len(match.unchecked)}개는 PAK과 게임 "
                           "폴더에서 찾아보지 않았습니다.", sample(match.unchecked), [], "info"))
    for rel in match.unreadable:
        out.append(Finding("faulty_unread", f"PAK '{rel}'을 읽지 못해 그 안의 파일은 확인하지 못했습니다.", [], [], "info"))
    return out


def _lua_findings(info: LogInfo, bases: Sequence[str], index: ModIndex) -> list[Finding]:
    out = []
    if info.stuck_script:
        rel = game_relative(info.stuck_script, bases)
        owners = [index.owner(rel)] if rel and index.owner(rel) else index.owners_by_tail(info.stuck_script)
        name = module_name(info.stuck_script)
        out.append(Finding("script", f"스크립트 '{name}'를 읽는 중에 멈췄습니다"
                           + _suspect(index, owners, "매니저가 설치하지 않은 스크립트입니다"),
                           [info.stuck_line], owners, "high" if info.stuck_sure else "medium"))
    by_script: dict[str, list] = {}
    for e in info.lua_errors:
        by_script.setdefault(norm_path(e.path).removeprefix("..."), []).append(e)
    for _key, errors in by_script.items():
        path = errors[0].path
        rel = game_relative(path, bases) if not path.startswith("...") else None
        owners = [index.owner(rel)] if rel and index.owner(rel) else index.owners_by_tail(path)
        count = f" {len(errors)}번" if len(errors) > 1 else ""
        out.append(Finding("lua", f"스크립트 '{module_name(path)}'에서 오류가{count} 났습니다" + _suspect(index, owners),
                           [e.raw for e in errors[:3]], owners, "medium"))
    if info.hook_errors:
        out.append(Finding("hooks", f"Lua 스크립트가 게임에 없는 함수를 고치려 했습니다({info.hook_errors}번). 게임 업데이트 뒤 "
                           "오래된 스크립트일 수 있습니다.", [], [], "low"))
    return out


_PLUGIN_REASONS = {
    "load": "REFramework가 플러그인 '{}'을 읽지 못했습니다(필요한 파일이 없거나 손상됨)",
    "major": "플러그인 '{}'이 지금 REFramework 버전과 맞지 않아 꺼졌습니다 → 모드나 REFramework를 업데이트해 보세요",
    "minor": "플러그인 '{}'이 더 새 REFramework를 필요로 해 꺼졌습니다 → REFramework를 업데이트해 보세요",
    "game": "플러그인 '{}'은 다른 게임용이라 꺼졌습니다",
    "init": "플러그인 '{}'이 시작하지 못했습니다",
    "exception": "플러그인 '{}'이 시작하다가 오류를 냈습니다",
}


def _plugin_owner(name: str, index: ModIndex) -> list[str]:
    stem = module_name(name)
    stem = stem[:-4] if stem.lower().endswith(".dll") else stem
    owner = index.owner(f"reframework/plugins/{stem}.dll")
    return [owner] if owner else index.owners_by_tail(f"/reframework/plugins/{stem}.dll")


def _setup_findings(info: LogInfo, bases: Sequence[str], index: ModIndex) -> list[Finding]:
    out = []
    for e in info.plugin_errors:
        owners = _plugin_owner(e.name, index)
        stem = module_name(e.name)
        name = stem if stem.lower().endswith(".dll") else f"{stem}.dll"
        out.append(Finding("plugin_load", _PLUGIN_REASONS.get(e.reason, _PLUGIN_REASONS["init"]).format(name)
                           + (f" (의심되는 모드 {_mods_text(index, owners)})" if owners else ""),
                           [e.line], owners, "medium", "update_refw" if e.reason in ("major", "minor") else ""))
    for path in info.paks_failed:
        rel = game_relative(path, bases)
        owner = index.owner(rel)
        out.append(Finding("pak_failed", f"PAK '{module_name(path)}'이 게임에 적용되지 않았습니다"
                           + _suspect(index, [owner] if owner else [], "매니저가 넣지 않은 PAK입니다"),
                           [f"pak_load_check_function_hook returned false for string: {path}"],
                           [owner] if owner else [], "high"))
    for name in info.paks_slot_taken:
        out.append(Finding("pak_slot", f"PAK '{name}'을 넣을 자리가 이미 게임 파일로 차 있어 적용되지 않았습니다 → REFramework를 "
                           "업데이트해 보세요", [f"slot … for '{name}' is already occupied by a real pak"], [], "medium",
                           "update_refw"))
    if info.paks_none:
        out.append(Finding("pak_none", f"pak_mods의 PAK {info.paks_none}개가 하나도 적용되지 않았습니다. REFramework가 이 게임 "
                           "버전에 맞지 않을 수 있습니다 → REFramework를 최신 빌드로 업데이트해 보세요",
                           [f"{info.paks_none} custom pak(s) were cached but NONE were injected"], [], "medium",
                           "update_refw"))
    if info.paks_out_of_range:
        out.append(Finding("pak_none", "pak_mods의 PAK 일부가 적용되지 않았습니다(번호가 범위를 벗어남) → REFramework를 "
                           "업데이트해 보세요", info.paks_out_of_range[:3], [], "medium", "update_refw"))
    if info.paks_disabled and index.has_paks():
        out.append(Finding("pak_disabled", "REFramework 설정에서 pak_mods 읽기가 꺼져 있어 PAK 모드가 적용되지 않았습니다.",
                           ["Pak directory loading is disabled, skipping it."], [], "medium"))
    if info.loose_failed and index.has_loose():
        out.append(Finding("loose_failed", "REFramework가 이 게임 버전에서 루스 파일(natives) 읽기를 켜지 못해 루스 모드가 "
                           "적용되지 않았습니다 → REFramework를 최신 빌드로 업데이트해 보세요", [info.loose_failed], [], "medium",
                           "update_refw"))
    if info.init_errors:
        out.append(Finding("init", "REFramework가 이 게임 버전에서 제대로 시작하지 못했습니다 → REFramework를 최신 빌드로 "
                           "업데이트해 보세요", info.init_errors[:3], [], "medium", "update_refw"))
    if info.present_errors:
        names = {"-7785fffb": "DEVICE_REMOVED", "-7785fffa": "DEVICE_HUNG", "-7785fff9": "DEVICE_RESET"}
        shown = ", ".join(names.get(c, c) for c in info.present_errors[:3])
        out.append(Finding("present", f"그래픽 오류가 기록되어 있습니다({shown}). 모드의 모델·텍스처·재질 파일이나 그래픽 "
                           "플러그인, 드라이버·VRAM 부족·오버레이 때문일 수 있습니다.",
                           [f"Present failed: {c}" for c in info.present_errors[:3]], [], "low"))
    return out


def _material_findings(index: ModIndex) -> list[Finding]:
    out = []
    for m in index.mods.values():
        if not index.was_on(m):
            continue
        if m.stuck:
            shown = ", ".join(m.stuck[:2]) + (f" 외 {len(m.stuck) - 2}개" if len(m.stuck) > 2 else "")
            out.append(Finding("stuck", f"'{m.name}'의 파일 {len(m.stuck)}개({shown})는 이 게임 버전에 맞게 고치지 못했습니다. "
                               "게임이 멈추거나 튕길 수 있습니다", ["재질 점검 결과"], [m.id], "medium"))
        elif m.material_state == "stale":
            out.append(Finding("material", f"'{m.name}'에 이 게임 버전과 맞지 않는 재질·데이터 파일이 있습니다 → 재질 점검·복구를 "
                               "해 보세요", ["재질 점검 결과"], [m.id], "medium", "matfix"))
    return out


def _accessed_findings(session: Session | None, index: ModIndex) -> list[Finding]:
    """The mods whose files the game asked for in the last seconds before the crash (REFramework's record of
    the files the game read; only mods with game files - natives\\ or PAKs - show in it)."""
    from . import accessed

    when = accessed.crash_time(session)
    found = accessed.session_exposure(session, index, (), tail_only=True) if when is not None else None
    if found is None or not found.tail:
        return []
    shown = list(found.tail)[:4]
    evidence = [f"튕기기 {secs:.1f}초 전: {path}" for secs, path, mod in found.tail_files[:3]]
    if found.exclusive:
        k = found.tail[found.exclusive]
        return [Finding("accessed", f"튕기기 직전 {accessed.TAIL_SECONDS:.0f}초 동안 게임이 읽은 모드 파일 {k}개가 모두 "
                        f"{index.label(found.exclusive)}의 것입니다" + _suspect(index, [found.exclusive]), evidence,
                        [found.exclusive], "high")]
    if len(found.tail) == 1:
        return [Finding("accessed_some", f"튕기기 직전 {accessed.TAIL_SECONDS:.0f}초 동안 게임이 "
                        f"{index.label(shown[0])}의 파일 1개를 읽었습니다", evidence, shown, "low")]
    return [Finding("accessed_some", f"튕기기 직전 {accessed.TAIL_SECONDS:.0f}초 동안 게임이 여러 모드의 파일을 읽었습니다: "
                    + _mods_text(index, shown) + (f" 외 {len(found.tail) - len(shown)}개" if len(found.tail) > 4 else ""),
                    evidence, shown, "low")]


def _bases(index: ModIndex, info: LogInfo | None, session: Session | None) -> list[str]:
    bases = [info.game_path if info else "", str(index.game_dir) if index.game_dir else "",
             session.info.get("source", "") if session else ""]
    return [b for b in dict.fromkeys(bases) if b]


def _sorted(findings: list[Finding], lead: Sequence[Finding] = ()) -> tuple[list[Finding], list[str]]:
    """``lead`` (where it crashed, or that nothing was recorded) first, then the strongest first."""
    order = sorted(range(len(findings)), key=lambda i: (_LEVELS.get(findings[i].level, 9), i))
    findings = [*lead, *(findings[i] for i in order)]
    suspects = list(dict.fromkeys(m for f in sorted(findings, key=lambda f: _LEVELS.get(f.level, 9))
                                  for m in f.mods if m))
    return findings, suspects


def explain(session: Session | None, index: ModIndex, builds=None, now: float | None = None,
            running: bool = False) -> Explanation:
    """What a session's records point at, as findings for the 게임 튕김 기록 window: matched to the
    mods as they were in the session (ModIndex.for_session). ``builds`` (refw.Build, optional) name the
    REFramework build ("nightly 01432"); ``running``: the session is the game running now. Reads paks
    in the game folder for files the game could not load, so run it off the GUI thread."""
    index = index.for_session(session)
    info = session.log() if session is not None else None
    findings: list[Finding] = []
    lead: list[Finding] = []
    crash = info.crash if info else None
    report = session.game_report() if session is not None else None
    bases = _bases(index, info, session)
    if crash is not None:
        located = _crash_findings(crash, bases, index)
        lead, findings = located[:1], located[1:]
    if report is not None:
        findings += _game_report_findings(report, bases, index, session)
    if info is not None:
        findings += _lua_findings(info, bases, index)
        findings += _setup_findings(info, bases, index)
    faulty = session.faulty() if session is not None else []
    if faulty:
        findings += _faulty_findings(index.match_faulty(faulty, session_loose(session)), index)
    findings += _accessed_findings(session, index)
    if session is None:
        lead.append(Finding("no_record", NO_RECORD))
    elif session.dump_only and report is None:
        lead.append(Finding("dump_only", "크래시 덤프만 남아 있습니다. 그 뒤 게임을 다시 실행해 REFramework 기록이 "
                                "지워졌습니다. 튕긴 뒤에는 매니저에서 게임을 실행하면 기록을 먼저 챙깁니다.", [], [], "info", "bisect"))
    elif info is None and report is None:
        lead.append(Finding("no_record", NO_RECORD))
    elif crash is None and running:
        lead.append(Finding("running", "게임이 실행 중입니다. 이 기록은 게임을 끈 뒤에 완성됩니다.", [], [], "info"))
    elif crash is None and report is not None:
        lead.append(_report_finding(report))
    elif crash is None:
        last = info.last_lines[-2:]
        lead.append(Finding("no_crash", "REFramework 기록에는 오류가 없습니다. 게임이 멈춰서 강제로 껐거나 게임 자체 오류 "
                                "창이 떴다면 기록이 남지 않습니다. 오류 창이 떴다면 문구를 붙여 넣어 주세요.",
                                [f"마지막 기록: {line}" for line in last], [], "info"))
    if report is not None and report.device_error:  # (said better by the report)
        findings = [f for f in findings if f.kind != "present"]
    findings += _material_findings(index)
    findings, suspects = _sorted(findings, lead)
    code = crash.code if crash else (report.code if report else "")
    category = module_place(crash.place, bases)[0] if crash else (session.summary.get("category", "") if report else "")
    return Explanation(summary=_summary(session, info, builds, now, running), findings=findings, suspects=suspects,
                       crashed=crash is not None or report is not None or bool(session and session.dump_only),
                       code=code, category=category, has_log=info is not None)


def _report_finding(report: crashlog.GameReport) -> Finding:
    """The lead finding of a session the game's own crash reporter recorded (REFramework saw no error)."""
    evidence = [e for e in (report.message, f"예외 코드: {exception_label(report.code)}" if report.code else "") if e]
    if report.device_error:
        return Finding("game_report", f"그래픽 오류({report.device_error})로 게임이 멈췄고, 게임이 스스로 오류 보고서"
                       "(CrashReport)를 만든 뒤 종료했습니다. 이런 오류는 REFramework 기록에 남지 않습니다.", evidence,
                       [], "info")
    return Finding("game_report", "게임이 스스로 오류를 잡아 오류 보고서(CrashReport)를 만든 뒤 종료했습니다. 이런 오류는 "
                   "REFramework 기록에 남지 않습니다.", evidence, [], "info")


def _game_report_findings(report: crashlog.GameReport, bases: list[str], index: ModIndex,
                          session: Session | None) -> list[Finding]:
    """What the game's own report points at: its error message (a graphics device error, a corrupted pak ...)
    and, when the error happened outside the game's own code, that module."""
    out = explain_text(report.message, index, session) if report.message else []
    top = report.modules[0] if report.modules else ""
    if top and module_place(top, bases)[0] not in ("game", "system", "unknown"):
        out.append(crash_finding(top, bases, index, [f"오류 보고서의 첫 호출: {top}"]))
    return out


def _summary(session: Session | None, info: LogInfo | None, builds, now, running: bool = False) -> str:
    if session is None:
        return ""
    crash = info.crash if info else None
    report = session.summary.get("game_report")
    if crash is not None:
        parts = [f"{_when(_epoch(crash.time) or session.end_time, now)} 종료", f"오류 코드 {exception_label(crash.code)}"]
    elif report:
        parts = [f"{_when(session.summary.get('crash_time') or session.end_time, now)} 종료"]
        if session.summary.get("device_error"):
            parts.append(f"그래픽 오류 ({session.summary['device_error']})")
        elif session.summary.get("crash"):
            parts.append(f"오류 코드 {exception_label(session.summary['crash'])}")
        parts.append("게임 오류 보고서")
    elif session.dump_only:
        parts = [f"{_when(session.end_time, now)} 종료", "크래시 덤프만 남음"]
    else:
        parts = [f"{_when(session.end_time, now)} 마지막 기록", "게임 실행 중" if running else "오류 기록 없음"]
    label = refw_label(info.header, builds) if info is not None else ""
    if label:
        parts.append(f"REFramework {label}")
    return " · ".join(parts)


def explain_text(text: str, index: ModIndex, session: Session | None = None) -> list[Finding]:
    """Findings from text the user pasted from an error window (the game's, REFramework's or the
    Windows event log's); ``session`` adds the pak names REFramework gave the mods' paks (and the mods
    as they were in it)."""
    index = index.for_session(session)
    found = crashlog.read_error_text(text)
    info = session.log() if session is not None else None
    bases = _bases(index, info, session)
    out: list[Finding] = []
    injected = {k.casefold(): v for k, v in (info.paks_injected if info else {}).items()}
    for pak in found.corrupted_paks:
        real = injected.get(module_name(pak).casefold())
        rel = game_relative(real, bases) if real else norm_path(pak).lstrip("/")
        owner = index.owner(rel) or index.owner(f"{PAK_MODS_DIR}/{module_name(pak)}")
        evidence = [f"file looks corrupted: {pak}"] + ([f"REFramework: {pak} = {real}"] if real else [])
        if owner:
            out.append(Finding("corrupted_pak", f"게임이 PAK '{module_name(real or pak)}'을 손상된 파일로 보았습니다"
                               + _suspect(index, [owner]), evidence, [owner], "high"))
        else:
            out.append(Finding("corrupted_pak", f"게임이 PAK '{module_name(pak)}'을 손상된 파일로 보았습니다. 게임 업데이트 뒤 "
                               "REFramework가 오래됐거나, 예전 모드 PAK이 남아 있거나, 게임 파일이 손상됐을 수 있습니다 → "
                               "REFramework 업데이트와 Steam의 '게임 파일 무결성 확인'을 해 보세요", evidence, [], "medium",
                               "update_refw"))
    for number, name, hresult, reason in found.d3d:
        evidence = [f"Fatal D3D error ({number}, {name}, {hresult})"
                    + (f" DeviceRemovedReason({reason})" if reason else "")]
        removed = crashlog.DXGI_ERRORS.get(reason.removeprefix("0x"), "") if reason else ""
        error = removed or name.removeprefix("DXGI_ERROR_")
        if name in crashlog.D3D_OUT_OF_MEMORY or removed == "E_OUTOFMEMORY":
            out.append(Finding("d3d", "그래픽 메모리(VRAM)가 모자라 게임이 멈췄습니다 → 그래픽 설정이나 고해상도 텍스처 모드를 "
                               "줄여 보세요", evidence, [], "info"))
        elif error in ("DEVICE_HUNG", "DEVICE_RESET"):  # ("badly formed commands": Microsoft's DXGI documentation)
            out.append(Finding("d3d", f"게임이 그래픽카드에 보낸 그리기 작업을 그래픽카드가 끝내지 못해({error}) Windows가 "
                               "그래픽카드를 다시 시작했고, 그래서 게임이 멈췄습니다. 그래픽카드가 고장 났다는 뜻은 대개 아닙니다. "
                               "잘못 만들어진 작업 때문인데, 모드를 쓰면 모드의 모델·텍스처·재질·셰이더 파일(특히 게임 업데이트 "
                               "전에 만든 것)이나 그래픽 플러그인(ReShade·업스케일러 등) 때문인 경우가 많고, 그래픽 드라이버, GPU "
                               "오버클럭·언더볼트, 프레임 생성, 화면 위 오버레이(녹화·FPS 표시)도 원인일 수 있습니다 → 재질 "
                               "점검·복구를 해 보고 원인 모드 찾기로 좁혀 보세요. 모든 모드를 꺼도 튕기면 그래픽 드라이버와 설정을 "
                               "확인해 보세요", evidence, [], "info", "bisect"))
        elif error == "DRIVER_INTERNAL_ERROR":
            out.append(Finding("d3d", "그래픽 드라이버 안에서 문제가 생겨(DRIVER_INTERNAL_ERROR) 게임이 멈췄습니다 → 먼저 그래픽 "
                               "드라이버를 새로 설치해 보세요. 모드의 모델·텍스처·재질 파일이나 그래픽 플러그인 때문일 수도 있으니, "
                               "그래도 튕기면 원인 모드 찾기로 좁혀 보세요", evidence, [], "info", "bisect"))
        else:
            out.append(Finding("d3d", f"그래픽 오류({name})로 게임이 멈췄습니다. 모드의 모델·텍스처·재질 파일이나 그래픽 플러그인, "
                               "드라이버·오버레이·프레임 생성 설정이 원인일 수 있어 기록만으로는 모드를 알 수 없습니다.", evidence, [],
                               "info", "bisect"))
    for where in found.unhandled:
        out.append(Finding("unhandled", "게임 본체에서 처리되지 않은 오류가 났습니다. 기록만으로는 어떤 모드인지 알 수 없습니다. "
                           "시작하자마자 난다면 REFramework가 게임 버전에 맞지 않거나 pak_mods의 PAK이 너무 많을 수 있습니다.",
                           [f"unhandled exception {where}"], [], "info", "bisect"))
    for module in found.modules:
        code = found.codes[0] if found.codes else ""
        out.append(crash_finding(module, bases, index, [f"오류 모듈: {module}"]
                                 + ([f"예외 코드: {exception_label(code)}"] if code else [])))
    if found.codes and not found.modules:
        out.append(Finding("code", f"오류 코드 {exception_label(found.codes[0])}. 어느 파일에서 났는지는 이 문구만으로는 알 수 "
                           "없습니다.", [f"예외 코드: {exception_label(c)}" for c in found.codes[:3]], [], "info"))
    if found.faulty:
        out += _faulty_findings(index.match_faulty(found.faulty, session_loose(session)), index)
    for path, line in found.scripts:
        owners = index.owners_by_tail(path)
        out.append(Finding("lua", f"스크립트 '{module_name(path)}'에서 오류가 났습니다" + _suspect(index, owners),
                           [f"{path}:{line}"], owners, "medium"))
    if not out and (found.aborted or found.gpu_crash):
        out.append(Finding("d3d" if found.gpu_crash else "unhandled", "게임이 자체 오류 창을 띄웠습니다. 문구에 파일 이름이나 오류 "
                           "번호가 없어 원인을 알 수 없습니다.", [], [], "info", "bisect"))
    return _sorted(out)[0]


# ---- the support zip ---------------------------------------------------------------------------------
@dataclass
class ReportMod:
    """One mod in report.txt (in load order)."""
    name: str
    enabled: bool
    kinds: str = ""  # "PAK+루스"
    version: str = ""
    nexus_id: int = 0
    options: Sequence[str] = ()  # names of the chosen options
    material: str = ""  # material_state
    missing: Sequence[str] = ()  # names of missing requirements


def report_text(*, app_version: str, game_dir=None, game_version: str = "", steam: dict | None = None, refw: str = "",
                profile: str = "", mods: Sequence[ReportMod] = (), verify: Sequence[str] = (),
                session: Session | None = None, explanation: Explanation | None = None,
                extra: Sequence[str] = ()) -> str:
    """report.txt of the support zip (masked when written)."""
    lines = [f"몬스터 헌터 와일즈 모드 매니저 {app_version} 진단 정보", REPORT_NOTE,
             f"만든 때: {datetime.now():%Y-%m-%d %H:%M}", "",
             f"게임 폴더: {game_dir or '-'}", f"게임 버전(파일 지문): {game_version or '-'}"]
    if steam:
        lines.append(f"Steam 빌드: {steam.get('buildid', '-')} (마지막 업데이트 {steam.get('LastUpdated', '-')})")
    lines += [f"REFramework: {refw or '-'}", f"프로필: {profile or '-'}", ""]
    lines.append("[게임 기록]")
    if session is not None:
        origin = "매니저에서 실행" if session.launched else "매니저 밖에서 실행했거나 실행 기록 없음"
        lines.append(f"기록: {session.id} ({origin}, 원본 폴더 {session.info.get('source', '-')})")
    lines += (explanation.lines() if explanation else [NO_RECORD]) + [""]
    if session is not None and session.context.get("mods"):
        on = [m.get("name", "?") for m in session.context["mods"] if isinstance(m, dict) and m.get("enabled")]
        when = "실행할 때" if session.launched else "기록을 모을 때"
        lines += [f"[이 기록의 {when} 켜져 있던 모드 {len(on)}개, 프로필 {session.context.get('profile', '-')}]",
                  ", ".join(on) or "-", ""]
    lines.append(f"[모드 {len(mods)}개, 로드 순서]")
    for i, m in enumerate(mods, 1):
        parts = [f"{i:3d}. [{'켜짐' if m.enabled else '꺼짐'}] {m.name}"]
        parts += [p for p in (m.version and f"v{m.version}", m.kinds, m.nexus_id and f"Nexus {m.nexus_id}") if p]
        if m.options:
            parts.append("옵션: " + ", ".join(m.options))
        if m.material and m.material != "none":
            parts.append(f"재질: {m.material}")
        if m.missing:
            parts.append("없는 필수 모드: " + ", ".join(m.missing))
        lines.append("  ".join(parts))
    lines += ["", "[적용 상태 점검]"] + ([f"- {p}" for p in verify] or ["- 문제 없음"])
    if extra:
        lines += ["", *extra]
    return "\n".join(lines) + "\n"


def _masked(path: Path, max_bytes: int = MAX_LOG_COPY) -> str:
    return mask_user(crashlog.read_text(path, max_bytes))


def _accessed_tail(s: Session, lines: int = REPORT_ACCESSED_LINES) -> str:
    """The last lines of a session's record of the files the game read ("" without one)."""
    path = s.file(ACCESSED_COPY)
    if path is None:
        return ""
    kept: deque = deque(maxlen=lines)
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                kept.append(line.rstrip("\r\n"))
    except (OSError, EOFError, zlib.error) as exc:
        log.info("게임이 읽은 파일 기록을 읽지 못했습니다 (%s): %s", s.id, exc)
    return "\n".join(kept) + "\n" if kept else ""


def write_report(target, text: str, records: Sequence[Session] = (), *, manager_log=None,
                 deployed: dict | None = None, names: dict[str, str] | None = None, include_dump: bool = False) -> Path:
    """The support zip: report.txt, each session's copied records (``<id>/``; the dump only with
    ``include_dump``), the last lines of manager.log and the deployed files with their mods. User
    names in paths are masked; state.json is never included."""
    target = Path(target)
    partial = target.with_name(target.name + ".partial")
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("report.txt", mask_user(text))
            for s in records:
                for name in TEXT_FILES:
                    path = s.folder / name
                    if path.is_file():
                        zf.writestr(f"{s.id}/{name}", _masked(path))
                if include_dump and s.dump_path is not None:
                    zf.write(s.dump_path, f"{s.id}/{DUMP_FILE}")
                tail = _accessed_tail(s)
                if tail:
                    zf.writestr(f"{s.id}/{ACCESSED_TAIL}", mask_user(tail))
            if manager_log and Path(manager_log).is_file():
                tail = Path(manager_log).read_text(encoding="utf-8", errors="replace").splitlines()[-REPORT_LOG_LINES:]
                zf.writestr("manager.log", mask_user("\n".join(tail) + "\n"))
            if deployed:
                rows = sorted((str(e.get("path", k)), (names or {}).get(e.get("mod", ""), e.get("mod", "")))
                              for k, e in deployed.items() if isinstance(e, dict))
                zf.writestr("deployed.txt", mask_user("".join(f"{p}\t{m}\n" for p, m in rows)))
            if any(i.filename.rsplit("/", 1)[-1].lower() == "state.json" for i in zf.infolist()):
                raise RuntimeError("state.json must never be in a report")
        os.replace(partial, target)
    except BaseException:
        fsutil.remove_file(partial)
        raise
    return target
