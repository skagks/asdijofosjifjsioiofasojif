"""Repair RSZ files (.user / .pfb / .scn) whose types changed in a game update.

An RSZ file is a table of instances (type id + CRC) followed by each instance's field
data in order. The CRC is the type's version stamp: when Capcom changes a class, the
game refuses a file that still carries the old CRC (REFramework: "[Invalid file]",
and the game can hang on a black screen). Fixes, in order (MHWmodfixer's, extended):

  CRC 갱신  every instance still reads exactly under the current layout: only the
           stale CRCs in the instance table change, every other byte stays.
  재구성    some types gained or lost fields: each instance is read with the layout it
           was written with (looked up by its old CRC in the bundled history of REasy's
           dumps), written again in the current layout keeping every field the mod set,
           and new fields take the value the current game's own file has.
  원본 교체  the mod cannot be rewritten safely, but it differs from the current game file
           only in values: the game file is used, with the mod's values copied into it
           wherever a field still exists.
Anything else is left untouched and reported ("복구 불가"). Every output is read back
under the current layout before it is used.

Layouts come from rsz_types.json.xz (tools/make_rsz_types.py: REasy and MHWmodfixer, MIT).
Which variant of them applies is checked against the current game's own copy of the
file: a layout that does not read the game's file exactly is never used.
"""

from __future__ import annotations

import json
import lzma
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

MAGICS = {b"USR\x00": "user", b"PFB\x00": "pfb", b"SCN\x00": "scn"}
RSZ_MAGIC = b"RSZ\x00"
REGISTRY_FILE = Path(__file__).with_name("rsz_types.json.xz")
REFERENCE_KINDS = {"Object", "UserData"}  # values are indexes into the file's own instance table
MAX_COUNT = 1 << 20
MAX_STRING_DIFF = 2  # resource strings that may differ before a file no longer counts as "the same file"
SEARCH_BUDGET = 20000  # field reads allowed while looking for how a changed instance was laid out
MAX_SEARCH_INSTANCES = 800  # larger files are only read with known layouts (no guessing)
_GENERIC_NAME = re.compile(r"^v\d+$")
_CODE_RE = re.compile(r"[a-z]{2}\d{2}", re.IGNORECASE)


class LayoutError(Exception):
    pass


# ---- type registry ----------------------------------------------------------------
class TypeRegistry:
    """Field layouts and CRCs of the game's RSZ types: [name, kind, size, align, is_array]
    per field; "Struct:<type name>" kinds embed another type's fields."""

    def __init__(self, data: dict):
        self.meta = data.get("meta", {})
        self._types = data.get("types", {})
        self._empty = data.get("empty", {})
        self._alt = data.get("alt", {})
        self._old = data.get("old", {})
        self._cache: dict = {}
        self._by_name: dict[str, str] | None = None

    _loaded: "TypeRegistry | None" = None
    _from_file = False

    @classmethod
    def load(cls) -> "TypeRegistry":
        """The bundled registry, kept until release() (it takes some 50 MB)."""
        if cls._loaded is None:
            with lzma.open(REGISTRY_FILE, "rt", encoding="utf-8") as f:
                cls._loaded = cls(json.load(f))
            cls._from_file = True
        return cls._loaded

    @classmethod
    def release(cls) -> None:
        if cls._from_file:
            cls._loaded, cls._from_file = None, False

    @staticmethod
    def _key(type_id: int) -> str:
        return format(type_id, "x")

    def _decode(self, cache_key, text: str | None):
        if text is None:
            return None
        if cache_key not in self._cache:
            self._cache[cache_key] = tuple(tuple(f) for f in json.loads(text))
        return self._cache[cache_key]

    def crc(self, type_id: int) -> int | None:
        key = self._key(type_id)
        if key in self._types:
            return int(self._types[key][0], 16)
        if key in self._empty:
            return int(self._empty[key], 16)
        return None

    def name(self, type_id: int) -> str:
        entry = self._types.get(self._key(type_id))
        return entry[1] if entry else f"type {type_id:08X}"

    def fields(self, type_id: int, alt: bool = False):
        """Current fields; () for a type without fields, None for an unknown type."""
        key = self._key(type_id)
        if alt and key in self._alt:
            return self._decode(("alt", key), self._alt[key])
        if key in self._types:
            return self._decode(("cur", key), self._types[key][2])
        return () if key in self._empty else None

    def old_fields(self, type_id: int, crc: int, alt: bool = False):
        """The fields a type had when its CRC was ``crc`` (an older game version)."""
        key = self._key(type_id)
        found = self._old.get(key, {}).get(format(crc, "x"))
        if found == 1:  # only the CRC changed
            return self.fields(type_id, alt)
        return self._decode(("old", key, crc), found)

    def struct_fields(self, type_name: str):
        if self._by_name is None:
            self._by_name = {v[1]: k for k, v in self._types.items()}
        key = self._by_name.get(type_name)
        return self._decode(("cur", key), self._types[key][2]) if key else None


