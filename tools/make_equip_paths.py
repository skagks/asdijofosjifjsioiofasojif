"""Bake mhwmm/equip/equip_paths.txt.xz: the game paths the equipment viewer resolves .pak mods with.

A mod .pak stores its files under path hashes only; these are the game's equipment paths
(every path mhwmm.equip.paths.classify() recognises) to hash them against, plus the NPC
VisualSetting files (GameDesign/NPC/Character/<group>/<NPC id>/Data/*_VisualSetting.user.3) the
catalog reads to name NPC models (pathlist.npc_visual_settings() picks them out).  streaming/
copies are left out: they exist only for .mesh/.tex and are rebuilt by pathlist.PathIndex.

Source: REasy by Battlezone (MIT, https://github.com/seifhassine/REasy),
resources/data/lists/MHWS_STM.list at a pinned commit (the community MH Wilds file list).

The file: an xz-compressed text file, one path per line, sorted, lower case, with its version
suffix and WITHOUT the common "natives/stm/" prefix; lines starting with "#" are comments, the
first one names the source.

Usage:
    python tools/make_equip_paths.py              (downloads the list at the pinned commit)
    python tools/make_equip_paths.py <list file>  (a local copy, e.g. for offline use)
"""

from __future__ import annotations

import collections
import hashlib
import lzma
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mhwmm.equip.paths import classify, is_npc_visual_setting  # noqa: E402

COMMIT = "ffcecb48830a1bc9f4ce8710ab88a98641366eb8"
LIST = "resources/data/lists/MHWS_STM.list"
URL = f"https://raw.githubusercontent.com/seifhassine/REasy/{COMMIT}/{LIST}"
SHA256 = "f5947d5bae6c94285635512a692f40c023484ca61b0ec0a8dec1607e13bc6bc4"  # of the list at COMMIT
PREFIX = "natives/stm/"
STREAMING = PREFIX + "streaming/"
OUT = Path(__file__).resolve().parent.parent / "mhwmm" / "equip" / "equip_paths.txt.xz"


def select(lines: list[str]) -> tuple[list[str], collections.Counter]:
    """The kept paths (lower case, natives/stm/ removed, sorted) and how many per kind."""
    paths = {line.strip().replace("\\", "/").lower() for line in lines}
    paths = {p for p in paths if p.startswith(PREFIX)}
    kept, kinds = set(), collections.Counter()
    for p in paths:
        if p.startswith(STREAMING) and PREFIX + p[len(STREAMING):] in paths:
            continue  # rebuilt at runtime from its twin
        target = classify(p)
        if target is not None:
            kinds[target.kind] += 1
        elif is_npc_visual_setting(p):
            kinds["npc visual setting"] += 1
        else:
            continue
        kept.add(p[len(PREFIX):])
    return sorted(kept), kinds


def build(raw: bytes, source: str) -> bytes:
    """The xz file for a list's bytes."""
    kept, kinds = select(raw.decode("utf-8", "replace").splitlines())
    header = [
        f"# source: {source}",
        f"# {len(kept)} paths without the leading {PREFIX!r} (streaming/ copies of .mesh/.tex left out); "
        "equipment paths and GameDesign/NPC/Character/*/*/Data/*_VisualSetting.user",
    ]
    for kind, n in kinds.most_common():
        print(f"  {kind:20s} {n:6d}")
    text = "\n".join(header + kept) + "\n"
    return lzma.compress(text.encode("utf-8"), preset=9 | lzma.PRESET_EXTREME)


def main(argv: list[str]) -> int:
    if argv:
        raw = Path(argv[0]).read_bytes()
    else:
        print(f"downloading {URL}")
        with urllib.request.urlopen(urllib.request.Request(URL, headers={"User-Agent": "MHWildsModManager"}),
                                    timeout=120) as r:
            raw = r.read()
    digest = hashlib.sha256(raw).hexdigest()
    if digest == SHA256:
        source = f"REasy (MIT, (c) 2025 Battlezone) {LIST} commit {COMMIT}"
    elif argv:
        source = f"{Path(argv[0]).name} sha256 {digest}"
        print(f"note: not the list of commit {COMMIT[:8]} (sha256 {digest[:16]}...)")
    else:
        print(f"the download does not match the pinned list (sha256 {digest})")
        return 1
    data = build(raw, source)
    OUT.write_bytes(data)
    print(f"{OUT.name}: {len(data):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
