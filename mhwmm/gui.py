"""Tkinter user interface."""

from __future__ import annotations

import collections
import gc
import logging
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import (__version__, accessed, bisect, crashes, crashlog, evidence, fsutil, gamewatch, matfix, nexus, precheck,
               refw, steam, updater)
from .analyzer import classify, package_name
from .archive import ARCHIVE_EXTS, ArchiveError
from .constants import (APP_TITLE, GAME_EXE, NEXUS_SEARCH_URL, NEXUS_URL, PAK_MODS_DIR, REFRAMEWORK_URL,
                        STEAM_APP_ID)
from .deploy import DeployError, DeployReport, is_root_pak, path_key
from .helptext import SECTIONS as HELP_SECTIONS
from .manager import (InstallError, InstallSession, MaterialJob, MaterialRun, Mod, ModManager,
                      acquire_instance_lock, is_game_running, prepare_data_dir, resolve_data_dir, setup_logging)
from .matfix import MatfixError
from .precheck import stuck_summary
from .theme import THEME_NAMES, Theme, set_dark_titlebar
from .updater import Release, UpdateError

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
except Exception:  # optional dependency
    TkinterDnD = None
    DND_FILES = None

try:
    from PIL import Image, ImageTk  # type: ignore
except Exception:  # optional dependency
    Image = ImageTk = None

log = logging.getLogger(__name__)

KIND_LABELS = {
    "reframework": "REFramework",
    "pak": "PAK",
    "loose": "루스 파일",
    "script": "REF 스크립트",
    "dll": "DLL",
    "other": "기타",
}
MAX_FILE_ROWS = 3000
# Lines kept in the log pane (all of them go to manager.log). Tk measures every line of a word-wrapped Text
# again whenever it gets wider or narrower, so a long log made resizing the window stutter.
LOG_LINES = 300
INSTALL_FILETYPES = [
    ("모드 파일", " ".join(f"*{e}" for e in (*ARCHIVE_EXTS, ".pak", ".lua", ".dll"))),
    ("모든 파일", "*.*"),
]
# Mod list filters: label -> predicate(mod, conflicts)
FILTERS = {
    "전체": None,
    "활성": lambda mod, conflicts: mod.enabled,
    "비활성": lambda mod, conflicts: not mod.enabled,
    "옵션 선택 필요": lambda mod, conflicts: mod.needs_option,
    "충돌 있음": lambda mod, conflicts: any(conflicts.counts(mod.id)),
    "업데이트 있음": lambda mod, conflicts: bool(mod.update),
    "필요한 모드 없음": None,  # these two need the manager: handled in App.refresh
    "문제 있음": None,
}
FILTER_ALL = "전체"
FILTER_UPDATES = "업데이트 있음"
FILTER_MISSING = "필요한 모드 없음"
FILTER_PROBLEMS = "문제 있음"
# Errors that are shown to the user without a traceback in the log.
EXPECTED_ERRORS = (DeployError, InstallError, ArchiveError, UpdateError, MatfixError, OSError, ValueError)


RETARGET_TITLE = "다른 장비로 옮기기"
RETARGET_ACTION = RETARGET_TITLE + "..."  # (retargetview.RetargetDialog) in the context menu and the 장비 외형 window

KIND_SHORT = {"reframework": "REF", "pak": "PAK", "loose": "루스", "script": "스크립트", "dll": "DLL", "other": "기타"}


def kinds_text(kinds: set[str]) -> str:
    return " + ".join(label for k, label in KIND_LABELS.items() if k in kinds) or "-"


def kinds_short(kinds: set[str]) -> str:
    return " · ".join(label for k, label in KIND_SHORT.items() if k in kinds) or "-"


def format_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def shorten_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = limit - 1
    return text[: keep // 2] + "…" + text[-(keep - keep // 2):]


def _virtual_screen(root: tk.Misc) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the desktop across all monitors."""
    if sys.platform == "win32":
        try:
            import ctypes

            metric = ctypes.windll.user32.GetSystemMetrics  # type: ignore[attr-defined]
            left, top = metric(76), metric(77)  # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN
            return left, top, left + metric(78), top + metric(79)
        except Exception:
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def load_image(path: Path, width: int, height: int):
    """``path`` as a PhotoImage that fits in width × height, or None if it can't be shown."""
    try:
        if Image is not None:
            with Image.open(path) as img:
                img.draft(img.mode, (width, height))  # JPEG: decode at a smaller size, much faster
                img.thumbnail((width, height))
                return ImageTk.PhotoImage(img)
        if path.suffix.lower() in (".png", ".gif"):
            img = tk.PhotoImage(file=str(path))
            factor = max(1, -(-img.width() // width), -(-img.height() // height))
            return img.subsample(factor) if factor > 1 else img
    except Exception as exc:
        log.debug("미리보기 이미지를 열 수 없습니다: %s", exc)
    return None


def autohide(scrollbar: ttk.Scrollbar, **pack_options):
    """yscrollcommand that only shows ``scrollbar`` while there is something to scroll."""
    def set_(first, last):
        if float(first) <= 0.0 and float(last) >= 1.0:
            if scrollbar.winfo_manager():
                scrollbar.pack_forget()
        elif not scrollbar.winfo_manager():
            scrollbar.pack(**pack_options)
        scrollbar.set(first, last)
    return set_


# While a window's edge is dragged, Tk gets a new size for every mouse step (on Windows each one is handled
# before the next mouse move). The window's content keeps its size during the drag (FreezeContent) and follows
# once the size has not changed for this long.
RESIZE_SETTLE_MS = 120
# A change on its own (maximize, restore) is followed at once. If it was the first step of a drag, laying the
# window out for it can take longer than RESIZE_SETTLE_MS (all the tiles of 장비 외형 at 150%), so a change this
# soon after a change on its own is the drag's next step (else each step would be laid out at once again). The
# same after a frozen drag's content followed (a pause with the mouse button still down, FreezeContent._settled).
# On Windows only with the mouse button down or in Windows' move or size loop (RESIZE_FIRST_STEP).
RESIZE_FIRST_MS = 600
# What FreezeContent does while the window's edge is dragged (resize_mode()):
#   "freeze": the content keeps its size: nothing in it is laid out again until the drag stops (and on Windows,
#             with RESIZE_CLASS_REDRAW, nothing in it is drawn again either).
#   "off":    the content is laid out again on every step (as before the freeze; for the resize benchmark).
# (Any other value is the freeze.)
RESIZE_MODE = "freeze"
RESIZE_MODES = ("freeze", "off")
# The older switch: False is RESIZE_MODE "off".
RESIZE_FREEZE = True
# Windows: the mouse tells a drag of the window's edge from the other size changes, already at the drag's first
# step (_drag_step; else that step lays the whole window out):
#   - a change of the wm state (maximize, restore) is never one: also a title-bar double-click (it acts on the
#     button's press, so the button is down) and a maximized window dragged by its title bar; a zoomed window has
#     no edge to drag, and snapping or a double-click on the top border keeps "normal";
#   - with the primary button up and no move or size loop (Win+arrow keys, the maximize button), or with "show
#     window contents while dragging" off (Windows sizes the window once, at the drag's end), a change is one only
#     right after the last change (RESIZE_SETTLE_MS; RESIZE_FIRST_MS is for a drag the mouse does not show);
#   - with the button down or in Windows' move or size loop (a pen or a finger) it is one when the cursor is on
#     the window's sizing border, or when the frame kept at least two of its edges and was not as wide or as tall
#     as the work area before or after the change (a drag of an edge keeps three edges, of a corner two; dragging
#     a snapped window's title bar gives it back its size from before the snap and moves it), or by the time rules
#     (RESIZE_SETTLE_MS, RESIZE_FIRST_MS, FreezeContent._settled: else the step that goes on with the drag after a
#     pause, the cursor ahead of a snapped window's edge, would lay the whole window out again).
# Only queries (no window message is sent); when one fails, the time rules decide as elsewhere.
RESIZE_FIRST_STEP = True
# Windows: Tk registers its window classes with CS_HREDRAW|CS_VREDRAW (TkTopLevel, a toplevel's outer window:
# tkWinWm.c; TkChild, every other Tk window, the toplevel's own one too: tkWinX.c), so on each step of a drag of
# the window's edge Windows has the resized windows repainted whole, with all their child windows, also the ones
# whose size did not change (the content FreezeContent keeps as it is too), and Tk draws each of them again. What
# class_redraw takes these two styles off, once per process (a class's style is the process's: all its Tk
# windows of that class):
#   "keep":  nothing (Tk as it is);
#   "top":   the outer windows' class only;
#   "child": the class of all the other windows only;
#   "both":  both classes.
# Without them Windows repaints only what a resize uncovered; Tk's widgets draw themselves again whole when their
# own size changes (their <Configure>). Combines with RESIZE_MODE: "both" with the freeze took a step of a drag
# of the main window from ~145 ms (everything laid out and drawn) to ~10 ms, as a plain Tk window, with no stale
# pixel after a drag, a maximize, a theme switch or in the dialogs and 장비 외형 (tools/resize_bench.py on real
# Windows; the freeze alone, ~50 ms, still had all the content drawn again).
RESIZE_CLASS_REDRAW = "both"
RESIZE_CLASS_REDRAWS = ("keep", "top", "child", "both")
_RESIZE_TAG = "MhwmmResize"


def resize_mode() -> str:
    """The resize mode in effect: RESIZE_MODE, or "off" when RESIZE_FREEZE is False."""
    if not RESIZE_FREEZE:
        return "off"
    return RESIZE_MODE if RESIZE_MODE in RESIZE_MODES else "freeze"


def _win_functions(dll: str, specs: dict) -> dict:
    """ctypes functions of a Windows DLL with their argument and result types: {name: (restype, *argtypes)}."""
    import ctypes

    lib = ctypes.WinDLL(dll, use_last_error=True)  # type: ignore[attr-defined]
    out = {}
    for name, (restype, *argtypes) in specs.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, list(argtypes)
        out[name] = fn
    return out


class _WinPointer:
    """The mouse button, Windows' move or size loop, where the cursor is on a toplevel's frame and where the frame
    is, as Windows has them (RESIZE_FIRST_STEP). Only queries: no window message is sent (this runs inside Tk's
    event handling, also in Windows' sizing loop)."""

    VK_LBUTTON, VK_RBUTTON = 0x01, 0x02
    SM_SWAPBUTTON, SM_CYSIZEFRAME, SM_CXPADDEDBORDER = 23, 33, 92
    MONITOR_DEFAULTTONEAREST = 2
    GUI_INMOVESIZE = 0x00000002
    SPI_GETDRAGFULLWINDOWS = 0x0026

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                        ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND), ("hwndMoveSize", wt.HWND),
                        ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]

        self.ctypes, self.wt, self.MONITORINFO, self.GUITHREADINFO = ctypes, wt, MONITORINFO, GUITHREADINFO
        rect, point = ctypes.POINTER(wt.RECT), ctypes.POINTER(wt.POINT)
        self.f = _win_functions("user32", {
            "GetAsyncKeyState": (ctypes.c_short, ctypes.c_int),
            "GetSystemMetrics": (ctypes.c_int, ctypes.c_int),
            "GetCursorPos": (wt.BOOL, point),
            "GetWindowRect": (wt.BOOL, wt.HWND, rect),
            "GetClientRect": (wt.BOOL, wt.HWND, rect),
            "ClientToScreen": (wt.BOOL, wt.HWND, point),
            "MonitorFromWindow": (wt.HMONITOR, wt.HWND, wt.DWORD),
            "GetMonitorInfoW": (wt.BOOL, wt.HMONITOR, ctypes.POINTER(MONITORINFO)),
            "GetWindowThreadProcessId": (wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD)),
            "GetGUIThreadInfo": (wt.BOOL, wt.DWORD, ctypes.POINTER(GUITHREADINFO)),
            "SystemParametersInfoW": (wt.BOOL, wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT),
        })

    @staticmethod
    def frame(top: tk.Misc) -> int:
        """The toplevel's outer (wrapper) window."""
        return int(str(top.wm_frame()), 16)

    def button_down(self) -> bool:
        """The primary mouse button is down (GetAsyncKeyState reads the physical buttons)."""
        f = self.f
        vk = self.VK_RBUTTON if f["GetSystemMetrics"](self.SM_SWAPBUTTON) else self.VK_LBUTTON
        return bool(f["GetAsyncKeyState"](vk) & 0x8000)

    def in_move_size(self, top: tk.Misc) -> bool:
        """Windows' move or size loop runs in the toplevel's thread (its edge or title bar dragged, with the mouse,
        a pen or a finger; or 크기 조정 of its system menu, with the arrow keys)."""
        f, ctypes = self.f, self.ctypes
        thread = f["GetWindowThreadProcessId"](self.frame(top), None)
        if not thread:
            return False
        info = self.GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if not f["GetGUIThreadInfo"](thread, ctypes.byref(info)):
            return False
        return bool(info.flags & self.GUI_INMOVESIZE)

    def shows_contents(self) -> bool:
        """Windows shows a window's contents while it is dragged (창을 끄는 동안 내용 표시, the default; else it
        draws an outline and sizes the window once, when the drag ends)."""
        value = self.wt.BOOL()
        if not self.f["SystemParametersInfoW"](self.SPI_GETDRAGFULLWINDOWS, 0, self.ctypes.byref(value), 0):
            return True
        return bool(value.value)

    def dragging(self, top: tk.Misc) -> bool:
        """The window's size may be changing in steps of a drag: the primary button is down, or Windows' move or
        size loop runs (a pen or a finger need not show as the mouse button); not with an outline drag."""
        return self.shows_contents() and (self.button_down() or self.in_move_size(top))

    def _window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        r = self.wt.RECT()
        if not self.f["GetWindowRect"](hwnd, self.ctypes.byref(r)):
            raise OSError("GetWindowRect failed")
        return r.left, r.top, r.right, r.bottom

    def on_sizing_border(self, top: tk.Misc) -> bool:
        """The cursor is on the toplevel's frame (or just outside it) and not in its client area or title bar."""
        f, byref, wt = self.f, self.ctypes.byref, self.wt
        hwnd = self.frame(top)
        cursor = wt.POINT()
        client, origin = wt.RECT(), wt.POINT(0, 0)
        if not (f["GetCursorPos"](byref(cursor)) and f["GetClientRect"](hwnd, byref(client))
                and f["ClientToScreen"](hwnd, byref(origin))):
            return False
        left, top_, right, bottom = self._window_rect(hwnd)
        border = max(1, f["GetSystemMetrics"](self.SM_CYSIZEFRAME) + f["GetSystemMetrics"](self.SM_CXPADDEDBORDER))
        x, y = cursor.x, cursor.y
        slack = 2 * border  # (the cursor can be ahead of the edge that follows it)
        if not (left - slack <= x < right + slack and top_ - slack <= y < bottom + slack):
            return False
        inside_x = origin.x <= x < origin.x + client.right
        if inside_x and origin.y <= y < origin.y + client.bottom:
            return False  # the client area
        return not (inside_x and top_ + border <= y < origin.y)  # (not the title bar)

    def where(self, top: tk.Misc) -> tuple[tuple[int, int, int, int], bool]:
        """The toplevel's frame on the screen (left, top, right, bottom), and whether it is as wide or as tall as
        the work area of its monitor (maximized, or snapped)."""
        f, byref = self.f, self.ctypes.byref
        hwnd = self.frame(top)
        rect = left, top_, right, bottom = self._window_rect(hwnd)
        info = self.MONITORINFO()
        info.cbSize = self.ctypes.sizeof(info)
        monitor = f["MonitorFromWindow"](hwnd, self.MONITOR_DEFAULTTONEAREST)
        if not monitor or not f["GetMonitorInfoW"](monitor, byref(info)):
            return rect, False
        work = info.rcWork
        return rect, (left <= work.left and right >= work.right) or (top_ <= work.top and bottom >= work.bottom)


_POINTER: list = []  # [the _WinPointer, or None], made the first time it is needed


def _pointer():
    """Windows' mouse button and frame queries (_WinPointer), or None (not Windows, or not usable here)."""
    if not _POINTER:
        probe = None
        if sys.platform == "win32":
            try:
                probe = _WinPointer()
            except Exception as exc:
                log.debug("마우스 상태를 읽을 수 없습니다: %s", exc)
        _POINTER.append(probe)
    return _POINTER[0]


def _place(top: tk.Misc, probe) -> tuple:
    """(the toplevel's wm state, probe.where(top)), each None when it cannot be read: noted at each Configure
    (moves too) for the next size change."""
    try:
        state = top.wm_state()
    except tk.TclError:
        state = None
    try:
        where = probe.where(top)
    except Exception as exc:
        log.debug("창의 자리를 알 수 없습니다: %s", exc)
        where = None
    return state, where


def _resized_in_place(before, after) -> bool:
    """The frame kept at least two of its edges and was not as big as the work area before or after the change
    (``before``, ``after``: _place's): a drag of an edge keeps three edges, of a corner two."""
    if before is None or before[1] is None or after[1] is None:
        return False
    (old, spanned), (new, spans) = before[1], after[1]
    return not spanned and not spans and sum(a == b for a, b in zip(old, new)) >= 2


def _drag_step(top: tk.Misc, probe, before, after, by_last: bool, by_alone: bool) -> bool:
    """Is the size change a step of a drag of the window's edge (``resizing``)? ``by_last``: it came within
    RESIZE_SETTLE_MS of the last change; ``by_alone``: within RESIZE_FIRST_MS of a change on its own. Without the
    mouse (``probe`` None: not Windows, RESIZE_FIRST_STEP off) by these alone; with it see RESIZE_FIRST_STEP
    (``before``, ``after``: _place's for the last Configure and this one)."""
    if probe is None:
        return by_last or by_alone
    try:
        old, new = (before or (None, None))[0], after[0]
        if old is not None and new is not None and old != new:
            return False  # a maximize or restore
        if not probe.dragging(top):
            return by_last  # (Win+arrow keys, the maximize button: soon after a drag or another snap too)
        return by_last or by_alone or probe.on_sizing_border(top) or _resized_in_place(before, after)
    except Exception as exc:  # (as without it)
        log.debug("창 크기 바꾸기를 알 수 없습니다: %s", exc)
        return by_last or by_alone


def _last_error(ctypes) -> int:
    """Windows' last error of a ctypes call made with use_last_error (0 elsewhere)."""
    get = getattr(ctypes, "get_last_error", None)
    return int(get()) if get is not None else 0


class _WinClasses:
    """Tk's window classes for RESIZE_CLASS_REDRAW: found from a toplevel's windows with GetClassNameW (never by a
    name taken for granted), and CS_HREDRAW|CS_VREDRAW taken off their style (SetClassLongPtrW, GCL_STYLE)."""

    GCL_STYLE = -26
    CS_VREDRAW, CS_HREDRAW = 0x0001, 0x0002

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt

        self.ctypes, self.wt = ctypes, wt
        self.f = _win_functions("user32", {
            "GetClassNameW": (ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int),
            "GetParent": (wt.HWND, wt.HWND),
            "GetWindowThreadProcessId": (wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD)),
        })
        try:  # (64-bit; a 32-bit user32 has only the ...Long functions, the same there)
            styles = _win_functions("user32", {
                "GetClassLongPtrW": (ctypes.c_size_t, wt.HWND, ctypes.c_int),
                "SetClassLongPtrW": (ctypes.c_size_t, wt.HWND, ctypes.c_int, ctypes.c_ssize_t),
            })
            self.get_style, self.set_style = styles["GetClassLongPtrW"], styles["SetClassLongPtrW"]
        except AttributeError:
            styles = _win_functions("user32", {
                "GetClassLongW": (wt.DWORD, wt.HWND, ctypes.c_int),
                "SetClassLongW": (wt.DWORD, wt.HWND, ctypes.c_int, wt.LONG),
            })
            self.get_style, self.set_style = styles["GetClassLongW"], styles["SetClassLongW"]

    def class_name(self, hwnd: int) -> str:
        buf = self.ctypes.create_unicode_buffer(256)
        n = self.f["GetClassNameW"](hwnd, buf, len(buf))
        return buf.value[:n] if n > 0 else ""

    def ours(self, hwnd: int) -> bool:
        """The window is this process's."""
        pid = self.wt.DWORD()
        return bool(self.f["GetWindowThreadProcessId"](hwnd, self.ctypes.byref(pid))) and pid.value == os.getpid()

    def windows(self, top: tk.Misc) -> dict:
        """{"child": (class name, handle) of the toplevel's own window, "top": (class name, handle) of its outer
        window: the parent of its own window (GetParent) that `wm frame` names, there once the toplevel has been
        shown}. A kind is left out while its window is not there, is not this process's or has the other's class."""
        inner = int(top.winfo_id())
        name = self.class_name(inner)
        if not name or not self.ours(inner):
            return {}
        out = {"child": (name, inner)}
        outer = int(self.f["GetParent"](inner) or 0)
        try:
            frame = int(str(top.wm_frame()), 16)
        except (tk.TclError, ValueError):
            frame = 0
        if outer and outer == frame and outer != inner and self.ours(outer):
            outer_name = self.class_name(outer)
            if outer_name and outer_name != name:
                out["top"] = (outer_name, outer)
        return out

    def drop_redraw(self, hwnd: int) -> tuple[int, int]:
        """CS_HREDRAW|CS_VREDRAW off the class of the window ``hwnd``: (its style before, after)."""
        mask = self.CS_HREDRAW | self.CS_VREDRAW
        before = int(self.get_style(hwnd, self.GCL_STYLE))
        if before & mask:
            self.set_style(hwnd, self.GCL_STYLE, before & ~mask)
        after = int(self.get_style(hwnd, self.GCL_STYLE))
        if after & mask:
            raise OSError(f"the class style is still {after:#x} (error {_last_error(self.ctypes)})")
        return before, after


_CLASSES: list = []  # [the _WinClasses, or None], made the first time it is needed
# What class_redraw did, by kind ("top", "child"): {"class": its name, "before": style, "after": style}, or with
# "error" (not tried again). Read by the resize benchmark.
_CLASS_REDRAW: dict = {}
_CLASS_REDRAW_KINDS = {"top": ("top",), "child": ("child",), "both": ("top", "child")}


def _win_classes():
    """Tk's window classes on Windows (_WinClasses), or None (not Windows, or not usable here)."""
    if not _CLASSES:
        api = None
        if sys.platform == "win32":
            try:
                api = _WinClasses()
            except Exception as exc:
                log.debug("창 클래스를 바꿀 수 없습니다: %s", exc)
        _CLASSES.append(api)
    return _CLASSES[0]


def class_redraw(top: tk.Misc) -> None:
    """Take CS_HREDRAW|CS_VREDRAW off Tk's window classes as RESIZE_CLASS_REDRAW says (Windows; nothing elsewhere),
    each class once per process: the class of the toplevel's own window at once, its outer window's once the
    toplevel has been shown (called again at each <Configure> until then). Never raises: a class that cannot be
    changed stays as it is."""
    todo = [kind for kind in _CLASS_REDRAW_KINDS.get(RESIZE_CLASS_REDRAW, ()) if kind not in _CLASS_REDRAW]
    if not todo:
        return
    api = _win_classes()
    if api is None:
        return
    try:
        found = api.windows(top)
    except Exception as exc:
        for kind in todo:
            _CLASS_REDRAW[kind] = {"error": f"{type(exc).__name__}: {exc}"}
        log.debug("창 클래스를 찾지 못했습니다: %s", exc)
        return
    for kind in todo:
        if kind not in found:
            continue  # (the outer window: not shown yet)
        name, hwnd = found[kind]
        try:
            before, after = api.drop_redraw(hwnd)
        except Exception as exc:
            _CLASS_REDRAW[kind] = {"class": name, "error": f"{type(exc).__name__}: {exc}"}
            log.debug("창 클래스 %s의 스타일을 바꾸지 못했습니다: %s", name, exc)
            continue
        _CLASS_REDRAW[kind] = {"class": name, "before": before, "after": after}
        log.debug("창 클래스 %s: CS_HREDRAW|CS_VREDRAW 끔 (%#x -> %#x)", name, before, after)


def _note_resize(event):
    top = event.widget
    if isinstance(top, str):  # (a toplevel tkinter does not know)
        return
    if RESIZE_CLASS_REDRAW != "keep":
        class_redraw(top)  # (the outer window's class once the toplevel has been shown; then nothing)
    probe = _pointer() if RESIZE_FIRST_STEP else None
    before = getattr(top, "_mhwmm_place", None)
    top._mhwmm_place = after = _place(top, probe) if probe is not None else None  # (Windows: also when moved)
    size = (event.width, event.height)
    if getattr(top, "_mhwmm_size", None) != size:  # (moving the window is not resizing it)
        now = time.perf_counter()  # (time.monotonic() is in 15.6 ms steps on Windows before Python 3.13)
        last, alone = getattr(top, "_mhwmm_changed", None), getattr(top, "_mhwmm_alone", None)
        # a drag from its second step on (on Windows from its first, when the mouse tells): a change on its own
        # (maximize, restore, snapping to a screen edge, a drag with "show window contents while dragging" off)
        # is followed at once
        drag = _drag_step(top, probe, before, after,
                          last is not None and now - last < RESIZE_SETTLE_MS / 1000,
                          alone is not None and now - alone < RESIZE_FIRST_MS / 1000)
        top._mhwmm_size, top._mhwmm_changed = size, now
        top._mhwmm_resized, top._mhwmm_alone = (now, alone) if drag else (None, now)
        for on_size in getattr(top, "_mhwmm_on_size", ()):
            on_size(event)


def watch_resizing(top: tk.Misc, on_size=None):
    """Note when the toplevel ``top`` changes size (for ``resizing``) and then call ``on_size(event)``: before
    anything in the window is laid out for the new size. One binding on the toplevel only. Also takes the redraw
    styles off Tk's window classes as RESIZE_CLASS_REDRAW says (class_redraw)."""
    class_redraw(top)
    if on_size is not None:
        top._mhwmm_on_size = getattr(top, "_mhwmm_on_size", ()) + (on_size,)
    tags = top.bindtags()
    if _RESIZE_TAG in tags:
        return
    if not top.bind_class(_RESIZE_TAG, "<Configure>"):
        top.bind_class(_RESIZE_TAG, "<Configure>", _note_resize)
    top.bindtags((_RESIZE_TAG,) + tuple(tags))


def resizing(widget: tk.Misc) -> bool:
    """True while the window of ``widget`` is being resized by dragging its edge: its size changed less than
    RESIZE_SETTLE_MS ago, and less than RESIZE_SETTLE_MS after the change before (or soon after a change on its
    own, see RESIZE_FIRST_MS; or on Windows from the first step, see RESIZE_FIRST_STEP)."""
    top = widget.winfo_toplevel()
    at = getattr(top, "_mhwmm_resized", None)
    return at is not None and time.perf_counter() - at < RESIZE_SETTLE_MS / 1000


class FreezeContent:
    """All of the toplevel ``top``'s content in one frame, ``frame`` (a child of ``top``), placed to fill the
    window. While the window's edge is dragged (``resizing``) the frame keeps the size it has, so Tk lays out
    nothing in it on the steps of the drag (only the window's own background shows where it grew; where it got
    smaller the content is cut off). RESIZE_SETTLE_MS after the last change the frame fills the window again and
    is laid out once. Other changes (maximize, restore, snapping to a screen edge, the drag's first step unless
    the mouse tells, see RESIZE_FIRST_STEP, and all changes within RESIZE_FIRST_MS of the window's first size:
    shown zoomed or moved to its monitor) are followed at once.

    On Windows every widget is a window of its own and Tk laid out and drew all of them again for each step of
    the drag before the next mouse move, which made resizing stutter. Frozen, nothing in the content is laid out;
    with Tk's window classes without CS_HREDRAW|CS_VREDRAW (RESIZE_CLASS_REDRAW) nothing in it is drawn again
    either, only the area the window gained.

    Placing the frame does not give the window a requested size: the window's size is set with geometry()."""

    def __init__(self, top: tk.Misc, frame: tk.Misc):
        self.top, self.frame = top, frame
        self.frozen = False
        self._after = None
        self._shown: float | None = None  # when the window got its first size
        # (what, ms): "restore" (the content laid out and drawn for the size a drag stopped at) - for the resize
        # benchmark
        self.timings: collections.deque = collections.deque(maxlen=64)
        frame.place(x=0, y=0, relwidth=1, relheight=1)
        watch_resizing(top, self._on_size)
        frame.bind("<Destroy>", self._cancel, add="+")

    def _on_size(self, _event):
        """The window got a new size (before the frame is laid out for it)."""
        now = time.perf_counter()
        if self._shown is None:
            self._shown = now
        if resize_mode() == "off" or not resizing(self.top) or now - self._shown < RESIZE_FIRST_MS / 1000:
            self.thaw()  # (a change on its own: followed at once)
            return
        try:
            if not self.frozen:
                width, height = self.frame.winfo_width(), self.frame.winfo_height()
                if width <= 1 or height <= 1:
                    return  # not laid out yet
                self.frame.place_configure(relwidth=0, relheight=0, width=width, height=height)
                self.frozen = True
            self._cancel()
            self._after = self.frame.after(RESIZE_SETTLE_MS, self._settled)
        except tk.TclError:  # closed meanwhile
            pass

    def _settled(self):
        """The size has not changed for RESIZE_SETTLE_MS: the content follows. The mouse button may still be down
        (a pause in the drag), so a change soon after this goes on with the drag and is frozen, as a step soon
        after the drag's first step is (else the step that resumes the drag would be laid out at once: the whole
        window laid out twice for one pause, the second time just as the mouse moves again)."""
        self._after = None
        self.top._mhwmm_alone = time.perf_counter()  # (see _note_resize)
        self.thaw(settled=True)

    def thaw(self, settled: bool = False) -> bool:
        """The frame fills the window again (laid out when Tk is idle; at once when the drag ``settled``); False
        when it was not frozen."""
        self._cancel()
        if not self.frozen:
            return False
        self.frozen = False
        started = time.perf_counter()
        try:
            self.frame.place(x=0, y=0, width="", height="", relwidth=1, relheight=1)
            if settled:
                self.frame.update_idletasks()  # (laid out and drawn now, and timed for the benchmark)
                self.timings.append(("restore", (time.perf_counter() - started) * 1000))
        except tk.TclError:  # closed meanwhile
            pass
        return True

    def _cancel(self, _event=None):
        if self._after is not None:
            try:
                self.frame.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None


def follow_width(canvas: tk.Canvas, item):
    """Keep the canvas window ``item`` (a scrolled frame of cards) as wide as ``canvas``: only when the width
    changed (a new width lays all the cards out again)."""
    given = [None]

    def on_configure(event):
        if event.width != given[0]:
            given[0] = event.width
            canvas.itemconfigure(item, width=event.width)

    canvas.bind("<Configure>", on_configure, add="+")


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__(logging.INFO)
        self.q = q
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            self.q.put(("log", record.levelno, self.format(record)))
        except Exception:
            pass


class Tooltip:
    def __init__(self, theme: Theme, widget, text):
        self.theme = theme
        self.widget = widget
        self.text = text
        self.tip = None
        self.after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self.after_id = self.widget.after(450, self._show)

    def _cancel(self):
        if self.after_id:
            self.widget.after_cancel(self.after_id)
            self.after_id = None

    def _show(self):
        self.after_id = None
        text = self.text() if callable(self.text) else self.text
        if not text or not self.widget.winfo_exists():
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        label = tk.Label(tip, text=text, justify="left", padx=self.theme.px(10), pady=self.theme.px(6),
                         font=self.theme.fonts["small"])
        self.theme.mark(label, "tooltip")
        label.pack()
        tip.update_idletasks()
        x = self.widget.winfo_rootx()
        x = max(0, min(x, self.widget.winfo_screenwidth() - tip.winfo_reqwidth() - 4))
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + self.theme.px(6)
        tip.wm_geometry(f"+{x}+{y}")
        self.tip = tip

    def _hide(self, _event=None):
        self._cancel()
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class RowTooltip(Tooltip):
    """Tooltip for the Treeview row under the mouse; ``text(row id)`` gives its text ("" for none)."""

    def __init__(self, theme: Theme, tree: ttk.Treeview, text):
        super().__init__(theme, tree, text)
        self.row = ""
        tree.bind("<Motion>", self._on_motion, add="+")

    def _on_motion(self, event):
        row = self.widget.identify_row(event.y)
        if row != self.row:
            self.row = row
            self._hide()
            if row:
                self._schedule()

    def _hide(self, _event=None):
        super()._hide()
        if _event is not None and _event.type == tk.EventType.Leave:
            self.row = ""

    def _show(self):
        self.after_id = None
        text = self.text(self.row) if self.row and self.widget.exists(self.row) else ""
        if not text:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        label = tk.Label(tip, text=text, justify="left", padx=self.theme.px(10), pady=self.theme.px(6),
                         font=self.theme.fonts["small"])
        self.theme.mark(label, "tooltip")
        label.pack()
        tip.update_idletasks()
        bbox = self.widget.bbox(self.row) or (0, 0, 0, self.widget.winfo_height())
        x = self.widget.winfo_pointerx() + self.theme.px(14)
        x = max(0, min(x, self.widget.winfo_screenwidth() - tip.winfo_reqwidth() - 4))
        y = self.widget.winfo_rooty() + bbox[1] + bbox[3] + self.theme.px(2)
        tip.wm_geometry(f"+{x}+{y}")
        self.tip = tip


class Dialog(tk.Toplevel):
    """Small modal dialog base."""

    # a window meant to be made bigger (its content does not change size once it is shown): the body keeps its
    # size while the window's edge is dragged (FreezeContent)
    FREEZE = False

    def __init__(self, parent, theme: Theme, title: str):
        super().__init__(parent)
        self.withdraw()
        self.theme = theme
        self.title(title)
        self.transient(parent)
        theme.mark(self, "window")
        self.result = None
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())
        self.body = ttk.Frame(self, padding=theme.px(22))
        self.body.pack(fill="both", expand=True)

    def buttons(self, ok_text: str, ok_command, cancel_text: str = "취소"):
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(self.theme.px(18), 0))
        ttk.Button(row, text=cancel_text, command=self.cancel).pack(side="right")
        ttk.Button(row, text=ok_text, style="Accent.TButton", command=ok_command).pack(
            side="right", padx=(0, self.theme.px(8)))
        self.bind("<Return>", lambda e: ok_command())
        return row

    def show(self):
        self.update_idletasks()
        x, y = self._position()
        if self.FREEZE:
            self._freeze_body()
        self.geometry(f"+{x}+{y}")
        set_dark_titlebar(self, self.theme.name == "dark")
        self.deiconify()
        self.grab_set()
        self.focus_set()
        self.wait_window()
        return self.result

    def _freeze_body(self):
        """The body placed instead of packed (FreezeContent) and the window given the size the packed body asked
        for (a placed body does not ask the window for a size)."""
        min_w, min_h = self.minsize()
        width, height = max(self.winfo_reqwidth(), min_w), max(self.winfo_reqheight(), min_h)
        self.body.pack_forget()
        self.freeze = FreezeContent(self, self.body)
        self.geometry(f"{width}x{height}")

    def _position(self) -> tuple[int, int]:
        """Where show() puts the dialog: over its parent, centred and a third of the way down."""
        parent = self.master
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_reqwidth()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_reqheight()) // 3)
        return x, y

    def cancel(self):
        self.result = None
        self.destroy()

    def destroy(self):
        super().destroy()
        self._release_variables(*vars(self))

    def _release_variables(self, *names: str):
        """Drop the dialog's Tk variables (alone, or a list or dict of them) once it is destroyed - every dialog's,
        on the GUI thread: a dialog and its widgets refer to each other, so left to the garbage collector they
        could be freed on another thread, which Tk does not allow (Python 3.10 may even crash)."""
        for name in names:
            value = getattr(self, name, None)
            items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else None
            if isinstance(value, tk.Variable) or (items and all(isinstance(v, tk.Variable) for v in items)):
                setattr(self, name, None)


