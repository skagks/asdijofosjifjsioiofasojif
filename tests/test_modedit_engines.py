"""The engine registry, the rebase rule and bulk values, with a fake engine over a tiny JSON format
(the real engines have their own tests: test_modedit_rszdata.py, test_modedit_textconfig.py)."""

from __future__ import annotations

import json
import math
import re
import struct
from pathlib import Path

import pytest

from mhwmm.modedit import EditError, StaleError, engines, model
from mhwmm.modedit.engines import Value, apply, float_text, plain_number, scaled
from mhwmm.modedit.model import Change

HEAD = b"USR\0"


# ---- a fake engine -----------------------------------------------------------------------------------
def fake_file(values: dict, **flags) -> bytes:
    """``values``: at → text, or at → (kind, text[, editable[, why[, raw]]]); flags: stale, broken."""
    rows = []
    for at, v in values.items():
        if isinstance(v, str):
            v = ("S32", v)
        kind, text, *rest = v
        editable = rest[0] if len(rest) > 0 else True
        why = rest[1] if len(rest) > 1 else ""
        raw = rest[2] if len(rest) > 2 else text
        rows.append({"at": at, "kind": kind, "text": text, "editable": editable, "why": why, "raw": raw})
    return HEAD + json.dumps({"values": rows, **flags}, ensure_ascii=False, sort_keys=True).encode("utf-8")


def read_fake(data: bytes) -> dict:
    """at → text of a fake file."""
    return {r["at"]: r["text"] for r in json.loads(data[len(HEAD):])["values"]}


def _pattern(rule_at: str) -> re.Pattern:
    return re.compile(re.escape(rule_at).replace(re.escape("[*]"), r"\[[^\]]*\]") + "$")


def fake_rule_of(at: str) -> str:
    """rszdata.rule_of for the simple addresses of these tests (no quoted keys)."""
    i = at.rfind("[")
    return at if i < 0 else at[:i] + "[*]" + at[at.index("]", i) + 1:]


class FakeDoc:
    def __init__(self, engine: "FakeEngine", data: bytes):
        self.engine = engine
        body = json.loads(data[len(HEAD):].decode("utf-8"))
        self.body = body
        self.notes = []
        self.rows = {r["at"]: Value(at=r["at"], label=r["at"].rsplit(".", 1)[-1], group="", kind=r["kind"],
                                    text=r["text"], editable=r["editable"], why=r["why"],
                                    rule_at=fake_rule_of(r["at"]) if "[" in r["at"] else "", raw=r["raw"])
                     for r in body["values"]}

    def values(self):
        return list(self.rows.values())

    def get(self, at):
        return self.rows.get(at)

    def expand(self, rule_at, before):
        pat = _pattern(rule_at)
        return [v for v in self.rows.values() if pat.match(v.at) and self.engine.same(v, v.text, before)]

    def write(self, changes):
        self.engine.writes.append(dict(changes))
        if self.engine.lie:
            return fake_file({r["at"]: (r["kind"], r["text"], r["editable"], r["why"], r["raw"])
                              for r in self.body["values"]})
        rows = []
        for r in self.body["values"]:
            r = dict(r)
            if r["at"] in changes:
                if not r["editable"]:
                    raise EditError(f"read-only {r['at']}")
                r["text"] = r["raw"] = changes[r["at"]]
            rows.append(r)
        return HEAD + json.dumps({**self.body, "values": rows}, ensure_ascii=False, sort_keys=True).encode("utf-8")


