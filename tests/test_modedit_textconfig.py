"""스크립트 설정 (mhwmm/modedit/textconfig.py): the Lua, JSON, INI and TOML readers and writers on
synthetic samples, and the engines the 모드 수정 window and CLI use.

The Lua reader was checked against a real Lua 5.4: with lupa installed (not a dependency) the tests
that need it compile every rewritten file and compare each value with what Lua computes; without it
the same cases are checked against expectations baked from such a run.  TOML values are editable only
with tomllib (Python 3.11+)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

import modedit_fixtures as mf
from mhwmm.modedit import EditError, engines
from mhwmm.modedit import textconfig as tc
from mhwmm.modedit.model import Change


# ------------------------------------------------------------------------- Lua helpers (lupa)
@pytest.fixture
def lua():
    """Lua 5.4 through lupa; the test is skipped without it."""
    return pytest.importorskip("lupa.lua54")


def lua_compiles(lupa, src: bytes, tmp: Path) -> str | None:
    """None when Lua 5.4 compiles src as REFramework does (luaL_loadfilex via sol's script_file: a UTF-8
    BOM and a first line starting with '#' are skipped), without running it; else the error."""
    path = tmp / "probe.lua"
    path.write_bytes(src)
    rt = lupa.LuaRuntime(unpack_returned_tuples=True, encoding=None)
    ok, err = rt.eval("function(p) local f, e = loadfile(p, 't'); return f ~= nil, e end")(str(path).encode())
    return None if ok else (err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err))


def lua_values(lupa, src: bytes, keys: list[str]) -> dict:
    """Run a synthetic sample and read each item path: key -> (math.type or type, value)."""
    rt = lupa.LuaRuntime(unpack_returned_tuples=True, encoding=None)
    text = src.decode("utf-8")
    if any(k.startswith("return") for k in keys) and "return {" in text:
        text = text.replace("return {", "local __ret = {", 1)
    exprs = [("__ret" + k[len("return"):]) if k.startswith("return") else k for k in keys]
    probe = "\nreturn {" + ", ".join(f"{{ (math.type({e}) or type({e})), {e} }}" for e in exprs) + "}\n"
    res = rt.execute((text + probe).encode("utf-8"))
    out = {}
    for i, k in enumerate(keys, 1):
        pair = res[i]
        value = pair[2]
        out[k] = (pair[1].decode(), value.decode("utf-8") if isinstance(value, bytes) else value)
    return out


LUA_TYPES = {"int": "integer", "float": "float", "bool": "boolean", "string": "string"}


def check_lua_items(lupa, src: bytes):
    """Every editable item's parsed value equals what Lua 5.4 computes."""
    sc = tc.scan(src, "lua")
    assert not sc.errors, sc.errors
    got = lua_values(lupa, src, [i.key for i in sc.items if i.editable])
    for item in sc.items:
        if not item.editable:
            continue
        t, v = got[item.key]
        assert t == LUA_TYPES[item.kind], (item.key, t)
        assert v == item.value or (isinstance(v, float) and math.isclose(v, item.value)), (item.key, v, item.value)
    return sc


def check_baked(src: bytes, baked: dict):
    """Every editable item's kind and value equal what Lua 5.4 computed when the test was written."""
    sc = tc.scan(src, "lua")
    assert not sc.errors, sc.errors
    assert {i.key for i in sc.items if i.editable} == set(baked)
    for item in sc.items:
        if item.editable:
            t, v = baked[item.key]
            assert (LUA_TYPES[item.kind], item.value) == (t, v), item.key
            assert type(item.value) is type(v), item.key
            if isinstance(v, float):
                assert math.copysign(1, v) == math.copysign(1, item.value), item.key
    return sc


SAMPLE = """\
-- My mod settings
local SPEED = 1.5 -- how fast
local COUNT <const> = 10
local NAME = "a -- not a comment"
local QUOTE = 'it\\'s "fine"'
local LONG = [==[
line one
]] still inside -- not a comment
]==]
local NEG = - 3
local HEX = 0xFF00ff00
local EXP = 1e-3
local DOT = 1.
local LEAD = .5
local A, B = 1, "two"
local config = {
    enabled = true,
    ratio = -0.25,
    list = { 1, 2.5, "three", false, },
    ["key with space"] = "v",
    [10] = "ten",
    nested = { deep = { x = 7 }; y = "z" },
    fn = function() local a, b = 1, 2 return a + b end,
    expr = 1 + 2,
    call = math.max(1, 2),
}
config.extra = 42
config["more"] = "m"
local function helper() return { not_an_item = 1 } end
if SPEED > 1 then
    local inner = 99
end
"""

# what Lua 5.4 computes for SAMPLE's items (baked with lupa)
SAMPLE_LUA = {
    "SPEED": ("float", 1.5), "COUNT": ("integer", 10), "NAME": ("string", "a -- not a comment"),
    "QUOTE": ("string", "it's \"fine\""), "LONG": ("string", "line one\n]] still inside -- not a comment\n"),
    "NEG": ("integer", -3), "HEX": ("integer", 4278255360), "EXP": ("float", 0.001), "DOT": ("float", 1.0),
    "LEAD": ("float", 0.5), "A": ("integer", 1), "B": ("string", "two"), "config.enabled": ("boolean", True),
    "config.ratio": ("float", -0.25), "config.list[1]": ("integer", 1), "config.list[2]": ("float", 2.5),
    "config.list[3]": ("string", "three"), "config.list[4]": ("boolean", False),
    'config["key with space"]': ("string", "v"), "config[10]": ("string", "ten"),
    "config.nested.deep.x": ("integer", 7), "config.nested.y": ("string", "z"), "config.extra": ("integer", 42),
    "config.more": ("string", "m"),
}

# (key, value, the literal written in its place): each rewritten file compiled with Lua 5.4 and read
# back as that value when this was baked
REWRITES = [
    ("SPEED", 2.0, b"2.0"), ("SPEED", -0.125, b"-0.125"), ("SPEED", 1e20, b"1e+20"), ("COUNT", -7, b"-7"),
    ("NAME", "x -- y \" ' \\ \n\t end", b'"x -- y \\" \' \\\\ \\n\\t end"'),
    ("QUOTE", "한국어 값", "'한국어 값'".encode()),
    ("LONG", "]] ]=] ]==] tricky\n", b"[===[]] ]=] ]==] tricky\n]===]"),
    ("LONG", "\nleading newline", b"[==[\n\nleading newline]==]"), ("NEG", 5, b"5"), ("NEG", -10, b"-10"),
    ("HEX", 0x12, b"0x00000012"), ("HEX", -1, b"-0x00000001"), ("EXP", 3.0, b"3.0"), ("DOT", 0.1, b"0.1"),
    ("A", 9007199254740993, b"9007199254740993"), ("B", "", b'""'), ("config.enabled", False, b"false"),
    ("config.ratio", 4.0, b"4.0"), ("config.list[2]", 0.0, b"0.0"), ("config.list[3]", "]]", b'"]]"'),
    ("config.list[4]", True, b"true"), ('config["key with space"]', "\x00\x01 bytes", b'"\\000\\001 bytes"'),
    ("config.nested.deep.x", 123456, b"123456"), ("config.extra", 0, b"0"),
    ("config.more", "--[[ not a comment ]]", b'"--[[ not a comment ]]"'),
]