class ImageDialog(Dialog):
    """A picture shown large; a click or any key closes it."""

    def __init__(self, parent, theme: Theme, title: str, path: Path):
        super().__init__(parent, theme, title)
        width = min(theme.px(1280), parent.winfo_screenwidth() * 4 // 5)
        height = min(theme.px(800), parent.winfo_screenheight() * 3 // 4)
        self.image = load_image(path, width, height)
        picture = ttk.Label(self.body, image=self.image or "", text="" if self.image else "그림을 열 수 없습니다.")
        picture.pack()
        ttk.Label(self.body, text=title, style="Muted.TLabel").pack(anchor="w", pady=(theme.px(10), 0))
        for widget in (self, picture):
            widget.bind("<Button-1>", lambda e: self.cancel())
        self.bind("<Key>", lambda e: self.cancel())


class TextDialog(Dialog):
    def __init__(self, parent, theme: Theme, title: str, prompt: str, initial: str = ""):
        super().__init__(parent, theme, title)
        ttk.Label(self.body, text=prompt, style="DialogTitle.TLabel").pack(anchor="w", pady=(0, theme.px(10)))
        self.var = tk.StringVar(value=initial)
        self.entry = ttk.Entry(self.body, textvariable=self.var, width=46)
        self.entry.pack(fill="x")
        self.entry.select_range(0, "end")
        self.entry.focus_set()
        self.buttons("확인", self.ok)
        self.after(50, self.entry.focus_set)

    def ok(self):
        self.result = self.var.get()
        self.destroy()


class UpdateDialog(Dialog):
    """Shows what a new release changes; result is "install", "page" or None."""

    def __init__(self, parent, theme: Theme, release: Release, can_install: bool):
        super().__init__(parent, theme, "업데이트")
        px = theme.px
        history = release.history or [release]
        ttk.Label(self.body, text=f"새 버전 v{release.version}이 나왔습니다", style="DialogTitle.TLabel").pack(anchor="w")
        summary = f"지금 쓰는 버전은 v{__version__}입니다."
        if len(history) > 1:
            summary += f" 그동안 나온 {len(history)}개 버전의 변경 내역입니다."
        ttk.Label(self.body, text=summary, style="Muted.TLabel").pack(anchor="w", pady=(px(4), px(12)))
        notes = theme.mark(tk.Text(self.body, height=14, width=70, wrap="word", font=theme.fonts["small"],
                                   padx=px(14), pady=px(10), cursor="arrow"), "text")
        notes.tag_configure("version", font=theme.fonts["bold"], spacing1=px(10), spacing3=px(4))
        notes.tag_configure("date", foreground=theme.p["muted"])
        for item in history:
            notes.insert("end", f"v{item.version}", "version")
            if item.published:
                notes.insert("end", f"   {item.published}", ("version", "date"))
            notes.insert("end", "\n" + (item.plain_changes or "변경 내역이 없습니다.") + "\n")
        notes.configure(state="disabled")
        notes.pack(fill="both", expand=True)
        if not can_install:
            ttk.Label(self.body, text="Python으로 실행 중이라 자동 업데이트 대신 다운로드 페이지를 엽니다.",
                      style="Muted.TLabel", wraplength=px(480), justify="left").pack(anchor="w", pady=(px(10), 0))
        row = self.buttons("지금 업데이트" if can_install else "다운로드 페이지 열기",
                           lambda: self.finish("install" if can_install else "page"), cancel_text="나중에")
        if can_install:
            ttk.Button(row, text="릴리즈 페이지", command=lambda: self.finish("page")).pack(side="left")

    def finish(self, result: str):
        self.result = result
        self.destroy()


class RefwDialog(Dialog):
    """REFramework status; result is "install", "page" or None."""

    def __init__(self, parent, theme: Theme, status: refw.Status, managed: bool):
        super().__init__(parent, theme, "REFramework")
        px = theme.px
        latest = status.latest
        ttk.Label(self.body, text="REFramework", style="DialogTitle.TLabel").pack(anchor="w")
        ttk.Label(self.body, text="루스 파일 · PAK 모드와 Lua 스크립트를 쓰려면 필요한 프로그램입니다.",
                  style="Muted.TLabel").pack(anchor="w", pady=(px(4), px(14)))
        grid = ttk.Frame(self.body)
        grid.pack(fill="x")
        rows = (("설치된 버전", refw.describe_installed(status)),
                ("최신 버전", f"{latest.label}" + (f"   ({latest.published})" if latest.published else "")))
        for i, (key, value) in enumerate(rows):
            ttk.Label(grid, text=key, style="Muted.TLabel").grid(row=i, column=0, sticky="w", padx=(0, px(16)), pady=px(3))
            ttk.Label(grid, text=value).grid(row=i, column=1, sticky="w", pady=px(3))
        notes = ["VR을 쓰지 않을 때의 REFramework 안내에 따라 dinput8.dll만 설치합니다."]
        if status.installed and not managed:
            notes.append("직접 넣어 둔 dinput8.dll은 백업해 두었다가, 이 REFramework를 끄거나 지우면 되돌립니다.")
        for note in notes:
            ttk.Label(self.body, text="• " + note, style="Muted.TLabel", font=theme.fonts["small"],
                      wraplength=px(460), justify="left").pack(anchor="w", pady=(px(8), 0))
        if not status.installed:
            ok_text = "설치"
        elif status.up_to_date:
            ok_text = "다시 설치"
        else:
            ok_text = "업데이트"
        row = self.buttons(ok_text, lambda: self.finish("install"), cancel_text="닫기")
        ttk.Button(row, text="릴리즈 페이지", command=lambda: self.finish("page")).pack(side="left")

    def finish(self, result: str):
        self.result = result
        self.destroy()


class HelpDialog(Dialog):
    """Usage guide: topics on the left, the whole guide as one scrolling page on the right."""

    INLINE = re.compile(r"\*\*(.+?)\*\*|\[\[(.+?)\]\]")
    FREEZE = True  # (the whole guide is wrapped again for each new width)

    def __init__(self, parent, theme: Theme, topic: str | None = None):
        super().__init__(parent, theme, "사용법")
        px, f = theme.px, theme.fonts
        self.minsize(px(760), px(480))
        self.body.configure(padding=0)
        nav = ttk.Frame(self.body, padding=(px(16), px(20), px(8), px(18)))
        nav.pack(side="left", fill="y")
        ttk.Label(nav, text="사용법", style="DialogTitle.TLabel").pack(anchor="w", padx=px(6))
        ttk.Label(nav, text=f"버전 {__version__}", style="Muted.TLabel", font=f["small"]).pack(
            anchor="w", padx=px(6), pady=(px(2), px(14)))
        self.nav_buttons = [ttk.Button(nav, text=s.title, style="Nav.TButton", command=lambda i=i: self.go(i))
                            for i, s in enumerate(HELP_SECTIONS)]
        for button in self.nav_buttons:
            button.pack(fill="x", pady=(0, px(2)))
        ttk.Button(nav, text="닫기", command=self.cancel).pack(side="bottom", fill="x")

        card = theme.mark(tk.Frame(self.body), "card")
        card.pack(side="left", fill="both", expand=True, padx=(px(8), px(18)), pady=px(18))
        self.text = theme.mark(tk.Text(card, wrap="word", width=70, height=30, font=f["base"], padx=px(30),
                                       pady=px(24), cursor="arrow", spacing2=px(3)), "text")
        scrollbar = ttk.Scrollbar(card, orient="vertical", command=self.text.yview)
        show_scrollbar = autohide(scrollbar, side="right", fill="y")
        self.text.configure(yscrollcommand=lambda first, last: (show_scrollbar(first, last), self._follow_scroll()))
        self.text.pack(side="left", fill="both", expand=True)
        self._active: int | None = None
        self._jumped = 0.0
        self._configure_tags()
        self._render()
        self.text.configure(state="disabled")
        keys = [s.key for s in HELP_SECTIONS]
        self.after_idle(lambda: self.go(keys.index(topic) if topic in keys else 0))

    def _configure_tags(self):
        p, f, px = self.theme.p, self.theme.fonts, self.theme.px
        t = self.text
        t.tag_configure("h1", font=f["title"], spacing1=px(30), spacing3=px(12))
        t.tag_configure("first", spacing1=0)
        t.tag_configure("lead", foreground=p["muted"], spacing3=px(10))
        t.tag_configure("p", spacing1=px(10), spacing3=px(8))
        t.tag_configure("step", lmargin1=0, lmargin2=px(34), tabs=(px(34),), spacing1=px(6), spacing3=px(6))
        t.tag_configure("bullet", lmargin1=px(8), lmargin2=px(26), tabs=(px(26),), spacing1=px(4), spacing3=px(4))
        t.tag_configure("dot", foreground=p["accent"], font=f["bold"])
        t.tag_configure("keyrow", tabs=(px(160),), spacing1=px(4), spacing3=px(4))
        t.tag_configure("strong", font=f["bold"])
        for tag, fg, bg in (("tip", "info", "info_bg"), ("warn", "warn", "warn_bg")):
            t.tag_configure(tag, background=p[bg], lmargin1=px(14), lmargin2=px(14), rmargin=px(14),
                            spacing1=px(10), spacing3=px(10))
            try:
                t.tag_configure(tag, lmargincolor=p[bg], rmargincolor=p[bg])
            except tk.TclError:  # Tk < 8.6.6
                pass
            t.tag_configure(f"{tag}_label", foreground=p[fg], font=f["bold"])
        t.tag_configure("gap", font=f["small"], spacing1=0, spacing3=0)

    def _chip(self, text: str, tags: tuple, accent: bool = False):
        """A key cap or step number (a label: tag backgrounds would also fill the line spacing)."""
        p, px = self.theme.p, self.theme.px
        label = tk.Label(self.text, text=text, font=self.theme.fonts["small_bold"], padx=px(6), pady=0, bd=0,
                         bg=p["accent"] if accent else p["pressed"], fg=p["on_accent"] if accent else p["text"])
        index = self.text.index("end-1c")
        self.text.window_create(index, window=label, align="center", padx=px(2))
        for tag in tags:
            self.text.tag_add(tag, index)

    def _inline(self, text: str, tags: tuple):
        pos = 0
        for m in self.INLINE.finditer(text):
            self.text.insert("end", text[pos:m.start()], tags)
            if m.group(1):
                self.text.insert("end", m.group(1), tags + ("strong",))
            else:
                self._chip(m.group(2), tags)
            pos = m.end()
        self.text.insert("end", text[pos:] + "\n", tags)

    def _render(self):
        t = self.text
        for i, section in enumerate(HELP_SECTIONS):
            t.mark_set(f"section{i}", "end-1c")
            t.mark_gravity(f"section{i}", "left")
            t.insert("end", section.title + "\n", ("h1", "first") if i == 0 else ("h1",))
            for kind, content in section.blocks:
                if kind in ("lead", "p"):
                    self._inline(content, (kind,))
                elif kind == "steps":
                    for n, line in enumerate(content, 1):
                        self._chip(str(n), ("step",), accent=True)
                        t.insert("end", "\t", ("step",))
                        self._inline(line, ("step",))
                elif kind == "bullets":
                    for line in content:
                        t.insert("end", "•", ("bullet", "dot"))
                        t.insert("end", "\t", ("bullet",))
                        self._inline(line, ("bullet",))
                elif kind == "keys":
                    for keys, what in content:
                        for n, key in enumerate(keys):
                            if n:
                                t.insert("end", " ", ("keyrow",))
                            self._chip(key, ("keyrow",))
                        t.insert("end", f"\t{what}\n", ("keyrow",))
                elif kind in ("tip", "warn"):
                    t.insert("end", "\n", ("gap",))
                    t.insert("end", ("팁" if kind == "tip" else "주의") + "   ", (kind, f"{kind}_label"))
                    self._inline(content, (kind,))
                    t.insert("end", "\n", ("gap",))
        t.delete("end-2c", "end-1c")  # no empty line at the very end

    def go(self, index: int):
        """Scroll the page to a topic."""
        self.text.yview(f"section{index}")
        self._jumped = time.monotonic()
        self._set_active(index)

    def _follow_scroll(self):
        """Highlight the topic being read while scrolling."""
        if time.monotonic() - self._jumped < 0.3:  # the scroll from clicking a topic
            return
        first, last = self.text.yview()
        if last >= 1.0 and first > 0.0:
            self._set_active(len(HELP_SECTIONS) - 1)
            return
        line = self.text.index(f"@0,{self.text.winfo_height() // 4}")
        self._set_active(max((i for i in range(len(HELP_SECTIONS))
                              if self.text.compare(f"section{i}", "<=", line)), default=0))

    def _set_active(self, index: int):
        if index == self._active:
            return
        self._active = index
        for i, button in enumerate(self.nav_buttons):
            button.configure(style="NavActive.TButton" if i == index else "Nav.TButton")


class ConflictsDialog(Dialog):
    """Every two enabled mods that change the same files; result is a mod id to show in the list."""

    FREEZE = True

    def __init__(self, parent, theme: Theme, manager: ModManager):
        super().__init__(parent, theme, "충돌 한눈에 보기")
        px = theme.px
        self.minsize(px(640), px(480))
        conflicts = manager.conflicts()
        self.pairs = conflicts.pairs()
        mods = {m.id: m for m in manager.mods}
        order = {m.id: i + 1 for i, m in enumerate(manager.mods)}
        label = lambda mod_id: f"{order[mod_id]}. {mods[mod_id].name}"  # noqa: E731

        ttk.Label(self.body, text="충돌 한눈에 보기", style="DialogTitle.TLabel").pack(anchor="w")
        if self.pairs:
            involved = {mod_id for w, l, _ in self.pairs for mod_id in (w, l)}
            files = {path for _, _, paths in self.pairs for path in paths}
            summary = (f"켜진 모드 {len(involved)}개가 파일 {len(files)}개를 함께 바꿉니다. 같은 파일은 목록에서 더 "
                       "아래에 있는 모드(적용되는 모드)의 것이 게임에 들어갑니다.")
        else:
            summary = "같은 파일을 바꾸는 켜진 모드가 없습니다."
        ttk.Label(self.body, text=summary, style="Muted.TLabel", wraplength=px(600),
                  justify="left").pack(anchor="w", pady=(px(4), px(10)))
        hidden = manager.hidden_mods(conflicts)
        if hidden:
            callout = ttk.Frame(self.body, style="Callout.TFrame", padding=(px(14), px(10)))
            callout.pack(fill="x", pady=(0, px(10)))
            ttk.Label(callout, text=f"적용되지 않는 모드 {len(hidden)}개", style="CalloutTitle.TLabel").pack(anchor="w")
            ttk.Label(callout, text=", ".join(label(m.id) for m in hidden) + " — 파일이 모두 다른 모드에 밀려 게임에 "
                      "아무것도 들어가지 않습니다. 순서를 바꾸거나 끄세요.", style="Callout.TLabel",
                      wraplength=px(580), justify="left").pack(anchor="w", pady=(px(2), 0))

        card = theme.mark(tk.Frame(self.body), "card")
        card.pack(fill="both", expand=True)
        self.pairs_tree = ttk.Treeview(card, columns=("winner", "loser", "count"), show="headings", height=8,
                                       selectmode="browse")
        for col, text, width, anchor in (("winner", "적용되는 모드", 240, "w"), ("loser", "밀리는 모드", 240, "w"),
                                         ("count", "겹치는 파일", 90, "e")):
            self.pairs_tree.heading(col, text=text, anchor=anchor)
            self.pairs_tree.column(col, width=px(width), anchor=anchor, stretch=col != "count")
        self.pairs_tree.tag_configure("odd", background=theme.p["row_alt"])
        for i, (winner, loser, paths) in enumerate(self.pairs):
            self.pairs_tree.insert("", "end", iid=str(i), values=(label(winner), label(loser), f"{len(paths)}개"),
                                   tags=("odd",) if i % 2 else ())
        self.pairs_tree.pack(fill="both", expand=True)
        self.pairs_tree.bind("<<TreeviewSelect>>", lambda e: self._show_files())
        self.pairs_tree.bind("<Double-1>", lambda e: self.show_in_list())

        ttk.Label(self.body, text="선택한 두 모드가 함께 바꾸는 파일", style="DialogSection.TLabel").pack(
            anchor="w", pady=(px(12), px(4)))
        files_card = theme.mark(tk.Frame(self.body), "card")
        files_card.pack(fill="both", expand=True)
        self.files_tree = ttk.Treeview(files_card, columns=("path",), show="headings", height=7, selectmode="none")
        self.files_tree.heading("path", text="게임 폴더 기준 경로", anchor="w")
        self.files_tree.column("path", width=px(560), anchor="w")
        fsb = ttk.Scrollbar(files_card, orient="vertical", command=self.files_tree.yview)
        self.files_tree.configure(yscrollcommand=autohide(fsb, side="right", fill="y"))
        self.files_tree.pack(side="left", fill="both", expand=True)

        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(px(16), 0))
        ttk.Button(row, text="닫기", command=self.cancel).pack(side="right")
        show = ttk.Button(row, text="목록에서 밀리는 모드 보기", style="Accent.TButton", command=self.show_in_list)
        show.pack(side="right", padx=(0, px(8)))
        if self.pairs:
            self.pairs_tree.selection_set("0")
            self._show_files()
        else:
            show.state(["disabled"])

    def _show_files(self):
        self.files_tree.delete(*self.files_tree.get_children())
        sel = self.pairs_tree.selection()
        if sel:
            for path in self.pairs[int(sel[0])][2][:MAX_FILE_ROWS]:
                self.files_tree.insert("", "end", values=(path,))

    def show_in_list(self):
        sel = self.pairs_tree.selection()
        if sel:
            self.result = self.pairs[int(sel[0])][1]
            self.destroy()


class RequirementsDialog(Dialog):
    """Mods that were just installed but need other mods that are missing or switched off."""

    def __init__(self, parent, theme: Theme, items: list[tuple[Mod, list[dict]]]):
        super().__init__(parent, theme, "필요한 모드")
        px = theme.px
        ttk.Label(self.body, text="필요한 모드가 없습니다", style="DialogTitle.TLabel").pack(anchor="w")
        ttk.Label(self.body, text="아래 모드를 설치하거나 켜야 제대로 작동합니다. Nexus에서 받은 파일은 이 창에 "
                                  "끌어 놓으면 설치됩니다.", style="Muted.TLabel", wraplength=px(480),
                  justify="left").pack(anchor="w", pady=(px(4), 0))
        for mod, missing in items:
            ttk.Label(self.body, text=mod.name, style="DialogSection.TLabel").pack(anchor="w", pady=(px(14), px(4)))
            for req in missing:
                row = ttk.Frame(self.body)
                row.pack(fill="x", pady=px(2))
                ttk.Label(row, text="• " + req["name"]).pack(side="left")
                ttk.Label(row, text="   " + requirement_note(req), style="Muted.TLabel",
                          font=theme.fonts["small"]).pack(side="left")
                if req["url"]:
                    ttk.Button(row, text="Nexus에서 찾기" if req["kind"] == "lua" else "열기", style="Link.TButton",
                               command=lambda url=req["url"]: webbrowser.open(url)).pack(side="right")
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(px(18), 0))
        ttk.Button(row, text="확인", style="Accent.TButton", command=self.cancel).pack(side="right")
        self.bind("<Return>", lambda e: self.cancel())


def material_summary(results: list) -> str:
    """What a repair changes in one mod, in a line."""
    fixable = [r for r in results if r.state == "fixable"]
    names = sorted({n for r in fixable for n in r.fixed}, key=str.casefold)
    parts = [f"파일 {len(fixable)}개"]
    if names:
        parts.append("재질 " + ", ".join(names[:5]) + (f" 외 {len(names) - 5}개" if len(names) > 5 else ""))
    items = [i for r in fixable for i in r.rsz if not i.get("reason")]
    if items:
        kinds = sorted({i["kind"] for i in items})
        methods = sorted({i["method"] for i in items})
        parts.append(f"{'·'.join('.' + k for k in kinds)} {len(items)}개 ({', '.join(methods)})")
    renamed = sum(1 for r in fixable if r.new_rel and r.new_rel != r.rel)
    if renamed:
        parts.append(f"버전 번호를 바꿀 파일 {renamed}개")
    return " · ".join(parts)


class MaterialsDialog(Dialog):
    """Mods whose materials no longer fit the game; result is the ids of those to repair."""

    def __init__(self, parent, theme: Theme, manager: ModManager, mods: list[Mod]):
        super().__init__(parent, theme, "재질 복구")
        px = theme.px
        self.mods = mods
        ttk.Label(self.body, text="현재 게임과 맞지 않는 모드", style="DialogTitle.TLabel").pack(anchor="w")
        ttk.Label(self.body, text="게임 업데이트로 재질(.mdf2)이나 .user·.pfb 파일의 구조가 바뀌어, 아래 모드는 텍스처가 "
                                  "깨지거나 게임이 검은 화면에서 멈출 수 있습니다. 복구하면 현재 게임의 구조에 모드의 내용을 "
                                  "옮겨 담은 파일을 대신 적용합니다. 모드의 원래 파일은 그대로 두므로 언제든 되돌릴 수 있고, "
                                  "나중에 고치려면 모드를 오른쪽 클릭해 '재질 복구'를 고르세요.",
                  style="Muted.TLabel", wraplength=px(580), justify="left").pack(anchor="w", pady=(px(4), px(10)))
        card = theme.mark(tk.Frame(self.body), "card")
        card.pack(fill="both", expand=True)
        self.on, self.off = theme.image("check_on", 20), theme.image("check_off", 20)
        self.tree = ttk.Treeview(card, columns=("detail",), show="tree headings", selectmode="none",
                                 height=min(8, max(3, len(mods))))
        self.tree.heading("#0", text="모드", anchor="w")
        self.tree.heading("detail", text="복구할 내용", anchor="w")
        self.tree.column("#0", width=px(230), anchor="w")
        self.tree.column("detail", width=px(350), anchor="w")
        self.checked = {m.id for m in mods}
        for mod in mods:
            self.tree.insert("", "end", iid=mod.id, text=" " + mod.name, image=self.on,
                             values=(material_summary(manager.material_results(mod.id)),))
        self.tree.bind("<Button-1>", self._toggle)
        sb = ttk.Scrollbar(card, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=autohide(sb, side="right", fill="y"))
        self.tree.pack(side="left", fill="both", expand=True)
        ttk.Label(self.body, text=matfix.CREDIT, style="Muted.TLabel", font=theme.fonts["small"],
                  wraplength=px(580), justify="left").pack(anchor="w", pady=(px(10), 0))
        self.buttons("선택한 모드 복구", self.ok, "나중에")

    def _toggle(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.checked ^= {item}
            self.tree.item(item, image=self.on if item in self.checked else self.off)

    def ok(self):
        self.result = [m.id for m in self.mods if m.id in self.checked]
        self.destroy()


def requirement_note(req: dict) -> str:
    """Short status of a requirement for the user."""
    if req["state"] == "off" and req.get("option") and req.get("mod_off"):
        return f"설치되어 있지만 꺼져 있고 옵션도 꺼져 있음 ('{req['by']}'을(를) 켜고 옵션 '{req['option']}'을(를) 켜세요)"
    if req["state"] == "off" and req.get("option"):
        return f"설치되어 있지만 옵션이 꺼져 있음 ('{req['by']}'의 옵션 '{req['option']}')"
    if req["state"] == "off":
        return f"설치되어 있지만 꺼져 있음 ({req['by']})"
    if req["state"] == "missing" and req["kind"] == "shader":
        return "없음 · 재질이 쓰는 셰이더 파일이 게임에도 설치한 모드에도 없음 (셰이더 모드가 필요할 수 있음)"
    if req["state"] == "ok":
        return f"있음 ({req['by']})" if req["by"] else "있음"
    if req["state"] == "unknown":
        return "다른 사이트·게임의 모드 · 직접 확인하세요"
    note = "없음 · 스크립트가 불러오는 Lua 라이브러리" if req["kind"] == "lua" else "없음"
    return f"{note} · {req['notes']}" if req.get("notes") else note


class SettingsDialog(Dialog):
    def __init__(self, parent, theme: Theme, manager: ModManager):
        super().__init__(parent, theme, "설정")
        px = theme.px
        self.manager = manager
        s = manager.settings
        self.vars: dict[str, tk.BooleanVar] = {}
        ttk.Label(self.body, text="설정", style="DialogTitle.TLabel").pack(anchor="w")
        columns = ttk.Frame(self.body)
        columns.pack(fill="x")
        left = ttk.Frame(columns)
        left.pack(side="left", fill="y", anchor="n", padx=(0, px(28)))
        right = ttk.Frame(columns)
        right.pack(side="left", fill="y", anchor="n")

        def section(parent, title):
            ttk.Label(parent, text=title, style="DialogSection.TLabel").pack(anchor="w", pady=(px(16), px(4)))

        def check(parent, key, text):
            var = tk.BooleanVar(value=getattr(s, key))
            self.vars[key] = var
            theme.checkbutton(parent, text, var).pack(anchor="w", pady=px(1))

        section(left, "화면")
        row = ttk.Frame(left)
        row.pack(anchor="w", pady=(0, px(4)))
        ttk.Label(row, text="테마").pack(side="left", padx=(px(4), px(12)))
        self.theme_var = tk.StringVar(value=THEME_NAMES.get(s.theme, THEME_NAMES["dark"]))
        ttk.Combobox(row, textvariable=self.theme_var, values=list(THEME_NAMES.values()), state="readonly",
                     width=10).pack(side="left")
        check(left, "remember_window", "창 크기와 위치 기억")
        section(left, "게임 실행")
        check(left, "apply_before_launch", "실행 전에 남은 변경사항 먼저 적용")
        check(left, "minimize_on_launch", "게임을 실행하면 이 창 최소화")
        section(left, "업데이트")
        check(left, "check_updates", "시작할 때 새 버전 확인")
        check(left, "check_refw_updates", "시작할 때 REFramework 업데이트 확인")

        section(right, "모드 적용")
        check(right, "auto_apply", "변경 즉시 게임 폴더에 적용")
        check(right, "enable_on_install", "설치한 모드를 바로 활성화")
        check(right, "confirm_remove", "모드를 삭제하기 전에 확인")
        check(right, "check_game_running", "적용 전에 게임 실행 여부 확인")
        check(right, "check_materials", "시작할 때와 설치한 뒤 재질 점검")
        ttk.Label(right, text=f"PAK 모드는 게임 폴더의 {PAK_MODS_DIR} 폴더에 넣고, REFramework가 불러옵니다.",
                  style="Muted.TLabel", font=theme.fonts["small"], wraplength=px(300),
                  justify="left").pack(anchor="w", padx=(px(4), 0), pady=(px(10), 0))

        section(self.body, "Nexus Mods")
        ttk.Label(self.body, text="설치한 모드의 새 버전을 Nexus에서 확인합니다. Nexus 계정 설정의 API 키 화면에서 "
                                  "개인 API 키(Personal API Key)를 복사해 넣으세요.",
                  style="Muted.TLabel", font=theme.fonts["small"], wraplength=px(560),
                  justify="left").pack(anchor="w", padx=(px(4), 0), pady=(0, px(6)))
        row = ttk.Frame(self.body)
        row.pack(fill="x", padx=(px(4), 0))
        ttk.Label(row, text="API 키").pack(side="left", padx=(0, px(12)))
        self.key_var = tk.StringVar(value=s.nexus_api_key)
        ttk.Entry(row, textvariable=self.key_var, show="•", width=40).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="키 받기", style="Compact.TButton",
                   command=lambda: webbrowser.open(nexus.API_KEY_URL)).pack(side="right", padx=(px(6), 0))
        ttk.Button(row, text="확인", style="Compact.TButton", command=self.validate_key).pack(side="right",
                                                                                         padx=(px(6), 0))
        self.key_status = ttk.Label(self.body, text="", style="Muted.TLabel", font=theme.fonts["small"])
        self.key_status.pack(anchor="w", padx=(px(4), 0), pady=(px(4), 0))
        check(self.body, "check_mod_updates", "시작할 때 설치한 모드의 업데이트 확인")
        self._key_events: queue.Queue = queue.Queue()

        section(self.body, "데이터 폴더")
        row = ttk.Frame(self.body)
        row.pack(fill="x")
        ttk.Label(row, text=shorten_middle(str(manager.data_dir), 64), style="Muted.TLabel").pack(side="left")
        ttk.Button(row, text="열기", style="Compact.TButton",
                   command=lambda: fsutil.open_in_explorer(manager.data_dir)).pack(side="right")
        self.buttons("저장", self.ok)

    def validate_key(self):
        """Ask Nexus whose key this is, without blocking the dialog."""
        key = self.key_var.get().strip()
        if not key:
            self.key_status.configure(text="API 키를 넣으세요.")
            return
        self.key_status.configure(text="확인 중...")

        def worker():
            try:
                self._key_events.put(nexus.NexusClient(key).validate())
            except nexus.NexusError as exc:
                self._key_events.put(exc)

        threading.Thread(target=worker, daemon=True).start()
        self.after(100, self._poll_key)

    def _poll_key(self):
        try:
            result = self._key_events.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_key)
            return
        if isinstance(result, Exception):
            self.key_status.configure(text=f"✕  {result}")
        else:
            kind = "프리미엄" if result.get("is_premium") else "일반"
            self.key_status.configure(text=f"✓  {result.get('name') or '이름 없음'} 계정 ({kind}) 키입니다.")

    def ok(self):
        theme_name = next((k for k, v in THEME_NAMES.items() if v == self.theme_var.get()), "dark")
        try:
            self.manager.update_settings(
                theme=theme_name,
                nexus_api_key=self.key_var.get().strip(),
                **{key: var.get() for key, var in self.vars.items()},
            )
        except ValueError as exc:
            messagebox.showerror("설정", str(exc), parent=self)
            return
        self.result = True
        self.destroy()


FINDING_COLORS = {"high": "danger", "medium": "warn", "low": "subtle", "info": "info"}  # the dot before a finding
BISECT_TIP = "모드를 켜고 끄며 실행해 보고 게임을 튕기게 하는 모드를 찾습니다 (기록만으로 모드를 알 수 없을 때)"
CRASH_ACCESS_OFF = ("켜면 REFramework 설정(LooseFileLoader_LogAccessedFiles)을 바꿔, 튕긴 기록에 튕기기 직전 게임이 읽은 모드 "
                    "파일이 함께 남습니다. 원인 모드 찾기를 할 때만 켜려면 그 창에서 고르세요.")
CRASH_ACCESS_ON = "켜져 있습니다. 게임을 실행할 때마다 게임이 읽는 파일이 기록됩니다. 끄면 REFramework의 기본 설정으로 돌아갑니다."
CRASH_ACCESS_DONE = "켰습니다. 다음에 게임을 실행할 때부터 게임이 읽는 파일이 기록됩니다."
CRASH_ACCESS_SEARCHING = "원인 모드 찾기 중에는 바꿀 수 없습니다. 원인 찾기를 마친 뒤 바꾸세요."
CRASH_ACCESS_PENDING = "원인 모드 찾기 때 켠 기록이라 게임을 끄면 원래대로 되돌립니다. 계속 켜 두려면 체크하세요."
CRASH_ACCESS_OFF_DONE = "껐습니다. REFramework의 기본 설정입니다."
CRASH_ACCESS_LOOSE_KEPT = " 루스 파일 읽기(LooseFileLoader)는 루스 파일을 읽는 모드가 있어 켠 채로 둡니다."
PASTE_HINT = "게임 오류 창(RE ENGINE - Aborted, Fatal D3D error 등)이나 Windows 이벤트 뷰어의 문구를 붙여 넣으면 분석합니다."


def link_mod_names(t: tk.Text, start: str, mod_ids, names: dict[str, str]) -> bool:
    """Make the names of these mods after ``start`` (always in quotes) links tagged ``mod:<id>`` (a click
    shows the mod in the list). Returns whether there was one."""
    found = False
    for mod_id in mod_ids:
        name = names.get(mod_id)
        if not name:
            continue
        pos = start
        while True:
            pos = t.search(f"'{name}'", pos, stopindex="end", exact=True)
            if not pos:
                break
            end = f"{pos}+{len(name) + 2}c"
            t.tag_add("link", f"{pos}+1c", f"{end}-1c")
            t.tag_add(f"mod:{mod_id}", f"{pos}+1c", f"{end}-1c")
            found = True
            pos = end
    return found


def session_context_text(session) -> str:
    """How a session was started and what was on, in a line (게임 튕김 기록 window)."""
    if session is None:
        return ""
    context = session.context
    mods = context.get("mods") if isinstance(context.get("mods"), list) else None
    on = sum(1 for m in mods or () if isinstance(m, dict) and m.get("enabled"))
    if session.launched:
        parts = ["매니저에서 실행"]
        if context.get("profile"):
            parts.append(f"프로필 {context['profile']}")
        if mods is not None:
            parts.append(f"켜진 모드 {on}개")
    else:
        parts = ["매니저 밖에서 실행했거나 실행 기록 없음"]
        if mods is not None:
            parts.append(f"기록을 모을 때 켜진 모드 {on}개")
    if session.dump_path is not None:
        parts.append("크래시 덤프 있음")
    return " · ".join(parts)


