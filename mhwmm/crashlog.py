"""Reading what REFramework leaves behind after a game session, and the game's own error text.

REFramework (praydog, MIT, https://github.com/praydog/REFramework) writes next to the game exe,
or into %APPDATA%\\REFramework\\MonsterHunterWilds\\ when the game folder is not writable:

- ``re2_framework_log.txt``: spdlog lines ``[YYYY-MM-DD hh:mm:ss.mmm] [REFramework] [level] msg``
  (CRLF, local time, some with a ``[File.cpp:N]`` block); a line without that head continues the
  previous record (call stacks).  On an unhandled exception it logs ``Exception occurred: c0000005``,
  the registers, ``Call stack:`` with one record of frames, ``Module: <base> <path>`` and writes
  ``reframework_crash.dmp``.  Truncated on every start; builds since 2026-10-11 first keep the
  previous session as ``re2_framework_log.prev.txt``.
- ``reframework_faulty_files.txt`` (builds since 2026-03-04): one ``"error" 2 "Invalid file" "<path>"``
  line per resource the game could not load.
- ``re2_fw_config.txt``: ``key=value`` lines, read once at start and rewritten by REFramework while
  the game runs (so it is only changed while the game is closed).

Builds differ (no .prev.txt, no faulty list, no RIP line in older ones), so everything here is
tolerant: a line that is not understood is skipped, never an error.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LOG_FILE = "re2_framework_log.txt"
PREV_LOG_FILE = "re2_framework_log.prev.txt"
DUMP_FILE = "reframework_crash.dmp"
FAULTY_FILE = "reframework_faulty_files.txt"
CONFIG_FILE = "re2_fw_config.txt"
APPDATA_SUBDIR = ("REFramework", "MonsterHunterWilds")  # under %APPDATA%: the exe name without .exe

LUA_LOG_KEY = "ScriptRunner_LogToDisk"  # Lua errors reach the log only when this is true
LOOSE_FILES_KEY = "LooseFileLoader_Enabled"
PAK_DIRECTORY_KEY = "IntegrityCheckBypass_LoadPakDirectory"

TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

_HEAD = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d{1,6})?)\] \[([^\]]*)\] "
                   r"\[(trace|debug|info|warn|warning|err|error|critical)\] ?"
                   r"(?:\[([\w.]+\.(?:cpp|hpp|cc|c|h):\d+)\] )?(.*)$")
_HEADER = {"Commit hash": "commit", "Tag": "tag", "Commits past tag": "commits_past", "Branch": "branch",
           "Total commits": "total_commits", "Build date": "build_date", "Build time": "build_time",
           "Game name": "game_name", "Current game path": "game_path"}
HEADER_RECORDS = 400  # the session header is at the very start of the log

_EXCEPTION = re.compile(r"^Exception occurred: ([0-9a-fA-F]{1,8})$")  # only the fatal handler logs exactly this
_RIP = re.compile(r"^RIP(?: \(exception record\))?: ([0-9a-fA-F]+)$")
_REGISTER = re.compile(r"^(?:RIP|RSP|RCX|RDX|R\d{1,2}|RAX|RBX|RBP|RSI|RDI|EFLAGS|CS|DS|ES|FS|GS|SS)"
                       r"(?: \(exception record\))?: [0-9a-fA-F]+$")
_MODULE = re.compile(r"^Module: (?:([0-9a-fA-F]+) (.+)|Unknown)$")
_DUMP = re.compile(r"^Attempting to write dump to (.+)$")
_DUMP_FAILED = re.compile(r"^Exception occurred, but could not (create dump file|load dbghelp\.dll)")
_LOCATION = re.compile(r"^(.+?) \+ 0x([0-9a-fA-F]+)$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]+$")
_TDB = re.compile(r"\[TDB: ([^\]]+)\]")
_TDB_NAME = re.compile(r"^(?:app|via|ace|System|soundlib|share|snow)\.[\w.`<>\[\],]+$")
DISPATCHER = "KiUserExceptionDispatcher"

_RUNNING_SCRIPT = re.compile(r"^\[ScriptState\] Running script (.+?)\.\.\.$")
_LUA_INIT = "[ScriptRunner] Initializing Lua state for the first time..."
_LUA_DONE = "[ScriptRunner] Lua state initialized."
# "<path>.lua:<line>: <message>": searched for from the literal, so a long line costs linear time
_LUA_AT = re.compile(r"\.lua:(\d+):\s?", re.IGNORECASE)
_SCRIPT_UNKNOWN = re.compile(r"^Unknown error when running script (.+)$")
SCRIPT_STUCK_SECONDS = 30  # a script still loading this close to the end of the log is suspected

_PLUGIN_LOADED = re.compile(r"^\[PluginLoader\] Loaded (.+)$")
_PLUGIN_ERRORS = (
    (re.compile(r"^\[PluginLoader\] Failed to load (.+)$"), "load"),
    (re.compile(r"^\[PluginLoader\] Plugin (.+) requires a different major version$"), "major"),
    (re.compile(r"^\[PluginLoader\] Plugin (.+) requires a newer minor version$"), "minor"),
    (re.compile(r"^\[PluginLoader\] Plugin (.+) is for a different game (.*)$"), "game"),
    (re.compile(r"^\[PluginLoader\] Failed to initialize (?!plugins:)(.+)$"), "init"),
    (re.compile(r"^\[PluginLoader\] (.+) has an exception in reframework_plugin_\w+, skipping\.\.\.$"), "exception"),
)

_PAK_CACHED = re.compile(r"Cached custom pak with name: .+? at path: (.+)$")
_PAK_COUNT = re.compile(r"Finished caching custom pak paths\. Total count: (\d+)")
_PAK_INJECT = re.compile(r"injecting (.+) as native-form '([^']+)'")
_PAK_REDIRECT = re.compile(r"Redirecting load of (.+?) to custom pak at path: (.+)$")  # before 2026-09-10
_PAK_FAILED = re.compile(r"pak_load_check_function_hook returned false for string: (.+)$")
_PAK_SLOT = re.compile(r"slot (\d+) for '(.+)' is already occupied by a real pak")
_PAK_NONE = re.compile(r"(\d+) custom pak\(s\) were cached but NONE were injected")
_PAK_RANGE = re.compile(r"Patch number (\d+) is out of range for PAK directory's PAK")
_PAK_DISABLED = "Pak directory loading is disabled"

_GAME_DLL = re.compile(r"DLL loaded from game directory: (.+)$")
_PRESENT = re.compile(r"^Present failed: (-?[0-9a-fA-F]+)$")
PRESENT_OCCLUDED = "87a0001"  # DXGI_STATUS_OCCLUDED: the window was hidden, not an error
_INIT_ERRORS = ("Initialization of mods failed", "::on_initialize() has failed",
                "Timed out waiting for VM to initialize", "TypeDB not found", "Unable to find TDB inside VM",
                "We're going to crash, could not locate functions")
# REFramework's loose file loading did not start: loose mods (natives\) are not read
_LOOSE_FAILED = ("[LooseFileLoader] Failed to find path_to_hash candidate, cannot continue",
                 "[LooseFileLoader] Failed to hook path_to_hash")
_HOOK_ERRORS = ("[HookManager] Cannot add nullptr function", "[HookManager] Cannot add method that resolves to nullptr",
                "[HookManager] Failed to hook function for")

# Lines every normal session has (REFramework issue #1869 ran 9 minutes with all of them): never findings.
NOISE = (
    "Present failed: 87a0001", "Failed to find function start", "conditional_jmp for DD2",
    "Could not find stack destroyer", "Failed to find get_encoded_pointer", "Renderer::hasInstance not found",
    "Failed to get type info: unknown exception", "Failed to find get_Instance method for",
    "Failed to find three consecutive vtable calls", "[Streamline] Failed to get sl.dlss_g.dll module handle",
    "VEH not allowed, returning nullptr", "Could not read flags from re_chunk_",
)

EXCEPTION_NAMES = {
    "c0000005": "메모리 접근 오류", "c00000fd": "스택 넘침", "c0000409": "즉시 종료",
    "e06d7363": "C++ 예외", "80000003": "중단점", "c000001d": "잘못된 명령", "c0000096": "특권 명령 오류",
    "c0000374": "힙 손상", "c0000094": "0으로 나누기", "c0000006": "디스크 읽기 오류", "c0000017": "메모리 부족",
}


def exception_label(code: str) -> str:
    """``c0000005`` -> ``0xC0000005 (메모리 접근 오류)``."""
    code = normalize_code(code)
    if not code:
        return ""
    name = EXCEPTION_NAMES.get(code)
    return f"0x{code.upper()}" + (f" ({name})" if name else "")


def normalize_code(code) -> str:
    """An exception code as 8 lower-case hex digits ("" when it is not one)."""
    if isinstance(code, int):
        return f"{code & 0xFFFFFFFF:08x}"
    text = str(code or "").strip().lower().removeprefix("0x")
    return f"{int(text, 16):08x}" if re.fullmatch(r"[0-9a-f]{1,8}", text) else ""


def parse_time(stamp: str) -> datetime | None:
    """A log time stamp (local time) as a naive datetime."""
    try:
        return datetime.strptime(stamp, TIME_FORMAT if "." in stamp else TIME_FORMAT[:-3])
    except (TypeError, ValueError):
        return None


def is_noise(message: str) -> bool:
    return any(n in message for n in NOISE)


# ---- the log -----------------------------------------------------------------------------
@dataclass
class Frame:
    module: str  # module path as logged ("" for an address in no module)
    offset: int | None
    symbol: str = ""  # nearest export: meaningless for the game exe and dinput8.dll
    tdb: str = ""  # game method near the address (only a hint: the nearest one within 0x10000)

    def describe(self) -> str:
        name = self.module.replace("/", "\\").rsplit("\\", 1)[-1] if self.module else "?"
        text = f"{name} + 0x{self.offset:x}" if self.offset is not None else name
        return text + (f" [{self.tdb}]" if self.tdb else "")


@dataclass
class Crash:
    """The block the fatal exception handler wrote."""
    time: str
    code: str  # 8 hex digits, "c0000005"
    suspicious: bool = False  # "SUSPICIOUS EXCEPTION" before it (possibly raised on purpose by the game's protection)
    rip: str = ""
    module: str = ""  # path of the module holding the crashing address ("" for "Unknown" or missing)
    module_base: str = ""
    module_logged: bool = False  # the handler wrote its Module: line ("Unknown": the address is in no module)
    frames: list[Frame] = field(default_factory=list)  # after KiUserExceptionDispatcher: the crashed thread's own
    dump: str = ""  # where it wrote the dump
    dump_failed: str = ""
    lines: list[str] = field(default_factory=list)  # the evidence lines, as logged

    @property
    def place(self) -> str:
        """The crashing module: the Module: line ("" for Unknown: code in no module, such as a hook), else
        (the handler died before writing it) the first frame's."""
        if self.module or self.module_logged:
            return self.module
        return self.frames[0].module if self.frames else ""


