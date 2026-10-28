"""스크립트 설정: safe editors for the text settings files a mod ships (engines "lua" "json" "ini" "toml").

Formats
* ``lua``  - REFramework scripts: top-level ``local NAME = literal``, ``NAME.field = literal``,
             ``return { ... }`` and table constructors (nested) with ``key = literal`` / positional literals.
* ``json`` - every scalar (number/bool/string) of a JSON file (strict: what nlohmann::json, which
             REFramework uses, reads).
* ``ini``  - ``key=value`` lines (sections, comments and line endings kept): ReShade presets,
             REFramework's re2_fw_config.txt, plugin .ini/.cfg files.  Values have no type: an unquoted
             one is shown as the kind it looks like, and text written over text may read back as a number.
* ``toml`` - simple ``key = scalar`` lines of a TOML file (validated with tomllib when present: on
             Python 3.10 every TOML value is read-only; the exe is built with 3.12).

Every edit replaces the bytes of ONE value and nothing else, then reads the new file again and
checks that only that value changed (else EditError and nothing is written; for Lua also the token
stream outside the replaced spans, a check that does not depend on the reader).  Anything the
reader is not sure about is shown read-only (``Item.editable`` False with ``Item.why``) or, for a
file it cannot read at all, ``Scan.errors`` is non-empty and there are no items.  The Lua reader
agreed with Lua 5.4 on 745 real mod scripts (research with lupa, which is not a dependency).

Only the files a mod ships are edited, through the 모드 수정 overlay (``listed`` says which); nothing
in the game folder is written here.

API
    scan(data: bytes, fmt: str) -> Scan
    set_value(data: bytes, fmt: str, key: str, value) -> bytes          (raises EditError)
    set_values(data: bytes, fmt: str, changes: dict[str, object]) -> bytes
    parse_input(item: Item, text: str) -> value                         (raises EditError)
    settings_like(item: Item) -> bool
    format_of(game_path: str) -> str | None
    listed(rel: str, size: int | None = None) -> str | None             (the engine for a mod file)
    ConfigDoc, TextEngine                                               (engines.Doc / engines.Engine)
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from decimal import Decimal

from . import EditError
from .engines import Value

MAX_BYTES = 4 * 1024 * 1024
INT64_MIN, INT64_MAX = -(1 << 63), (1 << 63) - 1


@dataclass(frozen=True)
class Item:
    key: str  # stable path: "Config.UI_Scale", "M.LIST[2]", "colors.main", "[General] Scale"
    kind: str  # "int" | "float" | "bool" | "string" | "nil"
    value: object  # int | float | bool | str | None
    raw: str  # the literal as written in the file
    start: int  # offsets into Scan.text (Lua and INI in latin-1/decoded text, JSON in decoded text)
    end: int
    line: int  # 1-based
    editable: bool = True
    why: str = ""  # why it is read-only
    note: str = ""  # comment next to it or right above it
    warn: str = ""  # editable, but the user should know (e.g. the script may replace it from a saved file)
    style: str = ""  # numbers: "dec" | "hex" | "exp"; strings: '"', "'", "[[", "[=[" ...; ini: "quoted"/"plain"
    group: str = ""  # key of the table/section it is in


@dataclass
class Scan:
    format: str
    items: list = field(default_factory=list)
    errors: list = field(default_factory=list)  # the file is refused: no items
    notes: list = field(default_factory=list)  # things the user should know (Korean)
    data_files: list = field(default_factory=list)  # lua: reframework/data paths the script loads/saves
    text: str = ""
    encoding: str = "utf-8"
    bom: bytes = b""
    ascii_only: bool = False  # the file is not UTF-8: new text values must be ASCII

    def get(self, key: str) -> Item | None:
        return next((i for i in self.items if i.key == key), None)


FORMATS = {".lua": "lua", ".json": "json", ".ini": "ini", ".cfg": "ini", ".toml": "toml"}
INI_TXT_NAMES = {"re2_fw_config.txt"}


def format_of(game_path: str) -> str | None:
    low = game_path.replace("\\", "/").lower()
    name = low.rsplit("/", 1)[-1]
    if name in INI_TXT_NAMES:
        return "ini"
    dot = name.rfind(".")
    return FORMATS.get(name[dot:]) if dot >= 0 else None


def listed(rel: str, size: int | None = None) -> str | None:
    """The engine ("lua" | "json" | "ini" | "toml") for a mod file 스크립트 설정 offers, by its game
    path: .lua under reframework/autorun/, .json/.ini/.toml/.cfg under reframework/ (any depth), and
    .ini at the game root only (the only root settings file a mod installs: analyzer.ROOT_FILE_EXTS).
    Never under natives/, and never a file over MAX_BYTES (when ``size`` is given)."""
    low = rel.replace("\\", "/").lstrip("/").lower()
    if size is not None and size > MAX_BYTES:
        return None
    parts = low.split("/")
    name = parts[-1]
    dot = name.rfind(".")
    ext = name[dot:] if dot > 0 else ""
    if parts[0] == "natives" or not ext:
        return None
    if len(parts) == 1:
        return "ini" if ext == ".ini" else None
    if parts[0] != "reframework":
        return None
    if ext == ".lua":
        return "lua" if len(parts) > 2 and parts[1] == "autorun" else None
    return FORMATS.get(ext) if ext in (".json", ".ini", ".toml", ".cfg") else None


def scan(data: bytes, fmt: str) -> Scan:
    if len(data) > MAX_BYTES:
        return Scan(fmt, errors=["파일이 너무 커서 열지 않습니다."])
    if fmt == "lua":
        return _lua_scan(data)
    if fmt == "json":
        return _json_scan(data)
    if fmt in ("ini", "toml"):
        return _ini_scan(data, toml=fmt == "toml")
    raise ValueError(fmt)


def set_value(data: bytes, fmt: str, key: str, value) -> bytes:
    return set_values(data, fmt, {key: value})


def set_values(data: bytes, fmt: str, changes: dict) -> bytes:
    """Write several values at once (each one replaced in place); checks the result."""
    before = scan(data, fmt)
    if before.errors:
        raise EditError(before.errors[0])
    edits = []
    expected = {}
    for key, value in changes.items():
        item = before.get(key)
        if item is None:
            raise EditError(f"'{key}' 값을 찾지 못했습니다.")
        if not item.editable:
            raise EditError(f"'{key}' 값은 바꿀 수 없습니다: {item.why}")
        value = _coerce(item, value)
        literal = _WRITERS[fmt](item, value, before)
        edits.append((item.start, item.end, literal))
        expected[key] = value
    text = before.text
    for start, end, literal in sorted(edits, reverse=True):
        text = text[:start] + literal + text[end:]
    new = _encode(before, text)
    after = scan(new, fmt)
    _verify(before, after, expected)
    if fmt == "lua":
        _verify_lua_tokens(before.text, after.text, sorted(edits))
    return new


def _verify_lua_tokens(old: str, new: str, edits: list) -> None:
    """Every token outside the replaced spans is the same before and after (a check that does not depend
    on the reader that found the values)."""
    old_spans, new_spans, shift = [], [], 0
    for start, end, literal in edits:
        old_spans.append((start, end))
        new_spans.append((start + shift, start + shift + len(literal)))
        shift += len(literal) - (end - start)

    def outside(toks, spans):
        return [(t.kind, t.text) for t in toks if not any(a <= t.start < b for a, b in spans)]

    try:
        a, b = lua_tokens(old)[0], lua_tokens(new)[0]
    except LuaSyntax as exc:
        raise EditError(f"바꾼 파일을 다시 읽지 못해 쓰지 않았습니다: {exc}") from None
    if outside(a, old_spans) != outside(b, new_spans):
        raise EditError("바꾼 값 바깥의 내용이 달라져 쓰지 않았습니다.")
    for (x, y), (p, q) in zip(old_spans, new_spans):  # each span is still one literal (or '-' and a number)
        if len([t for t in b if p <= t.start < q]) not in (1, 2):
            raise EditError("바꾼 값이 하나의 값으로 읽히지 않아 쓰지 않았습니다.")


def parse_input(item: Item, text: str):
    """The value the user typed for ``item`` (keeps its type)."""
    t = text.strip()
    if item.kind == "bool":
        low = t.lower()
        if low in ("true", "1", "on", "yes", "켜기", "예"):
            return True
        if low in ("false", "0", "off", "no", "끄기", "아니요"):
            return False
        raise EditError("참(true)이나 거짓(false)이어야 합니다.")
    if item.kind == "int":
        try:
            return int(t, 0) if t.lower().startswith(("0x", "-0x")) else int(t)
        except ValueError:
            raise EditError("정수를 적어 주세요.") from None
    if item.kind == "float":
        try:
            v = float(t)
        except ValueError:
            raise EditError("숫자를 적어 주세요.") from None
        if not math.isfinite(v):
            raise EditError("무한대나 NaN은 쓸 수 없습니다.")
        return v
    if item.kind == "string":
        return text
    raise EditError("이 값은 바꿀 수 없습니다.")


# ----------------------------------------------------------------------------------- shared
def _coerce(item: Item, value):
    k = item.kind
    if k == "bool":
        if not isinstance(value, bool):
            raise EditError("참/거짓 값이어야 합니다.")
    elif k == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            else:
                raise EditError("정수여야 합니다 (소수점 없이).")
        if not INT64_MIN <= value <= INT64_MAX:
            raise EditError("너무 큰 정수입니다.")
    elif k == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EditError("숫자여야 합니다.")
        value = float(value)
        if not math.isfinite(value):
            raise EditError("무한대나 NaN은 쓸 수 없습니다.")
    elif k == "string":
        if not isinstance(value, str):
            raise EditError("글자여야 합니다.")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise EditError("쓸 수 없는 글자가 들어 있습니다.") from None
    else:
        raise EditError("이 값은 바꿀 수 없습니다.")
    return value


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return a == b or (math.isnan(a) and math.isnan(b))
    return type(a) is type(b) and a == b


def _untyped(item: Item, fmt: str) -> bool:
    """An unquoted INI text value: the file gives it no type (the reader only guesses one), so text
    written there may read back as a number or true/false."""
    return fmt == "ini" and item.kind == "string" and item.style == "plain"


def _verify(before: Scan, after: Scan, expected: dict) -> None:
    if after.errors:
        raise EditError("바꾼 파일을 다시 읽지 못해 쓰지 않았습니다: " + after.errors[0])
    if [i.key for i in before.items] != [i.key for i in after.items]:
        raise EditError("바꾼 뒤 파일 구조가 달라져 쓰지 않았습니다.")
    for old, new in zip(before.items, after.items):
        if old.key in expected and _untyped(old, before.format):
            # the text as written, unquoted, whatever kind the reader guesses for it now ('' → 112)
            if new.style != "plain" or new.raw != expected[old.key]:
                raise EditError(f"'{old.key}'에 쓴 값이 다르게 읽혀 쓰지 않았습니다.")
            continue
        if old.kind != new.kind:
            raise EditError(f"'{old.key}'의 종류가 달라져 쓰지 않았습니다.")
        if old.key in expected:
            if not _same(new.value, expected[old.key]):
                raise EditError(f"'{old.key}'에 쓴 값이 다르게 읽혀 쓰지 않았습니다.")
        elif new.raw != old.raw or not _same(new.value, old.value) or new.editable != old.editable:
            raise EditError(f"다른 값('{old.key}')이 달라져 쓰지 않았습니다.")


def _encode(scan_: Scan, text: str) -> bytes:
    try:
        return scan_.bom + text.encode(scan_.encoding)
    except UnicodeEncodeError:
        raise EditError("이 파일의 글자 인코딩으로 쓸 수 없는 글자입니다 (영문·숫자만 쓸 수 있습니다).") from None


def _decode_text(data: bytes) -> tuple[str, str, bytes] | None:
    """(text, encoding, bom) of a UTF-8/UTF-16 text file; None when it is neither."""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            try:
                return data[len(bom):].decode(enc), enc, bom
            except UnicodeDecodeError:
                return None
    try:
        return data.decode("utf-8"), "utf-8", b""
    except UnicodeDecodeError:
        return None


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for m in re.finditer(r"\r\n|\n\r|\n|\r", text):
        starts.append(m.end())
    return starts


def _line_of(starts: list[int], pos: int) -> int:
    import bisect
    return bisect.bisect_right(starts, pos)


# ==================================================================================== LUA
KEYWORDS = {"and", "break", "do", "else", "elseif", "end", "false", "for", "function", "goto", "if", "in",
            "local", "nil", "not", "or", "repeat", "return", "then", "true", "until", "while"}
BINOPS = {"+", "-", "*", "/", "//", "%", "^", "..", "==", "~=", "<", "<=", ">", ">=", "&", "|", "~", "<<", ">>",
          "and", "or"}
UNOPS = {"-", "not", "#", "~"}
OPENERS = {"function", "do", "if", "repeat"}
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SPACE = re.compile(r"[ \t\v\f]+")
_NUM_HEX = re.compile(r"0[xX](?:[pP][+-]?|[0-9a-fA-F.])*")
_NUM_DEC = re.compile(r"(?:[0-9]|\.[0-9])(?:[eE][+-]?|[0-9a-fA-F.])*")
_DEC_INT = re.compile(r"[0-9]+\Z")
_DEC_FLT = re.compile(r"(?:(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[0-9]+[eE][+-]?[0-9]+)\Z")
_HEX_INT = re.compile(r"0[xX][0-9a-fA-F]+\Z")
_HEX_FLT = re.compile(r"0[xX](?:(?:[0-9a-fA-F]+\.[0-9a-fA-F]*|\.[0-9a-fA-F]+)(?:[pP][+-]?[0-9]+)?|"
                      r"[0-9a-fA-F]+[pP][+-]?[0-9]+)\Z")
_LONG_OPEN = re.compile(r"\[(=*)\[")
_OPS3 = ("...",)
_OPS2 = ("==", "~=", "<=", ">=", "//", "::", "<<", ">>", "..")
_OPS1 = set("+-*/%^#&~|<>=(){}[];:,.")


class LuaSyntax(Exception):
    pass


@dataclass
class Tok:
    kind: str  # name kw num str op eof
    text: str  # name/kw/op text; for num/str the raw source
    start: int
    end: int
    line: int
    value: object = None  # num: (kind, value); str: bytes
    style: str = ""


def _newline_len(text: str, i: int) -> int:
    """Length of the newline sequence at i (\n, \r, \r\n, \n\r count as one, as Lua's lexer does)."""
    c = text[i]
    if i + 1 < len(text) and text[i + 1] in "\r\n" and text[i + 1] != c:
        return 2
    return 1


def lua_tokens(text: str) -> tuple[list[Tok], list[tuple[int, int, int, str]]]:
    """Tokens and comments (start, end, line, body) of Lua 5.4 source decoded as latin-1."""
    toks: list[Tok] = []
    comments = []
    i, n, line = 0, len(text), 1
    if text.startswith("#"):  # first line skipped by luaL_loadfile (shebang)
        while i < n and text[i] not in "\r\n":
            i += 1
    while i < n:
        c = text[i]
        if c in "\r\n":
            i += _newline_len(text, i)
            line += 1
            continue
        m = _SPACE.match(text, i)
        if m:
            i = m.end()
            continue
        start, sline = i, line
        if c == "-" and text.startswith("--", i):
            m = _LONG_OPEN.match(text, i + 2)
            if m:
                close = "]" + m.group(1) + "]"
                j = text.find(close, m.end())
                if j < 0:
                    raise LuaSyntax(f"{line}번째 줄: 닫히지 않은 주석")
                body = text[m.end():j]
                line += len(re.findall(r"\r\n|\n\r|\n|\r", body))
                i = j + len(close)
            else:
                j = i + 2
                while j < n and text[j] not in "\r\n":
                    j += 1
                body = text[i + 2:j]
                i = j
            comments.append((start, i, sline, body))
            continue
        if c == "[":
            m = _LONG_OPEN.match(text, i)
            if m:
                value, i, line = _long_string(text, m, line)
                toks.append(Tok("str", text[start:i], start, i, sline, value, "[" + m.group(1) + "["))
                continue
            if text.startswith("[=", i):
                raise LuaSyntax(f"{line}번째 줄: 잘못된 긴 글자 구분자")
        if c in "\"'":
            value, i, line = _short_string(text, i, line)
            toks.append(Tok("str", text[start:i], start, i, sline, value, c))
            continue
        if c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            m = (_NUM_HEX if text.startswith(("0x", "0X"), i) else _NUM_DEC).match(text, i)
            j = m.end()
            if j < n and (text[j].isalpha() or text[j] == "_") and text[j].isascii():
                raise LuaSyntax(f"{line}번째 줄: 잘못된 숫자 {text[i:j + 1]!r}")
            raw = text[i:j]
            toks.append(Tok("num", raw, start, j, sline, _lua_number(raw, line)))
            i = j
            continue
        m = _NAME.match(text, i)
        if m:
            word = m.group()
            toks.append(Tok("kw" if word in KEYWORDS else "name", word, start, m.end(), sline))
            i = m.end()
            continue
        for op in _OPS3 + _OPS2:
            if text.startswith(op, i):
                break
        else:
            op = c if c in _OPS1 else None
        if op is None:
            raise LuaSyntax(f"{line}번째 줄: 알 수 없는 글자 {c!r}")
        toks.append(Tok("op", op, start, i + len(op), sline))
        i += len(op)
    toks.append(Tok("eof", "", n, n, line))
    return toks, comments


def _long_string(text: str, m: re.Match, line: int) -> tuple[bytes, int, int]:
    close = "]" + m.group(1) + "]"
    j = text.find(close, m.end())
    if j < 0:
        raise LuaSyntax(f"{line}번째 줄: 닫히지 않은 긴 글자")
    body = text[m.end():j]
    nl = re.compile(r"\r\n|\n\r|\n|\r")
    line += len(nl.findall(body))
    first = nl.match(body)
    if first:
        body = body[first.end():]
    return nl.sub("\n", body).encode("latin-1"), j + len(close), line


_ESC = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34, "'": 39}


