"""Building one item's output (commit, reapply, carry) with the fake engine of test_modedit_engines."""

from __future__ import annotations

import hashlib
import os
import weakref
from pathlib import Path

import pytest

import modedit_fixtures as mf
from mhwmm.matfix import rsz
from mhwmm.matfix.pak import COMPRESSION_DEFLATE, PakFile, compress_as, write_pak
from mhwmm.modedit import EditError, StaleError, build, engines, model, pakedit, rszdata
from mhwmm.modedit.build import BuildInput, build_item, collect
from mhwmm.modedit.model import BaseRef, Change, Conflict, Item, Member, Out, Record, Source
from test_modedit_engines import FakeEngine, fake_file, fake_rule_of, read_fake

REL = "natives/STM/GameDesign/Common/Item/ItemData.user.3"


@pytest.fixture
def fake(monkeypatch):
    eng = FakeEngine()
    monkeypatch.setitem(engines._ENGINES, "rsz", eng)
    monkeypatch.setattr(model, "_rule_of", fake_rule_of)
    return eng


@pytest.fixture
def rsz_engine(monkeypatch):
    """The real "rsz" engine, with the made-up registry of modedit_fixtures."""
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", mf.registry())
    monkeypatch.setattr(rsz.TypeRegistry, "_from_file", False)
    monkeypatch.setattr(rsz.TypeRegistry, "_holds", 0)
    monkeypatch.setattr(rszdata, "_CHECKED", weakref.WeakKeyDictionary())
    return engines.get("rsz")


def C(at, before, after, kind="S32", **kw):
    return Change(at=at, kind=kind, before=before, after=after, label=at, **kw)


def loose(tmp_path: Path, values: dict, changes, conflicts=(), mode="commit", new=None, repaired=None, **flags):
    """A stored .user file (and, with ``repaired``, a matfix repair of it as the base)."""
    stored = tmp_path / "mod" / "files" / REL
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(fake_file(values, **flags))
    base = BaseRef(stored, REL, "stored", REL)
    if repaired is not None:
        path = tmp_path / "mod" / "matfix" / REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fake_file(repaired))
        base = BaseRef(path, f"matfix/{REL}", "repair", REL)
    item = Item(Source("", 0, REL), members=[Member("", "rsz", "ItemData", list(changes), list(conflicts))])
    if new is None:
        new = frozenset(("", c.at) for c in changes)
    return BuildInput(index=1, item=item, key=REL, rel=REL, base=base, stored_path=stored, mode=mode, new=new)


def sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


# ---- loose files -------------------------------------------------------------------------------------
def test_commit_builds_and_checks_the_output(tmp_path, fake):
    inp = loose(tmp_path, {"a": "1", "b": "2"}, [C("a", "1", "5")])
    stage = tmp_path / "stage"
    got = build_item(inp, stage, 9)
    assert got.index == 1 and got.out_path == stage / "edits" / "0009_ItemData.user.3"
    assert read_fake(got.out_path.read_bytes()) == {"a": "5", "b": "2"}
    st = inp.stored_path.stat()
    assert got.item.base == model.Base(REL, st.st_size, st.st_mtime_ns, sha1(inp.stored_path), sha1(inp.stored_path))
    assert got.item.out == Out("edits/0009_ItemData.user.3", got.out_path.stat().st_size, sha1(got.out_path))
    assert got.item.members[0].changes == [C("a", "1", "5")] and not got.item.members[0].conflicts
    assert inp.item.base is None  # the input is not changed
    assert not got.notes


def test_a_repair_is_the_base(tmp_path, fake):
    inp = loose(tmp_path, {"a": "1"}, [C("a", "1", "5")], repaired={"a": "1", "new": "0"})
    got = build_item(inp, tmp_path / "stage", 1)
    assert read_fake(got.out_path.read_bytes()) == {"a": "5", "new": "0"}
    assert got.item.base.key == f"matfix/{REL}"
    assert got.item.base.sha1 == sha1(inp.base.path) != got.item.base.stored_sha1 == sha1(inp.stored_path)


