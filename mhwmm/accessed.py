"""REFramework's record of the files the game asked for (``reframework_accessed_files.txt``).

REFramework (praydog, MIT) writes it when ``LooseFileLoader_LogAccessedFiles`` is on (src/mods/
LooseFileLoader.cpp): spdlog's default lines ``[YYYY-MM-DD hh:mm:ss.mmm] [LooseFileLoader] [info] <path>``
in local time (the same clock as re2_framework_log.txt), flushed line by line so the lines before a
crash survive it.  The file is truncated at every start and begins with ``LooseFileLoader constructed``
(written even with logging off).  A path is logged once per thread on its first request, and only
while ``LooseFileLoader_Enabled`` is on; it is the UTF-8 of the path the game hashes for its pak
lookup (``natives/STM/<path>.<ext>.<version>`` as far as the source shows - not seen in a real file
yet, so everything here is tolerant: a form that maps to no mod gives no evidence, never
"never requested").

This module reads the file (and the manager's gzip copy of it in a crash record), maps the requests
to the mods that were on (the loose file the game read, else the pak REFramework gave the highest
patch number), and turns the logging setting on and back off with the user's consent while the game
is closed - the only REFramework setting 원인 모드 찾기 changes.
"""

from __future__ import annotations

import gzip
import logging
import math
import os
import sys
import threading
import time
import zlib
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import crashes, crashlog, fsutil
from .constants import PAK_MODS_DIR, REFRAMEWORK_DLL
from .crashlog import ACCESSED_FILE  # noqa: F401 (named here too; crashlog holds it, crashes copies it)

log = logging.getLogger(__name__)

COPY_NAME = crashes.ACCESSED_COPY  # ACCESSED_FILE + ".gz", in a crash session folder
LOG_KEY = "LooseFileLoader_LogAccessedFiles"
LOOSE_KEY = crashlog.LOOSE_FILES_KEY  # "LooseFileLoader_Enabled": nothing is logged without it
CONSTRUCTED = "LooseFileLoader constructed"
MAX_READ = 64 * 1024 * 1024  # a larger file: its first line, then the last MAX_READ bytes
TAIL_SECONDS = 10.0
COVER_SECONDS = 60.0  # logging must have run this long (before a crash, or in all) for "never requested"
REPORT_SLACK = 2.0  # the game's own crash report gives the crash time in whole seconds (its minidump) or 2 s (zip)
REPORT_SLACK_MINUTE = 60.0  # ... or only the minute (the report's file name)
COPY_SLACK = 5.0  # a copy made this long before its session ended was made while the game ran (only a part)
PARTIAL_GAP = 300.0  # a crash this long after the record's last line: its end is missing (copied while it ran)
MAX_PATHS = 400_000  # distinct paths kept (the rest: truncated=True)
MAX_OTHER = 20  # lines that are neither paths nor CONSTRUCTED, kept for report.txt
BELONGS_BEFORE = 5.0  # the record may start this long before its REFramework log (clock, rounding)
BELONGS_AFTER = 180.0  # LooseFileLoader is made at "VM initialized", after the REFramework log starts
RESTORE_FILE = "refw_restore.json"  # in the data folder: a restore left for when the game is closed
KEPT_FILE = "refw_logging.json"  # in the data folder: what the 게임 튕김 기록 window turned on (undone when turned off there)
RESTORE_FORMAT = 1
MAX_TAIL_FILES = 50
_EXT_LOOSE = "loose"
_EXT_PAK = "pak"

LOG_CHECKBOX = "게임이 읽는 파일을 기록해 더 정확하게 찾기"
LOG_NOTE = ("REFramework 설정(LooseFileLoader_LogAccessedFiles)을 켭니다. 튕기기 전에 어떤 모드의 파일을 읽었는지, 실행에서 "
            "의심되는 모드의 파일을 읽었는지 알 수 있습니다. 게임이 꺼져 있을 때만 바꾸고, 원인 찾기를 마치면 원래대로 "
            "되돌립니다. 게임 로딩이 조금 느려질 수 있습니다.")
LOG_NOTE_LOOSE = " 루스 파일 읽기(LooseFileLoader)도 함께 켭니다. natives 폴더에 파일이 없어 게임이 읽는 파일은 달라지지 않습니다."
LOG_UNAVAILABLE_LOOSE = ("루스 파일 읽기(LooseFileLoader)가 꺼져 있고 natives 폴더에 파일이 있어 켜지 않습니다. 켜면 그 파일을 "
                         "게임이 새로 읽어 튕기는 상황이 달라질 수 있습니다.")
