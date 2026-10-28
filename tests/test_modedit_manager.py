"""모드 수정 in the manager (Part C): the overlay in mod_entries and every reader beside it, the stamps,
matfix, saving (edit_job → build_edit → finish_edit), the game copies a save must not lose (I11), the
carry-over of updates and re-applying after the base changed. Synthetic files only (tests/modedit_fixtures
and a made-up type registry)."""

from __future__ import annotations

import hashlib
import json
import uuid
import weakref
from pathlib import Path

import pytest

import modedit_fixtures as mf
import test_matfix as M
import test_rsz as R
from conftest import make_tree, make_zip, touch_later, write
from mhwmm import fsutil, matfix
from mhwmm.constants import GAME_EXE
from mhwmm.manager import ModManager
from mhwmm.matfix import rsz
from mhwmm.matfix.pak import PakFile, path_hash
from mhwmm.matfix.repair import check_mod
from mhwmm.modedit import EDITS_RECORD, EditError, GameCopyChanged
from mhwmm.modedit import build as edit_build
from mhwmm.modedit import model as em
from mhwmm.modedit import rszdata, textconfig

ITEM = "natives/STM/GameDesign/Common/Item/ItemData.user.3"
AREA = "natives/STM/GameDesign/Common/Area/AreaTable.user.3"
TEX = "natives/STM/Art/Model/x/x_ALBD.tex.241106027"
LUA = "reframework/autorun/Items/config.lua"
SELL = "_Values[_ItemId={}]._SellPrice"


@pytest.fixture(autouse=True)
def types(monkeypatch):
    """The made-up registry of modedit_fixtures for every file opened here (matfix's check included)."""
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", mf.registry())
    monkeypatch.setattr(rsz.TypeRegistry, "_from_file", False)
    monkeypatch.setattr(rsz.TypeRegistry, "_holds", 0)
    monkeypatch.setattr(rszdata, "_CHECKED", weakref.WeakKeyDictionary())


@pytest.fixture
def mgame(tmp_path):
    """A game whose pak has the game's own ItemData (every _SellPrice 10, one row more than the mods')."""
    if not matfix.available():
        pytest.skip("게임 pak을 읽을 zstd 모듈이 없습니다")
    root = tmp_path / "MonsterHunterWilds"
    write(root / GAME_EXE, "exe")
    M.write_game_pak(root / "re_chunk_000.pak", {ITEM: mf.item_data(rows=[mf.item_row(i, _SellPrice=10) for i in range(4)]),
                                                 AREA: mf.area_table()})
    return root


@pytest.fixture
def mlib(tmp_path, mgame):
    m = ModManager(tmp_path / "data")
    m.settings.check_game_running = False
    m.set_game_dir(mgame)
    return m


# ---- helpers ---------------------------------------------------------------------------------------------
def install(m: ModManager, tmp_path: Path, name: str, files: dict, replace=None):
    """A mod from a folder (no options)."""
    src = make_tree(tmp_path / "src" / uuid.uuid4().hex[:8] / name, files)
    with m.prepare_install(src) as session:
        return m.install_package(session, session.variants, replace_id=replace)


def install_options(m: ModManager, tmp_path: Path, name: str, options: dict, replace=None, selected=None):
    """A mod with options ({option name: {game path: data}}), from a zip."""
    files = {f"{name}/{opt}/{rel}": data for opt, tree in options.items() for rel, data in tree.items()}
    archive = make_zip(tmp_path / "zips" / uuid.uuid4().hex[:8] / f"{name}.zip", files)
    with m.prepare_install(archive) as session:
        return m.install_package(session, session.variants, replace_id=replace, selected=selected)


def option_id(m: ModManager, mod_id: str, name: str) -> str:
    return next(o["id"] for o in m.get(mod_id).options if o["name"] == name)


def pending(m: ModManager, mod_id: str):
    snap = m.edit_snapshot(mod_id)
    return snap, em.Pending(snap.record)


def save(m: ModManager, mod_id: str, change, force=False, dry_run=False):
    """apply_edit_plan of what ``change(pending, snapshot)`` asks for."""
    snap, p = pending(m, mod_id)
    change(p, snap)
    return m.apply_edit_plan(p.plan(mod_id, snap.stamp, snap.rev), force=force, dry_run=dry_run)


def target_of(m: ModManager, snap, rel: str, option: str = "", member: str = "", tool: str = "data"):
    return next(t for t in m.edit_targets(snap, tool) if t.source.rel == rel and t.source.option == option
                and t.member == member)


def set_values(m: ModManager, mod_id: str, rel: str, values: dict, option: str = "", member: str = "",
               rule: bool = False, tool: str = "data", force=False):
    """Set ``values`` ({address: text}) of one file as the 게임 수치 page does (``rule``: every row of the
    table with the row's value)."""
    snap, p = pending(m, mod_id)
    target = target_of(m, snap, rel, option, member, tool)
    view = m.open_values(snap, target)
    assert not view.readonly, view.readonly
    rows = {r.value.at: r for r in view.rows}
    for at, text in values.items():
        v = rows[at].value
        p.set_value(target.source, member, target.engine, target.label, at=v.rule_at if rule else v.at, kind=v.kind,
                    before=v.text, after=text, rule=rule, raw=v.raw)
    return m.apply_edit_plan(p.plan(mod_id, snap.stamp, snap.rev), force=force)


def exclude(m: ModManager, mod_id: str, rel: str, option: str = "", flag: bool = True, n: int = 0):
    return save(m, mod_id, lambda p, snap: p.exclude(em.Source(option, n, rel), flag))


def value(data: bytes, at: str) -> str:
    return rszdata.DataFile(data, mf.registry()).get(at).text


def stored_hashes(m: ModManager, mod_id: str) -> dict:
    """The mod's stored files (files/ and options/) by path, hashed (I1: 모드 수정 never writes them)."""
    out = {}
    for sub in ("files", "options"):
        base = m.mod_dir(mod_id) / sub
        for path in sorted(base.rglob("*")) if base.is_dir() else ():
            if path.is_file():
                out[path.relative_to(m.mod_dir(mod_id)).as_posix()] = (hashlib.sha1(path.read_bytes()).hexdigest(),
                                                                       path.stat().st_mtime_ns)
    return out


def keys(m: ModManager, mod_id: str) -> dict:
    return {rel: key for rel, (_path, key) in m.mod_entries(mod_id).items()}


# ---- the overlay ---------------------------------------------------------------------------------------
def test_files_left_out_without_options_and_put_back(manager, tmp_path, game_dir):
    mod = install(manager, tmp_path, "Look", {TEX: "t", "natives/STM/Art/y.mdf2.45": "m"})
    manager.deploy()
    before = stored_hashes(manager, mod.id)
    result = exclude(manager, mod.id, TEX)
    assert result.changed and result.deploy and result.equipment and not result.lua
    assert result.materials  # (the mod has a material: its check counts the files left out)
    assert manager.mod_files(mod.id) == ["natives/STM/Art/y.mdf2.45"] and manager.excluded_files(mod.id) == [TEX]
    assert manager.dirty and manager.edit_summary(mod.id).text() == "뺀 파일 1개"
    manager.deploy()
    assert not (game_dir / TEX).exists()
    exclude(manager, mod.id, TEX, flag=False)  # 원래대로: the record goes
    assert not (manager.mod_dir(mod.id) / EDITS_RECORD).exists() and manager.edit_summary(mod.id).empty
    manager.deploy()
    assert (game_dir / TEX).read_text() == "t"
    assert stored_hashes(manager, mod.id) == before  # I1


def test_every_file_left_out_is_allowed_and_the_mod_still_loads(manager, tmp_path):
    mod = install(manager, tmp_path, "Gone", {TEX: "t"})
    exclude(manager, mod.id, TEX)  # no "keep at least one file"
    assert manager.mod_files(mod.id) == [] and manager.mod_entries(mod.id) == {}
    again = ModManager(manager.data_dir)
    assert again.get(mod.id).name == "Gone" and again.mod_files(mod.id) == [] and again.excluded_files(mod.id) == [TEX]


def test_leaving_out_a_later_option_s_copy_lets_the_earlier_one_win(manager, tmp_path, game_dir):
    mod = install_options(manager, tmp_path, "Colors", {"Blue": {TEX: "blue", "natives/STM/b.tex": "b"},
                                                        "Red": {TEX: "red"}})
    blue, red = option_id(manager, mod.id, "Blue"), option_id(manager, mod.id, "Red")
    manager.set_options(mod.id, [blue, red])
    assert keys(manager, mod.id)[TEX] == f"{red}/{TEX}"  # the later option wins
    snap = manager.edit_snapshot(mod.id)
    files = {(f.source.option, f.rel): f for f in snap.files}
    assert files[("Blue", TEX)].state == "overridden" and files[("Blue", TEX)].overridden_by == "Red"
    assert files[("Blue", TEX)].overlap == files[("Red", TEX)].overlap == ("Blue", "Red")
    # 쓸 옵션 = Blue: the other selected options' copies are left out
    exclude(manager, mod.id, TEX, option="Red")
    assert keys(manager, mod.id)[TEX] == f"{blue}/{TEX}"
    assert manager.excluded_files(mod.id) == []  # (Blue's copy still goes in)
    files = {(f.source.option, f.rel): f for f in manager.edit_snapshot(mod.id).files}
    assert (files[("Blue", TEX)].state, files[("Red", TEX)].state) == ("in", "excluded")
    manager.deploy()
    assert (game_dir / TEX).read_text() == "blue"
    manager.set_options(mod.id, [blue])  # leaving out an option that is off changes nothing
    assert keys(manager, mod.id)[TEX] == f"{blue}/{TEX}"
    manager.set_options(mod.id, [red])
    assert TEX not in manager.mod_entries(mod.id) and manager.excluded_files(mod.id) == [TEX]
    files = {(f.source.option, f.rel): f for f in manager.edit_snapshot(mod.id).files}
    assert files[("Blue", TEX)].state == "option_off"
    manager.set_options(mod.id, [])
    assert {f.state for f in manager.edit_snapshot(mod.id).files} == {"needs_option", "excluded"}


def test_snapshot_lists_every_file_with_its_kind_twin_and_base(manager, tmp_path):
    twin = "natives/STM/streaming/Art/Model/x/x_ALBD.tex.241106027"
    files = {TEX: "t", twin: "s", ITEM: mf.item_data(), LUA: mf.NIER_CONFIG_LUA, "Mod.pak": "p",
             "reframework/plugins/x.dll": "MZ", "natives/STM/Sound/bgm.sbnk.1.x64": "b"}
    mod = install(manager, tmp_path, "All", files)
    snap = manager.edit_snapshot(mod.id)
    by = {f.rel: f for f in snap.files}
    assert {rel: f.kind for rel, f in by.items()} == {TEX: "텍스처", twin: "텍스처", ITEM: "데이터", LUA: "스크립트",
                                                      "Mod.pak": "PAK", "reframework/plugins/x.dll": "플러그인",
                                                      "natives/STM/Sound/bgm.sbnk.1.x64": "소리"}
    assert (by[TEX].twin, by[twin].twin, by[ITEM].twin) == (twin, TEX, "")
    assert by[TEX].option_id is None and by[TEX].state == "in" and snap.options == []
    assert snap.bases[ITEM] == em.BaseRef(manager.files_dir(mod.id) / ITEM, ITEM, "stored", ITEM)
    assert snap.stamp == manager.get(mod.id).stamp and snap.rev == 0 and snap.issues == [] and not snap.pending
    assert snap.placed == {} or set(snap.placed.values()) == {"none"}


