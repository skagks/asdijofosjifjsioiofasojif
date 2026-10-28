"""Synthetic files for the 모드 수정 tests (no Capcom data).

RSZ data files are written with a small made-up type registry laid out like the game's tables: an
ItemData-like table (rows with ids, a GUID, ints, a bool, a float and an int array), a reward-like
table whose ids are enum wrapper instances (``*_Serializable``), a table of embedded structs, a
table with no id-like field (its rows are named by a value column, like NpcPartnerStatusParam), one
field of every kind, an instance shared by two fields, a file with two roots, one with a RuntimeType
field, and an ItemData variant written with an older layout of its row type (a stale mod).  PAKs
holding them are written with matfix.pak.write_pak.  Settings samples cover Lua, JSON, INI and TOML.

Instances are dicts as in tests/equip_game_fixtures.py ({"$type": name, field: value}); vector and
shape fields take their bytes (``vec``)."""

from __future__ import annotations

import json
import math
import struct
import zlib
from pathlib import Path

import equip_game_fixtures as fx
from mhwmm.matfix import rsz
from mhwmm.matfix.pak import path_hash, write_pak

TYPE, USER = fx.TYPE, fx.USER

ITEM, ITEM_ROW = "app.user_data.TestItemData", "app.user_data.TestItemData.cData"
REWARD, REWARD_ROW = "app.user_data.TestRewardData", "app.user_data.TestRewardData.cData"
ID_WRAPPER, KIND_WRAPPER = "app.TestDef.ID_Serializable", "app.TestDef.KIND_Serializable"
AREAS, AREA = "app.user_data.TestAreaTable", "app.TestArea"
KINDS, PARAM, SHARED = "app.user_data.TestKinds", "app.TestParam", "app.user_data.TestShared"
RUNTIME = "app.user_data.TestRuntime"
STATUS, STATUS_ROW = "app.user_data.TestStatusParam", "app.user_data.TestStatusParam.cData"

ROW_CRC, OLD_ROW_CRC = 0x5A000001, 0x5A0000F0
ITEM_ROW_FIELDS = [["_Index", "S32", 4, 4, 0], ["_ItemId", "S32", 4, 4, 0], ["_RawName", "Guid", 16, 8, 0],
                   ["_SortId", "S16", 2, 2, 0], ["_Type", "S32", 4, 4, 0], ["_Rare", "S32", 4, 4, 0],
                   ["_MaxCount", "S16", 2, 2, 0], ["_SellPrice", "S32", 4, 4, 0], ["_BuyPrice", "S32", 4, 4, 0],
                   ["_Eatable", "Bool", 1, 1, 0], ["_Weight", "F32", 4, 4, 0], ["_GetRank", "S32", 4, 4, 1]]
OLD_ITEM_ROW_FIELDS = [f for f in ITEM_ROW_FIELDS if f[0] != "_Weight"]  # before an update added _Weight
REWARD_ROW_FIELDS = [["_Index", "S32", 4, 4, 0], ["_dataId", "S32", 4, 4, 0], ["_rewardType", "Object", 4, 4, 0],
                     ["_partsIndex", "S32", 4, 4, 0], ["_IdStory", "Object", 4, 4, 0],
                     ["_probabilityStory", "S32", 4, 4, 0], ["_IdEx", "Object", 4, 4, 1],
                     ["_probabilityEx", "S32", 4, 4, 1]]
AREA_FIELDS = [["_Name", "String", 4, 4, 0], ["_Min", "F32", 4, 4, 0], ["_Max", "F32", 4, 4, 0],
               ["_Ids", "S32", 4, 4, 1]]
AREAS_FIELDS = [["_Areas", f"Struct:{AREA}", 1, 4, 1], ["_Scale", "F32", 4, 4, 0], ["_Center", "Vec3", 16, 16, 0],
                ["_Tint", "Color", 4, 4, 0], ["_Span", "Range", 8, 4, 0]]
