"""다른 장비로 옮기기 (mhwmm/equip/retarget.py) on synthetic files only: the renamer's rules, which files
of a loose armor mod move, stay or are left out, materials and prefabs rewritten in place, a weapon mod,
a .pak mod re-keyed, partial parts, and what the target's own prefabs in a fake game say."""

from __future__ import annotations

import struct
import zlib

import pytest

import equip_game_fixtures as F
from conftest import write
from mhwmm.equip import retarget as R
from mhwmm.equip.model import Target
from mhwmm.equip.pathlist import PathIndex
from mhwmm.equip.retarget import Renamer, RetargetError, Slot, why_not
from mhwmm.matfix import rsz
from mhwmm.matfix.mdf2 import Mdf2File, assemble_mdf2
from mhwmm.matfix.pak import COMPRESSION_DEFLATE, COMPRESSION_NONE, PakFile, compress_as, path_hash, write_pak

N = "natives/STM/"
CHAR = "Art/Model/Character/"
SHADER = "MaterialShader/Variation/Base_Equip.mmtr"
A, B = Slot("armor", "034/001"), Slot("armor", "087/000")
STAFF, STAFF_B = Slot("weapon", "09/000021"), Slot("weapon", "09/000029")
SWORD, SWORD_B = Slot("weapon", "03/000009"), Slot("weapon", "03/000029")
TEX = b"TEX\x00" + bytes(60)
MESH = b"MESH" + bytes(60)

# a female body mod of 034/001 (design 1)
BODY = CHAR + "ch03/034/001/2/ch03_034_0012"
BODY_B = CHAR + "ch03/087/000/2/ch03_087_0002"
ALBD = CHAR + "ch03/034/001/2/textures/ch03_034_0012_ALBD.tex"  # shipped
NRRO = CHAR + "ch03/034/001/2/textures/ch03_034_0012_NRRO.tex"  # the game's, not shipped
CUSTOM = "Art/Arsinia/Bunny/body_ATOS.tex"  # the author's own folder
GPBF = CHAR + "ch03/034/001/2/textures/materialblend/ch03_034_0012_mb.gpbf"
CHAIN = CHAR + "ch02/034/001/2/ch02_034_0012.chain2"  # the female prefab's physics live under ch02
CLSP = CHAR + "ch03/034/001/2/ch03_034_0012.clsp"  # the game's, not shipped
PFB = N + "GameDesign/Equip/_Prefab/Armor/Female/034/001/Body/ch03_034_0012.pfb.18"
PFB_B = N + "GameDesign/Equip/_Prefab/Armor/Female/087/000/Body/ch03_087_0002.pfb.18"
AVP = N + "GameDesign/Equip/_Prefab/Armor/Female/034/001/034_001_avp.user.3"


def material(name="body", textures=(), gpbf=()):
    return {"name": name, "mmtr_path": SHADER, "shading_type": 0, "alpha_flags_raw": b"\x98\x00\x00\x88",
            "isV2_unk_0x24": 0, "isV2_unk_0x2C": 0, "isV2_unk_0x30": 0, "gpbf_split": (len(gpbf), len(gpbf)),
            # name / path pairs, as the game's files keep them
            "gpbf_entries": [e for slot, path in gpbf for e in ((slot, 1, 2), (path, 0, 1))],
            "textures": [{"type": f"Map{i}", "path": p, "ukn": 0} for i, p in enumerate(textures)],
            "props": [{"name": "Color", "values": [1.0, 1.0, 1.0, 1.0], "count_flags": 0}], "header_version": 1}


def mdf2(*materials) -> bytes:
    return assemble_mdf2(list(materials), 45)


def texture_paths(data: bytes) -> list[str]:
    mdf = Mdf2File(data, 45)
    return [p.text for m in mdf.materials for _t, p in m.textures] + [g.text for m in mdf.materials for g in m.gpbf]


def _align(n: int) -> int:
    return n + (-n) % 16


def prefab(objects: list[dict], resources=()) -> bytes:
    """A prefab laid out like the game's: header, resource table, and an RSZ block holding ``objects``
    (GameObject names and Resource fields as inline strings, written with the real type registry)."""
    reg = F.registry()
    w = F._Writer(reg)
    roots = [w.add(obj) for obj in objects]
    strings = b"".join(r.encode("utf-16-le") + b"\0\0" for r in resources)
    table_off = 56
    start = table_off + 8 * len(resources)
    rsz_off = _align(start + len(strings))
    header = struct.pack("<4s5I3QQ", b"PFB\0", len(roots), len(resources), 0, 0, 0, 0, table_off, 0, rsz_off)
    offsets, pos = b"", start
    for r in resources:
        offsets += struct.pack("<Q", pos)
        pos += (len(r) + 1) * 2
    inst_off = 48 + 4 * len(roots)
    ud_off = _align(rsz_off + inst_off + 8 * len(w.insts)) - rsz_off
    block = bytearray(struct.pack("<4sIIIII", b"RSZ\0", 16, len(roots), len(w.insts), 0, 0)) + bytes(24)
    block += struct.pack(f"<{len(roots)}i", *roots)
    for tid, crc, _f, _v in w.insts:
        block += struct.pack("<II", tid, crc)
    block = block.ljust(ud_off, b"\0")
    data_off = _align(rsz_off + len(block)) - rsz_off
    block = block.ljust(data_off, b"\0")
    struct.pack_into("<QQQ", block, 24, inst_off, data_off, ud_off)
    data = bytearray()
    for _tid, _crc, fields, values in w.insts:
        if fields is not None:
            rsz.write_values(data, rsz_off + data_off, fields, values, rsz.Schema(), reg)
    return (header + offsets + strings).ljust(rsz_off, b"\0") + bytes(block) + bytes(data)


def body_prefab(stem=BODY, chain=CHAIN, extra=()) -> bytes:
    name = stem.rpartition("/")[2]
    mesh, mat = "@" + stem + ".mesh", stem + ".mdf2"
    return prefab([{F.TYPE: "via.GameObject", "Name": name, "Tag": "", "TimeScale": -1.0},
                   {F.TYPE: "via.render.Mesh", "Enabled": True, "Mesh": mesh[1:], "Material": mat},
                   {F.TYPE: "via.motion.Chain2", "ChainAsset": chain}],
                  resources=(mesh, mat, chain, CLSP, *extra))


def rsz_values(data: bytes) -> dict[str, list]:
    """Field name -> values (decoded strings) of every instance, read back exactly with the current layout."""
    walked = rsz.walk_current(rsz.parse(data), rsz.Schema(), F.registry())
    assert walked is not None, "does not read exactly"
    out: dict[str, list] = {}
    for inst in walked[0]:
        for f, v in zip(inst.fields or (), inst.values or ()):
            if f[1] in ("String", "Resource") and not f[4]:
                out.setdefault(f[0], []).append(bytes(v[4:]).decode("utf-16-le").rstrip("\0"))
    return out


def stage(tmp_path, files: dict) -> dict:
    """{game rel: stored file} of a loose mod (stored under other names, as installed mods are)."""
    return {rel: write(tmp_path / "store" / f"{i}.bin", data) for i, (rel, data) in enumerate(files.items())}


def loose_mod(tmp_path, files: dict) -> list[R.Item]:
    return R.items_of(stage(tmp_path, files), None)


def armor_mod() -> dict:
    return {
        BODY + ".mesh.241111606": MESH,
        N + "streaming/" + BODY + ".mesh.241111606": MESH + b"high",
        N + BODY + ".mdf2.45": mdf2(material("body", (ALBD, NRRO, CUSTOM), gpbf=(("MultiBlend_BAB", GPBF),))),
        N + ALBD + ".241106027": TEX,
        N + "streaming/" + ALBD + ".241106027": TEX + b"mips",
        N + CUSTOM + ".241106027": TEX + b"custom",
        N + GPBF + ".1": b"GPBF",
        N + CHAIN + ".14": b"CHAIN",
        PFB: body_prefab(),
        AVP: b"USR\x00" + bytes(60),
        N + CHAR + "ch03/022/000/3/ch03_022_0003.mesh.241111606": MESH,  # another armor
        "reframework/autorun/bunny.lua": b"-- lua",
    }


def fix_paths(files: dict) -> dict:
    return {(k if k.startswith(("natives/", "reframework/")) else N + k): v for k, v in files.items()}


# ---- the renamer ----------------------------------------------------------------------------------
def test_slots_of_targets_and_why_not():
    assert Slot.of(Target("armor", "f/034/001", "body")) == A
    assert Slot.of(Target("armor", "m/034/001")) == A  # both bodies move together
    assert Slot.of(Target("weapon", "03/000009", "sub")) == SWORD
    assert Slot.of(Target("armor", "f/002/000")) is None  # innerwear
    assert Slot.of(Target("hunter", "inner/f/000")) is None
    assert Slot.of(Target("palico", "062", "helm")) is None
    assert not Slot("weapon", "10/030005").valid and not Slot("weapon", "03/990001").valid  # kinsect, test folder
    assert Slot("weapon", "10/000005").valid and Slot("weapon", "03/100004").valid
    assert why_not(A, B) is None and why_not(SWORD, SWORD_B) is None
    assert "같은 장비" in why_not(A, A)
    assert "방어구는 방어구로" in why_not(A, SWORD)
    assert "같은 종류(태도)" in why_not(SWORD, STAFF_B)
    assert "방어구와 무기만" in why_not(Slot("palico", "062"), Slot("palico", "063"))
    assert why_not(A, Slot("armor", "34/1")) is not None
    with pytest.raises(RetargetError):
        Renamer(SWORD, STAFF_B)


