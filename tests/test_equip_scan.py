"""Which equipment a mod changes: loose files and a synthetic mod .pak (named entries, streaming
copies, a mod's own textures named through its material, unnamed materials and prefabs judged by
what they point at, look swaps through prefabs, broken files)."""

import struct
from pathlib import Path

import pytest

from mhwmm import matfix
from mhwmm.equip import scan as scanmod
from mhwmm.equip.model import Target
from mhwmm.equip.pathlist import PathIndex
from mhwmm.equip.scan import ModScan, scan_files
from mhwmm.matfix import rsz
from mhwmm.matfix.mdf2 import assemble_mdf2
from mhwmm.matfix.pak import path_hash, write_pak
from conftest import write

SHADER = "MaterialShader/Variation/Base_Equip.mmtr"


def A(key, part=""):
    return Target("armor", key, part)


def W(key, part=""):
    return Target("weapon", key, part)


def mdf2(*textures: str) -> bytes:
    mat = {"name": "m", "mmtr_path": SHADER, "shading_type": 0, "alpha_flags_raw": b"\x98\x00\x00\x88",
           "isV2_unk_0x24": 0, "isV2_unk_0x2C": 0, "isV2_unk_0x30": 0, "gpbf_split": (0, 0), "gpbf_entries": [],
           "textures": [{"type": f"Map{i}", "path": p, "ukn": 0} for i, p in enumerate(textures)],
           "props": [{"name": "Color", "values": [1.0, 1.0, 1.0, 1.0], "count_flags": 0}], "header_version": 1}
    return assemble_mdf2([mat], 45)


def pfb(*resources: str) -> bytes:
    """A prefab with a resource table and an empty RSZ block, laid out like the game's."""
    strings = b"".join(r.encode("utf-16-le") + b"\0\0" for r in resources)
    table = 56
    start = table + 8 * len(resources)
    rsz_off = (start + len(strings) + 15) // 16 * 16
    header = struct.pack("<4s5I3QQ", b"PFB\0", 1, len(resources), 0, 0, 0, 0, table, 0, rsz_off)
    offsets, pos = b"", start
    for r in resources:
        offsets += struct.pack("<Q", pos)
        pos += (len(r) + 1) * 2
    block = struct.pack("<4sIIIII", b"RSZ\0", 16, 1, 1, 0, 0) + struct.pack("<QQQ", 64, 80, 80)
    block = (block + struct.pack("<i", 0)).ljust(64, b"\0") + struct.pack("<II", 0, 0)
    return (header + offsets + strings).ljust(rsz_off, b"\0") + block.ljust(80, b"\0")


TEX = b"TEX\x00" + bytes(60)
MESH = b"MESH" + bytes(60)

HELM_PFB = "natives/STM/GameDesign/Equip/_Prefab/Armor/Female/008/001/Helm/ch03_008_0013.pfb.18"
NERSCYLLA_HELM = ("@Art/Model/Character/ch03/022/000/3/ch03_022_0003.mesh",
                  "Art/Model/Character/ch03/022/000/3/ch03_022_0003.mdf2",
                  "Art/Model/Character/ch03/022/000/3/ch03_022_0003.clsp")


def test_prefab_fixture_reads_like_a_game_prefab():
    info = rsz.parse(pfb(*NERSCYLLA_HELM))
    assert info is not None and info.kind == "pfb" and info.resources() == set(NERSCYLLA_HELM)


# ---- loose files ---------------------------------------------------------------------------
def stage(base: Path, files: dict) -> dict:
    return {rel: write(base / rel.replace("natives/STM/", "natives/stm/"), data) for rel, data in files.items()}


