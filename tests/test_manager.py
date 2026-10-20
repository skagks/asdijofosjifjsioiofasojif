import zipfile
from pathlib import Path

import pytest

from mhwmm.archive import extract, ArchiveError
from mhwmm import fsutil
from mhwmm.manager import InstallError, ModManager

from conftest import make_tree, make_zip, write



def install(manager, path, pick=None, replace=False):
    with manager.prepare_install(path) as session:
        variants = session.variants if pick is None else [session.variants[i] for i in pick]
        mods = []
        for v in variants:
            existing = manager.find_by_name(v.name)
            mods.append(manager.install_variant(session, v, existing.id if existing and replace else None))
        return mods


def test_install_deploy_and_remove(manager, game_dir, tmp_path):
    archive = make_zip(tmp_path / "Armor-10-1-0-1740000000.zip", {
        "Armor/natives/STM/a.tex": "A",
        "Armor/readme.txt": "r",
    })
    (mod,) = install(manager, archive)
    assert mod.name == "Armor" and mod.version == "1.0" and mod.enabled
    assert manager.mod_files(mod.id) == ["natives/STM/a.tex"]
    assert manager.dirty

    report = manager.deploy()
    assert report.copied == 1 and not manager.dirty
    assert (game_dir / "natives/STM/a.tex").read_text() == "A"

    manager.remove(mod.id)
    assert manager.dirty
    manager.deploy()
    assert not (game_dir / "natives").exists()
    assert not manager.mod_dir(mod.id).exists()
    assert not list(manager.temp_root.glob("*")) if manager.temp_root.exists() else True


def test_replace_keeps_order_and_state(manager, tmp_path):
    first = make_zip(tmp_path / "v1" / "Thing.zip", {"natives/STM/a.tex": "old"})
    other = make_zip(tmp_path / "Other.zip", {"natives/STM/b.tex": "b"})
    (thing,) = install(manager, first)
    install(manager, other)
    manager.set_enabled(thing.id, False)

    second = make_zip(tmp_path / "v2" / "Thing.zip", {"natives/STM/a.tex": "new", "natives/STM/c.tex": "c"})
    (updated,) = install(manager, second, replace=True)
    assert updated.id == thing.id
    assert [m.id for m in manager.mods][0] == thing.id
    assert not updated.enabled
    assert sorted(manager.mod_files(thing.id)) == ["natives/STM/a.tex", "natives/STM/c.tex"]
    assert (manager.files_dir(thing.id) / "natives/STM/a.tex").read_text() == "new"


def test_duplicate_names_get_suffix(manager, tmp_path):
    a = make_zip(tmp_path / "1" / "Same.zip", {"natives/a.tex": "a"})
    b = make_zip(tmp_path / "2" / "Same.zip", {"natives/b.tex": "b"})
    install(manager, a)
    (second,) = install(manager, b)
    assert second.name == "Same (2)"


def test_conflicts(manager, tmp_path):
    (a,) = install(manager, make_zip(tmp_path / "A.zip", {"natives/x.tex": "a", "natives/y.tex": "a"}))
    (b,) = install(manager, make_zip(tmp_path / "B.zip", {"natives/X.tex": "b"}))
    c = manager.conflicts()
    assert c.counts(a.id) == (0, 1)
    assert c.counts(b.id) == (1, 0)
    beats, beaten = c.for_mod(b.id)
    assert list(beats) == [a.id]
    manager.move(b.id, -1)
    assert manager.conflicts().counts(a.id) == (1, 0)


def test_state_persists_and_recovers(manager, tmp_path, game_dir):
    (a,) = install(manager, make_zip(tmp_path / "A.zip", {"natives/x.tex": "a"}))
    manager.rename(a.id, "새 이름")
    reloaded = ModManager(manager.data_dir)
    assert reloaded.get(a.id).name == "새 이름"
    assert reloaded.game_dir == game_dir

    manager.state_path.write_text("{broken", encoding="utf-8")
    recovered = ModManager(manager.data_dir)
    assert [m.name for m in recovered.mods] == ["새 이름"]
    assert not recovered.mods[0].enabled