# ---- values: build, deploy, revert ----------------------------------------------------------------------
def test_a_value_edit_reaches_the_game_and_reverts(manager, tmp_path, game_dir):
    shipped = mf.item_data(3)
    mod = install(manager, tmp_path, "Items", {ITEM: shipped, TEX: "t"})
    before = stored_hashes(manager, mod.id)
    snap = manager.edit_snapshot(mod.id)
    (target,) = manager.edit_targets(snap, "data")
    assert target.label == "ItemData.user.3 · natives/STM/GameDesign/Common/Item" and not target.edited
    view = manager.open_values(snap, target)
    assert view.total == len(view.rows) > 30 and not view.compared  # (the fake game has no readable pak)
    assert view.notes == [rszdata.NO_GAME]
    result = set_values(manager, mod.id, ITEM, {SELL.format(101): "5000"})
    assert result.changed and result.deploy and not result.materials and result.summary == "바꾼 값 1개 (파일 1개)"
    path, key = manager.mod_entries(mod.id)[ITEM]
    assert key == "edits/" + ITEM and path.parent == manager.mod_dir(mod.id) / "edits"
    assert value(path.read_bytes(), SELL.format(101)) == "5000" and value(path.read_bytes(), SELL.format(100)) == "30"
    record = manager.edit_record(mod.id)
    (item,) = record.items
    assert record.rev == 1 and item.base.key == ITEM and item.out.file == path.relative_to(manager.mod_dir(mod.id)).as_posix()
    assert manager.get(mod.id).edited_at and manager.get(mod.id).stamp.endswith("|" + manager.get(mod.id).edited_at)
    manager.deploy()
    assert value((game_dir / ITEM).read_bytes(), SELL.format(101)) == "5000"
    # the window sees the change it made
    snap = manager.edit_snapshot(mod.id)
    row = next(r for r in manager.open_values(snap, target_of(manager, snap, ITEM)).rows if r.value.at == SELL.format(101))
    assert row.base == "60" and row.mine.after == "5000" and target_of(manager, snap, ITEM).edited
    # 원래 값으로
    result = save(manager, mod.id, lambda p, s: p.revert_value(em.Source("", 0, ITEM), "", SELL.format(101)))
    assert result.changed and result.summary == "이 모드에 한 수정이 없습니다."
    assert not (manager.mod_dir(mod.id) / EDITS_RECORD).exists() and keys(manager, mod.id)[ITEM] == ITEM
    manager.deploy()
    assert (game_dir / ITEM).read_bytes() == shipped
    assert stored_hashes(manager, mod.id) == before  # I1


def test_a_rule_and_its_rows(manager, tmp_path):
    rows = [mf.item_row(i, _SellPrice=99999 if i != 1 else 5) for i in range(4)]
    mod = install(manager, tmp_path, "Cost", {ITEM: mf.item_data(rows=rows)})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "5000"}, rule=True)
    (change,) = manager.edit_record(mod.id).items[0].members[0].changes
    assert change.at == "_Values[*]._SellPrice" and change.before == "99999" and change.count == 3
    data = manager.mod_entries(mod.id)[ITEM][0].read_bytes()
    assert [value(data, SELL.format(100 + i)) for i in range(4)] == ["5000", "5", "5000", "5000"]
    snap = manager.edit_snapshot(mod.id)
    found = {r.value.at: r for r in manager.open_values(snap, target_of(manager, snap, ITEM)).rows}
    assert found[SELL.format(102)].rule == change and found[SELL.format(101)].rule is None
    (line,) = manager.edit_items(mod.id)
    assert line.kind == "data" and line.state == "active" and line.changes == (change,)


def test_pak_value_edit_end_to_end(manager, tmp_path, game_dir):
    pak = mf.pak(tmp_path / "pak" / "Rates.pak", {ITEM: mf.item_data(3), TEX: b"TEXTURE", AREA: mf.area_table()},
                 compress=True)
    with manager.prepare_install(pak) as session:
        mod = manager.install_package(session, session.variants)
    snap = manager.edit_snapshot(mod.id)
    targets = manager.edit_targets(snap, "data")
    members = {t.member: t for t in targets}
    item_key, area_key = f"#{path_hash(ITEM):016X}", f"#{path_hash(AREA):016X}"
    assert set(members) == {item_key, area_key}  # (the texture is not a data file)
    assert members[item_key].label.startswith("Rates.pak · TestItemData (") and not members[item_key].readonly
    set_values(manager, mod.id, "Rates.pak", {SELL.format(102): "123"}, member=item_key)
    path, key = manager.mod_entries(mod.id)["Rates.pak"]
    assert key == "edits/Rates.pak"
    built = PakFile(path)
    assert value(built.read_hash(path_hash(ITEM)), SELL.format(102)) == "123"
    assert built.read_hash(path_hash(TEX)) == b"TEXTURE" and list(built.order) == list(PakFile(pak).order)
    manager.deploy()
    (placed,) = (game_dir / "pak_mods").glob("*.pak")
    assert value(PakFile(placed).read_hash(path_hash(ITEM)), SELL.format(102)) == "123"
    save(manager, mod.id, lambda p, s: p.drop(em.Source("", 0, "Rates.pak")))  # 원래대로 on the whole file
    manager.deploy()
    (placed,) = (game_dir / "pak_mods").glob("*.pak")
    assert placed.read_bytes() == pak.read_bytes()


def test_paks_that_cannot_be_rebuilt_and_big_ones(manager, tmp_path, monkeypatch):
    from mhwmm.modedit import pakedit

    pak = mf.pak(tmp_path / "pak" / "Big.pak", {ITEM: mf.item_data(3)})
    with manager.prepare_install(pak) as session:
        mod = manager.install_package(session, session.variants)
    monkeypatch.setattr(pakedit, "BIG_PAK", 10)
    (target,) = manager.edit_targets(manager.edit_snapshot(mod.id), "data")
    assert not target.readonly and target.note.startswith("이 PAK(") and target.note.endswith("복사본을 하나 더 저장합니다.")
    monkeypatch.setattr(pakedit, "check_rebuildable", lambda path: "나누어 저장된 PAK입니다")
    snap = manager.edit_snapshot(mod.id)
    (target,) = manager.edit_targets(snap, "data")
    assert target.readonly == "이 PAK은 다시 만들 수 없는 형식이라 값을 바꿀 수 없습니다 (나누어 저장된 PAK입니다)"
    assert manager.open_values(snap, target).readonly == target.readonly


def test_script_settings_value(manager, tmp_path, game_dir):
    mod = install(manager, tmp_path, "Nier", {LUA: mf.NIER_CONFIG_LUA})
    snap = manager.edit_snapshot(mod.id)
    (target,) = manager.edit_targets(snap, "config")
    assert (target.engine, target.tool) == ("lua", "config") and manager.edit_targets(snap, "data") == []
    view = manager.open_values(snap, target)
    assert {r.value.at: r.base for r in view.rows}["Config.UI_Scale"] == "1.0" and not view.compared
    result = set_values(manager, mod.id, LUA, {"Config.UI_Scale": "1.25"}, tool="config")
    assert result.changed and not result.equipment and not result.materials
    manager.deploy()
    assert b"Config.UI_Scale = 1.25 -- size" in (game_dir / LUA).read_bytes()


def test_dry_run_writes_nothing(manager, tmp_path):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    snap, p = pending(manager, mod.id)
    target = target_of(manager, snap, ITEM)
    v = next(r.value for r in manager.open_values(snap, target).rows if r.value.at == SELL.format(100))
    p.set_value(target.source, "", "rsz", "", at=v.at, kind=v.kind, before=v.text, after="1")
    result = manager.apply_edit_plan(p.plan(mod.id, snap.stamp, snap.rev), dry_run=True)
    assert not result.changed and result.summary == "바꾼 값 1개 (파일 1개)"
    assert not (manager.mod_dir(mod.id) / EDITS_RECORD).exists() and not (manager.mod_dir(mod.id) / "edits").exists()
    assert not list(manager.temp_root.glob("modedit-*")) and manager.get(mod.id).edited_at == ""


def test_record_addresses_are_matched_as_the_file_spells_them(manager, tmp_path):
    """A change keeps the address it was made at until its file is built again (a re-apply that waits, an update
    not applied yet): a row named then by its position or another key still shows the change, the conflict and
    the rule (#8)."""
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    set_values(manager, mod.id, ITEM, {SELL.format(101): "5000"})
    record = manager.edit_record(mod.id).copy()
    member = record.items[0].members[0]
    member.changes = [em.Change("_Values[1]._SellPrice", "S32", "60", "5000"),  # (by position)
                      em.Change("_Values[_Index=1,_ItemId=101]._GetRank[*]", "S32", "2", "7", count=1)]
    member.conflicts = [em.Conflict(em.Change("_Values[2]._SellPrice", "S32", "90", "9"), "95", "changed")]
    em.save(manager.mod_dir(mod.id), record)
    manager._invalidate(mod.id)
    snap = manager.edit_snapshot(mod.id)
    rows = {r.value.at: r for r in manager.open_values(snap, target_of(manager, snap, ITEM)).rows}
    assert rows[SELL.format(101)].mine == member.changes[0] and rows[SELL.format(100)].mine is None
    assert rows[SELL.format(102)].conflict == member.conflicts[0]
    assert rows["_Values[_ItemId=101]._GetRank[1]"].rule == member.changes[1]
    assert rows["_Values[_ItemId=101]._GetRank[0]"].rule is None  # (its value is 1, the rule's before is 2)


def test_values_say_which_game_values_came_from_rows_paired_by_a_key(mlib, tmp_path):
    """게임 값의 몇 배 and 모드가 바꾼 만큼의 몇 배 use only the values whose rows were paired with the game's by a key
    (ValueView.keyed, from rszdata.compare): rows paired by their position may have been moved by the mod (#11)."""
    mod = install(mlib, tmp_path, "Items", {ITEM: mf.item_data(3)})
    snap = mlib.edit_snapshot(mod.id)
    view = mlib.open_values(snap, target_of(mlib, snap, ITEM))
    assert view.compared and SELL.format(101) in view.keyed and view.keyed <= {r.value.at for r in view.rows}
    zones = install(mlib, tmp_path, "Zones", {AREA: mf.area_table([mf.area("zone", 1.0, 2.0), mf.area("zone", 3.0, 4.0)])})
    snap = mlib.edit_snapshot(zones.id)
    view = mlib.open_values(snap, target_of(mlib, snap, AREA))
    rows = {r.value.at: r for r in view.rows}
    assert view.compared and rows["_Areas[0]._Min"].game == "1.0"  # shown, paired by position
    assert "_Areas[0]._Min" not in view.keyed and "_Scale" in view.keyed  # (in no table)


