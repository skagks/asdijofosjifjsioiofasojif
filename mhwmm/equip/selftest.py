"""The 3D preview's self-test on the installed game ("적용 후 미리보기" 점검): for a few of the game's own
equipment parts, how their meshes, streaming companions, materials and textures read and what the
preview makes of them, as text.  It goes into the 장비 외형 diagnostics zip so that what could not be
checked without the game (vanilla meshes and their streaming data, resident vs streaming textures,
materials with only AlphaMaskUsed set, timings on real hardware) can be read from a user's report.

Only sizes, versions, flags, names, timings and mean colours are written: no game pictures or data.
"""

from __future__ import annotations

import platform
import struct
import time
from collections import Counter

from . import EquipError
from . import look
from . import mesh as meshmod
from .model import PALICO_PARTS, Target
from .pathlist import versions
from .tex import Tex

DEFAULT_ITEMS = 12  # the command line's default; the diagnostics zip takes fewer
PICTURE_SIDE = 128  # the preview drawn for each part (only its timings and mean colour are kept)
MAX_MATERIALS = 12  # materials listed per mesh
OPAQUE_ALPHA = 200
ARMOR_ORDER = ("body", "helm", "arm", "waist", "leg")  # the body first: it says the most


def _middle_first(items: list) -> list:
    """``items`` from the middle on: typical equipment rather than the first (often placeholder) numbers."""
    mid = len(items) // 2
    return items[mid:] + items[:mid]


def candidates(files: look.GameFiles) -> list[Target]:
    """The game's equipment parts in the order the self-test tries them: by turns a female and a male
    armor part (several parts of one set), weapons of each type (main parts, then sub parts) and once a
    palico piece."""
    have = files.by_target
    items = sorted({t.item for t in have})

    def parts(kind: str, prefix: str, names) -> list[Target]:
        chosen = _middle_first([i for i in items if i.kind == kind and i.key.startswith(prefix)])
        return [i.with_part(p) for i in chosen for p in names if i.with_part(p) in have]

    by_type: dict[str, list] = {}
    for i in items:
        if i.kind == "weapon":
            by_type.setdefault(i.key.split("/")[0], []).append(i)
    by_type = {k: _middle_first(v) for k, v in sorted(by_type.items())}
    weapons = []  # one of each type (main parts, then sub parts), then the next one of each type ...
    for r in range(max((len(v) for v in by_type.values()), default=0)):
        row = [v[r] for v in by_type.values() if r < len(v)]
        weapons += [i.with_part(p) for p in ("main", "sub") for i in row if i.with_part(p) in have]
    streams = {"f": parts("armor", "f/", ARMOR_ORDER), "m": parts("armor", "m/", ARMOR_ORDER),
               "w": weapons, "p": parts("palico", "", PALICO_PARTS)}
    out, first = [], True
    while streams["f"] or streams["m"] or streams["w"]:
        for name in ("f", "m", "w", "p") if first else ("f", "m", "w", "w"):
            if streams[name]:
                out.append(streams[name].pop(0))
        first = False
    return out + streams["p"]


def _in_game(game: look.Source, path: str) -> bool:
    twin = meshmod.streaming_path(path)
    return game.has(path) or bool(twin and game.has(twin))


def pick(files: look.GameFiles, game: look.Source, count: int) -> list[Target]:
    """The first ``count`` candidates whose main meshes the game has."""
    out = []
    for target in candidates(files):
        if len(out) >= count:
            break
        paths, _before = look.mesh_plan(target, files=files)
        if paths and any(_in_game(game, p) for p in paths):
            out.append(target)
    return out


# ---- what one file says ----------------------------------------------------------------------
def _size(n: int | None) -> str:
    return "-" if n is None else f"{n:,} B"


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f} ms"


def _why(exc: BaseException) -> str:
    text = str(exc) if isinstance(exc, EquipError) else f"{type(exc).__name__}: {exc}"
    return text[:300]


def _tex_line(data: bytes | None, side: int) -> str:
    """A texture file's header: size, format, mips, flags (bit 0, bits 13-15 as REasy names them)."""
    if data is None:
        return "-"
    try:
        t = Tex(data)
        level = t.best_level(side)
        w, h = t.mip_size(level)
        return (f"{_size(len(data))} {t.width}x{t.height} {t.format_name} mips {t.mips} flags {t.flags:#x} "
                f"(bit0 {t.flags & 1}, bits13-15 {(t.flags >> 13) & 7}) -> mip {level} {w}x{h}"
                + (", chunk table" if t.chunks else ""))
    except Exception as exc:  # noqa: BLE001 - reported
        return f"{_size(len(data))} unreadable: {_why(exc)}"


