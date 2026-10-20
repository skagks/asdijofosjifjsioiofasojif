"""Which equipment a mod changes the look of, from the files it puts into the game.

Loose files are matched by their paths (paths.classify).  The entries of a .pak mod are named by
their path hashes (pathlist.PathIndex).  For what is left, the mod's own materials and prefabs
are read (small files only): the paths they point at are hashed in turn, which names a mod's own
textures and meshes; an unnamed material or prefab belongs to the equipment most of its textures
or meshes do (a mod's material points at its textures, which live in its model folder); and a
file that still has no equipment (or a weapon file of no known part) counts for the one
equipment whose files point at it.

A prefab that loads the meshes of another equipment (a look swap, e.g. a Chatacabra helm prefab
pointing at the Nerscylla helm mesh) is reported as a redirect.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from ..deploy import is_root_pak
from ..matfix import MatfixError, rsz
from ..matfix.mdf2 import Mdf2File, detect_version
from ..matfix.pak import PakFile, path_hash
from .model import Target
from .pathlist import PREFIX, PathIndex, game_paths, streaming_twin, versions
from .paths import classify, normalize

log = logging.getLogger(__name__)

SCAN_FORMAT = 2  # raise when results can change, so that cached scans are made again (2: only game textures count for the other body)
MAX_READ = 4 << 20  # larger files are never read: materials and prefabs are far smaller
MDF2_MAGIC = b"MDF\x00"
PFB_MAGIC = b"PFB\x00"
MDF2_VERSION = 45  # tried first on a material without a name
CONTENT_EXTS = ("mdf2", "pfb")
# the same two by their first bytes: .user/.scn files are not read (a scene places the meshes of
# whatever stands in it, which says nothing about the equipment a mod changes)
_CONTENT_MAGICS = {MDF2_MAGIC, PFB_MAGIC}


def _why(exc: BaseException) -> str:
    """A short reason in Korean for a file that could not be read."""
    if isinstance(exc, MatfixError):
        return str(exc).rpartition(".pak: ")[2]  # the pak reader's messages, without the pak's name
    if isinstance(exc, FileNotFoundError):
        return "파일이 없습니다"
    if isinstance(exc, PermissionError):
        return "접근 권한이 없습니다"
    if isinstance(exc, OSError):
        return "읽기 오류"
    if isinstance(exc, MemoryError):
        return "너무 큽니다"
    return "손상된 파일"


@dataclass
class ModScan:
    targets: dict[Target, int] = field(default_factory=dict)  # files per equipment part
    redirects: list[tuple[Target, Target]] = field(default_factory=list)  # (prefab's own, mesh it loads)
    unknown: int = 0  # files and pak entries of no equipment
    paks: int = 0
    errors: list[str] = field(default_factory=list)
    # (a replaced game texture's own part, the other body's part that uses the same texture)
    shared: list[tuple[Target, Target]] = field(default_factory=list)

    def items(self) -> dict[Target, int]:
        """Files per equipment, parts merged."""
        out: Counter = Counter()
        for target, n in self.targets.items():
            out[target.item] += n
        return dict(out)

    def to_dict(self) -> dict:
        return {"format": SCAN_FORMAT,
                "targets": [[t.kind, t.key, t.part, n] for t, n in sorted(self.targets.items())],
                "redirects": [[[a.kind, a.key, a.part], [b.kind, b.key, b.part]] for a, b in self.redirects],
                "unknown": self.unknown, "paks": self.paks, "errors": list(self.errors),
                "shared": [[[a.kind, a.key, a.part], [b.kind, b.key, b.part]] for a, b in self.shared]}

    @classmethod
    def from_dict(cls, data) -> "ModScan | None":
        """None for a scan of another format or a damaged one."""
        try:
            if data.get("format") != SCAN_FORMAT:
                return None
            return cls({Target(str(k), str(key), str(p)): int(n) for k, key, p, n in data["targets"]},
                       [(Target(*map(str, a)), Target(*map(str, b))) for a, b in data["redirects"]],
                       int(data["unknown"]), int(data["paks"]), [str(e) for e in data["errors"]],
                       [(Target(*map(str, a)), Target(*map(str, b))) for a, b in data["shared"]])
        except (AttributeError, KeyError, TypeError, ValueError):
            return None


@dataclass(eq=False)
class _File:
    source: str  # for messages: the loose file's name, or the pak of an entry
    path: str | None  # game path: the loose file's, or what the pak entry resolved to
    size: int
    read: Callable[[], bytes]
    head: Callable[[], bytes] | None = None  # first bytes (pak entries)
    hash64: int | None = None  # pak entries
    target: Target | None = None
    magic: bytes | None = None
    refs: set[str] | None = None  # normalized paths the file points at, once read

    def ext(self) -> str:
        return normalize(self.path).rpartition(".")[2] if self.path else ""

    def wants_owner(self) -> bool:
        """Could the files that point at this one tell its equipment (or the part)?"""
        if self.hash64 is None and not self.path.replace("\\", "/").lower().startswith("natives/"):
            return False  # reframework scripts and the like: no game file points at them
        return self.target is None or (self.target.kind == "weapon" and not self.target.part)


class _Errors:
    def __init__(self, scan: ModScan):
        self.scan, self.seen = scan, set()

    def add(self, text: str) -> None:
        if text not in self.seen:
            self.seen.add(text)
            self.scan.errors.append(text)


def _loose(rel: str, stored: Path) -> _File:
    try:
        size = stored.stat().st_size
    except (OSError, ValueError):
        size = 0
    return _File(rel.replace("\\", "/").rpartition("/")[2], rel, size, stored.read_bytes)


def _pak_files(rel: str, stored: Path, index: PathIndex | None, errors: _Errors) -> list[_File]:
    try:
        pak = PakFile(stored)
    except Exception as exc:  # noqa: BLE001 - OSError, MatfixError, or a table broken in another way
        errors.add(f"{rel}: pak 파일을 읽지 못했습니다 ({_why(exc)})")
        return []
    return [_File(rel, index.lookup(h) if index is not None else None, pak.dsizes[i],
                  lambda i=i: pak.read(i), lambda i=i: pak.head(i, 4), h)
            for i, h in enumerate(pak.hashes)]


def _magic(f: _File, errors: _Errors) -> bytes:
    """The first bytes of a pak entry small enough to be a material or a prefab."""
    if f.magic is None:
        f.magic = b""
        if f.head is not None and f.size <= MAX_READ:
            try:
                f.magic = bytes(f.head()[:4])
            except Exception as exc:  # noqa: BLE001 - encrypted entry, no zstd module, broken data
                errors.add(f"{f.source}: 일부 파일을 읽지 못했습니다 ({_why(exc)})")
    return f.magic


def _read_refs(f: _File, errors: _Errors) -> set[str]:
    """Normalized paths a material (its textures) or an RSZ file (its resources) points at."""
    if f.refs is not None:
        return f.refs
    f.refs = set()
    if f.size > MAX_READ:
        return f.refs
    try:
        data = f.read()
    except Exception as exc:  # noqa: BLE001 - as in _magic
        errors.add(f"{f.source}: {'일부 ' if f.hash64 is not None else ''}파일을 읽지 못했습니다 ({_why(exc)})")
        return f.refs
    try:
        if data[:4] == MDF2_MAGIC:
            tail = f.path.rpartition(".")[2] if f.path else ""
            version = detect_version(data, prefer=int(tail) if tail.isdigit() else MDF2_VERSION)
            if version is not None:
                mdf = Mdf2File(data, version)
                f.refs = {normalize(path.text) for mat in mdf.materials for _kind, path in mat.textures if path.text}
        elif data[:4] in rsz.MAGICS:
            info = rsz.parse(data)
            if info is not None:
                f.refs = {normalize(s) for s in info.resources() if s}
    except Exception as exc:  # noqa: BLE001 - a mod's file can be broken in any way
        log.debug("%s (%s): %s", f.source, f.path, exc)
    return f.refs


def _swaps(own: Target, mesh: Target) -> bool:
    """Does a prefab of ``own`` that loads ``mesh`` show another equipment than its own?  A Seikret
    equipment prefab (equip/<NNN>) always loads ch07 models (model/<GGG>/<NNNN>): paths alone cannot
    tell which ones are its own.  An armor prefab may load the other body's model of its own design
    (the game does that for some sets, e.g. glasses)."""
    if own.kind == mesh.kind == "seikret" and own.key.startswith("equip/") and mesh.key.startswith("model/"):
        return False
    return not (own.kind == mesh.kind == "armor" and own.key[1:] == mesh.key[1:])


_TWINS: tuple[dict[tuple[str, str], list[str]], frozenset[str]] | None = None


def texture_twins() -> dict[tuple[str, str], list[str]]:
    """(armor key, part) of a folder with textures -> the other body's folders of the same design that
    have models but no textures of their own: their materials use these textures (a body wearing the
    other body's design, e.g. ch03/060/000 uses ch02/060/000's textures)."""
    return _twins()[0]


def _twins() -> tuple[dict[tuple[str, str], list[str]], frozenset[str]]:
    """texture_twins() and the game's own armor textures (normalized paths): only a mod file that
    replaces one of those changes the look of the folders that borrow it - a texture of the mod's
    own name is used by the mod's own materials only."""
    global _TWINS
    if _TWINS is None:
        meshes, textures, files = set(), set(), set()
        for p in game_paths():
            if "/model/character/ch0" not in p:
                continue
            t = classify(p)
            if t is None or t.kind != "armor" or not t.part:
                continue
            if ".mesh." in p:
                meshes.add((t.key, t.part))
            elif ".tex." in p:
                textures.add((t.key, t.part))
                files.add(normalize(p))
        twins: dict[tuple[str, str], list[str]] = {}
        for key, part in sorted(meshes - textures):
            other = ("f" if key[0] == "m" else "m") + key[1:]
            if (other, part) in textures:
                twins.setdefault((other, part), []).append(key)
        _TWINS = twins, frozenset(files)
    return _TWINS