@dataclass
class PluginError:
    name: str  # plugin file stem, or the path for "load"
    reason: str  # "load" | "major" | "minor" | "game" | "init" | "exception"
    line: str


@dataclass
class LuaError:
    time: str
    path: str  # as logged: a full path, or "..." and the last 56 characters
    line: int
    message: str
    raw: str


@dataclass
class LogInfo:
    started: str = ""
    ended: str = ""
    records: int = 0
    header: dict = field(default_factory=dict)  # commit, tag, commits_past, build_date, game_name, game_path, tdb...
    previous_kept: bool = False  # the build keeps the previous session as .prev.txt
    appdata: bool = False  # the game folder was not writable: files went to %APPDATA%
    crashes: list[Crash] = field(default_factory=list)
    vm_exceptions: int = 0  # access violations inside game calls from Lua that REFramework caught (not fatal)
    scripts: list[str] = field(default_factory=list)  # Lua scripts in the order they ran
    stuck_script: str = ""  # the script that was loading when the log ended or the game crashed
    stuck_line: str = ""
    stuck_sure: bool = False  # the first load of scripts never finished
    lua_errors: list[LuaError] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    plugin_errors: list[PluginError] = field(default_factory=list)
    paks_cached: list[str] = field(default_factory=list)  # pak_mods files REFramework found
    pak_count: int | None = None
    paks_injected: dict[str, str] = field(default_factory=dict)  # native-form name -> real path
    paks_failed: list[str] = field(default_factory=list)  # pak_mods files the game refused
    paks_slot_taken: list[str] = field(default_factory=list)
    paks_none: int = 0  # "N custom pak(s) were cached but NONE were injected"
    paks_disabled: bool = False
    paks_out_of_range: list[str] = field(default_factory=list)
    game_dlls: list[str] = field(default_factory=list)  # DLLs loaded from the game folder
    present_errors: list[str] = field(default_factory=list)  # Present failures other than "window hidden"
    init_errors: list[str] = field(default_factory=list)  # REFramework could not find what it needs (too old?)
    loose_failed: str = ""  # the line saying loose file loading could not start
    hook_errors: int = 0  # scripts hooking game methods that do not exist (old scripts after an update)
    errors: int = 0  # other error lines, noise left out
    error_lines: list[str] = field(default_factory=list)  # the first few of them
    last_lines: list[str] = field(default_factory=list)  # the last records, noise left out

    @property
    def crash(self) -> Crash | None:
        """The fatal block (the first one: a second one is the handler failing on the way)."""
        return self.crashes[0] if self.crashes else None

    @property
    def game_path(self) -> str:
        return self.header.get("game_path", "")


