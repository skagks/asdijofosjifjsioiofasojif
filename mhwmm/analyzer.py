"""Work out how the contents of a mod archive map onto the game folder.

Mods for Monster Hunter Wilds come in a handful of layouts:

* loose files: ``natives/STM/...`` (needs REFramework's loose file loader)
* PAK patches: ``*.pak`` files that get numbered into the patch chain
* REFramework scripts/plugins: ``reframework/autorun/*.lua``, ``reframework/plugins/*.dll``
  (often shipped without the leading ``reframework/`` folder)
* DLL mods such as REFramework itself (``dinput8.dll``) in the game root
* Fluffy Mod Manager packages: one folder per option, each with a ``modinfo.ini``

Archives often wrap these in one or more extra folders, and some contain several
options.  Every folder that holds mod content becomes a ``Variant``; an archive with
several of them is installed as one mod whose options can be switched later.
"""

from __future__ import annotations

import os
import posixpath
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from . import fsutil
from .modinfo import ModInfo, parse_modinfo
from .nexus import parse_source_name

JUNK_DIRS = {"__macosx", ".git", ".svn", "fomod"}
JUNK_FILES = {".ds_store", "thumbs.db", "desktop.ini"}
MODINFO = "modinfo.ini"

GAME_DIRS = {"natives", "reframework"}
REFW_DIRS = {"autorun", "plugins"}
REFW_EXTRA_DIRS = {"data", "fonts", "images"}
EXTRA_GAME_DIRS = {"reshade-shaders"}
ROOT_MARKER_EXTS = {".pak", ".dll", ".asi"}
ROOT_FILE_EXTS = ROOT_MARKER_EXTS | {".ini"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def clean_archive_name(stem: str) -> tuple[str, str]:
    """Strip the Nexus Mods download suffix: ``Name-123-1-2-1740000000`` -> (``Name``, ``1.2``)."""
    ref = parse_source_name(stem)
    if ref and ref.name:
        return ref.name, ref.version
    return stem, ""


def classify(targets: Iterable[str]) -> set[str]:
    """Kinds of content in a list of game-relative paths."""
    kinds: set[str] = set()
    for t in targets:
        low = t.lower()
        if "/" not in low:
            if low.endswith(".pak"):
                kinds.add("pak")
            elif low == "dinput8.dll":
                kinds.add("reframework")
            elif low.endswith((".dll", ".asi")):
                kinds.add("dll")
            else:
                kinds.add("other")
        elif low.startswith("natives/"):
            kinds.add("loose")
        elif low.startswith("reframework/"):
            kinds.add("script")
        else:
            kinds.add("other")
    return kinds


@dataclass
class Variant:
    name: str
    root: str  # folder inside the archive ("" = archive root)
    files: list[tuple[str, str]] = field(default_factory=list)  # (archive path, game path)
    skipped: list[str] = field(default_factory=list)
    info: ModInfo | None = None
    screenshot: str | None = None  # archive path of a preview image
    version: str = ""
    package: str = ""  # name of the mod this variant belongs to
    option: str = ""  # name of the variant as an option of that mod

    @property
    def targets(self) -> list[str]:
        return [dst for _, dst in self.files]

    @property
    def kinds(self) -> set[str]:
        return classify(self.targets)


def list_files(base) -> list[str]:
    """Relative posix paths of all regular files below ``base`` (junk excluded)."""
    top = fsutil.longpath(base)
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = sorted(
            d for d in dirnames
            if d.lower() not in JUNK_DIRS and not os.path.islink(os.path.join(dirpath, d))
        )
        rel_dir = os.path.relpath(dirpath, top)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        for fn in sorted(filenames):
            if fn.lower() in JUNK_FILES or fn.startswith("._"):
                continue
            if os.path.islink(os.path.join(dirpath, fn)):
                continue
            out.append(f"{rel_dir}/{fn}" if rel_dir else fn)
    return out


def _parent(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _is_under(rel: str, root: str) -> bool:
    return root == "" or rel.startswith(root + "/")


def _rel_to(rel: str, root: str) -> str:
    return rel if root == "" else rel[len(root) + 1:]


def _depth(root: str) -> int:
    return 0 if root == "" else root.count("/") + 1


def _ext(name: str) -> str:
    return posixpath.splitext(name.lower())[1]


def _common_dir(paths: list[str]) -> str:
    split = [p.split("/") if p else [] for p in paths]
    common: list[str] = []
    for parts in zip(*split):
        if all(p == parts[0] for p in parts):
            common.append(parts[0])
        else:
            break
    return "/".join(common)


def _marker_root(path: str) -> str | None:
    """Folder that acts as the game root for ``path``, if it carries mod content."""
    parts = path.split("/")
    low = [p.lower() for p in parts]
    for i in range(len(parts) - 1):
        if low[i] in GAME_DIRS:
            return "/".join(parts[:i])
    for i in range(len(parts) - 1):
        if low[i] in REFW_DIRS:
            return "/".join(parts[:i])
    if _ext(low[-1]) in ROOT_MARKER_EXTS:
        return "/".join(parts[:-1])
    return None


def _detect_mode(rels: list[str]) -> str:
    tops = set()
    root_exts = set()
    for rel in rels:
        if "/" in rel:
            tops.add(rel.split("/", 1)[0].lower())
        else:
            root_exts.add(_ext(rel))
    if tops & GAME_DIRS or root_exts & ROOT_MARKER_EXTS:
        return "game"
    if tops & REFW_DIRS:
        return "refw"
    return "game"


def _map_root(rels: list[str], mode: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Map paths relative to a content root to game paths. Returns (mapping, skipped)."""
    mapped: list[tuple[str, str]] = []
    skipped: list[str] = []
    for rel in rels:
        parts = rel.split("/")
        top = parts[0].lower()
        rest = "/".join(parts[1:])
        name = parts[-1].lower()
        dst: str | None = None
        if mode == "autorun":
            if len(parts) > 1 or _ext(name) == ".lua":
                dst = "reframework/autorun/" + rel
        elif len(parts) == 1:
            if name == MODINFO:
                dst = None
            elif _ext(name) in ROOT_FILE_EXTS:
                dst = rel
            elif _ext(name) == ".lua":
                dst = "reframework/autorun/" + rel
        elif top == "natives":
            dst = "natives/" + rest
        elif top == "reframework":
            dst = "reframework/" + rest
        elif top in REFW_DIRS or (mode == "refw" and top in REFW_EXTRA_DIRS):
            dst = f"reframework/{top}/{rest}"
        elif top in EXTRA_GAME_DIRS:
            dst = rel
        if dst is None:
            skipped.append(rel)
        else:
            mapped.append((rel, dst))
    return mapped, skipped


def analyze(base, archive_name: str = "") -> list[Variant]:
    """Find every installable variant inside the extracted folder ``base``."""
    base = Path(base)
    files = list_files(base)
    base_name, nexus_version = clean_archive_name(archive_name or base.name)

    infos: dict[str, ModInfo] = {}
    for f in files:
        if posixpath.basename(f).lower() == MODINFO:
            infos[_parent(f)] = parse_modinfo(base / f)

    roots: set[str] = set(infos)
    for f in files:
        r = _marker_root(f)
        if r is not None:
            roots.add(r)

    forced_mode: dict[str, str] = {}
    if not roots:
        lua = [f for f in files if _ext(f) == ".lua"]
        if not lua:
            return []
        root = _common_dir([_parent(f) for f in lua])
        roots.add(root)
        forced_mode[root] = "autorun"

    deepest_first = sorted(roots, key=_depth, reverse=True)
    members: dict[str, list[str]] = {r: [] for r in roots}
    for f in files:
        for r in deepest_first:
            if _is_under(f, r):
                members[r].append(f)
                break

    variants: list[Variant] = []
    for root in sorted(roots, key=lambda r: (_depth(r), r.lower())):
        rels = [_rel_to(f, root) for f in members[root]]
        mode = forced_mode.get(root) or _detect_mode(rels)
        mapped, skipped = _map_root(rels, mode)
        if not mapped:
            continue
        prefix = f"{root}/" if root else ""
        v = Variant(
            name="",
            root=root,
            files=[(prefix + src, dst) for src, dst in mapped],
            skipped=[prefix + s for s in skipped],
            info=infos.get(root),
        )
        inherited = _inherited_info(root, infos)
        v.version = (v.info.version if v.info else "") or (inherited.version if inherited else "") or nexus_version
        v.screenshot = _find_screenshot(root, rels, v.info or inherited, files)
        variants.append(v)

    _name_variants(variants, infos, base_name)
    return variants


def _inherited_info(root: str, infos: dict[str, ModInfo]) -> ModInfo | None:
    r = root
    while r:
        r = _parent(r)
        if r in infos:
            return infos[r]
    return None


def _find_screenshot(root: str, rels: list[str], info: ModInfo | None, all_files: list[str]) -> str | None:
    prefix = f"{root}/" if root else ""
    lookup = {f.lower(): f for f in all_files}
    if info and info.screenshot:
        for candidate in (prefix + info.screenshot, info.screenshot):
            if candidate.lower() in lookup:
                return lookup[candidate.lower()]
    for rel in rels:
        if "/" not in rel and _ext(rel) in IMAGE_EXTS:
            return prefix + rel
    return None


def _name_variants(variants: list[Variant], infos: dict[str, ModInfo], base_name: str) -> None:
    if len(variants) == 1:
        v = variants[0]
        inherited = _inherited_info(v.root, infos)
        v.name = (v.info and v.info.name) or (inherited and inherited.name) or base_name
        v.package = v.option = v.name
        return
    common = _common_dir([v.root for v in variants])
    for v in variants:
        inherited = _inherited_info(v.root, infos)
        parent = (inherited and inherited.name) or base_name
        rel = _rel_to(v.root, common) if v.root != common else ""
        v.package = parent
        if v.info and v.info.name:
            v.name = v.option = v.info.name
        else:
            v.option = rel.replace("/", " / ") if rel else parent
            v.name = f"{parent} - {v.option}" if rel else parent
    for attr in ("name", "option"):
        seen: dict[str, int] = {}
        for v in variants:
            key = getattr(v, attr).casefold()
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                setattr(v, attr, f"{getattr(v, attr)} ({seen[key]})")


def package_name(variants: list[Variant]) -> str:
    """Name for the single mod that holds all ``variants`` as options."""
    names = Counter(v.package or v.name for v in variants)
    return names.most_common(1)[0][0] if names else ""


def raw_variant(base, archive_name: str = "") -> Variant:
    """Fallback: install the archive contents as-is (minus wrapper folders)."""
    base = Path(base)
    files = [f for f in list_files(base) if posixpath.basename(f).lower() != MODINFO]
    prefix = ""
    while files and all("/" in f[len(prefix):] for f in files):
        first = {f[len(prefix):].split("/", 1)[0] for f in files}
        if len(first) != 1:
            break
        prefix += first.pop() + "/"
    name, version = clean_archive_name(archive_name or base.name)
    return Variant(
        name=name,
        root=prefix.rstrip("/"),
        files=[(f, f[len(prefix):]) for f in files],
        version=version,
        package=name,
        option=name,
    )
