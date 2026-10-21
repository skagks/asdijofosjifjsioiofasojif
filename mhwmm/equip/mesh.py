"""MH Wilds models (``*.mesh.241111606``, internal version 240704828), read only, for the 3D preview.

Reads only what a preview needs: the LOD / group / submesh tables, material names, bounds and, for one
chosen LOD, float32 positions, half-float UV0 (optional), snorm8 normals (optional) and the triangle list.
:func:`pieces` then turns that into clean per-material pieces for drawing.

Pure Python (struct + array), no numpy.  Layout facts follow REasy (seifhassine/REasy, MIT licence,
Copyright (c) 2025 Battlezone), file_handlers/mesh/mesh_file.py, mhwilds_contract.py and
file_handlers/mesh/profiles/*.py at commit b26b84021f823056f160d521bbbabd03c4fbe975 (the MH Wilds profile
inherits RE8 -> RE_RT -> RE4 -> SF6 -> DD2 -> Onimusha2 -> MHWilds; the effective field list is written
out below).  Cross-checked against kagenocookie/RE-Engine-Lib (MIT) REE-Lib/OtherFiles/MeshFile.cs
@7d5af062, eigeen/ree-pak-gui (MIT per src-tauri/Cargo.toml) model-insight-wasm/src/formats/mesh.rs
@00917abf and the header offset table in Dimcirui/REE-JCNS-Editor tools/mesh_joints.py (facts only).

Only ``MESH`` magic files are handled.  ``MPLY`` (meshlet-compressed, quantized positions; used by
MH Wilds props and stage models) raises ``Unsupported``.

Streaming: a vanilla mesh lists "streaming buffers" (StreamingInfo).  Their bytes are not in the .mesh;
they are a headerless blob in the companion file at the same path with ``streaming/`` inserted after
``natives/STM/``.  Pass that blob as ``streaming=``.  Without it only LODs that use the resident buffer
(buffer 0) can be decoded.  Mod meshes written by RE-Mesh-Editor have no streaming entries; some mods
ship a full copy of the .mesh under streaming/ instead, which is ignored when passed as the blob.

Space: Y up, metres; weapons run along +Z.  UV v = 0 is the top row of the texture (no flip).
"""

from __future__ import annotations

import math
import re
import struct
import sys
from array import array
from dataclasses import dataclass, field
from functools import cached_property
from itertools import compress
from operator import itemgetter

from . import EquipError

MESH_MAGIC = 0x4853454D  # b"MESH"
MPLY_MAGIC = 0x594C504D  # b"MPLY"
WILDS_INTERNAL_VERSION = 240704828
FILE_VERSION = 241111606  # the ".mesh.<n>" suffix
HEADER_SIZE = 0xB0
BUFFER_LAYOUT_SIZE = 64  # resident layout and each streaming layout
MAX_ELEMENTS = 64  # vertex attributes per buffer (real files have about 5)
MAX_SUBMESHES = 0xFFFF  # submesh records read in all LODs together (the header's own count is a u16)

# Vertex element semantics (REasy VertexBufferType)
POSITION, NORMAL_TANGENT, UV0, UV1, BONE_WEIGHTS, COLOR, UV2, EXTRA_WEIGHTS = range(8)

COLLAPSE_EPS = 1e-3  # a group whose vertices all lie within 1 mm is hidden (how mods hide a part)
AREA_EPS = 1e-24  # |cross product|^2 at or below this (area < 5e-13 m^2) is a degenerate triangle

_LITTLE = sys.byteorder == "little"
_INF = float("inf")
_STREAMING_RE = re.compile(r"(natives[/\\][^/\\]+[/\\])(?!streaming[/\\])", re.IGNORECASE)


class MeshError(EquipError, ValueError):
    """The file is not a mesh this reader understands, or it is damaged."""


class Unsupported(MeshError):
    """A valid mesh variant this reader does not decode (MPLY, other game versions)."""


