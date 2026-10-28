"""다른 장비로 옮기기 in the GUI: the 모드 수정 window (the mod picked in it, 원래 장비, the table of its parts and
the equipment each goes to, the searchable list of equipment, what the new mod would hold, 원래 모드 끄기), the
new mod made through the app, and where it is offered (the toolbar, the context menu, the 장비 외형 window). The manager's retarget API is replaced on the instance by fakes (the files
themselves: test_equip_retarget.py); what the mods change is the real equipment scan of their files."""

import gc
import queue
import threading
import time
from collections.abc import Mapping

import pytest

from conftest import make_tree, make_zip
from test_modtool_gui import patch_window
from mhwmm.equip.model import PART_LABELS, Target
from mhwmm.equip.retarget import Move, Plan, RetargetError, Slot

tk = pytest.importorskip("tkinter")

ARMOR = Target("armor", "f/004/001")
WEAPON = Target("weapon", "02/000029")
GANA = Target("armor", "f/045/002")
DARA = Target("armor", "f/060/000")
LONA = Target("armor", "f/050/000")
BLADES_B = Target("weapon", "02/000031")
BLADES_C = Target("weapon", "02/000044")
CHOICES = {"armor": [(GANA, "가나 갑옷 · 가나 갑옷 β"), (DARA, "다라 갑옷"), (LONA, "로나 갑옷")],
           "weapon": [(BLADES_B, "쌍검 나"), (BLADES_C, "쌍검 다")]}
CHARACTER = "natives/STM/Art/Model/Character/"
HELM = CHARACTER + "ch03/004/001/3/ch03_004_0013.mdf2.45"
BODY = CHARACTER + "ch03/004/001/2/ch03_004_0012.mesh.241111606"
BLADES = "natives/STM/Art/Model/Item/it02/00/0029/it0200_0029_0.mesh.241111606"
PALICO = CHARACTER + "ch05/062/0000/ch05_062_0000.mesh.241111606"
AVP = "natives/STM/GameDesign/Equip/_Prefab/Armor/Female/004/001/004_001_avp.user.3"
PART_FILES = {"helm": HELM, "body": BODY}


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


@pytest.fixture(autouse=True)
def answer_dialogs(monkeypatch):
    """A message box left open would block the test."""
    from mhwmm import gui

    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: None)
    for name in ("askyesno", "askyesnocancel"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: True)