@pytest.mark.parametrize("old, new", [
    # model folders and file names, streaming/ copies, textures
    (N + BODY + ".mesh.241111606", N + BODY_B + ".mesh.241111606"),
    (N + "streaming/" + ALBD + ".241106027",
     N + "streaming/" + CHAR + "ch03/087/000/2/textures/ch03_087_0002_ALBD.tex.241106027"),
    (CHAR + "ch02/034/001/1/ch02_034_0011_sc.jcns", CHAR + "ch02/087/000/1/ch02_087_0001_sc.jcns"),
    # a private name in A's folder: the folder moves
    (CHAR + "ch03/034/001/2/textures/bunny_ALBD.tex", CHAR + "ch03/087/000/2/textures/bunny_ALBD.tex"),
    # prefab folders (any case), slinger ropes of both spellings
    (PFB, PFB_B),
    (N + "GameDesign/Equip/_Prefab/Armor/Male/034/001/Slinger/ch02_034_0016.pfb.18",
     N + "GameDesign/Equip/_Prefab/Armor/Male/087/000/Slinger/ch02_087_0006.pfb.18"),
    ("natives/stm/gamedesign/equip/_prefab/armor/male/034/001/slingerrope_034_001.pfb.18",
     "natives/stm/gamedesign/equip/_prefab/armor/male/087/000/slingerrope_087_000.pfb.18"),
    (N + "GameDesign/Equip/_Prefab/Armor/Male/034/001/SlingerRope_034.pfb.18",
     N + "GameDesign/Equip/_Prefab/Armor/Male/087/000/SlingerRope_087.pfb.18"),
    # case is kept, only digits change; mh03 custom slots; resource strings with @
    ("NATIVES/STM/ART/MODEL/CHARACTER/CH03/034/001/2/CH03_034_0012.MESH",
     "NATIVES/STM/ART/MODEL/CHARACTER/CH03/087/000/2/CH03_087_0002.MESH"),
    ("@Art/Model/Character/mh03/034/001/2/mh03_034_0012.mesh", "@Art/Model/Character/mh03/087/000/2/mh03_087_0002.mesh"),
    # helm hair settings: the helm's id changes, the hair's does not
    (CHAR + "ch02/034/001/3/hair/ch02_034_0013__ch01_000_0001__m_hairadjustparam.user",
     CHAR + "ch02/087/000/3/hair/ch02_087_0003__ch01_000_0001__m_hairadjustparam.user"),
    (CHAR + "ch03/034/001/3/Hair/ch03_034_0013_HairAdjustList.user.3",
     CHAR + "ch03/087/000/3/Hair/ch03_087_0003_HairAdjustList.user.3"),
    # bare names (a prefab's GameObject name)
    ("ch03_034_0012", "ch03_087_0002"),
    ("ch02_034_0015_rs", "ch02_087_0005_rs"),
])
def test_armor_renames_keep_the_length(old, new):
    assert Renamer(A, B).rename(old) == new
    assert len(new) == len(old)


@pytest.mark.parametrize("path", [
    AVP,  # never: hide / merge settings that must match the target's own
    "034_001_avp",
    CHAR + "ch03/034/000/2/ch03_034_0002.mesh",  # the other design of the same model
    CHAR + "ch03/134/001/2/ch03_134_0012.mesh",
    "xch03_034_0012", "ch03_034_00123", "ch01_034_0012",
    N + "GameDesign/Equip/_Prefab/Armor/Female/034/001/ArmorVisual.user.3",  # no part
])
def test_armor_paths_that_are_not_renamed(path):
    assert Renamer(A, B).rename(path) is None
    assert Renamer(A, B).rename(path, all_parts=True) is None


def test_parts_filter():
    helm = Renamer(A, B, ["helm"])
    assert helm.parts == ("helm",)
    assert helm.rename(N + BODY + ".mesh.241111606") is None
    assert helm.rename(N + BODY + ".mesh.241111606", all_parts=True) == N + BODY_B + ".mesh.241111606"
    assert helm.part_of(N + BODY + ".mesh.241111606") == "body"
    assert helm.rename("ch03_034_0013") == "ch03_087_0003"
    assert helm.part_of(PFB.replace("Body", "Helm").replace("0012", "0013")) == "helm"
    assert helm.part_of(N + "GameDesign/Equip/_Prefab/Armor/Male/034/001/slingerrope_034_001.pfb.18") == "slinger"
    assert helm.part_of(CHAR + "ch03/022/000/3/ch03_022_0003.mesh") is None
    assert Renamer(A, B).parts == ("helm", "body", "arm", "waist", "leg", "slinger")
    with pytest.raises(RetargetError, match="알 수 없는 부위"):
        Renamer(A, B, ["head"])


@pytest.mark.parametrize("old, new", [
    (N + "Art/Model/Item/it03/00/0009/it0300_0009_0.mesh.241111606",
     N + "Art/Model/Item/it03/00/0029/it0300_0029_0.mesh.241111606"),
    (N + "streaming/Art/Model/Item/it03/00/0009/textures/it0300_0009_1_ALBD.tex.241106027",
     N + "streaming/Art/Model/Item/it03/00/0029/textures/it0300_0029_1_ALBD.tex.241106027"),
    (N + "GameDesign/Equip/_Prefab/Weapon/wp03/00/0009/it0300_0009_1.pfb.18",
     N + "GameDesign/Equip/_Prefab/Weapon/wp03/00/0029/it0300_0029_1.pfb.18"),
    (N + "Sound/Wwise/it0300_0009_m.sbnk.1.x64", N + "Sound/Wwise/it0300_0029_m.sbnk.1.x64"),
    ("Art/Model/Item/it03/00/0009/textures/runestaff_ALBD.tex", "Art/Model/Item/it03/00/0029/textures/runestaff_ALBD.tex"),
    ("it0300_0009_0", "it0300_0029_0"),
])
def test_weapon_renames(old, new):
    renamer = Renamer(SWORD, SWORD_B)
    assert renamer.rename(old) == new and len(new) == len(old)
    assert renamer.parts == ("main", "sub")


def test_weapon_parts_bands_and_never_moved():
    renamer = Renamer(SWORD, SWORD_B, ["helm"])  # a weapon always moves both models
    assert renamer.part_of("it0300_0009_0_ALBD.tex") == "main"
    assert renamer.part_of(N + "Art/Model/Item/it03/00/0009/it0300_0009_1.mdf2.45") == "sub"
    assert renamer.part_of("it0300_0009_m.sbnk") == ""
    assert renamer.rename(N + "GameDesign/Equip/_Prefab/Weapon/wp03/00/0009/it0300_0009_0_wvp.user.3") is None
    assert renamer.rename("it0300_0009") is None  # not followed by "_"
    assert renamer.rename(N + "Art/Model/Item/it01/00/0009/it0100_0009_0.mesh") is None
    assert renamer.rename(N + "Art/Model/Item/it03/00/0019/it0300_0019_0.mesh") is None
    artian = Renamer(SWORD, Slot("weapon", "03/100004"))
    assert artian.rename(N + "Art/Model/Item/it03/00/0009/it0300_0009_0.mesh.241111606") == \
        N + "Art/Model/Item/it03/10/0004/it0310_0004_0.mesh.241111606"