LOG_NO_REFW = "REFramework가 없어 게임이 읽는 파일을 기록할 수 없습니다."
LOG_RUNNING = "게임이 실행 중이라 지금은 켤 수 없습니다."
LOG_ALREADY = "이미 켜져 있습니다 (REFramework 설정)."
CRASH_WINDOW_CHECKBOX = "게임이 읽는 파일도 기록하기 (REFramework 설정 변경, 게임이 꺼져 있을 때만 · 로딩이 조금 느려질 수 있음)"
RESTORED = "REFramework 파일 기록 설정을 원래대로 되돌렸습니다."
RESTORE_PENDING = "게임이 실행 중이라 REFramework 파일 기록 설정은 게임을 끈 뒤 되돌립니다."
RESTORE_FAILED = "REFramework 파일 기록 설정을 되돌리지 못했습니다 (다음에 다시 해 봅니다): {error}"
NOTE_OFF = "게임이 읽은 파일이 기록되지 않았습니다"
NOTE_TRUNCATED = "파일 기록이 너무 길어 일부만 남아 있어 읽지 않은 파일은 판단하지 않습니다"
NOTE_PARTIAL = "게임이 실행 중일 때 복사한 기록이라 일부만 있어 읽지 않은 파일은 판단하지 않습니다"
NOTE_NO_HITS = "기록에 모드 파일이 하나도 없어(기록 형식이 다를 수 있음) 판단하지 않습니다"
NOTE_COVER = "기록이 켜진 지 1분 안에 튕겨 읽은 파일로 판단하지 않습니다"


# ---- the file -----------------------------------------------------------------------------------
@dataclass
class AccessLog:
    started: float | None = None  # epoch of the CONSTRUCTED line (else the first timed line)
    paths: dict[str, float] = field(default_factory=dict)  # path (forward slashes, as logged) -> first request (epoch)
    lines: int = 0
    first_path: float | None = None  # when the first path was logged
    last_time: float | None = None  # the last timed line
    truncated: bool = False  # only a part was read (or kept): what is missing is not known
    partial: bool = False  # the copy was made while the game still ran (its end is missing): the same
    other: list[str] = field(default_factory=list)  # the first lines that are neither paths nor CONSTRUCTED
    # pak hash halves of paths already worked out (exposure runs more than once on a record)
    _hashes: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def logging(self) -> bool:
        """Paths were logged (else logging was off, or the loose file loader was off or not hooked)."""
        return bool(self.paths)


def _path(text: str) -> str | None:
    """A logged line as a path: forward slashes, no quotes or leading "@", an absolute prefix cut at
    ``natives/``; None for a line that is not a path."""
    p = text.strip().strip("\"'").replace("\\", "/").lstrip("@")
    if not p:
        return None
    low = p.lower()
    i = low.find("natives/")
    if i > 0 and p[i - 1] == "/":
        p, low, i = p[i:], low[i:], 0
    name = p.rsplit("/", 1)[-1]
    if "/" not in p or "." not in name.strip("."):
        return None
    if i != 0 and (" " in p or "\t" in p):  # (messages have spaces; a game path outside natives/ has none)
        return None
    return p


class _Clock:
    """Time stamps as epoch seconds; each whole second is parsed once (strptime is slow)."""

    def __init__(self):
        self.seconds: dict[str, float | None] = {}

    def __call__(self, stamp: str) -> float | None:
        whole, _, frac = stamp.partition(".")
        if whole not in self.seconds:
            t = crashlog.parse_time(whole)
            self.seconds[whole] = t.timestamp() if t is not None else None
        base = self.seconds[whole]
        if base is None:
            return None
        return base + (int(frac) / 10 ** len(frac) if frac.isdigit() else 0.0)


def parse(text_or_lines) -> AccessLog:
    """Everything in the file (``str`` or an iterable of lines): the first time each path was asked for."""
    lines = text_or_lines.splitlines() if isinstance(text_or_lines, str) else text_or_lines
    out = AccessLog()
    clock = _Clock()
    head = crashlog.HEAD
    paths = out.paths
    previous = first_timed = constructed = None  # (an untimed line gets the previous line's time)
    first = True
    for raw in lines:
        line = raw.rstrip("\r\n")
        if first:
            line, first = line.lstrip("\ufeff"), False
        if not line.strip():
            continue
        out.lines += 1
        m = head.match(line)
        t = clock(m.group(1)) if m else None
        if t is not None:
            previous = out.last_time = t
            first_timed = t if first_timed is None else first_timed
        else:
            t = previous
        message = (m.group(5) if m else line).strip()
        if message == CONSTRUCTED:
            constructed = t if constructed is None else constructed
            continue
        p = _path(message)
        if p is None:
            if len(out.other) < MAX_OTHER:
                out.other.append(message[:300])
            continue
        if p in paths:
            continue  # (the same path from another thread: the first time wins)
        if len(paths) >= MAX_PATHS:
            out.truncated = True
            continue
        paths[p] = t if t is not None else -math.inf
        if out.first_path is None and t is not None:
            out.first_path = t
    out.started = constructed if constructed is not None else first_timed  # (the first line may be cut off)
    return out


