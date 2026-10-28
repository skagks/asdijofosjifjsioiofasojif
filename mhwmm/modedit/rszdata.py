"""게임 수치: the values of RSZ data files (.user.N) changed in place (engine "rsz").

A data file is opened only when it reads exactly with the current type registry (rsz.parse plus
choose_schema/walk_current); one that reads only with an older layout of its types raises StaleError
(it needs a matfix repair first), anything else that cannot be read EditError.  Values are named by
text addresses, the keys of the 모드 수정 recipe, that stay valid when rows are added or moved:

    _Values[_ItemId=101]._SellPrice     a field of a table row, the row found by its id
    _Values[_dataId=7,_partsIndex=2]    ... by several fields together when one does not tell rows apart
    _Areas[2]._Min, _GetRank[0]         by position where no field does (and in arrays of plain values)
    _Param._Scale, _Pos~1               a field of the instance a field points at; the 2nd field "_Pos"
    _Values[*]._SellPrice               a rule: every row of that table (whose value is the one it matched)

Row keys are chosen in the file being addressed: id-like fields (…Id, …Type, …No, …Index, …) alone,
then two or three of them together, then ``_Index``, then any other field (GUIDs, strings and enum
values before the ones that can be edited), unique in that table (else the position).  So the same row
may be spelled differently in another version of the file (or by an older key): ``get`` and ``expand``
give each value at its address in this file (the spelling ``values()`` lists), and engines.apply keys
everything by that.  A row key can be changed like any other value: a recipe is always applied to the
file whose rows it names, and a written value is read back at the place it was written (``place``).

Only fixed-size values are written, in place: integers, floats, bools, vectors and colours.  The file
keeps its length, layout, CRCs, strings and resource paths, so matfix judges the output exactly as
the file it was made from (I5).  Every write is checked (V1-V5 in ``DataFile.write``) and every file
opened through the engine checks itself first (``selfcheck``: saving without changes, and rewriting
values with themselves, must give the same bytes; a file that does not is read-only).  Read-only:
enum wrappers (``*_Serializable._Value``, shown at the field that points at them), values of an
instance reached through more than one reference, values outside the file's first root, NaN/Inf, and
every other kind (strings, paths, GUIDs, references, shapes).

``compare`` pairs a mod's file with the game's own copy of it, for display only: the game's value
of each paired value, the rows the game does not have, how many of the game's rows the mod lacks,
and the values the game uses in each table column (the "게임에 쓰인 값" hint).  Table rows are paired
only by fields a mod does not change as values (id-like ones, ``_Index``, GUIDs, strings, enum values),
else by position; ``keyed`` says which values were paired without relying on the rows' order.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import math
import re
import struct
import threading
import uuid
import weakref
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Mapping

from ..matfix import rsz
from ..equip.userdata import external_paths
from . import EditError, StaleError
from .engines import Value, float_text

log = logging.getLogger(__name__)

MAX_LEAVES = 300_000
MAX_FILE = 64 * 1024 * 1024
MAX_HINTS = 12  # distinct values a column may have in the game file to be offered as a hint
SELFCHECK_SCAN = 4000  # values looked at for selfcheck's rewrite (the first editable one of each kind)
CHECKED_KEEP = 32  # files remembered as checked, per registry (a build opens its base and its output again)

INTS = {"S8": "<b", "U8": "<B", "S16": "<h", "U16": "<H", "S32": "<i", "U32": "<I", "S64": "<q", "U64": "<Q"}
FLOATS = {"F16": "<e", "F32": "<f", "F64": "<d"}
# fixed-size compound values: the format of the meaningful part (the field's other bytes are kept)
VECTORS = {"Vec2": "<2f", "Vec3": "<3f", "Vec4": "<4f", "Float2": "<2f", "Float3": "<3f", "Float4": "<4f",
           "Int2": "<2i", "Int3": "<3i", "Int4": "<4i", "Uint2": "<2I", "Uint3": "<3I", "Uint4": "<4I",
           "Quaternion": "<4f", "Range": "<2f", "RangeI": "<2i", "Size": "<2f", "Point": "<2f",
           "Position": "<3d", "Half4": "<4e", "Color": "<4B"}
EDITABLE: frozenset[str] = frozenset(INTS) | frozenset(FLOATS) | {"Bool"} | frozenset(VECTORS)
# shown, never written
SHOWN_INTS = {"C8": "<B", "C16": "<H", "DateTime": "<q"}
SHOWN_NUMBERS = {"Mat4": "<16f", "AABB": "<8f", "OBB": "<20f", "Sphere": "<4f", "Capsule": "<12f",
                 "Cylinder": "<12f", "Triangle": "<12f", "Plane": "<4f", "Frustum": "<24f", "Rect": "<4f",
                 "KeyFrame": "<4f", "Area": "<12f"}
KEY_INTS = set(INTS) | {"C8", "C16"}
_SCALARS = {kind: (struct.Struct(fmt), struct.calcsize(fmt)) for kind, fmt in {**INTS, **FLOATS, **SHOWN_INTS}.items()}
_ELEMENT_KIND = {"f": "F32", "d": "F64", "e": "F16", "i": "S32", "I": "U32", "B": "U8"}
_INT_RANGE = {fmt: (-(1 << (8 * struct.calcsize(fmt) - 1)), (1 << (8 * struct.calcsize(fmt) - 1)) - 1)
              if fmt[-1].islower() else (0, (1 << (8 * struct.calcsize(fmt))) - 1) for fmt in INTS.values()}
KEY_HINT = re.compile(r"(id|fixedid|series|hash|type|kind|no|index)$", re.IGNORECASE)
_GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_DATA_FILE = re.compile(r"\.user\.\d+$", re.IGNORECASE)

STALE = "지금 게임과 형식이 달라 읽지 못했습니다. 재질 점검에서 복구한 뒤에 바꿀 수 있습니다."
REGISTRY_HINT = " 재질 점검을 하면 새 형식 정보를 받아 옵니다."
SELFCHECK_FAILED = "이 파일은 저장해 보니 원래와 달라져 고칠 수 없습니다 (로그 참고)."
NO_GAME = "게임 파일과 비교하지 못해 모든 값을 보여 줍니다."
WHY_OUTSIDE = "첫 번째 루트 밖의 값입니다"
WHY_ENUM = "열거형 값은 아직 바꿀 수 없습니다"
WHY_SHARED = "다른 곳과 함께 쓰는 값이라 바꿀 수 없습니다"
WHY_NAN = "숫자가 아닌 값은 바꾸지 않습니다"
WHY_KIND = "이 종류의 값은 아직 바꿀 수 없습니다"
WHY_REF = "다른 구성요소를 가리키는 값이라 바꿀 수 없습니다"
# shown by the 게임 수치 page (and the CLI) around the values of a data file
STUCK_SUFFIX = " (복구할 수 없는 파일이라 지금은 바꿀 수 없습니다)"
REPAIR_NOTE = "재질 점검에서 복구한 파일을 바탕으로 고칩니다. 복구가 바뀌면 자동으로 다시 적용합니다."
NOT_IN_HINT = "게임에 쓰인 값에 없는 번호입니다."
INT_CAUTION = ("정수 값 가운데에는 종류를 나타내는 번호(아이템·몬스터 번호 등)도 있습니다. 게임에 쓰인 값 밖의 번호로 "
               "바꾸면 게임이 튕길 수 있습니다.")
ONLINE_CAUTION = ("보상 · 피해량 · 체력 같은 값을 바꾼 채 온라인(멀티플레이)에 나가면 다른 사람에게 영향을 주거나 "
                  "제재를 받을 수 있습니다.")


def is_data_file(rel: str) -> bool:
    """A loose data file 게임 수치 offers: ``natives/**.user.N``."""
    low = rel.replace("\\", "/").lstrip("/").lower()
    return low.startswith("natives/") and _DATA_FILE.search(low) is not None


def registry() -> rsz.TypeRegistry:
    """The type registry, kept loaded until the matching release() (rsz.TypeRegistry.hold)."""
    return rsz.TypeRegistry.hold()


def release() -> None:
    """End one registry() (rsz.TypeRegistry.unhold)."""
    rsz.TypeRegistry.unhold()


# ---- one value ---------------------------------------------------------------------------------------
def _fits(kind: str, size: int) -> bool:
    """Whether a field of ``kind`` and ``size`` holds a value this module can write."""
    if kind in INTS:
        return size == struct.calcsize(INTS[kind])
    if kind in FLOATS:
        return size == struct.calcsize(FLOATS[kind])
    if kind == "Bool":
        return size == 1
    if kind in VECTORS:
        return size >= struct.calcsize(VECTORS[kind])
    return False


def _spec(kind: str) -> tuple:
    """Value.spec: (kind, lowest, highest) for integers, (kind, count, format) for vectors, (kind,)."""
    if kind in INTS:
        return (kind, *_INT_RANGE[INTS[kind]])
    if kind in VECTORS:
        fmt = VECTORS[kind]
        return (kind, int(fmt[1:-1]), fmt)
    return (kind,)


def decode(kind: str, size: int, raw: bytes):
    """The value of a field's bytes: int, float, bool, list (vectors and shapes), str (GUID, String,
    Resource with its leading "@"), else the bytes."""
    return _decode(kind.strip(), raw)


def _decode(kind: str, raw: bytes):
    got = _SCALARS.get(kind)
    if got is not None and len(raw) == got[1]:
        return got[0].unpack(raw)[0]
    if kind == "Bool" and raw:
        return raw[0] != 0
    fmt = VECTORS.get(kind) or SHOWN_NUMBERS.get(kind)
    if fmt is not None and len(raw) >= struct.calcsize(fmt):
        return list(struct.unpack_from(fmt, raw))
    if kind in ("Guid", "GameObjectRef") and len(raw) >= 16:
        return str(uuid.UUID(bytes_le=bytes(raw[:16])))
    if kind in ("String", "Resource") and len(raw) >= 4:
        text = bytes(raw[4:]).decode("utf-16-le", "replace")
        nul = text.find("\x00")
        return text if nul < 0 else text[:nul]
    return bytes(raw)


def format_value(kind: str, value) -> str:
    """Canonical text: integers in decimal, true/false, F32/F16 the shortest text that packs back to
    the same bits (engines.float_text), F64 repr, vectors their elements joined by ", "."""
    return _format(kind.strip(), value)


def _format(kind: str, value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if kind == "Bool":
        return "true" if value else "false"
    if kind in INTS or kind in SHOWN_INTS:
        return str(int(value))
    if kind in FLOATS:
        return float_text(float(value), kind)
    fmt = VECTORS.get(kind) or SHOWN_NUMBERS.get(kind)
    if fmt is not None and isinstance(value, (list, tuple)):
        elem = _ELEMENT_KIND[fmt[-1]]
        return ", ".join(_format(elem, x) for x in value)
    return str(value)


def _int_text(text: str) -> int:
    t = text.strip().replace("_", "")
    low = t.lower()
    if low.startswith(("0x", "-0x", "+0x")):
        return int(t, 16)
    if re.fullmatch(r"[+-]?\d+", t) is None:
        raise ValueError(text)
    return int(t)


def parse_value(kind: str, text: str):
    """The value typed (or stored as canonical text) for a field of ``kind``.  Bools take 켬/끔,
    true/false or 1/0; integers decimal or 0x hex, within the kind's range; floats finite and within
    the kind's range; vectors and colours their elements as "a, b, c".  EditError (Korean) otherwise."""
    kind = kind.strip()
    t = str(text).strip()
    if kind == "Bool":
        low = t.lower()
        if low in ("켬", "true", "1"):
            return True
        if low in ("끔", "false", "0"):
            return False
        raise EditError("켬이나 끔(true/false)이어야 합니다.")
    if kind in INTS:
        low, high = _INT_RANGE[INTS[kind]]
        try:
            n = _int_text(t)
        except ValueError:
            raise EditError("정수를 적어 주세요.") from None
        if not low <= n <= high:
            raise EditError(f"{low} ~ {high} 사이의 정수여야 합니다.")
        return n
    if kind in FLOATS:
        try:
            x = float(t)
        except ValueError:
            raise EditError("숫자를 적어 주세요.") from None
        if not math.isfinite(x):
            raise EditError("무한대나 NaN은 쓸 수 없습니다.")
        try:
            packed = struct.unpack(FLOATS[kind], struct.pack(FLOATS[kind], x))[0]
        except (OverflowError, struct.error):
            raise EditError(f"{kind}에 담기에는 너무 큰 수입니다.") from None
        if not math.isfinite(packed):
            raise EditError(f"{kind}에 담기에는 너무 큰 수입니다.")
        return x
    if kind in VECTORS:
        fmt = VECTORS[kind]
        count, elem = int(fmt[1:-1]), _ELEMENT_KIND[fmt[-1]]
        parts = [p for p in re.split(r"\s*,\s*|\s+", t) if p] if t else []
        if len(parts) != count:
            if kind == "Color":
                raise EditError("색은 0~255 사이의 정수 4개를 쉼표로 나눠 적어 주세요 (예: 255, 128, 0, 255).")
            raise EditError(f"값 {count}개를 쉼표로 나눠 적어 주세요 (예: {', '.join(['1'] * count)}).")
        return [parse_value(elem, p) for p in parts]
    raise EditError(WHY_KIND + ".")


def encode(kind: str, size: int | None, value, old: bytes | None = None) -> bytes:
    """The bytes of ``value`` for a field of ``kind``; ``old`` (the bytes it replaces) keeps a vector's
    trailing bytes and a bool's own true byte."""
    kind = kind.strip()
    if kind == "Bool":
        if old is not None and len(old) == 1 and (old[0] != 0) == bool(value):
            return bytes(old)
        return b"\x01" if value else b"\x00"
    if kind in INTS:
        return struct.pack(INTS[kind], value)
    if kind in FLOATS:
        return struct.pack(FLOATS[kind], value)
    if kind in VECTORS:
        fmt = VECTORS[kind]
        head = struct.pack(fmt, *value)
        n = len(head)
        size = n if size is None else size
        rest = old[n:size] if old is not None and len(old) == size else bytes(size - n)
        return head + rest
    raise EditError(WHY_KIND + ".")


def _bits(kind: str, text: str) -> bytes:
    """What two texts of a value are compared by: the packed bytes of its meaningful part."""
    kind = kind.strip()
    value = parse_value(kind, text)
    if kind == "Bool":
        return b"\x01" if value else b"\x00"
    if kind in VECTORS:
        return struct.pack(VECTORS[kind], *value)
    return encode(kind, None, value)


def same_value(kind: str, a: str, b: str) -> bool:
    """Whether two texts are the same value of ``kind`` (floats by their packed bits)."""
    if a == b:
        return True
    if kind.strip() not in EDITABLE:
        return False
    try:
        return _bits(kind, a) == _bits(kind, b)
    except (EditError, struct.error, ValueError, OverflowError):
        return False


def _finite(kind: str, value) -> bool:
    if kind in FLOATS:
        return math.isfinite(value)
    if kind in VECTORS and VECTORS[kind][-1] in "fde":
        return all(math.isfinite(x) for x in value)
    return True


# ---- addresses ---------------------------------------------------------------------------------------
def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _key_text(raw: str) -> str | None:
    """The canonical text of a row key as written in an address (None: not one)."""
    t = raw.strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        out, i = [], 1
        while i < len(t) - 1:
            ch = t[i]
            if ch == "\\":
                if i + 1 >= len(t) - 1 or t[i + 1] not in '\\"':
                    return None
                out.append(t[i + 1])
                i += 2
                continue
            if ch == '"':
                return None
            out.append(ch)
            i += 1
        return _quote("".join(out))
    low = t.lower()
    if low in ("true", "false"):
        return low
    if _GUID.match(t):
        return low
    if re.fullmatch(r"[+-]?\d+", t):
        return str(int(t))
    return None


def _close(at: str, start: int) -> int:
    """The index just past the "]" closing the "[" at ``start`` (outside quotes); -1 when unclosed."""
    i, quoted = start + 1, False
    while i < len(at):
        ch = at[i]
        if quoted:
            if ch == "\\":
                i += 1
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch == "]":
            return i + 1
        i += 1
    return -1


def _split_pairs(body: str) -> list[str]:
    parts, cur, quoted, i = [], [], False, 0
    while i < len(body):
        ch = body[i]
        if quoted:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(body):
                cur.append(body[i + 1])
                i += 1
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
            cur.append(ch)
        elif ch == ",":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def _selector(body: str):
    """("*",), ("i", position) or ("k", ((name, key text), ...)); None when malformed."""
    b = body.strip()
    if b == "*":
        return ("*",)
    if b.isdigit():
        return ("i", int(b))
    pairs = []
    for part in _split_pairs(b):
        name, eq, raw = part.partition("=")
        name = name.strip()
        text = _key_text(raw) if eq else None
        if not name or text is None:
            return None
        pairs.append((name, text))
    return ("k", tuple(pairs))


def parse_address(at: str):
    """[(field name, n-th, selector | None), ...] of an address; None when it is not one."""
    segs, i, n = [], 0, len(at)
    if not at:
        return None
    while True:
        j = i
        while j < n and at[j] not in ".~[]":
            j += 1
        name = at[i:j]
        if not name:
            return None
        nth = 0
        if j < n and at[j] == "~":
            k = j + 1
            while k < n and at[k].isdigit():
                k += 1
            if k == j + 1 or int(at[j + 1:k]) < 1:
                return None
            nth, j = int(at[j + 1:k]), k
        sel = None
        if j < n and at[j] == "[":
            end = _close(at, j)
            if end < 0:
                return None
            sel = _selector(at[j + 1:end - 1])
            if sel is None:
                return None
            j = end
        segs.append((name, nth, sel))
        if j == n:
            return segs
        if at[j] != ".":
            return None
        i = j + 1


def _selectors_in(at: str) -> list[tuple[int, int]]:
    """(start, end) of every "[...]" selector of an address, outside quotes."""
    out, i = [], 0
    while i < len(at):
        if at[i] == "[":
            end = _close(at, i)
            if end < 0:
                break
            out.append((i, end))
            i = end
            continue
        i += 1
    return out


def rule_of(at: str) -> str:
    """The rule address of a value in a table: ``at`` with its last array selector replaced by ``*``
    (``_Values[_ItemId=101]._SellPrice`` → ``_Values[*]._SellPrice``); "" outside tables."""
    spans = _selectors_in(at)
    if not spans:
        return ""
    s, e = spans[-1]
    return at[:s] + "[*]" + at[e:]


def _split_last(at: str) -> tuple[str, str]:
    """(group, label): the address of what holds a value, and its last segment."""
    cut = -1
    i = 0
    while i < len(at):
        ch = at[i]
        if ch == "[":
            end = _close(at, i)
            if end < 0:
                break
            i = end
            continue
        if ch == ".":
            cut = i
        i += 1
    return (at[:cut], at[cut + 1:]) if cut >= 0 else ("", at)


def _seg(name: str, n: int) -> str:
    return name if not n else f"{name}~{n}"


def _ref(raw: bytes) -> int:
    return struct.unpack("<i", raw)[0]


# ---- the document ------------------------------------------------------------------------------------
class _Leaf:
    """Where one value lies: ``path`` (instance, field[, element[, struct field, ...]]) and its bytes."""

    __slots__ = ("path", "kind", "size", "raw", "owner", "folded", "ref")

    def __init__(self, path, kind, size, raw, owner, folded=False, ref=None):
        self.path, self.kind, self.size, self.raw, self.owner = path, kind, size, raw, owner
        self.folded, self.ref = folded, ref


class DataFile:
    """One RSZ data file read exactly with the current registry (engines.Doc).  Raises StaleError when
    the file reads only with an older layout of its types, EditError for anything else it cannot read
    (not a .user file, a RuntimeType field, truncated, over MAX_FILE).

    ``values()`` lists the values from the first root in file order, at most ``max_leaves``
    (``truncated`` then becomes true and a note says so).  ``readonly`` is set (and every value is
    read-only) when the file failed its selfcheck."""

    def __init__(self, data: bytes, registry: rsz.TypeRegistry, max_leaves: int = MAX_LEAVES):
        self.data = bytes(data)
        self.registry = registry
        self.max_leaves = max_leaves
        self.notes: list[str] = []
        self.readonly = ""
        self._values: list[Value] | None = None
        self._truncated = False
        if len(self.data) > MAX_FILE:
            raise EditError(f"파일이 너무 커서({MAX_FILE // (1024 * 1024)}MB 넘음) 열지 않습니다.")
        info = rsz.parse(self.data)
        if info is None or info.kind != "user":
            raise EditError("데이터 파일(.user)이 아니어서 열지 않습니다.")
        if not info.objects or not 0 < info.objects[0] < len(info.insts):
            raise EditError("루트가 없는 데이터 파일이라 열지 않습니다.")
        self.info = info
        self._refuse_runtime_types()
        schema, insts = rsz.choose_schema(None, info, registry)
        if schema is None:
            self._refuse_unreadable()
        self.schema = schema
        self.insts: list[rsz.Instance] = insts
        if self.insts[info.objects[0]].fields is None:
            raise EditError("루트가 없는 데이터 파일이라 열지 않습니다.")
        self.type_name = registry.name(self.insts[info.objects[0]].type_id)
        self.external = external_paths(info) if info.external else {}
        self._layouts: dict[int, tuple] = {}
        self._wrappers: dict[int, bool] = {}
        self._keys: dict[tuple, tuple] = {}
        self._maps: dict[tuple, dict] = {}
        self._shared = self._shared_instances()

    # -- opening
    def _refuse_runtime_types(self) -> None:
        def runtime(fields, depth=0) -> bool:
            for f in fields or ():
                kind = f[1].strip()
                if kind == "RuntimeType":
                    return True
                if kind.startswith("Struct:") and depth < 8 and runtime(self.registry.struct_fields(kind[7:]), depth + 1):
                    return True
            return False

        for t in {t for i, (t, _c) in enumerate(self.info.insts) if i and t and i not in self.info.external}:
            if runtime(self.registry.fields(t)):
                raise EditError("이 파일에는 아직 읽을 수 없는 종류의 값(RuntimeType)이 있어 열지 않습니다.")

    def _refuse_unreadable(self):
        reg, info = self.registry, self.info
        inline = [(t, c) for i, (t, c) in enumerate(info.insts) if i and t and i not in info.external]
        if any(reg.fields(t) is None for t, _c in inline) or \
                any(reg.crc(t) not in (None, c) and reg.old_fields(t, c) is None for t, c in inline):
            raise StaleError(STALE + REGISTRY_HINT)  # types or CRCs the registry does not know: it may be older
        for schema in (rsz.Schema(True, False), rsz.Schema(True, True)):
            if rsz.walk_mod(info, schema, reg, reg.crc) is not None:
                raise StaleError(STALE)
        why: list[str] = []
        rsz.walk_current(info, rsz.Schema(True, False), reg, why)
        raise EditError("파일을 지금 형식 정보로 정확히 읽지 못했습니다" + (f" ({why[0]})" if why else "") + ".")

    def _shared_instances(self) -> set[int]:
        """Instances reached by more than one reference (or only through such an instance)."""
        counts: Counter = Counter(self.info.objects)
        targets: dict[int, list[int]] = {}
        for inst in self.insts:
            if inst.fields is None:
                continue
            refs = [t for kind, t in rsz._ref_values(inst.fields, inst.values, self.schema, self.registry)
                    if kind == "Object" and t]
            targets[inst.index] = refs
            counts.update(refs)
        shared = {i for i, n in counts.items() if n > 1}
        for inst in reversed(self.insts):  # a reference always points at an earlier instance
            if inst.index in shared:
                shared.update(targets.get(inst.index, ()))
        return shared

    def lock(self, why: str) -> None:
        """Make every value read-only (``why`` shown with each)."""
        self.readonly = why
        self._values = None

    # -- helpers
    def _layout(self, fields) -> tuple:
        """(fields, [(name, n-th)] per field, {(name, n-th): field index}), cached per layout."""
        got = self._layouts.get(id(fields))
        if got is None or got[0] is not fields:
            segs, index, seen = [], {}, {}
            for fi, f in enumerate(fields):
                name = rsz._norm(f[0])
                n = seen.get(name, 0)
                seen[name] = n + 1
                segs.append((name, n))
                index[(name, n)] = fi
            got = self._layouts[id(fields)] = (fields, segs, index)
        return got

    def _inline(self, index: int) -> bool:
        return 0 < index < len(self.insts) and self.insts[index].fields is not None

    def _is_wrapper(self, index: int) -> bool:
        """An enum wrapper instance (``app.X.Y_Serializable`` holding one ``_Value``)."""
        got = self._wrappers.get(index)
        if got is None:
            inst = self.insts[index] if self._inline(index) else None
            got = inst is not None and len(inst.fields) == 1 and inst.fields[0][0] == "_Value" and \
                not inst.fields[0][4] and self.registry.name(inst.type_id).endswith("_Serializable")
            self._wrappers[index] = got
        return got

    def _type(self, index: int) -> str:
        return self.registry.name(self.insts[index].type_id) if 0 < index < len(self.insts) else ""

    def _folded(self, target: int) -> _Leaf:
        f = self.insts[target].fields[0]
        return _Leaf((target, 0), f[1].strip(), f[2], self.insts[target].values[0], target, folded=True)

    def _reference(self, path, kind: str, raw: bytes, owner: int) -> _Leaf:
        return _Leaf(path, kind, 4, raw, owner, ref=_ref(raw))

    def _row_keys(self, fields, values) -> dict[str, str]:
        """name → key text of the fields a row may be found by (single ints, bools, GUIDs, strings and
        enum wrapper values; only the first field of each name)."""
        _f, segs, _i = self._layout(fields)
        out = {}
        for (name, n), f, v in zip(segs, fields, values):
            if n or f[4]:
                continue
            kind = f[1].strip()
            if kind == "Object":
                t = _ref(v)
                if not self._is_wrapper(t):
                    continue
                wf = self.insts[t].fields[0]
                kind, v = wf[1].strip(), self.insts[t].values[0]
            if kind in KEY_INTS or kind == "Bool":
                out[name] = _format(kind, _decode(kind, v))
            elif kind == "Guid" and len(v) == 16:
                out[name] = str(uuid.UUID(bytes_le=bytes(v)))
            elif kind == "String":
                out[name] = _quote(_decode(kind, v))
        return out

    def _fixed(self, f, v) -> frozenset:
        """Names among an array's row keys whose values cannot be edited (GUIDs, strings, enum
        wrappers): preferred as keys over fields that can (neither the user nor an update changes them
        as easily), and the only fields other than id-like ones that ``compare`` pairs rows by."""
        kind = f[1].strip()
        if kind.startswith("Struct:"):
            fields = self.registry.struct_fields(kind[7:]) or ()
        else:
            first = next((t for t in map(_ref, v) if self._inline(t) and not self._is_wrapper(t)), 0)
            fields = self.insts[first].fields if first else ()
        return frozenset(rsz._norm(g[0]) for g in fields if not g[4] and g[1].strip() in ("Guid", "String", "Object"))

    def _rows(self, f, v) -> list[dict | None]:
        """The key texts of each element of an Object or Struct array (None: no fields, e.g. null)."""
        kind = f[1].strip()
        if kind.startswith("Struct:"):
            sub = self.registry.struct_fields(kind[7:]) or ()
            return [self._row_keys(sub, elem) for elem in v]
        rows = []
        for raw in v:
            t = _ref(raw)
            if self._inline(t) and not self._is_wrapper(t):
                inst = self.insts[t]
                rows.append(self._row_keys(inst.fields, inst.values))
            else:
                rows.append(None)
        return rows

    def _selectors(self, arr: tuple, f, v) -> tuple[list[str], frozenset, list]:
        """(selector text per element, the key field names, key texts per element) of an Object or
        Struct array, cached per array (``arr``: the path of its field)."""
        got = self._keys.get(arr)
        if got is None:
            rows = self._rows(f, v)
            keys = choose_key(rows, self._fixed(f, v))
            sels = [",".join(f"{k}={row[k]}" for k in keys) if keys and row is not None else str(e)
                    for e, row in enumerate(rows)]
            got = self._keys[arr] = (sels, frozenset(keys), rows)
        return got

    def _lookup(self, arr: tuple, f, v, pairs) -> list[int]:
        """The elements whose fields have the key texts of a selector (lazily indexed per array)."""
        rows = self._selectors(arr, f, v)[2]
        names = tuple(name for name, _t in pairs)
        found = self._maps.get((arr, names))
        if found is None:
            found = {}
            for e, row in enumerate(rows):
                if row is not None and all(name in row for name in names):
                    found.setdefault(tuple(row[name] for name in names), []).append(e)
            self._maps[(arr, names)] = found
        return found.get(tuple(t for _n, t in pairs), [])

    def _why(self, leaf: _Leaf, outside: bool) -> str:
        if self.readonly:
            return self.readonly
        if outside:
            return WHY_OUTSIDE
        if leaf.folded:
            return WHY_ENUM
        if leaf.ref is not None:
            return WHY_REF
        if leaf.owner in self._shared:
            return WHY_SHARED
        if leaf.kind not in EDITABLE or not _fits(leaf.kind, leaf.size):
            return WHY_KIND
        if (leaf.kind in FLOATS or leaf.kind in VECTORS) and not _finite(leaf.kind, _decode(leaf.kind, leaf.raw)):
            return WHY_NAN
        return ""

    def _text(self, leaf: _Leaf) -> str:
        if leaf.ref is not None:
            if not leaf.ref:
                return "없음"
            return "→ " + (self._type(leaf.ref) or f"#{leaf.ref}")
        return _format(leaf.kind, _decode(leaf.kind, leaf.raw))

    def _value(self, at: str, group: str, label: str, rule_at: str, leaf: _Leaf, why: str) -> Value:
        note = ""
        if leaf.ref is not None:
            note = self.external.get(leaf.ref, "")
        elif leaf.folded:
            note = self._type(leaf.owner)
        return Value(at, label, group, leaf.kind, self._text(leaf), editable=not why, why=why, note=note,
                     spec=_spec(leaf.kind), rule_at=rule_at)

    # -- listing
    def _iter_leaves(self):
        """(address, group, label, rule address, leaf, why) of every value, in file order: the first
        root's fields, each instance listed where it is first referenced."""
        visited: set[int] = set()
        for k, root in enumerate(self.info.objects):
            if not self._inline(root) or root in visited:
                continue
            visited.add(root)
            inst = self.insts[root]
            yield from self._leaves((root,), inst.fields, inst.values, root, f"#{k}" if k else "", None,
                                    k > 0, visited)

    def _leaves(self, path, fields, values, owner, addr, rule, outside, visited):
        _f, segs, _i = self._layout(fields)
        base = addr + "." if addr else ""

        def rule_at(at):
            return at[:rule[0]] + "[*]" + at[rule[1]:] if rule else ""

        for fi, f in enumerate(fields):
            v = values[fi]
            name, n = segs[fi]
            seg = _seg(name, n)
            here = base + seg
            kind = f[1].strip()
            if f[4]:
                cut = len(here)
                if kind.startswith("Struct:"):
                    sub = self.registry.struct_fields(kind[7:]) or ()
                    sels = self._selectors(path + (fi,), f, v)[0]
                    for e, elem in enumerate(v):
                        at = f"{here}[{sels[e]}]"
                        yield from self._leaves(path + (fi, e), sub, elem, owner, at, (cut, len(at)), outside,
                                                visited)
                elif kind == "Object":
                    sels = self._selectors(path + (fi,), f, v)[0]
                    for e, raw in enumerate(v):
                        at = f"{here}[{sels[e]}]"
                        t = _ref(raw)
                        if self._is_wrapper(t):
                            leaf = self._folded(t)
                        elif self._inline(t) and t not in visited:
                            visited.add(t)
                            inst = self.insts[t]
                            yield from self._leaves((t,), inst.fields, inst.values, t, at, (cut, len(at)), outside,
                                                    visited)
                            continue
                        else:
                            leaf = self._reference(path + (fi, e), kind, raw, owner)
                        yield at, addr, f"{seg}[{sels[e]}]", at[:cut] + "[*]", leaf, self._why(leaf, outside)
                else:
                    for e, raw in enumerate(v):
                        at = f"{here}[{e}]"
                        if kind == "UserData":
                            leaf = self._reference(path + (fi, e), kind, raw, owner)
                        else:
                            leaf = _Leaf(path + (fi, e), kind, f[2], raw, owner)
                        yield at, addr, f"{seg}[{e}]", at[:cut] + "[*]", leaf, self._why(leaf, outside)
                continue
            if kind == "Object":
                t = _ref(v)
                if self._is_wrapper(t):
                    leaf = self._folded(t)
                elif self._inline(t) and t not in visited:
                    visited.add(t)
                    inst = self.insts[t]
                    yield from self._leaves((t,), inst.fields, inst.values, t, here, rule, outside, visited)
                    continue
                else:
                    leaf = self._reference(path + (fi,), kind, v, owner)
            elif kind == "UserData":
                leaf = self._reference(path + (fi,), kind, v, owner)
            else:
                leaf = _Leaf(path + (fi,), kind, f[2], v, owner)
            yield here, addr, seg, rule_at(here), leaf, self._why(leaf, outside)

    def values(self) -> list[Value]:
        """Every value from the first root on (then the other roots, read-only), at most max_leaves."""
        if self._values is None:
            out: list[Value] = []
            self._truncated = False
            for at, group, label, rule_at, leaf, why in self._iter_leaves():
                if len(out) >= self.max_leaves:
                    self._truncated = True
                    note = f"값이 너무 많아 앞쪽 {self.max_leaves:,}개만 읽었습니다"
                    if note not in self.notes:
                        self.notes.append(note)
                    break
                out.append(self._value(at, group, label, rule_at, leaf, why))
            self._values = out
        return self._values

    @property
    def truncated(self) -> bool:
        self.values()
        return self._truncated

    # -- addresses
    def _find(self, at: str):
        """[(leaf, why, address, star)] of an address (several for a rule): ``address`` is the value's
        address in this file, spelled as values() lists it (a row may have been found by another key or
        by its position), ``star`` the (start, end) in it of the selector a "*" stands for (None without
        one).  None when the address is malformed."""
        segs = parse_address(at)
        if segs is None:
            return None
        k = 0
        if segs[0][0].startswith("#"):
            if not segs[0][0][1:].isdigit() or segs[0][2] is not None or segs[0][1] or len(segs) < 2:
                return None
            k, segs = int(segs[0][0][1:]), segs[1:]
            if k < 1:
                return None
        if k >= len(self.info.objects) or not self._inline(self.info.objects[k]):
            return []
        root = self.info.objects[k]
        inst = self.insts[root]
        out: list = []
        self._descend((root,), inst.fields, inst.values, root, segs, 0, k > 0, f"#{k}" if k else "", None, out)
        return out

    def _descend(self, path, fields, values, owner, segs, i, outside, addr, star, out) -> None:
        """One segment of ``_find``: ``addr`` is the address so far as values() spells it."""
        name, nth, sel = segs[i]
        fi = self._layout(fields)[2].get((name, nth))
        if fi is None:
            return
        f, v = fields[fi], values[fi]
        kind = f[1].strip()
        last = i == len(segs) - 1
        at = (addr + "." if addr else "") + _seg(name, nth)
        if f[4]:
            if sel is None:
                return
            here = path + (fi,)
            if kind.startswith("Struct:") or kind == "Object":
                sels = self._selectors(here, f, v)[0]
                if sel[0] == "*":
                    chosen = range(len(v))
                elif sel[0] == "i":
                    chosen = [sel[1]] if sel[1] < len(v) else []
                else:
                    chosen = self._lookup(here, f, v, sel[1])
                for e in chosen:
                    row = f"{at}[{sels[e]}]"
                    mark = (len(at), len(row)) if sel[0] == "*" else star
                    if kind.startswith("Struct:"):
                        if not last:
                            sub = self.registry.struct_fields(kind[7:]) or ()
                            self._descend(here + (e,), sub, v[e], owner, segs, i + 1, outside, row, mark, out)
                        continue
                    t = _ref(v[e])
                    if self._is_wrapper(t):
                        if last:
                            leaf = self._folded(t)
                            out.append((leaf, self._why(leaf, outside), row, mark))
                    elif self._inline(t):  # a row: its fields are the values
                        if not last:
                            inst = self.insts[t]
                            self._descend((t,), inst.fields, inst.values, t, segs, i + 1, outside, row, mark, out)
                    elif last:
                        leaf = self._reference(here + (e,), kind, v[e], owner)
                        out.append((leaf, self._why(leaf, outside), row, mark))
                return
            if not last or sel[0] == "k":
                return
            chosen = range(len(v)) if sel[0] == "*" else ([sel[1]] if sel[1] < len(v) else [])
            for e in chosen:
                if kind == "UserData":
                    leaf = self._reference(here + (e,), kind, v[e], owner)
                else:
                    leaf = _Leaf(here + (e,), kind, f[2], v[e], owner)
                row = f"{at}[{e}]"
                out.append((leaf, self._why(leaf, outside), row, (len(at), len(row)) if sel[0] == "*" else star))
            return
        if sel is not None:
            return
        if kind == "Object":
            t = _ref(v)
            if self._is_wrapper(t):
                if last:
                    leaf = self._folded(t)
                    out.append((leaf, self._why(leaf, outside), at, star))
            elif self._inline(t):  # an instance: its fields are the values
                if not last:
                    inst = self.insts[t]
                    self._descend((t,), inst.fields, inst.values, t, segs, i + 1, outside, at, star, out)
            elif last:
                leaf = self._reference(path + (fi,), kind, v, owner)
                out.append((leaf, self._why(leaf, outside), at, star))
            return
        if not last:
            return
        if kind == "UserData":
            leaf = self._reference(path + (fi,), kind, v, owner)
        else:
            leaf = _Leaf(path + (fi,), kind, f[2], v, owner)
        out.append((leaf, self._why(leaf, outside), at, star))

    def get(self, at: str) -> Value | None:
        """The value at an address (None when there is none, or more than one), with ``at`` as values()
        spells it: a row named by another of its keys, an older one or its position comes back under
        the key this file names it by."""
        found = self._find(at)
        if not found or len(found) != 1:
            return None
        leaf, why, here, _star = found[0]
        group, label = _split_last(here)
        return self._value(here, group, label, rule_of(here), leaf, why)

    def expand(self, rule_at: str, before: str) -> list[Value]:
        """The values a rule (an address with one ``[*]``) covers whose value is ``before`` (the same
        bytes), each at its own address as values() spells it; their ``rule_at`` is the rule spelled the
        same way."""
        if sum(1 for s, e in _selectors_in(rule_at) if rule_at[s:e] == "[*]") != 1:
            return []
        out = []
        for leaf, why, here, star in self._find(rule_at) or ():
            group, label = _split_last(here)
            rule = here[:star[0]] + "[*]" + here[star[1]:] if star else rule_of(here)
            value = self._value(here, group, label, rule, leaf, why)
            if same_value(value.kind, value.text, before):
                out.append(value)
        return out

    def place(self, at: str):
        """Where the value at an address lies (its path through instances, fields and elements), or None
        when the address names no single value.  A file written from this one holds the value written
        there at the same place (``at_place``), whatever its rows are named by there."""
        found = self._find(at)
        if not found or len(found) != 1:
            return None
        return found[0][0].path

    def at_place(self, place) -> Value | None:
        """The value at a ``place`` (from ``place`` of this file or of the one it was written from), with
        its kind and text only; None when there is none."""
        leaf = self._leaf_at(tuple(place))
        return None if leaf is None else Value("", "", "", leaf.kind, self._text(leaf))

    def _leaf_at(self, path: tuple) -> _Leaf | None:
        if not path or not self._inline(path[0]):
            return None
        index = path[0]
        inst = self.insts[index]
        fields, values, rest = inst.fields, inst.values, path[1:]
        while rest:
            fi = rest[0]
            if not 0 <= fi < len(fields):
                return None
            f, v = fields[fi], values[fi]
            kind = f[1].strip()
            if not f[4]:
                if len(rest) != 1:
                    return None
                raw = v
            else:
                if len(rest) < 2 or not 0 <= rest[1] < len(v):
                    return None
                if kind.startswith("Struct:"):
                    fields, values, rest = self.registry.struct_fields(kind[7:]) or (), v[rest[1]], rest[2:]
                    continue
                if len(rest) != 2:
                    return None
                raw = v[rest[1]]
            if kind in ("Object", "UserData"):
                return self._reference(path, kind, raw, index)
            return _Leaf(path, kind, f[2], raw, index)
        return None

    # -- writing
    def write(self, changes: Mapping[str, str]) -> bytes:
        """The file with each ``address → canonical text`` set in place, checked (V1-V5)."""
        if self.readonly:
            raise EditError(self.readonly)
        edits: dict[tuple, tuple[_Leaf, bytes, str]] = {}
        for at, text in changes.items():
            found = self._find(at)
            if not found or len(found) != 1:
                raise EditError(f"바꿀 값을 찾지 못했습니다: {at}")
            leaf, why, _here, _star = found[0]
            if why:
                raise EditError(f"바꿀 수 없는 값입니다: {at} ({why})")
            new = encode(leaf.kind, leaf.size, parse_value(leaf.kind, text), leaf.raw)
            if len(new) != len(leaf.raw):
                raise EditError(f"값의 크기가 맞지 않습니다: {at}")
            prev = edits.get(leaf.path)
            if prev is not None and prev[1] != new:
                raise EditError(f"같은 값을 서로 다르게 바꾸려고 했습니다: {at}")
            edits[leaf.path] = (leaf, new, at)
        spans = self._spans(edits)
        writes = []
        for p, (leaf, new, at) in edits.items():
            start, end = spans[p]
            if self.data[start:end] != leaf.raw or end - start != len(new):
                raise EditError(f"값의 자리를 잘못 찾았습니다: {at}")
            writes.append((start, end, new))
        out = _patch(self.data, writes)
        self._check(out, edits, [(s, e) for s, e, _n in writes])
        if edits:
            _remember(self.registry, out)  # V1-V5 passed: what selfcheck would find on it
        return out

    def _spans(self, edits) -> dict[tuple, tuple[int, int]]:
        """(start, end) in the file of every value to set, found as rsz.read_values reads them."""
        spans: dict[tuple, tuple[int, int]] = {}
        by_instance: dict[int, list[tuple]] = {}
        for p in edits:
            by_instance.setdefault(p[0], []).append(p)
        for index, paths in sorted(by_instance.items()):
            inst = self.insts[index]
            found: dict[tuple, tuple[int, int]] = {}
            locate(self.data, inst.start, len(self.data), inst.fields, self.schema, self.registry, (index,), found)
            for p in paths:
                if p not in found:
                    raise EditError("값의 자리를 찾지 못했습니다")
                spans[p] = found[p]
        return spans

    def _check(self, out: bytes, edits, spans) -> None:
        """V1 same length; V3 reads exactly with the same schema, same instances and CRCs; V5 passes
        rsz._verify; V2 no byte differs outside the values set; V4 every instance holds the values it
        held, except those set, which hold exactly the new bytes."""
        def fail(code: str, why: str):
            raise EditError(f"바꾼 파일을 확인하지 못해 저장하지 않았습니다: {why} ({code})")

        if len(out) != len(self.data):
            fail("V1", "파일 크기가 달라졌습니다")
        info = rsz.parse(out)
        walked = rsz.walk_current(info, self.schema, self.registry) if info is not None else None
        if walked is None:
            fail("V3", "바꾼 파일을 다시 읽지 못했습니다")
        if info.insts != self.info.insts or info.objects != self.info.objects or info.data_start != self.info.data_start:
            fail("V3", "인스턴스 목록이 달라졌습니다")
        if not rsz._verify(out, self.schema, self.registry, None, source=self.info):
            fail("V5", "형식 점검을 통과하지 못했습니다")
        probe = bytearray(out)
        for start, end in spans:
            probe[start:end] = self.data[start:end]
        if probe != self.data:
            fail("V2", "바꾼 값 밖의 바이트가 달라졌습니다")
        by_instance: dict[int, list] = {}
        for p, (_leaf, new, _at) in edits.items():
            by_instance.setdefault(p[0], []).append((p[1:], new))
        for old, new in zip(self.insts, walked[0]):
            if old.fields is None:
                if new.fields is not None:
                    fail("V4", "인스턴스 구성이 달라졌습니다")
                continue
            want = old.values
            if old.index in by_instance:
                want = copy.deepcopy(want)
                for rest, raw in by_instance[old.index]:
                    _put(want, rest, raw)
            if new.values != want:
                fail("V4", f"{self.registry.name(old.type_id)}(#{old.index})의 값이 기대와 다릅니다")

    def selfcheck(self) -> None:
        """Saving without changes, and rewriting values with their own value (the first editable one of
        each kind), must give the input back; else EditError (SELFCHECK_FAILED, details logged)."""
        try:
            if self.write({}) != self.data:
                raise EditError("바꾼 것 없이 저장한 파일이 원래와 다릅니다")
            picks: dict[str, tuple[str, str]] = {}
            for n, (at, _group, _label, _rule, leaf, why) in enumerate(self._iter_leaves()):
                if n >= SELFCHECK_SCAN:
                    break
                if not why and leaf.kind not in picks:
                    picks[leaf.kind] = (at, self._text(leaf))
            if picks and self.write(dict(picks.values())) != self.data:
                raise EditError("같은 값으로 다시 쓴 파일이 원래와 다릅니다")
        except EditError as exc:
            log.warning("모드 수정: 데이터 파일 %s이(가) 자체 점검을 통과하지 못했습니다: %s", self.type_name, exc)
            raise EditError(SELFCHECK_FAILED) from exc


def _put(values: list, rest: tuple, raw: bytes) -> None:
    """Set the value at ``rest`` (field[, element[, struct field, ...]]) of an instance's values."""
    cont, key = values, rest[0]
    rest = rest[1:]
    while rest:
        arr = cont[key]
        if len(rest) == 1:
            cont, key, rest = arr, rest[0], ()
        else:
            cont, key, rest = arr[rest[0]], rest[1], rest[2:]
    cont[key] = raw


def _patch(data: bytes, writes) -> bytes:
    """``data`` with each (start, end, new bytes) written over (the in-place patcher)."""
    out = bytearray(data)
    for start, end, new in writes:
        out[start:end] = new
    return bytes(out)


def locate(data: bytes, pos: int, end: int, fields, schema, registry, prefix: tuple, out: dict) -> int:
    """rsz.read_values, recording where each value lies: out[path] = (start, end)."""
    for fi, (_name, kind, size, align, is_array) in enumerate(fields):
        p = prefix + (fi,)
        if is_array:
            pos, _clean = rsz._pad(data, pos, 4)
            count = rsz._u32(data, pos, end)
            pos += 4
            if kind.startswith("Struct:"):
                sub = registry.struct_fields(kind[7:])
                if count:
                    pos, _clean = rsz._pad(data, pos, align)
                for e in range(count):
                    pos = locate(data, pos, end, sub, schema, registry, p + (e,), out)
            else:
                for e in range(count):
                    pos, _clean = rsz._pad(data, pos, 4 if schema.variable(kind) else align)
                    start = pos
                    _raw, pos = rsz._read_one(data, pos, end, kind, size, schema)
                    out[p + (e,)] = (start, pos)
        else:
            pos, _clean = rsz._pad(data, pos, 4 if schema.variable(kind) else align)
            start = pos
            _raw, pos = rsz._read_one(data, pos, end, kind, size, schema)
            out[p] = (start, pos)
    return pos


def _candidates(names: list[str], fixed=frozenset()) -> list[tuple[str, ...]]:
    """Row keys to try, in order: id-like fields alone, then two or three together, then ``_Index``,
    then any other field (those that cannot be edited first)."""
    index = [k for k in names if k.lower() == "_index"]
    hinted = [k for k in names if KEY_HINT.search(k) and k not in index]
    others = sorted((k for k in names if k not in hinted and k not in index), key=lambda k: k not in fixed)
    return [(k,) for k in hinted] + [c for size in (2, 3) for c in combinations(hinted[:8], size)] + \
        [(k,) for k in index + others]


def choose_key(rows: list[dict | None], fixed=frozenset()) -> tuple[str, ...]:
    """The fields that tell the rows of a table apart (() for their position), the first of
    _candidates unique among the rows that have fields (the others are found by position).  ``fixed``:
    the names whose values cannot be edited."""
    keyable = [r for r in rows if r is not None]
    if not keyable:
        return ()
    names = [k for k in keyable[0] if all(k in r for r in keyable)]

    def unique(keys) -> bool:
        seen = set()
        for r in keyable:
            value = tuple(r[k] for k in keys)
            if value in seen:
                return False
            seen.add(value)
        return True

    return next((keys for keys in _candidates(names, fixed) if unique(keys)), ())


# ---- the engine ----------------------------------------------------------------------------------------
_CHECKED: "weakref.WeakKeyDictionary[rsz.TypeRegistry, OrderedDict]" = weakref.WeakKeyDictionary()
_CHECKED_LOCK = threading.Lock()


def _digest(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def _remember(registry, data: bytes) -> None:
    """``data`` passed selfcheck (or V1-V5) with ``registry``: the next open need not check it again."""
    with _CHECKED_LOCK:
        seen = _CHECKED.setdefault(registry, OrderedDict())
        seen[_digest(data)] = True
        seen.move_to_end(_digest(data))
        while len(seen) > CHECKED_KEEP:
            seen.popitem(last=False)


def _checked(registry, data: bytes) -> bool:
    with _CHECKED_LOCK:
        seen = _CHECKED.get(registry)
        return seen is not None and _digest(data) in seen


def root_type(data: bytes, registry) -> str:
    """The type name of an RSZ file's first root, read from its tables only ("" when it is not one):
    pakedit.members' ``type_of`` for the labels of PAK entries."""
    info = rsz.parse(data)
    if info is None or not info.objects or not 0 < info.objects[0] < len(info.insts):
        return ""
    type_id = info.insts[info.objects[0]][0]
    return registry.name(type_id) if registry.crc(type_id) is not None else ""


class RszEngine:
    """engines.Engine for RSZ data files.  ``open`` holds the registry while it reads (callers that open
    many files hold it themselves with registry()) and runs the file's selfcheck once per content: a
    file that fails it opens read-only (``DataFile.readonly``)."""

    name = "rsz"
    tool = "data"

    def open(self, data: bytes, rel: str) -> DataFile:
        reg = rsz.TypeRegistry.hold()
        try:
            doc = DataFile(data, reg)
            if not _checked(reg, doc.data):
                try:
                    doc.selfcheck()
                except EditError as exc:
                    doc.lock(str(exc))
                else:
                    _remember(reg, doc.data)
            return doc
        finally:
            rsz.TypeRegistry.unhold()

    def parse(self, value: Value, text: str) -> str:
        kind = value.kind.strip()
        return format_value(kind, parse_value(kind, text))

    def same(self, value: Value, a: str, b: str) -> bool:
        return same_value(value.kind, a, b)


# ---- comparing with the game's own file ------------------------------------------------------------------
@dataclass
class GameValues:
    """What the game's own copy of a data file holds, by the mod file's addresses (display only)."""

    values: dict[str, str] = field(default_factory=dict)  # address → the game's value (paired values only)
    added: frozenset[str] = frozenset()  # groups (row addresses) of rows the game's file lacks, nested ones too
    removed_rows: int = 0  # rows of the game's file the mod's lacks (the mod left them out, it seems)
    game_only_rows: int = 0  # ... at the end of a table or in a file made for another game version
    hints: dict[str, list[str]] = field(default_factory=dict)  # rule address → distinct game values
    note: str = ""  # why nothing was compared ("" when compared)
    # addresses of ``values`` whose table rows were all paired by a key unique in both files (values in no
    # table count, and so do the elements of plain arrays, which have nothing but their position):
    # 게임 값의 몇 배 and 모드가 바꾼 만큼의 몇 배 use only these (rows paired by position may have been
    # moved by the mod)
    keyed: frozenset[str] = frozenset()

    @property
    def compared(self) -> bool:
        return not self.note

    def notes(self) -> list[str]:
        """The lines the page shows about the comparison."""
        if self.note:
            return [self.note]
        out = []
        if self.removed_rows:
            out.append(f"모드가 뺀 행 {self.removed_rows}개 (바꿀 수 없음)")
        if self.game_only_rows:
            out.append(f"게임에 새로 생긴 행 {self.game_only_rows}개가 모드 파일에는 없습니다 (게임 업데이트 뒤 모드가 "
                       "아직 업데이트되지 않았을 수 있습니다)")
        return out

    def hint(self, rule_at: str) -> str:
        """"게임에 쓰인 값: 0, 1, 2, 5" for a table column ("" without one)."""
        values = self.hints.get(rule_at) if rule_at else None
        return "게임에 쓰인 값: " + ", ".join(values) if values else ""

    def outside_hint(self, value, text: str) -> bool:
        """Whether an integer's new value is one the game does not use in that column (NOT_IN_HINT)."""
        values = self.hints.get(value.rule_at) if value.rule_at else None
        if not values or value.kind not in INTS:
            return False
        return not any(same_value(value.kind, text, v) for v in values)


def compare(data: bytes, game_data: bytes | None, registry, doc: DataFile | None = None) -> GameValues:
    """Pair the mod's file (``data``, or the open ``doc``) with the game's (``game_data``) from their
    first roots: fields by name (and n-th), table rows by a key unique in both files (_pair_rows), else by
    position when both tables are as long, plain arrays by position when as long; pairing stops where
    the types differ.  Never raises: what cannot be compared comes back with ``note``."""
    if game_data is None:
        return GameValues(note=NO_GAME)
    try:
        mod = doc if doc is not None else DataFile(data, registry)
        try:
            game = DataFile(game_data, registry)
        except EditError as exc:
            log.info("모드 수정: 게임 파일을 읽지 못해 비교하지 않았습니다: %s", exc)
            return GameValues(note="게임 파일을 지금 형식 정보로 읽지 못해 비교하지 못했습니다. 모든 값을 보여 줍니다.")
        pairing = _Pairing(mod, game)
        if not pairing.run():
            return GameValues(note="게임 파일과 구성이 달라 비교하지 못했습니다. 모든 값을 보여 줍니다.")
        return GameValues(pairing.values, frozenset(pairing.added), pairing.removed, pairing.game_only,
                          pairing.hint_lists(), "", frozenset(pairing.keyed))
    except Exception:  # display only: never stop the page
        log.exception("모드 수정: 게임 파일과 비교하지 못했습니다")
        return GameValues(note="게임 파일과 비교하지 못했습니다. 모든 값을 보여 줍니다.")


class _Pairing:
    """Walks the mod's file the way DataFile.values() lists it (so the addresses are the same) with the
    game's matching instance beside it."""

    def __init__(self, mod: DataFile, game: DataFile):
        self.mod, self.game = mod, game
        self.values: dict[str, str] = {}
        self.keyed: set[str] = set()
        self.added: set[str] = set()
        self.removed = self.game_only = 0
        self.hints: dict[str, Counter | None] = {}
        self.seen: set[int] = set()
        mod_crcs, game_crcs = rsz.crc_map(mod.info), rsz.crc_map(game.info)
        self.other_version = any(game_crcs.get(t, c) != c for t, c in mod_crcs.items())

    def run(self) -> bool:
        m_roots, g_roots = self.mod.info.objects, self.game.info.objects
        if self.mod.insts[m_roots[0]].type_id != self.game.insts[g_roots[0]].type_id:
            return False
        for k, root in enumerate(m_roots):
            if not self.mod._inline(root) or root in self.seen:
                continue
            g = g_roots[k] if k < len(g_roots) else 0
            self.instance(root, g, f"#{k}" if k else "", False, (), True)
        return True

    # -- the game's side
    def _game_inst(self, index: int | None, type_id: int):
        game = self.game
        if index is None or not game._inline(index) or game._is_wrapper(index):
            return None
        inst = game.insts[index]
        return inst if inst.type_id == type_id else None

    def _hint(self, rule_at: str, kind: str, text: str) -> None:
        if not (kind in INTS or kind == "Bool"):
            return
        counter = self.hints.get(rule_at, Counter())
        if counter is None:
            return
        counter[text] += 1
        self.hints[rule_at] = counter if len(counter) <= MAX_HINTS else None

    def _collect(self, fields, values, prefix: str, depth: int = 0) -> None:
        """Hints from one game row: its values and those of instances its fields point at (not arrays)."""
        game = self.game
        _f, segs, _i = game._layout(fields)
        for (name, n), f, v in zip(segs, fields, values):
            if f[4]:
                continue
            kind = f[1].strip()
            at = f"{prefix}.{_seg(name, n)}"
            if kind == "Object":
                t = _ref(v)
                if game._inline(t) and not game._is_wrapper(t) and depth < 16:
                    inst = game.insts[t]
                    self._collect(inst.fields, inst.values, at, depth + 1)
            elif kind in INTS or kind == "Bool":
                self._hint(at, kind, _format(kind, _decode(kind, v)))

    def hint_lists(self) -> dict[str, list[str]]:
        def order(item):
            text, count = item
            try:
                return (-count, 0, int(text), "")
            except ValueError:
                return (-count, 1, 0, text)

        return {at: [t for t, _n in sorted(c.items(), key=order)] for at, c in self.hints.items() if c}

    # -- walking (``keyed``: every table row on the way was paired by a key)
    def _pair(self, at: str, text: str, keyed: bool) -> None:
        self.values[at] = text
        if keyed:
            self.keyed.add(at)

    def instance(self, mi: int, gi: int | None, addr: str, adding: bool, skip, keyed: bool) -> None:
        self.seen.add(mi)
        m = self.mod.insts[mi]
        g = self._game_inst(gi, m.type_id) if not adding else None
        self.container((mi,), m.fields, m.values, g.fields if g else None, g.values if g else None, addr,
                       adding, skip, keyed)

    def container(self, path, fields, values, gfields, gvalues, addr, adding, skip, keyed) -> None:
        mod, game = self.mod, self.game
        if adding:
            self.added.add(addr)
        _f, segs, _i = mod._layout(fields)
        gindex = game._layout(gfields)[2] if gfields is not None else {}
        base = addr + "." if addr else ""
        for fi, f in enumerate(fields):
            v = values[fi]
            name, n = segs[fi]
            here = base + _seg(name, n)
            gfi = gindex.get((name, n))
            gf = gfields[gfi] if gfi is not None else None
            gv = gvalues[gfi] if gf is not None and rsz._same_field(gf, f) and (name, n) not in skip else None
            kind = f[1].strip()
            if f[4]:
                self.array(path + (fi,), f, v, gv, here, adding, keyed)
                continue
            if kind == "Object":
                t = _ref(v)
                if mod._is_wrapper(t):
                    gt = _ref(gv) if gv is not None else 0
                    if gv is not None and game._is_wrapper(gt):
                        self._pair(here, game._text(game._folded(gt)), keyed)
                elif mod._inline(t) and t not in self.seen:
                    self.instance(t, _ref(gv) if gv is not None else None, here, adding, (), keyed)
                elif gv is not None:
                    self._pair(here, game._text(game._reference((), kind, gv, 0)), keyed)
            elif gv is not None:
                if kind == "UserData":
                    self._pair(here, game._text(game._reference((), kind, gv, 0)), keyed)
                else:
                    self._pair(here, game._text(_Leaf((), kind, f[2], gv, 0)), keyed)

    def array(self, arr, f, v, gv, here, adding, keyed) -> None:
        mod, game = self.mod, self.game
        kind = f[1].strip()
        if not (kind.startswith("Struct:") or kind == "Object"):
            if gv is not None:
                for raw in gv:
                    if kind in INTS or kind == "Bool":
                        self._hint(here + "[*]", kind, _format(kind, _decode(kind, raw)))
                if len(gv) == len(v):  # (a plain array's elements are told apart only by position)
                    for e, raw in enumerate(gv):
                        leaf = game._reference((), kind, raw, 0) if kind == "UserData" else _Leaf((), kind, f[2], raw, 0)
                        self._pair(f"{here}[{e}]", game._text(leaf), keyed)
            return
        sels, mkeys, mrows = mod._selectors(arr, f, v)
        struct_kind = kind.startswith("Struct:")
        sub = self.mod.registry.struct_fields(kind[7:]) or () if struct_kind else ()
        pairs: dict[int, int] = {}
        new_rows: set[int] = set()
        skip: tuple = ()
        by_key = False
        if gv is not None:
            grows = game._rows(f, gv)
            for e, raw in enumerate(gv):  # the game's values per column
                if struct_kind:
                    self._collect(sub, raw, here + "[*]")
                else:
                    t = _ref(raw)
                    if game._inline(t) and not game._is_wrapper(t):
                        self._collect(game.insts[t].fields, game.insts[t].values, here + "[*]")
            pairs, new_rows, skip, by_key = self._pair_rows(mrows, grows, mkeys, len(v), len(gv),
                                                            mod._fixed(f, v))
        row_keyed = keyed and by_key
        for e in range(len(v)):
            at = f"{here}[{sels[e]}]"
            ge = pairs.get(e)
            row_added = adding or e in new_rows
            if struct_kind:
                self.container(arr + (e,), sub, v[e], sub if ge is not None else None,
                               gv[ge] if ge is not None else None, at, row_added, skip, row_keyed)
                continue
            t = _ref(v[e])
            gt = _ref(gv[ge]) if ge is not None else None
            if mod._is_wrapper(t):
                if gt is not None and game._is_wrapper(gt):
                    self._pair(at, game._text(game._folded(gt)), row_keyed)
            elif mod._inline(t) and t not in self.seen:
                self.instance(t, gt, at, row_added, skip, row_keyed)
            elif gt is not None:
                self._pair(at, game._text(game._reference((), kind, gv[ge], 0)), row_keyed)

    def _pair_rows(self, mrows, grows, mkeys, m_len: int, g_len: int, fixed=frozenset()):
        """(mod element → game element, mod elements the game lacks, fields not to pair, whether by a key):
        by a key unique in both tables, made only of fields a mod does not change as values (id-like
        names, ``_Index``, ``fixed`` ones: GUIDs, strings, enum values), else by position when both are
        as long.  (Rows named by a plain value column would pair with the wrong game rows once a mod
        changed that column, say every _MaxHealth.)"""
        keys = tuple(sorted(mkeys)) if mkeys and all(_stable(k, fixed) for k in mkeys) else ()
        g_keyable = [r for r in grows if r is not None]
        if keys and not (all(all(k in r for k in keys) for r in g_keyable) and _unique(g_keyable, keys)):
            keys = ()
        if not keys:
            keys = _common_key(mrows, grows, fixed)
        if not keys:
            if m_len == g_len:
                return {e: e for e in range(m_len)}, set(), (), False
            return {}, set(), (), False
        where = {tuple(r[k] for k in keys): ge for ge, r in enumerate(grows) if r is not None}
        pairs, new_rows = {}, set()
        for me, r in enumerate(mrows):
            if r is None:
                continue
            ge = where.get(tuple(r[k] for k in keys))
            if ge is None:
                new_rows.add(me)
            else:
                pairs[me] = ge
        matched = set(pairs.values())
        last = max(matched, default=-1)
        for ge, r in enumerate(grows):
            if r is not None and ge not in matched:
                if self.other_version or ge > last:
                    self.game_only += 1
                else:
                    self.removed += 1
        skip: tuple = ()
        if "_Index" not in keys and _numbered(mrows) and _numbered(grows):
            skip = (("_Index", 0),)  # rows numbered by position: a row put in renumbers the rest
        return pairs, new_rows, skip, True


def _unique(rows, keys) -> bool:
    seen = set()
    for r in rows:
        value = tuple(r[k] for k in keys)
        if value in seen:
            return False
        seen.add(value)
    return True


def _stable(name: str, fixed=frozenset()) -> bool:
    """Whether rows may be paired by this field: an id-like name, ``_Index``, or one that cannot be
    edited (``fixed``: GUIDs, strings, enum values)."""
    return name in fixed or name.lower() == "_index" or KEY_HINT.search(name) is not None


def _common_key(mrows, grows, fixed=frozenset()) -> tuple[str, ...]:
    """A key of stable fields (``_stable``) unique in both tables (in choose_key's order), () when there
    is none."""
    a, b = [r for r in mrows if r is not None], [r for r in grows if r is not None]
    if not a or not b:
        return ()
    names = [k for k in a[0] if all(k in r for r in a) and all(k in r for r in b) and _stable(k, fixed)]
    return next((keys for keys in _candidates(names, fixed) if _unique(a, keys) and _unique(b, keys)), ())


def _numbered(rows) -> bool:
    """Every row with fields has ``_Index`` equal to its position."""
    have = [(e, r) for e, r in enumerate(rows) if r is not None]
    return bool(have) and all(r.get("_Index") == str(e) for e, r in have)
