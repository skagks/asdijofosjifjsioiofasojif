"""RE Engine textures (.tex), read only: MH Wilds (.tex.241106027) and the older layouts.

Layout as in REasy by Battlezone (MIT, file_handlers/tex); this is an independent reader:

    0x00 "TEX\\0", i32 version, u16 width, height, depth
    versions 8, 10, 11, 190820018 (32-byte header): u8 mips, u8 images, i32 format, i32 -1,
        u32 cubemap, u32 flags
    the others (40-byte header): u16 images | mips << 12, i32 format, i32 -1, u32 cubemap (0 or 4),
        u32 flags, 8 bytes reserved
    mip table, image-major: images * mips x (u64 offset, u32 row pitch, u32 size of one depth slice)
    MH Wilds and later: then images * mips x (u32 stored size, u32 offset from the end of this
        table) - each chunk is the mip as it is (stored size = its size) or a GDeflate stream
        (gdeflate.py), and the mip table's offsets are those the data would have uncompressed

The format is a DXGI_FORMAT value.  Every row is padded to the pitch (BC formats to a multiple of
256 bytes).  Pixels are decoded by Pillow (its "bcn" decoder for BC1-BC7), imported only when a
picture is made - Pillow is optional, without it to_image() raises EquipError.
"""

from __future__ import annotations

import struct

from . import EquipError, gdeflate

MAGIC = b"TEX\x00"
MHWILDS_VERSION = 241106027
MAX_SIDE = 16384
_LEGACY_VERSIONS = {8, 10, 11, 190820018}  # RE7, RE2, DMC5, RE3: the 32-byte header

# DXGI format -> (name, bytes per 4x4 block, Pillow bcn decoder, its pixel format, Pillow mode)
_BC = {
    70: ("BC1", 8, 1, "BC1", "RGBA"), 71: ("BC1", 8, 1, "BC1", "RGBA"), 72: ("BC1 sRGB", 8, 1, "BC1", "RGBA"),
    73: ("BC2", 16, 2, "BC2", "RGBA"), 74: ("BC2", 16, 2, "BC2", "RGBA"), 75: ("BC2 sRGB", 16, 2, "BC2", "RGBA"),
    76: ("BC3", 16, 3, "BC3", "RGBA"), 77: ("BC3", 16, 3, "BC3", "RGBA"), 78: ("BC3 sRGB", 16, 3, "BC3", "RGBA"),
    79: ("BC4", 8, 4, "BC4", "L"), 80: ("BC4", 8, 4, "BC4", "L"),
    82: ("BC5", 16, 5, "BC5", "RGB"), 83: ("BC5", 16, 5, "BC5", "RGB"), 84: ("BC5 signed", 16, 5, "BC5S", "RGB"),
    94: ("BC6H", 16, 6, "BC6H", "RGB"), 95: ("BC6H", 16, 6, "BC6H", "RGB"), 96: ("BC6H signed", 16, 6, "BC6HS", "RGB"),
    97: ("BC7", 16, 7, "BC7", "RGBA"), 98: ("BC7", 16, 7, "BC7", "RGBA"), 99: ("BC7 sRGB", 16, 7, "BC7", "RGBA"),
}
# DXGI format -> (name, bytes per pixel, Pillow mode, raw mode); "A" is an alpha mask, "RG" two channels
_RAW = {
    27: ("RGBA8", 4, "RGBA", "RGBA"), 28: ("RGBA8", 4, "RGBA", "RGBA"), 29: ("RGBA8 sRGB", 4, "RGBA", "RGBA"),
    87: ("BGRA8", 4, "RGBA", "BGRA"), 90: ("BGRA8", 4, "RGBA", "BGRA"), 91: ("BGRA8 sRGB", 4, "RGBA", "BGRA"),
    88: ("BGRX8", 4, "RGB", "BGRX"), 92: ("BGRX8", 4, "RGB", "BGRX"), 93: ("BGRX8 sRGB", 4, "RGB", "BGRX"),
    60: ("R8", 1, "L", "L"), 61: ("R8", 1, "L", "L"), 65: ("A8", 1, "A", "L"),
    48: ("RG8", 2, "RG", "RG"), 49: ("RG8", 2, "RG", "RG"),
}


def _pil():
    try:
        from PIL import Image
    except ImportError:
        raise EquipError("그림을 읽으려면 Pillow가 필요합니다") from None
    return Image


def format_name(fmt: int) -> str:
    info = _BC.get(fmt) or _RAW.get(fmt)
    return info[0] if info else f"DXGI {fmt}"