# ---- a loose armor mod ----------------------------------------------------------------------------------
def test_a_loose_armor_mod_is_planned_and_built(tmp_path):
    items = loose_mod(tmp_path, fix_paths(armor_mod()))
    plan = R.plan(items, A, B)
    assert plan.ok, plan.errors
    moves = {m.old: (m.new, m.how) for m in plan.moves}
    assert moves == {
        N + BODY + ".mesh.241111606": (N + BODY_B + ".mesh.241111606", "copy"),
        N + "streaming/" + BODY + ".mesh.241111606": (N + "streaming/" + BODY_B + ".mesh.241111606", "copy"),
        N + BODY + ".mdf2.45": (N + BODY_B + ".mdf2.45", "mdf2"),
        N + ALBD + ".241106027": (N + CHAR + "ch03/087/000/2/textures/ch03_087_0002_ALBD.tex.241106027", "copy"),
        N + "streaming/" + ALBD + ".241106027":
            (N + "streaming/" + CHAR + "ch03/087/000/2/textures/ch03_087_0002_ALBD.tex.241106027", "copy"),
        N + GPBF + ".1": (N + CHAR + "ch03/087/000/2/textures/materialblend/ch03_087_0002_mb.gpbf.1", "copy"),
        N + CHAIN + ".14": (N + CHAR + "ch02/087/000/2/ch02_087_0002.chain2.14", "copy"),
        PFB: (PFB_B, "rsz"),
    }
    assert all(m.pak is None for m in plan.moves)
    assert plan.kept == [N + CUSTOM + ".241106027"]
    dropped = dict(plan.dropped)
    assert set(dropped) == {AVP, N + CHAR + "ch03/022/000/3/ch03_022_0003.mesh.241111606", "reframework/autorun/bunny.lua"}
    assert "외형 설정" in dropped[AVP] and "다른 장비" in dropped[N + CHAR + "ch03/022/000/3/ch03_022_0003.mesh.241111606"]
    assert plan.vanilla_refs == 2  # the game's NRRO texture and clsp, left as they are
    notes = " / ".join(plan.notes)
    assert "게임 텍스처 등 2개를 그대로" in notes and "_avp.user" in notes and "스크립트" in notes
    assert "확인하지 못했습니다" in notes  # no game to check the target against
    assert plan.parts == ("helm", "body", "arm", "waist", "leg", "slinger")

    built = R.build(plan, items, tmp_path / "out")
    assert sorted(built.files) == sorted([new for new, _how in moves.values()] + [N + CUSTOM + ".241106027"])
    assert sorted(built.moves) == sorted((new, old) for old, (new, _h) in moves.items()) and built.pak_moves == []
    out = tmp_path / "out"
    assert not (out / "reframework").exists() and not list(out.rglob("*avp*"))
    assert (out / (N + BODY_B + ".mesh.241111606")).read_bytes() == MESH
    assert (out / N / "streaming" / (BODY_B + ".mesh.241111606")).read_bytes() == MESH + b"high"
    assert (out / (N + CUSTOM + ".241106027")).read_bytes() == TEX + b"custom"

    # the material: only references to moved files changed, in place
    old = armor_mod()[N + BODY + ".mdf2.45"]
    new = (out / (N + BODY_B + ".mdf2.45")).read_bytes()
    assert len(new) == len(old)
    assert texture_paths(new) == [CHAR + "ch03/087/000/2/textures/ch03_087_0002_ALBD.tex", NRRO, CUSTOM,
                                  "MultiBlend_BAB", CHAR + "ch03/087/000/2/textures/materialblend/ch03_087_0002_mb.gpbf"]
    mat = Mdf2File(new, 45).materials[0]
    assert (mat.name.text, mat.mmtr_path) == ("body", SHADER)
    differ = [i for i in range(len(old)) if old[i] != new[i]]
    paths = Mdf2File(old, 45).materials[0]
    spans = [(r.str_pos, r.str_pos + 2 * len(r.text)) for r in (paths.textures[0][1], paths.gpbf[1])]
    assert differ and all(any(a <= i < b for a, b in spans) for i in differ)

    # the prefab: the moved files' paths and the GameObject name, still read exactly
    pfb = (out / PFB_B).read_bytes()
    assert len(pfb) == len(armor_mod()[PFB])
    assert rsz.parse(pfb).resources() == {"@" + BODY_B + ".mesh", BODY_B + ".mdf2",
                                         CHAR + "ch02/087/000/2/ch02_087_0002.chain2", CLSP}
    values = rsz_values(pfb)
    assert values["Name"] == ["ch03_087_0002"]
    assert values["Mesh"] == [BODY_B + ".mesh"] and values["Material"] == [BODY_B + ".mdf2"]
    assert values["ChainAsset"] == [CHAR + "ch02/087/000/2/ch02_087_0002.chain2"]


def test_mod_entries_pairs_and_a_kept_material_pointing_at_moved_files(tmp_path):
    """A material in the author's own folder stays where it is, but what it points at in A's folder moves:
    its references follow."""
    own = "Art/Arsinia/Bunny/body.mdf2"
    files = fix_paths({
        BODY + ".mesh.241111606": MESH,
        own + ".45": mdf2(material("body", (ALBD, CUSTOM))),
        ALBD + ".241106027": TEX,
        PFB: prefab([{F.TYPE: "via.GameObject", "Name": "ch03_034_0012"}],
                    resources=(BODY + ".mesh", own)),
    })
    stored = {rel: (path, "key") for rel, path in stage(tmp_path, files).items()}  # ModManager.mod_entries
    items = R.items_of(stored, None)
    plan = R.plan(items, A, B)
    assert plan.ok and plan.kept == [N + own + ".45"]
    assert {m.old: m.how for m in plan.moves}[PFB] == "rsz"  # the GameObject name
    R.build(plan, items, tmp_path / "out")
    kept = (tmp_path / "out" / (N + own + ".45")).read_bytes()
    assert texture_paths(kept) == [CHAR + "ch03/087/000/2/textures/ch03_087_0002_ALBD.tex", CUSTOM]
    assert rsz.parse((tmp_path / "out" / PFB_B).read_bytes()).resources() == {BODY_B + ".mesh", own}


def test_some_parts_with_a_reference_into_another_part(tmp_path):
    helm = CHAR + "ch03/034/001/3/ch03_034_0013"
    files = fix_paths({
        helm + ".mesh.241111606": MESH,
        helm + ".mdf2.45": mdf2(material("helm", (CHAR + "ch03/034/001/3/textures/ch03_034_0013_ALBD.tex", ALBD))),
        CHAR + "ch03/034/001/3/textures/ch03_034_0013_ALBD.tex.241106027": TEX,
        ALBD + ".241106027": TEX + b"body",  # the body's texture, also used by the helm
        BODY + ".mesh.241111606": MESH,
        BODY + ".mdf2.45": mdf2(material("body", (ALBD,))),
    })
    items = loose_mod(tmp_path, files)
    plan = R.plan(items, A, B, parts=["helm"])
    assert plan.ok and plan.parts == ("helm",)
    assert sorted(m.new.rpartition("/")[2] for m in plan.moves) == [
        "ch03_087_0002_ALBD.tex.241106027", "ch03_087_0003.mdf2.45", "ch03_087_0003.mesh.241111606",
        "ch03_087_0003_ALBD.tex.241106027"]
    assert {m.how for m in plan.moves if m.new.endswith(".mdf2.45")} == {"mdf2"}
    assert dict(plan.dropped) == {N + BODY + ".mesh.241111606": "고르지 않은 부위입니다 (몸통)",
                                  N + BODY + ".mdf2.45": "고르지 않은 부위입니다 (몸통)"}
    assert any("고르지 않은 몸통 파일 1개도" in n for n in plan.notes)
    assert not any(n.startswith("몸통:") for n in plan.notes)  # the pulled-in part gets no "no model" note
    with pytest.raises(RetargetError):
        R.build(R.plan(items, A, B, parts=[]), items, tmp_path / "none")
    assert "하나 이상" in R.plan(items, A, B, parts=[]).errors[0]
    assert "알 수 없는 부위" in R.plan(items, A, B, parts=["head"]).errors[0]
    assert R.plan(items, A, B, parts=["leg"]).errors == ["이 모드에는 옮길 원래 장비의 파일이 없습니다"]


def test_a_part_without_a_model_is_noted(tmp_path):
    hair = CHAR + "ch03/034/001/3/Hair/ch03_034_0013_HairAdjustList.user.3"  # only the helm's hair settings
    items = loose_mod(tmp_path, fix_paths({hair: prefab([{F.TYPE: "via.GameObject", "Name": "hair"}])}))
    plan = R.plan(items, A, B)
    assert plan.ok and any(n.startswith("머리: 모드에 모델(.mesh)이 없어") for n in plan.notes)
    assert not plan.fills


def test_moving_onto_files_the_mod_already_has_is_refused(tmp_path):
    files = fix_paths({BODY + ".mesh.241111606": MESH, BODY + ".mdf2.45": mdf2(material()),
                       BODY_B + ".mesh.241111606": MESH})
    plan = R.plan(loose_mod(tmp_path, files), A, B)
    assert not plan.ok and "이미 있어" in plan.errors[0] and BODY_B in plan.errors[0]


def test_broken_and_unreadable_files(tmp_path):
    files = fix_paths({BODY + ".mesh.241111606": MESH, BODY + ".mdf2.45": b"MDF\x00broken"})
    plan = R.plan(loose_mod(tmp_path, files), A, B)
    assert not plan.ok and "파일을 읽지 못했습니다" in plan.errors[0]
    assert R.why_not(A, B) is None
    plan = R.plan([], A, A)
    assert plan.errors == ["원래 장비와 같은 장비입니다"] and not plan.moves


def test_the_built_files_stay_in_the_new_folder(tmp_path):
    with pytest.raises(RetargetError):
        R._out_path(tmp_path, "natives/../../evil.dll")
    with pytest.raises(RetargetError):
        R._out_path(tmp_path, "C:/Windows/evil.dll")
    assert R._out_path(tmp_path, N + "Art/x.tex") == tmp_path / "natives" / "STM" / "Art" / "x.tex"


# ---- rewriting -------------------------------------------------------------------------------------
def test_mover_and_rewrite():
    renamer = Renamer(A, B)
    moved = R.mover([(N + BODY_B + ".mesh.241111606", N + BODY + ".mesh.241111606")], renamer)
    assert moved("@" + BODY + ".mesh") == "@" + BODY_B + ".mesh"
    assert moved(BODY.lower() + ".mesh") == BODY_B.lower() + ".mesh"  # the reference's own spelling
    assert moved(BODY + ".mdf2") is None  # not moved: left as it is
    assert R.rewrite(MESH, "x.mesh.241111606", moved, renamer) == (MESH, 0)  # no paths inside
    data = body_prefab()
    new, n = R.rewrite(data, PFB, moved, renamer)
    assert n == 3  # the resource string, the Mesh field and the GameObject name
    values = rsz_values(new)
    assert values["Name"] == ["ch03_087_0002"] and values["Material"] == [BODY + ".mdf2"]
    kept, n = R.rewrite(data, PFB, moved, None)
    assert n == 2 and rsz_values(kept)["Name"] == ["ch03_034_0012"]  # bare names only with a renamer
    with pytest.raises(RetargetError):
        R.rewrite(b"PFB\x00" + bytes(12), PFB, moved, renamer)


