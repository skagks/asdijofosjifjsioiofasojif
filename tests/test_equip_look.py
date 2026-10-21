"""The 3D preview's look (mhwmm/equip/look.py) on synthetic files only: a made-up mod (loose files and a
.pak) and a fake game - which copy of each file is used, the statuses and notes, colour chips and
texture swatches, and the shared framing of a before / after pair."""

from __future__ import annotations

import struct

import pytest

import equip_mesh_fixtures as MF
import equip_tex_fixtures as TF
from equip_game_fixtures import FakeGame
from mhwmm.equip import EquipError
from mhwmm.equip import look as L
from mhwmm.equip import mesh as M
from mhwmm.equip import tex as T
from mhwmm.equip.model import Target
from mhwmm.matfix import pak
from mhwmm.matfix.mdf2 import assemble_mdf2

WEAPON = "natives/STM/Art/Model/Item/it09/00/0021/it0900_0021_0"  # in the bundled path list
SHIELD = "natives/STM/Art/Model/Item/it09/00/0021/it0900_0021_1"
MAIN, SUB = Target("weapon", "09/000021", "main"), Target("weapon", "09/000021", "sub")
TEXTURES = "Art/Model/Item/it09/00/0021/textures/"
ALBD = TEXTURES + "it0900_0021_0_ALBD.tex"
ATOS = TEXTURES + "it0900_0021_0_ATOS.tex"
RED, GREEN, BLUE, WHITE = (200, 40, 40), (40, 200, 40), (40, 40, 200), (240, 240, 240)
OPAQUE = 0x88000098  # the flags of every opaque weapon material in real mods (AlphaMaskUsed among them)


@pytest.fixture
def pil():
    return pytest.importorskip("PIL.Image")


# ---- synthetic files ------------------------------------------------------------------------
def mesh_file(base=WEAPON) -> str:
    return f"{base}.mesh.241111606"


def mdf2_file(base=WEAPON) -> str:
    return f"{base}.mdf2.45"


def tex_file(path=ALBD, streaming=False) -> str:
    return f"natives/STM/{'streaming/' if streaming else ''}{path}.241106027"


def material(name="body", albedo=ALBD, slot="BaseDielectricMap", flags=OPAQUE, props=(), more=()):
    textures = ([(slot, albedo)] if albedo else []) + list(more)
    return {"name": name, "mmtr_path": "MaterialShader/x.mmtr", "shading_type": 0,
            "alpha_flags_raw": struct.pack("<I", flags), "isV2_unk_0x24": 0, "isV2_unk_0x2C": 0,
            "isV2_unk_0x30": 0, "gpbf_split": (0, 0), "gpbf_entries": [],
            "textures": [{"type": t, "path": p, "ukn": 0} for t, p in textures],
            "props": [{"name": n, "values": list(v), "count_flags": 0} for n, v in props], "header_version": 1}


def mdf2(*materials) -> bytes:
    return assemble_mdf2(list(materials or [material()]), 45)


def solid(rgb, side=8, alpha=255, mips=False) -> bytes:
    """An RGBA8 texture of one colour (every mip down to 1x1 with ``mips``)."""
    chain = TF.mip_chain(side, side, 28, lambda level, w, h: bytes([*rgb, alpha]) * (w * h)) if mips \
        else [bytes([*rgb, alpha]) * (side * side)]
    return TF.build_tex(side, side, 28, chain, gdeflate=False)


def square(material=0, size=1.0, group=0):
    return MF.quad(material, group, (-size / 2, -size / 2, 0.0), size)


def weapon_mesh(names=("body",), shape=None) -> bytes:
    return MF.build_mesh(list(names), [shape or MF.grid(4)])[0]


def game(**extra) -> L.GameSource:
    files = {mesh_file(): weapon_mesh(), mdf2_file(): mdf2(), tex_file(): solid(RED)}
    files.update({k.replace("__", "/"): v for k, v in extra.items()})
    return L.GameSource(FakeGame({k: v for k, v in files.items() if v is not None}))


