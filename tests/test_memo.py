"""모드 메모 without a window: clean_memo, ModManager.set_memo (kept in state.json and mod.json, not a change of
what the game gets), memos loaded from a hand-edited state.json, updates and 다른 장비로 옮기기 keeping them, the
support zips leaving them out, and the ``memo`` command (also while the window holds the lock and while 원인 모드
찾기 runs) with the memo line of ``list``. Synthetic files only."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
import zipfile

import pytest

import test_retarget_manager as RM
from conftest import make_tree, write
from mhwmm import bisect
from mhwmm.cli import build_parser, changes_mods, main
from mhwmm.manager import MAX_MEMO, Mod, ModManager, acquire_instance_lock, clean_memo
from test_crashes import log_text

TEX = "natives/STM/Art/x/a_ALBD.tex.241106027"
SECRET = "비밀메모-7Q3Z"  # a memo that no report may hold

# the synthetic game and library of test_retarget_manager (a game pak with the catalog, the Bunny helm mod)
game, library, types = RM.game, RM.library, RM.types


@pytest.fixture(autouse=True)
def root_logger():
    """main() adds its log file and stderr handlers to the root logger on every call: take them off again."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in root.handlers[:]:
        if h not in handlers:
            root.removeHandler(h)
            h.close()
    root.setLevel(level)


def install(m: ModManager, tmp_path, name: str, files: dict | None = None, replace=None) -> Mod:
    src = make_tree(tmp_path / "src" / uuid.uuid4().hex[:8] / name, files or {TEX: name})
    with m.prepare_install(src) as session:
        return m.install_package(session, session.variants, replace_id=replace)


def memo(m: ModManager, capsys, *argv) -> tuple[int, str, str]:
    """``mhwmm memo <argv>`` on m's data folder: (exit code, stdout, stderr)."""
    capsys.readouterr()
    code = main(["--data-dir", str(m.data_dir), "memo", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def fresh(m: ModManager) -> ModManager:
    """The data folder as it was saved."""
    return ModManager(m.data_dir)


def listed(m: ModManager, capsys) -> list[str]:
    """The lines of ``mhwmm list``."""
    capsys.readouterr()
    assert main(["--data-dir", str(m.data_dir), "list"]) == 0
    return capsys.readouterr().out.splitlines()


def row(lines: list[str], name: str) -> int:
    """The line of the mod named ``name`` in ``list``."""
    return next(i for i, line in enumerate(lines) if re.match(rf"\s*\d+\. \[[x ]\] {re.escape(name)}  \(", line))


# ---- clean_memo ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, cleaned", [
    ("첫 줄\r\n둘째 줄\r셋째 줄", "첫 줄\n둘째 줄\n셋째 줄"),
    ("\ud83d" + "\ude00 웃음", "\U0001F600 웃음"),  # the UTF-16 pair Tk may hand over: one character
    ("앞\ud800뒤", "앞\ufffd뒤"),  # a lone surrogate (no save could write it)
    ("끝\ud83d", "끝\ufffd"),
    ("\udc00", "\ufffd"),
    ("a\x00b\x07c\x1bd\x7fe\x85f", "abcdef"),  # control characters
    ("이름\t값\n\t들여 쓴 줄", "이름\t값\n\t들여 쓴 줄"),  # tabs stay
    ("줄 끝 공백   \n다음\t \n", "줄 끝 공백\n다음"),
    ("\n\n  \n  들여 쓴 첫 줄\n\n\n가운데 빈 줄\n \n\n", "  들여 쓴 첫 줄\n\n\n가운데 빈 줄"),
    (" \t\n\u3000\n\r\n ", ""),  # only whitespace
    ("", ""),
    ("\ufeff앞의 BOM 글자", "\ufeff앞의 BOM 글자"),  # (not a control character: kept)
    (None, ""),
    (12, ""),
    (["목록"], ""),
])
def test_clean_memo(text, cleaned):
    assert clean_memo(text) == cleaned
    assert clean_memo(cleaned) == cleaned  # idempotent
    cleaned.encode("utf-8")  # (what save() needs)