def test_install_single_pak_and_folder(manager, game_dir, tmp_path):
    pak = write(tmp_path / "downloads" / "MyPak.pak", "P")
    (m1,) = install(manager, pak)
    assert manager.mod_files(m1.id) == ["MyPak.pak"]
    assert pak.exists()  # the user's file is copied, not moved

    folder = make_tree(tmp_path / "Folder Mod", {"natives/STM/z.tex": "z"})
    (m2,) = install(manager, folder)
    assert (folder / "natives/STM/z.tex").exists()
    report = manager.deploy()
    assert (game_dir / "pak_mods/001_MyPak.pak").read_text() == "P"
    assert (game_dir / "natives/STM/z.tex").read_text() == "z"
    assert report.paks[0][1] == m1.name


def test_switch_game_dir_purges_old(manager, game_dir, tmp_path):
    install(manager, make_zip(tmp_path / "A.zip", {"natives/x.tex": "a"}))
    manager.deploy()
    new_game = tmp_path / "Other"
    write(new_game / "MonsterHunterWilds.exe", "exe")
    manager.set_game_dir(new_game)
    assert not (game_dir / "natives/x.tex").exists()
    manager.deploy()
    assert (new_game / "natives/x.tex").read_text() == "a"


class LegacyNameInfo(zipfile.ZipInfo):
    """Zip entry stored in cp949 without the UTF-8 flag, like old Korean Windows tools."""

    def _encodeFilenameFlags(self):
        return self.filename.encode("cp949"), self.flag_bits & ~0x800


