"""Game paths -> equipment, the bundled path list and its hash index (synthetic data; the only
real paths are names from the community file list and from users' mods)."""

import importlib.util
import lzma
from pathlib import Path

import pytest

from mhwmm.equip import pathlist
from mhwmm.equip.model import Target
from mhwmm.equip.pathlist import PathIndex, full_path, game_paths, npc_visual_settings, streaming_twin
from mhwmm.equip.paths import classify, is_npc_visual_setting, normalize
from mhwmm.matfix.pak import path_hash

ROOT = Path(__file__).resolve().parent.parent


def A(key, part=""):
    return Target("armor", key, part)


def W(key, part=""):
    return Target("weapon", key, part)


CASES = [
    # armor models: body from ch02/ch03, part from the digit folder, any subfolder below it
    ("natives/STM/Art/Model/Character/ch03/004/001/3/ch03_004_0013.mdf2.45", A("f/004/001", "helm")),
    ("natives/STM/Art/Model/Character/ch03/030/601/1/ch03_030_6011.mdf2.45", A("f/030/601", "arm")),
    ("natives\\STM\\Art\\Model\\Character\\ch03\\060\\000\\5\\ch03_060_0005.mesh.241111606", A("f/060/000", "waist")),
    ("Art/Model/Character/ch02/018/001/3/Hair/ch02_018_0013_HairAdjustList.user.3", A("m/018/001", "helm")),
    ("natives/STM/streaming/Art/Model/Character/ch02/004/001/2/textures/ch02_004_0012_FM_MSK4.tex.241106027",
     A("m/004/001", "body")),
    ("Art/Model/Character/ch02/032/500/4/Textures/ch02_032_5004_ALBD.tex.241106027", A("m/032/500", "leg")),
    ("Art/Model/Character/ch02/104/001/6/6/ch02_104_0006.mesh.241111606", A("m/104/001", "slinger")),
    ("natives/STM/Art/Model/Character/mh03/004/000/5/mh03_004_0005.mdf2.45", A("f/004/000", "waist")),  # custom slot
    ("Art/Model/Character/ch02/000/900/ch02_000_9000.fbxskel.7", None),  # the skeleton
    ("Art/Model/Character/ch02/common/common_root_box.clsp.3", None),
    # armor prefabs and visual parameters
    ("natives/STM/GameDesign/Equip/_Prefab/Armor/Female/008/001/Helm/ch03_008_0013.pfb.18", A("f/008/001", "helm")),
    ("GameDesign/Equip/_Prefab/Armor/Female/000/000/Slinger/ch02_000_0006.pfb.18", A("f/000/000", "slinger")),
    ("GameDesign/Equip/_Prefab/Armor/Male/030/601/Arm/ch02_030_6011.pfb.18", A("m/030/601", "arm")),
    ("natives/STM/GameDesign/Equip/_Prefab/Armor/Female/060/000/060_000_avp.user.3", A("f/060/000")),
    ("GameDesign/Equip/_Prefab/Armor/Male/059/010/SlingerRope_059_010.pfb.18", A("m/059/010", "slinger")),
    ("GameDesign/Equip/_Prefab/Armor/Male/069/SlingerRope_069.pfb.18", None),
    ("GameDesign/Equip/_Prefab/Armor/ArmorVisualSetting/PlayerArmorVisualSetting.user.3", None),
    # innerwear (armor model 002)
    ("Art/Model/Character/ch03/002/100/2/ch03_002_1002.chain2.14", Target("hunter", "inner/f/100")),
    ("GameDesign/Equip/_Prefab/Armor/Male/002/200/002_200_avp.user.3", Target("hunter", "inner/m/200")),
    # weapons: model id = SS * 10000 + NNNN, part from the _0/_1 of the file name
    ("natives/STM/Art/Model/Item/it02/00/0029/it0200_0029_1.mesh.241111606", W("02/000029", "sub")),
    ("Art/Model/Item/it02/00/0029/textures/it0200_0029_0_ALBD.tex.241106027", W("02/000029", "main")),
    ("Art/Model/Item/it00/01/0004/it0001_0004_0.mdf2.45", W("00/010004", "main")),
    ("Art/Model/Item/it02/10/0009/it0210_0009_0.mdf2.45", W("02/100009", "main")),
    ("streaming/Art/Model/Item/it11/00/0023/textures/it1100_0023_1_VE_MSK3.tex.241106027", W("11/000023", "sub")),
    ("Art/Model/Item/it10/00/0017/it1000_0017_0.mdf2.45", W("10/000017", "main")),
    ("Art/Model/Item/it02/00/0008/it0200_00008_0.sfur.5", W("02/000008", "main")),
    ("Art/Model/Item/it04/00/0002/it0400_0002.chain2.13", W("04/000002")),
    ("Art/Model/Item/it09/00/0021/textures/runestaff_vivid_axe_ALBA.tex.241106027", W("09/000021")),
    ("Art/Model/Item/it00/99/it0099_0000_0.chain2.13", None),
    ("GameDesign/Equip/_Prefab/Weapon/Wp00/00/0001/it0000_0001_0.pfb.18", W("00/000001", "main")),
    ("GameDesign/Equip/_Prefab/Weapon/Wp03/00/0016/it0300_0016_1_wvp.user.3", W("03/000016", "sub")),
    ("GameDesign/Equip/_Prefab/Weapon/Wp02/10/0000/it0210_0000_0.pfb.18", W("02/100000", "main")),
    ("GameDesign/Equip/_Prefab/Weapon/Wp02/99/it0299_0000_0_mpgd.user.3", None),
    # kinsects
    ("Art/Model/Item/it10/03/0005/it1003_0005_0.mesh.241111606", Target("kinsect", "0005")),
    ("GameDesign/Equip/_Prefab/Weapon/Wp10/03/0018/it1003_0018_0.pfb.18", Target("kinsect", "0018")),
    # palico: 0000 body, 1000 helm, 2000 weapon
    ("Art/Model/Character/ch05/062/0000/ch05_062_0000.mesh.241111606", Target("palico", "062", "body")),
    ("Art/Model/Character/ch05/019/1000/textures/ch05_019_1000_detail_MSK4.tex.241106027",
     Target("palico", "019", "helm")),
    ("streaming/Art/Model/Character/ch05/046/1000/Texture/ch05_046_1000_ALBD.tex.241106027",
     Target("palico", "046", "helm")),
    ("Art/Model/Character/ch05/019/2000/ch05_019_2000.mesh.241111606", Target("palico", "019", "weapon")),
    ("Art/Model/Character/ch05/000/0001/ch05_000_0001.mesh.241111606", None),
    ("GameDesign/Otomo/_Prefab/Equip/062/ch05_062_0000.pfb.18", Target("palico", "062", "body")),
    ("GameDesign/Otomo/_Prefab/Equip/011/ch05_011_1000.pfb.18", Target("palico", "011", "helm")),
    ("GameDesign/Otomo/_Prefab/Equip/006/OtomoHeadPartsEnableData_006.user.3", Target("palico", "006", "helm")),
    ("GameDesign/Otomo/_Prefab/Equip/004/OtomoWeaponConstData_004.user.3", None),
    ("GameDesign/Otomo/_Prefab/Equip/NPC/ch05_210_0000.pfb.18", None),
    # seikret
    ("Art/Model/Character/ch07/901/0010/ch07_901_0010.mesh.241111606", Target("seikret", "model/901/0010")),
    ("GameDesign/Porter/_Prefab/Equip/012/Body_012.pfb.18", Target("seikret", "equip/012", "body")),
    ("GameDesign/Porter/_Prefab/Equip/800/Saddle_800.pfb.18", Target("seikret", "equip/800", "saddle")),
    ("GameDesign/Porter/_Prefab/PT00_00_000.pfb.18", None),
    # NPCs
    ("Art/Model/Character/ch04/0/00/00/00/ch04_000_0000.mesh.241111606", Target("npc", "000_0000")),
    ("Art/Model/Character/ch04/2/01/04/00/ch04_201_0400.mesh.241111606", Target("npc", "201_0400")),
    ("Art/Model/Character/ch00/500/0007/textures/ch00_500_0007_ALBD.tex.241106027", Target("npc", "face/500")),
    ("Art/Model/Character/ch00/800/CommonTextures/OT_paint_01_MSK4.tex.241106027", Target("npc", "face/800")),
    ("Art/Model/Character/ch01/910/0/001/texture/ch01_910_0001_NRRO.tex.241106027", Target("npc", "hair/910")),
    ("GameDesign/NPC/_Prefab/Model/Human/ch04_000_0000.pfb.18", Target("npc", "000_0000")),
    ("GameDesign/NPC/_Prefab/Model/HunterSlinger/ch04_000_0071_9_slinger.pfb.18", Target("npc", "000_0071")),
    ("GameDesign/NPC/_Prefab/Model/HumanFace/ch00_507_0000.pfb.18", Target("npc", "face/507")),
    ("GameDesign/NPC/_Prefab/Model/RyujinBeard/ch01_930_2100.pfb.18", Target("npc", "hair/930")),
    ("GameDesign/NPC/_Prefab/Model/Animal/ch06_000_0000.pfb.18", None),
    # monsters
    ("Art/Model/Character/ch90/002/5000/textures/ch90_002_5000_1003_ALBD.tex.241106027", Target("monster", "em0002")),
    ("Art/Model/Character/ch91/151/0000/ziva/ch91_151_0000.zivacomb.240321828.stm", Target("monster", "em1151")),
    # the hunter's own looks
    ("Art/Model/Character/ch00/002/0000/ch00_002_0000.mesh.241111606", Target("hunter", "face/002")),
    ("Art/Model/Character/ch01/001/0/501/textures/ch01_001_0501_ALBD.tex.241106027", Target("hunter", "hair/f/0501")),
    ("Art/Model/Character/ch01/000/2/106/ch01_000_2106.mesh.241111606", Target("hunter", "beard/m/2106")),
    ("GameDesign/Equip/_Prefab/Hair/ch01_000_0008.pfb.18", Target("hunter", "hair/m/0008")),
    ("GameDesign/Equip/_Prefab/EyeBrow/ch01_001_1003.pfb.18", Target("hunter", "eyebrow/f/1003")),
    ("GameDesign/Equip/_Prefab/Beard/ch01_920_2100.pfb.18", Target("npc", "hair/920")),
    ("GameDesign/Equip/_Prefab/Face/ch00_002_0000.pfb.18", Target("hunter", "face/002")),
    ("Art/Model/Character/ch03/CommonTextures/skin/f_body_base_albd.tex.241106027", Target("hunter", "skin/f")),
    ("Art/Model/Character/ch02/CommonTextures/inner/00/inner_00_0014_ALBD.tex.241106027", Target("hunter", "skin/m")),
    ("Art/Model/Character/ch00/CommonTextures/FaceSkin/f/f_face_blend_02_NRRO.tex.241106027",
     Target("hunter", "skin/f")),
    ("Art/Model/Character/ch00/CommonTextures/eye/eye_01_albd.tex.241106027", None),
    ("Art/Model/Character/ch01/002/0/000/ch01_002_0000.mesh.241111606", None),
    # charms, mantles, the amulet
    ("Art/Model/Item/it48/00/0009/it4800_0009_0.mesh.241111606", Target("other", "charm/00/0009")),
    ("GameDesign/Equip/_Prefab/Charm/00/0037/Charm0037_cvp.user.3", Target("other", "charm/00/0037")),
    ("Art/Model/Item/it52/04/0001/texture/it5204_0001_0_ATOS.tex.241106027", Target("other", "mantle/04")),
    ("GameDesign/Equip/_Prefab/Mantle/52/01/FudouSuit.pfb.18", Target("other", "mantle/01")),
    ("Art/Model/Item/it50/15/0001/it5015_0001_3.mesh.241111606", Target("other", "amulet/0001")),
    ("GameDesign/Equip/_Prefab/Amulet/it5015_0001_5.pfb.18", Target("other", "amulet/0001")),
    ("Art/Model/Item/it50/12/0000/textures/it5012_0000_niku_0_atos.tex.241106027", None),
    # not equipment
    ("natives/STM/GUI/ui_texture/tex080000/tex_thumbnail/Character/ch02/tex_ch02_00_003_0_3_IMLM4.tex.241106027", None),
    ("natives/STM/Sound/Wwise/it0100_0006_m.sbnk.1.x64", None),
    ("natives/STM/Art/Model/StageModel/sm42/sm42_320/sm42_320_16.mesh.241111606", None),
    ("natives/STM/GameDesign/Catalog/00_00/Data/PlayerArmorList.user.3", None),
    ("natives/STM/GameDesign/Player/ActionData/Wp12/GlobalParam/Wp12GlobalActionParam.user.3", None),
    ("Art/Model/Character/ch80/001/0500/ch80_001_0500.chain2.14", None),
    ("reframework/autorun/rune_guard_circle.lua", None),
    ("RuneStaff.pak", None),
    ("", None),
]


