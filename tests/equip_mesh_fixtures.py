"""Synthetic MH Wilds .mesh files for the tests (nothing from the game or from mods): build_mesh()
writes the layout mhwmm.equip.mesh reads (REasy's description, see mesh.py).

    Sub(material, positions, uvs, triangles, group=0, normals=None)
        material: index into the material names; positions [(x, y, z)]; uvs [(u, v)] (None: zeros);
        triangles [(a, b, c)] counted from the submesh's first vertex; normals [(x, y, z)] (None: +Y).
        Submeshes of one group are stored together, groups in first-seen order.  A plain tuple
        (material, positions, uvs, triangles) works too.

    build_mesh(materials, lods, streamed=(), ...) -> (mesh bytes, streaming blob or None)
        lods: [LOD0 submeshes, LOD1 submeshes, ...]; a plain submesh list is one LOD.  The LODs in
        ``streamed`` get their own streaming buffers in a headerless blob (as vanilla files do, after
        ``empty_slots`` empty StreamingInfo entries); the others share the resident buffer 0.
"""

from __future__ import annotations

import math
import struct
from typing import NamedTuple

from mhwmm.equip import mesh

HEADER = mesh.HEADER_SIZE


class Sub(NamedTuple):
    material: int
    positions: list
    uvs: list | None
    triangles: list
    group: int = 0
    normals: list | None = None


def _pad(b: bytearray, n: int = 16) -> None:
    b.extend(b"\0" * (-len(b) % n))


def _snorm(v: float) -> int:
    return max(-128, min(127, round(v * 127)))


def _buffer(lods, index32: bool, uv0: bool):
    """Vertex block (arrays one after another: position f32 x3, normal + tangent snorm8 x8, UV0 half x2)
    and index block of the LODs that share one buffer -> (vertex bytes, index bytes, elements,
    {lod: [(group id, vertex count, index count, [(material, index count, first index, base vertex)])]})."""
    pos, nrm, uv, idx = bytearray(), bytearray(), bytearray(), bytearray()
    base = first = 0
    groups_of = {}
    for li, subs in lods:
        groups = {}
        for s in subs:
            groups.setdefault(s.group, []).append(s)
        recs = []
        for gid, members in groups.items():
            gverts = gindices = 0
            subrecs = []
            for s in members:
                uvs = s.uvs or [(0.0, 0.0)] * len(s.positions)
                for k, (x, y, z) in enumerate(s.positions):
                    pos += struct.pack("<3f", x, y, z)
                    n = s.normals[k] if s.normals else (0.0, 1.0, 0.0)
                    nrm += struct.pack("<8b", *(_snorm(c) for c in n), 0, 127, 0, 0, 127)
                    uv += struct.pack("<2e", *uvs[k])
                flat = [i for tri in s.triangles for i in tri]
                idx += struct.pack(f"<{len(flat)}{'I' if index32 else 'H'}", *flat)
                stored = len(flat)
                if not index32 and stored % 2:  # Wilds keeps each 16-bit index range 4-byte aligned
                    idx += b"\0\0"
                    stored += 1
                subrecs.append((s.material, len(flat), first, base))
                first += stored
                base += len(s.positions)
                gverts += len(s.positions)
                gindices += stored
            recs.append((gid, gverts, gindices, subrecs))
        groups_of[li] = recs
    elements = [(mesh.POSITION, 12, 0), (mesh.NORMAL_TANGENT, 8, len(pos))]
    if uv0:
        elements.append((mesh.UV0, 4, len(pos) + len(nrm)))
    vb = pos + nrm + (uv if uv0 else b"")
    _pad(vb)
    _pad(idx)
    return bytes(vb), bytes(idx), elements, groups_of


