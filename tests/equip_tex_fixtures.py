"""Synthetic textures for the tests: a small GDeflate encoder, .tex and colorPreset.gcp builders.

Nothing here comes from the game.  The encoder mirrors the decoder's control flow: every read of
bits is recorded on its sub-stream and every point where a sub-stream may load a word; a second
pass writes each sub-stream's bits as 32-bit words in the order the decoder will load them.
"""

from __future__ import annotations

import heapq
import struct

from mhwmm.equip import gdeflate, tex

N = gdeflate.STREAMS
TILE = gdeflate.TILE_SIZE
_LEN_BASE = gdeflate._LEN_BASE[:28]
_LEN_EXTRA = gdeflate._LEN_EXTRA[:28]
_DIST_BASE = gdeflate._DIST_BASE
_DIST_EXTRA = gdeflate._DIST_EXTRA
_PRECODE_ORDER = gdeflate._PRECODE_ORDER


def _rev(code: int, n: int) -> int:
    return int(format(code, f"0{n}b")[::-1], 2) if n else 0


def _len_sym(length: int) -> tuple[int, int, int]:
    """(litlen symbol, extra value, extra bits); lengths over 258 use Deflate64's symbol 285."""
    if length > 258:
        return 285, length - 3, 16
    i = max(k for k, base in enumerate(_LEN_BASE) if base <= length)
    return 257 + i, length - _LEN_BASE[i], _LEN_EXTRA[i]


def _dist_sym(dist: int) -> tuple[int, int, int]:
    i = max(k for k, base in enumerate(_DIST_BASE) if base <= dist)
    return i, dist - _DIST_BASE[i], _DIST_EXTRA[i]


def huffman_lengths(freqs: dict[int, int], limit: int = 15) -> dict[int, int]:
    """Code lengths of a Huffman code for the used symbols (one symbol: length 1); counts are
    flattened until no code is longer than ``limit``."""
    used = [(f, s) for s, f in freqs.items() if f]
    if len(used) == 1:
        return {used[0][1]: 1}
    while True:
        heap = [(f, k, (s,)) for k, (f, s) in enumerate(sorted(used))]
        heapq.heapify(heap)
        depth = {s: 0 for _, s in used}
        k = len(heap)
        while len(heap) > 1:
            f1, _, a = heapq.heappop(heap)
            f2, _, b = heapq.heappop(heap)
            for s in a + b:
                depth[s] += 1
            heapq.heappush(heap, (f1 + f2, k, a + b))
            k += 1
        if max(depth.values()) <= limit:
            return depth
        used = [(f >> 1 | 1, s) for f, s in used]


def canonical(lengths: list[int]) -> dict[int, tuple[int, int]]:
    """symbol -> (bit-reversed code, length) for a list of code lengths."""
    count = [0] * 16
    for n in lengths:
        count[n] += 1
    count[0] = 0
    code, nxt = 0, [0] * 16
    for n in range(1, 16):
        code = (code + count[n - 1]) << 1
        nxt[n] = code
    out = {}
    for sym, n in enumerate(lengths):
        if n:
            out[sym] = (_rev(nxt[n], n), n)
            nxt[n] += 1
    return out


STATIC_LIT = canonical([8] * 144 + [9] * 112 + [7] * 24 + [8] * 8)
STATIC_DIST = canonical([5] * 32)


class _Streams:
    def __init__(self):
        self.events: list[tuple[int, int]] = []  # (stream, bits read) or (stream, -1) = may load a word
        self.chunks: list[list[tuple[int, int]]] = [[] for _ in range(N)]

    def put(self, s: int, value: int, n: int) -> None:
        if n:
            self.events.append((s, n))
            self.chunks[s].append((value & ((1 << n) - 1), n))

    def ensure(self, s: int) -> None:
        self.events.append((s, -1))

    def serialise(self, pad_words: int = N) -> bytes:
        words: list[list[int]] = []
        for chunks in self.chunks:  # every sub-stream's bits as 32-bit words
            acc = nbits = 0
            ws = []
            for value, n in chunks:
                acc |= value << nbits
                nbits += n
                while nbits >= 32:
                    ws.append(acc & 0xFFFFFFFF)
                    acc >>= 32
                    nbits -= 32
            ws.append(acc)
            words.append(ws)
        taken, left, out = [0] * N, [0] * N, []

        def load(s: int) -> None:
            ws = words[s]
            out.append(ws[taken[s]] if taken[s] < len(ws) else 0)
            taken[s] += 1
            left[s] += 32

        for s in range(N):
            load(s)
        for s, n in self.events:
            if n >= 0:
                left[s] -= n
                assert left[s] >= 0, "read more bits than buffered"
            elif left[s] < 32:
                load(s)
        return struct.pack(f"<{len(out)}I", *out) + bytes(4 * pad_words)