def test_clean_memo_is_idempotent_on_mixed_text():
    for text in ("\r\r\n\x00 \ud83d\n\ude00\t\r", "a \r\n\r\n b\x0b\x0c \n\u2028", "\ud800\udc00\ud800 \n "):
        once = clean_memo(text)
        assert clean_memo(once) == once and "\r" not in once
        once.encode("utf-8")


# ---- the manager -----------------------------------------------------------------------------------------
def test_set_memo_is_kept_and_changes_nothing_the_game_gets(manager, tmp_path):
    mod = install(manager, tmp_path, "Armor")
    other = install(manager, tmp_path, "Other", {"natives/STM/b.tex": "b"})
    manager.deploy()
    manager.mod_entries(mod.id)  # (cached)
    before = (manager.revision, manager.dirty, mod.stamp, mod.files_stamp, mod.installed_at, mod.updated_at,
              mod.edited_at)
    assert (mod.memo, mod.memo_at) == ("", "")

    assert manager.set_memo(mod.id, "  쓰는 옵션: 파랑  \r\n\r\n받은 곳: 넥서스\n\n") is True
    assert mod.memo == "  쓰는 옵션: 파랑\n\n받은 곳: 넥서스"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", mod.memo_at)
    assert (manager.revision, manager.dirty, mod.stamp, mod.files_stamp, mod.installed_at, mod.updated_at,
            mod.edited_at) == before
    assert mod.id in manager._entries_cache and not manager.dirty
    assert other.memo == ""

    # kept in state.json and in mod.json (from which a mod missing from the list is put back)
    meta = json.loads((manager.mod_dir(mod.id) / "mod.json").read_text(encoding="utf-8"))
    assert (meta["memo"], meta["memo_at"]) == (mod.memo, mod.memo_at)
    again = fresh(manager)
    assert (again.get(mod.id).memo, again.get(mod.id).memo_at) == (mod.memo, mod.memo_at)
    assert not again.dirty

    # the same memo (once cleaned) changes nothing, not even the time
    stamp = mod.memo_at = "2026-01-02T03:04:05"
    assert manager.set_memo(mod.id, mod.memo + "  \n") is False and mod.memo_at == stamp

    # cleared
    assert manager.set_memo(mod.id, " \n\t") is True
    assert (mod.memo, mod.memo_at) == ("", "")
    assert fresh(manager).get(mod.id).memo == "" and manager.set_memo(mod.id, "") is False
    assert (manager.revision, manager.dirty) == before[:2]


def test_set_memo_limits(manager, tmp_path):
    mod = install(manager, tmp_path, "Armor")
    assert manager.set_memo(mod.id, "가" * MAX_MEMO)
    assert len(mod.memo) == MAX_MEMO == 5000
    kept = (mod.memo, mod.memo_at)
    with pytest.raises(ValueError) as caught:
        manager.set_memo(mod.id, "나" * (MAX_MEMO + 1) + "   \n\n")  # (counted once cleaned)
    assert str(caught.value) == "메모는 5,000자까지 쓸 수 있습니다 (지금 5,001자)."
    assert (mod.memo, mod.memo_at) == kept and fresh(manager).get(mod.id).memo == kept[0]
    assert manager.set_memo(mod.id, "나" * MAX_MEMO + " " * 10 + "\n\n")  # spaces at the end do not count
    with pytest.raises(KeyError):
        manager.set_memo("nothing", "메모")
    with pytest.raises(ValueError):  # (too long is told before an unknown mod)
        manager.set_memo("nothing", "x" * (MAX_MEMO + 1))


