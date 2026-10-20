"""Optional: real game and mod files from a "재질 진단 파일" zip (not shipped: they belong to
Capcom and the mod authors). Point MHWMM_RSZ_SAMPLES at the extracted folder (mod/, game/)."""

import os
from pathlib import Path

import pytest

from mhwmm.matfix import rsz

SAMPLES = os.environ.get("MHWMM_RSZ_SAMPLES")
pytestmark = pytest.mark.skipif(not SAMPLES, reason="MHWMM_RSZ_SAMPLES가 없습니다")


def pairs():
    root = Path(SAMPLES or ".")
    for mod in sorted((root / "mod").rglob("*")):
        game = root / "game" / mod.relative_to(root / "mod")
        if mod.is_file() and game.is_file():
            yield mod, game


def test_game_files_read_exactly():
    reg = rsz.TypeRegistry.load()
    for _mod, game in pairs():
        info = rsz.parse(game.read_bytes())
        schema, _ = rsz.choose_schema(info, info, reg)
        assert schema is not None and schema.resource_string, game.name
        assert rsz.walk_current(info, schema, reg)[1], game.name  # padding clean


def test_mod_files_are_repaired_keeping_every_value():
    reg = rsz.TypeRegistry.load()
    for mod, game in pairs():
        plan = rsz.plan(mod.name, mod.read_bytes(), game.read_bytes(), reg)
        assert plan.state in ("current", "fixable"), (mod.name, plan.reason, plan.detail)
        if plan.data is None:
            continue
        out, game_info = rsz.parse(plan.data), rsz.parse(game.read_bytes())
        crcs = rsz.crc_map(game_info)
        assert all(crcs.get(t) in (None, c) for t, c in out.insts[1:]), mod.name
        assert rsz.walk_current(out, rsz.Schema(), reg) is not None, mod.name
        assert not any("없어진 필드" in n for n in plan.notes), (mod.name, plan.notes)
