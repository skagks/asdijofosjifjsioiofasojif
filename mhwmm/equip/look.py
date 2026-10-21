"""What an equipment part looks like with a mod's files: its meshes, materials and textures, read from
the mod first and from the game second, drawn with render.py ("적용 후 미리보기").

Rules (the facts follow REasy (MIT), MHWmodfixer (MIT) and the Havens modding wiki; no code taken):

- The meshes of a part are the game's own meshes of it in the bundled path list (pathlist.game_paths()
  sorted by paths.classify), only the main ones: alternatives such as ``ch03_003_0015_9`` or a ``_vfx``
  mesh next to ``ch03_003_0015`` are left out, unless the mod replaces only those.  The mod's own meshes
  of new names are added.  A look swap (ModScan.redirects: the part's prefab loads another equipment's
  mesh) draws that equipment's meshes instead.  A weapon is drawn one part at a time (main = the *_0
  models, sub = *_1), as the Target parts are.
- A mod's copy of a file always beats the game's.  A mesh's streaming companion (``streaming/``) comes
  from the same source as the mesh; a ``streaming/`` file that is itself a mesh is a full copy, used
  when the main path is missing.  A texture is read from the first source that has either copy, and
  there from the ``streaming/`` copy only when the resident one is smaller than MIN_SIDE: a resident
  copy of one source is never paired with a streaming copy of the other.  Within one mod, loose files
  beat its .pak files (REFramework loads loose files over paks) and a later pak beats an earlier one.
- Material: ``<mesh base>.mdf2.<version>``; submeshes are bound to its materials by NAME.  A name the
  .mdf2 does not have is drawn as a grey checkerboard, as the game shows such a part.  The albedo is the
  first texture of ALBEDO_SLOTS whose path is not a constant (``/null...``, ``systems/rendering/...``),
  else one whose path has ``_alb``.  ALBD/ALBM alpha is metalness, never opacity; ColorParam multiplies
  the colour; holes are cut only for BaseAlphaTestEnable (ATOS red) or a BaseAlphaMap albedo (its
  alpha), at max(AlphaTest_Ref, 0.25); BaseTwoSideEnable / ForcedTwoSideEnable draw both sides.
- Not drawn: dye colour layers, normal and roughness maps, glow, transparency (films become cut-outs).
  A .mdf2 colour change is listed as colour chips and a replaced albedo texture as a before / after
  swatch, so that a recolour that the picture cannot show still shows something.
- Textures are decoded at the smallest mip of at least TEXTURE_SIDE (tex.Tex.best_level) and kept in a
  small in-memory cache (Textures).  A texture whose smallest usable mip is over BIG_MIP (a mod
  texture of one mip: seconds of pure-Python GDeflate) is decoded once and kept downscaled on disk in
  the folder the caller gives; one over HUGE_MIP is not decoded at all and its part drawn in one colour.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from . import EquipError
from . import mesh as meshmod
from . import render
from ..constants import PAK_PATCH_RE
from ..deploy import is_root_pak
from ..matfix.mdf2 import Mdf2File, detect_version
from ..matfix.pak import PakFile, path_hash
from .model import Target
from .pathlist import PREFIX, game_paths, versions
from .paths import classify
from .scan import _why
from .tex import Tex

log = logging.getLogger(__name__)

PREVIEW_FORMAT = 1  # raise when the pictures can change (mesh.py, render.py, look.py): cached previews are made again
MAX_TRIANGLES = 150_000  # LOD budget of one mesh: its most detailed LOD within this
TEXTURE_SIDE = 512  # textures are decoded at the smallest mip with a side of at least this
MIN_SIDE = 256  # a resident texture smaller than this is read from its streaming/ copy (of the same source)
SWATCH_SIDE = 128
SUPERSAMPLE_SIZE = 512  # pictures up to this side are drawn at twice the size and scaled down
BIG_MIP = 1024  # a texture whose usable mip is bigger is decoded once and kept downscaled on disk
HUGE_MIP = 4096  # ... and one bigger than this is not decoded at all
MAX_SWATCHES = 6
MAX_CHIPS = 12
MAX_NOTES = 8
CACHE_PIXELS = 8 << 20  # decoded textures kept in memory (about 32 MB)

STATUS = {
    "ok": "",
    "hidden": "숨김 (모드가 이 부위를 감춤)",
    "no_mesh": "그릴 수 있는 모델이 없습니다",
    "unsupported": "이 모델 형식은 미리보기를 지원하지 않습니다",
    "missing": "모드 파일 누락",
    "too_big": "모델이 너무 커서 그리지 않았습니다",
    "error": "미리보기를 만들지 못했습니다",
}

# .mdf2 material flags (the low bits of the u64 after the shader type; REasy mdf_file.py names)
BASE_TWO_SIDE, BASE_ALPHA_TEST, ALPHA_MASK_USED, FORCED_TWO_SIDE = 1 << 0, 1 << 1, 1 << 7, 1 << 8
ALBEDO_SLOTS = ("BaseDielectricMap", "ALBD", "ALBDmap", "BackMap", "BaseMetalMap", "BaseDielectricMapBase",
                "BaseAlphaMap", "BaseShiftMap")  # tried in this order (our own rule: one albedo slot is usual)
ALPHA_ALBEDO = "BaseAlphaMap"  # ALBA: its alpha is opacity
OPACITY_SLOTS = ("AlphaTranslucentOcclusionSSSMap", "AlphaTranslucentOcclusionCavityMap")  # ATOS / ATOC: red
_NOT_ALBEDO = ("detail", "panorama", "secondary", "multiblend", "megane")  # slots whose "_alb" texture is not it
LAYERS = ("R", "G", "B", "A")
COLOR_PARAMS = ("ColorParam", *(f"ColorLayer_{c}" for c in LAYERS), "Emissive_Color")
COLOR_LABELS = {"ColorParam": "기본 색", **{f"ColorLayer_{c}": f"염색 {c}" for c in LAYERS}, "Emissive_Color": "발광 색"}
NEUTRAL_LAYER = 0.502  # a colour layer of this grey changes nothing (weapons keep all four at it)
MIN_CUT = 0.25
GREY = (190, 190, 190)
CHECKER = ((200, 200, 200), (128, 128, 128))
_CHECKER_IMAGE = None


def _version(ext: str, fallback: str) -> str:
    return (versions().get(ext) or (fallback,))[0]


def mesh_version() -> str:
    return _version("mesh", str(meshmod.FILE_VERSION))


def mdf2_path(mesh_path: str) -> str:
    """``x/y.mesh.241111606`` -> ``x/y.mdf2.45`` (the material the game pairs with a mesh)."""
    i = mesh_path.lower().rfind(".mesh.")
    return f"{mesh_path[:i] if i >= 0 else mesh_path}.mdf2.{_version('mdf2', '45')}"


def texture_path(path: str, streaming: bool = False) -> str:
    """The pak path of a texture a material names (``Art/.../x_ALBD.tex``), or of its streaming/ copy."""
    p = path.replace("\\", "/").strip().lstrip("@/")
    for head in (PREFIX, "streaming/"):
        if p.lower().startswith(head):
            p = p[len(head):]
    tail = "" if p.lower().rstrip("0123456789").endswith(".tex.") else f".{_version('tex', '241106027')}"
    return f"natives/STM/{'streaming/' if streaming else ''}{p}{tail}"


def _name(path: str) -> str:
    """The file name for a note, without the version suffix."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    stem, _, last = name.rpartition(".")
    return stem if stem and last.isdigit() else name


