"""Window-resize benchmark for the main window, made for real Windows (GitHub Actions windows-latest).

Builds a data folder with synthetic mods through the real ModManager (no game files: ~150 mods, some with
options and pictures, long names and descriptions, a selected mod with long 정보/충돌 text), opens the
real App (gui.App) with the log pane open and full, and measures resizing the window:

* real drag (Windows only): SendInput presses the left button on the window's bottom-right sizing border
  (found from GetWindowRect of root.wm_frame() and checked with WM_NCHITTEST = HTBOTTOMRIGHT), moves the
  mouse ~60 times by 8-15 px every ~10 ms (wider, narrower, taller) and releases it, on a worker thread
  while Tk's main loop runs (Windows' modal sizing loop runs inside Tk's event handling). Every size change
  of the toplevel (<Configure>, through a bindtag on the root) and every SendInput is timed with
  time.perf_counter(). Reported: Configure events seen per mouse move, moves coalesced (the app could not
  keep up, so Windows merged them), the gap between consecutive Configure events, the latency from a move
  to its Configure (both from the drag's second step on; the first step, laid out at once also with the
  freeze, on its own: the gap to the next Configure), and the drag time against the input time.
  Also: the longest stall of the event loop after the last move (a 5 ms after() heartbeat, in Windows'
  ~15.6 ms timer ticks; the layout deferred to the end of the drag runs then).
* synthetic steps (by the time rules: no mouse button is down, so gui.RESIZE_FIRST_STEP is off for them):
  SetWindowPos on the wrapper window for each size of the same path, each call timed, and
  the call plus update() (the Configure, layout and paints it queued), and the time from the window's
  Configure until update() returned; then the longest single update() while the app settles. On
  Linux/macOS root.geometry() steps with update() instead (to smoke-test the tool under xvfb). (Not inside
  a faked WM_ENTERSIZEMOVE: Tk would then run Python callbacks from inside the ctypes call, outside any
  _tkinter call, and _tkinter would restore a NULL thread state.)

Each variant runs in its own process (a hang or a crash is reported, not inherited; a timed out variant is
killed and the mouse button released). Variants change the app before/after it is built, to find what costs
on Windows; join modifiers with '+', e.g. --variant nolog+scale150. --list shows them. The resize modes
(gui.RESIZE_MODE: snapshot, the default; freeze; off) and the first-step detection (gui.RESIZE_FIRST_STEP,
Windows) are variants too; 'all' compares them. In the freeze and snapshot modes the steps of a drag after the
first are the same frozen step whatever the window holds, so the removals (nolog, emptylist, ...) say what costs
in a layout only with +off. Each variant reports the mode that was in effect (snapshot falls back to freeze where
there is no capture: off Windows, unless +fakecapture), the time to take and show the content's picture at the
drag's start and to bring the content back at its end (FreezeContent.timings), whether the drag's first step
was frozen and what the first-step probe read at it (the mouse button, Windows' move or size loop, the cursor on
the sizing border). A last check times the parts of a picture (GDI grab, PhotoImage, Label) and, on Windows, compares the
grab with the screen.

Output: a readable table, and one line per variant: RESIZE_BENCH_JSON {...} (also --json-out FILE, rewritten
after each variant; --report FILE prints its tables again). --budget SECONDS caps the whole run: variants that
do not fit are reported as skipped and the tables are printed either way.

  python tools/resize_bench.py                          all variants (see --list)
  python tools/resize_bench.py --variant snapshot --variant freeze --variant off --repeat 3
  python tools/resize_bench.py --screen 1920x1080       try that display mode first (Windows, CI runners)
  python tools/resize_bench.py --report resize-bench.json
  xvfb-run -a python tools/resize_bench.py --quick      smoke test of the fallback on Linux
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import random
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

JSON_PREFIX = "RESIZE_BENCH_JSON "
MIN_VARIANT_S = 30  # (--budget: a variant is not started with less time than this left)
PARTIAL_PREFIX = "RESIZE_BENCH_PARTIAL "  # (after each phase: what is kept if the variant's process dies later)
WIN = sys.platform == "win32"
pc = time.perf_counter

# ---------------------------------------------------------------------------------------------- variants
MODIFIERS = {
    "base": "150 mods (options, pictures, long names), the selected mod with a picture, long 정보 text and many "
            "conflicts, log pane open with LOG_LINES lines, 원인 찾기 bar shown with long text",
    "bare": "a plain Tk window with a few ttk widgets, no app: what resizing costs Tk itself",
    "nolog": "log pane hidden (로그 숨기기)",
    "emptylist": "no mods installed",
    "noimages": "the same mods without any pictures (no option/mod pictures)",
    "nobisect": "without the 원인 찾기 bar",
    "noheader": "header and toolbar hidden",
    "nodetails": "the details card emptied (notebook unpacked; the paned window stays)",
    "nolist": "the mod list card emptied (the paned window stays)",
    "noinfo": "the 정보 tab without text and picture",
    "fewwidgets": "only the paned window (header, toolbar, status bar, log, notice and 원인 찾기 bar hidden)",
    "options": "the 옵션 tab of a mod with option pictures shown (cards that follow the tab's width)",
    "conflicts": "the 충돌 tab shown",
    "files": "the 파일 tab shown",
    "nodefer": "RESIZE_SETTLE_MS = RESIZE_FIRST_MS = 0: no change is taken for a drag step, so every step is laid "
               "out (the same as off; not in 'all')",
    "snapshot": "gui.RESIZE_MODE = 'snapshot', the default: a picture of the content stands in for it during a drag "
                "and the content is moved out of the window (needs a capture: Windows, or +fakecapture; else it is "
                "the freeze)",
    "freeze": "gui.RESIZE_MODE = 'freeze': the content keeps its size during a drag (on Windows still drawn again on "
              "every step); with a gui before RESIZE_MODE: RESIZE_FREEZE = True",
    "off": "gui.RESIZE_MODE = 'off': the window's content laid out again on every step of a drag, as before the "
           "freeze; with a gui before RESIZE_MODE: RESIZE_FREEZE = False",
    "nofreeze": "the same as off (its older name)",
    "firststep": "gui.RESIZE_FIRST_STEP = True, the default: on Windows a size change while the mouse button is down "
                 "(or Windows' move or size loop runs) with the cursor on the sizing border is a drag step already at "
                 "the drag's first step (not in 'all')",
    "nofirststep": "gui.RESIZE_FIRST_STEP = False: the drag's first step is laid out at once (it is not told apart "
                   "from a maximize)",
    "unmap": "gui.RESIZE_SNAPSHOT_HIDE = 'unmap': while its picture shows the content is unmapped (place_forget) "
             "instead of moved out of the window (not in 'all')",
    "fakecapture": "the snapshot's picture made by a stand-in (gui.photo_from_bgrx of plain bytes, the content's "
                   "size) instead of taken from the screen: the snapshot path off Windows (not in 'all')",
    "noclassredraw": "Windows: CS_HREDRAW|CS_VREDRAW taken off Tk's window classes (TkTopLevel, TkChild) once the "
                     "window is shown, so that Windows repaints only the uncovered part of a resized window and not "
                     "every child window (an experiment, not in the app; not in 'all')",
    "nodnd": "tk.Tk() instead of TkinterDnD.Tk()",
    "scale150": "tk scaling for 150% (144 dpi) before the app is built (default: 100%, 96 dpi)",
}
# The resize modes against each other (snapshot is the default: base), with and without the first-step detection
# (Windows), and at 150 %. A removal (nolog, emptylist, ...) shows what costs in a layout only with +off: in the
# freeze and snapshot modes every step after the drag's first is the same frozen step.
REMOVALS = ["nolog", "emptylist", "noimages", "nobisect", "noheader", "nodetails", "nolist", "noinfo", "fewwidgets",
            "options", "conflicts", "files"]
ALL = ["bare", "base", "off", "freeze", "snapshot", "freeze+nofirststep", "snapshot+nofirststep", "scale150",
       "scale150+off"]
QUICK = ["bare", "base", "off", "freeze", "snapshot+fakecapture"]
MODE_PARTS = {"snapshot": "snapshot", "freeze": "freeze", "off": "off", "nofreeze": "off"}


def effective_mode(gui, fake: bool = False) -> str:
    """What the window's content does during a drag with this gui as set: snapshot (snapshot-fake: +fakecapture),
    freeze (freeze*: snapshot asked for, but no capture here), off, or layout (a gui before the freeze)."""
    if hasattr(gui, "resize_mode"):
        mode = gui.resize_mode()
        if mode == "snapshot":
            if getattr(gui, "_capture_content", None) is None:
                return "freeze*"
            return "snap-fake" if fake else "snapshot"
        return mode
    if hasattr(gui, "RESIZE_FREEZE"):
        return "freeze" if gui.RESIZE_FREEZE else "off"
    return "layout"


def parse_spec(spec: str) -> list[str]:
    spec = spec.strip().strip("'\"")  # (quotes kept by a shell that did not remove them)
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    unknown = [p for p in parts if p not in MODIFIERS]
    if unknown:
        raise ValueError(f"unknown variant part(s): {', '.join(unknown)} (see --list)")
    return parts or ["base"]


def dataset_of(parts: list[str]) -> str | None:
    if "bare" in parts:
        return None
    if "emptylist" in parts:
        return "empty"
    return "nopics" if "noimages" in parts else "full"


# ---------------------------------------------------------------------------------------------- statistics
def stats(values: list[float]) -> dict | None:
    """median/p95/max (and mean, n) of ``values`` in the unit they are in (rounded)."""
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    p95 = values[min(len(values) - 1, max(0, int(round(0.95 * (len(values) - 1)))))]
    return {"n": len(values), "median": round(statistics.median(values), 2), "p95": round(p95, 2),
            "max": round(values[-1], 2), "mean": round(statistics.fmean(values), 2)}


def ms(seconds: list[float]) -> list[float]:
    return [s * 1000.0 for s in seconds]


# ---------------------------------------------------------------------------------------------- synthetic data
LONG_NAMES = [
    "[KR 번역] Female Layered Armor Overhaul — Gore Magala α+ · Arkveld β+ HD Textures (4K)",
    "Better Hunter Hair and Eyes Retexture Collection with Physics Fix and Extra Colours",
    "아르크벨드 무기 전체 리텍스처 모음 (대검·태도·한손검·쌍검·해머·수렵피리·랜스·건랜스)",
    "No Lens Dirt, No Vignette, No Chromatic Aberration, Sharper TAA and Clear Water",
]
WORDS = ["Armor", "Weapon", "Glow", "Hair", "Retexture", "Palico", "Outfit", "Clear", "Water", "Camera",
         "레더", "메일", "방어구", "무기", "색상", "고화질", "텍스처", "모음", "수정"]
LOG_MESSAGES = [
    "적용 완료: 파일 {n}개 · PAK {p}개 · 백업 {b}개 (natives\\STM\\Art\\Model\\Character\\ch02\\004\\000\\2\\"
    "ch02_004_0002.mesh.241111606 외)",
    "'{name}' 설치 완료 — 옵션 {o}개 중 2개 사용, 충돌: 덮어씀 {n} · 밀림 {p}",
    "재질 점검: 현재 게임과 맞지 않는 파일 {n}개 (GameDesign/Equip/_Prefab/Armor/Male/003/001/003_001_avp.user, ...)",
    "Nexus에서 모드 업데이트 확인 중... {n}/{p} — Better Hunter Hair and Eyes Retexture Collection with Physics Fix",
    "게임 튕김 기록을 복사했습니다: C:\\Users\\user\\AppData\\Roaming\\MHWildsModManager\\crashes\\"
    "2026-10-21_184512 (덤프 {b}MB)",
]


def png(width: int, height: int, seed: int) -> bytes:
    """An RGB PNG (a colour with some noise) without Pillow."""
    rnd = random.Random(seed)
    color = bytes(rnd.randrange(40, 220) for _ in range(3))
    row = bytes(color * width)
    raw = bytearray()
    for _ in range(height):
        line = bytearray(row)
        for _ in range(width // 10):
            x = rnd.randrange(width) * 3
            line[x:x + 3] = rnd.randbytes(3)
        raw += b"\x00" + line

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def description(i: int, paragraphs: int = 4) -> str:
    rnd = random.Random(1000 + i)
    out = []
    for _ in range(paragraphs):
        words = " ".join(rnd.choice(WORDS) for _ in range(40))
        out.append(words + ". 이 모드는 게임의 텍스처와 재질을 바꿉니다. 설치 후 REFramework의 LooseFileLoader를 "
                           "켜 주세요. 다른 방어구 모드와 함께 쓰면 아래쪽 모드가 우선합니다.")
    return "\\n\\n".join(out)  # (modinfo.ini keeps it on one line; the app turns \n into line breaks)


def modinfo(name: str, i: int, shot: str | None, paragraphs: int = 4) -> str:
    lines = [f"name={name}", f"version=1.{i % 7}.{i % 3}", f"author=Author{i % 13}",
             f"description={description(i, paragraphs)}", "category=Armor"]
    if shot:
        lines.append(f"screenshot={shot}")
    return "\r\n".join(lines) + "\r\n"


def loose_files(i: int) -> dict:
    """An armor mod's files; the mesh and the common textures are shared with other mods (conflicts)."""
    part = 4 + i % 40
    files = {f"natives/STM/Art/Model/Character/ch02/{part:03d}/000/2/ch02_{part:03d}_0002.mesh.241111606": "m"}
    for k in range(6):
        files[f"natives/STM/Art/Model/Character/ch02/{part:03d}/000/2/tex_{i}_{k}.tex.241106027"] = "t"
    for k in range(3):
        files[f"natives/STM/common/shared_{(i + k) % 12}/tex_{k}.tex.241106027"] = f"s{i}"
    return files


