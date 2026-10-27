"""The bundled list of the game's equipment paths, and a path-hash index of it for .pak mods.

A .pak stores its files under hashes of their paths (matfix.pak.path_hash), so the entries of a
mod pak are named by hashing every path they could be: the bundled list (equip_paths.txt.xz, see
tools/make_equip_paths.py) plus the streaming/ copy of each .mesh/.tex in it, and any extra
candidates the caller adds.  Hashing some 65,000 paths in pure Python takes seconds, so the
sorted hashes are cached in a binary file keyed by the list's content.
"""

from __future__ import annotations

import hashlib
import logging
import lzma
import os
import re
import struct
import threading
from array import array
from bisect import bisect_left
from collections import Counter
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from ..matfix.pak import path_hash
from .paths import is_npc_visual_setting

log = logging.getLogger(__name__)

LIST_FILE = Path(__file__).with_name("equip_paths.txt.xz")
PREFIX = "natives/stm/"  # left out of every line of the list
STREAMING = "streaming/"
_TWIN_EXTS = (".mesh", ".tex")  # the only files the game keeps a streaming/ copy of
_CACHE_MAGIC = b"MHWMMEQP"
_CACHE_VERSION = 1
_CACHE_HEAD = struct.Struct("<8sIQ20s20s")  # magic, version, count, list key, payload sha1

_lock = threading.Lock()
_loaded: dict = {}  # "all", "npc", "versions", "version"


def _read_list() -> list[str]:
    try:
        text = lzma.decompress(LIST_FILE.read_bytes()).decode("utf-8")
    except (OSError, lzma.LZMAError, UnicodeDecodeError) as exc:
        log.warning("장비 경로 목록(%s)을 읽지 못했습니다: %s", LIST_FILE.name, exc)
        return []
    return [PREFIX + line for line in text.splitlines() if line and not line.startswith("#")]


def game_paths() -> list[str]:
    """Every bundled path: lower case, ``natives/stm/...`` with the version suffix (read once;
    do not modify the list)."""
    with _lock:
        if "all" not in _loaded:
            _loaded["all"] = _read_list()
        return _loaded["all"]


def list_version() -> str:
    """Changes whenever the bundled list does (an app update): results worked out with an older list
    are made again.  "" without a readable list."""
    with _lock:
        if "version" not in _loaded:
            try:
                _loaded["version"] = hashlib.sha1(LIST_FILE.read_bytes()).hexdigest()[:16]
            except OSError:
                _loaded["version"] = ""
        return _loaded["version"]


def npc_visual_settings() -> list[str]:
    """The bundled GameDesign/NPC/Character/<group>/<NPC id>/Data/*_VisualSetting.user paths, e.g.
    ``natives/stm/gamedesign/npc/character/main/npc102_00_001/data/npc102_00_001_00_visualsetting.user.3``
    (``split("/")``: [5] the group, [6] the NPC id)."""
    paths = game_paths()
    with _lock:
        if "npc" not in _loaded:
            _loaded["npc"] = [p for p in paths if is_npc_visual_setting(p)]
        return _loaded["npc"]


_VERSION_RE = re.compile(r"\.([a-z0-9]+)\.(\d+)$")
_DEFAULT_VERSIONS = {"tex": ("241106027",), "mesh": ("241111606",), "mdf2": ("45",), "pfb": ("18",), "user": ("3",)}


def versions() -> dict[str, tuple[str, ...]]:
    """File version suffixes by extension as the bundled list has them, the most common first
    (``{"tex": ("241106027",), "chain2": ("14", "13"), ...}``): a resource string inside a game
    file names its file without the version, a pak hashes it with one."""
    paths = game_paths()
    with _lock:
        if "versions" not in _loaded:
            counts: dict[str, Counter] = {}
            for p in paths:
                m = _VERSION_RE.search(p)
                if m:
                    counts.setdefault(m[1], Counter())[m[2]] += 1
            found = dict(_DEFAULT_VERSIONS)
            found.update({ext: tuple(v for v, _n in c.most_common()) for ext, c in counts.items()})
            _loaded["versions"] = found
        return _loaded["versions"]


def streaming_twin(path: str) -> str | None:
    """``natives/stm/streaming/...`` for a .mesh/.tex path under natives/stm/ (None otherwise)."""
    low = path.lower()
    if not low.startswith(PREFIX) or low.startswith(PREFIX + STREAMING):
        return None
    if not low.rstrip("0123456789").endswith(tuple(e + "." for e in _TWIN_EXTS)):
        return None
    return path[:len(PREFIX)] + STREAMING + path[len(PREFIX):]