PARAM_FIELDS = [["_Scale", "F32", 4, 4, 0], ["_Count", "S32", 4, 4, 0]]
STATUS_ROW_FIELDS = [["_MaxHealth", "S32", 4, 4, 0], ["_Defence", "S32", 4, 4, 0], ["_Speed", "F32", 4, 4, 0]]
# one field of every kind: (name, kind, size, align)
KIND_LAYOUT = [
    ("_S8", "S8", 1, 1), ("_U8", "U8", 1, 1), ("_S16", "S16", 2, 2), ("_U16", "U16", 2, 2), ("_S32", "S32", 4, 4),
    ("_U32", "U32", 4, 4), ("_S64", "S64", 8, 8), ("_U64", "U64", 8, 8), ("_F16", "F16", 2, 2), ("_F32", "F32", 4, 4),
    ("_F64", "F64", 8, 8), ("_Bool", "Bool", 1, 1), ("_Vec2", "Vec2", 8, 4), ("_Vec3", "Vec3", 16, 16),
    ("_Vec4", "Vec4", 16, 16), ("_Float2", "Float2", 8, 4), ("_Float3", "Float3", 12, 4), ("_Float4", "Float4", 16, 16),
    ("_Int2", "Int2", 8, 4), ("_Int3", "Int3", 12, 4), ("_Int4", "Int4", 16, 16), ("_Uint2", "Uint2", 8, 4),
    ("_Uint3", "Uint3", 12, 4), ("_Uint4", "Uint4", 16, 16), ("_Quat", " Quaternion", 16, 16), ("_Range", "Range", 8, 4),
    ("_RangeI", "RangeI", 8, 4), ("_Size", "Size", 8, 4), ("_Point", "Point", 8, 4), ("_Position", "Position", 24, 8),
    ("_Half4", "Half4", 8, 2), ("_Color", "Color", 4, 4),
    ("_C8", "C8", 1, 1), ("_C16", "C16", 2, 2), ("_Text", "String", 4, 4), ("_Path", "Resource", 4, 4),
    ("_Guid", "Guid", 16, 8), ("_Mat", "Mat4", 64, 16), ("_When", "DateTime", 8, 8),
    ("_GameObject", "GameObjectRef", 16, 8), ("_Data", "Data", 8, 8), ("_Box", "AABB", 32, 16),
    ("_Pos", "F32", 4, 4), ("_Pos", "F32", 4, 4),  # a name used twice: the second is "_Pos~1"
    ("_Param", "Object", 4, 4), ("_Common", "UserData", 4, 4),
]
KIND_FIELDS = [[name, kind, size, align, 0] for name, kind, size, align in KIND_LAYOUT]

_TYPES = {  # type id → (name, crc, fields)
    0xA0000001: (ITEM, 0x5A000000, [["_Values", "Object", 4, 4, 1]]),
    0xA0000002: (ITEM_ROW, ROW_CRC, ITEM_ROW_FIELDS),
    0xA0000011: (REWARD, 0x5A000010, [["_Values", "Object", 4, 4, 1]]),
    0xA0000012: (REWARD_ROW, 0x5A000012, REWARD_ROW_FIELDS),
    0xA0000013: (ID_WRAPPER, 0x5A000013, [["_Value", "S32", 4, 4, 0]]),
    0xA0000014: (KIND_WRAPPER, 0x5A000014, [["_Value", "S32", 4, 4, 0]]),
    0xA0000021: (AREAS, 0x5A000021, AREAS_FIELDS),
    0xA0000022: (AREA, 0x5A000022, AREA_FIELDS),
    0xA0000031: (KINDS, 0x5A000031, KIND_FIELDS),
    0xA0000032: (PARAM, 0x5A000032, PARAM_FIELDS),
    0xA0000041: (SHARED, 0x5A000041, [["_A", "Object", 4, 4, 0], ["_B", "Object", 4, 4, 0],
                                      ["_Own", "Object", 4, 4, 0]]),
    0xA0000051: (RUNTIME, 0x5A000051, [["_Count", "S32", 4, 4, 0], ["_Kind", "RuntimeType", 4, 4, 0]]),
    0xA0000061: (STATUS, 0x5A000061, [["_DataList", "Object", 4, 4, 1]]),
    0xA0000062: (STATUS_ROW, 0x5A000062, STATUS_ROW_FIELDS),
}
ROW_TYPE = 0xA0000002
_REGISTRIES: dict[str, rsz.TypeRegistry] = {}


def _make(old: bool) -> rsz.TypeRegistry:
    types = {format(t, "x"): [format(crc, "x"), name, json.dumps(fields)] for t, (name, crc, fields) in _TYPES.items()}
    history = {format(ROW_TYPE, "x"): {format(OLD_ROW_CRC, "x"): json.dumps(OLD_ITEM_ROW_FIELDS)}}
    if old:
        types[format(ROW_TYPE, "x")] = [format(OLD_ROW_CRC, "x"), ITEM_ROW, json.dumps(OLD_ITEM_ROW_FIELDS)]
        history = {}
    return rsz.TypeRegistry({"meta": {"reasy_date": "2026-01-01"}, "types": types, "empty": {}, "alt": {},
                             "old": history})