def pump(root, until, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if until():
            return
        time.sleep(0.02)
    raise AssertionError("시간 초과")


def on_gui_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


class FakeRetarget:
    """The manager's retarget API (retarget_choices/_name, plan_retarget, create_retarget) for the mods of the
    ``env`` fixture, recording what it was asked; retarget_sources only records (the GUI scans itself)."""

    def __init__(self, m, mods, tmp_path):
        self.m, self.mods, self.tmp_path = m, mods, tmp_path
        self.plans, self.created, self.sources_asked, self.files = [], [], [], []

    def sources(self, mod_id, files=None):
        self.sources_asked.append((on_gui_thread(), files is not None))
        return []

    def choices(self, source, progress=None):
        return list(CHOICES[source.kind])

    def name(self, mod_id, target, progress=None):
        def first(t):
            return dict(CHOICES[t.kind])[t].split(" · ")[0]

        if isinstance(target, Mapping):  # (as the manager names a move of the parts to different equipment)
            parts = " · ".join(f"{PART_LABELS[p]} {first(t)}" for p, t in target.items())
            return f"{self.m.get(mod_id).name} → {parts}"
        return f"{self.m.get(mod_id).name} → {first(target)}"

    def plan(self, mod_id, source, target, parts=None, files=None):
        self.plans.append((mod_id, source, target, parts))
        self.files.append(files)
        a = Slot.of(source)
        if isinstance(target, Mapping):  # each part to its own equipment; 다라 갑옷 has no helm
            targets = {part: Slot.of(t) for part, t in target.items()}
            moves = [Move(PART_FILES[part], PART_FILES[part].replace("004/001", slot.key), target=slot)
                     for part, slot in targets.items()]
            bad = {part for part, t in target.items() if part == "helm" and t == DARA}
            return Plan(a, next(iter(targets.values())), parts=tuple(target), moves=moves, targets=targets,
                        errors=[f"{PART_LABELS[p]}: 대상 장비에는 {PART_LABELS[p]}가 없습니다" for p in bad],
                        bad_parts=bad, dropped=[(AVP, "외형 설정 파일(_avp.user)은 옮기지 않습니다")])
        b = Slot.of(target)
        if target == BLADES_C:
            raise RetargetError("모드 파일을 읽지 못했습니다")
        if target == DARA:
            return Plan(a, b, parts=tuple(parts or ("helm", "body")),
                        moves=[Move(HELM, HELM.replace("004/001", "060/000"))],
                        errors=["대상 장비에는 머리가 없습니다"], bad_parts={"helm"})
        if target == LONA:  # nothing left behind but the original equipment's own settings
            new = BODY.replace("004/001", "050/000").replace("004_0012", "050_0002")
            return Plan(a, b, parts=tuple(parts or ("helm", "body")),
                        moves=[Move(BODY, new), Move(BODY.replace(".mesh.", ".mdf2.").replace(".241111606", ".45"),
                                                     new.replace(".mesh.", ".mdf2."), "fill")],
                        dropped=[(AVP, "외형 설정 파일(_avp.user)은 옮기지 않습니다")])
        new = HELM.replace("004/001", "045/002").replace("004_0013", "045_0023")
        return Plan(a, b, parts=tuple(parts or ("helm", "body")), moves=[Move(HELM, new, "mdf2")],
                    kept=["natives/STM/Art/Author/shared_ALBD.tex.241106027"],
                    dropped=[(AVP, "외형 설정 파일은 옮기지 않습니다"),
                             ("reframework/autorun/x.lua", "게임 파일이 아닙니다 (스크립트·플러그인 등)")],
                    notes=["모드가 원래 장비의 게임 텍스처 등 2개를 그대로 씁니다."])

    def create(self, mod_id, source, target, parts=None, disable_original=True, enable=True, progress=None):
        self.created.append((mod_id, source, target, parts, disable_original, enable))
        progress(0, 1, "새 모드 만드는 중...")
        folder = make_tree(self.tmp_path / "made" / str(len(self.created)), {HELM.replace("004/001", "045/002"): "m"})
        with self.m.prepare_install(folder) as s:
            mod = self.m.install_package(s, s.variants)
        self.m.set_enabled(mod.id, enable)
        if disable_original:
            self.m.set_enabled(mod_id, False)
        return mod


@pytest.fixture
def env(root, manager, tmp_path, monkeypatch):
    """The app with an armor-and-weapon mod, a helm mod, a palico mod, a script mod and a mod with options, and
    the fake retarget API; every read of a mod's state (mod_entries, equipment_job) is recorded with whether it
    ran on the GUI thread."""
    from mhwmm import gui

    mods = {}
    for name, files in (("Armor", {HELM: "m", BODY: "b", BLADES: "w"}), ("Helm", {HELM: "m", BODY: "b"}),
                        ("Palico", {PALICO: "p"}), ("Script", {"reframework/autorun/x.lua": "--"})):
        with manager.prepare_install(make_tree(tmp_path / "src" / name, files)) as s:
            mods[name] = manager.install_package(s, s.variants)
        manager.set_enabled(mods[name].id, True)
    with manager.prepare_install(make_zip(tmp_path / "Colors.zip", {f"Red/{HELM}": "r", f"Blue/{HELM}": "b",
                                                                    f"Gold/{HELM}": "g"})) as s:
        mods["Colors"] = manager.install_package(s, s.variants)
    manager.set_enabled(mods["Colors"].id, True)
    manager.set_options(mods["Colors"].id, [mods["Colors"].options[0]["id"]])
    manager.settings.check_materials = False
    fake = FakeRetarget(manager, mods, tmp_path)
    # (set on the instance: the manager's own methods, once there, are replaced the same way)
    monkeypatch.setattr(manager, "equipment_catalog", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(manager, "retarget_sources", fake.sources, raising=False)
    monkeypatch.setattr(manager, "retarget_choices", fake.choices, raising=False)
    monkeypatch.setattr(manager, "retarget_name", fake.name, raising=False)
    monkeypatch.setattr(manager, "plan_retarget", fake.plan, raising=False)
    monkeypatch.setattr(manager, "create_retarget", fake.create, raising=False)
    reads = fake.reads = []
    for name in ("mod_entries", "equipment_job"):
        def read(*a, _real=getattr(manager, name), _name=name, **k):
            reads.append((_name, on_gui_thread()))
            return _real(*a, **k)
        monkeypatch.setattr(manager, name, read)
    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: True)
    return app, manager, mods, fake


def rows(dialog):
    return [tuple(dialog.tree.item(i, "values")) for i in dialog.tree.get_children()]


def enabled(button) -> bool:
    return "disabled" not in button.state()


def part_table(dialog) -> list[tuple]:
    """The table 부위별로 옮길 장비: (부위, 옮길 장비, 번호) per row."""
    return [tuple(dialog.parts_tree.item(i, "values")) for i in dialog.parts_tree.get_children()]


def choose_rows(dialog, *rows):
    """Select rows of the table (as a click or Ctrl+click does)."""
    dialog.parts_tree.selection_set(rows)
    dialog.update()


def shown(widget) -> bool:
    return bool(widget.winfo_manager())


def test_dialog_lists_the_equipment_and_shows_the_plan(env):
    from mhwmm.retargetview import Choice, RetargetDialog

    app, m, mods, fake = env
    root = app.root
    dialog = RetargetDialog(root, app, mods["Armor"].id, ARMOR)
    try:
        pump(root, lambda: isinstance(dialog.choices.get(ARMOR), list) and dialog.tree.get_children())
        assert dialog.source == ARMOR and dialog.source_box.current() == 0
        assert list(dialog.source_box.cget("values")) == ["방어구 004-001 (여성)", "쌍검 모델 00-0029 (쌍검)"]
        # a row per part the mod changes on that design (from the equipment scan), all selected, none moved yet
        assert part_table(dialog) == [("머리", "옮기지 않음", ""), ("몸통", "옮기지 않음", "")]
        assert dialog.parts_tree.selection() == ("helm", "body")
        assert dialog.pick_hint.cget("text") == "머리 · 몸통에 입힐 장비를 고르세요"
        assert not enabled(dialog.all_button) and not enabled(dialog.clear_button)
        assert "남성용·여성용" in dialog.kind_note.cget("text")
        assert rows(dialog) == [("가나 갑옷 · 가나 갑옷 β", "045-002"), ("다라 갑옷", "060-000"), ("로나 갑옷", "050-000")]
        assert dialog.shown_text() == "옮길 장비를 목록에서 고르세요." and not enabled(dialog.create_button)

        # the search filters by name and by number; the list says what it shows
        dialog.search_var.set("060")
        assert rows(dialog) == [("다라 갑옷", "060-000")] and dialog.list_note.cget("text") == "장비 1개 (전체 3개)"
        dialog.search_var.set("β")
        assert rows(dialog) == [("가나 갑옷 · 가나 갑옷 β", "045-002")]
        dialog.search_var.set("없는 장비")
        assert rows(dialog) == [] and dialog.list_note.cget("text") == "검색에 맞는 장비가 없습니다."
        dialog.search_var.set("")
        assert len(rows(dialog)) == 3

        # picking one gives it to the selected rows (all: the whole design, as one move) and plans it on another
        # thread, from the mod's files read when the dialog opened
        dialog.tree.selection_set("0")
        root.update()
        assert dialog.target == GANA and dialog.shown_text() == "확인 중..."
        assert part_table(dialog) == [("머리", "가나 갑옷", "045-002"), ("몸통", "가나 갑옷", "045-002")]
        assert enabled(dialog.clear_button)
        pump(root, lambda: "옮길 파일" in dialog.shown_text())
        assert fake.plans == [(mods["Armor"].id, ARMOR, GANA, None)]  # every part to it: all of them
        assert set(fake.files[-1]) == {HELM, BODY, BLADES}
        text = dialog.shown_text()
        assert text.startswith("옮길 파일 1개 · 그대로 넣을 파일 1개 · 넣지 않는 파일 2개\n")
        assert "\nch03_004_0013.mdf2 → ch03_045_0023.mdf2\n" in text
        assert "새 모드: Armor → 가나 갑옷\n" in text  # (the name create_retarget gives it)
        assert "알아 둘 점\n· 모드가 원래 장비의 게임 텍스처 등 2개를 그대로 씁니다." in text
        assert ("넣지 않는 파일\n· 파일 1개: 외형 설정 파일은 옮기지 않습니다\n"
                "· 파일 1개: 게임 파일이 아닙니다 (스크립트·플러그인 등)") in text
        assert "만들 수 없습니다" not in text
        assert enabled(dialog.create_button)

        # a part not moved (옮기지 않기 on its row): only the others are planned, and the text says where each goes;
        # none: nothing to plan
        choose_rows(dialog, "body")
        assert dialog.pick_hint.cget("text") == "몸통에 입힐 장비를 고르세요" and enabled(dialog.all_button)
        assert dialog.tree.selection() == ("0",)  # (the row's equipment shown in the list)
        dialog.clear_button.invoke()
        assert not enabled(dialog.create_button) and not enabled(dialog.clear_button)
        assert part_table(dialog)[1] == ("몸통", "옮기지 않음", "")
        pump(root, lambda: len(fake.plans) == 2 and "옮길 파일" in dialog.shown_text())
        assert fake.plans[-1][2:] == (GANA, ("helm",)) and enabled(dialog.create_button)
        assert dialog.shown_text().startswith("머리 → 가나 갑옷 (045-002)\n몸통은 옮기지 않습니다\n옮길 파일 1개")
        assert dialog.tree.selection() == ()
        choose_rows(dialog, "helm")
        dialog.clear_button.invoke()
        assert dialog.shown_text() == "옮길 장비를 목록에서 고르세요." and not enabled(dialog.create_button)
        dialog.all_button.invoke()
        assert dialog.parts_tree.selection() == ("helm", "body") and not enabled(dialog.all_button)
        dialog.tree.selection_set("0")  # all again: shown at once from before
        root.update()
        assert "옮길 파일" in dialog.shown_text() and enabled(dialog.create_button) and len(fake.plans) == 2

        # a plan with errors cannot be made; one about a part says which to leave out
        dialog.tree.selection_set("1")
        pump(root, lambda: "만들 수 없습니다" in dialog.shown_text())
        text = dialog.shown_text()
        assert text.startswith("만들 수 없습니다\n")
        assert ("· 대상 장비에는 머리가 없습니다\n'부위별로 옮길 장비'에서 머리를 고른 뒤 '옮기지 않기'를 누르거나 다른 "
                "장비를 고르면 나머지 부위는 그대로 옮길 수 있습니다\n") in text
        assert not enabled(dialog.create_button)
        dialog.ok()  # (the button is off: nothing happens)
        assert dialog.result is None and not dialog.closed
        # as it says: the part selected in the table, then 옮기지 않기 leaves the other parts as they were
        choose_rows(dialog, "helm")
        dialog.clear_button.invoke()
        assert dialog.assigned == {"body": DARA}
        dialog.all_button.invoke()

        # the chosen equipment stays chosen while a search hides it
        dialog.tree.selection_set("0")
        root.update()
        dialog.search_var.set("다라")
        root.update()
        assert dialog.target == GANA and enabled(dialog.create_button)
        dialog.search_var.set("")
        root.update()
        assert dialog.tree.selection() == ("0",)

        dialog.off_check.invoke()
        dialog.ok()
        assert dialog.closed
        assert dialog.result == Choice(ARMOR, GANA, None, True, "Armor → 가나 갑옷")
    finally:
        if not dialog.closed:
            dialog.destroy()


def test_each_part_to_its_own_equipment(env):
    """부위마다 다른 장비로: the pick goes to the selected rows only; rows of different equipment make one move of
    each part to its own (planned and made as a mapping), and the text says where each goes."""
    from mhwmm.retargetview import Choice, RetargetDialog

    app, m, mods, fake = env
    root = app.root
    armor = mods["Armor"].id
    dialog = RetargetDialog(root, app, armor, ARMOR)
    try:
        pump(root, lambda: dialog.tree.get_children())
        # 머리 alone: the pick goes to it; 몸통 is not moved
        choose_rows(dialog, "helm")
        assert dialog.pick_hint.cget("text") == "머리에 입힐 장비를 고르세요"
        assert enabled(dialog.all_button) and not enabled(dialog.clear_button)
        dialog.tree.selection_set("0")
        pump(root, lambda: "옮길 파일" in dialog.shown_text())
        assert part_table(dialog) == [("머리", "가나 갑옷", "045-002"), ("몸통", "옮기지 않음", "")]
        assert fake.plans[-1] == (armor, ARMOR, GANA, ("helm",))  # (one equipment: as when a part was unticked)
        assert dialog.shown_text().startswith("머리 → 가나 갑옷 (045-002)\n몸통은 옮기지 않습니다\n")

        # then 몸통 (nothing shown selected in the list: it has no equipment yet) to 로나 갑옷
        choose_rows(dialog, "body")
        assert dialog.tree.selection() == () and dialog.pick_hint.cget("text") == "몸통에 입힐 장비를 고르세요"
        dialog.tree.selection_set("2")
        root.update()
        assert dialog.target is None and dialog.assigned == {"helm": GANA, "body": LONA}
        assert part_table(dialog) == [("머리", "가나 갑옷", "045-002"), ("몸통", "로나 갑옷", "050-000")]
        pump(root, lambda: enabled(dialog.create_button))
        assert fake.plans[-1] == (armor, ARMOR, {"helm": GANA, "body": LONA}, None)
        assert list(fake.plans[-1][2]) == ["helm", "body"]  # (in the game's order)
        text = dialog.shown_text()
        assert text.startswith("머리 → 가나 갑옷 (045-002)\n몸통 → 로나 갑옷 (050-000)\n옮길 파일 2개 · ")
        assert "새 모드: Armor → 머리 가나 갑옷 · 몸통 로나 갑옷\n" in text

        # one row selected: its equipment shown selected in the list; rows of different equipment: none
        choose_rows(dialog, "helm")
        assert dialog.tree.selection() == ("0",)
        choose_rows(dialog, "body")
        assert dialog.tree.selection() == ("2",)
        dialog.all_button.invoke()
        root.update()
        assert dialog.tree.selection() == () and dialog.pick_hint.cget("text") == "머리 · 몸통에 입힐 장비를 고르세요"
        assert not enabled(dialog.all_button) and enabled(dialog.clear_button)
        # a search that hides the rows' equipment: still theirs
        choose_rows(dialog, "body")
        dialog.search_var.set("가나")
        root.update()
        assert dialog.tree.selection() == () and dialog.assigned == {"helm": GANA, "body": LONA}
        dialog.search_var.set("")
        root.update()
        assert dialog.tree.selection() == ("2",) and enabled(dialog.create_button)

        # an error about one part: its line in red, and what to do about it
        choose_rows(dialog, "helm")
        dialog.tree.selection_set("1")
        pump(root, lambda: "만들 수 없습니다" in dialog.shown_text())
        text = dialog.shown_text()
        assert text.startswith("머리 → 다라 갑옷 (060-000)\n몸통 → 로나 갑옷 (050-000)\n만들 수 없습니다\n"
                               "· 머리: 대상 장비에는 머리가 없습니다\n'부위별로 옮길 장비'에서 머리를 고른 뒤 "
                               "'옮기지 않기'를 누르거나 다른 장비를 고르면 나머지 부위는 그대로 옮길 수 있습니다\n")
        assert "error" in dialog.text.tag_names("1.0") and "error" not in dialog.text.tag_names("2.0")
        assert not enabled(dialog.create_button)
        planned = len(fake.plans)
        dialog.tree.selection_set("0")  # back: shown at once from before
        root.update()
        assert enabled(dialog.create_button) and len(fake.plans) == planned

        dialog.ok()
        assert dialog.closed
        assert dialog.result == Choice(ARMOR, {"helm": GANA, "body": LONA}, None, False,
                                       "Armor → 머리 가나 갑옷 · 몸통 로나 갑옷")
    finally:
        if not dialog.closed:
            dialog.destroy()

    # made as one mod, the mapping passed on as it is
    app._create_retarget(armor, dialog.result)
    pump(root, lambda: fake.created and app.busy is None)
    assert fake.created == [(armor, ARMOR, {"helm": GANA, "body": LONA}, None, False, True)]
    assert app.tree.selection() == (m.mods[-1].id,)


def test_the_table_works_from_the_keyboard(env):
    """Tab reaches the table, the arrows move its selection, Return in the list gives the row the equipment
    the list's keyboard focus is on."""
    from mhwmm.retargetview import RetargetDialog

    app, m, mods, fake = env
    root = app.root
    dialog = RetargetDialog(root, app, mods["Helm"].id)
    try:
        dialog.deiconify()
        pump(root, lambda: dialog.tree.get_children())
        after = dialog.source_box.tk_focusNext()  # (the window's tools, then the page)
        while after in dialog.nav.values():
            after = after.tk_focusNext()
        assert after is dialog.parts_tree
        # (then the buttons under it when they can be used: not yet, every row is selected and none moved)
        assert dialog.parts_tree.tk_focusNext() is dialog.search
        dialog.parts_tree.focus_force()
        pump(root, lambda: dialog.focus_get() is dialog.parts_tree)
        assert dialog.parts_tree.focus() == "helm"
        dialog.parts_tree.event_generate("<Down>")
        root.update()
        assert dialog.parts_tree.selection() == ("body",)
        assert dialog.pick_hint.cget("text") == "몸통에 입힐 장비를 고르세요" and enabled(dialog.all_button)
        assert dialog.parts_tree.tk_focusNext() is dialog.all_button
        dialog.tree.focus_force()
        pump(root, lambda: dialog.focus_get() is dialog.tree)
        dialog.tree.event_generate("<Return>")
        root.update()
        assert dialog.assigned == {"body": GANA} and not dialog.closed
        pump(root, lambda: enabled(dialog.create_button))
        assert fake.plans[-1][2:] == (GANA, ("body",))
        dialog.clear_button.focus_force()  # Return on a button presses it (not 새 모드 만들기)
        pump(root, lambda: dialog.focus_get() is dialog.clear_button)
        dialog.clear_button.event_generate("<Return>")
        root.update()
        assert dialog.assigned == {} and not dialog.closed
        dialog.tree.focus_force()
        pump(root, lambda: dialog.focus_get() is dialog.tree)
        dialog.tree.event_generate("<Return>")
        pump(root, lambda: enabled(dialog.create_button))
        assert dialog.assigned == {"body": GANA}
        dialog.parts_tree.focus_force()
        pump(root, lambda: dialog.focus_get() is dialog.parts_tree)
        dialog.parts_tree.event_generate("<Up>")
        root.update()
        assert dialog.parts_tree.selection() == ("helm",) and dialog.tree.selection() == ()
        dialog.parts_tree.event_generate("<Return>")  # (the dialog's Return: makes the mod)
        root.update()
        assert dialog.closed and dialog.result.target == GANA and dialog.result.parts == ("body",)
    finally:
        if not dialog.closed:
            dialog.destroy()


def test_dialog_weapons_errors_and_mods_without_equipment(env):
    from mhwmm.retargetview import RetargetDialog

    app, m, mods, fake = env
    root = app.root
    dialog = RetargetDialog(root, app, mods["Armor"].id)  # no equipment given: the first one
    try:
        pump(root, lambda: dialog.tree.get_children())
        assert dialog.source == ARMOR and shown(dialog.all_button)
        dialog.tree.selection_set("0")
        pump(root, lambda: enabled(dialog.create_button))
        # another source: its own list (same weapon type), one row (its models move together), nothing chosen yet
        dialog.source_box.current(1)
        dialog.source_box.event_generate("<<ComboboxSelected>>")
        assert dialog.source == WEAPON and dialog.target is None and dialog.assigned == {}
        assert part_table(dialog) == [("본체·보조", "옮기지 않음", "")]
        assert dialog.parts_tree.selection() == ("weapon",) and not enabled(dialog.all_button)  # (nothing else)
        assert "본체와 보조" in dialog.kind_note.cget("text") and not enabled(dialog.create_button)
        pump(root, lambda: rows(dialog) == [("쌍검 나", "00-0031"), ("쌍검 다", "00-0044")])
        assert dialog.pick_hint.cget("text") == "이 무기의 모습을 입힐 무기를 고르세요"
        dialog.tree.selection_set("0")
        pump(root, lambda: enabled(dialog.create_button))
        assert fake.plans[-1] == (mods["Armor"].id, WEAPON, BLADES_B, None)  # (one equipment, as before)
        assert part_table(dialog) == [("본체·보조", "쌍검 나", "00-0031")]
        assert not dialog.shown_text().startswith("본체")  # (no per-part lines)
        # a plan that fails says why
        dialog.tree.selection_set("1")
        pump(root, lambda: "확인하지 못했습니다" in dialog.shown_text())
        assert "모드 파일을 읽지 못했습니다" in dialog.shown_text() and not enabled(dialog.create_button)
    finally:
        dialog.destroy()

    for name in ("Palico", "Script"):  # nothing it can be moved from
        dialog = RetargetDialog(root, app, mods[name].id)
        try:
            pump(root, lambda: "옮길 수 있는" in dialog.shown_text())
            assert "방어구나 무기의 외형이 없습니다" in dialog.shown_text()
            assert str(dialog.source_box.cget("state")) == "disabled" and not enabled(dialog.create_button)
        finally:
            dialog.destroy()


def test_plan_text_parts_hint_and_fills():
    """The hint to leave a part out only when other parts can still be moved; A's game files that complete a
    part are counted apart from the mod's own files; where each part goes when they do not all go to one."""
    from mhwmm.retargetview import mapping_lines, part_hint, plan_text

    a, b = Slot.of(ARMOR), Slot.of(DARA)
    bad = Plan(a, b, moves=[Move(HELM, HELM)], errors=["대상 장비에는 팔이 없습니다"], bad_parts={"arm", "helm"})
    assert part_hint(bad, ["helm", "body", "arm"]) == ("'부위별로 옮길 장비'에서 머리·팔을 고른 뒤 '옮기지 않기'를 "
                                                       "누르거나 다른 장비를 고르면 나머지 부위는 그대로 옮길 수 있습니다")
    assert part_hint(bad, ["helm", "arm"]) == ""  # (nothing would be left)
    assert part_hint(bad, ["helm"]) == "" and part_hint(bad, []) == ""
    assert part_hint(Plan(a, b, errors=["x"], bad_parts={"body"}), ["helm", "body"]).startswith(
        "'부위별로 옮길 장비'에서 몸통을 고른 뒤")
    assert part_hint(Plan(a, b, errors=["x"]), ["helm", "body"]) == ""  # (an error about no part)
    # the line above the list stays one line (the window does not grow while rows are clicked)
    from types import SimpleNamespace
    from mhwmm.retargetview import RetargetPage
    six = SimpleNamespace(part_rows=["helm", "body", "arm", "waist", "leg", "slinger"])
    assert RetargetPage._pick_hint(six, six.part_rows) == "모든 부위에 입힐 장비를 고르세요"
    assert RetargetPage._pick_hint(six, six.part_rows[1:]) == "부위 5개에 입힐 장비를 고르세요"
    assert RetargetPage._pick_hint(six, ["helm", "body", "arm"]) == "머리 · 몸통 · 팔에 입힐 장비를 고르세요"
    assert "'옮기지 않기'" not in "".join(text for text, _tags in plan_text(bad, "", ["helm"]))

    fill = BODY.replace(".mesh.241111606", ".mdf2.45")
    filled = Plan(a, Slot.of(LONA), moves=[Move(BODY, BODY.replace("004/001", "050/000")),
                                           Move(fill, fill.replace("004/001", "050/000"), "fill")])
    text = "".join(text for text, _tags in plan_text(filled, "Helm → 로나 갑옷"))
    assert text.startswith("옮길 파일 1개 · 원래 장비의 게임 파일로 채움 1개\nch03_004_0012.mesh → ")
    assert "ch03_004_0012.mdf2" not in text and "새 모드: Helm → 로나 갑옷" in text

    # the per-part lines: none when every row goes to one equipment (the new mod's name says where)
    names = dict(CHOICES["armor"])
    assert mapping_lines(["helm", "body"], {"helm": GANA, "body": GANA}, names) == []
    assert mapping_lines(["helm", "body"], {}, names) == []
    assert mapping_lines(["helm", "body", "arm", "waist"], {"helm": GANA, "body": DARA}, names) == [
        ("helm", "머리 → 가나 갑옷 (045-002)"), ("body", "몸통 → 다라 갑옷 (060-000)"), (None, "팔·허리는 옮기지 않습니다")]
    assert mapping_lines(["helm", "body"], {"body": GANA}, {}) == [("body", "몸통 → 045-002"),
                                                                  (None, "머리는 옮기지 않습니다")]
    mapping = mapping_lines(["helm", "body"], {"helm": DARA, "body": LONA}, names)
    mixed = Plan(a, b, parts=("helm", "body"), moves=[Move(HELM, HELM), Move(BODY, BODY)],
                 errors=["머리: 대상 장비에는 머리가 없습니다"], bad_parts={"helm"},
                 targets={"helm": b, "body": Slot.of(LONA)})
    pieces = plan_text(mixed, "", ["helm", "body"], mapping)
    assert pieces[:4] == [("머리 → 다라 갑옷 (060-000)\n", ("error",)), ("몸통 → 로나 갑옷 (050-000)\n", ()),
                          ("만들 수 없습니다\n", ("heading_error", "gap")),
                          ("· 머리: 대상 장비에는 머리가 없습니다\n", ("error",))]
    assert pieces[4][0].startswith("'부위별로 옮길 장비'에서 머리를 고른 뒤") and pieces[5][1] == ("lead", "gap")
    pieces = plan_text(Plan(a, b, parts=("helm",), moves=[Move(HELM, HELM)]), "", ["helm"],
                       mapping_lines(["helm", "body"], {"helm": GANA}, names))
    assert pieces[:3] == [("머리 → 가나 갑옷 (045-002)\n", ()), ("몸통은 옮기지 않습니다\n", ("muted",)),
                          ("옮길 파일 1개\n", ("lead", "gap"))]


def test_original_off_by_default_only_when_nothing_else_is_lost(env):
    """'원래 모드 끄기' is on only when the new mod holds all the original changes but the original equipment
    itself; the note says what each choice does. An original that is off has nothing to switch off."""
    from mhwmm import retargetview
    from mhwmm.retargetview import RetargetDialog, left_behind

    app, m, mods, fake = env
    root = app.root
    dialog = RetargetDialog(root, app, mods["Helm"].id)
    try:
        pump(root, lambda: dialog.tree.get_children())
        note = dialog.off_note.cget("text")
        assert "끄면 원래 장비는 게임 모습으로 돌아가고, 원래 모드가 바꾸던 다른 장비·부위·스크립트도 함께 꺼집니다" in note
        assert "끄지 않으면 원래 장비와 옮긴 장비가 모두 모드 모습입니다" in note
        assert dialog.off_var.get() and shown(dialog.off_check.master)  # (one equipment: until a plan says more)
        dialog.tree.selection_set("2")  # 로나 갑옷: only the original's own settings stay behind
        pump(root, lambda: enabled(dialog.create_button))
        assert dialog.off_var.get() and dialog.off_why.cget("text") == ""
        dialog.tree.selection_set("0")  # 가나 갑옷: a script stays behind
        pump(root, lambda: "옮길 파일" in dialog.shown_text())
        assert not dialog.off_var.get()
        assert dialog.off_why.cget("text") == "원래 모드에는 새 모드에 들어가지 않는 파일(스크립트·플러그인)도 있습니다"
        choose_rows(dialog, "body")
        dialog.clear_button.invoke()  # (a part not moved: the same)
        pump(root, lambda: "옮길 파일" in dialog.shown_text())
        assert not dialog.off_var.get()
        dialog.off_check.invoke()  # the user's choice stays
        choose_rows(dialog, "helm")
        dialog.tree.selection_set("2")
        pump(root, lambda: enabled(dialog.create_button))
        assert dialog.assigned == {"helm": LONA}
        dialog.all_button.invoke()
        dialog.tree.selection_set("2")
        pump(root, lambda: enabled(dialog.create_button) and dialog.assigned == {"helm": LONA, "body": LONA})
        assert dialog.off_var.get()
        dialog.off_check.invoke()
        dialog.tree.selection_set("2")
        pump(root, lambda: enabled(dialog.create_button))
        assert not dialog.off_var.get()
        dialog.ok()
        assert dialog.result.disable_original is False
    finally:
        if not dialog.closed:
            dialog.destroy()

    # a mod of two pieces of equipment keeps the other one on (both bodies of one design are one)
    dialog = RetargetDialog(root, app, mods["Armor"].id)
    try:
        pump(root, lambda: dialog.tree.get_children())
        assert not dialog.off_var.get()
        dialog.tree.selection_set("2")
        pump(root, lambda: enabled(dialog.create_button))
        assert not dialog.off_var.get() and "파일(다른 장비)도 있습니다" in dialog.off_why.cget("text")
    finally:
        dialog.destroy()
    plan = Plan(Slot.of(ARMOR), Slot.of(GANA), dropped=[("natives/STM/x/other.tex.1", "다른 장비의 파일입니다"),
                                                        ("natives/STM/y.tex.1", "고르지 않은 부위입니다 (팔)"),
                                                        ("dinput8.dll", "게임 파일이 아닙니다")])
    assert left_behind(plan, [ARMOR, Target("armor", "m/004/001")]) == [retargetview.OTHER_EQUIPMENT,
                                                                       retargetview.OTHER_PARTS, retargetview.OUTSIDE]
    assert left_behind(None, [ARMOR, Target("armor", "m/004/001")]) == []

    # the original off: nothing to switch off, the new mod is made on
    m.set_enabled(mods["Helm"].id, False)
    dialog = RetargetDialog(root, app, mods["Helm"].id)
    try:
        pump(root, lambda: dialog.tree.get_children())
        assert shown(dialog.off_check.master) and not enabled(dialog.off_check) and not dialog.off_var.get()
        assert dialog.off_note.cget("text") == "원래 모드는 꺼져 있습니다. 새 모드는 켠 상태로 만듭니다."
        dialog.tree.selection_set("2")
        pump(root, lambda: enabled(dialog.create_button))
        dialog.ok()
        assert dialog.result.disable_original is False
    finally:
        if not dialog.closed:
            dialog.destroy()


def test_original_stays_on_when_other_game_files_stay_behind(env, monkeypatch):
    """Game files of no equipment the new mod leaves out (the armor's icon, its name in the game's text, a sound
    bank, a shader nothing moved uses, an unnamed pak entry) go out of the game with the original too: '원래
    모드 끄기' starts off and says so. The original equipment's own files (its avp, its sound bank, a file in
    its folder) do not count."""
    from mhwmm import retargetview
    from mhwmm.retargetview import RetargetDialog, left_behind

    unused, sound = "옮기는 파일이 쓰지 않습니다", "소리 파일은 옮기지 않습니다"
    icon = "natives/STM/GUI/ui_texture/tex000201/tex000201_0_IMLM4.tex.241106027"
    armor, blades = (Slot.of(ARMOR), Slot.of(LONA)), (Slot.of(WEAPON), Slot.of(BLADES_B))
    for dropped in ([(icon, unused)], [("natives/STM/GameDesign/Text/Excel_Equip/ArmorSeries.msg.23", unused)],
                    [("natives/STM/GameDesign/Common/Equip/ArmorData.user.3", unused)],
                    [("natives/STM/Sound/Wwise/Armor_Common.sbnk.1.x64", sound)],
                    [("natives/STM/Art/Model/Author/glow.mmtr.1", unused)],
                    [("Armor.pak#2EA41075C115A5A2", unused)]):
        assert left_behind(Plan(*armor, dropped=dropped), [ARMOR]) == [retargetview.OTHER_FILES], dropped
    own = [(AVP, "외형 설정 파일(_avp.user)은 옮기지 않습니다"), (CHARACTER + "ch03/004/001/3/glow.mmtr.1", unused)]
    assert left_behind(Plan(*armor, dropped=own), [ARMOR]) == []
    bank = "natives/STM/Sound/Wwise/it0200_0029_m.sbnk.1.x64"
    assert left_behind(Plan(*blades, dropped=[(bank, sound)]), [WEAPON]) == []  # the weapon's own sound
    assert left_behind(Plan(*blades, dropped=[(bank.replace("0029", "0031"), sound)]), [WEAPON]) == \
        [retargetview.OTHER_FILES]
    assert left_behind(Plan(*armor, dropped=[(icon, unused), ("x.dll", "게임 파일이 아닙니다")]), [ARMOR]) == \
        [retargetview.OTHER_FILES, retargetview.OUTSIDE]

    # in the dialog: 로나 갑옷 (only the original's own settings stay behind) with the armor's icon left out too
    app, m, mods, fake = env

    def plan(mod_id, source, target, parts=None, files=None):
        result = fake.plan(mod_id, source, target, parts, files)
        result.dropped.append((icon, unused))
        return result

    monkeypatch.setattr(m, "plan_retarget", plan)
    dialog = RetargetDialog(app.root, app, mods["Helm"].id)
    try:
        pump(app.root, lambda: dialog.tree.get_children())
        dialog.tree.selection_set("2")
        pump(app.root, lambda: enabled(dialog.create_button))
        assert not dialog.off_var.get()
        assert dialog.off_why.cget("text") == "원래 모드에는 새 모드에 들어가지 않는 파일(그 밖의 게임 파일)도 있습니다"
        dialog.ok()
        assert dialog.result.disable_original is False
    finally:
        if not dialog.closed:
            dialog.destroy()


def test_a_mod_with_options_says_only_the_chosen_ones_move(env):
    from mhwmm.retargetview import RetargetDialog

    app, m, mods, fake = env
    colors = mods["Colors"]
    lines = []
    for selected in ([colors.options[0]["id"]], [o["id"] for o in colors.options], []):
        m.set_options(colors.id, selected)
        dialog = RetargetDialog(app.root, app, colors.id)
        try:
            lines.append([dialog.options_note.cget("text")])
        finally:
            dialog.destroy()
    names = [o["name"] for o in colors.options]
    assert lines == [[f"지금 켠 옵션({names[0]})의 파일만 옮깁니다. 새 모드에는 옵션이 없습니다."],
                     [f"지금 켠 옵션({', '.join(names)})의 파일만 옮깁니다. 새 모드에는 옵션이 없습니다."],
                     ["켠 옵션이 없어 옵션 밖의 파일만 옮깁니다. 새 모드에는 옵션이 없습니다."]]
    dialog = RetargetDialog(app.root, app, mods["Helm"].id)
    try:
        assert dialog.options_note.cget("text") == ""
    finally:
        dialog.destroy()


def test_the_list_works_from_the_keyboard(env):
    app, m, mods, fake = env
    root = app.root
    from mhwmm.retargetview import RetargetDialog

    dialog = RetargetDialog(root, app, mods["Helm"].id)
    try:
        dialog.deiconify()
        pump(root, lambda: dialog.tree.get_children())
        assert dialog.tree.focus() == "0" and dialog.tree.selection() == ()  # (Tk's keys need a focus row)
        # Down in the search box: into the list, its first row picked
        dialog.search.focus_force()
        dialog.search_var.set("갑옷")
        pump(root, lambda: dialog.focus_get() is dialog.search)
        dialog.search.event_generate("<Down>")
        root.update()
        assert dialog.focus_get() is dialog.tree and dialog.tree.selection() == ("0",) and dialog.target == GANA
        # the arrows move on from there; Return makes the mod once it can be made
        dialog.tree.event_generate("<Down>")
        dialog.tree.event_generate("<Down>")
        root.update()
        assert dialog.target == LONA
        dialog.tree.event_generate("<Return>")  # (not planned yet: nothing happens)
        root.update()
        assert not dialog.closed
        pump(root, lambda: enabled(dialog.create_button))
        dialog.tree.event_generate("<Return>")
        root.update()
        assert dialog.closed and dialog.result.target == LONA
    finally:
        if not dialog.closed:
            dialog.destroy()

    dialog = RetargetDialog(root, app, mods["Helm"].id)
    try:
        dialog.deiconify()
        pump(root, lambda: dialog.tree.get_children())
        # Tab into the list: the first arrow key (or Return) picks the row the focus is on
        dialog.tree.focus_force()
        pump(root, lambda: dialog.focus_get() is dialog.tree)
        dialog.tree.event_generate("<Down>")
        root.update()
        assert dialog.tree.selection() == ("0",) and dialog.target == GANA
        # a search that keeps the picked one: Return in the search box goes back to it
        dialog.search.focus_force()
        dialog.search_var.set("가나")
        pump(root, lambda: dialog.focus_get() is dialog.search)
        dialog.search.event_generate("<Return>")
        root.update()
        assert dialog.focus_get() is dialog.tree and dialog.target == GANA and not dialog.closed
    finally:
        if not dialog.closed:
            dialog.destroy()


def test_no_worker_reads_the_mods_state(env):
    """The mods' state (mod_entries and its caches) is the GUI thread's: the warm-up, the dialog's scan and
    its plans work from files read on the GUI thread."""
    from mhwmm.retargetview import RetargetDialog

    app, m, mods, fake = env
    root = app.root
    app.warm_equipment()
    pump(root, lambda: not app._equip_warming)
    dialog = RetargetDialog(root, app, mods["Armor"].id)
    try:
        pump(root, lambda: dialog.tree.get_children())
        dialog.tree.selection_set("0")
        pump(root, lambda: enabled(dialog.create_button))
    finally:
        dialog.destroy()
    assert fake.plans and all(files is not None for files in fake.files)
    assert all(on_gui for on_gui, with_files in fake.sources_asked if not with_files)
    assert fake.reads and all(on_gui for _name, on_gui in fake.reads)


def test_create_makes_the_mod_and_shows_it(env, monkeypatch):
    from mhwmm import modtool, retargetview
    from mhwmm.manager import MaterialJob, MaterialRun

    app, m, mods, fake = env
    root = app.root
    armor = mods["Armor"].id
    called = []
    for name in ("check_materials_background", "warm_equipment", "request_apply"):
        monkeypatch.setattr(app, name, lambda name=name: called.append(name))
    opened = []
    answer = [retargetview.Choice(ARMOR, GANA, ("helm",), True, "Armor → 가나 갑옷")]

    def show(self):
        opened.append((self.mod_id, self.wanted, self.master, self.page_key))
        self.destroy()
        return answer[0]

    monkeypatch.setattr(modtool.ModToolDialog, "show", show)
    app.open_retarget(armor, ARMOR)
    assert app.busy == "retarget"
    pump(root, lambda: app.busy is None)
    assert opened == [(armor, ARMOR, root, "retarget")]
    assert fake.created == [(armor, ARMOR, GANA, ("helm",), True, True)]  # (made on)
    new = m.mods[-1]
    assert app.tree.selection() == (new.id,) and not m.get(armor).enabled and new.enabled
    assert called == ["request_apply", "check_materials_background", "warm_equipment"]
    made = f"새 모드를 만들었습니다: {new.name} · 원래 모드는 껐습니다"
    assert app.status_var.get() == made
    # its first material check tells of the shaders it lacks, as an install's does; the line that says what
    # became of the original stays through the check (its progress and its end replace only its own line)
    told = []
    monkeypatch.setattr(app, "_notify_shader_requirements", lambda ids: told.append(ids))
    monkeypatch.setattr(app, "_offer_materials", lambda: None)
    monkeypatch.setattr(m, "finish_materials", lambda run, jobs: [])
    job = MaterialJob(new.id, new.name, f"{new.installed_at}|{new.updated_at}", [])
    app._matfix_running = True
    app._matfix_status(f"재질 점검: {new.name}")
    assert app.status_var.get() == made
    app._on_materials_checked(MaterialRun("fp", False, {new.id: []}), [job], None)
    assert told == [[new.id]] and new.id not in app._first_check
    assert app.status_var.get() == made and not app._matfix_running
    app.status_var.set("준비")  # a check that shows its progress puts back "준비" at its end
    app._matfix_running = True
    app._matfix_status(f"재질 점검: {new.name}")
    assert app.status_var.get() == f"재질 점검: {new.name}"
    app._on_materials_checked(MaterialRun("fp", False, {new.id: []}), [job], None)
    assert app.status_var.get() == "준비"

    # the original left on
    answer[0] = answer[0]._replace(disable_original=False)
    m.set_enabled(armor, True)
    app.open_retarget(armor)
    pump(root, lambda: app.busy is None)
    assert fake.created[-1] == (armor, ARMOR, GANA, ("helm",), False, True) and m.get(armor).enabled
    assert app.tree.selection() == (m.mods[-1].id,) and len(m.mods) == 7
    assert app.status_var.get().endswith(" · 원래 모드도 켜 두었습니다")

    # a choice made while another task runs waits for it
    app.busy = "deploy"
    app._create_retarget(armor, answer[0])
    root.update()
    assert len(fake.created) == 2 and "끝나면" in app.status_var.get()
    app.busy = None
    pump(root, lambda: len(fake.created) == 3 and app.busy is None)

    # cancelled: nothing is made
    answer[0] = None
    app.open_retarget(armor)
    root.update()
    assert len(fake.created) == 3 and app.busy is None


def test_no_launch_while_the_mod_is_made(env, monkeypatch):
    """Making the mod ends with an apply: the game does not start meanwhile, nor while the mod waits for a
    running task (the apply's end would start the game asked for during it before the mod is made)."""
    from mhwmm.retargetview import Choice

    app, m, mods, fake = env
    root = app.root
    started = []
    monkeypatch.setattr(app, "_start_game", lambda *a, **k: started.append(len(fake.created)))
    app.busy = "retarget"
    app._start_when_ready()
    assert started == [] and app.status_var.get() == "작업이 끝난 뒤에 다시 시도하세요."
    app.busy = None

    m.settings.auto_apply = False  # (no apply after it)
    armor, helm = mods["Armor"].id, mods["Helm"].id
    app.busy = "deploy"  # the dialog closed while an apply runs
    app._create_retarget(armor, Choice(ARMOR, GANA, None, True, ""))
    app.busy = None  # the apply is done; the launch asked for meanwhile comes first
    m.dirty = False
    app._start_when_ready()
    assert started == [] and app.status_var.get() == "작업이 끝난 뒤에 다시 시도하세요."
    pump(root, lambda: fake.created and app.busy is None)
    assert app._retarget_waiting == 0
    m.dirty = False
    app._start_when_ready()
    assert started == [1]

    # one given up (its mod removed meanwhile) no longer holds the game back
    app.busy = "deploy"
    app._create_retarget(helm, Choice(ARMOR, GANA, None, True, ""))
    m.remove(helm)
    app.busy = None
    pump(root, lambda: app._retarget_waiting == 0)
    m.dirty = False
    app._start_when_ready()
    assert started == [1, 1] and len(fake.created) == 1


def test_blocked_during_the_search_and_while_busy(env, monkeypatch):
    from mhwmm import bisect, gui, retargetview

    app, m, mods, fake = env
    opened, told = [], []

    class Dialog:
        def __init__(self, parent, app, mod_id, page=None, source=None, pending=None):
            self.mod_id = mod_id
            opened.append(mod_id)

        def show(self):
            return retargetview.Choice(ARMOR, GANA, None, True, "Armor → 가나 갑옷")

    patch_window(monkeypatch, Dialog)
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, text, parent=None: told.append((title, text, parent)))
    armor = mods["Armor"].id
    app._bisect_preparing = True  # 원인 모드 찾기
    app.open_retarget(armor)
    assert opened == [] and "원인" in app.status_var.get() and told == [(bisect.TITLE, bisect.BLOCKED, app.root)]
    app._create_retarget(armor, Dialog(None, app, armor).show())  # (chosen before it started)
    assert fake.created == [] and app.busy is None
    # from another window (장비 외형) the reason is told over that window
    window = tk.Toplevel(app.root)
    try:
        told.clear()
        app.open_retarget(armor, ARMOR, parent=window)
        assert told == [(bisect.TITLE, bisect.BLOCKED, window)]
        app._bisect_preparing = False
        opened.clear()
        told.clear()
        app.busy = "install"
        app.open_retarget(armor)
        assert opened == [] and told == [] and app.status_var.get() == "작업이 끝난 뒤에 다시 시도하세요."
        app.open_retarget(armor, ARMOR, parent=window)
        assert opened == [] and told == [(gui.RETARGET_TITLE, "다른 작업이 진행 중입니다. 작업이 끝난 뒤에 다시 "
                                                              "시도하세요.", window)]
        app.busy = None
        app.open_retarget(armor, ARMOR, parent=window)
        assert opened == [armor]
    finally:
        app.busy = None
        window.destroy()


