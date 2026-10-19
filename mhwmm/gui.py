"""Tkinter user interface."""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__, fsutil, steam, updater
from .analyzer import classify, package_name
from .archive import ARCHIVE_EXTS, ArchiveError
from .constants import (APP_TITLE, DEFAULT_PAK_CHAIN, GAME_EXE, NEXUS_URL, PAK_CHAINS,
                        REFRAMEWORK_URL, STEAM_APP_ID)
from .deploy import DeployError, DeployReport, is_root_pak, path_key
from .manager import (InstallError, InstallSession, Mod, ModManager, acquire_instance_lock,
                      default_data_dir, setup_logging)
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
INSTALL_FILETYPES = [
    ("모드 파일", " ".join(f"*{e}" for e in (*ARCHIVE_EXTS, ".pak", ".lua", ".dll"))),
    ("모든 파일", "*.*"),
]
# Errors that are shown to the user without a traceback in the log.
EXPECTED_ERRORS = (DeployError, InstallError, ArchiveError, UpdateError, OSError, ValueError)


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


class Dialog(tk.Toplevel):
    """Small modal dialog base."""

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
        parent = self.master
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_reqwidth()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_reqheight()) // 3)
        self.geometry(f"+{x}+{y}")
        set_dark_titlebar(self, self.theme.name == "dark")
        self.deiconify()
        self.grab_set()
        self.focus_set()
        self.wait_window()
        return self.result

    def cancel(self):
        self.result = None
        self.destroy()


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
        ttk.Label(self.body, text=f"새 버전 v{release.version}이 나왔습니다", style="DialogTitle.TLabel").pack(anchor="w")
        ttk.Label(self.body, text=f"지금 쓰는 버전은 v{__version__}입니다.", style="Muted.TLabel").pack(
            anchor="w", pady=(px(4), px(12)))
        notes = theme.mark(tk.Text(self.body, height=12, width=66, wrap="word", font=theme.fonts["small"],
                                   padx=px(14), pady=px(10), cursor="arrow"), "text")
        text = release.changes.replace("**", "").replace("`", "") or "변경 내역이 없습니다."
        notes.insert("1.0", text)
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


class SettingsDialog(Dialog):
    def __init__(self, parent, theme: Theme, manager: ModManager):
        super().__init__(parent, theme, "설정")
        px = theme.px
        self.manager = manager
        s = manager.settings
        ttk.Label(self.body, text="설정", style="DialogTitle.TLabel").pack(anchor="w", pady=(0, px(14)))
        grid = ttk.Frame(self.body)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="테마").grid(row=0, column=0, sticky="w", pady=px(6), padx=(0, px(16)))
        self.theme_var = tk.StringVar(value=THEME_NAMES.get(s.theme, THEME_NAMES["dark"]))
        ttk.Combobox(grid, textvariable=self.theme_var, values=list(THEME_NAMES.values()), state="readonly",
                     width=12).grid(row=0, column=1, sticky="w", pady=px(6))

        ttk.Label(grid, text="PAK 대상 파일").grid(row=1, column=0, sticky="w", pady=px(6), padx=(0, px(16)))
        self.chain = tk.StringVar(value=s.pak_chain)
        ttk.Combobox(grid, textvariable=self.chain, values=PAK_CHAINS, width=34).grid(
            row=1, column=1, sticky="ew", pady=px(6))
        ttk.Label(grid, text=f"기본값 {DEFAULT_PAK_CHAIN}. 파일 이름에 번호가 들어 있는 PAK은 그 이름을 따릅니다.",
                  style="Muted.TLabel", font=theme.fonts["small"], wraplength=px(420), justify="left").grid(
            row=2, column=1, sticky="w")

        self.auto_apply = tk.BooleanVar(value=s.auto_apply)
        self.enable_on_install = tk.BooleanVar(value=s.enable_on_install)
        self.check_running = tk.BooleanVar(value=s.check_game_running)
        self.check_updates = tk.BooleanVar(value=s.check_updates)
        checks = ttk.Frame(self.body)
        checks.pack(fill="x", pady=(px(14), 0))
        for text, var in (("변경 즉시 게임 폴더에 적용", self.auto_apply),
                          ("설치한 모드를 바로 활성화", self.enable_on_install),
                          ("적용 전에 게임 실행 여부 확인", self.check_running),
                          ("시작할 때 새 버전 확인", self.check_updates)):
            theme.checkbutton(checks, text, var).pack(anchor="w", pady=px(1))

        data = ttk.Frame(self.body)
        data.pack(fill="x", pady=(px(14), 0))
        ttk.Label(data, text="데이터 폴더").pack(anchor="w")
        row = ttk.Frame(data)
        row.pack(fill="x", pady=(px(4), 0))
        ttk.Label(row, text=shorten_middle(str(manager.data_dir), 60), style="Muted.TLabel").pack(side="left")
        ttk.Button(row, text="열기", style="Compact.TButton",
                   command=lambda: fsutil.open_in_explorer(manager.data_dir)).pack(side="right")
        self.buttons("저장", self.ok)

    def ok(self):
        theme_name = next((k for k, v in THEME_NAMES.items() if v == self.theme_var.get()), "dark")
        try:
            self.manager.update_settings(
                theme=theme_name,
                pak_chain=self.chain.get().strip().lower(),
                auto_apply=self.auto_apply.get(),
                enable_on_install=self.enable_on_install.get(),
                check_game_running=self.check_running.get(),
                check_updates=self.check_updates.get(),
            )
        except ValueError as exc:
            messagebox.showerror("설정", str(exc), parent=self)
            return
        self.result = True
        self.destroy()