def test_a_file_that_does_not_read_is_read_only_not_an_error(manager, tmp_path, monkeypatch):
    """Whatever goes wrong while a file or a PAK is read, the page says so and shows nothing to change (#25)."""
    from mhwmm.modedit import engines, pakedit

    pak = mf.pak(tmp_path / "pak" / "Rates.pak", {ITEM: mf.item_data(3)})
    with manager.prepare_install(pak) as session:
        mod = manager.install_package(session, session.variants)
    item = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    snap = manager.edit_snapshot(item.id)
    target = target_of(manager, snap, ITEM)
    get = engines.get

    class Broken:
        name, tool = "rsz", "data"

        def open(self, data, rel):
            raise IndexError("list index out of range")

    monkeypatch.setattr(engines, "get", lambda name: Broken() if name == "rsz" else get(name))
    view = manager.open_values(snap, target)
    assert view.readonly == "파일을 읽지 못했습니다: list index out of range" and view.rows == []
    assert rsz.TypeRegistry._holds == 0  # (the registry was given back)
    monkeypatch.setattr(engines, "get", get)

    def unreadable(path, **kw):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(pakedit, "members", unreadable)
    (target,) = manager.edit_targets(manager.edit_snapshot(mod.id), "data")
    assert target.readonly == "PAK을 읽지 못했습니다: [Errno 5] Input/output error" and not target.member


# ---- readers beside mod_entries --------------------------------------------------------------------------
def test_material_files_provided_and_shader_sources_leave_out_excluded_files(manager, tmp_path):
    shader = "natives/STM/Shader/mine.mmtr.1"
    mod = install_options(manager, tmp_path, "Pack", {
        "A": {"natives/STM/Art/a.mdf2.45": "m", TEX: "t", shader: "s"},
        "B": {"natives/STM/Art/b.mdf2.45": "m"}})
    a = option_id(manager, mod.id, "A")
    assert len(manager.material_files(mod.id)) == 2
    (job,) = manager.material_jobs([mod.id])
    assert "x_albd.tex" in job.provided and "mine.mmtr" in job.provided and job.excluded == ""
    assert manager._shader_providers("shader/mine.mmtr") == [(mod.id, a)]
    for rel in (TEX, shader, "natives/STM/Art/a.mdf2.45"):
        exclude(manager, mod.id, rel, option="A")
    assert [k for k, _rel, _p in manager.material_files(mod.id)] == [f"{option_id(manager, mod.id, 'B')}/natives/STM/Art/b.mdf2.45"]
    (job,) = manager.material_jobs([mod.id])
    assert "x_albd.tex" not in job.provided and "mine.mmtr" not in job.provided  # must-fix 8
    assert job.excluded == manager._exclusion_digest(mod.id) == em.exclusion_digest(
        [f"{a}/{rel}" for rel in (TEX, shader, "natives/STM/Art/a.mdf2.45")])
    assert manager._shader_providers("shader/mine.mmtr") == []


def test_lua_requires_follow_the_scripts_left_out(manager, tmp_path):
    mod = install(manager, tmp_path, "Scripts", {"reframework/autorun/a.lua": 'local x = require("LibX")',
                                                 "reframework/autorun/b.lua": "-- b"})
    assert manager.get(mod.id).lua_requires == ["LibX"]
    result = exclude(manager, mod.id, "reframework/autorun/a.lua")
    assert result.lua and manager.get(mod.id).lua_requires == []
    assert ModManager(manager.data_dir).get(mod.id).lua_requires == []
    assert exclude(manager, mod.id, "reframework/autorun/a.lua", flag=False).lua
    assert manager.get(mod.id).lua_requires == ["LibX"]


def test_foreign_shaders_follow_an_edited_file(manager, tmp_path):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    record = {"game": manager.game_version(), "format": 5, "results": [
        {"key": ITEM, "rel": ITEM, "kind": "user", "state": "current", "shaders": [["mat", "Shader/x.mmtr"]]}]}
    fsutil.atomic_write_json(manager.mod_dir(mod.id) / "matfix.json", record)
    manager._invalidate(mod.id)
    assert manager.foreign_shaders(mod.id) == {"Shader/x.mmtr": ["mat"]}
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"})
    assert keys(manager, mod.id)[ITEM] == "edits/" + ITEM
    assert manager.foreign_shaders(mod.id) == {"Shader/x.mmtr": ["mat"]}  # (edits/ is stripped like matfix/)


# ---- stamps ------------------------------------------------------------------------------------------------
def test_stamps(manager, tmp_path):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3), TEX: "t"})
    files_stamp = f"{mod.installed_at}|{mod.updated_at}"
    assert mod.stamp == mod.files_stamp == files_stamp  # byte-identical for a mod never edited
    assert manager.crash_context()["mods"][0]["stamp"] == files_stamp
    equip = manager.equipment_job(mod.id).stamp
    manager._shader_index()
    shaders = manager._shader_cache[0]
    jobs = manager.material_jobs([mod.id])
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"})
    assert mod.files_stamp == files_stamp and mod.stamp == f"{files_stamp}|{mod.edited_at}"
    assert manager.equipment_job(mod.id).stamp != equip
    manager._shader_index()
    assert manager._shader_cache[0] != shaders
    assert manager.crash_context()["mods"][0]["stamp"] == mod.stamp
    assert jobs[0].stamp == files_stamp == manager.material_jobs([mod.id])[0].stamp  # (a value edit: same input)
    first = mod.edited_at
    exclude(manager, mod.id, TEX)  # (within the same second: still a new stamp)
    assert mod.edited_at > first


# ---- matfix ------------------------------------------------------------------------------------------------
def test_a_check_started_before_an_edit_is_still_stored(mlib, tmp_path):
    mod = install(mlib, tmp_path, "Items", {ITEM: mf.item_data(3)})
    jobs = mlib.material_jobs([mod.id])
    found = mlib.run_materials(jobs)
    set_values(mlib, mod.id, ITEM, {SELL.format(100): "1"})
    mlib.finish_materials(found, jobs)
    assert mlib.material_state(mod.id) == "current"


def test_value_edits_keep_the_material_verdict(mlib, tmp_path, mgame):
    """I5: a value edit changes value bytes in place only, so matfix judges its output as it judges the base."""
    mod = install(mlib, tmp_path, "Items", {ITEM: mf.item_data(3)})
    set_values(mlib, mod.id, ITEM, {SELL.format(101): "777", "_Values[_ItemId=100]._Weight": "2.5"})
    out = mlib.mod_entries(mod.id)[ITEM][0]
    game = mlib.game_files()
    verdict = lambda path: [(r.state, r.rsz, r.notes) for r in check_mod([(ITEM, ITEM, path)], game, None)]
    assert verdict(out) == verdict(mlib.files_dir(mod.id) / ITEM) == [("current", [], [])]

    stale = install(mlib, tmp_path, "Old", {ITEM: mf.stale_item_data(3)})
    snap = mlib.edit_snapshot(stale.id)
    view = mlib.open_values(snap, target_of(mlib, snap, ITEM))
    assert view.readonly == rszdata.STALE and view.rows == []  # Q4: never written in the old layout
    snap.stuck[ITEM] = "고칠 방법이 없습니다"  # (what matfix says of one it could not repair)
    assert mlib.open_values(snap, target_of(mlib, snap, ITEM)).readonly == rszdata.STALE + rszdata.STUCK_SUFFIX
    M.run(mlib, [stale.id], repair=True)
    assert mlib.material_state(stale.id) == "repaired"
    repaired = mlib.mod_entries(stale.id)[ITEM][0]
    set_values(mlib, stale.id, ITEM, {SELL.format(101): "777"})
    out = mlib.mod_entries(stale.id)[ITEM][0]
    assert verdict(out) == verdict(repaired) == [("current", [], [])]
    a, b = rszdata.DataFile(repaired.read_bytes(), mf.registry()), rszdata.DataFile(out.read_bytes(), mf.registry())
    assert {v.at: v.text for v in a.values() if v.at != SELL.format(101)} == \
        {v.at: v.text for v in b.values() if v.at != SELL.format(101)}
    assert b.get(SELL.format(101)).text == "777" and len(out.read_bytes()) == len(repaired.read_bytes())


def test_values_are_compared_with_the_game(mlib, tmp_path):
    mod = install(mlib, tmp_path, "Items", {ITEM: mf.item_data(3)})
    snap = mlib.edit_snapshot(mod.id)
    view = mlib.open_values(snap, target_of(mlib, snap, ITEM))
    rows = {r.value.at: r for r in view.rows}
    assert view.compared and rows[SELL.format(101)].game == "10" and rows[SELL.format(101)].base == "60"
    assert rows["_Values[_ItemId=100]._MaxCount"].game == "10"
    assert any("게임에 새로 생긴 행 1개" in n for n in view.notes)


def test_repair_restore_and_re_apply(mlib, tmp_path, game_dir=None):
    """A repair in use is the base; declining it falls back to the stored file (the edit waits: the stored file
    is stale) and repairing again applies the edit on the new repair."""
    mod = install(mlib, tmp_path, "Old", {ITEM: mf.stale_item_data(3)})
    M.run(mlib, [mod.id], repair=True)
    set_values(mlib, mod.id, ITEM, {SELL.format(102): "4242"})
    (item,) = mlib.edit_record(mod.id).items
    assert item.base.key == "matfix/" + ITEM and keys(mlib, mod.id)[ITEM] == "edits/" + ITEM
    snap = mlib.edit_snapshot(mod.id)
    assert rszdata.REPAIR_NOTE in mlib.open_values(snap, target_of(mlib, snap, ITEM)).notes

    assert mlib.restore_materials(mod.id)  # 복구 취소
    assert keys(mlib, mod.id)[ITEM] == ITEM  # I4: the output does not fit: the stored file, without edits
    assert mlib.edits_pending() == [mod.id] and mlib.edit_issues(mod.id) == ["다시 적용 대기 1개"]
    (result,) = mlib.reapply_edits()
    assert result.changed and result.pending == 1
    (item,) = mlib.edit_record(mod.id).items
    assert item.base.key == ITEM and item.out is None and item.members[0].pending == rszdata.STALE
    assert item.members[0].changes[0].after == "4242"  # kept, waiting
    assert mlib.edits_pending() == [] and mlib.edit_issues(mod.id)[0].startswith("다시 적용하지 못한 파일 1개: ")

    M.run(mlib, [mod.id], repair=True)  # repaired again: a new base
    assert mlib.edits_pending() == [mod.id] and keys(mlib, mod.id)[ITEM] == "matfix/" + ITEM
    mlib.reapply_edits([mod.id])
    (item,) = mlib.edit_record(mod.id).items
    assert item.base.key == "matfix/" + ITEM and not item.members[0].pending
    assert value(mlib.mod_entries(mod.id)[ITEM][0].read_bytes(), SELL.format(102)) == "4242"
    assert mlib.edit_issues(mod.id) == []