# ---- file structure -----------------------------------------------------------------
_HEADER_DATA_OFFSET = {"user": 32, "pfb": 48, "scn": 56}  # where each header keeps the RSZ block's offset


@dataclass
class RszInfo:
    data: bytes
    kind: str
    rsz_off: int
    insts: list  # [(type id, crc)], index 0 is the null instance
    inst_table: int  # file offset of the instance table
    external: set  # instances that are references to other .user files (no inline data)
    data_start: int  # file offset of the instance data, which runs to the end of the file

    @property
    def types(self) -> set[int]:
        return {t for i, (t, _c) in enumerate(self.insts) if i and t and i not in self.external}

    def strings(self) -> set[str]:
        """UTF-16 strings before the RSZ block: resource and userdata paths, mainly."""
        return {s for _o, s in scan_strings(self.data[:self.rsz_off])}


def _rsz_header(data: bytes, off: int):
    if off < 0 or off + 48 > len(data) or data[off:off + 4] != RSZ_MAGIC:
        return None
    version, _objects, count, userdata, _reserved = struct.unpack_from("<IIIII", data, off + 4)
    inst_off, data_off, user_off = struct.unpack_from("<QQQ", data, off + 24)
    if version < 4 or count > MAX_COUNT or userdata > MAX_COUNT:
        return None
    if off + inst_off + count * 8 > len(data) or off + user_off + userdata * 16 > len(data) or off + data_off > len(data):
        return None
    return count, userdata, inst_off, data_off, user_off


def parse(data: bytes) -> RszInfo | None:
    kind = MAGICS.get(bytes(data[:4]))
    if kind is None:
        return None
    candidates = []
    if len(data) >= _HEADER_DATA_OFFSET[kind] + 8:
        candidates.append(struct.unpack_from("<Q", data, _HEADER_DATA_OFFSET[kind])[0])
    candidates.append(data.find(RSZ_MAGIC, 16))
    for off in candidates:
        header = _rsz_header(data, off)
        if header is None:
            continue
        count, userdata, inst_off, data_off, user_off = header
        insts = [struct.unpack_from("<II", data, off + inst_off + i * 8) for i in range(count)]
        external = {struct.unpack_from("<i", data, off + user_off + i * 16)[0] for i in range(userdata)}
        return RszInfo(bytes(data), kind, off, insts, off + inst_off, external, off + data_off)
    return None


def scan_strings(region: bytes) -> list[tuple[int, str]]:
    """UTF-16LE printable-ASCII runs longer than two characters, with their offsets."""
    out = []
    i, n = 0, len(region)
    while i < n - 1:
        if region[i + 1] == 0 and 0x20 <= region[i] < 0x7F:
            j = i
            chars = []
            while j < n - 1 and region[j + 1] == 0 and 0x20 <= region[j] < 0x7F:
                chars.append(chr(region[j]))
                j += 2
            if len(chars) > 2:
                out.append((i, "".join(chars)))
                i = j
                continue
        i += 1
    return out


