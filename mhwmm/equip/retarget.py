"""Move an appearance mod from one piece of equipment to another (다른 장비로 옮기기).

A mod that changes the look of equipment A becomes a new mod that changes equipment B instead:
A's files are put at B's paths and the paths inside the moved materials (.mdf2) and RSZ files
(.pfb/.user/.scn) are pointed at the moved copies.  The ids in a path are fixed-width digits
(armor ``ch03/034/001/2/ch03_034_0012``, weapon ``it03/00/0009/it0300_0009_0``), so every rename
keeps the length of the string and every content edit is made in place: no file is laid out again.

- armor: one design folder MMM/SSS with both bodies (ch02/ch03 model folders, Male/Female prefabs)
  to another design folder, all of the parts or some, and each part to a design of its own if need
  be (부위마다 다른 장비로: the helm to one design, the body to another, in one new mod); weapon: one
  model TT/MMMMMM (main and sub) to another model of the same type.  Bodies, parts and weapon types
  never change.
- which files move follows the references: a shipped file a moved (or kept) material or RSZ file
  points at moves too when it is A's (in any part), and is kept unchanged otherwise (a custom
  folder, a shared texture).  References to A's game files the mod does not ship stay as they are.
  A file of a chosen part goes to that part's target wherever it is used from; a file of a part not
  chosen goes with the target of the parts whose files use it (parts of different targets using the
  same such file: the move cannot be made).
- never moved: the per-design ``MMM_SSS_avp.user`` (hide/merge settings that must match the target's
  own: a copy can freeze the appearance menu or turn the armor black) and ``*_wvp.user`` (tuned for
  the target's model); sound banks (their ids are the original weapon's, the target's bank list
  would load sounds nothing plays); files outside natives/ (scripts, plugins, DLLs) are left out too.
  A shader (.mmtr) the mod ships stays where it is, unchanged: materials name it, and the path of
  a shader inside a material is never rewritten.
- the target's own prefab loads a model (.mesh) and its material (.mdf2) of the same name together:
  when a part of the mod has only one of them (or only textures) and no prefab of its own, the
  other half is A's game file, put at B's path as well (Plan.fills): A's model with B's material
  would not match (material names, UVs).  A prefab of the mod's own loads what it names; when that
  is a material of A's game that reads textures the part moves (and nothing moved reads them), that
  material is put at B's path too, so the moved prefab and the moved textures meet.
- with the game, the target's own prefab tells whether the moved model will show: some prefabs load
  another equipment's mesh (203 weapons, a few armor parts), which would never show the moved one;
  a prefab of the mod's own must land where the target's is.
- PAK entries whose names are unknown stay as they are; a model among them, or a material or RSZ
  file that names A inside, makes the move impossible (it would stay at A, the rest of its part at
  B).  A sound bank among them (told by its first bytes) is left out like a loose one.

Facts about the layouts are from the research for this feature (the community's Equip Slot Converter
and MHWmodfixer's retargeting were read for their findings only; no code is taken from them).
"""

from __future__ import annotations

import logging
import re
import shutil
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

from ..deploy import is_root_pak
from ..matfix import rsz
from ..matfix.mdf2 import Mdf2File, detect_version, version_from_name
from ..matfix.pak import COMPRESSION_NONE, PakFile, compress_as, path_hash, write_pak
from .model import ARMOR_DIGITS, ARMOR_PARTS, BODIES, PART_LABELS, WEAPON_PARTS, WEAPON_TYPES, Target, \
    weapon_type_label
from .pathlist import PathIndex, full_path, streaming_twin, stored_paths, versions
from .paths import BODY_CH, INNER_MODEL, classify, normalize
from .scan import MAX_READ, _why

log = logging.getLogger(__name__)

# stored in the recipe (Mod.retarget); raise when the rules change.  2: "to" may be a part -> target map
RETARGET_FORMAT = 2
KINDS = ("armor", "weapon")
_CONTENT = {"mdf2": "mdf2", "pfb": "rsz", "user": "rsz", "scn": "rsz"}  # files with paths inside
_MAGICS = {b"MDF\x00": "mdf2", **{magic: "rsz" for magic in rsz.MAGICS}}
# an unnamed pak entry that must not stay behind (a material or RSZ file only when it names A), by kind
_UNNAMED_KINDS = {b"MESH": "모델", b"MPLY": "모델", b"MDF\x00": "재질", b"PFB\x00": "프리팹", b"USR\x00": "데이터",
                  b"SCN\x00": "장면"}
_SOUND_MAGICS = (b"BKHD", b"AKPK")  # Wwise sound banks (.sbnk) and packages (.spck)
_MDF2_VERSION = 45  # tried first for a material whose name has no version
_PREFAB_VERSIONS = ("18",)
_PART_DIGITS = {part: digit for digit, part in ARMOR_DIGITS.items()}
_NEVER = re.compile(r"_[aw]vp(?![a-z0-9])", re.IGNORECASE)  # MMM_SSS_avp.user, it..._k_wvp.user
# sound banks and the bank lists that load them (paths.normalize'd): natives/STM/sound/..., .sbnk, .spck
_SOUND = re.compile(r"^sound/|\.(?:sbnk|spck)$|banklist", re.IGNORECASE)
# a part's own prefab (normalized): armor <Male|Female>/MMM/SSS/<Part>/ (not a SlingerRope prefab), a
# weapon model's it<TT><SS>_<NNNN>_<k>.pfb
_OWN_PREFAB = re.compile(r"gamedesign/equip/_prefab/(?:armor/(?:male|female)/\d{3}/\d{3}/"
                         r"(?:arm|body|helm|leg|waist|slinger)/[^/]+|weapon/wp\d\d/\d\d/\d{4}/it\d{4}_\d{4}_[01])\.pfb")
_ARMOR_KEY = re.compile(r"^(\d{3})/(\d{3})$")
_WEAPON_KEY = re.compile(r"^(\d{2})/(\d{2})(\d{4})$")
_ARTIAN_BAND = "10"  # weapons it<TT>/10/<NNNN>: the artian weapons, several share one model
_ENCRYPTED = 0x00FF0000  # pak entry attributes of a separately encrypted file


class RetargetError(Exception):
    """A mod that cannot be moved to the other equipment (the message is for the user)."""


def _josa(word: str, pair: str) -> str:
    """``word`` with the particle that fits its last syllable: ``pair`` "이가", "은는", "을를"..."""
    last = word[-1:]
    final = "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0
    return word + (pair[0] if final else pair[1])


def _reason(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, RetargetError) else _why(exc)


def _game_file(path: str | None) -> bool:
    """Is it a file the game reads (under natives/), not a script, plugin or DLL?"""
    return path is not None and path.replace("\\", "/").lstrip("/").lower().startswith("natives/")


def _is_sound(path: str) -> bool:
    """A sound bank or a bank list: never moved (the ids inside are the original equipment's)."""
    return bool(_SOUND.search(normalize(path)))


def _is_shader(path: str) -> bool:
    """A shader (.mmtr): kept where it is, materials name it and that name is never rewritten."""
    return normalize(path).endswith(".mmtr")


def _own_prefab(path: str) -> bool:
    """The prefab of an armor part (in its part folder) or of a weapon model, not a SlingerRope one."""
    return bool(_OWN_PREFAB.fullmatch(normalize(path)))


# ---- the two pieces of equipment ------------------------------------------------------------
@dataclass(frozen=True, order=True)
class Slot:
    """What a retarget moves from or to: armor ``MMM/SSS`` (one design folder, both bodies) or a
    weapon ``TT/MMMMMM`` (model.weapon_key: folder it<TT>/<SS>/<NNNN>, MMMMMM = SS * 10000 + NNNN)."""
    kind: str
    key: str

    @classmethod
    def of(cls, target: Target) -> "Slot | None":
        """An armor Target (key ``f/034/001``, any part) -> Slot("armor", "034/001"); a weapon Target ->
        its key; None for anything else (innerwear, palico, seikret...)."""
        if target.kind == "armor":
            body, _, design = target.key.partition("/")
            slot = cls("armor", design) if body in BODIES else None
        elif target.kind == "weapon":
            slot = cls("weapon", target.key)
        else:
            return None
        return slot if slot is not None and slot.valid else None

    @property
    def valid(self) -> bool:
        if self.kind == "armor":
            m = _ARMOR_KEY.match(self.key)
            return bool(m) and int(m[1]) != INNER_MODEL
        if self.kind == "weapon":  # not the kinsects' it10/03 nor the it<TT>/99 test folder (paths.py)
            m = _WEAPON_KEY.match(self.key)
            return bool(m) and int(m[1]) < len(WEAPON_TYPES) and m[2] != "99" and (m[1], m[2]) != ("10", "03")
        return False

    def ids(self) -> tuple[str, ...]:
        """The digits as paths spell them: armor (MMM, SSS), weapon (TT, SS, NNNN)."""
        m = (_ARMOR_KEY if self.kind == "armor" else _WEAPON_KEY).match(self.key)
        if not m:
            raise RetargetError(f"옮길 수 없는 장비입니다 ({self.key})")
        return m.groups()


