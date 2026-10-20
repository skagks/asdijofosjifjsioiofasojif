"""MDF2 material files: read, patch in place, or rebuild from material blobs.

Layout from the RE-Engine-010-Templates "RE_Engine_MDF.bt" (alphaZomega and others),
as ported in MHWmodfixer's mdf2.py/mdf2_slice.py.  The layout version is NOT stored
in the file: it is the number in the file name (``.mdf2.45``); inside a pak, where
there is no name, it is found by trying versions until one parses sensibly.

In-place edits keep every byte that is not edited identical to the current game's
file: strings are replaced and every offset pointing past the edit is moved (as the
template's own Fix_Off does); strings shared by several fields are never edited in
place but copied to the end.  Besides texture paths (as in MHWmodfixer), shader
parameter values, render state and gpbf paths can be patched in place too, so a mod
matched 1:1 to one current file keeps all of its own tuning without a rebuild.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from .pak import murmur3_32

_VERSION_RE = re.compile(r"\.(\d+)$")


def version_from_name(name: str) -> int:
    m = _VERSION_RE.search(name)
    if not m:
        raise ValueError(f"파일 이름에 버전 번호가 없습니다: {name}")
    return int(m.group(1))


def _u32(buf, pos):
    return struct.unpack_from("<I", buf, pos)[0]


def _u64(buf, pos):
    return struct.unpack_from("<Q", buf, pos)[0]


def read_wstring(buf, pos: int) -> tuple[str, int]:
    """UTF-16 string ending in a 2-byte zero: (text, length in bytes with the zero)."""
    end, n = pos, len(buf)
    if pos < 0 or pos >= n:
        raise ValueError("string offset outside the file")
    while end + 1 < n and not (buf[end] == 0 and buf[end + 1] == 0):
        end += 2
    return bytes(buf[pos:end]).decode("utf-16-le"), end - pos + 2


@dataclass
class StringRef:
    field_pos: int  # where the 8-byte offset to the string is stored
    str_pos: int  # where the string itself is
    text: str


@dataclass
class Material:
    index: int
    hdr_pos: int
    name: StringRef
    mmtr: StringRef
    shading_pos: int
    alpha_pos: int
    props_offs_pos: int
    tex_hdr_offs: int
    prop_hdrs_offs: int
    gpbf_offs: int | None
    gpbf_split: tuple[int, int]
    tex_count: int
    prop_count: int
    textures: list[tuple[StringRef, StringRef]] = field(default_factory=list)  # (type, path)
    props: list[tuple[StringRef, int]] = field(default_factory=list)  # (name, header position)
    gpbf: list[StringRef] = field(default_factory=list)  # split[0] slot names, then split[1] paths

    @property
    def mmtr_path(self) -> str:
        return self.mmtr.text


class Mdf2File:
    def __init__(self, data: bytes, version: int):
        self.buf = bytearray(data)
        self.version = version
        self.is_v2 = 30 <= version < 100
        self.has_gpbf = 19 <= version < 100000
        self.materials: list[Material] = []
        self._offset_fields: list[int] = []
        self._parse()

    # ---- parsing ---------------------------------------------------------------
    def _off(self, pos: int) -> int:
        self._offset_fields.append(pos)
        return _u64(self.buf, pos)

    def _string(self, field_pos: int) -> StringRef:
        pos = self._off(field_pos)
        return StringRef(field_pos, pos, read_wstring(self.buf, pos)[0])

    def _parse(self):
        buf = self.buf
        if bytes(buf[:4]) != b"MDF\x00":
            raise ValueError("MDF2 파일이 아닙니다")
        count = struct.unpack_from("<H", buf, 6)[0]
        pos = 16
        for i in range(count):
            hdr = pos
            name = self._string(pos)
            pos += 8 + 4  # name offset, hash
            if self.version == 6:
                pos += 8
            pos += 4  # size of the float block
            prop_count, tex_count = struct.unpack_from("<II", buf, pos)
            pos += 8
            split = (0, 0)
            if self.has_gpbf:
                split = struct.unpack_from("<ii", buf, pos)
                pos += 8
            shading_pos = pos
            pos += 4
            if self.is_v2:
                pos += 4
            alpha_pos = pos
            pos += 4
            if self.is_v2:
                pos += 8
            prop_hdrs = self._off(pos)
            tex_hdrs = self._off(pos + 8)
            pos += 16
            gpbf_offs = None
            if self.has_gpbf:
                gpbf_offs = self._off(pos)
                pos += 8
            props_offs_pos = pos
            self._off(pos)
            pos += 8
            mmtr = self._string(pos)
            pos += 8
            if self.is_v2:
                self._off(pos)  # uknStructs
                pos += 8
            self.materials.append(Material(i, hdr, name, mmtr, shading_pos, alpha_pos, props_offs_pos, tex_hdrs,
                                           prop_hdrs, gpbf_offs, tuple(split), tex_count, prop_count))
        for mat in self.materials:
            p = mat.tex_hdr_offs
            for _ in range(mat.tex_count):
                kind = self._string(p)
                path = self._string(p + 16)  # after the type offset and its two hashes
                mat.textures.append((kind, path))
                p += 32 if self.version >= 13 else 24
            p = mat.prop_hdrs_offs
            for _ in range(mat.prop_count):
                mat.props.append((self._string(p), p))
                p += 24
            if self.has_gpbf and mat.gpbf_offs:
                p = mat.gpbf_offs
                for _ in range(max(0, sum(mat.gpbf_split))):
                    mat.gpbf.append(self._string(p))
                    p += 16

    # ---- reading values --------------------------------------------------------
    def _prop_header(self, hdr_pos: int) -> tuple[int, int, int]:
        """(offset in the material's float block, number of floats, flags)."""
        a, b = struct.unpack_from("<ii", self.buf, hdr_pos + 16)
        offset, raw = (a, b) if self.version >= 13 else (b, a)
        raw &= 0xFFFFFFFF
        return offset, raw & 0xFFFF, raw >> 16  # only the low 16 bits count floats

    def prop_values(self, mat: Material, i: int) -> list[float]:
        offset, n, _ = self._prop_header(mat.props[i][1])
        return list(struct.unpack_from(f"<{n}f", self.buf, _u64(self.buf, mat.props_offs_pos) + offset))

    # ---- in-place editing ------------------------------------------------------
    def _refs(self):
        for m in self.materials:
            yield m.name
            yield m.mmtr
            for kind, path in m.textures:
                yield kind
                yield path
            for name, _ in m.props:
                yield name
            yield from m.gpbf

    def set_string(self, ref: StringRef, text: str) -> bool:
        """Point one string field at ``text``. Returns False if it already was."""
        if ref.text == text:
            return False
        buf, new = self.buf, text.encode("utf-16-le") + b"\x00\x00"
        if sum(1 for r in self._refs() if r.str_pos == ref.str_pos) > 1:
            # shared with other fields (the exporter de-duplicates strings): add a copy
            ref.str_pos = len(buf)
            buf.extend(new)
            struct.pack_into("<Q", buf, ref.field_pos, ref.str_pos)
        else:
            old_pos = ref.str_pos
            old_len = read_wstring(buf, old_pos)[1]
            delta = len(new) - old_len
            buf[old_pos:old_pos + old_len] = new
            if delta:
                for pos in self._offset_fields:
                    value = _u64(buf, pos)
                    if value > old_pos:
                        struct.pack_into("<Q", buf, pos, value + delta)
                for r in self._refs():
                    if r.str_pos > old_pos:
                        r.str_pos += delta
        ref.text = text
        return True

    def set_prop_values(self, mat: Material, i: int, values: list[float]) -> bool:
        offset, n, _ = self._prop_header(mat.props[i][1])
        if len(values) != n:
            return False
        pos = _u64(self.buf, mat.props_offs_pos) + offset
        old = struct.unpack_from(f"<{n}f", self.buf, pos)
        new = struct.pack(f"<{n}f", *values)
        if struct.pack(f"<{n}f", *old) == new:
            return False
        self.buf[pos:pos + 4 * n] = new
        return True

    def render_state(self, mat: Material) -> tuple[int, bytes]:
        return _u32(self.buf, mat.shading_pos), bytes(self.buf[mat.alpha_pos:mat.alpha_pos + 4])

    def set_render_state(self, mat: Material, shading: int, alpha: bytes) -> bool:
        if self.render_state(mat) == (shading, alpha):
            return False
        struct.pack_into("<I", self.buf, mat.shading_pos, shading)
        self.buf[mat.alpha_pos:mat.alpha_pos + 4] = alpha
        return True

    def to_bytes(self) -> bytes:
        return bytes(self.buf)


def detect_version(data: bytes, prefer: int | None = None, candidates=range(1, 200)) -> int | None:
    """Layout version of a nameless .mdf2 (inside a pak). Several wrong low versions
    parse without error by skipping the real content - every material then has an
    empty shader path, which rejects them (MHWmodfixer found 842 such files)."""
    order = ([prefer] if prefer else []) + [v for v in candidates if v != prefer]
    for version in order:
        try:
            mdf = Mdf2File(data, version)
            if mdf.materials and all(m.mmtr_path for m in mdf.materials):
                for i in range(len(mdf.materials)):
                    extract_material(mdf, i)
                return version
        except Exception:
            continue
    return None


# ---- material blobs ---------------------------------------------------------------
def extract_material(mdf: Mdf2File, index: int) -> dict:
    """One material as plain data (to compare, or to build a new file from)."""
    buf, mat = mdf.buf, mdf.materials[index]
    pos = mat.shading_pos
    shading = _u32(buf, pos)
    unk_24 = _u32(buf, pos + 4) if mdf.is_v2 else None
    alpha = bytes(buf[mat.alpha_pos:mat.alpha_pos + 4])
    unk_2c = unk_30 = None
    if mdf.is_v2:
        unk_2c, unk_30 = struct.unpack_from("<ii", buf, mat.alpha_pos + 4)
    gpbf_entries = []
    if mdf.has_gpbf and mat.gpbf_offs:
        p = mat.gpbf_offs
        for ref in mat.gpbf:
            _off, h1, h2 = struct.unpack_from("<Qii", buf, p)
            gpbf_entries.append((ref.text, h1, h2))
            p += 16
    textures = []
    for kind, path in mat.textures:
        ukn = _u64(buf, path.field_pos + 8) if mdf.version >= 13 else None
        textures.append({"type": kind.text, "path": path.text, "ukn": ukn})
    props = []
    for i, (name, hdr) in enumerate(mat.props):
        _offset, _n, flags = mdf._prop_header(hdr)
        props.append({"name": name.text, "values": mdf.prop_values(mat, i), "count_flags": flags})
    return {
        "name": mat.name.text,
        "mmtr_path": mat.mmtr_path,
        "shading_type": shading,
        "alpha_flags_raw": alpha,
        "isV2_unk_0x24": unk_24,
        "isV2_unk_0x2C": unk_2c,
        "isV2_unk_0x30": unk_30,
        "gpbf_split": tuple(mat.gpbf_split),
        "gpbf_entries": gpbf_entries,
        "textures": textures,
        "props": props,
        "header_version": struct.unpack_from("<H", buf, 4)[0],
    }


def material_to_json(blob: dict) -> dict:
    out = dict(blob)
    out["alpha_flags_raw"] = blob["alpha_flags_raw"].hex()
    out["gpbf_split"] = list(blob["gpbf_split"])
    out["gpbf_entries"] = [list(e) for e in blob["gpbf_entries"]]
    return out


def material_from_json(data: dict) -> dict:
    out = dict(data)
    out["alpha_flags_raw"] = bytes.fromhex(data["alpha_flags_raw"])
    out["gpbf_split"] = tuple(data["gpbf_split"])
    out["gpbf_entries"] = [tuple(e) for e in data["gpbf_entries"]]
    return out


def _pad16(n: int) -> int:
    # the float block is stored padded to 16 bytes; a smaller size made the engine
    # read past it (MHWmodfixer: renderer hang)
    return (n + 15) // 16 * 16


def assemble_mdf2(materials: list[dict], version: int) -> bytes:
    """A new MDF2 file from material blobs, which may come from different files.
    The optional per-material "uknStructs" block is written empty, as the template allows."""
    is_v2 = 30 <= version < 100
    has_gpbf = 19 <= version < 100000
    header_version = materials[0]["header_version"] if materials else 1
    entry = 8 + 4 + (8 if version == 6 else 0) + 12 + (8 if has_gpbf else 0) + 4 + (4 if is_v2 else 0) + 4 \
        + (8 if is_v2 else 0) + 16 + (8 if has_gpbf else 0) + 16 + (8 if is_v2 else 0)
    tex_entry = 24 + (8 if version >= 13 else 0)
    off_tex = 16 + len(materials) * entry
    off_props = off_tex + sum(len(m["textures"]) for m in materials) * tex_entry
    off_gpbf = off_props + sum(len(m["props"]) for m in materials) * 24
    off_values = off_gpbf + (sum(sum(m["gpbf_split"]) for m in materials) * 16 if has_gpbf else 0)
    off_strings = off_values + sum(_pad16(sum(len(p["values"]) for p in m["props"]) * 4) for m in materials)
    buf = bytearray(off_strings)
    pool: dict[str, int] = {}

    def intern(text: str) -> int:
        if text not in pool:
            pool[text] = len(buf)
            buf.extend(text.encode("utf-16-le") + b"\x00\x00")
        return pool[text]

    # the last header field is 1 in every real file; 0 made the game ignore the file
    struct.pack_into("<4sHHQ", buf, 0, b"MDF\x00", header_version, len(materials), 1)
    tex_cur, prop_cur, gpbf_cur, val_cur = off_tex, off_props, off_gpbf, off_values
    for i, m in enumerate(materials):
        pos = 16 + i * entry
        block_size = _pad16(sum(len(p["values"]) for p in m["props"]) * 4)
        this_tex, this_props, this_gpbf, this_values = tex_cur, prop_cur, gpbf_cur if has_gpbf else 0, val_cur
        struct.pack_into("<QI", buf, pos, intern(m["name"]), murmur3_32(m["name"].encode("utf-16-le")))
        pos += 12
        if version == 6:
            pos += 8
        struct.pack_into("<III", buf, pos, block_size, len(m["props"]), len(m["textures"]))
        pos += 12
        if has_gpbf:
            struct.pack_into("<ii", buf, pos, *m["gpbf_split"])
            pos += 8
        struct.pack_into("<I", buf, pos, m["shading_type"])
        pos += 4
        if is_v2:
            struct.pack_into("<I", buf, pos, m["isV2_unk_0x24"] or 0)
            pos += 4
        buf[pos:pos + 4] = m["alpha_flags_raw"]
        pos += 4
        if is_v2:
            struct.pack_into("<ii", buf, pos, m["isV2_unk_0x2C"] or 0, m["isV2_unk_0x30"] or 0)
            pos += 8
        struct.pack_into("<QQ", buf, pos, this_props, this_tex)
        pos += 16
        if has_gpbf:
            struct.pack_into("<Q", buf, pos, this_gpbf)
            pos += 8
        struct.pack_into("<QQ", buf, pos, this_values, intern(m["mmtr_path"]))
        pos += 16
        if is_v2:
            struct.pack_into("<Q", buf, pos, 0)
        for t in m["textures"]:
            struct.pack_into("<QIIQ", buf, tex_cur, intern(t["type"]), murmur3_32(t["type"].encode("utf-16-le")),
                             murmur3_32(t["type"].encode("ascii", "ignore")), intern(t["path"]))
            if version >= 13:
                struct.pack_into("<Q", buf, tex_cur + 24, t["ukn"] or 0)
            tex_cur += tex_entry
        for p in m["props"]:
            n = len(p["values"])
            count = (n & 0xFFFF) | ((p.get("count_flags", 0) & 0xFFFF) << 16)
            struct.pack_into("<QII", buf, prop_cur, intern(p["name"]), murmur3_32(p["name"].encode("utf-16-le")),
                             murmur3_32(p["name"].encode("ascii", "ignore")))
            pair = (val_cur - this_values, count) if version >= 13 else (count, val_cur - this_values)
            struct.pack_into("<ii", buf, prop_cur + 16, *pair)
            struct.pack_into(f"<{n}f", buf, val_cur, *p["values"])
            prop_cur += 24
            val_cur += 4 * n
        val_cur = this_values + block_size
        if has_gpbf:
            for name, h1, h2 in m["gpbf_entries"]:
                struct.pack_into("<Qii", buf, gpbf_cur, intern(name), h1, h2)
                gpbf_cur += 16
    return bytes(buf)
