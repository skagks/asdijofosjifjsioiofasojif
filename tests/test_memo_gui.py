"""모드 메모 in the window: the ✎ mark and the row's tooltip, search and the 메모 있음 filter, the memo on the info tab
(and '＋ 메모 추가' without one), MemoDialog (saving, Enter as a new line, cancelling, clearing, the limit),
App.edit_memo (nothing to apply), its entry points, MemoOverviewDialog and both themes. Synthetic mods only."""

from __future__ import annotations

import gc
import logging
import queue
import re
import time

import pytest

from conftest import make_zip

tk = pytest.importorskip("tkinter")

LONG = "\n".join([f"{n}번째 줄" for n in range(1, 12)] + ["가" * 120])  # 12 lines, the last one too long


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("화면(display)이 없어 GUI 테스트를 건너뜁니다")
    r.maxsize(4000, 3000)  # (Windows CI's screen is small: windows keep the size they ask for)
    yield r
    gc.collect()  # Tk variables of the test must go before the interpreter does (else Python 3.10 can crash)
    try:
        r.destroy()
    except tk.TclError:
        pass


@pytest.fixture(autouse=True)
def answer_dialogs(monkeypatch):
    """A message box left open would block the test, so answer them all."""
    from mhwmm import gui

    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: None)
    for name in ("askyesno", "askyesnocancel"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: True)
    monkeypatch.setattr(gui.App, "warm_equipment", lambda self: None)
    monkeypatch.setattr(gui.App, "precheck_background", lambda self: None)


def pump(root, until, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if until():
            return
        time.sleep(0.02)
    raise AssertionError("시간 초과")


def install(manager, tmp_path, name: str, files: dict | None = None):
    with manager.prepare_install(make_zip(tmp_path / f"{name}.zip", files or {f"natives/STM/{name}.tex": name})) as s:
        return manager.install_package(s, s.variants)


@pytest.fixture
def mods(manager, tmp_path):
    """A (no memo), B (a memo), Opts (no option chosen: a problem, with a memo)."""
    a = install(manager, tmp_path, "A")
    b = install(manager, tmp_path, "B")
    opts = install(manager, tmp_path, "Opts", {"Opts/One/natives/STM/o.tex": "1", "Opts/Two/natives/STM/o.tex": "2"})
    manager.set_memo(b.id, "허리 텍스처가 깨지면 끄기\n받은 곳: Nexus")
    manager.set_memo(opts.id, "옵션은 나중에")
    return a, b, opts


def app_of(root, manager):
    from mhwmm import gui

    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: True)
    return app


def shown(dialog):
    """A dialog's show() returning at once (its window open, with the grab)."""
    dialog.wait_window = lambda *a: None
    dialog.show()
    pump(dialog, lambda: dialog.winfo_ismapped())
    return dialog


def key(widget, name):
    """A key pressed on ``widget`` (with the keyboard focus)."""
    widget.focus_force()
    pump(widget, lambda: str(widget.tk.call("focus")) == str(widget))
    widget.event_generate(name)
    widget.update()