def read(path, max_bytes: int = MAX_READ) -> AccessLog:
    """The file as REFramework left it, or the manager's gzip copy (``.gz``); an empty AccessLog for a file
    that cannot be read. A larger file is read from its end (the lines before a crash matter most)."""
    p = Path(path)
    try:
        if p.suffix.lower() == ".gz":
            with gzip.open(p, "rt", encoding="utf-8", errors="replace", newline="") as f:
                return parse(f)
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size <= max_bytes:
                data = f.read()
                truncated = False
            else:
                first_line = f.readline(64 * 1024)  # the CONSTRUCTED line dates the session
                f.seek(size - max_bytes)
                f.readline()  # (a cut line)
                data = first_line.rstrip(b"\r\n") + b"\n" + f.read()
                truncated = True
    except (OSError, EOFError, ValueError, zlib.error) as exc:  # (a damaged gzip copy: BadGzipFile, EOFError...)
        log.info("게임이 읽은 파일 기록을 읽지 못했습니다 (%s): %s", p, exc)
        return AccessLog()
    out = parse(data.decode("utf-8", errors="replace"))
    out.truncated = out.truncated or truncated
    return out


def read_head(path, max_bytes: int = 64 * 1024) -> tuple[bytes, AccessLog] | None:
    """The record's first line (as written, without its line end) and its start parsed (enough to date it and
    to see whether a path follows CONSTRUCTED); None when it cannot be read."""
    try:
        with open(path, "rb") as f:
            head = f.read(max_bytes)
    except OSError:
        return None
    if len(head) == max_bytes and b"\n" in head:
        head = head.rsplit(b"\n", 1)[0]  # (a cut last line)
    return head.split(b"\n", 1)[0].rstrip(b"\r\n"), parse(head.decode("utf-8", errors="replace"))


def belongs(log_: AccessLog, session_started: float | None) -> bool:
    """The record was written in the game session whose REFramework log started at ``session_started``
    (LooseFileLoader is made a little after the log starts; the file is truncated at every start)."""
    if log_ is None or log_.started is None or session_started is None:
        return False
    return -BELONGS_BEFORE <= log_.started - session_started <= BELONGS_AFTER


# ---- pak hashes ------------------------------------------------------------------------------------------
_M = 0xFFFFFFFF


def _blocks(h: int, data: bytes) -> int:
    """Murmur3's body over whole 4-byte blocks (matfix.pak.murmur3_32)."""
    words = array("I")
    words.frombytes(data)
    if sys.byteorder != "little":
        words.byteswap()
    for k in words:
        k = (k * 0xCC9E2D51) & _M
        k = (((k << 15) | (k >> 17)) & _M) * 0x1B873593 & _M
        h ^= k
        h = ((((h << 13) | (h >> 19)) & _M) * 5 + 0xE6546B64) & _M
    return h


def _finish(h: int, tail: bytes, length: int) -> int:
    if tail:
        k = int.from_bytes(tail, "little")
        k = (k * 0xCC9E2D51) & _M
        h ^= (((k << 15) | (k >> 17)) & _M) * 0x1B873593 & _M
    h ^= length
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & _M
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & _M
    return h ^ (h >> 16)


class _Hasher:
    """matfix.pak.string_hash, faster for many paths in the same folders: the hash state after a folder is
    kept (murmur3 runs over the UTF-16 text in 4-byte blocks, so a prefix of an even length has one state)."""

    def __init__(self):
        self.states: dict[str, int] = {}

    def __call__(self, text: str) -> int:
        if not text.isascii():
            from .matfix.pak import string_hash

            return string_hash(text)
        data = text.encode("utf-16-le")
        cut = text.rfind("/") + 1
        cut -= cut & 1
        prefix = text[:cut]
        h = self.states.get(prefix)
        if h is None:
            h = self.states[prefix] = _blocks(0xFFFFFFFF, data[:2 * cut])
        n = len(data) & ~3
        return _finish(_blocks(h, data[2 * cut:n]), data[n:], len(data))


def pak_key(path: str) -> str:
    """The path a pak stores a logged request under: ``natives/STM/`` in front when it has no ``natives/``."""
    p = path.replace("\\", "/").lstrip("/")
    return p if p.lower().startswith("natives/") else "natives/STM/" + p


# ---- what the game read, per mod --------------------------------------------------------------------------
@dataclass
class Exposure:
    """What one session's record says of the mods that were on."""
    log: bool = False  # usable for at least one class of files (loose, pak)
    requested: dict[str, int] = field(default_factory=dict)  # mod id -> distinct files requested (until)
    unexposed: list[str] = field(default_factory=list)  # content mods on, never requested
    tail: dict[str, int] = field(default_factory=dict)  # mod id -> distinct files requested in the last TAIL_SECONDS
    exclusive: str = ""  # the tail's only mod when it has >= 2 files there
    note: str = ""  # why "never requested" was not judged ("" when it was)
    tail_files: list[tuple[float, str, str]] = field(default_factory=list)  # (seconds before the crash, path, mod)

    @property
    def files(self) -> int:
        return sum(self.requested.values())