class CrashDialog(Dialog):
    """게임 튕김 기록: the kept game sessions and what each one's records point at, text pasted from an
    error window, REFramework's Lua error logging and the support zip. Matching the records to the mods
    reads paks, so it runs off the GUI thread. Result: ("show", mod id), ("matfix", [mod ids]),
    ("refw",) or None."""

    def __init__(self, parent, theme: Theme, manager: ModManager, sessions: list, selected=None,
                 builds: list | None = None, refw_text: str = "", live: str = "", searching: bool = False):
        super().__init__(parent, theme, crashes.TITLE)
        px, f, p = theme.px, theme.fonts, theme.p
        self.manager = manager
        self.sessions = sessions
        self.builds = builds
        self.refw_text = refw_text
        self.live = live  # start of the session of the game running now (its records are not finished)
        self.explanation = None  # crashes.Explanation of the shown session (None while it is read)
        self.pasted: list | None = None  # findings in the pasted text (None: nothing analysed)
        self.pasted_text = ""
        self._show_paste = False  # scroll to what the pasted text gave once it is shown
        self._events: queue.Queue = queue.Queue()
        self._jobs: dict[str, int] = {}  # kind -> number of the latest job (older results are dropped)
        self._poll_id = None
        self._links = False  # mod names in the findings are links
        self._saving = False  # the support zip is being written
        wrap = px(640)

        ttk.Label(self.body, text=crashes.TITLE, style="DialogTitle.TLabel").pack(anchor="w")
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(px(10), 0))
        self.session_var = tk.StringVar()
        if sessions:
            ttk.Label(row, text="기록").pack(side="left", padx=(0, px(10)))
            titles = [f"{i}. {s.title(running=self._running(s))}" for i, s in enumerate(sessions, 1)]
            self.picker = ttk.Combobox(row, textvariable=self.session_var, values=titles, state="readonly", width=58)
            self.picker.pack(side="left")
            if selected is not None:
                index = next((i for i, s in enumerate(sessions) if s.id == selected.id), 0)
            else:  # the newest finished one (not the game running now)
                index = next((i for i, s in enumerate(sessions) if not self._running(s)), 0)
            self.picker.current(index)
            self.picker.bind("<<ComboboxSelected>>", lambda e: (self.picker.selection_clear(), self._explain()))
            ttk.Button(row, text="기록 폴더 열기", style="Link.TButton", command=self._open_folder).pack(side="right")
        self.summary = ttk.Label(self.body, text="", wraplength=wrap, justify="left")
        self.summary.pack(anchor="w", pady=(px(10), 0))
        self.context_label = ttk.Label(self.body, text="", style="Muted.TLabel", font=f["small"], wraplength=wrap,
                                       justify="left")
        self.context_label.pack(anchor="w", pady=(px(2), 0))

        ttk.Label(self.body, text=crashes.SUSPECTS_HEADER, style="DialogSection.TLabel").pack(
            anchor="w", pady=(px(14), px(4)))
        card = theme.mark(tk.Frame(self.body), "card")
        card.pack(fill="both", expand=True)
        self.actions = ttk.Frame(card, style="Card.TFrame", padding=(px(16), px(2), px(12), px(10)))
        lines = 9 if self.winfo_screenheight() >= px(900) else 6  # the window fits a 768-pixel screen
        self.text = theme.mark(tk.Text(card, wrap="word", width=80, height=lines, font=f["base"], padx=px(16),
                                       pady=px(10), cursor="arrow", spacing1=px(2)), "text")
        sb = ttk.Scrollbar(card, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=autohide(sb, side="right", fill="y"))
        self.text.pack(side="left", fill="both", expand=True)
        t = self.text
        t.tag_configure("finding", lmargin1=0, lmargin2=px(20), tabs=(px(20),), spacing1=px(8))
        t.tag_configure("lead", font=f["bold"])
        t.tag_configure("evidence", foreground=p["muted"], font=f["small"], lmargin1=px(20), lmargin2=px(20))
        t.tag_configure("heading", foreground=p["accent"], font=f["small_bold"], spacing1=px(14))
        t.tag_configure("muted", foreground=p["muted"])
        t.tag_configure("link", foreground=p["accent"], underline=True)
        t.tag_bind("link", "<Enter>", lambda e: t.configure(cursor="hand2"))
        t.tag_bind("link", "<Leave>", lambda e: t.configure(cursor="arrow"))
        t.tag_bind("link", "<Button-1>", self._on_link)
        for level, color in FINDING_COLORS.items():
            t.tag_configure(f"dot_{level}", foreground=p[color], font=f["bold"])

        ttk.Label(self.body, text="오류 창 문구", style="DialogSection.TLabel").pack(anchor="w", pady=(px(14), px(2)))
        ttk.Label(self.body, text=PASTE_HINT, style="Muted.TLabel", font=f["small"], wraplength=wrap,
                  justify="left").pack(anchor="w", pady=(0, px(6)))
        row = ttk.Frame(self.body)
        row.pack(fill="x")
        paste_card = theme.mark(tk.Frame(row), "card")
        paste_card.pack(side="left", fill="x", expand=True)
        self.paste = theme.mark(tk.Text(paste_card, height=2, width=60, wrap="word", font=f["small"], padx=px(10),
                                        pady=px(6), undo=True), "text")
        self.paste.pack(fill="both", expand=True)
        self.paste.bind("<<Paste>>", lambda e: self.after(50, self.analyze), add="+")
        self.analyze_button = ttk.Button(row, text="문구 분석", command=self.analyze)
        self.analyze_button.pack(side="left", anchor="n", padx=(px(8), 0))

        refw_ready = bool(manager.game_dir) and (manager.refw_installed() or crashes.config_path(manager.game_dir).is_file())
        self.lua_var = tk.BooleanVar(value=refw_ready and crashes.lua_logging(manager.game_dir))
        self.lua_check = theme.checkbutton(self.body, crashes.LUA_LOG_CHECKBOX, self.lua_var, command=self._toggle_lua)
        self.lua_check.pack(anchor="w", pady=(px(12), 0))
        self.lua_note = ttk.Label(self.body, text="", style="Muted.TLabel", font=f["small"], wraplength=wrap,
                                  justify="left")
        if not refw_ready:
            self.lua_check.state(["disabled"])
            self._lua_note("REFramework가 설치되어 있지 않습니다.")
        # REFramework's record of the files the game reads (LooseFileLoader_LogAccessedFiles), for good: the
        # records then show which mods' files the game read right before a crash (원인 모드 찾기 turns it on
        # for its launches only, with its own box)
        self.searching = searching
        self.access_offer = None
        self._access_kept = ""  # the note of a change just made (the offer worked out again after it keeps it)
        self.access_var = tk.BooleanVar(self, value=False)
        self.access_check = theme.checkbutton(self.body, accessed.CRASH_WINDOW_CHECKBOX, self.access_var,
                                              command=self._toggle_access)
        self.access_check.pack(anchor="w", pady=(px(6), 0))
        self.access_note = ttk.Label(self.body, text="", style="Muted.TLabel", font=f["small"], wraplength=wrap,
                                     justify="left")
        self.access_check.state(["disabled"])
        if refw_ready:
            self._offer()
        else:
            self._access_note(accessed.LOG_NO_REFW)

        ttk.Separator(self.body).pack(fill="x", pady=(px(14), px(10)))
        ttk.Label(self.body, text=crashes.REPORT_NOTE, style="Muted.TLabel", font=f["small"], wraplength=wrap,
                  justify="left").pack(anchor="w")
        self.dump_var = tk.BooleanVar(value=False)
        self.dump_check = theme.checkbutton(self.body, crashes.DUMP_CHECKBOX, self.dump_var)
        self.dump_check.pack(anchor="w", pady=(px(6), 0))
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(px(12), 0))
        bisect_button = ttk.Button(row, text=bisect.TITLE, command=self._bisect)
        bisect_button.pack(side="left")
        Tooltip(theme, bisect_button, BISECT_TIP)
        ttk.Button(row, text="닫기", command=self.cancel).pack(side="right")
        self.save_button = ttk.Button(row, text="진단 파일 저장", style="Accent.TButton", command=self.save_report)
        self.save_button.pack(side="right", padx=(0, px(8)))
        self._explain()

    # ---- background work
    def _run(self, kind: str, func):
        """Run ``func`` on another thread; its result (or exception) reaches _done on this one."""
        job = self._jobs[kind] = self._jobs.get(kind, 0) + 1

        def worker():
            try:
                result = func()
            except Exception as exc:  # shown in the window
                if not isinstance(exc, EXPECTED_ERRORS):
                    log.error("게임 기록을 읽다가 오류가 났습니다:\n%s", traceback.format_exc())
                result = exc
            self._events.put((kind, job, result))

        threading.Thread(target=worker, daemon=True).start()
        if self._poll_id is None:
            self._poll_id = self.after(60, self._poll)

    def _poll(self):
        self._poll_id = None
        try:
            while self.winfo_exists():
                kind, job, result = self._events.get_nowait()
                if job == self._jobs.get(kind):
                    self._done(kind, result)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self._poll_id = self.after(60, self._poll)

    def destroy(self):
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        super().destroy()  # (Dialog drops its Tk variables)

    def _done(self, kind: str, result):
        if kind == "explain":
            self.explanation = result
            self._render()
        elif kind == "paste":
            self.pasted = result
            self._render()
        elif kind == "lua":
            self._lua_done(*(result if isinstance(result, tuple) else (self.lua_var.get(), result)))
        elif kind == "offer":
            self._offer_done(result)
        elif kind == "access":
            self._access_done(*(result if isinstance(result, tuple) else (self.access_var.get(), result)))
        elif kind == "report":
            self._saving = False
            self.save_button.configure(text="진단 파일 저장")
            self.save_button.state(["!disabled"])
            if isinstance(result, Exception):
                log.error("진단 파일을 만들지 못했습니다: %s", result)
                messagebox.showerror(crashes.TITLE, f"진단 파일을 만들지 못했습니다:\n{result}", parent=self)
            else:
                log.info("게임 튕김 진단 파일을 저장했습니다: %s", result)
                messagebox.showinfo(crashes.TITLE, f"저장했습니다:\n{result}", parent=self)

    # ---- the shown session
    def _running(self, session) -> bool:
        return bool(self.live) and session is not None and session.started == self.live

    def _session(self):
        if not self.sessions:
            return None
        index = self.picker.current() if self.picker.current() >= 0 else 0
        return self.sessions[index]

    def _open_folder(self):
        session = self._session()
        try:
            if session is not None:
                fsutil.open_in_explorer(session.folder)
        except OSError as exc:
            messagebox.showerror(crashes.TITLE, f"폴더를 열지 못했습니다: {exc}", parent=self)

    def _explain(self):
        session = self._session()
        running = self._running(session)
        self.explanation = None
        self._show_paste = False  # another session: from the top
        if session is None:
            self.summary.configure(text="모은 기록이 없습니다. 매니저를 켤 때와 매니저에서 게임을 실행할 때 REFramework "
                                        "기록을 챙겨 둡니다.", style="Muted.TLabel")
        else:
            self.summary.configure(text=session.title(running=running), style="TLabel")
        self.context_label.configure(text=session_context_text(session))
        has_dump = session is not None and session.dump_path is not None
        self.dump_check.state(["!disabled"] if has_dump else ["disabled"])
        if not has_dump:
            self.dump_var.set(False)
        if not self._saving:
            self.save_button.state(["disabled"])
        self._render()
        index, builds = self.manager.crash_index(), self.builds
        self._run("explain", lambda: crashes.explain(session, index, builds, running=running))
        if self.pasted_text:
            self._analyze(self.pasted_text)

    def analyze(self):
        """Look at the text pasted from an error window."""
        text = self.paste.get("1.0", "end-1c").strip()
        if text == self.pasted_text and self.pasted is not None:
            return
        self.pasted_text = text
        if not text:
            self.pasted = None
            self._render()
            return
        self._show_paste = True
        self._analyze(text)

    def _analyze(self, text: str):
        session, index = self._session(), self.manager.crash_index()
        self._run("paste", lambda: crashes.explain_text(text, index, session))

    # ---- what is shown
    def _finding(self, finding, lead: bool = False):
        t = self.text
        t.insert("end", "●", ("finding", f"dot_{finding.level}" if finding.level in FINDING_COLORS else "dot_info"))
        start = t.index("end-1c")
        t.insert("end", "\t" + finding.text + "\n", ("finding", "lead") if lead else ("finding",))
        if link_mod_names(t, start, finding.mods, {m.id: m.name for m in self.manager.mods}):
            self._links = True
        for line in finding.evidence[:3]:
            t.insert("end", f"근거: {line}\n", ("evidence",))

    def _on_link(self, event):
        for tag in self.text.tag_names(f"@{event.x},{event.y}"):
            if tag.startswith("mod:"):
                self.finish(("show", tag[4:]))
                return

    def _render(self):
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        for tag in t.tag_names():
            if tag.startswith("mod:"):
                t.tag_delete(tag)
        self._links = False
        e = self.explanation
        if e is None:
            t.insert("end", "기록을 읽는 중...\n", ("muted",))
        elif isinstance(e, Exception):
            t.insert("end", f"기록을 읽지 못했습니다: {e}\n", ("muted",))
        else:
            if e.summary:
                self.summary.configure(text=e.summary, style="TLabel")
            for i, finding in enumerate(e.findings):
                self._finding(finding, lead=i == 0)
            if not e.findings:
                t.insert("end", "찾은 것이 없습니다.\n", ("muted",))
        if self.pasted is not None:
            t.insert("end", "붙여 넣은 문구에서 찾은 것\n", ("heading",))
            if isinstance(self.pasted, Exception):
                t.insert("end", f"문구를 분석하지 못했습니다: {self.pasted}\n", ("muted",))
            for finding in self.pasted if isinstance(self.pasted, list) else ():
                self._finding(finding)
            if self.pasted == []:
                t.insert("end", "알아볼 수 있는 내용이 없습니다. 오류 창의 문구를 고치지 말고 그대로 붙여 넣어 주세요.\n",
                         ("muted",))
        t.delete("end-2c", "end-1c")  # no empty line at the end
        t.configure(state="disabled")
        heading = t.tag_ranges("heading")
        if self._show_paste and self.pasted is not None and heading:  # below the session's findings: show it
            self.update_idletasks()  # (scrolled by the text's size as shown: other parts of the window may move)
            t.see("end")
            t.see(heading[0])
        self._fill_actions()
        if e is not None and not self._saving:  # also without findings: the zip has the mods and the logs
            self.save_button.state(["!disabled"])

    def _findings(self) -> list:
        found = list(self.explanation.findings) if isinstance(self.explanation, crashes.Explanation) else []
        return found + (list(self.pasted) if isinstance(self.pasted, list) else [])

    def _fill_actions(self):
        """Buttons for what may help (the material repair, a REFramework update) under the findings."""
        for child in self.actions.winfo_children():
            child.destroy()
        px = self.theme.px
        findings = self._findings()
        installed = {m.id for m in self.manager.mods}
        matfix_ids = list(dict.fromkeys(i for f in findings if f.action == "matfix" for i in f.mods if i in installed))
        refw_update = any(f.action == "update_refw" for f in findings)
        if self._links:
            ttk.Label(self.actions, text="모드 이름을 누르면 목록에서 그 모드를 보여 줍니다.", style="CardMuted.TLabel",
                      font=self.theme.fonts["small"]).pack(side="left")
        if refw_update:
            ttk.Button(self.actions, text="REFramework 확인", style="Compact.TButton",
                       command=lambda: self.finish(("refw",))).pack(side="right", padx=(px(6), 0))
        if matfix_ids:
            ttk.Button(self.actions, text="재질 점검·복구", style="Compact.TButton",
                       command=lambda: self.finish(("matfix", matfix_ids))).pack(side="right", padx=(px(6), 0))
        if self.actions.winfo_children():
            if not self.actions.winfo_manager():
                self.actions.pack(side="bottom", fill="x", before=self.text)
        else:
            self.actions.pack_forget()

    def finish(self, result):
        self.result = result
        self.destroy()

    def _bisect(self):
        """원인 모드 찾기, started from the shown record."""
        session = self._session()
        self.finish(("bisect", session.id if session is not None else ""))

    # ---- REFramework's Lua error logging
    def _lua_note(self, text: str):
        self.lua_note.configure(text=text)
        if not self.lua_note.winfo_manager():
            self.lua_note.pack(anchor="w", padx=(self.theme.px(30), 0), after=self.lua_check)

    def _toggle_lua(self):
        """Switch ScriptRunner_LogToDisk in REFramework's config: the user ticked the box; only while
        the game is closed (REFramework writes its config again while it runs)."""
        want = self.lua_var.get()
        self.lua_check.state(["disabled"])
        self._lua_note("REFramework 설정을 바꾸는 중...")

        def work():
            try:
                self.manager.set_refw_option(crashlog.LUA_LOG_KEY, want)
            except (DeployError, ValueError, OSError) as exc:
                return want, exc
            return want, None

        self._run("lua", work)

    def _lua_done(self, want: bool, error):
        self.lua_check.state(["!disabled"])
        if error is not None:
            self.lua_var.set(not want)
            self._lua_note(f"바꾸지 못했습니다: {error}")
            return
        log.info("REFramework 설정: Lua 스크립트 오류 기록 %s (%s)", "켬" if want else "끔", crashlog.LUA_LOG_KEY)
        self._lua_note("켰습니다. 다음에 게임을 실행할 때부터 Lua 스크립트 오류가 REFramework 기록에 남습니다." if want
                       else "껐습니다. REFramework의 기본 설정입니다.")

    # ---- REFramework's record of the files the game reads
    def _access_note(self, text: str):
        self.access_note.configure(text=text)
        if not self.access_note.winfo_manager():
            self.access_note.pack(anchor="w", padx=(self.theme.px(30), 0), after=self.access_check)

    def _loose_mods(self) -> bool:
        """A mod with loose (natives) files is on (GUI thread)."""
        return any("loose" in self.manager.mod_kinds(m.id) for m in self.manager.mods if m.enabled)

    def _offer(self):
        """Work out (again) whether the record can be turned on or off now, and whether a search's restore waits
        for the game to close (off the GUI thread: reads a few files)."""
        manager, loose = self.manager, self._loose_mods()
        self._run("offer", lambda: (accessed.manager_offer(manager, is_game_running(), loose=loose),
                                    accessed.pending(manager.data_dir)))

    def _offer_done(self, result):
        """Whether the record can be turned on or off now ((accessed.Offer, the pending restore), or the error
        looking at it)."""
        offer, pending = result if isinstance(result, tuple) else (result, None)
        kept, self._access_kept = self._access_kept, ""
        if not isinstance(offer, accessed.Offer):
            self.access_offer = None
            self._access_note(f"REFramework 설정을 확인하지 못했습니다: {offer}")
            return
        self.access_offer = offer
        self.access_var.set(offer.already)
        if self.searching:
            self._access_note(CRASH_ACCESS_SEARCHING)
        elif offer.already and pending is not None:
            # (on for a search that ended while the game ran: off again once the game is closed, unless kept here)
            self.access_var.set(False)
            self.access_check.state(["!disabled"])
            self._access_note(kept or CRASH_ACCESS_PENDING)
        elif offer.already:
            self.access_check.state(["!disabled"])
            self._access_note(kept or CRASH_ACCESS_ON)
        elif offer.available:
            self.access_check.state(["!disabled"])
            self._access_note(kept or CRASH_ACCESS_OFF + (accessed.LOG_NOTE_LOOSE if accessed.LOOSE_KEY in offer.changes
                                                          else ""))
        else:
            self._access_note(offer.text)

    def _toggle_access(self):
        """Turn the record on for good (no restore after a search: the user chose it here; what it changed is kept
        to put back when it is turned off here), or off again; only while the game is closed
        (ModManager.set_refw_option refuses otherwise - keeping a search's pending restore from undoing it works
        any time)."""
        want, offer = self.access_var.get(), self.access_offer
        if offer is None:
            self.access_var.set(not want)
            return
        self.access_check.state(["disabled"])
        self._access_note("REFramework 설정을 바꾸는 중...")
        manager, loose_mods = self.manager, self._loose_mods()

        def work():
            stays = False
            try:
                if want:
                    restore = accessed.enable_logging(manager.set_refw_option, offer)
                    # (an earlier search's restore must not undo this: taken over, to put back when turned off here)
                    accessed.keep_enabled(manager.data_dir, restore, accessed.pending(manager.data_dir))
                    accessed.drop_pending(manager.data_dir)
                else:
                    stays = accessed.disable_logging(manager.set_refw_option, manager.data_dir, offer.path or None,
                                                     loose_mods, manager.game_dir)
            except (DeployError, ValueError, OSError) as exc:
                return want, exc, False
            return want, None, stays

        self._run("access", work)

    def _access_done(self, want: bool, error, stays: bool = False):
        if error is not None:
            self.access_var.set(not want)
            self.access_check.state(["!disabled"])
            self._access_note(f"바꾸지 못했습니다: {error}")
            return
        log.info("REFramework 설정: 게임이 읽는 파일 기록 %s (%s)", "켬" if want else "끔", accessed.LOG_KEY)
        self._access_kept = CRASH_ACCESS_DONE if want else CRASH_ACCESS_OFF_DONE + (CRASH_ACCESS_LOOSE_KEPT if stays
                                                                                     else "")
        self._access_note(self._access_kept)
        self._offer()

    # ---- the support zip
    def save_report(self):
        session = self._session()
        stamp = session.id if session is not None else time.strftime("%Y%m%d-%H%M%S")
        path = filedialog.asksaveasfilename(parent=self, title="진단 파일 저장", defaultextension=".zip",
                                            initialfile=f"게임튕김기록_{stamp}.zip", filetypes=[("zip", "*.zip")])
        if not path:
            return
        mods = self.manager.crash_report_mods(kinds_text)
        extra = []
        if self.pasted_text:
            extra = ["[붙여 넣은 오류 창 문구]", self.pasted_text[:4000], ""]
            for finding in self.pasted if isinstance(self.pasted, list) else ():
                extra += [f"• {finding.text}", *(f"    근거: {e}" for e in finding.evidence[:3])]
        explanation = self.explanation if isinstance(self.explanation, crashes.Explanation) else None
        include_dump = self.dump_var.get() and session is not None and session.dump_path is not None
        self._saving = True
        self.save_button.configure(text="저장 중...")
        self.save_button.state(["disabled"])
        self._run("report", lambda: self.manager.export_crash_report(
            Path(path), session, explanation, mods, self.refw_text, extra, include_dump))


# ---- 원인 모드 찾기 -------------------------------------------------------------------------------------
BISECT_HOW = (
    "목록 위 안내 줄의 '게임 실행'으로 게임을 켜고, 안내 줄에 나온 대로 해 봅니다(튕기던 상황 · 플레이 시간).",
    "게임이 끝나면 '또 튕겼나요?'에 답합니다. 매니저가 확인한 종료 코드와 기록을 먼저 골라 두지만 직접 본 것이 우선합니다.",
    "근거가 충분히 모이면 결과를 가능성(%)과 근거로 보여 줍니다. 그 모드를 끄고 원래 프로필로 돌아갈지는 직접 고릅니다.",
)
BISECT_WHEN = "언제 튕기나요?"
BISECT_WHEN_CHOICES = ("start", "same", "equip", "time", "rare")
BISECT_RECENT = "최근 기록: 실행 후 약 {m}분에 튕겼습니다"
BISECT_ASK_NOTE = ("튕기던 상황을 다시 해 봤을 때 또 튕겼는지 알려 주세요. 매니저가 확인한 것보다 직접 본 것이 우선합니다. "
                   "그 상황까지 가 보지 못했다면 '잘 모르겠어요'를 고르세요.")
BISECT_RETRY_HINT = ("튕기는 상황을 만들기 어렵다면 목록 위 '그만두기'로 그만둘 수 있습니다. 그때 지금까지 가장 의심되는 모드를 "
                     "알려 드립니다.")
BISECT_TAIL = "튕기기 직전 10초 동안 게임이 읽은 모드 파일: {mods}"
BISECT_PLAYING = "게임을 실행했습니다. 튕기던 상황을 다시 해 보세요. 게임이 끝나면 또 튕겼는지 묻습니다."
BISECT_PLAYING_MANUAL = "게임을 실행했습니다. 튕기던 상황을 다시 해 본 뒤 게임을 끄고 '결과 알리기'를 누르세요."
BISECT_ANSWER = "결과 알리기"
BISECT_STILL_RUNNING = ("게임이 아직 실행 중입니다. 게임을 끈 뒤에 답해 주세요(멈췄다면 작업 관리자에서 끄세요). 게임이 끝나면 "
                        "또 튕겼는지 묻습니다.")
BISECT_REFW = "REFramework 업데이트"
BISECT_SHOW_RESULT = "결과 보기"
BISECT_COMPUTING = "다음 실행을 고르는 중..."
BISECT_PREPARING = "원인 찾기 준비 중... (기록 살펴보는 중)"


def run_time(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}초"
    if seconds < 3600:
        return f"{seconds // 60}분 {seconds % 60}초"
    return f"{seconds // 3600}시간 {seconds % 3600 // 60}분"


def exit_detail(game_exit) -> str:
    """``종료 코드 0xC0000005 (메모리 접근 오류) · 9분 33초 실행`` (a gamewatch.GameExit; "" for None)."""
    if game_exit is None:
        return ""
    parts = [f"종료 코드 {gamewatch.code_text(game_exit.code)}" if game_exit.code is not None
             else "종료 코드 모름 (게임을 관리자 권한으로 실행한 경우 등)"]
    if game_exit.seconds is not None:
        parts.append(f"{run_time(game_exit.seconds)} 실행" + ("" if game_exit.started is not None else " 이상"))
    return " · ".join(parts)


def exit_summary(detection) -> tuple[str, str, str]:
    """How the game of a step ended, for 또 튕겼나요?: (box style prefix or "", headline, detail)."""
    if detection is None or (detection.exit is None and detection.record is None):
        return "", "게임이 어떻게 끝났는지 확인하지 못했습니다. 직접 본 대로 알려 주세요.", ""
    detail = exit_detail(detection.exit)
    if detection.record is not None:
        record = detection.record
        what = "게임 오류 보고서" if record.summary.get("crash_from") == "report" else "REFramework 기록"
        detail = "\n".join(x for x in (detail, f"{what}: {record.title()}") if x)
    if detection.crashed:
        return "Alert", detection.label, detail
    if detection.crashed is False:  # what was seen, not a verdict: a crash may end with code 0 too
        return "Banner", "게임이 종료 코드 0으로 끝났고 REFramework 오류 기록은 없습니다.", "\n".join(
            x for x in (detail, "오류 창(RE ENGINE - Aborted 등)이 떴거나 게임이 멈췄다면 '튕겼어요'를 고르세요.") if x)
    return "", "게임이 끝났지만 튕겼는지는 확인하지 못했습니다.", detail


class _ScrollBody:
    """A dialog's content that scrolls when it is taller than ``share`` of the screen (the buttons stay
    under it). Build into ``inner``, then call ``fit()``."""

    def __init__(self, dialog: Dialog, share: float = 0.85):
        # (no reference to the dialog: a cycle would leave its Tk variables to the garbage collector, which may run
        # on another thread)
        self.share, self.px = share, dialog.theme.px
        theme = dialog.theme
        self.frame = ttk.Frame(dialog.body)
        self.frame.pack(fill="both", expand=True)
        self.canvas = theme.mark(tk.Canvas(self.frame), "canvas_bg")
        self.scrollbar = ttk.Scrollbar(self.frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=autohide(self.scrollbar, side="right", fill="y", padx=(theme.px(8), 0)))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

    def fit(self, below: int = 0) -> bool:
        """Size the canvas to the content (at most ``share`` of the screen less ``below`` pixels for what
        comes under it). Returns whether it scrolls."""
        self.inner.update_idletasks()
        width, height = self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()
        least = self.px(240)
        limit = max(least, int(self.frame.winfo_toplevel().winfo_screenheight() * self.share) - below - self.px(60))
        tall = height > limit
        self.canvas.configure(width=width, height=min(height, limit))
        if tall:
            self._bind_wheel(self.inner)
        return tall

    def _bind_wheel(self, widget):
        def wheel(event):
            delta = -1 if (getattr(event, "delta", 0) > 0 or getattr(event, "num", 0) == 4) else 1
            self.canvas.yview_scroll(delta, "units")

        for w in (widget, self.canvas):
            w.bind("<MouseWheel>", wheel, add="+")
            w.bind("<Button-4>", wheel, add="+")
            w.bind("<Button-5>", wheel, add="+")
        for child in widget.winfo_children():
            self._bind_wheel(child)


class BisectStartDialog(Dialog):
    """원인 모드 찾기 before it starts: the suspects with their 근거 (bisect.Plan), 언제 튕기나요?, the record
    of the files the game reads (accessed.Offer) and how it goes. Result: {"when", "minutes", "equip", "log"}
    or None. ``log_files``: the box's default (Settings.bisect_log_files, the last choice)."""

    def __init__(self, parent, theme: Theme, plan, record_title: str = "", offer=None, log_files: bool = False):
        super().__init__(parent, theme, bisect.TITLE)
        px, f = theme.px, theme.fonts
        wrap = px(600)
        self.plan, self.offer = plan, offer
        self.problem = plan.problem
        scroll = self.scroll = _ScrollBody(self)
        body = scroll.inner
        names = plan.names
        ttk.Label(body, text=bisect.TITLE, style="DialogTitle.TLabel").pack(anchor="w")
        record_title = record_title or str(plan.data.get("record_title") or "")
        if record_title:
            ttk.Label(body, text=f"게임 튕김 기록: {record_title}", style="Muted.TLabel", font=f["small"],
                      wraplength=wrap, justify="left").pack(anchor="w", pady=(px(2), 0))
        ttk.Label(body, text=self.problem or plan.intro(), wraplength=wrap, justify="left").pack(
            anchor="w", pady=(px(10), 0))
        self.when_var = tk.StringVar(self, value=plan.when if plan.when in BISECT_WHEN_CHOICES else "same")
        # (the box shows the record's minutes rounded: left as it is, they are not the user's - Session.new keeps them)
        self._minutes_shown = str(int(round(plan.minutes or 10)))
        self.minutes_var = tk.StringVar(self, value=self._minutes_shown)
        self.equip_var = tk.StringVar(self)
        self.log_var = tk.BooleanVar(self, value=False)
        self.equipment = [e for e in plan.data.get("equipment") or [] if isinstance(e, list) and len(e) == 3]
        self.equip_picker = self.minutes_box = self.log_check = None
        if not self.problem:
            self._when(body, wrap)
            self._ranking(body, wrap, names)
            self._log(body, wrap, log_files)
            ttk.Label(body, text="진행 방법", style="DialogSection.TLabel").pack(anchor="w", pady=(px(14), px(2)))
            for n, line in enumerate(BISECT_HOW, 1):
                row = ttk.Frame(body)
                row.pack(fill="x", pady=(px(2), 0))
                ttk.Label(row, text=f"{n}.", style="Muted.TLabel", width=3).pack(side="left", anchor="n")
                ttk.Label(row, text=line, wraplength=wrap - px(28), justify="left").pack(side="left", anchor="w")
            details = plan.lines()
            if details:
                ttk.Label(body, text="이번 찾기에서는", style="DialogSection.TLabel").pack(
                    anchor="w", pady=(px(14), px(2)))
                for line in details:
                    ttk.Label(body, text="• " + line, style="Muted.TLabel", font=f["small"], wraplength=wrap,
                              justify="left").pack(anchor="w", pady=(px(2), 0))
        row = self.buttons("시작", self.ok)
        if self.problem:
            row.winfo_children()[-1].state(["disabled"])
        row.update_idletasks()
        scroll.fit(below=row.winfo_reqheight())

    def _when(self, body, wrap: int):
        """언제 튕기나요?: the r prior and the play line of the steps (bisect.WHEN)."""
        px, f = self.theme.px, self.theme.fonts
        ttk.Label(body, text=BISECT_WHEN, style="DialogSection.TLabel").pack(anchor="w", pady=(px(14), px(2)))
        for key in BISECT_WHEN_CHOICES:
            if key == "equip" and not self.equipment:
                continue  # (only with the equipment window's kept results)
            row = ttk.Frame(body)
            row.pack(fill="x")
            ttk.Radiobutton(row, text=bisect.WHEN_TEXT[key], value=key, variable=self.when_var).pack(side="left")
            if key == "equip":
                labels = [e[1] for e in self.equipment]
                self.equip_picker = ttk.Combobox(row, textvariable=self.equip_var, values=labels, state="readonly",
                                                 width=34)
                self.equip_picker.pack(side="left", padx=(px(10), 0))
                self.equip_picker.current(0)
                self.equip_picker.bind("<<ComboboxSelected>>", lambda e: (self.equip_picker.selection_clear(),
                                                                          self.when_var.set("equip")))
            elif key == "time":
                self.minutes_box = ttk.Spinbox(row, from_=1, to=240, increment=1, textvariable=self.minutes_var,
                                               width=5, command=lambda: self.when_var.set("time"))
                self.minutes_box.pack(side="left", padx=(px(10), px(4)))
                ttk.Label(row, text="분쯤").pack(side="left")
        hint = getattr(self.plan.evidence, "minutes_hint", None)
        if isinstance(hint, (int, float)) and hint > 0:
            ttk.Label(body, text=BISECT_RECENT.format(m=max(1, round(hint))), style="Muted.TLabel", font=f["small"],
                      wraplength=wrap, justify="left").pack(anchor="w", pady=(px(4), 0))

    def _ranking(self, body, wrap: int, names: dict):
        """The most suspected mods (%, name, 근거), then the rest summed up."""
        px, f = self.theme.px, self.theme.fonts
        ttk.Label(body, text=bisect.RANKING_HEADING, style="DialogSection.TLabel").pack(anchor="w", pady=(px(14), px(4)))
        plan = self.plan
        rows = [] if plan.uniform else plan.ranking_rows(names)  # (no evidence: every mod the same)
        note = plan.ranking_note(names)
        singles = plan.singles
        if rows and len(singles) > 1 and singles[0][1] - singles[-1][1] < 0.005 and not any(why for _l, _p, why in rows):
            # (only "not the mods" or the pairs moved: rows of the same % say nothing)
            fam = plan.families
            rows, note = [], (f"모드 {len(singles)}개를 모두 같은 정도(각각 약 {bisect.percent(singles[0][1])})로 의심합니다. "
                              f"그 밖: 두 모드 이상이 함께 원인 약 "
                              f"{bisect.percent(fam.get('and', 0) + fam.get('and3', 0) + fam.get('or', 0))} · "
                              f"모드 문제가 아님 약 {bisect.percent(fam.get('none', 0))}")
        if rows:
            card = self.theme.mark(tk.Frame(body), "card")
            card.pack(fill="x")
            inner = ttk.Frame(card, style="Card.TFrame", padding=(px(14), px(8), px(14), px(10)))
            inner.pack(fill="x")
            inner.columnconfigure(1, weight=1)
            for n, (label, p, why) in enumerate(rows):
                top = px(6) if n else 0
                ttk.Label(inner, text=f"약 {bisect.percent(p)}", style="Card.TLabel", font=f["bold"],
                          width=9).grid(row=2 * n, column=0, sticky="nw", pady=(top, 0))
                ttk.Label(inner, text=label, style="Card.TLabel", wraplength=wrap - px(120), justify="left").grid(
                    row=2 * n, column=1, sticky="w", pady=(top, 0))
                if why:
                    ttk.Label(inner, text="\n".join(f"근거: {w}" for w in why), style="CardMuted.TLabel",
                              font=f["small"], wraplength=wrap - px(120), justify="left").grid(
                        row=2 * n + 1, column=1, sticky="w")
        none = [str(x) for x in plan.data.get("none_reasons") or []][:2]
        if none and not plan.uniform:
            note += "\n모드 문제가 아닐 수 있는 근거: " + " · ".join(none)
        if note:
            ttk.Label(body, text=note, style="Muted.TLabel", font=f["small"], wraplength=wrap, justify="left").pack(
                anchor="w", pady=(px(6), 0))

    def _log(self, body, wrap: int, default: bool):
        """게임이 읽는 파일을 기록해 더 정확하게 찾기 (REFramework's LooseFileLoader_LogAccessedFiles)."""
        offer = self.offer
        if offer is None:
            return
        from . import accessed

        px, f = self.theme.px, self.theme.fonts
        self.log_var.set(bool(offer.already or (offer.available and default)))
        self.log_check = self.theme.checkbutton(body, accessed.LOG_CHECKBOX, self.log_var)
        self.log_check.pack(anchor="w", pady=(px(14), 0))
        if offer.already or not offer.available:
            self.log_check.state(["disabled"])
        ttk.Label(body, text=offer.text, style="Muted.TLabel", font=f["small"], wraplength=wrap - px(30),
                  justify="left").pack(anchor="w", padx=(px(30), 0))

    def choice(self) -> dict:
        """What the window says now: {"when", "minutes", "equip", "log"}."""
        when = self.when_var.get() if self.when_var.get() in BISECT_WHEN_CHOICES else "same"
        minutes = None
        if when == "time" and not (self.plan.minutes and self.minutes_var.get().strip() == self._minutes_shown):
            try:
                minutes = float(self.minutes_var.get())
            except ValueError:
                minutes = None
            minutes = minutes if minutes is not None and 0 < minutes <= 24 * 60 else None
        equip = None
        if when == "equip" and self.equip_picker is not None:
            index = self.equip_picker.current()
            equip = self.equipment[index][0] if 0 <= index < len(self.equipment) else None
            if equip is None:
                when = "same"
        return {"when": when, "minutes": minutes, "equip": equip, "log": bool(self.log_var.get())}

    def ok(self):
        if self.problem:
            return
        self.result = self.choice()
        self.destroy()


class BisectAskDialog(Dialog):
    """또 튕겼나요? after the game of a step. Result: "crashed", "fine", "retry" (잘 모르겠어요: the same
    step again) or None (answered later). What the manager detected is the default answer. ``run``: what
    accessed.step_run found of the launch (play time, files never requested, the last seconds before the
    crash); ``retries``: how often 잘 모르겠어요 was answered for this step."""

    def __init__(self, parent, theme: Theme, step, detection=None, names: dict | None = None, run: dict | None = None,
                 retries: int = 0):
        super().__init__(parent, theme, bisect.TITLE)
        px, f = theme.px, theme.fonts
        wrap = px(520)
        names = names or {}
        ttk.Label(self.body, text=bisect.ASK, style="DialogTitle.TLabel").pack(anchor="w")
        if step.engine:
            sub = f"{step.number}번째 실행 · {bisect.step_summary(step, names)}"
        else:
            sub = f"원인 찾기 {step.number}/{step.total}단계 · 모드 {len(step.on)}개를 켜고 실행했습니다."
        ttk.Label(self.body, text=sub, style="Muted.TLabel", wraplength=wrap, justify="left").pack(
            anchor="w", pady=(px(4), px(12)))
        style, head, detail = exit_summary(detection)
        box = ttk.Frame(self.body, style=f"{style}.TFrame" if style else "TFrame",
                        padding=(px(14), px(10)) if style else 0)
        box.pack(fill="x")
        head_style, detail_style = (f"{style}Title.TLabel", f"{style}Muted.TLabel") if style else ("TLabel", "Muted.TLabel")
        inner = wrap - px(28) if style else wrap
        ttk.Label(box, text=("⚠  " if style == "Alert" else "") + head, style=head_style, wraplength=inner,
                  justify="left").pack(anchor="w")
        if detail:
            ttk.Label(box, text=detail, style=detail_style, font=f["small"], wraplength=inner,
                      justify="left").pack(anchor="w", pady=(px(2), 0))
        notes = []
        if step.engine:
            run = run if isinstance(run, dict) else {}
            seconds = run.get("seconds")
            if seconds is None and detection is not None and detection.exit is not None:
                seconds = detection.exit.seconds
            crashed = detection is not None and detection.crashed
            notes = [bisect.run_note(step, seconds) if not crashed else "",
                     bisect.exposure_note(step, run.get("unexposed") or [], names)]
            tail = [i for i in run.get("tail") or [] if isinstance(i, str)]
            if tail:
                notes.append(BISECT_TAIL.format(mods=", ".join(f"'{names.get(i, i)}'" for i in tail[:4])
                                                + (f" 외 {len(tail) - 4}개" if len(tail) > 4 else "")))
        self.notes = [n for n in notes if n]
        for note in self.notes:
            ttk.Label(self.body, text=note, style="Muted.TLabel", font=f["small"], wraplength=wrap,
                      justify="left").pack(anchor="w", pady=(px(8), 0))
        ttk.Label(self.body, text=BISECT_ASK_NOTE, style="Muted.TLabel", font=f["small"], wraplength=wrap,
                  justify="left").pack(anchor="w", pady=(px(12), 0))
        if retries >= 2:
            ttk.Label(self.body, text=BISECT_RETRY_HINT, style="Muted.TLabel", font=f["small"], wraplength=wrap,
                      justify="left").pack(anchor="w", pady=(px(6), 0))
        # only a detected crash is picked first (the exit code of a normal quit is not known for sure)
        self.default = "crashed" if detection is not None and detection.crashed else None
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(px(18), 0))
        self.answer_buttons = {}
        for key, text in (("retry", bisect.ANSWER_RETRY), ("fine", bisect.ANSWER_FINE),
                          ("crashed", bisect.ANSWER_CRASHED)):
            button = ttk.Button(row, text=text, style="Accent.TButton" if key == self.default else "TButton",
                                command=lambda k=key: self.finish(k))
            button.pack(side="right", padx=(px(8), 0))
            self.answer_buttons[key] = button
        if self.default:
            self.bind("<Return>", lambda e: self.finish(self.default))
            self.after(50, self.answer_buttons[self.default].focus_set)

    def finish(self, result: str):
        self.result = result
        self.destroy()


class BisectResultDialog(Dialog):
    """The end of 원인 모드 찾기: what it found (a suspicion with its probability and 근거) and the way back to
    the user's profile. Result: ("disable", [mod ids]), ("restore",), ("refw",) (back, then the REFramework
    window), ("more",) (계속 찾기, or 한 번 더 확인 for a named result) or None (decide later). 진단 파일 저장
    works inside the window."""

    def __init__(self, parent, theme: Theme, manager: ModManager, session, result, refw_text: str = ""):
        super().__init__(parent, theme, bisect.TITLE)
        px, f = theme.px, theme.fonts
        self.manager, self.session, self.refw_text = manager, session, refw_text
        self.mods = list(result.mods) if result.can_disable else []
        self._jobs: queue.Queue = queue.Queue()
        self._poll_id = None
        wrap = px(580)
        names = session.names
        aborted = result.kind == "aborted"
        unsure = result.kind == "unsure"
        scroll = self.scroll = _ScrollBody(self)
        body = scroll.inner
        ttk.Label(body, text="원인 찾기를 그만두었습니다" if aborted else "원인 찾기 결과",
                  style="DialogSection.TLabel").pack(anchor="w")
        ttk.Label(body, text=bisect.result_title(result, names), style="DialogTitle.TLabel", wraplength=wrap,
                  justify="left").pack(anchor="w", pady=(px(4), 0))
        if result.runs and not unsure:  # (an unsure result says how often in its lines)
            done = f"게임을 {result.runs}번 실행해 찾았습니다." if result.can_disable else f"게임을 {result.runs}번 실행했습니다."
            ttk.Label(body, text=done, style="Muted.TLabel").pack(anchor="w", pady=(px(4), 0))
        if result.evidence:
            ttk.Label(body, text="지금까지 가장 의심되는 것" if unsure else "근거", style="DialogSection.TLabel").pack(
                anchor="w", pady=(px(12), px(2)))
            for line in result.evidence:
                ttk.Label(body, text="• " + line, style="Muted.TLabel", font=f["small"], wraplength=wrap,
                          justify="left").pack(anchor="w", pady=(px(2), 0))
        refw_mod = manager.refw_mod()
        refw_id = refw_mod.id if refw_mod is not None else None
        # REFramework itself: updating it is the advice; turning it off stops every mod that needs it
        self.refw_hit = refw_id is not None and refw_id in self.mods and not unsure
        advice = session.result_lines(result, refw_id)
        if advice:
            ttk.Label(body, text="해 볼 것", style="DialogSection.TLabel").pack(anchor="w", pady=(px(12), px(0)))
        for line in advice:
            ttk.Label(body, text="• " + line, wraplength=wrap, justify="left").pack(anchor="w", pady=(px(6), 0))
        steps = session.step_lines()
        if steps:
            ttk.Label(body, text="실행한 구성", style="DialogSection.TLabel").pack(anchor="w", pady=(px(14), px(4)))
            card = theme.mark(tk.Frame(body), "card")
            card.pack(fill="both", expand=True)
            text = theme.mark(tk.Text(card, wrap="none", width=74, height=min(len(steps), 6), font=f["small"],
                                      padx=px(12), pady=px(6), cursor="arrow"), "text")
            sb = ttk.Scrollbar(card, orient="vertical", command=text.yview)
            text.configure(yscrollcommand=autohide(sb, side="right", fill="y"))
            text.insert("1.0", "\n".join(steps))
            text.configure(state="disabled")
            text.pack(side="left", fill="both", expand=True)
        back = f"원래 프로필 '{session.original_profile}'(으)로 돌아가면 임시 프로필 '{session.temp_profile}'은(는) 지웁니다."
        if self.mods:
            back += " 끈 모드는 나중에 원래 프로필에서 다시 켤 수 있습니다."
            if unsure:
                back += " 끄기 버튼의 %는 그 모드가 원인에 들어 있을 가능성입니다(혼자, 또는 다른 모드와 함께)."
        if self.refw_hit:
            back += f" '{BISECT_REFW}'를 누르면 원래 프로필로 돌아간 뒤 REFramework 창을 엽니다."
        ttk.Label(body, text=back, style="Muted.TLabel", font=f["small"], wraplength=wrap,
                  justify="left").pack(anchor="w", pady=(px(12), 0))
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(px(16), 0))
        self.report_button = ttk.Button(row, text=bisect.RESULT_REPORT, command=self.save_report)
        self.report_button.pack(side="left")
        disable_first = bool(self.mods) and not self.refw_hit and not unsure
        restore = ttk.Button(row, text=bisect.RESULT_RESTORE, style="TButton" if disable_first else "Accent.TButton",
                             command=lambda: self.finish(("restore",)))
        restore.pack(side="right")
        if self.mods:
            if unsure:
                label = bisect.disable_unsure_text(result, names)
            else:
                label = bisect.RESULT_DISABLE if len(self.mods) == 1 else bisect.RESULT_DISABLE_MANY
            ttk.Button(row, text=label, style="Accent.TButton" if disable_first else "TButton",
                       command=lambda: self.finish(("disable", self.mods))).pack(side="right", padx=(0, px(8)))
        self.more = ""
        if unsure:
            self.more = bisect.RESULT_MORE
        elif result.can_confirm and result.kind in bisect.NAMED:
            self.more = bisect.RESULT_CONFIRM
        if self.more:
            ttk.Button(row, text=self.more, command=lambda: self.finish(("more",))).pack(side="right", padx=(0, px(8)))
        if self.refw_hit:
            ttk.Button(row, text=BISECT_REFW, command=lambda: self.finish(("refw",))).pack(
                side="right", padx=(0, px(8)))
        row.update_idletasks()
        scroll.fit(below=row.winfo_reqheight())

    def finish(self, result):
        self.result = result
        self.destroy()

    def save_report(self):
        """The 게임 튕김 기록 support zip with the search's steps in report.txt (in the background)."""
        path = filedialog.asksaveasfilename(parent=self, title=bisect.RESULT_REPORT, defaultextension=".zip",
                                            initialfile=f"원인찾기_{time.strftime('%Y%m%d-%H%M%S')}.zip",
                                            filetypes=[("zip", "*.zip")])
        if not path:
            return
        m = self.manager
        record = crashes.find_session(m.crash_dir, self.session.record_id) if self.session.record_id else None
        if record is None:
            record = crashes.last_fatal(m.crash_dir)
        index, mods, extra = m.crash_index(), m.crash_report_mods(kinds_text), self.session.report_lines()
        self.report_button.configure(text="저장 중...")
        self.report_button.state(["disabled"])

        def worker():
            try:
                explanation = crashes.explain(record, index) if record is not None else None
                result = m.export_crash_report(Path(path), record, explanation, mods, self.refw_text, extra)
            except Exception as exc:  # shown in the window
                if not isinstance(exc, EXPECTED_ERRORS):
                    log.error("진단 파일을 만들다가 오류가 났습니다:\n%s", traceback.format_exc())
                result = exc
            self._jobs.put(result)

        threading.Thread(target=worker, daemon=True).start()
        self._poll_id = self.after(100, self._poll_report)

    def destroy(self):
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        super().destroy()

    def _poll_report(self):
        try:
            result = self._jobs.get_nowait()
        except queue.Empty:
            self._poll_id = self.after(100, self._poll_report)
            return
        self._poll_id = None
        self.report_button.configure(text=bisect.RESULT_REPORT)
        self.report_button.state(["!disabled"])
        if isinstance(result, Exception):
            log.error("진단 파일을 만들지 못했습니다: %s", result)
            messagebox.showerror(bisect.TITLE, f"진단 파일을 만들지 못했습니다:\n{result}", parent=self)
        else:
            log.info("원인 찾기 진단 파일을 저장했습니다: %s", result)
            messagebox.showinfo(bisect.TITLE, f"저장했습니다:\n{result}", parent=self)


