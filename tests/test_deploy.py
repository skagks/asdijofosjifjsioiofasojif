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


def test_pak_numbering_after_official_patches(tmp_path, game_dir):
    write(game_dir / patch(2), "official-2")
    a = mod(tmp_path, "a", {"cool.pak": "A"})
    b = mod(tmp_path, "b", {"re_chunk_000.pak.sub_000.pak.patch_009.pak": "B"})
    d = Deployer(game_dir, tmp_path / "data")

    r = d.run([a, b])
    assert (game_dir / patch(3)).read_text() == "A"
    assert (game_dir / patch(4)).read_text() == "B"
    assert [t for t, _, _ in r.paks] == [patch(3), patch(4)]

    r = d.run([b, a])  # swapping the order only renames
    assert (game_dir / patch(3)).read_text() == "B"
    assert (game_dir / patch(4)).read_text() == "A"
    assert r.renamed == 2 and r.copied == 0

    r = d.run([a])  # removing one keeps the chain contiguous
    assert (game_dir / patch(3)).read_text() == "A"
    assert not (game_dir / patch(4)).exists()
    assert (game_dir / patch(2)).read_text() == "official-2"


def test_game_update_overwrites_mod_pak(tmp_path, game_dir):
    a = mod(tmp_path, "a", {"a.pak": "A"})
    b = mod(tmp_path, "b", {"b.pak": "B"})
    d = Deployer(game_dir, tmp_path / "data")
    d.run([a, b])
    assert (game_dir / patch(2)).read_text() == "A"

    # A game update ships its own patch_002 over mod A.
    touch_later(game_dir / patch(2), "official-2")
    assert any("patch_002" in p for p in d.verify())

    r = d.run([a, b])
    assert r.warnings
    assert (game_dir / patch(2)).read_text() == "official-2"  # never deleted
    assert (game_dir / patch(3)).read_text() == "A"  # load order is preserved
    assert (game_dir / patch(4)).read_text() == "B"
    assert r.copied == 1 and r.renamed == 1

    d.run([])
    assert (game_dir / patch(1)).read_text() == "official-1"
    assert (game_dir / patch(2)).read_text() == "official-2"
    assert not (game_dir / patch(3)).exists()
    assert not (game_dir / patch(4)).exists()


def test_foreign_paks_are_skipped(tmp_path, game_dir):
    write(game_dir / patch(3), "other tool")  # gap at 2, foreign file at 3
    a = mod(tmp_path, "a", {"a.pak": "A"})
    b = mod(tmp_path, "b", {"b.pak": "B"})
    d = Deployer(game_dir, tmp_path / "data")
    d.run([a, b])
    assert (game_dir / patch(2)).read_text() == "A"
    assert (game_dir / patch(3)).read_text() == "other tool"
    assert (game_dir / patch(4)).read_text() == "B"
    d.run([])
    assert (game_dir / patch(3)).read_text() == "other tool"


def test_other_chain_from_filename(tmp_path, game_dir):
    a = mod(tmp_path, "a", {"re_chunk_000.pak.patch_005.pak": "A"})
    d = Deployer(game_dir, tmp_path / "data")
    d.run([a])
    assert (game_dir / "re_chunk_000.pak.patch_001.pak").read_text() == "A"


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
    assert (game_dir / patch(2)).read_text() == "P2"


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


def test_gap_left_by_another_tool_is_found_and_closed(tmp_path, game_dir):
    # Another mod manager (e.g. Fluffy) had PAKs at 2 and 3, so ours went to 4.
    write(game_dir / patch(2), "fluffy 1")
    write(game_dir / patch(3), "fluffy 2")
    a = mod(tmp_path, "a", {"Chen_Tex.pak": "tex"})
    d = Deployer(game_dir, tmp_path / "data")
    d.run([a])
    assert (game_dir / patch(4)).read_text() == "tex"
    assert d.verify() == [] and d.pak_gaps() == []

    # Removing those leaves a gap: the game stops at 2 and never reads our PAK.
    (game_dir / patch(2)).unlink()
    (game_dir / patch(3)).unlink()
    gaps = d.pak_gaps()
    assert [(g.chain, g.missing, g.unread) for g in gaps] == [(CHAIN, [2, 3], [patch(4)])]
    problems = d.verify()
    assert len(problems) == 1 and patch(2) in problems[0] and patch(4) in problems[0]

    d.run([a])  # applying again moves our PAK down into the gap
    assert (game_dir / patch(2)).read_text() == "tex" and not (game_dir / patch(4)).exists()
    assert d.verify() == [] and d.pak_gaps() == []


def test_gap_only_below_foreign_paks_is_not_ours(tmp_path, game_dir):
    a = mod(tmp_path, "a", {"a.pak": "A"})
    d = Deployer(game_dir, tmp_path / "data")
    d.run([a])  # ours at 2
    write(game_dir / patch(9), "other tool")  # someone else's PAK after a gap
    assert d.pak_gaps() == []