def _put_dist(st: _Streams, s: int, dist: int, table) -> None:
    sym, extra, nx = _dist_sym(dist)
    code, n = table[sym]
    st.put(s, code, n)
    st.put(s, extra, nx)


def _huffman_body(st: _Streams, tokens, lit, dist) -> None:
    """The symbols of a static or dynamic block, round-robin with deferred distances."""
    pend: list[int | None] = [None] * N
    i = t = 0
    while True:
        if pend[i] is not None:
            _put_dist(st, i, pend[i], dist)
            pend[i] = None
        else:
            if t == len(tokens):
                st.put(i, *lit[256])
                break
            tok = tokens[t]
            t += 1
            if isinstance(tok, int):
                st.put(i, *lit[tok])
            else:
                length, d = tok
                sym, extra, nx = _len_sym(length)
                st.put(i, *lit[sym])
                st.put(i, extra, nx)
                pend[i] = d
        st.ensure(i)
        i = (i + 1) % N
    for _ in range(N):  # end of block: the pending distances, one advance each
        if pend[i] is not None:
            _put_dist(st, i, pend[i], dist)
            pend[i] = None
        st.ensure(i)
        i = (i + 1) % N


def _rle_lengths(lens: list[int]) -> list[tuple[int, int, int]]:
    """Code lengths as precode symbols (symbol, extra value, extra bits)."""
    out, i = [], 0
    while i < len(lens):
        v = lens[i]
        run = 1
        while i + run < len(lens) and lens[i + run] == v:
            run += 1
        if v == 0 and run >= 11:
            k = min(run, 138)
            out.append((18, k - 11, 7))
        elif v == 0 and run >= 3:
            k = min(run, 10)
            out.append((17, k - 3, 3))
        elif v and run >= 4:
            out.append((v, 0, 0))
            k = 1 + min(run - 1, 6)
            out.append((16, k - 4, 2))
        else:
            out.append((v, 0, 0))
            k = 1
        i += k
    return out


def _dynamic(st: _Streams, tokens) -> None:
    lfreq: dict[int, int] = {256: 1}
    dfreq: dict[int, int] = {}
    for tok in tokens:
        if isinstance(tok, int):
            lfreq[tok] = lfreq.get(tok, 0) + 1
        else:
            sym = _len_sym(tok[0])[0]
            lfreq[sym] = lfreq.get(sym, 0) + 1
            d = _dist_sym(tok[1])[0]
            dfreq[d] = dfreq.get(d, 0) + 1
    ll = huffman_lengths(lfreq)
    dl = huffman_lengths(dfreq) if dfreq else {0: 1}
    hlit = max(257, max(ll) + 1)
    hdist = max(1, max(dl) + 1)
    lens = [ll.get(s, 0) for s in range(hlit)] + [dl.get(s, 0) for s in range(hdist)]
    rle = _rle_lengths(lens)
    pfreq: dict[int, int] = {}
    for sym, _, _ in rle:
        pfreq[sym] = pfreq.get(sym, 0) + 1
    pl = huffman_lengths(pfreq, 7)
    order = [pl.get(s, 0) for s in _PRECODE_ORDER]
    hclen = max(4, max(k + 1 for k, n in enumerate(order) if n))
    st.put(0, (hlit - 257) | (hdist - 1) << 5 | (hclen - 4) << 10, 14)
    st.ensure(0)
    for k in range(hclen):
        st.put(k, order[k], 3)
        st.ensure(k)
    pre = canonical([pl.get(s, 0) for s in range(19)])
    i = 0
    for sym, extra, nx in rle:
        st.put(i, *pre[sym])
        st.put(i, extra, nx)
        st.ensure(i)
        i = (i + 1) % N
    _huffman_body(st, tokens, canonical(lens[:hlit]), canonical(lens[hlit:]))


