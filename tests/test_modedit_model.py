"""The 모드 수정 record (edits.json), its rules, option scopes and the window's unsaved changes."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mhwmm.modedit import EDITS_FORMAT, EDITS_RECORD, EditError, model
from mhwmm.modedit.model import (Base, Change, Conflict, EditPlan, Item, Member, MemberPlan, Out, Pending, Record,
                                 Source)
from test_modedit_engines import fake_rule_of

DATA = "natives/STM/GameDesign/Common/Item/ItemData.user.3"
PAK = "hr_xp.pak"
LUA = "reframework/autorun/NierUI/Config.lua"
HASH = "#5B1E0F9C3A7D2E41"


@pytest.fixture(autouse=True)
def listing(monkeypatch):
    """textconfig.listed's rule for the files of these tests (the real one: test_listed_comes_from_textconfig)."""
    monkeypatch.setattr(model, "config_engine", lambda rel: "lua" if rel.startswith("reframework/autorun/")
                        and rel.endswith(".lua") else "ini" if rel.endswith(".ini") and "/" not in rel else None)
    monkeypatch.setattr(model, "_rule_of", fake_rule_of)


def sample() -> Record:
    """The record of the spec's example (§2.2)."""
    return Record(rev=5, next=10, items=[
        Item(Source("Blue", 0, "natives/STM/Sound/Wwise/bgm_field_03.sbnk.1.x64"), exclude=True),
        Item(Source("", 0, DATA), members=[Member("", "rsz", "ItemData", changes=[
            Change("_Values[*]._SellPrice", "S32", "99999", "5000", "_SellPrice", count=412),
            Change("_Values[_ItemId=101]._BuyPrice", "S32", "1", "100", "_BuyPrice")],
            conflicts=[Conflict(Change("_Values[_ItemId=102]._BuyPrice", "S32", "1", "100", "_BuyPrice"), "50",
                                "changed")])],
            base=Base(DATA, 1203456, 1761600000000000000, "3f", "3f"), out=Out("edits/0009_ItemData.user.3", 1203456, "9a")),
        Item(Source("x10", 0, PAK), members=[Member(HASH, "rsz", "RewardRateData", changes=[
            Change("_HunterRankRate", "F32", "10.0", "4.0", "_HunterRankRate",
                   how={"mode": "change", "k": 0.333333, "game": "1.0"})])],
            base=Base("matfix/o3/hr_xp.pak", 41233, 1761600000000000000, "aa", "bb"),
            out=Out("edits/0007_hr_xp.pak", 41233, "cc")),
        Item(Source("", 0, LUA), members=[Member("", "lua", "Config.lua", changes=[
            Change("Config.UI_Scale", "float", "1.0", "1.25", "UI_Scale", raw="1.0")])],
            base=Base(LUA, 911, 1761600000000000000, "dd", "dd"), out=Out("edits/0008_Config.lua", 913, "ee")),
    ], notice={"kind": "update", "at": "2026-10-28T21:14:05", "kept": 3, "reapplied": 1, "conflicts": 1,
               "pending": 0, "dropped": ["natives/STM/Art/Model/x/old.tex.241106027"]})


# ---- the record --------------------------------------------------------------------------------------
def test_round_trip_and_read_back(tmp_path):
    rec = sample()
    data = rec.to_dict()
    assert data["format"] == EDITS_FORMAT and data["items"][0] == {
        "option": "Blue", "option_n": 0, "rel": "natives/STM/Sound/Wwise/bgm_field_03.sbnk.1.x64", "exclude": True}
    assert data["items"][1]["members"][0]["conflicts"][0]["now"] == "50"
    back = Record.from_dict(json.loads(json.dumps(data)))
    assert not back.error and back == rec
    assert back.items[2].members[0].changes[0].how == {"mode": "change", "k": 0.333333, "game": "1.0"}
    model.save(tmp_path, rec)
    loaded = model.load(tmp_path)
    assert loaded == rec and loaded.to_dict() == data
    assert rec.outputs() == {"edits/0009_ItemData.user.3", "edits/0007_hr_xp.pak", "edits/0008_Config.lua"}


