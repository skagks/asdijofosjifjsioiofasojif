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


@pytest.fixture(autouse=True)
def answer_dialogs(monkeypatch):
    """A message box left open would block the test, so answer them all."""
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


def test_gui_closes_pak_gap_at_startup(root, manager, tmp_path, game_dir):
    from mhwmm import gui

    from conftest import write

    # PAKs of another mod manager sat at 2 and 3, so ours went to 4 ...
    chain = "re_chunk_000.pak.sub_000.pak"
    for n in (2, 3):
        write(game_dir / f"{chain}.patch_{n:03d}.pak", "fluffy")
    with manager.prepare_install(make_zip(tmp_path / "Chen.zip", {"Chen_Tex.pak": "tex"})) as s:
        manager.install_variant(s, s.variants[0])
    manager.deploy()
    assert (game_dir / f"{chain}.patch_004.pak").read_text() == "tex"
    # ... then they were removed: the game stops at 2 and never reads our textures
    for n in (2, 3):
        (game_dir / f"{chain}.patch_{n:03d}.pak").unlink()
    assert manager.pak_gaps()

    app = gui.App(root, manager, queue.Queue())
    pump(root, lambda: (game_dir / f"{chain}.patch_002.pak").exists() and app.busy is None and not manager.dirty)
    assert (game_dir / f"{chain}.patch_002.pak").read_text() == "tex"
    assert not (game_dir / f"{chain}.patch_004.pak").exists() and not manager.pak_gaps()