# lexer edge cases: the items Lua 5.4 computes (baked with lupa)
LEXER_EDGES = [
    (b"local s = 'a\\z   \n   b'\n", {"s": ("string", "ab")}),
    (b"local s = \"\\65\\066\\x43\\u{48}\\u{D55C}\"\n", {"s": ("string", "ABCH한")}),
    (b"local s = 'line\\\nnext'\n", {"s": ("string", "line\nnext")}),
    (b"local s = [[\r\nfirst skipped\r\nsecond]]\n", {"s": ("string", "first skipped\nsecond")}),
    (b"local n = 0x1p4\n", {"n": ("float", 16.0)}),
    (b"local n = 0xA.8p0\n", {"n": ("float", 10.5)}),
    (b"local n = 0xffffffffffffffff\n", {"n": ("integer", -1)}),
    (b"local n = 3e+2\n", {"n": ("float", 300.0)}),
    (b"local n = -0.0\n", {"n": ("float", -0.0)}),
    (b"local s = [==[]]]==]\n", {"s": ("string", "]]")}),
    (b"#!/usr/bin/lua\nlocal n = 4\n", {"n": ("integer", 4)}),
    (b"\xef\xbb\xbflocal n = 5\n", {"n": ("integer", 5)}),
    (b"local n = 1 -- c1\n--[==[ long\ncomment ]==] local m = 2\n", {"n": ("integer", 1), "m": ("integer", 2)}),
    (b"local t = {[1.0] = 'one', [2] = 'two'}\n", {"t[1]": ("string", "one"), "t[2]": ("string", "two")}),
    (b"local n = 0x8000000000000000\n", {"n": ("integer", -9223372036854775808)}),
    # integers of hundreds or thousands of digits (past a double, or Python's limit for int(text)): read-only
    # floats in Lua (inf), or an ordinary integer after many leading zeros
    (b"local a = 1" + b"0" * 400 + b"\nlocal b = " + b"1" * 5000 + b"\nlocal c = 2\n", {"c": ("integer", 2)}),
    (b"local n = " + b"0" * 5000 + b"5\n", {"n": ("integer", 5)}),
]


# ------------------------------------------------------------------------------- Lua tests
def test_lua_items_found():
    sc = tc.scan(SAMPLE.encode(), "lua")
    assert not sc.errors
    keys = {i.key: i for i in sc.items}
    assert keys["SPEED"].value == 1.5 and keys["SPEED"].kind == "float"
    assert keys["SPEED"].note.endswith("how fast")
    assert keys["COUNT"].value == 10 and keys["COUNT"].kind == "int"
    assert keys["NAME"].value == "a -- not a comment"
    assert keys["QUOTE"].value == "it's \"fine\""
    assert keys["LONG"].value == "line one\n]] still inside -- not a comment\n"
    assert keys["NEG"].value == -3 and keys["NEG"].raw == "- 3"
    assert keys["HEX"].value == 0xFF00FF00 and keys["HEX"].style == "hex"
    assert keys["EXP"].value == 0.001
    assert keys["DOT"].value == 1.0 and keys["DOT"].kind == "float"
    assert keys["LEAD"].value == 0.5
    assert keys["A"].value == 1 and keys["B"].value == "two"
    assert keys["config.enabled"].value is True
    assert keys["config.ratio"].value == -0.25
    assert [keys[f"config.list[{n}]"].value for n in range(1, 5)] == [1, 2.5, "three", False]
    assert keys['config["key with space"]'].value == "v"
    assert keys["config[10]"].value == "ten"
    assert keys["config.nested.deep.x"].value == 7
    assert keys["config.nested.y"].value == "z"
    assert keys["config.extra"].value == 42
    assert keys["config.more"].value == "m"  # ["more"] is .more
    for absent in ("config.fn", "config.expr", "config.call", "not_an_item", "inner"):
        assert absent not in keys
    assert all(i.editable for i in sc.items), [(i.key, i.why) for i in sc.items if not i.editable]


def test_lua_values_match_real_lua(lua):
    check_lua_items(lua, SAMPLE.encode())


def test_lua_values_match_baked_lua():
    check_baked(SAMPLE.encode(), SAMPLE_LUA)


@pytest.mark.parametrize("key,value,_literal", REWRITES)
def test_lua_rewrite_one_value(lua, tmp_path, key, value, _literal):
    src = SAMPLE.encode()
    new = tc.set_value(src, "lua", key, value)
    assert lua_compiles(lua, new, tmp_path) is None
    item = tc.scan(src, "lua").get(key)
    lit = tc.scan(new, "lua").get(key)
    assert new[:item.start] == src[:item.start]  # every byte outside the value is kept
    assert new[lit.end:] == src[item.end:]
    got = lua_values(lua, new, [key])[key]
    assert got[1] == value, got
    assert got[0] == LUA_TYPES[item.kind]
    check_lua_items(lua, new)


@pytest.mark.parametrize("key,value,literal", REWRITES)
def test_lua_rewrite_one_value_baked(key, value, literal):
    src = SAMPLE.encode()
    item = tc.scan(src, "lua").get(key)
    new = tc.set_value(src, "lua", key, value)
    assert new == src[:item.start] + literal + src[item.end:]
    back = tc.scan(new, "lua").get(key)
    assert back.kind == item.kind and back.value == value and type(back.value) is type(value)
    baked = dict(SAMPLE_LUA)
    baked[key] = (LUA_TYPES[item.kind], value)
    check_baked(new, baked)


def test_lua_keeps_type():
    src = SAMPLE.encode()
    with pytest.raises(tc.EditError):
        tc.set_value(src, "lua", "COUNT", 1.5)  # int stays int
    new = tc.set_value(src, "lua", "SPEED", 3)  # 3 -> 3.0 for a float
    assert b"local SPEED = 3.0 -- how fast" in new
    with pytest.raises(tc.EditError):
        tc.set_value(src, "lua", "config.enabled", 1)
    with pytest.raises(tc.EditError):
        tc.set_value(src, "lua", "SPEED", float("inf"))
    with pytest.raises(tc.EditError):
        tc.set_value(src, "lua", "NAME", 5)


def test_lua_hex_style_kept():
    new = tc.set_value(SAMPLE.encode(), "lua", "HEX", 0xABCDEF)
    assert b"local HEX = 0x00ABCDEF" in new  # same digit count, upper case kept from the original


def test_lua_set_values_many():
    src = SAMPLE.encode()
    new = tc.set_values(src, "lua", {"SPEED": 9.5, "config.list[1]": 100, "config.nested.y": "zz"})
    sc = tc.scan(new, "lua")
    assert sc.get("SPEED").value == 9.5 and sc.get("config.list[1]").value == 100
    assert sc.get("config.nested.y").value == "zz"


@pytest.mark.parametrize("src,why", [
    ("local x = 1\nx = 2\n", "여러 번"),  # set twice at top level
    ("local t = { a = 1 }\nt.a = 2\n", "여러 번"),
    ("local t = { a = 1, a = 2 }\n", "여러 번"),  # order of duplicate keys is undefined
    ("local t = { 'a', [1] = 'b' }\n", "여러 번"),
    # a key set to a literal and to a table (or to two tables): the literal and everything in the tables
    ("local t = { hud = true, hud = { scale = 1.0 } }\n", "여러 번"),
    ("local t = { hud = { scale = 1.0 }, hud = true }\n", "여러 번"),
    ("local t = { hud = { a = 1 }, hud = { b = 2 } }\n", "여러 번"),
    ("local t = { hud = { a = 1 }, hud = f() }\n", "여러 번"),
    ("local t = { { 1, 2 }, [1] = 5 }\n", "여러 번"),
    ("local t = { [1] = { 1, 2 }, 5 }\n", "여러 번"),
    ("return { cfg = { on = true }, ['cfg'] = { size = 2 } }\n", "여러 번"),
    ("local t = { a = 1 }\nt = {}\n", "다시 정함"),  # replaced by a literal later
    ("local x = nil\n", "nil"),
    ("local x = 9223372036854775808\n", "큰 정수"),
    ("local s = '\\xff\\xfe'\n", "UTF-8"),
])
def test_lua_read_only_cases(src, why):
    sc = tc.scan(src.encode(), "lua")
    assert not sc.errors
    assert sc.items and all(not i.editable for i in sc.items if i.kind != "string" or "xff" in src), \
        [(i.key, i.editable, i.why) for i in sc.items]
    assert any(why in i.why for i in sc.items), [(i.key, i.why) for i in sc.items]


