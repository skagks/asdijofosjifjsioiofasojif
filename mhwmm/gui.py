"""Tkinter user interface."""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import traceback
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__, fsutil, steam
from .analyzer import Variant
from .archive import ARCHIVE_EXTS, ArchiveError
from .constants import (APP_TITLE, DEFAULT_PAK_CHAIN, GAME_EXE, NEXUS_URL, PAK_CHAINS,
                        REFRAMEWORK_URL, STEAM_APP_ID)
from .deploy import DeployError, DeployReport, is_root_pak, path_key
from .manager import InstallError, InstallSession, Mod, ModManager, acquire_instance_lock, setup_logging

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
CHECK_ON, CHECK_OFF = "☑", "☐"
MAX_FILE_ROWS = 3000
INSTALL_FILETYPES = [
    ("모드 파일", " ".join(f"*{e}" for e in (*ARCHIVE_EXTS, ".pak", ".lua", ".dll"))),
    ("모든 파일", "*.*"),
]
GREEN, ORANGE, RED, GREY = "#1a7f37", "#b35900", "#c62828", "#777777"
# Errors that are shown to the user without a traceback in the log.
EXPECTED_ERRORS = (DeployError, InstallError, ArchiveError, OSError, ValueError)


def kinds_text(kinds: set[str]) -> str:
    return " + ".join(label for k, label in KIND_LABELS.items() if k in kinds) or "-"


def format_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


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


class Dialog(tk.Toplevel):
    """Small modal dialog base."""

    def __init__(self, parent, title: str):
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.transient(parent)
        self.resizable(True, True)
        self.result = None
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())

    def show(self):
        self.update_idletasks()
        parent = self.master
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_reqwidth()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_reqheight()) // 3)
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.grab_set()
        self.focus_set()
        self.wait_window()
        return self.result

    def cancel(self):
        self.result = None
        self.destroy()


class VariantDialog(Dialog):
    def __init__(self, parent, source_name: str, variants: list[Variant]):
        super().__init__(parent, "설치할 옵션 선택")
        self.variants = variants
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"'{source_name}' 안에 여러 옵션이 있습니다.\n"
                             "설치할 옵션을 고르세요. 선택한 옵션은 각각 별도의 모드로 추가됩니다.",
                  justify="left").pack(anchor="w", pady=(0, 8))
        frame = ttk.Frame(body)
        frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(frame, highlightthickness=0, height=min(320, 34 * len(variants) + 8))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.vars = []
        for i, v in enumerate(variants):
            var = tk.BooleanVar(value=(i == 0))
            self.vars.append(var)
            text = f"{v.name}    —  {kinds_text(v.kinds)}, {len(v.files)}개 파일"
            ttk.Checkbutton(inner, text=text, variable=var).pack(anchor="w", pady=2)
        inner.update_idletasks()
        canvas.configure(width=min(820, max(420, inner.winfo_reqwidth())))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="모두 선택", command=lambda: [v.set(True) for v in self.vars]).pack(side="left")
        ttk.Button(buttons, text="취소", command=self.cancel).pack(side="right")
        ttk.Button(buttons, text="설치", command=self.ok).pack(side="right", padx=6)
        self.bind("<Return>", lambda e: self.ok())

    def ok(self):
        chosen = [v for v, var in zip(self.variants, self.vars) if var.get()]
        if not chosen:
            messagebox.showwarning("선택 없음", "설치할 옵션을 하나 이상 선택하세요.", parent=self)
            return
        self.result = chosen
        self.destroy()