def _read(game: look.Source, path: str) -> bytes | None:
    """The file, None when the game does not have it; EquipError with the reader's own message (not the
    short Korean reason the preview shows) when it cannot be read."""
    raw = getattr(game, "game", None)  # a GameSource's index
    try:
        if raw is not None:
            return raw.read(path) if raw.has(path) else None
        return game.get(path) if game.has(path) else None
    except EquipError:
        raise
    except Exception as exc:  # noqa: BLE001 - a damaged pak, an encrypted entry, no zstd module
        raise EquipError(f"{type(exc).__name__}: {exc}") from None


def _tex(game: look.Source, path: str, side: int) -> tuple[str, bool]:
    """(the line about one copy of a texture, whether the game has it)."""
    try:
        data = _read(game, path)
    except EquipError as exc:
        return f"read error: {_why(exc)}", True
    return _tex_line(data, side), data is not None


def _mesh_lines(game: look.Source, path: str, notes: Counter) -> tuple[list[str], list[str] | None]:
    """(lines about a mesh and its streaming companion, its material names or None)."""
    lines = [f"  mesh {path}"]
    t0 = time.perf_counter()
    try:
        data = _read(game, path)
    except EquipError as exc:
        notes["mesh read error"] += 1
        return lines + [f"    read error: {_why(exc)}"], None
    twin = meshmod.streaming_path(path)
    try:
        blob = _read(game, twin) if twin else None
    except EquipError as exc:
        blob = None
        lines.append(f"    streaming companion read error: {_why(exc)}")
    read = time.perf_counter() - t0
    if data is None:
        if blob is not None and meshmod.is_mesh(blob):
            notes["full mesh under streaming/"] += 1
            lines.append(f"    only a full copy under streaming/ ({_size(len(blob))})")
            data, blob = blob, None
        else:
            notes["mesh missing"] += 1
            return lines + ["    not in the game"], None
    magic = bytes(data[:4]).decode("ascii", "replace") if len(data) >= 4 else "?"
    version = struct.unpack_from("<I", data, 4)[0] if len(data) >= 8 else None
    notes[f"mesh {magic} {version}"] += 1
    lines.append(f"    {_size(len(data))}, magic {magic}, version {version}, read {_ms(read)}")
    if blob is None:
        lines.append("    streaming companion: none")
    elif meshmod.is_mesh(blob):
        lines.append(f"    streaming companion: a full mesh copy ({_size(len(blob))})")
    else:
        lines.append(f"    streaming companion: {_size(len(blob))}")
    try:
        info = meshmod.read_info(data)
    except meshmod.MeshError as exc:
        notes["mesh unsupported" if isinstance(exc, meshmod.Unsupported) else "mesh unreadable"] += 1
        return lines + [f"    tables: {_why(exc)}"], None
    used = [s for s in info.streaming_entries if s[1]]
    lines.append(f"    streaming entries {len(info.streaming_entries)} ({len(used)} with data, "
                 f"{_size(sum(s for _o, s in used))}); buffers {sorted(info.buffers)}; uv sets {info.uv_count}; "
                 f"32-bit indices {info.index32}")
    for i, lod in enumerate(info.lods):
        groups = ",".join(str(g[0]) for g in lod.groups)
        lines.append(f"    LOD {i}: {lod.triangle_count:,} triangles, buffers {lod.buffers}, groups [{groups}]")
    without = meshmod.available_lods(info, None)
    with_twin = meshmod.available_lods(info, blob)
    try:
        chosen = meshmod.pick_lod(info, blob, look.MAX_TRIANGLES)
        lines.append(f"    LODs readable without the companion {without}, with it {with_twin}; "
                     f"preview LOD {chosen} ({info.lods[chosen].triangle_count:,} triangles)")
        notes[f"LOD {chosen} chosen"] += 1
        if info.needs_streaming and blob is None:
            notes["streamed mesh without companion"] += 1
    except meshmod.MeshError as exc:
        notes["no readable LOD"] += 1
        lines.append(f"    LODs readable: none ({_why(exc)})")
    return lines, list(info.material_names)