# ---- reading and writing field values --------------------------------------------------
@dataclass(frozen=True)
class Schema:
    """How fields are read: REasy reads Resource fields as strings, MHWmodfixer as plain
    4-byte values, and their native layouts differ for some types; the current game's own
    file decides which applies (see choose_schema)."""
    resource_string: bool = True
    alt: bool = False

    def variable(self, kind: str) -> bool:
        return kind == "String" or (kind == "Resource" and self.resource_string)


class Budget:
    def __init__(self, reads: int = SEARCH_BUDGET):
        self.reads = reads

    def spend(self):
        self.reads -= 1
        if self.reads < 0:
            raise LayoutError("search budget exhausted")


def _u32(data: bytes, pos: int, end: int) -> int:
    if pos + 4 > end:
        raise LayoutError("past the end")
    return struct.unpack_from("<I", data, pos)[0]


def _pad(data: bytes, pos: int, align: int) -> tuple[int, bool]:
    new = pos + (-pos) % max(align, 1)
    return new, not any(data[pos:new])


def _read_one(data, pos, end, kind, size, schema) -> tuple[bytes, int]:
    if schema.variable(kind):
        length = 4 + _u32(data, pos, end) * 2
        if length > 4 + 2 * MAX_COUNT:
            raise LayoutError("implausible string length")
    else:
        length = size
    if pos + length > end:
        raise LayoutError("past the end")
    return data[pos:pos + length], pos + length


def read_values(data: bytes, pos: int, end: int, fields, schema: Schema, registry: TypeRegistry,
                budget: Budget | None = None) -> tuple[list, int, bool]:
    """Values of one instance (or struct) starting at ``pos``: raw bytes for a single field,
    a list of elements for an array (a struct element is a list of values).
    Returns (values, end position, whether every padding byte was zero)."""
    values, clean = [], True
    for _name, kind, size, align, is_array in fields:
        if budget is not None:
            budget.spend()
        if is_array:
            pos, ok = _pad(data, pos, 4)
            clean &= ok
            count = _u32(data, pos, end)
            if count > MAX_COUNT:
                raise LayoutError("implausible array length")
            pos += 4
            elems = []
            if kind.startswith("Struct:"):
                sub = registry.struct_fields(kind[7:])
                if sub is None:
                    raise LayoutError(f"unknown struct {kind[7:]}")
                if count:
                    pos, ok = _pad(data, pos, align)
                    clean &= ok
                for _ in range(count):
                    elem, pos, ok = read_values(data, pos, end, sub, schema, registry, budget)
                    clean &= ok
                    elems.append(elem)
            else:
                for _ in range(count):
                    pos, ok = _pad(data, pos, 4 if schema.variable(kind) else align)
                    clean &= ok
                    elem, pos = _read_one(data, pos, end, kind, size, schema)
                    elems.append(elem)
            values.append(elems)
        else:
            if kind.startswith("Struct:"):
                raise LayoutError("a single embedded struct")
            pos, ok = _pad(data, pos, 4 if schema.variable(kind) else align)
            clean &= ok
            value, pos = _read_one(data, pos, end, kind, size, schema)
            values.append(value)
        if pos > end:
            raise LayoutError("past the end")
    return values, pos, clean


def write_values(out: bytearray, base: int, fields, values, schema: Schema, registry: TypeRegistry) -> None:
    """Append values in the layout ``fields``; padding is recomputed for where they land
    (``base`` is the file offset of out[0])."""
    def pad(align):
        out.extend(b"\x00" * ((-(base + len(out))) % max(align, 1)))

    for (_name, kind, size, align, is_array), value in zip(fields, values):
        if is_array:
            pad(4)
            out += struct.pack("<I", len(value))
            if kind.startswith("Struct:"):
                sub = registry.struct_fields(kind[7:])
                if value:
                    pad(align)
                for elem in value:
                    write_values(out, base, sub, elem, schema, registry)
            else:
                for elem in value:
                    pad(4 if schema.variable(kind) else align)
                    out += elem
        else:
            pad(4 if schema.variable(kind) else align)
            out += value