def _records(lines):
    """(time, level, source, message, continuation lines) for every log record."""
    current = None
    for raw in lines:
        line = raw.rstrip("\r\n")
        m = _HEAD.match(line)
        if m:
            if current is not None:
                yield current
            current = (m.group(1), m.group(3), m.group(4) or "", m.group(5), [])
        elif current is not None:
            current[4].append(line)
    if current is not None:
        yield current


def parse_frames(lines: list[str]) -> list[Frame]:
    """The frames of a call stack record: `` <symbol>`` then ``  <module> + 0x<offset>``."""
    frames, symbol = [], ""
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("  "):
            loc = line.strip()
            m = _LOCATION.match(loc)
            if m:
                module = "" if _ADDRESS.match(m.group(1)) else m.group(1)
                offset = int(m.group(2), 16)
            else:
                module, offset = ("" if _ADDRESS.match(loc) else loc), None
            tdb = _TDB.search(symbol)
            frames.append(Frame(module, offset, _TDB.sub("", symbol).strip(),
                                tdb.group(1) if tdb else (symbol if _TDB_NAME.match(symbol) else "")))
            symbol = ""
        else:
            symbol = line.strip()
    return frames


def crashed_frames(frames: list[Frame]) -> list[Frame]:
    """The crashed thread's frames: the stack is taken inside the handler, so everything up to the
    exception dispatcher is REFramework's handler and Windows (dinput8.dll at the top means nothing)."""
    for i in range(len(frames) - 1, -1, -1):
        if DISPATCHER in frames[i].symbol:
            return frames[i + 1:]
    return frames