def registry() -> rsz.TypeRegistry:
    """The made-up registry (one object for the whole run: the writer caches type ids per registry)."""
    if "current" not in _REGISTRIES:
        _REGISTRIES["current"] = _make(False)
    return _REGISTRIES["current"]


def old_registry() -> rsz.TypeRegistry:
    """The same types as an older game version had them: the item row without ``_Weight``."""
    if "old" not in _REGISTRIES:
        _REGISTRIES["old"] = _make(True)
    return _REGISTRIES["old"]


# ---- values ------------------------------------------------------------------------------------------
def vec(fmt: str, *values, size: int | None = None) -> bytes:
    """A vector or shape field's bytes: ``values`` packed with ``fmt``, padded to ``size``."""
    head = struct.pack(fmt, *values)
    return head + bytes((size or len(head)) - len(head))


def wrapper(value: int, kind: str = ID_WRAPPER) -> dict:
    return {TYPE: kind, "_Value": value}


def f32(value: float) -> float:
    """``value`` as float32 holds it."""
    return struct.unpack("<f", struct.pack("<f", value))[0]


# ---- files -------------------------------------------------------------------------------------------
def _align(n: int, a: int = 16) -> int:
    return n + (-n) % a


def user_file(roots, reg: rsz.TypeRegistry | None = None, resources=()) -> bytes:
    """A .user file whose object table holds ``roots`` (one dict, or a list of them), laid out like
    tests/equip_game_fixtures.user_file (which takes one root)."""
    reg = reg or registry()
    roots = [roots] if isinstance(roots, dict) else list(roots)
    w = fx._Writer(reg)
    indexes = [w.add(r) for r in roots]
    res_strings = [r.encode("utf-16-le") + b"\0\0" for r in resources]
    ud_strings = [p.encode("utf-16-le") + b"\0\0" for _i, _t, p in w.userdata]
    res_table = 48
    ud_table = res_table + 8 * len(resources)
    pos = ud_table + 16 * len(w.userdata)
    res_offsets, ud_offsets = [], []
    for s in res_strings:
        res_offsets.append(pos)
        pos += len(s)
    for s in ud_strings:
        ud_offsets.append(pos)
        pos += len(s)
    rsz_off = _align(pos)
    head = bytearray(struct.pack("<4siiiQQQQ", b"USR\0", len(resources), len(w.userdata), 0, res_table, ud_table,
                                 rsz_off, 0))
    for off in res_offsets:
        head += struct.pack("<Q", off)
    for (_i, tid, _p), off in zip(w.userdata, ud_offsets):
        head += struct.pack("<IIQ", tid, 0, off)
    head += b"".join(res_strings) + b"".join(ud_strings)
    head = head.ljust(rsz_off, b"\0")

    inst_off = 48 + 4 * len(indexes)
    ud_off = _align(rsz_off + inst_off + 8 * len(w.insts)) - rsz_off
    strings_off = ud_off + 16 * len(w.userdata)
    block = bytearray(struct.pack("<4sIIIII", b"RSZ\0", 16, len(indexes), len(w.insts), len(w.userdata), 0))
    block += bytes(24)
    block += struct.pack(f"<{len(indexes)}i", *indexes)
    for tid, crc, _f, _v in w.insts:
        block += struct.pack("<II", tid, crc)
    block = block.ljust(ud_off, b"\0")
    pos = strings_off
    table, strings = bytearray(), bytearray()
    for (idx, tid, _p), s in zip(w.userdata, ud_strings):
        table += struct.pack("<iIQ", idx, tid, pos)
        strings += s
        pos += len(s)
    block += table + strings
    data_off = _align(rsz_off + len(block)) - rsz_off
    block = block.ljust(data_off, b"\0")
    struct.pack_into("<QQQ", block, 24, inst_off, data_off, ud_off)
    data = bytearray()
    for _tid, _crc, fields, values in w.insts:
        if fields is not None:
            rsz.write_values(data, rsz_off + data_off, fields, values, rsz.Schema(), reg)
    return bytes(head) + bytes(block) + bytes(data)


