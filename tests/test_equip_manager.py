"""The equipment viewer end to end: a synthetic game in a real (encrypted) pak, mods installed as loose
files and as a .pak, the manager's caches, the command line and the window."""

import gc
import queue
import threading
import time

import pytest

import equip_mesh_fixtures as MF
import equip_tex_fixtures as F
import test_equip_catalog as C
import test_equip_look as LK
import test_matfix as M
from conftest import make_tree, make_zip, write
from equip_game_fixtures import pfb_file
from mhwmm import cli, matfix
from mhwmm.equip import catalog as equip_catalog
from mhwmm.equip import icons, look, pathlist
from mhwmm.equip import scan as equip_scan
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
    selftest = zf_text(target, "preview-selftest.txt")
    assert selftest.startswith("preview self-test: ") and "== summary" in selftest


def test_diagnostics_report_survives_a_failing_self_test(equip_manager, tmp_path, monkeypatch):
    m, _mods = equip_manager

    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr("mhwmm.equip.selftest.run", boom)
    target = tmp_path / "report.zip"
    m.export_equipment_report(target)
    assert zf_text(target, "preview-selftest.txt") == "preview self-test failed: RuntimeError: bug\n"


def zf_text(path, name: str) -> str:
    import zipfile

    with zipfile.ZipFile(path) as zf:
        return zf.read(name).decode("utf-8")


# ---- 3D previews -------------------------------------------------------------------------------
LONG_SWORD = "natives/STM/Art/Model/Item/it03/00/0009/it0300_0009_0"
@pytest.fixture
def preview_manager(tmp_path):
    """A game with the synthetic weapon of test_equip_look (red) and a mod that makes its texture blue."""
    pytest.importorskip("PIL")
    if not matfix.available():
        pytest.skip("게임 pak을 읽을 zstd 모듈이 없습니다")
    files = {LK.mesh_file(): LK.weapon_mesh(), LK.mdf2_file(): LK.mdf2(), LK.tex_file(): LK.solid(LK.RED),
             LK.mesh_file(LK.SHIELD): LK.weapon_mesh(shape=LK.square()), LK.mdf2_file(LK.SHIELD): LK.mdf2(),
             LK.mesh_file(LONG_SWORD): LK.weapon_mesh(shape=MF.grid(6)), LK.mdf2_file(LONG_SWORD): LK.mdf2()}
    game = tmp_path / "MonsterHunterWilds"
    write(game / "MonsterHunterWilds.exe", "exe")
    M.write_game_pak(game / "re_chunk_000.pak", files)
    m = ModManager(tmp_path / "data")
    m.settings.check_updates = m.settings.check_refw_updates = m.settings.check_game_running = False
    m.set_game_dir(game)
    with m.prepare_install(make_tree(tmp_path / "src" / "Blue Staff", {LK.tex_file(): LK.solid(LK.BLUE)})) as s:
        mod = m.install_package(s, s.variants)
    return m, mod


def picture_colour(path):
    from PIL import Image

    with Image.open(path) as img:
        return LK.colour(img)


def test_previews_are_drawn_and_kept(preview_manager, monkeypatch):
    m, mod = preview_manager
    stale = m.data_dir / "cache" / "equip-previews" / "0123456789abcdef-0"
    stale.mkdir(parents=True)
    job = m.equipment_job(mod.id)
    p = m.equipment_preview(job, LK.MAIN, 64, compare=True)
    assert p.status == "ok" and p.text == "" and p.changed and p.notes == []
    assert p.after.parent.name == f"{m.game_version()[:16]}-{m.preview_formats()}" and not stale.exists()
    assert m.preview_formats() == f"{look.PREVIEW_FORMAT}-{icons.PICTURE_FORMAT}-{equip_scan.SCAN_FORMAT}"
    assert LK.leans(picture_colour(p.after), 2) and LK.leans(picture_colour(p.before), 0)
    [swatch] = p.swatches
    assert swatch.name == "it0900_0021_0_ALBD.tex" and not swatch.new
    assert LK.leans(picture_colour(swatch.before), 0) and LK.leans(picture_colour(swatch.after), 2)
    vanilla = m.equipment_preview(None, LK.SUB, 64, compare=True)  # the game's own look: no "before"
    assert vanilla.status == "ok" and not vanilla.changed and vanilla.before is None and vanilla.after.is_file()

    # kept: neither this manager nor a new one draws them again
    monkeypatch.setattr(look, "preview", lambda *a, **k: pytest.fail("drawn again"))
    assert m.equipment_preview(job, LK.MAIN, 64, compare=True) == p
    again = ModManager(m.data_dir)
    assert again.equipment_preview(again.equipment_job(mod.id), LK.MAIN, 64, compare=True) == p
    assert again.equipment_preview(None, LK.SUB, 64) == vanilla