def test_absent_record_is_empty_and_empty_save_deletes(tmp_path):
    rec = model.load(tmp_path)
    assert rec == Record() and rec.empty() and not rec.error
    model.save(tmp_path, sample())
    assert (tmp_path / EDITS_RECORD).exists()
    model.save(tmp_path, Record(rev=6, next=12))
    assert not (tmp_path / EDITS_RECORD).exists()
    assert not Record(notice={"kind": "update"}).empty()


@pytest.mark.parametrize("content, why", [
    (b"{not json", "손상"),
    (b"\xff\xfe\x00", "손상"),
    (b"[1, 2]", "JSON 객체가 아닙니다"),
    (b'{"items": []}', "형식 번호"),
    (b'{"format": true, "items": []}', "형식 번호"),
])
def test_unreadable_records(tmp_path, content, why):
    (tmp_path / EDITS_RECORD).write_bytes(content)
    rec = model.load(tmp_path)
    assert why in rec.error and not rec.items and not rec.empty()
    with pytest.raises(EditError):
        model.save(tmp_path, rec)
    assert (tmp_path / EDITS_RECORD).read_bytes() == content  # never written, never moved


def test_newer_record(tmp_path):
    data = sample().to_dict()
    data["format"] = EDITS_FORMAT + 1
    (tmp_path / EDITS_RECORD).write_text(json.dumps(data))
    rec = model.load(tmp_path)
    assert rec.newer and "더 새로운 버전" in rec.error and not rec.items


def _broken(change):
    data = sample().to_dict()
    change(data)
    return Record.from_dict(json.loads(json.dumps(data)))


@pytest.mark.parametrize("change", [
    lambda d: d["items"][0].update(exclude="yes"),
    lambda d: d["items"][0].update(option_n=-1),
    lambda d: d["items"][0].update(rel="../outside.dll"),
    lambda d: d["items"][0].update(rel="C:/Windows/x.dll"),
    lambda d: d["items"][0].update(rel="natives\\STM\\x.tex"),
    lambda d: d["items"][1]["members"][0]["changes"][0].update(after=5000),
    lambda d: d["items"][1]["members"][0]["changes"][0].pop("at"),
    lambda d: d["items"][1]["members"][0]["conflicts"][0].update(why="whatever"),
    lambda d: d["items"][1]["members"][0].update(engine=""),
    lambda d: d["items"][1]["out"].update(file="../../game/evil.dll"),
    lambda d: d["items"][1]["out"].update(file="edits/sub/0001_x"),
    lambda d: d["items"][1]["base"].update(size="12"),
    lambda d: d["items"].append(dict(d["items"][0])),
    lambda d: d["items"].append("item"),
    lambda d: d.update(rev=-1),
    lambda d: d.update(notice=[]),
])
def test_malformed_items_make_the_whole_record_unreadable(change):
    rec = _broken(change)
    assert rec.error.startswith("기록의 내용이 맞지 않습니다") and not rec.items  # nothing dropped silently


def test_set_aside_naming(tmp_path):
    assert model.set_aside(tmp_path) is None
    (tmp_path / EDITS_RECORD).write_text("{bad")
    first = model.set_aside(tmp_path)
    assert re.fullmatch(r"edits\.json\.bad-\d{8}-\d{6}", first.name) and first.read_text() == "{bad"
    assert not (tmp_path / EDITS_RECORD).exists()
    (tmp_path / EDITS_RECORD).write_text("{bad again")
    second = model.set_aside(tmp_path)
    assert second != first and second.exists() and first.exists()


def test_numbers_are_never_reused(tmp_path):
    rec = sample()
    assert model.number_from(tmp_path, rec) == 10
    assert model.number_from(tmp_path, Record()) == 1
    (tmp_path / "edits").mkdir()
    (tmp_path / "edits" / "0041_left.user.3").write_bytes(b"x")  # a previous generation kept for workers
    assert model.number_from(tmp_path, Record()) == 42
    assert model.number_from(tmp_path, Record(next=50)) == 50
    assert model.output_name(7, "natives/STM/a/ItemData.user.3") == "edits/0007_ItemData.user.3"
    assert model.output_name(12345, "x.pak") == "edits/12345_x.pak"