# ---- 실행 전 점검 ---------------------------------------------------------------------------------------
PRECHECK_HEADINGS = {"high": "높음 · 모드가 작동하지 않거나 게임이 튕기거나 멈출 수 있음", "medium": "보통 · 흔한 원인이거나 추정",
                     "low": "낮음 · 알아 두면 좋은 것", "info": "참고"}
PRECHECK_LAUNCH_INTRO = "게임을 켜기 전에 모드와 게임 폴더에서 찾은 것입니다. 원인일 수 있다는 뜻이고 확실하다는 뜻은 아닙니다."
PRECHECK_LAUNCH_NOTE = ("항목의 버튼을 누르면 게임을 실행하지 않고 그 일을 합니다(설정 고치기 · 폴더 열기는 이 창에서 합니다). "
                        "모드 이름을 누르면 목록에서 그 모드를 보여 줍니다.")
PRECHECK_MENU_NOTE = ("게임을 실행할 때는 높음 · 보통 항목 중 새로 생긴 것만 묻고, 실행을 막지는 않습니다. 매니저가 넣지 않은 "
                      "파일은 지우지 않으니 직접 확인하세요.")
PRECHECK_NOTHING = ("원인일 수 있는 것을 찾지 못했습니다. 게임이 튕기지 않는다는 뜻은 아니니, 튕기면 ☰ → 게임 튕김 기록에서 "
                    "원인을 찾아보세요.\n")
PRECHECK_NOTHING_LAUNCH = "이제 확인할 것이 없습니다. 아래 버튼을 누르면 게임을 실행합니다.\n"
PRECHECK_NEW = "새로"
PRECHECK_DELAY = 5000  # ms after the startup records are copied (the mods' own material check goes first)


def _columns(text: str) -> int:
    """Width of a line in columns of the Text widget (Korean and other wide characters count two)."""
    return sum(2 if ord(c) >= 0x1100 else 1 for c in text)


class PrecheckDialog(Dialog):
    """실행 전 점검: what was found, each with a button for what may help. Before a launch (``launch``) the
    high and medium issues with 그래도 실행 · 취소 and 같은 내용이면 다시 묻지 않기 (``new`` ones are
    marked when not all are new); from the menu every issue. 설정 고치기 (after asking) and 폴더 열기
    work in the window, then ``recheck()`` gives the report again. Result: ("run",) (launch only),
    ("action", Issue) or ("show", mod id) for the main window (the game is not started), or None.
    ``dont_ask`` tells whether the box was ticked."""

    def __init__(self, parent, theme: Theme, manager: ModManager, report, launch: bool = False, new=(),
                 recheck=None, note: str = ""):
        super().__init__(parent, theme, precheck.TITLE)
        px, f, p = theme.px, theme.fonts, theme.p
        self.manager, self.report, self.launch, self.recheck = manager, report, launch, recheck
        self.dont_ask_var: tk.BooleanVar | None = None
        # (marks only help while some of the issues were seen before)
        self.new = {tuple(i.keys) for i in new} if len(new) < len(report.important) else set()
        self.dont_ask = False
        self._events: queue.Queue = queue.Queue()
        self._poll_id = None
        self._fixing = False
        self._buttons: list = []  # the buttons in the text (made again with it)
        wrap = px(680)

        self.title_label = ttk.Label(self.body, text="", style="DialogTitle.TLabel")
        self.title_label.pack(anchor="w")
        self.summary = ttk.Label(self.body, text="", style="Muted.TLabel", wraplength=wrap, justify="left")
        self.summary.pack(anchor="w", pady=(px(4), px(10)))
        card = theme.mark(tk.Frame(self.body), "card")
        card.pack(fill="both", expand=True)
        self.text = theme.mark(tk.Text(card, wrap="word", width=84, height=6, font=f["base"], padx=px(16), pady=px(10),
                                       cursor="arrow", spacing1=px(2)), "text")
        sb = ttk.Scrollbar(card, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=autohide(sb, side="right", fill="y"))
        self.text.pack(side="left", fill="both", expand=True)
        t = self.text
        t.tag_configure("issue", lmargin1=0, lmargin2=px(22), tabs=(px(22),), spacing1=px(10))
        t.tag_configure("detail", foreground=p["muted"], font=f["small"], lmargin1=px(22), lmargin2=px(22))
        t.tag_configure("action", lmargin1=px(22), spacing1=px(5), spacing3=px(1))
        t.tag_configure("heading", foreground=p["accent"], font=f["small_bold"], spacing1=px(16))
        t.tag_configure("top", spacing1=0)
        t.tag_configure("new", background=p["chip"], foreground=p["chip_text"], font=f["small_bold"])
        t.tag_configure("muted", foreground=p["muted"])
        t.tag_configure("link", foreground=p["accent"], underline=True)
        t.tag_bind("link", "<Enter>", lambda e: t.configure(cursor="hand2"))
        t.tag_bind("link", "<Leave>", lambda e: t.configure(cursor="arrow"))
        t.tag_bind("link", "<Button-1>", self._on_link)
        for level, color in FINDING_COLORS.items():
            t.tag_configure(f"dot_{level}", foreground=p[color], font=f["bold"])

        self.status = ttk.Label(self.body, text="", style="Muted.TLabel", font=f["small"], wraplength=wrap,
                                justify="left")  # what 설정 고치기 did
        notes = PRECHECK_LAUNCH_NOTE if launch else " ".join(x for x in (PRECHECK_MENU_NOTE, note) if x)
        self.note = ttk.Label(self.body, text=notes, style="Muted.TLabel", font=f["small"], wraplength=wrap,
                              justify="left")
        self.note.pack(anchor="w", pady=(px(10), 0))
        if launch:
            self.dont_ask_var = tk.BooleanVar(self, value=False)
            theme.checkbutton(self.body, precheck.DONT_ASK, self.dont_ask_var).pack(anchor="w", pady=(px(10), 0))
            self.buttons(precheck.RUN_ANYWAY, lambda: self.finish(("run",)), precheck.CANCEL)
        else:
            row = ttk.Frame(self.body)
            row.pack(fill="x", pady=(px(18), 0))
            ttk.Button(row, text="닫기", style="Accent.TButton", command=self.cancel).pack(side="right")
            self.bind("<Return>", lambda e: self.cancel())
        self._render()
        limit = 18 if self.winfo_screenheight() >= px(900) else 11  # the window fits a 768-pixel screen
        t.configure(height=max(4, min(limit, self._lines())))

    def _shown(self) -> list:
        return self.report.important if self.launch else self.report.issues

    def _lines(self) -> int:
        """About how many lines the issues take at the text's width (its height before it is shown)."""
        cols = int(self.text.cget("width")) - 4
        lines, level = 0, None
        for issue in self._shown():
            if issue.level != level:
                level, lines = issue.level, lines + 2
            lines += sum(-(-_columns(x) // cols) for x in (issue.text + "  " + PRECHECK_NEW,
                                                            *issue.detail[:precheck.DETAIL]))
            lines += 3 if issue.action_label else 1
        return lines

    def _can(self, issue) -> bool:
        """The issue's button would do something here."""
        if issue.action == "fix_config":
            return bool(issue.fix)
        if issue.action == "open_folder":
            return bool(self.manager.game_dir)
        if issue.action in ("matfix", "matfix_check"):
            return matfix.available() and bool(issue.mods)
        if issue.action in ("show_mods", "enable_refw"):
            return bool(issue.mods)
        return bool(issue.action_label)

    def _render(self):
        t, report = self.text, self.report
        issues = self._shown()
        for button in self._buttons:
            button.destroy()
        self._buttons = []
        t.configure(state="normal")
        t.delete("1.0", "end")
        for tag in t.tag_names():
            if tag.startswith("mod:"):
                t.tag_delete(tag)
        self.title_label.configure(text=report.title(issues))
        if self.launch:
            self.summary.configure(text=PRECHECK_LAUNCH_INTRO)
        else:
            counts = [(precheck.LEVEL_LABELS[lv], sum(1 for i in issues if i.level == lv)) for lv in precheck.LEVELS]
            self.summary.configure(text=" · ".join(f"{label} {n}" for label, n in counts if n) or
                                   "모드와 게임 폴더에서 흔한 문제를 찾지 못했습니다.")
        names = {m.id: m.name for m in self.manager.mods}
        level = None
        for issue in issues:
            if issue.level != level:
                level = issue.level
                t.insert("end", PRECHECK_HEADINGS.get(level, level) + "\n", ("heading", "top") if t.index("end-1c") == "1.0"
                         else ("heading",))
            t.insert("end", "●", ("issue", f"dot_{issue.level}" if issue.level in FINDING_COLORS else "dot_info"))
            t.insert("end", "\t", ("issue",))
            if tuple(issue.keys) in self.new:
                t.insert("end", f" {PRECHECK_NEW} ", ("issue", "new"))
                t.insert("end", " ", ("issue",))
            start = t.index("end-1c")
            t.insert("end", issue.text + "\n", ("issue",))
            link_mod_names(t, start, issue.mods, names)
            for line in issue.detail[:precheck.DETAIL]:
                t.insert("end", line + "\n", ("detail",))
            if issue.action_label and self._can(issue):
                button = ttk.Button(t, text=issue.action_label, style="Action.TButton",
                                    command=lambda i=issue: self._act(i))
                pos = t.index("end-1c")
                t.window_create(pos, window=button)
                t.insert("end", "\n")
                t.tag_add("action", pos, "end-1c")
                self._buttons.append(button)
        if not issues:
            t.insert("end", PRECHECK_NOTHING_LAUNCH if self.launch else PRECHECK_NOTHING, ("muted",))
        t.delete("end-2c", "end-1c")  # no empty line at the end
        t.configure(state="disabled")

    # ---- the buttons
    def _act(self, issue):
        if self._fixing:
            return
        if issue.action == "fix_config":
            self._fix(issue)
        elif issue.action == "open_folder":
            self._open_folder(issue.folder)
        else:
            self.finish(("action", issue))

    def _on_link(self, event):
        for tag in self.text.tag_names(f"@{event.x},{event.y}"):
            if tag.startswith("mod:"):
                self.finish(("show", tag[4:]))
                return

    def _open_folder(self, folder: str):
        game = Path(self.manager.game_dir)
        path = game / folder if folder else game
        while path != game and not path.is_dir():  # the nearest one that is there
            path = path.parent
        try:
            fsutil.open_in_explorer(path)
        except OSError as exc:
            messagebox.showerror(precheck.TITLE, f"폴더를 열지 못했습니다: {exc}", parent=self)

    def _fix(self, issue):
        """설정 고치기: one line of REFramework's config, after asking (only while the game is closed)."""
        if not messagebox.askyesno(precheck.TITLE, precheck.fix_question(issue), parent=self):
            return
        self._fixing = True
        self._status("REFramework 설정을 바꾸는 중...")
        manager, (key, value), path = self.manager, issue.fix, issue.config or None

        def worker():
            try:
                manager.set_refw_option(key, value, path=path)
                result = None
            except Exception as exc:  # shown in the window
                if not isinstance(exc, EXPECTED_ERRORS):
                    log.error("REFramework 설정을 바꾸다가 오류가 났습니다:\n%s", traceback.format_exc())
                result = exc
            self._events.put((key, value, result))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_id = self.after(60, self._poll)

    def _poll(self):
        try:
            key, value, error = self._events.get_nowait()
        except queue.Empty:
            self._poll_id = self.after(60, self._poll)
            return
        self._poll_id = None
        self._fixing = False
        if isinstance(error, DeployError):
            self._status(precheck.RUNNING)
            return
        if error is not None:
            self._status(f"바꾸지 못했습니다: {error}")
            return
        log.info("실행 전 점검: REFramework 설정을 바꿨습니다 (%s=%s)", key, value)
        if self.recheck is not None:
            try:
                self.report = self.recheck()
            except Exception:
                log.exception("실행 전 점검을 다시 하지 못했습니다.")
            self._render()
        self._status(f"REFramework 설정을 바꿨습니다 ({key}={value}). 다음에 게임을 실행할 때부터 적용됩니다.")

    def _status(self, text: str):
        self.status.configure(text=text)
        if not self.status.winfo_manager():
            self.status.pack(anchor="w", pady=(self.theme.px(10), 0), before=self.note)

    def finish(self, result):
        self.result = result
        self.destroy()

    def destroy(self):
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        if self.dont_ask_var is not None:
            try:
                self.dont_ask = bool(self.dont_ask_var.get())
            except tk.TclError:  # (the manager is closing)
                pass
            self.dont_ask_var = None
        super().destroy()