def parse_log(text_or_lines) -> LogInfo:
    """Everything useful in a REFramework log (``str`` or an iterable of lines)."""
    lines = text_or_lines.split("\n") if isinstance(text_or_lines, str) else text_or_lines
    info = LogInfo()
    crash: Crash | None = None
    stage = ""  # inside the fatal block: "regs", "stack", "module", "dump" (the dump line was the last), "done"
    suspicious = False
    first_init = done_at = None  # record numbers of the Lua state lines
    script_at = script_time = None
    for time, level, _src, msg, extra in _records(lines):
        n = info.records
        info.records += 1
        info.started = info.started or time
        info.ended = time
        if n < HEADER_RECORDS:
            key, sep, value = msg.partition(": ")
            if sep and key in _HEADER and _HEADER[key] not in info.header:
                info.header[_HEADER[key]] = value.strip()
            elif msg.startswith("[VM::update_pointers] TDB Version: ") and "tdb" not in info.header:
                info.header["tdb"] = msg.rsplit(" ", 1)[-1]
            if msg.startswith("Previous log preserved as "):
                info.previous_kept = True
            if "falling back to appdata folder" in msg:
                info.appdata = True
        # the fatal exception block
        if msg.startswith("SUSPICIOUS EXCEPTION"):
            suspicious = True
            continue
        m = _EXCEPTION.match(msg)
        if m:
            crash = Crash(time, normalize_code(m.group(1)), suspicious, lines=[msg])
            info.crashes.append(crash)
            stage, suspicious = "regs", False
            continue
        if crash is not None and stage == "dump":
            stage = "done"  # REFramework logs the failure after the "Attempting to write dump" line
            if m := _DUMP_FAILED.match(msg):
                crash.dump_failed = m.group(1)
                crash.lines.append(msg)
                continue
        if crash is not None and stage != "done":
            if stage == "regs" and (m := _RIP.match(msg)):
                crash.rip = crash.rip or m.group(1).lower()
                continue
            if msg == "Call stack:":
                stage = "stack"
                continue
            if stage == "stack" and not msg.strip() and extra:
                crash.frames = crashed_frames(parse_frames(extra))
                stage = "module"
                continue
            if m := _MODULE.match(msg):
                crash.module_base, crash.module = (m.group(1) or "").lower(), (m.group(2) or "").strip()
                crash.module_logged = True
                crash.lines.append(msg)
                stage = "module"
                continue
            if m := _DUMP.match(msg):
                crash.dump = m.group(1).strip()
                stage = "dump"
                continue
            if m := _DUMP_FAILED.match(msg):
                crash.dump_failed = m.group(1)
                crash.lines.append(msg)
                stage = "done"
                continue
        if msg.startswith("VMContext: Caught exception code"):
            info.vm_exceptions += 1
            continue
        if _REGISTER.match(msg) or msg == "Call stack:" or (not msg.strip() and extra):
            continue  # register dumps and call stacks (also the ones of exceptions REFramework caught)
        # Lua scripts
        if m := _RUNNING_SCRIPT.match(msg):
            info.scripts.append(m.group(1).strip())
            script_at, script_time, info.stuck_line = n, time, msg
            continue
        if msg == _LUA_INIT:
            first_init = n if first_init is None else first_init
            continue
        if msg == _LUA_DONE:
            done_at = n
            continue
        if level in ("err", "error", "critical") and not msg.startswith("[") and (m := _LUA_AT.search(msg)) and m.start():
            path = msg[:m.start() + 4].rsplit(": ", 1)[-1].strip()
            info.lua_errors.append(LuaError(time, path, int(m.group(1)), msg[m.end():].strip(), msg))
            continue
        if m := _SCRIPT_UNKNOWN.match(msg):
            info.lua_errors.append(LuaError(time, m.group(1).strip(), 0, "알 수 없는 오류", msg))
            continue
        # plugins
        if m := _PLUGIN_LOADED.match(msg):
            info.plugins.append(m.group(1).strip())
            continue
        found = next(((rx.match(msg), reason) for rx, reason in _PLUGIN_ERRORS if rx.match(msg)), None)
        if found:
            info.plugin_errors.append(PluginError(found[0].group(1).strip(), found[1], msg))
            continue
        # pak_mods
        if "IntegrityCheckBypass" in msg or "custom pak" in msg or "Patch number" in msg:
            if m := _PAK_CACHED.search(msg):
                info.paks_cached.append(m.group(1).strip())
            elif m := _PAK_COUNT.search(msg):
                info.pak_count = int(m.group(1))
            elif m := _PAK_INJECT.search(msg):
                info.paks_injected[m.group(2)] = m.group(1).strip()
            elif m := _PAK_REDIRECT.search(msg):
                info.paks_injected[m.group(1).strip()] = m.group(2).strip()
            elif m := _PAK_FAILED.search(msg):
                if is_pak_mods_path(m.group(1)):  # the game's own patch numbers fail like this in every session
                    info.paks_failed.append(m.group(1).strip())
            elif m := _PAK_SLOT.search(msg):
                info.paks_slot_taken.append(m.group(2))
            elif m := _PAK_NONE.search(msg):
                info.paks_none = int(m.group(1))
            elif m := _PAK_RANGE.search(msg):
                info.paks_out_of_range.append(msg)
            elif _PAK_DISABLED in msg:
                info.paks_disabled = True
        if m := _GAME_DLL.search(msg):
            info.game_dlls.append(m.group(1).strip())
            continue
        if m := _PRESENT.match(msg):
            if m.group(1).lower() != PRESENT_OCCLUDED and m.group(1).lower() not in info.present_errors:
                info.present_errors.append(m.group(1).lower())
            continue
        if any(e in msg for e in _INIT_ERRORS):
            info.init_errors.append(msg)
            continue
        if msg.startswith(_LOOSE_FAILED):
            info.loose_failed = info.loose_failed or msg
            continue
        if any(msg.startswith(e) for e in _HOOK_ERRORS):
            info.hook_errors += 1
            continue
        if level in ("err", "error", "critical") and msg.strip() and not is_noise(msg):
            info.errors += 1
            if len(info.error_lines) < 20:
                info.error_lines.append(msg)
        if msg.strip() and not is_noise(msg):
            info.last_lines = (info.last_lines + [f"[{time}] {msg}"])[-5:]
    _find_stuck_script(info, script_at, script_time, first_init, done_at)
    return info


