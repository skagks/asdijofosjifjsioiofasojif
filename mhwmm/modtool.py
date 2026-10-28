"""모드 수정: the window that changes the mod picked in it, in place or as a new mod. Its tools are pages of one
window (one size whichever is shown): 파일 고르기 · 게임 수치 · 스크립트 설정 change the mod itself (an overlay of
edits the manager builds and deploys: mhwmm/modedit), 다른 장비로 옮기기 makes a new mod of it (retargetview),
수정 내용 lists what was changed and undoes it.

The window (ModToolDialog) holds what every page shares: the 모드 box (another mod picked switches every page to
it in place), the column of tools (할 일: a tool that does not apply to the mod is disabled and says why), the
unsaved changes of the edit pages (model.Pending, saved together by 저장), the button that saves or makes the new
mod, and the work done for the pages on other threads (their results come back through the app's events, those
for a mod shown before dropped). The mod's state is read on the GUI thread only: a snapshot of it
(manager.edit_snapshot, manager.equipment_job) is taken when a mod is picked and the pages' workers read only
that. The dialog only chooses: the app writes (App.open_mod_tool: _commit_edits, _create_retarget).

The pages are made from their modules when the window opens (modeditview, retargetview: imported here, not at the
top, as they import this module); one that cannot be made is shown disabled.

The window is frozen once shown (Dialog.FREEZE: its content keeps its size while the window's edge is dragged),
so nothing in it may change its requested size afterwards: every line whose text changes is as tall as its
tallest text from the start (steady), the panels have fixed widths, and the tables' heights are set once from the
screen (_fit)."""

from __future__ import annotations

import gc
import importlib
import logging
import threading
import tkinter as tk
import traceback
from dataclasses import replace
from tkinter import messagebox, ttk
from typing import NamedTuple

from .deploy import is_root_pak
from .equip.retarget import RetargetError
from .gui import EXPECTED_ERRORS, MOD_TOOL_TITLE, Dialog, Tooltip, _virtual_screen
from .modedit import EditError, model

log = logging.getLogger(__name__)

TOOLS = ("files", "data", "config", "retarget", "summary")  # the tools in the column's order
TOOL_TITLES = {"files": "파일 고르기", "data": "게임 수치", "config": "스크립트 설정",
               "retarget": "다른 장비로 옮기기", "summary": "수정 내용"}
EDIT_TOOLS = ("files", "data", "config")  # 이 모드 고치기 (the mod itself, saved with 저장)
NEW_TOOLS = ("retarget",)  # 새 모드 만들기
# the line under the window's title for each page (one line at 100% and 150%; 다른 장비로 옮기기 has its own)
INTROS = {
    "files": "파일 고르기: 게임에 넣지 않을 파일을 빼거나, 옵션끼리 겹치는 파일을 어느 옵션 것으로 쓸지 고릅니다.",
    "data": "게임 수치: 데이터 파일(.user)의 값(보상 · 확률 · 가격 등)을 바꿉니다. 모드의 원래 파일은 그대로 둡니다.",
    "config": "스크립트 설정: 모드에 든 Lua · JSON · INI · TOML 설정 파일의 값을 바꿉니다. 모드의 원래 파일은 그대로 둡니다.",
    "summary": "수정 내용: 이 모드에 한 수정을 모두 보고 되돌립니다. 모드를 업데이트하면 새 파일에 다시 적용합니다.",
}
# why a tool does not apply to a mod (the disabled button's tooltip)
NOT_DATA = "데이터 파일(.user)이 없는 모드입니다"
NOT_CONFIG = "스크립트 설정 파일(Lua · JSON · INI · TOML)이 없는 모드입니다"
NOT_RETARGET = "게임 파일(natives 폴더나 .pak)이 없는 모드입니다"
NOT_READ = "모드 수정 정보를 읽지 못했습니다 (자세한 내용은 로그)"
NOT_LOADED = "이 기능을 불러오지 못했습니다 (자세한 내용은 로그)"
# the pages: key -> (module in this package, class, keyword arguments)
PAGE_CLASSES = {
    "files": ("modeditview", "FilesPage", {}),
    "data": ("modeditview", "ValuesPage", {"tool": "data"}),
    "config": ("modeditview", "ValuesPage", {"tool": "config"}),
    "retarget": ("retargetview", "RetargetPage", {}),
    "summary": ("modeditview", "SummaryPage", {}),
}
MOD_BOX_STYLE = "ModPick.TCombobox"  # the 모드 box (its list made wider: _widen_list)
MOD_LIST_ROWS = 15  # names its list shows at once
NAV_WIDTH = 150  # the column of tools
PAGE_WIDTH = 850  # the pages at most (wider ones are given this)
FIT_ROWS = (3, 14)  # the pages' main tables: at least, at most (as many as the screen has room for)
SHARED_NOTE = "수정은 모든 프로필에 똑같이 쓰입니다"
WAIT_NOTE = "저장하지 않은 수정이 있습니다. 먼저 저장하거나 취소하세요."  # on 다른 장비로 옮기기
WHEEL_KEYS = ("<MouseWheel>", "<Button-4>", "<Button-5>", "<TouchpadScroll>")


class EditChoice(NamedTuple):
    """What the dialog gives back from an edit page: the changes to save, and the page to open the window on
    again when saving them fails."""
    plan: model.EditPlan
    page: str