class PakNames:
    """pak entry hash -> game path, from the bundled list of the game's equipment paths (equip.pathlist.PathIndex:
    built on first use - a few seconds - and kept in ``cache_dir``, shared with the equipment window). A pak mod is
    judged "never requested" only when every entry is named (so its file types are known). Thread-safe."""
    _shared: dict = {}
    _lock = threading.Lock()

    def __init__(self, cache_dir=None):
        self.cache_dir = Path(cache_dir) if cache_dir else None

    def _index(self):
        key = str(self.cache_dir)
        with PakNames._lock:
            if key not in PakNames._shared:
                try:
                    from .equip.pathlist import PathIndex

                    PakNames._shared[key] = PathIndex(self.cache_dir)
                except Exception as exc:  # (then no pak mod is judged)
                    log.warning("게임 파일 이름 목록을 만들지 못했습니다: %s", exc)
                    PakNames._shared[key] = None
            return PakNames._shared[key]

    def __call__(self, hash64: int) -> str | None:
        index = self._index()
        return index.lookup(hash64) if index is not None else None


def _pak_mods_name(path: str) -> str:
    """A pak's path under pak_mods (casefolded, forward slashes) from a game path or REFramework's full path; its
    file name when it has no pak_mods folder."""
    low = path.replace("\\", "/").casefold()
    folder = PAK_MODS_DIR.casefold() + "/"
    i = low.rfind("/" + folder)
    if i >= 0:
        return low[i + len(folder) + 1:]
    return low[len(folder):] if low.startswith(folder) else low.rsplit("/", 1)[-1]


def _loaded_paks(info) -> Callable[[str], bool]:
    """Whether REFramework put a pak of pak_mods (a game path) into the game, by its log of the session
    (crashlog.LogInfo; None: not known - as if it did). It did not with pak loading off ("Pak directory loading
    is disabled"), when it injected none (an outdated REFramework after a game update), for a pak the game
    refused or that found its slot taken, and for one it did not list among those it injected; the game then
    reads its own files for those paths. Paks outside pak_mods are the game's own to load."""
    if info is None:
        return lambda rel: True
    none = bool(getattr(info, "paks_disabled", False) or getattr(info, "paks_none", 0))
    refused = {_pak_mods_name(p) for p in getattr(info, "paks_failed", ())}
    taken = {str(p).replace("\\", "/").rsplit("/", 1)[-1].casefold() for p in getattr(info, "paks_slot_taken", ())}
    injected = {_pak_mods_name(p) for p in (getattr(info, "paks_injected", None) or {}).values()}

    def loaded(rel: str) -> bool:
        if not rel.replace("\\", "/").casefold().startswith(PAK_MODS_DIR.casefold() + "/"):
            return True
        name = _pak_mods_name(rel)
        if none or name in refused or name.rsplit("/", 1)[-1] in taken:
            return False
        return not injected or name in injected

    return loaded


class _Owners:
    """Logged path -> (mod, class) of the copy the game read: a loose file of a mod that was on wins over any
    pak (LooseFileLoader serves it first: only that very file - the same folder and version suffix); else the
    pak with the highest patch number, which maps to no mod when it is one no mod placed. A pak REFramework did
    not load (``refw``: the session's crashlog.LogInfo) is left out: no request is its, and its mod is not
    judged "never requested"."""

    def __init__(self, log_: AccessLog, index, on: set[str], refw=None):
        self.index = index
        self.loose = any(f.lower().startswith("natives/") for m in on if (ref := index.mods.get(m)) is not None
                         for f in ref.files)
        self.table: dict[int, list[tuple[int, int, str]]] = {}
        self.paks: dict[str, list] = {}  # mod -> its pak tables (for the file types of a pak mod)
        loaded = _loaded_paks(refw)
        for mod, rel, rank in index.pak_paths():
            if mod and mod not in on:
                continue
            if not loaded(rel):
                if mod:
                    self.paks.setdefault(mod, []).append(None)  # (its file types not known: not judged)
                continue
            pak = index.pak_file(rel)
            if pak is None:
                if mod:
                    self.paks.setdefault(mod, []).append(None)
                continue
            if mod:
                self.paks.setdefault(mod, []).append(pak)
            for h in pak.hashes:
                self.table.setdefault(h & _M, []).append((int(h), rank, mod))
        self.cache = log_._hashes
        self.hasher = _Hasher()

    def __call__(self, path: str) -> tuple[str, str]:
        if self.loose:
            owner = self.index.loose_path_owner(pak_key(path))
            if owner:
                return owner, _EXT_LOOSE
        if not self.table:
            return "", ""
        p = pak_key(path)
        low = self.cache.get(p)
        if low is None:
            low = self.cache[p] = self.hasher(p.lower())
        hits = self.table.get(low)
        if not hits:
            return "", ""
        up = self.cache.get("\0" + p)
        if up is None:
            up = self.cache["\0" + p] = self.hasher(p.upper())
        full = (up << 32) | low
        best = max(((rank, mod) for h, rank, mod in hits if h == full), default=None)
        if best is None or not best[1]:
            return "", ""
        return best[1], _EXT_PAK