def test_files_left_out_make_the_check_unchecked(mlib, tmp_path):
    mod = install(mlib, tmp_path, "Items", {ITEM: mf.stale_item_data(3), TEX: "t"})
    M.run(mlib, [mod.id])
    assert mlib.material_state(mod.id) == "stale"
    mlib.mark_materials_offered([mod])
    assert mlib.materials_to_offer() == [] and mlib._material_record(mod.id)["excluded"] == ""
    result = exclude(mlib, mod.id, TEX)
    assert result.materials and mlib.material_state(mod.id) == "unchecked"
    M.run(mlib, unchecked_only=True)
    record = mlib._material_record(mod.id)
    assert record["excluded"] == mlib._exclusion_digest(mod.id) != "" and "offered" not in record
    assert mlib.material_state(mod.id) == "stale" and mlib.materials_to_offer() == [mod]  # offered again


def test_records_without_the_excluded_key_stay_current(mlib, tmp_path):
    mod = install(mlib, tmp_path, "Items", {ITEM: mf.item_data(3)})
    M.run(mlib, [mod.id])
    record = dict(mlib._material_record(mod.id))
    del record["excluded"]  # (made before v2.7.0)
    mlib._save_material_record(mod.id, record)
    assert mlib.material_state(mod.id) == "current"


def test_a_pak_repaired_for_another_entry_can_be_edited_and_reverted(mlib, tmp_path, mgame):
    pak = mf.pak(tmp_path / "pak" / "Mix.pak", {ITEM: mf.stale_item_data(3), AREA: mf.area_table(), TEX: b"T"})
    with mlib.prepare_install(pak) as session:
        mod = mlib.install_package(session, session.variants)
    M.run(mlib, [mod.id], repair=True)
    assert keys(mlib, mod.id)["Mix.pak"] == "matfix/Mix.pak"
    repaired = mlib.mod_entries(mod.id)["Mix.pak"][0]
    area = f"#{path_hash(AREA):016X}"
    set_values(mlib, mod.id, "Mix.pak", {"_Scale": "3.0"}, member=area)
    assert mlib.edit_record(mod.id).items[0].base.key == "matfix/Mix.pak"
    path, key = mlib.mod_entries(mod.id)["Mix.pak"]
    assert key == "edits/Mix.pak"
    built = PakFile(path)
    assert value(built.read_hash(path_hash(AREA)), "_Scale") == "3.0"
    assert built.read_hash(path_hash(ITEM)) == PakFile(repaired).read_hash(path_hash(ITEM))  # the repair stays
    save(mlib, mod.id, lambda p, s: p.revert_value(em.Source("", 0, "Mix.pak"), area, "_Scale"))  # never refused
    assert keys(mlib, mod.id)["Mix.pak"] == "matfix/Mix.pak"


def test_a_repair_of_a_file_left_out_stays(mlib, tmp_path, mgame):
    """The material check leaves out the files 모드 수정 left out, so it keeps their repairs (and the values set on
    top of them): put back, the game gets the repair again, not the stored file in the old layout (§2.7, #2)."""
    mod = install(mlib, tmp_path, "Old", {ITEM: mf.stale_item_data(3), AREA: mf.area_table(), TEX: "t"})
    M.run(mlib, [mod.id], repair=True)  # the repair, accepted
    set_values(mlib, mod.id, ITEM, {SELL.format(102): "4242"})
    repaired = mlib.repaired_materials(mod.id)[ITEM][1]
    repaired_bytes = repaired.read_bytes()
    exclude(mlib, mod.id, ITEM)
    M.run(mlib, unchecked_only=True)  # the check the save starts: nothing else to repair
    assert mlib.material_state(mod.id) == "current" and mlib.repaired_materials(mod.id) == {ITEM: (ITEM, repaired)}
    exclude(mlib, mod.id, ITEM, flag=False)
    assert keys(mlib, mod.id)[ITEM] == "edits/" + ITEM and mlib.edits_pending() == [] and mlib.edit_issues(mod.id) == []
    mlib.deploy()
    assert value((mgame / ITEM).read_bytes(), SELL.format(102)) == "4242"
    M.run(mlib, unchecked_only=True)
    assert mlib.material_state(mod.id) == "repaired" and mlib.materials_to_offer() == []

    exclude(mlib, mod.id, ITEM)
    M.run(mlib, [mod.id], repair=True)  # a repair run while it is left out keeps it too (made for this game)
    ((rel, path),) = mlib.repaired_materials(mod.id).values()
    assert rel == ITEM and path.read_bytes() == repaired_bytes
    exclude(mlib, mod.id, ITEM, flag=False)
    assert keys(mlib, mod.id)[ITEM].endswith("/" + ITEM)  # (the repair: with the values once they are built on it)
    mlib.reapply_edits()
    assert value(mlib.mod_entries(mod.id)[ITEM][0].read_bytes(), SELL.format(102)) == "4242"
    M.run(mlib, unchecked_only=True)
    assert mlib.material_state(mod.id) == "repaired"

    exclude(mlib, mod.id, ITEM)  # one made for another game version is not kept by a repair run
    record = dict(mlib._material_record(mod.id))
    record["applied_game"] = "an older game"
    mlib._save_material_record(mod.id, record)
    M.run(mlib, [mod.id], repair=True)
    assert mlib.repaired_materials(mod.id) == {}


def test_a_stuck_entry_of_a_pak_says_it_cannot_be_repaired(mlib, tmp_path, monkeypatch):
    """matfix labels a PAK's entry "<pak>#<hash>.user": the 게임 수치 page adds that it cannot be repaired for it as
    for a loose file, also when another entry's repair made the repair PAK the base (§2.7, #6)."""
    from mhwmm.matfix import repair

    member = f"{path_hash(ITEM):016X}"
    real = repair.RszContext.plan

    def plan(self, label, data, donor, provided=None):  # (as rsz.plan says of an entry it finds no way to repair)
        found = real(self, label, data, donor, provided)
        if found is not None and member in label:
            found.state, found.data, found.reason, found.method = "stuck", None, "고칠 방법이 없습니다", ""
        elif found is not None and f"{path_hash(AREA):016X}" in label and fix_area:
            found.state, found.data, found.method = "fixable", data, "CRC 갱신"
        return found

    monkeypatch.setattr(repair.RszContext, "plan", plan)
    for fix_area, via in ((False, "stored"), (True, "repair")):
        pak = mf.pak(tmp_path / via / "Mix.pak", {ITEM: mf.stale_item_data(3), AREA: mf.area_table()})
        with mlib.prepare_install(pak) as session:
            mod = mlib.install_package(session, session.variants)
        M.run(mlib, [mod.id], repair=fix_area)
        assert [s["label"] for s in mlib.material_stuck(mod.id)] == [f"Mix.pak#{member}.user"]
        snap = mlib.edit_snapshot(mod.id)
        assert snap.stuck == {f"Mix.pak#{member}": "고칠 방법이 없습니다"} and snap.bases["Mix.pak"].via == via
        view = mlib.open_values(snap, target_of(mlib, snap, "Mix.pak", member="#" + member))
        assert view.readonly == rszdata.STALE + rszdata.STUCK_SUFFIX


def _registry_library(tmp_path, monkeypatch):
    """A library with an AVP the bundled .user type data (2000-01-01, from before the game update) cannot repair and
    the newer data (2099-01-01: what test_rsz knows) can; ``download_registry`` gives the newer one. Returns the
    manager, the mod and the newer data's bytes."""
    import io
    import lzma

    if not matfix.available():
        pytest.skip("게임 pak을 읽을 zstd 모듈이 없습니다")

    def blob(date, types, old):
        return lzma.compress(json.dumps({"meta": {"reasy_date": date}, "types": types, "empty": {}, "alt": {},
                                         "old": old}).encode())

    old_types = {format(R.PARAM, "x"): [format(R.NO_CHILD_CRC, "x"), R.PARAM_NAME, json.dumps(R.PARAM_FIELDS[:-1])],
                 format(R.CHILD, "x"): [format(R.CHILD_CRC, "x"), "app.TestChild", json.dumps(R.CHILD_FIELDS)]}
    newer = blob("2099-01-01", R.registry()._types, R.registry()._old)

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    real_download = rsz.download_registry
    monkeypatch.setattr(rsz, "download_registry", lambda folder: real_download(folder, opener=lambda url: Response(newer)))
    monkeypatch.setattr(rsz, "REGISTRY_FILE", write(tmp_path / "bundled" / rsz.REGISTRY_NAME,
                                                    blob("2000-01-01", old_types, {})))
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", None)
    root = tmp_path / "MonsterHunterWilds"
    write(root / GAME_EXE, "exe")
    M.write_game_pak(root / "re_chunk_000.pak", {M.AVP: R.game_file()})
    m = ModManager(tmp_path / "data")
    m.settings.check_game_running = False
    m.set_game_dir(root)
    return m, M.install_tree(m, tmp_path / "Lepus", {M.AVP: M.old_avp()}), newer


def _background_check(m: ModManager) -> None:
    jobs = m.material_jobs(unchecked_only=True)
    if jobs:
        m.finish_materials(m.run_materials(jobs), jobs)


@pytest.mark.parametrize("held, before_the_jobs", [(True, False), (False, True), (True, True)])
def test_newer_type_data_is_read_by_the_next_check_while_the_window_holds_the_old(tmp_path, monkeypatch, held,
                                                                                 before_the_jobs):
    """A download of newer .user type data while 모드 수정's window holds the old one (or one found out of date
    before the jobs) is what the check reads next, and the record says the date of what it read: a file stuck
    with the old data is repaired, not kept stuck until the next game update (#7)."""
    from mhwmm.matfix import repair
    from mhwmm.matfix.pak import path_hash as hash_of

    m, mod, _newer = _registry_library(tmp_path, monkeypatch)
    if before_the_jobs:  # (the game's own copy shows the bundled data is out of date)
        monkeypatch.setattr(repair, "SAMPLE_PFB", hash_of(M.AVP))
    if held:
        rszdata.registry()  # the 게임 수치 page is open
    try:
        _background_check(m)
        _background_check(m)  # (the one _matfix_again starts after a download)
    finally:
        if held:
            rszdata.release()
    assert m.material_state(mod.id) == "stale" and not m.material_stuck(mod.id)
    assert m._material_record(mod.id)["registry"] == "2099-01-01"


