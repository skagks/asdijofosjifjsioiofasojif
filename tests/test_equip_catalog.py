"""The game's equipment catalog from a small synthetic game (no Capcom data): .user tables read
through the real type registry, names from .msg files (Korean, English when Korean is missing),
thumbnail tables, aliases, problems for missing tables, and the JSON cache."""

import json

import pytest

import equip_game_fixtures as fx
from mhwmm.equip import EquipError, catalog, msg
from mhwmm.equip.catalog import Catalog
from mhwmm.equip.model import WEAPON_TYPES, Target
from mhwmm.equip.userdata import read_rows, read_user, rows

N = "natives/STM/"
GUI = N + "GUI/ui_texture/tex080000/tex_thumbnail/"
TEX = ".tex.241106027"
NPC_MAIN = N + "GameDesign/NPC/Character/Main/NPC102_00_001/Data/NPC102_00_001_00_VisualSetting.user.3"
NPC_LOBBY = N + "GameDesign/NPC/Character/Lobby/NPC111_01_003/Data/NPC111_01_003_VisualSetting.user.3"
VISUAL = "app.PorterDef.VISUAL_SETTING_TYPE_Serializable"
ENTRY = ("app.user_data.CharacterEditThumbnails`2.cEntry<app.PorterDef.VISUAL_SETTING_TYPE,"
         "app.PorterDef.VISUAL_SETTING_TYPE_Serializable>")
VISUAL_1 = 7001
DAWN_TREE = -1234567


def A(key, part=""):
    return Target("armor", key, part)


def W(key, part=""):
    return Target("weapon", key, part)


def series(n):
    return fx.enum("app.ArmorDef.SERIES_Serializable", n)


def part(n):
    return fx.enum("app.ArmorDef.ARMOR_PARTS_Serializable", n)


def enum_maker(*rows_):
    return fx.table("ace.user_data.EnumMaker", "ace.user_data.EnumMaker.cData",
                    [{"_EnumName": name, "_EnumValue": value, "_FixedID": fixed} for name, value, fixed in rows_],
                    field="_DataList")


def thumbs(*pairs):
    return fx.app_table("EquipTextureData", [{"_TextureId": tid, "_Prefab": fx.prefab(path)} for tid, path in pairs],
                        field="_DataList")


def thumb_prefab(folder, name):
    return f"GameDesign/GUI/Common/_Prefab/Thumbnail/{folder}/Thumbnail_{name}.pfb"


def thumb_tex(folder, name):
    return f"{GUI}{'Character' if folder.startswith('ch') else 'Item'}/{folder}/tex_{name}_IMLM4{TEX}"


def armor_files() -> dict:
    g = fx.guid
    files = {
        "GameDesign/Common/Equip/ArmorSeriesData": fx.app_table("ArmorSeriesData", [
            # listed out of order: the game's sort id decides (base before alpha)
            {"_Index": 2, "_Series": series(1001), "_Name": g(2), "_ModId": 4, "_ModSubMaleId": 0, "_ModSubFemaleId": 1,
             "_SortId": 36, "_Rare": fx.rare(5)},
            {"_Index": 1, "_Series": series(5), "_Name": g(1), "_ModId": 4, "_ModSubMaleId": 0, "_ModSubFemaleId": 1,
             "_SortId": 4, "_Rare": fx.rare(1)},
            {"_Index": 3, "_Series": series(2002), "_Name": g(3), "_ModId": 60, "_ModSubMaleId": 0, "_ModSubFemaleId": 0,
             "_SortId": 451, "_Rare": fx.rare(5)},
            {"_Index": 0, "_Series": series(0), "_Name": g(0), "_ModId": 0, "_ModSubMaleId": 0, "_ModSubFemaleId": 1}]),
        "GameDesign/Common/Equip/ArmorData": fx.app_table("ArmorData", [
            {"_Series": series(5), "_PartsType": part(0), "_Name": g(10)},
            {"_Series": series(5), "_PartsType": part(1), "_Name": g(11)},
            {"_Series": series(1001), "_PartsType": part(0), "_Name": g(12)},
            {"_Series": series(1001), "_PartsType": part(4), "_Name": g(13)}]),
        "GameDesign/Common/Equip/OuterArmorData": fx.app_table("OuterArmorData", [
            {"_Series": series(2002), "_PartsType": part(0), "_NameMale": g(20), "_NameFemale": g(21)}]),
        "GameDesign/Player/EnumMaker/ArmorSeries": enum_maker(("ID_004", 5, 5), ("ID_061", 61, 1001), ("ID_155", 155, 2002)),
        "GameDesign/GUI/Common/_UserData/ArmorThumbnailTextureData": thumbs(
            (30000510, thumb_prefab("ch03", "ch03_00_005_1_0")),  # female, type 2, helm
            (30000511, thumb_prefab("ch03", "ch03_00_005_1_1")),  # female, type 2, body
            (30000501, thumb_prefab("ch03", "ch03_00_005_0_1")),  # female, type 1, body
            (20006104, thumb_prefab("ch02", "ch02_00_061_0_4")),  # male, type 1, leg: only the alpha has one
            (20015500, thumb_prefab("ch02", "ch02_00_155_0_0"))),
        "GameDesign/Catalog/00_00/Data/PlayerArmorList": {
            fx.TYPE: "app.user_data.PlayerArmorList",
            "_DataList": [{fx.TYPE: "app.user_data.PlayerArmorList.cData", "_ArmorID": m, "_ArmorSubID": s}
                          for m, s in ((4, 0), (4, 1), (60, 0))],
            "_FemaleDataList": [{fx.TYPE: "app.user_data.PlayerArmorList.cData", "_ArmorID": m, "_ArmorSubID": s}
                                for m, s in ((4, 0), (4, 1))]},
    }
    texts = {
        "GameDesign/Text/Excel_Equip/ArmorSeries": fx.names("ArmorSeries", {g(0): ("-", "-"), g(1): ("시험", "Trial"),
                                                                            g(2): ("", "Trial α"), g(3): ("기사단", "Order")}),
        "GameDesign/Text/Excel_Equip/Armor": fx.names("Armor", {g(10): ("시험투구", "Trial Helm"), g(11): ("시험갑옷", "Trial Mail"),
                                                                g(12): ("시험투구α", "Trial Helm α"), g(13): ("시험장화α", "")}),
        "GameDesign/Text/Excel_Equip/OuterArmor": fx.names("OuterArmor", {g(20): ("기사단모자", ""),
                                                                          g(21): ("기사단모자(여)", "")}),
    }
    present = [thumb_tex("ch03", n) for n in ("ch03_00_005_1_0", "ch03_00_005_1_1", "ch03_00_005_0_1")] + \
        [thumb_tex("ch02", "ch02_00_061_0_4"), N + "GUI/ui_texture/elsewhere/tex_guild" + TEX]
    return {"users": files, "msgs": texts, "present": present,
            "raw": {  # this thumbnail's texture is not where its name says: found through the prefab's resources
                N + thumb_prefab("ch02", "ch02_00_155_0_0") + ".18": fx.pfb_file("GUI/ui_texture/elsewhere/tex_guild.tex")}}