def is_supported(fmt: int) -> bool:
    return fmt in _BC or fmt in _RAW


class Tex:
    """One .tex file; mips are read and decoded on demand."""

    def __init__(self, data: bytes):
        try:
            self._parse(bytes(data))
        except (struct.error, IndexError, ValueError, OverflowError) as exc:
            raise EquipError(f"텍스처 파일을 읽지 못했습니다: {exc}") from None

    def _parse(self, data: bytes) -> None:
        if len(data) < 32 or data[:4] != MAGIC:
            raise ValueError("not a .tex file")
        self.data = data
        self.version, self.width, self.height, self.depth = struct.unpack_from("<iHHH", data, 4)
        if self.version in _LEGACY_VERSIONS:
            self.mips, self.images = data[14], data[15]
            self.format, _swizzle, self.cubemap, self.flags = struct.unpack_from("<iiII", data, 16)
            table = 32
        else:
            packed, self.format, _swizzle, self.cubemap, self.flags = struct.unpack_from("<HiiII", data, 14)
            self.images, self.mips = packed & 0xFFF, packed >> 12
            table = 40
        if not (0 < self.width <= MAX_SIDE and 0 < self.height <= MAX_SIDE and self.depth > 0):
            raise ValueError(f"size {self.width}x{self.height}x{self.depth}")
        n = self.images * self.mips
        if not n:
            raise ValueError("no mips")
        self.mip_table = [struct.unpack_from("<QII", data, table + 16 * i) for i in range(n)]
        end = table + 16 * n
        spans = [self.span(i) for i in range(n)]
        self.chunks = self._chunk_table(data, end, spans)
        if self.chunks is None:  # stored as it is, at the offsets of the mip table
            pos = end
            for i, (offset, _pitch, _size) in enumerate(self.mip_table):
                if offset != pos:
                    raise ValueError(f"mip {i} is not where the table says")
                pos += spans[i]
            if pos != len(data):
                raise ValueError("the mip table does not account for the file")

    @staticmethod
    def _chunk_table(data: bytes, end: int, spans: list[int]) -> list[tuple[int, int]] | None:
        """(file offset, stored size) of every mip from the stored-chunk table after the mip table
        (MH Wilds and later); None when there is no such table."""
        payload = end + 8 * len(spans)
        if payload > len(data):
            return None
        chunks = []
        pos = 0
        for i, span in enumerate(spans):
            size, offset = struct.unpack_from("<II", data, end + 8 * i)
            if offset != pos or not size:
                return None
            if size != span and not gdeflate.is_gdeflate(data[payload + offset:payload + offset + 8]):
                return None  # neither the mip as it is nor GDeflate
            chunks.append((payload + offset, size))
            pos += size
        return chunks if payload + pos == len(data) else None

    # ---- layout ------------------------------------------------------------------------
    @property
    def format_name(self) -> str:
        return format_name(self.format)

    def index(self, level: int, image: int = 0) -> int:
        if not (0 <= level < self.mips and 0 <= image < self.images):
            raise EquipError(f"텍스처에 없는 mip입니다: {level}/{image}")
        return image * self.mips + level

    def mip_size(self, level: int) -> tuple[int, int]:
        return max(1, self.width >> level), max(1, self.height >> level)

    def span(self, i: int) -> int:
        """Bytes of mip table entry i (every depth slice)."""
        return self.mip_table[i][2] * max(1, self.depth >> (i % self.mips))

    def best_level(self, max_side: int) -> int:
        """The smallest mip whose longer side is still at least ``max_side`` (or mip 0)."""
        for level in range(self.mips - 1, -1, -1):
            if max(self.mip_size(level)) >= max_side:
                return level
        return 0

    def _rows(self, level: int, pitch: int) -> tuple[int, int, int]:
        """(row bytes without padding, pixel rows per row, rows) of a mip, checked against its pitch."""
        w, h = self.mip_size(level)
        if self.format in _BC:
            row, step = (w + 3) // 4 * _BC[self.format][1], 4
        elif self.format in _RAW:
            row, step = w * _RAW[self.format][1], 1
        else:
            raise EquipError(f"지원하지 않는 텍스처 형식입니다 (DXGI {self.format})")
        if not row <= pitch <= row + 4096:
            raise EquipError(f"텍스처의 행 크기가 맞지 않습니다 ({pitch}, {row})")
        return row, step, (h + step - 1) // step

    # ---- data --------------------------------------------------------------------------
    def mip_bytes(self, level: int = 0, image: int = 0) -> bytes:
        """One mip as stored uncompressed: every slice, rows padded to the pitch."""
        i = self.index(level, image)
        return self._read(i, 0, self.span(i))[1]

    def _read(self, i: int, start: int, end: int) -> tuple[int, bytes]:
        """Bytes [start, end) of mip table entry i, or more: (offset of the returned bytes, bytes)."""
        span = self.span(i)
        if self.chunks is None:
            offset = self.mip_table[i][0]
            return start, self.data[offset + start:offset + min(end, span)]
        pos, size = self.chunks[i]
        chunk = self.data[pos:pos + size]
        if size == span:
            return start, chunk[start:end]
        try:
            total = gdeflate.uncompressed_size(chunk)
            if not span <= total <= span + gdeflate.TILE_SIZE:
                raise gdeflate.GDeflateError(f"GDeflate chunk holds {total} bytes, the mip {span}")
            if start <= 0 and end >= span:
                return 0, gdeflate.decompress(chunk)[:span]
            return gdeflate.decompress_range(chunk, start, min(end, span))
        except gdeflate.GDeflateError as exc:
            raise EquipError(f"텍스처 압축을 풀지 못했습니다: {exc}") from None

    # ---- pictures ----------------------------------------------------------------------
    def to_image(self, level: int = 0, image: int = 0):
        """One mip (its first depth slice) as an RGBA PIL image."""
        return self.band(0, self.mip_size(level)[1], level, image)

    def band(self, y0: int, y1: int, level: int = 0, image: int = 0):
        """Pixel rows [y0, y1) of a mip, full width, as an RGBA PIL image.  Only the GDeflate tiles
        over those rows are decoded, so a strip of a big atlas costs a fraction of the whole."""
        Image = _pil()
        i = self.index(level, image)
        w, h = self.mip_size(level)
        y0, y1 = max(0, y0), min(h, y1)
        if y0 >= y1:
            raise EquipError("빈 영역입니다")
        pitch, size = self.mip_table[i][1:]
        row, step, rows = self._rows(level, pitch)
        if not (rows - 1) * pitch + row <= size <= rows * pitch:
            raise EquipError(f"텍스처 크기가 맞지 않습니다 ({size}, {rows} x {pitch})")
        r0, r1 = y0 // step, (y1 + step - 1) // step
        start, end = r0 * pitch, min(r1 * pitch, size)
        base, raw = self._read(i, start, end)
        raw = raw[start - base:end - base]
        if pitch != row:
            raw = b"".join(raw[k * pitch:k * pitch + row] for k in range(r1 - r0))
        if len(raw) < (r1 - r0) * row:
            raise EquipError("텍스처 데이터가 모자랍니다")
        size_px = (w, (r1 - r0) * step)
        try:
            if self.format in _BC:
                _name, _bs, n, pixel_format, mode = _BC[self.format]
                im = Image.frombytes(mode, size_px, raw, "bcn", n, pixel_format)
            else:
                _name, _bpp, mode, rawmode = _RAW[self.format]
                if mode == "A":  # an alpha mask: white with that alpha
                    alpha = Image.frombytes("L", size_px, raw, "raw", "L")
                    im = Image.new("RGBA", size_px, (255, 255, 255, 255))
                    im.putalpha(alpha)
                elif mode == "RG":
                    pair = Image.frombytes("LA", size_px, raw, "raw", "LA")
                    red, green = pair.split()
                    im = Image.merge("RGB", (red, green, Image.new("L", size_px, 0)))
                else:
                    im = Image.frombytes(mode, size_px, raw, "raw", rawmode)
        except (ValueError, OSError) as exc:
            raise EquipError(f"텍스처를 그림으로 바꾸지 못했습니다: {exc}") from None
        top = y0 - r0 * step
        if top or im.height != y1 - y0:
            im = im.crop((0, top, w, top + y1 - y0))
        return im if im.mode == "RGBA" else im.convert("RGBA")


def decode(data: bytes, max_side: int | None = None):
    """A .tex file as an RGBA PIL image: mip 0, or with ``max_side`` the smallest mip whose longer
    side is at least that (the caller shrinks it the rest of the way)."""
    tex = Tex(data)
    return tex.to_image(tex.best_level(max_side) if max_side else 0)


def crop_band(data: bytes, y0: int, y1: int, level: int = 0):
    """Pixel rows [y0, y1) of one mip of a .tex file (see Tex.band)."""
    return Tex(data).band(y0, y1, level)