def _classes(ref, paks=(), names: Callable[[int], str | None] | None = None) -> tuple[set[str], set[str]] | None:
    """(classes, file types) of a mod's files: "loose" for natives\\, "pak" for .pak files, with the types of the
    files in its paks named by ``names`` (pak hash -> path). None when they are not all known: a pak of it that
    cannot be read, or an entry no name is known for."""
    classes, types = set(), set()
    for f in ref.files if ref is not None else ():
        low = f.replace("\\", "/").lower()
        if low.startswith("natives/"):
            classes.add(_EXT_LOOSE)
            types.add(_type(low))
        elif low.endswith(".pak"):
            classes.add(_EXT_PAK)
    if _EXT_PAK in classes:
        if not paks or names is None:
            return None
        for pak in paks:
            if pak is None:
                return None
            for h in pak.hashes:
                name = names(int(h))
                if not name:
                    return None
                types.add(_type(name))
    return classes, types


def _type(path: str) -> str:
    """A file's type for "was this kind of file logged at all": its extension, "streaming/" in front for the
    streaming copy of a texture or mesh (asked for on its own path, perhaps by another loader)."""
    low = path.replace("\\", "/").lower()
    rest = low.split("/", 2)[2] if low.startswith("natives/") and low.count("/") >= 2 else low
    name = crashlog.resource_key(low).rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    return ("streaming/" if rest.startswith("streaming/") else "") + ext


def crash_slack(session) -> float:
    """How much later than its recorded time a session may have crashed: the game's own crash report gives whole
    seconds (its minidump, or 2 s: its zip) or only the minute (its file name); REFramework's handler
    milliseconds (0). A report kept before the manager noted where its time came from: a time on a whole minute
    is taken for the name's."""
    summary = session.summary if session is not None else {}
    if summary.get("crash_from") != "report":
        return 0.0
    source = summary.get("crash_time_from")
    if source:
        return REPORT_SLACK_MINUTE if source == "name" else REPORT_SLACK
    t = summary.get("crash_time")
    return REPORT_SLACK_MINUTE if isinstance(t, (int, float)) and t and float(t) % 60 == 0 else REPORT_SLACK


def exposure(log_: AccessLog | None, index, on: Iterable[str], content: Iterable[str],
             crash: float | None = None, tail_only: bool = False, slack: float = 0.0,
             names: Callable[[int], str | None] | None = None, refw=None) -> Exposure:
    """Which of the mods on (``on``, ids) the game asked for files of, in one session's record (``index``:
    crashes.ModIndex of that session, ModIndex.for_session). ``content``: the mods with only loose and pak
    files - only these can be "never requested" (unexposed), and only when the record is usable for every
    class and every file type they have (a pak's types: ``names``, pak hash -> path; without it pak mods are
    not judged); ``crash``: when the session crashed (epoch; ``slack``: it may have been this much later - a
    time in whole seconds); ``tail_only``: only the last TAIL_SECONDS before the crash (tail, exclusive: much
    faster). "Never requested" is judged over the whole record (a request after the crash only clears a mod
    of it), and only when logging ran at least COVER_SECONDS before the crash (or in all). ``refw``: the session's
    REFramework log (crashlog.LogInfo), for the pak_mods paks it did not load. Reads pak tables: off the GUI
    thread."""
    out = Exposure()
    if log_ is None or not log_.paths:
        out.note = NOTE_OFF
        return out
    if tail_only and crash is None:
        return out
    on = [m for m in dict.fromkeys(on) if m]
    on_set = set(on)
    owners = _Owners(log_, index, on_set, refw)
    tail_from = crash - TAIL_SECONDS if crash is not None else None
    tail_to = crash + max(0.0, slack) if crash is not None else None
    usable: set[str] = set()
    types: set[str] = set()  # file types the game asked for at all
    tail_files = []
    for path, t in log_.paths.items():
        if tail_only and not tail_from <= t <= tail_to:
            continue
        types.add(_type(path))
        owner, kind = owners(path)
        if not owner or owner not in on_set:
            continue
        usable.add(kind)
        out.requested[owner] = out.requested.get(owner, 0) + 1
        if tail_from is not None and tail_from <= t <= tail_to:
            out.tail[owner] = out.tail.get(owner, 0) + 1
            tail_files.append((max(0.0, crash - t), path, owner))
    tail_files.sort(key=lambda x: (x[0], x[1]))
    out.tail_files = tail_files[:MAX_TAIL_FILES]
    out.tail = dict(sorted(out.tail.items(), key=lambda kv: (-kv[1], on.index(kv[0]))))
    if len(out.tail) == 1:
        (only, count), = out.tail.items()
        out.exclusive = only if count >= 2 else ""
    if tail_only:
        out.requested = {}
        return out
    if log_.truncated or log_.partial:
        out.note = NOTE_TRUNCATED if log_.truncated else NOTE_PARTIAL
        return out
    if not usable:
        out.note = NOTE_NO_HITS
        return out
    out.log = True
    end = crash if crash is not None else log_.last_time  # (a crash not detected - a freeze ended by hand: its end)
    if end is None or log_.first_path is None or log_.first_path > end - COVER_SECONDS:
        out.note = NOTE_COVER
        return out
    content = set(content)
    for m in on:
        if m not in content or m in out.requested:
            continue
        found = _classes(index.mods.get(m), owners.paks.get(m, ()), names)
        if found is None:
            continue
        classes, kinds = found
        if classes and classes <= usable and kinds <= types:
            out.unexposed.append(m)
    return out