def test_a_check_says_the_date_of_the_type_data_it_read(tmp_path, monkeypatch):
    """Newer type data put in place by another run of the manager (the command line) while the window holds the
    older one: the check reads the held one and says so, so what is stuck with it is checked again (#7)."""
    m, mod, newer = _registry_library(tmp_path, monkeypatch)
    monkeypatch.setattr(rsz, "download_registry", lambda folder: None)
    rszdata.registry()
    try:
        write(m.data_dir / "cache" / rsz.REGISTRY_NAME, newer)
        _background_check(m)
        assert m._material_record(mod.id)["registry"] == "2000-01-01" and m.material_stuck(mod.id) == []
        assert m.material_state(mod.id) == "unchecked"
    finally:
        rszdata.release()
    _background_check(m)
    assert m.material_state(mod.id) == "stale" and m._material_record(mod.id)["registry"] == "2099-01-01"


# ---- I11 and saving ------------------------------------------------------------------------------------------
def test_changed_game_copies_refuse_values(manager, tmp_path, game_dir):
    mod = install(manager, tmp_path, "Nier", {LUA: mf.NIER_CONFIG_LUA, ITEM: mf.item_data(3)})
    manager.deploy()
    touch_later(game_dir / LUA, mf.NIER_CONFIG_LUA.replace(b"1.0", b"2.0"))  # the script saved its settings
    snap = manager.edit_snapshot(mod.id)
    assert {f.rel: f.placed for f in snap.files} == {LUA: "changed", ITEM: "ok"}
    (target,) = manager.edit_targets(snap, "config")
    assert target.readonly == textconfig.GAME_COPY_CHANGED
    assert manager.open_values(snap, target).readonly == textconfig.GAME_COPY_CHANGED
    p = em.Pending(snap.record)
    p.set_value(target.source, "", "lua", "", at="Config.UI_Scale", kind="float", before="1.0", after="1.5")
    with pytest.raises(EditError, match="게임 안에서 이 파일이 바뀌었습니다"):
        manager.edit_job(p.plan(mod.id, snap.stamp, snap.rev))
    # a data file kept aside in edited\ while the mod is off
    touch_later(game_dir / ITEM, b"changed in the game")
    manager.set_enabled(mod.id, False)
    manager.deploy()
    snap = manager.edit_snapshot(mod.id)
    assert {f.rel: f.placed for f in snap.files} == {LUA: "stash", ITEM: "stash"}
    assert target_of(manager, snap, ITEM).readonly.startswith("게임 폴더의 이 파일은 매니저가 넣은 뒤에 바뀌었습니다")
    p = em.Pending(snap.record)
    p.set_value(em.Source("", 0, ITEM), "", "rsz", "", at=SELL.format(100), kind="S32", before="30", after="1")
    with pytest.raises(EditError, match="게임 폴더의 이 파일은"):
        manager.edit_job(p.plan(mod.id, snap.stamp, snap.rev))


def test_putting_back_a_changed_game_copy_needs_force(manager, tmp_path, game_dir):
    mod = install(manager, tmp_path, "Nier", {LUA: mf.NIER_CONFIG_LUA})
    set_values(manager, mod.id, LUA, {"Config.UI_Scale": "1.5"}, tool="config")
    manager.deploy()
    touch_later(game_dir / LUA, b"-- saved by the script")
    revert = lambda p, s: p.revert_value(em.Source("", 0, LUA), "", "Config.UI_Scale")
    with pytest.raises(GameCopyChanged) as caught:
        save(manager, mod.id, revert)
    assert caught.value.rels == [LUA]
    assert save(manager, mod.id, revert, force=True).changed
    # leaving a file out needs no question (deploy keeps the changed copy aside, and puts it back)
    assert exclude(manager, mod.id, LUA).changed


def kept(m: ModManager) -> dict:
    """The copies the deploy kept aside in edited\\ ({path there: bytes})."""
    base = m.data_dir / "edited"
    return {p.relative_to(base).as_posix(): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()} \
        if base.is_dir() else {}


def test_choosing_another_option_s_copy_keeps_the_changed_game_copy(manager, tmp_path, game_dir):
    """쓸 옵션, putting back a later option's copy and 모두 원래대로 make another option's copy the one the mod puts
    there. They need no question when the game copy changed, as switching options in the 옵션 tab: deploy keeps
    the changed copy aside (with a warning) and puts it back with its own option's file (#0)."""
    mod = install_options(manager, tmp_path, "Presets", {"A": {LUA: b"Scale = 1.0\n"}, "B": {LUA: b"Scale = 2.0\n"}})
    a, b = option_id(manager, mod.id, "A"), option_id(manager, mod.id, "B")
    manager.set_options(mod.id, [a, b])
    manager.deploy()
    copy_of = lambda: (game_dir / LUA).read_bytes()
    use_a = lambda p, s: p.exclude(em.Source("B", 0, LUA), True)
    put_back = lambda p, s: p.exclude(em.Source("B", 0, LUA), False)
    touch_later(game_dir / LUA, b"Scale = 7.5 -- B, mine\n")
    assert save(manager, mod.id, use_a).changed  # (no GameCopyChanged)
    report = manager.deploy()
    assert copy_of() == b"Scale = 1.0\n" and kept(manager) == {LUA: b"Scale = 7.5 -- B, mine\n"}
    (warning,) = report.warnings
    assert LUA in warning and "따로 보관" in warning and "다시 들어가면 보관한 파일로 되돌립니다" in warning
    touch_later(game_dir / LUA, b"Scale = 3.0 -- A, mine\n")
    assert save(manager, mod.id, put_back).changed
    manager.deploy()  # B's copy is back, A's goes aside beside it
    assert copy_of() == b"Scale = 7.5 -- B, mine\n" and kept(manager) == {LUA + ".2": b"Scale = 3.0 -- A, mine\n"}
    assert save(manager, mod.id, use_a).changed
    manager.deploy()
    assert copy_of() == b"Scale = 3.0 -- A, mine\n" and kept(manager) == {LUA: b"Scale = 7.5 -- B, mine\n"}
    assert save(manager, mod.id, lambda p, s: p.reset_all()).changed  # 모두 원래대로
    manager.deploy()
    assert copy_of() == b"Scale = 7.5 -- B, mine\n"
    # every copy left out: kept aside; A's put back: A's copy is back
    assert save(manager, mod.id, lambda p, s: [p.exclude(em.Source(o, 0, LUA), True) for o in "AB"]).changed
    manager.deploy()
    assert not (game_dir / LUA).exists() and sorted(kept(manager).values()) == [b"Scale = 3.0 -- A, mine\n",
                                                                                b"Scale = 7.5 -- B, mine\n"]
    assert save(manager, mod.id, lambda p, s: p.exclude(em.Source("A", 0, LUA), False)).changed
    manager.deploy()
    assert copy_of() == b"Scale = 3.0 -- A, mine\n"

    # the 옵션 tab does the same
    assert save(manager, mod.id, lambda p, s: p.reset_all()).changed
    manager.deploy()
    assert copy_of() == b"Scale = 7.5 -- B, mine\n"
    manager.set_options(mod.id, [a])
    report = manager.deploy()
    assert copy_of() == b"Scale = 3.0 -- A, mine\n" and kept(manager) == {LUA: b"Scale = 7.5 -- B, mine\n"}
    assert len(report.warnings) == 1
    manager.set_options(mod.id, [b])
    report = manager.deploy()
    assert copy_of() == b"Scale = 7.5 -- B, mine\n" and kept(manager) == {LUA + ".2": b"Scale = 3.0 -- A, mine\n"}
    assert len(report.warnings) == 1


def test_another_option_s_copy_put_in_later_keeps_what_the_script_saved(manager, tmp_path, game_dir, monkeypatch):
    """The save is checked while the game copy is still the mod's, but the deploy comes later (the game is running)
    and the script saves its settings in between: the deploy keeps them aside, and they come back with their
    option's file (#0, #12)."""
    import mhwmm.manager as manager_module
    from mhwmm.deploy import DeployError

    manager.settings.check_game_running = True
    running = [False]
    monkeypatch.setattr(manager_module, "is_game_running", lambda: running[0])
    mod = install_options(manager, tmp_path, "Presets", {"A": {LUA: b"Scale = 1.0\n"}, "B": {LUA: b"Scale = 2.0\n"}})
    manager.set_options(mod.id, [option_id(manager, mod.id, "A"), option_id(manager, mod.id, "B")])
    manager.deploy()
    copy_of = lambda: (game_dir / LUA).read_bytes()

    def later(change, mine: bytes):
        running[0] = True
        assert save(manager, mod.id, change).changed
        with pytest.raises(DeployError):  # (the apply after the save is refused)
            manager.deploy()
        touch_later(game_dir / LUA, mine)  # the script saves its settings
        running[0] = False
        report = manager.deploy()
        assert len(report.warnings) == 1 and not report.errors
        return report

    use_a = lambda p, s: p.exclude(em.Source("B", 0, LUA), True)
    later(use_a, b"Scale = 2.0 -- mine\n")  # 쓸 옵션 = A
    assert copy_of() == b"Scale = 1.0\n" and kept(manager) == {LUA: b"Scale = 2.0 -- mine\n"}
    later(lambda p, s: p.exclude(em.Source("B", 0, LUA), False), b"Scale = 1.0 -- mine\n")  # B's copy put back
    assert copy_of() == b"Scale = 2.0 -- mine\n" and kept(manager) == {LUA + ".2": b"Scale = 1.0 -- mine\n"}
    later(use_a, b"Scale = 2.0 -- mine again\n")
    assert copy_of() == b"Scale = 1.0 -- mine\n" and kept(manager) == {LUA: b"Scale = 2.0 -- mine again\n"}
    later(lambda p, s: p.reset_all(), b"Scale = 1.0 -- mine, 2\n")  # 모두 원래대로
    assert copy_of() == b"Scale = 2.0 -- mine again\n" and kept(manager) == {LUA + ".2": b"Scale = 1.0 -- mine, 2\n"}


def test_a_game_copy_changed_after_the_save_is_kept_aside_by_the_deploy(manager, tmp_path, game_dir):
    """I11 is checked when the edit is saved; the deploy may come later (the game was running, nothing applied at
    once) and the script may save its settings in between: that copy is kept in edited\\, not deleted, and comes
    back with the mod's own file (#12)."""
    mod = install(manager, tmp_path, "Nier", {LUA: mf.NIER_CONFIG_LUA})
    manager.deploy()
    set_values(manager, mod.id, LUA, {"Config.UI_Scale": "1.5"}, tool="config")  # the game copy is the mod's
    touch_later(game_dir / LUA, b"-- saved by the script")  # before the save reached the game
    report = manager.deploy()
    assert b"Config.UI_Scale = 1.5" in (game_dir / LUA).read_bytes() and not report.errors
    assert (manager.data_dir / "edited" / LUA).read_bytes() == b"-- saved by the script"
    (warning,) = report.warnings
    assert LUA in warning and "따로 보관" in warning
    save(manager, mod.id, lambda p, s: p.revert_value(em.Source("", 0, LUA), "", "Config.UI_Scale"))
    manager.deploy()  # 원래대로: the mod's own file, and with it the user's copy
    assert (game_dir / LUA).read_bytes() == b"-- saved by the script"