def _short_string(text: str, i: int, line: int) -> tuple[bytes, int, int]:
    quote = text[i]
    i += 1
    n = len(text)
    out = bytearray()
    while True:
        if i >= n or text[i] in "\r\n":
            raise LuaSyntax(f"{line}번째 줄: 닫히지 않은 글자")
        c = text[i]
        if c == quote:
            return bytes(out), i + 1, line
        if c != "\\":
            out.append(ord(c))
            i += 1
            continue
        i += 1
        if i >= n:
            raise LuaSyntax(f"{line}번째 줄: 닫히지 않은 글자")
        e = text[i]
        if e in _ESC:
            out.append(_ESC[e])
            i += 1
        elif e in "\r\n":
            out.append(10)
            i += _newline_len(text, i)
            line += 1
        elif e == "x":
            h = text[i + 1:i + 3]
            if not re.fullmatch(r"[0-9a-fA-F]{2}", h):
                raise LuaSyntax(f"{line}번째 줄: 잘못된 \\x 이스케이프")
            out.append(int(h, 16))
            i += 3
        elif e == "z":
            i += 1
            while i < n and text[i] in " \t\v\f\r\n":
                if text[i] in "\r\n":
                    i += _newline_len(text, i)
                    line += 1
                else:
                    i += 1
        elif e.isdigit():
            m = re.compile(r"[0-9]{1,3}").match(text, i)
            v = int(m.group())
            if v > 255:
                raise LuaSyntax(f"{line}번째 줄: 잘못된 \\ddd 이스케이프")
            out.append(v)
            i = m.end()
        elif e == "u":
            m = re.compile(r"\{([0-9a-fA-F]+)\}").match(text, i + 1)
            if not m or int(m.group(1), 16) > 0x7FFFFFFF:
                raise LuaSyntax(f"{line}번째 줄: 잘못된 \\u 이스케이프")
            out += _utf8_extended(int(m.group(1), 16))
            i = m.end()
        else:
            raise LuaSyntax(f"{line}번째 줄: 잘못된 이스케이프 \\{e}")