def why_not(a: Slot, b: Slot) -> str | None:
    """Why a mod of ``a`` cannot be moved to ``b`` (Korean), or None when it can."""
    if a.kind not in KINDS or b.kind not in KINDS:
        return "방어구와 무기만 다른 장비로 옮길 수 있습니다"
    if a.kind != b.kind:
        return "방어구는 방어구로, 무기는 무기로만 옮길 수 있습니다"
    for slot in (a, b):
        if not slot.valid:
            return f"옮길 수 없는 장비입니다 ({slot.key})"
    if a.kind == "weapon" and a.ids()[0] != b.ids()[0]:
        return f"무기는 같은 종류({weapon_type_label(int(a.ids()[0]))})의 무기로만 옮길 수 있습니다"
    if a == b:
        return "원래 장비와 같은 장비입니다"
    return None


def sources_of(targets: Iterable[Target]) -> list[Target]:
    """The equipment among ``targets`` (a ModScan's, any part) a mod can be moved away from: armor
    (one Target per body) and weapons, without the part, sorted."""
    return sorted({t.item for t in targets if Slot.of(t) is not None})


def parts_on(targets: Iterable[Target], source: Target) -> list[str]:
    """The armor parts among ``targets`` (a ModScan's) on ``source``'s design, both bodies together (they
    move together), in ARMOR_PARTS order ([] for a weapon)."""
    if source.kind != "armor":
        return []
    design = source.key.partition("/")[2]
    found = {t.part for t in targets if t.kind == "armor" and t.key.partition("/")[2] == design and t.part}
    return [p for p in ARMOR_PARTS if p in found]


class Renamer:
    """A's ids -> B's in a game path, a resource string or a bare name.  ``parts``: the armor parts to
    move (model.ARMOR_PARTS names, None: all); a weapon always moves both of its models."""

    def __init__(self, a: Slot, b: Slot, parts: Iterable[str] | None = None):
        problem = why_not(a, b)
        if problem:
            raise RetargetError(problem)
        self.source, self.target = a, b
        if a.kind == "armor":
            chosen = set(ARMOR_PARTS if parts is None else parts)
            unknown = sorted(chosen - set(ARMOR_PARTS))
            if unknown:
                raise RetargetError(f"알 수 없는 부위입니다: {', '.join(unknown)}")
            self.parts = tuple(p for p in ARMOR_PARTS if p in chosen)
            (m, s), (m2, s2) = a.ids(), b.ids()
            self._digits = {"m": m2, "s": s2, "m2": m2, "s2": s2}
            ch = r"(?<![a-z0-9])[cm]h0[23]"
            digit = lambda x: ARMOR_DIGITS[x["d"]]  # noqa: E731
            self._rules = [
                # Art/Model/Character model folders (streaming/ too), any case: only the digits change
                (re.compile(rf"{ch}/(?P<m>{m})/(?P<s>{s})/(?P<d>[1-6])/", re.I), digit),
                # GameDesign/Equip/_Prefab/Armor/<Male|Female>/MMM/SSS/<part>/
                (re.compile(rf"/(?:male|female)/(?P<m>{m})/(?P<s>{s})/(?P<p>arm|body|helm|leg|waist|slinger)/", re.I),
                 lambda x: x["p"].lower()),
                # SlingerRope_MMM_SSS.pfb (or SlingerRope_MMM.pfb) in the design folder
                (re.compile(rf"/(?:male|female)/(?P<m>{m})/(?P<s>{s})/slingerrope_(?P<m2>{m})(?:_(?P<s2>{s}))?\.",
                            re.I), lambda x: "slinger"),
                # file and bare names: ch03_034_0012.mesh, ..._ALBD.tex, ch02_034_0013__ch01_000_0001__m_...
                (re.compile(rf"{ch}_(?P<m>{m})_(?P<s>{s})(?P<d>[1-6])(?![0-9])", re.I), digit),
            ]
        else:
            self.parts = WEAPON_PARTS
            (t, ss, n), (ss2, n2) = a.ids(), b.ids()[1:]
            self._digits = {"ss": ss2, "n": n2}
            self._rules = [
                (re.compile(rf"(?<![a-z0-9])it{t}/(?P<ss>{ss})/(?P<n>{n})/", re.I), None),  # Art/Model/Item
                (re.compile(rf"(?<![a-z0-9])wp{t}/(?P<ss>{ss})/(?P<n>{n})/", re.I), None),  # _Prefab/Weapon
                # file and bare names (models, textures, sound banks it0300_0009_m.sbnk): _0 main, _1 sub
                (re.compile(rf"(?<![a-z0-9])it{t}(?P<ss>{ss})_(?P<n>{n})(?=_)(?P<k>_[01](?![0-9]))?", re.I),
                 lambda x: {"_0": "main", "_1": "sub"}.get(x["k"] or "", "")),
            ]

    def _scan(self, text: str) -> tuple[dict[tuple[int, int], str], str | None]:
        """(span of each of A's ids in ``text`` -> B's digits, the part: the first match's, "" for a
        weapon file of neither model) - ({}, None) when ``text`` is not A's."""
        if _NEVER.search(text):
            return {}, None
        edits: dict[tuple[int, int], str] = {}
        parts = []
        for rx, part in self._rules:
            for m in rx.finditer(text):
                parts.append(part(m) if part else "")
                for group, digits in self._digits.items():
                    if group in rx.groupindex and m.start(group) >= 0:
                        edits[(m.start(group), m.end(group))] = digits
        if not edits:
            return {}, None
        return edits, next((p for p in parts if p), "")

    def part_of(self, path: str) -> str | None:
        """The part of A ``path`` (or a name) belongs to, "" for a weapon file of neither model, None
        when it is not A's."""
        return self._scan(path)[1]

    def rename(self, path: str, all_parts: bool = False) -> str | None:
        """``path`` with A's ids replaced by B's when it belongs to A (and, unless ``all_parts``, to a
        chosen part); None otherwise.  Any spelling: natives/STM/..., resource strings (Art/..., @...),
        streaming/ paths, bare names; the case is kept (only digits change)."""
        edits, part = self._scan(path)
        if part is None or (not all_parts and self.source.kind == "armor" and part not in self.parts):
            return None
        out, last = [], 0
        for (start, end), digits in sorted(edits.items()):
            if start < last:
                continue
            out += [path[last:start], digits]
            last = end
        out.append(path[last:])
        new = "".join(out)
        if len(new) != len(path):
            raise RetargetError(f"경로의 길이가 바뀌어 옮길 수 없습니다: {path}")
        return new

    def renames(self, path: str) -> list[str]:
        """Every new spelling ``path`` can get (any part): [rename(path, all_parts=True)], [] when it is
        not A's.  (A MixedRenamer can give one per target: :func:`mover` picks the planned one.)"""
        new = self.rename(path, all_parts=True)
        return [] if new is None else [new]

    def to(self, target: Slot) -> "Renamer":
        """The renamer of the parts that go to ``target``: this one (see MixedRenamer.to)."""
        if target != self.target:
            raise RetargetError(f"옮길 장비가 계획과 다릅니다 ({target.key})")
        return self


class MixedRenamer:
    """Several Renamers as one: the armor parts of one design moved to different designs (one Renamer
    per target with the parts that go there, :func:`renamer_of`), or back (each target's ids to the
    design its parts came from).  It has Renamer's methods: a path goes by the member whose source it
    belongs to and whose parts hold its part.  ``rename(path, all_parts=True)`` of a part no member
    moves could be any member's: it gives the first member's, which tells whether a path is the
    source's; where such a file goes plan decides (the target of the files that use it), and
    :func:`mover` takes the member that gives the planned path (``renames``).  ``source`` and
    ``target``: the first member's."""

    def __init__(self, members: Iterable[Renamer]):
        self.members = tuple(members)
        if not self.members:
            raise RetargetError("옮길 부위를 하나 이상 고르세요")
        taken: dict[Slot, set[str]] = {}
        for member in self.members:
            have = taken.setdefault(member.source, set())
            if have & set(member.parts):
                raise RetargetError("한 부위를 두 장비로 옮길 수 없습니다")
            have.update(member.parts)
        self.source, self.target = self.members[0].source, self.members[0].target
        order = ARMOR_PARTS + WEAPON_PARTS
        self.parts = tuple(p for p in order if any(p in member.parts for member in self.members))

    def _owner(self, path: str) -> Renamer | None:
        for member in self.members:
            part = member.part_of(path)
            if part is not None and (part in member.parts or member.source.kind != "armor"):
                return member
        return None

    def part_of(self, path: str) -> str | None:
        return next((part for member in self.members if (part := member.part_of(path)) is not None), None)

    def rename(self, path: str, all_parts: bool = False) -> str | None:
        owner = self._owner(path)
        if owner is not None:
            return owner.rename(path, all_parts)
        if not all_parts:
            return None
        return next((new for member in self.members if (new := member.rename(path, all_parts=True)) is not None),
                    None)

    def renames(self, path: str) -> list[str]:
        return list(dict.fromkeys(new for member in self.members
                                  if (new := member.rename(path, all_parts=True)) is not None))

    def to(self, target: Slot) -> Renamer:
        """The member that moves to ``target`` (its parts' Renamer)."""
        member = next((m for m in self.members if m.target == target), None)
        if member is None:
            raise RetargetError(f"옮길 장비가 계획과 다릅니다 ({target.key})")
        return member


