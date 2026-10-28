"""게임 수치 (mhwmm/modedit/rszdata.py): RSZ data files opened exactly, values addressed and changed
in place under the V1-V5 checks, the selfcheck, comparing with the game's own copy, and the "rsz"
engine.  Synthetic files only (tests/modedit_fixtures.py, a made-up registry); real files only with
MHWMM_RSZ_SAMPLES (a 재질 진단 zip's folder)."""

from __future__ import annotations

import logging
import math
import os
import struct
import weakref
from pathlib import Path

import pytest

import equip_game_fixtures as fx
import modedit_fixtures as mf
from mhwmm.matfix import rsz
from mhwmm.modedit import EditError, StaleError, engines
from mhwmm.modedit import rszdata as R
from mhwmm.modedit.model import Change, Conflict

REG = mf.registry()
ITEMS = "natives/STM/GameDesign/Common/Item/ItemData.user.3"


def open_doc(data: bytes, **kwargs) -> R.DataFile:
    return R.DataFile(data, REG, **kwargs)


def values(data: bytes) -> dict[str, object]:
    return {v.at: v for v in open_doc(data).values()}


@pytest.fixture
def engine(monkeypatch):
    """The "rsz" engine with the made-up registry in place of the bundled one."""
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", REG)
    monkeypatch.setattr(rsz.TypeRegistry, "_from_file", False)
    monkeypatch.setattr(rsz.TypeRegistry, "_holds", 0)
    monkeypatch.setattr(R, "_CHECKED", weakref.WeakKeyDictionary())  # no file checked yet
    return engines.get("rsz")


def differing(a: bytes, b: bytes) -> list[int]:
    return [i for i, (x, y) in enumerate(zip(a, b)) if x != y]


# ---- opening ---------------------------------------------------------------------------------------
def test_opens_a_file_that_reads_exactly():
    doc = open_doc(mf.item_data(3))
    assert doc.type_name == mf.ITEM and doc.notes == [] and not doc.readonly
    assert len(doc.values()) == 3 * 11 + 4 and not doc.truncated  # 11 fields + _GetRank's elements (1, 2, 1)


def test_stale_file_raises_stale_error():
    with pytest.raises(StaleError) as err:
        open_doc(mf.stale_item_data())
    assert str(err.value) == R.STALE and isinstance(err.value, EditError)


def test_registry_older_than_the_file_says_so():
    stale = bytearray(mf.stale_item_data())
    info = rsz.parse(bytes(stale))
    for i, (t, _c) in enumerate(info.insts):
        if t == mf.ROW_TYPE:
            struct.pack_into("<I", stale, info.inst_table + 8 * i + 4, 0x7777)  # a CRC no layout is known for
    with pytest.raises(StaleError) as err:
        open_doc(bytes(stale))
    assert str(err.value) == R.STALE + R.REGISTRY_HINT
    without_rows = rsz.TypeRegistry({"types": {k: v for k, v in REG._types.items() if int(k, 16) != mf.ROW_TYPE}})
    with pytest.raises(StaleError, match="새 형식 정보"):
        R.DataFile(mf.item_data(2), without_rows)


@pytest.mark.parametrize("data,why", [
    (mf.item_data(3)[:-3], "정확히 읽지 못했습니다"),  # truncated
    (mf.runtime_file(), "RuntimeType"),
    (b"junk" * 40, "아니어서 열지 않습니다"),
    (fx.pfb_file("Art/x.mesh"), "아니어서 열지 않습니다"),
], ids=["truncated", "runtime-type", "not-rsz", "prefab"])
def test_files_it_cannot_read_are_refused(data, why):
    with pytest.raises(EditError, match=why) as err:
        open_doc(data)
    assert not isinstance(err.value, StaleError)


def test_files_over_the_size_limit_are_refused(monkeypatch):
    data = mf.item_data(3)
    monkeypatch.setattr(R, "MAX_FILE", len(data) - 1)
    with pytest.raises(EditError, match="너무 커서"):
        open_doc(data)


def test_is_data_file():
    assert R.is_data_file(ITEMS) and R.is_data_file("NATIVES\\STM\\x.USER.2")
    assert not R.is_data_file("natives/STM/x.user") and not R.is_data_file("x.user.3")
    assert not R.is_data_file("natives/STM/x.pfb.17")


# ---- values and kinds ------------------------------------------------------------------------------
def test_listing_names_rows_by_their_id():
    rows = values(mf.item_data(3))
    price = rows["_Values[_ItemId=101]._SellPrice"]
    assert (price.label, price.group, price.kind, price.text) == ("_SellPrice", "_Values[_ItemId=101]", "S32", "60")
    assert price.rule_at == "_Values[*]._SellPrice" and price.editable and price.settings
    assert price.spec == ("S32", -(1 << 31), (1 << 31) - 1)
    rank = rows["_Values[_ItemId=101]._GetRank[1]"]
    assert (rank.label, rank.group, rank.rule_at) == ("_GetRank[1]", "_Values[_ItemId=101]",
                                                      "_Values[_ItemId=101]._GetRank[*]")
    assert rows["_Values[_ItemId=100]._Weight"].text == "0.1"  # the shortest text of the float32


@pytest.mark.parametrize("at,text,shown", [
    ("_S8", "-128", "-128"), ("_U8", "255", "255"), ("_S16", "-32768", "-32768"), ("_U16", "0x10", "16"),
    ("_S32", "2147483647", "2147483647"), ("_U32", "4294967295", "4294967295"),
    ("_S64", "-9223372036854775808", "-9223372036854775808"), ("_U64", "18446744073709551615", "18446744073709551615"),
    ("_F16", "0.5", "0.5"), ("_F16", "1000", "1000.0"), ("_F32", "0.1", "0.1"), ("_F32", "0.30000001", "0.3"),
    ("_F32", "1e20", "1e+20"), ("_F32", "-0", "-0.0"), ("_F64", "0.1", "0.1"), ("_F64", "1e300", "1e+300"),
    ("_Bool", "끔", "false"), ("_Bool", "false", "false"), ("_Bool", "0", "false"),
    ("_Vec2", "3,4", "3.0, 4.0"), ("_Vec3", "9, 8, 7", "9.0, 8.0, 7.0"), ("_Vec4", "1 2 3 4", "1.0, 2.0, 3.0, 4.0"),
    ("_Float2", "0.1, 0.2", "0.1, 0.2"), ("_Float3", "1, 2, 3", "1.0, 2.0, 3.0"), ("_Float4", "1,2,3,4", "1.0, 2.0, 3.0, 4.0"),
    ("_Int2", "-1, 1", "-1, 1"), ("_Int3", "-2147483648, 0, 2147483647", "-2147483648, 0, 2147483647"),
    ("_Int4", "1,2,3,4", "1, 2, 3, 4"), ("_Uint2", "0, 4294967295", "0, 4294967295"), ("_Uint3", "7,8,9", "7, 8, 9"),
    ("_Uint4", "1,2,3,4", "1, 2, 3, 4"), ("_Quat", "0, 0.7071068, 0, 0.7071068", "0.0, 0.7071068, 0.0, 0.7071068"),
    ("_Range", "1, 2", "1.0, 2.0"), ("_RangeI", "-5, 5", "-5, 5"), ("_Size", "1920, 1080", "1920.0, 1080.0"),
    ("_Point", "0.5, 0.5", "0.5, 0.5"), ("_Position", "1e300, 0, -1", "1e+300, 0.0, -1.0"),
    ("_Half4", "1, 2, 3, 4", "1.0, 2.0, 3.0, 4.0"), ("_Color", "1, 2, 3, 4", "1, 2, 3, 4"),
    ("_Pos~1", "5.5", "5.5"), ("_Param._Count", "-4", "-4"),
])
def test_every_editable_kind_is_written_in_place(at, text, shown):
    data = mf.kinds_file()
    doc = open_doc(data)
    out = doc.write({at: text})
    assert len(out) == len(data)
    assert open_doc(out).get(at).text == shown
    assert open_doc(out).get(at).kind == doc.get(at).kind
    changed = differing(data, out)
    leaf = doc._find(at)[0][0]
    spans = {}
    inst = doc.insts[leaf.path[0]]
    R.locate(data, inst.start, len(data), inst.fields, doc.schema, REG, (leaf.path[0],), spans)
    start, end = spans[leaf.path]
    assert all(start <= i < end for i in changed)  # only the value's own bytes