def weapon_files() -> dict:
    g = fx.guid
    users, texts = {}, {}
    for t, (file, _ko, _en) in enumerate(WEAPON_TYPES):
        users[f"GameDesign/Common/Weapon/{file}"] = fx.app_table("WeaponData", [])
        users[f"GameDesign/Common/Weapon/Outer{file}Data"] = fx.app_table("OuterWeaponData", [])
        users[f"GameDesign/Common/Weapon/{file}Tree"] = {fx.TYPE: "app.user_data.WeaponTree"}
        texts[f"GameDesign/Text/Excel_Equip/{file}"] = fx.names(file, {})
        texts[f"GameDesign/Text/Excel_Equip/Outer{file}"] = fx.names("Outer" + file, {})
    users["GameDesign/Common/Weapon/TwinSword"] = fx.app_table("WeaponData", [
        {"_Index": 80, "_Type": 12, "_ModelId": 29, "_TwinSword": 84, "_Name": g(31), "_SortId": 107, "_Rare": fx.rare(8)},
        {"_Index": 79, "_Type": 12, "_ModelId": 29, "_TwinSword": 83, "_Name": g(30), "_SortId": 106, "_Rare": fx.rare(6)},
        {"_Index": 0, "_Type": 12, "_ModelId": 28, "_TwinSword": 1, "_Name": g(32), "_SortId": 0, "_Rare": fx.rare(1)},
        {"_Index": 1, "_Type": 12, "_ModelId": 31, "_TwinSword": 89, "_Name": g(33), "_SortId": 200}])
    users["GameDesign/Common/Weapon/OuterTwinSwordData"] = fx.app_table("OuterWeaponData", [
        {"_Index": 0, "_ModelId": 29, "_Name": g(40), "_SortId": 1, "_Rare": 11},  # inline Fixed: rarity 8
        {"_Index": 1, "_ModelId": 2, "_Name": g(41), "_SortId": 2, "_Rare": 11}])
    users["GameDesign/Common/Weapon/TwinSwordTree"] = {
        fx.TYPE: "app.user_data.WeaponTree",
        "_WeaponTreeList": [{fx.TYPE: "app.user_data.WeaponTree.cWeaponTree", "_WeaponID": 83, "_RowDataLevel": 24}],
        "_RowDataList": [{fx.TYPE: "app.user_data.WeaponTree.cRowData", "_Series": 39, "_RowLevel": 24}]}
    texts["GameDesign/Text/Excel_Equip/TwinSword"] = fx.names("TwinSword", {
        g(30): ("새벽쌍검Ⅰ", "Dawn Blades I"), g(31): ("황혼쌍검", "Dusk Blades"),
        g(32): ("첫걸음쌍검Ⅰ", "First Blades I"), g(33): ("<COLOR FF0000>#Rejected#</COLOR> TwinSword_89", "")})
    texts["GameDesign/Text/Excel_Equip/OuterTwinSword"] = fx.names("OuterTwinSword", {g(40): ("새벽쌍검 덧입기", ""),
                                                                                     g(41): ("덧입기 쌍검", "")})
    users["GameDesign/Common/Equip/WeaponSeriesData"] = fx.app_table(
        "WeaponSeriesData", [{"_Series": fx.enum("app.WeaponDef.SERIES_Serializable", DAWN_TREE), "_Name": g(50)}])
    users["GameDesign/Player/EnumMaker/WeaponSeries"] = enum_maker(("ID_039", 39, DAWN_TREE))
    texts["GameDesign/Text/Excel_Equip/WeaponSeries"] = fx.names("WeaponSeries", {g(50): ("새벽 파생", "Dawn Tree")})
    users["GameDesign/GUI/Common/_UserData/WeaponThumbnailTextureData"] = thumbs(
        (2000083, thumb_prefab("it02", "it0200_0083")), (2000084, thumb_prefab("it02", "it0200_0084")),
        (10030005, thumb_prefab("it10", "it1003_0005")))
    # kinsects
    users["GameDesign/Common/Weapon/RodInsectData"] = fx.app_table("RodInsectData", [
        {"_Index": 6, "_Name": g(60), "_ModelID": 30005, "_SortID": 7, "_Rare": fx.rare(4)}])
    texts["GameDesign/Text/Excel_Equip/RodInsect"] = fx.names("RodInsect", {g(60): ("시험벌레", "Test Kinsect")})
    return {"users": users, "msgs": texts,
            "present": [thumb_tex("it02", "it0200_0083"), thumb_tex("it02", "it0200_0084"), thumb_tex("it10", "it1003_0005")]}


