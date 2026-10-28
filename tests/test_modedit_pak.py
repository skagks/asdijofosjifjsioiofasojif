"""PAK members and rebuilding a mod PAK with replaced entries (synthetic paks only)."""

from __future__ import annotations

import random
import struct
from array import array
from collections import namedtuple
from pathlib import Path

import pytest

from mhwmm import matfix
from mhwmm.matfix import pak as pakmod
from mhwmm.matfix.pak import COMPRESSION_DEFLATE, COMPRESSION_NONE, COMPRESSION_ZSTD, PakFile, compress_as, write_pak
from mhwmm.modedit import EditError, pakedit

ZSTD = matfix.available()
USR = b"USR\0"


def user(n: int, size: int = 64) -> bytes:
    return USR + bytes([n]) * (size - 4)


def make_pak(path: Path, entries, compressions=None) -> Path:
    """``entries``: [(hash, bytes)], each compressed as in ``compressions`` (none by default)."""
    rows = []
    for i, (h, data) in enumerate(entries):
        c = (compressions or {}).get(i, COMPRESSION_NONE)
        rows.append((h, compress_as(c, data), len(data), c))
    path.parent.mkdir(parents=True, exist_ok=True)
    write_pak(rows, path)
    return path


def sample(tmp_path: Path, zstd: bool = ZSTD) -> tuple[Path, dict]:
    entries = [(0x30, user(1)), (0x10, b"MDF\0" + b"m" * 60), (0x20, user(2, 300)), (0x40, user(3, 5000))]
    comp = {0: COMPRESSION_NONE, 1: COMPRESSION_DEFLATE, 2: COMPRESSION_DEFLATE,
            3: COMPRESSION_ZSTD if zstd else COMPRESSION_NONE}
    return make_pak(tmp_path / "mod.pak", entries, comp), dict(entries)


# ---- members ------------------------------------------------------------------------------------------
def test_members_heads_names_types_and_size_cap(tmp_path, monkeypatch):
    path, data = sample(tmp_path)
    got = pakedit.members(path, names={0x20: "natives/STM/GameDesign/x.user.3"}.get,
                          type_of=lambda b: f"app.T{b[4]}")
    assert [(m.hash64, m.key, m.size, m.name, m.type_name) for m in got] == [
        (0x30, "#0000000000000030", 64, None, "app.T1"),  # table order, not hash order
        (0x20, "#0000000000000020", 300, "natives/STM/GameDesign/x.user.3", "app.T2"),
        (0x40, "#0000000000000040", 5000, None, "app.T3")]
    assert [m.compression for m in got][:2] == [COMPRESSION_NONE, COMPRESSION_DEFLATE]
    assert [m.hash64 for m in pakedit.members(path, kinds=(b"MDF\0",))] == [0x10]
    assert all(m.type_name == "" for m in pakedit.members(path))
    monkeypatch.setattr(pakedit, "MAX_MEMBER", 1000)
    assert [m.hash64 for m in pakedit.members(path)] == [0x30, 0x20]
    with pytest.raises(EditError, match="너무 커서"):
        pakedit.read_member(path, 0x40)


def test_members_survive_a_type_reader_that_fails(tmp_path):
    path, _ = sample(tmp_path)

    def broken(_data):
        raise ValueError("bad")

    assert len(pakedit.members(path, type_of=broken)) == 3


def test_read_member(tmp_path):
    path, data = sample(tmp_path)
    assert pakedit.read_member(path, 0x20) == data[0x20]
    with pytest.raises(KeyError):
        pakedit.read_member(path, 0x99)
    with pytest.raises(EditError):
        pakedit.read_member(make_broken(tmp_path), 1)
    with pytest.raises(EditError):
        pakedit.members(make_broken(tmp_path))


def make_broken(tmp_path: Path) -> Path:
    p = tmp_path / "broken.pak"
    p.write_bytes(b"not a pak at all")
    return p


@pytest.mark.parametrize("compression,size", [
    (COMPRESSION_DEFLATE, 4000),
    pytest.param(COMPRESSION_ZSTD, 400_000, marks=pytest.mark.skipif(not ZSTD, reason="zstd 없음"))])