def renamer_of(source: Slot, targets: Mapping[str, Slot], reverse: bool = False) -> Renamer | MixedRenamer:
    """The renamer of a move of ``source``'s parts to ``targets`` (part -> its target, as Plan.targets: a
    weapon's main and sub to one weapon): a Renamer when every part goes to one target (the same as
    ``Renamer(source, target, parts)``), else a MixedRenamer of one Renamer per target with the parts that
    go there.  ``reverse``: back, each target's ids to ``source``'s (how the material check reads the new
    mod's files).  RetargetError as Renamer."""
    groups: dict[Slot, list[str]] = {}
    for part, slot in targets.items():
        groups.setdefault(slot, []).append(part)
    if not groups:
        raise RetargetError("옮길 부위를 하나 이상 고르세요")
    members = []
    for slot, parts in groups.items():
        chosen = parts if source.kind == "armor" else None
        members.append(Renamer(slot, source, chosen) if reverse else Renamer(source, slot, chosen))
    return members[0] if len(members) == 1 else MixedRenamer(members)


# ---- the mod's files --------------------------------------------------------------------------------
@dataclass(eq=False)
class Item:
    """One file of the mod: a loose file, or an entry of one of its root .pak files."""
    path: str | None  # game rel ("natives/STM/..."), or the name a pak entry resolved to (None: unnamed)
    read: Callable[[], bytes]
    pak: str | None = None  # the mod's pak (root "*.pak") of an entry
    hash64: int | None = None  # the pak entry's key
    stored: Path | None = field(default=None, repr=False)  # a loose file's stored copy
    entry: tuple[PakFile, int] | None = field(default=None, repr=False)  # a pak entry: its pak, its index
    magic: bytes | None = field(default=None, repr=False)  # an unnamed entry's first bytes, once read


def _kind(name: str | None, data: bytes | None = None) -> str | None:
    """"mdf2" / "rsz" for a file with paths inside, by its name (by its first bytes when it has none)."""
    if name:
        return _CONTENT.get(normalize(name).rpartition(".")[2])
    return _MAGICS.get(bytes(data[:4])) if data is not None else None


def _size(item: Item) -> int:
    try:
        if item.entry is not None:
            return item.entry[0].dsizes[item.entry[1]]
        return item.stored.stat().st_size if item.stored is not None else 0
    except (OSError, ValueError):
        return 0


def _magic(item: Item) -> bytes:
    """The first four bytes of an unnamed pak entry (b"" when they cannot be read)."""
    if item.magic is None:
        item.magic = b""
        if item.entry is not None:
            pak, i = item.entry
            try:
                # (an encrypted or chunked entry is read whole for its first bytes: only a small one)
                whole = pak.entry(i)[3] & _ENCRYPTED or pak.chunk_offsets is not None
                if not whole or _size(item) <= MAX_READ:
                    item.magic = bytes(pak.head(i, 4)[:4])
            except Exception:  # noqa: BLE001 - no zstd module, broken data: not read
                pass
    return item.magic


def _item_kind(item: Item) -> str | None:
    """_kind of an item; an unnamed pak entry is told by its first bytes."""
    if item.path is not None:
        return _kind(item.path)
    return _MAGICS.get(_magic(item)) if _size(item) <= MAX_READ else None


def _names_source(item: Item, renamer: Renamer) -> bool:
    """Does an unnamed pak entry (a material or RSZ file) name A inside: a path or a bare name (a
    prefab's GameObject, a material's names) of A in any of its UTF-16 strings?  True when it cannot
    be read: it may be A's own file."""
    if _size(item) > MAX_READ:
        return True
    try:
        return any(renamer.rename(s, all_parts=True) is not None for _offset, s in rsz.scan_strings(item.read()))
    except Exception:  # noqa: BLE001 - a broken entry
        return True


def _label(item: Item) -> str:
    return item.path if item.path is not None else f"{item.pak} 안의 파일 #{item.hash64:016X}"


def _kept_name(item: Item) -> str:
    """How Plan.kept names an item: its path, ``<pak>#<hash>`` for an unnamed pak entry."""
    return item.path if item.path is not None else f"{item.pak}#{item.hash64:016X}"


def items_of(files: Mapping[str, Path], index: PathIndex | None) -> list[Item]:
    """The items of a mod's entries (game rel -> stored file, or ModManager.mod_entries' (stored, key)
    pairs).  Root .pak files are read entry by entry; entries are named from ``index`` and, as
    scan.scan_files does, from the paths the mod's own materials and RSZ files point at (hashed with
    each version the game uses, and as streaming/ copies).  Raises RetargetError for a pak that does
    not read."""
    items: list[Item] = []
    for rel, stored in files.items():
        if isinstance(stored, tuple):
            stored = stored[0]
        stored = Path(stored)
        if is_root_pak(rel):
            try:
                pak = PakFile(stored)
            except Exception as exc:  # noqa: BLE001 - OSError, MatfixError, or a table broken in another way
                raise RetargetError(f"{rel}: pak 파일을 읽지 못했습니다 ({_why(exc)})") from exc
            for i, h in enumerate(pak.hashes):
                items.append(Item(index.lookup(h) if index is not None else None,
                                  lambda pak=pak, i=i: pak.read(i), rel, h, entry=(pak, i)))
        else:
            items.append(Item(rel, stored.read_bytes, stored=stored))
    _name_entries(items)
    return items


def _name_entries(items: list[Item]) -> None:
    unnamed: dict[int, list[Item]] = {}
    for item in items:
        if item.path is None:
            unnamed.setdefault(item.hash64, []).append(item)
    seen: set[int] = set()
    while unnamed:
        # named materials and RSZ files first; only then the first bytes of what is still unnamed
        batch = [it for it in items if id(it) not in seen and it.path is not None and _kind(it.path)]
        if not batch:
            batch = [it for group in unnamed.values() for it in group if id(it) not in seen and _item_kind(it)]
        if not batch:
            return
        refs: set[str] = set()
        for item in batch:
            seen.add(id(item))
            if _size(item) > MAX_READ:
                continue
            try:
                refs.update(normalize(r) for r in references(item.read(), item.path))
            except Exception as exc:  # noqa: BLE001 - a mod's file can be broken in any way
                log.debug("%s: %s", _label(item), exc)
        for ref in sorted(refs):
            for p in stored_paths(ref):
                for item in unnamed.pop(path_hash(p), ()):
                    item.path = p
            if not unnamed:
                return


# ---- references inside files --------------------------------------------------------------------
def _open_mdf2(data: bytes, name: str | None) -> Mdf2File:
    prefer = _MDF2_VERSION
    if name:
        try:
            prefer = version_from_name(name)
        except ValueError:
            pass
    version = detect_version(data, prefer=prefer)
    if version is None:
        raise RetargetError("재질 파일(.mdf2)을 읽지 못했습니다")
    return Mdf2File(data, version)


def _mdf2_paths(mdf: Mdf2File):
    """A material file's texture paths and gpbf paths (its gpbf list is name / path pairs: a path has a
    "/"); material names and .mmtr shader paths are never among them."""
    for mat in mdf.materials:
        for _type, path in mat.textures:
            if path.text:
                yield path
        for ref in mat.gpbf:
            if "/" in ref.text:
                yield ref


def references(data: bytes, name: str | None = None) -> list[str]:
    """The paths a file points at, as written in it: a material's texture and gpbf paths and its
    shaders (.mmtr: followed, never rewritten); every UTF-16 string with a "/" of an RSZ file (resource
    and userdata tables, Resource fields).  [] for other files; RetargetError for a material or RSZ
    file that does not read."""
    kind = _kind(name, data)
    if kind == "mdf2":
        mdf = _open_mdf2(data, name)
        shaders = dict.fromkeys(mat.mmtr_path for mat in mdf.materials if mat.mmtr_path)
        return [ref.text for ref in _mdf2_paths(mdf)] + list(shaders)
    if kind == "rsz":
        if rsz.parse(data) is None:
            raise RetargetError("파일 구조를 읽지 못했습니다")
        return [s for _offset, s in rsz.scan_strings(data) if "/" in s]
    return []


def _same_length(old: str, new: str) -> None:
    if len(old) != len(new):
        raise RetargetError(f"경로의 길이가 바뀌어 고칠 수 없습니다: {old}")


def rewrite(data: bytes, name: str | None, moved: Callable[[str], str | None],
            renamer: Renamer | MixedRenamer | None = None) -> tuple[bytes, int]:
    """``data`` with its references to moved files pointed at their new paths, in place (every new
    string is as long as the old one): (new bytes, number of strings changed).  ``name``: the file's
    name (its type and version; None for an unnamed pak entry: told by its first bytes).  ``moved(ref)``
    gives a reference's new text or None (see :func:`mover`).  ``renamer`` also renames the bare names
    of A in an RSZ file (a prefab's GameObject name ``ch03_034_0012``: with a MixedRenamer, each to its
    own part's target, as references to files go); None leaves them.  Other files
    than .mdf2 and RSZ files hold no paths and come back unchanged.  RetargetError when the result
    does not read back as intended."""
    kind = _kind(name, data)
    if kind == "mdf2":
        return _rewrite_mdf2(data, name, moved)
    if kind == "rsz":
        return _rewrite_rsz(data, moved, renamer)
    return data, 0