def _key(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("/").lower()


def is_constant(path: str | None) -> bool:
    """Texture paths the engine fills in itself (NullWhite, NullATOS ...): not files to read."""
    p = (path or "").replace("\\", "/").lower()
    return not p or "/null" in p or p.startswith("null") or p.startswith("systems/rendering/")


def _constant_color(path: str | None) -> tuple[int, int, int]:
    p = (path or "").lower()
    if "white" in p:
        return 255, 255, 255
    if "black" in p:
        return 24, 24, 24
    return GREY


# ---- sources --------------------------------------------------------------------------
class Source:
    """Files by full game path (``natives/STM/...`` with the version; case does not matter)."""

    label = ""
    from_mod = False

    def has(self, path: str) -> bool:
        raise NotImplementedError

    def get(self, path: str) -> bytes | None:
        """The bytes, None when the source has no such file; EquipError when it cannot be read."""
        raise NotImplementedError

    def ident(self, path: str) -> str | None:
        """A cheap stand-in for the file's content (for caches), None without the file."""
        raise NotImplementedError


def _pak_order(rel: str):
    m = PAK_PATCH_RE.match(rel)
    return (0, int(m.group("num")), rel.lower()) if m else (1, 0, rel.lower())


class ModSource(Source):
    """One mod's files: ``files`` maps its game paths to the stored files (EquipJob.files; the values may
    also be (stored file, key) pairs).  Root .pak files are read entry by entry by path hash."""

    label = "모드"
    from_mod = True

    def __init__(self, files: Mapping):
        self.loose: dict[str, Path] = {}
        self.names: dict[str, str] = {}  # lower-case path -> as the mod spells it
        self.paks: list[tuple[PakFile, str]] = []  # the later (winning) pak first
        self.errors: list[str] = []
        paks = []
        for rel, stored in files.items():
            if isinstance(stored, tuple):
                stored = stored[0]
            if is_root_pak(rel):
                paks.append((rel, Path(stored)))
            else:
                self.loose[_key(rel)] = Path(stored)
                self.names[_key(rel)] = rel.replace("\\", "/").strip().lstrip("/")
        for rel, stored in sorted(paks, key=lambda r: _pak_order(r[0]), reverse=True):
            try:
                pak = PakFile(stored)
                st = stored.stat()
            except Exception as exc:  # noqa: BLE001 - OSError, MatfixError or a table broken in another way
                self.errors.append(f"{rel}: pak 파일을 읽지 못했습니다 ({_why(exc)})")
                continue
            self.paks.append((pak, f"{stored}|{st.st_size}|{st.st_mtime_ns}"))

    def _find(self, path: str):
        stored = self.loose.get(_key(path))
        if stored is not None:
            return stored, None, -1
        if self.paks:
            h = path_hash(path)
            for pak, stamp in self.paks:
                i = pak.find(h)
                if i is not None:
                    return pak, stamp, i
        return None

    def has(self, path: str) -> bool:
        return self._find(path) is not None

    def get(self, path: str) -> bytes | None:
        found = self._find(path)
        if found is None:
            return None
        where, _stamp, i = found
        try:
            return where.read_bytes() if i < 0 else where.read(i)
        except Exception as exc:  # noqa: BLE001 - a missing stored file, an encrypted entry, no zstd module
            raise EquipError(f"{_name(path)}: 모드 파일을 읽지 못했습니다 ({_why(exc)})") from None

    def ident(self, path: str) -> str | None:
        found = self._find(path)
        if found is None:
            return None
        where, stamp, i = found
        if i < 0:
            try:
                st = where.stat()
            except OSError:
                return f"{where}|?"
            return f"{where}|{st.st_size}|{st.st_mtime_ns}"
        return f"{stamp}|{where.hashes[i]:x}|{where.offsets[i]}|{where.dsizes[i]}"

    def loose_paths(self) -> list[str]:
        """The game paths of the mod's loose files, spelt as the mod does."""
        return list(self.names.values())

    def has_mesh(self, path: str) -> bool:
        """The mesh or a full copy of it under streaming/."""
        if self.has(path):
            return True
        twin = meshmod.streaming_path(path)
        return twin is not None and self.has(twin)

    def mesh_paths(self) -> list[str]:
        """Main paths (lower case) of the mod's loose .mesh files; a streaming/ copy counts for its main path."""
        out = []
        mesh_ext = f".mesh.{mesh_version()}"
        for key in self.loose:
            if not key.endswith(mesh_ext):
                continue
            main = key.replace(f"{PREFIX}streaming/", PREFIX, 1) if key.startswith(f"{PREFIX}streaming/") else key
            if main not in out:
                out.append(main)
        return out


class GameSource(Source):
    """The installed game: anything with read(path) and has(path) (matfix.game.GameIndex)."""

    label = "게임"

    def __init__(self, game):
        self.game = game
        self.version = str(getattr(game, "fingerprint", "") or f"id{id(game)}")  # what changes with the game

    def has(self, path: str) -> bool:
        try:
            return bool(self.game.has(path))
        except Exception:  # noqa: BLE001 - a broken index answers "no"
            return False

    def get(self, path: str) -> bytes | None:
        try:
            return self.game.read(path)
        except Exception as exc:  # noqa: BLE001 - an encrypted entry, a damaged pak, no zstd module
            raise EquipError(f"{_name(path)}: 게임 파일을 읽지 못했습니다 ({_why(exc)})") from None

    def ident(self, path: str) -> str | None:
        return f"game|{self.version}|{_key(path)}" if self.has(path) else None


# ---- which meshes make up a part ----------------------------------------------------------------
class GameFiles:
    """The bundled path list's model files (meshes, materials, textures) by equipment part.  ``paths``
    replaces the bundled list (tests)."""

    def __init__(self, paths=None):
        self.by_target: dict[Target, dict[str, list[str]]] = {}
        self.by_item: dict[Target, list[Target]] = {}
        for p in game_paths() if paths is None else paths:
            low = p.lower()
            kind = "mesh" if ".mesh." in low else "mdf2" if ".mdf2." in low else "tex" if ".tex." in low else None
            if kind is None:
                continue
            t = classify(low)
            if t is None:
                continue
            entry = self.by_target.get(t)
            if entry is None:
                entry = self.by_target[t] = {"mesh": [], "mdf2": [], "tex": []}
                self.by_item.setdefault(t.item, []).append(t)
            entry[kind].append(low if low.startswith(PREFIX) else PREFIX + low.lstrip("/"))

    def files(self, target: Target, kind: str) -> list[str]:
        """``kind`` ("mesh", "mdf2", "tex") files of a part; of every part for a target without one."""
        return [p for t in self.parts(target) for p in self.by_target[t][kind]]

    def parts(self, target: Target) -> list[Target]:
        """The parts the list has files of: the target itself, or for one without a part all of its parts."""
        if target.part:
            return [target] if target in self.by_target else []
        return list(self.by_item.get(target, ()))


_FILES: GameFiles | None = None
_files_lock = threading.Lock()


def game_files() -> GameFiles:
    """The bundled list's GameFiles (built once, about 0.1 s)."""
    global _FILES
    with _files_lock:
        if _FILES is None:
            _FILES = GameFiles()
        return _FILES


def _matches(found: Target | None, target: Target) -> bool:
    return found is not None and (found == target if target.part else found.item == target)


def _stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].split(".", 1)[0]