def test_memos_of_a_hand_edited_state_load_and_save(manager, tmp_path):
    keep = install(manager, tmp_path, "Keep")
    bad = install(manager, tmp_path, "Bad")
    blank = install(manager, tmp_path, "Blank")
    long = install(manager, tmp_path, "Long")
    lost = install(manager, tmp_path, "Lost")
    state = json.loads(manager.state_path.read_text(encoding="utf-8"))
    by_id = {raw["id"]: raw for raw in state["mods"]}
    by_id[keep.id].update(memo="보통 메모", memo_at="2026-10-01T10:00:00")
    by_id[bad.id].update(memo="깨진\ud800글자\x00\r\n", memo_at="2026-10-01T10:00:00")
    by_id[blank.id].update(memo="  \n ", memo_at="2026-10-01T10:00:00")
    by_id[long.id].update(memo="x" * (MAX_MEMO + 100), memo_at=7)  # (memo_at not a str: left out)
    state["mods"] = [raw for raw in state["mods"] if raw["id"] != lost.id]  # put back from its mod.json
    write(manager.mod_dir(lost.id) / "mod.json", json.dumps({"name": "Lost", "memo": "\ud83d\ude00 mod.json의 메모"}))
    write(manager.state_path, json.dumps(state, ensure_ascii=True))  # (\ud800 as JSON writes it)

    again = fresh(manager)
    assert (again.get(keep.id).memo, again.get(keep.id).memo_at) == ("보통 메모", "2026-10-01T10:00:00")
    assert (again.get(bad.id).memo, again.get(bad.id).memo_at) == ("깨진\ufffd글자", "2026-10-01T10:00:00")
    assert (again.get(blank.id).memo, again.get(blank.id).memo_at) == ("", "")  # no time without a memo
    assert len(again.get(long.id).memo) == MAX_MEMO + 100 and again.get(long.id).memo_at == ""  # not cut
    assert again.get(lost.id).memo == "\U0001F600 mod.json의 메모"
    again.save()  # (a lone surrogate would make this fail)
    again.rename(bad.id, "Bad 2")
    assert fresh(again).get(bad.id).memo == "깨진\ufffd글자"
    assert Mod(memo=None).memo == "" and Mod(memo="", memo_at="2026-10-01T10:00:00").memo_at == ""


def test_an_update_keeps_the_memo(manager, tmp_path):
    mod = install(manager, tmp_path, "Armor", {TEX: "v1"})
    manager.set_memo(mod.id, "업데이트해도 남는 메모")
    at = mod.memo_at
    updated = install(manager, tmp_path, "Armor", {TEX: "v2"}, replace=mod.id)
    assert updated.id == mod.id and (manager.files_dir(mod.id) / TEX).read_text() == "v2"
    assert (updated.memo, updated.memo_at) == ("업데이트해도 남는 메모", at)
    assert fresh(manager).get(mod.id).memo == "업데이트해도 남는 메모"
    manager.remove(mod.id)  # (the memo goes with the mod)
    assert mod.id not in json.dumps(json.loads(manager.state_path.read_text(encoding="utf-8"))["mods"])


def test_a_mod_moved_to_other_equipment_gets_the_memo(library):
    m, mod, _first, _last = library
    m.set_memo(mod.id, "토끼 투구\n옮겨도 그대로")
    mod.memo_at = "2026-10-30T12:34:56"
    new = m.create_retarget(mod.id, RM.A, RM.B)
    assert (new.memo, new.memo_at) == ("토끼 투구\n옮겨도 그대로", "2026-10-30T12:34:56")
    assert json.loads((m.mod_dir(new.id) / "mod.json").read_text(encoding="utf-8"))["memo"] == new.memo
    assert fresh(m).get(new.id).memo == new.memo
    m.set_memo(new.id, "옮긴 모드만의 메모")  # each its own
    assert mod.memo == "토끼 투구\n옮겨도 그대로"
    onward = m.create_retarget(new.id, RM.B, RM.THIRD, disable_original=False)  # (moved again)
    assert (onward.memo, onward.memo_at) == (new.memo, new.memo_at)
    m.set_memo(mod.id, "")
    plain = m.create_retarget(mod.id, RM.A, RM.THIRD)  # (without a memo now: none)
    assert (plain.memo, plain.memo_at) == ("", "")