def encode_tile(blocks) -> bytes:
    """One tile from blocks: ("stored", bytes), or ("static" | "dynamic", tokens) where a token
    is a literal byte or (length, distance)."""
    st = _Streams()
    for k, (kind, payload) in enumerate(blocks):
        final = 1 if k == len(blocks) - 1 else 0
        st.put(0, final | {"stored": 0, "static": 1, "dynamic": 2}[kind] << 1, 3)
        st.ensure(0)
        if kind == "stored":
            st.put(0, len(payload), 16)
            i = 0
            for byte in payload:
                st.put(i, byte, 8)
                st.ensure(i)
                i = (i + 1) % N
            for _ in range(N):
                st.ensure(i)
                i = (i + 1) % N
        elif kind == "static":
            _huffman_body(st, payload, STATIC_LIT, STATIC_DIST)
        else:
            _dynamic(st, payload)
    return st.serialise()


def expand(tokens) -> bytes:
    """What a token list decodes to."""
    out = bytearray()
    for tok in tokens:
        if isinstance(tok, int):
            out.append(tok)
        else:
            length, d = tok
            for _ in range(length):
                out.append(out[-d])
    return bytes(out)


def tokenize(data: bytes, max_len: int = 258) -> list:
    """Greedy LZ77 with a 3-byte hash (enough to give the decoder real matches)."""
    tokens: list = []
    last: dict[bytes, int] = {}
    i = 0
    while i < len(data):
        key = data[i:i + 3]
        j = last.get(key)
        last[key] = i
        if j is not None and len(key) == 3:
            n = 3
            while n < max_len and i + n < len(data) and data[j + n] == data[i + n]:
                n += 1
            tokens.append((n, i - j))
            i += n
        else:
            tokens.append(data[i])
            i += 1
    return tokens


def tile_stream(tiles: list[bytes], total_size: int) -> bytes:
    """Encoded tiles in the TileStream header and tile offset table."""
    n = len(tiles)
    last = total_size - (n - 1) * TILE if n else 0
    last = 0 if last == TILE else last
    offsets, pos = [], 0
    for t in tiles:
        offsets.append(pos)
        pos += len(t)
    if n:
        offsets[0] = len(tiles[-1])  # entry 0 holds the size of the LAST tile
    return struct.pack("<BBHI", 4, 0xFB, n, 1 | last << 2) + struct.pack(f"<{n}I", *offsets) + b"".join(tiles)


def compress(data: bytes, kind: str = "static") -> bytes:
    """``data`` as a GDeflate stream of one block per tile ("stored", "static" or "dynamic")."""
    tiles = []
    for pos in range(0, len(data), TILE):
        part = data[pos:pos + TILE]
        if kind == "stored":  # a stored block holds at most 65535 bytes
            blocks = [("stored", part[k:k + 0x8000]) for k in range(0, len(part), 0x8000)] or [("stored", b"")]
        else:
            blocks = [(kind, tokenize(part))]
        tiles.append(encode_tile(blocks))
    return tile_stream(tiles, len(data))


# ---- textures ---------------------------------------------------------------------------
def row_bytes(fmt: int, width: int) -> tuple[int, int]:
    """(bytes of one row of blocks or pixels, pixel rows in it) of a format."""
    if fmt in tex._BC:
        return (width + 3) // 4 * tex._BC[fmt][1], 4
    return width * tex._RAW[fmt][1], 1