def palico_files() -> dict:
    g = fx.guid
    data_id = fx.enum("app.OtEquipDef.EQUIP_DATA_ID_Serializable", 777)
    users = {
        "GameDesign/Otomo/DataParam/OtomoEquipSeriesData": fx.app_table("OtomoEquipSeriesData", [
            {"_Series": data_id, "_Name": g(70), "_SortId": 3, "_Rare": fx.rare(1),
             "_ModelId": fx.enum("app.OtEquipDef.EQUIP_SERIES_ID_Serializable", 1613199232)},  # ch05/004
            {"_Series": fx.enum("app.OtEquipDef.EQUIP_DATA_ID_Serializable", 778), "_Name": g(71),
             "_ModelId": fx.enum("app.OtEquipDef.EQUIP_SERIES_ID_Serializable", 123)},  # not in the table
            {"_Series": fx.enum("app.OtEquipDef.EQUIP_DATA_ID_Serializable", 779), "_Name": g(74),
             "_ModelId": fx.enum("app.OtEquipDef.EQUIP_SERIES_ID_Serializable", 1132691456)}]),  # the enum's MAX
        "GameDesign/Otomo/DataParam/OtomoArmorData": fx.app_table("OtArmorData", [
            {"_Series": data_id, "_EquipCategory": fx.enum("app.OtEquipDef.EQUIP_TYPE_Serializable", 1), "_Name": g(72)}]),
        "GameDesign/Otomo/DataParam/OtomoWeaponData": fx.app_table("OtWeaponData", [
            {"_Series": data_id, "_Name": g(73)}]),
        "GameDesign/Otomo/DataParam/OtomoOuterArmorData": fx.app_table("OtomoOuterArmorData", []),
        "GameDesign/GUI/Common/_UserData/OtEquipThumnailTextureData": thumbs(
            (500410, thumb_prefab("ch05", "ch05_004_10")),
            (507510, thumb_prefab("ch05", "ch05_075_10"))),  # a folder newer than the embedded numbers
    }
    texts = {
        "GameDesign/Text/Excel_Equip/OtomoEquipSeries": fx.names("OtomoEquipSeries", {g(70): ("시험고양이", ""), g(71): ("?", ""),
                                                                                     g(74): ("최대", "")}),
        "GameDesign/Text/Excel_Equip/OtomoArmor": fx.names("OtomoArmor", {g(72): ("시험고양이모자", "")}),
        "GameDesign/Text/Excel_Equip/OtomoWeapon": fx.names("OtomoWeapon", {g(73): ("시험고양이망치", "")}),
        "GameDesign/Text/Excel_Equip/OtomoOuterArmor": fx.names("OtomoOuterArmor", {}),
    }
    return {"users": users, "msgs": texts, "present": [thumb_tex("ch05", "ch05_004_10"), thumb_tex("ch05", "ch05_075_10")]}


