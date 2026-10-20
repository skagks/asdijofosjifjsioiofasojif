"""Synthetic game files for the equipment catalog tests (no Capcom data): RSZ .user files written
with the real type registry, .msg text files, GUI prefabs and a fake game to read them from.

An instance to write is a dict like read_user returns: {"$type": <type name>, <field>: value};
fields left out get zero values.  Values by registry kind: int, bool, float, str (String /
Resource), bytes or a uuid string (Guid), a dict (Object: another instance, written before the
one that points at it, as the game does; the same dict is written once), {"$type": <type>,
"$user": <path>} (a reference to another .user file), lists for arrays.
"""

from __future__ import annotations

import struct
import uuid

from mhwmm.equip import msg as msgmod
from mhwmm.matfix import rsz
from mhwmm.matfix.pak import murmur3_32, path_hash

TYPE, USER = "$type", "$user"
WILDS_LANGUAGES = [i if i in (0, 1, 2, 3, 4, 5, 6, 7, 10, 11, 12, 13, 21, 26, 32) else -1 for i in range(33)]
_INTS = {"S8": "<b", "U8": "<B", "C8": "<B", "S16": "<h", "U16": "<H", "C16": "<H",
         "S32": "<i", "U32": "<I", "S64": "<q", "U64": "<Q"}
_FLOATS = {"F32": "<f", "F64": "<d", "F16": "<e"}
_TYPE_IDS: dict[int, dict[str, int]] = {}


def registry() -> rsz.TypeRegistry:
    return rsz.TypeRegistry.load()


def type_id(name: str, reg: rsz.TypeRegistry | None = None) -> int:
    reg = reg or registry()
    ids = _TYPE_IDS.get(id(reg))
    if ids is None:
        ids = _TYPE_IDS[id(reg)] = {v[1]: int(k, 16) for k, v in reg._types.items()}
    return ids[name]


# ---- building blocks ---------------------------------------------------------------------
def enum(type_name: str, value: int) -> dict:
    """An enum wrapper instance (``app.X.Y_Serializable``) holding a Fixed value."""
    return {TYPE: type_name, "_Value": value}


def rare(n: int) -> dict:
    """app.ItemDef.RARE for in-game rarity n (1..12): stored as Fixed 18 - (n - 1)."""
    return enum("app.ItemDef.RARE_Serializable", 19 - n)


def prefab(path: str) -> dict:
    return {TYPE: "via.Prefab", "Standby": False, "Path": path}


def user_ref(type_name: str, path: str) -> dict:
    return {TYPE: type_name, USER: path}


def guid(n: int) -> bytes:
    """A made-up GUID (16 bytes, as .user and .msg files store them)."""
    return uuid.UUID(int=(0x5EED << 112) | n).bytes_le


