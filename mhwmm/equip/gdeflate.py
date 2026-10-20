"""GDeflate, the DirectStorage tile stream MH Wilds compresses its .tex chunks with, in pure Python.

Written from microsoft/DirectStorage GDeflate/GDeflate/{README.md,TileStream.h} (Apache-2.0) and
the reference decoder in NVIDIA's libdeflate fork, lib/gdeflate_decompress_template.h (libdeflate
MIT, Eric Biggers; NVIDIA's changes Apache-2.0) - an independent implementation, no code taken.

    u8 id 4, u8 id ^ 0xFF, u16 tiles, u32 bits: tile size index:2 (1 = 64 KiB),
        last tile size:18 (0 = a full tile), reserved:12
    u32[tiles] tile offsets from the end of this table; entry 0 holds the size of the LAST tile
    tile data: every tile is an independent Deflate64 stream of up to 64 KiB

Inside a tile the bits are dealt out over 32 sub-streams with their own bit buffers, which take
32-bit words from one shared cursor whenever they hold fewer than 32 bits.  Symbols are read
round-robin, one per sub-stream; every block starts again at sub-stream 0; a match reserves its
bytes when its length is read and copies them when its sub-stream comes round again (that is
when its distance is read); a stored block is a 16-bit length (no NLEN, no alignment) and bytes
read 8 bits a time round-robin.  Lengths 285-287 are 3 + 16 extra bits, distance codes 30-31
exist (libdeflate's DEFLATE64 tables).

Tiles are independent, so decompress_range() decodes only the tiles over a byte range (a band
of rows of a texture).  The main loop is written for CPython speed (about 20 MB/s of output on
3.12, 1.8 times the straightforward version): every bit buffer carries a marker bit above its
data, so a buffer under 2**32 holds fewer than 32 bits and only a refill needs bit_length() to
find where the bits end; Huffman tables resolve most length and distance extra bits in the same
lookup; copies are slice assignments.  Bad data raises GDeflateError; every symbol uses at
least one bit, so a broken stream runs out of words instead of looping.
"""

from __future__ import annotations

import struct
from itertools import cycle

from . import EquipError

ID = 4
TILE_SIZE = 0x10000
STREAMS = 32

_LOW = 1 << 32         # a buffer below this holds fewer than 32 bits (see the module docstring)
_FILL = 0xFFFFFFFF     # buffer += (word + _FILL) << bits appends a word and moves the marker up 32
_PAD = (0,) * 64       # zero words the sub-streams may load past the end of a tile
_LIT_BITS = 10         # minimum Huffman table sizes (bits): a table resolves the extra bits of a
_DIST_BITS = 10        # length or distance when they fit after the code
_SLOW = 1 << 20        # litlen value of a length whose extra bits are read separately

_LEN_BASE = (3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115,
             131, 163, 195, 227, 3, 3, 3)
_LEN_EXTRA = (0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5,
              16, 16, 16)
_DIST_BASE = (1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537,
              2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577, 32769, 49153)
_DIST_EXTRA = (0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12,
               13, 13, 14, 14)
_PRECODE_ORDER = (16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15)
_REV8 = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))

# What a table entry decodes to: an int as it is, or (extra bits, base, value when the extra bits
# do not fit in the table).  Literal/length: 0-255 a byte, 256 end of block, 256 + n a match of n
# bytes, _SLOW + i length code i with its extra bits still to read.  Distance: d, or -code (codes
# 0-3 have no extra bits, so a code read separately is always negative).
_LIT_VALUES = list(range(257)) + [(x, 256 + b, _SLOW + i) for i, (b, x) in enumerate(zip(_LEN_BASE, _LEN_EXTRA))]
_DIST_VALUES = [(x, b, -i) for i, (b, x) in enumerate(zip(_DIST_BASE, _DIST_EXTRA))]
_PRE_VALUES = list(range(19))
_static = None


class GDeflateError(EquipError):
    """A GDeflate stream that is not valid."""


def _table(lens, values, min_bits: int = 0) -> tuple[list, int]:
    """A decode table for a canonical Huffman code: table[next bits] = (bits used, value).

    Codes are read least significant bit first, so a code's entries are its bit-reversed value
    plus every pattern of the bits after it.  As in libdeflate an over-full code is an error and
    an incomplete one is allowed only when empty or a single 1-bit code (which then decodes
    every bit pattern)."""
    count = [0] * 16
    for n in lens:
        count[n] += 1
    used = 0
    for n in range(1, 16):
        used = (used << 1) + count[n]
    if used > 1 << 15:
        raise GDeflateError("over-subscribed Huffman code")
    if used == 1 << 15:
        codes = []
        code = 0
        nxt = [0] * 16
        for n in range(1, 16):
            code = (code + count[n - 1]) << 1 if n > 1 else 0
            nxt[n] = code
        for sym, n in enumerate(lens):
            if n:
                c = nxt[n]
                nxt[n] = c + 1
                codes.append((sym, n, ((_REV8[c & 255] << 8 | _REV8[c >> 8]) >> (16 - n))))
        bits = max(max(lens), min_bits)
    elif used == 0 or (used == 1 << 14 and count[1] == 1):
        sym = next((s for s, n in enumerate(lens) if n), 0)
        codes = [(sym, 1, 0), (sym, 1, 1)]
        bits = max(1, min_bits)
    else:
        raise GDeflateError("incomplete Huffman code")
    size = 1 << bits
    table: list = [None] * size
    for sym, n, rev in codes:
        v = values[sym]
        if type(v) is int:
            table[rev::1 << n] = [(n, v)] * (size >> n)
            continue
        x, base, slow = v
        if n + x > bits:
            table[rev::1 << n] = [(n, slow)] * (size >> n)
            continue
        step = 1 << (n + x)
        for k in range(1 << x):
            table[rev | k << n::step] = [(n + x, base + k)] * (size >> (n + x))
    return table, size - 1