def game_of(files: dict) -> L.GameSource:
    return L.GameSource(FakeGame(files))


@pytest.fixture(params=["loose", "pak"])
def make_mod(request, tmp_path):
    """make_mod({game path: bytes}) -> ModSource of a mod shipping those files as loose files or in a .pak."""
    def make(files: dict) -> L.ModSource:
        folder = tmp_path / f"mod{len(list(tmp_path.glob('mod*')))}"
        folder.mkdir()
        if request.param == "pak":
            target = folder / "Mod.pak"
            pak.write_pak([(pak.path_hash(p), d, len(d), pak.COMPRESSION_NONE) for p, d in files.items()], target)
            return L.ModSource({"Mod.pak": target})
        stored = {}
        for i, (p, d) in enumerate(files.items()):
            stored[p] = folder / f"{i}.bin"
            stored[p].write_bytes(d)
        return L.ModSource(stored)
    make.kind = request.param
    return make


def loose_mod(tmp_path, files: dict) -> L.ModSource:
    stored = {}
    for i, (p, d) in enumerate(files.items()):
        stored[p] = tmp_path / f"loose{i}.bin"
        stored[p].write_bytes(d)
    return L.ModSource(stored)


def pixels(img) -> list:
    raw = img.convert("RGBA").tobytes()
    return [tuple(raw[i:i + 4]) for i in range(0, len(raw), 4)]


def colour(img) -> tuple:
    """Mean colour of the drawn (opaque) pixels."""
    px = [p for p in pixels(img) if p[3] > 200]
    assert px, "nothing drawn"
    return tuple(sum(p[c] for p in px) / len(px) for c in range(3))


def leans(rgb, channel: int) -> bool:
    return all(rgb[channel] > rgb[c] + 40 for c in range(3) if c != channel)


def width(img) -> int:
    box = img.getchannel("A").getbbox()
    return box[2] - box[0] if box else 0


# ---- which files are used ---------------------------------------------------------------------
def test_a_texture_mod_on_the_game_mesh(pil, make_mod):
    mod = make_mod({tex_file(): solid(BLUE)})
    look = L.preview(MAIN, mod, game(), 64, compare=True)
    assert look.status == "ok" and look.changed and not look.notes
    assert look.image.size == look.before.size == (64, 64)
    assert leans(colour(look.image), 2) and leans(colour(look.before), 0)
    [(name, before, after, new)] = look.swatches  # the replaced albedo, the game's beside the mod's
    assert name == "it0900_0021_0_ALBD.tex" and not new
    assert leans(colour(before), 0) and leans(colour(after), 2)
    assert look.chips == []
    # the game's own look alone (no mod): the same as the "before" picture
    alone = L.preview(MAIN, None, game(), 64)
    assert alone.status == "ok" and not alone.changed and alone.before is None
    assert alone.image.tobytes() == look.before.tobytes()


def test_loose_and_pak_mods_give_the_same_picture(pil, tmp_path):
    files = {mesh_file(): weapon_mesh(shape=MF.grid(3)), tex_file(): solid(GREEN)}
    target = tmp_path / "Mod.pak"
    pak.write_pak([(pak.path_hash(p), d, len(d), pak.COMPRESSION_NONE) for p, d in files.items()], target)
    from_pak = L.preview(MAIN, L.ModSource({"Mod.pak": target}), game(), 48)
    from_files = L.preview(MAIN, loose_mod(tmp_path, files), game(), 48)
    assert from_pak.status == from_files.status == "ok"
    assert from_pak.image.tobytes() == from_files.image.tobytes()


