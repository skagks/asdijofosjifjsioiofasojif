import gc
import queue
import sys
import time

import pytest

from conftest import make_zip

tk = pytest.importorskip("tkinter")


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
    """A message box left open would block the test, so answer them all."""
    from mhwmm import gui

    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: None)
    for name in ("askyesno", "askyesnocancel"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: True)

    def run_anyway(self):  # 실행 전 점검 before a launch: 그래도 실행
        self.destroy()
        return ("run",)

    monkeypatch.setattr(gui.PrecheckDialog, "show", run_anyway)


def pump(root, until, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        root.update()
        if until():
            return
        time.sleep(0.02)
    raise AssertionError("시간 초과")


def test_gui_toggle_applies(root, manager, tmp_path, game_dir, monkeypatch):
    from mhwmm import gui

    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: None)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)

    with manager.prepare_install(make_zip(tmp_path / "A.zip", {"natives/STM/a.tex": "a", "x.pak": "p"})) as s:
        a = manager.install_variant(s, s.variants[0])
    with manager.prepare_install(make_zip(tmp_path / "B.zip", {"natives/STM/a.tex": "b"})) as s:
        b = manager.install_variant(s, s.variants[0])

    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: True)
    assert list(app.tree.get_children()) == [a.id, b.id]
    assert "밀림 1" in app.tree.item(a.id, "values")[6]

    app.tree.selection_set(a.id)
    app._update_details()
    statuses = {app.files_tree.item(i, "values")[0]: app.files_tree.item(i, "values")[1]
                for i in app.files_tree.get_children()}
    assert statuses["natives/STM/a.tex"].startswith("덮어써짐")

    app.apply()
    pump(root, lambda: app.busy is None and not manager.dirty)
    assert (game_dir / "natives/STM/a.tex").read_text() == "b"
    assert (game_dir / "pak_mods/001_A.pak").read_text() == "p"

    app.toggle([b.id])  # auto-apply kicks in
    pump(root, lambda: app.busy is None and not manager.dirty and not app._apply_pending)
    assert (game_dir / "natives/STM/a.tex").read_text() == "a"
    assert app.tree.item(b.id, "image")[0] == str(app.theme.image("check_off", 20))

    # a mod with options starts with all of them off and is applied once one is chosen
    with manager.prepare_install(make_zip(tmp_path / "Opts.zip", {
            "Opts/One/natives/STM/o.tex": "1", "Opts/Two/natives/STM/o.tex": "2"})) as s:
        opts = manager.install_package(s, s.variants)
    app.refresh()
    assert app.tree.item(opts.id, "values")[1] == "⚠ Opts"  # no option on yet: marked as a problem
    assert app.tree.item(opts.id, "values")[4] == "선택 필요" and "problem" in app.tree.item(opts.id, "tags")
    app.show_options(opts.id)
    assert app.notebook.select() == str(app.options_tab)
    app._option_vars[1][1].set(True)
    app._on_options_changed(opts.id)
    pump(root, lambda: app.busy is None and not manager.dirty and not app._apply_pending)
    assert (game_dir / "natives/STM/o.tex").read_text() == "2"
    assert app.tree.item(opts.id, "values")[4] == "1/2"

    # a newer release shows the update button and its dialog
    from mhwmm.updater import Release
    release = Release("9.9.9", "v9.9.9", "### 변경 내역\n\n- **새 기능**", "https://example", None, 0, None)
    app._on_update_checked(release, None, False)
    assert app.update_button.winfo_manager() == "pack"
    assert "v9.9.9" in app.update_button.cget("text")
    older = Release("9.9.8", "v9.9.8", "### 변경 내역\n\n- 이전 수정", "https://example", None, 0, None, "2026-10-18")
    release.history = [release, older]
    dialog = gui.UpdateDialog(root, app.theme, release, can_install=True)
    shown = dialog.body.winfo_children()[2].get("1.0", "end")
    assert "v9.9.9" in shown and "v9.9.8" in shown and "이전 수정" in shown
    dialog.destroy()

    app.apply_theme("light")  # switching themes repaints without errors
    app.search_var.set("zzz")
    assert app.tree.get_children() == ()
    app.search_var.set("")
    assert len(app.tree.get_children()) == 3


def test_gui_options_show_pictures(root, manager, tmp_path, monkeypatch):
    from tkinter import ttk

    from mhwmm import gui

    def picture(name, color):
        img = tk.PhotoImage(master=root, width=320, height=180)
        img.put(color, to=(0, 0, 320, 180))
        img.write(str(tmp_path / name), format="png")
        return (tmp_path / name).read_bytes()

    archive = make_zip(tmp_path / "Pack.zip", {
        "Pack/One/natives/STM/a.tex": "1", "Pack/One/preview.png": picture("1.png", "#c03030"),
        "Pack/Two/natives/STM/a.tex": "2", "Pack/Two/preview.png": picture("2.png", "#3030c0"),
        "Pack/Three/natives/STM/a.tex": "3",
    })
    with manager.prepare_install(archive) as s:
        mod = manager.install_package(s, s.variants)
    manager.settings.auto_apply = False
    app = gui.App(root, manager, queue.Queue())
    app.show_options(mod.id)

    def widgets(parent):
        for w in parent.winfo_children():
            yield w
            yield from widgets(w)

    def texts(parent):
        return [str(w.cget("text")) for w in widgets(parent) if isinstance(w, (ttk.Label, ttk.Button))]

    # "모두 켜기/끄기" stay above the list instead of after the last option
    assert {"모두 켜기", "모두 끄기"} <= set(texts(app.options_head))
    assert "모두 켜기" not in texts(app.options_inner)

    # thumbnails load after the tab is drawn; an option without a picture has no box at all
    pump(root, lambda: len(app._option_images) == 2)
    thumbs = [w for w in widgets(app.options_inner) if isinstance(w, ttk.Label) and str(w.cget("style")) == "Thumb.TLabel"]
    assert [bool(str(w.cget("image"))) for w in thumbs] == [True, True]  # One, Two (Three has none)
    assert "그림 없음" not in texts(app.options_inner)

    # switching an option rebuilds the list with the cached thumbnails right away
    app._option_vars[0][1].set(True)
    app._on_options_changed(mod.id)
    root.update()
    assert manager.get(mod.id).selected == [mod.options[0]["id"]] and len(app._option_images) == 2

    # a click shows the picture large
    shown = []
    monkeypatch.setattr(gui.ImageDialog, "show", lambda self: shown.append((self.title(), self.image)) or self.destroy())
    thumb = next(w for w in widgets(app.options_inner)
                 if isinstance(w, ttk.Label) and str(w.cget("style")) == "Thumb.TLabel" and str(w.cget("image")))
    app.show_picture(manager.option_preview(mod.id, mod.options[0]), "One")
    assert shown[-1][0] == "One" and shown[-1][1] is not None
    thumb.event_generate("<Button-1>")
    root.update()
    assert len(shown) == 2


def _quiet(root, seconds=0.8):
    """Let the window settle (the next change is one on its own, not a step of a drag)."""
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def _configures(widget):
    """Counts the <Configure> events of ``widget`` (each one is a new layout of what is in it)."""
    seen = []
    widget.bind("<Configure>", lambda e: seen.append((e.width, e.height)), add="+")
    return seen


def test_freeze_content_while_the_window_is_dragged(root, monkeypatch):
    """The window's content keeps its size while the window's edge is dragged (laying all of it out and drawing
    it again on every step made resizing stutter) and fills the window once the drag stops, laid out once; the
    drag's first step and a change on its own (maximize, restore) are followed at once."""
    from tkinter import ttk

    from mhwmm import gui

    monkeypatch.setattr(gui, "RESIZE_SETTLE_MS", 600)  # (a slow machine must not make the drag look finished)
    monkeypatch.setattr(gui, "RESIZE_FIRST_MS", 600)
    root.maxsize(4000, 3000)  # (Windows keeps a window within the screen unless told otherwise)
    root.geometry("600x400")
    content = ttk.Frame(root)
    freeze = gui.FreezeContent(root, content)
    inside = ttk.Frame(content)
    inside.pack(fill="both", expand=True)
    laid_out = _configures(inside)

    def size(widget):
        return widget.winfo_width(), widget.winfo_height()

    pump(root, lambda: size(inside) == size(root) == (600, 400))  # the first layout at once
    assert content.winfo_manager() == "place" and not freeze.frozen
    for width in (620, 630):  # changes right after the window is shown (zoomed, moved to its monitor): at once
        root.geometry(f"{width}x400")
        root.update()
        assert size(inside) == (width, 400) and not freeze.frozen
    _quiet(root)
    root.geometry("640x400")  # the drag's first step (or a change on its own): at once
    root.update()
    assert size(inside) == (640, 400) and not freeze.frozen and not gui.resizing(root)
    laid_out.clear()
    for width, height in ((680, 400), (720, 430), (700, 460), (610, 380)):  # dragged on: the content waits
        root.geometry(f"{width}x{height}")
        root.update()
        assert size(root) == (width, height) and gui.resizing(root) and freeze.frozen
        assert size(content) == size(inside) == (640, 400)  # (cut off where the window got smaller)
    assert laid_out == []
    pump(root, lambda: size(inside) == (610, 380))  # ... and follows once it stopped: laid out once
    assert laid_out == [(610, 380)] and not freeze.frozen and freeze._after is None
    assert content.place_info()["relwidth"] == "1" and content.place_info()["width"] == ""

    for width, height in ((900, 600), (700, 500), (860, 560)):  # maximize, restore (one change each): at once
        _quiet(root)
        root.geometry(f"{width}x{height}")
        root.update()
        assert size(inside) == (width, height) and not freeze.frozen

    monkeypatch.setattr(gui, "RESIZE_FREEZE", False)  # (switched off: every step is followed, as before)
    _quiet(root)
    for width in (880, 900, 920):
        root.geometry(f"{width}x560")
        root.update()
        assert size(inside) == (width, 560) and not freeze.frozen
    monkeypatch.setattr(gui, "RESIZE_FREEZE", True)

    _quiet(root)
    for width in (940, 960):
        root.geometry(f"{width}x560")
        root.update()
    assert freeze.frozen and freeze._after is not None
    errors = []
    root.report_callback_exception = lambda *exc: errors.append(exc)
    content.destroy()  # a drag still settling when the window closes
    assert freeze._after is None
    _quiet(root)
    assert not errors


def test_freeze_content_a_slow_first_step_is_not_a_pause(root, monkeypatch):
    """The drag's first step is followed at once; when laying the window out for it takes longer than
    RESIZE_SETTLE_MS (all the tiles of 장비 외형 at 150%), the next steps are still frozen (each one laid out at
    once again would make the drag stutter)."""
    from tkinter import ttk

    from mhwmm import gui

    monkeypatch.setattr(gui, "RESIZE_SETTLE_MS", 200)
    monkeypatch.setattr(gui, "RESIZE_FIRST_MS", 1500)
    monkeypatch.setattr(gui, "_pointer", lambda: None)  # (the time rule; on Windows the mouse would say "up")
    root.maxsize(4000, 3000)
    root.geometry("600x400")
    content = ttk.Frame(root)
    gui.FreezeContent(root, content)
    inside = ttk.Frame(content)
    inside.pack(fill="both", expand=True)
    laid_out = []

    def slow(event):  # a layout that takes longer than RESIZE_SETTLE_MS
        laid_out.append(event.width)
        time.sleep(0.35)

    inside.bind("<Configure>", slow)
    pump(root, lambda: inside.winfo_width() == 600)
    _quiet(root, 1.6)
    laid_out.clear()
    root.geometry("640x400")  # the first step: at once (and slow)
    root.update()
    assert laid_out == [640]
    for width in (660, 680, 700):  # (the second step comes more than RESIZE_SETTLE_MS after the first)
        root.geometry(f"{width}x400")
        root.update()
        assert inside.winfo_width() == 640 and gui.resizing(root)
    pump(root, lambda: inside.winfo_width() == 700)
    assert laid_out == [640, 700]  # once more, when the drag stopped


class _FakePointer:
    """A stand-in for Windows' mouse and frame queries (gui._pointer): ``down`` the mouse button down (or Windows'
    move or size loop), ``border`` the cursor on the window's sizing border, ``origin`` the frame's top-left corner
    on the screen (its size is the window's), ``spans`` as big as the work area; ``fail`` the mouse not readable."""

    def __init__(self, **setup):
        self.set(**setup)

    def set(self, down=False, border=False, spans=False, fail=False, origin=(0, 0)):
        self.down, self.border, self.spans, self.fail, self.origin = down, border, spans, fail, origin

    def dragging(self, top):
        if self.fail:
            raise OSError("no mouse")
        return self.down

    def on_sizing_border(self, top):
        return self.border

    def where(self, top):
        x, y = self.origin
        return (x, y, x + top.winfo_width(), y + top.winfo_height()), self.spans


def _size(widget):
    return widget.winfo_width(), widget.winfo_height()


def _content(root, monkeypatch, settle_ms=600):
    """A 600×400 window with its content in a FreezeContent, laid out and quiet (the next change is one on its
    own): (content, freeze, a frame filling the content)."""
    from tkinter import ttk

    from mhwmm import gui

    monkeypatch.setattr(gui, "RESIZE_SETTLE_MS", settle_ms)  # (a slow machine must not make the drag look finished)
    monkeypatch.setattr(gui, "RESIZE_FIRST_MS", 600)
    root.maxsize(4000, 3000)  # (Windows keeps a window within the screen unless told otherwise)
    root.geometry("600x400")
    content = ttk.Frame(root)
    freeze = gui.FreezeContent(root, content)
    inside = ttk.Frame(content)
    inside.pack(fill="both", expand=True)
    pump(root, lambda: _size(inside) == (600, 400))
    _quiet(root, 1.3)  # (longer than RESIZE_SETTLE_MS + RESIZE_FIRST_MS)
    return content, freeze, inside


def test_resize_modes(root, monkeypatch):
    """gui.RESIZE_MODE picks what the content does during a drag (RESIZE_FREEZE = False, the older switch, is
    "off"; any other value is the freeze): off lays it out on every step, freeze keeps it as it is."""
    from mhwmm import gui

    for mode, freeze_on, expected in (("freeze", True, "freeze"), ("off", True, "off"), ("freeze", False, "off"),
                                      ("off", False, "off"), ("snapshot", True, "freeze"), ("other", True, "freeze")):
        monkeypatch.setattr(gui, "RESIZE_MODE", mode)
        monkeypatch.setattr(gui, "RESIZE_FREEZE", freeze_on)
        assert gui.resize_mode() == expected
    monkeypatch.setattr(gui, "RESIZE_FREEZE", True)
    monkeypatch.setattr(gui, "_pointer", lambda: None)  # (the drag's first step is one on its own)
    content, freeze, inside = _content(root, monkeypatch)
    laid_out = _configures(inside)
    for mode in ("off", "freeze"):
        monkeypatch.setattr(gui, "RESIZE_MODE", mode)
        _quiet(root, 1.3)
        laid_out.clear()
        width = root.winfo_width()
        for k in (1, 2, 3):
            root.geometry(f"{width + 20 * k}x400")
            root.update()
        assert freeze.frozen == (mode != "off")
        assert len(laid_out) == (3 if mode == "off" else 1)  # (the first step at once)
        pump(root, lambda: not freeze.frozen and _size(inside) == (width + 60, 400))


def test_resize_defaults_freeze_the_content_in_place(root, monkeypatch):
    """The defaults: the content is frozen during a drag (it stays in the window, mapped, at its size), Tk's
    window classes lose CS_HREDRAW|CS_VREDRAW (Windows) and the mouse tells the drag's first step (Windows). Once
    the drag stops the content is laid out once (timed for the resize benchmark)."""
    from mhwmm import gui

    assert gui.RESIZE_MODE == "freeze" and gui.RESIZE_FREEZE and gui.resize_mode() == "freeze"
    assert gui.RESIZE_CLASS_REDRAW == "both" and gui.RESIZE_FIRST_STEP is True
    monkeypatch.setattr(gui, "_pointer", lambda: None)  # (the time rules: on Windows the mouse would say "up")
    content, freeze, inside = _content(root, monkeypatch)
    laid_out = _configures(inside)
    for width in (620, 640, 660):
        root.geometry(f"{width}x400")
        root.update()
    assert freeze.frozen and _size(content) == (620, 400) and laid_out == [(620, 400)]  # (the first step at once)
    assert content.place_info()["x"] == "0" and content.winfo_x() == 0 and content.winfo_y() == 0
    assert content.winfo_viewable() and inside.winfo_viewable()
    assert [what for what, _ in freeze.timings] == []
    pump(root, lambda: not freeze.frozen and _size(inside) == (660, 400))
    assert laid_out == [(620, 400), (660, 400)] and [what for what, _ in freeze.timings] == ["restore"]
    assert content.place_info()["relwidth"] == "1" and content.place_info()["width"] == ""


def test_freeze_window_closed_or_thawed_mid_drag(root, monkeypatch):
    """The content follows the window at once when that is asked for during a drag (the main window saves its
    state when it closes), and a content destroyed or a window closed during a drag leaves no callback behind."""
    from tkinter import ttk

    from mhwmm import gui

    errors = []
    root.report_callback_exception = lambda *exc: errors.append(exc)
    monkeypatch.setattr(gui, "_pointer", lambda: None)
    content, freeze, inside = _content(root, monkeypatch)

    def drag(top, start):
        for k in (1, 2, 3):
            top.geometry(f"{start + 20 * k}x400")
            top.update()

    drag(root, 600)
    assert freeze.frozen and freeze._after is not None
    assert freeze.thaw() and not freeze.thaw()  # (False: no longer frozen)
    assert not freeze.frozen and freeze._after is None
    assert content.place_info()["x"] == "0" and content.place_info()["relwidth"] == "1"
    root.update()
    assert _size(inside) == (660, 400)
    _quiet(root, 1.3)
    drag(root, 660)
    assert freeze.frozen
    content.destroy()
    assert freeze._after is None
    _quiet(root)

    top = tk.Toplevel(root)
    top.maxsize(4000, 3000)
    top.geometry("500x400")
    other = ttk.Frame(top)
    other_freeze = gui.FreezeContent(top, other)
    ttk.Frame(other).pack(fill="both", expand=True)
    pump(top, lambda: _size(other) == (500, 400))
    _quiet(root, 1.3)
    drag(top, 500)
    assert other_freeze.frozen
    top.destroy()  # closed during the drag
    assert other_freeze._after is None
    _quiet(root)
    assert not errors


def test_first_step_of_a_drag_is_frozen_when_the_mouse_says_so(root, monkeypatch):
    """Windows (RESIZE_FIRST_STEP): a size change while the mouse button is down (or in Windows' move or size
    loop) with the cursor on the window's sizing border, or one that kept at least two of the frame's edges and
    is not as big as the work area before or after, is a drag step from the drag's first step on: frozen at once
    A change of the wm state (maximize, restore: also a title-bar double-click, while the
    button is down, with the cursor where the restored window's top border is), unsnapping by dragging the title
    bar (the window moves), the button up, the switch off or the mouse not readable: followed at once."""
    from mhwmm import gui

    pointer = _FakePointer()
    monkeypatch.setattr(gui, "_pointer", lambda: pointer)
    state = ["normal"]
    root.wm_state = lambda *a: state[0]  # (what Tk would say: "zoomed" after a maximize)
    content, freeze, inside = _content(root, monkeypatch)
    laid_out = _configures(inside)

    def first_step(before=None, grow=(20, 0), **setup):
        """Is a change after a quiet while frozen at once? ``setup``: the pointer and the wm state (``state``) at
        the change; ``before``: at the Configure before it (a move; by default the state and ``spans`` of the
        change, at the origin)."""
        _quiet(root, 1.3)
        pump(root, lambda: not freeze.frozen)
        width, height = _size(root)
        prior = {"state": setup.get("state", "normal"), "spans": setup.get("spans", False), **(before or {})}
        state[0] = prior.pop("state")
        pointer.set(**prior)
        root.event_generate("<Configure>", width=width, height=height)  # (moved: where it is, is noted)
        state[0] = setup.pop("state", "normal")
        pointer.set(**setup)
        laid_out.clear()
        size = (width + grow[0], height + grow[1])
        root.geometry("%dx%d" % size)
        root.update()
        assert _size(root) == size
        frozen = freeze.frozen
        if frozen:
            assert laid_out == [] and _size(content) == (width, height)
            pump(root, lambda: not freeze.frozen)  # (and followed once the drag stops)
            assert _size(inside) == size and len(laid_out) == 1
        else:
            assert len(laid_out) == 1 and _size(inside) == size
        return frozen

    assert not first_step()  # the button up: Win+arrow keys, the maximize button, a change made by the app
    assert first_step(down=True, border=True)  # on the sizing border (also a double-click on it: frozen a while)
    assert first_step(down=True)  # not on the border (the cursor ahead of the edge): the right edge moved
    assert first_step(down=True, grow=(20, -10))  # ... a corner: two edges kept
    assert first_step(down=True, border=True, spans=True)  # the free edge of a window snapped to half the screen
    assert not first_step(down=True, spans=True)  # ... not on the border: as big as the work area
    # a snapped window's title bar dragged: it gets its size from before the snap (as big as the work area before;
    # a quarter of the screen: not, but it moved)
    assert not first_step(down=True, before=dict(spans=True), grow=(30, -20))
    assert not first_step(down=True, origin=(-100, 0), grow=(30, -20))
    # a title-bar double-click restore (it acts on the button's press) with the restored window near the top of
    # the screen: the cursor is where the restored window's top border is, but the state changed
    assert not first_step(down=True, border=True, before=dict(state="zoomed", spans=True), grow=(-20, -20))
    # a title-bar double-click maximize, also with the cursor on where the border was
    assert not first_step(down=True, border=True, state="zoomed", spans=True, before=dict(state="normal"))
    assert not first_step(down=True, border=True, fail=True)  # the mouse not readable
    monkeypatch.setattr(gui, "RESIZE_FIRST_STEP", False)
    assert not first_step(down=True, border=True)
    monkeypatch.setattr(gui, "RESIZE_FIRST_STEP", True)
    assert first_step(down=True, border=True, state="zoomed")  # (the state as before: on the border, a drag)