def session_on(session) -> list[str] | None:
    """The mods that were on (and applied) in a session, from its context (None: not known)."""
    context = session.context if session is not None else {}
    mods = context.get("mods")
    if not isinstance(mods, list):
        return None
    placed = context.get("placed")
    placed = set(i for i in placed if isinstance(i, str)) if isinstance(placed, list) else None
    return [m["id"] for m in mods if isinstance(m, dict) and isinstance(m.get("id"), str) and m.get("enabled")
            and (placed is None or m["id"] in placed)]


def crash_time(session) -> float | None:
    """When a session crashed (epoch; None when it did not, as far as its records say)."""
    if session is None or session.problem() != "crash":
        return None
    t = session.summary.get("crash_time")
    return float(t) if isinstance(t, (int, float)) and t else None


def _refw_log(session):
    """The session's REFramework log (crashlog.LogInfo; None when it has none or it cannot be read)."""
    try:
        return session.log() if session is not None else None
    except Exception as exc:  # (only for the paks it did not load)
        log.debug("REFramework 기록을 읽지 못했습니다: %s", exc)
        return None


def session_exposure(session, index, content: Iterable[str], on: Iterable[str] | None = None,
                     tail_only: bool = False, names: Callable[[int], str | None] | None = None) -> Exposure | None:
    """Exposure of a kept session (a crash record) with the mods that were on in it (``on`` overrides);
    None when it has no record of the files the game read. ``names``: PakNames."""
    access = session.accessed() if session is not None else None
    if access is None:
        return None
    view = index.for_session(session)
    if on is None:
        on = session_on(session)
        if on is None:
            on = [m.id for m in view.mods.values() if m.enabled]
    return exposure(access, view, on, content, crash_time(session), tail_only, crash_slack(session), names,
                    _refw_log(session))


def step_run(detection, on: Iterable[str], index, content: Iterable[str],
             names: Callable[[int], str | None] | None = None) -> dict:
    """What 원인 모드 찾기 keeps of one launch (gamewatch.Detection after it ended; ``on``: the step's mods;
    ``index``: the manager's crashes.ModIndex; ``names``: PakNames): {"seconds", "log", "unexposed", "tail",
    "record", "auto"}. Never raises (off the GUI thread: reads the record's copy and pak tables)."""
    run = {"seconds": None, "log": False, "unexposed": [], "tail": [], "record": "", "auto": ""}
    try:
        exit_ = getattr(detection, "exit", None)
        seconds = getattr(exit_, "seconds", None) if exit_ is not None else None
        run["seconds"] = float(seconds) if isinstance(seconds, (int, float)) else None
        crashed = getattr(detection, "crashed", None)
        run["auto"] = "crash" if crashed is True else "normal" if getattr(detection, "normal", False) else ""
        session = getattr(detection, "session", None)
        record = getattr(detection, "record", None)
        run["record"] = (session or record).id if (session or record) is not None else ""
        if crashed and record is not None and session is not None and record.id != session.id:
            # (the crash record is another run's than the launch's own: the game was started again before this
            # was looked at - which files the launch's crash came after is not known)
            return run
        access = session.accessed() if session is not None else None
        if access is None or index is None:
            return run
        when = crash_time(session) if crashed else None
        slack = crash_slack(session) if when is not None else 0.0
        if crashed and when is None:  # (a crash without REFramework's record of it: when the game ended)
            ended = getattr(exit_, "ended", None)
            when = float(ended) if isinstance(ended, (int, float)) else access.last_time
        found = exposure(access, index.for_session(session), on, content, when, slack=slack, names=names,
                         refw=_refw_log(session))
        run["log"] = found.log
        run["unexposed"] = list(found.unexposed)
        run["tail"] = list(found.tail)
    except Exception as exc:  # only a help for the search
        log.warning("이번 실행에서 게임이 읽은 파일을 확인하지 못했습니다: %s", exc)
    return run


# ---- consent and restore -------------------------------------------------------------------------------
@dataclass
class Offer:
    available: bool  # the checkbox can be ticked
    already: bool  # LOG_KEY is true already (checkbox ticked and disabled, nothing to restore)
    text: str  # the note under the checkbox
    changes: dict[str, str] = field(default_factory=dict)  # key -> "true" to set
    path: str = ""  # the config file (crashes.config_path)


