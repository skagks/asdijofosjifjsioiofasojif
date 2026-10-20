"""The equipment viewer end to end: a synthetic game in a real (encrypted) pak, mods installed as loose
files and as a .pak, the manager's caches, the command line and the window."""

import gc
import queue
import time

import pytest

import equip_tex_fixtures as F
import test_equip_catalog as C
import test_matfix as M
from conftest import make_tree, write
from mhwmm import cli, matfix
from mhwmm.equip import catalog as equip_catalog
from mhwmm.equip import icons, pathlist
from mhwmm.equip.model import Target
from mhwmm.manager import ModManager
from mhwmm.matfix import pak

HELM_PICTURE = C.thumb_tex("ch03", "ch03_00_005_1_0")  # female, type 2, helm of the synthetic "시험" set
BLADES = next(p for p in pathlist.game_paths() if "/it02/00/0029/" in p and p.endswith(".mesh.241111606"))


@pytest.fixture
def equip_game(tmp_path):
    if not matfix.available():
        pytest.skip("게임 pak을 읽을 zstd 모듈이 없습니다")
    files = {}
    for part_files, present in C.game_parts():
        files.update(part_files)
        files.update((path, b"") for path in present)
    picture = F.build_tex(64, 64, 28, [bytes([200, 60, 40, 255]) * 64 * 64, bytes([200, 60, 40, 255]) * 32 * 32])
    files[HELM_PICTURE] = picture
    files[icons.PALETTE_PATH] = F.build_gcp([(0, 0, 0, 255)] * 22 + [(10 * n, 100, 200, 255) for n in range(1, 13)])
    game = tmp_path / "MonsterHunterWilds"
    write(game / "MonsterHunterWilds.exe", "exe")
    M.write_game_pak(game / "re_chunk_000.pak", files)
    return game


@pytest.fixture
def equip_manager(tmp_path, equip_game):
    m = ModManager(tmp_path / "data")
    m.settings.check_updates = m.settings.check_refw_updates = False
    m.settings.check_game_running = False
    m.set_game_dir(equip_game)
    mods = {}
    for name, files in (
            ("Trial Helm", {"natives/STM/Art/Model/Character/ch03/004/001/3/ch03_004_0013.mdf2.45": "m",
                            "natives/STM/Art/Model/Character/ch03/004/001/2/ch03_004_0012.mesh.241111606": "b"}),
            ("Other Helm", {"natives/STM/Art/Model/Character/ch03/004/001/3/textures/ch03_004_0013_ALBD.tex.241106027": "t"}),
            ("Script", {"reframework/autorun/x.lua": "--"})):
        with m.prepare_install(make_tree(tmp_path / "src" / name, files)) as s:
            mods[name] = m.install_package(s, s.variants)
    blades = tmp_path / "src" / "Blades.pak"
    blades.parent.mkdir(parents=True, exist_ok=True)
    pak.write_pak([(pak.path_hash(BLADES), b"mesh", 4, pak.COMPRESSION_NONE)], blades)
    with m.prepare_install(blades) as s:
        mods["Blades"] = m.install_package(s, s.variants)
    for mod in mods.values():
        mod.enabled = True
    return m, mods


def overview(m):
    from mhwmm.equip import overview as ov

    jobs = m.equipment_jobs()
    catalog = m.equipment_catalog()
    scans = m.equipment_scans(jobs)
    return ov.build([(j.mod_id, j.name, j.enabled, j.order, scans[j.mod_id]) for j in jobs], catalog), catalog


def test_manager_names_equipment_from_the_game(equip_manager, monkeypatch):
    m, mods = equip_manager
    result, catalog = overview(m)
    assert catalog is not None and not catalog.problems
    by_target = {i.target: i for i in result.items}
    helm = by_target[Target("armor", "f/004/001")]
    assert helm.name == "시험 · Trial α" and helm.subtitle == "여성 · 타입 2"
    assert [u.name for u in helm.uses] == ["Trial Helm", "Other Helm"]
    assert list(helm.conflicts()) == ["helm"]  # both change the helm: the later one wins
    blades = by_target[Target("weapon", "02/000029")]
    assert [u.name for u in blades.uses] == ["Blades"]  # found inside the .pak by its path hash
    assert result.mods_without == ["Script"]
    assert any("시험" in line and "머리" in line for line in m.equipment_summary(mods["Trial Helm"].id))
    assert m.equipment_summary(mods["Script"].id) == []

    # cached: a new manager reads neither the game's tables nor the mods again
    monkeypatch.setattr(equip_catalog, "build", lambda *a, **k: pytest.fail("built again"))
    again = ModManager(m.data_dir)
    assert again.equipment_catalog().names(Target("armor", "f/004/001")) == ["시험", "Trial α"]
    assert again.equipment_summary(mods["Trial Helm"].id)
    monkeypatch.setattr("mhwmm.equip.scan.scan_files", lambda *a, **k: pytest.fail("scanned again"))
    assert set(again.equipment_scans(again.equipment_jobs())) == {mod.id for mod in mods.values()}


def test_a_changed_mod_is_looked_at_again(equip_manager):
    m, mods = equip_manager
    overview(m)
    m.set_enabled(mods["Other Helm"].id, False)
    m.remove(mods["Trial Helm"].id)
    result, _catalog = overview(m)
    helm = next(i for i in result.items if i.target == Target("armor", "f/004/001"))
    assert [(u.name, u.enabled) for u in helm.uses] == [("Other Helm", False)] and not helm.conflicts()