class SettingsDialog(Dialog):
    def __init__(self, parent, manager: ModManager):
        super().__init__(parent, "설정")
        self.manager = manager
        s = manager.settings
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="PAK 모드 대상 파일").grid(row=0, column=0, sticky="w", pady=4)
        self.chain = tk.StringVar(value=s.pak_chain)
        ttk.Combobox(body, textvariable=self.chain, values=PAK_CHAINS, width=36).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(body, text=f"기본값: {DEFAULT_PAK_CHAIN}  (파일 이름에 번호가 들어 있는 PAK은 그 이름을 따릅니다)",
                  foreground=GREY).grid(row=1, column=0, columnspan=2, sticky="w")

        self.auto_apply = tk.BooleanVar(value=s.auto_apply)
        self.enable_on_install = tk.BooleanVar(value=s.enable_on_install)
        self.check_running = tk.BooleanVar(value=s.check_game_running)
        ttk.Checkbutton(body, text="변경 즉시 게임 폴더에 적용", variable=self.auto_apply).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(12, 2))
        ttk.Checkbutton(body, text="설치한 모드를 바로 활성화", variable=self.enable_on_install).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(body, text="적용 전에 게임 실행 여부 확인", variable=self.check_running).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=2)

        ttk.Label(body, text="데이터 폴더").grid(row=5, column=0, sticky="w", pady=(12, 2))
        data_row = ttk.Frame(body)
        data_row.grid(row=5, column=1, sticky="ew", pady=(12, 2))
        ttk.Label(data_row, text=str(manager.data_dir), foreground=GREY).pack(side="left", fill="x", expand=True)
        ttk.Button(data_row, text="열기", command=lambda: fsutil.open_in_explorer(manager.data_dir)).pack(side="right")

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="확인", command=self.ok).pack(side="left", padx=6)
        ttk.Button(buttons, text="취소", command=self.cancel).pack(side="left")

    def ok(self):
        try:
            self.manager.update_settings(
                pak_chain=self.chain.get().strip().lower(),
                auto_apply=self.auto_apply.get(),
                enable_on_install=self.enable_on_install.get(),
                check_game_running=self.check_running.get(),
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
        self._drag: dict | None = None
        self._image = None
        self._conflicts = None
        self._closed = False

        root.title(f"{APP_TITLE} {__version__}")
        root.geometry("1180x760")
        root.minsize(900, 560)
        self._setup_style()
        self._build_menu()
        self._build_header()
        self._build_toolbar()
        self._build_statusbar()
        self._build_main()
        self._bind_keys()
        self._setup_dnd()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.refresh()
        root.after(100, self._poll_events)
        root.after(300, self._startup)

    # ------------------------------------------------------------------ layout
    def _setup_style(self):
        import tkinter.font as tkfont

        style = ttk.Style(self.root)
        if sys.platform != "win32" and "clam" in style.theme_names():
            style.theme_use("clam")
        if sys.platform == "win32" and "Malgun Gothic" in tkfont.families(self.root):
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                tkfont.nametofont(name).configure(family="Malgun Gothic")
        base = tkfont.nametofont("TkDefaultFont")
        self.font_bold = base.copy()
        self.font_bold.configure(weight="bold")
        self.font_title = base.copy()
        self.font_title.configure(weight="bold", size=abs(int(base.cget("size"))) + 3)
        style.configure("Treeview", rowheight=26)
        style.configure("Accent.TButton", font=self.font_bold)
        style.configure("Status.TLabel", padding=(6, 2))

    def _build_menu(self):
        m = tk.Menu(self.root)
        file_menu = tk.Menu(m, tearoff=False)
        file_menu.add_command(label="모드 파일 추가...", accelerator="Ctrl+O", command=self.add_files)
        file_menu.add_command(label="폴더에서 모드 추가...", command=self.add_folder)
        file_menu.add_separator()
        file_menu.add_command(label="게임 폴더 열기", command=self.open_game_folder)
        file_menu.add_command(label="데이터 폴더 열기", command=lambda: fsutil.open_in_explorer(self.manager.data_dir))
        file_menu.add_separator()
        file_menu.add_command(label="종료", command=self.on_close)
        m.add_cascade(label="파일", menu=file_menu)

        tools = tk.Menu(m, tearoff=False)
        tools.add_command(label="게임에 적용", accelerator="Ctrl+S", command=self.apply)
        tools.add_command(label="적용 상태 점검", command=self.verify)
        tools.add_command(label="게임 폴더에서 모든 모드 파일 제거", command=self.purge)
        tools.add_separator()
        tools.add_command(label="모두 활성화", command=lambda: self.set_enabled_all(True))
        tools.add_command(label="모두 비활성화", command=lambda: self.set_enabled_all(False))
        tools.add_separator()
        tools.add_command(label="설정...", command=self.open_settings)
        m.add_cascade(label="도구", menu=tools)

        links = tk.Menu(m, tearoff=False)
        links.add_command(label="REFramework 다운로드", command=lambda: webbrowser.open(REFRAMEWORK_URL))
        links.add_command(label="Nexus Mods (몬헌 와일즈)", command=lambda: webbrowser.open(NEXUS_URL))
        m.add_cascade(label="링크", menu=links)

        help_menu = tk.Menu(m, tearoff=False)
        help_menu.add_command(label="사용법", command=self.show_help)
        help_menu.add_command(label="정보", command=self.show_about)
        m.add_cascade(label="도움말", menu=help_menu)
        self.root.config(menu=m)

    def _build_header(self):
        top = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        top.pack(fill="x")
        ttk.Label(top, text="게임 폴더").pack(side="left")
        self.refw_label = ttk.Label(top, text="")
        self.refw_label.pack(side="right")
        self.refw_button = ttk.Button(top, text="받기", width=5, command=lambda: webbrowser.open(REFRAMEWORK_URL))
        ttk.Button(top, text="자동 감지", command=self.detect_game).pack(side="right", padx=(4, 12))
        ttk.Button(top, text="찾아보기...", command=self.browse_game).pack(side="right")
        self.game_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.game_var, state="readonly")
        entry.pack(side="left", fill="x", expand=True, padx=6)

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(10, 2, 10, 6))
        bar.pack(fill="x")
        self.buttons = {}

        def button(key, text, command, style=None, side="left", padx=(0, 4)):
            b = ttk.Button(bar, text=text, command=command, style=style) if style else ttk.Button(bar, text=text, command=command)
            b.pack(side=side, padx=padx)
            self.buttons[key] = b
            return b

        button("add", "＋ 모드 추가", self.add_files)
        button("folder", "폴더 추가", self.add_folder, padx=(0, 12))
        button("up", "▲ 위로", lambda: self.move_selected(-1))
        button("down", "▼ 아래로", lambda: self.move_selected(1))
        button("rename", "이름 변경", self.rename_selected)
        button("remove", "삭제", self.remove_selected, padx=(0, 12))
        button("launch", "▶ 게임 실행", self.launch_game, side="right", padx=(4, 0))
        button("apply", "게임에 적용", self.apply, style="Accent.TButton", side="right", padx=(4, 0))
        self.auto_var = tk.BooleanVar(value=self.manager.settings.auto_apply)
        ttk.Checkbutton(bar, text="즉시 적용", variable=self.auto_var, command=self._toggle_auto_apply).pack(side="right", padx=8)

    def _build_main(self):
        log_frame = ttk.LabelFrame(self.root, text="로그", padding=(6, 2))
        log_frame.pack(side="bottom", fill="x", padx=10, pady=(6, 0))
        self.log_text = tk.Text(log_frame, height=6, wrap="word", relief="flat", borderwidth=0, font="TkDefaultFont")
        lsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set, state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        self.log_text.tag_configure("warn", foreground=ORANGE)
        self.log_text.tag_configure("error", foreground=RED)

        paned = ttk.Panedwindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10)

        left = ttk.Frame(paned)
        hint = ttk.Label(left, text="아래쪽 모드일수록 우선 적용됩니다 · 드래그해서 순서 변경",
                         foreground=GREY)
        hint.pack(anchor="w", pady=(0, 4))
        cols = ("on", "order", "name", "kind", "version", "files", "conflict")
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", selectmode="extended")
        for col, text, width, anchor, stretch in (
            ("on", "사용", 44, "center", False),
            ("order", "#", 34, "center", False),
            ("name", "이름", 230, "w", True),
            ("kind", "종류", 120, "w", False),
            ("version", "버전", 60, "w", False),
            ("files", "파일", 50, "e", False),
            ("conflict", "충돌", 120, "w", False),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=stretch)
        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        self.tree.tag_configure("disabled", foreground=GREY)
        self.tree.tag_configure("overridden", foreground=ORANGE)
        self.empty_label = ttk.Label(tree_frame, text="'＋ 모드 추가'를 누르거나 모드 압축 파일을 여기로 끌어 놓으세요.\n(zip / 7z / rar / pak / 폴더)",
                                     foreground=GREY, justify="center")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_details())
        self.tree.bind("<ButtonPress-1>", self._on_press)
        self.tree.bind("<B1-Motion>", self._on_motion)
        self.tree.bind("<ButtonRelease-1>", self._on_release)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Button-3>", self._on_context)
        self.tree.bind("<Button-2>", self._on_context)
        paned.add(left, weight=3)

        right = ttk.Notebook(paned)
        info = ttk.Frame(right, padding=8)
        self.image_label = ttk.Label(info)
        self.image_label.pack(anchor="n")
        self.info_text = tk.Text(info, wrap="word", height=10, relief="flat", borderwidth=0, font="TkDefaultFont",
                                 background=self.root.cget("background") if sys.platform == "win32" else "#fbfbfb")
        self.info_text.pack(fill="both", expand=True, pady=(6, 0))
        self.info_text.tag_configure("title", font=self.font_title)
        self.info_text.tag_configure("key", foreground=GREY)
        self.info_text.tag_configure("warn", foreground=ORANGE)
        self.info_text.configure(state="disabled")
        right.add(info, text="정보")

        files = ttk.Frame(right, padding=4)
        self.files_tree = ttk.Treeview(files, columns=("path", "status"), show="headings")
        self.files_tree.heading("path", text="게임 폴더 기준 경로")
        self.files_tree.heading("status", text="상태")
        self.files_tree.column("path", width=300)
        self.files_tree.column("status", width=200)
        fsb = ttk.Scrollbar(files, orient="vertical", command=self.files_tree.yview)
        self.files_tree.configure(yscrollcommand=fsb.set)
        self.files_tree.pack(side="left", fill="both", expand=True)
        fsb.pack(side="right", fill="y")
        self.files_tree.tag_configure("overridden", foreground=ORANGE)
        self.files_tree.tag_configure("pending", foreground=GREY)
        right.add(files, text="파일")

        conflicts = ttk.Frame(right, padding=8)
        self.conflict_text = tk.Text(conflicts, wrap="word", relief="flat", borderwidth=0, font="TkDefaultFont",
                                     background=self.info_text.cget("background"))
        self.conflict_text.pack(fill="both", expand=True)
        self.conflict_text.tag_configure("head", font=self.font_bold)
        self.conflict_text.tag_configure("dim", foreground=GREY)
        self.conflict_text.configure(state="disabled")
        right.add(conflicts, text="충돌")
        paned.add(right, weight=2)

        self.menu = tk.Menu(self.root, tearoff=False)

    def _build_statusbar(self):
        bar = ttk.Frame(self.root, padding=(10, 4))
        bar.pack(side="bottom", fill="x")
        self.status_var = tk.StringVar(value="준비")
        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        self.state_label = ttk.Label(bar, text="", style="Status.TLabel")
        self.state_label.pack(side="right")
        self.progress = ttk.Progressbar(bar, length=220, mode="determinate")
        self.progress.pack(side="right", padx=8)

    def _bind_keys(self):
        self.root.bind("<Control-o>", lambda e: self.add_files())
        self.root.bind("<Control-s>", lambda e: self.apply())
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
            for widget in (self.tree, self.empty_label):
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
    def refresh(self, select: str | None = None):
        m = self.manager
        self._conflicts = m.conflicts()
        existing = set(self.tree.get_children())
        for index, mod in enumerate(m.mods):
            wins, losses = self._conflicts.counts(mod.id)
            files = m.mod_files(mod.id)
            conflict = ""
            if wins or losses:
                conflict = "  ".join(t for t in (f"덮어씀 {wins}" if wins else "", f"밀림 {losses}" if losses else "") if t)
            tags = []
            if not mod.enabled:
                tags.append("disabled")
            elif losses and not wins and losses == len([f for f in files if not is_root_pak(f)]):
                tags.append("overridden")
            values = (CHECK_ON if mod.enabled else CHECK_OFF, index + 1, mod.name,
                      kinds_text(m.mod_kinds(mod.id)), mod.version, len(files), conflict)
            if mod.id in existing:
                self.tree.item(mod.id, values=values, tags=tags)
                if self.tree.index(mod.id) != index:
                    self.tree.move(mod.id, "", index)
                existing.discard(mod.id)
            else:
                self.tree.insert("", index, iid=mod.id, values=values, tags=tags)
        for iid in existing:
            self.tree.delete(iid)
        if select and self.tree.exists(select):
            self.tree.selection_set(select)
            self.tree.see(select)
        if m.mods:
            self.empty_label.place_forget()
        else:
            self.empty_label.place(relx=0.5, rely=0.4, anchor="center")
        self._update_header()
        self._update_details()

    def _update_header(self):
        m = self.manager
        self.game_var.set(str(m.game_dir) if m.game_dir else "(설정되지 않음 — 찾아보기 또는 자동 감지)")
        if not m.game_dir:
            self.refw_label.configure(text="", foreground=GREY)
            self.refw_button.pack_forget()
        elif m.refw_installed():
            self.refw_label.configure(text="● REFramework 설치됨", foreground=GREEN)
            self.refw_button.pack_forget()
        else:
            needed = " (루스/스크립트 모드에 필요)" if m.needs_refw() else ""
            self.refw_label.configure(text=f"● REFramework 없음{needed}", foreground=RED if needed else ORANGE)
            self.refw_button.pack(side="right", padx=(6, 0), before=self.refw_label)
        if not m.game_dir:
            self.state_label.configure(text="게임 폴더 미설정", foreground=RED)
        elif m.dirty:
            self.state_label.configure(text="● 적용 필요", foreground=ORANGE)
        else:
            self.state_label.configure(text="✔ 게임에 적용됨", foreground=GREEN)

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

    def _set_text(self, widget: tk.Text, chunks):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for text, tag in chunks:
            widget.insert("end", text, tag)
        widget.configure(state="disabled")

    def _update_details(self):
        mod = self._selected_mod()
        self.files_tree.delete(*self.files_tree.get_children())
        if mod is None:
            self.image_label.configure(image="")
            self._image = None
            n = len(self._selected_ids())
            msg = f"{n}개 모드 선택됨" if n > 1 else "모드를 선택하면 정보가 표시됩니다."
            self._set_text(self.info_text, [(msg, "key")])
            self._set_text(self.conflict_text, [])
            return
        m = self.manager
        self._show_image(m.screenshot_path(mod))
        files = m.mod_files(mod.id)
        kinds = m.mod_kinds(mod.id)
        chunks = [(mod.name + "\n", "title")]
        for label, value in (
            ("상태", "활성" if mod.enabled else "비활성"),
            ("종류", kinds_text(kinds)),
            ("버전", mod.version),
            ("제작자", mod.author),
            ("분류", mod.category),
            ("파일", f"{len(files)}개, {format_size(m.mod_size(mod.id))}"),
            ("원본", mod.source),
            ("설치일", mod.installed_at.replace("T", " ")),
            ("업데이트", mod.updated_at.replace("T", " ")),
        ):
            if value:
                chunks += [(f"{label}: ", "key"), (f"{value}\n", None)]
        if kinds & {"loose", "script"} and not m.refw_installed():
            chunks.append(("\n이 모드는 REFramework가 필요합니다. 루스 파일 모드는 REFramework 메뉴의 "
                           "LooseFileLoader 항목에서 'Enable Loose File Loader'를 켜야 합니다.\n", "warn"))
        if mod.description:
            chunks.append(("\n" + mod.description.replace("\\n", "\n") + "\n", None))
        self._set_text(self.info_text, chunks)
        self._fill_files(mod, files)
        self._fill_conflicts(mod)

    def _show_image(self, path: Path | None):
        self._image = None
        if path is not None:
            try:
                if Image is not None:
                    img = Image.open(path)
                    img.thumbnail((360, 200))
                    self._image = ImageTk.PhotoImage(img)
                elif path.suffix.lower() in (".png", ".gif"):
                    img = tk.PhotoImage(file=str(path))
                    factor = max(1, -(-img.width() // 360), -(-img.height() // 200))
                    self._image = img.subsample(factor) if factor > 1 else img
            except Exception as exc:
                log.debug("미리보기 이미지를 열 수 없습니다: %s", exc)
                self._image = None
        self.image_label.configure(image=self._image or "")

    def _fill_files(self, mod: Mod, files: list[str]):
        deployed = self.manager.deployed_files()
        by_source = {(e.get("mod"), e.get("src")): e["path"] for e in deployed.values()}
        names = {x.id: x.name for x in self.manager.mods}
        conflicts = self._conflicts
        for rel in files[:MAX_FILE_ROWS]:
            key = path_key(rel)
            tag = ""
            if not mod.enabled:
                status = "비활성"
                tag = "pending"
            elif is_root_pak(rel):
                target = by_source.get((mod.id, rel))
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
            self.files_tree.insert("", "end", values=(rel, status), tags=(tag,) if tag else ())
        if len(files) > MAX_FILE_ROWS:
            self.files_tree.insert("", "end", values=(f"... 외 {len(files) - MAX_FILE_ROWS}개", ""))

    def _fill_conflicts(self, mod: Mod):
        names = {x.id: x.name for x in self.manager.mods}
        chunks = []
        if not mod.enabled:
            chunks.append(("비활성화된 모드는 충돌을 계산하지 않습니다.\n", "dim"))
        else:
            beats, beaten = self._conflicts.for_mod(mod.id) if self._conflicts else ({}, {})
            if not beats and not beaten:
                chunks.append(("다른 활성 모드와 겹치는 루스 파일이 없습니다.\n", None))
            for title, data in (("이 모드가 덮어쓰는 모드", beats), ("이 모드를 덮어쓰는 모드", beaten)):
                if not data:
                    continue
                chunks.append((f"{title}\n", "head"))
                for other, paths in data.items():
                    chunks.append((f"  • {names.get(other, other)} — {len(paths)}개 파일\n", None))
                    for p in paths[:8]:
                        chunks.append((f"      {p}\n", "dim"))
                    if len(paths) > 8:
                        chunks.append((f"      ... 외 {len(paths) - 8}개\n", "dim"))
                chunks.append(("\n", None))
        if "pak" in self.manager.mod_kinds(mod.id):
            chunks.append(("\nPAK 모드끼리의 충돌은 파일 내용을 열어보지 않으면 알 수 없습니다. "
                           "같은 대상을 바꾸는 PAK 모드가 있다면 목록 아래쪽 모드가 우선 적용됩니다.\n", "dim"))
        self._set_text(self.conflict_text, chunks)

    # ------------------------------------------------------------------ logging
    def _append_log(self, level: int, msg: str):
        tag = "error" if level >= logging.ERROR else "warn" if level >= logging.WARNING else None
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n", tag)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 2000:
            self.log_text.delete("1.0", f"{lines - 2000}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------ background tasks
    def can_mutate(self) -> bool:
        if self.busy in self.MUTATING_OK_DURING and not self._installing:
            return True
        self.root.bell()
        self.status_var.set("작업이 끝난 뒤에 다시 시도하세요.")
        return False

    def run_task(self, kind: str, label: str, func, on_done=None, on_error=None):
        self.busy = kind
        self.status_var.set(label)
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
        for b in self.buttons.values():
            b.state(["!disabled"])

    def _poll_events(self):
        try:
            while not self._closed:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._append_log(event[1], event[2])
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
                    self.status_var.set("오류")
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
        if self.busy is None and self._close_after_task:
            self._destroy()
            return
        if self.busy is None and self._apply_pending and not self._installing:
            self._apply_pending = False
            self.apply()
        self._update_header()

    def _idle(self) -> bool:
        """True when no background work is running; otherwise tell the user to wait."""
        if self.busy is None and not self._installing:
            return True
        self.root.bell()
        self.status_var.set("작업이 끝난 뒤에 다시 시도하세요.")
        return False

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
                                               "'찾아보기...'로 MonsterHunterWilds.exe가 있는 폴더를 지정하세요.",
                                    parent=self.root)
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
            messagebox.showinfo("자동 감지", "Steam 라이브러리에서 게임을 찾지 못했습니다. '찾아보기...'로 지정하세요.", parent=self.root)
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
        elif len(variants) > 1:
            chosen = VariantDialog(self.root, name, variants).show()
            if not chosen:
                session.cleanup()
                self.root.after(1, self._next_install)
                return
            variants = chosen

        plan = []
        for v in variants:
            existing = self.manager.find_by_name(v.name)
            replace_id = None
            if existing:
                answer = messagebox.askyesnocancel(
                    "이미 설치된 모드",
                    f"'{existing.name}' 모드가 이미 있습니다.\n\n"
                    "예: 기존 모드를 새 파일로 교체 (업데이트)\n아니요: 별도의 모드로 추가\n취소: 이 항목 건너뛰기",
                    parent=self.root)
                if answer is None:
                    continue
                replace_id = existing.id if answer else None
            plan.append((v, replace_id))
        if not plan:
            session.cleanup()
            self.root.after(1, self._next_install)
            return

        def work(progress):
            try:
                mods = []
                for i, (v, rid) in enumerate(plan):
                    progress(i, len(plan), f"설치 중: {v.name}")
                    mods.append(self.manager.install_variant(session, v, rid))
                return mods
            finally:
                session.cleanup()

        def done(mods):
            for v, _ in plan:
                if v.skipped:
                    log.info("  '%s'에서 게임에 필요 없는 파일 %d개는 제외했습니다 (예: %s)", v.name, len(v.skipped), v.skipped[0])
            self.refresh(select=mods[-1].id if mods else None)
            if any(mod.enabled for mod in mods):
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
        name = simpledialog.askstring("이름 변경", "새 이름:", initialvalue=mod.name, parent=self.root)
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
            fsutil.open_in_explorer(self.manager.files_dir(mod.id))

    def open_settings(self):
        if SettingsDialog(self.root, self.manager).show():
            self.auto_var.set(self.manager.settings.auto_apply)
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
            "• 게임 업데이트 후에는 '도구 > 적용 상태 점검'을 실행하세요. 업데이트가 덮어쓴 번호를 피해서 다시 배치합니다.\n"
            "• natives 폴더(루스 파일) 모드와 Lua 스크립트는 REFramework가 필요합니다. REFramework도 이 프로그램으로 설치할 수 있습니다.\n"
            "• 7z/rar 파일은 7-Zip이 설치되어 있어야 풀 수 있습니다."), parent=self.root)

    def show_about(self):
        messagebox.showinfo("정보", f"{APP_TITLE}\n버전 {__version__}\n\n데이터 폴더:\n{self.manager.data_dir}", parent=self.root)

    # ------------------------------------------------------------------ tree interaction
    def _on_press(self, event):
        self._drag = None
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if self.tree.identify_column(event.x) == "#1":
            if item in self.tree.selection() and len(self.tree.selection()) > 1:
                self.toggle(self._selected_ids())
            else:
                self.tree.selection_set(item)
                self.toggle([item])
            return "break"
        self._drag = {"item": item, "y": event.y, "moved": False}

    def _on_motion(self, event):
        d = self._drag
        if not d or len(self.tree.selection()) > 1:
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
        if self.tree.identify_region(event.x, event.y) != "cell" or self.tree.identify_column(event.x) == "#1":
            return
        item = self.tree.identify_row(event.y)
        if item:
            self.toggle([item])

    def _on_context(self, event):
        item = self.tree.identify_row(event.y)
        if item and item not in self.tree.selection():
            self.tree.selection_set(item)
        ids = self._selected_ids()
        self.menu.delete(0, "end")
        if ids:
            mods = [self.manager.get(i) for i in ids]
            self.menu.add_command(label="비활성화" if all(m.enabled for m in mods) else "활성화",
                                  command=lambda: self.toggle(ids))
            if len(ids) == 1:
                self.menu.add_separator()
                self.menu.add_command(label="맨 위로", command=lambda: self.move_to_edge(True))
                self.menu.add_command(label="맨 아래로 (최우선)", command=lambda: self.move_to_edge(False))
                self.menu.add_command(label="이름 변경", command=self.rename_selected)
                self.menu.add_command(label="저장된 모드 폴더 열기", command=self.open_mod_folder)
            self.menu.add_separator()
            self.menu.add_command(label="삭제", command=self.remove_selected)
        else:
            self.menu.add_command(label="모드 추가...", command=self.add_files)
        self.menu.tk_popup(event.x_root, event.y_root)

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


def _enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:
        pass


def main(data_dir=None) -> int:
    _enable_dpi_awareness()
    root = None
    if TkinterDnD is not None:
        try:
            root = TkinterDnD.Tk()
        except Exception:
            root = None
    if root is None:
        root = tk.Tk()
    events: queue.Queue = queue.Queue()
    try:
        data_path = Path(data_dir) if data_dir else None
        from .manager import default_data_dir

        lock = acquire_instance_lock(data_path or default_data_dir())
        if lock is None:
            root.withdraw()
            messagebox.showerror(APP_TITLE, "모드 매니저가 이미 실행 중입니다.")
            root.destroy()
            return 1
        manager = ModManager(data_path)
    except Exception as exc:
        messagebox.showerror(APP_TITLE, f"데이터 폴더를 열 수 없습니다:\n{exc}")
        root.destroy()
        return 1
    setup_logging(manager.data_dir, QueueLogHandler(events))
    App(root, manager, events)
    root.mainloop()
    lock.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