class App:
    MUTATING_OK_DURING = (None, "deploy")

    def __init__(self, root: tk.Tk, manager: ModManager, events: queue.Queue):
        self.root = root
        self.manager = manager
        self.events = events
        self.busy: str | None = None
        self._apply_pending = False
        self._install_queue: list[str] = []
        self._installing = False
        self._close_after_task = False
        self._closed = False
        self._drag: dict | None = None
        self._preview = None
        self._conflicts = None
        self._log_visible = True
        self._update: Release | None = None

        self.theme = Theme(root, manager.settings.theme)
        self.theme.mark(root, "window")
        self.theme.use(self.theme.name)
        px = self.theme.px
        root.title(f"{APP_TITLE} {__version__}")
        root.geometry(f"{px(1240)}x{px(800)}")
        root.minsize(px(980), px(620))
        self._set_icon()

        self._build_header()
        self._build_toolbar()
        self._build_statusbar()
        self._build_log()
        self._build_main()
        self._configure_tags()
        self._bind_keys()
        self._setup_dnd()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        set_dark_titlebar(root, self.theme.name == "dark")

        self.refresh()
        self._update_search_hint()
        root.after(60, self._place_sash)
        root.after(100, self._poll_events)
        root.after(300, self._startup)
        if manager.settings.check_updates:
            root.after(2500, lambda: self.check_updates(manual=False))

    def _place_sash(self):
        width = self.paned.winfo_width()
        if width > 1:
            self.paned.sashpos(0, int(width * 0.58))

    # ------------------------------------------------------------------ layout
    def _set_icon(self):
        try:
            self.root.iconphoto(True, self.theme.image("logo", 64, exact=True), self.theme.image("logo", 32, exact=True))
        except tk.TclError:
            pass

    def _build_header(self):
        t, px = self.theme, self.theme.px
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(px(20), px(12)))
        header.pack(fill="x")
        ttk.Separator(self.root).pack(fill="x")

        ttk.Label(header, image=t.image("logo", 40), style="Header.TLabel").pack(side="left")
        titles = ttk.Frame(header, style="Header.TFrame")
        titles.pack(side="left", padx=(px(12), 0))
        ttk.Label(titles, text="MONSTER HUNTER WILDS", style="Eyebrow.TLabel").pack(anchor="w")
        ttk.Label(titles, text="모드 매니저", style="Title.TLabel").pack(anchor="w")

        menu_button = ttk.Menubutton(header, text="☰", style="Ghost.TMenubutton")
        menu_button.pack(side="right")
        self.main_menu = self._build_main_menu(menu_button)
        menu_button.configure(menu=self.main_menu)
        Tooltip(t, menu_button, "메뉴")
        gear = ttk.Button(header, text="⚙", style="Ghost.TButton", command=self.open_settings)
        gear.pack(side="right", padx=(0, px(2)))
        Tooltip(t, gear, "설정")
        ttk.Separator(header, orient="vertical").pack(side="right", fill="y", padx=px(12), pady=px(4))
        launch = ttk.Button(header, text="▶  게임 실행", command=self.launch_game)
        launch.pack(side="right")
        Tooltip(t, launch, "Steam으로 몬스터 헌터 와일즈 실행")

        self.game_button = ttk.Menubutton(header, text="", style="Chip.TMenubutton")
        self.game_button.pack(side="right", padx=(0, px(8)))
        self.game_menu = t.mark(tk.Menu(self.game_button, tearoff=False, postcommand=self._fill_game_menu), "menu")
        self.game_button.configure(menu=self.game_menu)
        Tooltip(t, self.game_button, lambda: str(self.manager.game_dir) if self.manager.game_dir
                else "MonsterHunterWilds.exe가 있는 폴더를 지정하세요")

        self.refw_badge = ttk.Label(header, text="", style="BadgeNeutral.TLabel", cursor="hand2")
        self.refw_badge.pack(side="right", padx=(0, px(10)))
        self.refw_badge.bind("<Button-1>", lambda e: self._on_refw_click())
        Tooltip(t, self.refw_badge, self._refw_tooltip)
        # shown once a newer release is found
        self.update_button = ttk.Button(header, text="", style="Update.TButton", command=self.show_update)
        Tooltip(t, self.update_button, "새 버전의 변경 내역을 보고 업데이트합니다")

    def _build_main_menu(self, parent) -> tk.Menu:
        m = self.theme.mark(tk.Menu(parent, tearoff=False), "menu")
        m.add_command(label="모드 파일 추가...", accelerator="Ctrl+O", command=self.add_files)
        m.add_command(label="폴더에서 모드 추가...", command=self.add_folder)
        m.add_separator()
        m.add_command(label="게임에 적용", accelerator="Ctrl+S", command=self.apply)
        m.add_command(label="적용 상태 점검", command=self.verify)
        m.add_command(label="게임 폴더에서 모든 모드 파일 제거", command=self.purge)
        m.add_separator()
        m.add_command(label="모두 활성화", command=lambda: self.set_enabled_all(True))
        m.add_command(label="모두 비활성화", command=lambda: self.set_enabled_all(False))
        m.add_separator()
        m.add_command(label="데이터 폴더 열기", command=lambda: fsutil.open_in_explorer(self.manager.data_dir))
        m.add_command(label="REFramework 다운로드", command=lambda: webbrowser.open(REFRAMEWORK_URL))
        m.add_command(label="Nexus Mods (몬헌 와일즈)", command=lambda: webbrowser.open(NEXUS_URL))
        m.add_separator()
        m.add_command(label="업데이트 확인", command=lambda: self.check_updates(manual=True))
        m.add_command(label="사용법", command=self.show_help)
        m.add_command(label="정보", command=self.show_about)
        m.add_separator()
        m.add_command(label="종료", command=self.on_close)
        return m

    def _build_toolbar(self):
        t, px = self.theme, self.theme.px
        bar = ttk.Frame(self.root, padding=(px(20), px(14), px(20), px(2)))
        bar.pack(fill="x")
        self.buttons: dict[str, ttk.Button] = {}

        def button(key, text, command, style="TButton", tip=None, side="left", padx=None):
            b = ttk.Button(bar, text=text, command=command, style=style)
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
        button("remove", "삭제", self.remove_selected, tip="Delete")

        button("apply", "✔  게임에 적용", self.apply, side="right", padx=(px(10), 0), tip="Ctrl+S")
        self.auto_var = tk.BooleanVar(value=self.manager.settings.auto_apply)
        switch = ttk.Checkbutton(bar, text="  즉시 적용", variable=self.auto_var, command=self._toggle_auto_apply,
                                 style="Switch.TCheckbutton", compound="left",
                                 image=(t.image("switch_off", 20), "selected", t.image("switch_on", 20)))
        switch.pack(side="right")
        Tooltip(t, switch, "켜 두면 모드를 켜고 끄거나 순서를 바꿀 때 바로 게임 폴더에 반영합니다")

    def _build_statusbar(self):
        px = self.theme.px
        self.status_frame = ttk.Frame(self.root, style="Status.TFrame", padding=(px(20), px(7)))
        self.status_frame.pack(side="bottom", fill="x")
        self.status_sep = ttk.Separator(self.root)
        self.status_sep.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="준비")
        ttk.Label(self.status_frame, textvariable=self.status_var, style="StatusMuted.TLabel").pack(side="left")
        self.state_badge = ttk.Label(self.status_frame, text="", style="BadgeOk.TLabel")
        self.state_badge.pack(side="right")
        self.log_button = ttk.Button(self.status_frame, text="로그 숨기기", style="Link.TButton", command=self.toggle_log)
        self.log_button.pack(side="right", padx=(0, px(10)))
        self.progress = ttk.Progressbar(self.status_frame, length=px(220), mode="determinate",
                                        style="Accent.Horizontal.TProgressbar")

    def _build_log(self):
        t, px = self.theme, self.theme.px
        self.log_frame = ttk.Frame(self.root, padding=(px(20), 0, px(20), px(12)))
        self.log_frame.pack(side="bottom", fill="x")
        card = t.mark(tk.Frame(self.log_frame), "card")
        card.pack(fill="x")
        self.log_text = t.mark(tk.Text(card, height=5, wrap="word", font=t.fonts["small"], padx=px(14),
                                       pady=px(8), cursor="arrow"), "log")
        lsb = ttk.Scrollbar(card, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=autohide(lsb, side="right", fill="y", padx=(0, px(3)), pady=px(3)),
                                state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)

    def _build_main(self):
        t, px = self.theme, self.theme.px
        paned = self.paned = ttk.Panedwindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=px(20), pady=(px(10), px(12)))

        # ---- mod list card
        left = t.mark(tk.Frame(paned), "card")
        head = ttk.Frame(left, style="Card.TFrame", padding=(px(18), px(14), px(14), px(2)))
        head.pack(fill="x")
        ttk.Label(head, text="설치된 모드", style="Section.TLabel").pack(side="left")
        self.count_label = ttk.Label(head, text="", style="CardMuted.TLabel")
        self.count_label.pack(side="left", padx=(px(10), 0))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(head, textvariable=self.search_var, width=24)
        self.search_entry.pack(side="right")
        self.search_hint = ttk.Label(head, text="⌕  모드 검색  (Ctrl+F)", style="FieldHint.TLabel", cursor="xterm")
        self.search_hint.bind("<Button-1>", lambda e: self.search_entry.focus_set())
        self.search_entry.bind("<FocusIn>", lambda e: self._update_search_hint(), add="+")
        self.search_entry.bind("<FocusOut>", lambda e: self._update_search_hint(), add="+")
        self.search_entry.bind("<Escape>", lambda e: self.search_var.set(""))
        self.search_var.trace_add("write", lambda *a: (self._update_search_hint(), self.refresh()))
        ttk.Label(left, text="아래쪽 모드일수록 우선 적용됩니다 · 드래그해서 순서를 바꾸세요",
                  style="Hint.TLabel", padding=(px(18), px(2), px(18), px(10))).pack(fill="x")

        area = ttk.Frame(left, style="Card.TFrame")
        area.pack(fill="both", expand=True, padx=px(6), pady=(0, px(6)))
        cols = ("order", "name", "kind", "version", "files", "conflict")
        self.tree = ttk.Treeview(area, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="")
        self.tree.column("#0", width=px(46), minwidth=px(46), stretch=False)
        for col, text, width, anchor, stretch in (
            ("order", "#", 34, "center", False),
            ("name", "이름", 190, "w", True),
            ("kind", "종류", 140, "w", False),
            ("version", "버전", 56, "w", False),
            ("files", "파일", 46, "e", False),
            ("conflict", "충돌", 118, "w", False),
        ):
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=px(width), anchor=anchor, stretch=stretch)
        ysb = ttk.Scrollbar(area, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=autohide(ysb, side="right", fill="y"))
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_details())
        self.tree.bind("<ButtonPress-1>", self._on_press)
        self.tree.bind("<B1-Motion>", self._on_motion)
        self.tree.bind("<ButtonRelease-1>", self._on_release)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Button-3>", self._on_context)
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

        info = ttk.Frame(self.notebook, style="Card.TFrame")
        self.preview_label = ttk.Label(info, style="Card.TLabel", anchor="center")
        self.info_text = t.mark(tk.Text(info, wrap="word", font=t.fonts["base"], padx=px(18), pady=px(14),
                                        height=10, cursor="arrow"), "text")
        self.info_text.pack(fill="both", expand=True)
        self.info_text.configure(state="disabled", tabs=(px(84),))
        self.notebook.add(info, text="정보")

        self.options_tab = ttk.Frame(self.notebook, style="Card.TFrame")
        self.options_canvas = t.mark(tk.Canvas(self.options_tab), "canvas")
        osb = ttk.Scrollbar(self.options_tab, orient="vertical", command=self.options_canvas.yview)
        self.options_canvas.configure(yscrollcommand=autohide(osb, side="right", fill="y"))
        self.options_canvas.pack(side="left", fill="both", expand=True)
        self.options_inner = ttk.Frame(self.options_canvas, style="Card.TFrame", padding=(px(18), px(14)))
        window = self.options_canvas.create_window((0, 0), window=self.options_inner, anchor="nw")
        self.options_inner.bind("<Configure>", lambda e: self.options_canvas.configure(
            scrollregion=self.options_canvas.bbox("all")))
        self.options_canvas.bind("<Configure>", lambda e: self.options_canvas.itemconfigure(window, width=e.width))
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
        paned.add(right, weight=2)

        self.context_menu = t.mark(tk.Menu(self.tree, tearoff=False), "menu")

    def _configure_tags(self):
        p, f, px = self.theme.p, self.theme.fonts, self.theme.px
        for tree in (self.tree, self.files_tree):
            tree.tag_configure("odd", background=p["row_alt"])
            tree.tag_configure("disabled", foreground=p["subtle"])
            tree.tag_configure("overridden", foreground=p["warn"])
            tree.tag_configure("pending", foreground=p["subtle"])
            tree.tag_configure("attention", foreground=p["warn"])
        info = self.info_text
        info.tag_configure("title", font=f["detail_title"], spacing3=px(8))
        info.tag_configure("chip", background=p["chip"], foreground=p["chip_text"], font=f["small_bold"])
        info.tag_configure("chip_ok", background=p["success_bg"], foreground=p["success"], font=f["small_bold"])
        info.tag_configure("chip_off", background=p["raised"], foreground=p["muted"], font=f["small_bold"])
        info.tag_configure("chip_warn", background=p["warn_bg"], foreground=p["warn"], font=f["small_bold"])
        info.tag_configure("key", foreground=p["muted"], spacing1=px(3), spacing3=px(3))
        info.tag_configure("value", foreground=p["text"], spacing1=px(3), spacing3=px(3))
        info.tag_configure("muted", foreground=p["muted"])
        info.tag_configure("desc", spacing1=px(4), spacing2=px(3))
        for tag, fg_key, bg_key in (("warn", "warn", "warn_bg"),):
            info.tag_configure(tag, background=p[bg_key], foreground=p[fg_key], lmargin1=px(12), lmargin2=px(12),
                               rmargin=px(12), spacing1=px(8), spacing3=px(8))
            try:
                info.tag_configure(tag, lmargincolor=p[bg_key], rmargincolor=p[bg_key])
            except tk.TclError:  # Tk < 8.6.6
                pass
        info.tag_configure("warn_title", font=f["bold"])
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
        self._update_search_hint()
        self.refresh()

    def _bind_keys(self):
        self.root.bind("<Control-o>", lambda e: self.add_files())
        self.root.bind("<Control-s>", lambda e: self.apply())
        self.root.bind("<Control-f>", lambda e: (self.search_entry.focus_set(), "break")[1])
        self.root.bind("<F5>", lambda e: self.refresh())
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
        query = self._query()
        visible = [(i, mod) for i, mod in enumerate(m.mods)
                   if not query or query in mod.name.casefold() or query in mod.author.casefold()]
        existing = set(self.tree.get_children())
        on, off = t.image("check_on", 20), t.image("check_off", 20)
        for pos, (index, mod) in enumerate(visible):
            wins, losses = self._conflicts.counts(mod.id)
            files = m.mod_files(mod.id)
            conflict = " · ".join(x for x in (f"덮어씀 {wins}" if wins else "", f"밀림 {losses}" if losses else "") if x)
            tags = ["odd"] if pos % 2 else []
            if not mod.enabled:
                tags.append("disabled")
            elif mod.needs_option:
                tags.append("attention")
            elif losses and not wins and losses == sum(1 for f in files if not is_root_pak(f)):
                tags.append("overridden")
            name = mod.name
            if mod.options:
                name += ("   ·  옵션을 선택하세요" if mod.needs_option
                         else f"   ·  옵션 {len(mod.selected)}/{len(mod.options)}")
            values = (index + 1, name, kinds_short(self._display_kinds(mod)), mod.version, len(files), conflict)
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
        self.count_label.configure(text=f"{len(m.mods)}개 · 활성 {enabled}개" if m.mods else "")
        if not m.mods:
            self.empty_title.configure(text="아직 설치된 모드가 없습니다")
            self.empty_sub.configure(text="Nexus Mods 등에서 받은 zip · 7z · rar · pak 파일을\n이곳에 끌어 놓거나 아래 버튼을 누르세요.")
            self.empty_button.pack(pady=(self.theme.px(16), 0))
            self.empty.place(relx=0.5, rely=0.42, anchor="center")
        elif not visible:
            self.empty_title.configure(text="검색 결과가 없습니다")
            self.empty_sub.configure(text=f"'{self.search_var.get().strip()}'와(과) 일치하는 모드가 없습니다.")
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
        if not m.game_dir:
            self.refw_badge.configure(text="● REFramework", style="BadgeNeutral.TLabel")
        elif m.refw_installed():
            self.refw_badge.configure(text="● REFramework 설치됨", style="BadgeOk.TLabel")
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

    def _refw_tooltip(self) -> str:
        m = self.manager
        if not m.game_dir:
            return "게임 폴더를 지정하면 REFramework 설치 여부를 확인합니다"
        if m.refw_installed():
            return "dinput8.dll이 게임 폴더에 있습니다"
        text = "REFramework(dinput8.dll)가 없습니다. 클릭하면 다운로드 페이지를 엽니다."
        if m.needs_refw():
            text += "\n활성화된 루스 파일/스크립트 모드가 동작하려면 필요합니다."
        return text

    def _on_refw_click(self):
        if self.manager.game_dir and not self.manager.refw_installed():
            webbrowser.open(REFRAMEWORK_URL)

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
            self._show_preview(None)
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
            return
        m = self.manager
        self._show_preview(m.screenshot_path(mod))
        files = m.mod_files(mod.id)
        kinds = m.mod_kinds(mod.id)
        chunks = [(mod.name + "\n", "title")]
        chips = [("활성" if mod.enabled else "비활성", "chip_ok" if mod.enabled else "chip_off")]
        if mod.needs_option:
            chips.append(("옵션 선택 필요", "chip_warn"))
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
        if mod.needs_option:
            chunks += [("\n", None), ("옵션을 활성화해 주세요\n", ("warn", "warn_title")),
                       ("이 모드에는 옵션이 여러 개 있습니다. '옵션' 탭에서 사용할 옵션을 켜기 전에는 "
                        "게임에 아무 파일도 적용되지 않습니다.\n", "warn")]
        if kinds & {"loose", "script"} and not m.refw_installed():
            chunks += [("\n", None), ("REFramework가 필요합니다\n", ("warn", "warn_title")),
                       ("REFramework를 설치하고, 게임 안 REFramework 메뉴의 LooseFileLoader에서 "
                        "'Enable Loose File Loader'를 켜세요.\n", "warn")]
        if mod.description:
            chunks.append(("\n" + mod.description.replace("\\n", "\n") + "\n", "desc"))
        self._set_text(self.info_text, chunks)
        self._fill_files(mod, files)
        self._fill_conflicts(mod)
        self._fill_options(mod)

    def _fill_options(self, mod: Mod | None):
        for child in self.options_inner.winfo_children():
            child.destroy()
        self._option_vars = []
        if mod is None or not mod.options:
            self.notebook.tab(self.options_tab, state="hidden", text="옵션")
            return
        t, px, m = self.theme, self.theme.px, self.manager
        inner = self.options_inner
        self.notebook.tab(self.options_tab, state="normal",
                          text=f"옵션 {len(mod.selected)}/{len(mod.options)}" if mod.selected else "옵션 ●")
        ttk.Label(inner, text="옵션", style="CardTitle.TLabel").pack(anchor="w")
        if mod.needs_option:
            callout = ttk.Frame(inner, style="Callout.TFrame", padding=(px(14), px(10)))
            callout.pack(fill="x", pady=(px(10), px(4)))
            ttk.Label(callout, text="옵션을 활성화해 주세요", style="CalloutTitle.TLabel").pack(anchor="w")
            ttk.Label(callout, text="사용할 옵션을 아래에서 하나 이상 켜야 게임에 적용됩니다.",
                      style="Callout.TLabel", wraplength=px(380), justify="left").pack(anchor="w", pady=(px(2), 0))
        else:
            ttk.Label(inner, text="여러 옵션을 함께 켤 수 있습니다. 같은 파일이 겹치면 아래쪽 옵션이 우선합니다.",
                      style="CardMuted.TLabel", wraplength=px(380), justify="left").pack(anchor="w", pady=(px(4), px(4)))
        for opt in mod.options:
            var = tk.BooleanVar(value=opt["id"] in mod.selected)
            self._option_vars.append((opt["id"], var))
            row = ttk.Frame(inner, style="Card.TFrame")
            row.pack(fill="x", pady=(px(10), 0))
            t.checkbutton(row, opt["name"], var, card=True,
                          command=lambda: self._on_options_changed(mod.id)).pack(side="left")
            ttk.Button(row, text="이것만 사용", style="Link.TButton",
                       command=lambda oid=opt["id"]: self._set_options(mod.id, [oid])).pack(side="right")
            files = m.option_files(mod.id, opt["id"])
            ttk.Label(inner, text=f"{kinds_text(classify(files))} · 파일 {len(files)}개",
                      style="CardMuted.TLabel", font=t.fonts["small"]).pack(anchor="w", padx=(px(34), 0))
        buttons = ttk.Frame(inner, style="Card.TFrame")
        buttons.pack(fill="x", pady=(px(16), 0))
        ttk.Button(buttons, text="모두 켜기", style="Compact.TButton",
                   command=lambda: self._set_options(mod.id, [o["id"] for o in mod.options])).pack(side="left")
        ttk.Button(buttons, text="모두 끄기", style="Compact.TButton",
                   command=lambda: self._set_options(mod.id, [])).pack(side="left", padx=(px(6), 0))
        self._bind_wheel(inner, self.options_canvas)
        self.options_canvas.yview_moveto(0)

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
        if not self.can_mutate():
            self.root.after_idle(self._update_details)  # put the checkboxes back
            return
        self.manager.set_options(mod_id, selected)
        # rebuild after the click handler has returned (it destroys the clicked widget)
        self.root.after_idle(lambda: self._changed(select=mod_id))

    def show_options(self, mod_id: str):
        self.tree.selection_set(mod_id)
        self.tree.see(mod_id)
        self._update_details()
        self.notebook.select(self.options_tab)

    def _show_preview(self, path: Path | None):
        self._preview = None
        if path is not None:
            max_w, max_h = self.theme.px(380), self.theme.px(200)
            try:
                if Image is not None:
                    img = Image.open(path)
                    img.thumbnail((max_w, max_h))
                    self._preview = ImageTk.PhotoImage(img)
                elif path.suffix.lower() in (".png", ".gif"):
                    img = tk.PhotoImage(file=str(path))
                    factor = max(1, -(-img.width() // max_w), -(-img.height() // max_h))
                    self._preview = img.subsample(factor) if factor > 1 else img
            except Exception as exc:
                log.debug("미리보기 이미지를 열 수 없습니다: %s", exc)
                self._preview = None
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
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 2000:
            self.log_text.delete("1.0", f"{lines - 2000}.0")
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
                    self.status_var.set("준비")
                    if event[1]:
                        event[1](event[2])
                    self._after_task()
                elif kind == "error":
                    _, handler, exc, tb = event
                    self._finish_task()
                    self.status_var.set("오류가 발생했습니다")
                    if not isinstance(exc, EXPECTED_ERRORS):
                        log.error("예상하지 못한 오류:\n%s", tb)
                    if handler:
                        handler(exc)
                    else:
                        self.show_error("오류", str(exc))
                    self._after_task()
        except queue.Empty:
            pass
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
        try:
            problems = m.verify()
        except Exception as exc:
            log.warning("적용 상태를 점검하지 못했습니다: %s", exc)
            return
        if problems:
            for p in problems[:20]:
                log.warning(p)
            m.dirty = True
            m.save()
            self._update_header()
            if messagebox.askyesno(APP_TITLE, f"게임 폴더의 모드 파일 {len(problems)}개가 바뀌었거나 없어졌습니다.\n"
                                              "게임 업데이트 후라면 PAK 모드 번호를 다시 맞춰야 합니다.\n\n지금 다시 적용할까요?",
                                   parent=self.root):
                self.apply()

    # ------------------------------------------------------------------ game folder
    def browse_game(self):
        if not self._idle():
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
        if not self._idle():
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

        self.run_task("game", "게임 폴더 변경 중...", lambda p: self.manager.set_game_dir(path, p), on_done=done)

    # ------------------------------------------------------------------ install
    def add_files(self):
        if not self.can_mutate():
            return
        paths = filedialog.askopenfilenames(parent=self.root, title="모드 파일 선택", filetypes=INSTALL_FILETYPES)
        if paths:
            self.add_paths(list(paths))

    def add_folder(self):
        if not self.can_mutate():
            return
        path = filedialog.askdirectory(parent=self.root, title="모드 폴더 선택")
        if path:
            self.add_paths([path])

    def add_paths(self, paths: list[str]):
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
        existing = self.manager.find_by_name(package)
        replace_id = None
        if existing:
            answer = messagebox.askyesnocancel(
                "이미 설치된 모드",
                f"'{existing.name}' 모드가 이미 있습니다.\n\n"
                "예: 기존 모드를 새 파일로 교체 (업데이트)\n아니요: 별도의 모드로 추가\n취소: 설치하지 않음",
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
            self.root.after(1, self._next_install)

        def failed(exc):
            self.show_error("설치 실패", f"{name}\n\n{exc}")
            self.refresh()
            self.root.after(1, self._next_install)

        self.run_task("install", f"설치 중: {name}", work, on_done=done, on_error=failed)

    # ------------------------------------------------------------------ edits
    def toggle(self, ids: list[str]):
        if not ids or not self.can_mutate():
            return
        mods = [self.manager.get(i) for i in ids]
        target = not all(m.enabled for m in mods)
        for m in mods:
            self.manager.set_enabled(m.id, target)
        self._changed()

    def set_enabled_all(self, enabled: bool):
        if not self.manager.mods or not self.can_mutate():
            return
        for m in self.manager.mods:
            self.manager.set_enabled(m.id, enabled)
        self._changed()

    def move_selected(self, delta: int):
        ids = self._selected_ids()
        if len(ids) != 1 or not self.can_mutate():
            return
        self.manager.move(ids[0], delta)
        self._changed(select=ids[0])

    def move_to_edge(self, top: bool):
        ids = self._selected_ids()
        if len(ids) != 1 or not self.can_mutate():
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
        if not ids or not self._idle():
            return
        mods = [self.manager.get(i) for i in ids]
        names = "\n".join(f"  • {m.name}" for m in mods[:10]) + ("\n  ..." if len(mods) > 10 else "")
        if not messagebox.askyesno("모드 삭제", f"다음 모드를 삭제할까요? 저장된 모드 파일도 지워집니다.\n\n{names}", parent=self.root):
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
                more = f"\n... 외 {len(report.errors) - 8}개" if len(report.errors) > 8 else ""
                self.show_error("일부 파일을 적용하지 못했습니다",
                                "\n".join(report.errors[:8]) + more +
                                "\n\n게임이 실행 중이라면 종료한 뒤 다시 '게임에 적용'을 누르세요.")

        def failed(exc):
            self.show_error("게임에 적용", str(exc))
            self._update_header()

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
        if not m.game_dir or not self._idle():
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
        if self.manager.dirty and self.manager.game_dir and not messagebox.askyesno(
                "게임 실행", "아직 게임에 적용하지 않은 변경사항이 있습니다.\n그대로 실행할까요?", parent=self.root):
            return
        url = f"steam://rungameid/{STEAM_APP_ID}"
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

    def open_game_folder(self):
        if self.manager.game_dir and self.manager.game_dir.is_dir():
            fsutil.open_in_explorer(self.manager.game_dir)

    def open_mod_folder(self):
        mod = self._selected_mod()
        if mod:
            fsutil.open_in_explorer(self.manager.mod_dir(mod.id) if mod.options else self.manager.files_dir(mod.id))

    def open_settings(self):
        old_theme = self.manager.settings.theme
        if SettingsDialog(self.root, self.theme, self.manager).show():
            self.auto_var.set(self.manager.settings.auto_apply)
            if self.manager.settings.theme != old_theme:
                self.apply_theme(self.manager.settings.theme)
            else:
                self.refresh()
            if self.manager.dirty:
                self.request_apply()

    def show_help(self):
        messagebox.showinfo("사용법", (
            "1. 게임 폴더를 지정합니다 (보통 자동으로 찾습니다).\n"
            "2. '＋ 모드 추가'로 Nexus 등에서 받은 zip/7z/rar 파일을 추가하거나 창에 끌어 놓습니다.\n"
            "3. 체크박스로 모드를 켜고 끕니다. '즉시 적용'이 켜져 있으면 바로 게임 폴더에 반영됩니다.\n"
            "4. 같은 파일을 바꾸는 모드가 여러 개면 목록 아래쪽 모드가 이깁니다. 드래그나 ▲▼로 순서를 바꾸세요.\n\n"
            "• PAK 모드는 re_chunk_000.pak.sub_000.pak.patch_XXX.pak 번호를 자동으로 매깁니다.\n"
            "• 게임 업데이트 후에는 '☰ > 적용 상태 점검'을 실행하세요. 업데이트가 덮어쓴 번호를 피해서 다시 배치합니다.\n"
            "• natives 폴더(루스 파일) 모드와 Lua 스크립트는 REFramework가 필요합니다. REFramework도 이 프로그램으로 설치할 수 있습니다.\n"
            "• 7z/rar 파일은 PC에 설치된 반디집이나 7-Zip으로 풉니다."), parent=self.root)

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
            self.update_button.pack(side="right", padx=(0, self.theme.px(10)), after=self.refw_badge)
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
        messagebox.showinfo("정보", f"{APP_TITLE}\n버전 {__version__}\n\n데이터 폴더:\n{self.manager.data_dir}", parent=self.root)

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
        if not d or len(self.tree.selection()) > 1 or self._query():
            return
        if not d["moved"] and abs(event.y - d["y"]) < 8:
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
            menu.add_separator()
            menu.add_command(label="삭제", command=self.remove_selected)
        else:
            menu.add_command(label="모드 추가...", command=self.add_files)
        menu.tk_popup(event.x_root, event.y_root)

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
    try:
        import ctypes

        # Own taskbar group/icon instead of python.exe's when run from source.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MHWildsModManager")  # type: ignore[attr-defined]
    except Exception:
        pass


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
        data_path = Path(data_dir) if data_dir else None
        lock = acquire_instance_lock(data_path or default_data_dir())
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
