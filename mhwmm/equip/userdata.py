"""RSZ .user files (the game's data tables) as plain Python values.

``read_user`` returns the root object of a file as a dict: field name -> value, with "$type"
the instance's type name.  Values by registry kind: integers (S8..U64, enums), bools, floats,
str (String / Resource, without a resource's leading "@"), bytes (Guid: the 16 raw bytes, the
same bytes a .msg entry stores; any other kind: its raw bytes), a dict for an Object reference
(the same dict wherever an instance is referenced again), {"$type": type, "$user": path} for a
reference to another .user file (the path as the file names it: without natives/STM/ and the
version), and lists for arrays.  An enum wrapper instance (``*_Serializable`` with a
single ``_Value``) collapses to that value: the enum's Fixed number.

Nothing is guessed: a file is read only when every instance reads exactly with the registry's
current layout (rsz.choose_schema), otherwise the result is None.
"""

from __future__ import annotations

import logging
import struct

from ..matfix import rsz

log = logging.getLogger(__name__)

TYPE = "$type"
USER = "$user"
ROW_FIELDS = ("_Values", "_DataList", "_Entries")  # where the game's tables keep their rows

_INTS = {kind: (fmt, struct.calcsize(fmt)) for kind, fmt in (
    ("S8", "<b"), ("U8", "<B"), ("C8", "<B"), ("S16", "<h"), ("U16", "<H"), ("C16", "<H"),
    ("S32", "<i"), ("U32", "<I"), ("S64", "<q"), ("U64", "<Q"))}
_FLOATS = {("F32", 4): "<f", ("F64", 8): "<d", ("F16", 2): "<e"}


def read_user(data: bytes, registry: rsz.TypeRegistry | None = None) -> dict | None:
    """The root object of an RSZ file (normally a .user file); None when it is not one or does
    not read exactly with the registry."""
    try:
        info = rsz.parse(data) if data else None
        if info is None or not info.objects:
            return None
        registry = registry or rsz.TypeRegistry.load()
        schema, instances = rsz.choose_schema(None, info, registry)
        if schema is None:
            return None
        root = _Reader(info, instances, registry).object(info.objects[0])
        return root if isinstance(root, dict) else None
    except (struct.error, ValueError, IndexError, UnicodeDecodeError, RecursionError) as exc:
        log.debug("user 파일을 읽지 못했습니다: %s", exc)
        return None


def rows(root, field: str | None = None) -> list[dict]:
    """The rows of a table: ``root[field]``, or the first of _Values / _DataList / _Entries that
    is a list; only the dict elements (null references are left out)."""
    if not isinstance(root, dict):
        return []
    for name in (field,) if field else ROW_FIELDS:
        value = root.get(name)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    return []


def read_rows(data: bytes, registry: rsz.TypeRegistry | None = None, field: str | None = None) -> list[dict] | None:
    """``rows(read_user(data))``; None when the file does not read."""
    root = read_user(data, registry)
    return None if root is None else rows(root, field)


def external_paths(info: rsz.RszInfo) -> dict[int, str]:
    """Instance index -> path of the .user file it stands for (the RSZ block's userdata table)."""
    out: dict[int, str] = {}
    data, base = info.data, info.rsz_off
    _version, _objects, _count, userdata, _reserved = struct.unpack_from("<IIIII", data, base + 4)
    table = struct.unpack_from("<Q", data, base + 40)[0]
    for i in range(min(userdata, rsz.MAX_COUNT)):
        index, _type_id, offset = struct.unpack_from("<iIQ", data, base + table + 16 * i)
        out[index] = _wide(data, base + offset)
    return out


def _wide(data: bytes, start: int) -> str:
    """A NUL-terminated UTF-16LE string."""
    end = start
    while True:  # the first 00 00 at an even distance from the start
        end = data.find(b"\x00\x00", end)
        if end < 0:
            end = len(data)
            break
        if (end - start) % 2 == 0:
            break
        end += 1
    return data[start:end].decode("utf-16-le", "replace")


def _text(raw: bytes) -> str:
    text = raw[4:].decode("utf-16-le", "replace")
    nul = text.find("\x00")
    text = text if nul < 0 else text[:nul]
    return text[1:] if text.startswith("@") else text


def scalar(kind: str, size: int, raw: bytes):
    """One field value that is not a reference."""
    found = _INTS.get(kind)
    if found is not None and len(raw) == found[1]:
        return struct.unpack(found[0], raw)[0]
    if kind == "Bool" and raw:
        return raw[0] != 0
    fmt = _FLOATS.get((kind, len(raw)))
    if fmt is not None:
        return struct.unpack(fmt, raw)[0]
    if kind in ("String", "Resource"):
        return _text(raw)
    return bytes(raw)


class _Reader:
    def __init__(self, info: rsz.RszInfo, instances: list, registry: rsz.TypeRegistry):
        self.instances = instances
        self.registry = registry
        self.external = external_paths(info) if info.external else {}
        self.memo: dict[int, object] = {}

    def object(self, index: int):
        if index in self.memo:
            return self.memo[index]
        if not 0 < index < len(self.instances):
            return None
        inst = self.instances[index]
        if index in self.external:
            out = self.memo[index] = {TYPE: self.registry.name(inst.type_id), USER: self.external[index]}
            return out
        if inst.fields is None:
            return None
        name = self.registry.name(inst.type_id)
        fields = inst.fields
        if name.endswith("_Serializable") and len(fields) == 1 and fields[0][0] == "_Value" and not fields[0][4]:
            value = self.memo[index] = self.value(fields[0], inst.values[0])
            return value
        out = self.memo[index] = {TYPE: name}  # before the fields: a cycle comes back to this dict
        self.fill(out, fields, inst.values)
        return out

    def fill(self, out: dict, fields, values) -> None:
        for field, value in zip(fields, values):
            name, kind, _size, _align, is_array = field
            if is_array:
                if kind.startswith("Struct:"):
                    sub = self.registry.struct_fields(kind[7:]) or ()
                    items = []
                    for elem in value:
                        item = {TYPE: kind[7:]}
                        self.fill(item, sub, elem)
                        items.append(item)
                    out[name] = items
                else:
                    out[name] = [self.value(field, v) for v in value]
            else:
                out[name] = self.value(field, value)

    def value(self, field, raw):
        _name, kind, size, _align, _is_array = field
        if kind in rsz.REFERENCE_KINDS:
            return self.object(struct.unpack("<i", raw)[0])
        return scalar(kind, size, raw)