def main_meshes(paths: list[str]) -> list[str]:
    """The main meshes of a part: those with the fewest "_" in their name (``ch03_003_0015`` beside its
    alternatives ``ch03_003_0015_0`` and ``_9``, ``it1299_0000_0`` beside ``it1299_0000_0_vfx``)."""
    if not paths:
        return []
    fewest = min(_stem(p).count("_") for p in paths)
    return [p for p in paths if _stem(p).count("_") == fewest]


def mesh_plan(target: Target, mod: ModSource | None = None, redirects=(), files: GameFiles | None = None
              ) -> tuple[list[str], list[str]]:
    """(meshes to draw with the mod, the game's own meshes to compare with) of a part, as lower-case
    game paths.  Without a mod both are the game's main meshes."""
    files = files or game_files()

    def own(t: Target) -> tuple[list[str], list[str]]:
        base, known = [], set()
        for part in files.parts(t):
            listed = files.files(part, "mesh")
            known.update(listed)
            main = main_meshes(listed)
            changed = [p for p in listed if mod.has_mesh(p)] if mod is not None else []
            if changed and not set(changed) & set(main):
                main = changed  # the mod replaces only alternatives: show those
            base += main
        if mod is None:
            return base, []
        new = [p for p in mod.mesh_paths() if p not in known and _matches(classify(p), t)]
        return base, new

    base, new = own(target)
    if mod is None:
        return base, base
    swaps = []
    for mine, other in redirects or ():
        if (mine == target or (not target.part and mine.item == target)) and other not in swaps:
            swaps.append(other)
    after: list[str] = []
    for t in swaps or [target]:
        b, n = own(t)
        after += [p for p in b + n if p not in after]
    return after, base


# ---- materials -----------------------------------------------------------------------------
@dataclass
class Surface:
    """What the preview uses of one .mdf2 material."""
    name: str
    albedo: str | None = None  # texture path as the material names it (Art/.../x_ALBD.tex)
    slot: str = ""
    constant: tuple | None = None  # rgb of a constant albedo (NullWhite ...)
    opacity: str | None = None  # "albedo" (ALBA alpha) | "atos" (ATOS red) | None
    atos: str | None = None
    cut: float | None = None
    two_sided: bool = False
    tint: tuple = (1.0, 1.0, 1.0)
    colors: dict = field(default_factory=dict)  # COLOR_PARAMS name -> (rgba values, used)
    flags: int = 0


def _floats(values, n: int = 4) -> tuple:
    out = []
    for v in list(values)[:n]:
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = 0.0
        out.append(v if math.isfinite(v) else 0.0)
    return tuple(out)


