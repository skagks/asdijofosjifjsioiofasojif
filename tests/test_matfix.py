"""Material repair against a small fake game: encrypted (and chunked) paks, an
outdated costume mod as loose files and as a mod pak."""

import copy
import os
import struct
from pathlib import Path

import pytest

from mhwmm import matfix

pytestmark = pytest.mark.skipif(not matfix.available(), reason="zstd 모듈이 없습니다")

from mhwmm.matfix import pak as pakmod  # noqa: E402
from mhwmm.matfix.game import GameIndex  # noqa: E402
from mhwmm.matfix.mdf2 import Mdf2File, assemble_mdf2, extract_material  # noqa: E402
from mhwmm.matfix.repair import NULL_MASK, FileResult, check_mod  # noqa: E402
import test_rsz as R  # noqa: E402

BODY = "natives/STM/Art/Model/Character/ch03/030/601/1/ch03_030_6011"
LEGS = "natives/STM/Art/Model/Character/ch03/030/601/2/ch03_030_6012"
WEAPON = "natives/STM/Art/Model/Item/it01/00/0001/it0100_0001"
SHADER = "MaterialShader/Variation/Base_Equip.mmtr"
AVP = "natives/STM/GameDesign/Equip/_Prefab/Armor/Female/004/000/004_000_avp.user.3"


def material(name, textures, props, mmtr=SHADER, alpha=b"\x98\x00\x00\x88", gpbf=(("MultiBlend_BAB", "g.gpbf"),)):
    return {"name": name, "mmtr_path": mmtr, "shading_type": 0, "alpha_flags_raw": alpha,
            "isV2_unk_0x24": 0, "isV2_unk_0x2C": 0, "isV2_unk_0x30": 0, "gpbf_split": (len(gpbf), len(gpbf)),
            "gpbf_entries": [(slot, 1, 2) for slot, _ in gpbf] + [(path, 0, 1) for _, path in gpbf],
            "textures": [{"type": t, "path": p, "ukn": 0} for t, p in textures],
            "props": [{"name": n, "values": list(v), "count_flags": 0} for n, v in props], "header_version": 1}


def current_body():
    """The game's material now: a new DetailMaskMap slot and a new shader parameter ('skin', on the same
    shader, did not get them)."""
    return [material("body", [("BaseDielectricMap", "Art/ch03/body_ALBD.tex"), ("NormalRoughnessMap", "Art/ch03/body_NRRO.tex"),
                              ("DetailMaskMap", "Art/other/someone_MSK.tex")],
                     [("Color", (1.0, 1.0, 1.0, 1.0)), ("Roughness", (0.5,)), ("NewParam", (0.25,))]),
            material("skin", [("BaseDielectricMap", "Art/ch03/skin_ALBD.tex")], [("Color", (1.0, 1.0, 1.0, 1.0))])]


def old_body_mod():
    """What the mod shipped, made before the update: its own textures and tuning."""
    return [material("body", [("BaseDielectricMap", "Art/Dimcirui/chen/body_ALBD.tex"),
                              ("NormalRoughnessMap", "Art/Dimcirui/chen/body_NRRO.tex")],
                     [("Color", (0.2, 0.4, 0.6, 1.0)), ("Roughness", (0.9,))], alpha=b"\x9b\x00\x00\x08",
                     gpbf=(("MultiBlend_BAB", "systems/rendering/Empty.gpbf"),)),
            material("skin", [("BaseDielectricMap", "Art/Dimcirui/chen/skin_ALBD.tex")], [("Color", (1.0, 0.8, 0.8, 1.0))])]


def write_game_pak(path: Path, files: dict, chunked: bool = False):
    """An encrypted game pak (feature 8), or a chunked one (feature 40)."""
    key = os.urandom(127) + b"\x00"  # below the modulus
    blobs, rows = [], []
    offset = 0
    header_size = 16 + 48 * len(files) + 128 + (8 + 8 * len(files) if chunked else 0)
    chunk_rows = []
    for i, (name, data) in enumerate(files.items()):
        stored = matfix.zstd_compress(data)
        h = pakmod.path_hash(name)
        if chunked:  # one chunk per file; offset holds the chunk index
            chunk_rows.append((header_size + offset, len(stored) << 10))
            rows.append(struct.pack("<IIqqqqQ", h & 0xFFFFFFFF, h >> 32, i, len(stored), len(data), 0x1000000, 0))
        else:
            rows.append(struct.pack("<IIqqqqQ", h & 0xFFFFFFFF, h >> 32, header_size + offset, len(stored), len(data), 2, 0))
        blobs.append(stored)
        offset += len(stored)
    table = pakmod.xor_table(b"".join(rows), key)
    with open(path, "wb") as f:
        f.write(struct.pack("<IBBhii", pakmod.MAGIC, 4, 0, 40 if chunked else 8, len(files), 0))
        f.write(table + key)
        if chunked:
            f.write(struct.pack("<ii", 524288, len(chunk_rows)))
            for off, size in chunk_rows:
                f.write(struct.pack("<II", off, size))
        for b in blobs:
            f.write(b)


@pytest.fixture
def game(tmp_path):
    root = tmp_path / "MonsterHunterWilds"
    root.mkdir()
    write_game_pak(root / "re_chunk_000.pak", {
        BODY + ".mdf2.45": b"stale base copy",
        LEGS + ".mdf2.45": assemble_mdf2([material("legs", [("BaseDielectricMap", "Art/ch03/legs.tex")],
                                                   [("Color", (1.0,) * 4)])], 45),
        WEAPON + ".mdf2.45": assemble_mdf2(current_body()[:1], 45),
        AVP: R.game_file(),
    })
    # the latest patch wins over the base pak; this one is chunked
    write_game_pak(root / "re_chunk_000.pak.patch_001.pak", {BODY + ".mdf2.45": assemble_mdf2(current_body(), 45)},
                   chunked=True)
    return root


