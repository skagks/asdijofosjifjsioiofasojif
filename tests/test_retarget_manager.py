"""다른 장비로 옮기기 end to end on a synthetic game (an encrypted game pak with the catalog's tables, the
target's own prefabs and the original equipment's current materials): a loose armor mod and a .pak weapon
mod moved by the manager, the new mod's files, recipe, name, place and switch, plans that refuse, the
equipment scan of the new mod, its material check against the original equipment's game files (and an
RSZ repair that keeps the new paths), the command line and 원인 모드 찾기 blocking it."""

from __future__ import annotations

import json
import zipfile

import pytest

import test_equip_catalog as C
import test_matfix as M
import test_rsz as R
from conftest import write
from equip_game_fixtures import pfb_file
from mhwmm import bisect, matfix
from mhwmm.cli import main
from mhwmm.equip import pathlist as equip_pathlist, retarget
from mhwmm.equip.model import Target
from mhwmm.manager import RETARGET_BODY, InstallError, ModManager, _chained_recipe, retarget_aliases
from mhwmm.matfix import pak, rsz
from mhwmm.matfix.game import GameIndex
from mhwmm.matfix.mdf2 import Mdf2File, assemble_mdf2, extract_material
from mhwmm.matfix.repair import NULL_MASK, check_mod

N = "natives/STM/"
CH = "Art/Model/Character/"
A, B = Target("armor", "f/004/001"), Target("armor", "f/004/000")  # the synthetic catalog's "시험" designs
THIRD = Target("armor", "f/005/000")  # not in the catalog
HELM_A, HELM_B = CH + "ch03/004/001/3/ch03_004_0013", CH + "ch03/004/000/3/ch03_004_0003"
HELM_C = CH + "ch03/005/000/3/ch03_005_0003"
ALBD_A = CH + "ch03/004/001/3/textures/bunny_ALBD.tex"  # the author's name in the helm's folder (the game's
ALBD_B = CH + "ch03/004/000/3/textures/bunny_ALBD.tex"  # own helm textures are shared with the male body)
HAIR_A = CH + "ch03/004/001/3/hair/ch03_004_0013_HairAdjustList.user.3"
HAIR_B = CH + "ch03/004/000/3/hair/ch03_004_0003_HairAdjustList.user.3"
HAIR_C = CH + "ch03/005/000/3/hair/ch03_005_0003_HairAdjustList.user.3"
PFB_A = N + "GameDesign/Equip/_Prefab/Armor/Female/004/001/Helm/ch03_004_0013.pfb.18"
PFB_B = N + "GameDesign/Equip/_Prefab/Armor/Female/004/000/Helm/ch03_004_0003.pfb.18"
PFB_C = N + "GameDesign/Equip/_Prefab/Armor/Female/005/000/Helm/ch03_005_0003.pfb.18"
MALE_HELM_A, MALE_HELM_B = CH + "ch02/004/001/3/ch02_004_0013", CH + "ch02/004/000/3/ch02_004_0003"
MALE_PFB_B = N + "GameDesign/Equip/_Prefab/Armor/Male/004/000/Helm/ch02_004_0003.pfb.18"
AVP_A = N + "GameDesign/Equip/_Prefab/Armor/Female/004/001/004_001_avp.user.3"
ITEM_A, ITEM_B = "Art/Model/Item/it02/00/0029/it0200_0029_0", "Art/Model/Item/it02/00/0028/it0200_0028_0"
WTEX_A = "Art/Model/Item/it02/00/0029/textures/it0200_0029_0_ALBD.tex"
WTEX_B = "Art/Model/Item/it02/00/0028/textures/it0200_0028_0_ALBD.tex"
WPFB_B = N + "GameDesign/Equip/_Prefab/Weapon/wp02/00/0028/it0200_0028_0.pfb.18"
TEX, MESH = b"TEX\x00" + bytes(60), b"MESH" + bytes(60)
SHORT = R.PARAM_FIELDS[:-1]  # the hair list's settings type before it got _Child: the game's file is newer


def mdf2(name: str, *textures) -> bytes:
    return assemble_mdf2([M.material(name, list(textures), [("Color", (1.0, 1.0, 1.0, 1.0))])], 45)


def current_mdf2(name: str, albd: str) -> bytes:
    """The original equipment's material now: an update gave it a slot the mod does not have."""
    return mdf2(name, ("BaseDielectricMap", albd), ("DetailMaskMap", "Art/Common/new_MSK.tex"))


def hair_list(crc: int, fields, *, scale: float, hide: int = 1, new: int = 0, refs=(HELM_A, HELM_A)) -> bytes:
    """A helm's hair settings (.user) pointing at the helm's mesh and material (a test_rsz layout)."""
    mesh, mat = refs[0] + ".mesh", refs[1] + ".mdf2"
    values = R.param(fields, scale=scale, hide=hide, new=new, mesh=mesh)
    child = R.s32(42 if crc != R.NEW_CRC else 7)
    return R.user_file([(R.CHILD, R.CHILD_CRC, R.CHILD_FIELDS, [child]), (R.PARAM, crc, fields, values)],
                       resources=(mesh, mat))


def string_of(value: bytes) -> str:
    return bytes(value[4:]).decode("utf-16-le").rstrip("\0")


def bunny() -> dict:
    """A helm mod for the female Trial (type 2) with its hair settings, a script and the set's _avp.user."""
    return {
        N + HELM_A + ".mesh.241111606": MESH,
        N + HELM_A + ".mdf2.45": mdf2("helm", ("BaseDielectricMap", ALBD_A)),
        N + ALBD_A + ".241106027": TEX,
        N + HAIR_A: hair_list(R.NO_CHILD_CRC, SHORT, scale=3.0),
        AVP_A: b"USR\x00" + bytes(60),
        "reframework/autorun/bunny.lua": b"-- bunny",
        "modinfo.ini": "name=Bunny\nversion=1.2\nauthor=Arsinia\ndescription=토끼 투구\ncategory=방어구\n"
                       "screenshot=preview.png\n".encode("utf-8"),
        "preview.png": b"\x89PNG picture",
    }


def blades_entries() -> list:
    material = mdf2("blade", ("BaseDielectricMap", WTEX_A))
    return [(pak.path_hash(N + ITEM_A + ".mesh.241111606"), MESH, len(MESH), pak.COMPRESSION_NONE),
            (pak.path_hash(N + ITEM_A + ".mdf2.45"), material, len(material), pak.COMPRESSION_NONE),
            (pak.path_hash(N + WTEX_A + ".241106027"), TEX, len(TEX), pak.COMPRESSION_NONE)]


@pytest.fixture
def game(tmp_path):
    if not matfix.available():
        pytest.skip("게임 pak을 읽을 zstd 모듈이 없습니다")
    files = {}
    for part_files, present in C.game_parts():
        files.update(part_files)
        files.update((path, b"") for path in present)
    files.update({
        # the targets' own prefabs load their own models
        PFB_A: pfb_file(HELM_A + ".mesh", HELM_A + ".mdf2"),
        PFB_B: pfb_file(HELM_B + ".mesh", HELM_B + ".mdf2"),
        PFB_C: pfb_file(HELM_C + ".mesh", HELM_C + ".mdf2"),
        MALE_PFB_B: pfb_file(MALE_HELM_B + ".mesh", MALE_HELM_B + ".mdf2"),
        WPFB_B: pfb_file(ITEM_B + ".mesh", ITEM_B + ".mdf2"),
        # the original equipment's current files (an update added a slot) and the targets' (without it)
        N + HELM_A + ".mdf2.45": current_mdf2("helm", ALBD_A),
        N + HELM_B + ".mdf2.45": mdf2("helm", ("BaseDielectricMap", ALBD_B)),
        N + HAIR_A: hair_list(R.NEW_CRC, R.PARAM_FIELDS, scale=1.0, hide=0, new=1),
        N + ITEM_A + ".mdf2.45": current_mdf2("blade", WTEX_A),
        N + ITEM_B + ".mdf2.45": mdf2("blade", ("BaseDielectricMap", WTEX_B)),
    })
    root = tmp_path / "MonsterHunterWilds"
    write(root / "MonsterHunterWilds.exe", "exe")
    M.write_game_pak(root / "re_chunk_000.pak", files)
    return root


@pytest.fixture
def library(tmp_path, game):
    m = ModManager(tmp_path / "data")
    m.settings.check_updates = m.settings.check_refw_updates = m.settings.check_game_running = False
    m.set_game_dir(game)
    first = M.install_tree(m, tmp_path / "src" / "First", {N + CH + "ch02/060/000/2/ch02_060_0002.mesh.241111606": MESH})
    mod = M.install_tree(m, tmp_path / "src" / "Bunny", bunny())
    last = M.install_tree(m, tmp_path / "src" / "Last", {N + "Art/last.tex.241106027": TEX})
    return m, mod, first, last