def surfaces(data: bytes, version: int | None = None) -> dict[str, Surface]:
    """The materials of a .mdf2 by name (EquipError when it cannot be read)."""
    try:
        found = detect_version(data, prefer=version or int(_version("mdf2", "45")))
        if found is None:
            raise ValueError("unknown layout")
        mdf = Mdf2File(data, found)
    except Exception as exc:  # noqa: BLE001 - a mod's file can be broken in any way
        raise EquipError(f"재질 파일을 읽지 못했습니다 ({exc})") from None
    out = {}
    for m in mdf.materials:
        s = Surface(m.name.text)
        try:
            slots = [(k.text, p.text) for k, p in m.textures]
            props = {}
            for i, (n, _hdr) in enumerate(m.props):
                try:
                    props[n.text] = mdf.prop_values(m, i)
                except (struct.error, ValueError, IndexError):
                    continue
            s.flags = struct.unpack_from("<I", mdf.buf, m.alpha_pos)[0]
        except (struct.error, ValueError, IndexError):
            out.setdefault(s.name, s)
            continue
        by_slot = {}
        for k, p in slots:
            by_slot.setdefault(k, p)
        for k in ALBEDO_SLOTS:
            if k in by_slot and by_slot[k]:
                if is_constant(by_slot[k]):
                    s.constant = s.constant or _constant_color(by_slot[k])
                    continue
                s.slot, s.albedo = k, by_slot[k]
                break
        else:
            for k, p in slots:
                if not is_constant(p) and "_alb" in p.lower() and not any(w in k.lower() for w in _NOT_ALBEDO):
                    s.slot, s.albedo = k, p
                    break
        s.two_sided = bool(s.flags & (BASE_TWO_SIDE | FORCED_TWO_SIDE))
        ref = _floats(props.get("AlphaTest_Ref") or [0.1], 1)
        ref = ref[0] if ref and 0 < ref[0] < 1 else 0.1
        if s.albedo and s.slot == ALPHA_ALBEDO:
            s.opacity, s.cut = "albedo", max(ref, MIN_CUT)
        elif s.flags & BASE_ALPHA_TEST:
            atos = next((by_slot[k] for k in OPACITY_SLOTS if k in by_slot and not is_constant(by_slot[k])), None)
            if atos:
                s.opacity, s.atos, s.cut = "atos", atos, max(ref, MIN_CUT)
        tint = _floats(props.get("ColorParam") or (), 3)
        if len(tint) == 3:
            s.tint = tuple(min(max(c, 0.0), 4.0) for c in tint)
        for name in COLOR_PARAMS:
            if name in props and props[name]:
                used = True
                if name.startswith("ColorLayer_"):
                    flag = props.get(f"Use_{name[-1]}")
                    used = not flag or _floats(flag, 1)[0] > 0.5
                s.colors[name] = (_floats(props[name]), used)
        out.setdefault(s.name, s)
    return out


@dataclass
class Chip:
    """One colour value of a material that the mod changes (before: the game's; None when unknown)."""
    material: str
    name: str  # COLOR_PARAMS
    after: tuple | None
    before: tuple | None = None
    after_on: bool = True  # a colour layer can be switched off (Use_R ...)
    before_on: bool = True

    @staticmethod
    def hex(values) -> str | None:
        """'#rrggbb' of 0..1 values (None for None)."""
        if not values:
            return None
        rgb = [int(round(min(max(float(v), 0.0), 1.0) * 255)) for v in list(values)[:3]] + [0, 0, 0]
        return "#%02x%02x%02x" % tuple(rgb[:3])

    def to_list(self) -> list:
        return [self.material, self.name, list(self.after) if self.after else None,
                list(self.before) if self.before else None, self.after_on, self.before_on]

    @classmethod
    def from_list(cls, data) -> "Chip":
        material, name, after, before, after_on, before_on = data
        return cls(str(material), str(name), tuple(map(float, after)) if after else None,
                   tuple(map(float, before)) if before else None, bool(after_on), bool(before_on))


def _differs(a, b) -> bool:
    return len(a) != len(b) or any(abs(x - y) > 1e-3 for x, y in zip(a, b))


def _plain(name: str, values: tuple, used: bool) -> bool:
    """Is a colour value one that changes nothing (only shown when the game's value is unknown)?"""
    if name == "ColorParam":
        return not _differs(values[:3], (1.0, 1.0, 1.0))
    if name.startswith("ColorLayer_"):
        return not used or all(abs(v - NEUTRAL_LAYER) < 0.01 for v in values[:3])
    return True  # Emissive_Color: only a change from the game's value says anything


def chips(after: dict[str, Surface], before: dict[str, Surface] | None) -> list[Chip]:
    """Colour values that differ between the game's materials (before; None without the game) and the
    mod's (after)."""
    out = []
    for name, s in after.items():
        old = before.get(name) if before is not None else None
        for param in COLOR_PARAMS:
            if param not in s.colors:
                continue
            values, used = s.colors[param]
            prev = old.colors.get(param) if old is not None else None
            if prev is None:  # no game value to compare with: only colours that do something
                if not _plain(param, values, used):
                    out.append(Chip(name, param, values, None, used))
            elif _differs(values, prev[0]) or used != prev[1]:
                out.append(Chip(name, param, values, prev[0], used, prev[1]))
    return out[:MAX_CHIPS]


# ---- textures -----------------------------------------------------------------------------
def _pil():
    try:
        from PIL import Image
    except ImportError:
        raise EquipError("3D 미리보기를 그리려면 Pillow가 필요합니다") from None
    return Image


class _Skip(Exception):
    """A texture too big to decode."""