def _rewrite_mdf2(data: bytes, name: str | None, moved) -> tuple[bytes, int]:
    # Strings are overwritten where they are, never with Mdf2File.set_string: that appends a copy of a
    # string several fields share and moves offsets, and the shader-LOD table's pointers are not tracked
    mdf = _open_mdf2(data, name)
    changed: dict[int, str] = {}  # field position -> new text
    strings: dict[int, str] = {}  # string position -> new text
    for ref in _mdf2_paths(mdf):
        new = moved(ref.text)
        if new is None or new == ref.text:
            continue
        _same_length(ref.text, new)
        if strings.setdefault(ref.str_pos, new) != new:
            raise RetargetError(f"재질 파일의 같은 경로를 두 가지로 고쳐야 합니다: {ref.text}")
        changed[ref.field_pos] = new
    if not changed:
        return data, 0
    out = bytearray(data)
    for pos, text in strings.items():
        raw = text.encode("utf-16-le")
        out[pos:pos + len(raw)] = raw
    check = Mdf2File(bytes(out), mdf.version)
    before, after = list(mdf._refs()), list(check._refs())
    if len(out) != len(data) or len(before) != len(after) or any(
            b.text != changed.get(a.field_pos, a.text) for a, b in zip(before, after)):
        raise RetargetError("고친 재질 파일을 다시 읽어 보니 다른 곳까지 바뀌었습니다")
    return bytes(out), len(changed)


def _rewrite_rsz(data: bytes, moved, renamer: Renamer | None) -> tuple[bytes, int]:
    info = rsz.parse(data)
    if info is None:
        raise RetargetError("파일 구조를 읽지 못했습니다")

    def new_text(s: str) -> str | None:
        if "/" in s:
            return moved(s)
        return renamer.rename(s) if renamer is not None else None

    out, count = bytearray(data), 0
    for offset, s in rsz.scan_strings(data):  # the whole file, as rsz.apply_substitution
        new = new_text(s)
        if new is None or new == s:
            continue
        _same_length(s, new)
        out[offset:offset + 2 * len(s)] = new.encode("utf-16-le")
        count += 1
    if not count:
        return data, 0
    check = rsz.parse(bytes(out))
    if (check is None or len(out) != len(data) or check.insts != info.insts or check.rsz_off != info.rsz_off
            or check.resources() != {new_text(r) or r for r in info.resources()}):
        raise RetargetError("고친 파일을 다시 읽어 보니 다른 곳까지 바뀌었습니다")
    return bytes(out), count


def mover(pairs: Iterable[tuple[str, str]], renamer: Renamer | MixedRenamer) -> Callable[[str], str | None]:
    """The ``moved`` function of :func:`rewrite` for moved files ``pairs`` ((new path, old path), as
    Built.moves and the recipe keep them): a reference to an old path (compared normalized: case,
    natives/STM/, streaming/, version) -> that reference renamed, its own spelling kept; None for
    anything else.  With a MixedRenamer, the member that gives the reference its file's new path (a
    file of one part a file of another part points at went to its own part's target)."""
    table = {normalize(old): normalize(new) for new, old in pairs}
    renames = getattr(renamer, "renames", None)
    if renames is None:  # (anything with Renamer.rename)
        def renames(ref: str) -> list[str]:
            new = renamer.rename(ref, all_parts=True)
            return [] if new is None else [new]

    def moved(ref: str) -> str | None:
        want = table.get(normalize(ref))
        if want is None:
            return None
        new = next((new for new in renames(ref) if normalize(new) == want), None)
        if new is None:
            raise RetargetError(f"옮긴 파일을 가리키는 경로를 고치지 못했습니다: {ref}")
        return new

    return moved


# ---- the plan ---------------------------------------------------------------------------------------------
@dataclass
class Move:
    old: str  # game rel (or the pak entry's name)
    new: str
    # "copy" (renamed only) | "mdf2" | "rsz" (references inside rewritten) | "fill" (A's game file the
    # mod does not ship, completing a model and its material: Plan.fills)
    how: str = "copy"
    pak: str | None = None  # the pak of an entry
    target: Slot | None = None  # the equipment the file went to (Plan.targets: its part's, or its users')


@dataclass
class Plan:
    source: Slot
    # the target of a move of every part to one piece of equipment; of a move of the parts to different
    # ones (부위마다 다른 장비로), that of the first chosen part in ARMOR_PARTS order (Plan.targets has all)
    target: Slot
    parts: tuple[str, ...] = ()  # the chosen armor parts (a weapon: both models)
    moves: list[Move] = field(default_factory=list)
    # shipped files copied to the same path (custom and shared files the moved ones use; an unnamed
    # pak entry as "<pak>#<hash>"): a reference inside them to a moved file is pointed at its new path
    kept: list[str] = field(default_factory=list)
    # (path, Korean reason); an unnamed pak entry (a sound bank) as "<pak>#<hash>", as in kept
    dropped: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # Korean, what the user should know
    errors: list[str] = field(default_factory=list)  # Korean; any: the mod cannot be made
    vanilla_refs: int = 0  # A's game files the moved files use and the mod does not ship (left as they are)
    # A's game files (read through the game) that complete a part, written like moved files at their
    # new path (a material's references to moved files rewritten); also in moves, how "fill"
    fills: list[Item] = field(default_factory=list)
    bad_parts: set[str] = field(default_factory=set)  # armor parts an error names (unticking them helps)
    # each chosen part -> the equipment it goes to (a move to one: every part to ``target``; a weapon: main
    # and sub to it); filled from ``parts`` when not given
    targets: dict[str, Slot] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.targets and self.parts:
            self.targets = {part: self.target for part in self.parts}

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.moves)

    @property
    def mixed(self) -> bool:
        """Do the chosen parts go to different equipment?"""
        return len(set(self.targets.values())) > 1


def _mapped(source: Slot, mapping: Mapping[str, Slot], parts: Iterable[str] | None) -> tuple[Plan, dict | None]:
    """A Plan for a move of each part to its own target (``mapping``: part -> target, armor only) with the
    chosen parts and their targets, and those targets in ARMOR_PARTS order; None (the Plan's errors say
    why) when it cannot be made: a weapon (its models go together), a part that is not one, ``parts``
    other than the mapping's, a target a part cannot go to (why_not; named with the part, which goes
    into bad_parts, unless every part goes to the same one: then as a move to that one)."""
    first = next((slot for slot in mapping.values() if isinstance(slot, Slot)), source)  # (source: none)
    if source.kind == "weapon":
        result = Plan(source, first)
        result.errors.append("무기는 본체와 보조를 함께 한 무기로만 옮길 수 있습니다")
        return result, None
    chosen = {part: mapping[part] for part in ARMOR_PARTS if part in mapping}
    slots = {part: slot for part, slot in chosen.items() if isinstance(slot, Slot)}
    result = Plan(source, next(iter(slots.values()), first))
    result.parts, result.targets = tuple(chosen), slots
    unknown = sorted(str(part) for part in mapping if part not in ARMOR_PARTS)
    if unknown:
        result.errors.append(f"알 수 없는 부위입니다: {', '.join(unknown)}")
        return result, None
    if parts is not None and set(parts) != set(chosen):
        result.errors.append("옮길 부위가 부위마다 고른 장비와 맞지 않습니다")
        return result, None
    if not chosen:
        result.errors.append("옮길 부위를 하나 이상 고르세요")
        return result, None
    problems = {}
    for part, slot in chosen.items():
        problem = why_not(source, slot) if isinstance(slot, Slot) else f"옮길 수 없는 장비입니다 ({slot})"
        if problem:
            problems[part] = problem
    if problems:
        same = len(slots) == len(chosen) and not result.mixed
        if same or (len(problems) == len(chosen) and len(set(problems.values())) == 1):
            result.errors.append(next(iter(problems.values())))  # (the source's own problem, or one target)
        else:
            for part, problem in problems.items():
                result.errors.append(f"{PART_LABELS[part]}: {problem}")
                result.bad_parts.add(part)
        return result, None
    return result, chosen