def _static_tables():
    global _static
    if _static is None:
        _static = (*_table([8] * 144 + [9] * 112 + [7] * 24 + [8] * 8, _LIT_VALUES, _LIT_BITS),
                   *_table([5] * 32, _DIST_VALUES, _DIST_BITS))
    return _static


def _finish(out: bytearray, p: int, bb: int, dt: list, dm: int) -> int:
    """Read the distance of the match reserved as ``p`` and copy it; returns the bit buffer."""
    n, d = dt[bb & dm]
    bb >>= n
    if d < 0:
        x = _DIST_EXTRA[-d]
        d = _DIST_BASE[-d] + (bb & ((1 << x) - 1))
        bb >>= x
    length = p >> 17
    dst = p & 0x1FFFF
    s = dst - d
    if s < 0:
        raise GDeflateError("match before the start of the tile")
    if d >= length:
        out[dst:dst + length] = out[s:s + length]
    else:  # overlapping: the last d bytes repeated
        out[dst:dst + length] = (out[s:dst] * (length // d + 1))[:length]
    return bb


def _tile(src, out_size: int) -> bytearray:
    """Decode one tile of exactly ``out_size`` bytes."""
    if len(src) % 4:
        src = bytes(src) + bytes(4 - len(src) % 4)
    words = struct.unpack(f"<{len(src) // 4}I", src) + _PAD
    bufs = [w + _LOW for w in words[:STREAMS]]
    wp = STREAMS
    pend = [0] * STREAMS  # a reserved match per sub-stream: length << 17 | output position
    out = bytearray(out_size)
    op = 0
    final = 0
    while not final:
        # block header: all from sub-stream 0
        bb = bufs[0]
        final = bb & 1
        btype = bb >> 1 & 3
        bb >>= 3
        if bb < _LOW:
            bb += (words[wp] + _FILL) << (bb.bit_length() - 1)
            wp += 1
        i = 0
        if btype == 2:
            hlit = (bb & 31) + 257
            total = hlit + (bb >> 5 & 31) + 1
            hclen = (bb >> 10 & 15) + 4
            bb >>= 14
            if bb < _LOW:
                bb += (words[wp] + _FILL) << (bb.bit_length() - 1)
                wp += 1
            bufs[0] = bb
            pre = [0] * 19
            for k in range(hclen):  # one 3-bit length per sub-stream
                bb = bufs[k]
                pre[_PRECODE_ORDER[k]] = bb & 7
                bb >>= 3
                if bb < _LOW:
                    bb += (words[wp] + _FILL) << (bb.bit_length() - 1)
                    wp += 1
                bufs[k] = bb
            ptab, pmask = _table(pre, _PRE_VALUES)
            lens: list[int] = []
            while len(lens) < total:
                bb = bufs[i]
                n, sym = ptab[bb & pmask]
                bb >>= n
                if sym < 16:
                    lens.append(sym)
                elif sym == 16:
                    if not lens:
                        raise GDeflateError("repeated code length with no previous length")
                    lens += [lens[-1]] * (3 + (bb & 3))
                    bb >>= 2
                elif sym == 17:
                    lens += [0] * (3 + (bb & 7))
                    bb >>= 3
                else:
                    lens += [0] * (11 + (bb & 127))
                    bb >>= 7
                if bb < _LOW:
                    bb += (words[wp] + _FILL) << (bb.bit_length() - 1)
                    wp += 1
                bufs[i] = bb
                i = (i + 1) & 31
            lt, lm = _table(lens[:hlit], _LIT_VALUES, _LIT_BITS)
            dt, dm = _table(lens[hlit:total], _DIST_VALUES, _DIST_BITS)  # a longer run is ignored
        elif btype == 1:
            bufs[0] = bb
            lt, lm, dt, dm = _static_tables()
        elif btype == 0:
            n = bb & 0xFFFF
            bufs[0] = bb >> 16
            if op + n > out_size:
                raise GDeflateError("stored block runs past the tile")
            for _ in range(n):
                bb = bufs[i]
                out[op] = bb & 255
                op += 1
                bb >>= 8
                if bb < _LOW:
                    bb += (words[wp] + _FILL) << (bb.bit_length() - 1)
                    wp += 1
                bufs[i] = bb
                i = (i + 1) & 31
        else:
            raise GDeflateError("reserved block type")

        if btype:
            for i in cycle(range(STREAMS)):
                bb = bufs[i]
                p = pend[i]
                if p:  # the distance of this sub-stream's match, then the copy (_finish, inline)
                    n, d = dt[bb & dm]
                    bb >>= n
                    if d < 0:
                        x = _DIST_EXTRA[-d]
                        d = _DIST_BASE[-d] + (bb & ((1 << x) - 1))
                        bb >>= x
                    pend[i] = 0
                    length = p >> 17
                    dst = p & 0x1FFFF
                    s = dst - d
                    if s < 0:
                        raise GDeflateError("match before the start of the tile")
                    if d >= length:
                        out[dst:dst + length] = out[s:s + length]
                    else:  # overlapping: the last d bytes repeated
                        out[dst:dst + length] = (out[s:dst] * (length // d + 1))[:length]
                else:
                    n, v = lt[bb & lm]
                    bb >>= n
                    if v < 256:
                        out[op] = v
                        op += 1
                    elif v > 256:
                        if v >= 1048576:  # _SLOW
                            x = _LEN_EXTRA[v - 1048576]
                            v = _LEN_BASE[v - 1048576] + 256 + (bb & ((1 << x) - 1))
                            bb >>= x
                        v -= 256
                        if op + v > out_size:
                            raise GDeflateError("match runs past the tile")
                        pend[i] = v << 17 | op
                        op += v
                    else:  # end of block: this sub-stream does not advance
                        bufs[i] = bb
                        break
                if bb < 4294967296:  # _LOW
                    bb += (words[wp] + 0xFFFFFFFF) << (bb.bit_length() - 1)
                    wp += 1
                bufs[i] = bb

        # end of block: every sub-stream finishes its match and advances once, from the current one
        for k in range(STREAMS):
            j = (i + k) & 31
            bb = bufs[j]
            if pend[j]:
                bb = _finish(out, pend[j], bb, dt, dm)
                pend[j] = 0
            if bb < _LOW:
                bb += (words[wp] + _FILL) << (bb.bit_length() - 1)
                wp += 1
            bufs[j] = bb
    if op != out_size:
        raise GDeflateError(f"tile holds {op} bytes, expected {out_size}")
    return out


def _decode_tile(src, out_size: int) -> bytearray:
    try:
        return _tile(src, out_size)
    except IndexError:  # the words ran out (or a literal ran past the tile)
        raise GDeflateError("truncated or corrupt tile") from None
    except (TypeError, ValueError, struct.error) as exc:
        raise GDeflateError(f"corrupt tile: {exc}") from None


def is_gdeflate(data) -> bool:
    """Whether ``data`` starts like a GDeflate tile stream (04 FB)."""
    return len(data) >= 8 and data[0] == ID and data[1] == ID ^ 0xFF


def _layout(data) -> tuple[list[tuple[int, int, int]], int]:
    """[(start, end, decoded size)] of every tile, and the decoded size of the whole stream."""
    if not is_gdeflate(data):
        raise GDeflateError("not a GDeflate stream")
    tiles, bits = struct.unpack_from("<HI", data, 2)
    last = bits >> 2 & 0x3FFFF
    if bits & 3 != 1 or last > TILE_SIZE or (not tiles and last):
        raise GDeflateError("unsupported GDeflate header")
    base = 8 + 4 * tiles
    if base > len(data):
        raise GDeflateError("truncated GDeflate tile table")
    offsets = struct.unpack_from(f"<{tiles}I", data, 8)
    room = len(data) - base
    spans = []
    for t in range(tiles):
        start = offsets[t] if t else 0
        end = offsets[t + 1] if t + 1 < tiles else start + offsets[0]
        if not start <= end <= room:
            raise GDeflateError("bad GDeflate tile offsets")
        spans.append((base + start, base + end, last if t + 1 == tiles and last else TILE_SIZE))
    return spans, sum(s[2] for s in spans)


def uncompressed_size(data) -> int:
    """The decoded size the header gives (nothing is decoded)."""
    return _layout(data)[1]


def decompress(data, size: int | None = None) -> bytes:
    """The whole stream; ``size`` (if given) must be its decoded size, checked before decoding."""
    spans, total = _layout(data)
    if size is not None and size != total:
        raise GDeflateError(f"GDeflate stream holds {total} bytes, expected {size}")
    return b"".join(_decode_tile(data[s:e], n) for s, e, n in spans)


def decompress_range(data, start: int, end: int) -> tuple[int, bytes]:
    """Decode only the 64 KiB tiles over decoded bytes [start, end) (clamped to the stream).

    Returns (decoded offset of the first returned byte, bytes); the bytes cover whole tiles."""
    spans, total = _layout(data)
    start, end = max(0, start), min(end, total)
    if start >= end:
        return min(start, total), b""
    first, last = start // TILE_SIZE, (end - 1) // TILE_SIZE
    return first * TILE_SIZE, b"".join(_decode_tile(data[s:e], n) for s, e, n in spans[first:last + 1])