def _utf8_extended(cp: int) -> bytes:
    if cp < 0x80:
        return bytes([cp])
    # Lua's luaO_utf8esc: up to 6 bytes (values up to 2^31)
    out = []
    mfb = 0x3F
    while True:
        out.append(0x80 | (cp & 0x3F))
        cp >>= 6
        mfb >>= 1
        if cp <= mfb:
            break
    out.append(((~mfb << 1) & 0xFF) | cp)
    return bytes(reversed(out))


def _lua_number(raw: str, line: int):
    if _DEC_INT.match(raw):
        digits = raw.lstrip("0") or "0"
        v = int(digits) if len(digits) <= 19 else INT64_MAX + 1
        return ("int", v) if v <= INT64_MAX else ("bigint", float(digits))  # Lua reads it as a float (or inf)
    if _HEX_INT.match(raw):
        v = int(raw, 16) & ((1 << 64) - 1)  # hex integers wrap around
        return ("int", v - (1 << 64) if v > INT64_MAX else v)
    if _DEC_FLT.match(raw):
        return ("float", float(raw))
    if _HEX_FLT.match(raw):
        return ("float", float.fromhex(raw))
    raise LuaSyntax(f"{line}번째 줄: 잘못된 숫자 {raw!r}")


@dataclass
class _Lit:
    """A literal value found at a path (pending until its statement is complete)."""
    path: tuple
    kind: str
    value: object
    raw: str
    start: int
    end: int
    line: int
    style: str
    anchor: int  # line of the statement/field start (for the comment above)
    why: str = ""
    stmt: int = 0  # top-level statement number