def build_mesh(materials, lods, streamed=(), index32=False, uv0=True, empty_slots=0, bones=("root",)):
    """A .mesh (and its streaming blob when some LOD is streamed); see the module docstring."""
    if lods and not isinstance(lods[0], list):
        lods = [lods]
    lods = [[s if isinstance(s, Sub) else Sub(*s) for s in subs] for subs in lods]
    resident = _buffer([(i, s) for i, s in enumerate(lods) if i not in streamed], index32, uv0)
    streams = [(i, _buffer([(i, lods[i])], index32, uv0)) for i in sorted(streamed)]
    buffer_of = {i: empty_slots + k + 1 for k, (i, _b) in enumerate(streams)}
    groups_of = dict(resident[3])
    for _i, b in streams:
        groups_of.update(b[3])

    d = bytearray(HEADER)

    def put(raw: bytes, align: int = 16) -> int:
        _pad(d, align)
        off = len(d)
        d.extend(raw)
        return off

    names = list(materials) + list(bones)
    name_offs = [put(n.encode() + b"\0", 1) for n in names]
    names_table = put(struct.pack(f"<{len(names)}q", *name_offs))
    matidx = put(struct.pack(f"<{len(materials)}h", *range(len(materials)))) if materials else 0
    lod_recs = []
    for li in range(len(lods)):
        buf = buffer_of.get(li, 0)
        g_offs = []
        for gid, gverts, gindices, subrecs in groups_of[li]:
            rec = bytearray(struct.pack("<BB6xII", gid, len(subrecs), gverts, gindices))
            for mat, count, first, base in subrecs:
                rec += struct.pack("<BBBBiIIiiiI", mat, 0, buf, 0, 0, count, first, base, 0, 0, 0)
            g_offs.append(put(bytes(rec)))
        g_table = put(struct.pack(f"<{len(g_offs)}q", *g_offs))
        lod_recs.append(put(struct.pack("<BBBBfq", len(g_offs), 0, li, 0, 0.5 ** li, g_table)))
    lod_table = put(struct.pack(f"<{len(lod_recs)}q", *lod_recs))
    pts = [c for subs in lods for s in subs for c in s.positions if all(math.isfinite(v) for v in c)]
    lo = [min(c[i] for c in pts) for i in range(3)] if pts else [0.0] * 3
    hi = [max(c[i] for c in pts) for i in range(3)] if pts else [0.0] * 3
    ctr = [(lo[i] + hi[i]) / 2 for i in range(3)]
    rad = max((math.dist(c, ctr) for c in pts), default=0.0)
    meshdata = put(struct.pack("<BBBBHBB4f3fI3fIq", len(lod_recs), len(materials), 1 if uv0 else 0, 0,
                               sum(len(s) for s in lods), int(index32), 0, *ctr, rad, *lo, 0, *hi, 0, lod_table))

    el_offs = [put(b"".join(struct.pack("<HHI", *e) for e in b[2])) for b in [resident] + [b for _i, b in streams]]
    blob = bytearray()
    entries = [(0, 0)] * empty_slots
    for _i, (vb, ib, _e, _g) in streams:
        _pad(blob)
        entries.append((len(blob), len(vb) + len(ib)))
        blob += vb + ib
    stream_info = 0
    if entries:
        ent = put(struct.pack(f"<{2 * len(entries)}I", *(v for e in entries for v in e)))
        stream_info = put(struct.pack("<iiq", len(entries), 0, ent))
    layout = put(b"\0" * (mesh.BUFFER_LAYOUT_SIZE * (1 + len(streams))))
    vb, ib = resident[0], resident[1]
    vstart = put(vb)
    d.extend(ib)
    struct.pack_into("<qqqiiHH", d, layout, el_offs[0], vstart, 0, len(vb) + len(ib), len(vb),
                     len(resident[2]), len(resident[2]))
    for k, (_i, (svb, _ib, sel, _g)) in enumerate(streams):
        rec = layout + mesh.BUFFER_LAYOUT_SIZE * (k + 1)
        start, size = entries[empty_slots + k]
        struct.pack_into("<qqqIIHH", d, rec, el_offs[k + 1], start, 0, size, len(svb), len(sel), len(sel))
        struct.pack_into("<i", d, rec + 60, empty_slots + k + 1)  # one-based StreamingInfo slot

    struct.pack_into("<III", d, 0, mesh.MESH_MAGIC, mesh.WILDS_INTERNAL_VERSION, len(d))
    struct.pack_into("<H", d, 0x14, len(names))
    struct.pack_into("<H", d, 0x1A, (len(streams) + 1) & 7)
    struct.pack_into("<q", d, 0x28, vstart)
    struct.pack_into("<16q", d, 0x30, meshdata, 0, 0, 0, 0, layout, 0, 0, 0, 0, matidx, 0, 0,
                     names_table, stream_info, 0)
    return bytes(d), (bytes(blob) if streams else None)


# ---- ready-made shapes ------------------------------------------------------------------------
QUAD = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)]
QUAD_UV = [(0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)]
QUAD_TRIS = [(0, 1, 2), (0, 2, 3)]


def quad(material=0, group=0, offset=(0.0, 0.0, 0.0), size=1.0) -> Sub:
    """A square of two triangles (counter-clockwise seen from +Z)."""
    ox, oy, oz = offset
    pts = [(ox + x * size, oy + y * size, oz + z) for x, y, z in QUAD]
    return Sub(material, pts, QUAD_UV, QUAD_TRIS, group)


def grid(n: int, material=0, group=0, z=0.0) -> Sub:
    """An n x n square grid in the XY plane (2 * n * n triangles, (n + 1)^2 vertices)."""
    pts = [(x / n, y / n, z) for y in range(n + 1) for x in range(n + 1)]
    uvs = [(x / n, 1 - y / n) for y in range(n + 1) for x in range(n + 1)]
    tris = []
    for y in range(n):
        for x in range(n):
            a = y * (n + 1) + x
            tris += [(a, a + 1, a + n + 2), (a, a + n + 2, a + n + 1)]
    return Sub(material, pts, uvs, tris, group)
