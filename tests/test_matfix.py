"""Material repair against a small fake game: encrypted (and chunked) paks, an
outdated costume mod as loose files and as a mod pak."""

import os
import struct
from pathlib import Path

import pytest

from mhwmm import matfix

pytestmark = pytest.mark.skipif(not matfix.available(), reason="zstd 모듈이 없습니다")

from mhwmm.matfix import pak as pakmod  # noqa: E402
from mhwmm.matfix.game import GameIndex  # noqa: E402
from mhwmm.matfix.mdf2 import Mdf2File, assemble_mdf2, extract_material  # noqa: E402
from mhwmm.matfix.repair import NULL_MASK, check_mod  # noqa: E402
import test_rsz as R  # noqa: E402

BODY = "natives/STM/Art/Model/Character/ch03/030/601/1/ch03_030_6011"
LEGS = "natives/STM/Art/Model/Character/ch03/030/601/2/ch03_030_6012"
WEAPON = "natives/STM/Art/Model/Item/it01/00/0001/it0100_0001"
SHADER = "MaterialShader/Variation/Base_Equip.mmtr"
AVP = "natives/STM/GameDesign/Equip/_Prefab/Armor/Female/004/000/004_000_avp.user.3"


def material(name, textures, props, mmtr=SHADER, alpha=b"\x98\x00\x00\x88", gpbf=(("MultiBlend_BAB", "g.gpbf"),)):
    return {"name": name, "mmtr_path": mmtr, "shading_type": 0, "alpha_flags_raw": alpha,
            "isV2_unk_0x24": 0, "isV2_unk_0x2C": 0, "isV2_unk_0x30": 0, "gpbf_split": (len(gpbf), len(gpbf)),
            "gpbf_entries": [(slot, 1, 2) for slot, _ in gpbf] + [(path, 0, 1) for _, path in gpbf],
            "textures": [{"type": t, "path": p, "ukn": 0} for t, p in textures],
            "props": [{"name": n, "values": list(v), "count_flags": 0} for n, v in props], "header_version": 1}


def current_body():
    """The game's material now: a new DetailMaskMap slot and a new shader parameter."""
    return [material("body", [("BaseDielectricMap", "Art/ch03/body_ALBD.tex"), ("NormalRoughnessMap", "Art/ch03/body_NRRO.tex"),
                              ("DetailMaskMap", "Art/other/someone_MSK.tex")],
                     [("Color", (1.0, 1.0, 1.0, 1.0)), ("Roughness", (0.5,)), ("NewParam", (0.25,))]),
            material("skin", [("BaseDielectricMap", "Art/ch03/skin_ALBD.tex")], [("Color", (1.0, 1.0, 1.0, 1.0))])]


def old_body_mod():
    """What the mod shipped, made before the update: its own textures and tuning."""
    return [material("body", [("BaseDielectricMap", "Art/Dimcirui/chen/body_ALBD.tex"),
                              ("NormalRoughnessMap", "Art/Dimcirui/chen/body_NRRO.tex")],
                     [("Color", (0.2, 0.4, 0.6, 1.0)), ("Roughness", (0.9,))], alpha=b"\x9b\x00\x00\x08",
                     gpbf=(("MultiBlend_BAB", "systems/rendering/Empty.gpbf"),)),
            material("skin", [("BaseDielectricMap", "Art/Dimcirui/chen/skin_ALBD.tex")], [("Color", (1.0, 0.8, 0.8, 1.0))])]