def seikret_files() -> dict:
    g = fx.guid
    setting = "GameDesign/Porter/Data/PorterVisualSetting/PT00_00_000_EQUIP_{:03d}_PorterVisualSetting.user"
    texture = "GUI/ui_texture/tex010000/tex_thumbnail/cecletcustomize/cc01/tex_cc01_000_0001_IMLM4.tex"
    kinds = {1: VISUAL_1, 2: VISUAL_1 + 1, 3: VISUAL_1 + 2}
    equip = "GameDesign/Porter/_Prefab/Equip/{0:03d}/{1}_{0:03d}.pfb"
    users = {
        "GameDesign/Facility/SeikretEquipData": fx.app_table("SeikretEquipData", [
            {"_Index": n, "_VisualSettingType": fx.enum(VISUAL, kind), "_Name": g(80 + n)} for n, kind in kinds.items()]),
        "GameDesign/Porter/Data/PorterVisualSettingPack/PT00_00_000_PorterVisualSettingPack": fx.app_table(
            "PorterVisualSettingPack", [
                {"_VisualSettingType": fx.enum(VISUAL, kind),
                 "_PorterVisualSetting": fx.user_ref("app.user_data.PorterVisualSetting", setting.format(n))}
                for n, kind in kinds.items()], field="_DataList"),
        # equipment 3 has no catalog entry: its folder comes from the setting's name
        **{setting.format(n)[:-5]: {fx.TYPE: "app.user_data.PorterVisualSetting", "_EquipSeriesId": 11 + n} for n in kinds},
        "GameDesign/Catalog/04_00/Data/PorterEquipList_04_00": fx.app_table("PorterEquipList", [
            {"_SeriesID": 11 + n, "_PartsPrefabs": [fx.prefab(equip.format(n, "Body")), fx.prefab(equip.format(n, "Saddle")),
                                                    fx.prefab(""), fx.prefab("")]} for n in (1, 2)], field="_DataList"),
        "GameDesign/Common/CharaMake/CharacterEditThumbnailsSeikretEquip": {
            fx.TYPE: "app.user_data.CharacterEditThumbnailsSeikretEquip",
            "_Entries": [{fx.TYPE: ENTRY, "_Key": fx.enum(VISUAL, VISUAL_1),
                          "_Value": {fx.TYPE: "app.user_data.CharacterEditThumbnailTextureData", "Texture": texture}}]},
    }
    shared = "Art/Model/Character/ch07/000/0000/ch07_000_0000.mesh"
    raw = {
        N + equip.format(1, "Body") + ".18": fx.pfb_file("@Art/Model/Character/ch07/901/0010/ch07_901_0010.mesh", shared),
        N + equip.format(1, "Saddle") + ".18": fx.pfb_file("Art/Model/Character/ch07/902/0010/ch07_902_0010.mdf2",
                                                            "Art/Model/Character/ch07/903/0010/ch07_903_0010.mesh"),
        N + equip.format(2, "Body") + ".18": fx.pfb_file(shared, "Art/Model/Character/ch07/903/0010/ch07_903_0010.mdf2"),
    }
    texts = {"GameDesign/Text/Excel_Data/SeikretEquip": fx.names("SeikretEquip", {
        g(81): ("철가면 장비", "Iron Gear"), g(82): ("바람 장비", ""), g(83): ("", "Old Gear")})}
    return {"users": users, "msgs": texts, "raw": raw, "present": [N + texture + ".241106027"]}


def npc_files() -> dict:
    g = fx.guid
    users = {
        "GameDesign/NPC/EnumMaker/NpcID": enum_maker(("NPC102_00_001", 8, 86), ("NPC111_01_003", 20, 4242)),
        "GameDesign/Dialogue/CommonData/NpcNameData": fx.app_table("NpcNameData", [
            {"_NpcID": 86, "_Name": g(90)}, {"_NpcID": 4242, "_Name": g(91)}]),
    }
    empty = {fx.TYPE: "ace.user_data.EnumMaker"}
    raw = {  # the prefab names are what counts: here in the resource table of a small .user file
        NPC_MAIN: fx.user_file(empty, resources=("GameDesign/NPC/_Prefab/Model/Human/ch04_000_0000.pfb",
                                                 "GameDesign/NPC/_Prefab/Model/HumanFace/ch00_500_0000.pfb",
                                                 "GameDesign/NPC/_Prefab/Model/Human/ch04_200_0203.pfb")),
        NPC_LOBBY: fx.user_file(empty, resources=("GameDesign/NPC/_Prefab/Model/Human/ch04_200_0203.pfb",
                                                  "GameDesign/NPC/_Prefab/Model/HumanFace/ch00_900_0000.pfb")),
    }
    texts = {"GameDesign/Text/Excel_Data/NpcName": fx.names("NpcName", {g(90): ("동료", "Partner"), g(91): ("주민", "Villager")})}
    return {"users": users, "msgs": texts, "raw": raw}


def other_files() -> dict:
    g = fx.guid
    ref = fx.msg_file([(g(100 + t), f"RefEquipment_0001_{t:03d}", {msg.KOREAN: f"{ko}(게임)", msg.ENGLISH: en})
                       for t, (_f, ko, en) in enumerate(WEAPON_TYPES) if t != 5] +
                      [(g(120), "RefEquipment_0002_000", {msg.KOREAN: "머리 장비(게임)", msg.ENGLISH: "Head"})])
    enemy = fx.msg_file([
        (g(130), "EnemyText_NAME_INVALID", {}),
        (g(131), "EnemyText_NAME_EM0002_50_0", {msg.KOREAN: "수호 시험룡", msg.ENGLISH: "Guardian Testwyvern"}),
        (g(132), "EnemyText_NAME_EM0002_00_0", {msg.KOREAN: "시험룡", msg.ENGLISH: "Testwyvern"}),
        (g(133), "EnemyText_NAME_EM1150_00_1", {msg.ENGLISH: "Testnoth (Female)"}),
        (g(134), "EnemyText_EXP_EM0002_00_0", {msg.KOREAN: "설명"})])
    return {"msgs": {"GameDesign/Text/Reference/RefEquipment": ref, "GameDesign/Text/Excel_Data/EnemyText": enemy}}