def test_the_mod_copy_wins_and_copies_of_two_sources_are_not_mixed(pil, make_mod):
    # the game: a tiny resident red copy and a bigger green streaming copy
    g = game(**{tex_file(): solid(RED, 4), tex_file(streaming=True): solid(GREEN, 16, mips=True)})
    look = L.preview(MAIN, make_mod({tex_file(): solid(BLUE, 4)}), g, 64, compare=True)
    assert leans(colour(look.image), 2)  # the mod's small copy, not the game's bigger streaming one
    assert leans(colour(look.before), 1)  # in the game, the streaming copy beats the too-small resident one
    # a mod that ships only the streaming copy: that one
    look = L.preview(MAIN, make_mod({tex_file(streaming=True): solid(BLUE, 16)}), g, 64)
    assert leans(colour(look.image), 2)


def test_a_streamed_game_mesh_with_a_mod_texture(pil, make_mod, monkeypatch):
    data, blob = MF.build_mesh(["body"], [[MF.grid(10)], [MF.grid(2)]], streamed=(0,), empty_slots=1)
    streaming = M.streaming_path(mesh_file())
    mod = make_mod({tex_file(): solid(BLUE)})
    stats = {}
    look = L.preview(MAIN, mod, game(**{mesh_file(): data, streaming: blob}), 64, stats=stats)
    assert look.status == "ok" and look.triangles == 200 and leans(colour(look.image), 2)  # LOD 0, streamed
    assert stats["meshes"] == [(mesh_file().lower(), "게임", 0, 200)]
    # without the streaming file only the resident LOD can be drawn
    assert L.preview(MAIN, mod, game(**{mesh_file(): data}), 64).triangles == 8
    # the LOD budget picks the most detailed LOD within it
    monkeypatch.setattr(L, "MAX_TRIANGLES", 100)
    assert L.preview(MAIN, mod, game(**{mesh_file(): data, streaming: blob}), 64).triangles == 8


def test_a_mod_mesh_never_takes_the_games_streaming_data(pil, make_mod):
    data, blob = MF.build_mesh(["body"], [[MF.grid(10)], [MF.grid(2)]], streamed=(0,))
    g = game(**{M.streaming_path(mesh_file()): blob})  # the game's companion belongs to the game's mesh
    look = L.preview(MAIN, make_mod({mesh_file(): data}), g, 48)
    assert look.status == "ok" and look.triangles == 8
    # a streaming/ file that is a whole mesh (as some mods ship) is used when the main path is missing
    look = L.preview(MAIN, make_mod({M.streaming_path(mesh_file()): weapon_mesh(shape=MF.grid(3))}), g, 48)
    assert look.status == "ok" and look.triangles == 18 and look.changed


def test_material_names_must_match(pil, make_mod):
    look = L.preview(MAIN, make_mod({mesh_file(): weapon_mesh(["lambert3"])}), game(), 64)
    assert look.status == "ok" and "재질 이름 불일치: lambert3 (게임에서는 체크무늬로 보입니다)" in look.notes
    greys = {p[:3] for p in pixels(look.image) if p[3] == 255}
    assert all(abs(r - g) < 4 and abs(g - b) < 4 for r, g, b in greys)  # grey, two shades lit
    assert max(r for r, _g, _b in greys) - min(r for r, _g, _b in greys) > 30


def test_a_collapsed_part_is_hidden(pil, make_mod):
    point = MF.Sub(0, [(0.1, 0.2, 0.3)] * 4, None, MF.QUAD_TRIS)
    look = L.preview(MAIN, make_mod({mesh_file(): weapon_mesh(shape=point)}), game(), 64, compare=True)
    assert look.status == "hidden" and look.image is None and look.text == "숨김 (모드가 이 부위를 감춤)"
    assert look.notes[0] == look.text and look.before is not None and look.changed