@pytest.mark.parametrize("at,text,why", [
    ("_S8", "128", "-128 ~ 127"), ("_U8", "-1", "0 ~ 255"), ("_U16", "65536", "0 ~ 65535"),
    ("_S32", "2147483648", "사이의 정수"), ("_U64", "-1", "사이의 정수"), ("_S32", "1.5", "정수를 적어"),
    ("_S32", "", "정수를 적어"), ("_F32", "1e39", "너무 큰 수"), ("_F16", "70000", "너무 큰 수"),
    ("_F32", "nan", "NaN"), ("_F64", "inf", "NaN"), ("_F32", "x", "숫자를 적어"), ("_Bool", "2", "켬이나 끔"),
    ("_Vec3", "1, 2", "값 3개"), ("_Color", "1, 2, 3, 256", "0 ~ 255"), ("_Color", "1, 2, 3", "색은"),
    ("_Int2", "1.5, 2", "정수를 적어"), ("_Half4", "1, 2, 3, 99999", "너무 큰 수"),
])
def test_values_out_of_range_are_refused(at, text, why):
    doc = open_doc(mf.kinds_file())
    with pytest.raises(EditError, match=why):
        doc.write({at: text})
    with pytest.raises(EditError):
        R.parse_value(doc.get(at).kind, text)


def test_vector_keeps_its_trailing_bytes():
    data = mf.kinds_file()
    out = open_doc(data).write({"_Vec3": "9, 8, 7"})
    pad = data.find(b"\xAB\xCD\xEF\x01")
    assert pad > 0 and out[pad:pad + 4] == b"\xAB\xCD\xEF\x01"
    assert out[pad - 12:pad] == struct.pack("<3f", 9, 8, 7)


def test_float_text_is_the_shortest_that_packs_back():
    assert R.format_value("F32", mf.f32(0.1)) == "0.1"
    assert R.format_value("F32", mf.f32(1 / 3)) == "0.33333334"
    assert R.format_value("F32", 1.0) == "1.0" and R.format_value("F32", 100000.0) == "100000.0"
    assert R.format_value("F64", 0.1) == "0.1" and R.format_value("F64", 1 / 3) == repr(1 / 3)
    assert R.format_value("Vec3", [mf.f32(0.1), 2.0, -0.0]) == "0.1, 2.0, -0.0"
    assert R.format_value("Bool", True) == "true" and R.format_value(" S32", -5) == "-5"
    for x in (0.1, 1 / 3, 1e-40, 3.4e38, 123456.7):
        text = R.format_value("F32", mf.f32(x))
        assert struct.pack("<f", float(text)) == struct.pack("<f", x)


def test_parse_value():
    assert R.parse_value("Bool", "켬") is True and R.parse_value("Bool", "FALSE") is False
    assert R.parse_value("S32", " 0x7f ") == 127 and R.parse_value("S32", "-0x10") == -16
    assert R.parse_value("S32", "+12") == 12 and R.parse_value("U8", "007") == 7
    assert R.parse_value("Vec3", "1,2 ,3") == [1.0, 2.0, 3.0]
    assert R.parse_value("Color", "255, 0, 0, 255") == [255, 0, 0, 255]
    with pytest.raises(EditError, match="아직 바꿀 수 없습니다"):
        R.parse_value("String", "x")


def test_bool_keeps_its_own_true_byte():
    data = bytearray(mf.kinds_file())
    doc = open_doc(bytes(data))
    leaf = doc._find("_Bool")[0][0]
    spans = {}
    inst = doc.insts[leaf.path[0]]
    R.locate(bytes(data), inst.start, len(data), inst.fields, doc.schema, REG, (leaf.path[0],), spans)
    data[spans[leaf.path][0]] = 2  # a non-zero byte other than 1 is true too
    doc = open_doc(bytes(data))
    assert doc.get("_Bool").text == "true"
    assert doc.write({"_Bool": "true"}) == bytes(data)
    doc.selfcheck()


# ---- read-only values --------------------------------------------------------------------------------
def test_enum_wrappers_are_shown_folded_and_read_only():
    rows = values(mf.reward_data(4))
    story = rows["_Values[_dataId=7000,_rewardType=1]._IdStory"]
    assert (story.text, story.kind, story.editable, story.why) == ("200", "S32", False, R.WHY_ENUM)
    assert story.note == mf.ID_WRAPPER
    assert rows["_Values[_dataId=7001,_rewardType=2]._IdEx[1]"].text == "403"
    assert not rows["_Values[_dataId=7001,_rewardType=2]._IdEx[1]"].editable
    with pytest.raises(EditError, match="열거형"):
        open_doc(mf.reward_data(4)).write({"_Values[_dataId=7000,_rewardType=1]._IdStory": "5"})


def test_shared_instances_are_read_only():
    rows = values(mf.shared_file())
    assert rows["_A._Scale"].why == R.WHY_SHARED and rows["_A._Count"].why == R.WHY_SHARED
    assert (rows["_B"].kind, rows["_B"].text, rows["_B"].why) == ("Object", "→ " + mf.PARAM, R.WHY_REF)
    assert rows["_Own._Scale"].editable
    doc = open_doc(mf.shared_file())
    assert doc.get("_B._Scale").why == R.WHY_SHARED  # the same instance reached the other way
    with pytest.raises(EditError, match="함께 쓰는"):
        doc.write({"_B._Scale": "3"})


def test_nan_and_inf_are_read_only():
    rows = values(mf.nan_file())
    assert rows["_F32"].why == R.WHY_NAN and rows["_F32"].text == "nan"
    assert rows["_F64"].why == R.WHY_NAN and rows["_F64"].text == "inf"
    assert rows["_Vec3"].why == R.WHY_NAN and rows["_Vec3"].text == "1.0, nan, 3.0"
    assert rows["_Vec2"].editable


@pytest.mark.parametrize("at,text", [
    ("_C8", "65"), ("_C16", "44032"), ("_Text", "hello"), ("_Path", "@Art/x.tex"),
    ("_Guid", "5eed0000-0000-0000-0000-000000000007"), ("_When", "123456789"),
    ("_GameObject", "5eed0000-0000-0000-0000-000000000009"), ("_Data", "0001020304050607"),
    ("_Mat", ", ".join(f"{i}.0" for i in range(16))), ("_Box", ", ".join(f"{i}.0" for i in range(8))),
])
def test_other_kinds_are_shown_read_only(at, text):
    value = values(mf.kinds_file())[at]
    assert value.text == text and not value.editable and value.why == R.WHY_KIND


def test_references_to_other_files():
    common = values(mf.kinds_file())["_Common"]
    assert (common.kind, common.text, common.why) == ("UserData", "→ " + mf.PARAM, R.WHY_REF)
    assert common.note == "GameDesign/Test/common.user"


def test_values_outside_the_first_root_are_read_only():
    doc = open_doc(mf.two_roots())
    assert [(v.at, v.editable) for v in doc.values()] == [
        ("_Scale", True), ("_Count", True), ("#1._Scale", False), ("#1._Count", False)]
    assert doc.get("#1._Count").text == "5" and doc.get("#1._Count").why == R.WHY_OUTSIDE
    with pytest.raises(EditError, match="루트 밖"):
        doc.write({"#1._Count": "6"})
    assert doc.get("#2._Count") is None and doc.get("#0._Count") is None