def test_lua_key_set_to_a_table_twice_is_not_written():
    """Lua keeps one of the values a constructor gives one key: an edit of the other one would do nothing."""
    src = b"local cfg = {\n    hud = true,\n    hud = { scale = 1.0 },\n}\nlocal list = { { 1, 2 }, [1] = 5 }\n"
    sc = tc.scan(src, "lua")
    assert {i.key: (i.editable, i.why) for i in sc.items} == {
        "cfg.hud": (False, tc.DUPLICATE_WHY),
        "cfg.hud.scale": (False, "'cfg.hud' 값이 여러 번 정해져 있어 어느 것이 쓰이는지 확실하지 않음"),
        "list[1][1]": (False, "'list[1]' 값이 여러 번 정해져 있어 어느 것이 쓰이는지 확실하지 않음"),
        "list[1][2]": (False, "'list[1]' 값이 여러 번 정해져 있어 어느 것이 쓰이는지 확실하지 않음"),
        "list[1]": (False, tc.DUPLICATE_WHY),
    }
    for key, value in (("cfg.hud", False), ("cfg.hud.scale", 2.0), ("list[1]", 9)):
        with pytest.raises(EditError, match="바꿀 수 없습니다"):
            tc.set_value(src, "lua", key, value)


# a table set again in a later statement (the "later" rule), or tables at different keys: the values Lua
# 5.4 computes for the editable items (baked with lupa)
TABLES_SET_LATER = [
    (b"local settings = { hud = true }\nsettings.hud = { scale = 1 }\n", {"settings.hud.scale": ("integer", 1)}),
    (b"local cfg = {}\ncfg = { a = 1 }\n", {"cfg.a": ("integer", 1)}),
    (b"local t = { a = { x = 1 }, a = { y = 2 } }\nt.a = 5\n", {"t.a": ("integer", 5)}),
    (b"local list = { { 1, 2 }, { 3 }, n = { 4 } }\n",
     {"list[1][1]": ("integer", 1), "list[1][2]": ("integer", 2), "list[2][1]": ("integer", 3),
      "list.n[1]": ("integer", 4)}),
]


@pytest.mark.parametrize("src,baked", TABLES_SET_LATER)
def test_lua_tables_set_later_keep_their_fields_baked(src, baked):
    check_baked(src, baked)


@pytest.mark.parametrize("src,_baked", TABLES_SET_LATER)
def test_lua_tables_set_later_keep_their_fields(lua, src, _baked):
    check_lua_items(lua, src)


def test_lua_default_overridden_by_saved_file_is_a_warning():
    src = b'local cfg = { speed = 1 }\ncfg = json.load_file("MyMod/config.json") or cfg\n'
    sc = tc.scan(src, "lua")
    item = sc.get("cfg.speed")
    assert item.editable and "기본값" in item.warn
    assert sc.data_files == ["MyMod/config.json"]
    assert sc.notes and "MyMod/config.json" in sc.notes[0]


def test_lua_not_top_level_or_not_literal():
    src = b"""
local a = 1 + 2
local b = -x
local c = not true
local d = "a" .. "b"
local e = f "str"
local g = { h = 1 } .. x
local i = - - 1
local j = 2 ^ -1
local k = #"abc"
do local l = 1 end
while false do local m = 1 end
for n = 1, 2 do local o = 3 end
repeat local p = 1 until true
local q = (5)
"""
    sc = tc.scan(src, "lua")
    assert not sc.errors, sc.errors
    assert [i.key for i in sc.items] == []


def test_lua_return_table_module():
    src = b"return {\n  volume = 0.8, -- 0..1\n  keys = { open = 'F1' },\n}\n"
    sc = tc.scan(src, "lua")
    assert [i.key for i in sc.items] == ["return.volume", "return.keys.open"]
    assert sc.get("return.volume").note == "0..1"
    new = tc.set_value(src, "lua", "return.keys.open", "F2")
    assert new == src.replace(b"'F1'", b"'F2'")


BAD_LUA = [
    b"local x = 'unfinished\n",
    b"local x = [[unfinished\n",
    b"--[[ unfinished comment\nlocal x = 1\n",
    b"local x = 1..2\n",
    b"local x = 0x\n",
    b"local x = 3f\n",
    b"local x = { 1, 2\n",
    b"function f() local x = 1\n",
    b"local x = '\\q'\n",
    b"local x = 1 @\n",
    b"local x = [=x\n",
    b"if x then\n",
    b"end\n",
]


@pytest.mark.parametrize("src", BAD_LUA)
def test_lua_refuses_bad_files(src):
    sc = tc.scan(src, "lua")
    assert sc.errors and not sc.items
    with pytest.raises(tc.EditError):
        tc.set_value(src, "lua", "x", 1)


@pytest.mark.parametrize("src", BAD_LUA)
def test_lua_refuses_what_lua_refuses(lua, tmp_path, src):
    assert lua_compiles(lua, src, tmp_path) is not None


@pytest.mark.parametrize("src,_baked", LEXER_EDGES)
def test_lua_lexer_edge_values(lua, tmp_path, src, _baked):
    sc = tc.scan(src, "lua")
    assert not sc.errors, sc.errors
    assert lua_compiles(lua, src, tmp_path) is None
    body = src[3:] if src.startswith(b"\xef\xbb\xbf") else src
    if body.startswith(b"#"):
        body = body.split(b"\n", 1)[1]
    check_lua_items(lua, body)


@pytest.mark.parametrize("src,baked", LEXER_EDGES)
def test_lua_lexer_edge_values_baked(src, baked):
    check_baked(src, baked)


def test_lua_huge_integers_are_read_only_floats():
    sc = tc.scan(b"local a = 1" + b"0" * 400 + b"\nlocal b = -" + b"1" * 5000 + b"\n", "lua")
    assert [(i.key, i.value, i.editable) for i in sc.items] == [("a", math.inf, False), ("b", -math.inf, False)]
    assert sc.items[0].why == "Lua가 소수로 읽는 큰 정수"


@pytest.mark.parametrize("literal,written", [
    ("5", "0x8000000000000000"), ("-3", "0x8000000000000000"), ("0XAB", "0X8000000000000000"),
    ("- 0x1f", "0x8000000000000000"),
])
def test_lua_smallest_integer_is_written_in_hex(literal, written):
    """-9223372036854775808 is a float in Lua (the minus is applied to an integer one too big): the smallest
    integer is written in hex, which wraps around to it, so what 적용 accepts is saved as an integer."""
    src = f"local x = {literal}\nlocal y = 1\n".encode()
    engine = engines.get("lua")
    v = engine.open(src, "reframework/autorun/x.lua").get("x")
    text = engine.parse(v, "-9223372036854775808")
    result = engines.apply(engine, src, "reframework/autorun/x.lua", [Change(v.at, v.kind, v.text, text)])
    assert result.data == f"local x = {written}\nlocal y = 1\n".encode() and len(result.done) == 1
    item = tc.scan(result.data, "lua").get("x")
    assert (item.kind, item.value, item.editable) == ("int", tc.INT64_MIN, True)
    item = tc.scan(b"local x = -9223372036854775808\n", "lua").get("x")  # (as written: a float)
    assert not item.editable and item.why == "Lua가 소수로 읽는 큰 정수"