class _FakeUser32:
    """user32 for gui._WinPointer (gui._win_functions patched; no Windows needed): one window, 0x1a2b, on one
    monitor whose work area is 1920×1040; a byref'd structure is filled through its ``_obj``."""

    HWND, THREAD = 0x1A2B, 77

    def __init__(self):
        self.left = self.right = self.swapped = self.loop = False
        self.contents = True  # ("show window contents while dragging")
        self.cursor, self.work = (0, 0), (0, 0, 1920, 1040)
        self.normal(100, 300, 900, 700)

    def normal(self, left, top, width, height):
        """A normal window: the frame's invisible 8 px resize border left, right and below, a 31 px title bar."""
        self.rect = (left, top, left + width, top + height)
        self.origin, self.client = (left + 8, top + 31), (width - 16, height - 39)

    def zoomed(self):
        """Maximized: the frame 8 px beyond the work area, a 23 px title bar at the top of the screen."""
        self.rect, self.origin, self.client = (-8, -8, 1928, 1048), (0, 23), (1920, 1017)

    def functions(self, dll, specs):
        assert dll == "user32"
        return {name: getattr(self, name) for name in specs}

    @staticmethod
    def _fill(arg, **values):
        target = getattr(arg, "_obj", arg)
        for name, value in values.items():
            setattr(target, name, value)
        return 1

    def _rect(self, arg, rect):
        left, top, right, bottom = rect
        return self._fill(arg, left=left, top=top, right=right, bottom=bottom)

    def GetAsyncKeyState(self, vk):
        return -32768 if (vk, True) in ((1, self.left), (2, self.right)) else 0

    def GetSystemMetrics(self, index):
        return {23: int(self.swapped), 33: 4, 92: 4}[index]

    def GetCursorPos(self, point):
        return self._fill(point, x=self.cursor[0], y=self.cursor[1])

    def GetWindowRect(self, hwnd, rect):
        assert hwnd == self.HWND
        return self._rect(rect, self.rect)

    def GetClientRect(self, hwnd, rect):
        assert hwnd == self.HWND
        return self._rect(rect, (0, 0) + self.client)

    def ClientToScreen(self, hwnd, point):
        assert hwnd == self.HWND
        point = getattr(point, "_obj", point)
        return self._fill(point, x=point.x + self.origin[0], y=point.y + self.origin[1])

    def MonitorFromWindow(self, hwnd, flags):
        assert hwnd == self.HWND and flags == 2
        return 5

    def GetMonitorInfoW(self, monitor, info):
        import ctypes

        target = getattr(info, "_obj", info)
        assert monitor == 5 and target.cbSize == ctypes.sizeof(target)
        return self._rect(target.rcWork, self.work)

    def GetWindowThreadProcessId(self, hwnd, process):
        assert hwnd == self.HWND and process is None
        return self.THREAD

    def GetGUIThreadInfo(self, thread, info):
        import ctypes

        target = getattr(info, "_obj", info)
        assert thread == self.THREAD and target.cbSize == ctypes.sizeof(target)
        return self._fill(target, flags=0x2 if self.loop else 0)

    def SystemParametersInfoW(self, action, param, value, ini):
        assert action == 0x26 and param == ini == 0
        return self._fill(value, value=int(self.contents))


class _StubTop:
    """The toplevel _WinPointer is asked about: its frame's handle and its wm state."""

    state = "normal"

    def wm_frame(self):
        return "0x1a2b"

    def wm_state(self):
        return self.state


def test_win_pointer_reads_the_frame_and_the_mouse(monkeypatch):
    """_WinPointer (RESIZE_FIRST_STEP) over a fake user32: where the cursor is on the frame (the sizing border
    and just outside it, not the client area or the title bar), whether the frame is as big as the work area,
    the primary button (also swapped), Windows' move or size loop and an outline drag; and _drag_step for a drag
    of the edge, a title-bar double-click restore near the top of the screen, unsnapping and the button up."""
    from mhwmm import gui

    user32 = _FakeUser32()
    monkeypatch.setattr(gui, "_win_functions", user32.functions)
    pointer, top = gui._WinPointer(), _StubTop()

    def border(x, y):
        user32.cursor = (x, y)
        return pointer.on_sizing_border(top)

    # a normal window at (100, 300), 900×700: the client area from (108, 331) to (992, 992)
    for x, y in ((1003, 600), (1012, 600), (101, 600), (90, 600), (500, 995), (500, 303), (500, 290), (995, 995)):
        assert border(x, y), (x, y)  # the right, left and bottom edges, the top band, and up to 16 px outside
    for x, y in ((500, 600), (500, 315), (1020, 600), (500, 280), (1300, 600)):
        assert not border(x, y), (x, y)  # the client area, the title bar, further away
    assert pointer.where(top) == ((100, 300, 1000, 1000), False)
    user32.zoomed()
    assert not border(900, 10)  # a maximized window's title bar
    assert pointer.where(top) == ((-8, -8, 1928, 1048), True)
    user32.rect = (-8, 0, 968, 1048)  # snapped to the left half: as tall as the work area
    assert pointer.where(top)[1]
    user32.rect = (-8, 0, 968, 528)  # a quarter of the screen
    assert not pointer.where(top)[1]

    assert not pointer.dragging(top)
    user32.left = True
    assert pointer.button_down() and pointer.dragging(top)
    user32.swapped = True  # (the buttons swapped: the primary one is the right one)
    assert not pointer.button_down()
    user32.right = True
    assert pointer.button_down()
    user32.left = user32.right = user32.swapped = False
    user32.loop = True  # (a pen or a finger: the button not seen, but Windows' move or size loop runs)
    assert not pointer.button_down() and pointer.in_move_size(top) and pointer.dragging(top)
    user32.left = True
    user32.contents = False  # (an outline drag: Windows sizes the window once, when the drag ends)
    assert not pointer.dragging(top)
    user32.left = user32.loop = False
    user32.contents = True

    def drag_step(change, by_last=False, by_alone=False, state="normal", **mouse):
        """_drag_step for ``change`` (it changes user32's window) from the window as it is now."""
        before = gui._place(top, pointer)
        change()
        top.state = state
        user32.__dict__.update(mouse)
        try:
            return gui._drag_step(top, pointer, before, gui._place(top, pointer), by_last, by_alone)
        finally:
            user32.left = user32.loop = False

    user32.normal(100, 300, 900, 700)
    wider = lambda: user32.normal(100, 300, 920, 700)  # noqa: E731
    assert drag_step(wider, left=True, cursor=(1010, 600))  # the right edge dragged: on the border
    user32.normal(100, 300, 900, 700)
    assert drag_step(wider, left=True, cursor=(1040, 600))  # ... the cursor ahead of it: three edges kept
    user32.normal(100, 300, 900, 700)
    assert drag_step(wider, loop=True, cursor=(1010, 600))  # (with a finger)
    user32.normal(100, 300, 900, 700)
    assert not drag_step(wider, cursor=(1010, 600), by_alone=True)  # the button up: on its own, unless ...
    user32.normal(100, 300, 900, 700)
    assert drag_step(wider, cursor=(1010, 600), by_last=True)  # ... right after the last change
    # the free edge of a window snapped to the left half (as tall as the work area) dragged, the cursor ahead of it:
    # neither on the border nor resized in place, so with the button down by the time rules (a step right after the
    # last change, or soon after a change on its own or after a pause in the drag, FreezeContent._settled)
    snapped_wider = lambda: user32.__dict__.update(rect=(-8, 0, 988, 1048), client=(972, 1009))  # noqa: E731
    for down in (dict(left=True), dict(loop=True)):
        for by_last, by_alone in ((False, False), (True, False), (False, True)):
            user32.rect, user32.origin, user32.client = (-8, 0, 968, 1048), (0, 31), (952, 1009)
            assert drag_step(snapped_wider, by_last, by_alone, cursor=(1030, 600), **down) is (by_last or by_alone)
            assert not pointer.on_sizing_border(top) and pointer.where(top)[1]
    user32.zoomed()
    top.state = "zoomed"
    # a title-bar double-click on the maximized window at (900, 15), restored to 1240×800 at (200, 10): the cursor
    # is in the restored window's top band, but the state changed
    restore = lambda: user32.normal(200, 10, 1240, 800)  # noqa: E731
    assert not drag_step(restore, left=True, cursor=(900, 15))
    assert border(900, 15)
    user32.rect, user32.origin, user32.client = (-8, 0, 968, 1048), (0, 31), (952, 1009)  # snapped to the left
    # its title bar dragged: its size from before the snap, under the cursor
    assert not drag_step(lambda: user32.normal(-120, 0, 1240, 800), left=True, cursor=(500, 15))
    user32.rect, user32.origin, user32.client = (-8, 0, 968, 528), (0, 31), (952, 489)  # a quarter of the screen
    assert not drag_step(lambda: user32.normal(-120, 0, 1240, 800), left=True, cursor=(500, 15))
    user32.normal(100, 300, 900, 700)

    def broken(*args):
        raise OSError("no mouse")

    monkeypatch.setattr(user32, "GetAsyncKeyState", broken)
    pointer = gui._WinPointer()
    assert not drag_step(wider, cursor=(1010, 600))  # the mouse not readable: the time rules
    user32.normal(100, 300, 900, 700)
    assert drag_step(wider, cursor=(1010, 600), by_alone=True)


class _FakeClassUser32:
    """user32 for gui._WinClasses (gui._win_functions patched): windows {handle: [class name, parent, process]} and
    class styles by name. ``wide``: GetClassLongPtrW/SetClassLongPtrW exist (64-bit), else only the ...Long ones."""

    INNER, OUTER = 0x501, 0x777

    def __init__(self, wide=True, child="TkChild", top="TkTopLevel"):
        import os

        self.wide = wide
        self.windows = {self.INNER: [child, self.OUTER, os.getpid()], self.OUTER: [top, 0, os.getpid()]}
        self.styles = {child: 0x0003 | 0x0008, top: 0x0003}  # (CS_DBLCLKS too: the other bits stay)
        self.calls: list = []
        self.stuck = False  # SetClassLong... changes nothing

    def functions(self, dll, specs):
        assert dll == "user32"
        wide = {"GetClassLongPtrW", "SetClassLongPtrW"}
        if not self.wide and wide & set(specs):
            raise AttributeError("GetClassLongPtrW")  # (as a 32-bit user32)
        return {name: getattr(self, name) for name in specs}

    def GetClassNameW(self, hwnd, buf, size):
        self.calls.append(("name", hwnd))
        name = self.windows[hwnd][0]
        buf.value = name
        return len(name)

    def GetParent(self, hwnd):
        return self.windows[hwnd][1] or None

    def GetWindowThreadProcessId(self, hwnd, process):
        getattr(process, "_obj", process).value = self.windows[hwnd][2]
        return 9

    def _get(self, hwnd, index):
        assert index == -26  # GCL_STYLE
        return self.styles[self.windows[hwnd][0]]

    def _set(self, hwnd, index, value):
        assert index == -26
        name = self.windows[hwnd][0]
        self.calls.append(("set", name, value))
        old = self.styles[name]
        if not self.stuck:
            self.styles[name] = value
        return old

    GetClassLongPtrW = GetClassLongW = _get
    SetClassLongPtrW = SetClassLongW = _set


class _ClassTop:
    """A toplevel for class_redraw: its own window's handle and `wm frame` (its own handle until it is shown)."""

    def __init__(self, shown=True):
        self.shown = shown

    def winfo_id(self):
        return _FakeClassUser32.INNER

    def wm_frame(self):
        return hex(_FakeClassUser32.OUTER if self.shown else _FakeClassUser32.INNER)


def _classes(monkeypatch, value, **user32):
    """gui's RESIZE_CLASS_REDRAW set to ``value`` over a fake user32, nothing done yet: the fake."""
    from mhwmm import gui

    fake = _FakeClassUser32(**user32)
    monkeypatch.setattr(gui, "_win_functions", fake.functions)
    monkeypatch.setattr(gui, "_CLASSES", [gui._WinClasses()])
    monkeypatch.setattr(gui, "_CLASS_REDRAW", {})
    monkeypatch.setattr(gui, "RESIZE_CLASS_REDRAW", value)
    return fake


def test_class_redraw_takes_the_styles_off_the_classes_it_finds(monkeypatch):
    """RESIZE_CLASS_REDRAW (Windows, over a fake user32): the classes are those GetClassNameW names for the
    toplevel's own window (child) and for its parent that `wm frame` names (top, once the toplevel has been
    shown); only CS_HREDRAW|CS_VREDRAW go, once; "keep" changes nothing; 32-bit user32 has only ...ClassLongW."""
    from mhwmm import gui

    fake = _classes(monkeypatch, "keep")
    gui.class_redraw(_ClassTop())
    assert fake.calls == [] and gui._CLASS_REDRAW == {}

    fake = _classes(monkeypatch, "child", child="TkChild86", top="TkTopLevel86")  # (names read, not assumed)
    gui.class_redraw(_ClassTop())
    assert fake.styles == {"TkChild86": 0x0008, "TkTopLevel86": 0x0003}
    assert gui._CLASS_REDRAW == {"child": {"class": "TkChild86", "before": 0x000B, "after": 0x0008}}
    gui.class_redraw(_ClassTop())  # (once)
    assert [c for c in fake.calls if c[0] == "set"] == [("set", "TkChild86", 0x0008)]

    fake = _classes(monkeypatch, "top")
    top = _ClassTop(shown=False)
    gui.class_redraw(top)  # no outer window yet: nothing, and tried again later
    assert fake.styles["TkTopLevel"] == 0x0003 and gui._CLASS_REDRAW == {}
    top.shown = True
    gui.class_redraw(top)
    assert fake.styles == {"TkChild": 0x000B, "TkTopLevel": 0}
    assert gui._CLASS_REDRAW == {"top": {"class": "TkTopLevel", "before": 3, "after": 0}}

    for wide in (True, False):
        fake = _classes(monkeypatch, "both", wide=wide)
        gui.class_redraw(_ClassTop())
        assert fake.styles == {"TkChild": 0x0008, "TkTopLevel": 0}
        assert set(gui._CLASS_REDRAW) == {"top", "child"}


def test_class_redraw_leaves_what_is_not_tks_own_and_never_raises(monkeypatch):
    """RESIZE_CLASS_REDRAW: a parent of another process, or of the same class as the toplevel's own window, is not
    its outer window; a style that does not change and a failing call are noted (not tried again) and never
    raise."""
    import os

    from mhwmm import gui

    fake = _classes(monkeypatch, "both")
    fake.windows[fake.OUTER][2] = os.getpid() + 1
    gui.class_redraw(_ClassTop())
    assert fake.styles["TkTopLevel"] == 0x0003 and set(gui._CLASS_REDRAW) == {"child"}

    fake = _classes(monkeypatch, "both")
    fake.windows[fake.OUTER][0] = "TkChild"
    gui.class_redraw(_ClassTop())
    assert set(gui._CLASS_REDRAW) == {"child"}
    assert [c for c in fake.calls if c[0] == "set"] == [("set", "TkChild", 0x0008)]

    fake = _classes(monkeypatch, "both")
    fake.stuck = True
    gui.class_redraw(_ClassTop())
    assert all("still" in gui._CLASS_REDRAW[kind]["error"] for kind in ("top", "child"))
    calls = len(fake.calls)
    gui.class_redraw(_ClassTop())
    assert len(fake.calls) == calls  # (not tried again)

    fake = _classes(monkeypatch, "child")

    def broken(*args):
        raise OSError("no class")

    monkeypatch.setattr(fake, "GetClassNameW", broken)
    monkeypatch.setattr(gui, "_CLASSES", [gui._WinClasses()])
    gui.class_redraw(_ClassTop())
    assert "no class" in gui._CLASS_REDRAW["child"]["error"] and fake.styles["TkChild"] == 0x000B

    monkeypatch.setattr(gui, "_CLASSES", [None])  # (not Windows, or user32 not usable)
    monkeypatch.setattr(gui, "_CLASS_REDRAW", {})
    gui.class_redraw(_ClassTop())
    assert gui._CLASS_REDRAW == {}


def test_class_redraw_runs_when_the_window_is_watched_and_shown(root, monkeypatch):
    """RESIZE_CLASS_REDRAW is applied by the windows that watch their size (FreezeContent): the child class when
    the toplevel is watched, the outer window's class at a <Configure> once it has been shown; then not again."""
    from mhwmm import gui

    class Classes:
        def __init__(self):
            self.shown, self.dropped = False, []

        def windows(self, top):
            assert top is root
            found = {"child": ("TkChild", 1)}
            if self.shown:
                found["top"] = ("TkTopLevel", 2)
            return found

        def drop_redraw(self, hwnd):
            self.dropped.append(hwnd)
            return 3, 0

    classes = Classes()
    monkeypatch.setattr(gui, "_CLASSES", [classes])
    monkeypatch.setattr(gui, "_CLASS_REDRAW", {})
    monkeypatch.setattr(gui, "RESIZE_CLASS_REDRAW", "both")
    root.geometry("400x300")
    gui.FreezeContent(root, tk.Frame(root))
    assert classes.dropped == [1]
    classes.shown = True
    root.geometry("420x300")
    pump(root, lambda: classes.dropped == [1, 2])
    root.geometry("440x300")
    _quiet(root, 0.3)
    assert classes.dropped == [1, 2]
    assert set(gui._CLASS_REDRAW) == {"top", "child"}


@pytest.mark.skipif(sys.platform == "win32", reason="Windows changes the real classes")
def test_class_redraw_is_nothing_off_windows(root, monkeypatch):
    """Off Windows RESIZE_CLASS_REDRAW does nothing (there are no window classes)."""
    from mhwmm import gui

    monkeypatch.setattr(gui, "_CLASSES", [])
    monkeypatch.setattr(gui, "_CLASS_REDRAW", {})
    monkeypatch.setattr(gui, "RESIZE_CLASS_REDRAW", "both")
    root.geometry("400x300")
    gui.FreezeContent(root, tk.Frame(root))
    root.geometry("420x300")
    _quiet(root, 0.3)
    assert gui._CLASSES == [None] and gui._CLASS_REDRAW == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Tk's window classes are Windows'")
def test_windows_tk_window_classes_are_found(root):
    """On Windows the toplevel's own window has Tk's child class and its outer window Tk's toplevel class, read
    with GetClassNameW, and their styles can be read (nothing is changed here; the app's windows in the tests
    before may have taken CS_HREDRAW|CS_VREDRAW off, RESIZE_CLASS_REDRAW)."""
    import ctypes

    from mhwmm import gui

    root.geometry("320x200+60+60")
    root.update()
    classes = gui._WinClasses()
    found = classes.windows(root)
    assert found["child"][0] == "TkChild" and found["child"][1] == root.winfo_id()
    assert found["top"][0] == "TkTopLevel" and found["top"][1] == int(root.wm_frame(), 16)
    for _name, hwnd in found.values():
        ctypes.set_last_error(0)
        style = classes.get_style(hwnd, classes.GCL_STYLE)
        assert style or not ctypes.get_last_error()  # (read: a style of 0 only without an error)
        assert not style & ~0xFFFF  # (class styles are CS_* bits)


