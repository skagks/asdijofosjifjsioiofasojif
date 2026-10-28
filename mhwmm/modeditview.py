"""모드 수정 → 파일 고르기 · 게임 수치 · 스크립트 설정 · 수정 내용: the pages of the 모드 수정 window
(modtool.ModToolDialog) that change the mod itself.

Nothing a page does is saved at once: every action is an entry of the window's unsaved changes (shell.pending, a
modedit.model.Pending), shown at once on every page (● on what changed, the count beside 저장) and saved together
by 저장 (the app builds and deploys them: App._commit_edits).

- 파일 고르기 (FilesPage): leave files out of what the mod puts into the game or put them back (a texture's
  streaming copy follows it); for a path that several chosen options have, which option's copy is used (kept as
  the other copies left out). The table shows every stored file as the game would get it once saved.
- 게임 수치 / 스크립트 설정 (ValuesPage, tool "data" / "config"): the values of one file (a .user data file, a .user
  entry of the mod's PAK, a script's settings file) beside the mod's own and the game's, changed one at a time in
  the panel beside them (ValuePanel), many at once (data: 값 정하기, 게임 값의 몇 배, 모드가 바꾼 만큼의 몇 배) or as
  a rule (data: every row of a table with the same value).
- 수정 내용 (SummaryPage): every edit of the mod with its state, undone one by one or all at once, and what an
  update left to check (내 값 유지 / 작성자 값 쓰기).

The lists of files and their values are read on other threads from the snapshot the window took when the mod was
picked (manager.edit_targets, manager.open_values: shell.run); everything else works from that snapshot on this
thread. The window is frozen once shown (modtool): the panels have fixed widths and scroll when the page is not
tall enough for them, and every line whose text changes keeps its height."""

from __future__ import annotations

import logging
import tkinter as tk
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk

from .deploy import is_root_pak, path_key
from .gui import RowTooltip, Tooltip, autohide, follow_width, format_size
from .modedit import EditError, engines, model
from .modtool import Page, steady
from .requirements import module_key, provided_modules, required_modules

log = logging.getLogger(__name__)

SEARCH_DELAY = 150  # ms: a search runs once the typing stops
MAX_ROWS = 2000  # rows put into a table at most (the rest is found by searching)
PANEL = 280  # the panels beside the tables
CHANGED = "● "  # a change, saved or not yet
CONFLICT = "✕ "  # a value an update left to check
WARN = "⚠ "  # a setting the script may replace with one it saved
LOCKED = "수정 기록을 읽지 못해 지금은 고칠 수 없습니다. 수정 내용에서 '기록 지우기'를 한 뒤에 고치세요."
NOT_READ = "모드 수정 정보를 읽지 못했습니다 (자세한 내용은 로그)"

# 파일 고르기
VIEWS = ("모든 파일", "켠 옵션의 파일", "옵션끼리 겹치는 파일", "다른 모드에 덮어써지는 파일", "뺀 파일")
USE, LEFT_OUT = "☑", "☐"
AUTO = "자동 (아래쪽 옵션)"  # 쓸 옵션: the later chosen option's copy (as without 모드 수정)
ALL_OUT = "모든 파일을 빼면 이 모드는 게임에 아무것도 넣지 않습니다."
PART_OUT = "같은 이름의 파일 가운데 일부만 뺐습니다: {stem} ({inn}은 넣고 {out}은 뺌). 함께 빼야 할 수 있습니다."
REQUIRED = "다른 스크립트({name})가 이 파일을 불러옵니다."
OVERRIDDEN = "아래쪽 옵션 '{name}'의 파일이 이 파일을 덮어씁니다. 그 옵션의 파일을 빼면 이 파일이 들어갑니다."
OPTION_OFF = "이 옵션은 지금 꺼져 있어 빼도 게임에는 달라지는 것이 없습니다."
OTHER_MOD = "다른 모드 '{name}'이(가) 이 파일을 덮어씁니다."
TWIN_OUT, TWIN_IN = "스트리밍 사본도 함께 뺍니다", "스트리밍 사본도 함께 넣습니다"
TWIN_REVERT = "되돌리면 스트리밍 사본도 함께 넣습니다"  # (수정 내용: a texture or model left out with its copy)
NEEDS_OPTION = "켠 옵션이 없어 게임에 넣는 파일이 없습니다. 옵션 탭에서 옵션을 켜세요."
CAPPED = "결과가 많아 처음 {n:,}개만 보여 줍니다. 검색어를 더 적어 주세요."
CHECK_IN_SUMMARY = "수정 내용에서 확인하세요."

# 게임 수치 / 스크립트 설정
READING, READING_PAK = "읽는 중...", "PAK 안을 읽는 중..."
NO_TARGETS = {"data": "고칠 수 있는 데이터 파일이 없습니다.", "config": "고칠 수 있는 설정 파일이 없습니다."}
# the tool's button off once its files were read and none can be changed (a PAK without data files)
TOOL_EMPTY = {"data": "고칠 수 있는 데이터 파일(.user)이 없는 모드입니다", "config": "고칠 수 있는 설정 파일이 없는 모드입니다"}
FILTERS = {"data": "모드가 바꾼 값만", "config": "설정 같은 값만"}
MORE = "… 외 {n:,}개 (검색으로 좁혀 보세요)"
NOT_IN_GAME = "없음 (게임에 없는 행)"
UNPAIRED = "알 수 없음 (게임 파일에서 짝을 찾지 못한 값)"
GONE_GROUP = "업데이트 뒤 확인할 값"  # changes whose value the file does not have any more
PICK_VALUE = "값을 고르세요."
RULE_NOTE = "이 값은 '값이 {before}인 행 모두 → {after}' 규칙으로 바뀌었습니다. 원래 값으로 하면 규칙 전체를 되돌립니다."
CONFLICT_CHANGED = "모드가 업데이트되어 작성자의 값이 {before}에서 {now}(으)로 바뀌었습니다. 내 값: {after}"
CONFLICT_GONE = "업데이트로 이 값이 없어졌습니다 (내 값: {after})"
BULK_MODES = (("set", "값 정하기"), ("times", "게임 값의 몇 배"), ("change", "모드가 바꾼 만큼의 몇 배"))
BULK_EXAMPLE = "예: 3배 모드를 2배로 → '게임 값의 몇 배'에 2"
NOT_EDITABLE = "바꿀 수 없는 값"
# 게임 값의 몇 배 / 모드가 바꾼 만큼의 몇 배: a value whose game value is of a row paired with the game's by its place
# only (no key unique in both files): another row's when the mod put its rows in another order
UNKEYED = "게임과 행을 맞추지 못해 건너뛴 값"

# 수정 내용
EMPTY = "이 모드에 한 수정이 없습니다."
SHARED = "수정은 모든 프로필에 똑같이 쓰입니다. 옵션별 수정은 그 옵션을 켠 프로필에서만 게임에 들어갑니다."
KIND_LABELS = {"exclude": "뺌", "data": "수치", "config": "설정"}
STATES = {"active": "적용 중", "inactive_option": "쓰이지 않음 (옵션이 꺼져 있음)",
          "inactive_excluded": "쓰이지 않음 (파일을 뺌)", "waiting": "다시 적용 대기"}
UNSAVED, UNSAVED_REVERT, UNSAVED_REAPPLY = "● 저장 전", "● 저장 전 (되돌림)", "● 저장 전 (다시 적용)"
# what the unsaved changes do to the whole record (a line of its own under the top line)
UNSAVED_CLEAR = f"{UNSAVED}: 기록을 지웁니다 (모드의 원래 파일을 씁니다)"
UNSAVED_RESET = f"{UNSAVED}: 모든 수정을 되돌립니다"
REVERT, DELETE, REAPPLY = "이 수정 되돌리기", "지우기", "다시 적용"
KEEP, TAKE, KEEP_ALL, TAKE_ALL = "내 값 유지", "작성자 값 쓰기", "모두 내 값 유지", "모두 작성자 값 쓰기"


# ---- helpers (no Tk) -------------------------------------------------------------------------------------------
def option_label(source: model.Source) -> str:
    """The option of a stored file as the tables name it ("" without options; the 2nd of two options of one name
    counted)."""
    if not source.option:
        return ""
    return source.option if not source.n else f"{source.option} ({source.n + 1}번째)"


def file_states(snap: model.EditSnapshot, excluded) -> dict[str, tuple[str, str]]:
    """Source key -> (state, the option whose copy wins over it) of every stored file of ``snap`` as the game would
    get it with the files ``excluded(file)`` says are left out (the unsaved changes applied): "conflict" (an update
    left the edit to check: its file goes in as stored), "excluded", "needs_option" (no option chosen),
    "option_off", "overridden" (a later chosen option's copy of the path wins, as in mod_entries), "in"."""
    chosen = {o.id for o in snap.options if o.selected}
    names = {o.id: option_label(model.Source(o.name, o.n)) for o in snap.options}

    def out(f) -> bool:
        return f.state != "conflict" and excluded(f)

    winner: dict[str, model.EditFile] = {}
    for f in snap.files:  # (in the options' order: the later one wins)
        if (snap.options and f.option_id not in chosen) or out(f):
            continue
        winner[path_key(f.rel)] = f
    states = {}
    for f in snap.files:
        if f.state == "conflict":
            states[f.key] = ("conflict", "")
        elif out(f):
            states[f.key] = ("excluded", "")
        elif snap.options and not chosen:
            states[f.key] = ("needs_option", "")
        elif snap.options and f.option_id not in chosen:
            states[f.key] = ("option_off", "")
        else:
            top = winner.get(path_key(f.rel), f)
            states[f.key] = ("in", "") if top is f else ("overridden", names.get(top.option_id or "", ""))
    return states


def stem_of(rel: str) -> tuple[str, str, str]:
    """(folder, name before its first dot, first extension) of a game path:
    natives/x/ch03_004.mesh.2311 -> ("natives/x", "ch03_004", ".mesh")."""
    folder, _, name = rel.rpartition("/")
    stem, dot, rest = name.partition(".")
    return folder, stem, ("." + rest.split(".", 1)[0]) if dot else ""


def lua_requirers(scripts: list[tuple[str, str, Path]]) -> dict[str, list[str]]:
    """Source key -> the names of the mod's other scripts that ``require`` it (``scripts``: (source key, game path,
    stored file) of every .lua of the mod). Reads the scripts (worker thread)."""
    needs = {key: {module_key(n) for n in required_modules({rel: path})} for key, rel, path in scripts}
    out = {}
    for key, rel, _path in scripts:
        mine = provided_modules([rel])
        names = {other.rsplit("/", 1)[-1] for k, other, _p in scripts if k != key and needs[k] & mine}
        if names:
            out[key] = sorted(names, key=str.casefold)
    return out


def shown(kind: str, text: str | None) -> str:
    """A value as the pages show it (켬/끔 for a bool, "" for none)."""
    if text is None:
        return ""
    if kind.strip() in engines.BOOL_KINDS:
        return {"true": "켬", "false": "끔"}.get(text, text)
    return text


def type_text(value: engines.Value) -> str:
    """What kind of value it is, for the panel: "정수 (S32) · -2147483648 ~ 2147483647", "실수 (F32)", "켜기·끄기",
    "값 3개 (Vec3)", "색 (R, G, B, A: 0~255)", "글자"."""
    kind = value.kind.strip()
    if kind == "int":
        return "정수"
    if kind in engines.INT_RANGES:
        low, high = engines.INT_RANGES[kind]
        return f"정수 ({kind}) · {low} ~ {high}"
    if kind in engines.FLOAT_KINDS:
        return "실수" if kind == "float" else f"실수 ({kind})"
    if kind in engines.BOOL_KINDS:
        return "켜기·끄기"
    if kind == "Color":
        return "색 (R, G, B, A: 0~255)"
    if kind in engines.VECTOR_KINDS:
        count = value.spec[1] if len(value.spec) > 1 and isinstance(value.spec[1], int) else \
            len([x for x in value.text.split(",") if x.strip()])
        return f"값 {count}개 ({kind})"
    if kind in ("string", "String"):
        return "글자" if kind == "string" else "글자 (String)"
    return kind


def numeric(value: engines.Value) -> bool:
    return value.kind.strip() in engines.INT_RANGES or value.kind.strip() in engines.FLOAT_KINDS


def hint_values(hint: str) -> list[str]:
    """The values of a "게임에 쓰인 값: 0, 1, 2" hint."""
    _head, sep, rest = hint.partition(": ")
    return [x for x in rest.split(", ") if x] if sep else []


def hint_text(kind: str, hint: str) -> str:
    """The "게임에 쓰인 값" hint as the page shows values (켬/끔 for a bool)."""
    values = hint_values(hint)
    if not values or kind.strip() not in engines.BOOL_KINDS:
        return hint
    return "게임에 쓰인 값: " + ", ".join(shown(kind, x) for x in values)


def added_groups(view: model.ValueView) -> set[str]:
    """The groups (rows) of the mod's file the game's file does not have (from rszdata.compare). A value without
    a game value outside them was not paired: the row numbers rszdata does not compare, an array the game has at
    another length."""
    return set(view.added)


def skip_text(reason: str, n: int) -> str:
    """Values 한꺼번에 적용 skipped for one reason, for its result line: a reason that names the values ("게임 값이 없어
    건너뛴 값") with the count after it, a sentence (an engine's why: "켜기·끄기 값은 ...") in brackets after "건너뛴 값
    n개"."""
    if reason.endswith("값"):
        return f"{reason} {n:,}개"
    return f"건너뛴 값 {n:,}개 ({reason.rstrip('. ')})"