# ---- rules ---------------------------------------------------------------------------------------------
def rec_of(*items) -> Record:
    return Record(items=list(items))


def data_item(rel=DATA, member="", engine="rsz", changes=None, **kw):
    changes = [Change("_A", "S32", "1", "2")] if changes is None else changes
    return Item(Source("", 0, rel), members=[Member(member, engine, "", changes)], **kw)


def test_normalize_keeps_a_good_record():
    rec = sample()
    assert model.normalize(rec) == rec


def test_normalize_member_rules():
    model.normalize(rec_of(data_item("natives/STM/x/Y.USER.2")))
    with pytest.raises(EditError, match="natives 폴더의 .user"):
        model.normalize(rec_of(data_item("reframework/data/x.user.3")))
    with pytest.raises(EditError, match="natives 폴더의 .user"):
        model.normalize(rec_of(data_item("natives/STM/x/a.pfb.17")))
    with pytest.raises(EditError, match="natives 폴더의 .user"):
        model.normalize(rec_of(data_item("sub/inner.pak", member=HASH)))
    with pytest.raises(EditError, match="파일 안 항목"):
        model.normalize(rec_of(data_item(member=HASH)))
    with pytest.raises(EditError, match="PAK 안 항목"):
        model.normalize(rec_of(data_item(PAK, member="")))
    with pytest.raises(EditError, match="PAK 안 항목"):
        model.normalize(rec_of(data_item(PAK, member="#123")))
    got = model.normalize(rec_of(data_item(PAK, member=HASH.lower())))
    assert got.items[0].members[0].member == HASH
    model.normalize(rec_of(data_item(LUA, engine="lua")))
    model.normalize(rec_of(data_item("plugin.ini", engine="ini")))
    with pytest.raises(EditError, match="스크립트 설정으로 고칠 수 없는"):
        model.normalize(rec_of(data_item("reframework/autorun/a.lua", engine="json")))
    with pytest.raises(EditError, match="스크립트 설정으로 고칠 수 없는"):
        model.normalize(rec_of(data_item("natives/STM/a.lua", engine="lua")))
    with pytest.raises(EditError, match="알 수 없는 수정 종류"):
        model.normalize(rec_of(data_item(engine="msg")))


def test_normalize_value_rules():
    with pytest.raises(EditError, match="두 번"):
        model.normalize(rec_of(data_item(changes=[Change("_A", "S32", "1", "2"), Change("_A", "S32", "1", "3")])))
    with pytest.raises(EditError, match="두 번"):
        item = data_item()
        item.members[0].conflicts.append(Conflict(Change("_A", "S32", "1", "5"), "3", "changed"))
        model.normalize(rec_of(item))
    # two rules on one field, for different values
    model.normalize(rec_of(data_item(changes=[Change("_V[*]._P", "S32", "1", "2"),
                                              Change("_V[*]._P", "S32", "5", "6")])))
    with pytest.raises(EditError, match="규칙은 데이터 파일"):
        model.normalize(rec_of(data_item(LUA, engine="lua", changes=[Change("a[*].b", "int", "1", "2")])))
    with pytest.raises(EditError, match="두 번"):
        model.normalize(rec_of(data_item(), data_item()))
    with pytest.raises(EditError, match="같은 파일 안 항목"):
        item = data_item(PAK, member=HASH)
        item.members.append(Member(HASH.lower(), "rsz", "", [Change("_B", "S32", "1", "2")]))
        model.normalize(rec_of(item))
    with pytest.raises(EditError, match="경로"):
        model.normalize(rec_of(Item(Source("", 1, DATA), exclude=True)))
    with pytest.raises(EditError):
        model.normalize(Record(error="x"))


