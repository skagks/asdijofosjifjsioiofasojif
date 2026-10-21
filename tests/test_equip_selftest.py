"""The 3D preview's self-test (mhwmm/equip/selftest.py) on a fake game of synthetic files: which parts it
looks at, what it writes about meshes, streaming companions, materials and textures, and that a broken
game never stops the report."""

from __future__ import annotations

import pytest

import equip_mesh_fixtures as MF
import test_equip_look as LK
from equip_game_fixtures import FakeGame
from mhwmm.equip import look, selftest
from mhwmm.equip.model import Target
from mhwmm.equip.paths import classify

CH = "natives/STM/Art/Model/Character"
F_BODY, F_HELM = f"{CH}/ch03/004/001/2/ch03_004_0012", f"{CH}/ch03/004/001/3/ch03_004_0013"
M_BODY = f"{CH}/ch02/004/001/2/ch02_004_0012"
PALICO = f"{CH}/ch05/001/0000/ch05_001_0000"
SWORD = "natives/STM/Art/Model/Item/it03/00/0009/it0300_0009_0"
MESHES = [F_BODY, F_HELM, M_BODY, PALICO, SWORD, LK.WEAPON, LK.SHIELD]


def files() -> look.GameFiles:
    paths = [LK.mesh_file(p) for p in MESHES]
    assert all(classify(p) is not None for p in paths)
    return look.GameFiles(paths)


def test_parts_are_tried_by_turns():
    order = selftest.candidates(files())
    assert order[:5] == [Target("armor", "f/004/001", "body"), Target("armor", "m/004/001", "body"),
                         Target("weapon", "03/000009", "main"), Target("palico", "001", "body"),
                         Target("armor", "f/004/001", "helm")]
    # every weapon type's main part comes before the sub parts; one palico piece in the first round only
    rest = order[5:]
    assert rest[:2] == [Target("weapon", "09/000021", "main"), Target("weapon", "09/000021", "sub")]
    assert len(order) == len(set(order)) == 7


def test_only_parts_the_game_has_are_picked(pil):
    game = FakeGame({LK.mesh_file(SWORD): LK.weapon_mesh(),
                     "natives/STM/streaming/" + LK.mesh_file(PALICO)[len("natives/STM/"):]: LK.weapon_mesh()})
    picked = selftest.pick(files(), look.GameSource(game), 5)
    assert picked == [Target("weapon", "03/000009", "main"), Target("palico", "001", "body")]


def test_the_report(pil):
    lod0, lod1 = [MF.grid(4)], [MF.grid(1)]
    mesh, blob = MF.build_mesh(["body", "film"], [lod0, lod1], streamed=(0,), empty_slots=1)
    film = LK.material("film", albedo=LK.ALBD, more=[("AlphaTranslucentOcclusionSSSMap", LK.ATOS)],
                       props=[("ColorParam", (0.5, 0.5, 0.5, 1.0))])
    game = FakeGame({
        LK.mesh_file(SWORD): mesh, "natives/STM/streaming/" + LK.mesh_file(SWORD)[12:]: blob,
        LK.mdf2_file(SWORD): LK.mdf2(LK.material("body"), film),
        LK.tex_file(): LK.solid(LK.RED, side=16, mips=True), LK.tex_file(streaming=True): LK.solid(LK.RED, side=64),
        LK.tex_file(LK.ATOS): LK.solid(LK.WHITE),
        LK.mesh_file(LK.WEAPON): b"MESH" + bytes(300),  # not a Wilds mesh
        LK.mesh_file(LK.SHIELD): LK.weapon_mesh()})  # no material in the game
    report = selftest.run(game, 10, files=files(), thumb=lambda target: None)
    assert report.startswith("preview self-test: format ") and "parts: 3 (weapon 3)" in report
    sword = report.split("== weapon 03/000009 main")[1].split("==")[0]
    assert "magic MESH, version 240704828" in sword and "streaming entries 2 (1 with data" in sword
    assert f"streaming companion: {len(blob):,} B" in sword
    assert "LODs readable without the companion [1], with it [0, 1]; preview LOD 0 (32 triangles)" in sword
    assert "names match the mesh" in sword and "material film: flags 0x88000098 [alpha-mask-used]" in sword
    assert f"AlphaMaskUsed only (no holes cut): ATOS {LK.ATOS}" in sword
    assert "ColorParam 0.500 0.500 0.500 1.000" in sword
    assert "resident  " in sword and "16x16 RGBA8 mips 5" in sword and "streaming " in sword and "64x64 RGBA8" in sword
    assert "preview: ok, 32 triangles" in sword and "drawn it0300_0009_0.mesh from 게임 LOD 0" in sword
    assert "mean colour: preview " in sword and "game thumbnail -" in sword
    assert "tables: MH Wilds 메시가 아닙니다 (버전 0)" in report
    assert "preview: unsupported" in report and "mdf2 natives" in report and "not in the game" in report
    summary = report.split("== summary")[1]
    assert "statuses: missing 1, ok 1, unsupported 1" in summary and "AlphaMaskUsed only, with ATOS: 1" in summary


def test_a_broken_game_does_not_stop_the_report(pil):
    class Broken(FakeGame):
        def read(self, path):
            if path.lower().endswith(".mdf2.45"):
                raise RuntimeError("damaged pak")
            return super().read(path)

    game = Broken({LK.mesh_file(SWORD): LK.weapon_mesh(), LK.mdf2_file(SWORD): LK.mdf2(),
                   LK.mesh_file(LK.WEAPON): LK.weapon_mesh(), LK.mdf2_file(LK.WEAPON): LK.mdf2()})
    report = selftest.run(game, 5, files=files())
    assert "parts: 2" in report and report.count("preview: ok") == 2
    assert report.count("read error: RuntimeError: damaged pak") == 2 and "mdf2 read error: 2" in report
    assert "note: it0300_0009_0.mdf2: 게임 파일을 읽지 못했습니다 (손상된 파일)" in report
    assert "stopped after 0 s (2 parts left)" in selftest.run(game, 5, files=files(), seconds=0)

    def boom(*a, **k):
        raise RuntimeError("bug")

    import mhwmm.equip.selftest as module

    original = module._part_lines
    try:
        module._part_lines = boom
        report = selftest.run(game, 5, files=files())
    finally:
        module._part_lines = original
    assert report.count("failed: RuntimeError: bug") == 2 and "statuses: exception 2" in report


@pytest.fixture
def pil():
    return pytest.importorskip("PIL.Image")