def _fit_table(tree: ttk.Treeview) -> None:
    """The card of a table (_EditPage.table) asks for the table's size as the page made it (its columns' widths,
    its rows: fit), whether its scroll bar shows or not and whatever the window's size made of the columns meanwhile
    (ttk stretches them): the bar takes its width from the table, and the page asks for the same width (the window
    is frozen: a wider page would take the width from the panel beside the table, or all of it once the window is
    made smaller again)."""
    card = tree.master
    edge = 2 * (int(card.cget("highlightthickness")) + int(card.cget("borderwidth")))
    card.configure(width=tree.winfo_reqwidth() + edge, height=tree.winfo_reqheight() + edge)
    card.pack_propagate(False)


@dataclass
class Prepared:
    """A file's values ready to show (worked out on the worker): the view, whether each row's mod value differs
    from the game's or is in a row the game lacks ("모드가 바꾼 값만"), whether the game lacks its row
    (``missing``: else a value without a game value was not paired) and the text a search looks in."""
    view: model.ValueView
    differs: list[bool]
    words: list[str]
    missing: list[bool] = None

    def __post_init__(self):
        if self.missing is None:
            self.missing = [False] * len(self.differs)


def prepare(view: model.ValueView, engine) -> Prepared:
    differs, words, missing = [], [], []
    added = added_groups(view) if view.compared else set()
    for row in view.rows:
        v = row.value
        if not view.compared:
            lacks, differ = False, False
        elif row.game is None:
            lacks = v.group in added
            differ = lacks  # (a row the game does not have; a value not paired is not shown as changed)
        else:
            lacks, differ = False, row.game != row.base and not engine.same(v, row.base, row.game)
        differs.append(differ)
        missing.append(lacks)
        words.append(f"{v.label}\n{v.at}\n{v.group}".casefold())
    return Prepared(view, differs, words, missing)


class _Scroll:
    """A column of fixed width (``width``) whose content (``inner``) scrolls when it is taller than the column: it
    asks for no height, so the window keeps its size whatever it shows."""

    def __init__(self, theme, parent, width: int):
        self.frame = ttk.Frame(parent, width=width)
        self.frame.pack_propagate(False)
        self.canvas = theme.mark(tk.Canvas(self.frame, width=1, height=1), "canvas_bg")
        bar = ttk.Scrollbar(self.frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=autohide(bar, side="right", fill="y"))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        follow_width(self.canvas, window)

    def wheel(self) -> None:
        """The mouse wheel over the content scrolls it (bound once everything is made)."""
        def scroll(event):
            if float(self.canvas.yview()[1]) < 1.0 or float(self.canvas.yview()[0]) > 0.0:
                up = getattr(event, "delta", 0) > 0 or getattr(event, "num", 0) == 4
                self.canvas.yview_scroll(-1 if up else 1, "units")

        def bind(widget):
            if widget.winfo_class() != "TCombobox":  # (its wheel is blocked: guard_box)
                for key in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                    widget.bind(key, scroll, add="+")
            for child in widget.winfo_children():
                bind(child)

        bind(self.canvas)

    def see(self, widget) -> None:
        """Scroll so that ``widget`` (in the content) can be seen."""
        try:
            self.inner.update_idletasks()
            top = widget.winfo_rooty() - self.inner.winfo_rooty()
            total = max(1, self.inner.winfo_height())
            first, last = (float(x) for x in self.canvas.yview())
            if top / total < first or (top + widget.winfo_height()) / total > last:
                self.canvas.yview_moveto(max(0.0, top / total - 0.05))
        except tk.TclError:
            pass


class _Stack:
    """Widgets packed one under the other in a fixed order, those with nothing to show left out (in a panel that
    scrolls: its height may change, the window's does not)."""

    def __init__(self):
        self.items: list[tuple[tk.Misc, dict]] = []

    def add(self, widget, **options):
        self.items.append((widget, options))
        widget.pack(**options)
        return widget

    def show(self, hidden=()) -> None:
        """Every widget again but those in ``hidden`` and the labels without text."""
        for widget, _options in self.items:
            widget.pack_forget()
        for widget, options in self.items:
            empty = widget.winfo_class() == "TLabel" and not str(widget.cget("text"))
            if widget not in hidden and not empty:
                widget.pack(**options)


class _EditPage(Page):
    """What the three pages share: the snapshot of the mod, the unsaved changes, delayed work (after) cancelled
    when the window closes, a search box, a table in a card and the table's keyboard."""

    def __init__(self, shell, frame):
        super().__init__(shell, frame)
        self.snap: model.EditSnapshot | None = None
        self.search_var: tk.StringVar | None = None
        self._afters: dict[str, str] = {}

    @property
    def pending(self) -> model.Pending:
        return self.shell.pending

    @property
    def mod_id(self) -> str:
        return self.shell.mod_id

    @property
    def generation(self) -> int:
        return getattr(self.shell, "_generation", 0)

    def plan(self) -> model.EditPlan:
        snap = self.snap
        return self.pending.plan(self.mod_id, snap.stamp if snap else "", snap.rev if snap else 0)

    def locked(self) -> str:
        """Why nothing can be changed now ("" when it can): the mod could not be read, or its record cannot be read
        and 기록 지우기 (or 모든 수정 되돌리기) is not among the unsaved changes."""
        if self.snap is None:
            return NOT_READ
        if not self.snap.record.error:
            return ""
        plan = self.plan()
        return "" if plan.reset or plan.clear_bad else LOCKED

    def later(self, name: str, ms: int, func) -> None:
        """``func`` in ``ms`` (a call of the same ``name`` waiting is dropped)."""
        old = self._afters.pop(name, None)
        if old is not None:
            self.frame.after_cancel(old)

        def run():
            self._afters.pop(name, None)
            if not self.closed:
                func()

        self._afters[name] = self.frame.after(ms, run)

    def close(self) -> None:
        for after in self._afters.values():
            try:
                self.frame.after_cancel(after)
            except tk.TclError:
                pass
        self._afters.clear()

    def changed(self) -> None:
        """The unsaved changes changed here: the other pages, the count and 저장 follow."""
        self.shell.pending_changed()

    # ---- the search box
    def search_box(self, parent, hint_text: str) -> ttk.Entry:
        """A search entry with its placeholder: the table follows SEARCH_DELAY ms after the typing stops
        (search_changed()); Return or Down go into the table (into_table()); Escape empties it first (an empty one
        lets Escape close the window)."""
        px = self.theme.px
        self.search_var = tk.StringVar(self.shell)
        search = self.search = ttk.Entry(parent, textvariable=self.search_var)
        hint = ttk.Label(parent, text=hint_text, style="FieldHint.TLabel", cursor="xterm")
        hint.bind("<Button-1>", lambda e: search.focus_set())

        def update(*_args):
            if self.closed or self.search_var is None:
                return
            try:
                focused = str(search.tk.call("focus")) == str(search)
            except tk.TclError:
                focused = False
            if self.search_var.get() or focused:
                hint.place_forget()
            else:
                hint.place(in_=search, x=px(10), rely=0.5, anchor="w")

        def escape(_event):
            if self.search_var.get():
                self.search_var.set("")
                return "break"  # (emptied first; another Escape closes the window)
            return None

        def into(_event):
            self.into_table()
            return "break"

        search.bind("<FocusIn>", update, add="+")
        search.bind("<FocusOut>", update, add="+")
        search.bind("<Escape>", escape)
        for key in ("<Return>", "<KP_Enter>", "<Down>"):
            search.bind(key, into)
        self.search_var.trace_add("write", update)
        self.search_var.trace_add("write", lambda *a: self.later("search", SEARCH_DELAY, self.search_changed))
        self.frame.after_idle(update)
        return search

    def query(self) -> str:
        return self.search_var.get().strip().casefold() if self.search_var is not None else ""

    def clear_search(self) -> None:
        """Empty the search box without searching again (the caller fills the table)."""
        if self.search_var is not None and self.search_var.get():
            self.search_var.set("")
            old = self._afters.pop("search", None)
            if old is not None:
                self.frame.after_cancel(old)

    def search_changed(self) -> None:
        pass

    def first_row(self) -> str | None:
        """The table's first row the keyboard goes to from the search box."""
        rows = self.tree.get_children()
        return rows[0] if rows else None

    def into_table(self) -> None:
        """From the search box into the table: its first row focused and selected."""
        tree = self.tree
        first = self.first_row()
        if first is None:
            self.frame.bell()
            return
        tree.focus_set()
        tree.focus(first)
        tree.selection_set(first)
        tree.see(first)

    # ---- tables
    def table(self, parent, columns, show: str, selectmode: str) -> ttk.Treeview:
        """A table in a card (its scroll bar only while there is something to scroll, its width taken from the
        table: the card asks for the size fit() gave the table, _fit_table); the first arrow key picks the row with
        the keyboard focus (Tk's arrows would move away from it)."""
        card = self.theme.mark(tk.Frame(parent), "card")
        card.pack(fill="both", expand=True)
        tree = ttk.Treeview(card, columns=columns, show=show, height=1, selectmode=selectmode)
        bar = ttk.Scrollbar(card, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=autohide(bar, side="right", fill="y", before=tree))  # (first: its width)
        tree.pack(side="left", fill="both", expand=True)
        for key in ("<Up>", "<Down>"):
            tree.bind(key, lambda e, t=tree: self._first_key(t))
        return tree

    @staticmethod
    def _first_key(tree: ttk.Treeview):
        focus = tree.focus()
        if focus and focus not in tree.selection():
            tree.selection_set(focus)
            tree.see(focus)
            return "break"
        return None  # (Tk moves the selection)

    def fit(self, rows: int) -> None:
        self.tree.configure(height=rows)
        _fit_table(self.tree)

    def first_focus(self):
        return self.tree


def _line(parent, wrap: int, lines: int = 1, **options) -> ttk.Label:
    """A label as tall as ``lines`` lines wrapped at ``wrap`` pixels whatever it says later (modtool.steady)."""
    return steady(parent, ["\n".join(["가"] * lines)], wrap, **options)