def build_dataset(base: Path, kind: str, mods: int, log=print) -> Path:
    """A data folder (and a fake game folder) under ``base``; returns the data folder. ``kind``: full, nopics
    (no pictures at all) or empty (no mods). The ids the variants pick are in <data>/resize_bench.json."""
    from mhwmm.manager import ModManager

    if base.exists():
        shutil.rmtree(base)
    game = base / "MonsterHunterWilds"
    for rel, data in (("MonsterHunterWilds.exe", "exe"), ("re_chunk_000.pak", "base"), ("dinput8.dll", "refw"),
                      ("re2_fw_config.txt", "FontSize=16\r\n")):
        (game / rel).parent.mkdir(parents=True, exist_ok=True)
        (game / rel).write_text(data)
    m = ModManager(base / "data")
    m.update_settings(check_game_running=False, check_updates=False, check_refw_updates=False,
                      check_mod_updates=False, check_materials=False, auto_apply=False, remember_window=False,
                      log_visible=True, window_geometry="", window_zoomed=False)
    m.set_game_dir(game)
    picks: dict = {}
    started = pc()
    if kind != "empty":
        pictures = kind == "full"
        shots = [png(640, 360, 7 + k) for k in range(6)] if pictures else []
        src = base / "src"
        src.mkdir(parents=True)
        for i in range(mods + 1):
            hub = i == mods  # the selected mod: shares files with most others, long text, a picture
            if hub:
                name = "[선택] Ultimate Armor & Weapon Overhaul Collection — 모든 방어구·무기 고화질 텍스처 통합판"
            elif i % 6 == 0:
                name = f"{LONG_NAMES[(i // 6) % len(LONG_NAMES)]} #{i}"
            else:
                name = f"{random.Random(i).choice(WORDS)} {random.Random(i + 1).choice(WORDS)} Mod {i:03d}"
            files: dict = {}
            package = not hub and i % 5 == 0  # options (a folder per option, Fluffy style)
            if hub:
                for j in range(0, mods, 3):
                    files.update(loose_files(j))
                files["modinfo.ini"] = modinfo(name, i, "preview.png" if pictures else None, paragraphs=14)
                if pictures:
                    files["preview.png"] = shots[0]
            elif package:
                for k in range(3 + i % 3):
                    folder = f"Pack{i}/Option {k} {'긴 이름 ' * (k % 3)}".rstrip()
                    has_shot = pictures and i % 10 == 0
                    files[f"{folder}/modinfo.ini"] = modinfo(f"{name} — 옵션 {k}", i, "preview.png" if has_shot else None)
                    for rel, data in loose_files(i + k).items():
                        files[f"{folder}/{rel}"] = data
                    if has_shot:
                        files[f"{folder}/preview.png"] = shots[k % len(shots)]
            elif i % 7 == 3:
                files[f"{name.split()[0]}{i}.pak"] = "pak" * 50
                files["modinfo.ini"] = modinfo(name, i, "preview.png" if pictures else None)
                if pictures:
                    files["preview.png"] = shots[1]
            elif i % 11 == 4:
                files[f"reframework/autorun/script{i}.lua"] = "return {}"
            else:
                files.update(loose_files(i))
                shot = pictures and i % 2 == 0
                files["modinfo.ini"] = modinfo(name, i, "preview.png" if shot else None)
                if shot:
                    files["preview.png"] = shots[2 + i % 4]
            path = src / f"mod{i:03d}.zip"
            with zipfile.ZipFile(path, "w") as zf:
                for rel, data in files.items():
                    zf.writestr(rel, data)
            with m.prepare_install(path) as session:
                mod = m.install_package(session, session.variants) if package else \
                    m.install_variant(session, session.variants[0])
            if package and i % 3 != 0:
                m.set_options(mod.id, [o["id"] for o in m.get(mod.id).options[:2]])
            m.set_enabled(mod.id, hub or i % 9 != 8)
            if hub:
                picks["hub"] = mod.id
            elif package and pictures and i % 10 == 0 and "options" not in picks:
                picks["options"] = mod.id
            elif package and "options" not in picks and i >= 10:
                picks.setdefault("options_plain", mod.id)
            if (i + 1) % 50 == 0:
                log(f"    {kind}: {i + 1}/{mods + 1} mods ({pc() - started:.1f} s)")
        picks.setdefault("options", picks.get("options_plain"))
        m.deploy()
    m.save()
    (base / "data" / "resize_bench.json").write_text(json.dumps(picks))
    log(f"    {kind}: ready in {pc() - started:.1f} s")
    return base / "data"


