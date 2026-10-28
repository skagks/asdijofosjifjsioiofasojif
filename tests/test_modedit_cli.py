"""모드 수정 on the command line (``edit`` / ``modedit``): every action's output and exit code, --dry-run, the
forms of 파일 and 경로 (game paths, file names, patterns, <pak>#HASH, numbers), options, the game copy a revert
would lose, conflicts after an update, re-applying, an unreadable record, waiting while 원인 모드 찾기 runs, the
instance lock, and the install and materials commands carrying edits along. Synthetic files only."""

from __future__ import annotations

import json
import logging
import uuid
import weakref

import pytest

import modedit_fixtures as mf
import test_matfix as M
from conftest import make_tree, make_zip, touch_later, write
from mhwmm import bisect, matfix
from mhwmm.cli import EDIT_WRITES, build_parser, changes_mods, main
from mhwmm.constants import GAME_EXE, REFW_ORIGIN
from mhwmm.manager import ModManager, acquire_instance_lock
from mhwmm.matfix import rsz
from mhwmm.matfix.pak import PakFile, path_hash
from mhwmm.modedit import EDITS_DIR, EDITS_RECORD
from mhwmm.modedit import rszdata, textconfig

ITEM = "natives/STM/GameDesign/Common/Item/ItemData.user.3"
AREA = "natives/STM/GameDesign/Common/Area/AreaTable.user.3"
TEX = "natives/STM/Art/Model/x/x_ALBD.tex.241106027"
TWIN = "natives/STM/streaming/Art/Model/x/x_ALBD.tex.241106027"
LUA = "reframework/autorun/NierUI/Config.lua"
CFG = "reframework/data/Presets/settings.json"
SELL = "_Values[_ItemId={}]._SellPrice"
BUY = "_Values[_ItemId={}]._BuyPrice"


@pytest.fixture(autouse=True)
def types(monkeypatch):
    """The made-up registry of modedit_fixtures for every file opened here."""
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", mf.registry())
    monkeypatch.setattr(rsz.TypeRegistry, "_from_file", False)
    monkeypatch.setattr(rsz.TypeRegistry, "_holds", 0)
    monkeypatch.setattr(rszdata, "_CHECKED", weakref.WeakKeyDictionary())


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


@pytest.fixture
def mgame(tmp_path):
    """A game whose pak has the game's own ItemData (every _SellPrice 10, one row more than the mods')."""
    if not matfix.available():
        pytest.skip("게임 pak을 읽을 zstd 모듈이 없습니다")
    root = tmp_path / "MonsterHunterWilds"
    write(root / GAME_EXE, "exe")
    game_items = mf.item_data(rows=[mf.item_row(i, _SellPrice=10) for i in range(4)])
    M.write_game_pak(root / "re_chunk_000.pak", {ITEM: game_items, AREA: mf.area_table()})
    return root


@pytest.fixture
def mlib(tmp_path, mgame):
    m = ModManager(tmp_path / "data")
    m.settings.check_game_running = m.settings.check_updates = m.settings.check_refw_updates = False
    m.set_game_dir(mgame)
    return m


def game_library(tmp_path, files: dict) -> ModManager:
    """mlib on a game whose pak has ``files`` ({game path: data})."""
    if not matfix.available():
        pytest.skip("게임 pak을 읽을 zstd 모듈이 없습니다")
    root = tmp_path / "MonsterHunterWilds"
    write(root / GAME_EXE, "exe")
    M.write_game_pak(root / "re_chunk_000.pak", files)
    m = ModManager(tmp_path / "data")
    m.settings.check_game_running = m.settings.check_updates = m.settings.check_refw_updates = False
    m.set_game_dir(root)
    return m


# ---- helpers ---------------------------------------------------------------------------------------------
def install(m: ModManager, tmp_path, name: str, files: dict, replace=None):
    src = make_tree(tmp_path / "src" / uuid.uuid4().hex[:8] / name, files)
    with m.prepare_install(src) as session:
        return m.install_package(session, session.variants, replace_id=replace)


def install_options(m: ModManager, tmp_path, name: str, options: dict, selected=None):
    """A mod with options ({option name: {game path: data}}) from a zip; ``selected``: option indices."""
    files = {f"{name}/{opt}/{rel}": data for opt, tree in options.items() for rel, data in tree.items()}
    archive = make_zip(tmp_path / "zips" / uuid.uuid4().hex[:8] / f"{name}.zip", files)
    with m.prepare_install(archive) as session:
        return m.install_package(session, session.variants, selected=selected)


def install_pak(m: ModManager, tmp_path, name: str, members: dict):
    pak = mf.pak(tmp_path / "pak" / uuid.uuid4().hex[:8] / f"{name}.pak", members, compress=True)
    with m.prepare_install(pak) as session:
        return m.install_package(session, session.variants)