def test_a_mesh_without_triangles_is_hidden(pil, make_mod):
    nothing = MF.Sub(0, [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)], None, [])  # a submesh of no indices
    empty = weapon_mesh(shape=nothing)
    no_subs = bytearray(empty)  # ... and one whose group has no submeshes at all (no LOD has a buffer)
    info = M.read_info(empty)
    lods = struct.unpack_from("<q", empty, 0x30)[0]
    lod = struct.unpack_from("<q", empty, struct.unpack_from("<q", empty, lods + 56)[0])[0]
    group = struct.unpack_from("<q", empty, struct.unpack_from("<q", empty, lod + 8)[0])[0]
    no_subs[group + 1] = 0
    assert M.read_info(bytes(no_subs)).lods[0].buffers == [] and info.lods[0].triangle_count == 0
    for data in (empty, bytes(no_subs)):
        look = L.preview(MAIN, make_mod({mesh_file(): data}), game(), 48, compare=True)
        assert look.status == "hidden" and look.text == L.STATUS["hidden"] and look.notes == [look.text]
        assert look.image is None and look.before is not None and look.changed
    with pytest.raises(M.MeshError, match="그릴 수 있는 LOD가 없습니다"):
        M.pick_lod(M.read_info(bytes(no_subs)), None)
    # an empty LOD is drawn only when no other can be read: then it is not "hidden" (the game streams the other)
    data, _blob = MF.build_mesh(["body"], [[MF.grid(4)], [nothing]], streamed=(0,))
    assert M.pick_lod(M.read_info(MF.build_mesh(["body"], [[nothing], [MF.grid(2)]])[0]), None) == 1
    look = L.preview(MAIN, make_mod({mesh_file(): data}), game(), 48)
    assert look.status == "error" and look.notes == ["it0900_0021_0.mesh: 그릴 수 있는 삼각형이 없습니다"]


def test_a_broken_streaming_file_falls_back_to_the_resident_lod(pil):
    data, blob = MF.build_mesh(["body"], [[MF.grid(12)], [MF.grid(3)]], streamed=(0,))
    bad = blob[:-6] + b"\xff" * 6  # the last indices of the streamed LOD point past its vertices
    files = {mesh_file(): data, M.streaming_path(mesh_file()): bad}
    stats = {}
    look = L.preview(MAIN, None, game(**files), 64, stats=stats)
    assert look.status == "ok" and look.triangles == 18 and look.image is not None
    assert look.notes == ["it0900_0021_0.mesh: 스트리밍 파일을 읽지 못해 낮은 LOD로 그렸습니다 (인덱스가 정점 수를 넘습니다)"]
    assert stats["meshes"] == [(mesh_file().lower(), "게임", 1, 18)]
    # with a good streaming file the streamed LOD is drawn; a mesh of streamed LODs only stays an error
    assert L.preview(MAIN, None, game(**{mesh_file(): data, M.streaming_path(mesh_file()): blob}), 64).triangles == 288
    only, only_blob = MF.build_mesh(["body"], [[MF.grid(12)]], streamed=(0,))
    look = L.preview(MAIN, None, game(**{mesh_file(): only, M.streaming_path(mesh_file()): only_blob[:-6] + bad[-6:]}),
                     64)
    assert look.status == "error" and look.notes == ["모델을 읽지 못했습니다: it0900_0021_0.mesh (인덱스가 정점 수를 넘습니다)"]


def test_unsupported_and_broken_meshes(pil, make_mod):
    mply = b"MPLY" + bytes(0xC0)
    look = L.preview(MAIN, make_mod({mesh_file(): mply}), game(), 48)
    assert look.status == "unsupported" and look.image is None
    assert look.notes == ["it0900_0021_0.mesh: MPLY(메시렛) 모델은 지원하지 않습니다"]
    other = bytearray(weapon_mesh())
    other[4:8] = struct.pack("<I", 1234)
    assert L.preview(MAIN, make_mod({mesh_file(): bytes(other)}), game(), 48).status == "unsupported"
    broken = L.preview(MAIN, make_mod({mesh_file(): weapon_mesh()[:300]}), game(), 48)  # cut short
    assert broken.status == "error" and broken.notes[0].startswith("모델을 읽지 못했습니다: it0900_0021_0.mesh")
    data = bytearray(weapon_mesh())
    struct.pack_into("<3f", data, len(data) - 200, float("nan"), float("inf"), 1e30)  # garbage in the vertices
    assert L.preview(MAIN, make_mod({mesh_file(): bytes(data)}), game(), 48).status in ("ok", "error")