@pytest.mark.parametrize("path, expected", CASES)
def test_classify(path, expected):
    assert classify(path) == expected


def test_resource_strings_of_a_prefab_classify_like_files():
    # prefab resources: no natives/STM/, no version, a leading @
    assert classify("@Art/Model/Character/ch03/022/000/3/ch03_022_0003.mesh") == A("f/022/000", "helm")


@pytest.mark.parametrize("path, expected", [
    ("natives\\STM\\Art\\Model\\X.mesh.241111606", "art/model/x.mesh"),
    ("natives/stm/streaming/art/a/b.tex.241106027", "art/a/b.tex"),
    ("@Art/Model/Character/ch03/022/000/3/ch03_022_0003.mesh", "art/model/character/ch03/022/000/3/ch03_022_0003.mesh"),
    ("natives/stm/sound/wwise/event_dia.sbnk.1.x64.es419", "sound/wwise/event_dia.sbnk"),
    ("natives/stm/gamedesign/event/a/face.motlist.992.en", "gamedesign/event/a/face.motlist"),
    ("natives/stm/art/model/character/ch91/151/0000/ziva/a.zivacomb.240321828.stm",
     "art/model/character/ch91/151/0000/ziva/a.zivacomb"),
    ("Art/Model/Item/it02/00/0029/it0200_0029_0.chain2.14", "art/model/item/it02/00/0029/it0200_0029_0.chain2"),
    ("Art/a/b.mdf2", "art/a/b.mdf2"),
    ("  natives/STM/Art/x.user.3\n", "art/x.user"),
    # a mod's own name with numbers in it: only the version (and platform/language) goes
    ("Art/Model/Item/it02/00/0029/textures/hat.001.tex", "art/model/item/it02/00/0029/textures/hat.001.tex"),
    ("natives/STM/Art/mine/hat.001.mdf2.45", "art/mine/hat.001.mdf2"),
    ("natives/stm/sound/a.sbnk.1.x64.zhcn", "sound/a.sbnk"),
])
def test_normalize(path, expected):
    assert normalize(path) == expected