def test_commit_refuses_a_new_change_that_conflicts(tmp_path, fake):
    inp = loose(tmp_path, {"a": "3"}, [C("a", "1", "5")])
    with pytest.raises(EditError, match="값이 그사이 바뀌었습니다: a. 창을 다시 열어 주세요."):
        build_item(inp, tmp_path / "stage", 1)
    assert not (tmp_path / "stage" / "edits").exists()


def test_commit_keeps_old_conflicts_and_records_new_ones_of_old_changes(tmp_path, fake):
    old_conflict = Conflict(C("x", "1", "2"), "7", "changed")
    changes = [C("a", "1", "5"), C("b", "1", "9")]  # b was recorded before; only a is new in this save
    inp = loose(tmp_path, {"a": "1", "b": "4", "x": "7"}, changes, [old_conflict], new=frozenset({("", "a")}))
    got = build_item(inp, tmp_path / "stage", 2)
    m = got.item.members[0]
    assert [c.at for c in m.changes] == ["a"]
    assert [(x.change.at, x.why, x.now) for x in m.conflicts] == [("x", "changed", "7"), ("b", "changed", "4")]
    assert got.notes == ["ItemData: 확인할 값 1개"]


@pytest.mark.parametrize("mode", ["reapply", "carry"])
def test_a_file_that_does_not_open_makes_the_member_pending(tmp_path, fake, mode):
    inp = loose(tmp_path, {"a": "1"}, [C("a", "1", "5")], mode=mode, stale=True)
    got = build_item(inp, tmp_path / "stage", 3)
    m = got.item.members[0]
    assert m.pending == "지금 게임과 형식이 달라 읽지 못했습니다." and m.changes == [C("a", "1", "5")]
    assert got.out_path is None and got.item.out is None and got.item.base is not None
    assert got.notes == ["ItemData: 지금 게임과 형식이 달라 읽지 못했습니다."]


def test_commit_raises_when_the_file_does_not_open(tmp_path, fake):
    with pytest.raises(StaleError):
        build_item(loose(tmp_path, {"a": "1"}, [C("a", "1", "5")], stale=True), tmp_path / "stage", 1)
    with pytest.raises(EditError):
        build_item(loose(tmp_path, {"a": "1"}, [C("a", "1", "5")], broken=True), tmp_path / "stage", 1)


def test_commit_that_only_takes_values_away_from_a_file_that_waits(tmp_path, fake):
    """#4 (I9): 원래 값으로 on one value of a file that cannot be opened now (a stale stored file after a
    repair was undone or an update): the save goes through and the member keeps waiting, as a
    re-apply would leave it; a save that sets one of its values still stops."""
    for n, flags in enumerate(({"stale": True}, {"broken": True})):
        inp = loose(tmp_path, {"a": "1", "b": "1"}, [C("a", "1", "5")], new=frozenset(), **flags)
        got = build_item(inp, tmp_path / f"stage{n}", 1)
        m = got.item.members[0]
        assert m.pending and m.changes == [C("a", "1", "5")]
        assert got.out_path is None and got.item.out is None and got.item.base is not None
    with pytest.raises(StaleError):
        build_item(loose(tmp_path, {"a": "1"}, [C("a", "1", "5")], new=frozenset({("", "a")}), stale=True),
                   tmp_path / "stage", 1)