def test_normalize_drops_empty_and_keeps_excluded_members():
    empty = Item(Source("", 0, DATA), members=[Member("", "rsz", "", [])], base=Base(DATA, 1, 1, "a"),
                 out=Out("edits/0001_x", 1, "b"))
    excluded = Item(Source("o", 0, "natives/STM/a.tex.1"), exclude=True,
                    base=Base("o1/natives/STM/a.tex.1", 1, 1, "a"))
    kept = Item(Source("", 0, LUA), exclude=True, members=[Member("", "lua", "", [Change("a", "int", "1", "2")])])
    got = model.normalize(rec_of(empty, excluded, kept))
    assert [i.source.rel for i in got.items] == ["natives/STM/a.tex.1", LUA]
    assert got.items[0].base is None and got.items[0].out is None  # exclude-only: no base, no output
    assert got.items[1].members[0].changes  # an excluded item keeps its members (inactive meanwhile)


def test_merge():
    rec = sample()
    lua = Source("", 0, LUA)
    data = Source("", 0, DATA)
    tex = Source("Blue", 0, "natives/STM/x.tex.1")
    plan = EditPlan("m", "s", 5, exclude={tex: True, Source("Blue", 0, "natives/STM/Sound/Wwise/bgm_field_03.sbnk.1.x64"): False},
                    members={(lua, ""): MemberPlan("lua", "Config.lua", ()),
                             (data, ""): MemberPlan("rsz", "", (Change("_A", "S32", "1", "2"),))},
                    drop=frozenset({Source("x10", 0, PAK)}))
    got = model.merge(rec, plan)
    assert [(i.source.rel, i.exclude) for i in got.items] == [(DATA, False), ("natives/STM/x.tex.1", True)]
    assert got.items[0].members[0].label == "ItemData" and [c.at for c in got.items[0].members[0].changes] == ["_A"]
    assert got.rev == 5 and got.next == 10 and got.notice == rec.notice
    assert sample() == rec  # the input is not changed
    reset = model.merge(rec, EditPlan("m", "s", 5, reset=True, exclude={tex: True}))
    assert [i.source for i in reset.items] == [tex] and reset.next == 10 and not reset.notice
    bad = Record(error="broken")
    with pytest.raises(EditError):
        model.merge(bad, EditPlan("m", "s", 0, exclude={tex: True}))
    assert model.merge(bad, EditPlan("m", "s", 0, clear_bad=True)) == Record()
    assert model.merge(bad, EditPlan("m", "s", 0, reset=True)) == Record()


def test_listed_comes_from_textconfig(monkeypatch):
    textconfig = pytest.importorskip("mhwmm.modedit.textconfig")
    if not hasattr(textconfig, "listed"):
        pytest.skip("textconfig.listed is not there yet")
    monkeypatch.undo()
    assert model.config_engine("reframework/autorun/NierUI/Config.lua") == "lua"
    assert model.config_engine("natives/STM/x.lua") is None


# ---- option scopes ---------------------------------------------------------------------------------------
def opts(*names):
    return [{"id": f"o{i}", "name": n} for i, n in enumerate(names, 1)]


def test_resolve_option_by_name_and_n():
    o = opts("Red", "Blue", "Red")
    assert model.resolve_option(o, "Red", 0) == "o1"
    assert model.resolve_option(o, "Red", 1) == "o3"
    assert model.resolve_option(o, "Red", 2) is None
    assert model.resolve_option(o, "Green", 0) is None
    assert model.option_ref(o, "o3") == ("Red", 1) and model.option_ref(o, "o2") == ("Blue", 0)
    with pytest.raises(EditError):
        model.option_ref(o, "o9")
    # an option inserted by an update: ids move, names do not
    old, new = opts("Blue", "Red"), opts("Blue", "Green", "Red")
    name, n = model.option_ref(old, "o2")
    assert model.resolve_option(new, name, n) == "o3"
    src = model.source_of(old, "o2/natives/STM/x.tex.1")
    assert src == Source("Red", 0, "natives/STM/x.tex.1") and model.key_of(new, src) == "o3/natives/STM/x.tex.1"


