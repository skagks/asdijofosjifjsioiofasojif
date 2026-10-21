from pathlib import Path

from mhwmm.deploy import Deployer, ModSource
from mhwmm.analyzer import list_files

from conftest import make_tree, touch_later, write

CHAIN = "re_chunk_000.pak.sub_000.pak"


def patch(n: int) -> str:
    return f"{CHAIN}.patch_{n:03d}.pak"


def mod(tmp_path: Path, mod_id: str, files: dict) -> ModSource:
    d = make_tree(tmp_path / "mods" / mod_id, files)
    return ModSource(mod_id, mod_id, d, list_files(d))


def test_loose_override_and_removal(tmp_path, game_dir):
    a = mod(tmp_path, "a", {"natives/STM/x.tex": "A", "natives/STM/only_a.tex": "A"})
    b = mod(tmp_path, "b", {"natives/STM/x.tex": "B"})
    d = Deployer(game_dir, tmp_path / "data")

    r = d.run([a, b])
    assert not r.errors
    assert (game_dir / "natives/STM/x.tex").read_text() == "B"
    assert (game_dir / "natives/STM/only_a.tex").read_text() == "A"

    r = d.run([b, a])  # reorder: a wins now
    assert (game_dir / "natives/STM/x.tex").read_text() == "A"
    assert r.copied == 1 and r.kept == 1

    d.run([])
    assert not (game_dir / "natives").exists()  # empty folders are cleaned up
    assert (game_dir / patch(1)).read_text() == "official-1"


def test_paks_go_to_pak_mods_in_load_order(tmp_path, game_dir):
    write(game_dir / patch(2), "official-2")
    a = mod(tmp_path, "a", {"cool.pak": "A"})
    b = mod(tmp_path, "b", {"re_chunk_000.pak.sub_000.pak.patch_009.pak": "B", "b2.pak": "B2"})
    d = Deployer(game_dir, tmp_path / "data")

    r = d.run([a, b])
    assert [t for t, _, _ in r.paks] == ["pak_mods/001_a.pak", "pak_mods/002_b_re_chunk_000_pak_sub_000_pak_patch_009.pak",
                                         "pak_mods/003_b_b2.pak"]
    assert (game_dir / "pak_mods/001_a.pak").read_text() == "A"
    assert (game_dir / "pak_mods/003_b_b2.pak").read_text() == "B2"
    assert sorted(p.name for p in game_dir.glob("*.patch_*.pak")) == [patch(1), patch(2)]  # the chain is the game's

    r = d.run([b, a])  # the last mod gets the highest number (wins); reordering only renames
    assert (game_dir / "pak_mods/003_a.pak").read_text() == "A"
    assert r.renamed == 3 and r.copied == 0

    r = d.run([a])
    assert [p.name for p in (game_dir / "pak_mods").iterdir()] == ["001_a.pak"]
    d.run([])
    assert not (game_dir / "pak_mods").exists()
    assert (game_dir / patch(2)).read_text() == "official-2"


def test_other_files_in_pak_mods_are_left_alone(tmp_path, game_dir):
    write(game_dir / "pak_mods/someone_else.pak", "other tool")
    a = mod(tmp_path, "a", {"a.pak": "A"})
    d = Deployer(game_dir, tmp_path / "data")
    d.run([a])
    d.run([])
    assert (game_dir / "pak_mods/someone_else.pak").read_text() == "other tool"


def _old_chain_deployment(tmp_path, game_dir, d, mods_and_numbers):
    """The manifest an older version left after renaming PAKs into the patch chain."""
    import json

    files = {}
    for source, number in mods_and_numbers:
        rel = patch(number)
        src_path, key = source.source(source.files[0])
        write(game_dir / rel, src_path.read_text())
        st, src_st = (game_dir / rel).stat(), src_path.stat()
        files[rel] = {"path": rel, "mod": source.mod_id, "src": key, "src_size": src_st.st_size,
                      "src_mtime": src_st.st_mtime_ns, "size": st.st_size, "mtime": st.st_mtime_ns, "pak": True}
    write(d.manifest_path, json.dumps({"version": 1, "game_dir": str(game_dir), "files": files, "backups": {}}))


def test_paks_from_the_patch_chain_move_to_pak_mods(tmp_path, game_dir):
    a = mod(tmp_path, "a", {"Chen_Tex.pak": "tex"})
    b = mod(tmp_path, "b", {"b.pak": "B"})
    d = Deployer(game_dir, tmp_path / "data")
    _old_chain_deployment(tmp_path, game_dir, d, [(a, 16), (b, 17)])
    assert d.outdated_paks() == [patch(16), patch(17)]
    assert any("pak_mods" in p for p in d.verify()) and d.verify(include_outdated=False) == []

    r = d.run([a, b])
    assert (game_dir / "pak_mods/001_a.pak").read_text() == "tex"
    assert (game_dir / "pak_mods/002_b.pak").read_text() == "B"
    assert not (game_dir / patch(16)).exists() and not (game_dir / patch(17)).exists()
    assert r.renamed == 2 and r.copied == 0 and d.outdated_paks() == [] and d.verify() == []