def test_windows_snaps_soon_after_a_change_are_followed(root, monkeypatch):
    """Windows (RESIZE_FIRST_STEP): with the mouse button up (and no move or size loop) a change soon after a
    change on its own or after a drag (Win+Left and then Win+Up, a maximize just after a drag) is followed at
    once: RESIZE_FIRST_MS is for a drag the mouse does not show. Right after the last change (RESIZE_SETTLE_MS) it
    is still a step; with the button down the time rules hold as without the mouse (a pause in the drag)."""
    from mhwmm import gui

    pointer = _FakePointer()
    monkeypatch.setattr(gui, "_pointer", lambda: pointer)
    state = ["normal"]
    root.wm_state = lambda *a: state[0]
    content, freeze, inside = _content(root, monkeypatch, settle_ms=200)
    monkeypatch.setattr(gui, "RESIZE_FIRST_MS", 1500)  # (a slow machine must not end the time rule too soon)
    laid_out = _configures(inside)

    def change(size, wait=0.3, **setup):
        """The window changed to ``size`` ``wait`` seconds after the change before: frozen?"""
        _quiet(root, wait)
        state[0] = setup.pop("state", "normal")
        pointer.set(**setup)
        laid_out.clear()
        root.geometry("%dx%d" % size)
        root.update()
        assert _size(root) == size
        if not freeze.frozen:
            assert laid_out == [size] and _size(inside) == size
        return freeze.frozen

    assert not change((700, 500), wait=2.0)  # Win+Left (on its own)
    assert not change((700, 260))  # Win+Up: a quarter of the screen (the state stays "normal")
    assert not change((1200, 800), state="zoomed", spans=True)  # maximized
    assert not change((700, 260))  # restored
    for width in (720, 740, 760):  # a drag of the edge ...
        assert change((width, 260), wait=0.05, down=True, border=True)
    pump(root, lambda: not freeze.frozen)  # (it stops, the button let go)
    assert not change((1200, 800), wait=0.05, state="zoomed", spans=True)  # ... and a maximize just after it
    assert not change((760, 260))  # restored
    assert change((780, 260), wait=0.01)  # (right after the last change: a step whatever the mouse says)
    pump(root, lambda: not freeze.frozen)
    assert not change((800, 300), wait=0.05)  # a snap with the keys just after the content followed
    # the button down soon after a change on its own: the time rule (here the free edge of a snapped window with
    # the cursor ahead of it: neither on the border nor resized in place)
    assert change((820, 300), down=True, spans=True)
    pump(root, lambda: not freeze.frozen)
    assert not change((840, 300), wait=2.0, down=True, spans=True)  # (long after: on its own)


def test_follow_width_gives_the_cards_the_canvas_width(root):
    """Cards in a scrolled canvas are as wide as the canvas, at once; a change of height alone leaves them."""
    from tkinter import ttk

    from mhwmm import gui

    root.geometry("600x400")
    canvas = tk.Canvas(root, width=1, height=1)
    canvas.pack(side="left", fill="both", expand=True)
    inner = ttk.Frame(canvas, height=50)
    item = canvas.create_window((0, 0), window=inner, anchor="nw")
    gui.follow_width(canvas, item)
    laid_out = _configures(inner)

    def given():
        return int(float(canvas.itemcget(item, "width")))

    pump(root, lambda: canvas.winfo_width() > 500 and given() == canvas.winfo_width() == inner.winfo_width())
    for geometry in ("640x400", "700x400", "620x400"):
        root.geometry(geometry)
        root.update()
        assert given() == canvas.winfo_width() == inner.winfo_width()
    laid_out.clear()
    root.geometry("620x500")  # taller only: the cards stay as they are
    root.update()
    assert canvas.winfo_height() > 450 and laid_out == []
    side = ttk.Frame(root, width=60)  # the canvas narrower while the window keeps its size
    side.pack(side="right", fill="y", before=canvas)
    root.update()
    assert given() == canvas.winfo_width() == inner.winfo_width() < 620 - 50


def test_gui_content_waits_for_the_drag_to_stop(root, manager, tmp_path, monkeypatch):
    """Nothing in the main window is laid out again while its edge is dragged (the whole content keeps its size);
    once the drag stops the content fills the window, laid out once, and the 옵션 cards, the 원인 찾기 bar's text
    and the log follow the new size."""
    import logging

    from mhwmm import gui

    monkeypatch.setattr(gui, "RESIZE_SETTLE_MS", 400)
    monkeypatch.setattr(gui, "_pointer", lambda: None)  # (the drag's first step is one on its own)
    with manager.prepare_install(make_zip(tmp_path / "Pack.zip", {
            "Pack/One/natives/STM/a.tex": "1", "Pack/Two/natives/STM/a.tex": "2"})) as s:
        mod = manager.install_package(s, s.variants)
    manager.settings.auto_apply = False
    manager.settings.remember_window = False
    app = gui.App(root, manager, queue.Queue())
    px = app.theme.px
    root.maxsize(px(4000), px(3000))  # (Windows keeps a window within the screen unless told otherwise)
    root.geometry(f"{px(1240)}x{px(800)}")
    app.show_options(mod.id)
    canvas, inner = app.options_canvas, app.options_inner
    pump(root, lambda: canvas.winfo_ismapped() and inner.winfo_width() == canvas.winfo_width())
    app.bisect_title.configure(text="원인 찾기 · 3번째 실행")
    app.bisect_text.configure(text="모드 14개를 끄고 게임을 실행합니다. " * 12)
    app.bisect_bar.pack(fill="x", padx=px(20), pady=(px(10), 0), before=app.paned)
    text = app.bisect_text.master

    def wrapped():
        return int(float(str(app.bisect_text.cget("wraplength")))) == max(px(200), text.winfo_width() - px(4))

    def settled():
        return (app.content.winfo_width(), app.content.winfo_height()) == (root.winfo_width(), root.winfo_height()) \
            and inner.winfo_width() == canvas.winfo_width() and wrapped()

    pump(root, wrapped)
    _quiet(root)  # (the sash placed at startup, and the drag's first step not part of an earlier one)

    width, height = root.winfo_width(), root.winfo_height()
    assert width == px(1240) and app.content.winfo_width() == width
    seen = {name: _configures(widget) for name, widget in (
        ("panes", app.paned), ("list", app.tree), ("cards", inner), ("log", app.log_text), ("bar", text),
        ("toolbar", app.toolbar), ("status", app.status_frame))}
    root.geometry(f"{width + px(40)}x{height}")  # the drag's first step: at once
    root.update()
    assert app.content.winfo_width() == width + px(40) and not app.freeze.frozen
    assert inner.winfo_width() == canvas.winfo_width() and wrapped()
    for widget in seen.values():
        widget.clear()
    for step, taller in ((80, 0), (120, 30), (160, 60), (120, 30), (60, 0)):  # dragged on, then back a little
        root.geometry(f"{width + px(step)}x{height + px(taller)}")
        root.update()
        assert app.freeze.frozen and app.content.winfo_width() == width + px(40)
        assert app.content.winfo_height() == height and app.content.winfo_x() == 0
    assert root.winfo_width() == width + px(60)
    assert all(not events for events in seen.values())  # nothing in the window was laid out on these steps
    pump(root, settled)
    assert len(seen["panes"]) == 1 and len(seen["toolbar"]) == 1  # laid out once, for the size it stopped at
    assert not app.freeze.frozen and app.content.winfo_x() == 0
    for step in (40, 80, 120):  # ... and narrower
        root.geometry(f"{width + px(60) - px(step)}x{height}")
        root.update()
    pump(root, lambda: root.winfo_width() == width + px(60) - px(120) and settled())

    # the log keeps its last lines (all of them are in manager.log)
    for i in range(gui.LOG_LINES + 40):
        app._append_log(logging.INFO, f"줄 {i}")
    root.update()
    lines = app.log_text.get("1.0", "end-1c").splitlines()
    assert len(lines) == gui.LOG_LINES
    assert lines[0].endswith("  줄 40") and lines[-1].endswith(f"  줄 {gui.LOG_LINES + 39}")
    assert app.log_text.yview()[1] == 1.0  # still at the end


def test_gui_sash_after_a_drag_is_where_the_weights_put_it(root, manager, tmp_path, monkeypatch):
    """While the window's edge is dragged the mod list and the details card keep their widths (the content is
    frozen); once the drag stops the sash is where ttk's pane weights put it, the same as when every step of the
    drag is laid out."""
    from mhwmm import gui

    for n in range(3):
        with manager.prepare_install(make_zip(tmp_path / f"M{n}.zip", {f"natives/STM/m{n}.tex": "x"})) as s:
            manager.install_variant(s, s.variants[0])
    manager.settings.auto_apply = False
    manager.settings.remember_window = True
    manager.settings.window_geometry = ""
    manager.settings.sash_ratio = 0.8  # a wide list, a narrow details card
    app = gui.App(root, manager, queue.Queue())
    px = app.theme.px
    root.maxsize(px(4000), px(3000))  # (Windows keeps a window within the screen unless told otherwise)
    paned = app.paned
    left, right = (paned.nametowidget(str(p)) for p in paned.panes())
    height = px(800)
    start = px(1240)
    pump(root, lambda: root.winfo_width() > 1)
    narrowest = root.minsize()[0]
    sizes = [start + px(20) * k for k in range(1, 25)]  # wider ...
    sizes += list(range(sizes[-1] - px(30), narrowest, -px(30))) + [narrowest]  # ... as narrow as it goes ...
    sizes += [narrowest + px(25) * k for k in range(1, 9)]  # ... and a little wider again

    def drag(settle_ms):
        monkeypatch.setattr(gui, "RESIZE_SETTLE_MS", settle_ms)
        monkeypatch.setattr(gui, "RESIZE_FIRST_MS", settle_ms)
        root.geometry(f"{start}x{height}")  # (may be a step of the drag before: then settled within 2 × settle_ms)
        _quiet(root, 1.5)
        pump(root, lambda: root.winfo_width() == start == app.content.winfo_width())
        app._place_sash()  # (the saved ratio: the same sash both times)
        root.update()
        steps = []
        for width in sizes:
            root.geometry(f"{width}x{height}")
            root.update()
            assert root.winfo_width() == width
            steps.append((left.winfo_width(), right.winfo_width()))
        pump(root, lambda: not app.freeze.frozen and app.content.winfo_width() == root.winfo_width() == sizes[-1])
        root.update()
        return steps, paned.sashpos(0)

    free, free_sash = drag(0)  # (0: no change is a drag step: every step laid out)
    held, held_sash = drag(600)
    assert held_sash == free_sash  # where ttk's weights put the sash
    assert left.winfo_width() == free_sash
    assert all(a != b for (a, _), (b, _) in zip(free[:24], free[1:24]))  # every step laid out
    assert all(step == held[0] for step in held)  # frozen from the drag's second step on
    assert held[0] == free[0]  # (the first step laid out at once)


@pytest.mark.parametrize("mouse", [None, "border", "ahead"], ids=["time", "mouse", "ahead"])
def test_gui_a_pause_in_the_drag_lays_the_window_out_once(root, manager, monkeypatch, mouse):
    """A pause in the drag (the mouse button still down) lays the window out once, for the size it paused at; the
    step that goes on with the drag is frozen again (laid out at once, the whole window would be laid out a second
    time for the pause, just as the mouse moves again). A change long after the pause is one on its own. By the
    time rules (``mouse`` None), and on Windows by the mouse (RESIZE_FIRST_STEP): the button down on the border
    ("border"), frozen from the drag's first step on; the button down with the cursor ahead of a snapped window's
    free edge ("ahead": neither on the border nor resized in place), by the time rules as without the mouse; a
    change with the button up is on its own."""
    from mhwmm import gui

    monkeypatch.setattr(gui, "RESIZE_SETTLE_MS", 300)  # (a slow machine must not make the drag look finished)
    monkeypatch.setattr(gui, "RESIZE_FIRST_MS", 600)
    pointer = {None: None, "border": _FakePointer(down=True, border=True),
               "ahead": _FakePointer(down=True, spans=True)}[mouse]
    monkeypatch.setattr(gui, "_pointer", lambda: pointer)  # (without it, on Windows the mouse would say "up")
    manager.settings.auto_apply = False
    manager.settings.remember_window = False
    scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
    # (before the window gets its size: Windows keeps a window within the screen unless told otherwise, and the
    # CI's screen is narrower than the window's minimum width, so every step of the drag would be refused)
    root.maxsize(int(round(4000 * scale)), int(round(3000 * scale)))
    app = gui.App(root, manager, queue.Queue())
    px = app.theme.px
    pump(root, lambda: root.winfo_width() > 1 and app.content.winfo_width() == root.winfo_width())
    _quiet(root, 1.2)
    width, height = root.winfo_width(), root.winfo_height()
    panes = _configures(app.paned)
    steps = [0]

    def step():
        steps[0] += 1
        root.geometry(f"{width + px(4) * steps[0]}x{height}")
        root.update()

    def drag(seconds):  # a step every 50 ms, each one frozen
        end = time.perf_counter() + seconds
        while True:
            step()
            assert app.freeze.frozen and gui.resizing(root)
            if time.perf_counter() >= end:
                return
            time.sleep(0.05)

    def laid_out():
        return not app.freeze.frozen and app.paned.winfo_width() == root.winfo_width() - 2 * px(20)

    root.event_generate("<Configure>", width=width, height=height)  # (moved: noted in its state now, not X11's
    # "iconic" of the window's first Configure)
    step()  # the drag's first step: at once (by the mouse on the border: frozen too)
    on_border = mouse == "border"
    assert app.freeze.frozen == on_border and len(panes) == (0 if on_border else 1)
    first = len(panes)
    time.sleep(0.05)
    drag(0.9)  # (longer than RESIZE_FIRST_MS: the pause does not come soon after the first step)
    assert len(panes) == first
    pump(root, laid_out)  # the pause: laid out once
    assert len(panes) == first + 1 and panes[-1][0] == app.paned.winfo_width()
    panes.clear()
    drag(0.3)  # the drag goes on: frozen from its first step
    assert panes == []
    pump(root, laid_out)  # and stops: laid out once more
    assert len(panes) == 1
    _quiet(root, 1.2)  # (longer than RESIZE_SETTLE_MS + RESIZE_FIRST_MS)
    panes.clear()
    if pointer is not None:
        pointer.set()  # (the button let go)
    step()  # a change on its own (maximize, restore): at once
    assert not app.freeze.frozen and len(panes) == 1 and app.content.winfo_width() == root.winfo_width()


def test_gui_mod_list_columns_fit_the_list(root, manager, tmp_path, monkeypatch):
    """The mod list's columns fit the list (no column cut off at its right edge while the name column can give
    way), at the start and after a frozen drag wider and back: ttk keeps a change of the list's width as slack
    until the list is wider or narrower than its columns, so the columns kept their widths from the start."""
    from mhwmm import gui

    monkeypatch.setattr(gui, "RESIZE_SETTLE_MS", 400)
    monkeypatch.setattr(gui, "RESIZE_FIRST_MS", 600)
    for n in range(40):  # (the list's scroll bar shows)
        with manager.prepare_install(make_zip(tmp_path / f"M{n}.zip", {f"natives/STM/m{n}.tex": "x"})) as s:
            manager.install_variant(s, s.variants[0])
    manager.settings.auto_apply = False
    manager.settings.remember_window = False
    scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
    # (before the window gets its size: Windows keeps a window within the screen unless told otherwise, and the
    # CI's screen is narrower than the window)
    root.maxsize(int(round(4000 * scale)), int(round(3000 * scale)))
    app = gui.App(root, manager, queue.Queue())
    px = app.theme.px
    tree = app.tree
    columns = ("#0",) + tuple(tree["columns"])

    def fits():
        root.update()
        total = sum(int(tree.column(c, "width")) for c in columns)
        return tree.xview() == (0.0, 1.0) and total == tree.winfo_width()

    pump(root, lambda: root.winfo_width() > 1 and app.paned.winfo_width() > 1)
    _quiet(root, 1.0)  # (the sash and the columns placed 60 ms after the start)
    assert fits()
    name = int(tree.column("name", "width"))
    width, height = root.winfo_width(), root.winfo_height()
    end = max(width - px(120), root.minsize()[0])  # (narrower than at the start, but not below the window's minimum)
    assert end < width, (width, root.minsize())
    path = [width + px(10) * k for k in range(1, 33)]
    path += list(range(path[-1] - px(10), end, -px(10))) + [end]
    for w in path:  # one drag, wider and back (frozen from its second step)
        root.geometry(f"{w}x{height}")
        root.update()
    assert app.freeze.frozen
    try:
        pump(root, lambda: not app.freeze.frozen and app.content.winfo_width() == root.winfo_width() == end)
    except AssertionError:
        raise AssertionError(f"frozen {app.freeze.frozen}, content {app.content.winfo_width()}, window "
                             f"{root.winfo_width()}, wanted {end} (min {root.minsize()}, start {width})") from None
    assert fits() and int(tree.column("name", "width")) < name  # (the name column gave the width back)


def test_gui_window_size_and_sash_are_restored(root, manager):
    """The main window's content is placed (it asks the window for no size): the window gets the saved size, or
    1240×800, its minimum size keeps the toolbar whole, and the sash goes to the saved share, as before."""
    from mhwmm import gui

    scale = max(1.0, root.winfo_fpixels("1i") / 96.0)

    def px(n):
        return int(round(n * scale))

    root.maxsize(px(4000), px(3000))  # (Windows keeps a window within the screen unless told otherwise)
    manager.settings.auto_apply = False
    manager.settings.remember_window = True
    manager.settings.window_geometry = f"{px(1300)}x{px(850)}+10+20"
    manager.settings.sash_ratio = 0.45
    app = gui.App(root, manager, queue.Queue())
    assert app.theme.px(1300) == px(1300)
    pump(root, lambda: root.winfo_width() > 1 and app.paned.winfo_width() > 1)
    _quiet(root, 0.4)
    app._place_sash()  # (as 60 ms after the start: then the window may not be laid out yet on every system)
    root.update()
    assert root.minsize()[0] >= px(980) and root.minsize()[1] == px(620)  # (the toolbar as it is laid out first)
    assert (root.winfo_width(), root.winfo_height()) == (px(1300), px(850))
    assert (app.content.winfo_width(), app.content.winfo_height()) == (px(1300), px(850))
    assert app.content.winfo_manager() == "place" and not app.freeze.frozen
    assert app.paned.sashpos(0) == int(app.paned.winfo_width() * 0.45)
    app._save_window_state()
    assert manager.settings.window_geometry.startswith(f"{px(1300)}x{px(850)}+")
    assert manager.settings.sash_ratio == round(app.paned.sashpos(0) / app.paned.winfo_width(), 3)
    assert abs(manager.settings.sash_ratio - 0.45) < 0.005


def test_gui_window_takes_its_minimum_and_default_size(root, manager):
    from mhwmm import gui

    manager.settings.auto_apply = False
    manager.settings.remember_window = True
    manager.settings.window_geometry = "300x200+10+20"  # smaller than the window may be
    app = gui.App(root, manager, queue.Queue())
    px = app.theme.px
    pump(root, lambda: root.winfo_width() > 1 and app.content.winfo_width() == root.winfo_width())
    assert root.minsize()[0] >= px(980) and root.minsize()[1] == px(620)
    assert (root.winfo_width(), root.winfo_height()) == root.minsize()
    assert app.content.winfo_height() == root.winfo_height()


def test_gui_toolbar_mod_tool_button(root, manager, tmp_path, monkeypatch):
    """모드 수정 sits right after 장비 외형 on the toolbar; it works on any mod but REFramework (a script mod too:
    the window says which tools work on it); with no such mod it says so and opens no window."""
    from test_modtool_gui import patch_window

    from mhwmm import gui, refw

    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: True)
    button = app.buttons["modtool"]
    slaves = app.toolbar.pack_slaves()
    assert button.cget("text") == gui.MOD_TOOL_TITLE == "모드 수정"
    assert slaves.index(button) == slaves.index(app.buttons["equipment"]) + 1
    assert str(button.cget("style")) == str(app.buttons["equipment"].cget("style"))
    opened, told = [], []

    class Dialog:
        def __init__(self, parent, app, mod_id, page=None, source=None, pending=None):
            opened.append((mod_id, page))

        def show(self):
            return None

    patch_window(monkeypatch, Dialog)
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, text, parent=None: told.append((title, text)))
    button.invoke()  # (no mod at all)
    assert opened == [] and told == [("모드 수정", "고칠 수 있는 모드가 없습니다. 모드를 추가하면 고칠 수 있습니다.")]

    with manager.prepare_install(make_zip(tmp_path / "R.zip", {"dinput8.dll": "dll"})) as s:
        framework = manager.install_variant(s, s.variants[0])
    framework.origin = refw.ORIGIN  # (REFramework itself: nothing to change)
    app.refresh()
    app.tree.selection_set(framework.id)
    button.invoke()
    assert opened == [] and len(told) == 2

    with manager.prepare_install(make_zip(tmp_path / "S.zip", {"reframework/autorun/a.lua": "--"})) as s:
        script = manager.install_variant(s, s.variants[0])
    app.refresh()
    app.tree.selection_set(script.id)
    button.invoke()
    assert opened == [(script.id, None)] and len(told) == 2


def test_gui_window_default_size_and_sash(root, manager):
    from mhwmm import gui

    root.maxsize(4000, 3000)  # (Windows keeps a window within the screen unless told otherwise)
    manager.settings.auto_apply = False
    manager.settings.remember_window = False
    app = gui.App(root, manager, queue.Queue())
    px = app.theme.px
    pump(root, lambda: root.winfo_width() > 1 and app.paned.winfo_width() > 1)
    _quiet(root, 0.4)
    app._place_sash()  # (as 60 ms after the start)
    root.update()
    assert (root.winfo_width(), root.winfo_height()) == (px(1240), px(800))
    assert app.paned.sashpos(0) == int(app.paned.winfo_width() * 0.58)


