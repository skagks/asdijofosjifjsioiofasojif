"""RE Engine .pak (KPKA) archives: the game's own (encrypted entry table, sometimes
chunked) and the small unencrypted ones mods ship.  Only the header and the entry
table are read up front; single entries are read and decompressed on demand, so even
the 40 GB re_chunk_000.pak costs a few seconds once.

Format as in MHWmodfixer's pak_reader/pak_writer (ported from Ekey's REE.PAK.Tool):
entries are keyed by Murmur3 hashes of the upper- and lower-case UTF-16 path; the
entry table of the game's paks is XORed with a key that an RSA public operation
unwraps.  The XOR keystream repeats every 7424 bytes, which lets a whole table be
decrypted in one big-integer XOR instead of a Python loop over every byte.
"""

from __future__ import annotations

import struct
import zlib
from array import array
from pathlib import Path

from . import MatfixError, zstd_compress, zstd_decompress, zstd_start

MAGIC = 0x414B504B  # "KPKA"

COMPRESSION_NONE = 0
COMPRESSION_DEFLATE = 1
COMPRESSION_ZSTD = 2

FEATURE_ENCRYPTED_RESOURCES = 8
FEATURE_DLC_EXTRA_DATA1 = 12
FEATURE_EXTRA_DATA = 24
FEATURE_CHUNKED_RESOURCES = 40
FEATURE_DLC_EXTRA_DATA2 = 44
_ENCRYPTED_TABLE = {FEATURE_ENCRYPTED_RESOURCES, FEATURE_DLC_EXTRA_DATA1, FEATURE_EXTRA_DATA,
                    FEATURE_CHUNKED_RESOURCES, FEATURE_DLC_EXTRA_DATA2}
_CHUNKED = {FEATURE_CHUNKED_RESOURCES, FEATURE_DLC_EXTRA_DATA2}
_CHUNKED_ATTRIBUTES = (0x1000000, 0x1000400)
MAX_CHUNK = 524288
HEAD_INPUT = 192 * 1024


# ---- path hashes ----------------------------------------------------------------
def _rotl(x: int, r: int) -> int:
    return ((x << r) | (x >> (32 - r))) & 0xFFFFFFFF


def murmur3_32(data: bytes, seed: int = 0xFFFFFFFF) -> int:
    c1, c2 = 0xCC9E2D51, 0x1B873593
    h = seed
    n = len(data) // 4 * 4
    for (k,) in struct.iter_unpack("<I", data[:n]):
        k = _rotl((k * c1) & 0xFFFFFFFF, 15) * c2 & 0xFFFFFFFF
        h = (_rotl(h ^ k, 13) * 5 + 0xE6546B64) & 0xFFFFFFFF
    tail = data[n:]
    if tail:
        k = int.from_bytes(tail, "little")
        h ^= _rotl((k * c1) & 0xFFFFFFFF, 15) * c2 & 0xFFFFFFFF
    h ^= len(data)
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    return h ^ (h >> 16)


def string_hash(text: str) -> int:
    return murmur3_32(text.encode("utf-16-le"))


def path_hash(path: str) -> int:
    """The 64-bit key a pak stores a file under: hash(upper) << 32 | hash(lower)."""
    return (string_hash(path.upper()) << 32) | string_hash(path.lower())


# ---- entry table encryption -----------------------------------------------------
_MODULUS = int.from_bytes(bytes([
    0x7D, 0x0B, 0xF8, 0xC1, 0x7C, 0x23, 0xFD, 0x3B, 0xD4, 0x75, 0x16, 0xD2, 0x33, 0x21, 0xD8, 0x10,
    0x71, 0xF9, 0x7C, 0xD1, 0x34, 0x93, 0xBA, 0x77, 0x26, 0xFC, 0xAB, 0x2C, 0xEE, 0xDA, 0xD9, 0x1C,
    0x89, 0xE7, 0x29, 0x7B, 0xDD, 0x8A, 0xAE, 0x50, 0x39, 0xB6, 0x01, 0x6D, 0x21, 0x89, 0x5D, 0xA5,
    0xA1, 0x3E, 0xA2, 0xC0, 0x8C, 0x93, 0x13, 0x36, 0x65, 0xEB, 0xE8, 0xDF, 0x06, 0x17, 0x67, 0x96,
    0x06, 0x2B, 0xAC, 0x23, 0xED, 0x8C, 0xB7, 0x8B, 0x90, 0xAD, 0xEA, 0x71, 0xC4, 0x40, 0x44, 0x9D,
    0x1C, 0x7B, 0xBA, 0xC4, 0xB6, 0x2D, 0xD6, 0xD2, 0x4B, 0x62, 0xD6, 0x26, 0xFC, 0x74, 0x20, 0x07,
    0xEC, 0xE3, 0x59, 0x9A, 0xE6, 0xAF, 0xB9, 0xA8, 0x35, 0x8B, 0xE0, 0xE8, 0xD3, 0xCD, 0x45, 0x65,
    0xB0, 0x91, 0xC4, 0x95, 0x1B, 0xF3, 0x23, 0x1E, 0xC6, 0x71, 0xCF, 0x3E, 0x35, 0x2D, 0x6B, 0xE3,
]), "little")
_EXPONENT = 65537
_KEYSTREAM_PERIOD = 256 * 29  # lcm(256, 32 * 29): the byte index wraps at 256, the key indices at 32 and 29