def test_offered_for_mods_with_game_files(env, monkeypatch):
    from mhwmm import equipview, gui

    app, m, mods, fake = env
    root = app.root
    opened = []

    class Dialog:
        def __init__(self, parent, app, mod_id, page=None, source=None, pending=None):
            opened.append((mod_id, source, parent, page))

        def show(self):
            return None

    patch_window(monkeypatch, Dialog)

    # the main list's context menu: 모드 수정 for every mod with files, also a script mod (its tools that need game
    # files are off in the window), at once (the window says when nothing of them can be moved)
    assert not app._equip_warming
    app.context_menu.tk_popup = lambda *a: None
    app.context_menu.add_command(label="-")  # (Python 3.10's tkinter cannot clear an empty menu of Tk 9)

    class Event:
        x = y = -50
        x_root = y_root = 0

    def entries(mod_id) -> dict:
        app.tree.selection_set(mod_id)
        app._on_context(Event())
        menu = app.context_menu
        return {menu.entrycget(i, "label"): i for i in range(menu.index("end") + 1) if menu.type(i) == "command"}

    found = entries(mods["Armor"].id)
    assert gui.MOD_TOOL_ACTION == "모드 수정..." and gui.RETARGET_ACTION not in found
    assert list(found).index(gui.MOD_TOOL_ACTION) == list(found).index("장비 외형 보기") + 1
    assert gui.MOD_TOOL_ACTION in entries(mods["Palico"].id)
    assert gui.MOD_TOOL_ACTION in entries(mods["Script"].id)
    app.context_menu.invoke(entries(mods["Armor"].id)[gui.MOD_TOOL_ACTION])
    assert opened == [(mods["Armor"].id, None, root, None)]  # (the tool that fits the mod)

    # the 장비 외형 window: a link under each mod of an armor or a weapon (its own words), opening the same window
    # over it on that mod and equipment
    monkeypatch.setattr(equipview, "Image", None)  # (no 3D pictures)
    app.show_equipment()
    window = app.equipment_window
    try:
        pump(root, lambda: window.overview is not None)

        def links():
            return [w for w in window._descendants(window.detail)
                    if w.winfo_class() == "TButton" and w.cget("text") == gui.RETARGET_ACTION]

        assert {i.target for i in window.overview.items} >= {ARMOR, WEAPON, Target("palico", "062")}
        window.select(WEAPON)
        found = links()
        assert len(found) == 1
        found[0].invoke()
        assert opened[-1] == (mods["Armor"].id, WEAPON, window, "retarget")
        window.select(ARMOR)  # (three mods change it)
        assert len(links()) == 3
        window.select(Target("palico", "062"))
        assert links() == []
    finally:
        window.close()