def test_resizable_dialogs_keep_their_size_and_freeze(root, manager, monkeypatch):
    """사용법 and 충돌 한눈에 보기 (windows meant to be made bigger) are as big as their packed content asks for,
    with the body placed instead: it keeps its size while the window's edge is dragged and follows once the drag
    stops."""
    from mhwmm import gui

    manager.settings.auto_apply = False
    app = gui.App(root, manager, queue.Queue())
    monkeypatch.setattr(gui, "RESIZE_SETTLE_MS", 600)
    monkeypatch.setattr(gui, "RESIZE_FIRST_MS", 600)
    for cls, make in ((gui.HelpDialog, lambda: gui.HelpDialog(root, app.theme)),
                      (gui.ConflictsDialog, lambda: gui.ConflictsDialog(root, app.theme, manager))):
        shown = {}
        for freeze in (False, True):
            monkeypatch.setattr(cls, "FREEZE", freeze)
            dialog = make()
            dialog.maxsize(4000, 3000)
            dialog.wait_window = lambda *a: None  # (show() returns at once)
            dialog.show()
            pump(root, lambda: dialog.winfo_ismapped() and dialog.body.winfo_width() > 1)
            root.update()
            assert dialog.body.winfo_manager() == ("place" if freeze else "pack")
            shown[freeze] = (dialog.winfo_width(), dialog.winfo_height(), dialog.body.winfo_width(),
                             dialog.body.winfo_height(), dialog.winfo_rootx(), dialog.winfo_rooty())
            if freeze:
                _quiet(root)
                width, height = dialog.winfo_width(), dialog.winfo_height()
                for k in (1, 2, 3, 4):
                    dialog.geometry(f"{width + 20 * k}x{height + 10 * k}")
                    dialog.update()
                assert dialog.freeze.frozen and dialog.body.winfo_width() == width + 20
                pump(root, lambda: (dialog.body.winfo_width(), dialog.body.winfo_height()) == (width + 80, height + 40))
            dialog.destroy()
        assert shown[True] == shown[False]


def test_gui_keeps_the_picked_tab_and_reads_old_option_pictures(root, manager, tmp_path, monkeypatch):
    from tkinter import ttk

    from mhwmm import fsutil, gui

    def picture(name, color):
        img = tk.PhotoImage(master=root, width=32, height=18)
        img.put(color, to=(0, 0, 32, 18))
        img.write(str(tmp_path / name), format="png")
        return (tmp_path / name).read_bytes()

    archive = make_zip(tmp_path / "dl" / "Pack.zip", {
        "Pack/One/natives/STM/a.tex": "1", "Pack/One/preview.png": picture("1.png", "#c03030"),
        "Pack/Two/natives/STM/a.tex": "2", "Pack/Two/preview.png": picture("2.png", "#3030c0"),
    })
    with manager.prepare_install(archive) as s:
        pack = manager.install_package(s, s.variants)
    with manager.prepare_install(make_zip(tmp_path / "Plain.zip", {"Plain/natives/STM/p.tex": "p"})) as s:
        plain = manager.install_package(s, s.variants)
    for opt in pack.options:  # installed by a version before 1.12.0
        opt.pop("preview", None)
    fsutil.rmtree(manager.mod_dir(pack.id) / "previews")
    pack.pictures_read = False
    manager.settings.auto_apply = False
    app = gui.App(root, manager, queue.Queue())
    nb = app.notebook
    app.tree.selection_set(pack.id)
    app._update_details()
    pump(root, lambda: nb.winfo_ismapped() and nb.winfo_width() > 100)

    def click_tab(tab):
        for y in range(2, 60, 3):
            for x in range(2, nb.winfo_width(), 3):
                index = nb.tk.call(nb._w, "identify", "tab", x, y)
                if index != "" and nb.tabs()[int(index)] == str(tab):
                    nb.event_generate("<ButtonPress-1>", x=x, y=y)
                    nb.event_generate("<ButtonRelease-1>", x=x, y=y)
                    root.update()
                    return
        raise AssertionError("탭을 찾지 못했습니다")

    # 옵션 picked, a mod without options shows another tab, the next mod with options shows 옵션 again
    click_tab(app.options_tab)
    assert nb.select() == str(app.options_tab)
    app.tree.selection_set(plain.id)
    app._update_details()
    assert nb.select() != str(app.options_tab)
    app.tree.selection_set(pack.id)
    app._update_details()
    assert nb.select() == str(app.options_tab)
    # a tab picked on purpose stays
    click_tab(app.conflicts_tab)
    app.tree.selection_set(plain.id)
    app._update_details()
    app.tree.selection_set(pack.id)
    app._update_details()
    assert nb.select() == str(app.conflicts_tab)
    # ... also one picked with the keyboard
    root.update()  # (the selection events of the clicks above)
    nb.tk.call("ttk::notebook::CycleTab", nb._w, -1)  # what the notebook's <Left> key does
    root.update()
    assert nb.select() != str(app.conflicts_tab)
    picked = nb.select()
    app._update_details()
    root.update()
    assert nb.select() == picked

    # the old mod's pictures come from its archive (still where it was installed from)
    app.show_options(pack.id)

    def widgets(parent):
        for w in parent.winfo_children():
            yield w
            yield from widgets(w)

    button = next(w for w in widgets(app.options_inner)
                  if isinstance(w, ttk.Button) and str(w.cget("text")) == "압축 파일에서 옵션 그림 가져오기")
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: pytest.fail("the archive was found"))
    button.invoke()
    pump(root, lambda: app.busy is None and len(app._option_images) == 2)
    assert manager.get(pack.id).pictures_read
    assert not any(isinstance(w, ttk.Button) and str(w.cget("text")) == "압축 파일에서 옵션 그림 가져오기"
                   for w in widgets(app.options_inner))
    assert "옵션 그림 2개" in app.status_var.get()

    # the menu command does every such mod and tells which archives it could not find
    shown = []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, text, **k: shown.append(text))
    with manager.prepare_install(make_zip(tmp_path / "dl" / "Gone.zip", {
            "Gone/A/natives/STM/g.tex": "a", "Gone/A/preview.png": picture("3.png", "#30c030"),
            "Gone/B/natives/STM/g.tex": "b"})) as s:
        gone = manager.install_package(s, s.variants)
    (tmp_path / "dl" / "Gone.zip").unlink()
    for mod in (manager.get(pack.id), gone):
        for opt in mod.options:
            opt.pop("preview", None)
        mod.pictures_read = False
    app.read_all_option_pictures()
    pump(root, lambda: app.busy is None and shown)
    assert shown[-1] == "· Pack: 그림 2개\n\n압축 파일을 찾지 못한 모드 (옵션 탭에서 직접 골라 주세요):\n· Gone"
    assert manager.get(pack.id).pictures_read and not manager.get(gone.id).pictures_read
    shown.clear()
    app.read_all_option_pictures()
    pump(root, lambda: app.busy is None and shown)
    assert shown[-1].startswith("설치할 때 쓴 압축 파일을 찾지 못했습니다") and shown[-1].endswith("· Gone")
    app.read_all_option_pictures = lambda: pytest.fail("not again")
    # ... which the user then picks
    elsewhere = make_zip(tmp_path / "moved" / "Gone.zip", {
        "Gone/A/natives/STM/g.tex": "a", "Gone/A/preview.png": picture("3.png", "#30c030"),
        "Gone/B/natives/STM/g.tex": "b"})
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **k: str(elsewhere))
    app.read_option_pictures(gone.id)
    pump(root, lambda: app.busy is None and manager.get(gone.id).pictures_read)
    assert manager.option_preview(gone.id, manager.get(gone.id).options[0]) is not None
    shown.clear()
    del app.read_all_option_pictures
    app.read_all_option_pictures()
    assert shown == ["옵션 그림을 가져올 모드가 없습니다."]


def test_gui_moves_old_data_with_progress(root, tmp_path, monkeypatch):
    from mhwmm import gui, manager as mgr

    from conftest import write

    app_dir = tmp_path / "app"
    write(app_dir / "MHWildsModManager.exe", "exe")
    legacy = tmp_path / "legacy"
    write(legacy / "state.json", "{}")
    write(legacy / "mods" / "m1" / "files" / "a.pak", "p" * 1000)
    monkeypatch.setattr(mgr.sys, "frozen", True, raising=False)
    monkeypatch.setattr(mgr.sys, "executable", str(app_dir / "MHWildsModManager.exe"))
    monkeypatch.setattr(mgr, "user_data_dir", lambda: legacy)
    monkeypatch.delenv("MHWMM_DATA_DIR", raising=False)
    monkeypatch.setattr(mgr, "_rename_dir", lambda s, d: (_ for _ in ()).throw(OSError("x")) if s == legacy
                        else mgr.os.replace(s, d))
    path, problem = gui._prepare_data_dir(root, None)
    assert problem is None and path == app_dir / "MHWildsModManager_data"
    assert (path / "mods" / "m1" / "files" / "a.pak").exists() and not legacy.exists()


def test_gui_profiles_filter_settings_and_launch(root, manager, tmp_path, game_dir, monkeypatch):
    from mhwmm import gui

    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: None)
    with manager.prepare_install(make_zip(tmp_path / "A.zip", {"natives/STM/a.tex": "a"})) as s:
        a = manager.install_variant(s, s.variants[0])
    with manager.prepare_install(make_zip(tmp_path / "B.zip", {"natives/STM/a.tex": "b"})) as s:
        b = manager.install_variant(s, s.variants[0])
    manager.set_enabled(b.id, False)
    app = gui.App(root, manager, queue.Queue())
    app.apply()
    pump(root, lambda: app.busy is None and not manager.dirty and not app._apply_pending)

    # filter
    app.filter_var.set("비활성")
    app.refresh()
    assert list(app.tree.get_children()) == [b.id]
    app.filter_var.set("충돌 있음")
    app.refresh()
    assert list(app.tree.get_children()) == []
    app.filter_var.set(gui.FILTER_ALL)
    app.refresh()

    # a new empty profile turns everything off in the game folder
    monkeypatch.setattr(gui.TextDialog, "show", lambda self: "바닐라")
    app.new_profile(copy_current=False)
    pump(root, lambda: app.busy is None and not manager.dirty and not app._apply_pending)
    assert manager.profile == "바닐라" and not (game_dir / "natives").exists()
    assert "바닐라" in app.profile_button.cget("text")
    app.switch_profile("기본")
    pump(root, lambda: app.busy is None and not manager.dirty and not app._apply_pending)
    assert (game_dir / "natives/STM/a.tex").read_text() == "a"

    # launching with pending changes applies them first
    started = []
    monkeypatch.setattr(app, "_start_game", lambda: started.append(True))
    manager.update_settings(auto_apply=False)
    manager.set_enabled(b.id, True)
    app.launch_game()
    pump(root, lambda: bool(started))
    assert (game_dir / "natives/STM/a.tex").read_text() == "b" and not manager.dirty

    # settings dialog saves the new options
    dialog = gui.SettingsDialog(root, app.theme, manager)
    dialog.vars["confirm_remove"].set(False)
    dialog.vars["minimize_on_launch"].set(True)
    dialog.ok()
    assert not manager.settings.confirm_remove and manager.settings.minimize_on_launch

    # window state is remembered
    app.toggle_log()
    app._save_window_state()
    assert manager.settings.window_geometry and not manager.settings.log_visible


def test_gui_refw_badge_and_install(root, manager, game_dir, monkeypatch):
    import zipfile

    from mhwmm import gui, refw

    from conftest import write

    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: None)
    old = refw.Build("nightly-01420-a", 1420, "a" * 40, refw.DOWNLOAD_PREFIX + "o/REFramework.zip", 0, None, "p")
    new = refw.Build("nightly-01432-b", 1432, "b" * 40, refw.DOWNLOAD_PREFIX + "n/REFramework.zip", 0, None, "p",
                     "2026-10-11")
    write(game_dir / "dinput8.dll", b"MZ" + old.commit.encode())
    app = gui.App(root, manager, queue.Queue())
    status = refw.check(game_dir, [new, old])
    app._on_refw_checked(status, None, False)
    assert "업데이트" in app.refw_badge.cget("text")
    dialog = gui.RefwDialog(root, app.theme, status, managed=False)
    dialog.destroy()

    def fake_download(build, dest, progress=None):
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("dinput8.dll", b"MZ" + build.commit.encode())
        return dest

    monkeypatch.setattr(refw, "download", fake_download)
    app.install_refw(new)
    pump(root, lambda: app.busy is None and not manager.dirty and "최신" in app.refw_badge.cget("text"))
    assert manager.refw_mod().version == "nightly 01432"
    assert (game_dir / "dinput8.dll").read_bytes() == b"MZ" + new.commit.encode()


def test_gui_nexus_updates(root, manager, tmp_path, monkeypatch):
    from mhwmm import gui, nexus

    from test_nexus import files_data

    archive = make_zip(tmp_path / "Better Armor-1234-1-0-1740000000.zip", {"natives/STM/a.tex": "a"})
    with manager.prepare_install(archive) as s:
        mod = manager.install_variant(s, s.variants[0])
    with manager.prepare_install(make_zip(tmp_path / "Other.zip", {"natives/STM/b.tex": "b"})) as s:
        other = manager.install_variant(s, s.variants[0])
    monkeypatch.setattr(nexus.NexusClient, "validate", lambda self: {"name": "hunter", "is_premium": False})
    monkeypatch.setattr(nexus.NexusClient, "files", lambda self, mod_id: files_data())
    app = gui.App(root, manager, queue.Queue())
    assert "Nexus 키 필요" in app.mods_badge.cget("text")  # no key yet: the badge points to the settings

    # the API key is checked and saved in the settings
    dialog = gui.SettingsDialog(root, app.theme, manager)
    dialog.key_var.set(" secret ")
    dialog.validate_key()
    pump(root, lambda: "hunter" in dialog.key_status.cget("text"))
    dialog.ok()
    assert manager.settings.nexus_api_key == "secret"

    app.check_mod_updates(manual=True)
    pump(root, lambda: not app._checking_mods)
    assert mod.update["version"] == "1.2" and manager.settings.last_mod_check
    assert app.filter_var.get() == gui.FILTER_UPDATES  # "show only those?" was answered yes
    assert list(app.tree.get_children()) == [mod.id]
    assert app.tree.item(mod.id, "values")[3] == "1.0 → 1.2" and "update" in app.tree.item(mod.id, "tags")
    assert "업데이트 1개" in app.count_label.cget("text")
    app.tree.selection_set(mod.id)
    app._update_details()
    shown = app.info_text.get("1.0", "end")
    assert "새 버전이 있습니다" in shown and "모드 페이지 #1234" in shown
    opened = []
    monkeypatch.setattr(gui.webbrowser, "open", opened.append)
    app._open_nexus(lambda m: nexus.FILES_URL.format(m.nexus_id))
    assert opened == ["https://www.nexusmods.com/monsterhunterwilds/mods/1234?tab=files"]

    # linking a mod by hand, and removing the link again
    app.filter_var.set(gui.FILTER_ALL)
    app.refresh()
    app.tree.selection_set(other.id)
    monkeypatch.setattr(gui.TextDialog, "show", lambda self: "https://www.nexusmods.com/monsterhunterwilds/mods/77")
    app.link_nexus()
    assert other.nexus_id == 77
    monkeypatch.setattr(gui.TextDialog, "show", lambda self: "")
    app.link_nexus()
    assert other.nexus_id == 0 and other.nexus_unlinked


def test_gui_help_window(root, manager):
    from mhwmm import gui, helptext
    from mhwmm.theme import Theme

    theme = Theme(root, "dark")
    theme.use("dark")
    dialog = gui.HelpDialog(root, theme, "nexus")
    pump(root, lambda: dialog._active is not None)
    shown = dialog.text.get("1.0", "end")
    for section in helptext.SECTIONS:
        assert section.title in shown
    assert "**" not in shown and "[[" not in shown  # markup is rendered, not shown
    assert dialog._active == [s.key for s in helptext.SECTIONS].index("nexus")
    assert dialog.text.window_names()  # step numbers and keys are drawn as chips
    dialog.go(0)
    assert str(dialog.nav_buttons[0].cget("style")) == "NavActive.TButton"
    assert str(dialog.nav_buttons[1].cget("style")) == "Nav.TButton"
    dialog.destroy()


def test_gui_checks_mod_updates_at_startup(root, manager, tmp_path, monkeypatch):
    from mhwmm import gui, nexus

    from test_nexus import files_data

    archive = make_zip(tmp_path / "Better Armor-1234-1-0-1740000000.zip", {"natives/STM/a.tex": "a"})
    with manager.prepare_install(archive) as s:
        mod = manager.install_variant(s, s.variants[0])
    manager.update_settings(nexus_api_key="secret", last_mod_check=time.time())  # checked a moment ago
    monkeypatch.setattr(nexus.NexusClient, "files", lambda self, mod_id: files_data())
    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: mod.update and not app._checking_mods)
    assert app.mods_badge.cget("text") == "● 모드 업데이트 1개" and app.mods_badge.winfo_manager()
    assert "1.0 → 1.2" in app._mods_tooltip()
    app._on_mods_badge_click()
    assert app.filter_var.get() == gui.FILTER_UPDATES and list(app.tree.get_children()) == [mod.id]

    # nothing new: the badge says so, and a failed check is shown too
    manager.set_update_results({mod.id: {}})
    app.refresh()
    assert app.mods_badge.cget("text") == "● 모드 최신"
    app._on_mods_checked(None, [], nexus.NexusError("Nexus 요청 한도를 넘었습니다."), False)
    assert "실패" in app.mods_badge.cget("text") and "한도" in app._mods_tooltip()
    manager.update_settings(check_mod_updates=False)
    app._mod_check_error = None
    app.refresh()
    assert not app.mods_badge.winfo_manager()


def test_gui_moves_old_chain_paks_to_pak_mods_at_startup(root, manager, tmp_path, game_dir):
    from mhwmm import gui

    from test_deploy import _old_chain_deployment

    with manager.prepare_install(make_zip(tmp_path / "Chen.zip", {"Chen_Tex.pak": "tex"})) as s:
        chen = manager.install_variant(s, s.variants[0])
    source = manager.prepare_deploy()[0][0]
    # an older version put the PAK at patch_017, which the game never reads
    _old_chain_deployment(tmp_path, game_dir, manager.deployer(), [(source, 17)])
    manager.dirty = False
    assert manager.outdated_paks()

    app = gui.App(root, manager, queue.Queue())
    target = game_dir / "pak_mods" / "001_Chen.pak"
    pump(root, lambda: target.exists() and app.busy is None and not manager.dirty)
    assert target.read_text() == "tex" and chen.enabled
    assert not (game_dir / "re_chunk_000.pak.sub_000.pak.patch_017.pak").exists() and not manager.outdated_paks()


def test_exe_keeps_the_default_taskbar_identity(monkeypatch):
    """A pinned taskbar shortcut and the running exe must stay one taskbar button."""
    import ctypes
    import types

    from mhwmm import gui

    calls = []
    fake = types.SimpleNamespace(
        shcore=types.SimpleNamespace(SetProcessDpiAwareness=lambda v: calls.append("dpi")),
        shell32=types.SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=lambda v: calls.append(v)))
    monkeypatch.setattr(ctypes, "windll", fake, raising=False)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    monkeypatch.setattr(gui.sys, "frozen", True, raising=False)
    gui._windows_setup()
    assert calls == ["dpi"]
    calls.clear()
    monkeypatch.setattr(gui.sys, "frozen", False)
    gui._windows_setup()  # from source: its own button instead of python.exe's
    assert calls == ["dpi", "MHWildsModManager"]


def test_gui_conflict_overview_and_highlight(root, manager, tmp_path):
    from mhwmm import gui

    with manager.prepare_install(make_zip(tmp_path / "A.zip", {"natives/STM/x.tex": "a", "natives/STM/y.tex": "a"})) as s:
        a = manager.install_variant(s, s.variants[0])
    with manager.prepare_install(make_zip(tmp_path / "B.zip", {"natives/STM/x.tex": "b", "natives/STM/y.tex": "b"})) as s:
        b = manager.install_variant(s, s.variants[0])
    with manager.prepare_install(make_zip(tmp_path / "C.zip", {"natives/STM/z.tex": "c"})) as s:
        c = manager.install_variant(s, s.variants[0])
    app = gui.App(root, manager, queue.Queue())
    assert app.conflicts_button.cget("text") == "충돌 1쌍 보기 ›" and app.conflicts_button.winfo_manager()
    # A is hidden behind B entirely: a problem, shown with the problems button and a tooltip
    assert "problem" in app.tree.item(a.id, "tags") and app.tree.item(a.id, "values")[1] == "⚠ A"
    assert app.problems_button.cget("text") == "⚠ 문제 1개" and "밀려" in app._row_tooltip(a.id)
    app.show_problems()
    assert list(app.tree.get_children()) == [a.id]
    app.filter_var.set(gui.FILTER_ALL)
    app.refresh()

    # selecting a mod colours the ones it fights with
    app.tree.selection_set(a.id)
    app._highlight_related()
    assert "over_sel" in app.tree.item(b.id, "tags") and app.legend.winfo_manager()
    assert not {"over_sel", "under_sel"} & set(app.tree.item(c.id, "tags"))
    app.tree.selection_set(b.id)
    app._highlight_related()
    assert "under_sel" in app.tree.item(a.id, "tags") and "over_sel" not in app.tree.item(a.id, "tags")
    app.tree.selection_set(c.id)
    app._highlight_related()
    assert not app.legend.winfo_manager()

    dialog = gui.ConflictsDialog(root, app.theme, manager)
    assert [(w, l, len(f)) for w, l, f in dialog.pairs] == [(b.id, a.id, 2)]
    assert dialog.pairs_tree.item("0", "values")[0] == "2. B" and len(dialog.files_tree.get_children()) == 2
    dialog.show_in_list()
    assert dialog.result == a.id  # A is hidden behind B entirely
    assert manager.hidden_mods() == [a]


