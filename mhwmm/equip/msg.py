"""RE Engine GMSG text files (MH Wilds: *.msg.23), read only.

Layout and the string-pool key as in dtlnor's REMSG_Converter (MIT) and REasy (MIT); this is
an independent reader:

    0x00 u32 version (23)   0x04 "GMSG"   0x08 u64 header size (0x10)
    0x10 u32 entries, u32 attributes, u32 language slots (33), u32 padding
    0x20 u64 string pool offset (encrypted versions only; the pool runs to the end of the file)
    then u64 offsets of: an unused u64, the language table, attribute types, attribute names
    then u64[entries] absolute offsets of the entry heads
    entry head: 16-byte GUID (.NET byte order, the same bytes an RSZ Guid field stores),
        u32 sound id, u32 murmur3 hash of the entry name (versions > 15), u64 name pointer,
        u64 attribute pointer, u64[language slots] text pointers (absolute, into the pool)

The pool is UTF-16LE, NUL-terminated strings, XOR-chained with a 16-byte key:
plain[i] = enc[i] ^ enc[i-1] ^ KEY[i % 16].  Slot i of a v23 file is via.Language i
(0 Japanese, 1 English, 11 Korean ...); unused slots are -1 in the language table.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from ..matfix.pak import murmur3_32
from . import EquipError

MAGIC = b"GMSG"
KEY = bytes((0xCF, 0xCE, 0xFB, 0xF8, 0xEC, 0x0A, 0x33, 0x66, 0x93, 0xA9, 0x1D, 0x93, 0x50, 0x39, 0x5F, 0x09))
JAPANESE, ENGLISH, KOREAN = 0, 1, 11
DEFAULT_LANGUAGES = (KOREAN, ENGLISH, JAPANESE)

_PLAIN_VERSIONS = {0x2022033D, 0x0100010C}  # > 12 but not encrypted (other RE Engine games)
_INDEX_VERSIONS = {0x2022033D, 0x0100010C, 0x0300010E, 0x0400010F}  # > 15 but no name hash
_TAG = re.compile(r"<[^>]*>")


def is_encrypted(version: int) -> bool:
    return version > 12 and version not in _PLAIN_VERSIONS


def is_hashed(version: int) -> bool:
    return version > 15 and version not in _INDEX_VERSIONS


def decrypt_pool(enc: bytes) -> bytes:
    """The XOR chain undone at once with big integers (byte i of x << 8 is byte i-1 of x)."""
    n = len(enc)
    if not n:
        return b""
    x = int.from_bytes(enc, "little")
    key = int.from_bytes((KEY * (n // 16 + 1))[:n], "little")
    return ((x ^ (x << 8) ^ key) & ((1 << (8 * n)) - 1)).to_bytes(n, "little")


def encrypt_pool(plain: bytes) -> bytes:
    """The inverse of decrypt_pool (for building test files)."""
    out, prev = bytearray(len(plain)), 0
    for i, b in enumerate(plain):
        prev = out[i] = b ^ prev ^ KEY[i & 15]
    return bytes(out)


def clean_text(text: str) -> str:
    """A name as the game shows it: markup tags removed, line breaks turned into spaces."""
    return " ".join(_TAG.sub("", text).split())


def is_placeholder(text: str) -> bool:
    """Development leftovers and empty rows ("-", "#Rejected#", "{0}")."""
    t = text.strip()
    return not t or t in ("-", "―", "—") or "#Rejected#" in t or t.startswith("{0}")


@dataclass
class MsgEntry:
    guid: bytes
    name: str
    content: list[str]  # one string per language slot
    hash: int = 0


@dataclass
class MsgFile:
    version: int
    languages: list[int]  # slot -> via.Language code, -1 = unused
    entries: list[MsgEntry] = field(default_factory=list)
    _by_guid: dict | None = None
    _by_name: dict | None = None

    def by_guid(self, guid: bytes) -> MsgEntry | None:
        if self._by_guid is None:
            self._by_guid = {e.guid: e for e in self.entries}
        return self._by_guid.get(bytes(guid))

    def by_name(self, name: str) -> MsgEntry | None:
        if self._by_name is None:
            self._by_name = {e.name: e for e in self.entries}
        return self._by_name.get(name)

    def _slot(self, lang: int) -> int | None:
        if 0 <= lang < len(self.languages) and self.languages[lang] == lang:
            return lang
        try:
            return self.languages.index(lang)
        except ValueError:
            return None

    def text(self, entry: MsgEntry | None, languages=DEFAULT_LANGUAGES) -> str:
        """The entry's text in the first of ``languages`` that has one (cleaned, "" for none)."""
        if entry is None:
            return ""
        for lang in languages:
            i = self._slot(lang)
            if i is not None and i < len(entry.content):
                text = clean_text(entry.content[i])
                if not is_placeholder(text):
                    return text
        return ""

    def name_of(self, guid: bytes, languages=DEFAULT_LANGUAGES) -> str:
        return self.text(self.by_guid(guid), languages) if guid and any(guid) else ""