def test_lua_smallest_integer_in_lua(lua):
    got = lua_values(lua, b"local x = 0x8000000000000000\nlocal y = -9223372036854775808\n", ["x", "y"])
    assert got == {"x": ("integer", tc.INT64_MIN), "y": ("float", float(tc.INT64_MIN))}


def test_lua_crlf_and_bom_kept():
    src = b"\xef\xbb\xbflocal a = 1\r\nlocal b = 'x'\r\n"
    new = tc.set_value(src, "lua", "b", "y")
    assert new == b"\xef\xbb\xbflocal a = 1\r\nlocal b = 'y'\r\n"


def test_lua_field_assignments_module_style():
    src = b"local M = {}\nM.USER_CONFIG_PATH = 'X/UserConfig.json'\nM.MAX = 9999\nM.COLOR = 0xeb4034ff\n" \
          b"M.BTN = Vector2f.new(300, 50)\nM.LIST = { 50000, 200000 }\nreturn M\n"
    sc = tc.scan(src, "lua")
    assert [i.key for i in sc.items] == ["M.USER_CONFIG_PATH", "M.MAX", "M.COLOR", "M.LIST[1]", "M.LIST[2]"]
    assert sc.get("M.COLOR").style == "hex"


# ------------------------------------------------------------------------------ JSON tests
JSON_SAMPLE = """{
    "enabled": true,
    "scale": 1.0,
    "count": 3,
    "name": "\\ud55c\\uae00 \\"q\\"",
    "list": [1, 2.5, "x", null, false],
    "nested": {"deep": {"v": -1e-5}},
    "empty": {},
    "weird key.with dots": 0
}"""


def test_json_items_and_rewrite_keeps_format():
    src = JSON_SAMPLE.encode()
    sc = tc.scan(src, "json")
    assert not sc.errors
    keys = {i.key: i for i in sc.items}
    assert keys["scale"].kind == "float" and keys["count"].kind == "int"
    assert keys["name"].value == '한글 "q"'
    assert keys["list[3]"].kind == "nil" and not keys["list[3]"].editable
    assert keys['["weird key.with dots"]'].value == 0
    new = tc.set_value(src, "json", "scale", 2)
    assert new == src.replace(b'"scale": 1.0', b'"scale": 2.0')
    new = tc.set_value(src, "json", "nested.deep.v", 0.5)
    assert json.loads(new)["nested"]["deep"]["v"] == 0.5
    assert new.replace(b"0.5", b"-1e-5") == src
    new = tc.set_value(src, "json", "name", "새 \"값\"\n")
    assert json.loads(new)["name"] == "새 \"값\"\n"
    with pytest.raises(tc.EditError):
        tc.set_value(src, "json", "count", 1.5)


@pytest.mark.parametrize("src", [
    b'{"a": 1,}',  # trailing comma (nlohmann refuses)
    b'{"a": 1} // comment',
    b'{"a": NaN}',
    b'{"a": Infinity}',
    b"{'a': 1}",
    b'{"a": 01}',
    b'{"a": "\\ud800"}',
    b'{"a": "tab\there"}',
    b'[1, 2',
    b'\xff\xfe{}',
])
def test_json_refused(src):
    sc = tc.scan(src, "json")
    assert sc.errors and not sc.items


def test_json_duplicate_keys_read_only():
    sc = tc.scan(b'{"a": 1, "a": 2, "b": {"c": 1}, "b": {"c": 2}}', "json")
    assert sc.items and not any(i.editable for i in sc.items)


def test_json_bom_and_crlf_kept():
    src = b'\xef\xbb\xbf{\r\n  "a": "x"\r\n}\r\n'
    assert tc.set_value(src, "json", "a", "y") == b'\xef\xbb\xbf{\r\n  "a": "y"\r\n}\r\n'


def test_json_refw_style_dump():
    # what REFramework's json.dump_file writes (nlohmann, indent 4, sorted keys, no newline at the end)
    src = b'{\n    "Enabled": true,\n    "Hotkey": 112,\n    "Opacity": 0.800000011920929\n}'
    new = tc.set_value(src, "json", "Opacity", 0.5)
    assert new == b'{\n    "Enabled": true,\n    "Hotkey": 112,\n    "Opacity": 0.5\n}'


# ------------------------------------------------------------------------------- INI tests
INI_SAMPLE = """\ufeff; ReShade preset
PreprocessorDefinitions=
Techniques=Bloom@Bloom.fx,SMAA@SMAA.fx

[Bloom.fx]
; strength of the glow
BloomStrength=0.250000
Enabled = true
Name = "quoted value"
Path=C:\\Games\\x ; not a comment for every parser

[General]
Scale=2
Scale2=1e3
"""


def test_ini_items_and_rewrite():
    src = INI_SAMPLE.encode("utf-8")
    sc = tc.scan(src, "ini")
    assert not sc.errors
    keys = {i.key: i for i in sc.items}
    assert keys["Techniques"].value == "Bloom@Bloom.fx,SMAA@SMAA.fx"
    assert keys["[Bloom.fx] BloomStrength"].kind == "float"
    assert keys["[Bloom.fx] BloomStrength"].note == "strength of the glow"
    assert keys["[Bloom.fx] Enabled"].kind == "bool"
    assert keys["[Bloom.fx] Name"].value == "quoted value"
    assert keys["[Bloom.fx] Path"].kind == "string"
    assert keys["[General] Scale"].kind == "int"
    new = tc.set_value(src, "ini", "[Bloom.fx] BloomStrength", 0.5)
    assert new == src.replace(b"BloomStrength=0.250000", b"BloomStrength=0.5")
    new = tc.set_value(src, "ini", "[Bloom.fx] Enabled", False)
    assert b"Enabled = false\n" in new
    new = tc.set_value(src, "ini", "[Bloom.fx] Name", "new")
    assert b'Name = "new"\n' in new
    new = tc.set_value(src, "ini", "PreprocessorDefinitions", "A=1")
    assert b"PreprocessorDefinitions=A=1\n" in new and new.startswith(b"\xef\xbb\xbf")
    with pytest.raises(tc.EditError):
        tc.set_value(src, "ini", "Techniques", "two\nlines")
    with pytest.raises(tc.EditError):
        tc.set_value(src, "ini", "[General] Scale", 1.5)


def test_ini_duplicates_and_utf16_and_unknown_encoding():
    sc = tc.scan(b"[a]\nk=1\nk=2\n[b]\nk=3\n", "ini")
    assert [i.editable for i in sc.items] == [False, False, True]
    src = "\ufeff[S]\r\nKey=값\r\n".encode("utf-16-le")
    src = b"\xff\xfe" + src[2:]  # BOM + body
    sc = tc.scan(src, "ini")
    assert sc.encoding == "utf-16-le" and sc.get("[S] Key").value == "값"
    new = tc.set_value(src, "ini", "[S] Key", "새값")
    assert new.decode("utf-16") == "[S]\r\nKey=새값\r\n"
    cp949 = "[S]\nName=한글\nNum=3\n".encode("cp949")
    sc = tc.scan(cp949, "ini")
    assert not sc.get("[S] Name").editable and sc.get("[S] Num").editable
    new = tc.set_value(cp949, "ini", "[S] Num", 4)
    assert new == cp949.replace(b"Num=3", b"Num=4")
    with pytest.raises(tc.EditError):
        tc.set_value(cp949, "ini", "[S] Num", 4.5)