def _find_stuck_script(info: LogInfo, script_at, script_time, first_init, done_at) -> None:
    if script_at is None or (done_at is not None and done_at > script_at):
        info.stuck_script = info.stuck_line = ""
        return
    # the first load of all scripts never finished (REFramework logs both lines since 2022)
    sure = first_init is not None and first_init < script_at and (done_at is None or done_at < first_init)
    end = parse_time(info.crash.time if info.crash else info.ended)
    start = parse_time(script_time)
    near = end is not None and start is not None and (end - start).total_seconds() <= SCRIPT_STUCK_SECONDS
    if sure or near:
        info.stuck_script, info.stuck_sure = info.scripts[-1], sure
    else:
        info.stuck_script = info.stuck_line = ""


def is_pak_mods_path(path: str) -> bool:
    return "/pak_mods/" in path.replace("\\", "/").casefold()


def read_log(path, max_bytes: int = 64 * 1024 * 1024) -> LogInfo:
    """parse_log of a file (an over-large one only in part: its start and its end)."""
    return parse_log(read_text(path, max_bytes))


def read_text(path, max_bytes: int = 64 * 1024 * 1024) -> str:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size <= max_bytes:
            data = f.read()
        else:
            head = f.read(max_bytes // 8)
            f.seek(size - (max_bytes - len(head)))
            data = head + b"\n" + f.read()
    return data.decode("utf-8", errors="replace")


def first_time(path) -> str:
    """Time stamp of the first record of a log ("" if it has none yet)."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096).decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in head.split("\n"):
        m = _HEAD.match(line.rstrip("\r"))
        if m:
            return m.group(1)
    return ""


def refw_label(header: dict, builds=None) -> str:
    """The REFramework build a log was written by: ``nightly 01432`` when ``builds`` (refw.Build)
    know its commit, else ``v1.5.9.1+514 (2026-10-11 빌드)``."""
    commit = (header.get("commit") or "").lower()
    for build in builds or ():
        known = (getattr(build, "commit", "") or "").lower()
        if commit and len(known) >= 7 and (commit.startswith(known) or known.startswith(commit)):
            return build.label
    tag = header.get("tag", "")
    past = header.get("commits_past", "")
    text = tag + (f"+{past}" if tag and past and past != "0" else "")
    date = header.get("build_date", "")
    m = re.fullmatch(r"(\d\d)\.(\d\d)\.(\d{4})", date)
    if m:
        date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    if date:
        text = f"{text} ({date} 빌드)" if text else f"{date} 빌드"
    if not text and commit:
        text = commit[:7]
    return text


# ---- the faulty files list ------------------------------------------------------------------
_FAULTY = re.compile(r'^"(\w+)" (-?\d+) "([^"]*)" "(.*)"$')
_TOAST = re.compile(r"^\[(Missing file|Invalid file|Unknown reason|PAK should be encrypted)\] (.+)$")
_REASONS = {"Unknown reason": 0, "Missing file": 1, "Invalid file": 2, "PAK should be encrypted": 3}
_RESOURCE_PREFIX = re.compile(r"^(?:natives/(?:stm/|x64/)?)?(?:streaming/)?", re.IGNORECASE)
_EXT_SUFFIX = re.compile(r"\.([A-Za-z][A-Za-z0-9]*)((?:\.\d+)+(?:\.(?:x64|stm))?(?:\.[a-z]{2}(?:\d{3}|[a-z]{2})?)?)?$")


@dataclass(frozen=True)
class FaultyFile:
    level: str  # "error", "warning", "info" ("" when not given)
    reason: int  # 0 unknown, 1 missing, 2 invalid, 3 should be encrypted, -1 not given
    label: str
    path: str  # as logged: "GameDesign/Equip/.../003_001_avp.user" (no natives/STM/, no version)

    @property
    def key(self) -> str:
        return resource_key(self.path)

    @property
    def name(self) -> str:
        return self.path.replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def missing(self) -> bool:
        return self.reason == 1


def resource_key(path: str) -> str:
    """``natives/STM/GameDesign/X.user.3`` and ``GameDesign/x.user`` -> ``gamedesign/x.user``."""
    p = path.replace("\\", "/").strip().strip('"').lstrip("@/")
    p = _RESOURCE_PREFIX.sub("", p, count=1)
    m = _EXT_SUFFIX.search(p)
    if m and m.group(2):
        p = p[:m.end(1)]
    return p.casefold()


def split_version(path: str) -> tuple[str, str]:
    """``x.user.3`` -> (``x.user``, ``.3``); ``x.user`` -> (``x.user``, "")."""
    m = _EXT_SUFFIX.search(path)
    if m and m.group(2):
        return path[:m.end(1)], m.group(2)
    return path, ""


def parse_faulty(text: str) -> list[FaultyFile]:
    """The entries of reframework_faulty_files.txt (or pasted ``[Invalid file] <path>`` toasts), once each."""
    out, seen = [], set()
    for raw in text.split("\n"):
        line = raw.strip().lstrip("\ufeff")
        if not line:
            continue
        m = _FAULTY.match(line)
        if m:
            item = FaultyFile(m.group(1), int(m.group(2)), m.group(3), m.group(4).strip())
        elif m := _TOAST.search(line):
            item = FaultyFile("", _REASONS[m.group(1)], m.group(1), m.group(2).strip())
        else:
            quoted = re.findall(r'"([^"]+)"', line)
            path = quoted[-1] if quoted else line
            if "/" not in path.replace("\\", "/") or " " in path.strip() and not quoted:
                continue
            item = FaultyFile("", -1, "", path.strip())
        if item.path and item.key not in seen:
            seen.add(item.key)
            out.append(item)
    return out


# ---- the config file -------------------------------------------------------------------------
def parse_config(text: str) -> dict[str, str]:
    """``key=value`` lines split at the first ``=``, as REFramework reads them (no trimming)."""
    out = {}
    for line in text.split("\n"):
        key, sep, value = line.rstrip("\r").partition("=")
        if sep and key and value:
            out[key] = value
    return out


def config_bool(value) -> bool | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return True if text == "true" else False if text == "false" else None


def set_config_value(path, key: str, value) -> bool:
    """Set ``key=value`` in a REFramework config file, line by line: every other line, the line
    endings (CRLF) and the encoding stay as they are; a missing key is added at the end and a
    missing file is made.  Returns False when it already had that value.  Only call this while the
    game is closed: REFramework rewrites the whole file from memory while it runs."""
    if "=" in key or "\n" in key or not key:
        raise ValueError(f"잘못된 설정 이름입니다: {key!r}")
    value = ("true" if value else "false") if isinstance(value, bool) else str(value)
    if "\n" in value or "\r" in value:
        raise ValueError("설정 값에 줄바꿈을 넣을 수 없습니다.")
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b""
    text = data.decode("utf-8", errors="surrogateescape")
    newline = "\r\n" if "\r\n" in text or "\n" not in text else "\n"
    lines = text.splitlines(keepends=True) if text else []
    changed, found = False, False
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        k, sep, old = body.partition("=")
        if sep and k == key:
            found = True
            if old != value:
                lines[i] = f"{key}={value}" + line[len(body):]
                changed = True
    if not found:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        lines.append(f"{key}={value}{newline}")
        changed = True
    if not changed:
        return False
    tmp = path.with_name(path.name + ".mhwmm-part")
    try:
        with open(tmp, "wb") as f:  # on disk before it replaces the user's settings (a PC that crashes...)
            f.write("".join(lines).encode("utf-8", errors="surrogateescape"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return True


# ---- where a module is -------------------------------------------------------------------------
GAME_EXE_NAME = "monsterhunterwilds.exe"
REFW_DLL_NAME = "dinput8.dll"
# DLLs the game ships in its folder (one install, October 2026; REFramework issue #1869's log).
VANILLA_GPU_DLLS = {
    "gfsdk_aftermath_lib.x64.dll", "amd_ags_x64.dll", "amd_fidelityfx_framegeneration_dx12.dll",
    "amd_fidelityfx_loader_dx12.dll", "amd_fidelityfx_upscaler_dx12.dll", "libxess.dll", "nvngx_dlss.dll",
    "nvngx_dlssd.dll", "nvngx_dlssg.dll", "sl.common.dll", "sl.dlss.dll", "sl.dlss_d.dll", "sl.dlss_g.dll",
    "sl.interposer.dll", "sl.pcl.dll", "sl.reflex.dll",
}
VANILLA_GAME_DLLS = {
    "akconvolutionreverb.dll", "aksoundseedair.dll", "crashhandler.dll", "crashreportdll.dll", "masteringsuite.dll",
    "partywin.dll", "dstorage.dll", "dstoragecore.dll", "steam_api64.dll",
}
# graphics runtimes and drivers (outside the game folder, or in its D3D12\ Agility SDK folder), and the
# upscaler and frame generation runtimes the game ships (an update may add new ones to its folder)
_GPU_PREFIXES = ("d3d12", "dxgi", "d3d11", "dxcore", "nvwgf2", "nvgpucomp", "nvapi64", "nvngx", "_nvngx", "nvlddmkm",
                 "amdxc", "amdxx", "atidxx", "atiumd", "amd_fidelityfx", "amd_ags", "ffx_", "igd12", "igdumd", "igc64",
                 "intel_gfx", "libxess", "libxell", "sl.", "gfsdk_aftermath", "vulkan-1", "nvoglv")
# names Windows loads from the game folder before System32: in its folder they are proxies someone put there
# (ReShade, OptiScaler, fakenvapi, ...), never the game's own files
PROXY_DLLS = {
    "dxgi.dll", "d3d9.dll", "d3d10.dll", "d3d11.dll", "d3d12.dll", "dinput8.dll", "winmm.dll", "version.dll",
    "dbghelp.dll", "winhttp.dll", "wininet.dll", "dsound.dll", "xinput1_3.dll", "xinput1_4.dll", "xinput9_1_0.dll",
    "nvapi64.dll", "nvngx.dll",
}
# Windows DLLs an error window may name without their folder
WINDOWS_DLLS = {
    "ntdll.dll", "kernelbase.dll", "kernel32.dll", "ucrtbase.dll", "msvcrt.dll", "msvcp140.dll", "msvcp140_1.dll",
    "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll", "user32.dll", "win32u.dll", "gdi32.dll",
    "gdi32full.dll", "combase.dll", "ole32.dll", "oleaut32.dll", "rpcrt4.dll", "advapi32.dll", "sechost.dll",
    "ws2_32.dll", "shell32.dll", "cfgmgr32.dll", "xinput1_4.dll",
}
KNOWN_PROGRAMS = {
    "gameoverlayrenderer64.dll": "Steam 오버레이", "discordhook64.dll": "Discord 오버레이",
    "rtsshooks64.dll": "RivaTuner(RTSS)", "graphics-hook64.dll": "OBS", "aswhook.dll": "Avast·AVG·Norton 백신",
    "nvspcap64.dll": "NVIDIA 오버레이", "reshade64.dll": "ReShade", "reshade.dll": "ReShade",
    "owclient.dll": "Overwolf", "medal-hook64.dll": "Medal", "atcuf64.dll": "Bitdefender 백신",
    "bdhkm64.dll": "Bitdefender 백신", "mdnsnsp.dll": "Bonjour",
}


def norm_path(path: str) -> str:
    """Case-folded with forward slashes and no ``\\\\?\\`` prefix, for comparing Windows paths."""
    p = str(path).strip().strip('"').replace("\\", "/")
    if p.startswith("//?/"):
        p = p[4:]
    return p.casefold()


def game_relative(path: str, bases=()) -> str | None:
    """A logged path as a game-relative path (case-folded, ``/``), or None outside the game folder.
    ``bases`` are game folders it may start with; without a match the folders REFramework and the
    manager use (reframework\\autorun, reframework\\plugins, pak_mods, natives\\STM) are looked for.
    REFramework names the game folder's DLLs by their copies in ``_storage_``."""
    p = norm_path(path)
    rel = None
    for base in bases:
        b = norm_path(base).rstrip("/")
        if b and p.startswith(b + "/"):
            rel = p[len(b) + 1:]
            break
    if rel is None:
        for anchor in ("/reframework/autorun/", "/reframework/plugins/"):
            i = p.rfind(anchor)
            if i >= 0:
                rel = p[i + 1:]
                break
    if rel is None:
        for anchor in ("/_storage_/", "/pak_mods/", "/natives/stm/"):
            i = p.find(anchor)
            if i >= 0:
                rel = p[i + 1:]
                break
    if rel is not None and rel.startswith("_storage_/"):
        rel = rel[len("_storage_/"):]
    return rel or None


def _is_gpu(name: str) -> bool:
    return name.startswith(_GPU_PREFIXES)


def module_place(path: str, bases=()) -> tuple[str, str]:
    """(category, game-relative path or "") of a module in a crash record. Categories:
    ``plugin`` (reframework\\plugins\\), ``reframework`` (dinput8.dll), ``game`` (the exe and the game's
    own DLLs), ``gpu`` (graphics runtime, driver, upscalers), ``root_dll`` (another DLL in the game
    folder: a proxy such as ReShade or OptiScaler, or a mod's), ``system`` (Windows), ``other`` (another
    program: overlay, antivirus...), ``unknown`` (no module: code REFramework or a script made) and
    ``named`` (a file name alone, from an error window, that says nothing of where it is)."""
    if not path or path.strip().lower() == "unknown":
        return "unknown", ""
    p = norm_path(path)
    name = p.rsplit("/", 1)[-1]
    rel = game_relative(path, bases)
    if rel is not None:
        return game_place(rel)
    if "/" not in p:
        return name_place(name)
    if name == GAME_EXE_NAME:
        return "game", GAME_EXE_NAME
    windows = re.match(r"^[a-z]:/windows/", p) or "/windows/system32/" in p or "/driverstore/" in p
    if _is_gpu(name):
        return "gpu", ""
    if windows:
        return "system", ""
    return "other", ""


def game_place(rel: str) -> tuple[str, str]:
    """module_place of a game-relative path (case-folded, ``/``)."""
    name = rel.rsplit("/", 1)[-1]
    if rel.startswith("reframework/plugins/"):
        return "plugin", rel
    if rel == REFW_DLL_NAME:
        return "reframework", rel
    if rel == GAME_EXE_NAME or rel in VANILLA_GAME_DLLS:
        return "game", rel
    if rel in VANILLA_GPU_DLLS or rel.startswith("d3d12/"):
        return "gpu", rel
    if "/" not in rel and rel in PROXY_DLLS:
        return "root_dll", rel
    return ("gpu" if _is_gpu(name) else "root_dll"), rel


def name_place(name: str) -> tuple[str, str]:
    """module_place of a module known only by its file name (Windows' error windows often give no folder)."""
    name = name.strip().casefold()
    if name == GAME_EXE_NAME or name in VANILLA_GAME_DLLS:
        return "game", name
    if name == REFW_DLL_NAME:  # Windows has one too, but with REFramework the game loads REFramework's
        return "reframework", name
    if name in VANILLA_GPU_DLLS:
        return "gpu", name
    if name in WINDOWS_DLLS:
        return "system", ""
    if _is_gpu(name):
        return "gpu", ""
    if name in KNOWN_PROGRAMS:
        return "other", ""
    if not name or name == "unknown" or name.startswith("stackhash"):  # WER's name for code in no module
        return "unknown", ""
    return "named", ""


def module_name(path: str) -> str:
    """The file name of a module path (as written)."""
    return str(path).strip().replace("/", "\\").rsplit("\\", 1)[-1]


# ---- privacy -----------------------------------------------------------------------------
_USER_DIR = re.compile(r"(?i)([a-z]:[\\/]+users[\\/]+)([^\\/\r\n\"'<>|:*?]+)")
_HOME_DIR = re.compile(r"((?:[A-Za-z]:)?[\\/]home[\\/]+)([^\\/\r\n\"'<>|:*?]+)")  # also Wine's Z:\home\<name>
USER_MASK = "<사용자>"


def mask_user(text: str) -> str:
    """``C:\\Users\\<name>\\`` -> ``C:\\Users\\<사용자>\\`` (any drive or separator, also in ``\\\\?\\`` paths)."""
    text = _USER_DIR.sub(lambda m: m.group(1) + (m.group(2) if m.group(2).lower() in ("public", "default")
                                                  else USER_MASK), text)
    return _HOME_DIR.sub(lambda m: m.group(1) + USER_MASK, text)


# ---- the game's own error windows ------------------------------------------------------------
_CORRUPTED = re.compile(r"file looks corrupted:\s*\"?([^\s\"]+\.pak)", re.IGNORECASE)
_D3D = re.compile(r"Fatal D3D error \((\d+),\s*(\w+),\s*(0x[0-9a-fA-F]+)\)"
                  r"(?:\s*DeviceRemovedReason\((0x[0-9a-fA-F]+)\))?")
_UNHANDLED = re.compile(r"unhandled exception(?: at)?\s*(0x[0-9a-fA-F]+)\s*\+\s*(0x[0-9a-fA-F]+)", re.IGNORECASE)
# the event log (event 1000) and Reliability Monitor (안정성 모니터; its codes have no 0x)
_EVENT_CODE = re.compile(r"(?:Exception code|예외 코드)\s*:\s*(?:0x([0-9a-fA-F]{1,8})|([0-9a-fA-F]{8}))\b",
                         re.IGNORECASE)
_EVENT_MODULE = re.compile(r"(?:Faulting module name|Fault Module Name|오류 있는 모듈 이름|오류 모듈 이름)\s*:\s*"
                           r"([^,\r\n\t]+)", re.IGNORECASE)
_EVENT_PATH = re.compile(r"(?:Faulting module path|오류 있는 모듈 경로)\s*:\s*([^\r\n]+)", re.IGNORECASE)
# Windows Error Reporting (event 1001): "P4: <module>" and the code in P7 (APPCRASH) or P8 (BEX64)
_WER_EVENT = re.compile(r"\b(APPCRASH|BEX64|BEX)\b")
_WER_PARAM = re.compile(r"^\s*P(\d{1,2})\s*:\s*(\S[^\r\n]*?)\s*$", re.MULTILINE)
D3D_OUT_OF_MEMORY = {"E_OUTOFMEMORY"}


@dataclass
class ErrorText:
    """What a pasted error window (the game's, REFramework's or Windows' event log) says."""
    corrupted_paks: list[str] = field(default_factory=list)  # "file looks corrupted: <pak>"
    d3d: list[tuple[int, str, str, str]] = field(default_factory=list)  # (number, name, HRESULT, removed reason)
    unhandled: list[str] = field(default_factory=list)  # "0x140000000 + 0xa4d69d0"
    codes: list[str] = field(default_factory=list)  # exception codes (8 hex digits)
    modules: list[str] = field(default_factory=list)  # faulting module names or paths (event log)
    faulty: list[FaultyFile] = field(default_factory=list)  # [Invalid file] / [Missing file] toasts
    scripts: list[tuple[str, int]] = field(default_factory=list)  # (script path as written, "..." if cut; line)
    aborted: bool = False  # "RE ENGINE - Aborted"
    gpu_crash: bool = False  # "GPU Crash" / graphics driver window

    def __bool__(self) -> bool:
        return bool(self.corrupted_paks or self.d3d or self.unhandled or self.codes or self.modules or self.faulty
                    or self.scripts or self.aborted or self.gpu_crash)


def read_error_text(text: str) -> ErrorText:
    out = ErrorText()
    text = str(text or "")

    def add(items: list, value) -> None:
        if value not in items:
            items.append(value)

    for m in _CORRUPTED.finditer(text):
        add(out.corrupted_paks, m.group(1))
    for m in _D3D.finditer(text):
        add(out.d3d, (int(m.group(1)), m.group(2), m.group(3).lower(), (m.group(4) or "").lower()))
    for m in _UNHANDLED.finditer(text):
        add(out.unhandled, f"{m.group(1).lower()} + {m.group(2).lower()}")
    for m in _EVENT_CODE.finditer(text):
        add(out.codes, normalize_code(m.group(1) or m.group(2)))
    for m in _EVENT_PATH.finditer(text):
        add(out.modules, m.group(1).strip())
    names = [m.group(1).strip() for m in _EVENT_MODULE.finditer(text)]
    wer = _WER_EVENT.search(text)
    if wer:
        params = {int(m.group(1)): m.group(2) for m in _WER_PARAM.finditer(text)}
        module, code = params.get(4, ""), params.get(8 if wer.group(1).startswith("BEX") else 7, "")
        if re.search(r"\.(?:dll|exe)$", module, re.IGNORECASE) or module.lower().startswith("stackhash"):
            names.append(module)
        if normalize_code(code):
            add(out.codes, normalize_code(code))
    for name in names:
        if not any(module_name(p).casefold() == name.casefold() for p in out.modules):
            add(out.modules, name)
    out.faulty = [f for f in parse_faulty("\n".join(m.group(0) for m in re.finditer(
        r"\[(?:Missing file|Invalid file|Unknown reason|PAK should be encrypted)\] [^\r\n]+", text)))]
    for line in text.splitlines():
        m = _LUA_AT.search(line)
        if m and m.start():  # "sol: runtime error: ...\reframework\autorun\foo.lua:12: ..." (cut to "..." + its end)
            path = line[:m.start() + 4].rsplit(": ", 1)[-1].strip().strip("\"'")
            add(out.scripts, (path, int(m.group(1))))
    low = text.casefold()
    out.aborted = "re engine - aborted" in low or "re engine aborted" in low
    out.gpu_crash = "gpu crash" in low or "graphics driver" in low or "그래픽 드라이버" in low
    return out
