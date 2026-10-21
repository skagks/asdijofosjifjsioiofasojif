"""The small 3D renderer (mhwmm/equip/render.py) on synthetic geometry only."""

from __future__ import annotations

import hashlib
import sys

import pytest

from mhwmm.equip import EquipError
from mhwmm.equip import render as R

NAN = float("nan")
INF = float("inf")
RED, GREEN, BLUE, YELLOW = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)


@pytest.fixture
def Image():
    return pytest.importorskip("PIL.Image")


def quad(x0=-1.0, y0=-1.0, x1=1.0, y1=1.0, z=0.0, back=False):
    """(pos, idx, nrm, uv) of a rectangle in the plane z facing +Z (back: facing -Z).  Vertices: top-left,
    top-right, bottom-right, bottom-left; UV (0, 0) is the top-left corner seen from +Z."""
    pos = [x0, y1, z, x1, y1, z, x1, y0, z, x0, y0, z]
    idx = [3, 1, 2, 3, 0, 1] if back else [3, 2, 1, 3, 1, 0]  # counter-clockwise seen from its front
    nrm = [0.0, 0.0, -1.0 if back else 1.0] * 4
    uv = [0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0]
    return pos, idx, nrm, uv


def part(geo, material=None, **changes):
    pos, idx, nrm, uv = geo
    values = dict(pos=pos, idx=idx, nrm=nrm, uv=uv)
    values.update(changes)
    return R.Part(values["pos"], values["idx"], values["nrm"], values["uv"], material)


def box_mesh(sx=1.0, sy=1.0, sz=1.0, dx=0.0):
    """(pos, idx, nrm, uv) of a closed box: four vertices per face, counter-clockwise seen from outside."""
    pos, idx, nrm, uv = [], [], [], []
    half = (sx, sy, sz)
    for axis in range(3):
        for sign in (1.0, -1.0):
            u, v = ((axis + 1) % 3, (axis + 2) % 3) if sign > 0 else ((axis + 2) % 3, (axis + 1) % 3)
            base = len(pos) // 3
            for cu, cv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                p = [0.0, 0.0, 0.0]
                p[axis] = sign * half[axis]
                p[u] = cu * half[u]
                p[v] = cv * half[v]
                p[0] += dx
                pos += p
                n = [0.0, 0.0, 0.0]
                n[axis] = sign
                nrm += n
                uv += [(cu + 1) / 2, (1 - cv) / 2]
            idx += [base, base + 1, base + 2, base, base + 2, base + 3]
    return pos, idx, nrm, uv


def quadrants(Image, size=8, alpha=255):
    """A texture with red / green / blue / yellow quarters (top-left, top-right, bottom-left, bottom-right)."""
    img = Image.new("RGBA", (size, size))
    h = size // 2
    for color, (x, y) in ((RED, (0, 0)), (GREEN, (h, 0)), (BLUE, (0, h)), (YELLOW, (h, h))):
        img.paste((*color, alpha), (x, y, x + h, y + h))
    return img


def near(pixel, color, tol=3):
    return all(abs(a - b) <= tol for a, b in zip(pixel, color))