def test_finish_checks_the_game_copy_again(manager, tmp_path, game_dir):
    mod = install(manager, tmp_path, "Nier", {LUA: mf.NIER_CONFIG_LUA})
    manager.deploy()
    snap, p = pending(manager, mod.id)
    p.set_value(em.Source("", 0, LUA), "", "lua", "", at="Config.UI_Scale", kind="float", before="1.0", after="1.5")
    job = manager.edit_job(p.plan(mod.id, snap.stamp, snap.rev))
    manager.build_edit(job)
    touch_later(game_dir / LUA, b"-- saved while the save was being built")
    with pytest.raises(EditError, match="게임 안에서 이 파일이 바뀌었습니다"):
        manager.finish_edit(job)
    assert not (manager.mod_dir(mod.id) / EDITS_RECORD).exists() and not list(manager.temp_root.glob("modedit-*"))


def test_stamp_rev_and_base_must_not_change_meanwhile(manager, tmp_path):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3), TEX: "t"})
    snap, p = pending(manager, mod.id)
    p.exclude(em.Source("", 0, TEX), True)
    with pytest.raises(EditError, match="그사이 바뀌었습니다"):
        manager.edit_job(p.plan(mod.id, snap.stamp, snap.rev + 1))
    with pytest.raises(EditError, match="그사이 바뀌었습니다"):
        manager.edit_job(p.plan(mod.id, "other|stamp", snap.rev))
    job = manager.edit_job(p.plan(mod.id, snap.stamp, snap.rev))
    manager.build_edit(job)
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"})  # another save in between
    with pytest.raises(EditError, match="모드가 그사이 바뀌어 저장하지 않았습니다"):
        manager.finish_edit(job)
    # the base changed while the save was built
    snap, p = pending(manager, mod.id)
    p.set_value(em.Source("", 0, ITEM), "", "rsz", "", at=SELL.format(101), kind="S32", before="60", after="2")
    job = manager.edit_job(p.plan(mod.id, snap.stamp, snap.rev))
    manager.build_edit(job)
    touch_later(manager.files_dir(mod.id) / ITEM, mf.item_data(3))
    with pytest.raises(EditError, match="모드가 그사이 바뀌어"):
        manager.finish_edit(job)
    assert manager.edit_record(mod.id).rev == 1


def test_a_failed_post_check_rolls_everything_back(manager, tmp_path, monkeypatch):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3), TEX: "t"})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"})
    record_bytes = (manager.mod_dir(mod.id) / EDITS_RECORD).read_bytes()
    outputs = sorted(p.name for p in (manager.mod_dir(mod.id) / "edits").iterdir())
    edited_at = mod.edited_at
    real = ModManager._edit_overlay
    monkeypatch.setattr(ModManager, "_edit_overlay",
                        lambda self, mod_id: type(real(self, mod_id))(set(), dict(real(self, mod_id).outputs)))
    with pytest.raises(EditError, match="수정을 저장한 뒤 확인하지 못해 되돌렸습니다"):
        save(manager, mod.id, lambda p, s: (p.exclude(em.Source("", 0, TEX), True),
                                            p.set_value(em.Source("", 0, ITEM), "", "rsz", "", at=SELL.format(102),
                                                        kind="S32", before="90", after="9")))
    monkeypatch.undo()
    assert (manager.mod_dir(mod.id) / EDITS_RECORD).read_bytes() == record_bytes  # F2
    assert sorted(p.name for p in (manager.mod_dir(mod.id) / "edits").iterdir()) == outputs
    assert mod.edited_at == edited_at and not list(manager.temp_root.glob("modedit-*"))
    manager._invalidate(mod.id)
    assert manager.excluded_files(mod.id) == [] and keys(manager, mod.id)[ITEM] == "edits/" + ITEM


def test_outputs_are_kept_one_generation(manager, tmp_path):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    folder = manager.mod_dir(mod.id) / "edits"
    for n, price in enumerate(("1", "2", "3"), 1):
        set_values(manager, mod.id, ITEM, {SELL.format(100): price})
        names = sorted(p.name for p in folder.iterdir())
        assert names == [f"{k:04d}_ItemData.user.3" for k in range(max(1, n - 1), n + 1)]
    write(folder / "0099_left.user.3", "x")  # left behind by a crash: no record refers to it
    ModManager(manager.data_dir)  # startup: nothing is kept for workers
    assert sorted(p.name for p in folder.iterdir()) == ["0003_ItemData.user.3"]
    set_values(manager, mod.id, ITEM, {SELL.format(100): "4"})
    assert sorted(p.name for p in folder.iterdir())[-1] == "0004_ItemData.user.3"  # numbers are never reused


def test_reset_deletes_the_record_and_clear_bad_sets_an_unreadable_one_aside(manager, tmp_path):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3), TEX: "t"})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"})
    exclude(manager, mod.id, TEX)
    result = save(manager, mod.id, lambda p, s: p.reset_all())
    assert result.changed and not (manager.mod_dir(mod.id) / EDITS_RECORD).exists()
    assert keys(manager, mod.id) == {ITEM: ITEM, TEX: TEX}
    write(manager.mod_dir(mod.id) / EDITS_RECORD, json.dumps({"format": 99, "items": []}))
    manager._invalidate(mod.id)
    assert manager.edit_issues(mod.id) == ["모드 수정 기록이 더 새로운 버전의 매니저에서 만든 것이라 쓰지 않습니다"]
    assert manager.edit_summary(mod.id).error and manager.edit_items(mod.id) == []
    with pytest.raises(EditError):
        exclude(manager, mod.id, TEX)  # never written over
    assert save(manager, mod.id, lambda p, s: p.clear_bad()).changed
    assert not (manager.mod_dir(mod.id) / EDITS_RECORD).exists()
    (bad,) = manager.mod_dir(mod.id).glob(EDITS_RECORD + ".bad-*")
    assert json.loads(bad.read_text())["format"] == 99 and manager.edit_issues(mod.id) == []


def test_only_conflicts_taken_away_keeps_the_output(manager, tmp_path):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1", SELL.format(101): "2"})
    record = manager.edit_record(mod.id).copy()
    member = record.items[0].members[0]
    gone = em.Change(SELL.format(109), "S32", "1", "2")
    member.conflicts.append(em.Conflict(gone, None, "gone"))
    em.save(manager.mod_dir(mod.id), record)
    manager._invalidate(mod.id)
    out = manager.edit_record(mod.id).items[0].out
    assert manager.edit_summary(mod.id).text() == "바꾼 값 2개 (파일 1개) · 확인할 값 1개"
    save(manager, mod.id, lambda p, s: p.take_author(em.Source("", 0, ITEM), "", SELL.format(109)))
    (item,) = manager.edit_record(mod.id).items
    assert item.out == out and not item.members[0].conflicts  # (no rebuild: the game's copy stays the same)


# ---- updates -------------------------------------------------------------------------------------------------
def test_an_update_shifting_option_ids_keeps_the_files_left_out(manager, tmp_path):
    mod = install_options(manager, tmp_path, "Colors", {"Blue": {TEX: "b"}, "Red": {TEX: "r", "natives/STM/r.tex": "r"}})
    red = option_id(manager, mod.id, "Red")
    manager.set_options(mod.id, [red])
    exclude(manager, mod.id, "natives/STM/r.tex", option="Red")
    assert manager.excluded_files(mod.id) == ["natives/STM/r.tex"] and red == "o2"
    seen = {}
    real = ModManager._carry_edits

    def spy(self, mod_id, old_dir, stage, old_options, new_options, progress=None):
        seen["old"], seen["live"] = old_options, [dict(o) for o in self.get(mod_id).options]
        return real(self, mod_id, old_dir, stage, old_options, new_options, progress)

    ModManager._carry_edits = spy
    try:
        install_options(manager, tmp_path, "Colors", {"Black": {TEX: "k"}, "Blue": {TEX: "b2"},
                                                      "Red": {TEX: "r2", "natives/STM/r.tex": "r2"}}, replace=mod.id)
    finally:
        ModManager._carry_edits = real
    # the live Mod already has the new options when the carry-over runs; the old ones were read before
    assert [o["name"] for o in seen["old"]] == ["Blue", "Red"] and [o["name"] for o in seen["live"]] == ["Black", "Blue", "Red"]
    assert option_id(manager, mod.id, "Red") == "o3" and manager.get(mod.id).selected == ["o3"]
    assert manager.excluded_files(mod.id) == ["natives/STM/r.tex"] and keys(manager, mod.id) == {TEX: f"o3/{TEX}"}
    notice = manager.edit_record(mod.id).notice
    assert notice["kind"] == "update" and notice["kept"] == 1 and notice["conflicts"] == 0


@pytest.mark.parametrize("windows", [False, True])
def test_an_update_that_changes_only_the_letter_case_keeps_the_edits(manager, tmp_path, game_dir, monkeypatch, windows):
    """An update whose archive spells a folder with other letter cases (natives/STM → natives/stm) has the same
    files: the carry-over names them the update's way, so the files left out stay out and the values stay in
    (Windows finds the old spelling too, simulated with a link the listing skips; the keys have the new one)."""
    import os

    if windows and os.name == "nt":
        pytest.skip("Windows에서는 실제 파일 시스템으로 windows=False가 이 경우를 확인합니다")
    old_tex, new_tex = "natives/STM/Art/x.tex", "natives/stm/Art/x.tex"
    old_item, new_item = ITEM, ITEM.replace("natives/STM/", "natives/stm/")
    mod = install(manager, tmp_path, "Look", {old_tex: "x", "natives/STM/Art/y.tex": "y", old_item: mf.item_data(3)})
    exclude(manager, mod.id, old_tex)
    set_values(manager, mod.id, old_item, {SELL.format(100): "4321"})
    if windows:
        real = ModManager._carry_edits

        def windows_like(self, mod_id, old_dir, stage, old_options, new_options, progress=None):
            link = stage / "files" / "natives" / "STM"
            try:
                os.symlink("stm", link)
            except OSError:
                pytest.skip("링크를 만들 수 없습니다")
            try:
                return real(self, mod_id, old_dir, stage, old_options, new_options, progress)
            finally:
                os.unlink(link)

        monkeypatch.setattr(ModManager, "_carry_edits", windows_like)
    install(manager, tmp_path, "Look", {new_tex: "x2", "natives/stm/Art/y.tex": "y2", new_item: mf.item_data(3)},
            replace=mod.id)
    record = manager.edit_record(mod.id)
    assert sorted(i.source.rel for i in record.items) == sorted([new_tex, new_item])
    report = em.CarryReport.from_notice(record.notice)
    assert (report.kept, report.dropped, report.conflicts) == (2, [], 0)
    assert manager.excluded_files(mod.id) == [new_tex] and new_tex not in keys(manager, mod.id)
    assert keys(manager, mod.id)[new_item] == "edits/" + new_item and manager.edit_issues(mod.id) == []
    manager.deploy()
    assert not (game_dir / new_tex).exists()
    assert value((game_dir / new_item).read_bytes(), SELL.format(100)) == "4321"