@dataclass
class Element:
    semantic: int
    stride: int
    offset: int  # byte offset of this attribute's array inside the buffer's vertex block


@dataclass
class Buffer:
    index: int  # 0 = resident (inside the .mesh), 1.. = streaming companion
    elements: list
    vertex_start: int  # absolute offset (resident: in the .mesh; streaming: in the streaming file)
    vertex_size: int
    index_start: int
    index_size: int
    vertex_count: int = 0


@dataclass
class Submesh:
    material: int
    buffer: int
    index_count: int
    index_start: int  # in indices, relative to the buffer's index block
    base_vertex: int
    group: int = 0  # mesh group ("part") id of the owning group


@dataclass
class Lod:
    level: int
    factor: float
    groups: list  # (group_id, vertex_count, index_count, [Submesh]); not changed once read

    # worked out once: several LOD slots may share one record, and every slot is asked
    @cached_property
    def submeshes(self):
        return [s for g in self.groups for s in g[3]]

    @cached_property
    def buffers(self):
        return sorted({s.buffer for s in self.submeshes})

    @cached_property
    def triangle_count(self) -> int:
        return sum(s.index_count for s in self.submeshes) // 3


@dataclass
class MeshInfo:
    size: int
    content_flags: int
    names: list
    material_names: list
    lods: list
    bbox_min: tuple
    bbox_max: tuple
    sphere: tuple
    uv_count: int
    index32: bool
    buffers: dict  # buffer index -> Buffer (streaming ones only when their layout exists)
    streaming_entries: list  # [(start, size)] in the streaming companion file

    @property
    def needs_streaming(self) -> bool:
        return any(size for _, size in self.streaming_entries)


@dataclass
class Geometry:
    lod: int
    positions: array  # float32 xyz, flat
    uvs: array | None  # float u, v flat (or None); DirectX convention: v = 0 is the top row of the texture
    normals: array | None  # float xyz flat (or None)
    triangles: list = field(default_factory=list)  # [(material_index, array of vertex indices, flat)]
    parts: list = field(default_factory=list)  # mesh group id of each entry in ``triangles``


@dataclass
class Piece:
    """One material of one mesh group, cleaned for drawing: finite positions and UVs, no degenerate
    triangles and only the vertices its triangles use (indices count from this piece's first vertex)."""
    material_name: str
    group: int
    positions: array  # float32 xyz, flat
    indices: array  # "I", three per triangle
    uvs: array | None  # float32 u, v flat
    normals: array | None  # float32 xyz flat

    @property
    def vertex_count(self) -> int:
        return len(self.positions) // 3

    @property
    def triangle_count(self) -> int:
        return len(self.indices) // 3


def streaming_path(path: str) -> str | None:
    """``natives/STM/x.mesh.N`` -> ``natives/STM/streaming/x.mesh.N`` (None if not below natives/<platform>)."""
    m = _STREAMING_RE.search(path)
    if not m:
        return None
    sep = "\\" if "\\" in m.group(1) else "/"
    return path[:m.end(1)] + "streaming" + sep + path[m.end(1):]


def is_mesh(data: bytes) -> bool:
    """Does the data start like a mesh file (MESH or MPLY)?  A streaming/ copy that does is a full copy
    of the .mesh, not the headerless streaming blob."""
    return len(data) >= 4 and struct.unpack_from("<I", data, 0)[0] in (MESH_MAGIC, MPLY_MAGIC)


def _u16(d, o):
    return struct.unpack_from("<H", d, o)[0]


def _i16(d, o):
    return struct.unpack_from("<h", d, o)[0]


def _i32(d, o):
    return struct.unpack_from("<i", d, o)[0]


def _i64(d, o):
    return struct.unpack_from("<q", d, o)[0]


def _check(cond, msg):
    if not cond:
        raise MeshError(msg)


def _table(d, off, what):
    """A table offset read from the file: after the header and inside the file."""
    _check(HEADER_SIZE <= off < len(d), f"{what} 위치가 파일 밖입니다 ({off})")
    return off