def _material_lines(game: look.Source, mesh_path: str, names: list[str] | None, seen_tex: set,
                    notes: Counter) -> list[str]:
    from ..matfix.mdf2 import Mdf2File, detect_version

    path = look.mdf2_path(mesh_path)
    lines = [f"  mdf2 {path}"]
    try:
        data = _read(game, path)
    except EquipError as exc:
        notes["mdf2 read error"] += 1
        return lines + [f"    read error: {_why(exc)}"]
    if data is None:
        notes["mdf2 missing"] += 1
        return lines + ["    not in the game"]
    try:
        version = detect_version(data, prefer=int((versions().get("mdf2") or ("45",))[0]))
        if version is None:
            raise ValueError("unknown layout")
        mdf = Mdf2File(data, version)
        surfaces = look.surfaces(data, version)
    except Exception as exc:  # noqa: BLE001 - reported
        notes["mdf2 unreadable"] += 1
        return lines + [f"    unreadable: {_why(exc)}"]
    own = [m.name.text for m in mdf.materials]
    lines.append(f"    layout {version}, {len(own)} materials")
    if names is not None:
        only_mesh = [n for n in names if n not in own]
        only_mdf = [n for n in own if n not in names]
        lines.append("    names match the mesh" if not (only_mesh or only_mdf) else
                     f"    names: only in the mesh {only_mesh[:8]}, only in the mdf2 {only_mdf[:8]}")
        if only_mesh:
            notes["material names missing from the mdf2"] += 1
    for m in mdf.materials[:MAX_MATERIALS]:
        s = surfaces.get(m.name.text)
        try:
            slots = [(k.text, p.text) for k, p in m.textures]
        except (AttributeError, ValueError):
            slots = []
        flags = s.flags if s is not None else 0
        bits = [n for bit, n in ((look.BASE_TWO_SIDE, "two-sided"), (look.BASE_ALPHA_TEST, "alpha-test"),
                                 (look.ALPHA_MASK_USED, "alpha-mask-used"), (look.FORCED_TWO_SIDE, "forced-two-sided"))
                if flags & bit]
        lines.append(f"    material {m.name.text}: flags {flags:#010x} [{' '.join(bits)}]")
        if s is None:
            continue
        if s.albedo:
            lines.append(f"      albedo {s.slot} {s.albedo}")
        else:
            lines.append(f"      albedo: none ({'constant' if s.constant else 'no texture'})")
        mask_only = flags & look.ALPHA_MASK_USED and not flags & look.BASE_ALPHA_TEST and s.opacity != "albedo"
        atos = next((p for k, p in slots if k in look.OPACITY_SLOTS and not look.is_constant(p)), None)
        if mask_only:
            notes["AlphaMaskUsed only"] += 1
            if atos:
                notes["AlphaMaskUsed only, with ATOS"] += 1
            lines.append(f"      AlphaMaskUsed only (no holes cut): ATOS {atos or 'none'}")
        elif s.opacity:
            lines.append(f"      holes from {s.opacity} at {s.cut:.2f}")
        colours = []
        for name, (values, used) in s.colors.items():
            colours.append(f"{name} {' '.join(f'{v:.3f}' for v in values[:4])}{'' if used else ' (off)'}")
        if colours:
            lines.append("      " + "; ".join(colours))
        for tex in (s.albedo, atos if mask_only or s.opacity == "atos" else None):
            if not tex or tex.lower() in seen_tex:
                continue
            seen_tex.add(tex.lower())
            resident, has_resident = _tex(game, look.texture_path(tex), look.TEXTURE_SIDE)
            streaming, has_streaming = _tex(game, look.texture_path(tex, streaming=True), look.TEXTURE_SIDE)
            notes["texture " + ("both copies" if has_resident and has_streaming else "resident only" if has_resident
                                else "streaming only" if has_streaming else "missing")] += 1
            lines.append(f"      {tex}")
            lines.append(f"        resident  {resident}")
            lines.append(f"        streaming {streaming}")
    if len(mdf.materials) > MAX_MATERIALS:
        lines.append(f"    ... {len(mdf.materials) - MAX_MATERIALS} more materials")
    return lines


def _mean(img) -> str:
    """Mean colour of the opaque pixels of an RGBA picture ("-" when nothing is opaque)."""
    from PIL import ImageStat

    if img is None:
        return "-"
    rgba = img.convert("RGBA")
    mask = rgba.getchannel("A").point(lambda a: 255 if a > OPAQUE_ALPHA else 0)
    if mask.getbbox() is None:
        return "-"
    return " ".join(f"{v:.0f}" for v in ImageStat.Stat(rgba.convert("RGB"), mask=mask).mean)