def item_row(i: int, **override) -> dict:
    row = {TYPE: ITEM_ROW, "_Index": i, "_ItemId": 100 + i, "_RawName": fx.guid(i), "_SortId": i, "_Type": 1,
           "_Rare": 18 - (i % 3), "_MaxCount": 10, "_SellPrice": 30 * (i + 1), "_BuyPrice": 60 * (i + 1),
           "_Eatable": i % 2 == 0, "_Weight": 0.1 * (i + 1), "_GetRank": [1, 2] if i % 2 else [3]}
    row.update(override)
    return row


def item_data(n: int = 3, rows=None, reg: rsz.TypeRegistry | None = None) -> bytes:
    """An ItemData-like table: n rows (``item_row``), or the given ones."""
    rows = [item_row(i) for i in range(n)] if rows is None else rows
    return user_file({TYPE: ITEM, "_Values": rows}, reg)


def stale_item_data(n: int = 3) -> bytes:
    """item_data written for an older game version: its row type has the old CRC and layout (no
    ``_Weight``), so it reads only with the layout in the registry's history."""
    rows = []
    for i in range(n):
        row = item_row(i)
        row.pop("_Weight")
        rows.append(row)
    return user_file({TYPE: ITEM, "_Values": rows}, old_registry())


def reward_row(i: int, **override) -> dict:
    row = {TYPE: REWARD_ROW, "_Index": i, "_dataId": 7000 + i // 2, "_rewardType": wrapper(i % 2 + 1, KIND_WRAPPER),
           "_partsIndex": i % 2, "_IdStory": wrapper(200 + i), "_probabilityStory": 25,
           "_IdEx": [wrapper(300 + i), wrapper(400 + i)], "_probabilityEx": [50, 10]}
    row.update(override)
    return row


def reward_data(n: int = 4, rows=None) -> bytes:
    """A reward-like table: two rows per ``_dataId`` told apart by ``_partsIndex``; ids are enum
    wrappers."""
    rows = [reward_row(i) for i in range(n)] if rows is None else rows
    return user_file({TYPE: REWARD, "_Values": rows})


def status_data(rows) -> bytes:
    """A table whose rows have no id-like field, no ``_Index`` and nothing fixed: ``rows`` are
    (_MaxHealth, _Defence[, _Speed]) tuples, so the first unique int column names them."""
    return user_file({TYPE: STATUS, "_DataList": [
        {TYPE: STATUS_ROW, "_MaxHealth": r[0], "_Defence": r[1], "_Speed": r[2] if len(r) > 2 else 1.0}
        for r in rows]})


def area(name: str, low: float, high: float, ids=()) -> dict:
    return {TYPE: AREA, "_Name": name, "_Min": low, "_Max": high, "_Ids": list(ids)}


def area_table(areas=None, **override) -> bytes:
    """A table of embedded structs (``Struct:app.TestArea``) beside a Vec3, a Color and a Range."""
    areas = [area("north", 1.0, 2.0, (1, 2)), area("south", 3.0, 4.0)] if areas is None else areas
    root = {TYPE: AREAS, "_Areas": areas, "_Scale": 1.5, "_Center": vec("<3f", 1.0, 2.0, 3.0, size=16),
            "_Tint": bytes([255, 128, 0, 255]), "_Span": vec("<2f", 0.25, 0.75)}
    root.update(override)
    return user_file(root)