def _cstr(d, o):
    _check(HEADER_SIZE <= o < len(d), f"이름 위치가 파일 밖입니다 ({o})")
    end = d.find(b"\x00", o)
    _check(end >= 0, "이름이 끝나지 않습니다")
    return d[o:end].decode("utf-8", "replace")


def read_info(data: bytes) -> MeshInfo:
    """Parse the tables of a MH Wilds .mesh (no vertex data is decoded)."""
    try:
        return _read_info(data)
    except MeshError:
        raise
    except (struct.error, IndexError, ValueError, OverflowError) as e:
        raise MeshError(f"메시 파일이 손상되었습니다: {e}") from None


def _read_info(data: bytes) -> MeshInfo:
    d = data if isinstance(data, (bytes, bytearray)) else bytes(data)
    n = len(d)
    _check(n >= HEADER_SIZE, "메시 파일이 너무 작습니다")
    magic, version, file_size = struct.unpack_from("<III", d, 0)
    if magic == MPLY_MAGIC:
        raise Unsupported("MPLY(메시렛) 모델은 지원하지 않습니다")
    _check(magic == MESH_MAGIC, "메시 파일이 아닙니다")
    if version != WILDS_INTERNAL_VERSION:
        raise Unsupported(f"MH Wilds 메시가 아닙니다 (버전 {version})")
    _check(file_size == n, f"헤더의 파일 크기({file_size})가 실제 크기({n})와 다릅니다")

    # --- MeshHeader (Wilds layout) ---
    # 0x0C u32 LOD hash, 0x10 u32 light-map bits, 0x14 u16 name count, 0x16 u32 content flags (low 24 bits),
    # 0x1A u16 buffer-count storage, 0x1C 3 x i32 reserved, 0x28 i64 GPU buffer base, then 16 x i64 offsets:
    name_count = _u16(d, 0x14)
    content_flags = struct.unpack_from("<I", d, 0x16)[0] & 0xFFFFFF
    (lods_off, _shadow_off, _occluder_off, _nrecalc_off, _blend_off, layout_off, _bsbm_off,
     _floats_off, _bounds_off, _bones_off, matidx_off, _boneidx_off, _bsidx_off,
     names_off, stream_off, _sdf_off) = struct.unpack_from("<16q", d, 0x30)
    _check(lods_off > 0 and layout_off > 0, "LOD 표나 버퍼 정보가 없습니다")
    _table(d, lods_off, "LOD 표")
    _table(d, layout_off, "버퍼 정보")

    # --- StreamingInfo: i32 count, i32 pad, i64 entries -> count x (u32 start, u32 size) ---
    entries = []
    if stream_off:
        count, _pad, ent_off = struct.unpack_from("<iiq", d, _table(d, stream_off, "스트리밍 정보"))
        _check(0 <= count <= 64, f"스트리밍 항목 수가 이상합니다 ({count})")
        if count:
            _table(d, ent_off, "스트리밍 항목")
        entries = [struct.unpack_from("<II", d, ent_off + 8 * i) for i in range(count)]

    # --- MeshData (LOD layout header) ---
    # u8 LOD count, u8 material count, u8 UV count, u8 skin weight count, u16 total meshes, u8 32-bit indices,
    # u8 reserve; vec4 sphere; vec3 min + u32; vec3 max + u32; i64 LOD offset table
    lod_count, material_count, uv_count, _skin, _total_meshes, index32, _r = struct.unpack_from(
        "<BBBBHBB", d, lods_off)
    sphere = struct.unpack_from("<4f", d, lods_off + 8)
    bmin = struct.unpack_from("<3f", d, lods_off + 24)
    bmax = struct.unpack_from("<3f", d, lods_off + 40)
    lod_offsets = struct.unpack_from(f"<{lod_count}q", d, _table(d, _i64(d, lods_off + 56), "LOD 목록"))

    lods = []
    vertex_end = {}  # buffer -> running vertex count (groups are packed back to back per buffer)
    seen = {}
    parsed = 0  # submesh records read so far
    for off in lod_offsets:
        if off <= 0:
            continue
        if off in seen:  # several LOD slots may point at the same LOD record
            lods.append(seen[off])
            continue
        # MeshLOD: u8 group count, u8 vertex format, u8 level, u8 reserve, f32 factor, i64 group table
        group_count, _vfmt, level, _res, factor, group_table = struct.unpack_from("<BBBBfq", d, _table(d, off, "LOD"))
        groups = []
        for goff in struct.unpack_from(f"<{group_count}q", d, _table(d, group_table, "그룹 목록")):
            if goff <= 0:
                continue
            # MeshGroup: u8 id, u8 submesh count, 6 reserved, u32 vertex count, u32 index count, submeshes
            gid, sub_count = d[_table(d, goff, "그룹")], d[goff + 1]
            gverts, gindices = struct.unpack_from("<II", d, goff + 8)
            parsed += sub_count  # LODs may share group tables and records: a crafted file could make millions
            _check(parsed <= MAX_SUBMESHES, "서브메시가 너무 많습니다")
            subs = []
            p = goff + 16
            for _ in range(sub_count):
                # Submesh (32 bytes): u8 material, u8 isQuad, u8 buffer, u8 reserve, i32 reserved,
                # u32 index count, u32 start index, i32 base vertex, i32 x2 streaming offsets, u32 tail
                mat, is_quad, buf, _r2, _r3, icount, istart, base = struct.unpack_from("<BBBBiIIi", d, p)
                _check(not is_quad, "사각형 서브메시는 지원하지 않습니다")
                _check(base >= 0, "기준 정점이 음수입니다")
                subs.append(Submesh(mat, buf, icount, istart, base, gid))
                p += 32
            if subs:
                _check(len({s.buffer for s in subs}) == 1, "한 그룹이 여러 버퍼에 걸쳐 있습니다")
                b = subs[0].buffer
                vertex_end[b] = vertex_end.get(b, 0) + gverts
            groups.append((gid, gverts, gindices, subs))
        lod = Lod(level, factor, groups)
        seen[off] = lod
        lods.append(lod)

    # --- Buffer layouts: resident at layout_off, streaming ones follow (64 bytes each) ---
    def elements(off, count):
        _check(count <= MAX_ELEMENTS, f"정점 속성 수가 이상합니다 ({count})")
        if count:
            _table(d, off, "정점 속성 표")
        out = []
        for i in range(count):
            sem, stride, eoff = struct.unpack_from("<HHI", d, off + 8 * i)
            out.append(Element(sem, stride, eoff))
        return out

    # i64 element table, i64 vertex block, i64 shape-key pointer (0), i32 total size, i32 vertex size,
    # u16 element count, u16 total element count, u32 shadow / occluder index offsets, 5 x i32 tail
    (el_off, vb_off, _skw, total_size, vb_size, main_count, _all_count) = struct.unpack_from(
        "<qqqiiHH", d, layout_off)
    _check(HEADER_SIZE <= vb_off and 0 <= vb_size <= total_size and vb_off + total_size <= n,
           "상주 버퍼가 파일 밖입니다")
    _check(_i64(d, 0x28) == vb_off, "헤더의 GPU 버퍼 위치가 상주 버퍼와 다릅니다")
    buffers = {0: Buffer(0, elements(el_off, main_count), vb_off, vb_size, vb_off + vb_size,
                         total_size - vb_size, vertex_end.get(0, 0))}
    hp = layout_off + BUFFER_LAYOUT_SIZE
    for k, (start, size) in enumerate(entries):
        if size == 0:  # empty entries have no layout record
            continue
        # same record; buffer index (one-based StreamingInfo slot) at +60
        (el_off, s_start, _skw, s_size, s_vlen, main_count, _all) = struct.unpack_from("<qqqIIHH", d, hp)
        buf_index = _i32(d, hp + 60)
        _check(s_start == start and s_size == size, f"스트리밍 버퍼 {k}의 정보가 항목과 다릅니다")
        _check(s_vlen <= s_size, f"스트리밍 버퍼 {k}의 정점 영역이 데이터보다 큽니다")
        _check(1 <= buf_index <= len(entries) and buf_index not in buffers,
               f"스트리밍 버퍼 번호가 이상합니다 ({buf_index})")
        buffers[buf_index] = Buffer(buf_index, elements(el_off, main_count), start, s_vlen,
                                    start + s_vlen, size - s_vlen, vertex_end.get(buf_index, 0))
        hp += BUFFER_LAYOUT_SIZE
    for b in buffers.values():  # attribute arrays are stored one after another (structure of arrays)
        if not b.vertex_count:  # e.g. an empty resident buffer when every LOD streams
            continue
        ends = sorted({e.offset for e in b.elements} | {b.vertex_size})
        for e in b.elements:
            nxt = next((o for o in ends if o > e.offset), -1)
            _check(e.offset + e.stride * b.vertex_count <= nxt,
                   f"버퍼 {b.index}의 정점 속성 {e.semantic}이 영역을 넘습니다")

    # --- Names and material remap ---
    names = []
    if names_off and name_count:
        for o in struct.unpack_from(f"<{name_count}q", d, _table(d, names_off, "이름 표")):
            names.append(_cstr(d, o))
    material_names = []
    if matidx_off and material_count:
        _table(d, matidx_off, "재질 표")
        for i in range(material_count):
            ni = _i16(d, matidx_off + 2 * i)
            material_names.append(names[ni] if 0 <= ni < len(names) else f"material{i}")

    return MeshInfo(n, content_flags, names, material_names, lods, bmin, bmax, sphere, uv_count,
                    bool(index32), buffers, list(entries))