# ---- a weapon mod --------------------------------------------------------------------------------------
ITEM = "Art/Model/Item/it03/00/0009/it0300_0009"
ITEM_B = "Art/Model/Item/it03/00/0029/it0300_0029"
WPFB_B = N + "GameDesign/Equip/_Prefab/Weapon/wp03/00/0029/it0300_0029_{}.pfb.18"


def sword_mod() -> dict:
    return fix_paths({
        ITEM + "_0.mesh.241111606": MESH,
        "streaming/" + ITEM + "_0.mesh.241111606": MESH,
        ITEM + "_0.mdf2.45": mdf2(material("lambert2", ("Art/Model/Item/it03/00/0009/textures/it0300_0009_0_ALBD.tex",))),
        ITEM + "_1.mesh.241111606": MESH,
        ITEM + "_1.mdf2.45": mdf2(material("blade", ("Art/Model/Item/it03/00/0009/textures/blade_ALBD.tex",))),
        "Art/Model/Item/it03/00/0009/textures/blade_ALBD.tex.241106027": TEX,
        "Sound/Wwise/it0300_0009_m.sbnk.1.x64": b"BKHD",
        "GameDesign/Equip/_Prefab/Weapon/wp03/00/0009/it0300_0009_0_wvp.user.3": b"USR\x00",
    })


def sword_game(**prefabs) -> F.FakeGame:
    files = {WPFB_B.format(k): F.pfb_file(f"{ITEM_B}_{k}.mesh", f"{ITEM_B}_{k}.mdf2") for k in (0, 1)}
    files.update({WPFB_B.format(k[1:]): v for k, v in prefabs.items()})
    return F.FakeGame({k: v for k, v in files.items() if v is not None})


def test_a_weapon_mod_with_main_and_sub(tmp_path):
    items = loose_mod(tmp_path, sword_mod())
    plan = R.plan(items, SWORD, SWORD_B, game=sword_game())
    assert plan.ok, plan.errors
    assert sorted(m.new for m in plan.moves) == sorted(fix_paths({
        ITEM_B + "_0.mesh.241111606": 0, "streaming/" + ITEM_B + "_0.mesh.241111606": 0, ITEM_B + "_0.mdf2.45": 0,
        ITEM_B + "_1.mesh.241111606": 0, ITEM_B + "_1.mdf2.45": 0,
        "Art/Model/Item/it03/00/0029/textures/blade_ALBD.tex.241106027": 0}))
    assert {m.new.rpartition("/")[2]: m.how for m in plan.moves if ".mdf2" in m.new} == \
        {"it0300_0029_0.mdf2.45": "copy", "it0300_0029_1.mdf2.45": "mdf2"}
    assert plan.vanilla_refs == 1  # the game's own main texture
    assert any("_wvp.user" in n for n in plan.notes) and not any("확인하지 못했습니다" in n for n in plan.notes)
    # the model's sound bank stays behind: its ids are the original weapon's
    assert dict(plan.dropped)[N + "Sound/Wwise/it0300_0009_m.sbnk.1.x64"] == "소리 파일은 옮기지 않습니다"
    assert "소리 파일은 옮기지 않습니다 (게임이 다른 무기의 소리로 읽지 않습니다)" in plan.notes
    assert not any("아티아" in n or "겹칠 때" in n for n in plan.notes)  # (armor and artian notes)
    R.build(plan, items, tmp_path / "out")
    sub = (tmp_path / "out" / (N + ITEM_B + "_1.mdf2.45")).read_bytes()
    assert texture_paths(sub) == ["Art/Model/Item/it03/00/0029/textures/blade_ALBD.tex"]
    main = (tmp_path / "out" / (N + ITEM_B + "_0.mdf2.45")).read_bytes()
    assert texture_paths(main) == ["Art/Model/Item/it03/00/0009/textures/it0300_0009_0_ALBD.tex"]


def test_what_the_target_weapons_prefabs_say(tmp_path):
    items = loose_mod(tmp_path, sword_mod())
    no_sub = R.plan(items, SWORD, SWORD_B, game=sword_game(_1=None))
    assert no_sub.ok and "보조 모델이 없어" in " ".join(no_sub.notes)
    shared = sword_game(_0=F.pfb_file("Art/Model/Item/it03/10/0001/it0310_0001_0.mesh", f"{ITEM_B}_0.mdf2"))
    plan = R.plan(items, SWORD, SWORD_B, game=shared)
    assert plan.errors == ["이 무기는 다른 무기의 모델을 함께 써서 옮긴 모델이 보이지 않습니다 (본체)"]
    assert R.plan(items, SWORD, SWORD_B, game=sword_game(_0=None)).errors == ["대상 장비에는 본체가 없습니다"]
    assert plan.bad_parts == set()  # a weapon moves both of its models: nothing to untick


# ---- what the target armor's prefabs say ----------------------------------------------------------------
def armor_game(prefab_data=None, mesh=True) -> F.FakeGame:
    files = {}
    if prefab_data is not None:
        files[PFB_B] = prefab_data
    return F.FakeGame(files, present=[N + BODY_B + ".mesh.241111606"] if mesh else ())


def without_prefab() -> dict:
    files = fix_paths(armor_mod())
    del files[PFB]
    return files


def test_the_target_part_must_exist(tmp_path):
    items = loose_mod(tmp_path, without_prefab())
    plan = R.plan(items, A, B, game=armor_game())
    assert plan.errors == ["대상 장비에는 여성 몸통이 없습니다"] and plan.bad_parts == {"body"}
    loads = armor_game(F.pfb_file(BODY_B + ".mesh", BODY_B + ".mdf2", CHAR + "ch02/087/000/2/ch02_087_0002.chain2"))
    assert R.plan(items, A, B, game=loads).ok
    # the mod's own prefab moves with it and takes the place of the target's, whatever that one loads ...
    other = armor_game(F.pfb_file(CHAR + "ch03/099/000/2/ch03_099_0002.mesh"))
    own = R.plan(loose_mod(tmp_path / "own", fix_paths(armor_mod())), A, B, game=other)
    assert own.ok, own.errors
    # ... but the game must ask for it: a target without that prefab does not (even with a model there)
    own = R.plan(loose_mod(tmp_path / "own2", fix_paths(armor_mod())), A, B, game=armor_game())
    assert own.errors == ["대상 장비에는 여성 몸통이 없습니다"] and own.bad_parts == {"body"}


def test_a_target_prefab_that_loads_another_model(tmp_path):
    items = loose_mod(tmp_path, without_prefab())
    other = armor_game(F.pfb_file(CHAR + "ch03/099/000/2/ch03_099_0002.mesh", BODY_B + ".mdf2"))
    plan = R.plan(items, A, B, game=other)
    assert plan.errors == ["대상 장비의 여성 몸통은 다른 장비의 모델을 불러와서, 옮긴 모델이 보이지 않습니다"]
    assert plan.bad_parts == {"body"}
    # the other body's model of the same design counts when the mod moves that one too
    files = without_prefab()
    files[N + CHAR + "ch02/034/001/2/ch02_034_0012.mesh.241111606"] = MESH
    male = armor_game(F.pfb_file(CHAR + "ch02/087/000/2/ch02_087_0002.mesh"))
    male.add(N + "GameDesign/Equip/_Prefab/Armor/Male/087/000/Body/ch02_087_0002.pfb.18",
             F.pfb_file(CHAR + "ch02/087/000/2/ch02_087_0002.mesh"))
    male.add(N + CHAR + "ch02/034/001/2/ch02_034_0012.mdf2.45", mdf2(material("ch02_034_0012_body")))
    both = R.plan(loose_mod(tmp_path / "both", files), A, B, game=male)
    assert both.ok, both.errors
    assert [m.new for m in both.moves if m.how == "fill"] == [N + CHAR + "ch02/087/000/2/ch02_087_0002.mdf2.45"]


def test_physics_notes(tmp_path):
    items = loose_mod(tmp_path, without_prefab())
    cloth = armor_game(F.pfb_file("@" + BODY_B + ".mesh", BODY_B + ".mdf2", BODY_B + ".gpuc"))
    plan = R.plan(items, A, B, game=cloth)
    assert plan.ok
    notes = " / ".join(plan.notes)
    assert "흔들림이 빠질 수 있습니다" in notes  # the mod's chain2 is not loaded by the target
    assert "GPU 옷감(.gpuc)" in notes
    assert "여성 몸통은" in notes


def test_the_female_slinger_prefab_named_after_the_male_body(tmp_path):
    sling = CHAR + "ch03/034/001/6/ch03_034_0016"
    items = loose_mod(tmp_path, fix_paths({sling + ".mesh.241111606": MESH, sling + ".mdf2.45": mdf2(material("s"))}))
    game = F.FakeGame({N + "GameDesign/Equip/_Prefab/Armor/Female/087/000/Slinger/ch02_087_0006.pfb.18":
                       F.pfb_file(CHAR + "ch03/087/000/6/ch03_087_0006.mesh")})
    assert R.plan(items, A, B, game=game).ok


# ---- a .pak mod ---------------------------------------------------------------------------------------
STAFF_ITEM = "natives/stm/art/model/item/it09/00/0021/it0900_0021_0"
PRIVATE = "Art/Model/Item/it09/00/0021/textures/runestaff_tripo_ALBD.tex"  # not a game name
SECRET = path_hash("natives/STM/Art/Someone/secret.bin.1")


