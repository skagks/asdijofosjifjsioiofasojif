"""다른 장비로 옮기기: the dialog that makes a new mod of an appearance mod for other equipment
(equip/retarget.py does the work through the manager).

Each armor part can go to equipment of its own (부위마다 다른 장비로: the helm to one design, the body to
another, in one new mod): the table 부위별로 옮길 장비 has a row per part, and the equipment picked in the
list goes to the rows selected in it (all of them at first: one pick is a move of every part to one
equipment). A weapon is one row: its main and sub models move together.

What the mod can be moved from, the equipment it can go to (the game's names, read once and kept)
and what the new mod would hold are all worked out off the GUI thread from the mod's files as they
were when the dialog opened (read here: the mod's state is the GUI thread's); their results come back
through the app's events. The dialog only chooses: the app makes the mod (App.open_retarget)."""

from __future__ import annotations

import logging
import re
import threading
import tkinter as tk
import traceback
from collections import Counter
from tkinter import ttk
from typing import NamedTuple

from .equip import overview as ov
from .equip.model import ARMOR_PARTS, PART_LABELS, Target, parse_armor_key, parse_weapon_key
from .equip.paths import classify
from .equip.retarget import Renamer, RetargetError, Slot, parts_on, sources_of
from .gui import EXPECTED_ERRORS, RETARGET_ACTION, RETARGET_TITLE, Dialog, autohide

log = logging.getLogger(__name__)

TITLE = RETARGET_TITLE
ACTION = RETARGET_ACTION  # the context menu's entry and the 장비 외형 window's link
PLAN_DELAY = 150  # ms: a plan is worked out once the clicks stop
MOVES_SHOWN = 3  # moved files named as examples
OPTIONS_SHOWN = 3  # chosen options named
# what switching the original off loses besides the original equipment's look (left_behind)
OTHER_EQUIPMENT, OTHER_PARTS, OTHER_FILES, OUTSIDE = "다른 장비", "고르지 않은 부위", "그 밖의 게임 파일", "스크립트·플러그인"
# the table of the parts and the equipment each goes to
PARTS_TITLE = "부위별로 옮길 장비"
SELECT_ALL, CLEAR = "모든 부위 고르기", "옮기지 않기"  # its buttons
NOT_MOVED = "옮기지 않음"  # a row given no equipment
# the one row of a weapon (main and sub move together) and of armor whose parts are not known (all move)
WEAPON_ROW, ALL_ROW = "weapon", "all"
ROW_LABELS = {WEAPON_ROW: "본체·보조", ALL_ROW: "모든 부위"}
MANY_PARTS = 3  # more rows chosen than this: the line above the list says 모든 부위 (every row) or 부위 N개


class Choice(NamedTuple):
    """What the dialog gives back: the mod is made from ``source``'s files for ``target``."""
    source: Target
    # one equipment for every part moved, or (armor, parts to different equipment) {part: equipment} in
    # ARMOR_PARTS order; manager.create_retarget takes either
    target: Target | dict[str, Target]
    parts: tuple[str, ...] | None  # armor parts moved to the one equipment (None: all; with a dict: its parts)
    disable_original: bool
    new_name: str  # what the new mod is called (manager.retarget_name), "" when that was not known


def short_code(target: Target) -> str:
    """The equipment's folder numbers as in the lists (armor 034-001, weapon 00-0009)."""
    try:
        if target.kind == "armor":
            _body, model, design = parse_armor_key(target.key)
            return f"{model:03d}-{design:03d}"
        if target.kind == "weapon":
            _wtype, model = parse_weapon_key(target.key)
            return f"{model // 10000:02d}-{model % 10000:04d}"
    except ValueError:
        pass
    return target.key


def source_label(target: Target, catalog) -> str:
    """'이름 (여성 · 타입 2)', '이름 (쌍검)': an equipment the mod changes, for the 원래 장비 box."""
    title = ov.describe(target, catalog)[0]
    entry = catalog.entry(target) if catalog is not None else None
    return f"{title} ({ov._subtitle(target, entry, catalog)})"


def source_parts(scan, source: Target) -> list[str]:
    """The armor parts a mod's scan (equip.scan.ModScan) changes on the source's design, both bodies
    together (they move together), in the game's order."""
    return parts_on(scan.targets, source) if scan is not None else []


def _file_name(path: str) -> str:
    """A file's name without its folder and version number (ch03_034_0012.mesh)."""
    return re.sub(r"\.\d+$", "", path.replace("\\", "/").rsplit("/", 1)[-1])