def test_a_mod_that_changes_nothing_of_the_part_says_so(pil, make_mod):
    prefab = "natives/STM/GameDesign/Equip/_Prefab/Weapon/wp09/00/0021/it0900_0021_0.pfb.18"
    look = L.preview(MAIN, make_mod({prefab: b"PFB\x00"}), game(), 48)
    assert look.status == "ok" and not look.changed and look.image is not None
    assert look.notes == ["이 모드는 이 부위의 모델·재질·텍스처를 바꾸지 않습니다 (게임 그대로)"]


def test_missing_files(pil, make_mod):
    # the mod's material points at a texture that is in neither the mod nor the game
    own = TEXTURES + "my_ALBD.tex"
    look = L.preview(MAIN, make_mod({mdf2_file(): mdf2(material(albedo=own))}), game(), 48)
    assert look.status == "missing" and look.image is not None  # drawn grey: the game would hang on it
    assert "모드 파일 누락: my_ALBD.tex (모드에도 게임에도 없습니다)" in look.notes
    # the game has no such mesh (the bundled list is newer than the game)
    look = L.preview(MAIN, make_mod({tex_file(): solid(BLUE)}), game_of({}), 48)
    assert look.status == "missing" and look.notes == ["게임에 없는 파일: it0900_0021_0.mesh"]
    # a part the game has no mesh for at all (a seikret equipment prefab only places other models)
    look = L.preview(Target("seikret", "equip/012", "body"), make_mod({tex_file(): solid(BLUE)}), game(), 48)
    assert look.status == "no_mesh" and look.image is None


def test_a_material_only_recolour_lists_colour_chips(pil, make_mod):
    plain = [("ColorParam", (1.0, 1.0, 1.0, 1.0)), ("ColorLayer_R", (0.502, 0.502, 0.502, 1.0)), ("Use_R", (1.0,)),
             ("ColorLayer_G", (0.502, 0.502, 0.502, 1.0)), ("Use_G", (0.0,)), ("Emissive_Color", (0, 0, 0, 1))]
    tinted = [("ColorParam", (1.0, 0.25, 0.25, 1.0)), ("ColorLayer_R", (0.8, 0.1, 0.1, 1.0)), ("Use_R", (1.0,)),
              ("ColorLayer_G", (0.502, 0.502, 0.502, 1.0)), ("Use_G", (0.0,)), ("Emissive_Color", (0, 0, 0, 1))]
    g = game(**{mdf2_file(): mdf2(material(props=plain)), tex_file(): solid(WHITE)})
    look = L.preview(MAIN, make_mod({mdf2_file(): mdf2(material(props=tinted))}), g, 48, compare=True)
    assert look.status == "ok" and look.changed and look.swatches == []
    assert [(c.material, c.name) for c in look.chips] == [("body", "ColorParam"), ("body", "ColorLayer_R")]
    first = look.chips[0]
    assert first.before == (1.0, 1.0, 1.0, 1.0) and first.after == (1.0, 0.25, 0.25, 1.0)
    assert L.Chip.hex(first.after) == "#ff4040" and L.Chip.hex(first.before) == "#ffffff"
    assert leans(colour(look.image), 0) and not leans(colour(look.before), 0)  # ColorParam multiplies
    assert L.preview(MAIN, make_mod({mdf2_file(): mdf2(material(props=tinted))}), g, 48, extras=False).chips == []
    # without the game there is nothing to compare with: only colours that change something
    alone = L.preview(MAIN, make_mod({mdf2_file(): mdf2(material(props=tinted)), mesh_file(): weapon_mesh()}),
                      None, 48)
    assert [(c.name, c.before) for c in alone.chips] == [("ColorParam", None), ("ColorLayer_R", None)]


