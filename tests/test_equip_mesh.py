"""MH Wilds .mesh reading and cleaning for the 3D preview (synthetic files from equip_mesh_fixtures)."""

import math
import random
import struct
import time
from array import array

import equip_mesh_fixtures as F
import pytest

from mhwmm.equip import EquipError
from mhwmm.equip import mesh as M
from mhwmm.equip.mesh import Geometry, MeshError, Unsupported

NAN, INF = float("nan"), float("inf")
TRI = [(0.0, 0.0, 1.0), (1.0, 0.0, 1.0), (0.0, 1.0, 1.0)]


def two_materials():
    return [F.Sub(0, F.QUAD, F.QUAD_UV, F.QUAD_TRIS),
            F.Sub(1, TRI, F.QUAD_UV[:3], [(0, 1, 2)], group=3, normals=[(1, 0, 0), (0, 0, -1.01), (0, 0.5, 0)])]


def corners(piece_or_geo, idx=None):
    """Triangles as sorted tuples of corner positions (to compare geometry before and after cleaning)."""
    p = piece_or_geo.positions
    idx = piece_or_geo.indices if idx is None else idx
    return sorted(tuple(tuple(p[3 * v:3 * v + 3]) for v in idx[t:t + 3]) for t in range(0, len(idx), 3))


# ---- tables ------------------------------------------------------------------------------------
def test_errors_are_equip_errors():
    assert issubclass(MeshError, EquipError) and issubclass(MeshError, ValueError)
    assert issubclass(Unsupported, MeshError)


@pytest.mark.parametrize("index32", [False, True])
def test_header_and_tables(index32):
    data, blob = F.build_mesh(["Body", "Trim"], two_materials(), index32=index32)
    assert blob is None and M.is_mesh(data)
    info = M.read_info(data)
    assert info.size == len(data) and info.index32 is index32
    assert info.material_names == ["Body", "Trim"] and info.names == ["Body", "Trim", "root"]
    assert info.uv_count == 1 and not info.needs_streaming and info.streaming_entries == []
    assert len(info.lods) == 1 and info.lods[0].triangle_count == 3 and info.lods[0].buffers == [0]
    assert [(g[0], g[1]) for g in info.lods[0].groups] == [(0, 4), (3, 3)]
    assert info.bbox_min == (0.0, 0.0, 0.0) and info.bbox_max == (1.0, 1.0, 1.0)
    assert info.buffers[0].vertex_count == 7
    assert M.material_name(info, 1) == "Trim" and M.material_name(info, 9) == "material9"


@pytest.mark.parametrize("index32", [False, True])
def test_geometry(index32):
    data, _ = F.build_mesh(["Body", "Trim"], two_materials(), index32=index32)
    geo = M.read_geometry(data, normals=True)
    assert geo.lod == 0 and len(geo.positions) == 21
    assert list(geo.positions[:6]) == [0, 0, 0, 1, 0, 0] and list(geo.positions[-3:]) == [0, 1, 1]
    assert list(geo.uvs[:4]) == [0.0, 1.0, 1.0, 1.0]  # stored as is: v = 0 is the top of the texture
    assert list(geo.normals[:3]) == [0, 1, 0]
    assert list(geo.normals[12:15]) == [1, 0, 0] and list(geo.normals[15:18]) == [0, 0, -1]  # -128 -> -1
    assert geo.normals[19] == pytest.approx(64 / 127)
    assert [(m, list(i)) for m, i in geo.triangles] == [(0, [0, 1, 2, 0, 2, 3]), (1, [4, 5, 6])]
    assert geo.parts == [0, 3]
    assert M.bounds(geo) == ((0, 0, 0), (1, 1, 1)) and not M.is_collapsed(geo)
    plain = M.read_geometry(data, uv=False)
    assert plain.uvs is None and plain.normals is None and plain.positions == geo.positions


def test_missing_uvs_read_as_zero():
    data, _ = F.build_mesh(["Body"], [F.quad()], uv0=False)
    info = M.read_info(data)
    assert info.uv_count == 0
    assert list(M.read_geometry(data, info).uvs) == [0.0] * 8