def test_loose_files(tmp_path):
    files = stage(tmp_path / "mod", {
        "natives/STM/Art/Model/Character/ch03/004/001/3/ch03_004_0013.mdf2.45":
            mdf2("Art/Model/Character/ch03/004/001/3/textures/ch03_004_0013_ALBD.tex"),
        "natives/STM/Art/Model/Character/ch03/004/001/3/textures/ch03_004_0013_ALBD.tex.241106027": TEX,
        "natives/STM/Art/Model/Item/it02/00/0029/it0200_0029_0.mesh.241111606": MESH,
        "natives/STM/Art/Model/Item/it02/00/0029/it0200_0029_1.mdf2.45":
            mdf2("Art/Model/Item/it02/00/0029/textures/blahaj_ALBD.tex", "systems/rendering/NullWhite.tex"),
        # a texture of the mod's own name: its part comes from the material that uses it
        "natives/STM/Art/Model/Item/it02/00/0029/textures/blahaj_ALBD.tex.241106027": TEX,
        # a prefab that loads its own weapon's meshes: no look swap
        "natives/STM/GameDesign/Equip/_Prefab/Weapon/Wp02/00/0029/it0200_0029_0.pfb.18":
            pfb("@Art/Model/Item/it02/00/0029/it0200_0029_0.mesh", "@Art/Model/Item/it02/00/0029/it0200_0029_1.mesh"),
        # the Lepus case: a Chatacabra helm prefab that loads the Nerscylla helm
        HELM_PFB: pfb(*NERSCYLLA_HELM),
        "natives/STM/Art/mymod/unused.tex.241106027": TEX,
        "reframework/autorun/script.lua": b"-- lua",
    })
    files["reframework/autorun/script.lua"] = (files["reframework/autorun/script.lua"], "key")  # as mod_entries gives
    result = scan_files(files, None)
    # the male body's type-2 helm has no textures of its own in the game: it uses the female folder's
    assert result.targets == {A("f/004/001", "helm"): 2, A("m/004/001", "helm"): 1, W("02/000029", "main"): 2,
                              W("02/000029", "sub"): 2, A("f/008/001", "helm"): 1}
    assert result.redirects == [(A("f/008/001", "helm"), A("f/022/000", "helm"))]
    assert result.shared == [(A("f/004/001", "helm"), A("m/004/001", "helm"))]
    assert (result.unknown, result.paks, result.errors) == (2, 0, [])
    assert result.items() == {A("f/004/001"): 2, A("m/004/001"): 1, W("02/000029"): 4, A("f/008/001"): 1}


def test_own_textures_do_not_change_the_other_body(tmp_path):
    # textures of the mod's own names in the female folder: only the mod's female material uses them,
    # the male type-2 helm (which borrows the female folder's game textures) keeps its look
    files = stage(tmp_path / "mod", {
        "natives/STM/Art/Model/Character/ch03/004/001/3/ch03_004_0013.mdf2.45":
            mdf2("Art/Model/Character/ch03/004/001/3/textures/perlica_ALBD.tex"),
        "natives/STM/Art/Model/Character/ch03/004/001/3/ch03_004_0013.mesh.241111606": MESH,
        "natives/STM/Art/Model/Character/ch03/004/001/3/textures/perlica_ALBD.tex.241106027": TEX,
        "natives/STM/Art/Model/Character/ch03/004/001/3/textures/ch03_004_0013_ALBD_new.tex.241106027": TEX,
    })
    result = scan_files(files, None)
    assert result.targets == {A("f/004/001", "helm"): 4} and result.shared == []


def test_unreadable_loose_files_do_not_stop_the_scan(tmp_path):
    files = stage(tmp_path / "mod", {
        HELM_PFB: b"PFB\x00 broken",
        "natives/STM/Art/Model/Item/it02/00/0029/it0200_0029_1.mdf2.45": b"MDF\x00 broken",
    })
    files["natives/STM/Art/Model/Item/it02/00/0029/textures/gone.tex.241106027"] = tmp_path / "missing.tex"
    files["natives/STM/Art/Model/Item/it02/00/0029/it0200_0029_0.mdf2.45"] = tmp_path / "missing.mdf2"
    result = scan_files(files, None)
    assert result.targets == {A("f/008/001", "helm"): 1, W("02/000029", "sub"): 1, W("02/000029"): 1,
                              W("02/000029", "main"): 1}
    assert result.redirects == []
    # short and in Korean: the file's name and why, not the OS's message with the full path
    assert result.errors == ["it0200_0029_0.mdf2.45: 파일을 읽지 못했습니다 (파일이 없습니다)"]