@dataclass
class Instance:
    index: int
    type_id: int
    crc: int
    fields: tuple | None  # None: carries no data here (null instance, external userdata)
    values: list | None
    start: int
    end: int
    layout: str = "current"  # "current" | "old" (from the history) | "short" (the current one minus new fields)


def walk_current(info: RszInfo, schema: Schema, registry: TypeRegistry) -> tuple[list[Instance], bool] | None:
    """Every instance read with the current layout: (instances, padding clean), or None
    when a type is unknown or the data does not end exactly at the end of the file."""
    pos, end, clean, out = info.data_start, len(info.data), True, []
    for i, (t, c) in enumerate(info.insts):
        if i == 0 or not t or i in info.external:
            out.append(Instance(i, t, c, None, None, pos, pos))
            continue
        fields = registry.fields(t, schema.alt)
        if fields is None:
            return None
        try:
            values, new, ok = read_values(info.data, pos, end, fields, schema, registry)
        except LayoutError:
            return None
        out.append(Instance(i, t, c, fields, values, pos, new))
        clean &= ok
        pos = new
    return (out, clean) if pos == end else None


def walk_mod(info: RszInfo, schema: Schema, registry: TypeRegistry, current_crc) -> list[Instance] | None:
    """The mod's instances, each read with the layout it was written with: the current one
    when its CRC is current, the historical one for an older CRC, or failing those the
    current layout without some of its last fields (fields are added at the end of a class).
    Requires exactly one way of reading the whole data block; None otherwise."""
    end = len(info.data)
    known = _walk_known(info, schema, registry, current_crc)
    if known is not None or len(info.insts) > MAX_SEARCH_INSTANCES:
        return known
    budget = Budget()
    solutions: list[list[Instance]] = []

    def candidates(i, t, c):
        out = []
        now = registry.fields(t, schema.alt)
        cur = current_crc(t)
        if now is not None and (cur is None or cur == c):
            out.append((now, "current"))
        old = registry.old_fields(t, c, schema.alt) if cur is not None and cur != c else None
        if old is not None:
            out.append((old, "old"))
        elif now is not None and cur is not None and cur != c:
            out.append((now, "current"))  # only the CRC changed
        if now:
            out += [(now[:len(now) - k], "short") for k in range(1, len(now))]
        return out

    def step(i, pos, acc):
        if len(solutions) > 1:
            return
        if i == len(info.insts):
            if pos == end:
                solutions.append(list(acc))
            return
        t, c = info.insts[i]
        if i == 0 or not t or i in info.external:
            acc.append(Instance(i, t, c, None, None, pos, pos))
            step(i + 1, pos, acc)
            acc.pop()
            return
        for fields, how in candidates(i, t, c):
            try:
                values, new, clean = read_values(info.data, pos, end, fields, schema, registry, budget)
            except LayoutError as exc:
                if "budget" in str(exc):
                    raise
                continue
            if how != "current" and not clean:
                continue  # a guessed layout must not read nonzero bytes as padding
            acc.append(Instance(i, t, c, fields, values, pos, new, how))
            step(i + 1, new, acc)
            acc.pop()
            if solutions and how != "short":
                return  # a known layout explains this instance: no need to guess

    try:
        step(0, info.data_start, [])
    except (LayoutError, RecursionError):
        return None
    return solutions[0] if len(solutions) == 1 else None