class FakeEngine:
    def __init__(self, name: str = "rsz", tool: str = "data"):
        self.name, self.tool = name, tool
        self.lie = False
        self.opened = 0
        self.writes = []

    def open(self, data, rel):
        self.opened += 1
        if not data.startswith(HEAD):
            raise EditError("not a fake file")
        body = json.loads(data[len(HEAD):].decode("utf-8"))
        if body.get("stale"):
            raise StaleError("지금 게임과 형식이 달라 읽지 못했습니다.")
        if body.get("broken"):
            raise EditError("이 파일은 고칠 수 없습니다.")
        return FakeDoc(self, data)

    def parse(self, value, text):
        kind = value.kind
        if kind in engines.INT_RANGES:
            try:
                n = int(text, 0)
            except ValueError:
                raise EditError("정수가 아닙니다") from None
            low, high = engines.INT_RANGES[kind]
            if not low <= n <= high:
                raise EditError(f"{low} ~ {high} 사이의 값이어야 합니다")
            return str(n)
        if kind in engines.FLOAT_KINDS:
            try:
                x = float(text)
                if not math.isfinite(x):
                    raise ValueError
                return float_text(x, kind)
            except (ValueError, OverflowError):
                raise EditError("실수가 아닙니다") from None
        if kind in engines.BOOL_KINDS:
            t = text.strip().lower()
            if t in ("true", "1", "켬"):
                return "true"
            if t in ("false", "0", "끔"):
                return "false"
            raise EditError("켬이나 끔이어야 합니다")
        return text

    def same(self, value, a, b):
        if value.kind in ("F32", "F16"):
            fmt = "<f" if value.kind == "F32" else "<e"
            try:
                return struct.pack(fmt, float(a)) == struct.pack(fmt, float(b))
            except (ValueError, OverflowError):
                return a == b
        if value.kind in engines.FLOAT_KINDS:
            try:
                return float(a) == float(b)
            except ValueError:
                return a == b
        return a == b


@pytest.fixture
def fake(monkeypatch):
    """A fake "rsz" engine in the registry for one test."""
    eng = FakeEngine()
    monkeypatch.setitem(engines._ENGINES, "rsz", eng)
    monkeypatch.setattr(model, "_rule_of", fake_rule_of)  # the real one: test_rule_of_of_the_data_engine
    return eng


def C(at, before, after, kind="S32", **kw):
    return Change(at=at, kind=kind, before=before, after=after, **kw)


# ---- the registry ------------------------------------------------------------------------------------
def test_register_get_and_unknown(monkeypatch):
    eng = FakeEngine("fake1", "data")
    engines.register(eng)
    try:
        assert engines.get("fake1") is eng
        assert engines.registered("fake1") is eng
        assert model.engine_tool("fake1") == "data"
    finally:
        engines.unregister("fake1")
    assert engines.registered("fake1") is None
    with pytest.raises(EditError, match="알 수 없는 수정 종류"):
        engines.get("fake1")


def test_engine_for(fake, monkeypatch):
    monkeypatch.setattr(model, "config_engine", lambda rel: "lua" if rel.endswith(".lua") else None)
    lua = FakeEngine("lua", "config")
    monkeypatch.setitem(engines._ENGINES, "lua", lua)
    assert engines.engine_for("natives/STM/GameDesign/Item/ItemData.user.3") is fake
    assert engines.engine_for("NATIVES/STM/a.USER.2") is fake
    assert engines.engine_for("x.pak", HEAD + b"rest") is fake
    assert engines.engine_for("x.pak", b"PFB\0") is None
    assert engines.engine_for("reframework/autorun/a.lua") is lua
    assert engines.engine_for("natives/STM/a.tex.241106027") is None


def test_importing_the_core_loads_no_engine_module():
    import subprocess
    import sys

    code = ("import sys; import mhwmm.modedit.engines, mhwmm.modedit.model, mhwmm.modedit.build; "
            "print(sorted(m for m in ('mhwmm.modedit.rszdata', 'mhwmm.modedit.textconfig', 'mhwmm.matfix.rsz') "
            "if m in sys.modules))")
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_rule_of_of_the_data_engine():
    rszdata = pytest.importorskip("mhwmm.modedit.rszdata")
    assert model._rule_of("_Values[_ItemId=101]._SellPrice") == rszdata.rule_of("_Values[_ItemId=101]._SellPrice")
    assert model._rule_of("_Values[_ItemId=101]._SellPrice") == "_Values[*]._SellPrice"


# ---- the rebase rule ---------------------------------------------------------------------------------
REL = "natives/STM/GameDesign/Item/ItemData.user.3"


