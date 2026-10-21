"""A small 3D picture of an equipment piece: pure Python and Pillow (no numpy, OpenGL or native code).

Written for this project from textbook algorithms only (edge functions, z-buffer, painter's algorithm,
Lambert / Gouraud shading).  A picture is made in three steps:

1. ``Scene``: the vertices in screen space (orthographic), the model fitted into the picture - or into a
   given model-space box, so that a "before" and an "after" picture share one scale and centre.  Broken
   triangles (a coordinate that is not finite or absurdly far, an index outside the vertex list) are left
   out, triangles without area too (mods hide a group by collapsing it to one point), and only vertices
   of the triangles drawn count for the framing.
2. raster: which triangle is seen in each pixel.  ``raster_zbuffer`` is an exact z-buffer that also cuts
   alpha-tested materials; above PAINTER_TRIANGLES ``render`` uses ``raster_painter`` (the painter's
   algorithm with Pillow's polygon fill: faster, but close or crossing layers can come out wrong).
3. ``resolve``: colour the visible pixels - 'pixel' samples the texture in every pixel (nearest or
   bilinear), 'face' gives each triangle one colour.  The picture is drawn at ``ss`` times the size and
   reduced, which smooths the edges.

Mesh data are flat sequences (lists or array.array): pos [x, y, z, ...], idx [a, b, c, ...],
nrm [nx, ny, nz, ...] or None, uv [u, v, ...] or None.  Front faces are counter-clockwise seen from
outside in a right-handed, +Y-up space; UV (0, 0) is the top-left texel (RE Engine, no V flip).  With
yaw = pitch = 0 the camera looks down -Z, so the model's +Z side faces the viewer.  The z-buffer path
gives the same bytes for the same input.  Pillow is imported only by the functions that draw.
"""

from __future__ import annotations

import math
import time
from array import array
from itertools import compress, groupby

from . import EquipError

__all__ = ["Texture", "Material", "Part", "Lighting", "Scene", "rotation", "auto_matrix", "best_roll",
           "bounds", "union_bounds", "view_bounds", "camera", "view_for", "vertex_normals", "raster_zbuffer",
           "raster_painter", "resolve", "render"]

PAINTER_TRIANGLES = 400_000  # render() draws more triangles than this with the painter and one colour each
MAX_TRIANGLES = 1_500_000  # callers do not render more than this (well over a second even with the painter)
FRONT_VIEW = (-20.0, 10.0)  # yaw, pitch for armor and palico gear: their +Z front, a little from the left, above
AUTO_VIEW = (-25.0, 15.0)  # yaw, pitch after auto_matrix: weapons and the rest
LIGHT = (-0.45, 0.55, 0.70)  # direction towards the key light in view space: upper left, in front

INF = float("inf")
_LIMIT = 1e5  # a coordinate this far (100 km) from the origin is broken data
_UV_LIMIT = 65536.0  # beyond any half-float UV
_OFF = 4096.0  # added to UVs so that texel coordinates stay positive and int() == floor()
_TINY = 1e-3  # triangles with less screen area (in supersampled pixels) get one value instead of a gradient
_ROLL_POINTS = 3000  # vertices best_roll looks at
_MASK = bytes([0]) + bytes([255]) * 255  # 0 -> 0, anything else -> 255


def _pil():
    try:
        from PIL import Image
    except ImportError:
        raise EquipError("3D 미리보기를 그리려면 Pillow가 필요합니다") from None
    return Image


# ---- data -----------------------------------------------------------------------------
class Texture:
    """A decoded picture prepared for sampling (repeat wrap); made once and kept on its Material."""

    __slots__ = ("img", "w", "h", "rgba", "_padded")

    def __init__(self, img):
        im = img if img.mode == "RGBA" else img.convert("RGBA")
        self.img = im
        self.w, self.h = im.size
        self.rgba = im.tobytes()
        self._padded: dict = {}

    def sample(self, u: float, v: float) -> tuple[int, int, int, int]:
        """The nearest texel (r, g, b, a)."""
        w, h = self.w, self.h
        x = int((u % 1.0) * w) % w
        y = int((v % 1.0) * h) % h
        k = (y * w + x) * 4
        d = self.rgba
        return d[k], d[k + 1], d[k + 2], d[k + 3]

    def padded(self, stride: int) -> tuple[bytes, bytes, bytes, bytes]:
        """(R, G, B, A) bytes in rows of ``stride`` (> w) texels plus one more row.  Column w and row h
        repeat column 0 and row 0, so the bilinear neighbours of texel k are k + 1, k + stride and
        k + stride + 1 without any modulo."""
        hit = self._padded.get(stride)
        if hit is None:
            Image = _pil()
            w, h, src = self.w, self.h, self.img
            im = Image.new("RGBA", (stride, h + 1), (0, 0, 0, 0))
            im.paste(src, (0, 0))
            im.paste(src.crop((0, 0, 1, h)), (w, 0))
            im.paste(src.crop((0, 0, w, 1)), (0, h))
            im.paste(src.crop((0, 0, 1, 1)), (w, h))
            hit = self._padded[stride] = tuple(ch.tobytes() for ch in im.split())
        return hit


class Material:
    """How a part looks: a texture or a flat colour, alpha-tested (cut-out) or not, one- or two-sided."""

    __slots__ = ("texture", "color", "alpha_cut", "two_sided", "_tex")

    def __init__(self, texture=None, color=(190, 190, 190), alpha_cut=None, two_sided=False):
        self.texture = texture  # PIL image (any mode), Texture or None
        self.color = color  # (r, g, b) without a texture
        self.alpha_cut = alpha_cut  # None, or 0..1: texels with less alpha are holes
        self.two_sided = two_sided  # its back faces are drawn even when culling (cloth, capes, hair cards)
        self._tex = None

    def tex(self) -> Texture | None:
        """The texture as a Texture (made once per picture); None without a usable one."""
        t = self.texture
        if t is None:
            return None
        if not isinstance(t, Texture):
            if self._tex is None or self._tex[0] is not t:
                try:
                    self._tex = (t, Texture(t))
                except (OSError, ValueError, AttributeError):  # not a picture Pillow can turn into RGBA
                    self._tex = (t, None)
            t = self._tex[1]
        return t if t is not None and t.w and t.h else None