# ---- 파일 고르기 ----------------------------------------------------------------------------------------------
class FilesPage(_EditPage):
    """파일 고르기: every stored file of the mod (of every option), with 넣기 (☑ goes into the game, ☐ left out) and
    what the game gets of it once saved. Left out or put back with space, Return, a double click or a click on
    넣기, Delete or the panel's buttons (a texture's streaming copy in the same option follows it); 쓸 옵션 picks
    which chosen option's copy of a path the game gets (the others left out)."""

    key = "files"
    top_text = "보기"

    def __init__(self, shell, frame):
        super().__init__(shell, frame)
        t, px, f, p = self.theme, self.theme.px, self.theme.fonts, self.theme.p
        self.files: list[model.EditFile] = []
        self.by_key: dict[str, model.EditFile] = {}
        self.by_scope: dict[tuple, model.EditFile] = {}  # (option id, path key) -> file
        self.by_path: dict[str, list[model.EditFile]] = {}  # path key -> the chosen options' copies, in their order
        self.states: dict[str, tuple[str, str]] = {}  # key -> file_states() with the unsaved changes
        self.shown_keys: list[str] = []  # the table's rows (the files' keys, in order)
        self.capped = 0  # rows the table leaves out (MAX_ROWS)
        self.requirers: dict[str, list[str]] = {}  # .lua key -> the scripts that require it (lua_requirers)
        self._lua_loaded: int | None = None  # the window's _generation the scripts were read for
        self._copies: list[model.EditFile] = []  # 쓸 옵션: the chosen options' copies of the path shown
        self._group: list[model.EditFile] = []  # 함께 빼기: the same-name files still put in

        self.view_var = tk.StringVar(shell, value=VIEWS[0])
        self.view_box = ttk.Combobox(shell.top, textvariable=self.view_var, state="readonly", width=52, values=VIEWS)
        self.place_top(self.view_box)
        shell.guard_box(self.view_box)
        self.view_box.bind("<<ComboboxSelected>>", lambda e: self.fill(), add="+")

        columns = ttk.Frame(frame)
        columns.pack(fill="both", expand=True)
        left = ttk.Frame(columns)
        left.pack(side="left", fill="both", expand=True)
        self.search_box(left, "⌕  파일 이름 검색").pack(fill="x", pady=(0, px(6)), ipady=px(1))
        self.tree = self.table(left, ("use", "file", "option", "state"), "headings", "extended")
        widths = (("use", "넣기", 52, False), ("file", "파일", 220, True), ("option", "옵션", 110, False),
                  ("state", "상태", 170, False))
        for col, text, width, stretch in widths:
            self.tree.heading(col, text=text, anchor="center" if col == "use" else "w")
            self.tree.column(col, width=px(width), anchor="center" if col == "use" else "w", stretch=stretch)
        # (foregrounds: the earliest tag made wins, then the background of odd rows)
        self.tree.tag_configure("changed", foreground=p["accent"], font=f["bold"])
        self.tree.tag_configure("muted", foreground=p["muted"])
        self.tree.tag_configure("odd", background=p["row_alt"])
        RowTooltip(t, self.tree, lambda iid: self.by_key[iid].rel if iid in self.by_key else "")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_panel())
        self.tree.bind("<Button-1>", self._click)
        for key in ("<space>", "<Return>", "<KP_Enter>"):
            self.tree.bind(key, lambda e: (self.toggle(), "break")[1])
        self.tree.bind("<Double-Button-1>", self._double)
        self.tree.bind("<Delete>", lambda e: (self.set_out(self.selected(), True), "break")[1])
        wrap = px(sum(w for _c, _t, w, _s in widths))
        self.count = _line(left, wrap, 2, style="Muted.TLabel", font=f["small"])
        self.count.master.pack(fill="x", pady=(px(4), 0))

        # the panel: the file, 빼기 / 넣기, 쓸 옵션, what to know about it
        scroll = self.scroll = _Scroll(t, columns, px(PANEL))
        scroll.frame.pack(side="left", fill="y", padx=(px(16), 0))
        inner, wrap = scroll.inner, px(PANEL - 20)
        self.stack = stack = _Stack()

        def label(pady=0, **options):
            return stack.add(ttk.Label(inner, wraplength=wrap, justify="left", **options), anchor="w", pady=(pady, 0))

        self.p_name = label(font=f["bold"])
        self.p_folder = label(style="Muted.TLabel", font=f["small"])
        self.p_info = label(px(6))
        self.p_options = label(style="Muted.TLabel", font=f["small"])
        buttons = stack.add(ttk.Frame(inner), anchor="w", pady=(px(8), 0))
        self.out_button = ttk.Button(buttons, text="빼기", style="Compact.TButton",
                                     command=lambda: self.set_out(self.selected(), True))
        self.out_button.pack(side="left", padx=(0, px(8)))
        self.in_button = ttk.Button(buttons, text="넣기", style="Compact.TButton",
                                    command=lambda: self.set_out(self.selected(), False))
        self.in_button.pack(side="left")
        self.p_twin = label(px(4), style="Muted.TLabel", font=f["small"])
        self.use_row = stack.add(ttk.Frame(inner), anchor="w", fill="x", pady=(px(10), 0))  # (a path of 2+ options)
        ttk.Label(self.use_row, text="겹치는 파일: 쓸 옵션", style="DialogSection.TLabel").pack(anchor="w",
                                                                                         pady=(0, px(4)))
        self.use_var = tk.StringVar(shell)
        self.use_box = ttk.Combobox(self.use_row, textvariable=self.use_var, state="readonly", width=28)
        self.use_box.pack(anchor="w", fill="x")
        shell.guard_box(self.use_box)
        self.use_box.bind("<<ComboboxSelected>>", lambda e: self.use(self.use_box.current()), add="+")
        self.p_notes = label(px(10), style="EditWarn.TLabel")
        self.together_button = stack.add(ttk.Button(inner, text="함께 빼기", style="Compact.TButton",
                                                    command=self.together), anchor="w", pady=(px(4), 0))
        self.p_values = label(px(10), style="Muted.TLabel", font=f["small"])
        scroll.wheel()

    # ---- the shell
    def set_mod(self, snap):
        self.snap = snap
        self.files = list(snap.files) if snap is not None else []
        self.by_key = {f.key: f for f in self.files}
        self.by_scope = {(f.option_id, path_key(f.rel)): f for f in self.files}
        chosen = {o.id for o in snap.options if o.selected} if snap is not None else set()
        self.by_path = {}
        for f in self.files:
            if f.option_id and f.option_id in chosen:
                self.by_path.setdefault(path_key(f.rel), []).append(f)
        self.requirers = {}
        self._lua_loaded = None
        self.view_var.set(VIEWS[0])
        self.clear_search()
        self.fill()
        if self.shell.page is self:
            self._load_lua()

    def show(self):
        super().show()
        if self._lua_loaded != self.generation:
            self._load_lua()

    def refresh(self):
        self.mark()

    def done(self, kind, tag, result):
        if kind != "lua" or tag != self.generation:
            return
        if isinstance(result, Exception):
            log.info("모드의 스크립트를 살펴보지 못했습니다: %s", result)
            return
        self.requirers = result
        self.show_panel()

    def _load_lua(self):
        """Which of the mod's scripts require which (read on another thread, for the panel's note)."""
        self._lua_loaded = self.generation
        scripts = [(f.key, f.rel, f.path) for f in self.files if f.rel.lower().endswith(".lua")]
        if len(scripts) > 1:
            self.run("lua", lambda: lua_requirers(scripts), self.generation)

    # ---- the table
    def excluded(self, f: model.EditFile) -> bool:
        """Left out once saved (the record and the unsaved changes)."""
        return self.pending.excluded(f.source)

    def saved_out(self, f: model.EditFile) -> bool:
        record = self.snap.record if self.snap is not None else None
        item = record.item(f.source) if record is not None and not record.error else None
        return bool(item and item.exclude and not item.conflict)

    def copies(self, f: model.EditFile) -> list[model.EditFile]:
        """The chosen options' copies of ``f``'s path, in the options' order (the later one wins)."""
        copies = self.by_path.get(path_key(f.rel), [])
        return copies if f in copies else []

    def _wanted(self, f: model.EditFile, view: str, query: str) -> bool:
        if query and query not in f.rel.casefold():
            return False
        state = self.states.get(f.key, ("in", ""))[0]
        if view == VIEWS[1]:
            return state not in ("option_off", "needs_option")
        if view == VIEWS[2]:
            return len(self.copies(f)) >= 2
        if view == VIEWS[3]:
            return self.snap is not None and path_key(f.rel) in self.snap.overlaps
        if view == VIEWS[4]:
            return state == "excluded"
        return True

    def fill(self):
        """The table for the 보기 and the search (the selection and the keyboard's row kept when still shown)."""
        if self.closed:
            return
        self.states = file_states(self.snap, self.excluded) if self.snap is not None else {}
        tree = self.tree
        kept = [k for k in self.selected_keys()]
        focus = tree.focus()
        tree.delete(*tree.get_children())
        view, query = self.view_var.get(), self.query()
        wanted = [f for f in self.files if self._wanted(f, view, query)]
        self.capped = max(0, len(wanted) - MAX_ROWS)
        self.shown_keys = [f.key for f in wanted[:MAX_ROWS]]
        for i, key in enumerate(self.shown_keys):
            tree.insert("", "end", iid=key, values=self._values(self.by_key[key]), tags=self._tags(self.by_key[key], i))
        kept = [k for k in kept if tree.exists(k)]
        if kept:
            tree.selection_set(kept)
        if self.shown_keys:
            row = focus if focus and tree.exists(focus) else kept[0] if kept else self.shown_keys[0]
            tree.focus(row)
        self.show_panel()
        self.show_count()

    def search_changed(self):
        self.fill()

    def _values(self, f: model.EditFile) -> tuple:
        state, by = self.states.get(f.key, ("in", ""))
        if state == "conflict":
            item = self.snap.record.item(f.source) if self.snap is not None else None
            text = f"확인 필요: {item.conflict if item else ''}"
        elif state == "excluded":
            text = "뺌"
        elif state in ("option_off", "needs_option"):
            text = "옵션 꺼짐"
        elif state == "overridden":
            text = f"'{by}'의 파일에 덮어써짐"
        elif len(self.copies(f)) >= 2:
            text = f"'{option_label(f.source)}'의 것"
        else:
            text = "넣음"
        if self.excluded(f) != self.saved_out(f) and state != "conflict":
            text = CHANGED + text
        folder, _, name = f.rel.rpartition("/")
        return (LEFT_OUT if state == "excluded" else USE, f"{name} · {folder}" if folder else name,
                option_label(f.source), text)

    def _tags(self, f: model.EditFile, i: int) -> tuple:
        tags = []
        if self.excluded(f) != self.saved_out(f) and f.state != "conflict":
            tags.append("changed")
        if self.states.get(f.key, ("in", ""))[0] != "in":
            tags.append("muted")
        if i % 2:
            tags.append("odd")
        return tuple(tags)

    def mark(self):
        """The rows (and the panel and the count) again after the unsaved changes changed."""
        if self.closed:
            return
        self.states = file_states(self.snap, self.excluded) if self.snap is not None else {}
        for i, key in enumerate(self.shown_keys):
            f = self.by_key[key]
            self.tree.item(key, values=self._values(f), tags=self._tags(f, i))
        self.show_panel()
        self.show_count()

    def show_count(self):
        out = sum(1 for s, _by in self.states.values() if s == "excluded")
        text = f"파일 {len(self.files):,}개 · 뺀 파일 {out:,}개"
        shown = len(self.shown_keys) + self.capped
        if shown < len(self.files):
            text += f" · 보이는 파일 {shown:,}개"
        second = self.locked()
        snap = self.snap
        if not second and snap is not None and snap.options and not any(o.selected for o in snap.options):
            second = NEEDS_OPTION
        if not second and self.capped:
            second = CAPPED.format(n=MAX_ROWS)
        self.count.configure(text=f"{text}\n{second}" if second else text)

    # ---- choosing
    def selected_keys(self) -> list[str]:
        return [k for k in self.tree.selection() if k in self.by_key]

    def selected(self) -> list[model.EditFile]:
        return [self.by_key[k] for k in self.selected_keys()]

    def _click(self, event):
        """A click on a row's 넣기 box: that file left out or put back (and the row selected)."""
        tree = self.tree
        if tree.identify_region(event.x, event.y) != "cell" or tree.identify_column(event.x) != "#1":
            return None
        row = tree.identify_row(event.y)
        if not row or row not in self.by_key:
            return None
        tree.selection_set(row)
        tree.focus(row)
        f = self.by_key[row]
        self.set_out([f], not self.excluded(f))
        return "break"

    def _double(self, event):
        """A double click: the selected files left out or put back (on the 넣기 box its first click did)."""
        if self.tree.identify_column(event.x) != "#1":
            self.toggle()
        return "break"

    def toggle(self):
        """The selected files left out, or put back when all of them are left out."""
        files = self.selected()
        if not files:
            self.frame.bell()
            return
        self.set_out(files, not all(self.excluded(f) for f in files))

    def twin(self, f: model.EditFile) -> model.EditFile | None:
        """Its streaming copy (or the file a streaming copy is of) in the same option, when the mod has it."""
        return self.by_scope.get((f.option_id, path_key(f.twin))) if f.twin else None

    def set_out(self, files, flag: bool) -> None:
        """Leave ``files`` out (``flag``) or put them back, with their streaming copies (a file an update left to
        check is not changed: 수정 내용 settles it)."""
        why = self.locked()
        files = [f for f in files if f.state != "conflict"]
        if why or not files:
            self.frame.bell()
            if why:
                self.show_count()
            return
        self.apply_flags({f: flag for f in files})

    def apply_flags(self, flags: dict) -> None:
        for f, flag in flags.items():
            for g in (f, self.twin(f)):
                if g is not None and g.state != "conflict":
                    self.pending.exclude(g.source, flag)
        self.mark()
        self.changed()

    def use(self, index: int) -> None:
        """쓸 옵션 picked: that option's copy of the path goes in, the other chosen options' copies are left out
        (자동: none of them left out, the later option's copy wins)."""
        copies = self._copies
        if not 0 <= index <= len(copies) or len(copies) < 2:
            return
        if self.locked():
            self.frame.bell()
            self.show_panel()
            return
        pick = copies[index] if index < len(copies) else None
        self.apply_flags({c: pick is not None and c is not pick for c in copies})

    def together(self) -> None:
        """함께 빼기: the same-name files still put in are left out too."""
        if self._group:
            self.set_out(self._group, True)

    # ---- the panel
    def show_panel(self):
        if self.closed:
            return
        files = self.selected()
        why = self.locked()
        for label in (self.p_name, self.p_folder, self.p_info, self.p_options, self.p_twin, self.p_notes,
                      self.p_values):
            label.configure(text="")
        self._copies, self._group = [], []
        try:
            self._panel(files, why)
        finally:
            hidden = {self.use_row} if not self._copies else set()
            if not self._group:
                hidden.add(self.together_button)
            self.stack.show(hidden)

    def _panel(self, files: list[model.EditFile], why: str):
        if not files:
            for button in (self.out_button, self.in_button):
                button.state(["disabled"])
            self.p_name.configure(text="파일을 고르세요." if self.files else "")
            if self.files and self.all_out():
                self.p_notes.configure(text=ALL_OUT)
            return
        free = [f for f in files if f.state != "conflict"]
        self.out_button.state(["!disabled"] if not why and any(not self.excluded(f) for f in free) else ["disabled"])
        self.in_button.state(["!disabled"] if not why and any(self.excluded(f) for f in free) else ["disabled"])
        if len(files) > 1:
            self.p_name.configure(text=f"고른 파일 {len(files):,}개")
            out = sum(1 for f in files if self.excluded(f))
            self.p_info.configure(text=f"뺀 파일 {out:,}개 · 넣는 파일 {len(files) - out:,}개")
            notes = [why] if why else []
            if self.all_out():
                notes.append(ALL_OUT)
            self.p_notes.configure(text="\n".join(notes))
            return
        f = files[0]
        folder, _, name = f.rel.rpartition("/")
        self.p_name.configure(text=name)
        self.p_folder.configure(text=folder)
        self.p_info.configure(text=f"{f.kind} · {format_size(f.size)}")
        if self.snap is not None and self.snap.options:
            having = [option_label(g.source) for g in self.files if path_key(g.rel) == path_key(f.rel)]
            self.p_options.configure(text="이 파일이 든 옵션: " + ", ".join(having))
        twin = self.twin(f)
        if twin is not None:
            self.p_twin.configure(text=TWIN_IN if self.excluded(f) else TWIN_OUT)
        copies = self.copies(f)
        if len(copies) >= 2:
            self._copies = copies
            labels = [option_label(c.source) for c in copies] + [AUTO]
            self.use_box.configure(values=labels, state="disabled" if why else "readonly")
            out = [c for c in copies if self.excluded(c)]
            winner = next((c for c in reversed(copies) if not self.excluded(c)), None)
            self.use_box.current(len(copies) if not out or winner is None else copies.index(winner))
        notes = self.notes(f)
        if why:
            notes.insert(0, why)
        self.p_notes.configure(text="\n".join(notes))
        lines = []
        mp = [self.pending.member(f.source, name) for name in self._member_names(f)]
        values = sum(len(x.changes) for x in mp if x is not None)
        if values:
            lines.append(f"이 파일의 값 {values:,}개를 바꿨습니다")
        if f.repaired:
            lines.append("재질 복구본이 쓰이고 있습니다")
        self.p_values.configure(text="\n".join(lines))

    def _member_names(self, f: model.EditFile) -> list[str]:
        """The members (the file's values, or its PAK's entries') the record and the unsaved changes have."""
        names = set()
        record = self.snap.record if self.snap is not None else None
        item = record.item(f.source) if record is not None and not record.error else None
        if item is not None:
            names.update(m.member for m in item.members)
        names.update(member for (source, member) in self.plan().members if source == f.source)
        return sorted(names)

    def all_out(self) -> bool:
        """Every file that would go into the game is left out."""
        live = [s for s, _by in self.states.values() if s not in ("option_off", "needs_option", "conflict")]
        return bool(live) and all(s == "excluded" for s in live)

    def notes(self, f: model.EditFile) -> list[str]:
        """What to know about leaving ``f`` out (§3.1): never enforced."""
        state, by = self.states.get(f.key, ("in", ""))
        out = []
        if self.all_out():
            out.append(ALL_OUT)
        folder, stem, _ext = stem_of(f.rel)
        group = [g for g in self.files if g.option_id == f.option_id and g.state != "conflict"
                 and stem_of(g.rel)[:2] == (folder, stem)]
        if stem and len(group) > 1:
            inn = [g for g in group if not self.excluded(g)]
            gone = [g for g in group if self.excluded(g)]
            if inn and gone:
                def exts(files):
                    return ", ".join(dict.fromkeys(stem_of(g.rel)[2] or g.rel.rsplit("/", 1)[-1] for g in files))

                out.append(PART_OUT.format(stem=stem, inn=exts(inn), out=exts(gone)))
                self._group = inn
        if f.key in self.requirers:
            out.append(REQUIRED.format(name=", ".join(self.requirers[f.key])))
        if state == "overridden":
            out.append(OVERRIDDEN.format(name=by))
        elif state in ("option_off", "needs_option"):
            out.append(OPTION_OFF)
        elif state == "conflict":
            item = self.snap.record.item(f.source) if self.snap is not None else None
            out.append(f"확인 필요: {item.conflict if item else ''}. {CHECK_IN_SUMMARY}")
        other = self.snap.overlaps.get(path_key(f.rel)) if self.snap is not None else None
        if other:
            out.append(OTHER_MOD.format(name=other))
        return out


