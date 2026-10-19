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
    assert app.tree.item(b.id, "image")[0] == str(app.theme.image("check_off", 20))

    # a mod with options starts with all of them off and is applied once one is chosen
    with manager.prepare_install(make_zip(tmp_path / "Opts.zip", {
            "Opts/One/natives/STM/o.tex": "1", "Opts/Two/natives/STM/o.tex": "2"})) as s:
        opts = manager.install_package(s, s.variants)
    app.refresh()
    assert app.tree.item(opts.id, "values")[1] == "Opts"
    assert app.tree.item(opts.id, "values")[4] == "선택 필요"
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