def test_the_support_zips_leave_the_memos_out(library, types, tmp_path, capsys):
    m, mod, first, _last = library
    game_dir = m.game_dir
    started = time.time() - 3600  # (a recent session: the oldest are not collected)
    write(game_dir / "re2_framework_log.txt", log_text(game_dir, started, crash=str(game_dir / "MonsterHunterWilds.exe")))
    m.deploy()
    assert main(["--data-dir", str(m.data_dir), "memo", mod.name, "--set", f"{SECRET}\n둘째 줄 {SECRET}"]) == 0
    m = fresh(m)
    m.set_memo(first.id, SECRET)
    assert SECRET in m.state_path.read_text(encoding="utf-8")  # (where it is kept: never in a zip)

    # 게임 튕김 기록's support zip: report.txt, the session's records, manager.log, the deployed files
    out = tmp_path / "crash.zip"
    capsys.readouterr()
    assert main(["--data-dir", str(m.data_dir), "crash", "report", "--out", str(out)]) == 0
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "report.txt" in names and "manager.log" in names and any(n.endswith("re2_framework_log.txt")
                                                                         for n in names)
        report = zf.read("report.txt").decode("utf-8")
        assert "Bunny" in report  # (the mods are listed, without their memos)
        for name in names:
            data = zf.read(name)
            assert SECRET.encode("utf-8") not in data and SECRET.encode("utf-16-le") not in data, name
    assert not any(SECRET in repr(r) for r in m.crash_report_mods())

    # 재질 점검's diagnostics zip
    (job,) = m.material_jobs([mod.id])
    m.finish_materials(m.run_materials([job]), [job])
    out = tmp_path / "material.zip"
    assert m.export_material_report(job, out) >= 1
    assert main(["--data-dir", str(m.data_dir), "materials", "report", mod.name, "--out",
                 str(tmp_path / "cli.zip")]) == 0
    for path in (out, tmp_path / "cli.zip"):
        with zipfile.ZipFile(path) as zf:
            assert "report.json" in zf.namelist()
            for name in zf.namelist():
                data = zf.read(name)
                assert SECRET.encode("utf-8") not in data and SECRET.encode("utf-16-le") not in data, name
                assert "비밀메모".encode("unicode_escape") not in data, name  # (nor as JSON's \\uXXXX)


# ---- the command line ------------------------------------------------------------------------------------
def test_the_parser():
    parser = build_parser()
    args = parser.parse_args(["memo"])
    assert (args.cmd, args.mod, args.set, args.append, args.file, args.clear) == ("memo", None, None, None, None,
                                                                                 False)
    assert not changes_mods(args)
    for argv in (["memo", "Mod", "--set", "x"], ["memo", "Mod", "--append", "x"], ["memo", "Mod", "--file", "f"],
                 ["memo", "Mod", "--clear"], ["memo", "--set", "x", "Mod"]):
        args = parser.parse_args(argv)
        assert args.mod == "Mod" and not changes_mods(args), argv  # (allowed while 원인 모드 찾기 runs)
    for argv in (["memo", "Mod", "--set", "x", "--clear"], ["memo", "Mod", "--file", "f", "--append", "x"],
                 ["memo", "Mod", "Other"]):
        with pytest.raises(SystemExit) as caught:
            parser.parse_args(argv)
        assert caught.value.code == 2, argv


def test_memo_command(manager, tmp_path, capsys):
    first = install(manager, tmp_path, "First")
    armor = install(manager, tmp_path, "Armor")
    install(manager, tmp_path, "Plain")
    manager.set_enabled(first.id, False)

    assert memo(manager, capsys) == (0, "메모를 적은 모드가 없습니다.\n", "")
    assert memo(manager, capsys, "Armor") == (0, "'Armor'에 적은 메모가 없습니다.\n", "")

    code, out, _err = memo(manager, capsys, "Armor", "--set", "쓰는 옵션: 파랑  ")
    assert (code, out) == (0, "메모를 저장했습니다: Armor\n")
    assert fresh(manager).get(armor.id).memo == "쓰는 옵션: 파랑"
    code, out, _err = memo(manager, capsys, "2", "--set", "쓰는 옵션: 파랑")  # (by number: unchanged)
    assert (code, out) == (0, "메모가 그대로입니다: Armor\n")
    code, out, _err = memo(manager, capsys, armor.id, "--append", "함께 쓰면 안 되는 모드: Plain")  # (by id)
    assert (code, out) == (0, "메모를 저장했습니다: Armor\n")
    assert fresh(manager).get(armor.id).memo == "쓰는 옵션: 파랑\n함께 쓰면 안 되는 모드: Plain"
    code, out, _err = memo(manager, capsys, "first", "--append", "첫 메모")  # (no new line before it: was empty)
    assert (code, out) == (0, "메모를 저장했습니다: First\n") and fresh(manager).get(first.id).memo == "첫 메모"

    code, out, _err = memo(manager, capsys, "Armor")
    lines = out.splitlines()
    assert code == 0 and lines[:2] == ["쓰는 옵션: 파랑", "함께 쓰면 안 되는 모드: Plain"]
    assert re.fullmatch(r"\(\d{4}-\d\d-\d\d \d\d:\d\d 수정\)", lines[2]) and len(lines) == 3

    code, out, _err = memo(manager, capsys)
    assert code == 0 and out == ("  1. [ ] First\n"
                                 "       첫 메모\n"
                                 "\n"
                                 "  2. [x] Armor\n"
                                 "       쓰는 옵션: 파랑\n"
                                 "       함께 쓰면 안 되는 모드: Plain\n")

    code, out, _err = memo(manager, capsys, "First", "--clear")
    assert (code, out) == (0, "메모를 지웠습니다: First\n") and fresh(manager).get(first.id).memo == ""
    code, out, _err = memo(manager, capsys, "First", "--clear")
    assert (code, out) == (0, "메모가 그대로입니다: First\n")
    code, out, _err = memo(manager, capsys, "Armor", "--set", "")
    assert (code, out) == (0, "메모를 지웠습니다: Armor\n")
    assert memo(manager, capsys)[1] == "메모를 적은 모드가 없습니다.\n"