class _LuaReader:
    def __init__(self, toks: list[Tok], text: str):
        self.t = toks
        self.text = text
        self.i = 0
        self.lits: list[_Lit] = []
        self.assigned: list[tuple[tuple, int, bool]] = []  # (path, statement, clean value) of top-level targets
        self.complex_fields: list[tuple[tuple, int]] = []  # table fields with a non-literal value
        self.tables: list[tuple[tuple, int]] = []  # (path, statement) of each table constructor value
        self.stmt = 0

    # -- helpers
    def peek(self, k: int = 0) -> Tok:
        return self.t[min(self.i + k, len(self.t) - 1)]

    def next(self) -> Tok:
        tok = self.t[self.i]
        if tok.kind != "eof":
            self.i += 1
        return tok

    def is_op(self, text: str, k: int = 0) -> bool:
        tok = self.peek(k)
        return tok.kind == "op" and tok.text == text

    def is_kw(self, text: str, k: int = 0) -> bool:
        tok = self.peek(k)
        return tok.kind == "kw" and tok.text == text

    def expect_op(self, text: str) -> Tok:
        if not self.is_op(text):
            raise LuaSyntax(f"{self.peek().line}번째 줄: '{text}'가 있어야 합니다")
        return self.next()

    def expect_kw(self, text: str) -> Tok:
        if not self.is_kw(text):
            raise LuaSyntax(f"{self.peek().line}번째 줄: '{text}'가 있어야 합니다")
        return self.next()

    def expect_name(self) -> Tok:
        if self.peek().kind != "name":
            raise LuaSyntax(f"{self.peek().line}번째 줄: 이름이 있어야 합니다")
        return self.next()

    def binop_next(self) -> bool:
        tok = self.peek()
        return (tok.kind == "op" or tok.kind == "kw") and tok.text in BINOPS

    # -- chunk
    def chunk(self) -> None:
        while self.peek().kind != "eof":
            self.stmt += 1
            self.statement()

    def statement(self) -> None:
        tok = self.peek()
        if tok.kind == "op" and tok.text == ";":
            self.next()
        elif tok.kind == "op" and tok.text == "::":
            self.next()
            self.expect_name()
            self.expect_op("::")
        elif tok.kind == "kw":
            w = tok.text
            if w == "break":
                self.next()
            elif w == "goto":
                self.next()
                self.expect_name()
            elif w in ("do", "while", "for", "if", "repeat", "function"):
                self.compound()
            elif w == "local":
                self.local_stat()
            elif w == "return":
                self.next()
                if self.peek().kind != "eof" and not self.is_op(";"):
                    self.explist([("return",)], tok.line)
                if self.is_op(";"):
                    self.next()
                if self.peek().kind != "eof":
                    raise LuaSyntax(f"{self.peek().line}번째 줄: return 뒤에 코드가 있습니다")
            else:
                raise LuaSyntax(f"{tok.line}번째 줄: 예상하지 못한 '{w}'")
        else:
            self.expr_stat()

    def local_stat(self) -> None:
        first = self.next()  # local
        if self.is_kw("function"):
            self.next()
            self.expect_name()
            self.funcbody()
            return
        names = []
        while True:
            name = self.expect_name().text
            if self.is_op("<"):
                self.next()
                attrib = self.expect_name().text
                self.expect_op(">")
                if attrib not in ("const", "close"):
                    raise LuaSyntax(f"{first.line}번째 줄: 알 수 없는 속성 <{attrib}>")
                if attrib == "close":
                    name = None
            names.append((name,) if name else None)
            if not self.is_op(","):
                break
            self.next()
        clean = []
        if self.is_op("="):
            self.next()
            clean = self.explist(names, first.line)
        for j, p in enumerate(names):
            if p:
                self.assigned.append((p, self.stmt, clean[j] if j < len(clean) else True))

    def expr_stat(self) -> None:
        first = self.peek()
        path = self.suffixed()
        if self.is_op("=") or self.is_op(","):
            targets = [path]
            while self.is_op(","):
                self.next()
                targets.append(self.suffixed())
            self.expect_op("=")
            clean = self.explist(targets, first.line)
            for j, p in enumerate(targets):
                if p:
                    self.assigned.append((p, self.stmt, clean[j] if j < len(clean) else True))
        # else: a call statement

    def explist(self, targets: list, anchor: int) -> list[bool]:
        j = 0
        clean = []
        while True:
            path = targets[j] if j < len(targets) else None
            clean.append(self.exp(path, anchor))
            j += 1
            if not self.is_op(","):
                break
            self.next()
        return clean

    # -- expressions
    def exp(self, path, anchor: int) -> bool:
        """Read one expression; literals in it are recorded at ``path`` when the whole expression is a
        literal (or a table constructor). Returns True for such a 'clean' expression."""
        mark, tables = len(self.lits), len(self.tables)
        neg_tok = None
        unops = 0
        while (self.peek().kind in ("op", "kw")) and self.peek().text in UNOPS:
            tok = self.next()
            unops += 1
            neg_tok = tok if tok.text == "-" else None
        clean = self.simple(path if unops <= 1 else None, anchor)
        if unops:
            if clean and unops == 1 and neg_tok is not None and len(self.lits) == mark + 1 and \
                    self.lits[-1].kind in ("int", "float", "bigint") and self.lits[-1].path == path and \
                    self.lits[-1].start == self.t[self.i - 1].start:
                lit = self.lits[-1]
                lit.start = neg_tok.start
                lit.raw = self.text[lit.start:lit.end]
                lit.value = -lit.value
                if lit.kind == "int" and lit.value > INT64_MAX:  # -(-2^63) wraps around in Lua
                    lit.why = "정수 범위 끝의 값"
            else:
                clean = False
        if self.binop_next():
            clean = False
            while self.binop_next():
                self.next()
                while (self.peek().kind in ("op", "kw")) and self.peek().text in UNOPS:
                    self.next()
                self.simple(None, anchor)
        if not clean:
            del self.lits[mark:]
            del self.tables[tables:]
            if path is not None and path != ("return",):
                self.complex_fields.append((path, self.stmt))
        return clean

    def simple(self, path, anchor: int) -> bool:
        tok = self.peek()
        if tok.kind == "num":
            self.next()
            kind, value = tok.value
            if path is not None:
                style = "hex" if tok.text[:2].lower() == "0x" else "exp" if "e" in tok.text.lower() else "dec"
                why = "Lua가 소수로 읽는 큰 정수" if kind == "bigint" else ""
                self.lits.append(_Lit(path, kind, value, tok.text, tok.start, tok.end, tok.line, style, anchor, why,
                                      self.stmt))
            return True
        if tok.kind == "str":
            self.next()
            if path is not None:
                try:
                    value, why = tok.value.decode("utf-8"), ""
                except UnicodeDecodeError:
                    value, why = tok.value.decode("utf-8", "replace"), "UTF-8이 아닌 글자"
                self.lits.append(_Lit(path, "string", value, tok.text, tok.start, tok.end, tok.line, tok.style,
                                      anchor, why, self.stmt))
            return True
        if tok.kind == "kw" and tok.text in ("true", "false", "nil"):
            self.next()
            if path is not None:
                kind = "nil" if tok.text == "nil" else "bool"
                value = None if kind == "nil" else tok.text == "true"
                self.lits.append(_Lit(path, kind, value, tok.text, tok.start, tok.end, tok.line, "", anchor,
                                      "값이 없음(nil): 종류를 알 수 없음" if kind == "nil" else "", self.stmt))
            return True
        if tok.kind == "op" and tok.text == "{":
            self.table(path, anchor)
            return True
        if tok.kind == "kw" and tok.text == "function":
            self.next()
            self.funcbody()
            return False
        if tok.kind == "op" and tok.text == "...":
            self.next()
            return False
        self.suffixed()
        return False

    def suffixed(self):
        """Skip a suffixed expression; returns its path when it is NAME{.NAME|[literal]}, else None."""
        tok = self.peek()
        path = None
        if tok.kind == "name":
            self.next()
            path = (tok.text,)
        elif tok.kind == "op" and tok.text == "(":
            self.next()
            self.exp(None, tok.line)
            self.expect_op(")")
        else:
            raise LuaSyntax(f"{tok.line}번째 줄: 예상하지 못한 '{tok.text or '파일 끝'}'")
        while True:
            tok = self.peek()
            if tok.kind == "op" and tok.text == ".":
                self.next()
                name = self.expect_name().text
                path = path + (_key_seg(name),) if path else None
            elif tok.kind == "op" and tok.text == "[":
                self.next()
                k = self.peek()
                key = None
                if k.kind in ("str", "num") and (self.peek(1).kind == "op" and self.peek(1).text == "]"):
                    key = _literal_key(k)
                self.exp(None, tok.line)
                self.expect_op("]")
                path = path + (key,) if (path and key is not None) else None
            elif tok.kind == "op" and tok.text == ":":
                self.next()
                self.expect_name()
                self.args()
                path = None
            elif (tok.kind == "op" and tok.text in ("(", "{")) or tok.kind == "str":
                self.args()
                path = None
            else:
                return path

    def args(self) -> None:
        tok = self.peek()
        if tok.kind == "str":
            self.next()
        elif tok.kind == "op" and tok.text == "{":
            self.table(None, tok.line)
        elif tok.kind == "op" and tok.text == "(":
            self.next()
            if not self.is_op(")"):
                self.explist([None], tok.line)
            self.expect_op(")")
        else:
            raise LuaSyntax(f"{tok.line}번째 줄: 함수 인자가 있어야 합니다")

    def table(self, path, anchor: int) -> None:
        if path is not None:
            self.tables.append((path, self.stmt))
        self.expect_op("{")
        index = 0
        while not self.is_op("}"):
            first = self.peek()
            if self.is_op("["):
                self.next()
                k = self.peek()
                key = _literal_key(k) if k.kind in ("str", "num") and self.is_op("]", 1) else None
                self.exp(None, first.line)
                self.expect_op("]")
                self.expect_op("=")
                sub = path + (key,) if (path is not None and key is not None) else None
                self.exp(sub, first.line)
            elif first.kind == "name" and self.is_op("=", 1):
                self.next()
                self.next()
                sub = path + (_key_seg(first.text),) if path is not None else None
                self.exp(sub, first.line)
            else:
                index += 1
                sub = path + (("i", index),) if path is not None else None
                self.exp(sub, first.line)
            if self.is_op(",") or self.is_op(";"):
                self.next()
            elif not self.is_op("}"):
                raise LuaSyntax(f"{self.peek().line}번째 줄: 표 안에 ',' 또는 '}}'가 있어야 합니다")
        self.next()

    def funcbody(self) -> None:
        # name parts of 'function a.b:c' are skipped by the caller or here
        while self.is_op(".") or self.is_op(":"):
            self.next()
            self.expect_name()
        self.expect_op("(")
        while not self.is_op(")"):
            self.next()
            if self.peek().kind == "eof":
                raise LuaSyntax("함수 인자 목록이 닫히지 않았습니다")
        self.next()
        self.block_to("end")

    def block_to(self, closer: str) -> None:
        depth = 0
        while True:
            tok = self.next()
            if tok.kind == "eof":
                raise LuaSyntax(f"'{closer}'가 없습니다 (블록이 닫히지 않음)")
            if tok.kind != "kw":
                continue
            if tok.text in OPENERS:
                depth += 1
            elif tok.text in ("end", "until"):
                if depth == 0:
                    if tok.text != closer:
                        raise LuaSyntax(f"{tok.line}번째 줄: '{closer}' 대신 '{tok.text}'")
                    if closer == "until":
                        self.exp(None, tok.line)
                    return
                depth -= 1  # (the condition after a nested 'until' only holds balanced function bodies)

    def compound(self) -> None:
        tok = self.next()
        w = tok.text
        if w == "do":
            self.block_to("end")
        elif w == "while":
            self.exp(None, tok.line)
            self.expect_kw("do")
            self.block_to("end")
        elif w == "for":
            self.expect_name()
            if self.is_op("="):
                self.next()
                self.explist([None], tok.line)
            else:
                while self.is_op(","):
                    self.next()
                    self.expect_name()
                self.expect_kw("in")
                self.explist([None], tok.line)
            self.expect_kw("do")
            self.block_to("end")
        elif w == "if":
            self.exp(None, tok.line)
            self.expect_kw("then")
            self.block_to("end")
        elif w == "repeat":
            self.block_to("until")
        elif w == "function":
            self.expect_name()
            self.funcbody()


def _key_seg(name: str):
    return ("k", name)


def _literal_key(tok: Tok):
    if tok.kind == "str":
        try:
            return ("k", tok.value.decode("utf-8"))
        except UnicodeDecodeError:
            return None
    kind, value = tok.value
    if kind == "float" and value.is_integer():  # t[1.0] is t[1]
        return ("i", int(value))
    if kind == "int":
        return ("i", value)
    return None