@pytest.mark.parametrize("src,key,typed,new", [
    (b"[Hotkeys]\r\nOverlayKey=\r\nMode=auto\r\n", "[Hotkeys] OverlayKey", "112",  # empty
     b"[Hotkeys]\r\nOverlayKey=112\r\nMode=auto\r\n"),
    (b"[Hotkeys]\nMode=auto\n", "[Hotkeys] Mode", "2", b"[Hotkeys]\nMode=2\n"),  # a word
    (b"[Hotkeys]\nMode=auto\n", "[Hotkeys] Mode", "true", b"[Hotkeys]\nMode=true\n"),
    (b"[Hotkeys]\nMode=auto\n", "[Hotkeys] Mode", "1.5", b"[Hotkeys]\nMode=1.5\n"),
    (b"[Display]\nScale=1.0 ; 0.5 - 2.0\nX=1\n", "[Display] Scale", "1.5",  # with an inline comment
     b"[Display]\nScale=1.5\nX=1\n"),
    (b"[A]\nName=Hunter\n", "[A] Name", "FALSE", b"[A]\nName=FALSE\n"),
    (b"[A]\nBig=1e999\n", "[A] Big", "-7", b"[A]\nBig=-7\n"),  # (not a finite number: text)
])
def test_ini_text_value_takes_a_number_or_true_false(src, key, typed, new):
    """INI values have no type: text set to a number or true/false is written as typed (the reader then
    guesses another kind for it), and what 적용 accepts is saved."""
    engine = engines.get("ini")
    v = engine.open(src, "x.ini").get(key)
    assert v.kind == "string" and v.editable
    text = engine.parse(v, typed)
    assert text == typed
    result = engines.apply(engine, src, "x.ini", [Change(key, v.kind, v.text, text, raw=v.raw)])
    assert not result.conflicts and len(result.done) == 1 and result.data == new
    assert tc.set_value(src, "ini", key, typed) == new
    assert tc.scan(result.data, "ini").get(key).raw == typed


def test_ini_text_value_read_back_is_checked():
    src = b"[A]\nName=Hunter\nN=1\n"
    engine = engines.get("ini")
    name = engine.open(src, "x.ini").get("[A] Name")
    for typed in ('"x"', "'7'"):  # the reader would take the quotes off: refused by 적용, not only on 저장
        with pytest.raises(EditError, match="따옴표"):
            engine.parse(name, typed)
        with pytest.raises(EditError):
            tc.set_value(src, "ini", "[A] Name", typed)
    assert engine.parse(name, 'say "hi"') == 'say "hi"'
    for typed in ("a\x0bb", "a\u2028b"):  # line breaks for the reader too (str.splitlines)
        with pytest.raises(EditError, match="줄바꿈"):
            engine.parse(name, typed)


@pytest.mark.parametrize("raw,typed,written", [
    ("0.5", "0.0000123", "0.0000123"),  # more decimals than format(x, "f") keeps
    ("0.5", "1e-7", "0.0000001"),
    ("0.5", "-1.5e-12", "-0.0000000000015"),
    ("0.5", "5e-324", "0." + "0" * 323 + "5"),
    ("0.5", "1e16", "10000000000000000.0"),  # (with no point it would read back as an integer)
    ("1.0", "1.5e22", "15000000000000000000000.0"),
    ("0.5", "0.00001", "0.00001"),
    ("1e3", "1e-7", "1e-07"),  # a value the file writes with an exponent keeps one
])
def test_ini_float_is_written_out_exactly(raw, typed, written):
    """An INI float is written without an exponent (unless the file used one) and exactly, so a small
    number 적용 accepts is saved and reads back as that number."""
    src = f"[Display]\r\nSens={raw}\r\nX=1\r\n".encode()
    engine = engines.get("ini")
    v = engine.open(src, "x.ini").get("[Display] Sens")
    assert v.kind == "float"
    text = engine.parse(v, typed)
    result = engines.apply(engine, src, "x.ini", [Change(v.at, v.kind, v.text, text, raw=v.raw)])
    assert not result.conflicts and len(result.done) == 1
    assert result.data == src.replace(f"Sens={raw}".encode(), f"Sens={written}".encode())
    assert tc.scan(result.data, "ini").get("[Display] Sens").value == float(typed)


def test_ini_integers_of_the_whole_64_bit_range():
    """The reader takes every integer the writers write (_coerce: 64 bits, so 19 digits); bigger stays text."""
    sc = tc.scan(b"[A]\nMax=9223372036854775807\nMin=-9223372036854775808\nOver=9223372036854775808\n"
                 b"Long=12345678901234567890\n", "ini")
    assert [(i.kind, i.value) for i in sc.items] == [
        ("int", tc.INT64_MAX), ("int", tc.INT64_MIN), ("string", "9223372036854775808"),
        ("string", "12345678901234567890")]
    src = b"[A]\nN=5\n"
    engine = engines.get("ini")
    v = engine.open(src, "x.ini").get("[A] N")
    for typed in ("1234567890123456789", "9223372036854775807", "-9223372036854775808"):
        text = engine.parse(v, typed)
        result = engines.apply(engine, src, "x.ini", [Change(v.at, v.kind, v.text, text)])
        assert result.data == b"[A]\nN=" + typed.encode() + b"\n"
        assert tc.scan(result.data, "ini").get("[A] N").value == int(typed)
    with pytest.raises(EditError, match="너무 큰"):
        engine.parse(v, "9223372036854775808")


def test_refw_config_txt_like():
    src = b"FirstTime=false\r\nScriptRunner_LogToDisk=true\r\nMenuKey=46\r\n"
    assert tc.format_of("re2_fw_config.txt") == "ini"
    new = tc.set_value(src, "ini", "ScriptRunner_LogToDisk", False)
    assert new == src.replace(b"LogToDisk=true", b"LogToDisk=false")


# ------------------------------------------------------------------------------ TOML tests
TOML_SAMPLE = """\
# plugin settings
title = "My plugin" # trailing comment
enabled = true
ratio = 0.5
count = 1_000
list = [
  "a=b",
  "c",
]
inline = { a = 1 }
when = 2024-01-01

[ui]
'literal key' = 'C:\\path'
"quoted.key" = "x"

[[presets]]
name = "one"
[[presets]]
name = "two"
"""


def test_toml_items():
    pytest.importorskip("tomllib")
    src = TOML_SAMPLE.encode()
    sc = tc.scan(src, "toml")
    assert not sc.errors, sc.errors
    keys = {i.key: i for i in sc.items}
    assert keys["title"].value == "My plugin" and keys["title"].editable
    assert keys["count"].value == 1000
    assert not keys["list"].editable and not keys["inline"].editable and not keys["when"].editable
    assert "a" not in keys and '"a' not in keys  # lines inside the array are not items
    assert keys["[ui] literal key"].value == "C:\\path"
    assert keys["[presets[1]] name"].value == "two"
    new = tc.set_value(src, "toml", "title", 'He said "hi"')
    assert b'title = "He said \\"hi\\"" # trailing comment' in new
    new = tc.set_value(src, "toml", "[presets[1]] name", "zwei")
    assert b'name = "zwei"' in new and b'name = "one"' in new


@pytest.mark.parametrize("key,typed,written", [
    ("s", "a\x7fb", '"a\\u007fb"'),  # TOML escapes DEL as well as the control characters JSON escapes
    ("s", "a\x01b", '"a\\u0001b"'),
    ("l", "a\x01b", '"a\\u0001b"'),  # a literal string cannot hold one: written as a basic string
    ("l", "a\x7fb", '"a\\u007fb"'),
    ("l", "a\tb", "'a\tb'"),  # (a tab it can)
])
def test_toml_control_characters(key, typed, written):
    tomllib = pytest.importorskip("tomllib")
    src = b"s = \"x\"\nl = 'y'\n"
    engine = engines.get("toml")
    v = engine.open(src, "reframework/x.toml").get(key)
    text = engine.parse(v, typed)
    result = engines.apply(engine, src, "reframework/x.toml", [Change(v.at, v.kind, v.text, text)])
    assert f"{key} = {written}\n".encode() in result.data and len(result.done) == 1
    assert tomllib.loads(result.data.decode())[key] == typed


