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
REGISTRY_NAME = REGISTRY_FILE.name
# The same file as kept current in the repository (rebaked when REasy updates its dump)
REGISTRY_URL = "https://raw.githubusercontent.com/skagks/asdijofosjifjsioiofasojif/HEAD/mhwmm/matfix/" + REGISTRY_NAME
REFERENCE_KINDS = {"Object", "UserData"}  # values are indexes into the file's own instance table
CHAIN_TYPES = ("app.ChainSetting", "via.motion.Chain", "via.motion.ChildSecondary")  # see plan(): never rewritten
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
    updates: Path | None = None  # folder that may hold a newer registry than the bundled one

    @classmethod
    def path(cls) -> Path:
        """The newest registry available: the bundled one or a downloaded one."""
        best = REGISTRY_FILE
        if cls.updates is not None:
            downloaded = cls.updates / REGISTRY_NAME
            if downloaded.is_file() and registry_date(downloaded) > registry_date(REGISTRY_FILE):
                best = downloaded
        return best

    @classmethod
    def load(cls) -> "TypeRegistry":
        """The registry, kept until release() (it takes some 50 MB)."""
        if cls._loaded is None:
            with lzma.open(cls.path(), "rt", encoding="utf-8") as f:
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


def registry_date(path: Path) -> str:
    """The date of the REasy dump a registry file was made from ("" if unreadable), read
    from the start of the file only (meta comes first)."""
    try:
        with lzma.open(path, "rt", encoding="utf-8") as f:
            head = f.read(4096)
    except (OSError, lzma.LZMAError, EOFError):
        return ""
    m = re.search(r'"reasy_date"\s*:\s*"([0-9-]+)"', head)
    return m.group(1) if m else ""


