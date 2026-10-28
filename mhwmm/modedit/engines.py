"""Value engines and the rebase rule.

An engine opens one file (or PAK entry) as a ``Doc`` of addressed values and writes changed values
back in place: ``rsz`` for .user data files (rszdata.py), ``lua``/``json``/``ini``/``toml`` for the
settings files scripts ship with (textconfig.py).  Engines are found by name; the modules are imported
only when an engine is first asked for (the RSZ type registry loads only when a file is opened).

``apply`` re-applies a member's recorded changes on a file - the file they were made on, or a newer
one after an update or a matfix repair - under one rule for every engine:

- rules (``[*]``, data files) are judged first, in record order, on the values in the file: every
  editable row whose value equals the rule's ``before`` gets ``after``; no such row is a conflict
  "norow";
- then single values: gone, read-only or of another kind → conflict; already the wanted value →
  nothing to write (the change stays recorded); still the value it was made on (or, in a config file,
  the same literal) → set, over a rule's row; otherwise the author changed it → conflict "changed";
- the written file is opened again and every value set must read back as asked.

Everything is keyed by the address the file itself names a value by (``Doc.get``/``expand``), so a
change recorded under another spelling of the same value (a data file's row found by an older key)
is the same change: the kept changes come back spelled the file's way, and of two changes of one value
the later wins.  Conflicts left from an earlier build are judged again the same way (``conflicts``):
one whose value is back to what the change was made on is applied, one that already holds the wanted
value is kept as a change, the others come back with what the file holds now.  Conflicts are never
applied over the author's value: the file keeps it until the user decides.
"""

from __future__ import annotations

import math
import re
import struct
import threading
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Mapping, Protocol, Sequence

from . import EditError
from .model import Change, Conflict

INT_RANGES = {
    "S8": (-(1 << 7), (1 << 7) - 1), "U8": (0, (1 << 8) - 1),
    "S16": (-(1 << 15), (1 << 15) - 1), "U16": (0, (1 << 16) - 1),
    "S32": (-(1 << 31), (1 << 31) - 1), "U32": (0, (1 << 32) - 1),
    "S64": (-(1 << 63), (1 << 63) - 1), "U64": (0, (1 << 64) - 1),
    "int": (-(1 << 63), (1 << 63) - 1),  # config integers (larger ones are read-only)
}
FLOAT_KINDS = frozenset({"F16", "F32", "F64", "float"})
BOOL_KINDS = frozenset({"Bool", "bool"})
VECTOR_KINDS = frozenset({
    "Vec2", "Vec3", "Vec4", "Float2", "Float3", "Float4", "Int2", "Int3", "Int4", "Uint2", "Uint3", "Uint4",
    "Quaternion", "Range", "RangeI", "Size", "Point", "Position", "Half4", "Color",
})
MODES = ("set", "times", "change")
THOUSANDS = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?")  # 1,000 / -12,345.5: the only commas a number may have


@dataclass(frozen=True)
class Value:
    """One addressed value of an open file.

    ``group`` is the row it belongs to ("" at the root), ``text`` its canonical text, ``editable``
    false with ``why`` for read-only leaves; ``note``/``warn`` are shown with it, ``spec`` is the
    engine's own detail (for example an element count), ``settings`` whether a config value looks
    like a setting, ``rule_at`` the rule address of a table row ("" outside tables) and ``raw`` the
    literal as written (config files)."""

    at: str
    label: str
    group: str
    kind: str
    text: str
    editable: bool = True
    why: str = ""
    note: str = ""
    warn: str = ""
    spec: tuple = ()
    settings: bool = True
    rule_at: str = ""
    raw: str = ""


class Doc(Protocol):
    notes: list[str]
    # optional: ``readonly`` (str), why the whole file cannot be changed (for example its selfcheck failed);
    # apply then raises EditError with it instead of turning every change into a conflict.
    # optional: ``place(at)`` (where a value lies, the same in a file written from this one) and
    # ``at_place(place)`` (the Value there): apply then reads a written value back by its place, not by
    # its address (a data file's rows may be named differently once a key value changed)

    def values(self) -> Sequence[Value]: ...

    def get(self, at: str) -> Value | None:
        """The value ``at`` names, with ``Value.at`` the address this file names it by."""
        ...

    def expand(self, rule_at: str, before: str) -> list[Value]:
        """The rows of a ``[*]`` rule whose value in this file equals ``before`` (``Value.rule_at``: the
        rule as this file spells it)."""
        ...

    def write(self, changes: Mapping[str, str]) -> bytes:
        """The file with ``at → canonical text`` set, verified (EditError when it is not)."""
        ...