def write_game_pak(path: Path, files: dict, chunked: bool = False):
    """An encrypted game pak (feature 8), or a chunked one (feature 40)."""
    key = os.urandom(127) + b"\x00"  # below the modulus
    blobs, rows = [], []
    offset = 0
    header_size = 16 + 48 * len(files) + 128 + (8 + 8 * len(files) if chunked else 0)
    chunk_rows = []
    for i, (name, data) in enumerate(files.items()):
        stored = matfix.zstd_compress(data)
        h = pakmod.path_hash(name)
        if chunked:  # one chunk per file; offset holds the chunk index
            chunk_rows.append((header_size + offset, len(stored) << 10))
            rows.append(struct.pack("<IIqqqqQ", h & 0xFFFFFFFF, h >> 32, i, len(stored), len(data), 0x1000000, 0))
        else:
            rows.append(struct.pack("<IIqqqqQ", h & 0xFFFFFFFF, h >> 32, header_size + offset, len(stored), len(data), 2, 0))
        blobs.append(stored)
        offset += len(stored)
    table = pakmod.xor_table(b"".join(rows), key)
    with open(path, "wb") as f:
        f.write(struct.pack("<IBBhii", pakmod.MAGIC, 4, 0, 40 if chunked else 8, len(files), 0))
        f.write(table + key)
        if chunked:
            f.write(struct.pack("<ii", 524288, len(chunk_rows)))
            for off, size in chunk_rows:
                f.write(struct.pack("<II", off, size))
        for b in blobs:
            f.write(b)


@pytest.fixture
def game(tmp_path):
    root = tmp_path / "MonsterHunterWilds"
    root.mkdir()
    write_game_pak(root / "re_chunk_000.pak", {
        BODY + ".mdf2.45": b"stale base copy",
        LEGS + ".mdf2.45": assemble_mdf2([material("legs", [("BaseDielectricMap", "Art/ch03/legs.tex")],
                                                   [("Color", (1.0,) * 4)])], 45),
        WEAPON + ".mdf2.45": assemble_mdf2(current_body()[:1], 45),
        AVP: R.game_file(),
    })
    # the latest patch wins over the base pak; this one is chunked
    write_game_pak(root / "re_chunk_000.pak.patch_001.pak", {BODY + ".mdf2.45": assemble_mdf2(current_body(), 45)},
                   chunked=True)
    return root