def test_previews_follow_the_options(preview_manager, tmp_path):
    m, _mod = preview_manager
    archive = make_zip(tmp_path / "Colors.zip", {f"Colors/Blue/{LK.tex_file()}": LK.solid(LK.BLUE),
                                                 f"Colors/Green/{LK.tex_file()}": LK.solid(LK.GREEN)})
    with m.prepare_install(archive) as s:
        mod = m.install_package(s, s.variants)
    blue, green = (o["id"] for o in mod.options)
    m.set_options(mod.id, [green])
    first = m.equipment_preview(m.equipment_job(mod.id), LK.MAIN, 48)
    assert LK.leans(picture_colour(first.after), 1)
    m.set_options(mod.id, [blue])
    second = m.equipment_preview(m.equipment_job(mod.id), LK.MAIN, 48)
    assert second.after != first.after and LK.leans(picture_colour(second.after), 2)
    m.set_options(mod.id, [green])
    assert m.equipment_preview(m.equipment_job(mod.id), LK.MAIN, 48) == first


def test_previews_without_a_game(tmp_path):
    pytest.importorskip("PIL")
    m = ModManager(tmp_path / "data")
    with m.prepare_install(make_tree(tmp_path / "src" / "Staff", {
            LK.mesh_file(): LK.weapon_mesh(), LK.mdf2_file(): LK.mdf2(), LK.tex_file(): LK.solid(LK.BLUE)})) as s:
        mod = m.install_package(s, s.variants)
    p = m.equipment_preview(m.equipment_job(mod.id), LK.MAIN, 48, compare=True)
    assert p.status == "ok" and p.before is None and LK.leans(picture_colour(p.after), 2)
    assert p.after.parent.name == f"nogame-{m.preview_formats()}"
    assert p.notes == ["게임 폴더가 없어 원래 모습은 그릴 수 없습니다"]
    assert m.equipment_preview(None, LK.MAIN, 48).status == "no_mesh"


def test_previews_never_raise(preview_manager, tmp_path, monkeypatch, caplog):
    m, mod = preview_manager
    with m.prepare_install(make_tree(tmp_path / "src" / "Broken", {LK.mesh_file(): b"MESH" + bytes(300)})) as s:
        broken = m.install_package(s, s.variants)
    job, bad = m.equipment_job(mod.id), m.equipment_job(broken.id)
    calls = [(job, LK.MAIN, 48), (job, LK.SUB, 48), (None, LK.MAIN, 48), (bad, LK.MAIN, 48), (job, LK.MAIN, 48, True),
             (bad, Target("weapon", "09/000021"), 48), (job, Target("seikret", "equip/012", "body"), 48)]
    results, errors = [None] * len(calls), []

    def run(i):
        try:
            results[i] = m.equipment_preview(*calls[i])
        except BaseException as exc:  # noqa: BLE001 - the test reports it
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors and all(isinstance(r, look.Preview) for r in results)
    assert [r.status for r in results] == ["ok", "ok", "ok", "unsupported", "ok", "ok", "no_mesh"]
    assert results[3].notes == ["it0900_0021_0.mesh: MH Wilds 메시가 아닙니다 (버전 0)"]
    assert results[5].notes == results[3].notes  # the whole weapon: the shield is drawn, the blade noted

    # a bug is logged and not tried again in this session
    tried = []

    def boom(*a, **k):
        tried.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(look, "preview", boom)
    p = m.equipment_preview(job, LK.MAIN, 40)
    assert p.status == "error" and p.notes == ["미리보기를 만들지 못했습니다: boom"] and p.after is None
    assert m.equipment_preview(job, LK.MAIN, 40) == p and tried == [1]
    assert "3D 미리보기를 만들지 못했습니다" in caplog.text


