"""Bake mhwmm/matfix/rsz_types.json.xz, the RSZ type registry used to repair .user/.pfb/.scn files.

Sources (both MIT licensed):
  - REasy (https://github.com/seifhassine/REasy): resources/data/dumps/rszmhwilds.json, a dump of
    every RSZ type of MH Wilds with its CRC and field layout. Its git history keeps one dump per
    game update, which gives the layout a type had under each older CRC - exactly what a mod made
    for that update was written with.
  - MHWmodfixer (https://github.com/PoeticJustice79/MHWmodfixer): tools/rsz_fields_mhwilds.json.gz,
    whose layouts for some native (via.*) types differ from REasy's; kept as alternatives that are
    used only when the current game's own file parses with them and not with REasy's.

Usage (a REasy clone made with `git clone --filter=blob:none --no-checkout` is enough):
    python tools/make_rsz_types.py <REasy clone> <rsz_fields_mhwilds.json.gz>
"""

from __future__ import annotations

import gzip
import json
import lzma
import subprocess
import sys
from pathlib import Path

DUMP = "resources/data/dumps/rszmhwilds.json"
OUT = Path(__file__).resolve().parent.parent / "mhwmm" / "matfix" / "rsz_types.json.xz"


def compact_fields(fields: list[dict]) -> list:
    """[name, type, size, align, is_array]; a Struct field's type names its struct: "Struct:<type name>"."""
    out = []
    for f in fields:
        kind = f.get("type") or "Data"
        if kind == "Struct":
            kind = "Struct:" + (f.get("original_type") or "")
        out.append([f.get("name") or "", kind, int(f.get("size") or 0), int(f.get("align") or 1), int(bool(f.get("array")))])
    return out


def load_dump(repo: Path, rev: str) -> dict:
    raw = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{DUMP}"], capture_output=True, check=True).stdout
    return json.loads(raw)


def main(repo: Path, modfixer: Path) -> None:
    revs = subprocess.run(["git", "-C", str(repo), "log", "--format=%H %cs", "--", DUMP],
                          capture_output=True, text=True, check=True).stdout.split("\n")
    revs = [r.split() for r in revs if r.strip()]
    head_rev, head_date = revs[0]
    print(f"current: {head_rev[:8]} {head_date}, {len(revs)} versions in history")
    current = load_dump(repo, head_rev)

    types, empty, crc_now = {}, {}, {}
    for key, entry in current.items():
        if not isinstance(entry, dict) or "crc" not in entry:
            continue
        crc_now[key] = entry["crc"]
        name = entry.get("name") or ""
        if entry.get("fields"):
            types[key] = [entry["crc"], name, json.dumps(compact_fields(entry["fields"]), separators=(",", ":"))]
        elif not name.startswith("System.") and key != "0":
            empty[key] = entry["crc"]

    # MHWmodfixer's layouts that disagree with REasy's (mostly native via.* types)
    alt = {}
    with gzip.open(modfixer, "rt", encoding="utf-8") as f:
        registry = json.load(f)
    for key, entry in registry.items():
        if key == "_meta" or entry.get("fieldless") or key not in types:
            continue
        mine = json.loads(types[key][2])
        theirs = []
        for i, field in enumerate(entry["f"]):
            name, size, align, is_array, is_variable = field[:5]
            kind = field[5] if len(field) > 5 else ("String" if is_variable else "Data")
            if kind == "Struct" and i < len(mine) and mine[i][1].startswith("Struct:"):
                kind = mine[i][1]
            theirs.append([name, kind, size, align, int(bool(is_array))])
        if [f[2:] for f in theirs] != [f[2:] for f in mine]:
            alt[key] = json.dumps(theirs, separators=(",", ":"))

    # older layouts by CRC, newest dump first so the most corrected layout of a CRC wins
    old: dict[str, dict[str, str]] = {}
    seen_dates = {}
    for rev, date in revs[1:]:
        try:
            dump = load_dump(repo, rev)
        except (subprocess.CalledProcessError, ValueError) as exc:
            print(f"  skip {rev[:8]}: {exc}")
            continue
        added = 0
        for key, entry in dump.items():
            if not isinstance(entry, dict) or not entry.get("fields") or key not in crc_now:
                continue
            crc = entry.get("crc")
            if not crc or crc == crc_now[key] or crc in old.get(key, {}):
                continue
            layout = json.dumps(compact_fields(entry["fields"]), separators=(",", ":"))
            # 1: the same fields as now, only the CRC changed
            old.setdefault(key, {})[crc] = 1 if key in types and layout == types[key][2] else layout
            added += 1
        seen_dates[rev[:8]] = date
        print(f"  {rev[:8]} {date}: {added} older layouts")
        del dump

    data = {
        "meta": {"source": "REasy rszmhwilds.json + MHWmodfixer rsz_fields_mhwilds.json.gz",
                 "reasy_commit": head_rev, "reasy_date": head_date, "history": seen_dates},
        "types": types, "empty": empty, "alt": alt, "old": old,
    }
    blob = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    OUT.write_bytes(lzma.compress(blob, preset=9 | lzma.PRESET_EXTREME))
    print(f"{OUT}: {len(types)} types, {len(empty)} without fields, {len(alt)} alternative layouts, "
          f"{sum(len(v) for v in old.values())} older CRCs of {len(old)} types "
          f"({sum(1 for v in old.values() for x in v.values() if x != 1)} with other fields), {OUT.stat().st_size:,} bytes")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