@pytest.fixture
def types(monkeypatch):
    """test_rsz's small registry for the material check of the hair lists (the catalog then has no names)."""
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", R.registry())
    monkeypatch.setattr(rsz.TypeRegistry, "_from_file", False)


def results_by_kind(m, mod_id) -> dict:
    return {r.kind: r for r in m.material_results(mod_id)}


# ---- creating the new mod -----------------------------------------------------------------------
def test_sources_choices_and_the_plan(library):
    m, mod, first, _last = library
    assert m.retarget_sources(mod.id) == [A]
    assert m.retarget_sources(first.id) == [Target("armor", "m/060/000")]
    assert m.retarget_choices(A) == [(B, "시험 · Trial α")]  # the same body, not the source itself
    assert m.retarget_choices(Target("palico", "062")) == []
    weapons = m.retarget_choices(Target("weapon", "02/000029", "main"))
    assert [t.key for t, _name in weapons] == ["02/000002", "02/100010", "02/100009", "02/000028", "02/000014"]
    assert weapons[3] == (Target("weapon", "02/000028"), "첫걸음쌍검Ⅰ")  # sorted by name

    plan = m.plan_retarget(mod.id, A, B)
    assert plan.ok, plan.errors
    assert {mv.old: mv.new for mv in plan.moves} == {
        N + HELM_A + ".mesh.241111606": N + HELM_B + ".mesh.241111606",
        N + HELM_A + ".mdf2.45": N + HELM_B + ".mdf2.45",
        N + ALBD_A + ".241106027": N + ALBD_B + ".241106027",
        N + HAIR_A: N + HAIR_B}
    assert dict(plan.dropped).keys() == {AVP_A, "reframework/autorun/bunny.lua"}
    assert not any("확인하지 못했습니다" in n for n in plan.notes)  # the target's prefab was read from the game


def test_choices_without_a_game(tmp_path):
    m = ModManager(tmp_path / "data")
    choices = m.retarget_choices(A)  # the bundled path list's equipment, by number
    assert (B, "방어구 004-000") in choices and A not in dict(choices)
    assert all(t.key.startswith("f/") and not t.key.startswith("f/002/") for t, _name in choices)
    assert [name for _t, name in choices] == sorted(name for _t, name in choices)
    swords = m.retarget_choices(Target("weapon", "03/000009"))
    assert swords and all(t.key.startswith("03/") and t.key != "03/000009" for t, _name in swords)


def test_plans_that_cannot_be_made_are_refused(library):
    m, mod, _first, _last = library
    assert m.plan_retarget(mod.id, A, Target("weapon", "02/000028")).errors == \
        ["방어구는 방어구로, 무기는 무기로만 옮길 수 있습니다"]
    assert "옮길 수 없는 장비" in m.plan_retarget(mod.id, A, Target("armor", "f/002/000")).errors[0]  # innerwear
    before = [x.id for x in m.mods]
    with pytest.raises(InstallError, match="대상 장비에는 여성 머리가 없습니다"):
        m.create_retarget(mod.id, A, Target("armor", "f/099/000"))  # no such helm in the game
    with pytest.raises(InstallError, match="원래 장비와 같은 장비"):
        m.create_retarget(mod.id, A, Target("armor", "m/004/001"))  # the same design folder
    assert [x.id for x in m.mods] == before and mod.enabled
    assert not list(m.temp_root.glob("retarget-*")) and len(list(m.mods_dir.iterdir())) == len(before)


def test_the_new_mod(library):
    m, mod, first, last = library
    m.deploy()
    assert not m.dirty
    new = m.create_retarget(mod.id, A, B)
    assert new.name == "Bunny → 시험" and new.id != mod.id
    assert [x.id for x in m.mods] == [first.id, mod.id, new.id, last.id]  # right after the original
    assert new.enabled and not mod.enabled and m.dirty  # the original is switched off
    assert sorted(m.mod_files(new.id)) == sorted([N + HELM_B + ".mesh.241111606", N + HELM_B + ".mdf2.45",
                                                  N + ALBD_B + ".241106027", N + HAIR_B])
    assert (new.author, new.version, new.description, new.category) == ("Arsinia", "1.2", "토끼 투구", "방어구")
    assert m.mod_picture(new.id).read_bytes() == b"\x89PNG picture"
    assert (new.nexus_id, new.update, new.source, new.options) == (0, {}, "", [])
    recipe = new.retarget
    assert {k: recipe[k] for k in ("format", "kind", "from", "to", "parts", "source_mod", "source_name")} == {
        "format": retarget.RETARGET_FORMAT, "kind": "armor", "from": "f/004/001", "to": "f/004/000",
        "parts": ["helm", "body", "arm", "waist", "leg", "slinger"], "source_mod": mod.id, "source_name": "Bunny"}
    assert sorted(recipe["moves"]) == sorted([[N + HELM_B + ".mesh.241111606", N + HELM_A + ".mesh.241111606"],
                                              [N + HELM_B + ".mdf2.45", N + HELM_A + ".mdf2.45"],
                                              [N + ALBD_B + ".241106027", N + ALBD_A + ".241106027"],
                                              [N + HAIR_B, N + HAIR_A]])
    assert recipe["pak_moves"] == [] and m.material_state(new.id) == "unchecked"
    # the moved files point at each other: the material at the moved texture, the hair list at the moved model
    material = Mdf2File((m.files_dir(new.id) / (N + HELM_B + ".mdf2.45")).read_bytes(), 45)
    assert [p.text for _t, p in material.materials[0].textures] == [ALBD_B]
    assert rsz.parse((m.files_dir(new.id) / (N + HAIR_B)).read_bytes()).resources() == \
        {HELM_B + ".mesh", HELM_B + ".mdf2"}
    # kept in mod.json and in the state
    assert json.loads((m.mod_dir(new.id) / "mod.json").read_text(encoding="utf-8"))["retarget"] == recipe
    again = ModManager(m.data_dir)
    assert again.get(new.id).retarget == recipe and again.get(new.id).enabled
    assert again.mods[2].id == new.id
    # the equipment window sees the target
    assert m.retarget_sources(new.id) == [B]
    scan = m.equipment_scans([m.equipment_job(new.id)])[new.id]
    assert {t.item for t in scan.targets} == {B} and Target("armor", "f/004/000", "helm") in scan.targets
    # deployed at the target's paths, the original's files gone from the game
    m.deploy()
    assert (m.game_dir / N / (HELM_B + ".mesh.241111606")).read_bytes() == MESH
    assert not (m.game_dir / N / (HELM_A + ".mesh.241111606")).exists()

    # the new mod moved again, keeping it on: compared with what its files first came from (A's) ...
    onward = m.create_retarget(new.id, B, THIRD, disable_original=False)
    assert onward.name == "Bunny → 시험 → 방어구 005-000" and new.enabled and onward.enabled
    assert {k: onward.retarget[k] for k in ("from", "to", "source_mod", "source_name")} == \
        {"from": "f/004/001", "to": "f/005/000", "source_mod": new.id, "source_name": "Bunny → 시험"}
    assert [N + HELM_C + ".mdf2.45", N + HELM_A + ".mdf2.45"] in onward.retarget["moves"]
    # ... and back where they came from it is a mod like any other
    home = m.create_retarget(new.id, B, A, disable_original=False)
    assert home.name == "Bunny → 시험 → 시험" and home.retarget == {}
    assert sorted(m.mod_files(home.id)) == sorted([
        N + HELM_A + ".mesh.241111606", N + HELM_A + ".mdf2.45", N + ALBD_A + ".241106027", N + HAIR_A])
    # some parts; a name already taken gets a number, said before the mod is made
    assert m.retarget_name(mod.id, B) == m.retarget_name(mod.id, Target("armor", "f/004/000", "helm")) == \
        "Bunny → 시험 (2)"
    third = m.create_retarget(mod.id, A, B, parts=["helm"])
    assert third.name == "Bunny → 시험 (2)" and third.enabled  # on, although the original is off
    assert third.retarget["parts"] == ["helm"]
    assert [x.id for x in m.mods] == [first.id, mod.id, third.id, new.id, home.id, onward.id, last.id]