# ---- 게임 수치 / 스크립트 설정 ------------------------------------------------------------------------------------
class ValuePanel(ttk.Frame):
    """The panel beside a value table, ``PANEL`` pixels wide (it scrolls when the page is not tall enough): one
    value (its name, address and kind, the mod's and the game's value, an editor of its kind, the rule box, hints,
    why it cannot be changed, 적용 / 원래 값으로, a conflict an update left with its buttons) or, for several values
    or a group (게임 수치), the bulk editor; the cautions of 게임 수치 at the end. It shows what the page gives it
    and calls the page back (apply_value, revert_value, keep_mine, take_author, apply_bulk)."""

    def __init__(self, parent, page: "ValuesPage"):
        super().__init__(parent)
        self.page = page
        t, px, f = page.theme, page.theme.px, page.theme.fonts
        self.scroll = _Scroll(t, self, px(PANEL))
        self.scroll.frame.pack(fill="both", expand=True)
        inner, wrap = self.scroll.inner, px(PANEL - 20)
        self.value_var = tk.StringVar(self)
        self.check_var = tk.BooleanVar(self)
        self.rule_var = tk.BooleanVar(self)
        self.mode_var = tk.StringVar(self)
        self.bulk_var = tk.StringVar(self)
        self.text = ""  # what the editor shows for the value (Escape puts it back)
        self.rule_was = False  # the rule box as it was shown (ticked or unticked since: not applied yet)
        self.editable = False  # the value shown can be changed (its editor is on)
        self.bulk_typed = False  # 한꺼번에 적용's value or 방법 changed since it was last applied
        self.mode = ""  # what is shown: "" (nothing or a message), "one" (a value), "many" (the bulk editor)
        self.hidden: set = set()  # widgets of the shown part left out (the conflict's, the game's line, ...)

        def label(parent, **options):
            return ttk.Label(parent, wraplength=wrap, justify="left", **options)

        self.stack = top = _Stack()  # the parts: what is shown, then the cautions
        # nothing chosen (or a group of 스크립트 설정)
        self.none = top.add(ttk.Frame(inner), fill="x")
        self.none_label = label(self.none, style="Muted.TLabel")
        self.none_label.pack(anchor="w")

        # one value
        self.one = top.add(ttk.Frame(inner), fill="x")
        self.lines = lines = _Stack()
        self.name = lines.add(label(self.one, font=f["bold"]), anchor="w")
        self.address = lines.add(label(self.one, style="Muted.TLabel", font=f["small"]), anchor="w")
        self.kind = lines.add(label(self.one, style="Muted.TLabel", font=f["small"]), anchor="w", pady=(px(6), 0))
        self.base = lines.add(label(self.one), anchor="w", pady=(px(4), 0))
        self.game = lines.add(label(self.one), anchor="w")
        self.conflict = lines.add(ttk.Frame(self.one), fill="x", pady=(px(8), 0))  # (a value to check)
        self.conflict_text = label(self.conflict, style="EditWarn.TLabel")
        self.conflict_text.pack(anchor="w")
        row = ttk.Frame(self.conflict)
        row.pack(anchor="w", pady=(px(4), 0))
        self.keep_button = ttk.Button(row, text=KEEP, style="Compact.TButton", command=page.keep_mine)
        self.keep_button.pack(side="left", padx=(0, px(8)))
        self.take_button = ttk.Button(row, text=TAKE, style="Compact.TButton", command=page.take_author)
        self.take_button.pack(side="left")
        self.slot = lines.add(ttk.Frame(self.one), fill="x", pady=(px(8), 0))  # the editor of the value's kind
        self.entry = ttk.Entry(self.slot, textvariable=self.value_var)
        self.check = t.checkbutton(self.slot, "켬", self.check_var, command=page.apply_value)
        for key in ("<Return>", "<KP_Enter>"):
            self.entry.bind(key, lambda e: (page.apply_value(from_key=True), "break")[1])
        self.entry.bind("<Escape>", lambda e: (self._escape(self.entry), "break")[1])
        self.check.bind("<Escape>", lambda e: (page.tree.focus_set(), "break")[1])
        self.rule = lines.add(t.checkbutton(self.one, "", self.rule_var), anchor="w", pady=(px(4), 0))
        self.hint = lines.add(label(self.one, style="Muted.TLabel", font=f["small"]), anchor="w", pady=(px(4), 0))
        self.warn = lines.add(label(self.one, style="EditWarn.TLabel", font=f["small"]), anchor="w", pady=(px(2), 0))
        self.note = lines.add(label(self.one, style="Muted.TLabel", font=f["small"]), anchor="w", pady=(px(2), 0))
        self.error = lines.add(label(self.one, style="Danger.TLabel", font=f["small"]), anchor="w", pady=(px(4), 0))
        row = lines.add(ttk.Frame(self.one), anchor="w", pady=(px(8), 0))
        self.apply_button = ttk.Button(row, text="적용", style="Compact.TButton", command=page.apply_value)
        self.apply_button.pack(side="left", padx=(0, px(8)))
        self.revert_button = ttk.Button(row, text="원래 값으로", style="Compact.TButton", command=page.revert_value)
        self.revert_button.pack(side="left")

        # several values (게임 수치)
        self.many = top.add(ttk.Frame(inner), fill="x")
        self.bulk_lines = bulk = _Stack()
        self.many_title = bulk.add(label(self.many, font=f["bold"]), anchor="w")
        bulk.add(ttk.Label(self.many, text="방법", style="DialogSection.TLabel"), anchor="w", pady=(px(8), px(4)))
        self.mode_box = bulk.add(ttk.Combobox(self.many, textvariable=self.mode_var, state="readonly", width=26),
                                 anchor="w", fill="x")
        page.shell.guard_box(self.mode_box)
        row = bulk.add(ttk.Frame(self.many), fill="x", pady=(px(8), 0))
        self.bulk_entry = ttk.Entry(row, textvariable=self.bulk_var, width=12)
        self.bulk_entry.pack(side="left", fill="x", expand=True, padx=(0, px(8)))
        self.bulk_button = ttk.Button(row, text="한꺼번에 적용", style="Compact.TButton", command=page.apply_bulk)
        self.bulk_button.pack(side="left")
        for key in ("<Return>", "<KP_Enter>"):
            self.bulk_entry.bind(key, lambda e: (page.apply_bulk(), "break")[1])
        self.bulk_entry.bind("<Escape>", lambda e: (self._escape(self.bulk_entry), "break")[1])
        self.bulk_var.trace_add("write", lambda *a: setattr(self, "bulk_typed", True))
        self.mode_box.bind("<<ComboboxSelected>>", lambda e: setattr(self, "bulk_typed", True), add="+")
        bulk.add(label(self.many, text=BULK_EXAMPLE, style="Muted.TLabel", font=f["small"]), anchor="w",
                 pady=(px(4), 0))
        self.bulk_error = bulk.add(label(self.many, style="Danger.TLabel", font=f["small"]), anchor="w",
                                   pady=(px(4), 0))
        self.bulk_result = bulk.add(label(self.many, style="Muted.TLabel", font=f["small"]), anchor="w",
                                    pady=(px(4), 0))

        # the cautions of 게임 수치 (under what is shown)
        self.cautions = top.add(ttk.Frame(inner), fill="x", pady=(px(14), 0))
        self.caution_lines = cautions = _Stack()
        self.int_caution = cautions.add(label(self.cautions, style="Muted.TLabel", font=f["small"]), anchor="w")
        self.online_caution = cautions.add(label(self.cautions, style="Muted.TLabel", font=f["small"]), anchor="w",
                                           pady=(px(4), 0))
        self.nothing("")
        self.scroll.wheel()

    def destroy(self):
        super().destroy()
        for name in ("value_var", "check_var", "rule_var", "mode_var", "bulk_var"):
            setattr(self, name, None)  # (on this thread: see Dialog._release_variables)

    def _show(self, part, value=None) -> None:
        """Show ``part`` (none, one or many) and the cautions for ``value`` (one shown, or None)."""
        from .modedit import rszdata

        data = self.page.tool == "data"
        ints = data and value is not None and value.kind.strip() in rszdata.INTS
        self.int_caution.configure(text=rszdata.INT_CAUTION if ints else "")
        self.online_caution.configure(text=rszdata.ONLINE_CAUTION if data else "")
        self.caution_lines.show()
        self.lines.show(self.hidden)
        self.bulk_lines.show()
        others = {p for p in (self.none, self.one, self.many) if p is not part}
        if not data:
            others.add(self.cautions)
        self.stack.show(others)

    def nothing(self, text: str) -> None:
        if self.mode or self.none_label.cget("text") != text:
            self.scroll.canvas.yview_moveto(0)
        self.mode = ""
        self.none_label.configure(text=text)
        self._show(self.none)

    def set_error(self, text: str) -> None:
        """The error line of the value shown (Danger)."""
        self.error.configure(text=text)
        self.lines.show(self.hidden)

    def set_bulk(self, error: str = "", result: str = "") -> None:
        self.bulk_error.configure(text=error)
        self.bulk_result.configure(text=result)
        self.bulk_lines.show()

    def single(self, value, base: str, game: str, after: str, *, editable: bool, why: str, hint: str = "",
               warn: str = "", note: str = "", conflict=None, rule_text: str = "", rule_on: bool = False,
               revertable: bool = False, fresh: bool = True, keep: bool = False) -> None:
        """Show one value: ``game`` what the game's value line says ("" leaves it out), ``after`` what the value is
        once saved (the editor's text), ``rule_text`` the rule box's text ("" hides it), ``conflict`` a
        model.Conflict to settle. ``fresh`` (another value than before) empties the error line and scrolls the
        panel to its top; ``keep``: what was typed for the same value and not applied stays in the editor."""
        px = self.page.theme.px
        typed = keep and not fresh and self.mode == "one" and self.address.cget("text") == value.at and \
            (self.entered() != self.text or self.rule_on() != self.rule_was)
        if fresh:
            self.scroll.canvas.yview_moveto(0)
        self.mode = "one"
        self.hidden = set()
        self.name.configure(text=value.label or value.at)
        self.address.configure(text=value.at)
        self.kind.configure(text=type_text(value))
        self.base.configure(text=f"모드의 원래 값: {shown(value.kind, base) if base else '없음'}")
        self.game.configure(text=f"게임 값: {game}" if game else "")
        if conflict is not None:
            c = conflict.change
            if conflict.why == "changed":
                text = CONFLICT_CHANGED.format(before=shown(c.kind, c.before), now=shown(c.kind, conflict.now),
                                               after=shown(c.kind, c.after))
            elif conflict.why == "gone":
                text = CONFLICT_GONE.format(after=shown(c.kind, c.after))
            else:
                text = conflict.text()
            self.conflict_text.configure(text=text)
            self.keep_button.pack_forget()
            if conflict.why == "changed":
                self.keep_button.pack(side="left", padx=(0, px(8)), before=self.take_button)
            self.take_button.configure(text=TAKE if conflict.why == "changed" else DELETE)
        else:
            self.hidden.add(self.conflict)
        boolean = value.kind.strip() in engines.BOOL_KINDS
        self.entry.pack_forget()
        self.check.pack_forget()
        self.text, self.editable = after, editable
        if boolean:
            if not typed:
                self.check_var.set(after == "true")
            self.check.pack(anchor="w")
            self.check.state(["!disabled"] if editable else ["disabled"])
        else:
            if not typed:
                self.value_var.set(after)
            self.entry.pack(fill="x")
            self.entry.state(["!disabled"] if editable else ["disabled"])
        self.rule_was = bool(rule_text) and rule_on
        if rule_text:
            self.rule.configure(text="  " + rule_text)
            if not typed:
                self.rule_var.set(rule_on)
            self.rule.state(["!disabled"] if editable else ["disabled"])
        else:
            self.rule_var.set(False)
            self.hidden.add(self.rule)
        self.hint.configure(text=hint)
        self.warn.configure(text=warn)
        self.note.configure(text="\n".join(x for x in (note, why) if x))
        if fresh:
            self.error.configure(text="")
        self.apply_button.state(["!disabled"] if editable and not boolean else ["disabled"])
        self.revert_button.state(["!disabled"] if revertable else ["disabled"])
        self._show(self.one, value)

    def bulk(self, count: int, numbers: int, compared: bool, fresh: bool = True) -> None:
        """Show the bulk editor for ``count`` values (``numbers`` of them numbers); the modes that start from the
        game's value only when the file was compared with the game's."""
        if self.mode != "many" or fresh:
            self.scroll.canvas.yview_moveto(0)
            self.bulk_error.configure(text="")
            self.bulk_result.configure(text="")
        self.mode = "many"
        self.many_title.configure(text=f"고른 값 {count:,}개 (숫자 {numbers:,}개)")
        modes = [label for _mode, label in (BULK_MODES if compared else BULK_MODES[:1])]
        current = self.mode_var.get()
        self.mode_box.configure(values=modes)
        if current not in modes:
            self.mode_box.current(0)
        self._show(self.many)

    def bulk_mode(self) -> str:
        label = self.mode_var.get()
        return next((mode for mode, text in BULK_MODES if text == label), "set")

    def rule_on(self) -> bool:
        return bool(self.rule.winfo_manager()) and bool(self.rule_var.get())

    def entered(self) -> str:
        """The editor's value as typed (or true/false from the box)."""
        if self.check.winfo_manager():
            return "true" if self.check_var.get() else "false"
        return self.value_var.get()

    def focus_editor(self) -> bool:
        widget = self.check if self.check.winfo_manager() else self.entry
        if not widget.winfo_manager() or widget.instate(["disabled"]):
            return False
        widget.focus_set()
        if widget is self.entry:
            self.entry.select_range(0, "end")
            self.entry.icursor("end")
        self.scroll.see(widget)
        return True

    def focus_bulk(self) -> None:
        self.bulk_entry.focus_set()
        self.bulk_entry.select_range(0, "end")
        self.scroll.see(self.bulk_entry)

    def _escape(self, entry) -> None:
        """Escape in an editor: the text it had put back first, then back to the table."""
        if entry is self.entry and self.value_var.get() != self.text:
            self.value_var.set(self.text)
            self.set_error("")
        elif entry is self.bulk_entry and self.bulk_var.get():
            self.bulk_var.set("")
            self.set_bulk()
        else:
            self.page.tree.focus_set()