@pytest.mark.parametrize("mode", ["carry", "reapply", "commit"])
def test_conflicts_of_an_earlier_update_are_judged_again(tmp_path, fake, mode):
    """#1: the author changed the value again (60 → 50 → 70): the conflict says 70, so 내 값 유지 makes a
    change that applies; the author put it back to 60: my change applies again."""
    mine = C("a", "60", "5000")
    left = [Conflict(mine, "50", "changed")]
    inp = loose(tmp_path, {"a": "70", "b": "1"}, [C("b", "1", "2")], left, mode=mode, new=frozenset())
    got = build_item(inp, tmp_path / "stage1", 1)
    m = got.item.members[0]
    assert [(x.why, x.now) for x in m.conflicts] == [("changed", "70")] and m.changes == [C("b", "1", "2")]
    assert not any("확인할 값" in n for n in got.notes)  # not a new one
    inp = loose(tmp_path, {"a": "60", "b": "1"}, [C("b", "1", "2")], left, mode=mode, new=frozenset())
    got = build_item(inp, tmp_path / "stage2", 2)
    m = got.item.members[0]
    assert not m.conflicts and m.changes == [C("b", "1", "2"), mine]
    assert read_fake(got.out_path.read_bytes()) == {"a": "5000", "b": "2"}


def test_changes_named_by_an_older_key_are_kept_the_new_files_way(tmp_path, rsz_engine):
    """#8: an update made _ItemId repeat, so the rows are named by _SortId: the carried changes (a rule
    and a single value over one of its rows) apply and are kept under the new names; a value this save
    sets under the old name that changed meanwhile still stops the save."""
    stored = tmp_path / "mod" / "files" / REL
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(mf.item_data(rows=[mf.item_row(i) for i in range(3)] + [mf.item_row(3, _ItemId=102)]))
    old = "_Values[_ItemId=101]._SellPrice"

    def inp(changes, mode, new=frozenset()):
        item = Item(Source("", 0, REL), members=[Member("", "rsz", "ItemData", list(changes))])
        return BuildInput(1, item, REL, REL, BaseRef(stored, REL, "stored", REL), stored, mode, new)

    changes = [C("_Values[*]._MaxCount", "10", "99", kind="S16"), C(old, "60", "500"),
               C("_Values[_ItemId=101]._MaxCount", "10", "50", kind="S16")]
    got = build_item(inp(changes, "carry"), tmp_path / "stage", 1)
    m = got.item.members[0]
    assert not m.pending and not m.conflicts and got.out_path is not None
    assert [c.at for c in m.changes] == ["_Values[*]._MaxCount", "_Values[_SortId=1]._SellPrice",
                                         "_Values[_SortId=1]._MaxCount"]
    with pytest.raises(EditError, match="그사이 바뀌었습니다"):
        build_item(inp([C(old, "61", "500")], "commit", frozenset({("", old)})), tmp_path / "stage2", 2)


def test_reapply_clears_an_old_pending_reason(tmp_path, fake):
    inp = loose(tmp_path, {"a": "1"}, [C("a", "1", "5")], mode="reapply")
    inp.item.members[0].pending = "예전 이유"
    got = build_item(inp, tmp_path / "stage", 4)
    assert got.item.members[0].pending == "" and got.out_path is not None


def test_carry_rebases_on_the_new_file(tmp_path, fake):
    # the author's update: a unchanged, b already my value, c changed by the author, d gone, a new row for the rule
    changes = [C("_V[*]._P", "10", "20"), C("a", "1", "5"), C("b", "1", "9"), C("c", "1", "2"), C("d", "1", "2")]
    values = {"_V[1]._P": "10", "_V[2]._P": "10", "a": "1", "b": "9", "c": "3"}
    inp = loose(tmp_path, values, changes, mode="carry", new=frozenset())
    got = build_item(inp, tmp_path / "stage", 5)
    m = got.item.members[0]
    assert read_fake(got.out_path.read_bytes()) == {"_V[1]._P": "20", "_V[2]._P": "20", "a": "5", "b": "9", "c": "3"}
    assert [(c.at, c.count) for c in m.changes] == [("_V[*]._P", 2), ("a", 0), ("b", 0)]
    assert [(x.change.at, x.why) for x in m.conflicts] == [("c", "changed"), ("d", "gone")]
    assert got.notes == ["ItemData: 확인할 값 2개"]


