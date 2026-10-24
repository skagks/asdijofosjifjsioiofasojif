"""Material repair against a small fake game: encrypted (and chunked) paks, an
outdated costume mod as loose files and as a mod pak."""

import os
import struct
from pathlib import Path

import pytest

from mhwmm import matfix

pytestmark = pytest.mark.skipif(not matfix.available(), reason="zstd 모듈이 없습니다")

from mhwmm.matfix import pak as pakmod  # noqa: E402
from mhwmm.matfix.game import GameIndex  # noqa: E402
from mhwmm.matfix.mdf2 import Mdf2File, assemble_mdf2, extract_material  # noqa: E402
from mhwmm.matfix.repair import NULL_MASK, check_mod  # noqa: E402
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


def test_game_update_and_reinstall_need_a_new_check(library, game, tmp_path):
    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    run(library, [mod.id], repair=True)
    assert library.material_state(mod.id) == "repaired"

    patch = game / "re_chunk_000.pak.patch_001.pak"
    os.utime(patch, (patch.stat().st_atime, patch.stat().st_mtime + 100))  # a game update
    assert [j.mod_id for j in library.material_jobs(unchecked_only=True)] == [mod.id]
    assert library.material_state(mod.id) == "unchecked"
    assert library.mod_entries(mod.id)[BODY + ".mdf2.45"][1].startswith("matfix/")  # kept until checked again
    run(library, unchecked_only=True)
    assert library.material_state(mod.id) == "stale"  # repaired for the previous game version

    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(current_body(), 45)})
    assert library.material_state(mod.id) == "unchecked" and not library.repaired_materials(mod.id)


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

    mod = install_tree(library, tmp_path / "Lepus", {AVP: old_avp()})
    jobs = library.material_jobs([mod.id])
    found = library.run_materials(jobs, repair=True)
    with caplog.at_level(logging.INFO):
        gui.App._log_materials(None, found, jobs)
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