# ---------------------------------------------------------------------------------------------- Windows API
class WinApi:
    """The few user32 calls the benchmark needs, with argtypes/restype for 64-bit."""

    INPUT_MOUSE = 0
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_VIRTUALDESK = 0x4000
    MOUSEEVENTF_ABSOLUTE = 0x8000
    WM_NCHITTEST = 0x0084
    HTBOTTOMRIGHT = 17
    GA_ROOT = 2
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x1, 0x2, 0x4, 0x10, 0x200
    SM_CXSCREEN, SM_CYSCREEN, SM_SWAPBUTTON = 0, 1, 23
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
    SPI_GETWORKAREA, SPI_GETDRAGFULLWINDOWS, SPI_SETDRAGFULLWINDOWS = 0x30, 0x26, 0x25
    DESKTOP_READOBJECTS, UOI_NAME = 0x0001, 2
    ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
    DM_PELSWIDTH, DM_PELSHEIGHT = 0x00080000, 0x00100000

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt

        self.ctypes, self.wt = ctypes, wt
        ULONG_PTR = ctypes.c_size_t  # pointer-sized, unsigned
        LRESULT = wt.LPARAM  # pointer-sized, signed (ctypes.wintypes has no LRESULT before 3.13)

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                        ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                        ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]

        class DEVMODEW(ctypes.Structure):  # the display variant of the union; sizeof = 220
            _fields_ = [("dmDeviceName", wt.WCHAR * 32), ("dmSpecVersion", wt.WORD), ("dmDriverVersion", wt.WORD),
                        ("dmSize", wt.WORD), ("dmDriverExtra", wt.WORD), ("dmFields", wt.DWORD),
                        ("dmPosition", wt.POINTL), ("dmDisplayOrientation", wt.DWORD),
                        ("dmDisplayFixedOutput", wt.DWORD), ("dmColor", ctypes.c_short),
                        ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                        ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                        ("dmFormName", wt.WCHAR * 32), ("dmLogPixels", wt.WORD), ("dmBitsPerPel", wt.DWORD),
                        ("dmPelsWidth", wt.DWORD), ("dmPelsHeight", wt.DWORD), ("dmDisplayFlags", wt.DWORD),
                        ("dmDisplayFrequency", wt.DWORD), ("dmICMMethod", wt.DWORD), ("dmICMIntent", wt.DWORD),
                        ("dmMediaType", wt.DWORD), ("dmDitherType", wt.DWORD), ("dmReserved1", wt.DWORD),
                        ("dmReserved2", wt.DWORD), ("dmPanningWidth", wt.DWORD), ("dmPanningHeight", wt.DWORD)]

        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        if ctypes.sizeof(INPUT) != expected:
            raise RuntimeError(f"sizeof(INPUT) is {ctypes.sizeof(INPUT)}, expected {expected}")
        if ctypes.sizeof(DEVMODEW) != 220:
            raise RuntimeError(f"sizeof(DEVMODEW) is {ctypes.sizeof(DEVMODEW)}, expected 220")
        self.MOUSEINPUT, self.INPUT, self.INPUTUNION, self.DEVMODEW = MOUSEINPUT, INPUT, _INPUTUNION, DEVMODEW

        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)

        def proto(name, restype, *argtypes):
            f = getattr(u, name)
            f.restype, f.argtypes = restype, list(argtypes)
            return f

        HWND = wt.HWND
        self.SendInput = proto("SendInput", wt.UINT, wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self.SetCursorPos = proto("SetCursorPos", wt.BOOL, ctypes.c_int, ctypes.c_int)
        self.GetCursorPos = proto("GetCursorPos", wt.BOOL, ctypes.POINTER(wt.POINT))
        self.GetWindowRect = proto("GetWindowRect", wt.BOOL, HWND, ctypes.POINTER(wt.RECT))
        self.SendMessageW = proto("SendMessageW", LRESULT, HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        self.WindowFromPoint = proto("WindowFromPoint", HWND, wt.POINT)
        self.GetAncestor = proto("GetAncestor", HWND, HWND, wt.UINT)
        self.SetWindowPos = proto("SetWindowPos", wt.BOOL, HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wt.UINT)
        self.GetSystemMetrics = proto("GetSystemMetrics", ctypes.c_int, ctypes.c_int)
        self.SystemParametersInfoW = proto("SystemParametersInfoW", wt.BOOL, wt.UINT, wt.UINT, ctypes.c_void_p,
                                           wt.UINT)
        self.SetForegroundWindow = proto("SetForegroundWindow", wt.BOOL, HWND)
        self.GetForegroundWindow = proto("GetForegroundWindow", HWND)
        self.IsWindow = proto("IsWindow", wt.BOOL, HWND)
        self.OpenInputDesktop = proto("OpenInputDesktop", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
        self.CloseDesktop = proto("CloseDesktop", wt.BOOL, wt.HANDLE)
        self.GetUserObjectInformationW = proto("GetUserObjectInformationW", wt.BOOL, wt.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD))
        self.EnumDisplaySettingsW = proto("EnumDisplaySettingsW", wt.BOOL, wt.LPCWSTR, wt.DWORD,
                                          ctypes.POINTER(DEVMODEW))
        self.ChangeDisplaySettingsW = proto("ChangeDisplaySettingsW", wt.LONG, ctypes.POINTER(DEVMODEW), wt.DWORD)
        try:
            self.GetDpiForWindow = proto("GetDpiForWindow", wt.UINT, HWND)
        except AttributeError:  # before Windows 10 1607
            self.GetDpiForWindow = None
        try:
            self.GetClassLong = proto("GetClassLongPtrW", ctypes.c_size_t, HWND, ctypes.c_int)
            self.SetClassLong = proto("SetClassLongPtrW", ctypes.c_size_t, HWND, ctypes.c_int, ctypes.c_ssize_t)
        except AttributeError:  # (32-bit: only the ...Long names)
            self.GetClassLong = proto("GetClassLongW", wt.DWORD, HWND, ctypes.c_int)
            self.SetClassLong = proto("SetClassLongW", wt.DWORD, HWND, ctypes.c_int, wt.LONG)

    def last_error(self) -> int:
        return self.ctypes.get_last_error()

    # -- windows
    @staticmethod
    def wrapper(root) -> int:
        """The toplevel's outer (wrapper) window: Tk's wm frame."""
        return int(str(root.wm_frame()), 16)

    def rect(self, hwnd: int) -> tuple[int, int, int, int]:
        r = self.wt.RECT()
        if not self.GetWindowRect(hwnd, self.ctypes.byref(r)):
            raise OSError(f"GetWindowRect failed (error {self.last_error()})")
        return r.left, r.top, r.right, r.bottom

    def hit_test(self, hwnd: int, x: int, y: int) -> int:
        lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)  # MAKELPARAM of screen coordinates (signed 16-bit each)
        if self.ctypes.sizeof(self.wt.LPARAM) == 4 and lparam >= 1 << 31:
            lparam -= 1 << 32
        return int(self.SendMessageW(hwnd, self.WM_NCHITTEST, 0, lparam))

    def top_window_at(self, x: int, y: int) -> int:
        hwnd = self.WindowFromPoint(self.wt.POINT(x, y))
        return int(self.GetAncestor(hwnd, self.GA_ROOT) or 0) if hwnd else 0

    def set_size(self, hwnd: int, width: int, height: int) -> bool:
        flags = self.SWP_NOMOVE | self.SWP_NOZORDER | self.SWP_NOACTIVATE | self.SWP_NOOWNERZORDER
        return bool(self.SetWindowPos(hwnd, None, 0, 0, width, height, flags))

    def dpi(self, hwnd: int) -> int | None:
        return int(self.GetDpiForWindow(hwnd)) if self.GetDpiForWindow else None

    GCL_STYLE, CS_VREDRAW, CS_HREDRAW = -26, 0x0001, 0x0002

    def drop_class_redraw(self, hwnd: int) -> tuple[int, int]:
        """Take CS_HREDRAW|CS_VREDRAW off the window class of ``hwnd`` (all windows of that class in this process);
        (the class style before, after)."""
        before = int(self.GetClassLong(hwnd, self.GCL_STYLE))
        after = before & ~(self.CS_HREDRAW | self.CS_VREDRAW)
        if after != before:
            self.SetClassLong(hwnd, self.GCL_STYLE, after)
        return before, int(self.GetClassLong(hwnd, self.GCL_STYLE))

    # -- screen
    def work_area(self) -> tuple[int, int, int, int]:
        r = self.wt.RECT()
        if self.SystemParametersInfoW(self.SPI_GETWORKAREA, 0, self.ctypes.byref(r), 0):
            return r.left, r.top, r.right, r.bottom
        return 0, 0, self.GetSystemMetrics(self.SM_CXSCREEN), self.GetSystemMetrics(self.SM_CYSCREEN)

    def screen(self) -> tuple[int, int]:
        return self.GetSystemMetrics(self.SM_CXSCREEN), self.GetSystemMetrics(self.SM_CYSCREEN)

    def drag_full_windows(self) -> bool:
        value = self.wt.BOOL()
        self.SystemParametersInfoW(self.SPI_GETDRAGFULLWINDOWS, 0, self.ctypes.byref(value), 0)
        return bool(value.value)

    def set_drag_full_windows(self, on: bool) -> bool:
        """For this session only (not saved, no WM_SETTINGCHANGE broadcast: a sizing loop reads it when it starts)."""
        return bool(self.SystemParametersInfoW(self.SPI_SETDRAGFULLWINDOWS, 1 if on else 0, None, 0))

    def input_desktop(self) -> str | None:
        """The name of the desktop that receives input ("Default" in an interactive session), None without one."""
        desk = self.OpenInputDesktop(0, False, self.DESKTOP_READOBJECTS)
        if not desk:
            return None
        try:
            buf = self.ctypes.create_unicode_buffer(256)
            needed = self.wt.DWORD()
            if self.GetUserObjectInformationW(desk, self.UOI_NAME, buf, self.ctypes.sizeof(buf),
                                              self.ctypes.byref(needed)):
                return buf.value
            return "?"
        finally:
            self.CloseDesktop(desk)

    def display_modes(self) -> list[tuple[int, int]]:
        modes, i = set(), 0
        dm = self.DEVMODEW()
        dm.dmSize = self.ctypes.sizeof(self.DEVMODEW)
        while i < 2000 and self.EnumDisplaySettingsW(None, i, self.ctypes.byref(dm)):
            modes.add((int(dm.dmPelsWidth), int(dm.dmPelsHeight)))
            i += 1
        return sorted(modes)

    def set_display_mode(self, width: int, height: int) -> int:
        """Change the primary display's resolution for this session (not saved). 0 = DISP_CHANGE_SUCCESSFUL."""
        dm = self.DEVMODEW()
        dm.dmSize = self.ctypes.sizeof(self.DEVMODEW)
        if not self.EnumDisplaySettingsW(None, self.ENUM_CURRENT_SETTINGS, self.ctypes.byref(dm)):
            return -100
        dm.dmPelsWidth, dm.dmPelsHeight = width, height
        dm.dmFields = self.DM_PELSWIDTH | self.DM_PELSHEIGHT
        return int(self.ChangeDisplaySettingsW(self.ctypes.byref(dm), 0))

    def reset_display_mode(self) -> int:
        return int(self.ChangeDisplaySettingsW(None, 0))

    # -- mouse
    def cursor(self) -> tuple[int, int]:
        p = self.wt.POINT()
        if not self.GetCursorPos(self.ctypes.byref(p)):
            raise OSError(f"GetCursorPos failed (error {self.last_error()})")
        return p.x, p.y

    def _send_mouse(self, flags: int, dx: int = 0, dy: int = 0) -> int:
        inp = self.INPUT(self.INPUT_MOUSE, self.INPUTUNION(mi=self.MOUSEINPUT(dx, dy, 0, flags, 0, 0)))
        return int(self.SendInput(1, self.ctypes.byref(inp), self.ctypes.sizeof(self.INPUT)))

    def button(self, down: bool) -> int:
        swapped = bool(self.GetSystemMetrics(self.SM_SWAPBUTTON))  # (the primary button is the right one)
        if swapped:
            flag = self.MOUSEEVENTF_RIGHTDOWN if down else self.MOUSEEVENTF_RIGHTUP
        else:
            flag = self.MOUSEEVENTF_LEFTDOWN if down else self.MOUSEEVENTF_LEFTUP
        return self._send_mouse(flag)

    def move_to(self, x: int, y: int) -> int:
        """An absolute mouse move to screen pixel (x, y) (the middle of the pixel in 0..65535 coordinates of
        the virtual desktop)."""
        vx, vy = self.GetSystemMetrics(self.SM_XVIRTUALSCREEN), self.GetSystemMetrics(self.SM_YVIRTUALSCREEN)
        vw, vh = self.GetSystemMetrics(self.SM_CXVIRTUALSCREEN), self.GetSystemMetrics(self.SM_CYVIRTUALSCREEN)
        nx = min(65535, max(0, int((x - vx + 0.5) * 65536 / max(1, vw))))
        ny = min(65535, max(0, int((y - vy + 0.5) * 65536 / max(1, vh))))
        flags = self.MOUSEEVENTF_MOVE | self.MOUSEEVENTF_ABSOLUTE | self.MOUSEEVENTF_VIRTUALDESK
        return self._send_mouse(flags, nx, ny)


def release_mouse_button() -> None:
    """Let go of the primary mouse button (after a killed variant that held it)."""
    if not WIN:
        return
    try:
        WinApi().button(down=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------------------------- child: the app
def pump(root, seconds: float) -> float:
    """Run the event loop for ``seconds``; returns the longest single update() (seconds)."""
    end, longest = pc() + seconds, 0.0
    while pc() < end:
        t = pc()
        root.update()
        longest = max(longest, pc() - t)
        time.sleep(0.002)
    return longest


class ConfigureLog:
    """Times every size change of the toplevel: a bindtag before all others (the event reaches the app's
    handlers right after), one after them, and an idle callback queued from the first: it runs in the first idle
    pass after the event ("first_idle_ms"), which is NOT the end of the layout. Tk runs only the idle work older
    than a pass in that pass; the placer queues the content's pack/grid arrangement and the redraws as newer idle
    work, so most of the layout comes after it (on Windows a sizing loop runs one idle pass per window message).
    The layout's cost is the synthetic path's Configure->done and call+update, and the real drag's gap between
    Configures and move->Configure latency. ``frozen()`` is read after the app's handlers (whether the window's
    content was frozen for that size: the drag's first step with the first-step detection); ``probe()`` before
    them at a drag's first step (what gui's first-step probe reads: the mouse button, Windows' move or size loop,
    the cursor on the sizing border)."""

    FIRST, LAST = "ResizeBenchFirst", "ResizeBenchLast"

    def __init__(self, root):
        self.root = root
        self.events: list[list] = []  # [time, width, height, after the app's handlers, idle, frozen, probe]
        self._current: list | None = None
        self.frozen = lambda: None
        self.probe = lambda: None
        root.bind_class(self.FIRST, "<Configure>", self._first)
        root.bind_class(self.LAST, "<Configure>", self._last)
        self.reset()

    def reset(self) -> None:
        """Before a measurement: the size now, and the bindtags first and last (in case they were set again)."""
        tags = tuple(t for t in self.root.bindtags() if t not in (self.FIRST, self.LAST))
        self.root.bindtags((self.FIRST,) + tags + (self.LAST,))
        self.size = (self.root.winfo_width(), self.root.winfo_height())

    def _first(self, event):
        t = pc()
        if str(event.widget) != ".":
            return
        size = (event.width, event.height)
        if size == self.size:  # moved, or the same size again
            self._current = None
            return
        self.size = size
        reading = None
        if not self.events or t - self.events[-1][0] > 0.3:  # (a drag's first step)
            try:
                reading = self.probe()
            except Exception as exc:
                reading = {"error": f"{type(exc).__name__}: {exc}"}
        record = [t, event.width, event.height, None, None, None, reading]
        self.events.append(record)
        self._current = record
        self.root.after_idle(self._idle, record)

    def _last(self, event):
        if self._current is not None and str(event.widget) == ".":
            self._current[3] = pc()
            try:
                self._current[5] = self.frozen()
            except Exception:
                pass
            self._current = None

    @staticmethod
    def _idle(record):
        record[4] = pc()

    def since(self, t0: float) -> list[list]:
        return [e for e in self.events if e[0] >= t0]


def plan_moves(count: int, seed: int = 1234) -> list[tuple[int, int]]:
    """Mouse moves (dx, dy) of 8-15 px: a third wider, a third narrower, a third taller."""
    rnd = random.Random(seed)
    third = max(1, count // 3)
    moves = [(rnd.randint(8, 15), 0) for _ in range(third)]
    moves += [(-rnd.randint(8, 15), 0) for _ in range(third)]
    moves += [(0, rnd.randint(8, 15)) for _ in range(count - 2 * third)]
    return moves


def path_extent(moves: list[tuple[int, int]]) -> tuple[int, int, int, int]:
    """(lowest x, highest x, lowest y, highest y) of the cumulative path from (0, 0)."""
    x = y = 0
    lo_x = hi_x = lo_y = hi_y = 0
    for dx, dy in moves:
        x, y = x + dx, y + dy
        lo_x, hi_x, lo_y, hi_y = min(lo_x, x), max(hi_x, x), min(lo_y, y), max(hi_y, y)
    return lo_x, hi_x, lo_y, hi_y


def same_share(data: bytes, width: int, height: int, screen) -> dict:
    """The share of sampled pixels of ``data`` (B, G, R, unused a pixel, width × height) that match the PIL image
    ``screen`` (within 8 of each channel), over the part both cover."""
    from PIL import Image

    mine = Image.frombuffer("RGB", (width, height), data, "raw", "BGRX", 0, 1)
    w, h = min(width, screen.size[0]), min(height, screen.size[1])
    same = total = 0
    step_x, step_y = max(1, w // 160), max(1, h // 100)
    for y in range(0, h, step_y):
        for x in range(0, w, step_x):
            a, b = mine.getpixel((x, y)), screen.getpixel((x, y))
            total += 1
            same += max(abs(a[i] - b[i]) for i in range(3)) <= 8
    return {"share": round(same / max(1, total), 4), "samples": total, "size": [w, h]}


class Child:
    def __init__(self, args):
        self.args = args
        self.parts = parse_spec(args.child)
        self.result: dict = {"variant": args.child, "parts": self.parts, "ok": False, "skipped": None, "error": None,
                             "notes": [], "env": {}, "drag": None, "steps": None}
        self.notes = self.result["notes"]
        self.api: WinApi | None = None
        self.root = None
        self.app = None
        self.restore_drag_setting: bool | None = None
        self.grabs: list = []  # (width, height, data) of the GDI grabs (the snapshot's pictures) of a drag

    # -- setup
    def make_root(self):
        import tkinter as tk

        from mhwmm import gui

        gui._windows_setup()  # DPI awareness, as main() (before any window)
        root = None
        if "nodnd" not in self.parts and "bare" not in self.parts:
            if gui.TkinterDnD is not None:
                try:
                    root = gui.TkinterDnD.Tk()
                except Exception as exc:
                    self.notes.append(f"TkinterDnD.Tk() failed, using tk.Tk(): {exc}")
            else:
                self.notes.append("tkinterdnd2 is not installed: tk.Tk() (as nodnd)")
        self.result["env"]["dnd"] = root is not None
        return root or tk.Tk()

    def build(self):
        import tkinter as tk
        from tkinter import ttk

        from mhwmm import gui
        from mhwmm.manager import ModManager, setup_logging

        env = self.result["env"]
        root = self.root = self.make_root()
        root.withdraw()
        env["tk"] = str(root.tk.call("info", "patchlevel"))
        env["windowing"] = str(root.tk.call("tk", "windowingsystem"))
        env["native_scaling"] = round(float(root.tk.call("tk", "scaling")), 4)
        scale = 1.5 if "scale150" in self.parts else 1.0
        root.tk.call("tk", "scaling", 96.0 * scale / 72.0)  # (Theme.scale follows: winfo_fpixels("1i") / 96)
        env["scale"] = scale

        if "bare" in self.parts:
            env["mode"] = "bare"
            root.title("resize bench (bare)")
            root.geometry("1240x800")
            root.minsize(400, 300)
            frame = ttk.Frame(root, padding=20)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text="A plain window: Tk's own cost of a resize step.").pack(anchor="w")
            ttk.Button(frame, text="Button").pack(anchor="e")
            ttk.Entry(frame).pack(fill="x", pady=8)
            tk.Text(frame, height=6).pack(fill="both", expand=True)
            root.deiconify()
            pump(root, 0.5)
            return

        # what would run in the background or show a dialog: not part of a resize
        for name in ("showinfo", "showwarning", "showerror"):
            setattr(gui.messagebox, name, lambda *a, **k: self.notes.append("a message box was answered") or None)
        for name in ("askyesno", "askyesnocancel", "askokcancel", "askretrycancel"):
            setattr(gui.messagebox, name, lambda *a, **k: self.notes.append("a question was answered no") or False)
        gui.App.warm_equipment = lambda self_: None
        gui.App.precheck_background = lambda self_: None
        gui.App._fetch_unread_requirements = lambda self_: None
        if hasattr(gui, "is_game_running"):
            gui.is_game_running = lambda: False
        if "nodefer" in self.parts:
            gui.RESIZE_SETTLE_MS = 0
            gui.RESIZE_FIRST_MS = 0
        skip = self.set_modes(gui)
        if skip:
            self.result["skipped"] = skip
            return
        env["resize_settle_ms"] = gui.RESIZE_SETTLE_MS

        work = Path(self.args.work)
        data = work / "data"
        if data.exists():
            shutil.rmtree(data)
        shutil.copytree(self.args.template, data)
        picks = json.loads((data / "resize_bench.json").read_text())
        manager = ModManager(data)
        events: queue.Queue = queue.Queue()
        setup_logging(manager.data_dir, gui.QueueLogHandler(events))  # (as main(): log lines reach the pane)
        app = self.app = gui.App(root, manager, events)
        root.deiconify()
        env["theme_scale"] = app.theme.scale
        env["mods"] = len(manager.mods)
        env["log_lines"] = getattr(gui, "LOG_LINES", None)
        pump(root, 0.6)
        if hasattr(app, "_hide_notice"):
            app._hide_notice()
        self.pick(picks)
        self.fill_log()
        self.modify()
        pump(root, max(0.5, self.args.settle))
        env["shown"] = self.shown()

    def set_modes(self, gui) -> str | None:
        """The resize switches the variant names (gui.RESIZE_MODE, RESIZE_FIRST_STEP, ...), before the app is built;
        the reason to skip the variant when this gui does not have one."""
        parts, env = self.parts, self.result["env"]
        modes = {MODE_PARTS[p] for p in parts if p in MODE_PARTS}
        if len(modes) > 1:
            return f"more than one resize mode: {', '.join(sorted(modes))}"
        if modes:
            mode = modes.pop()
            if hasattr(gui, "RESIZE_MODE"):
                gui.RESIZE_MODE = mode
                if hasattr(gui, "RESIZE_FREEZE"):
                    gui.RESIZE_FREEZE = True  # (its False would be "off" whatever the mode)
            elif mode != "snapshot" and hasattr(gui, "RESIZE_FREEZE"):
                gui.RESIZE_FREEZE = mode == "freeze"  # (a gui before RESIZE_MODE)
            else:
                return f"gui has no RESIZE_MODE{'' if mode == 'snapshot' else ' or RESIZE_FREEZE'} in this version"
        for part, value in (("firststep", True), ("nofirststep", False)):
            if part in parts:
                if not hasattr(gui, "RESIZE_FIRST_STEP"):
                    return "gui.RESIZE_FIRST_STEP does not exist in this version"
                gui.RESIZE_FIRST_STEP = value
        if "unmap" in parts:
            if not hasattr(gui, "RESIZE_SNAPSHOT_HIDE"):
                return "gui.RESIZE_SNAPSHOT_HIDE does not exist in this version"
            gui.RESIZE_SNAPSHOT_HIDE = "unmap"
        if "fakecapture" in parts:
            if not hasattr(gui, "_capture_content") or not hasattr(gui, "photo_from_bgrx"):
                return "gui has no snapshot capture in this version"

            def fake(frame, width, height):  # (the same conversion and Label as a real picture)
                return gui.photo_from_bgrx(frame, b"\x60\x48\x30\x00" * (width * height), width, height)

            gui._capture_content = fake
        env["resize_mode"] = getattr(gui, "RESIZE_MODE", None)
        env["resize_freeze"] = getattr(gui, "RESIZE_FREEZE", None)
        env["mode"] = effective_mode(gui, fake="fakecapture" in parts)
        env["first_step"] = getattr(gui, "RESIZE_FIRST_STEP", None)
        pointer = getattr(gui, "_pointer", None)
        env["first_step_probe"] = (pointer() is not None) if pointer else None  # (Windows' mouse button state)
        env["snapshot_hide"] = getattr(gui, "RESIZE_SNAPSHOT_HIDE", None)
        return None

    def timings_reset(self) -> None:
        """Forget the content's picture timings (FreezeContent.timings) before a measurement."""
        timings = getattr(getattr(self.app, "freeze", None), "timings", None)
        if timings is not None:
            timings.clear()

    def timings_take(self) -> dict:
        """What FreezeContent did during a measurement: pictures taken and shown at a drag's start (snapshot_ms),
        content brought back at its end (restore_ms: laid out and drawn), frozen without a picture (no_snapshot)."""
        timings = list(getattr(getattr(self.app, "freeze", None), "timings", None) or [])
        snaps = [ms_ for what, ms_ in timings if what == "snapshot"]
        restores = [ms_ for what, ms_ in timings if what == "restore"]
        return {"snapshots": len(snaps), "no_snapshot": sum(1 for what, _ in timings if what == "no snapshot"),
                "snapshot_ms": stats(snaps), "restore_ms": stats(restores),
                "first_snapshot_ms": round(snaps[0], 1) if snaps else None}

    def capture_check(self) -> dict:
        """The parts of a picture of the content timed: the PhotoImage made from pixels (Pillow, or PPM without it)
        and its Label (Tk makes the image's copy for the screen then), at the content's size and at 2560x1400; on
        Windows also the GDI grab, and how much of it matches the screen (ImageGrab, the window on top)."""
        import tkinter as tk

        from mhwmm import gui

        content = getattr(self.app, "content", None)
        if content is None or not hasattr(gui, "photo_from_bgrx"):
            return {"path": "capture", "skipped": "no content frame or no photo_from_bgrx in this gui"}
        root = self.root
        width, height = content.winfo_width(), content.winfo_height()
        out: dict = {"path": "capture", "size": [width, height], "pillow": gui.Image is not None}
        label = tk.Label(root, borderwidth=0, highlightthickness=0)  # (never placed)

        def photo(w: int, h: int) -> dict:
            data = b"\x60\x48\x30\x00" * (w * h)
            t0 = pc()
            image = gui.photo_from_bgrx(root, data, w, h)
            t1 = pc()
            label.configure(image=image)
            t2 = pc()
            label.configure(image="")
            del image
            t3 = pc()
            return {"size": [w, h], "photo_ms": round((t1 - t0) * 1000, 1), "label_ms": round((t2 - t1) * 1000, 1),
                    "drop_ms": round((t3 - t2) * 1000, 1)}

        try:
            photo(16, 16)  # (Pillow's Tk part loads at its first picture)
            out["photos"] = [photo(width, height), photo(2560, 1400)]
            capture = getattr(gui, "_capture_content", None)
            if WIN and capture is not None and capture is getattr(gui, "_gdi_capture", None):
                grabber = gui._CAPTURE[0] if gui._CAPTURE else gui._WinCapture()
                times = []
                for _ in range(3):
                    t0 = pc()
                    data = grabber.grab(content.winfo_id(), width, height)
                    times.append((pc() - t0) * 1000)
                out["grab_ms"] = stats(times)
                out["grab_bytes"] = len(data)
                out["same_as_screen"] = self.compare_with_screen(content, width, height)
        finally:
            label.destroy()
        out["ok"] = True
        return out

    def compare_with_screen(self, content, width: int, height: int) -> dict:
        """The share of sampled pixels of a GDI grab of the content that match a screen grab (ImageGrab) of the
        same place, with the window on top (a grab that missed the window's surface is black or stale)."""
        from PIL import ImageGrab

        from mhwmm import gui

        root = self.root
        grabber = gui._CAPTURE[0] if gui._CAPTURE else gui._WinCapture()
        try:
            root.attributes("-topmost", True)
            root.lift()
            pump(root, 0.4)
            x, y = content.winfo_rootx(), content.winfo_rooty()
            data = grabber.grab(content.winfo_id(), width, height)
            screen = ImageGrab.grab(bbox=(x, y, x + width, y + height), all_screens=True).convert("RGB")
        finally:
            try:
                root.attributes("-topmost", False)
            except Exception:
                pass
        if screen.size != (width, height):
            return {"error": f"screen grab {screen.size} vs {(width, height)}"}
        return dict(same_share(data, width, height, screen), at=[x, y])

    def record_grabs(self) -> None:
        """Keep the GDI grabs (the snapshot's pictures) of a drag, to compare them with the screen before it."""
        from mhwmm import gui

        cls = getattr(gui, "_WinCapture", None)
        if not WIN or cls is None:
            return
        original, grabs = cls.grab, self.grabs

        def grab(capture, hwnd, width, height):
            data = original(capture, hwnd, width, height)
            if len(grabs) < 3:
                grabs.append((width, height, data))
            return data

        cls.grab = grab

    def screen_of_content(self):
        """What the screen shows where the window's content is (the window is on top), as a PIL image; None off
        Windows or without Pillow."""
        content = getattr(self.app, "content", None)
        if content is None or not WIN:
            return None
        try:
            from PIL import ImageGrab

            x, y = content.winfo_rootx(), content.winfo_rooty()
            w, h = content.winfo_width(), content.winfo_height()
            return ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).convert("RGB")
        except Exception as exc:
            self.notes.append(f"no screen grab before the drag: {exc}")
            return None

    def drop_class_redraw(self) -> dict | None:
        """noclassredraw: CS_HREDRAW|CS_VREDRAW off Tk's toplevel (wrapper) and child window classes."""
        if not self.api:
            self.notes.append("noclassredraw: only on Windows")
            return None
        root, out = self.root, {}
        for name, hwnd in (("TkTopLevel", self.api.wrapper(root)), ("TkChild", root.winfo_id())):
            before, after = self.api.drop_class_redraw(hwnd)
            out[name] = [hex(before), hex(after)]
        return out

    def shown(self) -> dict:
        """Which parts of the window are on screen after modify() (a variant that hides nothing shows up here)."""
        from tkinter import ttk

        app = self.app
        host = getattr(app, "content", self.root)
        header = next((w for w in host.winfo_children() if isinstance(w, ttk.Frame)
                       and str(w.cget("style")) == "Header.TFrame"), None)
        parts = {"header": header, "toolbar": getattr(app, "toolbar", None),
                 "status": getattr(app, "status_frame", None), "log": getattr(app, "log_frame", None),
                 "notice": getattr(app, "notice", None), "bisect": getattr(app, "bisect_bar", None),
                 "list": getattr(app, "tree", None), "details": getattr(app, "notebook", None)}
        out = {}
        for name, widget in parts.items():
            try:
                out[name] = bool(widget is not None and widget.winfo_viewable())
            except Exception:
                out[name] = None
        return out

    @staticmethod
    def quiet_s() -> float:
        """How long the window must keep its size before the next change is one on its own (not a drag step;
        with the freeze: laid out at once), plus a margin."""
        from mhwmm import gui

        return (getattr(gui, "RESIZE_SETTLE_MS", 120) + getattr(gui, "RESIZE_FIRST_MS", 600)) / 1000 + 0.2

    def pick(self, picks: dict):
        app, root = self.app, self.root
        want = picks.get("options") if "options" in self.parts else picks.get("hub")
        if not want:
            return
        app.tree.selection_set(want)
        app.tree.focus(want)
        app.tree.see(want)
        pump(root, 0.3)
        if "options" in self.parts:
            app.show_options(want)
            end = pc() + 5  # option thumbnails load one at a time after the tab is drawn
            last, since = -1, pc()
            while pc() < end:
                pump(self.root, 0.05)
                n = len(getattr(app, "_option_images", []))
                if n != last:
                    last, since = n, pc()
                elif pc() - since > 0.6:
                    break
            self.result["env"]["option_pictures"] = last
        elif "conflicts" in self.parts:
            app.notebook.select(app.conflicts_tab)
        elif "files" in self.parts:
            app.notebook.select(app.notebook.tabs()[2])

    def fill_log(self):
        import logging

        from mhwmm import gui

        app = self.app
        rnd = random.Random(5)
        levels = [logging.INFO] * 6 + [logging.WARNING, logging.ERROR]
        for _ in range(getattr(gui, "LOG_LINES", 300) + 20):
            msg = rnd.choice(LOG_MESSAGES).format(n=rnd.randrange(9999), p=rnd.randrange(99), b=rnd.randrange(99),
                                                  o=rnd.randrange(9), name=LONG_NAMES[0])
            app._append_log(rnd.choice(levels), msg)

    def modify(self):
        from tkinter import ttk

        app, root, parts = self.app, self.root, self.parts
        px = app.theme.px
        if "nobisect" not in parts and "fewwidgets" not in parts:
            app.bisect_title.configure(text="원인 찾기 · 3번째 실행")
            app.bisect_text.configure(text="모드 14개를 끄고 게임을 실행합니다. 튕기던 상황(예: 9분 정도 플레이)까지 해 "
                                           "보고 돌아와서 결과를 알려 주세요.\n의심: ArtianQoL 41% · 방어구 A 리텍스처 "
                                           "18% · Gajau Recolor 9% — 남은 실행 4~6번\n지난 답으로 ArtianQoL의 가능성이 "
                                           "22%에서 41%로 올랐습니다")
            app.bisect_stop.pack(side="right")
            app.bisect_main.configure(text="▶  게임 실행")
            app.bisect_main.pack(side="right", padx=(0, px(6)))
            app.bisect_bar.pack(fill="x", padx=px(20), pady=(px(10), 0), before=app.paned)
        if "nolog" in parts and app._log_visible:
            app.toggle_log()
        host = getattr(app, "content", root)  # (the window's content is in one frame since the freeze)
        if "noheader" in parts:
            slaves = host.pack_slaves()
            for i, w in enumerate(slaves):
                if isinstance(w, ttk.Frame) and str(w.cget("style")) == "Header.TFrame":
                    w.pack_forget()
                    if i + 1 < len(slaves) and isinstance(slaves[i + 1], ttk.Separator):
                        slaves[i + 1].pack_forget()
            app.toolbar.pack_forget()
        if "fewwidgets" in parts:
            for w in host.pack_slaves():
                if w is not app.paned:
                    w.pack_forget()
        left, right = (app.paned.nametowidget(str(p)) for p in app.paned.panes())
        if "nodetails" in parts:
            for w in right.pack_slaves():
                w.pack_forget()
        if "nolist" in parts:
            for w in left.pack_slaves():
                w.pack_forget()
        if "noinfo" in parts:
            app._show_preview(None, "")
            app._set_text(app.info_text, [])

    # -- window placement
    def place(self, moves: list[tuple[int, int]]) -> dict:
        """Put the window where the whole path fits on the screen; returns the plan (inner/outer start sizes)."""
        root = self.root
        lo_x, hi_x, lo_y, hi_y = path_extent(moves)
        margin = 24
        px = self.app.theme.px if self.app else (lambda n: int(n))
        want_w, want_h = px(1240), px(800)  # the app's default window size
        root.maxsize(20000, 20000)  # (Windows keeps a window within the screen unless told otherwise)
        if self.api:
            left, top, right, bottom = self.api.work_area()
        else:
            left, top, right, bottom = 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
        x0, y0 = left + margin, top + margin
        root.state("normal")
        root.geometry(f"{want_w}x{want_h}+{x0}+{y0}")
        pump(root, 0.3)
        frame_w = frame_h = 0
        if self.api:
            l, t, r, b = self.api.rect(self.api.wrapper(root))
            frame_w, frame_h = (r - l) - root.winfo_width(), (b - t) - root.winfo_height()
        room_w = right - margin - x0 - frame_w - hi_x
        room_h = bottom - margin - y0 - frame_h - hi_y
        inner_w, inner_h = min(want_w, room_w), min(want_h, room_h)
        if inner_w < want_w or inner_h < want_h:
            self.notes.append(f"the screen ({right - left}x{bottom - top} work area) is too small for the default "
                              f"{want_w}x{want_h} window and the drag: started at {inner_w}x{inner_h}")
        min_w, min_h = (int(v) for v in root.minsize())
        low_w, low_h = inner_w + lo_x, inner_h + lo_y
        if low_w - 8 < min_w or low_h - 8 < min_h:
            root.minsize(min(min_w, max(100, low_w - 40)), min(min_h, max(100, low_h - 40)))
            self.notes.append(f"minsize {min_w}x{min_h} lowered to {root.minsize()[0]}x{root.minsize()[1]} so that "
                              "the drag is not stopped by it")
        root.geometry(f"{inner_w}x{inner_h}+{x0}+{y0}")
        pump(root, self.quiet_s())  # (the first measurement's first step is a change on its own)
        plan = {"inner": (root.winfo_width(), root.winfo_height()), "position": (x0, y0),
                "work_area": (left, top, right, bottom)}
        if self.api:
            l, t, r, b = self.api.rect(self.api.wrapper(root))
            plan["outer"] = (r - l, b - t)
            plan["rect"] = (l, t, r, b)
        return plan

    def restore(self, plan: dict):
        w, h = plan["inner"]
        x, y = plan["position"]
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        # (more than RESIZE_SETTLE_MS + RESIZE_FIRST_MS: the next measurement's first step is not taken for a step
        # of this change, nor for one going on after a drag's pause)
        pump(self.root, max(self.quiet_s(), self.args.settle / 2))

    # -- measurements
    def real_drag(self, moves: list[tuple[int, int]], plan: dict, rec: ConfigureLog) -> dict:
        api, root = self.api, self.root
        out: dict = {"path": "SendInput", "ok": False}
        hwnd = api.wrapper(root)
        if not api.IsWindow(hwnd):
            out["error"] = f"wm frame {hwnd:#x} is not a window"
            return out
        # on top and in front before the sizing border is looked for (Tk's first map does not raise or activate
        # the window: another window over its corner would make every hit test fail)
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        try:
            root.lift()
            pump(root, 0.2)
            out["foreground_set"] = bool(api.SetForegroundWindow(hwnd))
            pump(root, 0.2)
            return self._drag(hwnd, moves, rec, out)
        finally:
            try:
                root.attributes("-topmost", False)
            except Exception:
                pass

    def _drag(self, hwnd: int, moves: list[tuple[int, int]], rec: ConfigureLog, out: dict) -> dict:
        api, root = self.api, self.root
        left, top, right, bottom = api.rect(hwnd)
        corner, codes = None, []
        for d in (3, 2, 4, 5, 1, 6, 7, 8, 10):
            x, y = right - d, bottom - d  # (right/bottom are outside the window)
            code = api.hit_test(hwnd, x, y)
            on_top = api.top_window_at(x, y)
            codes.append((d, code, on_top == hwnd))
            if code == api.HTBOTTOMRIGHT and on_top == hwnd:
                corner = (x, y)
                break
        out["hit_tests"] = codes
        if corner is None:
            out["error"] = "no bottom-right sizing border found (WM_NCHITTEST/WindowFromPoint): " + repr(codes)
            return out
        x0, y0 = corner
        path, x, y = [], x0, y0
        for dx, dy in moves:
            x, y = x + dx, y + dy
            path.append((x, y))
        inner0 = (root.winfo_width(), root.winfo_height())
        expected = [(inner0[0] + px_ - x0, inner0[1] + py_ - y0) for px_, py_ in path]
        interval = self.args.interval_ms / 1000.0
        state: dict = {"moves": [], "error": None}
        done = threading.Event()

        def drive():
            try:
                if not api.SetCursorPos(x0, y0):
                    state["error"] = f"SetCursorPos failed (error {api.last_error()})"
                    return
                time.sleep(0.25)
                where = api.cursor()
                state["cursor_start"] = where
                if abs(where[0] - x0) > 1 or abs(where[1] - y0) > 1:
                    state["error"] = f"the cursor is at {where}, not at {(x0, y0)} after SetCursorPos"
                    return
                state["t_down"] = pc()
                if api.button(down=True) != 1:
                    state["error"] = f"SendInput refused the button press (error {api.last_error()})"
                    state.pop("t_down")
                    return
                time.sleep(0.15)  # (the sizing loop starts on the press)
                start = pc()
                for i, (px_, py_) in enumerate(path):
                    delay = start + i * interval - pc()
                    if delay > 0:
                        time.sleep(delay)
                    t = pc()
                    sent = api.move_to(px_, py_)
                    state["moves"].append((t, pc(), sent))
                time.sleep(0.15)
                state["cursor_end"] = api.cursor()
                state["t_up"] = pc()
                api.button(down=False)
            except BaseException as exc:  # reported in the result
                state["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                if "t_down" in state and "t_up" not in state:
                    state["t_up"] = pc()
                    try:
                        api.button(down=False)
                    except Exception:
                        pass
                done.set()

        deadline = pc() + self.args.drag_timeout
        beats: list[float] = []

        def beat():  # (when it does not run, the event loop is busy: the stall after the drag)
            beats.append(pc())
            state["beat"] = root.after(5, beat)

        def poll():
            now = pc()
            if now > deadline:  # (this runs inside a sizing loop too: Windows' timer messages reach Tk there)
                state["timeout"] = not done.is_set()
                api.button(down=False)
                root.quit()
                return
            if done.is_set():
                last = rec.events[-1][0] if rec.events else 0.0
                if now - max(last, state.get("t_up", 0.0)) > 0.5:
                    root.quit()
                    return
            root.after(20, poll)

        before = self.screen_of_content()  # (what the snapshot's picture should show)
        self.grabs.clear()
        rec.reset()
        self.timings_reset()
        cpu0, wall0 = time.process_time(), pc()
        worker = threading.Thread(target=drive, name="resize-bench-mouse", daemon=True)
        worker.start()
        root.after(20, poll)
        state["beat"] = root.after(5, beat)
        root.mainloop()
        root.after_cancel(state["beat"])
        worker.join(5)
        out["cpu_s"] = round(time.process_time() - cpu0, 3)
        out["wall_s"] = round(pc() - wall0, 3)
        if worker.is_alive():
            out["error"] = "the mouse thread did not finish"
        if state.get("timeout"):
            out["error"] = f"timed out after {self.args.drag_timeout} s"
        if state["error"]:
            out["error"] = state["error"]
        out.update(self.timings_take())
        if before is not None and self.grabs:  # the drag's first picture against the screen before the drag
            try:
                out["picture_vs_screen"] = same_share(*self.grabs[0], before)
            except Exception as exc:
                out["picture_vs_screen"] = {"error": f"{type(exc).__name__}: {exc}"}
        self.grabs.clear()
        out["cursor_start"], out["cursor_end"] = state.get("cursor_start"), state.get("cursor_end")
        if state.get("cursor_end") and max(abs(a - b) for a, b in zip(state["cursor_end"], path[-1])) > 1:
            self.notes.append(f"the cursor ended at {state['cursor_end']}, the path at {path[-1]}")
        sent = state["moves"]
        out["moves"] = len(sent)
        out["moves_refused"] = sum(1 for m in sent if m[2] != 1)
        if not sent or "t_down" not in state:
            out.setdefault("error", "no mouse input was sent")
            return out
        events = [e for e in rec.events if e[0] >= state["t_down"]]
        out["configures"] = len(events)
        expected = expected[:len(sent)]  # (the mouse thread may have stopped early)
        final = (root.winfo_width(), root.winfo_height())
        out["final"], out["final_expected"] = final, expected[-1]
        out["final_ok"] = abs(final[0] - expected[-1][0]) <= 2 and abs(final[1] - expected[-1][1]) <= 2
        if not events:
            out["error"] = out.get("error") or ("the window never changed size: the press did not start a sizing "
                                                "loop (no interactive desktop, another window on top, or input "
                                                "blocked)")
            return out
        # each Configure to the move that asked for its size (in order; a move without one was coalesced)
        latencies, matched, j = [], 0, 0  # (latencies: one per Configure, None when no move matched it)
        for e in events:
            latency = None
            for i in range(j, len(expected)):
                if abs(expected[i][0] - e[1]) <= 2 and abs(expected[i][1] - e[2]) <= 2:
                    latency = e[0] - sent[i][0]
                    matched += 1
                    j = i + 1
                    break
            latencies.append(latency)
        times = [e[0] for e in events]
        gaps = [b - a for a, b in zip(times, times[1:])]
        first_move, last_move = sent[0][0], sent[-1][0]
        after = [b for b in beats if b >= last_move]
        stall = max((b - a for a, b in zip(after, after[1:])), default=None)
        out.update({
            "matched": matched,
            "coalesced": len(sent) - matched,
            # the drag's first step is laid out at once (with the freeze too: it is not told apart from a maximize),
            # so its layout is in the gap to the next Configure; the steps after it are what dragging costs
            "first_gap_ms": round(gaps[0] * 1000, 1) if gaps else None,
            "first_latency_ms": round(latencies[0] * 1000, 1) if latencies[0] is not None else None,
            # the drag's first step frozen (on Windows with the first-step detection), None: no freeze in this gui
            "first_frozen": events[0][5],
            # what the first-step probe read at it (Windows: the button, the move or size loop, on the border)
            "first_probe": events[0][6],
            "gap_ms": stats(ms(gaps[1:])),
            "latency_ms": stats(ms([v for v in latencies[1:] if v is not None])),
            "handlers_ms": stats(ms([e[3] - e[0] for e in events if e[3] is not None])),
            "first_idle_ms": stats(ms([e[4] - e[0] for e in events if e[4] is not None])),
            "input_s": round(last_move - first_move, 3),
            "drag_s": round(times[-1] - first_move, 3),
            "tail_ms": round((times[-1] - last_move) * 1000, 1),
            "send_ms": stats(ms([b - a for a, b, _ in sent])),
            "stall_ms": round(stall * 1000, 1) if stall is not None else None,
            "ok": out.get("error") is None,
        })
        return out

    def synthetic(self, moves: list[tuple[int, int]], plan: dict, rec: ConfigureLog) -> dict:
        """The synthetic steps, by the time rules: no mouse button is pressed for them, so on Windows the
        first-step detection (gui.RESIZE_FIRST_STEP) would take each one for a change on its own (each laid out
        at once whenever the one before took longer than RESIZE_SETTLE_MS). Off for them, the first step is laid
        out at once and the next ones are frozen, as in a drag the mouse does not show."""
        from mhwmm import gui

        first_step = getattr(gui, "RESIZE_FIRST_STEP", None)
        if first_step:
            gui.RESIZE_FIRST_STEP = False
        try:
            return self._synthetic(moves, plan, rec)
        finally:
            if first_step:
                gui.RESIZE_FIRST_STEP = first_step

    def _synthetic(self, moves: list[tuple[int, int]], plan: dict, rec: ConfigureLog) -> dict:
        root, api = self.root, self.api
        sizes = []
        if api:
            w, h = plan["outer"]
            path = "SetWindowPos"
        else:
            w, h = plan["inner"]
            path = "geometry"
        for dx, dy in moves:
            w, h = w + dx, h + dy
            sizes.append((w, h))
        out: dict = {"path": path, "n": len(sizes)}
        sync, step, done = [], [], []  # done: from the root's Configure until update() returned (or None)
        rec.reset()
        self.timings_reset()
        t_start = pc()
        cpu0 = time.process_time()

        def configured(seen: int, end: float) -> float | None:
            return end - rec.events[seen][0] if len(rec.events) > seen else None

        if api:
            # (Tk only queues the Configure inside the call: Tcl_ServiceAll does nothing outside a sizing loop,
            # so no Python callback runs from inside ctypes; update() then handles it)
            hwnd = api.wrapper(root)
            for w, h in sizes:
                seen = len(rec.events)
                t0 = pc()
                ok = api.set_size(hwnd, w, h)
                t1 = pc()
                root.update()
                t2 = pc()
                if not ok:
                    out["error"] = f"SetWindowPos failed (error {api.last_error()})"
                    break
                sync.append(t1 - t0)
                step.append(t2 - t0)
                done.append(configured(seen, t2))
        else:
            for w, h in sizes:
                seen = len(rec.events)
                t0 = pc()
                root.geometry(f"{w}x{h}")
                root.update()
                end = t0 + 0.5
                while (root.winfo_width(), root.winfo_height()) != (w, h) and pc() < end:
                    root.update()
                t1 = pc()
                sync.append(t1 - t0)
                step.append(t1 - t0)
                done.append(configured(seen, t1))
        out["cpu_s"] = round(time.process_time() - cpu0, 3)
        out["total_s"] = round(pc() - t_start, 3)
        out["stall_ms"] = round(pump(root, 0.6) * 1000, 1)  # (what was deferred to the end of the "drag")
        events = rec.since(t_start)
        out["configures"] = len(events)
        out.update(self.timings_take())
        out["first_frozen"] = events[0][5] if events else None
        # the first step is laid out at once (with the freeze too: a change on its own); from the second on, the
        # steps of a drag
        out["sync_ms"] = stats(ms(sync[1:]))
        out["first_ms"] = round(step[0] * 1000, 1) if step else None
        out["step_ms"] = stats(ms(step[1:]))
        out["done_ms"] = stats(ms([v for v in done[1:] if v is not None]))
        out["handlers_ms"] = stats(ms([e[3] - e[0] for e in events if e[3] is not None]))
        out["first_idle_ms"] = stats(ms([e[4] - e[0] for e in events if e[4] is not None]))
        out["ok"] = "error" not in out and bool(sync)
        return out

    # -- the whole run
    def run(self) -> dict:
        env = self.result["env"]
        env.update({"platform": sys.platform, "python": sys.version.split()[0], "pid": os.getpid()})
        try:
            if WIN:
                try:
                    self.api = WinApi()
                except Exception as exc:
                    self.notes.append(f"the Windows API is not usable: {exc}")
            if self.api:
                env["input_desktop"] = self.api.input_desktop()
                env["screen"] = self.api.screen()
                env["work_area"] = self.api.work_area()
                env["drag_full_windows"] = self.api.drag_full_windows()
                env["session"] = os.environ.get("SESSIONNAME")
            self.build()
            if self.result["skipped"]:
                return self.result
            root = self.root
            if self.api:
                env["dpi"] = self.api.dpi(self.api.wrapper(root))
            rec = ConfigureLog(root)
            freeze = getattr(self.app, "freeze", None)
            if freeze is not None:
                rec.frozen = lambda: bool(freeze.frozen)
            probe = first_step_probe()
            if probe is not None:
                rec.probe = lambda: probe_reading(probe, root)
            if "noclassredraw" in self.parts:
                env["class_styles"] = self.drop_class_redraw()
            self.record_grabs()
            moves = plan_moves(self.args.moves)
            plan = self.place(moves)
            self.result["plan"] = plan
            drags, steps = [], []
            if not self.api:
                self.result["drag"] = {"path": None, "skipped": f"no SendInput on {sys.platform}" if not WIN
                                       else "the Windows API is not usable"}
            elif self.args.no_drag:
                self.result["drag"] = {"path": None, "skipped": "--no-drag"}
            for n in range(self.args.repeat):
                if self.api and not self.args.no_drag:
                    if not self.api.drag_full_windows() and not self.args.keep_drag_setting:
                        if self.api.set_drag_full_windows(True):
                            self.restore_drag_setting = False
                            self.notes.append("'show window contents while dragging' was off: turned on for the drag")
                    drags.append(self.guarded(self.real_drag, "SendInput", moves, plan, rec))
                    self.result["drag"] = merge_drags(drags)
                    self.emit(PARTIAL_PREFIX)
                    self.restore(plan)
                steps.append(self.guarded(self.synthetic, "SetWindowPos" if self.api else "geometry", moves, plan,
                                          rec))
                self.result["steps"] = merge_steps(steps)
                self.emit(PARTIAL_PREFIX)
                self.restore(plan)
            if self.app is not None:
                self.result["capture"] = self.guarded(self.capture_check, "capture")
            self.result["ok"] = bool((self.result["drag"] or {}).get("ok") or (self.result["steps"] or {}).get("ok"))
        except BaseException as exc:
            self.result["error"] = f"{type(exc).__name__}: {exc}"
            self.result["traceback"] = traceback.format_exc()
        finally:
            if self.restore_drag_setting is not None and self.api:
                self.api.set_drag_full_windows(self.restore_drag_setting)
        return self.result

    @staticmethod
    def guarded(measure, path: str, *args) -> dict:
        try:
            return measure(*args)
        except Exception as exc:  # (the other measurement still runs)
            return {"path": path, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()}

    def emit(self, prefix: str) -> None:
        print(prefix + json.dumps(self.result, ensure_ascii=True), flush=True)

    def close(self):
        import gc

        try:
            if self.app is not None:
                self.app.watcher.stop()
            gc.collect()  # (Tk variables before the interpreter: Python 3.10 can crash otherwise)
            if self.root is not None:
                self.root.destroy()
        except Exception:
            pass


def join_stats(parts: list[dict | None]) -> dict | None:
    """The stats of several runs in one: the median of the medians and the worst run's p95/max (not joined
    exactly)."""
    parts = [p for p in parts if p]
    if not parts:
        return None
    return {"n": sum(p["n"] for p in parts), "median": round(statistics.median(p["median"] for p in parts), 2),
            "p95": max(p["p95"] for p in parts), "max": max(p["max"] for p in parts)}


# what the first-step probe is asked (gui._WinPointer): {key: (method, it takes the toplevel)}
PROBE_QUERIES = {"down": ("button_down", False), "loop": ("in_move_size", True), "border": ("on_sizing_border", True)}


def first_step_probe():
    """gui's first-step probe (gui._pointer(): Windows only), or None."""
    from mhwmm import gui

    pointer = getattr(gui, "_pointer", None)
    try:
        return pointer() if pointer is not None and getattr(gui, "RESIZE_FIRST_STEP", False) else None
    except Exception:
        return None


def probe_reading(probe, top) -> dict:
    """What the first-step probe reads now: {key: True/False, None (no such query in this gui), or the error}."""
    out: dict = {}
    for key, (name, with_top) in PROBE_QUERIES.items():
        query = getattr(probe, name, None)
        try:
            out[key] = None if query is None else bool(query(top) if with_top else query())
        except Exception as exc:
            out[key] = f"{type(exc).__name__}: {exc}"
    return out


def merge_drags(runs: list[dict]) -> dict:
    """All repeats of the real drag in one summary (lists joined), with each run's short form."""
    good = [r for r in runs if r.get("configures")]
    out: dict = {"path": "SendInput", "runs": [{k: r.get(k) for k in ("ok", "moves", "configures", "matched",
                                                                       "coalesced", "first_gap_ms", "input_s",
                                                                       "drag_s", "tail_ms", "stall_ms", "final_ok",
                                                                       "first_frozen", "first_probe",
                                                                       "snapshots",
                                                                       "first_snapshot_ms", "picture_vs_screen",
                                                                       "error", "cpu_s", "wall_s")}
                                                for r in runs]}
    if not good:
        out["ok"] = False
        out["error"] = next((r.get("error") for r in runs if r.get("error")), "no run")
        out["hit_tests"] = runs[0].get("hit_tests") if runs else None
        out["traceback"] = next((r["traceback"] for r in runs if r.get("traceback")), None)
        return out
    out["ok"] = all(r.get("ok") for r in good)
    for key in ("moves", "configures", "matched", "coalesced", "snapshots", "no_snapshot"):
        out[key] = sum(r.get(key) or 0 for r in good)
    out["first_frozen"] = sum(1 for r in good if r.get("first_frozen"))  # (runs whose first step was frozen)
    readings = [r["first_probe"] for r in good if isinstance(r.get("first_probe"), dict)]
    if readings:  # (runs where each of the probe's queries read true, or failed)
        out["first_probe"] = {key: sum(1 for p in readings if p.get(key) is True) for key in PROBE_QUERIES}
        out["first_probe"]["runs"] = len(readings)
        out["first_probe"]["errors"] = sum(1 for p in readings if any(isinstance(v, str) for v in p.values()))
    out["n_runs"] = len(good)
    for key in ("gap_ms", "latency_ms", "handlers_ms", "first_idle_ms", "send_ms", "snapshot_ms", "restore_ms"):
        joined = join_stats([r.get(key) for r in good])
        if joined:
            out[key] = joined
    for key in ("first_gap_ms", "first_latency_ms"):  # (one value per run)
        out[key] = stats([r.get(key) for r in good])
    for key in ("input_s", "drag_s", "tail_ms", "cpu_s", "wall_s"):
        out[key] = round(statistics.median(r[key] for r in good if r.get(key) is not None), 3)
    stalls = [r["stall_ms"] for r in good if r.get("stall_ms") is not None]
    out["stall_ms"] = max(stalls) if stalls else None  # (the worst run)
    shares = [r["picture_vs_screen"]["share"] for r in good if (r.get("picture_vs_screen") or {}).get("share") is not None]
    out["picture_vs_screen"] = min(shares) if shares else None  # (the worst run)
    out["final_ok"] = all(r.get("final_ok") for r in good)
    errors = [r.get("error") for r in runs if r.get("error")]
    if errors:
        out["error"] = errors[0]
    return out


def merge_steps(runs: list[dict]) -> dict:
    good = [r for r in runs if r.get("sync_ms")]
    out: dict = {"path": runs[0]["path"] if runs else None, "runs": len(runs)}
    if not good:
        out["ok"] = False
        out["error"] = next((r.get("error") for r in runs if r.get("error")), "no run")
        out["traceback"] = next((r["traceback"] for r in runs if r.get("traceback")), None)
        return out
    out["ok"] = all(r.get("ok") for r in good)
    out["n"] = sum(r["n"] for r in good)
    out["configures"] = sum(r["configures"] for r in good)
    for key in ("snapshots", "no_snapshot"):
        out[key] = sum(r.get(key) or 0 for r in good)
    out["first_frozen"] = sum(1 for r in good if r.get("first_frozen"))
    out["n_runs"] = len(good)
    for key in ("sync_ms", "step_ms", "done_ms", "handlers_ms", "first_idle_ms", "snapshot_ms", "restore_ms"):
        joined = join_stats([r.get(key) for r in good])
        if joined:
            out[key] = joined
    out["first_ms"] = stats([r.get("first_ms") for r in good])  # (one value per run)
    for key in ("total_s", "cpu_s"):
        out[key] = round(statistics.median(r[key] for r in good), 3)
    out["stall_ms"] = max(r.get("stall_ms") or 0.0 for r in good)  # (the worst run)
    return out


def child_main(args) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.setswitchinterval(0.001)  # (the mouse thread gets the GIL quickly while Tk callbacks run)
    child = Child(args)
    child.run()
    child.emit(JSON_PREFIX)
    child.close()
    return 0


# ---------------------------------------------------------------------------------------------- parent
def fmt_num(value) -> str:
    return "-" if value is None else f"{value:.0f}"


def fmt_stat(s: dict | None) -> str:
    if not s:
        return "-"
    return f"{s['median']:.1f}/{s['p95']:.1f}/{s['max']:.1f}"


def fmt_runs(s: dict | None) -> str:
    """One value per run: median/max of the runs."""
    if not s:
        return "-"
    return f"{s['median']:.0f}/{s['max']:.0f}"


def fmt_mm(s: dict | None) -> str:
    """median/max."""
    if not s:
        return "-"
    return f"{s['median']:.0f}/{s['max']:.0f}"


def mode_of(r: dict) -> str:
    env = r.get("env") or {}
    mode = env.get("mode")
    if mode is None:  # (a result from before the modes)
        freeze = env.get("resize_freeze")
        mode = "-" if freeze is None else ("freeze" if freeze else "off")
    return str(mode)[:9]


def print_picture_tables(results: list[dict]) -> None:
    print()
    print("Resize mode and the content's picture (FreezeContent.timings; ms as median/max over the drags/step runs): "
          "1st step = RESIZE_FIRST_STEP\n  (probe: Windows' mouse button read); 1st frz = runs whose first step was "
          "frozen / runs; snaps = pictures taken; snapshot = taking and\n  showing the picture at the drag's start; "
          "restore = bringing the content back at its end (laid out and drawn); no snap = frozen without a\n  "
          "picture (capture failed or refused); pic=scr = share of the drag's first picture equal to the screen before "
          "the drag\n  (Windows, worst run; 1.0: the picture showed the content)")
    head = f"{'variant':<24} {'mode':<9} {'1st step':<9} | {'drag: 1st frz':>13} {'snaps':>5} {'snapshot':>9} " \
           f"{'restore':>9} {'no snap':>7} {'pic=scr':>7} | {'steps: 1st frz':>14} {'snaps':>5} {'snapshot':>9} " \
           f"{'restore':>9}"
    print(head)
    print("-" * len(head))
    for r in results:
        name = r["variant"][:24]
        if r.get("skipped"):
            print(f"{name:<24} skipped: {r['skipped']}")
            continue
        env = r.get("env") or {}
        first = env.get("first_step")
        first = "-" if first is None else ("on" if first else "off") + ("+probe" if env.get("first_step_probe") else "")
        cells = []
        for part in (r.get("drag") or {}, r.get("steps") or {}):
            runs = part.get("n_runs")
            cells.append((f"{part.get('first_frozen', 0)}/{runs}" if runs else "-",
                          part.get("snapshots", "-") if runs else "-", fmt_mm(part.get("snapshot_ms")),
                          fmt_mm(part.get("restore_ms")), part.get("no_snapshot", "-") if runs else "-"))
        (df, ds, dsn, dr, dn), (sf, ss, ssn, sr, _sn) = cells
        share = (r.get("drag") or {}).get("picture_vs_screen")
        share = "-" if share is None else f"{share:.3f}"
        print(f"{name:<24} {mode_of(r):<9} {first:<9} | {df:>13} {ds!s:>5} {dsn:>9} {dr:>9} {dn!s:>7} {share:>7} | "
              f"{sf:>14} {ss!s:>5} {ssn:>9} {sr:>9}")
    probed = [(r["variant"], (r.get("drag") or {}).get("first_probe")) for r in results if not r.get("skipped")]
    probed = [(variant, p) for variant, p in probed if p]
    if probed:
        print()
        print("First-step probe at the real drag's first step (gui._pointer, read before the app's handlers): runs "
              "where it read true / runs;\n  down = the primary mouse button, loop = Windows' move or size loop, "
              "border = the cursor on the sizing border; errors = runs where a query failed")
        head = f"{'variant':<24} {'down':>6} {'loop':>6} {'border':>6} {'errors':>6}"
        print(head)
        print("-" * len(head))
        for variant, p in probed:
            runs = p.get("runs", 0)
            print(f"{variant[:24]:<24} " + " ".join(f"{p.get(key, 0)}/{runs}".rjust(6) for key in PROBE_QUERIES)
                  + f" {p.get('errors', 0):>6}")
    checks = [(r["variant"], r.get("capture")) for r in results if not r.get("skipped") and r.get("capture")]
    if not checks:
        return
    print()
    print("Parts of a picture (once per variant, after the measurements): photo = PhotoImage from pixels (Pillow, or "
          "PPM), label = giving it to\n  a Label (Tk's copy for the screen), at the content's size and at 2560x1400; "
          "grab = GDI BitBlt from the content's window (Windows);\n  same = share of sampled grab pixels equal to a "
          "screen grab of the same place (1.0: the grab is what the screen shows)")
    head = f"{'variant':<24} {'content':>9} {'photo':>6} {'label':>6} | {'2560x1400':>9} {'photo':>6} {'label':>6} | " \
           f"{'grab':>11} {'same':>6}"
    print(head)
    print("-" * len(head))
    for variant, c in checks:
        name = variant[:24]
        if c.get("error") or c.get("skipped"):
            print(f"{name:<24} {(c.get('error') or c.get('skipped'))[:100]}")
            continue
        photos = c.get("photos") or []
        a = photos[0] if photos else {}
        b = photos[1] if len(photos) > 1 else {}
        size = "x".join(str(v) for v in a.get("size", [])) or "-"
        same = (c.get("same_as_screen") or {})
        share = same.get("share")
        print(f"{name:<24} {size:>9} {fmt_num(a.get('photo_ms')):>6} {fmt_num(a.get('label_ms')):>6} | {'':>9} "
              f"{fmt_num(b.get('photo_ms')):>6} {fmt_num(b.get('label_ms')):>6} | {fmt_stat(c.get('grab_ms')):>11} "
              f"{'-' if share is None else f'{share:.3f}':>6}" + (f"  ({same['error']})" if same.get("error") else ""))


def print_tables(results: list[dict]) -> None:
    print()
    print("Read: mode = what the window's content does during a drag (snapshot: its picture stands in for it and "
          "the content is out of the\n  window; freeze: it keeps its size; off: laid out on every step; freeze*: "
          "snapshot asked for, no capture here; snap-fake:\n  +fakecapture). snapshot/freeze vs off: what they save; "
          "vs bare: what is left. +nofirststep: the drag's first step laid out at once.")
    print()
    print("Real edge drag (SendInput; ms as median/p95/max; cfg/moves = Configure events / mouse moves; coal = moves "
          "without their own Configure;\n  1st gap = from the drag's first Configure to the next (its layout; "
          "median/max of the runs); gap and move->Configure: from the second step on;\n  tail = last Configure after "
          "the last move; stall = longest gap of a 5 ms after() heartbeat after the last move (Windows timers tick "
          "every\n  ~15.6 ms: below ~20 is no stall))")
    head = f"{'variant':<24} {'mode':<9} {'cfg/moves':>9} {'coal':>5} {'1st gap':>9} {'gap between Configures':>23} " \
           f"{'move->Configure':>18} {'drag/input s':>13} {'tail':>6} {'stall':>6}"
    print(head)
    print("-" * len(head))
    for r in results:
        d = r.get("drag") or {}
        name = r["variant"][:24]
        if r.get("skipped"):
            print(f"{name:<24} skipped: {r['skipped']}")
            continue
        if not d.get("configures"):
            why = r.get("error") or d.get("error") or d.get("skipped") or "no data"
            print(f"{name:<24} {why[:110]}")
            continue
        print(f"{name:<24} {mode_of(r):<9} {d['configures']:>4}/{d['moves']:<4} {d['coalesced']:>5} "
              f"{fmt_runs(d.get('first_gap_ms')):>9} {fmt_stat(d.get('gap_ms')):>23} {fmt_stat(d.get('latency_ms')):>18} "
              f"{d['drag_s']:>6.2f}/{d['input_s']:<6.2f} {d['tail_ms']:>6.0f} {fmt_num(d.get('stall_ms')):>6}")
    print()
    print("Synthetic steps (SetWindowPos + update() on Windows, geometry + update() elsewhere; ms as median/p95/max; "
          "call, call+update and\n  Configure->done (from the window's Configure until update() returned: handlers, layout, "
          "redraw) from the second step on; 1st = the first step's\n  call+update (median/max of the runs); stall = "
          "longest update() while it settles)")
    head = f"{'variant':<24} {'mode':<9} {'path':<13} {'n':>4} {'call':>17} {'call+update':>20} " \
           f"{'Configure->done':>20} {'1st':>9} {'stall':>6} {'total s':>8}"
    print(head)
    print("-" * len(head))
    for r in results:
        s = r.get("steps") or {}
        name = r["variant"][:24]
        if r.get("skipped"):
            print(f"{name:<24} skipped: {r['skipped']}")
            continue
        if not s.get("sync_ms"):
            why = r.get("error") or s.get("error") or "no data"
            print(f"{name:<24} {why[:110]}")
            continue
        print(f"{name:<24} {mode_of(r):<9} {s['path']:<13} {s['n']:>4} {fmt_stat(s.get('sync_ms')):>17} "
              f"{fmt_stat(s.get('step_ms')):>20} {fmt_stat(s.get('done_ms')):>20} {fmt_runs(s.get('first_ms')):>9} "
              f"{fmt_num(s.get('stall_ms')):>6} {s['total_s']:>8.2f}")
    print_picture_tables(results)
    shown = [(r["variant"], (r.get("env") or {}).get("shown")) for r in results if not r.get("skipped")]
    shown = [(v, parts) for v, parts in shown if parts]
    if shown:
        print()
        print("On screen while measured")
        for variant, parts in shown:
            print(f"  {variant:<24} {' '.join(k for k, on in parts.items() if on) or '(none of the parts)'}")
    notes = [(r["variant"], n) for r in results for n in dict.fromkeys(r.get("notes") or [])]
    if notes:
        print()
        print("Notes")
        for variant, note in notes:
            print(f"  {variant}: {note}")
    paths = {r["variant"]: ("real drag" if (r.get("drag") or {}).get("configures") else
                            "synthetic only" if (r.get("steps") or {}).get("sync_ms") else "nothing")
             for r in results if not r.get("skipped")}
    worked = sorted(set(paths.values()))
    print()
    print(f"Measured with: {', '.join(worked) if worked else 'nothing'}")


def run_child(args, spec: str, template: Path | None, work: Path, timeout: float | None = None) -> dict:
    timeout = args.timeout if timeout is None else timeout
    cmd = [sys.executable, os.path.abspath(__file__), "--child", spec, "--work", str(work),
           "--moves", str(args.moves), "--interval-ms", str(args.interval_ms), "--repeat", str(args.repeat),
           "--settle", str(args.settle), "--drag-timeout", str(args.drag_timeout)]
    if template is not None:
        cmd += ["--template", str(template)]
    for flag in ("no_drag", "keep_drag_setting"):
        if getattr(args, flag):
            cmd.append("--" + flag.replace("_", "-"))
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    started = pc()
    result: dict | None = None
    output, error = "", None
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, env=env,
                              cwd=str(ROOT))
        output = proc.stdout.decode("utf-8", "replace")
        if proc.returncode:
            error = f"the variant's process exited with {proc.returncode}"
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        error = f"timed out after {timeout:.0f} s (killed)"
        release_mouse_button()
    partial: dict | None = None
    for line in output.splitlines():
        for prefix in (JSON_PREFIX, PARTIAL_PREFIX):
            if line.startswith(prefix):
                try:
                    value = json.loads(line[len(prefix):])
                except ValueError as exc:
                    error = error or f"unreadable result line: {exc}"
                    break
                if prefix == JSON_PREFIX:
                    result = value
                else:
                    partial = value
                break
        else:
            if line.strip():
                print(f"    | {line}")
    if result is None and partial is not None:  # died after a phase: what it measured until then
        result = partial
        result["error"] = result.get("error") or (error or "the variant's process ended early") + \
            " (partial result)"
    if result is None:
        result = {"variant": spec, "ok": False, "error": error or "the variant printed no result", "notes": []}
    elif error and not result.get("error"):
        result["notes"] = (result.get("notes") or []) + [error]
    result["seconds"] = round(pc() - started, 1)
    return result


def report(path: str) -> int:
    """The tables of an earlier run's --json-out file (the CI job summary is made this way: it does not depend on
    the run having got to its own tables)."""
    try:
        results = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"--report: cannot read {path}: {exc}")
        return 1
    if not isinstance(results, list):
        print(f"--report: {path} is not a list of results")
        return 1
    print_tables(results)
    return 0


def parent_main(args) -> int:
    begun = pc()
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    if args.report:
        return report(args.report)
    if args.list:
        for name in MODIFIERS:
            print(f"{name:<14} {MODIFIERS[name]}")
        print(f"\nall: {' '.join(ALL)}\nquick: {' '.join(QUICK)}")
        return 0
    specs: list[str] = []
    for value in args.variant or []:
        for spec in value.replace(",", " ").split():
            specs.extend(ALL if spec.lower() == "all" else [spec])
    if not specs:
        specs = list(QUICK if args.quick else ALL)
    try:
        for spec in specs:
            parse_spec(spec)
    except ValueError as exc:
        print(exc)
        return 2
    print(f"resize bench: {len(specs)} variant(s) on {sys.platform}, Python {sys.version.split()[0]}; "
          f"{args.moves} moves every {args.interval_ms} ms, {args.repeat} run(s) each")
    api = None
    changed_display = False
    if WIN:
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # (as the app and the variants: real pixels)
        except Exception:
            pass
        try:
            api = WinApi()
            print(f"screen {api.screen()}, work area {api.work_area()}, input desktop {api.input_desktop()!r}, "
                  f"show window contents while dragging: {api.drag_full_windows()}")
        except Exception as exc:
            print(f"the Windows API is not usable: {exc}")
    if args.screen:
        if api is None:
            print("--screen: only on Windows")
        else:
            try:
                w, h = (int(v) for v in args.screen.lower().split("x"))
                modes = api.display_modes()
                if (w, h) not in modes and modes:
                    fits = [m for m in modes if m[0] <= w and m[1] <= h] or modes
                    w, h = max(fits, key=lambda m: m[0] * m[1])
                    print(f"--screen: {args.screen} is not a display mode here; trying {w}x{h} "
                          f"(modes: {', '.join(f'{a}x{b}' for a, b in modes[-12:])})")
                before = api.screen()
                code = api.set_display_mode(w, h) if (w, h) != before else 0
                changed_display = code == 0 and (w, h) != before
                print(f"--screen: ChangeDisplaySettings {w}x{h} -> {code} (0 = done); screen {before} -> "
                      f"{api.screen()}")
            except Exception as exc:
                print(f"--screen: could not change the display mode: {exc}")
    tmp = Path(args.work or tempfile.mkdtemp(prefix="resize-bench-"))
    tmp.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    built: dict[str, Path] = {}

    def save():  # (after each variant: a job stopped by its time limit keeps what it measured)
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(results, indent=1, ensure_ascii=False), encoding="utf-8")

    try:
        for n, spec in enumerate(specs, 1):
            left = args.budget - (pc() - begun) if args.budget else None
            if left is not None and left < MIN_VARIANT_S:  # (the tables are still printed, with what was measured)
                results.append({"variant": spec, "ok": False, "notes": [],
                                "skipped": f"not run: the --budget of {args.budget:.0f} s was used up"})
                print(f"[{n}/{len(specs)}] {spec}: skipped (--budget used up)", flush=True)
                save()
                continue
            kind = dataset_of(parse_spec(spec))
            if kind is not None and kind not in built:
                print(f"building the {kind} data ({0 if kind == 'empty' else args.mods} mods)...", flush=True)
                built[kind] = build_dataset(tmp / f"data-{kind}", kind, args.mods,
                                            log=lambda s: print(s, flush=True))
            print(f"[{n}/{len(specs)}] {spec}", flush=True)
            timeout = args.timeout
            if args.budget:
                timeout = max(MIN_VARIANT_S, min(timeout, args.budget - (pc() - begun)))
            result = run_child(args, spec, built.get(kind), tmp / f"run-{n:02d}", timeout)
            results.append(result)
            print(JSON_PREFIX + json.dumps(result, ensure_ascii=True), flush=True)
            save()
            d, s = result.get("drag") or {}, result.get("steps") or {}
            print(f"    -> {result.get('skipped') or result.get('error') or 'done'}; drag: "
                  f"{d.get('configures', '-')}/{d.get('moves', '-')} Configure/moves, gap {fmt_stat(d.get('gap_ms'))}; "
                  f"steps: {fmt_stat(s.get('step_ms'))} ms ({result.get('seconds')} s)", flush=True)
    finally:
        if changed_display and api is not None:
            print(f"display mode reset -> {api.reset_display_mode()}")
        if not args.keep and not args.work:
            shutil.rmtree(tmp, ignore_errors=True)
    print_tables(results)
    if args.json_out:
        print(f"\nwritten: {args.json_out}")
    measured = [r for r in results if (r.get("drag") or {}).get("configures") or (r.get("steps") or {}).get("sync_ms")]
    return 0 if measured else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--variant", action="append", help="a variant (modifiers joined by '+'); repeatable; 'all'")
    p.add_argument("--list", action="store_true", help="list the variant modifiers")
    p.add_argument("--quick", action="store_true", help=f"fewer variants ({', '.join(QUICK)}), mods, moves and runs")
    p.add_argument("--mods", type=int, default=None, help="mods in the data (default 150; --quick 40)")
    p.add_argument("--moves", type=int, default=None, help="mouse moves per drag (default 60; --quick 30)")
    p.add_argument("--interval-ms", type=float, default=10.0, help="time between mouse moves (default 10)")
    p.add_argument("--repeat", type=int, default=None, help="drags and step runs per variant (default 2; --quick 1)")
    p.add_argument("--settle", type=float, default=None, help="seconds to let the app settle (default 2.5)")
    p.add_argument("--timeout", type=float, default=240, help="seconds per variant before it is killed")
    p.add_argument("--budget", type=float, default=None,
                   help="seconds for the whole run (data included): a variant gets at most what is left, and the "
                        "ones that do not fit are reported as skipped (the tables are printed either way)")
    p.add_argument("--report", metavar="FILE", help="only print the tables of a --json-out file")
    p.add_argument("--drag-timeout", type=float, default=30, help="seconds one real drag may take")
    p.add_argument("--screen", help="Windows: change the display mode first, e.g. 1920x1080 (reset at the end)")
    p.add_argument("--no-drag", action="store_true", help="only the synthetic steps")
    p.add_argument("--keep-drag-setting", action="store_true",
                   help="do not turn on 'show window contents while dragging' when it is off")
    p.add_argument("--json-out", help="write all results as JSON to this file")
    p.add_argument("--work", help="folder for the data and runs (default: a temporary folder, removed)")
    p.add_argument("--keep", action="store_true", help="keep the temporary folder")
    p.add_argument("--child", help=argparse.SUPPRESS)
    p.add_argument("--template", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    args.mods = args.mods if args.mods is not None else (40 if args.quick else 150)
    args.moves = max(3, args.moves if args.moves is not None else (30 if args.quick else 60))
    args.repeat = max(1, args.repeat if args.repeat is not None else (1 if args.quick else 2))
    args.settle = args.settle if args.settle is not None else (1.5 if args.quick else 2.5)
    if args.child:
        return child_main(args)
    return parent_main(args)


if __name__ == "__main__":
    sys.exit(main())
