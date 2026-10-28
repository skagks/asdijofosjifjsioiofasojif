"""모드 수정's pages in the GUI (modeditview: 파일 고르기, 게임 수치 / 스크립트 설정 and their ValuePanel, 수정 내용), run
inside the window's shell (modtool.ModToolDialog) with an app and a manager of the test (FakeManager: snapshots,
file lists and values made here; the real ones: test_modedit_manager.py), and once with the real manager end to
end (the plan a page makes is one the manager saves). Values of data files come from the real engine on the
synthetic files of tests/modedit_fixtures (a made-up type registry)."""

from __future__ import annotations

import gc
import queue
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

import modedit_fixtures as mf
from conftest import make_tree
from mhwmm.matfix import rsz
from mhwmm.modedit import engines, model, rszdata

tk = pytest.importorskip("tkinter")

ITEM = "natives/STM/GameDesign/Common/Item/ItemData.user.3"
HELM = "natives/STM/Art/Model/Character/ch03/004/001/3/ch03_004_0013.mdf2.45"
MESH = "natives/STM/Art/Model/Character/ch03/004/001/3/ch03_004_0013.mesh.241111606"
TEX = "natives/STM/Art/Model/Character/ch03/004/001/3/ch03_004_0013_ALBD.tex.241106027"
STREAM = "natives/STM/streaming/Art/Model/Character/ch03/004/001/3/ch03_004_0013_ALBD.tex.241106027"
LUA = "reframework/autorun/NierUI/Config.lua"
MAIN = "reframework/autorun/NierUI.lua"
SELL = "_Values[_ItemId={}]._SellPrice"
EAT = "_Values[_ItemId={}]._Eatable"


@pytest.fixture(autouse=True)
def types(monkeypatch):
    """The made-up registry of modedit_fixtures (the window holds the registry for 게임 수치)."""
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", mf.registry())
    monkeypatch.setattr(rsz.TypeRegistry, "_from_file", False)
    monkeypatch.setattr(rsz.TypeRegistry, "_holds", 0)
    monkeypatch.setattr(rszdata, "_CHECKED", weakref.WeakKeyDictionary())


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("화면(display)이 없어 GUI 테스트를 건너뜁니다")
    yield r
    gc.collect()  # Tk variables of the test must go before the interpreter does (else Python 3.10 can crash)
    try:
        r.destroy()
    except tk.TclError:
        pass


class Answers(list):
    """What the message boxes were asked ((title, text) each) and what askyesno / askyesnocancel answer, in order
    (``replies``; then 아니요 / 취소: the window stays)."""

    def __init__(self):
        super().__init__()
        self.replies: list = []


@pytest.fixture(autouse=True)
def answers(monkeypatch):
    """A message box left open would block the test."""
    from tkinter import messagebox

    asked = Answers()
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(messagebox, name, lambda *a, **k: asked.append(a))
    monkeypatch.setattr(messagebox, "askyesno",
                        lambda *a, **k: (asked.append(a), asked.replies.pop(0) if asked.replies else False)[1])
    monkeypatch.setattr(messagebox, "askyesnocancel",
                        lambda *a, **k: (asked.append(a), asked.replies.pop(0) if asked.replies else None)[1])
    return asked


# ---- the app and the manager of the tests -----------------------------------------------------------------------
@dataclass
class FakeMod:
    id: str
    name: str
    enabled: bool = True
    options: list = field(default_factory=list)
    selected: list = field(default_factory=list)
    retarget: dict = field(default_factory=dict)
    origin: str = ""


class FakeManager:
    """The manager's 모드 수정 API the pages use (§4), from what the test puts in: ``snaps`` (mod id -> snapshot),
    ``targets`` ((mod id, tool) -> list or exception), ``views`` ((key, member) -> ValueView), ``items``
    (mod id -> EditItemView list); ``calls``: what was asked (name, ..., on the GUI thread)."""

    def __init__(self):
        self.mods: list[FakeMod] = []
        self.snaps: dict = {}
        self.targets: dict = {}
        self.views: dict = {}
        self.items: dict = {}
        self.calls: list = []
        self.gate: threading.Event | None = None  # open_values waits for it

    def _call(self, *what):
        self.calls.append((*what, threading.current_thread() is threading.main_thread()))

    def add(self, mod: FakeMod, snap: model.EditSnapshot):
        self.mods.append(mod)
        self.snaps[mod.id] = snap
        return mod

    def get(self, mod_id):
        return next(m for m in self.mods if m.id == mod_id)

    def edit_snapshot(self, mod_id, conflicts=None):
        self._call("edit_snapshot", mod_id)
        return self.snaps[mod_id]

    def equipment_job(self, mod_id):
        return None

    def mod_kinds(self, mod_id):
        return set()

    def equipment_summary(self, mod_id):
        return []

    def edit_targets(self, snap, tool, progress=None):
        self._call("edit_targets", snap.mod_id, tool)
        found = self.targets.get((snap.mod_id, tool), [])
        if isinstance(found, Exception):
            raise found
        return list(found)

    def open_values(self, snap, target, progress=None):
        self._call("open_values", target.key, target.member)
        if self.gate is not None:
            self.gate.wait(10)
        found = self.views[(target.key, target.member)]
        if isinstance(found, Exception):
            raise found
        return found

    def edit_items(self, mod_id):
        self._call("edit_items", mod_id)
        return list(self.items.get(mod_id, []))

    def drop_edit_notice(self, mod_id):
        self._call("drop_edit_notice", mod_id)


def make_app(root, manager):
    from mhwmm.theme import Theme

    theme = Theme(root, "light")
    theme.use("light")
    return SimpleNamespace(root=root, theme=theme, manager=manager, events=queue.Queue(), editable=lambda mod: True,
                           _mod_tool_page=None, _tool_file={}, _conflicts=None)


def pump(app, until, timeout=10.0):
    """Run the window's events (results of the pages' work come back through app.events) until ``until()``."""
    end = time.time() + timeout
    while True:
        while True:
            try:
                item = app.events.get_nowait()
            except queue.Empty:
                break
            if item[0] == "call":
                item[1](*item[2:])
        app.root.update()
        if until():
            return
        if time.time() > end:
            raise AssertionError("시간 초과")
        time.sleep(0.01)


def window(app, mod_id, page):
    """The window on ``page``, shown (not modal: the test drives it) so that keys can reach it."""
    from mhwmm import modtool

    dialog = modtool.ModToolDialog(app.root, app, mod_id, page=page)
    dialog.deiconify()
    return dialog


def options_of(mod: FakeMod) -> list[model.OptionRef]:
    out, seen = [], {}
    for o in mod.options:
        n = seen.get(o["name"], 0)
        seen[o["name"]] = n + 1
        out.append(model.OptionRef(o["id"], o["name"], n, o["id"] in mod.selected))
    return out


def efile(rel, option: model.OptionRef | None = None, path: Path | None = None, state="in", twin="", kind="기타"):
    source = model.Source(option.name, option.n, rel) if option else model.Source("", 0, rel)
    key = f"{option.id}/{rel}" if option else rel
    return model.EditFile(source, option.id if option else None, key, rel, path or Path(rel), 1234, kind, state,
                          twin=twin)


def snapshot(mod: FakeMod, files, record: model.Record | None = None, overlaps=None, issues=()):
    record = record or model.Record()
    return model.EditSnapshot(mod.id, mod.name, mod.enabled, "1|", record.rev, options_of(mod), list(files),
                              record.copy(), {}, {}, {}, dict(overlaps or {}), frozenset(), list(issues), None, {},
                              Path("."))


def focus(app, widget):
    widget.focus_force()
    pump(app, lambda: str(widget.tk.call("focus")) == str(widget))


def key(app, widget, name):
    """A key pressed on ``widget`` (with the keyboard focus)."""
    focus(app, widget)
    widget.event_generate(name)
    app.root.update()


def double_click(app, widget, x, y):
    """Two clicks in a row at (x, y): Tk makes the second a double click."""
    for _ in range(2):
        widget.event_generate("<ButtonPress-1>", x=x, y=y)
        widget.event_generate("<ButtonRelease-1>", x=x, y=y)
    app.root.update()


def pick(app, tree, *rows):
    tree.selection_set(rows)
    app.root.update()


# ---- the pages' helpers (no window) ----------------------------------------------------------------------------
def test_file_states_follow_the_unsaved_exclusions():
    """What the game gets of each stored file once saved: the later chosen option's copy wins, a copy left out lets
    the earlier one win; an option that is off, no option on, an update's conflict."""
    from mhwmm.modeditview import file_states

    mod = FakeMod("c", "Colors", options=[{"id": "o1", "name": "Red"}, {"id": "o2", "name": "Red"},
                                          {"id": "o3", "name": "Blue"}], selected=["o1", "o2"])
    red, red2, blue = options_of(mod)
    files = [efile(HELM, red), efile(HELM, red2), efile(HELM, blue), efile(TEX, red, state="conflict")]
    snap = snapshot(mod, files)
    out = set()
    states = file_states(snap, lambda f: f.key in out)
    assert states == {f"o1/{HELM}": ("overridden", "Red (2번째)"), f"o2/{HELM}": ("in", ""),
                      f"o3/{HELM}": ("option_off", ""), f"o1/{TEX}": ("conflict", "")}
    out = {f"o2/{HELM}", f"o1/{TEX}"}  # (a conflict's exclusion is not used)
    states = file_states(snap, lambda f: f.key in out)
    assert states[f"o1/{HELM}"] == ("in", "") and states[f"o2/{HELM}"] == ("excluded", "")
    assert states[f"o1/{TEX}"] == ("conflict", "")
    mod.selected = []
    assert set(file_states(snapshot(mod, files), lambda f: False).values()) == {("needs_option", ""), ("conflict", "")}