def test_the_name_said_before_is_the_name_given(library):
    m, mod, _first, _last = library
    assert m.retarget_choices(A) == [(B, "시험 · Trial α")]  # every name of the design, to search by
    assert m.retarget_name(mod.id, B) == "Bunny → 시험"  # the mod gets the first one only
    assert m.create_retarget(mod.id, A, B, disable_original=False).name == "Bunny → 시험"
    assert m.retarget_name(mod.id, B) == "Bunny → 시험 (2)"
    assert m.retarget_name(mod.id, THIRD) == "Bunny → 방어구 005-000"  # (not in the catalog: by number)


def test_the_new_mod_is_on_unless_asked_otherwise(library):
    m, mod, _first, _last = library
    m.set_enabled(mod.id, False)  # the original is off: its equipment shows the game's look
    m.deploy()
    new = m.create_retarget(mod.id, A, B)
    assert new.enabled and not mod.enabled and m.dirty  # the new one changes the target all the same
    m.deploy()
    off = m.create_retarget(mod.id, A, B, enable=False)
    assert not off.enabled and not m.dirty  # nothing in the game changes
    m.set_enabled(mod.id, True)
    m.deploy()
    last = m.create_retarget(mod.id, A, B, enable=False)  # the original switched off: the game changes
    assert not last.enabled and not mod.enabled and m.dirty
    assert [ModManager(m.data_dir).get(x.id).enabled for x in (new, off, last)] == [True, False, False]


def test_armor_keys_need_the_body(library):
    m, mod, _first, _last = library
    bodiless_a, bodiless_b = Target("armor", "004/001"), Target("armor", "004/000")
    for source, target in ((bodiless_a, B), (A, bodiless_b), (bodiless_a, bodiless_b)):
        assert m.plan_retarget(mod.id, source, target).errors == [RETARGET_BODY]
        with pytest.raises(InstallError, match="체형까지"):
            m.create_retarget(mod.id, source, target)
    assert "옮길 수 없는 장비" in m.plan_retarget(mod.id, A, Target("armor", "002/000")).errors[0]  # innerwear
    assert len(m.mods) == 3


def test_the_command_line_adds_the_body(library, capsys):
    m, mod, _first, _last = library
    data = str(m.data_dir)
    assert main(["--data-dir", data, "retarget", "Bunny", "--from", "armor/004/001", "--to", "armor/004/000",
                 "--keep-original", "--no-deploy"]) == 0
    assert "시험 (armor/f/004/001) → 시험 (armor/f/004/000)" in capsys.readouterr().out
    assert main(["--data-dir", data, "retarget", "Bunny", "--to", "armor/004/000", "--no-deploy"]) == 0
    after = ModManager(data)
    made = [x for x in after.mods if x.retarget]
    assert [(x.retarget["from"], x.retarget["to"]) for x in made] == [("f/004/001", "f/004/000")] * 2
    run = after.run_materials(after.material_jobs([x.id for x in made]))
    assert not run.errors and set(run.results) == {x.id for x in made}  # the recipes read
    assert main(["--data-dir", data, "retarget", "Bunny", "--from", "armor/060/000", "--to", "armor/004/000",
                 "--dry-run"]) == 1  # a design the mod does not change: no body to take, so it is asked for
    assert RETARGET_BODY in capsys.readouterr().err