class Engine(Protocol):
    name: str  # "rsz" | "lua" | "json" | "ini" | "toml"
    tool: str  # "data" | "config"

    def open(self, data: bytes, rel: str) -> Doc:
        """StaleError (an older layout) or EditError (anything else it cannot edit)."""
        ...

    def parse(self, value: Value, text: str) -> str:
        """User input → canonical text; EditError with a Korean message."""
        ...

    def same(self, value: Value, a: str, b: str) -> bool:
        """Whether two texts are the same value (data floats by their packed bits, config by value)."""
        ...


_ENGINES: dict[str, Engine] = {}
_LOCK = threading.Lock()
_LOAD_LOCK = threading.Lock()


def register(engine: Engine) -> None:
    """Add or replace an engine (tests register fakes)."""
    with _LOCK:
        _ENGINES[engine.name] = engine


def unregister(name: str) -> None:
    with _LOCK:
        _ENGINES.pop(name, None)


def registered(name: str) -> Engine | None:
    """The engine registered under ``name``, without importing anything."""
    return _ENGINES.get(name)


def _load(name: str) -> None:
    if name == "rsz":
        from . import rszdata

        if name not in _ENGINES:
            register(rszdata.RszEngine())
    elif name in ("lua", "json", "ini", "toml"):
        from . import textconfig

        if name not in _ENGINES:
            register(textconfig.TextEngine(name))


def get(name: str) -> Engine:
    """The engine called ``name`` (EditError when there is none)."""
    engine = _ENGINES.get(name)
    if engine is None:
        with _LOAD_LOCK:
            if name not in _ENGINES:
                _load(name)
        engine = _ENGINES.get(name)
    if engine is None:
        raise EditError(f"알 수 없는 수정 종류입니다: {name}")
    return engine


def engine_for(rel: str, head: bytes = b"") -> Engine | None:
    """The engine for a stored file (or, with ``head``, a PAK entry starting with those bytes)."""
    from . import model

    if head[:4] == b"USR\0" or (not head and model.is_data_rel(rel)):
        return get("rsz")
    if head:
        return None
    name = model.config_engine(rel)
    return get(name) if name else None


# ---- canonical numbers ---------------------------------------------------------------------------------
def float_text(x: float, kind: str = "F64") -> str:
    """Canonical text of a float: F32/F16 the shortest ``%.{6..9}g`` that packs back to the same bits
    (``.0`` added when integral), anything else ``repr``."""
    fmt = {"F32": "<f", "F16": "<e"}.get(kind)
    if fmt is None:
        return repr(float(x))
    bits = struct.pack(fmt, x)
    value = struct.unpack(fmt, bits)[0]
    text = repr(value)
    for p in range(6, 10):
        t = f"{value:.{p}g}"
        if struct.pack(fmt, float(t)) == bits:
            text = t
            break
    if not any(ch in text for ch in ".eEn"):
        text += ".0"
    return text


def _round_half_away(x: Fraction) -> int:
    n = math.floor(abs(x) + Fraction(1, 2))
    return n if x >= 0 else -n