def test_memo_from_a_file(manager, tmp_path, capsys):
    armor = install(manager, tmp_path, "Armor")
    note = tmp_path / "메모.txt"
    note.write_bytes("\ufeff첫 줄\r\n\r\n\t셋째 줄  \r\n".encode("utf-8"))  # (with the BOM Notepad may write)
    code, out, _err = memo(manager, capsys, "Armor", "--file", str(note))
    assert (code, out) == (0, "메모를 저장했습니다: Armor\n")
    assert fresh(manager).get(armor.id).memo == "첫 줄\n\n\t셋째 줄"

    note.write_bytes("첫 줄".encode("cp949"))
    code, _out, err = memo(manager, capsys, "Armor", "--file", str(note))
    assert code == 1 and err.startswith("오류: 메모 파일이 UTF-8이 아닙니다: ")
    code, _out, err = memo(manager, capsys, "Armor", "--file", str(tmp_path / "없는 파일.txt"))
    assert code == 1 and err.startswith("오류: 메모 파일을 읽지 못했습니다: ")
    assert fresh(manager).get(armor.id).memo == "첫 줄\n\n\t셋째 줄"


def test_a_memo_that_starts_with_a_dash(manager, tmp_path, capsys):
    """argparse reads a value that starts with "-" (and is not a number) after a space as an option: written after
    "=" it is the memo, as --set's and --append's help (and the README) say."""
    armor = install(manager, tmp_path, "Armor")
    assert memo(manager, capsys, "Armor", "--set=-주의") == (0, "메모를 저장했습니다: Armor\n", "")
    assert memo(manager, capsys, "Armor", "--append=-30% 데미지")[0] == 0
    assert fresh(manager).get(armor.id).memo == "-주의\n-30% 데미지"
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["memo", "Armor", "--set", "-주의"])
    assert caught.value.code == 2
    with pytest.raises(SystemExit) as caught:
        main(["memo", "--help"])
    out = capsys.readouterr().out
    assert caught.value.code == 0 and '--set="-내용"' in out and '--append="-내용"' in out


def test_memo_command_errors(manager, tmp_path, capsys):
    armor = install(manager, tmp_path, "Armor")
    manager.set_memo(armor.id, "그대로")
    code, out, err = memo(manager, capsys, "Armor", "--set", "x" * (MAX_MEMO + 1))
    assert (code, out) == (1, "") and err == "오류: 메모는 5,000자까지 쓸 수 있습니다 (지금 5,001자).\n"
    code, out, err = memo(manager, capsys, "Armor", "--append", "x" * MAX_MEMO)
    assert code == 1 and "지금 5,004자" in err
    assert fresh(manager).get(armor.id).memo == "그대로"
    with pytest.raises(SystemExit, match="모드를 찾을 수 없습니다: Nothing"):
        main(["--data-dir", str(manager.data_dir), "memo", "Nothing"])
    with pytest.raises(SystemExit, match="모드를 찾을 수 없습니다: Nothing"):
        main(["--data-dir", str(manager.data_dir), "memo", "Nothing", "--set", "x"])
    for flag in (["--set", "x"], ["--append", "x"], ["--file", "f.txt"], ["--clear"]):
        with pytest.raises(SystemExit, match="메모를 쓸 모드를 지정하세요"):
            main(["--data-dir", str(manager.data_dir), "memo", *flag])
    assert fresh(manager).get(armor.id).memo == "그대로"