def _part_lines(game: look.Source, target: Target, files: look.GameFiles, textures: look.Textures, thumb,
                notes: Counter, statuses: Counter, times: list) -> list[str]:
    lines = [f"== {target.kind} {target.key} {target.part}"]
    paths, _before = look.mesh_plan(target, files=files)
    seen_tex: set = set()
    for path in paths:
        mesh_lines, names = _mesh_lines(game, path, notes)
        lines += mesh_lines
        lines += _material_lines(game, path, names, seen_tex, notes)
    stats: dict = {}
    t0 = time.perf_counter()
    result = look.preview(target, None, game, PICTURE_SIDE, textures=textures, stats=stats)
    took = time.perf_counter() - t0
    times.append(took)
    statuses[result.status] += 1
    lines.append(f"  preview: {result.status}, {result.triangles:,} triangles, load {_ms(stats.get('load', 0))}, "
                 f"render {_ms(stats.get('render', 0))}, total {_ms(took)}")
    for path, source, lod, tris in stats.get("meshes", []):
        lines.append(f"    drawn {look._name(path)} from {source} LOD {lod}, {tris:,} triangles")
    for note in result.notes:
        lines.append(f"    note: {note}")
    shown = thumb(target) if thumb is not None else None
    if shown is not None:
        try:
            from PIL import Image

            with Image.open(shown) as img:
                game_mean = _mean(img)
        except (OSError, ValueError) as exc:
            game_mean = f"unreadable ({exc})"
    else:
        game_mean = "-"
    lines.append(f"    mean colour: preview {_mean(result.image)}; game thumbnail {game_mean}")
    return lines


def run(game, count: int = DEFAULT_ITEMS, thumb=None, seconds: float | None = None, files=None,
        progress=None) -> str:
    """The report for ``count`` of the game's equipment parts (``game``: anything with read/has, such as
    matfix.game.GameIndex).  ``thumb(target)``: the game's thumbnail PNG of a part or None (its mean colour
    is compared with the preview's); ``seconds``: stop starting new parts after this long.  Never raises."""
    t0 = time.perf_counter()
    lines = [f"preview self-test: format {look.PREVIEW_FORMAT}, python {platform.python_version()}, "
             "file versions " + ", ".join(f"{k} {'/'.join(versions().get(k) or ('?',))}" for k in ("mesh", "mdf2", "tex"))]
    try:
        look._pil()
    except EquipError as exc:
        return "\n".join(lines + [str(exc)]) + "\n"
    source = game if isinstance(game, look.Source) else look.GameSource(game)
    notes, statuses, times = Counter(), Counter(), []
    try:
        files = files or look.game_files()
        targets = pick(files, source, max(0, int(count)))
    except Exception as exc:  # noqa: BLE001 - the report says what went wrong
        return "\n".join(lines + [f"could not choose parts: {_why(exc)}"]) + "\n"
    lines.append(f"parts: {len(targets)} ({', '.join(f'{k} {n}' for k, n in Counter(t.kind for t in targets).items())})")
    textures = look.Textures()
    done = 0
    for i, target in enumerate(targets):
        if seconds is not None and time.perf_counter() - t0 > seconds:
            lines.append(f"stopped after {seconds:.0f} s ({len(targets) - i} parts left)")
            break
        if progress is not None:
            progress(i, len(targets), f"3D 미리보기 점검 중... {target.kind} {target.key} {target.part}")
        lines.append("")
        try:
            lines += _part_lines(source, target, files, textures, thumb, notes, statuses, times)
        except Exception as exc:  # noqa: BLE001 - one broken part must not end the report
            statuses["exception"] += 1
            lines.append(f"  failed: {_why(exc)}")
        done += 1
    lines.append("")
    lines.append(f"== summary: {done} parts in {_ms(time.perf_counter() - t0)}")
    lines.append("statuses: " + ", ".join(f"{k} {n}" for k, n in sorted(statuses.items())))
    if times:
        ordered = sorted(times)
        lines.append(f"preview time: median {_ms(ordered[len(ordered) // 2])}, max {_ms(ordered[-1])}")
    for key, n in sorted(notes.items()):
        lines.append(f"{key}: {n}")
    return "\n".join(lines) + "\n"