_PARTS = None


def game_parts() -> list[tuple[dict, list]]:
    global _PARTS
    if _PARTS is None:  # written once: the registry is large
        reg = fx.registry()
        parts = []
        for part_ in (armor_files(), weapon_files(), palico_files(), seikret_files(), npc_files(), other_files()):
            files = {N + rel + ".user.3": fx.user_file(root, reg) for rel, root in part_.get("users", {}).items()}
            files.update({N + rel + ".msg.23": data for rel, data in part_.get("msgs", {}).items()})
            files.update(part_.get("raw", {}))
            parts.append((files, part_.get("present", [])))
        _PARTS = parts
    return _PARTS


def make_game(drop=(), replace=None) -> fx.FakeGame:
    game = fx.FakeGame()
    for files, present in game_parts():
        for path, data in files.items():
            game.add(path, data)
        for path in present:
            game.add(path, b"")
    for path in drop:
        game.remove(path)
    for path, data in (replace or {}).items():
        game.add(path, data)
    return game


def build(game=None, **kw) -> Catalog:
    return Catalog(catalog.build(game or make_game(), fx.registry(), npc_settings=(NPC_MAIN.lower(), NPC_LOBBY), **kw))


@pytest.fixture(scope="module")
def cat():
    return build()


# ---- userdata -------------------------------------------------------------------------------
def test_user_files_read_back_as_python_values():
    shared = fx.enum("app.ArmorDef.MODEL_VARIETY_Serializable", 1)
    root = fx.app_table("ArmorSeriesData", [
        {"_Index": 1, "_Series": series(-1830990336), "_Name": fx.guid(1), "_ModelVariety": shared, "_ModId": 4,
         "_ModSubFemaleId": 1, "_SortId": 4000000000, "_Rare": fx.rare(5), "_IsOneSet": True},
        {"_Index": 2, "_Series": series(7), "_ModelVariety": shared}])
    got = read_user(fx.user_file(root), fx.registry())
    assert got[fx.TYPE] == "app.user_data.ArmorSeriesData"
    first, second = got["_Values"]
    assert first[fx.TYPE] == "app.user_data.ArmorSeriesData.cData"
    assert first["_Series"] == -1830990336 and first["_ModelVariety"] == 1 and first["_Rare"] == 14  # wrappers collapsed
    assert first["_Name"] == fx.guid(1) and second["_Name"] == bytes(16)
    assert (first["_ModId"], first["_ModSubMaleId"], first["_ModSubFemaleId"], first["_SortId"]) == (4, 0, 1, 4000000000)
    assert first["_IsOneSet"] is True and second["_IsOneSet"] is False
    assert first["_Color"] is None  # a null reference
    assert rows(got) == [first, second] == read_rows(fx.user_file(root), fx.registry())


def test_user_files_with_references_arrays_and_structs():
    reg = fx.registry()
    package = {fx.TYPE: "app.cArmorSetPackage",
               "_PartsPrefabs": [fx.prefab("GameDesign/Equip/_Prefab/Armor/Male/004/000/Helm/ch02_004_0003.pfb"), None],
               "_VisualParam": fx.user_ref("app.user_data.PlayerArmorVisualParam", "GameDesign/X/004_000_avp.user")}
    root = {fx.TYPE: "app.user_data.PlayerArmorList",
            "_DataList": [{fx.TYPE: "app.user_data.PlayerArmorList.cData", "_ArmorID": 4, "_ArmorPackage": package},
                          {fx.TYPE: "app.user_data.PlayerArmorList.cData", "_ArmorID": 5, "_ArmorPackage": package}]}
    got = read_user(fx.user_file(root), reg)
    a, b = rows(got, "_DataList")
    assert a["_ArmorPackage"] is b["_ArmorPackage"]  # one instance, referenced twice
    prefabs = a["_ArmorPackage"]["_PartsPrefabs"]
    assert prefabs[0] == {fx.TYPE: "via.Prefab", "Standby": False,
                          "Path": "GameDesign/Equip/_Prefab/Armor/Male/004/000/Helm/ch02_004_0003.pfb"}
    assert prefabs[1] is None
    assert a["_ArmorPackage"]["_VisualParam"] == {fx.TYPE: "app.user_data.PlayerArmorVisualParam",
                                                  "$user": "GameDesign/X/004_000_avp.user"}
    assert read_user(fx.user_file(got, reg), reg) == got  # what read_user gives, the fixtures write back
    # strings (a resource's "@" dropped), and arrays of embedded structs
    tex = read_user(fx.user_file({fx.TYPE: "app.user_data.CharacterEditThumbnailTextureData", "Texture": "@GUI/a.tex",
                                  "MissionID": -1}), reg)
    assert tex["Texture"] == "GUI/a.tex" and tex["MissionID"] == -1
    dyn = "ace.DYNAMIC_ARRAY`1<app.cAnimalContextCreator_ExEvent.CREATION_INFO>"
    got = read_user(fx.user_file({fx.TYPE: dyn, "_Count": 2, "_Array": [{"GraphDataIndex": 3, "PointDataIndex": 4},
                                                                      {"GraphDataIndex": 5}]}), reg)
    assert got["_Count"] == 2
    assert [(e[fx.TYPE], e["GraphDataIndex"], e["PointDataIndex"]) for e in got["_Array"]] == \
        [("app.cAnimalContextCreator_ExEvent.CREATION_INFO", 3, 4), ("app.cAnimalContextCreator_ExEvent.CREATION_INFO", 5, 0)]