def test_odd_16_bit_index_ranges_are_padded():
    subs = [F.Sub(0, TRI, None, [(0, 1, 2)]), F.Sub(0, TRI, None, [(0, 2, 1)]), F.Sub(1, TRI, None, [(1, 2, 0)])]
    data, _ = F.build_mesh(["A", "B"], subs)
    info = M.read_info(data)
    assert [s.index_start for s in info.lods[0].submeshes] == [0, 4, 8]
    geo = M.read_geometry(data, info)
    assert [list(i) for _, i in geo.triangles] == [[0, 1, 2], [3, 5, 4], [7, 8, 6]]


def test_streaming_path_and_is_mesh():
    p = "natives/STM/Art/Model/Item/it09/00/0021/it0900_0021_0.mesh.241111606"
    assert M.streaming_path(p) == "natives/STM/streaming/Art/Model/Item/it09/00/0021/it0900_0021_0.mesh.241111606"
    assert M.streaming_path("NATIVES\\stm\\art\\x.mesh.241111606") == "NATIVES\\stm\\streaming\\art\\x.mesh.241111606"
    assert M.streaming_path(M.streaming_path(p)) is None
    assert M.streaming_path("Art/Model/x.mesh.241111606") is None
    assert not M.is_mesh(b"") and not M.is_mesh(b"\0" * 16)
    assert M.is_mesh(b"MPLY" + bytes(12))


# ---- streaming and LODs ------------------------------------------------------------------------
def test_streamed_lod0_needs_the_blob():
    full, small = two_materials(), two_materials()[:1]
    data, blob = F.build_mesh(["Body", "Trim"], [full, small], streamed=(0,))
    info = M.read_info(data)
    assert info.needs_streaming and info.streaming_entries == [(0, len(blob))]
    assert [L.buffers for L in info.lods] == [[1], [0]] and sorted(info.buffers) == [0, 1]
    assert M.available_lods(info, None) == [1] and M.available_lods(info, blob) == [0, 1]
    assert M.available_lods(info, blob[:-16]) == [1]  # a short blob does not hold the index block
    resident = M.read_geometry(data, info)
    assert resident.lod == 1 and len(resident.triangles) == 1
    streamed = M.read_geometry(data, info, streaming=blob, normals=True)
    assert streamed.lod == 0 and streamed.parts == [0, 3]
    whole, _ = F.build_mesh(["Body", "Trim"], full)
    plain = M.read_geometry(whole, normals=True)
    assert streamed.positions == plain.positions and streamed.uvs == plain.uvs
    assert streamed.normals == plain.normals
    assert [list(i) for _, i in streamed.triangles] == [list(i) for _, i in plain.triangles]
    with pytest.raises(MeshError):
        M.read_geometry(data, info, lod=0)
    with pytest.raises(MeshError):
        M.read_geometry(data, info, lod=5)
    # a full .mesh copy under streaming/ (some mods ship one) is not the blob
    assert M.read_geometry(data, info, streaming=data).lod == 1


def test_empty_streaming_slots_and_shared_resident_buffer():
    data, blob = F.build_mesh(["A"], [[F.grid(4)], [F.grid(2)], [F.grid(1)]], streamed=(0,), empty_slots=2)
    info = M.read_info(data)
    assert info.streaming_entries[:2] == [(0, 0), (0, 0)]
    assert sorted(info.buffers) == [0, 3] and [L.buffers for L in info.lods] == [[3], [0], [0]]
    assert [L.triangle_count for L in info.lods] == [32, 8, 2]
    lod0 = M.read_geometry(data, info, streaming=blob)
    assert lod0.lod == 0 and len(lod0.positions) == 3 * 25
    assert [p.triangle_count for p in M.pieces(lod0, info)] == [32]
    assert info.buffers[0].vertex_count == 9 + 4  # LOD1 and LOD2 share the resident buffer
    lod2 = M.read_geometry(data, info, lod=2)
    assert len(lod2.positions) == 3 * 13 and list(lod2.triangles[0][1]) == [9, 10, 12, 9, 12, 11]
    (piece,) = M.pieces(lod2, info)
    assert piece.vertex_count == 4 and list(piece.indices) == [0, 1, 3, 0, 3, 2]
    assert corners(piece) == corners(lod2, lod2.triangles[0][1])


