"""다른 장비로 옮기기: the dialog that makes a new mod of an appearance mod for other equipment
(equip/retarget.py does the work through the manager).

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
from .equip.retarget import Renamer, RetargetError, Slot, sources_of
from .gui import EXPECTED_ERRORS, RETARGET_ACTION, RETARGET_TITLE, Dialog, autohide

log = logging.getLogger(__name__)

TITLE = RETARGET_TITLE
ACTION = RETARGET_ACTION  # the context menu's entry and the 장비 외형 window's link
PLAN_DELAY = 150  # ms: a plan is worked out once the clicks stop
MOVES_SHOWN = 3  # moved files named as examples
OPTIONS_SHOWN = 3  # chosen options named
# what switching the original off loses besides the original equipment's look (left_behind)
OTHER_EQUIPMENT, OTHER_PARTS, OTHER_FILES, OUTSIDE = "다른 장비", "고르지 않은 부위", "그 밖의 게임 파일", "스크립트·플러그인"


class Choice(NamedTuple):
    """What the dialog gives back: the mod is made from ``source``'s files for ``target``."""
    source: Target
    target: Target
    parts: tuple[str, ...] | None  # armor parts to move (None: all)
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
    if scan is None or source.kind != "armor":
        return []
    design = source.key.partition("/")[2]
    found = {t.part for t in scan.targets if t.kind == "armor" and t.key.partition("/")[2] == design and t.part}
    return [p for p in ARMOR_PARTS if p in found]


def _file_name(path: str) -> str:
    """A file's name without its folder and version number (ch03_034_0012.mesh)."""
    return re.sub(r"\.\d+$", "", path.replace("\\", "/").rsplit("/", 1)[-1])