def test_the_fields_rows_are_found_by_can_be_changed(engine):
    """#9: a key is a value like any other (§3.2.1 lists no key as read-only): an id, and a value column
    that names the rows only because the table has no id-like field (NpcPartnerStatusParam._MaxHealth),
    even in a table of one row."""
    rows = values(mf.item_data(3))
    assert rows["_Values[_ItemId=100]._ItemId"].editable and rows["_Values[_ItemId=100]._ItemId"].why == ""
    assert rows["_Values[_ItemId=100]._Index"].editable and rows["_Values[_ItemId=100]._SortId"].editable
    reward = values(mf.reward_data(4))
    assert reward["_Values[_dataId=7000,_rewardType=1]._dataId"].editable
    assert reward["_Values[_dataId=7000,_rewardType=1]._partsIndex"].editable
    at = "_DataList[_MaxHealth=1000]._MaxHealth"
    for table in ([(1000, 50), (1500, 55), (2000, 60)], [(1000, 50)]):
        data = mf.status_data(table)
        assert values(data)[at].editable
        result = engines.apply(engine, data, ITEMS, [Change(at, "S32", "1000", "3000")])
        assert not result.conflicts and [c.at for c in result.done] == [at]
        back = engine.open(result.data, ITEMS)  # its row has another name now
        assert back.get(at) is None and back.get("_DataList[_MaxHealth=3000]._MaxHealth").text == "3000"
        assert back.get("_DataList[_MaxHealth=3000]._Defence").text == "50"
    rule = Change("_DataList[*]._MaxHealth", "S32", "1500", "1")
    result = engines.apply(engine, mf.status_data([(1000, 50), (1500, 55)]), ITEMS, [rule])
    assert [c.count for c in result.kept] == [1] and not result.conflicts
    result = engines.apply(engine, mf.item_data(3), ITEMS, [Change("_Values[_ItemId=101]._ItemId", "S32", "101", "7")])
    assert not result.conflicts and engine.open(result.data, ITEMS).get("_Values[_ItemId=7]._SellPrice").text == "60"


def test_a_written_value_is_read_back_at_its_place():
    doc = open_doc(mf.status_data([(1000, 50), (1500, 55)]))
    at = "_DataList[_MaxHealth=1000]._MaxHealth"
    where = doc.place(at)
    back = open_doc(doc.write({at: "3000"}))
    assert back.get(at) is None and back.at_place(where).text == "3000"
    assert back.at_place(doc.place("_DataList[_MaxHealth=1500]._Defence")).text == "55"
    assert doc.place("_Nope") is None and doc.place("_DataList[*]._Defence") is None
    assert back.at_place((999, 0)) is None and back.at_place(where + (0,)) is None and back.at_place(()) is None
    kinds = open_doc(mf.kinds_file())
    for name in ("_Param._Count", "_Vec3", "_Pos~1", "_Common", "_Bool"):
        assert kinds.at_place(kinds.place(name)).text == kinds.get(name).text
    areas = open_doc(mf.area_table())
    for name in ('_Areas[_Name="north"]._Ids[1]', '_Areas[_Name="south"]._Max'):
        assert areas.at_place(areas.place(name)).text == areas.get(name).text


def test_a_locked_file_is_read_only():
    doc = open_doc(mf.item_data(2))
    doc.lock("고칠 수 없는 파일")
    assert all(not v.editable and v.why == "고칠 수 없는 파일" for v in doc.values())
    assert doc.get("_Values[_ItemId=100]._SellPrice").why == "고칠 수 없는 파일"
    with pytest.raises(EditError, match="고칠 수 없는 파일"):
        doc.write({"_Values[_ItemId=100]._SellPrice": "1"})


# ---- addresses ----------------------------------------------------------------------------------------
def test_choose_key_order():
    def rows(*dicts):
        return [dict(d) for d in dicts]

    unique_id = rows({"_Index": "0", "_Price": "5", "_ItemId": "1"}, {"_Index": "1", "_Price": "6", "_ItemId": "2"})
    assert R.choose_key(unique_id) == ("_ItemId",)  # id-like before _Index and before other fields
    pair = rows({"_dataId": "7", "_partsIndex": "0", "_Index": "0"}, {"_dataId": "7", "_partsIndex": "1", "_Index": "1"},
                {"_dataId": "8", "_partsIndex": "0", "_Index": "2"})
    assert R.choose_key(pair) == ("_dataId", "_partsIndex")  # two id-like fields before _Index alone
    index = rows({"_Index": "0", "_Name": '"a"', "_Kind": "1"}, {"_Index": "1", "_Name": '"b"', "_Kind": "1"})
    assert R.choose_key(index) == ("_Index",)
    other = rows({"_Name": '"a"', "_Kind": "1"}, {"_Name": '"b"', "_Kind": "1"})
    assert R.choose_key(other) == ("_Name",)
    assert R.choose_key(rows({"_Kind": "1"}, {"_Kind": "1"})) == ()  # by position
    assert R.choose_key([None, {"_Id": "1"}, None, {"_Id": "2"}]) == ("_Id",)  # null rows are found by position
    assert R.choose_key([None, None]) == () and R.choose_key([]) == ()
    assert R.choose_key(rows({"_Id": "1", "_Extra": "x"}, {"_Id": "2"})) == ("_Id",)  # fields every row has
    values_and_names = rows({"_Price": "5", "_Name": '"a"'}, {"_Price": "6", "_Name": '"b"'})
    assert R.choose_key(values_and_names) == ("_Price",)
    assert R.choose_key(values_and_names, fixed={"_Name"}) == ("_Name",)  # a name before a value


def test_rows_without_a_key_are_found_by_position():
    same = [mf.item_row(0), mf.item_row(0)]
    rows = values(mf.item_data(rows=same))
    assert "_Values[0]._SellPrice" in rows and "_Values[1]._SellPrice" in rows
    assert rows["_Values[1]._ItemId"].editable  # no key field
    doc = open_doc(mf.item_data(rows=same))
    out = doc.write({"_Values[1]._SellPrice": "77"})
    assert [v.text for v in open_doc(out).values() if v.label == "_SellPrice"] == ["30", "77"]


def test_composite_keys_and_wrapper_values_as_keys():
    doc = open_doc(mf.reward_data(4))
    at = "_Values[_dataId=7001,_rewardType=2]._probabilityStory"
    out = doc.write({at: "60"})
    assert open_doc(out).get(at).text == "60"
    assert open_doc(out).get("_Values[_dataId=7001,_rewardType=1]._probabilityStory").text == "25"
    # any fields that pick one row find it, in any order
    assert doc.get("_Values[_rewardType=2,_dataId=7001]._probabilityStory").text == "25"
    assert doc.get("_Values[_partsIndex=1,_dataId=7001]._probabilityStory") is not None


def test_nth_field_of_a_name():
    doc = open_doc(mf.kinds_file())
    out = doc.write({"_Pos": "1.5", "_Pos~1": "2.5"})
    back = open_doc(out)
    assert (back.get("_Pos").text, back.get("_Pos~1").text) == ("1.5", "2.5")
    assert back.get("_Pos~2") is None


def test_string_keys_are_quoted_and_escaped():
    names = ['say "hi"', "a\\b]c,d=e", "plain"]
    data = mf.area_table(areas=[mf.area(n, i, i + 1) for i, n in enumerate(names)])
    doc = open_doc(data)
    ats = [v.at for v in doc.values() if v.label == "_Min"]
    assert ats == ['_Areas[_Name="say \\"hi\\""]._Min', '_Areas[_Name="a\\\\b]c,d=e"]._Min',
                   '_Areas[_Name="plain"]._Min']
    out = doc.write({ats[1]: "9"})
    assert open_doc(out).get(ats[1]).text == "9.0" and open_doc(out).get(ats[0]).text == "0.0"
    assert R.parse_address(ats[1]) == [("_Areas", 0, ("k", (("_Name", '"a\\\\b]c,d=e"'),))), ("_Min", 0, None)]


