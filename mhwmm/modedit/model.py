"""The 모드 수정 record (``edits.json``), the plans the window and the CLI ask for, ``Pending`` (the
window's unsaved changes) and the view types the manager fills for the pages and the CLI.

A record lists *items*, one per stored file of the mod.  An item is in a scope (an option, by name and
by which of the options with that name it is: ids move when an update inserts an option), may be left
out (``exclude``) and may hold *members* with value changes: ``""`` for a loose file, ``"#<hash>"`` for
an entry of a root PAK.  A change says what a value was in the file it was made on (``before``) and
what it should be (``after``), so it can be applied again on a new version of the file under the
rebase rule (engines.apply).  Values are kept as canonical text:

    integers   decimal                  bool     true / false
    F32, F16   the shortest %.{6..9}g text that packs back to the same bits, ".0" when integral
    F64        repr                     config   floats repr, strings as they are
    vectors    elements joined by ", "

Nothing here touches the manager or Tk; ``load``/``save``/``set_aside`` are the only functions that
read or write files.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping

from .. import fsutil
from ..deploy import is_root_pak
from . import BAD_SUFFIX, EDITS_DIR, EDITS_FORMAT, EDITS_RECORD, EditError

if TYPE_CHECKING:
    from .engines import Value

log = logging.getLogger(__name__)

DATA_RE = re.compile(r"\.user\.\d+$", re.IGNORECASE)  # .user data files (engine "rsz")
PAK_MEMBER_RE = re.compile(r"#[0-9A-Fa-f]{16}")
DATA_ENGINES = ("rsz",)
CONFIG_ENGINES = ("lua", "json", "ini", "toml")
WHYS = ("changed", "gone", "readonly", "kind", "norow")
OUT_RE = re.compile(r"edits/(\d+)_[^/\\]+")


# ---- the record ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Source:
    """Which stored file: the option by name and n (0 = the first option with that name, in the mod's
    order; ``""``/0 for a mod without options) and the path inside it."""

    option: str = ""
    n: int = 0
    rel: str = ""

    def label(self) -> str:
        if not self.option:
            return self.rel
        name = self.option if not self.n else f"{self.option} ({self.n + 1}번째)"
        return f"{name}: {self.rel}"


@dataclass(frozen=True)
class Change:
    """One value to set.  ``at`` with ``[*]`` is a rule (data files only): every row of that table
    whose value in the file is ``before``; ``count`` is how many rows it changed at the last build.
    ``raw`` (config files) is the literal as it was written in the file, ``how`` how a bulk value was
    computed (only shown)."""

    at: str
    kind: str
    before: str
    after: str
    label: str = ""
    raw: str = ""
    count: int = field(default=0, compare=False)  # (count and how are only shown: equal changes may differ)
    how: dict | None = field(default=None, compare=False)

    @property
    def rule(self) -> bool:
        return "[*]" in self.at

    @property
    def ident(self) -> tuple[str, str]:
        """What a change is told apart by in its member: single values by address, rules by
        address and the value they match (two rules may cover the same field)."""
        return (self.at, self.before if self.rule else "")


@dataclass(frozen=True)
class Conflict:
    """A change that could not be applied as it was made (never applied until the user decides):
    ``why`` is changed (the author changed the value: ``now``), gone, readonly (``note`` says why),
    kind (the value's type changed) or norow (a rule matched no row)."""

    change: Change
    now: str | None
    why: str
    note: str = ""

    @property
    def ident(self) -> tuple[str, str]:
        return self.change.ident

    def text(self) -> str:
        c = self.change
        if self.why == "changed":
            return f"작성자가 바꾼 값: {c.before} → {self.now} (내 값 {c.after})"
        if self.why == "gone":
            return f"없어진 값입니다 (내 값 {c.after})"
        if self.why == "readonly":
            return f"바꿀 수 없는 값이 되었습니다: {self.note}" if self.note else "바꿀 수 없는 값이 되었습니다"
        if self.why == "kind":
            return "값의 종류가 바뀌었습니다"
        return f"값이 {c.before}인 행이 없습니다"


@dataclass
class Member:
    """The value changes of one file (``""``) or PAK entry (``"#%016X"``).  ``pending`` is why it could
    not be opened at the last build (its changes are kept but not applied)."""

    member: str = ""
    engine: str = ""
    label: str = ""
    changes: list[Change] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    pending: str = ""


@dataclass(frozen=True)
class Base:
    """What an output was built from: the effective source key (``<key>`` or ``matfix/<key>``), its
    size, mtime and sha1, and the sha1 of the stored file."""

    key: str
    size: int
    mtime_ns: int
    sha1: str
    stored_sha1: str = ""


@dataclass(frozen=True)
class Out:
    file: str  # "edits/NNNN_<basename>", relative to the mod's folder
    size: int
    sha1: str


@dataclass
class Item:
    source: Source
    exclude: bool = False
    members: list[Member] = field(default_factory=list)
    base: Base | None = None
    out: Out | None = None
    conflict: str = ""  # the option or the file is gone after an update: the whole item is inactive

    def member(self, name: str) -> Member | None:
        return next((m for m in self.members if m.member == name), None)

    def changes(self) -> int:
        return sum(len(m.changes) for m in self.members)

    def conflicts(self) -> int:
        return sum(len(m.conflicts) for m in self.members)


@dataclass
class Record:
    format: int = EDITS_FORMAT
    rev: int = 0
    next: int = 1  # the output counter; numbers are never reused
    items: list[Item] = field(default_factory=list)
    notice: dict = field(default_factory=dict)  # the last carry-over report, shown once
    error: str = ""  # unreadable: never applied, never written

    @property
    def newer(self) -> bool:
        """Made by a newer version of the manager (the error says so too)."""
        return self.format > EDITS_FORMAT

    def item(self, source: Source) -> Item | None:
        return next((i for i in self.items if i.source == source), None)

    def empty(self) -> bool:
        return not self.items and not self.notice and not self.error

    def copy(self) -> "Record":
        return copy.deepcopy(self)

    def outputs(self) -> set[str]:
        """The output files (``edits/...``) the record refers to."""
        return {i.out.file for i in self.items if i.out is not None}

    def to_dict(self) -> dict:
        return {"format": self.format, "rev": self.rev, "next": self.next,
                "items": [_item_dict(i) for i in self.items], "notice": self.notice}

    @classmethod
    def from_dict(cls, data) -> "Record":
        """The record in ``data`` (parsed JSON); any shape error gives ``Record(error=...)``, never a
        record with the bad parts left out."""
        if not isinstance(data, dict):
            return cls(error="기록이 JSON 객체가 아닙니다")
        fmt = data.get("format")
        if not _is_int(fmt) or fmt < 1:
            return cls(error="기록의 형식 번호가 없습니다")
        if fmt > EDITS_FORMAT:
            return cls(format=fmt, error=f"더 새로운 버전의 매니저에서 만든 기록입니다 (형식 {fmt})")
        try:
            rev, nxt = data.get("rev", 0), data.get("next", 1)
            _need(_is_int(rev) and rev >= 0, "rev")
            _need(_is_int(nxt) and nxt >= 1, "next")
            notice = data.get("notice", {})
            _need(isinstance(notice, dict), "notice")
            raw_items = data.get("items", [])
            _need(isinstance(raw_items, list), "items")
            items, seen = [], set()
            for n, raw in enumerate(raw_items, 1):
                try:
                    item = _item_from(raw)
                except _Shape as exc:
                    raise _Shape(f"항목 {n}: {exc}") from None
                key = (item.source.option, item.source.n, item.source.rel)
                _need(key not in seen, f"항목 {n}: 같은 파일이 두 번 적혀 있음")
                seen.add(key)
                items.append(item)
        except _Shape as exc:
            return cls(error=f"기록의 내용이 맞지 않습니다 ({exc})")
        return cls(format=fmt, rev=rev, next=nxt, items=items, notice=notice)


class _Shape(Exception):
    pass


def _need(ok, what: str) -> None:
    if not ok:
        raise _Shape(what)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str(data: dict, key: str, default: str | None = None) -> str:
    value = data.get(key, default)
    _need(isinstance(value, str), key)
    return value


def _int(data: dict, key: str, default: int | None = None, low: int | None = 0) -> int:
    value = data.get(key, default)
    _need(_is_int(value) and (low is None or value >= low), key)
    return value


def _safe_rel(rel: str) -> bool:
    if not rel or "\\" in rel or rel.startswith("/") or ":" in rel.split("/", 1)[0]:
        return False
    return all(part not in ("", ".", "..") for part in rel.split("/"))


def _change_from(raw) -> Change:
    _need(isinstance(raw, dict), "값")
    at = _str(raw, "at")
    _need(at, "at")
    how = raw.get("how")
    _need(how is None or isinstance(how, dict), "how")
    return Change(at=at, kind=_str(raw, "kind", ""), before=_str(raw, "before"), after=_str(raw, "after"),
                  label=_str(raw, "label", ""), raw=_str(raw, "raw", ""), count=_int(raw, "count", 0), how=how)


def _change_dict(c: Change) -> dict:
    out = {"at": c.at, "kind": c.kind, "before": c.before, "after": c.after, "label": c.label}
    if c.raw:
        out["raw"] = c.raw
    if c.count or c.rule:
        out["count"] = c.count
    if c.how is not None:
        out["how"] = c.how
    return out


def _conflict_from(raw) -> Conflict:
    change = _change_from(raw)
    now = raw.get("now")
    _need(now is None or isinstance(now, str), "now")
    why = _str(raw, "why")
    _need(why in WHYS, "why")
    return Conflict(change, now, why, _str(raw, "note", ""))


def _conflict_dict(c: Conflict) -> dict:
    out = _change_dict(c.change)
    out["now"] = c.now
    out["why"] = c.why
    if c.note:
        out["note"] = c.note
    return out


def _member_from(raw) -> Member:
    _need(isinstance(raw, dict), "파일 안 항목")
    engine = _str(raw, "engine")
    _need(engine, "engine")
    changes, conflicts = raw.get("changes", []), raw.get("conflicts", [])
    _need(isinstance(changes, list) and isinstance(conflicts, list), "changes")
    return Member(member=_str(raw, "member", ""), engine=engine, label=_str(raw, "label", ""),
                  changes=[_change_from(c) for c in changes], conflicts=[_conflict_from(c) for c in conflicts],
                  pending=_str(raw, "pending", ""))


def _member_dict(m: Member) -> dict:
    return {"member": m.member, "engine": m.engine, "label": m.label,
            "changes": [_change_dict(c) for c in m.changes],
            "conflicts": [_conflict_dict(c) for c in m.conflicts], "pending": m.pending}


def _base_from(raw) -> Base | None:
    if raw is None:
        return None
    _need(isinstance(raw, dict), "base")
    key = _str(raw, "key")
    _need(_safe_rel(key), "base.key")
    return Base(key=key, size=_int(raw, "size"), mtime_ns=_int(raw, "mtime_ns", low=None), sha1=_str(raw, "sha1"),
                stored_sha1=_str(raw, "stored_sha1", ""))


def _out_from(raw) -> Out | None:
    if raw is None:
        return None
    _need(isinstance(raw, dict), "out")
    name = _str(raw, "file")
    _need(OUT_RE.fullmatch(name) is not None, "out.file")
    return Out(file=name, size=_int(raw, "size"), sha1=_str(raw, "sha1"))


def _item_from(raw) -> Item:
    _need(isinstance(raw, dict), "항목이 객체가 아님")
    rel = _str(raw, "rel")
    _need(_safe_rel(rel), "rel")
    exclude = raw.get("exclude", False)
    _need(isinstance(exclude, bool), "exclude")
    members = raw.get("members", [])
    _need(isinstance(members, list), "members")
    return Item(source=Source(_str(raw, "option", ""), _int(raw, "option_n", 0), rel), exclude=exclude,
                members=[_member_from(m) for m in members], base=_base_from(raw.get("base")),
                out=_out_from(raw.get("out")), conflict=_str(raw, "conflict", ""))


def _item_dict(item: Item) -> dict:
    out = {"option": item.source.option, "option_n": item.source.n, "rel": item.source.rel,
           "exclude": item.exclude}
    if item.members:
        out["members"] = [_member_dict(m) for m in item.members]
        out["base"] = None if item.base is None else {
            "key": item.base.key, "size": item.base.size, "mtime_ns": item.base.mtime_ns,
            "sha1": item.base.sha1, "stored_sha1": item.base.stored_sha1}
        out["out"] = None if item.out is None else {"file": item.out.file, "size": item.out.size,
                                                    "sha1": item.out.sha1}
    if item.members or item.conflict:
        out["conflict"] = item.conflict
    return out


# ---- files -----------------------------------------------------------------------------------------
def load(mod_dir: Path) -> Record:
    """The mod's record; an empty Record when there is none.  An unreadable one comes back with
    ``error`` and is left where it is (only ``set_aside`` moves it)."""
    path = Path(mod_dir) / EDITS_RECORD
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Record()
    except OSError as exc:
        return Record(error=f"기록을 읽지 못했습니다 ({exc})")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return Record(error=f"기록이 손상되었습니다 ({exc})")
    return Record.from_dict(data)


def save(mod_dir: Path, record: Record) -> None:
    """Write the record atomically and read it back; an empty record deletes the file.  An unreadable
    record is never written."""
    if record.error:
        raise EditError(f"읽지 못한 수정 기록은 저장하지 않습니다: {record.error}")
    path = Path(mod_dir) / EDITS_RECORD
    if record.empty():
        fsutil.remove_file(path)
        return
    data = record.to_dict()
    fsutil.atomic_write_json(path, data)
    back = load(mod_dir)
    if back.error or back.to_dict() != data:
        raise EditError(f"수정 기록을 저장한 뒤 다시 읽어 보니 달랐습니다{f': {back.error}' if back.error else ''}")


def set_aside(mod_dir: Path) -> Path | None:
    """Move ``edits.json`` to ``edits.json.bad-<YYYYMMDD-HHMMSS>`` (기록 지우기); None when there is none."""
    path = Path(mod_dir) / EDITS_RECORD
    if not path.exists():
        return None
    stem = f"{EDITS_RECORD}{BAD_SUFFIX}{time.strftime('%Y%m%d-%H%M%S')}"
    target, n = path.with_name(stem), 1
    while target.exists():
        n += 1
        target = path.with_name(f"{stem}-{n}")
    os.replace(path, target)
    log.warning("모드 수정 기록을 %s(으)로 옮겼습니다", target)
    return target


def number_from(mod_dir: Path, record: Record) -> int:
    """The first output number to use: the record's counter, but past every file already in ``edits/``
    (a deleted or set-aside record must not make a number come back while an old output still
    exists)."""
    top = max(record.next, 1)
    for out in record.outputs():
        m = OUT_RE.fullmatch(out)
        top = max(top, int(m.group(1)) + 1)
    try:
        names = os.listdir(Path(mod_dir) / EDITS_DIR)
    except OSError:
        names = []
    for name in names:
        m = re.match(r"(\d+)_", name)
        if m:
            top = max(top, int(m.group(1)) + 1)
    return top


def output_name(number: int, rel: str) -> str:
    """``edits/NNNN_<basename of rel>``."""
    return f"{EDITS_DIR}/{number:04d}_{rel.rsplit('/', 1)[-1]}"


# ---- rules -----------------------------------------------------------------------------------------
def config_engine(rel: str) -> str | None:
    """The config engine textconfig lists ``rel`` under (None: not a script settings file)."""
    from .textconfig import listed

    return listed(rel)


def engine_tool(name: str) -> str | None:
    """"data" or "config" for an engine name; None when unknown (fakes registered in tests count)."""
    if name in DATA_ENGINES:
        return "data"
    if name in CONFIG_ENGINES:
        return "config"
    from . import engines

    eng = engines.registered(name)
    return None if eng is None else eng.tool


def is_data_rel(rel: str) -> bool:
    """A loose .user data file under natives/."""
    return rel.lower().startswith("natives/") and DATA_RE.search(rel) is not None


def member_name(rel: str, engine: str, member: str) -> str:
    """The canonical member name (PAK entries upper case), or EditError when the engine may not be
    used on that file (§2.2)."""
    tool = engine_tool(engine)
    if tool is None:
        raise EditError(f"알 수 없는 수정 종류입니다: {engine}")
    if tool == "data":
        if is_data_rel(rel):
            if member:
                raise EditError(f"파일 안 항목이 맞지 않습니다: {rel} {member}")
            return ""
        if is_root_pak(rel):
            if PAK_MEMBER_RE.fullmatch(member) is None:
                raise EditError(f"PAK 안 항목이 맞지 않습니다: {rel} {member}")
            return member.upper()
        raise EditError(f"데이터 값은 natives 폴더의 .user 파일이나 모드의 .pak 안에서만 바꿀 수 있습니다: {rel}")
    listed = config_engine(rel)
    if listed is None or (engine in CONFIG_ENGINES and listed != engine):
        raise EditError(f"스크립트 설정으로 고칠 수 없는 파일입니다: {rel}")
    if member:
        raise EditError(f"파일 안 항목이 맞지 않습니다: {rel} {member}")
    return ""


def normalize(record: Record) -> Record:
    """A copy of ``record`` that follows the rules of §2.2: one item per file, members only where their
    engine may be used, one change per value (rules per value and matched value), no empty members or
    items.  Raises EditError on a violation."""
    if record.error:
        raise EditError(record.error)
    out = Record(format=EDITS_FORMAT, rev=record.rev, next=max(record.next, 1),
                 notice=copy.deepcopy(record.notice))
    seen = set()
    for item in record.items:
        src = item.source
        if not _safe_rel(src.rel) or src.n < 0 or (not src.option and src.n):
            raise EditError(f"파일 경로가 맞지 않습니다: {src.label()}")
        key = (src.option, src.n, src.rel)
        if key in seen:
            raise EditError(f"같은 파일의 수정이 두 번 적혀 있습니다: {src.label()}")
        seen.add(key)
        members, names = [], set()
        for m in item.members:
            name = member_name(src.rel, m.engine, m.member)
            if name in names:
                raise EditError(f"같은 파일 안 항목의 수정이 두 번 적혀 있습니다: {src.label()} {name}")
            names.add(name)
            idents = set()
            for c in [*m.changes, *(x.change for x in m.conflicts)]:
                if not c.at:
                    raise EditError(f"값의 주소가 비어 있습니다: {src.label()}")
                if c.rule and engine_tool(m.engine) != "data":
                    raise EditError(f"규칙은 데이터 파일에서만 쓸 수 있습니다: {c.at}")
                if c.ident in idents:
                    raise EditError(f"같은 값의 수정이 두 번 적혀 있습니다: {src.label()} {c.at}")
                idents.add(c.ident)
            for x in m.conflicts:
                if x.why not in WHYS:
                    raise EditError(f"알 수 없는 확인 사유입니다: {x.why}")
            if m.changes or m.conflicts:
                members.append(Member(name, m.engine, m.label, list(m.changes), list(m.conflicts), m.pending))
        if not item.exclude and not members:
            continue
        keep = bool(members)
        out.items.append(Item(src, item.exclude, members, item.base if keep else None, item.out if keep else None,
                              item.conflict))
    return out


def merge(record: Record, plan: "EditPlan") -> Record:
    """The record ``plan`` asks for, normalized (EditError on a violation): reset or clear_bad start
    from an empty record (keeping rev and the output counter of a readable one), then items are
    dropped, exclusions set and members replaced by the plan's lists."""
    if plan.reset:
        out = Record() if record.error else Record(rev=record.rev, next=record.next)
    elif record.error:
        if not plan.clear_bad:
            raise EditError(record.error)
        out = Record()
    else:
        out = record.copy()
    out.items = [i for i in out.items if i.source not in plan.drop]
    for source, flag in plan.exclude.items():
        item = out.item(source)
        if item is None:
            item = Item(source)
            out.items.append(item)
        item.exclude = bool(flag)
    for (source, name), mp in plan.members.items():
        item = out.item(source)
        if item is None:
            item = Item(source)
            out.items.append(item)
        m = item.member(name)
        if m is None:
            m = Member(member=name)
            item.members.append(m)
        m.engine, m.label = mp.engine, mp.label or m.label
        m.changes, m.conflicts = list(mp.changes), list(mp.conflicts)
        if not mp.changes and not mp.conflicts:
            item.members.remove(m)
    return normalize(out)


# ---- option scopes ---------------------------------------------------------------------------------
def resolve_option(options: list[dict], name: str, n: int) -> str | None:
    """Today's option id for a scope stored by name and n; ``""`` for a mod without options; None when
    the scope does not resolve (the option is gone, or the mod gained or lost its options)."""
    if not options:
        return "" if not name and not n else None
    if not name:
        return None
    ids = [o.get("id") for o in options if o.get("name") == name]
    return ids[n] if 0 <= n < len(ids) else None


def option_ref(options: list[dict], option_id: str) -> tuple[str, int]:
    """(name, n) of an option id (``("", 0)`` for ``""``); EditError when there is no such option."""
    if not option_id:
        return "", 0
    seen: dict[str, int] = {}
    for o in options:
        name = o.get("name", "")
        if o.get("id") == option_id:
            return name, seen.get(name, 0)
        seen[name] = seen.get(name, 0) + 1
    raise EditError(f"옵션을 찾지 못했습니다: {option_id}")


def key_of(options: list[dict], source: Source) -> str | None:
    """The source key (``rel`` or ``<option id>/<rel>``) of a stored file; None when its scope does not
    resolve."""
    oid = resolve_option(options, source.option, source.n)
    if oid is None:
        return None
    return f"{oid}/{source.rel}" if oid else source.rel


def source_of(options: list[dict], key: str) -> Source:
    """The Source of a source key (``edits/`` and ``matfix/`` prefixes are not accepted here)."""
    if not options:
        return Source("", 0, key)
    oid, _, rel = key.partition("/")
    name, n = option_ref(options, oid)
    return Source(name, n, rel)


def exclusion_digest(keys: Iterable[str]) -> str:
    """The first 16 hex characters of the sha1 of the sorted excluded source keys; "" for none (stored
    in the matfix record: a different set makes the mod 'unchecked')."""
    keys = sorted(set(keys))
    if not keys:
        return ""
    return hashlib.sha1("\n".join(keys).encode("utf-8")).hexdigest()[:16]


# ---- plans and the window's unsaved changes ----------------------------------------------------------
@dataclass(frozen=True)
class MemberPlan:
    engine: str
    label: str
    changes: tuple[Change, ...]
    conflicts: tuple[Conflict, ...] = ()

    @classmethod
    def of(cls, member: Member) -> "MemberPlan":
        return cls(member.engine, member.label, tuple(member.changes), tuple(member.conflicts))

    def same(self, other: "MemberPlan | None") -> bool:
        """The same values (labels and ``how`` aside)."""
        if other is None:
            return not self.changes and not self.conflicts
        return self.changes == other.changes and self.conflicts == other.conflicts


@dataclass(frozen=True)
class EditPlan:
    """What the window (or the CLI) asks for, relative to the record at ``rev``.  ``members`` hold the
    whole new lists of a member (empty lists remove it)."""

    mod_id: str
    stamp: str
    rev: int
    exclude: Mapping[Source, bool] = field(default_factory=dict)  # True: leave out, False: put back
    members: Mapping[tuple[Source, str], MemberPlan] = field(default_factory=dict)
    reapply: frozenset[Source] = frozenset()
    drop: frozenset[Source] = frozenset()  # remove the whole item (원래대로 on a file, 지우기 on an item conflict)
    reset: bool = False  # 모두 원래대로 (implies clear_bad)
    clear_bad: bool = False  # 기록 지우기

    def empty(self) -> bool:
        return not self.count()

    def count(self) -> int:
        """Entries of the plan: an exclusion, a member's new list, a re-apply or a drop each count one
        (Pending.count counts single values instead)."""
        return (len(self.exclude) + len(self.members) + len(self.reapply) + len(self.drop)
                + int(self.reset) + int(self.clear_bad and not self.reset))

    def sources(self) -> set[Source]:
        """Every file the plan touches (a reset touches all, which this does not list)."""
        return {*self.exclude, *(s for s, _m in self.members), *self.reapply, *self.drop}


def _rule_of(at: str) -> str:
    """The rule address of a row value (the last array selector replaced by ``*``)."""
    from .rszdata import rule_of

    return rule_of(at)


class Pending:
    """The window's unsaved changes on top of a record (pure: tested without Tk).  Every action keeps
    the plan minimal: an entry equal to what the record already holds is removed."""

    def __init__(self, record: Record):
        self.record = record.copy()
        self._exclude: dict[Source, bool] = {}
        self._members: dict[tuple[Source, str], MemberPlan] = {}
        self._reapply: set[Source] = set()
        self._drop: set[Source] = set()
        self._reset = False
        self._clear_bad = False

    # -- the state the record would have after saving
    def _base_item(self, source: Source) -> Item | None:
        """The record's item, unless the plan removes it."""
        if self._reset or self.record.error or source in self._drop:
            return None
        return self.record.item(source)

    def _base_member(self, source: Source, member: str) -> MemberPlan | None:
        item = self._base_item(source)
        m = None if item is None else item.member(member)
        return None if m is None else MemberPlan.of(m)

    def excluded(self, source: Source) -> bool:
        """Whether the file is left out after saving."""
        if source in self._exclude:
            return self._exclude[source]
        item = self._base_item(source)
        return bool(item and item.exclude)

    def member(self, source: Source, member: str) -> MemberPlan | None:
        """The member's values after saving: the pending lists, else the record's."""
        if (source, member) in self._members:
            mp = self._members[(source, member)]
            return mp if mp.changes or mp.conflicts else None
        return self._base_member(source, member)

    def touched(self, source: Source) -> bool:
        """Whether the plan changes anything of this file."""
        return (source in self._exclude or source in self._reapply or source in self._drop
                or any(s == source for s, _m in self._members))

    def _put(self, source: Source, member: str, mp: MemberPlan) -> None:
        if mp.same(self._base_member(source, member)):
            self._members.pop((source, member), None)
        else:
            self._members[(source, member)] = mp

    def _current(self, source: Source, member: str, engine: str = "", label: str = "") -> MemberPlan:
        mp = self.member(source, member)
        if mp is None:
            mp = MemberPlan(engine, label, ())
        return mp

    # -- actions
    def exclude(self, source: Source, flag: bool) -> None:
        item = self._base_item(source)
        if bool(item and item.exclude) == bool(flag):
            self._exclude.pop(source, None)
        else:
            self._exclude[source] = bool(flag)

    def set_value(self, source: Source, member: str, engine: str, label: str, *, at: str, kind: str,
                  before: str, after: str, how: dict | None = None, rule: bool = False, raw: str = "",
                  name: str = "") -> None:
        """Set one value (``at`` of the row, ``before`` its value in the file now), or with ``rule`` every
        row of the table whose value is ``before`` (``at`` is then the rule address, Value.rule_at).
        A single value set back to ``before`` removes the change; a value with a conflict loses the
        conflict.  Setting a rule removes the single changes it covers.  ``name`` is the value's label."""
        if rule and "[*]" not in at:
            raise EditError(f"규칙 주소가 아닙니다: {at}")
        if not rule and "[*]" in at:
            raise EditError(f"한 값의 주소가 아닙니다: {at}")
        cur = self._current(source, member, engine, label)
        new = Change(at=at, kind=kind, before=before, after=after, label=name or _last_name(at), raw=raw, how=how)
        changes, placed = [], False
        for c in cur.changes:
            if c.ident == new.ident:
                if after != before:
                    changes.append(new)  # in the record's place (rules apply in record order)
                placed = True
            elif not (rule and not c.rule and c.before == before and _rule_of(c.at) == at):
                changes.append(c)
        if not placed and after != before:
            changes.append(new)
        conflicts = [x for x in cur.conflicts if x.ident != new.ident]
        self._put(source, member, MemberPlan(engine or cur.engine, label or cur.label, tuple(changes),
                                             tuple(conflicts)))

    def revert_value(self, source: Source, member: str, at: str, before: str | None = None) -> None:
        """원래 값으로: remove the change (or conflict) of ``at``.  A rule address removes that rule (all
        rules of the address unless ``before`` names one); a row without a change of its own reverts
        the rule that covers it."""
        cur = self.member(source, member)
        if cur is None:
            return

        def hit(c: Change) -> bool:
            if "[*]" in at:
                return c.rule and c.at == at and (before is None or c.before == before)
            return not c.rule and c.at == at

        changes = [c for c in cur.changes if not hit(c)]
        conflicts = [x for x in cur.conflicts if not hit(x.change)]
        if len(changes) == len(cur.changes) and len(conflicts) == len(cur.conflicts) and "[*]" not in at:
            if any(c.rule for c in (*cur.changes, *(x.change for x in cur.conflicts))):
                rule_at = _rule_of(at)

                def covers(c: Change) -> bool:
                    return c.rule and c.at == rule_at and (before is None or c.before == before)

                changes = [c for c in changes if not covers(c)]
                conflicts = [x for x in conflicts if not covers(x.change)]
        self._put(source, member, MemberPlan(cur.engine, cur.label, tuple(changes), tuple(conflicts)))

    def _conflict(self, source: Source, member: str, at: str, before: str | None) -> tuple[MemberPlan, Conflict]:
        cur = self.member(source, member)
        for x in (cur.conflicts if cur else ()):
            if x.change.at == at and (before is None or not x.change.rule or x.change.before == before):
                return cur, x
        raise EditError(f"확인할 값을 찾지 못했습니다: {at}")

    def keep_mine(self, source: Source, member: str, at: str, before: str | None = None) -> None:
        """내 값 유지: the conflict becomes a change from the author's new value to mine."""
        cur, x = self._conflict(source, member, at, before)
        if x.why != "changed" or x.now is None:
            raise EditError("이 값은 지금 파일에서 바꿀 수 없어 내 값을 유지할 수 없습니다. 작성자 값을 쓰거나 지우세요.")
        mine = replace(x.change, before=x.now, raw="", count=0)
        conflicts = tuple(c for c in cur.conflicts if c is not x)
        changes = [c for c in cur.changes if c.ident != mine.ident]
        if mine.after != mine.before:
            changes.append(mine)
        self._put(source, member, MemberPlan(cur.engine, cur.label, tuple(changes), conflicts))

    def take_author(self, source: Source, member: str, at: str, before: str | None = None) -> None:
        """작성자 값 쓰기 (or 지우기 on a gone value): the conflict is removed."""
        cur, x = self._conflict(source, member, at, before)
        conflicts = tuple(c for c in cur.conflicts if c is not x)
        self._put(source, member, MemberPlan(cur.engine, cur.label, cur.changes, conflicts))

    def reapply(self, source: Source) -> None:
        self._reapply.add(source)

    def drop(self, source: Source) -> None:
        """원래대로 on a whole file (or 지우기 on an item conflict): the item goes, with its pending entries."""
        self._exclude.pop(source, None)
        self._reapply.discard(source)
        for key in [k for k in self._members if k[0] == source]:
            del self._members[key]
        if self.record.item(source) is not None and not self._reset and not self.record.error:
            self._drop.add(source)

    def reset_all(self) -> None:
        """모두 원래대로 (also clears an unreadable record)."""
        self._exclude.clear()
        self._members.clear()
        self._reapply.clear()
        self._drop.clear()
        self._reset = True
        self._clear_bad = False

    def clear_bad(self) -> None:
        """기록 지우기 (only for an unreadable record: nothing to do otherwise)."""
        if self.record.error and not self._reset:
            self._clear_bad = True

    def count(self) -> int:
        """Unsaved changes as the footer counts them: each value set or reverted, each file left out or
        put back, each re-apply or file reverted, and a reset or 기록 지우기."""
        n = len(self._exclude) + len(self._reapply) + len(self._drop) + int(self._reset) + int(self._clear_bad)
        for (source, member), mp in self._members.items():
            old, new = _values(self._base_member(source, member)), _values(mp)
            n += max(1, sum(1 for k in old.keys() | new.keys() if old.get(k) != new.get(k)))
        return n

    def plan(self, mod_id: str, stamp: str, rev: int) -> EditPlan:
        return EditPlan(mod_id=mod_id, stamp=stamp, rev=rev, exclude=dict(self._exclude),
                        members=dict(self._members), reapply=frozenset(self._reapply), drop=frozenset(self._drop),
                        reset=self._reset, clear_bad=self._clear_bad)

    @classmethod
    def from_plan(cls, record: Record, plan: EditPlan) -> "Pending":
        """The window's changes again after a failed save."""
        p = cls(record)
        p._reset = plan.reset
        p._clear_bad = plan.clear_bad and not plan.reset and bool(record.error)
        p._drop = {s for s in plan.drop if record.item(s) is not None and not plan.reset and not record.error}
        for source, flag in plan.exclude.items():
            p.exclude(source, flag)
        for (source, member), mp in plan.members.items():
            p._put(source, member, mp)
        p._reapply = set(plan.reapply)
        return p


def _values(mp: MemberPlan | None) -> dict:
    """ident → the change or conflict of a member (for counting what differs)."""
    if mp is None:
        return {}
    return {**{c.ident: c for c in mp.changes}, **{x.ident: x for x in mp.conflicts}}


def _last_name(at: str) -> str:
    """The field name at the end of an address (``_Values[_ItemId=101]._SellPrice`` → ``_SellPrice``)."""
    depth, quoted, escaped, cut = 0, False, False, 0
    for i, ch in enumerate(at):
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "." and depth == 0:
            cut = i + 1
    name = at[cut:]
    return name.split("[", 1)[0] or name


# ---- snapshots and views (filled by the manager, read by pages and CLI) ------------------------------
@dataclass(frozen=True)
class OptionRef:
    id: str
    name: str
    n: int
    selected: bool


@dataclass(frozen=True)
class BaseRef:
    """The effective base of a stored file: the matfix repair in use ("repair") or the stored file."""

    path: Path
    key: str  # effective source key: <key> or matfix/<key>
    via: str  # "stored" | "repair"
    rel: str  # the game path it is placed under (a repair may have renamed the version number)


@dataclass(frozen=True)
class EditFile:
    """A row of 파일 고르기."""

    source: Source
    option_id: str | None
    key: str
    rel: str
    path: Path
    size: int
    kind: str  # 텍스처 | 재질 | 모델 | 데이터 | 프리팹 | 이펙트 | 소리 | 글 | 스크립트 | 설정 | 플러그인 | PAK | 기타
    state: str  # in | excluded | overridden | option_off | conflict | needs_option
    overridden_by: str = ""  # option name whose copy wins this path
    overlap: tuple[str, ...] = ()  # selected option names having this path (≥2: 쓸 옵션 offered)
    values: int = 0  # value changes on this file
    repaired: bool = False
    placed: str = "none"  # Deployer.placed state
    twin: str = ""  # its streaming twin's rel when stored in the same scope


@dataclass
class EditSnapshot:
    """Taken on the GUI thread; workers read only this."""

    mod_id: str
    name: str
    enabled: bool
    stamp: str
    rev: int
    options: list[OptionRef]
    files: list[EditFile]
    record: Record  # deep copy
    bases: dict[str, BaseRef]  # source key → effective base
    placed: dict[str, str]  # source key → Deployer.placed state (the deployed copy's scope only)
    stuck: dict[str, str]  # source key → matfix stuck reason
    overlaps: dict[str, str]  # path_key → name of another mod that wins it
    pending: frozenset[str]  # source keys whose items need a build
    issues: list[str]
    game_dir: Path | None
    aliases: dict  # Mod.retarget
    data_dir: Path


@dataclass(frozen=True)
class EditTarget:
    """A file or PAK member a value page can open."""

    source: Source
    key: str
    member: str
    engine: str
    tool: str  # "data" | "config"
    label: str
    size: int
    edited: bool
    readonly: str = ""
    note: str = ""


@dataclass(frozen=True)
class ValueRow:
    value: "Value"
    base: str  # the value in the effective base
    game: str | None  # the game's value (paired leaves only)
    hint: str = ""
    mine: Change | None = None
    conflict: Conflict | None = None
    rule: Change | None = None  # the rule that covers this row


@dataclass
class ValueView:
    target: EditTarget
    rows: list[ValueRow]
    total: int
    compared: bool
    truncated: bool
    notes: list[str]
    readonly: str
    base: BaseRef
    data_files: list[str] = field(default_factory=list)
    added: frozenset[str] = frozenset()  # groups (rows) of the file the game's copy lacks (data files, compared)
    # values (Value.at) whose game value comes from rows paired by a key unique in both files, or from no
    # table (rszdata.GameValues.keyed): 게임 값의 몇 배 / 모드가 바꾼 만큼의 몇 배 use only these
    keyed: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EditItemView:
    """A line of 수정 내용 and of CLI ``edit status``."""

    kind: str  # "exclude" | "data" | "config"
    source: Source
    member: str
    label: str
    changes: tuple[Change, ...]
    conflicts: tuple[Conflict, ...]
    state: str  # active | inactive_option | inactive_excluded | conflict | pending | waiting | item_conflict
    why: str = ""


@dataclass(frozen=True)
class EditSummary:
    excluded: int
    values: int
    files: int
    conflicts: int
    pending: int
    waiting: int
    error: str

    @property
    def empty(self) -> bool:
        return not (self.excluded or self.values or self.conflicts or self.pending or self.waiting or self.error)

    def text(self) -> str:
        """"뺀 파일 2개 · 바꾼 값 5개 (파일 1개)" (+ " · 확인할 값 1개")."""
        if self.error:
            return f"모드 수정 기록을 읽지 못했습니다: {self.error}"
        parts = []
        if self.excluded:
            parts.append(f"뺀 파일 {self.excluded}개")
        if self.values:
            parts.append(f"바꾼 값 {self.values}개 (파일 {self.files}개)")
        if self.conflicts:
            parts.append(f"확인할 값 {self.conflicts}개")
        if self.pending:
            parts.append(f"다시 적용하지 못한 파일 {self.pending}개")
        if self.waiting:
            parts.append(f"다시 적용 대기 {self.waiting}개")
        return " · ".join(parts) or "이 모드에 한 수정이 없습니다."

    def problem(self) -> str:
        """"" or the first thing that needs the user."""
        if self.error:
            return f"모드 수정 기록을 읽지 못해 쓰지 않고 있습니다: {self.error}"
        if self.conflicts:
            return f"확인할 값 {self.conflicts}개"
        if self.pending:
            return f"다시 적용하지 못한 파일 {self.pending}개"
        if self.waiting:
            return f"다시 적용 대기 {self.waiting}개"
        return ""


@dataclass
class CarryReport:
    """What an update did to the edits: ``kept`` edits carried as they were (exclusions, outputs
    whose file did not change), ``reapplied`` values applied again on a changed file, ``conflicts``
    values left to check, ``pending`` files that could not be opened, ``dropped`` exclusions of files
    the update removed, ``error`` why nothing was applied (the old record could not be read)."""

    kept: int = 0
    reapplied: int = 0
    conflicts: int = 0
    pending: int = 0
    dropped: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def problems(self) -> int:
        return self.conflicts + self.pending + len(self.dropped) + int(bool(self.error))

    def text(self) -> str:
        """"수정을 다시 적용했습니다: 수정 5개 · 확인할 값 2개 · 없어진 파일 1개" (or the error)."""
        if self.error:
            return self.error
        parts = [f"수정 {self.kept + self.reapplied}개"]
        if self.conflicts:
            parts.append(f"확인할 값 {self.conflicts}개")
        if self.pending:
            parts.append(f"다시 적용하지 못한 파일 {self.pending}개")
        if self.dropped:
            parts.append(f"없어진 파일 {len(self.dropped)}개")
        return "수정을 다시 적용했습니다: " + " · ".join(parts)

    def notice(self, at: str) -> dict:
        """The record's ``notice`` for this report (``at``: local time, ISO seconds)."""
        notice = {"kind": "update", "at": at, "kept": self.kept, "reapplied": self.reapplied,
                  "conflicts": self.conflicts, "pending": self.pending, "dropped": list(self.dropped)}
        if self.error:
            notice["error"] = self.error
        return notice

    @classmethod
    def from_notice(cls, notice: Mapping) -> "CarryReport":
        def num(key):
            v = notice.get(key, 0)
            return v if _is_int(v) and v >= 0 else 0

        dropped, error = notice.get("dropped", []), notice.get("error", "")
        return cls(kept=num("kept"), reapplied=num("reapplied"), conflicts=num("conflicts"), pending=num("pending"),
                   dropped=[d for d in dropped if isinstance(d, str)] if isinstance(dropped, list) else [],
                   error=error if isinstance(error, str) else "")