def _with_object(word: str) -> str:
    """``word`` with 을 or 를 (by its last syllable)."""
    last = word[-1:]
    return word + ("을" if "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 else "를")


def part_hint(plan, parts_shown) -> str:
    """For errors about armor parts (Plan.bad_parts) when more than one part can be ticked: which to untick
    to move the others ("" when that would not help)."""
    shown = list(parts_shown or ())
    bad = [p for p in shown if p in (getattr(plan, "bad_parts", None) or ())]
    if len(shown) < 2 or not bad or len(bad) == len(shown) or not plan.errors:
        return ""
    labels = "·".join(PART_LABELS.get(p, p) for p in bad)
    return f"'옮길 부위'에서 {_with_object(labels)} 끄면 나머지 부위만 옮길 수 있습니다"


def plan_text(plan, new_name: str = "", parts_shown=()) -> list[tuple[str, tuple[str, ...]]]:
    """What a retarget.Plan would do, as (text, Text tags) pieces: why it cannot be made (and, with the
    part boxes ``parts_shown``, which part to untick), the files, the notes and the files left out
    (grouped by reason)."""
    out: list[tuple[str, tuple[str, ...]]] = []
    if plan.errors:
        out.append(("만들 수 없습니다\n", ("heading_error",)))
        out += [(f"· {e}\n", ("error",)) for e in plan.errors]
        hint = part_hint(plan, parts_shown)
        if hint:
            out.append((f"{hint}\n", ("hint",)))
    moves = [m for m in plan.moves if m.how != "fill"]
    fills = len(getattr(plan, "fills", None) or ()) or len(plan.moves) - len(moves)
    bits = [f"옮길 파일 {len(moves)}개"]
    if fills:
        bits.append(f"원래 장비의 게임 파일로 채움 {fills}개")
    if plan.kept:
        bits.append(f"그대로 넣을 파일 {len(plan.kept)}개")
    if plan.dropped:
        bits.append(f"넣지 않는 파일 {len(plan.dropped)}개")
    out.append((" · ".join(bits) + "\n", ("lead",) if not plan.errors else ("lead", "after_errors")))
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
    """다른 장비로 옮기기 for one mod: 원래 장비 (and, for armor, its parts), 옮길 장비 from a searchable
    list, and what the new mod would hold. Result: a Choice, or None."""

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
        self.new_names: dict[Target, str] = {}  # what the new mod would be called (manager.retarget_name)
        self.target: Target | None = None
        self.rows: dict[str, Target] = {}  # list row -> equipment
        self.plans: dict[tuple, object] = {}  # (source, target, parts) -> retarget.Plan or the exception
        self.part_vars: dict[str, tk.BooleanVar] = {}
        self._planning: tuple | None = None  # the plan being worked out
        self._plan_after = None
        self._off_touched = False  # the user ticked '원래 모드 끄기' themselves: the plans no longer change it
        wrap = px(780)

        ttk.Label(self.body, text=TITLE, style="DialogTitle.TLabel").pack(anchor="w")
        ttk.Label(self.body, text=f"'{self.mod.name}'이(가) 바꾸는 장비의 모습을 다른 장비에 입힌 새 모드를 만듭니다. "
                                  "원래 모드는 지우지 않습니다.",
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
        self.parts_row = ttk.Frame(self.body)  # (packed when there are parts to tick)
        self.kind_note = ttk.Label(self.body, text="", style="Muted.TLabel", font=f["small"], wraplength=wrap,
                                   justify="left")
        self.kind_note.pack(anchor="w", pady=(px(2), 0))

        columns = ttk.Frame(self.body)
        columns.pack(fill="both", expand=True, pady=(px(14), 0))
        # the window fits a 768-pixel screen (with the taskbar), also with the options line
        rows = 9 if self.winfo_screenheight() >= px(900) else 5 if options else 6
        left = ttk.Frame(columns)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="옮길 장비", style="DialogSection.TLabel").pack(anchor="w", pady=(0, px(4)))
        self.search_var = tk.StringVar(self)
        self.search = ttk.Entry(left, textvariable=self.search_var)
        self.search.pack(fill="x", pady=(0, px(6)), ipady=px(1))
        self._search_hint(left)
        card = t.mark(tk.Frame(left), "card")
        card.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(card, columns=("name", "code"), show="headings", height=rows, selectmode="browse")
        for col, text, width, anchor in (("name", "장비", 300, "w"), ("code", "번호", 90, "w")):
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
        self.list_note = ttk.Label(left, text="", style="Muted.TLabel", font=f["small"])
        self.list_note.pack(anchor="w", pady=(px(4), 0))

        right = ttk.Frame(columns)
        right.pack(side="left", fill="both", expand=True, padx=(px(16), 0))
        ttk.Label(right, text="새 모드에 들어갈 내용", style="DialogSection.TLabel").pack(anchor="w", pady=(0, px(4)))
        box = t.mark(tk.Frame(right), "card")
        box.pack(fill="both", expand=True)
        self.text = t.mark(tk.Text(box, wrap="word", width=44, height=8, font=f["base"], padx=px(14), pady=px(10),
                                   cursor="arrow", spacing1=px(2)), "text")
        text_bar = ttk.Scrollbar(box, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=autohide(text_bar, side="right", fill="y"))
        self.text.pack(side="left", fill="both", expand=True)
        tx = self.text
        tx.tag_configure("lead", font=f["bold"])
        tx.tag_configure("after_errors", spacing1=px(10))
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
        ttk.Button(row, text="취소", command=self.cancel).pack(side="right")
        self.create_button = ttk.Button(row, text="새 모드 만들기", style="Accent.TButton", command=self.ok)
        self.create_button.pack(side="right", padx=(0, px(8)))
        self.create_button.state(["disabled"])
        self.bind("<Return>", lambda e: self.ok())
        self.bind("<KP_Enter>", lambda e: self.ok())

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
        self.target = None
        self._fill_parts()
        if source not in self.choices:
            self.choices[source] = None  # being read
            self._run("choices", lambda m=self.manager: list(m.retarget_choices(source)), source)
        if self.search_var.get():
            self.search_var.set("")  # (fills the list)
        else:
            self._fill_list()
        self._want_plan()

    def _fill_parts(self):
        """Armor: a box per part the mod changes on that design (all on); a weapon moves whole."""
        px = self.theme.px
        for child in self.parts_row.winfo_children():
            child.destroy()
        self.part_vars = {}
        parts = (self.parts_of.get(self.source) or []) if self.source.kind == "armor" else []
        if parts:  # (an empty frame would keep the height it had)
            self.parts_row.pack(fill="x", pady=(px(6), 0), before=self.kind_note)
        else:
            self.parts_row.pack_forget()
        self.kind_note.pack_configure(pady=(px(2) if parts else px(6), 0))
        if self.source.kind != "armor":
            self.kind_note.configure(text="무기는 본체와 보조 모델을 함께 옮깁니다. 같은 종류의 무기로만 옮길 수 있습니다.")
            return
        if parts:
            ttk.Label(self.parts_row, text="옮길 부위").pack(side="left", padx=(0, px(12)))
        for part in parts:
            var = self.part_vars[part] = tk.BooleanVar(self, value=True)
            self.theme.checkbutton(self.parts_row, PART_LABELS.get(part, part), var, command=self._want_plan).pack(
                side="left", padx=(0, px(10)))
        note = "방어구는 남성용·여성용 파일을 함께 옮깁니다. 남성용은 남성용 장비로, 여성용은 여성용 장비로 갑니다."
        if not parts:
            note += " 부위를 알 수 없어 모드가 바꾸는 부위를 모두 옮깁니다."
        self.kind_note.configure(text=note)

    def _parts(self) -> tuple[str, ...] | None:
        """The ticked parts; None for all of them (or a weapon), () for none."""
        if not self.part_vars:
            return None
        chosen = tuple(p for p in ARMOR_PARTS if p in self.part_vars and self.part_vars[p].get())
        return None if len(chosen) == len(self.part_vars) else chosen

    # ---- 옮길 장비
    def _choices_ready(self, source: Target, result):
        self.choices[source] = result
        if not isinstance(result, Exception):
            self.names.update(result)
        if source == self.source:
            self._fill_list()

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
        picked = None
        for i, (target, name) in enumerate(shown):
            iid = str(i)
            self.rows[iid] = target
            self.tree.insert("", "end", iid=iid, values=(name, short_code(target)), tags=("odd",) if i % 2 else ())
            if target == self.target:
                picked = iid
        if picked is not None:
            self.tree.selection_set(picked)
            self.tree.see(picked)
        if shown:  # the row the keyboard starts from (Tk's keys do nothing without one)
            self.tree.focus(picked if picked is not None else "0")
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
        sel = self.tree.selection()
        target = self.rows.get(sel[0]) if sel else None
        if target is None or target == self.target:
            return  # (nothing picked when a search hides the chosen one: it stays chosen)
        self.target = target
        self._want_plan()

    # ---- what the new mod would hold
    def _key(self) -> tuple | None:
        if self.source is None or self.target is None:
            return None
        parts = self._parts()
        return None if parts == () else (self.source, self.target, parts)

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
            if not self.sources:
                return
            why = "옮길 부위를 하나 이상 고르세요." if self.target is not None else "옮길 장비를 목록에서 고르세요."
            self._show([(why, ("muted",))])
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
        source, target, parts = key
        m, mod_id, files = self.manager, self.mod_id, self.job.files
        named = target in self.new_names

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
            self._show(plan_text(result, self.new_names.get(key[1], ""), list(self.part_vars)))
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
        source, target, parts = self._key()
        self.result = Choice(source, target, parts, bool(self.off_var.get()) and self.mod.enabled,
                             self.new_names.get(target, ""))
        self.destroy()

    def destroy(self):
        self.closed = True
        if self._plan_after is not None:
            self.after_cancel(self._plan_after)
            self._plan_after = None
        super().destroy()  # (Dialog drops its Tk variables)