def _walk_known(info: RszInfo, schema: Schema, registry: TypeRegistry, current_crc) -> list[Instance] | None:
    """walk_mod without guessing: the current layout, or the historical one for an older CRC."""
    pos, end, out = info.data_start, len(info.data), []
    for i, (t, c) in enumerate(info.insts):
        if i == 0 or not t or i in info.external:
            out.append(Instance(i, t, c, None, None, pos, pos))
            continue
        cur = current_crc(t)
        old = registry.old_fields(t, c, schema.alt) if cur is not None and cur != c else None
        fields, how = (old, "old") if old is not None else (registry.fields(t, schema.alt), "current")
        if fields is None:
            return None
        try:
            values, new, _clean = read_values(info.data, pos, end, fields, schema, registry)
        except LayoutError:
            return None
        out.append(Instance(i, t, c, fields, values, pos, new, how))
        pos = new
    return out if pos == end else None


# ---- comparing with the current game file --------------------------------------------
def choose_schema(donor: RszInfo | None, mod: RszInfo, registry: TypeRegistry):
    """The first way of reading fields under which the current game's file reads exactly
    (or, without one, the mod's). Returns (schema, instances of that file) or (None, None)."""
    target = donor if donor is not None else mod
    for schema in (Schema(True, False), Schema(False, False), Schema(True, True), Schema(False, True)):
        walked = walk_current(target, schema, registry)
        if walked is not None:
            return schema, walked[0]
    return None, None


def crc_map(info: RszInfo) -> dict[int, int]:
    found, mixed = {}, set()
    for i, (t, c) in enumerate(info.insts):
        if i == 0 or not t:
            continue
        if found.setdefault(t, c) != c:
            mixed.add(t)
    return {t: c for t, c in found.items() if t not in mixed}


def _strip_at(s: str) -> str:
    return s[1:] if s.startswith("@") else s


def _diff(a: set[str], b: set[str]) -> set[str]:
    return {_strip_at(s) for s in a} ^ {_strip_at(s) for s in b}


def find_substitution(mod_strings: set[str], donor_strings: set[str]):
    """("close", None) when the two files reference (almost) the same resources;
    ("close", (donor code, mod code)) when they do once e.g. "ch03" reads "mh03" (a mod in a
    custom character slot); None when they are different files."""
    if len(_diff(mod_strings, donor_strings)) <= MAX_STRING_DIFF:
        return "close", None
    mod_codes = {m.group(0) for s in mod_strings for m in _CODE_RE.finditer(s)}
    donor_codes = {m.group(0) for s in donor_strings for m in _CODE_RE.finditer(s)}
    best = None
    for donor_code in donor_codes - mod_codes:
        for mod_code in mod_codes - donor_codes:
            if mod_code[2:] != donor_code[2:]:
                continue
            d = len(_diff(mod_strings, {s.replace(donor_code, mod_code) for s in donor_strings}))
            if best is None or d < best[0]:
                best = (d, donor_code, mod_code)
    if best is not None and best[0] <= MAX_STRING_DIFF:
        return "close", (best[1], best[2])
    return None


def apply_substitution(data: bytes, donor_code: str, mod_code: str, provided: set[str]) -> bytes:
    """Put the mod's character code back into the game file's strings (same length, in place).
    A path is only changed when the mod ships that file; a bare name always is."""
    out = bytearray(data)
    new = mod_code.encode("utf-16-le")
    for offset, s in scan_strings(data):
        if donor_code not in s:
            continue
        if "/" in s and Path(_strip_at(s.replace(donor_code, mod_code))).name.lower() not in provided:
            continue
        start = 0
        while (idx := s.find(donor_code, start)) != -1:
            pos = offset + idx * 2
            out[pos:pos + len(new)] = new
            start = idx + len(donor_code)
    return bytes(out)


# ---- repair --------------------------------------------------------------------------
@dataclass
class RszPlan:
    label: str
    kind: str  # "user" | "pfb" | "scn"
    state: str = "current"  # "current" | "fixable" | "stuck"
    method: str = ""  # "CRC 갱신" | "재구성" | "원본 교체"
    data: bytes | None = None
    notes: list[str] = field(default_factory=list)
    reason: str = ""  # why a stale file cannot be repaired