def test_apply_todo_already_changed_gone_readonly_kind(fake):
    data = fake_file({"a": "1", "b": "5", "c": "7", "d": ("S32", "3", False, "다른 곳과 함께 쓰는 값이라 바꿀 수 없습니다"),
                      "e": ("F32", "1.0"), "f": "9"})
    changes = [C("a", "1", "2"),           # todo: still the value it was made on
               C("b", "1", "5"),           # already the wanted value
               C("c", "1", "100"),         # the author changed it: 1 → 7
               C("gone", "1", "2"),        # not in the file any more
               C("d", "3", "4"),           # read-only now
               C("e", "1", "2", kind="S32"),  # another kind now
               C("f", "9", "10", label="에프")]
    got = apply(fake, data, REL, changes)
    assert read_fake(got.data) == {"a": "2", "b": "5", "c": "7", "d": "3", "e": "1.0", "f": "10"}
    assert [c.at for c in got.done] == ["a", "f"]
    assert [c.at for c in got.already] == ["b"]
    assert [c.at for c in got.kept] == ["a", "b", "f"]
    why = {x.change.at: (x.why, x.now) for x in got.conflicts}
    assert why == {"c": ("changed", "7"), "gone": ("gone", None), "d": ("readonly", "3"), "e": ("kind", "1.0")}
    readonly = next(x for x in got.conflicts if x.why == "readonly")
    assert readonly.text() == "바꿀 수 없는 값이 되었습니다: 다른 곳과 함께 쓰는 값이라 바꿀 수 없습니다"
    assert next(x for x in got.conflicts if x.why == "changed").text() == "작성자가 바꾼 값: 1 → 7 (내 값 100)"
    assert fake.writes == [{"a": "2", "f": "10"}]


def test_apply_nothing_to_write_returns_the_input(fake):
    data = fake_file({"a": "2"})
    got = apply(fake, data, REL, [C("a", "1", "2")])
    assert got.data is data and fake.writes == [] and got.already and not got.done


def test_rules_are_judged_on_base_values_and_singles_override(fake):
    data = fake_file({"_V[1]._P": "99999", "_V[2]._P": "99999", "_V[3]._P": "50", "_V[4]._P": "99999",
                      "_V[5]._P": ("S32", "99999", False, "x"), "_V[6]._P": "1"})
    changes = [C("_V[*]._P", "99999", "5000"),
               C("_V[*]._P", "1", "100"),
               C("_V[*]._P", "7", "8"),          # no row has 7: norow
               C("_V[2]._P", "99999", "123"),   # a single value over a rule's row
               C("_V[4]._P", "99999", "99999")]  # (a no-op single is kept out of the targets too)
    got = apply(fake, data, REL, changes)
    assert read_fake(got.data) == {"_V[1]._P": "5000", "_V[2]._P": "123", "_V[3]._P": "50",
                                   "_V[4]._P": "99999", "_V[5]._P": "99999", "_V[6]._P": "100"}
    rules = [c for c in got.kept if c.rule]
    assert [(c.before, c.count) for c in rules] == [("99999", 1), ("1", 1)]  # row 5 is read-only, 2 and 4 single
    assert got.counts == {"_V[*]._P": 2}
    assert [(x.change.before, x.why, x.now) for x in got.conflicts] == [("7", "norow", None)]
    assert model.Conflict(changes[2], None, "norow").text() == "값이 7인 행이 없습니다"


def test_rule_after_an_update_also_covers_new_rows(fake):
    old = fake_file({"_V[1]._P": "10"})
    new = fake_file({"_V[1]._P": "10", "_V[9]._P": "10", "_V[10]._P": "11"})
    rule = C("_V[*]._P", "10", "20")
    assert apply(fake, old, REL, [rule]).kept[0].count == 1
    got = apply(fake, new, REL, [rule])
    assert read_fake(got.data) == {"_V[1]._P": "20", "_V[9]._P": "20", "_V[10]._P": "11"}
    assert got.kept[0].count == 2


def test_a_lying_engine_is_caught_by_the_post_check(fake):
    fake.lie = True
    with pytest.raises(EditError, match="확인하지 못해"):
        apply(fake, fake_file({"a": "1"}), REL, [C("a", "1", "2")])


def test_a_read_only_file_is_not_judged(fake, monkeypatch):
    real = fake.open

    def locked(data, rel):
        doc = real(data, rel)
        doc.readonly = "이 파일은 저장해 보니 원래와 달라져 고칠 수 없습니다 (로그 참고)."
        return doc

    monkeypatch.setattr(fake, "open", locked)
    with pytest.raises(EditError, match="저장해 보니"):
        apply(fake, fake_file({"a": "1"}), REL, [C("a", "1", "2")])
    assert apply(fake, fake_file({"a": "1"}), REL, []).data == fake_file({"a": "1"})
    left = model.Conflict(C("a", "0", "2"), "3", "changed")
    got = apply(fake, fake_file({"a": "1"}), REL, [], [left])  # its conflicts stay as they were
    assert got.still == [left] and not got.conflicts and not got.kept