def test_gui_tells_about_missing_requirements(root, manager, tmp_path, monkeypatch):
    from mhwmm import gui, nexus

    app = gui.App(root, manager, queue.Queue())
    shown = []
    monkeypatch.setattr(gui.RequirementsDialog, "show", lambda self: shown.append(self))
    monkeypatch.setattr(nexus, "fetch_requirements", lambda ids, key="": {3664: [
        {"name": "Lua Bone System", "url": nexus.MOD_URL.format(10), "nexus_id": 10, "notes": "", "external": False}]})
    archive = make_zip(tmp_path / "Chen-3664-1-0-1740000000.zip", {
        "natives/STM/a.mesh": "m", "reframework/autorun/chen.lua": 'require("_CatLib")'})
    app.add_paths([str(archive)])
    pump(root, lambda: bool(shown))
    (mod,) = manager.mods
    assert [r["name"] for r in manager.missing_requirements(mod.id)] == ["Lua Bone System", "_CatLib"]
    dialog_text = " ".join(str(w.cget("text")) for w in shown[0].body.winfo_children() if isinstance(w, gui.ttk.Label))
    assert "Chen" in dialog_text

    app.refresh()
    assert "problem" in app.tree.item(mod.id, "tags") and app.problems_button.cget("text") == "⚠ 문제 1개"
    assert "Lua Bone System" in app._row_tooltip(mod.id)
    app.filter_var.set(gui.FILTER_MISSING)
    app.refresh()
    assert list(app.tree.get_children()) == [mod.id]
    app.tree.selection_set(mod.id)
    app._update_details()
    info = app.info_text.get("1.0", "end")
    assert "필요한 모드가 없습니다" in info and "Lua Bone System" in info and "_CatLib" in info


def test_requirement_notes_of_shaders():
    from mhwmm import gui

    shader = {"kind": "shader", "by": "Endfield Style Shader Pack", "option": "", "notes": ""}
    assert gui.requirement_note({**shader, "state": "ok"}) == "있음 (Endfield Style Shader Pack)"
    assert gui.requirement_note({**shader, "state": "off", "by": "Endfield", "option": "Pack"}) == \
        "설치되어 있지만 옵션이 꺼져 있음 ('Endfield'의 옵션 'Pack')"
    assert gui.requirement_note({**shader, "state": "off"}) == "설치되어 있지만 꺼져 있음 (Endfield Style Shader Pack)"
    assert "셰이더 파일이 게임에도 설치한 모드에도 없음" in gui.requirement_note({**shader, "state": "missing", "by": ""})


def test_gui_tells_about_missing_shaders_after_the_first_check(root, manager, tmp_path, monkeypatch):
    from mhwmm import gui
    from mhwmm.manager import MaterialJob, MaterialRun

    app = gui.App(root, manager, queue.Queue())
    shown = []
    monkeypatch.setattr(gui.RequirementsDialog, "show", lambda self: shown.append(self))
    with manager.prepare_install(make_zip(tmp_path / "Rossi.zip", {"natives/STM/a.mdf2.45": "m"})) as s:
        mod = manager.install_variant(s, s.variants[0])
    manager.set_enabled(mod.id, True)
    item = {"name": "셰이더 EF_Skin_NPR.mmtr", "url": "", "notes": "", "kind": "shader", "state": "missing", "by": "",
            "option": "", "shaders": ["MaterialShader/Variation/EF_Skin_NPR.mmtr"], "materials": ["body"]}
    monkeypatch.setattr(manager, "shader_requirements", lambda mod_id: [dict(item)] if mod_id == mod.id else [])
    monkeypatch.setattr(manager, "finish_materials", lambda run, jobs: [])
    monkeypatch.setattr(app, "_offer_materials", lambda: None)
    run, job = MaterialRun("fp", False, {mod.id: []}), MaterialJob(mod.id, mod.name, f"{mod.installed_at}|{mod.updated_at}", [])
    app._first_check.add(mod.id)  # (installed just now: the material check runs after the install)
    app._on_materials_checked(run, [job], None)
    assert len(shown) == 1 and mod.id not in app._first_check
    text = " ".join(str(w.cget("text")) for row in shown[0].body.winfo_children()
                    for w in [row, *row.winfo_children()] if isinstance(w, gui.ttk.Label))
    assert "Rossi" in text and "셰이더 EF_Skin_NPR.mmtr" in text and "게임에도 설치한 모드에도 없음" in text
    app._on_materials_checked(run, [job], None)  # a later check (game update...): the ⚠ mark tells
    assert len(shown) == 1
    app.refresh()
    assert "problem" in app.tree.item(mod.id, "tags") and "셰이더 EF_Skin_NPR.mmtr" in app._row_tooltip(mod.id)


def test_shader_notices_wait_for_their_turn(root, manager, tmp_path, monkeypatch):
    """Told once the stored check has them: not for a run whose result was thrown away (the mod was
    reinstalled meanwhile), not over another dialog or task, not during 원인 찾기, and not again by the
    install-time notice of requirements."""
    from mhwmm import gui
    from mhwmm.manager import MaterialJob, MaterialRun

    app = gui.App(root, manager, queue.Queue())
    shown = []
    monkeypatch.setattr(gui.RequirementsDialog, "show", lambda self: shown.append(self))
    with manager.prepare_install(make_zip(tmp_path / "Rossi.zip", {"natives/STM/a.mdf2.45": "m"})) as s:
        mod = manager.install_variant(s, s.variants[0])
    manager.set_enabled(mod.id, True)
    item = {"name": "셰이더 EF_Skin_NPR.mmtr", "url": "", "notes": "", "kind": "shader", "state": "off",
            "by": "Endfield", "option": "", "mod_off": True, "shaders": [], "materials": ["body"]}
    monkeypatch.setattr(manager, "shader_requirements", lambda mod_id: [dict(item)] if mod_id == mod.id else [])
    monkeypatch.setattr(manager, "finish_materials", lambda run, jobs: [])
    monkeypatch.setattr(app, "_offer_materials", lambda: None)
    stamp = f"{mod.installed_at}|{mod.updated_at}"
    run = MaterialRun("fp", False, {mod.id: []})
    app._first_check.add(mod.id)
    app._on_materials_checked(run, [MaterialJob(mod.id, mod.name, "an older install", [])], None)
    assert shown == [] and mod.id in app._first_check  # (that result was not stored: told after the next check)
    app._on_materials_checked(MaterialRun("fp", False, errors={mod.id: "x"}), [MaterialJob(mod.id, mod.name, stamp, [])],
                              None)
    assert shown == [] and mod.id in app._first_check  # (nor when the check failed)

    modal = [True]  # another dialog is open: the notice waits for it
    monkeypatch.setattr(app, "_modal_open", lambda: modal[0])
    app._on_materials_checked(run, [MaterialJob(mod.id, mod.name, stamp, [])], None)
    assert shown == [] and mod.id not in app._first_check
    root.after(300, lambda: modal.__setitem__(0, False))
    pump(root, lambda: bool(shown))
    assert len(shown) == 1

    app._bisect = object()  # 원인 찾기 turns mods off on purpose: not told then
    app._notify_shader_requirements([mod.id])
    assert len(shown) == 1
    app._bisect = None

    monkeypatch.setattr(manager, "missing_requirements", lambda mod_id: [dict(item)])
    app._queue_requirement_notices([mod.id])  # (the Nexus requirements arriving after the check)
    assert len(shown) == 1
    assert gui.requirement_note(item) == "설치되어 있지만 꺼져 있음 (Endfield)"
    assert gui.requirement_note({**item, "option": "Pack"}) == \
        "설치되어 있지만 꺼져 있고 옵션도 꺼져 있음 ('Endfield'을(를) 켜고 옵션 'Pack'을(를) 켜세요)"


def test_shader_notice_of_a_mod_with_options_waits_for_them(root, manager, tmp_path, monkeypatch):
    from mhwmm import gui
    from mhwmm.manager import MaterialJob, MaterialRun

    app = gui.App(root, manager, queue.Queue())
    shown = []
    monkeypatch.setattr(gui.RequirementsDialog, "show", lambda self: shown.append(self))
    with manager.prepare_install(make_zip(tmp_path / "Rossi.zip", {"NPR/natives/STM/a.mdf2.45": "m",
                                                                   "Vanilla/natives/STM/a.mdf2.45": "v"})) as s:
        mod = manager.install_package(s, s.variants)
    manager.set_enabled(mod.id, True)
    assert mod.needs_option  # (a new mod with options: none chosen yet)
    item = {"name": "셰이더 EF_Skin_NPR.mmtr", "url": "", "notes": "", "kind": "shader", "state": "missing", "by": "",
            "option": "", "mod_off": False, "shaders": [], "materials": ["body"]}
    monkeypatch.setattr(manager, "shader_requirements",
                        lambda mod_id: [dict(item)] if mod_id == mod.id and manager.get(mod_id).selected else [])
    monkeypatch.setattr(manager, "finish_materials", lambda run, jobs: [])
    monkeypatch.setattr(manager, "material_state", lambda mod_id: "current")
    monkeypatch.setattr(app, "_offer_materials", lambda: None)
    app._first_check.add(mod.id)
    run = MaterialRun("fp", False, {mod.id: []})
    app._on_materials_checked(run, [MaterialJob(mod.id, mod.name, f"{mod.installed_at}|{mod.updated_at}", [])], None)
    assert shown == [] and mod.id in app._first_check  # (checked before an option was chosen: told once they are)
    app._set_options(mod.id, [mod.options[0]["id"]])
    pump(root, lambda: bool(shown))
    assert len(shown) == 1 and mod.id not in app._first_check


def test_the_install_time_requirement_notice_waits_for_another_window(root, manager, tmp_path, monkeypatch):
    from mhwmm import gui

    app = gui.App(root, manager, queue.Queue())
    shown = []
    monkeypatch.setattr(gui.RequirementsDialog, "show", lambda self: shown.append(self))
    with manager.prepare_install(make_zip(tmp_path / "Chen.zip", {"natives/STM/a.tex": "t"})) as s:
        mod = manager.install_variant(s, s.variants[0])
    monkeypatch.setattr(manager, "missing_requirements", lambda mod_id: [
        {"name": "_CatLib", "url": "", "notes": "", "kind": "lua", "state": "missing", "by": ""}])
    modal = [True]
    monkeypatch.setattr(app, "_modal_open", lambda: modal[0])
    app._queue_requirement_notices([mod.id])
    assert shown == [] and app._req_notices == [mod.id]
    root.after(300, lambda: modal.__setitem__(0, False))
    pump(root, lambda: bool(shown))
    assert len(shown) == 1 and app._req_notices == []


def test_gui_links_mods_from_file_names_and_reads_requirements(root, manager, tmp_path, monkeypatch):
    from mhwmm import gui, nexus

    with manager.prepare_install(make_zip(tmp_path / "Plain.zip", {"natives/STM/p.tex": "p"})) as s:
        plain = manager.install_variant(s, s.variants[0])
    with manager.prepare_install(make_zip(tmp_path / "Mod Name-787-v1-0-1740000000.zip", {"natives/STM/a.tex": "a"})) as s:
        mod = manager.install_variant(s, s.variants[0])
    assert mod.nexus_id == 787
    mod.nexus_id = 0  # what an older version left for a "v1.0" file name
    manager.save()
    asked = []
    monkeypatch.setattr(nexus, "fetch_requirements", lambda ids, key="": asked.append(ids) or {787: [
        {"name": "Lua Bone System", "url": nexus.MOD_URL.format(10), "nexus_id": 10, "notes": "", "external": False}]})

    app = gui.App(root, manager, queue.Queue())
    assert mod.nexus_id == 787  # read from the original file name at start
    pump(root, lambda: mod.requirements_checked, timeout=8)  # requirements are read without an API key
    assert asked == [[787]] and mod.requirements[0]["name"] == "Lua Bone System"
    app.tree.selection_set(mod.id)
    app._update_details()
    info = app.info_text.get("1.0", "end")
    assert "모드 페이지 #787" in info and "Lua Bone System" in info

    # a mod without a Nexus file name offers to link it
    app.tree.selection_set(plain.id)
    app._update_details()
    assert "연결 안 됨" in app.info_text.get("1.0", "end")


def crash_log(game_dir, start: float, plugin=None) -> str:
    """A REFramework log (CRLF, local times) with, for ``plugin``, a crash in it one minute in."""
    from datetime import datetime

    def rec(t, msg, level="info"):
        return f"[{datetime.fromtimestamp(t):%Y-%m-%d %H:%M:%S}.000] [REFramework] [{level}] {msg}"

    lines = [rec(start, "REFramework entry"), rec(start, f"Current game path: {game_dir}")]
    if plugin is not None:
        lines += [rec(start + 60, "Exception occurred: c0000005", "error"), rec(start + 60, "Call stack:", "error"),
                  rec(start + 60, "", "error"), " KiUserExceptionDispatcher", r"  C:\WINDOWS\SYSTEM32\ntdll.dll + 0x165dee",
                  "", " Unknown symbol", f"  {plugin} + 0x1234", "", rec(start + 60, f"Module: 7ff600000000 {plugin}", "error")]
    else:
        lines.append(rec(start + 60, "Hooked DirectX 12"))
    return "\r\n".join(lines) + "\r\n"


def test_gui_crash_records(root, manager, tmp_path, game_dir, monkeypatch):
    import zipfile

    import mhwmm.manager
    from mhwmm import crashes, gui

    from conftest import write

    monkeypatch.setenv("APPDATA", "")  # REFramework's fallback folder of this PC is not part of the test
    with manager.prepare_install(make_zip(tmp_path / "Boom.zip", {"reframework/plugins/Boom.dll": "dll"})) as s:
        boom = manager.install_variant(s, s.variants[0])
    manager.update_settings(nexus_api_key="SECRET-KEY")
    manager.deploy()
    write(game_dir / "dinput8.dll", "refw")
    write(game_dir / "re2_fw_config.txt", "FontSize=16\r\nScriptRunner_LogToDisk=false\r\n")
    write(game_dir / "re2_framework_log.txt",
          crash_log(game_dir, time.time() - 600, game_dir / "reframework" / "plugins" / "Boom.dll"))

    # at start the records are copied and the crash is told in a bar that blocks nothing (once)
    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: bool(app.notice.winfo_manager()))
    assert crashes.NOTICE_CRASH in app.notice_title.cget("text") and "0xC0000005" in app.notice_detail.cget("text")
    (session,) = manager.crash_sessions()
    assert session.problem() == "crash" and crashes.pending_notice(manager.crash_dir) is None

    dialog = gui.CrashDialog(root, app.theme, manager, manager.crash_sessions())
    pump(root, lambda: isinstance(dialog.explanation, crashes.Explanation))
    shown = dialog.text.get("1.0", "end")
    assert "플러그인 'Boom.dll'에서 오류가 났습니다 → 의심되는 모드 'Boom'" in shown
    assert "근거: Exception occurred: c0000005" in shown and "0xC0000005" in dialog.summary.cget("text")
    link = dialog.text.tag_ranges(f"mod:{boom.id}")  # the mod's name shows it in the list
    assert link and dialog.text.get(*link[:2]) == "Boom"
    assert "크래시 덤프" not in dialog.context_label.cget("text") and dialog.dump_check.instate(["disabled"])

    # text pasted from the game's error window: what it gave is scrolled into view (below the session's)
    dialog.text.configure(height=3)
    dialog.deiconify()
    dialog.paste.insert("1.0", "Fatal D3D error (6, E_OUTOFMEMORY, 0x8007000e)")
    dialog.analyze()
    pump(root, lambda: dialog.pasted is not None)
    assert "붙여 넣은 문구에서 찾은 것" in dialog.text.get("1.0", "end") and "VRAM" in dialog.text.get("1.0", "end")
    dialog.update()
    assert dialog.text.yview()[0] > 0 and dialog.text.bbox(dialog.text.tag_ranges("heading")[0]) is not None
    dialog.withdraw()

    # REFramework's settings are not changed while the game runs (it writes them back from memory)
    config = (game_dir / "re2_fw_config.txt").read_bytes()
    monkeypatch.setattr(mhwmm.manager, "is_game_running", lambda: True)
    dialog.lua_var.set(True)
    dialog._toggle_lua()
    pump(root, lambda: "바꾸지 못했습니다" in dialog.lua_note.cget("text"))
    assert "게임이 실행 중입니다" in dialog.lua_note.cget("text") and not dialog.lua_var.get()
    assert (game_dir / "re2_fw_config.txt").read_bytes() == config
    monkeypatch.setattr(mhwmm.manager, "is_game_running", lambda: False)

    # REFramework's Lua error logging: one line of its config changes (CRLF kept)
    dialog.lua_var.set(True)
    dialog._toggle_lua()
    pump(root, lambda: "켰습니다" in dialog.lua_note.cget("text"))
    assert (game_dir / "re2_fw_config.txt").read_bytes() == b"FontSize=16\r\nScriptRunner_LogToDisk=true\r\n"

    # the support zip: the records, no state.json (the Nexus API key)
    target = tmp_path / "out" / "report.zip"
    target.parent.mkdir()
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **k: str(target))
    dialog.save_report()
    pump(root, lambda: target.is_file() and dialog.save_button.instate(["!disabled"]))
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
        everything = "".join(zf.read(n).decode("utf-8", "replace") for n in names)
        report = zf.read("report.txt").decode("utf-8")
    assert f"{session.id}/re2_framework_log.txt" in names and not any(n.endswith("state.json") for n in names)
    assert "SECRET-KEY" not in everything and "[켜짐] Boom" in report and "Fatal D3D error" in report
    dialog.destroy()

    # the window from the menu; a click on a mod name selects it in the list. While it is open the
    # manager's other events still come (a task finishing, the background material check...)
    handled = []

    def show(self):
        app.events.put(("call", handled.append, "while open"))
        pump(root, lambda: handled, timeout=3)
        self.destroy()
        return "show", boom.id

    monkeypatch.setattr(gui.CrashDialog, "show", show)
    app.show_crashes()
    pump(root, lambda: app.tree.selection() == (boom.id,))
    assert not app.notice.winfo_manager() and handled == ["while open"]
    # an error opening it does not stop them either
    real_sessions = manager.crash_sessions
    monkeypatch.setattr(manager, "crash_sessions", lambda: (_ for _ in ()).throw(PermissionError("locked")))
    monkeypatch.setattr(root, "report_callback_exception", lambda *a: handled.append("error"))
    app.show_crashes()
    pump(root, lambda: "error" in handled)
    app.events.put(("call", handled.append, "after"))
    pump(root, lambda: "after" in handled)
    monkeypatch.setattr(manager, "crash_sessions", real_sessions)

    # starting the game from here: the last game's records are copied first, the new one gets the mods
    started = []
    monkeypatch.setattr(gui.webbrowser, "open", lambda url: started.append(url))
    monkeypatch.setattr(gui.os, "startfile", lambda url: started.append(url), raising=False)
    write(game_dir / "re2_framework_log.txt", crash_log(game_dir, time.time() - 120))
    asked = []  # nothing new to tell: no look at the running programs (tasklist) before the game starts
    monkeypatch.setattr(gui, "is_game_running", lambda: asked.append(True) or False)
    app.launch_game()
    pump(root, lambda: bool(started))
    assert len(manager.crash_sessions()) == 2 and asked == []
    launch = (manager.crash_dir / crashes.LAUNCH_FILE).read_text(encoding="utf-8")
    assert boom.id in launch


def test_gui_crash_records_while_the_game_runs(root, manager, tmp_path, game_dir, monkeypatch):
    from mhwmm import crashes, gui

    from conftest import write

    monkeypatch.setenv("APPDATA", "")
    monkeypatch.setattr(gui, "is_game_running", lambda: True)
    with manager.prepare_install(make_zip(tmp_path / "Armor.zip", {"natives/STM/GameDesign/x.user.3": "u"})) as s:
        manager.install_variant(s, s.variants[0])
    manager.deploy()
    # the game crashed, was started again from Steam and runs now: its record (with a file of the armor
    # mod REFramework could not load) is not finished
    write(game_dir / "re2_framework_log.prev.txt",
          crash_log(game_dir, time.time() - 3600, game_dir / "MonsterHunterWilds.exe"))
    write(game_dir / "re2_framework_log.txt", crash_log(game_dir, time.time() - 300))
    write(game_dir / "reframework_faulty_files.txt", '"error" 2 "Invalid file" "GameDesign/x.user"\r\n')

    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: bool(app.notice.winfo_manager()))
    assert crashes.NOTICE_CRASH in app.notice_title.cget("text") and "0xC0000005" in app.notice_detail.cget("text")

    seen = {}

    def show(self):
        seen["titles"] = [str(t) for t in self.picker["values"]]
        seen["selected"] = self.picker.current()
        pump(root, lambda: isinstance(self.explanation, crashes.Explanation))
        seen["lead"] = self.explanation.findings[0].kind
        self.picker.current(0)
        self._explain()
        pump(root, lambda: isinstance(self.explanation, crashes.Explanation))
        seen["running"] = self.explanation.findings[0].text
        self.destroy()

    monkeypatch.setattr(gui.CrashDialog, "show", show)
    app.show_crashes()
    pump(root, lambda: "running" in seen)
    assert "게임 실행 중" in seen["titles"][0] and "0xC0000005" in seen["titles"][1]
    assert seen["selected"] == 1 and seen["lead"] == "game"  # the finished crash is shown first
    assert seen["running"] == "게임이 실행 중입니다. 이 기록은 게임을 끈 뒤에 완성됩니다."