class Textures:
    """Decoded textures shared by the pictures (thread-safe): a small in-memory cache keyed by what the
    sources say about each file, and on disk (``folder``; None: none) a downscaled copy of every texture
    whose usable mip is bigger than BIG_MIP."""

    def __init__(self, folder: Path | None = None, pixels: int = CACHE_PIXELS):
        self.folder = Path(folder) if folder else None
        self.pixels = pixels
        self._items: OrderedDict = OrderedDict()
        self._used = 0
        self._lock = threading.Lock()

    def _get(self, key):
        with self._lock:
            img = self._items.get(key)
            if img is not None:
                self._items.move_to_end(key)
            return img

    def _put(self, key, img) -> None:
        n = img.width * img.height
        with self._lock:
            if key in self._items:
                return
            self._items[key] = img
            self._used += n
            while self._used > self.pixels and len(self._items) > 1:
                _k, old = self._items.popitem(last=False)
                self._used -= old.width * old.height

    def load(self, chain, path: str, side: int = TEXTURE_SIDE):
        """(RGBA picture of about ``side``, the source it came from, problem) for a texture a material
        names: problem is None, "missing" (in no source), "huge" (too big to decode) or the reason it could
        not be read.  Never mixes a resident copy of one source with a streaming copy of another."""
        resident, streaming = texture_path(path), texture_path(path, streaming=True)
        for src in chain:
            has_res, has_str = src.has(resident), src.has(streaming)
            if not (has_res or has_str):
                continue
            try:
                ident = src.ident(resident if has_res else streaming)
                key = (ident, side)
                img = self._get(key) if ident else None
                if img is None:
                    img = self._decode(src, resident if has_res else streaming, streaming if has_res and has_str
                                       else None, side, ident)
                    if ident:
                        self._put(key, img)
                return img, src, None
            except _Skip:
                return None, src, "huge"
            except (EquipError, OSError, ValueError, MemoryError) as exc:
                return None, src, str(exc) if isinstance(exc, EquipError) else _why(exc)
        return None, None, "missing"

    def _decode(self, src: Source, first: str, twin: str | None, side: int, ident: str | None):
        Image = _pil()
        data = src.get(first)
        if data is None:
            raise EquipError("파일이 없습니다")
        tex = Tex(data)
        if twin is not None and max(tex.width, tex.height) < min(side, MIN_SIDE):
            try:
                big = Tex(src.get(twin) or b"")
                if max(big.width, big.height) > max(tex.width, tex.height):
                    tex = big
            except EquipError:
                pass  # a broken streaming copy: the resident one will do
        level = tex.best_level(side)
        w, h = tex.mip_size(level)
        if max(w, h) > HUGE_MIP:
            raise _Skip()
        disk = None
        if max(w, h) > BIG_MIP and self.folder is not None and ident:
            name = hashlib.sha1(f"{ident}|{level}|{side}".encode("utf-8")).hexdigest()[:24]
            disk = self.folder / f"{name}.png"
            try:
                with Image.open(disk) as cached:
                    return cached.convert("RGBA")
            except (OSError, ValueError):
                pass
        img = tex.to_image(level)
        if max(img.size) > side:
            img.thumbnail((side, side), Image.BILINEAR)
        if disk is not None:
            try:
                disk.parent.mkdir(parents=True, exist_ok=True)
                tmp = disk.with_name(f"{disk.name}.{os.getpid()}-{threading.get_ident()}.tmp")
                img.save(tmp, "PNG")
                tmp.replace(disk)
            except OSError as exc:
                log.debug("미리보기 텍스처를 저장하지 못했습니다: %s", exc)
        return img


