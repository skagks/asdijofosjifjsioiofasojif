import gc
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