def _particle(word: str, after_consonant: str, after_vowel: str) -> str:
    """``word`` with the particle its last syllable takes (을/를, 은/는)."""
    last = word[-1:]
    return word + (after_consonant if "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 else after_vowel)


def _with_object(word: str) -> str:
    """``word`` with 을 or 를."""
    return _particle(word, "을", "를")


def _with_topic(word: str) -> str:
    """``word`` with 은 or 는."""
    return _particle(word, "은", "는")


def row_label(row: str) -> str:
    """A row of the table: the armor part's name (머리), 본체·보조 (a weapon), 모든 부위."""
    return ROW_LABELS.get(row) or PART_LABELS.get(row, row)


def first_name(name: str) -> str:
    """The first of the names retarget_choices joins ('가나 갑옷 · 가나 갑옷 β' -> '가나 갑옷')."""
    return name.split(" · ", 1)[0]


def equipment_text(target: Target, names) -> str:
    """'가나 갑옷 (045-002)': equipment by its first name (``names``: target -> the list's name) and numbers."""
    code = short_code(target)
    name = first_name(names.get(target) or "")
    return code if not name else name if code in name else f"{name} ({code})"


def mapping_lines(rows, assigned, names) -> list[tuple[str | None, str]]:
    """(row, text) lines the plan text starts with when the rows do not all go to one equipment: one per row
    given equipment ("머리 → 호프 갑옷 (045-002)", in the table's order), then (None, "팔·허리는 옮기지
    않습니다") for the rows given none. None when every row goes to the same one (as in a move of the whole:
    the new mod's name says where). ``assigned``: row -> Target; ``names``: Target -> the list's name."""
    given = [row for row in rows if row in assigned]
    if not given or (len(given) == len(rows) and len({assigned[row] for row in given}) == 1):
        return []
    out: list[tuple[str | None, str]] = [(row, f"{row_label(row)} → {equipment_text(assigned[row], names)}")
                                         for row in given]
    left = [row_label(row) for row in rows if row not in assigned]
    if left:
        out.append((None, f"{_with_topic('·'.join(left))} 옮기지 않습니다"))
    return out


def part_hint(plan, parts_shown) -> str:
    """For errors about armor parts (Plan.bad_parts) when more than one part is moved (``parts_shown``: those
    given equipment): which to leave out or give other equipment to move the others ("" when that would not
    help)."""
    shown = list(parts_shown or ())
    bad = [p for p in shown if p in (getattr(plan, "bad_parts", None) or ())]
    if len(shown) < 2 or not bad or len(bad) == len(shown) or not plan.errors:
        return ""
    labels = "·".join(PART_LABELS.get(p, p) for p in bad)
    # (the table's buttons and the list act on the rows selected: the part has to be selected first)
    return (f"'{PARTS_TITLE}'에서 {_with_object(labels)} 고른 뒤 '{CLEAR}'를 누르거나 다른 장비를 고르면 나머지 "
            "부위는 그대로 옮길 수 있습니다")


def plan_text(plan, new_name: str = "", parts_shown=(), mapping=()) -> list[tuple[str, tuple[str, ...]]]:
    """What a retarget.Plan would do, as (text, Text tags) pieces: where each part goes when they do not all
    go to one equipment (``mapping``: mapping_lines; a part an error names in red), why it cannot be made
    (and, with the parts moved ``parts_shown``, which part to leave out), the files, the notes and the files
    left out (grouped by reason)."""
    out: list[tuple[str, tuple[str, ...]]] = []
    bad = getattr(plan, "bad_parts", None) or ()
    for row, line in mapping:
        out.append((f"{line}\n", ("muted",) if row is None else ("error",) if row in bad else ()))
    gap = ("gap",) if out else ()
    if plan.errors:
        out.append(("만들 수 없습니다\n", ("heading_error", *gap)))
        out += [(f"· {e}\n", ("error",)) for e in plan.errors]
        hint = part_hint(plan, parts_shown)
        if hint:
            out.append((f"{hint}\n", ("hint",)))
        gap = ("gap",)
    moves = [m for m in plan.moves if m.how != "fill"]
    fills = len(getattr(plan, "fills", None) or ()) or len(plan.moves) - len(moves)
    bits = [f"옮길 파일 {len(moves)}개"]
    if fills:
        bits.append(f"원래 장비의 게임 파일로 채움 {fills}개")
    if plan.kept:
        bits.append(f"그대로 넣을 파일 {len(plan.kept)}개")
    if plan.dropped:
        bits.append(f"넣지 않는 파일 {len(plan.dropped)}개")
    out.append((" · ".join(bits) + "\n", ("lead", *gap)))
    for move in moves[:MOVES_SHOWN]:
        out.append((f"{_file_name(move.old)} → {_file_name(move.new)}\n", ("muted",)))
    if len(moves) > MOVES_SHOWN:
        out.append((f"… 외 {len(moves) - MOVES_SHOWN}개\n", ("muted",)))
    if plan.ok and new_name:
        out.append((f"새 모드: {new_name}\n", ("name",)))
    if plan.notes:
        out.append(("알아 둘 점\n", ("heading",)))
        out += [(f"· {n}\n", ("warn",)) for n in plan.notes]
    if plan.dropped:
        out.append(("넣지 않는 파일\n", ("heading",)))
        for reason, n in Counter(reason for _path, reason in plan.dropped).most_common():
            out.append((f"· 파일 {n}개: {reason}\n", ("muted",)))
    return out


def left_behind(plan, sources=()) -> list[str]:
    """What the original mod changes that the new mod would not hold besides the original equipment itself
    (OTHER_EQUIPMENT, OTHER_PARTS, OTHER_FILES, OUTSIDE): switching the original off takes it out of the game
    too. Every game file left out counts unless its path makes it the original equipment's own (its avp/wvp,
    its sound banks): UI icons, text, data, effects, sound banks and shaders of no equipment (OTHER_FILES),
    an unnamed pak entry too. ``sources``: the equipment the mod can be moved from (both bodies of a design
    are one); ``plan``: a retarget.Plan, or None before one is known."""
    found = set()
    if len({Slot.of(s) or s for s in sources}) > 1:
        found.add(OTHER_EQUIPMENT)
    for path, reason in plan.dropped if plan is not None else ():
        name = str(path or "").replace("\\", "/").lstrip("/")
        in_pak = path is None or ".pak#" in name.lower()  # an unnamed pak entry (Plan.kept's <pak>#<hash>)
        if not in_pak and not name.lower().startswith("natives/"):
            found.add(OUTSIDE)
        elif reason.startswith("다른 장비"):
            found.add(OTHER_EQUIPMENT)
        elif reason.startswith("고르지 않은 부위"):
            found.add(OTHER_PARTS)
        elif in_pak or not _source_file(plan, name):
            found.add(OTHER_FILES)
    return [what for what in (OTHER_EQUIPMENT, OTHER_PARTS, OTHER_FILES, OUTSIDE) if what in found]


def _source_file(plan, path: str) -> bool:
    """Is a game file the plan leaves out the original equipment's own: in its folders (equip.paths.classify),
    or named with its numbers (a weapon's sound bank it0300_0009_m.sbnk, as retarget.plan tells them)?"""
    owner = classify(path)
    if owner is not None:
        return Slot.of(owner) == plan.source
    try:
        return Renamer(plan.source, plan.target).rename(path, all_parts=True) is not None
    except RetargetError:
        return False


def options_line(mod) -> str:
    """A mod with options: only the chosen ones' files are moved, and the new mod has none."""
    if not mod.options:
        return ""
    names = [o["name"] for o in mod.options if o["id"] in mod.selected]
    if not names:
        return "켠 옵션이 없어 옵션 밖의 파일만 옮깁니다. 새 모드에는 옵션이 없습니다."
    shown = ", ".join(names[:OPTIONS_SHOWN])
    if len(names) > OPTIONS_SHOWN:
        shown += f" 외 {len(names) - OPTIONS_SHOWN}개"
    return f"지금 켠 옵션({shown})의 파일만 옮깁니다. 새 모드에는 옵션이 없습니다."


class RetargetDialog(Dialog):
    """다른 장비로 옮기기 for one mod: 원래 장비, the table 부위별로 옮길 장비 (a row per armor part the mod
    changes there, one for a weapon) given equipment from a searchable list, and what the new mod would hold.
    Result: a Choice, or None."""

    def __init__(self, parent, app, mod_id: str, source: Target | None = None):
        super().__init__(parent, app.theme, TITLE)
        t, px, f, p = self.theme, self.theme.px, self.theme.fonts, self.theme.p
        self.app, self.manager, self.mod_id = app, app.manager, mod_id
        self.mod = app.manager.get(mod_id)
        # the mod's files as they are now (the selected options, repaired materials): read on this thread, the
        # mod's state is the GUI thread's; the scan and every plan work from these
        self.job = app.manager.equipment_job(mod_id)
        self.wanted = source  # picked once the sources are known (when the mod changes it)
        self.closed = False
        self.sources: list[Target] = []
        self.parts_of: dict[Target, list[str]] = {}
        self.source: Target | None = None
        self.choices: dict[Target, object] = {}  # source -> [(Target, name)] or the exception, once read
        self.names: dict[Target, str] = {}  # names of the equipment in the lists read so far
        self.new_names: dict[object, str] = {}  # what the new mod would be called (manager.retarget_name), by move
        self.part_rows: list[str] = []  # the table's rows: armor parts (ARMOR_PARTS order), WEAPON_ROW or ALL_ROW
        self.assigned: dict[str, Target] = {}  # row -> the equipment it goes to (rows without: not moved)
        self.rows: dict[str, Target] = {}  # list row -> equipment
        self._shown_pick: Target | None = None  # the list's row selected to show the chosen rows' equipment
        self._rows_seen: tuple[str, ...] = ()  # the table's selection the list and the buttons follow
        self.plans: dict[tuple, object] = {}  # (source, move, parts) -> retarget.Plan or the exception
        self._planning: tuple | None = None  # the plan being worked out
        self._plan_after = None
        self._off_touched = False  # the user ticked '원래 모드 끄기' themselves: the plans no longer change it
        wrap = px(900)

        ttk.Label(self.body, text=TITLE, style="DialogTitle.TLabel").pack(anchor="w")
        ttk.Label(self.body, text=f"'{self.mod.name}'이(가) 바꾸는 장비의 모습을 다른 장비에 입힌 새 모드를 만듭니다. "
                                  "부위마다 다른 장비를 고를 수 있습니다. 원래 모드는 지우지 않습니다.",
                  style="Muted.TLabel", wraplength=wrap, justify="left").pack(anchor="w", pady=(px(4), 0))
        options = options_line(self.mod)
        if options:
            ttk.Label(self.body, text=options, style="Muted.TLabel", wraplength=wrap, justify="left").pack(
                anchor="w", pady=(px(2), 0))

        ttk.Label(self.body, text="원래 장비", style="DialogSection.TLabel").pack(anchor="w", pady=(px(14), px(4)))
        self.source_var = tk.StringVar(self, value="읽는 중...")
        self.source_box = ttk.Combobox(self.body, textvariable=self.source_var, state="disabled", width=60)
        self.source_box.pack(anchor="w")
        self.source_box.bind("<<ComboboxSelected>>", lambda e: (self.source_box.selection_clear(),
                                                                self._set_source(self.source_box.current())))
        self.kind_note = ttk.Label(self.body, text="", style="Muted.TLabel", font=f["small"], wraplength=wrap,
                                   justify="left")
        self.kind_note.pack(anchor="w", pady=(px(6), 0))

        # three columns, read from the left: the parts, the equipment picked for them, what the new mod holds
        columns = ttk.Frame(self.body)
        columns.pack(fill="both", expand=True, pady=(px(14), 0))
        # the window fits a 768-pixel screen (with the taskbar), also with the options line, and a lower one
        # (1080 pixels at 150%)
        high = self.winfo_screenheight()
        rows = 9 if high >= px(900) else (5 if options else 6) if high >= px(760) else 3 if options else 4

        table = ttk.Frame(columns)
        table.pack(side="left", fill="y")
        ttk.Label(table, text=PARTS_TITLE, style="DialogSection.TLabel").pack(anchor="w", pady=(0, px(4)))
        card = t.mark(tk.Frame(table), "card")
        card.pack(fill="x")
        # rows start selected (a pick goes to every part); keyboard: Tab reaches it, the arrows move the selection
        self.parts_tree = ttk.Treeview(card, columns=("part", "target", "code"), show="headings", height=1,
                                       selectmode="extended", takefocus=True)
        for col, text, width in (("part", "부위", 74), ("target", "옮길 장비", 124), ("code", "번호", 70)):
            self.parts_tree.heading(col, text=text, anchor="w")
            self.parts_tree.column(col, width=px(width), anchor="w", stretch=col == "target")
        self.parts_tree.tag_configure("odd", background=p["row_alt"])
        self.parts_tree.tag_configure("none", foreground=p["muted"])
        self.parts_tree.pack(fill="x")
        self.parts_tree.bind("<<TreeviewSelect>>", lambda e: self._rows_selected())
        buttons = ttk.Frame(table)
        buttons.pack(anchor="w", pady=(px(8), 0))
        self.all_button = ttk.Button(buttons, text=SELECT_ALL, command=self.select_all_rows)  # (packed for 2+ rows)
        self.clear_button = ttk.Button(buttons, text=CLEAR, command=self.clear_rows)
        self.clear_button.pack(side="left")
        for button in (self.all_button, self.clear_button):
            button.state(["disabled"])

        middle = ttk.Frame(columns)
        middle.pack(side="left", fill="both", expand=True, padx=(px(16), 0))
        ttk.Label(middle, text="옮길 장비", style="DialogSection.TLabel").pack(anchor="w", pady=(0, px(2)))
        # which rows of the table the pick goes to
        self.pick_hint = ttk.Label(middle, text="", wraplength=px(310), justify="left")
        self.pick_hint.pack(anchor="w", pady=(0, px(6)))
        self.search_var = tk.StringVar(self)
        self.search = ttk.Entry(middle, textvariable=self.search_var)
        self.search.pack(fill="x", pady=(0, px(6)), ipady=px(1))
        self._search_hint(middle)
        card = t.mark(tk.Frame(middle), "card")
        card.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(card, columns=("name", "code"), show="headings", height=rows, selectmode="browse")
        for col, text, width, anchor in (("name", "장비", 236, "w"), ("code", "번호", 80, "w")):
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=px(width), anchor=anchor, stretch=col == "name")
        self.tree.tag_configure("odd", background=p["row_alt"])
        bar = ttk.Scrollbar(card, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=autohide(bar, side="right", fill="y"))
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_pick())
        # keyboard: the row with the keyboard focus is picked by the first arrow key, Return or space (Tk's
        # arrows move away from it, and its Return only opens tree items)
        for key in ("<Down>", "<Up>", "<Return>", "<KP_Enter>"):
            self.tree.bind(key, self._key_in_list)
        self.list_note = ttk.Label(middle, text="", style="Muted.TLabel", font=f["small"])
        self.list_note.pack(anchor="w", pady=(px(4), 0))

        right = ttk.Frame(columns)
        right.pack(side="left", fill="both", expand=True, padx=(px(16), 0))
        ttk.Label(right, text="새 모드에 들어갈 내용", style="DialogSection.TLabel").pack(anchor="w", pady=(0, px(4)))
        box = t.mark(tk.Frame(right), "card")
        box.pack(fill="both", expand=True)
        self.text = t.mark(tk.Text(box, wrap="word", width=36, height=8, font=f["base"], padx=px(14), pady=px(10),
                                   cursor="arrow", spacing1=px(2)), "text")
        text_bar = ttk.Scrollbar(box, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=autohide(text_bar, side="right", fill="y"))
        self.text.pack(side="left", fill="both", expand=True)
        tx = self.text
        tx.tag_configure("lead", font=f["bold"])
        tx.tag_configure("gap", spacing1=px(10))
        tx.tag_configure("name", spacing1=px(6))
        tx.tag_configure("muted", foreground=p["muted"], font=f["small"])
        tx.tag_configure("heading", foreground=p["accent"], font=f["small_bold"], spacing1=px(12))
        tx.tag_configure("heading_error", foreground=p["danger"], font=f["small_bold"])
        tx.tag_configure("error", foreground=p["danger"])
        tx.tag_configure("warn", foreground=p["warn"])
        tx.tag_configure("hint", spacing1=px(4))
        tx.configure(state="disabled")

        # 원래 모드 끄기: on only when the new mod holds everything the original changes but the original
        # equipment (left_behind), worked out again with each plan until the user ticks it themselves
        self.off_var = tk.BooleanVar(self, value=False)
        off_row = ttk.Frame(self.body)
        self.off_check = t.checkbutton(off_row, "원래 모드 끄기", self.off_var, command=self._off_ticked)
        self.off_check.pack(side="left")
        self.off_why = ttk.Label(off_row, text="", style="Muted.TLabel", font=f["small"])  # why it starts unticked
        self.off_why.pack(side="left", padx=(px(12), 0))
        self.off_note = ttk.Label(self.body, text="", style="Muted.TLabel", font=f["small"], wraplength=wrap,
                                  justify="left")
        if self.mod.enabled:
            off_row.pack(fill="x", pady=(px(12), 0))
            self.off_note.pack(anchor="w")
            self.off_note.configure(text="끄면 원래 장비는 게임 모습으로 돌아가고, 원래 모드가 바꾸던 다른 장비·부위·스크립트도 "
                                         "함께 꺼집니다.\n끄지 않으면 원래 장비와 옮긴 장비가 모두 모드 모습입니다.")
        else:  # (switching it off would change nothing)
            self.off_note.configure(text="원래 모드는 꺼져 있습니다. 새 모드는 켠 상태로 만듭니다.")
            self.off_note.pack(anchor="w", pady=(px(12), 0))
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=(px(16), 0))
        cancel = ttk.Button(row, text="취소", command=self.cancel)
        cancel.pack(side="right")
        self.create_button = ttk.Button(row, text="새 모드 만들기", style="Accent.TButton", command=self.ok)
        self.create_button.pack(side="right", padx=(0, px(8)))
        self.create_button.state(["disabled"])
        self.bind("<Return>", lambda e: self.ok())
        self.bind("<KP_Enter>", lambda e: self.ok())
        for button in (self.all_button, self.clear_button, cancel):  # Return on one of them presses it (as on
            for key in ("<Return>", "<KP_Enter>"):  # Windows), not 새 모드 만들기
                button.bind(key, lambda e, b=button: (b.invoke(), "break")[1])

        self._show([("장비 정보를 읽는 중... 처음 한 번은 조금 걸립니다.", ("muted",))])
        self._load_sources()

    def _search_hint(self, parent):
        """The placeholder of the search box (like the 장비 외형 window's)."""
        px = self.theme.px
        search = self.search
        hint = ttk.Label(parent, text="⌕  장비 이름 · 번호 검색", style="FieldHint.TLabel", cursor="xterm")
        hint.bind("<Button-1>", lambda e: search.focus_set())

        def update(*_args):
            if self.closed:
                return
            if self.search_var.get() or self.focus_get() is search:
                hint.place_forget()
            else:
                hint.place(in_=search, x=px(10), rely=0.5, anchor="w")

        def escape(_event):
            if self.search_var.get():
                self.search_var.set("")
                return "break"  # clear the search first; another Escape closes the dialog
            return None

        search.bind("<FocusIn>", update, add="+")
        search.bind("<FocusOut>", update, add="+")
        search.bind("<Escape>", escape)
        for key in ("<Down>", "<Return>", "<KP_Enter>"):  # on into the list (its first row picked)
            search.bind(key, self._into_list)
        self.search_var.trace_add("write", update)
        self.search_var.trace_add("write", lambda *a: self._fill_list())
        self.after_idle(update)

    # ---- background work
    def _run(self, kind: str, func, tag=None):
        """``func()`` on another thread; its result (or exception) comes back to _done on this one."""
        events = self.app.events

        def worker():
            try:
                result = func()
            except Exception as exc:  # shown in the dialog
                if not isinstance(exc, (*EXPECTED_ERRORS, RetargetError)):
                    log.error("다른 장비로 옮기기를 준비하다가 오류가 났습니다:\n%s", traceback.format_exc())
                result = exc
            events.put(("call", self._done, kind, tag, result))

        threading.Thread(target=worker, daemon=True).start()

    def _done(self, kind: str, tag, result):
        if self.closed:
            return
        if kind == "sources":
            self._sources_ready(result)
        elif kind == "choices":
            self._choices_ready(tag, result)
        elif kind == "plan":
            self._planned(tag, result)

    # ---- 원래 장비
    def _load_sources(self):
        m, mod_id, job = self.manager, self.mod_id, self.job

        def work():
            # the equipment the mod's files change (equipment_scans: from the files read when the dialog opened,
            # kept until they change) and among it what can be moved, with the parts to tick
            scan = m.equipment_scans([job]).get(mod_id)
            sources = sources_of(scan.targets) if scan is not None else []
            try:  # the game's names (read once, then kept); the folder numbers without them
                catalog = m.equipment_catalog() if sources else None
            except Exception as exc:
                log.info("장비 이름을 읽지 못했습니다: %s", exc)
                catalog = None
            return sources, [source_label(s, catalog) for s in sources], {s: source_parts(scan, s) for s in sources}

        self._run("sources", work)

    def _sources_ready(self, result):
        if isinstance(result, Exception):
            self.source_var.set("")
            self._show([("옮길 수 있는 장비를 찾지 못했습니다\n", ("heading_error",)), (str(result), ("error",))])
            return
        self.sources, labels, self.parts_of = result
        if not self.sources:
            self.source_var.set("")
            self._show([("이 모드에는 다른 장비로 옮길 수 있는 방어구나 무기의 외형이 없습니다.", ())])
            return
        self.source_box.configure(values=labels, state="readonly")
        self._off_default(left_behind(None, self.sources))  # (until a plan says more)
        index = self.sources.index(self.wanted) if self.wanted in self.sources else 0
        self.source_box.current(index)
        self._set_source(index)

    def _set_source(self, index: int):
        if not 0 <= index < len(self.sources) or self.sources[index] == self.source:
            return
        self.source = source = self.sources[index]
        self._fill_rows()
        if source not in self.choices:
            self.choices[source] = None  # being read
            self._run("choices", lambda m=self.manager: list(m.retarget_choices(source)), source)
        if self.search_var.get():
            self.search_var.set("")  # (fills the list)
        else:
            self._fill_list()
        self._want_plan()

    # ---- 부위별로 옮길 장비
    def _fill_rows(self):
        """The table for the source: a row per part the mod changes on that design (armor, both bodies: they
        move together; one row for all of them when the parts are not known), one row for a weapon (its
        models move together). Every row selected, none given equipment."""
        tree = self.parts_tree
        tree.delete(*tree.get_children())
        self.assigned = {}
        if self.source.kind == "armor":
            self.part_rows = list(self.parts_of.get(self.source) or []) or [ALL_ROW]
            note = "방어구는 남성용·여성용 파일을 함께 옮깁니다. 남성용은 남성용 장비로, 여성용은 여성용 장비로 갑니다."
            if self.part_rows == [ALL_ROW]:
                note += " 부위를 알 수 없어 모드가 바꾸는 부위를 모두 옮깁니다."
        else:
            self.part_rows = [WEAPON_ROW]
            note = "무기는 본체와 보조 모델을 함께 옮깁니다. 같은 종류의 무기로만 옮길 수 있습니다."
        self.kind_note.configure(text=note)
        for row in self.part_rows:
            tree.insert("", "end", iid=row)
        if len(self.part_rows) > 1:
            self.all_button.pack(side="left", padx=(0, self.theme.px(8)), before=self.clear_button)
        else:  # (one row: nothing else to choose)
            self.all_button.pack_forget()
        self._show_rows()
        tree.configure(height=len(self.part_rows))
        tree.selection_set(self.part_rows)
        tree.focus(self.part_rows[0])  # (the row Tk's arrow keys start from)
        self._parts_changed()

    def _show_rows(self):
        """Each row's equipment (its first name and numbers) or 옮기지 않음."""
        for i, row in enumerate(self.part_rows):
            target = self.assigned.get(row)
            if target is None:
                values, tags = (row_label(row), NOT_MOVED, ""), ("none",)
            else:
                name = first_name(self.names.get(target) or "") or short_code(target)
                values, tags = (row_label(row), name, short_code(target)), ()
            self.parts_tree.item(row, values=values, tags=(*tags, "odd") if i % 2 else tags)

    def selected_rows(self) -> list[str]:
        """The rows the equipment picked in the list goes to, in the table's order."""
        chosen = set(self.parts_tree.selection())
        return [row for row in self.part_rows if row in chosen]

    def select_all_rows(self):
        """모든 부위 고르기."""
        if self.part_rows:
            self.parts_tree.selection_set(self.part_rows)
            self._parts_changed()

    def clear_rows(self):
        """옮기지 않기: the selected rows lose their equipment (they are not moved)."""
        self._assign(self.selected_rows(), None)

    def _assign(self, rows: list[str], target: Target | None):
        for row in rows:
            if target is None:
                self.assigned.pop(row, None)
            else:
                self.assigned[row] = target
        self._show_rows()
        self._parts_changed()
        self._want_plan()

    def _rows_selected(self):
        """The table's selection changed (a click, an arrow key): followed unless it already was (Tk tells of
        the dialog's own selections later, maybe after a pick in the list that it would undo)."""
        if tuple(self.selected_rows()) != self._rows_seen:
            self._parts_changed()

    def _parts_changed(self):
        """The rows selected or their equipment changed: the line above the list, the two buttons and the
        list's selection follow."""
        if self.closed:
            return
        rows = self.selected_rows()
        self._rows_seen = tuple(rows)
        self.pick_hint.configure(text=self._pick_hint(rows))
        self.all_button.state(["!disabled"] if len(rows) < len(self.part_rows) else ["disabled"])
        self.clear_button.state(["!disabled"] if any(row in self.assigned for row in rows) else ["disabled"])
        self._highlight()

    def _pick_hint(self, rows: list[str]) -> str:
        """Which rows the next pick goes to ("머리 · 몸통에 입힐 장비를 고르세요")."""
        if not self.part_rows:
            return ""
        if not rows:
            return f"'{PARTS_TITLE}'에서 부위를 고르세요"
        if rows == [WEAPON_ROW]:
            return "이 무기의 모습을 입힐 무기를 고르세요"
        if rows == [ALL_ROW] or len(rows) == len(self.part_rows) > MANY_PARTS:
            return "모든 부위에 입힐 장비를 고르세요"
        if len(rows) > MANY_PARTS:  # (one line: the window does not grow while rows are clicked)
            return f"부위 {len(rows)}개에 입힐 장비를 고르세요"
        return f"{' · '.join(row_label(row) for row in rows)}에 입힐 장비를 고르세요"

    def _highlight(self):
        """The list shows the equipment of the selected rows selected when they all have the same one and the
        search shows it; nothing selected otherwise (its own pick does not give it to the rows again)."""
        wanted = {self.assigned.get(row) for row in self.selected_rows()}
        target = next(iter(wanted)) if len(wanted) == 1 else None
        iid = next((i for i, t in self.rows.items() if t == target), None) if target is not None else None
        self._shown_pick = target if iid is not None else None
        if iid is None:
            if self.tree.selection():
                self.tree.selection_set(())
        else:
            self.tree.focus(iid)
            self.tree.selection_set(iid)
            self.tree.see(iid)

    @property
    def target(self) -> Target | None:
        """The equipment every row given one goes to (None: no row has one, or rows go to different ones)."""
        found = set(self.assigned.values())
        return next(iter(found)) if len(found) == 1 else None

    # ---- 옮길 장비
    def _choices_ready(self, source: Target, result):
        self.choices[source] = result
        if not isinstance(result, Exception):
            self.names.update(result)
        if source == self.source:
            self._fill_list()
            self._show_rows()

    def _fill_list(self):
        if self.closed:
            return
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        choices = self.choices.get(self.source) if self.source is not None else []
        if choices is None:
            self.list_note.configure(text="장비 목록을 읽는 중...")
            return
        if isinstance(choices, Exception):
            self.list_note.configure(text=f"장비 목록을 읽지 못했습니다: {choices}")
            return
        query = self.search_var.get().strip().casefold()
        shown = [(target, name) for target, name in choices
                 if not query or query in f"{name} {short_code(target)} {target.key}".casefold()]
        for i, (target, name) in enumerate(shown):
            iid = str(i)
            self.rows[iid] = target
            self.tree.insert("", "end", iid=iid, values=(name, short_code(target)), tags=("odd",) if i % 2 else ())
        if shown:  # the row the keyboard starts from (Tk's keys do nothing without one): the one shown selected
            self.tree.focus("0")
        self._highlight()
        if not choices:
            note = "옮길 수 있는 장비가 없습니다."
        elif not shown:
            note = "검색에 맞는 장비가 없습니다."
        else:
            note = f"장비 {len(shown)}개" + (f" (전체 {len(choices)}개)" if len(shown) < len(choices) else "")
        self.list_note.configure(text=note)

    def _select_row(self, iid: str):
        self.tree.focus(iid)
        self.tree.selection_set(iid)
        self.tree.see(iid)

    def _key_in_list(self, event):
        """An arrow key, Return or Enter in the list: picks the row with the keyboard focus when it is not
        picked yet; else Tk moves on (arrows) or the dialog makes the mod (Return)."""
        focus = self.tree.focus()
        if focus and focus not in self.tree.selection():
            self._select_row(focus)
            return "break"
        return None

    def _into_list(self, _event=None):
        """Down or Return in the search box: into the list, its picked (or first) row picked."""
        items = self.tree.get_children()
        if items:
            self.tree.focus_set()
            self._select_row(self.tree.focus() or items[0])
        return "break"

    def _on_pick(self):
        """Equipment picked in the list: it goes to the selected rows of the table. (A search that hides the
        chosen one picks nothing: the rows keep it.)"""
        sel = self.tree.selection()
        target = self.rows.get(sel[0]) if sel else None
        if target is None or target == self._shown_pick:
            return  # (nothing, or the selected rows' own equipment shown)
        rows = self.selected_rows()
        if not rows:
            self._highlight()  # (no row to give it to: the line above the list says so)
            return
        self._assign(rows, target)

    # ---- what the new mod would hold
    def _move(self) -> tuple | None:
        """What the table asks for: (equipment, parts) when every row given equipment has the same (parts:
        those armor rows, None when that is every row), else ((part, equipment) pairs in ARMOR_PARTS order,
        None); None when no row has any."""
        given = [row for row in self.part_rows if row in self.assigned]
        if not given:
            return None
        targets = {self.assigned[row] for row in given}
        if len(targets) == 1:
            whole = len(given) == len(self.part_rows) or given[0] not in ARMOR_PARTS
            return next(iter(targets)), None if whole else tuple(given)
        return tuple((row, self.assigned[row]) for row in given), None

    def _key(self) -> tuple | None:
        move = self._move() if self.source is not None else None
        return None if move is None else (self.source, *move)

    @staticmethod
    def _target(move) -> Target | dict[str, Target]:
        """A key's move as the manager takes it: one Target, or {part: Target}."""
        return move if isinstance(move, Target) else dict(move)

    def _want_plan(self):
        """Work out what the chosen move would do, shortly (several clicks in a row cost one plan)."""
        if self.closed:
            return
        if self._plan_after is not None:
            self.after_cancel(self._plan_after)
            self._plan_after = None
        key = self._key()
        self._update_button()
        if key is None:
            if self.sources:
                self._show([("옮길 장비를 목록에서 고르세요.", ("muted",))])
            return
        if key in self.plans:
            self._render_plan(key)
            return
        self._show([("확인 중...", ("muted",))])
        self._plan_after = self.after(PLAN_DELAY, self._start_plan)

    def _start_plan(self):
        """One plan at a time: the newest choice is planned once the running one is done."""
        self._plan_after = None
        key = self._key()
        if self.closed or key is None or key in self.plans or self._planning is not None:
            return
        self._planning = key
        source, move, parts = key
        target = self._target(move)
        m, mod_id, files = self.manager, self.mod_id, self.job.files
        named = move in self.new_names

        def work():
            plan = m.plan_retarget(mod_id, source, target, parts=parts, files=files)
            name = None
            if not named:  # what create_retarget will call it (it only reads names)
                try:
                    name = m.retarget_name(mod_id, target)
                except Exception as exc:
                    log.info("새 모드의 이름을 알아내지 못했습니다: %s", exc)
            return plan, name

        self._run("plan", work, key)

    def _planned(self, key: tuple, result):
        self._planning = None
        if not isinstance(result, Exception):
            result, name = result
            if name:
                self.new_names[key[1]] = name
        self.plans[key] = result
        if key == self._key():
            self._render_plan(key)
        else:
            self._start_plan()  # the choice changed meanwhile

    def _render_plan(self, key: tuple):
        result = self.plans[key]
        if isinstance(result, Exception):
            self._show([("확인하지 못했습니다\n", ("heading_error",)), (str(result) or type(result).__name__, ("error",))])
        else:
            moved = [row for row in self.part_rows if row in self.assigned and row in ARMOR_PARTS]
            self._show(plan_text(result, self.new_names.get(key[1], ""), moved,
                                 mapping_lines(self.part_rows, self.assigned, self.names)))
            self._off_default(left_behind(result, self.sources))
        self._update_button()

    # ---- 원래 모드 끄기
    def _off_default(self, lost: list[str]):
        """Ticked by default only when switching the original off loses nothing but the original equipment's
        look (``lost``: left_behind; the line beside it says what else the original changes)."""
        if not self.mod.enabled:
            return
        if not self._off_touched:
            self.off_var.set(not lost)
        self.off_why.configure(text=f"원래 모드에는 새 모드에 들어가지 않는 파일({', '.join(lost)})도 있습니다"
                               if lost else "")

    def _off_ticked(self):
        self._off_touched = True

    def _ready(self) -> bool:
        key = self._key()
        plan = self.plans.get(key) if key is not None else None
        return plan is not None and not isinstance(plan, Exception) and bool(plan.ok)

    def _update_button(self):
        self.create_button.state(["!disabled"] if self._ready() else ["disabled"])

    def _show(self, pieces):
        tx = self.text
        tx.configure(state="normal")
        tx.delete("1.0", "end")
        for text, tags in pieces:
            tx.insert("end", text, tags)
        if tx.get("end-2c", "end-1c") == "\n":  # (no empty line below the last one)
            tx.delete("end-2c", "end-1c")
        tx.configure(state="disabled")
        tx.yview_moveto(0)

    def shown_text(self) -> str:
        """What the result box says (for the tests)."""
        return self.text.get("1.0", "end-1c")

    # ---- result
    def ok(self):
        if not self._ready():
            self.bell()
            return
        source, move, parts = self._key()
        self.result = Choice(source, self._target(move), parts, bool(self.off_var.get()) and self.mod.enabled,
                             self.new_names.get(move, ""))
        self.destroy()

    def destroy(self):
        self.closed = True
        if self._plan_after is not None:
            self.after_cancel(self._plan_after)
            self._plan_after = None
        super().destroy()  # (Dialog drops its Tk variables)
