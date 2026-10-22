"""tools/resize_bench.py off Windows: its variants (gui.RESIZE_CLASS_REDRAW with the resize modes), the visual checks'
arithmetic and pictures, and the visual checks of a drag and of a settled window over a fake Windows API (the
measurements themselves need Windows: the Resize bench workflow)."""

import gc
import importlib.util
import queue
import sys
import threading
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("resize_bench_under_test", ROOT / "tools" / "resize_bench.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # (its NamedTuple looks its module up)
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _child(bench, spec, tmp_path, **args):
    values = dict(child=spec, no_visual=False, frames_dir=str(tmp_path / "frames"), interval_ms=2.0,
                  drag_timeout=20.0, settle=0.3, no_drag=False, repeat=1, moves=6)
    values.update(args)
    return bench.Child(Namespace(**values))


def _picture(bench, width, height, rgb=(40, 90, 160), x=0):
    return bench.Picture(width, height, bytes((rgb[2], rgb[1], rgb[0], x)) * (width * height))


def _set(bench, pic, x, y, rgb):
    data = bytearray(pic.data)
    i = (y * pic.width + x) * 4
    data[i:i + 3] = bytes((rgb[2], rgb[1], rgb[0]))
    return bench.Picture(pic.width, pic.height, bytes(data))


def _pillow(bench):
    return [True, False] if bench._pil() is not None else [False]


def test_variants_name_the_class_redraw_and_the_mode(bench, tmp_path):
    """'all' compares the modes and Tk's window classes (RESIZE_CLASS_REDRAW) changed one at a time and both; a
    variant sets gui.RESIZE_CLASS_REDRAW before the app is built, or, with a gui without it, leaves the change to
    the benchmark; without a class part the app's default stays."""
    assert bench.ALL == ["bare", "off", "freeze", "snapshot", "snapshot+unmap", "top+freeze", "child+freeze",
                         "both+freeze", "both+off", "both+snapshot", "scale150+both+freeze"]
    for spec in bench.ALL + bench.QUICK + ["noclassredraw+freeze", "keep+snapshot"]:
        bench.parse_spec(spec)
    assert "both+freeze" in bench.QUICK

    class Gui:
        RESIZE_CLASS_REDRAW = "keep"

    for spec, value in (("both+freeze", "both"), ("top+child+off", "both"), ("child+snapshot", "child"),
                        ("noclassredraw", "both"), ("keep+freeze", "keep"), ("freeze", "keep")):
        gui = type("Gui", (Gui,), {})
        child = _child(bench, spec, tmp_path)
        child.set_class_redraw(gui)
        env = child.result["env"]
        assert (gui.RESIZE_CLASS_REDRAW, env["class_redraw"], env["class_redraw_by"]) == (value, value, "gui")

    class Old:  # (a gui before the switch)
        pass

    child = _child(bench, "both+freeze", tmp_path)
    child.set_class_redraw(Old)
    assert child.result["env"]["class_redraw"] == "both" and child.result["env"]["class_redraw_by"] == "bench"
    assert not hasattr(Old, "RESIZE_CLASS_REDRAW")
    child = _child(bench, "freeze", tmp_path)
    child.set_class_redraw(Old)
    assert child.result["env"]["class_redraw"] == "keep" and child.result["env"]["class_redraw_by"] is None

    result = {"env": {"class_redraw": "both", "class_styles": {"top": {"before": "0x3", "after": "0x0"}}}}
    assert bench.redraw_of(result) == "both!"  # (the child class was not changed)
    result["env"]["class_styles"]["child"] = {"before": "0xb", "after": "0x8"}
    assert bench.redraw_of(result) == "both"
    assert bench.redraw_of({"env": {"class_redraw": "keep"}}) == "keep"


@pytest.mark.parametrize("pillow", [True, False])
def test_differing_pixels(bench, pillow):
    """differing() counts the pixels that differ in a channel (the unused byte of a grab does not count; by more than
    ``tol`` when one is given, which no check does), inside a box, against a picture or a colour; the same with
    Pillow and without it."""
    if pillow and bench._pil() is None:
        pytest.skip("Pillow is not installed")
    a = _picture(bench, 6, 4)
    b = bench.Picture(6, 4, bytes(v if i % 4 != 3 else 0xFF for i, v in enumerate(a.data)))  # (unused bytes)
    assert bench.differing(a, b, use_pil=pillow)["differ"] == 0
    b = _set(bench, b, 1, 1, (41, 90, 160))  # (one level off: it differs)
    b = _set(bench, b, 4, 2, (40, 90, 220))
    found = bench.differing(a, b, use_pil=pillow, want_mask=True)
    assert (found["pixels"], found["differ"], found["box"]) == (24, 2, [1, 1, 5, 3])
    assert found["share"] == round(2 / 24, 6)
    assert ("mask" in found) == pillow
    tolerant = bench.differing(a, b, tol=8, use_pil=pillow)
    assert (tolerant["differ"], tolerant["box"]) == (1, [4, 2, 5, 3])
    assert bench.differing(a, b, (0, 0, 4, 4), use_pil=pillow)["differ"] == 1
    assert bench.differing(a, b, (2, 0, 4, 4), use_pil=pillow)["differ"] == 0
    assert bench.differing(a, b, (0, 0, 0, 4), use_pil=pillow) == {"pixels": 0, "differ": 0, "share": 0.0,
                                                                    "box": None}
    assert bench.differing(a, (40, 90, 160), use_pil=pillow)["differ"] == 0
    assert bench.differing(b, (40, 90, 160), (3, 0, 6, 4), use_pil=pillow)["box"] == [4, 2, 5, 3]
    bigger = _picture(bench, 9, 7)
    assert bench.differing(a, bigger, use_pil=pillow)["pixels"] == 24  # (the part both cover)
    spread = bench.off_colour(_set(bench, bigger, 8, 6, (0, 0, 0)), bench.grown_boxes((6, 4), (9, 7)),
                              (40, 90, 160), use_pil=pillow)
    assert (spread["pixels"], spread["differ"], spread["box"]) == (9 * 7 - 6 * 4, 1, [8, 6, 9, 7])
    assert bench.off_colour(a, [], (0, 0, 0))["share"] is None


@pytest.mark.parametrize("pillow", [True, False])
def test_one_differing_pixel_fails_a_check(bench, pillow):
    """The settled, direct and window checks compare exactly: a stale block in a neighbouring colour of the app's
    palette (the dark theme's surface and row_alt: 4 levels apart at most) and a 1 px high-contrast segment of 100
    px (below SHARE_TOL of a 1240x800 window) are both found, and neither is ok."""
    if pillow and bench._pil() is None:
        pytest.skip("Pillow is not installed")
    surface, row_alt = (0x1a, 0x1d, 0x23), (0x1d, 0x20, 0x27)
    width, height = 1240, 800
    clean = _picture(bench, width, height, surface)

    def drawn(x0, y0, x1, y1, rgb):
        data = bytearray(clean.data)
        for y in range(y0, y1):
            data[(y * width + x0) * 4:(y * width + x1) * 4] = bytes((rgb[2], rgb[1], rgb[0], 0)) * (x1 - x0)
        return bench.Picture(width, height, bytes(data))

    block = bench.differing(drawn(400, 300, 700, 500, row_alt), clean, use_pil=pillow)
    assert (block["differ"], block["box"]) == (300 * 200, [400, 300, 700, 500])
    line = bench.differing(drawn(500, 600, 600, 601, (255, 255, 255)), clean, use_pil=pillow)
    assert (line["differ"], line["box"]) == (100, [500, 600, 600, 601]) and 0 < line["share"] < bench.SHARE_TOL
    for found in (block, line):
        assert bench.visual_verdict({"checks": [dict(found, what="drag 1")]}) == "stale"
        assert bench.visual_verdict({"checks": [{"what": "drag 1", "differ": 0, "share": 0.0}],
                                     "direct": dict(found, what="direct")}) == "layout"
        assert bench.visual_verdict({"checks": [dict(found, what="help steps", kind="dialog")]}) == "dialog"
        assert bench.visual_verdict({"checks": [dict(found, what="equipment direct", kind="equip",
                                                     direct=True)]}) == "equip"
    assert bench.visual_verdict({"checks": [dict(bench.differing(clean, clean), what="drag 1")]}) == "ok"


def test_kept_part_and_grown_area_of_a_drag(bench):
    """The part that stayed inside the window since the drag began (the smallest width and height so far) and
    the area the window gained (a strip at the right and one below, not overlapping)."""
    assert bench.kept_box((300, 200), [(320, 200), (290, 210)]) == (0, 0, 290, 200)
    assert bench.kept_box((300, 200), []) == (0, 0, 300, 200)
    assert bench.grown_boxes((300, 200), (320, 230)) == [(300, 0, 320, 230), (0, 200, 300, 230)]
    assert bench.grown_boxes((300, 200), (290, 230)) == [(0, 200, 290, 230)]
    assert bench.grown_boxes((300, 200), (290, 190)) == []


def test_mid_drag_report_and_its_pictures(bench, tmp_path):
    """A drag's frames against the grab before it: the kept part and the gained area's background; the frames
    are saved, and -diff pictures where something differs (with Pillow)."""
    content, background = (40, 90, 160), (217, 217, 217)
    before = _picture(bench, 8, 6, content)

    def frame(width, height, kept=(8, 6)):
        pic = _picture(bench, width, height, background)
        for y in range(min(height, kept[1])):
            for x in range(min(width, kept[0])):
                pic = _set(bench, pic, x, y, content)
        return pic

    good = frame(10, 7)
    narrower = frame(7, 7)  # (the content cut off at the right)
    wrong = _set(bench, frame(10, 7), 2, 2, (255, 255, 255))
    stray = _set(bench, frame(10, 7), 9, 6, (0, 0, 0))
    # (as a snapshot whose picture lost the column the window was narrower by: that column left the window and came
    # back, and is neither the kept part nor gained)
    regrown = frame(10, 7, kept=(7, 6))
    frames = [(1, 1.0, good), (2, 2.0, narrower), (3, 3.0, wrong), (4, 4.0, stray), (5, 5.0, regrown)]
    sizes = [(0.5, 10, 7), (1.5, 7, 7), (2.5, 10, 7)]
    report = bench.mid_drag_report(before, frames, sizes, background, True, tmp_path / "out")
    rows = report["rows"]
    assert [r["kept"] for r in rows] == [[0, 0, 8, 6], [0, 0, 7, 6], [0, 0, 7, 6], [0, 0, 7, 6], [0, 0, 7, 6]]
    assert [r["differ"] for r in rows] == [0, 0, 1, 0, 0]
    assert [r["grown_differ"] for r in rows] == [0, 0, 0, 1, 0]
    assert rows[1]["grown_pixels"] == 7 and rows[1]["grown_share"] == 0
    assert report["frames"] == 5 and report["share"] == round(1 / 42, 6) and report["expect_same"]
    assert report["background"] == "#d9d9d9"
    saved = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert "drag-0-before.png" in saved and "drag-3-step03.png" in saved
    if bench._pil() is not None:
        assert {"drag-3-step03-diff.png", "drag-4-step04-diff.png"} <= set(saved)
        assert "drag-1-step01-diff.png" not in saved and "drag-5-step05-diff.png" not in saved
        from PIL import Image

        with Image.open(tmp_path / "out" / "drag-3-step03-diff.png") as marked:
            assert marked.convert("RGB").getpixel((2, 2)) == (255, 0, 255)
        with Image.open(tmp_path / "out" / "drag-3-step03.png") as plain:
            assert plain.convert("RGB").getpixel((2, 2)) == (255, 255, 255)
            assert plain.convert("RGB").getpixel((0, 0)) == content
    assert bench.mid_drag_report(before, [], [], background, False)["share"] is None
    assert report["reference"] == "before" and report["judged"] == 5
    assert all(r["against"] == "before" and r["judged"] for r in rows)


def test_mid_drag_report_of_a_drag_whose_first_step_was_laid_out(bench, tmp_path):
    """A drag whose first step was not frozen (laid out at once for its size): its kept content is the one of the
    first frame, so frames 2 and later are compared with frame 1 (the part kept since then; the area beyond frame
    1's size against the background) and frame 1 is not judged; with frame 1 alone nothing is."""
    content, background = (40, 90, 160), (217, 217, 217)
    before = _picture(bench, 8, 6, content)  # (the content before the drag: 8x6)

    def frame(width, height, kept):
        pic = _picture(bench, width, height, background)
        for y in range(min(height, kept[1])):
            for x in range(min(width, kept[0])):
                pic = _set(bench, pic, x, y, content)
        return pic

    first = frame(10, 7, (10, 7))  # (laid out for 10x7 at the first step, then kept)
    frames = [(1, 1.0, first), (2, 2.0, frame(12, 7, (10, 7))),
              (3, 3.0, _set(bench, frame(11, 8, (10, 7)), 2, 2, (255, 255, 255))),
              (4, 4.0, _set(bench, frame(12, 8, (10, 7)), 11, 7, (0, 0, 0)))]
    sizes = [(0.5, 10, 7), (1.5, 12, 7), (2.5, 11, 8), (3.5, 12, 8)]
    report = bench.mid_drag_report(before, frames, sizes, background, True, tmp_path / "out", first_frozen=False)
    rows = report["rows"]
    assert rows[0] == {"step": 1, "size": [10, 7], "against": None, "judged": False}
    assert [r["against"] for r in rows[1:]] == ["frame 1"] * 3 and all(r["judged"] for r in rows[1:])
    assert [r["kept"] for r in rows[1:]] == [[0, 0, 10, 7]] * 3
    assert [r["differ"] for r in rows[1:]] == [0, 1, 0] and [r["grown_differ"] for r in rows[1:]] == [0, 0, 1]
    assert (report["judged"], report["reference"], report["differ"], report["grown_differ"]) == (3, "frame 1", 1, 1)
    assert (tmp_path / "out" / "drag-1-step01.png").exists()  # (saved, though not judged)
    visual = {"checks": [], "mid": dict(report, first_frozen=False)}
    assert bench.visual_verdict(visual) == "mid"
    # against the frame before the drag, the content laid out for the first step is where the background should be
    assert bench.mid_drag_report(before, frames, sizes, background, True)["grown"] > 0.5
    alone = bench.mid_drag_report(before, frames[:1], sizes, background, True, first_frozen=False)
    assert (alone["frames"], alone["judged"], alone["share"]) == (1, 0, None)
    assert bench.visual_verdict({"checks": [{"what": "drag 1", "differ": 0}], "mid": alone}) == "mid?"
    laid_out = bench.mid_drag_report(before, frames, sizes, background, False, first_frozen=False)
    assert laid_out["reference"] == "before" and laid_out["judged"] == 0  # (off: not judged at all)


def test_pictures_are_saved_without_pillow(bench, tmp_path, monkeypatch):
    """Without Pillow the pictures are still PNG files (a plain encoder); no -diff pictures then."""
    monkeypatch.setattr(bench, "_pil", lambda: None)
    pic = _set(bench, _picture(bench, 5, 3, (10, 20, 30)), 4, 2, (200, 100, 50))
    path = bench.save_png(tmp_path / "a" / "pic.png", pic)
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert bench.save_marked(tmp_path / "a" / "diff.png", pic, [((0, 0), None, (255, 0, 255))]) is None
    monkeypatch.undo()
    if bench._pil() is not None:
        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("RGB")
            assert image.size == (5, 3)
            assert image.getpixel((0, 0)) == (10, 20, 30) and image.getpixel((4, 2)) == (200, 100, 50)


def test_visual_verdicts_and_their_table(bench, capsys):
    """The verdict: stale when a settled check's repaint changed a pixel, mid / minor when a kept content's drag
    frames differ (in more / fewer than SHARE_TOL of their pixels; only where the content is kept), mid? when no
    frame could be judged, layout for the direct check, dialog / equip for the other windows' checks; and the table
    shows them (differing pixels), with the frames of a mode that lays the content out in parentheses."""
    zero = {"differ": 0, "share": 0.0}
    ok = {"checks": [dict(zero, what="drag 1"), dict(zero, what="zoomed"), dict(zero, what="help steps", kind="dialog"),
                     dict(zero, what="equipment direct", kind="equip", direct=True)],
          "mid": {"frames": 4, "judged": 4, "differ": 0, "share": 0.0, "grown": 0.0, "grown_differ": 0,
                  "expect_same": True, "reference": "before"},
          "direct": dict(zero)}
    assert bench.visual_verdict(ok) == "ok"
    stale = dict(ok, checks=ok["checks"] + [{"what": "theme light", "differ": 1, "share": 0.000001,
                                             "pixels": 1000000, "box": [1, 2, 3, 4]}])
    assert bench.visual_verdict(stale) == "stale"
    mid = dict(ok, mid=dict(ok["mid"], differ=9000, share=0.2, rows=[
        {"step": 19, "size": [900, 700], "against": "before", "judged": True, "kept": [0, 0, 880, 700],
         "share": 0.2, "differ": 9000, "box": [0, 0, 5, 5], "grown_share": None, "grown_differ": 0}]))
    assert bench.visual_verdict(mid) == "mid"
    minor = dict(ok, mid=dict(ok["mid"], differ=0, grown_differ=3, grown=0.00001))
    assert bench.visual_verdict(minor) == "minor"
    unjudged = dict(ok, mid=dict(ok["mid"], judged=0, first_frozen=False, differ=None, share=None, rows=[
        {"step": 1, "size": [900, 700], "against": None, "judged": False}]))
    assert bench.visual_verdict(unjudged) == "mid?"
    by_first = dict(ok, mid=dict(ok["mid"], judged=3, reference="frame 1", first_frozen=False))
    assert bench.visual_verdict(by_first) == "ok"
    older = {"checks": [dict(zero, what="drag 1")], "mid": {"frames": 4, "share": 0.0, "expect_same": True}}
    assert bench.visual_verdict(older) == "ok"  # (a result from before "judged": all frames were judged)
    assert bench.visual_verdict(dict(older, mid=dict(older["mid"], first_frozen=False))) == "mid?"
    laid_out = dict(ok, mid={"frames": 4, "judged": 0, "differ": 9000, "share": 0.4, "grown": 0.9,
                             "grown_differ": 300000, "expect_same": False})
    assert bench.visual_verdict(laid_out) == "ok"
    assert bench.visual_verdict(dict(ok, direct={"differ": 7, "share": 0.5})) == "layout"
    windows = dict(ok, checks=ok["checks"] + [{"what": "conflicts drag", "kind": "dialog", "differ": 12,
                                               "share": 0.00002, "pixels": 600000, "box": [0, 0, 4, 3]},
                                              {"what": "equipment steps", "kind": "equip", "error": "no window"}])
    assert bench.visual_verdict(windows) == "dialog,error"
    assert bench.visual_verdict({"checks": [{"what": "zoomed", "error": "bad"}]}) == "error"
    assert bench.visual_verdict({"checks": [], "mid": None, "direct": None}) == "-"
    worst = bench.worst_checks(windows["checks"] + [{"what": "visual drag", "differ": 2, "share": 0.001}])
    assert worst["drag"] == {"differ": 2, "share": 0.001, "what": "visual drag"} and worst["steps"] is None
    assert worst["zoom"]["differ"] == 0 and worst["theme"] is None
    assert worst["dialog"]["what"] == "conflicts drag" and worst["equip"]["differ"] == 0

    def result(variant, mode, visual):
        return {"variant": variant, "env": {"mode": mode, "class_redraw": "both",
                                            "class_styles": {"top": {"after": "0x0"}, "child": {"after": "0x8"}}},
                "visual": visual}

    bench.print_visual_table([result("both+freeze", "freeze", ok), result("both+snapshot", "snapshot", stale),
                              result("both+off", "off", laid_out), result("top+freeze", "freeze", mid),
                              result("child+freeze", "freeze", unjudged), result("both+frame1", "freeze", by_first),
                              result("top+snapshot", "snapshot", windows),
                              {"variant": "x", "env": {}, "visual": {"skipped": "Windows only (GDI grabs)"}}])
    out = capsys.readouterr().out
    lines = {line.split()[0]: line for line in out.splitlines() if line.startswith(("both+", "top+", "child+", "x "))}
    assert lines["both+freeze"].rstrip().endswith("| ok")
    assert lines["both+snapshot"].rstrip().endswith("stale")
    assert lines["both+snapshot"].split("|")[1].split() == ["0", "-", "0", "1"]  # (drag, steps, zoom, theme)
    assert "(9000)" in lines["both+off"] and "(300k)" in lines["both+off"] and lines["both+off"].rstrip().endswith("ok")
    assert "Windows only" in out
    assert "both+snapshot: theme light: 1 of 1000000 pixels differ (0.000%), box [1, 2, 3, 4]" in out
    assert lines["top+freeze"].rstrip().endswith("mid") and "both!" not in out
    assert "top+freeze: mid-drag step 19 at [900, 700] against before: kept part [0, 0, 880, 700] 9000 px" in out
    assert lines["child+freeze"].rstrip().endswith("mid?") and " ? " in lines["child+freeze"]
    assert "child+freeze: mid-drag step 1 at [900, 700]: first step not frozen (not judged)" in out
    assert "0*" in lines["both+frame1"]
    cells = [c.strip() for c in lines["top+snapshot"].split("|")]
    assert cells[-2].split() == ["12", "0"] and cells[-1] == "dialog,error"  # (dialog, equip; verdict)
    assert "top+snapshot: dialog conflicts drag: 12 of 600000 pixels" in out
    assert "top+snapshot: equip equipment steps: no window" in out


def test_the_variant_gets_the_frames_folder_also_when_there_is_none(bench, tmp_path, monkeypatch):
    """--frames-dir "" (no pictures) reaches the variant's process as "" (not left out, which would be the option's
    default folder), a folder as itself; the variant reads "" as no folder."""
    commands = []

    class Done:
        returncode = 0
        stdout = (bench.JSON_PREFIX + '{"variant": "freeze", "ok": true, "notes": []}\n').encode()

    monkeypatch.setattr(bench.subprocess, "run", lambda cmd, **kw: commands.append(cmd) or Done())
    args = bench.make_parser().parse_args(["--frames-dir", "", "--no-windows"])
    args.moves, args.repeat, args.settle = 6, 1, 0.3
    assert bench.run_child(args, "freeze", None, tmp_path / "run", frames=None)["ok"]
    bench.run_child(args, "freeze", None, tmp_path / "run", frames=tmp_path / "frames" / "freeze")
    (first, second) = commands
    assert first[first.index("--frames-dir") + 1] == "" and "--no-windows" in first
    assert second[second.index("--frames-dir") + 1] == str(tmp_path / "frames" / "freeze")
    child_args = bench.make_parser().parse_args(first[2:])
    assert child_args.frames_dir == "" and child_args.no_windows and child_args.child == "freeze"
    assert bench.Child(child_args).frames_dir() is None
    assert bench.Child(bench.make_parser().parse_args(second[2:])).frames_dir() == tmp_path / "frames" / "freeze"


# ------------------------------------------------------------------------------ over a fake Windows API
try:
    import tkinter as tk
except ImportError:  # (the tests above still run; the ones below are skipped by their root fixture)
    tk = None


@pytest.fixture
def root():
    if tk is None:
        pytest.skip("tkinter이 없어 GUI 테스트를 건너뜁니다")
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("화면(display)이 없어 GUI 테스트를 건너뜁니다")
    yield r
    gc.collect()  # (the test's Tk objects before the interpreter, on this thread: Python 3.10 can crash otherwise)
    try:
        r.destroy()
    except tk.TclError:
        pass


class FakeWindows:
    """The part of resize_bench.WinApi the visual checks use, over Tk windows on any platform (the main window and
    other toplevels: each has a fake outer window, its "wrapper"): the mouse drags the bottom-right corner of the
    window last hit-tested (each move a size, applied on the main thread), and a grab draws a window as a kept
    content shows during a drag: the content's colour where the window was when the button went down, the
    background beyond. ``garble``: drag grabs (1, 2, ...) with wrong pixels in the content; ``stale``: rectangles
    ((x0, y0, x1, y1), (r, g, b)) drawn into the grabs of the toplevel ``stale_on`` (default: the main window) before
    its first repaint (the repaint changes them)."""

    HTBOTTOMRIGHT = 17
    CONTENT = (40, 90, 160)

    def __init__(self, bench, root, garble=(), stale=(), stale_on=None, area=(0, 0, 420, 320)):
        self.bench, self.root = bench, root
        self.cursor_at = (0, 0)
        self.pressed = None
        self.target = root  # (the window the mouse drags)
        self.sizes: queue.Queue = queue.Queue()
        self.wrappers: dict = {}  # fake outer window -> its toplevel
        self.views: dict = {}  # toplevel's path -> {"shown": its size in the grabs, "kept": the content's size}
        self.garble, self.stale, self.stale_on = set(garble), list(stale), stale_on
        self.area = area
        self.drag_grabs = self.repaints = 0
        self.repainted: set = set()
        self.background = tuple(v >> 8 for v in root.winfo_rgb(root.cget("background")))
        self.threads: set = set()
        self.grabbed: set = set()
        self._serving = None

    def _view(self, top):
        return self.views.setdefault(str(top), {"shown": (top.winfo_width(), top.winfo_height()), "kept": None})

    # -- windows
    def wrapper(self, top):
        for hwnd, known in self.wrappers.items():
            if known is top:
                return hwnd
        hwnd = 0x51 + len(self.wrappers)
        self.wrappers[hwnd] = top
        return hwnd

    def IsWindow(self, hwnd):
        return 1

    def SetForegroundWindow(self, hwnd):
        return 1

    def rect(self, hwnd):
        r = self.wrappers[hwnd]
        x, y = r.winfo_rootx(), r.winfo_rooty()
        return x, y, x + r.winfo_width(), y + r.winfo_height()

    def work_area(self):  # (default small: a small window, quick to grab)
        return self.area

    def set_size(self, hwnd, width, height):  # (SetWindowPos: the fake's outer window is the toplevel itself)
        self.wrappers[hwnd].geometry("%dx%d" % (width, height))
        return True

    def hit_test(self, hwnd, x, y):  # (on the main thread, before the drag)
        self.target = self.wrappers[hwnd]
        self._view(self.target)["shown"] = (self.target.winfo_width(), self.target.winfo_height())
        if self._serving is None:
            self._serving = self.root.after(5, self._apply)
        return 17

    def top_window_at(self, x, y):
        return self.wrapper(self.target)

    def last_error(self):
        return 0

    # -- the mouse (the drag's thread)
    def SetCursorPos(self, x, y):
        self.cursor_at = (x, y)
        return 1

    def cursor(self):
        return self.cursor_at

    def button(self, down):
        view = self.views[str(self.target)] if down else None
        self.pressed = (self.cursor_at, view["shown"]) if down else None
        if down:
            view["kept"] = view["shown"]
        return 1

    def move_to(self, x, y):
        self.cursor_at = (x, y)
        if self.pressed:
            (x0, y0), (w0, h0) = self.pressed
            self.sizes.put((w0 + x - x0, h0 + y - y0))
        return 1

    def _apply(self):
        while not self.sizes.empty():
            self.target.geometry("%dx%d" % self.sizes.get())
            self.target.update_idletasks()
            self._view(self.target)["shown"] = (self.target.winfo_width(), self.target.winfo_height())
        self._serving = self.root.after(5, self._apply)

    def close(self):
        if self._serving is not None:
            self.root.after_cancel(self._serving)

    # -- pictures
    def _toplevels(self):
        found, todo = [self.root], list(self.root.winfo_children())
        while todo:
            widget = todo.pop()
            todo.extend(widget.winfo_children())
            if isinstance(widget, tk.Toplevel):
                found.append(widget)
        return found

    def grab(self, hwnd):
        tops = [*self.wrappers.values()] if threading.current_thread() is not threading.main_thread() else \
            self._toplevels()  # (no Tk call off the main thread: the dragged window is known)
        top = next(t for t in tops + [self.target] if t.winfo_id() == hwnd)
        self.grabbed.add(str(top))
        main = threading.current_thread() is threading.main_thread()
        self.threads.add(main)
        view = self._view(top)
        if main and not (self.pressed and top is self.target):  # (no drag: the content follows the window)
            view["shown"], view["kept"] = (top.winfo_width(), top.winfo_height()), None
        (w, h), b = view["shown"], self.bench
        kw, kh = view["kept"] or view["shown"]
        content = bytes((self.CONTENT[2], self.CONTENT[1], self.CONTENT[0], 0))
        background = bytes((self.background[2], self.background[1], self.background[0], 0))
        rows = [content * min(w, kw) + background * max(0, w - kw) if y < kh else background * w for y in range(h)]
        data = bytearray(b"".join(rows))
        if not main:
            self.drag_grabs += 1
            if self.drag_grabs in self.garble:  # (a 20 px square: more than SHARE_TOL)
                for y in range(3, 23):
                    data[(y * w + 3) * 4:(y * w + 23) * 4] = b"\xff\xff\xff\x00" * 20
        if main and (self.stale_on or self.root) is top and self.wrapper(top) not in self.repainted:
            for (x0, y0, x1, y1), (r, g, bl) in self.stale:
                for y in range(y0, y1):
                    data[(y * w + x0) * 4:(y * w + x1) * 4] = bytes((bl, g, r, 0)) * (x1 - x0)
        return b.Picture(w, h, bytes(data))

    def repaint_all(self, hwnd):
        assert hwnd in self.wrappers and threading.current_thread() is threading.main_thread()
        self.repaints += 1
        self.repainted.add(hwnd)
        return True


def _fake_run(bench, root, tmp_path, spec="freeze", **fake):
    root.geometry("300x200+40+40")
    root.update()
    child = _child(bench, spec, tmp_path)
    child.root, child.api = root, FakeWindows(bench, root, **fake)
    child.result["env"]["mode"] = bench.parse_spec(spec)[0]
    rec = bench.ConfigureLog(root)
    moves = bench.plan_moves(6)
    plan = child.place(moves)
    return child, rec, moves, plan


def test_visual_drag_grabs_its_steps_from_the_mouse_thread(bench, root, tmp_path):
    """The visual drag: the mouse thread grabs the window (no Tk call there) after some of its moves; the frames
    are compared with the window before the drag, in the part that stayed inside it (a kept content: the same;
    the gained area: the background), and saved for the artifact; wrong pixels in one frame are a mid verdict."""
    child, rec, moves, plan = _fake_run(bench, root, tmp_path)
    try:
        out = child.visual_drag(moves, plan, rec)
    finally:
        child.api.close()
    assert out.get("error") is None and out["configures"] >= 3, out
    assert child.api.threads == {True, False}  # (the frames on the mouse thread)
    mid = child.visual["mid"]
    assert mid["grab_at"] == [1, 3, 5] and mid["frames"] == 3
    assert mid["share"] == 0 and mid["grown"] == 0 and mid["expect_same"]
    start = tuple(plan["inner"])
    assert mid["rows"][0]["size"][0] > start[0] and mid["rows"][0]["grown_pixels"] > 0
    assert all(r["kept"][2] <= start[0] and r["kept"][3] <= start[1] for r in mid["rows"])
    assert mid["rows"][2]["size"][1] > start[1]  # (the last third: taller)
    folder = tmp_path / "frames"
    assert (folder / "drag-0-before.png").exists() and (folder / "drag-3-step05.png").exists()
    child.visual_summary()
    assert child.visual["verdict"] == "ok"

    child, rec, moves, plan = _fake_run(bench, root, tmp_path, garble=(2,))
    try:
        child.visual_drag(moves, plan, rec)
    finally:
        child.api.close()
    rows = child.visual["mid"]["rows"]
    assert [r["differ"] for r in rows] == [0, 400, 0] and rows[1]["box"] == [3, 3, 23, 23]
    child.visual_summary()
    assert child.visual["verdict"] == "mid"
    if bench._pil() is not None:
        assert (tmp_path / "frames" / "drag-2-step03-diff.png").exists()


def test_settled_and_direct_checks_force_a_repaint(bench, root, tmp_path):
    """A settled check grabs the window until it stops changing, has Windows repaint it whole (RedrawWindow on the
    outer window, then update()) and grabs it again: a window the same after it passes, one the repaint changed
    is stale (its pictures saved). The direct check sets the size again from the start size and compares. The
    state checks note a wm state this Tk does not have as an error and go on."""
    child, rec, moves, plan = _fake_run(bench, root, tmp_path)
    check = child.settled_check("drag 1")
    assert check["share"] == 0 and check["size"] == [root.winfo_width(), root.winfo_height()] and check["repaint"]
    assert child.api.repaints == 1 and "unsettled" not in check
    root.geometry("320x230")
    root.update()
    direct = child.direct_check(plan)
    assert direct["share"] == 0 and direct["size"] == [320, 230] and direct["via"] == list(plan["inner"])
    assert (root.winfo_width(), root.winfo_height()) == (320, 230)

    child, rec, moves, plan = _fake_run(bench, root, tmp_path, stale=[((20, 20, 30, 30), (0, 0, 0))])
    check = child.settled_check("theme light")
    assert check["differ"] == 100 and check["box"] == [20, 20, 30, 30]
    saved = {p.name for p in (tmp_path / "frames").iterdir()}
    assert {"settled-01-theme_light-before.png", "settled-01-theme_light-repainted.png"} <= saved
    states = []
    real_state = root.state

    def state(value=None):  # (Tk on X11 has no zoomed state: an error noted, the next check runs)
        if value is not None:
            states.append(value)
        if value == "zoomed":
            raise tk.TclError('bad argument "zoomed"')
        return real_state(value) if value is not None else real_state()

    root.state = state
    try:
        child.state_checks(plan)
    finally:
        del root.state
    assert states[:2] == ["zoomed", "normal"]
    assert [c["what"] for c in child.visual["checks"]] == ["theme light", "zoomed", "normal"]
    assert "zoomed" in child.visual["checks"][1]["error"] and child.visual["checks"][2]["share"] == 0
    child.visual_summary()
    assert child.visual["verdict"] == "stale,error"
    assert child.visual["worst"]["theme"]["differ"] == 100 and child.visual["worst"]["zoom"]["differ"] == 0

    near = [((40, 30, 80, 60), tuple(v + 4 for v in FakeWindows.CONTENT)), ((10, 80, 110, 81), (255, 255, 255))]
    child, rec, moves, plan = _fake_run(bench, root, tmp_path, stale=near)  # (4 levels off, and a 1 px line)
    check = child.settled_check("drag 1")
    assert check["differ"] == 40 * 30 + 100
    child.visual_summary()
    assert child.visual["verdict"] == "stale"

    child = _child(bench, "freeze", tmp_path, no_visual=True)
    child.root, child.api = root, FakeWindows(bench, root)
    assert child.settled_check("drag 1") is None and child.visual["checks"] == []


def test_another_window_is_checked_on_its_own(bench, root, tmp_path):
    """window_check: another toplevel placed with room for its drag, stepped (SetWindowPos), dragged by its corner
    and set to its size directly, each followed by a check of its own window (grabbed from its own handle, repainted
    from its own outer window), as checks of its kind; stale pixels in it give that kind's verdict, not the main
    window's."""
    child, rec, moves, plan = _fake_run(bench, root, tmp_path)
    top = tk.Toplevel(root)
    try:
        top.geometry("260x180+60+60")
        top.update()
        child.api.stale = [((10, 10, 14, 30), (41, 90, 160))]  # (one level off the content)
        child.api.stale_on = top
        child.quiet_s = lambda: 0.15
        try:
            info = child.window_check("dialog", "help", top)
        finally:
            child.api.close()
        checks = child.visual["checks"]
        assert [(c["kind"], c["what"]) for c in checks] == [("dialog", "help steps"), ("dialog", "help drag"),
                                                            ("dialog", "help direct")]
        assert [c.get("error") for c in checks] == [None] * 3, checks
        assert [c["differ"] for c in checks] == [80, 0, 0] and checks[0]["box"] == [10, 10, 14, 30]
        assert checks[2]["direct"] and checks[2]["size"] == [top.winfo_width(), top.winfo_height()]
        assert child.visual["direct"] is None  # (the main window's own)
        assert info["steps"]["n"] == bench.WINDOW_MOVES and info["drag"]["configures"] >= 3, info
        assert child.api.grabbed == {str(top)} and child.api.repainted == {child.api.wrapper(top)}
        child.visual_summary()
        assert child.visual["verdict"] == "dialog"
        assert child.visual["worst"]["dialog"]["differ"] == 80 and child.visual["worst"]["drag"] is None
        saved = {p.name for p in (tmp_path / "frames").iterdir()}
        assert "settled-01-help_steps-repainted.png" in saved
    finally:
        top.destroy()


def test_the_dialogs_and_the_equipment_window_are_opened_and_checked(bench, root, tmp_path, manager, monkeypatch):
    """other_windows: 사용법 and 충돌 한눈에 보기 shown without waiting for them to close, and 장비 외형 with its
    tiles (the mods' armor read as without a game), each checked while it is open (window_check) and closed after;
    the manager's game data reader is its own again. --no-windows leaves them out."""
    from conftest import make_zip

    from mhwmm import gui

    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(gui.messagebox, name, lambda *a, **k: None)
    for name in ("warm_equipment", "precheck_background"):  # (in the background: not part of this, as in the bench)
        monkeypatch.setattr(gui.App, name, lambda self: None)
    mesh = "natives/STM/Art/Model/Character/ch02/004/000/2/ch02_004_0002.mesh.241111606"
    for n in range(2):  # (two mods with the same file: a conflict)
        with manager.prepare_install(make_zip(tmp_path / f"m{n}.zip", {mesh: str(n)})) as session:
            manager.install_variant(session, session.variants[0])
    app = gui.App(root, manager, queue.Queue())
    try:
        child = _child(bench, "freeze", tmp_path, no_windows=True)
        child.root, child.app, child.api = root, app, FakeWindows(bench, root, area=(0, 0, 1000, 760))
        child.other_windows()
        assert child.visual["checks"] == [] and child.visual["windows"] == {}

        child = _child(bench, "freeze", tmp_path)
        child.root, child.app, child.api = root, app, FakeWindows(bench, root, area=(0, 0, 1000, 760))
        child.quiet_s = lambda: 0.15
        seen = []

        def window_check(kind, name, top):  # (what is open while it is checked; no drag: quicker)
            seen.append((kind, name, type(top).__name__, bool(top.winfo_viewable()),
                         "equipment_catalog" in vars(manager), getattr(top, "tiles", None)))
            child.settled_check(f"{name} steps", top, kind)
            return {}

        child.window_check = window_check
        try:
            child.other_windows()
        finally:
            child.api.close()
        assert [s[:5] for s in seen] == [("dialog", "help", "HelpDialog", True, False),
                                         ("dialog", "conflicts", "ConflictsDialog", True, False),
                                         ("equip", "equipment", "EquipmentWindow", True, True)]
        assert len(seen[2][5]) >= 1 and child.result["env"]["equip_tiles"] == len(seen[2][5])
        assert [(c["kind"], c.get("error"), c.get("differ")) for c in child.visual["checks"]] == \
            [("dialog", None, 0), ("dialog", None, 0), ("equip", None, 0)]
        assert "equipment_catalog" not in vars(manager) and app.equipment_window is None
        assert not [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
        child.visual_summary()
        assert child.visual["verdict"] == "ok"

        def broken(*args):
            raise RuntimeError("no dialog")

        monkeypatch.setattr(gui, "HelpDialog", broken)
        child = _child(bench, "freeze", tmp_path)
        child.root, child.app, child.api = root, app, FakeWindows(bench, root, area=(0, 0, 1000, 760))
        child.quiet_s = lambda: 0.05
        child.window_check = lambda kind, name, top: {}
        child.other_windows()
        assert [(c["what"], c.get("error")) for c in child.visual["checks"]] == [("help", "RuntimeError: no dialog")]
        assert not [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    finally:
        app.watcher.stop()
        del app
        gc.collect()