def test_resolve_option_layout_changes():
    assert model.resolve_option([], "", 0) == ""
    assert model.resolve_option([], "Red", 0) is None  # the mod lost its options
    assert model.resolve_option(opts("Red"), "", 0) is None  # the mod gained options
    assert model.key_of([], Source("", 0, DATA)) == DATA
    assert model.key_of(opts("Red"), Source("", 0, DATA)) is None
    assert model.source_of([], DATA) == Source("", 0, DATA)
    assert model.option_ref([], "") == ("", 0)


def test_exclusion_digest():
    assert model.exclusion_digest([]) == ""
    d = model.exclusion_digest(["o1/b", "a"])
    assert re.fullmatch(r"[0-9a-f]{16}", d)
    assert d == model.exclusion_digest(["a", "o1/b", "a"]) != model.exclusion_digest(["a"])


def test_source_label():
    assert Source("", 0, DATA).label() == DATA
    assert Source("Blue", 0, "x").label() == "Blue: x"
    assert Source("Blue", 1, "x").label() == "Blue (2번째): x"


# ---- Pending ---------------------------------------------------------------------------------------------------
S = Source("", 0, DATA)


def test_pending_set_and_revert_single_values():
    p = Pending(sample())
    assert p.count() == 0 and p.plan("m", "st", 5).empty()
    p.set_value(S, "", "rsz", "ItemData", at="_Values[_ItemId=7]._BuyPrice", kind="S32", before="3", after="9")
    assert p.count() == 1
    mp = p.member(S, "")
    assert mp.changes[-1] == Change("_Values[_ItemId=7]._BuyPrice", "S32", "3", "9", "_BuyPrice")
    # set back to the value in the file: the change goes, and so does the entry
    p.set_value(S, "", "rsz", "ItemData", at="_Values[_ItemId=7]._BuyPrice", kind="S32", before="3", after="3")
    assert p.count() == 0 and p.member(S, "") == MemberPlan.of(sample().items[1].members[0])
    # change a recorded value in place (record order kept), then revert it
    p.set_value(S, "", "rsz", "", at="_Values[_ItemId=101]._BuyPrice", kind="S32", before="1", after="7")
    assert [c.after for c in p.member(S, "").changes] == ["5000", "7"]
    p.revert_value(S, "", "_Values[_ItemId=101]._BuyPrice")
    assert [c.at for c in p.member(S, "").changes] == ["_Values[*]._SellPrice"] and p.count() == 1
    plan = p.plan("m", "st", 5)
    assert plan.count() == 1 and (S, "") in plan.members


def test_pending_value_on_a_conflict_replaces_it():
    p = Pending(sample())
    p.set_value(S, "", "rsz", "", at="_Values[_ItemId=102]._BuyPrice", kind="S32", before="50", after="80")
    mp = p.member(S, "")
    assert not mp.conflicts and mp.changes[-1].before == "50" and mp.changes[-1].after == "80"


def test_pending_rules():
    p = Pending(sample())
    # the same rule again (its row count aside) is no change
    p.set_value(S, "", "rsz", "", at="_Values[*]._SellPrice", kind="S32", before="99999", after="5000", rule=True)
    assert p.count() == 0
    # a rule replaces the single values it covers (same table field, same value in the file)
    p.set_value(S, "", "rsz", "", at="_Values[_ItemId=5]._BuyPrice", kind="S32", before="1", after="3")
    p.set_value(S, "", "rsz", "", at="_Values[*]._BuyPrice", kind="S32", before="1", after="100", rule=True)
    ats = [(c.at, c.before) for c in p.member(S, "").changes]
    assert ats == [("_Values[*]._SellPrice", "99999"), ("_Values[*]._BuyPrice", "1")]
    # 원래 값으로 on a row the rule covers removes the rule
    p.revert_value(S, "", "_Values[_ItemId=33]._BuyPrice")
    assert [c.at for c in p.member(S, "").changes] == ["_Values[*]._SellPrice"]
    # and on a rule address, only the rule for that value when it is named
    p.set_value(S, "", "rsz", "", at="_Values[*]._SellPrice", kind="S32", before="1", after="2", rule=True)
    p.revert_value(S, "", "_Values[*]._SellPrice", before="1")
    assert [(c.at, c.before) for c in p.member(S, "").changes] == [("_Values[*]._SellPrice", "99999")]
    with pytest.raises(EditError):
        p.set_value(S, "", "rsz", "", at="_Values[1]._X", kind="S32", before="1", after="2", rule=True)
    with pytest.raises(EditError):
        p.set_value(S, "", "rsz", "", at="_Values[*]._X", kind="S32", before="1", after="2")