class ValuesPage(_EditPage):
    """게임 수치 (``tool`` "data") or 스크립트 설정 ("config"): the values of the file picked in 파일 (the mod's files
    that tool can change, of every option), in groups (a table's row, a settings section), with the game's value
    (data), the mod's and the one it gets once saved; changed in the panel beside them (ValuePanel)."""

    top_text = "파일"

    def __init__(self, shell, frame, tool: str = "data"):
        if tool not in FILTERS:
            raise ValueError(tool)
        self.tool = tool
        self.key = tool
        super().__init__(shell, frame)
        t, px, f, p = self.theme, self.theme.px, self.theme.fonts, self.theme.p
        data = tool == "data"
        self.targets: list[model.EditTarget] | Exception | None = None  # (None: being read)
        self.target: model.EditTarget | None = None
        self.views: dict[tuple, object] = {}  # (key, member) -> Prepared or the exception, while the window is open
        self.ready_view: Prepared | None = None  # the shown file's
        self.rows: list[model.ValueRow] = []  # its values (and stand-ins for changes whose value is gone)
        self.differs: list[bool] = []
        self.missing: list[bool] = []  # rows whose table row the game's file lacks
        self.words: list[str] = []
        self.extra: set[int] = set()  # rows that are stand-ins
        self.by_rule: dict[str, list[int]] = {}  # a table column's rule address -> its rows
        self.covered: dict[int, model.Change] = {}  # row -> the rule that changes it once saved
        self.have: set[str] = set()  # the addresses of the file's values
        self.iids: dict[str, int] = {}  # tree row -> index in rows
        self.groups: dict[str, list[int]] = {}  # group row -> the indexes of its rows the filter and search show
        self.shown_count = 0
        self._loaded: int | None = None  # the window's _generation the files were asked for
        self._index: tuple = ({}, [], {}, [])  # the member's changes once saved: singles, rules, conflicts, rule conflicts

        self.file_var = tk.StringVar(shell, value=READING)
        self.file_box = ttk.Combobox(shell.top, textvariable=self.file_var, state="disabled", width=52)
        self.place_top(self.file_box)
        shell.guard_box(self.file_box)
        self.file_box.bind("<<ComboboxSelected>>", lambda e: self.pick(self.file_box.current()), add="+")

        columns = ttk.Frame(frame)
        columns.pack(fill="both", expand=True)
        left = ttk.Frame(columns)
        left.pack(side="left", fill="both", expand=True)
        row = ttk.Frame(left)
        row.pack(fill="x", pady=(0, px(6)))
        self.search_box(row, "⌕  값 이름 검색").pack(side="left", fill="x", expand=True, ipady=px(1))
        self.filter_var = tk.BooleanVar(shell, value=False)
        self.filter_check = t.checkbutton(row, FILTERS[tool], self.filter_var, command=self.fill)
        self.filter_check.pack(side="left", padx=(px(12), 0))
        if data:
            cols = (("game", "게임", 80), ("mod", "모드", 80), ("new", "바꿀 값", 90))
            first = 240
        else:
            cols = (("mod", "모드의 값", 110), ("new", "바꿀 값", 110))
            first = 270
        wrap = px(first + sum(w for _c, _t, w in cols))
        self.banner = _line(left, wrap, 3, style="Muted.TLabel")
        self.banner.master.pack(fill="x", pady=(0, px(4)))
        self.tree = self.table(left, tuple(c for c, _t, _w in cols), "tree headings",
                               "extended" if data else "browse")
        self.tree.heading("#0", text="이름", anchor="w")
        self.tree.column("#0", width=px(first), anchor="w", stretch=True)
        for col, text, width in cols:
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=px(width), anchor="w", stretch=False)
        # (foregrounds: the earliest tag made wins; the background of odd rows last)
        self.tree.tag_configure("conflict", foreground=p["danger"])
        self.tree.tag_configure("changed", foreground=p["accent"], font=f["bold"])
        self.tree.tag_configure("readonly", foreground=p["muted"])
        self.tree.tag_configure("group", foreground=p["muted"], font=f["small_bold"])
        self.tree.tag_configure("more", foreground=p["muted"])
        self.tree.tag_configure("odd", background=p["row_alt"])
        RowTooltip(t, self.tree, self._tip)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_panel())
        for key in ("<Return>", "<KP_Enter>"):
            self.tree.bind(key, lambda e: (self._enter(toggle=True), "break")[1])
        for key in ("<F2>", "<Double-Button-1>"):
            self.tree.bind(key, lambda e: (self._enter(toggle=False), "break")[1])
        self.tree.bind("<space>", lambda e: (self.toggle_bool(), "break")[1])
        self.tree.bind("<Delete>", lambda e: (self.revert_value(), "break")[1])
        for key in ("<Control-a>", "<Control-A>"):
            self.tree.bind(key, lambda e: (self.select_all(), "break")[1])
        # the other notes, then the file's own (its option is off, a big PAK: target.note)
        self.notes = _line(left, wrap, 3, style="Muted.TLabel", font=f["small"])
        self.notes.master.pack(fill="x", pady=(px(4), 0))
        self.count = _line(left, wrap, 1, style="Muted.TLabel", font=f["small"])
        self.count.master.pack(fill="x")

        self.panel = ValuePanel(columns, self)
        self.panel.pack(side="left", fill="y", padx=(px(16), 0))

    # ---- the shell
    @property
    def engine(self):
        return engines.get(self.target.engine) if self.target is not None else None

    def set_mod(self, snap):
        self.snap = snap
        self.targets = None
        self.target = None
        self.views = {}
        self._loaded = None
        self._show_none(clear=True)
        self.clear_search()
        reason = self.applies(snap) if snap is not None else NOT_READ
        paks = snap is not None and self.tool == "data" and any(is_root_pak(f.rel) for f in snap.files)
        self.file_box.configure(values=(), state="disabled")
        self.file_var.set("" if reason else READING_PAK if paks else READING)
        self.banner.configure(text=reason)
        if not reason and self.shell.page is self:
            self._load()

    def show(self):
        super().show()
        if self._loaded != self.generation and self.snap is not None and not self.applies(self.snap):
            self._load()

    def refresh(self):
        self.mark(keep=True)  # (hidden: what was typed in its panel stays until it is applied or given up)

    def first_focus(self):
        return self.tree if self.tree.get_children() else self.file_box

    def unapplied(self) -> str:
        """The value typed in the panel and not applied (Return or 적용 not pressed; the rule box's tick changed
        too): "address = value", or 한꺼번에 적용's value: "방법 value (고른 값 n개)"."""
        panel = self.panel
        if self.closed or self.ready_view is None:
            return ""
        if panel.mode == "one":
            i = self.one()
            if i is None or i in self.extra or not panel.editable:
                return ""
            entered, rule = panel.entered(), panel.rule_on()
            if entered == panel.text and rule == panel.rule_was:
                return ""
            v = self.rows[i].value
            what = f"{v.at} = {shown(v.kind, entered.strip())}"
            return f"{what} ({panel.rule.cget('text').strip()})" if rule else what
        k = panel.bulk_var.get().strip()
        if panel.mode == "many" and panel.bulk_typed and k:
            return f"{panel.mode_var.get()} {k} (고른 값 {len(self.chosen()):,}개)"
        return ""

    def apply_unapplied(self) -> bool:
        """The value typed and not applied, applied (as 적용 or 한꺼번에 적용); when it cannot be, this page is shown
        with why under it and the keyboard in its box."""
        panel = self.panel
        mode = panel.mode
        if mode not in ("one", "many"):
            return True
        if (self.apply_value() if mode == "one" else self.apply_bulk()):
            return True
        self.shell.select(self.key, force=True)
        if mode == "one":
            panel.focus_editor()
        else:
            panel.focus_bulk()
        return False

    def first_row(self) -> str | None:
        """The first value shown (from the search box: not its group's row, which would choose the group)."""
        return next(iter(self.iids), None)

    def done(self, kind, tag, result):
        if kind == "targets":
            self._targets_ready(result)
        elif kind == "values":
            self.views[tag] = result
            if self.target is not None and tag == (self.target.key, self.target.member):
                self._show(result)

    # ---- 파일
    def _load(self):
        self._loaded = self.generation
        m, snap, tool = self.manager, self.snap, self.tool
        self.run("targets", lambda: m.edit_targets(snap, tool))

    def label(self, target: model.EditTarget) -> str:
        """A file in the 파일 box: its name and folder (or its PAK, type and number), option, (수정함) once saved."""
        edited = self.pending.member(target.source, target.member) is not None
        return target.label + (" (수정함)" if edited else "")

    def _targets_ready(self, result):
        if isinstance(result, Exception):
            self.targets = result
            self.file_var.set("")
            self.banner.configure(text=f"파일을 읽지 못했습니다: {result}")
            return
        self.targets = list(result)
        if not self.targets:
            self.file_var.set("")
            self.banner.configure(text=NO_TARGETS[self.tool])
            self.shell.tool_empty(self.key, TOOL_EMPTY[self.tool])  # (a PAK without data files: the next tool)
            return
        self.file_box.configure(values=[self.label(t) for t in self.targets], state="readonly")
        remembered = self._remembered()
        index = next((i for i, t in enumerate(self.targets) if (t.key, t.member) == remembered), None)
        if index is None:
            index = next((i for i, t in enumerate(self.targets) if t.edited), 0)
        self.pick(index)

    def _remembered(self):
        files = getattr(self.app, "_tool_file", None)
        entry = files.get(self.mod_id) if isinstance(files, dict) else None
        return entry.get(self.tool) if isinstance(entry, dict) else None

    def _remember(self, target: model.EditTarget):
        files = getattr(self.app, "_tool_file", None)
        if isinstance(files, dict):
            entry = files.get(self.mod_id)
            if not isinstance(entry, dict):
                entry = files[self.mod_id] = {}
            entry[self.tool] = (target.key, target.member)

    def pick(self, index: int):
        """The file at ``index`` of 파일: its values (read once while the window is open)."""
        if not isinstance(self.targets, list) or not 0 <= index < len(self.targets):
            return
        target = self.targets[index]
        self.file_box.current(index)
        if target == self.target and self.ready_view is not None:
            return
        self.target = target
        self._remember(target)
        ident = (target.key, target.member)
        if ident in self.views:
            self._show(self.views[ident])
            return
        self._show_none(clear=True)
        self.banner.configure(text=READING)
        self.notes.configure(text=target.note)
        m, snap = self.manager, self.snap

        def work():
            return prepare(m.open_values(snap, target), engines.get(target.engine))

        self.run("values", work, ident)

    # ---- the values
    def _show_none(self, clear: bool = False):
        self.ready_view = None
        self.rows, self.differs, self.missing, self.words, self.extra = [], [], [], [], set()
        self.iids, self.groups, self.by_rule, self.covered = {}, {}, {}, {}
        self.tree.delete(*self.tree.get_children())
        self.notes.configure(text="")
        self.count.configure(text="")
        if clear:
            self.banner.configure(text="")
        self.filter_check.state(["disabled"])
        self.panel.nothing("")
        self.panel.bulk_typed = False  # (another mod or file: what was typed was for the values of the one before)

    def _show(self, result):
        self.panel.bulk_typed = False
        if isinstance(result, Exception):
            self._show_none(clear=True)
            self.banner.configure(text=f"값을 읽지 못했습니다: {result}")
            self.notes.configure(text=self.target.note if self.target else "")
            return
        view = result.view
        self.ready_view = result
        notes = list(view.notes)
        if view.readonly:
            first, rest = view.readonly, notes
        else:
            first, rest = (notes[0], notes[1:]) if notes else ("", [])
        self.banner.configure(text=first)
        self.notes.configure(text="\n".join([*rest, view.target.note] if view.target.note else rest))
        if self.tool == "data":
            self.filter_var.set(view.compared)
            self.filter_check.state(["!disabled"] if view.compared else ["disabled"])
        else:
            self.filter_var.set(view.target.engine == "lua")
            self.filter_check.state(["!disabled"])
        self._index_member()
        self._rows(result)
        self.fill(keep=False)

    def _rows(self, result: Prepared):
        """The file's rows and, first, a stand-in for each change to check whose value the file does not have
        (gone, or a rule that matches no row)."""
        rows, differs, words = list(result.view.rows), list(result.differs), list(result.words)
        missing = list(result.missing)
        self.have = have = {r.value.at for r in rows}
        stand = []
        singles, rules, conflicts, rule_conflicts = self._index
        for x in [*conflicts.values(), *rule_conflicts]:
            c = x.change
            if c.rule or c.at not in have:
                value = engines.Value(c.at, c.label or c.at, GONE_GROUP, c.kind, "", editable=False, why="")
                stand.append(model.ValueRow(value, "", None, conflict=x))
        self.extra = set(range(len(stand)))
        self.rows = stand + rows
        self.differs = [True] * len(stand) + differs
        self.missing = [False] * len(stand) + missing
        self.words = [f"{r.value.label}\n{r.value.at}".casefold() for r in stand] + words
        self.by_rule = {}
        for i, row in enumerate(self.rows):
            if row.value.rule_at and i not in self.extra:
                self.by_rule.setdefault(row.value.rule_at, []).append(i)
        self._cover()

    def _index_member(self):
        """The shown file's changes once saved (the record and the unsaved changes), for its rows."""
        target = self.target
        mp = self.pending.member(target.source, target.member) if target is not None else None
        singles, rules, conflicts, rule_conflicts = {}, [], {}, []
        for c in (mp.changes if mp else ()):
            if c.rule:
                rules.append(c)
            else:
                singles[c.at] = c
        for x in (mp.conflicts if mp else ()):
            if x.change.rule:
                rule_conflicts.append(x)
            else:
                conflicts[x.change.at] = x
        self._index = (singles, rules, conflicts, rule_conflicts)
        self._cover()

    def _cover(self):
        """Which rows the rules change once saved (the first rule in the record's order wins a row)."""
        self.covered = {}
        engine = self.engine
        for c in self._index[1]:
            for i in self.by_rule.get(c.at, ()):
                row = self.rows[i]
                if i not in self.covered and row.value.editable and engine.same(row.value, row.base, c.before):
                    self.covered[i] = c

    def state(self, i: int):
        """(the row's own change, the rule that covers it, its conflict) once saved."""
        row = self.rows[i]
        singles, rules, conflicts, rule_conflicts = self._index
        if i in self.extra:
            x = row.conflict
            live = (rule_conflicts if x.change.rule else list(conflicts.values()))
            return None, None, next((y for y in live if y.ident == x.ident), None)
        v = row.value
        return singles.get(v.at), self.covered.get(i), conflicts.get(v.at)

    def after(self, i: int) -> str:
        """The value a row has once saved."""
        change, rule, _conflict = self.state(i)
        return change.after if change else rule.after if rule else self.rows[i].base

    def _wanted(self, i: int, only: bool, query: str) -> bool:
        if query and query not in self.words[i]:
            return False
        if not only or i in self.extra:
            return True
        change, rule, conflict = self.state(i)
        if change or rule or conflict:
            return True  # (what was changed stays in sight)
        if self.tool == "data":
            return self.differs[i]
        return self.rows[i].value.settings

    def fill(self, keep: bool = True):
        """The table for the filter and the search (rows in groups; MAX_ROWS at most); ``keep``: the selection and
        the keyboard's row kept when still shown."""
        if self.closed:
            return
        tree = self.tree
        selection = [self.iids.get(i) for i in tree.selection()] if keep else []
        focus = self.iids.get(tree.focus()) if keep else None
        top = tree.yview()[0] if keep else 0.0
        tree.delete(*tree.get_children())
        self.iids, self.groups = {}, {}
        if self.ready_view is None:
            return
        only, query = bool(self.filter_var.get()) and "disabled" not in self.filter_check.state(), self.query()
        wanted = [i for i in range(len(self.rows)) if self._wanted(i, only, query)]
        heads: dict[str, str] = {}
        shown = 0
        for i in wanted:
            group = self.rows[i].value.group
            if group and group not in heads:
                gid = f"g{len(heads)}"
                heads[group] = gid
                self.groups[gid] = []
                if shown < MAX_ROWS:
                    tree.insert("", "end", iid=gid, text=group, open=True, tags=("group",))
            if group:
                self.groups[heads[group]].append(i)
            if shown >= MAX_ROWS:
                continue
            iid = f"r{i}"
            self.iids[iid] = i
            parent = heads.get(group, "") if group else ""
            if parent and not tree.exists(parent):
                tree.insert("", "end", iid=parent, text=group, open=True, tags=("group",))
            text, values, tags = self._display(i, shown)
            tree.insert(parent, "end", iid=iid, text=text, values=values, tags=tags)
            shown += 1
        rest = len(wanted) - shown
        if rest > 0:
            tree.insert("", "end", iid="more", text=MORE.format(n=rest), tags=("more",))
        self.shown_count = len(wanted)
        back = {i: iid for iid, i in self.iids.items()}
        chosen = [back[i] for i in selection if i in back]
        if chosen:
            tree.selection_set(chosen)
        leaves = list(self.iids)
        if leaves:
            tree.focus(back.get(focus, chosen[0] if chosen else leaves[0]))
        if keep:
            tree.yview_moveto(top)
        self.show_count()
        self.show_panel(fresh=not keep)

    def _display(self, i: int, n: int) -> tuple:
        row = self.rows[i]
        v = row.value
        change, rule, conflict = self.state(i)
        new = ""
        if conflict is not None:
            new = CONFLICT + shown(v.kind, conflict.change.after)
        elif change is not None:
            new = CHANGED + shown(v.kind, change.after)
        elif rule is not None:
            new = CHANGED + shown(v.kind, rule.after)
        base = shown(v.kind, row.base)
        if self.tool == "data":
            game = shown(v.kind, row.game) if row.game is not None else "없음" if self.missing[i] else ""
            values = (game, base, new)
        else:
            values = (base, new)
        tags = ["conflict"] if conflict is not None else ["changed"] if change or rule else []
        if not v.editable:
            tags.append("readonly")
        if n % 2:
            tags.append("odd")
        return (WARN if v.warn else "") + (v.label or v.at), values, tuple(tags)

    def mark(self, keep: bool = False):
        """The rows, the panel, the count and the 파일 box again after the unsaved changes changed (``keep``: a
        value typed in the panel and not applied stays)."""
        if self.closed:
            return
        if isinstance(self.targets, list) and self.targets:
            index = self.file_box.current()
            self.file_box.configure(values=[self.label(t) for t in self.targets])
            if index >= 0:
                self.file_box.current(index)
        if self.ready_view is None:
            return
        before = {x.ident for i in self.extra for x in [self.rows[i].conflict]}
        self._index_member()
        singles, rules, conflicts, rule_conflicts = self._index
        now = {x.ident for x in [*conflicts.values(), *rule_conflicts] if x.change.rule or x.change.at not in self.have}
        if now - before:  # (a change to check came back: its stand-in row)
            self._rows(self.ready_view)
            self.fill()
            return
        for n, (iid, i) in enumerate(self.iids.items()):
            text, values, tags = self._display(i, n)
            self.tree.item(iid, text=text, values=values, tags=tags)
        self.show_count()
        self.show_panel(fresh=False, keep=keep)

    def show_count(self):
        view = self.ready_view.view if self.ready_view else None
        if view is None:
            self.count.configure(text="")
            return
        total = len(self.rows)
        text = f"값 {view.total:,}개"
        if self.tool == "data" and view.compared:
            text += f" · 모드가 바꾼 값 {sum(self.differs[i] for i in range(total) if i not in self.extra):,}개"
        elif self.tool == "config":
            changed = sum(1 for i in range(total) if i not in self.extra and any(self.state(i)))
            text += f" · 바꾼 값 {changed:,}개"
        if self.shown_count < total:
            text += f" · 보이는 값 {self.shown_count:,}개"
        self.count.configure(text=text)

    def search_changed(self):
        self.fill()

    def _tip(self, iid: str) -> str:
        i = self.iids.get(iid)
        if i is None:
            return ""
        v = self.rows[i].value
        _change, _rule, conflict = self.state(i)
        if conflict is not None:
            return conflict.text()
        return v.why or v.warn or ""

    # ---- choosing rows
    def chosen(self) -> list[int]:
        """The rows chosen: those selected and the rows (shown by the filter and the search) of a selected group."""
        out: list[int] = []
        for iid in self.tree.selection():
            if iid in self.iids:
                out.append(self.iids[iid])
            elif iid in self.groups:
                out.extend(self.groups[iid])
        return list(dict.fromkeys(out))

    def one(self) -> int | None:
        """The one value row selected (None for none, several or a group)."""
        sel = self.tree.selection()
        return self.iids.get(sel[0]) if len(sel) == 1 else None

    def select_all(self):
        leaves = list(self.iids)
        if leaves and self.tool == "data":
            self.tree.selection_set(leaves)

    def _enter(self, toggle: bool):
        """Return / Enter (``toggle``), F2 or a double click on the table: into the panel's editor; on a group row
        Return opens or closes it, F2 goes to 한꺼번에 적용's box (게임 수치)."""
        tree = self.tree
        row = tree.focus()
        if row and not tree.selection():  # (the keyboard's row, picked: nothing was)
            tree.selection_set(row)
            self.show_panel()
        sel = tree.selection()
        if len(sel) == 1 and sel[0] in self.groups:
            if toggle:
                tree.item(sel[0], open=not tree.item(sel[0], "open"))
            elif self.panel.mode == "many":
                self.panel.focus_bulk()
            else:  # (스크립트 설정: a section has no editor)
                self.frame.bell()
            return
        if self.panel.mode == "many":
            self.panel.focus_bulk()
        elif self.panel.mode != "one" or not self.panel.focus_editor():
            self.frame.bell()

    def show_panel(self, fresh: bool = True, keep: bool = False):
        if self.closed:
            return
        panel = self.panel
        if self.ready_view is None:
            panel.nothing("")
            return
        view = self.ready_view.view
        sel = self.tree.selection()
        groups = [iid for iid in sel if iid in self.groups]
        if self.tool == "data" and (len(sel) > 1 or groups):
            rows = self.chosen()
            panel.bulk(len(rows), sum(1 for i in rows if numeric(self.rows[i].value)), view.compared, fresh=fresh)
            return
        if groups:  # (스크립트 설정: a section)
            panel.nothing(f"{self.tree.item(groups[0], 'text')}: 값 {len(self.groups[groups[0]]):,}개")
            return
        i = self.one()
        if i is None:
            panel.nothing(view.readonly or PICK_VALUE)
            return
        row = self.rows[i]
        v = row.value
        change, rule, conflict = self.state(i)
        why = self.locked() or ("" if v.editable else v.why or NOT_EDITABLE)
        editable = v.editable and not self.locked() and i not in self.extra
        after = self.after(i)
        note = v.note
        if rule is not None and change is None:
            note = "\n".join(x for x in (RULE_NOTE.format(before=shown(v.kind, rule.before),
                                                          after=shown(v.kind, rule.after)), note) if x)
        warn = v.warn
        if self.tool == "data" and v.kind.strip() in engines.INT_RANGES and (change or rule):
            values = hint_values(row.hint)
            if values and not any(self.engine.same(v, after, h) for h in values):
                from .modedit import rszdata

                warn = "\n".join(x for x in (warn, rszdata.NOT_IN_HINT) if x)
        rule_text, rule_on = "", False
        if self.tool == "data" and v.rule_at and i not in self.extra:
            same = self.same_rows(i)
            if len(same) > 1:
                rule_text = f"이 표에서 값이 {shown(v.kind, row.base)}인 행 {len(same):,}개 모두"
                rule_on = rule is not None and change is None
        revertable = not self.locked() and (change is not None or rule is not None or conflict is not None)
        if i in self.extra:
            panel.single(v, "", "", conflict.change.after if conflict else "", editable=False, why="",
                         conflict=conflict, revertable=revertable, fresh=fresh)
            return
        panel.single(v, row.base, self.game_text(i), after, editable=editable, why=why,
                     hint=hint_text(v.kind, row.hint), warn=warn, note=note, conflict=conflict, rule_text=rule_text,
                     rule_on=rule_on, revertable=revertable, fresh=fresh, keep=keep)

    def game_text(self, i: int) -> str:
        """What the panel says of the game's value of row ``i`` (게임 수치 only): the value; 없음 when the file
        was not compared or the game lacks the row; a value of a row it has that was not paired."""
        row = self.rows[i]
        if self.tool != "data" or i in self.extra:
            return ""
        if row.game is not None:
            return shown(row.value.kind, row.game)
        compared = self.ready_view is not None and self.ready_view.view.compared
        return UNPAIRED if compared and not self.missing[i] else NOT_IN_GAME if compared else "없음"

    def same_rows(self, i: int) -> list[int]:
        """The rows of the file a rule from row ``i`` would change: editable, of its table's column, with its value."""
        row = self.rows[i]
        engine = self.engine
        return [j for j in self.by_rule.get(row.value.rule_at, ())
                if self.rows[j].value.editable and engine.same(self.rows[j].value, self.rows[j].base, row.base)]

    # ---- changing values
    def _member_label(self) -> str:
        target = self.target
        mp = self.pending.member(target.source, target.member)
        if mp is not None and mp.label:
            return mp.label
        if not target.member:
            return target.source.rel.rsplit("/", 1)[-1]
        suffix = f" (옵션 {target.source.option})"
        return target.label[:-len(suffix)] if target.source.option and target.label.endswith(suffix) else target.label

    def _set(self, i: int, after: str, *, rule: bool = False, how: dict | None = None) -> None:
        row, target = self.rows[i], self.target
        v = row.value
        self.pending.set_value(target.source, target.member, target.engine, self._member_label(),
                               at=v.rule_at if rule else v.at, kind=v.kind, before=row.base, after=after, how=how,
                               rule=rule, raw=v.raw if self.tool == "config" and not rule else "", name=v.label)

    def apply_value(self, from_key: bool = False) -> bool:
        """적용 (or Return in the editor, a click on the bool box): the value typed becomes an unsaved change (the
        rule's, when its box is ticked); a value that cannot be read says why under it (False)."""
        i = self.one()
        panel = self.panel
        if i is None or i in self.extra or panel.mode != "one":
            self.frame.bell()
            return False
        v = self.rows[i].value
        why = self.locked()
        if why or not v.editable:
            panel.set_error(why or v.why or NOT_EDITABLE)
            self.frame.bell()
            return False
        try:
            after = self.engine.parse(v, panel.entered())
        except EditError as exc:
            panel.set_error(str(exc))
            self.frame.bell()
            return False
        panel.set_error("")
        self._set(i, after, rule=panel.rule_on())
        self.mark()
        self.changed()
        if from_key:
            self.tree.focus_set()
        return True

    def toggle_bool(self):
        """space on a bool value: it is switched (an unsaved change)."""
        i = self.one()
        if i is None or i in self.extra:
            self.frame.bell()
            return
        v = self.rows[i].value
        if v.kind.strip() not in engines.BOOL_KINDS or not v.editable or self.locked():
            self.frame.bell()
            return
        self._set(i, self.engine.parse(v, "false" if self.after(i) == "true" else "true"))
        self.mark()
        self.changed()

    def revert_value(self):
        """원래 값으로 (or Delete) on the chosen rows: their changes go (a row a rule changed takes the whole rule
        back; a change to check is dropped)."""
        rows = self.chosen()
        target = self.target
        if not rows or target is None or self.locked():
            self.frame.bell()
            return
        done = False
        for i in rows:
            change, rule, conflict = self.state(i)
            if not (change or rule or conflict):
                continue
            row = self.rows[i]
            if i in self.extra:
                self.pending.take_author(target.source, target.member, conflict.change.at,
                                         conflict.change.before if conflict.change.rule else None)
            else:
                self.pending.revert_value(target.source, target.member, row.value.at, before=row.base)
            done = True
        if not done:
            self.frame.bell()
            return
        self.mark()
        self.changed()

    def _conflict(self):
        i = self.one()
        return None if i is None else self.state(i)[2]

    def keep_mine(self):
        """내 값 유지: my value on top of the author's new one."""
        x, target = self._conflict(), self.target
        if x is None or self.locked():
            self.frame.bell()
            return
        try:
            self.pending.keep_mine(target.source, target.member, x.change.at,
                                   x.change.before if x.change.rule else None)
        except EditError as exc:
            self.panel.set_error(str(exc))
            self.frame.bell()
            return
        self.mark()
        self.changed()

    def take_author(self):
        """작성자 값 쓰기 (지우기 on a value that is gone): the change to check goes."""
        x, target = self._conflict(), self.target
        if x is None or self.locked():
            self.frame.bell()
            return
        self.pending.take_author(target.source, target.member, x.change.at, x.change.before if x.change.rule else None)
        self.mark()
        self.changed()

    def apply_bulk(self) -> bool:
        """한꺼번에 적용: every chosen value set by the 방법 (engines.scaled; 게임 값의 몇 배 and 모드가 바꾼 만큼의 몇 배
        only from a game value of a row paired with the game's by a key: view.keyed); what was skipped and why,
        and the integers brought into their range, said under it. False when nothing was changed."""
        panel, view = self.panel, self.ready_view.view if self.ready_view else None
        rows = self.chosen()
        if view is None or not rows or panel.mode != "many":
            self.frame.bell()
            return False
        why = self.locked()
        mode, k = panel.bulk_mode(), panel.bulk_var.get().strip()
        if why or not k:
            panel.set_bulk(error=why or "값을 적어 주세요.")
            self.frame.bell()
            return False
        # the factor, or a value for numbers, read as engines.scaled reads it: a comma only between thousands (0,5 is
        # refused, not read as 5); a value for texts only is taken as typed
        if mode != "set" or ("," in k and any(numeric(self.rows[i].value) for i in rows)):
            try:
                factor = engines.plain_number(k)
            except EditError as exc:
                panel.set_bulk(error=str(exc))
                self.frame.bell()
                return False
        engine = self.engine
        done, clamped, skipped = 0, 0, Counter()
        for i in rows:
            row = self.rows[i]
            v = row.value
            if i in self.extra or not v.editable:
                skipped[NOT_EDITABLE] += 1
                continue
            text, note = engines.scaled(engine, v, mode, k, row.base, row.game)
            if text is None:
                skipped[note] += 1
                continue
            if mode != "set" and v.at not in view.keyed:  # (its game value may be another row's)
                skipped[UNKEYED] += 1
                continue
            if note:
                clamped += 1
            how = {"mode": mode, "k": k} if mode == "set" else {"mode": mode, "k": factor, "game": row.game}
            self._set(i, text, how=how)
            done += 1
        if not done:
            reason = skipped.most_common(1)[0][0] if skipped else ""
            panel.set_bulk(error=f"바꾼 값이 없습니다: {reason}" if reason else "바꾼 값이 없습니다")
            self.frame.bell()
            return False
        parts = [f"값 {done:,}개를 바꿨습니다"]
        parts += [skip_text(reason, n) for reason, n in skipped.most_common()]
        if clamped:
            parts.append(f"범위를 넘어 맞춘 값 {clamped:,}개")
        panel.set_bulk(result=" · ".join(parts))
        panel.bulk_typed = False
        self.mark()
        self.changed()
        return True