def _rebuild(info: RszInfo, instances: list[Instance], schema: Schema, registry: TypeRegistry,
             crcs: dict[int, int] | None) -> bytes:
    """``info``'s file with its instance data written again from ``instances`` (each with
    its fields and values) and, when given, every instance's CRC from ``crcs``."""
    out = bytearray()
    for inst in instances:
        if inst.fields is not None:
            write_values(out, info.data_start, inst.fields, inst.values, schema, registry)
    result = bytearray(info.data[:info.data_start]) + out
    if crcs:
        for i, (t, c) in enumerate(info.insts):
            if i and t in crcs and crcs[t] != c:
                struct.pack_into("<I", result, info.inst_table + i * 8 + 4, crcs[t])
    return bytes(result)


def _verify(data: bytes, schema: Schema, registry: TypeRegistry) -> bool:
    info = parse(data)
    return info is not None and walk_current(info, schema, registry) is not None


def _same_field(a, b) -> bool:
    return a[0] == b[0] and a[1] == b[1] and a[2] == b[2] and a[4] == b[4]


def _occurrences(instances: list[Instance]) -> dict[int, list[Instance]]:
    by_type: dict[int, list[Instance]] = {}
    for inst in instances:
        if inst.fields is not None:
            by_type.setdefault(inst.type_id, []).append(inst)
    return by_type


def _is_null(value) -> bool:
    return not any(value) if isinstance(value, (bytes, bytearray)) else not value


def _paths_in(value) -> list[str]:
    blobs = value if isinstance(value, list) else [value]
    out = []
    for blob in blobs:
        if isinstance(blob, (bytes, bytearray)) and len(blob) >= 4:
            text = blob[4:].decode("utf-16-le", errors="ignore").rstrip("\x00")
            if "/" in text:
                out.append(_strip_at(text).lower())
    return out


def _migrate(mod: RszInfo, walked: list[Instance], donor_insts: list[Instance] | None, schema: Schema,
             registry: TypeRegistry, notes: list[str]) -> list[Instance] | None:
    """The mod's instances in the current layout: the mod's own value for every field that
    still exists, the current game file's value for new ones. None when a new field cannot
    be filled safely (a reference into the game file's own instances, an undeclared path)."""
    donors = _occurrences(donor_insts or [])
    seen: dict[int, int] = {}
    manifest = {_strip_at(s).lower() for s in mod.strings()}
    out, added, dropped = [], set(), set()
    for inst in walked:
        if inst.fields is None or inst.layout == "current":
            out.append(inst)
            if inst.fields is not None:
                seen[inst.type_id] = seen.get(inst.type_id, 0) + 1
            continue
        now = registry.fields(inst.type_id, schema.alt)
        if now is None:
            return None
        if any(_GENERIC_NAME.match(f[0]) for f in now) and [f[0] for f in now[:len(inst.fields)]] != \
                [f[0] for f in inst.fields]:
            return None  # unnamed (native) fields cannot be matched by name across versions
        k = seen.get(inst.type_id, 0)
        seen[inst.type_id] = k + 1
        pool = donors.get(inst.type_id, [])
        donor = pool[k] if k < len(pool) else (pool[0] if pool else None)
        mine = {f[0]: (f, v) for f, v in zip(inst.fields, inst.values)}
        theirs = dict(zip((f[0] for f in donor.fields), donor.values)) if donor is not None else {}
        values = []
        for f in now:
            have = mine.get(f[0])
            if have is not None and _same_field(have[0], f):
                values.append(have[1])
                continue
            if f[0] not in theirs:
                return None  # a new field with no current value to take
            value = theirs[f[0]]
            if (f[1] in REFERENCE_KINDS or f[1] == "GameObjectRef") and not _is_null(value):
                return None  # points at instances only the game file has
            if schema.variable(f[1]) and any(p not in manifest for p in _paths_in(value)):
                return None  # would reference a resource the mod's file does not declare
            values.append(value)
            added.add(f"{registry.name(inst.type_id)}.{f[0]}")
        dropped |= {f"{registry.name(inst.type_id)}.{f[0]}" for f in inst.fields
                    if not any(_same_field(f, g) for g in now)}
        out.append(Instance(inst.index, inst.type_id, inst.crc, now, values, inst.start, inst.end))
    if added:
        notes.append("새 필드는 게임 원본 값으로 채움: " + ", ".join(sorted(added)[:6]) +
                     (f" 외 {len(added) - 6}개" if len(added) > 6 else ""))
    if dropped:
        notes.append("게임에서 없어진 필드: " + ", ".join(sorted(dropped)[:6]) +
                     (f" 외 {len(dropped) - 6}개" if len(dropped) > 6 else ""))
    return out