def _path_text(path: tuple) -> str:
    out = ""
    for seg in path:
        if isinstance(seg, str):  # root name or "return"
            out += seg
        elif seg[0] == "k":
            name = seg[1]
            if _NAME.fullmatch(name) and name not in KEYWORDS:
                out += "." + name
            else:
                out += "[" + json.dumps(name, ensure_ascii=False) + "]"
        else:
            out += f"[{seg[1]}]"
    return out


DUPLICATE_WHY = "같은 값이 여러 번 정해져 있어 어느 것이 쓰이는지 확실하지 않음"


def _lua_scan(data: bytes) -> Scan:
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    text = data[len(bom):].decode("latin-1")
    out = Scan("lua", text=text, encoding="latin-1", bom=bom)
    try:
        data[len(bom):].decode("utf-8")
    except UnicodeDecodeError:
        out.ascii_only = True
        out.notes.append("UTF-8이 아닌 파일이라 글자 값에는 영문·숫자만 쓸 수 있습니다.")
    try:
        toks, comments = lua_tokens(text)
        reader = _LuaReader(toks, text)
        reader.chunk()
    except LuaSyntax as exc:
        out.errors.append(f"Lua 문법을 읽지 못했습니다: {exc}")
        return out
    except RecursionError:
        out.errors.append("표가 너무 깊이 겹쳐 있어 읽지 않습니다.")
        return out
    json_paths, json_refs = _json_calls(toks)
    out.data_files = sorted(set(json_paths))
    # comments per line
    tok_lines = {t.line for t in toks}
    by_line: dict[int, list] = {}
    for c in comments:
        by_line.setdefault(c[2], []).append(c)
    counts: dict[str, int] = {}
    keys = [_path_text(p.path) for p in reader.lits]
    for k in keys + [_path_text(p) for p, _ in reader.complex_fields]:
        counts[k] = counts.get(k, 0) + 1
    # a key one statement (a table constructor, or a multiple assignment) sets more than once, at least once
    # to a table: Lua keeps one of them, so neither a literal there nor anything inside such a table is sure
    sets: dict[tuple, list[bool]] = {}
    for path, stmt, is_table in [(p.path, p.stmt, False) for p in reader.lits] + \
            [(p, s, False) for p, s in reader.complex_fields] + [(p, s, True) for p, s in reader.tables]:
        sets.setdefault((path, stmt), []).append(is_table)
    twice = [key for key, tables in sets.items() if len(tables) > 1 and any(tables)]
    later = [(_path_text(p), s, clean) for p, s, clean in reader.assigned]
    starts = [t.start for t in toks]
    for lit, key in zip(reader.lits, keys):
        why, warn = lit.why, ""
        if not why and counts[key] > 1:
            why = DUPLICATE_WHY
        if not why:
            d = next((d for d, s in twice if s == lit.stmt and lit.path[:len(d)] == d), None)
            if d is not None:
                why = DUPLICATE_WHY if len(d) == len(lit.path) else \
                    f"'{_path_text(d)}' 값이 여러 번 정해져 있어 어느 것이 쓰이는지 확실하지 않음"
        if not why:
            for p, s, clean in later:
                if s > lit.stmt and (key == p or key.startswith(p + ".") or key.startswith(p + "[")):
                    if clean:
                        why = f"뒤에서 '{p}'를 다시 정함"
                        break
                    warn = f"스크립트가 뒤에서 '{p}'를 다른 값(저장된 설정 등)으로 바꿀 수 있어 기본값일 수 있습니다."
        note = _note_for(lit, by_line, tok_lines, toks, starts)
        group = _path_text(lit.path[:-1]) if len(lit.path) > 1 else ""
        raw = lit.raw.encode("latin-1").decode("utf-8", "replace")
        out.items.append(Item(key, "int" if lit.kind == "bigint" else lit.kind, lit.value, raw, lit.start,
                              lit.end, lit.line, editable=not why and lit.kind != "nil", why=why, note=note,
                              warn=warn, style=lit.style, group=group))
    for ref in json_refs:  # json.load_file(Const.CONFIG_PATH) with Const.CONFIG_PATH = "x.json" above
        item = next((i for i in out.items if i.key.endswith(ref) and i.kind == "string"), None)
        if item and item.value not in out.data_files:
            out.data_files.append(item.value)
    if out.data_files:
        out.notes = [n for n in out.notes if "reframework/data" not in n]
        out.notes.append("이 스크립트는 설정을 reframework/data 폴더의 파일(" + ", ".join(out.data_files) +
                         ")에 저장합니다. 그 파일이 있으면 스크립트 안의 기본값보다 그 파일의 값이 쓰일 수 있습니다.")
    return out


def _json_calls(toks: list[Tok]) -> tuple[list[str], list[str]]:
    """json.load_file / json.dump_file anywhere in the script (also inside functions): literal first
    arguments, and NAME{.NAME} arguments to look up among the items."""
    paths, refs = [], []
    for k in range(len(toks) - 4):
        if not (toks[k].kind == "name" and toks[k].text == "json" and toks[k + 1].text == "." and
                toks[k + 2].text in ("load_file", "dump_file") and toks[k + 3].text == "("):
            continue
        arg = toks[k + 4]
        if arg.kind == "str" and toks[k + 5].text in (",", ")"):
            try:
                paths.append(arg.value.decode("utf-8"))
            except UnicodeDecodeError:
                pass
            continue
        j, parts = k + 4, []
        while toks[j].kind == "name":
            parts.append(toks[j].text)
            if toks[j + 1].text == "." and toks[j + 1].kind == "op":
                j += 2
                continue
            if toks[j + 1].kind == "op" and toks[j + 1].text in (",", ")"):
                refs.append(".".join(parts))
            break
    return paths, refs


def _note_for(lit: _Lit, by_line, tok_lines, toks, starts) -> str:
    import bisect
    parts = []
    for c in by_line.get(lit.line, []):
        if c[0] >= lit.end:
            # only when nothing but , ; } ) follows the value on this line before the comment
            k = bisect.bisect_left(starts, lit.end)
            if all(toks[j].kind == "op" and toks[j].text in ",;})" for j in range(k, len(toks))
                   if toks[j].start < c[0]):
                parts.append(c[3].strip())
            break
    above = []
    line = lit.anchor - 1
    while line > 0 and line not in tok_lines and by_line.get(line) and len(above) < 3:
        above.insert(0, by_line[line][0][3].strip())
        line -= 1
    text = " ".join(p for p in above + parts if any(ch.isalnum() for ch in p))
    return text.encode("latin-1").decode("utf-8", "replace")[:300]


def _lua_number_text(item: Item, value) -> str:
    if item.kind == "int":
        if value == INT64_MIN:  # -9223372036854775808 is a float in Lua (minus applied to a too big integer)
            prefix = item.raw.lstrip("-").strip()[:2] if item.style == "hex" else "0x"
            return prefix + "8000000000000000"  # (hex integers wrap around: this one is the smallest integer)
        if item.style == "hex":
            raw = item.raw.lstrip("-").strip()
            digits = raw[2:]
            upper = any(c in "ABCDEF" for c in digits)
            text = format(abs(value), "X" if upper else "x").rjust(len(digits), "0")
            return ("-" if value < 0 else "") + raw[:2] + text
        return str(value)
    text = repr(float(value))
    if "inf" in text or "nan" in text:
        raise EditError("무한대나 NaN은 쓸 수 없습니다.")
    return text


def _lua_string_text(item: Item, value: str) -> str:
    raw = value.encode("utf-8")
    style = item.style
    if style.startswith("[") and b"\r" not in raw:  # (Lua turns any newline in a long string into \n)
        level = len(style) - 2
        while (raw + b"]" + b"=" * level + b"]").find(b"]" + b"=" * level + b"]") != len(raw):
            level += 1
        lead = b"\n" if raw.startswith(b"\n") else b""
        eq = "=" * level
        return "[" + eq + "[" + (lead + raw).decode("latin-1") + "]" + eq + "]"
    quote = style if style in ("'", '"') else '"'
    out = []
    for i, b in enumerate(raw):
        ch = chr(b)
        if ch == "\\":
            out.append("\\\\")
        elif ch == quote:
            out.append("\\" + quote)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif b < 0x20 or b == 0x7F:
            out.append(f"\\{b:03d}")
        else:
            out.append(ch)  # bytes >= 0x80 stay as they are (latin-1 text = the file's bytes)
    return quote + "".join(out) + quote


def _lua_write(item: Item, value, scan_: Scan) -> str:
    if item.kind == "string" and scan_.ascii_only and not value.isascii():
        raise EditError("UTF-8이 아닌 파일이라 영문·숫자만 쓸 수 있습니다.")
    if item.kind == "bool":
        return "true" if value else "false"
    if item.kind in ("int", "float"):
        return _lua_number_text(item, value)
    if item.kind == "string":
        return _lua_string_text(item, value)
    raise EditError("이 값은 바꿀 수 없습니다.")