def test_cyclic_references_read_safely():
    reg = fx.registry()
    # instances [null, row, root]: the row points back at the root
    root = {fx.TYPE: "app.user_data.PlayerArmorList",
            "_DataList": [{fx.TYPE: "app.user_data.PlayerArmorList.cData", "_ArmorID": 4, "_ArmorPackage": 2}]}
    got = read_user(fx.user_file(root, reg), reg)
    (row,) = rows(got, "_DataList")
    assert row["_ArmorPackage"] is got and row["_ArmorID"] == 4


def test_files_that_do_not_read_exactly_give_none():
    reg = fx.registry()
    good = fx.user_file(enum_maker(("A", 1, 2)), reg)
    assert read_user(good, reg) is not None
    for bad in (b"", b"USR\0" + bytes(60), b"not an rsz file at all", good[:-3], good + b"\0" * 4):
        assert read_user(bad, reg) is None
    assert read_rows(b"junk", reg) is None
    assert rows(None) == [] and rows({"_Values": "x"}) == [] and rows({"_DataList": [None, {"a": 1}]}) == [{"a": 1}]


# ---- the catalog ---------------------------------------------------------------------------
def test_a_complete_game_has_no_problems(cat):
    assert cat.problems == []
    assert {k: cat.count(k) for k in ("armor", "weapon", "kinsect", "palico", "seikret", "npc", "monster")} == \
        {"armor": 5, "weapon": 3, "kinsect": 1, "palico": 2, "seikret": 3, "npc": 4, "monster": 2}


def test_armor_both_designs_and_bodies(cat):
    for body in "mf":
        for design, key in ((1, f"{body}/004/000"), (2, f"{body}/004/001")):
            entry = cat.entry(A(key))
            assert cat.names(A(key)) == ["시험", "Trial α"]  # sort id order; English where Korean is missing
            assert (entry["design"], entry["single_design"], cat.rarity(A(key))) == (design, False, 1)
            assert entry["pieces"] == {"helm": "시험투구", "body": "시험갑옷", "leg": "시험장화α"}
    assert cat.name(A("f/004/001", "helm")) == "시험투구" and cat.name(A("f/004/001", "arm")) == "시험"
    assert cat.name(A("f/004/001")) == "시험"
    # thumbnails: body B, series' plain value, G = type 2 -> 1, part
    assert cat.thumb(A("f/004/001", "helm")) == thumb_tex("ch03", "ch03_00_005_1_0")
    assert cat.thumb(A("f/004/001")) == thumb_tex("ch03", "ch03_00_005_1_1")  # the item's: its body piece
    assert cat.thumb(A("f/004/001", "leg")) == thumb_tex("ch03", "ch03_00_005_1_1")  # no picture of its own
    assert cat.thumb(A("f/004/000")) == thumb_tex("ch03", "ch03_00_005_0_1")
    assert cat.entry(A("m/004/000"))["thumbs"] == {"leg": thumb_tex("ch02", "ch02_00_061_0_4")}  # the alpha's
    assert cat.thumb(A("m/004/001")) is None
    assert cat.entry(A("m/000/000")) is None  # the series NONE


def test_armor_single_design_layered_only_and_one_body(cat):
    entry = cat.entry(A("m/060/000"))
    assert cat.names(A("m/060/000")) == ["기사단"] and entry["design"] == 1 and entry["single_design"]
    assert entry["pieces"] == {"helm": "기사단모자"} and cat.rarity(A("m/060/000")) == 5
    assert cat.thumb(A("m/060/000", "helm")) == N + "GUI/ui_texture/elsewhere/tex_guild" + TEX  # from the GUI prefab
    assert cat.entry(A("f/060/000")) is None  # the catalog lists no female prefab
    assert cat.entry(A("m/060/001")) is None


def test_weapons_share_models_and_layered_ones_follow(cat):
    key = W("02/000029", "main")
    assert cat.names(key) == ["새벽쌍검Ⅰ", "황혼쌍검", "새벽쌍검 덧입기"]
    assert cat.name(key) == "새벽쌍검Ⅰ" and cat.rarity(key) == 6
    assert cat.thumb(key) == thumb_tex("it02", "it0200_0083") and cat.thumb(W("02/000029")) == cat.thumb(key)
    assert cat.entry(key)["tree"] == "새벽 파생"
    assert cat.names(W("02/000028")) == ["첫걸음쌍검Ⅰ"] and cat.thumb(W("02/000028")) is None
    assert cat.names(W("02/000002")) == ["덧입기 쌍검"] and cat.rarity(W("02/000002")) == 8
    assert cat.entry(W("02/000031")) is None  # only a #Rejected# name and no picture
    assert cat.weapon_type_name(2) == "쌍검(게임)"
    assert cat.weapon_type_name(5) == "수렵피리"  # not in the game's text: the built-in label
    assert cat.part_label("helm") == "머리 장비(게임)" and cat.part_label("slinger") == "슬링어"