def test_guid_and_bool_keys():
    rows = [mf.item_row(i, _ItemId=1, _SortId=1, _Type=1, _Index=0, _Eatable=i == 1, _RawName=fx.guid(i))
            for i in range(2)]  # nothing id-like tells them apart: the GUID does
    doc = open_doc(mf.item_data(rows=rows))
    ats = [v.at for v in doc.values() if v.label == "_SellPrice"]
    assert ats == ["_Values[_RawName=5eed0000-0000-0000-0000-000000000000]._SellPrice",
                   "_Values[_RawName=5eed0000-0000-0000-0000-000000000001]._SellPrice"]
    assert doc.get("_Values[_RawName=5EED0000-0000-0000-0000-000000000001]._SellPrice").text == "60"
    assert doc.get("_Values[_Eatable=true]._SellPrice").text == "60"


def test_resolving_none_one_or_several():
    doc = open_doc(mf.item_data(3))
    assert doc.get("_Values[_ItemId=999]._SellPrice") is None  # no such row
    assert doc.get("_Values[_ItemId=102]._SellPrice").text == "90"
    assert doc.get("_Values[_Type=1]._SellPrice") is None  # three rows
    assert doc.get("_Values[_ItemId=101]") is None  # a row, not a value
    assert doc.get("_Values") is None and doc.get("_Nope") is None and doc.get("_Values[9]._SellPrice") is None
    assert doc.get("_Values[_ItemId=101]._GetRank") is None and doc.get("_Values[_ItemId=101]._GetRank[5]") is None
    assert doc.get("_Values[_ItemId=101]._GetRank[_X=1]") is None
    with pytest.raises(EditError, match="찾지 못했습니다"):
        doc.write({"_Values[_Type=1]._SellPrice": "1"})


def rekeyed() -> bytes:
    """ItemData after an update that made ``_ItemId`` repeat: its rows are named by ``_SortId`` now."""
    return mf.item_data(rows=[mf.item_row(i) for i in range(3)] + [mf.item_row(3, _ItemId=102)])


def test_a_value_comes_back_at_the_address_the_file_names_it_by():
    """#8: an address spelled with another key (an older one, or the position) still finds its row, and
    get/expand say what values() calls it."""
    doc = open_doc(rekeyed())
    listed = {v.at for v in doc.values()}
    v = doc.get("_Values[_ItemId=101]._SellPrice")
    assert (v.at, v.group, v.label, v.rule_at, v.text) == (
        "_Values[_SortId=1]._SellPrice", "_Values[_SortId=1]", "_SellPrice", "_Values[*]._SellPrice", "60")
    assert v.at in listed
    assert doc.get("_Values[1]._SellPrice").at == "_Values[_SortId=1]._SellPrice"  # by position
    assert doc.get("_Values[_Index=1,_ItemId=101]._SellPrice").at == "_Values[_SortId=1]._SellPrice"
    assert doc.get("_Values[_ItemId=101]._GetRank[1]").at == "_Values[_SortId=1]._GetRank[1]"
    assert doc.get("_Values[_ItemId=102]._SellPrice") is None  # two rows now
    assert [(x.at, x.rule_at) for x in doc.expand("_Values[*]._SellPrice", "60")] == [
        ("_Values[_SortId=1]._SellPrice", "_Values[*]._SellPrice")]
    assert [(x.at, x.rule_at) for x in doc.expand("_Values[_ItemId=101]._GetRank[*]", "2")] == [
        ("_Values[_SortId=1]._GetRank[1]", "_Values[_SortId=1]._GetRank[*]")]
    guid = doc.get("_Values[_RawName=5EED0000-0000-0000-0000-000000000001]._BuyPrice")
    assert guid.at == "_Values[_SortId=1]._BuyPrice"
    assert open_doc(mf.two_roots()).get("#01._Count").at == "#1._Count"
    kinds = open_doc(mf.kinds_file())
    assert [kinds.get(a).at for a in ("_Pos~1", "_Param._Count", "_Common", "_S8")] == [
        "_Pos~1", "_Param._Count", "_Common", "_S8"]
    areas = open_doc(mf.area_table())
    assert areas.get("_Areas[1]._Max").at == '_Areas[_Name="south"]._Max'
    assert areas.get('_Areas[_Name="north"]._Ids[0]').at == '_Areas[_Name="north"]._Ids[0]'
    for value in doc.values():  # every listed address is its own spelling
        assert doc.get(value.at) == value


@pytest.mark.parametrize("at", ["", "a..b", ".a", "a.", "a[", "a[]", "a[x]", "a~0", "a~", "a~x", "a]b", "a[1]b",
                                '_Values[_ItemId="x]', "_Values[_ItemId=1.5]", "_Values[=1]", "#x._A"])
def test_malformed_addresses(at):
    assert R.parse_address(at) is None or at.startswith("#")
    assert open_doc(mf.item_data(2)).get(at) is None


def test_rule_of():
    assert R.rule_of("_Values[_ItemId=101]._SellPrice") == "_Values[*]._SellPrice"
    assert R.rule_of("_Values[_ItemId=101]._GetRank[1]") == "_Values[_ItemId=101]._GetRank[*]"
    assert R.rule_of('_Areas[_Name="a]b"]._Min') == "_Areas[*]._Min"
    assert R.rule_of("_Param._Scale") == "" and R.rule_of("_Scale") == ""
    assert R.rule_of("_Values[*]._SellPrice") == "_Values[*]._SellPrice"


def test_expand_a_rule():
    data = mf.item_data(3)
    doc = open_doc(data)
    rows = doc.expand("_Values[*]._MaxCount", "10")
    assert [v.at for v in rows] == [f"_Values[_ItemId={i}]._MaxCount" for i in (100, 101, 102)]
    assert doc.expand("_Values[*]._MaxCount", "11") == []
    assert [v.at for v in doc.expand("_Values[*]._SellPrice", "60")] == ["_Values[_ItemId=101]._SellPrice"]
    assert [v.at for v in doc.expand("_Values[*]._Weight", "0.10000000149")] == ["_Values[_ItemId=100]._Weight"]
    assert [v.at for v in doc.expand("_Values[_ItemId=101]._GetRank[*]", "2")] == ["_Values[_ItemId=101]._GetRank[1]"]
    assert [v.editable for v in doc.expand("_Values[*]._ItemId", "101")] == [True]  # the key, too (#9)
    assert {v.rule_at for v in doc.expand("_Values[*]._MaxCount", "10")} == {"_Values[*]._MaxCount"}
    assert doc.expand("_Values[_ItemId=101]._SellPrice", "60") == [] and doc.expand("_Nope[*]._X", "1") == []
    areas = open_doc(mf.area_table())
    assert [v.at for v in areas.expand("_Areas[*]._Max", "4")] == ['_Areas[_Name="south"]._Max']


def test_write_several_values_and_refusals():
    data = mf.item_data(3)
    doc = open_doc(data)
    out = doc.write({"_Values[_ItemId=101]._SellPrice": "9999", "_Values[_ItemId=100]._Weight": "0.75",
                     "_Values[_ItemId=101]._GetRank[1]": "7", "_Values[_ItemId=102]._Eatable": "false",
                     "_Values[1]._BuyPrice": "5"})
    back = open_doc(out)
    assert [back.get(a).text for a in ("_Values[_ItemId=101]._SellPrice", "_Values[_ItemId=100]._Weight",
                                       "_Values[_ItemId=101]._GetRank[1]", "_Values[_ItemId=102]._Eatable",
                                       "_Values[_ItemId=101]._BuyPrice")] == ["9999", "0.75", "7", "false", "5"]
    assert doc.write({}) == data
    with pytest.raises(EditError, match="서로 다르게"):
        doc.write({"_Values[_ItemId=101]._SellPrice": "1", "_Values[1]._SellPrice": "2"})
    assert doc.write({"_Values[_ItemId=101]._SellPrice": "1", "_Values[1]._SellPrice": "1"}) != data