def test_a_preview_drawn_while_the_game_could_not_be_read_is_not_kept(preview_manager, monkeypatch):
    m, mod = preview_manager
    job = m.equipment_job(mod.id)

    def locked(*a, **k):
        raise PermissionError("locked")

    monkeypatch.setattr(m, "_equip_game", locked)  # e.g. the paks are being rewritten by a game update
    p = m.equipment_preview(job, LK.MAIN, 48)
    assert p.status == "no_mesh" and p.notes[0] == "게임 파일 목록을 읽지 못했습니다: locked"
    monkeypatch.undo()
    assert m.equipment_preview(job, LK.MAIN, 48).status == "ok"


def test_window_draws_previews_of_each_mods_parts(preview_manager, tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("화면(display)이 없어 GUI 테스트를 건너뜁니다")
    from mhwmm import equipview, gui

    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: None)
    m, blue = preview_manager
    m.settings.check_materials = False
    mods = {"blue": blue}
    for name, files in (("Broken", {LK.mesh_file(): b"MESH" + bytes(300)}),
                        ("Sword", {LK.mesh_file(LONG_SWORD): LK.weapon_mesh(shape=MF.grid(3))})):
        with m.prepare_install(make_tree(tmp_path / "src" / name, files)) as s:
            mods[name] = m.install_package(s, s.variants)
    # the worker waits until the test lets it go, and says what it was asked to draw in which order
    calls, swatches, gate, real = [], [], threading.Event(), m.equipment_preview

    def slow(job, target, size=256, compare=False, extras=True):
        calls.append((job.mod_id, target, compare))
        swatches.append(extras)
        gate.wait(30)
        return real(job, target, size, compare, extras)

    monkeypatch.setattr(m, "equipment_preview", slow)
    staff, sword = Target("weapon", "09/000021"), Target("weapon", "03/000009")
    try:
        app = gui.App(root, m, queue.Queue())

        def pump(until, timeout=30.0):
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
        assert window.selected == sword and list(window.look_cells) == [(mods["Sword"].id, "main")]
        pump(lambda: len(calls) == 1)  # the sword's picture is being drawn
        window.select(staff)
        assert list(window.look_cells) == [(blue.id, "main"), (mods["Broken"].id, "main")]
        window.select(sword)  # the staff's pictures are no longer wanted: they are not drawn
        window.select(staff)
        shown = []
        monkeypatch.setattr(equipview.LookDialog, "show", lambda self: shown.append(self))
        cell = window.look_cells[(blue.id, "main")]
        assert cell.cget("text") == "…"
        cell.event_generate("<Button-1>")
        root.update()
        [dialog] = shown
        assert dialog.master is window and str(dialog.after_label.cget("text")) == "그리는 중..."
        gate.set()
        pump(lambda: dialog.preview is not None and all(c.look is not None for c in window.look_cells.values()))
        # the dialog's picture first, then the strip's in order; the dropped requests never ran
        assert calls == [(mods["Sword"].id, sword.with_part("main"), False), (blue.id, staff.with_part("main"), True),
                         (blue.id, staff.with_part("main"), False), (mods["Broken"].id, staff.with_part("main"), False)]
        assert swatches == [False, True, False, False]  # the small pictures show no swatches or chips
        assert cell.look.status == "ok" and str(cell.cget("image")) and cell.caption.cget("text") == "본체"
        broken = window.look_cells[(mods["Broken"].id, "main")]
        assert broken.look.status == "unsupported" and broken.cget("text") == "지원 안 함" and not str(broken.cget("image"))
        assert "이 모델 형식은 미리보기를 지원하지 않습니다" in window._look_tip(broken)

        # the dialog: both pictures in one framing, the label, the replaced texture
        p = dialog.preview
        assert p.status == "ok" and p.before is not None and p.after is not None
        assert str(dialog.before_label.cget("image")) and str(dialog.after_label.cget("image"))
        texts = [str(w.cget("text")) for w in window._descendants(dialog) if isinstance(w, gui.ttk.Label)]
        assert equipview.LOOK_NOTE in texts and "it0900_0021_0_ALBD" in texts and "원래 모습" in texts
        dialog.cancel()
        assert not dialog.winfo_exists()

        # drawn pictures are kept: showing the item again asks nobody
        calls.clear()
        window.select(sword)
        window.select(staff)
        assert window.look_cells[(blue.id, "main")].look is not None and calls == []
        window.close()
    finally:
        gate.set()
        gc.collect()
        root.destroy()