def test_value_helpers(tmp_path):
    """The kind line, how values are shown, the hint's values, the same-name stem, the scripts that require a script,
    and the rows the mod changed (worked out on the worker)."""
    from mhwmm import modeditview as mv

    value = engines.Value("a", "a", "", "S16", "1", spec=("S16", -32768, 32767))
    assert mv.type_text(value) == "정수 (S16) · -32768 ~ 32767"
    assert mv.type_text(engines.Value("a", "a", "", "F32", "1.0")) == "실수 (F32)"
    assert mv.type_text(engines.Value("a", "a", "", "float", "1.0")) == "실수"
    assert mv.type_text(engines.Value("a", "a", "", "Bool", "true")) == "켜기·끄기"
    assert mv.type_text(engines.Value("a", "a", "", "Vec3", "1.0, 2.0, 3.0", spec=("Vec3", 3, "<3f"))) == "값 3개 (Vec3)"
    assert mv.type_text(engines.Value("a", "a", "", "Color", "1, 2, 3, 4")) == "색 (R, G, B, A: 0~255)"
    assert mv.type_text(engines.Value("a", "a", "", "string", "x")) == "글자"
    assert mv.shown("Bool", "true") == "켬" and mv.shown("bool", "false") == "끔" and mv.shown("S32", None) == ""
    assert mv.hint_values("게임에 쓰인 값: 0, 1, 25") == ["0", "1", "25"] and mv.hint_values("") == []
    assert mv.stem_of(MESH) == (MESH.rsplit("/", 1)[0], "ch03_004_0013", ".mesh")
    assert mv.stem_of("dinput8.dll") == ("", "dinput8", ".dll")
    assert mv.option_label(model.Source("Red", 1, "x")) == "Red (2번째)" and mv.option_label(model.Source()) == ""
    folder = make_tree(tmp_path, {LUA: "return {}", MAIN: 'local c = require("NierUI.Config")\n',
                                  "reframework/autorun/other.lua": "require('json')"})
    scripts = [(rel, rel, folder / rel) for rel in (LUA, MAIN, "reframework/autorun/other.lua")]
    assert mv.lua_requirers(scripts) == {LUA: ["NierUI.lua"]}
    assert mv.hint_text("Bool", "게임에 쓰인 값: true, false") == "게임에 쓰인 값: 켬, 끔"
    assert mv.hint_text("S32", "게임에 쓰인 값: 1, 2") == "게임에 쓰인 값: 1, 2" and mv.hint_text("Bool", "") == ""
    # the rows the mod changed: a value the game has another of, every value of a row the game lacks; a value
    # without a game value in a row the game has was not paired (as rszdata leaves _Index): not changed
    target = data_target()
    view = item_view(target, {SELL.format(100): "10", SELL.format(101): None}, lacking={"_Values[_ItemId=102]"})
    prepared = mv.prepare(view, engines.get("rsz"))
    differs = {row.value.at for row, d in zip(view.rows, prepared.differs) if d}
    assert differs == {SELL.format(100)} | {r.value.at for r in view.rows if r.value.group == "_Values[_ItemId=102]"}
    missing = {row.value.group for row, x in zip(view.rows, prepared.missing) if x}
    assert missing == {"_Values[_ItemId=102]"} and mv.added_groups(view) == {"_Values[_ItemId=102]"}
    assert prepared.words[0] == "_index\n_values[_itemid=100]._index\n_values[_itemid=100]"
    view.added = frozenset()  # (only the groups compare found are rows the game lacks: no guessing)
    assert not any(mv.prepare(view, engines.get("rsz")).missing)


# ---- 파일 고르기 --------------------------------------------------------------------------------------------------
@pytest.fixture
def files_env(root, tmp_path):
    m = FakeManager()
    mod = FakeMod("loose", "Armor")
    files = [efile(HELM, kind="재질"), efile(MESH, kind="모델"), efile(TEX, twin=STREAM, kind="텍스처"),
             efile(STREAM, twin=TEX, kind="텍스처"), efile("dinput8.dll", kind="플러그인")]
    m.add(mod, snapshot(mod, files, overlaps={HELM.casefold(): "Other Armor"}))
    app = make_app(root, m)
    return app, m, mod


def row_of(page, rel):
    return next(k for k in page.tree.get_children() if page.by_key[k].rel == rel)