def test_an_edit_keeps_what_matfix_judges():
    """I5: the instances and their CRCs, the strings and resource paths stay; matfix reads the output as
    it reads the input."""
    data = mf.kinds_file()
    out = open_doc(data).write({"_S32": "1", "_Vec3": "1, 1, 1", "_Param._Scale": "9"})
    a, b = rsz.parse(data), rsz.parse(out)
    assert a.insts == b.insts and a.objects == b.objects and a.data[:a.data_start] == b.data[:b.data_start]
    assert a.resources() == b.resources()
    assert rsz.plan("x.user.3", out, data, REG).state == rsz.plan("x.user.3", data, data, REG).state == "current"


# ---- checks on every write ----------------------------------------------------------------------------
def _patched(monkeypatch, change):
    real = R._patch

    def patch(data, writes):
        return change(bytearray(real(data, writes)), writes)

    monkeypatch.setattr(R, "_patch", patch)


def _span(doc: R.DataFile, at: str) -> tuple[int, int]:
    leaf = doc._find(at)[0][0]
    inst = doc.insts[leaf.path[0]]
    spans = {}
    R.locate(doc.data, inst.start, len(doc.data), inst.fields, doc.schema, REG, (leaf.path[0],), spans)
    return spans[leaf.path]


def test_v1_length(monkeypatch):
    _patched(monkeypatch, lambda out, writes: bytes(out) + b"\0")
    with pytest.raises(EditError, match=r"\(V1\)"):
        open_doc(mf.item_data(3)).write({"_Values[_ItemId=101]._SellPrice": "1"})


def test_v3_reads_back(monkeypatch):
    doc = open_doc(mf.item_data(3))
    rsz_off = doc.info.rsz_off

    def corrupt(out, writes):
        out[rsz_off] ^= 0xFF  # the RSZ block's magic
        return bytes(out)

    _patched(monkeypatch, corrupt)
    with pytest.raises(EditError, match=r"\(V3\)"):
        doc.write({"_Values[_ItemId=101]._SellPrice": "1"})


def test_v3_same_instances(monkeypatch):
    doc = open_doc(mf.item_data(3))
    table = doc.info.inst_table

    def crc(out, writes):
        out[table + 8 + 4] ^= 0x01  # a CRC in the instance table: still reads
        return bytes(out)

    _patched(monkeypatch, crc)
    with pytest.raises(EditError, match=r"\(V3\)"):
        doc.write({"_Values[_ItemId=101]._SellPrice": "1"})


def test_v5_references(monkeypatch):
    doc = open_doc(mf.item_data(3))
    root = doc.info.objects[0]
    spans = {}
    inst = doc.insts[root]
    R.locate(doc.data, inst.start, len(doc.data), inst.fields, doc.schema, REG, (root,), spans)
    start, end = spans[(root, 0, 0)]  # the first row of _Values

    def forward(out, writes):
        out[start:end] = struct.pack("<i", root)  # points at the root itself: still reads, _verify refuses
        return bytes(out)

    _patched(monkeypatch, forward)
    with pytest.raises(EditError, match=r"\(V5\)"):
        doc.write({"_Values[_ItemId=101]._SellPrice": "1"})


def test_v2_bytes_outside_the_values(monkeypatch):
    doc = open_doc(mf.item_data(3))
    other = _span(doc, "_Values[_ItemId=100]._BuyPrice")

    def flip(out, writes):
        out[other[0]] ^= 0x01  # another value: everything still reads
        return bytes(out)

    _patched(monkeypatch, flip)
    with pytest.raises(EditError, match=r"\(V2\)"):
        doc.write({"_Values[_ItemId=101]._SellPrice": "1"})


def test_v4_a_value_not_written(monkeypatch):
    monkeypatch.setattr(R, "_patch", lambda data, writes: bytes(data))  # skips the span
    with pytest.raises(EditError, match=r"\(V4\)"):
        open_doc(mf.item_data(3)).write({"_Values[_ItemId=101]._SellPrice": "1"})


def test_v4_a_value_of_the_wrong_kind(monkeypatch):
    real = R._patch
    monkeypatch.setattr(R, "_patch", lambda data, writes: real(data, [(s, e, struct.pack("<f", 1.0))
                                                                       for s, e, _n in writes]))
    with pytest.raises(EditError, match=r"\(V4\)"):
        open_doc(mf.item_data(3)).write({"_Values[_ItemId=101]._SellPrice": "1"})


def test_every_failure_says_it_was_not_saved(monkeypatch):
    _patched(monkeypatch, lambda out, writes: bytes(out) + b"\0")
    with pytest.raises(EditError, match="바꾼 파일을 확인하지 못해 저장하지 않았습니다"):
        open_doc(mf.item_data(3)).write({"_Values[_ItemId=101]._SellPrice": "1"})


# ---- selfcheck ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("make", [mf.item_data, mf.reward_data, mf.area_table, mf.kinds_file, mf.shared_file,
                                  mf.two_roots, mf.nan_file])
def test_selfcheck_passes_on_every_sample(make):
    open_doc(make()).selfcheck()


def test_selfcheck_fails_when_saving_changes_the_file(monkeypatch, caplog):
    _patched(monkeypatch, lambda out, writes: bytes(out[:-1]) + bytes([out[-1] ^ 1]))
    doc = open_doc(mf.item_data(3))
    with caplog.at_level(logging.WARNING, logger="mhwmm.modedit.rszdata"):
        with pytest.raises(EditError) as err:
            doc.selfcheck()
    assert str(err.value) == R.SELFCHECK_FAILED
    assert "자체 점검" in caplog.text


def test_selfcheck_fails_when_a_value_does_not_read_back_as_itself(monkeypatch):
    real = R.encode

    def off_by_one(kind, size, value, old=None):
        return real(kind, size, value + 1 if kind == "S32" else value, old)

    monkeypatch.setattr(R, "encode", off_by_one)
    with pytest.raises(EditError, match="로그 참고"):
        open_doc(mf.item_data(3)).selfcheck()


def test_a_file_failing_its_selfcheck_opens_read_only(engine, monkeypatch):
    _patched(monkeypatch, lambda out, writes: bytes(out[:-1]) + bytes([out[-1] ^ 1]))
    doc = engine.open(mf.item_data(3), ITEMS)
    assert doc.readonly == R.SELFCHECK_FAILED
    assert all(not v.editable and v.why == R.SELFCHECK_FAILED for v in doc.values())
    with pytest.raises(EditError, match="로그 참고"):  # the member waits (or the save stops)
        engines.apply(engine, mf.item_data(3), ITEMS, [Change("_Values[_ItemId=101]._SellPrice", "S32", "60", "1")])


# ---- the engine ----------------------------------------------------------------------------------------
def test_engine_is_registered(engine):
    assert isinstance(engine, R.RszEngine) and (engine.name, engine.tool) == ("rsz", "data")
    assert engines.engine_for(ITEMS) is engine and engines.engine_for("x.pak", b"USR\0") is engine


def test_engine_open_holds_the_registry_only_while_reading(engine):
    doc = engine.open(mf.item_data(2), ITEMS)
    assert doc.registry is REG and rsz.TypeRegistry._holds == 0 and not doc.readonly
    held = R.registry()
    assert held is REG and rsz.TypeRegistry._holds == 1
    engine.open(mf.item_data(2), ITEMS)
    assert rsz.TypeRegistry._holds == 1
    R.release()
    assert rsz.TypeRegistry._holds == 0 and rsz.TypeRegistry._loaded is REG  # not loaded from file: kept


def test_engine_checks_each_content_once(engine, monkeypatch):
    calls = []
    real = R.DataFile.selfcheck
    monkeypatch.setattr(R.DataFile, "selfcheck", lambda self: calls.append(len(self.data)) or real(self))
    data = mf.item_data(3)
    engine.open(data, ITEMS)
    engine.open(data, ITEMS)
    assert len(calls) == 1
    out = engine.open(data, ITEMS).write({"_Values[_ItemId=101]._SellPrice": "1"})
    engine.open(out, ITEMS)  # the output of a checked write: not checked again
    assert len(calls) == 1
    engine.open(mf.item_data(4), ITEMS)
    assert len(calls) == 2
    monkeypatch.setattr(R, "CHECKED_KEEP", 1)
    engine.open(mf.item_data(5), ITEMS)
    engine.open(data, ITEMS)  # forgotten: only the last one is kept
    assert len(calls) == 4