def test_cli_saves_a_preview(preview_manager, tmp_path, capsys):
    from PIL import Image

    m, _mod = preview_manager
    out = tmp_path / "out" / "staff.png"
    out.parent.mkdir()
    base = ["--data-dir", str(m.data_dir), "equip-preview"]
    assert cli.main(base + ["Blue Staff", "weapon/09/000021/main", str(out), "--before", "--size", "64"]) == 0
    text = capsys.readouterr().out
    assert "Blue Staff · weapon/09/000021/main: 정상 (삼각형 32개)" in text and f"저장했습니다: {out}" in text
    assert "  텍스처: it0900_0021_0_ALBD.tex\n" in text  # the game has it: not "(새 텍스처)"
    with Image.open(out) as img:
        assert img.size == (64, 64)
    assert LK.leans(picture_colour(out), 2) and LK.leans(picture_colour(out.with_name("staff-before.png")), 0)
    # the game's own look ("-"), a Korean part name, and a part without a model
    assert cli.main(base + ["-", "weapon/09/000021/보조", str(tmp_path / "shield.png"), "--size", "48"]) == 0
    assert "게임 · weapon/09/000021/sub: 정상" in capsys.readouterr().out
    assert cli.main(base + ["1", "seikret/equip/012/body", str(tmp_path / "none.png")]) == 1
    assert "그릴 수 있는 모델이 없습니다" in capsys.readouterr().out and not (tmp_path / "none.png").exists()
    for wrong in (["Blue Staff", "nothing/12", "x.png"], ["Blue Staff"], ["Nobody", "weapon/09/000021/main", "x.png"]):
        with pytest.raises(SystemExit):
            cli.main(base + wrong)


def test_cli_target_names():
    assert cli.parse_target("weapon/09/000021/main") == Target("weapon", "09/000021", "main")
    assert cli.parse_target("armor/f/004/001/머리") == Target("armor", "f/004/001", "helm")
    assert cli.parse_target("Armor\\f\\004\\001\\slinger") == Target("armor", "f/004/001", "slinger")
    assert cli.parse_target("kinsect/0005") == Target("kinsect", "0005")
    assert cli.parse_target("seikret/model/901/0010") == Target("seikret", "model/901/0010")
    assert cli.parse_target("weapon") is None and cli.parse_target("sword/1/main") is None