def test_toml_integers_past_64_bits_are_read_only():
    pytest.importorskip("tomllib")
    src = b"a = 9223372036854775807\nb = 9223372036854775808\nc = 1" + b"0" * 30 + b"\nd = -1_000\n"
    sc = tc.scan(src, "toml")
    assert [(i.key, i.editable, i.why) for i in sc.items] == [
        ("a", True, ""), ("b", False, "너무 큰 정수"), ("c", False, "너무 큰 정수"), ("d", True, "")]
    assert engines.get("toml").open(src, "reframework/x.toml").get("c").text == "1" + "0" * 30


@pytest.mark.parametrize("fmt,src", [
    ("toml", b"x = " + b"1" * 5000 + b"\n"),  # past Python's limit for int(text)
    ("toml", b"x = " + b"[" * 3000 + b"]" * 3000 + b"\n"),  # deeper than tomllib's recursion
    ("json", b"[" + b"1" * 5000 + b"]"),
    ("json", b'{"a": -1' + b"0" * 400 + b"}"),  # (past a double's range: nlohmann::json refuses the file)
])
def test_huge_or_deep_values_refuse_the_file(fmt, src):
    """A file the reader cannot take is refused with EditError, not another exception."""
    if fmt == "toml":
        pytest.importorskip("tomllib")
    sc = tc.scan(src, fmt)
    assert sc.errors and not sc.items
    with pytest.raises(EditError):
        engines.get(fmt).open(src, "reframework/x." + fmt)


def test_toml_read_only_without_tomllib(monkeypatch):
    """Python 3.10 has no tomllib: every TOML value is shown read-only."""
    import builtins

    real_import = builtins.__import__

    def no_tomllib(name, *args, **kwargs):
        if name == "tomllib":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_tomllib)
    sc = tc.scan(TOML_SAMPLE.encode(), "toml")
    assert sc.items and not any(i.editable for i in sc.items)
    assert all("3.11" in i.why or i.why for i in sc.items)


# ------------------------------------------------------------------------------- general
def test_format_of():
    assert tc.format_of("reframework/autorun/a/b.lua") == "lua"
    assert tc.format_of("reframework/data/x/config.json") == "json"
    assert tc.format_of("ReShade.ini") == "ini"
    assert tc.format_of("readme.txt") is None


def test_parse_input():
    sc = tc.scan(SAMPLE.encode(), "lua")
    assert tc.parse_input(sc.get("HEX"), "0x10") == 16
    assert tc.parse_input(sc.get("SPEED"), " 2.5 ") == 2.5
    assert tc.parse_input(sc.get("config.enabled"), "끄기") is False
    with pytest.raises(tc.EditError):
        tc.parse_input(sc.get("SPEED"), "nan")
    with pytest.raises(tc.EditError):
        tc.parse_input(sc.get("COUNT"), "1.5")


MORE_SYNTAX = b"""\
---[[ three dashes: a line comment, not a long one
local a = 1
--[==[ ]] still a comment ]==] local b = 2
::top::
local t = {
    f = function(x) if x then return { inner = 1 } end; return "end" end,
    s = "end", -- the word end in a string
    g = { function() end, 5 },
    h = 7 // 2,
    i = 1 << 3,
}
function t.method(self) end
t.obj = {}
function t.obj:m() repeat local z = 1 until z == 1 end
log = { info = function() end }
local u = { [ "a" .. "b" ] = 1, ["c"] = 3 }
log.info "call with a string"
;(tostring)("paren call")
goto skip
::skip::
local after = "yes"
"""


def test_lua_more_syntax_items():
    sc = tc.scan(MORE_SYNTAX, "lua")
    assert not sc.errors, sc.errors
    assert [i.key for i in sc.items] == ["a", "b", "t.s", "t.g[2]", "u.c", "after"]
    check_baked(MORE_SYNTAX, {"a": ("integer", 1), "b": ("integer", 2), "t.s": ("string", "end"),
                              "t.g[2]": ("integer", 5), "u.c": ("integer", 3), "after": ("string", "yes")})


def test_lua_more_syntax(lua, tmp_path):
    sc = check_lua_items(lua, MORE_SYNTAX)
    assert lua_compiles(lua, MORE_SYNTAX, tmp_path) is None
    for item in sc.items:
        new = tc.set_value(MORE_SYNTAX, "lua", item.key, tc.parse_input(item, "9"))
        assert lua_compiles(lua, new, tmp_path) is None


def test_lua_non_utf8_file_ascii_only():
    src = "local s = '한글'\nlocal n = 'x'\n".encode("cp949")
    sc = tc.scan(src, "lua")
    assert sc.ascii_only and not sc.get("s").editable and sc.get("n").editable
    assert tc.set_value(src, "lua", "n", "y").endswith(b"local n = 'y'\n")
    with pytest.raises(tc.EditError):
        tc.set_value(src, "lua", "n", "값")


def test_lua_notes_utf8_and_decoration():
    src = "-- /////////////\n-- 이동 속도\nlocal SPEED = 1.5 -- 기본 1.5\n".encode()
    assert tc.scan(src, "lua").get("SPEED").note == "이동 속도 기본 1.5"


def test_lua_data_file_through_a_constant():
    src = b"""local M = { Const = { CONFIG_PATH = "skill_tracker.json" } }
local config = { enabled = true }
local function load()
  local loaded = json.load_file(M.Const.CONFIG_PATH)
end
"""
    sc = tc.scan(src, "lua")
    assert sc.data_files == ["skill_tracker.json"]


@pytest.mark.parametrize("fmt,src,key,value,broken", [
    ("lua", b'local a = "x"\nlocal b = 2\n', "a", 'say "hi" b', lambda item, v, s: '"' + v + '"'),
    ("lua", b"local a = 1\nlocal b = 2\n", "a", 5, lambda item, v, s: "5 local c = 3"),
    ("lua", b"local a = 1\nlocal b = 2\n", "a", 5, lambda item, v, s: "5.0"),  # type changed
    ("json", b'{"a": "x", "b": 1}', "a", 'q"', lambda item, v, s: '"' + v + '"'),
    ("ini", b"[s]\na=1\nb=2\n", "[s] a", 3, lambda item, v, s: "3\nb=9"),
    ("ini", b"[s]\na=x\nb=2\n", "[s] a", "5", lambda item, v, s: "6"),  # untyped text: as typed
    ("ini", b"[s]\na=x\nb=2\n", "[s] a", "5", lambda item, v, s: '"5"'),  # and unquoted
])
def test_verification_catches_a_broken_writer(monkeypatch, fmt, src, key, value, broken):
    monkeypatch.setitem(tc._WRITERS, fmt, broken)
    with pytest.raises(tc.EditError):
        tc.set_value(src, fmt, key, value)


def test_edit_error_is_the_packages():
    assert tc.EditError is EditError and issubclass(tc.EditError, ValueError)