def test_seikret_prefabs_load_ch07_models_without_a_look_swap(tmp_path):
    files = stage(tmp_path / "mod", {
        "natives/STM/GameDesign/Porter/_Prefab/Equip/012/Body_012.pfb.18":
            pfb("@Art/Model/Character/ch07/901/0010/ch07_901_0010.mesh"),
        # but a Seikret prefab that loads an armor mesh does change the look
        "natives/STM/GameDesign/Porter/_Prefab/Equip/012/Saddle_012.pfb.18":
            pfb("@Art/Model/Character/ch02/004/000/2/ch02_004_0002.mesh"),
    })
    result = scan_files(files, None)
    assert result.targets == {Target("seikret", "equip/012", "body"): 1, Target("seikret", "equip/012", "saddle"): 1}
    assert result.redirects == [(Target("seikret", "equip/012", "saddle"), A("m/004/000", "body"))]


def test_scenes_and_user_files_are_not_judged_by_what_they_point_at(tmp_path):
    def scene(magic: bytes, *resources: str) -> bytes:
        data = bytearray(pfb(*resources))
        data[:4] = magic
        return bytes(data)

    npc_mesh = "@Art/Model/Character/ch04/0/00/00/00/ch04_000_0000.mesh"
    pak = make_pak(tmp_path / "Stage.pak", [
        (path_hash("natives/STM/Mine/stage.scn.20"), scene(b"SCN\x00", npc_mesh)),
        (path_hash("natives/STM/Mine/setting.user.3"), scene(b"USR\x00", npc_mesh)),
        (path_hash("natives/STM/Mine/npc.pfb.18"), pfb(npc_mesh)),
    ])
    result = scan_files({"Stage.pak": pak}, PathIndex(None, paths=LISTED))
    assert result.targets == {Target("npc", "000_0000"): 1} and result.unknown == 2


# ---- a mod pak -------------------------------------------------------------------------------
W_MESH = "natives/stm/art/model/item/it09/00/0021/it0900_0021_0.mesh.241111606"
W_MDF2 = "natives/stm/art/model/item/it09/00/0021/it0900_0021_0.mdf2.45"
LISTED = [W_MESH, W_MDF2, HELM_PFB.lower()]


def mod_pak_entries() -> list[tuple[int, bytes]]:
    return [
        # named by the list
        (path_hash(W_MDF2),
         mdf2("Art/Model/Item/it09/00/0021/textures/rune_ALBD.tex", "systems/rendering/NullWhite.tex")),
        (path_hash("natives/STM/streaming/Art/Model/Item/it09/00/0021/it0900_0021_0.mesh.241111606"), MESH),
        (path_hash(HELM_PFB), pfb(*NERSCYLLA_HELM)),
        # the mod's own texture, named through the material that uses it
        (path_hash("natives/STM/Art/Model/Item/it09/00/0021/textures/rune_ALBD.tex.241106027"), TEX),
        # a material nothing names: judged by its textures; its own texture is then named through it
        (path_hash("natives/STM/Art/mine/nerscylla_helm.mdf2.45"),
         mdf2("Art/Model/Character/ch03/022/000/3/textures/ch03_022_0003_ALBD.tex",
              "Art/Model/Character/ch03/022/000/3/textures/mine_NRRO.tex", "systems/rendering/NullWhite.tex")),
        (path_hash("natives/STM/streaming/Art/Model/Character/ch03/022/000/3/textures/mine_NRRO.tex.241106027"), TEX),
        # a prefab nothing names: judged by its mesh
        (path_hash("natives/STM/GameDesign/Mine/palico_hat.pfb.18"),
         pfb("@Art/Model/Character/ch05/062/1000/ch05_062_1000.mesh", "Art/x/hat.mdf2")),
        # neither
        (path_hash("natives/STM/Art/mine/whatever.bin"), b"\x01\x02\x03\x04" * 8),
        (path_hash("natives/STM/Art/mine/broken.mdf2.45"), b"MDF\x00" + b"\xff" * 40),
    ]