def build_tex(width: int, height: int, fmt: int, mips_data: list[bytes], gdeflate=True, align: int = 256,
              version: int = tex.MHWILDS_VERSION, images: int = 1) -> bytes:
    """A .tex file.  ``mips_data`` holds every mip (image-major) with tightly packed rows; rows are
    padded to a multiple of ``align``.  ``gdeflate``: True compresses every mip ("static" blocks), a
    block kind name picks it, a list gives one choice per mip (False = stored as it is).  Versions
    without the stored-chunk table (28, 30 ...) and the 32-byte header ones (8, 10, 11) are written
    in their own layout."""
    mips = len(mips_data) // images
    table, chunks = [], []
    for i, raw in enumerate(mips_data):
        level = i % mips
        w, h = max(1, width >> level), max(1, height >> level)
        row, step = row_bytes(fmt, w)
        rows = (h + step - 1) // step
        assert len(raw) == row * rows, (i, len(raw), row * rows)
        pitch = (row + align - 1) // align * align
        body = b"".join(raw[r * row:(r + 1) * row].ljust(pitch, b"\0") for r in range(rows))
        table.append((pitch, len(body), body))
    legacy = version in tex._LEGACY_VERSIONS
    head = 32 if legacy else 40
    n = len(mips_data)
    first = head + 16 * n
    stored = version >= tex.MHWILDS_VERSION and version not in (251211553, 760230703)
    choice = gdeflate if isinstance(gdeflate, list) else [gdeflate] * n
    entries, offset = [], first
    for pitch, size, _body in table:
        entries.append(struct.pack("<QII", offset, pitch, size))
        offset += size
    if legacy:
        header = struct.pack("<4siHHHBBiiII", b"TEX\0", version, width, height, 1, mips, images, fmt, -1, 0, 0)
    else:
        header = struct.pack("<4siHHHHiiII8x", b"TEX\0", version, width, height, 1, images | mips << 12, fmt, -1,
                             0, 0)
    if not stored:
        return header + b"".join(entries) + b"".join(body for _p, _s, body in table)
    for (_pitch, _size, body), how in zip(table, choice):
        if how:
            chunks.append(compress(body, "static" if how is True else how))
        else:
            chunks.append(body)
    pos, refs = 0, []
    for c in chunks:
        refs.append(struct.pack("<II", len(c), pos))
        pos += len(c)
    return header + b"".join(entries) + b"".join(refs) + b"".join(chunks)


def mip_chain(width: int, height: int, fmt: int, make) -> list[bytes]:
    """Data for every mip down to 1x1: make(level, w, h) -> tightly packed bytes."""
    out, level = [], 0
    while True:
        w, h = max(1, width >> level), max(1, height >> level)
        out.append(make(level, w, h))
        if w == 1 and h == 1:
            return out
        level += 1


def rgb565(r: int, g: int, b: int) -> int:
    return (r >> 3) << 11 | (g >> 2) << 5 | b >> 3


def bc1_block(rgb=None, mask: int = 0xFFFF) -> bytes:
    """A BC1 block: ``rgb`` where ``mask`` (bit = pixel y * 4 + x) is set, transparent elsewhere.
    Use colours whose low bits survive RGB565 (multiples of 8, green of 4) to compare exactly."""
    c = rgb565(*rgb) if rgb else 0
    idx = 0
    for p in range(16):
        idx |= (0 if mask >> p & 1 and rgb else 3) << (2 * p)
    return struct.pack("<HHI", c, c, idx)  # color0 <= color1: index 3 is transparent black


def bc1_image(width: int, height: int, pixel) -> bytes:
    """BC1 data for a picture of one colour per block: pixel(x, y) -> (r, g, b) or None (clear)."""
    out = bytearray()
    for by in range(0, height, 4):
        for bx in range(0, width, 4):
            rgb, mask = None, 0
            for p in range(16):
                x, y = bx + p % 4, by + p // 4
                c = pixel(x, y) if x < width and y < height else None
                if c is not None:
                    rgb = rgb or c
                    mask |= 1 << p
            out += bc1_block(rgb, mask)
    return bytes(out)


def bc7_solid(r: int, g: int, b: int, a: int = 255) -> bytes:
    """A BC7 mode 6 block of one colour (channels are rounded down to even values)."""
    bits = [(0x40, 7)]  # mode 6
    for c in (r, g, b, a):
        bits += [(c >> 1, 7), (c >> 1, 7)]
    bits += [(0, 1), (0, 1), (0, 3)] + [(0, 4)] * 15  # p-bits, then indices (anchor 3 bits)
    value = pos = 0
    for v, n in bits:
        value |= v << pos
        pos += n
    assert pos == 128
    return value.to_bytes(16, "little")


def build_gcp(colors: list[tuple[int, int, int, int]], version: int = 2) -> bytes:
    """colorPreset.gcp: entry i holds colors[i] in all four slots."""
    body = b"".join(bytes(c) * 4 + bytes(8) + bytes(16) for c in colors)
    return struct.pack("<I4sII", version, b"GCPR", len(colors), 0) + body