def test_nothing_to_write_gives_no_output(tmp_path, fake):
    inp = loose(tmp_path, {"a": "5"}, [C("a", "1", "5")], mode="reapply")
    got = build_item(inp, tmp_path / "stage", 6)
    assert got.out_path is None and got.item.out is None and got.item.members[0].changes == [C("a", "1", "5")]
    assert not (tmp_path / "stage").exists()
    inp = loose(tmp_path, {"a": "3"}, [C("a", "1", "5")], mode="reapply")
    got = build_item(inp, tmp_path / "stage", 6)
    assert got.out_path is None and not got.item.members[0].changes and got.item.members[0].conflicts


def test_v6_read_back(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(build, "_sha1", lambda path: "0" * 40)
    inp = loose(tmp_path, {"a": "1"}, [C("a", "1", "5")])
    with pytest.raises(EditError, match="다시 읽어 보니"):
        build_item(inp, tmp_path / "stage", 7)
    assert not (tmp_path / "stage" / "edits" / "0007_ItemData.user.3").exists()
    # re-apply: the member waits instead, as it was given
    inp = loose(tmp_path, {"a": "1"}, [C("a", "1", "5")], mode="reapply")
    got = build_item(inp, tmp_path / "stage", 7)
    assert got.out_path is None and "다시 읽어 보니" in got.item.members[0].pending
    assert got.item.members[0].changes == [C("a", "1", "5")]


def test_commit_that_only_takes_values_away_when_the_output_cannot_be_written(tmp_path, fake, monkeypatch):
    """#4 (I9): 원래 값으로 on one value while the output cannot be written (it did not read back, or the
    disk is full): the save goes through, the item waits with its changes as given and no output (the
    game gets the base); a save that sets one of its values still stops, and a re-apply's OSError still
    goes up to the manager (_failed_build)."""
    read_back = build._sha1
    monkeypatch.setattr(build, "_sha1", lambda path: "0" * 40)
    inp = loose(tmp_path, {"a": "1", "b": "1"}, [C("a", "1", "5")], new=frozenset())
    got = build_item(inp, tmp_path / "stage", 1)
    m = got.item.members[0]
    assert "다시 읽어 보니" in m.pending and m.changes == [C("a", "1", "5")]
    assert got.out_path is None and got.item.out is None and got.item.base is not None
    assert got.notes == [f"ItemData: {m.pending}"]
    monkeypatch.setattr(build, "_sha1", read_back)

    def full(stage, name, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(build, "_write", full)
    old = Conflict(C("x", "1", "2"), "7", "changed")
    inp = loose(tmp_path, {"a": "1", "x": "7"}, [C("a", "1", "5")], [old], new=frozenset())
    got = build_item(inp, tmp_path / "stage", 2)
    m = got.item.members[0]
    assert m.pending == "바꾼 파일을 저장하지 못했습니다: [Errno 28] No space left on device"
    assert m.changes == [C("a", "1", "5")] and m.conflicts == [old] and got.out_path is None
    with pytest.raises(OSError):
        build_item(loose(tmp_path, {"a": "1"}, [C("a", "1", "5")]), tmp_path / "stage", 3)
    with pytest.raises(OSError):
        build_item(loose(tmp_path, {"a": "1"}, [C("a", "1", "5")], mode="reapply"), tmp_path / "stage", 4)


def test_numbering_and_names(tmp_path, fake):
    stage = tmp_path / "stage"
    a = build_item(loose(tmp_path, {"a": "1"}, [C("a", "1", "5")]), stage, 12)
    b = build_item(loose(tmp_path, {"a": "1"}, [C("a", "1", "6")]), stage, 13)
    assert (a.item.out.file, b.item.out.file) == ("edits/0012_ItemData.user.3", "edits/0013_ItemData.user.3")
    assert read_fake(a.out_path.read_bytes()) == {"a": "5"} and read_fake(b.out_path.read_bytes()) == {"a": "6"}
    with pytest.raises(ValueError):
        build_item(loose(tmp_path, {"a": "1"}, [], mode="later"), stage, 1)


# ---- PAK files -------------------------------------------------------------------------------------------
PAK = "data_mod.pak"
H1, H2, H3 = 0x5B1E0F9C3A7D2E41, 0x10, 0x20


def pak_input(tmp_path: Path, members, mode="commit", entries=None, new=None) -> BuildInput:
    stored = tmp_path / "mod" / "files" / PAK
    stored.parent.mkdir(parents=True, exist_ok=True)
    entries = entries or [(H1, fake_file({"rate": ("F32", "10.0")})), (H2, b"MDF\0other"),
                          (H3, fake_file({"n": "1"}))]
    write_pak([(h, compress_as(COMPRESSION_DEFLATE, d), len(d), COMPRESSION_DEFLATE) for h, d in entries], stored)
    item = Item(Source("x10", 0, PAK), members=[Member(pakedit.member_key(h), "rsz", "", list(cs)) for h, cs in members])
    if new is None:
        new = frozenset((pakedit.member_key(h), c.at) for h, cs in members for c in cs)
    return BuildInput(index=0, item=item, key=f"o1/{PAK}", rel=PAK, base=BaseRef(stored, f"o1/{PAK}", "stored", PAK),
                      stored_path=stored, mode=mode, new=new)


def test_pak_member_edit(tmp_path, fake):
    inp = pak_input(tmp_path, [(H1, [C("rate", "10.0", "4.0", kind="F32")]), (H3, [C("n", "1", "1")])])
    got = build_item(inp, tmp_path / "stage", 7)
    assert got.item.out.file == "edits/0007_data_mod.pak" and got.item.out.sha1 == sha1(got.out_path)
    back, old = PakFile(got.out_path), PakFile(inp.stored_path)
    assert back.order == old.order
    assert read_fake(back.read_hash(H1)) == {"rate": "4.0"}
    assert back.read_hash(H2) == old.read_hash(H2) and back.read_hash(H3) == old.read_hash(H3)
    assert all(a & 0xF == COMPRESSION_DEFLATE for a in back.attrs)
    assert got.item.base.sha1 == sha1(inp.stored_path)


def test_pak_member_gone(tmp_path, fake):
    inp = pak_input(tmp_path, [(0x99, [C("rate", "1", "2")]), (H1, [C("rate", "10.0", "4.0", kind="F32")])],
                    mode="carry")
    got = build_item(inp, tmp_path / "stage", 1)
    gone, kept = got.item.members
    assert not gone.changes and [(x.why, x.now) for x in gone.conflicts] == [("gone", None)]
    assert kept.changes and got.out_path is not None
    with pytest.raises(EditError, match="그사이 바뀌었습니다"):
        build_item(pak_input(tmp_path, [(0x99, [C("rate", "1", "2")])]), tmp_path / "stage2", 1)
    inp = pak_input(tmp_path, [(0x99, [C("rate", "1", "2")])], mode="carry")  # its earlier conflicts are gone too
    inp.item.members[0].conflicts = [Conflict(C("x", "1", "2"), "7", "changed")]
    got = build_item(inp, tmp_path / "stage3", 1)
    assert [(x.change.at, x.why, x.now) for x in got.item.members[0].conflicts] == [("x", "gone", None),
                                                                                   ("rate", "gone", None)]
    assert got.notes == [f"{PAK} #0000000000000099: 확인할 값 1개"]


def test_pak_that_cannot_be_rebuilt(tmp_path, fake):
    dup = [(H1, fake_file({"rate": ("F32", "10.0")})), (H1, fake_file({"rate": ("F32", "10.0")}))]
    inp = pak_input(tmp_path, [(H1, [C("rate", "10.0", "4.0", kind="F32")])], mode="reapply", entries=dup)
    got = build_item(inp, tmp_path / "stage", 1)
    assert "다시 만들 수 없는 형식" in got.item.members[0].pending and got.out_path is None
    inp = pak_input(tmp_path, [(H1, [C("rate", "10.0", "4.0", kind="F32")])], entries=dup)
    with pytest.raises(EditError, match="다시 만들 수 없는 형식"):
        build_item(inp, tmp_path / "stage", 1)


def test_pak_commit_that_only_takes_values_away(tmp_path, fake):
    """#4 (I9): a PAK that cannot be written again, or a member that does not open: a save that only
    takes values away from it is saved, the member waits; another member's new value is still applied."""
    dup = [(H1, fake_file({"rate": ("F32", "10.0")})), (H1, fake_file({"rate": ("F32", "10.0")}))]
    inp = pak_input(tmp_path, [(H1, [C("rate", "10.0", "4.0", kind="F32")])], entries=dup, new=frozenset())
    got = build_item(inp, tmp_path / "stage", 1)
    assert "다시 만들 수 없는 형식" in got.item.members[0].pending and got.out_path is None
    assert got.item.members[0].changes == [C("rate", "10.0", "4.0", kind="F32")]
    entries = [(H1, fake_file({"rate": ("F32", "10.0")}, stale=True)), (H3, fake_file({"n": "1"}))]
    inp = pak_input(tmp_path, [(H1, [C("rate", "10.0", "4.0", kind="F32")]), (H3, [C("n", "1", "2")])],
                    entries=entries, new=frozenset({(pakedit.member_key(H3), "n")}))
    got = build_item(inp, tmp_path / "stage2", 2)
    stale, ok = got.item.members
    assert stale.pending and stale.changes and not ok.pending
    assert read_fake(PakFile(got.out_path).read_hash(H3)) == {"n": "2"}


def test_pak_member_that_does_not_unpack(tmp_path, fake):
    """#25: an entry whose data is cut short waits with a message (re-apply) or stops the save, as an
    EditError and not zlib's own error."""
    data = fake_file({f"v{i}": str(i * 7919 % 1000) for i in range(400)})
    for n, (mode, new) in enumerate((("reapply", frozenset()), ("commit", None))):
        inp = pak_input(tmp_path / str(n), [(H1, [C("v1", "919", "5")])], mode=mode, entries=[(H1, data)], new=new)
        packed = PakFile(inp.stored_path).entry(0)[1]
        raw = inp.stored_path.read_bytes()
        inp.stored_path.write_bytes(raw[:len(raw) - packed // 2])
        if mode == "commit":
            with pytest.raises(EditError, match="PAK 안의 파일을 풀지 못했습니다"):
                build_item(inp, tmp_path / "stage", 1)
            continue
        got = build_item(inp, tmp_path / "stage", 1)
        m = got.item.members[0]
        assert m.pending.startswith("PAK 안의 파일을 풀지 못했습니다") and m.changes == [C("v1", "919", "5")]
        assert got.out_path is None


def test_pak_without_disk_space(tmp_path, fake, monkeypatch):
    def full(folder, need):
        raise EditError("저장 공간이 모자라 PAK을 다시 만들지 않았습니다 (1 MB 필요 · 남은 공간 0 B)")

    monkeypatch.setattr(pakedit, "check_space", full)
    inp = pak_input(tmp_path, [(H1, [C("rate", "10.0", "4.0", kind="F32")])], mode="carry")
    got = build_item(inp, tmp_path / "stage", 1)
    m = got.item.members[0]
    assert m.pending.startswith("저장 공간이 모자라") and m.changes == [C("rate", "10.0", "4.0", kind="F32")]
    assert got.out_path is None and not list((tmp_path / "stage").rglob("*.pak"))
    with pytest.raises(EditError, match="저장 공간"):
        build_item(pak_input(tmp_path, [(H1, [C("rate", "10.0", "4.0", kind="F32")])]), tmp_path / "stage", 1)


def test_pak_commit_that_only_takes_values_away_without_disk_space(tmp_path, fake, monkeypatch):
    """#4 (I9): a PAK waits for room (an update could not rebuild it) and the user takes one of two values
    back while there is still none: the save goes through, every member waits as given, nothing is
    written; a save that sets a value of any member of the PAK still stops.  The same for a full disk
    while the PAK is written."""
    def full(folder, need):
        raise EditError("저장 공간이 모자라 PAK을 다시 만들지 않았습니다 (1 MB 필요 · 남은 공간 0 B)")

    room = pakedit.check_space
    monkeypatch.setattr(pakedit, "check_space", full)
    members = [(H1, [C("rate", "10.0", "4.0", kind="F32")]), (H3, [C("n", "1", "2")])]
    got = build_item(pak_input(tmp_path, members, new=frozenset()), tmp_path / "stage", 1)
    assert [(m.pending.startswith("저장 공간이 모자라"), m.changes) for m in got.item.members] == [
        (True, members[0][1]), (True, members[1][1])]
    assert got.out_path is None and got.item.out is None and not list((tmp_path / "stage").rglob("*.pak"))
    assert len(got.notes) == 2
    with pytest.raises(EditError, match="저장 공간"):
        build_item(pak_input(tmp_path, members, new=frozenset({(pakedit.member_key(H3), "n")})),
                   tmp_path / "stage", 1)
    monkeypatch.setattr(pakedit, "check_space", room)
    written = pakedit.write_pak

    def cut(rows, target):
        written(rows[:1], target)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pakedit, "write_pak", cut)
    got = build_item(pak_input(tmp_path, members, new=frozenset()), tmp_path / "stage", 2)
    assert all(m.pending == "바꾼 파일을 저장하지 못했습니다: [Errno 28] No space left on device"
               for m in got.item.members)
    assert got.out_path is None and not list((tmp_path / "stage").rglob("*.pak"))
    with pytest.raises(OSError):
        build_item(pak_input(tmp_path, members), tmp_path / "stage", 3)


def test_pak_member_stale_in_reapply(tmp_path, fake):
    entries = [(H1, fake_file({"rate": ("F32", "10.0")}, stale=True)), (H3, fake_file({"n": "1"}))]
    inp = pak_input(tmp_path, [(H1, [C("rate", "10.0", "4.0", kind="F32")]), (H3, [C("n", "1", "2")])],
                    mode="reapply", entries=entries)
    got = build_item(inp, tmp_path / "stage", 1)
    stale, ok = got.item.members
    assert stale.pending and stale.changes and not ok.pending
    assert read_fake(PakFile(got.out_path).read_hash(H3)) == {"n": "2"}


# ---- GC ------------------------------------------------------------------------------------------------------
def test_collect_keeps_the_record_and_one_generation(tmp_path):
    edits = tmp_path / "edits"
    edits.mkdir()
    for name in ("0001_a.user.3", "0002_a.user.3", "0003_b.pak", "0004_c.lua", "0005_x.mhwmm-part"):
        (edits / name).write_bytes(b"x")
    (edits / "sub").mkdir()
    rec = Record(next=5, items=[Item(Source("", 0, "c"), members=[Member("", "lua", "", [C("a", "1", "2")])],
                                     out=Out("edits/0004_c.lua", 1, "s"))])
    gone = collect(tmp_path, rec, keep={"edits/0003_b.pak"})
    assert sorted(gone) == ["edits/0001_a.user.3", "edits/0002_a.user.3", "edits/0005_x.mhwmm-part"]
    assert sorted(os.listdir(edits)) == ["0003_b.pak", "0004_c.lua", "sub"]
    assert collect(tmp_path, rec) == ["edits/0003_b.pak"]  # at startup: nothing kept beyond the record
    assert collect(tmp_path, Record(error="unreadable")) == []  # a newer manager's outputs stay
    assert (edits / "0004_c.lua").exists()
    assert collect(tmp_path / "nowhere", rec) == []