def test_a_pak_mod_is_rekeyed(tmp_path):
    index = PathIndex(None, paths=[STAFF_ITEM + ".mesh.241111606", STAFF_ITEM + ".mdf2.45",
                                   "natives/stm/gamedesign/equip/_prefab/weapon/wp09/00/0021/it0900_0021_0_wvp.user.3",
                                   "natives/stm/art/model/item/it02/00/0029/it0200_0029_0.mesh.241111606"])
    material_data = mdf2(material("Emblem", (PRIVATE, "Art/Model/Item/it09/00/0021/textures/it0900_0021_0_NRRO.tex")))
    deflated = compress_as(COMPRESSION_DEFLATE, material_data)
    entries = [
        (path_hash(STAFF_ITEM + ".mesh.241111606"), MESH, len(MESH), COMPRESSION_NONE),
        (path_hash(STAFF_ITEM + ".mdf2.45"), deflated, len(material_data), COMPRESSION_DEFLATE),
        (path_hash(N + PRIVATE + ".241106027"), TEX, len(TEX), COMPRESSION_NONE),  # named by the material
        (SECRET, b"secret", 6, COMPRESSION_NONE),  # never named: kept as it is
        (path_hash(N + "GameDesign/Equip/_Prefab/Weapon/wp09/00/0021/it0900_0021_0_wvp.user.3"), b"USR\x00", 4, 0),
        (path_hash(N + "Art/Model/Item/it02/00/0029/it0200_0029_0.mesh.241111606"), MESH, len(MESH), 0),
    ]
    write_pak(entries, tmp_path / "RuneStaff.pak")
    items = R.items_of({"RuneStaff.pak": tmp_path / "RuneStaff.pak"}, index)
    assert sum(1 for item in items if item.path is None) == 1
    plan = R.plan(items, STAFF, STAFF_B)
    assert plan.ok, plan.errors
    assert {m.new: m.how for m in plan.moves} == {
        "natives/stm/art/model/item/it09/00/0029/it0900_0029_0.mesh.241111606": "copy",
        "natives/stm/art/model/item/it09/00/0029/it0900_0029_0.mdf2.45": "mdf2",
        "natives/stm/art/model/item/it09/00/0029/textures/runestaff_tripo_albd.tex.241106027": "copy"}
    assert {m.pak for m in plan.moves} == {"RuneStaff.pak"}
    assert plan.kept == [f"RuneStaff.pak#{SECRET:016X}"]
    assert len(plan.dropped) == 2 and any("PAK 안의 이름을 알 수 없는 파일 1개" in n for n in plan.notes)

    built = R.build(plan, items, tmp_path / "out")
    assert built.files == ["RuneStaff.pak"] and len(built.moves) == 3
    new_pak = PakFile(tmp_path / "out" / "RuneStaff.pak")
    rekeyed = {path_hash("natives/STM/Art/Model/Item/it09/00/0029/it0900_0029_0.mesh.241111606"),
               path_hash("natives/STM/Art/Model/Item/it09/00/0029/it0900_0029_0.mdf2.45"),
               path_hash("natives/STM/Art/Model/Item/it09/00/0029/textures/runestaff_tripo_ALBD.tex.241106027")}
    assert set(new_pak.order) == rekeyed | {SECRET}
    assert {new for new, _old in built.pak_moves} == rekeyed
    assert {old for _new, old in built.pak_moves} == {e[0] for e in entries[:3]}
    # the table's order is kept for what is left, every entry compressed as it was
    old_order = [h for h in PakFile(tmp_path / "RuneStaff.pak").order if h in {e[0] for e in entries[:4]}]
    assert new_pak.order == [dict((o, n) for n, o in built.pak_moves).get(h, h) for h in old_order]
    i = new_pak.find(path_hash("natives/STM/Art/Model/Item/it09/00/0029/it0900_0029_0.mdf2.45"))
    assert new_pak.attrs[i] & 0xF == COMPRESSION_DEFLATE
    rewritten = zlib.decompress(new_pak.read_raw(i), -15)
    assert texture_paths(rewritten) == ["Art/Model/Item/it09/00/0029/textures/runestaff_tripo_ALBD.tex",
                                        "Art/Model/Item/it09/00/0021/textures/it0900_0021_0_NRRO.tex"]
    assert new_pak.read_hash(SECRET) == b"secret"


def test_an_unreadable_pak_is_an_error(tmp_path):
    write(tmp_path / "Broken.pak", b"nope")
    with pytest.raises(RetargetError, match="Broken.pak"):
        R.items_of({"Broken.pak": tmp_path / "Broken.pak"}, None)


# ---- a model and its material go together ---------------------------------------------------------------
def with_a(game: F.FakeGame, stem: str = BODY, name: str = "ch03_034_0012_body") -> F.FakeGame:
    """The original equipment's own game model (and its streaming/ copy) and material."""
    game.add(N + stem + ".mesh.241111606", MESH + b"A")
    game.add(N + "streaming/" + stem + ".mesh.241111606", MESH + b"A high")
    game.add(N + stem + ".mdf2.45", mdf2(material(name, (ALBD, NRRO))))
    return game


def body_game() -> F.FakeGame:
    return with_a(armor_game(F.pfb_file(BODY_B + ".mesh", BODY_B + ".mdf2")))


def test_a_part_with_only_its_model_gets_the_originals_material(tmp_path):
    """A mesh edit relies on A's game material (its material names are A's): B's own material would not
    fit, so A's goes with it, pointed at the textures the mod moves."""
    files = fix_paths({BODY + ".mesh.241111606": MESH, "streaming/" + BODY + ".mesh.241111606": MESH + b"high",
                       ALBD + ".241106027": TEX})
    items = loose_mod(tmp_path, files)
    game = body_game()
    plan = R.plan(items, A, B, game=game)
    assert plan.ok, plan.errors
    moves = {m.old: (m.new, m.how) for m in plan.moves}
    assert moves[N + BODY + ".mdf2.45"] == (N + BODY_B + ".mdf2.45", "fill")
    assert [fill.path for fill in plan.fills] == [N + BODY + ".mdf2.45"] and plan.bad_parts == set()
    assert plan.vanilla_refs == 1  # A's NRRO texture, read from the game as before
    assert "여성 몸통: 모드에 모델(.mesh)과 재질(.mdf2)이 짝으로 들어 있지 않아, 원래 장비의 게임 파일 1개(재질)를 " \
           "함께 넣어 짝을 맞춥니다" in plan.notes

    built = R.build(plan, items, tmp_path / "out")
    assert (N + BODY_B + ".mdf2.45", N + BODY + ".mdf2.45") in built.moves  # the recipe compares it with A's
    assert N + BODY_B + ".mdf2.45" in built.files
    data = (tmp_path / "out" / (N + BODY_B + ".mdf2.45")).read_bytes()
    assert texture_paths(data) == [CHAR + "ch03/087/000/2/textures/ch03_087_0002_ALBD.tex", NRRO]
    assert Mdf2File(data, 45).materials[0].name.text == "ch03_034_0012_body"  # A's names, as the model wants

    # without the game, or without A's material in it, that part cannot be moved
    no_game = R.plan(items, A, B)
    assert no_game.errors == ["여성 몸통: 모델(.mesh)과 재질(.mdf2)이 짝으로 들어 있지 않아 원래 장비의 게임 파일로 "
                              "채워야 하는데, 게임 파일을 읽을 수 없습니다"]
    assert no_game.bad_parts == {"body"} and not no_game.fills
    game.remove(N + BODY + ".mdf2.45")
    missing = R.plan(items, A, B, game=game)
    assert missing.errors == ["여성 몸통: 모델(.mesh)과 재질(.mdf2)이 짝으로 들어 있지 않아 원래 장비의 게임 파일로 "
                              "채워야 하는데, 게임에서 찾지 못했습니다 (ch03_034_0012.mdf2)"]
    assert missing.bad_parts == {"body"}
    game.add(N + BODY + ".mdf2.45", b"MDF\x00broken")
    assert "게임 파일을 읽지 못했습니다" in R.plan(items, A, B, game=game).errors[0]