# ------------------------------------------------------------------- which files 스크립트 설정 offers
@pytest.mark.parametrize("rel,engine", [
    ("reframework/autorun/NierUI/Config.lua", "lua"),
    ("reframework/autorun/a.lua", "lua"),
    ("REFramework/Autorun/Deep/More/x.LUA", "lua"),
    ("reframework/plugins/x.lua", None),  # Lua only under autorun
    ("reframework/data/MyMod/config.json", "json"),
    ("reframework/plugins/x.ini", "ini"),
    ("reframework/plugins/x.cfg", "ini"),
    ("reframework/x.toml", "toml"),
    ("reframework/autorun/x.json", "json"),
    ("ReShade.ini", "ini"),  # the root: .ini only (analyzer.ROOT_FILE_EXTS)
    ("x.toml", None),
    ("x.cfg", None),
    ("x.lua", None),
    ("x.json", None),
    ("natives/STM/x.ini", None),
    ("natives/STM/x.json", None),
    ("reframework/readme.txt", None),
    ("reframework/fonts/x.ttf", None),
    ("other/x.ini", None),
    ("reframework\\autorun\\b.lua", "lua"),
])
def test_listed(rel, engine):
    assert tc.listed(rel) == engine


def test_listed_size_limit():
    assert tc.listed("reframework/data/x.json", size=tc.MAX_BYTES) == "json"
    assert tc.listed("reframework/data/x.json", size=tc.MAX_BYTES + 1) is None


# --------------------------------------------------------------------------------- the engines
def test_engines_are_registered_by_name():
    for name in ("lua", "json", "ini", "toml"):
        engine = engines.get(name)
        assert isinstance(engine, tc.TextEngine) and engine.name == name and engine.tool == "config"
    with pytest.raises(ValueError):
        tc.TextEngine("rsz")


def test_lua_values_and_canonical_text():
    doc = engines.get("lua").open(mf.NIER_CONFIG_LUA, "reframework/autorun/NierUI/Config.lua")
    rows = {v.at: v for v in doc.values()}
    assert list(rows) == ["Config.UI_Scale", "Config.ShowMinimap", "Config.FontName", "Config.Opacity",
                          "Config.Hotkey"]
    scale = rows["Config.UI_Scale"]
    assert (scale.label, scale.group, scale.kind, scale.text, scale.raw) == ("UI_Scale", "Config", "float", "1.0", "1.0")
    assert scale.note == "size of the whole HUD" and scale.settings and scale.editable and scale.rule_at == ""
    hotkey = rows["Config.Hotkey"]
    assert (hotkey.kind, hotkey.text, hotkey.raw) == ("int", "112", "0x70")  # canonical decimal, raw as written
    assert rows["Config.ShowMinimap"].text == "true" and rows["Config.FontName"].text == "NotoSansKR"
    assert doc.get("Config.Opacity").text == "0.85" and doc.get("Config.Nope") is None
    assert doc.expand("Config.UI_Scale", "1.0") == []


def test_settings_flag_lua_only():
    lua_doc = engines.get("lua").open(b"local helper_count = 3\nlocal MAX_SIZE = 9\n", "reframework/autorun/x.lua")
    assert {v.at: v.settings for v in lua_doc.values()} == {"helper_count": False, "MAX_SIZE": True}
    json_doc = engines.get("json").open(b'{"helper_count": 3}', "reframework/data/x.json")
    assert json_doc.get("helper_count").settings


def test_notes_for_runtime_settings_and_warnings():
    doc = engines.get("lua").open(mf.LUA_RUNTIME, "reframework/autorun/MyMod/x.lua")
    assert doc.data_files == ["MyMod/config.json"]
    assert tc.data_file_note("MyMod/config.json") in doc.notes
    assert "reframework/data/MyMod/config.json" in tc.data_file_note("MyMod/config.json")
    assert not any(n.startswith("이 스크립트는 설정을 reframework/data 폴더의 파일(") for n in doc.notes)
    assert tc.SETTINGS_NOTE in doc.notes  # values marked ⚠ (warn) exist
    assert doc.get("cfg.speed").warn
    plain = engines.get("lua").open(mf.NIER_CONFIG_LUA, "reframework/autorun/NierUI/Config.lua")
    assert plain.notes == []


@pytest.mark.parametrize("rel,noted", [
    ("reframework/autorun/MyMod/lang/ko.lua", True),
    ("reframework/autorun/MyMod/Languages/ko.lua", True),
    ("reframework/autorun/MyMod/language.lua", False),
])
def test_language_files_get_the_font_note(rel, noted):
    doc = engines.get("lua").open(b"return { title = 'x', count = 1 }\n", rel)
    assert (tc.LANG_NOTE in doc.notes) == noted


def test_duplicate_keys_listed_once_each():
    doc = engines.get("ini").open(b"[a]\nk=1\nk=2\n", "x.ini")
    assert [(v.at, v.editable) for v in doc.values()] == [("[a] k", False), ("[a] k #2", False)]
    assert doc.get("[a] k").text == "1"


def test_unreadable_file_refused():
    with pytest.raises(EditError, match="JSON"):
        engines.get("json").open(b'{"a": 1,}', "reframework/data/x.json")
    with pytest.raises(EditError):
        engines.get("lua").open(b"local x = 'unfinished\n", "reframework/autorun/x.lua")


def test_parse_gives_canonical_text():
    lua_engine = engines.get("lua")
    doc = lua_engine.open(mf.NIER_CONFIG_LUA, "reframework/autorun/NierUI/Config.lua")
    assert lua_engine.parse(doc.get("Config.Hotkey"), "0x71") == "113"
    assert lua_engine.parse(doc.get("Config.UI_Scale"), " 1.25 ") == "1.25"
    assert lua_engine.parse(doc.get("Config.UI_Scale"), "2") == "2.0"
    assert lua_engine.parse(doc.get("Config.ShowMinimap"), "끄기") == "false"
    assert lua_engine.parse(doc.get("Config.FontName"), " 맑은 고딕") == " 맑은 고딕"  # strings as typed
    for at, text in [("Config.Hotkey", "1.5"), ("Config.UI_Scale", "inf"), ("Config.ShowMinimap", "maybe")]:
        with pytest.raises(EditError):
            lua_engine.parse(doc.get(at), text)


def test_parse_refuses_what_the_file_cannot_hold():
    ini = engines.get("ini")
    doc = ini.open(mf.INI_SAMPLE, "x.ini")
    with pytest.raises(EditError, match='"'):
        ini.parse(doc.get("[General] Name"), 'say "hi"')
    with pytest.raises(EditError):
        ini.parse(doc.get("[General] Name"), "two\nlines")
    cp949 = "[S]\nName=abc\n".encode("cp949") + "; 한글\n".encode("cp949")
    doc = ini.open(cp949, "x.ini")
    assert ini.parse(doc.get("[S] Name"), "xyz") == "xyz"
    with pytest.raises(EditError):
        ini.parse(doc.get("[S] Name"), "한글")
    quoted = '[S]\nTitle="Overlay"\n'.encode("cp949") + "; 한글\n".encode("cp949")  # (a quoted value too)
    doc = ini.open(quoted, "x.ini")
    assert ini.parse(doc.get("[S] Title"), "abc") == "abc"
    for text in ("오버레이", "Café"):
        with pytest.raises(EditError, match="영문·숫자만"):
            ini.parse(doc.get("[S] Title"), text)
    lua_engine = engines.get("lua")
    doc = lua_engine.open("local s = '한글'\nlocal n = 'x'\n".encode("cp949"), "reframework/autorun/x.lua")
    with pytest.raises(EditError):
        lua_engine.parse(doc.get("n"), "값")
    with pytest.raises(EditError):
        lua_engine.parse(doc.get("s"), "x")  # read-only