def test_pictures_and_game_colours(equip_manager):
    pytest.importorskip("PIL")
    m, _mods = equip_manager
    catalog = m.equipment_catalog()
    tex = catalog.thumb(Target("armor", "f/004/001", "helm"))
    assert tex == HELM_PICTURE
    png = m.equipment_thumbnail(tex, 48)
    from PIL import Image

    with Image.open(png) as img:
        assert img.size == (48, 48) and img.getpixel((24, 24))[:3] == (200, 60, 40)
    assert m.equipment_thumbnail(tex, 48) == png  # cached
    assert m.equipment_thumbnail(C.thumb_tex("ch02", "ch02_00_061_0_4"), 48) is None  # an empty file in the game
    ui = m.equipment_ui()
    assert ui["rarity"]["1"] == "#0a64c8" and ui["weapon_icons"] == {}  # no icon atlas in this game


def test_without_a_game_numbers_are_shown(tmp_path):
    m = ModManager(tmp_path / "data")
    with m.prepare_install(make_tree(tmp_path / "src" / "A", {
            "natives/STM/Art/Model/Character/ch02/004/000/2/ch02_004_0002.mesh.241111606": "x"})) as s:
        mod = m.install_package(s, s.variants)
    result, catalog = overview(m)
    assert catalog is None and [i.name for i in result.items] == ["방어구 004-000"]
    assert m.equipment_summary(mod.id) == ["방어구 004-000 (남성: 몸통)"]


def test_cli_lists_each_mods_equipment(equip_manager, capsys):
    m, _mods = equip_manager
    assert cli.main(["--data-dir", str(m.data_dir), "equipment"]) == 0
    out = capsys.readouterr().out
    assert "Trial Helm" in out and "방어구 시험 · Trial α (여성 · 타입 2" in out and "[머리, 몸통]" in out
    assert "겹침: 머리 - 'Other Helm'도 바꿈 (같은 파일은 아래쪽 'Other Helm' 우선)" in out
    assert "Blades" in out and "Script" not in out


def test_equipment_window(equip_manager, monkeypatch):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("화면(display)이 없어 GUI 테스트를 건너뜁니다")
    from mhwmm import gui

    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: None)
    m, mods = equip_manager
    m.settings.check_materials = False
    try:
        app = gui.App(root, m, queue.Queue())

        def pump(until, timeout=15.0):
            end = time.time() + timeout
            while time.time() < end:
                root.update()
                if until():
                    return
                time.sleep(0.02)
            raise AssertionError("시간 초과")

        app.show_equipment()
        window = app.equipment_window
        pump(lambda: window.overview is not None)
        # Other Helm's texture is also the male body's type-2 helm (that folder has no textures of its own)
        assert list(window.tiles) == [Target("armor", "f/004/001"), Target("armor", "m/004/001"),
                                      Target("weapon", "02/000029")]
        assert window.selected == Target("armor", "f/004/001")
        assert str(window.tiles[window.selected].cget("style")) == "TileSel.TFrame"
        labels = [b.cget("text") for b in window.nav.winfo_children()]
        assert labels[0] == "전체  3" and "방어구  2" in labels and "쌍검(게임)  1" in labels
        window.select(Target("weapon", "02/000029"))
        window._set_group("weapon/02")
        assert list(window.tiles) == [Target("weapon", "02/000029")]
        window.filter_mod(mods["Other Helm"].id)
        assert list(window.tiles) == [Target("armor", "f/004/001"), Target("armor", "m/004/001")]
        assert window.mod_var.get() == mods["Other Helm"].id and window.mod_button.cget("text") == "모드: Other Helm"
        window.search_var.set("없는 이름")
        window._search_now()  # (typing waits a moment before filtering)
        assert not window.tiles
        window.search_var.set("")
        window._search_now()
        app.show_equipment(mods["Blades"].id)  # the open window is reused
        assert app.equipment_window is window and list(window.tiles) == [Target("weapon", "02/000029")]
        app.show_mod(mods["Blades"].id)
        assert app.tree.selection() == (mods["Blades"].id,)

        # a mod's own picture opens over the window it was clicked in (not over the main window,
        # which Windows would bring to the front with the picture)
        blades = m.get(mods["Blades"].id)
        picture = tk.PhotoImage(master=root, width=32, height=18)
        picture.put("#3060c0", to=(0, 0, 32, 18))
        picture.write(str(m.mod_dir(blades.id) / "preview.png"), format="png")
        blades.screenshot = "preview.png"
        shown = []
        monkeypatch.setattr(gui.ImageDialog, "show", lambda self: shown.append(self.master) or self.destroy())
        window.select(Target("weapon", "02/000029"))
        label = next(w for w in window._descendants(window.detail)
                     if isinstance(w, gui.ttk.Label) and str(w.cget("cursor")) == "hand2")
        label.event_generate("<Button-1>")
        root.update()
        assert shown == [window]

        # switching a mod off in the main list reloads the window
        app.toggle([mods["Blades"].id])
        pump(lambda: not window.overview.items[-1].enabled if window.overview.items else False)
        window.close()
        assert app.equipment_window is None
    finally:
        gc.collect()
        root.destroy()


def test_diagnostics_report(equip_manager, tmp_path):
    import zipfile

    m, _mods = equip_manager
    overview(m)
    target = tmp_path / "report.zip"
    m.export_equipment_report(target)
    with zipfile.ZipFile(target) as zf:
        names = set(zf.namelist())
        report = zf.read("report.txt").decode("utf-8")
    assert "report.txt" in names and "equip-scans.json" in names and any(n.startswith("equip-catalog-") for n in names)
    assert "ArmorThumbnailTextureData: " in report and "read ok" in report
    assert "it0200_0083_IMLM4" in report