def test_pending_conflicts_keep_mine_and_take_author():
    at = "_Values[_ItemId=102]._BuyPrice"
    p = Pending(sample())
    p.keep_mine(S, "", at)
    mp = p.member(S, "")
    assert not mp.conflicts and mp.changes[-1] == Change(at, "S32", "50", "100", "_BuyPrice")
    p = Pending(sample())
    p.take_author(S, "", at)
    mp = p.member(S, "")
    assert not mp.conflicts and [c.at for c in mp.changes] == ["_Values[*]._SellPrice", "_Values[_ItemId=101]._BuyPrice"]
    assert p.count() == 1
    with pytest.raises(EditError):
        p.take_author(S, "", at)  # nothing left to resolve
    gone = sample()
    gone.items[1].members[0].conflicts = [Conflict(Change("_X", "S32", "1", "2"), None, "gone")]
    p = Pending(gone)
    with pytest.raises(EditError, match="내 값을 유지할 수 없습니다"):
        p.keep_mine(S, "", "_X")
    p.take_author(S, "", "_X")  # 지우기
    assert not p.member(S, "").conflicts


def test_pending_exclude_drop_reapply_reset():
    rec = sample()
    blue = rec.items[0].source
    tex = Source("Blue", 0, "natives/STM/x.tex.1")
    p = Pending(rec)
    assert p.excluded(blue) and not p.excluded(tex)
    p.exclude(blue, True)
    assert p.count() == 0  # equal to the record
    p.exclude(tex, True)
    p.exclude(blue, False)
    assert p.excluded(tex) and not p.excluded(blue) and p.count() == 2 and p.touched(tex)
    p.exclude(tex, False)
    assert p.count() == 1 and not p.touched(tex)
    lua = Source("", 0, LUA)
    p.set_value(lua, "", "lua", "", at="Config.UI_Scale", kind="float", before="1.0", after="2.0")
    p.drop(lua)
    assert p.member(lua, "") is None and p.count() == 2
    p.reapply(S)
    plan = p.plan("m", "st", 5)
    assert plan.drop == {lua} and plan.reapply == {S} and plan.exclude == {blue: False} and plan.count() == 3
    # dropping a file that only had pending entries leaves nothing behind
    p.set_value(tex, "", "rsz", "", at="_A", kind="S32", before="1", after="2")
    p.drop(tex)
    assert tex not in p.plan("m", "st", 5).drop
    p.reset_all()
    assert p.count() == 1 and p.member(S, "") is None and not p.excluded(blue)
    plan = p.plan("m", "st", 5)
    assert plan.reset and not plan.drop and not plan.exclude and not plan.reapply
    # after a reset, entries are relative to an empty record
    p.exclude(blue, True)
    assert p.plan("m", "st", 5).exclude == {blue: True}
    assert model.merge(rec, p.plan("m", "st", 5)).items[0].source == blue


def test_pending_clear_bad():
    p = Pending(sample())
    p.clear_bad()
    assert p.count() == 0  # nothing to clear on a readable record
    p = Pending(Record(error="broken"))
    p.clear_bad()
    plan = p.plan("m", "st", 0)
    assert plan.clear_bad and plan.count() == 1
    p.reset_all()
    plan = p.plan("m", "st", 0)
    assert plan.reset and not plan.clear_bad and plan.count() == 1