def test_kinsect_palico_and_monsters(cat):
    kinsect = Target("kinsect", "0005")
    assert cat.names(kinsect) == ["시험벌레"] and cat.rarity(kinsect) == 4
    assert cat.thumb(kinsect) == thumb_tex("it10", "it1003_0005")
    palico = Target("palico", "004", "helm")
    assert cat.names(palico) == ["시험고양이"] and cat.name(palico) == "시험고양이모자" and cat.rarity(palico) == 1
    assert cat.entry(palico)["pieces"] == {"helm": "시험고양이모자", "weapon": "시험고양이망치"}
    assert cat.thumb(palico) == thumb_tex("ch05", "ch05_004_10")
    assert cat.thumb(Target("palico", "004", "body")) == thumb_tex("ch05", "ch05_004_10")  # the item's
    newer = Target("palico", "075", "helm")  # no name known, but the game's picture
    assert cat.names(newer) == [] and cat.thumb(newer) == thumb_tex("ch05", "ch05_075_10")
    assert cat.entry(Target("palico", "507")) is None  # the enum's MAX is no folder
    assert cat.names(Target("monster", "em0002")) == ["시험룡", "수호 시험룡"]
    assert cat.names(Target("monster", "em1150")) == ["Testnoth (Female)"]


def test_seikret_models_resolve_to_their_equipment(cat):
    equip = Target("seikret", "equip/001", "body")
    assert cat.names(equip) == ["철가면 장비"]
    assert cat.thumb(equip) == N + "GUI/ui_texture/tex010000/tex_thumbnail/cecletcustomize/cc01/tex_cc01_000_0001_IMLM4" + TEX
    assert cat.names(Target("seikret", "equip/002")) == ["바람 장비"] and cat.thumb(Target("seikret", "equip/002")) is None
    assert cat.names(Target("seikret", "equip/003")) == ["Old Gear"]
    assert cat.resolve(Target("seikret", "model/901/0010")) == Target("seikret", "equip/001", "body")
    assert cat.resolve(Target("seikret", "model/902/0010")) == Target("seikret", "equip/001", "saddle")
    assert cat.names(Target("seikret", "model/901/0010")) == ["철가면 장비"]
    for shared in ("model/000/0000", "model/903/0010"):  # used by two pieces of equipment
        assert cat.resolve(Target("seikret", shared)) == Target("seikret", shared)
        assert cat.entry(Target("seikret", shared)) is None


def test_npc_models_named_main_cast_first(cat):
    assert cat.names(Target("npc", "000_0000")) == ["동료"]
    assert cat.names(Target("npc", "200_0203")) == ["동료", "주민"]
    assert cat.entry(Target("npc", "200_0203"))["npcs"] == 2
    # a face worn with one body only stands for it; the partner's face goes with two bodies
    assert cat.resolve(Target("npc", "face/900")) == Target("npc", "200_0203")
    assert cat.names(Target("npc", "face/900")) == ["동료", "주민"]
    assert cat.data["items"]["npc"]["face/900"]["names"] == ["주민"]
    assert cat.resolve(Target("npc", "face/500")) == Target("npc", "face/500")
    assert cat.names(Target("npc", "face/500")) == ["동료"]


def test_a_face_keeps_its_names_when_its_body_has_none():
    reg = fx.registry()
    g = fx.guid
    empty = {fx.TYPE: "ace.user_data.EnumMaker"}
    named = N + "GameDesign/NPC/Character/Main/NPC120_00_001/Data/NPC120_00_001_VisualSetting.user.3"
    unnamed = N + "GameDesign/NPC/Character/Lobby/NPC121_00_001/Data/NPC121_00_001_VisualSetting.user.3"
    face = "GameDesign/NPC/_Prefab/Model/HumanFace/ch00_530_0000.pfb"
    game = fx.FakeGame({
        N + "GameDesign/NPC/EnumMaker/NpcID.user.3": fx.user_file(enum_maker(("NPC120_00_001", 1, 501)), reg),
        N + "GameDesign/Dialogue/CommonData/NpcNameData.user.3":
            fx.user_file(fx.app_table("NpcNameData", [{"_NpcID": 501, "_Name": g(1)}]), reg),
        N + "GameDesign/Text/Excel_Data/NpcName.msg.23": fx.names("NpcName", {g(1): ("안내인", "Guide")}),
        named: fx.user_file(empty, reg, resources=(face,)),
        unnamed: fx.user_file(empty, reg, resources=("GameDesign/NPC/_Prefab/Model/Human/ch04_300_0100.pfb", face)),
    })
    cat = Catalog(catalog.build(game, reg, npc_settings=(named, unnamed)))
    assert cat.entry(Target("npc", "300_0100")) is None
    assert cat.resolve(Target("npc", "face/530")) == Target("npc", "face/530")
    assert cat.names(Target("npc", "face/530")) == ["안내인"]
    assert any(p.startswith("방어구:") for p in cat.problems)  # the rest of this game is missing