def root_update(app):
    for _ in range(3):
        app.root.update()


def test_the_toolbar_opens_the_mod_tool(env, monkeypatch):
    """모드 수정 on the toolbar: the window on the first selected mod it can work on, else on the list's first such
    mod; refused during 원인 모드 찾기 and while another task runs (as the context menu's entry)."""
    from mhwmm import bisect, gui

    app, m, mods, fake = env
    opened, told = [], []

    class Dialog:
        def __init__(self, parent, app, mod_id, page=None, source=None, pending=None):
            self.mod_id = mod_id
            opened.append((mod_id, source, parent))

        def show(self):
            return None

    patch_window(monkeypatch, Dialog)
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, text, parent=None: told.append((title, text)))
    armor, helm, palico, script = (mods[name].id for name in ("Armor", "Helm", "Palico", "Script"))
    button = app.buttons["modtool"]
    app.tree.selection_set(helm)
    button.invoke()
    assert opened == [(helm, None, app.root)]
    app.tree.selection_set((script, palico))  # (the first of them in the list's order)
    button.invoke()
    assert opened[-1][0] == palico
    app.tree.selection_set((script,))  # only that: the window on it (it says what it cannot do with it)
    button.invoke()
    assert opened[-1][0] == script
    app.tree.selection_set(())  # nothing selected: the first the list shows
    button.invoke()
    assert opened[-1][0] == armor
    app.search_var.set("Helm")
    root_update(app)
    button.invoke()
    assert opened[-1][0] == helm
    app.search_var.set("")
    root_update(app)
    assert len(opened) == 5 and told == []

    app._bisect_preparing = True
    button.invoke()
    assert len(opened) == 5 and told == [(bisect.TITLE, bisect.BLOCKED)]
    app._bisect_preparing = False
    app.busy = "install"
    button.invoke()
    assert len(opened) == 5 and app.status_var.get() == "작업이 끝난 뒤에 다시 시도하세요."
    app.busy = None


