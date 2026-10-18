"""Fluffy Mod Manager style ``modinfo.ini`` parsing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ModInfo:
    name: str = ""
    version: str = ""
    author: str = ""
    description: str = ""
    screenshot: str = ""
    category: str = ""


def read_text_guess(path) -> str:
    data = Path(path).read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    for enc in ("utf-8-sig", "cp949", "shift_jis"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def parse_modinfo_text(text: str) -> ModInfo:
    info = ModInfo()
    fields = {f: f for f in ("name", "version", "author", "description", "screenshot", "category")}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith((";", "#", "[")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        attr = fields.get(key.strip().lower())
        if attr and not getattr(info, attr):
            setattr(info, attr, value.strip())
    info.screenshot = info.screenshot.replace("\\", "/").strip("/")
    return info


def parse_modinfo(path) -> ModInfo:
    try:
        return parse_modinfo_text(read_text_guess(path))
    except OSError:
        return ModInfo()