def _checker():
    global _CHECKER_IMAGE
    if _CHECKER_IMAGE is None:
        Image = _pil()
        img = Image.new("RGBA", (16, 16), CHECKER[0] + (255,))
        for y in range(16):
            for x in range(16):
                if (x // 4 + y // 4) % 2:
                    img.putpixel((x, y), CHECKER[1] + (255,))
        _CHECKER_IMAGE = img
    return _CHECKER_IMAGE


def _flat(rgb, tint) -> tuple[int, int, int]:
    return tuple(int(min(255, max(0, round(c * k)))) for c, k in zip(rgb, tint))


def _tinted(img, tint, alpha=None):
    """``img`` (RGBA) with its colour multiplied by ``tint`` and ``alpha`` (an "L" picture, or 255) as
    alpha; a new picture (cached textures are never changed)."""
    Image = _pil()
    r, g, b, _a = img.split()
    if tint != (1.0, 1.0, 1.0):
        r, g, b = (ch.point([min(255, int(v * k + 0.5)) for v in range(256)]) for ch, k in zip((r, g, b), tint))
    if alpha is None:
        alpha = Image.new("L", img.size, 255)
    elif alpha.size != img.size:
        alpha = alpha.resize(img.size, Image.BILINEAR)
    return Image.merge("RGBA", (r, g, b, alpha))


# ---- building the parts ---------------------------------------------------------------------
@dataclass
class _Built:
    parts: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    status: str = "ok"
    triangles: int = 0
    from_mod: bool = False
    touched: bool = False  # some of the mod's files were found (even when they could not be used)
    albedos: list = field(default_factory=list)  # (texture path, Source) of the albedos drawn
    materials: list = field(default_factory=list)  # mdf2 paths of the meshes drawn
    meshes: list = field(default_factory=list)  # [(mesh path, source label, lod, triangles)]


def _first(chain, path: str):
    """(source, bytes) of the first source that has ``path`` (None, None); EquipError from reading."""
    for src in chain:
        if src.has(path):
            data = src.get(path)
            if data is not None:
                return src, data
    return None, None


def _read_mesh(chain, path: str):
    """(source, mesh bytes, streaming blob or None) from the first source that has the mesh (or a full
    copy of it under streaming/); the companion only from that source."""
    twin = meshmod.streaming_path(path)
    for src in chain:
        if src.has(path):
            data = src.get(path)
            if data is None:
                continue
            blob = src.get(twin) if twin and src.has(twin) else None
            if blob is not None and meshmod.is_mesh(blob):
                blob = None  # a full copy, not the streaming data
            return src, data, blob
        if twin and src.has(twin):
            data = src.get(twin)
            if data is not None and meshmod.is_mesh(data):
                return src, data, None
    return None, None, None


def build(chain, mesh_paths: list[str], textures: Textures, tex_side: int = TEXTURE_SIDE,
          max_triangles: int | None = None) -> _Built:
    """render.Part objects of the meshes (each with its material and textures from the first source of
    ``chain`` that has them), a status and the notes for the picture.  Broken files become notes.
    ``max_triangles``: the LOD budget of each mesh (MAX_TRIANGLES)."""
    out = _Built()
    budget = max_triangles or MAX_TRIANGLES
    has_game = any(not s.from_mod for s in chain)
    missing = hidden = too_big = unsupported = errors = no_game = 0
    flat_for_game = False
    surf_cache: dict[str, tuple] = {}  # mdf2 path -> (its source, {name: Surface} or None)
    mats: dict[tuple, render.Material] = {}  # (mdf2 path, material name) -> one Material for all its pieces

    def note(text: str) -> None:
        if text not in out.notes:
            out.notes.append(text)

    def gone(path: str, by_mod: bool) -> None:
        """A file in no source; ``by_mod``: a file the mod's own files point at (the game would hang on it)."""
        nonlocal missing
        missing += 1
        note(f"모드 파일 누락: {_name(path)} (모드에도 게임에도 없습니다)" if by_mod else f"게임에 없는 파일: {_name(path)}")

    def material_of(name: str, table: dict, msrc) -> render.Material:
        """The look of one mesh material (``table``: the .mdf2's, from ``msrc``)."""
        nonlocal errors, flat_for_game
        s = table.get(name)
        if s is None and table:
            note(f"재질 이름 불일치: {name} (게임에서는 체크무늬로 보입니다)")
            return render.Material(_checker(), two_sided=True)
        if s is None:
            return render.Material(color=GREY)
        material, problem, tsrc = _material(s, chain, textures, tex_side)
        if tsrc is not None:
            out.from_mod |= tsrc.from_mod
            out.albedos.append((s.albedo, tsrc))
        if problem == "missing":
            if has_game:
                gone(texture_path(s.albedo), msrc is not None and msrc.from_mod)
            else:
                flat_for_game = True
        elif problem == "huge":
            note(f"텍스처가 너무 커서 단색으로 그렸습니다: {_name(s.albedo)}")
        elif problem:
            errors += 1
            note(f"텍스처를 읽지 못했습니다: {_name(s.albedo)} ({problem})")
        return material

    for path in mesh_paths:
        try:
            src, data, blob = _read_mesh(chain, path)
        except EquipError as exc:
            errors += 1
            note(f"모델을 읽지 못했습니다: {exc}")
            continue
        out.touched |= src is not None and src.from_mod
        if data is None:  # a game path (the mod's own meshes are there): the bundled list is newer than the game
            if has_game:
                gone(path, False)
            else:
                no_game += 1
                note("게임 폴더가 없어 게임의 모델은 그릴 수 없습니다")
            continue
        try:
            info = meshmod.read_info(data)
            if info.lods and not any(one.triangle_count for one in info.lods):
                out.meshes.append((path, src.label, 0, 0))  # a mesh of no triangles: the game draws nothing
                out.from_mod |= src.from_mod
                hidden += 1
                continue
            lod = meshmod.pick_lod(info, blob, budget)
            tris = info.lods[lod].triangle_count
            if tris > render.MAX_TRIANGLES:
                too_big += 1
                note(f"{_name(path)}: 삼각형 {tris:,}개 - 너무 커서 그리지 않았습니다")
                continue
            try:
                geo = meshmod.read_geometry(data, info, blob, lod, uv=True, normals=True)
            except meshmod.MeshError as exc:
                alt = _resident_lod(info, blob, lod, budget)
                if alt is None:
                    raise
                try:
                    geo = meshmod.read_geometry(data, info, None, alt, uv=True, normals=True)
                except meshmod.MeshError:
                    raise exc from None  # the streaming data's reason, not the fallback's
                lod = alt
                note(f"{_name(path)}: 스트리밍 파일을 읽지 못해 낮은 LOD로 그렸습니다 ({exc})")
            st: dict = {}
            pieces = meshmod.pieces(geo, info, st)
        except meshmod.Unsupported as exc:
            unsupported += 1
            note(f"{_name(path)}: {exc}")
            continue
        except (meshmod.MeshError, EquipError, MemoryError) as exc:
            errors += 1
            note(f"모델을 읽지 못했습니다: {_name(path)} ({exc if isinstance(exc, EquipError) else _why(exc)})")
            continue
        out.meshes.append((path, src.label, lod, sum(p.triangle_count for p in pieces)))
        out.from_mod |= src.from_mod  # a mod mesh that hides the part changes it too
        if not pieces:
            if st.get("hidden"):  # collapsed or only broken triangles: the game shows nothing
                hidden += 1
            else:  # the LODs that can be read have no triangles; the others need the streaming file
                errors += 1
                note(f"{_name(path)}: 그릴 수 있는 삼각형이 없습니다")
            continue
        mpath = mdf2_path(path)
        out.materials.append(mpath)
        if mpath not in surf_cache:
            try:
                msrc, mdata = _first(chain, mpath)
                surf_cache[mpath] = (msrc, surfaces(mdata) if mdata is not None else None)
            except EquipError as exc:
                surf_cache[mpath] = (None, {})
                errors += 1
                text = str(exc)  # a read error names the file already, one of surfaces() does not
                note(text if text.startswith(_name(mpath)) else f"{_name(mpath)}: {text}")
        msrc, table = surf_cache[mpath]
        if table is None:  # in no source (without the game it may well be the game's)
            if has_game:
                gone(mpath, src.from_mod)
            else:
                note("게임 폴더가 없어 게임의 재질은 쓰지 못했습니다")
            table = {}
            surf_cache[mpath] = (None, table)
        elif msrc is not None:
            out.from_mod |= msrc.from_mod
        for pc in pieces:
            material = mats.get((mpath, pc.material_name))
            if material is None:
                material = mats[(mpath, pc.material_name)] = material_of(pc.material_name, table, msrc)
            out.parts.append(render.Part(pc.positions, pc.indices, pc.normals, pc.uvs, material))
            out.triangles += pc.triangle_count
    if flat_for_game:
        note("게임 폴더가 없어 게임의 텍스처를 쓰는 부분은 단색으로 그렸습니다")
    if out.parts:
        out.status = "missing" if missing else "ok"
        if out.triangles > render.MAX_TRIANGLES:
            out.status, out.parts = "too_big", []
            note(f"삼각형 {out.triangles:,}개 - 너무 커서 그리지 않았습니다")
    elif not mesh_paths or (no_game and not (missing or hidden or too_big or unsupported or errors)):
        out.status = "no_mesh"
    elif hidden and not (missing or too_big or unsupported or errors):
        out.status = "hidden"
    elif too_big:
        out.status = "too_big"
    elif unsupported:
        out.status = "unsupported"
    elif missing:
        out.status = "missing"
    else:
        out.status = "error"
    return out


def _resident_lod(info, blob, lod: int, budget: int) -> int | None:
    """The LOD to draw instead of ``lod`` when its streaming data could not be read: the best one of the
    .mesh itself (None when there is none, or ``lod`` did not use the streaming data)."""
    if blob is None or not any(info.lods[lod].buffers) or not meshmod.available_lods(info, None):
        return None
    alt = meshmod.pick_lod(info, None, budget)
    return alt if alt != lod and info.lods[alt].triangle_count <= render.MAX_TRIANGLES else None


def _material(s: Surface, chain, textures: Textures, side: int):
    """(render.Material, texture problem or None, the albedo's source or None) of a surface."""
    if s.albedo is None:
        return render.Material(color=_flat(s.constant or GREY, s.tint), two_sided=s.two_sided), None, None
    img, src, problem = textures.load(chain, s.albedo, side)
    if img is None:
        return render.Material(color=_flat(GREY, s.tint), two_sided=s.two_sided), problem, src
    alpha = None
    if s.opacity == "albedo":
        alpha = img.getchannel("A")
    elif s.opacity == "atos":
        atos, _src, _problem = textures.load(chain, s.atos, side)
        if atos is not None:
            alpha = atos.getchannel("R")
    cut = s.cut if alpha is not None else None
    return render.Material(_tinted(img, s.tint, alpha), alpha_cut=cut, two_sided=s.two_sided), None, src


# ---- the picture ------------------------------------------------------------------------------
@dataclass
class Look:
    """What preview() made: pictures as RGBA PIL images (transparent background)."""
    status: str
    image: object | None = None  # with the mod's files; without a mod the game's own look
    before: object | None = None  # the game's own look in the same camera and framing (compare)
    notes: list = field(default_factory=list)
    swatches: list = field(default_factory=list)  # [(texture name, game's picture | None, mod's picture, new)]
    chips: list = field(default_factory=list)  # [Chip]
    triangles: int = 0
    changed: bool = False  # the picture uses some of the mod's files (or other meshes than the game's)

    @property
    def text(self) -> str:
        return STATUS.get(self.status, "")


def _swatches(mod: ModSource, game: Source | None, target: Target, built: _Built | None,
              textures: Textures, files: GameFiles, tex_side: int) -> list:
    """Before / after pictures of the albedo textures the mod replaces (or adds) for the part.  The mod's
    are shrunk from what the picture decoded (a texture of one big mip is decoded only once)."""
    names: dict[str, str] = {}  # lower case -> texture path as a material names it
    for path, src in (built.albedos if built is not None else []):
        if src.from_mod:
            names.setdefault(path.lower(), path)
    if not names:  # nothing drawn: the mod's albedo-like textures of the part
        tex_ext = f".tex.{_version('tex', '241106027')}"
        found = [k for k in mod.loose_paths() if k.lower().endswith(tex_ext) and "_alb" in _name(k).lower()
                 and not k.lower().startswith(f"{PREFIX}streaming/") and _matches(classify(k), target)]
        found += [p for p in files.files(target, "tex") if "_alb" in _name(p) and mod.has(p)]
        for k in found:
            rel = k[len(PREFIX):] if k.lower().startswith(PREFIX) else k
            rel = rel.rpartition(".")[0]  # without the version: as a material names it
            names.setdefault(rel.lower(), rel)
    out = []
    for path in list(names.values())[:MAX_SWATCHES]:
        after, _src, _p = textures.load([mod], path, tex_side)
        if after is None:
            continue
        before, why = None, None
        if game is not None:  # without the game (or its copy unreadable) it is not known to be new
            before, _src, why = textures.load([game], path, SWATCH_SIDE)
        out.append((_name(path), _small(before), _small(after), why == "missing"))
    return out


def _small(img):
    if img is None:
        return None
    if max(img.size) > SWATCH_SIDE:
        img = img.copy()
        img.thumbnail((SWATCH_SIDE, SWATCH_SIDE))
    return img


def _chips(mod: ModSource, game: Source | None, target: Target, built: _Built | None, files: GameFiles) -> list:
    paths = list(built.materials) if built is not None else []
    mdf_ext = f".mdf2.{_version('mdf2', '45')}"
    paths += [k for k in mod.loose_paths() if k.lower().endswith(mdf_ext) and _matches(classify(k), target)]
    paths += [p for p in files.files(target, "mdf2") if mod.has(p)]
    out, seen = [], set()
    for path in paths:
        if _key(path) in seen or not mod.has(path):
            continue
        seen.add(_key(path))
        try:
            after = surfaces(mod.get(path) or b"")
        except EquipError:
            continue
        before = None
        if game is not None:
            try:
                data = game.get(path) if game.has(path) else None
                before = surfaces(data) if data is not None else {}
            except EquipError:
                before = {}
        out += chips(after, before)
    return out[:MAX_CHIPS]


def preview(target: Target, mod: ModSource | None = None, game: Source | None = None, size: int = 256,
            compare: bool = False, redirects=(), textures: Textures | None = None, files: GameFiles | None = None,
            stats: dict | None = None, extras: bool = True) -> Look:
    """A picture of ``target`` (one part; a target without a part draws all of them) with the mod's files,
    or the game's own look without a mod; with ``compare`` also the game's own look in the same framing.
    ``extras``: also the texture swatches and colour chips (not wanted for the small pictures).
    Bad files become a status and notes; EquipError only without Pillow."""
    _pil()
    t0 = time.perf_counter()
    textures = textures or Textures()
    files = files or game_files()
    size = max(16, int(size))
    tex_side = min(TEXTURE_SIDE, max(128, 2 * size))
    look = Look("ok")
    if mod is not None:
        look.notes += mod.errors
    after_paths, before_paths = mesh_plan(target, mod, redirects, files)
    chain = [s for s in (mod, game) if s is not None]
    if chain:
        after = build(chain, after_paths, textures, tex_side)
    else:
        after = _Built(status="no_mesh", notes=["게임 폴더가 없어 게임의 모델은 그릴 수 없습니다"])
    before = None
    if mod is not None and compare:
        if game is not None:
            before = build([game], before_paths, textures, tex_side)
        else:
            look.notes.append("게임 폴더가 없어 원래 모습은 그릴 수 없습니다")
    t1 = time.perf_counter()
    swapped = mod is not None and after_paths != before_paths  # a look swap or meshes of new names
    look.status, look.triangles, look.changed = after.status, after.triangles, after.from_mod or swapped
    look.notes += after.notes
    if before is not None:
        look.notes += [f"원래 모습: {n}" for n in before.notes]
    if after.status == "hidden":
        look.notes.insert(0, STATUS["hidden"])
    if mod is not None and after.parts and not (look.changed or after.touched):
        look.notes.append("이 모드는 이 부위의 모델·재질·텍스처를 바꾸지 않습니다 (게임 그대로)")
    shown = after.parts + (before.parts if before is not None else [])
    if shown:
        matrix = render.camera(shown, render.view_for(target.kind, target.part))
        fit = render.view_bounds(shown, matrix) if before is not None else None  # one frame for the pair
        ss = 2 if size <= SUPERSAMPLE_SIZE else 1  # big pictures (HiDPI screens) need no smoothing, and 4x cost
        if after.parts:
            look.image = render.render(after.parts, size, ss=ss, matrix=matrix, fit=fit)
        if before is not None and before.parts:
            look.before = render.render(before.parts, size, ss=ss, matrix=matrix, fit=fit)
    t2 = time.perf_counter()
    if mod is not None and extras:
        look.swatches = _swatches(mod, game, target, after, textures, files, tex_side)
        look.chips = _chips(mod, game, target, after, files)
    look.notes = list(dict.fromkeys(look.notes))[:MAX_NOTES]
    if stats is not None:
        stats.update(load=t1 - t0, render=t2 - t1, extras=time.perf_counter() - t2, total=time.perf_counter() - t0,
                     after_paths=after_paths, before_paths=before_paths, meshes=after.meshes,
                     before_meshes=before.meshes if before is not None else [], triangles=after.triangles)
    return look


# ---- what the manager keeps ------------------------------------------------------------------
@dataclass
class Swatch:
    name: str  # texture name
    before: Path | None  # the game's (None: a new texture, no game, or the game's copy could not be read)
    after: Path | None  # the mod's
    new: bool = False  # the game has no texture of this name


@dataclass
class Preview:
    """ModManager.equipment_preview's answer: PNG files (transparent background) and what to say."""
    status: str
    after: Path | None = None  # the picture with the mod (without a mod: the game's own look)
    before: Path | None = None  # compare: the game's own look, same camera and framing
    notes: list = field(default_factory=list)  # Korean, for the window
    swatches: list = field(default_factory=list)  # [Swatch]
    chips: list = field(default_factory=list)  # [Chip]
    triangles: int = 0
    changed: bool = False

    @property
    def text(self) -> str:
        """The status as the window shows it ("" for ok)."""
        return STATUS.get(self.status, "")

    def files(self) -> list[Path]:
        return [p for p in (self.after, self.before, *(x for s in self.swatches for x in (s.before, s.after)))
                if p is not None]

    def to_dict(self) -> dict:
        name = (lambda p: p.name if p is not None else None)  # noqa: E731
        return {"format": PREVIEW_FORMAT, "status": self.status, "after": name(self.after),
                "before": name(self.before), "notes": list(self.notes),
                "swatches": [[s.name, name(s.before), name(s.after), s.new] for s in self.swatches],
                "chips": [c.to_list() for c in self.chips], "triangles": self.triangles, "changed": self.changed}

    @classmethod
    def from_dict(cls, data, folder: Path) -> "Preview | None":
        """None for another format, a damaged record or one whose pictures are gone."""
        try:
            if data.get("format") != PREVIEW_FORMAT or data.get("status") not in STATUS:
                return None
            path = (lambda n: folder / Path(str(n)).name if n else None)  # noqa: E731 - only files of the folder
            out = cls(str(data["status"]), path(data.get("after")), path(data.get("before")),
                      [str(n) for n in data.get("notes", [])],
                      [Swatch(str(n), path(b), path(a), bool(new and new[0]))
                       for n, b, a, *new in data.get("swatches", [])],
                      [Chip.from_list(c) for c in data.get("chips", [])], int(data.get("triangles", 0)),
                      bool(data.get("changed", False)))
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        return out if all(p.is_file() for p in out.files()) else None