class Part:
    """One submesh with one material.  Treat pos and idx as read-only once it is drawn: what the renderer
    works out from them (the triangles it draws, the box) is kept until they are replaced."""

    __slots__ = ("pos", "idx", "nrm", "uv", "material", "_info")

    def __init__(self, pos, idx, nrm=None, uv=None, material=None):
        self.pos, self.idx, self.nrm, self.uv = pos, idx, nrm, uv
        self.material = material or Material()
        self._info = None


def _rgb(color) -> tuple[int, int, int]:
    try:
        r, g, b = (min(255, max(0, int(v))) for v in tuple(color)[:3])
    except (TypeError, ValueError, OverflowError):
        return 190, 190, 190
    return r, g, b


def _cut(material: Material) -> int | None:
    """The alpha (0..255) a texel needs to be drawn, or None when the material is not alpha-tested."""
    try:
        c = float(material.alpha_cut)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not c > 0.0:  # also NaN
        return None
    return 255 if c >= 1.0 else int(c * 255)


def _within(values, limit: float) -> bool:
    """Are all values finite and inside +-limit?  (the sum catches NaN and Inf, then min / max are exact)"""
    if not len(values):
        return True
    return math.isfinite(sum(values)) and -limit < min(values) and max(values) < limit


def _clean(p: Part):
    """(vertex count, a, b, c, used) of a part: the vertex ids of the triangles it draws and the vertices
    those use (None = all of them).  Left out are broken triangles (a coordinate that is not finite or
    beyond _LIMIT, an index outside the vertex list) and those without area (a group a mod hides by
    collapsing it to one point, repeated indices): they draw nothing and must not frame the picture."""
    P = p.pos if p.pos is not None else ()
    idx = p.idx if p.idx is not None else ()
    n = len(P) // 3
    m = len(idx) - len(idx) % 3
    if not n or not m:
        return n, [], [], [], None
    if m != len(idx):
        idx = idx[:m]
    if len(P) != 3 * n:
        P = P[:3 * n]
    X, Y, Z = P[0::3], P[1::3], P[2::3]
    a, b, c = idx[0::3], idx[1::3], idx[2::3]
    whole = min(idx) >= 0 and max(idx) < n and _within(P, _LIMIT)
    if not whole:
        lim = _LIMIT
        ok = [-lim < x < lim and -lim < y < lim and -lim < z < lim for x, y, z in zip(X, Y, Z)]
        keep = [0 <= i < n and 0 <= j < n and 0 <= k < n and ok[i] and ok[j] and ok[k] for i, j, k in zip(a, b, c)]
        a, b, c = list(compress(a, keep)), list(compress(b, keep)), list(compress(c, keep))
    # twice the area as a cross product; any non-zero component will do (exactly zero = no area)
    keep = [(Y[j] - Y[i]) * (Z[k] - Z[i]) != (Z[j] - Z[i]) * (Y[k] - Y[i])
            or (Z[j] - Z[i]) * (X[k] - X[i]) != (X[j] - X[i]) * (Z[k] - Z[i])
            or (X[j] - X[i]) * (Y[k] - Y[i]) != (Y[j] - Y[i]) * (X[k] - X[i]) for i, j, k in zip(a, b, c)]
    if not all(keep):
        whole = False
        a, b, c = list(compress(a, keep)), list(compress(b, keep)), list(compress(c, keep))
    if whole:
        used = set(idx)
    else:
        used = set(a)
        used.update(b)
        used.update(c)
    return n, a, b, c, (None if len(used) == n else used)


def _box(P, n: int, used):
    xs, ys, zs = P[0:3 * n:3], P[1:3 * n:3], P[2:3 * n:3]
    if used is not None:
        xs = [xs[i] for i in used]
        ys = [ys[i] for i in used]
        zs = [zs[i] for i in used]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _prepare(parts) -> list:
    """[(part, n, a, b, c, used, lo, hi)] of the parts with something to draw (see _clean); lo, hi is the
    model-space box of the vertices used.  Kept on each part while its pos and idx stay the same objects."""
    out = []
    for p in parts:
        pos, idx = p.pos, p.idx
        sizes = (0 if pos is None else len(pos), 0 if idx is None else len(idx))
        info = p._info
        if info is None or info[0] is not pos or info[1] is not idx or info[2] != sizes:
            n, a, b, c, used = _clean(p)
            box = _box(pos, n, used) if len(a) else None
            info = p._info = (pos, idx, sizes, (n, a, b, c, used, box))
        n, a, b, c, used, box = info[3]
        if box is not None:
            out.append((p, n, a, b, c, used, box[0], box[1]))
    return out