def test_without_a_game_the_mods_own_files_are_drawn(pil, make_mod):
    mod = make_mod({mesh_file(): weapon_mesh(), mdf2_file(): mdf2()})  # its texture is the game's
    look = L.preview(MAIN, mod, None, 48, compare=True)
    assert look.status == "ok" and look.image is not None and look.before is None
    assert "게임 폴더가 없어 원래 모습은 그릴 수 없습니다" in look.notes
    assert "게임 폴더가 없어 게임의 텍스처를 쓰는 부분은 단색으로 그렸습니다" in look.notes
    # a texture-only mod: no model to draw, its texture as a swatch
    look = L.preview(MAIN, make_mod({tex_file(): solid(BLUE)}), None, 48)
    assert look.status == "no_mesh" and look.image is None
    assert look.notes == ["게임 폴더가 없어 게임의 모델은 그릴 수 없습니다"]
    [(name, before, after, new)] = look.swatches
    assert name.lower() == "it0900_0021_0_albd.tex" and before is None and leans(colour(after), 2)
    assert not new  # without the game it is not known to be a new texture
    # nothing at all
    assert L.preview(MAIN, None, None, 48).status == "no_mesh"


def test_textures_the_game_does_not_have_are_new(pil, make_mod):
    own = TEXTURES + "my_ALBD.tex"
    look = L.preview(MAIN, make_mod({mdf2_file(): mdf2(material(albedo=own)), tex_file(own): solid(BLUE)}), game(), 48)
    assert [(name, before is None, new) for name, before, _after, new in look.swatches] == [("my_ALBD.tex", True, True)]
    # the game's copy could not be read: not "new"
    look = L.preview(MAIN, make_mod({tex_file(): solid(BLUE)}), game(**{tex_file(): b"TEX\x00broken"}), 48)
    assert [(name, before is None, new) for name, before, _after, new in look.swatches] == [
        ("it0900_0021_0_ALBD.tex", True, False)]
    assert L.preview(MAIN, make_mod({tex_file(): solid(BLUE)}), game(), 48, extras=False).swatches == []


def test_big_pictures_are_drawn_without_supersampling(pil, make_mod, monkeypatch):
    used = []
    real = L.render.render
    monkeypatch.setattr(L.render, "render", lambda *a, **k: (used.append(k["ss"]), real(*a, **k))[1])
    mod = make_mod({tex_file(): solid(BLUE)})
    for size in (L.SUPERSAMPLE_SIZE, L.SUPERSAMPLE_SIZE + 1):
        look = L.preview(MAIN, mod, game(), size, compare=True)
        assert look.image.size == look.before.size == (size, size)
    assert used == [2, 2, 1, 1]  # the pair always alike


def test_before_and_after_share_one_framing(pil, make_mod):
    mod = make_mod({mesh_file(): weapon_mesh(shape=square(size=2.0))})
    g = game(**{mesh_file(): weapon_mesh(shape=square(size=1.0))})
    pair = L.preview(MAIN, mod, g, 96, compare=True)
    assert 0.4 < width(pair.before) / width(pair.image) < 0.6  # half the size, as the models are
    alone = L.preview(MAIN, mod, g, 96)
    vanilla = L.preview(MAIN, None, g, 96)
    assert abs(width(alone.image) - width(vanilla.image)) <= 2  # each fills its own picture
    assert width(pair.image) == width(alone.image)


def test_cut_outs_and_two_sided_materials(pil, make_mod):
    half = TF.build_tex(8, 8, 28, [bytes([*BLUE, 255]) * 32 + bytes([*BLUE, 0]) * 32], gdeflate=False)
    film = material(slot="BaseAlphaMap", flags=0x0800009C)
    mod = make_mod({mdf2_file(): mdf2(film), tex_file(): half})
    look = L.preview(MAIN, mod, game(), 64)
    full = L.preview(MAIN, make_mod({tex_file(): half}), game(), 64)  # ALBD: alpha is not opacity
    covered = sum(1 for p in pixels(look.image) if p[3] > 128)
    assert 0 < covered < 0.75 * sum(1 for p in pixels(full.image) if p[3] > 128)


