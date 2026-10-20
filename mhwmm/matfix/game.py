"""Index of the installed game's .pak files, merged the way the game layers them.

re_chunk_000.pak and re_chunk_000.pak.sub_000.pak each get their numbered patches
on top (a higher patch number wins), so one lookup returns the current bytes of any
game file.  Building the index reads every entry table once; it is cached in the
manager's data folder under a fingerprint of the paks' names, sizes and times, so
it is rebuilt only after a game update.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import lzma
import os
import re
import struct
import threading
from array import array
from bisect import bisect_left
from pathlib import Path

from . import MatfixError
from .pak import PakFile, path_hash, read_entry

log = logging.getLogger(__name__)

_PATCH_RE = re.compile(r"\.patch_(\d+)\.pak$", re.IGNORECASE)
_CACHE_MAGIC = b"MHWMMIDX"
_CACHE_VERSION = 1
MAX_VERSION = 500


def discover_paks(game_dir) -> list[Path]:
    """re_chunk_000.pak and its patches, then sub_000 and its patches (base first)."""
    groups: dict[str, list[tuple[int, Path]]] = {"main": [], "sub": []}
    for p in Path(game_dir).glob("re_chunk_000.pak*"):
        name = p.name.lower()
        if name == "re_chunk_000.pak":
            groups["main"].append((-1, p))
        elif name == "re_chunk_000.pak.sub_000.pak":
            groups["sub"].append((-1, p))
        else:
            m = _PATCH_RE.search(name)
            if m:
                groups["sub" if ".sub_000.pak." in name else "main"].append((int(m.group(1)), p))
    return [p for group in ("main", "sub") for _, p in sorted(groups[group], key=lambda t: t[0])]


def fingerprint(paks: list[Path]) -> str:
    """Changes whenever a game update adds or rewrites a pak."""
    parts = []
    for p in paks:
        st = p.stat()
        parts.append(f"{p.name}:{st.st_size}:{int(st.st_mtime)}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def game_fingerprint(game_dir) -> str:
    paks = discover_paks(game_dir)
    return fingerprint(paks) if paks else ""


class GameIndex:
    def __init__(self, game_dir, cache_dir=None, progress=None):
        self.game_dir = Path(game_dir)
        self.paks = discover_paks(self.game_dir)
        if not self.paks:
            raise MatfixError("게임 폴더에서 re_chunk_000.pak을 찾지 못했습니다.")
        self.fingerprint = fingerprint(self.paks)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.hashes = array("Q")
        self.pak_ids = array("H")
        self.offsets = array("q")
        self.csizes = array("q")
        self.dsizes = array("q")
        self.attrs = array("q")
        self.chunks: dict[int, tuple[array, array]] = {}
        if not self._load_cache():
            self._build(progress or (lambda done, total, msg: None))
            self._save_cache()

    # ---- building --------------------------------------------------------------
    def _build(self, progress):
        merged: dict[int, tuple] = {}
        for i, path in enumerate(self.paks):
            progress(i, len(self.paks), f"게임 파일 목록 읽는 중... {path.name}")
            try:
                pak = PakFile(path)
            except (OSError, MatfixError) as exc:
                log.warning("%s을(를) 읽지 못했습니다: %s", path.name, exc)
                continue
            if pak.chunk_offsets is not None:
                self.chunks[i] = (pak.chunk_offsets, pak.chunk_sizes)
            for j, h in enumerate(pak.hashes):
                merged[h] = (i, pak.offsets[j], pak.csizes[j], pak.dsizes[j], pak.attrs[j])  # later paks win
        for h in sorted(merged):
            i, off, csize, dsize, attrs = merged[h]
            self.hashes.append(h)
            self.pak_ids.append(i)
            self.offsets.append(off)
            self.csizes.append(csize)
            self.dsizes.append(dsize)
            self.attrs.append(attrs)
        if not self.hashes:
            raise MatfixError("게임의 pak 파일을 읽지 못했습니다.")
        progress(len(self.paks), len(self.paks), f"게임 파일 {len(self.hashes):,}개를 찾았습니다")

    def _cache_file(self) -> Path | None:
        return self.cache_dir / f"pakindex-{self.fingerprint}.bin" if self.cache_dir else None

    def _arrays(self):
        return (self.hashes, self.pak_ids, self.offsets, self.csizes, self.dsizes, self.attrs)

    def _save_cache(self):
        target = self._cache_file()
        if target is None:
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            for old in target.parent.glob("pakindex-*.bin"):
                old.unlink(missing_ok=True)  # from before a game update
            names = json.dumps([p.name for p in self.paks]).encode("utf-8")
            tmp = target.with_name(f"{target.name}.{os.getpid()}-{threading.get_ident()}.tmp")
            with open(tmp, "wb") as f:
                f.write(_CACHE_MAGIC + struct.pack("<IIQ", _CACHE_VERSION, len(names), len(self.hashes)) + names)
                for arr in self._arrays():
                    f.write(arr.tobytes())
                f.write(struct.pack("<I", len(self.chunks)))
                for i, (offsets, sizes) in self.chunks.items():
                    f.write(struct.pack("<IQ", i, len(offsets)) + offsets.tobytes() + sizes.tobytes())
            tmp.replace(target)
        except OSError as exc:
            log.warning("게임 파일 목록을 저장하지 못했습니다: %s", exc)

    def _load_cache(self) -> bool:
        source = self._cache_file()
        if source is None or not source.is_file():
            return False
        try:
            with open(source, "rb") as f:
                if f.read(8) != _CACHE_MAGIC:
                    return False
                version, name_len, count = struct.unpack("<IIQ", f.read(16))
                if version != _CACHE_VERSION or json.loads(f.read(name_len)) != [p.name for p in self.paks]:
                    return False
                for arr in self._arrays():
                    arr.frombytes(f.read(count * arr.itemsize))
                    if len(arr) != count:
                        raise ValueError("truncated")
                (chunk_paks,) = struct.unpack("<I", f.read(4))
                for _ in range(chunk_paks):
                    i, n = struct.unpack("<IQ", f.read(12))
                    offsets, sizes = array("q"), array("q")
                    offsets.frombytes(f.read(n * 8))
                    sizes.frombytes(f.read(n * 8))
                    self.chunks[i] = (offsets, sizes)
            return True
        except (OSError, ValueError, struct.error, EOFError):
            for arr in self._arrays():
                del arr[:]
            self.chunks.clear()
            return False

    # ---- lookups ---------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.hashes)

    def _find(self, hash64: int) -> int | None:
        i = bisect_left(self.hashes, hash64)
        return i if i < len(self.hashes) and self.hashes[i] == hash64 else None

    def has_hash(self, hash64: int) -> bool:
        return self._find(hash64) is not None

    def has(self, path: str) -> bool:
        return self.has_hash(path_hash(path))

    def read_hash(self, hash64: int) -> bytes | None:
        i = self._find(hash64)
        if i is None:
            return None
        pak = self.pak_ids[i]
        offsets, sizes = self.chunks.get(pak, (None, None))
        return read_entry(self.paks[pak], self.offsets[i], self.csizes[i], self.dsizes[i], self.attrs[i],
                          offsets, sizes)

    def read(self, path: str) -> bytes | None:
        return self.read_hash(path_hash(path))

    def versioned_path(self, base: str, ext: str, prefer=()) -> str | None:
        """``base.ext.N`` as the current game has it (``prefer`` versions are tried first)."""
        tried = set()
        for n in (*prefer, *range(1, MAX_VERSION)):
            if n in tried:
                continue
            tried.add(n)
            candidate = f"{base}.{ext}.{n}"
            if self.has(candidate):
                return candidate
        return None

    def read_versioned(self, base: str, ext: str, prefer=()) -> tuple[str, bytes] | None:
        path = self.versioned_path(base, ext, prefer)
        return (path, self.read(path)) if path else None


# ---- whole-game shader index ----------------------------------------------------
_FILELIST = None


def known_material_paths() -> list[str]:
    """Paths (with version) of the game's .mdf2 files, from the community file list
    MHWmodfixer ships (the paks themselves only store hashes)."""
    global _FILELIST
    if _FILELIST is None:
        from .filelist import DATA

        _FILELIST = lzma.decompress(base64.b64decode(DATA)).decode("utf-8").splitlines()
    return _FILELIST


_VERSION_RE = re.compile(r"\.mdf2\.(\d+)$", re.IGNORECASE)


class ShaderIndex:
    """Every material in the game by its shader (.mmtr) - the last place to look for a
    current material when neither the mod's own file nor its set has one.  Built on
    first use (it reads thousands of small files) and cached per game version."""

    def __init__(self, game: GameIndex, progress=None):
        self.game = game
        self.progress = progress or (lambda done, total, msg: None)
        self._by_mmtr: dict[str, list[tuple[str, dict]]] | None = None

    def _cache_file(self) -> Path | None:
        return self.game.cache_dir / f"shaders-{self.game.fingerprint}.json.gz" if self.game.cache_dir else None

    def _load(self) -> None:
        from .mdf2 import Mdf2File, detect_version, extract_material, material_from_json, material_to_json

        source = self._cache_file()
        if source is not None and source.is_file():
            try:
                with gzip.open(source, "rt", encoding="utf-8") as f:
                    data = json.load(f)
                self._by_mmtr = {k: [(src, material_from_json(m)) for src, m in v] for k, v in data.items()}
                return
            except (OSError, ValueError, KeyError, TypeError):
                pass
        by_mmtr: dict[str, list[tuple[str, dict]]] = {}
        paths = known_material_paths()
        for i, listed in enumerate(paths):
            if i % 200 == 0:
                self.progress(i, len(paths), f"게임 전체 재질 목록 만드는 중 (처음 한 번)... {i}/{len(paths)}")
            m = _VERSION_RE.search(listed)
            base = listed[:m.start()] if m else listed
            try:
                found = self.game.read_versioned(base, "mdf2", prefer=(int(m.group(1)),) if m else ())
                if found is None:
                    continue
                path, raw = found
                nv = detect_version(raw, prefer=int(path.rsplit(".", 1)[1]))
                if nv is None:
                    continue
                mdf = Mdf2File(raw, nv)
                for j, mat in enumerate(mdf.materials):
                    by_mmtr.setdefault(mat.mmtr_path, []).append((path, extract_material(mdf, j)))
            except Exception:  # one odd file must not stop the other ten thousand
                continue
        self._by_mmtr = by_mmtr
        target = self._cache_file()
        if target is not None:
            try:
                for old in target.parent.glob("shaders-*.json.gz"):
                    old.unlink(missing_ok=True)
                with gzip.open(target, "wt", encoding="utf-8") as f:
                    json.dump({k: [(src, material_to_json(b)) for src, b in v] for k, v in by_mmtr.items()}, f)
            except OSError as exc:
                log.warning("재질 목록을 저장하지 못했습니다: %s", exc)

    def find(self, mmtr: str) -> list[tuple[str, dict]]:
        if self._by_mmtr is None:
            self._load()
        return self._by_mmtr.get(mmtr, [])