def _transplant(donor: RszInfo, donor_insts: list[Instance], walked: list[Instance] | None,
                schema: Schema, registry: TypeRegistry, notes: list[str]) -> bytes:
    """The current game file with the mod's values copied in: for each instance of the same
    type (in order) every field that still exists, except references to other instances and
    paths the game file does not declare. Without the mod's values the game file as is."""
    if walked is None:
        notes.append("모드가 바꾼 값은 옮기지 못하고 게임 원본 값을 씁니다")
        return donor.data
    mods = _occurrences(walked)
    seen: dict[int, int] = {}
    manifest = {_strip_at(s).lower() for s in donor.strings()}
    kept = lost = 0
    out = []
    for inst in donor_insts:
        if inst.fields is None:
            out.append(inst)
            continue
        k = seen.get(inst.type_id, 0)
        seen[inst.type_id] = k + 1
        pool = mods.get(inst.type_id, [])
        mine = pool[k] if k < len(pool) else None
        values = list(inst.values)
        if mine is not None:
            theirs = {f[0]: (f, v) for f, v in zip(mine.fields, mine.values)}
            for j, f in enumerate(inst.fields):
                have = theirs.get(f[0])
                if have is None or not _same_field(have[0], f) or have[1] == values[j]:
                    continue
                if f[1] in REFERENCE_KINDS or f[1] == "GameObjectRef" or f[1].startswith("Struct:") or \
                        (schema.variable(f[1]) and any(p not in manifest for p in _paths_in(have[1]))):
                    lost += 1
                    continue
                values[j] = have[1]
                kept += 1
        out.append(Instance(inst.index, inst.type_id, inst.crc, inst.fields, values, inst.start, inst.end))
    notes.append(f"모드가 바꾼 값 {kept}개를 옮겼습니다" + (f", {lost}개(다른 구성요소·경로를 가리키는 값)는 게임 원본 값을 씁니다"
                                                    if lost else ""))
    return _rebuild(donor, out, schema, registry, None)


