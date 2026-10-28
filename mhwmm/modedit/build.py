"""Building one item's output into a temporary stage (worker thread).

``build_item`` reads the item's effective base (the matfix repair in use, else the stored file),
re-applies each member's changes under the rebase rule (engines.apply) and writes the result as
``<stage>/edits/NNNN_<name>``, read back before it is used.  Three modes:

- ``commit``: the user's own save.  A member this save adds or changes values of must open and its
  output must be written (else the save stops), and a change of this save that ends as a conflict
  raises (the file changed while the window was open).  Values it only takes away (원래 값으로, I9)
  never stop it: a member it sets no value of waits like in ``reapply`` when it cannot be opened, and
  when the output cannot be written (no room, it did not read back) and the save sets no value of the
  item, every member waits with no output (the game gets the base until the item is built again).
- ``reapply``: after the base changed (a repair accepted or declined, a hand edit).  A member that
  cannot be opened keeps its changes and gets ``pending`` with the reason; nothing is raised.
- ``carry``: the same, on an update's new stored file.

The item that comes back holds the new ``base``, ``out`` (None when there was nothing to write) and
the members as built: changes kept (rules with their row count, every address spelled the way the
new file names it), conflicts (the earlier ones judged again on the new file, then the new ones),
``pending`` set or cleared.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .. import fsutil
from ..deploy import is_root_pak
from . import EDITS_DIR, EditError, engines, pakedit
from .model import Base, BaseRef, Conflict, Item, Member, Out, Record, output_name

log = logging.getLogger(__name__)

MODES = ("commit", "reapply", "carry")


@dataclass
class BuildInput:
    """One item to build, taken on the GUI thread."""

    index: int  # the item's index in the desired record
    item: Item  # the desired item (members with the plan's lists)
    key: str  # its source key (rel or <option id>/<rel>)
    rel: str  # the stored game path
    base: BaseRef  # the effective base
    stored_path: Path
    mode: str  # "commit" | "reapply" | "carry"
    new: frozenset[tuple[str, str]] = frozenset()  # (member, at) added or changed by this commit: must apply


@dataclass
class Built:
    index: int
    item: Item  # with base, out and members as built
    out_path: Path | None  # the staged output (None: nothing to write, the number was not used)
    notes: list[str] = field(default_factory=list)


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(fsutil.longpath(path), "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _base(inp: BuildInput) -> tuple[Base, bytes | None]:
    """The new Base (stat taken before reading, so a change while building shows in finish_edit) and,
    for a loose file, its bytes."""
    st = os.stat(fsutil.longpath(inp.base.path))
    data = None
    if is_root_pak(inp.rel):
        sha1 = _sha1(inp.base.path)
    else:
        with open(fsutil.longpath(inp.base.path), "rb") as f:
            data = f.read()
        sha1 = hashlib.sha1(data).hexdigest()
    same = fsutil.same_path(inp.base.path, inp.stored_path)
    stored = sha1 if same else _sha1(inp.stored_path)
    return Base(inp.base.key, st.st_size, st.st_mtime_ns, sha1, stored), data


def _label(member: Member, rel: str) -> str:
    return member.label or (f"{rel.rsplit('/', 1)[-1]} {member.member}" if member.member else rel.rsplit("/", 1)[-1])


def _apply(member: Member, data: bytes, rel: str) -> engines.Applied:
    """Re-apply ``member``'s changes on ``data`` and judge its conflicts again (an earlier update's
    conflict may hold another value now, or none: engines.apply); updates the member (kept changes,
    the conflicts still open then the new ones, pending cleared) and returns the result.  Errors
    propagate."""
    engine = engines.get(member.engine)
    result = engines.apply(engine, data, rel, member.changes, member.conflicts)
    member.changes = list(result.kept)
    member.conflicts = [*result.still, *result.conflicts]
    member.pending = ""
    return result


def _asks(inp: BuildInput, member: Member) -> bool:
    """Whether this save adds or changes a value of ``member``."""
    return any(name == member.member for name, _at in inp.new)


def _check_new(inp: BuildInput, member: Member, conflicts, renamed=None) -> None:
    """Commit mode: a change of this save that became a conflict means the file changed meanwhile
    (``conflicts``: the new ones, spelled as the file names them; ``renamed``: given → that)."""
    renamed = renamed or {}
    asked = {renamed.get(at, at) for name, at in inp.new if name == member.member}
    for x in conflicts:
        if x.change.at in asked:
            raise EditError(f"값이 그사이 바뀌었습니다: {x.change.label or x.change.at}. 창을 다시 열어 주세요.")


def _write(stage: Path, name: str, data: bytes) -> tuple[Path, str]:
    """Write a loose output and read it back (V6)."""
    path = stage / name
    fsutil.makedirs(path.parent)
    with open(fsutil.longpath(path), "wb") as f:
        f.write(data)
    sha1 = hashlib.sha1(data).hexdigest()
    if _sha1(path) != sha1:
        fsutil.remove_file(path)
        raise EditError("바꾼 파일을 확인하지 못해 저장하지 않았습니다: 다시 읽어 보니 내용이 다릅니다")
    return path, sha1


def build_item(inp: BuildInput, stage: Path, number: int, progress=None) -> Built:
    """Build ``inp`` into ``stage`` as output number ``number`` (see the module doc)."""
    if inp.mode not in MODES:
        raise ValueError(inp.mode)
    commit = inp.mode == "commit"
    item = copy.deepcopy(inp.item)
    fresh: dict[str, int] = {}  # member → its new conflicts
    notes: list[str] = []
    base, data = _base(inp)
    item.base, item.out = base, None
    rel = inp.base.rel or inp.rel
    name = output_name(number, inp.rel)

    def hold(member: Member, why: str, exc: Exception | None = None) -> None:
        """The member waits with ``why`` (its changes and conflicts as given); a commit that sets one of
        its values stops instead (with ``exc``)."""
        if commit and _asks(inp, member):
            raise exc if exc is not None else EditError(why)
        member.pending = why
        notes.append(f"{_label(member, inp.rel)}: {why}")

    def hold_all(why: str, exc: Exception | None = None) -> None:
        """Nothing could be written: every member back as it was given, pending (a commit that sets a
        value of one of them stops instead)."""
        item.members = copy.deepcopy(inp.item.members)
        fresh.clear()
        for member in item.members:
            hold(member, why, exc)

    def unwritten(exc: Exception) -> None:
        """The output could not be written: every member waits (hold_all).  An OSError (a full disk) of a
        re-apply or carry goes up as it is (the manager lets the item wait: _failed_build)."""
        if isinstance(exc, OSError):
            if not commit:
                raise exc
            hold_all(f"바꾼 파일을 저장하지 못했습니다: {exc}", exc)
        else:
            hold_all(str(exc), exc)

    out_path = sha1 = None
    if not is_root_pak(inp.rel):
        new = data
        for member in item.members:
            try:
                result = _apply(member, new, rel)
            except EditError as exc:  # StaleError too
                hold(member, str(exc), exc)
                continue
            new = result.data
            fresh[member.member] = len(result.conflicts)
            if commit:
                _check_new(inp, member, result.conflicts, result.renamed)
        if new != data:
            try:
                out_path, sha1 = _write(stage, name, new)
            except (EditError, OSError) as exc:
                unwritten(exc)
    else:
        why = pakedit.check_rebuildable(inp.base.path)
        replaced: dict[int, bytes] = {}
        if why:
            hold_all(f"이 PAK은 다시 만들 수 없는 형식이라 값을 바꿀 수 없습니다 ({why})")
        else:
            for member in item.members:
                h = pakedit.member_hash(member.member)
                try:
                    entry = pakedit.read_member(inp.base.path, h)
                except KeyError:  # the entry is gone: every change of it is a conflict
                    gone = [Conflict(c, None, "gone") for c in member.changes]
                    member.conflicts = [*(Conflict(x.change, None, "gone") for x in member.conflicts), *gone]
                    member.changes = []
                    member.pending = ""
                    fresh[member.member] = len(gone)
                    if commit:
                        _check_new(inp, member, gone)
                    continue
                except EditError as exc:
                    hold(member, str(exc), exc)
                    continue
                try:
                    result = _apply(member, entry, f"{rel}#{h:016X}")
                except EditError as exc:
                    hold(member, str(exc), exc)
                    continue
                if result.data != entry:
                    replaced[h] = result.data
                fresh[member.member] = len(result.conflicts)
                if commit:
                    _check_new(inp, member, result.conflicts, result.renamed)
        if replaced:
            path = stage / name
            try:
                sha1 = pakedit.rebuild(inp.base.path, path, replaced, progress)
                out_path = path
            except (EditError, OSError) as exc:  # no space, or the result did not check
                unwritten(exc)
    if out_path is not None:
        item.out = Out(name, os.stat(fsutil.longpath(out_path)).st_size, sha1)
    for member in item.members:
        if fresh.get(member.member, 0) > 0:
            notes.append(f"{_label(member, inp.rel)}: 확인할 값 {fresh[member.member]}개")
    return Built(inp.index, item, out_path, notes)


def collect(mod_dir: Path, record: Record, keep=frozenset()) -> list[str]:
    """Delete the files in ``edits/`` that neither ``record`` nor ``keep`` (``edits/...`` names) refers
    to; returns the names deleted.  Nothing is deleted for an unreadable record (a newer manager's
    outputs stay for it)."""
    if record.error:
        return []
    folder = Path(mod_dir) / EDITS_DIR
    try:
        names = sorted(os.listdir(fsutil.longpath(folder)))
    except OSError:
        return []
    used = record.outputs() | set(keep)
    gone = []
    for n in names:
        rel = f"{EDITS_DIR}/{n}"
        path = folder / n
        if rel in used or not fsutil.is_file(path):
            continue
        try:
            fsutil.remove_file(path)
            gone.append(rel)
        except OSError as exc:
            log.warning("모드 수정 출력 파일을 지우지 못했습니다: %s (%s)", path, exc)
    if gone:
        log.info("지난 모드 수정 출력 %d개를 지웠습니다 (%s)", len(gone), folder)
    return gone