def _half_pairs(raw: bytes) -> array:
    return array("f", struct.unpack(f"<{len(raw) // 2}e", raw))


def _snorm8_xyz(raw: bytes) -> array:
    """8-byte records: normal x y z w, tangent x y z w (signed bytes / 127, clamped to -1)."""
    b = array("b", raw)
    k = 1.0 / 127.0
    out = array("f", bytes(12 * (len(b) // 8)))
    for c in range(3):
        out[c::3] = array("f", [v * k if v != -128 else -1.0 for v in b[c::8]])
    return out


def _blob(streaming):
    """The streaming blob, or None when there is none (or it is a full .mesh copy instead)."""
    return None if streaming is None or is_mesh(streaming) else streaming


def available_lods(info: MeshInfo, streaming: bytes | None):
    """Indices of the LODs whose vertex/index data is present (most detailed first)."""
    streaming = _blob(streaming)

    def ok(b):
        if b == 0:
            return True
        buf = info.buffers.get(b)
        return streaming is not None and buf is not None and buf.index_start + buf.index_size <= len(streaming)

    return [i for i, lod in enumerate(info.lods) if lod.buffers and all(ok(b) for b in lod.buffers)]


def pick_lod(info: MeshInfo, streaming: bytes | None, max_triangles: int | None = None) -> int:
    """The most detailed available LOD; with ``max_triangles``, the most detailed one within that budget
    (or the smallest available one when none fits).  A LOD without triangles only when no other is
    available."""
    avail = available_lods(info, streaming)
    if not avail:
        if not any(lod.buffers for lod in info.lods):
            raise MeshError("그릴 수 있는 LOD가 없습니다")  # no LOD records, or none with a submesh
        if _blob(streaming) is None:
            raise MeshError("스트리밍 파일 없이 읽을 수 있는 LOD가 없습니다")
        raise MeshError("스트리밍 파일이 메시와 맞지 않습니다")  # too short for the buffers it should hold
    avail = [i for i in avail if info.lods[i].triangle_count] or avail
    if max_triangles is None:
        return avail[0]
    for i in avail:
        if info.lods[i].triangle_count <= max_triangles:
            return i
    return min(avail, key=lambda i: info.lods[i].triangle_count)


def read_geometry(data: bytes, info: MeshInfo | None = None, streaming: bytes | None = None,
                  lod: int | None = None, uv: bool = True, normals: bool = False,
                  max_triangles: int | None = None) -> Geometry:
    """Decode one LOD (default: the best one available, see :func:`pick_lod`) into flat arrays."""
    try:
        return _read_geometry(data, info, streaming, lod, uv, normals, max_triangles)
    except MeshError:
        raise
    except (struct.error, IndexError, ValueError, OverflowError) as e:
        raise MeshError(f"메시 파일이 손상되었습니다: {e}") from None


def _attribute(src, b: Buffer, e: Element) -> bytes:
    """The bytes of one vertex attribute array of buffer ``b`` (``src``: the .mesh or the blob)."""
    start = b.vertex_start + e.offset
    end = start + e.stride * b.vertex_count
    _check(end <= b.vertex_start + b.vertex_size and end <= len(src), "정점 데이터가 영역을 넘습니다")
    return bytes(src[start:end])


def _read_geometry(data, info, streaming, lod, uv, normals, max_triangles) -> Geometry:
    info = info or read_info(data)
    streaming = _blob(streaming)
    if lod is None:
        lod = pick_lod(info, streaming, max_triangles)
    _check(0 <= lod < len(info.lods), f"LOD {lod}이 없습니다")
    L = info.lods[lod]
    geo = Geometry(lod, array("f"), array("f") if uv else None, array("f") if normals else None)
    isize = 4 if info.index32 else 2
    for buf_index in L.buffers:  # normally one buffer per LOD; several are concatenated
        b = info.buffers.get(buf_index)
        _check(b is not None, f"버퍼 {buf_index}의 정보가 없습니다")
        src = data if b.index == 0 else streaming
        _check(src is not None, "스트리밍 파일이 필요합니다")
        nverts = b.vertex_count
        first = len(geo.positions) // 3
        by_sem = {e.semantic: e for e in b.elements}
        pe = by_sem.get(POSITION)
        _check(pe is not None and pe.stride == 12, "float3 위치 정보가 없습니다")
        pos = array("f")
        pos.frombytes(_attribute(src, b, pe))
        if not _LITTLE:
            pos.byteswap()
        geo.positions.extend(pos)
        if uv:
            e = by_sem.get(UV0)
            geo.uvs.extend(_half_pairs(_attribute(src, b, e)) if e and e.stride == 4
                           else array("f", bytes(8 * nverts)))
        if normals:
            e = by_sem.get(NORMAL_TANGENT)
            geo.normals.extend(_snorm8_xyz(_attribute(src, b, e)) if e and e.stride == 8
                               else array("f", bytes(12 * nverts)))
        for s in L.submeshes:
            if s.buffer != buf_index:
                continue
            start = b.index_start + s.index_start * isize
            end = start + s.index_count * isize
            _check(end <= b.index_start + b.index_size and end <= len(src), "인덱스가 버퍼를 넘습니다")
            idx = array("I" if info.index32 else "H")
            idx.frombytes(bytes(src[start:end]))
            if not _LITTLE:
                idx.byteswap()
            shift = s.base_vertex + first
            if shift:
                idx = array("I", [i + shift for i in idx])
            _check(not idx or max(idx) < first + nverts, "인덱스가 정점 수를 넘습니다")
            geo.triangles.append((s.material, idx))
            geo.parts.append(s.group)
    return geo


def material_name(info: MeshInfo, material: int) -> str:
    return info.material_names[material] if 0 <= material < len(info.material_names) else f"material{material}"


def _extent(xs, ys, zs, used):
    """(min xyz, max xyz) of the vertices ``used`` whose coordinates are all finite (None if none)."""
    sel = list(used)
    X, Y, Z = list(map(xs.__getitem__, sel)), list(map(ys.__getitem__, sel)), list(map(zs.__getitem__, sel))
    if not math.isfinite(sum(X) + sum(Y) + sum(Z)):  # float32 values cannot overflow a double sum
        fin = math.isfinite
        keep = [fin(x) and fin(y) and fin(z) for x, y, z in zip(X, Y, Z)]
        X, Y, Z = ([v for v, k in zip(C, keep) if k] for C in (X, Y, Z))
    if not X:
        return None
    return (min(X), min(Y), min(Z)), (max(X), max(Y), max(Z))


def bounds(geo: Geometry):
    """(min xyz, max xyz) of the finite vertices the triangles actually use (header bounds can be wrong)."""
    p = geo.positions
    nv = len(p) // 3
    used = set()
    for _, idx in geo.triangles:
        used.update(idx)
    if used and max(used) >= nv:  # only hand-made geometry has indices past the vertices
        used = {i for i in used if i < nv}
    if not used:
        return None
    return _extent(p[0::3].tolist(), p[1::3].tolist(), p[2::3].tolist(), used)


def is_collapsed(geo: Geometry, eps: float = COLLAPSE_EPS) -> bool:
    """True when all used vertices sit at (almost) one point: the usual way mods hide a model."""
    b = bounds(geo)
    return b is None or max(b[1][i] - b[0][i] for i in range(3)) < eps


def piece_bounds(items) -> tuple | None:
    """(min xyz, max xyz) over pieces from :func:`pieces` (None when there is nothing to draw)."""
    lo, hi = [_INF] * 3, [-_INF] * 3
    for pc in items:
        if not pc.indices:
            continue
        for c in range(3):
            col = pc.positions[c::3]
            lo[c] = min(lo[c], min(col))
            hi[c] = max(hi[c], max(col))
    return None if lo[0] == _INF else (tuple(lo), tuple(hi))


def _drawable(idx, X, Y, Z):
    """Triangles of the flat index list ``idx`` with a finite area above AREA_EPS.  A NaN or Inf anywhere
    in a triangle makes its squared cross product NaN or Inf, so this also drops those."""
    m = len(idx) - len(idx) % 3
    a, b, c = idx[0:m:3], idx[1:m:3], idx[2:m:3]
    keep = []
    put = keep.append
    for i, j, k in zip(a, b, c):
        ax, ay, az = X[i], Y[i], Z[i]
        ux, uy, uz = X[j] - ax, Y[j] - ay, Z[j] - az
        vx, vy, vz = X[k] - ax, Y[k] - ay, Z[k] - az
        fx, fy, fz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        put(AREA_EPS < fx * fx + fy * fy + fz * fz < _INF)
    kept = sum(keep)
    if kept == len(keep):
        return idx if m == len(idx) else idx[:m], kept
    out = array("I", bytes(12 * kept))
    for o, col in enumerate((a, b, c)):
        out[o::3] = array("I", compress(col, keep))
    return out, kept


def pieces(geo: Geometry, info: MeshInfo | None = None, stats: dict | None = None,
           eps: float = COLLAPSE_EPS) -> list:
    """The geometry cut into :class:`Piece` objects ready for drawing, one per (mesh group, material),
    in file order.

    Triangles that use a non-finite position and zero-area triangles are dropped, then the vertices
    no triangle uses; non-finite UVs become 0.  A group that had triangles but draws nothing (all its
    vertices within ``eps`` of one point - how mods hide a part - or nothing but broken triangles) is
    hidden and gives no piece, so an empty result with ``stats["hidden"]`` set means the whole mesh is
    hidden.  ``stats`` (a dict) is filled with: triangles (in), kept, dropped, hidden (group ids),
    nonfinite (vertices with a NaN/Inf position) and uv_fixed (UV values set to 0)."""
    p = geo.positions
    n = len(p) // 3
    X, Y, Z = (p[c:3 * n:3].tolist() for c in range(3))
    fin = math.isfinite
    nonfinite = 0
    if not fin(sum(X) + sum(Y) + sum(Z)):  # float32 values cannot overflow a double sum
        nonfinite = sum(1 for x, y, z in zip(X, Y, Z) if not (fin(x) and fin(y) and fin(z)))
    uvs = geo.uvs if geo.uvs is not None and len(geo.uvs) >= 2 * n else None
    uv_fixed = 0
    if uvs is not None and not fin(sum(uvs)):
        uv_fixed = sum(1 for v in uvs if not fin(v))
        uvs = array("f", [v if fin(v) else 0.0 for v in uvs])
    nrm = geo.normals if geo.normals is not None and len(geo.normals) >= 3 * n else None
    columns = {}  # uv / normal columns as lists, made when a piece first needs a gather

    # (group, material) -> index arrays, in first-seen order; indices past the vertices only come
    # from hand-made geometry (read_geometry checks them) and drop their triangles
    order = {}
    parts = list(geo.parts) + [0] * (len(geo.triangles) - len(geo.parts))
    total = 0
    for (mat, idx), gid in zip(geo.triangles, parts):
        m = len(idx) - len(idx) % 3
        total += m // 3
        if m and max(idx) >= n:
            idx = array("I", [v for t in zip(idx[0:m:3], idx[1:m:3], idx[2:m:3]) if max(t) < n for v in t])
        if len(idx) >= 3:
            order.setdefault((gid, mat), []).append(idx)

    found, drawn = [], {}  # every piece; group -> its pieces
    remap = None
    for (gid, mat), lists in order.items():
        group = drawn.setdefault(gid, [])
        idx = lists[0] if len(lists) == 1 else array("I", [v for one in lists for v in one])
        idx, kept = _drawable(idx, X, Y, Z)
        if not kept:
            continue
        used = sorted(set(idx))
        lo, hi = used[0], used[-1]
        if len(used) == hi - lo + 1:  # the usual case: each submesh has its own run of vertices
            ni = array("I", idx) if lo == 0 else array("I", [v - lo for v in idx])
            pos = p[3 * lo:3 * hi + 3]
            uv = uvs[2 * lo:2 * hi + 2] if uvs is not None else None
            nm = nrm[3 * lo:3 * hi + 3] if nrm is not None else None
        else:
            if remap is None:
                remap = [0] * n
            for k, v in enumerate(used):
                remap[v] = k
            ni = array("I", map(remap.__getitem__, idx))
            pos = _gather((X, Y, Z), used)
            uv = nm = None
            if uvs is not None:
                if "uv" not in columns:
                    columns["uv"] = tuple(uvs[c:2 * n:2].tolist() for c in range(2))
                uv = _gather(columns["uv"], used)
            if nrm is not None:
                if "nrm" not in columns:
                    columns["nrm"] = tuple(nrm[c:3 * n:3].tolist() for c in range(3))
                nm = _gather(columns["nrm"], used)
        name = material_name(info, mat) if info is not None else f"material{mat}"
        piece = Piece(name, gid, pos, ni, uv, nm)
        group.append(piece)
        found.append(piece)

    hidden = []
    for gid, items in drawn.items():
        b = piece_bounds(items)
        if b is None or max(b[1][c] - b[0][c] for c in range(3)) < eps:
            hidden.append(gid)
    out = [pc for pc in found if pc.group not in hidden]
    if stats is not None:
        kept = sum(pc.triangle_count for pc in out)
        stats.update(triangles=total, kept=kept, dropped=total - kept, hidden=hidden,
                     nonfinite=nonfinite, uv_fixed=uv_fixed)
    return out


def _gather(columns, used):
    """Float32 records (x, y, ... interleaved) of the vertices ``used`` (two or more) from per-coordinate
    lists."""
    w = len(columns)
    out = array("f", bytes(4 * w * len(used)))
    get = itemgetter(*used)
    for c, col in enumerate(columns):
        out[c::w] = array("f", get(col))
    return out