def tool_applies(key: str, snap, manager=None, mod_id: str = "") -> str:
    """"" when the tool can work on the mod, else why not (from the snapshot's files, of every option): 게임 수치
    needs a .user data file in natives or a .pak, 스크립트 설정 a settings file textconfig offers, 다른 장비로 옮기기
    game files where the selected options put them (manager.mod_kinds)."""
    if key == "retarget":
        kinds = manager.mod_kinds(mod_id or snap.mod_id) if manager is not None else set()
        return "" if kinds & {"loose", "pak"} else NOT_RETARGET
    if snap is None:  # (the edit pages work from the snapshot)
        return NOT_READ
    if key == "data":
        return "" if any(model.is_data_rel(f.rel) or is_root_pak(f.rel) for f in snap.files) else NOT_DATA
    if key == "config":
        from .modedit.textconfig import listed

        return "" if any(listed(f.rel, f.size) for f in snap.files) else NOT_CONFIG
    return ""


def steady(parent, texts, wrap: int, **options) -> ttk.Label:
    """A label as tall as the tallest of ``texts`` wrapped at ``wrap`` pixels (none at all when every text is
    empty), in a frame of its own (pack or grid ``label.master``): its text changed later to any of them leaves
    the window's size as it is. It asks for no width (a longer text is cut off on the right)."""
    holder = ttk.Frame(parent)
    label = ttk.Label(holder, wraplength=wrap, justify="left", anchor="w", **options)
    label.pack(fill="x")
    height = 0
    for text in texts:
        if text:
            label.configure(text=text)
            height = max(height, label.winfo_reqheight())
    label.configure(text="")
    holder.configure(height=height, width=1)
    holder.pack_propagate(False)
    return label


_MISSING: set[str] = set()  # page modules that could not be imported (told once)


def _page_module(module: str):
    """The page module ``module`` of this package, the known ones imported by name in the code (the exe's
    builder, PyInstaller, bundles only modules it sees imported)."""
    if module == "modeditview":
        from . import modeditview
        return modeditview
    if module == "retargetview":
        from . import retargetview
        return retargetview
    return importlib.import_module(f".{module}", __package__)


def _page_class(module: str, name: str):
    """The page class ``name`` of ``module`` (in this package), or None when it cannot be imported."""
    try:
        return getattr(_page_module(module), name)
    except Exception as exc:
        if module not in _MISSING:
            _MISSING.add(module)
            if isinstance(exc, ModuleNotFoundError) and exc.name == f"{__package__}.{module}":
                log.warning("모드 수정의 %s 화면이 없습니다", module)
            else:
                log.error("모드 수정의 %s 화면을 불러오지 못했습니다:\n%s", module, traceback.format_exc())
        return None


def _drop_variables(obj) -> None:
    """Drop an object's Tk variables (alone, or in a list or dict) on the GUI thread (as
    Dialog._release_variables: left to the garbage collector they could be freed on another thread)."""
    for name, value in list(vars(obj).items()):
        items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else None
        if isinstance(value, tk.Variable) or (items and all(isinstance(v, tk.Variable) for v in items)):
            setattr(obj, name, None)


class Page:
    """One tool of the window, in its frame (a page of the shell's tabless notebook). What the shell calls:

    applies(snap)  "" or why the tool does not work on the mod (its button disabled, the reason as its tooltip)
    set_mod(snap)  the mod picked (a new snapshot): what was chosen for the one before goes
    show()/hide()  shown: its control in the top row (shell.top, row 1, column 1) gridded; hidden: grid_remove()d
                   (out of the Tab order)
    done(kind, tag, result)  a result of run() (the shell dropped those for a mod shown before and after closing)
    ready()        retarget: the new mod can be made
    result()       retarget: its Choice (the edit pages give None: the shell builds the EditChoice)
    first_focus()  the widget the keyboard goes to when the page is switched to by Ctrl+Tab
    fit(rows)      its main table ``rows`` high (1 while the shell measures the rest)
    refresh()      the pending changes changed on another page: redraw its marks
    unapplied()    "" or the value typed in it but not applied yet (Return or 적용 not pressed), as the questions
                   of 저장 and of giving up the changes name it
    apply_unapplied()  that value applied (an unsaved change); False when it could not be (the page shown, with
                   why under the value and the keyboard on it)
    close()        the window closes (cancel its after() calls)

    Pages work through the shell: shell.app, .manager, .theme, .snapshot (EditSnapshot), .equip_job,
    .pending (model.Pending: the unsaved changes of every edit page), .top, .run(key, kind, func, tag),
    .update_ok(), .pending_changed(), .select(key), .bind_keys(widget), .tool_empty(key, reason)."""

    key = ""
    title = ""
    intro = ""
    ok_text = "저장"
    top_text: str | None = None  # the label over its control in the top row (None: no control there)
    ANY_GENERATION: frozenset = frozenset()  # result kinds kept whichever mod asked (shared lists)

    def __init__(self, shell: "ModToolDialog", frame: ttk.Frame):
        self.shell, self.frame = shell, frame
        self.title = self.title or TOOL_TITLES.get(self.key, "")
        self.intro = self.intro or INTROS.get(self.key, "")
        self.top_label: ttk.Label | None = None
        self.top_control: tk.Widget | None = None
        if self.top_text is not None:
            px = shell.theme.px
            self.top_label = ttk.Label(shell.top, text=self.top_text, style="DialogSection.TLabel")
            self.top_label.grid(row=0, column=1, sticky="w", padx=(px(16), 0), pady=(0, px(4)))

    # ---- for the page's own code
    @property
    def app(self):
        return self.shell.app

    @property
    def manager(self):
        return self.shell.manager

    @property
    def theme(self):
        return self.shell.theme

    @property
    def closed(self) -> bool:
        return self.shell.closed

    def place_top(self, widget) -> None:
        """Put the page's control in the shell's top row (under top_text)."""
        self.top_control = widget
        widget.grid(row=1, column=1, sticky="w", padx=(self.shell.theme.px(16), 0))

    def run(self, kind: str, func, tag=None) -> None:
        """``func()`` on another thread; its result (or exception) comes back to done() on this one."""
        self.shell.run(self.key, kind, func, tag)

    # ---- what the shell calls
    def applies(self, snap) -> str:
        return tool_applies(self.key, snap, self.shell.manager, self.shell.mod_id)

    def set_mod(self, snap) -> None:
        pass

    def show(self) -> None:
        for widget in (self.top_label, self.top_control):
            if widget is not None:
                widget.grid()

    def hide(self) -> None:
        for widget in (self.top_label, self.top_control):
            if widget is not None:
                widget.grid_remove()

    def done(self, kind: str, tag, result) -> None:
        pass

    def ready(self) -> bool:
        return True

    def result(self):
        return None

    def first_focus(self):
        return self.top_control if self.top_control is not None else self.frame

    def fit(self, rows: int) -> None:
        pass

    def refresh(self) -> None:
        pass

    def unapplied(self) -> str:
        return ""

    def apply_unapplied(self) -> bool:
        return True

    def close(self) -> None:
        pass