# ---- rules without pictures --------------------------------------------------------------------
def test_material_rules():
    s = L.surfaces(mdf2(material(slot="ALBD", albedo="a_ALBD.tex", more=[("BaseDielectricMap", "b_ALBD.tex")])))
    assert s["body"].albedo == "b_ALBD.tex" and s["body"].slot == "BaseDielectricMap"  # slot priority
    s = L.surfaces(mdf2(material(albedo="systems/rendering/NullWhite.tex")))["body"]
    assert s.albedo is None and s.constant == (255, 255, 255)
    ref = [("AlphaTest_Ref", (0.1,))]
    atos = [("AlphaTranslucentOcclusionSSSMap", ATOS)]
    s = L.surfaces(mdf2(material(flags=L.BASE_ALPHA_TEST, props=ref, more=atos)))["body"]
    assert (s.opacity, s.atos, s.cut) == ("atos", ATOS, 0.25)
    s = L.surfaces(mdf2(material(flags=OPAQUE, props=ref, more=atos)))["body"]  # AlphaMaskUsed alone: no holes
    assert s.opacity is None and s.cut is None and not s.two_sided
    s = L.surfaces(mdf2(material(slot="BaseAlphaMap", props=[("AlphaTest_Ref", (0.5,))])))["body"]
    assert (s.opacity, s.cut) == ("albedo", 0.5)
    assert L.surfaces(mdf2(material(flags=L.BASE_TWO_SIDE)))["body"].two_sided
    assert L.surfaces(mdf2(material(flags=L.FORCED_TWO_SIDE)))["body"].two_sided
    slots = [("Detail_ALBD_R", "detail_ALBD.tex"), ("PanoramaMap", "k_PAN_ALB.tex"), ("SomeMap", "y_alb.tex")]
    s = L.surfaces(mdf2(material(albedo=None, more=slots)))["body"]
    assert s.albedo == "y_alb.tex"  # the "_alb" fallback skips detail and panorama slots
    s = L.surfaces(mdf2(material(props=[("ColorParam", (0.5, float("nan"), 9.0, 1.0))])))["body"]
    assert s.tint == (0.5, 0.0, 4.0)
    with pytest.raises(EquipError):
        L.surfaces(b"MDF\x00" + bytes(40))


def test_which_meshes_make_up_a_part(tmp_path):
    files = L.game_files()
    waist = Target("armor", "f/003/001", "waist")
    base = "natives/stm/art/model/character/ch03/003/001/5/ch03_003_0015"
    assert files.files(waist, "mesh") == [f"{base}{s}.mesh.241111606" for s in ("", "_0", "_9")]
    assert L.mesh_plan(waist) == ([f"{base}.mesh.241111606"], [f"{base}.mesh.241111606"])  # the main mesh only
    alt = f"{base}_9.mesh.241111606"
    mod = loose_mod(tmp_path, {alt.replace("natives/stm", "natives/STM"): b"x"})
    assert L.mesh_plan(waist, mod) == ([alt], [alt])  # the mod changes only an alternative: compare that one
    extra = f"{base}_extra.mesh.241111606"
    mod = loose_mod(tmp_path, {extra: b"x", f"{base}.mdf2.45": b"y"})
    assert L.mesh_plan(waist, mod) == ([f"{base}.mesh.241111606", extra], [f"{base}.mesh.241111606"])
    # weapons one part at a time
    assert L.mesh_plan(MAIN)[0] == [mesh_file().lower()] and L.mesh_plan(SUB)[0] == [mesh_file(SHIELD).lower()]
    assert L.mesh_plan(Target("weapon", "09/000021"))[0] == [mesh_file().lower(), mesh_file(SHIELD).lower()]
    # a look swap: the part's prefab loads another equipment's mesh
    helm, other = Target("armor", "f/004/001", "helm"), Target("armor", "f/003/001", "helm")
    swapped, own = L.mesh_plan(helm, loose_mod(tmp_path, {}), [(helm, other)])
    assert swapped == L.main_meshes(files.files(other, "mesh")) and swapped
    assert own == L.main_meshes(files.files(helm, "mesh")) and own != swapped