def click_tag(text, tag):
    """A click on the first character with ``tag`` in a Text (Tk picks the tag under the mouse on its motion)."""
    first = text.tag_ranges(tag)[0]
    text.see(first)
    pump(text, lambda: text.bbox(first) is not None)
    x, y, _w, h = text.bbox(first)
    text.event_generate("<Motion>", x=x + 1, y=y + h // 2)
    text.event_generate("<Button-1>", x=x + 1, y=y + h // 2)
    text.update()


def tagged(text, tag) -> list[str]:
    ranges = text.tag_ranges(tag)
    return [text.get(ranges[i], ranges[i + 1]) for i in range(0, len(ranges), 2)]


def test_the_list_marks_mods_with_a_memo_and_the_tooltip_shows_it(root, manager, mods):
    from mhwmm import gui

    a, b, opts = mods
    app = app_of(root, manager)
    names = {mod.id: app.tree.item(mod.id, "values")[1] for mod in mods}
    assert names == {a.id: "A", b.id: "✎ B", opts.id: "⚠ ✎ Opts"}  # (a problem's mark first)

    assert app._row_tooltip(a.id) == ""
    assert app._row_tooltip(b.id) == "✎ 메모\n허리 텍스처가 깨지면 끄기\n받은 곳: Nexus"
    problem, memo = app._row_tooltip(opts.id).split("\n\n")
    assert problem.startswith("⚠ ") and memo == "✎ 메모\n옵션은 나중에"

    manager.set_memo(a.id, LONG)
    lines = app._row_tooltip(a.id).split("\n")
    assert lines[0] == "✎ 메모" and lines[1:11] == [f"{n}번째 줄" for n in range(1, 11)]
    assert lines[-1] == "… (정보 탭에서 모두 보기)" and len(lines) == 12
    manager.set_memo(a.id, "가" * 120)
    lines = app._row_tooltip(a.id).split("\n")
    assert lines[1] == "가" * (gui.MEMO_TIP_CHARS - 1) + "…" and lines[2] == "… (정보 탭에서 모두 보기)"

    # the tooltip never gets wider than its wrap length
    app.refresh()
    tip = gui.RowTooltip(app.theme, app.tree, app._row_tooltip)
    tip.row = a.id
    tip._show()
    label = tip.tip.winfo_children()[0]
    assert int(str(label.cget("wraplength"))) == app.theme.px(460)
    assert label.cget("text").startswith("✎ 메모\n")
    tip._hide()


def test_a_long_memo_tooltip_stays_on_the_screen(root, manager, mods):
    """A row low on the screen whose tooltip (a long memo) would run past the bottom of the work area gets it above
    the row, all of it on the screen; with room below the row it stays below."""
    from mhwmm import gui

    _a, _b, opts = mods
    manager.set_memo(opts.id, LONG)
    app = app_of(root, manager)
    tree, px = app.tree, app.theme.px

    def row_box() -> tuple[int, int]:
        _x, y, _w, h = tree.bbox(opts.id)
        return tree.winfo_rooty() + y, tree.winfo_rooty() + y + h

    def move(bottom: int):
        """The window moved so that the row's bottom is at ``bottom`` on the screen."""
        x, y = map(int, re.search(r"\+(-?\d+)\+(-?\d+)$", root.wm_geometry()).groups())
        root.wm_geometry(f"+{x}+{y + bottom - row_box()[1]}")
        pump(root, lambda: row_box()[1] == bottom)

    def tooltip() -> tuple[int, int]:
        tip = gui.RowTooltip(app.theme, tree, app._row_tooltip)
        tip.row = opts.id
        tip._show()
        tip.tip.update_idletasks()
        _w, height, _x, y = map(int, re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", tip.tip.wm_geometry()).groups())
        tip._hide()
        return y, height

    _left, top, _right, bottom = gui._work_area(tree, 0, 0)
    move(bottom - px(10))
    y, height = tooltip()
    assert row_box()[1] + px(2) + height > bottom  # (below the row it would run off the screen)
    assert top <= y and y + height <= row_box()[0]  # above the row, all of it on the screen
    move(top + (bottom - top) // 3)
    y, height = tooltip()
    assert y == row_box()[1] + px(2) and y + height <= bottom


def test_search_and_the_memo_filter(root, manager, mods):
    from mhwmm import gui

    a, b, opts = mods
    app = app_of(root, manager)
    assert list(gui.FILTERS)[-1] == gui.FILTER_MEMO == "메모 있음"
    app.search_var.set("NEXUS")  # (the memo's words, case left aside)
    assert list(app.tree.get_children()) == [b.id]
    app.search_var.set("")
    app.filter_var.set(gui.FILTER_MEMO)
    app.refresh()
    assert list(app.tree.get_children()) == [b.id, opts.id]
    manager.set_memo(b.id, "")
    app.refresh()
    assert list(app.tree.get_children()) == [opts.id]


def test_the_info_tab_shows_the_memo_first(root, manager, mods, monkeypatch):
    a, b, opts = mods
    manager.set_memo(b.id, "**굵게가 아님** \\n 그대로\n\n셋째 줄")  # (plain text: neither markup nor "\n" escapes)
    app = app_of(root, manager)
    app.tree.selection_set(b.id)
    app._update_details()
    info = app.info_text
    shown_text = info.get("1.0", "end")
    assert tagged(info, "memo_title") == ["✎ 메모"] and tagged(info, "memo_edit") == ["편집"]
    assert f"{b.memo_at.replace('T', ' ')[:16]} 수정" in shown_text
    assert tagged(info, "memo") == ["**굵게가 아님** \\n 그대로\n\n셋째 줄\n"]
    assert tagged(info, "memo_top") == ["**굵게가 아님** \\n 그대로\n"] and tagged(info, "memo_end") == ["셋째 줄\n"]
    assert not info.tag_ranges("memo_add")
    # before the key/value rows
    assert info.compare(info.tag_ranges("memo")[0], "<", info.search("파일\t", "1.0"))

    calls = []
    monkeypatch.setattr(app, "edit_memo", lambda *a: calls.append(a))
    click_tag(info, "memo_edit")
    assert calls == [()]

    app.tree.selection_set(a.id)
    app._update_details()
    assert tagged(info, "memo_add") == ["＋ 메모 추가"] and not info.tag_ranges("memo")
    assert info.get(f"{info.tag_ranges('memo_add')[0]} linestart", info.tag_ranges("memo_add")[0]) == "메모\t"
    click_tag(info, "memo_add")
    assert calls == [(), ()]

    app.tree.selection_set(a.id, b.id)  # several mods: no memo
    app._update_details()
    assert not info.tag_ranges("memo_add") and not info.tag_ranges("memo")


def test_memo_dialog_saves_with_ctrl_enter_and_ctrl_s_not_enter(root, manager, mods):
    from mhwmm import gui

    _a, b, _opts = mods
    app = app_of(root, manager)
    dialog = shown(gui.MemoDialog(root, app.theme, b))
    assert dialog.title() == "메모" and dialog.text.get("1.0", "end-1c") == b.memo
    assert dialog.count.cget("text") == f"{len(b.memo)} / 5,000자"
    assert dialog.text.index("insert") == dialog.text.index("end-1c")  # (the caret at the end)
    key(dialog.text, "<Return>")  # a new line, not a save
    assert dialog.winfo_exists() and dialog.text.get("1.0", "end-1c") == b.memo + "\n"
    dialog.text.insert("end", "  더한 줄  \n\n")
    dialog.update()
    assert dialog.count.cget("text") == f"{len(b.memo) + 7} / 5,000자"  # (as it will be kept)
    key(dialog.text, "<Control-Return>")
    assert dialog.result == b.memo + "\n  더한 줄" and not dialog.winfo_exists()

    dialog = shown(gui.MemoDialog(root, app.theme, b))
    dialog.text.insert("1.0", "앞에 ")
    key(dialog.text, "<Control-s>")
    assert dialog.result == "앞에 " + b.memo


def test_memo_dialog_cancel_clear_and_limit(root, manager, mods, monkeypatch):
    from mhwmm import gui

    a, b, _opts = mods
    app = app_of(root, manager)
    asked, answer = [], [False]
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **k: (asked.append(args), answer[0])[1])

    dialog = shown(gui.MemoDialog(root, app.theme, b))
    dialog.text.insert("end", "   \n")  # (blank: not a change)
    dialog.cancel()
    assert asked == [] and dialog.result is None and not dialog.winfo_exists()

    dialog = shown(gui.MemoDialog(root, app.theme, b))
    dialog.text.insert("end", " 고침")
    key(dialog.text, "<Escape>")
    assert asked == [("메모", "고친 메모를 저장하지 않고 닫을까요?")] and dialog.winfo_exists()  # 아니요: stays
    answer[0] = True
    assert dialog.protocol("WM_DELETE_WINDOW")  # (closing the window asks the same: Dialog.cancel)
    dialog.cancel()
    assert dialog.result is None and not dialog.winfo_exists()

    asked.clear()
    dialog = shown(gui.MemoDialog(root, app.theme, b))
    buttons = [w.cget("text") for row in dialog.body.winfo_children() for w in row.winfo_children()
               if str(w.winfo_class()) == "TButton"]
    assert sorted(buttons) == sorted(["취소", "저장", "메모 지우기"])
    dialog.clear()
    assert asked == [("메모 지우기", "'B'의 메모를 지울까요?")] and dialog.result == ""

    dialog = shown(gui.MemoDialog(root, app.theme, a))  # no memo yet: nothing to clear
    buttons = [w.cget("text") for row in dialog.body.winfo_children() for w in row.winfo_children()
               if str(w.winfo_class()) == "TButton"]
    assert "메모 지우기" not in buttons and dialog.count.cget("text") == "0 / 5,000자"
    warned = []
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *args, **k: warned.append(args))
    dialog.text.insert("1.0", "x" * 5001)
    dialog.update()
    assert dialog.count.cget("text") == "5,001 / 5,000자" and str(dialog.count.cget("style")) == "EditWarn.TLabel"
    dialog.save()
    assert warned == [("메모", "메모는 5,000자까지 쓸 수 있습니다 (지금 5,001자).")] and dialog.winfo_exists()
    dialog.text.delete("1.0", "1.1")
    dialog.update()
    assert str(dialog.count.cget("style")) == "Muted.TLabel"
    dialog.save()
    assert dialog.result == "x" * 5000 and not dialog.winfo_exists()


def test_edit_memo_keeps_it_and_applies_nothing(root, manager, mods, monkeypatch, caplog):
    from mhwmm import gui

    a, b, _opts = mods
    app = app_of(root, manager)
    revision, dirty, stamp = manager.revision, manager.dirty, a.stamp
    applied = []
    monkeypatch.setattr(app, "request_apply", lambda: applied.append(True))
    answers = ["새 메모  \n\n", None, ""]
    seen = []

    def show(self):
        seen.append(self.initial)
        self.destroy()
        return answers.pop(0)

    monkeypatch.setattr(gui.MemoDialog, "show", show)
    app.tree.selection_set(a.id)
    with caplog.at_level(logging.INFO, logger="mhwmm.gui"):
        app.edit_memo()  # the selected mod
    assert a.memo == "새 메모" and a.memo_at and manager.get(a.id).memo == "새 메모"
    assert app.tree.item(a.id, "values")[1] == "✎ A" and "메모를 저장했습니다: A" in caplog.text
    assert (manager.revision, manager.dirty, a.stamp) == (revision, dirty, stamp) and applied == []
    assert tuple(app.tree.selection()) == (a.id,) and "새 메모" in app.info_text.get("1.0", "end")

    app.edit_memo(b.id)  # cancelled
    assert b.memo.startswith("허리") and seen == ["", b.memo]
    with caplog.at_level(logging.INFO, logger="mhwmm.gui"):
        app.edit_memo(b.id)  # cleared
    assert b.memo == "" and b.memo_at == "" and app.tree.item(b.id, "values")[1] == "B"
    assert "메모를 지웠습니다: B" in caplog.text and applied == []

    def too_long(self):
        self.destroy()
        return "x" * 6000

    warned = []
    monkeypatch.setattr(gui.MemoDialog, "show", too_long)
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *args, **k: warned.append(args))
    app.edit_memo(a.id)
    assert a.memo == "새 메모" and warned and "5,000자" in warned[0][1]

    app.busy = "install"  # (a task that changes the mods: wait)
    monkeypatch.setattr(gui.MemoDialog, "show", lambda self: pytest.fail("열리면 안 됨"))
    app.edit_memo(a.id)
    app.busy = None
    app.edit_memo("없는 모드")  # (gone meanwhile: nothing)


def test_context_menu_ctrl_m_and_removing(root, manager, mods, monkeypatch):
    a, b, _opts = mods
    app = app_of(root, manager)
    monkeypatch.setattr(app.context_menu, "tk_popup", lambda *args: None)
    app.context_menu.add_command(label="-")  # (Python 3.10's tkinter cannot read Tk 9's index of an empty menu)

    class Event:
        x = y = -1
        x_root = y_root = 0

    def entries():
        menu = app.context_menu
        return {menu.entrycget(i, "label"): menu.entrycget(i, "accelerator")
                for i in range(menu.index("end") + 1) if menu.type(i) == "command"}

    app.tree.selection_set(b.id)
    app._on_context(Event())
    labels = list(entries())
    assert entries()["메모 편집..."] == "Ctrl+M" and labels.index("메모 편집...") == labels.index("이름 변경") + 1
    app.tree.selection_set(a.id)
    app._on_context(Event())
    assert "메모 추가..." in entries()
    app.tree.selection_set(a.id, b.id)
    app._on_context(Event())
    assert not {"메모 추가...", "메모 편집..."} & set(entries())

    calls = []
    monkeypatch.setattr(app, "edit_memo", lambda *args: calls.append(args))
    app.tree.selection_set(b.id)
    app._on_context(Event())
    app.context_menu.invoke("메모 편집...")
    assert calls == [(b.id,)]
    for name in ("<Control-m>", "<Control-M>"):  # (Caps Lock too)
        key(app.tree, name)
    assert calls == [(b.id,), (), ()]

    asked = []
    monkeypatch.setattr("mhwmm.gui.messagebox.askyesno", lambda *args, **k: (asked.append(args[1]), False)[1])
    app.tree.selection_set(a.id)
    app.remove_selected()
    app.tree.selection_set(a.id, b.id)
    app.remove_selected()
    assert not asked[0].endswith("메모도 함께 지워집니다.")
    assert asked[1].endswith("\n\n메모를 적은 모드는 메모도 함께 지워집니다.")


def test_the_menu_and_the_memo_overview(root, manager, mods, monkeypatch):
    from mhwmm import gui

    a, b, opts = mods
    manager.set_enabled(opts.id, False)  # (switched off: said so)
    app = app_of(root, manager)
    labels = [app.main_menu.entrycget(i, "label") for i in range(app.main_menu.index("end") + 1)
              if app.main_menu.type(i) == "command"]
    assert labels.index("메모 모아 보기") == labels.index("충돌 한눈에 보기") + 1

    dialog = shown(gui.MemoOverviewDialog(root, app.theme, manager.mods))
    text = dialog.text.get("1.0", "end")
    assert "메모를 적은 모드 2개 (목록 순서)" in [w.cget("text") for w in dialog.body.winfo_children()
                                          if str(w.winfo_class()) == "TLabel"]
    assert "A" not in tagged(dialog.text, "name") and tagged(dialog.text, "name") == ["2. B", "3. Opts"]
    assert text.index("2. B") < text.index("3. Opts") and "3. Opts  · 꺼짐" in text
    assert tagged(dialog.text, "memo") == [b.memo + "\n", "옵션은 나중에\n"]
    assert dialog.minsize() == (app.theme.px(560), app.theme.px(420))
    assert dialog.body.winfo_manager() == "place"  # (made bigger: the page keeps its size while the edge is dragged)
    click_tag(dialog.text, "memo_edit_3")
    assert dialog.result == ("edit", opts.id) and not dialog.winfo_exists()
    dialog = shown(gui.MemoOverviewDialog(root, app.theme, manager.mods))
    click_tag(dialog.text, "memo_show_2")
    assert dialog.result == ("show", b.id)

    empty = gui.MemoOverviewDialog(root, app.theme, [a])
    assert "아직 메모를 적은 모드가 없습니다." in empty.text.get("1.0", "end")
    empty.destroy()

    # what App.show_memos does with the result
    results = [("show", b.id), ("edit", opts.id), None]
    monkeypatch.setattr(gui.MemoOverviewDialog, "show", lambda self: (self.destroy(), results.pop(0))[1])
    edited = []
    monkeypatch.setattr(app, "edit_memo", lambda *args: edited.append(args))
    app.search_var.set("zzz")  # (hidden by a search: shown anyway)
    app._select_tab(app.conflicts_tab)
    app.show_memos()
    pump(root, lambda: True)
    assert tuple(app.tree.selection()) == (b.id,) and app.notebook.select() == str(app.info_tab)
    assert edited == []
    app.show_memos()
    assert tuple(app.tree.selection()) == (opts.id,) and edited == [(opts.id,)]
    app.show_memos()
    assert edited == [(opts.id,)]


def test_shorter_memo_windows_keep_their_buttons(root, manager, mods, monkeypatch):
    """메모 모아 보기 made as short as its minsize allows, the memo window made short: the text gets shorter, the
    buttons stay on the window."""
    from mhwmm import gui

    _a, b, _opts = mods
    manager.set_memo(b.id, "\n".join(f"{n}번째 줄" for n in range(30)))
    app = app_of(root, manager)
    monkeypatch.setattr(gui.MemoOverviewDialog, "FREEZE", False)  # (the body packed: it follows the window at once)
    for dialog, height in ((gui.MemoOverviewDialog(root, app.theme, manager.mods), app.theme.px(420)),
                           (gui.MemoDialog(root, app.theme, b), app.theme.px(300))):
        shown(dialog)
        assert dialog.winfo_height() > height
        dialog.geometry(f"{dialog.winfo_width()}x{height}")
        pump(dialog, lambda: dialog.winfo_height() == height)
        dialog.update()
        buttons = [w for row in dialog.body.winfo_children() for w in row.winfo_children()
                   if str(w.winfo_class()) == "TButton"]
        assert buttons
        for button in buttons:
            assert button.winfo_ismapped(), button.cget("text")
            assert button.winfo_rooty() + button.winfo_height() <= dialog.winfo_rooty() + height, button.cget("text")
        dialog.destroy()


@pytest.mark.parametrize("first", ["dark", "light"])
def test_both_themes_colour_the_memo(root, manager, mods, first):
    from mhwmm.theme import PALETTES

    _a, b, _opts = mods
    manager.settings.theme = first
    app = app_of(root, manager)
    app.tree.selection_set(b.id)
    app._update_details()
    for name in (first, "light" if first == "dark" else "dark"):
        app.apply_theme(name)
        p = PALETTES[name]
        info = app.info_text
        assert str(info.tag_cget("memo", "background")) == p["memo_bg"]
        assert str(info.tag_cget("memo", "foreground")) == p["text"]
        assert str(info.tag_cget("memo_title", "foreground")) == p["memo"]
        assert tagged(info, "memo")  # (still shown after the theme's refresh)
    for palette in PALETTES.values():
        assert {"memo", "memo_bg"} <= set(palette)