# ---- 원인 모드 찾기 ----------------------------------------------------------------------------------
def bisect_mods(manager, tmp_path):
    """REFramework and three plain mods, applied."""
    ids = {}
    for name, files in (("REFramework", {"dinput8.dll": "MZ"}), ("Armor A", {"natives/STM/a.tex": "a"}),
                        ("Armor B", {"natives/STM/b.tex": "b"}), ("Armor C", {"natives/STM/c.tex": "c"})):
        with manager.prepare_install(make_zip(tmp_path / "zips" / f"{name}.zip", files)) as s:
            ids[name] = manager.install_variant(s, s.variants[0]).id
    manager.deploy()
    return ids


def texts(widget):
    """Every label's and button's text in a window."""
    out = []
    for child in widget.winfo_children():
        try:
            out.append(str(child.cget("text")))
        except tk.TclError:
            pass
        out += texts(child)
    return out


class FakeGame:
    """The game's processes for the watcher (as gamewatch.ProcessApi; the Windows API in the app): each
    launch starts one that ends after ``run`` seconds of real time with the exit code ``code_for()``
    gives at its start (the test pumps the event loop meanwhile)."""

    def __init__(self, game_dir, code_for, run=0.3):
        import threading

        self.game_dir, self.code_for, self.run = game_dir, code_for, run
        self.procs = {}
        self.lock = threading.Lock()

    def launch(self, url=None, run=None):
        now = time.time()
        with self.lock:
            pid = 1000 + len(self.procs)
            self.procs[pid] = {"created": now, "ended": now + (self.run if run is None else run),
                               "code": self.code_for()}
        return pid

    def alive(self, pid):
        return time.time() < self.procs[pid]["ended"]

    def find(self, exe_name):
        with self.lock:
            return [pid for pid, p in self.procs.items() if p["created"] <= time.time() < p["ended"]]

    def open(self, pid):
        return pid

    def image_path(self, pid):
        return str(self.game_dir / "MonsterHunterWilds.exe")

    def times(self, pid):
        p = self.procs[pid]
        return p["created"], None if self.alive(pid) else p["ended"]

    def wait(self, pid, ms):
        left = self.procs[pid]["ended"] - time.time()
        if left > 0:
            time.sleep(min(ms / 1000, left, 0.05))
        return not self.alive(pid)

    def exit_code(self, pid):
        return 259 if self.alive(pid) else self.procs[pid]["code"]

    def close(self, pid):
        pass


def watch_fast(watcher, game):
    """The app's watcher on a FakeGame, looking often and not waiting for a relaunch."""
    watcher.api, watcher.poll, watcher.relaunch_seconds, watcher.relaunch_wait = game, 0.05, 0, 0.1


def launcher(monkeypatch, game=None):
    """steam:// opens (and starts ``game``) here: the launches, in order."""
    from mhwmm import gui

    started = []

    def start(url):
        started.append(url)
        if game is not None:
            game.launch(url)

    monkeypatch.setattr(gui.webbrowser, "open", start)
    monkeypatch.setattr(gui.os, "startfile", start, raising=False)
    return started


def test_gui_bisect_finds_the_mod_and_goes_back(root, manager, tmp_path, game_dir, monkeypatch):
    from mhwmm import bisect, gamewatch, gui

    monkeypatch.setenv("APPDATA", "")
    monkeypatch.setattr(gui, "is_game_running", lambda: False)
    ids = bisect_mods(manager, tmp_path)
    manager.update_settings(auto_apply=False, apply_before_launch=False)  # the steps are applied anyway
    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: app.busy is None)
    # the game crashes (0xC0000005) when Armor B is in the game folder; the watcher sees it end
    game = FakeGame(game_dir, lambda: 0xC0000005 if (game_dir / "natives/STM/b.tex").exists() else 0)
    watch_fast(app.watcher, game)

    seen = {}

    def start_show(self):
        seen["start"] = texts(self)
        self.destroy()
        return True

    monkeypatch.setattr(gui.BisectStartDialog, "show", start_show)
    app.show_bisect()  # ☰ → 원인 모드 찾기
    pump(root, lambda: app._bisect is not None and app.busy is None and not manager.dirty)
    assert any("켜져 있는 모드 4개 중" in t for t in seen["start"])
    assert any("다른 모드에 필요한 모드(그 모드를 켤 때 함께 켬): REFramework" in t for t in seen["start"])
    assert manager.profile == bisect.TEMP_PROFILE and app.bisect_bar.winfo_manager() == "pack"
    assert app.bisect_title.cget("text") == "원인 찾기 · 1번째 실행"
    assert "적용했습니다" in app.bisect_text.cget("text")
    assert app.bisect_main.cget("text") == "▶  " + bisect.BUTTON_LAUNCH
    assert "원인 찾기 (임시)" in app.profile_button.cget("text")

    started = launcher(monkeypatch, game)
    asked = []

    def ask_show(self):
        on = {m.id for m in manager.mods if m.enabled}
        asked.append((self.default, ids["Armor B"] in on))
        self.destroy()
        return "crashed" if ids["Armor B"] in on else "fine"

    def result_show(self):
        seen["result"] = texts(self)
        seen["steps"] = [dict(s) for s in self.session.steps]
        self.destroy()
        return "disable", self.mods

    monkeypatch.setattr(gui.BisectAskDialog, "show", ask_show)
    monkeypatch.setattr(gui.BisectResultDialog, "show", result_show)
    for _ in range(14):
        if app._bisect is None:
            break
        n = len(started)
        pump(root, lambda: not app.watcher.busy)
        app.launch_game()  # ▶ 게임 실행: the watcher finds the game, and asks when it ends
        pump(root, lambda: len(started) > n)
        assert app._bisect.awaiting_answer and app.bisect_main.cget("text") == gui.BISECT_ANSWER
        assert app.bisect_text.cget("text") == gui.BISECT_PLAYING
        on = {m.id for m in manager.mods if m.enabled}
        assert (game_dir / "natives/STM/b.tex").exists() == (ids["Armor B"] in on)  # the step is in the game folder
        # (the next launch is worked out on another thread, then applied)
        pump(root, lambda: len(asked) > n and app.busy is None and not app._apply_pending
             and (app._bisect is None or (app._bisect_ready() and not manager.dirty)))
    # only a crash the manager saw is the answer picked first (an exit with code 0 is not taken as fine)
    assert asked and all(default == ("crashed" if crash else None) for default, crash in asked)
    assert any("원인으로 의심되는 모드: Armor B" in t for t in seen["result"])
    # what the manager saw of each step's game went with the answer: how long it ran, a detected crash
    assert all(s["seconds"] is not None and s["seconds"] < 60 for s in seen["steps"])
    assert all(s["auto"] == ("crash" if s["crashed"] else "") for s in seen["steps"])
    assert app._bisect is None and not app.bisect_bar.winfo_manager() and not bisect.active(manager.data_dir)
    pump(root, lambda: app.busy is None and not manager.dirty)
    assert manager.profile == "기본" and list(manager.profiles) == ["기본"]
    assert not manager.get(ids["Armor B"]).enabled and manager.get(ids["Armor A"]).enabled
    assert not (game_dir / "natives/STM/b.tex").exists() and (game_dir / "natives/STM/a.tex").exists()
    # the steps were not the user's setup: no last good set from them, and no crash notice either
    assert not (manager.crash_dir / gamewatch.LAST_GOOD_FILE).exists() and not app.notice.winfo_manager()

    # outside a search: a crash is told in the notice bar; with no record of it, its button finds the mod
    game.code_for = lambda: 0xC0000409
    pump(root, lambda: not app.watcher.busy)
    app.launch_game()
    pump(root, lambda: bool(app.notice.winfo_manager()))
    assert "비정상적으로 종료" in app.notice_title.cget("text") and "0xC0000409" in app.notice_detail.cget("text")
    assert app.notice_button.cget("text") == bisect.TITLE
    now = time.time()
    app._on_game_exit(gamewatch.GameExit(8, 0, now, now - 1200))  # a normal exit keeps its mods as the last good set
    pump(root, lambda: (manager.crash_dir / gamewatch.LAST_GOOD_FILE).exists())
    good = gamewatch.load_last_good(manager.crash_dir)
    assert good["code"] == 0 and {m["id"] for m in good["context"]["mods"] if m["enabled"]} == \
        {ids["REFramework"], ids["Armor A"], ids["Armor C"]}


def test_gui_bisect_waits_for_changes_and_goes_on_after_a_restart(root, manager, tmp_path, game_dir, monkeypatch):
    from mhwmm import bisect, gui

    from conftest import write

    monkeypatch.setenv("APPDATA", "")
    monkeypatch.setattr(gui, "is_game_running", lambda: False)
    ids = bisect_mods(manager, tmp_path)
    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: app.busy is None)
    # while the search is being prepared (its plan holds the mods' state) nothing changes either
    app._bisect_preparing = True
    app.toggle([ids["Armor A"]])
    assert manager.get(ids["Armor A"]).enabled and app.status_var.get() == bisect.BLOCKED
    app._bisect_preparing = False
    monkeypatch.setattr(gui.BisectStartDialog, "show", lambda self: (self.destroy(), True)[1])
    app._open_bisect_start(bisect.prepare(manager), False, "")
    pump(root, lambda: app.busy is None and not manager.dirty and not app._apply_pending)

    # what would change the mods the search turns on and off waits until it ends
    told, opened = [], []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: told.append(a[1]))
    monkeypatch.setattr(gui.TextDialog, "show", lambda self: (opened.append("text"), self.destroy(), None)[2])
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda *a, **k: opened.append("folder") or "")
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: opened.append("ask") or False)
    monkeypatch.setattr(manager, "detect_game_dir", lambda: opened.append("detect"))
    enabled = {m.id: m.enabled for m in manager.mods}
    order = [m.id for m in manager.mods]
    app.tree.selection_set(ids["Armor C"])
    app.toggle([ids["Armor A"]])
    app.move_selected(-1)
    app._set_options(ids["Armor C"], [])
    app.set_enabled_all(False)
    app.switch_profile("기본")
    app.add_paths([str(tmp_path / "zips" / "Armor A.zip")])
    app.remove_selected()
    app.purge()
    for action in (lambda: app.new_profile(True), app.rename_profile, app.delete_profile, app.browse_game,
                   app.detect_game, lambda: app.install_refw(None)):
        action()
    assert {m.id: m.enabled for m in manager.mods} == enabled and [m.id for m in manager.mods] == order
    assert manager.profile == bisect.TEMP_PROFILE and not app._install_queue and manager.get(ids["Armor C"])
    assert told == [bisect.BLOCKED] * 11 and app.status_var.get() == bisect.BLOCKED and not opened
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)

    # the step's game is started, then the manager is closed; the game crashes meanwhile
    started = launcher(monkeypatch)
    app.launch_game()
    pump(root, lambda: bool(started))
    session = app._bisect
    session.mark_launched(time.time() - 300)
    write(game_dir / "re2_framework_log.txt",
          crash_log(game_dir, time.time() - 290, game_dir / "MonsterHunterWilds.exe"))
    app._destroy()

    root2 = tk.Tk()
    try:
        asked = []

        def ask_show(self):
            asked.append((self.default, texts(self)))
            self.destroy()
            return None  # answered later, from the bar

        monkeypatch.setattr(gui.BisectAskDialog, "show", ask_show)
        app2 = gui.App(root2, manager.__class__(manager.data_dir), queue.Queue())
        pump(root2, lambda: bool(asked))
        m2 = app2.manager
        assert app2._bisect is not None and app2.bisect_bar.winfo_manager() == "pack"
        default, shown = asked[0]
        assert default == "crashed" and any("비정상 종료를 확인했습니다 (0xC0000005)" in t for t in shown)
        assert app2._bisect.awaiting_answer and app2.bisect_main.cget("text") == gui.BISECT_ANSWER
        assert not app2.notice.winfo_manager()  # the crash of a step is the search's own question
        app2._answer_bisect(True, "비정상 종료를 확인했습니다 (0xC0000005)")
        assert app2._bisect.steps[-1]["crashed"] and app2._bisect.steps[-1]["detected"].endswith("(0xC0000005)")
        pump(root2, lambda: app2.busy is None and not m2.dirty and not app2._apply_pending)

        # a game update stops the search; its window takes the user back to the profile
        write(game_dir / "re_chunk_000.pak.sub_000.pak.patch_002.pak", "update")
        results = []

        def result_show(self):
            results.append(texts(self))
            self.destroy()
            return ("restore",)

        monkeypatch.setattr(gui.BisectResultDialog, "show", result_show)
        app2.launch_game()
        pump(root2, lambda: bool(results) and app2.busy is None and not m2.dirty)
        assert any("게임이 업데이트되어" in t for t in results[0]) and len(started) == 1
        assert m2.profile == "기본" and all(m.enabled for m in m2.mods) and app2._bisect is None
        assert (game_dir / "natives/STM/a.tex").exists() and (game_dir / "natives/STM/b.tex").exists()
        app2._destroy()
    finally:
        gc.collect()
        try:
            root2.destroy()
        except tk.TclError:
            pass


def test_gui_bisect_from_the_crash_records(root, manager, tmp_path, game_dir, monkeypatch):
    from mhwmm import bisect, gamewatch, gui

    from conftest import write

    monkeypatch.setenv("APPDATA", "")
    monkeypatch.setattr(gui, "is_game_running", lambda: False)
    bisect_mods(manager, tmp_path)
    write(game_dir / "re2_framework_log.txt", crash_log(game_dir, time.time() - 600, game_dir / "MonsterHunterWilds.exe"))
    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: bool(app.notice.winfo_manager()))
    (record,) = manager.crash_sessions()

    dialog = gui.CrashDialog(root, app.theme, manager, manager.crash_sessions())
    assert gui.bisect.TITLE in texts(dialog)
    dialog._bisect()  # its 원인 모드 찾기 button
    assert dialog.result == ("bisect", record.id)

    seen = []

    def start_show(self):
        seen.append(texts(self))
        self.destroy()
        return None  # 취소

    monkeypatch.setattr(gui.BisectStartDialog, "show", start_show)
    monkeypatch.setattr(gui.CrashDialog, "show", lambda self: (self.destroy(), ("bisect", record.id))[1])
    app.show_crashes()
    pump(root, lambda: bool(seen))
    assert any(t == f"게임 튕김 기록: {record.title()}" for t in seen[0]) and app._bisect is None
    assert manager.profile == "기본" and not bisect.active(manager.data_dir)

    # the texts of how a step's game ended
    now = time.time()
    crash = gamewatch.Detection(True, "c0000005", gamewatch.GameExit(1, 0xC0000005, now, now - 573), record)
    style, head, detail = gui.exit_summary(crash)
    assert (style, head) == ("Alert", "비정상 종료를 확인했습니다 (0xC0000005)")
    assert "종료 코드 0xC0000005 (메모리 접근 오류) · 9분 33초 실행" in detail and "REFramework 기록: " in detail
    record.info["summary"] = {**record.summary, "crash_from": "report"}  # the game's own report, not REFramework's
    detail = gui.exit_summary(crash)[2]
    assert "게임 오류 보고서: " in detail and "REFramework 기록" not in detail
    normal = gamewatch.Detection(False, "", gamewatch.GameExit(1, 0, now, now - 1260))
    style, head, detail = gui.exit_summary(normal)  # what was seen, not a verdict (code 0 may be a crash too)
    assert (style, head) == ("Banner", "게임이 종료 코드 0으로 끝났고 REFramework 오류 기록은 없습니다.")
    assert "21분 0초 실행" in detail and "'튕겼어요'를 고르세요" in detail
    polled = gamewatch.Detection(None, "", gamewatch.GameExit(1, None, now, None, now - 95, method="poll"))
    assert gui.exit_summary(polled)[0] == "" and "1분 35초 실행 이상" in gui.exit_summary(polled)[2]
    assert "확인하지 못했습니다" in gui.exit_summary(None)[1]
    # only a detected crash is the answer picked first
    step = bisect.Step(1, 3, "half", ["a"], [])
    for detection, default in ((crash, "crashed"), (normal, None), (polled, None), (None, None)):
        dialog = gui.BisectAskDialog(root, app.theme, step, detection)
        assert dialog.default == default
        dialog.destroy()


def test_gui_bisect_asks_when_the_answer_can_be_taken(root, manager, tmp_path, game_dir, monkeypatch):
    from mhwmm import bisect, gamewatch, gui

    monkeypatch.setenv("APPDATA", "")
    bisect_mods(manager, tmp_path)
    game = FakeGame(game_dir, lambda: 0, run=600)
    monkeypatch.setattr(gui, "is_game_running", lambda: bool(game.find("MonsterHunterWilds.exe")))
    monkeypatch.setattr(gamewatch, "default_api", lambda: game)  # the app watches the fake game from its start
    app = gui.App(root, manager, queue.Queue())
    watch_fast(app.watcher, game)
    pump(root, lambda: app.busy is None)
    monkeypatch.setattr(gui.BisectStartDialog, "show", lambda self: (self.destroy(), True)[1])
    app._open_bisect_start(bisect.prepare(manager), False, "")
    pump(root, lambda: app.busy is None and not manager.dirty and not app._apply_pending)
    asked, told = [], []

    def ask_show(self):
        asked.append((self.default, texts(self)))
        self.destroy()
        return "crashed"

    monkeypatch.setattr(gui.BisectAskDialog, "show", ask_show)
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: told.append(a[1]))
    launcher(monkeypatch, game)
    app.launch_game()
    pump(root, lambda: app.watcher.pid is not None)
    app.ask_bisect()  # 결과 알리기 while the game still runs: asked once it has ended
    assert told == [gui.BISECT_STILL_RUNNING] and not asked and app._bisect.awaiting_answer
    (pid,) = game.find("MonsterHunterWilds.exe")
    app._destroy()  # the manager is closed while the game runs

    root2 = tk.Tk()
    try:
        app2 = gui.App(root2, manager.__class__(manager.data_dir), queue.Queue())
        watch_fast(app2.watcher, game)
        # opened again: it finds the game and waits for it (the search's next launch worked out again first)
        pump(root2, lambda: app2.watcher.pid == pid and app2._bisect_ready())
        assert app2._bisect.awaiting_answer and app2.bisect_text.cget("text") == gui.BISECT_PLAYING and not asked
        # the game crashes while 모든 모드 재질 점검 runs: the question waits until the answer can be taken
        app2.busy = "matfix"
        game.procs[pid].update(ended=time.time(), code=0xC0000005)
        pump(root2, lambda: app2._bisect_detection is not None)
        end = time.time() + 1.5
        while time.time() < end:
            root2.update()
            time.sleep(0.02)
        assert not asked and not app2._bisect.steps
        app2.busy = None
        pump(root2, lambda: bool(asked))
        default, shown = asked[0]
        assert default == "crashed" and any("비정상 종료를 확인했습니다 (0xC0000005)" in t for t in shown)
        pump(root2, lambda: app2.busy is None and not app2._apply_pending and not app2.manager.dirty
             and app2._bisect_ready())
        session = app2._bisect
        assert session.steps[0]["crashed"] and session.steps[0]["exit"]["code"] == 0xC0000005
        assert "적용했습니다" in app2.bisect_text.cget("text")
        app2.manager.dirty = True  # (a deploy runs, or failed): the line does not say it is applied
        app2._show_bisect_bar()
        assert "게임을 실행하면 먼저 게임 폴더에 적용합니다" in app2.bisect_text.cget("text")
        app2.manager.dirty = False

        # the next step's game is not found in time (Steam had to start first): the bar says to answer by hand
        pump(root2, lambda: not app2.watcher.busy)
        session.mark_launched(time.time())
        app2._show_bisect_bar()
        assert app2.bisect_text.cget("text") == gui.BISECT_PLAYING
        assert app2.watcher.watch(session.launched, seconds=0.2)
        pump(root2, lambda: app2.bisect_text.cget("text") == gui.BISECT_PLAYING_MANUAL)
        app2._destroy()
    finally:
        gc.collect()
        try:
            root2.destroy()
        except tk.TclError:
            pass