def _number(text) -> Fraction:
    """A number typed or stored as text (decimal, float or 0x hex; thousands separators taken out:
    1,000).  Any other comma is refused with EditError, not dropped (a decimal comma: 0,5 would read
    as 5); ValueError when it is not a number."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        if isinstance(text, float) and not math.isfinite(text):
            raise ValueError(text)
        return Fraction(text)
    s = str(text).strip()
    if "," in s:
        if not THOUSANDS.fullmatch(s):
            raise EditError(f"숫자를 적어 주세요 (소수점은 . 으로): {s}")
        s = s.replace(",", "")
    if s.lower().startswith(("0x", "-0x", "+0x")):
        return Fraction(int(s, 16))
    x = float(s)
    if not math.isfinite(x):
        raise ValueError(s)
    try:
        return Fraction(s)
    except ValueError:
        return Fraction(x)


def plain_number(text) -> float:
    """A number the user typed, read by the rule of the bulk values (``scaled``): decimal, float or 0x
    hex, finite, with thousands separators taken out (1,000).  EditError (Korean) when it is not one,
    and for a comma anywhere else (0,5 is refused, not read as 5)."""
    try:
        return float(_number(text))
    except EditError:
        raise
    except (ValueError, OverflowError, ZeroDivisionError):
        raise EditError("숫자를 적어 주세요.") from None


# ---- the rebase rule ------------------------------------------------------------------------------------
@dataclass
class Applied:
    """The result of ``apply``: the new bytes (the input when nothing was written), the changes written
    (rules with their row ``count``), those already in place, the new conflicts (of ``changes``), the
    rows changed per rule address, ``kept``: the changes that stay in the record (written or already in
    place: those given, then the earlier conflicts that no longer are), ``still``: the earlier conflicts
    that still are, as the file holds them now, and ``renamed``: the addresses given that the file
    spells another way (given → the file's).  Everything returned is spelled the file's way."""

    data: bytes
    done: list[Change]
    already: list[Change]
    conflicts: list[Conflict]
    counts: dict[str, int] = field(default_factory=dict)
    kept: list[Change] = field(default_factory=list)
    still: list[Conflict] = field(default_factory=list)
    renamed: dict[str, str] = field(default_factory=dict)


def apply(engine: Engine, data: bytes, rel: str, changes: Sequence[Change],
          conflicts: Sequence[Conflict] = ()) -> Applied:
    """Apply ``changes`` to ``data`` under the rebase rule and judge the earlier ``conflicts`` again (see
    the module doc).  Errors opening the file propagate (the caller marks the member pending); a
    written file that does not read back as asked raises EditError."""
    doc = engine.open(data, rel)
    locked = getattr(doc, "readonly", "")
    if locked:
        if changes:
            raise EditError(locked)  # the member waits (or the save stops): its changes are not judged
        return Applied(data, [], [], [], {}, [], list(conflicts))
    config = getattr(engine, "tool", "") == "config"
    given = [*changes, *(x.change for x in conflicts)]
    old = len(changes)  # given[old:] are the earlier conflicts
    # where each change lies, by the file's own address (rules: their editable rows)
    found: dict[int, list[Value]] = {}
    single: dict[int, Value | None] = {}
    canon: dict[int, str] = {}
    for i, c in enumerate(given):
        if c.rule:
            rows = doc.expand(c.at, c.before)
            found[i] = [v for v in rows if v.editable]
            canon[i] = next((v.rule_at for v in rows if v.rule_at), c.at)
        else:
            v = single[i] = doc.get(c.at)
            canon[i] = v.at if v is not None and v.at else c.at
    # one change per value: the last change given wins, an earlier conflict loses to any change
    winner: dict[tuple[str, str], int] = {}
    for i, c in enumerate(given):
        ident = (canon[i], c.before if c.rule else "")
        if i < old or ident not in winner or winner[ident] >= old:
            winner[ident] = i
    live = set(winner.values())
    renamed = {c.at: canon[i] for i, c in enumerate(given) if i in live and canon[i] != c.at}

    def spelled(i: int) -> Change:
        c = given[i]
        return c if canon[i] == c.at else replace(c, at=canon[i])

    targets: dict[str, str] = {}
    labels: dict[str, str] = {}
    owner: dict[str, int] = {}  # row address → the rule (index in given) that set it
    rows_of: dict[int, int] = {}
    result: dict[int, Change] = {}  # index in given → the change as kept
    done_at: set[int] = set()
    already, new, still = [], [], []

    def conflict(i: int, now: str | None, why: str, note: str = "") -> None:
        (new if i < old else still).append(Conflict(spelled(i), now, why, note))

    for i, c in enumerate(given):
        if not c.rule or i not in live:
            continue
        rows = found[i]
        if not rows:
            conflict(i, None, "norow")
            continue
        for v in rows:
            if v.at in owner:  # two rules on one row cannot both hold: the first in record order wins
                continue
            targets[v.at] = c.after
            labels[v.at] = v.label
            owner[v.at] = i
            rows_of[i] = rows_of.get(i, 0) + 1
        result[i] = spelled(i)
    for i, c in enumerate(given):
        if c.rule or i not in live:
            continue
        v = single[i]
        if v is None:
            conflict(i, None, "gone")
            continue
        if not v.editable:
            conflict(i, v.text, "readonly", v.why)
            continue
        if c.kind and v.kind != c.kind:
            conflict(i, v.text, "kind")
            continue
        at = canon[i]
        if engine.same(v, v.text, c.after):
            targets.pop(at, None)  # also a rule's row: the single value wins, nothing to write
            already.append(i)
        elif engine.same(v, v.text, c.before) or (config and c.raw and v.raw == c.raw):
            targets[at] = c.after  # over a rule's row
            labels[at] = v.label or c.label
            done_at.add(i)
        else:
            conflict(i, v.text, "changed")
            continue
        if at in owner:
            rows_of[owner.pop(at)] -= 1
        result[i] = spelled(i)
    counts: dict[str, int] = {}
    for i, c in list(result.items()):
        if c.rule:
            n = rows_of.get(i, 0)
            result[i] = c = replace(c, count=n)
            counts[c.at] = counts.get(c.at, 0) + n
            done_at.add(i)
    kept = [result[i] for i in sorted(result)]
    done = [result[i] for i in sorted(done_at)]
    already = [result[i] for i in already]
    if not targets:
        return Applied(data, done, already, new, counts, kept, still, renamed)
    out = doc.write(targets)
    back = engine.open(out, rel)
    place, at_place = getattr(doc, "place", None), getattr(back, "at_place", None)
    for at, text in targets.items():
        if place is not None and at_place is not None:
            where = place(at)
            v = None if where is None else at_place(where)
        else:
            v = back.get(at)
        if v is None or not engine.same(v, v.text, text):
            raise EditError(f"바꾼 파일을 확인하지 못해 저장하지 않았습니다: {labels.get(at) or at} 값이 "
                            f"{text}(으)로 읽히지 않습니다")
    return Applied(out, done, already, new, counts, kept, still, renamed)


# ---- bulk values ---------------------------------------------------------------------------------------
def scaled(engine: Engine, value: Value, mode: str, k: float | str, mod_text: str,
           game_text: str | None) -> tuple[str | None, str]:
    """A bulk value for one row: ``set`` (``k`` is the value, as typed), ``times`` (game × k) or
    ``change`` (game + k·(mod − game)).  Returns (canonical text, note) or (None, why it was skipped).
    Integers round half away from zero and are clamped to their kind's range; floats must be finite;
    bool rows support only ``set``; vectors are skipped.  ``k`` is read as ``plain_number`` reads it
    (a decimal comma skips every number row with that message; a text row's ``set`` takes it as typed)."""
    if mode not in MODES:
        raise ValueError(mode)
    kind = value.kind
    if kind in VECTOR_KINDS:
        return None, "벡터 값은 하나씩 바꾸세요"
    numeric = kind in INT_RANGES or kind in FLOAT_KINDS
    if mode == "set" and not numeric:
        try:
            return engine.parse(value, str(k)), ""
        except EditError as exc:
            return None, str(exc)
    if kind in BOOL_KINDS:
        return None, "켜기·끄기 값은 '값 정하기'로만 바꿀 수 있습니다"
    if not numeric:
        return None, "숫자가 아닌 값이라 건너뛴 값"
    try:
        if mode == "set":
            x = _number(k)
        else:
            if game_text is None:
                return None, "게임 값이 없어 건너뛴 값"
            game, factor = _number(game_text), _number(k)
            x = game * factor if mode == "times" else game + factor * (_number(mod_text) - game)
    except EditError as exc:  # (a comma that is not a thousands separator: 0,5)
        return None, str(exc)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None, "숫자로 읽지 못해 건너뛴 값"
    if kind in INT_RANGES:
        low, high = INT_RANGES[kind]
        n = _round_half_away(x)
        note = ""
        if n < low or n > high:
            n = min(max(n, low), high)
            note = f"범위를 넘어 {n}로 맞췄습니다"
        try:
            return engine.parse(value, str(n)), note
        except EditError as exc:
            return None, str(exc)
    try:
        f = float(x)
    except OverflowError:
        return None, "값이 너무 커서 건너뛴 값"
    if not math.isfinite(f):
        return None, "값이 너무 커서 건너뛴 값"
    try:
        return engine.parse(value, repr(f)), ""
    except EditError as exc:
        return None, str(exc)