def plan(items: list[Item], source: Slot, target: Slot | Mapping[str, Slot], parts: Iterable[str] | None = None,
         game=None) -> Plan:
    """What moving ``items`` (items_of) from ``source`` to ``target`` would do.  ``target``: the equipment
    the chosen ``parts`` (armor parts, None: all) go to, or for armor a mapping part -> the equipment that
    part goes to (부위마다 다른 장비로; the parts not in it are not moved, ``parts`` must then be None or
    its parts; a mapping of every part to one target is the same as that target).  ``game`` (has(path) /
    read(path) like matfix.game.GameIndex; None: not checked) shows whether the targets' own prefabs
    would load the moved models, and gives the model or material a part of the mod lacks (Plan.fills: A's
    game file; without the game such a part is an error).  Reads the mod's materials and RSZ files; never
    raises for a broken file (it becomes an error)."""
    if isinstance(target, Mapping):
        result, targets = _mapped(source, target, parts)
        if targets is None:
            return result
        try:
            renamer = renamer_of(source, targets)
        except RetargetError as exc:
            result.errors.append(str(exc))
            return result
    else:
        result = Plan(source, target)
        problem = why_not(source, target)
        if problem:
            result.errors.append(problem)
            return result
        try:
            renamer = Renamer(source, target, parts)
        except RetargetError as exc:
            result.errors.append(str(exc))
            return result
        result.parts = renamer.parts
        if not renamer.parts:
            result.errors.append("옮길 부위를 하나 이상 고르세요")
            return result
        targets = {part: target for part in renamer.parts}
        result.targets = dict(targets)
    mixed = result.mixed
    weapon = source.kind == "weapon"

    named: dict[str, list[Item]] = {}  # normalized path -> items (a file and its streaming/ copy, versions)
    for item in items:
        if item.path is not None:
            named.setdefault(normalize(item.path), []).append(item)
    fate: dict[int, str] = {}  # id(item) -> "move" | "keep"
    pulled: list[Item] = []  # files of parts not chosen that move because moved files use them
    uses: dict[int, list[Item]] = {}  # id(item) -> the shipped files it points at that are moved or kept
    # unnamed models, and materials and RSZ files that name A: they would stay at A, the rest at B
    unknown: Counter = Counter()
    for item in items:
        if item.path is None:
            magic = _magic(item)
            if magic in _SOUND_MAGICS:
                continue  # a sound bank: left out below, as a loose one
            fate[id(item)] = "keep"  # never guessed, never rewritten
            if magic in _UNNAMED_KINDS and (magic not in _MAGICS or _names_source(item, renamer)):
                unknown[_UNNAMED_KINDS[magic]] += 1
        elif (_game_file(item.path) and not _is_sound(item.path) and not _is_shader(item.path)
              and renamer.rename(item.path) is not None):
            fate[id(item)] = "move"
    if unknown:
        kinds = "·".join(dict.fromkeys(kind for kind in _UNNAMED_KINDS.values() if kind in unknown))
        result.errors.append(f"PAK 안에 이름을 알 수 없는 {kinds} 파일이 있어 옮길 수 없습니다 (파일 "
                             f"{sum(unknown.values())}개: 그 파일만 원래 장비에 남게 됩니다)")

    # ownership follows references: what the moved and kept files point at goes with them
    kinds = {id(item): _item_kind(item) for item in items}
    contents: dict[int, tuple[bytes, list[str]]] = {}
    queue = deque(item for item in items if id(item) in fate and item.path is not None and kinds[id(item)])

    def follow(refs: Iterable[str], user: Item) -> None:
        edges = uses.setdefault(id(user), [])
        for ref in refs:
            for other in named.get(normalize(ref), ()):
                if id(other) not in fate:
                    if not _game_file(other.path) or _NEVER.search(other.path) or _is_sound(other.path):
                        continue
                    if not _is_shader(other.path) and renamer.rename(other.path, all_parts=True) is not None:
                        fate[id(other)] = "move"
                        pulled.append(other)
                    else:  # a custom folder's or a shared file, a shader: where it is
                        fate[id(other)] = "keep"
                    if kinds[id(other)]:
                        queue.append(other)
                edges.append(other)

    def drain() -> None:
        while queue:
            item = queue.popleft()
            if id(item) in contents:
                continue
            try:
                data = item.read()
                refs = references(data, item.path)
            except Exception as exc:  # noqa: BLE001 - a mod's file can be broken in any way
                contents[id(item)] = (b"", [])
                if fate[id(item)] == "move":
                    result.errors.append(f"{_label(item)}: 파일을 읽지 못했습니다 ({_reason(exc)})")
                else:
                    log.info("%s: %s", _label(item), exc)
                continue
            contents[id(item)] = (data, refs)
            follow(refs, item)

    drain()
    fills, failed, filled_for = _fills(result, [item for item in items if fate.get(id(item)) == "move"], renamer,
                                       game, contents, set(named))
    for fill in fills:  # A's material's own files
        follow(contents.get(id(fill), (b"", []))[1], fill)
    drain()
    result.fills = fills

    moving = [item for item in items if fate.get(id(item)) == "move"]
    keeping = [item for item in items if fate.get(id(item)) == "keep"]
    filled = {id(fill) for fill in fills}
    filled_paths = {normalize(fill.path) for fill in fills}  # (a moved prefab may name them: not left as they are)
    placed = [*moving, *fills]  # everything that lands at the targets' paths
    if not moving:
        result.errors.append("이 모드에는 옮길 원래 장비의 파일이 없습니다")

    # where each file goes: a chosen part's to its target, any other to the target of the files using it
    dest: dict[int, Slot] = {}
    reach: dict[int, dict[Slot, set[str]]] = {}  # id(item) -> target -> the chosen parts whose files use it
    work: deque = deque()
    for item in placed:
        part = renamer.part_of(item.path)
        if id(item) in filled and part not in targets:
            part = filled_for[id(item)]  # (a material a part's prefab names)
        if weapon or part in targets:
            dest[id(item)] = result.target if weapon else targets[part]
            reach[id(item)] = {dest[id(item)]: {part or ""}}
            work.append(item)
    while work:
        item = work.popleft()
        for other in uses.get(id(item), ()):
            if id(other) in dest:
                continue
            mine, grew = reach.setdefault(id(other), {}), False
            for slot, users in reach[id(item)].items():
                have = mine.setdefault(slot, set())
                if not users <= have:
                    have |= users
                    grew = True
            if grew:
                work.append(other)
    order = list(dict.fromkeys(targets.values()))
    shared: dict[tuple[str, frozenset], list[str]] = {}  # (part, the parts using it) -> files of different targets
    carried: dict[tuple[str, Slot], tuple[int, set[str]]] = {}  # (part, target) -> files, the parts using them
    for item in pulled:
        got = reach.get(id(item)) or {result.target: set()}
        slots = sorted(got, key=lambda slot: order.index(slot) if slot in order else len(order))
        dest[id(item)] = slots[0]
        part = renamer.part_of(item.path) or ""
        users = set().union(*got.values())
        if len(slots) > 1:
            shared.setdefault((part, frozenset(users)), []).append(item.path)
        else:
            n, before = carried.get((part, slots[0]), (0, set()))
            carried[(part, slots[0])] = (n + 1, before | users)
    for (part, users), paths in sorted(shared.items(), key=lambda kv: (_part_order(kv[0][0]), sorted(kv[1]))):
        label, names = PART_LABELS.get(part, part), sorted(paths, key=str.lower)
        more = f" 외 {len(names) - 1}개" if len(names) > 1 else ""
        result.errors.append(f"고르지 않은 {label} 파일({names[0].rpartition('/')[2]}{more})을 여러 부위"
                             f"({_labels(users)})가 함께 써서 한 장비로 옮길 수 없습니다. 이 부위들을 같은 장비로 "
                             f"옮기거나 {label}도 함께 옮기세요")
        result.bad_parts.update(users)  # (leaving one of them where it is helps too)

    own = {slot: renamer.to(slot) for slot in order}  # each target's renamer
    new_of = {id(item): own[dest[id(item)]].rename(item.path, all_parts=True) for item in placed}

    # a moved file must not land on a file the mod already has (it then changes the target already)
    staying = {key for key, group in named.items() if any(fate.get(id(it)) != "move" for it in group)}
    seen: set[tuple[str | None, int]] = set()
    clashes = []
    for item in placed:
        new = new_of[id(item)]
        key = (item.pak, path_hash(full_path(new)))
        if normalize(new) in staying or key in seen:
            clashes.append(new)
        seen.add(key)
    if clashes:
        more = f" 외 {len(clashes) - 1}개" if len(clashes) > 1 else ""
        result.errors.append(f"모드에 대상 장비의 파일이 이미 있어 옮길 수 없습니다: {clashes[0]}{more}")

    moved = mover([(new_of[id(item)], item.path) for item in placed], renamer)
    vanilla: set[str] = set()
    for item in placed:
        fill = id(item) in filled
        how = "fill" if fill else "copy"
        kind = _kind(item.path) if fill else kinds[id(item)]
        data, refs = contents.get(id(item), (b"", []))
        if data and kind:
            try:
                if rewrite(data, item.path, moved, None if fill else renamer)[1] and not fill:
                    how = kind
            except RetargetError as exc:
                result.errors.append(f"{item.path}: {exc}")
            for ref in refs:
                norm = normalize(ref)
                if norm not in named and norm not in filled_paths and renamer.rename(ref, all_parts=True) is not None:
                    vanilla.add(norm)
        result.moves.append(Move(item.path, new_of[id(item)], how, item.pak, dest[id(item)]))
    result.moves.sort(key=lambda m: (m.pak or "", m.new.lower()))
    result.vanilla_refs = len(vanilla)
    for item in keeping:
        data, _refs = contents.get(id(item), (b"", []))
        if data and item.path is not None:
            try:
                rewrite(data, item.path, moved, None)
            except RetargetError as exc:
                result.notes.append(f"{_label(item)}: 안의 경로를 고치지 못해 그대로 넣습니다 ({exc})")
    result.kept = sorted(dict.fromkeys(_kept_name(item) for item in keeping))

    never, outside, sounds = set(), 0, 0
    for item in items:
        if id(item) in fate:
            continue
        path, owner = item.path, None
        if path is None:  # an unnamed pak entry left out: a sound bank (its first bytes), whosever it is
            path, reason, sounds = _kept_name(item), "소리 파일은 옮기지 않습니다", sounds + 1
        elif not _game_file(path):
            reason, outside = "게임 파일이 아닙니다 (스크립트·플러그인 등)", outside + 1
        elif _is_sound(path):
            reason = "소리 파일은 옮기지 않습니다"
            sounds += renamer.rename(path, all_parts=True) is not None
        elif (owner := classify(path)) is not None and _NEVER.search(path) and Slot.of(owner) == source:
            reason = "무기 외형 설정 파일(_wvp.user)은 옮기지 않습니다" if weapon else \
                "외형 설정 파일(_avp.user)은 옮기지 않습니다"
            never.add(source.kind)
        elif _is_shader(path):
            reason = "옮기는 파일이 쓰지 않습니다"
        elif (part := renamer.part_of(path)) is not None:
            reason = f"고르지 않은 부위입니다 ({PART_LABELS.get(part, part)})"
        elif owner is not None and Slot.of(owner) != source:
            reason = "다른 장비의 파일입니다"
        else:
            reason = "옮기는 파일이 쓰지 않습니다"
        result.dropped.append((path, reason))
    result.dropped.sort(key=lambda d: d[0].lower())

    for (part, slot), (n, users) in sorted(carried.items(), key=lambda kv: (_part_order(kv[0][0]),
                                                                           order.index(kv[0][1]))):
        label = PART_LABELS.get(part, part)
        if mixed:  # the parts using them name the target
            users_label = _labels(users)
            result.notes.append(f"고르지 않은 {label} 파일 {n}개도 {users_label} 파일이 써서 함께 옮깁니다. "
                                f"{_josa(users_label, '을를')} 옮길 장비의 {label} 모습도 달라질 수 있습니다")
        else:
            result.notes.append(f"고르지 않은 {label} 파일 {n}개도 옮기는 파일이 써서 함께 옮깁니다. "
                                f"대상 장비의 {label} 모습도 달라질 수 있습니다")
    if moving:
        new_paths = [new_of[id(item)] for item in placed]
        _check_game(result, [(new_of[id(item)], dest[id(item)]) for item in placed], game)
        _meshless_notes(result, new_paths, {part for _body, part in failed})
        _body_notes(result, [new_of[id(item)] for item in moving], new_paths, failed)
    if result.vanilla_refs:
        result.notes.append(f"모드가 원래 장비의 게임 텍스처 등 {result.vanilla_refs}개를 그대로 씁니다. "
                            "원래 장비를 바꾸는 다른 모드를 켜면 옮긴 모드의 모습도 달라질 수 있습니다")
    if "armor" in never:
        whose = "부위마다 옮길 장비의" if mixed else "대상 장비의"
        result.notes.append(f"방어구 외형 설정 파일(_avp.user)은 {whose} 설정과 맞아야 해서 옮기지 않았습니다. "
                            f"{whose} 설정이 그대로 쓰입니다")
    if "weapon" in never:
        result.notes.append("무기 외형 설정 파일(_wvp.user)은 원래 무기의 모델에 맞춘 것이라 옮기지 않았습니다. "
                            "대상 무기의 설정이 그대로 쓰입니다")
    if moving and source.kind == "armor":
        # the targets' avp (and the hair settings it names) stays in force: it was made for its own models
        whose = "부위마다 옮길 장비의" if mixed else "대상 장비의"
        if "helm" in renamer.parts and any(renamer.part_of(item.path) == "helm" for item in moving):
            result.notes.append(f"머리카락 숨김·다른 부위와 겹칠 때 숨기는 설정은 {whose} 것을 씁니다. "
                                "머리카락이 투구를 뚫고 나오거나 일부가 보이지 않을 수 있습니다")
        else:
            result.notes.append(f"다른 부위와 겹칠 때 숨기는 설정은 {whose} 것을 씁니다. 일부가 보이지 않을 수 있습니다")
    if moving and weapon and result.target.ids()[1] == _ARTIAN_BAND:
        result.notes.append("아티아 무기는 여러 무기가 같은 모델을 함께 씁니다. 옮긴 모습이 다른 아티아 무기에도 보일 수 "
                            "있습니다")
    if sounds:
        result.notes.append(f"소리 파일은 옮기지 않습니다 (게임이 다른 {'무기' if weapon else '장비'}의 "
                            "소리로 읽지 않습니다)")
    unnamed = sum(1 for item in keeping if item.path is None)
    if unnamed and not unknown:
        result.notes.append(f"PAK 안의 이름을 알 수 없는 파일 {unnamed}개는 옮기지 못해 그대로 넣습니다. "
                            "원래 장비의 파일이라면 원래 장비에도 그대로 적용됩니다")
    if outside:
        result.notes.append(f"게임 폴더(natives) 밖의 파일 {outside}개(스크립트·플러그인 등)는 새 모드에 넣지 않습니다")
    return result


