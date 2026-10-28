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


def kept_files(tmp_path: Path) -> dict:
    """What is kept aside in edited\\ of the data folder: {path there: text}."""
    base = tmp_path / "data/edited"
    return {p.relative_to(base).as_posix(): p.read_text() for p in sorted(base.rglob("*")) if p.is_file()} \
        if base.is_dir() else {}


def test_a_copy_changed_before_a_mod_edit_is_placed_is_kept(tmp_path, game_dir):
    """모드 수정 checks the game copy when it saves (I11), but the deploy may come later (the game was running, or
    nothing is applied at once) and a script may save its settings in between: the copy is kept aside with a
    warning instead of being deleted, and it comes back with the mod's own file (#12). A copy kept of an edit's
    output never comes back by itself, but it is the user's: neither another edit nor an update deletes it."""
    rel = "reframework/data/a/config.json"
    a = mod(tmp_path, "a", {rel: "default"})

    def edited(text: str) -> ModSource:  # what mod_entries gives for an edit's output
        out = write(tmp_path / "mods/a-edits" / f"{text}.json", text)
        return ModSource("a", "a", a.files_dir, a.files, {rel: (out, "edits/" + rel)})

    d = Deployer(game_dir, tmp_path / "data")
    d.run([a])
    cfg, kept = game_dir / rel, tmp_path / "data/edited" / rel
    touch_later(cfg, "user settings")  # saved by the script after the edit's check
    r = d.run([edited("edit 1")])
    assert cfg.read_text() == "edit 1" and kept.read_text() == "user settings" and not r.errors
    assert r.warnings == [f"게임 폴더에서 바뀐 {rel}을(를) 따로 보관하고 모드 수정에 맞는 파일을 넣었습니다 (전에 넣었던 "
                          f"파일이 다시 들어가면 보관한 파일로 되돌립니다). 보관한 파일: {kept}"]
    r = d.run([edited("edit 2")])  # another edit on top of the mod's file: the kept copy stays for that file
    assert cfg.read_text() == "edit 2" and kept.read_text() == "user settings" and r.warnings == []
    d.run([a])  # 원래대로: the mod's own file, so the user's copy is back
    assert cfg.read_text() == "user settings" and not (tmp_path / "data/edited").exists()
    assert d.placed("a", [rel]) == {rel: "changed"}

    # the other way: the edit's output changed in the game, then 원래대로 before it was applied
    d.run([edited("edit 3")])  # (the user's copy goes aside again)
    touch_later(cfg, "edit 3, user settings")
    r = d.run([a])  # the user's copy of the mod's file comes back, the changed output goes aside beside it
    assert cfg.read_text() == "user settings" and not r.errors
    assert kept_files(tmp_path) == {rel + ".2": "edit 3, user settings"}
    assert r.warnings == [f"게임 폴더에서 바뀐 {rel}을(를) 따로 보관하고 모드의 원래 파일을 넣었습니다. 보관한 파일: "
                          f"{tmp_path / 'data/edited' / (rel + '.2')}"]
    r = d.run([edited("edit 4")])  # a later edit keeps it (and the user's copy of the mod's file beside it)
    assert cfg.read_text() == "edit 4" and len(r.warnings) == 1
    assert kept_files(tmp_path) == {rel: "user settings", rel + ".2": "edit 3, user settings"}
    d.run([a])
    assert cfg.read_text() == "user settings" and kept_files(tmp_path) == {rel + ".2": "edit 3, user settings"}
    # an update of the mod brings its new copy instead of the user's copy of its old one, as before
    touch_later(cfg, "user settings again")
    touch_later(a.files_dir / rel, "default 2")
    r = d.run([a])
    assert cfg.read_text() == "default 2" and r.warnings == []
    assert kept_files(tmp_path) == {rel + ".2": "edit 3, user settings"}
    d.run([], installed=set())  # (the mod is uninstalled: nothing of it is kept)
    assert not (tmp_path / "data/edited").exists() and d.load_manifest()["edited"] == {}


def option(tmp_path: Path, mod_id: str, oid: str, files: dict) -> ModSource:
    """What mod_entries gives for a mod with options: one option's files, under source keys of that option."""
    d = make_tree(tmp_path / "mods" / mod_id / "options" / oid, files)
    return ModSource(mod_id, mod_id, d, list_files(d), {rel: (d / rel, f"{oid}/{rel}") for rel in list_files(d)})