def test_another_mod_picked_in_the_window(env, monkeypatch):
    """The 모드 box lists the mods the window can work on (in the list's order; (꺼짐) for one that is off); another
    one picked switches the window to it in place: nothing chosen, its sources read again, its options line and
    원래 모드 끄기. What was still being worked out for the mod before is dropped when it comes back (its plan of
    the same move is not taken for this mod's)."""
    from mhwmm.retargetview import RetargetDialog

    app, m, mods, fake = env
    root = app.root
    armor, helm, colors = mods["Armor"].id, mods["Helm"].id, mods["Colors"].id
    m.set_enabled(helm, False)
    held, release, made = [], threading.Event(), {}

    def plan(mod_id, source, target, parts=None, files=None):  # the first mod's plan waits until let go
        if mod_id == armor:
            held.append(target)
            release.wait(30)
        made[mod_id] = fake.plan(mod_id, source, target, parts, files)
        return made[mod_id]

    monkeypatch.setattr(m, "plan_retarget", plan)
    dialog = RetargetDialog(root, app, armor, ARMOR)
    try:
        dialog.deiconify()
        assert dialog.title() == "모드 수정"
        assert list(dialog.mod_box.cget("values")) == ["Armor", "Helm (꺼짐)", "Palico", "Script",
                                                       mods["Colors"].name]
        assert dialog.mod_box.current() == 0 and str(dialog.mod_box.cget("state")) == "readonly"
        size = (dialog.winfo_reqwidth(), dialog.winfo_reqheight())
        pump(root, lambda: dialog.tree.get_children())
        assert dialog.mod_box.tk_focusNext() is dialog.source_box  # (the mod first, then its equipment)
        dialog.tree.selection_set("0")  # 가나 갑옷: planned on another thread, held there
        pump(root, lambda: held)
        before = dialog._generation

        dialog.mod_box.current(1)
        dialog.mod_box.event_generate("<<ComboboxSelected>>")
        assert dialog.mod_id == helm and dialog.mod is m.get(helm) and dialog._generation == before + 1
        assert dialog.assigned == {} and dialog.plans == {} and dialog._planning is None and part_table(dialog) == []
        assert str(dialog.source_box.cget("state")) == "disabled" and dialog.source_var.get() == "읽는 중..."
        assert dialog.shown_text() == "장비 정보를 읽는 중..." and not enabled(dialog.create_button)
        assert rows(dialog) == [] and dialog.kind_note.cget("text") == "" and dialog.pick_hint.cget("text") == ""
        assert not enabled(dialog.off_check) and not dialog.off_var.get()  # (it is off: nothing to switch off)
        assert dialog.off_note.cget("text") == "원래 모드는 꺼져 있습니다. 새 모드는 켠 상태로 만듭니다."
        pump(root, lambda: dialog.source == ARMOR and dialog.tree.get_children())
        assert list(dialog.source_box.cget("values")) == ["방어구 004-001 (여성)"]  # (Helm changes no weapon)
        assert part_table(dialog) == [("머리", "옮기지 않음", ""), ("몸통", "옮기지 않음", "")]
        assert dialog.parts_tree.selection() == ("helm", "body")
        # the first mod's sources coming back late do not fill this one's
        dialog._done("sources", None, ([WEAPON], ["쌍검 모델 00-0029 (쌍검)"], {WEAPON: []}), before)
        assert dialog.sources == [ARMOR] and list(dialog.source_box.cget("values")) == ["방어구 004-001 (여성)"]

        # the same move for this mod: planned for it at once (not waiting for the first mod's), and the first
        # mod's plan coming back after it is dropped
        dialog.tree.selection_set("0")
        pump(root, lambda: enabled(dialog.create_button))
        assert fake.plans[-1] == (helm, ARMOR, GANA, None) and "새 모드: Helm → 가나 갑옷" in dialog.shown_text()
        came = []

        def done(*args, _real=dialog._done):
            came.append(args)
            _real(*args)

        dialog._done = done
        release.set()
        pump(root, lambda: [args for args in came if args[0] == "plan"])
        assert [args[3] for args in came if args[0] == "plan"] == [before] and made[armor] is not made[helm]
        assert dialog.plans == {(ARMOR, GANA, None): made[helm]}
        assert "새 모드: Helm → 가나 갑옷" in dialog.shown_text() and enabled(dialog.create_button)

        # Return on the 모드 box does not make the mod (the dialog's Return would)
        dialog.mod_box.focus_force()
        pump(root, lambda: dialog.focus_get() is dialog.mod_box)
        dialog.mod_box.event_generate("<Return>")
        root.update()
        assert not dialog.closed and dialog.mod_id == helm

        # a mod with options: its line above the columns (always there: the window keeps its size, the list its
        # height); one that is on: 원래 모드 끄기 usable
        dialog.mod_box.current(4)
        dialog.mod_box.event_generate("<<ComboboxSelected>>")
        assert dialog.mod_id == colors and "옵션" in dialog.options_note.cget("text")
        packed = dialog.pages["retarget"].frame.pack_slaves()
        assert packed.index(dialog.options_note.master) < packed.index(dialog.kind_note.master)
        assert int(dialog.tree.cget("height")) == dialog.rows
        assert enabled(dialog.off_check) and "끄면 원래 장비는" in dialog.off_note.cget("text")
        assert packed.index(dialog.off_row) + 1 == packed.index(dialog.off_note.master) == len(packed) - 1
        pump(root, lambda: dialog.source == ARMOR and dialog.tree.get_children())
        assert dialog.off_var.get()  # (one equipment, until a plan says more)
        assert (dialog.winfo_reqwidth(), dialog.winfo_reqheight()) == size
        dialog.mod_box.current(0)
        dialog.mod_box.event_generate("<<ComboboxSelected>>")
        assert dialog.mod_id == armor and dialog.options_note.cget("text") == ""
        assert int(dialog.tree.cget("height")) == dialog.rows and dialog.wanted is None
        pump(root, lambda: dialog.source == ARMOR and dialog.tree.get_children())  # (the first: none wanted)
        assert not dialog.off_var.get()  # (it changes two pieces of equipment)
    finally:
        release.set()
        if not dialog.closed:
            dialog.destroy()


