"""모드 수정 in the GUI: the window's shell (modtool.ModToolDialog: the 모드 box, the column of tools, the pages
without tabs, the unsaved changes, the keyboard, fitting the screen) and the app around it (App.open_mod_tool,
saving the changes as a task, building edits again, the info and files tabs, updates). The manager's 모드 수정
API is replaced by fakes on the instance (FakeEdits; the real one: test_modedit_manager.py), the pages of
modeditview by small pages of the test where a test needs one."""

from __future__ import annotations

import gc
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from conftest import make_tree, make_zip
from mhwmm.modedit import EditError, GameCopyChanged, model

tk = pytest.importorskip("tkinter")

HELM = "natives/STM/Art/Model/Character/ch03/004/001/3/ch03_004_0013.mdf2.45"
ITEMS = "natives/STM/GameDesign/Common/Item/ItemData.user.3"
CONFIG = "reframework/autorun/NierUI/Config.lua"
SCRIPT = "reframework/autorun/x.lua"

# the screens of §5.4: (tk scaling, screen width, screen height)
SCREENS = {"1366x768@100%": (1.3333, 1366, 768), "1920x1080@150%": (2.0, 1920, 1080),
           "1366x768@125%": (1.6667, 1366, 768)}
LIMITS = {"1366x768@100%": (1070, 688), "1920x1080@150%": (1600, 1000)}  # (hard: about at most)


# ---- the manager's 모드 수정 API, faked ----------------------------------------------------------------------
class FakeEdits:
    """The manager's 모드 수정 API (§4) for tests of the window and the app: snapshots made from the mod's stored
    files, a record per mod (``records``), issues per mod, and what the app asked of it (``calls``: (name, mod
    id, on the GUI thread)). ``job_error``/``finish_error``/``build_error``: raised by edit_job (once, a
    GameCopyChanged only without force), finish_edit and build_edit; ``result``: what finish_edit gives."""

    def __init__(self, m):
        self.m = m
        self.records: dict[str, model.Record] = {}
        self.issues: dict[str, list[str]] = {}
        self.pending: list[str] = []
        self.calls: list[tuple] = []
        self.job_error: Exception | None = None
        self.finish_error: Exception | None = None
        self.build_error: Exception | None = None
        self.result = dict(changed=True, deploy=True, materials=False, equipment=False, lua=False,
                           summary="뺀 파일 1개", conflicts=0, pending=0, notes=[])
        self.reapplied: list[str] = []
        self.gate: threading.Event | None = None  # build_edit waits for it (a save that is still running)

    def _call(self, name, mod_id=""):
        self.calls.append((name, mod_id, threading.current_thread() is threading.main_thread()))

    def record(self, mod_id) -> model.Record:
        return self.records.setdefault(mod_id, model.Record())

    def edit_record(self, mod_id):
        self._call("edit_record", mod_id)
        return self.record(mod_id)

    def edit_snapshot(self, mod_id, conflicts=None):
        self._call("edit_snapshot", mod_id)
        mod = self.m.get(mod_id)
        base = self.m.mod_dir(mod_id)
        files = []
        scopes = [("", "", base / "files")] + [(o["id"], o["name"], base / "options" / o["id"]) for o in mod.options]
        seen: dict[str, int] = {}
        for oid, name, folder in scopes:
            n = seen.get(name, 0)
            if oid:
                seen[name] = n + 1
            if not folder.is_dir():
                continue
            for path in sorted(p for p in folder.rglob("*") if p.is_file()):
                rel = path.relative_to(folder).as_posix()
                files.append(model.EditFile(source=model.Source(name, n if oid else 0, rel), option_id=oid or None,
                                            key=f"{oid}/{rel}" if oid else rel, rel=rel, path=path,
                                            size=path.stat().st_size, kind="기타", state="in"))
        record = self.record(mod_id)
        options = [model.OptionRef(o["id"], o["name"], 0, o["id"] in mod.selected) for o in mod.options]
        return model.EditSnapshot(mod_id=mod_id, name=mod.name, enabled=mod.enabled, stamp=mod.stamp,
                                  rev=record.rev, options=options, files=files, record=record.copy(), bases={},
                                  placed={}, stuck={}, overlaps={}, pending=frozenset(),
                                  issues=list(self.issues.get(mod_id, [])), game_dir=self.m.game_dir,
                                  aliases=dict(mod.retarget), data_dir=self.m.data_dir)

    def edit_issues(self, mod_id):
        return list(self.issues.get(mod_id, []))

    def edit_summary(self, mod_id):
        record = self.record(mod_id)
        if record.error:
            return model.EditSummary(0, 0, 0, 0, 0, 0, record.error)
        values = sum(i.changes() for i in record.items)
        return model.EditSummary(excluded=sum(1 for i in record.items if i.exclude), values=values,
                                 files=sum(1 for i in record.items if i.changes()),
                                 conflicts=sum(i.conflicts() for i in record.items), pending=0,
                                 waiting=1 if mod_id in self.pending else 0, error="")

    def edit_items(self, mod_id):
        record = self.record(mod_id)
        return [model.EditItemView("exclude" if i.exclude else "data", i.source, "", i.source.rel, (), (),
                                   "waiting" if mod_id in self.pending else "active") for i in record.items]

    def edits_waiting(self, mod_id):
        return [i.source.rel for i in self.record(mod_id).items if i.members] if mod_id in self.pending else []

    def excluded_files(self, mod_id):
        return [i.source.rel for i in self.record(mod_id).items if i.exclude]

    def edits_pending(self):
        self._call("edits_pending")
        return list(self.pending)

    def drop_edit_notice(self, mod_id):
        self._call("drop_edit_notice", mod_id)
        self.record(mod_id).notice = {}

    def _job(self, plan, mode, force=False):
        mod = self.m.get(plan.mod_id)
        return SimpleNamespace(mod_id=plan.mod_id, name=mod.name, stamp=plan.stamp, rev=plan.rev, mode=mode,
                               plan=plan, force=force, stage=None, built=[])

    def edit_job(self, plan, force=False):
        self._call("edit_job", plan.mod_id)
        error, self.job_error = self.job_error, None
        if error is not None and not (force and isinstance(error, GameCopyChanged)):
            if isinstance(error, GameCopyChanged):
                self.job_error = error  # (until it is forced)
            raise error
        return self._job(plan, "commit", force)

    def reapply_job(self, mod_id, sources=None):
        self._call("reapply_job", mod_id)
        mod = self.m.get(mod_id)
        return self._job(model.EditPlan(mod_id, mod.stamp, self.record(mod_id).rev), "reapply")

    def build_edit(self, job, progress=None):
        self._call("build_edit", job.mod_id)
        if self.gate is not None:
            self.gate.wait(10)
        if self.build_error is not None:
            raise self.build_error
        return job

    def finish_edit(self, job):
        self._call("finish_edit", job.mod_id)
        if self.finish_error is not None:
            error, self.finish_error = self.finish_error, None
            raise error
        if job.mode == "reapply":
            self.reapplied.append(job.mod_id)
            if job.mod_id in self.pending:
                self.pending.remove(job.mod_id)
        record = self.record(job.mod_id)
        record.rev += 1
        return SimpleNamespace(mod_id=job.mod_id, **self.result)


EDIT_API = ("edit_record", "edit_snapshot", "edit_issues", "edit_summary", "edit_items", "edits_waiting",
            "excluded_files",
            "edits_pending", "drop_edit_notice", "edit_job", "reapply_job", "build_edit", "finish_edit")


def patch_window(monkeypatch, cls) -> None:
    """modtool.ModToolDialog replaced by ``cls`` for a test. retargetview is imported first: its RetargetDialog is
    made on the class modtool has when it is first imported, and the module stays for the tests after."""
    from mhwmm import modtool, retargetview

    assert issubclass(retargetview.RetargetDialog, modtool.ModToolDialog)
    monkeypatch.setattr(modtool, "ModToolDialog", cls)


def install_edit_fakes(m, monkeypatch) -> FakeEdits:
    """Put FakeEdits in place of the manager's 모드 수정 API."""
    fake = FakeEdits(m)
    for name in EDIT_API:
        monkeypatch.setattr(m, name, getattr(fake, name))
    return fake


# ---- fixtures ------------------------------------------------------------------------------------------------
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


class Answers:
    """The message boxes: what they were asked (``asked``: (kind, title, text)) and the answers to askyesno
    (``yes``: the next ones, then True)."""

    def __init__(self):
        self.asked: list[tuple] = []
        self.yes: list[bool] = []