def test_root_type():
    assert R.root_type(mf.item_data(2), REG) == mf.ITEM
    assert R.root_type(mf.stale_item_data(), REG) == mf.ITEM  # from the tables only
    assert R.root_type(b"junk", REG) == "" and R.root_type(mf.item_data(2), rsz.TypeRegistry({"types": {}})) == ""


def test_engine_parse_and_same(engine):
    doc = engine.open(mf.kinds_file(), ITEMS)
    f32, s32, vec3, flag = doc.get("_F32"), doc.get("_S32"), doc.get("_Vec3"), doc.get("_Bool")
    assert engine.parse(f32, "0.10000000149") == "0.1" and engine.parse(s32, "0x10") == "16"
    assert engine.parse(vec3, "1,2,3") == "1.0, 2.0, 3.0" and engine.parse(flag, "끔") == "false"
    with pytest.raises(EditError):
        engine.parse(s32, "x")
    assert engine.same(f32, "0.1", "0.10000000149") and not engine.same(f32, "0.1", "0.1000001")
    assert engine.same(s32, "16", "0x10") and engine.same(vec3, "1,2,3", "1.0, 2.0, 3.0")
    assert engine.same(flag, "true", "켬") and not engine.same(flag, "true", "false")
    assert not engine.same(s32, "1", "x")
    text = doc.get("_Text")
    assert engine.same(text, "hello", "hello") and not engine.same(text, "hello", "Hello")


def test_engine_apply_single_values_and_rules(engine):
    data = mf.item_data(3)
    changes = [Change("_Values[*]._MaxCount", "S16", "10", "99"),
               Change("_Values[_ItemId=101]._MaxCount", "S16", "10", "5"),  # over the rule's row
               Change("_Values[_ItemId=102]._SellPrice", "S32", "90", "1"),
               Change("_Values[_ItemId=100]._SellPrice", "S32", "30", "30")]  # already
    result = engines.apply(engine, data, ITEMS, changes)
    back = engine.open(result.data, ITEMS)
    assert [back.get(f"_Values[_ItemId={i}]._MaxCount").text for i in (100, 101, 102)] == ["99", "5", "99"]
    assert back.get("_Values[_ItemId=102]._SellPrice").text == "1" and not result.conflicts
    assert result.counts == {"_Values[*]._MaxCount": 2}
    assert [c.at for c in result.already] == ["_Values[_ItemId=100]._SellPrice"]
    assert len(result.data) == len(data)


def test_engine_apply_conflicts(engine):
    updated = mf.item_data(rows=[mf.item_row(0, _SellPrice=31), mf.item_row(1)])  # the author changed one
    result = engines.apply(engine, updated, ITEMS, [
        Change("_Values[_ItemId=100]._SellPrice", "S32", "30", "1"),
        Change("_Values[_ItemId=102]._SellPrice", "S32", "90", "1"),  # the row is gone
        Change("_Values[_ItemId=101]._RawName", "Guid", "x", "y"),  # read-only
        Change("_Values[_ItemId=101]._SellPrice", "F32", "60", "1"),  # another kind
        Change("_Values[*]._MaxCount", "S16", "11", "1")])  # no such rows
    assert [(c.why, c.now) for c in result.conflicts] == [
        ("norow", None), ("changed", "31"), ("gone", None), ("readonly", "5eed0000-0000-0000-0000-000000000001"), ("kind", "60")]
    assert result.data == updated


def test_engine_apply_spells_changes_the_way_the_file_names_them(engine):
    """#8: changes recorded when the rows were named by _ItemId, applied on a file that names them by
    _SortId: a rule with a single value over one of its rows is not refused, and every change comes
    back spelled the new way (so the page and the next edit find it)."""
    old_price, old_count = "_Values[_ItemId=101]._SellPrice", "_Values[_ItemId=101]._MaxCount"
    rule = Change("_Values[*]._MaxCount", "S16", "10", "99")
    changes = [rule, Change(old_price, "S32", "60", "500"), Change(old_count, "S16", "10", "50")]
    result = engines.apply(engine, rekeyed(), ITEMS, changes)
    assert not result.conflicts and not result.still
    assert [c.at for c in result.kept] == ["_Values[*]._MaxCount", "_Values[_SortId=1]._SellPrice",
                                          "_Values[_SortId=1]._MaxCount"]
    assert result.renamed == {old_price: "_Values[_SortId=1]._SellPrice", old_count: "_Values[_SortId=1]._MaxCount"}
    assert result.kept[0].count == 3 and result.counts == {"_Values[*]._MaxCount": 3}
    back = engine.open(result.data, ITEMS)
    assert [v.text for v in back.values() if v.label == "_MaxCount"] == ["99", "50", "99", "99"]
    assert back.get("_Values[_SortId=1]._SellPrice").text == "500"
    # the conflicts are spelled the new way too
    result = engines.apply(engine, rekeyed(), ITEMS, [Change(old_price, "S32", "61", "500")])
    assert [(x.change.at, x.why, x.now) for x in result.conflicts] == [("_Values[_SortId=1]._SellPrice", "changed", "60")]


def test_engine_apply_one_change_per_value(engine):
    """#8: two spellings of one value are one value: the later change wins, an earlier conflict of it
    goes (as Pending.set_value drops the conflict of a value set again)."""
    older = Change("_Values[_ItemId=101]._SellPrice", "S32", "60", "500")
    newer = Change("_Values[_SortId=1]._SellPrice", "S32", "60", "700")
    result = engines.apply(engine, rekeyed(), ITEMS, [older, newer])
    assert [(c.at, c.after) for c in result.kept] == [("_Values[_SortId=1]._SellPrice", "700")]
    assert engine.open(result.data, ITEMS).get(newer.at).text == "700"
    result = engines.apply(engine, rekeyed(), ITEMS, [newer, older])
    assert [(c.at, c.after) for c in result.kept] == [("_Values[_SortId=1]._SellPrice", "500")]
    stale = Conflict(Change(older.at, "S32", "60", "5000"), "50", "changed")
    result = engines.apply(engine, rekeyed(), ITEMS, [newer], [stale])
    assert [c.after for c in result.kept] == ["700"] and not result.still and not result.conflicts


def test_engine_apply_judges_earlier_conflicts_again(engine):
    """#1: a conflict of an earlier update is judged on the file as it is now: still the author's
    (with the value now), back to the value it was made on (applied), already mine (kept), a gone row
    or a rule's rows back."""
    def items(price=None, gone=False):
        rows = [mf.item_row(0), *([] if gone else [mf.item_row(1, **({} if price is None else {"_SellPrice": price}))]),
                mf.item_row(2)]
        return mf.item_data(rows=rows)

    mine = Change("_Values[_ItemId=101]._SellPrice", "S32", "60", "5000")
    stale = Conflict(mine, "50", "changed")
    got = engines.apply(engine, items(70), ITEMS, [], [stale])
    assert [(x.why, x.now) for x in got.still] == [("changed", "70")]
    assert got.still[0].text() == "작성자가 바꾼 값: 60 → 70 (내 값 5000)"
    assert not got.conflicts and not got.kept and got.data == items(70)
    got = engines.apply(engine, items(), ITEMS, [], [stale])
    assert not got.still and got.kept == [mine] and got.done == [mine]
    assert engine.open(got.data, ITEMS).get(mine.at).text == "5000"
    got = engines.apply(engine, items(5000), ITEMS, [], [stale])
    assert not got.still and got.already == [mine] and got.kept == [mine] and got.data == items(5000)
    gone = Conflict(mine, None, "gone")
    assert engines.apply(engine, items(), ITEMS, [], [gone]).done == [mine]
    assert [(x.why, x.now) for x in engines.apply(engine, items(gone=True), ITEMS, [], [gone]).still] == [("gone", None)]
    rule = Change("_Values[*]._MaxCount", "S16", "11", "1")
    got = engines.apply(engine, mf.item_data(rows=[mf.item_row(0, _MaxCount=11), mf.item_row(1)]), ITEMS, [],
                        [Conflict(rule, None, "norow")])
    assert [c.count for c in got.kept] == [1] and not got.still
    # the changes come first, then the conflicts that are changes again
    other = Change("_Values[_ItemId=100]._SellPrice", "S32", "30", "1")
    got = engines.apply(engine, items(), ITEMS, [other], [stale])
    assert got.kept == [other, mine]