def test_chain_pak_overwritten_by_game_update_is_kept(tmp_path, game_dir):
    a = mod(tmp_path, "a", {"a.pak": "A"})
    d = Deployer(game_dir, tmp_path / "data")
    _old_chain_deployment(tmp_path, game_dir, d, [(a, 2)])
    touch_later(game_dir / patch(2), "official-2")  # a game update shipped its own patch_002
    assert any("patch_002" in p for p in d.verify())

    r = d.run([a])
    assert r.warnings
    assert (game_dir / patch(2)).read_text() == "official-2"  # never deleted
    assert (game_dir / "pak_mods/001_a.pak").read_text() == "A"
    d.run([])
    assert (game_dir / patch(2)).read_text() == "official-2"


def test_backup_and_restore_of_game_files(tmp_path, game_dir):
    write(game_dir / "dinput8.dll", "user's refw")
    a = mod(tmp_path, "a", {"dinput8.dll": "mod refw"})
    d = Deployer(game_dir, tmp_path / "data")
    r = d.run([a])
    assert r.backed_up == 1
    assert (game_dir / "dinput8.dll").read_text() == "mod refw"
    r = d.run([])
    assert r.restored == 1
    assert (game_dir / "dinput8.dll").read_text() == "user's refw"


def test_updated_game_file_is_not_replaced_by_old_backup(tmp_path, game_dir):
    write(game_dir / "amd_fidelityfx_dx12.dll", "v1")
    a = mod(tmp_path, "a", {"amd_fidelityfx_dx12.dll": "mod"})
    d = Deployer(game_dir, tmp_path / "data")
    d.run([a])
    touch_later(game_dir / "amd_fidelityfx_dx12.dll", "v2 from update")
    d.run([])
    assert (game_dir / "amd_fidelityfx_dx12.dll").read_text() == "v2 from update"


def test_user_modified_config_is_kept(tmp_path, game_dir):
    a = mod(tmp_path, "a", {"reframework/data/a/config.json": "default"})
    d = Deployer(game_dir, tmp_path / "data")
    d.run([a])
    cfg = game_dir / "reframework/data/a/config.json"
    touch_later(cfg, "user settings")
    r = d.run([a])
    assert cfg.read_text() == "user settings"
    assert r.copied == 0
    d.run([])
    assert not cfg.exists()
    # kept aside while the mod is off, and back when it is on again (still the user's after that)
    assert (tmp_path / "data/edited/reframework/data/a/config.json").read_text() == "user settings"
    d.run([a])
    assert cfg.read_text() == "user settings" and not (tmp_path / "data/edited").exists()
    d.run([])
    d.run([a])
    assert cfg.read_text() == "user settings"
    # an update of the mod brings its new copy
    d.run([])
    touch_later(a.files_dir / "reframework/data/a/config.json", "default 2")
    d.run([a])
    assert cfg.read_text() == "default 2" and not (tmp_path / "data/edited").exists()


def test_an_edited_file_under_another_mod_s_and_a_removed_mod(tmp_path, game_dir):
    a = mod(tmp_path, "a", {"reframework/data/shared.ini": "a", "natives/STM/a.tex": "a"})
    b = mod(tmp_path, "b", {"reframework/data/shared.ini": "b"})
    d = Deployer(game_dir, tmp_path / "data")
    d.run([a])
    ini = game_dir / "reframework/data/shared.ini"
    touch_later(ini, "a, edited")
    d.run([a, b])  # b's copy wins now: a's edited one is kept aside, not lost
    assert ini.read_text() == "b"
    d.run([a])
    assert ini.read_text() == "a, edited"
    d.run([], installed={"b"})  # (a is uninstalled: nothing of it is kept)
    assert not ini.exists() and not (tmp_path / "data/edited").exists()
    d.run([a])
    assert ini.read_text() == "a"


def test_externally_deleted_file_is_restored(tmp_path, game_dir):
    a = mod(tmp_path, "a", {"natives/STM/x.tex": "A"})
    d = Deployer(game_dir, tmp_path / "data")
    d.run([a])
    (game_dir / "natives/STM/x.tex").unlink()
    r = d.run([a])
    assert r.copied == 1
    assert (game_dir / "natives/STM/x.tex").read_text() == "A"


def test_changed_mod_source_is_recopied(tmp_path, game_dir):
    a = mod(tmp_path, "a", {"natives/STM/x.tex": "A1", "a.pak": "P1"})
    d = Deployer(game_dir, tmp_path / "data")
    d.run([a])
    touch_later(a.files_dir / "natives/STM/x.tex", "A2")
    touch_later(a.files_dir / "a.pak", "P2")
    r = d.run([a])
    assert r.copied == 2
    assert (game_dir / "natives/STM/x.tex").read_text() == "A2"
    assert (game_dir / "pak_mods/001_a.pak").read_text() == "P2"


def test_unrelated_files_are_left_alone(tmp_path, game_dir):
    write(game_dir / "natives/STM/manual.tex", "manual")
    a = mod(tmp_path, "a", {"natives/STM/x.tex": "A"})
    d = Deployer(game_dir, tmp_path / "data")
    d.run([a])
    d.run([])
    assert (game_dir / "natives/STM/manual.tex").read_text() == "manual"
    assert not (game_dir / "natives/STM/x.tex").exists()


def test_refuses_other_game_dir(tmp_path, game_dir):
    import pytest
    from mhwmm.deploy import DeployError

    a = mod(tmp_path, "a", {"natives/x.tex": "A"})
    Deployer(game_dir, tmp_path / "data").run([a])
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(DeployError):
        Deployer(other, tmp_path / "data").run([a])