def wstring(text: str) -> bytes:
    """An RSZ String / Resource value: UTF-16 code units (with the NUL) and the text."""
    if not text:
        return struct.pack("<I", 0)
    raw = (text + "\0").encode("utf-16-le")
    return struct.pack("<I", len(raw) // 2) + raw


# ---- .user files -------------------------------------------------------------------------
class _Writer:
    def __init__(self, reg: rsz.TypeRegistry):
        self.reg = reg
        self.insts: list[tuple] = [(0, 0, None, None)]  # (type id, crc, fields | None, values)
        self.index: dict[int, int] = {}
        self.busy: set[int] = set()
        self.userdata: list[tuple[int, int, str]] = []  # (instance, type id, path)

    def add(self, obj) -> int:
        if obj is None:
            return 0
        if id(obj) in self.index:
            return self.index[id(obj)]
        if id(obj) in self.busy:
            raise ValueError("a cycle of instances cannot be written")
        tid = type_id(obj[TYPE], self.reg)
        crc = self.reg.crc(tid) or 0
        if USER in obj:
            idx = len(self.insts)
            self.insts.append((tid, crc, None, None))
            self.userdata.append((idx, tid, obj[USER]))
        else:
            fields = self.reg.fields(tid)
            if fields is None:
                raise KeyError(obj[TYPE])
            self.busy.add(id(obj))
            values = self.values(fields, obj)
            self.busy.discard(id(obj))
            idx = len(self.insts)
            self.insts.append((tid, crc, fields, values))
        self.index[id(obj)] = idx
        return idx

    def values(self, fields, obj: dict) -> list:
        out = []
        for field in fields:
            name, kind, _size, _align, is_array = field
            value = obj.get(name)
            if is_array:
                items = value or []
                if kind.startswith("Struct:"):
                    sub = self.reg.struct_fields(kind[7:])
                    out.append([self.values(sub, item) for item in items])
                else:
                    out.append([self.one(field, item) for item in items])
            else:
                out.append(self.one(field, value))
        return out

    def one(self, field, value) -> bytes:
        name, kind, size, _align, _is_array = field
        if kind in rsz.REFERENCE_KINDS:
            return struct.pack("<i", value if isinstance(value, int) else self.add(value))
        if kind in _INTS:
            return struct.pack(_INTS[kind], int(value or 0))
        if kind in _FLOATS and size == struct.calcsize(_FLOATS[kind]):
            return struct.pack(_FLOATS[kind], float(value or 0))
        if kind == "Bool":
            return b"\x01" if value else b"\x00"
        if kind in ("String", "Resource"):
            return wstring(value or "")
        if kind == "Guid" and isinstance(value, str):
            return uuid.UUID(value).bytes_le
        if value is None:
            return bytes(size)
        if len(value) != size:
            raise ValueError(f"{name}: {size} bytes expected")
        return bytes(value)


def _align(n: int, a: int = 16) -> int:
    return n + (-n) % a


def user_file(root: dict, reg: rsz.TypeRegistry | None = None, resources=()) -> bytes:
    """A .user file whose object table holds ``root``, laid out like the game's (USR header,
    resource and userdata tables, RSZ block with its own userdata table, 16-aligned data)."""
    reg = reg or registry()
    w = _Writer(reg)
    root_index = w.add(root)
    res_strings = [r.encode("utf-16-le") + b"\0\0" for r in resources]
    ud_strings = [p.encode("utf-16-le") + b"\0\0" for _i, _t, p in w.userdata]
    res_table = 48
    ud_table = res_table + 8 * len(resources)
    pos = ud_table + 16 * len(w.userdata)
    head = bytearray()
    res_offsets, ud_offsets = [], []
    for s in res_strings:
        res_offsets.append(pos)
        pos += len(s)
    for s in ud_strings:
        ud_offsets.append(pos)
        pos += len(s)
    rsz_off = _align(pos)
    head += struct.pack("<4siiiQQQQ", b"USR\0", len(resources), len(w.userdata), 0, res_table, ud_table, rsz_off, 0)
    for off in res_offsets:
        head += struct.pack("<Q", off)
    for (_i, tid, _p), off in zip(w.userdata, ud_offsets):
        head += struct.pack("<IIQ", tid, 0, off)
    head += b"".join(res_strings) + b"".join(ud_strings)
    head = head.ljust(rsz_off, b"\0")

    inst_off = 48 + 4
    ud_off = _align(rsz_off + inst_off + 8 * len(w.insts)) - rsz_off
    strings_off = ud_off + 16 * len(w.userdata)
    block = bytearray(struct.pack("<4sIIIII", b"RSZ\0", 16, 1, len(w.insts), len(w.userdata), 0))
    block += bytes(24)  # offsets, filled in below
    block += struct.pack("<i", root_index)
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


def table(root_type: str, row_type: str, rows: list[dict], field: str = "_Values", **extra) -> dict:
    """A game table: the root instance with its rows (each a dict of fields, given the row type)."""
    return {TYPE: root_type, field: [dict(r, **{TYPE: row_type}) for r in rows], **extra}


def app_table(name: str, rows: list[dict], field: str = "_Values") -> dict:
    """app.user_data.<name> with rows of app.user_data.<name>.cData (most of the game's tables)."""
    return table(f"app.user_data.{name}", f"app.user_data.{name}.cData", rows, field)


# ---- prefabs -----------------------------------------------------------------------------
def pfb_file(*resources: str) -> bytes:
    """A prefab with a resource table and an empty RSZ block, laid out like the game's."""
    strings = b"".join(r.encode("utf-16-le") + b"\0\0" for r in resources)
    table_off = 56
    start = table_off + 8 * len(resources)
    rsz_off = _align(start + len(strings))
    header = struct.pack("<4s5I3QQ", b"PFB\0", 1, len(resources), 0, 0, 0, 0, table_off, 0, rsz_off)
    offsets, pos = b"", start
    for r in resources:
        offsets += struct.pack("<Q", pos)
        pos += (len(r) + 1) * 2
    block = struct.pack("<4sIIIII", b"RSZ\0", 16, 1, 1, 0, 0) + struct.pack("<QQQ", 64, 80, 80)
    block = (block + struct.pack("<i", 0)).ljust(64, b"\0") + struct.pack("<II", 0, 0)
    return (header + offsets + strings).ljust(rsz_off, b"\0") + block.ljust(80, b"\0")


# ---- .msg files --------------------------------------------------------------------------
def msg_file(entries, languages=WILDS_LANGUAGES, version: int = 23) -> bytes:
    """A GMSG file (the layout in mhwmm.equip.msg): entries (guid bytes, name, {language: text})."""
    languages = list(languages)
    n, slots = len(entries), len(languages)
    table_off = 0x48
    unused_off = table_off + 8 * n
    lang_off = unused_off + 8
    attr_off = _align(lang_off + 4 * slots, 8)
    first = attr_off
    head_size = 40 + 8 * slots
    pool_off = first + n * head_size
    pool, where = bytearray(), {}

    def put(text: str) -> int:
        if text not in where:
            where[text] = len(pool)
            pool.extend((text + "\0").encode("utf-16-le"))
        return pool_off + where[text]

    put("")
    out = bytearray(pool_off)
    struct.pack_into("<I4sQIII", out, 0, version, b"GMSG", 0x10, n, 0, slots)
    struct.pack_into("<QQQQQ", out, 0x20, pool_off, unused_off, lang_off, attr_off, attr_off)
    struct.pack_into(f"<{slots}i", out, lang_off, *languages)
    for i, (gid, name, texts) in enumerate(entries):
        head = first + i * head_size
        struct.pack_into("<Q", out, table_off + 8 * i, head)
        ptrs = [put(texts.get(code, "")) if code >= 0 else put("") for code in languages]
        struct.pack_into("<16sIIQQ", out, head, bytes(gid), 0, murmur3_32(name.encode("utf-16-le")), put(name), pool_off)
        struct.pack_into(f"<{slots}Q", out, head + 40, *ptrs)
    return bytes(out) + msgmod.encrypt_pool(bytes(pool))


def names(prefix: str, rows: dict) -> bytes:
    """A .msg file of Korean / English names: {guid: (korean, english)} (either may be "")."""
    return msg_file([(g, f"{prefix}_{i}", {msgmod.KOREAN: ko, msgmod.ENGLISH: en})
                     for i, (g, (ko, en)) in enumerate(rows.items())])


# ---- the game ----------------------------------------------------------------------------
class FakeGame:
    """Game files by path (case-insensitive, as the game's paks): read(path) / has(path) like
    matfix.game.GameIndex.  ``present``: paths that exist without content (textures)."""

    def __init__(self, files: dict[str, bytes] | None = None, present=()):
        self.files: dict[int, bytes] = {}
        self.reads: list[str] = []
        for path, data in (files or {}).items():
            self.add(path, data)
        for path in present:
            self.add(path, b"")

    def add(self, path: str, data: bytes) -> None:
        self.files[path_hash(path)] = data

    def remove(self, path: str) -> None:
        self.files.pop(path_hash(path), None)

    def has(self, path: str) -> bool:
        return path_hash(path) in self.files

    def read(self, path: str) -> bytes | None:
        self.reads.append(path)
        return self.files.get(path_hash(path))