def test_cli_self_test(preview_manager, capsys):
    m, _mod = preview_manager
    assert cli.main(["--data-dir", str(m.data_dir), "equip-preview", "--self-test", "2"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("preview self-test: ") and "parts: 2 (weapon 2)" in out
    assert "magic MESH, version 240704828" in out and out.count("preview: ok") == 2


def test_a_look_swap_draws_the_other_equipments_mesh(preview_manager, tmp_path):
    m, _mod = preview_manager
    prefab = "natives/STM/GameDesign/Equip/_Prefab/Weapon/wp09/00/0021/it0900_0021_0.pfb.18"
    with m.prepare_install(make_tree(tmp_path / "src" / "Swap", {
            prefab: pfb_file("Art/Model/Item/it03/00/0009/it0300_0009_0.mesh")})) as s:
        mod = m.install_package(s, s.variants)
    job = m.equipment_job(mod.id)
    assert m.equipment_scans([job])[mod.id].redirects == [(LK.MAIN, Target("weapon", "03/000009", "main"))]
    p = m.equipment_preview(job, LK.MAIN, 48, compare=True)
    assert p.status == "ok" and p.triangles == 72  # the long sword's 6 x 6 grid, not the staff's 4 x 4
    assert p.changed and p.notes == []
    assert p.before.is_file() and p.after.is_file()


def test_a_preview_of_an_old_job_leaves_the_current_scan_alone(preview_manager, tmp_path, monkeypatch):
    m, _mod = preview_manager
    archive = make_zip(tmp_path / "Colors.zip", {f"Colors/Blue/{LK.tex_file()}": LK.solid(LK.BLUE),
                                                 f"Colors/Green/{LK.tex_file()}": LK.solid(LK.GREEN),
                                                 f"Colors/Green/{LK.mesh_file()}": LK.weapon_mesh()})
    with m.prepare_install(archive) as s:
        mod = m.install_package(s, s.variants)
    blue, green = (o["id"] for o in mod.options)
    m.set_options(mod.id, [blue])
    old = m.equipment_job(mod.id)
    m.equipment_scans([old])
    m.set_options(mod.id, [green])  # the main window changes the option and scans the new files at once ...
    new = m.equipment_job(mod.id)
    m.equipment_scans([new])
    summary = m.equipment_summary(mod.id)
    assert summary
    scans, real = [], equip_scan.scan_files
    monkeypatch.setattr(equip_scan, "scan_files", lambda *a, **k: (scans.append(1), real(*a, **k))[1])
    p = m.equipment_preview(old, LK.MAIN, 32)  # ... while the equipment window still draws with the old job
    assert p.status == "ok" and LK.leans(picture_colour(p.after), 2) and scans == [1]
    assert m._load_equip_scans()[mod.id][0] == new.stamp and m.equipment_summary(mod.id) == summary
    assert m.equipment_preview(new, LK.MAIN, 32).status == "ok" and scans == [1]  # the current scan is used


def test_previews_follow_the_picture_rules_and_repaired_files(preview_manager, monkeypatch):
    m, mod = preview_manager
    job = m.equipment_job(mod.id)
    first = m.equipment_preview(job, LK.MAIN, 40)
    monkeypatch.setattr(icons, "PICTURE_FORMAT", icons.PICTURE_FORMAT + 1)  # e.g. a texture decoding fix
    again = m.equipment_preview(job, LK.MAIN, 40)
    assert again.after.parent != first.after.parent and not first.after.parent.exists()
    # a material repaired again keeps the repaired file's name: its content counts
    repaired = m.mod_dir(mod.id) / "matfix" / "001_it0900_0021_0.mdf2.45"
    repaired.parent.mkdir()
    repaired.write_bytes(b"one")
    fixed = type(job)(job.mod_id, job.name, job.enabled, job.order, job.stamp, dict(job.files, **{LK.mdf2_file(): repaired}))
    key = m._preview_key(fixed, LK.MAIN, 40, False)
    assert key != m._preview_key(job, LK.MAIN, 40, False) and key == m._preview_key(fixed, LK.MAIN, 40, False)
    repaired.write_bytes(b"other")
    assert m._preview_key(fixed, LK.MAIN, 40, False) != key
    assert m._preview_key(job, LK.MAIN, 40, False, extras=False) != m._preview_key(job, LK.MAIN, 40, False)


def test_kept_previews_never_wait_and_a_rebuild_forgets_failures(preview_manager, monkeypatch):
    m, mod = preview_manager
    job = m.equipment_job(mod.id)
    kept = m.equipment_preview(job, LK.MAIN, 40)
    held, release, answers = threading.Event(), threading.Event(), []

    def hold():  # e.g. '게임 데이터 다시 읽기' building the catalog
        with m._equip_lock:
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        held.wait(10)
        asker = threading.Thread(target=lambda: answers.append(m.equipment_preview(job, LK.MAIN, 40)))
        asker.start()
        asker.join(5)
        assert answers == [kept]
    finally:
        release.set()
        holder.join(10)

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(look, "preview", boom)
    assert m.equipment_preview(job, LK.MAIN, 44).status == "error"
    monkeypatch.undo()
    assert m.equipment_preview(job, LK.MAIN, 44).status == "error"  # remembered for the session ...
    m.equipment_catalog(rebuild=True)
    assert m.equipment_preview(job, LK.MAIN, 44).status == "ok"  # ... until the game data is read again


def _png(path, rgb=(200, 40, 40), side=64):
    from PIL import Image

    Image.new("RGBA", (side, side), rgb + (255,)).save(path)
    return path


def test_window_armor_strip_warnings_colour_chips_and_dialog_fit(preview_manager, tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("화면(display)이 없어 GUI 테스트를 건너뜁니다")
    from mhwmm import equipview, gui

    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: None)
    m, _blue = preview_manager
    m.settings.check_materials = False
    armor = Target("armor", "f/004/001")
    parts = ("helm", "body", "arm", "waist", "leg")
    files = {}
    for part in parts:
        [mdf] = look.game_files().files(armor.with_part(part), "mdf2")
        files["natives/STM/" + mdf[len("natives/stm/"):]] = LK.mdf2()
    with m.prepare_install(make_tree(tmp_path / "src" / "Armor", files)) as s:
        mod = m.install_package(s, s.variants)
    pic = _png(tmp_path / "pic.png")
    chips = [look.Chip("body", name, (1.0, 0.1, 0.1, 1.0), (1.0, 1.0, 1.0, 1.0) if i % 2 else None)
             for i, name in enumerate(look.COLOR_PARAMS * 2)]
    full = look.Preview("ok", pic, pic, [f"참고 {i}: 아주 긴 설명이 붙은 참고 사항입니다" for i in range(8)],
                        [look.Swatch(f"t{i}_ALBD.tex", None if i < 2 else pic, pic, i == 0) for i in range(6)], chips, 10)

    def canned(job, target, size=256, compare=False, extras=True):
        if compare:
            return full
        if target.part == "helm":
            return look.Preview("missing", pic, notes=["모드 파일 누락: x_ALBD.tex (모드에도 게임에도 없습니다)"])
        return look.Preview("ok", pic)

    monkeypatch.setattr(m, "equipment_preview", canned)
    # a laptop screen: the dialog must fit 768 px, 닫기 included
    monkeypatch.setattr(equipview.LookDialog, "winfo_screenheight", lambda self: 768, raising=False)
    monkeypatch.setattr(equipview.LookDialog, "winfo_screenwidth", lambda self: 1366, raising=False)
    monkeypatch.setattr(equipview.LookDialog, "wm_maxsize", lambda self, *a: (1366, 768), raising=False)
    try:
        app = gui.App(root, m, queue.Queue())

        def pump(until, timeout=30.0):
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
        window.select(armor)
        assert list(window.look_cells) == [(mod.id, part) for part in parts]
        pump(lambda: all(c.look is not None for c in window.look_cells.values()))
        captions = {part: window.look_cells[(mod.id, part)].caption for part in parts}
        danger = window.theme.p["danger"]
        assert str(captions["helm"].cget("text")).endswith("⚠") and str(captions["helm"].cget("foreground")) == danger
        assert not any(str(captions[p].cget("text")).endswith("⚠") for p in parts[1:])
        assert "모드 파일 누락" in window._look_tip(window.look_cells[(mod.id, "helm")])

        shown = []
        monkeypatch.setattr(equipview.LookDialog, "show", lambda self: shown.append(self))
        window.look_cells[(mod.id, "body")].event_generate("<Button-1>")
        pump(lambda: shown and shown[0].preview is not None)
        [dialog] = shown
        texts = [str(w.cget("text")) for w in window._descendants(dialog) if isinstance(w, (gui.ttk.Label, gui.ttk.Button))]
        assert any(look.COLOR_LABELS["ColorParam"] in t for t in texts) and "닫기" in texts
        assert texts.count("새 텍스처") == 1 and texts.count("?") >= 1  # only the first swatch is new
        dialog.update_idletasks()
        assert dialog.winfo_reqheight() <= dialog._room() <= 768 - window.theme.px(80)
        assert dialog.shown < dialog.size  # smaller pictures ...
        assert int(dialog.extra_canvas.cget("height")) < dialog.extra.winfo_reqheight()  # ... and the rest scrolls
        assert dialog._on_screen(500) == 768 - window.theme.px(80) - dialog.winfo_reqheight()
        dialog.cancel()
        window.close()
    finally:
        gc.collect()
        root.destroy()