def test_lod_choice_with_a_triangle_budget():
    data, blob = F.build_mesh(["A"], [[F.grid(4)], [F.grid(2)], [F.grid(1)]], streamed=(0,))
    info = M.read_info(data)
    assert M.pick_lod(info, None) == 1 and M.pick_lod(info, blob) == 0
    assert M.pick_lod(info, blob, max_triangles=32) == 0
    assert M.pick_lod(info, blob, max_triangles=31) == 1
    assert M.pick_lod(info, blob, max_triangles=5) == 2
    assert M.pick_lod(info, blob, max_triangles=1) == 2  # none fits: the smallest one
    assert M.pick_lod(info, None, max_triangles=1) == 2
    assert M.read_geometry(data, info, streaming=blob, max_triangles=10).lod == 1


def test_only_streamed_lods_without_the_blob():
    data, blob = F.build_mesh(["A"], [F.quad()], streamed=(0,))
    info = M.read_info(data)
    assert info.buffers[0].vertex_count == 0 and M.available_lods(info, None) == []
    with pytest.raises(MeshError):
        M.pick_lod(info, None)
    with pytest.raises(MeshError):
        M.read_geometry(data, info)
    assert M.read_geometry(data, info, streaming=blob).lod == 0


# ---- what is not read ----------------------------------------------------------------------------
def test_mply_and_other_versions_are_unsupported():
    data, _ = F.build_mesh(["A"], [F.quad()])
    with pytest.raises(Unsupported):
        M.read_info(b"MPLY" + data[4:])
    other = bytearray(data)
    struct.pack_into("<I", other, 4, 230517984)
    with pytest.raises(Unsupported):
        M.read_info(bytes(other))
    with pytest.raises(MeshError) as exc:
        M.read_info(b"TEX\0" + data[4:])
    assert not isinstance(exc.value, Unsupported)
    with pytest.raises(MeshError):
        M.read_info(data + b"\0")  # header size != file size


def _shared_tables(lods: int, records: int) -> bytes:
    """A small crafted mesh: ``lods`` LOD slots over ``records`` LOD records that all share one group table
    of 255 pointers to one group record of 255 submeshes."""
    d = bytearray(F.build_mesh(["m"], [F.quad()])[0])

    def put(raw: bytes) -> int:
        d.extend(b"\0" * (-len(d) % 16))
        d.extend(raw)
        return len(d) - len(raw)

    group = struct.pack("<BB6xII", 0, 255, 0, 3) + struct.pack("<BBBBiIIiiiI", 0, 0, 0, 0, 0, 3, 0, 0, 0, 0, 0) * 255
    table = put(struct.pack("<255q", *[put(group)] * 255))
    recs = [put(struct.pack("<BBBBfq", 255, 0, i, 0, 1.0, table)) for i in range(records)]
    slots = put(struct.pack(f"<{lods}q", *(recs[i % records] for i in range(lods))))
    meshdata = struct.unpack_from("<q", d, 0x30)[0]
    d[meshdata] = lods
    struct.pack_into("<q", d, meshdata + 56, slots)
    struct.pack_into("<I", d, 8, len(d))
    return bytes(d)


def test_shared_tables_cannot_make_millions_of_submeshes():
    data = _shared_tables(32, 32)  # about 11 KB: 32 x 255 x 255 submesh records
    t0 = time.perf_counter()
    with pytest.raises(MeshError, match="서브메시가 너무 많습니다"):
        M.read_info(data)
    # 255 slots of one record of 65,025 submeshes is allowed: each record is summed up once
    info = M.read_info(_shared_tables(255, 1))
    assert len(info.lods) == 255 and M.available_lods(info, None) == list(range(255))
    assert M.pick_lod(info, None, 150_000) == 0 and info.lods[254].triangle_count == 65_025
    assert time.perf_counter() - t0 < 2.0