def kinds_root(**override) -> dict:
    """One field of every kind (KIND_LAYOUT), with made-up values."""
    root = {TYPE: KINDS, "_S8": -5, "_U8": 200, "_S16": -300, "_U16": 60000, "_S32": -70000, "_U32": 4000000000,
            "_S64": -(1 << 40), "_U64": 1 << 63, "_F16": 0.5, "_F32": 0.1, "_F64": 0.1, "_Bool": True,
            "_Vec2": vec("<2f", 1.0, 2.0), "_Vec3": vec("<3f", 1.0, 2.0, 3.0) + b"\xAB\xCD\xEF\x01",
            "_Vec4": vec("<4f", 1, 2, 3, 4), "_Float2": vec("<2f", 0.5, 1.5), "_Float3": vec("<3f", 1, 2, 3),
            "_Float4": vec("<4f", 1, 2, 3, 4), "_Int2": vec("<2i", -1, 2), "_Int3": vec("<3i", 6, 30, 0),
            "_Int4": vec("<4i", 1, 2, 3, 4), "_Uint2": vec("<2I", 1, 2), "_Uint3": vec("<3I", 1, 2, 3),
            "_Uint4": vec("<4I", 1, 2, 3, 4), "_Quat": vec("<4f", 0, 0, 0, 1), "_Range": vec("<2f", 0.25, 0.75),
            "_RangeI": vec("<2i", 1, 9), "_Size": vec("<2f", 64, 32), "_Point": vec("<2f", 3, 4),
            "_Position": vec("<3d", 1.5, 2.5, 3.5), "_Half4": vec("<4e", 1, 0.5, 0.25, 2),
            "_Color": bytes([255, 128, 0, 255]), "_C8": 65, "_C16": 0xAC00, "_Text": "hello", "_Path": "@Art/x.tex",
            "_Guid": fx.guid(7), "_Mat": vec("<16f", *range(16)), "_When": vec("<q", 123456789),
            "_GameObject": fx.guid(9), "_Data": bytes(range(8)), "_Box": vec("<8f", *range(8)),
            "_Param": {TYPE: PARAM, "_Scale": 2.0, "_Count": 3},
            "_Common": {TYPE: PARAM, USER: "GameDesign/Test/common.user"}}
    root.update(override)
    return root


def kinds_file(**override) -> bytes:
    """kinds_root as a file.  The second "_Pos" field (``_Pos~1``) is written as 0."""
    return user_file(kinds_root(**override))


def nan_file() -> bytes:
    """A file whose F32 and Vec3 hold NaN / Inf."""
    return kinds_file(_F32=math.nan, _F64=math.inf, _Vec3=vec("<3f", 1.0, math.nan, 3.0, size=16))


def shared_file() -> bytes:
    """``_A`` and ``_B`` point at the same instance (its values are shared); ``_Own`` at its own."""
    common = {TYPE: PARAM, "_Scale": 1.0, "_Count": 1}
    return user_file({TYPE: SHARED, "_A": common, "_B": common, "_Own": {TYPE: PARAM, "_Scale": 2.0, "_Count": 2}})


def two_roots() -> bytes:
    """A file whose object table holds two roots (the second one's values are read-only)."""
    return user_file([{TYPE: PARAM, "_Scale": 1.0, "_Count": 1}, {TYPE: PARAM, "_Scale": 5.0, "_Count": 5}])


def runtime_file() -> bytes:
    """A type with a RuntimeType field (files with one are not opened)."""
    return user_file({TYPE: RUNTIME, "_Count": 1})


# ---- PAKs --------------------------------------------------------------------------------------------
def member_hash(path: str) -> int:
    return path_hash(path)


def pak(path: Path, members, compress: bool = False) -> Path:
    """A mod PAK holding ``members`` ({game path or hash64: bytes}), stored or deflated."""
    entries = []
    for key, data in dict(members).items():
        h = key if isinstance(key, int) else path_hash(key)
        if compress:
            z = zlib.compressobj(9, zlib.DEFLATED, -15)
            entries.append((h, z.compress(data) + z.flush(), len(data), 1))
        else:
            entries.append((h, data, len(data), 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    write_pak(entries, path)
    return path


# ---- settings files ----------------------------------------------------------------------------------
NIER_CONFIG_LUA = b"""\
-- NierUI settings: change the values below
local Config = {}

Config.UI_Scale = 1.0 -- size of the whole HUD
Config.ShowMinimap = true
Config.FontName = "NotoSansKR"
Config.Opacity = 0.85
Config.Hotkey = 0x70

return Config
"""

LUA_RUNTIME = b"""\
local CONFIG_PATH = "MyMod/config.json"
local cfg = { enabled = true, speed = 1 }
local function load()
    local saved = json.load_file(CONFIG_PATH)
    if saved then cfg = saved end
end
cfg = json.load_file(CONFIG_PATH) or cfg
"""

JSON_SAMPLE = b"""{
    "enabled": true,
    "scale": 1.0,
    "count": 3,
    "name": "x",
    "list": [1, 2.5, null],
    "nested": {"deep": {"v": -1e-5}}
}"""

INI_SAMPLE = b"""; plugin settings
[General]
Enabled=true
Scale=2
Opacity=0.5
Name = "quoted"
"""

TOML_SAMPLE = b"""# plugin settings
title = "My plugin"
enabled = true
ratio = 0.5
count = 1_000

[ui]
font = 'NotoSans'
"""