def test_a_part_with_only_its_material_or_textures_gets_the_originals_model(tmp_path):
    game = body_game()
    # a recolor: the material and its texture, made for A's model
    recolor = loose_mod(tmp_path / "recolor", fix_paths({BODY + ".mdf2.45": mdf2(material("body", (ALBD,))),
                                                         ALBD + ".241106027": TEX}))
    plan = R.plan(recolor, A, B, game=game)
    assert plan.ok, plan.errors
    assert {m.old: m.new for m in plan.moves if m.how == "fill"} == {
        N + BODY + ".mesh.241111606": N + BODY_B + ".mesh.241111606",
        N + "streaming/" + BODY + ".mesh.241111606": N + "streaming/" + BODY_B + ".mesh.241111606"}
    assert any("게임 파일 2개(모델)를 함께 넣어" in n for n in plan.notes)
    assert not any("모양이 맞지 않을 수" in n for n in plan.notes)
    out = tmp_path / "recolor-out"
    R.build(plan, recolor, out)
    assert (out / (N + BODY_B + ".mesh.241111606")).read_bytes() == MESH + b"A"
    assert (out / N / "streaming" / (BODY_B + ".mesh.241111606")).read_bytes() == MESH + b"A high"

    # only textures: A's model and material both, the material pointed at the moved texture
    textures = loose_mod(tmp_path / "tex", fix_paths({ALBD + ".241106027": TEX}))
    plan = R.plan(textures, A, B, game=game)
    assert plan.ok, plan.errors
    assert sorted(m.new for m in plan.moves if m.how == "fill") == [
        N + BODY_B + ".mdf2.45", N + BODY_B + ".mesh.241111606", N + "streaming/" + BODY_B + ".mesh.241111606"]
    assert any("게임 파일 3개(모델·재질)를 함께 넣어" in n for n in plan.notes)
    out = tmp_path / "tex-out"
    R.build(plan, textures, out)
    assert texture_paths((out / (N + BODY_B + ".mdf2.45")).read_bytes()) == [
        CHAR + "ch03/087/000/2/textures/ch03_087_0002_ALBD.tex", NRRO]
    plan = R.plan(textures, A, B)  # no game: no longer only a soft note
    assert not plan.ok and plan.bad_parts == {"body"} and not any("모양이 맞지 않을 수" in n for n in plan.notes)


def test_a_part_with_its_own_prefab_needs_nothing_from_the_game(tmp_path):
    """The mod's prefab names the material it uses (A's game one, left as it is)."""
    files = fix_paths({BODY + ".mesh.241111606": MESH, PFB: body_prefab()})
    plan = R.plan(loose_mod(tmp_path, files), A, B)
    assert plan.ok and not plan.fills and plan.vanilla_refs == 3  # A's material, clsp and chain


def test_a_prefab_of_the_mods_own_and_textures_bring_the_originals_material(tmp_path):
    """A retexture at A's own paths next to a prefab of the mod's own (a physics edit) relies on the material of
    A's game the prefab names to read the textures: that material is put at the target too, the moved prefab
    names it and it the moved textures (else the textures would be read by nothing at the target)."""
    game = body_game()
    files = fix_paths({PFB: body_prefab(), BODY + ".mesh.241111606": MESH,
                       "streaming/" + BODY + ".mesh.241111606": MESH + b"high", ALBD + ".241106027": TEX})
    items = loose_mod(tmp_path, files)
    plan = R.plan(items, A, B, game=game)
    assert plan.ok, plan.errors
    assert [(m.old, m.new) for m in plan.moves if m.how == "fill"] == [(N + BODY + ".mdf2.45", N + BODY_B + ".mdf2.45")]
    assert [fill.path for fill in plan.fills] == [N + BODY + ".mdf2.45"] and plan.bad_parts == set()
    assert plan.vanilla_refs == 3  # the chain, the clsp and A's NRRO texture (not the material: it moves)
    assert "여성 몸통: 옮기는 텍스처를 쓰는 재질이 모드에 없어, 모드의 프리팹이 불러오는 원래 장비의 게임 재질 1개를 " \
           "함께 넣어 옮긴 텍스처를 쓰게 합니다" in plan.notes
    assert not any("짝을 맞춥니다" in n or "모드에 모델(.mesh)이 없어" in n for n in plan.notes)
    out = tmp_path / "out"
    R.build(plan, items, out)
    pfb = (out / PFB_B).read_bytes()
    assert rsz.parse(pfb).resources() == {"@" + BODY_B + ".mesh", BODY_B + ".mdf2", CHAIN, CLSP}
    assert rsz_values(pfb)["Material"] == [BODY_B + ".mdf2"]
    assert texture_paths((out / (N + BODY_B + ".mdf2.45")).read_bytes()) == [
        CHAR + "ch03/087/000/2/textures/ch03_087_0002_ALBD.tex", NRRO]

    # only the prefab and textures: the prefab still loads A's game model (it exists), as it did at A
    textures = loose_mod(tmp_path / "tex", fix_paths({PFB: body_prefab(), ALBD + ".241106027": TEX}))
    plan = R.plan(textures, A, B, game=game)
    assert plan.ok, plan.errors
    assert [m.new for m in plan.moves if m.how == "fill"] == [N + BODY_B + ".mdf2.45"]
    assert plan.vanilla_refs == 4  # A's model too
    assert not any("모드에 모델(.mesh)이 없어" in n for n in plan.notes)  # the files go onto A's model, as at A
    R.build(plan, textures, tmp_path / "tex-out")
    assert rsz.parse((tmp_path / "tex-out" / PFB_B).read_bytes()).resources() == {
        "@" + BODY + ".mesh", BODY_B + ".mdf2", CHAIN, CLSP}
    assert texture_paths((tmp_path / "tex-out" / (N + BODY_B + ".mdf2.45")).read_bytes())[0] == \
        CHAR + "ch03/087/000/2/textures/ch03_087_0002_ALBD.tex"

    # without the game (or A's material) that part cannot be moved
    want = "여성 몸통: 옮기는 텍스처를 쓰는 재질이 모드에 없어 모드의 프리팹이 불러오는 원래 장비의 게임 재질(.mdf2)을 함께 " \
           "넣어야 하는데, "
    no_game = R.plan(textures, A, B)
    assert no_game.errors == [want + "게임 파일을 읽을 수 없습니다"] and no_game.bad_parts == {"body"}
    missing = body_game()
    missing.remove(N + BODY + ".mdf2.45")
    assert R.plan(textures, A, B, game=missing).errors == [want + "게임에서 찾지 못했습니다 (ch03_034_0012.mdf2)"]
    # a material of A's that does not read them: nothing to bring (they were read by nothing at A either)
    other = body_game()
    other.add(N + BODY + ".mdf2.45", mdf2(material("ch03_034_0012_body", (NRRO,))))
    plan = R.plan(textures, A, B, game=other)
    assert plan.ok and not plan.fills
    # textures a material of the mod's own reads need nothing either (here one in the author's folder)
    own = "Art/Arsinia/Bunny/body.mdf2"
    files = fix_paths({PFB: body_prefab(extra=(own,)), ALBD + ".241106027": TEX,
                       own + ".45": mdf2(material("body", (ALBD,)))})
    plan = R.plan(loose_mod(tmp_path / "read", files), A, B, game=game)
    assert plan.ok and not plan.fills and plan.kept == [N + own + ".45"]


def test_a_prefab_of_the_mods_own_with_a_material_but_no_model(tmp_path):
    """The prefab loads A's game model and the mod's material: the material moves, the model stays A's (it
    exists) and nothing is put onto the target's model."""
    files = fix_paths({PFB: body_prefab(), BODY + ".mdf2.45": mdf2(material("body", (ALBD,))), ALBD + ".241106027": TEX})
    items = loose_mod(tmp_path, files)
    plan = R.plan(items, A, B, game=body_game())
    assert plan.ok, plan.errors
    assert not plan.fills and plan.vanilla_refs == 3  # A's model, chain and clsp
    assert not any("모드에 모델(.mesh)이 없어" in n for n in plan.notes)
    R.build(plan, items, tmp_path / "out")
    assert rsz.parse((tmp_path / "out" / PFB_B).read_bytes()).resources() == {
        "@" + BODY + ".mesh", BODY_B + ".mdf2", CHAIN, CLSP}


def test_a_pak_mod_gets_the_originals_material_inside_its_pak(tmp_path):
    index = PathIndex(None, paths=[(N + BODY + ".mesh.241111606").lower()])
    write_pak([(path_hash(N + BODY + ".mesh.241111606"), MESH, len(MESH), COMPRESSION_NONE)], tmp_path / "Body.pak")
    items = R.items_of({"Body.pak": tmp_path / "Body.pak"}, index)
    plan = R.plan(items, A, B, game=body_game())
    assert plan.ok, plan.errors
    assert [(m.new, m.how, m.pak) for m in plan.moves if m.how == "fill"] == [
        (N + BODY_B + ".mdf2.45", "fill", "Body.pak")]
    built = R.build(plan, items, tmp_path / "out")
    assert built.files == ["Body.pak"] and not (tmp_path / "out" / "natives").exists()
    fill = path_hash(N + BODY_B + ".mdf2.45")
    assert (fill, path_hash(N + BODY + ".mdf2.45")) in built.pak_moves
    assert (N + BODY_B + ".mdf2.45", N + BODY + ".mdf2.45") in built.moves
    new = PakFile(tmp_path / "out" / "Body.pak")
    assert new.order == [path_hash(N + BODY_B + ".mesh.241111606"), fill]
    assert texture_paths(new.read_hash(fill)) == [ALBD, NRRO]  # the mod has no textures: A's game ones


def test_a_weapon_model_without_its_material(tmp_path):
    files = sword_mod()
    del files[N + ITEM + "_1.mdf2.45"]  # the blade texture stays (it moves with the folder)
    items = loose_mod(tmp_path, files)
    game = sword_game()
    game.add(N + ITEM + "_1.mdf2.45",
             mdf2(material("blade", ("Art/Model/Item/it03/00/0009/textures/blade_ALBD.tex",))))
    plan = R.plan(items, SWORD, SWORD_B, game=game)
    assert plan.ok, plan.errors
    assert [m.new for m in plan.moves if m.how == "fill"] == [N + ITEM_B + "_1.mdf2.45"]
    R.build(plan, items, tmp_path / "out")
    assert texture_paths((tmp_path / "out" / (N + ITEM_B + "_1.mdf2.45")).read_bytes()) == [
        "Art/Model/Item/it03/00/0029/textures/blade_ALBD.tex"]
    plan = R.plan(items, SWORD, SWORD_B, game=sword_game())
    assert plan.errors == ["보조: 모델(.mesh)과 재질(.mdf2)이 짝으로 들어 있지 않아 원래 장비의 게임 파일로 채워야 하는데, "
                           "게임에서 찾지 못했습니다 (it0300_0009_1.mdf2)"]
    assert plan.bad_parts == set()