def test_the_mod_box_wheel_letters_and_list(env):
    """The wheel over the closed 모드 or 원래 장비 box changes nothing (it would drop what was chosen); a letter
    typed on the 모드 box picks the next mod whose name starts with it, in its open list it moves there; the
    list is as wide as its longest name."""
    from mhwmm.retargetview import MOD_BOX_STYLE, RetargetDialog

    app, m, mods, fake = env
    root = app.root
    armor, palico, colors = mods["Armor"].id, mods["Palico"].id, mods["Colors"].id
    dialog = RetargetDialog(root, app, armor)
    try:
        dialog.deiconify()
        pump(root, lambda: dialog.tree.get_children())
        dialog.tree.selection_set("0")
        pump(root, lambda: enabled(dialog.create_button))
        for box in (dialog.mod_box, dialog.source_box):
            for key, extra in (("<MouseWheel>", {"delta": -120}), ("<Button-5>", {})):
                box.event_generate(key, **extra)
                root.update()
        assert dialog.mod_id == armor and dialog.source == ARMOR and dialog.assigned

        dialog.mod_box.focus_force()
        pump(root, lambda: dialog.focus_get() is dialog.mod_box)
        dialog.mod_box.event_generate("<KeyPress>", keysym="c")
        root.update()
        assert dialog.mod_id == colors and dialog.mod_box.current() == 4
        dialog.mod_box.event_generate("<KeyPress>", keysym="P")
        root.update()
        assert dialog.mod_id == palico
        assert dialog._next_named("p", 2) == 2 and dialog._next_named("z", 0) is None
        assert dialog._next_named(" ", 0) is None and dialog._next_named("", 0) is None

        dialog.mod_box.configure(values=[*dialog.mod_box.cget("values")[:-1], "A" * 200])  # (a long name)
        dialog._widen_list()
        offset = [int(v) for v in str(root.tk.call("ttk::style", "lookup", MOD_BOX_STYLE, "-postoffset")).split()]
        assert offset[:2] == [0, 0] and offset[3] == 0 and offset[2] > 0
        assert dialog.mod_box.winfo_rootx() + dialog.mod_box.winfo_width() + offset[2] <= dialog.winfo_screenwidth()
        listbox = f"{root.tk.call('ttk::combobox::PopdownWindow', dialog.mod_box)}.f.l"
        root.tk.call("ttk::combobox::Post", dialog.mod_box)  # (keys reach the list only while it is shown)
        root.update()
        root.tk.call(listbox, "activate", 0)
        root.tk.call("event", "generate", listbox, "<KeyPress>", "-keysym", "h")
        root.update()
        assert int(root.tk.call(listbox, "index", "active")) == 1 and dialog.mod_id == palico  # (not picked yet)
        root.tk.call("ttk::combobox::Unpost", dialog.mod_box)
        root.update()
    finally:
        if not dialog.closed:
            dialog.destroy()