def test_damaged_files_raise_only_mesh_error():
    rng = random.Random(20261020)
    shapes = [
        F.build_mesh(["Body", "Trim"], two_materials()),
        F.build_mesh(["Body", "Trim"], two_materials(), index32=True),
        F.build_mesh(["A"], [[F.grid(3), F.quad(0, 1)], [F.grid(1)]], streamed=(0,), empty_slots=1),
    ]
    outcome = {"ok": 0, "error": 0}
    for data, blob in shapes:
        for trial in range(240):
            d, b = bytearray(data), blob
            kind = trial % 4
            if kind == 0:  # truncated, header size kept consistent
                d = d[:rng.randrange(len(d))]
                if len(d) >= 12:
                    struct.pack_into("<I", d, 8, len(d))
            elif kind in (1, 2):  # a few changed bytes, mostly in the tables
                for _ in range(rng.randrange(1, 6)):
                    d[rng.randrange(min(len(d), 0x300) if kind == 1 else len(d))] = rng.randrange(256)
            elif blob is not None:  # a damaged or short streaming blob
                b = bytearray(blob[:rng.randrange(len(blob) + 1)])
                for _ in range(rng.randrange(4)):
                    if b:
                        b[rng.randrange(len(b))] = rng.randrange(256)
                b = bytes(b)
            else:  # random bytes behind a valid magic and version
                d = bytearray(data[:8]) + bytearray(rng.randrange(256) for _ in range(rng.randrange(len(data))))
            try:
                info = M.read_info(bytes(d))
                for lod in M.available_lods(info, b):
                    M.pieces(M.read_geometry(bytes(d), info, streaming=b, lod=lod, normals=True), info)
                M.pieces(M.read_geometry(bytes(d), info, streaming=b, normals=True), info)
                outcome["ok"] += 1
            except MeshError:
                outcome["error"] += 1
    assert outcome["ok"] > 50 and outcome["error"] > 50


# ---- cleaning for drawing -------------------------------------------------------------------------
def test_pieces_per_group_and_material():
    subs = [F.quad(0, 0), F.quad(1, 0, (2, 0, 0)), F.quad(0, 0, (4, 0, 0)), F.quad(0, 5, (0, 2, 0))]
    data, _ = F.build_mesh(["Body", "Trim"], subs)
    info = M.read_info(data)
    geo = M.read_geometry(data, info, normals=True)
    stats = {}
    ps = M.pieces(geo, info, stats)
    assert [(p.material_name, p.group, p.vertex_count, p.triangle_count) for p in ps] == [
        ("Body", 0, 8, 4), ("Trim", 0, 4, 2), ("Body", 5, 4, 2)]
    assert stats == {"triangles": 8, "kept": 8, "dropped": 0, "hidden": [], "nonfinite": 0, "uv_fixed": 0}
    for p in ps:
        assert p.positions.typecode == "f" and p.indices.typecode == "I"
        assert len(p.uvs) == 2 * p.vertex_count and len(p.normals) == 3 * p.vertex_count
        assert sorted(set(p.indices)) == list(range(p.vertex_count))
    assert corners(ps[0]) == sorted(corners(geo, geo.triangles[0][1]) + corners(geo, geo.triangles[2][1]))
    assert M.piece_bounds(ps) == ((0, 0, 0), (5, 3, 0)) and M.piece_bounds([]) is None
    assert [p.material_name for p in M.pieces(geo)] == ["material0", "material1", "material0"]
    bare = M.pieces(M.read_geometry(data, info, uv=False))
    assert all(p.uvs is None and p.normals is None for p in bare)


def test_non_finite_positions_and_uvs():
    pts = F.QUAD + [(NAN, 0.0, 0.0), (0.0, INF, 0.0), (2.0, 2.0, 2.0)]
    uvs = [(NAN, 0.5), (INF, -INF)] + F.QUAD_UV[2:] + [(0, 0)] * 3
    tris = F.QUAD_TRIS + [(0, 1, 4), (5, 2, 3), (1, 6, 2)]
    data, _ = F.build_mesh(["Body"], [F.Sub(0, pts, uvs, tris)])
    info = M.read_info(data)
    geo = M.read_geometry(data, info)
    assert math.isnan(geo.positions[12]) and math.isnan(geo.uvs[0])
    stats = {}
    (piece,) = M.pieces(geo, info, stats)
    assert piece.triangle_count == 3 and piece.vertex_count == 5  # the NaN and Inf vertices are gone
    assert all(math.isfinite(v) for v in piece.positions) and all(math.isfinite(v) for v in piece.uvs)
    assert list(piece.uvs[:4]) == [0.0, 0.5, 0.0, 0.0]
    assert stats["nonfinite"] == 2 and stats["uv_fixed"] == 3 and stats["dropped"] == 2
    assert M.bounds(geo) == ((0, 0, 0), (2, 2, 2))
    assert M.piece_bounds([piece]) == ((0, 0, 0), (2, 2, 2))