def _has_files(folder: Path) -> bool:
    """A file anywhere under ``folder`` (stops at the first)."""
    try:
        for _root, _dirs, files in os.walk(fsutil.longpath(folder)):
            if files:
                return True
    except OSError:
        return True  # (not known: as if there were files)
    return False


def _config_values(path: Path | None) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace") if path is not None and path.is_file() else ""
    except OSError:
        text = ""
    return crashlog.parse_config(text)


def logging_offer(game_dir, appdata=None, running: bool = False, loose_mods: bool = False,
                  fallback: bool | None = None) -> Offer:
    """Whether the record of the files the game reads can be turned on now, and how. Loose file loading
    (LooseFileLoader_Enabled), which the record needs, is turned on with it only when natives\\ holds no file
    (else the game would read loose files it never read before); ``loose_mods``: mods with natives files
    may be applied during the search (then never); ``fallback``: REFramework's last log said it used
    %APPDATA% (crashes.config_path). Reads a few files."""
    path = crashes.config_path(game_dir, appdata, fallback) if game_dir else None
    where = str(path) if path is not None else ""
    if not game_dir or not (Path(game_dir) / REFRAMEWORK_DLL).is_file():
        return Offer(False, False, LOG_NO_REFW, {}, where)
    values = _config_values(path)
    if crashlog.config_bool(values.get(LOG_KEY)) is True and crashlog.config_bool(values.get(LOOSE_KEY)) is True:
        return Offer(True, True, LOG_ALREADY, {}, where)
    if running:
        return Offer(False, False, LOG_RUNNING, {}, where)
    if crashlog.config_bool(values.get(LOOSE_KEY)) is True:
        return Offer(True, False, LOG_NOTE, {LOG_KEY: "true"}, where)
    if not loose_mods and not _has_files(Path(game_dir) / "natives"):
        return Offer(True, False, LOG_NOTE + LOG_NOTE_LOOSE, {LOOSE_KEY: "true", LOG_KEY: "true"}, where)
    return Offer(False, False, LOG_UNAVAILABLE_LOOSE, {}, where)


def manager_offer(manager_, running: bool = False, loose: bool | None = None) -> Offer:
    """logging_offer for a manager.ModManager: ``loose_mods`` from its enabled mods (a mod with loose files
    on: LooseFileLoader is never switched on with the record; ``loose``: that, when the caller knows it - then
    any thread) and ``fallback`` from 실행 전 점검's last look at REFramework's log (which folder it kept its
    settings in). Reads a few files."""
    from . import precheck

    if loose is None:
        loose = True  # (not known: as if there were)
        try:
            loose = any("loose" in manager_.mod_kinds(m.id) for m in manager_.mods if m.enabled)
        except Exception as exc:
            log.warning("모드 종류를 확인하지 못했습니다: %s", exc)
    fallback = None
    try:
        bg = precheck.load_background(Path(manager_.data_dir) / "cache")
        if bg is not None and bg.session_log:
            fallback = bg.appdata
    except Exception as exc:  # then the folder the settings file is in
        log.debug("실행 전 점검 결과를 읽지 못했습니다: %s", exc)
    return logging_offer(manager_.game_dir, running=running, loose_mods=loose, fallback=fallback)


def enable_logging(set_option: Callable, offer: Offer) -> dict | None:
    """Make the offer's changes (``set_option``: ModManager.set_refw_option(key, value, path), which refuses
    while the game runs). Returns the restore record {"path", "values": {key: old value or None}} (None:
    nothing changed). A failure undoes what was changed and raises."""
    if not offer.available or offer.already or not offer.changes:
        return None
    path = offer.path or None
    old = _config_values(Path(path)) if path else {}
    done: dict[str, str | None] = {}
    try:
        for key, value in offer.changes.items():
            if set_option(key, value, path):
                done[key] = old.get(key)
    except Exception:
        if done:
            try:
                restore_logging(set_option, {"path": offer.path, "values": done})
            except Exception as exc:
                log.warning("REFramework 설정을 되돌리지 못했습니다: %s", exc)
        raise
    if not done:
        return None
    log.info("원인 찾기를 위해 게임이 읽는 파일 기록을 켰습니다 (%s).", ", ".join(done))
    return {"path": offer.path, "values": done}


def _valid(restore) -> bool:
    return isinstance(restore, dict) and isinstance(restore.get("values"), dict) and bool(restore["values"])


def restore_logging(set_option: Callable, restore: dict) -> None:
    """Put the settings enable_logging changed back (a key that was not in the file is written "false",
    REFramework's default). Only the logging keys are ever touched."""
    if not _valid(restore):
        return
    path = restore.get("path") or None
    if path and not Path(path).is_file():
        return  # (the settings file is gone: REFramework uses its defaults, logging off)
    values = restore["values"]
    for key in (LOG_KEY, LOOSE_KEY):  # (the record off first)
        if key in values:
            old = values[key]
            set_option(key, old if isinstance(old, str) and old else "false", path)