def test_the_mod_is_made_from_the_mod_picked_last(env, monkeypatch):
    """Opened on one mod, another picked in the window: the new mod is made of the one picked."""
    from mhwmm import modtool

    app, m, mods, fake = env
    root = app.root
    armor, helm = mods["Armor"].id, mods["Helm"].id
    for name in ("check_materials_background", "warm_equipment", "request_apply"):
        monkeypatch.setattr(app, name, lambda: None)
    opened = []

    def show(self):  # 다른 장비로 옮기기, 모드: Helm, then 가나 갑옷 for every part, 새 모드 만들기
        opened.append(self.mod_id)
        self.select("retarget")
        pump(root, lambda: self.tree.get_children())
        self.mod_box.current(self.mod_ids.index(helm))
        self.mod_box.event_generate("<<ComboboxSelected>>")
        pump(root, lambda: self.source == ARMOR and self.tree.get_children())
        self.tree.selection_set("0")
        pump(root, lambda: enabled(self.create_button))
        self.ok()
        return self.result

    monkeypatch.setattr(modtool.ModToolDialog, "show", show)
    app.tree.selection_set(armor)
    app.buttons["modtool"].invoke()
    pump(root, lambda: fake.created and app.busy is None)
    assert opened == [armor]
    assert fake.created == [(helm, ARMOR, GANA, None, False, True)]  # (a script stays behind: Helm stays on)
    new = m.mods[-1]
    assert app.tree.selection() == (new.id,) and m.get(helm).enabled and m.get(armor).enabled
    assert app.status_var.get() == f"새 모드를 만들었습니다: {new.name} · 원래 모드도 켜 두었습니다"