# ---- prefabs of the mod's own -------------------------------------------------------------------------------
SLINGER_M = CHAR + "ch02/034/001/6/ch02_034_0016"
SLINGER_F = CHAR + "ch03/034/001/6/ch03_034_0016"
SLINGER_PFB_B = N + "GameDesign/Equip/_Prefab/Armor/{}/087/000/Slinger/{}_087_0006.pfb.18"


def test_a_slinger_rope_prefab_is_not_the_slingers_own(tmp_path):
    """A rope prefab moves with the slinger, but the target's slinger prefab still decides which model shows."""
    rope = N + "GameDesign/Equip/_Prefab/Armor/Male/034/001/SlingerRope_034_001.pfb.18"
    files = fix_paths({SLINGER_M + ".mesh.241111606": MESH, SLINGER_M + ".mdf2.45": mdf2(material("sling"))})
    files[rope] = prefab([{F.TYPE: "via.GameObject", "Name": "SlingerRope_034_001"}])
    game = F.FakeGame({SLINGER_PFB_B.format("Male", "ch02"): F.pfb_file(CHAR + "ch02/001/000/6/ch02_001_0006.mesh")})
    plan = R.plan(loose_mod(tmp_path, files), A, B, game=game)
    assert plan.errors == ["대상 장비의 남성 슬링어는 다른 장비의 모델을 불러와서, 옮긴 모델이 보이지 않습니다"]
    assert plan.bad_parts == {"slinger"}
    assert "SlingerRope_087_000.pfb.18" in {m.new.rpartition("/")[2] for m in plan.moves}


def test_the_mods_own_prefab_must_land_where_the_target_keeps_its_own(tmp_path):
    """Three of the game's female slinger prefabs are named after the male body: the mod's ch03_... prefab
    moved there would never be asked for."""
    own = N + "GameDesign/Equip/_Prefab/Armor/Female/034/001/Slinger/ch03_034_0016.pfb.18"
    files = fix_paths({SLINGER_F + ".mesh.241111606": MESH, SLINGER_F + ".mdf2.45": mdf2(material("sling"))})
    files[own] = body_prefab(stem=SLINGER_F, chain=CHAR + "ch03/034/001/6/ch03_034_0016.chain2")
    items = loose_mod(tmp_path, files)
    game = F.FakeGame({SLINGER_PFB_B.format("Female", "ch02"): F.pfb_file(CHAR + "ch02/087/000/6/ch02_087_0006.mesh")})
    plan = R.plan(items, A, B, game=game)
    assert plan.errors == ["대상 장비의 여성 슬링어 프리팹은 이름(ch02_087_0006.pfb.18)이 모드의 프리팹"
                           "(ch03_087_0006.pfb.18)과 달라, 옮긴 프리팹을 게임이 불러오지 않습니다"]
    assert plan.bad_parts == {"slinger"}
    # under the same name the target's prefab is replaced, whatever it loads
    game.add(SLINGER_PFB_B.format("Female", "ch03"), F.pfb_file(CHAR + "ch02/087/000/6/ch02_087_0006.mesh"))
    assert R.plan(items, A, B, game=game).ok
    # a target without the part, and a target that does not read
    assert R.plan(items, A, B, game=F.FakeGame()).errors == ["대상 장비에는 여성 슬링어가 없습니다"]
    other_part = R.plan(items, A, B, parts=["helm", "body", "slinger"], game=F.FakeGame())
    assert other_part.bad_parts == {"slinger"}


# ---- files that stay where they are or out ------------------------------------------------------------------
def test_the_mods_own_shader_stays_where_it_is(tmp_path):
    """Materials name their shader (.mmtr) and that name is never rewritten: the shader is kept as it is, even
    one in A's own folder."""
    custom = "MaterialShader/Custom/Arsinia_Skin.mmtr"
    in_folder = CHAR + "ch03/034/001/2/bunny_skin.mmtr"
    body, skin = material("body", (ALBD,)), material("skin")
    body["mmtr_path"], skin["mmtr_path"] = custom, in_folder
    files = fix_paths({BODY + ".mesh.241111606": MESH, BODY + ".mdf2.45": assemble_mdf2([body, skin], 45),
                       ALBD + ".241106027": TEX, custom + ".250206176": b"MMTR one", in_folder + ".250206176": b"MMTR two",
                       "MaterialShader/Custom/unused.mmtr.250206176": b"MMTR three"})
    items = loose_mod(tmp_path, files)
    plan = R.plan(items, A, B)
    assert plan.ok, plan.errors
    assert plan.kept == sorted([N + custom + ".250206176", N + in_folder + ".250206176"])
    assert not any(".mmtr" in m.old for m in plan.moves)
    assert dict(plan.dropped) == {N + "MaterialShader/Custom/unused.mmtr.250206176": "옮기는 파일이 쓰지 않습니다"}
    out = tmp_path / "out"
    R.build(plan, items, out)
    assert (out / (N + custom + ".250206176")).read_bytes() == b"MMTR one"
    assert (out / (N + in_folder + ".250206176")).read_bytes() == b"MMTR two"
    moved = Mdf2File((out / (N + BODY_B + ".mdf2.45")).read_bytes(), 45)
    assert [m.mmtr_path for m in moved.materials] == [custom, in_folder]
    assert R.references(files[N + BODY + ".mdf2.45"], BODY + ".mdf2.45")[-2:] == [custom, in_folder]


def test_sound_files_are_left_out(tmp_path):
    files = fix_paths({ITEM + "_0.mesh.241111606": MESH, ITEM + "_0.mdf2.45": mdf2(material("blade")),
                       "Sound/Wwise/it0300_0009_m.sbnk.1.x64": b"BKHD", "Sound/Wwise/it0300_0009_m.spck.1.x64": b"AKPK",
                       "Sound/UserData/12_Bank/Weapon/wp03/it0300_0009_BankListData.user.3": b"USR\x00"})
    plan = R.plan(loose_mod(tmp_path, files), SWORD, SWORD_B)
    assert plan.ok, plan.errors
    assert sorted(m.new.rpartition("/")[2] for m in plan.moves) == ["it0300_0029_0.mdf2.45", "it0300_0029_0.mesh.241111606"]
    assert {reason for _path, reason in plan.dropped} == {"소리 파일은 옮기지 않습니다"} and len(plan.dropped) == 3
    assert "소리 파일은 옮기지 않습니다 (게임이 다른 무기의 소리로 읽지 않습니다)" in plan.notes


def test_sound_banks_in_a_pak_are_left_out_too(tmp_path):
    """The path list names no sound files: a pak's bank (Wwise BKHD) or package (AKPK) is told by its first bytes
    and left out as a loose one would be (kept, it would go on changing the original weapon's sound)."""
    files = {N + ITEM + "_0.mesh.241111606": MESH, N + ITEM + "_0.mdf2.45": mdf2(material("blade")),
             N + "Sound/Wwise/it0300_0009_m.sbnk.1.x64": b"BKHD" + bytes(28),
             N + "Sound/Wwise/it0300_0009_m.spck.1.x64": b"AKPK" + bytes(28)}
    index = PathIndex(None, paths=[path.lower() for path in list(files)[:2]])
    write_pak([(path_hash(p), d, len(d), COMPRESSION_NONE) for p, d in files.items()], tmp_path / "Sword.pak")
    items = R.items_of({"Sword.pak": tmp_path / "Sword.pak"}, index)
    assert sum(1 for item in items if item.path is None) == 2
    plan = R.plan(items, SWORD, SWORD_B, game=sword_game())
    assert plan.ok, plan.errors
    banks = [path_hash(p) for p in list(files)[2:]]
    assert sorted(plan.dropped) == sorted((f"Sword.pak#{h:016X}", "소리 파일은 옮기지 않습니다") for h in banks)
    assert "소리 파일은 옮기지 않습니다 (게임이 다른 무기의 소리로 읽지 않습니다)" in plan.notes
    assert plan.kept == [] and not any("이름을 알 수 없는" in n for n in plan.notes)
    R.build(plan, items, tmp_path / "out")
    new = PakFile(tmp_path / "out" / "Sword.pak")
    assert len(new.order) == 2 and all(new.read_hash(h) is None for h in banks)