def test_pak_index_layers_patches_and_caches(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    assert len(index) == 4
    assert index.read(BODY + ".mdf2.45") == assemble_mdf2(current_body(), 45)  # from the chunked patch
    assert index.versioned_path(BODY.lower(), "mdf2") == BODY.lower() + ".mdf2.45"  # paths are case-insensitive
    assert index.read_versioned(BODY, "mdf2", prefer=(40,))[0].endswith(".mdf2.45")
    assert list((tmp_path / "cache").glob("pakindex-*.bin"))
    again = GameIndex(game, tmp_path / "cache")  # from the cache
    assert list(again.hashes) == list(index.hashes) and again.read(LEGS + ".mdf2.45") == index.read(LEGS + ".mdf2.45")


def stage(tmp_path, files):
    out = []
    for rel, data in files.items():
        path = tmp_path / "mod" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        out.append((rel, rel, path))
    return out


def to(tmp_path):
    """check_mod's ``out``: repaired files go to tmp_path/out/<new game path>."""
    return lambda result: tmp_path / "out" / result.new_rel


def test_outdated_loose_material_is_rebuilt(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    files = stage(tmp_path, {
        BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45),
        LEGS + ".mdf2.45": assemble_mdf2([material("legs", [("BaseDielectricMap", "Art/mine/legs.tex")],
                                                   [("Color", (0.1,) * 4)])], 45),  # already current
        "natives/STM/Art/Dimcirui/chen/body_ALBD.tex.241106027": b"tex",
    })
    (body, legs) = check_mod(files, index, None)
    assert (body.state, legs.state) == ("fixable", "current")
    assert body.fixed == ["body"] and body.new_rel == BODY + ".mdf2.45" and body.file is None

    (body, _legs) = check_mod(files, index, None, out=to(tmp_path))
    fixed = Mdf2File(body.file.read_bytes(), 45)
    got = {m.name.text: extract_material(fixed, m.index) for m in fixed.materials}
    tex = {t["type"]: t["path"] for t in got["body"]["textures"]}
    assert tex == {"BaseDielectricMap": "Art/Dimcirui/chen/body_ALBD.tex",
                   "NormalRoughnessMap": "Art/Dimcirui/chen/body_NRRO.tex",
                   "DetailMaskMap": NULL_MASK}  # a new mask never takes another model's texture
    props = {p["name"]: p["values"] for p in got["body"]["props"]}
    assert props["Color"] == pytest.approx([0.2, 0.4, 0.6, 1.0]) and props["Roughness"] == pytest.approx([0.9])
    assert props["NewParam"] == [0.25]  # new parameter: the game's value
    assert got["body"]["alpha_flags_raw"] == b"\x9b\x00\x00\x08"  # the mod's render state is kept
    assert got["body"]["gpbf_entries"][1][0] == "systems/rendering/Empty.gpbf"
    # the untouched material keeps exactly what the mod had
    assert got["skin"]["props"][0]["values"] == pytest.approx([1.0, 0.8, 0.8, 1.0])


def test_old_version_number_is_renamed(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    current_legs = [material("legs", [("BaseDielectricMap", "Art/mine/legs.tex")], [("Color", (0.1,) * 4)])]
    (legs,) = check_mod(stage(tmp_path, {LEGS + ".mdf2.40": assemble_mdf2(current_legs, 40)}), index, None, out=to(tmp_path))
    assert legs.state == "fixable" and legs.new_rel == LEGS + ".mdf2.45" and legs.fixed == []
    assert legs.file.read_bytes() == assemble_mdf2(current_legs, 40)  # same layout: only the name changes


def test_custom_slot_and_unknown_shader(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    mh = BODY.replace("ch03/030", "mh03/030").replace("ch03_030", "mh03_030")
    mod = old_body_mod() + [material("glow", [("EmissiveMap", "Art/x/glow.tex")], [], mmtr="Custom/Nobody.mmtr")]
    (body,) = check_mod(stage(tmp_path, {mh + ".mdf2.45": assemble_mdf2(mod, 45)}), index, None, out=to(tmp_path))
    assert body.state == "fixable" and body.new_rel == mh + ".mdf2.45"  # keeps the mod's own slot path
    assert body.unresolved == ["glow"]
    fixed = Mdf2File(body.file.read_bytes(), 45)
    assert [m.name.text for m in fixed.materials] == ["body", "skin", "glow"]
    assert extract_material(fixed, 2) == extract_material(Mdf2File(assemble_mdf2(mod, 45), 45), 2)  # left as shipped


def test_mod_pak_is_patched_in_place(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    weapon_mod = [material("body", [("BaseDielectricMap", "Art/myweapon/w_ALBD.tex"),
                                    ("NormalRoughnessMap", "Art/myweapon/w_NRRO.tex")],
                           [("Color", (0.3, 0.3, 0.3, 1.0)), ("Roughness", (0.1,))])]
    mod_pak = tmp_path / "mod" / "Weapon.pak"
    mod_pak.parent.mkdir(parents=True)
    entries = [(pakmod.path_hash("natives/STM/Art/myweapon/w_ALBD.tex.241106027"), b"TEX" * 100, 300, 0),
               (pakmod.path_hash(WEAPON + ".mdf2.45"), assemble_mdf2(weapon_mod, 45), 0, 0)]
    pakmod.write_pak([(h, d, len(d), c) for h, d, _, c in entries], mod_pak)
    (result,) = check_mod([("Weapon.pak", "Weapon.pak", mod_pak)], index, None, out=to(tmp_path))
    assert result.state == "fixable" and result.fixed == ["body"]
    fixed = pakmod.PakFile(result.file)
    assert fixed.order == pakmod.PakFile(mod_pak).order  # same entries, same order
    assert fixed.read_hash(entries[0][0]) == b"TEX" * 100
    mat = extract_material(Mdf2File(fixed.read_hash(entries[1][0]), 45), 0)
    assert {t["type"]: t["path"] for t in mat["textures"]}["BaseDielectricMap"] == "Art/myweapon/w_ALBD.tex"
    assert {p["name"]: p["values"] for p in mat["props"]}["Roughness"] == pytest.approx([0.1])
    assert {p["name"] for p in mat["props"]} == {"Color", "Roughness", "NewParam"}


def test_current_mod_is_left_alone(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    mine = current_body()
    mine[0]["textures"][0]["path"] = "Art/mine/body.tex"
    (body,) = check_mod(stage(tmp_path, {BODY + ".mdf2.45": assemble_mdf2(mine, 45)}), index, None)
    assert body.state == "current" and not body.fixed


def test_in_place_only_when_the_materials_are_the_current_files(game, tmp_path):
    from mhwmm.matfix import repair

    index = GameIndex(game, tmp_path / "cache")

    def patched(mod):
        unit = repair._loose_unit(BODY + ".mdf2.45", assemble_mdf2(mod, 45), index)
        repair.plan_entries([unit], None)
        wanted = [repair.apply_overrides(m.donor, m.mod, []) if m.stale else m.mod for m in unit.materials]
        return unit, repair._patch_in_place(unit, wanted)

    unit, data = patched(old_body_mod())  # same materials as the game's file: patched in place
    assert data is not None and repair.build_entry(unit)[0] == data

    renamed = old_body_mod()
    renamed[0]["name"] = "body_custom"  # the mod's mesh names its materials differently
    assert patched(renamed)[1] is None  # in place would rename the material

    extra = old_body_mod()
    extra[1]["textures"].append({"type": "EmissiveMap", "path": "Art/x/glow.tex", "ukn": 0})  # a slot of its own
    unit, data = patched(extra)
    assert data is None
    rebuilt = Mdf2File(repair.build_entry(unit)[0], 45)
    assert [t["type"] for t in extract_material(rebuilt, 1)["textures"]] == ["BaseDielectricMap", "EmissiveMap"]


def test_compressed_mod_pak_entry_stays_compressed(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    data = assemble_mdf2([material("body", [("BaseDielectricMap", "Art/w.tex")], [("Color", (0.5,) * 4)])], 45)
    mod_pak = tmp_path / "Weapon.pak"
    pakmod.write_pak([(pakmod.path_hash(WEAPON + ".mdf2.45"), matfix.zstd_compress(data), len(data),
                       pakmod.COMPRESSION_ZSTD)], mod_pak)
    (result,) = check_mod([("Weapon.pak", "Weapon.pak", mod_pak)], index, None, out=to(tmp_path))
    fixed = pakmod.PakFile(result.file)
    assert fixed.attrs[0] & 0xF == pakmod.COMPRESSION_ZSTD
    assert len(Mdf2File(fixed.read(0), 45).materials[0].props) == 3


# ---- which materials are stale: the copied material, or any same-shader one, not one picked donor ----
# Nexus 4052 'Rossi - Hunter (LBS)' hides the other armour parts with one material each (named 'null')
# and was reported stale against the hunter's base body, one of 121 game materials with that shader.
HIDDEN = "natives/STM/Art/Model/Character/ch03/034/001/1/ch03_034_0011"
HUNTER = "MaterialShader/Variation/Base_Hunter.mmtr"
VARIANT = "MaterialShader/Variation/Base_Equip_NoMultiBlend.mmtr"
BASE_BODY = "natives/stm/art/model/character/ch00/000/0000/ch00_000_0000.mdf2.45"
ITEM = "natives/stm/art/model/item/it55/10/0008/it5510_0008_0.mdf2.45"
OTHER = "natives/stm/art/model/character/ch04/0/02/00/00/ch04_002_0000.mdf2.45"


def rich(name="skin", mmtr=HUNTER):
    """A game material with more slots than the mod's: what an arbitrary donor can look like."""
    return material(name, [("BaseDielectricMap", "Art/ch00/skin_ALBD.tex"), ("NormalRoughnessMap", "Art/ch00/skin_NRRO.tex"),
                           ("DetailMaskMap", "Art/ch00/skin_MSK.tex")],
                    [("Color", (1.0,) * 4), ("Roughness", (0.5,)), ("Fresnel", (0.04,))], mmtr=mmtr)


def plain(name="eyeshadow", mmtr=HUNTER):
    """A game material with only the slots the mod's has."""
    return material(name, [("BaseDielectricMap", "Art/it55/shadow_ALBD.tex")], [("Color", (1.0,) * 4)], mmtr=mmtr)


def hider(name="cover", mmtr=HUNTER):
    return material(name, [("BaseDielectricMap", "Art/rossi/clear_ALBD.tex")], [("Color", (0.0,) * 4)], mmtr=mmtr)


def with_slots(mat, textures=(), props=()):
    for t in textures:
        mat["textures"].append({"type": t, "path": f"Art/x/{t}.tex", "ukn": 0})
    for name in props:
        mat["props"].append({"name": name, "values": [0.5], "count_flags": 0})
    return mat


class Shaders:
    """The whole-game index check_mod reads (ShaderIndex.find), with the lookups it was asked for."""

    def __init__(self, by_mmtr):
        self.by_mmtr, self.asked = by_mmtr, []

    def find(self, mmtr):
        self.asked.append(mmtr)
        return list(self.by_mmtr.get(mmtr, []))


def armour_game(game, current):
    """The game with the current version of the hidden part's file in a newer patch."""
    write_game_pak(game / "re_chunk_000.pak.patch_002.pak", {HIDDEN + ".mdf2.45": assemble_mdf2(current, 45)})
    return GameIndex(game, game.parent / "cache")


def test_a_slot_some_game_material_of_the_shader_lacks_is_not_stale(game, tmp_path):
    index = armour_game(game, [material("body", [("BaseDielectricMap", "Art/ch03/body.tex")], [("Color", (1.0,) * 4)])])
    files = stage(tmp_path, {HIDDEN + ".mdf2.45": assemble_mdf2([hider()], 45)})
    shaders = Shaders({HUNTER: [(BASE_BODY, rich()), (OTHER, plain())]})  # the donor picked is the base body
    (result,) = check_mod(files, index, shaders)
    assert result.state == "current" and result.fixed == [] and result.notes == []  # (no "후보가 2개라 ...를 썼습니다")
    assert set(shaders.asked) == {HUNTER}  # (to find the donor, then all of that shader's materials)


def test_a_slot_every_game_material_of_the_shader_has_is_stale(game, tmp_path):
    index = armour_game(game, [material("body", [("BaseDielectricMap", "Art/ch03/body.tex")], [("Color", (1.0,) * 4)])])
    files = stage(tmp_path, {HIDDEN + ".mdf2.45": assemble_mdf2([hider()], 45)})
    newer = with_slots(plain(), ["DetailMaskMap"])
    shaders = Shaders({HUNTER: [(BASE_BODY, rich()), (OTHER, newer)]})
    (result,) = check_mod(files, index, shaders, out=to(tmp_path))
    assert result.state == "fixable" and result.fixed == ["cover"]
    assert result.notes == [  # the closest one, not the donor: the slot every one of them has
        f"재질 'cover': 같은 셰이더를 쓰는 후보가 2개라 {BASE_BODY}의 것을 썼습니다",
        f"재질 'cover': 현재 게임의 같은 셰이더 재질 2개 중 가장 가까운 재질({OTHER}의 'eyeshadow')에도 있는 칸이 없습니다: "
        "텍스처 DetailMaskMap"]
    got = extract_material(Mdf2File(result.file.read_bytes(), 45), 0)  # rebuilt from the donor picked
    assert {t["type"]: t["path"] for t in got["textures"]} == {
        "BaseDielectricMap": "Art/rossi/clear_ALBD.tex", "NormalRoughnessMap": "Art/ch00/skin_NRRO.tex",
        "DetailMaskMap": NULL_MASK}
    assert [p["name"] for p in got["props"]] == ["Color", "Roughness", "Fresnel"]


def test_slots_missing_from_different_materials_do_not_add_up(game, tmp_path):
    """One current material lacks texture T, another parameter P: none lacks both, so a mod material
    without either is not one the current game has (each material is compared as a whole)."""
    index = armour_game(game, [material("body", [("BaseDielectricMap", "Art/ch03/body.tex")], [("Color", (1.0,) * 4)])])
    files = stage(tmp_path, {HIDDEN + ".mdf2.45": assemble_mdf2([hider()], 45)})
    g1, g2 = with_slots(plain("g1"), ["T"]), with_slots(plain("g2"), props=["P"])
    (result,) = check_mod(files, index, Shaders({HUNTER: [(BASE_BODY, g1), (OTHER, g2)]}))
    assert result.state == "fixable" and result.fixed == ["cover"]
    assert f"재질 'cover': 현재 게임의 같은 셰이더 재질 2개 중 가장 가까운 재질({BASE_BODY}의 'g1')에도 있는 칸이 없습니다: " \
           "텍스처 T" in result.notes  # (a tie: the donor's)

    (result,) = check_mod(files, index, Shaders({HUNTER: [(BASE_BODY, g1), (OTHER, g2), (ITEM, plain())]}))
    assert result.state == "current"  # ... unless one of them lacks both


def test_the_mods_own_current_file_counts_and_without_the_index_the_donor_decides(game, tmp_path):
    """The other materials of the current file are game materials too; without the whole-game index
    (none given) the donor alone is compared, as before."""
    index = armour_game(game, [rich(), plain()])
    files = stage(tmp_path, {HIDDEN + ".mdf2.45": assemble_mdf2([hider()], 45)})
    (result,) = check_mod(files, index, Shaders({}))
    assert result.state == "current" and result.notes == []

    (result,) = check_mod(files, index, None)
    assert result.state == "fixable" and result.fixed == ["cover"]
    assert result.notes == [
        f"재질 'cover': 같은 셰이더를 쓰는 후보가 2개라 {HIDDEN}.mdf2.45의 것을 썼습니다",
        f"재질 'cover': 현재 게임의 재질({HIDDEN}.mdf2.45의 'skin')에 있는 칸이 없습니다: "
        "텍스처 DetailMaskMap, NormalRoughnessMap · 파라미터 Fresnel, Roughness"]


@pytest.mark.parametrize("shaders", [None, "empty", "whole game"])
def test_the_copied_material_decides_even_when_others_of_its_shader_lack_the_new_slot(game, tmp_path, shaders):
    """The update case the check is for: the game's 'body' got DetailMaskMap and NewParam; 'skin' in the same
    file and 'legs' of the set use the same shader without them. The mod's 'body' is a copy of the old 'body'."""
    index = GameIndex(game, tmp_path / "cache")
    files = stage(tmp_path, {
        BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45),
        LEGS + ".mdf2.45": assemble_mdf2([material("legs", [("BaseDielectricMap", "Art/mine/legs.tex")],
                                                   [("Color", (0.1,) * 4)])], 45)})
    whole = {None: None, "empty": Shaders({}), "whole game": Shaders({SHADER: [(ITEM, plain(mmtr=SHADER))]})}[shaders]
    (body, legs) = check_mod(files, index, whole)
    assert (body.state, body.fixed, legs.state) == ("fixable", ["body"], "current")
    assert body.notes == [f"재질 'body': 현재 게임의 재질({BODY}.mdf2.45의 'body')에 있는 칸이 없습니다: "
                          "텍스처 DetailMaskMap · 파라미터 NewParam"]
    assert whole is None or whole.asked == []  # (no other material is looked at)


def test_the_one_same_named_candidate_decides(game, tmp_path):
    """No current file of its own: of the game's materials with the shader, the one with the mod's material
    name is what the author copied, so a slot it has and the mod's lacks is stale even if others lack it."""
    custom = "natives/STM/Art/Model/Character/ch00/000/rossi/ch00_000_0000"
    index = GameIndex(game, tmp_path / "cache")
    files = stage(tmp_path, {custom + ".mdf2.45": assemble_mdf2([hider("cover")], 45)})
    copied = with_slots(plain("cover"), ["DetailMaskMap"])
    (result,) = check_mod(files, index, Shaders({HUNTER: [(BASE_BODY, copied), (OTHER, plain())]}))
    assert result.state == "fixable" and result.fixed == ["cover"]
    assert result.notes == [f"재질 'cover': 현재 게임의 재질({BASE_BODY}의 'cover')에 있는 칸이 없습니다: 텍스처 DetailMaskMap"]

    twice = Shaders({HUNTER: [(BASE_BODY, copied), (ITEM, with_slots(plain("cover"), ["DetailMaskMap"])), (OTHER, plain())]})
    (result,) = check_mod(files, index, twice)
    assert result.state == "current"  # two of that name: one picked of many, and 'eyeshadow' lacks the slot


def test_a_variant_match_compares_with_the_variants_materials(game, tmp_path):
    """The mod's shader is gone and the current file has a "_NoMultiBlend" sibling: the materials of the
    sibling are the reference (the mod's own shader has none to compare with)."""
    index = armour_game(game, [rich("trim", VARIANT)])
    files = stage(tmp_path, {HIDDEN + ".mdf2.45": assemble_mdf2([hider(mmtr=SHADER)], 45)})
    shaders = Shaders({VARIANT: [(ITEM, plain(mmtr=VARIANT))]})
    (result,) = check_mod(files, index, shaders)
    assert result.state == "current" and shaders.asked[-1] == VARIANT

    shaders = Shaders({VARIANT: [(ITEM, rich("other", VARIANT))]})
    (result,) = check_mod(files, index, shaders)
    assert result.state == "fixable" and result.fixed == ["cover"]
    assert f"재질 'cover': 현재 게임의 같은 셰이더 재질 2개 중 가장 가까운 재질({HIDDEN}.mdf2.45의 'trim')에도 있는 칸이 " \
           "없습니다: 텍스처 DetailMaskMap, NormalRoughnessMap · 파라미터 Fresnel, Roughness" in result.notes


def test_a_mod_pak_entry_and_its_game_file_count_once(game, tmp_path, monkeypatch):
    """A mod PAK's current entry is labelled "<pak>#<hash>"; the whole-game index has the same file under its
    path. It is one material, not two."""
    from mhwmm.matfix import game as gamemod

    monkeypatch.setattr(gamemod, "known_material_paths", lambda: [WEAPON + ".mdf2.45"])
    index = GameIndex(game, tmp_path / "cache")
    mod_pak = tmp_path / "mod" / "Weapon.pak"
    mod_pak.parent.mkdir(parents=True)
    h = pakmod.path_hash(WEAPON + ".mdf2.45")
    data = assemble_mdf2([hider("blade", SHADER)], 45)
    pakmod.write_pak([(h, data, len(data), 0)], mod_pak)
    (result,) = check_mod([("Weapon.pak", "Weapon.pak", mod_pak)], index, gamemod.ShaderIndex(index))
    assert result.state == "fixable" and result.fixed == ["blade"]
    assert result.notes == [f"재질 'blade': 현재 게임에서 같은 셰이더를 쓰는 유일한 재질(Weapon.pak#{h:016X}의 'body')에 "
                            "있는 칸이 없습니다: 텍스처 DetailMaskMap, NormalRoughnessMap · 파라미터 NewParam, Roughness"]


@pytest.mark.parametrize("name", ["null", "Null", "NULL"])
def test_a_hidden_parts_null_material_is_left_alone(game, tmp_path, name):
    index = armour_game(game, [material("body", [("BaseDielectricMap", "Art/ch03/body.tex")], [("Color", (1.0,) * 4)])])
    files = stage(tmp_path, {HIDDEN + ".mdf2.45": assemble_mdf2([hider(name)], 45)})
    (result,) = check_mod(files, index, Shaders({HUNTER: [(BASE_BODY, rich())]}))  # every one has more slots
    assert result.state == "current" and result.fixed == []
    assert result.notes == [f"숨김용 재질 '{name}'은 그대로 둡니다"]


def test_only_the_bare_name_null_is_a_placeholder(game, tmp_path):
    index = armour_game(game, [material("body", [("BaseDielectricMap", "Art/ch03/body.tex")], [("Color", (1.0,) * 4)])])
    files = stage(tmp_path, {HIDDEN + ".mdf2.45": assemble_mdf2([hider("null_mat"), hider("null")], 45)})
    (result,) = check_mod(files, index, Shaders({HUNTER: [(BASE_BODY, rich())]}))
    assert result.state == "fixable" and result.fixed == ["null_mat"]
    assert "숨김용 재질 'null'은 그대로 둡니다" in result.notes


def test_a_placeholder_in_an_old_version_file_is_still_renamed(game, tmp_path):
    index = armour_game(game, [material("body", [("BaseDielectricMap", "Art/ch03/body.tex")], [("Color", (1.0,) * 4)])])
    shipped = assemble_mdf2([hider("null")], 40)
    files = stage(tmp_path, {HIDDEN + ".mdf2.40": shipped})
    (result,) = check_mod(files, index, Shaders({HUNTER: [(BASE_BODY, rich())]}), out=to(tmp_path))
    assert result.state == "fixable" and result.fixed == [] and result.new_rel == HIDDEN + ".mdf2.45"
    assert result.file.read_bytes() == shipped  # same layout: only the version number the game reads


def test_the_whole_game_index_is_read_only_when_the_donor_says_stale(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    shaders = Shaders({SHADER: [(BASE_BODY, plain(mmtr=SHADER))]})
    current = current_body()
    current[0]["textures"][0]["path"] = "Art/mine/body.tex"
    (result,) = check_mod(stage(tmp_path, {BODY + ".mdf2.45": assemble_mdf2(current, 45)}), index, shaders)
    assert result.state == "current" and shaders.asked == []  # (building it reads thousands of game files)


def test_missing_slots_and_their_note():
    from mhwmm.matfix import repair

    mod = material("m", [("A", "a.tex")], [("P", (1.0,))], gpbf=(("MultiBlend_BAB", "e.gpbf"),))
    wide = material("w", [("A", "a.tex"), ("B", "b.tex")], [("P", (1.0,)), ("Q", (1.0,))],
                    gpbf=(("MultiBlend_BAB", "g.gpbf"), ("Extra", "x.gpbf")))
    assert repair.missing_slots(mod, wide) == (["B"], ["Q"], ["Extra"])
    assert repair.missing_slots(mod, repair.Slots.of(wide)) == (["B"], ["Q"], ["Extra"])
    assert repair.needs_fix(mod, wide) and not repair.needs_fix(wide, mod)
    same_split = material("s", [("A", "a.tex"), ("B", "b.tex")], [("P", (1.0,))], gpbf=(("Other", "o.gpbf"),))
    assert repair.missing_slots(mod, same_split) == (["B"], [], [])  # (MHWmodfixer's rule)
    refs = repair.References(2, tuple((repair.Slots.of(m), f"{m['name']}.mdf2", m["name"]) for m in (wide, same_split)))
    assert refs.closest(mod) == ((["B"], [], []), "s.mdf2", "s")
    assert refs.closest(mod, (repair.Slots.of(same_split), "donor.mdf2", "d")) == ((["B"], [], []), "donor.mdf2", "d")
    assert repair.slot_list((list("ABCDEFGH"), ["P"], [])) == "텍스처 A, B, C, D, E, F 외 2개 · 파라미터 1개"
    assert repair.slot_list((["A"], ["P", "Q"], ["X"])) == "텍스처 A · 파라미터 P, Q · gpbf 버퍼 X"


# ---- installed mods --------------------------------------------------------------
@pytest.fixture
def library(game, tmp_path):
    from mhwmm.manager import ModManager

    m = ModManager(tmp_path / "data")
    m.settings.check_game_running = False
    m.set_game_dir(game)
    return m


def install_tree(manager, base, files):
    for rel, data in files.items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_bytes(data)
    with manager.prepare_install(base) as session:
        return manager.install_package(session, session.variants)


def run(manager, mod_ids=None, repair=False, unchecked_only=False):
    jobs = manager.material_jobs(mod_ids, unchecked_only=unchecked_only)
    return manager.finish_materials(manager.run_materials(jobs, repair=repair), jobs)


def test_installed_mod_is_checked_repaired_and_restored(library, game, tmp_path):
    shipped = assemble_mdf2(old_body_mod(), 45)
    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": shipped, "natives/STM/Art/chen.tex": b"t"})
    assert library.material_state(mod.id) == "unchecked"
    assert library.material_files(mod.id) == [(BODY + ".mdf2.45", BODY + ".mdf2.45",
                                               library.files_dir(mod.id) / (BODY + ".mdf2.45"))]

    assert run(library, unchecked_only=True) == []  # checking alone changes nothing
    assert library.material_state(mod.id) == "stale"
    assert library.stale_material_mods() == [mod] and library.materials_to_offer() == [mod]
    library.mark_materials_offered([mod])
    assert library.materials_to_offer() == [] and library.material_state(mod.id) == "stale"
    library.deploy()

    assert run(library, [mod.id], repair=True) == [mod]
    assert library.material_state(mod.id) == "repaired" and library.dirty
    path, key = library.mod_entries(mod.id)[BODY + ".mdf2.45"]
    assert key == "matfix/" + BODY + ".mdf2.45" and path.parent.name == "matfix"
    assert (library.files_dir(mod.id) / (BODY + ".mdf2.45")).read_bytes() == shipped  # the original stays
    library.deploy()
    deployed = (game / (BODY + ".mdf2.45")).read_bytes()
    assert deployed == path.read_bytes() and deployed != shipped
    assert not list(library.temp_root.glob("matfix-*"))

    assert run(library, [mod.id], repair=True) == [mod]  # repairs always start from the original again
    assert library.material_state(mod.id) == "repaired"

    assert library.restore_materials(mod.id)
    assert library.material_state(mod.id) == "declined" and not (library.mod_dir(mod.id) / "matfix").exists()
    assert library.mod_entries(mod.id)[BODY + ".mdf2.45"][1] == BODY + ".mdf2.45"
    library.deploy()
    assert (game / (BODY + ".mdf2.45")).read_bytes() == shipped


def test_installed_hiding_mod_against_the_whole_game_index(library, game, tmp_path, monkeypatch):
    """The reported mod end to end, with the real whole-game index made from the game's paks."""
    from mhwmm.matfix import game as gamemod

    write_game_pak(game / "re_chunk_000.pak.patch_002.pak", {
        HIDDEN + ".mdf2.45": assemble_mdf2([material("body", [("BaseDielectricMap", "Art/ch03/body.tex")],
                                                     [("Color", (1.0,) * 4)])], 45),
        BASE_BODY: assemble_mdf2([rich()], 45), OTHER: assemble_mdf2([plain()], 45)})
    monkeypatch.setattr(gamemod, "known_material_paths", lambda: [BASE_BODY, OTHER])
    mod = install_tree(library, tmp_path / "Rossi", {HIDDEN + ".mdf2.45": assemble_mdf2([hider(), hider("null")], 45)})
    run(library, unchecked_only=True)
    assert library.material_state(mod.id) == "current" and library.stale_material_mods() == []
    (result,) = library.material_results(mod.id)
    assert result.state == "current" and result.notes == []


def test_a_pak_past_a_gap_in_the_patch_chain_is_not_the_game(game, tmp_path):
    """The game reads a chain's patches up to the first missing number: an old copy another program left
    at patch_003 (no patch_002) neither replaces the current file nor makes the mod's material current."""
    from mhwmm.matfix.game import discover_paks, game_fingerprint

    version = game_fingerprint(game)
    write_game_pak(game / "re_chunk_000.pak.patch_003.pak", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    assert [p.name for p in discover_paks(game)] == ["re_chunk_000.pak", "re_chunk_000.pak.patch_001.pak"]
    assert game_fingerprint(game) == version
    index = GameIndex(game, tmp_path / "cache")
    assert index.read(BODY + ".mdf2.45") == assemble_mdf2(current_body(), 45)
    (body,) = check_mod(stage(tmp_path, {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)}), index, Shaders({}))
    assert body.state == "fixable" and body.fixed == ["body"]

    write_game_pak(game / "re_chunk_000.pak.patch_002.pak", {LEGS + ".mdf2.45": b"x"})  # the gap filled
    assert [p.name for p in discover_paks(game)][-2:] == ["re_chunk_000.pak.patch_002.pak", "re_chunk_000.pak.patch_003.pak"]


def test_the_managers_old_chain_paks_are_left_out(library, game, tmp_path):
    """A PAK an older version renamed into the patch chain holds a mod's files, not the game's: with it the
    mod's own old copy was its "current" material. Left out, the mod is stale against the real game file,
    and the record is for this game version (the fingerprint still counts every pak the game reads)."""
    import json

    chain = "re_chunk_000.pak.patch_002.pak"
    write_game_pak(game / chain, {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    st = (game / chain).stat()
    deployer = library.deployer()
    deployer.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    deployer.manifest_path.write_text(json.dumps({"version": 1, "game_dir": str(game), "backups": {}, "files": {
        chain: {"path": chain, "mod": "old", "src": "x.pak", "size": st.st_size, "mtime": st.st_mtime_ns, "pak": True}}}))
    assert library.outdated_paks() == [chain]
    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    run(library, unchecked_only=True)
    assert library.material_state(mod.id) == "stale"
    assert GameIndex(game, tmp_path / "c2").read(BODY + ".mdf2.45") == assemble_mdf2(old_body_mod(), 45)  # (without)
    index = library._equip_game()
    assert chain not in [p.name for p in index.paks] and index.fingerprint == library.game_version()
    assert index.read(BODY + ".mdf2.45") == assemble_mdf2(current_body(), 45)
    assert sorted(p.name for p in (library.data_dir / "cache").glob("pakindex-*.bin")) == [f"pakindex-{index.key}.bin"]
    assert index.key != index.fingerprint


def update_game(game, files=None):
    """A game update: the latest patch (written again with ``files``) is newer, so the game version changes."""
    patch = game / "re_chunk_000.pak.patch_001.pak"
    if files is not None:
        write_game_pak(patch, files, chunked=True)
    os.utime(patch, (patch.stat().st_atime, patch.stat().st_mtime + 100))


def newer_body():
    """The game's material after another update: the body got one more shader parameter."""
    mats = current_body()
    mats[0]["props"].append({"name": "NewerParam", "values": [0.5], "count_flags": 0})
    return mats


def test_game_update_and_reinstall_need_a_new_check(library, game, tmp_path, caplog):
    """After a game update every mod is checked again. The mod's own file is still stale, but the repair in use
    fits the new version too (the update did not change its material): it carries over - no ⚠, no offer."""
    import logging

    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    run(library, [mod.id], repair=True)
    assert library.material_state(mod.id) == "repaired"
    before = library.game_version()
    entry = library.mod_entries(mod.id)[BODY + ".mdf2.45"]
    repaired = entry[0].read_bytes()

    update_game(game)
    assert [j.mod_id for j in library.material_jobs(unchecked_only=True)] == [mod.id]
    assert library.material_state(mod.id) == "unchecked"
    assert library.mod_entries(mod.id)[BODY + ".mdf2.45"][1].startswith("matfix/")  # kept until checked again
    with caplog.at_level(logging.INFO, logger="mhwmm.manager"):
        assert run(library, unchecked_only=True) == []  # nothing the game gets changes
    assert library.material_state(mod.id) == "repaired" and not library.material_repairs_outdated(mod.id)
    (result,) = library.material_results(mod.id)
    assert result.state == "fixable"  # (the mod's own file: still the old layout)
    assert library._material_record(mod.id)["applied_game"] == library.game_version() != before
    assert library.problems() == {} and library.materials_to_offer() == [] and library.stale_material_mods() == []
    assert library.mod_entries(mod.id)[BODY + ".mdf2.45"] == entry and entry[0].read_bytes() == repaired
    library.deploy()
    assert (game / (BODY + ".mdf2.45")).read_bytes() == repaired
    assert caplog.text.count("게임 업데이트 뒤에도 전에 복구한 파일이 그대로 맞아 계속 씁니다") == 1
    assert run(library) == [] and library.material_state(mod.id) == "repaired"  # (checked again: the same)

    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(current_body(), 45)})
    assert library.material_state(mod.id) == "unchecked" and not library.repaired_materials(mod.id)


def test_a_game_update_that_changes_the_material_needs_the_repair_again(library, game, tmp_path):
    from mhwmm import precheck

    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    run(library, [mod.id], repair=True)
    entry = library.mod_entries(mod.id)[BODY + ".mdf2.45"]
    repaired = entry[0].read_bytes()

    update_game(game, {BODY + ".mdf2.45": assemble_mdf2(newer_body(), 45)})  # the repair lacks the new parameter
    assert run(library, unchecked_only=True) == []
    assert library.material_state(mod.id) == "stale" and library.material_repairs_outdated(mod.id)
    assert library.problems() == {mod.id: [precheck.REPAIRS_OUTDATED]} and library.materials_to_offer() == [mod]
    (issue,) = [i for i in precheck.mod_issues(precheck.mod_states(library)) if i.kind == "material"]
    assert issue.text.startswith("게임이 업데이트되어 'Chen'의 전에 복구한 재질·.user·.pfb 파일이 현재 게임과 맞지 않습니다")
    assert issue.action == "matfix" and issue.mods == [mod.id]
    assert library.mod_entries(mod.id)[BODY + ".mdf2.45"] == entry and entry[0].read_bytes() == repaired  # still used

    assert run(library, [mod.id], repair=True) == [mod]
    assert library.material_state(mod.id) == "repaired" and not library.material_repairs_outdated(mod.id)
    assert library.problems() == {}
    library.deploy()
    mat = extract_material(Mdf2File((game / (BODY + ".mdf2.45")).read_bytes(), 45), 0)
    assert "NewerParam" in {p["name"] for p in mat["props"]}
    assert {t["type"]: t["path"] for t in mat["textures"]}["BaseDielectricMap"] == "Art/Dimcirui/chen/body_ALBD.tex"


def test_one_repair_that_no_longer_fits_leaves_the_mod_stale(library, game, tmp_path):
    weapon = [material("body", [("BaseDielectricMap", "Art/myweapon/w_ALBD.tex")], [("Color", (0.3, 0.3, 0.3, 1.0))])]
    mod = install_tree(library, tmp_path / "Both", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45),
                                                    WEAPON + ".mdf2.45": assemble_mdf2(weapon, 45)})
    run(library, [mod.id], repair=True)
    repaired = library.repaired_materials(mod.id)
    assert set(repaired) == {BODY + ".mdf2.45", WEAPON + ".mdf2.45"}

    # the body's material changed, not the weapon's
    update_game(game, {BODY + ".mdf2.45": assemble_mdf2(newer_body(), 45)})
    jobs = library.material_jobs(unchecked_only=True)
    found = library.run_materials(jobs)
    assert found.carried == {mod.id: {BODY + ".mdf2.45": False, WEAPON + ".mdf2.45": True}}
    assert library.finish_materials(found, jobs) == []
    assert library.material_state(mod.id) == "stale" and library.material_repairs_outdated(mod.id)
    assert library.repaired_materials(mod.id) == repaired  # both stay in use until it is repaired again


def test_a_file_the_update_made_stale_needs_its_first_repair(library, game, tmp_path):
    """The repair in use still fits, but the update made another of the mod's files stale: the repair carries over
    and the mod is stale for that file alone (told as one never repaired, not as one to repair again)."""
    legs = [material("legs", [("BaseDielectricMap", "Art/mine/legs.tex")], [("Color", (0.1,) * 4)])]
    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45),
                                                    LEGS + ".mdf2.45": assemble_mdf2(legs, 45)})
    run(library, [mod.id], repair=True)
    assert set(library.repaired_materials(mod.id)) == {BODY + ".mdf2.45"}  # (the legs fit then)

    newer_legs = [material("legs", [("BaseDielectricMap", "Art/ch03/legs.tex"), ("NormalRoughnessMap", "n.tex")],
                           [("Color", (1.0,) * 4)])]
    update_game(game, {BODY + ".mdf2.45": assemble_mdf2(current_body(), 45),
                       LEGS + ".mdf2.45": assemble_mdf2(newer_legs, 45)})
    jobs = library.material_jobs(unchecked_only=True)
    found = library.run_materials(jobs)
    assert found.carried == {mod.id: {BODY + ".mdf2.45": True}}
    library.finish_materials(found, jobs)
    assert library.material_state(mod.id) == "stale" and not library.material_repairs_outdated(mod.id)
    assert library._material_record(mod.id)["applied_game"] == library.game_version()
    assert library.problems()[mod.id] == ["재질이나 .user·.pfb 파일이 현재 게임과 맞지 않습니다 (오른쪽 클릭 → 재질 복구)"]
    # a repair from before in use for a file that fits now, the legs stale: not one to repair again either
    record = copy.deepcopy(library._material_record(mod.id))
    record["applied_game"] = "the version before"
    for r in record["results"]:
        r["state"] = "current" if r["key"] == BODY + ".mdf2.45" else r["state"]
    library._save_material_record(mod.id, record)
    assert library.material_state(mod.id) == "stale" and not library.material_repairs_outdated(mod.id)
    run(library, [mod.id], repair=True)
    assert library.material_state(mod.id) == "repaired"
    assert set(library.repaired_materials(mod.id)) == {BODY + ".mdf2.45", LEGS + ".mdf2.45"}


@pytest.mark.parametrize("change", ["made_for", "file"])
def test_repairs_changed_while_they_were_checked_do_not_carry_over(library, game, tmp_path, change):
    """finish_materials keeps the repairs only when they are still the ones the run looked at."""
    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    run(library, [mod.id], repair=True)
    update_game(game)
    jobs = library.material_jobs(unchecked_only=True)
    found = library.run_materials(jobs)
    assert found.carried == {mod.id: {BODY + ".mdf2.45": True}}
    record = copy.deepcopy(library._material_record(mod.id))
    if change == "made_for":
        record["applied_game"] = "another version"
    else:
        entry = record["applied"][BODY + ".mdf2.45"]
        other = library.mod_dir(mod.id) / "matfix" / "002_other.mdf2.45"
        other.write_bytes((library.mod_dir(mod.id) / entry["file"]).read_bytes())
        entry["file"] = "matfix/002_other.mdf2.45"
    library._save_material_record(mod.id, record)
    library.finish_materials(found, jobs)
    assert library.material_state(mod.id) == "stale"


@pytest.mark.parametrize("kind", ["mdf2", "user"])
def test_a_repaired_copy_that_cannot_be_read_does_not_carry_over(library, game, tmp_path, types, kind):
    rel, shipped = (BODY + ".mdf2.45", assemble_mdf2(old_body_mod(), 45)) if kind == "mdf2" else (AVP, old_avp())
    mod = install_tree(library, tmp_path / "Mod", {rel: shipped})
    run(library, [mod.id], repair=True)
    assert library.material_state(mod.id) == "repaired"
    library.repaired_materials(mod.id)[rel][1].write_bytes(b"broken")
    update_game(game)
    run(library, unchecked_only=True)
    assert library.material_state(mod.id) == "stale" and library.material_repairs_outdated(mod.id)


@pytest.mark.parametrize("compression", [pakmod.COMPRESSION_NONE, pakmod.COMPRESSION_ZSTD], ids=["stored", "zstd"])
@pytest.mark.parametrize("damage", ["cut", "half", "garbled"])
def test_a_repaired_pak_whose_entries_cannot_be_read_does_not_carry_over(library, game, tmp_path, damage,
                                                                          compression):
    """check_mod passes over a PAK's entry it cannot read (no result, or a PAK that looks current): the repaired
    copy must still read every entry the check of the mod's own PAK reads. Damaged, the mod is offered a repair,
    which makes the PAK again from the mod's own one."""
    material_rel = WEAPON + ".mdf2.45"
    data = assemble_mdf2([material("body", [("BaseDielectricMap", "Art/mine/w.tex")], [("Color", (0.1,) * 4)])], 45)
    tex = os.urandom(3000)
    pack = (lambda b: matfix.zstd_compress(b)) if compression == pakmod.COMPRESSION_ZSTD else (lambda b: b)
    source = tmp_path / "Weapon" / "Weapon.pak"
    source.parent.mkdir()
    pakmod.write_pak([(pakmod.path_hash(material_rel), pack(data), len(data), compression),
                      (pakmod.path_hash("natives/STM/Art/mine/w.tex.241106027"), pack(tex), len(tex), compression)],
                     source)
    with library.prepare_install(source) as session:
        mod = library.install_package(session, session.variants)
    run(library, [mod.id], repair=True)
    assert library.material_state(mod.id) == "repaired"
    ((_rel, path),) = library.repaired_materials(mod.id).values()
    pak = pakmod.PakFile(path)
    off, csize, _dsize, _attrs = pak.entry(pak.find(pakmod.path_hash(material_rel)))
    raw = bytearray(path.read_bytes())
    if damage == "cut":  # nothing after the entry table
        raw = raw[:min(pak.offsets)]
    elif damage == "half":  # cut short in the middle of the material
        raw = raw[:off + csize // 2]
    else:  # the material's bytes after its first four overwritten
        raw[off + 4:off + csize] = b"\xff" * (csize - 4)
    path.write_bytes(bytes(raw))

    update_game(game)  # nothing about the materials changes
    jobs = library.material_jobs(unchecked_only=True)
    found = library.run_materials(jobs)
    assert found.carried == {mod.id: {"Weapon.pak": False}}
    library.finish_materials(found, jobs)
    assert library.material_state(mod.id) == "stale" and library.material_repairs_outdated(mod.id)
    assert library.materials_to_offer() == [mod]
    assert run(library, [mod.id], repair=True) == [mod]
    assert library.material_state(mod.id) == "repaired"
    ((_rel, path),) = library.repaired_materials(mod.id).values()
    assert pakmod.PakFile(path).read_hash(pakmod.path_hash(material_rel))[:4] == b"MDF\x00"


NEWER_CRC = 0x5555  # the .user type's layout after another game update: one more field
NEWER_FIELDS = R.PARAM_FIELDS + [["_Extra", "S32", 4, 4, 0]]


def newer_game_avp():
    values = R.param(R.PARAM_FIELDS, scale=1.0, hide=0, new=1) + [R.s32(9)]
    return R.user_file([(R.CHILD, R.CHILD_CRC, R.CHILD_FIELDS, [R.s32(7)]), (R.PARAM, NEWER_CRC, NEWER_FIELDS, values)])


def newer_types(history=True, **extra):
    """Type data that came out after that update: it knows the new layout and the ones before (without
    ``history``: not the one just before it)."""
    from mhwmm.matfix import rsz

    types = {format(R.PARAM, "x"): [format(NEWER_CRC, "x"), R.PARAM_NAME, R.json.dumps(NEWER_FIELDS)],
             format(R.CHILD, "x"): [format(R.CHILD_CRC, "x"), "app.TestChild", R.json.dumps(R.CHILD_FIELDS)], **extra}
    old = {format(R.OLD_CRC, "x"): R.json.dumps(R.OLD_PARAM)}
    if history:
        old[format(R.NEW_CRC, "x")] = R.json.dumps(R.PARAM_FIELDS)
    return rsz.TypeRegistry({"types": types, "empty": {}, "alt": {}, "old": {format(R.PARAM, "x"): old}})


@pytest.mark.parametrize("in_pak", [False, True], ids=["loose", "pak"])
def test_a_repair_the_updated_game_cannot_check_yet_does_not_carry_over(library, game, tmp_path, types, monkeypatch,
                                                                        in_pak):
    """A game update gave the .user type a layout the type data does not know yet: the mod's own .user and its
    repaired copy are both stuck. The copy is one the old repair rewrote, so it is not known to fit: the mod stays
    stale (to repair again). Carried over, nothing would check it again once newer type data comes - while a
    repair would then change it."""
    from mhwmm.matfix import rsz
    from mhwmm.matfix.repair import RszContext

    body, user = assemble_mdf2(old_body_mod(), 45), old_avp()
    if in_pak:
        source = tmp_path / "Both" / "Both.pak"
        source.parent.mkdir()
        pakmod.write_pak([(pakmod.path_hash(BODY + ".mdf2.45"), body, len(body), 0),
                          (pakmod.path_hash(AVP), user, len(user), 0)], source)
        with library.prepare_install(source) as session:
            mod = library.install_package(session, session.variants)
    else:
        mod = install_tree(library, tmp_path / "Both", {BODY + ".mdf2.45": body, AVP: user})
    run(library, [mod.id], repair=True)
    assert library.material_state(mod.id) == "repaired"
    repaired, made_for = library.repaired_materials(mod.id), library.game_version()
    fits = {"Both.pak": False} if in_pak else {BODY + ".mdf2.45": True, AVP: False}

    monkeypatch.setattr(rsz, "download_registry", lambda folder: None)  # nothing newer published yet
    update_game(game, {BODY + ".mdf2.45": assemble_mdf2(current_body(), 45), AVP: newer_game_avp()})
    jobs = library.material_jobs(unchecked_only=True)
    found = library.run_materials(jobs)
    assert found.carried == {mod.id: fits}
    assert library.finish_materials(found, jobs) == []
    assert library.material_state(mod.id) == "stale" and library.material_repairs_outdated(mod.id)
    assert library.material_stuck(mod.id) and library.materials_to_offer() == [mod]
    assert library._material_record(mod.id)["applied_game"] == made_for != library.game_version()
    assert library.repaired_materials(mod.id) == repaired  # (in use until the mod is repaired again)

    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", newer_types())  # newer type data comes
    monkeypatch.setattr(library, "registry_date", lambda refresh=False: "2099-01-01")
    assert library.material_state(mod.id) == "unchecked"
    jobs = library.material_jobs(unchecked_only=True)
    found = library.run_materials(jobs)
    assert found.carried == {mod.id: fits}  # (the repaired copies are checked again: still made for the old one)
    library.finish_materials(found, jobs)
    assert library.material_state(mod.id) == "stale" and library.material_repairs_outdated(mod.id)
    assert not library.material_stuck(mod.id) and library.materials_to_offer() == [mod]

    assert run(library, [mod.id], repair=True) == [mod]
    assert library.material_state(mod.id) == "repaired"
    rel = "Both.pak" if in_pak else AVP
    index = GameIndex(game, tmp_path / "cache2")
    (verdict,) = check_mod([(rel, rel, library.mod_entries(mod.id)[rel][0])], index, None, rszs=RszContext(index))
    assert verdict.state == "current"  # what is deployed now fits the updated game


def test_pak_and_user_file_repairs_carry_over(library, game, tmp_path, types):
    weapon = tmp_path / "Weapon" / "Weapon.pak"
    weapon.parent.mkdir()
    data = assemble_mdf2([material("body", [("BaseDielectricMap", "Art/w.tex")], [("Color", (0.5,) * 4)])], 45)
    pakmod.write_pak([(pakmod.path_hash(WEAPON + ".mdf2.45"), data, len(data), 0)], weapon)
    lepus = tmp_path / "Lepus" / "Lepus.pak"
    lepus.parent.mkdir()
    user = old_avp(scale=4.0)
    pakmod.write_pak([(pakmod.path_hash(AVP), user, len(user), 0),
                      (pakmod.path_hash("natives/STM/Art/x.tex.241106027"), b"TEX", 3, 0)], lepus)
    mods = []
    for source in (weapon, lepus):
        with library.prepare_install(source) as session:
            mods.append(library.install_package(session, session.variants))
    mods.append(install_tree(library, tmp_path / "Loose", {AVP: old_avp()}))
    run(library, repair=True)
    assert [library.material_state(m.id) for m in mods] == ["repaired"] * 3
    entries = {m.id: library.mod_entries(m.id) for m in mods}
    assert [sorted(k for _, k in entries[m.id].values() if k.startswith("matfix/")) for m in mods] == \
        [["matfix/Weapon.pak"], ["matfix/Lepus.pak"], ["matfix/" + AVP]]

    update_game(game)
    assert run(library, unchecked_only=True) == []
    assert [library.material_state(m.id) for m in mods] == ["repaired"] * 3
    assert {m.id: library.mod_entries(m.id) for m in mods} == entries and library.problems() == {}
    assert all(library._material_record(m.id)["applied_game"] == library.game_version() for m in mods)


def test_records_of_v270_after_a_game_update_are_checked_again(library, game, tmp_path):
    """v2.7.0 checked every repaired mod after a game update and called it stale (format 5): the new rules check
    those records again, so the repairs that still fit carry over right after the manager is updated."""
    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    run(library, [mod.id], repair=True)
    update_game(game)
    run(library, unchecked_only=True)
    record = dict(library._material_record(mod.id))
    record.update(format=5, applied_game="the version before the update")  # what v2.7.0 left
    library._save_material_record(mod.id, record)
    assert library.material_state(mod.id) == "unchecked"
    assert [j.mod_id for j in library.material_jobs(unchecked_only=True)] == [mod.id]
    run(library, unchecked_only=True)
    assert library.material_state(mod.id) == "repaired" and library.materials_to_offer() == []
    assert library._material_record(mod.id)["applied_game"] == library.game_version()


def test_versions_and_paks_of_installed_mods(library, game, tmp_path):
    legs = [material("legs", [("BaseDielectricMap", "Art/mine/legs.tex")], [("Color", (0.1,) * 4)])]
    old = install_tree(library, tmp_path / "Legs", {LEGS + ".mdf2.40": assemble_mdf2(legs, 40)})
    both = install_tree(library, tmp_path / "Both", {BODY + ".mdf2.40": assemble_mdf2(old_body_mod(), 40),
                                                     BODY + ".mdf2.45": assemble_mdf2(current_body(), 45)})
    weapon = tmp_path / "Weapon" / "Weapon.pak"
    weapon.parent.mkdir()
    data = assemble_mdf2([material("body", [("BaseDielectricMap", "Art/w.tex")], [("Color", (0.5,) * 4)])], 45)
    pakmod.write_pak([(pakmod.path_hash(WEAPON + ".mdf2.45"), data, len(data), 0)], weapon)
    with library.prepare_install(weapon) as session:
        pak_mod = library.install_package(session, session.variants)

    assert [rel for _, rel, _ in library.material_files(both.id)] == [BODY + ".mdf2.45"]  # the game reads .45
    run(library, repair=True)
    assert (library.material_state(old.id), library.material_state(both.id), library.material_state(pak_mod.id)) == \
        ("repaired", "current", "repaired")
    assert sorted(library.mod_files(old.id)) == [LEGS + ".mdf2.45"]  # renamed to the version the game reads
    library.deploy()
    assert (game / (LEGS + ".mdf2.45")).is_file() and not (game / (LEGS + ".mdf2.40")).exists()
    (placed,) = (game / "pak_mods").glob("*.pak")
    assert len(Mdf2File(pakmod.PakFile(placed).read(0), 45).materials[0].props) == 3


def test_gui_finds_offers_repairs_and_restores(library, tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    import queue
    import time

    from mhwmm import gui

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("화면(display)이 없어 GUI 테스트를 건너뜁니다")
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: None)
    s = library.settings
    s.check_updates = s.check_refw_updates = s.check_mod_updates = s.check_materials = False
    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    offered = []

    def show(dialog):
        offered.append((list(dialog.checked), dict(app._problems)))
        dialog.destroy()
        return [mod.id]

    monkeypatch.setattr(gui.MaterialsDialog, "show", show)

    def pump(until):
        end = time.time() + 20
        while time.time() < end:
            root.update()
            if until():
                return
            time.sleep(0.02)
        raise AssertionError("시간 초과")

    try:
        app = gui.App(root, library, queue.Queue())
        s.check_materials = True
        app.check_materials_background()  # what startup and installs do
        pump(lambda: offered and app.busy is None and library.material_state(mod.id) == "repaired")
        (checked, problems), = offered
        assert checked == [mod.id] and "재질" in problems[mod.id][0]
        assert mod.id not in app._problems
        pump(lambda: not library.dirty)  # applied right away
        app.tree.selection_set(mod.id)
        app._update_details()
        assert "재질 복구됨" in app.info_text.get("1.0", "end")
        statuses = [app.files_tree.item(i, "values")[1] for i in app.files_tree.get_children()]
        assert any("재질 복구본" in st for st in statuses)

        app.check_materials_background()  # nothing new: no second offer
        pump(lambda: not app._matfix_running)
        assert len(offered) == 1

        app.restore_materials(mod.id)
        assert library.material_state(mod.id) == "declined" and mod.id not in app._problems
    finally:
        root.destroy()


def test_cli_checks_repairs_and_restores(library, tmp_path, capsys):
    from mhwmm.cli import main

    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    data = str(library.data_dir)
    assert main(["--data-dir", data, "materials", "check"]) == 0
    out = capsys.readouterr().out
    assert "Chen: 복구 필요" in out and "body" in out
    assert main(["--data-dir", data, "matfix", "repair", "Chen"]) == 0
    assert "Chen: 복구됨" in capsys.readouterr().out
    assert main(["--data-dir", data, "materials", "restore", "Chen", "--no-deploy"]) == 0
    assert "원래 재질 파일로 되돌림" in capsys.readouterr().out
    from mhwmm.manager import ModManager
    assert ModManager(library.data_dir).material_state(mod.id) == "declined"



def test_cli_says_which_repairs_carried_over_and_which_to_redo(library, game, tmp_path, types, capsys):
    """The labels, and the mods' own stale files under a "복구됨" one: handled by the repairs in use (not "복구 필요")."""
    from mhwmm import cli
    from mhwmm.cli import main

    install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    install_tree(library, tmp_path / "Lepus", {AVP: old_avp()})
    data = str(library.data_dir)
    assert main(["--data-dir", data, "matfix", "repair"]) == 0
    capsys.readouterr()
    body, user = f"{BODY}.mdf2.45: 재질 body", f"{AVP}: user"
    update_game(game)
    assert main(["--data-dir", data, "materials", "check"]) == 0
    out = capsys.readouterr().out
    assert f"Chen: {cli.MATERIAL_CARRIED}" in out and f"Lepus: {cli.MATERIAL_CARRIED}" in out
    assert f"{body}{cli.MATERIAL_IN_USE}\n" in out and f"{user}{cli.MATERIAL_IN_USE}\n" in out
    assert "복구 필요" not in out and "복구하려면" not in out
    assert main(["--data-dir", data, "materials", "check"]) == 0  # (checked for this version already)
    out = capsys.readouterr().out
    assert "Chen: 복구됨\n" in out and "Lepus: 복구됨\n" in out
    assert f"{body}{cli.MATERIAL_IN_USE}\n" in out and f"{user}{cli.MATERIAL_IN_USE}\n" in out
    assert "복구 필요" not in out
    update_game(game, {BODY + ".mdf2.45": assemble_mdf2(newer_body(), 45)})
    assert main(["--data-dir", data, "materials", "check"]) == 0
    out = capsys.readouterr().out
    assert f"Chen: {cli.MATERIAL_REDO}" in out and "복구하려면: materials repair" in out
    assert f"{body}\n" in out and f"Lepus: {cli.MATERIAL_CARRIED}" in out and f"{user}{cli.MATERIAL_IN_USE}\n" in out


def test_gui_tells_repairs_from_before_a_game_update(library, game, tmp_path):
    tk = pytest.importorskip("tkinter")
    import queue

    from mhwmm import gui, precheck

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("화면(display)이 없어 GUI 테스트를 건너뜁니다")
    s = library.settings
    s.check_updates = s.check_refw_updates = s.check_mod_updates = s.check_materials = False
    weapon = [material("body", [("BaseDielectricMap", "Art/myweapon/w_ALBD.tex")], [("Color", (0.3, 0.3, 0.3, 1.0))])]
    chen = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    run(library, [chen.id], repair=True)
    never = install_tree(library, tmp_path / "Never", {WEAPON + ".mdf2.45": assemble_mdf2(weapon, 45)})
    update_game(game, {BODY + ".mdf2.45": assemble_mdf2(newer_body(), 45)})
    run(library, unchecked_only=True)
    assert [library.material_state(m.id) for m in (chen, never)] == ["stale", "stale"]
    try:
        app = gui.App(root, library, queue.Queue())
        app.refresh()
        assert app._row_tooltip(chen.id) == "⚠ " + precheck.REPAIRS_OUTDATED
        assert app._row_tooltip(never.id) == "⚠ 재질이나 .user·.pfb 파일이 현재 게임과 맞지 않습니다 (오른쪽 클릭 → 재질 복구)"
        texts = {}
        for mod in (chen, never):
            app.tree.selection_set(mod.id)
            app._update_details()
            texts[mod.id] = app.info_text.get("1.0", "end")
        assert "재질 다시 복구 필요" in texts[chen.id] and "전에 복구한 파일이 현재 게임과 맞지 않음" in texts[chen.id]
        assert "게임이 업데이트되어 다시 복구해야 합니다" in texts[chen.id] and "원래대로 되돌리기" in texts[chen.id]
        assert "현재 게임과 맞지 않는 파일이 있습니다" not in texts[chen.id]
        assert "재질 복구 필요" in texts[never.id] and "다시 복구" not in texts[never.id]
        assert "현재 게임과 맞지 않는 파일이 있습니다" in texts[never.id]
        offer = gui.MaterialsDialog(root, app.theme, library, [chen, never])
        assert offer.tree.item(chen.id, "values")[0].startswith("다시 복구 · 파일 1개 · 재질 body")
        assert offer.tree.item(never.id, "values")[0].startswith("파일 1개 · 재질 body")
        offer.destroy()
    finally:
        root.destroy()


# ---- .user / .pfb files ----------------------------------------------------------
@pytest.fixture
def types(monkeypatch):
    """The small registry of test_rsz instead of the bundled one."""
    from mhwmm.matfix import rsz

    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", R.registry())
    monkeypatch.setattr(rsz.TypeRegistry, "_from_file", False)  # release() must not drop it (other tests load the real one)


def old_avp(scale=2.5):
    return R.user_file([(R.CHILD, R.CHILD_CRC, R.CHILD_FIELDS, [R.s32(42)]),
                        (R.PARAM, R.OLD_CRC, R.OLD_PARAM, R.param(R.OLD_PARAM, scale=scale, hide=1))])


def test_installed_user_file_is_repaired_and_restored(library, game, tmp_path, types, caplog):
    shipped = old_avp()
    mod = install_tree(library, tmp_path / "Lepus", {AVP: shipped})
    run(library, unchecked_only=True)
    assert library.material_state(mod.id) == "stale"
    (result,) = library.material_results(mod.id)
    assert result.kind == "user" and result.rsz == [{"label": AVP, "kind": "user", "method": "재구성"}]

    assert run(library, [mod.id], repair=True) == [mod]
    assert library.material_state(mod.id) == "repaired" and not library.material_stuck(mod.id)
    path, key = library.mod_entries(mod.id)[AVP]
    assert key == "matfix/" + AVP and (library.files_dir(mod.id) / AVP).read_bytes() == shipped
    library.deploy()
    deployed = (game / AVP).read_bytes()
    values, info = R.fields_of(deployed)
    assert info.insts[2] == (R.PARAM, R.NEW_CRC) and values[(2, "_Scale")] == R.f32(2.5)

    assert library.restore_materials(mod.id)
    library.deploy()
    assert (game / AVP).read_bytes() == shipped


def test_user_file_inside_a_mod_pak(library, game, tmp_path, types):
    pak = tmp_path / "Lepus" / "Lepus.pak"
    pak.parent.mkdir()
    data = old_avp(scale=4.0)
    pakmod.write_pak([(pakmod.path_hash(AVP), data, len(data), 0),
                      (pakmod.path_hash("natives/STM/Art/x.tex.241106027"), b"TEX", 3, 0)], pak)
    with library.prepare_install(pak) as session:
        mod = library.install_package(session, session.variants)
    run(library, repair=True)
    assert library.material_state(mod.id) == "repaired"
    (result,) = library.material_results(mod.id)
    assert result.rsz[0]["method"] == "재구성" and result.rsz[0]["label"].endswith(".user")
    fixed = pakmod.PakFile(library.mod_entries(mod.id)["Lepus.pak"][0])
    values, info = R.fields_of(fixed.read_hash(pakmod.path_hash(AVP)))
    assert info.insts[2] == (R.PARAM, R.NEW_CRC) and values[(2, "_Scale")] == R.f32(4.0)
    assert fixed.read_hash(pakmod.path_hash("natives/STM/Art/x.tex.241106027")) == b"TEX"


def test_user_file_that_cannot_be_repaired_is_reported(library, game, tmp_path, types, monkeypatch):
    from mhwmm.matfix import rsz

    extra = {format(R.EXTRA, "x"): ["4444", "app.CustomChain", R.json.dumps(R.CHILD_FIELDS)]}
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", R.registry(**extra))
    short = R.PARAM_FIELDS[:-1]
    shipped = R.user_file([(R.EXTRA, 0x4444, R.CHILD_FIELDS, [R.s32(1)]),
                           (R.CHILD, R.CHILD_CRC, R.CHILD_FIELDS, [R.s32(42)]),
                           (R.PARAM, R.NO_CHILD_CRC, short, R.param(short))])
    mod = install_tree(library, tmp_path / "Lepus", {AVP: shipped})
    run(library, repair=True)
    (stuck,) = library.material_stuck(mod.id)
    assert stuck["label"] == AVP and "app.CustomChain" in stuck["reason"]
    assert library.material_state(mod.id) == "current" and not library.repaired_materials(mod.id)


def test_rsz_lines_in_the_log(library, game, tmp_path, types, caplog):
    pytest.importorskip("tkinter")
    import logging

    from mhwmm import gui

    class App:  # just enough of gui.App for its log of a run (no window): its methods, on this manager
        manager = library

        def __getattr__(self, name):
            return getattr(gui.App, name).__get__(self)

    mod = install_tree(library, tmp_path / "Lepus", {AVP: old_avp()})
    jobs = library.material_jobs([mod.id])
    found = library.run_materials(jobs, repair=True)
    with caplog.at_level(logging.INFO):
        App()._log_materials(found, jobs)
    library.finish_materials(found, jobs)
    assert f"'Lepus' {AVP}: user 복구 (재구성)" in caplog.text


def test_records_of_the_previous_version_are_checked_again(library, game, tmp_path, types):
    """v1.9.0 checked only .mdf2: its records must not hide a stale .user after the update."""
    mod = install_tree(library, tmp_path / "Lepus", {AVP: old_avp()})
    run(library, unchecked_only=True)
    record = dict(library._material_record(mod.id))
    game_now = library.game_version()
    record.update(results=[], offered=game_now)  # what v1.9.0 left behind: no format, mdf2 results only
    record.pop("format")
    library._save_material_record(mod.id, record)
    assert library.material_state(mod.id) == "unchecked"
    assert [j.mod_id for j in library.material_jobs(unchecked_only=True)] == [mod.id]
    run(library, unchecked_only=True)
    assert library.material_state(mod.id) == "stale" and library.materials_to_offer() == [mod]


def test_diagnostics_report_holds_both_files(library, game, tmp_path, types, capsys):
    import json
    import zipfile

    from mhwmm import manager
    from mhwmm.cli import main

    shipped = old_avp()
    mod = install_tree(library, tmp_path / "Lepus", {AVP: shipped, BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    run(library, repair=True)
    (job,) = library.material_jobs([mod.id])
    out = tmp_path / "report.zip"
    assert library.export_material_report(job, out) == 1
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert {f"mod/{AVP}", f"game/{AVP}", f"repaired/{AVP}", "report.json"} <= names
        assert zf.read(f"mod/{AVP}") == shipped and zf.read(f"game/{AVP}") == R.game_file()
        report = json.loads(zf.read("report.json"))
    assert report["mod"] == "Lepus" and report["record"]["format"] == manager.MATFIX_FORMAT
    assert main(["--data-dir", str(library.data_dir), "materials", "report", "Lepus", "--out", str(tmp_path / "cli.zip")]) == 0
    assert (tmp_path / "cli.zip").is_file() and "1개" in capsys.readouterr().out


def test_a_pak_with_an_entry_that_cannot_be_repaired_carries_over(library, game, tmp_path, types, monkeypatch):
    """A PAK's entry that could not be repaired stays as shipped in its repair: checked again after a game update
    the repaired PAK still has it, as a new repair would - that alone does not make it stale."""
    from mhwmm.matfix import rsz

    extra = {format(R.EXTRA, "x"): ["4444", "app.CustomChain", R.json.dumps(R.CHILD_FIELDS)]}
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", R.registry(**extra))
    short = R.PARAM_FIELDS[:-1]
    user = R.user_file([(R.EXTRA, 0x4444, R.CHILD_FIELDS, [R.s32(1)]),
                        (R.CHILD, R.CHILD_CRC, R.CHILD_FIELDS, [R.s32(42)]),
                        (R.PARAM, R.NO_CHILD_CRC, short, R.param(short))])
    weapon = assemble_mdf2([material("body", [("BaseDielectricMap", "Art/w.tex")], [("Color", (0.5,) * 4)])], 45)
    source = tmp_path / "Mixed" / "Mixed.pak"
    source.parent.mkdir()
    pakmod.write_pak([(pakmod.path_hash(WEAPON + ".mdf2.45"), weapon, len(weapon), 0),
                      (pakmod.path_hash(AVP), user, len(user), 0)], source)
    with library.prepare_install(source) as session:
        mod = library.install_package(session, session.variants)
    run(library, repair=True)
    (result,) = library.material_results(mod.id)
    assert result.state == "fixable" and len(result.stuck) == 1 and library.material_state(mod.id) == "repaired"

    made_for = library.game_version()
    update_game(game)
    jobs = library.material_jobs(unchecked_only=True)
    found = library.run_materials(jobs)
    assert found.carried == {mod.id: {"Mixed.pak": True}}  # (the type data is this game version's)
    library.finish_materials(found, jobs)
    assert library.material_state(mod.id) == "repaired" and len(library.material_stuck(mod.id)) == 1
    assert library._material_record(mod.id)["carried"] == {"from": made_for, "registry": library.registry_date()}


EXTRA_PART = {format(R.EXTRA, "x"): ["4444", "app.TestExtraPart", R.json.dumps(R.CHILD_FIELDS)]}


def avp_with_a_part():
    """A mod's .user made for the fixture's game, with a part of its own the game's file does not have."""
    return R.user_file([(R.EXTRA, 0x4444, R.CHILD_FIELDS, [R.s32(1)]), (R.CHILD, R.CHILD_CRC, R.CHILD_FIELDS, [R.s32(42)]),
                        (R.PARAM, R.NEW_CRC, R.PARAM_FIELDS, R.param(R.PARAM_FIELDS, scale=3.0, hide=1))])


@pytest.mark.parametrize("data", ["behind", "no history"])
def test_a_pak_entry_stuck_in_the_updated_game_is_checked_again_with_newer_type_data(library, game, tmp_path, monkeypatch,
                                                                                    data):
    """The repaired PAK has the .user entry as shipped (it fit the game then). A game update changes the .user type,
    and the entry is stuck in the mod's PAK and in the repaired one alike:
    - "behind": the type data does not know the new layout yet (it cannot read the game's file). Stuck only for
      that, the entry is not known to fit: no carry-over, the mod is to repair again.
    - "no history": the type data is this version's, only without the entry's old layout. The repairs carry over,
      and the record says so.
    Newer type data then finds the entry stale in both PAKs: the mod is to repair again (carried over, the check
    looks at the repaired PAK again), and a repair makes it fit."""
    from mhwmm import precheck
    from mhwmm.matfix import rsz
    from mhwmm.matfix.repair import RszContext

    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", R.registry(**EXTRA_PART))
    monkeypatch.setattr(rsz.TypeRegistry, "_from_file", False)
    weapon = assemble_mdf2([material("body", [("BaseDielectricMap", "Art/w.tex")], [("Color", (0.5,) * 4)])], 45)
    user = avp_with_a_part()
    source = tmp_path / "Mixed" / "Mixed.pak"
    source.parent.mkdir()
    pakmod.write_pak([(pakmod.path_hash(WEAPON + ".mdf2.45"), weapon, len(weapon), 0),
                      (pakmod.path_hash(AVP), user, len(user), 0)], source)
    with library.prepare_install(source) as session:
        mod = library.install_package(session, session.variants)
    run(library, [mod.id], repair=True)
    assert library.material_state(mod.id) == "repaired"
    repaired, made_for = library.repaired_materials(mod.id), library.game_version()
    assert pakmod.PakFile(repaired["Mixed.pak"][1]).read_hash(pakmod.path_hash(AVP)) == user  # (left as shipped)

    monkeypatch.setattr(rsz, "download_registry", lambda folder: None)  # nothing newer published yet
    if data == "no history":
        monkeypatch.setattr(rsz.TypeRegistry, "_loaded", newer_types(history=False, **EXTRA_PART))
    update_game(game, {BODY + ".mdf2.45": assemble_mdf2(current_body(), 45), AVP: newer_game_avp()})
    jobs = library.material_jobs(unchecked_only=True)
    found = library.run_materials(jobs)
    (own,) = found.results[mod.id]
    assert own.state == "fixable" and [i["label"] for i in own.stuck] == [f"Mixed.pak#{pakmod.path_hash(AVP):016X}.user"]
    assert found.carried == {mod.id: {"Mixed.pak": data == "no history"}}
    assert library.finish_materials(found, jobs) == []
    record = library._material_record(mod.id)
    if data == "behind":
        assert library.material_state(mod.id) == "stale" and library.material_repairs_outdated(mod.id)
        assert record["applied_game"] == made_for and "carried" not in record
        assert library.materials_to_offer() == [mod]
    else:
        assert library.material_state(mod.id) == "repaired" and library.materials_to_offer() == []
        assert record["applied_game"] == library.game_version()
        assert record["carried"] == {"from": made_for, "registry": library.registry_date()}
    assert library.material_stuck(mod.id)

    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", newer_types(**EXTRA_PART))  # newer type data comes
    monkeypatch.setattr(library, "registry_date", lambda refresh=False: "2099-01-01")
    assert library.material_state(mod.id) == "unchecked"
    jobs = library.material_jobs(unchecked_only=True)
    found = library.run_materials(jobs)
    assert found.carried == {mod.id: {"Mixed.pak": False}}  # the entry in the repaired PAK is stale now
    assert library.finish_materials(found, jobs) == []
    record = library._material_record(mod.id)
    assert library.material_state(mod.id) == "stale" and library.material_repairs_outdated(mod.id)
    assert record["applied_game"] == made_for != library.game_version() and "carried" not in record
    assert not library.material_stuck(mod.id) and library.materials_to_offer() == [mod]
    assert library.problems() == {mod.id: [precheck.REPAIRS_OUTDATED]}
    assert library.repaired_materials(mod.id) == repaired  # (in use until the mod is repaired again)

    assert run(library, [mod.id], repair=True) == [mod]
    assert library.material_state(mod.id) == "repaired" and "carried" not in library._material_record(mod.id)
    index = GameIndex(game, tmp_path / "cache2")
    path = library.mod_entries(mod.id)["Mixed.pak"][0]
    (verdict,) = check_mod([("Mixed.pak", "Mixed.pak", path)], index, None, rszs=RszContext(index))
    assert verdict.state == "current"  # what is deployed now fits the updated game


def test_carried_over_repairs_are_checked_again_with_newer_type_data(library, game, tmp_path, types, monkeypatch):
    """Newer .user/.pfb type data checks a mod whose repairs carried over a game update again, the repaired copies
    too: still fitting, they stay (the record says with which type data they were looked at)."""
    from mhwmm.matfix import rsz

    mod = install_tree(library, tmp_path / "Lepus", {AVP: old_avp(), BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    run(library, [mod.id], repair=True)
    made_for = library.game_version()
    update_game(game)
    run(library, unchecked_only=True)
    assert library.material_state(mod.id) == "repaired"
    assert library._material_record(mod.id)["carried"] == {"from": made_for, "registry": library.registry_date()}

    monkeypatch.setattr(rsz.TypeRegistry, "date_in_use", classmethod(lambda cls: "2099-01-01"))  # (the same types)
    monkeypatch.setattr(library, "registry_date", lambda refresh=False: "2099-01-01")
    assert library.material_state(mod.id) == "unchecked"
    jobs = library.material_jobs(unchecked_only=True)
    assert [(j.mod_id, j.carried, j.repaired_for == library.game_version()) for j in jobs] == [(mod.id, True, True)]
    found = library.run_materials(jobs)
    assert found.carried == {mod.id: {AVP: True, BODY + ".mdf2.45": True}}
    assert library.finish_materials(found, jobs) == []
    record = library._material_record(mod.id)
    assert library.material_state(mod.id) == "repaired" and library.problems() == {}
    assert record["applied_game"] == library.game_version()
    assert record["carried"] == {"from": made_for, "registry": "2099-01-01"}
    assert library.material_jobs(unchecked_only=True) == []

    library.restore_materials(mod.id)
    assert "carried" not in library._material_record(mod.id)


def test_carried_over_repairs_are_looked_at_by_every_check(library, game, tmp_path):
    """A check of a mod whose repairs carried over looks at the repaired copies again: one that no longer fits
    (here: no longer reads) makes the mod one to repair again, as before the carry-over."""
    from mhwmm import precheck

    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    run(library, [mod.id], repair=True)
    made_for = library.game_version()
    update_game(game)
    run(library, unchecked_only=True)
    assert library.material_state(mod.id) == "repaired" and "carried" in library._material_record(mod.id)
    library.mark_materials_offered([mod])
    ((_rel, path),) = library.repaired_materials(mod.id).values()
    path.write_bytes(b"broken")

    jobs = library.material_jobs([mod.id])
    found = library.run_materials(jobs)
    assert found.carried == {mod.id: {BODY + ".mdf2.45": False}}
    library.finish_materials(found, jobs)
    record = library._material_record(mod.id)
    assert library.material_state(mod.id) == "stale" and library.material_repairs_outdated(mod.id)
    assert record["applied_game"] == made_for and "carried" not in record
    assert library.problems() == {mod.id: [precheck.REPAIRS_OUTDATED]} and library.materials_to_offer() == [mod]
    assert run(library, [mod.id], repair=True) == [mod]
    assert library.material_state(mod.id) == "repaired" and "carried" not in library._material_record(mod.id)


def test_what_counts_as_a_repair_that_still_fits(tmp_path, monkeypatch):
    """The rules of _repairs_fit on made-up results of the repaired copies' check (``seen``) next to the mod's own
    files' (``own``). The PAKs are real files: an entry stuck in a repaired copy fits only when the old repair left
    it as shipped, and every entry the own PAK's check reads must read in the copy too."""
    from mhwmm import manager

    user = tmp_path / "ok.user.3"
    user.write_bytes(R.game_file())
    broken = tmp_path / "broken.user.3"
    broken.write_bytes(b"not an RSZ file")
    stuck_user = tmp_path / "stuck.user.3"
    stuck_user.write_bytes(R.game_file())
    weapon = assemble_mdf2([material("body", [("BaseDielectricMap", "Art/w.tex")], [("Color", (0.5,) * 4)])], 45)
    tex = ("natives/STM/Art/x.tex.241106027", b"TEX" * 10)

    def pak(path, entries):
        path.parent.mkdir(parents=True, exist_ok=True)
        pakmod.write_pak([(pakmod.path_hash(name), data, len(data), 0) for name, data in entries], path)
        return path

    def label(name, rel=AVP):
        return f"{name}#{pakmod.path_hash(rel):016X}.user"

    paks = {  # name: (the mod's own PAK, its repaired copy)
        "Stuck.pak": ([(AVP, old_avp())], [(AVP, old_avp())]),  # the stuck entry left as shipped
        "Other.pak": ([(AVP, old_avp())], [(AVP, old_avp())]),
        "Rewritten.pak": ([(AVP, old_avp())], [(AVP, R.game_file())]),  # the stuck entry is one the repair rewrote
        "Empty.pak": ([tex], [tex]),
        "Damaged.pak": ([(WEAPON + ".mdf2.45", weapon), tex], [(WEAPON + ".mdf2.45", b"MDF\x00" + b"\xff" * 64), tex]),
        "Gone.pak": ([(WEAPON + ".mdf2.45", weapon), tex], [tex]),
    }
    names = ["cur.mdf2.45", "fix.mdf2.45", "err.mdf2.45", "same.mdf2.45", "new.mdf2.45", *paks, "none.mdf2.45",
             "ok.user.3", "broken.user.3", "stuck.user.3"]
    paths = {"ok.user.3": user, "broken.user.3": broken, "stuck.user.3": stuck_user}
    own_paths = {}
    for name, (mine, repaired) in paks.items():
        own_paths[name] = pak(tmp_path / "own" / name, mine)
        paths[name] = pak(tmp_path / "repaired" / name, repaired)
    files = [(n, n, own_paths.get(n, tmp_path / "own" / n)) for n in names]
    job = manager.MaterialJob("m", "M", "s", files, repaired={n: (n, paths.get(n, tmp_path / n)) for n in names}
                              | {"elsewhere.mdf2.45": ("elsewhere.mdf2.45", tmp_path / "x")}, repaired_for="old")

    def result(key, state, unresolved=(), stuck=()):
        kind = "pak" if key.endswith(".pak") else key.split(".")[1]
        return FileResult(key, key, kind, state, unresolved=list(unresolved),
                          rsz=[{"label": label, "kind": "user", "method": "", "reason": "r"} for label in stuck])

    special = ("same.mdf2.45", "Stuck.pak", "Other.pak", "Rewritten.pak", "stuck.user.3")
    own = [result(n, "fixable") for n in names if n not in special]
    own += [result("same.mdf2.45", "fixable", ["m1"]), result("Stuck.pak", "fixable", stuck=[label("Stuck.pak")]),
            result("Other.pak", "fixable", stuck=[label("Other.pak")]),
            result("Rewritten.pak", "fixable", stuck=[label("Rewritten.pak")]),
            result("stuck.user.3", "stuck", stuck=["stuck.user.3"])]
    seen = [result("cur.mdf2.45", "current"), result("fix.mdf2.45", "fixable"), result("err.mdf2.45", "error"),
            result("same.mdf2.45", "unresolved", ["m1"]), result("new.mdf2.45", "unresolved", ["m2"]),
            result("Stuck.pak", "stuck", stuck=[label("Stuck.pak")]),
            result("Other.pak", "stuck", stuck=[label("Other.pak", WEAPON + ".mdf2.45")]),
            result("Rewritten.pak", "stuck", stuck=[label("Rewritten.pak")]),
            result("Damaged.pak", "current"),
            result("ok.user.3", "current"), result("broken.user.3", "current"),
            result("stuck.user.3", "stuck", stuck=["stuck.user.3"])]
    checked = []
    monkeypatch.setattr(manager, "check_mod", lambda files, *a, **k: checked.append(files) or seen)
    assert manager.ModManager._repairs_fit(job, own, None, None, None, None) == {
        "cur.mdf2.45": True, "fix.mdf2.45": False, "err.mdf2.45": False,
        "same.mdf2.45": True, "new.mdf2.45": False,  # (a material left as shipped only by the repaired copy)
        "Stuck.pak": True, "Other.pak": False,  # (an entry stuck only in the repaired copy)
        "Rewritten.pak": False,  # stuck in this game, and not as shipped: not known to fit (newer type data)
        "Empty.pak": True, "none.mdf2.45": False,  # nothing to look at in a PAK; a loose file always has a result
        "Damaged.pak": False, "Gone.pak": False,  # an entry the own PAK's check reads does not read in the copy
        "ok.user.3": True, "broken.user.3": False,  # (check_mod takes one that does not read for no RSZ file)
        "stuck.user.3": False,  # a repaired loose .user is one the old repair rewrote: stuck, it is not known to fit
        "elsewhere.mdf2.45": False}  # not one of the files looked at
    assert checked == [[(n, n, paths.get(n, tmp_path / n)) for n in names]]  # the repaired copies in their place

    # the entry left as shipped is stuck while the type data is behind the game (a game file it could not read or
    # whose CRCs it does not have, during the checks since ``marks``, or the game's sample .pfb): not known to fit
    from types import SimpleNamespace

    for context, marks, fits in [(SimpleNamespace(game_unreadable=2, registry_outdated=1, matches_game=lambda: None),
                                  (2, 1), True),
                                 (SimpleNamespace(game_unreadable=2, registry_outdated=1, matches_game=lambda: None),
                                  (2, 0), False),
                                 (SimpleNamespace(game_unreadable=1, registry_outdated=0, matches_game=lambda: None),
                                  None, False),
                                 (SimpleNamespace(game_unreadable=0, registry_outdated=0, matches_game=lambda: False),
                                  (0, 0), False)]:
        assert manager.ModManager._repairs_fit(job, own, None, None, context, None, marks)["Stuck.pak"] is fits


def test_stuck_files_are_checked_again_with_newer_type_data(library, game, tmp_path, types, monkeypatch):
    from mhwmm.matfix import repair, rsz

    extra = {format(R.EXTRA, "x"): ["4444", "app.CustomChain", R.json.dumps(R.CHILD_FIELDS)]}
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", R.registry(**extra))
    short = R.PARAM_FIELDS[:-1]
    shipped = R.user_file([(R.EXTRA, 0x4444, R.CHILD_FIELDS, [R.s32(1)]),
                           (R.CHILD, R.CHILD_CRC, R.CHILD_FIELDS, [R.s32(42)]),
                           (R.PARAM, R.NO_CHILD_CRC, short, R.param(short))])
    mod = install_tree(library, tmp_path / "Lepus", {AVP: shipped})
    downloads = []
    monkeypatch.setattr(repair.RszContext, "matches_game", lambda self: False)  # the game moved past our data
    monkeypatch.setattr(rsz, "download_registry", lambda folder: downloads.append(folder) or "2099-01-01")
    jobs = library.material_jobs([mod.id])
    found = library.run_materials(jobs)
    assert found.registry_updated == "2099-01-01" and len(downloads) == 1
    library.finish_materials(found, jobs)
    assert library.material_stuck(mod.id)
    run(library)  # tried for this game version already: no second download
    assert len(downloads) == 1
    monkeypatch.setattr(library, "registry_date", lambda refresh=False: "2099-01-01")
    assert library.material_state(mod.id) == "unchecked"  # the new data may repair it


def test_results_of_older_repair_rules_are_checked_again(library, game, tmp_path, types):
    """v1.11.0 stored a .pfb as not repairable; v1.11.1 could repair it, but the record (same game,
    same type data) kept the old result until the rules version was raised."""
    from mhwmm import manager

    mod = install_tree(library, tmp_path / "Lepus", {AVP: old_avp()})
    run(library)
    record = dict(library._material_record(mod.id))
    record["format"] = manager.MATFIX_FORMAT - 1
    library._save_material_record(mod.id, record)
    assert library.material_state(mod.id) == "unchecked"
    assert [j.mod_id for j in library.material_jobs(unchecked_only=True)] == [mod.id]
    run(library, unchecked_only=True)
    assert library.material_state(mod.id) == "stale" and library.materials_to_offer() == [mod]


def test_shader_index_cache_cut_short_is_made_again(tmp_path, monkeypatch):
    """The whole-game material list is saved aside and then put in place; a cache file cut short (the
    manager closed while it was written) is made again instead of failing every lookup."""
    import types

    from mhwmm.matfix import game as gamemod

    raw = assemble_mdf2(current_body(), 45)
    fake = types.SimpleNamespace(cache_dir=tmp_path, fingerprint="fp", key="fp",
                                 read_versioned=lambda base, ext, prefer=(): (f"{base}.mdf2.45", raw))
    monkeypatch.setattr(gamemod, "known_material_paths", lambda: [BODY + ".mdf2.45"])
    index = gamemod.ShaderIndex(fake)
    mmtr = Mdf2File(raw, 45).materials[0].mmtr_path
    assert index.find(mmtr)
    cache = tmp_path / "shaders-fp.json.gz"
    assert cache.is_file() and [p.name for p in tmp_path.iterdir()] == ["shaders-fp.json.gz"]  # (no .tmp left)
    cache.write_bytes(cache.read_bytes()[:20])  # cut short
    assert gamemod.ShaderIndex(fake).find(mmtr) == index.find(mmtr)
    assert gamemod.ShaderIndex(fake).find(mmtr)  # (and saved whole again)


# ---- shaders another mod puts in (a shader pack) ----------------------------------------------------
EF_SKIN = "MaterialShader/Variation/EF_Skin_NPR.mmtr"
EF_HAIR = "MaterialShader/Variation/EF_Hair_NPR.mmtr"
PROBE = "natives/STM/MaterialShader/Variation/Base_Equip.mmtr.240906211"  # (repair.SHADER_PROBE)
ROSSI = "natives/STM/Art/Model/Character/ch03/034/001/2/ch03_034_0012"


def npr_costume():
    """A costume whose materials use a shader pack's shaders: no game material uses them."""
    return [material("body", [("BaseDielectricMap", "Art/Dimcirui/rossi/body_ALBD.tex"),
                              ("NPR_SDFMaskMap", "Art/Dimcirui/rossi/body_SDF.tex")],
                     [("NPR_LightDir", (0.0, 1.0, 0.0, 0.0))], mmtr=EF_SKIN),
            material("hair", [("BaseDielectricMap", "Art/Dimcirui/rossi/hair_ALBD.tex")], [], mmtr=EF_HAIR)]


def shader_game(game, files=()):
    """The game with its shaders numbered as now (the probe shader) and ``files`` in a newer patch."""
    write_game_pak(game / "re_chunk_000.pak.patch_002.pak", {PROBE: b"SDF\x00", **{f: b"SDF\x00" for f in files}})


def test_materials_on_a_shader_the_game_does_not_have_are_listed_with_it(game, tmp_path):
    shader_game(game, ["natives/STM/" + EF_HAIR + ".250206176"])  # a shader file no listed material uses
    index = GameIndex(game, tmp_path / "cache")
    (result,) = check_mod(stage(tmp_path, {ROSSI + ".mdf2.45": assemble_mdf2(npr_costume(), 45)}), index, None)
    assert result.state == "unresolved" and result.unresolved == ["body", "hair"]
    assert result.shaders == [("body", EF_SKIN)]  # (the game has the hair shader: only no material of it)
    assert FileResult.from_dict(result.to_dict()).shaders == result.shaders
    assert FileResult.from_dict({**result.to_dict(), "shaders": [["x"], "y", ["a", "b"]]}).shaders == [("a", "b")]
    old = result.to_dict()
    del old["shaders"]  # (a record of an older version)
    assert FileResult.from_dict(old).shaders == []


def test_shaders_are_not_listed_when_the_game_numbers_its_shaders_otherwise(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")  # (no shader under a known version number at all)
    (result,) = check_mod(stage(tmp_path, {ROSSI + ".mdf2.45": assemble_mdf2(npr_costume(), 45)}), index, None)
    assert result.unresolved == ["body", "hair"] and result.shaders == []  # (unknown is not "missing")


def test_a_mod_paks_materials_on_shaders_the_game_does_not_have(game, tmp_path):
    shader_game(game)
    index = GameIndex(game, tmp_path / "cache")
    data = assemble_mdf2(npr_costume(), 45)
    mod_pak = tmp_path / "rossi.pak"
    pakmod.write_pak([(pakmod.path_hash(ROSSI + ".mdf2.45"), data, len(data), 0)], mod_pak)
    (result,) = check_mod([("rossi.pak", "rossi.pak", mod_pak)], index, None)
    assert result.kind == "pak" and result.state == "unresolved"
    assert result.shaders == [("body", EF_SKIN), ("hair", EF_HAIR)]


def shader_pack_tree():
    """The Endfield Style Shader Pack archive: the shaders, and an optional script (Fluffy packages)."""
    return {"Endfield Style Shader Pack/modinfo.ini": b"name = Endfield Style Shader Pack\n",
            "Endfield Style Shader Pack/natives/STM/" + EF_SKIN + ".240906211": b"SDF\x00skin",
            "Endfield Style Shader Pack/natives/STM/" + EF_HAIR + ".240906211": b"SDF\x00hair",
            "Endfield Style Shader Driver/modinfo.ini":
                b"name = Endfield Style Shader Driver\nRequirement=Endfield Style Shader Pack\n",
            "Endfield Style Shader Driver/reframework/autorun/endfield_npr_face_sdf.lua": b"-- face SDF\n"}


def shader_items(manager, mod_id):
    return [(r["state"], r["by"], r["option"], r["name"]) for r in manager.requirement_status(mod_id)
            if r["kind"] == "shader"]


def test_the_shader_pack_a_costume_uses_is_one_of_its_requirements(library, game, tmp_path):
    from mhwmm import bisect

    shader_game(game)
    rossi = install_tree(library, tmp_path / "Rossi", {ROSSI + ".mdf2.45": assemble_mdf2(npr_costume(), 45)})
    library.set_enabled(rossi.id, True)
    run(library, unchecked_only=True)
    assert library.material_state(rossi.id) == "current"  # (left as shipped: nothing to repair)
    assert shader_items(library, rossi.id) == [("missing", "", "", "셰이더 EF_Skin_NPR.mmtr 외 1개")]
    (req,) = library.missing_requirements(rossi.id)
    assert req["shaders"] == [EF_SKIN, EF_HAIR] and req["materials"] == ["body", "hair"]
    assert library.problems()[rossi.id] == ["필요한 모드가 없거나 꺼져 있습니다: 셰이더 EF_Skin_NPR.mmtr 외 1개 (재질이 "
                                            "쓰는 셰이더 파일이 게임에도 설치한 모드에도 없음)"]

    pack = install_tree(library, tmp_path / "Endfield_Style_Shader_Pack", shader_pack_tree())
    options = {o["name"]: o["id"] for o in pack.options}
    library.set_enabled(pack.id, True)
    library.set_options(pack.id, [options["Endfield Style Shader Driver"]])  # the script alone
    assert shader_items(library, rossi.id) == [("off", pack.name, "Endfield Style Shader Pack",
                                                "셰이더 EF_Skin_NPR.mmtr 외 1개")]
    assert library.problems()[rossi.id] == ["필요한 모드가 없거나 꺼져 있습니다: 셰이더 EF_Skin_NPR.mmtr 외 1개 "
                                            "('Endfield_Style_Shader_Pack'의 옵션 'Endfield Style Shader Pack'이(가) 꺼져 있음)"]
    library.set_options(pack.id, list(options.values()))
    assert shader_items(library, rossi.id) == [("ok", pack.name, "", "셰이더 EF_Skin_NPR.mmtr 외 1개")]
    assert library.missing_requirements(rossi.id) == [] and rossi.id not in library.problems()
    found = {f.id: f for f in bisect.facts(library)}
    assert bisect.requirement_ids(list(found.values()))[rossi.id] == [pack.id]  # (원인 찾기: on together)
    library.set_enabled(pack.id, False)
    assert shader_items(library, rossi.id) == [("off", pack.name, "", "셰이더 EF_Skin_NPR.mmtr 외 1개")]
    assert shader_items(library, pack.id) == []


def test_a_shader_pack_as_a_pak_or_in_the_game_folder_counts_too(library, game, tmp_path):
    shader_game(game)
    rossi = install_tree(library, tmp_path / "Rossi", {ROSSI + ".mdf2.45": assemble_mdf2(npr_costume(), 45)})
    run(library, unchecked_only=True)
    loose = game / "natives" / "STM" / "MaterialShader" / "Variation"  # put there by hand (not a mod)
    loose.mkdir(parents=True)
    (loose / "ef_skin_npr.mmtr.240906211").write_bytes(b"SDF\x00")
    assert set(shader_items(library, rossi.id)) == {("missing", "", "", "셰이더 EF_Hair_NPR.mmtr"),
                                                    ("ok", "게임 폴더", "", "셰이더 EF_Skin_NPR.mmtr")}
    base = tmp_path / "EF pak"
    base.mkdir()
    entry = pakmod.path_hash("natives/STM/" + EF_HAIR + ".240906211")
    pakmod.write_pak([(entry, b"SDF\x00", 4, 0)], base / "EF_shaders.pak")
    pack = install_tree(library, base, {})
    library.set_enabled(pack.id, True)
    assert set(shader_items(library, rossi.id)) == {("ok", "게임 폴더", "", "셰이더 EF_Skin_NPR.mmtr"),
                                                    ("ok", pack.name, "", "셰이더 EF_Hair_NPR.mmtr")}


def test_a_costume_with_its_own_shaders_needs_nothing(library, game, tmp_path):
    shader_game(game)
    rossi = install_tree(library, tmp_path / "Rossi", {
        ROSSI + ".mdf2.45": assemble_mdf2(npr_costume(), 45),
        "natives/STM/" + EF_SKIN + ".240906211": b"SDF\x00", "natives/STM/" + EF_HAIR + ".240906211": b"SDF\x00"})
    run(library, unchecked_only=True)
    assert library.material_results(rossi.id)[0].shaders  # (the game does not have them) ...
    assert shader_items(library, rossi.id) == []  # ... but the mod does


def test_records_of_older_rules_name_no_shaders(library, game, tmp_path):
    from mhwmm import manager

    shader_game(game)
    rossi = install_tree(library, tmp_path / "Rossi", {ROSSI + ".mdf2.45": assemble_mdf2(npr_costume(), 45)})
    run(library, unchecked_only=True)
    record = dict(library._material_record(rossi.id))
    record["format"] = manager.SHADERS_FORMAT - 1
    library._save_material_record(rossi.id, record)
    assert library.material_state(rossi.id) == "unchecked" and shader_items(library, rossi.id) == []


def test_the_command_line_names_the_shaders_and_where_they_are(library, game, tmp_path, capsys):
    from mhwmm.cli import main

    shader_game(game)
    rossi = install_tree(library, tmp_path / "Rossi", {ROSSI + ".mdf2.45": assemble_mdf2(npr_costume(), 45)})
    library.set_enabled(rossi.id, True)
    pack = install_tree(library, tmp_path / "Endfield_Style_Shader_Pack", shader_pack_tree())
    library.set_enabled(pack.id, True)  # (no option chosen yet)
    library.save()
    data = str(library.data_dir)
    assert main(["--data-dir", data, "materials", "check", rossi.name]) == 0
    out = capsys.readouterr().out
    assert "게임에 없는 셰이더(EF_Hair_NPR.mmtr, EF_Skin_NPR.mmtr)를 쓰는 재질이라 그대로 둠 - body, hair" in out
    assert ("재질이 쓰는 셰이더 EF_Skin_NPR.mmtr 외 1개: 'Endfield_Style_Shader_Pack'의 옵션 "
            "'Endfield Style Shader Pack'이(가) 꺼져 있음") in out
    assert main(["--data-dir", data, "list"]) == 0
    assert ("필요한 모드 꺼져 있음: 셰이더 EF_Skin_NPR.mmtr 외 1개 (재질이 쓰는 셰이더 파일) - "
            "'Endfield_Style_Shader_Pack'의 옵션 'Endfield Style Shader Pack'") in capsys.readouterr().out


def vanilla_costume():
    return [material("body", [("BaseDielectricMap", "Art/Dimcirui/rossi/body_ALBD.tex")], [("Color", (1.0,) * 4)])]


def test_only_the_options_chosen_need_shaders(library, game, tmp_path):
    """A costume with an NPR option and a vanilla one: the vanilla one needs no shader pack."""
    from mhwmm import bisect

    shader_game(game)
    mod = install_tree(library, tmp_path / "Rossi", {
        "Rossi NPR/" + ROSSI + ".mdf2.45": assemble_mdf2(npr_costume(), 45),
        "Rossi Vanilla/" + ROSSI + ".mdf2.45": assemble_mdf2(vanilla_costume(), 45)})
    options = {o["name"]: o["id"] for o in mod.options}
    library.set_enabled(mod.id, True)
    library.set_options(mod.id, [options["Rossi Vanilla"]])
    run(library, unchecked_only=True)
    assert shader_items(library, mod.id) == [] and mod.id not in library.problems()
    assert {f.id: f for f in bisect.facts(library)}[mod.id].shaders_needed == frozenset()
    library.set_options(mod.id, [options["Rossi NPR"]])
    assert shader_items(library, mod.id) == [("missing", "", "", "셰이더 EF_Skin_NPR.mmtr 외 1개")]


def test_the_costumes_own_shader_option_not_chosen_is_told(library, game, tmp_path):
    shader_game(game)
    mod = install_tree(library, tmp_path / "Rossi", {
        "Rossi/" + ROSSI + ".mdf2.45": assemble_mdf2(npr_costume(), 45),
        "Shaders/natives/STM/" + EF_SKIN + ".240906211": b"SDF\x00", "Shaders/natives/STM/" + EF_HAIR + ".240906211": b"SDF\x00"})
    options = {o["name"]: o["id"] for o in mod.options}
    library.set_enabled(mod.id, True)
    library.set_options(mod.id, [options["Rossi"]])
    run(library, unchecked_only=True)
    assert shader_items(library, mod.id) == [("off", mod.name, "Shaders", "셰이더 EF_Skin_NPR.mmtr 외 1개")]
    library.set_options(mod.id, list(options.values()))
    assert shader_items(library, mod.id) == []


def test_a_pack_that_is_off_with_its_option_not_chosen_says_both(library, game, tmp_path):
    from mhwmm import precheck

    shader_game(game)
    rossi = install_tree(library, tmp_path / "Rossi", {ROSSI + ".mdf2.45": assemble_mdf2(npr_costume(), 45)})
    library.set_enabled(rossi.id, True)
    pack = install_tree(library, tmp_path / "Endfield_Style_Shader_Pack", shader_pack_tree())
    library.set_enabled(pack.id, False)
    run(library, unchecked_only=True)
    (req,) = [r for r in library.requirement_status(rossi.id) if r["kind"] == "shader"]
    assert (req["state"], req["option"], req["mod_off"]) == ("off", "Endfield Style Shader Pack", True)
    assert precheck.requirement_label(req) == ("셰이더 EF_Skin_NPR.mmtr 외 1개 ('Endfield_Style_Shader_Pack'과(와) 그 옵션 "
                                               "'Endfield Style Shader Pack'이(가) 꺼져 있음)")
    library.set_options(pack.id, [o["id"] for o in pack.options])  # (the option first: the mod is still off)
    (req,) = [r for r in library.requirement_status(rossi.id) if r["kind"] == "shader"]
    assert (req["state"], req["option"], req["mod_off"]) == ("off", "", True)


def test_shaders_of_a_check_against_another_game_version_are_not_used(library, game, tmp_path):
    shader_game(game)
    rossi = install_tree(library, tmp_path / "Rossi", {ROSSI + ".mdf2.45": assemble_mdf2(npr_costume(), 45)})
    run(library, unchecked_only=True)
    assert shader_items(library, rossi.id)
    record = dict(library._material_record(rossi.id))
    record["game"] = "an older game version"
    library._save_material_record(rossi.id, record)
    assert library.material_state(rossi.id) == "unchecked" and shader_items(library, rossi.id) == []


def test_a_damaged_pak_does_not_stop_the_shader_lookup(library, game, tmp_path, monkeypatch):
    from mhwmm import manager

    shader_game(game)
    rossi = install_tree(library, tmp_path / "Rossi", {ROSSI + ".mdf2.45": assemble_mdf2(npr_costume(), 45)})
    base = tmp_path / "Broken"
    base.mkdir()
    pakmod.write_pak([(1, b"x", 1, 0)], base / "broken.pak")
    install_tree(library, base, {})
    run(library, unchecked_only=True)

    def boom(path):
        raise struct.error("unpack requires a buffer of 8 bytes")  # (a chunk table cut short)

    monkeypatch.setattr(manager, "PakFile", boom)
    assert shader_items(library, rossi.id) == [("missing", "", "", "셰이더 EF_Skin_NPR.mmtr 외 1개")]


def test_a_material_a_later_chosen_option_replaces_needs_no_shader(library, game, tmp_path):
    shader_game(game)
    mod = install_tree(library, tmp_path / "Rossi", {
        "Rossi NPR/" + ROSSI + ".mdf2.45": assemble_mdf2(npr_costume(), 45),
        "Rossi Vanilla/" + ROSSI + ".mdf2.45": assemble_mdf2(vanilla_costume(), 45)})
    library.set_enabled(mod.id, True)
    library.set_options(mod.id, [o["id"] for o in mod.options])  # (the lower option wins the same file)
    run(library, unchecked_only=True)
    assert [k for _p, k in library.mod_entries(mod.id).values()] == [f"{mod.options[1]['id']}/{ROSSI}.mdf2.45"]
    assert shader_items(library, mod.id) == [] and mod.id not in library.problems()