def test_gui_bisect_refw_result_and_old_paks(root, manager, tmp_path, game_dir, monkeypatch):
    import json

    from mhwmm import bisect, gui

    monkeypatch.setenv("APPDATA", "")
    monkeypatch.setattr(gui, "is_game_running", lambda: False)
    ids = bisect_mods(manager, tmp_path)
    # a PAK left in the game's patch chain by a version before 1.7.0
    with manager.prepare_install(make_zip(tmp_path / "zips" / "Pak.zip", {"x.pak": "P"})) as s:
        manager.install_variant(s, s.variants[0])
    manager.deploy()
    path = manager.data_dir / "deployment.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    key = next(k for k, e in manifest["files"].items() if e.get("pak"))
    entry = manifest["files"].pop(key)
    chain = "re_chunk_000.pak.sub_000.pak.patch_003.pak"
    (game_dir / entry["path"]).replace(game_dir / chain)
    st = (game_dir / chain).stat()
    entry.update(path=chain, size=st.st_size, mtime=st.st_mtime_ns)
    manifest["files"][chain] = entry
    path.write_text(json.dumps(manifest), encoding="utf-8")

    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: app.busy is None)
    told, shown = [], []
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: told.append(a[1]))
    monkeypatch.setattr(gui.BisectStartDialog, "show", lambda self: (shown.append(1), self.destroy(), True)[2])
    app.show_bisect()  # moved first (the search would read moving them as a game update), then press again
    assert any("예전 방식" in t for t in told) and not shown
    pump(root, lambda: app.busy is None and not manager.dirty and not app._apply_pending)
    assert not manager.outdated_paks() and app._bisect is None
    app.show_bisect()
    pump(root, lambda: app._bisect is not None and app.busy is None and not manager.dirty)
    assert shown

    # REFramework itself crashes: going back without turning it off comes first, and its update is offered
    session = app._bisect
    refw_id = ids["REFramework"]
    for _ in range(12):
        nxt = session.next()
        if isinstance(nxt, bisect.Result):
            break
        session.mark_launched()
        bisect.answer(manager, session, refw_id in nxt.on)
    assert nxt.kind == "core" and nxt.mods == [refw_id]
    dialog = gui.BisectResultDialog(root, app.theme, manager, session, nxt)
    styles = {}

    def buttons(widget):
        for child in widget.winfo_children():
            if isinstance(child, gui.ttk.Button):
                styles[str(child.cget("text"))] = str(child.cget("style"))
            buttons(child)

    buttons(dialog)
    assert styles[bisect.RESULT_RESTORE] == "Accent.TButton" and styles[bisect.RESULT_DISABLE] == "TButton"
    assert gui.BISECT_REFW in styles and any("모두 동작하지 않습니다" in t for t in texts(dialog))
    dialog.destroy()
    clicked = []
    monkeypatch.setattr(app, "_on_refw_click", lambda: clicked.append(1))
    monkeypatch.setattr(gui.BisectResultDialog, "show", lambda self: (self.destroy(), ("refw",))[1])
    app.show_bisect_result()
    pump(root, lambda: bool(clicked) and app.busy is None)
    assert app._bisect is None and manager.profile == "기본" and manager.get(refw_id).enabled


def test_gui_bisect_resumes_after_the_startup_checks(root, manager, tmp_path, game_dir, monkeypatch):
    from mhwmm import bisect, deploy, gui, manager as manager_module

    monkeypatch.setenv("APPDATA", "")
    monkeypatch.setattr(gui, "is_game_running", lambda: False)
    ids = bisect_mods(manager, tmp_path)
    with manager.prepare_install(make_zip(tmp_path / "zips" / "Big.zip", {
            f"natives/STM/big/f{i:03d}.tex": "x" for i in range(60)})) as s:
        manager.install_variant(s, s.variants[0])
    manager.deploy()
    session, step = bisect.start(manager, bisect.prepare(manager))
    manager.deploy()
    bisect.answer(manager, session, ids["Armor B"] in step.on)  # the next step is set, then the manager closes
    assert manager.dirty

    state = {"deploying": False, "verified": []}
    real_run, real_verify = deploy.Deployer.run, manager_module.ModManager.verify

    def run(self, *a, **k):
        state["deploying"] = True
        try:
            return real_run(self, *a, **k)
        finally:
            state["deploying"] = False

    def verify(self, *a, **k):
        state["verified"].append(state["deploying"])
        return real_verify(self, *a, **k)

    asked = []
    monkeypatch.setattr(deploy.Deployer, "run", run)
    monkeypatch.setattr(manager_module.ModManager, "verify", verify)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: asked.append(a[1]) or False)
    app = gui.App(root, manager.__class__(manager.data_dir), queue.Queue())
    pump(root, lambda: bool(state["verified"]) and app.busy is None and not app.manager.dirty)
    # the folder was checked before the step's deploy started, not while it changed the folder
    assert state["verified"] == [False] and not asked
    assert app._bisect is not None and "적용했습니다" in app.bisect_text.cget("text")


def v2_plan(evidence=None, **kw):
    """A plan over REFramework and twelve plain mods (synthetic facts, no manager)."""
    from mhwmm import bisect

    from test_bisect import twelve

    return bisect.make_plan(twelve(), profile="기본", evidence=evidence, **kw)


def test_gui_bisect_start_window(root, monkeypatch, tmp_path):
    from mhwmm import accessed, bisect, gui

    from test_bisect import Ev

    theme = gui.Theme(root, "dark")
    # a crash in a mod's plugin: that mod first with its 근거, the rest summed up
    ev = Ev(shares={"m7": 0.5}, reasons={"m7": ["튕긴 곳이 이 모드의 플러그인입니다 (m7.dll + 0x10)"]},
            none_reasons=["Windows 시스템 파일에서 튕겼습니다"], minutes_hint=9.0, record_title="10월 21일 18:45 · 오류 코드",
            equipment=[("armor:1", "레더 메일", ["m1", "m2"]), ("armor:2", "본 헬름", ["m3"])])
    plan = v2_plan(ev)
    offer = accessed.Offer(True, False, accessed.LOG_NOTE, {accessed.LOG_KEY: "true"}, "x/re2_fw_config.txt")
    dialog = gui.BisectStartDialog(root, theme, plan, "", offer, log_files=True)
    shown = texts(dialog)
    assert "게임 튕김 기록: 10월 21일 18:45 · 오류 코드" in shown and plan.intro() in shown
    assert bisect.RANKING_HEADING in shown and "'Mod 7'" in shown
    assert any(t.startswith("약 ") and t.endswith("%") for t in shown)
    assert "근거: 튕긴 곳이 이 모드의 플러그인입니다 (m7.dll + 0x10)" in shown
    assert any(t.startswith("그 밖: 나머지 모드 ") and "모드 문제가 아닐 수 있는 근거: Windows 시스템 파일" in t for t in shown)
    assert bisect.WHEN_TEXT["equip"] in shown and gui.BISECT_RECENT.format(m=9) in shown
    assert dialog.when_var.get() == "same" and accessed.LOG_NOTE in shown
    assert dialog.log_var.get() and "disabled" not in dialog.log_check.state()  # (the last choice)
    assert dialog.choice() == {"when": "same", "minutes": None, "equip": None, "log": True}
    dialog.when_var.set("time")
    dialog.minutes_var.set("12")
    dialog.log_var.set(False)
    assert dialog.choice() == {"when": "time", "minutes": 12.0, "equip": None, "log": False}
    dialog.equip_picker.current(1)
    dialog.when_var.set("equip")
    dialog.ok()
    assert dialog.result == {"when": "equip", "minutes": None, "equip": "armor:2", "log": False}
    # the box left at the record's minutes (rounded) is not a time the user typed: the record's crash times stay
    timed = v2_plan(Ev(minutes_hint=16.7, when_hint="time", crash_times=[1000.0, 3010.0, 3600.0]))
    dialog = gui.BisectStartDialog(root, theme, timed, "", offer)
    assert dialog.minutes_var.get() == "17" and dialog.choice() == {"when": "time", "minutes": None, "equip": None,
                                                                    "log": False}
    choice = dialog.choice()
    session = bisect.Session.new(tmp_path, timed, "기본", {}, "temp", when=choice["when"], minutes=choice["minutes"])
    assert not session.data["minutes_typed"] and session.data["params"]["passive_times"]
    assert session.data["crash_times"] == [1000.0, 3010.0, 3600.0]
    dialog.minutes_var.set("30")  # (typed: it replaces them)
    assert dialog.choice()["minutes"] == 30.0
    dialog.destroy()

    # no evidence at all: no rows, the same suspicion for every mod; the record cannot be turned on; no equipment
    plain = v2_plan()
    dialog = gui.BisectStartDialog(root, theme, plain, "", accessed.Offer(False, False, accessed.LOG_UNAVAILABLE_LOOSE),
                                   log_files=True)
    shown = texts(dialog)
    assert bisect.NO_EVIDENCE in shown and "'Mod 7'" not in shown and bisect.WHEN_TEXT["equip"] not in shown
    assert accessed.LOG_UNAVAILABLE_LOOSE in shown and not dialog.log_var.get()
    assert "disabled" in dialog.log_check.state()
    assert not any("그래픽" in t for t in shown)
    dialog.destroy()
    # already on (REFramework settings): ticked, nothing to change. A graphics error record (DEVICE_HUNG) is a crash like
    # any other: its title line and the render data mods' 근거, no box telling the user to check the drivers first
    line = "게임이 그래픽 오류(DEVICE_HUNG)로 멈췄고 이 모드에 모델·텍스처 파일이 있습니다"
    device = v2_plan(Ev(device_error="DEVICE_HUNG", record_title="10월 21일 18:46 · 그래픽 오류 (DEVICE_HUNG)",
                        weights={"m3": 2.5}, reasons={"m3": [line]}))
    dialog = gui.BisectStartDialog(root, theme, device, "", accessed.Offer(True, True, accessed.LOG_ALREADY))
    shown = texts(dialog)
    assert dialog.log_var.get() and "disabled" in dialog.log_check.state() and accessed.LOG_ALREADY in shown
    assert "게임 튕김 기록: 10월 21일 18:46 · 그래픽 오류 (DEVICE_HUNG)" in shown and f"근거: {line}" in shown
    assert not any("드라이버" in t or "드뭅니다" in t or "그래픽카드" in t for t in shown)
    assert not hasattr(device, "device_note")
    dialog.destroy()
    # a window taller than the screen scrolls (its buttons stay under it)
    monkeypatch.setattr(gui.BisectStartDialog, "winfo_screenheight", lambda self: 500)
    dialog = gui.BisectStartDialog(root, theme, plan, "", offer)
    dialog.update_idletasks()
    assert dialog.scroll.scrollbar.winfo_manager() == "pack" and dialog.scroll.canvas.winfo_reqheight() < 500
    dialog.destroy()


def test_gui_bisect_ask_window_notes(root):
    from mhwmm import bisect, gamewatch, gui

    theme = gui.Theme(root, "dark")
    names = {"a": "Armor A", "b": "Armor B", "r": "REFramework"}
    step = bisect.Step(3, 7, "off_some", ["r", "a"], ["b"], reason="split", focus=["b"],
                       ranking=[bisect.Suspect("one", ("a",), 0.62)], suspects=2, p=0.62, remaining=(2, 4), minutes=22,
                       crash_minutes=10.0, play="22분 넘게 해 보세요.")
    now = time.time()
    normal = gamewatch.Detection(False, "", gamewatch.GameExit(1, 0, now, now - 240))
    run = {"seconds": 240.0, "log": True, "unexposed": ["a"], "tail": [], "record": "", "auto": "normal"}
    dialog = gui.BisectAskDialog(root, theme, step, normal, names, run, retries=2)
    shown = texts(dialog)
    assert "3번째 실행 · 'Armor B'만 끄고 실행" in shown
    assert bisect.run_note(step, 240.0) in shown and "4분만 실행했습니다" in bisect.run_note(step, 240.0)
    assert bisect.exposure_note(step, ["a"], names) in shown and gui.BISECT_RETRY_HINT in shown
    assert dialog.default is None
    dialog.destroy()
    # a crash: the mods whose files the game read right before it (play time is not the question then)
    crash = gamewatch.Detection(True, "c0000005", gamewatch.GameExit(1, 0xC0000005, now, now - 900))
    run = {"seconds": 900.0, "log": True, "unexposed": [], "tail": ["b", "a"], "record": "", "auto": "crash"}
    dialog = gui.BisectAskDialog(root, theme, step, crash, names, run)
    shown = texts(dialog)
    assert gui.BISECT_TAIL.format(mods="'Armor B', 'Armor A'") in shown and dialog.default == "crashed"
    assert not any("오래 했습니다" in t for t in shown) and gui.BISECT_RETRY_HINT not in shown
    dialog.destroy()


def test_gui_bisect_more_confirm_and_stopping(root, manager, tmp_path, game_dir, monkeypatch):
    from mhwmm import bisect, gui

    monkeypatch.setenv("APPDATA", "")
    monkeypatch.setattr(gui, "is_game_running", lambda: False)
    ids = bisect_mods(manager, tmp_path)
    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: app.busy is None)
    monkeypatch.setattr(gui.BisectStartDialog, "show", lambda self: (self.destroy(), {"when": "same"})[1])
    app._open_bisect_start(bisect.prepare(manager), False, "")
    pump(root, lambda: app.busy is None and not manager.dirty and not app._apply_pending)
    session = app._bisect

    # 그만두기 before a result says what is suspected most so far
    asked = []
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: asked.append(a[1]) or False)
    app.stop_bisect()
    # (every mod as suspected as the others yet: none is named for its place in the list)
    assert asked[0].endswith(" 아직 두드러지게 의심되는 모드는 없습니다(같은 정도 4개, 각각 약 20%).") and app._bisect is session

    # while the next launch is worked out the bar says so and only 그만두기 is there
    app._bisect_computing = True
    app._show_bisect_bar()
    assert app.bisect_title.cget("text") == "원인 찾기" and app.bisect_text.cget("text") == gui.BISECT_COMPUTING
    assert not app.bisect_main.winfo_manager() and app.bisect_stop.winfo_manager() == "pack"
    app.launch_game()  # (nothing starts meanwhile)
    pump(root, lambda: app.status_var.get() == gui.BISECT_COMPUTING)
    assert session.launched is None
    app._bisect_computing = False
    app._show_bisect_bar()
    step = session.next()
    assert app.bisect_text.cget("text").endswith(bisect.progress_text(step, session.names))
    assert "의심되는 모드 " in app.bisect_text.cget("text") and app.bisect_main.winfo_manager() == "pack"

    # answered like a user whose game crashes with Armor A: found, then 한 번 더 확인 goes on with three pairs
    seen = []

    def result_show(self):
        seen.append((texts(self), self.more))
        self.destroy()
        return ("more",) if len(seen) == 1 else ("restore",)

    monkeypatch.setattr(gui.BisectResultDialog, "show", result_show)
    for _ in range(20):
        nxt = session.next()
        if not isinstance(nxt, bisect.Step) or seen:
            break
        session.mark_launched()
        app._answer_bisect(ids["Armor A"] in nxt.on)
        pump(root, lambda: app._bisect is None or (app._bisect_ready() and app.busy is None and not manager.dirty
                                                   and not app._apply_pending))
        if not seen:  # how the answer moved the top suspect stays in the bar (the deploy takes the status bar over)
            change = session.last_change()
            assert change.startswith("원인 찾기: ") and app.bisect_text.cget("text").endswith("\n" + change)
    shown, more = seen[0]
    assert more == bisect.RESULT_CONFIRM and bisect.RESULT_CONFIRM in shown and "근거" in shown
    assert any("원인으로 의심되는 모드: Armor A (가능성 약 " in t for t in shown)
    pump(root, lambda: app._bisect_ready() and app.busy is None and not manager.dirty)
    assert session.strict and isinstance(session.next(), bisect.Step) and app._bisect is session
    for _ in range(12):
        nxt = session.next()
        if not isinstance(nxt, bisect.Step):
            break
        session.mark_launched()
        app._answer_bisect(ids["Armor A"] in nxt.on)
        pump(root, lambda: app._bisect is None or (app._bisect_ready() and app.busy is None and not manager.dirty
                                                   and not app._apply_pending))
    pump(root, lambda: len(seen) == 2 and app._bisect is None and app.busy is None)
    assert seen[1][1] == "" and manager.profile == "기본" and all(m.enabled for m in manager.mods)

    # unsure: 계속 찾기 and turning the top suspect off only with its probability on the button
    app._open_bisect_start(bisect.prepare(manager), False, "")
    pump(root, lambda: app.busy is None and not manager.dirty and not app._apply_pending)
    session = app._bisect
    guess = session.interim()
    session.data["result"] = bisect.Result("unsure", [ids["Armor B"]], 4, p=0.62, ranking=list(guess.ranking),
                                           evidence=["'Armor B' 약 55%"], r_mean=0.9).to_dict()
    session.save()
    dialog = gui.BisectResultDialog(root, app.theme, manager, session, session.result)
    shown = texts(dialog)
    assert "아직 하나로 좁히지 못했습니다." in shown and bisect.RESULT_MORE in shown and dialog.more == bisect.RESULT_MORE
    assert "'Armor B'(약 62%)을(를) 끄고 원래 프로필로" in shown and "지금까지 가장 의심되는 것" in shown
    dialog.destroy()
    weak = bisect.Result("unsure", [], 4, p=0.3, ranking=list(guess.ranking))
    dialog = gui.BisectResultDialog(root, app.theme, manager, session, weak)
    assert not any("끄고 원래 프로필로" in t and "약" in t for t in texts(dialog)) and dialog.mods == []
    dialog.destroy()
    monkeypatch.setattr(gui.BisectResultDialog, "show", lambda self: (self.destroy(), ("more",))[1])
    app.show_bisect_result()
    pump(root, lambda: app._bisect_ready() and app.busy is None and not manager.dirty)
    assert session.extra == bisect.MORE_LAUNCHES and session.result is None and isinstance(session.next(), bisect.Step)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    app.stop_bisect()
    pump(root, lambda: app._bisect is None and app.busy is None)
    assert manager.profile == "기본" and not bisect.active(manager.data_dir)


def test_gui_bisect_file_record_setting(root, manager, tmp_path, game_dir, monkeypatch):
    from mhwmm import accessed, bisect, gui

    from conftest import write

    monkeypatch.setenv("APPDATA", "")
    monkeypatch.setattr(gui, "is_game_running", lambda: False)
    # REFramework and two PAK mods (natives\ has no files: loose loading may be switched on with the record)
    ids = {}
    for name, files in (("REFramework", {"dinput8.dll": "MZ"}), ("Pak A", {"a.pak": "A"}), ("Pak B", {"b.pak": "B"})):
        with manager.prepare_install(make_zip(tmp_path / "zips" / f"{name}.zip", files)) as s:
            ids[name] = manager.install_variant(s, s.variants[0]).id
    manager.deploy()
    config = write(game_dir / "re2_fw_config.txt", "FontSize=16\r\n")
    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: app.busy is None)

    seen = []

    def start_show(self):
        seen.append((texts(self), self.log_var.get(), str(self.log_check.state())))
        self.log_var.set(True)
        self.ok()
        return self.result

    monkeypatch.setattr(gui.BisectStartDialog, "show", start_show)
    app.show_bisect()
    pump(root, lambda: app._bisect is not None and app.busy is None and not manager.dirty)
    shown, ticked, state = seen[0]
    assert accessed.LOG_NOTE + accessed.LOG_NOTE_LOOSE in shown and not ticked and "disabled" not in state
    assert b"LooseFileLoader_LogAccessedFiles=true" in config.read_bytes()
    assert b"LooseFileLoader_Enabled=true" in config.read_bytes() and manager.settings.bisect_log_files
    session = app._bisect
    assert session.refw_restore["values"] == {accessed.LOOSE_KEY: None, accessed.LOG_KEY: None}

    # back: the setting as it was (written "false", REFramework's default)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    app.stop_bisect()
    pump(root, lambda: app._bisect is None and app.busy is None)
    text = config.read_text()
    assert "LooseFileLoader_LogAccessedFiles=false" in text and "LooseFileLoader_Enabled=false" in text
    assert "FontSize=16" in text and not (manager.data_dir / accessed.RESTORE_FILE).exists()
    assert app.status_var.get() == accessed.RESTORED  # (said after the deploy that follows, not replaced by "준비")

    # the 게임 튕김 기록 window turns it on for good (no restore kept), and off again
    write(manager.data_dir / accessed.RESTORE_FILE,
          '{"format": 1, "time": 1, "restore": {"path": "", "values": {"LooseFileLoader_LogAccessedFiles": null}}}')
    dialog = gui.CrashDialog(root, app.theme, manager, manager.crash_sessions())
    assert "  " + accessed.CRASH_WINDOW_CHECKBOX in texts(dialog)
    pump(root, lambda: dialog.access_offer is not None)
    assert not dialog.access_var.get() and "disabled" not in dialog.access_check.state()
    assert dialog.access_note.cget("text").startswith(gui.CRASH_ACCESS_OFF)
    dialog.access_var.set(True)
    dialog._toggle_access()
    pump(root, lambda: dialog.access_offer is not None and dialog.access_offer.already)
    text = config.read_text()
    assert "LooseFileLoader_LogAccessedFiles=true" in text and "LooseFileLoader_Enabled=true" in text
    assert not (manager.data_dir / accessed.RESTORE_FILE).exists()
    # (what was just done stays said: the offer worked out again after it does not replace it)
    assert dialog.access_var.get() and dialog.access_note.cget("text") == gui.CRASH_ACCESS_DONE
    dialog.access_var.set(False)
    dialog._toggle_access()
    pump(root, lambda: not dialog.access_offer.already)
    text = config.read_text()  # off: both settings it turned on as they were, as the window said
    assert "LooseFileLoader_LogAccessedFiles=false" in text and "LooseFileLoader_Enabled=false" in text
    assert not dialog.access_var.get() and dialog.access_note.cget("text") == gui.CRASH_ACCESS_OFF_DONE
    assert not (manager.data_dir / accessed.KEPT_FILE).exists()
    # on, then a mod with loose files installed: off keeps loose file loading on (else that mod would stop working)
    dialog.access_var.set(True)
    dialog._toggle_access()
    pump(root, lambda: dialog.access_offer.already)
    with manager.prepare_install(make_zip(tmp_path / "zips" / "Loose.zip", {"natives/STM/a.tex": "t"})) as s:
        manager.install_variant(s, s.variants[0])
    dialog.access_var.set(False)
    dialog._toggle_access()
    pump(root, lambda: not dialog.access_offer.already)
    text = config.read_text()
    assert "LooseFileLoader_LogAccessedFiles=false" in text and "LooseFileLoader_Enabled=true" in text
    assert dialog.access_note.cget("text") == gui.CRASH_ACCESS_OFF_DONE + gui.CRASH_ACCESS_LOOSE_KEPT
    dialog.destroy()
    assert all(getattr(dialog, name) is None for name in ("session_var", "lua_var", "access_var", "dump_var"))
    # on for a search that ended while the game ran (its restore waits for the game to close): not said to be on for
    # good - ticked here, it is kept on (the restore dropped; turning it off here later puts it back)
    write(config, "LooseFileLoader_Enabled=true\r\nLooseFileLoader_LogAccessedFiles=true\r\n")
    write(manager.data_dir / accessed.RESTORE_FILE,
          '{"format": 1, "time": 1, "restore": {"path": "", "values": {"LooseFileLoader_LogAccessedFiles": null}}}')
    dialog = gui.CrashDialog(root, app.theme, manager, manager.crash_sessions())
    pump(root, lambda: dialog.access_offer is not None)
    assert dialog.access_offer.already and not dialog.access_var.get() and "disabled" not in dialog.access_check.state()
    assert dialog.access_note.cget("text") == gui.CRASH_ACCESS_PENDING
    dialog.access_var.set(True)
    dialog._toggle_access()
    pump(root, lambda: not (manager.data_dir / accessed.RESTORE_FILE).exists() and dialog.access_var.get()
         and "disabled" not in dialog.access_check.state())
    assert accessed.enabled_here(manager.data_dir)["values"] == {accessed.LOG_KEY: None}
    assert dialog.access_note.cget("text") == gui.CRASH_ACCESS_DONE and "LogAccessedFiles=true" in config.read_text()
    dialog.destroy()
    # while a search runs it waits
    dialog = gui.CrashDialog(root, app.theme, manager, manager.crash_sessions(), searching=True)
    pump(root, lambda: dialog.access_offer is not None)
    assert "disabled" in dialog.access_check.state() and dialog.access_note.cget("text") == gui.CRASH_ACCESS_SEARCHING
    dialog.destroy()
    assert ids and not bisect.active(manager.data_dir)