def _labels(parts: Iterable[str]) -> str:
    """"머리·몸통": part labels in ARMOR_PARTS order."""
    return "·".join(PART_LABELS.get(part, part) for part in sorted(parts, key=_part_order))


def _part_order(part: str) -> int:
    order = ARMOR_PARTS + WEAPON_PARTS
    return order.index(part) if part in order else len(order)


def _part_label(body: str, part: str) -> str:
    """"여성 몸통" (a body's part), "본체" (a weapon's model)."""
    name = PART_LABELS.get(part, part)
    return f"{BODIES[body]} {name}" if body in BODIES else name


def _key_order(key: tuple[str, str]) -> tuple[int, str]:
    return _part_order(key[1]), key[0]


def _streaming(path: str) -> bool:
    return path.replace("\\", "/").lstrip("/").lower().startswith("natives/stm/streaming/")


def _model_stem(slot: Slot, body: str, part: str) -> str:
    """The model and material a part's own prefab loads, without natives/STM/ and the extension:
    ``Art/Model/Character/ch03/034/001/2/ch03_034_0012``, ``Art/Model/Item/it03/00/0009/it0300_0009_0``."""
    if slot.kind == "armor":
        (m, s), ch, d = slot.ids(), "ch02" if body == "m" else "ch03", _PART_DIGITS[part]
        return f"Art/Model/Character/{ch}/{m}/{s}/{d}/{ch}_{m}_{s}{d}"
    t, ss, n = slot.ids()
    return f"Art/Model/Item/it{t}/{ss}/{n}/it{t}{ss}_{n}_{'0' if part == 'main' else '1'}"


def _versions_of(ext: str, paths: Iterable[str]) -> list[str]:
    """The version numbers to try for a file of ``ext``: the game's (the bundled list's), then those of
    the mod's own files of that type."""
    rx = re.compile(rf"\.{re.escape(ext)}\.(\d+)$", re.IGNORECASE)
    found = [m[1] for m in map(rx.search, paths) if m]
    extra = (str(_MDF2_VERSION),) if ext == "mdf2" else _PREFAB_VERSIONS if ext == "pfb" else ()
    return list(dict.fromkeys([*versions().get(ext, ()), *extra, *found]))


def _read_game(game, path: str) -> bytes:
    data = game.read(path)
    if not data:
        raise RetargetError(f"게임 파일을 읽지 못했습니다: {path}")
    return bytes(data)