def edit(m: ModManager, capsys, *argv) -> tuple[int, str, str]:
    """``mhwmm edit <argv>`` on m's data folder: (exit code, stdout, stderr)."""
    capsys.readouterr()
    code = main(["--data-dir", str(m.data_dir), "edit", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def fresh(m: ModManager) -> ModManager:
    """The data folder as the command line left it."""
    return ModManager(m.data_dir)


def value(data: bytes, at: str) -> str:
    return rszdata.DataFile(data, mf.registry()).get(at).text


def keys(m: ModManager, mod_id: str) -> dict:
    return {rel: key for rel, (_path, key) in m.mod_entries(mod_id).items()}


def nothing_written(m: ModManager, mod_id: str) -> bool:
    folder = m.mod_dir(mod_id)
    leftovers = list(m.temp_root.glob("modedit-*")) if m.temp_root.is_dir() else []
    return not (folder / EDITS_RECORD).exists() and not (folder / EDITS_DIR).exists() and not leftovers


# ---- the parser and what waits for 원인 모드 찾기 -----------------------------------------------------------
def test_the_parser_and_the_writing_actions():
    parser = build_parser()
    args = parser.parse_args(["edit", "Mod"])
    assert (args.cmd, args.mod, args.action) == ("edit", "Mod", "status") and not changes_mods(args)
    args = parser.parse_args(["modedit", "3", "set", "a.user.3", "_X=1", "--option", "Blue", "b[k=1]=2", "--same"])
    assert (args.cmd, args.file, args.pairs, args.option, args.same) == ("modedit", "a.user.3", ["_X=1", "b[k=1]=2"],
                                                                          "Blue", True)
    assert changes_mods(args)
    args = parser.parse_args(["edit", "Mod", "scale", "f", "--to", "-5", "--dry-run"])
    assert args.to == "-5" and not changes_mods(args)  # (--dry-run writes nothing)
    for action, extra in (("exclude", ["x"]), ("include", ["x"]), ("use", ["x", "--option", "A"]),
                          ("set", ["f", "a=1"]), ("scale", ["f", "--times", "2"]), ("revert", ["--all"]),
                          ("resolve", ["f", "--every", "--keep"]), ("reapply", []), ("clear-record", ["--yes"])):
        args = parser.parse_args(["edit", "Mod", action, *extra])
        assert action in EDIT_WRITES and changes_mods(args), action
    for action in ("status", "files", "values"):
        assert not changes_mods(parser.parse_args(["edit", "Mod", action])), action
    for argv in (["edit", "Mod", "bogus"], ["edit", "Mod", "set", "f"], ["edit", "Mod", "resolve", "f", "--keep"],
                 ["edit", "Mod", "resolve", "f", "a", "--every", "--take"], ["edit", "Mod", "use", "x"],
                 ["edit", "Mod", "use", "x", "--option", "A", "--nth", "0"], ["edit", "Mod", "scale", "f"],
                 ["edit", "Mod", "exclude", "x", "--option", "A", "--all-options"], ["edit", "Mod", "files", "x"]):
        with pytest.raises(SystemExit) as caught:
            parser.parse_args(argv)
        assert caught.value.code == 2, argv


def test_writing_actions_wait_while_the_search_runs(manager, tmp_path, capsys):
    mod = install(manager, tmp_path, "Look", {TEX: "t", ITEM: mf.item_data(3)})
    write(manager.data_dir / bisect.SESSION_FILE, json.dumps({"format": bisect.SESSION_FORMAT}))
    code, _out, err = edit(manager, capsys, "Look", "exclude", TEX, "--no-deploy")
    assert code == 1 and bisect.BLOCKED in err and nothing_written(manager, mod.id)
    code, _out, err = edit(manager, capsys, "Look", "clear-record", "--yes")
    assert code == 1 and bisect.BLOCKED in err
    code, out, _err = edit(manager, capsys, "Look", "exclude", TEX, "--dry-run")  # (changes nothing)
    assert code == 0 and "(--dry-run: 저장하지 않았습니다)" in out and nothing_written(manager, mod.id)
    assert edit(manager, capsys, "Look")[0] == 0 and edit(manager, capsys, "Look", "values")[0] == 0


def test_edit_takes_the_instance_lock(manager, tmp_path, capsys):
    install(manager, tmp_path, "Look", {TEX: "t"})
    lock = acquire_instance_lock(manager.data_dir)
    try:
        code, _out, err = edit(manager, capsys, "Look")  # (even status: it reads what another manager may be writing)
        assert code == 2 and "이미 실행 중" in err
    finally:
        lock.close()
    assert edit(manager, capsys, "Look")[0] == 0


def test_reframework_and_unknown_mods(manager, tmp_path, capsys):
    mod = install(manager, tmp_path, "REFramework", {"dinput8.dll": "MZ"})
    mod.origin = REFW_ORIGIN
    manager._write_mod_meta(mod)
    manager.save()
    code, _out, err = edit(manager, capsys, "REFramework")
    assert code == 1 and "REFramework는 모드 수정으로 고칠 수 없습니다" in err
    with pytest.raises(SystemExit, match="모드를 찾을 수 없습니다: Nothing"):
        main(["--data-dir", str(manager.data_dir), "edit", "Nothing"])


# ---- 파일 고르기 ----------------------------------------------------------------------------------------------
def test_status_and_files_of_a_mod_without_edits(manager, tmp_path, capsys):
    install(manager, tmp_path, "Look", {TEX: "t", TWIN: "s", ITEM: mf.item_data(3), LUA: mf.NIER_CONFIG_LUA})
    code, out, _err = edit(manager, capsys, "Look")
    assert code == 0 and out == "Look: 이 모드에 한 수정이 없습니다.\n"
    code, out, _err = edit(manager, capsys, "Look", "files")
    lines = out.splitlines()
    assert code == 0 and lines[0] == "Look: 파일 4개"
    assert lines[1:] == [f"  [넣음]  {TEX}  (텍스처)", f"  [넣음]  {ITEM}  (데이터)", f"  [넣음]  {TWIN}  (텍스처)",
                         f"  [넣음]  {LUA}  (스크립트)"]
    code, out, _err = edit(manager, capsys, "Look", "files", "--find", "albd", "--excluded")
    assert code == 0 and out.splitlines()[1:] == ["  (맞는 파일이 없습니다)"]
    code, _out, err = edit(manager, capsys, "Look", "files", "--option", "Blue")
    assert code == 1 and "이 모드에는 옵션이 없습니다" in err


def test_exclude_and_include_with_patterns_and_twins(manager, tmp_path, game_dir, capsys):
    mod = install(manager, tmp_path, "Look", {TEX: "t", TWIN: "s", "natives/STM/Art/Model/x/x.mdf2.45": "m"})
    manager.deploy()
    code, out, _err = edit(manager, capsys, "Look", "exclude", "x_albd.TEX.*", "--dry-run")  # file names, any case
    assert code == 0 and out.startswith(f"  뺌: {TEX}\n  뺌: {TWIN}\n")
    code, out, _err = edit(manager, capsys, "Look", "exclude", "natives/stm/art/model/X/x_albd.tex.*", "--dry-run")
    assert code == 0 and out.startswith(f"  뺌: {TEX}\n  뺌: {TWIN} (스트리밍 사본도 함께 뺍니다)\n")  # (the twin follows)
    assert out.endswith("(--dry-run: 저장하지 않았습니다)\n") and nothing_written(manager, mod.id)

    code, out, _err = edit(manager, capsys, "Look", "exclude", TEX)
    assert code == 0 and "모드 수정을 저장했습니다: Look (뺀 파일 2개)" in out
    assert "참고: 같은 이름의 파일 가운데 일부만 뺐습니다" not in out  # (x.mdf2 is another stem)
    assert "재질 점검 결과는 다시 점검할 때까지 '점검 전'입니다" in out
    assert not (game_dir / TEX).exists() and not (game_dir / TWIN).exists()  # applied
    after = fresh(manager)
    assert after.excluded_files(mod.id) == [TEX, TWIN] and after.edit_summary(mod.id).excluded == 2
    code, out, _err = edit(manager, capsys, "Look")
    assert out.splitlines() == ["Look: 뺀 파일 2개", f"  [뺌]    {TEX}", f"  [뺌]    {TWIN}",
                                "수정은 모든 프로필에 똑같이 쓰입니다."]
    code, out, _err = edit(manager, capsys, "Look", "files", "--excluded")
    assert out.splitlines() == ["Look: 파일 3개 · 뺀 파일 2개", f"  [뺌]  {TEX}  (텍스처)", f"  [뺌]  {TWIN}  (텍스처)"]

    code, out, _err = edit(manager, capsys, "Look", "exclude", TEX, "--no-deploy")
    assert code == 0 and f"  이미 뺀 파일입니다: {TEX}\n" in out and "바뀐 것이 없습니다." in out
    code, _out, err = edit(manager, capsys, "Look", "exclude", "*.dds", "--no-deploy")
    assert code == 1 and "오류: 맞는 파일이 없습니다: *.dds" in err

    code, out, _err = edit(manager, capsys, "Look", "include", TEX)
    assert code == 0 and f"  다시 넣음: {TEX}\n  다시 넣음: {TWIN} (스트리밍 사본도 함께 넣습니다)\n" in out
    assert (game_dir / TEX).read_text() == "t" and not (manager.mod_dir(mod.id) / EDITS_RECORD).exists()


def test_exclude_notes(manager, tmp_path, capsys):
    bank = "natives/STM/Sound/bgm_field.sbnk.1.x64"
    install(manager, tmp_path, "Music", {bank: "b", "natives/STM/Sound/bgm_field.spck.1.x64": "p",
                                         "reframework/autorun/music/util.lua": "return {}",
                                         "reframework/autorun/music.lua": 'local u = require("music.util")'})
    code, out, _err = edit(manager, capsys, "Music", "exclude", bank, "--dry-run")
    assert code == 0
    assert ("참고: 같은 이름의 파일 가운데 일부만 뺐습니다: bgm_field (bgm_field.spck.1.x64은 넣고 bgm_field.sbnk.1.x64은 "
            "뺌). 함께 빼야 할 수 있습니다. (함께 빼기: edit Music exclude \"natives/STM/Sound/bgm_field.*\")") in out
    code, out, _err = edit(manager, capsys, "Music", "exclude", "util.lua", "--dry-run")
    assert "참고: reframework/autorun/music/util.lua: 다른 스크립트(music.lua)가 이 파일을 불러옵니다." in out
    code, out, _err = edit(manager, capsys, "Music", "exclude", "*", "--dry-run")
    assert "경고: 모든 파일을 빼면 이 모드는 게임에 아무것도 넣지 않습니다." in out


def test_options_need_a_choice_and_use_picks_a_copy(manager, tmp_path, game_dir, capsys):
    mod = install_options(manager, tmp_path, "Colors", {"Blue": {TEX: "blue", TWIN: "blue-s", "natives/STM/b.tex": "b"},
                                                        "Green": {TEX: "green"}, "Red": {TEX: "red"}},
                          selected=[0, 1])
    code, out, _err = edit(manager, capsys, "Colors", "files", "--overlaps")
    assert code == 0 and out.splitlines()[0] == "Colors: 파일 5개 · 켠 옵션: Blue, Green"
    assert f"  [덮임]       Blue: {TEX}  (텍스처 · 아래쪽 옵션 'Green'의 파일이 덮어씀 · 켠 옵션 Blue, Green에 있음)" in out
    assert "쓸 옵션을 고르려면: edit Colors use <경로> --option <이름>" in out

    code, _out, err = edit(manager, capsys, "Colors", "exclude", TEX, "--no-deploy")
    assert code == 1 and "여러 옵션에 있는 파일입니다" in err and "(옵션: 'Blue', 'Green', 'Red')" in err
    code, _out, err = edit(manager, capsys, "Colors", "exclude", TEX, "--option", "Purple", "--no-deploy")
    assert code == 1 and "옵션을 찾지 못했습니다: Purple (옵션: 'Blue', 'Green', 'Red')" in err
    code, out, _err = edit(manager, capsys, "Colors", "exclude", TEX, "--option", "Red", "--dry-run")
    assert code == 0 and f"참고: Red: {TEX}: 이 옵션은 지금 꺼져 있어 빼도 게임에는 달라지는 것이 없습니다." in out

    # 쓸 옵션: Blue's copy (Green's is left out)
    code, out, _err = edit(manager, capsys, "Colors", "use", TEX, "--option", "Blue")
    assert code == 0 and f"  {TEX}: 옵션 'Blue'의 파일을 씁니다 (뺌: 'Green')" in out
    assert f"(자동으로 아래쪽 옵션의 파일을 쓰려면: edit Colors include {TEX} --all-options)" in out
    assert "모드 수정을 저장했습니다: Colors (옵션 'Blue'의 파일 쓰기 · 뺀 파일 1개)" in out
    assert (game_dir / TEX).read_text() == "blue"
    after = fresh(manager)
    blue = next(o["id"] for o in after.get(mod.id).options if o["name"] == "Blue")
    assert keys(after, mod.id)[TEX] == f"{blue}/{TEX}"
    code, out, _err = edit(manager, capsys, "Colors")
    assert out.splitlines() == ["Colors: 뺀 파일 1개", f"  [뺌]    Green: {TEX}", "수정은 모든 프로필에 똑같이 쓰입니다.",
                                "옵션별 수정은 그 옵션을 켠 프로필에서만 게임에 들어갑니다."]
    code, out, _err = edit(manager, capsys, "Colors", "use", TEX, "--option", "Blue", "--no-deploy")
    assert code == 0 and "이미 옵션 'Blue'의 파일을 쓰고 있습니다" in out and "바뀐 것이 없습니다." in out
    code, _out, err = edit(manager, capsys, "Colors", "use", TEX, "--option", "Red")
    assert code == 1 and "옵션 'Red'이(가) 꺼져 있습니다" in err
    code, _out, err = edit(manager, capsys, "Colors", "use", "natives/STM/b.tex", "--option", "Blue")
    assert code == 1 and "다른 켠 옵션에 같은 파일이 없어 고를 것이 없습니다" in err

    # 자동 (아래쪽 옵션) again
    code, out, _err = edit(manager, capsys, "Colors", "include", TEX, "--all-options")
    assert code == 0 and f"  다시 넣음: Green: {TEX}" in out and (game_dir / TEX).read_text() == "green"
    assert fresh(manager).edit_summary(mod.id).empty


def test_revert_of_a_left_out_texture_puts_back_its_streaming_copy(manager, tmp_path, game_dir, capsys):
    mesh, mesh_twin = "natives/STM/Art/Model/x/x.mesh.241111606", "natives/STM/streaming/Art/Model/x/x.mesh.241111606"
    files = {TEX: "t", TWIN: "s", mesh: "m", mesh_twin: "ms"}
    mod = install(manager, tmp_path, "Look", files)
    for path, twin in ((TEX, TWIN), (mesh, mesh_twin)):  # (a model's streaming copy follows it as a texture's does)
        assert edit(manager, capsys, "Look", "exclude", path)[0] == 0  # (the streaming copy is left out with it)
        for ref, other in ((path, twin), (twin, path)):  # (either one puts back both)
            code, out, _err = edit(manager, capsys, "Look", "revert", ref, "--no-deploy")
            assert code == 0 and f"  되돌림: {ref} (뺀 파일)\n  다시 넣음: {other} (스트리밍 사본도 함께 넣습니다)\n" in out
            assert "텍스처" not in out and "모드 수정을 저장했습니다: Look (되돌린 수정 2개)" in out
            after = fresh(manager)
            assert after.excluded_files(mod.id) == [] and after.edit_summary(mod.id).empty
            assert edit(manager, capsys, "Look", "exclude", path, "--no-deploy")[0] == 0
        code, _out, _err = edit(manager, capsys, "Look", "revert", path)
        assert code == 0 and [(game_dir / p).read_text() for p in (path, twin)] == [files[path], files[twin]]


def test_option_given_with_a_number_or_the_list(manager, tmp_path, capsys):
    mod = install_options(manager, tmp_path, "Prices", {"A": {ITEM: mf.item_data(3)}, "B": {ITEM: mf.item_data(3)},
                                                        "C": {TEX: "t"}}, selected=[0, 1])
    code, _out, err = edit(manager, capsys, "Prices", "values", "--option", "Purple")
    assert code == 1 and "오류: 옵션을 찾지 못했습니다: Purple (옵션: 'A', 'B', 'C')" in err
    code, out, _err = edit(manager, capsys, "Prices", "values", "--option", "C")
    assert code == 0 and out.splitlines() == ["Prices: 값을 바꿀 수 있는 파일 2개", "  (옵션 'C'에는 값을 바꿀 수 있는 파일이 없습니다)",
                                              "값을 보려면: edit Prices values <번호 또는 파일>"]
    code, out, _err = edit(manager, capsys, "Prices", "values", "--option", "B")
    assert code == 0 and out.splitlines()[1:-1] == [f"    2. [데이터] B: {ITEM}"]  # (numbers are of the whole list)

    # a number names one file: an --option that is not its option is refused, not dropped
    for argv in (("values", "1"), ("set", "1", f"{SELL.format(100)}=1", "--no-deploy")):
        code, _out, err = edit(manager, capsys, "Prices", *argv, "--option", "B")
        assert code == 1 and ("오류: 1번 파일은 옵션 'A'의 파일입니다 (옵션 'B'의 파일 번호: edit <모드> values --option B)"
                              in err), argv
        code, _out, err = edit(manager, capsys, "Prices", *argv, "--option", "Purple")
        assert code == 1 and "오류: 옵션을 찾지 못했습니다: Purple" in err, argv
    assert nothing_written(manager, mod.id)
    code, out, _err = edit(manager, capsys, "Prices", "set", "2", f"{SELL.format(100)}=1", "--option", "B", "--no-deploy")
    assert code == 0 and "모드 수정을 저장했습니다: Prices (바꾼 값 1개)" in out
    (item,) = fresh(manager).edit_record(mod.id).items
    assert item.source.option == "B"
    code, _out, err = edit(manager, capsys, "Prices", "revert", "2", "--option", "A", "--no-deploy")
    assert code == 1 and "오류: 2번 파일은 옵션 'B'의 파일입니다" in err
    code, _out, err = edit(manager, capsys, "Prices", "revert", ITEM, "--option", "Purple", "--no-deploy")
    assert code == 1 and "오류: 옵션을 찾지 못했습니다: Purple" in err
    code, out, _err = edit(manager, capsys, "Prices", "revert", "2", "--option", "B", "--no-deploy")
    assert code == 0 and f"  되돌림: B: {ITEM} (바꾼 값 1개)" in out


def test_revert_all_with_an_option_is_refused(manager, tmp_path, capsys):
    mod = install_options(manager, tmp_path, "Prices", {"A": {ITEM: mf.item_data(3)}, "B": {ITEM: mf.item_data(3)},
                                                        "C": {TEX: "t"}}, selected=[0, 1])
    for n, option in (("1", "A"), ("2", "B")):
        code, _out, _err = edit(manager, capsys, "Prices", "set", n, f"{SELL.format(100)}=1", "--option", option,
                                "--no-deploy")
        assert code == 0, option

    def options_edited():
        return sorted(i.source.option for i in fresh(manager).edit_record(mod.id).items)

    # --all is every option's edits: with --option it is refused, not run on every option (a typo too)
    code, _out, err = edit(manager, capsys, "Prices", "revert", "--all", "--option", "B", "--no-deploy")
    assert code == 1 and err.strip() == ("오류: --all은 모든 옵션의 수정을 되돌리므로 --option과 함께 쓸 수 없습니다. 옵션 'B'의 "
                                         "수정은 파일마다 되돌립니다: edit Prices revert ItemData.user.3 --option B")
    code, _out, err = edit(manager, capsys, "Prices", "revert", "--all", "--option", "Purple", "--no-deploy")
    assert code == 1 and "오류: 옵션을 찾지 못했습니다: Purple (옵션: 'A', 'B', 'C')" in err
    code, _out, err = edit(manager, capsys, "Prices", "revert", "--all", "--option", "C", "--no-deploy")
    assert code == 1 and err.strip().endswith("함께 쓸 수 없습니다. 옵션 'C'에는 수정한 파일이 없습니다")
    # without --all the hint does not lead to it with the option
    code, _out, err = edit(manager, capsys, "Prices", "revert", "--option", "B", "--no-deploy")
    assert code == 1 and err.strip() == ("오류: 되돌릴 파일을 적어 주세요. 옵션 'B'의 수정은 파일마다 되돌립니다: edit Prices revert "
                                         "ItemData.user.3 --option B (모든 옵션의 수정을 되돌리려면: edit Prices revert --all)")
    code, _out, err = edit(manager, capsys, "Prices", "revert", "--option", "Purple", "--no-deploy")
    assert code == 1 and "오류: 옵션을 찾지 못했습니다: Purple" in err
    assert options_edited() == ["A", "B"]

    code, out, _err = edit(manager, capsys, "Prices", "revert", ITEM, "--option", "B", "--no-deploy")  # (as the hint)
    assert code == 0 and f"  되돌림: B: {ITEM} (바꾼 값 1개)" in out and options_edited() == ["A"]
    code, out, _err = edit(manager, capsys, "Prices", "revert", "--all", "--no-deploy")
    assert code == 0 and options_edited() == []

    # a mod without options, and an unreadable record (only --all without an option sets it aside)
    plain = install(manager, tmp_path, "Plain", {ITEM: mf.item_data(3)})
    code, _out, err = edit(manager, capsys, "Plain", "revert", "--all", "--option", "X", "--no-deploy")
    assert code == 1 and "오류: 이 모드에는 옵션이 없습니다 (--option X)" in err
    write(manager.mod_dir(mod.id) / EDITS_RECORD, "{broken")
    code, _out, err = edit(manager, capsys, "Prices", "revert", "--all", "--option", "B", "--no-deploy")
    assert code == 1 and "모드 수정 기록을 읽지 못해 고칠 수 없습니다" in err and "revert --all" in err
    assert (manager.mod_dir(mod.id) / EDITS_RECORD).read_text() == "{broken" and nothing_written(manager, plain.id)


def test_set_and_scale_say_when_the_game_does_not_get_the_file(manager, tmp_path, game_dir, capsys):
    mod = install_options(manager, tmp_path, "Prices", {"A": {ITEM: mf.item_data(3)}, "B": {ITEM: mf.item_data(3)}},
                          selected=[0, 1])
    manager.deploy()
    over = "참고: 아래쪽 옵션 'B'의 파일이 이 파일을 덮어써 바꾼 값이 게임에 들어가지 않습니다."
    code, out, _err = edit(manager, capsys, "Prices", "values", ITEM, "--option", "A")
    assert code == 0 and out.splitlines()[1] == over
    for argv in (("set", ITEM, f"{SELL.format(100)}=1", "--dry-run"),
                 ("scale", ITEM, "--to", "7", "--field", "*_BuyPrice", "--dry-run"),
                 ("set", ITEM, f"{SELL.format(100)}=1")):
        code, out, _err = edit(manager, capsys, "Prices", *argv, "--option", "A")
        lines = out.splitlines()
        assert code == 0 and over in lines, argv
        assert lines.index(over) == len(lines) - (2 if "--dry-run" in argv else 3), argv  # (just before the save)
    assert value((game_dir / ITEM).read_bytes(), SELL.format(100)) == "30"  # (B's copy is what the game gets)
    code, out, _err = edit(manager, capsys, "Prices", "set", ITEM, f"{SELL.format(100)}=2", "--option", "B")
    assert code == 0 and "참고: 아래쪽" not in out and value((game_dir / ITEM).read_bytes(), SELL.format(100)) == "2"

    # its option off, the file left out
    b = next(o["id"] for o in manager.get(mod.id).options if o["name"] == "B")
    manager.set_options(mod.id, [b])
    code, out, _err = edit(manager, capsys, "Prices", "set", ITEM, f"{SELL.format(101)}=1", "--option", "A",
                           "--no-deploy")
    assert code == 0 and "참고: 옵션 'A'이(가) 꺼져 있어 바꾼 값은 그 옵션을 켜야 게임에 들어갑니다." in out
    assert edit(manager, capsys, "Prices", "exclude", ITEM, "--option", "B", "--no-deploy")[0] == 0
    code, out, _err = edit(manager, capsys, "Prices", "scale", ITEM, "--to", "3", "--field", "*_BuyPrice",
                           "--option", "B", "--dry-run")
    assert code == 0 and "참고: 게임에 넣지 않는 파일이라 바꾼 값은 이 파일을 다시 넣어야 게임에 들어갑니다." in out


def test_another_options_copy_over_a_changed_game_copy(manager, tmp_path, game_dir, capsys):
    """exclude, include and use need no --yes when the game copy changed: the deploy keeps the changed copy in
    edited\\ (with a warning) and puts it back with its own option's file (#0), as the window does."""
    mod = install_options(manager, tmp_path, "Presets", {"A": {CFG: b'{"scale": 1.0}\n'},
                                                         "B": {CFG: b'{"scale": 2.0}\n'}}, selected=[0, 1])
    manager.deploy()
    mine = b'{"scale": 7.5, "note": "saved by the script"}\n'
    touch_later(game_dir / CFG, mine)  # (B's copy, changed in the game folder)
    kept = manager.data_dir / "edited" / CFG
    code, out, err = edit(manager, capsys, "Presets", "use", CFG, "--option", "A")
    assert code == 0 and "모드 수정을 저장했습니다: Presets" in out and not err
    assert (game_dir / CFG).read_bytes() == b'{"scale": 1.0}\n' and kept.read_bytes() == mine
    assert any(line.startswith("경고: ") and CFG in line and "따로 보관" in line for line in out.splitlines())
    code, out, _err = edit(manager, capsys, "Presets", "include", CFG, "--option", "B")
    assert code == 0 and (game_dir / CFG).read_bytes() == mine  # (B's copy back: the user's)
    assert fresh(manager).edit_summary(mod.id).empty
    for argv in (("use", CFG, "--option", "A"), ("include", CFG, "--all-options"), ("exclude", CFG, "--option", "B")):
        with pytest.raises(SystemExit) as caught:  # (no --yes: nothing is lost)
            build_parser().parse_args(["edit", "Presets", *argv, "--yes"])
        assert caught.value.code == 2, argv


# ---- 게임 수치 -------------------------------------------------------------------------------------------------
def test_values_lists_files_and_their_values(manager, tmp_path, capsys):
    install(manager, tmp_path, "Items", {ITEM: mf.item_data(3), LUA: mf.NIER_CONFIG_LUA, TEX: "t"})
    code, out, _err = edit(manager, capsys, "Items", "values")
    assert code == 0 and out.splitlines() == [
        "Items: 값을 바꿀 수 있는 파일 2개", f"    1. [데이터] {ITEM}", f"    2. [설정]   {LUA}",
        "값을 보려면: edit Items values <번호 또는 파일>"]
    # without a game copy every value is shown
    code, out, _err = edit(manager, capsys, "Items", "values", "itemdata.user.3", "--find", "SellPrice")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith(f"{ITEM} (app.user_data.TestItemData) · 값 ")
    assert f"참고: {rszdata.NO_GAME}" in lines
    assert [line.split()[0] for line in lines if line.startswith("  _")] == [SELL.format(100 + i) for i in range(3)]
    code, out, _err = edit(manager, capsys, "Items", "values", "1", "--limit", "2")
    assert code == 0 and out.splitlines()[-1].startswith("  … ") and "개 중 2개 (--limit으로 더 보기)" in out
    code, out, _err = edit(manager, capsys, "Items", "values", "2")
    assert code == 0 and out.splitlines()[0] == f"{LUA} · 값 5개 · 설정 같은 값 5개"
    assert "  Config.UI_Scale     float    1.0" in out.splitlines()
    code, _out, err = edit(manager, capsys, "Items", "values", "3")
    assert code == 1 and "3번 파일이 없습니다" in err
    code, _out, err = edit(manager, capsys, "Items", "values", "Other.user.3")
    assert code == 1 and "값을 바꿀 수 있는 파일 가운데 맞는 것이 없습니다: Other.user.3" in err


def test_set_single_values_and_rules(mlib, tmp_path, capsys):
    rows = [mf.item_row(i, _SellPrice=99999 if i != 1 else 5) for i in range(3)]
    mod = install(mlib, tmp_path, "Item Cost Tweaks", {ITEM: mf.item_data(rows=rows)})
    code, out, _err = edit(mlib, capsys, "Item Cost Tweaks", "values", "ItemData.user.3", "--find", "SellPrice")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith(f"{ITEM} (app.user_data.TestItemData) · 값 ") and "· 모드가 바꾼 값 " in lines[0]
    assert f"  {SELL.format(101)}  S32   게임 10 → 모드 5" in lines
    assert "(모드가 바꾼 값만 보여 줍니다. 모두 보기: --all)" in lines and "  게임에 쓰인 값: 10 (_Values[*]._SellPrice)" in lines

    code, out, _err = edit(mlib, capsys, "Item Cost Tweaks", "set", "ItemData.user.3", f"{SELL.format(100)}=5000",
                           "--same", "--no-deploy")
    assert code == 0 and "  _Values[*]._SellPrice (값이 99999인 행 2개): 99999 → 5000" in out
    assert f"참고: {rszdata.INT_CAUTION}" in out and f"참고: {rszdata.ONLINE_CAUTION}" in out
    assert f"    경고: {rszdata.NOT_IN_HINT} (게임에 쓰인 값: 10)" in out
    assert "모드 수정을 저장했습니다: Item Cost Tweaks (바꾼 값 1개)" in out
    code, out, _err = edit(mlib, capsys, "Item Cost Tweaks", "set", ITEM, f"{BUY.format(101)}=100", "--no-deploy")
    assert code == 0 and f"  {BUY.format(101)}: 120 → 100" in out

    after = fresh(mlib)
    data = after.mod_entries(mod.id)[ITEM][0].read_bytes()
    assert [value(data, SELL.format(100 + i)) for i in range(3)] == ["5000", "5", "5000"]
    assert value(data, BUY.format(101)) == "100"
    code, out, _err = edit(mlib, capsys, "Item Cost Tweaks")
    assert out.splitlines() == ["Item Cost Tweaks: 바꾼 값 2개 (파일 1개)", f"  [수치]  {ITEM}",
                                "          _Values[*]._SellPrice: 99999 → 5000 (행 2개)",
                                f"          {BUY.format(101)}: 120 → 100", "수정은 모든 프로필에 똑같이 쓰입니다."]
    code, out, _err = edit(mlib, capsys, "Item Cost Tweaks", "values", ITEM, "--find", "SellPrice")
    assert f"  {SELL.format(102)}  S32   게임 10 → 모드 99999 → 5000 (규칙: 값이 99999인 행 모두)" in out.splitlines()

    # a row the rule covers cannot be set back on its own: the rule is reverted instead
    code, _out, err = edit(mlib, capsys, "Item Cost Tweaks", "set", ITEM, f"{SELL.format(102)}=99999", "--no-deploy")
    assert code == 1 and "규칙으로 바뀝니다" in err and "revert ItemData.user.3 \"_Values[*]._SellPrice\"" in err
    code, out, _err = edit(mlib, capsys, "Item Cost Tweaks", "revert", ITEM, SELL.format(102), "--no-deploy")
    assert code == 0 and "규칙으로 바뀌었습니다. 원래 값으로 하면 규칙 전체를 되돌립니다 (행 2개)" in out
    code, out, _err = edit(mlib, capsys, "Item Cost Tweaks", "revert", ITEM, BUY.format(101), "--no-deploy")
    assert code == 0 and f"  {BUY.format(101)}: 100 → 120 (원래 값)" in out
    assert fresh(mlib).edit_summary(mod.id).empty and keys(fresh(mlib), mod.id)[ITEM] == ITEM

    for pair, why in (("_Values[_ItemId=999]._SellPrice=1", "이 파일에 없는 주소입니다: _Values[_ItemId=999]._SellPrice"),
                      ("_SellPrice", "'주소=값' 꼴로 적어 주세요"), (f"{SELL.format(100)}=abc", "")):
        code, _out, err = edit(mlib, capsys, "Item Cost Tweaks", "set", ITEM, pair, "--no-deploy")
        assert code == 1 and "오류: " in err and why in err
    code, _out, err = edit(mlib, capsys, "Item Cost Tweaks", "revert", ITEM, SELL.format(100), "--no-deploy")
    assert code == 1 and f"수정한 파일 가운데 맞는 것이 없습니다: {ITEM}" in err


def test_set_dry_run_builds_but_writes_nothing(manager, tmp_path, capsys):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    code, out, _err = edit(manager, capsys, "Items", "set", ITEM, f"{SELL.format(101)}=1", "--dry-run")
    assert code == 0 and f"  {SELL.format(101)}: 60 → 1" in out and out.endswith("(--dry-run: 저장하지 않았습니다)\n")
    assert nothing_written(manager, mod.id) and fresh(manager).get(mod.id).edited_at == ""
    # a dry run still fails as the save would (the value does not parse)
    code, _out, err = edit(manager, capsys, "Items", "set", ITEM, f"{SELL.format(101)}=1.5", "--dry-run")
    assert code == 1 and "오류: " in err and nothing_written(manager, mod.id)


def test_pak_entries_by_hash_and_number(manager, tmp_path, game_dir, capsys):
    mod = install_pak(manager, tmp_path, "Rates", {ITEM: mf.item_data(3), AREA: mf.area_table(), TEX: b"TEXTURE"})
    item, area = f"#{path_hash(ITEM):016X}", f"#{path_hash(AREA):016X}"
    code, out, _err = edit(manager, capsys, "Rates", "values")
    lines = out.splitlines()
    assert code == 0 and lines[0] == "Rates: 값을 바꿀 수 있는 파일 2개"
    assert {line.split()[2] for line in lines[1:3]} == {f"Rates.pak{item}", f"Rates.pak{area}"}
    code, out, _err = edit(manager, capsys, "Rates", "values", "Rates.pak")
    assert code == 0 and out.startswith("PAK 안에 값을 바꿀 수 있는 파일이 여러 개입니다. #해시까지 적거나 번호로 고르세요.")
    code, out, _err = edit(manager, capsys, "Rates", "values", f"rates.pak{area.lower()}")
    assert code == 0 and out.splitlines()[0] == f"Rates.pak{area} (app.user_data.TestAreaTable) · 값 12개"
    assert "  _Scale" in out
    code, _out, err = edit(manager, capsys, "Rates", "set", "Rates.pak", "_Scale=3", "--no-deploy")
    assert code == 1 and "#해시까지 적어 주세요" in err

    code, out, _err = edit(manager, capsys, "Rates", "set", f"Rates.pak{area}", "_Scale=3", "_Center=1,2,4")
    assert code == 0 and "  _Scale: 1.5 → 3.0" in out and "  _Center: 1.0, 2.0, 3.0 → 1.0, 2.0, 4.0" in out
    assert "참고: 정수 값" not in out and f"참고: {rszdata.ONLINE_CAUTION}" in out
    after = fresh(manager)
    path, key = after.mod_entries(mod.id)["Rates.pak"]
    assert key == "edits/Rates.pak" and value(PakFile(path).read_hash(path_hash(AREA)), "_Scale") == "3.0"
    (placed,) = (game_dir / "pak_mods").glob("*.pak")  # applied
    assert value(PakFile(placed).read_hash(path_hash(AREA)), "_Center") == "1.0, 2.0, 4.0"
    code, out, _err = edit(manager, capsys, "Rates")
    assert f"  [수치]  Rates.pak{area}" in out.splitlines() and "          _Scale: 1.5 → 3.0" in out.splitlines()

    number = next(line.split(".")[0].strip() for line in edit(manager, capsys, "Rates", "values")[1].splitlines()
                  if area in line)
    code, out, _err = edit(manager, capsys, "Rates", "revert", number, "--no-deploy")
    assert code == 0 and f"  되돌림: Rates.pak{area} (값 2개)" in out
    assert fresh(manager).edit_summary(mod.id).empty
    code, _out, err = edit(manager, capsys, "Rates", "revert", number, "--no-deploy")
    assert code == 1 and "파일에는 수정한 값이 없습니다" in err


def test_scale_without_a_game_copy(manager, tmp_path, capsys):
    install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    code, _out, err = edit(manager, capsys, "Items", "scale", ITEM, "--times", "2", "--no-deploy")
    assert code == 1 and "오류: 게임 파일과 비교하지 못해 --times/--change를 쓸 수 없습니다" in err
    code, _out, err = edit(manager, capsys, "Items", "scale", ITEM, "--to", "7", "--no-deploy")
    assert code == 1 and "--field로 고르세요" in err
    code, out, _err = edit(manager, capsys, "Items", "scale", ITEM, "--to", "7", "--field", "*SellPrice", "--dry-run")
    assert code == 0 and [line for line in out.splitlines() if line.startswith("  _")] == [
        f"  {SELL.format(100)}: 30 → 7", f"  {SELL.format(101)}: 60 → 7", f"  {SELL.format(102)}: 90 → 7"]
    code, _out, err = edit(manager, capsys, "Items", "scale", ITEM, "--times", "x", "--no-deploy")
    assert code == 1 and "--times에는 숫자를 적어 주세요: x" in err


def test_scale_from_the_game_values(mlib, tmp_path, capsys):
    mod = install(mlib, tmp_path, "HR XP", {ITEM: mf.item_data(3)})
    code, out, _err = edit(mlib, capsys, "HR XP", "scale", ITEM, "--change", "0.5", "--field", "*SellPrice",
                           "--no-deploy")
    assert code == 0 and [line for line in out.splitlines() if line.startswith("  _")] == [
        f"  {SELL.format(100)}: 30 → 20", f"  {SELL.format(101)}: 60 → 35", f"  {SELL.format(102)}: 90 → 50"]
    assert "모드 수정을 저장했습니다: HR XP (바꾼 값 3개)" in out
    (item,) = fresh(mlib).edit_record(mod.id).items
    how = {c.at: c.how for c in item.members[0].changes}
    assert how[SELL.format(101)] == {"mode": "change", "k": 0.5, "game": "10"}
    code, out, _err = edit(mlib, capsys, "HR XP", "scale", ITEM, "--times", "2", "--field", "*SellPrice", "--no-deploy")
    assert code == 0 and f"  {SELL.format(101)}: 35 → 20" in out and "  이미 그 값이라 건너뛴 값 1개" in out
    code, out, _err = edit(mlib, capsys, "HR XP", "scale", ITEM, "--to", "9", "--field", "_Rare", "--no-deploy")
    assert code == 1  # (_Rare: the mod's values are the game's)
    code, out, _err = edit(mlib, capsys, "HR XP", "scale", ITEM, "--to", "99999999999", "--field", "*SellPrice",
                           "--no-deploy")
    assert code == 0 and f"  {SELL.format(100)}: 20 → 2147483647 (범위를 넘어 2147483647로 맞췄습니다)" in out
    data = fresh(mlib).mod_entries(mod.id)[ITEM][0].read_bytes()
    assert value(data, SELL.format(102)) == "2147483647"


def test_scale_refuses_a_decimal_comma(mlib, tmp_path, capsys):
    mod = install(mlib, tmp_path, "Shop", {ITEM: mf.item_data(rows=[mf.item_row(i, _SellPrice=100) for i in range(3)])})
    for flag, k in (("--times", "0,5"), ("--change", "1,5"), ("--to", "0,5"), ("--times", "1,5e3"), ("--to", "1,5e3"),
                    ("--to", "1,2,3")):
        code, _out, err = edit(mlib, capsys, "Shop", "scale", ITEM, flag, k, "--field", "*SellPrice", "--no-deploy")
        assert code == 1 and f"오류: {flag}에는 숫자를 적어 주세요: {k} (소수점은 .으로)" in err, (flag, k)
    assert nothing_written(mlib, mod.id)
    for flag, k, now in (("--times", "1,000", 10000), ("--to", "1,000", 1000), ("--times", "0.5", 5)):
        code, out, _err = edit(mlib, capsys, "Shop", "scale", ITEM, flag, k, "--field", "*SellPrice", "--dry-run")
        assert code == 0 and f"  {SELL.format(100)}: 100 → {now}" in out, (flag, k)  # (thousands separators: fine)


def test_scale_says_why_it_skipped_values(manager, tmp_path, capsys):
    install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    code, out, _err = edit(manager, capsys, "Items", "scale", ITEM, "--to", "5", "--field", "_Values[_ItemId=100].*",
                           "--dry-run")
    assert code == 0 and f"  {SELL.format(100)}: 30 → 5" in out
    assert "  건너뛴 값 1개 (켬이나 끔(true/false)이어야 합니다)" in out.splitlines()  # (the reason is a sentence)


def test_scale_from_the_game_values_only_on_rows_paired_by_a_key(tmp_path, capsys):
    """게임 값의 몇 배 / 모드가 바꾼 만큼의 몇 배 take a row's game value only from the row of the game's file with the
    same key: rows without one are paired by their place, and the mod may list them in another order."""
    zones = [mf.area("zone", 1.0, 2.0), mf.area("zone", 5.0, 6.0), mf.area("zone", 10.0, 20.0)]
    m = game_library(tmp_path, {AREA: mf.area_table(zones)})
    mine = [mf.area("zone", 12.0, 20.0), mf.area("zone", 5.0, 6.0), mf.area("zone", 1.0, 2.0)]  # (last first)
    mod = install(m, tmp_path, "Areas", {AREA: mf.area_table(mine, _Scale=3.0)})
    for flag in ("--times", "--change"):
        code, out, _err = edit(m, capsys, "Areas", "scale", AREA, flag, "2", "--field", "*_Min", "--no-deploy")
        assert code == 0 and not [line for line in out.splitlines() if line.startswith("  _")], flag
        assert "  게임과 행을 맞추지 못해 건너뛴 값 2개\n  (게임과 행을 맞추지 못한 값은 --to나 set으로 바꿀 수 있습니다)\n" in out
        assert out.endswith("바뀐 것이 없습니다.\n") and nothing_written(m, mod.id)
    code, out, _err = edit(m, capsys, "Areas", "scale", AREA, "--times", "3", "--field", "_Scale", "--dry-run")
    assert code == 0 and "  _Scale: 3.0 → 4.5\n" in out and "맞추지 못해" not in out  # (no table: its own value)
    code, out, _err = edit(m, capsys, "Areas", "scale", AREA, "--to", "3", "--field", "*_Min", "--dry-run")
    assert code == 0 and "  _Areas[0]._Min: 12.0 → 3.0\n  _Areas[2]._Min: 1.0 → 3.0\n" in out  # (값 정하기: any row)

    # rows with a key both files have: paired whatever their order
    named = [mf.area("a", 1.0, 2.0), mf.area("b", 5.0, 6.0), mf.area("c", 10.0, 20.0)]
    m = game_library(tmp_path / "keyed", {AREA: mf.area_table(named)})
    install(m, tmp_path, "Areas", {AREA: mf.area_table([mf.area("c", 12.0, 20.0), mf.area("b", 5.0, 6.0),
                                                        mf.area("a", 1.0, 2.0)])})
    code, out, _err = edit(m, capsys, "Areas", "scale", AREA, "--times", "2", "--field", "*_Min", "--dry-run")
    assert code == 0 and '  _Areas[_Name="c"]._Min: 12.0 → 20.0\n' in out and "맞추지 못해" not in out


# ---- 스크립트 설정 ---------------------------------------------------------------------------------------------
def test_config_files_set_and_revert(manager, tmp_path, game_dir, capsys):
    mod = install(manager, tmp_path, "NierUI", {LUA: mf.NIER_CONFIG_LUA})
    code, out, _err = edit(manager, capsys, "NierUI", "set", "Config.lua", "Config.UI_Scale=1.25", "--dry-run")
    assert code == 0 and out == "  Config.UI_Scale: 1.0 → 1.25\n(--dry-run: 저장하지 않았습니다)\n"
    code, out, _err = edit(manager, capsys, "NierUI", "set", "Config.lua", "Config.UI_Scale=1.25",
                           "Config.ShowMinimap=끔", 'Config.FontName=Noto=Sans')
    assert code == 0 and "  Config.ShowMinimap: true → false" in out
    assert "  Config.FontName: NotoSansKR → Noto=Sans" in out  # (the value starts after the address the file has)
    assert "참고: 보상" not in out  # (the online caution is for data files)
    text = (game_dir / LUA).read_bytes()
    assert b"Config.UI_Scale = 1.25 -- size of the whole HUD" in text and b"Config.ShowMinimap = false" in text
    code, out, _err = edit(manager, capsys, "NierUI", "values", "Config.lua")
    assert "  Config.UI_Scale     float    1.0 → 1.25" in out.splitlines()
    code, out, _err = edit(manager, capsys, "NierUI")
    assert f"  [설정]  {LUA}" in out.splitlines() and "          Config.UI_Scale: 1.0 → 1.25" in out.splitlines()
    code, out, _err = edit(manager, capsys, "NierUI", "revert", "Config.lua", "Config.UI_Scale")
    assert code == 0 and "  Config.UI_Scale: 1.25 → 1.0 (원래 값)" in out
    code, _out, err = edit(manager, capsys, "NierUI", "set", "Config.lua", "Config.Missing=1", "--no-deploy")
    assert code == 1 and "이 파일에 없는 주소입니다: Config.Missing" in err
    code, _out, err = edit(manager, capsys, "NierUI", "set", "Config.lua", "Config.UI_Scale=1", "--same", "--no-deploy")
    assert code == 1 and "--same은 데이터 파일(.user)에서만 쓸 수 있습니다." in err
    code, out, _err = edit(manager, capsys, "NierUI", "revert", "Config.lua")
    assert code == 0 and f"  되돌림: {LUA} (바꾼 값 2개)" in out
    assert (game_dir / LUA).read_bytes() == mf.NIER_CONFIG_LUA and fresh(manager).edit_summary(mod.id).empty


def test_revert_of_a_changed_game_copy_needs_yes(manager, tmp_path, game_dir, capsys):
    mod = install(manager, tmp_path, "NierUI", {LUA: mf.NIER_CONFIG_LUA})
    assert edit(manager, capsys, "NierUI", "set", "Config.lua", "Config.UI_Scale=1.5")[0] == 0
    touch_later(game_dir / LUA, b"-- saved by the script")
    code, _out, err = edit(manager, capsys, "NierUI", "set", "Config.lua", "Config.Opacity=0.5", "--no-deploy")
    assert code == 1 and textconfig.GAME_COPY_CHANGED in err
    for argv in (("revert", "Config.lua"), ("revert", "--all")):
        code, _out, err = edit(manager, capsys, "NierUI", *argv, "--no-deploy")
        assert code == 1 and f"오류: 게임 폴더의 다음 파일은 매니저가 넣은 뒤에 바뀌었습니다: {LUA}" in err
        assert "그래도 하려면 --yes를 붙이세요." in err
    assert fresh(manager).edit_summary(mod.id).values == 1
    code, out, _err = edit(manager, capsys, "NierUI", "revert", "Config.lua", "--yes")
    assert code == 0 and "모드 수정을 저장했습니다: NierUI (되돌린 수정 1개)" in out
    assert (game_dir / LUA).read_bytes() == mf.NIER_CONFIG_LUA


# ---- after an update ------------------------------------------------------------------------------------------
def test_install_reports_the_carry_over_and_resolve(manager, tmp_path, capsys):
    data = str(manager.data_dir)
    first = make_tree(tmp_path / "v1" / "Items", {ITEM: mf.item_data(3), TEX: "t"})
    assert main(["--data-dir", data, "install", str(first), "--no-deploy"]) == 0
    assert edit(manager, capsys, "Items", "set", ITEM, f"{SELL.format(100)}=1", f"{SELL.format(102)}=3",
                f"{BUY.format(102)}=7", "--no-deploy")[0] == 0
    assert edit(manager, capsys, "Items", "exclude", TEX, "--no-deploy")[0] == 0
    rows = [mf.item_row(0), mf.item_row(1), mf.item_row(2, _SellPrice=777, _BuyPrice=1), mf.item_row(3)]
    second = make_tree(tmp_path / "v2" / "Items", {ITEM: mf.item_data(rows=rows)})
    capsys.readouterr()
    assert main(["--data-dir", data, "install", str(second), "--replace", "--no-deploy"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["설치: Items  (루스, 1개 파일)",
                                "  모드 수정: 수정을 다시 적용했습니다: 수정 1개 · 확인할 값 2개 · 없어진 파일 1개",
                                f"    없어진 파일 (빼 두었던 것): {TEX}", "  확인하려면: edit Items"]

    code, out, _err = edit(manager, capsys, "Items")
    lines = out.splitlines()
    assert code == 0 and lines == [
        "Items: 바꾼 값 1개 (파일 1개) · 확인할 값 2개",
        "지난 업데이트: 수정을 다시 적용했습니다: 수정 1개 · 확인할 값 2개 · 없어진 파일 1개",
        f"  없어진 파일 (빼 두었던 것): {TEX}",
        f"  [수치]  {ITEM}", f"          {SELL.format(100)}: 30 → 1",
        f"        ✕ {SELL.format(102)}: 작성자가 바꾼 값: 90 → 777 (내 값 3)",
        f"        ✕ {BUY.format(102)}: 작성자가 바꾼 값: 180 → 1 (내 값 7)",
        "          (edit Items resolve ItemData.user.3 --every --keep|--take, 값마다 고르려면 --every 대신 주소)",
        "수정은 모든 프로필에 똑같이 쓰입니다."]
    code, out, _err = edit(manager, capsys, "Items")  # the update's report is shown once (the values to check stay)
    assert "지난 업데이트" not in out and out.splitlines()[0] == "Items: 바꾼 값 1개 (파일 1개) · 확인할 값 2개"
    assert fresh(manager).edit_record(fresh(manager).find("Items").id).notice == {}
    code, out, _err = edit(manager, capsys, "Items", "reapply")
    assert code == 1 and out == "다시 적용할 수정이 없습니다.\n확인할 값 2개 (확인: edit Items)\n"

    code, out, _err = edit(manager, capsys, "Items", "resolve", "ItemData.user.3", SELL.format(102), "--keep",
                           "--no-deploy")
    assert code == 0 and f"  {SELL.format(102)}: 내 값 유지 (777 → 3)" in out
    code, _out, err = edit(manager, capsys, "Items", "resolve", ITEM, SELL.format(102), "--take", "--no-deploy")
    assert code == 1 and f"확인할 값이 아닙니다: {SELL.format(102)}" in err
    code, out, _err = edit(manager, capsys, "Items", "resolve", ITEM, "--every", "--take", "--no-deploy")
    assert code == 0 and f"  {BUY.format(102)}: 작성자 값 쓰기 (1)" in out
    after = fresh(manager)
    mod = after.find("Items")
    assert after.edit_issues(mod.id) == [] and after.edit_summary(mod.id).values == 2
    data = after.mod_entries(mod.id)[ITEM][0].read_bytes()
    assert (value(data, SELL.format(102)), value(data, BUY.format(102))) == ("3", "1")
    code, _out, err = edit(manager, capsys, "Items", "resolve", ITEM, "--every", "--keep", "--no-deploy")
    assert code == 1 and "확인할 값이 없습니다" in err


def test_revert_of_a_value_to_check_says_what_the_game_gets(manager, tmp_path, game_dir, capsys):
    """A value to check after an update was never applied: undoing it leaves the author's new value (or nothing,
    for a row the update took away), not the old version's value."""
    data = str(manager.data_dir)
    assert main(["--data-dir", data, "install", str(make_tree(tmp_path / "v1" / "Items", {ITEM: mf.item_data(3)}))]) == 0
    assert edit(manager, capsys, "Items", "set", ITEM, f"{SELL.format(101)}=5", f"{SELL.format(102)}=3",
                f"{BUY.format(100)}=7")[0] == 0
    rows = [mf.item_row(0), mf.item_row(1, _SellPrice=444)]  # (row 102 is gone)
    second = make_tree(tmp_path / "v2" / "Items", {ITEM: mf.item_data(rows=rows)})
    assert main(["--data-dir", data, "install", str(second), "--replace"]) == 0
    code, out, _err = edit(manager, capsys, "Items", "revert", ITEM, SELL.format(101), SELL.format(102),
                           BUY.format(100))
    assert code == 0 and out.splitlines()[:3] == [f"  {SELL.format(101)}: 작성자 값 쓰기 (444)",
                                                  f"  {SELL.format(102)}: 작성자 값 쓰기 (내 수정을 지움)",
                                                  f"  {BUY.format(100)}: 7 → 60 (원래 값)"]
    assert "모드 수정을 저장했습니다: Items (되돌린 수정 3개)" in out
    assert value((game_dir / ITEM).read_bytes(), SELL.format(101)) == "444"
    assert value((game_dir / ITEM).read_bytes(), BUY.format(100)) == "60"


def test_install_reports_an_unreadable_record_it_kept_aside(manager, tmp_path, capsys):
    mod = install(manager, tmp_path, "Items", {TEX: "t"})
    write(manager.mod_dir(mod.id) / EDITS_RECORD, "{broken")
    second = make_tree(tmp_path / "v2" / "Items", {TEX: "t2"})
    capsys.readouterr()
    assert main(["--data-dir", str(manager.data_dir), "install", str(second), "--replace", "--no-deploy"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("설치: Items") and lines[1].startswith("  모드 수정: 수정 기록을 읽지 못해")
    code, out, _err = edit(manager, capsys, "Items")
    assert code == 0 and out.splitlines()[1].startswith("지난 업데이트: 수정 기록을 읽지 못해")
    assert len(list(manager.mod_dir(mod.id).glob(EDITS_RECORD + ".bad-*"))) == 1


def test_reapply_after_a_hand_edit(manager, tmp_path, capsys):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3)})
    assert edit(manager, capsys, "Items", "set", ITEM, f"{SELL.format(100)}=1", "--no-deploy")[0] == 0
    touch_later(manager.files_dir(mod.id) / ITEM, mf.item_data(4))  # (the stored file changed by hand)
    code, out, _err = edit(manager, capsys, "Items")
    assert out.splitlines()[:2] == ["Items: 바꾼 값 1개 (파일 1개) · 다시 적용 대기 1개", f"  [수치]  {ITEM} (다시 적용 대기)"]
    assert "          (다시 적용: edit Items reapply ItemData.user.3)" in out.splitlines()
    code, out, _err = edit(manager, capsys, "Items", "reapply", "--dry-run")
    assert code == 0 and f"  다시 적용: {ITEM}" in out and "(--dry-run: 저장하지 않았습니다)" in out
    assert fresh(manager).edits_pending() == [mod.id]
    code, out, _err = edit(manager, capsys, "Items", "reapply", "--no-deploy")
    assert code == 0 and "모드 수정을 저장했습니다: Items (다시 적용한 파일 1개)" in out
    after = fresh(manager)
    assert after.edits_pending() == []
    assert value(after.mod_entries(mod.id)[ITEM][0].read_bytes(), SELL.format(100)) == "1"
    code, out, _err = edit(manager, capsys, "Items", "reapply", "--no-deploy")
    assert code == 0 and out == "다시 적용할 수정이 없습니다.\n"
    code, out, _err = edit(manager, capsys, "Items", "reapply", "ItemData.user.3", "--no-deploy")  # asked: built again
    assert code == 0 and f"  다시 적용: {ITEM}" in out
    code, _out, err = edit(manager, capsys, "Items", "reapply", "Other.user.3", "--no-deploy")
    assert code == 1 and "수정한 파일 가운데 맞는 것이 없습니다: Other.user.3" in err


def test_an_unreadable_record(manager, tmp_path, capsys):
    mod = install(manager, tmp_path, "Items", {ITEM: mf.item_data(3), TEX: "t"})
    write(manager.mod_dir(mod.id) / EDITS_RECORD, "{broken")
    code, out, _err = edit(manager, capsys, "Items")
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("Items: 모드 수정 기록을 읽지 못했습니다: ")
    assert lines[1].startswith("  모드 수정 기록을 읽지 못해 쓰지 않고 있습니다: ")
    assert lines[2] == "기록을 옆으로 옮기려면: edit Items clear-record --yes · 모든 수정을 되돌리려면: edit Items revert --all"
    for argv in (("exclude", TEX), ("set", ITEM, f"{SELL.format(100)}=1"), ("reapply",)):
        code, _out, err = edit(manager, capsys, "Items", *argv, "--no-deploy")
        assert code == 1 and "모드 수정 기록을 읽지 못해 고칠 수 없습니다" in err, argv
    code, _out, err = edit(manager, capsys, "Items", "clear-record")
    assert code == 1 and "--yes를 붙이세요" in err
    assert (manager.mod_dir(mod.id) / EDITS_RECORD).read_text() == "{broken"
    code, out, _err = edit(manager, capsys, "Items", "clear-record", "--yes")
    assert code == 0 and "읽지 못한 기록은 모드 폴더의 edits.json.bad-<시각>으로 옮겨 두었습니다." in out
    (bad,) = manager.mod_dir(mod.id).glob(EDITS_RECORD + ".bad-*")
    assert bad.read_text() == "{broken" and not (manager.mod_dir(mod.id) / EDITS_RECORD).exists()
    code, _out, err = edit(manager, capsys, "Items", "clear-record", "--yes")
    assert code == 1 and "수정 기록을 읽을 수 있어 옮길 필요가 없습니다" in err
    # 모두 원래대로 also sets an unreadable record aside
    write(manager.mod_dir(mod.id) / EDITS_RECORD, json.dumps({"format": 99}))
    code, out, _err = edit(manager, capsys, "Items", "revert", "--all", "--no-deploy")
    assert code == 0 and "  (읽지 못한 기록은 edits.json.bad-<시각>으로 옮겨 둡니다)" in out
    assert len(list(manager.mod_dir(mod.id).glob(EDITS_RECORD + ".bad-*"))) == 2
    code, out, _err = edit(manager, capsys, "Items", "revert", "--all", "--no-deploy")
    assert code == 0 and out == "되돌릴 수정이 없습니다.\n"


# ---- materials ------------------------------------------------------------------------------------------------
def test_materials_repair_and_restore_re_apply_the_edits(mlib, tmp_path, capsys):
    mod = install(mlib, tmp_path, "Old", {ITEM: mf.stale_item_data(3)})
    data = str(mlib.data_dir)

    def materials(*argv):
        capsys.readouterr()
        code = main(["--data-dir", data, "materials", *argv, "Old", "--no-deploy"])
        return code, capsys.readouterr().out

    code, _out, err = edit(mlib, capsys, "Old", "set", ITEM, f"{SELL.format(102)}=4242", "--no-deploy")
    assert code == 1 and rszdata.STALE in err  # (a stale file is changed on its repair)
    assert materials("repair")[0] == 0
    assert edit(mlib, capsys, "Old", "set", ITEM, f"{SELL.format(102)}=4242", "--no-deploy")[0] == 0
    assert fresh(mlib).edit_record(mod.id).items[0].base.key == "matfix/" + ITEM

    code, out = materials("restore")  # the repair goes: the edit is built again on the stored file (and waits)
    assert code == 0 and "모드 수정을 다시 적용했습니다: Old · 다시 적용하지 못한 파일 1개" in out
    (item,) = fresh(mlib).edit_record(mod.id).items
    assert item.base.key == ITEM and item.members[0].pending and keys(fresh(mlib), mod.id)[ITEM] == ITEM

    code, out = materials("repair")  # repaired again: built on the new repair
    assert code == 0 and out.splitlines()[-1] == "모드 수정을 다시 적용했습니다: Old"
    after = fresh(mlib)
    (item,) = after.edit_record(mod.id).items
    assert item.base.key == "matfix/" + ITEM and not item.members[0].pending and after.edit_issues(mod.id) == []
    assert value(after.mod_entries(mod.id)[ITEM][0].read_bytes(), SELL.format(102)) == "4242"

    # not while 원인 모드 찾기 runs (a check is allowed then; the edits wait for the search to end)
    assert materials("restore")[0] == 0
    touch_later(after.files_dir(mod.id) / ITEM, mf.stale_item_data(4))
    write(mlib.data_dir / bisect.SESSION_FILE, json.dumps({"format": bisect.SESSION_FORMAT}))
    code, out = materials("check")
    assert "원인 모드 찾기 중이라 모드 수정은 다시 적용하지 않았습니다: Old" in out
    assert fresh(mlib).edits_pending() == [mod.id]