@pytest.fixture(autouse=True)
def answers(monkeypatch):
    """A message box left open would block the test."""
    from mhwmm import gui

    told = Answers()
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name,
                            lambda title, text, parent=None, name=name: told.asked.append((name, title, text)))

    def ask(title, text, parent=None):
        told.asked.append(("askyesno", title, text))
        return told.yes.pop(0) if told.yes else True

    for name in ("askyesno", "askyesnocancel"):
        monkeypatch.setattr(gui.messagebox, name, ask)
    return told


def pump(root, until, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if until():
            return
        time.sleep(0.02)
    raise AssertionError("시간 초과")


def enabled(widget) -> bool:
    return "disabled" not in widget.state()


def stub_pages(monkeypatch):
    """modeditview's pages replaced by the test's (whether that module is there or not): a box in the top row
    (none for 수정 내용), a table as high as fit() says, a button that leaves a file out (a pending change), and
    what the shell called."""
    from tkinter import ttk

    from mhwmm import modtool

    class Stub(modtool.Page):
        top_text = "파일"

        def __init__(self, shell, frame, tool=None):
            super().__init__(shell, frame)
            self.tool, self.calls = tool, []
            if self.top_text:
                self.box = ttk.Combobox(shell.top, state="readonly", width=52, values=("가", "나"))
                self.place_top(self.box)
                shell.guard_box(self.box)
            self.tree = ttk.Treeview(frame, columns=("a",), show="headings", height=1)
            self.tree.pack(fill="both", expand=True)
            self.button = ttk.Button(frame, text="빼기", command=self.leave_out)
            self.button.pack(anchor="w")
            self.var = tk.BooleanVar(shell)  # (dropped when the window closes)

        def set_mod(self, snap):
            self.calls.append(("set_mod", snap.mod_id))

        def leave_out(self, rel=HELM):
            self.shell.pending.exclude(model.Source("", 0, rel), True)
            self.shell.pending_changed()

        def fit(self, rows):
            self.calls.append(("fit", rows))
            self.tree.configure(height=rows)

        def refresh(self):
            self.calls.append(("refresh",))

        def done(self, kind, tag, result):
            self.calls.append(("done", kind, tag, result))

        def first_focus(self):
            return self.tree

    class Summary(Stub):
        top_text = None

    classes = {"FilesPage": Stub, "ValuesPage": Stub, "SummaryPage": Summary}
    real = modtool._page_class
    monkeypatch.setattr(modtool, "_page_class",
                        lambda module, name: classes[name] if module == "modeditview" else real(module, name))
    return classes


@pytest.fixture
def env(root, manager, tmp_path, monkeypatch):
    """The app with an armor mod (Armor: loose game files), a data mod (Data: a .user file and a Lua settings
    file), a script mod, a mod with options and REFramework; the 모드 수정 API faked (FakeEdits) and the test's
    pages in the window."""
    from mhwmm import gui, refw

    mods = {}
    for name, files in (("Armor", {HELM: "m"}), ("Data", {ITEMS: "u", CONFIG: "Config = {}"}),
                        ("Script", {SCRIPT: "--"}), ("Framework", {"dinput8.dll": "d"})):
        with manager.prepare_install(make_tree(tmp_path / "src" / name, files)) as s:
            mods[name] = manager.install_package(s, s.variants)
        manager.set_enabled(mods[name].id, True)
    mods["Framework"].origin = refw.ORIGIN
    with manager.prepare_install(make_zip(tmp_path / "Colors.zip", {f"Red/{HELM}": "r", f"Blue/{HELM}": "b"})) as s:
        mods["Colors"] = manager.install_package(s, s.variants)
    manager.set_options(mods["Colors"].id, [mods["Colors"].options[0]["id"]])
    manager.settings.check_materials = False
    manager.settings.auto_apply = False
    fake = install_edit_fakes(manager, monkeypatch)
    monkeypatch.setattr(manager, "equipment_catalog", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(manager, "retarget_choices", lambda *a, **k: [], raising=False)
    stub_pages(monkeypatch)
    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: True)
    return app, manager, mods, fake


def window(app, mod_id, **kw):
    from mhwmm import modtool

    return modtool.ModToolDialog(app.root, app, mod_id, **kw)


def shown_size(dialog):
    """The size of the window once shown (frozen: Dialog.FREEZE)."""
    dialog.maxsize(4000, 3000)
    dialog.wait_window = lambda *a: None  # (show() returns at once)
    dialog.show()
    pump(dialog, lambda: dialog.winfo_ismapped() and dialog.body.winfo_width() > 1)
    for _ in range(5):
        dialog.update()
    return dialog.winfo_width(), dialog.winfo_height()


# ---- the shell -----------------------------------------------------------------------------------------------
def test_pages_without_tabs_and_the_tools_column(env):
    """The pages are a notebook without tabs: the hidden pages' controls in the top row are unmapped and out of
    the Tab order; the tools that do not work on the mod are off and say why; Ctrl+Tab goes to the next one that
    works (skipping the others)."""
    from tkinter import ttk

    from mhwmm import modtool

    app, m, mods, fake = env
    root = app.root
    dialog = window(app, mods["Script"].id)
    try:
        dialog.deiconify()
        style = ttk.Style(root)
        assert str(dialog.book.cget("style")) == "Pages.TNotebook"
        assert "tab" not in str(style.layout("Pages.TNotebook.Tab")).lower()  # (no tabs drawn)
        assert len(dialog.book.tabs()) == len(modtool.TOOLS)
        # 스크립트 설정 first for a script mod; 게임 수치 and 다른 장비로 옮기기 off, with the reason as the tooltip
        assert dialog.page_key == "config" and dialog.intro.cget("text") == modtool.INTROS["config"]
        assert [k for k in modtool.TOOLS if enabled(dialog.nav[k])] == ["files", "config", "summary"]
        assert dialog.tips["retarget"].text() == modtool.NOT_RETARGET and dialog.tips["data"].text() == modtool.NOT_DATA
        assert dialog.tips["config"].text() == ""
        assert str(dialog.nav["config"].cget("style")) == "NavActive.TButton"
        assert str(dialog.nav["files"].cget("style")) == "Nav.TButton"
        assert dialog.create_button.cget("text") == "저장" and not enabled(dialog.create_button)
        page = dialog.pages["config"]
        pump(root, lambda: page.box.winfo_ismapped())
        hidden = [p.box for k, p in dialog.pages.items() if k in ("files", "data")]
        assert not any(box.winfo_ismapped() for box in hidden)
        # Tab: 모드 → the page's box → the tools that work → the page → 저장 (off: skipped) → 취소
        order, widget = [], dialog.mod_box
        for _ in range(12):
            order.append(widget)
            widget = widget.tk_focusNext()
        assert order[:7] == [dialog.mod_box, page.box, dialog.nav["files"], dialog.nav["config"],
                             dialog.nav["summary"], page.tree, page.button]
        assert not set(hidden) & set(order)
        # Ctrl+Tab / Ctrl+Shift+Tab: the next and previous tool that works (round), the keyboard on the page
        dialog.focus_force()
        dialog.event_generate("<Control-Tab>")
        root.update()
        assert dialog.page_key == "summary" and dialog.focus_get() is dialog.pages["summary"].tree
        dialog.event_generate("<Control-Tab>")
        assert dialog.page_key == "files"
        dialog.event_generate("<Control-Tab>")
        assert dialog.page_key == "config"
        dialog.step(-1)
        assert dialog.page_key == "files"
        dialog.select("retarget")  # (off: nothing happens)
        pump(root, lambda: dialog.pages["files"].box.winfo_ismapped())
        assert dialog.page_key == "files" and not page.box.winfo_ismapped()
    finally:
        dialog.destroy()


def test_the_page_it_opens_on(env):
    """Asked for by name (even a tool that does not work: it says why), else 수정 내용 when something needs the
    user, else the tool used last, else 다른 장비로 옮기기 for a mod known to change equipment, else 게임 수치,
    스크립트 설정, 파일 고르기 - the first that works."""
    app, m, mods, fake = env
    cases = []

    def opened(mod, **kw):
        dialog = window(app, mods[mod].id, **kw)
        try:
            cases.append(dialog.page_key)
        finally:
            dialog.destroy()

    opened("Armor")  # (no equipment known yet)
    opened("Data")
    opened("Script")
    opened("Script", page="retarget")
    fake.issues[mods["Armor"].id] = ["확인할 값 1개"]
    opened("Armor")
    fake.issues.clear()
    app._mod_tool_page = "config"
    opened("Data")
    opened("Armor")  # (the last one does not work on it)
    app._mod_tool_page = None
    m.equipment_summary = lambda mod_id: ["방어구 004-001 (여성: 머리)"]
    opened("Armor")
    assert cases == ["files", "data", "config", "retarget", "summary", "config", "files", "retarget"]


def test_the_tool_picked_is_remembered_not_one_asked_for(env, monkeypatch):
    """The window opens on the tool the user picked last in it (its column, Ctrl+Tab), not on one asked for by
    name (수정 내용 from the info tab's link or an update, 다른 장비로 옮기기 from 장비 외형) nor on 수정 내용 picked; a
    remembered 다른 장비로 옮기기 only for a mod known to change equipment (as when nothing is remembered)."""
    app, m, mods, fake = env
    then = []
    seen = choose(monkeypatch, lambda dialog: then.pop(0)(dialog) if then else None)

    def opened(mod, page=None):
        seen.clear()
        app.open_mod_tool(mods[mod].id, page)
        return seen[0][1]

    assert opened("Script") == "config"
    assert opened("Data", "summary") == "summary"  # (the info tab's link)
    assert opened("Armor", "retarget") == "retarget"  # (the 장비 외형 link)
    assert app._mod_tool_page is None
    assert opened("Script") == "config" and opened("Data") == "data"

    def pick(*keys):
        def run(dialog):
            for key in keys:
                dialog.nav[key].invoke()
        then.append(run)

    pick("files", "summary")  # (수정 내용 picked after 파일 고르기: 파일 고르기 is remembered)
    opened("Data")
    assert app._mod_tool_page == "files"
    assert opened("Script") == "files" and opened("Data", "summary") == "summary"
    assert app._mod_tool_page == "files"

    then.append(lambda dialog: dialog.step(1))  # (Ctrl+Tab: 스크립트 설정 after 파일 고르기)
    opened("Script")
    assert app._mod_tool_page == "config"

    app._mod_tool_page = "retarget"
    assert opened("Data") == "data"  # (no equipment known of it: its own tool)
    monkeypatch.setattr(m, "equipment_summary", lambda mod_id: ["방어구 004-001 (여성: 머리)"], raising=False)
    assert opened("Data") == "retarget"


def test_size_stays_and_fits_the_screen(env, monkeypatch):
    """The window is as big on every page and for every mod (frozen once shown), and its tables are as high as
    the screen has room for (FIT_ROWS)."""
    from mhwmm import modtool

    app, m, mods, fake = env
    monkeypatch.setattr(modtool.ModToolDialog, "_room", lambda self: self.theme.px(688))
    monkeypatch.setattr(modtool.ModToolDialog, "_room_width", lambda self: self.theme.px(1350))
    dialog = window(app, mods["Armor"].id, page="retarget")
    try:
        size = shown_size(dialog)
        assert dialog.body.winfo_manager() == "place" and dialog.minsize() == size
        assert all(("fit", dialog.rows) in p.calls for k, p in dialog.pages.items() if k != "retarget")
        assert int(dialog.tree.cget("height")) == dialog.rows and modtool.FIT_ROWS[0] <= dialog.rows
        for key in ("files", "data", "summary", "config", "retarget"):
            dialog.select(key, force=True)
            dialog.update()
            assert (dialog.winfo_reqwidth(), dialog.winfo_reqheight()) == size, key
        for mod in ("Data", "Colors", "Script", "Armor"):
            dialog.mod_box.current(dialog.mod_ids.index(mods[mod].id))
            dialog.mod_box.event_generate("<<ComboboxSelected>>")
            dialog.update()
            assert (dialog.winfo_reqwidth(), dialog.winfo_reqheight()) == size, mod
        dialog.select("retarget")  # (its equipment read once shown: the window keeps its size)
        pump(app.root, lambda: dialog.sources and dialog.part_rows)
        dialog.update()
        assert (dialog.winfo_reqwidth(), dialog.winfo_reqheight()) == size
        assert (dialog.winfo_width(), dialog.winfo_height()) == size
    finally:
        dialog.destroy()
    # a lower screen: fewer rows (at least FIT_ROWS[0])
    monkeypatch.setattr(modtool.ModToolDialog, "_room", lambda self: self.theme.px(300))
    dialog = window(app, mods["Armor"].id)
    try:
        assert dialog.rows == modtool.FIT_ROWS[0]
    finally:
        dialog.destroy()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("screen", list(SCREENS))
def test_fits_the_screens(manager, tmp_path, monkeypatch, theme, screen):
    """§5.4: 1366×768 at 100% and 1920×1080 at 150% fit (about); 1366×768 at 125% is measured and printed."""
    from mhwmm import gui, modtool

    scaling, width, height = SCREENS[screen]
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("화면(display)이 없어 GUI 테스트를 건너뜁니다")
    try:
        root.tk.call("tk", "scaling", scaling)
        with manager.prepare_install(make_tree(tmp_path / "src" / "A", {HELM: "m", ITEMS: "u"})) as s:
            mod = manager.install_package(s, s.variants)
        manager.settings.theme, manager.settings.auto_apply = theme, False
        install_edit_fakes(manager, monkeypatch)
        stub_pages(monkeypatch)
        app = gui.App(root, manager, queue.Queue())
        px = app.theme.px
        monkeypatch.setattr(modtool.ModToolDialog, "_room", lambda self: height - px(80))
        monkeypatch.setattr(modtool.ModToolDialog, "_room_width", lambda self: width - px(16))
        dialog = window(app, mod.id, page="retarget")
        try:
            size = shown_size(dialog)
            print(f"\n모드 수정 {screen} {theme}: {size[0]}x{size[1]} (표 {dialog.rows}줄)")
            base = app.theme.fonts["base"]
            if base.measure("가") < base.measure("0"):  # (venv310's Tk here: no font draws Hangul)
                print("  한글을 그리는 글꼴이 없어 크기는 재기만 했습니다")
            elif screen in LIMITS:
                assert size[0] <= LIMITS[screen][0] and size[1] <= LIMITS[screen][1], size
            else:
                assert size[0] <= 1350, size  # (the target width; the height is only measured)
        finally:
            dialog.destroy()
        gc.collect()
    finally:
        root.destroy()


def test_keys_of_the_window(env, answers):
    """Return on the 모드 box and the other boxes does nothing, on a button presses it (a tool's: shows it), on
    the rest saves (bell when nothing to save); Ctrl+S saves on an edit page, not on 다른 장비로 옮기기; Escape
    with unsaved changes asks first."""
    from mhwmm import modtool

    app, m, mods, fake = env
    root = app.root
    dialog = window(app, mods["Data"].id)
    try:
        dialog.deiconify()
        page = dialog.pages["data"]

        def key(widget, name):
            widget.focus_force()
            pump(root, lambda: dialog.focus_get() is widget)
            widget.event_generate(name)
            root.update()

        for widget in (dialog.mod_box, page.box):
            key(widget, "<Return>")
            key(widget, "<KP_Enter>")
        assert not dialog.closed and dialog.result is None
        key(dialog.nav["config"], "<Return>")  # (a tool's button: its page)
        assert dialog.page_key == "config" and not dialog.closed
        key(dialog.pages["config"].tree, "<Return>")  # (nothing to save: the bell)
        assert not dialog.closed
        dialog.pages["config"].button.focus_force()
        key(dialog.pages["config"].button, "<Return>")  # (빼기: a pending change, not saved yet)
        assert dialog.pending.count() == 1 and not dialog.closed
        assert dialog.footer.cget("text") == f"저장하지 않은 수정 1개 · {modtool.SHARED_NOTE}"
        assert enabled(dialog.create_button)
        # 다른 장비로 옮기기 with unsaved changes: its button off and the line says why
        dialog.select("retarget", force=True)
        assert not enabled(dialog.create_button) and dialog.footer.cget("text") == modtool.WAIT_NOTE
        key(dialog.mod_box, "<Control-s>")
        assert not dialog.closed
        dialog.select("config")
        # Escape: asks; 아니요 keeps the window
        answers.yes = [False]
        key(dialog.mod_box, "<Escape>")
        assert not dialog.closed and answers.asked[-1] == ("askyesno", "모드 수정", "저장하지 않은 수정 1개를 버릴까요?")
        key(dialog.pages["config"].tree, "<Control-s>")
        assert dialog.closed and isinstance(dialog.result, modtool.EditChoice)
        plan = dialog.result.plan
        assert dialog.result.page == "config" and plan.mod_id == mods["Data"].id
        assert plan.exclude == {model.Source("", 0, HELM): True} and (plan.stamp, plan.rev) == (mods["Data"].stamp, 0)
    finally:
        if not dialog.closed:
            dialog.destroy()

    dialog = window(app, mods["Data"].id)
    try:
        dialog.pages["data"].leave_out()
        answers.yes = [True]
        dialog.cancel()  # (the window's close button and 취소: the same)
        assert dialog.closed and dialog.result is None
    finally:
        if not dialog.closed:
            dialog.destroy()


def test_another_mod_with_unsaved_changes(env, answers):
    """Another mod picked with unsaved changes asks first: 아니요 shows the mod in the box again; 예 starts over on
    the other mod (every page set to it, the tool kept when it works on it)."""
    app, m, mods, fake = env
    dialog = window(app, mods["Data"].id, page="files")
    try:
        files = dialog.pages["files"]
        files.leave_out()
        index = dialog.mod_ids.index(mods["Armor"].id)
        answers.yes = [False]
        dialog.mod_box.current(index)
        dialog.mod_box.event_generate("<<ComboboxSelected>>")
        assert dialog.mod_id == mods["Data"].id and dialog.mod_box.get() == "Data" and dialog.pending.count() == 1
        before = dialog._generation
        dialog.mod_box.current(index)
        dialog.mod_box.event_generate("<<ComboboxSelected>>")
        assert dialog.mod_id == mods["Armor"].id and dialog._generation == before + 1 and not dialog.pending.count()
        assert files.calls[-1] == ("set_mod", mods["Armor"].id) and dialog.page_key == "files"
        assert [a[0] for a in answers.asked].count("askyesno") == 2
        # a tool that does not work on the next mod: the one that fits it
        dialog.mod_box.current(dialog.mod_ids.index(mods["Data"].id))
        dialog.mod_box.event_generate("<<ComboboxSelected>>")
        dialog.select("data")
        assert dialog.page_key == "data"
        dialog.mod_box.current(dialog.mod_ids.index(mods["Script"].id))
        dialog.mod_box.event_generate("<<ComboboxSelected>>")
        assert dialog.page_key == "config"
        # the mods it works on: all but REFramework, in the list's order
        assert dialog.mod_ids == [mods[n].id for n in ("Armor", "Data", "Script", "Colors")]
    finally:
        dialog.destroy()


def test_results_of_a_mod_shown_before_are_dropped(env):
    """A result for the mod shown before (its _generation) is dropped, one for the shown mod reaches its page;
    nothing after the window closed. The work runs on another thread; 게임 수치 keeps the game's data types loaded
    until the window closes."""
    from mhwmm.modedit import rszdata

    app, m, mods, fake = env
    root = app.root
    held = []
    rszdata_registry, rszdata_release = rszdata.registry, rszdata.release
    try:
        rszdata.registry = lambda: held.append("hold")
        rszdata.release = lambda: held.append("release")
        dialog = window(app, mods["Data"].id)
        page = dialog.pages["data"]
        threads = []
        dialog.run("data", "values", lambda: threads.append(threading.current_thread()) or 7, "tag")
        pump(root, lambda: ("done", "values", "tag", 7) in page.calls)
        assert threads and threads[0] is not threading.main_thread() and held == ["hold"]
        dialog.run("data", "values", lambda: 8)
        pump(root, lambda: ("done", "values", None, 8) in page.calls)
        assert held == ["hold"]  # (once)
        old = dialog._generation
        dialog._done("values", None, 9, old - 1, "data")
        dialog._done("values", None, EditError("x"), old, "data")
        assert ("done", "values", None, 9) not in page.calls and page.calls[-1][3].args == ("x",)
        dialog.destroy()
        assert held == ["hold", "release"]
        dialog._done("values", None, 10, dialog._generation, "data")
        assert ("done", "values", None, 10) not in page.calls
    finally:
        rszdata.registry, rszdata.release = rszdata_registry, rszdata_release


def test_retarget_dialog_reads_through_the_window(env):
    """RetargetDialog: the window on 다른 장비로 옮기기 (its old signature); the page's attributes read through the
    window, and its old four-argument _done still works."""
    from mhwmm.retargetview import RetargetDialog, RetargetPage

    app, m, mods, fake = env
    dialog = RetargetDialog(app.root, app, mods["Armor"].id)
    try:
        page = dialog.pages["retarget"]
        assert isinstance(page, RetargetPage) and dialog.page_key == "retarget"
        assert dialog.tree is page.tree and dialog.source_box is page.source_box and dialog.off_var is page.off_var
        assert dialog.create_button is dialog.ok_button and dialog.create_button.cget("text") == "새 모드 만들기"
        assert dialog._set_source.__func__ is RetargetPage._set_source
        with pytest.raises(AttributeError):
            dialog.no_such_thing
        dialog._done("sources", None, ([], [], {}), dialog._generation)  # (the page's, by default)
        assert "외형이 없습니다" in dialog.shown_text()
    finally:
        dialog.destroy()


def test_retarget_page_says_what_becomes_of_the_edits(env):
    """다른 장비로 옮기기 on a mod changed with 모드 수정: the new mod gets its files as the game gets them (and not
    the edits still waiting to be applied again)."""
    from mhwmm import retargetview
    from mhwmm.retargetview import RetargetDialog

    app, m, mods, fake = env
    armor = mods["Armor"].id
    fake.records[armor] = model.Record(rev=1, items=[model.Item(model.Source("", 0, HELM), exclude=True)])
    dialog = RetargetDialog(app.root, app, armor)
    try:
        assert dialog.shown_text().startswith(retargetview.EDITED_NOTE + "\n장비 정보를 읽는 중...")
        assert "muted" in dialog.text.tag_names("1.0")
        fake.records[armor].items[0].members = [model.Member("", "rsz", "x", pending="형식이 다릅니다")]
        dialog.mod_box.current(dialog.mod_ids.index(mods["Data"].id))
        dialog.mod_box.event_generate("<<ComboboxSelected>>")
        assert not dialog.shown_text().startswith(retargetview.EDITED_NOTE)
        dialog.mod_box.current(dialog.mod_ids.index(armor))
        dialog.mod_box.event_generate("<<ComboboxSelected>>")
        assert dialog.shown_text().startswith(f"{retargetview.EDITED_NOTE}\n{retargetview.WAITING_NOTE}\n")
    finally:
        dialog.destroy()


def test_theme_styles(root, manager):
    from tkinter import ttk

    from mhwmm import gui

    app = gui.App(root, manager, queue.Queue())
    style, p = ttk.Style(root), app.theme.p
    for name in ("dark", "light"):
        app.apply_theme(name)
        p = app.theme.p
        assert style.lookup("Danger.TLabel", "foreground") == p["danger"]
        assert style.lookup("CardDanger.TLabel", "foreground") == p["danger"]
        assert style.lookup("CardDanger.TLabel", "background") == p["surface"]
        assert style.lookup("Pages.TNotebook", "background") == p["bg"]
        assert style.lookup("NavActive.TButton", "background") == p["select"]


def test_reopened_with_the_changes_of_a_failed_save(env):
    """The window opened again after a save failed: the changes come back, those of files the mod no longer
    has left out (the footer says so)."""
    app, m, mods, fake = env
    data = mods["Data"]
    plan = model.EditPlan(data.id, "old stamp", 0, exclude={model.Source("", 0, ITEMS): True,
                                                            model.Source("", 0, "natives/STM/gone.tex.1"): True})
    dialog = window(app, data.id, page="files", pending=plan)
    try:
        assert dialog.pending.count() == 1 and dialog.pending.excluded(model.Source("", 0, ITEMS))
        assert "지금은 없는 파일의 수정 1개는 빼고 다시 열었습니다" in dialog.footer.cget("text")
        dialog.ok()
        assert dialog.result.plan.stamp == data.stamp and dialog.result.page == "files"
    finally:
        if not dialog.closed:
            dialog.destroy()


def test_summary_tool_counts_the_edits(env):
    """수정 내용 in the column says how many edits the mod has, ⚠ and how many things need the user."""
    app, m, mods, fake = env
    data = mods["Data"].id
    fake.records[data] = model.Record(rev=2, items=[
        model.Item(model.Source("", 0, CONFIG), exclude=True),
        model.Item(model.Source("", 0, ITEMS), members=[model.Member("", "rsz", "ItemData", changes=[
            model.Change("_A", "S32", "1", "2"), model.Change("_B", "S32", "1", "3")])])])
    labels = []
    for issues in ([], ["확인할 값 1개", "다시 적용 대기 1개"]):
        fake.issues[data] = issues
        dialog = window(app, data)
        try:
            labels.append(dialog.nav["summary"].cget("text"))
        finally:
            dialog.destroy()
    assert labels == ["수정 내용 (3)", "⚠ 수정 내용 (2)"]


# ---- the app ---------------------------------------------------------------------------------------------------
def choose(monkeypatch, then):
    """The 모드 수정 window opened by the app shows and gives what ``then(dialog)`` chose (at once)."""
    from mhwmm import modtool

    seen = []

    def show(self):
        seen.append((self.mod_id, self.page_key))
        then(self)
        if not self.closed:
            self.destroy()
        return self.result

    monkeypatch.setattr(modtool.ModToolDialog, "show", show)
    return seen


def leave_out_and_save(dialog):
    dialog.pages[dialog.page_key].leave_out()
    dialog.ok()


def test_saving_waits_for_a_running_task_and_follows_up(env, monkeypatch):
    """저장: once no other task runs (no launch meanwhile, nor during the save: "modedit" is a folder task), checked
    and planned on this thread, built on another, put in place here; then the list, the status line, the apply,
    and only what the result asks for (the material check after files were left out, not after values)."""
    from mhwmm import gui

    app, m, mods, fake = env
    root = app.root
    data = mods["Data"].id
    called, launched = [], []
    for name in ("check_materials_background", "warm_equipment", "request_apply"):
        monkeypatch.setattr(app, name, lambda name=name: called.append(name))
    monkeypatch.setattr(app, "_check_requirements_of", lambda mod: called.append(("lua", mod.id)))
    monkeypatch.setattr(app, "_start_game", lambda *a, **k: launched.append(1))
    assert "modedit" in gui.App.FOLDER_TASKS
    seen = choose(monkeypatch, leave_out_and_save)

    fake.gate = threading.Event()  # (the save holds until let go)
    app.busy = "deploy"  # (an apply runs when the window closes)
    app.open_mod_tool(data, "files")
    assert seen == [(data, "files")] and app._tool_waiting == 1 and app._retarget_waiting == 1
    assert app.status_var.get() == "진행 중인 작업이 끝나면 모드 수정을 저장합니다..."
    assert not [c for c in fake.calls if c[0] == "edit_job"]
    app.busy = None
    m.dirty = False
    app._start_when_ready()  # (waiting: no launch)
    assert launched == [] and app.status_var.get() == "작업이 끝난 뒤에 다시 시도하세요."
    pump(root, lambda: app.busy == "modedit")
    app._start_when_ready()  # (saving: no launch)
    assert launched == []
    fake.gate.set()
    pump(root, lambda: app.busy is None and ("finish_edit", data, True) in fake.calls)
    assert app._tool_waiting == 0 and app.tree.selection() == (data,)
    assert app.status_var.get() == "모드 수정을 저장했습니다: Data (뺀 파일 1개)"
    assert called == ["request_apply"]  # (res: deploy only)
    names = [c[:2] for c in fake.calls if c[0] in ("edit_job", "build_edit", "finish_edit")]
    assert names == [("edit_job", data), ("build_edit", data), ("finish_edit", data)]
    assert all(on_gui == (name != "build_edit") for name, _id, on_gui in fake.calls)  # (only the build elsewhere)
    assert app._mod_tool_page is None  # (a tool asked for by name is not remembered: only one picked in the window)

    # what the result asks for: the material check (files left out), the equipment, the Lua modules
    called.clear()
    fake.result.update(deploy=False, materials=True, equipment=True, lua=True, summary="")
    app.open_mod_tool(data, "files")
    pump(root, lambda: app.busy is None and len(called) == 3)
    assert called == ["check_materials_background", "warm_equipment", ("lua", data)]
    assert app.status_var.get() == "모드 수정을 저장했습니다: Data"


def test_closing_the_app_waits_for_a_save_that_waits(env, answers, monkeypatch):
    """The app closed (작업이 끝나면 종료할까요? 예) while a 저장 waits for a running task: the save runs first, then the
    app closes; asked also while only the save waits; a save that fails keeps the app open and the window again
    (the changes would go with the app); one that cannot start (원인 모드 찾기 meanwhile) lets it close."""
    from mhwmm import modtool

    app, m, mods, fake = env
    root = app.root
    data = mods["Data"].id
    closed = []
    monkeypatch.setattr(app, "_destroy", lambda: closed.append([c[0] for c in fake.calls]))
    monkeypatch.setattr(app, "request_apply", lambda: None)
    choose(monkeypatch, leave_out_and_save)

    app.busy = "deploy"  # (an apply runs when the window closes)
    app.open_mod_tool(data, "files")
    assert app._tool_waiting == 1 and not [c for c in fake.calls if c[0] == "edit_job"]
    app.on_close()
    assert answers.asked[-1] == ("askyesno", "종료", "작업이 끝나면 종료할까요?") and app._close_after_task
    app.busy = None
    app._after_task()  # (the apply's end)
    assert closed == [] and app._tool_waiting == 1
    pump(root, lambda: closed)
    assert len(closed) == 1 and "finish_edit" in closed[0]  # (saved, then closed)

    # only the save waits (for an install between its steps): the app asks too, and closes once it is saved
    closed.clear()
    fake.calls.clear()
    app._close_after_task = False
    plan = model.EditPlan(data, mods["Data"].stamp, 0, exclude={model.Source("", 0, ITEMS): True})
    app._installing = True
    app._commit_edits(modtool.EditChoice(plan, "files"))
    app.on_close()
    assert answers.asked[-1] == ("askyesno", "종료", "작업이 끝나면 종료할까요?") and closed == []
    app._installing = False
    pump(root, lambda: closed)
    assert len(closed) == 1 and "finish_edit" in closed[0]

    # the save fails: the app stays open, the window opens again with the changes
    closed.clear()
    app._close_after_task = False
    reopened = []
    monkeypatch.setattr(app, "open_mod_tool", lambda *a, **k: reopened.append((a, k)))
    fake.job_error = EditError("모드 수정 기록이 그사이 바뀌었습니다. 창을 다시 열어 주세요.")
    app.busy = "deploy"
    app._commit_edits(modtool.EditChoice(plan, "files"))
    app.on_close()
    app.busy = None
    app._after_task()
    pump(root, lambda: reopened)
    assert reopened == [((data, "files"), {"pending": plan})] and not app._close_after_task
    root.update()
    assert closed == []

    # the save cannot start (원인 모드 찾기 started meanwhile): nothing waits any more, the app closes
    jobs = len([c for c in fake.calls if c[0] == "edit_job"])
    app.busy = "deploy"
    app._commit_edits(modtool.EditChoice(plan, "files"))
    app.on_close()
    app._bisect_preparing = True
    app.busy = None
    app._after_task()
    assert closed == []
    pump(root, lambda: closed)
    assert app._tool_waiting == 0 and len([c for c in fake.calls if c[0] == "edit_job"]) == jobs
    app._bisect_preparing = False


def native_box(root, until=lambda: False, timeout=0.6):
    """A message box as Windows shows it (MessageBoxW, served with Tcl's events): the app's timers and events
    run while it is up, and Tk has no grab. Up until ``until()`` or ``timeout`` seconds."""
    end = time.time() + timeout
    while time.time() < end and not until():
        root.update()
        time.sleep(0.01)


@pytest.mark.parametrize("case", ["answered after", "answered during", "refused"])
def test_closing_waits_for_a_save_whose_message_box_is_up(env, answers, monkeypatch, case):
    """Two saves wait for an apply and the app is asked to close. The first one asks (a changed game copy) or is
    refused (its error), and under that message box the app's timers run (as on Windows): the second save runs
    meanwhile. The first still counts as waiting: the app does not close under the box, it never starts its task
    beside the second's (예 while that runs: it waits again, and is not asked twice), and it is saved (or its
    window opens again, the app open) before the app closes."""
    from mhwmm import gui

    app, m, mods, fake = env
    root = app.root
    first, second = mods["Data"].id, mods["Armor"].id
    closed, started, under, opened = [], [], [], []
    monkeypatch.setattr(app, "_destroy", lambda: closed.append([c[:2] for c in fake.calls]))
    monkeypatch.setattr(app, "request_apply", lambda: None)

    def then(dialog):
        opened.append(dialog.mod_id)
        if len(opened) <= 2:  # (a window opened again is closed as it is)
            leave_out_and_save(dialog)

    choose(monkeypatch, then)
    gate = threading.Event()  # (the second save's build holds until let go)
    real_job, real_build, real_run = m.edit_job, m.build_edit, app.run_task

    def edit_job(plan, force=False):
        if plan.mod_id == first and not force:
            fake._call("edit_job", plan.mod_id)
            raise EditError("모드 수정 기록이 그사이 바뀌었습니다.") if case == "refused" else GameCopyChanged("바뀜", [HELM])
        return real_job(plan, force)

    def build_edit(job, progress=None):
        if job.mod_id == second:
            gate.wait(10)
        return real_build(job, progress)

    def run_task(kind, label, func, on_done=None, on_error=None):
        started.append((kind, app.busy))
        return real_run(kind, label, func, on_done=on_done, on_error=on_error)

    def box(title, text, parent=None):
        if text == "작업이 끝나면 종료할까요?":
            return True
        answers.asked.append(("box", title, text))
        if case == "answered during":  # (예 while the second save's build runs)
            native_box(root, lambda: app.busy == "modedit", timeout=5)
        else:  # (the second save runs and is done under the box)
            native_box(root, lambda: ("finish_edit", second, True) in fake.calls and app.busy is None, timeout=5)
            native_box(root, timeout=0.3)
        under.append(list(closed))
        return True

    monkeypatch.setattr(m, "edit_job", edit_job)
    monkeypatch.setattr(m, "build_edit", build_edit)
    monkeypatch.setattr(app, "run_task", run_task)
    monkeypatch.setattr(gui.messagebox, "askyesno", box)
    monkeypatch.setattr(gui.messagebox, "showerror", box)
    if case != "answered during":
        gate.set()

    app.busy = "deploy"  # (an apply runs when the windows close)
    app.open_mod_tool(first, "files")
    app.open_mod_tool(second, "files")
    assert opened == [first, second] and app._tool_waiting == 2
    app.on_close()
    assert app._close_after_task
    app.busy = None
    app._after_task()  # (the apply's end)
    pump(root, lambda: under)
    assert under == [[]]  # (not closed under the box)
    gate.set()
    pump(root, lambda: app.busy is None and not app._tool_waiting and ("finish_edit", second, True) in fake.calls)
    pump(root, lambda: closed or case == "refused")
    root.update()
    assert all(busy is None for _kind, busy in started)  # (never a task beside another)
    assert len([a for a in answers.asked if a[0] == "box"]) == 1  # (asked once)
    if case == "refused":  # the window again with the changes, the app open
        assert opened == [first, second, first] and closed == [] and not app._close_after_task
        assert ("finish_edit", first, True) not in fake.calls
    else:  # saved, then closed
        assert len(closed) == 1 and ("finish_edit", first) in closed[0] and ("finish_edit", second) in closed[0]
        assert opened == [first, second]
    assert app._tool_waiting == 0


def test_saving_refused_during_the_search(env, answers, monkeypatch):
    from mhwmm import bisect, modtool

    app, m, mods, fake = env
    data = mods["Data"].id
    seen = choose(monkeypatch, leave_out_and_save)
    app._bisect_preparing = True  # 원인 모드 찾기
    app.open_mod_tool(data)
    assert seen == [] and answers.asked[-1] == ("showinfo", bisect.TITLE, bisect.BLOCKED)
    plan = model.EditPlan(data, mods["Data"].stamp, 0, exclude={model.Source("", 0, ITEMS): True})
    app._commit_edits(modtool.EditChoice(plan, "files"))  # (chosen before it started)
    assert not [c for c in fake.calls if c[0] == "edit_job"] and app.busy is None
    app._bisect_preparing = False
    app._reapply_soon()
    assert app.busy is None


def test_a_changed_game_copy_asks_and_a_failed_save_opens_the_window_again(env, answers, monkeypatch):
    """Reverting files whose copy in the game folder changed asks first (예: saved anyway, 아니요: the window
    again); a save that fails says why and opens the window again with the changes."""
    from mhwmm import modtool

    app, m, mods, fake = env
    root = app.root
    data = mods["Data"].id
    plan = model.EditPlan(data, mods["Data"].stamp, 0, exclude={model.Source("", 0, ITEMS): True})
    choice = modtool.EditChoice(plan, "data")
    reopened = []
    monkeypatch.setattr(app, "open_mod_tool", lambda *a, **k: reopened.append((a, k)))

    fake.job_error = GameCopyChanged("바뀜", [ITEMS])
    answers.yes = [False]
    app._commit_edits(choice)
    pump(root, lambda: reopened)
    assert answers.asked[-1][0] == "askyesno" and f"• {ITEMS}" in answers.asked[-1][2]
    assert answers.asked[-1][2].startswith("게임 폴더의 다음 파일은 매니저가 넣은 뒤에 바뀌었습니다:\n")
    assert reopened == [((data, "data"), {"pending": plan})] and app.busy is None

    reopened.clear()
    answers.yes = [True]
    app._commit_edits(choice)
    pump(root, lambda: app.busy is None and ("finish_edit", data, True) in fake.calls)
    assert [c[:2] for c in fake.calls if c[0] == "edit_job"] == [("edit_job", data)] * 3 and not reopened

    # refused by the check (stamp, record): told, then the window again
    fake.job_error = EditError("모드 수정 기록이 그사이 바뀌었습니다. 창을 다시 열어 주세요.")
    app._commit_edits(choice)
    pump(root, lambda: reopened)
    assert answers.asked[-1] == ("showerror", "모드 수정", "모드 수정 기록이 그사이 바뀌었습니다. 창을 다시 열어 주세요.")
    # one the check did not expect: as a save that failed (nothing is left waiting: the game could not start)
    reopened.clear()
    fake.job_error = OSError("디스크를 읽지 못했습니다")
    app._commit_edits(choice)
    pump(root, lambda: reopened)
    assert answers.asked[-1] == ("showerror", "모드 수정", "모드 수정을 저장하지 못했습니다.\n\n디스크를 읽지 못했습니다")
    assert reopened == [((data, "data"), {"pending": plan})] and app._tool_waiting == 0

    # failed while building or putting in place
    for attr in ("build_error", "finish_error"):
        reopened.clear()
        setattr(fake, attr, EditError("저장 공간이 모자랍니다"))
        app._commit_edits(choice)
        pump(root, lambda: reopened and app.busy is None)
        assert answers.asked[-1] == ("showerror", "모드 수정", "모드 수정을 저장하지 못했습니다.\n\n저장 공간이 모자랍니다")
        assert reopened == [((data, "data"), {"pending": plan})]
        fake.build_error = None


def test_edits_built_again_when_nothing_else_runs(env, monkeypatch):
    """Edits that wait (their file changed) are built again once nothing runs, never during 원인 모드 찾기, and a
    mod that keeps waiting is not built over and over (until the next change)."""
    app, m, mods, fake = env
    root = app.root
    data = mods["Data"].id
    monkeypatch.setattr(app, "request_apply", lambda: None)
    fake.pending = [data]
    app._bisect_preparing = True
    app._reapply_soon()
    assert not [c for c in fake.calls if c[0] in ("edits_pending", "reapply_job")]
    app._bisect_preparing = False
    app.busy = "deploy"
    app._reapply_soon()
    assert app._reapply_after is not None and not [c for c in fake.calls if c[0] == "reapply_job"]
    app.busy = None
    pump(root, lambda: fake.reapplied == [data] and app.busy is None)
    assert app.status_var.get() == "모드 수정을 다시 적용했습니다: Data"
    assert all(on_gui == (name != "build_edit") for name, _id, on_gui in fake.calls)

    # one that keeps waiting: built again at most REAPPLY_TRIES times, then only after a change
    from mhwmm import gui

    monkeypatch.setattr(m, "finish_edit", lambda job: SimpleNamespace(
        changed=False, deploy=False, equipment=False, lua=False, conflicts=0) if fake.reapplied.append(job.mod_id)
        is None else None)
    fake.pending = [data]
    fake.reapplied.clear()
    app._reapply_soon()
    pump(root, lambda: len(fake.reapplied) == gui.REAPPLY_TRIES and app.busy is None)
    app._reapply_soon(retry=True)
    root.update()
    assert len(fake.reapplied) == gui.REAPPLY_TRIES and app.busy is None
    app._reapply_soon()  # (a change: tried again)
    pump(root, lambda: len(fake.reapplied) > gui.REAPPLY_TRIES)


def test_info_and_files_tabs_show_the_edits(env, monkeypatch):
    """정보: a line with what 모드 수정 changed and a link to 수정 내용 (✕ and the problem when something needs the
    user); 파일: the files built by 모드 수정 (수정본), the ones waiting to be built again, the files left out (last,
    muted)."""
    app, m, mods, fake = env
    root = app.root
    data = mods["Data"]
    fake.records[data.id] = model.Record(rev=1, items=[
        model.Item(model.Source("", 0, CONFIG), exclude=True),
        model.Item(model.Source("", 0, ITEMS), members=[model.Member("", "rsz", "ItemData", changes=[
            model.Change("_A", "S32", "1", "2"), model.Change("_B", "S32", "1", "3")])])])
    fake.pending = [data.id]
    entries = dict(m.mod_entries(data.id))
    entries.pop(CONFIG, None)
    entries[ITEMS] = (entries[ITEMS][0], "edits/" + ITEMS)
    real = m.mod_entries
    monkeypatch.setattr(m, "mod_entries", lambda mod_id: entries if mod_id == data.id else real(mod_id))
    opened = []
    monkeypatch.setattr(app, "open_mod_tool", lambda *a, **k: opened.append(a))
    app.refresh(select=data.id)
    root.update()
    info = app.info_text.get("1.0", "end")
    assert "수정\t뺀 파일 1개 · 바꾼 값 2개 (파일 1개) · 다시 적용 대기 1개   모드 수정에서 보기\n" in info
    assert "\t✕ 다시 적용 대기 1개\n" in info
    rows = [tuple(app.files_tree.item(i, "values")) for i in app.files_tree.get_children()]
    assert rows[-1] == (CONFIG, "뺌 (모드 수정)") and "disabled" in app.files_tree.item(
        app.files_tree.get_children()[-1], "tags")
    assert any(r[0] == ITEMS and r[1].endswith(" · 수정본 · 다시 적용 대기") for r in rows)
    # the link
    app.notebook.select(0)
    start = app.info_text.search("모드 수정에서 보기", "1.0")
    pump(root, lambda: app.info_text.winfo_ismapped())
    app.info_text.see(start)
    root.update()
    x, y, _w, _h = app.info_text.bbox(f"{start}+2c")
    app.info_text.event_generate("<Motion>", x=x + 1, y=y + 1)  # (where the text's tags see the mouse)
    app.info_text.event_generate("<Button-1>", x=x + 1, y=y + 1)
    root.update()
    assert opened == [(data.id, "summary")]
    # an unreadable record: why, and nothing else
    fake.records[data.id] = model.Record(error="JSON이 아닙니다")
    app.refresh(select=data.id)
    app._update_details()
    info = app.info_text.get("1.0", "end")
    assert "수정\t✕ 모드 수정 기록을 읽지 못해 쓰지 않고 있습니다: JSON이 아닙니다   모드 수정에서 보기\n" in info


def test_an_update_carries_the_edits(env, answers, monkeypatch, tmp_path):
    """Replacing a mod changed with 모드 수정: the question says the edits are applied to the new files; what
    could not be is offered in 수정 내용 (opened once the installs are done), else the status line says so."""
    app, m, mods, fake = env
    root = app.root
    data = mods["Data"]
    fake.records[data.id] = model.Record(rev=1, items=[model.Item(model.Source("", 0, CONFIG), exclude=True)])
    opened = []
    monkeypatch.setattr(app, "open_mod_tool", lambda *a, **k: opened.append(a))
    real_install = m.install_package
    notice = {"kind": "update", "at": "2026-10-28T21:14:05", "kept": 3, "reapplied": 1, "conflicts": 1,
              "pending": 0, "dropped": []}

    def install(session, variants, replace_id=None, progress=None):
        mod = real_install(session, variants, replace_id)
        fake.record(mod.id).notice = dict(notice)
        return mod

    monkeypatch.setattr(m, "install_package", install)
    archive = make_tree(tmp_path / "new" / "Data", {ITEMS: "u2", CONFIG: "Config = {}"})
    app.add_paths([str(archive)])
    pump(root, lambda: opened and not app._installing and app.busy is None)
    question = next(a for a in answers.asked if a[1] == "이미 설치된 모드")[2]
    assert "예: 기존 모드를 새 파일로 교체 (업데이트)\n(이 모드에서 수정한 내용은 새 파일에 다시 적용합니다.)\n" in question
    carried = next(a for a in answers.asked if a[1] == "모드 수정")
    assert carried == ("askyesno", "모드 수정", "'Data' 업데이트: 수정 5개 가운데 1개를 새 파일에 다시 적용하지 "
                                              "못했습니다. 지금 모드 수정 → 수정 내용에서 확인할까요?")
    assert opened == [(data.id, "summary")] and m.get(data.id).updated_at

    # all of them applied again: the status line, and the notice dropped
    fake.record(data.id).notice = dict(notice, conflicts=0)
    app._tell_carried_edits(m.get(data.id))
    assert app.status_var.get() == "업데이트하며 모드 수정을 다시 적용했습니다 (수정 4개)"
    assert ("drop_edit_notice", data.id, True) in fake.calls and fake.record(data.id).notice == {}

    # the old record could not be read: nothing was applied, and the question says why
    unreadable = model.CarryReport(error="수정 기록을 읽지 못해 다시 적용하지 않았습니다: 깨짐")
    fake.record(data.id).notice = unreadable.notice("2026-10-28T21:14:05")
    answers.yes.append(False)
    app._tell_carried_edits(m.get(data.id))
    assert answers.asked[-1][2] == ("'Data' 업데이트: 수정 기록을 읽지 못해 다시 적용하지 않았습니다: 깨짐. 지금 모드 수정 → "
                                    "수정 내용에서 확인할까요?")


def test_two_updates_open_both_summaries_one_after_another(env, answers, monkeypatch, tmp_path):
    """Two mods changed with 모드 수정 updated together (two archives dropped at once), each leaving values to check,
    and 예 to both questions: 수정 내용 of each opens, the second once the first window is closed and what was saved
    in it has run."""
    app, m, mods, fake = env
    root = app.root
    data, armor = mods["Data"], mods["Armor"]
    for mod in (data, armor):
        fake.records[mod.id] = model.Record(rev=1, items=[model.Item(model.Source("", 0, CONFIG), exclude=True)])
    notice = {"kind": "update", "at": "2026-10-28T21:14:05", "kept": 0, "reapplied": 0, "conflicts": 1,
              "pending": 0, "dropped": []}
    opened = []

    class Recorder:
        def __init__(self, parent, app_, mod_id, page=None, source=None, pending=None):
            opened.append((m.get(mod_id).name, page))

        def show(self):
            if len(opened) == 1:
                app.busy = "modedit"  # (a save of the first window still runs)
            return None

    patch_window(monkeypatch, Recorder)
    real_install = m.install_package

    def install(session, variants, replace_id=None, progress=None):
        mod = real_install(session, variants, replace_id)
        if replace_id:
            fake.record(mod.id).notice = dict(notice)
        return mod

    monkeypatch.setattr(m, "install_package", install)
    app.add_paths([str(make_tree(tmp_path / "new" / "Data", {ITEMS: "u2", CONFIG: "Config = {}"})),
                   str(make_tree(tmp_path / "new" / "Armor", {HELM: "m2"}))])
    pump(root, lambda: opened, timeout=30)
    assert len([a for a in answers.asked if a[1] == "모드 수정"]) == 2  # (예 to both)
    for _ in range(20):
        root.update()
        root.after(20)
    assert opened == [("Data", "summary")]  # (not while the first one's save runs)
    app.busy = None
    pump(root, lambda: len(opened) == 2)
    assert opened == [("Data", "summary"), ("Armor", "summary")] and app._edit_notice_mods == []


@pytest.mark.parametrize("fails", ["build", "question", "retarget"])
def test_the_next_summary_waits_while_a_save_of_the_one_before_is_unresolved(env, answers, monkeypatch, tmp_path,
                                                                             fails):
    """Two updated mods' 수정 내용 one after another: the first window's save fails (its build: the error box) or
    asks first (it waited for an apply, then a changed game copy: the question, 아니요), and under that message
    box the app's timers run (as on Windows: no grab). The next mod's window waits until the first one's is
    open again with its changes; after a new mod (다른 장비로 옮기기) that could not be made, until its error box is
    closed."""
    from mhwmm import gui, modtool
    from mhwmm.retargetview import Choice

    app, m, mods, fake = env
    root = app.root
    data, armor = mods["Data"], mods["Armor"]
    for mod in (data, armor):
        fake.records[mod.id] = model.Record(rev=1, items=[model.Item(model.Source("", 0, CONFIG), exclude=True)])
    notice = {"kind": "update", "at": "2026-10-28T21:14:05", "kept": 0, "reapplied": 0, "conflicts": 1,
              "pending": 0, "dropped": []}
    opened, boxes, under = [], [], []
    monkeypatch.setattr(app, "request_apply", lambda: None)

    class Recorder:
        def __init__(self, parent, app_, mod_id, page=None, source=None, pending=None):
            self.mod_id, self.name, self.pending, self.picked = mod_id, m.get(mod_id).name, pending, ""
            opened.append((self.name, page, pending is not None))

        def show(self):
            if self.name != "Data" or self.pending is not None:
                return None  # (the next mod's window, the changes again: closed as they are)
            if fails == "retarget":
                return Choice("ARMOR", "GANA", None, True, "")
            if fails == "question":  # (an apply started meanwhile: the save waits for it)
                app.busy = "deploy"
                root.after(300, lambda: setattr(app, "busy", None))
            plan = model.EditPlan(self.mod_id, m.get(self.mod_id).stamp, 1,
                                  exclude={model.Source("", 0, CONFIG): False})
            return modtool.EditChoice(plan, "summary")

    real_job, real_build = m.edit_job, m.build_edit

    def edit_job(plan, force=False):
        if fails == "question" and plan.mod_id == data.id and not force:
            raise GameCopyChanged("바뀜", [CONFIG])
        return real_job(plan, force)

    def build_edit(job, progress=None):
        if fails == "build" and job.mod_id == data.id:
            raise EditError("저장 공간이 모자랍니다")
        return real_build(job, progress)

    def ask(title, text, parent=None):
        answers.asked.append(("askyesno", title, text))
        if not text.startswith("게임 폴더의 다음 파일은"):
            return True  # (the updates and 확인할까요?)
        boxes.append(text)
        native_box(root)
        return False

    def error(title, text, parent=None):
        boxes.append(text)
        before = len(opened)
        native_box(root)
        under.append(opened[before:])  # (what opened under it)

    def create_retarget(*a, **k):
        raise EditError("저장 공간이 모자랍니다")

    patch_window(monkeypatch, Recorder)
    monkeypatch.setattr(m, "edit_job", edit_job)
    monkeypatch.setattr(m, "build_edit", build_edit)
    monkeypatch.setattr(m, "create_retarget", create_retarget, raising=False)
    monkeypatch.setattr(gui.messagebox, "askyesno", ask)
    monkeypatch.setattr(gui.messagebox, "showerror", error)
    real_install = m.install_package

    def install(session, variants, replace_id=None, progress=None):
        mod = real_install(session, variants, replace_id)
        if replace_id:
            fake.record(mod.id).notice = dict(notice)
        return mod

    monkeypatch.setattr(m, "install_package", install)
    app.add_paths([str(make_tree(tmp_path / "new" / "Data", {ITEMS: "u2", CONFIG: "Config = {}"})),
                   str(make_tree(tmp_path / "new" / "Armor", {HELM: "m2"}))])
    pump(root, lambda: len(opened) == (2 if fails == "retarget" else 3), timeout=30)
    native_box(root, timeout=0.3)
    assert len(boxes) == 1 and boxes[0].startswith({"build": "모드 수정을 저장하지 못했습니다", "question": "게임 폴더의 "
                                                    "다음 파일은", "retarget": "새 모드를 만들지 못했습니다"}[fails])
    again = [] if fails == "retarget" else [("Data", "summary", True)]  # (no changes to show again)
    assert opened == [("Data", "summary", False), *again, ("Armor", "summary", False)]
    assert under == ([] if fails == "question" else [[]])  # (nothing opened under the error box)
    assert app._edit_notice_mods == [] and app._tool_waiting == 0 and app.busy is None


def test_context_menu_and_toolbar_on_any_mod(env, monkeypatch):
    """모드 수정... in the list's menu for any mod but REFramework (a script mod too), on the tool that fits it."""
    from mhwmm import gui

    app, m, mods, fake = env
    opened = []

    class Dialog:
        def __init__(self, parent, app, mod_id, page=None, source=None, pending=None):
            opened.append((mod_id, page))

        def show(self):
            return None

    patch_window(monkeypatch, Dialog)
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

    found = entries(mods["Script"].id)
    app.context_menu.invoke(found[gui.MOD_TOOL_ACTION])
    assert opened == [(mods["Script"].id, None)]
    assert gui.MOD_TOOL_ACTION not in entries(mods["Framework"].id)
    assert app.editable(mods["Colors"]) and not app.editable(mods["Framework"])


def test_saving_with_the_manager(root, manager, tmp_path, monkeypatch):
    """The whole round with the manager's own 모드 수정 (not faked): a file left out in the window is saved by the
    app, the mod no longer puts it into the game, and the info and files tabs say so; 모두 원래대로 puts it back."""
    from mhwmm import gui

    with manager.prepare_install(make_tree(tmp_path / "src" / "Data", {ITEMS: "u", HELM: "m"})) as s:
        mod = manager.install_package(s, s.variants)
    manager.set_enabled(mod.id, True)
    manager.settings.auto_apply = False
    manager.settings.check_materials = False
    stub_pages(monkeypatch)
    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: True)
    monkeypatch.setattr(app, "warm_equipment", lambda: None)
    seen = choose(monkeypatch, leave_out_and_save)
    app.open_mod_tool(mod.id, "files")
    pump(root, lambda: app.busy is None and manager.excluded_files(mod.id))
    assert seen == [(mod.id, "files")] and manager.excluded_files(mod.id) == [HELM]
    assert HELM not in manager.mod_files(mod.id) and manager.dirty
    assert app.status_var.get().startswith("모드 수정을 저장했습니다: Data")
    app.refresh(select=mod.id)
    app._update_details()
    assert "수정\t뺀 파일 1개" in app.info_text.get("1.0", "end")
    rows = [tuple(app.files_tree.item(i, "values")) for i in app.files_tree.get_children()]
    assert rows[-1] == (HELM, "뺌 (모드 수정)")

    def reset(dialog):
        dialog.pending.reset_all()
        dialog.pending_changed()
        dialog.ok()

    choose(monkeypatch, reset)
    app.open_mod_tool(mod.id, "summary")
    pump(root, lambda: app.busy is None and not manager.excluded_files(mod.id))
    assert HELM in manager.mod_files(mod.id) and manager.edit_summary(mod.id).empty