def test_another_option_s_copy_keeps_the_changed_copy_aside(tmp_path, game_dir):
    """Another option's copy of a path taking the place of the one the user changed (the 옵션 tab, 쓸 옵션, 모두
    원래대로) keeps the changed copy aside with a warning, as another mod's file does; it comes back with its
    option's file. Each file of the mod has its own kept copy there (#0)."""
    rel = "reframework/autorun/Presets/config.lua"
    a = option(tmp_path, "p", "o1", {rel: "Scale = 1.0"})
    b = option(tmp_path, "p", "o2", {rel: "Scale = 2.0"})
    d = Deployer(game_dir, tmp_path / "data")
    lua = game_dir / rel
    d.run([b])
    touch_later(lua, "Scale = 7.5 -- B, mine")
    r = d.run([a])
    assert lua.read_text() == "Scale = 1.0" and kept_files(tmp_path) == {rel: "Scale = 7.5 -- B, mine"}
    assert r.warnings == [f"게임 폴더에서 바뀐 {rel}을(를) 따로 보관하고 모드의 다른 파일을 넣었습니다 (전에 넣었던 파일이 "
                          f"다시 들어가면 보관한 파일로 되돌립니다). 보관한 파일: {tmp_path / 'data/edited' / rel}"]
    assert d.placed("p", [rel]) == {rel: "ok"}
    touch_later(lua, "Scale = 3.0 -- A, mine")  # the script saves A's copy too
    r = d.run([b])  # B's copy is back, A's goes aside beside it
    assert lua.read_text() == "Scale = 7.5 -- B, mine" and len(r.warnings) == 1 and not r.errors
    assert kept_files(tmp_path) == {rel + ".2": "Scale = 3.0 -- A, mine"}
    d.run([])  # the mod goes off: both are kept
    assert kept_files(tmp_path) == {rel: "Scale = 7.5 -- B, mine", rel + ".2": "Scale = 3.0 -- A, mine"}
    assert d.placed("p", [rel]) == {rel: "stash"}
    d.run([a])
    assert lua.read_text() == "Scale = 3.0 -- A, mine" and kept_files(tmp_path) == {rel: "Scale = 7.5 -- B, mine"}
    # an update of A's file replaces the user's copy of it, but not the one of B's file
    touch_later(a.files_dir / rel, "Scale = 1.5")
    r = d.run([a])
    assert lua.read_text() == "Scale = 1.5" and kept_files(tmp_path) == {rel: "Scale = 7.5 -- B, mine"}
    assert r.warnings == []
    touch_later(b.files_dir / rel, "Scale = 2.5")  # ... and an update of B's drops what was kept of it
    d.run([b])
    assert lua.read_text() == "Scale = 2.5" and kept_files(tmp_path) == {}


def test_kept_copies_from_another_mod_and_an_older_manifest(tmp_path, game_dir):
    """A copy kept for another mod's file and one kept for another file of the same mod stay side by side, and
    each comes back with its own file; a manifest of an older version (one kept copy per path) still reads."""
    rel = "reframework/data/shared.ini"
    a, b = option(tmp_path, "a", "o1", {rel: "a1"}), option(tmp_path, "a", "o2", {rel: "a2"})
    c = mod(tmp_path, "c", {rel: "c"})
    d = Deployer(game_dir, tmp_path / "data")
    ini = game_dir / rel
    d.run([a])
    touch_later(ini, "a1, mine")
    d.run([a, c])  # another mod's file: kept aside, no warning (as before)
    d.run([b])
    touch_later(ini, "a2, mine")
    r = d.run([c])
    assert ini.read_text() == "c" and r.warnings == []
    assert sorted(kept_files(tmp_path).values()) == ["a1, mine", "a2, mine"]
    d.run([a])
    assert ini.read_text() == "a1, mine"
    d.run([b])
    assert ini.read_text() == "a2, mine"
    # (an older manager's manifest: one kept copy per path, no "more")
    d.run([])
    manifest = d.load_manifest()
    entry = manifest["edited"][rel]
    assert "more" in entry and entry["src"] == f"o2/{rel}"
    del entry["more"]
    d._save(manifest)
    d.run([b])
    assert ini.read_text() == "a2, mine"


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



def test_placed_states_of_a_mod_s_game_copies(tmp_path, game_dir):
    """모드 수정 reads these (Deployer.placed) before changing a loose file whose game copy the user changed."""
    a = mod(tmp_path, "a", {"reframework/autorun/a.lua": "a", "reframework/data/a.json": "{}",
                            "natives/STM/x.tex": "A", "a.pak": "PAK", "reframework/b.ini": "x=1",
                            "natives/STM/gone.tex": "g"})
    b = mod(tmp_path, "b", {"natives/STM/x.tex": "B"})
    d = Deployer(game_dir, tmp_path / "data")
    rels = ["reframework/autorun/a.lua", "reframework/data/a.json", "natives/STM/x.tex", "a.pak", "reframework/b.ini",
            "natives/STM/gone.tex", "natives/STM/never.tex"]
    assert set(d.placed("a", rels).values()) == {"none"}  # nothing placed yet
    d.run([a, b])
    touch_later(game_dir / "reframework/data/a.json", '{"saved": 1}')  # a script saved its settings
    (game_dir / "natives/STM/gone.tex").unlink()
    assert d.placed("a", rels) == {
        "reframework/autorun/a.lua": "ok", "reframework/data/a.json": "changed", "natives/STM/x.tex": "other",
        "a.pak": "none", "reframework/b.ini": "ok", "natives/STM/gone.tex": "missing", "natives/STM/never.tex": "none"}
    assert d.placed("b", ["natives/STM/x.tex", "REFRAMEWORK/AUTORUN/A.LUA"]) == {
        "natives/STM/x.tex": "ok", "REFRAMEWORK/AUTORUN/A.LUA": "other"}

    touch_later(game_dir / "reframework/b.ini", "x=2")
    d.run([b])  # a goes off: its changed copies are kept aside in edited\
    assert d.placed("a", ["reframework/data/a.json", "reframework/b.ini", "reframework/autorun/a.lua"]) == {
        "reframework/data/a.json": "stash", "reframework/b.ini": "stash", "reframework/autorun/a.lua": "none"}
    assert d.placed("b", ["reframework/b.ini"]) == {"reframework/b.ini": "none"}  # (another mod's kept copy)

    d.run([a, b])  # back with the same files: the kept copy is put back and the entry says it is not the mod's
    assert (game_dir / "reframework/b.ini").read_text() == "x=2"
    assert d.placed("a", ["reframework/b.ini"]) == {"reframework/b.ini": "changed"}
    manifest = d.load_manifest()
    assert manifest["files"]["reframework/b.ini"]["edited"] is True