def test_zip_slip_and_korean_names(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../evil.txt", "bad")
        zf.writestr(LegacyNameInfo("모드/natives/a.tex"), "ok")
    out = tmp_path / "out"
    extract(archive, out)
    assert not (tmp_path / "evil.txt").exists()
    assert (out / "모드" / "natives" / "a.tex").read_text() == "ok"


def test_bad_zip(tmp_path):
    bad = write(tmp_path / "bad.zip", "not a zip")
    with pytest.raises(ArchiveError):
        extract(bad, tmp_path / "out")


def test_settings_from_older_versions_load(manager):
    import json

    state = json.loads((manager.data_dir / "state.json").read_text(encoding="utf-8"))
    state["settings"]["pak_chain"] = "re_chunk_000.pak"  # removed setting
    (manager.data_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    reloaded = type(manager)(manager.data_dir)
    assert not hasattr(reloaded.settings, "pak_chain") and reloaded.settings.auto_apply == manager.settings.auto_apply


def test_instance_lock(tmp_path):
    from mhwmm.manager import acquire_instance_lock

    first = acquire_instance_lock(tmp_path)
    assert first is not None
    assert acquire_instance_lock(tmp_path) is None
    first.close()
    second = acquire_instance_lock(tmp_path)
    assert second is not None
    second.close()


def test_options_are_off_until_chosen(manager, game_dir, tmp_path):
    archive = make_zip(tmp_path / "Colors-55-1-0-1740000000.zip", {
        "Colors/Red/natives/STM/a.tex": "red",
        "Colors/Red/natives/STM/red_only.tex": "r",
        "Colors/Blue/natives/STM/a.tex": "blue",
        "Colors/Blue/x.pak": "bluepak",
    })
    with manager.prepare_install(archive) as session:
        mod = manager.install_package(session, session.variants)
    assert mod.name == "Colors" and mod.version == "1.0"
    assert [o["name"] for o in mod.options] == ["Blue", "Red"]
    assert mod.needs_option and manager.mod_files(mod.id) == []
    manager.deploy()
    assert not (game_dir / "natives").exists()

    blue, red = (o["id"] for o in mod.options)
    manager.set_options(mod.id, [red])
    assert manager.dirty
    manager.deploy()
    assert (game_dir / "natives/STM/a.tex").read_text() == "red"
    assert (game_dir / "natives/STM/red_only.tex").exists()
    assert not (game_dir / "pak_mods/001_Colors.pak").exists()

    manager.set_options(mod.id, [blue])
    manager.deploy()
    assert (game_dir / "natives/STM/a.tex").read_text() == "blue"
    assert not (game_dir / "natives/STM/red_only.tex").exists()
    assert (game_dir / "pak_mods/001_Colors.pak").read_text() == "bluepak"

    manager.set_options(mod.id, [red, blue])  # both on: the later option wins shared files
    manager.deploy()
    assert (game_dir / "natives/STM/a.tex").read_text() == "red"
    assert (game_dir / "pak_mods/001_Colors.pak").read_text() == "bluepak"
    assert ModManager(manager.data_dir).get(mod.id).selected == [blue, red]

    # an update keeps the chosen options by name; new options start switched off
    update = make_zip(tmp_path / "v2" / "Colors-55-1-1-1750000000.zip", {
        "Colors/Red/natives/STM/a.tex": "red2",
        "Colors/Blue/natives/STM/a.tex": "blue2",
        "Colors/Green/natives/STM/a.tex": "green",
    })
    with manager.prepare_install(update) as session:
        updated = manager.install_package(session, session.variants, replace_id=mod.id)
    assert updated.version == "1.1"
    names = {o["id"]: o["name"] for o in updated.options}
    assert sorted(names[i] for i in updated.selected) == ["Blue", "Red"]
    manager.deploy()
    assert (game_dir / "natives/STM/a.tex").read_text() == "red2"
    assert not (game_dir / "pak_mods/001_Colors.pak").exists()


def test_options_keep_their_own_pictures(manager, tmp_path):
    archive = make_zip(tmp_path / "Colors.zip", {
        "Colors/Red/natives/STM/a.tex": "red",
        "Colors/Red/preview.png": "red picture",
        "Colors/Blue/natives/STM/a.tex": "blue",
        "Colors/Blue/modinfo.ini": "name=Blue\nscreenshot=shot.JPG\n",
        "Colors/Blue/shot.JPG": "blue picture",
        "Colors/Green/natives/STM/a.tex": "green",
    })
    with manager.prepare_install(archive) as session:
        mod = manager.install_package(session, session.variants)
    pictures = {o["name"]: manager.option_preview(mod.id, o) for o in mod.options}
    assert pictures["Red"].read_text() == "red picture"
    assert pictures["Blue"].read_text() == "blue picture" and pictures["Blue"].suffix == ".jpg"
    assert pictures["Green"] is None
    assert manager.screenshot_path(mod).read_text() == "blue picture"  # the mod's own picture as before
    reloaded = ModManager(manager.data_dir)
    assert [reloaded.option_preview(mod.id, o) for o in reloaded.get(mod.id).options] == list(pictures.values())
    # only pictures in the mod's previews folder
    assert manager.option_preview(mod.id, {"preview": "previews/../mod.json"}) is None
    assert manager.option_preview(mod.id, {"preview": "preview.jpg"}) is None
    assert manager.option_preview(mod.id, {"preview": 3}) is None
    # the picture that shows the mod as it is used: the selected option's, else the mod's
    assert manager.mod_picture(mod.id) == manager.screenshot_path(mod)
    red = next(o["id"] for o in mod.options if o["name"] == "Red")
    manager.set_options(mod.id, [red])
    assert manager.mod_picture(mod.id).read_text() == "red picture"

    # an update brings the pictures of the new archive
    update = make_zip(tmp_path / "v2" / "Colors.zip", {
        "Colors/Red/natives/STM/a.tex": "red2",
        "Colors/Blue/natives/STM/a.tex": "blue2",
        "Colors/Blue/preview.png": "new blue",
    })
    with manager.prepare_install(update) as session:
        mod = manager.install_package(session, session.variants, replace_id=mod.id)
    pictures = {o["name"]: manager.option_preview(mod.id, o) for o in mod.options}
    assert pictures["Blue"].read_text() == "new blue" and pictures["Red"] is None
    assert [f.name for f in (manager.mod_dir(mod.id) / "previews").iterdir()] == ["o1.png"]

    # the same picture in every option is just the mod's picture
    same = make_zip(tmp_path / "Same.zip", {
        "Same/A/natives/STM/a.tex": "a", "Same/A/preview.png": "cover",
        "Same/B/natives/STM/a.tex": "b", "Same/B/preview.png": "cover",
    })
    with manager.prepare_install(same) as session:
        mod = manager.install_package(session, session.variants)
    assert [manager.option_preview(mod.id, o) for o in mod.options] == [None, None]
    assert manager.screenshot_path(mod).read_text() == "cover"
    assert not (manager.mod_dir(mod.id) / "previews").exists()


def test_option_pictures_from_the_archive_of_an_older_install(manager, tmp_path, monkeypatch):
    downloads = tmp_path / "home" / "Downloads"
    archive = make_zip(downloads / "Colors.zip", {
        "Colors/Red/natives/STM/a.tex": "red", "Colors/Red/preview.png": "red picture",
        "Colors/Blue/natives/STM/a.tex": "blue", "Colors/Blue/preview.png": "blue picture",
        "Colors/Green/natives/STM/a.tex": "green",
    })
    with manager.prepare_install(archive) as session:
        mod = manager.install_package(session, session.variants)
    assert mod.pictures_read and manager.settings.archive_dirs == [str(downloads)]
    assert manager.find_archive(mod.id) == archive  # the folder it was installed from

    # as a version before 1.12.0 left it: no option pictures, the archive never looked at
    for opt in mod.options:
        opt.pop("preview", None)
    fsutil.rmtree(manager.mod_dir(mod.id) / "previews")
    mod.pictures_read = False
    manager.settings.archive_dirs = []
    files = manager.mod_files(mod.id)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert manager.find_archive(mod.id) == archive  # still in Downloads
    assert manager.read_option_pictures(mod.id, archive) == 2
    reloaded = ModManager(manager.data_dir).get(mod.id)
    pictures = {o["name"]: manager.option_preview(mod.id, o) for o in reloaded.options}
    assert pictures["Red"].read_text() == "red picture" and pictures["Blue"].read_text() == "blue picture"
    assert pictures["Green"] is None and reloaded.pictures_read
    assert manager.mod_files(mod.id) == files  # the mod's files are not touched

    # options renamed since: matched by position when no name matches
    renamed = make_zip(tmp_path / "Renamed.zip", {
        "C/Blau/natives/STM/a.tex": "b", "C/Blau/preview.png": "blau",
        "C/Gruen/natives/STM/a.tex": "g",
        "C/Rot/natives/STM/a.tex": "r", "C/Rot/preview.png": "rot",
    })
    assert manager.read_option_pictures(mod.id, renamed) == 2
    pictures = [manager.option_preview(mod.id, o) for o in manager.get(mod.id).options]
    assert [p.read_text() if p else None for p in pictures] == ["blau", None, "rot"]
    assert sorted(f.name for f in (manager.mod_dir(mod.id) / "previews").iterdir()) == ["o1.png", "o3.png"]
    # the mod's own picture stays, and no other one is added next to it
    assert manager.screenshot_path(manager.get(mod.id)).read_text() == "blue picture"
    jpgs = make_zip(tmp_path / "Jpg.zip", {"J/Blue/natives/STM/a.tex": "b", "J/Blue/shot.jpg": "jpg blue",
                                           "J/Red/natives/STM/a.tex": "r", "J/Red/shot.jpg": "jpg red"})
    assert manager.read_option_pictures(mod.id, jpgs) == 2
    assert sorted(f.name for f in manager.mod_dir(mod.id).glob("preview.*")) == ["preview.png"]
    # a mod without a picture of its own gets the archive's
    manager.screenshot_path(manager.get(mod.id)).unlink()
    manager.read_option_pictures(mod.id, jpgs)
    assert manager.screenshot_path(manager.get(mod.id)).read_text() == "jpg blue"

    # an archive of something else
    other = make_zip(tmp_path / "Other.zip", {"Other/natives/STM/x.tex": "x", "Other/preview.png": "p"})
    with pytest.raises(InstallError):
        manager.read_option_pictures(mod.id, other)
    with manager.prepare_install(other) as session:
        single = manager.install_package(session, session.variants)
    with pytest.raises(InstallError):
        manager.read_option_pictures(single.id, other)
    assert manager.find_archive(single.id) == other
    single.source = "../escape.zip"
    assert manager.find_archive(single.id) is None

    # only the user's own folders are remembered: not the app's (a REFramework download), not a relative one
    remembered = list(manager.settings.archive_dirs)
    refw_zip = make_zip(manager.temp_root / "REFramework-1.zip", {"Other2/natives/STM/y.tex": "y"})
    with manager.prepare_install(refw_zip) as session:
        manager.install_package(session, session.variants)
    monkeypatch.chdir(tmp_path)
    with manager.prepare_install(Path("Other.zip")) as session:
        manager.install_package(session, session.variants)
    assert manager.settings.archive_dirs == remembered
    manager.settings.archive_dirs = [".", str(manager.temp_root), 3, *remembered]
    assert manager.archive_folders()[:len(remembered)] == [Path(d) for d in remembered]


@pytest.fixture
def frozen_exe(tmp_path, monkeypatch):
    """Pretend to run as the packaged exe in tmp_path/app, with the old data folder in tmp_path/legacy."""
    from mhwmm import manager as mgr

    app = tmp_path / "app"
    write(app / "MHWildsModManager.exe", "exe")
    monkeypatch.setattr(mgr.sys, "frozen", True, raising=False)
    monkeypatch.setattr(mgr.sys, "executable", str(app / "MHWildsModManager.exe"))
    monkeypatch.setattr(mgr, "user_data_dir", lambda: tmp_path / "legacy")
    monkeypatch.delenv("MHWMM_DATA_DIR", raising=False)
    return app


def test_data_dir_next_to_exe(frozen_exe, tmp_path, monkeypatch):
    from mhwmm import manager as mgr

    assert mgr.resolve_data_dir() == (frozen_exe / "MHWildsModManager_data", None)
    write(tmp_path / "legacy" / "state.json", "{}")
    assert mgr.resolve_data_dir() == (frozen_exe / "MHWildsModManager_data", tmp_path / "legacy")
    (frozen_exe / "MHWildsModManager_data").mkdir()
    assert mgr.resolve_data_dir() == (frozen_exe / "MHWildsModManager_data", None)
    (frozen_exe / "MHWildsModManager_data").rmdir()
    monkeypatch.setattr(mgr, "_dir_writable", lambda path: False)  # e.g. C:\Program Files
    assert mgr.resolve_data_dir() == (tmp_path / "legacy", None)
    monkeypatch.setenv("MHWMM_DATA_DIR", str(tmp_path / "custom"))
    assert mgr.resolve_data_dir() == (tmp_path / "custom", None)


@pytest.mark.parametrize("same_drive", [True, False])
def test_old_data_is_moved_next_to_exe(frozen_exe, tmp_path, monkeypatch, same_drive):
    from mhwmm import manager as mgr

    legacy = tmp_path / "legacy"
    old = ModManager(legacy)
    with old.prepare_install(make_zip(tmp_path / "A.zip", {"natives/x.tex": "a"})) as session:
        mod = old.install_variant(session, session.variants[0])
    stored = old.files_dir(mod.id) / "natives/x.tex"
    mtime = stored.stat().st_mtime_ns
    if not same_drive:
        real = mgr._rename_dir
        monkeypatch.setattr(mgr, "_rename_dir",
                            lambda src, dst: (_ for _ in ()).throw(OSError("other drive")) if src == legacy else real(src, dst))
    seen = []
    path, problem = mgr.prepare_data_dir(progress=lambda done, total, msg: seen.append(done))
    target = frozen_exe / "MHWildsModManager_data"
    assert (path, problem) == (target, None)
    assert not legacy.exists()
    moved = ModManager(target)
    assert [m.name for m in moved.mods] == ["A"]
    assert (moved.files_dir(mod.id) / "natives/x.tex").stat().st_mtime_ns == mtime  # no redeploy needed
    assert bool(seen) is not same_drive


def test_failed_move_keeps_old_data(frozen_exe, tmp_path, monkeypatch):
    from mhwmm import manager as mgr

    legacy = tmp_path / "legacy"
    write(legacy / "state.json", "{}")
    write(legacy / "mods" / "m1" / "files" / "a.pak", "p")
    monkeypatch.setattr(mgr, "_rename_dir", lambda src, dst: (_ for _ in ()).throw(OSError("other drive")))
    monkeypatch.setattr(mgr.fsutil, "copy_file", lambda src, dst: (_ for _ in ()).throw(OSError("disk full")))
    path, problem = mgr.prepare_data_dir()
    assert path == legacy and "disk full" in problem
    assert (legacy / "mods" / "m1" / "files" / "a.pak").exists()
    assert not (frozen_exe / "MHWildsModManager_data.partial").exists()


def test_profiles_remember_enabled_options_and_order(manager, game_dir, tmp_path):
    (a,) = install(manager, make_zip(tmp_path / "A.zip", {"natives/STM/a.tex": "a"}))
    (b,) = install(manager, make_zip(tmp_path / "B.zip", {"natives/STM/a.tex": "b"}))
    with manager.prepare_install(make_zip(tmp_path / "Opts.zip", {
            "Opts/One/natives/STM/o.tex": "1", "Opts/Two/natives/STM/o.tex": "2"})) as s:
        opts = manager.install_package(s, s.variants, None, [0])
    manager.deploy()
    assert manager.profile == "기본" and list(manager.profiles) == ["기본"]
    assert (game_dir / "natives/STM/a.tex").read_text() == "b"

    manager.create_profile("사진용")  # copy of the current setup
    manager.move_to(b.id, 0)  # now A wins
    manager.set_enabled(opts.id, True)
    manager.set_options(opts.id, [opts.options[1]["id"]])
    manager.deploy()
    assert (game_dir / "natives/STM/a.tex").read_text() == "a"
    assert (game_dir / "natives/STM/o.tex").read_text() == "2"

    manager.switch_profile("기본")
    assert [m.id for m in manager.mods] == [a.id, b.id, opts.id]
    assert manager.get(opts.id).selected == [opts.options[0]["id"]]
    manager.deploy()
    assert (game_dir / "natives/STM/a.tex").read_text() == "b"
    assert (game_dir / "natives/STM/o.tex").read_text() == "1"

    # a mod installed now is off (and last) in the other profile
    (c,) = install(manager, make_zip(tmp_path / "C.zip", {"natives/STM/c.tex": "c"}))
    assert c.enabled
    manager.switch_profile("사진용")
    assert not manager.get(c.id).enabled and manager.mods[-1].id == c.id

    reloaded = ModManager(manager.data_dir)
    assert reloaded.profile == "사진용" and list(reloaded.profiles) == ["기본", "사진용"]
    assert [m.id for m in reloaded.mods][:2] == [b.id, a.id]


def test_profile_management_rules(manager, tmp_path):
    (a,) = install(manager, make_zip(tmp_path / "A.zip", {"natives/a.tex": "a"}))
    manager.create_profile("빈 프로필", copy_current=False)
    assert not manager.get(a.id).enabled
    with pytest.raises(ValueError):
        manager.create_profile("빈 프로필")
    with pytest.raises(ValueError):
        manager.create_profile("   ")
    manager.rename_profile("빈 프로필", "테스트")
    assert manager.profile == "테스트"
    manager.delete_profile("테스트")  # deleting the active profile switches back
    assert manager.profile == "기본" and manager.get(a.id).enabled
    with pytest.raises(ValueError):
        manager.delete_profile("기본")


def test_settings_type_checks(manager):
    from mhwmm.manager import Settings, _from_dict

    s = _from_dict(Settings, {"sash_ratio": 1, "remember_window": "yes", "window_geometry": 5})
    assert s.sash_ratio == 1.0 and s.remember_window is True and s.window_geometry == ""
