"""Check a freshly baked mhwmm/matfix/rsz_types.json.xz before it is committed.

    python tools/check_rsz_types.py <previous rsz_types.json.xz> <real game .user/.pfb> ...

Fails when a type CRC the previous registry knew (current or older) is gone, or when a real
game file does not read exactly under the new registry (with the layouts its CRCs stand for, so
a file from before a later game update still counts).
"""

from __future__ import annotations

import json
import lzma
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mhwmm.matfix import rsz  # noqa: E402


def known_crcs(data: dict) -> set[tuple[str, str]]:
    out = {(k, v[0]) for k, v in data.get("types", {}).items()}
    out |= {(k, v) for k, v in data.get("empty", {}).items()}
    out |= {(k, crc) for k, olds in data.get("old", {}).items() for crc in olds}
    return out


def main(previous: Path, samples: list[Path]) -> int:
    with lzma.open(previous, "rt", encoding="utf-8") as f:
        before = known_crcs(json.load(f))
    with lzma.open(rsz.REGISTRY_FILE, "rt", encoding="utf-8") as f:
        after = known_crcs(json.load(f))
    lost = before - after
    if lost:
        print(f"{len(lost)} CRCs disappeared, e.g. {sorted(lost)[:5]}")
        return 1
    registry = rsz.TypeRegistry.load()
    for sample in samples:
        info = rsz.parse(sample.read_bytes())
        why: list[str] = []
        walked = rsz.walk_mod(info, rsz.Schema(), registry, registry.crc, why) if info else None
        if walked is None:
            print(f"{sample.name} does not read exactly with the new registry: {why}")
            return 1
        print(f"{sample.name}: OK ({len(info.insts) - 1} instances)")
    print(f"{len(after)} CRCs, none lost")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.exit(main(Path(sys.argv[1]), [Path(p) for p in sys.argv[2:]]))