class MissingPage(Page):
    """A tool whose page could not be made (its module failed to import or to build it): disabled."""

    def __init__(self, shell, frame, key: str):
        self.key = key
        super().__init__(shell, frame)
        ttk.Label(frame, text=NOT_LOADED, style="Muted.TLabel").pack(anchor="w")

    def applies(self, snap) -> str:
        return NOT_LOADED

    def ready(self) -> bool:
        return False


class ModToolDialog(Dialog):
    """모드 수정 on the mod picked in it (모드: the mods it can work on, App.editable, and the one it was opened with;
    ``mod_id`` first) with the tool ``page`` shown (else: the first that fits, _first_page); ``source``: the
    equipment 다른 장비로 옮기기 starts from; ``pending``: an EditPlan saved before that failed (its changes come back,
    those of files that are gone left out). Result: a retargetview.Choice (a new mod) or an EditChoice (the changes
    to save) for the mod picked last (``mod_id``), or None."""

    FREEZE = True

    def __init__(self, parent, app, mod_id: str, page: str | None = None, source=None, pending=None):
        super().__init__(parent, app.theme, MOD_TOOL_TITLE)
        px = self.theme.px
        self.body.configure(padding=px(18))  # (a little less than other dialogs: it fits 768 pixels at 125%)
        self.app, self.manager = app, app.manager
        self.closed = False
        self.initial_source = source  # (taken by 다른 장비로 옮기기 the first time it is set to a mod)
        # the mods it can work on, in the list's order (and the one it was opened with)
        self.mod_ids = [mod.id for mod in app.manager.mods if mod.id == mod_id or app.editable(mod)]
        # which mod the work on other threads was started for: what comes back for one shown before is dropped
        self._generation = 0
        self._list_keys = False  # the 모드 box's open list finds a typed letter (bound once: _widen_list)
        self._guarded: set = set()  # boxes guard_box has bound
        self._registry_lock = threading.Lock()
        self._registry = False  # the type registry held for 게임 수치 (rszdata.registry: released by destroy)
        self.mod_id: str = ""
        self.mod = None
        self.snapshot = None  # model.EditSnapshot of the mod (None: it could not be read)
        self.equip_job = None
        self.pending = model.Pending(model.Record())
        self._note = ""  # the footer's note on a plan given back (its changes of files that are gone left out)
        self._applies: dict[str, str] = {}
        self.page: Page | None = None
        # the tool the user picked last in the window (its column, Ctrl+Tab; never 수정 내용): what the app opens
        # the window on next time (App._mod_tool_page)
        self.picked = ""
        # the page shown when the window chose it (not asked for by name nor picked): a tool that finds nothing to
        # change in the mod gives way to the next one (tool_empty)
        self._auto = ""
        room = self._room_width() - px(36)

        ttk.Label(self.body, text=MOD_TOOL_TITLE, style="DialogTitle.TLabel").pack(anchor="w")
        # (as wide as the window and as tall as the longest page's line: _fit)
        self.intro = steady(self.body, [], room, style="Muted.TLabel")
        self.intro.master.pack(fill="x", pady=(px(4), 0))

        # 모드 | the page's own control (원래 장비, 파일, 보기) in one row; the mod first for Tab
        self.top = top = ttk.Frame(self.body)
        top.pack(fill="x", pady=(px(10), 0))
        ttk.Label(top, text="모드", style="DialogSection.TLabel").grid(row=0, column=0, sticky="w", pady=(0, px(4)))
        self.mod_var = tk.StringVar(self)
        # its list as wide as the longest name (_widen_list) and long enough to look through; a letter typed on
        # it goes to the next mod whose name starts with it (_find_mod)
        self.mod_box = ttk.Combobox(top, textvariable=self.mod_var, state="readonly", width=42,
                                    style=MOD_BOX_STYLE, height=min(MOD_LIST_ROWS, max(1, len(self.mod_ids))),
                                    values=[self._mod_label(i) for i in self.mod_ids], postcommand=self._widen_list)
        self.mod_box.grid(row=1, column=0, sticky="w")
        self.mod_box.bind("<<ComboboxSelected>>", lambda e: (self.mod_box.selection_clear(),
                                                             self._pick_mod(self.mod_box.current())))
        self.mod_box.bind("<KeyPress>", self._find_mod)
        self.guard_box(self.mod_box)

        columns = ttk.Frame(self.body)
        columns.pack(fill="both", expand=True, pady=(px(12), 0))
        # 할 일: the tools, in a column of its own width (it costs width, not height)
        nav = ttk.Frame(columns, width=px(NAV_WIDTH))
        nav.pack(side="left", fill="y", padx=(0, px(16)))
        nav.pack_propagate(False)
        self.nav: dict[str, ttk.Button] = {}
        self.tips: dict[str, Tooltip] = {}  # (why a tool is off)

        def header(text, gap):
            ttk.Label(nav, text=text, style="DialogSection.TLabel").pack(anchor="w", pady=(gap, px(4)))

        header("이 모드 고치기", 0)
        for key in EDIT_TOOLS:
            self._nav_button(nav, key)
        header("새 모드 만들기", px(12))
        for key in NEW_TOOLS:
            self._nav_button(nav, key)
        ttk.Separator(nav).pack(fill="x", pady=(px(10), px(8)))
        self._nav_button(nav, "summary")
        # the pages: a notebook without tabs (the column picks the page; hidden pages are unmapped, so out of the
        # Tab order, and the window is as big as the biggest page whichever is shown)
        self.book = ttk.Notebook(columns, style="Pages.TNotebook", takefocus=False)
        self.book.pack(side="left", fill="both", expand=True)

        # 저장 / 새 모드 만들기 and 취소 (made in this order: Tab goes from the page to them), what is not saved yet
        # on the left
        self.button_row = row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(px(12), 0))
        self.ok_button = ttk.Button(row, text="저장", style="Accent.TButton", command=self.ok)
        cancel = ttk.Button(row, text="취소", command=self.cancel)
        cancel.pack(side="right")
        self.ok_button.pack(side="right", padx=(0, px(8)))
        self.footer = steady(row, ["저장하지 않은 수정 0개"], room, style="Muted.TLabel")
        self.footer.master.pack(side="left", fill="x", expand=True, padx=(0, px(16)))

        self.pages: dict[str, Page] = {}
        for key in TOOLS:
            self.pages[key] = self._make_page(key)
        for page_ in self.pages.values():  # (each page's top control was gridded when it was made)
            page_.hide()
        widths = [w.winfo_reqwidth() for p in self.pages.values() for w in (p.top_label, p.top_control)
                  if w is not None]
        top.columnconfigure(1, minsize=max(widths, default=0) + px(16))  # (one width whichever page is shown)

        for key in ("<Return>", "<KP_Enter>"):
            self.bind(key, lambda e: (self.ok(), "break")[1])
        for key in ("<Control-s>", "<Control-S>"):
            self.bind(key, lambda e: (self._save_key(), "break")[1])
        self.bind("<Control-Tab>", lambda e: (self.step(1), "break")[1])
        for key in ("<Control-Shift-Tab>", "<Control-ISO_Left_Tab>"):  # (X11 sends ISO_Left_Tab with Shift)
            try:
                self.bind(key, lambda e: (self.step(-1), "break")[1])
            except tk.TclError:
                pass
        self.bind_keys(self.body)

        self._set_mod(mod_id, pending)
        first = self._first_page(page)
        self.select(first, force=True)  # (one that does not work only when asked for or none does)
        self._auto = "" if page in self.pages else first
        self._fit()

    # ---- building
    def _nav_button(self, nav, key: str) -> None:
        px = self.theme.px
        button = ttk.Button(nav, text=TOOL_TITLES[key], style="Nav.TButton", command=lambda: self.select(key))
        button.pack(fill="x", pady=(0, px(2)))
        self.tips[key] = Tooltip(self.theme, button, lambda: self._applies.get(key, ""))
        self.nav[key] = button

    def _make_page(self, key: str) -> Page:
        """The page of ``key`` from its module, or a disabled stand-in when it cannot be made."""
        frame = ttk.Frame(self.book)
        self.book.add(frame)
        module, name, kwargs = PAGE_CLASSES[key]
        cls = _page_class(module, name)
        if cls is not None:
            try:
                page = cls(self, frame, **kwargs)
                page.key, page.title = key, TOOL_TITLES[key]  # (the window's names for it)
                page.intro = page.intro or INTROS.get(key, "")
                return page
            except Exception:
                log.error("모드 수정의 '%s'을(를) 만들지 못했습니다:\n%s", TOOL_TITLES[key], traceback.format_exc())
        for child in frame.winfo_children():
            child.destroy()
        for child in self.top.grid_slaves(column=1):  # (what it put in the top row before failing)
            if child not in {w for p in self.pages.values() for w in (p.top_label, p.top_control)}:
                child.destroy()
        return MissingPage(self, frame, key)

    @staticmethod
    def _height(label: ttk.Label, text: str) -> int:
        """How tall ``label`` (a steady one) is with ``text``."""
        if not text:
            return 0
        shown = label.cget("text")
        label.configure(text=text)
        height = label.winfo_reqheight()
        label.configure(text=shown)
        return height

    def guard_box(self, box: ttk.Combobox) -> None:
        """A readonly box of the window: Return on it does nothing (the window's Return would save or make the
        mod), the wheel over it closed picks nothing (it would drop what was chosen), and a pick leaves no text
        selected."""
        for key in ("<Return>", "<KP_Enter>"):
            box.bind(key, lambda e: "break")
        for key in WHEEL_KEYS:
            try:
                box.bind(key, lambda e: "break")
            except tk.TclError:  # (<TouchpadScroll>: Tk 9 only)
                pass
        box.bind("<<ComboboxSelected>>", lambda e: box.selection_clear(), add="+")
        self._guarded.add(box)

    def bind_keys(self, widget) -> None:
        """Return or Enter on a button or check box of ``widget`` (and in it) presses it (as on Windows), not the
        window's 저장; the boxes are guarded (guard_box). Pages call it for widgets they make later."""
        cls = widget.winfo_class()
        if cls in ("TButton", "TCheckbutton", "TRadiobutton"):
            for key in ("<Return>", "<KP_Enter>"):
                widget.bind(key, lambda e, w=widget: (self._press(w), "break")[1])
        elif cls == "TCombobox" and widget is not self.mod_box and widget not in self._guarded:
            self.guard_box(widget)
        for child in widget.winfo_children():
            self.bind_keys(child)

    def _press(self, widget) -> None:
        if widget.instate(["disabled"]):
            self.bell()
        else:
            widget.invoke()

    # ---- fitting the screen
    def _room(self) -> int:
        """The height the window may take: the screen less the taskbar and the title bar, or the window manager's
        limit (on Windows the work area) when that is less (as LookDialog._room)."""
        px = self.theme.px
        return min(self.winfo_screenheight() - px(80), self.wm_maxsize()[1] - px(16))

    def _room_width(self) -> int:
        px = self.theme.px
        return min(self.winfo_screenwidth() - px(16), self.wm_maxsize()[0] - px(16))

    def _position(self) -> tuple[int, int]:
        """Over its parent as other dialogs, but kept on the screen (it is as tall as the screen allows: a third
        of the way down a low main window would put its buttons below the taskbar)."""
        x, y = super()._position()
        left, top, right, bottom = _virtual_screen(self)
        px = self.theme.px
        x = max(left, min(x, right - self.winfo_reqwidth() - px(8)))
        y = max(top, min(y, bottom - self.winfo_reqheight() - px(48)))  # (the title bar, the taskbar)
        return x, y

    def _fit(self) -> None:
        """The pages' area as wide as the biggest page asks (at most PAGE_WIDTH and the screen), the line under
        the title wrapped at the window's width, and the pages' tables as high as the screen has room for
        (FIT_ROWS), measured with everything built: the window is the same size on every page and for every mod
        (the pages keep the size given here)."""
        px = self.theme.px
        self.book.configure(width=0, height=0)
        for page in self.pages.values():
            page.fit(1)
        self.update_idletasks()
        others = self.winfo_reqwidth() - self.book.winfo_reqwidth()
        frames = [p.frame for p in self.pages.values()]
        width = max(1, min(max(fr.winfo_reqwidth() for fr in frames), px(PAGE_WIDTH), self._room_width() - others))
        self.book.configure(width=width)
        self.update_idletasks()
        wrap = self.winfo_reqwidth() - 2 * px(18)  # (the body's padding)
        self.intro.configure(wraplength=wrap)
        self.intro.master.configure(height=max([self._height(self.intro, p.intro) for p in self.pages.values()],
                                               default=0))
        self.update_idletasks()
        rest = self.winfo_reqheight()
        try:
            row = int(ttk.Style(self).lookup("Treeview", "rowheight") or 0) or px(34)
        except (tk.TclError, ValueError):
            row = px(34)
        low, high = FIT_ROWS
        rows = max(low, min(high, 1 + (self._room() - rest) // row))
        for page in self.pages.values():
            page.fit(rows)
        self.update_idletasks()
        self.book.configure(height=max(fr.winfo_reqheight() for fr in frames))
        self.update_idletasks()
        self.rows = rows
        self.minsize(self.winfo_reqwidth(), self.winfo_reqheight())

    # ---- the mod
    def _mod_label(self, mod_id: str) -> str:
        """A mod in the 모드 box: its name, (꺼짐) when it is off."""
        mod = self.manager.get(mod_id)
        return mod.name if mod.enabled else f"{mod.name} (꺼짐)"

    def _widen_list(self):
        """The 모드 box's list as wide as its longest name (within the screen), so that names that start the
        same and (꺼짐) can be read."""
        px, box = self.theme.px, self.mod_box
        font = self.theme.fonts["base"]
        widest = max((font.measure(label) for label in box.cget("values")), default=0) + px(40)  # (scrollbar)
        room = self.winfo_screenwidth() - box.winfo_rootx() - px(8)
        extra = max(0, min(widest, room) - box.winfo_width())
        ttk.Style(self).configure(MOD_BOX_STYLE, postoffset=(0, 0, extra, 0))
        if not self._list_keys:  # a letter typed in the open list: the next name starting with it (not picked yet)
            self._list_keys = True
            popdown = self.tk.call("ttk::combobox::PopdownWindow", box)
            listbox = f"{popdown}.f.l"

            def find(char):
                found = self._next_named(char, int(self.tk.call(listbox, "index", "active")))
                if found is not None:
                    self.tk.call(listbox, "selection", "clear", 0, "end")
                    self.tk.call(listbox, "selection", "set", found)
                    self.tk.call(listbox, "activate", found)
                    self.tk.call(listbox, "see", found)

            self.tk.call("bind", listbox, "<KeyPress>", f"+{self.register(find)} %A")

    def _find_mod(self, event):
        """A letter typed on the 모드 box: the next mod whose name starts with it (after the one shown)."""
        found = self._next_named(event.char, self.mod_box.current())
        if found is not None:
            self.mod_box.current(found)
            self._pick_mod(found)
        return "break" if event.char and event.char.isprintable() else None

    def _next_named(self, char: str, after: int) -> int | None:
        """The index of the next mod (after ``after``, round) whose name starts with ``char`` (any case)."""
        if not char or not char.isprintable() or char.isspace():
            return None
        char = char.casefold()
        count = len(self.mod_ids)
        for step in range(1, count + 1):
            i = (after + step) % count
            if self.manager.get(self.mod_ids[i]).name.casefold().startswith(char):
                return i
        return None

    def _pick_mod(self, index: int):
        """Another mod picked in the 모드 box: the unsaved changes of this one are given up only when the user
        says so (else the box shows this one again)."""
        if not 0 <= index < len(self.mod_ids) or self.mod_ids[index] == self.mod_id:
            return
        if not self._discard_ok():
            self.mod_box.current(self.mod_ids.index(self.mod_id))
            return
        self._set_mod(self.mod_ids[index])
        if self.page is not None and self._applies.get(self.page.key):  # (the tool does not work on this mod)
            first = self._first_page()
            self.select(first, force=True)
            self._auto = first
        elif self.page is not None:  # (picked or asked for on the mod before: on this one it gives way when empty)
            self._auto = self.page.key

    def _discard_ok(self) -> bool:
        """The unsaved changes (and the values typed but not applied) given up when the user says so."""
        typed = self._typed()
        n = self.pending.count() + len(typed)
        if not n:
            return True
        text = f"저장하지 않은 수정 {n}개를 버릴까요?"
        if typed:
            text += "\n\n적용하지 않은 값:\n" + "\n".join(f"• {what}" for _page, what in typed)
        return messagebox.askyesno(MOD_TOOL_TITLE, text, parent=self)

    def _typed(self) -> list[tuple[Page, str]]:
        """The values typed in the pages (shown or not) but not applied: (page, what the value is)."""
        out = []
        for page in self.pages.values():
            try:
                what = page.unapplied()
            except Exception:
                log.error("모드 수정 창의 입력을 알지 못했습니다:\n%s", traceback.format_exc())
                what = ""
            if what:
                out.append((page, what))
        return out

    def _apply_typed(self) -> bool:
        """Before 저장: the values typed but not applied are applied first when the user says so (아니요: saved
        without them; with nothing else to save, the window stays). False when the window stays: 취소, or a value
        that cannot be read (its page shows why)."""
        typed = self._typed()
        if not typed:
            return True
        lines = "\n".join(f"• {what}" for _page, what in typed)
        if not self.pending.count():  # (without them nothing would be saved)
            answer = messagebox.askyesno(MOD_TOOL_TITLE, f"적용하지 않은 값이 있습니다:\n{lines}\n\n적용하고 저장할까요?",
                                         parent=self) or None
        else:
            answer = messagebox.askyesnocancel(
                MOD_TOOL_TITLE, f"적용하지 않은 값이 있습니다:\n{lines}\n\n예: 적용하고 저장합니다\n아니요: 적용하지 않은 값은 "
                                "빼고 저장합니다", parent=self)
        if answer is None:
            return False
        if answer:
            for page, _what in typed:
                if not page.apply_unapplied():  # (the page says why)
                    return False
        return True

    def _set_mod(self, mod_id: str, plan: model.EditPlan | None = None):
        """Every page on mod ``mod_id``: its state read again on this thread (what the pages' workers work from),
        what was chosen or changed for the mod before given up (``plan``: changes to start from) and what is still
        being worked out for it dropped when it comes back (_generation)."""
        self._generation += 1
        self.mod_id = mod_id
        self.mod = self.manager.get(mod_id)
        self.mod_box.current(self.mod_ids.index(mod_id))
        try:
            self.snapshot = self.manager.edit_snapshot(mod_id, conflicts=self.app._conflicts)
        except Exception:
            log.error("모드 수정 정보를 읽지 못했습니다:\n%s", traceback.format_exc())
            self.snapshot = None
        self.equip_job = self.manager.equipment_job(mod_id)
        record = self.snapshot.record if self.snapshot is not None else model.Record()
        self._note = ""
        self.pending = model.Pending(record)
        if plan is not None and plan.mod_id == mod_id and self.snapshot is not None:
            self._restore(plan)
        for key, page in self.pages.items():
            try:
                self._applies[key] = page.applies(self.snapshot)
            except Exception:
                log.error("모드 수정의 '%s'을(를) 쓸 수 있는지 알지 못했습니다:\n%s", TOOL_TITLES[key],
                          traceback.format_exc())
                self._applies[key] = NOT_LOADED
        for key, page in self.pages.items():
            try:
                page.set_mod(self.snapshot)
            except Exception:
                log.error("모드 수정의 '%s'을(를) 이 모드로 바꾸지 못했습니다:\n%s", TOOL_TITLES[key],
                          traceback.format_exc())
                self._applies[key] = NOT_LOADED
        self._update_nav()
        self.update_ok()

    def _restore(self, plan: model.EditPlan) -> None:
        """The changes of a save that failed, again (the window opened once more): those of files that are not
        in the mod any more are left out, and the footer says so."""
        snap, record = self.snapshot, self.snapshot.record
        keys = {f.key for f in snap.files}
        options = self.mod.options

        def here(source) -> bool:
            key = model.key_of(options, source)
            return key is not None and key in keys

        kept = replace(plan, exclude={s: v for s, v in plan.exclude.items() if here(s)},
                       members={k: v for k, v in plan.members.items() if here(k[0])},
                       reapply=frozenset(s for s in plan.reapply if record.item(s) is not None),
                       drop=frozenset(s for s in plan.drop if record.item(s) is not None))
        try:
            self.pending = model.Pending.from_plan(record, kept)
        except EditError as exc:
            log.warning("저장하지 못한 모드 수정을 다시 열지 못했습니다: %s", exc)
            self.pending = model.Pending(record)
            kept = replace(plan, exclude={}, members={}, reapply=frozenset(), drop=frozenset(), reset=False,
                           clear_bad=False)
        lost = plan.count() - kept.count()
        if lost:
            self._note = f"지금은 없는 파일의 수정 {lost}개는 빼고 다시 열었습니다"

    # ---- pages
    def _first_page(self, wanted: str | None = None) -> str:
        """The page the window opens on: the one asked for; else 수정 내용 when something of the mod's edits needs
        the user; else the tool the user picked last in this session (App._mod_tool_page) when it works on the mod
        (다른 장비로 옮기기 as below); else 다른 장비로 옮기기 for a mod known to change equipment; else the first of
        게임 수치, 스크립트 설정, 파일 고르기 that works."""
        if wanted in self.pages:
            return wanted

        def fits(key) -> bool:
            return key in self.pages and not self._applies.get(key)

        def looks() -> bool:  # (다른 장비로 옮기기 only for a mod known to change equipment)
            try:
                return bool(self.manager.equipment_summary(self.mod_id))
            except Exception as exc:
                log.info("장비 외형을 알아보지 못했습니다: %s", exc)
                return False

        if self.snapshot is not None and self.snapshot.issues and fits("summary"):
            return "summary"
        last = getattr(self.app, "_mod_tool_page", None)
        if last and fits(last) and (last != "retarget" or looks()):
            return last
        if fits("retarget") and looks():
            return "retarget"
        return next((key for key in ("data", "config", "files") if fits(key)),
                    next((key for key in TOOLS if fits(key)), "files"))

    @property
    def page_key(self) -> str:
        return self.page.key if self.page is not None else ""

    def select(self, key: str, force: bool = False) -> None:
        """Show the page of ``key`` (a tool that does not work on the mod only when ``force``: asked for by name
        or chosen by the window; else the user picked it: remembered, 수정 내용 aside)."""
        page = self.pages.get(key)
        if page is None or self.closed:
            return
        if self._applies.get(key) and not force:
            self.bell()
            return
        if not force:
            self._auto = ""
            if key != "summary":
                self.picked = key
        old = self.page
        if old is page:
            return
        try:
            focus = self.tk.call("focus")
        except tk.TclError:
            focus = ""
        if old is not None:
            old.hide()
        self.page = page
        page.show()
        self.book.select(page.frame)
        self.intro.configure(text=page.intro)
        self._update_nav()
        self.update_ok()
        if old is not None and focus and (str(focus).startswith(str(old.frame) + ".") or
                                          any(str(focus) == str(w) for w in (old.top_control,) if w is not None)):
            target = page.first_focus()  # (the keyboard was on what is hidden now)
            if target is not None:
                target.focus_set()

    def step(self, direction: int) -> None:
        """Ctrl+Tab / Ctrl+Shift+Tab: the next or previous tool that works on the mod."""
        keys = [k for k in TOOLS if k in self.pages and not self._applies.get(k)]
        if not keys:
            self.bell()
            return
        at = keys.index(self.page.key) if self.page is not None and self.page.key in keys else \
            (-1 if direction > 0 else 0)
        target = keys[(at + direction) % len(keys)]
        if self.page is not None and target == self.page.key:
            return
        self.select(target)
        focus = self.page.first_focus() if self.page is not None else None
        if focus is not None:
            focus.focus_set()

    def _update_nav(self) -> None:
        """The tools' buttons: the shown one marked, those that do not work on the mod disabled, 수정 내용 with how
        many edits the mod has (⚠ and how many things need the user)."""
        for key, button in self.nav.items():
            button.configure(style="NavActive.TButton" if self.page is not None and self.page.key == key
                             else "Nav.TButton")
            button.state(["disabled"] if self._applies.get(key) else ["!disabled"])
        issues = len(self.snapshot.issues) if self.snapshot is not None else 0
        edits = self._edit_count()
        title = TOOL_TITLES["summary"]
        self.nav["summary"].configure(text=f"⚠ {title} ({issues})" if issues else
                                      f"{title} ({edits})" if edits else title)

    def tool_empty(self, key: str, reason: str) -> None:
        """Page ``key`` found nothing it can change in the mod (게임 수치: a PAK without data files): its tool is
        off with ``reason``, and when the window chose it (not asked for by name nor picked) the next tool that
        fits the mod is shown instead."""
        if key not in self.pages or self.closed or self._applies.get(key):
            return
        self._applies[key] = reason
        self._update_nav()
        if self.page is self.pages[key] and self._auto == key:
            first = self._first_page()
            if first != key:
                self.select(first, force=True)
                self._auto = first

    def _edit_count(self) -> int:
        """The mod's saved edits: files left out and values changed (or to check)."""
        if self.snapshot is None:
            return 0
        return sum(int(item.exclude) + item.changes() + item.conflicts() for item in self.snapshot.record.items)

    # ---- the pages' shared state
    @property
    def create_button(self) -> ttk.Button:
        """The window's main button (새 모드 만들기 on 다른 장비로 옮기기, else 저장)."""
        return self.ok_button

    def update_ok(self) -> None:
        """The main button (the page's text, usable or not) and the line beside it (what is not saved yet)."""
        page = self.page
        if page is None:
            return
        n = self.pending.count()
        if page.key in NEW_TOOLS:
            ready = not n and self._page_ready(page)
            text = WAIT_NOTE if n else ""
        else:
            ready = bool(n)
            text = f"저장하지 않은 수정 {n}개 · {SHARED_NOTE}" if n else ""
        self.ok_button.configure(text=page.ok_text)
        self.ok_button.state(["!disabled"] if ready else ["disabled"])
        self.footer.configure(text=" · ".join(filter(None, (text, self._note))))

    @staticmethod
    def _page_ready(page: Page) -> bool:
        try:
            return bool(page.ready())
        except Exception:
            log.error("모드 수정 창의 상태를 알지 못했습니다:\n%s", traceback.format_exc())
            return False

    def pending_changed(self) -> None:
        """A page changed the unsaved changes: the others redraw their marks, the button and the footer follow."""
        for page in self.pages.values():
            if page is not self.page:
                try:
                    page.refresh()
                except Exception:
                    log.error("모드 수정 창을 다시 그리지 못했습니다:\n%s", traceback.format_exc())
        self.update_ok()

    # ---- background work
    def run(self, page_key: str, kind: str, func, tag=None) -> None:
        """``func()`` on another thread for page ``page_key``; its result (or exception) comes back to the page's
        done() on this one, unless another mod was picked meanwhile (_generation) or the window was closed.
        ``func`` gets nothing of the window: only plain data taken on this thread (the snapshot)."""
        events, generation = self.app.events, self._generation
        hold = page_key == "data"  # (게임 수치: the type registry kept loaded while the window is open)
        gc.collect()  # (here: tk objects left for the collector must not be freed on the worker thread)

        def worker():
            try:
                if hold:
                    self._hold_registry()
                result = func()
            except Exception as exc:  # shown in the page
                if not isinstance(exc, (*EXPECTED_ERRORS, EditError, RetargetError)):
                    log.error("모드 수정의 '%s'을(를) 준비하다가 오류가 났습니다:\n%s", TOOL_TITLES.get(page_key, page_key),
                              traceback.format_exc())
                result = exc
            events.put(("call", self._done, kind, tag, result, generation, page_key))

        threading.Thread(target=worker, daemon=True).start()

    def _done(self, kind: str, tag, result, generation: int, page: str = "retarget"):
        if self.closed:
            return
        target = self.pages.get(page)
        if target is None or (generation != self._generation and kind not in target.ANY_GENERATION):
            return  # (for a mod picked before: its plan may have the key of one of this mod's)
        target.done(kind, tag, result)

    def _hold_registry(self) -> None:
        """Keep the game data types loaded until the window closes (a worker's; loaded once)."""
        with self._registry_lock:
            if self._registry or self.closed:
                return
            from .modedit import rszdata

            rszdata.registry()
            self._registry = True

    def _release_registry(self) -> None:
        with self._registry_lock:
            if not self._registry:
                return
            self._registry = False
            from .modedit import rszdata

            try:
                rszdata.release()
            except Exception as exc:
                log.debug("타입 정보를 놓지 못했습니다: %s", exc)

    # ---- result
    def _save_key(self) -> None:
        """Ctrl+S: 저장 on an edit page (not on 다른 장비로 옮기기: it makes a new mod)."""
        if self.page is None or self.page.key in NEW_TOOLS:
            self.bell()
        else:
            self.ok()

    def ok(self):
        page = self.page
        if page is None:
            self.bell()
            return
        if page.key in NEW_TOOLS:
            if self.pending.count() or not self._page_ready(page):
                self.bell()
                return
            typed = self._typed()  # (the new mod is made from the mod as it is: they would be gone with the window)
            if typed and not messagebox.askyesno(
                    MOD_TOOL_TITLE, "적용하지 않은 값이 있습니다:\n" + "\n".join(f"• {what}" for _page, what in typed) +
                    "\n\n새 모드에는 이 값이 들어가지 않습니다. 이 값은 버리고 새 모드를 만들까요?", parent=self):
                return
            self.result = page.result()
        else:
            if self.snapshot is None:
                self.bell()
                return
            if not self._apply_typed():
                return
            if not self.pending.count():
                self.bell()
                return
            self.result = EditChoice(self.pending.plan(self.mod_id, self.snapshot.stamp, self.snapshot.rev),
                                     self.page.key)
        self.destroy()

    def cancel(self):
        """취소, Escape, the window's close button: the unsaved changes are given up only when the user says so."""
        if self.closed:
            return
        if not self._discard_ok():
            return
        super().cancel()

    def destroy(self):
        self.closed = True
        for page in getattr(self, "pages", {}).values():
            try:
                page.close()
            except Exception:
                log.error("모드 수정 창을 닫다가 오류가 났습니다:\n%s", traceback.format_exc())
        self._release_registry()
        super().destroy()  # (Dialog drops its Tk variables)
        for page in getattr(self, "pages", {}).values():
            _drop_variables(page)

    def __getattr__(self, name: str):
        """What the window was before it had pages: the attributes of 다른 장비로 옮기기 (tree, parts_tree,
        source_box, off_var, ...) read through the window."""
        pages = self.__dict__.get("pages")
        page = pages.get("retarget") if pages and not name.startswith("__") else None
        if page is not None:
            try:
                return getattr(page, name)
            except AttributeError:
                pass
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