def test_engine_open_refuses_a_stale_file(engine):
    with pytest.raises(StaleError):
        engine.open(mf.stale_item_data(), ITEMS)


# ---- comparing with the game -------------------------------------------------------------------------
def test_compare_pairs_rows_by_key():
    game = mf.item_data(5)
    rows = [mf.item_row(9)] + [mf.item_row(i) for i in (0, 1, 3)]  # adds 109 first, lacks 102 and 104
    rows[2]["_SellPrice"] = 5000
    mod = mf.item_data(rows=rows)
    gv = R.compare(mod, game, REG)
    assert gv.compared and gv.note == ""
    doc = open_doc(mod)
    listed = {v.at: v for v in doc.values()}
    assert set(gv.values) <= set(listed)  # the mod file's own addresses
    changed = {at: (gv.values[at], v.text) for at, v in listed.items()
               if at in gv.values and not R.same_value(v.kind, v.text, gv.values[at])}
    assert changed == {"_Values[_ItemId=101]._SellPrice": ("60", "5000")}
    assert gv.added == frozenset({"_Values[_ItemId=109]"})
    assert not any(at.startswith("_Values[_ItemId=109]") for at in gv.values)
    assert (gv.removed_rows, gv.game_only_rows) == (1, 1)  # 102 left out; 104 at the end: new in the game?


def test_compare_counts_every_missing_row_as_new_for_another_game_version():
    game = bytearray(mf.item_data(5))
    info = rsz.parse(bytes(game))
    for i, (t, c) in enumerate(info.insts):
        if t == mf.ROW_TYPE:
            struct.pack_into("<I", game, info.inst_table + 8 * i + 4, c + 1)  # the game's row type has a newer CRC
    gv = R.compare(mf.item_data(rows=[mf.item_row(i) for i in (0, 1, 3)]), bytes(game), REG)
    assert (gv.removed_rows, gv.game_only_rows) == (0, 2)


def test_compare_skips_index_when_rows_are_numbered_by_position():
    game = mf.item_data(3)
    mod = mf.item_data(rows=[mf.item_row(0), mf.item_row(9, _Index=1), mf.item_row(1, _Index=2), mf.item_row(2, _Index=3)])
    gv = R.compare(mod, game, REG)
    assert "_Values[_ItemId=101]._Index" not in gv.values and "_Values[_ItemId=101]._SellPrice" in gv.values
    assert gv.added == frozenset({"_Values[_ItemId=109]"})


def test_compare_by_position_only_when_as_long():
    same = [mf.item_row(0), mf.item_row(0)]
    longer = mf.item_data(rows=same + [mf.item_row(0)])
    gv = R.compare(mf.item_data(rows=same), longer, REG)  # no key, other length: not paired
    assert not any(at.startswith("_Values[") for at in gv.values) and not gv.added
    changed = [dict(r) for r in same]
    changed[1]["_Weight"] = 9.5
    gv = R.compare(mf.item_data(rows=changed), mf.item_data(rows=same), REG)
    assert gv.values["_Values[1]._Weight"] == "0.1" and gv.values["_Values[0]._Weight"] == "0.1"


def test_compare_plain_arrays_by_position_when_as_long():
    game = mf.item_data(2)
    mod = mf.item_data(rows=[mf.item_row(0, _GetRank=[4]), mf.item_row(1, _GetRank=[1, 2, 3])])
    gv = R.compare(mod, game, REG)
    assert gv.values["_Values[_ItemId=100]._GetRank[0]"] == "3"
    assert not any(at.startswith("_Values[_ItemId=101]._GetRank") for at in gv.values)


def test_compare_structs_wrappers_and_nested_instances():
    game = mf.area_table()
    mod = mf.area_table(areas=[mf.area("north", 1.0, 2.5, (1, 2)), mf.area("south", 3.0, 4.0), mf.area("east", 5, 6)])
    gv = R.compare(mod, game, REG)
    assert gv.values['_Areas[_Name="north"]._Max'] == "2.0" and gv.added == frozenset({'_Areas[_Name="east"]'})
    reward = [mf.reward_row(i) for i in range(4)]
    reward[3]["_IdStory"] = mf.wrapper(999)
    gv = R.compare(mf.reward_data(rows=reward), mf.reward_data(4), REG)
    assert gv.values["_Values[_dataId=7001,_rewardType=2]._IdStory"] == "203"
    assert gv.values["_Values[_dataId=7001,_rewardType=2]._IdEx[1]"] == "403"
    kinds = R.compare(mf.kinds_file(_S32=5), mf.kinds_file(), REG)
    assert kinds.values["_S32"] == "-70000" and kinds.values["_Param._Count"] == "3"
    assert kinds.values["_Common"] == "→ " + mf.PARAM and kinds.values["_Text"] == "hello"


def test_compare_does_not_pair_rows_by_a_value_column_the_mod_changed():
    """#10: with no id-like field the rows are named by _MaxHealth; a mod that changed every _MaxHealth
    must not have its rows paired with game rows that happen to share a value (or none)."""
    game = mf.status_data([(1000, 50), (1500, 55), (2000, 60)])
    gv = R.compare(mf.status_data([(2000, 50), (3000, 55), (4000, 60)]), game, REG)
    assert gv.notes() == [] and gv.added == frozenset() and (gv.removed_rows, gv.game_only_rows) == (0, 0)
    assert gv.values["_DataList[_MaxHealth=2000]._Defence"] == "50"  # its own row (by position), not the game's 2000
    assert gv.values["_DataList[_MaxHealth=2000]._MaxHealth"] == "1000"
    assert gv.values["_DataList[_MaxHealth=4000]._Defence"] == "60"
    gv = R.compare(mf.status_data([(1500, 50), (2000, 55), (2500, 60)]), game, REG)  # health +500
    assert [gv.values[f"_DataList[_MaxHealth={h}]._Defence"] for h in (1500, 2000, 2500)] == ["50", "55", "60"]
    gv = R.compare(mf.status_data([(2000, 50), (3000, 55), (4000, 60), (5000, 65)]), game, REG)  # another length
    assert not any(at.startswith("_DataList[") for at in gv.values) and gv.notes() == [] and not gv.added
    # rows named by an id or a name are still paired by it, in any order
    gv = R.compare(mf.item_data(rows=[mf.item_row(2), mf.item_row(0, _SellPrice=1)]), mf.item_data(3), REG)
    assert gv.values["_Values[_ItemId=100]._SellPrice"] == "30" and gv.values["_Values[_ItemId=102]._SellPrice"] == "90"
    gv = R.compare(mf.area_table([mf.area("south", 3.0, 9.0), mf.area("north", 1.0, 2.0)]), mf.area_table(), REG)
    assert gv.values['_Areas[_Name="south"]._Max'] == "4.0"