def table_keystream(encrypted_key: bytes) -> bytes:
    key = pow(int.from_bytes(encrypted_key[:128], "little"), _EXPONENT, _MODULUS).to_bytes(128, "little")
    return bytes((i + key[i % 32] * key[i % 29]) & 0xFF for i in range(_KEYSTREAM_PERIOD))


def xor_table(table: bytes, encrypted_key: bytes) -> bytes:
    """Decrypt (or, being an XOR, encrypt) an entry table."""
    if not table:
        return table
    stream = table_keystream(encrypted_key)
    stream = (stream * (len(table) // len(stream) + 1))[:len(table)]
    return (int.from_bytes(table, "little") ^ int.from_bytes(stream, "little")).to_bytes(len(table), "little")


# ---- reading --------------------------------------------------------------------
class PakFile:
    """Entry table of one .pak: hashes sorted, with offset/sizes/attributes alongside."""

    def __init__(self, path):
        self.path = Path(path)
        self.hashes = array("Q")
        self.offsets = array("q")
        self.csizes = array("q")
        self.dsizes = array("q")
        self.attrs = array("q")
        self.order: list[int] = []  # hashes in the order of the table (mod paks keep it when rewritten)
        self.chunk_offsets: array | None = None
        self.chunk_sizes: array | None = None
        self.encrypted_table = False
        self._parse()

    def _parse(self):
        with open(self.path, "rb") as f:
            head = f.read(16)
            if len(head) < 16:
                raise MatfixError(f"{self.path.name}: pak 파일이 아닙니다")
            magic, major, _minor, feature, count, _fingerprint = struct.unpack("<IBBhii", head)
            if magic != MAGIC:
                raise MatfixError(f"{self.path.name}: pak 파일이 아닙니다")
            if major not in (2, 4) or count < 0:
                raise MatfixError(f"{self.path.name}: 지원하지 않는 pak 버전입니다 ({major})")
            size = 24 if major == 2 else 48
            table = f.read(count * size)
            if len(table) != count * size:
                raise MatfixError(f"{self.path.name}: pak 파일이 잘려 있습니다")
            if feature in _ENCRYPTED_TABLE:
                if feature == FEATURE_EXTRA_DATA:
                    f.read(4)
                elif feature in (FEATURE_DLC_EXTRA_DATA1, FEATURE_DLC_EXTRA_DATA2):
                    f.read(9)
                table = xor_table(table, f.read(128))
                self.encrypted_table = True
                if feature in _CHUNKED:
                    block, chunks = struct.unpack("<ii", f.read(8))
                    raw = array("I")
                    raw.frombytes(f.read(chunks * 8))
                    offsets, sizes = array("q"), array("q")
                    high, prev = 0, 0
                    for i in range(chunks):
                        off = raw[2 * i]
                        if i and off < prev:
                            high += 1 << 32
                        offsets.append(high | off)
                        sizes.append(raw[2 * i + 1] >> 10)
                        prev = off
                    self.chunk_offsets, self.chunk_sizes = offsets, sizes
        rows = []
        if major == 2:
            for off, dsize, lo, hi in struct.iter_unpack("<qqII", table):
                rows.append(((hi << 32) | lo, off, 0, dsize, 0))
        else:
            for lo, hi, off, csize, dsize, attrs, _checksum in struct.iter_unpack("<IIqqqqQ", table):
                rows.append(((hi << 32) | lo, off, csize, dsize, attrs))
        self.order = [r[0] for r in rows]
        for h, off, csize, dsize, attrs in sorted(rows):
            self.hashes.append(h)
            self.offsets.append(off)
            self.csizes.append(csize)
            self.dsizes.append(dsize)
            self.attrs.append(attrs)

    def __len__(self) -> int:
        return len(self.hashes)

    def find(self, hash64: int) -> int | None:
        from bisect import bisect_left

        i = bisect_left(self.hashes, hash64)
        return i if i < len(self.hashes) and self.hashes[i] == hash64 else None

    def entry(self, index: int) -> tuple[int, int, int, int]:
        return self.offsets[index], self.csizes[index], self.dsizes[index], self.attrs[index]

    def read_raw(self, index: int) -> bytes:
        """The stored bytes, still compressed (for copying an entry unchanged)."""
        off, csize, _dsize, _attrs = self.entry(index)
        with open(self.path, "rb") as f:
            f.seek(off)
            return f.read(csize)

    def read(self, index: int) -> bytes:
        return read_entry(self.path, *self.entry(index), self.chunk_offsets, self.chunk_sizes)

    def head(self, index: int, size: int = 4) -> bytes:
        """First bytes of an entry, without reading or unpacking all of it (a texture pak can
        hold gigabytes)."""
        off, csize, dsize, attrs = self.entry(index)
        compression = attrs & 0xF
        if attrs & 0x00FF0000 or (self.chunk_offsets is not None and attrs in _CHUNKED_ATTRIBUTES):
            return self.read(index)[:size]
        with open(self.path, "rb") as f:
            f.seek(off)
            if compression == COMPRESSION_NONE:
                return f.read(min(size, dsize))
            # a zstd block holds at most 128 KiB, so this much input always yields the first bytes
            start = f.read(min(csize, HEAD_INPUT))
        if compression == COMPRESSION_DEFLATE:
            return zlib.decompressobj(-15).decompress(start, size)[:size]
        if compression == COMPRESSION_ZSTD:
            return zstd_start(start, size)
        return self.read(index)[:size]

    def read_hash(self, hash64: int) -> bytes | None:
        i = self.find(hash64)
        return None if i is None else self.read(i)


def read_entry(path, offset: int, csize: int, dsize: int, attrs: int, chunk_offsets=None, chunk_sizes=None) -> bytes:
    with open(path, "rb") as f:
        if chunk_offsets is not None and attrs in _CHUNKED_ATTRIBUTES:
            # chunked entries: offset is the first chunk's index
            out = bytearray()
            remaining, chunk = csize, offset
            while remaining > 0:
                f.seek(chunk_offsets[chunk])
                size = chunk_sizes[chunk]
                block = f.read(size)
                out += block if size == MAX_CHUNK else zstd_decompress(block, MAX_CHUNK)
                chunk += 1
                remaining -= size
            return bytes(out[:dsize])
        f.seek(offset)
        raw = f.read(csize if csize else dsize)
    if (attrs & 0x00FF0000) >> 16:
        raise MatfixError("이 파일은 따로 암호화되어 있어 읽을 수 없습니다")
    compression = attrs & 0xF
    if compression == COMPRESSION_NONE:
        return raw[:dsize]
    if compression == COMPRESSION_DEFLATE:
        return zlib.decompress(raw, -15)
    if compression == COMPRESSION_ZSTD:
        return zstd_decompress(raw, dsize)
    raise MatfixError(f"알 수 없는 압축 방식입니다 ({compression})")


# ---- writing mod paks -----------------------------------------------------------
def compress_as(compression: int, data: bytes) -> bytes:
    """Compress the way the original entry was: a mod pak whose stored entries changed
    from uncompressed to zstd hung the game in MHWmodfixer's tests."""
    if compression == COMPRESSION_NONE:
        return data
    if compression == COMPRESSION_DEFLATE:
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        return co.compress(data) + co.flush()
    if compression == COMPRESSION_ZSTD:
        return zstd_compress(data)
    raise MatfixError(f"알 수 없는 압축 방식입니다 ({compression})")


def write_pak(entries: list[tuple[int, bytes, int, int]], path) -> None:
    """Unencrypted mod pak from (hash64, stored bytes, decompressed size, compression),
    in the given order. Checksums and fingerprint are 0, as in real mod paks.

    The stored bytes may also be a function returning them, so a large pak is copied
    one entry at a time."""
    with open(path, "wb") as f:
        f.write(struct.pack("<IiiI", MAGIC, 4, len(entries), 0))
        f.write(bytes(48 * len(entries)))
        rows = []
        for hash64, data, dsize, compression in entries:
            if callable(data):
                data = data()
            offset = f.tell()
            f.write(data)
            rows.append(struct.pack("<IIqqqqQ", hash64 & 0xFFFFFFFF, hash64 >> 32, offset, len(data), dsize,
                                    compression & 0xF, 0))
        f.seek(16)
        f.write(b"".join(rows))