def make_pak(path: Path, entries, compress=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for h, data in entries:
        stored, how = (compress(data), 2) if compress else (data, 0)
        rows.append((h, stored, len(data), how))
    write_pak(rows, path)
    return path


EXPECTED_PAK = {W("09/000021", "main"): 3, A("f/022/000", "helm"): 2, A("f/008/001", "helm"): 1,
                Target("palico", "062", "helm"): 1}


def test_mod_pak(tmp_path):
    index = PathIndex(tmp_path / "cache", paths=LISTED)
    files = {"Mod.pak": make_pak(tmp_path / "files" / "Mod.pak", mod_pak_entries()),
             "natives/STM/Art/Model/Character/ch05/062/0000/ch05_062_0000.mesh.241111606":
                 write(tmp_path / "files" / "body.mesh", MESH)}
    result = scan_files(files, index)
    assert result.targets == {**EXPECTED_PAK, Target("palico", "062", "body"): 1}
    assert result.redirects == [(A("f/008/001", "helm"), A("f/022/000", "helm"))]
    assert (result.unknown, result.paks, result.errors) == (2, 1, [])


@pytest.mark.skipif(not matfix.available(), reason="zstd 모듈이 없습니다")
def test_compressed_mod_pak(tmp_path):
    index = PathIndex(None, paths=LISTED)
    pak = make_pak(tmp_path / "Mod.pak", mod_pak_entries(), compress=matfix.zstd_compress)
    result = scan_files({"Mod.pak": pak}, index)
    assert result.targets == EXPECTED_PAK and result.unknown == 2 and result.errors == []


def test_mod_pak_without_the_list(tmp_path):
    result = scan_files({"Mod.pak": make_pak(tmp_path / "Mod.pak", mod_pak_entries())}, None)
    # what the files say still counts; a prefab of no known name is no look swap
    assert result.targets[A("f/022/000", "helm")] >= 3
    assert result.targets[Target("palico", "062", "helm")] == 1
    assert result.redirects == [] and result.paks == 1 and result.errors == []


def test_large_entries_are_not_read(tmp_path, monkeypatch):
    monkeypatch.setattr(scanmod, "MAX_READ", 16)
    pak = make_pak(tmp_path / "Mod.pak", mod_pak_entries())
    result = scan_files({"Mod.pak": pak}, PathIndex(None, paths=LISTED))
    assert result.redirects == []  # the prefab was not read either
    assert result.targets == {W("09/000021", "main"): 2, A("f/008/001", "helm"): 1}
    assert result.unknown == 6


def test_broken_paks_are_reported_not_raised(tmp_path):
    good = make_pak(tmp_path / "Good.pak", mod_pak_entries())
    encrypted = make_pak(tmp_path / "Enc.pak", [(path_hash("natives/STM/Art/mine/a.mdf2.45"), mdf2("Art/a.tex"))])
    data = bytearray(encrypted.read_bytes())
    struct.pack_into("<q", data, 16 + 32, 0x10000)  # the entry's attributes: encrypted on its own
    encrypted.write_bytes(bytes(data))
    files = {"Broken.pak": write(tmp_path / "Broken.pak", b"KPKA" + b"\x04\x00" + b"\xff" * 20),
             "NotAPak.pak": write(tmp_path / "NotAPak.pak", b"hello"),
             "Missing.pak": tmp_path / "Missing.pak",
             "Enc.pak": encrypted,
             "Good.pak": good}
    result = scan_files(files, PathIndex(None, paths=LISTED))
    assert result.paks == 5
    assert [e.split(":")[0] for e in result.errors] == ["Broken.pak", "NotAPak.pak", "Missing.pak", "Enc.pak"]
    assert all("읽지 못했습니다" in e for e in result.errors)
    assert result.targets == EXPECTED_PAK
    assert result.unknown == 2 + 1


def test_scan_survives_a_json_round_trip(tmp_path):
    files = stage(tmp_path / "mod", {HELM_PFB: pfb(*NERSCYLLA_HELM), "reframework/x.lua": b""})
    result = scan_files(files, None)
    again = ModScan.from_dict(result.to_dict())
    assert again == result
    assert ModScan.from_dict({**result.to_dict(), "format": -1}) is None
    assert ModScan.from_dict({"format": scanmod.SCAN_FORMAT}) is None
    assert ModScan.from_dict(None) is None


def test_empty_mod():
    assert scan_files({}, None) == ModScan()