def _fills(result: Plan, moving: list[Item], renamer: Renamer | MixedRenamer, game, contents: dict,
           shipped: set[str]) -> tuple[list[Item], set[tuple[str, str]], dict[int, str]]:
    """A's game files that complete what a chosen part (armor per body, weapon per model) shows at the
    target, put at B's paths like the moved files:
    - a part without a prefab of the mod's own: the target's own prefab loads ``<stem>.mesh`` and
      ``<stem>.mdf2`` together, so a mod with only the model (its material names are A's), only the
      material or only textures (made for A's model) would put them next to the target's other half:
      A's other half;
    - a part with one: the prefab loads what it names.  When the part moves textures (or gpbf) that
      nothing moved or kept reads, the mod relies on a material of A's game that the prefab names to
      read them (a retexture at A's own paths): that material, so the moved prefab names its copy and
      the copy the moved textures.  A's game model the prefab names stays as it is (it still exists).
    A material read here goes into ``contents`` like the mod's.  ``shipped``: the normalized paths of the
    mod's files.  (fills, the (body, part)s that could not be completed: errors, armor parts in
    Plan.bad_parts, id(fill) -> the part it completes: it goes to that part's target unless it is a file of
    another chosen part)."""
    source = result.source
    weapon = source.kind == "weapon"
    groups: dict[tuple[str, str], list[Item]] = {}
    for item in moving:
        t = classify(item.path)
        if t is None or t.kind != source.kind or t.part not in renamer.parts:
            continue  # (files of a part not chosen come only because moved files use them)
        groups.setdefault(("" if weapon else t.key[0], t.part), []).append(item)
    paired: list[Item] = []  # the halves of a model and its material
    named: list[Item] = []  # the materials prefabs of the mod's own name
    paired_parts, named_parts, failed = [], [], set()
    completes: dict[int, str] = {}
    read = {normalize(ref) for _data, refs in contents.values() for ref in refs}  # what the moved and kept files use

    def find(base: str, ext: str, group: list[Item]) -> str | None:
        """A's game file ``base`` (natives/STM/..., with its extension ``ext``) with the version the game has."""
        return next((p for p in (f"{base}.{v}" for v in _versions_of(ext, (it.path for it in group)))
                     if game.has(p)), None)

    def fill_item(path: str, companion: Item) -> Item:
        """A's game file at ``path`` (in the pak of ``companion``); a material is read now, its references
        followed and rewritten like the mod's (RetargetError and others when it does not read)."""
        fill = Item(path, lambda p=path: _read_game(game, p), companion.pak,
                    path_hash(full_path(path)) if companion.pak is not None else None)
        if _kind(path):
            data = _read_game(game, path)
            contents[id(fill)] = (data, references(data, path))
        return fill

    # the parts without a prefab of the mod's own first: what A's materials put there read counts below
    for (body, part), group in sorted(groups.items(), key=lambda kv: (any(_own_prefab(it.path) for it in kv[1]),
                                                                       _key_order(kv[0]))):
        prefabs = [item for item in group if _own_prefab(item.path)]
        new: list[Item] = []
        problem = None if game is not None else "게임 파일을 읽을 수 없습니다"
        try:
            if prefabs:
                why = "옮기는 텍스처를 쓰는 재질이 모드에 없어 모드의 프리팹이 불러오는 원래 장비의 게임 재질(.mdf2)을 함께 넣어야 하는데"
                unread = {normalize(it.path) for it in group if normalize(it.path).endswith((".tex", ".gpbf"))} - read
                have = shipped | {normalize(fill.path) for fill in paired}
                wanted: dict[str, str] = {}  # normalized -> the reference: A's materials the mod does not ship
                for prefab in prefabs:
                    for ref in contents.get(id(prefab), (b"", []))[1]:
                        norm = normalize(ref)
                        if norm.endswith(".mdf2") and norm not in have and renamer.rename(ref, all_parts=True):
                            wanted.setdefault(norm, ref)
                if not unread or not wanted:
                    continue
                for ref in wanted.values() if problem is None else ():
                    base = full_path(ref)
                    path = find(base, "mdf2", group)
                    if path is None:
                        problem = f"게임에서 찾지 못했습니다 ({base.rpartition('/')[2]})"
                        break
                    fill = fill_item(path, prefabs[0])
                    if unread & {normalize(r) for r in contents[id(fill)][1]}:
                        new.append(fill)
                    else:  # it does not read them (nor did it at the original equipment)
                        del contents[id(fill)]
                if problem is None and not new:
                    continue
            else:
                why = "모델(.mesh)과 재질(.mdf2)이 짝으로 들어 있지 않아 원래 장비의 게임 파일로 채워야 하는데"
                stem = _model_stem(source, body, part)
                low = stem.lower()
                mesh = mdf2 = twin = textures = False
                for item in group:
                    norm = normalize(item.path)
                    if norm == low + ".mesh":
                        twin, mesh = (True, mesh) if _streaming(item.path) else (twin, True)
                    elif norm == low + ".mdf2":
                        mdf2 = True
                    elif norm.startswith(low.rpartition("/")[0] + "/") and norm.endswith(".tex"):
                        textures = True
                if (mesh and mdf2) or not (mesh or mdf2 or twin or textures):
                    continue
                companion = next((it for it in group if normalize(it.path).startswith(low + ".")), group[0])
                for ext in [e for e, have in (("mdf2", mdf2), ("mesh", mesh)) if not have and problem is None]:
                    path = find(f"natives/STM/{stem}.{ext}", ext, group)
                    if path is None:
                        problem = f"게임에서 찾지 못했습니다 ({stem.rpartition('/')[2]}.{ext})"
                        break
                    new.append(fill_item(path, companion))
                    twin_path = streaming_twin(path) if ext == "mesh" and not twin else None
                    if twin_path and game.has(twin_path):
                        new.append(fill_item(twin_path, companion))
        except Exception as exc:  # noqa: BLE001 - a broken game file
            problem = f"게임 파일을 읽지 못했습니다 ({_reason(exc)})"
        name = _part_label(body, part)
        if problem is not None:
            for fill in new:
                contents.pop(id(fill), None)
            result.errors.append(f"{name}: {why}, {problem}")
            failed.add((body, part))
            if not weapon:
                result.bad_parts.add(part)
            continue
        (named if prefabs else paired).extend(new)
        (named_parts if prefabs else paired_parts).append(name)
        completes.update((id(fill), part) for fill in new)
        read.update(normalize(ref) for fill in new for ref in contents.get(id(fill), (b"", []))[1])
    if paired:
        kinds = "·".join(label for ext, label in (("mesh", "모델"), ("mdf2", "재질"))
                         if any(normalize(fill.path).endswith("." + ext) for fill in paired))
        result.notes.append(f"{'·'.join(paired_parts)}: 모드에 모델(.mesh)과 재질(.mdf2)이 짝으로 들어 있지 않아, 원래 "
                            f"장비의 게임 파일 {len(paired)}개({kinds})를 함께 넣어 짝을 맞춥니다")
    if named:
        result.notes.append(f"{'·'.join(named_parts)}: 옮기는 텍스처를 쓰는 재질이 모드에 없어, 모드의 프리팹이 불러오는 "
                            f"원래 장비의 게임 재질 {len(named)}개를 함께 넣어 옮긴 텍스처를 쓰게 합니다")
    return paired + named, failed, completes


def _meshless_notes(result: Plan, new_paths: list[str], failed: set[str]) -> None:
    """A chosen part that moves files but no model (physics, hair settings...) and no prefab of the mod's
    own: they go onto the target's own model.  (A prefab of the mod's own loads the model it names, A's
    game model when the mod has none, as at the original equipment.)"""
    shown: dict[str, bool] = {}  # part -> a model or a prefab of the mod's own lands there
    for new in new_paths:
        t = classify(new)
        if t is not None and t.kind == result.target.kind and t.part in result.parts:
            shown[t.part] = shown.get(t.part, False) or normalize(new).endswith(".mesh") or _own_prefab(new)
    for part in sorted(shown, key=_part_order):
        if not shown[part] and part not in failed:
            result.notes.append(f"{PART_LABELS.get(part, part)}: 모드에 모델(.mesh)이 없어 대상 장비의 모델에 원래 "
                                "장비용 파일을 씌웁니다. 모양이 맞지 않을 수 있습니다")


_BODY_FOLDER = re.compile(r"^art/model/character/ch0([23])/")


def _body_notes(result: Plan, moved_paths: list[str], new_paths: list[str], failed: set[tuple[str, str]]) -> None:
    """Files moved into one body's model folder (ch02 male, ch03 female) for a part the mod has a model
    of only for the other body: the target's prefab of that body may load them (the game keeps the
    physics of some female parts under ch02, and one body's material may read the other's textures).
    (A part without any model gets _meshless_notes.)"""
    if result.target.kind != "armor":
        return
    meshes = set()
    for new in new_paths:
        t = classify(new)
        if t is not None and t.kind == "armor" and normalize(new).endswith(".mesh"):
            meshes.add((t.key[0], t.part))
    modelled = {part for _body, part in meshes}
    files: dict[str, Counter] = {}
    for new in moved_paths:
        m, t = _BODY_FOLDER.match(normalize(new)), classify(new)
        if m and t is not None and t.kind == "armor" and t.part in result.parts and t.part in modelled:
            body = BODY_CH[m[1]]
            if (body, t.part) not in meshes and (body, t.part) not in failed:
                files.setdefault(body, Counter())[t.part] += 1
    for body, parts in sorted(files.items()):
        labels = "·".join(PART_LABELS.get(p, p) for p in sorted(parts, key=_part_order))
        ch = "ch02" if body == "m" else "ch03"
        whose = "그 부위를 옮길 장비의" if result.mixed else "대상 장비의"
        result.notes.append(f"모드에 {BODIES[body]}용 {labels} 모델이 없는데 {BODIES[body]}용 폴더({ch})의 파일 "
                            f"{sum(parts.values())}개도 옮깁니다. {whose} {BODIES[body]}용에도 영향을 줄 수 있습니다")


# ---- what the game says about the target ----------------------------------------------------------
def _prefabs(slot: Slot, body: str, part: str, more: Iterable[str] = ()) -> list[str]:
    """Where the game keeps the target's own prefab of a part, most likely first (a few female slinger
    prefabs are named after the male body).  ``more``: other version numbers to try."""
    pfb = list(dict.fromkeys([*(versions().get("pfb") or _PREFAB_VERSIONS), *more]))
    if slot.kind == "armor":
        m, s = slot.ids()
        folder = (f"natives/STM/GameDesign/Equip/_Prefab/Armor/{'Female' if body == 'f' else 'Male'}/{m}/{s}/"
                  f"{part.capitalize()}")
        chs = ("ch03", "ch02") if body == "f" else ("ch02", "ch03")
        return [f"{folder}/{ch}_{m}_{s}{_PART_DIGITS[part]}.pfb.{v}" for ch in chs for v in pfb]
    t, ss, n = slot.ids()
    k = "0" if part == "main" else "1"
    return [f"natives/STM/GameDesign/Equip/_Prefab/Weapon/wp{t}/{ss}/{n}/it{t}{ss}_{n}_{k}.pfb.{v}" for v in pfb]


