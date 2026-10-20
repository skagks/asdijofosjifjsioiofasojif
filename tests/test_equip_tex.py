import random
import struct

import equip_tex_fixtures as F
import pytest

from mhwmm.equip import EquipError, gdeflate, icons
from mhwmm.equip.tex import Tex, decode, format_name

RNG = random.Random(20261020)


def rand(n: int) -> bytes:
    return bytes(RNG.randrange(256) for _ in range(n))


# ---- GDeflate ----------------------------------------------------------------------------
def test_stored_static_and_dynamic_blocks_round_trip():
    head = rand(300)
    tokens = list(head) + [(40, 300), 7, (25, 1), (200, 2), (1000, 3), 9, (5, 150)]
    blocks = [("stored", rand(1000)), ("static", tokens), ("stored", b""), ("dynamic", tokens),
              ("stored", rand(77))]
    tile = F.encode_tile(blocks)
    expect = b"".join(F.expand(p) if kind != "stored" else p for kind, p in blocks)
    stream = F.tile_stream([tile], len(expect))
    assert gdeflate.is_gdeflate(stream)
    assert gdeflate.uncompressed_size(stream) == len(expect)
    assert gdeflate.decompress(stream) == expect


def test_overlapping_long_and_far_matches():
    # distance 1 and 2 runs longer than the distance, Deflate64 lengths (> 258, 16 extra bits)
    # and distances past 32768 (codes 30-31)
    tokens = list(rand(40000)) + [(5000, 1), 65, (777, 2), (300, 39000), (19000, 33000)]
    expect = F.expand(tokens)
    assert len(expect) <= gdeflate.TILE_SIZE
    for kind in ("static", "dynamic"):
        stream = F.tile_stream([F.encode_tile([(kind, tokens)])], len(expect))
        assert gdeflate.decompress(stream) == expect


@pytest.mark.parametrize("kind", ["stored", "static", "dynamic"])
def test_multi_tile_streams_and_ranges(kind):
    data = rand(5000) * 12 + bytes(30000) + b"0123456789" * 12000 + rand(1234)  # 3 full tiles + a short one
    stream = F.compress(data, kind)
    assert struct.unpack_from("<H", stream, 2)[0] == 4
    assert gdeflate.decompress(stream) == data
    assert gdeflate.decompress(stream, size=len(data)) == data
    start, part = gdeflate.decompress_range(stream, 70000, 140000)  # tiles 1 and 2 only
    assert start == gdeflate.TILE_SIZE and len(part) == 2 * gdeflate.TILE_SIZE
    assert part == data[start:start + len(part)]
    start, part = gdeflate.decompress_range(stream, len(data) - 10, len(data) + 99)
    assert data[start:] == part
    assert gdeflate.decompress_range(stream, len(data), len(data) + 5) == (len(data), b"")


def test_full_last_tile_and_empty_stream():
    data = rand(gdeflate.TILE_SIZE * 2)
    stream = F.compress(data, "stored")
    assert struct.unpack_from("<I", stream, 4)[0] >> 2 == 0  # last tile size 0 = a full tile
    assert gdeflate.decompress(stream) == data
    assert gdeflate.decompress(F.tile_stream([], 0)) == b""