def test_list_shows_the_first_line_of_a_memo(manager, tmp_path, capsys):
    one = install(manager, tmp_path, "One")
    many = install(manager, tmp_path, "Many")
    long = install(manager, tmp_path, "Long")
    install(manager, tmp_path, "None")
    manager.set_memo(one.id, "한 줄 메모")
    manager.set_memo(many.id, "   들여 쓴 첫 줄\n\n둘째\n셋째")
    manager.set_memo(long.id, "가" * 61 + "\n다음 줄")
    lines = listed(manager, capsys)
    assert lines[row(lines, "One") + 1] == "       메모: 한 줄 메모"
    assert lines[row(lines, "Many") + 1] == "       메모: 들여 쓴 첫 줄 (외 2줄)"
    assert lines[row(lines, "Long") + 1] == "       메모: " + "가" * 59 + "… (외 1줄)"
    assert len(lines[row(lines, "Long") + 1]) == len("       메모: ") + 60 + len(" (외 1줄)")
    assert sum("메모:" in line for line in lines) == 3  # (none for None)


def test_the_memo_line_comes_before_missing_requirements(manager, tmp_path, capsys, monkeypatch):
    mod = install(manager, tmp_path, "Needs")
    manager.set_memo(mod.id, "필요한 모드보다 먼저")
    monkeypatch.setattr(ModManager, "missing_requirements", lambda self, mod_id: [
        {"name": "Lib", "state": "missing", "kind": "nexus", "url": "https://example.invalid/lib"}])
    lines = listed(manager, capsys)
    at = row(lines, "Needs")
    assert lines[at + 1] == "       메모: 필요한 모드보다 먼저"
    assert lines[at + 2].startswith("       필요한 모드 없음: Lib")


def test_reading_memos_while_the_window_holds_the_lock(manager, tmp_path, capsys):
    armor = install(manager, tmp_path, "Armor")
    manager.set_memo(armor.id, "창이 켜져 있어도 읽기")
    lock = acquire_instance_lock(manager.data_dir)
    try:
        code, out, _err = memo(manager, capsys)
        assert code == 0 and "창이 켜져 있어도 읽기" in out
        code, out, _err = memo(manager, capsys, "Armor")
        assert code == 0 and out.startswith("창이 켜져 있어도 읽기\n")
        for flag in (["--set", "x"], ["--append", "x"], ["--file", "f.txt"], ["--clear"]):
            code, _out, err = memo(manager, capsys, "Armor", *flag)
            assert code == 2 and "이미 실행 중" in err, flag
    finally:
        lock.close()
    assert fresh(manager).get(armor.id).memo == "창이 켜져 있어도 읽기"
    assert memo(manager, capsys, "Armor", "--clear")[0] == 0


def test_memos_can_be_written_while_the_search_runs(manager, tmp_path, capsys):
    armor = install(manager, tmp_path, "Armor")
    manager.deploy()
    write(manager.data_dir / bisect.SESSION_FILE, json.dumps({"format": bisect.SESSION_FORMAT}))
    assert bisect.active(manager.data_dir)
    code, out, err = memo(manager, capsys, "Armor", "--set", "원인 찾기 중에도")
    assert (code, out) == (0, "메모를 저장했습니다: Armor\n") and bisect.BLOCKED not in err
    again = fresh(manager)
    assert again.get(armor.id).memo == "원인 찾기 중에도" and not again.dirty
    assert main(["--data-dir", str(manager.data_dir), "disable", "Armor"]) == 1  # (what changes the mods waits)
    assert fresh(manager).get(armor.id).enabled