def test_the_command_line_moves_both_bodies_without_asking(library, tmp_path, capsys):
    m, _mod, _first, _last = library
    both = M.install_tree(m, tmp_path / "src" / "Both", {
        N + HELM_A + ".mesh.241111606": MESH, N + HELM_A + ".mdf2.45": mdf2("helm", ("BaseDielectricMap", ALBD_A)),
        N + MALE_HELM_A + ".mesh.241111606": MESH, N + MALE_HELM_A + ".mdf2.45": mdf2("helm")})
    assert m.retarget_sources(both.id) == [A, Target("armor", "m/004/001")]  # one design, two bodies
    data = str(m.data_dir)
    for to, line in (("armor/m/004/000", "(armor/m/004/001) → 시험 (armor/m/004/000)"),
                     ("armor/f/004/000", "(armor/f/004/001) → 시험 (armor/f/004/000)"),
                     ("armor/004/000", "(armor/f/004/001) → 시험 (armor/f/004/000)")):
        assert main(["--data-dir", data, "retarget", "Both", "--to", to, "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert line in out and "옮길 파일 4개" in out  # both bodies whichever was named
    M.install_tree(m, tmp_path / "src" / "Two", {N + HELM_A + ".mesh.241111606": MESH,
                                                 N + CH + "ch02/060/000/2/ch02_060_0002.mesh.241111606": MESH})
    with pytest.raises(SystemExit, match="원래 장비가 여러 개입니다.*armor/f/004/001, armor/m/060/000"):
        main(["--data-dir", data, "retarget", "Two", "--to", "armor/f/004/000", "--dry-run"])


def test_the_mods_files_taken_on_the_gui_thread(library, tmp_path, monkeypatch):
    """The dialog's workers get the mod's files from the GUI thread: they never read the mod's state or fill
    the caches the GUI thread clears when options change."""
    m, mod, _first, _last = library
    files = m.equipment_job(mod.id).files
    source = tmp_path / "src" / "Blades.pak"
    pak.write_pak(blades_entries(), source)
    with m.prepare_install(source) as session:
        blades = m.install_package(session, session.variants)
    pak_files = m.equipment_job(blades.id).files
    m._invalidate(mod.id)
    m._invalidate(blades.id)
    scans = dict(m._load_equip_scans())

    def no(*args, **kwargs):
        raise AssertionError("read on a worker thread")

    monkeypatch.setattr(m, "mod_entries", no)
    monkeypatch.setattr(m, "equipment_job", no)
    assert m.retarget_sources(mod.id, files) == [A]
    assert m.retarget_sources(blades.id, pak_files) == [Target("weapon", "02/000029")]
    plan = m.plan_retarget(mod.id, A, B, files={rel: (path, rel) for rel, path in files.items()})
    assert plan.ok and len(plan.moves) == 4
    assert m.plan_retarget(blades.id, Target("weapon", "02/000029"), Target("weapon", "02/000028"),
                           files=pak_files).ok
    assert mod.id not in m._entries_cache and blades.id not in m._entries_cache
    assert m._load_equip_scans() == scans


# ---- the material check of a moved mod -------------------------------------------------------------
def test_the_material_check_compares_with_the_original_equipments_files(library, types):
    m, mod, _first, _last = library
    new = m.create_retarget(mod.id, A, B)
    onward = m.create_retarget(new.id, B, THIRD)  # moved again: still compared with A's files (below)
    (job,) = m.material_jobs([new.id])
    assert job.retarget == new.retarget and "ch03_004_0013.mdf2" in job.provided  # the old names too

    # checked like any other mod it would be compared with the target's own files: nothing to do
    index = GameIndex(m.game_dir, m.data_dir / "cache")
    plain = check_mod(job.files, index, None, provided=job.provided)
    assert {r.kind: r.state for r in plain} == {"mdf2": "current", "user": "current"}

    run = m.run_materials([job])
    m.finish_materials(run, [job])
    found = results_by_kind(m, new.id)
    assert found["mdf2"].state == "fixable" and found["mdf2"].fixed == ["helm"]
    assert any(N + HELM_A + ".mdf2.45" in n and "DetailMaskMap" in n for n in found["mdf2"].notes)
    assert found["user"].state == "fixable" and found["user"].rsz[0]["method"] == "원본 교체"
    assert m.material_state(new.id) == "stale"

    run = m.run_materials([job], repair=True)
    m.finish_materials(run, [job])
    assert m.material_state(new.id) == "repaired"
    entries = m.mod_entries(new.id)
    # the material: the original equipment's structure, the mod's (moved) texture
    path, key = entries[N + HELM_B + ".mdf2.45"]
    assert key.startswith("matfix/")
    repaired = extract_material(Mdf2File(path.read_bytes(), 45), 0)
    assert {t["type"]: t["path"] for t in repaired["textures"]} == {"BaseDielectricMap": ALBD_B,
                                                                   "DetailMaskMap": NULL_MASK}
    # the hair list: the game's own file with the mod's values, pointing at the moved files - not back at A's
    path, key = entries[N + HAIR_B]
    data = path.read_bytes()
    assert key.startswith("matfix/") and rsz.parse(data).resources() == {HELM_B + ".mesh", HELM_B + ".mdf2"}
    values, info = R.fields_of(data)
    assert info.insts[2] == (R.PARAM, R.NEW_CRC) and values[(2, "_Scale")] == R.f32(3.0)
    assert string_of(values[(2, "_Mesh")]) == HELM_B + ".mesh"
    assert (m.files_dir(new.id) / (N + HAIR_B)).read_bytes() == \
        hair_list(R.NO_CHILD_CRC, SHORT, scale=3.0, refs=(HELM_B, HELM_B))  # the mod's own file stays

    jobs = m.material_jobs([onward.id])
    m.finish_materials(m.run_materials(jobs, repair=True), jobs)
    found = results_by_kind(m, onward.id)
    assert found["mdf2"].state == "fixable" and any(N + HELM_A + ".mdf2.45" in n for n in found["mdf2"].notes)
    data = m.mod_entries(onward.id)[N + HAIR_C][0].read_bytes()
    assert found["user"].state == "fixable" and rsz.parse(data).resources() == {HELM_C + ".mesh", HELM_C + ".mdf2"}


def test_a_moved_mod_repaired_and_moved_again(library, tmp_path, game):
    """After its repair the moved material has the version number the game uses: moved again, it is still
    compared with the original equipment's material, so the update of that one is found (it is the one
    the files were made for)."""
    m, _mod, _first, _last = library
    old = assemble_mdf2([M.material("helm", [("BaseDielectricMap", ALBD_A)], [("Color", (1.0, 1.0, 1.0, 1.0))])], 40)
    mod = M.install_tree(m, tmp_path / "src" / "Old", {N + HELM_A + ".mesh.241111606": MESH,
                                                       N + HELM_A + ".mdf2.40": old, N + ALBD_A + ".241106027": TEX})
    first = m.create_retarget(mod.id, A, B)
    assert [N + HELM_B + ".mdf2.40", N + HELM_A + ".mdf2.40"] in first.retarget["moves"]
    jobs = m.material_jobs([first.id])
    m.finish_materials(m.run_materials(jobs, repair=True), jobs)
    assert N + HELM_B + ".mdf2.45" in m.mod_entries(first.id)  # the repaired copy, at the game's version
    onward = m.create_retarget(first.id, B, THIRD)
    assert onward.retarget["from"] == "f/004/001"
    assert [N + HELM_C + ".mdf2.45", N + HELM_A + ".mdf2.45"] in onward.retarget["moves"]
    assert retarget_aliases(onward.retarget).old_path(N + HELM_C + ".mdf2.45") == N + HELM_A + ".mdf2.45"

    # an update gives the original equipment's material another slot: both mods are behind it now
    newer = mdf2("helm", ("BaseDielectricMap", ALBD_A), ("DetailMaskMap", "Art/Common/new_MSK.tex"),
                 ("AnotherNewMap", "Art/Common/another.tex"))
    M.write_game_pak(game / "re_chunk_000.pak.patch_001.pak", {N + HELM_A + ".mdf2.45": newer})
    m.game_version(refresh=True)
    for who in (first, onward):
        run = m.run_materials(m.material_jobs([who.id]))
        (result,) = run.results[who.id]
        assert result.state == "fixable" and any("AnotherNewMap" in n for n in result.notes), who.name


def test_a_chain_only_where_the_first_move_ended():
    first = {"kind": "armor", "from": "f/004/001", "to": "f/004/000", "parts": ["helm"],
             "moves": [[N + HELM_B + ".mdf2.40", N + HELM_A + ".mdf2.40"],
                       [N + "streaming/" + HELM_B + ".mesh.241111606", N + "streaming/" + HELM_A + ".mesh.241111606"],
                       [N + HELM_B + ".mesh.241111606", N + HELM_A + ".mesh.241111606"]],
             "pak_moves": []}
    then = {"kind": "armor", "from": "f/004/000", "to": "f/005/000", "parts": ["helm", "body"],
            "moves": [[N + HELM_C + ".MDF2.45", N + HELM_B + ".MDF2.45"],
                      [N + "streaming/" + HELM_C + ".mesh.241111606", N + "streaming/" + HELM_B + ".mesh.241111606"],
                      [N + HELM_C + ".mesh.241111606", N + HELM_B + ".mesh.241111606"],
                      [N + HAIR_C, N + HAIR_B]],  # (not the first move's: the mod had B's own)
            "pak_moves": []}
    chained = _chained_recipe(first, then)
    assert chained["from"] == "f/004/001" and chained["parts"] == ["helm"]
    assert chained["moves"] == [
        [N + HELM_C + ".MDF2.45", N + HELM_A + ".MDF2.45"],  # the spelling it has now, at the first's path
        [N + "streaming/" + HELM_C + ".mesh.241111606", N + "streaming/" + HELM_A + ".mesh.241111606"],
        [N + HELM_C + ".mesh.241111606", N + HELM_A + ".mesh.241111606"],
        [N + HAIR_C, N + HAIR_B]]
    # moved on from somewhere the first move did not put the files: nothing to chain
    elsewhere = {**then, "from": "f/005/000", "to": "f/004/000"}
    assert _chained_recipe(first, elsewhere) == elsewhere
    assert _chained_recipe({**first, "to": "004/000"}, then) == then  # (a first recipe that does not read)
    assert _chained_recipe(first, {**then, "to": "m/004/001"}) == {}  # back at the original's paths


def test_updating_a_moved_mod_from_an_archive(library, types, tmp_path):
    """The user keeps only the moved mod under the original's name and installs a new version of the
    original over it: its files are the archive's again, at the original equipment's paths."""
    m, mod, _first, _last = library
    new = m.create_retarget(mod.id, A, B)
    m.remove(mod.id)
    m.rename(new.id, "Bunny")
    src = tmp_path / "src2" / "Bunny"
    for rel, data in bunny().items():
        write(src / rel, data)
    with m.prepare_install(src) as session:
        existing = m.find_existing(session.variants[0].name, session.source.name)
        assert existing is new
        updated = m.install_package(session, session.variants, replace_id=existing.id)
    assert updated.retarget == {} and ModManager(m.data_dir).get(updated.id).retarget == {}
    jobs = m.material_jobs([updated.id])
    assert jobs[0].retarget == {}
    m.finish_materials(m.run_materials(jobs, repair=True), jobs)
    path, key = m.mod_entries(updated.id)[N + HAIR_A]
    assert key.startswith("matfix/") and rsz.parse(path.read_bytes()).resources() == \
        {HELM_A + ".mesh", HELM_A + ".mdf2"}  # the archive's paths, not the target's


def test_the_diagnostics_report_of_a_moved_mod(library, types, tmp_path, game):
    """The report pairs a moved RSZ file with the game file its check compared it with: the original
    equipment's, not the target's own (most helms have their own hair list)."""
    m, mod, _first, _last = library
    b_own = hair_list(R.NO_CHILD_CRC, SHORT, scale=7.0, refs=(HELM_B, HELM_B))
    a_game = hair_list(R.NEW_CRC, R.PARAM_FIELDS, scale=1.0, hide=0, new=1)
    M.write_game_pak(game / "re_chunk_000.pak.patch_001.pak", {N + HAIR_B: b_own})
    m.game_version(refresh=True)
    new = m.create_retarget(mod.id, A, B)
    (job,) = m.material_jobs([new.id])
    m.finish_materials(m.run_materials([job]), [job])
    assert results_by_kind(m, new.id)["user"].state == "fixable"  # against A's newer layout
    out = tmp_path / "report.zip"
    assert m.export_material_report(job, out) == 1
    with zipfile.ZipFile(out) as zf:
        assert zf.read(f"game/{N + HAIR_B}") == a_game
        assert zf.read(f"mod/{N + HAIR_B}") == (m.files_dir(new.id) / (N + HAIR_B)).read_bytes()
        assert rsz.parse(zf.read(f"restored/{N + HAIR_B}")).resources() == {HELM_A + ".mesh", HELM_A + ".mdf2"}
        report = json.loads(zf.read("report.json"))
    assert report["compared"][N + HAIR_B].lower() == (N + HAIR_A).lower()
    assert "원래 장비의 게임 파일" in report["retarget"]["note"]
    assert report["retarget"]["recipe"]["from"] == "f/004/001" and "moves" not in report["retarget"]["recipe"]

    # a mod like any other has neither
    (plain,) = m.material_jobs([_first.id]) or [None]
    if plain is not None:
        m.export_material_report(plain, tmp_path / "plain.zip")
        with zipfile.ZipFile(tmp_path / "plain.zip") as zf:
            assert "retarget" not in json.loads(zf.read("report.json"))


def test_the_diagnostics_report_of_a_moved_pak(library, tmp_path, game):
    """In a .pak a moved prefab is paired with the original model's prefab by its old hash."""
    m, _mod, _first, _last = library
    wpfb_a = WPFB_B.replace("0028", "0029")
    a_game = pfb_file(ITEM_A + ".mesh", ITEM_A + ".mdf2", "Art/Model/Item/it02/00/0029/extra.chain2")
    M.write_game_pak(game / "re_chunk_000.pak.patch_001.pak", {wpfb_a: a_game})
    m.game_version(refresh=True)
    own = pfb_file(ITEM_A + ".mesh", ITEM_A + ".mdf2")
    source = tmp_path / "src" / "Blades.pak"
    pak.write_pak(blades_entries() + [(pak.path_hash(wpfb_a), own, len(own), pak.COMPRESSION_NONE)], source)
    with m.prepare_install(source) as session:
        blades = m.install_package(session, session.variants)
    new = m.create_retarget(blades.id, Target("weapon", "02/000029"), Target("weapon", "02/000028"))
    assert [pak.path_hash(WPFB_B), pak.path_hash(wpfb_a)] in new.retarget["pak_moves"]
    (job,) = m.material_jobs([new.id])
    out = tmp_path / "report.zip"
    assert m.export_material_report(job, out) == 1
    label = f"Blades.pak_{pak.path_hash(WPFB_B):016X}.pfb"
    with zipfile.ZipFile(out) as zf:
        assert zf.read(f"game/{label}") == a_game  # (the target has its own prefab in the game: not that)
        assert rsz.parse(zf.read(f"restored/{label}")).resources() == {ITEM_A + ".mesh", ITEM_A + ".mdf2"}
        assert rsz.parse(zf.read(f"mod/{label}")).resources() == {ITEM_B + ".mesh", ITEM_B + ".mdf2"}
        report = json.loads(zf.read("report.json"))
    assert report["compared"][f"Blades.pak#{pak.path_hash(WPFB_B):016X}.pfb"] == f"#{pak.path_hash(wpfb_a):016X}"


def test_a_recipe_that_does_not_read_is_not_checked(library):
    m, mod, _first, _last = library
    new = m.create_retarget(mod.id, A, B)
    with pytest.raises(ValueError, match="다른 장비로 옮긴 기록"):
        retarget_aliases({**new.retarget, "to": "f/002/000"})
    with pytest.raises(ValueError):
        retarget_aliases({"kind": "armor"})
    new.retarget = {**new.retarget, "moves": [["only one"]]}
    jobs = m.material_jobs([new.id])
    run = m.run_materials(jobs)
    assert "다른 장비로 옮긴 기록" in run.errors[new.id] and new.id not in run.results


def test_a_pak_weapon_mod(library, tmp_path):
    m, _mod, _first, _last = library
    source = tmp_path / "src" / "Blades.pak"
    pak.write_pak(blades_entries(), source)
    with m.prepare_install(source) as session:
        blades = m.install_package(session, session.variants)
    weapon = Target("weapon", "02/000029")
    assert m.retarget_sources(blades.id) == [weapon]
    target = Target("weapon", "02/000028")
    new = m.create_retarget(blades.id, weapon, target)
    assert new.name == "Blades → 첫걸음쌍검Ⅰ" and m.mod_files(new.id) == ["Blades.pak"]
    assert new.retarget["parts"] == ["main", "sub"] and len(new.retarget["pak_moves"]) == 3
    moved = {pak.path_hash(N + ITEM_B + ".mesh.241111606"), pak.path_hash(N + ITEM_B + ".mdf2.45"),
             pak.path_hash(N + WTEX_B + ".241106027")}
    built = pak.PakFile(m.files_dir(new.id) / "Blades.pak")
    assert set(built.order) == moved and {h for h, _old in new.retarget["pak_moves"]} == moved
    assert m.retarget_sources(new.id) == [target]

    (job,) = m.material_jobs([new.id])
    assert check_mod(job.files, GameIndex(m.game_dir, m.data_dir / "cache"), None)[0].state == "current"
    run = m.run_materials([job], repair=True)
    m.finish_materials(run, [job])
    (result,) = m.material_results(new.id)
    assert result.state == "fixable" and result.fixed == ["blade"]  # against the original model's material
    fixed = pak.PakFile(m.mod_entries(new.id)["Blades.pak"][0])
    assert set(fixed.order) == moved
    repaired = extract_material(Mdf2File(fixed.read_hash(pak.path_hash(N + ITEM_B + ".mdf2.45")), 45), 0)
    assert {t["type"]: t["path"] for t in repaired["textures"]} == {"BaseDielectricMap": WTEX_B,
                                                                   "DetailMaskMap": NULL_MASK}


# ---- the command line ---------------------------------------------------------------------------------
def test_the_command_line(library, capsys):
    m, mod, _first, _last = library
    data = str(m.data_dir)
    assert main(["--data-dir", data, "retarget", "Bunny", "--to", "armor/f/004/000", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Bunny: 시험 (armor/f/004/001) → 시험 (armor/f/004/000)" in out
    assert "옮길 파일 4개 · 그대로 넣을 파일 0개 · 넣지 않을 파일 2개" in out and "--dry-run" in out
    assert "참고: 게임 폴더(natives) 밖의 파일 1개" in out
    assert len(ModManager(m.data_dir).mods) == 3

    assert main(["--data-dir", data, "retarget", "Bunny", "--to", "weapon/02/000028", "--dry-run"]) == 1
    assert "방어구는 방어구로" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="장비를 알 수 없습니다: nothing"):
        main(["--data-dir", data, "retarget", "Bunny", "--to", "nothing"])
    assert main(["--data-dir", data, "retarget", "Last", "--to", "armor/f/004/000"]) == 1
    assert "옮길 수 있는 방어구나 무기를 바꾸지 않습니다" in capsys.readouterr().err

    assert main(["--data-dir", data, "retarget", "Bunny", "--to", "armor/f/004/000/helm", "--parts", "머리",
                 "--keep-original", "--no-deploy"]) == 0
    out = capsys.readouterr().out
    assert "부위: 머리" in out and "새 모드: Bunny → 시험  (4개 파일, 켜짐)" in out and "원래 모드를 껐습니다" not in out
    after = ModManager(m.data_dir)
    new = after.find("Bunny → 시험")
    assert new.retarget["parts"] == ["helm"] and after.find("Bunny").enabled

    assert main(["--data-dir", data, "retarget", "2", "--from", "armor/f/004/001", "--to", "armor/f/004/000",
                 "--no-deploy"]) == 0
    assert "원래 모드를 껐습니다: Bunny" in capsys.readouterr().out
    after = ModManager(m.data_dir)
    assert not after.find("Bunny").enabled and after.find("Bunny → 시험 (2)").enabled


def test_the_search_for_a_crashing_mod_blocks_it(library, capsys):
    m, _mod, _first, _last = library
    data = str(m.data_dir)
    write(m.data_dir / bisect.SESSION_FILE, json.dumps({"format": bisect.SESSION_FORMAT}))
    assert main(["--data-dir", data, "retarget", "Bunny", "--to", "armor/f/004/000", "--no-deploy"]) == 1
    assert bisect.BLOCKED in capsys.readouterr().err
    assert main(["--data-dir", data, "retarget", "Bunny", "--to", "armor/f/004/000", "--dry-run"]) == 0  # (changes nothing)
    assert len(ModManager(m.data_dir).mods) == 3


# ---- each part to its own equipment (부위마다 다른 장비로) -------------------------------------------------
BODY_A, BODY_C = CH + "ch03/004/001/2/ch03_004_0012", CH + "ch03/005/000/2/ch03_005_0002"
BODY_ALBD_A = CH + "ch03/004/001/2/textures/duo_body_ALBD.tex"
BODY_ALBD_C = CH + "ch03/005/000/2/textures/duo_body_ALBD.tex"
BODY_PFB_C = N + "GameDesign/Equip/_Prefab/Armor/Female/005/000/Body/ch03_005_0002.pfb.18"
FOURTH = Target("armor", "f/006/000")  # not in the catalog either
HELM_D = CH + "ch03/006/000/3/ch03_006_0003"
ALBD_D = CH + "ch03/006/000/3/textures/bunny_ALBD.tex"
HAIR_D = CH + "ch03/006/000/3/hair/ch03_006_0003_HairAdjustList.user.3"
PFB_D = N + "GameDesign/Equip/_Prefab/Armor/Female/006/000/Helm/ch03_006_0003.pfb.18"
MIXED = {"helm": B, "body": THIRD}  # the helm to the catalog's "시험", the body to a design it does not name
MIXED_MOVES = {  # the original's file -> where the move puts it
    N + HELM_A + ".mesh.241111606": N + HELM_B + ".mesh.241111606",
    N + HELM_A + ".mdf2.45": N + HELM_B + ".mdf2.45",
    N + ALBD_A + ".241106027": N + ALBD_B + ".241106027",
    N + HAIR_A: N + HAIR_B,
    N + BODY_A + ".mesh.241111606": N + BODY_C + ".mesh.241111606",
    N + BODY_A + ".mdf2.45": N + BODY_C + ".mdf2.45",
    N + BODY_ALBD_A + ".241106027": N + BODY_ALBD_C + ".241106027",
}


def duo() -> dict:
    """A helm and a body for the female Trial (type 2): the helm's hair settings point at the helm's model
    and at the body's material (a file of the other part)."""
    return {
        N + HELM_A + ".mesh.241111606": MESH,
        N + HELM_A + ".mdf2.45": mdf2("helm", ("BaseDielectricMap", ALBD_A)),
        N + ALBD_A + ".241106027": TEX,
        N + HAIR_A: hair_list(R.NO_CHILD_CRC, SHORT, scale=3.0, refs=(HELM_A, BODY_A)),
        N + BODY_A + ".mesh.241111606": MESH,
        N + BODY_A + ".mdf2.45": mdf2("body", ("BaseDielectricMap", BODY_ALBD_A)),
        N + BODY_ALBD_A + ".241106027": TEX + b"body",
        AVP_A: b"USR\x00" + bytes(60),
    }


@pytest.fixture
def duo_library(library, game, tmp_path):
    """The game also has the second target's female body (and a fourth design's helm), the original's current
    body material, and its hair settings pointing at the helm's model and the body's material; the Duo mod."""
    m, _mod, _first, _last = library
    M.write_game_pak(game / "re_chunk_000.pak.patch_001.pak", {
        BODY_PFB_C: pfb_file(BODY_C + ".mesh", BODY_C + ".mdf2"),
        PFB_D: pfb_file(HELM_D + ".mesh", HELM_D + ".mdf2"),
        N + BODY_A + ".mdf2.45": current_mdf2("body", BODY_ALBD_A),
        N + HAIR_A: hair_list(R.NEW_CRC, R.PARAM_FIELDS, scale=1.0, hide=0, new=1, refs=(HELM_A, BODY_A))})
    m.game_version(refresh=True)
    return m, M.install_tree(m, tmp_path / "src" / "Duo", duo())


def small_registry(monkeypatch) -> None:
    """The ``types`` fixture from here on: the names are read before (the catalog keeps them, it would have
    none with this registry)."""
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", R.registry())
    monkeypatch.setattr(rsz.TypeRegistry, "_from_file", False)


def by_rel(m, mod_id) -> dict:
    return {r.rel: r for r in m.material_results(mod_id)}


def textures_of(path) -> dict:
    return {t["type"]: t["path"] for t in extract_material(Mdf2File(path.read_bytes(), 45), 0)["textures"]}


def test_each_part_to_its_own_equipment(duo_library, monkeypatch, tmp_path):
    """The helm to one design and the body to another in one new mod: the files at each part's target, the
    recipe with each part's target, the name with both, and a material check that compares each moved file
    with the original equipment's game file and repairs the hair settings keeping each part's new paths."""
    m, duo_mod = duo_library
    plan = m.plan_retarget(duo_mod.id, A, MIXED)
    assert plan.ok, plan.errors
    assert plan.mixed and plan.targets == {"helm": retarget.Slot.of(B), "body": retarget.Slot.of(THIRD)}
    assert {mv.old: mv.new for mv in plan.moves} == MIXED_MOVES
    assert {mv.new: mv.target for mv in plan.moves}[N + BODY_C + ".mdf2.45"] == retarget.Slot.of(THIRD)
    assert dict(plan.dropped).keys() == {AVP_A}
    assert m.retarget_name(duo_mod.id, MIXED) == "Duo → 머리 시험 · 몸통 방어구 005-000"

    new = m.create_retarget(duo_mod.id, A, MIXED)
    assert new.name == "Duo → 머리 시험 · 몸통 방어구 005-000" and new.enabled and not duo_mod.enabled
    assert sorted(m.mod_files(new.id)) == sorted(MIXED_MOVES.values())
    recipe = new.retarget
    assert {k: recipe[k] for k in ("format", "kind", "from", "to", "parts", "source_mod")} == {
        "format": 2, "kind": "armor", "from": "f/004/001", "to": {"helm": "f/004/000", "body": "f/005/000"},
        "parts": ["helm", "body"], "source_mod": duo_mod.id}
    assert sorted(map(tuple, recipe["moves"])) == sorted((new_, old) for old, new_ in MIXED_MOVES.items())
    assert ModManager(m.data_dir).get(new.id).retarget == recipe
    # each moved file points at the others' new paths, the hair settings at both targets
    assert rsz.parse((m.files_dir(new.id) / (N + HAIR_B)).read_bytes()).resources() == \
        {HELM_B + ".mesh", BODY_C + ".mdf2"}
    assert textures_of(m.files_dir(new.id) / (N + BODY_C + ".mdf2.45")) == {"BaseDielectricMap": BODY_ALBD_C}
    assert m.retarget_sources(new.id) == [B, THIRD]  # the equipment window sees both targets

    small_registry(monkeypatch)
    (job,) = m.material_jobs([new.id])
    m.finish_materials(m.run_materials([job]), [job])
    found = by_rel(m, new.id)
    helm, body, hair = found[N + HELM_B + ".mdf2.45"], found[N + BODY_C + ".mdf2.45"], found[N + HAIR_B]
    assert helm.state == body.state == hair.state == "fixable"
    assert any(N + HELM_A + ".mdf2.45" in n and "DetailMaskMap" in n for n in helm.notes)  # A's, not B's own
    assert any(N + BODY_A + ".mdf2.45" in n and "DetailMaskMap" in n for n in body.notes)  # (THIRD has none)
    assert hair.rsz[0]["method"] == "원본 교체"
    out = tmp_path / "report.zip"  # the diagnostics report pairs the hair settings with the original's too
    assert m.export_material_report(job, out) == 1
    with zipfile.ZipFile(out) as zf:
        report = json.loads(zf.read("report.json"))
        assert rsz.parse(zf.read(f"restored/{N + HAIR_B}")).resources() == {HELM_A + ".mesh", BODY_A + ".mdf2"}
    assert report["compared"][N + HAIR_B].lower() == (N + HAIR_A).lower()
    assert "(f/004/001 → 머리 f/004/000 · 몸통 f/005/000)" in report["retarget"]["note"]

    m.finish_materials(m.run_materials([job], repair=True), [job])
    assert m.material_state(new.id) == "repaired"
    entries = m.mod_entries(new.id)
    path, key = entries[N + BODY_C + ".mdf2.45"]
    assert key.startswith("matfix/")
    assert textures_of(path) == {"BaseDielectricMap": BODY_ALBD_C, "DetailMaskMap": NULL_MASK}
    path, key = entries[N + HAIR_B]
    data = path.read_bytes()
    assert key.startswith("matfix/") and rsz.parse(data).resources() == {HELM_B + ".mesh", BODY_C + ".mdf2"}
    values, _info = R.fields_of(data)
    assert string_of(values[(2, "_Mesh")]) == HELM_B + ".mesh" and values[(2, "_Scale")] == R.f32(3.0)


def test_one_part_of_a_mixed_move_moved_on(duo_library, monkeypatch):
    """The helm of a mod moved part by part, moved on again from its target: compared with the original
    equipment's files still, the body files it keeps where the first move put them too."""
    m, duo_mod = duo_library
    new = m.create_retarget(duo_mod.id, A, MIXED)
    onward = m.create_retarget(new.id, B, {"helm": FOURTH}, disable_original=False)
    assert onward.name == "Duo → 머리 시험 · 몸통 방어구 005-000 → 방어구 006-000"
    files = sorted(m.mod_files(onward.id))
    assert files == sorted([N + HELM_D + ".mesh.241111606", N + HELM_D + ".mdf2.45", N + ALBD_D + ".241106027",
                            N + HAIR_D, N + BODY_C + ".mdf2.45", N + BODY_ALBD_C + ".241106027"])  # (kept)
    recipe = onward.retarget
    assert {k: recipe[k] for k in ("from", "to", "parts", "source_mod")} == {
        "from": "f/004/001", "to": {"helm": "f/006/000", "body": "f/005/000"}, "parts": ["helm", "body"],
        "source_mod": new.id}
    assert sorted(map(tuple, recipe["moves"])) == sorted([
        (N + HELM_D + ".mesh.241111606", N + HELM_A + ".mesh.241111606"),
        (N + HELM_D + ".mdf2.45", N + HELM_A + ".mdf2.45"), (N + ALBD_D + ".241106027", N + ALBD_A + ".241106027"),
        (N + HAIR_D, N + HAIR_A),
        (N + BODY_C + ".mdf2.45", N + BODY_A + ".mdf2.45"),  # kept where the first move put them
        (N + BODY_ALBD_C + ".241106027", N + BODY_ALBD_A + ".241106027")])
    aliases = retarget_aliases(recipe)
    assert aliases.old_path(N + HELM_D + ".mdf2.45") == N + HELM_A + ".mdf2.45"
    # moved on to where the first move put the other part: one target for both
    again = m.create_retarget(new.id, B, THIRD, disable_original=False)
    assert again.retarget["to"] == "f/005/000" and again.retarget["parts"] == ["helm", "body"]
    assert [N + HELM_C + ".mdf2.45", N + HELM_A + ".mdf2.45"] in again.retarget["moves"]

    small_registry(monkeypatch)
    jobs = m.material_jobs([onward.id])
    m.finish_materials(m.run_materials(jobs, repair=True), jobs)
    found = by_rel(m, onward.id)
    assert any(N + HELM_A + ".mdf2.45" in n for n in found[N + HELM_D + ".mdf2.45"].notes)
    assert any(N + BODY_A + ".mdf2.45" in n for n in found[N + BODY_C + ".mdf2.45"].notes)
    data = m.mod_entries(onward.id)[N + HAIR_D][0].read_bytes()
    assert found[N + HAIR_D].state == "fixable" and rsz.parse(data).resources() == {HELM_D + ".mesh", BODY_C + ".mdf2"}


def test_one_part_of_a_mixed_pak_moved_on(duo_library, monkeypatch, tmp_path):
    """The same in a .pak: the body entries kept where the first move put them are moves by their names
    too, so the repaired hair settings point at the body's new material, not at the original's."""
    m, _loose = duo_library
    names = sorted(set(duo()) | set(MIXED_MOVES.values()) | {
        N + HELM_D + ".mesh.241111606", N + HELM_D + ".mdf2.45", N + ALBD_D + ".241106027", N + HAIR_D})
    real = equip_pathlist.PathIndex
    monkeypatch.setattr(equip_pathlist, "PathIndex", lambda cache=None, paths=None: real(None, paths=names))
    source = tmp_path / "src" / "Duo.pak"
    pak.write_pak([(pak.path_hash(p), d, len(d), pak.COMPRESSION_NONE) for p, d in duo().items()], source)
    with m.prepare_install(source) as session:
        duo_pak = m.install_package(session, session.variants)
    new = m.create_retarget(duo_pak.id, A, MIXED)
    onward = m.create_retarget(new.id, B, {"helm": FOURTH}, disable_original=False)
    recipe = onward.retarget
    assert [N + BODY_C + ".mdf2.45", N + BODY_A + ".mdf2.45"] in recipe["moves"]
    assert [pak.path_hash(N + BODY_C + ".mdf2.45"), pak.path_hash(N + BODY_A + ".mdf2.45")] in recipe["pak_moves"]

    small_registry(monkeypatch)
    jobs = m.material_jobs([onward.id])
    m.finish_materials(m.run_materials(jobs, repair=True), jobs)
    data = pak.PakFile(m.mod_entries(onward.id)["Duo.pak"][0]).read_hash(pak.path_hash(N + HAIR_D))
    assert rsz.parse(data).resources() == {HELM_D + ".mesh", BODY_C + ".mdf2"}


def test_chains_of_moves_part_by_part():
    """_chained_recipe with a part -> target map on either side."""
    a, b1, b2, c = "f/004/001", "f/004/000", "f/005/000", "f/006/000"
    body_a, body_b2 = N + BODY_A + ".mdf2.45", N + BODY_C + ".mdf2.45"
    first = {"kind": "armor", "from": a, "to": {"helm": b1, "body": b2}, "parts": ["helm", "body"],
             "moves": [[N + HELM_B + ".mdf2.45", N + HELM_A + ".mdf2.45"], [body_b2, body_a]], "pak_moves": []}
    then = {"kind": "armor", "from": b1, "to": c, "parts": list(retarget.ARMOR_PARTS),
            "moves": [[N + HELM_D + ".mdf2.45", N + HELM_B + ".mdf2.45"]], "pak_moves": []}
    chained = _chained_recipe(first, then)  # (every part of the second move: the body was not where it moved from)
    assert {k: chained[k] for k in ("from", "to", "parts", "moves")} == {
        "from": a, "to": {"helm": c, "body": b2}, "parts": ["helm", "body"],
        "moves": [[N + HELM_D + ".mdf2.45", N + HELM_A + ".mdf2.45"]]}
    assert _chained_recipe(first, then, kept=[body_b2, N + "Art/custom.tex"])["moves"][1:] == [[body_b2, body_a]]
    assert _chained_recipe(first, {**then, "from": "f/099/000"}) == {**then, "from": "f/099/000"}  # not moved there
    home = {**then, "to": a, "moves": [[N + HELM_A + ".mdf2.45", N + HELM_B + ".mdf2.45"]]}
    assert _chained_recipe(first, home) == {}
    assert _chained_recipe(first, {**home, "to": {"helm": a}, "parts": ["helm"]}) == {}
    kept = _chained_recipe(first, {**home, "to": {"helm": a}, "parts": ["helm"]}, kept=[body_b2])
    assert {k: kept[k] for k in ("to", "parts", "moves")} == {"to": b2, "parts": ["body"], "moves": [[body_b2, body_a]]}
    retarget_aliases(chained)  # (reads)

    # the first moved every part to one design, the second each part to its own
    uniform = {**first, "to": b1, "parts": ["helm", "body"], "moves": first["moves"][:1]}
    split = {"kind": "armor", "from": b1, "to": {"helm": c, "body": b2}, "parts": ["helm", "body"],
             "moves": [[N + HELM_D + ".mdf2.45", N + HELM_B + ".mdf2.45"]], "pak_moves": []}
    chained = _chained_recipe(uniform, split)
    assert (chained["from"], chained["to"], chained["parts"]) == (a, {"helm": c, "body": b2}, ["helm", "body"])
    assert chained["moves"] == [[N + HELM_D + ".mdf2.45", N + HELM_A + ".mdf2.45"]]
    # the parts the first move did not take go where the second one sent them only by path (one target)
    only_waist = {**split, "to": {"waist": c, "leg": b2}, "parts": ["waist", "leg"]}
    assert _chained_recipe({**uniform, "parts": ["helm"]}, only_waist) == only_waist  # (two: cannot be said)
    single = _chained_recipe({**uniform, "parts": ["helm"]}, {**only_waist, "to": {"waist": c}, "parts": ["waist"]})
    assert (single["to"], single["parts"]) == (c, [])


def test_a_recipe_of_v2_4_0(library, monkeypatch):
    """A recipe stored by v2.4.0 (format 1, one target): read by the material check, and moved on part by part."""
    m, mod, _first, _last = library
    new = m.create_retarget(mod.id, A, B)
    new.retarget = {  # as v2.4.0 wrote it
        "format": 1, "kind": "armor", "from": "f/004/001", "to": "f/004/000",
        "parts": ["helm", "body", "arm", "waist", "leg", "slinger"], "source_mod": mod.id, "source_name": "Bunny",
        "moves": [[N + HELM_B + ".mesh.241111606", N + HELM_A + ".mesh.241111606"],
                  [N + HELM_B + ".mdf2.45", N + HELM_A + ".mdf2.45"], [N + ALBD_B + ".241106027", N + ALBD_A + ".241106027"],
                  [N + HAIR_B, N + HAIR_A]], "pak_moves": []}
    m._write_mod_meta(new)
    m.save()
    m = ModManager(m.data_dir)
    onward = m.create_retarget(new.id, B, {"helm": THIRD})  # (one part: a move of it to that one)
    assert onward.name == "Bunny → 시험 → 방어구 005-000"
    assert {k: onward.retarget[k] for k in ("format", "from", "to", "parts")} == {
        "format": 2, "from": "f/004/001", "to": "f/005/000", "parts": ["helm"]}
    assert [N + HELM_C + ".mdf2.45", N + HELM_A + ".mdf2.45"] in onward.retarget["moves"]

    small_registry(monkeypatch)
    jobs = m.material_jobs([new.id, onward.id])
    assert jobs[0].retarget["format"] == 1
    m.finish_materials(m.run_materials(jobs), jobs)
    for who in (new, onward):
        found = results_by_kind(m, who.id)
        assert found["mdf2"].state == "fixable" and any(N + HELM_A + ".mdf2.45" in n for n in found["mdf2"].notes)
        assert found["user"].state == "fixable", who.name


def test_part_by_part_targets_that_cannot_be(library, tmp_path):
    m, mod, _first, _last = library
    other_body = Target("armor", "m/005/000")
    plan = m.plan_retarget(mod.id, A, {"helm": B, "body": other_body})
    assert plan.errors == ["몸통: 원래 장비와 체형이 다른 장비입니다 (armor/m/005/000)"] and plan.bad_parts == {"body"}
    assert plan.targets == {"helm": retarget.Slot.of(B), "body": retarget.Slot.of(other_body)} and not plan.ok
    assert m.plan_retarget(mod.id, A, {"helm": Target("armor", "004/000"), "body": THIRD}).errors == [RETARGET_BODY]
    assert m.plan_retarget(mod.id, Target("armor", "004/001"), MIXED).errors == [RETARGET_BODY]
    assert m.plan_retarget(mod.id, A, {"helm": "f/004/000"}).errors == ["옮길 수 없는 장비입니다 (f/004/000)"]
    itself = m.plan_retarget(mod.id, A, {"helm": A, "body": THIRD})  # (the core names the part)
    assert itself.errors == ["머리: 원래 장비와 같은 장비입니다"] and itself.bad_parts == {"helm"}
    assert m.plan_retarget(mod.id, A, MIXED, parts=["helm"]).errors == ["옮길 부위가 부위마다 고른 장비와 맞지 않습니다"]
    assert m.plan_retarget(mod.id, A, {}).errors == ["옮길 부위를 하나 이상 고르세요"]
    assert m.plan_retarget(mod.id, A, {"head": B}).errors == ["알 수 없는 부위입니다: head"]
    before = [x.id for x in m.mods]
    with pytest.raises(InstallError, match="체형이 다른"):
        m.create_retarget(mod.id, A, {"helm": B, "body": other_body})
    assert [x.id for x in m.mods] == before and not list(m.temp_root.glob("retarget-*"))

    # a weapon: its main and sub to one weapon only
    source = tmp_path / "src" / "Blades.pak"
    pak.write_pak(blades_entries(), source)
    with m.prepare_install(source) as session:
        blades = m.install_package(session, session.variants)
    weapon, target = Target("weapon", "02/000029"), Target("weapon", "02/000028")
    both = m.plan_retarget(blades.id, weapon, {"main": target, "sub": target})
    assert both.ok and not both.mixed and both.targets == dict.fromkeys(("main", "sub"), retarget.Slot.of(target))
    assert m.retarget_name(blades.id, {"main": target, "sub": target}) == "Blades → 첫걸음쌍검Ⅰ"
    assert m.plan_retarget(blades.id, weapon, {"main": target}).errors == \
        ["무기는 본체와 보조를 함께 한 무기로만 옮길 수 있습니다"]
    assert main(["--data-dir", str(m.data_dir), "retarget", "Blades", "--to", "weapon/02/000028", "--to",
                 "보조=weapon/02/000002", "--dry-run"]) == 1
    assert main(["--data-dir", str(m.data_dir), "retarget", "Blades", "--to", "main=weapon/02/000028", "--to",
                 "sub=weapon/02/000028", "--dry-run"]) == 0  # (both to one: as a move to it)
    for one in ("main", "보조"):  # (one model alone: not the whole weapon)
        assert main(["--data-dir", str(m.data_dir), "retarget", "Blades", "--to", f"{one}=weapon/02/000028",
                     "--dry-run"]) == 1


def test_parts_to_one_piece_of_equipment_are_a_move_to_it(library):
    m, mod, _first, _last = library
    grouped = m.plan_retarget(mod.id, A, {"body": B, "helm": B})
    uniform = m.plan_retarget(mod.id, A, B, parts=["helm", "body"])
    assert grouped.ok and not grouped.mixed and grouped.parts == uniform.parts == ("helm", "body")
    assert [(mv.old, mv.new, mv.how) for mv in grouped.moves] == [(mv.old, mv.new, mv.how) for mv in uniform.moves]
    assert m.retarget_name(mod.id, {"body": B, "helm": B}) == m.retarget_name(mod.id, B) == "Bunny → 시험"
    new = m.create_retarget(mod.id, A, {"body": B, "helm": B})
    assert new.name == "Bunny → 시험"
    assert (new.retarget["to"], new.retarget["parts"]) == ("f/004/000", ["helm", "body"])  # the uniform form


def test_names_of_moves_part_by_part(library):
    m, mod, _first, _last = library
    assert m.retarget_name(mod.id, {"arm": B, "body": THIRD, "helm": B}) == "Bunny → 머리·팔 시험 · 몸통 방어구 005-000"
    many = {"helm": B, "body": THIRD, "arm": FOURTH, "waist": Target("armor", "f/007/000"), "leg": B}
    assert m.retarget_name(mod.id, many) == "Bunny → 머리·다리 시험 외 3부위"
    m.create_retarget(mod.id, A, {"helm": B, "body": THIRD}, disable_original=False)  # (no body: still made)
    assert m.retarget_name(mod.id, {"helm": B, "body": THIRD}) == "Bunny → 머리 시험 · 몸통 방어구 005-000 (2)"


def test_the_command_line_part_by_part(duo_library, capsys):
    m, _duo = duo_library
    data = str(m.data_dir)
    assert main(["--data-dir", data, "retarget", "Duo", "--to", "armor/f/004/000", "--to", "body=armor/f/005/000",
                 "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Duo: 시험 (armor/f/004/001) → 부위마다 다른 장비" in out
    assert "  머리 → 시험 (armor/f/004/000)\n  몸통 → 방어구 005-000 (armor/f/005/000)\n  옮길 파일 7개" in out
    assert "부위:" not in out  # (only the parts Duo changes, not every part to the default)

    assert main(["--data-dir", data, "retarget", "Duo", "--to", "머리=armor/004/000", "--to", "몸통=armor/m/005/000",
                 "--keep-original", "--no-deploy"]) == 0  # (the body of a key is the original's)
    out = capsys.readouterr().out
    assert "  머리 → 시험 (armor/f/004/000)\n  몸통 → 방어구 005-000 (armor/f/005/000)" in out
    assert "새 모드: Duo → 머리 시험 · 몸통 방어구 005-000  (7개 파일, 켜짐)" in out
    made = ModManager(data).find("Duo → 머리 시험 · 몸통 방어구 005-000")
    assert made.retarget["to"] == {"helm": "f/004/000", "body": "f/005/000"}

    # a default and the parts limited: only those, each to its own
    assert main(["--data-dir", data, "retarget", "Duo", "--to", "armor/f/004/000", "--to", "body=armor/f/005/000",
                 "--parts", "helm,body", "--keep-original", "--no-deploy"]) == 0
    assert "새 모드: Duo → 머리 시험 · 몸통 방어구 005-000 (2)" in capsys.readouterr().out
    # every part to one: as a move to it
    assert main(["--data-dir", data, "retarget", "Duo", "--to", "armor/f/005/000", "--to", "helm=armor/f/005/000",
                 "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Duo: 시험 (armor/f/004/001) → 방어구 005-000 (armor/f/005/000)" in out and "부위: 머리, 몸통\n" in out

    for args, error in ((["--to", "helm=armor/f/004/000", "--parts", "body"], "--parts에 없는 부위의 --to입니다: 머리"),
                        (["--to", "helm=armor/f/004/000", "--parts", "helm,body"], "고르지 않은 부위입니다: 몸통"),
                        (["--to", "armor/f/004/000", "--to", "armor/f/005/000"], "하나만 적으세요"),
                        (["--to", "helm=armor/f/004/000", "--to", "머리=armor/f/005/000"], "두 번 적었습니다: 머리"),
                        (["--to", "head=armor/f/004/000"], "알 수 없는 부위입니다: head"),
                        (["--to", "helm=nothing"], "장비를 알 수 없습니다: nothing")):
        with pytest.raises(SystemExit, match=error):
            main(["--data-dir", data, "retarget", "Duo", *args, "--dry-run"])
    assert main(["--data-dir", data, "retarget", "Duo", "--to", "helm=armor/f/004/001", "--to", "body=armor/f/005/000",
                 "--dry-run"]) == 1
    assert "머리: 원래 장비와 같은 장비입니다" in capsys.readouterr().err