# every editable value of these files × every input: what 적용 (engine.parse) accepts is saved
ACCEPT_FILES = {
    "ini": ("x.ini", b'[A]\r\nEmpty=\r\nWord=auto\r\nNote=1.0 ; 0.5 - 2.0\r\nQuoted="Hunter"\r\nInt=5\r\n'
                     b"Signed=+5\r\nFloat=0.5\r\nExp=1e3\r\nBool=TRUE\r\n"),
    "json": ("reframework/data/x.json", b'{"i": 5, "f": 1.5, "e": 1e-05, "b": true, "s": "x", '
                                        b'"a": [1, 2.5, "a", false]}'),
    "toml": ("reframework/x.toml", b"s = \"x\"\nl = 'y'\ni = 5\nf = 1.0\nx = 1e3\nb = true\n[t]\nn = -1_000 # c\n"),
    "lua": ("reframework/autorun/x.lua", b"local i = 5\nlocal h = 0x1F\nlocal n = -3\nlocal f = 1.5\nlocal e = 1e-5\n"
                                         b"local b = true\nlocal s = \"x\"\nlocal q = 'y'\nlocal L = [[z]]\n"
                                         b"t = { a = 1, 2.5, c = { d = false } }\n"),
}
ACCEPT_INPUTS = [
    "112", "-7", "+5", "007", "0", "-0", "1.5", "1.", ".5", "-.5", "1e3", "1E-3", "1e999", "1e308",
    "1.7976931348623157e308", "5e-324", "1e-7", "0.0000123", "-1.5e-12", "1e16", "1.5e22", "0.1",
    "1234567890123456789", "9223372036854775807", "9223372036854775808", "-9223372036854775808", "0x10",
    "-0x10", "0x7FFFFFFFFFFFFFFF", "nan", "inf", "true", "True", "FALSE", "yes", "끄기", "", " ", "abc", "a b",
    "a=b", "; c", "#x", "[S]", '"q"', "'q'", '"', "'", "é", "한글", "\U0001f600", "a\tb", " 5", "5 ", "a\x0bb",
    "a b", "a\nb", "a\\b", 'a"b', "a'b", "]]", "--[[", "a\x00b", "a\x01b", "a\x1fb", "a\x7fb", "a\xa0",
    "'''", '"""', "a # b",
]


@pytest.mark.parametrize("fmt", sorted(ACCEPT_FILES))
def test_what_parse_accepts_is_saved(fmt):
    """적용 refuses up front what 저장 would refuse: every input engine.parse accepts for a value saves
    (engines.apply) and reads back as that value (an unquoted INI text value: as typed), also for
    json.loads and tomllib."""
    tomllib = pytest.importorskip("tomllib") if fmt == "toml" else None
    rel, data = ACCEPT_FILES[fmt]
    engine = engines.get(fmt)
    failed, saved = [], 0
    for v in engine.open(data, rel).values():
        if not v.editable:
            continue
        for typed in ACCEPT_INPUTS:
            try:
                text = engine.parse(v, typed)
            except EditError:
                continue
            try:
                result = engines.apply(engine, data, rel, [Change(v.at, v.kind, v.text, text, raw=v.raw)])
            except EditError as exc:
                failed.append(f"{v.at} ← {typed!r}: 적용 {text!r}, 저장: {exc}")
                continue
            back = engine.open(result.data, rel).get(v.at)
            if fmt == "ini" and v.kind == "string" and v.spec[1] == "plain":
                ok = back.raw == text
            else:
                ok = back.kind == v.kind and back.editable and engine.same(back, back.text, text)
                want = tc.typed(v.kind, text)
                if fmt == "json":
                    node = json.loads(result.data)
                    for part in v.at.replace("]", "").replace("[", ".").split("."):
                        node = node[int(part)] if part.isdigit() else node[part]
                    ok = ok and type(node) is type(want) and node == want
                elif fmt == "toml":
                    node = tomllib.loads(result.data.decode())
                    for part in v.at.replace("[", "").replace("] ", ".").split("."):
                        node = node[part]
                    ok = ok and type(node) is type(want) and node == want
            if not ok:
                failed.append(f"{v.at} ← {typed!r}: 적용 {text!r}, reads back {back.raw!r}")
            saved += 1
    assert not failed, "\n".join(failed)
    assert saved > 100


def test_same_compares_values():
    lua_engine = engines.get("lua")
    doc = lua_engine.open(mf.NIER_CONFIG_LUA, "reframework/autorun/NierUI/Config.lua")
    scale, hotkey = doc.get("Config.UI_Scale"), doc.get("Config.Hotkey")
    assert lua_engine.same(scale, "1.0", "1") and lua_engine.same(scale, "0.5", "5e-1")
    assert not lua_engine.same(scale, "1.0", "1.5")
    assert lua_engine.same(hotkey, "112", "0x70") and not lua_engine.same(hotkey, "112", "113")
    assert lua_engine.same(doc.get("Config.ShowMinimap"), "true", "TRUE")
    assert not lua_engine.same(doc.get("Config.FontName"), "a", "a ")
    assert not lua_engine.same(scale, "1.0", "x")


@pytest.mark.parametrize("name,rel,data,changes,expect", [
    ("lua", "reframework/autorun/NierUI/Config.lua", mf.NIER_CONFIG_LUA,
     [Change("Config.UI_Scale", "float", "1.0", "1.25", raw="1.0"), Change("Config.Hotkey", "int", "112", "113")],
     [b"Config.UI_Scale = 1.25 -- size of the whole HUD", b"Config.Hotkey = 0x71"]),
    ("json", "reframework/data/x.json", mf.JSON_SAMPLE,
     [Change("scale", "float", "1.0", "2.5"), Change("nested.deep.v", "float", "-1e-05", "0.5")],
     [b'"scale": 2.5,', b'"v": 0.5}']),
    ("ini", "x.ini", mf.INI_SAMPLE,
     [Change("[General] Opacity", "float", "0.5", "0.75"), Change("[General] Name", "string", "quoted", "new")],
     [b"Opacity=0.75\n", b'Name = "new"\n']),
], ids=["lua", "json", "ini"])
def test_apply_rewrites_only_the_values(name, rel, data, changes, expect):
    engine = engines.get(name)
    result = engines.apply(engine, data, rel, changes)
    assert not result.conflicts and len(result.done) == len(changes)
    for piece in expect:
        assert piece in result.data
    before, after = engine.open(data, rel), engine.open(result.data, rel)
    changed = {c.at for c in changes}
    assert [v.at for v in before.values()] == [v.at for v in after.values()]
    for v in before.values():
        if v.at not in changed:
            assert after.get(v.at).raw == v.raw


def test_apply_toml():
    pytest.importorskip("tomllib")
    engine = engines.get("toml")
    result = engines.apply(engine, mf.TOML_SAMPLE, "reframework/x.toml",
                           [Change("count", "int", "1000", "5"), Change("[ui] font", "string", "NotoSans", "Malgun")])
    assert b"count = 5\n" in result.data and b"font = 'Malgun'\n" in result.data and not result.conflicts


def test_apply_after_the_author_changed_a_value():
    engine = engines.get("lua")
    updated = mf.NIER_CONFIG_LUA.replace(b"UI_Scale = 1.0", b"UI_Scale = 1.1")
    result = engines.apply(engine, updated, "reframework/autorun/NierUI/Config.lua",
                           [Change("Config.UI_Scale", "float", "1.0", "1.25", raw="1.0")])
    assert result.data == updated and [(c.why, c.now) for c in result.conflicts] == [("changed", "1.1")]
    result = engines.apply(engine, mf.NIER_CONFIG_LUA, "reframework/autorun/NierUI/Config.lua",
                           [Change("Config.Gone", "float", "1.0", "1.25")])
    assert [c.why for c in result.conflicts] == ["gone"]