def _check_game(result: Plan, placed: list[tuple[str, Slot]], game) -> None:
    """Errors and notes from the targets' own files: does the target of a part have it, does its prefab
    load the moved model (and the moved physics), and does a prefab of the mod's own land where the
    target keeps its prefab?  ``placed``: (new path, the target it went to) of every file put at a target.
    A part an error names goes into Plan.bad_parts (armor)."""
    weapon = result.source.kind == "weapon"
    # (target, body, part) -> ext -> normalized new paths
    groups: dict[tuple[Slot, str, str], dict[str, set[str]]] = {}
    by_part: dict[tuple[Slot, str], dict[str, set[str]]] = {}  # the same over both bodies
    own: dict[tuple[Slot, str, str], list[str]] = {}  # -> the new paths of the mod's own prefabs
    for new, slot in placed:
        t = classify(new)
        if t is None or t.kind != result.source.kind or not t.part:
            continue
        norm = normalize(new)
        ext = norm.rpartition(".")[2]
        body = "" if weapon else t.key[0]
        groups.setdefault((slot, body, t.part), {}).setdefault(ext, set()).add(norm)
        by_part.setdefault((slot, t.part), {}).setdefault(ext, set()).add(norm)
        if _own_prefab(new):
            own.setdefault((slot, body, t.part), []).append(new)
    checked = sorted({key for key, got in groups.items() if "mesh" in got} | set(own),
                     key=lambda key: (_key_order(key[1:]), key[0]))
    if game is None:
        if checked:
            result.notes.append("게임 파일을 읽을 수 없어 대상 장비에 그 부위가 있는지 확인하지 못했습니다")
        return
    for slot, body, part in checked:
        name = _part_label(body, part)
        optional = weapon and part == "sub"  # the main model still shows

        def fail(text: str, optional: bool = optional, part: str = part) -> None:
            if optional:
                result.notes.append(text)
                return
            result.errors.append(text)
            if not weapon and part in result.parts:
                result.bad_parts.add(part)

        missing = ("대상 장비에는 보조 모델이 없어 옮긴 보조 모델은 보이지 않습니다" if optional
                   else f"대상 장비에는 {_josa(name, '이가')} 없습니다")
        mine = own.get((slot, body, part))
        try:
            if mine:  # the mod's own prefab moves too: the game must ask for it where it lands
                lost = next((p for p in mine if not game.has(p)), None)
                if lost is not None:
                    theirs = next((p for p in _prefabs(slot, body, part, _versions_of("pfb", mine))
                                   if game.has(p)), None)
                    fail(missing if theirs is None else
                         f"대상 장비의 {name} 프리팹은 이름({theirs.rpartition('/')[2]})이 모드의 프리팹"
                         f"({lost.rpartition('/')[2]})과 달라, 옮긴 프리팹을 게임이 불러오지 않습니다")
                continue
            prefab = next((p for p in _prefabs(slot, body, part) if game.has(p)), None)
            if prefab is None:
                fail(missing)
                continue
            data = game.read(prefab)
            info = rsz.parse(data) if data else None
        except Exception as exc:  # noqa: BLE001 - a broken game file must not stop the plan
            result.notes.append(f"대상 장비의 {name} 파일을 읽지 못해 확인하지 못했습니다 ({_reason(exc)})")
            continue
        if info is None:
            result.notes.append(f"대상 장비의 {name} 프리팹을 읽지 못해 옮긴 모델을 불러오는지 확인하지 못했습니다")
            continue
        loads = {normalize(r) for r in info.resources() if r}
        if not loads & by_part[(slot, part)]["mesh"]:
            fail(f"이 무기는 다른 무기의 모델을 함께 써서 옮긴 모델이 보이지 않습니다 ({name})" if weapon
                 else f"대상 장비의 {_josa(name, '은는')} 다른 장비의 모델을 불러와서, 옮긴 모델이 보이지 않습니다",
                 optional=False)
            continue
        chains = by_part[(slot, part)].get("chain2", set())
        if chains and not loads & chains:
            result.notes.append(f"대상 장비의 {_josa(name, '은는')} 모드의 흔들림 파일(.chain2)을 불러오지 않아 "
                                "흔들림이 빠질 수 있습니다")
        if any(r.endswith(".gpuc") for r in loads):
            result.notes.append(f"대상 장비의 {_josa(name, '은는')} GPU 옷감(.gpuc)을 써서 모드 모델과 어긋날 수 "
                                "있습니다")


# ---- building the new mod -----------------------------------------------------------------------------
@dataclass
class Built:
    files: list[str] = field(default_factory=list)  # game rels written under out_dir (rebuilt paks by their rel)
    moves: list[tuple[str, str]] = field(default_factory=list)  # (new rel, old rel) of every moved file
    pak_moves: list[tuple[int, int]] = field(default_factory=list)  # (new hash, old hash) of moved pak entries


def _out_path(out_dir: Path, rel: str) -> Path:
    parts = rel.replace("\\", "/").split("/")
    if not rel or ":" in rel or rel.startswith(("/", "\\")) or any(p in ("", ".", "..") for p in parts):
        raise RetargetError(f"새 모드 폴더 밖을 가리키는 경로입니다: {rel}")
    return out_dir.joinpath(*parts)


def build(plan: Plan, items: list[Item], out_dir: Path, progress=None) -> Built:
    """Write the new mod's files under ``out_dir`` (a new, empty folder): moved loose files at their new
    paths, kept ones at their own, each root .pak again with its moved entries re-keyed (the order of
    its table kept, dropped entries left out, A's game files of Plan.fills added at the end,
    uncompressed) and read back.  ``items``: the same items_of the plan was made from.  RetargetError
    (Korean) when the plan has errors or a file cannot be made; what was written so far is left for the
    caller to remove."""
    if not plan.ok:
        raise RetargetError("\n".join(plan.errors) or "옮길 파일이 없습니다")
    progress = progress or (lambda done, total, msg: None)
    out_dir = Path(out_dir)
    renamer = renamer_of(plan.source, plan.targets or {part: plan.target for part in plan.parts})
    moved = mover([(m.new, m.old) for m in plan.moves], renamer)
    moves = {(m.pak, m.old): m for m in plan.moves if m.how != "fill"}
    fills = {(m.pak, m.old): m for m in plan.moves if m.how == "fill"}
    kept = set(plan.kept)
    built, found = Built(), set()

    def content(item: Item, move: Move | None) -> bytes | None:
        """The item's bytes with its references rewritten, None when they do not change."""
        if move is not None:
            return rewrite(item.read(), item.path, moved, renamer)[0] if move.how != "copy" else None
        if item.path is None or not _item_kind(item):  # an unnamed entry is never rewritten
            return None
        try:
            new, n = rewrite(item.read(), item.path, moved, None)
        except RetargetError as exc:  # the plan noted it: copied as it is
            log.info("%s: %s", _label(item), exc)
            return None
        return new if n else None

    def filled(fill: Item) -> tuple[Move, bytes]:
        """A's game file of Plan.fills: its move and bytes (a material pointed at the moved files)."""
        move = fills.get((fill.pak, fill.path))
        if move is None:
            raise RetargetError(f"모드 파일이 계획과 달라 새 모드를 만들지 못했습니다: {fill.path}")
        data = fill.read()
        return move, rewrite(data, fill.path, moved, None)[0] if _kind(fill.path) else data

    loose = [item for item in items if item.pak is None]
    paks: dict[str, list[Item]] = {}
    for item in items:
        if item.pak is not None:
            paks.setdefault(item.pak, []).append(item)
    total, done = len(items) + len(plan.fills), 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for item in loose:
        done += 1
        move = moves.get((None, item.path))
        if move is None and item.path not in kept:
            continue
        rel = move.new if move else item.path
        progress(done, total, f"새 모드 만드는 중... {rel.rpartition('/')[2]}")
        target = _out_path(out_dir, rel)
        data = content(item, move)
        target.parent.mkdir(parents=True, exist_ok=True)
        if data is not None:
            target.write_bytes(data)
        elif item.stored is not None:
            shutil.copyfile(item.stored, target)
        else:
            target.write_bytes(item.read())
        built.files.append(rel)
        if move is not None:
            built.moves.append((move.new, move.old))
            found.add((None, item.path))
    for fill in plan.fills:
        if fill.pak is not None:
            continue
        done += 1
        move, data = filled(fill)
        progress(done, total, f"새 모드 만드는 중... {move.new.rpartition('/')[2]}")
        target = _out_path(out_dir, move.new)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        built.files.append(move.new)
        built.moves.append((move.new, move.old))
        found.add((None, fill.path))

    for rel, entries in paks.items():
        pak = entries[0].entry[0]
        by_hash = {item.hash64: item for item in entries}
        rows, order, changed = [], [], {}
        for h in dict.fromkeys(pak.order):
            item = by_hash.get(h)
            done += 1
            if item is None:
                continue
            move = moves.get((rel, item.path)) if item.path is not None else None
            if move is None and _kept_name(item) not in kept:
                continue
            progress(done, total, f"새 모드 만드는 중... {rel}")
            i = item.entry[1]
            _offset, _csize, dsize, attrs = pak.entry(i)
            if pak.chunk_offsets is not None or attrs & _ENCRYPTED:
                raise RetargetError(f"{rel}: 이 형식의 pak은 다시 만들 수 없습니다")
            compression = attrs & 0xF
            key = path_hash(full_path(move.new)) if move is not None else h
            data = content(item, move)
            if data is not None:
                rows.append((key, compress_as(compression, data), len(data), compression))
                changed[key] = data
            else:
                rows.append((key, lambda pak=pak, i=i: pak.read_raw(i), dsize, compression))
            order.append(key)
            if move is not None:
                built.moves.append((move.new, move.old))
                built.pak_moves.append((key, h))
                found.add((rel, item.path))
        for fill in (f for f in plan.fills if f.pak == rel):
            done += 1
            move, data = filled(fill)
            progress(done, total, f"새 모드 만드는 중... {rel}")
            key = path_hash(full_path(move.new))
            rows.append((key, data, len(data), COMPRESSION_NONE))
            changed[key] = data
            order.append(key)
            built.moves.append((move.new, move.old))
            built.pak_moves.append((key, fill.hash64 if fill.hash64 is not None else path_hash(full_path(fill.path))))
            found.add((rel, fill.path))
        if not rows:
            continue
        if len(set(order)) != len(order):
            raise RetargetError(f"{rel}: 옮긴 파일이 pak 안의 다른 파일과 겹칩니다")
        target = _out_path(out_dir, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_pak(rows, target)
        check = PakFile(target)
        if list(check.order) != order or any(check.read_hash(k) != d for k, d in changed.items()):
            raise RetargetError(f"{rel}: 새로 만든 pak을 다시 읽어 보니 내용이 다릅니다")
        built.files.append(rel)

    missing = [m.old for m in plan.moves if (m.pak, m.old) not in found]
    if missing:
        raise RetargetError(f"모드 파일이 계획과 달라 새 모드를 만들지 못했습니다: {missing[0]}")
    built.files.sort(key=str.lower)
    progress(total, total, "새 모드 파일을 만들었습니다")
    return built