def test_compare_says_which_values_were_paired_by_a_key():
    """#11: 게임 값의 몇 배 may use only values whose rows were paired by a key unique in both files."""
    gv = R.compare(mf.item_data(rows=[mf.item_row(2), mf.item_row(0), mf.item_row(1)]), mf.item_data(3), REG)
    assert {"_Values[_ItemId=101]._SellPrice", "_Values[_ItemId=101]._GetRank[1]"} <= gv.keyed <= set(gv.values)
    game = mf.area_table([mf.area("zone", 1.0, 2.0), mf.area("zone", 10.0, 20.0)])
    mod = mf.area_table([mf.area("zone", 12.0, 20.0), mf.area("zone", 1.0, 2.0)])  # moved, no key
    gv = R.compare(mod, game, REG)
    assert gv.values["_Areas[0]._Min"] == "1.0" and "_Areas[0]._Min" not in gv.keyed  # shown, by position
    assert not any(at.startswith("_Areas[") for at in gv.keyed)
    assert {"_Scale", "_Center", "_Tint"} <= gv.keyed  # in no table
    gv = R.compare(mf.status_data([(2000, 50), (3000, 55)]), mf.status_data([(1000, 50), (1500, 55)]), REG)
    assert "_DataList[_MaxHealth=2000]._Defence" in gv.values and not gv.keyed
    reward = R.compare(mf.reward_data(4), mf.reward_data(4), REG)
    assert "_Values[_dataId=7001,_rewardType=2]._probabilityStory" in reward.keyed
    assert R.compare(mf.item_data(2), None, REG).keyed == frozenset()


def test_compare_hints():
    rows = [mf.item_row(i, _Rare=[1, 1, 1, 2, 2, 5][i % 6], _MaxCount=i) for i in range(14)]
    gv = R.compare(mf.item_data(4), mf.item_data(rows=rows), REG)
    assert gv.hints["_Values[*]._Rare"] == ["1", "2", "5"]  # most frequent first
    assert "_Values[*]._MaxCount" not in gv.hints  # 14 different values: no hint
    assert gv.hints["_Values[*]._Eatable"] == ["false", "true"]  # 7 each: by value
    assert "_Values[*]._Weight" not in gv.hints  # ints and bools only
    assert gv.hints["_Values[_ItemId=101]._GetRank[*]"] == ["1", "2"]
    reward = R.compare(mf.reward_data(4), mf.reward_data(4), REG)
    assert reward.hints["_Values[*]._probabilityStory"] == ["25"]
    assert "_Values[*]._IdStory" not in reward.hints  # enum wrappers are read-only


def test_compare_hint_limit():
    rows = [mf.item_row(i, _Type=i % 13) for i in range(26)]
    assert "_Values[*]._Type" not in R.compare(mf.item_data(2), mf.item_data(rows=rows), REG).hints
    rows = [mf.item_row(i, _Type=i % 12) for i in range(24)]
    assert len(R.compare(mf.item_data(2), mf.item_data(rows=rows), REG).hints["_Values[*]._Type"]) == 12


def test_compare_texts():
    game = mf.item_data(5)
    gv = R.compare(mf.item_data(rows=[mf.item_row(i) for i in (0, 1, 3)]), game, REG)
    assert gv.notes() == ["모드가 뺀 행 1개 (바꿀 수 없음)",
                          "게임에 새로 생긴 행 1개가 모드 파일에는 없습니다 (게임 업데이트 뒤 모드가 아직 업데이트되지 "
                          "않았을 수 있습니다)"]
    assert gv.hint("_Values[*]._Type") == "게임에 쓰인 값: 1" and gv.hint("") == "" and gv.hint("_Nope[*]") == ""
    value = open_doc(game).get("_Values[_ItemId=100]._Type")
    assert gv.outside_hint(value, "2") and not gv.outside_hint(value, "0x1")
    price = open_doc(game).get("_Values[_ItemId=100]._Eatable")
    assert not gv.outside_hint(price, "false")  # integers only
    assert R.compare(mf.item_data(2), None, REG).notes() == [R.NO_GAME]


def test_compare_what_cannot_be_compared(monkeypatch):
    mod = mf.item_data(2)
    assert R.compare(mod, None, REG).note == R.NO_GAME
    assert "구성이 달라" in R.compare(mod, mf.area_table(), REG).note
    assert "읽지 못해" in R.compare(mod, b"junk", REG).note
    assert "읽지 못해" in R.compare(mod, mf.stale_item_data(), REG).note
    monkeypatch.setattr(R._Pairing, "run", lambda self: 1 / 0)
    gv = R.compare(mod, mf.item_data(2), REG)
    assert gv.note and not gv.compared and gv.values == {}


def test_compare_reuses_an_open_file():
    doc = open_doc(mf.item_data(2))
    gv = R.compare(b"", mf.item_data(2), REG, doc=doc)
    assert gv.compared and gv.values["_Values[_ItemId=101]._SellPrice"] == "60"


# ---- size limit -------------------------------------------------------------------------------------------
def test_values_beyond_the_limit_are_cut():
    doc = R.DataFile(mf.item_data(3), REG, max_leaves=5)
    assert len(doc.values()) == 5 and doc.truncated
    assert doc.notes == ["값이 너무 많아 앞쪽 5개만 읽었습니다"]
    assert doc.get("_Values[_ItemId=102]._SellPrice").text == "90"  # still found by address
    out = doc.write({"_Values[_ItemId=102]._SellPrice": "1"})
    assert open_doc(out).get("_Values[_ItemId=102]._SellPrice").text == "1"
    assert not R.DataFile(mf.item_data(3), REG, max_leaves=37).truncated
    assert R.DataFile(mf.item_data(3), REG, max_leaves=36).truncated
    assert R.MAX_LEAVES == 300_000 and "300,000" in f"{R.MAX_LEAVES:,}"


# ---- optional: real files ----------------------------------------------------------------------------------
SAMPLES = os.environ.get("MHWMM_RSZ_SAMPLES")


def _other_text(value) -> str | None:
    """A different value of the same kind (in range), or None."""
    kind = value.kind
    if kind == "Bool":
        return "false" if value.text == "true" else "true"
    if kind in R.INTS:
        high = value.spec[2]
        n = int(value.text)
        return str(n + 1 if n < high else n - 1)
    if kind in R.FLOATS:
        x = float(value.text)
        return R.format_value(kind, x + 1.0 if abs(x) < 1e4 else x / 2)
    if kind in R.VECTORS:
        parts = [p.strip() for p in value.text.split(",")]
        first = float(parts[0]) if "." in parts[0] or "e" in parts[0] else int(parts[0])
        if kind == "Color":
            first = (int(parts[0]) + 1) % 256
        elif isinstance(first, int):
            first = first + 1 if first < 1000 else first - 1
        else:
            first = first + 1.0
        return ", ".join([str(first)] + parts[1:])
    return None


@pytest.mark.skipif(not SAMPLES, reason="MHWMM_RSZ_SAMPLES가 없습니다")
def test_real_samples_edit_in_place():
    """One in-place edit per kind on every real .user file that reads exactly: checked (V1-V5), read back,
    and written back to the original bytes."""
    reg = rsz.TypeRegistry.load()
    tried = 0
    for path in sorted(Path(SAMPLES).rglob("*")):
        if not path.is_file() or not R.is_data_file("natives/" + path.name):
            continue
        data = path.read_bytes()
        try:
            doc = R.DataFile(data, reg)
        except EditError:
            continue
        doc.selfcheck()
        picks = {}
        for value in doc.values():
            if value.editable and value.kind not in picks and _other_text(value) is not None:
                picks[value.kind] = value
        for value in picks.values():
            out = doc.write({value.at: _other_text(value)})
            assert len(out) == len(data), path.name
            back = R.DataFile(out, reg)
            assert R.same_value(value.kind, back.get(value.at).text, _other_text(value)), (path.name, value.at)
            assert back.write({value.at: value.text}) == data, (path.name, value.at)
            tried += 1
        game = Path(SAMPLES) / "game" / path.relative_to(Path(SAMPLES) / "mod") if "mod" in path.parts else None
        if game is not None and game.is_file():
            assert R.compare(data, game.read_bytes(), reg).note == "", path.name
    assert tried or True  # nothing to try is not an error: the folder may hold only stale files


def test_nan_is_never_written():
    with pytest.raises(EditError):
        R.parse_value("F32", str(math.nan))