def test_an_update_with_the_same_file_copies_the_output(manager, tmp_path, game_dir):
    shipped = mf.item_data(3)
    mod = install(manager, tmp_path, "Items", {ITEM: shipped, TEX: "t"})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"})
    old = manager.edit_record(mod.id).items[0].out
    old_stat = (manager.mod_dir(mod.id) / old.file).stat()
    manager.deploy()
    install(manager, tmp_path, "Items", {ITEM: shipped, TEX: "t2"}, replace=mod.id)
    (item,) = manager.edit_record(mod.id).items
    assert item.out == old and (manager.mod_dir(mod.id) / old.file).stat().st_mtime_ns == old_stat.st_mtime_ns
    assert item.base.key == ITEM and item.base.mtime_ns == (manager.files_dir(mod.id) / ITEM).stat().st_mtime_ns
    assert keys(manager, mod.id)[ITEM] == "edits/" + ITEM and manager.edits_pending() == []
    assert em.CarryReport.from_notice(manager.edit_record(mod.id).notice).text() == "수정을 다시 적용했습니다: 수정 1개"
    report = manager.deploy()
    assert value((game_dir / ITEM).read_bytes(), SELL.format(100)) == "1" and report.copied == 1  # (only the texture)


def test_an_update_with_a_new_file_re_applies_and_reports_conflicts(manager, tmp_path):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1", SELL.format(101): "2", SELL.format(102): "3"})
    rows = [mf.item_row(0), mf.item_row(1, _SellPrice=2), mf.item_row(2, _SellPrice=777), mf.item_row(3)]
    install(manager, tmp_path, "Items", {ITEM: mf.item_data(rows=rows)}, replace=mod.id)
    (item,) = manager.edit_record(mod.id).items
    member = item.members[0]
    assert [c.at for c in member.changes] == [SELL.format(100), SELL.format(101)]  # re-applied, already
    (conflict,) = member.conflicts
    assert conflict.why == "changed" and conflict.now == "777" and conflict.change.after == "3"
    data = manager.mod_entries(mod.id)[ITEM][0].read_bytes()
    assert [value(data, SELL.format(100 + i)) for i in range(4)] == ["1", "2", "777", "120"]  # Q3: the author's
    report = em.CarryReport.from_notice(manager.edit_record(mod.id).notice)
    assert (report.reapplied, report.conflicts, report.pending) == (2, 1, 0)
    assert manager.edit_issues(mod.id) == ["확인할 값 1개"]
    assert manager.problems()[mod.id] == ["모드 수정 내용 일부가 적용되지 않았습니다: 확인할 값 1개 (모드 수정 → 수정 내용)"]
    assert [line.state for line in manager.edit_items(mod.id)] == ["conflict"]
    # 내 값 유지
    save(manager, mod.id, lambda p, s: p.keep_mine(em.Source("", 0, ITEM), "", SELL.format(102)))
    data = manager.mod_entries(mod.id)[ITEM][0].read_bytes()
    assert value(data, SELL.format(102)) == "3" and manager.edit_issues(mod.id) == []


def test_an_update_applies_a_rule_to_new_rows(manager, tmp_path):
    rows = [mf.item_row(i, _SellPrice=99999) for i in range(2)]
    mod = install(manager, tmp_path, "Cost", {ITEM: mf.item_data(rows=rows)})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "50"}, rule=True)
    rows = [mf.item_row(i, _SellPrice=99999) for i in range(3)] + [mf.item_row(3, _SellPrice=5)]
    install(manager, tmp_path, "Cost", {ITEM: mf.item_data(rows=rows)}, replace=mod.id)
    (change,) = manager.edit_record(mod.id).items[0].members[0].changes
    assert change.count == 3
    data = manager.mod_entries(mod.id)[ITEM][0].read_bytes()
    assert [value(data, SELL.format(100 + i)) for i in range(4)] == ["50", "50", "50", "5"]


def test_an_update_without_the_file_or_the_option(manager, tmp_path):
    mod = install_options(manager, tmp_path, "Pack", {"A": {ITEM: mf.item_data(3), TEX: "t", "natives/STM/k.tex": "k"},
                                                      "Red": {"natives/STM/r.tex": "r"}})
    manager.set_options(mod.id, [option_id(manager, mod.id, "A"), option_id(manager, mod.id, "Red")])
    exclude(manager, mod.id, TEX, option="A")
    exclude(manager, mod.id, "natives/STM/k.tex", option="A")
    exclude(manager, mod.id, "natives/STM/r.tex", option="Red")
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"}, option="A")
    result = install_options(manager, tmp_path, "Pack", {"A": {"natives/STM/k.tex": "k2"}, "B": {"natives/STM/b.tex": "b"}},
                             replace=mod.id)
    record = manager.edit_record(mod.id)
    by = {(i.source.option, i.source.rel): i for i in record.items}
    assert by[("A", "natives/STM/k.tex")].exclude and not by[("A", "natives/STM/k.tex")].conflict  # kept
    assert ("A", TEX) not in by  # an exclusion of a file the update removed: dropped and reported
    assert by[("A", ITEM)].conflict == "업데이트에서 파일이 없어졌습니다" and by[("A", ITEM)].members  # kept
    assert by[("Red", "natives/STM/r.tex")].conflict == "업데이트에서 옵션 'Red'이(가) 없어졌습니다"
    report = em.CarryReport.from_notice(record.notice)
    assert report.dropped == [f"A: {TEX}"] and report.kept == 1 and report.conflicts == 2
    issues = manager.edit_issues(result.id)
    assert f"A: {ITEM}: 업데이트에서 파일이 없어졌습니다" in issues
    assert "Red: natives/STM/r.tex: 업데이트에서 옵션 'Red'이(가) 없어졌습니다" in issues
    states = {(v.source.option, v.kind): v.state for v in manager.edit_items(mod.id)}
    assert states[("A", "data")] == "item_conflict" and states[("Red", "exclude")] == "item_conflict"
    # an option back in a later update: its exclusion counts again; the values of a file back are applied again
    install_options(manager, tmp_path, "Pack", {"A": {"natives/STM/k.tex": "k2", ITEM: mf.item_data(3)},
                                                "Red": {"natives/STM/r.tex": "r"}}, replace=mod.id)
    record = manager.edit_record(mod.id)
    by = {(i.source.option, i.source.rel): i for i in record.items}
    assert not by[("Red", "natives/STM/r.tex")].conflict and not by[("A", ITEM)].conflict
    assert em.CarryReport.from_notice(record.notice).conflicts == 0
    manager.set_options(mod.id, [option_id(manager, mod.id, "A"), option_id(manager, mod.id, "Red")])
    assert value(manager.mod_entries(mod.id)[ITEM][0].read_bytes(), SELL.format(100)) == "1"
    assert manager.excluded_files(mod.id) == ["natives/STM/k.tex", "natives/STM/r.tex"]
    # 지우기 on an item conflict
    install_options(manager, tmp_path, "Pack", {"A": {"natives/STM/k.tex": "k2"}}, replace=mod.id)
    assert manager.edit_record(mod.id).item(em.Source("Red", 0, "natives/STM/r.tex")).conflict
    save(manager, mod.id, lambda p, s: p.drop(em.Source("Red", 0, "natives/STM/r.tex")))
    assert not any(i.source.option == "Red" for i in manager.edit_record(mod.id).items)


def test_an_update_counts_the_values_to_check_it_makes(manager, tmp_path):
    """The update's notice counts the values to check that this update made, not the difference of the counts:
    a new conflict is reported though an earlier one heals in the same file (#1), an open one whose value the
    author changed again counts again, and one the file only names another way now (its rows keyed by another
    field) does not."""
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})

    def update(extra=(), **sell):  # rows 100-102 with these _SellPrice (by row index), and rows after them
        rows = [mf.item_row(i, **({"_SellPrice": sell[f"i{i}"]} if f"i{i}" in sell else {})) for i in range(3)]
        install(manager, tmp_path, "Items", {ITEM: mf.item_data(rows=[*rows, *extra])}, replace=mod.id)
        member = manager.edit_record(mod.id).items[0].members[0]
        notice = em.CarryReport.from_notice(manager.edit_record(mod.id).notice)
        return notice, [(c.at, c.after) for c in member.changes], [(x.change.at, x.now) for x in member.conflicts]

    set_values(manager, mod.id, ITEM, {SELL.format(100): "5000", SELL.format(101): "6000"})
    assert update(i1=50)[0].conflicts == 1
    notice, changes, conflicts = update(i0=77)  # 101 back to 60 (applied again), 100 changed by the author
    assert changes == [(SELL.format(101), "6000")] and conflicts == [(SELL.format(100), "77")]
    assert notice.conflicts == notice.problems == 1 and "확인할 값 1개" in notice.text()
    assert update(i0=88)[0].conflicts == 1  # changed again: to check again
    assert update(i0=88, i2=91)[0].conflicts == 0  # (the file changed, that value did not)
    # another row repeats _ItemId 102: the rows are named by _SortId now
    notice, changes, conflicts = update([mf.item_row(3, _ItemId=102)], i0=88, i2=91)
    assert changes == [("_Values[_SortId=1]._SellPrice", "6000")]
    assert conflicts == [("_Values[_SortId=0]._SellPrice", "88")]
    assert notice.conflicts == 0 and notice.reapplied == 1


@pytest.mark.parametrize("fails", ["build", "write"])
def test_values_left_waiting_by_an_update_are_named_as_its_file_names_them(manager, tmp_path, monkeypatch, fails):
    """An update that names the rows another way (another key) and whose file could not be written (no space):
    the values wait, named the way the update's file names them, so the 게임 수치 page and the command line find
    them on its rows (they match the record by address): 원래 값으로 and a new value work (#8)."""
    from mhwmm.modedit import EditError

    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    set_values(manager, mod.id, ITEM, {SELL.format(101): "5000", SELL.format(100): "6000"})
    calls = []
    if fails == "build":  # (the manager lets the item wait)
        real = edit_build.build_item

        def build_item(inp, *args, **kwargs):
            if inp.mode == "carry" and not calls:
                calls.append(inp.rel)
                raise OSError(28, "No space left on device")
            return real(inp, *args, **kwargs)

        monkeypatch.setattr(edit_build, "build_item", build_item)
    else:  # (the build lets every member wait as it was given)
        real = edit_build._write

        def write_output(*args):
            if not calls:
                calls.append(args[1])
                raise EditError("바꾼 파일을 확인하지 못해 저장하지 않았습니다")
            return real(*args)

        monkeypatch.setattr(edit_build, "_write", write_output)
    rows = [*(mf.item_row(i) for i in range(3)), mf.item_row(3, _ItemId=102)]
    install(manager, tmp_path, "Items", {ITEM: mf.item_data(rows=rows)}, replace=mod.id)
    assert calls
    (member,) = manager.edit_record(mod.id).items[0].members
    first, second = "_Values[_SortId=1]._SellPrice", "_Values[_SortId=0]._SellPrice"
    assert member.pending and [(c.at, c.after) for c in member.changes] == [(first, "5000"), (second, "6000")]
    assert value(manager.mod_entries(mod.id)[ITEM][0].read_bytes(), first) == "60"  # (waits)
    snap, p = pending(manager, mod.id)
    target = target_of(manager, snap, ITEM)
    rows = {r.value.at: r for r in manager.open_values(snap, target).rows}
    assert rows[first].mine.after == "5000" and rows[first].mine == p.member(target.source, "").changes[0]
    p.revert_value(target.source, "", first)  # 원래 값으로 on the row
    v = rows[second].value
    p.set_value(target.source, "", target.engine, target.label, at=v.at, kind=v.kind, before=v.text, after="7000")
    assert [(c.at, c.after) for c in p.member(target.source, "").changes] == [(second, "7000")]
    assert manager.apply_edit_plan(p.plan(mod.id, snap.stamp, snap.rev)).changed
    data = manager.mod_entries(mod.id)[ITEM][0].read_bytes()
    assert (value(data, first), value(data, second)) == ("60", "7000")
    (member,) = manager.edit_record(mod.id).items[0].members
    assert [(c.at, c.after) for c in member.changes] == [(second, "7000")] and not member.pending


