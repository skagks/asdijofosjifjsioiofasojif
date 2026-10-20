"""Other mods a mod needs.

Two sources: the requirements listed on the mod's Nexus page, and Lua libraries its
REFramework scripts ``require``.  REFramework looks modules up next to the running
script (``reframework/autorun/<name>.lua`` or ``<name>/init.lua``), so a library is
there when some installed mod (or a file in the game folder) provides that path.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Iterable

AUTORUN = "reframework/autorun/"
MAX_SCRIPT_BYTES = 2 * 1024 * 1024
LUA_STDLIB = {"string", "table", "math", "io", "os", "coroutine", "utf8", "debug", "package", "bit32", "ffi", "jit"}

_BLOCK_COMMENT = re.compile(r"--\[(=*)\[.*?\]\1\]", re.S)
_LINE_COMMENT = re.compile(r"--[^\n]*")
# require("a.b"), require 'a', require( "a" ) - but not pcall(require, "a"), which is optional
_REQUIRE = re.compile(r"""\brequire\s*\(?\s*(["'])([^"'\r\n]{1,200})\1""")
_NAME_CHARS = re.compile(r"[^0-9a-z가-힣]+")


def module_key(name: str) -> str:
    """``a.b``, ``a/b`` and ``A\\B`` all name the module file ``a/b``."""
    return name.strip().replace("\\", "/").replace(".", "/").strip("/").casefold()


def provided_modules(paths: Iterable[str]) -> set[str]:
    """Module names the given game paths make available to REFramework scripts."""
    out: set[str] = set()
    for rel in paths:
        low = rel.replace("\\", "/").casefold()
        if not low.startswith(AUTORUN):
            continue
        stem, ext = posixpath.splitext(low[len(AUTORUN):])
        if ext not in (".lua", ".dll"):
            continue
        parts = stem.replace(".", "/").split("/")
        if parts[-1] == "init" and len(parts) > 1:
            parts = parts[:-1]
        # scripts in subfolders can require their neighbours by the shorter name
        for i in range(len(parts)):
            out.add("/".join(parts[i:]))
    return out


def required_modules(files: dict[str, Path]) -> list[str]:
    """Lua modules the scripts among ``files`` (game path -> stored file) need from other mods."""
    own = provided_modules(files)
    wanted: dict[str, str] = {}
    for rel, path in files.items():
        if not rel.lower().endswith(".lua"):
            continue
        try:
            if path.stat().st_size > MAX_SCRIPT_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))
        for m in _REQUIRE.finditer(text):
            name = m.group(2).strip()
            key = module_key(name)
            if key and key.split("/")[0] not in LUA_STDLIB and key not in own:
                wanted.setdefault(key, name)
    return sorted(wanted.values(), key=str.casefold)


def normalize_name(name: str) -> str:
    return _NAME_CHARS.sub("", str(name).casefold())


def names_match(a: str, b: str) -> bool:
    """Loose match of two mod names ("Lua Bone System" ~ "LuaBoneSystem v1.2")."""
    x, y = normalize_name(a), normalize_name(b)
    if not x or not y:
        return False
    if x == y:
        return True
    shorter, longer = sorted((x, y), key=len)
    return len(shorter) >= 5 and longer.startswith(shorter)


def is_reframework(name: str) -> bool:
    return "reframework" in normalize_name(name)