def test_earlier_conflicts_are_judged_again(fake):
    """#1: conflicts left by an earlier build (an update, a repair) are judged on the file as it is now:
    the author's value again (with ``now`` as the file has it), the value the change was made on
    (applied), mine already (kept), still gone, a rule with rows now; one whose value this save sets
    goes."""
    data = fake_file({"a": "70", "b": "60", "c": "5000", "d": "1", "_V[1]._P": "7", "_V[2]._P": "6"})
    a, b, c = C("a", "60", "5000"), C("b", "60", "5000"), C("c", "60", "5000")
    gone, rule, d = C("gone", "1", "2"), C("_V[*]._P", "7", "8"), C("d", "1", "9")
    left = [model.Conflict(a, "50", "changed"), model.Conflict(b, "50", "changed"), model.Conflict(c, "50", "changed"),
            model.Conflict(gone, None, "gone"), model.Conflict(rule, None, "norow"), model.Conflict(d, "2", "changed")]
    got = apply(fake, data, REL, [C("d", "1", "4")], left)
    assert read_fake(got.data) == {"a": "70", "b": "5000", "c": "5000", "d": "4", "_V[1]._P": "8", "_V[2]._P": "6"}
    assert [(x.change.at, x.why, x.now) for x in got.still] == [("a", "changed", "70"), ("gone", "gone", None)]
    assert not got.conflicts
    assert got.kept == [C("d", "1", "4"), b, c, rule] and got.kept[3].count == 1
    assert got.done == [C("d", "1", "4"), b, rule] and got.already == [c]
    assert apply(fake, data, REL, [], []).still == []


def test_open_errors_propagate(fake):
    with pytest.raises(StaleError):
        apply(fake, fake_file({"a": "1"}, stale=True), REL, [C("a", "1", "2")])
    with pytest.raises(EditError):
        apply(fake, fake_file({"a": "1"}, broken=True), REL, [C("a", "1", "2")])


def test_config_raw_path(monkeypatch):
    lua = FakeEngine("lua", "config")
    data = fake_file({"Config.Scale": ("float", "2.0", True, "", "2.00")})
    # same() fails (the canonical text was 1.0 then), but the literal is still the one the change was made on
    c = Change(at="Config.Scale", kind="float", before="1.0", after="1.5", raw="2.00")
    got = apply(lua, data, "reframework/autorun/x.lua", [c])
    assert read_fake(got.data) == {"Config.Scale": "1.5"} and got.done == [c]
    # without a raw literal (or another one), it is a conflict
    for raw in ("", "1.0"):
        got = apply(lua, data, "reframework/autorun/x.lua", [Change("Config.Scale", "float", "1.0", "1.5", raw=raw)])
        assert [x.why for x in got.conflicts] == ["changed"]
    # data engines never use raw
    rsz = FakeEngine()
    got = apply(rsz, fake_file({"a": ("S32", "2", True, "", "1")}), REL, [Change("a", "S32", "1", "3", raw="1")])
    assert [x.why for x in got.conflicts] == ["changed"]


def test_float_values_compare_by_bits(fake):
    data = fake_file({"r": ("F32", "0.1")})
    got = apply(fake, data, REL, [C("r", "0.100000001", "0.5", kind="F32")])
    assert read_fake(got.data) == {"r": "0.5"}


# ---- canonical floats and bulk values ---------------------------------------------------------------------
def test_float_text():
    assert float_text(0.1, "F32") == "0.1"
    assert float_text(10.0, "F32") == "10.0"
    assert float_text(struct.unpack("<f", struct.pack("<f", 0.1))[0] * 3, "F32") == "0.3"
    assert float_text(1 / 3, "F32") == "0.33333334"
    assert float_text(65504.0, "F16") == "65504.0"
    assert float_text(0.1, "F64") == "0.1" and float_text(2.0) == "2.0"
    assert float_text(1e20, "F32") == "1e+20"


def V(kind, text="0", **kw):
    return Value(at="x", label="x", group="", kind=kind, text=text, **kw)