def test_npc_visual_settings_are_recognised():
    assert is_npc_visual_setting("natives/STM/GameDesign/NPC/Character/Main/NPC102_00_001/Data/"
                                 "NPC102_00_001_00_VisualSetting.user.3")
    assert not is_npc_visual_setting("natives/STM/GameDesign/NPC/Character/Main/NPC102_00_001/Data/"
                                     "NPC102_00_001_HunterEquip.user.3")


# ---- the bundled list ---------------------------------------------------------------------
def test_bundled_list():
    paths = game_paths()
    assert len(paths) > 40000
    assert paths == sorted(set(paths))
    assert all(p.startswith("natives/stm/") and p == p.lower() for p in paths)
    assert not any(p.startswith("natives/stm/streaming/") for p in paths)  # rebuilt from their twins
    assert all(classify(p) is not None or is_npc_visual_setting(p) for p in paths)
    for p in ("natives/stm/art/model/character/ch03/004/001/3/ch03_004_0013.mdf2.45",
              "natives/stm/gamedesign/equip/_prefab/armor/female/008/001/helm/ch03_008_0013.pfb.18",
              "natives/stm/art/model/item/it09/00/0021/it0900_0021_0.mesh.241111606"):
        assert p in paths
    npc = npc_visual_settings()
    assert len(npc) > 100 and all(is_npc_visual_setting(p) for p in npc)
    assert all(p.split("/")[6].startswith("npc") for p in npc)  # the NPC id folder
    header = lzma.decompress(pathlist.LIST_FILE.read_bytes()).split(b"\n", 1)[0].decode()
    assert header.startswith("# source: ") and "commit" in header