def lit(color):
    """A colour as drawn on a surface facing the camera (full light = 254/255)."""
    return tuple(c * 254 // 255 for c in color)


def alpha_box(img):
    return img.getchannel("A").getbbox()


def covered(img):
    """Pixels with any coverage."""
    hist = img.getchannel("A").histogram()
    return sum(hist) - hist[0]


# ---- geometry (no Pillow needed) ------------------------------------------------------
def test_bounds_skip_stray_broken_and_collapsed_vertices():
    pos, idx, nrm, uv = quad()
    assert R.bounds([part(quad())]) == ((-1.0, -1.0, 0.0), (1.0, 1.0, 0.0))
    stray = part(quad(), pos=pos + [1e9, 1e9, 1e9], nrm=None, uv=None)  # a vertex no triangle uses
    assert R.bounds([stray]) == ((-1.0, -1.0, 0.0), (1.0, 1.0, 0.0))
    hidden = R.Part([50.0, 50.0, 50.0] * 4, idx)  # a group collapsed to one point
    assert R.bounds([hidden]) is None
    assert R.bounds([part(quad()), hidden]) == ((-1.0, -1.0, 0.0), (1.0, 1.0, 0.0))
    broken = part(quad(), pos=[NAN, NAN, NAN] + pos[3:])  # vertex 0: only triangle (3, 1, 0) uses it
    assert R.bounds([broken]) == ((-1.0, -1.0, 0.0), (1.0, 1.0, 0.0))
    assert R.bounds([]) is None
    assert R.bounds([R.Part([], []), R.Part(None, None)]) is None


def test_union_bounds():
    a = ((0, 0, 0), (1, 1, 1))
    b = ((-1, 0.5, 0), (0.5, 2, 1))
    assert R.union_bounds(a, None, b) == ((-1, 0, 0), (1, 2, 1))
    assert R.union_bounds(None) is None


def test_zbuffer_covers_every_pixel_centre_once():
    # margin 0: the quad's edges lie half a pixel outside the outermost pixel centres
    scene = R.Scene([part(quad())], 10, 10, margin=0.0)
    ib = R.raster_zbuffer(scene)
    assert ib.count(0) == 0 and set(ib) == {1, 2}
    back = R.Scene([part(quad(back=True))], 10, 10, margin=0.0)
    assert set(R.raster_zbuffer(back)) == {0}  # culled
    assert 0 not in R.raster_zbuffer(back, cull=False)


def test_broken_triangles_are_left_out():
    pos, idx, nrm, uv = quad()
    cases = {  # vertex 0: only triangle (3, 1, 0) uses it
        "nan": [NAN] * 3 + pos[3:],
        "inf": [INF, 0.0, -INF] + pos[3:],
        "far": [1e9, 0.0, 0.0] + pos[3:],
    }
    for name, p in cases.items():
        scene = R.Scene([part(quad(), pos=p)], 16, 16)
        assert len(scene.A) == 1, name
    scene = R.Scene([part(quad(), idx=[0, 1, 7, -1, 0, 1] + idx + [1, 2])], 16, 16)  # bad indices, a stray pair
    assert len(scene.A) == 2
    scene = R.Scene([part(quad(), idx=[0, 0, 1, 2, 2, 2] + idx)], 16, 16)  # no area
    assert len(scene.A) == 2


def test_camera_presets():
    assert R.camera([], "front") == R.rotation(*R.FRONT_VIEW) == R.rotation(-20, 10)
    assert R.view_for("armor", "helm") == "front"
    assert R.view_for("palico", "body") == "front"
    assert R.view_for("palico", "weapon") == "auto"
    assert R.view_for("weapon", "main") == "auto"
    assert R.view_for("kinsect") == "auto"
    with pytest.raises(ValueError):
        R.camera([], "side")
    # a blade: long along X, thin along Z -> its broad side (+Z) faces the viewer, rolled onto the diagonal
    blade = [part(box_mesh(2.0, 0.1, 0.02))]
    plain = R.auto_matrix(blade)
    assert plain == R._matmul(R.rotation(*R.AUTO_VIEW), R._ALIGN[2])
    m = R.camera(blade)
    assert m != plain
    roll = R.best_roll([1.0, -1.0, 1.0, -1.0], [0.1, 0.1, -0.1, -0.1])
    assert abs(roll) == 45.0
    assert R.camera([]) == R._matmul(R.rotation(*R.AUTO_VIEW), R._ALIGN[2])  # nothing to look at


# ---- pictures -------------------------------------------------------------------------
def test_texture_orientation_and_colour(Image):
    img = R.render([part(quad(), R.Material(quadrants(Image)))], size=64)
    assert img.mode == "RGBA" and img.size == (64, 64)
    px = img.load()
    assert near(px[16, 16], (*lit(RED), 255))  # UV (0, 0) = top-left, no V flip
    assert near(px[48, 16], (*lit(GREEN), 255))
    assert near(px[16, 48], (*lit(BLUE), 255))
    assert near(px[48, 48], (*lit(YELLOW), 255))
    assert px[0, 0][3] == 0  # the margin stays transparent
    centre = Image.new("RGBA", (3, 3), (90, 90, 90, 255))
    centre.putpixel((1, 1), (255, 0, 255, 255))
    for kw in ({}, {"filter": "bilinear"}, {"ss": 1}):
        img = R.render([part(quad(), R.Material(centre))], size=64, **kw)
        assert near(img.getpixel((32, 32)), (*lit((255, 0, 255)), 255), tol=10), kw  # bilinear: a little grey
    # one colour per triangle: the texel at its centre (UV 2/3, 2/3 and 1/3, 1/3 of a 2 x 2 texture)
    for kw in ({"mode": "face"}, {"raster": "painter"}):
        img = R.render([part(quad(), R.Material(quadrants(Image, 2)))], size=64, **kw)
        assert near(img.getpixel((48, 48)), (*lit(YELLOW), 255)), kw
        assert near(img.getpixel((16, 16)), (*lit(RED), 255)), kw


def test_flat_colour_and_size(Image):
    img = R.render([part(quad(), R.Material(None, (200, 100, 50)), uv=None)], size=(48, 32), ss=3)
    assert img.size == (48, 32)
    assert near(img.getpixel((24, 16)), (*lit((200, 100, 50)), 255))
    bad = R.render([part(quad(), R.Material(None, (NAN, 999, -5)), uv=None)], size=16)  # broken colour
    assert bad.getpixel((8, 8))[3] == 255


def test_bilinear_blends_neighbouring_texels(Image):
    tex = Image.new("RGBA", (2, 1), (0, 0, 0, 255))
    tex.putpixel((1, 0), (255, 255, 255, 255))
    pos, idx, nrm, _ = quad()
    uv = [0.25, 0.0, 0.75, 0.0, 0.75, 1.0, 0.25, 1.0]  # texel centres at the left and right edges
    near_img = R.render([part(quad(), R.Material(tex), uv=uv)], size=32)
    bil_img = R.render([part(quad(), R.Material(tex), uv=uv)], size=32, filter="bilinear")
    row = [near_img.getpixel((x, 16))[0] for x in range(4, 28)]
    assert set(row) <= {0, 254}
    row = [bil_img.getpixel((x, 16))[0] for x in range(4, 28)]
    assert any(60 < v < 200 for v in row) and row == sorted(row)


def test_back_faces_are_culled_unless_two_sided(Image):
    one = R.Material(None, (200, 100, 50))
    two = R.Material(None, (200, 100, 50), two_sided=True)
    assert alpha_box(R.render([part(quad(back=True), one)], size=32)) is None
    for kw, material in (({}, two), ({"cull": False}, one)):
        img = R.render([part(quad(back=True), material)], size=32, **kw)
        assert near(img.getpixel((16, 16)), (*lit((200, 100, 50)), 255)), kw  # lit from its own side
    # per material: the one-sided left half is culled, the two-sided right half is drawn
    left = part(quad(-1.0, -1.0, 0.0, 1.0, back=True), one)
    right = part(quad(0.0, -1.0, 1.0, 1.0, back=True), two)
    img = R.render([left, right], size=32)
    assert img.getpixel((8, 16))[3] == 0 and img.getpixel((24, 16))[3] == 255
    painter = R.render([left, right], size=32, raster="painter")
    assert painter.getpixel((8, 16))[3] == 0 and painter.getpixel((24, 16))[3] == 255


def test_closed_box_looks_the_same_with_and_without_culling(Image):
    parts = [part(box_mesh(), R.Material(None, (180, 180, 180)))]
    culled = R.render(parts, size=48, yaw=-30, pitch=20)
    both = R.render(parts, size=48, yaw=-30, pitch=20, cull=False)
    assert culled.getchannel("A").tobytes() == both.getchannel("A").tobytes()
    wrong = R.render(parts, size=48, yaw=-30, pitch=20, front_ccw=False)  # inside out: the far faces show
    assert wrong.tobytes() != culled.tobytes()


def test_alpha_cut_shows_what_is_behind(Image):
    tex = Image.new("RGBA", (8, 8), (*RED, 0))
    tex.paste((*GREEN, 255), (4, 0, 8, 8))  # left half transparent, right half opaque
    front = part(quad(), R.Material(tex, alpha_cut=0.5))
    behind = part(quad(z=-1.0), R.Material(None, BLUE), uv=None)
    img = R.render([front, behind], size=64)
    assert near(img.getpixel((16, 32)), (*lit(BLUE), 255))
    assert near(img.getpixel((48, 32)), (*lit(GREEN), 255))
    alone = R.render([front], size=64)
    assert alone.getpixel((16, 32))[3] == 0 and alone.getpixel((48, 32))[3] == 255
    opaque = R.render([part(quad(), R.Material(tex, alpha_cut=NAN))], size=64)  # broken threshold: no cut
    assert opaque.getpixel((16, 32))[3] == 255
    assert alpha_box(R.render([front, behind], size=64, raster="painter")) is not None


def test_fit_gives_before_and_after_one_scale_and_centre(Image):
    big = part(quad())
    small = part(quad(-0.5, -0.5, 0.5, 0.5))
    fit = R.bounds([big, small])
    s1, s2, s3 = {}, {}, {}
    a = R.render([big], size=64, fit=fit, stats=s1)
    b = R.render([small], size=64, fit=fit, stats=s2)
    alone = R.render([small], size=64, stats=s3)
    assert s1["scale"] == s2["scale"]
    assert s3["scale"] == pytest.approx(2 * s1["scale"])
    ax0, ay0, ax1, ay1 = alpha_box(a)
    bx0, by0, bx1, by1 = alpha_box(b)
    assert abs((bx1 - bx0) * 2 - (ax1 - ax0)) <= 2 and abs((by1 - by0) * 2 - (ay1 - ay0)) <= 2
    assert abs((ax0 + ax1) - (bx0 + bx1)) <= 1 and abs((ay0 + ay1) - (by0 + by1)) <= 1
    assert alpha_box(alone) == alpha_box(a)  # on its own it fills the picture
    # moved to the right by half its width: the shared frame shows it moved
    moved = part(quad(0.0, -0.5, 1.0, 0.5))
    fit = R.bounds([small, moved])
    c = alpha_box(R.render([small], size=64, fit=fit))
    d = alpha_box(R.render([moved], size=64, fit=fit))
    assert d[0] > c[0] + 10 and d[1] == c[1] and d[3] == c[3]
    # a broken fit is ignored
    assert R.render([small], size=64, fit=((NAN, 0, 0), (1, 1, 1))).tobytes() == alone.tobytes()
    assert R.render([small], size=64, fit=((1, 1, 1), (0, 0, 0))).tobytes() == alone.tobytes()


def test_camera_pair_shares_matrix_and_scale(Image):
    before = [part(box_mesh(2.0, 0.1, 0.02), R.Material(None, (150, 150, 150)))]
    after = [part(box_mesh(2.6, 0.12, 0.02, dx=0.3), R.Material(None, (200, 60, 60)))]
    m = R.camera(before + after)
    fit = R.bounds(before + after)
    s1, s2 = {}, {}
    R.render(before, size=64, matrix=m, fit=fit, stats=s1)
    R.render(after, size=64, matrix=m, fit=fit, stats=s2)
    assert s1["matrix"] == s2["matrix"] == m and s1["scale"] == s2["scale"]
    # camera() and auto_matrix + auto_roll draw the same picture of one model
    one = R.render(after, size=64, matrix=R.camera(after))
    two = R.render(after, size=64, matrix=R.auto_matrix(after), auto_roll=True)
    assert one.tobytes() == two.tobytes()
    flat = R.render(after, size=64, matrix=R.auto_matrix(after))
    assert covered(one) > covered(flat)  # the roll makes the blade bigger


def test_broken_input_never_raises(Image):
    pos, idx, nrm, uv = quad()
    tex = quadrants(Image)
    full = R.render([part(quad(), R.Material(tex))], size=32)
    cases = [
        [],
        [R.Part([], [])],
        [R.Part(None, None, None, None, None)],
        [part(quad(), R.Material(tex), pos=[NAN] * 3 + pos[3:])],
        [part(quad(), R.Material(tex), pos=pos[:3] + [INF, -INF, INF] + pos[6:])],
        [part(quad(), R.Material(tex), uv=[NAN] * 8)],
        [part(quad(), R.Material(tex), uv=[INF, -INF] * 4)],
        [part(quad(), R.Material(tex), uv=[1e300, -1e300] * 4)],
        [part(quad(), R.Material(tex), uv=[-5000.3] * 8)],
        [part(quad(), R.Material(tex), uv=uv[:3])],
        [part(quad(), R.Material(tex), nrm=[NAN] * 12)],
        [part(quad(), R.Material(tex), nrm=[INF, -INF, INF] * 4)],
        [part(quad(), R.Material(tex), nrm=[0.0] * 12)],
        [part(quad(), R.Material(tex), idx=idx + [9, -1, 3, 1])],
        [part(quad(), R.Material(Image.new("RGBA", (0, 0))))],
        [part(quad(), R.Material(Image.new("L", (4, 4), 99)))],
        [part(quad(), R.Material(tex, alpha_cut=0.5), uv=[NAN] * 8)],
    ]
    for parts in cases:
        for kw in ({}, {"filter": "bilinear"}, {"mode": "face"}, {"raster": "painter"}, {"auto_roll": True},
                   {"matrix": [NAN] * 9}, {"fit": ((0, 0, 0), (0, 0, 0))}):
            img = R.render(parts, size=32, **kw)
            assert img.size == (32, 32) and img.mode == "RGBA"
    for parts in ([], [R.Part([], [])], [R.Part([7.0, 7.0, 7.0] * 4, idx)]):
        img = R.render(parts, size=24)
        assert img.size == (24, 24) and alpha_box(img) is None  # transparent
    # an unused far vertex and a hidden group do not change the picture
    stray = part(quad(), R.Material(tex), pos=pos + [1e9, -1e9, 1e9], nrm=nrm + [0, 0, 1], uv=uv + [0, 0])
    hidden = R.Part([50.0, 50.0, 50.0] * 4, idx, None, uv, R.Material(tex))
    assert R.render([stray], size=32).tobytes() == full.tobytes()
    assert R.render([part(quad(), R.Material(tex)), hidden], size=32).tobytes() == full.tobytes()


def test_light_out_of_range_is_clamped(Image):
    # a light above 1 overflows the per-pixel bytes: the pixel pass is repeated with every value checked
    def glare(nx, ny, nz):
        return 3.0

    for kw in ({}, {"filter": "bilinear"}, {"mode": "face"}):
        img = R.render([part(quad(), R.Material(quadrants(Image)))], size=32, lighting=glare, **kw)
        assert near(img.getpixel((8, 8)), (*RED, 255), tol=1), kw
        img = R.render([part(quad(), R.Material(None, (200, 100, 50)), uv=None)], size=32, lighting=glare, **kw)
        assert near(img.getpixel((16, 16)), (200, 100, 50, 255), tol=1), kw


def test_same_input_same_bytes(Image):
    def scene():
        tex = quadrants(Image, 16)
        cut = quadrants(Image, 8, alpha=0)
        cut.paste((255, 255, 255, 255), (0, 0, 4, 8))
        return [part(box_mesh(), R.Material(tex)),
                part(box_mesh(0.6, 1.4, 0.6, dx=0.5), R.Material(cut, alpha_cut=0.5, two_sided=True)),
                part(quad(-2.0, -2.0, 2.0, 2.0, z=-1.5), R.Material(None, (90, 120, 30)), nrm=None)]

    for kw in ({}, {"filter": "bilinear"}, {"mode": "face"}, {"ss": 3}):
        digests = {hashlib.sha256(R.render(scene(), size=40, yaw=-30, pitch=20, **kw).tobytes()).hexdigest()
                   for _ in range(2)}
        assert len(digests) == 1, kw
    img = R.render(scene(), size=40, yaw=-30, pitch=20)
    assert img.getchannel("A").getextrema() == (0, 255)


def test_painter_for_very_large_meshes(Image, monkeypatch):
    parts = [part(box_mesh(), R.Material(quadrants(Image)))]
    z, p = {}, {}
    zimg = R.render(parts, size=48, yaw=-30, pitch=20, stats=z)
    assert z["raster_used"] == "zbuffer"
    monkeypatch.setattr(R, "PAINTER_TRIANGLES", 4)
    pimg = R.render(parts, size=48, yaw=-30, pitch=20, stats=p)
    assert p["raster_used"] == "painter" and p["tris"] == 12
    zb, pb = alpha_box(zimg), alpha_box(pimg)
    assert all(abs(u - v) <= 1 for u, v in zip(zb, pb))  # the same outline
    assert pimg.getpixel((24, 24))[3] == 255


def test_stats_and_unknown_arguments(Image):
    st = {}
    R.render([part(quad())], size=16, stats=st)
    assert st["tris"] == 2 and st["visible"] == 2 and st["total"] >= 0
    with pytest.raises(ValueError):
        R.render([part(quad())], size=16, mode="vertex")
    with pytest.raises(ValueError):
        R.render([part(quad())], size=16, raster="opengl")


def test_render_needs_pillow(monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(EquipError):
        R.render([part(quad())], size=16)


def rod(length: float):
    """A thin box turned 45 degrees about Y: its model-space box is far bigger than the rod."""
    pos, idx, nrm, uv = box_mesh(0.05, 0.05, length)
    c = s = 0.5 ** 0.5
    for k in range(0, len(pos), 3):
        x, z = pos[k], pos[k + 2]
        pos[k], pos[k + 2] = c * x - s * z, s * x + c * z
    return pos, idx, nrm, uv


def test_view_bounds_frame_a_pair_as_tightly_as_one_model(Image):
    long, short = [part(rod(3.0))], [part(rod(1.5))]
    m = R.camera(long + short, "auto")
    assert R.view_bounds([], m) is None and R.view_bounds(long, None) is None
    alone, pair, loose = {}, {}, {}
    R.render(long, 64, matrix=m, stats=alone)
    R.render(long, 64, matrix=m, fit=R.view_bounds(long + short, m), stats=pair)
    R.render(long, 64, matrix=m, fit=R.bounds(long + short), stats=loose)
    assert pair["scale"] == pytest.approx(alone["scale"]) and loose["scale"] < 0.9 * alone["scale"]
    a = R.render(short, 64, matrix=m, fit=R.view_bounds(long + short, m), stats=pair)
    assert pair["scale"] == pytest.approx(alone["scale"]) and a.getchannel("A").getbbox()