def _vote(paths) -> Target | None:
    votes = Counter(t for t in map(classify, paths) if t is not None)
    if not votes:
        return None
    best = max(votes.values())
    return min(t for t, n in votes.items() if n == best)


def _infer(f: _File) -> Target | None:
    """What most of a material's textures or a prefab's meshes belong to."""
    refs = f.refs or set()
    return _vote([r for r in refs if r.endswith(".mesh")]) or _vote(refs)


def _name(unnamed: dict[int, list[_File]], refs: set[str]) -> None:
    """Name unnamed pak entries after the paths files point at, hashed with each version the game
    uses for the extension (and as streaming/ copies)."""
    table = versions()
    for ref in refs:
        for version in table.get(ref.rpartition(".")[2], ()):
            path = f"{PREFIX}{ref}.{version}"
            for p in filter(None, (path, streaming_twin(path))):
                for f in unnamed.pop(path_hash(p), ()):
                    f.path, f.target = p, classify(p)
        if not unnamed:
            return


def _adopt(items: list[_File]) -> bool:
    """A file that the files of one equipment point at belongs to it."""
    owners: dict[str, set[Target]] = {}
    for f in items:
        if f.refs and f.target is not None:
            for ref in f.refs:
                owners.setdefault(ref, set()).add(f.target)
    changed = False
    for f in items:
        if f.path is None or not f.wants_owner():
            continue
        found = owners.get(normalize(f.path))
        if not found:
            continue
        new = f.target
        if len(found) == 1:
            (owner,) = found
            if f.target is None or f.target.item == owner.item:
                new = owner
        elif f.target is None and len({t.item for t in found}) == 1:
            new = next(iter(found)).item  # shared by the parts of one equipment
        if new != f.target:
            f.target, changed = new, True
    return changed