def test_files_page_toggles_by_space_click_and_buttons(files_env):
    """space (and Return, a double click), a click on the row's 넣기 box, Delete and the panel's 빼기 / 넣기 leave a
    file out or put it back as an unsaved change: ☐ and "● 뺌" on its row, the count, the footer; the window stays
    open."""
    app, m, mod = files_env
    dialog = window(app, mod.id, "files")
    try:
        page = dialog.pages["files"]
        assert dialog.page_key == "files" and page.view_box.winfo_ismapped() is not None
        tree = page.tree
        helm = row_of(page, HELM)
        tree.selection_set(helm)
        tree.focus(helm)
        app.root.update()
        assert page.p_name.cget("text") == HELM.rsplit("/", 1)[1] and "재질" in page.p_info.cget("text")
        assert "Other Armor" in page.p_notes.cget("text")  # (another mod wins the path)
        key(app, tree, "<space>")
        source = model.Source("", 0, HELM)
        assert dialog.pending.excluded(source) and dialog.pending.count() == 1 and not dialog.closed
        values = tree.item(helm, "values")
        assert values[0] == "☐" and values[3] == "● 뺌" and "changed" in tree.item(helm, "tags")
        assert page.count.cget("text").startswith("파일 5개 · 뺀 파일 1개")
        assert dialog.footer.cget("text").startswith("저장하지 않은 수정 1개")
        key(app, tree, "<Return>")
        assert not dialog.pending.excluded(source) and dialog.pending.count() == 0 and not dialog.closed
        assert tree.item(helm, "values")[0] == "☑" and tree.item(helm, "values")[3] == "넣음"
        # a click on the 넣기 box of a row (that row only, selected)
        dll = row_of(page, "dinput8.dll")
        tree.see(dll)
        app.root.update()
        x, y, w, h = tree.bbox(dll, "use")
        tree.event_generate("<Button-1>", x=x + w // 2, y=y + h // 2)
        app.root.update()
        assert tree.selection() == (dll,) and dialog.pending.excluded(model.Source("", 0, "dinput8.dll"))
        page.in_button.invoke()
        assert dialog.pending.count() == 0
        page.out_button.invoke()
        assert dialog.pending.excluded(model.Source("", 0, "dinput8.dll"))
        page.in_button.invoke()
        # several rows: Delete leaves them out, space puts them back (all were out)
        tree.selection_set([helm, dll])
        app.root.update()
        assert page.p_name.cget("text") == "고른 파일 2개"
        key(app, tree, "<Delete>")
        assert dialog.pending.count() == 2
        key(app, tree, "<space>")
        assert dialog.pending.count() == 0
        # a double click on the file's name toggles; on the 넣기 box it is one toggle (its first click did it)
        pick(app, tree, helm)
        x, y, w, h = tree.bbox(helm, "file")
        double_click(app, tree, x + 5, y + h // 2)
        assert dialog.pending.excluded(source)
        x, y, w, h = tree.bbox(helm, "use")
        double_click(app, tree, x + w // 2, y + h // 2)
        assert not dialog.pending.excluded(source)
    finally:
        dialog.destroy()


def test_files_page_streaming_copy_follows(files_env):
    """A texture left out takes its streaming copy (same option) with it, and back; the panel says so."""
    app, m, mod = files_env
    dialog = window(app, mod.id, "files")
    try:
        page = dialog.pages["files"]
        tex = row_of(page, TEX)
        page.tree.selection_set(tex)
        app.root.update()
        assert page.p_twin.cget("text") == "스트리밍 사본도 함께 뺍니다"
        page.out_button.invoke()
        pending = dialog.pending
        assert pending.excluded(model.Source("", 0, TEX)) and pending.excluded(model.Source("", 0, STREAM))
        assert page.p_twin.cget("text") == "스트리밍 사본도 함께 넣습니다"
        assert page.tree.item(row_of(page, STREAM), "values")[0] == "☐"
        page.in_button.invoke()
        assert pending.count() == 0
    finally:
        dialog.destroy()


def test_files_page_use_option_is_exclusions(root):
    """쓸 옵션 on a path two chosen options have: picking one leaves the other chosen options' copies out (the
    rows say whose copy goes in); 자동 takes the exclusions back. An option that is off is not offered, and its
    rows say 옵션 꺼짐."""
    m = FakeManager()
    mod = FakeMod("colors", "Colors", options=[{"id": "o1", "name": "Red"}, {"id": "o2", "name": "Blue"},
                                               {"id": "o3", "name": "Green"}], selected=["o1", "o2"])
    red, blue, green = options_of(mod)
    m.add(mod, snapshot(mod, [efile(HELM, red), efile(HELM, blue), efile(HELM, green), efile(TEX, red)]))
    app = make_app(root, m)
    dialog = window(app, mod.id, "files")
    try:
        page = dialog.pages["files"]
        tree = page.tree
        keys = {"red": f"o1/{HELM}", "blue": f"o2/{HELM}", "green": f"o3/{HELM}"}
        assert tree.item(keys["red"], "values")[3] == "'Blue'의 파일에 덮어써짐"
        assert tree.item(keys["blue"], "values")[3] == "'Blue'의 것"
        assert tree.item(keys["green"], "values")[3] == "옵션 꺼짐" and "muted" in tree.item(keys["green"], "tags")
        tree.selection_set(keys["red"])
        app.root.update()
        assert page.use_row.winfo_manager() == "pack"
        assert list(page.use_box.cget("values")) == ["Red", "Blue", "자동 (아래쪽 옵션)"]
        assert page.use_box.current() == 2
        assert "아래쪽 옵션 'Blue'의 파일이 이 파일을 덮어씁니다" in page.p_notes.cget("text")
        page.use_box.current(0)
        page.use_box.event_generate("<<ComboboxSelected>>")
        app.root.update()
        pending = dialog.pending
        assert pending.excluded(model.Source("Blue", 0, HELM)) and not pending.excluded(model.Source("Red", 0, HELM))
        assert not pending.excluded(model.Source("Green", 0, HELM))  # (off: not one of the copies)
        assert tree.item(keys["red"], "values")[3] == "'Red'의 것"
        assert tree.item(keys["blue"], "values")[3] == "● 뺌"
        assert page.use_box.current() == 0
        page.use_box.current(2)
        page.use_box.event_generate("<<ComboboxSelected>>")
        app.root.update()
        assert pending.count() == 0 and tree.item(keys["red"], "values")[3] == "'Blue'의 파일에 덮어써짐"
        # the green copy: its option is off
        tree.selection_set(keys["green"])
        app.root.update()
        assert page.use_row.winfo_manager() == "" and "이 옵션은 지금 꺼져 있어" in page.p_notes.cget("text")
        assert "이 파일이 든 옵션: Red, Blue, Green" == page.p_options.cget("text")
    finally:
        dialog.destroy()


def test_files_page_same_name_note_and_together(files_env):
    """Part of a same-name group left out (the .mesh beside its .mdf2): the note says which are in and out, and
    함께 빼기 leaves the rest out."""
    app, m, mod = files_env
    dialog = window(app, mod.id, "files")
    try:
        page = dialog.pages["files"]
        mesh = row_of(page, MESH)
        page.tree.selection_set(mesh)
        app.root.update()
        assert page.together_button.winfo_manager() == ""
        page.out_button.invoke()
        app.root.update()
        text = page.p_notes.cget("text")
        assert "같은 이름의 파일 가운데 일부만 뺐습니다: ch03_004_0013 (.mdf2은 넣고 .mesh은 뺌)" in text
        assert page.together_button.winfo_manager() == "pack"
        page.together_button.invoke()
        assert dialog.pending.excluded(model.Source("", 0, HELM)) and dialog.pending.count() == 2
        assert page.together_button.winfo_manager() == ""
    finally:
        dialog.destroy()


def test_files_page_views_search_and_row_cap(files_env, monkeypatch):
    """보기 (켠 옵션의 파일 / 겹치는 파일 / 다른 모드에 덮어써지는 파일 / 뺀 파일) and the search narrow the table; at most
    MAX_ROWS rows, then the count line says so."""
    from mhwmm import modeditview

    app, m, mod = files_env
    dialog = window(app, mod.id, "files")
    try:
        page = dialog.pages["files"]

        def shown():
            return [page.by_key[k].rel for k in page.tree.get_children()]

        assert len(shown()) == 5
        page.view_box.current(3)
        page.view_box.event_generate("<<ComboboxSelected>>")
        assert shown() == [HELM]
        page.view_box.current(4)
        page.view_box.event_generate("<<ComboboxSelected>>")
        assert shown() == []
        page.view_box.current(0)
        page.view_box.event_generate("<<ComboboxSelected>>")
        pick(app, page.tree, row_of(page, "dinput8.dll"))
        page.out_button.invoke()
        page.view_box.current(4)
        page.view_box.event_generate("<<ComboboxSelected>>")
        assert shown() == ["dinput8.dll"]
        page.view_box.current(0)
        page.view_box.event_generate("<<ComboboxSelected>>")
        page.search_var.set("ALBD")
        pump(app, lambda: len(shown()) == 2)
        assert shown() == [TEX, STREAM] and "보이는 파일 2개" in page.count.cget("text")
        # Escape in the search empties it first (the window stays), Return goes into the table
        key(app, page.search, "<Escape>")
        assert page.search_var.get() == "" and not dialog.closed
        page.search_var.set("ALBD")
        key(app, page.search, "<Return>")
        assert str(page.tree.tk.call("focus")) == str(page.tree) and page.tree.selection() == (f"{TEX}",)
        monkeypatch.setattr(modeditview, "MAX_ROWS", 3)
        page.search_var.set("")
        page.fill()
        assert len(shown()) == 3 and "결과가 많아 처음 2,000개만" not in page.count.cget("text")
        assert "결과가 많아 처음 3개만 보여 줍니다" in page.count.cget("text")
    finally:
        dialog.destroy()


def test_files_page_keeps_its_width_when_its_table_scrolls(root):
    """The table's scroll bar takes its width from the 파일 column: the page asks for the same width with it and
    without it, so the panel beside the table keeps its width (the window is frozen: a wider page would take the
    width from the panel, which would jump sideways as a search shows or hides the bar)."""
    m = FakeManager()
    mod = FakeMod("many", "Many Files")
    m.add(mod, snapshot(mod, [efile(f"natives/STM/Art/x/file{i:03}.tex.241106027", kind="텍스처") for i in range(60)]))
    app = make_app(root, m)
    dialog = window(app, mod.id, "files")
    try:
        page = dialog.pages["files"]
        bar = next(w for w in page.tree.master.winfo_children() if w.winfo_class() == "TScrollbar")
        panel = page.scroll.frame

        def state():
            dialog.update()
            return bar.winfo_manager(), page.frame.winfo_reqwidth(), panel.winfo_width(), page.tree.winfo_width()

        pump(app, lambda: bar.winfo_manager() == "pack")
        shown = state()
        page.search_var.set("file001")
        pump(app, lambda: bar.winfo_manager() == "")
        hidden = state()
        page.search_var.set("")
        pump(app, lambda: bar.winfo_manager() == "pack")
        again = state()
        assert shown[1:3] == hidden[1:3] == again[1:3]  # (the page's width asked for, the panel's width)
        assert hidden[3] == shown[3] + bar.winfo_reqwidth()  # (the table is narrower by the bar)
        width = int(page.tree.column("file", "width"))
        page.search_var.set("file001")
        pump(app, lambda: bar.winfo_manager() == "")
        state()
        assert int(page.tree.column("file", "width")) == width + bar.winfo_reqwidth()  # (ttk stretches it)
    finally:
        dialog.destroy()


@pytest.mark.parametrize("key_", ["files", "data", "summary"])
def test_a_table_bar_while_the_window_was_bigger_leaves_the_panel_its_width(root, key_):
    """The window made bigger (ttk stretches the table's columns to fill it), the table's scroll bar hidden or shown
    (a search; on 수정 내용 fewer lines), the window made smaller again, in either order: the page still asks for the
    width it was made with, so the panel beside the table is there at its width and in its place (were the table to
    ask for its stretched width, the panel would be squeezed or gone once the window is smaller)."""
    scaling = root.tk.call("tk", "scaling")
    root.tk.call("tk", "scaling", 4 / 3)  # (100%: the window as made fits the tests' screen, 1280 pixels wide)
    m = FakeManager()
    mod = FakeMod("many", "Many")
    rels = [f"natives/STM/Art/x/file{i:03}.tex.241106027" for i in range(60)]
    out = rels[:40] if key_ == "summary" else []  # (수정 내용: 40 files left out, a line each)
    record = model.Record(rev=1, items=[model.Item(model.Source("", 0, rel), exclude=True) for rel in out])
    m.add(mod, snapshot(mod, [efile(rel, state="excluded" if rel in out else "in", kind="텍스처") for rel in rels] +
                       [efile(ITEM, kind="데이터")], record=record))
    lines = [model.EditItemView("exclude", model.Source("", 0, rel), "", rel, (), (), "active") for rel in out]
    m.items[mod.id] = lines
    target = data_target()
    m.targets[(mod.id, "data")] = [target]
    m.views[(target.key, "")] = item_view(target, {}, n=40)
    app = make_app(root, m)
    dialog = window(app, mod.id, key_)
    try:
        page = dialog.pages[key_]
        panel = page.panel if key_ == "data" else page.scroll.frame
        bar = next(w for w in page.tree.master.winfo_children() if w.winfo_class() == "TScrollbar")
        word = "file001" if key_ == "files" else "_ItemId=101]._SellPrice"
        if key_ == "data":
            pump(app, lambda: page.ready_view is not None)
            page.filter_check.invoke()  # (every value)
        dialog.maxsize(4000, 3000)
        dialog.wait_window = lambda *a: None
        dialog.show()
        pump(app, lambda: dialog.winfo_ismapped() and bar.winfo_manager() == "pack")
        dialog.update()
        width, height = dialog.winfo_width(), dialog.winfo_height()
        made = (panel.winfo_width(), panel.winfo_rootx() - dialog.winfo_rootx(), page.frame.winfo_reqwidth())
        book = dialog.book.winfo_width()
        assert made[2] <= book  # (at 100% the page fits the screen as made)

        def resize(w):
            dialog.geometry(f"{w}x{height}")
            pump(app, lambda: dialog.body.winfo_width() == w)
            for _ in range(3):
                dialog.update()

        def bar_shown(shown: bool):
            if key_ == "summary":  # (every line, or two)
                page.items = lines if shown else lines[:2]
                page.refresh()
            else:
                page.search_var.set("" if shown else word)
            pump(app, lambda: bool(bar.winfo_manager()) == shown, timeout=5)
            dialog.update()

        def as_made():
            assert panel.winfo_ismapped() and dialog.book.winfo_width() == book
            assert page.frame.winfo_reqwidth() == made[2]  # (as made, whatever the columns were stretched to)
            assert (panel.winfo_width(), panel.winfo_rootx() - dialog.winfo_rootx()) == made[:2]

        for first, then in ((False, True), (True, False)):
            resize(width + 300)
            assert dialog.book.winfo_width() == book + 300
            bar_shown(first)
            resize(width)
            as_made()
            bar_shown(then)
            as_made()
            resize(width + 300)
            bar_shown(not then)
            bar_shown(then)
            resize(width)
            as_made()
    finally:
        dialog.destroy()
        root.tk.call("tk", "scaling", scaling)


def test_files_page_script_required_and_no_option(root, tmp_path):
    """A .lua another script of the mod requires says so (read on another thread); a mod with options none of
    which is on says that nothing goes in; an unreadable record allows no change until 기록 지우기."""
    m = FakeManager()
    folder = make_tree(tmp_path / "s", {LUA: "return {}", MAIN: 'local c = require("NierUI.Config")\n'})
    mod = m.add(FakeMod("s", "Script"), None)
    m.snaps[mod.id] = snapshot(mod, [efile(LUA, path=folder / LUA, kind="스크립트"),
                                     efile(MAIN, path=folder / MAIN, kind="스크립트")])
    off = FakeMod("off", "Off", options=[{"id": "o1", "name": "A"}], selected=[])
    m.add(off, snapshot(off, [efile(HELM, options_of(off)[0])]))
    bad = FakeMod("bad", "Bad")
    m.add(bad, snapshot(bad, [efile(HELM)], record=model.Record(error="기록이 손상되었습니다 (x)")))
    app = make_app(root, m)
    dialog = window(app, mod.id, "files")
    try:
        page = dialog.pages["files"]
        page.tree.selection_set(LUA)
        pump(app, lambda: "다른 스크립트(NierUI.lua)가 이 파일을 불러옵니다." in page.p_notes.cget("text"))
        assert any(c[0] == "edit_snapshot" and c[-1] for c in m.calls)
        page.tree.selection_set(MAIN)
        app.root.update()
        assert "불러옵니다" not in page.p_notes.cget("text")
        dialog._set_mod(off.id)
        assert "켠 옵션이 없어 게임에 넣는 파일이 없습니다" in page.count.cget("text")
        assert page.tree.item(f"o1/{HELM}", "values")[3] == "옵션 꺼짐"
        dialog._set_mod(bad.id)
        page.tree.selection_set(HELM)
        app.root.update()
        assert "기록 지우기" in page.count.cget("text") and "disabled" in page.out_button.state()
        page.toggle()
        assert dialog.pending.count() == 0
    finally:
        dialog.destroy()


# ---- 게임 수치 / 스크립트 설정 -----------------------------------------------------------------------------------
def item_view(target, games: dict, n: int = 4, notes=(), hints=None, compared=True, lacking=(), unkeyed=()):
    """A ValueView of a synthetic ItemData (``n`` rows) read by the real engine, with the game's values given
    (``games``: address -> text, None for a value not paired; the rows of the groups in ``lacking`` the game does
    not have; the rest the same as the mod's unless ``compared`` is false). Its rows are paired with the game's by
    their _ItemId (keyed), but those of the groups in ``unkeyed`` (paired by their place only)."""
    doc = engines.get("rsz").open(mf.item_data(n), target.source.rel)
    rows = []
    for v in doc.values():
        game = None if v.group in lacking or not compared else games.get(v.at, v.text)
        rows.append(model.ValueRow(v, v.text, game, (hints or {}).get(v.rule_at, "")))
    base = model.BaseRef(Path(target.source.rel), target.key, "stored", target.source.rel)
    keyed = frozenset(r.value.at for r in rows if r.game is not None and r.value.group not in unkeyed)
    return model.ValueView(target, rows, len(rows), compared, False, list(notes), "", base,
                           added=frozenset(lacking) if compared else frozenset(), keyed=keyed)


def data_target(rel=ITEM, label="ItemData.user.3 · natives/STM/GameDesign/Common/Item", member="", note=""):
    return model.EditTarget(model.Source("", 0, rel), rel, member, "rsz", "data", label, 100, False, "", note)


@pytest.fixture
def data_env(root):
    m = FakeManager()
    mod = FakeMod("data", "Item Tweaks")
    m.add(mod, snapshot(mod, [efile(ITEM, kind="데이터")]))
    target = data_target()
    second = data_target(ITEM.replace("ItemData", "Other"), "Other.user.3 · natives/STM/GameDesign/Common/Item")
    m.targets[(mod.id, "data")] = [target, second]
    games = {SELL.format(100): "10", SELL.format(101): "20", SELL.format(102): "30", EAT.format(101): "true",
             "_Values[_ItemId=100]._Index": None}  # (a value not paired, as rszdata leaves _Index)
    m.views[(target.key, "")] = item_view(target, games, notes=["모드가 뺀 행 1개 (바꿀 수 없음)", "둘째 줄"],
                                          hints={"_Values[*]._Rare": "게임에 쓰인 값: 16, 17, 18",
                                                 "_Values[*]._Eatable": "게임에 쓰인 값: true, false"},
                                          lacking={"_Values[_ItemId=103]"})  # (a row the game does not have)
    m.views[(second.key, "")] = item_view(second, {}, n=2)
    app = make_app(root, m)
    return app, m, mod, target


def leaf(page, at):
    return next(iid for iid, i in page.iids.items() if page.rows[i].value.at == at)


def open_data(app, mod, page_key="data"):
    dialog = window(app, mod.id, page_key)
    page = dialog.pages[page_key]
    pump(app, lambda: page.ready_view is not None)
    return dialog, page


def test_values_page_fills_groups_with_the_mod_changed_values(data_env):
    """The file's values in groups (a table row each), with the game's and the mod's value; "모드가 바꾼 값만"
    on (compared): only the rows the mod changed and those the game does not have; the first note in the banner,
    the others under the table; the 파일 box lists the files of the tool, read on another thread."""
    app, m, mod, target = data_env
    dialog, page = open_data(app, mod)
    try:
        assert dialog.page_key == "data"
        assert [c[0] for c in m.calls if not c[-1]] == ["edit_targets", "open_values"]  # (on the worker)
        assert list(page.file_box.cget("values")) == [target.label, "Other.user.3 · natives/STM/GameDesign/Common/Item"]
        assert page.file_box.get() == target.label and app._tool_file[mod.id] == {"data": (ITEM, "")}
        assert page.filter_var.get() and page.banner.cget("text") == "모드가 뺀 행 1개 (바꿀 수 없음)"
        assert page.notes.cget("text") == "둘째 줄"
        tree = page.tree
        groups = tree.get_children()
        assert [tree.item(g, "text") for g in groups] == [f"_Values[_ItemId={i}]" for i in (100, 101, 102, 103)]
        shown_at = [page.rows[page.iids[c]].value.at for g in groups for c in tree.get_children(g)]
        assert shown_at[:4] == [SELL.format(100), SELL.format(101), EAT.format(101), SELL.format(102)]
        assert all(at.startswith("_Values[_ItemId=103]") for at in shown_at[4:])  # (the row the game lacks)
        row = leaf(page, SELL.format(100))
        assert tree.item(row, "text") == "_SellPrice" and tree.item(row, "values") == ("10", "30", "")
        assert tree.item(leaf(page, SELL.format(103)), "values")[:2] == ("없음", "120")  # (the game lacks the row)
        assert not tree.item(row, "tags")
        lacking = sum(1 for r in page.rows if r.value.group == "_Values[_ItemId=103]")
        changed = 4 + lacking  # (three prices and a bool, the row the game lacks)
        assert page.count.cget("text") == f"값 {len(page.rows)}개 · 모드가 바꾼 값 {changed}개 · 보이는 값 {changed}개"
        pick(app, tree, leaf(page, SELL.format(103)))
        assert page.panel.game.cget("text") == "게임 값: 없음 (게임에 없는 행)"
        # every value with the filter off; a value not paired: no game value, and the panel says it is not known
        page.filter_check.invoke()
        assert len(page.iids) == len(page.rows)
        index = leaf(page, "_Values[_ItemId=100]._Index")
        assert tree.item(index, "values") == ("", "0", "")
        pick(app, tree, index)
        assert page.panel.game.cget("text") == "게임 값: 알 수 없음 (게임 파일에서 짝을 찾지 못한 값)"
        # the search narrows it; Return in it goes to the first value (not its group's row)
        page.search_var.set("_eatable")
        pump(app, lambda: len(page.iids) == 4)
        assert all(page.rows[i].value.label == "_Eatable" for i in page.iids.values())
        assert "보이는 값 4개" in page.count.cget("text")
        key(app, page.search, "<Return>")
        first = leaf(page, EAT.format(100))
        assert str(tree.tk.call("focus")) == str(tree) and tree.selection() == (first,) and tree.focus() == first
        assert page.panel.mode == "one" and not dialog.closed
        # the other file: read when picked, kept while the window is open
        page.file_box.current(1)
        page.file_box.event_generate("<<ComboboxSelected>>")
        pump(app, lambda: page.target.key != ITEM and page.ready_view is not None)
        page.file_box.current(0)
        page.file_box.event_generate("<<ComboboxSelected>>")
        app.root.update()
        assert page.ready_view is not None and [c[0] for c in m.calls].count("open_values") == 2
    finally:
        dialog.destroy()


def test_values_page_drops_results_for_another_mod_or_file(data_env):
    """A result for a mod picked before is dropped by the window; one for a file no longer shown is kept for
    later but not shown."""
    app, m, mod, target = data_env
    dialog, page = open_data(app, mod)
    try:
        other = page.targets[1]
        ident = (other.key, other.member)
        view = page.views[(target.key, "")]
        dialog._done("values", ident, view, dialog._generation - 1, "data")
        assert ident not in page.views
        dialog._done("values", ident, view, dialog._generation, "data")
        assert page.views[ident] is view and page.target == target and page.ready_view.view.target == target
    finally:
        dialog.destroy()


def test_values_panel_applies_says_why_and_reverts(data_env):
    """The panel: the value's kind and range, the mod's and the game's value; a value it cannot read says why
    under it (nothing changed); one it can is an unsaved change (● on the row, the window's count), Return in the
    editor goes back to the table; 원래 값으로 takes it back."""
    app, m, mod, target = data_env
    dialog, page = open_data(app, mod)
    try:
        panel, tree = page.panel, page.tree
        row = leaf(page, SELL.format(101))
        tree.selection_set(row)
        tree.focus(row)
        app.root.update()
        assert panel.mode == "one" and panel.name.cget("text") == "_SellPrice"
        assert panel.kind.cget("text") == "정수 (S32) · -2147483648 ~ 2147483647"
        assert panel.base.cget("text") == "모드의 원래 값: 60" and panel.game.cget("text") == "게임 값: 20"
        assert panel.value_var.get() == "60" and "disabled" in panel.revert_button.state()
        assert "종류를 나타내는 번호" in panel.int_caution.cget("text")
        assert "온라인" in panel.online_caution.cget("text")
        key(app, tree, "<Return>")
        assert str(tree.tk.call("focus")) == str(panel.entry) and not dialog.closed
        panel.value_var.set("abc")
        key(app, panel.entry, "<Return>")
        assert panel.error.cget("text") == "정수를 적어 주세요." and dialog.pending.count() == 0 and not dialog.closed
        key(app, panel.entry, "<Escape>")
        assert panel.value_var.get() == "60" and panel.error.cget("text") == "" and not dialog.closed
        key(app, panel.entry, "<Escape>")
        assert str(tree.tk.call("focus")) == str(tree) and not dialog.closed
        tree.selection_set(row)
        app.root.update()
        panel.value_var.set("5000")
        key(app, panel.entry, "<Return>")
        mp = dialog.pending.member(target.source, "")
        assert [(c.at, c.before, c.after, c.kind) for c in mp.changes] == [(SELL.format(101), "60", "5000", "S32")]
        assert mp.engine == "rsz" and mp.label == "ItemData.user.3"
        assert tree.item(row, "values")[2] == "● 5000" and "changed" in tree.item(row, "tags")
        assert str(tree.tk.call("focus")) == str(tree) and dialog.pending.count() == 1
        assert page.file_box.get().endswith(" (수정함)")
        assert "disabled" not in panel.revert_button.state()
        # a value outside those the game uses in that column: a warning
        page.filter_check.invoke()
        pick(app, tree, leaf(page, "_Values[_ItemId=101]._Rare"))
        assert panel.hint.cget("text") == "게임에 쓰인 값: 16, 17, 18" and panel.warn.cget("text") == ""
        panel.value_var.set("99")
        panel.apply_button.invoke()
        assert panel.warn.cget("text") == "게임에 쓰인 값에 없는 번호입니다."
        pick(app, tree, leaf(page, SELL.format(101)))
        panel.revert_button.invoke()
        mp = dialog.pending.member(target.source, "")
        assert [c.at for c in mp.changes] == ["_Values[_ItemId=101]._Rare"]
        key(app, tree, "<Delete>")  # (nothing to take back on this row)
        pick(app, tree, leaf(page, "_Values[_ItemId=101]._Rare"))
        key(app, tree, "<Delete>")
        assert dialog.pending.count() == 0 and not dialog.closed
    finally:
        dialog.destroy()


def test_values_rule_box_counts_and_reverting_a_rule_row_takes_the_rule(data_env):
    """The rule box says how many rows of the table have the value; ticked, the value is a rule for all of them
    (every row marked); 원래 값으로 on a row the rule changed takes the whole rule back (the panel says so first)."""
    app, m, mod, target = data_env
    games = {SELL.format(100 + i): "1" for i in range(4)}
    rows = [mf.item_row(i, _SellPrice=99999 if i != 2 else 5) for i in range(4)]
    doc = engines.get("rsz").open(mf.item_data(rows=rows), ITEM)
    view = item_view(target, games)
    view.rows = [model.ValueRow(v, v.text, games.get(v.at, v.text)) for v in doc.values()]
    m.views[(ITEM, "")] = view
    dialog, page = open_data(app, mod)
    try:
        panel, tree = page.panel, page.tree
        tree.selection_set(leaf(page, SELL.format(100)))
        app.root.update()
        assert panel.rule.winfo_manager() == "pack"
        assert panel.rule.cget("text").strip() == "이 표에서 값이 99999인 행 3개 모두" and not panel.rule_var.get()
        panel.rule_var.set(True)
        panel.value_var.set("5000")
        panel.apply_button.invoke()
        mp = dialog.pending.member(target.source, "")
        assert [(c.at, c.before, c.after) for c in mp.changes] == [("_Values[*]._SellPrice", "99999", "5000")]
        marked = {page.rows[page.iids[i]].value.at for i in page.iids if tree.item(i, "values")[2] == "● 5000"}
        assert marked == {SELL.format(i) for i in (100, 101, 103)}
        assert dialog.pending.count() == 1
        tree.selection_set(leaf(page, SELL.format(101)))
        app.root.update()
        assert panel.rule_var.get() and "'값이 99999인 행 모두 → 5000' 규칙으로 바뀌었습니다" in panel.note.cget("text")
        assert panel.value_var.get() == "5000"
        panel.revert_button.invoke()
        assert dialog.pending.member(target.source, "") is None and dialog.pending.count() == 0
        assert not any(tree.item(i, "values")[2] for i in page.iids)
    finally:
        dialog.destroy()


def test_values_bulk_set_times_and_change(data_env):
    """Several values (or a group row): the bulk editor with the count of numbers; 게임 값의 몇 배 and 모드가 바꾼 만큼의
    몇 배 from the game's value (rows without one skipped, bools only by 값 정하기), integers rounded half away from
    zero and brought into their range, all said under it; ``how`` recorded."""
    app, m, mod, target = data_env
    dialog, page = open_data(app, mod)
    try:
        panel, tree = page.panel, page.tree
        page.filter_check.invoke()  # (every value)
        chosen = [leaf(page, SELL.format(100 + i)) for i in range(4)] + [leaf(page, EAT.format(100))]
        tree.selection_set(chosen)
        app.root.update()
        assert panel.mode == "many" and panel.many_title.cget("text") == "고른 값 5개 (숫자 4개)"
        assert list(panel.mode_box.cget("values")) == ["값 정하기", "게임 값의 몇 배", "모드가 바꾼 만큼의 몇 배"]
        panel.mode_box.current(1)
        panel.bulk_var.set("x")
        panel.bulk_button.invoke()
        assert panel.bulk_error.cget("text") == "숫자를 적어 주세요." and dialog.pending.count() == 0
        panel.bulk_var.set("2")
        key(app, panel.bulk_entry, "<Return>")
        mp = dialog.pending.member(target.source, "")
        assert {c.at: c.after for c in mp.changes} == {SELL.format(100): "20", SELL.format(101): "40",
                                                      SELL.format(102): "60"}
        assert mp.changes[0].how == {"mode": "times", "k": 2.0, "game": "10"}
        result = panel.bulk_result.cget("text")
        assert result.startswith("값 3개를 바꿨습니다") and "게임 값이 없어 건너뛴 값 1개" in result
        assert "건너뛴 값 1개 (켜기·끄기 값은 '값 정하기'로만 바꿀 수 있습니다)" in result and not dialog.closed
        panel.mode_box.current(2)
        panel.bulk_var.set("0.5")
        panel.bulk_button.invoke()
        changes = {c.at: c.after for c in dialog.pending.member(target.source, "").changes}
        assert changes[SELL.format(100)] == "20" and changes[SELL.format(101)] == "40"  # game + 0.5 * (mod - game)
        panel.mode_box.current(0)
        panel.bulk_var.set("99999999999")
        panel.bulk_button.invoke()
        changes = {c.at: c.after for c in dialog.pending.member(target.source, "").changes}
        assert changes[SELL.format(103)] == "2147483647" and "범위를 넘어 맞춘 값 4개" in panel.bulk_result.cget("text")
        # (a reason that is a sentence: in brackets after the count)
        assert panel.bulk_result.cget("text") == ("값 4개를 바꿨습니다 · 건너뛴 값 1개 (켬이나 끔(true/false)이어야 합니다) · "
                                                  "범위를 넘어 맞춘 값 4개")
        # a group row: its values
        group = tree.parent(leaf(page, SELL.format(102)))
        tree.selection_set(group)
        app.root.update()
        assert panel.mode == "many" and panel.many_title.cget("text").startswith("고른 값 ")
        key(app, tree, "<F2>")
        assert str(tree.tk.call("focus")) == str(panel.bulk_entry) and not dialog.closed
        key(app, panel.bulk_entry, "<Escape>")
        key(app, panel.bulk_entry, "<Escape>")
        assert str(tree.tk.call("focus")) == str(tree) and not dialog.closed
        open_before = tree.item(group, "open")
        key(app, tree, "<Return>")
        assert bool(tree.item(group, "open")) != bool(open_before) and not dialog.closed
    finally:
        dialog.destroy()


@pytest.mark.parametrize("mode", [0, 1, 2], ids=["set", "times", "change"])
def test_values_bulk_refuses_a_decimal_comma(data_env, mode):
    """한꺼번에 적용's value with a comma that is not between thousands (0,5: a decimal comma) is refused under it,
    as engines.plain_number says (the command line's rule), not read as 5; 1,000 is a thousand."""
    from mhwmm.modedit import EditError

    app, m, mod, target = data_env
    dialog, page = open_data(app, mod)
    try:
        panel, tree = page.panel, page.tree
        page.filter_check.invoke()  # (every value)
        tree.selection_set([leaf(page, SELL.format(100 + i)) for i in range(3)])
        app.root.update()
        panel.mode_box.current(mode)
        for text in ("0,5", "1,5", ",5", "5,", "1,00"):
            with pytest.raises(EditError) as refused:
                engines.plain_number(text)
            panel.bulk_var.set(text)
            panel.bulk_button.invoke()
            assert panel.bulk_error.cget("text") == str(refused.value)
            assert dialog.pending.count() == 0 and panel.bulk_result.cget("text") == ""
        panel.bulk_var.set("1,000")
        panel.bulk_button.invoke()
        changes = {c.at: c.after for c in dialog.pending.member(target.source, "").changes}
        assert panel.bulk_error.cget("text") == ""
        assert [changes[SELL.format(100 + i)] for i in range(3)] == [
            ["1000"] * 3, ["10000", "20000", "30000"], ["20010", "40020", "60030"]][mode]
    finally:
        dialog.destroy()


def test_values_table_keys_go_to_the_editor(data_env):
    """F2 and a double click on a value go to the panel's editor with its text selected (Return too: above);
    from the editor Escape goes back; the window stays open."""
    app, m, mod, target = data_env
    dialog, page = open_data(app, mod)
    try:
        tree, panel = page.tree, page.panel
        row = leaf(page, SELL.format(102))
        pick(app, tree, row)
        tree.focus(row)
        key(app, tree, "<F2>")
        assert str(tree.tk.call("focus")) == str(panel.entry) and panel.entry.selection_present()
        key(app, panel.entry, "<Escape>")
        assert str(tree.tk.call("focus")) == str(tree)
        tree.see(row)
        app.root.update()
        x, y, w, h = tree.bbox(row, "#0")
        double_click(app, tree, x + w // 2, y + h // 2)
        assert str(tree.tk.call("focus")) == str(panel.entry) and tree.selection() == (row,) and not dialog.closed
    finally:
        dialog.destroy()


def test_values_bulk_without_the_game_offers_only_set(data_env):
    app, m, mod, target = data_env
    m.views[(ITEM, "")] = item_view(target, {}, compared=False, notes=[rszdata.NO_GAME])
    dialog, page = open_data(app, mod)
    try:
        assert not page.filter_var.get() and "disabled" in page.filter_check.state()
        assert page.banner.cget("text") == rszdata.NO_GAME and len(page.iids) == len(page.rows)
        page.tree.selection_set([leaf(page, SELL.format(100)), leaf(page, SELL.format(101))])
        app.root.update()
        assert list(page.panel.mode_box.cget("values")) == ["값 정하기"]
        page.tree.selection_set(leaf(page, SELL.format(100)))
        app.root.update()
        assert page.panel.game.cget("text") == "게임 값: 없음"
    finally:
        dialog.destroy()


def test_values_read_only_rows_and_files(data_env):
    """A value that cannot be changed: muted, its reason as the row's tooltip and in the panel, the editor off; a
    file that cannot be changed: the reason in the banner and no rows."""
    app, m, mod, target = data_env
    dialog, page = open_data(app, mod)
    try:
        page.filter_check.invoke()
        row = leaf(page, "_Values[_ItemId=100]._RawName")
        assert "readonly" in page.tree.item(row, "tags")
        assert page._tip(row) == "이 종류의 값은 아직 바꿀 수 없습니다"
        page.tree.selection_set(row)
        app.root.update()
        panel = page.panel
        assert "disabled" in panel.entry.state() and "disabled" in panel.apply_button.state()
        assert "이 종류의 값은 아직 바꿀 수 없습니다" in panel.note.cget("text")
        page.apply_value()
        assert dialog.pending.count() == 0 and panel.error.cget("text") == "이 종류의 값은 아직 바꿀 수 없습니다"
    finally:
        dialog.destroy()
    stale = "지금 게임과 형식이 달라 읽지 못했습니다. 재질 점검에서 복구한 뒤에 바꿀 수 있습니다."
    m.views[(ITEM, "")] = model.ValueView(target, [], 0, False, False, [], stale,
                                          model.BaseRef(Path(ITEM), ITEM, "stored", ITEM))
    dialog, page = open_data(app, mod)
    try:
        assert page.banner.cget("text") == stale and not page.tree.get_children()
        assert page.panel.none_label.cget("text") == stale
    finally:
        dialog.destroy()


def test_values_conflicts_keep_mine_take_author_and_gone(data_env):
    """Values an update left to check: ✕ on the row and the author's new value in the panel with 내 값 유지 /
    작성자 값 쓰기; one whose value is gone has a row of its own (first) with 지우기."""
    app, m, mod, target = data_env
    changed = model.Conflict(model.Change(SELL.format(102), "S32", "1", "100", "_SellPrice"), "90", "changed")
    gone = model.Conflict(model.Change(SELL.format(109), "S32", "1", "100", "_SellPrice"), None, "gone")
    record = model.Record(rev=3, items=[model.Item(target.source, members=[
        model.Member("", "rsz", "ItemData", [], [changed, gone])])])
    m.snaps[mod.id] = snapshot(mod, [efile(ITEM)], record=record)
    dialog, page = open_data(app, mod)
    try:
        panel, tree = page.panel, page.tree
        first = tree.get_children()[0]
        assert tree.item(first, "text") == "업데이트 뒤 확인할 값"
        stand = tree.get_children(first)[0]
        assert tree.item(stand, "values")[2] == "✕ 100" and "conflict" in tree.item(stand, "tags")
        row = leaf(page, SELL.format(102))
        assert tree.item(row, "values")[2] == "✕ 100"
        tree.selection_set(row)
        app.root.update()
        assert panel.conflict.winfo_manager() == "pack"
        assert panel.conflict_text.cget("text") == ("모드가 업데이트되어 작성자의 값이 1에서 90(으)로 바뀌었습니다. "
                                                    "내 값: 100")
        panel.keep_button.invoke()
        mp = dialog.pending.member(target.source, "")
        assert [(c.at, c.before, c.after) for c in mp.changes] == [(SELL.format(102), "90", "100")]
        assert tree.item(row, "values")[2] == "● 100" and panel.conflict.winfo_manager() == ""
        tree.selection_set(stand)
        app.root.update()
        assert panel.conflict_text.cget("text") == "업데이트로 이 값이 없어졌습니다 (내 값: 100)"
        assert panel.take_button.cget("text") == "지우기" and panel.keep_button.winfo_manager() == ""
        panel.take_button.invoke()
        assert dialog.pending.member(target.source, "").conflicts == ()
        assert tree.item(stand, "values")[2] == "" and dialog.pending.count() == 2
    finally:
        dialog.destroy()


def test_values_space_toggles_a_bool(data_env):
    app, m, mod, target = data_env
    dialog, page = open_data(app, mod)
    try:
        page.filter_check.invoke()
        row = leaf(page, EAT.format(100))
        page.tree.selection_set(row)
        app.root.update()
        assert page.panel.check.winfo_manager() == "pack" and page.panel.kind.cget("text") == "켜기·끄기"
        assert page.panel.hint.cget("text") == "게임에 쓰인 값: 켬, 끔"
        assert page.tree.item(row, "values")[1] == "켬"
        key(app, page.tree, "<space>")
        mp = dialog.pending.member(target.source, "")
        assert [(c.before, c.after) for c in mp.changes] == [("true", "false")] and not dialog.closed
        assert page.tree.item(row, "values")[2] == "● 끔" and not page.panel.check_var.get()
        key(app, page.tree, "<space>")
        assert dialog.pending.count() == 0
        page.panel.check.invoke()  # (the box applies at once)
        assert dialog.pending.count() == 1
    finally:
        dialog.destroy()


LUA_MIXED = b"""\
local helper = 5
local Config = {}
Config.Scale = 1.0 -- size
Config.Enabled = true
"""


def test_values_bulk_game_modes_only_on_rows_paired_by_a_key(data_env):
    """게임 값의 몇 배 / 모드가 바꾼 만큼의 몇 배 start only from the game values of rows paired with the game's by a
    key (view.keyed): a row paired by its place only may have another row's game value (the mod put its rows in
    another order). Those are skipped and counted; 값 정하기 is not."""
    app, m, mod, target = data_env
    games = {SELL.format(100 + i): str(10 * (i + 1)) for i in range(4)}
    m.views[(ITEM, "")] = item_view(target, games, unkeyed={"_Values[_ItemId=102]", "_Values[_ItemId=103]"})
    dialog, page = open_data(app, mod)
    try:
        panel, tree = page.panel, page.tree
        tree.selection_set([leaf(page, SELL.format(100 + i)) for i in range(4)])
        app.root.update()
        panel.mode_box.current(1)
        panel.bulk_var.set("2")
        panel.bulk_button.invoke()
        changes = {c.at: c.after for c in dialog.pending.member(target.source, "").changes}
        assert changes == {SELL.format(100): "20", SELL.format(101): "40"}
        assert panel.bulk_result.cget("text") == "값 2개를 바꿨습니다 · 게임과 행을 맞추지 못해 건너뛴 값 2개"
        panel.mode_box.current(2)
        panel.bulk_var.set("0.5")
        panel.bulk_button.invoke()
        assert "게임과 행을 맞추지 못해 건너뛴 값 2개" in panel.bulk_result.cget("text")
        assert set(c.at for c in dialog.pending.member(target.source, "").changes) == set(changes)
        panel.mode_box.current(0)
        panel.bulk_var.set("5")
        panel.bulk_button.invoke()
        assert {c.at: c.after for c in dialog.pending.member(target.source, "").changes} == {
            SELL.format(100 + i): "5" for i in range(4)}
        assert panel.bulk_result.cget("text") == "값 4개를 바꿨습니다"
        # only rows paired by their place: nothing changed, and why
        tree.selection_set([leaf(page, SELL.format(102)), leaf(page, SELL.format(103))])
        app.root.update()
        panel.mode_box.current(1)
        panel.bulk_var.set("3")
        panel.bulk_button.invoke()
        assert panel.bulk_error.cget("text") == "바꾼 값이 없습니다: 게임과 행을 맞추지 못해 건너뛴 값"
    finally:
        dialog.destroy()


def test_values_typed_but_not_applied_are_not_dropped(data_env, answers):
    """A value typed in the panel and not applied (Return or 적용 not pressed): 저장 and Ctrl+S ask first (예: applied
    and saved, 아니요: saved without it, 취소: the window stays), one that cannot be read keeps the window open on it
    with why; giving the changes up counts it; another page's change keeps it in its panel; 한꺼번에 적용's value
    too."""
    app, m, mod, target = data_env
    dialog, page = open_data(app, mod)
    try:
        panel, tree = page.panel, page.tree
        pick(app, tree, leaf(page, SELL.format(101)))
        panel.value_var.set("5000")
        key(app, panel.entry, "<Return>")  # (applied)
        assert dialog.pending.count() == 1
        pick(app, tree, leaf(page, SELL.format(102)))
        focus(app, panel.entry)
        panel.value_var.set("777")  # (typed: not applied)
        question = (f"적용하지 않은 값이 있습니다:\n• {SELL.format(102)} = 777\n\n예: 적용하고 저장합니다\n"
                    "아니요: 적용하지 않은 값은 빼고 저장합니다")
        answers.replies = [None]  # 취소
        key(app, panel.entry, "<Control-s>")
        assert not dialog.closed and answers[-1] == ("모드 수정", question) and dialog.pending.count() == 1
        assert page.unapplied() == f"{SELL.format(102)} = 777"
        page.refresh()  # (another page changed the unsaved changes)
        assert panel.value_var.get() == "777" and page.unapplied() == f"{SELL.format(102)} = 777"
        key(app, tree, "<Escape>")  # (취소 of the window: the typed value counts; 아니요: it stays)
        assert answers[-1][1] == f"저장하지 않은 수정 2개를 버릴까요?\n\n적용하지 않은 값:\n• {SELL.format(102)} = 777"
        assert not dialog.closed
        panel.value_var.set("abc")
        answers.replies = [True]
        dialog.ok()  # (저장: 예, but it cannot be read)
        assert not dialog.closed and panel.error.cget("text") == "정수를 적어 주세요." and dialog.pending.count() == 1
        pump(app, lambda: str(tree.tk.call("focus")) == str(panel.entry))  # (the keyboard on it)
        panel.value_var.set("777")
        answers.replies = [True]
        key(app, panel.entry, "<Control-s>")
        assert dialog.closed
        changes = {c.at: c.after for mp in dialog.result.plan.members.values() for c in mp.changes}
        assert changes == {SELL.format(101): "5000", SELL.format(102): "777"}
    finally:
        if not dialog.closed:
            dialog.destroy()
    dialog, page = open_data(app, mod)
    try:
        panel, tree = page.panel, page.tree
        pick(app, tree, leaf(page, SELL.format(101)))
        panel.value_var.set("5000")
        panel.apply_button.invoke()
        tree.selection_set([leaf(page, SELL.format(100)), leaf(page, SELL.format(102))])
        app.root.update()
        panel.mode_box.current(1)
        panel.mode_box.event_generate("<<ComboboxSelected>>")
        panel.bulk_var.set("2")  # (한꺼번에 적용 not pressed)
        assert page.unapplied() == "게임 값의 몇 배 2 (고른 값 2개)"
        answers.replies = [False]  # 아니요: saved without it
        dialog.ok()
        assert dialog.closed and answers[-1][1].startswith("적용하지 않은 값이 있습니다:\n• 게임 값의 몇 배 2 (고른 값 2개)")
        changes = {c.at: c.after for mp in dialog.result.plan.members.values() for c in mp.changes}
        assert changes == {SELL.format(101): "5000"}
    finally:
        if not dialog.closed:
            dialog.destroy()
    dialog, page = open_data(app, mod)
    try:
        panel, tree = page.panel, page.tree
        tree.selection_set([leaf(page, SELL.format(100)), leaf(page, SELL.format(102))])
        app.root.update()
        panel.mode_box.current(1)
        panel.bulk_var.set("2")
        panel.bulk_button.invoke()  # (applied: nothing to ask, the value left in its box)
        assert dialog.pending.count() == 2 and page.unapplied() == ""
        asked = len(answers)
        dialog.ok()
        assert dialog.closed and len(answers) == asked
    finally:
        if not dialog.closed:
            dialog.destroy()


def test_values_typed_with_nothing_else_to_save_and_before_a_new_mod(data_env, answers, monkeypatch):
    """A value typed but not applied with nothing else to save: 저장 asks only whether to apply it and save
    (아니요: the window stays with it; without it nothing would be saved). 새 모드 만들기 on 다른 장비로 옮기기 asks
    first too: the new mod is made from the mod as it is, so the value would be gone with the window (아니요: the
    window stays; 예: the new mod)."""
    app, m, mod, target = data_env
    typed = f"적용하지 않은 값이 있습니다:\n• {SELL.format(102)} = 777\n\n"
    dialog, page = open_data(app, mod)
    try:
        panel, tree = page.panel, page.tree
        pick(app, tree, leaf(page, SELL.format(102)))
        focus(app, panel.entry)
        panel.value_var.set("777")
        key(app, panel.entry, "<Control-s>")  # (아니요)
        assert answers[-1] == ("모드 수정", typed + "적용하고 저장할까요?")
        assert not dialog.closed and panel.value_var.get() == "777" and dialog.pending.count() == 0
        retarget = dialog.pages["retarget"]
        monkeypatch.setattr(retarget, "ready", lambda: True)
        monkeypatch.setattr(retarget, "result", lambda: "새 모드")
        dialog.select("retarget", force=True)
        asked = len(answers)
        dialog.ok()  # (아니요)
        assert len(answers) == asked + 1 and not dialog.closed and dialog.result is None
        assert answers[-1] == ("모드 수정", typed + "새 모드에는 이 값이 들어가지 않습니다. 이 값은 버리고 새 모드를 만들까요?")
        answers.replies = [True]
        dialog.ok()
        assert dialog.closed and dialog.result == "새 모드"
    finally:
        if not dialog.closed:
            dialog.destroy()
    dialog, page = open_data(app, mod)
    try:
        panel, tree = page.panel, page.tree
        pick(app, tree, leaf(page, SELL.format(102)))
        focus(app, panel.entry)
        panel.value_var.set("777")
        answers.replies = [True]  # (예: applied and saved)
        key(app, panel.entry, "<Control-s>")
        assert dialog.closed and answers[-1] == ("모드 수정", typed + "적용하고 저장할까요?")
        changes = {c.at: c.after for mp in dialog.result.plan.members.values() for c in mp.changes}
        assert changes == {SELL.format(102): "777"}
    finally:
        if not dialog.closed:
            dialog.destroy()


def test_config_page_settings_filter_and_saved_settings_note(root):
    """스크립트 설정 of a Lua file: "설정 같은 값만" on (helper values hidden), the note where the script keeps its
    saved settings (in the banner), the column 모드의 값; a value set keeps the literal it was made on."""
    m = FakeManager()
    mod = FakeMod("cfg", "NierUI")
    m.add(mod, snapshot(mod, [efile(LUA, kind="스크립트")]))
    target = model.EditTarget(model.Source("", 0, LUA), LUA, "", "lua", "config", "Config.lua · reframework/autorun/NierUI",
                              len(LUA_MIXED), False)
    m.targets[(mod.id, "config")] = [target]
    doc = engines.get("lua").open(LUA_MIXED + mf.LUA_RUNTIME, LUA)
    rows = [model.ValueRow(v, v.text, None) for v in doc.values()]
    m.views[(LUA, "")] = model.ValueView(target, rows, len(rows), False, False, list(doc.notes), "",
                                         model.BaseRef(Path(LUA), LUA, "stored", LUA), list(doc.data_files))
    app = make_app(root, m)
    dialog = window(app, mod.id, "config")
    page = dialog.pages["config"]
    try:
        pump(app, lambda: page.ready_view is not None)
        assert page.filter_var.get() and str(page.tree.heading("mod", "text")) == "모드의 값"
        labels = [page.rows[i].value.at for i in page.iids.values()]
        assert "helper" not in labels and "Config.Scale" in labels
        assert page.banner.cget("text").startswith("이 스크립트는 설정을 게임 폴더의 reframework/data/MyMod/config.json에")
        assert "⚠ 표시는" in page.notes.cget("text")
        warned = leaf(page, "cfg.enabled")
        assert page.tree.item(warned, "text").startswith("⚠ ")
        page.filter_check.invoke()
        assert "helper" in [page.rows[i].value.at for i in page.iids.values()]
        row = leaf(page, "Config.Scale")
        page.tree.selection_set(row)
        app.root.update()
        panel = page.panel
        assert panel.kind.cget("text") == "실수" and panel.game.winfo_manager() == ""
        assert panel.rule.winfo_manager() == "" and panel.online_caution.cget("text") == ""
        assert panel.note.cget("text") == "size"
        panel.value_var.set("1.25")
        panel.apply_button.invoke()
        mp = dialog.pending.member(target.source, "")
        assert [(c.at, c.kind, c.before, c.after, c.raw) for c in mp.changes] == [
            ("Config.Scale", "float", "1.0", "1.25", "1.0")]
        assert mp.engine == "lua" and page.count.cget("text").startswith("값 ") and "바꾼 값 1개" in page.count.cget("text")
    finally:
        dialog.destroy()


def test_values_page_without_files_and_failures(data_env):
    """No file the tool can change, a list that could not be read, a file that could not be read: said in the
    banner."""
    app, m, mod, target = data_env
    m.targets[(mod.id, "data")] = []
    dialog = window(app, mod.id, "data")
    page = dialog.pages["data"]
    try:
        pump(app, lambda: page.targets is not None)
        assert page.banner.cget("text") == "고칠 수 있는 데이터 파일이 없습니다." and page.file_var.get() == ""
    finally:
        dialog.destroy()
    m.targets[(mod.id, "data")] = [target]
    from mhwmm.modedit import EditError

    m.views[(ITEM, "")] = EditError("망가진 파일")
    dialog = window(app, mod.id, "data")
    page = dialog.pages["data"]
    try:
        pump(app, lambda: page.banner.cget("text").startswith("값을 읽지"))
        assert page.banner.cget("text") == "값을 읽지 못했습니다: 망가진 파일" and not page.tree.get_children()
    finally:
        dialog.destroy()


def test_a_pak_without_data_files_gives_way_to_the_next_tool(root):
    """게임 수치 chosen by the window for a PAK mod (its PAK may hold data files) whose PAK has none, once read: the
    tool is off with why, and the window shows the next one that fits; asked for by name, or picked, it stays."""
    m = FakeManager()
    mod = FakeMod("pak", "Armor Look")
    m.add(mod, snapshot(mod, [efile("ArmorLook.pak", kind="PAK")]))
    app = make_app(root, m)
    dialog = window(app, mod.id, None)
    try:
        assert dialog.page_key == "data"  # (a PAK: it may hold data files)
        pump(app, lambda: dialog.page_key == "files")
        assert dialog.tips["data"].text() == "고칠 수 있는 데이터 파일(.user)이 없는 모드입니다"
        assert "disabled" in dialog.nav["data"].state()
        assert dialog.pages["data"].banner.cget("text") == "고칠 수 있는 데이터 파일이 없습니다."
        dialog.step(1)  # (Ctrl+Tab skips it now)
        assert dialog.page_key == "summary"
    finally:
        dialog.destroy()
    dialog = window(app, mod.id, "data")  # (asked for by name)
    try:
        page = dialog.pages["data"]
        pump(app, lambda: page.targets == [])
        app.root.update()
        assert dialog.page_key == "data" and "disabled" in dialog.nav["data"].state()
    finally:
        dialog.destroy()
    dialog = window(app, mod.id, "files")
    try:
        dialog.nav["data"].invoke()  # (picked)
        pump(app, lambda: dialog.pages["data"].targets == [])
        app.root.update()
        assert dialog.page_key == "data" and dialog.picked == "data"
    finally:
        dialog.destroy()
    # picked on another mod (a data mod), then this one picked in the 모드 box: it was not picked for this mod
    data = m.add(FakeMod("data", "Item Tweaks"), snapshot(FakeMod("data", "Item Tweaks"), [efile(ITEM, kind="데이터")]))
    target = data_target()
    m.targets[(data.id, "data")] = [target]
    m.views[(target.key, "")] = item_view(target, {SELL.format(100): "10"})
    dialog = window(app, data.id, "files")
    try:
        dialog.nav["data"].invoke()
        pump(app, lambda: dialog.pages["data"].ready_view is not None)
        dialog._pick_mod(dialog.mod_ids.index(mod.id))
        assert dialog.mod_id == mod.id and dialog.page_key == "data"  # (a PAK: it may hold data files)
        pump(app, lambda: dialog.page_key == "files")
        assert "disabled" in dialog.nav["data"].state() and dialog.picked == "data"
        dialog._pick_mod(dialog.mod_ids.index(data.id))  # (back: 게임 수치 works on it again)
        assert dialog.page_key == "files" and "disabled" not in dialog.nav["data"].state()
    finally:
        dialog.destroy()


# ---- 수정 내용 ----------------------------------------------------------------------------------------------------
@pytest.fixture
def summary_env(root):
    m = FakeManager()
    mod = FakeMod("sum", "Mixed", options=[{"id": "o1", "name": "Blue"}], selected=["o1"])
    blue = options_of(mod)[0]
    rule = model.Change("_Values[*]._SellPrice", "S32", "99999", "5000", "_SellPrice", count=412)
    single = model.Change(SELL.format(101), "S32", "1", "100", "_BuyPrice")
    conflict = model.Conflict(model.Change(SELL.format(102), "S32", "1", "100", "_BuyPrice"), "50", "changed")
    data_src, out_src, lua_src = (model.Source("Blue", 0, ITEM), model.Source("Blue", 0, HELM),
                                  model.Source("Blue", 0, LUA))
    lua_change = model.Change("Config.UI_Scale", "float", "1.0", "1.25", "UI_Scale", raw="1.0")
    record = model.Record(rev=2, items=[
        model.Item(out_src, exclude=True),
        model.Item(data_src, members=[model.Member("", "rsz", "ItemData", [rule, single], [conflict])]),
        model.Item(lua_src, members=[model.Member("", "lua", "Config.lua", [lua_change], [], "지금 게임과 형식이 달라")])],
        notice={"kind": "update", "at": "2026-10-28T21:14:05", "kept": 3, "reapplied": 1, "conflicts": 1,
                "pending": 0, "dropped": []})
    files = [efile(ITEM, blue), efile(HELM, blue, state="excluded"), efile(LUA, blue), efile(TEX, blue)]
    m.add(mod, snapshot(mod, files, record=record, issues=["확인할 값 1개"]))
    m.items[mod.id] = [
        model.EditItemView("exclude", out_src, "", HELM, (), (), "active"),
        model.EditItemView("data", data_src, "", "ItemData", (rule, single), (conflict,), "conflict"),
        model.EditItemView("config", lua_src, "", "Config.lua", (lua_change,), (), "pending", "지금 게임과 형식이 달라"),
    ]
    app = make_app(root, m)
    return app, m, mod, (out_src, data_src, lua_src), (rule, single, conflict, lua_change)


def summary_rows(page):
    tree = page.tree
    out = []
    for iid in tree.get_children():
        out.append(tuple(tree.item(iid, "values")))
        out += [("", *tree.item(c, "values")[1:2]) for c in tree.get_children(iid)]
    return out


def test_summary_page_lists_edits_and_drops_the_notice_once(summary_env):
    """Every edit with its state and a row per value (a rule with its rows), the update's report on top (dropped
    from the record once, when the page is shown); 기록 지우기 only for a record that cannot be read."""
    app, m, mod, sources, changes = summary_env
    dialog = window(app, mod.id, None)  # (opens on 수정 내용: the mod has issues)
    page = dialog.pages["summary"]
    try:
        assert dialog.page_key == "summary"
        assert summary_rows(page) == [
            ("뺌", HELM, "Blue", "적용 중"),
            ("수치", "ItemData", "Blue", "확인할 값 1개"),
            ("", "_SellPrice  99999 → 5000 (행 412개)"),
            ("", "_BuyPrice  1 → 100"),
            ("", "✕ _BuyPrice  1 → 50 (내 값 100)"),
            ("설정", "Config.lua", "Blue", "다시 적용하지 못함: 지금 게임과 형식이 달라"),
            ("", "UI_Scale  1.0 → 1.25"),
        ]
        assert page.top_line.cget("text") == "업데이트로 수정을 다시 적용했습니다: 수정 4개 · 확인할 값 1개"
        assert [c[0] for c in m.calls].count("drop_edit_notice") == 1
        dialog.select("files")
        dialog.select("summary")
        assert [c[0] for c in m.calls].count("drop_edit_notice") == 1  # (once)
        assert page.clear_button.winfo_manager() == "" and "disabled" not in page.reset_button.state()
    finally:
        dialog.destroy()
    m.snaps[mod.id] = snapshot(mod, [], record=model.Record(error="기록이 손상되었습니다 (x)"),
                               issues=["모드 수정 기록을 읽지 못해 쓰지 않고 있습니다: 기록이 손상되었습니다 (x)"])
    m.items[mod.id] = []
    dialog = window(app, mod.id, "summary")
    page = dialog.pages["summary"]
    try:
        assert page.top_line.cget("text").startswith("모드 수정 기록을 읽지 못해 쓰지 않고 있습니다")
        assert page.clear_button.winfo_manager() == "pack"
        page.clear_button.invoke()
        plan = dialog.pending.plan(mod.id, "1|", 0)
        assert plan.clear_bad and dialog.pending.count() == 1 and "disabled" in page.clear_button.state()
        assert page.pending_line.cget("text") == "● 저장 전: 기록을 지웁니다 (모드의 원래 파일을 씁니다)"
    finally:
        dialog.destroy()
    empty = FakeMod("empty", "Empty")
    m.add(empty, snapshot(empty, [efile(HELM)]))
    dialog = window(app, empty.id, "summary")
    try:
        assert dialog.pages["summary"].top_line.cget("text") == "이 모드에 한 수정이 없습니다."
        assert "disabled" in dialog.pages["summary"].reset_button.state()
    finally:
        dialog.destroy()


def test_summary_page_actions_are_pending_entries(summary_env):
    """Each action is an entry of the unsaved changes: the file put back, one value undone (a rule as a whole), 내
    값 유지 / 작성자 값 쓰기 (모두), 다시 적용 for a file that waits, 모든 수정 되돌리기; the rows say ● 저장 전."""
    app, m, mod, (out_src, data_src, lua_src), (rule, single, conflict, lua_change) = summary_env
    dialog = window(app, mod.id, "summary")
    page = dialog.pages["summary"]
    tree = page.tree
    pending = dialog.pending
    try:
        def select(text):
            for iid in tree.get_children():
                for row in (iid, *tree.get_children(iid)):
                    if text in tree.item(row, "values")[1]:
                        tree.selection_set(row)
                        app.root.update()
                        return row
            raise AssertionError(text)

        select(HELM)
        assert page.revert_button.cget("text") == "이 수정 되돌리기" and "disabled" in page.reapply_button.state()
        key(app, tree, "<Delete>")
        assert not pending.excluded(out_src) and pending.count() == 1 and not dialog.closed
        assert tree.item(select(HELM), "values")[3] == "● 저장 전 (되돌림)"
        assert "disabled" in page.revert_button.state()
        select("99999 → 5000")
        page.revert_button.invoke()
        mp = pending.member(data_src, "")
        assert mp.changes == (single,) and mp.conflicts == (conflict,)
        assert tree.item(select("ItemData"), "values")[3] == "● 저장 전"
        select("✕ _BuyPrice")
        assert page.keep_button.cget("text") == "내 값 유지" and "disabled" not in page.keep_button.state()
        assert page.p_title.cget("text") == SELL.format(102) == page._tip(tree.selection()[0])  # (which value)
        assert page.p_detail.cget("text") == conflict.text()
        page.keep_button.invoke()
        mp = pending.member(data_src, "")
        assert [(c.at, c.before, c.after) for c in mp.changes] == [(single.at, "1", "100"),
                                                                    (conflict.change.at, "50", "100")]
        assert mp.conflicts == ()
        select("Config.lua")
        assert "disabled" not in page.reapply_button.state() and page.p_detail.cget("text") == "지금 게임과 형식이 달라"
        page.reapply_button.invoke()
        assert lua_src in dialog.pending.plan(mod.id, "1|", 2).reapply
        assert tree.item(select("Config.lua"), "values")[3] == "● 저장 전 (다시 적용)"
        page.reset_button.invoke()
        plan = pending.plan(mod.id, "1|", 2)
        assert plan.reset and pending.count() == 1
        assert all(tree.item(i, "values")[3] == "● 저장 전 (되돌림)" for i in tree.get_children())
        assert page.pending_line.cget("text") == "● 저장 전: 모든 수정을 되돌립니다" and "disabled" in page.reset_button.state()
        assert page.top_line.cget("text") == "업데이트로 수정을 다시 적용했습니다: 수정 4개 · 확인할 값 1개"  # (kept)
    finally:
        dialog.destroy()


def test_summary_page_keep_all_take_all_and_new_pending_rows(summary_env):
    """모두 작성자 값 쓰기 on a file's row; a file left out on 파일 고르기 shows here at once as a new row."""
    app, m, mod, (out_src, data_src, lua_src), (rule, single, conflict, lua_change) = summary_env
    dialog = window(app, mod.id, "summary")
    page = dialog.pages["summary"]
    try:
        tree = page.tree
        data_row = tree.get_children()[1]
        tree.selection_set(data_row)
        app.root.update()
        assert page.keep_button.cget("text") == "모두 내 값 유지" and page.take_button.cget("text") == "모두 작성자 값 쓰기"
        page.take_button.invoke()
        assert dialog.pending.member(data_src, "").conflicts == () and dialog.pending.count() == 1
        dialog.select("files")
        files = dialog.pages["files"]
        pick(app, files.tree, f"o1/{TEX}")
        files.out_button.invoke()  # (on another page: 수정 내용 follows)
        dialog.select("summary")
        last = tree.get_children()[-1]
        assert tree.item(last, "values") == ("뺌", TEX, "Blue", "● 저장 전")
    finally:
        dialog.destroy()


@pytest.mark.parametrize("pair", [(TEX, STREAM, "텍스처"), (MESH, MESH.replace("STM/Art", "STM/streaming/Art"), "모델")],
                         ids=["texture", "model"])
def test_summary_revert_puts_back_the_streaming_copy(root, pair):
    """이 수정 되돌리기 on a texture or model left out puts back its streaming copy too, and the copy's line the file
    (left out together on 파일 고르기: one without the other would mix the mod's file with the game's copy); the
    panel says so, in words that fit both."""
    base, copy, kind = pair  # (a texture, or a model)
    m = FakeManager()
    mod = FakeMod("tex", "Colors", options=[{"id": "o1", "name": "Red"}], selected=["o1"])
    red = options_of(mod)[0]
    tex, stream = model.Source("Red", 0, base), model.Source("Red", 0, copy)
    record = model.Record(rev=1, items=[model.Item(tex, exclude=True), model.Item(stream, exclude=True)])
    files = [efile(base, red, state="excluded", twin=copy, kind=kind),
             efile(copy, red, state="excluded", twin=base, kind=kind), efile(HELM, red, kind="재질")]
    m.add(mod, snapshot(mod, files, record=record))
    m.items[mod.id] = [model.EditItemView("exclude", tex, "", base, (), (), "active"),
                       model.EditItemView("exclude", stream, "", copy, (), (), "active")]
    app = make_app(root, m)
    for rel in (base, copy):
        dialog = window(app, mod.id, "summary")
        try:
            page = dialog.pages["summary"]
            tree = page.tree
            pick(app, tree, next(i for i in tree.get_children() if tree.item(i, "values")[1] == rel))
            assert page.p_more.cget("text") == "게임에 넣지 않는 파일입니다.\n되돌리면 스트리밍 사본도 함께 넣습니다"
            page.revert_button.invoke()
            assert not dialog.pending.excluded(tex) and not dialog.pending.excluded(stream)
            assert dialog.pending.count() == 2
            assert [tree.item(i, "values")[3] for i in tree.get_children()] == ["● 저장 전 (되돌림)"] * 2
        finally:
            dialog.destroy()
    # the copy already put back (or never left out): the file alone
    m.snaps[mod.id] = snapshot(mod, files, record=model.Record(rev=1, items=[model.Item(tex, exclude=True)]))
    m.items[mod.id] = m.items[mod.id][:1]
    dialog = window(app, mod.id, "summary")
    try:
        page = dialog.pages["summary"]
        pick(app, page.tree, page.tree.get_children()[0])
        assert page.p_more.cget("text") == "게임에 넣지 않는 파일입니다."
        page.revert_button.invoke()
        assert dialog.pending.count() == 1 and not dialog.pending.excluded(stream)
    finally:
        dialog.destroy()


def test_summary_says_what_saving_does_on_a_line_of_its_own(root):
    """기록 지우기 / 모든 수정 되돌리기: "● 저장 전 ..." is on a line of its own under why the record cannot be read or
    the update's report (two lines long, they would leave it no room); a text too long for its lines is the line's
    tooltip."""
    m = FakeManager()
    mod = FakeMod("bad", "Bad")
    error = "기록이 손상되었습니다 (Expecting value: line 1 column 34 (char 33))"
    issue = f"모드 수정 기록을 읽지 못해 쓰지 않고 있습니다: {error}"
    m.add(mod, snapshot(mod, [efile(HELM)], record=model.Record(error=error), issues=[issue + " " + issue]))
    app = make_app(root, m)
    dialog = window(app, mod.id, "summary")
    try:
        page = dialog.pages["summary"]

        def seen_whole(text) -> bool:  # (a label of the page has it, and is as tall as its lines)
            labels, todo = [], [page.frame]
            while todo:
                w = todo.pop()
                todo += w.winfo_children()
                if w.winfo_class() == "TLabel" and text in str(w.cget("text")):
                    labels.append(w)
            return bool(labels) and all(w.winfo_reqheight() <= w.master.winfo_height() for w in labels)

        page.clear_button.invoke()
        dialog.update()
        assert seen_whole("● 저장 전: 기록을 지웁니다 (모드의 원래 파일을 씁니다)")
        top, line = page.top_line, page.pending_line
        assert top.cget("text") == issue + " " + issue  # (as it was)
        assert line.cget("text") == "● 저장 전: 기록을 지웁니다 (모드의 원래 파일을 씁니다)"
        assert 0 < line.winfo_reqheight() <= line.master.winfo_height()  # (all of it seen)
        assert page._cut(top) and not page._cut(line)  # (the long reason: in the tooltip)
    finally:
        dialog.destroy()


# ---- the window's size and keys -------------------------------------------------------------------------------
def test_pages_keep_the_window_size(data_env):
    """Frozen once shown: switching pages, picking values and long notes do not change the window's size."""
    app, m, mod, target = data_env
    dialog, page = open_data(app, mod)
    try:
        dialog.maxsize(4000, 3000)
        dialog.wait_window = lambda *a: None
        dialog.show()
        pump(app, lambda: dialog.winfo_ismapped() and dialog.body.winfo_width() > 1)
        for _ in range(3):
            app.root.update()
        size = (dialog.winfo_width(), dialog.winfo_height())
        req = (dialog.winfo_reqwidth(), dialog.winfo_reqheight())
        page.filter_check.invoke()
        page.tree.selection_set(leaf(page, "_Values[_ItemId=100]._RawName"))
        app.root.update()
        page.panel.note.configure(text="아주 긴 설명 " * 40)
        page.tree.selection_set([leaf(page, SELL.format(100)), leaf(page, SELL.format(101))])
        for key_ in ("files", "summary", "config", "data"):
            dialog.select(key_, force=True)
            for _ in range(3):
                app.root.update()
            assert (dialog.winfo_width(), dialog.winfo_height()) == size, key_
            assert (dialog.winfo_reqwidth(), dialog.winfo_reqheight()) == req, key_
    finally:
        dialog.destroy()


# ---- with the real manager --------------------------------------------------------------------------------------
def test_pages_make_plans_the_manager_saves(root, manager, tmp_path):
    """End to end with the real manager: a value set on 게임 수치 and on 스크립트 설정 and a file left out on 파일
    고르기 give a plan (EditChoice) that ModManager.apply_edit_plan saves."""
    from mhwmm import modtool

    files = {ITEM: mf.item_data(3), LUA: mf.NIER_CONFIG_LUA, HELM: b"mdf2"}
    with manager.prepare_install(make_tree(tmp_path / "src" / "Tweaks", files)) as s:
        mod = manager.install_package(s, s.variants)
    manager.settings.check_materials = False
    app = make_app(root, manager)
    dialog = window(app, mod.id, "data")
    try:
        page = dialog.pages["data"]
        pump(app, lambda: page.ready_view is not None, timeout=30)
        assert page.banner.cget("text") == rszdata.NO_GAME
        page.tree.selection_set(leaf(page, SELL.format(101)))
        app.root.update()
        page.panel.value_var.set("777")
        page.panel.apply_button.invoke()
        dialog.select("config")
        config = dialog.pages["config"]
        pump(app, lambda: config.ready_view is not None, timeout=30)
        config.tree.selection_set(leaf(config, "Config.UI_Scale"))
        app.root.update()
        config.panel.value_var.set("1.25")
        config.panel.apply_button.invoke()
        dialog.select("files")
        files_page = dialog.pages["files"]
        pick(app, files_page.tree, HELM)
        files_page.out_button.invoke()
        assert dialog.pending.count() == 3
        dialog.ok()
        choice = dialog.result
        assert isinstance(choice, modtool.EditChoice) and choice.page == "files"
    finally:
        if not dialog.closed:
            dialog.destroy()
    result = manager.apply_edit_plan(choice.plan)
    assert result.changed
    record = manager.edit_record(mod.id)
    found = {(i.source.rel, i.exclude, tuple((c.at, c.after) for m_ in i.members for c in m_.changes))
             for i in record.items}
    assert found == {(HELM, True, ()), (ITEM, False, ((SELL.format(101), "777"),)),
                     (LUA, False, (("Config.UI_Scale", "1.25"),))}
