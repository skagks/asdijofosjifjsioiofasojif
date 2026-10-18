import zipfile

import pytest

from mhwmm.archive import extract, ArchiveError
from mhwmm.manager import ModManager

from conftest import make_tree, make_zip, write

CHAIN = "re_chunk_000.pak.sub_000.pak"


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
    assert (game_dir / f"{CHAIN}.patch_002.pak").read_text() == "P"
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


def test_settings_chain_validation(manager):
    with pytest.raises(ValueError):
        manager.update_settings(pak_chain="nonsense.pak")
    manager.dirty = False
    manager.update_settings(pak_chain="re_chunk_000.pak")
    assert manager.dirty


def test_instance_lock(tmp_path):
    from mhwmm.manager import acquire_instance_lock

    first = acquire_instance_lock(tmp_path)
    assert first is not None
    assert acquire_instance_lock(tmp_path) is None
    first.close()
    second = acquire_instance_lock(tmp_path)
    assert second is not None
    second.close()