def test_missing_tables_are_problems_and_other_sections_stay():
    game = make_game(drop=[N + "GameDesign/Common/Equip/ArmorSeriesData.user.3",
                           N + "GameDesign/GUI/Common/_UserData/WeaponThumbnailTextureData.user.3"],
                     replace={N + "GameDesign/Text/Excel_Data/EnemyText.msg.23": b"GMSG broken"})
    cat = build(game)
    assert cat.count("armor") == 0 and cat.count("monster") == 0
    assert any(p.startswith("방어구:") and "ArmorSeriesData.user.3" in p for p in cat.problems)
    assert any(p.startswith("몬스터:") and "EnemyText.msg.23" in p for p in cat.problems)
    assert any(p.startswith("무기:") and "WeaponThumbnailTextureData.user.3" in p for p in cat.problems)
    assert cat.names(W("02/000029"))[0] == "새벽쌍검Ⅰ" and cat.thumb(W("02/000029")) is None
    assert cat.names(Target("palico", "004")) == ["시험고양이"]


def test_without_the_armor_list_both_bodies_are_assumed():
    cat = build(make_game(drop=[N + "GameDesign/Catalog/00_00/Data/PlayerArmorList.user.3"]))
    assert cat.names(A("f/060/000")) == ["기사단"] and cat.names(A("m/060/000")) == ["기사단"]
    assert cat.problems == []


def test_unreadable_tables_and_a_crashing_section(monkeypatch):
    def boom(self):
        raise RuntimeError("bug")

    monkeypatch.setattr(catalog._Builder, "_palico", boom)
    game = make_game(replace={N + "GameDesign/Common/Equip/ArmorData.user.3": b"USR\0 not really"})
    cat = build(game)
    assert "오토모 장비: 정보를 읽는 중 오류가 났습니다" in cat.problems
    assert any(p.startswith("방어구:") and "ArmorData.user.3" in p for p in cat.problems)
    assert cat.names(A("f/004/001")) == ["시험", "Trial α"] and cat.entry(A("f/004/001"))["pieces"] == {}
    assert cat.count("palico") == 0 and cat.count("weapon") == 3


def test_reading_errors_from_the_game_are_contained():
    class Flaky(fx.FakeGame):
        def read(self, path):
            if "OuterArmorData" in path:
                raise OSError("disk gone")
            return super().read(path)

    flaky = Flaky()
    flaky.files = make_game().files
    cat = build(flaky)
    assert any("OuterArmorData.user.3" in p for p in cat.problems)
    assert cat.names(A("m/060/000")) == ["기사단"] and cat.entry(A("m/060/000"))["pieces"] == {}


def test_progress_and_languages():
    calls = []
    data = catalog.build(make_game(), fx.registry(), lambda done, total, message: calls.append((done, total, message)),
                         languages=(msg.ENGLISH,))
    assert calls and all(total == calls[0][1] for _d, total, _m in calls)
    assert [d for d, _t, _m in calls] == sorted(d for d, _t, _m in calls) and calls[-1][0] == calls[-1][1]
    cat = Catalog(data)
    assert cat.names(A("f/004/001")) == ["Trial", "Trial α"] and cat.weapon_type_name(2) == "Dual Blades"
    assert data["languages"] == [msg.ENGLISH]


def test_json_round_trip(cat):
    text = cat.to_json()
    json.loads(text)
    again = Catalog.from_json(text)
    for target in (A("f/004/001", "helm"), W("02/000029"), Target("seikret", "model/901/0010"), Target("npc", "face/900")):
        assert (again.resolve(target), again.names(target), again.name(target), again.thumb(target), again.rarity(target)) == \
            (cat.resolve(target), cat.names(target), cat.name(target), cat.thumb(target), cat.rarity(target))
    assert again.weapon_type_name(2) == cat.weapon_type_name(2) and again.problems == cat.problems
    for bad in ("", "{", "[]", json.dumps({"format": catalog.CATALOG_FORMAT + 1, "items": {}}), json.dumps({"format": 1})):
        with pytest.raises(EquipError):
            Catalog.from_json(bad)


def test_an_empty_or_damaged_catalog_answers_safely():
    for data in ({}, {"items": {"armor": {"m/004/000": "junk"}, "weapon": []}, "aliases": {"seikret": {"x": [1, 2]}},
                      "labels": {"weapon_types": {"2": 5}}, "problems": [3, "note"]}):
        cat = Catalog(data)
        for target in (A("m/004/000", "helm"), W("02/000029"), Target("seikret", "x")):
            assert cat.entry(target) is None and cat.names(target) == [] and cat.name(target) == ""
            assert cat.thumb(target) is None and cat.rarity(target) is None and cat.resolve(target) == target
        assert cat.weapon_type_name(2) == "쌍검" and cat.part_label("helm") == "머리"
    assert Catalog({"problems": [3, "note"]}).problems == ["note"]