def test_list_version_follows_the_file(tmp_path, monkeypatch):
    version = pathlist.list_version()
    assert len(version) == 16 and version == pathlist.list_version()
    copy = tmp_path / "equip_paths.txt.xz"
    copy.write_bytes(lzma.compress(b"# source: test\nart/model/character/ch03/004/001/3/x.mesh.241111606\n"))
    monkeypatch.setattr(pathlist, "LIST_FILE", copy)
    monkeypatch.setattr(pathlist, "_loaded", {})
    assert pathlist.list_version() not in ("", version)
    assert game_paths() == ["natives/stm/art/model/character/ch03/004/001/3/x.mesh.241111606"]


def test_versions_from_the_list():
    found = pathlist.versions()
    assert found["tex"][0] == "241106027" and found["mesh"][0] == "241111606" and found["mdf2"][0] == "45"
    assert set(found["chain2"]) >= {"13", "14"}


def test_missing_list_gives_no_paths(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(pathlist, "LIST_FILE", tmp_path / "missing.txt.xz")
    monkeypatch.setattr(pathlist, "_loaded", {})
    assert game_paths() == [] and npc_visual_settings() == [] and pathlist.list_version() == ""
    assert "missing.txt.xz" in caplog.text
    assert pathlist.versions()["tex"] == ("241106027",)
    index = PathIndex(tmp_path / "cache")
    assert len(index) == 0 and index.lookup(1) is None


def test_streaming_twin_and_full_path():
    assert streaming_twin("natives/stm/art/a.mesh.241111606") == "natives/stm/streaming/art/a.mesh.241111606"
    assert streaming_twin("natives/STM/Art/a_ALBD.tex.241106027") == "natives/STM/streaming/Art/a_ALBD.tex.241106027"
    assert streaming_twin("natives/stm/art/a.mdf2.45") is None
    assert streaming_twin("natives/stm/streaming/art/a.mesh.241111606") is None
    assert streaming_twin("art/a.mesh.241111606") is None
    assert full_path("Art\\Model\\a.mesh.1") == "natives/STM/Art/Model/a.mesh.1"
    assert full_path("@natives/stm/x.tex.1") == "natives/stm/x.tex.1"


# ---- the hash index ----------------------------------------------------------------------
LIST = ["natives/stm/art/model/character/ch03/004/001/3/ch03_004_0013.mdf2.45",
        "natives/stm/art/model/character/ch03/004/001/3/ch03_004_0013.mesh.241111606",
        "natives/stm/art/model/character/ch03/004/001/3/textures/ch03_004_0013_albd.tex.241106027"]


def check_lookups(index):
    mdf2, mesh, tex = LIST
    assert index.lookup(path_hash(mdf2.replace("art/model", "Art/Model"))) == mdf2  # case does not matter
    assert index.lookup(path_hash(mesh)) == mesh
    assert index.lookup(path_hash(streaming_twin(mesh))) == "natives/stm/streaming/" + mesh[len("natives/stm/"):]
    assert index.lookup(path_hash(streaming_twin(tex))) == streaming_twin(tex)
    assert index.lookup(path_hash("natives/stm/streaming/" + mdf2[len("natives/stm/"):])) is None  # no such copy
    assert index.lookup(path_hash("natives/stm/art/other.mesh.241111606")) is None
    assert len(index) == 5


def test_path_index_round_trip_and_cache(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    first = PathIndex(cache, paths=LIST)
    assert not first.from_cache
    check_lookups(first)
    (saved,) = cache.glob("equip-paths-*.bin")

    calls = []
    monkeypatch.setattr(pathlist, "path_hash", lambda p: calls.append(p) or path_hash(p))
    second = PathIndex(cache, paths=LIST)
    assert second.from_cache and not calls  # nothing hashed again
    check_lookups(second)

    # another list (an app update): its own cache, the old one goes
    third = PathIndex(cache, paths=LIST[:2])
    assert not third.from_cache and not saved.exists() and len(list(cache.glob("equip-paths-*.bin"))) == 1


@pytest.mark.parametrize("damage", ["truncate", "flip", "garbage", "empty"])
def test_corrupt_cache_is_rebuilt(tmp_path, damage):
    cache = tmp_path / "cache"
    PathIndex(cache, paths=LIST)
    (saved,) = cache.glob("equip-paths-*.bin")
    data = bytearray(saved.read_bytes())
    if damage == "truncate":
        data = data[:-5]
    elif damage == "flip":
        data[-3] ^= 0xFF
    elif damage == "garbage":
        data = bytearray(b"x" * len(data))
    else:
        data = bytearray()
    saved.write_bytes(bytes(data))
    index = PathIndex(cache, paths=LIST)
    assert not index.from_cache
    check_lookups(index)
    assert PathIndex(cache, paths=LIST).from_cache  # written again


def test_path_index_without_a_cache_folder_and_extra_paths(tmp_path):
    index = PathIndex(None, paths=LIST)
    assert not index.from_cache and not list(tmp_path.iterdir())
    extra = "Art/Model/Character/ch03/022/000/3/textures/mine_ALBD.tex.241106027"
    index.add([extra, "natives/STM/GameDesign/Equip/x.user.3"])
    assert index.lookup(path_hash("natives/stm/" + extra.lower())) == "natives/STM/" + extra
    assert index.lookup(path_hash("natives/STM/streaming/" + extra)) == "natives/STM/streaming/" + extra
    assert index.lookup(path_hash("natives/STM/GameDesign/Equip/x.user.3")) == "natives/STM/GameDesign/Equip/x.user.3"
    assert len(index) == 5 + 3


def test_unwritable_cache_folder_still_works(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    index = PathIndex(blocker / "cache", paths=LIST)
    check_lookups(index)


# ---- the tool that makes the list ----------------------------------------------------------
def load_tool():
    spec = importlib.util.spec_from_file_location("make_equip_paths", ROOT / "tools" / "make_equip_paths.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_make_equip_paths_from_a_local_list(tmp_path, monkeypatch):
    tool = load_tool()
    source = tmp_path / "MHWS_STM.list"
    source.write_text("\n".join([
        "natives/STM/Art/Model/Character/ch03/004/001/3/ch03_004_0013.mesh.241111606",
        "natives/stm/streaming/art/model/character/ch03/004/001/3/ch03_004_0013.mesh.241111606",  # has a twin
        "natives/stm/streaming/art/model/item/it02/00/0029/it0200_0029_0.mesh.241111606",  # has none: kept
        "natives/stm/gamedesign/npc/character/main/npc102_00_001/data/npc102_00_001_00_visualsetting.user.3",
        "natives/stm/gui/ui_texture/tex080000/tex_thumbnail/item/it00/tex_it0000_0038_imlm4.tex.241106027",
        "natives/stm/sound/wwise/x.sbnk.1.x64",
        "not/natives/at/all.mesh.1",
        "",
    ]) + "\r\n", encoding="utf-8")
    out = tmp_path / "equip_paths.txt.xz"
    monkeypatch.setattr(tool, "OUT", out)
    assert tool.main([str(source)]) == 0
    lines = lzma.decompress(out.read_bytes()).decode("utf-8").splitlines()
    assert lines[0].startswith("# source: MHWS_STM.list sha256 ") and lines[1].startswith("#")
    assert lines[2:] == [
        "art/model/character/ch03/004/001/3/ch03_004_0013.mesh.241111606",
        "gamedesign/npc/character/main/npc102_00_001/data/npc102_00_001_00_visualsetting.user.3",
        "streaming/art/model/item/it02/00/0029/it0200_0029_0.mesh.241111606",
    ]


def test_make_equip_paths_refuses_a_download_that_is_not_the_pinned_list(tmp_path, monkeypatch):
    tool = load_tool()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"natives/stm/art/model/character/ch03/004/001/3/ch03_004_0013.mesh.241111606\n"

    monkeypatch.setattr(tool.urllib.request, "urlopen", lambda *a, **kw: Response())
    monkeypatch.setattr(tool, "OUT", tmp_path / "out.xz")
    assert tool.main([]) == 1 and not (tmp_path / "out.xz").exists()