def test_read_member_that_does_not_unpack(tmp_path, compression, size):
    """#25: an entry whose data was cut short raises EditError (shown, the file read-only), not zlib's or
    zstd's own error (a traceback in the CLI)."""
    data = USR + random.Random(size).randbytes(size)  # (a large zstd entry: its first block stays whole)
    path = make_pak(tmp_path / "cut.pak", [(0x20, data)], {0: compression})
    packed = PakFile(path).entry(0)[1]
    raw = path.read_bytes()
    path.write_bytes(raw[:len(raw) - packed // 2])
    with pytest.raises(EditError, match="PAK 안의 파일을 풀지 못했습니다"):
        pakedit.read_member(path, 0x20)
    assert [m.hash64 for m in pakedit.members(path)] == [0x20]  # listed: opening it says why


def test_members_skip_an_entry_whose_start_does_not_unpack(tmp_path):
    good = user(1)
    path = tmp_path / "odd.pak"
    write_pak([(0x10, b"\xff" * 64, 64, COMPRESSION_DEFLATE), (0x30, good, len(good), COMPRESSION_NONE)], path)
    assert [m.hash64 for m in pakedit.members(path)] == [0x30]
    with pytest.raises(EditError, match="풀지 못했습니다"):
        pakedit.read_member(path, 0x10)


# ---- rebuild ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("zstd", [False, pytest.param(True, marks=pytest.mark.skipif(not ZSTD, reason="zstd 없음"))])
def test_rebuild_keeps_order_and_compression(tmp_path, zstd):
    src, data = sample(tmp_path, zstd)
    new20, new40, new30 = user(9, 300), user(8, 5000), user(7)
    replaced = {0x20: new20, 0x30: new30}
    if zstd:
        replaced[0x40] = new40
    target = tmp_path / "stage" / "edits" / "0001_mod.pak"
    seen = []
    sha1 = pakedit.rebuild(src, target, replaced, progress=lambda n, total, text: seen.append((n, total)))
    old, back = PakFile(src), PakFile(target)
    assert back.order == old.order == [0x30, 0x10, 0x20, 0x40]
    for h in old.order:
        i, j = old.find(h), back.find(h)
        assert old.attrs[i] & 0xF == back.attrs[j] & 0xF  # compressed the way it was
        assert back.read(j) == replaced.get(h, data[h])
        if h not in replaced:
            assert back.read_raw(j) == old.read_raw(i) and back.csizes[j] == old.csizes[i]
    import hashlib

    assert sha1 == hashlib.sha1(target.read_bytes()).hexdigest()
    assert seen and all(total == 4 for _n, total in seen)


def test_rebuild_with_nothing_replaced_copies_entries(tmp_path):
    src, data = sample(tmp_path)
    target = tmp_path / "out.pak"
    pakedit.rebuild(src, target, {})
    back = PakFile(target)
    assert {h: back.read_hash(h) for h in back.order} == data


def test_rebuild_refuses_an_entry_that_is_not_there(tmp_path):
    src, _ = sample(tmp_path)
    with pytest.raises(EditError, match="없는 항목"):
        pakedit.rebuild(src, tmp_path / "out.pak", {0x77: b"x"})
    assert not (tmp_path / "out.pak").exists()


Row = namedtuple("Row", "hash data dsize compression")


def _faulty(how):
    """A write_pak that writes something other than it was given."""
    real = pakmod.write_pak

    def write(entries, path):
        rows = [Row(*e) for e in entries]
        if how == "P5":
            Path(path).write_bytes(b"KPKA garbage")
            return
        if how == "P2":
            rows = rows[:-1]
        elif how == "P1":
            rows[0], rows[1] = rows[1], rows[0]
        elif how == "P3":  # a kept entry recorded with another size
            i = next(i for i, r in enumerate(rows) if callable(r.data))
            rows[i] = rows[i]._replace(dsize=rows[i].dsize + 1)
        elif how == "P4":  # a replaced entry written with other bytes
            i = next(i for i, r in enumerate(rows) if not callable(r.data))
            rows[i] = rows[i]._replace(data=compress_as(rows[i].compression, user(0, rows[i].dsize)))
        real([tuple(r) for r in rows], path)

    return write


@pytest.mark.parametrize("check", ["P1", "P2", "P3", "P4", "P5"])
def test_each_check_deletes_the_target(tmp_path, monkeypatch, check):
    src, _ = sample(tmp_path, zstd=False)
    monkeypatch.setattr(pakedit, "write_pak", _faulty(check))
    target = tmp_path / "stage" / "x.pak"
    with pytest.raises(EditError, match=check):
        pakedit.rebuild(src, target, {0x20: user(9, 300)})
    assert not target.exists()


def test_an_interrupted_write_deletes_the_target(tmp_path, monkeypatch):
    src, _ = sample(tmp_path, zstd=False)
    target = tmp_path / "x.pak"

    def boom(entries, path):
        Path(path).write_bytes(b"half")
        raise KeyboardInterrupt

    monkeypatch.setattr(pakedit, "write_pak", boom)
    with pytest.raises(KeyboardInterrupt):
        pakedit.rebuild(src, target, {0x20: user(9, 300)})
    assert not target.exists()


# ---- refusals -------------------------------------------------------------------------------------------
def test_check_rebuildable(tmp_path, monkeypatch):
    src, _ = sample(tmp_path)
    assert pakedit.check_rebuildable(src) == ""
    assert "읽지 못했습니다" in pakedit.check_rebuildable(make_broken(tmp_path))
    # duplicate hashes: a rebuild would keep only one of them
    dup = make_pak(tmp_path / "dup.pak", [(0x10, user(1)), (0x10, user(2))])
    assert "두 번" in pakedit.check_rebuildable(dup)
    # separately encrypted entries (write_pak keeps only attrs & 0xF)
    enc = make_pak(tmp_path / "enc.pak", [(0x10, user(1)), (0x20, user(2))])
    raw = bytearray(enc.read_bytes())
    struct.pack_into("<q", raw, 16 + 48 + 32, 0x00010000)
    enc.write_bytes(bytes(raw))
    assert "암호화" in pakedit.check_rebuildable(enc)
    with pytest.raises(EditError, match="다시 만들 수 없는 형식"):
        pakedit.rebuild(enc, tmp_path / "o.pak", {0x10: user(5)})
    # an entry without a stored size (old pak versions)
    zero = make_pak(tmp_path / "zero.pak", [(0x10, user(1))])
    raw = bytearray(zero.read_bytes())
    struct.pack_into("<q", raw, 16 + 16, 0)
    zero.write_bytes(bytes(raw))
    assert "크기 정보" in pakedit.check_rebuildable(zero)

    # chunked paks
    class Chunked(PakFile):
        def _parse(self):
            super()._parse()
            self.chunk_offsets = array("q", [0])

    monkeypatch.setattr(pakedit, "PakFile", Chunked)
    assert "나누어" in pakedit.check_rebuildable(src)
    with pytest.raises(EditError, match="다시 만들 수 없는 형식"):
        pakedit.rebuild(src, tmp_path / "c.pak", {0x30: user(4)})


def test_check_space(tmp_path, monkeypatch):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(pakedit.shutil, "disk_usage", lambda p: Usage(10 << 30, 0, 5 << 20))
    with pytest.raises(EditError, match=r"저장 공간이 모자라 PAK을 다시 만들지 않았습니다 \(64\.0 MB 필요 · 남은 공간 5\.0 MB\)"):
        pakedit.check_space(tmp_path / "not" / "yet", 64 << 20)
    pakedit.check_space(tmp_path, 1 << 20)
    src, _ = sample(tmp_path)
    target = tmp_path / "stage" / "x.pak"
    with pytest.raises(EditError, match="저장 공간"):
        pakedit.rebuild(src, target, {0x20: user(9, 300)})
    assert not target.exists()


def test_size_text_and_keys():
    assert pakedit.size_text(512) == "512 B" and pakedit.size_text(200 * 1024 * 1024) == "200.0 MB"
    assert pakedit.member_key(0x5B1E0F9C3A7D2E41) == "#5B1E0F9C3A7D2E41"
    assert pakedit.member_hash("#5b1e0f9c3a7d2e41") == 0x5B1E0F9C3A7D2E41
