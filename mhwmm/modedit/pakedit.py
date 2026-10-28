"""The members of a mod's root PAK and rebuilding it with some entries replaced.

A value edit inside a PAK writes the whole PAK again (``rebuild``), the way matfix does for a repair
(repair._rebuild_pak): the entry table keeps its order, kept entries are copied byte for byte one at a
time, replaced entries are compressed the way they were (MHWmodfixer found that a mod PAK whose
entries changed compression can hang the game).  The result is read back and checked before it is
used (P1-P6), and deleted on any failure.  PAKs this cannot write again faithfully are refused up
front (``check_rebuildable``).
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from ..matfix import MatfixError
from ..matfix.pak import PakFile, compress_as, write_pak
from . import EditError

log = logging.getLogger(__name__)

MAX_MEMBER = 64 * 1024 * 1024  # larger entries are not offered (and not read)
SPACE_MARGIN = 64 * 1024 * 1024  # free space kept beside a rebuilt PAK
BIG_PAK = 200 * 1024 * 1024  # the page says a copy of this size is kept
ENCRYPTED = 0x00FF0000  # entry attributes: separately encrypted (write_pak keeps only attrs & 0xF)


@dataclass(frozen=True)
class PakMember:
    hash64: int
    key: str  # "#%016X", the member name in the record
    size: int  # decompressed
    compression: int
    name: str | None  # the game path, when the path list knows the hash
    type_name: str  # the root type (from type_of), "" when not read


def member_key(hash64: int) -> str:
    return f"#{hash64:016X}"


def member_hash(key: str) -> int:
    return int(key.lstrip("#"), 16)


def size_text(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _open(path: Path) -> PakFile:
    try:
        return PakFile(path)
    except MatfixError as exc:
        raise EditError(str(exc)) from None
    except OSError as exc:
        raise EditError(f"PAK을 읽지 못했습니다: {exc}") from None


def check_rebuildable(path: Path) -> str:
    """"" when the PAK can be written again entry for entry, else why not (Korean)."""
    try:
        pak = PakFile(path)
    except (OSError, MatfixError) as exc:
        return f"PAK을 읽지 못했습니다: {exc}"
    if pak.chunk_offsets is not None:
        return "나누어 저장된 PAK입니다"
    if any(a & ENCRYPTED for a in pak.attrs):
        return "따로 암호화된 항목이 있습니다"
    if len(set(pak.order)) != len(pak.order):
        return "같은 항목이 두 번 들어 있습니다"
    if any(c == 0 and d > 0 for c, d in zip(pak.csizes, pak.dsizes)):
        return "옛 형식이거나 크기 정보가 없는 항목이 있습니다"
    return ""


def members(path: Path, kinds=(b"USR\0",), names: Callable[[int], str | None] | None = None,
            type_of: Callable[[bytes], str] | None = None) -> list[PakMember]:
    """The PAK's entries whose first bytes are one of ``kinds``, in table order, at most MAX_MEMBER
    each.  ``names`` maps a hash to its game path (PathIndex.lookup), ``type_of`` reads an entry's
    root type from its bytes (the entry is read only then)."""
    pak = _open(path)
    out = []
    for h in dict.fromkeys(pak.order):
        i = pak.find(h)
        _off, _csize, dsize, attrs = pak.entry(i)
        if dsize > MAX_MEMBER or attrs & ENCRYPTED:
            continue
        try:
            if pak.head(i) not in kinds:
                continue
        except MatfixError:
            continue
        except OSError:
            raise
        except Exception as exc:  # its first bytes do not unpack (zlib.error, zstd's errors): not offered
            log.debug("%s %s: %r", path, member_key(h), exc)
            continue
        type_name = ""
        if type_of is not None:
            try:
                type_name = type_of(pak.read(i)) or ""
            except Exception as exc:  # a member that does not read is still listed (opening it says why)
                log.debug("%s %s: %s", path, member_key(h), exc)
        name = names(h) if names is not None else None
        out.append(PakMember(h, member_key(h), dsize, attrs & 0xF, name, type_name))
    return out


def read_member(path: Path, hash64: int) -> bytes:
    """One entry's bytes; KeyError when it is not in the PAK, EditError when it is too large or
    unreadable (its data cut short or damaged: deflate and zstd raise their own errors)."""
    pak = _open(path)
    i = pak.find(hash64)
    if i is None:
        raise KeyError(hash64)
    if pak.entry(i)[2] > MAX_MEMBER:
        raise EditError(f"PAK 안 파일이 너무 커서 바꿀 수 없습니다 ({size_text(pak.entry(i)[2])})")
    try:
        return pak.read(i)
    except MatfixError as exc:
        raise EditError(str(exc)) from None
    except OSError as exc:
        raise EditError(f"PAK을 읽지 못했습니다: {exc}") from None
    except Exception as exc:  # zlib.error, zstd's own errors, a length that does not fit
        log.debug("%s %s: %r", path, member_key(hash64), exc)
        raise EditError(f"PAK 안의 파일을 풀지 못했습니다: {exc}") from None


def check_space(folder: Path, need: int) -> None:
    """EditError when ``folder``'s drive has less than ``need`` bytes free."""
    folder = Path(folder)
    probe = folder
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return  # unknown: writing fails loudly anyway
    if free < need:
        raise EditError(f"저장 공간이 모자라 PAK을 다시 만들지 않았습니다 ({size_text(need)} 필요 · 남은 공간 {size_text(free)})")


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def rebuild(src: Path, target: Path, replaced: Mapping[int, bytes], progress=None) -> str:
    """Write ``src`` to ``target`` with the entries in ``replaced`` (hash → new bytes) and return the
    sha1 of the file written.  Refused (EditError) when the PAK cannot be rebuilt, an entry is not in
    it, or there is not enough free space; the target is deleted on any failure."""
    src, target = Path(src), Path(target)
    why = check_rebuildable(src)
    if why:
        raise EditError(f"이 PAK은 다시 만들 수 없는 형식이라 값을 바꿀 수 없습니다 ({why})")
    pak = _open(src)
    missing = [h for h in replaced if pak.find(h) is None]
    if missing:
        raise EditError(f"{src.name}: PAK 안에 없는 항목입니다 ({member_key(missing[0])})")
    target.parent.mkdir(parents=True, exist_ok=True)
    check_space(target.parent, src.stat().st_size + SPACE_MARGIN)
    total = len(pak.order)
    rows, kept = [], {}

    def copy(i: int, n: int):
        def read():
            if progress is not None:
                progress(n, total, src.name)
            return pak.read_raw(i)
        return read

    try:
        for n, h in enumerate(pak.order):
            i = pak.find(h)
            _off, csize, dsize, attrs = pak.entry(i)
            compression = attrs & 0xF
            if h in replaced:
                data = replaced[h]
                rows.append((h, compress_as(compression, data), len(data), compression))
            else:
                rows.append((h, copy(i, n), dsize, compression))
                kept[h] = (csize, dsize, compression)
        write_pak(rows, target)
        _verify(pak, target, replaced, kept)
        return _sha1(target)
    except MatfixError as exc:
        target.unlink(missing_ok=True)
        raise EditError(f"{src.name}: PAK을 다시 만들지 못했습니다: {exc}") from None
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _verify(pak: PakFile, target: Path, replaced: Mapping[int, bytes], kept: dict) -> None:
    def fail(check: str, what: str):
        raise EditError(f"다시 만든 PAK을 확인하지 못해 저장하지 않았습니다 ({check}: {what})")

    try:
        back = PakFile(target)  # P5
    except (OSError, MatfixError) as exc:
        fail("P5", f"열리지 않습니다: {exc}")
    if len(back.order) != len(pak.order):  # P2
        fail("P2", f"항목 수가 다릅니다 ({len(back.order)}/{len(pak.order)})")
    if list(back.order) != list(pak.order):  # P1
        fail("P1", "항목 순서가 다릅니다")
    for h, (csize, dsize, compression) in kept.items():  # P3
        _off, c, d, attrs = back.entry(back.find(h))
        if (c, d, attrs & 0xF) != (csize, dsize, compression):
            fail("P3", f"그대로 둔 항목이 달라졌습니다 ({member_key(h)})")
    for h, data in replaced.items():  # P4
        try:
            same = back.read_hash(h) == data
        except MatfixError:
            same = False
        if not same:
            fail("P4", f"바꾼 항목을 읽어 보니 내용이 다릅니다 ({member_key(h)})")