def test_the_window_is_made_of_the_real_pages_on_the_real_manager(root, manager, tmp_path):
    """No page is a stand-in (MissingPage: a module that did not import or a page that failed to build): every
    tool of the shell is its page of modeditview or retargetview, working on the manager's own 모드 수정 API."""
    from mhwmm import gui, modtool

    with manager.prepare_install(make_tree(tmp_path / "src" / "Data", {ITEMS: "u", CONFIG: "Config = {}"})) as s:
        mod = manager.install_package(s, s.variants)
    manager.settings.check_materials = False
    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: True)
    dialog = window(app, mod.id, page="config")
    try:
        assert {key: type(page).__name__ for key, page in dialog.pages.items()} == {
            "files": "FilesPage", "data": "ValuesPage", "config": "ValuesPage", "retarget": "RetargetPage",
            "summary": "SummaryPage"}
        assert not modtool._MISSING and dialog.page_key == "config"
        page = dialog.pages["config"]
        pump(root, lambda: page.ready_view is not None, timeout=20)  # (edit_targets, open_values on a worker)
        assert page.file_box.get().startswith("Config.lua")
        assert [page.rows[i].value.at for i in page.iids.values()] == []  # (a table, no literal value)
        assert set(dialog.pages["files"].tree.get_children()) == {CONFIG, ITEMS}
    finally:
        dialog.destroy()