def plan(label: str, mod_bytes: bytes, donor_bytes: bytes | None, registry: TypeRegistry,
         provided: set[str] | None = None, trust_registry: bool = False) -> RszPlan | None:
    """How to bring one RSZ file up to date with the current game. ``donor_bytes`` is the
    current game's own copy of the same file (None when the game has none); ``provided``
    the lower-cased file names (without version number) the mod ships, for custom slots;
    ``trust_registry``: the registry's CRCs are known to match this game version.
    None when the file is not an RSZ file."""
    mod = parse(mod_bytes)
    if mod is None:
        return None
    result = RszPlan(label, mod.kind)
    donor = parse(donor_bytes) if donor_bytes else None
    if donor is None and not trust_registry:
        return result  # nothing to compare with: leave it as it is
    donor_crcs = crc_map(donor) if donor is not None else {}

    def current_crc(t):
        if t in donor_crcs:
            return donor_crcs[t]
        return registry.crc(t) if trust_registry else None

    stale = sorted({t for i, (t, c) in enumerate(mod.insts) if i and t and current_crc(t) not in (None, c)})
    schema, donor_insts = choose_schema(donor, mod, registry)
    mod_now = walk_current(mod, schema, registry) if schema is not None else None
    if not stale and (mod_now is not None or schema is None or donor is None or mod.types - donor.types):
        # current, or nothing shows otherwise: a type the game file does not have may just be
        # one the registry describes wrongly
        return result

    result.state = "stuck"
    if stale:
        names = [registry.name(t) for t in stale]
        result.notes.append("CRC가 바뀐 타입: " + ", ".join(names[:5]) + (f" 외 {len(names) - 5}개" if len(names) > 5 else ""))
    crcs = {t: current_crc(t) for t, _c in mod.insts if t and current_crc(t) is not None}
    unknown = {t for i, (t, _c) in enumerate(mod.insts) if i and t and i not in mod.external and t not in crcs}
    if unknown:
        result.notes.append(f"현재 CRC를 확인할 수 없는 타입 {len(unknown)}개는 그대로 두었습니다")

    # 1. the layout is unchanged: only the CRCs. Not when the layout a stale CRC stands for is
    # known to differ: a new 1-byte field can sit where padding was, so the old data still
    # "reads" under the new layout, but with the wrong meaning.
    reshaped = any((old := registry.old_fields(t, c, schema.alt if schema else False)) is not None and
                   [f[1:] for f in old] != [f[1:] for f in (registry.fields(t, schema.alt if schema else False) or ())]
                   for i, (t, c) in enumerate(mod.insts) if i and t in stale)
    if schema is not None and mod_now is not None and not reshaped:
        patched = bytearray(mod.data)  # every byte but the CRCs stays
        for i, (t, c) in enumerate(mod.insts):
            if i and t in crcs and crcs[t] != c:
                struct.pack_into("<I", patched, mod.inst_table + i * 8 + 4, crcs[t])
        if _verify(bytes(patched), schema, registry):
            result.state, result.method, result.data = "fixable", "CRC 갱신", bytes(patched)
            return result

    # 2. some types changed: read each instance as it was written, write it in the current layout
    walked = walk_mod(mod, schema, registry, current_crc) if schema is not None else None
    if walked is not None:
        notes: list[str] = []
        migrated = _migrate(mod, walked, donor_insts if donor is not None else None, schema, registry, notes)
        if migrated is not None:
            data = _rebuild(mod, migrated, schema, registry, crcs)
            if _verify(data, schema, registry):
                result.state, result.method, result.data = "fixable", "재구성", data
                result.notes += notes
                return result

    # 3. the game's own file, with the mod's values where they still fit
    if donor is None:
        result.reason = "게임에 같은 파일이 없어 비교할 수 없습니다"
        return result
    extra = mod.types - donor.types
    if extra:
        result.reason = ("게임 원본에 없는 구성요소가 들어 있어 원본으로 바꿀 수 없습니다: " +
                         ", ".join(sorted(registry.name(t) for t in extra)[:3]))
        return result
    match = find_substitution(mod.strings(), donor.strings())
    if match is None:
        result.reason = "게임 원본과 참조하는 파일이 많이 달라 안전하게 판단할 수 없습니다"
        return result
    notes = []
    data = _transplant(donor, donor_insts, walked, schema, registry, notes) if schema is not None else donor.data
    if schema is None:
        notes.append("필드 구성을 확인할 수 없어 모드가 바꾼 값은 옮기지 못했습니다")
    elif not _verify(data, schema, registry):
        data = donor.data
        notes = ["모드가 바꾼 값은 옮기지 못하고 게임 원본 값을 씁니다"]
    if match[1] is not None:
        data = apply_substitution(data, match[1][0], match[1][1], provided or set())
        notes.append(f"'{match[1][0]}' 경로를 모드의 '{match[1][1]}'(으)로 바꿨습니다")
    result.state, result.method, result.data = "fixable", "원본 교체", data
    result.notes += notes
    return result