def parse(data: bytes, verify: bool = False) -> MsgFile:
    """Read a .msg file.  ``verify`` checks every entry name against its stored hash."""
    try:
        return _parse(data, verify)
    except (struct.error, IndexError, ValueError, OverflowError) as exc:
        raise EquipError(f"msg 파일을 읽지 못했습니다: {exc}") from None


def _parse(data: bytes, verify: bool) -> MsgFile:
    if len(data) < 0x48 or data[4:8] != MAGIC:
        raise ValueError("not a GMSG file")
    version = struct.unpack_from("<I", data, 0)[0]
    count, attr_count, lang_count = struct.unpack_from("<III", data, 0x10)
    if count > 1_000_000 or attr_count > 1000 or lang_count > 256:
        raise ValueError("implausible header")
    p = 0x20
    encrypted = is_encrypted(version)
    pool_off = None
    if encrypted:
        pool_off = struct.unpack_from("<Q", data, p)[0]
        p += 8
    _unused, lang_off, _attr_types, _attr_names = struct.unpack_from("<4Q", data, p)  # attributes: unused
    p += 32
    heads = struct.unpack_from(f"<{count}Q", data, p)
    languages = list(struct.unpack_from(f"<{lang_count}i", data, lang_off))
    head_size = 40 + 8 * lang_count
    if pool_off is None:  # unencrypted: heads, then every entry's attribute values, then the pool
        first = min(heads) if heads else p
        pool_off = first + count * head_size + count * attr_count * 8
    if pool_off > len(data):
        raise ValueError("string pool past the end")
    pool = bytes(data[pool_off:])
    if encrypted:
        pool = decrypt_pool(pool)
    cache: dict[int, str] = {}

    def wstr(ptr: int) -> str:
        rel = ptr - pool_off
        if rel < 0 or rel >= len(pool):
            return ""
        s = cache.get(rel)
        if s is None:
            end = rel
            while True:  # an even-aligned 00 00 (works on bytes: surrogate pairs cannot shift it)
                end = pool.find(b"\x00\x00", end)
                if end < 0:
                    end = len(pool)
                    break
                if (end - rel) % 2 == 0:
                    break
                end += 1
            s = cache[rel] = pool[rel:end].decode("utf-16-le", "replace")
        return s

    entries = []
    for head in heads:
        if head + head_size > len(data):
            raise ValueError("entry past the end")
        guid = bytes(data[head:head + 16])
        _sound, name_hash, name_ptr, _attr_ptr = struct.unpack_from("<IIQQ", data, head + 16)
        ptrs = struct.unpack_from(f"<{lang_count}Q", data, head + 40)
        name = wstr(name_ptr)
        if verify and is_hashed(version) and murmur3_32(name.encode("utf-16-le")) != name_hash:
            raise ValueError(f"name hash mismatch: {name!r}")
        entries.append(MsgEntry(guid, name, [wstr(q) for q in ptrs], name_hash))
    return MsgFile(version, languages, entries)