class App:
    MUTATING_OK_DURING = (None, "deploy")
    # end with an apply: no launch meanwhile
    FOLDER_TASKS = ("purge", "game", "remove", "refw", "install", "matfix", "retarget")

    def __init__(self, root: tk.Tk, manager: ModManager, events: queue.Queue):
        self.root = root
        self.manager = manager
        self.events = events
        self.busy: str | None = None
        self._apply_pending = False
        self._install_queue: list[str] = []
        self._installing = False
        self._close_after_task = False
        self._status_after_task = ""  # a line for the status bar once the running task is done (it says "준비")
        self._retarget_waiting = 0  # 다른 장비로 옮기기 waiting for a running task to make its mod (no launch meanwhile)
        self._closed = False
        self._drag: dict | None = None
        self._preview = None
        self._preview_path: tuple[Path, str] | None = None  # the picture on the info tab, for a larger view
        self._thumbs: dict[tuple, object] = {}  # thumbnails of option pictures, by file and size
        self._option_images: list = []  # the thumbnails on screen (Tk drops images nobody holds)
        self._options_mod: str | None = None
        self._options_gen = 0
        self._wanted_tab: str | None = None  # the details tab the user picked last
        self._shown_tab: str | None = None  # the tab shown before the last change
        self._conflicts = None
        self._log_visible = True
        self._update: Release | None = None
        self._launch_after_apply = False
        self._refw_status: refw.Status | None = None
        self._checking_mods = False
        self._mod_check_error: str | None = None
        self._req_notices: list[str] = []  # just installed mods to tell about missing requirements
        self._first_check: set[str] = set()  # ... and those still to tell about missing shaders after their check
        self._req_flush_scheduled = False
        self._problems: dict[str, list[str]] = {}  # enabled mods that cannot fully work, with the reasons
        self._matfix_running = False  # a background material check is running
        self._matfix_line = ""  # the status line it wrote last (only that one is put back to "준비" at its end)
        self.equipment_window = None  # the 장비 외형 window while it is open
        self._equip_warming = False
        self._equip_again = False
        self._matfix_again = False  # ... and another one was asked for meanwhile
        self._offer_scheduled = False
        self._launching = False  # copying the last game's records before starting the game
        self._notice_session: str | None = None  # the crash record the notice bar tells about
        self._live_record = ""  # start of the record of the game running now (from the last collect)
        self._collect_lock = threading.Lock()  # one copy of the game's records at a time
        self._missing: dict[str, list[dict]] = {}  # enabled mods' missing requirements (from the last refresh)
        # 실행 전 점검: one run of its slow part at a time (they share its cache file), how many are running,
        # one of them checking materials (the mods' material check waits for it: both read the game's
        # files), the launch waiting for it
        self._precheck_lock = threading.Lock()
        self._precheck_runs = 0
        self._precheck_reading = False
        self._precheck_waiting = False
        self._prechecking = False
        self._precheck_answered = None  # report.signature() the user just answered 그래도 실행 to
        self._unapplied_ok = None  # manager.revision the user agreed to start the game with, not applied
        self._precheck_menu = False
        # 원인 모드 찾기 (bisect.json): the search in progress, its 또 튕겼나요? window, how the step's game ended
        self._bisect = bisect.load(manager.data_dir)
        self._bisect_resume = self._bisect is not None  # picked up again by _startup (not one started since)
        self._bisect_preparing = False
        self._bisect_asking = False
        self._bisect_computing = False  # its next launch is being worked out on another thread
        self._bisect_detection: tuple | None = None  # (launch time of the step, gamewatch.Detection)
        # the game process: how it ends (exit code) after a launch from here or when it runs at startup
        self.watcher = gamewatch.GameWatcher(self._on_game_exit, events=events, game_dir=manager.game_dir,
                                             on_lost=self._on_watch_lost)
        self._watch_launched: float | None = None  # launch time of the game being watched (None: found running)
        self._watch_lost: float | None = None  # launch time of a game the watcher gave up on (not found)

        self.theme = Theme(root, manager.settings.theme)
        self.theme.mark(root, "window")
        self.theme.use(self.theme.name)
        px = self.theme.px
        root.title(f"{APP_TITLE} {__version__}")
        root.geometry(f"{px(1240)}x{px(800)}")
        root.minsize(px(980), px(620))
        self._restore_window()
        self._set_icon()

        # everything in the window is in one frame: it keeps its size while the window's edge is dragged
        self.content = ttk.Frame(root)
        self.freeze = FreezeContent(root, self.content)
        self._build_header()
        self._build_toolbar()
        self._build_statusbar()
        self._build_log()
        self._build_main()
        self._build_notice()
        self._build_bisect_bar()
        root.update_idletasks()
        root.minsize(max(px(980), self.toolbar.winfo_reqwidth()), px(620))  # the whole toolbar stays visible
        self._configure_tags()
        self._bind_keys()
        self._setup_dnd()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        set_dark_titlebar(root, self.theme.name == "dark")
        if not manager.settings.log_visible:
            self.toggle_log()

        manager.link_from_sources()  # names an older version could not read
        self.refresh()
        self._update_search_hint()
        root.after(60, self._place_sash)
        root.after(100, self._poll_events)
        root.after(300, self._startup)
        if manager.settings.check_updates:
            root.after(2500, lambda: self.check_updates(manual=False))
        if manager.settings.check_refw_updates:
            root.after(3500, lambda: self.check_refw(manual=False))
        if manager.settings.check_mod_updates and manager.settings.nexus_api_key:
            root.after(1500, lambda: self.check_mod_updates(manual=False))  # reads requirements too
        else:
            root.after(2000, self._fetch_unread_requirements)
        if manager.settings.check_materials:
            root.after(4000, self.check_materials_background)
        root.after(9000, self.warm_equipment)

    def _place_sash(self):
        width = self.paned.winfo_width()
        s = self.manager.settings
        ratio = min(0.8, max(0.3, s.sash_ratio)) if s.remember_window else 0.58
        if width > 1:
            self.paned.sashpos(0, int(width * ratio))
            # The mod list's columns fit the list from here on. ttk keeps a change of the list's width as "slack"
            # until the list is wider or narrower than its columns, so the columns depended on the widths the list
            # went through (a frozen drag skips them: wider and back left 파일/충돌 cut off); setting a column's
            # width on a laid out list zeroes the slack, and each layout after that fits the columns.
            self.root.update_idletasks()
            self.tree.column("name", width=self.tree.column("name", "width"))

    def _restore_window(self):
        s = self.manager.settings
        if not s.remember_window or not s.window_geometry:
            return
        m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", s.window_geometry)
        if not m:
            return
        min_w, min_h = self.root.minsize()
        w, h, x, y = max(int(m[1]), min_w), max(int(m[2]), min_h), int(m[3]), int(m[4])
        left, top, right, bottom = _virtual_screen(self.root)
        geometry = f"{w}x{h}"
        if left - 50 <= x <= right - 120 and top - 10 <= y <= bottom - 80:  # monitor still there
            geometry += f"+{x}+{y}"
        self.root.geometry(geometry)
        if s.window_zoomed:
            try:
                self.root.state("zoomed")
            except tk.TclError:
                pass

    def _save_window_state(self):
        s = self.manager.settings
        try:
            if self.freeze.thaw():  # (closed right after a drag: the panes as they are for the window's size)
                self.root.update_idletasks()
            changes: dict = {"log_visible": self._log_visible}
            if s.remember_window:
                zoomed = self.root.state() == "zoomed"
                changes["window_zoomed"] = zoomed
                if not zoomed:
                    changes["window_geometry"] = self.root.geometry()
                width = self.paned.winfo_width()
                if width > 1:
                    changes["sash_ratio"] = round(min(0.8, max(0.3, self.paned.sashpos(0) / width)), 3)
            self.manager.update_settings(**changes)
        except (tk.TclError, OSError, ValueError) as exc:
            log.debug("창 상태를 저장하지 못했습니다: %s", exc)

    # ------------------------------------------------------------------ layout
    def _set_icon(self):
        try:
            self.root.iconphoto(True, self.theme.image("logo", 64, exact=True), self.theme.image("logo", 32, exact=True))
        except tk.TclError:
            pass

    def _build_header(self):
        t, px = self.theme, self.theme.px
        header = ttk.Frame(self.content, style="Header.TFrame", padding=(px(20), px(12)))
        header.pack(fill="x")
        ttk.Separator(self.content).pack(fill="x")

        ttk.Label(header, image=t.image("logo", 40), style="Header.TLabel").pack(side="left")
        titles = ttk.Frame(header, style="Header.TFrame")
        titles.pack(side="left", padx=(px(12), 0))
        ttk.Label(titles, text="MONSTER HUNTER WILDS", style="Eyebrow.TLabel").pack(anchor="w")
        ttk.Label(titles, text="모드 매니저", style="Title.TLabel").pack(anchor="w")

        # the widgets on the right in one frame: when the window gets wider or narrower only the frame moves
        # (each widget moved is laid out and drawn again on every step of the drag)
        right = ttk.Frame(header, style="Header.TFrame")
        right.pack(side="right", fill="y")
        menu_button = ttk.Menubutton(right, text="☰", style="Ghost.TMenubutton")
        menu_button.pack(side="right")
        self.main_menu = self._build_main_menu(menu_button)
        menu_button.configure(menu=self.main_menu)
        Tooltip(t, menu_button, "메뉴")
        gear = ttk.Button(right, text="⚙", style="Ghost.TButton", command=self.open_settings)
        gear.pack(side="right", padx=(0, px(2)))
        Tooltip(t, gear, "설정")
        ttk.Separator(right, orient="vertical").pack(side="right", fill="y", padx=px(12), pady=px(4))
        launch = ttk.Button(right, text="▶  게임 실행", command=self.launch_game)
        launch.pack(side="right")
        Tooltip(t, launch, "Steam으로 몬스터 헌터 와일즈 실행")

        self.game_button = ttk.Menubutton(right, text="", style="Chip.TMenubutton")
        self.game_button.pack(side="right", padx=(0, px(8)))
        self.game_menu = t.mark(tk.Menu(self.game_button, tearoff=False, postcommand=self._fill_game_menu), "menu")
        self.game_button.configure(menu=self.game_menu)
        Tooltip(t, self.game_button, lambda: str(self.manager.game_dir) if self.manager.game_dir
                else "MonsterHunterWilds.exe가 있는 폴더를 지정하세요")

        self.refw_badge = ttk.Label(right, text="", style="BadgeNeutral.TLabel", cursor="hand2")
        self.refw_badge.pack(side="right", padx=(0, px(10)))
        self.refw_badge.bind("<Button-1>", lambda e: self._on_refw_click())
        Tooltip(t, self.refw_badge, self._refw_tooltip)
        # Nexus mod updates: shown while checking, with the result, or when the API key is missing
        self.mods_badge = ttk.Label(right, text="", style="BadgeNeutral.TLabel", cursor="hand2")
        self.mods_badge.bind("<Button-1>", lambda e: self._on_mods_badge_click())
        Tooltip(t, self.mods_badge, self._mods_tooltip)
        # shown once a newer release is found
        self.update_button = ttk.Button(right, text="", style="Update.TButton", command=self.show_update)
        Tooltip(t, self.update_button, "새 버전의 변경 내역을 보고 업데이트합니다")

    def _build_main_menu(self, parent) -> tk.Menu:
        m = self.theme.mark(tk.Menu(parent, tearoff=False), "menu")
        m.add_command(label="모드 파일 추가...", accelerator="Ctrl+O", command=self.add_files)
        m.add_command(label="폴더에서 모드 추가...", command=self.add_folder)
        m.add_separator()
        m.add_command(label="게임에 적용", accelerator="Ctrl+S", command=self.apply)
        m.add_command(label="적용 상태 점검", command=self.verify)
        m.add_command(label=crashes.TITLE, command=self.show_crashes)
        m.add_command(label=bisect.TITLE, command=self.show_bisect)
        m.add_command(label=precheck.TITLE, command=self.show_precheck)
        m.add_command(label="충돌 한눈에 보기", command=self.show_conflicts)
        m.add_command(label="장비 외형 보기", command=lambda: self.show_equipment())
        m.add_command(label="모든 모드 재질 점검", command=self.check_materials)
        m.add_command(label="옵션 그림 다시 찾기 (그림 없는 모드)", command=self.read_all_option_pictures)
        m.add_command(label="게임 폴더에서 모든 모드 파일 제거", command=self.purge)
        m.add_separator()
        m.add_command(label="모두 활성화", command=lambda: self.set_enabled_all(True))
        m.add_command(label="모두 비활성화", command=lambda: self.set_enabled_all(False))
        m.add_separator()
        m.add_command(label="데이터 폴더 열기", command=lambda: fsutil.open_in_explorer(self.manager.data_dir))
        m.add_command(label="REFramework 릴리즈 페이지", command=lambda: webbrowser.open(REFRAMEWORK_URL))
        m.add_command(label="Nexus Mods (몬헌 와일즈)", command=lambda: webbrowser.open(NEXUS_URL))
        m.add_separator()
        m.add_command(label="업데이트 확인", command=lambda: self.check_updates(manual=True))
        m.add_command(label="REFramework 업데이트 확인", command=lambda: self.check_refw(manual=True))
        m.add_command(label="모드 업데이트 확인 (Nexus)", command=lambda: self.check_mod_updates(manual=True))
        m.add_command(label="사용법", accelerator="F1", command=self.show_help)
        m.add_command(label="정보", command=self.show_about)
        m.add_separator()
        m.add_command(label="종료", command=self.on_close)
        return m

    def _build_toolbar(self):
        t, px = self.theme, self.theme.px
        bar = self.toolbar = ttk.Frame(self.content, padding=(px(20), px(14), px(20), px(2)))
        bar.pack(fill="x")
        self.buttons: dict[str, ttk.Button] = {}

        def button(key, text, command, style="TButton", tip=None, side="left", padx=None, parent=bar):
            b = ttk.Button(parent, text=text, command=command, style=style)
            b.pack(side=side, padx=padx if padx is not None else (0, px(6)))
            self.buttons[key] = b
            if tip:
                Tooltip(t, b, tip)
            return b

        button("add", "＋  모드 추가", self.add_files, "Accent.TButton", "zip · 7z · rar · pak 파일 추가 (Ctrl+O)")
        button("folder", "폴더 추가", self.add_folder, tip="압축을 풀어 둔 모드 폴더 추가", padx=(0, px(14)))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=(0, px(14)), pady=px(4))
        button("up", "▲", lambda: self.move_selected(-1), "Compact.TButton", "위로 (Ctrl+↑)")
        button("down", "▼", lambda: self.move_selected(1), "Compact.TButton", "아래로 · 우선순위 높이기 (Ctrl+↓)")
        button("rename", "이름 변경", self.rename_selected, tip="F2")
        button("remove", "삭제", self.remove_selected, tip="Delete", padx=(0, px(14)))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=(0, px(14)), pady=px(4))
        button("equipment", "장비 외형", lambda: self.show_equipment(), tip="모드가 어떤 장비의 모습을 바꾸는지 보기")

        # the widgets on the right in one frame, as in the header (made after the ones on the left: Tab goes
        # through widgets in the order they were made)
        right = ttk.Frame(bar)
        right.pack(side="right", fill="y")
        button("apply", "✔  게임에 적용", self.apply, side="right", padx=(px(10), 0), tip="Ctrl+S", parent=right)
        self.auto_var = tk.BooleanVar(value=self.manager.settings.auto_apply)
        switch = ttk.Checkbutton(right, text="  즉시 적용", variable=self.auto_var, command=self._toggle_auto_apply,
                                 style="Switch.TCheckbutton", compound="left",
                                 image=(t.image("switch_off", 20), "selected", t.image("switch_on", 20)))
        switch.pack(side="right")
        Tooltip(t, switch, "켜 두면 모드를 켜고 끄거나 순서를 바꿀 때 바로 게임 폴더에 반영합니다")
        self.profile_var = tk.StringVar(value=self.manager.profile)
        self.profile_button = ttk.Menubutton(right, text="", style="Chip.TMenubutton")
        self.profile_button.pack(side="right", padx=(0, px(14)))
        self.profile_menu = t.mark(tk.Menu(self.profile_button, tearoff=False, postcommand=self._fill_profile_menu),
                                   "menu")
        self.profile_button.configure(menu=self.profile_menu)
        Tooltip(t, self.profile_button, "프로필마다 켜 둔 모드, 옵션, 로드 순서를 따로 저장합니다")

    def _build_statusbar(self):
        px = self.theme.px
        self.status_frame = ttk.Frame(self.content, style="Status.TFrame", padding=(px(20), px(7)))
        self.status_frame.pack(side="bottom", fill="x")
        self.status_sep = ttk.Separator(self.content)
        self.status_sep.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="준비")
        ttk.Label(self.status_frame, textvariable=self.status_var, style="StatusMuted.TLabel").pack(side="left")
        right = ttk.Frame(self.status_frame, style="Status.TFrame")  # (in one frame, as in the header)
        right.pack(side="right", fill="y")
        self.state_badge = ttk.Label(right, text="", style="BadgeOk.TLabel")
        self.state_badge.pack(side="right")
        self.log_button = ttk.Button(right, text="로그 숨기기", style="Link.TButton", command=self.toggle_log)
        self.log_button.pack(side="right", padx=(0, px(10)))
        self.progress = ttk.Progressbar(right, length=px(220), mode="determinate",
                                        style="Accent.Horizontal.TProgressbar")

    def _build_log(self):
        t, px = self.theme, self.theme.px
        self.log_frame = ttk.Frame(self.content, padding=(px(20), 0, px(20), px(12)))
        self.log_frame.pack(side="bottom", fill="x")
        card = t.mark(tk.Frame(self.log_frame), "card")
        card.pack(fill="x")
        self.log_text = t.mark(tk.Text(card, height=5, wrap="word", font=t.fonts["small"], padx=px(14),
                                       pady=px(8), cursor="arrow"), "log")
        lsb = ttk.Scrollbar(card, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=autohide(lsb, side="right", fill="y", padx=(0, px(3)), pady=px(3)),
                                state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)

    def _build_notice(self):
        """A bar under the toolbar, shown when the last game ended in a crash (it does not block anything)."""
        t, px = self.theme, self.theme.px
        self.notice = ttk.Frame(self.content, style="Callout.TFrame", padding=(px(16), px(8), px(8), px(8)))
        self.notice_title = ttk.Label(self.notice, text="", style="CalloutTitle.TLabel")
        self.notice_title.pack(side="left")
        self.notice_detail = ttk.Label(self.notice, text="", style="Callout.TLabel")
        self.notice_detail.pack(side="left", padx=(px(12), 0))
        close = ttk.Button(self.notice, text="✕", style="CalloutClose.TButton", command=self._hide_notice)
        close.pack(side="right")
        Tooltip(t, close, "알림 닫기 (기록은 ☰ → 게임 튕김 기록에서 다시 볼 수 있습니다)")
        self.notice_button = ttk.Button(self.notice, text="기록 보기", style="Update.TButton",
                                        command=lambda: self.show_crashes(self._notice_session))
        self.notice_button.pack(side="right", padx=(0, px(6)))

    def _build_bisect_bar(self):
        """The bar under the toolbar while 원인 모드 찾기 runs: the step, starting the game, stopping."""
        px = self.theme.px
        bar = self.bisect_bar = ttk.Frame(self.content, style="Banner.TFrame",
                                          padding=(px(16), px(10), px(10), px(10)))
        buttons = ttk.Frame(bar, style="Banner.TFrame")
        buttons.pack(side="right", padx=(px(12), 0))
        text = ttk.Frame(bar, style="Banner.TFrame")
        text.pack(side="left", fill="x", expand=True)
        self.bisect_title = ttk.Label(text, text="", style="BannerTitle.TLabel")
        self.bisect_title.pack(anchor="w")
        self.bisect_text = ttk.Label(text, text="", style="Banner.TLabel", justify="left")
        self.bisect_text.pack(anchor="w", fill="x", pady=(px(2), 0))
        wrap = [None]

        def follow(event):  # (only when the width changed: each new wraplength lays the window out again)
            width = max(px(200), event.width - px(4))
            if width != wrap[0]:
                wrap[0] = width
                self.bisect_text.configure(wraplength=width)

        text.bind("<Configure>", follow)
        self.bisect_main = ttk.Button(buttons, text="", style="Update.TButton")
        self.bisect_second = ttk.Button(buttons, text="", style="BannerGhost.TButton")
        self.bisect_stop = ttk.Button(buttons, text=bisect.BUTTON_STOP, style="BannerGhost.TButton",
                                      command=self.stop_bisect)

    def _build_main(self):
        t, px = self.theme, self.theme.px
        paned = self.paned = ttk.Panedwindow(self.content, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=px(20), pady=(px(10), px(12)))

        # ---- mod list card
        left = t.mark(tk.Frame(paned), "card")
        head = ttk.Frame(left, style="Card.TFrame", padding=(px(18), px(14), px(14), px(2)))
        head.pack(fill="x")
        ttk.Label(head, text="설치된 모드", style="Section.TLabel").pack(side="left")
        self.count_label = ttk.Label(head, text="", style="CardMuted.TLabel")
        self.count_label.pack(side="left", padx=(px(10), 0))
        # shown when needed, between the title and the count (packed before the count so they always fit)
        self.problems_button = ttk.Button(head, text="", style="Problem.TButton", command=self.show_problems)
        Tooltip(t, self.problems_button, "제대로 작동하지 않는 모드만 보기 (옵션 미선택 · 필요한 모드 없음 · 전부 밀림)")
        self.conflicts_button = ttk.Button(head, text="", style="Conflict.TButton", command=self.show_conflicts)
        Tooltip(t, self.conflicts_button, "어떤 모드끼리 어떤 파일을 함께 바꾸는지 한눈에 봅니다")
        self.search_var = tk.StringVar()
        right = ttk.Frame(head, style="Card.TFrame")  # (in one frame, as in the header; before the hint over it)
        right.pack(side="right", fill="y")
        self.search_entry = ttk.Entry(right, textvariable=self.search_var, width=24)
        self.search_entry.pack(side="right")
        self.search_hint = ttk.Label(head, text="⌕  모드 검색  (Ctrl+F)", style="FieldHint.TLabel", cursor="xterm")
        self.search_hint.bind("<Button-1>", lambda e: self.search_entry.focus_set())
        self.search_entry.bind("<FocusIn>", lambda e: self._update_search_hint(), add="+")
        self.search_entry.bind("<FocusOut>", lambda e: self._update_search_hint(), add="+")
        self.search_entry.bind("<Escape>", lambda e: self.search_var.set(""))
        self.search_var.trace_add("write", lambda *a: (self._update_search_hint(), self.refresh()))
        self.filter_var = tk.StringVar(value=FILTER_ALL)
        filter_box = ttk.Combobox(right, textvariable=self.filter_var, values=list(FILTERS), state="readonly",
                                  width=12)
        filter_box.pack(side="right", padx=(0, px(8)))
        filter_box.bind("<<ComboboxSelected>>", lambda e: (filter_box.selection_clear(), self.refresh()))
        Tooltip(t, filter_box, "목록에 보일 모드 고르기")
        hint = ttk.Frame(left, style="Card.TFrame", padding=(px(18), px(2), px(14), px(10)))
        hint.pack(fill="x")
        self.hint_label = ttk.Label(hint, text="아래쪽 모드일수록 우선 적용 · 드래그로 순서 변경",
                                    style="Hint.TLabel")
        self.hint_label.pack(side="left")
        # explains the row colours while the selected mod has conflicts
        self.legend = ttk.Frame(hint, style="Card.TFrame")
        ttk.Label(self.legend, text="선택한 모드를 덮어씀", style="LegendOver.TLabel").pack(side="left")
        ttk.Label(self.legend, text="선택한 모드에 밀림", style="LegendInfo.TLabel").pack(side="left", padx=(px(6), 0))

        area = ttk.Frame(left, style="Card.TFrame")
        area.pack(fill="both", expand=True, padx=px(6), pady=(0, px(6)))
        cols = ("order", "name", "kind", "version", "options", "files", "conflict")
        self.tree = ttk.Treeview(area, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=px(46), minwidth=px(46), stretch=False)
        for col, text, width, anchor, stretch in (
            ("order", "#", 34, "center", False),
            ("name", "이름", 190, "w", True),
            ("kind", "종류", 140, "w", False),
            ("version", "버전", 78, "w", False),
            ("options", "옵션", 70, "center", False),
            ("files", "파일", 46, "e", False),
            ("conflict", "충돌", 118, "w", False),
        ):
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=px(width), anchor=anchor, stretch=stretch)
        ysb = ttk.Scrollbar(area, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=autohide(ysb, side="right", fill="y"))
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: (self._update_details(), self._highlight_related()))
        self.tree.bind("<ButtonPress-1>", self._on_press)
        self.tree.bind("<B1-Motion>", self._on_motion)
        self.tree.bind("<ButtonRelease-1>", self._on_release)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Button-3>", self._on_context)
        RowTooltip(t, self.tree, self._row_tooltip)
        self.tree.bind("<Button-2>", self._on_context)

        self.empty = ttk.Frame(area, style="Card.TFrame")
        ttk.Label(self.empty, text="＋", style="Display.TLabel").pack()
        self.empty_title = ttk.Label(self.empty, text="", style="EmptyTitle.TLabel")
        self.empty_title.pack(pady=(px(4), px(4)))
        self.empty_sub = ttk.Label(self.empty, text="", style="CardMuted.TLabel", justify="center")
        self.empty_sub.pack()
        self.empty_button = ttk.Button(self.empty, text="＋  모드 추가", style="Accent.TButton", command=self.add_files)
        paned.add(left, weight=3)

        # ---- details card
        right = t.mark(tk.Frame(paned), "card")
        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True, padx=px(2), pady=(0, px(2)))
        self.notebook.bind("<ButtonPress-1>", self._on_tab_click, add="+")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed, add="+")

        info = ttk.Frame(self.notebook, style="Card.TFrame")
        self.preview_label = ttk.Label(info, style="Card.TLabel", anchor="center", cursor="hand2")
        self.preview_label.bind("<Button-1>", lambda e: self._preview_path and self.show_picture(*self._preview_path))
        self.info_text = t.mark(tk.Text(info, wrap="word", font=t.fonts["base"], padx=px(18), pady=px(14),
                                        height=10, cursor="arrow"), "text")
        self.info_text.pack(fill="both", expand=True)
        self.info_text.configure(state="disabled", tabs=(px(84),))
        self.notebook.add(info, text="정보")

        self.options_tab = ttk.Frame(self.notebook, style="Card.TFrame")
        # title and "모두 켜기/끄기" stay at the top while the options scroll below
        self.options_head = ttk.Frame(self.options_tab, style="Card.TFrame", padding=(px(18), px(14), px(18), px(6)))
        self.options_head.pack(side="top", fill="x")
        self.options_canvas = t.mark(tk.Canvas(self.options_tab), "canvas")
        osb = ttk.Scrollbar(self.options_tab, orient="vertical", command=self.options_canvas.yview)
        self.options_canvas.configure(yscrollcommand=autohide(osb, side="right", fill="y"))
        self.options_canvas.pack(side="left", fill="both", expand=True)
        self.options_inner = ttk.Frame(self.options_canvas, style="Card.TFrame",
                                       padding=(px(18), 0, px(18), px(14)))
        window = self.options_canvas.create_window((0, 0), window=self.options_inner, anchor="nw")
        self.options_inner.bind("<Configure>", lambda e: self.options_canvas.configure(
            scrollregion=self.options_canvas.bbox("all")))
        follow_width(self.options_canvas, window)  # the cards as wide as the tab
        self._option_vars: list[tuple[str, tk.BooleanVar]] = []
        self.notebook.add(self.options_tab, text="옵션", state="hidden")

        files = ttk.Frame(self.notebook, style="Card.TFrame", padding=(px(6), px(8), px(6), px(6)))
        self.files_tree = ttk.Treeview(files, columns=("path", "status"), show="headings")
        self.files_tree.heading("path", text="게임 폴더 기준 경로", anchor="w")
        self.files_tree.heading("status", text="상태", anchor="w")
        self.files_tree.column("path", width=px(300))
        self.files_tree.column("status", width=px(190))
        fsb = ttk.Scrollbar(files, orient="vertical", command=self.files_tree.yview)
        self.files_tree.configure(yscrollcommand=autohide(fsb, side="right", fill="y"))
        self.files_tree.pack(side="left", fill="both", expand=True)
        self.notebook.add(files, text="파일")

        conflicts = ttk.Frame(self.notebook, style="Card.TFrame")
        self.conflict_text = t.mark(tk.Text(conflicts, wrap="word", font=t.fonts["base"], padx=px(18),
                                            pady=px(14), cursor="arrow"), "text")
        self.conflict_text.pack(fill="both", expand=True)
        self.conflict_text.configure(state="disabled")
        self.notebook.add(conflicts, text="충돌")
        self.conflicts_tab = conflicts
        paned.add(right, weight=2)

        self.context_menu = t.mark(tk.Menu(self.tree, tearoff=False), "menu")

    def _configure_tags(self):
        p, f, px = self.theme.p, self.theme.fonts, self.theme.px
        # mods that override / are overridden by the selected mod; created before "odd" because
        # ttk gives the earliest created tag priority
        self.tree.tag_configure("over_sel", background=p["over_bg"])
        self.tree.tag_configure("under_sel", background=p["info_bg"])
        # enabled mods that cannot work as they are (also before "odd": it is a background)
        self.tree.tag_configure("problem", background=p["problem_bg"], foreground=p["danger"])
        for tree in (self.tree, self.files_tree):
            tree.tag_configure("odd", background=p["row_alt"])
            tree.tag_configure("disabled", foreground=p["subtle"])
            tree.tag_configure("overridden", foreground=p["warn"])
            tree.tag_configure("pending", foreground=p["subtle"])
            tree.tag_configure("attention", foreground=p["warn"])
            tree.tag_configure("update", foreground=p["info"])
        info = self.info_text
        info.tag_configure("title", font=f["detail_title"], spacing3=px(8))
        info.tag_configure("chip", background=p["chip"], foreground=p["chip_text"], font=f["small_bold"])
        info.tag_configure("chip_ok", background=p["success_bg"], foreground=p["success"], font=f["small_bold"])
        info.tag_configure("chip_off", background=p["raised"], foreground=p["muted"], font=f["small_bold"])
        info.tag_configure("chip_warn", background=p["warn_bg"], foreground=p["warn"], font=f["small_bold"])
        info.tag_configure("chip_info", background=p["info_bg"], foreground=p["info"], font=f["small_bold"])
        info.tag_configure("key", foreground=p["muted"], spacing1=px(3), spacing3=px(3))
        info.tag_configure("value", foreground=p["text"], spacing1=px(3), spacing3=px(3))
        info.tag_configure("muted", foreground=p["muted"])
        info.tag_configure("desc", spacing1=px(4), spacing2=px(3))
        for tag, fg_key, bg_key in (("warn", "warn", "warn_bg"), ("note", "info", "info_bg")):
            info.tag_configure(tag, background=p[bg_key], foreground=p[fg_key], lmargin1=px(12), lmargin2=px(12),
                               rmargin=px(12), spacing1=px(8), spacing3=px(8))
            try:
                info.tag_configure(tag, lmargincolor=p[bg_key], rmargincolor=p[bg_key])
            except tk.TclError:  # Tk < 8.6.6
                pass
        info.tag_configure("warn_title", font=f["bold"])
        info.tag_configure("link", foreground=p["info"], underline=True)
        for tag, color in (("req_ok", "success"), ("req_missing", "danger"), ("req_off", "warn"),
                           ("req_unknown", "muted")):
            info.tag_configure(tag, foreground=p[color], font=f["bold"])
        for tag, action in (("nexus_page", lambda m: m.nexus_url), ("nexus_files", lambda m: nexus.FILES_URL.format(m.nexus_id))):
            info.tag_bind(tag, "<Button-1>", lambda e, action=action: self._open_nexus(action))
        info.tag_bind("nexus_set", "<Button-1>", lambda e: self.link_nexus())
        info.tag_bind("equip_show", "<Button-1>",
                      lambda e: self.show_equipment(self._selected_mod().id if self._selected_mod() else None))
        info.tag_bind("nexus_search", "<Button-1>", lambda e: self._search_nexus())
        for action in ("check", "repair", "restore", "report"):
            info.tag_bind(f"matfix_{action}", "<Button-1>", lambda e, action=action: self._material_action(action))
        info.tag_bind("link", "<Enter>", lambda e: info.configure(cursor="hand2"))
        info.tag_bind("link", "<Leave>", lambda e: info.configure(cursor="arrow"))
        ct = self.conflict_text
        ct.tag_configure("title", font=f["detail_title"], spacing3=px(8))
        ct.tag_configure("head", font=f["bold"], spacing1=px(10), spacing3=px(4))
        ct.tag_configure("row", spacing1=px(2))
        ct.tag_configure("dim", foreground=p["muted"], font=f["small"])
        ct.tag_configure("muted", foreground=p["muted"])
        ct.tag_configure("warn", foreground=p["warn"])
        ct.tag_configure("ok", foreground=p["success"])
        self.log_text.tag_configure("time", foreground=p["subtle"])
        self.log_text.tag_configure("warn", foreground=p["warn"])
        self.log_text.tag_configure("error", foreground=p["danger"])
        self.log_text.tag_configure("info", foreground=p["text"])

    def apply_theme(self, name: str):
        self.theme.use(name)
        self._configure_tags()
        set_dark_titlebar(self.root, name == "dark")
        if self.equipment_window is not None:
            set_dark_titlebar(self.equipment_window, name == "dark")
            self.equipment_window.render()  # its icons take the theme's colour
        self._update_search_hint()
        self.refresh()

    def _bind_keys(self):
        self.root.bind("<Control-o>", lambda e: self.add_files())
        self.root.bind("<Control-s>", lambda e: self.apply())
        self.root.bind("<Control-f>", lambda e: (self.search_entry.focus_set(), "break")[1])
        self.root.bind("<F5>", lambda e: self.refresh())
        self.root.bind("<F1>", lambda e: self.show_help())
        self.tree.bind("<space>", lambda e: (self.toggle(self._selected_ids()), "break")[1])
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<F2>", lambda e: self.rename_selected())
        self.tree.bind("<Control-Up>", lambda e: (self.move_selected(-1), "break")[1])
        self.tree.bind("<Control-Down>", lambda e: (self.move_selected(1), "break")[1])

    def _setup_dnd(self):
        if TkinterDnD is None or not hasattr(self.tree, "drop_target_register"):
            return
        try:
            for widget in (self.tree, self.empty):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as exc:  # pragma: no cover - depends on tkdnd install
            log.debug("드래그 앤 드롭을 사용할 수 없습니다: %s", exc)

    def _on_drop(self, event):
        paths = list(self.root.tk.splitlist(event.data))
        if paths:
            self.add_paths(paths)
        return event.action

    # ------------------------------------------------------------------ state display
    def _query(self) -> str:
        return self.search_var.get().strip().casefold()

    def _update_search_hint(self):
        if self.search_var.get() or self.root.focus_get() is self.search_entry:
            self.search_hint.place_forget()
        else:
            self.search_hint.place(in_=self.search_entry, x=self.theme.px(10), rely=0.5, anchor="w")

    def refresh(self, select: str | None = None):
        m = self.manager
        t = self.theme
        self._conflicts = m.conflicts()
        # enabled mods that need other mods which are missing or switched off
        missing = {mod.id: m.missing_requirements(mod.id) for mod in m.mods if mod.enabled}
        self._missing = missing = {mod_id: reqs for mod_id, reqs in missing.items() if reqs}
        self._problems = m.problems(self._conflicts, missing)
        query = self._query()
        keep = FILTERS.get(self.filter_var.get())
        if self.filter_var.get() == FILTER_MISSING:
            keep = lambda mod, conflicts: mod.id in missing  # noqa: E731
        elif self.filter_var.get() == FILTER_PROBLEMS:
            keep = lambda mod, conflicts: mod.id in self._problems  # noqa: E731
        visible = [(i, mod) for i, mod in enumerate(m.mods)
                   if (not query or query in mod.name.casefold() or query in mod.author.casefold())
                   and (keep is None or keep(mod, self._conflicts))]
        existing = set(self.tree.get_children())
        on, off = t.image("check_on", 20), t.image("check_off", 20)
        for pos, (index, mod) in enumerate(visible):
            wins, losses = self._conflicts.counts(mod.id)
            files = m.mod_files(mod.id)
            conflict = " · ".join(x for x in (f"덮어씀 {wins}" if wins else "", f"밀림 {losses}" if losses else "") if x)
            tags = ["odd"] if pos % 2 else []
            if not mod.enabled:
                tags.append("disabled")
            elif mod.id in self._problems:
                tags.append("problem")
            elif mod.update:
                tags.append("update")
            options = ""
            if mod.options:
                options = "선택 필요" if mod.needs_option else f"{len(mod.selected)}/{len(mod.options)}"
            version = mod.version
            if mod.update:
                version = f"{mod.version or '?'} → {mod.update.get('version') or '새 파일'}"
            name = f"⚠ {mod.name}" if mod.id in self._problems else mod.name
            values = (index + 1, name, kinds_short(self._display_kinds(mod)), version, options,
                      len(files), conflict)
            image = on if mod.enabled else off
            if mod.id in existing:
                self.tree.item(mod.id, image=image, values=values, tags=tags)
                if self.tree.index(mod.id) != pos:
                    self.tree.move(mod.id, "", pos)
                existing.discard(mod.id)
            else:
                self.tree.insert("", pos, iid=mod.id, text="", image=image, values=values, tags=tags)
        for iid in existing:
            self.tree.delete(iid)
        if select and self.tree.exists(select):
            self.tree.selection_set(select)
            self.tree.see(select)

        enabled = sum(1 for mod in m.mods if mod.enabled)
        updates = sum(1 for mod in m.mods if mod.update)
        count = f"{len(m.mods)}개 · 활성 {enabled}개" + (f" · 업데이트 {updates}개" if updates else "")
        self.count_label.configure(text=count if m.mods else "")
        pairs = len(self._conflicts.pairs())
        self.problems_button.configure(text=f"⚠ 문제 {len(self._problems)}개")
        self.conflicts_button.configure(text=f"충돌 {pairs}쌍 보기 ›")
        pills = ((self.problems_button, bool(self._problems)), (self.conflicts_button, bool(pairs)))
        for button, _ in pills:
            button.pack_forget()
        for button, show in pills:
            if show:
                button.pack(side="left", padx=(self.theme.px(8), 0), before=self.count_label)
        self._highlight_related()
        if self.equipment_window is not None:
            self.equipment_window.mods_changed()
        if not m.mods:
            self.empty_title.configure(text="아직 설치된 모드가 없습니다")
            self.empty_sub.configure(text="Nexus Mods 등에서 받은 zip · 7z · rar · pak 파일을\n이곳에 끌어 놓거나 아래 버튼을 누르세요.")
            self.empty_button.pack(pady=(self.theme.px(16), 0))
            self.empty.place(relx=0.5, rely=0.42, anchor="center")
        elif not visible:
            conditions = [f"검색어 '{self.search_var.get().strip()}'"] if query else []
            if keep is not None:
                conditions.append(f"'{self.filter_var.get()}' 필터")
            self.empty_title.configure(text="조건에 맞는 모드가 없습니다")
            self.empty_sub.configure(text=", ".join(conditions) + "에 해당하는 모드가 없습니다.")
            self.empty_button.pack_forget()
            self.empty.place(relx=0.5, rely=0.42, anchor="center")
        else:
            self.empty.place_forget()
        self._update_header()
        self._update_details()

    def _display_kinds(self, mod: Mod) -> set[str]:
        """Kinds of the files a mod deploys, or of all its options while none is chosen."""
        if mod.needs_option:
            kinds: set[str] = set()
            for opt in mod.options:
                kinds |= classify(self.manager.option_files(mod.id, opt["id"]))
            return kinds
        return self.manager.mod_kinds(mod.id)

    def _update_header(self):
        m = self.manager
        if m.game_dir:
            self.game_button.configure(text=f"⌂  {shorten_middle(m.game_dir.name, 28)}")
        else:
            self.game_button.configure(text="⌂  게임 폴더 지정")
        status = self._refw_status
        if not m.game_dir:
            self.refw_badge.configure(text="● REFramework", style="BadgeNeutral.TLabel")
        elif m.refw_installed() and status is not None and status.installed and not status.up_to_date:
            self.refw_badge.configure(text="● REFramework 업데이트", style="BadgeWarn.TLabel")
        elif m.refw_installed():
            latest = status is not None and status.up_to_date
            self.refw_badge.configure(text="● REFramework 최신" if latest else "● REFramework 설치됨",
                                      style="BadgeOk.TLabel")
        else:
            needed = m.needs_refw()
            self.refw_badge.configure(text="● REFramework 없음",
                                      style="BadgeBad.TLabel" if needed else "BadgeWarn.TLabel")
        if not m.game_dir:
            self.state_badge.configure(text="● 게임 폴더 미설정", style="BadgeBad.TLabel")
        elif m.dirty:
            self.state_badge.configure(text="● 적용 대기 중", style="BadgeWarn.TLabel")
        else:
            self.state_badge.configure(text="● 게임에 적용됨", style="BadgeOk.TLabel")
        self.buttons["apply"].configure(style="Accent.TButton" if m.dirty and m.game_dir else "TButton")
        self.profile_button.configure(text=f"프로필 · {shorten_middle(m.profile, 16)}")
        self._update_mods_badge()

    def _update_mods_badge(self):
        m, s = self.manager, self.manager.settings
        updates = sum(1 for mod in m.mods if mod.update)
        state = None
        if self._checking_mods:
            state = ("● 모드 업데이트 확인 중", "BadgeNeutral.TLabel")
        elif updates:
            state = (f"● 모드 업데이트 {updates}개", "BadgeInfo.TLabel")
        elif not s.check_mod_updates:
            state = None
        elif not s.nexus_api_key:
            if any(mod.origin != refw.ORIGIN for mod in m.mods):
                state = ("● Nexus 키 필요", "BadgeNeutral.TLabel")
        elif self._mod_check_error:
            state = ("● 모드 업데이트 확인 실패", "BadgeWarn.TLabel")
        elif s.last_mod_check:
            state = ("● 모드 최신", "BadgeOk.TLabel")
        if state is None:
            self.mods_badge.pack_forget()
            return
        self.mods_badge.configure(text=state[0], style=state[1])
        if not self.mods_badge.winfo_manager():
            self.mods_badge.pack(side="right", padx=(0, self.theme.px(10)), after=self.refw_badge)

    def _mods_tooltip(self) -> str:
        s = self.manager.settings
        if self._checking_mods:
            return "Nexus에서 설치한 모드의 새 버전을 확인하고 있습니다"
        if not s.nexus_api_key:
            return ("설정에서 Nexus API 키를 넣으면 프로그램을 켤 때마다 모드 업데이트를 확인합니다.\n"
                    "클릭하면 설정을 엽니다.")
        lines = []
        if self._mod_check_error:
            lines.append(f"확인하지 못했습니다: {self._mod_check_error}")
        updates = [mod for mod in self.manager.mods if mod.update]
        lines += [f"{mod.name}  {mod.version or '?'} → {mod.update.get('version') or '새 파일'}" for mod in updates[:10]]
        if len(updates) > 10:
            lines.append(f"… 외 {len(updates) - 10}개")
        if s.last_mod_check:
            lines.append("마지막 확인: " + time.strftime("%Y-%m-%d %H:%M", time.localtime(s.last_mod_check)))
        lines.append("클릭하면 업데이트가 있는 모드만 보여 줍니다." if updates else "클릭하면 다시 확인합니다.")
        return "\n".join(lines)

    def _on_mods_badge_click(self):
        if not self.manager.settings.nexus_api_key:
            self.open_settings()
        elif any(mod.update for mod in self.manager.mods):
            self.filter_var.set(FILTER_UPDATES)
            self.refresh()
        elif not self._checking_mods:
            self.check_mod_updates(manual=True)

    # ------------------------------------------------------------------ profiles
    def _fill_profile_menu(self):
        menu = self.profile_menu
        menu.delete(0, "end")
        self.profile_var.set(self.manager.profile)
        for name in self.manager.profiles:
            menu.add_radiobutton(label=name, value=name, variable=self.profile_var,
                                 command=lambda n=name: self.switch_profile(n))
        menu.add_separator()
        menu.add_command(label="새 프로필 (모든 모드 꺼짐)...", command=lambda: self.new_profile(copy_current=False))
        menu.add_command(label="현재 프로필 복제...", command=lambda: self.new_profile(copy_current=True))
        menu.add_command(label="이름 변경...", command=self.rename_profile)
        menu.add_command(label="현재 프로필 삭제...", command=self.delete_profile,
                         state="normal" if len(self.manager.profiles) > 1 else "disabled")

    def switch_profile(self, name: str):
        if name == self.manager.profile or self._search_blocks() or not self.can_mutate():
            self.profile_var.set(self.manager.profile)
            return
        self.manager.switch_profile(name)
        log.info("프로필 전환: %s", name)
        self._changed()
        self.warm_equipment()

    def new_profile(self, copy_current: bool):
        if self._search_blocks() or not self.can_mutate():
            return
        title = "현재 프로필 복제" if copy_current else "새 프로필"
        initial = f"{self.manager.profile} 복사본" if copy_current else ""
        name = TextDialog(self.root, self.theme, title, "새 프로필 이름", initial).show()
        if name is None:
            return
        try:
            self.manager.create_profile(name, copy_current)
        except ValueError as exc:
            messagebox.showerror(title, str(exc), parent=self.root)
            return
        log.info("새 프로필: %s", self.manager.profile)
        self._changed()

    def rename_profile(self):
        if self._search_blocks():
            return
        old = self.manager.profile
        name = TextDialog(self.root, self.theme, "프로필 이름 변경", "새 이름", old).show()
        if not name or name.strip() == old:
            return
        try:
            self.manager.rename_profile(old, name)
        except ValueError as exc:
            messagebox.showerror("프로필 이름 변경", str(exc), parent=self.root)
            return
        self._update_header()

    def delete_profile(self):
        name = self.manager.profile
        if self._search_blocks() or not self.can_mutate() or not messagebox.askyesno(
                "프로필 삭제", f"'{name}' 프로필을 삭제할까요?\n설치된 모드는 지워지지 않습니다.", parent=self.root):
            return
        try:
            self.manager.delete_profile(name)
        except ValueError as exc:
            messagebox.showerror("프로필 삭제", str(exc), parent=self.root)
            return
        log.info("프로필 삭제: %s (현재: %s)", name, self.manager.profile)
        self._changed()

    def _refw_tooltip(self) -> str:
        m = self.manager
        if not m.game_dir:
            return "게임 폴더를 지정하면 REFramework 설치 여부를 확인합니다"
        status = self._refw_status
        if status is not None:
            text = f"설치된 버전: {refw.describe_installed(status)}\n최신 버전: {status.latest.label}"
        elif m.refw_installed():
            text = "dinput8.dll이 게임 폴더에 있습니다"
        else:
            text = "REFramework(dinput8.dll)가 없습니다."
        if not m.refw_installed() and m.needs_refw():
            text += "\n활성화된 루스 파일 · PAK · 스크립트 모드가 동작하려면 필요합니다."
        return text + "\n클릭하면 설치·업데이트 창을 엽니다."

    def _on_refw_click(self):
        if not self.manager.game_dir:
            return
        if self._refw_status is None:
            self.check_refw(manual=True)
        else:
            self.show_refw()

    # ------------------------------------------------------------------ REFramework updates
    def check_refw(self, manual: bool, builds: list | None = None):
        """Find the installed and the latest REFramework build in the background."""
        game = self.manager.game_dir
        if not game:
            if manual:
                messagebox.showwarning("REFramework", "먼저 게임 폴더를 지정하세요.", parent=self.root)
            return

        def worker():
            try:
                status = refw.check(game, builds)
            except UpdateError as exc:
                self.events.put(("call", self._on_refw_checked, None, exc, manual))
            else:
                self.events.put(("call", self._on_refw_checked, status, None, manual))

        if manual:
            self.status_var.set("REFramework 확인 중...")
        threading.Thread(target=worker, daemon=True).start()

    def _on_refw_checked(self, status: refw.Status | None, error: Exception | None, manual: bool):
        if manual:
            self.status_var.set("준비")
        if error is not None:
            log.info("REFramework 확인 실패: %s", error)
            if manual:
                messagebox.showwarning("REFramework", str(error), parent=self.root)
            return
        was_outdated = self._refw_status is not None and self._refw_status.needs_action
        self._refw_status = status
        self._update_header()
        if status.installed and not status.up_to_date and not was_outdated:
            log.info("REFramework 새 빌드(%s)가 있습니다. 위쪽 REFramework 배지를 누르면 업데이트할 수 있습니다.",
                     status.latest.label)
        if manual:
            self.show_refw()

    def show_refw(self):
        status = self._refw_status
        if status is None:
            return
        managed = self.manager.refw_mod() is not None
        choice = RefwDialog(self.root, self.theme, status, managed).show()
        if choice == "page":
            webbrowser.open(status.latest.page_url)
        elif choice == "install":
            self.install_refw(status.latest)

    def install_refw(self, build: refw.Build):
        if not self.manager.game_dir or self._search_blocks() or not self._idle():
            return
        m = self.manager

        def work(progress):
            m.temp_root.mkdir(parents=True, exist_ok=True)
            archive = m.temp_root / f"REFramework-{build.number}.zip"
            try:
                refw.download(build, archive, progress)
                return m.install_refw(archive, build.label, f"{refw.ASSET_NAME} ({build.tag})")
            finally:
                fsutil.remove_file(archive)

        def done(mod):
            log.info("REFramework %s를 설치했습니다.", build.label)
            self.refresh(select=mod.id)
            self.apply()  # put the new dinput8.dll into the game folder right away

        self.run_task("refw", f"REFramework {build.label} 내려받는 중...", work, on_done=done,
                      on_error=lambda exc: self.show_error("REFramework", str(exc)))

    # ------------------------------------------------------------------ Nexus mod updates
    def check_mod_updates(self, manual: bool):
        """Look the mods that came from Nexus up there in the background."""
        if self._checking_mods:
            return
        key = self.manager.settings.nexus_api_key
        if not key:
            if manual and messagebox.askyesno(
                    "모드 업데이트 확인", "모드 업데이트를 확인하려면 Nexus API 키가 필요합니다.\n설정에서 키를 넣을까요?",
                    parent=self.root):
                self.open_settings()
            return
        items = self.manager.nexus_items()
        if not items:
            if manual:
                messagebox.showinfo("모드 업데이트 확인", (
                    "Nexus와 연결된 모드가 없습니다.\n\n"
                    "Nexus에서 받은 파일을 이름 그대로 설치하면 자동으로 연결됩니다. 다른 모드는 목록에서 "
                    "오른쪽 클릭 > 'Nexus 링크 설정'으로 모드 페이지 주소를 넣어 주세요."), parent=self.root)
            return
        self._checking_mods = True
        self._update_mods_badge()
        targets = self.manager.requirement_targets()

        def status(msg):
            if self.busy is None:
                self.status_var.set(msg)

        def worker():
            try:
                results, problems = nexus.check_mods(
                    nexus.NexusClient(key), items, lambda done, total, msg: self.events.put(("call", status, msg)))
            except Exception as exc:  # reported to the user, never fatal
                self.events.put(("call", self._on_mods_checked, None, [], exc, manual))
                return
            try:
                found = nexus.fetch_requirements(targets, key)
            except Exception as exc:
                log.info("Nexus에서 필요한 모드 정보를 받지 못했습니다: %s", exc)
                found = {}
            self.events.put(("call", self._on_mods_checked, results, problems, None, manual, found))

        status("모드 업데이트 확인 중...")
        threading.Thread(target=worker, daemon=True).start()

    def _on_mods_checked(self, results: dict | None, problems: list[str], error: Exception | None, manual: bool,
                         found: dict | None = None):
        self._checking_mods = False
        self._mod_check_error = str(error) if error is not None else None
        if self.busy is None:
            self.status_var.set("준비")
        if error is not None:
            if not isinstance(error, nexus.NexusError):
                log.error("모드 업데이트 확인 중 오류", exc_info=error)
            log.warning("모드 업데이트 확인 실패: %s", error)
            self._update_mods_badge()
            if manual:
                messagebox.showwarning("모드 업데이트 확인", str(error), parent=self.root)
            return
        self.manager.set_update_results(results)
        if found:
            self.manager.set_requirements(found)
        for problem in problems:
            log.warning(problem)
        self.refresh()
        self._update_details()
        updates = [m for m in self.manager.mods if m.update]
        lines = [f"• {m.name}  {m.version or '?'} → {m.update.get('version') or '새 파일'}" for m in updates]
        if updates:
            log.info("새 버전이 있는 모드 %d개:\n%s", len(updates), "\n".join(lines))
        else:
            log.info("Nexus와 연결된 모드 %d개가 모두 최신입니다.", len(results))
        if not manual:
            return
        note = ("\n\n" + "\n".join(problems)) if problems else ""
        if not updates:
            messagebox.showinfo("모드 업데이트 확인", f"Nexus와 연결된 모드 {len(results)}개가 모두 최신입니다.{note}",
                                parent=self.root)
            return
        shown = "\n".join(lines[:15]) + (f"\n… 외 {len(lines) - 15}개" if len(lines) > 15 else "")
        if messagebox.askyesno("모드 업데이트 확인", f"{len(updates)}개 모드에 새 버전이 있습니다.\n\n{shown}{note}\n\n"
                                                 "업데이트가 있는 모드만 볼까요?", parent=self.root):
            self.filter_var.set(FILTER_UPDATES)
            self.refresh()

    # ------------------------------------------------------------------ requirements
    def _check_requirements_of(self, mod: Mod):
        """After installing: read the mod's requirements from Nexus, then say what is missing."""
        if mod.nexus_id and mod.origin != refw.ORIGIN:
            self._fetch_requirements([mod.nexus_id], notify=[mod.id])
            return
        if mod.origin != refw.ORIGIN:
            log.info("'%s': 파일 이름에서 Nexus 모드 번호를 찾지 못했습니다. 정보 탭의 'Nexus 링크 설정'으로 모드 페이지를 "
                     "연결하면 업데이트와 필요한 모드를 확인할 수 있습니다.", mod.name)
        self._queue_requirement_notices([mod.id])

    def _fetch_unread_requirements(self):
        """Requirements of Nexus mods that were never read (needs no API key)."""
        ids = self.manager.requirement_targets(max_age=None)
        if ids:
            self._fetch_requirements(ids)

    def _fetch_requirements(self, nexus_ids: list[int], notify: list[str] = ()):
        key = self.manager.settings.nexus_api_key

        def worker():
            try:
                found = nexus.fetch_requirements(nexus_ids, key)
            except Exception as exc:  # offline etc.: the Lua check still works
                log.info("Nexus에서 필요한 모드 정보를 받지 못했습니다: %s", exc)
                found = {}
            self.events.put(("call", self._on_requirements_fetched, found, list(notify)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_requirements_fetched(self, found: dict, notify: list[str]):
        if found:
            self.manager.set_requirements(found)
            self.refresh()
        self._queue_requirement_notices(notify)

    def _queue_requirement_notices(self, mod_ids: list[str]):
        self._req_notices += [i for i in mod_ids if i not in self._req_notices]
        if not self._installing:
            self._flush_requirement_notices()

    def _flush_requirement_notices(self):
        """Tell about missing requirements of the mods just installed (once the install queue is done; not
        over another window or task: then a little later)."""
        if self._closed or not self._req_notices:
            return
        if self.busy is not None or self._modal_open():
            if not self._req_flush_scheduled:
                self._req_flush_scheduled = True

                def again():
                    self._req_flush_scheduled = False
                    if not self._installing:
                        self._flush_requirement_notices()

                self.root.after(1000, again)
            return
        ids, self._req_notices = self._req_notices, []
        items = []
        for mod_id in ids:
            try:
                mod = self.manager.get(mod_id)
            except KeyError:
                continue
            # (shaders are told after the mod's first material check: _notify_shader_requirements)
            missing = [r for r in self.manager.missing_requirements(mod_id) if r["kind"] != "shader"]
            if missing:
                items.append((mod, missing))
                for req in missing:
                    log.warning("'%s'에 필요한 모드: %s (%s)", mod.name, req["name"], requirement_note(req))
        if items and not self._closed:
            RequirementsDialog(self.root, self.theme, items).show()

    def _search_nexus(self):
        mod = self._selected_mod()
        if mod is not None:
            webbrowser.open(NEXUS_SEARCH_URL.format(urllib.parse.quote(mod.name)))

    def _open_nexus(self, url_of):
        mod = self._selected_mod()
        if mod is not None and mod.nexus_id:
            webbrowser.open(url_of(mod))

    def link_nexus(self):
        mod = self._selected_mod()
        if mod is None:
            return
        text = TextDialog(self.root, self.theme, "Nexus 링크 설정",
                          "Nexus 모드 페이지 주소나 모드 번호 (비우면 연결 해제)", mod.nexus_url).show()
        if text is None:
            return
        try:
            if text.strip():
                old_id = mod.nexus_id
                nexus_id = self.manager.link_nexus(mod.id, text)
                log.info("'%s'을(를) Nexus 모드 #%d에 연결했습니다.", mod.name, nexus_id)
                if nexus_id != old_id:
                    self._fetch_requirements([nexus_id], notify=[mod.id])
            elif mod.nexus_id:
                self.manager.unlink_nexus(mod.id)
                log.info("'%s'의 Nexus 연결을 해제했습니다.", mod.name)
        except ValueError as exc:
            messagebox.showwarning("Nexus 링크 설정", str(exc), parent=self.root)
            return
        self.refresh()
        self._update_details()

    def _fill_game_menu(self):
        menu = self.game_menu
        menu.delete(0, "end")
        game = self.manager.game_dir
        if game:
            menu.add_command(label=shorten_middle(str(game), 64), state="disabled")
            menu.add_separator()
            menu.add_command(label="게임 폴더 열기", command=self.open_game_folder)
        menu.add_command(label="다른 폴더 선택...", command=self.browse_game)
        menu.add_command(label="Steam에서 자동 감지", command=self.detect_game)

    def _selected_ids(self) -> list[str]:
        return [i for i in self.tree.selection() if self.tree.exists(i)]

    def _selected_mod(self) -> Mod | None:
        ids = self._selected_ids()
        if len(ids) != 1:
            return None
        try:
            return self.manager.get(ids[0])
        except KeyError:
            return None

    def _row_tooltip(self, mod_id: str) -> str:
        reasons = self._problems.get(mod_id)
        return "\n".join("⚠ " + r for r in reasons) if reasons else ""

    def _highlight_related(self):
        """Colour the mods that override, or are overridden by, the one selected mod."""
        mod = self._selected_mod()
        beats: dict = {}
        beaten: dict = {}
        if mod is not None and mod.enabled and self._conflicts is not None:
            beats, beaten = self._conflicts.for_mod(mod.id)
        for iid in self.tree.get_children():
            tags = [t for t in self.tree.item(iid, "tags") if t not in ("over_sel", "under_sel")]
            if iid in beaten:
                tags.append("over_sel")
            elif iid in beats:
                tags.append("under_sel")
            self.tree.item(iid, tags=tags)
        if beats or beaten:
            if not self.legend.winfo_manager():
                self.legend.pack(side="right", before=self.hint_label)  # gets its space before the hint
        else:
            self.legend.pack_forget()

    def show_problems(self):
        self.filter_var.set(FILTER_PROBLEMS)
        self.refresh()

    def warm_equipment(self):
        """Work out in the background which equipment the mods change (and read the game's names once),
        so that the info tab can list it and the 장비 외형 window opens quickly."""
        m = self.manager
        if self._closed or not m.mods:
            return
        if self._equip_warming:
            self._equip_again = True  # mods changed while the last one ran: go again afterwards
            return
        self._equip_warming = True
        jobs = m.equipment_jobs()

        def worker():
            try:
                m.equipment_catalog()
                m.equipment_scans(jobs)
            except Exception as exc:  # the window shows the problem when it is opened
                log.debug("장비 외형을 미리 읽지 못했습니다: %s", exc)
            self.events.put(("call", self._equipment_warmed))

        threading.Thread(target=worker, daemon=True).start()

    def _equipment_warmed(self):
        self._equip_warming = False
        if self._closed:
            return
        self._update_details()
        if self._equip_again:
            self._equip_again = False
            self.warm_equipment()

    def show_equipment(self, mod_id: str | None = None):
        """The 장비 외형 window (one at a time), optionally showing only one mod's equipment."""
        if self.equipment_window is not None:
            self.equipment_window.show(mod_id)
            return
        from .equipview import EquipmentWindow  # imports this module

        self.equipment_window = EquipmentWindow(self, mod_id)

    def open_retarget(self, mod_id: str, source=None, parent=None):
        """다른 장비로 옮기기: the dialog (over ``parent``, by default the main window; ``source``: the
        equipment picked first), then the new mod made from the mod's files for the equipment chosen. Over
        another window (장비 외형) a refusal is told there: the main window's status bar may be hidden."""
        window = parent if parent is not None and parent is not self.root else None
        if window is None:
            if self._search_blocks() or not self.can_mutate():
                return
        elif self._search_blocks(loud=False):
            messagebox.showinfo(bisect.TITLE, bisect.BLOCKED, parent=window)
            return
        elif not self.can_mutate():
            messagebox.showinfo(RETARGET_TITLE, "다른 작업이 진행 중입니다. 작업이 끝난 뒤에 다시 시도하세요.", parent=window)
            return
        if not any(m.id == mod_id for m in self.manager.mods):
            return
        from .retargetview import RetargetDialog  # imports this module

        choice = RetargetDialog(parent or self.root, self, mod_id, source).show()
        if choice is not None:
            self._create_retarget(mod_id, choice)

    def _create_retarget(self, mod_id: str, choice, waited: bool = False):
        """Make the mod the dialog chose once no other task runs (an apply may have started meanwhile). While
        it waits the game does not start (_retarget_waiting, as during the task: FOLDER_TASKS)."""
        if waited:
            self._retarget_waiting -= 1
        if self._closed or not any(m.id == mod_id for m in self.manager.mods):
            return
        if self.busy is not None or self._installing:
            self.status_var.set("진행 중인 작업이 끝나면 새 모드를 만듭니다...")
            self._retarget_waiting += 1
            self.root.after(200, lambda: self._create_retarget(mod_id, choice, waited=True))
            return
        if self._search_blocks():
            return
        m = self.manager
        original = m.get(mod_id)
        name, was_on = original.name, original.enabled

        def work(progress):
            return m.create_retarget(mod_id, choice.source, choice.target, parts=choice.parts,
                                     disable_original=choice.disable_original, enable=True, progress=progress)

        def done(mod: Mod):
            if was_on and not any(o.id == mod_id and o.enabled for o in m.mods):
                what = "원래 모드는 껐습니다"
                log.info("'%s'을(를) 껐습니다. 원래 장비는 게임 모습으로 돌아갑니다 (원래 모드를 다시 켜면 "
                         "원래 장비도 모드 모습이 됩니다).", name)
            elif was_on:
                what = "원래 모드도 켜 두었습니다"
                log.info("'%s'은(는) 켜 두었습니다. 원래 장비와 옮긴 장비가 모두 모드 모습입니다.", name)
            else:
                what = ""
            self.refresh(select=mod.id)
            if not self.tree.exists(mod.id):  # (hidden by the search or a filter)
                self.show_mod(mod.id)
            self.status_var.set(" · ".join(filter(None, (f"새 모드를 만들었습니다: {mod.name}", what))))
            if mod.enabled or was_on:
                if m.settings.auto_apply and m.game_dir:  # (the apply's end would say "준비" at once)
                    self._status_after_task = self.status_var.get()
                self.request_apply()
            self._first_check.add(mod.id)  # its first material check tells of shaders it lacks, as for an install
            self.check_materials_background()
            self.warm_equipment()

        def failed(exc):
            self.show_error(RETARGET_TITLE, f"새 모드를 만들지 못했습니다.\n\n{exc}")
            self.refresh(select=mod_id)

        self.run_task("retarget", f"새 모드 만드는 중: {name}", work, on_done=done, on_error=failed)

    def show_mod(self, mod_id: str):
        """Select a mod in the list (clearing a search or filter that hides it) and show its info."""
        if not any(m.id == mod_id for m in self.manager.mods):
            return
        if not self.tree.exists(mod_id):
            self.filter_var.set(FILTER_ALL)
            self.search_var.set("")
            self.refresh()
        self.tree.selection_set(mod_id)
        self.tree.see(mod_id)
        self.root.deiconify()
        self.root.lift()

    def show_conflicts(self):
        mod_id = ConflictsDialog(self.root, self.theme, self.manager).show()
        if mod_id and any(m.id == mod_id for m in self.manager.mods):
            if not self.tree.exists(mod_id):
                self.filter_var.set(FILTER_ALL)
                self.search_var.set("")
                self.refresh()
            self.tree.selection_set(mod_id)
            self.tree.see(mod_id)
            self._select_tab(self.conflicts_tab)

    @staticmethod
    def _set_text(widget: tk.Text, chunks):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for text, tag in chunks:
            widget.insert("end", text, tag)
        widget.configure(state="disabled")

    def _update_details(self):
        mod = self._selected_mod()
        self.files_tree.delete(*self.files_tree.get_children())
        if mod is None:
            self._show_preview(None, "")
            n = len(self._selected_ids())
            if n > 1:
                chunks = [(f"{n}개 모드 선택됨\n", "title"),
                          ("스페이스바로 한꺼번에 켜고 끄거나, Delete 키로 삭제할 수 있습니다.", "muted")]
            else:
                chunks = [("모드 정보\n", "title"),
                          ("목록에서 모드를 선택하면 정보, 파일 목록, 충돌 내용을 볼 수 있습니다.", "muted")]
            self._set_text(self.info_text, chunks)
            self._set_text(self.conflict_text, [("충돌\n", "title"), ("모드를 선택하세요.", "muted")])
            self._fill_options(None)
            self._follow_tab()
            return
        m = self.manager
        self._show_preview(m.screenshot_path(mod), mod.name)
        files = m.mod_files(mod.id)
        kinds = m.mod_kinds(mod.id)
        chunks = [(mod.name + "\n", "title")]
        chips = [("활성" if mod.enabled else "비활성", "chip_ok" if mod.enabled else "chip_off")]
        if mod.needs_option:
            chips.append(("옵션 선택 필요", "chip_warn"))
        if mod.update:
            chips.append(("새 버전 있음", "chip_info"))
        material = m.material_state(mod.id)
        if material == "stale":
            chips.append(("재질 복구 필요", "chip_warn"))
        elif material == "repaired":
            chips.append(("재질 복구됨", "chip_info"))
        chips += [(KIND_LABELS[k], "chip") for k in KIND_LABELS if k in self._display_kinds(mod)]
        if mod.version:
            chips.append((f"v{mod.version}" if mod.version[:1].isdigit() else mod.version, "chip"))
        for text, tag in chips:
            chunks += [(f"  {text}  ", tag), ("  ", None)]
        chunks.append(("\n\n", None))
        for label, value in (
            ("옵션", f"{len(mod.options)}개 중 {len(mod.selected)}개 사용" if mod.options else ""),
            ("제작자", mod.author),
            ("분류", mod.category),
            ("파일", f"{len(files)}개 · {format_size(m.mod_size(mod.id))}"),
            ("원본", mod.source),
            ("설치일", mod.installed_at.replace("T", " ")),
            ("업데이트", mod.updated_at.replace("T", " ")),
        ):
            if value:
                chunks += [(f"{label}\t", "key"), (f"{value}\n", "value")]
        reqs = m.requirement_status(mod.id)
        icons = {"ok": ("✓", "req_ok"), "missing": ("✕", "req_missing"), "off": ("!", "req_off"),
                 "unknown": ("?", "req_unknown")}
        for i, req in enumerate(reqs):
            icon, icon_tag = icons[req["state"]]
            name_tags = ("value", "link", f"req{i}") if req["url"] else ("value",)
            if req["url"]:
                self.info_text.tag_bind(f"req{i}", "<Button-1>", lambda e, url=req["url"]: webbrowser.open(url))
            chunks += [("필요한 모드\t" if i == 0 else "\t", "key"), (f"{icon} ", ("value", icon_tag)),
                       (req["name"], name_tags), (f"   {requirement_note(req)}\n", ("value", "muted"))]
        missing = [r for r in reqs if r["state"] in ("missing", "off")]
        if not mod.nexus_id and mod.origin != refw.ORIGIN:
            chunks += [("Nexus\t", "key"), ("연결 안 됨   ", ("value", "muted")),
                       ("링크 설정", ("value", "link", "nexus_set")), ("  ·  ", ("value", "muted")),
                       ("Nexus에서 찾기", ("value", "link", "nexus_search")), ("\n", "value")]
        if mod.nexus_id:
            chunks += [("Nexus\t", "key"), (f"모드 페이지 #{mod.nexus_id}", ("value", "link", "nexus_page"))]
            if mod.nexus_checked:
                checked = time.strftime("%Y-%m-%d %H:%M", time.localtime(mod.nexus_checked))
                chunks.append((f"   {checked} 확인", ("value", "muted")))
            chunks.append(("\n", "value"))
        stuck = m.material_stuck(mod.id)
        if material != "none":
            results = m.material_results(mod.id)
            repaired = len(m.repaired_materials(mod.id))
            chunks.append(("재질\t", "key"))
            if material == "unchecked":
                if matfix.available():
                    chunks += [("아직 점검하지 않음   ", ("value", "muted")), ("지금 점검", ("value", "link", "matfix_check"))]
                else:
                    chunks.append(("점검할 수 없음 (zstd 모듈 없음)", ("value", "muted")))
            elif material == "current" and stuck:
                chunks += [("✕ ", ("value", "req_missing")), (f"고칠 수 없는 {stuck_summary(stuck)}", "value")]
            elif material == "current":
                chunks += [("현재 게임과 맞음", "value")]
            elif material == "stale":
                chunks += [("✕ ", ("value", "req_missing")), ("현재 게임과 맞지 않음   ", "value"),
                           ("재질 복구", ("value", "link", "matfix_repair"))]
            elif material == "repaired":
                chunks += [("✓ ", ("value", "req_ok")), (f"복구한 파일 {repaired}개 사용 중   ", "value")]
            else:
                chunks += [("복구하지 않고 원래 파일 사용 중   ", ("value", "muted")),
                           ("재질 복구", ("value", "link", "matfix_repair"))]
            if repaired and material != "unchecked":
                chunks += [("  ·  ", ("value", "muted")), ("원래대로 되돌리기", ("value", "link", "matfix_restore"))]
            chunks.append(("\n", "value"))
            if material in ("stale", "repaired", "declined"):
                chunks.append(("\t" + material_summary(results) + "\n", ("value", "muted")))
            if stuck and material != "current":
                chunks += [("\t✕ ", ("value", "req_missing")), (f"고칠 수 없는 {stuck_summary(stuck)}\n", "value")]
            errors = sum(1 for r in results if r.state == "error")
            if errors:
                chunks.append((f"\t읽지 못한 파일 {errors}개 (자세한 내용은 로그)\n", ("value", "muted")))
        looks = m.equipment_summary(mod.id)
        for i, text in enumerate(looks[:4]):
            chunks += [("외형\t" if i == 0 else "\t", "key"), (f"{text}\n", "value")]
        if looks:
            more = f"외 {len(looks) - 4}개   " if len(looks) > 4 else ""
            chunks += [("\t", "key"), (more, ("value", "muted")),
                       ("장비 외형에서 보기", ("value", "link", "equip_show")), ("\n", "value")]
        if mod.update:
            new = mod.update
            what = " · ".join(x for x in (str(new.get("version") or ""), str(new.get("file") or ""),
                                         str(new.get("uploaded") or "")) if x)
            chunks += [("\n", None), ("새 버전이 있습니다\n", ("note", "warn_title")),
                       (f"Nexus에 새 파일이 올라왔습니다{f' ({what})' if what else ''}. 받은 파일을 이 창에 끌어 놓고 "
                        "'기존 모드를 새 파일로 교체'를 고르면 순서와 활성 상태를 유지한 채 바뀝니다.\n", "note"),
                       ("Nexus 파일 페이지 열기", ("note", "link", "nexus_files")), ("\n", "note")]
        if missing:
            names = ", ".join(r["name"] for r in missing)
            link = " 위 '필요한 모드'의 이름을 누르면 Nexus 페이지가 열립니다." if any(r["url"] for r in missing) else ""
            chunks += [("\n", None), ("필요한 모드가 없습니다\n", ("warn", "warn_title")),
                       (f"{names} — 설치하거나 켜야 이 모드가 제대로 작동합니다.{link}\n", "warn")]
        if material == "stale":
            chunks += [("\n", None), ("현재 게임과 맞지 않는 파일이 있습니다\n", ("warn", "warn_title")),
                       ("게임 업데이트로 재질(.mdf2)이나 .user·.pfb 파일의 구조가 바뀌어 텍스처가 깨지거나 게임이 "
                        "검은 화면에서 멈출 수 있습니다. 위 '재질 복구'를 누르면 모드의 내용을 현재 게임의 구조에 옮겨 "
                        "담은 파일을 대신 적용합니다. 모드의 원래 파일은 그대로 둡니다.\n", "warn")]
        if stuck:
            shown = "\n".join(f"• {i['label'].rsplit('/', 1)[-1]}: {i['reason']}" +
                              (f"\n   ({i['detail']})" if i.get("detail") else "") for i in stuck[:4])
            more = f"\n… 외 {len(stuck) - 4}개" if len(stuck) > 4 else ""
            chunks += [("\n", None), ("고칠 수 없는 파일이 있습니다\n", ("warn", "warn_title")),
                       ("현재 게임과 맞지 않지만 안전하게 고칠 방법을 찾지 못해 그대로 둔 파일입니다. 게임이 검은 화면에서 "
                        f"멈추면 이 모드를 끄고 모드 업데이트를 확인하세요.\n{shown}{more}\n", "warn"),
                       ("진단 파일 저장", ("warn", "link", "matfix_report")),
                       (" — 모드 파일, 게임 원본 파일, 점검 결과를 zip 하나로 저장합니다\n", "warn")]
        if mod.needs_option:
            chunks += [("\n", None), ("옵션을 활성화해 주세요\n", ("warn", "warn_title")),
                       ("이 모드에는 옵션이 여러 개 있습니다. '옵션' 탭에서 사용할 옵션을 켜기 전에는 "
                        "게임에 아무 파일도 적용되지 않습니다.\n", "warn")]
        if kinds & {"loose", "script", "pak"} and not m.refw_installed():
            how = "위쪽 REFramework 배지를 눌러 설치하세요."
            if kinds & {"loose"}:
                how += " 루스 파일은 게임 안 REFramework 메뉴의 LooseFileLoader에서 'Enable Loose File Loader'를 켜야 합니다."
            if "pak" in kinds:
                how += f" PAK은 REFramework가 {PAK_MODS_DIR} 폴더에서 불러옵니다."
            chunks += [("\n", None), ("REFramework가 필요합니다\n", ("warn", "warn_title")), (how + "\n", "warn")]
        if mod.description:
            chunks.append(("\n" + mod.description.replace("\\n", "\n") + "\n", "desc"))
        self._set_text(self.info_text, chunks)
        self._fill_files(mod, files)
        self._fill_conflicts(mod)
        self._fill_options(mod)
        self._follow_tab()

    def _on_tab_click(self, event):
        """Remember the details tab the user picks: it comes back as soon as another mod has it again
        (옵션 after a mod without options showed 파일)."""
        try:
            index = self.notebook.tk.call(self.notebook._w, "identify", "tab", event.x, event.y)
        except tk.TclError:
            return
        if index != "":
            self._wanted_tab = self.notebook.tabs()[int(index)]

    def _on_tab_changed(self, _event=None):
        """A tab picked with the keyboard (or by the app) counts too, but not the one Tk falls back to
        when the shown tab is hidden (옵션 for a mod without options)."""
        shown, self._shown_tab = self._shown_tab, self.notebook.select()
        try:
            if shown and str(self.notebook.tab(shown, "state")) == "hidden":
                return
        except tk.TclError:
            pass
        self._wanted_tab = self._shown_tab

    def _select_tab(self, tab):
        self._wanted_tab = str(tab)
        self.notebook.select(tab)

    def _follow_tab(self):
        wanted = self._wanted_tab
        if wanted and wanted in self.notebook.tabs() and self.notebook.select() != wanted \
                and str(self.notebook.tab(wanted, "state")) == "normal":
            self.notebook.select(wanted)

    def _fill_options(self, mod: Mod | None):
        for frame in (self.options_head, self.options_inner):
            for child in frame.winfo_children():
                child.destroy()
        self._option_vars = []
        self._option_images = []
        self._options_gen += 1
        if mod is None or not mod.options:
            self._options_mod = None
            self.notebook.tab(self.options_tab, state="hidden", text="옵션")
            return
        t, px, m = self.theme, self.theme.px, self.manager
        # the same mod again (an option was switched): stay where the list was scrolled to
        top = self.options_canvas.yview()[0] if self._options_mod == mod.id else 0.0
        self._options_mod = mod.id
        self.notebook.tab(self.options_tab, state="normal",
                          text=f"옵션 {len(mod.selected)}/{len(mod.options)}" if mod.selected else "옵션 ●")
        head = self.options_head
        ttk.Label(head, text="옵션", style="CardTitle.TLabel").pack(side="left")
        ttk.Button(head, text="모두 끄기", style="Compact.TButton",
                   command=lambda: self._set_options(mod.id, [])).pack(side="right")
        ttk.Button(head, text="모두 켜기", style="Compact.TButton",
                   command=lambda: self._set_options(mod.id, [o["id"] for o in mod.options])).pack(
            side="right", padx=(0, px(6)))
        inner = self.options_inner
        if mod.needs_option:
            callout = ttk.Frame(inner, style="Callout.TFrame", padding=(px(14), px(10)))
            callout.pack(fill="x", pady=(px(4), px(4)))
            ttk.Label(callout, text="옵션을 활성화해 주세요", style="CalloutTitle.TLabel").pack(anchor="w")
            ttk.Label(callout, text="사용할 옵션을 아래에서 하나 이상 켜야 게임에 적용됩니다.",
                      style="Callout.TLabel", wraplength=px(380), justify="left").pack(anchor="w", pady=(px(2), 0))
        else:
            ttk.Label(inner, text="여러 옵션을 함께 켤 수 있습니다. 같은 파일이 겹치면 아래쪽 옵션이 우선합니다.",
                      style="CardMuted.TLabel", wraplength=px(380), justify="left").pack(anchor="w", pady=(0, px(4)))
        pictures = {o["id"]: m.option_preview(mod.id, o) for o in mod.options}
        size = (px(128), px(72)) if any(pictures.values()) else None
        if size is None and not mod.pictures_read:
            # e.g. installed before options kept their pictures (1.12.0): they are still in the archive
            hint = ttk.Frame(inner, style="Card.TFrame")
            hint.pack(fill="x", pady=(px(4), 0))
            ttk.Label(hint, text="옵션 그림이 없습니다. 설치할 때 쓴 압축 파일에서 옵션 그림을 다시 찾아볼 수 있습니다 "
                                 "(1.12.0 이전에 설치한 모드는 옵션 그림을 저장하지 않았습니다).", style="CardMuted.TLabel",
                      wraplength=px(380), justify="left").pack(anchor="w")
            ttk.Button(hint, text="압축 파일에서 옵션 그림 가져오기", style="Link.TButton",
                       command=lambda: self.read_option_pictures(mod.id)).pack(anchor="w", pady=(px(2), 0))
        pending = []
        for opt in mod.options:
            var = tk.BooleanVar(value=opt["id"] in mod.selected)
            self._option_vars.append((opt["id"], var))
            row = ttk.Frame(inner, style="Card.TFrame")
            row.pack(fill="x", pady=(px(12 if size else 10), 0))
            path = pictures[opt["id"]]
            if path:  # an option without a picture has no box
                box = ttk.Frame(row, style="Thumb.TFrame", width=size[0], height=size[1])
                box.pack_propagate(False)
                box.pack(side="left", anchor="n", padx=(0, px(10)))
                thumb = ttk.Label(box, style="Thumb.TLabel", anchor="center")
                thumb.pack(fill="both", expand=True)
                image = self._thumbnail(path, *size, load=False)
                if image is not None:
                    self._show_option_thumb(thumb, path, opt["name"], image)
                else:
                    pending.append((thumb, path, opt["name"]))  # loaded once the tab is drawn
            body = ttk.Frame(row, style="Card.TFrame")
            body.pack(side="left", fill="x", expand=True)
            line = ttk.Frame(body, style="Card.TFrame")
            line.pack(fill="x")
            t.checkbutton(line, opt["name"], var, card=True,
                          command=lambda: self._on_options_changed(mod.id)).pack(side="left")
            ttk.Button(line, text="이것만 사용", style="Link.TButton",
                       command=lambda oid=opt["id"]: self._set_options(mod.id, [oid])).pack(side="right")
            files = m.option_files(mod.id, opt["id"])
            ttk.Label(body, text=f"{kinds_text(classify(files))} · 파일 {len(files)}개",
                      style="CardMuted.TLabel", font=t.fonts["small"]).pack(anchor="w", padx=(px(34), 0))
        self._bind_wheel(inner, self.options_canvas)
        self.options_canvas.yview_moveto(top)
        if top:
            self.root.after_idle(lambda: self.options_canvas.yview_moveto(top))
        if pending:
            gen = self._options_gen
            self.root.after(1, lambda: self._load_option_thumbs(gen, pending, size))

    def _thumbnail(self, path: Path, width: int, height: int, load: bool = True):
        """Thumbnail of a picture, cached; with ``load=False`` only one that was made before."""
        try:
            st = path.stat()
        except OSError:
            return None
        key = (str(path), st.st_mtime_ns, st.st_size, width, height)
        if key not in self._thumbs:
            if not load:
                return None
            if len(self._thumbs) >= 300:
                self._thumbs.clear()
            self._thumbs[key] = load_image(path, width, height)
        return self._thumbs[key]

    def _load_option_thumbs(self, gen: int, pending: list, size: tuple[int, int]):
        """Load the option thumbnails one at a time so that the window stays responsive."""
        if gen != self._options_gen or not pending:
            return
        thumb, path, name = pending.pop(0)
        image = self._thumbnail(path, *size)
        if thumb.winfo_exists():
            if image is not None:
                self._show_option_thumb(thumb, path, name, image)
            else:
                thumb.master.destroy()  # unreadable: as without a picture
        if pending:
            self.root.after(1, lambda: self._load_option_thumbs(gen, pending, size))

    def _show_option_thumb(self, thumb: ttk.Label, path: Path, name: str, image):
        self._option_images.append(image)
        thumb.configure(image=image, text="", cursor="hand2")
        thumb.bind("<Button-1>", lambda e: self.show_picture(path, name))

    def show_picture(self, path: Path, title: str, parent=None):
        """A picture shown large, over ``parent`` (the window it was clicked in; a dialog belongs to a
        window, and Windows brings that window to the front with it)."""
        ImageDialog(parent or self.root, self.theme, title, path).show()

    def _bind_wheel(self, widget, canvas: tk.Canvas):
        def wheel(event):
            delta = -1 if (getattr(event, "delta", 0) > 0 or getattr(event, "num", 0) == 4) else 1
            canvas.yview_scroll(delta, "units")

        for w in (widget, canvas):
            w.bind("<MouseWheel>", wheel)
            w.bind("<Button-4>", wheel)
            w.bind("<Button-5>", wheel)
        for child in widget.winfo_children():
            self._bind_wheel(child, canvas)

    def _on_options_changed(self, mod_id: str):
        self._set_options(mod_id, [oid for oid, var in self._option_vars if var.get()])

    def _set_options(self, mod_id: str, selected: list[str]):
        if self._search_blocks(loud=False) or not self.can_mutate():
            self.root.after_idle(self._update_details)  # put the checkboxes back
            return
        self.manager.set_options(mod_id, selected)
        # rebuild after the click handler has returned (it destroys the clicked widget)
        self.root.after_idle(lambda: self._changed(select=mod_id))
        self.warm_equipment()  # other options change other equipment
        if mod_id in self._first_check and selected and self.manager.material_state(mod_id) != "unchecked":
            self._first_check.discard(mod_id)  # (installed, checked, and now its options are chosen)
            self.root.after_idle(lambda: self._notify_shader_requirements([mod_id]))

    def show_options(self, mod_id: str):
        self.tree.selection_set(mod_id)
        self.tree.see(mod_id)
        self._update_details()
        self._select_tab(self.options_tab)

    def _missing_pictures(self, mod: Mod) -> bool:
        """A mod with options without pictures whose archive was not looked at for them."""
        return bool(mod.options) and not mod.pictures_read and \
            not any(self.manager.option_preview(mod.id, o) for o in mod.options)

    def read_option_pictures(self, mod_id: str):
        """Take a mod's option pictures from the archive it was installed from (found in the folders
        mods came from, Downloads or Desktop; else the user picks it)."""
        if not self._idle():
            return
        m = self.manager
        mod = m.get(mod_id)
        folders = m.archive_folders()
        path = m.find_archive(mod_id, folders)
        if path is None:
            chosen = filedialog.askopenfilename(
                parent=self.root, title=f"'{mod.source or mod.name}' 압축 파일 선택", filetypes=INSTALL_FILETYPES,
                initialdir=str(folders[0]) if folders else None)
            if not chosen or not self._idle():  # (something may have started while the dialog was open)
                return
            path = Path(chosen)

        def done(count):
            self._update_details()
            if count:
                self.status_var.set(f"'{mod.name}': 옵션 그림 {count}개를 가져왔습니다")
            else:
                messagebox.showinfo("옵션 그림", f"'{path.name}'에는 옵션마다 다른 그림이 없습니다."
                                    + (" 모드 그림은 정보 탭에 나옵니다." if m.screenshot_path(mod) else ""),
                                    parent=self.root)

        self.run_task("pictures", f"옵션 그림 가져오는 중: {path.name}",
                      lambda progress: m.read_option_pictures(mod_id, path, progress), on_done=done,
                      on_error=lambda exc: self.show_error("옵션 그림을 가져오지 못했습니다", f"{path.name}\n\n{exc}"))

    def read_all_option_pictures(self):
        """The same for every such mod whose archive is still in a known folder."""
        if not self._idle():
            return
        m = self.manager
        ids = [mod.id for mod in m.mods if self._missing_pictures(mod)]
        if not ids:
            messagebox.showinfo("옵션 그림", "옵션 그림을 가져올 모드가 없습니다.", parent=self.root)
            return

        def work(progress):
            progress(0, len(ids), "설치할 때 쓴 압축 파일 찾는 중...")
            folders = m.archive_folders()  # (in the background: a folder may be on a slow network drive)
            results, missing = [], []
            for i, mod_id in enumerate(ids):
                name = m.get(mod_id).name
                path = m.find_archive(mod_id, folders)
                if path is None:
                    missing.append(name)
                    continue
                progress(i, len(ids), f"옵션 그림 가져오는 중: {path.name}")
                try:
                    results.append((name, m.read_option_pictures(mod_id, path), ""))
                except Exception as exc:  # one broken archive does not stop the others
                    log.warning("옵션 그림을 가져오지 못했습니다: %s (%s)", path.name, exc)
                    results.append((name, 0, str(exc)))
            return results, missing

        def done(outcome):
            results, missing = outcome
            self._update_details()
            if not results:
                messagebox.showinfo("옵션 그림", "설치할 때 쓴 압축 파일을 찾지 못했습니다 (모드를 설치한 폴더, 다운로드, "
                                             "바탕 화면에서 찾음).\n\n모드의 옵션 탭에서 압축 파일을 직접 골라 주세요:\n"
                                    + "\n".join(f"· {n}" for n in missing), parent=self.root)
                return
            lines = [f"· {name}: " + (f"실패 - {error}" if error else f"그림 {n}개" if n else "옵션마다 다른 그림 없음")
                     for name, n, error in results]
            if missing:
                lines += ["", "압축 파일을 찾지 못한 모드 (옵션 탭에서 직접 골라 주세요):", *(f"· {n}" for n in missing)]
            messagebox.showinfo("옵션 그림", "\n".join(lines), parent=self.root)

        self.run_task("pictures", "옵션 그림 가져오는 중...", work, on_done=done)

    def _show_preview(self, path: Path | None, title: str):
        self._preview = self._thumbnail(path, self.theme.px(380), self.theme.px(200)) if path is not None else None
        self._preview_path = (path, title) if self._preview is not None else None
        if self._preview is not None:
            self.preview_label.configure(image=self._preview)
            if not self.preview_label.winfo_ismapped():
                self.preview_label.pack(fill="x", padx=self.theme.px(18), pady=(self.theme.px(18), 0),
                                        before=self.info_text)
        else:
            self.preview_label.configure(image="")
            self.preview_label.pack_forget()

    def _fill_files(self, mod: Mod, files: list[str]):
        deployed = self.manager.deployed_files()
        by_source = {(e.get("mod"), e.get("src")): e["path"] for e in deployed.values()}
        entries = self.manager.mod_entries(mod.id)
        if mod.needs_option:
            self.files_tree.insert("", "end", values=("선택된 옵션이 없습니다 — '옵션' 탭에서 옵션을 켜세요", ""),
                                   tags=("attention",))
        names = {x.id: x.name for x in self.manager.mods}
        conflicts = self._conflicts
        for i, rel in enumerate(files[:MAX_FILE_ROWS]):
            key = path_key(rel)
            tag = ""
            if not mod.enabled:
                status, tag = "비활성", "pending"
            elif is_root_pak(rel):
                target = by_source.get((mod.id, entries[rel][1] if rel in entries else rel))
                status = f"→ {target}" if target else "적용 대기"
                tag = "" if target else "pending"
            elif conflicts and key in conflicts.owners and conflicts.winner(key) != mod.id:
                status = f"덮어써짐 ← {names.get(conflicts.winner(key), '?')}"
                tag = "overridden"
            else:
                entry = deployed.get(key)
                applied = entry is not None and entry.get("mod") == mod.id
                status = "적용됨" if applied else "적용 대기"
                if conflicts and key in conflicts.owners:
                    status += " (우선)"
                tag = "" if applied else "pending"
            if rel in entries and entries[rel][1].startswith("matfix/"):
                status += " · 재질 복구본"
            tags = [t for t in (tag, "odd" if i % 2 else "") if t]
            self.files_tree.insert("", "end", values=(rel, status), tags=tags)
        if len(files) > MAX_FILE_ROWS:
            self.files_tree.insert("", "end", values=(f"... 외 {len(files) - MAX_FILE_ROWS}개", ""))

    def _fill_conflicts(self, mod: Mod):
        names = {x.id: x.name for x in self.manager.mods}
        chunks = [("충돌\n", "title")]
        if not mod.enabled:
            chunks.append(("비활성화된 모드는 충돌을 계산하지 않습니다.\n", "muted"))
        else:
            beats, beaten = self._conflicts.for_mod(mod.id) if self._conflicts else ({}, {})
            if not beats and not beaten:
                chunks.append(("✔  다른 활성 모드와 겹치는 루스 파일이 없습니다.\n", "ok"))
            for title, data, tag in (("이 모드가 덮어쓰는 모드", beats, None),
                                     ("이 모드를 덮어쓰는 모드", beaten, "warn")):
                if not data:
                    continue
                chunks.append((f"{title}\n", "head"))
                for other, paths in data.items():
                    chunks.append((f"•  {names.get(other, other)}", ("row", tag) if tag else "row"))
                    chunks.append((f"   {len(paths)}개 파일\n", "muted"))
                    for p in paths[:8]:
                        chunks.append((f"     {p}\n", "dim"))
                    if len(paths) > 8:
                        chunks.append((f"     ... 외 {len(paths) - 8}개\n", "dim"))
        if "pak" in self.manager.mod_kinds(mod.id):
            chunks.append(("\nPAK 모드끼리의 충돌은 파일 내용을 열어 보지 않으면 알 수 없습니다. "
                           "같은 대상을 바꾸는 PAK 모드가 있다면 목록 아래쪽 모드가 우선 적용됩니다.\n", "dim"))
        self._set_text(self.conflict_text, chunks)

    # ------------------------------------------------------------------ logging
    def _append_log(self, level: int, msg: str):
        tag = "error" if level >= logging.ERROR else "warn" if level >= logging.WARNING else "info"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("%H:%M:%S  "), "time")
        self.log_text.insert("end", msg + "\n", tag)
        lines = int(self.log_text.index("end-1c").split(".")[0]) - 1
        if lines > LOG_LINES:
            self.log_text.delete("1.0", f"{lines - LOG_LINES + 1}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def toggle_log(self):
        if self._log_visible:
            self.log_frame.pack_forget()
            self.log_button.configure(text="로그 보기")
        else:
            self.log_frame.pack(side="bottom", fill="x", after=self.status_sep)
            self.log_button.configure(text="로그 숨기기")
        self._log_visible = not self._log_visible

    # ------------------------------------------------------------------ background tasks
    def can_mutate(self) -> bool:
        if self.busy in self.MUTATING_OK_DURING and not self._installing:
            return True
        self.root.bell()
        self.status_var.set("작업이 끝난 뒤에 다시 시도하세요.")
        return False

    def _idle(self) -> bool:
        """True when no background work is running; otherwise tell the user to wait."""
        if self.busy is None and not self._installing:
            return True
        self.root.bell()
        self.status_var.set("작업이 끝난 뒤에 다시 시도하세요.")
        return False

    def run_task(self, kind: str, label: str, func, on_done=None, on_error=None):
        self.busy = kind
        self.status_var.set(label)
        self.progress.pack(side="right", padx=(0, self.theme.px(14)), before=self.log_button)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        for key in ("add", "folder", "remove", "apply", "rename"):
            self.buttons[key].state(["disabled"])

        def progress(done, total, msg):
            self.events.put(("progress", done, total, msg))

        def worker():
            try:
                result = func(progress)
            except BaseException as exc:  # report everything to the UI thread
                self.events.put(("error", on_error, exc, traceback.format_exc()))
            else:
                self.events.put(("done", on_done, result))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_task(self):
        self.busy = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.progress.pack_forget()
        for b in self.buttons.values():
            b.state(["!disabled"])

    def _poll_events(self):
        try:
            while not self._closed:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._append_log(event[1], event[2])
                elif kind == "call":  # result of a small background job
                    event[1](*event[2:])
                elif kind == "progress":
                    _, done, total, msg = event
                    if str(self.progress.cget("mode")) != "determinate":
                        self.progress.stop()
                        self.progress.configure(mode="determinate")
                    self.progress.configure(maximum=max(total, 1), value=done)
                    self.status_var.set(msg if len(msg) < 90 else "…" + msg[-88:])
                elif kind == "done":
                    self._finish_task()
                    self.status_var.set(self._status_after_task or "준비")
                    self._status_after_task = ""
                    if event[1]:
                        event[1](event[2])
                    self._after_task()
                elif kind == "error":
                    _, handler, exc, tb = event
                    self._finish_task()
                    self.status_var.set(" · ".join(filter(None, ("오류가 발생했습니다", self._status_after_task))))
                    self._status_after_task = ""
                    if not isinstance(exc, EXPECTED_ERRORS):
                        log.error("예상하지 못한 오류:\n%s", tb)
                    if handler:
                        handler(exc)
                    else:
                        self.show_error("오류", str(exc))
                    self._after_task()
        except queue.Empty:
            pass
        finally:  # also after a handler failed: the events after it still come
            if not self._closed:
                self.root.after(80, self._poll_events)

    def _after_task(self):
        if self._closed:
            return
        if self.busy is None and self._close_after_task:
            self._destroy()
            return
        if self.busy is None and self._apply_pending and not self._installing:
            self._apply_pending = False
            self.apply()
        self._update_header()

    def show_error(self, title: str, message: str):
        log.error("%s", message)
        messagebox.showerror(title, message, parent=self.root)

    # ------------------------------------------------------------------ startup
    def _startup(self):
        m = self.manager
        if self._bisect_resume:  # (the search kept in bisect.json; one started meanwhile needs nothing of this)
            self._bisect_resume = False
            self._resume_bisect()
        self._apply_pending_restore()  # (a search ended while the game ran: REFramework's setting back)
        if not m.game_dir:
            found = m.detect_game_dir()
            if found:
                self._set_game_dir(found)
                log.info("게임 폴더를 자동으로 찾았습니다: %s", found)
            else:
                messagebox.showinfo(APP_TITLE, "몬스터 헌터 와일즈 설치 폴더를 찾지 못했습니다.\n"
                                               "오른쪽 위 '⌂ 게임 폴더 지정'에서 MonsterHunterWilds.exe가 있는 "
                                               "폴더를 선택하세요.", parent=self.root)
            return
        if not m.game_dir.is_dir():
            log.warning("게임 폴더가 없습니다: %s", m.game_dir)
            return
        # before anything is applied: the records go with the mods as they are; then (after the material
        # check of the mods has started) the slow part of 실행 전 점검 looks at them
        self.collect_crashes(lambda: self.root.after(PRECHECK_DELAY, self.precheck_background))
        self._watch_launched = None
        self.watcher.game_dir = m.game_dir
        self.watcher.attach()  # a game started before the manager: how it ends
        if self.busy == "deploy":  # (the folder is being changed: verifying it now would see half of it)
            return
        try:
            problems = m.verify(include_outdated=False)
            outdated = m.outdated_paks()
        except Exception as exc:
            log.warning("적용 상태를 점검하지 못했습니다: %s", exc)
            return
        if outdated:
            log.info("게임이 읽지 않는 예전 방식(패치 번호)의 PAK %d개를 %s 폴더로 옮겨야 합니다.", len(outdated), PAK_MODS_DIR)
            if m.settings.auto_apply and not problems:
                m.dirty = True
                self.apply()  # only renames our own PAKs
                return
            problems = m.verify()
        if problems:
            for p in problems[:20]:
                log.warning(p)
            m.dirty = True
            m.save()
            self._update_header()
            shown = "\n".join(f"• {p}" for p in problems[:5]) + (f"\n… 외 {len(problems) - 5}개" if len(problems) > 5 else "")
            if messagebox.askyesno(APP_TITLE, f"게임 폴더에서 모드 적용 문제 {len(problems)}개를 찾았습니다.\n\n{shown}\n\n"
                                              "지금 다시 적용할까요?", parent=self.root):
                self.apply()

    # ------------------------------------------------------------------ game folder
    def browse_game(self):
        if self._search_blocks() or not self._idle():
            return
        initial = str(self.manager.game_dir) if self.manager.game_dir else None
        path = filedialog.askdirectory(parent=self.root, title="MonsterHunterWilds.exe가 있는 폴더 선택", initialdir=initial)
        if not path:
            return
        if not steam.is_game_dir(path) and not messagebox.askyesno(
                "게임 폴더", f"선택한 폴더에 {GAME_EXE}이(가) 없습니다.\n그래도 이 폴더를 사용할까요?", parent=self.root):
            return
        self._set_game_dir(Path(path))

    def detect_game(self):
        if self._search_blocks() or not self._idle():
            return
        found = self.manager.detect_game_dir()
        if not found:
            messagebox.showinfo("자동 감지", "Steam 라이브러리에서 게임을 찾지 못했습니다. '다른 폴더 선택...'으로 지정하세요.",
                                parent=self.root)
            return
        if self.manager.game_dir and fsutil.same_path(found, self.manager.game_dir):
            messagebox.showinfo("자동 감지", f"이미 설정된 폴더입니다:\n{found}", parent=self.root)
            return
        self._set_game_dir(found)

    def _set_game_dir(self, path: Path):
        def done(report):
            log.info("게임 폴더: %s", path)
            if report is not None:
                log.info("이전 게임 폴더에서 모드를 해제했습니다 (%s)", report.summary())
            self.refresh()
            self.request_apply()
            self.check_materials_background()
            self.warm_equipment()
            self.collect_crashes(lambda: self.root.after(PRECHECK_DELAY, self.precheck_background))

        self.run_task("game", "게임 폴더 변경 중...", lambda p: self.manager.set_game_dir(path, p), on_done=done)

    # ------------------------------------------------------------------ install
    def add_files(self):
        if self._search_blocks() or not self.can_mutate():
            return
        paths = filedialog.askopenfilenames(parent=self.root, title="모드 파일 선택", filetypes=INSTALL_FILETYPES)
        if paths:
            self.add_paths(list(paths))

    def add_folder(self):
        if self._search_blocks() or not self.can_mutate():
            return
        path = filedialog.askdirectory(parent=self.root, title="모드 폴더 선택")
        if path:
            self.add_paths([path])

    def add_paths(self, paths: list[str]):
        if self._search_blocks():
            return
        self._install_queue.extend(paths)
        if self._installing:
            return
        if self.busy is None:
            self._next_install()
        else:
            # something else is running; start once it finishes
            self.root.after(200, self._wait_then_install)

    def _wait_then_install(self):
        if self.busy is None and not self._installing:
            self._next_install()
        elif not self._installing:
            self.root.after(200, self._wait_then_install)

    def _next_install(self):
        if not self._install_queue:
            self._installing = False
            self.refresh()
            self._flush_requirement_notices()
            self.check_materials_background()
            self.warm_equipment()
            if self._apply_pending:
                self._apply_pending = False
                self.apply()
            return
        self._installing = True
        path = self._install_queue.pop(0)
        name = Path(path).name

        def failed(exc):
            self.show_error("설치 실패", f"{name}\n\n{exc}")
            self._next_install()

        self.run_task("install", f"압축 푸는 중: {name}", lambda p: self.manager.prepare_install(path),
                      on_done=self._on_prepared, on_error=failed)

    def _on_prepared(self, session: InstallSession):
        variants = session.variants
        name = session.source.name
        if not variants:
            raw = session.raw_variant()
            if not raw.files or not messagebox.askyesno(
                    "모드 구조를 알 수 없음",
                    f"'{name}'에서 natives, reframework, PAK 같은 모드 파일을 찾지 못했습니다.\n\n"
                    "압축 파일 내용을 게임 폴더에 그대로 설치할까요?", parent=self.root):
                session.cleanup()
                self.root.after(1, self._next_install)
                return
            variants = [raw]

        package = package_name(variants) if len(variants) > 1 else variants[0].name
        existing = self.manager.find_existing(package, session.source.name)
        replace_id = None
        if existing:
            why = (f"'{existing.name}' 모드가 이미 있습니다." if self.manager.find_by_name(package) is existing else
                   f"'{existing.name}' 모드와 같은 Nexus 모드 페이지에서 받은 같은 이름의 파일입니다.")
            answer = messagebox.askyesnocancel(
                "이미 설치된 모드",
                f"{why}\n\n예: 기존 모드를 새 파일로 교체 (업데이트)\n아니요: 별도의 모드로 추가\n취소: 설치하지 않음",
                parent=self.root)
            if answer is None:
                session.cleanup()
                self.root.after(1, self._next_install)
                return
            replace_id = existing.id if answer else None

        def work(progress):
            try:
                progress(0, 1, f"설치 중: {package}")
                return self.manager.install_package(session, variants, replace_id)
            finally:
                session.cleanup()

        def done(mod):
            for v in variants:
                if v.skipped:
                    log.info("  '%s'에서 게임에 필요 없는 파일 %d개는 제외했습니다 (예: %s)", v.name, len(v.skipped), v.skipped[0])
            self.refresh(select=mod.id)
            if mod.options:
                self.show_options(mod.id)
            if mod.enabled:
                self.request_apply()
            self._check_requirements_of(mod)
            self._first_check.add(mod.id)
            self.root.after(1, self._next_install)

        def failed(exc):
            self.show_error("설치 실패", f"{name}\n\n{exc}")
            self.refresh()
            self.root.after(1, self._next_install)

        self.run_task("install", f"설치 중: {name}", work, on_done=done, on_error=failed)

    # ------------------------------------------------------------------ edits
    def toggle(self, ids: list[str]):
        if not ids or self._search_blocks(loud=False) or not self.can_mutate():
            return
        mods = [self.manager.get(i) for i in ids]
        target = not all(m.enabled for m in mods)
        for m in mods:
            self.manager.set_enabled(m.id, target)
        self._changed()

    def set_enabled_all(self, enabled: bool):
        if not self.manager.mods or self._search_blocks() or not self.can_mutate():
            return
        for m in self.manager.mods:
            self.manager.set_enabled(m.id, enabled)
        self._changed()

    def move_selected(self, delta: int):
        ids = self._selected_ids()
        if len(ids) != 1 or self._search_blocks(loud=False) or not self.can_mutate():
            return
        self.manager.move(ids[0], delta)
        self._changed(select=ids[0])

    def move_to_edge(self, top: bool):
        ids = self._selected_ids()
        if len(ids) != 1 or self._search_blocks(loud=False) or not self.can_mutate():
            return
        self.manager.move_to(ids[0], 0 if top else len(self.manager.mods) - 1)
        self._changed(select=ids[0])

    def rename_selected(self):
        mod = self._selected_mod()
        if mod is None or not self.can_mutate():
            return
        name = TextDialog(self.root, self.theme, "이름 변경", "새 이름", mod.name).show()
        if name and name.strip() and name.strip() != mod.name:
            self.manager.rename(mod.id, name)
            self.refresh(select=mod.id)

    def remove_selected(self):
        ids = self._selected_ids()
        if not ids or self._search_blocks() or not self._idle():
            return
        mods = [self.manager.get(i) for i in ids]
        names = "\n".join(f"  • {m.name}" for m in mods[:10]) + ("\n  ..." if len(mods) > 10 else "")
        if self.manager.settings.confirm_remove and not messagebox.askyesno(
                "모드 삭제", f"다음 모드를 삭제할까요? 저장된 모드 파일도 지워집니다.\n\n{names}", parent=self.root):
            return
        any_enabled = any(m.enabled for m in mods)

        def work(progress):
            for i, m in enumerate(mods):
                progress(i, len(mods), f"삭제 중: {m.name}")
                self.manager.remove(m.id)

        def done(_):
            for m in mods:
                log.info("모드 삭제: %s", m.name)
            self.refresh()
            if any_enabled:
                self.request_apply()

        self.run_task("remove", "삭제 중...", work, on_done=done)

    def _toggle_auto_apply(self):
        self.manager.update_settings(auto_apply=self.auto_var.get())
        if self.auto_var.get() and self.manager.dirty:
            self.request_apply()

    def _changed(self, select: str | None = None):
        self.refresh(select=select)
        self.request_apply()

    def request_apply(self):
        if not self.manager.settings.auto_apply or not self.manager.game_dir:
            self._update_header()
            return
        if self.busy or self._installing:
            self._apply_pending = True
        else:
            self.root.after(150, self._apply_if_idle)

    def _apply_if_idle(self):
        if self.busy or self._installing:
            self._apply_pending = True
        else:
            self.apply()

    # ------------------------------------------------------------------ deploy
    def apply(self):
        m = self.manager
        if not m.game_dir:
            messagebox.showwarning("게임에 적용", "먼저 게임 폴더를 지정하세요.", parent=self.root)
            return
        if self.busy:
            self._apply_pending = True
            return
        sources, revision = m.prepare_deploy()

        def done(report: DeployReport):
            m.finish_deploy(report, revision)
            self._log_report(report)
            self.refresh(select=(self._selected_ids() or [None])[0])
            if report.errors:
                self._launch_after_apply = False
                more = f"\n... 외 {len(report.errors) - 8}개" if len(report.errors) > 8 else ""
                self.show_error("일부 파일을 적용하지 못했습니다",
                                "\n".join(report.errors[:8]) + more +
                                "\n\n게임이 실행 중이라면 종료한 뒤 다시 '게임에 적용'을 누르세요.")
            elif self._launch_after_apply and not self._apply_pending and not m.dirty:
                self._launch_after_apply = False
                # (a window opened here would hold up the events)
                self.root.after_idle(lambda: self._precheck_then_start(applied=True))
            elif not self._launch_after_apply:
                self.precheck_background()  # (when due: a game update, a new record)
            if self._refw_status is not None:
                self.check_refw(manual=False, builds=self._refw_status.builds)
            if self._bisect is not None:  # the step is in the game folder now (or not: its line says)
                self._show_bisect_bar()

        def failed(exc):
            self._launch_after_apply = False
            self.show_error("게임에 적용", str(exc))
            self._update_header()
            if self._bisect is not None:
                self._show_bisect_bar()

        self.run_task("deploy", "게임 폴더에 적용 중...", lambda p: m.run_deploy(sources, p), on_done=done, on_error=failed)

    def _log_report(self, report: DeployReport):
        if report.changed or report.errors:
            log.info("적용 완료: %s", report.summary())
        for target, mod_name, src in report.paks:
            log.info("  PAK %s ← %s", target, mod_name)
        for w in report.warnings:
            log.warning(w)
        for e in report.errors[:20]:
            log.error(e)

    def purge(self):
        m = self.manager
        if not m.game_dir or self._search_blocks() or not self._idle():
            return
        if not messagebox.askyesno("모드 파일 제거", "게임 폴더에서 이 프로그램이 넣은 모든 모드 파일을 제거하고 "
                                               "덮어썼던 원본 파일을 복원합니다.\n(설치된 모드 목록은 그대로 유지됩니다)\n\n진행할까요?",
                                   parent=self.root):
            return
        if m.settings.auto_apply:
            m.update_settings(auto_apply=False)
            self.auto_var.set(False)
            log.info("'즉시 적용'을 껐습니다. 다시 적용하려면 '게임에 적용'을 누르세요.")

        def done(report):
            self._log_report(report)
            log.info("게임 폴더에서 모드 파일을 제거했습니다.")
            self.refresh()

        self.run_task("purge", "모드 파일 제거 중...", lambda p: m.purge(p), on_done=done)

    def verify(self):
        if not self.manager.game_dir:
            return
        problems = self.manager.verify()
        if not problems:
            messagebox.showinfo("적용 상태 점검", "적용된 모드 파일에 문제가 없습니다.", parent=self.root)
            return
        for p in problems:
            log.warning(p)
        self.manager.dirty = True
        self.manager.save()
        self._update_header()
        if messagebox.askyesno("적용 상태 점검", f"문제 {len(problems)}개를 찾았습니다.\n\n" + "\n".join(problems[:8]) +
                               "\n\n다시 적용할까요?", parent=self.root):
            self.apply()

    # ------------------------------------------------------------------ misc actions
    def launch_game(self):
        """Start the game: first copy the last game's REFramework records (starting the game overwrites
        them), then apply what is left to apply."""
        if self._launching or self._prechecking:
            return
        self._launching = True
        self._precheck_answered = None
        self._unapplied_ok = None
        if self.busy is None:
            self.status_var.set("게임 기록 확인 중...")
        self.collect_crashes(self._continue_launch)

    def _continue_launch(self):
        self._launching = False
        if self.busy is None:
            self.status_var.set("준비")
        m = self.manager
        if m.game_dir and not m.dirty and m.outdated_paks():
            log.warning("게임이 읽지 않는 예전 방식의 PAK이 있어 %s 폴더로 옮깁니다.", PAK_MODS_DIR)
            m.dirty = True
            m.save()
            self._update_header()
        session = self._bisect
        if session is not None:
            if not self._bisect_ready():  # (its next launch is being worked out)
                self.status_var.set(BISECT_COMPUTING)
                return
            reason = bisect.problem(m, session)
            if reason:
                self._abort_bisect(reason)
                return
            if isinstance(session.next(), bisect.Result):
                self.show_bisect_result()
                return
            if m.dirty and m.game_dir:  # the step goes into the game folder first, whatever the settings say
                log.info("원인 찾기 단계를 적용한 뒤 게임을 실행합니다.")
                self._launch_after_apply = True
                self.apply()
                return
        if m.dirty and m.game_dir:
            if m.settings.apply_before_launch:
                log.info("남은 변경사항을 적용한 뒤 게임을 실행합니다.")
                self._launch_after_apply = True
                self.apply()
                return
            if not messagebox.askyesno("게임 실행", "아직 게임에 적용하지 않은 변경사항이 있습니다.\n그대로 실행할까요?",
                                       parent=self.root):
                return
            self._unapplied_ok = m.revision  # (not asked again before the start unless something changes)
        self._precheck_then_start()

    def _start_game(self):
        url = f"steam://rungameid/{STEAM_APP_ID}"
        self.manager.note_launch()  # the session this starts gets the mods it runs with
        launched = time.time()
        try:
            if sys.platform == "win32":
                os.startfile(url)  # type: ignore[attr-defined]
            else:
                webbrowser.open(url)
            log.info("Steam으로 게임을 실행합니다.")
        except OSError:
            exe = self.manager.game_dir / GAME_EXE if self.manager.game_dir else None
            if exe and exe.is_file():
                os.startfile(str(exe))  # type: ignore[attr-defined]
            else:
                self.show_error("게임 실행", "Steam을 실행할 수 없습니다.")
                return
        session = self._bisect
        if session is not None and self._bisect_ready() and isinstance(session.next(), bisect.Step):
            session.mark_launched(launched)
            self._bisect_detection = None
            self._show_bisect_bar()
        self._watch_game(launched)
        if self.manager.settings.minimize_on_launch:
            self.root.iconify()

    def open_game_folder(self):
        if self.manager.game_dir and self.manager.game_dir.is_dir():
            fsutil.open_in_explorer(self.manager.game_dir)

    def open_mod_folder(self):
        mod = self._selected_mod()
        if mod:
            fsutil.open_in_explorer(self.manager.mod_dir(mod.id) if mod.options else self.manager.files_dir(mod.id))

    def open_settings(self):
        old_theme, old_key = self.manager.settings.theme, self.manager.settings.nexus_api_key
        if SettingsDialog(self.root, self.theme, self.manager).show():
            self.auto_var.set(self.manager.settings.auto_apply)
            if self.manager.settings.theme != old_theme:
                self.apply_theme(self.manager.settings.theme)
            else:
                self.refresh()
            if self.manager.dirty:
                self.request_apply()
            if self.manager.settings.nexus_api_key and self.manager.settings.nexus_api_key != old_key:
                self.check_mod_updates(manual=False)  # a new key: look for updates right away

    def show_help(self, topic: str | None = None):
        HelpDialog(self.root, self.theme, topic).show()

    # ------------------------------------------------------------------ updates
    def check_updates(self, manual: bool):
        """Look for a newer release in the background."""
        def worker():
            try:
                release = updater.check_for_update()
            except UpdateError as exc:
                self.events.put(("call", self._on_update_checked, None, exc, manual))
            else:
                self.events.put(("call", self._on_update_checked, release, None, manual))

        if manual:
            self.status_var.set("새 버전 확인 중...")
        threading.Thread(target=worker, daemon=True).start()

    def _on_update_checked(self, release: Release | None, error: Exception | None, manual: bool):
        if manual:
            self.status_var.set("준비")
        if error is not None:
            log.info("업데이트 확인 실패: %s", error)
            if manual:
                messagebox.showwarning("업데이트 확인", str(error), parent=self.root)
            return
        if release is None:
            if manual:
                messagebox.showinfo("업데이트 확인", f"최신 버전(v{__version__})을 쓰고 있습니다.", parent=self.root)
            return
        self._update = release
        self.update_button.configure(text=f"⬆  v{release.version} 업데이트")
        if not self.update_button.winfo_manager():
            after = self.mods_badge if self.mods_badge.winfo_manager() else self.refw_badge
            self.update_button.pack(side="right", padx=(0, self.theme.px(10)), after=after)
        log.info("새 버전 v%s이 나왔습니다. 오른쪽 위 '업데이트' 버튼을 누르세요.", release.version)
        if manual:
            self.show_update()

    def show_update(self):
        release = self._update
        if release is None:
            return
        choice = UpdateDialog(self.root, self.theme, release, updater.can_self_update()).show()
        if choice == "page":
            webbrowser.open(release.page_url)
        elif choice == "install":
            self.install_update(release)

    def install_update(self, release: Release):
        if not self._idle():
            return
        target = updater.current_exe()
        staged = updater.staged_path(target)

        def done(path):
            messagebox.showinfo("업데이트", f"v{release.version}을 내려받았습니다.\n"
                                          "프로그램을 다시 시작해 업데이트를 적용합니다.", parent=self.root)
            try:
                updater.launch_finisher(path, target)
            except OSError as exc:
                self.show_error("업데이트", f"업데이트를 시작하지 못했습니다: {exc}")
                return
            log.info("v%s로 업데이트하기 위해 다시 시작합니다.", release.version)
            self._destroy()

        self.run_task("update", "업데이트 다운로드 중...", lambda p: updater.download(release, staged, p),
                      on_done=done, on_error=lambda exc: self.show_error("업데이트", str(exc)))

    def show_about(self):
        messagebox.showinfo("정보", f"{APP_TITLE}\n버전 {__version__}\n\n데이터 폴더:\n{self.manager.data_dir}\n\n"
                                    f"{matfix.CREDIT}", parent=self.root)

    # ------------------------------------------------------------------ tree interaction
    def _on_press(self, event):
        self._drag = None
        if self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if self.tree.identify_column(event.x) == "#0":
            if item in self.tree.selection() and len(self.tree.selection()) > 1:
                self.toggle(self._selected_ids())
            else:
                self.tree.selection_set(item)
                self.toggle([item])
            return "break"
        self._drag = {"item": item, "y": event.y, "moved": False}

    def _on_motion(self, event):
        d = self._drag
        if not d or len(self.tree.selection()) > 1 or self._query() or self.filter_var.get() != FILTER_ALL:
            return
        if not d["moved"] and abs(event.y - d["y"]) < 8:
            return
        if self._bisect is not None or self._bisect_preparing:  # the order stays while 원인 모드 찾기 runs
            if not d.get("told"):
                d["told"] = self._search_blocks(loud=False)
            return
        if self.busy not in self.MUTATING_OK_DURING or self._installing:
            return
        target = self.tree.identify_row(event.y)
        if target and target != d["item"]:
            self.tree.move(d["item"], "", self.tree.index(target))
            d["moved"] = True
        self.tree.configure(cursor="sb_v_double_arrow")

    def _on_release(self, event):
        d, self._drag = self._drag, None
        self.tree.configure(cursor="")
        if d and d["moved"]:
            self.manager.move_to(d["item"], self.tree.index(d["item"]))
            self._changed(select=d["item"])

    def _on_double(self, event):
        if self.tree.identify_region(event.x, event.y) not in ("cell", "tree") or self.tree.identify_column(event.x) == "#0":
            return
        item = self.tree.identify_row(event.y)
        if item:
            self.toggle([item])

    def _on_context(self, event):
        item = self.tree.identify_row(event.y)
        if item and item not in self.tree.selection():
            self.tree.selection_set(item)
        ids = self._selected_ids()
        menu = self.context_menu
        menu.delete(0, "end")
        if ids:
            mods = [self.manager.get(i) for i in ids]
            menu.add_command(label="비활성화" if all(m.enabled for m in mods) else "활성화",
                             command=lambda: self.toggle(ids))
            if len(ids) == 1:
                if mods[0].options:
                    menu.add_command(label="옵션 선택...", command=lambda: self.show_options(ids[0]))
                menu.add_separator()
                menu.add_command(label="맨 위로", command=lambda: self.move_to_edge(True))
                menu.add_command(label="맨 아래로 (최우선)", command=lambda: self.move_to_edge(False))
                menu.add_command(label="이름 변경", command=self.rename_selected)
                menu.add_command(label="저장된 모드 폴더 열기", command=self.open_mod_folder)
                menu.add_command(label="장비 외형 보기", command=lambda: self.show_equipment(ids[0]))
                if self.manager.mod_kinds(ids[0]) & {"loose", "pak"}:  # (the dialog says when nothing can be moved)
                    menu.add_command(label=RETARGET_ACTION, command=lambda: self.open_retarget(ids[0]))
                if mods[0].origin != refw.ORIGIN:
                    menu.add_separator()
                    if mods[0].update:
                        menu.add_command(label="새 버전 받기 (Nexus 파일 페이지)",
                                         command=lambda: self._open_nexus(lambda m: nexus.FILES_URL.format(m.nexus_id)))
                    if mods[0].nexus_id:
                        menu.add_command(label="Nexus 페이지 열기", command=lambda: self._open_nexus(lambda m: m.nexus_url))
                    menu.add_command(label="Nexus 링크 설정...", command=self.link_nexus)
            states = {i: self.manager.material_state(i) for i in ids}
            with_materials = [i for i in ids if states[i] != "none"]
            if with_materials:
                menu.add_separator()
                menu.add_command(label="재질 점검", command=lambda: self.check_materials(with_materials))
                fixable = [i for i in with_materials if states[i] in ("stale", "declined")]
                if fixable:
                    menu.add_command(label="재질 복구", command=lambda: self.repair_materials(fixable))
                if len(ids) == 1 and self.manager.repaired_materials(ids[0]):
                    menu.add_command(label="원래 재질로 되돌리기", command=lambda: self.restore_materials(ids[0]))
                if len(ids) == 1:
                    menu.add_command(label="재질 진단 파일 저장...", command=lambda: self.export_material_report(ids[0]))
            menu.add_separator()
            menu.add_command(label="삭제", command=self.remove_selected)
        else:
            menu.add_command(label="모드 추가...", command=self.add_files)
        menu.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------------------ materials
    def _modal_open(self) -> bool:
        try:
            return self.root.grab_current() is not None
        except (tk.TclError, KeyError):
            return False

    def _material_action(self, action: str):
        mod = self._selected_mod()
        if mod is None:
            return
        if action == "check":
            self.check_materials([mod.id])
        elif action == "repair":
            self.repair_materials([mod.id])
        elif action == "report":
            self.export_material_report(mod.id)
        else:
            self.restore_materials(mod.id)

    def _materials_ready(self) -> bool:
        if not self._idle():
            return False
        if self._matfix_running:
            self.root.bell()
            self._matfix_line = "재질 점검이 끝난 뒤에 다시 시도하세요."  # (the check's end puts back "준비")
            self.status_var.set(self._matfix_line)
            return False
        if not self.manager.game_dir:
            messagebox.showwarning("재질", "먼저 게임 폴더를 지정하세요.", parent=self.root)
            return False
        if not matfix.available():
            messagebox.showerror("재질", "게임 파일의 압축(zstd)을 풀 수 있는 모듈이 없어 재질을 점검할 수 없습니다.",
                                 parent=self.root)
            return False
        return True

    def check_materials_background(self):
        """Check the mods not checked against the current game yet, without blocking (startup, installs)."""
        m = self.manager
        if self._closed or not m.settings.check_materials or not matfix.available() \
                or not m.game_dir or not m.game_dir.is_dir():
            return
        if self._matfix_running:
            self._matfix_again = True
            return
        if self.busy == "matfix" or self._precheck_reading:  # one reader of the game's files at a time
            self.root.after(2000, self.check_materials_background)
            return
        jobs = m.material_jobs(unchecked_only=True)
        if not jobs:
            self._offer_materials()
            return
        self._matfix_running = True

        def progress(done, total, msg):
            self.events.put(("call", self._matfix_status, msg))

        def worker():
            try:
                run = m.run_materials(jobs, progress=progress)
            except Exception as exc:  # reported in the log only: nobody asked for this check
                self.events.put(("call", self._on_materials_checked, None, jobs, exc))
            else:
                self.events.put(("call", self._on_materials_checked, run, jobs, None))

        log.info("재질 점검: 모드 %d개를 현재 게임과 비교합니다", len(jobs))
        threading.Thread(target=worker, daemon=True).start()

    def _matfix_status(self, msg: str):
        """The background check's progress, over "준비" or its own line only: a line that tells what was
        done (새 모드를 만들었습니다...) stays."""
        if self.busy is None and self._matfix_running and self.status_var.get() in ("준비", self._matfix_line):
            self._matfix_line = msg if len(msg) < 90 else "…" + msg[-88:]
            self.status_var.set(self._matfix_line)

    def _on_materials_checked(self, run: MaterialRun | None, jobs: list[MaterialJob], error: Exception | None):
        if self._closed:
            return
        if self.busy is not None or self._installing:  # stored only while nothing else touches the mods
            self.root.after(500, lambda: self._on_materials_checked(run, jobs, error))
            return
        self._matfix_running = False
        if self._matfix_line and self.status_var.get() == self._matfix_line:  # (another line stays)
            self.status_var.set("준비")
        self._matfix_line = ""
        if error is not None:
            log.warning("재질을 점검하지 못했습니다: %s", error)
        else:
            changed = self.manager.finish_materials(run, jobs)
            self._log_materials(run, jobs)
            self._log_shader_requirements(run, jobs)
            self.refresh()
            if any(mod.enabled for mod in changed):
                self.request_apply()
            if run.registry_updated:
                self._matfix_again = True  # check what could not be repaired again with the new data
            fresh = [j.mod_id for j in jobs if j.mod_id in self._first_check and self._checked_now(run, j)
                     and not self._waits_for_options(j.mod_id)]  # (those are told once options are chosen)
            self._first_check -= set(fresh)
            self._notify_shader_requirements(fresh)
        if self._matfix_again:
            self._matfix_again = False
            self.check_materials_background()
        else:
            self._offer_materials()

    def _offer_materials(self):
        """Ask once per game version about the mods whose materials no longer fit."""
        if self._offer_scheduled or self._closed or not self.manager.materials_to_offer():
            return
        if self._bisect is not None:  # while 원인 모드 찾기 runs the repair waits (offered at a later check)
            return
        if self.busy is not None or self._installing or self._modal_open():
            self._offer_scheduled = True

            def again():
                self._offer_scheduled = False
                self._offer_materials()

            self.root.after(1000, again)
            return
        mods = self.manager.materials_to_offer()
        self.manager.mark_materials_offered(mods)
        chosen = MaterialsDialog(self.root, self.theme, self.manager, mods).show()
        if chosen:
            self.repair_materials(chosen)

    def _log_materials(self, run: MaterialRun, jobs: list[MaterialJob]):
        names = {j.mod_id: j.name for j in jobs}
        for mod_id, error in run.errors.items():
            log.warning("'%s': 재질을 점검하지 못했습니다: %s", names[mod_id], error)
        for mod_id, results in run.results.items():
            for r in results:
                where = f"'{names[mod_id]}' {r.rel}"
                moved = f" → {r.new_rel}" if r.new_rel and r.new_rel != r.rel else ""
                if r.state == "error":
                    log.warning("%s: 읽거나 고치지 못했습니다: %s", where, r.error)
                elif r.state == "fixable" and (r.fixed or r.kind == "mdf2"):
                    what = ", ".join(r.fixed) or "버전 번호"
                    if run.repair:
                        log.info("%s: 재질을 복구했습니다 (%s)%s", where, what, "" if r.rsz else moved)
                    else:
                        log.warning("%s: 재질이 현재 게임과 맞지 않습니다 (%s)", where, what)
                for item in r.rsz:
                    target = f"'{names[mod_id]}' {item['label']}"
                    if item.get("reason"):
                        log.warning("%s: %s 복구 불가 (%s)", target, item["kind"], item["reason"])
                        if item.get("detail"):
                            log.info("%s: 자세히 - %s", target, item["detail"])
                    elif r.state == "error":
                        continue
                    elif run.repair:
                        log.info("%s: %s 복구 (%s)%s", target, item["kind"], item["method"],
                                 moved if r.kind != "pak" else "")
                    else:
                        log.warning("%s: %s 파일이 현재 게임과 맞지 않습니다 (%s(으)로 고칠 수 있음)", target,
                                    item["kind"], item["method"])
                foreign = {name for name, _mmtr in r.shaders}
                plain = [name for name in r.unresolved if name not in foreign]
                if plain:
                    log.info("%s: 현재 게임에서 같은 셰이더를 쓰는 재질을 찾지 못해 그대로 둔 재질: %s", where, ", ".join(plain))
                if foreign:
                    shaders = sorted({mmtr.rsplit("/", 1)[-1] for _name, mmtr in r.shaders}, key=str.casefold)
                    log.info("%s: 게임에 없는 셰이더(%s)를 쓰는 재질이라 그대로 둡니다: %s", where, ", ".join(shaders),
                             ", ".join(n for n in r.unresolved if n in foreign))
                if run.repair:
                    for note in r.notes:
                        log.info("%s: %s", where, note)

    def _waits_for_options(self, mod_id: str) -> bool:
        try:
            return self.manager.get(mod_id).needs_option
        except KeyError:
            return False

    def _checked_now(self, run: MaterialRun, job: MaterialJob) -> bool:
        """The run's result for the job was stored (False: it failed, or the mod was reinstalled meanwhile; a
        removed mod counts as done)."""
        try:
            mod = self.manager.get(job.mod_id)
        except KeyError:
            return True
        return job.mod_id in run.results and f"{mod.installed_at}|{mod.updated_at}" == job.stamp

    def _log_shader_requirements(self, run: MaterialRun, jobs: list[MaterialJob]):
        """Where the shaders come from that the checked mods' materials use and the game does not have
        (a warning for the mods that are on)."""
        names = {j.mod_id: j.name for j in jobs}
        for mod_id in run.results:
            on = self._mod_on(mod_id)
            for req in self._shader_requirements(mod_id):
                (log.warning if on and req["state"] in ("missing", "off") else log.info)(
                    "'%s': 재질이 쓰는 %s - %s", names[mod_id], req["name"], requirement_note(req))

    def _mod_on(self, mod_id: str) -> bool:
        try:
            return self.manager.get(mod_id).enabled
        except KeyError:  # (removed meanwhile)
            return False

    def _shader_requirements(self, mod_id: str) -> list[dict]:
        """manager.shader_requirements, never raising (only a help)."""
        try:
            return self.manager.shader_requirements(mod_id)
        except Exception as exc:
            log.debug("필요한 셰이더를 확인하지 못했습니다: %s", exc)
            return []

    def _notify_shader_requirements(self, mod_ids: list[str]):
        """After the first material check of mods just installed: the shaders they need that no mod puts in
        (not while 원인 모드 찾기 runs: its profile turns mods off on purpose; then the ⚠ marks tell)."""
        if not mod_ids or self._closed or self._bisect is not None:
            return
        if self.busy is not None or self._installing or self._modal_open():
            self.root.after(1000, lambda: self._notify_shader_requirements(mod_ids))
            return
        items = []
        for mod_id in mod_ids:
            try:
                mod = self.manager.get(mod_id)
            except KeyError:
                continue
            missing = [r for r in self._shader_requirements(mod_id) if r["state"] in ("missing", "off")]
            if mod.enabled and missing:
                items.append((mod, missing))
        if items and not self._closed:
            RequirementsDialog(self.root, self.theme, items).show()

    def check_materials(self, mod_ids: list[str] | None = None):
        """Check mods (all by default) against the current game and offer to repair what is stale."""
        if not self._materials_ready():
            return
        m = self.manager
        jobs = m.material_jobs(mod_ids)
        if not jobs:
            messagebox.showinfo("재질 점검", "재질(.mdf2)이나 PAK 파일이 있는 모드가 없습니다.", parent=self.root)
            return

        def done(run: MaterialRun):
            changed = m.finish_materials(run, jobs)
            self._log_materials(run, jobs)
            self._log_shader_requirements(run, jobs)
            self.refresh()
            if any(mod.enabled for mod in changed):
                self.request_apply()
            if run.registry_updated:
                self.root.after(500, self.check_materials_background)
            stale = [m.get(j.mod_id) for j in jobs
                     if j.mod_id in run.results and m.material_state(j.mod_id) in ("stale", "declined")]
            if stale:
                m.mark_materials_offered(stale)
                chosen = MaterialsDialog(self.root, self.theme, m, stale).show()
                if chosen:
                    self.repair_materials(chosen)
                return
            repaired = sum(1 for j in jobs if m.material_state(j.mod_id) == "repaired")
            failed = len(run.errors) + sum(1 for rs in run.results.values() for r in rs if r.state == "error")
            stuck = [j.name for j in jobs if m.material_stuck(j.mod_id)]
            text = f"모드 {len(jobs)}개를 점검했습니다. 복구할 파일이 없습니다."
            if repaired:
                text += f"\n(이미 복구한 모드 {repaired}개 포함)"
            if stuck:
                text += (f"\n\n현재 게임과 맞지 않지만 고칠 수 없는 파일이 있는 모드: {', '.join(stuck[:5])}"
                         f"{' 외' if len(stuck) > 5 else ''} — 게임이 멈추면 이 모드를 끄고 업데이트를 확인하세요.")
            shaders = [f"'{j.name}': {r['name']} - {requirement_note(r)}" for j in jobs
                       if j.mod_id in run.results and self._mod_on(j.mod_id)
                       for r in self._shader_requirements(j.mod_id) if r["state"] in ("missing", "off")]
            if shaders:
                text += ("\n\n재질이 쓰는 셰이더(게임에 없어 다른 모드가 넣는 것)가 없거나 꺼져 있습니다:\n"
                         + "\n".join(shaders[:5]) + (f"\n외 {len(shaders) - 5}개" if len(shaders) > 5 else ""))
            if failed:
                text += f"\n\n읽지 못한 파일 {failed}개가 있습니다. 자세한 내용은 로그를 보세요."
            (messagebox.showwarning if stuck or shaders else messagebox.showinfo)("재질 점검", text, parent=self.root)

        self.run_task("matfix", "재질 점검 준비 중...", lambda p: m.run_materials(jobs, progress=p), on_done=done)

    def repair_materials(self, mod_ids: list[str]):
        if not mod_ids or self._search_blocks() or not self._materials_ready():
            return
        m = self.manager
        jobs = m.material_jobs(mod_ids)
        if not jobs:
            return

        def done(run: MaterialRun):
            changed = m.finish_materials(run, jobs)
            self._log_materials(run, jobs)
            self._log_shader_requirements(run, jobs)
            self.refresh(select=jobs[0].mod_id if len(jobs) == 1 else None)
            if run.registry_updated:
                self.root.after(500, self.check_materials_background)
            if any(mod.enabled for mod in changed):
                self.request_apply()
            fixed = sum(1 for j in jobs if m.material_state(j.mod_id) == "repaired")
            failed = len(run.errors) + sum(1 for rs in run.results.values() for r in rs if r.state == "error")
            if fixed:
                text = (f"모드 {fixed}개의 재질을 복구했습니다. 게임에 적용하면 바로 반영됩니다.\n\n모드의 원래 파일은 "
                        "그대로 있습니다. 문제가 생기면 모드를 오른쪽 클릭해 '원래 재질로 되돌리기'를 고르세요.")
            else:
                text = "복구한 모드가 없습니다."
            stuck = sum(len(m.material_stuck(j.mod_id)) for j in jobs)
            failed += stuck
            if failed:
                text += f"\n\n복구하지 못한 파일 {failed}개가 있습니다. 자세한 내용은 정보 탭과 로그를 보세요."
            (messagebox.showinfo if fixed and not failed else messagebox.showwarning)("재질 복구", text, parent=self.root)

        self.run_task("matfix", "재질 복구 준비 중...", lambda p: m.run_materials(jobs, repair=True, progress=p),
                      on_done=done)

    def export_material_report(self, mod_id: str):
        """Save the mod's .user/.pfb/.scn files, the game's copies and the check results to a zip."""
        if not self._materials_ready():
            return
        mod = self.manager.get(mod_id)
        jobs = self.manager.material_jobs([mod_id])
        if not jobs:
            messagebox.showinfo("재질 진단", "이 모드에는 재질이나 .user·.pfb 파일이 없습니다.", parent=self.root)
            return
        safe = re.sub(r'[\\/:*?"<>|]+', "_", mod.name).strip() or "mod"
        path = filedialog.asksaveasfilename(parent=self.root, title="재질 진단 파일 저장", defaultextension=".zip",
                                            initialfile=f"{safe}_재질진단.zip", filetypes=[("zip", "*.zip")])
        if not path:
            return

        def done(count: int):
            log.info("재질 진단 파일을 저장했습니다: %s (.user·.pfb 파일 %d개)", path, count)
            messagebox.showinfo("재질 진단", f"저장했습니다 (.user·.pfb 파일 {count}개):\n{path}", parent=self.root)

        self.run_task("matfix", "재질 진단 파일 만드는 중...",
                      lambda p: self.manager.export_material_report(jobs[0], Path(path), p), on_done=done)

    def restore_materials(self, mod_id: str):
        if self._search_blocks() or not self._idle():
            return
        mod = self.manager.get(mod_id)
        if self.manager.restore_materials(mod_id):
            log.info("'%s': 원래 재질 파일로 되돌렸습니다", mod.name)
            self._changed(select=mod_id)
        else:
            self.refresh(select=mod_id)

    # ------------------------------------------------------------------ crash records (게임 튕김 기록)
    def collect_crashes(self, then=None, notice: bool = True):
        """Copy REFramework's records of the last game sessions in the background (a game start overwrites
        them) and tell about a new crash in the notice bar (``notice``; else, for the 게임 튕김 기록 window,
        find which record is the running game's); ``then`` runs on this thread afterwards."""
        m = self.manager
        if not m.game_dir or not m.game_dir.is_dir():
            if then is not None:
                then()
            return
        try:
            deployed = m.deployed_files()
            context = m.crash_context(deployed)
            index = m.crash_index(deployed) if notice else None
        except Exception as exc:  # never in the way of starting the game
            log.warning("게임 기록을 모으지 못했습니다: %s", exc)
            if then is not None:
                then()
            return

        def worker():
            pending, live = None, ""
            try:
                with self._collect_lock:
                    m.collect_crash_evidence(context)
                if notice:
                    pending = crashes.pending_notice(m.crash_dir, index=index)
                # the running game's own record is not finished: not told about (nor explained) until it ends
                if (pending is not None or not notice) and is_game_running():
                    live = crashes.live_started(m.game_dir)
                    if pending is not None and pending.started == live:
                        pending = crashes.pending_notice(m.crash_dir, index=index, skip_started=live)
            except Exception as exc:  # never in the way of starting the game
                log.warning("게임 기록을 모으지 못했습니다: %s", exc)
            self.events.put(("call", self._crashes_collected, pending, live, then))

        threading.Thread(target=worker, daemon=True).start()

    def _crashes_collected(self, pending, live, then):
        if self._closed:
            return
        self._live_record = live
        if then is not None:  # outside the event pump: a window opened there would hold up every other event
            self.root.after_idle(lambda: None if self._closed else then())
        if pending is not None:
            try:
                if self._bisect is not None:  # a crash in a step of 원인 모드 찾기: the search asks about it itself
                    crashes.mark_notified(self.manager.crash_dir, pending)
                else:
                    self._show_notice(pending)
            except Exception:
                log.exception("게임 기록 알림을 보이지 못했습니다.")

    def _show_notice(self, session):
        """The notice bar about a crash (or files the game could not load), told once per record."""
        crashes.mark_notified(self.manager.crash_dir, session)
        self._notice_session = session.id
        self._pack_notice("⚠  " + crashes.notice_text(session), session.title())
        log.warning("%s (%s) ☰ → %s에서 볼 수 있습니다.", crashes.notice_text(session), session.title(), crashes.TITLE)

    def _pack_notice(self, title: str, detail: str, action: tuple | None = None):
        """The notice bar with its button: (text, command), by default 기록 보기 of _notice_session."""
        px = self.theme.px
        self.notice_title.configure(text=title)
        self.notice_detail.configure(text=detail)
        text, command = action or ("기록 보기", lambda: self.show_crashes(self._notice_session))
        self.notice_button.configure(text=text, command=command)
        if not self.notice.winfo_manager():
            self.notice.pack(fill="x", padx=px(20), pady=(px(10), 0), before=self.paned)

    def _notice_bisect(self):
        self._hide_notice()
        self.show_bisect()

    def _hide_notice(self):
        self.notice.pack_forget()

    def show_crashes(self, select: str | None = None):
        """The 게임 튕김 기록 window (after copying records not copied yet)."""
        if self._modal_open():
            return
        if self.busy is None:
            self.status_var.set("게임 기록 확인 중...")
        self.collect_crashes(lambda: self._open_crashes(select), notice=False)

    def _open_crashes(self, select: str | None):
        if self.busy is None:
            self.status_var.set("준비")
        if self._modal_open():
            return
        self._hide_notice()
        sessions = self.manager.crash_sessions()
        selected = next((s for s in sessions if s.id == select), None) if select else None
        status = self._refw_status
        dialog = CrashDialog(self.root, self.theme, self.manager, sessions, selected,
                             builds=status.builds if status else None,
                             refw_text=refw.describe_installed(status) if status else "", live=self._live_record,
                             searching=self._bisect is not None)
        result = dialog.show()
        if not result:
            return
        if result[0] == "show":
            self.show_mod(result[1])
        elif result[0] == "matfix":
            self.check_materials(result[1])
        elif result[0] == "refw":
            self._on_refw_click()
        elif result[0] == "bisect":
            self.show_bisect(record=result[1])

    # ------------------------------------------------------------------ 실행 전 점검 (before a launch)
    def _game_version_now(self) -> str:
        """The game's fingerprint looked at again (Steam may update the game while the manager is open):
        after an update the list shows the mods' materials as not checked yet, and their check starts."""
        m = self.manager
        before = m.game_version()
        now = m.game_version(refresh=True)
        if now != before:
            log.info("게임 파일이 바뀌었습니다 (게임 업데이트). 모드의 재질을 다시 점검합니다.")
            self.refresh()
            self.check_materials_background()
        return now

    def _precheck_facts(self, verify: bool = True):
        """precheck.Facts of the manager as it is now (this thread)."""
        self._game_version_now()
        return precheck.gather(self.manager, self._refw_status, self._conflicts, self._missing, verify=verify)

    def _precheck_report(self, facts, background):
        """precheck.check, telling it whether the mods' own material check runs now (then the mods not checked
        since a game update are said to be in check, and new ones not yet checked are left out)."""
        facts.checking = self._matfix_running
        return precheck.check(facts, background)

    def _precheck_then_start(self, applied: bool = False):
        """실행 전 점검 right before the game starts (after applying, or without): new high and medium issues
        are shown first. It never keeps the game from starting: whatever fails here, the game starts.
        The deploy check (it stats every file the manager placed) and the last game's record copied just
        now are looked at on another thread; ``applied``: right after an apply (its files are in place)."""
        if self._closed:
            return
        if self._bisect is not None:  # the search's steps turn mods off on purpose
            self._start_game()
            return
        m = self.manager
        try:
            facts = self._precheck_facts(verify=False)
            bg = precheck.load_background(m.data_dir / "cache")
            # a record copied just now (or a game update) is looked at first (``now``: not because the last
            # look at natives\ is old, which waits for the next background run)
            due = bg is None or precheck.background_due(facts, bg, m.crash_dir, now=bg.natives_made)
        except Exception:
            log.exception("실행 전 점검을 하지 못했습니다.")
            self._start_when_ready()
            return
        verify = not applied and bool(facts.game_dir)
        run_bg = bool(due and facts.game_dir) and self._precheck_lock.acquire(blocking=False)  # (else the result kept)
        if not verify and not run_bg:
            self._precheck_ask(facts, bg)
            return
        self._prechecking = True
        if self.busy is None:
            self.status_var.set("실행 전 점검 중...")

        def worker():
            result = bg
            try:
                if verify:
                    facts.verify = precheck.verify_files(m)
                if run_bg:
                    result = m.precheck_background(facts, natives=False)
            except Exception as exc:  # then without it
                log.warning("지난 게임 기록을 점검하지 못했습니다: %s", exc)
            finally:
                if run_bg:
                    self._precheck_lock.release()
            self.events.put(("call", self._precheck_ready, facts, result))

        threading.Thread(target=worker, daemon=True).start()

    def _precheck_ready(self, facts, background):
        if self.busy is None and self.status_var.get() == "실행 전 점검 중...":
            self.status_var.set("준비")
        # outside the event pump: a window opened there would hold up every other event
        self.root.after_idle(lambda: self._precheck_ask(facts, background))

    def _precheck_ask(self, facts, background):
        self._prechecking = False
        if self._closed:
            return
        m = self.manager
        dialog = None
        try:
            report = self._precheck_report(facts, background)
            ask = precheck.to_ask(report, m.data_dir)
            if report.issues:
                log.info("%s", report.summary())
            choice = ("run",)
            answered, self._precheck_answered = self._precheck_answered, None
            if ask and answered != report.signature():  # (not again right after 그래도 실행 made it apply first)
                dialog = PrecheckDialog(self.root, self.theme, m, report, launch=True, new=ask,
                                        recheck=lambda: self._precheck_report(facts, background))
                choice = dialog.show()
                if choice == ("run",):
                    self._precheck_answered = dialog.report.signature()
        except Exception:
            log.exception("실행 전 점검을 하지 못했습니다.")
            choice = ("run",)
        if dialog is not None and dialog.dont_ask:
            try:
                precheck.remember(m.data_dir, dialog.report.important, dialog.report.game_version)
            except Exception:
                log.exception("실행 전 점검 상태를 저장하지 못했습니다.")
        if choice == ("run",):
            self._start_when_ready()
        elif choice is None:
            log.info("실행 전 점검: 게임을 실행하지 않았습니다.")
        else:
            self._precheck_choice(choice)

    def _start_when_ready(self):
        """Start the game once nothing changes the game folder: what began while 실행 전 점검 ran (an apply
        after the mods' material check, the user's own changes) is finished, or applied, first."""
        if self._closed:
            return
        m = self.manager
        if self.busy == "deploy":  # its done handler starts the game (after looking again)
            self._launch_after_apply = True
            return
        if self.busy in self.FOLDER_TASKS or self._installing or self._retarget_waiting:
            log.info("다른 작업 중이라 게임을 실행하지 않았습니다. 작업이 끝난 뒤 다시 실행하세요.")
            self.status_var.set("작업이 끝난 뒤에 다시 시도하세요.")
            self._precheck_answered = None
            return
        if m.dirty and m.game_dir:
            if m.settings.apply_before_launch:
                log.info("남은 변경사항을 적용한 뒤 게임을 실행합니다.")
                self._launch_after_apply = True
                self.apply()
                return
            if self._unapplied_ok != m.revision and not messagebox.askyesno(
                    "게임 실행", "아직 게임에 적용하지 않은 변경사항이 있습니다.\n그대로 실행할까요?", parent=self.root):
                self._precheck_answered = None
                return
        self._precheck_answered = None
        self._start_game()

    def _precheck_choice(self, choice):
        """What was picked in the 실행 전 점검 window: ("action", Issue) or ("show", mod id)."""
        if not choice:
            return
        if choice[0] == "show":
            self.show_mod(choice[1])
            return
        issue = choice[1]
        log.info("실행 전 점검: %s", issue.action_label)
        m = self.manager
        ids = [i for i in issue.mods if any(mod.id == i for mod in m.mods)]
        action = issue.action
        if action == "matfix" and ids and all(m.material_state(i) in ("stale", "declined") for i in ids):
            if self._search_blocks() or not self._materials_ready():
                return
            mods = [m.get(i) for i in ids]
            m.mark_materials_offered(mods)
            chosen = MaterialsDialog(self.root, self.theme, m, mods).show()
            if chosen:
                self.repair_materials(chosen)
        elif action in ("matfix", "matfix_check") and ids:
            self.check_materials(ids)
        elif action in ("update_refw", "install_refw"):
            self._on_refw_click()
        elif action == "enable_refw" and ids:
            if self._search_blocks() or not self.can_mutate():
                return
            m.set_enabled(ids[0], True)
            log.info("REFramework를 켰습니다: %s", m.get(ids[0]).name)
            self._changed(select=ids[0])
        elif action == "apply":
            self.apply()
        elif action == "crash_record":
            self.show_crashes()
        elif action == "show_mods" and ids:
            self.show_mods(ids)

    def show_mods(self, ids: list[str]):
        """Select these mods in the list (clearing a search or filter that hides one of them)."""
        ids = [i for i in ids if any(m.id == i for m in self.manager.mods)]
        if len(ids) <= 1:
            if ids:
                self.show_mod(ids[0])
            return
        if not all(self.tree.exists(i) for i in ids):
            self.filter_var.set(FILTER_ALL)
            self.search_var.set("")
            self.refresh()
        self.tree.selection_set(ids)
        self.tree.see(ids[0])

    def precheck_background(self):
        """The slow part of 실행 전 점검 in the background when it is due (at startup, after applying, for a
        new game folder): the last game's records against the mods, and the loose files no mod placed
        with a read-only material check. Its result is kept for the next launch."""
        m = self.manager
        if self._closed or self._precheck_runs or not m.game_dir or not m.game_dir.is_dir():
            return
        if self.busy is not None or self._installing or self._matfix_running:  # the folder settles; one reader
            if not self._precheck_waiting:                                       # of the game's files at a time
                self._precheck_waiting = True
                self.root.after(2000, self._precheck_again)
            return
        try:
            version = self._game_version_now()
            bg = precheck.load_background(m.data_dir / "cache")
            if not precheck.background_due(precheck.Facts(m.game_dir, game_version=version), bg, m.crash_dir):
                return
            facts = precheck.gather(m, self._refw_status, self._conflicts, self._missing, verify=False)
        except Exception as exc:  # only a help
            log.warning("실행 전 점검을 준비하지 못했습니다: %s", exc)
            return
        self._run_precheck(facts, m.settings.check_materials)

    def _precheck_again(self):
        self._precheck_waiting = False
        self.precheck_background()

    def _run_precheck(self, facts, materials: bool, then=None):
        """The slow part on another thread (one at a time); ``then(result)`` (a precheck.Background, or the
        exception) runs on this thread afterwards."""
        m = self.manager
        self._precheck_runs += 1

        def progress(done, total, msg):
            self._precheck_reading = True  # (only the material check reports progress)
            self.events.put(("call", self._precheck_status, msg))

        def worker():
            try:
                with self._precheck_lock:
                    result = m.precheck_background(facts, materials=materials, progress=progress)
            except Exception as exc:  # only a help: in the log
                log.warning("실행 전 점검 중 일부를 하지 못했습니다: %s", exc)
                result = exc
            self.events.put(("call", self._precheck_done, result, then))

        threading.Thread(target=worker, daemon=True).start()

    def _precheck_status(self, msg: str):
        if self.busy is None and self._precheck_runs and not self._matfix_running:
            self.status_var.set(f"{precheck.TITLE}: " + (msg if len(msg) < 80 else "…" + msg[-78:]))

    def _precheck_done(self, result, then):
        self._precheck_runs -= 1
        self._precheck_reading = self._precheck_reading and self._precheck_runs > 0
        if self._closed:
            return
        if self.busy is None and self.status_var.get().startswith(precheck.TITLE):
            self.status_var.set("준비")
        if isinstance(result, precheck.Background) and result.matfix:
            log.debug("실행 전 점검: %s", result.matfix)
        if then is not None:  # outside the event pump (it may open a window)
            self.root.after_idle(lambda: None if self._closed else then(result))

    def show_precheck(self):
        """☰ → 실행 전 점검: the last game's records and the loose files no mod placed looked at again, then
        every issue (low ones too) in a window."""
        if self._precheck_menu or self._modal_open() or not self._idle():  # (a deploy: the folder is half done)
            return
        m = self.manager
        if not m.game_dir or not m.game_dir.is_dir():
            self._open_precheck(None)
            return
        self._precheck_menu = True
        if self.busy is None:
            self.status_var.set(f"{precheck.TITLE} 중...")
        try:
            facts = self._precheck_facts(verify=False)
        except Exception as exc:
            self._open_precheck(exc)
            return
        # (while the mods' own material check runs, files not checked yet wait for the next run)
        self._run_precheck(facts, not (self._matfix_running or self.busy == "matfix"), then=self._open_precheck)

    def _open_precheck(self, result):
        """The 실행 전 점검 window from the menu (``result``: what the slow part gave, an exception, or None)."""
        if self._modal_open() or self.busy in ("deploy", "purge"):  # another window, or the folder half done
            self._precheck_menu = True
            self.root.after(1000, lambda: None if self._closed else self._open_precheck(result))
            return
        self._precheck_menu = False
        if self.busy is None and self.status_var.get().startswith(precheck.TITLE):
            self.status_var.set("준비")
        m = self.manager
        bg = result if isinstance(result, precheck.Background) else None
        note = ""
        if isinstance(result, Exception):
            note = f"지난 게임 기록과 매니저가 넣지 않은 루스 파일은 살펴보지 못했습니다: {result}"
            bg = precheck.load_background(m.data_dir / "cache")
        elif bg is not None:
            note = bg.matfix
        try:
            facts = self._precheck_facts()  # as things are now (the mods may have changed meanwhile)
            report = self._precheck_report(facts, bg)
        except Exception as exc:
            log.error("실행 전 점검을 하지 못했습니다:\n%s", traceback.format_exc())
            self.show_error(precheck.TITLE, f"실행 전 점검을 하지 못했습니다: {exc}")
            return
        log.info("%s", report.summary())
        dialog = PrecheckDialog(self.root, self.theme, m, report, note=note,
                                recheck=lambda: self._precheck_report(facts, bg))
        self._precheck_choice(dialog.show())

    # ------------------------------------------------------------------ the game process (how it ended)
    def _watch_game(self, launched: float):
        """Look for the game just started and find out how it ends (in the background; Windows only)."""
        self.watcher.game_dir = self.manager.game_dir
        self._watch_lost = None
        if self.watcher.watch(launched):
            self._watch_launched = launched

    def _on_watch_lost(self, launched: float | None):
        """The game started from here was not found in time (or could not be watched): nothing will
        ask about it by itself, so the search's bar says to answer with 결과 알리기."""
        if self._closed:
            return
        if self._watch_launched == launched:
            self._watch_launched = None
        self._watch_lost = launched
        if self._bisect is not None:
            self._show_bisect_bar()

    def _on_game_exit(self, game_exit):
        """The watched game ended: collect its records and judge how (in the background), keep its mods
        as the last good set after a normal exit, and tell about a crash, or ask the search's question."""
        if self._closed:
            return
        m = self.manager
        session = self._bisect
        step = session.launched if session is not None and session.awaiting_answer else None
        if step is not None:
            session.note_exit(game_exit.to_dict())
        launched = step if step is not None else self._watch_launched
        self._watch_launched = None
        try:
            context = m.crash_context()
        except Exception as exc:  # the exit code still says something
            log.warning("모드 상태를 읽지 못했습니다: %s", exc)
            context = None
        crash_dir, game_dir, keep_good = m.crash_dir, m.game_dir, session is None
        run_args = self._step_run_args(session) if step is not None else None

        def worker():
            try:
                with self._collect_lock:
                    detection = gamewatch.after_exit(crash_dir, game_exit, launched, game_dir, context, keep_good)
            except Exception as exc:  # never in the user's way
                log.warning("게임이 어떻게 끝났는지 확인하지 못했습니다: %s", exc)
                detection = gamewatch.judge(game_exit)
            # what the record of the files the game read says of the step's mods (play time, never requested)
            run = accessed.step_run(detection, *run_args) if run_args is not None else None
            self.events.put(("call", self._game_ended, detection, step, run))

        threading.Thread(target=worker, daemon=True).start()

    def _game_ended(self, detection, step: float | None, run: dict | None = None):
        if self._closed:
            return
        self._apply_pending_restore()  # (a search that ended while the game ran: its REFramework setting back)
        session = self._bisect
        if session is not None:
            if detection.record is not None:  # a crash in a step: the search asks about it itself
                crashes.mark_notified(self.manager.crash_dir, detection.record)
            if step is not None and session.awaiting_answer and session.launched == step:
                if run is not None:
                    session.note_run(run)
                self._bisect_detection = (step, detection)
                self.root.after_idle(lambda: self.ask_bisect(auto=True))
            return
        if detection.record is not None and detection.alarm:
            self._show_notice(detection.record)
        elif detection.alarm:
            label = crashlog.exception_label(detection.code) if detection.code else ""
            self._notice_session = None
            # no record of this run to show (the records window would show another one): finding the mod
            # by turning mods off is what is left
            self._pack_notice("⚠  게임이 비정상적으로 종료되었습니다.",
                              (f"오류 코드 {label} · " if label else "") + "REFramework 기록은 없습니다",
                              (bisect.TITLE, self._notice_bisect))

    # ------------------------------------------------------------------ 원인 모드 찾기 (bisect)
    def _search_blocks(self, loud: bool = True) -> bool:
        """While 원인 모드 찾기 runs, what changes the mods it turns on and off waits (``loud``: in a window,
        else in the status bar)."""
        if self._bisect is None and not self._bisect_preparing:  # (preparing: the plan holds the mods' state)
            return False
        self.root.bell()
        self.status_var.set(bisect.BLOCKED)
        if loud:
            messagebox.showinfo(bisect.TITLE, bisect.BLOCKED, parent=self.root)
        return True

    def _bisect_ready(self) -> bool:
        """The search's next launch (or its result) is known. It is worked out on another thread after an
        answer and when a search is picked up again at startup; meanwhile the bar says so."""
        session = self._bisect
        return session is not None and session.ready and not self._bisect_computing

    def _settle_bisect(self, then, work=None):
        """What comes next in the search (``work``, by default Session.settle: the engine decides), on
        another thread, then ``then(step or result)`` here. A format-1 search, or one whose next launch
        is known already, needs no thread."""
        session = self._bisect
        if session is None or self._bisect_computing:
            return
        if session.legacy or (work is None and session.ready):
            then((work or session.settle)())
            return
        self._bisect_computing = True
        self._show_bisect_bar()
        gc.collect()  # (here: tk objects left for the collector must not be freed on the worker thread)

        def worker():
            try:
                result = (work or session.settle)()
            except Exception as exc:
                log.error("원인 찾기의 다음 실행을 고르지 못했습니다:\n%s", traceback.format_exc())
                result = exc
            self.events.put(("call", self._bisect_settled, session, result, then))

        threading.Thread(target=worker, daemon=True).start()

    def _bisect_settled(self, session, result, then):
        self._bisect_computing = False
        if self._closed or session is not self._bisect:  # (stopped meanwhile)
            return
        if isinstance(result, Exception):
            self._abort_bisect(f"원인 찾기의 다음 실행을 고르지 못했습니다: {result}")
            return
        then(result)

    def _show_bisect_bar(self):
        """The bar for the search's state: the step to play, the game running, the result, or the next
        launch being worked out."""
        session = self._bisect
        if session is None or self._closed:
            self.bisect_bar.pack_forget()
            return
        second = None
        stop = (bisect.BUTTON_STOP, self.stop_bisect)
        main = ("▶  " + bisect.BUTTON_LAUNCH, self.launch_game)
        enabled = True
        if not self._bisect_ready():
            title, body, enabled = "원인 찾기", BISECT_COMPUTING, False
        else:
            nxt = session.next()
            if isinstance(nxt, bisect.Result):
                title = "원인 찾기를 그만두었습니다" if nxt.kind == "aborted" else "원인 찾기 결과"
                body = bisect.result_title(nxt, session.names)
                main = (BISECT_SHOW_RESULT, self.show_bisect_result)
                stop = (bisect.RESULT_RESTORE, self.stop_bisect)
            else:
                title = f"원인 찾기 · {nxt.number}번째 실행" if nxt.engine else f"원인 찾기 {nxt.number}/{nxt.total}단계"
                if session.awaiting_answer:
                    watched = self.watcher.available and not (self._watch_lost is not None
                                                              and self._watch_lost == session.launched)
                    body = BISECT_PLAYING if watched else BISECT_PLAYING_MANUAL
                    if nxt.engine and nxt.minutes and nxt.play:
                        body += "\n" + nxt.play
                    main = (BISECT_ANSWER, self.ask_bisect)
                    second = ("게임 다시 실행", self.launch_game)
                else:  # (not in the game folder yet: a deploy runs, or failed)
                    body = session.banner(nxt, applied=not self.manager.dirty).split(": ", 1)[-1]
                    if nxt.engine:
                        body += "\n" + bisect.progress_text(nxt, session.names)
                        change = session.last_change()  # (how the last answer moved it: kept until the next one)
                        if change:
                            body += "\n" + change
        self.bisect_title.configure(text=title)
        self.bisect_text.configure(text=body)
        for button in (self.bisect_main, self.bisect_second, self.bisect_stop):
            button.pack_forget()
        self.bisect_stop.configure(text=stop[0], command=stop[1])
        self.bisect_stop.pack(side="right")
        if second is not None:
            self.bisect_second.configure(text=second[0], command=second[1])
            self.bisect_second.pack(side="right", padx=(0, self.theme.px(4)))
        self.bisect_main.configure(text=main[0], command=main[1])
        if enabled:  # (while the next launch is worked out only 그만두기 is there)
            self.bisect_main.pack(side="right", padx=(0, self.theme.px(6)))
        if not self.bisect_bar.winfo_manager():
            px = self.theme.px
            self.bisect_bar.pack(fill="x", padx=px(20), pady=(px(10), 0), before=self.paned)

    def show_bisect(self, record: str = ""):
        """☰ → 원인 모드 찾기 (and the 게임 튕김 기록 window's button): start one, or where the running one is."""
        session = self._bisect
        if session is None:
            self.start_bisect(record)
            return
        if not self._bisect_ready():
            self._show_bisect_bar()
            self.status_var.set(BISECT_COMPUTING)
            return
        nxt = session.next()
        if isinstance(nxt, bisect.Result):
            self.show_bisect_result()
        elif session.awaiting_answer:
            self.ask_bisect()
        else:
            self._show_bisect_bar()
            messagebox.showinfo(bisect.TITLE, f"{session.banner(nxt, not self.manager.dirty)}\n\n"
                                              f"목록 위 안내 줄의 '{bisect.BUTTON_LAUNCH}'을 "
                                              f"누르세요. 그만두려면 '{bisect.BUTTON_STOP}'를 누르세요.", parent=self.root)

    def start_bisect(self, record: str = ""):
        """Look at the mods and the records for a search (the slow part in the background: the evidence, the
        engine's first decision, the game folder), then show the start window."""
        m = self.manager
        if self._bisect is not None or self._bisect_preparing:
            return
        if not m.game_dir:
            messagebox.showwarning(bisect.TITLE, "먼저 게임 폴더를 지정하세요.", parent=self.root)
            return
        if not self._idle():
            return
        try:
            outdated = m.outdated_paks()
        except Exception as exc:
            log.warning("적용 상태를 점검하지 못했습니다: %s", exc)
            outdated = []
        if outdated:  # moved by the next deploy, which the search would read as a game update: first
            log.info("예전 방식(패치 번호)의 PAK %d개를 먼저 %s 폴더로 옮깁니다.", len(outdated), PAK_MODS_DIR)
            m.dirty = True
            m.save()
            messagebox.showinfo(bisect.TITLE, "예전 방식(패치 번호)으로 넣은 PAK을 먼저 pak_mods 폴더로 옮깁니다. "
                                              f"적용이 끝나면 '{bisect.TITLE}'를 다시 누르세요.", parent=self.root)
            self.apply()
            return
        try:
            owners = bisect.conflict_owners(m)
            mods, deployed, backups = bisect.facts(m, owners), m.deployed_files(), m.deployed_backups()
            last_good, version = gamewatch.load_last_good(m.crash_dir), m.game_version(refresh=True)
            inputs = evidence.gather(m, record, owners)
        except Exception as exc:
            log.error("원인 찾기를 준비하지 못했습니다:\n%s", traceback.format_exc())
            self.show_error(bisect.TITLE, f"원인 찾기를 준비하지 못했습니다: {exc}")
            return
        game_dir, profile, revision = m.game_dir, m.profile, m.revision
        units = [f.id for f in mods if f.enabled and f.has_files]
        loose = any("loose" in f.kinds for f in mods if f.enabled)
        self._bisect_preparing = True
        self.status_var.set(BISECT_PREPARING)
        gc.collect()  # (here: tk objects left for the collector must not be freed on the worker thread)

        def worker():
            running, plan, offer = False, None, None
            try:
                running = is_game_running()
                try:
                    unmanaged = bisect.unmanaged_files(game_dir, deployed, backups=backups)
                except Exception as exc:  # only a list for the user
                    log.warning("매니저가 넣지 않은 파일을 찾지 못했습니다: %s", exc)
                    unmanaged = ([], 0)
                found = evidence.build(inputs, evidence.equipment_choices(m, units))
                plan = bisect.make_plan(mods, last_good, version, unmanaged, profile, bisect.backed_up_refw(backups),
                                        revision, owners=owners, evidence=found)
                try:
                    offer = accessed.manager_offer(m, running, loose=loose)
                except Exception as exc:  # then the search runs without the record of the files the game reads
                    log.warning("REFramework 파일 기록 설정을 확인하지 못했습니다: %s", exc)
            except Exception as exc:
                log.error("원인 찾기를 준비하지 못했습니다:\n%s", traceback.format_exc())
                plan = exc
            self.events.put(("call", self._bisect_planned, plan, running, record, offer))

        threading.Thread(target=worker, daemon=True).start()

    def _bisect_planned(self, plan, running: bool, record: str, offer=None):
        self._bisect_preparing = False
        if self.busy is None:
            self.status_var.set("준비")
        # outside the event pump: a window opened there would hold up every other event
        self.root.after_idle(lambda: None if self._closed else self._open_bisect_start(plan, running, record, offer))

    def _open_bisect_start(self, plan, running: bool, record: str, offer=None):
        m = self.manager
        if isinstance(plan, Exception):
            self.show_error(bisect.TITLE, f"원인 찾기를 준비하지 못했습니다: {plan}")
            return
        if running:
            messagebox.showinfo(bisect.TITLE, "게임이 실행 중입니다. 게임을 끈 뒤 다시 시작하세요.", parent=self.root)
            return
        if self._bisect is not None or self._modal_open():
            return
        found = crashes.find_session(m.crash_dir, record) if record else None
        record_id = found.id if found else str(getattr(plan.evidence, "record", "") or "")
        choice = BisectStartDialog(self.root, self.theme, plan, found.title() if found else "", offer,
                                   m.settings.bisect_log_files).show()
        if not choice:
            return
        if choice is True:  # (a window that only says yes)
            choice = {}
        if not self._idle() or self._bisect is not None:
            return
        restore = None
        if offer is not None and offer.available and not offer.already:
            want = bool(choice.get("log"))
            if want != m.settings.bisect_log_files:
                m.update_settings(bisect_log_files=want)  # (the box's default next time)
            if want:
                try:
                    restore = accessed.enable_logging(m.set_refw_option, offer)
                except (DeployError, ValueError, OSError) as exc:
                    self.show_error(bisect.TITLE, f"게임이 읽는 파일 기록을 켜지 못해 기록 없이 찾습니다: {exc}")
                    restore = None
        try:
            session, _nxt = bisect.start(m, plan, record_id, when=choice.get("when"), minutes=choice.get("minutes"),
                                         equip=choice.get("equip"), refw_restore=restore)
        except (bisect.BisectError, ValueError, OSError) as exc:
            if restore:
                try:
                    accessed.restore_logging(m.set_refw_option, restore)
                except (DeployError, ValueError, OSError) as undo:
                    log.warning("REFramework 파일 기록 설정을 되돌리지 못했습니다: %s", undo)
            self.show_error(bisect.TITLE, f"원인 찾기를 시작하지 못했습니다: {exc}")
            return
        self._bisect = session
        self._bisect_detection = None
        self.refresh()
        self._show_bisect_bar()
        self._apply_bisect_step()

    def _apply_bisect_step(self):
        """Put the step's mods into the game folder (also with 즉시 적용 off: the next launch needs them)."""
        if self.manager.dirty and self.manager.game_dir:
            self.apply()

    def _resume_bisect(self):
        """Go on with the search that was running when the manager was closed (bisect.json; its next
        launch is worked out again from the answers first, on another thread)."""
        session = self._bisect
        if session is None:
            return
        if not session.ready:
            self._show_bisect_bar()
            self._settle_bisect(lambda _nxt: self._resume_bisect())
            return
        m = self.manager
        nxt = session.next()
        if isinstance(nxt, bisect.Step):
            reason = bisect.problem(m, session)
            if reason:
                self._abort_bisect(reason)
                return
            try:
                nxt = bisect.apply_step(m, session)
            except bisect.BisectError as exc:
                self._abort_bisect(str(exc))
                return
        log.info("원인 찾기를 이어서 합니다 (임시 프로필 '%s').", session.temp_profile)
        self.refresh()
        self._show_bisect_bar()
        if isinstance(nxt, bisect.Result):
            self.root.after_idle(lambda: self.show_bisect_result(auto=True))
            return
        if not session.awaiting_answer:
            # after _startup's checks (records collected with the mods as they are, the folder verified)
            self.root.after_idle(self._apply_bisect_step)
            return
        # the step's game was started before the manager closed: it may still run (then the watcher
        # finds it at startup), else how it ended is judged from its records
        noted = gamewatch.GameExit.from_dict((session.current or {}).get("exit"))
        launched, crash_dir, game_dir = session.launched, m.crash_dir, m.game_dir
        try:
            context = m.crash_context()
        except Exception:
            context = None
        run_args = self._step_run_args(session)

        def worker():
            detection = None
            try:
                if noted is not None or not is_game_running():
                    with self._collect_lock:
                        detection = gamewatch.after_exit(crash_dir, noted, launched, game_dir, context, keep_good=False)
            except Exception as exc:  # then the user answers without it
                log.warning("게임이 어떻게 끝났는지 확인하지 못했습니다: %s", exc)
                detection = gamewatch.judge(noted)
            if detection is not None:
                run = accessed.step_run(detection, *run_args) if run_args is not None else None
                self.events.put(("call", self._game_ended, detection, launched, run))

        threading.Thread(target=worker, daemon=True).start()

    def _step_run_args(self, session) -> tuple | None:
        """(the step's mods, the manager's crashes.ModIndex, the content mods, the names of pak files) for
        accessed.step_run, taken here on the GUI thread (None for a format-1 search)."""
        if session is None or session.legacy:
            return None
        try:
            index = self.manager.crash_index()
        except Exception as exc:  # then the launch is judged without the record of the files the game read
            log.warning("모드 파일 목록을 읽지 못했습니다: %s", exc)
            index = None
        return (list((session.current or {}).get("on") or []), index, list(session.content),
                accessed.PakNames(self.manager.data_dir / "cache"))

    def _apply_pending_restore(self):
        """REFramework's file record setting left to put back when the game is closed (accessed.apply_pending)."""
        try:
            msg = accessed.apply_pending(self.manager, searching=self._bisect is not None)
        except Exception as exc:  # tried again at the next start or game exit
            log.warning("REFramework 파일 기록 설정을 되돌리지 못했습니다: %s", exc)
            msg = ""
        if msg:
            log.info("%s", msg)
            self.status_var.set(msg)

    def ask_bisect(self, auto: bool = False):
        """또 튕겼나요? for the step's game (``auto``: the game was seen ending, else the user asked)."""
        session = self._bisect
        if session is None or self._bisect_asking or self._closed:
            return
        if not self._bisect_ready():
            if auto:
                self.root.after(1000, lambda: self.ask_bisect(auto=True))
            return
        nxt = session.next()
        if isinstance(nxt, bisect.Result):
            self.show_bisect_result(auto=auto)
            return
        if auto and not session.awaiting_answer:
            return  # answered meanwhile
        if self._modal_open():
            if auto:
                self.root.after(1000, lambda: self.ask_bisect(auto=True))
            return
        if self.busy not in self.MUTATING_OK_DURING or self._installing:  # the answer could not be taken now
            if auto:
                self.root.after(1000, lambda: self.ask_bisect(auto=True))
            else:
                self.can_mutate()
            return
        if not auto and self.watcher.pid is not None:  # the step's game still runs: its end is asked about
            messagebox.showinfo(bisect.TITLE, BISECT_STILL_RUNNING, parent=self.root)
            return
        found = self._bisect_detection
        detection = found[1] if found is not None and found[0] == session.launched else None
        current = session.current or {}
        self._bisect_asking = True
        try:
            if self.root.state() == "iconic":  # minimized when the game started
                self.root.deiconify()
            self.root.lift()
            choice = BisectAskDialog(self.root, self.theme, nxt, detection, session.names, current.get("run"),
                                     int(current.get("retries") or 0)).show()
        finally:
            self._bisect_asking = False
        if choice is None:
            self._show_bisect_bar()
            return
        self._answer_bisect({"crashed": True, "fine": False}.get(choice), detection.label if detection else "")

    def _answer_bisect(self, crashed: bool | None, detected: str = ""):
        """Take the answer for the step (what the manager saw of its game is noted with it), then work out
        the next launch on another thread and apply it, or show the result."""
        session = self._bisect
        if session is None:
            return
        if not self._bisect_ready():
            self._show_bisect_bar()
            return
        if not self.can_mutate():  # (a task is running: the answer can be given again from the bar)
            self._show_bisect_bar()
            return
        reason = bisect.problem(self.manager, session)
        if reason:
            self._abort_bisect(reason)
            return
        session.add_answer(crashed, detected)
        self._bisect_detection = None
        self._settle_bisect(self._bisect_next)

    def _bisect_next(self, nxt):
        """The next launch is known: turn its mods on and apply them, or show the result."""
        session = self._bisect
        if session is None:
            return
        if isinstance(nxt, bisect.Step):
            try:
                nxt = bisect.apply_step(self.manager, session)
            except bisect.BisectError as exc:
                self._abort_bisect(str(exc))
                return
        self.refresh()
        self._show_bisect_bar()
        change = session.last_change()
        if change:  # (in the bar too: applying the next step takes the status bar over at once)
            log.info("%s", change)
            self.status_var.set(change)
        if isinstance(nxt, bisect.Step):
            log.info("%s", session.banner(nxt))
            self._apply_bisect_step()
        else:
            self.root.after_idle(lambda: self.show_bisect_result(auto=True))

    def _abort_bisect(self, reason: str):
        """The search cannot go on (a game update, the temporary profile changed...): say why, then the
        result window takes the user back to their profile."""
        session = self._bisect
        if session is None:
            return
        log.warning("%s", reason)
        session.abort(reason)
        self._show_bisect_bar()
        self.root.after_idle(lambda: self.show_bisect_result(auto=True))

    def show_bisect_result(self, auto: bool = False):
        """The result window (``auto``: it came by itself; while a task runs it waits for it)."""
        session = self._bisect
        if session is None or self._closed:
            return
        if not self._bisect_ready():
            if auto:
                self.root.after(1000, lambda: self.show_bisect_result(auto=True))
            return
        result = session.next()
        if not isinstance(result, bisect.Result) or self._modal_open():
            self._show_bisect_bar()
            return
        if self.busy not in self.MUTATING_OK_DURING or self._installing:  # its choice could not be taken now
            self._show_bisect_bar()
            if auto:
                self.root.after(1000, lambda: self.show_bisect_result(auto=True))
            else:
                self.can_mutate()
            return
        status = self._refw_status
        choice = BisectResultDialog(self.root, self.theme, self.manager, session, result,
                                    refw.describe_installed(status) if status else "").show()
        if not choice:
            self._show_bisect_bar()
            return
        if choice[0] == "more":  # 계속 찾기 / 한 번 더 확인: the search goes on
            if not self.can_mutate():
                self._show_bisect_bar()
                return
            log.info("원인 찾기: %s", bisect.RESULT_MORE if result.kind == "unsure" else bisect.RESULT_CONFIRM)
            self._settle_bisect(self._bisect_next, work=session.more)
            return
        if self._finish_bisect(choice[1] if choice[0] == "disable" else ()) and choice[0] == "refw":
            self.root.after_idle(self._on_refw_click)

    def stop_bisect(self):
        """그만두기 (or 원래 프로필로 once there is a result)."""
        session = self._bisect
        if session is None:
            return
        ready = self._bisect_ready()
        if ready and isinstance(session.next(), bisect.Result):
            question = f"원래 프로필 '{session.original_profile}'(으)로 돌아갈까요? 원인 찾기용 임시 프로필은 지웁니다."
        else:
            question = (f"원인 찾기를 그만두고 원래 프로필 '{session.original_profile}'(으)로 돌아갈까요? "
                        "지금까지 확인한 결과는 남지 않습니다.")
            if ready:
                try:
                    question += session.interim_note()
                except Exception as exc:  # only a help
                    log.warning("지금까지 가장 의심되는 모드를 알아내지 못했습니다: %s", exc)
        if messagebox.askyesno(bisect.TITLE, question, parent=self.root):
            self._finish_bisect(())

    def _finish_bisect(self, disable=()) -> bool:
        """Back to the user's profile (turning ``disable`` off there), then apply it; REFramework's file
        record setting goes back as it was (or once the game is closed). Returns whether it did."""
        session = self._bisect
        if session is None or not self.can_mutate():
            return False
        names = [session.name(i) for i in disable]
        restore = session.refw_restore
        try:
            bisect.finish(self.manager, session, disable)
        except (KeyError, ValueError, OSError) as exc:
            self.show_error(bisect.TITLE, f"원래 프로필로 돌아가지 못했습니다: {exc}")
            return False
        self._bisect = None
        self._bisect_detection = None
        self._bisect_computing = False
        self.bisect_bar.pack_forget()
        if names:
            log.info("원인 찾기: '%s'을(를) 원래 프로필에서 껐습니다.", "', '".join(names))
        try:
            restored = accessed.finish_restore(self.manager, restore)
        except Exception as exc:  # (finish_restore keeps it for later itself; this is only in case)
            log.warning("REFramework 파일 기록 설정을 되돌리지 못했습니다: %s", exc)
            restored = ""
        if restored:
            log.info("%s", restored)
        self.refresh()
        self.warm_equipment()
        if self.manager.game_dir:
            self.apply()
        if restored:
            self.status_var.set(restored)
            if self.busy is not None or self._apply_pending:  # (the deploy's end would replace it with "준비" at once)
                self._status_after_task = restored
        return True

    # ------------------------------------------------------------------ closing
    def on_close(self):
        if self.busy:
            if messagebox.askyesno("종료", "작업이 끝나면 종료할까요?", parent=self.root):
                self._close_after_task = True
            return
        if self.manager.dirty and self.manager.game_dir and not self.manager.settings.auto_apply:
            answer = messagebox.askyesnocancel("종료", "게임에 적용하지 않은 변경사항이 있습니다.\n적용한 뒤 종료할까요?", parent=self.root)
            if answer is None:
                return
            if answer:
                self._close_after_task = True
                self.apply()
                return
        self._destroy()

    def _destroy(self):
        self.watcher.stop()
        if self.equipment_window is not None:
            self.equipment_window.close()
        if not self._closed:
            self._save_window_state()
        self._closed = True
        self.root.destroy()


def _windows_setup():
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        # The exe keeps Windows' default taskbar identity (its path), so a pinned shortcut to it and the
        # running window share one taskbar button. An explicit ID would make them two buttons.
        return
    try:
        import ctypes

        # From source: own taskbar group/icon instead of python.exe's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MHWildsModManager")  # type: ignore[attr-defined]
    except Exception:
        pass


def _prepare_data_dir(root: tk.Tk, explicit) -> tuple[Path, str | None]:
    """prepare_data_dir with a small progress window while data is being moved."""
    if explicit or resolve_data_dir()[1] is None:
        return prepare_data_dir(explicit)
    win = tk.Toplevel(root)
    win.title(APP_TITLE)
    win.resizable(False, False)
    frame = ttk.Frame(win, padding=20)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="데이터 폴더를 프로그램 옆으로 옮기는 중입니다.\n모드가 많으면 몇 분 걸릴 수 있습니다.",
              justify="left").pack(anchor="w")
    bar = ttk.Progressbar(frame, length=360, mode="determinate", maximum=1)
    bar.pack(fill="x", pady=(12, 0))
    updates: queue.Queue = queue.Queue()
    result: dict = {}

    def work():
        try:
            result["value"] = prepare_data_dir(None, lambda done, total, msg: updates.put((done, total)))
        except BaseException as exc:  # reported below
            result["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    while thread.is_alive():
        try:
            while True:
                done, total = updates.get_nowait()
                bar.configure(maximum=total, value=done)
        except queue.Empty:
            pass
        win.update()
        time.sleep(0.05)
    win.destroy()
    if "error" in result:
        raise result["error"]
    return result["value"]


def main(data_dir=None) -> int:
    _windows_setup()
    root = None
    if TkinterDnD is not None:
        try:
            root = TkinterDnD.Tk()
        except Exception:
            root = None
    if root is None:
        root = tk.Tk()
    root.withdraw()
    events: queue.Queue = queue.Queue()
    try:
        data_path, problem = _prepare_data_dir(root, data_dir)
        if problem:
            messagebox.showwarning(APP_TITLE, problem)
        lock = acquire_instance_lock(data_path)
        if lock is None:
            messagebox.showerror(APP_TITLE, "모드 매니저가 이미 실행 중입니다.")
            root.destroy()
            return 1
        manager = ModManager(data_path)
    except Exception as exc:
        messagebox.showerror(APP_TITLE, f"데이터 폴더를 열 수 없습니다:\n{exc}")
        root.destroy()
        return 1
    setup_logging(manager.data_dir, QueueLogHandler(events))
    if updater.can_self_update():
        updater.cleanup_staged()
    App(root, manager, events)
    root.deiconify()
    root.mainloop()
    lock.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