def test_textures_too_big_to_decode_every_time(pil, tmp_path, monkeypatch):
    big = solid(BLUE, 16)  # one mip: "big" once BIG_MIP is lowered
    g = game_of({tex_file(): big})
    monkeypatch.setattr(L, "BIG_MIP", 8)
    img, src, problem = L.Textures(tmp_path / "tex").load([g], ALBD, 64)
    assert problem is None and src is g and img.size == (16, 16)
    assert len(list((tmp_path / "tex").glob("*.png"))) == 1
    monkeypatch.setattr(T.Tex, "to_image", lambda *a, **k: pytest.fail("decoded again"))
    again, _src, _problem = L.Textures(tmp_path / "tex").load([g], ALBD, 64)  # a new session: from the disk
    assert again.tobytes() == img.tobytes()
    monkeypatch.setattr(L, "HUGE_MIP", 8)
    assert L.Textures(tmp_path / "tex").load([g], ALBD, 64)[2] == "huge"
    look = L.preview(MAIN, None, game(**{tex_file(): big}), 48)
    assert look.status == "ok" and look.notes == ["텍스처가 너무 커서 단색으로 그렸습니다: it0900_0021_0_ALBD.tex"]


def test_textures_are_shared_from_memory(pil, monkeypatch):
    g = game()
    textures = L.Textures()
    first = textures.load([g], ALBD, 64)[0]
    monkeypatch.setattr(T.Tex, "to_image", lambda *a, **k: pytest.fail("decoded again"))
    assert textures.load([g], ALBD, 64)[0] is first
    assert textures.load([game_of({})], ALBD, 64) == (None, None, "missing")
    broken = game_of({tex_file(): b"TEX\x00broken"})
    img, src, problem = textures.load([broken], ALBD, 64)
    assert img is None and src is broken and problem.startswith("텍스처 파일을 읽지 못했습니다")


def test_unreadable_mod_paks_become_notes(pil, tmp_path):
    bad = tmp_path / "Bad.pak"
    bad.write_bytes(b"not a pak")
    mod = L.ModSource({"Bad.pak": bad})
    assert mod.errors == ["Bad.pak: pak 파일을 읽지 못했습니다 (pak 파일이 아닙니다)"]
    look = L.preview(MAIN, mod, game(), 48)
    assert look.status == "ok" and look.notes[0] == mod.errors[0]
    gone = L.ModSource({tex_file(): tmp_path / "removed.bin"})  # a stored file deleted meanwhile
    look = L.preview(MAIN, gone, game(), 48)
    assert look.status == "ok" and any(n.startswith("텍스처를 읽지 못했습니다") for n in look.notes)


def test_preview_records(tmp_path):
    (tmp_path / "a.png").write_bytes(b"png")
    p = L.Preview("missing", tmp_path / "a.png", None, ["모드 파일 누락: x.tex"],
                  [L.Swatch("x.tex", None, tmp_path / "a.png", True), L.Swatch("y.tex", None, tmp_path / "a.png")],
                  [L.Chip("body", "ColorParam", (1.0, 0.0, 0.0, 1.0))], 12, True)
    again = L.Preview.from_dict(p.to_dict(), tmp_path)
    assert again == p and again.text == "모드 파일 누락" and [s.new for s in again.swatches] == [True, False]
    old = dict(p.to_dict(), swatches=[["x.tex", None, "a.png"]])  # a record without the "new" flag
    assert L.Preview.from_dict(old, tmp_path).swatches == [L.Swatch("x.tex", None, tmp_path / "a.png")]
    assert L.Preview.from_dict(dict(p.to_dict(), format=0), tmp_path) is None
    assert L.Preview.from_dict({"status": "ok"}, tmp_path) is None
    (tmp_path / "a.png").unlink()
    assert L.Preview.from_dict(p.to_dict(), tmp_path) is None  # its picture is gone: made again
