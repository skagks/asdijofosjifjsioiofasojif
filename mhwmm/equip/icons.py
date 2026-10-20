"""The game's own UI bits for the equipment window: rarity colours and weapon-type icons.

Read at runtime from the user's game (``read`` is GameIndex.read) and never shipped:

- natives/STM/GUI/colorPreset.gcp.2: u32 version, "GCPR", u32 count, 4 bytes, then 40-byte
  entries indexed by app.ColorPreset.TYPE: 4 x RGBA8 colours (slot 0 is used), 8 bytes, a GUID.
  RARE_01..RARE_12 = 22..33 are the colours of in-game rarity 1..12.
- natives/STM/GUI/ui_texture/tex000000/tex000201_1_IMLM4.tex.241106027: an icon atlas on a 100 px
  grid (20 cells a row at 2048 px wide) whose row 16 holds the weapon glyphs in columns 6-19: white
  and grey shapes with a soft black shadow around them (as in the community's crops of the real
  atlas).  The icons made here keep the brightness as alpha (white glyph, no shadow), which the
  game's own tint (multiply) and any background take well.

Where things are comes from community notes (kvasszn/mhwilds-data, preschian/mhwilds-companion;
facts only), not from the game's GUI files, so every function checks what it read and gives {} or
None when it does not look as expected; icon_sheet() draws the atlas with the assumed labels so a
user with the game can confirm them.  Pillow is imported only here, inside the functions.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Callable

from . import EquipError
from .model import WEAPON_TYPES
from .tex import Tex

log = logging.getLogger(__name__)

Reader = Callable[[str], "bytes | None"]

# Raise when the pictures made from the game's textures can change (icons.py, tex.py, gdeflate.py), so that
# the cached ones (cache/equip-pictures in the app's data folder) are made again.
PICTURE_FORMAT = 2  # 2: icons for armor parts, kinsects and palicos
PALETTE_PATH = "natives/STM/GUI/colorPreset.gcp.2"
ATLAS_PATH = "natives/STM/GUI/ui_texture/tex000000/tex000201_1_IMLM4.tex.241106027"
RARE_PRESET = 22  # app.ColorPreset.TYPE RARE_01; RARE_n = 21 + n
RARITIES = range(1, 13)
GRID_WIDTH = 2048  # the atlas width the 100 px grid is measured at
CELL = 100
COLUMNS = 20
WEAPON_ROW = 16
# atlas column -> app.WeaponDef.TYPE (the atlas is not in TYPE order)
WEAPON_COLUMNS = {6: 1, 7: 0, 8: 3, 9: 2, 10: 6, 11: 7, 12: 4, 13: 5, 14: 8, 15: 9, 16: 10, 17: 11, 18: 12,
                  19: 13}
# other glyphs of the same atlas (row, column), as a real game's icon sheet shows them: the five armor
# pieces before the weapons in row 16, the kinsect first in row 17, the palico's face first in row 18
OTHER_CELLS = {"helm": (16, 0), "body": (16, 1), "arm": (16, 2), "waist": (16, 3), "leg": (16, 4),
               "kinsect": (17, 0), "palico": (18, 0)}


def _read(read: Reader, path: str) -> bytes | None:
    try:
        data = read(path)
    except Exception as exc:  # noqa: BLE001 - the game's paks may fail in many ways; the caller only wants None
        log.debug("게임 파일을 읽지 못했습니다 (%s): %s", path, exc)
        return None
    return bytes(data) if data else None


# ---- rarity colours ------------------------------------------------------------------
def palette(data: bytes) -> list[tuple[int, int, int, int]]:
    """colorPreset.gcp: slot 0 of every entry (RGBA), indexed by app.ColorPreset.TYPE."""
    if len(data) < 16 or data[4:8] != b"GCPR":
        raise EquipError("색상 파일이 아닙니다")
    count = struct.unpack_from("<I", data, 8)[0]
    count = min(count, (len(data) - 16) // 40)
    return [tuple(data[16 + 40 * i:20 + 40 * i]) for i in range(count)]  # type: ignore[misc]


def rarity_colors(read: Reader) -> dict[int, tuple[int, int, int]]:
    """In-game rarity (1-12) -> its colour; {} when the palette cannot be read or looks wrong."""
    data = _read(read, PALETTE_PATH)
    if data is None:
        return {}
    try:
        colors = palette(data)
    except EquipError as exc:
        log.debug("레어도 색을 읽지 못했습니다: %s", exc)
        return {}
    if len(colors) < RARE_PRESET + len(RARITIES):
        return {}
    out = {n: colors[RARE_PRESET - 1 + n][:3] for n in RARITIES}
    rgb = list(out.values())
    if len(set(rgb)) < 4 or sum(1 for c in rgb if not any(c)) > len(rgb) // 2:
        log.debug("레어도 색이 예상과 다릅니다: %s", rgb)
        return {}
    return out


# ---- weapon-type icons -----------------------------------------------------------------
def tint(image, rgb):
    """``image`` (RGBA) with its colour multiplied by ``rgb``; alpha kept."""
    from PIL import Image

    r, g, b, a = image.convert("RGBA").split()
    bands = [band.point([v * c // 255 for v in range(256)]) for band, c in zip((r, g, b), rgb)]
    return Image.merge("RGBA", (*bands, a))


def _atlas(read: Reader, need_rows: int = WEAPON_ROW + 1):
    """(Tex, mip level, cell size in pixels at that level) of the icon atlas, or None when it is not
    there; EquipError when it is smaller than ``need_rows`` rows of cells."""
    data = _read(read, ATLAS_PATH)
    if data is None:
        return None
    tex = Tex(data)
    level = 0
    while level + 1 < tex.mips and tex.mip_size(level)[0] > GRID_WIDTH:
        level += 1
    w, h = tex.mip_size(level)
    # the grid scales with the picture (a 4096 px atlas without smaller mips: 200 px cells)
    cell = CELL if COLUMNS * CELL <= w <= GRID_WIDTH else w * CELL // GRID_WIDTH
    if cell < 8 or w < COLUMNS * cell or h < need_rows * cell:
        raise EquipError(f"아이콘 아틀라스 크기가 예상과 다릅니다 ({w}x{h})")
    return tex, level, cell


def _shape(cell):
    """What shows of a light glyph (brightness x alpha, an "L" image): its dark shadow drops out."""
    from PIL import ImageChops

    return ImageChops.multiply(cell.convert("L"), cell.getchannel("A"))


def _glyph(shape):
    """A white RGBA picture with ``shape`` as its alpha."""
    from PIL import Image

    out = Image.new("RGBA", shape.size, (255, 255, 255, 0))
    out.putalpha(shape)
    return out


def _check_cell(cell) -> str:
    """'ok' for a light grey-scale glyph on a transparent cell (a shadow around it is fine), else
    what is wrong with it."""
    from PIL import ImageChops

    visible = _shape(cell).point(lambda v: 255 if v > 64 else 0)
    if visible.getbbox() is None:
        return "empty"
    alpha = cell.getchannel("A")
    if sum(alpha.histogram()[:17]) < cell.width * cell.height // 10:
        return "opaque"
    r, g, b = cell.convert("RGB").split()
    spread = ImageChops.subtract(ImageChops.lighter(ImageChops.lighter(r, g), b),
                                 ImageChops.darker(ImageChops.darker(r, g), b)).histogram(mask=visible)
    if sum(v * n for v, n in enumerate(spread)) > 24 * sum(spread):
        return "colour"
    gray = cell.convert("L").histogram(mask=alpha.point(lambda v: 255 if v >= 128 else 0))
    if sum(gray[200:]) < max(1, sum(gray) // 16):
        return "dark"
    return "ok"


def _weapon_cells(read: Reader):
    """(cell size, {atlas column: cell image}) of the weapon row, or None.  Only the GDeflate tiles
    under that row are decoded."""
    found = _atlas(read)
    if found is None:
        return None
    tex, level, cell = found
    band = tex.band(WEAPON_ROW * cell, (WEAPON_ROW + 1) * cell, level)
    return cell, {col: band.crop((col * cell, 0, (col + 1) * cell, cell)) for col in WEAPON_COLUMNS}


def weapon_type_icons(read: Reader, size: int = 32) -> dict:
    """app.WeaponDef.TYPE -> a white glyph with alpha (PIL RGBA, size x size) from the game's icon
    atlas; {} when the atlas cannot be read or does not look as expected (or Pillow is missing).
    The glyphs are white: tint() them for a light background."""
    try:
        from PIL import Image

        found = _weapon_cells(read)
        if found is None:
            return {}
        cell, cells = found
        problems = {col: what for col, im in cells.items() if (what := _check_cell(im)) != "ok"}
        shapes = {col: _shape(im) for col, im in cells.items()}
        if problems or len({s.tobytes() for s in shapes.values()}) < len(shapes):
            log.info("무기 아이콘이 예상과 다릅니다: %s", problems or "같은 그림이 여럿")
            return {}
        # one square box around every glyph, so all icons share a scale and a baseline
        boxes = [s.point(lambda v: 255 if v > 16 else 0).getbbox() for s in shapes.values()]
        left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
        right, bottom = max(b[2] for b in boxes), max(b[3] for b in boxes)
        side = min(cell, max(right - left, bottom - top) + 2)
        x0 = min(max(0, (left + right - side) // 2), cell - side)
        y0 = min(max(0, (top + bottom - side) // 2), cell - side)
        out = {}
        for col, wtype in WEAPON_COLUMNS.items():
            glyph = _glyph(shapes[col].crop((x0, y0, x0 + side, y0 + side)))
            out[wtype] = glyph.resize((size, size), Image.LANCZOS) if side != size else glyph
        return out
    except Exception as exc:  # noqa: BLE001 - best effort: the window falls back to its own icons
        log.debug("무기 아이콘을 만들지 못했습니다: %s", exc)
        return {}


def other_icons(read: Reader, size: int = 32) -> dict:
    """"helm" ... "leg", "kinsect", "palico" (OTHER_CELLS) -> a white glyph with alpha like
    weapon_type_icons; a cell that does not look like a glyph is left out."""
    try:
        from PIL import Image

        found = _atlas(read, need_rows=max(r for r, _c in OTHER_CELLS.values()) + 1)
        if found is None:
            return {}
        tex, level, cell = found
        first = min(r for r, _c in OTHER_CELLS.values())
        band = tex.band(first * cell, (max(r for r, _c in OTHER_CELLS.values()) + 1) * cell, level)
        out = {}
        for name, (row, col) in OTHER_CELLS.items():
            im = band.crop((col * cell, (row - first) * cell, (col + 1) * cell, (row - first + 1) * cell))
            what = _check_cell(im)
            if what != "ok":
                log.info("아이콘이 예상과 다릅니다 (%s): %s", name, what)
                continue
            shape = _shape(im)
            left, top, right, bottom = shape.point(lambda v: 255 if v > 16 else 0).getbbox()
            side = min(cell, max(right - left, bottom - top) + 2)
            x0 = min(max(0, (left + right - side) // 2), cell - side)
            y0 = min(max(0, (top + bottom - side) // 2), cell - side)
            glyph = _glyph(shape.crop((x0, y0, x0 + side, y0 + side)))
            out[name] = glyph.resize((size, size), Image.LANCZOS) if side != size else glyph
        return out
    except Exception as exc:  # noqa: BLE001 - best effort, like weapon_type_icons
        log.debug("아이콘을 만들지 못했습니다: %s", exc)
        return {}


# ---- diagnostics -----------------------------------------------------------------------
def icon_sheet(read: Reader):
    """A picture to confirm the assumed positions on a real game: rows 15-17 of the icon atlas at
    full size with column numbers, under row 16 the TYPE each weapon column is taken for and
    whether its cell passed the checks, the rarity colours with a tinted glyph each, and the whole
    atlas at half size with its grid (also when it is too small for row 16).  None when the atlas
    cannot be read (or Pillow is missing)."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        found = _atlas(read, need_rows=1)
        if found is None:
            return None
        tex, level, cell = found
        w, h = tex.mip_size(level)
        whole = tex.to_image(level)
        has_rows = h >= (WEAPON_ROW + 2) * cell
        colors = rarity_colors(read)
        font = ImageFont.load_default()
        bg, grid, text, warn = (32, 32, 36, 255), (200, 60, 60, 255), (230, 230, 230, 255), (255, 170, 60, 255)
        pad, line = 40, 16
        notes = (ATLAS_PATH,
                 f"{w}x{h} (mip {level} of {tex.mips}, {tex.width}x{tex.height}), {tex.format_name}, cell {cell} px",
                 f"rows {WEAPON_ROW - 1}-{WEAPON_ROW + 1}; under row {WEAPON_ROW}: the app.WeaponDef.TYPE each column "
                 "is taken for and the check result" if has_rows else f"no row {WEAPON_ROW + 1}: the atlas is smaller "
                 "than expected",
                 f"rarity colours: {PALETTE_PATH} entries {RARE_PRESET}-{RARE_PRESET + 11} (rarity 1-12)")
        width = max(COLUMNS * cell, w // 2) + 2 * pad
        height = pad // 2 + (len(notes) + 1) * line + 3 * cell + 4 * line + cell + 4 * line + h // 2 + pad
        sheet = Image.new("RGBA", (width, height), bg)
        draw = ImageDraw.Draw(sheet)
        y = pad // 2
        for note in notes:
            draw.text((pad, y), note, fill=text if has_rows else warn, font=font)
            y += line
        sample = None
        if has_rows:
            for col in range(COLUMNS):
                draw.text((pad + col * cell + 4, y), str(col), fill=text, font=font)
            y += line
            rows = whole.crop((0, (WEAPON_ROW - 1) * cell, COLUMNS * cell, (WEAPON_ROW + 2) * cell))
            sheet.alpha_composite(rows, (pad, y))
            for k in range(4):
                draw.line((pad, y + k * cell, pad + COLUMNS * cell, y + k * cell), fill=grid)
            for col in range(COLUMNS + 1):
                draw.line((pad + col * cell, y, pad + col * cell, y + 3 * cell), fill=grid)
            for k in range(3):
                draw.text((4, y + k * cell + cell // 2 - 6), str(WEAPON_ROW - 1 + k), fill=text, font=font)
            y += 3 * cell + 2
            for col, wtype in WEAPON_COLUMNS.items():
                im = rows.crop((col * cell, cell, (col + 1) * cell, 2 * cell))
                if wtype == 0:
                    sample = im
                state = _check_cell(im)
                x = pad + col * cell + 2
                draw.text((x, y), f"T{wtype}", fill=text, font=font)
                draw.text((x, y + line), WEAPON_TYPES[wtype][2][:14], fill=text, font=font)
                draw.text((x, y + 2 * line), state, fill=text if state == "ok" else warn, font=font)
            y += 4 * line
        if colors:
            mono = _glyph(_shape(sample)) if sample is not None else None
            for n, rgb in colors.items():
                x = pad + (n - 1) * cell
                if mono is not None:
                    sheet.alpha_composite(tint(mono, rgb), (x, y))
                draw.rectangle((x, y + cell + 2, x + cell - 8, y + cell + 10), fill=(*rgb, 255))
                draw.text((x, y + cell + 12), f"R{n} #{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}", fill=text, font=font)
        else:
            draw.text((pad, y), "rarity colours: not found or unexpected", fill=warn, font=font)
        y += cell + 4 * line
        sheet.alpha_composite(whole.resize((max(1, w // 2), max(1, h // 2))), (pad, y))
        for col in range(COLUMNS + 1):
            x = pad + col * cell // 2
            draw.line((x, y, x, y + h // 2), fill=grid)
        for row in range(h // cell + 1):
            draw.line((pad, y + row * cell // 2, pad + w // 2, y + row * cell // 2), fill=grid)
            if row * cell < h:
                draw.text((4, y + row * cell // 2 + 2), str(row), fill=text, font=font)
        return sheet.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - a diagnostic: None tells the caller it could not be made
        log.info("아이콘 확인 그림을 만들지 못했습니다: %s", exc)
        return None