def test_scaled_ints_round_half_away_and_clamp(fake):
    assert scaled(fake, V("S32"), "times", 2.5, "1", "1") == ("3", "")
    assert scaled(fake, V("S32"), "times", 2.5, "-1", "-1") == ("-3", "")
    assert scaled(fake, V("S32"), "times", 0.5, "3", "3") == ("2", "")
    assert scaled(fake, V("U8"), "times", 3, "100", "100") == ("255", "범위를 넘어 255로 맞췄습니다")
    assert scaled(fake, V("U8"), "set", "-5", "1", None) == ("0", "범위를 넘어 0로 맞췄습니다")
    assert scaled(fake, V("S32"), "set", "1,000", "1", None) == ("1000", "")
    assert scaled(fake, V("S64"), "times", 3, "3074457345618258602", "3074457345618258602") == (
        "9223372036854775806", "")  # exact, not through a float


def test_scaled_floats_and_change_mode(fake):
    text, note = scaled(fake, V("F32"), "times", 3, "0.1", "0.1")
    assert (text, note) == ("0.3", "")
    # 3배 모드를 2배로: game 1, mod 10 → game + k·(mod − game) with k = 1/9 · (2 − 1)·… here k = 1/3
    assert scaled(fake, V("F32"), "change", 1 / 3, "10.0", "1.0") == ("4.0", "")
    assert scaled(fake, V("S32"), "change", 0.5, "300", "100") == ("200", "")
    assert scaled(fake, V("F64"), "set", "2.5", "1", None) == ("2.5", "")
    assert scaled(fake, V("F32"), "times", 1e39, "1.0", "1.0")[0] is None  # does not fit a float32


def test_scaled_skips(fake):
    assert scaled(fake, V("Vec3"), "set", "1", "1, 2, 3", None) == (None, "벡터 값은 하나씩 바꾸세요")
    assert scaled(fake, V("S32"), "times", 2, "1", None) == (None, "게임 값이 없어 건너뛴 값")
    assert scaled(fake, V("F32"), "change", 2, "1", None) == (None, "게임 값이 없어 건너뛴 값")
    assert scaled(fake, V("Bool"), "set", "켬", "false", None) == ("true", "")
    assert scaled(fake, V("Bool"), "times", 2, "true", "true")[0] is None
    assert scaled(fake, V("String"), "set", "abc", "x", None) == ("abc", "")
    assert scaled(fake, V("String"), "times", 2, "x", "x")[0] is None
    assert scaled(fake, V("F32"), "set", "nan", "1", None)[0] is None
    with pytest.raises(ValueError):
        scaled(fake, V("S32"), "double", 2, "1", "1")


COMMA = "숫자를 적어 주세요 (소수점은 . 으로): "


def test_only_thousands_separators_are_taken_out_of_a_number():
    """#27: a comma is dropped only as a thousands separator (1,000); any other comma is refused, not
    dropped: a decimal comma (0,5) used to read as 5, ten times the 0.5 meant."""
    for text, x in (("1,000", 1000.0), ("-12,345.5", -12345.5), ("+1,000,000", 1e6), (" 2.5 ", 2.5),
                    ("0x10", 16.0), ("1e3", 1000.0), (3, 3.0), (0.5, 0.5)):
        assert plain_number(text) == x, text
    for text in ("0,5", "1,5", ",5", "5,", "1,00", "1,0000", "1,5e3", "1,2,3", "0x1,0", "0, 5", "1,000,5"):
        with pytest.raises(EditError) as got:
            plain_number(text)
        assert str(got.value) == COMMA + text.strip(), text
    for text in ("abc", "", "nan", "inf", "-inf", "1e999", "1.2.3"):
        with pytest.raises(EditError, match=r"^숫자를 적어 주세요\.$"):
            plain_number(text)
    assert isinstance(EditError("x"), ValueError)  # (plain_number's callers may catch ValueError)


def test_scaled_refuses_a_decimal_comma(fake):
    """#27: the window's 한꺼번에 적용 and scale --to/--times/--change: every number row is skipped with the
    reason (the rows get nothing); a text row's 값 정하기 keeps the text as typed."""
    for mode, k in (("times", "0,5"), ("change", "1,5"), ("set", "0,5")):
        for kind in ("S32", "F32"):
            assert scaled(fake, V(kind), mode, k, "100", "10") == (None, COMMA + k), (mode, k, kind)
    assert scaled(fake, V("String"), "set", "0,5", "x", None) == ("0,5", "")
    assert scaled(fake, V("S32"), "times", "1,000", "1", "1") == ("1000", "")
    assert scaled(fake, V("F32"), "times", "0.5", "100.0", "10.0") == ("5.0", "")
    assert scaled(fake, V("S32"), "set", "abc", "1", None) == (None, "숫자로 읽지 못해 건너뛴 값")