# ---- 실행 전 점검 -----------------------------------------------------------------------------------
def test_gui_precheck_before_launch(root, manager, tmp_path, game_dir, monkeypatch):
    import mhwmm.manager
    from mhwmm import gui, precheck

    from conftest import write

    monkeypatch.setenv("APPDATA", "")
    monkeypatch.setattr(gui, "is_game_running", lambda: False)
    bisect_mods(manager, tmp_path)  # REFramework and three loose mods, applied
    config = game_dir / "re2_fw_config.txt"
    write(config, "FontSize=16\r\nLooseFileLoader_Enabled=false\r\n")
    write(game_dir / "dxgi.dll", "MZ")  # ReShade or the like, not placed by the manager
    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: app.busy is None)
    started = launcher(monkeypatch)
    seen, answers = [], []

    def show(self):
        seen.append({"title": self.title_label.cget("text"), "kinds": [i.kind for i in self._shown()],
                     "new": [i.kind for i in self._shown() if tuple(i.keys) in self.new],
                     "buttons": [b.cget("text") for b in self._buttons], "texts": texts(self)})
        answers.pop(0)(self)
        return self.result

    def fix_then_run(dialog):
        loose = next(i for i in dialog.report.important if i.kind == "config_loose")
        # not while the game runs: REFramework writes its settings back when it closes
        monkeypatch.setattr(mhwmm.manager, "is_game_running", lambda: True)
        dialog._act(loose)
        pump(root, lambda: not dialog._fixing)
        assert dialog.status.cget("text") == precheck.RUNNING and b"=false" in config.read_bytes()
        monkeypatch.setattr(mhwmm.manager, "is_game_running", lambda: False)
        dialog._act(loose)  # asked first (answered yes), then one line changes (CRLF kept)
        pump(root, lambda: not dialog._fixing)
        assert config.read_bytes() == b"FontSize=16\r\nLooseFileLoader_Enabled=true\r\n"
        assert [i.kind for i in dialog.report.important] == ["dll_proxy"] and "바꿨습니다" in dialog.status.cget("text")
        dialog.dont_ask_var.set(True)  # 같은 내용이면 다시 묻지 않기
        dialog.finish(("run",))

    monkeypatch.setattr(gui.PrecheckDialog, "show", show)
    answers.append(fix_then_run)
    app.launch_game()
    pump(root, lambda: started)
    (first,) = seen
    assert first["title"] == "실행 전 점검: 확인할 것 2개" and first["kinds"] == ["config_loose", "dll_proxy"]
    assert first["new"] == [] and first["buttons"] == ["설정 고치기", "폴더 열기"]  # (all new: none marked)
    assert precheck.DONT_ASK in "".join(first["texts"]) and precheck.RUN_ANYWAY in first["texts"]
    assert (manager.data_dir / "cache" / precheck.BACKGROUND_FILE).is_file()

    # the same again: not asked, the game starts right away
    app.launch_game()
    pump(root, lambda: len(started) == 2)
    assert len(seen) == 1

    # something new: asked, the new one marked; 취소 does not start the game
    write(game_dir / "pak_mods" / "hand.pak", "p")
    answers.append(lambda dialog: dialog.cancel())
    app.launch_game()
    pump(root, lambda: len(seen) == 2)
    pump(root, lambda: not app._prechecking)
    assert seen[1]["kinds"] == ["pak_unknown", "dll_proxy"] and seen[1]["new"] == ["pak_unknown"]
    assert len(started) == 2

    # 폴더 열기 works in the window; a button for the main window closes it without starting the game
    opened = []
    monkeypatch.setattr(gui.fsutil, "open_in_explorer", lambda path: opened.append(path))

    def open_then_apply(dialog):
        dialog._act(next(i for i in dialog.report.important if i.kind == "pak_unknown"))
        assert opened == [game_dir / "pak_mods"] and dialog.winfo_exists()
        dialog.finish(("action", precheck.Issue("verify", "medium", "x", action="apply")))

    applied = []
    monkeypatch.setattr(app, "apply", lambda: applied.append(True))
    answers.append(open_then_apply)
    app.launch_game()
    pump(root, lambda: applied)
    assert len(started) == 2

    # whatever fails in the check, the game starts
    monkeypatch.setattr(gui.precheck, "check", lambda *a, **k: 1 / 0)
    app.launch_game()
    pump(root, lambda: len(started) == 3)
    assert len(seen) == 3 and not app._prechecking

    # not during 원인 모드 찾기 (its steps turn mods off on purpose)
    monkeypatch.setattr(gui.precheck, "gather", lambda *a, **k: 1 / 0)
    plain = []
    monkeypatch.setattr(app, "_start_game", lambda: plain.append(True))
    app._bisect = object()
    app._precheck_then_start()
    app._bisect = None
    assert plain == [True]


def test_gui_precheck_from_the_menu_and_in_the_background(root, manager, tmp_path, game_dir, monkeypatch):
    import os

    from mhwmm import gui, precheck

    from conftest import write

    monkeypatch.setenv("APPDATA", "")
    monkeypatch.setattr(gui, "is_game_running", lambda: False)
    manager.settings.check_materials = False
    ids = {}
    for name, files in (("REFramework", {"dinput8.dll": "MZ"}), ("Armor", {"natives/STM/GameDesign/x.user.3": "u"}),
                        ("Camera", {"reframework/autorun/camera.lua": 'require("_CatLib")'})):
        with manager.prepare_install(make_zip(tmp_path / "zips" / f"{name}.zip", files)) as s:
            ids[name] = manager.install_variant(s, s.variants[0]).id
    old = time.time() - 7200
    for path in manager.mods_dir.rglob("*"):  # installed before the game below crashed
        os.utime(path, (old, old))
    manager.deploy()
    write(game_dir / "re2_fw_config.txt", "LooseFileLoader_Enabled=true\r\n")
    write(game_dir / "natives" / "STM" / "hand" / "manual.tex", "t")  # put in by hand
    # the last game: REFramework could not load a file of the armor mod
    write(game_dir / "re2_framework_log.txt", crash_log(game_dir, time.time() - 600))
    write(game_dir / "reframework_faulty_files.txt", '"error" 2 "Invalid file" "GameDesign/x.user"\r\n')

    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: app.busy is None and manager.crash_sessions())
    assert app._problems == manager.problems() == {ids["Camera"]: ["필요한 모드가 없거나 꺼져 있습니다: _CatLib"]}

    # the slow part in the background (at startup, after applying): kept for the next launch
    app.precheck_background()
    pump(root, lambda: app._precheck_runs == 0 and (manager.data_dir / "cache" / precheck.BACKGROUND_FILE).is_file())
    kept = precheck.load_background(manager.data_dir / "cache")
    assert kept.faulty[0]["mod"] == ids["Armor"] and kept.natives_total == 1
    assert app.status_var.get() == "준비"
    app.precheck_background()  # not due again
    assert app._precheck_runs == 0

    # ☰ → 실행 전 점검: every issue, low ones too; a button for the main window
    seen = {}

    def show(self):
        seen["kinds"] = [i.kind for i in self._shown()]
        seen["title"] = self.title_label.cget("text")
        seen["text"] = self.text.get("1.0", "end")
        seen["links"] = self.text.tag_ranges(f"mod:{ids['Armor']}")
        self.finish(("action", next(i for i in self._shown() if i.kind == "faulty")))
        return self.result

    checked = []
    monkeypatch.setattr(gui.PrecheckDialog, "show", show)
    monkeypatch.setattr(app, "check_materials", lambda mod_ids=None: checked.append(mod_ids))
    app.show_precheck()
    pump(root, lambda: checked)
    assert seen["kinds"][0] == "faulty" and {"requirement", "natives_unmanaged"} <= set(seen["kinds"])
    assert "지난번 게임에서 REFramework가 읽지 못한 파일: 'Armor'의 x.user" in seen["text"]
    assert gui.PRECHECK_HEADINGS["low"] in seen["text"] and seen["links"]
    assert checked == [[ids["Armor"]]] and not app._precheck_menu and app.status_var.get() == "준비"

    # REFramework off while mods need it: its button turns it on
    manager.set_enabled(ids["REFramework"], False)
    app.refresh()

    def enable(self):
        self.finish(("action", next(i for i in self._shown() if i.kind == "refw_off")))
        return self.result

    monkeypatch.setattr(gui.PrecheckDialog, "show", enable)
    app.show_precheck()
    pump(root, lambda: manager.get(ids["REFramework"]).enabled)

    # several mods at once are selected in the list
    app.filter_var.set("비활성")
    app.refresh()
    app.show_mods([ids["Armor"], ids["Camera"]])
    assert set(app.tree.selection()) == {ids["Armor"], ids["Camera"]} and app.filter_var.get() == gui.FILTER_ALL


def test_gui_precheck_window(root, manager, tmp_path):
    import types

    from mhwmm import gui, precheck
    from mhwmm.theme import Theme

    with manager.prepare_install(make_zip(tmp_path / "Armor.zip", {"natives/STM/a.user.3": "u"})) as s:
        armor = manager.install_variant(s, s.variants[0])
    issues = [
        precheck.Issue("faulty", "high", "지난번 게임에서 REFramework가 읽지 못한 파일: 'Armor'의 a.user → 재질 복구를 해 보세요",
                       ['"Invalid file" "a.user"'], [armor.id], "matfix", items=["a"]),
        precheck.Issue("config_loose", "high", "루스 파일 읽기가 꺼져 있습니다", ["LooseFileLoader_Enabled=false"], [],
                       "fix_config", ("LooseFileLoader_Enabled", "true"), items=["b"]),
        precheck.Issue("pak_count", "medium", "PAK이 많습니다", [], [], "open_folder", folder="pak_mods", items=["c"]),
        precheck.Issue("script_unmanaged", "low", "스크립트가 있습니다", ["reframework/autorun/x.lua"], [], "open_folder",
                       folder="reframework/autorun", items=["d"]),
    ]
    report = precheck.Report(issues, {}, "v1", "current")
    theme = Theme(root, "dark")

    # before a launch: the high and medium ones; only the new ones marked (some were seen before)
    dialog = gui.PrecheckDialog(root, theme, manager, report, launch=True, new=issues[:1])
    text = dialog.text.get("1.0", "end")
    assert dialog.title_label.cget("text") == "실행 전 점검: 확인할 것 3개"
    assert gui.PRECHECK_HEADINGS["high"] in text and gui.PRECHECK_HEADINGS["medium"] in text
    assert gui.PRECHECK_HEADINGS["low"] not in text and "스크립트가 있습니다" not in text
    assert text.count(gui.PRECHECK_NEW) == 1 and text.index(gui.PRECHECK_NEW) < text.index("지난번 게임")
    assert [b.cget("text") for b in dialog._buttons] == ["재질 복구", "설정 고치기", "폴더 열기"]
    assert {precheck.RUN_ANYWAY, precheck.CANCEL, "  " + precheck.DONT_ASK} <= set(texts(dialog))
    assert 4 <= int(dialog.text.cget("height")) <= 18
    # a mod's name shows it in the list; a button for the main window gives its issue
    dialog.deiconify()
    dialog.update()
    start = dialog.text.tag_ranges(f"mod:{armor.id}")[0]
    x, y, _w, _h = dialog.text.bbox(start)
    dialog._on_link(types.SimpleNamespace(x=x + 2, y=y + 2))
    assert dialog.result == ("show", armor.id) and not dialog.winfo_exists()
    dialog = gui.PrecheckDialog(root, theme, manager, report, launch=True, new=issues[:3])
    assert gui.PRECHECK_NEW not in dialog.text.get("1.0", "end")  # (all new: nothing marked)
    dialog._buttons[0].invoke()
    assert dialog.result == ("action", issues[0]) and dialog.dont_ask is False

    # from the menu: every issue, counted by level, no "don't ask" box
    dialog = gui.PrecheckDialog(root, theme, manager, report, note="재질 점검을 하지 못했습니다.")
    assert dialog.title_label.cget("text") == "실행 전 점검: 확인할 것 4개"
    assert dialog.summary.cget("text") == "높음 2 · 보통 1 · 낮음 1"
    assert "스크립트가 있습니다" in dialog.text.get("1.0", "end") and "재질 점검을 하지 못했습니다." in dialog.note.cget("text")
    assert "  " + precheck.DONT_ASK not in texts(dialog)
    dialog.destroy()

    # nothing found (light theme)
    theme.use("light")
    dialog = gui.PrecheckDialog(root, theme, manager, precheck.Report([], {}, "v1", "current"))
    assert dialog.title_label.cget("text") == "실행 전 점검: 확인할 것이 없습니다."
    assert dialog.text.get("1.0", "end").strip() == gui.PRECHECK_NOTHING.strip() and not dialog._buttons
    dialog.destroy()


def test_gui_precheck_after_applying_and_while_a_deploy_runs(root, manager, tmp_path, game_dir, monkeypatch):
    """▶ with changes not applied: applied first, then 실행 전 점검, then the game; an apply that begins while
    the window is open is waited for (the game never starts on a half-applied folder), and the same
    issues are not asked again right after it."""
    from mhwmm import gui

    from conftest import write

    monkeypatch.setenv("APPDATA", "")
    monkeypatch.setattr(gui, "is_game_running", lambda: False)
    ids = bisect_mods(manager, tmp_path)
    manager.update_settings(auto_apply=False, apply_before_launch=True, check_materials=False)
    write(game_dir / "re2_fw_config.txt", "LooseFileLoader_Enabled=true\r\n")
    write(game_dir / "dxgi.dll", "MZ")  # (a medium issue: asked about before a launch)
    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: app.busy is None)
    order = []
    monkeypatch.setattr(gui.webbrowser, "open", lambda url: order.append(("start", app.busy, manager.dirty)))
    monkeypatch.setattr(gui.os, "startfile", lambda url: order.append(("start", app.busy, manager.dirty)),
                        raising=False)
    during = []

    def show(self):
        order.append(("asked", app.busy, manager.dirty, [i.kind for i in self.report.important]))
        if during:  # something applies while the window is open (the material check's apply, a toggle)
            during.pop()()
        self.destroy()
        return ("run",)

    monkeypatch.setattr(gui.PrecheckDialog, "show", show)
    manager.set_enabled(ids["Armor C"], False)
    app.refresh()
    assert manager.dirty
    app.launch_game()
    pump(root, lambda: any(o[0] == "start" for o in order))
    assert order == [("asked", None, False, ["dll_proxy"]), ("start", None, False)]
    assert not (game_dir / "natives/STM/c.tex").exists()

    # a deploy that begins while the window is open: the game starts after it, without asking again
    order.clear()

    def toggle_and_apply():
        manager.set_enabled(ids["Armor B"], False)
        app.apply()
        assert app.busy == "deploy"

    during.append(toggle_and_apply)
    app.launch_game()
    pump(root, lambda: any(o[0] == "start" for o in order))
    pump(root, lambda: app.busy is None)
    assert order == [("asked", None, False, ["dll_proxy"]), ("start", None, False)]
    assert not (game_dir / "natives/STM/b.tex").exists()

    # turned on while the window is open (no apply yet): applied before the game starts
    order.clear()
    during.append(lambda: manager.set_enabled(ids["Armor B"], True))
    app.launch_game()
    pump(root, lambda: any(o[0] == "start" for o in order))
    assert order == [("asked", None, False, ["dll_proxy"]), ("start", None, False)]
    assert (game_dir / "natives/STM/b.tex").is_file()

    # not applying first (that setting off): asked once whether to start with the changes left out
    manager.update_settings(apply_before_launch=False)
    manager.set_enabled(ids["Armor C"], True)
    app.refresh()
    asked = []
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: asked.append(a[1]) or True)
    order.clear()
    app.launch_game()
    pump(root, lambda: any(o[0] == "start" for o in order))
    assert order == [("asked", None, True, ["dll_proxy"]), ("start", None, True)]
    assert len(asked) == 1 and "그대로 실행할까요?" in asked[0]


def test_gui_precheck_while_the_material_check_runs(root, manager, tmp_path, game_dir, monkeypatch):
    """After a game update the mods not checked since are asked about before a launch, also while their
    check runs; a mod never checked (a new one) is not asked about."""
    from mhwmm import gui, matfix
    from mhwmm.manager import MATFIX_FORMAT

    from conftest import write

    if not matfix.available():
        pytest.skip("zstd 모듈이 없습니다")
    monkeypatch.setenv("APPDATA", "")
    manager.update_settings(check_materials=False)
    ids = {}
    for name in ("Old Armor", "New Armor"):
        with manager.prepare_install(make_zip(tmp_path / "zips" / f"{name}.zip",
                                              {f"natives/STM/Art/{name[:3]}.mdf2.45": "m"})) as s:
            ids[name] = manager.install_variant(s, s.variants[0]).id
    manager.deploy()
    write(game_dir / "re2_fw_config.txt", "LooseFileLoader_Enabled=true\r\n")
    write(game_dir / "dinput8.dll", "MZ")  # (REFramework put in by hand)
    # checked against the game before its update
    manager._save_material_record(ids["Old Armor"], {"game": "before-the-update", "format": MATFIX_FORMAT})
    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: app.busy is None)
    started = launcher(monkeypatch)
    seen = []

    def show(self):
        seen.append([(i.kind, i.level, i.mods, i.action) for i in self.report.important])
        self.destroy()
        return ("run",)

    monkeypatch.setattr(gui.PrecheckDialog, "show", show)
    app._matfix_running = True  # (the mods' own check, started after the update)
    app.launch_game()
    pump(root, lambda: started)
    assert seen == [[("unchecked", "medium", [ids["Old Armor"]], "show_mods")]]
    app._matfix_running = False
    app.launch_game()
    pump(root, lambda: len(started) == 2)
    assert seen[1] == [("unchecked", "medium", [ids["Old Armor"]], "matfix_check")]
    # a mod never checked is worth knowing (☰ → 실행 전 점검), not asked about
    manager.set_enabled(ids["Old Armor"], False)
    manager.deploy()
    app.refresh()
    app.launch_game()
    pump(root, lambda: len(started) == 3)
    assert len(seen) == 2
    assert [i.kind for i in manager.prelaunch_check().issues if i.kind.startswith("unchecked")] == ["unchecked_new"]