def test_stray_and_degenerate_geometry_is_dropped():
    pts = F.QUAD + [(1e9, 1e9, 1e9), (0.5, 0.5, 0.0), (5.0, 5.0, 5.0), (5.0, 5.0, 5.0), (7.0, 7.0, 7.0)]
    tris = F.QUAD_TRIS + [(0, 1, 1), (0, 5, 2), (6, 7, 8), (6, 6, 6), (0, 2, 5)]
    data, _ = F.build_mesh(["Body"], [F.Sub(0, pts, None, tris)])
    geo = M.read_geometry(data)
    assert M.bounds(geo)[1] == (7, 7, 7)  # the unused far vertex does not count, the others do
    stats = {}
    (piece,) = M.pieces(geo, stats=stats)
    assert stats["dropped"] == 5 and stats["kept"] == 2
    assert piece.vertex_count == 4 and corners(piece) == corners(geo, array("I", [0, 1, 2, 0, 2, 3]))
    assert M.piece_bounds([piece]) == ((0, 0, 0), (1, 1, 0))


@pytest.mark.parametrize("spread", [0.0, 1e-4])
def test_collapsed_group_is_hidden(spread):
    hidden = [(0.3 + spread * (k % 2), 1.2, 0.5 + spread * (k // 2)) for k in range(4)]
    subs = [F.quad(0, 0), F.Sub(1, hidden, F.QUAD_UV, F.QUAD_TRIS, group=2)]
    data, _ = F.build_mesh(["Blade", "Sheath"], subs)
    info = M.read_info(data)
    geo = M.read_geometry(data, info)
    stats = {}
    ps = M.pieces(geo, info, stats)
    assert [(p.material_name, p.group) for p in ps] == [("Blade", 0)]
    assert stats["hidden"] == [2] and stats["dropped"] == 2
    assert not M.is_collapsed(geo)


def test_whole_mesh_collapsed():
    pts = [(0.08, 1e-5, 1.13)] * 3 + [(0.08, 1e-5 + 1e-7, 1.13)]
    data, _ = F.build_mesh(["lambert2"], [F.Sub(0, pts, None, F.QUAD_TRIS, group=0), F.Sub(0, pts, None, [(0, 1, 3)], group=3)])
    geo = M.read_geometry(data)
    stats = {}
    assert M.pieces(geo, stats=stats) == [] and stats["hidden"] == [0, 3] and stats["kept"] == 0
    assert M.is_collapsed(geo)


def test_scattered_vertices_are_compacted():
    rng = random.Random(7)
    n = 40
    pts = [(rng.random(), rng.random(), rng.random()) for _ in range(n)]
    tris = [tuple(rng.sample(range(0, n, 2), 3)) for _ in range(30)]  # only even vertices are used
    data, _ = F.build_mesh(["A"], [F.Sub(0, pts, [(p[0], p[1]) for p in pts], tris)])
    geo = M.read_geometry(data, normals=True)
    (piece,) = M.pieces(geo)
    assert piece.vertex_count == len({v for t in tris for v in t})
    assert corners(piece) == corners(geo, geo.triangles[0][1])
    for v in range(piece.vertex_count):  # UVs and normals follow their vertices
        x, y = piece.positions[3 * v], piece.positions[3 * v + 1]
        assert piece.uvs[2 * v] == pytest.approx(x, abs=1e-3) and piece.uvs[2 * v + 1] == pytest.approx(y, abs=1e-3)
        assert list(piece.normals[3 * v:3 * v + 3]) == [0, 1, 0]


def test_hand_made_geometry_does_not_break_cleaning():
    pos = array("f", [0, 0, 0, 1, 0, 0, 0, 1, 0, 9])  # a trailing partial vertex
    geo = Geometry(0, pos, array("f", [0.0] * 3), None,  # too few UVs: ignored
                   [(0, array("H", [0, 1, 2, 0, 1, 7, 2])), (1, array("I", []))])
    stats = {}
    (piece,) = M.pieces(geo, stats=stats)
    assert piece.triangle_count == 1 and piece.uvs is None and piece.group == 0
    assert stats["triangles"] == 2 and stats["dropped"] == 1
    assert M.bounds(geo) == ((0, 0, 0), (1, 1, 0))
    assert M.pieces(Geometry(0, array("f"), None, None)) == [] and M.bounds(Geometry(0, array("f"), None, None)) is None