def test_bad_streams_raise_gdeflate_error():
    data = rand(3000) + bytes(3000)
    good = F.compress(data, "static")
    bad = [
        b"\x04\xfa" + good[2:],                                  # magic
        good[:2] + good[2:4] + struct.pack("<I", 2 | (len(data) << 2)) + good[8:],  # tile size index
        good[:9],                                                # tile table cut off
        good[:len(good) // 2],                                   # tile data cut off
        good[:8] + struct.pack("<I", 10 ** 6) + good[12:],       # last tile size past the data
        F.tile_stream([F.encode_tile([("static", list(b"abc"))])], 4),   # one byte short
        F.tile_stream([F.encode_tile([("static", list(b"abcde"))])], 3),  # two bytes too many
        F.tile_stream([F.encode_tile([("static", [1, (3, 5)])])], 4),     # match before the tile start
    ]
    reserved = F._Streams()
    reserved.put(0, 1 | 3 << 1, 3)  # block type 3
    reserved.ensure(0)
    bad.append(F.tile_stream([reserved.serialise()], 10))
    oversubscribed = F._Streams()  # a dynamic block whose precode is over-full
    oversubscribed.put(0, 1 | 2 << 1, 3)
    oversubscribed.ensure(0)
    oversubscribed.put(0, 15 << 10, 14)
    oversubscribed.ensure(0)
    for k in range(19):
        oversubscribed.put(k, 1, 3)
        oversubscribed.ensure(k)
    bad.append(F.tile_stream([oversubscribed.serialise()], 10))
    for stream in bad:
        with pytest.raises(gdeflate.GDeflateError):
            gdeflate.decompress(stream)
    with pytest.raises(EquipError):
        gdeflate.decompress(good, size=len(data) + 1)
    assert not gdeflate.is_gdeflate(b"\x04\xfb\x01")


def test_random_corruption_never_escapes_or_hangs():
    data = rand(2000) + b"pattern!" * 2000 + rand(500)
    good = bytearray(F.compress(data, "dynamic"))
    rng = random.Random(7)
    for _ in range(150):
        broken = bytearray(good)
        for _ in range(rng.randrange(1, 4)):
            pos = rng.randrange(8 + 4, len(broken))
            broken[pos] ^= 1 << rng.randrange(8)
        try:
            out = gdeflate.decompress(bytes(broken))
        except gdeflate.GDeflateError:
            continue
        assert len(out) == len(data)


# ---- .tex --------------------------------------------------------------------------------
def rgba_mip(level, w, h):
    return b"".join(bytes((x * 16 + level, y * 16, 200, 255 - x)) for y in range(h) for x in range(w))


def test_raw_rgba_texture_with_gdeflate_chunks_and_padding():
    mips = F.mip_chain(8, 4, 28, rgba_mip)
    data = F.build_tex(8, 4, 28, mips, gdeflate=[True, "dynamic", False, False])
    t = Tex(data)
    assert (t.width, t.height, t.mips, t.images, t.format, t.version) == (8, 4, 4, 1, 28, 241106027)
    assert t.format_name == "RGBA8" and t.chunks is not None
    assert [m[1] for m in t.mip_table] == [256, 256, 256, 256]  # 32-byte rows padded to 256
    raw = t.mip_bytes(0)
    assert len(raw) == 4 * 256 and raw[:32] == mips[0][:32] and raw[32:256] == bytes(224)
    pytest.importorskip("PIL")
    for level in range(4):
        im = t.to_image(level)
        w, h = t.mip_size(level)
        assert im.mode == "RGBA" and im.size == (w, h)
        assert im.tobytes() == mips[level]


def test_bc1_and_bc7_pictures():
    pytest.importorskip("PIL")
    red, blue = (255, 0, 0), (0, 0, 255)  # exact in RGB565

    def pixel(x, y):
        return None if (x, y) == (0, 0) else red if x < 8 else blue

    bc1 = F.bc1_image(12, 8, pixel)  # 3 blocks a row: 24 bytes padded to a 256-byte pitch
    data = F.build_tex(12, 8, 72, [bc1])
    t = Tex(data)
    assert t.mip_table[0][1:] == (256, 512)
    im = t.to_image()
    assert im.getpixel((0, 0))[3] == 0
    assert im.getpixel((1, 0)) == (*red, 255) and im.getpixel((11, 7)) == (*blue, 255)
    bc7 = F.bc7_solid(10, 120, 250, 200) * 4  # 8x8
    im = decode(F.build_tex(8, 8, 98, [bc7], gdeflate="stored"))
    assert im.getpixel((5, 5)) == (10, 120, 250, 200)


def test_band_matches_the_whole_picture():
    pytest.importorskip("PIL")
    w, h = 64, 1100  # R8 rows padded to 256 bytes: 5 GDeflate tiles

    def gray(level, mw, mh):
        return bytes((x * 3 + y * 7) & 255 for y in range(mh) for x in range(mw))

    t = Tex(F.build_tex(w, h, 61, [gray(0, w, h)]))
    whole = t.to_image()
    assert whole.getpixel((5, 3)) == (36, 36, 36, 255)
    for y0, y1 in ((0, 1), (300, 301), (255, 700), (1000, 1100), (1090, 5000)):
        band = t.band(y0, y1)
        assert band.tobytes() == whole.crop((0, y0, w, min(y1, h))).tobytes()
    bc1 = F.bc1_image(32, 40, lambda x, y: (8 * (y // 4), 0, 0))
    t = Tex(F.build_tex(32, 40, 71, [bc1]))
    whole = t.to_image()
    for y0, y1 in ((0, 3), (5, 6), (6, 39), (37, 40)):
        assert t.band(y0, y1).tobytes() == whole.crop((0, y0, 32, y1)).tobytes()
    with pytest.raises(EquipError):
        t.band(40, 50)


def test_best_level_and_decode_max_side():
    mips = F.mip_chain(1024, 256, 71, lambda level, w, h: bytes(((w + 3) // 4) * ((h + 3) // 4) * 8))[:4]
    t = Tex(F.build_tex(1024, 256, 71, mips, gdeflate=False))
    assert [t.mip_size(k) for k in range(4)] == [(1024, 256), (512, 128), (256, 64), (128, 32)]
    assert t.best_level(128) == 3
    assert t.best_level(129) == 2
    assert t.best_level(512) == 1
    assert t.best_level(4096) == 0
    assert t.best_level(1) == 3
    pytest.importorskip("PIL")
    assert decode(F.build_tex(1024, 256, 71, mips, gdeflate=False), max_side=200).size == (256, 64)


def test_older_layouts():
    mips = F.mip_chain(4, 2, 28, rgba_mip)
    for version in (28, 143221013, 11):  # MH Rise, RE4 (40-byte header, no chunk table), DMC5 (32 bytes)
        t = Tex(F.build_tex(4, 2, 28, mips, version=version))
        assert t.version == version and t.chunks is None and t.mips == 3
        assert t.mip_bytes(1)[:8] == mips[1]


@pytest.mark.parametrize("fmt, raw, rgba", [
    (87, bytes((10, 20, 30, 40)), (30, 20, 10, 40)),     # BGRA8
    (88, bytes((10, 20, 30, 0)), (30, 20, 10, 255)),     # BGRX8: the fourth byte is not alpha
    (65, bytes((77,)), (255, 255, 255, 77)),             # A8: white with that alpha
    (49, bytes((10, 20)), (10, 20, 0, 255)),             # RG8
    (61, bytes((99,)), (99, 99, 99, 255)),               # R8 as grey
])
def test_raw_formats(fmt, raw, rgba):
    pytest.importorskip("PIL")
    im = Tex(F.build_tex(2, 2, fmt, [raw * 4], gdeflate=[True])).to_image()
    assert im.mode == "RGBA" and im.size == (2, 2) and im.getpixel((1, 1)) == rgba


def test_bad_textures():
    good = F.build_tex(8, 4, 28, F.mip_chain(8, 4, 28, rgba_mip))
    for data in (b"", b"DDS " + good[4:], good[:60], good[:-1], good + b"x",
                 good[:14] + struct.pack("<H", 0) + good[16:]):
        with pytest.raises(EquipError):
            Tex(data)
    t = Tex(good)
    with pytest.raises(EquipError):
        t.mip_bytes(9)
    odd = F.build_tex(4, 4, 28, [bytes(64)])
    odd = odd[:16] + struct.pack("<i", 10) + odd[20:]  # R16G16B16A16_FLOAT: read, not drawn
    t = Tex(odd)
    assert format_name(10) == "DXGI 10" and len(t.mip_bytes(0)) == 4 * 256
    pytest.importorskip("PIL")
    with pytest.raises(EquipError):
        t.to_image()
    broken = bytearray(F.build_tex(64, 64, 28, [bytes(range(256)) * 64]))
    start = Tex(bytes(broken)).chunks[0][0]
    broken[start + 8:start + 12] = struct.pack("<I", 1 << 30)  # the GDeflate tile table
    with pytest.raises(EquipError):
        Tex(bytes(broken)).to_image()


def test_pictures_need_pillow(monkeypatch):
    import sys

    t = Tex(F.build_tex(4, 4, 28, [bytes(64)]))
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(EquipError):
        t.to_image()
    assert icons.weapon_type_icons(lambda path: None) == {}


# ---- game UI ----------------------------------------------------------------------------
RARE = [(200, 200, 200, 255), (240, 240, 240, 255), (170, 210, 40, 255), (60, 170, 60, 255),
        (40, 200, 220, 255), (90, 110, 230, 255), (150, 90, 220, 255), (240, 130, 40, 255),
        (220, 60, 60, 255), (240, 200, 60, 255), (60, 60, 240, 255), (250, 250, 120, 255)]


def palette_file(rare=RARE):
    colors = [(i, i, i, 255) for i in range(icons.RARE_PRESET)] + list(rare) + [(1, 2, 3, 255)] * 80
    return F.build_gcp(colors)


def reader(files):
    return lambda path: files.get(path)


def test_rarity_colors():
    got = icons.rarity_colors(reader({icons.PALETTE_PATH: palette_file()}))
    assert got == {n: RARE[n - 1][:3] for n in range(1, 13)}
    assert icons.rarity_colors(reader({})) == {}
    assert icons.rarity_colors(reader({icons.PALETTE_PATH: palette_file()[:16 + 40 * 30]})) == {}
    assert icons.rarity_colors(reader({icons.PALETTE_PATH: palette_file()[:4] + b"GCPX" + palette_file()[8:]})) == {}
    assert icons.rarity_colors(reader({icons.PALETTE_PATH: palette_file([(0, 0, 0, 255)] * 12)})) == {}
    assert icons.rarity_colors(reader({icons.PALETTE_PATH: palette_file([(9, 9, 9, 255)] * 12)})) == {}

    def broken(path):
        raise OSError("pak gone")

    assert icons.rarity_colors(broken) == {}


def atlas_file(kind="good", fmt=28, gdeflate=False, others=False):
    """A 512x512 atlas (cell 25 px): glyphs of different shapes in row 16, columns 6-19 - white with
    a grey part and (RGBA only) a half-transparent black shadow, like the game's; ``others``: also in
    the cells of the armor pieces, the kinsect and the palico."""
    from PIL import Image, ImageDraw

    im = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    cell = 25
    for row in range(20):
        for col in range(20):
            if row != icons.WEAPON_ROW:
                x, y = col * cell, row * cell
                draw.rectangle((x + 8, y + 8, x + 12, y + 12), fill=(90, 40, 40, 255))
    for col in icons.WEAPON_COLUMNS:
        x, y = col * cell, icons.WEAPON_ROW * cell
        if kind == "opaque":
            draw.rectangle((x, y, x + cell - 1, y + cell - 1), fill=(255, 255, 255, 255))
        elif kind == "dark":
            draw.rectangle((x + 4, y + 4, x + 18, y + 18), fill=(30, 30, 30, 255))
        elif kind == "colour":  # bright, but not grey: the coloured tabs of another row
            draw.rectangle((x + 4, y + 4, x + 18, y + 18), fill=(240, 220, 40, 255))
        elif kind == "good" or col != 13:
            if fmt == 28:
                draw.rectangle((x + 1, y + 1, x + 23, y + 23), fill=(0, 0, 0, 120))
            draw.rectangle((x + 3, y + 4 + col % 5, x + 4 + col, y + 20), fill=(255, 255, 255, 255))
            draw.line((x + 3, y + 3, x + 21, y + 3 + col % 7), fill=(255, 255, 255, 255), width=2)
            draw.rectangle((x + 16, y + 12, x + 20, y + 16), fill=(128, 128, 128, 255))
    for n, (row, col) in enumerate(icons.OTHER_CELLS.values() if others else ()):
        x, y = col * cell, row * cell
        draw.rectangle((x + 1, y + 1, x + 23, y + 23), fill=(0, 0, 0, 120))
        draw.ellipse((x + 3, y + 3, x + 12 + n, y + 21), fill=(255, 255, 255, 255))
    if fmt == 28:
        mips = [im.tobytes()]
    else:  # BC1 keeps the shapes: one colour a block, 1-bit alpha
        px = im.load()
        mips = [F.bc1_image(512, 512, lambda x, y: px[x, y][:3] if px[x, y][3] else None)]
    return F.build_tex(512, 512, fmt, mips, gdeflate=gdeflate)


def test_icons_of_armor_pieces_kinsects_and_palicos():
    pytest.importorskip("PIL")
    got = icons.other_icons(reader({icons.ATLAS_PATH: atlas_file(others=True)}), 24)
    assert sorted(got) == sorted(icons.OTHER_CELLS)
    assert all(im.size == (24, 24) and im.getchannel("A").getbbox() for im in got.values())
    # cells that hold no glyph (here: small dark squares) are left out one by one
    assert icons.other_icons(reader({icons.ATLAS_PATH: atlas_file()}), 24) == {}
    assert icons.other_icons(reader({}), 24) == {}


def test_weapon_type_icons_and_tint():
    pytest.importorskip("PIL")
    files = {icons.ATLAS_PATH: atlas_file(), icons.PALETTE_PATH: palette_file()}
    got = icons.weapon_type_icons(reader(files), 32)
    assert sorted(got) == list(range(14))
    for im in got.values():
        assert im.mode == "RGBA" and im.size == (32, 32)
        assert im.getchannel("A").getbbox() is not None
        px = im.tobytes()
        assert {px[i:i + 3] for i in range(0, len(px), 4) if px[i + 3]} == {b"\xff\xff\xff"}
    assert len({im.tobytes() for im in got.values()}) == 14
    for im in icons.weapon_type_icons(reader(files), 64).values():
        alpha = im.getchannel("A")
        assert alpha.getpixel((0, 63)) == 0  # under the shadow only: dropped
        assert sum(alpha.histogram()[100:161]) > 64 * 64 // 50  # the grey part: half transparent
        assert sum(alpha.histogram()[33:]) < 64 * 64 * 2 // 3  # the shadow covers nearly all of it
    red = icons.tint(got[0], (200, 10, 10))
    r, g, _b, a = red.split()
    assert r.getextrema()[1] == 200 and g.getextrema()[1] == 10
    assert a.tobytes() == got[0].getchannel("A").tobytes()
    sheet = icons.icon_sheet(reader(files))
    assert sheet is not None and sheet.width >= 20 * 25


def test_cell_checks():
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    def cell(fill, shadow=None, size=40):
        im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(im)
        if shadow:
            draw.ellipse((2, 2, size - 3, size - 3), fill=shadow)
        draw.rectangle((10, 8, 22, 30), fill=fill)
        return im

    assert icons._check_cell(cell((255, 255, 255, 255))) == "ok"
    assert icons._check_cell(cell((250, 250, 250, 255), shadow=(0, 0, 0, 200))) == "ok"  # as in the game
    assert icons._check_cell(cell((0, 0, 0, 255))) == "empty"
    assert icons._check_cell(cell((90, 90, 90, 255))) == "dark"
    assert icons._check_cell(cell((250, 240, 60, 255))) == "colour"
    assert icons._check_cell(Image.new("RGBA", (40, 40), (255, 255, 255, 255))) == "opaque"
    shape = icons._shape(cell((128, 128, 128, 255), shadow=(0, 0, 0, 200)))
    assert shape.getpixel((4, 20)) == 0 and shape.getpixel((15, 15)) == 128


def test_weapon_icons_from_a_compressed_bc1_atlas():
    pytest.importorskip("PIL")
    files = {icons.ATLAS_PATH: atlas_file(fmt=71, gdeflate="stored")}
    assert sorted(icons.weapon_type_icons(reader(files), 16)) == list(range(14))


def test_weapon_icons_from_a_big_alpha_only_atlas():
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    im = Image.new("L", (4096, 3400), 0)  # no smaller mip: the 100 px grid becomes 200 px
    draw = ImageDraw.Draw(im)
    for col in icons.WEAPON_COLUMNS:
        x, y = col * 200, icons.WEAPON_ROW * 200
        draw.rectangle((x + 30, y + 40 + col * 3, x + 60 + col * 5, y + 170), fill=255)
        draw.ellipse((x + 100, y + 20, x + 150 + col, y + 90), fill=180)
    files = {icons.ATLAS_PATH: F.build_tex(4096, 3400, 65, [im.tobytes()], gdeflate=False)}  # A8
    assert sorted(icons.weapon_type_icons(reader(files), 24)) == list(range(14))


@pytest.mark.parametrize("kind", ["empty", "opaque", "dark", "colour"])
def test_weapon_icons_refuse_an_unexpected_atlas(kind):
    pytest.importorskip("PIL")
    files = {icons.ATLAS_PATH: atlas_file(kind)}
    assert icons.weapon_type_icons(reader(files), 32) == {}
    assert icons.icon_sheet(reader(files)) is not None  # the diagnostic still draws what is there


def test_weapon_icons_without_a_usable_atlas():
    pytest.importorskip("PIL")
    small = F.build_tex(64, 64, 28, [bytes(64 * 64 * 4)])
    for files in ({}, {icons.ATLAS_PATH: b"junk"}, {icons.ATLAS_PATH: small}):
        assert icons.weapon_type_icons(reader(files), 32) == {}
        assert icons.icon_sheet(reader(files)) is None


def test_a_short_atlas_still_gets_a_sheet():
    pytest.importorskip("PIL")
    files = {icons.ATLAS_PATH: F.build_tex(512, 256, 28, [bytes(512 * 256 * 4)], gdeflate=False)}  # 10 rows
    assert icons.weapon_type_icons(reader(files), 32) == {}
    assert icons.icon_sheet(reader(files)) is not None