# =================================================================================== JSON
class JsonSyntax(Exception):
    pass


_JSON_NUM = re.compile(r"-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_JSON_WS = re.compile(r"[ \t\n\r]*")


class _JsonReader:
    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.items: list[tuple] = []  # (path, kind, value, start, end)
        self.dups: set = set()
        self.depth = 0

    def ws(self):
        self.i = _JSON_WS.match(self.s, self.i).end()

    def value(self, path):
        self.ws()
        s, i = self.s, self.i
        if i >= len(s):
            raise JsonSyntax("값이 있어야 합니다")
        c = s[i]
        if c == "{":
            self.depth += 1
            if self.depth > 200:
                raise JsonSyntax("너무 깊이 겹친 값")
            self.i += 1
            self.ws()
            seen = set()
            if self.s.startswith("}", self.i):
                self.i += 1
                self.items.append((path, "object", {}, i, self.i))
            else:
                while True:
                    self.ws()
                    if not self.s.startswith('"', self.i):
                        raise JsonSyntax(f"{self.i}: 이름이 있어야 합니다")
                    key = self.string()
                    if key in seen:
                        self.dups.add(path + (key,))
                    seen.add(key)
                    self.ws()
                    if not self.s.startswith(":", self.i):
                        raise JsonSyntax(f"{self.i}: ':'가 있어야 합니다")
                    self.i += 1
                    self.value(path + (key,))
                    self.ws()
                    if self.s.startswith(",", self.i):
                        self.i += 1
                        continue
                    if self.s.startswith("}", self.i):
                        self.i += 1
                        break
                    raise JsonSyntax(f"{self.i}: ',' 또는 '}}'가 있어야 합니다")
            self.depth -= 1
            return
        if c == "[":
            self.depth += 1
            if self.depth > 200:
                raise JsonSyntax("너무 깊이 겹친 값")
            self.i += 1
            self.ws()
            if self.s.startswith("]", self.i):
                self.i += 1
                self.items.append((path, "array", [], i, self.i))
            else:
                k = 0
                while True:
                    self.value(path + (k,))
                    k += 1
                    self.ws()
                    if self.s.startswith(",", self.i):
                        self.i += 1
                        continue
                    if self.s.startswith("]", self.i):
                        self.i += 1
                        break
                    raise JsonSyntax(f"{self.i}: ',' 또는 ']'가 있어야 합니다")
            self.depth -= 1
            return
        if c == '"':
            v = self.string()
            self.items.append((path, "string", v, i, self.i))
            return
        for word, kind, v in (("true", "bool", True), ("false", "bool", False), ("null", "null", None)):
            if s.startswith(word, i):
                self.i += len(word)
                self.items.append((path, kind, v, i, self.i))
                return
        m = _JSON_NUM.match(s, i)
        if m and m.end() > i:
            raw = m.group()
            self.i = m.end()
            if m.group(1) or m.group(2):
                v = float(raw)
                if not math.isfinite(v):
                    raise JsonSyntax(f"{i}: 너무 큰 숫자")
                self.items.append((path, "float", v, i, self.i))
            else:
                if not math.isfinite(float(raw)):  # (as nlohmann::json: past a double's range is an error)
                    raise JsonSyntax(f"{i}: 너무 큰 숫자")
                v = int(raw)
                self.items.append((path, "int" if INT64_MIN <= v < (1 << 64) else "bigint", v, i, self.i))
            return
        raise JsonSyntax(f"{i}: 알 수 없는 값 {s[i:i + 10]!r}")

    def string(self) -> str:
        s, i = self.s, self.i
        j = i + 1
        while True:
            k = s.find('"', j)
            if k < 0:
                raise JsonSyntax(f"{i}: 닫히지 않은 글자")
            bs = 0
            p = k - 1
            while s[p] == "\\":
                bs += 1
                p -= 1
            if bs % 2 == 0:
                break
            j = k + 1
        raw = s[i:k + 1]
        if re.search(r"[\x00-\x1f]", raw):
            raise JsonSyntax(f"{i}: 글자 안에 제어 문자")
        try:
            v = json.loads(raw)
        except ValueError:
            raise JsonSyntax(f"{i}: 잘못된 글자") from None
        if re.search("[\ud800-\udfff]", v):
            raise JsonSyntax(f"{i}: 짝이 없는 UTF-16 대리 문자")
        self.i = k + 1
        return v


def _json_path_text(path: tuple) -> str:
    out = ""
    for seg in path:
        if isinstance(seg, int):
            out += f"[{seg}]"
        elif _NAME.fullmatch(seg):
            out += ("." if out else "") + seg
        else:
            out += "[" + json.dumps(seg, ensure_ascii=False) + "]"
    return out or "(전체)"


def _json_scan(data: bytes) -> Scan:
    out = Scan("json")
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    try:
        text = data[len(bom):].decode("utf-8")
    except UnicodeDecodeError:
        out.errors.append("UTF-8 글자가 아닌 파일입니다.")
        return out
    out.text, out.encoding, out.bom = text, "utf-8", bom
    reader = _JsonReader(text)
    try:
        reader.value(())
        reader.ws()
        if reader.i != len(text):
            raise JsonSyntax(f"{reader.i}: 값 뒤에 다른 내용이 있습니다 (주석이나 쉼표?)")
    except JsonSyntax as exc:
        out.errors.append(f"JSON을 읽지 못했습니다: {exc}")
        return out
    starts = _line_starts(text)
    for path, kind, value, start, end in reader.items:
        if kind in ("object", "array"):
            continue  # empty containers: nothing to edit
        why = ""
        if any(path[:n] in reader.dups for n in range(1, len(path) + 1)):
            why = "같은 이름이 여러 번 있어 어느 것이 쓰이는지 확실하지 않음"
        elif kind == "null":
            why = "값이 없음(null): 종류를 알 수 없음"
        elif kind == "bigint":
            why = "너무 큰 정수"
        style = "exp" if kind == "float" and "e" in text[start:end].lower() else ""
        kind = {"bigint": "int", "null": "nil"}.get(kind, kind)
        out.items.append(Item(_json_path_text(path), kind, value, text[start:end],
                              start, end, _line_of(starts, start), editable=not why, why=why, style=style,
                              group=_json_path_text(path[:-1]) if len(path) > 1 else ""))
    return out


def _json_write(item: Item, value, scan_: Scan) -> str:
    if item.kind == "bool":
        return "true" if value else "false"
    if item.kind == "int":
        return str(value)
    if item.kind == "float":
        text = repr(float(value))
        if "inf" in text or "nan" in text:
            raise EditError("무한대나 NaN은 쓸 수 없습니다.")
        return text
    if item.kind == "string":
        ascii_only = scan_.text.isascii() and "\\u" in scan_.text
        return json.dumps(value, ensure_ascii=ascii_only)
    raise EditError("이 값은 바꿀 수 없습니다.")


# ============================================================================= INI / TOML
_TOML_BARE = re.compile(r"[A-Za-z0-9_-]+")
_INT_TEXT = re.compile(r"[+-]?[0-9]+\Z")
_FLOAT_TEXT = re.compile(r"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z|[+-]?[0-9]+[eE][+-]?[0-9]+\Z")
_TOML_INT = re.compile(r"[+-]?(?:0|[1-9](?:_?[0-9])*)\Z")
_TOML_FLOAT = re.compile(r"[+-]?(?:0|[1-9](?:_?[0-9])*)(?:\.[0-9](?:_?[0-9])*)?(?:[eE][+-]?[0-9](?:_?[0-9])*)?\Z")


def _ini_scan(data: bytes, toml: bool = False) -> Scan:
    fmt = "toml" if toml else "ini"
    out = Scan(fmt)
    decoded = _decode_text(data)
    if decoded is None:
        if toml:
            out.errors.append("UTF-8 글자가 아닌 파일입니다.")
            return out
        # unknown 8-bit encoding (e.g. cp949): edit byte for byte, ASCII values only
        decoded = (data.decode("latin-1"), "latin-1", b"")
        out.notes.append("글자 인코딩을 알 수 없어 영문·숫자 값만 바꿀 수 있습니다.")
    text, out.encoding, out.bom = decoded
    out.text = text
    section = ""
    seen: dict[str, int] = {}
    raw_items = []
    pos = 0
    lines = text.splitlines(keepends=True)
    in_multi = None
    open_brackets = 0  # toml: lines of an array or inline table that goes on over several lines
    pending_comment: list[str] = []
    table_counts: dict[str, int] = {}
    for n, full in enumerate(lines, 1):
        body = full.rstrip("\r\n")
        line_start = pos
        pos += len(full)
        if in_multi:
            if in_multi in body:
                in_multi = None
            continue
        if open_brackets:
            open_brackets += _bracket_balance(body)
            if open_brackets < 0:
                out.errors.append(f"{n}번째 줄: 괄호가 맞지 않습니다.")
                return out
            continue
        stripped = body.strip()
        if not stripped:
            pending_comment = []
            continue
        if stripped[0] in ";#" or (not toml and stripped.startswith("//")):
            pending_comment.append(stripped.lstrip(";#/ ").strip())
            pending_comment = pending_comment[-3:]
            continue
        if stripped.startswith("[") and (not toml or not stripped.startswith("[[")) and "]" in stripped and \
                "=" not in stripped.split("]", 1)[0]:
            section = stripped[1:stripped.index("]")].strip()
            if toml:
                section = _toml_key_text(section)
            pending_comment = []
            continue
        if toml and stripped.startswith("[["):
            name = _toml_key_text(stripped[2:stripped.index("]]")].strip()) if "]]" in stripped else stripped
            table_counts[name] = table_counts.get(name, 0) + 1
            section = f"{name}[{table_counts[name] - 1}]"
            pending_comment = []
            continue
        eq = body.find("=")
        if eq < 0:
            pending_comment = []
            continue
        key_raw = body[:eq]
        key = key_raw.strip()
        vstart = eq + 1
        while vstart < len(body) and body[vstart] in " \t":
            vstart += 1
        vend = len(body)
        while vend > vstart and body[vend - 1] in " \t":
            vend -= 1
        value_raw = body[vstart:vend]
        why = ""
        kind, value, style = "string", value_raw, "plain"
        if toml:
            key = _toml_key_text(key)
            kind, value, style, why, cut = _toml_value(value_raw)
            if cut is not None:
                vend = vstart + cut  # value without its trailing comment
                value_raw = body[vstart:vend]
            if style.startswith("multi"):
                in_multi = style[5:]
            elif value_raw[:1] in "[{":
                open_brackets = max(0, _bracket_balance(value_raw))
        else:
            if len(value_raw) >= 2 and value_raw[0] == value_raw[-1] and value_raw[0] in "\"'":
                kind, value, style = "string", value_raw[1:-1], "quoted" + value_raw[0]
            else:
                kind, value = _ini_guess(value_raw)
        if out.encoding == "latin-1" and not (value_raw.isascii()):
            why = why or "글자 인코딩을 알 수 없는 값"
        full_key = f"[{section}] {key}" if section else key
        seen[full_key] = seen.get(full_key, 0) + 1
        raw_items.append(dict(key=full_key, kind=kind, value=value, raw=value_raw, start=line_start + vstart,
                              end=line_start + vend, line=n, why=why, style=style, group=section,
                              note=" ".join(pending_comment)))
        pending_comment = []
    if in_multi or open_brackets:
        out.errors.append("닫히지 않은 여러 줄 값이 있습니다.")
        return out
    for it in raw_items:
        if seen[it["key"]] > 1:
            it["why"] = it["why"] or "같은 이름이 여러 번 있어 어느 것이 쓰이는지 확실하지 않음"
        out.items.append(Item(it["key"], it["kind"], it["value"], it["raw"], it["start"], it["end"], it["line"],
                              editable=not it["why"], why=it["why"], note=it["note"][:300], style=it["style"],
                              group=it["group"]))
    if toml:
        _toml_check(out, data)
    return out


def _bracket_balance(text: str) -> int:
    """Open minus closed [ { of a TOML line outside strings and comments."""
    depth, i, n = 0, 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            break
        if c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if (c == '"' and text[j] == "\\") else 1
            i = j + 1
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
        i += 1
    return depth


def _ini_guess(raw: str):
    low = raw.lower()
    if low in ("true", "false"):
        return "bool", low == "true"
    if _INT_TEXT.match(raw) and len(raw.lstrip("+-")) <= 19 and INT64_MIN <= int(raw) <= INT64_MAX:
        return "int", int(raw)  # (the integers the writers take: _coerce)
    if _FLOAT_TEXT.match(raw) and math.isfinite(float(raw)):  # (1e999 stays text)
        return "float", float(raw)
    return "string", raw


def _toml_key_text(key: str) -> str:
    parts = []
    for p in re.findall(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'|[^.]+', key):
        p = p.strip()
        if p.startswith('"'):
            try:
                p = json.loads(p)
            except ValueError:
                pass
        elif p.startswith("'"):
            p = p[1:-1]
        parts.append(p)
    return ".".join(parts)


def _toml_value(raw: str):
    """(kind, value, style, why, cut) of a TOML value text (cut: length without a trailing comment)."""
    if raw.startswith(('"""', "'''")):
        q = raw[:3]
        if raw.count(q) >= 2 and raw.rstrip().endswith(q) and len(raw) >= 6:
            return "string", raw, "plain", "여러 줄 글자", None
        return "string", raw, "multi" + q, "여러 줄 글자", None
    if raw.startswith('"'):
        m = re.match(r'"(?:[^"\\]|\\.)*"', raw)
        if m:
            rest = raw[m.end():].strip()
            if rest and not rest.startswith("#"):
                return "string", raw, "plain", "알 수 없는 형식", None
            try:
                return "string", json.loads(m.group()), 'quoted"', "", m.end()
            except ValueError:
                return "string", raw, "plain", "TOML 이스케이프", m.end()
    if raw.startswith("'"):
        k = raw.find("'", 1)
        if k > 0:
            rest = raw[k + 1:].strip()
            if rest and not rest.startswith("#"):
                return "string", raw, "plain", "알 수 없는 형식", None
            return "string", raw[1:k], "quoted'", "", k + 1
    body = raw.split("#", 1)[0].rstrip()
    cut = len(body)
    if body in ("true", "false"):
        return "bool", body == "true", "plain", "", cut
    if _TOML_INT.match(body):
        digits = body.replace("_", "")
        if len(digits.lstrip("+-")) > 19 or not INT64_MIN <= int(digits) <= INT64_MAX:
            return "int", None, "plain", "너무 큰 정수", cut  # (TOML integers are 64-bit)
        return "int", int(digits), "plain", "", cut
    if _TOML_FLOAT.match(body):
        return "float", float(body.replace("_", "")), "plain", "", cut
    return "string", raw, "plain", "배열·표·날짜 등은 바꿀 수 없음", None


def _toml_check(out: Scan, data: bytes) -> None:
    try:
        import tomllib
    except ImportError:
        out.items = [replace(i, editable=False, why=i.why or "TOML을 확인할 수 없음 (Python 3.11 이상 필요)")
                     for i in out.items]
        return
    try:
        doc = tomllib.loads(out.text)
    except (ValueError, RecursionError) as exc:  # (TOMLDecodeError; an integer of thousands of digits)
        out.errors.append(f"TOML을 읽지 못했습니다: {exc}" if isinstance(exc, ValueError) else
                          "TOML을 읽지 못했습니다: 값이 너무 깊이 겹쳐 있습니다.")
        out.items = []
        return
    # each editable item must be found in the parsed document with the same value
    fixed = []
    for it in out.items:
        if it.editable:
            got = _toml_lookup(doc, it)
            if got is _MISSING or not _same(got if not isinstance(got, bool) else got, it.value) or \
                    type(got) is not type(it.value):
                it = replace(it, editable=False, why="TOML 읽기 결과와 맞지 않음")
        fixed.append(it)
    out.items = fixed


_MISSING = object()


def _toml_lookup(doc, item: Item):
    section = item.group
    key = item.key[len(f"[{section}] "):] if section else item.key
    node = doc
    parts = []
    for p in (section.split(".") if section else []):
        m = re.fullmatch(r"(.*)\[(\d+)\]", p)
        if m:
            parts += [m.group(1), int(m.group(2))]
        else:
            parts.append(p)
    parts += key.split(".")
    for p in parts:
        try:
            node = node[p]
        except (KeyError, IndexError, TypeError):
            return _MISSING
    return node


def _ini_write(item: Item, value, scan_: Scan) -> str:
    if item.kind == "bool":
        raw = item.raw
        text = "true" if value else "false"
        if raw.isupper():
            text = text.upper()
        elif raw[:1].isupper():
            text = text.capitalize()
    elif item.kind == "int":
        text = str(value)
    elif item.kind == "float":
        text = repr(float(value))
        if scan_.format == "ini" and "e" in text and "e" not in item.raw.lower():
            text = format(Decimal(text), "f")  # the same number written out in full (1e-07 → 0.0000001)
            text = text if "." in text else text + ".0"  # (1e+16 would read back as an integer)
    else:
        text = value
        if len(("x" + text + "x").splitlines()) > 1:  # (lines are split as str.splitlines does: \v ...)
            raise EditError("줄바꿈은 넣을 수 없습니다.")
        if scan_.encoding == "latin-1" and not text.isascii():  # (before the quoted values return too)
            raise EditError("글자 인코딩을 알 수 없는 파일이라 영문·숫자만 쓸 수 있습니다.")
        if scan_.format == "toml":
            if item.style == "quoted'" and not re.search(r"['\x00-\x08\x0a-\x1f\x7f]", text):
                return "'" + text + "'"
            return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")  # (TOML escapes DEL too)
        if item.style.startswith("quoted"):
            q = item.style[-1]
            if q in text:
                raise EditError(f"{q} 글자는 넣을 수 없습니다.")
            return q + text + q
        if text != text.strip():
            raise EditError("값의 앞뒤에 빈칸을 넣을 수 없습니다.")
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":  # would read back without them
            raise EditError("값의 앞뒤를 따옴표로 감쌀 수 없습니다.")
    if scan_.encoding == "latin-1" and not text.isascii():
        raise EditError("글자 인코딩을 알 수 없는 파일이라 영문·숫자만 쓸 수 있습니다.")
    return text


_WRITERS = {"lua": _lua_write, "json": _json_write, "ini": _ini_write, "toml": _ini_write}


# ------------------------------------------------------------------- which Lua values look like settings
_SETTINGS_WORDS = re.compile(r"conf|setting|option|pref|default|cfg|opts|user|custom|tweak", re.IGNORECASE)


def settings_like(item: Item) -> bool:
    """A guess for the default view: values in a table named like settings, UPPER_CASE constants, values
    with a comment, and every value of a ``return {...}`` module. The rest stays one click away."""
    root = re.split(r"[.\[]", item.key, maxsplit=1)[0]
    if root == "return" or item.note:
        return True
    if _SETTINGS_WORDS.search(item.group or "") or _SETTINGS_WORDS.search(root):
        return True
    last = re.split(r"[.\[]", item.key)[-1].rstrip("]")
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", last))


# ------------------------------------------------------------------------ 모드 수정 engines (engines.py)
ENGINES = ("lua", "json", "ini", "toml")
_DATA_NOTE_OLD = "이 스크립트는 설정을 reframework/data 폴더의 파일("  # scan()'s note, replaced by one note per file
SETTINGS_NOTE = "⚠ 표시는 스크립트가 실행하면서 저장된 설정으로 바꿀 수 있는 값입니다."
LANG_NOTE = "한글은 모드가 한글 글꼴을 쓸 때만 보입니다"
# a shipped file whose copy in the game folder changed (Deployer.placed "changed" or "stash"): read-only (I11)
GAME_COPY_CHANGED = ("게임 안에서 이 파일이 바뀌었습니다(스크립트가 설정을 저장했을 수 있습니다). 여기서 고치면 게임에서 "
                     "바꾼 내용이 사라지므로 게임 안의 설정 메뉴에서 바꾸세요.")
_LANG_DIR = re.compile(r"(^|/)(lang|languages)/", re.IGNORECASE)


def data_file_note(path: str) -> str:
    """The note for a settings file a script keeps in reframework/data (it is only pointed at)."""
    return (f"이 스크립트는 설정을 게임 폴더의 reframework/data/{path}에 저장합니다. 그 설정은 게임 안 "
            "REFramework 메뉴(Script Generated UI)에서 바꾸세요.")


def canonical(kind: str, value, raw: str = "") -> str:
    """The canonical text of a config value: integers in decimal (the writer keeps a hex literal's
    style), floats repr, true/false, strings as they are; anything else its literal."""
    if kind == "bool" and isinstance(value, bool):
        return "true" if value else "false"
    if kind == "int" and isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if kind == "float" and isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(float(value))
    if kind == "string" and isinstance(value, str):
        return value
    return raw


def typed(kind: str, text: str):
    """The value a canonical text stands for (EditError when it is not one of ``kind``)."""
    t = str(text)
    if kind == "bool":
        if t.strip().lower() in ("true", "false"):
            return t.strip().lower() == "true"
        raise EditError("참(true)이나 거짓(false)이어야 합니다.")
    if kind == "int":
        try:
            s = t.strip()
            return int(s, 16) if s.lower().startswith(("0x", "-0x")) else int(s)
        except ValueError:
            raise EditError("정수를 적어 주세요.") from None
    if kind == "float":
        try:
            v = float(t)
        except ValueError:
            raise EditError("숫자를 적어 주세요.") from None
        if not math.isfinite(v):
            raise EditError("무한대나 NaN은 쓸 수 없습니다.")
        return v
    if kind == "string":
        return t
    raise EditError("이 값은 바꿀 수 없습니다.")


def _label(item: Item, fmt: str) -> str:
    key, group = item.key, item.group
    if fmt in ("ini", "toml"):
        prefix = f"[{group}] " if group else ""
        return key[len(prefix):] if prefix and key.startswith(prefix) else key
    if group and key.startswith(group) and len(key) > len(group):
        rest = key[len(group):]
        return rest[1:] if rest.startswith(".") else rest
    return key


class ConfigDoc:
    """engines.Doc over a Scan: one Value per item (``at`` = Item.key; a key the file sets more than
    once is listed once per time, read-only, the later ones as "<key> #2" ...).  ``notes`` say where
    the script keeps its saved settings (one note per file, only pointed at), what ⚠ means, and that
    Korean text needs a Korean font for files under lang/ or languages/.  EditError when the file
    cannot be read."""

    def __init__(self, data: bytes, fmt: str, rel: str = ""):
        if fmt not in ENGINES:
            raise ValueError(fmt)
        self.data, self.format, self.rel = bytes(data), fmt, rel
        self.scan = scan(self.data, fmt)
        if self.scan.errors:
            raise EditError(self.scan.errors[0])
        self.data_files = list(self.scan.data_files)
        self.notes = [n for n in self.scan.notes if not n.startswith(_DATA_NOTE_OLD)]
        self.notes += [data_file_note(path) for path in self.data_files]
        items = self.scan.items
        if fmt == "lua" and any(i.warn for i in items):
            self.notes.append(SETTINGS_NOTE)
        if _LANG_DIR.search(rel.replace("\\", "/")) and any(i.kind == "string" for i in items):
            self.notes.append(LANG_NOTE)
        self._values: list[Value] = []
        self._by_key: dict[str, Value] = {}
        times: dict[str, int] = {}
        for item in items:
            n = times[item.key] = times.get(item.key, 0) + 1
            value = self._value(item, item.key if n == 1 else f"{item.key} #{n}")
            self._values.append(value)
            self._by_key.setdefault(value.at, value)

    def _value(self, item: Item, at: str) -> Value:
        return Value(at, _label(item, self.format), item.group, item.kind,
                     canonical(item.kind, item.value, item.raw), editable=item.editable, why=item.why,
                     note=item.note, warn=item.warn,
                     spec=(self.format, item.style, self.scan.encoding, self.scan.ascii_only),
                     settings=settings_like(item) if self.format == "lua" else True, raw=item.raw)

    def values(self) -> list[Value]:
        return self._values

    def get(self, at: str) -> Value | None:
        return self._by_key.get(at)

    def expand(self, rule_at: str, before: str) -> list[Value]:
        return []  # no rules in settings files

    def write(self, changes) -> bytes:
        """set_values with each ``key → canonical text`` (checked as set_values checks)."""
        values = {}
        for at, text in changes.items():
            item = self.scan.get(at)
            if item is None:
                raise EditError(f"'{at}' 값을 찾지 못했습니다.")
            values[at] = typed(item.kind, text)
        return set_values(self.data, self.format, values)


class TextEngine:
    """engines.Engine for one settings format ("lua" | "json" | "ini" | "toml")."""

    tool = "config"

    def __init__(self, name: str):
        if name not in ENGINES:
            raise ValueError(name)
        self.name = name

    def open(self, data: bytes, rel: str) -> ConfigDoc:
        return ConfigDoc(data, self.name, rel)

    def parse(self, value: Value, text: str) -> str:
        """The canonical text of what the user typed, refused (EditError) when the file could not hold
        it (a newline in an INI value, a quote in a quoted one, non-ASCII text in a non-UTF-8 file)."""
        if not value.editable:
            raise EditError(value.why or "이 값은 바꿀 수 없습니다.")
        fmt, style, encoding, ascii_only = (tuple(value.spec) + (self.name, "", "utf-8", False)[len(value.spec):])[:4]
        item = Item(value.at, value.kind, None, value.raw, 0, 0, 0, style=style)
        result = _coerce(item, parse_input(item, text))
        _WRITERS[self.name](item, result, Scan(self.name, encoding=encoding, ascii_only=ascii_only))
        return canonical(value.kind, result, value.raw)

    def same(self, value: Value, a: str, b: str) -> bool:
        if a == b:
            return True
        try:
            return _same(typed(value.kind, a), typed(value.kind, b))
        except EditError:
            return False