def test_pak_index_layers_patches_and_caches(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    assert len(index) == 4
    assert index.read(BODY + ".mdf2.45") == assemble_mdf2(current_body(), 45)  # from the chunked patch
    assert index.versioned_path(BODY.lower(), "mdf2") == BODY.lower() + ".mdf2.45"  # paths are case-insensitive
    assert index.read_versioned(BODY, "mdf2", prefer=(40,))[0].endswith(".mdf2.45")
    assert list((tmp_path / "cache").glob("pakindex-*.bin"))
    again = GameIndex(game, tmp_path / "cache")  # from the cache
    assert list(again.hashes) == list(index.hashes) and again.read(LEGS + ".mdf2.45") == index.read(LEGS + ".mdf2.45")


def stage(tmp_path, files):
    out = []
    for rel, data in files.items():
        path = tmp_path / "mod" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        out.append((rel, rel, path))
    return out


def to(tmp_path):
    """check_mod's ``out``: repaired files go to tmp_path/out/<new game path>."""
    return lambda result: tmp_path / "out" / result.new_rel


def test_outdated_loose_material_is_rebuilt(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    files = stage(tmp_path, {
        BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45),
        LEGS + ".mdf2.45": assemble_mdf2([material("legs", [("BaseDielectricMap", "Art/mine/legs.tex")],
                                                   [("Color", (0.1,) * 4)])], 45),  # already current
        "natives/STM/Art/Dimcirui/chen/body_ALBD.tex.241106027": b"tex",
    })
    (body, legs) = check_mod(files, index, None)
    assert (body.state, legs.state) == ("fixable", "current")
    assert body.fixed == ["body"] and body.new_rel == BODY + ".mdf2.45" and body.file is None

    (body, _legs) = check_mod(files, index, None, out=to(tmp_path))
    fixed = Mdf2File(body.file.read_bytes(), 45)
    got = {m.name.text: extract_material(fixed, m.index) for m in fixed.materials}
    tex = {t["type"]: t["path"] for t in got["body"]["textures"]}
    assert tex == {"BaseDielectricMap": "Art/Dimcirui/chen/body_ALBD.tex",
                   "NormalRoughnessMap": "Art/Dimcirui/chen/body_NRRO.tex",
                   "DetailMaskMap": NULL_MASK}  # a new mask never takes another model's texture
    props = {p["name"]: p["values"] for p in got["body"]["props"]}
    assert props["Color"] == pytest.approx([0.2, 0.4, 0.6, 1.0]) and props["Roughness"] == pytest.approx([0.9])
    assert props["NewParam"] == [0.25]  # new parameter: the game's value
    assert got["body"]["alpha_flags_raw"] == b"\x9b\x00\x00\x08"  # the mod's render state is kept
    assert got["body"]["gpbf_entries"][1][0] == "systems/rendering/Empty.gpbf"
    # the untouched material keeps exactly what the mod had
    assert got["skin"]["props"][0]["values"] == pytest.approx([1.0, 0.8, 0.8, 1.0])


def test_old_version_number_is_renamed(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    current_legs = [material("legs", [("BaseDielectricMap", "Art/mine/legs.tex")], [("Color", (0.1,) * 4)])]
    (legs,) = check_mod(stage(tmp_path, {LEGS + ".mdf2.40": assemble_mdf2(current_legs, 40)}), index, None, out=to(tmp_path))
    assert legs.state == "fixable" and legs.new_rel == LEGS + ".mdf2.45" and legs.fixed == []
    assert legs.file.read_bytes() == assemble_mdf2(current_legs, 40)  # same layout: only the name changes


def test_custom_slot_and_unknown_shader(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    mh = BODY.replace("ch03/030", "mh03/030").replace("ch03_030", "mh03_030")
    mod = old_body_mod() + [material("glow", [("EmissiveMap", "Art/x/glow.tex")], [], mmtr="Custom/Nobody.mmtr")]
    (body,) = check_mod(stage(tmp_path, {mh + ".mdf2.45": assemble_mdf2(mod, 45)}), index, None, out=to(tmp_path))
    assert body.state == "fixable" and body.new_rel == mh + ".mdf2.45"  # keeps the mod's own slot path
    assert body.unresolved == ["glow"]
    fixed = Mdf2File(body.file.read_bytes(), 45)
    assert [m.name.text for m in fixed.materials] == ["body", "skin", "glow"]
    assert extract_material(fixed, 2) == extract_material(Mdf2File(assemble_mdf2(mod, 45), 45), 2)  # left as shipped


def test_mod_pak_is_patched_in_place(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    weapon_mod = [material("body", [("BaseDielectricMap", "Art/myweapon/w_ALBD.tex"),
                                    ("NormalRoughnessMap", "Art/myweapon/w_NRRO.tex")],
                           [("Color", (0.3, 0.3, 0.3, 1.0)), ("Roughness", (0.1,))])]
    mod_pak = tmp_path / "mod" / "Weapon.pak"
    mod_pak.parent.mkdir(parents=True)
    entries = [(pakmod.path_hash("natives/STM/Art/myweapon/w_ALBD.tex.241106027"), b"TEX" * 100, 300, 0),
               (pakmod.path_hash(WEAPON + ".mdf2.45"), assemble_mdf2(weapon_mod, 45), 0, 0)]
    pakmod.write_pak([(h, d, len(d), c) for h, d, _, c in entries], mod_pak)
    (result,) = check_mod([("Weapon.pak", "Weapon.pak", mod_pak)], index, None, out=to(tmp_path))
    assert result.state == "fixable" and result.fixed == ["body"]
    fixed = pakmod.PakFile(result.file)
    assert fixed.order == pakmod.PakFile(mod_pak).order  # same entries, same order
    assert fixed.read_hash(entries[0][0]) == b"TEX" * 100
    mat = extract_material(Mdf2File(fixed.read_hash(entries[1][0]), 45), 0)
    assert {t["type"]: t["path"] for t in mat["textures"]}["BaseDielectricMap"] == "Art/myweapon/w_ALBD.tex"
    assert {p["name"]: p["values"] for p in mat["props"]}["Roughness"] == pytest.approx([0.1])
    assert {p["name"] for p in mat["props"]} == {"Color", "Roughness", "NewParam"}


def test_current_mod_is_left_alone(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    mine = current_body()
    mine[0]["textures"][0]["path"] = "Art/mine/body.tex"
    (body,) = check_mod(stage(tmp_path, {BODY + ".mdf2.45": assemble_mdf2(mine, 45)}), index, None)
    assert body.state == "current" and not body.fixed


def test_in_place_only_when_the_materials_are_the_current_files(game, tmp_path):
    from mhwmm.matfix import repair

    index = GameIndex(game, tmp_path / "cache")

    def patched(mod):
        unit = repair._loose_unit(BODY + ".mdf2.45", assemble_mdf2(mod, 45), index)
        repair.plan_entries([unit], None)
        wanted = [repair.apply_overrides(m.donor, m.mod, []) if m.stale else m.mod for m in unit.materials]
        return unit, repair._patch_in_place(unit, wanted)

    unit, data = patched(old_body_mod())  # same materials as the game's file: patched in place
    assert data is not None and repair.build_entry(unit)[0] == data

    renamed = old_body_mod()
    renamed[0]["name"] = "body_custom"  # the mod's mesh names its materials differently
    assert patched(renamed)[1] is None  # in place would rename the material

    extra = old_body_mod()
    extra[1]["textures"].append({"type": "EmissiveMap", "path": "Art/x/glow.tex", "ukn": 0})  # a slot of its own
    unit, data = patched(extra)
    assert data is None
    rebuilt = Mdf2File(repair.build_entry(unit)[0], 45)
    assert [t["type"] for t in extract_material(rebuilt, 1)["textures"]] == ["BaseDielectricMap", "EmissiveMap"]


def test_compressed_mod_pak_entry_stays_compressed(game, tmp_path):
    index = GameIndex(game, tmp_path / "cache")
    data = assemble_mdf2([material("body", [("BaseDielectricMap", "Art/w.tex")], [("Color", (0.5,) * 4)])], 45)
    mod_pak = tmp_path / "Weapon.pak"
    pakmod.write_pak([(pakmod.path_hash(WEAPON + ".mdf2.45"), matfix.zstd_compress(data), len(data),
                       pakmod.COMPRESSION_ZSTD)], mod_pak)
    (result,) = check_mod([("Weapon.pak", "Weapon.pak", mod_pak)], index, None, out=to(tmp_path))
    fixed = pakmod.PakFile(result.file)
    assert fixed.attrs[0] & 0xF == pakmod.COMPRESSION_ZSTD
    assert len(Mdf2File(fixed.read(0), 45).materials[0].props) == 3


# ---- installed mods --------------------------------------------------------------
@pytest.fixture
def library(game, tmp_path):
    from mhwmm.manager import ModManager

    m = ModManager(tmp_path / "data")
    m.settings.check_game_running = False
    m.set_game_dir(game)
    return m


def install_tree(manager, base, files):
    for rel, data in files.items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_bytes(data)
    with manager.prepare_install(base) as session:
        return manager.install_package(session, session.variants)


def run(manager, mod_ids=None, repair=False, unchecked_only=False):
    jobs = manager.material_jobs(mod_ids, unchecked_only=unchecked_only)
    return manager.finish_materials(manager.run_materials(jobs, repair=repair), jobs)


def test_installed_mod_is_checked_repaired_and_restored(library, game, tmp_path):
    shipped = assemble_mdf2(old_body_mod(), 45)
    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": shipped, "natives/STM/Art/chen.tex": b"t"})
    assert library.material_state(mod.id) == "unchecked"
    assert library.material_files(mod.id) == [(BODY + ".mdf2.45", BODY + ".mdf2.45",
                                               library.files_dir(mod.id) / (BODY + ".mdf2.45"))]

    assert run(library, unchecked_only=True) == []  # checking alone changes nothing
    assert library.material_state(mod.id) == "stale"
    assert library.stale_material_mods() == [mod] and library.materials_to_offer() == [mod]
    library.mark_materials_offered([mod])
    assert library.materials_to_offer() == [] and library.material_state(mod.id) == "stale"
    library.deploy()

    assert run(library, [mod.id], repair=True) == [mod]
    assert library.material_state(mod.id) == "repaired" and library.dirty
    path, key = library.mod_entries(mod.id)[BODY + ".mdf2.45"]
    assert key == "matfix/" + BODY + ".mdf2.45" and path.parent.name == "matfix"
    assert (library.files_dir(mod.id) / (BODY + ".mdf2.45")).read_bytes() == shipped  # the original stays
    library.deploy()
    deployed = (game / (BODY + ".mdf2.45")).read_bytes()
    assert deployed == path.read_bytes() and deployed != shipped
    assert not list(library.temp_root.glob("matfix-*"))

    assert run(library, [mod.id], repair=True) == [mod]  # repairs always start from the original again
    assert library.material_state(mod.id) == "repaired"

    assert library.restore_materials(mod.id)
    assert library.material_state(mod.id) == "declined" and not (library.mod_dir(mod.id) / "matfix").exists()
    assert library.mod_entries(mod.id)[BODY + ".mdf2.45"][1] == BODY + ".mdf2.45"
    library.deploy()
    assert (game / (BODY + ".mdf2.45")).read_bytes() == shipped


def test_game_update_and_reinstall_need_a_new_check(library, game, tmp_path):
    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    run(library, [mod.id], repair=True)
    assert library.material_state(mod.id) == "repaired"

    patch = game / "re_chunk_000.pak.patch_001.pak"
    os.utime(patch, (patch.stat().st_atime, patch.stat().st_mtime + 100))  # a game update
    assert [j.mod_id for j in library.material_jobs(unchecked_only=True)] == [mod.id]
    assert library.material_state(mod.id) == "unchecked"
    assert library.mod_entries(mod.id)[BODY + ".mdf2.45"][1].startswith("matfix/")  # kept until checked again
    run(library, unchecked_only=True)
    assert library.material_state(mod.id) == "stale"  # repaired for the previous game version

    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(current_body(), 45)})
    assert library.material_state(mod.id) == "unchecked" and not library.repaired_materials(mod.id)


def test_versions_and_paks_of_installed_mods(library, game, tmp_path):
    legs = [material("legs", [("BaseDielectricMap", "Art/mine/legs.tex")], [("Color", (0.1,) * 4)])]
    old = install_tree(library, tmp_path / "Legs", {LEGS + ".mdf2.40": assemble_mdf2(legs, 40)})
    both = install_tree(library, tmp_path / "Both", {BODY + ".mdf2.40": assemble_mdf2(old_body_mod(), 40),
                                                     BODY + ".mdf2.45": assemble_mdf2(current_body(), 45)})
    weapon = tmp_path / "Weapon" / "Weapon.pak"
    weapon.parent.mkdir()
    data = assemble_mdf2([material("body", [("BaseDielectricMap", "Art/w.tex")], [("Color", (0.5,) * 4)])], 45)
    pakmod.write_pak([(pakmod.path_hash(WEAPON + ".mdf2.45"), data, len(data), 0)], weapon)
    with library.prepare_install(weapon) as session:
        pak_mod = library.install_package(session, session.variants)

    assert [rel for _, rel, _ in library.material_files(both.id)] == [BODY + ".mdf2.45"]  # the game reads .45
    run(library, repair=True)
    assert (library.material_state(old.id), library.material_state(both.id), library.material_state(pak_mod.id)) == \
        ("repaired", "current", "repaired")
    assert sorted(library.mod_files(old.id)) == [LEGS + ".mdf2.45"]  # renamed to the version the game reads
    library.deploy()
    assert (game / (LEGS + ".mdf2.45")).is_file() and not (game / (LEGS + ".mdf2.40")).exists()
    (placed,) = (game / "pak_mods").glob("*.pak")
    assert len(Mdf2File(pakmod.PakFile(placed).read(0), 45).materials[0].props) == 3


def test_gui_finds_offers_repairs_and_restores(library, tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    import queue
    import time

    from mhwmm import gui

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("화면(display)이 없어 GUI 테스트를 건너뜁니다")
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: None)
    s = library.settings
    s.check_updates = s.check_refw_updates = s.check_mod_updates = s.check_materials = False
    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    offered = []

    def show(dialog):
        offered.append((list(dialog.checked), dict(app._problems)))
        dialog.destroy()
        return [mod.id]

    monkeypatch.setattr(gui.MaterialsDialog, "show", show)

    def pump(until):
        end = time.time() + 20
        while time.time() < end:
            root.update()
            if until():
                return
            time.sleep(0.02)
        raise AssertionError("시간 초과")

    try:
        app = gui.App(root, library, queue.Queue())
        s.check_materials = True
        app.check_materials_background()  # what startup and installs do
        pump(lambda: offered and app.busy is None and library.material_state(mod.id) == "repaired")
        (checked, problems), = offered
        assert checked == [mod.id] and "재질" in problems[mod.id][0]
        assert mod.id not in app._problems
        pump(lambda: not library.dirty)  # applied right away
        app.tree.selection_set(mod.id)
        app._update_details()
        assert "재질 복구됨" in app.info_text.get("1.0", "end")
        statuses = [app.files_tree.item(i, "values")[1] for i in app.files_tree.get_children()]
        assert any("재질 복구본" in st for st in statuses)

        app.check_materials_background()  # nothing new: no second offer
        pump(lambda: not app._matfix_running)
        assert len(offered) == 1

        app.restore_materials(mod.id)
        assert library.material_state(mod.id) == "declined" and mod.id not in app._problems
    finally:
        root.destroy()


def test_cli_checks_repairs_and_restores(library, tmp_path, capsys):
    from mhwmm.cli import main

    mod = install_tree(library, tmp_path / "Chen", {BODY + ".mdf2.45": assemble_mdf2(old_body_mod(), 45)})
    data = str(library.data_dir)
    assert main(["--data-dir", data, "materials", "check"]) == 0
    out = capsys.readouterr().out
    assert "Chen: 복구 필요" in out and "body" in out
    assert main(["--data-dir", data, "matfix", "repair", "Chen"]) == 0
    assert "Chen: 복구됨" in capsys.readouterr().out
    assert main(["--data-dir", data, "materials", "restore", "Chen", "--no-deploy"]) == 0
    assert "원래 재질 파일로 되돌림" in capsys.readouterr().out
    from mhwmm.manager import ModManager
    assert ModManager(library.data_dir).material_state(mod.id) == "declined"



# ---- .user / .pfb files ----------------------------------------------------------
@pytest.fixture
def types(monkeypatch):
    """The small registry of test_rsz instead of the bundled one."""
    from mhwmm.matfix import rsz

    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", R.registry())


def old_avp(scale=2.5):
    return R.user_file([(R.CHILD, R.CHILD_CRC, R.CHILD_FIELDS, [R.s32(42)]),
                        (R.PARAM, R.OLD_CRC, R.OLD_PARAM, R.param(R.OLD_PARAM, scale=scale, hide=1))])


def test_installed_user_file_is_repaired_and_restored(library, game, tmp_path, types, caplog):
    shipped = old_avp()
    mod = install_tree(library, tmp_path / "Lepus", {AVP: shipped})
    run(library, unchecked_only=True)
    assert library.material_state(mod.id) == "stale"
    (result,) = library.material_results(mod.id)
    assert result.kind == "user" and result.rsz == [{"label": AVP, "kind": "user", "method": "재구성"}]

    assert run(library, [mod.id], repair=True) == [mod]
    assert library.material_state(mod.id) == "repaired" and not library.material_stuck(mod.id)
    path, key = library.mod_entries(mod.id)[AVP]
    assert key == "matfix/" + AVP and (library.files_dir(mod.id) / AVP).read_bytes() == shipped
    library.deploy()
    deployed = (game / AVP).read_bytes()
    values, info = R.fields_of(deployed)
    assert info.insts[2] == (R.PARAM, R.NEW_CRC) and values[(2, "_Scale")] == R.f32(2.5)

    assert library.restore_materials(mod.id)
    library.deploy()
    assert (game / AVP).read_bytes() == shipped


def test_user_file_inside_a_mod_pak(library, game, tmp_path, types):
    pak = tmp_path / "Lepus" / "Lepus.pak"
    pak.parent.mkdir()
    data = old_avp(scale=4.0)
    pakmod.write_pak([(pakmod.path_hash(AVP), data, len(data), 0),
                      (pakmod.path_hash("natives/STM/Art/x.tex.241106027"), b"TEX", 3, 0)], pak)
    with library.prepare_install(pak) as session:
        mod = library.install_package(session, session.variants)
    run(library, repair=True)
    assert library.material_state(mod.id) == "repaired"
    (result,) = library.material_results(mod.id)
    assert result.rsz[0]["method"] == "재구성" and result.rsz[0]["label"].endswith(".user")
    fixed = pakmod.PakFile(library.mod_entries(mod.id)["Lepus.pak"][0])
    values, info = R.fields_of(fixed.read_hash(pakmod.path_hash(AVP)))
    assert info.insts[2] == (R.PARAM, R.NEW_CRC) and values[(2, "_Scale")] == R.f32(4.0)
    assert fixed.read_hash(pakmod.path_hash("natives/STM/Art/x.tex.241106027")) == b"TEX"


def test_user_file_that_cannot_be_repaired_is_reported(library, game, tmp_path, types, monkeypatch):
    from mhwmm.matfix import rsz

    extra = {format(R.EXTRA, "x"): ["4444", "app.CustomChain", R.json.dumps(R.CHILD_FIELDS)]}
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", R.registry(**extra))
    short = R.PARAM_FIELDS[:-1]
    shipped = R.user_file([(R.EXTRA, 0x4444, R.CHILD_FIELDS, [R.s32(1)]),
                           (R.CHILD, R.CHILD_CRC, R.CHILD_FIELDS, [R.s32(42)]),
                           (R.PARAM, R.NO_CHILD_CRC, short, R.param(short))])
    mod = install_tree(library, tmp_path / "Lepus", {AVP: shipped})
    run(library, repair=True)
    (stuck,) = library.material_stuck(mod.id)
    assert stuck["label"] == AVP and "app.CustomChain" in stuck["reason"]
    assert library.material_state(mod.id) == "current" and not library.repaired_materials(mod.id)


def test_rsz_lines_in_the_log(library, game, tmp_path, types, caplog):
    pytest.importorskip("tkinter")
    import logging

    from mhwmm import gui

    mod = install_tree(library, tmp_path / "Lepus", {AVP: old_avp()})
    jobs = library.material_jobs([mod.id])
    found = library.run_materials(jobs, repair=True)
    with caplog.at_level(logging.INFO):
        gui.App._log_materials(None, found, jobs)
    library.finish_materials(found, jobs)
    assert f"'Lepus' {AVP}: user 복구 (재구성)" in caplog.text


def test_records_of_the_previous_version_are_checked_again(library, game, tmp_path, types):
    """v1.9.0 checked only .mdf2: its records must not hide a stale .user after the update."""
    mod = install_tree(library, tmp_path / "Lepus", {AVP: old_avp()})
    run(library, unchecked_only=True)
    record = dict(library._material_record(mod.id))
    game_now = library.game_version()
    record.update(results=[], offered=game_now)  # what v1.9.0 left behind: no format, mdf2 results only
    record.pop("format")
    library._save_material_record(mod.id, record)
    assert library.material_state(mod.id) == "unchecked"
    assert [j.mod_id for j in library.material_jobs(unchecked_only=True)] == [mod.id]
    run(library, unchecked_only=True)
    assert library.material_state(mod.id) == "stale" and library.materials_to_offer() == [mod]