def download_registry(folder: Path, opener=None, timeout: float = 30) -> str | None:
    """Fetch the registry kept current in the repository into ``folder`` when it is newer
    than what is there now. Returns its date when it was taken, None otherwise."""
    import urllib.request

    opener = opener or (lambda url: urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "MHWildsModManager"}), timeout=timeout))
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / (REGISTRY_NAME + ".partial")
    try:
        with opener(REGISTRY_URL) as resp, open(partial, "wb") as f:
            while chunk := resp.read(1 << 16):
                f.write(chunk)
        with lzma.open(partial, "rt", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data.get("types"):
            raise ValueError("not a registry")
        date = registry_date(partial)
        if date <= registry_date(TypeRegistry.path()):
            return None
        partial.replace(folder / REGISTRY_NAME)
        return date
    finally:
        partial.unlink(missing_ok=True)


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

    def resources(self) -> set[str]:
        """The file's resource table (what it declares it loads); all strings before the RSZ
        block when the table cannot be read."""
        where = {"user": (4, 16), "pfb": (8, 32), "scn": (8, 32)}[self.kind]
        try:
            count = struct.unpack_from("<I", self.data, where[0])[0]
            table = struct.unpack_from("<Q", self.data, where[1])[0]
            if count > MAX_COUNT or table + count * 8 > self.rsz_off:
                raise ValueError
            out = set()
            for i in range(count):
                start = struct.unpack_from("<I", self.data, table + i * 8)[0]
                end = start
                while end + 1 < self.rsz_off and self.data[end:end + 2] != b"\x00\x00":
                    end += 2
                out.add(self.data[start:end].decode("utf-16-le"))
            return out
        except (struct.error, ValueError, UnicodeDecodeError):
            return self.strings()


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

    def reference(self, kind: str) -> bool:
        """Values that only mean something inside one file: instance indexes, and Resource
        fields read as indexes into the file's own resource table."""
        return kind in REFERENCE_KINDS or kind == "GameObjectRef" or (kind == "Resource" and not self.resource_string)


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


def _read_one(data, pos, end, kind, size, schema, strict=False) -> tuple[bytes, int]:
    if schema.variable(kind):
        count = _u32(data, pos, end)
        length = 4 + count * 2
        if length > 4 + 2 * MAX_COUNT:
            raise LayoutError("implausible string length")
        if strict and count and data[pos + length - 2:pos + length] != b"\x00\x00":
            raise LayoutError("a string that does not end with NUL: misread")  # the game writes the NUL
    else:
        length = size
    if pos + length > end:
        raise LayoutError("past the end")
    return data[pos:pos + length], pos + length


def read_values(data: bytes, pos: int, end: int, fields, schema: Schema, registry: TypeRegistry,
                budget: Budget | None = None, strict: bool = False) -> tuple[list, int, bool]:
    """Values of one instance (or struct) starting at ``pos``: raw bytes for a single field,
    a list of elements for an array (a struct element is a list of values).
    Returns (values, end position, whether every padding byte was zero). ``strict``: reject
    strings without their closing NUL (to tell a layout that only fits by length)."""
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
                    elem, pos, ok = read_values(data, pos, end, sub, schema, registry, budget, strict)
                    clean &= ok
                    elems.append(elem)
            else:
                for _ in range(count):
                    pos, ok = _pad(data, pos, 4 if schema.variable(kind) else align)
                    clean &= ok
                    elem, pos = _read_one(data, pos, end, kind, size, schema, strict)
                    elems.append(elem)
            values.append(elems)
        else:
            if kind.startswith("Struct:"):
                raise LayoutError("a single embedded struct")
            pos, ok = _pad(data, pos, 4 if schema.variable(kind) else align)
            clean &= ok
            value, pos = _read_one(data, pos, end, kind, size, schema, strict)
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
    # "current" | "old" (from the history) | "assumed" (the current one for an older CRC the history
    # does not know) | "short" (that minus its last fields) | "mapped" (values moved to the current fields)
    layout: str = "current"


def walk_current(info: RszInfo, schema: Schema, registry: TypeRegistry,
                 why: list[str] | None = None) -> tuple[list[Instance], bool] | None:
    """Every instance read with the current layout: (instances, padding clean), or None
    when a type is unknown or the data does not end exactly at the end of the file
    (``why`` then gets the reason)."""
    pos, end, clean, out = info.data_start, len(info.data), True, []
    for i, (t, c) in enumerate(info.insts):
        if i == 0 or not t or i in info.external:
            out.append(Instance(i, t, c, None, None, pos, pos))
            continue
        fields = registry.fields(t, schema.alt)
        if fields is None:
            _why(why, f"{registry.name(t)}: 타입 정보가 없음")
            return None
        try:
            values, new, ok = read_values(info.data, pos, end, fields, schema, registry)
        except LayoutError:
            _why(why, f"{registry.name(t)}(#{i})에서 읽기가 어긋남")
            return None
        out.append(Instance(i, t, c, fields, values, pos, new))
        clean &= ok
        pos = new
    if pos != end:
        _why(why, f"마지막 인스턴스 뒤에 {end - pos}바이트가 남음")
        return None
    return out, clean


def _why(why: list[str] | None, text: str) -> None:
    if why is not None:
        why.append(text)


def walk_mod(info: RszInfo, schema: Schema, registry: TypeRegistry, current_crc,
             why: list[str] | None = None) -> list[Instance] | None:
    """The mod's instances, each read with the layout it was written with: the current one
    when its CRC is current, the historical one for an older CRC, or failing those the
    current layout without some of its last fields (fields are added at the end of a class).
    Requires exactly one way of reading the whole data block; None otherwise."""
    end = len(info.data)
    known = _walk_known(info, schema, registry, current_crc, why)
    if known is not None or len(info.insts) > MAX_SEARCH_INSTANCES:
        return known
    budget = Budget()
    solutions: list[list[Instance]] = []

    def candidates(i, t, c):
        out = []
        now = registry.fields(t, schema.alt)
        cur = current_crc(t)
        if now is not None and (cur is None or cur == c):
            return [(now, "current")]  # an unchanged CRC: the type is read as it is now
        old = registry.old_fields(t, c, schema.alt)
        if old is not None:
            return [(old, "old")]
        if now:
            # an older CRC the history does not know: maybe only the CRC changed, maybe fields
            # were added at the end (the only guess made, and only when it is the one way to read the file)
            out.append((now, "assumed"))
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
                values, new, clean = read_values(info.data, pos, end, fields, schema, registry, budget,
                                                 strict=how != "current")
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
        _why(why, "다른 구성도 찾아봤지만 시간 안에 끝나지 않음")
        return None
    if len(solutions) > 1:
        _why(why, "다른 구성으로 읽는 방법이 여럿이라 하나로 정할 수 없음")
    return solutions[0] if len(solutions) == 1 else None


def _walk_known(info: RszInfo, schema: Schema, registry: TypeRegistry, current_crc,
                why: list[str] | None = None) -> list[Instance] | None:
    """walk_mod without guessing: the current layout, or the historical one for an older CRC."""
    pos, end, out = info.data_start, len(info.data), []
    for i, (t, c) in enumerate(info.insts):
        if i == 0 or not t or i in info.external:
            out.append(Instance(i, t, c, None, None, pos, pos))
            continue
        cur = current_crc(t)
        old = registry.old_fields(t, c, schema.alt) if cur is not None and cur != c else None
        if old is not None:
            fields, how = old, "old"
        else:  # current, or an older CRC the history does not know: assume only the CRC changed
            fields, how = registry.fields(t, schema.alt), ("current" if cur in (None, c) else "assumed")
        if fields is None:
            _why(why, f"{registry.name(t)}: 타입 정보가 없음")
            return None
        try:
            values, new, clean = read_values(info.data, pos, end, fields, schema, registry, strict=how != "current")
            if how == "assumed" and not clean:
                raise LayoutError("an assumed layout must leave the padding zero")
        except LayoutError:
            known = "옛 구성" if how == "old" else ("현재 구성" if cur in (None, c) else "옛 구성 기록이 없어 현재 구성")
            _why(why, f"{registry.name(t)}(#{i})을(를) {known}으로 읽지 못함")
            return None
        out.append(Instance(i, t, c, fields, values, pos, new, how))
        pos = new
    if pos != end:
        _why(why, f"마지막 인스턴스 뒤에 {end - pos}바이트가 남음")
        return None
    return out


# ---- comparing with the current game file --------------------------------------------
def choose_schema(donor: RszInfo | None, mod: RszInfo, registry: TypeRegistry, why: list[str] | None = None):
    """The first way of reading fields under which the current game's file reads exactly
    (or, without one, the mod's). Returns (schema, instances of that file) or (None, None)."""
    target = donor if donor is not None else mod
    first: list[str] = []
    # Resource fields are length-prefixed strings in MH Wilds (a real game .pfb reads exactly only
    # that way); the variants are REasy's and MHWmodfixer's native layouts
    for schema in (Schema(True, False), Schema(True, True)):
        walked = walk_current(target, schema, registry, first if not first else None)
        if walked is not None:
            return schema, walked[0]
    _why(why, f"{'게임 원본' if donor is not None else '모드'} 파일을 현재 타입 정보로 정확히 읽지 못함"
              f" ({first[0] if first else '?'})")
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
    detail: str = ""  # why each kind of repair did not apply (stuck files)
    game_unreadable: bool = False  # the game's own file did not read with the registry (it may be out of date)


def _rebuild(info: RszInfo, instances: list[Instance], schema: Schema, registry: TypeRegistry,
             crcs: dict[int, int] | None) -> bytes:
    """``info``'s file with its instance data written again from ``instances`` (each with
    its fields and values) and, when given, every instance's CRC from ``crcs``."""
    out = bytearray()
    for inst in instances:
        if inst.fields is None:
            continue
        if inst.layout != "mapped" and (info.data_start + len(out) - inst.start) % 16 == 0:
            out += info.data[inst.start:inst.end]  # unchanged, same alignment: keep its bytes, padding too
        else:
            write_values(out, info.data_start, inst.fields, inst.values, schema, registry)
    result = bytearray(info.data[:info.data_start]) + out
    if crcs:
        for i, (t, c) in enumerate(info.insts):
            if i and t in crcs and crcs[t] != c:
                struct.pack_into("<I", result, info.inst_table + i * 8 + 4, crcs[t])
    return bytes(result)


def _clean_read(data: bytes, inst: Instance, schema: Schema, registry: TypeRegistry) -> bool:
    try:
        _values, stop, clean = read_values(data, inst.start, len(data), inst.fields, schema, registry, strict=True)
    except LayoutError:
        return False
    return clean and stop == inst.end


def _verify(data: bytes, schema: Schema, registry: TypeRegistry) -> bool:
    info = parse(data)
    return info is not None and walk_current(info, schema, registry) is not None


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


def _norm(name: str) -> str:
    """Older dumps prefixed some field names ("v0_Enabled" for "Enabled")."""
    return re.sub(r"^v\d+_", "", name)


def _same_field(a, b) -> bool:
    return _norm(a[0]) == _norm(b[0]) and a[1] == b[1] and a[2] == b[2] and a[4] == b[4]


def _migrate(mod: RszInfo, walked: list[Instance], donor_insts: list[Instance] | None, schema: Schema,
             registry: TypeRegistry, notes: list[str], why: list[str] | None = None) -> list[Instance] | None:
    """The mod's instances in the current layout. An instance whose bytes read exactly the same
    way under the current layout keeps them (an older dump often only described the same bytes
    with other names or types); otherwise fields are matched by name: the mod's own value for
    every field that still exists, the current game file's value for new ones, and the instance
    is marked "mapped". None when that cannot be done safely (unnamed fields that changed, a new
    field pointing into the game file's own instances or at an undeclared path)."""
    donors = _occurrences(donor_insts or [])
    seen: dict[int, int] = {}
    manifest = {_strip_at(s).lower() for s in mod.resources()}
    out, added, dropped = [], set(), set()
    end = len(mod.data)
    for inst in walked:
        if inst.fields is None or inst.layout in ("current", "assumed"):
            out.append(inst)
            if inst.fields is not None:
                seen[inst.type_id] = seen.get(inst.type_id, 0) + 1
            continue
        now = registry.fields(inst.type_id, schema.alt)
        name = registry.name(inst.type_id)
        if now is None:
            _why(why, f"{name}: 현재 타입 정보가 없음")
            return None
        k = seen.get(inst.type_id, 0)
        seen[inst.type_id] = k + 1
        named = not any(_GENERIC_NAME.match(f[0]) for f in (*inst.fields, *now))
        same_names = [_norm(f[0]) for f in inst.fields] == [_norm(f[0]) for f in now]
        if not named or same_names:
            try:
                values, stop, clean = read_values(mod.data, inst.start, end, now, schema, registry, strict=True)
            except LayoutError:
                stop, clean = -1, False
            if stop == inst.end and clean:  # the same bytes, described differently: keep them
                out.append(Instance(inst.index, inst.type_id, inst.crc, now, values, inst.start, inst.end))
                continue
            if not named:
                _why(why, f"{name}: 이름 없는 필드의 구성이 실제로 바뀌어 맞출 수 없음")
                return None
        pool = donors.get(inst.type_id, [])
        donor = pool[k] if k < len(pool) else (pool[0] if pool else None)
        mine = {_norm(f[0]): (f, v) for f, v in zip(inst.fields, inst.values)}
        theirs = {_norm(f[0]): v for f, v in zip(donor.fields, donor.values)} if donor is not None else {}
        values = []
        for f in now:
            have = mine.get(_norm(f[0]))
            if have is not None and _same_field(have[0], f):
                values.append(have[1])
                continue
            if _norm(f[0]) not in theirs:
                _why(why, f"{name}.{f[0]}: 새 필드인데 게임 원본에 같은 인스턴스가 없음")
                return None  # a new field with no current value to take
            value = theirs[_norm(f[0])]
            if schema.reference(f[1]) and not _is_null(value):
                _why(why, f"{name}.{f[0]}: 새 필드가 게임 원본에만 있는 구성요소를 가리킴")
                return None  # points at instances only the game file has
            if schema.variable(f[1]) and any(p not in manifest for p in _paths_in(value)):
                _why(why, f"{name}.{f[0]}: 새 필드가 모드 파일에 선언되지 않은 경로를 가리킴")
                return None  # would reference a resource the mod's file does not declare
            values.append(value)
            added.add(f"{name}.{f[0]}")
        dropped |= {f"{name}.{f[0]}" for f in inst.fields if not any(_same_field(f, g) for g in now)}
        out.append(Instance(inst.index, inst.type_id, inst.crc, now, values, inst.start, inst.end, "mapped"))
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
    manifest = {_strip_at(s).lower() for s in donor.resources()}
    kept = lost = 0
    out, chains = [], set()
    for inst in donor_insts:
        if inst.fields is None:
            out.append(inst)
            continue
        if registry.name(inst.type_id).startswith(CHAIN_TYPES):
            chains.add(registry.name(inst.type_id))  # rewritten chain settings broke at boot: keep the game's
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
                if schema.reference(f[1]) or f[1].startswith("Struct:") or \
                        (schema.variable(f[1]) and any(p not in manifest for p in _paths_in(have[1]))):
                    lost += 1
                    continue
                values[j] = have[1]
                kept += 1
        out.append(Instance(inst.index, inst.type_id, inst.crc, inst.fields, values, inst.start, inst.end,
                            "mapped" if values != inst.values else inst.layout))
    notes.append(f"모드가 바꾼 값 {kept}개를 옮겼습니다" + (f", {lost}개(다른 구성요소·경로를 가리키는 값)는 게임 원본 값을 씁니다"
                                                    if lost else ""))
    if chains:
        notes.append("체인 물리 설정은 게임 원본 값을 씁니다: " + ", ".join(sorted(chains)[:3]))
    extra = {t: len(pool) - seen.get(t, 0) for t, pool in mods.items() if len(pool) > seen.get(t, 0)}
    if extra:
        notes.append("게임 원본보다 많은 모드 인스턴스 " + ", ".join(f"{registry.name(t)} {n}개" for t, n in sorted(extra.items())[:3])
                     + "는 대응하는 것이 없어 빠집니다")
    return _rebuild(donor, out, schema, registry, None)


def _user_refs(strings: set[str]) -> set[str]:
    return {_strip_at(x) for x in strings if x.lower().endswith(".user")}


def _carry_user_refs(data: bytes, mod: RszInfo, notes: list[str]) -> bytes | None:
    """Keep the .user files the mod's file points at (a borrowed helm keeps another set's hair
    settings that way; MHWmodfixer #30): each reference of the game file the mod replaced is
    overwritten in place with the mod's path of the same length. None when they do not pair
    up that way."""
    out_info = parse(data)
    if out_info is None:
        return None
    theirs, mine = _user_refs(out_info.strings()), _user_refs(mod.strings())
    mine_only, theirs_only = sorted(mine - theirs), sorted(theirs - mine)
    if not mine_only:
        return data
    if len(mine_only) != len(theirs_only):
        return None
    pairs = []
    for path in mine_only:
        same = [t for t in theirs_only if len(t) == len(path) and t not in {a for a, _b in pairs}]
        if len(same) != 1:
            return None
        pairs.append((same[0], path))
    out = bytearray(data)
    for old_path, path in pairs:
        old, new = old_path.encode("utf-16-le"), path.encode("utf-16-le")
        start = 0
        while (i := out.find(old, start)) != -1:
            if i % 2 == 0:
                out[i:i + len(new)] = new
            start = i + 2
        notes.append(f"모드가 가리키던 설정 파일 {path}을(를) 그대로 씁니다")
    return bytes(out)


def plan(label: str, mod_bytes: bytes, donor_bytes: bytes | None, registry: TypeRegistry,
         provided: set[str] | None = None, trust_registry: bool = False) -> RszPlan | None:
    """How to bring one RSZ file up to date with the current game. ``donor_bytes`` is the
    current game's own copy of the same file (None when the game has none); ``provided``
    the lower-cased file names (without version number) the mod ships, for custom slots;
    ``trust_registry``: the registry's CRCs are known to match this game version.
    None when the file is not an RSZ file.

    Rules from MHWmodfixer's in-game testing: a file whose CRCs are all current is never
    touched (a layout the registry cannot read is more likely the registry's fault, and
    dropping or rewriting parts of a working file made models vanish); nothing is written
    unless the current game's own file reads exactly with the registry; instances are never
    added; chain physics settings are never rewritten (rewritten ones broke at boot)."""
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
    if not stale:
        # the game accepts every CRC: leave it as it is. But when the game's own file reads and
        # the mod's (with only types the game file has) does not, the game will misread it.
        if donor is not None and mod.types <= donor.types:
            schema, _ = choose_schema(donor, mod, registry)
            why: list[str] = []
            if schema is not None and walk_current(mod, schema, registry, why) is None:
                result.state = "stuck"
                result.reason = ("CRC는 현재 게임과 같지만 현재 구성으로 읽히지 않습니다. 게임이 멈추면 모드 업데이트를 "
                                 "확인하세요 (모드가 괜찮다면 형식 정보 쪽 문제일 수 있습니다)")
                result.detail = "; ".join(why)
        return result

    result.state = "stuck"
    names = [registry.name(t) for t in stale]
    result.notes.append("CRC가 바뀐 타입: " + ", ".join(names[:5]) + (f" 외 {len(names) - 5}개" if len(names) > 5 else ""))
    trace: list[str] = []

    def stuck(reason: str) -> RszPlan:
        result.reason = reason
        result.detail = "; ".join(dict.fromkeys(trace))
        return result

    schema, donor_insts = choose_schema(donor, mod, registry, trace)
    result.game_unreadable = donor is not None and schema is None
    if schema is None:
        return stuck("게임 원본 파일도 지금 가진 형식 정보로 읽히지 않아 안전하게 고칠 수 없습니다"
                     if donor is not None else "형식 정보로 파일을 읽지 못했습니다")
    mod_now = walk_current(mod, schema, registry, trace)
    crcs = {t: current_crc(t) for t, _c in mod.insts if t and current_crc(t) is not None}
    unknown = {t for i, (t, _c) in enumerate(mod.insts) if i and t and i not in mod.external and t not in crcs}
    if unknown:
        result.notes.append(f"현재 CRC를 확인할 수 없는 타입 {len(unknown)}개는 그대로 두었습니다")
    extra = mod.types - donor.types if donor is not None else set()
    if extra:
        result.notes.append("게임 원본에 없는 구성요소는 모드 것을 그대로 둡니다: " +
                            ", ".join(sorted(registry.name(t) for t in extra)[:3]))

    # 1. the layout is unchanged: only the CRCs. Not when the layout a stale CRC stands for is
    # known to differ (a new 1-byte field can sit where padding was, so the old data still
    # "reads" under the new layout with the wrong meaning); for a CRC the history does not
    # know, the read must also leave every padding byte zero (MHWmodfixer's require_fits).
    olds = {(t, c): registry.old_fields(t, c, schema.alt) for i, (t, c) in enumerate(mod.insts) if i and t in stale}
    now_of = {t: registry.fields(t, schema.alt) or () for t, _c in olds}
    reshaped = sorted({registry.name(t) for (t, c), old in olds.items()
                       if old is not None and [f[1:] for f in old] != [f[1:] for f in now_of[t]]})
    assumed_dirty = mod_now is not None and any(
        not _clean_read(mod.data, inst, schema, registry) for inst in mod_now[0]
        if inst.fields is not None and olds.get((inst.type_id, inst.crc), 0) is None)
    if reshaped:
        trace.append("필드 구성이 바뀐 타입: " + ", ".join(reshaped[:4]) + (f" 외 {len(reshaped) - 4}개" if len(reshaped) > 4 else ""))
    elif assumed_dirty:
        trace.append("옛 구성 기록이 없는 타입을 현재 구성으로 읽으면 빈 칸이 0이 아님")
    if mod_now is not None and not reshaped and not assumed_dirty:
        patched = bytearray(mod.data)  # every byte but the CRCs stays
        for i, (t, c) in enumerate(mod.insts):
            if i and t in crcs and crcs[t] != c:
                struct.pack_into("<I", patched, mod.inst_table + i * 8 + 4, crcs[t])
        if _verify(bytes(patched), schema, registry):
            result.state, result.method, result.data = "fixable", "CRC 갱신", bytes(patched)
            return result

    # 2. some types changed: read each instance as it was written, write it in the current layout
    walked = walk_mod(mod, schema, registry, current_crc, trace)
    if walked is not None:
        notes: list[str] = []
        migrated = _migrate(mod, walked, donor_insts if donor is not None else None, schema, registry, notes, trace)
        chains = sorted({registry.name(i.type_id) for i in migrated or () if i.layout == "mapped"
                         and registry.name(i.type_id).startswith(CHAIN_TYPES)})
        if chains:
            trace.append("체인 물리 설정은 다시 쓰지 않음: " + ", ".join(chains[:3]))
        elif migrated is not None:
            data = _rebuild(mod, migrated, schema, registry, crcs)
            if _verify(data, schema, registry):
                rewritten = sorted({registry.name(i.type_id) for i in migrated if i.layout == "mapped"})
                same = data[mod.data_start:] == mod.data[mod.data_start:]
                missing = sorted({registry.name(t) for t in donor.types - mod.types}) if donor is not None else []
                if missing and mod.kind == "pfb":
                    notes.append("게임 원본에는 있지만 모드에는 없는 구성요소는 넣지 않았습니다: " + ", ".join(missing[:4]) +
                                 (f" 외 {len(missing) - 4}개" if len(missing) > 4 else ""))
                result.state, result.data = "fixable", data
                result.method = "CRC 갱신" if same else "재구성"
                if rewritten:
                    notes.insert(0, "다시 쓴 타입: " + ", ".join(rewritten[:5]) +
                                 (f" 외 {len(rewritten) - 5}개" if len(rewritten) > 5 else ""))
                result.notes += notes
                return result

    # 3. the game's own file, with the mod's values where they still fit
    if donor is None:
        return stuck("게임에 같은 파일이 없어 비교할 수 없습니다")
    if extra:
        return stuck("게임 원본에 없는 구성요소가 들어 있어 원본으로 바꿀 수 없습니다: " +
                     ", ".join(sorted(registry.name(t) for t in extra)[:3]))
    match = find_substitution(mod.strings(), donor.strings())
    if match is None:
        return stuck("게임 원본과 참조하는 파일이 많이 달라 안전하게 판단할 수 없습니다")
    notes = []
    if trace:
        notes.append("다시 쓰지 않은 이유: " + "; ".join(dict.fromkeys(trace)))
    data = _transplant(donor, donor_insts, walked, schema, registry, notes)
    if not _verify(data, schema, registry):
        data = donor.data
        notes = ["모드가 바꾼 값은 옮기지 못하고 게임 원본 값을 씁니다"]
    if match[1] is not None:
        data = apply_substitution(data, match[1][0], match[1][1], provided or set())
        notes.append(f"'{match[1][0]}' 경로를 모드의 '{match[1][1]}'(으)로 바꿨습니다")
    data = _carry_user_refs(data, mod, notes)
    if data is None:
        return stuck("모드가 게임 원본과 다른 설정 파일(.user)을 가리키고 있어 원본으로 바꿀 수 없습니다")
    result.state, result.method, result.data = "fixable", "원본 교체", data
    result.notes += notes
    return result