def test_pending_from_plan_restores_everything():
    rec = sample()
    p = Pending(rec)
    blue = rec.items[0].source
    p.exclude(blue, False)
    p.set_value(S, "", "rsz", "", at="_Values[_ItemId=9]._BuyPrice", kind="S32", before="1", after="2",
                how={"mode": "times", "k": 2})
    p.keep_mine(S, "", "_Values[_ItemId=102]._BuyPrice")
    p.reapply(Source("x10", 0, PAK))
    p.drop(Source("", 0, LUA))
    plan = p.plan("m", "st", 5)
    again = Pending.from_plan(rec, plan)
    assert again.plan("m", "st", 5) == plan and again.count() == p.count() == 5
    assert again.member(S, "").changes[-2].how == {"mode": "times", "k": 2}
    assert model.merge(rec, plan) == model.merge(rec, again.plan("m", "st", 5))


def test_edit_plan_count():
    s = Source("", 0, "a.dll")
    assert EditPlan("m", "s", 0).count() == 0 and EditPlan("m", "s", 0).empty()
    plan = EditPlan("m", "s", 0, exclude={s: True}, members={(S, ""): MemberPlan("rsz", "", ())},
                    reapply=frozenset({S}), drop=frozenset({Source("", 0, "b")}))
    assert plan.count() == 4 and plan.sources() == {s, S, Source("", 0, "b")}
    assert EditPlan("m", "s", 0, reset=True, clear_bad=True).count() == 1


# ---- views ------------------------------------------------------------------------------------------------------
def test_summary_and_carry_texts():
    s = model.EditSummary(excluded=2, values=5, files=1, conflicts=1, pending=0, waiting=0, error="")
    assert s.text() == "뺀 파일 2개 · 바꾼 값 5개 (파일 1개) · 확인할 값 1개" and not s.empty
    assert s.problem() == "확인할 값 1개"
    empty = model.EditSummary(0, 0, 0, 0, 0, 0, "")
    assert empty.empty and empty.problem() == ""
    bad = model.EditSummary(0, 0, 0, 0, 0, 0, "깨짐")
    assert not bad.empty and "깨짐" in bad.problem()
    report = model.CarryReport(kept=3, reapplied=2, conflicts=2, dropped=["x"])
    assert report.text() == "수정을 다시 적용했습니다: 수정 5개 · 확인할 값 2개 · 없어진 파일 1개"
    notice = report.notice("2026-10-28T21:14:05")
    assert model.CarryReport.from_notice(notice) == report and report.problems == 3
    assert model.CarryReport.from_notice({"kept": "x", "dropped": "y"}) == model.CarryReport()
    failed = model.CarryReport(error="수정 기록을 읽지 못해 다시 적용하지 않았습니다: 깨짐")
    assert failed.text() == failed.error and failed.problems == 1 and "error" not in notice
    assert model.CarryReport.from_notice(failed.notice("2026-10-28T21:14:05")) == failed


def test_conflict_texts():
    c = Change("_A", "S32", "1", "100")
    assert Conflict(c, "50", "changed").text() == "작성자가 바꾼 값: 1 → 50 (내 값 100)"
    assert Conflict(c, None, "gone").text() == "없어진 값입니다 (내 값 100)"
    assert Conflict(c, "1", "readonly").text() == "바꿀 수 없는 값이 되었습니다"
    assert Conflict(c, "1", "kind").text() == "값의 종류가 바뀌었습니다"
    assert Conflict(c, None, "norow").text() == "값이 1인 행이 없습니다"


def test_snapshot_types_build(tmp_path: Path):
    ref = model.BaseRef(tmp_path / "x", DATA, "stored", DATA)
    target = model.EditTarget(S, DATA, "", "rsz", "data", "ItemData", 10, False)
    view = model.ValueView(target, [], 0, False, False, [], "", ref)
    assert view.data_files == [] and target.readonly == ""
    assert view.keyed == frozenset()  # #11: no value may be scaled by the game's value unless the manager says so
    keyed = model.ValueView(target, [], 0, True, False, [], "", ref, keyed=frozenset({"_Values[_ItemId=1]._Price"}))
    assert keyed.keyed == {"_Values[_ItemId=1]._Price"}