def stored_paths(ref: str) -> Iterator[str]:
    """The paths a pak may keep the file a normalized reference (paths.normalize, e.g. ``art/x.tex``)
    names under: ``natives/stm/<ref>.<version>`` with each version the game uses for the extension,
    each followed by its streaming/ copy (.mesh/.tex)."""
    for version in versions().get(ref.rpartition(".")[2], ()):
        path = f"{PREFIX}{ref}.{version}"
        yield path
        twin = streaming_twin(path)
        if twin:
            yield twin


def full_path(path: str) -> str:
    """A game path as a pak hashes it: forward slashes and natives/STM/ in front."""
    p = path.replace("\\", "/").lstrip("@/")
    return p if p.lower().startswith("natives/") else "natives/STM/" + p


class PathIndex:
    """pak path hash -> game path, for the bundled paths, their streaming/ copies and whatever
    :meth:`add` was given.  ``cache_dir`` keeps the hashes between runs (None: no cache);
    ``paths`` replaces the bundled list (tests)."""

    def __init__(self, cache_dir: Path | None = None, paths: Sequence[str] | None = None):
        self.paths = list(game_paths() if paths is None else paths)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.hashes = array("Q")
        self.refs = array("I")  # 2 * index into self.paths, + 1 for its streaming/ copy
        self.extra: dict[int, str] = {}
        self.key = hashlib.sha1("\n".join(self.paths).encode("utf-8")).digest()
        self.from_cache = self._load_cache()
        if not self.from_cache:
            self._build()
            self._save_cache()

    # ---- building --------------------------------------------------------------
    def _build(self) -> None:
        pairs = []
        for i, p in enumerate(self.paths):
            pairs.append((path_hash(p), 2 * i))
            twin = streaming_twin(p)
            if twin:
                pairs.append((path_hash(twin), 2 * i + 1))
        pairs.sort()
        last = None
        for h, ref in pairs:
            if h != last:  # a collision keeps the first path
                self.hashes.append(h)
                self.refs.append(ref)
                last = h

    def _cache_file(self) -> Path | None:
        return self.cache_dir / f"equip-paths-{self.key.hex()[:16]}.bin" if self.cache_dir else None

    def _save_cache(self) -> None:
        target = self._cache_file()
        if target is None:
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            for old in target.parent.glob("equip-paths-*.bin"):
                if old != target:
                    old.unlink(missing_ok=True)  # from another list (an app update)
            payload = self.hashes.tobytes() + self.refs.tobytes()
            head = _CACHE_HEAD.pack(_CACHE_MAGIC, _CACHE_VERSION, len(self.hashes), self.key,
                                    hashlib.sha1(payload).digest())
            tmp = target.with_name(f"{target.name}.{os.getpid()}-{threading.get_ident()}.tmp")
            try:
                tmp.write_bytes(head + payload)
                tmp.replace(target)
            finally:
                tmp.unlink(missing_ok=True)  # left only when writing failed (a full disk)
        except OSError as exc:
            log.warning("장비 경로 색인을 저장하지 못했습니다: %s", exc)

    def _load_cache(self) -> bool:
        source = self._cache_file()
        if source is None or not source.is_file():
            return False
        try:
            data = source.read_bytes()
            magic, version, count, key, digest = _CACHE_HEAD.unpack_from(data)
            payload = memoryview(data)[_CACHE_HEAD.size:]
            split = count * self.hashes.itemsize
            if (magic != _CACHE_MAGIC or version != _CACHE_VERSION or key != self.key
                    or len(payload) != split + count * self.refs.itemsize
                    or hashlib.sha1(payload).digest() != digest):
                return False
            self.hashes.frombytes(payload[:split])
            self.refs.frombytes(payload[split:])
            if self.refs and max(self.refs) >= 2 * len(self.paths):
                raise ValueError("reference past the list")
            return True
        except (OSError, ValueError, struct.error):
            del self.hashes[:]
            del self.refs[:]
            return False

    # ---- lookups ---------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.hashes) + len(self.extra)

    def add(self, paths: Iterable[str]) -> None:
        """More candidate paths (with their version; natives/STM/ is added when missing), with the
        streaming/ copy of each .mesh/.tex."""
        for p in paths:
            p = full_path(p)
            self.extra.setdefault(path_hash(p), p)
            twin = streaming_twin(p)
            if twin:
                self.extra.setdefault(path_hash(twin), twin)

    def lookup(self, hash64: int) -> str | None:
        """The game path a pak entry hash stands for, or None."""
        found = self.extra.get(hash64)
        if found is not None:
            return found
        i = bisect_left(self.hashes, hash64)
        if i < len(self.hashes) and self.hashes[i] == hash64:
            ref = self.refs[i]
            p = self.paths[ref >> 1]
            return (streaming_twin(p) or p) if ref & 1 else p
        return None