def test_unnamed_models_and_materials_in_a_pak_are_refused(tmp_path):
    """A pak entry the path list cannot name stays at A's hash: a model or material among them would leave
    half of the part behind (a material of a version the list does not know, a model of new equipment)."""
    index = PathIndex(None, paths=[(N + BODY + ".mesh.241111606").lower()])
    material_data = mdf2(material("body", (ALBD,)))
    old_mdf2 = path_hash(N + BODY + ".mdf2.46")
    entries = [(path_hash(N + BODY + ".mesh.241111606"), MESH, len(MESH), COMPRESSION_NONE),
               (old_mdf2, material_data, len(material_data), COMPRESSION_NONE),  # unnamed: a version not in the list
               (path_hash(N + ALBD + ".241106027"), TEX, len(TEX), COMPRESSION_NONE),  # named after the material
               (path_hash(N + CHAR + "ch03/130/000/2/ch03_130_0002.mesh.241111606"), MESH, len(MESH), 0),  # unnamed
               (SECRET, b"secret", 6, COMPRESSION_NONE)]
    write_pak(entries, tmp_path / "Bunny.pak")
    items = R.items_of({"Bunny.pak": tmp_path / "Bunny.pak"}, index)
    assert sum(1 for item in items if item.path is None) == 3
    plan = R.plan(items, A, B)
    assert "PAK 안에 이름을 알 수 없는 모델·재질 파일이 있어 옮길 수 없습니다 (파일 2개: 그 파일만 원래 장비에 남게 " \
           "됩니다)" in plan.errors
    assert not any("이름을 알 수 없는 파일 3개" in n for n in plan.notes)
    # and an unnamed entry is never rewritten (it would point A's hash at B's files)
    plan.errors.clear()
    R.build(plan, items, tmp_path / "out")
    assert PakFile(tmp_path / "out" / "Bunny.pak").read_hash(old_mdf2) == material_data
    # unnamed files of other kinds are only noted
    write_pak([entries[0], entries[4]], tmp_path / "Other.pak")
    plan = R.plan(R.items_of({"Other.pak": tmp_path / "Other.pak"}, index), A, B, game=body_game())
    assert plan.ok and any("이름을 알 수 없는 파일 1개" in n for n in plan.notes)


def rsz_like(magic: bytes, *strings: str) -> bytes:
    """An RSZ file's head and UTF-16 strings, as much of one as an unnamed pak entry is looked at."""
    return magic + bytes(12) + b"".join(s.encode("utf-16-le") + b"\0\0" for s in strings)


def test_unnamed_rsz_files_and_materials_are_refused_only_when_they_name_the_original(tmp_path):
    """The path list holds equipment files only: a gameplay .user, an effect prefab or a material of a version it
    lacks stays unnamed.  Such a file stays at its hash; it blocks the move only when it names A inside (then it
    is most likely A's own file, which would stay behind); a model always does."""
    index = PathIndex(None, paths=[(N + BODY + ".mesh.241111606").lower(), (N + BODY + ".mdf2.45").lower()])
    mat = mdf2(material("ch03_034_0012_body"))
    param = rsz_like(b"USR\x00", "app.user_data.Wp11ActionParam", "GameDesign/Player/ActionData/Wp11/Wp11Param")
    effect = rsz_like(b"PFB\x00", "via.effect.EffectPlayer", "Art/VFX/Common/hit_spark.efx")
    other_mat = mdf2(material("skin", (CUSTOM,)))
    named = [(path_hash(N + BODY + ".mesh.241111606"), MESH, len(MESH), COMPRESSION_NONE),
             (path_hash(N + BODY + ".mdf2.45"), mat, len(mat), COMPRESSION_NONE)]
    unnamed = [(path_hash(N + "GameDesign/Player/ActionData/Wp11/Wp11Param.user.3"), param, len(param), 0),
               (path_hash(N + "Art/VFX/Common/hit_spark.pfb.18"), effect, len(effect), 0),
               (path_hash(N + "Art/Arsinia/Bunny/skin.mdf2.40"), other_mat, len(other_mat), 0)]
    write_pak(named + unnamed, tmp_path / "Bunny.pak")
    items = R.items_of({"Bunny.pak": tmp_path / "Bunny.pak"}, index)
    assert sum(1 for item in items if item.path is None) == 3
    plan = R.plan(items, A, B, game=body_game())
    assert plan.ok, plan.errors
    assert plan.kept == sorted(f"Bunny.pak#{e[0]:016X}" for e in unnamed)
    assert "PAK 안의 이름을 알 수 없는 파일 3개는 옮기지 못해 그대로 넣습니다. 원래 장비의 파일이라면 원래 장비에도 " \
           "그대로 적용됩니다" in plan.notes
    R.build(plan, items, tmp_path / "out")
    assert all(PakFile(tmp_path / "out" / "Bunny.pak").read_hash(e[0]) == e[1] for e in unnamed)

    # naming A inside: A's hair settings, A's prefab under a version the list lacks; the error says what they are
    hair = rsz_like(b"USR\x00", "app.HairAdjustParam", "ch03_034_0013")
    own = rsz_like(b"PFB\x00", "ch03_034_0012", "@" + BODY + ".mesh")
    blocked = [(path_hash(N + CHAR + "ch03/034/001/3/Hair/ch03_034_0013_HairAdjustList.user.4"), hair, len(hair), 0),
               (path_hash(PFB[:-2] + "19"), own, len(own), 0)]
    write_pak(named + unnamed + blocked[:1], tmp_path / "Hair.pak")
    plan = R.plan(R.items_of({"Hair.pak": tmp_path / "Hair.pak"}, index), A, B, game=body_game())
    assert plan.errors == ["PAK 안에 이름을 알 수 없는 데이터 파일이 있어 옮길 수 없습니다 (파일 1개: 그 파일만 원래 "
                           "장비에 남게 됩니다)"]
    write_pak(named + unnamed + blocked + [(path_hash(N + CHAR + "ch03/130/000/2/ch03_130_0002.mesh.241111606"), MESH,
                                            len(MESH), 0)], tmp_path / "All.pak")
    plan = R.plan(R.items_of({"All.pak": tmp_path / "All.pak"}, index), A, B, game=body_game())
    assert plan.errors == ["PAK 안에 이름을 알 수 없는 모델·프리팹·데이터 파일이 있어 옮길 수 없습니다 (파일 3개: 그 "
                           "파일만 원래 장비에 남게 됩니다)"]


# ---- what else the user should know --------------------------------------------------------------------------
def test_files_moved_into_a_body_folder_the_mod_has_no_model_for(tmp_path):
    """The game keeps the physics of some female parts under ch02: at the target, its male part loads them."""
    leg = CHAR + "ch03/034/001/4/ch03_034_0014"
    files = fix_paths({leg + ".mesh.241111606": MESH, leg + ".mdf2.45": mdf2(material("leg")),
                       CHAR + "ch02/034/001/4/ch02_034_0014.chain2.14": b"CHAIN"})
    plan = R.plan(loose_mod(tmp_path, files), A, B)
    assert plan.ok
    assert "모드에 남성용 다리 모델이 없는데 남성용 폴더(ch02)의 파일 1개도 옮깁니다. 대상 장비의 남성용에도 영향을 " \
           "줄 수 있습니다" in plan.notes
    assert not any("여성용 폴더" in n for n in plan.notes)
    # a part without any model gets the note about that instead
    chain = loose_mod(tmp_path / "chain", fix_paths({CHAR + "ch02/034/001/4/ch02_034_0014.chain2.14": b"CHAIN"}))
    notes = R.plan(chain, A, B).notes
    assert any(n.startswith("다리: 모드에 모델(.mesh)이 없어") for n in notes)
    assert not any("남성용 폴더" in n for n in notes)
    # with the male model too, nothing to say
    files[N + CHAR + "ch02/034/001/4/ch02_034_0014.mesh.241111606"] = MESH
    files[N + CHAR + "ch02/034/001/4/ch02_034_0014.mdf2.45"] = mdf2(material("leg"))
    assert not any("남성용 폴더" in n for n in R.plan(loose_mod(tmp_path / "both", files), A, B).notes)


def test_artian_weapons_share_their_models(tmp_path):
    items = loose_mod(tmp_path, sword_mod())
    artian = R.plan(items, SWORD, Slot("weapon", "03/100004"))
    assert artian.ok and any(n.startswith("아티아 무기는 여러 무기가 같은 모델을 함께 씁니다") for n in artian.notes)


def test_the_targets_own_visual_settings_stay(tmp_path):
    """The target's avp (hide/merge rules, the helm's hair settings) is never replaced: say so."""
    helm = CHAR + "ch03/034/001/3/ch03_034_0013"
    files = fix_paths({helm + ".mesh.241111606": MESH, helm + ".mdf2.45": mdf2(material("helm")),
                       BODY + ".mesh.241111606": MESH, BODY + ".mdf2.45": mdf2(material("body"))})
    items = loose_mod(tmp_path, files)
    hair = "머리카락 숨김·다른 부위와 겹칠 때 숨기는 설정은 대상 장비의 것을 씁니다. 머리카락이 투구를 뚫고 나오거나 " \
           "일부가 보이지 않을 수 있습니다"
    assert hair in R.plan(items, A, B).notes
    body = R.plan(items, A, B, parts=["body"]).notes
    assert hair not in body and "다른 부위와 겹칠 때 숨기는 설정은 대상 장비의 것을 씁니다. 일부가 보이지 않을 수 있습니다" in body


def test_sources_of_scanned_targets():
    targets = [Target("armor", "f/034/001", "body"), Target("armor", "f/034/001", "helm"),
               Target("armor", "m/034/001", "body"), Target("weapon", "03/000009", "main"),
               Target("weapon", "03/000009", "sub"), Target("hunter", "inner/f/000"), Target("palico", "062", "helm"),
               Target("weapon", "10/030005", "main")]
    assert R.sources_of(targets) == [Target("armor", "f/034/001"), Target("armor", "m/034/001"),
                                     Target("weapon", "03/000009")]
    assert R.sources_of([]) == []