def test_an_update_to_a_stale_file_leaves_the_values_waiting(manager, tmp_path):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"})
    install(manager, tmp_path, "Items", {ITEM: mf.stale_item_data(3)}, replace=mod.id)
    (item,) = manager.edit_record(mod.id).items
    assert item.members[0].pending == "지금 게임과 형식이 달라 다시 적용하지 못했습니다. 재질 점검에서 복구하면 다시 적용합니다."
    assert item.out is None and item.members[0].changes and keys(manager, mod.id)[ITEM] == ITEM
    assert em.CarryReport.from_notice(manager.edit_record(mod.id).notice).pending == 1
    assert manager.edits_pending() == []  # (it waits for a repair: a base change)


def test_an_unreadable_record_is_kept_aside_by_an_update(manager, tmp_path):
    mod = install(manager, tmp_path, "Items", {TEX: "t"})
    write(manager.mod_dir(mod.id) / EDITS_RECORD, "{broken")
    install(manager, tmp_path, "Items", {TEX: "t2"}, replace=mod.id)
    (bad,) = manager.mod_dir(mod.id).glob(EDITS_RECORD + ".bad-*")
    assert bad.read_text() == "{broken"
    record = manager.edit_record(mod.id)
    assert not record.error and record.items == [] and "수정 기록을 읽지 못해" in record.notice["error"]
    assert em.CarryReport.from_notice(record.notice).problems == 1  # (the window asks about it)
    manager.drop_edit_notice(mod.id)
    assert not (manager.mod_dir(mod.id) / EDITS_RECORD).exists() and manager.edit_record(mod.id).notice == {}


def test_a_failing_carry_over_never_stops_the_update(manager, tmp_path, monkeypatch):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3), TEX: "t"})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"})
    exclude(manager, mod.id, TEX)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(edit_build, "build_item", broken)  # one item fails: the others go on
    install(manager, tmp_path, "Items", {ITEM: mf.item_data(4), TEX: "t"}, replace=mod.id)
    by = {i.source.rel: i for i in manager.edit_record(mod.id).items}
    assert by[ITEM].members[0].pending == "업데이트 뒤 다시 적용하지 못했습니다: boom" and by[ITEM].out is None
    assert by[TEX].exclude and keys(manager, mod.id) == {ITEM: ITEM}

    monkeypatch.setattr(ModManager, "_carry", broken)  # the whole carry-over fails: everything waits
    updated = install(manager, tmp_path, "Items", {ITEM: mf.item_data(5), TEX: "t"}, replace=mod.id)
    assert updated.id == mod.id
    by = {i.source.rel: i for i in manager.edit_record(mod.id).items}
    assert by[ITEM].members[0].pending == "업데이트 뒤 다시 적용하지 못했습니다" and by[ITEM].out is None
    assert by[TEX].exclude and keys(manager, mod.id) == {ITEM: ITEM}
    assert not (manager.mod_dir(mod.id) / "edits").exists()
    monkeypatch.undo()
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", mf.registry())
    monkeypatch.setattr(rsz.TypeRegistry, "_from_file", False)
    assert manager.edits_pending() == [mod.id]  # (its base is the old file's: built again when idle)
    manager.reapply_edits()
    assert value(manager.mod_entries(mod.id)[ITEM][0].read_bytes(), SELL.format(100)) == "1"


def test_the_record_stays_when_every_value_conflicts(manager, tmp_path):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1", SELL.format(101): "2"})
    rows = [mf.item_row(i, _SellPrice=5) for i in range(3)]
    install(manager, tmp_path, "Items", {ITEM: mf.item_data(rows=rows)}, replace=mod.id)
    (item,) = manager.edit_record(mod.id).items
    assert item.members[0].changes == [] and len(item.members[0].conflicts) == 2 and item.out is None
    assert keys(manager, mod.id)[ITEM] == ITEM and manager.edit_summary(mod.id).conflicts == 2


def test_an_update_by_v2_6_wipes_the_edits(manager, tmp_path, monkeypatch):
    """What an update made with v2.6 (no carry-over) does: the folder swap takes the edits with it."""
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"})
    monkeypatch.setattr(ModManager, "_carry_edits", lambda self, *a, **k: (em.CarryReport(), None))
    install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)}, replace=mod.id)
    assert not (manager.mod_dir(mod.id) / EDITS_RECORD).exists() and keys(manager, mod.id) == {ITEM: ITEM}


def test_the_carry_over_holds_the_registry(manager, tmp_path, monkeypatch):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"})
    calls = []
    hold, unhold = rsz.TypeRegistry.hold.__func__, rsz.TypeRegistry.unhold.__func__
    monkeypatch.setattr(rsz.TypeRegistry, "hold", classmethod(lambda cls: calls.append("hold") or hold(cls)))
    monkeypatch.setattr(rsz.TypeRegistry, "unhold", classmethod(lambda cls: calls.append("unhold") or unhold(cls)))
    install(manager, tmp_path, "Items", {ITEM: mf.item_data(4)}, replace=mod.id)
    assert calls[0] == "hold" and calls[-1] == "unhold" and calls.count("hold") == calls.count("unhold")
    assert rsz.TypeRegistry._holds == 0


# ---- the rest ------------------------------------------------------------------------------------------------
def test_a_hand_edit_of_the_mod_folder_waits_and_is_re_applied(manager, tmp_path):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"})
    touch_later(manager.files_dir(mod.id) / ITEM, mf.item_data(4))
    again = ModManager(manager.data_dir)  # (startup: every cache is new)
    assert again.edits_pending() == [mod.id] and keys(again, mod.id)[ITEM] == ITEM  # I4: the base without edits
    assert again.edits_waiting(mod.id) == [ITEM]
    assert again.edit_summary(mod.id).waiting == 1 and again.edit_snapshot(mod.id).pending == {ITEM}
    assert [line.state for line in again.edit_items(mod.id)] == ["waiting"]
    job = again.reapply_job(mod.id)
    assert job.mode == "reapply" and [inp.mode for inp in job.inputs] == ["reapply"]
    again.build_edit(job)
    result = again.finish_edit(job)
    assert result.changed and again.edits_pending() == []
    data = again.mod_entries(mod.id)[ITEM][0].read_bytes()
    assert value(data, SELL.format(100)) == "1" and value(data, SELL.format(103)) == "120"


def test_a_re_apply_whose_mod_changed_meanwhile_is_dropped(manager, tmp_path):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3), TEX: "t"})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"})
    touch_later(manager.files_dir(mod.id) / ITEM, mf.item_data(4))
    manager._invalidate(mod.id)
    job = manager.reapply_job(mod.id)
    manager.build_edit(job)
    exclude(manager, mod.id, TEX)  # a save in between (it builds what waits too): this one is skipped, not raised
    assert manager.edits_pending() == []
    rev = manager.edit_record(mod.id).rev
    result = manager.finish_edit(job)
    assert result.skipped and not result.changed and manager.edit_record(mod.id).rev == rev
    assert not list(manager.temp_root.glob("modedit-*"))


def test_profiles_use_an_option_s_edits_only_with_that_option(manager, tmp_path):
    mod = install_options(manager, tmp_path, "Rates", {"x2": {ITEM: mf.item_data(3)}, "x10": {ITEM: mf.item_data(3)}})
    x2, x10 = option_id(manager, mod.id, "x2"), option_id(manager, mod.id, "x10")
    manager.set_options(mod.id, [x10])
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"}, option="x10")
    assert keys(manager, mod.id)[ITEM] == f"edits/{x10}/{ITEM}"
    manager.create_profile("둘째")
    manager.set_options(mod.id, [x2])
    assert keys(manager, mod.id)[ITEM] == f"{x2}/{ITEM}"
    assert [line.state for line in manager.edit_items(mod.id)] == ["inactive_option"]
    manager.switch_profile("기본")
    assert keys(manager, mod.id)[ITEM] == f"edits/{x10}/{ITEM}"
    assert [line.state for line in manager.edit_items(mod.id)] == ["active"]


def test_a_retarget_of_an_edited_mod_takes_the_edits(manager, tmp_path, monkeypatch):
    """다른 장비로 옮기기 reads mod_entries: the new mod has the files left out left out and the edited values."""
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3), TEX: "t"})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1"})
    exclude(manager, mod.id, TEX)
    from mhwmm.equip import retarget as equip_retarget

    seen = {}

    def items_of(files, index):
        seen.update(files)
        raise equip_retarget.RetargetError("stop")

    monkeypatch.setattr(equip_retarget, "items_of", items_of)
    from mhwmm.equip.model import Target

    manager.plan_retarget(mod.id, Target("armor", "f/004/001"), Target("armor", "f/004/000"))
    assert set(seen) == {ITEM} and seen[ITEM][1] == "edits/" + ITEM
    assert value(seen[ITEM][0].read_bytes(), SELL.format(100)) == "1"


def test_source_key_and_summary_lines(manager, tmp_path):
    assert ModManager.source_key("edits/o1/a.user.3") == "o1/a.user.3"
    assert ModManager.source_key("matfix/a.pak") == "a.pak" and ModManager.source_key("o2/x") == "o2/x"
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3), TEX: "t"})
    set_values(manager, mod.id, ITEM, {SELL.format(100): "1", SELL.format(101): "2"})
    exclude(manager, mod.id, TEX)
    summary = manager.edit_summary(mod.id)
    assert summary.text() == "뺀 파일 1개 · 바꾼 값 2개 (파일 1개)" and summary.problem() == ""
    lines = manager.edit_items(mod.id)
    assert [(line.kind, line.label, line.state) for line in lines] == [
        ("data", "ItemData.user.3 · natives/STM/GameDesign/Common/Item", "active"), ("exclude", TEX, "active")]