def vertex_normals(pos, idx) -> list[float]:
    """Area-weighted vertex normals for meshes that come without any."""
    return _vertex_normals(pos, len(pos) // 3, idx[0::3], idx[1::3], idx[2::3])


def _vertex_normals(pos, n, A, B, C) -> list[float]:
    NX = [0.0] * n
    NY = [0.0] * n
    NZ = [0.0] * n
    for a, b, c in zip(A, B, C):
        ax = pos[3 * a]; ay = pos[3 * a + 1]; az = pos[3 * a + 2]
        ux = pos[3 * b] - ax; uy = pos[3 * b + 1] - ay; uz = pos[3 * b + 2] - az
        vx = pos[3 * c] - ax; vy = pos[3 * c + 1] - ay; vz = pos[3 * c + 2] - az
        fx = uy * vz - uz * vy; fy = uz * vx - ux * vz; fz = ux * vy - uy * vx
        for k in (a, b, c):
            NX[k] += fx; NY[k] += fy; NZ[k] += fz
    out = [0.0] * (3 * n)
    for k in range(n):
        x = NX[k]; y = NY[k]; z = NZ[k]
        ln = math.sqrt(x * x + y * y + z * z) or 1.0
        out[3 * k] = x / ln; out[3 * k + 1] = y / ln; out[3 * k + 2] = z / ln
    return out


# ---- camera ---------------------------------------------------------------------------
def rotation(yaw: float = 0.0, pitch: float = 0.0, roll: float = 0.0) -> tuple:
    """Row-major 3x3 model -> view.  yaw turns the model about +Y (degrees), pitch then tilts it towards the
    camera (positive = seen from above), roll finally turns the picture counter-clockwise."""
    cy, sy = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    cp, sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
    R = (cy, 0.0, sy,
         sp * sy, cp, -sp * cy,
         -cp * sy, sp, cp * cy)
    return _roll(R, roll) if roll else R


def _roll(R, deg: float) -> tuple:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (c * R[0] - s * R[3], c * R[1] - s * R[4], c * R[2] - s * R[5],
            s * R[0] + c * R[3], s * R[1] + c * R[4], s * R[2] + c * R[5],
            R[6], R[7], R[8])


def _matmul(P, Q) -> tuple:
    return tuple(sum(P[3 * r + k] * Q[3 * k + c] for k in range(3)) for r in range(3) for c in range(3))


def union_bounds(*boxes):
    """The box around the given boxes (None ones are skipped); None when there is none."""
    lo = hi = None
    for box in boxes:
        if box is None:
            continue
        if lo is None:
            lo, hi = tuple(box[0]), tuple(box[1])
        else:
            lo = tuple(map(min, lo, box[0]))
            hi = tuple(map(max, hi, box[1]))
    return None if lo is None else (lo, hi)


def _prep_box(prep):
    return union_bounds(*((lo, hi) for *_, lo, hi in prep))


def bounds(parts):
    """((min x, y, z), (max x, y, z)) of the vertices that drawn triangles use, or None when nothing would be
    drawn.  bounds(before + after) is the ``fit`` that gives two pictures one scale and centre."""
    return _prep_box(_prepare(parts))


def view_bounds(parts, matrix):
    """((x0, y0), (x1, y1)) of the vertices that drawn triangles use, seen through ``matrix`` (camera()), or
    None when nothing would be drawn.  As ``fit`` with the same matrix it frames a before / after pair as
    tightly as each model alone would be (a model-space box frames its 8 corners: loose for a long model
    turned onto the diagonal)."""
    R = _matrix(matrix)
    if R is None:
        return None
    r00, r01, r02, r10, r11, r12 = R[:6]
    x0 = y0 = INF
    x1 = y1 = -INF
    for p, n, _a, _b, _c, used, _lo, _hi in _prepare(parts):
        P = p.pos
        xs, ys, zs = P[0:3 * n:3], P[1:3 * n:3], P[2:3 * n:3]
        if used is not None:
            xs, ys, zs = [xs[i] for i in used], [ys[i] for i in used], [zs[i] for i in used]
        vx = [r00 * x + r01 * y + r02 * z for x, y, z in zip(xs, ys, zs)]
        vy = [r10 * x + r11 * y + r12 * z for x, y, z in zip(xs, ys, zs)]
        x0 = min(x0, min(vx)); x1 = max(x1, max(vx))
        y0 = min(y0, min(vy)); y1 = max(y1, max(vy))
    return None if x0 == INF else ((x0, y0), (x1, y1))


_ALIGN = {0: (0.0, 0.0, -1.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0),  # +X -> viewer
          1: (1.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 1.0, 0.0),  # +Y -> viewer (seen from above)
          2: (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)}  # +Z -> viewer


def _auto_matrix(box, yaw, pitch, front_axis):
    if front_axis is None:
        if box is None:
            front_axis = 2
        else:
            lo, hi = box
            ext = [hi[k] - lo[k] for k in range(3)]
            front_axis = min(range(3), key=ext.__getitem__)
    return _matmul(rotation(yaw, pitch), _ALIGN[front_axis])


def auto_matrix(parts, yaw: float = AUTO_VIEW[0], pitch: float = AUTO_VIEW[1], front_axis: int | None = None):
    """Camera for models without a known front (weapons): look along the axis in which the model is thinnest
    (its broad side faces the viewer), then turn by yaw / pitch for a 3/4 view.  front_axis 0 / 1 / 2 forces
    the axis (2 = +Z, the usual character front)."""
    return _auto_matrix(bounds(parts) if front_axis is None else None, yaw, pitch, front_axis)


def best_roll(xs, ys, aspect: float = 1.0, gain: float = 1.15, step: int = 15) -> float:
    """In-plane turn (degrees) that lets a long, thin model fill the picture (usually +-45 = the diagonal);
    0 unless it makes the model at least ``gain`` times bigger.  Looks at about 3000 of the points."""
    k = max(1, len(xs) // _ROLL_POINTS)
    xs = xs[::k]
    ys = ys[::k]
    if not len(xs):
        return 0.0

    def fit(deg):
        c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        X = [c * x - s * y for x, y in zip(xs, ys)]
        Y = [s * x + c * y for x, y in zip(xs, ys)]
        return min(aspect / max(max(X) - min(X), 1e-12), 1.0 / max(max(Y) - min(Y), 1e-12))

    base = fit(0)
    cands = sorted(range(-90 + step, 91, step), key=lambda d: (abs(d), -d))  # small turns first, + before -
    scores = [(fit(d), d) for d in cands if d]
    top = max(s for s, _ in scores)
    if top < base * gain:
        return 0.0
    return float(next(d for s, d in scores if s >= top * 0.98))


def _auto_roll(prep, R, aspect: float) -> float:
    """best_roll for matrix R over an even sample of the vertices that frame the picture."""
    total = sum(n if used is None else len(used) for _, n, _, _, _, used, _, _ in prep)
    if not total:
        return 0.0
    k = -(-total // _ROLL_POINTS)
    r00, r01, r02, r10, r11, r12 = R[:6]
    xs, ys = [], []
    skip = 0
    for p, n, _a, _b, _c, used, _lo, _hi in prep:
        ids = range(n) if used is None else list(used)
        P = p.pos
        for i in ids[skip::k]:
            x = P[3 * i]; y = P[3 * i + 1]; z = P[3 * i + 2]
            xs.append(r00 * x + r01 * y + r02 * z)
            ys.append(r10 * x + r11 * y + r12 * z)
        skip = (skip - len(ids)) % k
    return best_roll(xs, ys, aspect)


def camera(parts, view: str = "auto", aspect: float = 1.0) -> tuple:
    """Camera matrix (row-major 3x3, any roll included) for render(matrix=...).

    'front': armor and palico gear - the model's +Z side faces the viewer, turned by FRONT_VIEW, no roll.
    'auto': weapons and the rest - auto_matrix (broad side to the viewer, AUTO_VIEW turn) and the roll that
    puts a long model on the diagonal.  For a before / after pair pass both (before + after) here and use
    bounds(before + after) as ``fit`` for both pictures."""
    if view == "front":
        return rotation(*FRONT_VIEW)
    if view != "auto":
        raise ValueError(f"unknown view {view!r}")
    prep = _prepare(parts)
    R = _auto_matrix(_prep_box(prep), AUTO_VIEW[0], AUTO_VIEW[1], None)
    deg = _auto_roll(prep, R, aspect)
    return _roll(R, deg) if deg else R


def view_for(kind: str, part: str = "") -> str:
    """camera() view of a Target kind / part: 'front' for armor and palico gear, else 'auto'."""
    return "front" if kind == "armor" or (kind == "palico" and part != "weapon") else "auto"


def _matrix(m):
    """m as a tuple of 9 finite floats, or None."""
    try:
        R = tuple(float(v) for v in m)
    except (TypeError, ValueError):
        return None
    return R if len(R) == 9 and all(-INF < v < INF for v in R) else None


def _valid_box(box, size: int = 3):
    try:
        lo, hi = tuple(map(float, box[0])), tuple(map(float, box[1]))
    except (TypeError, ValueError, IndexError):
        return None
    if len(lo) != size or len(hi) != size or not all(-INF < v < INF for v in lo + hi):
        return None
    if not all(lo[k] <= hi[k] for k in range(size)):
        return None
    return lo, hi


class Scene:
    """The parts in screen space with one numbering of vertices and of triangles (triangle ids start at 1).

    Only parts with something to draw are kept (``parts``; ``part_tris`` are their (first, last) triangle ids).
    ``fit`` is a model-space box ((x0, y0, z0), (x1, y1, z1)), whose 8 corners the picture frames instead of the
    model, or a view-space one ((x0, y0), (x1, y1)) from view_bounds(): pictures with the same fit and matrix
    share scale and centre."""

    def __init__(self, parts, width: int, height: int, matrix=None, yaw: float = 0.0, pitch: float = 0.0,
                 roll: float = 0.0, auto_roll: bool = False, fit=None, margin: float = 0.04,
                 front_ccw: bool = True):
        prep = _prepare(parts)
        self.W, self.H = width, height
        self.front_ccw = front_ccw
        R = _matrix(matrix) if matrix is not None else None
        self.roll = 0.0 if R is not None else roll
        if R is None:
            R = _matrix(rotation(yaw, pitch, roll)) or rotation()
        if auto_roll and prep:
            deg = _auto_roll(prep, R, width / height)
            if deg:
                R = _roll(R, deg)
                self.roll += deg
        self.R = R
        r00, r01, r02, r10, r11, r12, r20, r21, r22 = R
        VX, VY, VZ = [], [], []
        A, B, C, TP = [], [], [], []
        self.parts, self.counts, self.part_tris, self._prep = [], [], [], prep
        x0 = y0 = INF
        x1 = y1 = -INF
        base = 0
        for p, n, a, b, c, used, _lo, _hi in prep:
            P = p.pos
            xs, ys, zs = P[0:3 * n:3], P[1:3 * n:3], P[2:3 * n:3]
            vx = [r00 * x + r01 * y + r02 * z for x, y, z in zip(xs, ys, zs)]
            vy = [r10 * x + r11 * y + r12 * z for x, y, z in zip(xs, ys, zs)]
            vz = [r20 * x + r21 * y + r22 * z for x, y, z in zip(xs, ys, zs)]
            fx, fy = (vx, vy) if used is None else ([vx[i] for i in used], [vy[i] for i in used])
            x0 = min(x0, min(fx)); x1 = max(x1, max(fx))
            y0 = min(y0, min(fy)); y1 = max(y1, max(fy))
            if base:
                a = [i + base for i in a]
                b = [i + base for i in b]
                c = [i + base for i in c]
            if not front_ccw:
                b, c = c, b
            # screen y grows downwards, so a counter-clockwise front face is clockwise on screen: store (a, c, b)
            first = len(A) + 1
            A += a; B += c; C += b
            TP += [len(self.parts)] * len(a)
            self.part_tris.append((first, len(A)))
            self.parts.append(p)
            self.counts.append(n)
            VX += vx; VY += vy; VZ += vz
            base += n
        self.A, self.B, self.C, self.TP = A, B, C, TP
        self.VX, self.VY, self.VZ = VX, VY, VZ
        self.has_uv = [p.uv is not None and len(p.uv) >= 2 * n for p, n in zip(self.parts, self.counts)]
        self.visible = 0
        self._uv = None
        box = _valid_box(fit) if fit is not None else None
        if box is not None:
            (bx0, by0, bz0), (bx1, by1, bz1) = box
            corners = [(x, y, z) for x in (bx0, bx1) for y in (by0, by1) for z in (bz0, bz1)]
            cx = [r00 * x + r01 * y + r02 * z for x, y, z in corners]
            cy = [r10 * x + r11 * y + r12 * z for x, y, z in corners]
            x0, x1, y0, y1 = min(cx), max(cx), min(cy), max(cy)
        elif fit is not None:
            rect = _valid_box(fit, 2)
            if rect is not None:
                (x0, y0), (x1, y1) = rect
        if not VX:
            self.SX, self.SY, self.SQ = [], [], []
            self.scale = 1.0
            return
        s = min(width * (1 - 2 * margin) / max(x1 - x0, 1e-9), height * (1 - 2 * margin) / max(y1 - y0, 1e-9))
        ox = width * 0.5 - (x0 + x1) * 0.5 * s - 0.5  # pixel centres on integer coordinates
        oy = height * 0.5 + (y0 + y1) * 0.5 * s - 0.5
        self.SX = [ox + x * s for x in VX]
        self.SY = [oy - y * s for y in VY]
        self.SQ = VZ  # view z: nearer = larger
        self.scale = s

    def uv_lists(self) -> tuple[list, list]:
        """U and V of every vertex (0 for parts without UVs; broken values become 0)."""
        if self._uv is None:
            U, V = [], []
            for p, n, has in zip(self.parts, self.counts, self.has_uv):
                if has:
                    us, vs = p.uv[0:2 * n:2], p.uv[1:2 * n:2]
                    lim = _UV_LIMIT
                    if not _within(us, lim):
                        us = [u if -lim < u < lim else 0.0 for u in us]
                    if not _within(vs, lim):
                        vs = [v if -lim < v < lim else 0.0 for v in vs]
                    U += us; V += vs
                else:
                    U += [0.0] * n; V += [0.0] * n
            self._uv = U, V
        return self._uv

    def normal_lists(self) -> tuple[list, list, list]:
        """View-space vertex normals (from the geometry for parts that come without any)."""
        r00, r01, r02, r10, r11, r12, r20, r21, r22 = self.R
        NX, NY, NZ = [], [], []
        for p, n, a, b, c, _used, _lo, _hi in self._prep:
            nrm = p.nrm
            if nrm is not None and len(nrm) >= 3 * n:
                xs, ys, zs = nrm[0:3 * n:3], nrm[1:3 * n:3], nrm[2:3 * n:3]
            else:
                N = _vertex_normals(p.pos, n, a, b, c)
                if not self.front_ccw:
                    N = [-v for v in N]
                xs, ys, zs = N[0::3], N[1::3], N[2::3]
            NX += [r00 * x + r01 * y + r02 * z for x, y, z in zip(xs, ys, zs)]
            NY += [r10 * x + r11 * y + r12 * z for x, y, z in zip(xs, ys, zs)]
            NZ += [r20 * x + r21 * y + r22 * z for x, y, z in zip(xs, ys, zs)]
        return NX, NY, NZ


# ---- raster: z-buffer -----------------------------------------------------------------
def raster_zbuffer(scene: Scene, cull: bool = True) -> list[int]:
    """Exact z-buffer: the triangle id seen in every pixel (0 = background), W * H values.  Back faces are
    culled unless ``cull`` is off or the material is two-sided; alpha-tested materials leave holes."""
    W, H = scene.W, scene.H
    zb = [-INF] * (W * H)
    ib = [0] * (W * H)
    for pi, p in enumerate(scene.parts):
        first, last = scene.part_tris[pi]
        m = p.material
        do_cull = cull and not m.two_sided
        tris = zip(scene.A[first - 1:last], scene.B[first - 1:last], scene.C[first - 1:last])
        cut = _cut(m)
        tex = m.tex() if cut is not None and scene.has_uv[pi] else None
        if tex is not None:
            U, V = scene.uv_lists()
            _raster_cut(tris, scene, U, V, tex, cut, zb, ib, first - 1, do_cull)
        else:
            _raster(tris, scene.SX, scene.SY, scene.SQ, W, H, zb, ib, first - 1, do_cull)
    return ib


def _raster(tris, SX, SY, SQ, W, H, zb, ib, t, cull):
    """The hot loop.  Front faces have a positive screen area e1 x e2 (Scene already swapped the order)."""
    Wm1, Hm1 = W - 1, H - 1
    for a, b, c in tris:
        t += 1
        x0 = SX[a]; y0 = SY[a]; x1 = SX[b]; y1 = SY[b]; x2 = SX[c]; y2 = SY[c]
        e1x = x1 - x0; e1y = y1 - y0; e2x = x2 - x0; e2y = y2 - y0
        area = e1x * e2y - e2x * e1y
        if area <= 0.0:
            if cull or area == 0.0:
                continue
            x1, y1, x2, y2 = x2, y2, x1, y1  # a back face that is drawn: flip it
            e1x, e1y, e2x, e2y = e2x, e2y, e1x, e1y
            area = -area
            q0 = SQ[a]; q1 = SQ[c]; q2 = SQ[b]
        else:
            q0 = SQ[a]; q1 = SQ[b]; q2 = SQ[c]
        if x0 < x1:
            xmin = x0 if x0 < x2 else x2
            xmax = x1 if x1 > x2 else x2
        else:
            xmin = x1 if x1 < x2 else x2
            xmax = x0 if x0 > x2 else x2
        ix1 = int(xmax)
        if ix1 < xmin:  # no pixel-centre column inside (a sliver or a sub-pixel triangle)
            continue
        if y0 < y1:
            ymin = y0 if y0 < y2 else y2
            ymax = y1 if y1 > y2 else y2
        else:
            ymin = y1 if y1 < y2 else y2
            ymax = y0 if y0 > y2 else y2
        iy1 = int(ymax)
        if iy1 < ymin:
            continue
        ix0 = int(xmin)
        if ix0 < xmin:
            ix0 += 1
        iy0 = int(ymin)
        if iy0 < ymin:
            iy0 += 1
        if ix0 < 0:
            ix0 = 0
        if iy0 < 0:
            iy0 = 0
        if ix1 > Wm1:
            ix1 = Wm1
        if iy1 > Hm1:
            iy1 = Hm1
        inv = 1.0 / area
        dq1 = (q1 - q0) * inv
        dq2 = (q2 - q0) * inv
        qx = e2y * dq1 - e1y * dq2
        qy = e1x * dq2 - e2x * dq1
        if ix0 == ix1 and iy0 == iy1:
            # a single candidate pixel: the common case for dense meshes
            px = ix0 - x0
            py = iy0 - y0
            s = px * e2y - py * e2x
            if s < 0.0:
                continue
            r = e1x * py - e1y * px
            if r < 0.0 or s + r > area:
                continue
            q = q0 + px * qx + py * qy
            i = iy0 * W + ix0
            if q > zb[i]:
                zb[i] = q
                ib[i] = t
            continue
        if (ix1 - ix0 + 1) * (iy1 - iy0 + 1) <= 9:
            # a small box: test the pixel centres one by one
            for y in range(iy0, iy1 + 1):
                py = y - y0
                o = y * W
                sb = -py * e2x
                rb = e1x * py
                for x in range(ix0, ix1 + 1):
                    px = x - x0
                    s = px * e2y + sb
                    if s < 0.0:
                        continue
                    r = rb - e1y * px
                    if r < 0.0 or s + r > area:
                        continue
                    q = q0 + px * qx + py * qy
                    i = o + x
                    if q > zb[i]:
                        zb[i] = q
                        ib[i] = t
            continue
        # row by row: the exact x span from the three edge half-planes  k * (x - x0) + m >= 0
        kS = e2y
        kT = -e1y
        kU = e1y - e2y
        iS = 1.0 / kS if kS else 0.0
        iT = 1.0 / kT if kT else 0.0
        iU = 1.0 / kU if kU else 0.0
        dU = e2x - e1x
        o = iy0 * W
        for y in range(iy0, iy1 + 1):
            dy = y - y0
            lo = xmin
            hi = xmax
            m = -dy * e2x
            if kS > 0.0:
                v = x0 - m * iS
                if v > lo:
                    lo = v
            elif kS < 0.0:
                v = x0 - m * iS
                if v < hi:
                    hi = v
            elif m < 0.0:
                o += W
                continue
            m = e1x * dy
            if kT > 0.0:
                v = x0 - m * iT
                if v > lo:
                    lo = v
            elif kT < 0.0:
                v = x0 - m * iT
                if v < hi:
                    hi = v
            elif m < 0.0:
                o += W
                continue
            m = area + dy * dU
            if kU > 0.0:
                v = x0 - m * iU
                if v > lo:
                    lo = v
            elif kU < 0.0:
                v = x0 - m * iU
                if v < hi:
                    hi = v
            elif m < 0.0:
                o += W
                continue
            if lo > hi:  # empty row (this also keeps an infinite bound away from int())
                o += W
                continue
            xs = int(lo)
            if xs < lo:
                xs += 1
            xe = int(hi)
            if xs < ix0:
                xs = ix0
            if xe > ix1:
                xe = ix1
            if xs <= xe:
                q = q0 + (xs - x0) * qx + dy * qy
                for i in range(o + xs, o + xe + 1):
                    if q > zb[i]:
                        zb[i] = q
                        ib[i] = t
                    q += qx
            o += W
    return t


def _raster_cut(tris, scene, U, V, tex, acut, zb, ib, t, cull):
    """The alpha-tested variant (slower, only for cut-out materials): a pixel is taken only where the
    texel's alpha >= acut, so the holes show what is behind them."""
    SX, SY, SQ, W, H = scene.SX, scene.SY, scene.SQ, scene.W, scene.H
    tw, th, data = tex.w, tex.h, tex.rgba
    for a, b, c in tris:
        t += 1
        x0 = SX[a]; y0 = SY[a]
        e1x = SX[b] - x0; e1y = SY[b] - y0; e2x = SX[c] - x0; e2y = SY[c] - y0
        area = e1x * e2y - e2x * e1y
        if area <= 0.0:
            if cull or area == 0.0:
                continue
            b, c = c, b
            e1x, e1y, e2x, e2y = e2x, e2y, e1x, e1y
            area = -area
        x1 = SX[b]; y1 = SY[b]; x2 = SX[c]; y2 = SY[c]
        ix0 = max(0, math.ceil(min(x0, x1, x2)))
        ix1 = min(W - 1, int(max(x0, x1, x2)))
        iy0 = max(0, math.ceil(min(y0, y1, y2)))
        iy1 = min(H - 1, int(max(y0, y1, y2)))
        if ix0 > ix1 or iy0 > iy1:
            continue
        fu = ((U[a] + _OFF) * tw, (U[b] + _OFF) * tw, (U[c] + _OFF) * tw)
        fv = ((V[a] + _OFF) * th, (V[b] + _OFF) * th, (V[c] + _OFF) * th)
        if area < _TINY:
            q0, qx, qy = (SQ[a] + SQ[b] + SQ[c]) / 3.0, 0.0, 0.0
            u0, ux, uy = sum(fu) / 3.0, 0.0, 0.0
            v0, vx, vy = sum(fv) / 3.0, 0.0, 0.0
        else:
            inv = 1.0 / area

            def plane(f0, f1, f2):
                d1 = (f1 - f0) * inv
                d2 = (f2 - f0) * inv
                return f0, e2y * d1 - e1y * d2, e1x * d2 - e2x * d1

            q0, qx, qy = plane(SQ[a], SQ[b], SQ[c])
            u0, ux, uy = plane(*fu)
            v0, vx, vy = plane(*fv)
        for y in range(iy0, iy1 + 1):
            dy = y - y0
            o = y * W
            for x in range(ix0, ix1 + 1):
                dx = x - x0
                s = dx * e2y - dy * e2x
                r = e1x * dy - e1y * dx
                if s < 0.0 or r < 0.0 or s + r > area:
                    continue
                i = o + x
                q = q0 + dx * qx + dy * qy
                if q <= zb[i]:
                    continue
                iu = int(u0 + dx * ux + dy * uy) % tw
                iv = int(v0 + dx * vx + dy * vy) % th
                if data[(iv * tw + iu) * 4 + 3] < acut:
                    continue
                zb[i] = q
                ib[i] = t
    return t


# ---- raster: painter ------------------------------------------------------------------
def raster_painter(scene: Scene, cull: bool = True) -> list[int]:
    """Painter's algorithm with Pillow's polygon fill (in C): triangle ids drawn far to near into a 32-bit
    image.  Visibility is per triangle (sorted by depth), so close or crossing layers can come out wrong, and
    alpha-tested materials are cut per triangle later; render() uses it only for very large meshes."""
    Image = _pil()
    from PIL import ImageDraw

    W, H = scene.W, scene.H
    SX, SY, SQ = scene.SX, scene.SY, scene.SQ
    A, B, C = scene.A, scene.B, scene.C
    keys, ids = [], []
    for pi, p in enumerate(scene.parts):
        first, last = scene.part_tris[pi]
        do_cull = cull and not p.material.two_sided
        t = first - 1
        for a, b, c in zip(A[first - 1:last], B[first - 1:last], C[first - 1:last]):
            t += 1
            x0 = SX[a]
            y0 = SY[a]
            area = (SX[b] - x0) * (SY[c] - y0) - (SX[c] - x0) * (SY[b] - y0)
            if area <= 0.0 and (do_cull or area == 0.0):
                continue
            keys.append(SQ[a] + SQ[b] + SQ[c])
            ids.append(t)
    order = sorted(range(len(ids)), key=keys.__getitem__)  # far (small z) first
    lim = 16.0 * max(W, H)  # Pillow takes the corners as C ints: keep far-away ones in range
    if SX and (min(SX) < -lim or max(SX) > lim or min(SY) < -lim or max(SY) > lim):
        SX = [-lim if x < -lim else (lim if x > lim else x) for x in SX]
        SY = [-lim if y < -lim else (lim if y > lim else y) for y in SY]
    img = Image.new("I", (W, H), 0)
    dr = ImageDraw.Draw(img)
    try:  # the core call skips the ink conversion of the public polygon() for every triangle
        fill = dr.draw.draw_polygon
        fill(((0, 0), (0, 0), (0, 0)), 0, 1)
    except (AttributeError, TypeError):
        fill = None
    for k in order:
        t = ids[k]
        a = A[t - 1]
        b = B[t - 1]
        c = C[t - 1]
        xy = ((SX[a], SY[a]), (SX[b], SY[b]), (SX[c], SY[c]))
        if fill is not None:
            fill(xy, t, 1)
        else:
            dr.polygon(xy, fill=t)
    ib = array("i")
    ib.frombytes(img.tobytes())  # mode "I" = native int32
    return ib.tolist()


# ---- resolve --------------------------------------------------------------------------
class Lighting:
    """Ambient + a Lambert key light + a fill light from the camera, in view space."""

    def __init__(self, light=LIGHT, ambient: float = 0.30, diffuse: float = 0.72, fill: float = 0.20):
        n = math.sqrt(sum(v * v for v in light)) or 1.0
        self.L = tuple(v / n for v in light)
        self.ambient, self.diffuse, self.fill = ambient, diffuse, fill

    def __call__(self, nx: float, ny: float, nz: float) -> float:
        """0..1 for a normal facing the camera (a broken one gives the ambient light)."""
        lx, ly, lz = self.L
        d = nx * lx + ny * ly + nz * lz
        v = self.ambient + (self.diffuse * d if d > 0.0 else 0.0) + (self.fill * nz if nz > 0.0 else 0.0)
        if v >= 1.0:
            return 1.0
        return v if v > 0.0 else 0.0


def _flat_plane(f0, f1, f2):
    return (f0 + f1 + f2) / 3.0, 0.0, 0.0


def resolve(scene: Scene, ib: list[int], mode: str = "pixel", lighting: Lighting | None = None,
            filter: str = "nearest", exact: bool = True):
    """Colour the visible pixels: an RGBA image of the scene's size with a transparent background.

    mode 'pixel' (the texture in every pixel; filter 'nearest' or 'bilinear') or 'face' (one colour per
    triangle: the texel at its centre).  exact: ib comes from raster_zbuffer; False for raster_painter,
    whose ids are coloured per triangle ('face') and whose alpha-tested materials are cut per triangle here.
    Sets scene.visible to the number of triangles seen."""
    Image = _pil()
    from PIL import ImageChops

    W, H = scene.W, scene.H
    N = W * H
    size = (W, H)
    lighting = lighting or Lighting()
    A, B, C, TP = scene.A, scene.B, scene.C, scene.TP
    SX, SY = scene.SX, scene.SY
    parts = scene.parts
    texs = [p.material.tex() if has else None for p, has in zip(parts, scene.has_uv)]
    colors = [_rgb(p.material.color) for p in parts]
    U, V = scene.uv_lists()
    NX, NY, NZ = scene.normal_lists()

    def vertex_light(vi, back):
        if back:
            return lighting(-NX[vi], -NY[vi], -NZ[vi])
        return lighting(NX[vi], NY[vi], NZ[vi])

    if not exact:
        mode = "face"
    if mode == "face":
        cuts = [_cut(p.material) if not exact and texs[pi] is not None else None for pi, p in enumerate(parts)]
        col = [b"\0\0\0\0"] * (len(A) + 1)
        seen = set(ib)
        seen.discard(0)
        for t in seen:
            pi = TP[t - 1]
            a = A[t - 1]
            b = B[t - 1]
            c = C[t - 1]
            tex = texs[pi]
            if tex is not None:
                r, g, bl, al = tex.sample((U[a] + U[b] + U[c]) / 3.0, (V[a] + V[b] + V[c]) / 3.0)
                if cuts[pi] is not None and al < cuts[pi]:
                    continue
            else:
                r, g, bl = colors[pi]
            x0 = SX[a]
            y0 = SY[a]
            back = (SX[b] - x0) * (SY[c] - y0) - (SX[c] - x0) * (SY[b] - y0) < 0.0
            # the average of the vertex normals: smooth even with one value per face
            nx = NX[a] + NX[b] + NX[c]
            ny = NY[a] + NY[b] + NY[c]
            nz = NZ[a] + NZ[b] + NZ[c]
            ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            if back:
                ln = -ln
            li = lighting(nx / ln, ny / ln, nz / ln)
            li = 1.0 if li >= 1.0 else (li if li > 0.0 else 0.0)
            col[t] = bytes((int(r * li), int(g * li), int(bl * li), 255))
        scene.visible = len(seen)
        return Image.frombytes("RGBA", size, b"".join(map(col.__getitem__, ib)))

    if mode != "pixel":
        raise ValueError(f"unknown mode {mode!r}")
    # one channel atlas for all textures (rows of S texels, padded for bilinear) + an entry per flat colour
    used: list[Texture] = []
    for tex in texs:
        if tex is not None and all(tex is not u for u in used):
            used.append(tex)
    S = max([tex.w for tex in used], default=0) + 1
    chans: list[list[bytes]] = [[], [], [], []]
    base_of = {}
    n = 0
    for tex in used:
        base_of[id(tex)] = n
        for k, ch in enumerate(tex.padded(S)):
            chans[k].append(ch)
        n += S * (tex.h + 1)
    flat_of = {}
    for pi in range(len(parts)):
        if texs[pi] is None:
            flat_of[pi] = n
            for k, v in enumerate((*colors[pi], 255)):
                chans[k].append(bytes((v,)) * (S + 2))  # bilinear neighbours stay on the same colour
            n += S + 2
    RA, GA, BA, _AA = (b"".join(ch) for ch in chans)
    if not RA:
        return Image.new("RGBA", size, (0, 0, 0, 0))

    order = sorted(compress(range(N), ib), key=ib.__getitem__)  # visible pixels grouped by triangle
    bil = filter == "bilinear"
    half = 0.5 if bil else 0.0  # bilinear: texel centres at k + 0.5

    def setup(t):
        """Triangle t's vertices, whether it is a back face, and plane(f0, f1, f2) -> (fc, fx, fy) with
        f(x, y) = fc + fx * x + fy * y."""
        a = A[t - 1]
        b = B[t - 1]
        c = C[t - 1]
        x0 = SX[a]
        y0 = SY[a]
        e1x = SX[b] - x0
        e1y = SY[b] - y0
        e2x = SX[c] - x0
        e2y = SY[c] - y0
        area = e1x * e2y - e2x * e1y
        if -_TINY < area < _TINY:
            return a, b, c, area < 0.0, _flat_plane
        inv = 1.0 / area

        def plane(f0, f1, f2):
            d1 = (f1 - f0) * inv
            d2 = (f2 - f0) * inv
            fx = e2y * d1 - e1y * d2
            fy = e1x * d2 - e2x * d1
            return f0 - fx * x0 - fy * y0, fx, fy
        return a, b, c, area < 0.0, plane

    def texels(safe):
        """Per pixel: the atlas index, the light (0..255) and for bilinear the texel fractions.  safe checks
        every value (broken data); the normal run lets a bad one raise and is then repeated with it."""
        tix = [0] * N
        lum = bytearray(N)
        FU = bytearray(N) if bil else None
        FV = bytearray(N) if bil else None
        visible = 0
        for t, pix in groupby(order, ib.__getitem__):
            visible += 1
            a, b, c, back, plane = setup(t)
            pi = TP[t - 1]
            # light 0..1 -> 0.5..254.5: never below 0 or above 255 after interpolation inside the triangle
            lc, lx, ly = plane(vertex_light(a, back) * 254.0 + 0.5, vertex_light(b, back) * 254.0 + 0.5,
                               vertex_light(c, back) * 254.0 + 0.5)
            tex = texs[pi]
            if tex is None:
                base = flat_of[pi]
                for i in pix:
                    y, x = divmod(i, W)
                    tix[i] = base
                    if safe:
                        v = lc + lx * x + ly * y
                        lum[i] = 255 if v >= 255.0 else (int(v) if v > 0.0 else 0)
                    else:
                        lum[i] = int(lc + lx * x + ly * y)
                continue
            tw, th = tex.w, tex.h
            base = base_of[id(tex)]
            uc, ux, uy = plane((U[a] + _OFF) * tw - half, (U[b] + _OFF) * tw - half, (U[c] + _OFF) * tw - half)
            vc, vx, vy = plane((V[a] + _OFF) * th - half, (V[b] + _OFF) * th - half, (V[c] + _OFF) * th - half)
            if safe:
                for i in pix:
                    y, x = divmod(i, W)
                    fu = uc + ux * x + uy * y
                    fv = vc + vx * x + vy * y
                    if not -1e15 < fu < 1e15:
                        fu = 0.0
                    if not -1e15 < fv < 1e15:
                        fv = 0.0
                    iu = int(fu)
                    iv = int(fv)
                    tix[i] = base + (iv % th) * S + iu % tw
                    if bil:
                        FU[i] = int((fu - iu) * 255.99) if fu >= iu else 0
                        FV[i] = int((fv - iv) * 255.99) if fv >= iv else 0
                    v = lc + lx * x + ly * y
                    lum[i] = 255 if v >= 255.0 else (int(v) if v > 0.0 else 0)
            elif bil:
                for i in pix:
                    y, x = divmod(i, W)
                    fu = uc + ux * x + uy * y
                    fv = vc + vx * x + vy * y
                    iu = int(fu)
                    iv = int(fv)
                    tix[i] = base + (iv % th) * S + iu % tw
                    FU[i] = int((fu - iu) * 255.99) if fu >= iu else 0
                    FV[i] = int((fv - iv) * 255.99) if fv >= iv else 0
                    lum[i] = int(lc + lx * x + ly * y)
            else:  # the common case: no divisions, no clamps
                for i in pix:
                    y, x = divmod(i, W)
                    tix[i] = base + (int(vc + vx * x + vy * y) % th) * S + int(uc + ux * x + uy * y) % tw
                    lum[i] = int(lc + lx * x + ly * y)
        return tix, lum, FU, FV, visible

    try:
        tix, lum, FU, FV, scene.visible = texels(False)
    except (ValueError, OverflowError):  # a value out of range from broken data: check every one
        tix, lum, FU, FV, scene.visible = texels(True)

    def gather(offset=0):  # the texel lookups run in C, one channel at a time
        idx = tix if not offset else list(map(offset.__add__, tix))
        return Image.merge("RGB", [Image.frombytes("L", size, bytes(map(ch.__getitem__, idx)))
                                   for ch in (RA, GA, BA)])

    img = gather()
    if bil:  # four lookups blended by the texel fractions (in C)
        mu = Image.frombytes("L", size, bytes(FU))
        mv = Image.frombytes("L", size, bytes(FV))
        top = Image.composite(gather(1), img, mu)
        bot = Image.composite(gather(S + 1), gather(S), mu)
        img = Image.composite(bot, top, mv)
    shade = Image.frombytes("L", size, bytes(lum))
    rgb = ImageChops.multiply(img, Image.merge("RGB", (shade, shade, shade)))
    rgb.putalpha(Image.frombytes("L", size, bytes(map(bool, ib)).translate(_MASK)))
    return rgb


# ---- entry point ----------------------------------------------------------------------
def render(parts, size=256, ss: int = 2, matrix=None, yaw: float = 0.0, pitch: float = 0.0, roll: float = 0.0,
           auto_roll: bool = False, fit=None, mode: str = "pixel", filter: str = "nearest", cull: bool = True,
           margin: float = 0.04, front_ccw: bool = True, lighting: Lighting | None = None,
           raster: str = "auto", stats: dict | None = None):
    """The parts as an RGBA PIL image of ``size`` (int or (w, h)) with a transparent background; an empty
    picture when nothing can be drawn.  Raises EquipError without Pillow.

    matrix: camera (see camera()); otherwise yaw / pitch / roll, and auto_roll turns a long model onto the
    diagonal.  fit: a box both pictures of a before / after pair are framed by (view_bounds() or bounds() of both,
    with the same matrix).  cull: leave out back faces, except for two-sided materials.  raster 'auto'
    (z-buffer, the painter with mode 'face' above PAINTER_TRIANGLES), 'zbuffer' or 'painter'.
    ss: supersampling factor (2 = 2x2 samples per pixel).  lighting: a Lighting or any f(nx, ny, nz) -> 0..1.
    stats gets timings in seconds (scene, raster, resolve, reduce, total), tris (drawn), visible, raster_used,
    roll, scale (output pixels per model unit) and matrix (the camera used, roll included)."""
    Image = _pil()
    if mode not in ("pixel", "face"):
        raise ValueError(f"unknown mode {mode!r}")
    if raster not in ("auto", "zbuffer", "painter"):
        raise ValueError(f"unknown raster {raster!r}")
    w, h = (size, size) if isinstance(size, int) else (int(size[0]), int(size[1]))
    w, h = max(1, w), max(1, h)
    ss = max(1, int(ss))
    t0 = time.perf_counter()
    scene = Scene(parts, w * ss, h * ss, matrix, yaw, pitch, roll, auto_roll, fit, margin, front_ccw)
    tris = len(scene.A)
    if raster == "auto":
        raster = "painter" if tris > PAINTER_TRIANGLES else "zbuffer"
    t1 = time.perf_counter()
    if not tris:
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        t2 = t3 = t1
    else:
        if raster == "painter":
            ib = raster_painter(scene, cull)
        else:
            ib = raster_zbuffer(scene, cull)
        t2 = time.perf_counter()
        img = resolve(scene, ib, mode, lighting, filter, exact=raster == "zbuffer")
        t3 = time.perf_counter()
        if ss > 1:
            img = img.reduce(ss)
    t4 = time.perf_counter()
    if stats is not None:
        stats.update(scene=t1 - t0, raster=t2 - t1, resolve=t3 - t2, reduce=t4 - t3, total=t4 - t0,
                     tris=tris, visible=scene.visible, raster_used=raster, roll=scene.roll,
                     scale=scene.scale / ss, matrix=scene.R)
    return img