def _infer_all(content: list[_File]) -> bool:
    changed = False
    for f in content:
        if f.target is None and f.refs:
            f.target = _infer(f)
            changed |= f.target is not None
    return changed


def scan_files(files: Mapping[str, Path], index: PathIndex | None) -> ModScan:
    """What one mod changes: ``files`` maps its game paths to the stored files (values may also be
    the (stored file, key) pairs of ModManager.mod_entries); root .pak files are read entry by
    entry.  Never raises for a broken file: it counts as unknown and is noted in ``errors``."""
    scan = ModScan()
    errors = _Errors(scan)
    items: list[_File] = []
    for rel, stored in files.items():
        if isinstance(stored, tuple):
            stored = stored[0]
        if is_root_pak(rel):
            scan.paks += 1
            items.extend(_pak_files(rel, Path(stored), index, errors))
        else:
            items.append(_loose(rel, Path(stored)))
    for f in items:
        f.target = classify(f.path) if f.path is not None else None

    # prefabs of known equipment: which meshes they really load
    for f in items:
        if f.target is not None and f.ext() == "pfb":
            own = f.target
            for mesh in sorted({t for r in _read_refs(f, errors) if r.endswith(".mesh") and (t := classify(r))}):
                if mesh.item != own.item and _swaps(own, mesh) and (own, mesh) not in scan.redirects:
                    scan.redirects.append((own, mesh))

    # files without equipment (or weapon files of no part): what the mod's materials and prefabs say
    if any(f.wants_owner() for f in items):
        unnamed: dict[int, list[_File]] = {}
        for f in items:
            if f.path is None:
                unnamed.setdefault(f.hash64, []).append(f)
        content: list[_File] = []
        seen: set[int] = set()
        while True:
            # named materials and prefabs first; only then the first bytes of what is still unnamed
            batch = [f for f in items if id(f) not in seen and f.path is not None and f.ext() in CONTENT_EXTS]
            if not batch:
                batch = [f for fs in unnamed.values() for f in fs
                         if id(f) not in seen and _magic(f, errors) in _CONTENT_MAGICS]
            if not batch:
                break
            refs: set[str] = set()
            for f in batch:
                seen.add(id(f))
                refs |= _read_refs(f, errors)
            content += batch
            if unnamed and refs:
                _name(unnamed, refs)
        for _round in range(3):  # an equipment found for a material can in turn own its textures
            if not (_adopt(items) | _infer_all(content)):
                break

    twins, game_textures = _twins() if any(f.target and f.target.kind == "armor" and f.ext() == "tex"
                                           for f in items) else ({}, frozenset())
    for f in items:
        if f.target is None:
            scan.unknown += 1
            continue
        scan.targets[f.target] = scan.targets.get(f.target, 0) + 1
        if f.target.kind == "armor" and f.ext() == "tex" and normalize(f.path) in game_textures:
            # a game texture the other body's model of this design uses: its look changes too
            for key in twins.get((f.target.key, f.target.part), ()):
                twin = Target("armor", key, f.target.part)
                scan.targets[twin] = scan.targets.get(twin, 0) + 1
                if (f.target, twin) not in scan.shared:
                    scan.shared.append((f.target, twin))
    return scan
