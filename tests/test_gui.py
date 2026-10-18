import queue
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
    try:
        r.destroy()
    except tk.TclError:
        pass


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
    assert (game_dir / "re_chunk_000.pak.sub_000.pak.patch_002.pak").read_text() == "p"

    app.toggle([b.id])  # auto-apply kicks in
    pump(root, lambda: app.busy is None and not manager.dirty and not app._apply_pending)
    assert (game_dir / "natives/STM/a.tex").read_text() == "a"
    assert app.tree.item(b.id, "values")[0] == gui.CHECK_OFF