# ---- 수정 내용 ------------------------------------------------------------------------------------------------
@dataclass
class _Line:
    """A row of 수정 내용: a file left out or a file's (or PAK entry's) values (``item``), or one value of them
    (``change`` or ``conflict``); ``view`` the manager's line (None for one only in the unsaved changes),
    ``mark`` what the unsaved changes do to it."""
    kind: str  # exclude | data | config
    source: model.Source
    member: str = ""
    label: str = ""
    view: model.EditItemView | None = None
    change: model.Change | None = None
    conflict: model.Conflict | None = None
    changes: tuple = ()
    conflicts: tuple = ()
    mark: str = ""
    parent: str = ""  # the item's row (for a value)

    @property
    def ident(self) -> tuple:
        c = self.change or (self.conflict.change if self.conflict else None)
        return (self.kind, self.source, self.member, c.ident if c else None, self.conflict is not None)


class SummaryPage(_EditPage):
    """수정 내용: every edit of the mod (files left out, values of a file or PAK entry with a row per value) with its
    state and what the unsaved changes do to it; the panel's buttons undo one, keep my value or take the author's
    after an update, or build one again; under the table: undo everything, and 기록 지우기 for a record that cannot
    be read."""

    key = "summary"
    top_text = None

    def __init__(self, shell, frame):
        super().__init__(shell, frame)
        t, px, f, p = self.theme, self.theme.px, self.theme.fonts, self.theme.p
        self.items: list[model.EditItemView] = []  # the manager's lines (the record as saved)
        self.lines: dict[str, _Line] = {}
        self._noticed: int | None = None  # the window's _generation the update's report was dropped for
        self.notice = ""

        wrap = px(70 + 230 + 100 + 150)
        # the update's report or why the record cannot be read, then a line of its own for what the unsaved
        # changes do to the whole record (● 저장 전: ...): two lines of the first would leave none for it
        self.top_line = _line(frame, wrap, 2)
        self.top_line.master.pack(fill="x")
        Tooltip(t, self.top_line, lambda: self.top_line.cget("text") if self._cut(self.top_line) else "")
        self.pending_line = steady(frame, [UNSAVED_CLEAR, UNSAVED_RESET], wrap)
        self.pending_line.master.pack(fill="x", pady=(0, px(6)))
        columns = ttk.Frame(frame)
        columns.pack(fill="both", expand=True)
        left = ttk.Frame(columns)
        left.pack(side="left", fill="both", expand=True)
        self.tree = self.table(left, ("kind", "target", "option", "state"), "headings", "browse")
        for col, text, width, stretch in (("kind", "종류", 70, False), ("target", "대상", 230, True),
                                          ("option", "옵션", 100, False), ("state", "상태", 150, False)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=px(width), anchor="w", stretch=stretch)
        self.tree.tag_configure("conflict", foreground=p["danger"])
        self.tree.tag_configure("changed", foreground=p["accent"], font=f["bold"])
        self.tree.tag_configure("muted", foreground=p["muted"])
        self.tree.tag_configure("value", foreground=p["muted"], font=f["small"])
        self.tree.tag_configure("odd", background=p["row_alt"])
        RowTooltip(t, self.tree, self._tip)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_panel())
        self.tree.bind("<Delete>", lambda e: (self.revert(), "break")[1])
        for key in ("<Return>", "<KP_Enter>", "<space>"):
            self.tree.bind(key, lambda e: "break")  # (rows are always open; the window's Return is 저장)
        row = ttk.Frame(left)
        row.pack(fill="x", pady=(px(8), 0))
        self.reset_button = ttk.Button(row, text="모든 수정 되돌리기", style="Compact.TButton", command=self.reset)
        self.reset_button.pack(side="left", padx=(0, px(8)))
        self.clear_button = ttk.Button(row, text="기록 지우기", style="Compact.TButton", command=self.clear_bad)
        shared = _line(left, wrap, 2, style="Muted.TLabel", font=f["small"])
        shared.configure(text=SHARED)
        shared.master.pack(fill="x", pady=(px(6), 0))

        scroll = self.scroll = _Scroll(t, columns, px(PANEL))
        scroll.frame.pack(side="left", fill="y", padx=(px(16), 0))
        inner, wrap = scroll.inner, px(PANEL - 20)
        self.stack = stack = _Stack()

        def label(pady=0, **options):
            return stack.add(ttk.Label(inner, wraplength=wrap, justify="left", **options), anchor="w", pady=(pady, 0))

        self.p_title = label(font=f["bold"])
        self.p_sub = label(style="Muted.TLabel", font=f["small"])
        self.p_state = label(px(6))
        self.p_detail = label(px(4), style="EditWarn.TLabel")
        self.p_more = label(px(4), style="Muted.TLabel", font=f["small"])
        buttons = stack.add(ttk.Frame(inner), anchor="w", pady=(px(10), 0))
        self.revert_button = ttk.Button(buttons, text=REVERT, style="Compact.TButton", command=self.revert)
        self.revert_button.grid(row=0, column=0, sticky="w", padx=(0, px(8)), pady=(0, px(6)))
        self.reapply_button = ttk.Button(buttons, text=REAPPLY, style="Compact.TButton", command=self.reapply)
        self.reapply_button.grid(row=0, column=1, sticky="w", pady=(0, px(6)))
        self.keep_button = ttk.Button(buttons, text=KEEP, style="Compact.TButton", command=self.keep_mine)
        self.keep_button.grid(row=1, column=0, sticky="w", padx=(0, px(8)))
        self.take_button = ttk.Button(buttons, text=TAKE, style="Compact.TButton", command=self.take_author)
        self.take_button.grid(row=1, column=1, sticky="w")
        scroll.wheel()

    # ---- the shell
    def set_mod(self, snap):
        self.snap = snap
        self.notice = ""
        self.items = []
        if snap is not None:
            try:
                self.items = list(self.manager.edit_items(snap.mod_id))
            except Exception:
                log.exception("모드 수정 내용을 읽지 못했습니다")
            notice = snap.record.notice if not snap.record.error else {}
            if notice:
                self.notice = "업데이트로 " + model.CarryReport.from_notice(notice).text()
        self.fill()
        if self.shell.page is self:
            self._drop_notice()

    def show(self):
        super().show()
        self._drop_notice()

    def refresh(self):
        self.fill()

    def _drop_notice(self):
        """The update's report is shown once: forgotten by the record when the page is shown (it stays in sight
        while the window is open)."""
        snap = self.snap
        if snap is None or not self.notice or self._noticed == self.generation:
            return
        self._noticed = self.generation
        try:
            self.manager.drop_edit_notice(snap.mod_id)
        except Exception as exc:
            log.warning("모드 수정의 업데이트 알림을 지우지 못했습니다: %s", exc)

    # ---- the table
    def build(self) -> list[_Line]:
        """The items (the record as saved, then what only the unsaved changes have), each with what the unsaved
        changes do to it."""
        out: list[_Line] = []
        plan = self.plan()
        pending = self.pending
        seen_out, seen_members = set(), set()
        for v in self.items:
            if v.kind == "exclude":
                seen_out.add(v.source)
                mark = ""
                if v.state == "item_conflict":
                    mark = UNSAVED_REVERT if plan.reset or v.source in plan.drop else ""
                elif not pending.excluded(v.source):
                    mark = UNSAVED_REVERT
                out.append(_Line("exclude", v.source, "", v.source.rel, v, mark=mark))
                continue
            seen_members.add((v.source, v.member))
            mp = pending.member(v.source, v.member)
            changes, conflicts, mark = v.changes, v.conflicts, ""
            if mp is None:
                mark = UNSAVED_REVERT
            elif (tuple(mp.changes), tuple(mp.conflicts)) != (tuple(v.changes), tuple(v.conflicts)):
                changes, conflicts, mark = tuple(mp.changes), tuple(mp.conflicts), UNSAVED
            if v.source in plan.reapply and mark != UNSAVED_REVERT:
                mark = UNSAVED_REAPPLY
            out.append(_Line(v.kind, v.source, v.member, v.label, v, changes=changes, conflicts=conflicts, mark=mark))
        for source, flag in plan.exclude.items():
            if flag and source not in seen_out:
                out.append(_Line("exclude", source, "", source.rel, mark=UNSAVED))
        for (source, member), mp in plan.members.items():
            if (source, member) in seen_members or not (mp.changes or mp.conflicts):
                continue
            name = source.rel.rsplit("/", 1)[-1]
            out.append(_Line(model.engine_tool(mp.engine) or "data", source, member,
                             mp.label or (f"{name} {member}" if member else name), changes=tuple(mp.changes),
                             conflicts=tuple(mp.conflicts), mark=UNSAVED))
        return out

    def state_text(self, line: _Line) -> str:
        if line.mark:
            return line.mark
        v = line.view
        if v is None:
            return ""
        if v.state == "conflict":
            return f"확인할 값 {len(line.conflicts):,}개"
        if v.state == "pending":
            return f"다시 적용하지 못함: {v.why}"
        if v.state == "item_conflict":
            return f"확인 필요: {v.why}"
        return STATES.get(v.state, v.state)

    def fill(self):
        if self.closed:
            return
        tree = self.tree
        picked = self.selected()
        old = picked.ident if picked is not None else None
        tree.delete(*tree.get_children())
        self.lines = {}
        lines = self.build() if self.snap is not None else []
        again = None
        for n, line in enumerate(lines):
            iid = f"i{n}"
            self.lines[iid] = line
            tags = ["changed"] if line.mark else ["conflict"] if line.conflicts or (
                line.view is not None and line.view.state in ("pending", "item_conflict")) else []
            if line.view is not None and line.view.state.startswith("inactive"):
                tags.append("muted")
            if n % 2:
                tags.append("odd")
            tree.insert("", "end", iid=iid, open=True, tags=tuple(tags),
                        values=(KIND_LABELS.get(line.kind, line.kind), line.label, option_label(line.source),
                                self.state_text(line)))
            if line.ident == old:
                again = iid
            for k, c in enumerate(line.changes):
                cid = f"{iid}.c{k}"
                self.lines[cid] = _Line(line.kind, line.source, line.member, self.name(c), change=c, parent=iid,
                                        mark=line.mark)
                tree.insert(iid, "end", iid=cid, tags=("value",), values=("", self.change_text(c), "", ""))
                if self.lines[cid].ident == old:
                    again = cid
            for k, x in enumerate(line.conflicts):
                cid = f"{iid}.x{k}"
                self.lines[cid] = _Line(line.kind, line.source, line.member, self.name(x.change),
                                        conflict=x, parent=iid, mark=line.mark)
                tree.insert(iid, "end", iid=cid, tags=("conflict",),
                            values=("", self.conflict_text(x), "", "확인 필요"))
                if self.lines[cid].ident == old:
                    again = cid
        if again is not None:
            tree.selection_set(again)
            tree.focus(again)
            tree.see(again)
        elif self.lines:
            tree.focus(next(iter(self.lines)))
        self.show_top(lines)
        self.show_panel()

    @staticmethod
    def name(c: model.Change) -> str:
        """What a change is to, in the panel and the row's tooltip: a rule by its column's name, one value by its
        address (the rows of a table have the same names: "_Values[_ItemId=101]._SellPrice")."""
        return c.label or c.at if c.rule else c.at

    def _tip(self, iid: str) -> str:
        line = self.lines.get(iid)
        if line is None:
            return ""
        c = line.change or (line.conflict.change if line.conflict else None)
        return self.name(c) if c is not None else line.source.label()

    @staticmethod
    def conflict_text(x: model.Conflict) -> str:
        """"✕ _BuyPrice  1 → 50 (내 값 100)": a value to check, short (the panel says it in full)."""
        c = x.change
        if x.why == "changed":
            text = f"{shown(c.kind, c.before)} → {shown(c.kind, x.now)} (내 값 {shown(c.kind, c.after)})"
        else:
            text = x.text()
        return f"{CONFLICT}{c.label or c.at}  {text}"

    @staticmethod
    def change_text(c: model.Change) -> str:
        """"_SellPrice  99999 → 5000 (행 412개)" (the panel's title and the row's tooltip say which value)."""
        text = f"{c.label or c.at}  {shown(c.kind, c.before)} → {shown(c.kind, c.after)}"
        if c.rule:
            text += f" (행 {c.count:,}개)" if c.count else " (같은 값인 행 모두)"
        return text

    @staticmethod
    def _cut(label: ttk.Label) -> bool:
        """A steady label's text is taller than its lines (its whole text as its tooltip)."""
        try:
            return label.winfo_reqheight() > label.master.winfo_height() > 1
        except tk.TclError:
            return False

    def show_top(self, lines: list[_Line]):
        snap = self.snap
        plan = self.plan()
        parts, unsaved = [], ""
        if snap is None:
            parts.append(NOT_READ)
        elif snap.record.error:
            parts.append(snap.issues[0] if snap.issues else f"모드 수정 기록을 읽지 못해 쓰지 않고 있습니다: "
                                                             f"{snap.record.error}")
            if plan.clear_bad or plan.reset:
                unsaved = UNSAVED_CLEAR
        else:
            if self.notice:
                parts.append(self.notice)
            if plan.reset:
                unsaved = UNSAVED_RESET
            elif not lines:
                parts.append(EMPTY)
        self.top_line.configure(text="\n".join(parts))
        self.pending_line.configure(text=unsaved)
        error = snap is not None and bool(snap.record.error)
        if error:
            self.clear_button.pack(side="left")
        else:
            self.clear_button.pack_forget()
        self.clear_button.state(["!disabled"] if error and not (plan.clear_bad or plan.reset) else ["disabled"])
        anything = snap is not None and (error or any(not ln.mark.startswith(UNSAVED_REVERT) for ln in lines))
        self.reset_button.state(["!disabled"] if anything and not plan.reset else ["disabled"])

    # ---- the panel
    def selected(self) -> _Line | None:
        sel = self.tree.selection()
        return self.lines.get(sel[0]) if sel else None

    def show_panel(self):
        if self.closed:
            return
        line = self.selected()
        buttons = (self.revert_button, self.reapply_button, self.keep_button, self.take_button)
        for button in buttons:
            button.state(["disabled"])
        self.revert_button.configure(text=REVERT)
        self.keep_button.configure(text=KEEP)
        self.take_button.configure(text=TAKE)
        for label in (self.p_title, self.p_sub, self.p_state, self.p_detail, self.p_more):
            label.configure(text="")
        try:
            self._panel(line)
        finally:
            self.stack.show()

    def _panel(self, line: _Line | None):
        if line is None:
            self.p_title.configure(text="수정을 고르세요." if self.lines else "")
            return
        why = self.locked()
        item = self.lines.get(line.parent) if line.parent else line
        reverted = item.mark == UNSAVED_REVERT
        self.p_title.configure(text=line.label)
        where = KIND_LABELS.get(line.kind, line.kind)
        self.p_sub.configure(text=f"{where} · {line.source.label()}" + (f" {line.member}" if line.member else ""))
        self.p_state.configure(text=self.state_text(item))
        detail, more = "", ""
        view = item.view
        if line.conflict is not None:
            detail = line.conflict.text()
            changed = line.conflict.why == "changed"
            self.keep_button.state(["!disabled"] if changed and not why and not reverted else ["disabled"])
            self.take_button.configure(text=TAKE if changed else DELETE)
            self.take_button.state(["!disabled"] if not why and not reverted else ["disabled"])
            self.revert_button.state(["!disabled"] if not why and not reverted else ["disabled"])
        elif line.change is not None:
            c = line.change
            detail = self.change_text(c)
            if c.how:
                more = self.how_text(c)
            self.revert_button.state(["!disabled"] if not why and not reverted else ["disabled"])
        else:
            if view is not None and view.state == "item_conflict":
                detail = view.why
                more = "지우기를 누르면 이 수정을 기록에서 지웁니다."
                self.revert_button.configure(text=DELETE)
            elif view is not None and view.state == "pending":
                detail = view.why
            elif line.kind == "exclude":
                more = "게임에 넣지 않는 파일입니다."
                if not reverted and self._twin_out(line.source) is not None:
                    more += "\n" + TWIN_REVERT
            self.revert_button.state(["!disabled"] if not why and not reverted else ["disabled"])
            waiting = view is not None and view.state in ("pending", "waiting")
            self.reapply_button.state(["!disabled"] if waiting and not why and not reverted
                                      and item.mark != UNSAVED_REAPPLY else ["disabled"])
            if line.conflicts:
                self.keep_button.configure(text=KEEP_ALL)
                self.take_button.configure(text=TAKE_ALL)
                if any(x.why == "changed" for x in line.conflicts) and not why and not reverted:
                    self.keep_button.state(["!disabled"])
                if not why and not reverted:
                    self.take_button.state(["!disabled"])
        if why:
            more = "\n".join(x for x in (why, more) if x)
        self.p_detail.configure(text=detail)
        self.p_more.configure(text=more)

    @staticmethod
    def how_text(c: model.Change) -> str:
        how = c.how or {}
        mode, k, game = how.get("mode"), how.get("k"), how.get("game")
        if mode == "times":
            return f"게임 값({game})의 {k}배로 정한 값"
        if mode == "change":
            return f"게임 값({game})에서 모드가 바꾼 만큼의 {k}배로 정한 값"
        if mode == "set":
            return "한꺼번에 정한 값"
        return ""

    def _twin_out(self, source: model.Source) -> model.EditFile | None:
        """The streaming copy of a stored file left out (or the file a streaming copy is of), in the same option,
        when it is left out too: put back with it (as on 파일 고르기)."""
        snap = self.snap
        f = next((g for g in snap.files if g.source == source), None) if snap is not None else None
        if f is None or not f.twin:
            return None
        twin = next((g for g in snap.files if g.option_id == f.option_id and path_key(g.rel) == path_key(f.twin)),
                    None)
        return twin if twin is not None and twin.state != "conflict" and self.pending.excluded(twin.source) else None

    # ---- actions (each an unsaved change)
    def _done(self):
        self.fill()
        self.changed()

    def _refuse(self) -> bool:
        if self.locked():
            self.frame.bell()
            return True
        return False

    def revert(self):
        """이 수정 되돌리기 (지우기 on an item an update left without its option or file): the file put back (with
        its streaming copy, left out with it on 파일 고르기), a file's values or one value undone."""
        line = self.selected()
        if line is None:
            self.frame.bell()
            return
        if self._refuse():
            return
        item = self.lines.get(line.parent) if line.parent else line
        if item.mark == UNSAVED_REVERT:
            self.frame.bell()
            return
        pending = self.pending
        if line.change is not None or line.conflict is not None:
            c = line.change or line.conflict.change
            pending.revert_value(line.source, line.member, c.at, before=c.before if c.rule else None)
        elif item.view is not None and item.view.state == "item_conflict":
            pending.drop(line.source)
        elif line.kind == "exclude":
            twin = self._twin_out(line.source)
            pending.exclude(line.source, False)
            if twin is not None:  # (a texture and its streaming copy go in together)
                pending.exclude(twin.source, False)
        else:
            record = self.snap.record
            saved = record.item(line.source) if not record.error else None
            others = saved is not None and (saved.exclude or any(m.member != line.member for m in saved.members))
            if saved is not None and not others:
                pending.drop(line.source)
            else:  # (the file's other edits stay: this member's values one by one)
                for c in line.changes:
                    pending.revert_value(line.source, line.member, c.at, before=c.before if c.rule else None)
                for x in line.conflicts:
                    pending.take_author(line.source, line.member, x.change.at,
                                        x.change.before if x.change.rule else None)
        self._done()

    def reapply(self):
        line = self.selected()
        if line is None or self._refuse():
            return
        self.pending.reapply(line.source)
        self._done()

    def _conflicts(self, line: _Line) -> list[model.Conflict]:
        return [line.conflict] if line.conflict is not None else list(line.conflicts)

    def keep_mine(self):
        """내 값 유지 (모두: every value of the file the author changed)."""
        line = self.selected()
        if line is None or self._refuse():
            return
        for x in self._conflicts(line):
            if x.why == "changed":
                self.pending.keep_mine(line.source, line.member, x.change.at,
                                       x.change.before if x.change.rule else None)
        self._done()

    def take_author(self):
        """작성자 값 쓰기 (모두: every value of the file to check; 지우기 on one that is gone)."""
        line = self.selected()
        if line is None or self._refuse():
            return
        for x in self._conflicts(line):
            self.pending.take_author(line.source, line.member, x.change.at,
                                     x.change.before if x.change.rule else None)
        self._done()

    def reset(self):
        """모든 수정 되돌리기."""
        if self.snap is None:
            self.frame.bell()
            return
        self.pending.reset_all()
        self._done()

    def clear_bad(self):
        """기록 지우기 (a record that cannot be read: it is set aside when saved)."""
        if self.snap is None or not self.snap.record.error:
            self.frame.bell()
            return
        self.pending.clear_bad()
        self._done()