def _restore_path(data_dir) -> Path:
    return Path(data_dir) / RESTORE_FILE


def pending(data_dir) -> dict | None:
    """The restore left for when the game is closed (None: none)."""
    data = fsutil.read_json(_restore_path(data_dir), None)
    if not isinstance(data, dict) or data.get("format") != RESTORE_FORMAT or not _valid(data.get("restore")):
        return None
    return data["restore"]


def drop_pending(data_dir) -> None:
    """Forget a pending restore (the user turned the record on for good in the 게임 튕김 기록 window)."""
    fsutil.remove_file(_restore_path(data_dir))


def keep_enabled(data_dir, restore: dict | None, earlier: dict | None = None) -> None:
    """Remember what the 게임 튕김 기록 window turned on for good (enable_logging's restore record, with a search's
    pending one it takes over - ``earlier``: its values are older, so they win), so that turning it off there puts
    it back."""
    records = [r for r in (enabled_here(data_dir), earlier, restore) if _valid(r)]  # (the oldest first)
    if not records:
        return
    values: dict = {}
    for r in reversed(records):
        values.update(r["values"])
    try:
        fsutil.atomic_write_json(Path(data_dir) / KEPT_FILE, {
            "format": RESTORE_FORMAT, "time": time.time(),
            "restore": {"path": next((r.get("path") for r in reversed(records) if r.get("path")), ""),
                        "values": values}})
    except OSError as exc:
        log.warning("REFramework 설정을 되돌릴 기록을 저장하지 못했습니다: %s", exc)


def enabled_here(data_dir) -> dict | None:
    """What the 게임 튕김 기록 window turned on (keep_enabled; None: nothing, or it was on before)."""
    data = fsutil.read_json(Path(data_dir) / KEPT_FILE, None)
    if not isinstance(data, dict) or data.get("format") != RESTORE_FORMAT or not _valid(data.get("restore")):
        return None
    return data["restore"]


def disable_logging(set_option: Callable, data_dir, path: str | None, loose_mods: bool, game_dir=None) -> bool:
    """Turn the record off from the 게임 튕김 기록 window: what that window turned on goes back as it was
    (enabled_here), else only the record itself is turned off ("false", REFramework's default). Loose file loading
    it turned on stays on when the game reads loose files now (``loose_mods``: mods with natives files are on; or
    files in the game's natives\\). Returns whether it stayed on for that. Reads the folder a little."""
    kept = enabled_here(data_dir)
    values = dict(kept["values"]) if kept is not None else {}
    stays = LOOSE_KEY in values and (loose_mods or bool(game_dir) and _has_files(Path(game_dir) / "natives"))
    if stays:
        values.pop(LOOSE_KEY)
    values.setdefault(LOG_KEY, None)
    restore_logging(set_option, {"path": path, "values": values})
    fsutil.remove_file(Path(data_dir) / KEPT_FILE)
    return stays


def _keep_pending(data_dir, restore: dict) -> None:
    try:
        fsutil.atomic_write_json(_restore_path(data_dir), {"format": RESTORE_FORMAT, "time": time.time(),
                                                          "restore": restore})
    except OSError as exc:
        log.warning("REFramework 설정을 되돌릴 기록을 저장하지 못했습니다: %s", exc)


def _game_running() -> bool:
    from . import manager  # (the module function, so tests can stand in for it)

    return manager.is_game_running()


def finish_restore(manager_, restore: dict | None) -> str:
    """After bisect.finish: put the logging settings back now, or (the game runs) keep them in
    <data>/RESTORE_FILE for apply_pending. Returns the status line ("" when there was nothing to do)."""
    if not _valid(restore):
        return ""
    if _game_running():
        _keep_pending(manager_.data_dir, restore)
        log.info("게임이 실행 중이라 REFramework 파일 기록 설정은 게임을 끈 뒤 되돌립니다.")
        return RESTORE_PENDING
    try:
        restore_logging(manager_.set_refw_option, restore)
    except Exception as exc:
        log.warning("REFramework 파일 기록 설정을 되돌리지 못했습니다: %s", exc)
        _keep_pending(manager_.data_dir, restore)
        return RESTORE_FAILED.format(error=exc)
    if pending(manager_.data_dir) == restore:
        drop_pending(manager_.data_dir)
    return RESTORED


def apply_pending(manager_, searching: bool = False) -> str:
    """A restore finish_restore left for later, now that the game is closed (at startup and after a game
    exit). ``searching``: a search runs (it found the setting on and keeps it: the restore waits until it
    ends). Returns the status line ("" when there was nothing to do or the game still runs)."""
    restore = pending(manager_.data_dir)
    if restore is None or searching or _game_running():
        return ""
    try:
        restore_logging(manager_.set_refw_option, restore)
    except Exception as exc:
        log.warning("REFramework 파일 기록 설정을 되돌리지 못했습니다: %s", exc)
        return ""
    drop_pending(manager_.data_dir)
    return RESTORED
