"""실행 전 점검: the fast checks of the game folder, the slow part (the last session's records, loose
files the manager did not place) and "같은 내용이면 다시 묻지 않기" (synthetic folders, logs, lists and
paks written here; no real user data)."""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from mhwmm import crashes, crashlog, precheck
from mhwmm.matfix.pak import path_hash, write_pak
from mhwmm.precheck import Background, Facts, ModState
from mhwmm.refw import Build, Status

from conftest import make_zip, write

DAY = 86400
LOOSE = crashlog.LOOSE_FILES_KEY
PAKDIR = crashlog.PAK_DIRECTORY_KEY
AVP = "GameDesign/Equip/_Prefab/Armor/Male/003/001/003_001_avp.user"  # as REFramework names it
COMMIT = "d052e8fd3fd41d0a1a33394fcf6e6c19be39b1ee"


def kinds(report):
    return [i.kind for i in report.issues]


def issue(report, kind):
    found = [i for i in report.issues if i.kind == kind]
    assert len(found) == 1, (kind, kinds(report))
    return found[0]


def age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


@pytest.fixture
def game(tmp_path) -> Path:
    """A game folder as Steam leaves it, with REFramework put in by hand a while ago (before the
    game's files were last written, so "behind after an update" rules can fire when asked to)."""
    game = tmp_path / "MonsterHunterWilds"
    write(game / "MonsterHunterWilds.exe", "exe")
    write(game / "re_chunk_000.pak", "base")
    write(game / "re_chunk_000.pak.sub_000.pak", "sub")
    for name in ("steam_api64.dll", "CrashHandler.dll", "nvngx_dlss.dll", "sl.interposer.dll", "DStorageCore.dll",
                 "amd_fidelityfx_upscaler_dx12.dll"):
        write(game / name, "MZ")
    write(game / "dinput8.dll", f"MZ ... {COMMIT} ...")
    age(game / "dinput8.dll", 600)  # (within the slack of the game's files: the same install)
    return game


def mod(mod_id, name=None, enabled=True, kinds=(), **kw):
    return ModState(mod_id, name or mod_id.upper(), enabled, kw.pop("stamp", f"2026-10-01|{mod_id}"),
                    frozenset(kinds), **kw)


def facts(game, mods=(), deployed=None, **kw):
    kw.setdefault("appdata", "")  # (never the real %APPDATA%)
    return Facts(game, list(mods), dict(deployed or {}), game_version=kw.pop("game_version", "v1"), **kw)


def entry(path, mod_id, pak=False, src_mtime=None):
    return {"path": path, "mod": mod_id, "src": path, "src_size": 1,
            "src_mtime": int((src_mtime if src_mtime is not None else time.time() - 30 * DAY) * 1e9),
            "size": 1, "mtime": 1, "pak": pak}


def manifest(*items):
    return {e["path"].casefold(): e for e in items}


def config(game, text: str) -> Path:
    path = game / crashlog.CONFIG_FILE
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    return path


# ---- the mods' own problems -------------------------------------------------------------------------
def test_mod_problems_are_the_main_lists_reasons():
    stuck = [{"label": "a/x.user.3", "kind": "user", "reason": "r"},
             {"label": "b.pfb.18", "kind": "pfb", "reason": "r"}]
    mods = [mod("a", needs_option=True, missing=["Lib", "CatLib"], hidden=True),
            mod("b", material_state="stale", stuck=stuck),
            mod("c", hidden=True), mod("d", enabled=False, needs_option=True, hidden=True), mod("e")]
    assert precheck.mod_problems(mods) == {
        "a": ["옵션을 하나도 켜지 않아 아무 파일도 적용되지 않습니다", "필요한 모드가 없거나 꺼져 있습니다: Lib, CatLib",
              "파일이 모두 다른 모드에 밀려 아무것도 적용되지 않습니다"],
        "b": ["재질이나 .user·.pfb 파일이 현재 게임과 맞지 않습니다 (오른쪽 클릭 → 재질 복구)",
              "현재 게임과 맞지 않지만 고칠 수 없는 .pfb·.user 파일 2개이 있어 게임이 멈출 수 있습니다 (정보 탭 참고)"],
        "c": ["파일이 모두 다른 모드에 밀려 아무것도 적용되지 않습니다"],
    }
    assert list(precheck.mod_problems(mods)) == ["a", "b", "c"]  # (in load order, the hidden ones after)
    assert precheck.stuck_summary(stuck) == ".pfb·.user 파일 2개"


def test_mod_issues_are_grouped_by_problem():
    one = precheck.mod_issues([mod("a", "갑옷 A", material_state="stale"),
                               mod("b", enabled=False, material_state="stale")])
    (stale,) = one
    assert stale.kind == "material" and stale.level == "high" and stale.action == "matfix" and stale.mods == ["a"]
    assert stale.text.startswith("'갑옷 A'의 재질·.user·.pfb 파일이 현재 게임과 맞지 않습니다 → 재질 복구를 해 보세요")
    assert stale.items == ["a|2026-10-01|a"]

    many = precheck.mod_issues([mod(i, material_state="stale") for i in "abcde"]
                               + [mod("f", missing=["Lib"]), mod("g", missing=["X", "Y"]), mod("h", needs_option=True),
                                  mod("i", hidden=True), mod("j", material_state="unchecked", after_update=True),
                                  mod("k", material_state="unchecked")])
    by_kind = {i.kind: i for i in many}
    assert by_kind["material"].text.startswith("재질·.user·.pfb 파일이 현재 게임과 맞지 않는 모드 5개: 'A', 'B', 'C' 외 2개")
    assert by_kind["material"].mods == list("abcde")
    req = by_kind["requirement"]
    assert req.level == "medium" and req.detail == ["'F': Lib", "'G': X, Y"] and req.items == ["f|Lib", "g|X|Y"]
    assert by_kind["option"].level == by_kind["hidden"].level == "low"
    # not checked since the game was updated: asked about; never checked (a new mod): worth knowing only
    assert by_kind["unchecked"].level == "medium" and by_kind["unchecked"].action == "matfix_check"
    assert by_kind["unchecked"].mods == ["j"] and by_kind["unchecked"].text.startswith("게임이 업데이트된 뒤 'J'의")
    assert by_kind["unchecked_new"].level == "low" and by_kind["unchecked_new"].mods == ["k"]
    assert not {"unchecked", "unchecked_new"} & {i.kind for i in precheck.mod_issues(
        [mod("j", material_state="unchecked", after_update=True), mod("k", material_state="unchecked")], False)}
    # while the mods' own check runs: the updated ones are said to be in check (it finds what no longer fits)
    checking = precheck.mod_issues([mod("j", material_state="unchecked", after_update=True),
                                    mod("k", material_state="unchecked")], checking=True)
    assert [(i.kind, i.level, i.mods, i.action) for i in checking] == [("unchecked", "medium", ["j"], "show_mods")]
    assert "점검하는 중입니다 → 점검이 끝난 뒤 실행하면" in checking[0].text and checking[0].items == ["j|2026-10-01|j"]


def test_stuck_files_are_listed_by_name():
    stuck = [{"label": f"natives/STM/x/{n}.user.3", "kind": "user"} for n in "abcd"]
    (found,) = precheck.mod_issues([mod("a", "Lepus", material_state="current", stuck=stuck)])
    assert found.kind == "stuck" and found.level == "high"
    assert "'Lepus'에 현재 게임과 맞지 않지만 고칠 수 없는 .user 파일 4개이 있어" in found.text
    assert found.detail == ["'Lepus': a.user.3, b.user.3, c.user.3 외 1개"]
    (found,) = precheck.mod_issues([mod("a", stuck=stuck), mod("b", stuck=stuck[:1])])
    assert found.detail == ["'A': a.user.3, b.user.3, c.user.3 외 1개", "'B': a.user.3"]


# ---- REFramework and its settings ----------------------------------------------------------------------
def test_loose_file_loading_off_is_found_with_a_fix(game):
    path = config(game, f"{LOOSE}=false\nScriptRunner_LogToDisk=true\n")
    mods = [mod("r", kinds=["reframework"], refw=True), mod("a", "갑옷", kinds=["loose"])]
    report = precheck.check(facts(game, mods))
    found = issue(report, "config_loose")
    assert found.level == "high" and found.action == "fix_config" and found.fix == (LOOSE, "true")
    assert found.mods == ["a"] and found.detail[0] == f"{LOOSE}=false"
    assert "루스 파일(natives) 모드 1개가 적용되지 않습니다" in found.text
    assert "LooseFileLoader" in precheck.fix_question(found) and "게임이 꺼져 있을 때만" in precheck.fix_question(found)
    # the fix as the manager applies it: REFramework reads it as true, the rest of the file as it was
    assert crashes.set_refw_option(game, *found.fix)
    assert path.read_bytes() == f"{LOOSE}=true\r\nScriptRunner_LogToDisk=true\r\n".encode()
    assert "config_loose" not in kinds(precheck.check(facts(game, mods)))


@pytest.mark.parametrize("text, found", [
    ("", True),  # no line: off by default
    (f"{LOOSE}=True\n", True),  # REFramework takes only "true" and "false"; anything else is the default
    (f"{LOOSE}=true \n", True),
    (f"{LOOSE}=true\n", False),
    (f"{LOOSE}=false\n{LOOSE}=true\n", False),  # the last line wins
])
def test_settings_are_read_as_reframework_reads_them(game, text, found):
    config(game, text)
    report = precheck.check(facts(game, [mod("a", kinds=["loose"])]))
    assert ("config_loose" in kinds(report)) is found


def test_no_settings_file_yet(game):
    report = precheck.check(facts(game, [mod("a", kinds=["loose"]), mod("p", kinds=["pak"])]))
    found = issue(report, "config_loose")
    assert found.text.startswith("REFramework 설정 파일이 아직 없습니다") and found.fix == (LOOSE, "true")
    assert found.detail == [f"{LOOSE} 줄 없음 (기본값 false)"]
    assert "config_pak" not in kinds(report) and "config_faulty" not in kinds(report)  # (on by default)


def test_pak_folder_loading_off_and_crash_records_off(game):
    config(game, f"{PAKDIR}=false\n{precheck.FAULTY_KEY}=false\n")
    deployed = manifest(entry("pak_mods/001_A.pak", "p", pak=True))
    write(game / "pak_mods/001_A.pak", "pak")
    report = precheck.check(facts(game, [mod("p", kinds=["pak"])], deployed))
    pak = issue(report, "config_pak")
    assert pak.level == "high" and pak.fix == (PAKDIR, "true") and pak.mods == ["p"]
    faulty = issue(report, "config_faulty")
    assert faulty.level == "low" and faulty.fix == (precheck.FAULTY_KEY, "true")
    # nothing needs pak_mods: its setting does not matter
    assert "config_pak" not in kinds(precheck.check(facts(game, [mod("a", kinds=["loose"])])))


def test_mods_in_the_game_folder_count_even_when_off_now(game):
    config(game, f"{LOOSE}=false\n")
    deployed = manifest(entry("natives/STM/a.tex", "a"))
    report = precheck.check(facts(game, [mod("a", enabled=False, kinds=["loose"])], deployed))
    assert issue(report, "config_loose").mods == ["a"]


def test_settings_in_appdata_are_used_when_the_game_folder_has_none(game, tmp_path):
    appdata = tmp_path / "Roaming"
    write(appdata / "REFramework" / "MonsterHunterWilds" / crashlog.CONFIG_FILE, f"{LOOSE}=true\r\n")
    report = precheck.check(facts(game, [mod("a", kinds=["loose"])], appdata=str(appdata)))
    assert "config_loose" not in kinds(report)
    assert "config_loose" in kinds(precheck.check(facts(game, [mod("a", kinds=["loose"])], appdata="")))


def test_reframework_missing_or_off(game):
    (game / "dinput8.dll").unlink()
    config(game, f"{LOOSE}=false\n")
    report = precheck.check(facts(game, [mod("a", kinds=["loose"]), mod("s", kinds=["script"])]))
    found = issue(report, "refw_missing")
    assert found.level == "medium" and found.action == "install_refw" and found.mods == ["a", "s"]
    assert "config_loose" not in kinds(report)  # (REFramework's settings matter once it is there)
    # the REFramework mod is off: turning it on helps
    mods = [mod("r", "REFramework", enabled=False, kinds=["reframework"], refw=True), mod("a", kinds=["loose"])]
    found = issue(precheck.check(facts(game, mods)), "refw_off")
    assert found.action == "enable_refw" and found.mods == ["r", "a"]
    assert found.detail == ["REFramework 모드: 'REFramework'"]
    # nothing needs it: nothing to say
    assert kinds(precheck.check(facts(game, [mod("x", kinds=["other"])]))) == []


def test_reframework_off_but_one_put_in_by_hand_comes_back(game):
    mods = [mod("r", enabled=False, kinds=["reframework"], refw=True), mod("a", kinds=["script"])]
    backups = {"dinput8.dll": {"path": "dinput8.dll", "backup": "dinput8.dll"}}
    (game / "dinput8.dll").unlink()
    assert "refw_off" not in kinds(precheck.check(facts(game, mods, backups=backups)))
    write(game / "dinput8.dll", "MZ")  # one by hand, not the mod's
    assert "refw_off" not in kinds(precheck.check(facts(game, mods)))
    deployed = manifest(entry("dinput8.dll", "r"))  # the mod's, which the next apply removes
    assert "refw_off" in kinds(precheck.check(facts(game, mods, deployed)))


def builds(*numbers):
    return [Build(f"nightly-{n:05d}-abcdef{n}", n, f"abcdef{n}", "u", 1, None, "p") for n in numbers]


def test_reframework_behind_after_a_game_update(game):
    age(game / "dinput8.dll", 20 * DAY)  # the game was updated since
    mods = [mod("a", kinds=["loose"])]
    config(game, f"{LOOSE}=true\n")
    hint = issue(precheck.check(facts(game, mods)), "refw_old")
    assert hint.level == "low" and hint.action == "update_refw" and hint.detail[0].startswith("dinput8.dll: ")
    known = builds(1440, 1432)
    behind = Status(True, known[0], known[1], known)
    found = issue(precheck.check(facts(game, mods, refw_status=behind)), "refw_old")
    assert found.level == "medium" and "최신보다 8개 이전 빌드(nightly 01432)" in found.text
    assert "최신: nightly 01440" in found.detail
    unknown = Status(True, known[0], None, known)
    assert "최근 빌드 목록에 없고" in issue(precheck.check(facts(game, mods, refw_status=unknown)), "refw_old").text
    latest = Status(True, known[0], known[0], known)
    assert "refw_old" not in kinds(precheck.check(facts(game, mods, refw_status=latest)))
    # put in after the update: not older than the game, whatever the status
    age(game / "dinput8.dll", 0)
    assert "refw_old" not in kinds(precheck.check(facts(game, mods, refw_status=behind)))


def test_paks_put_in_by_hand_are_not_a_game_update(game, tmp_path):
    """Only the game's exe (or Steam's record) tells when the game was updated, not a patch pak another
    program or an old guide put in the game folder."""
    for name in ("MonsterHunterWilds.exe", "re_chunk_000.pak", "re_chunk_000.pak.sub_000.pak"):
        age(game / name, 40 * DAY)
    age(game / "dinput8.dll", 30 * DAY)
    write(game / "re_chunk_000.pak.sub_000.pak.patch_005.pak", "mod")
    age(game / "re_chunk_000.pak.sub_000.pak.patch_005.pak", DAY)
    known = builds(1440, 1437)
    behind = Status(True, known[0], known[1], known)
    mods = [mod("a", kinds=["loose"])]
    config(game, f"{LOOSE}=true\n")
    assert "refw_old" not in kinds(precheck.check(facts(game, mods, refw_status=behind)))
    assert precheck.game_updated(game) == pytest.approx(time.time() - 40 * DAY, abs=60)
    # no exe to look at: Steam's record of the last update, else the base paks
    steam = tmp_path / "steamapps" / "common" / "MonsterHunterWilds"
    write(steam / "re_chunk_000.pak", "base")
    age(steam / "re_chunk_000.pak", 50 * DAY)
    assert precheck.game_updated(steam) == pytest.approx(time.time() - 50 * DAY, abs=60)
    write(tmp_path / "steamapps" / "appmanifest_2246340.acf", '"AppState"\n{\n\t"LastUpdated"\t\t"1792000000"\n}\n')
    assert precheck.game_updated(steam) == 1792000000


def test_reframework_age_hint_needs_mods_that_use_it(game):
    age(game / "dinput8.dll", 20 * DAY)
    assert kinds(precheck.check(facts(game, [mod("x", kinds=["other"])]))) == []


# ---- the game folder ----------------------------------------------------------------------------------
def test_pak_count_is_a_soft_estimate(game):
    deployed = {}
    for n in range(1, precheck.PAK_COUNT_WARN):
        rel = f"pak_mods/{n:03d}_Mod{n}.pak"
        write(game / rel, "p")
        deployed.update(manifest(entry(rel, f"m{n}", pak=True)))
    assert "pak_count" not in kinds(precheck.check(facts(game, [], deployed)))
    write(game / "pak_mods/sub/extra.pak", "p")  # one more, by hand, in a subfolder (REFramework reads those too)
    report = precheck.check(facts(game, [], deployed))
    found = issue(report, "pak_count")
    assert found.level == "medium" and "PAK이 80개 있습니다" in found.text and "추정치" in found.text
    assert found.detail == ["매니저가 넣은 PAK 79개, 넣지 않은 PAK 1개"] and found.items == ["8"]
    assert issue(report, "pak_unknown").items == ["pak_mods/sub/extra.pak"]


def test_paks_the_manager_did_not_place(game):
    deployed = manifest(entry("pak_mods/001_Armor.pak", "a", pak=True))
    for rel in ("pak_mods/001_Armor.pak", "pak_mods/zz_hand.pak", "pak_mods/old/005_Old.pak", "pak_mods/BIG.PAK",
                "pak_mods/001_Armor.pak.mhwmm-tmp", "pak_mods/readme.txt"):
        write(game / rel, "p")
    report = precheck.check(facts(game, [mod("a", kinds=["pak"])], deployed))
    found = issue(report, "pak_unknown")
    assert found.level == "medium" and found.folder == "pak_mods" and found.action == "open_folder"
    assert "매니저가 넣지 않은 PAK 2개('005_Old.pak', 'zz_hand.pak')가 있습니다" in found.text
    assert found.detail[:2] == ["pak_mods/old/005_Old.pak", "pak_mods/zz_hand.pak"]
    assert "예전에 매니저가 넣은 것일 수 있습니다" in found.detail[-1]  # (named the manager's way)
    case = issue(report, "pak_case")
    assert case.level == "low" and case.detail == ["pak_mods/BIG.PAK"]
    write(game / "pak_mods/zz_hand.pak", "p")
    (game / "pak_mods/old/005_Old.pak").unlink()
    assert "'zz_hand.pak'이 있습니다" in issue(precheck.check(facts(game, [], deployed)), "pak_unknown").text


def test_dlls_in_the_game_folder(game):
    for name in ("dxgi.dll", "ReShade.ini", "openxr_loader.dll", "openvr_api.dll", "version.dll", "mystery.dll",
                 "plugin.asi", "sl.newthing.dll", "nvngx_dlssx.dll", "winmm.dll"):
        write(game / name, "MZ")
    (game / "reshade-shaders").mkdir()
    deployed = manifest(entry("openvr_api.dll", "v"), entry("winmm.dll", "w"))
    report = precheck.check(facts(game, [mod("v", "VR 모드"), mod("w")], deployed))
    by_hand, of_mod = [i for i in report.issues if i.kind == "dll_vr"]
    assert by_hand.level == "medium" and by_hand.action == "open_folder" and by_hand.items == ["openxr_loader.dll"]
    assert "VR 파일(openxr_loader.dll)이 있습니다" in by_hand.text and "VR을 쓰지 않으면 빼세요" in by_hand.text
    # a mod places it: taking it out by hand does not last (the next apply puts it back)
    assert of_mod.level == "medium" and of_mod.action == "show_mods" and of_mod.mods == ["v"]
    assert "모드 'VR 모드'이(가) 게임 폴더에 REFramework의 VR 파일(openvr_api.dll)을 넣습니다" in of_mod.text
    assert "그 모드를 끄거나 VR 파일 없이 다시 설치하세요" in of_mod.text
    proxy = issue(report, "dll_proxy")
    assert proxy.level == "medium" and proxy.detail == ["dxgi.dll", "version.dll"] and "(ReShade 파일로 보임)" in proxy.text
    other = issue(report, "dll_other")  # (the game's own and its upscalers' names are left alone: updates add some)
    assert other.level == "low" and other.detail == ["mystery.dll", "plugin.asi"]
    # REFramework's zip added as a mod: the manager's own REFramework has dinput8.dll only
    refw = [mod("r", "REFramework", kinds=["reframework"], refw=True)]
    (game / "openxr_loader.dll").unlink()
    found = issue(precheck.check(facts(game, refw, manifest(entry("openvr_api.dll", "r")))), "dll_vr")
    assert found.level == "low" and found.action == "update_refw" and found.mods == ["r"]
    assert "REFramework 창에서 다시 설치하세요" in found.text
    (game / "version.dll").unlink()
    for name in ("ReShade.ini",):
        (game / name).unlink()
    (game / "reshade-shaders").rmdir()
    proxy = issue(precheck.check(facts(game, [], deployed)), "dll_proxy")
    assert "'dxgi.dll'이 있습니다(ReShade·OptiScaler 같은 프로그램일 수 있음)" in proxy.text and proxy.detail == []


def test_reframework_put_in_by_hand_is_not_a_stray_dll(game):
    assert kinds(precheck.check(facts(game))) == []


def test_plugins_and_scripts_the_manager_did_not_place(game):
    deployed = manifest(entry("reframework/autorun/mine.lua", "s"), entry("reframework/plugins/Mine.dll", "s"))
    for rel in ("reframework/autorun/mine.lua", "reframework/autorun/hand.lua", "reframework/autorun/lib/module.lua",
                "reframework/autorun/data.json", "reframework/plugins/Mine.dll", "reframework/plugins/Hand.dll",
                "reframework/plugins/sub/Deep.dll", "reframework/data/x.lua"):
        write(game / rel, "x")
    report = precheck.check(facts(game, [mod("s", kinds=["script"])], deployed))
    assert issue(report, "plugin_unmanaged").detail == ["reframework/plugins/Hand.dll"]
    found = issue(report, "script_unmanaged")
    assert found.level == "low" and found.detail == ["reframework/autorun/hand.lua"]  # (only those REFramework runs)
    assert found.folder == "reframework/autorun"


def test_folder_names_in_any_case(game):
    write(game / "REFramework/Plugins/Hand.dll", "x")
    report = precheck.check(facts(game))
    assert issue(report, "plugin_unmanaged").detail == ["REFramework/Plugins/Hand.dll"]


def test_deploy_check_problems(game):
    report = precheck.check(facts(game, verify=["없어진 파일: natives/STM/a.tex", "없어진 파일: natives/STM/b.tex"]))
    found = issue(report, "verify")
    assert found.level == "medium" and found.action == "apply"
    assert found.text == "게임 폴더의 모드 파일에 문제가 있습니다: 없어진 파일: natives/STM/a.tex 외 1개 → 다시 적용하세요."


def test_without_a_game_folder_only_the_mods_are_checked(tmp_path):
    report = precheck.check(Facts(None, [mod("a", material_state="stale", kinds=["loose"])]))
    assert kinds(report) == ["material"] and report.problems == {"a": [precheck.mod_problems(
        [mod("a", material_state="stale")])["a"][0]]}
    assert kinds(precheck.check(Facts(tmp_path / "gone", [mod("a", kinds=["loose"])]))) == []


def test_one_check_failing_leaves_the_others(game, monkeypatch, caplog):
    write(game / "dxgi.dll", "MZ")
    write(game / "pak_mods/hand.pak", "p")

    def broken(*args):
        raise PermissionError("denied")

    monkeypatch.setattr(precheck, "scan_root", broken)
    monkeypatch.setattr(precheck, "read_config", broken)
    report = precheck.check(facts(game, [mod("a", material_state="stale", kinds=["loose"])]))
    assert {"material", "pak_unknown", "config_loose"} <= set(kinds(report)) and "dll_proxy" not in kinds(report)
    assert "실행 전 점검 중 일부를 하지 못했습니다" in caplog.text


def test_the_fast_part_reads_no_paks_and_no_records(game, monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("the fast part must not read this")

    import mhwmm.matfix.pak as pakmod

    monkeypatch.setattr(pakmod.PakFile, "__init__", forbidden)
    monkeypatch.setattr(crashes, "sessions", forbidden)
    monkeypatch.setattr(precheck, "check_materials", forbidden)
    deployed = {}
    for n in range(300):
        rel = f"natives/STM/Art/mod{n % 30}/file{n}.tex.241106027"
        write(game / rel, "t")
        deployed.update(manifest(entry(rel, "a")))
    for n in range(100):
        write(game / f"pak_mods/{n:03d}_x.pak", "p")
    started = time.perf_counter()
    report = precheck.check(facts(game, [mod("a", kinds=["loose"])], deployed), Background(game_version="v1"))
    assert time.perf_counter() - started < 2.0  # (well under a second on a PC; generous for slow CI disks)
    assert "pak_count" in kinds(report) and report.background == "current"


# ---- the report and "같은 내용이면 다시 묻지 않기" --------------------------------------------------------------
def test_report_title_lines_and_signature(game):
    config(game, f"{LOOSE}=false\n")
    write(game / "mystery.dll", "MZ")
    report = precheck.check(facts(game, [mod("a", kinds=["loose"]), mod("b", missing=["Lib"])]))
    assert report.title() == "실행 전 점검: 확인할 것 2개"
    assert [i.kind for i in report.important] == ["config_loose", "requirement"]
    lines = report.lines()
    assert lines[0] == "실행 전 점검: 확인할 것 2개" and lines[1].startswith("• [높음] REFramework 설정에서")
    assert f"    (설정 고치기: {LOOSE}=true)" in lines and any(line.startswith("• [낮음]") for line in lines)
    assert not any(line.startswith("• [낮음]") for line in report.lines(low=False))
    assert report.summary() == "실행 전 점검: 높음 1 · 보통 1 · 낮음 1"
    again = precheck.Report(list(reversed(report.issues)), game_version="v1")
    assert again.signature() == report.signature() != precheck.Report([], game_version="v1").signature()
    assert precheck.Report().title() == "실행 전 점검: 확인할 것이 없습니다."


def test_user_names_are_masked_in_the_text():
    report = precheck.Report([precheck.Issue("verify", "medium", r"C:\Users\홍길동\Games\x", [r"C:\Users\홍길동\a"])])
    assert "홍길동" not in "\n".join(report.lines()) and r"C:\Users\<사용자>\Games\x" in report.lines()[1]


def test_only_new_important_issues_are_asked(game, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    write(game / "pak_mods/hand.pak", "p")
    write(game / "mystery.dll", "MZ")
    report = precheck.check(facts(game))
    assert [i.kind for i in precheck.to_ask(report, data)] == ["pak_unknown"]  # (low ones are never asked)
    precheck.remember(data, report.important, report.game_version)
    assert precheck.to_ask(precheck.check(facts(game)), data) == []
    # another unknown pak: the same kind of issue, but with something new in it
    write(game / "pak_mods/other.pak", "p")
    assert [i.kind for i in precheck.to_ask(precheck.check(facts(game)), data)] == ["pak_unknown"]
    (game / "pak_mods/other.pak").unlink()
    (game / "pak_mods/hand.pak").unlink()
    write(game / "pak_mods/hand.pak", "p")
    assert precheck.to_ask(precheck.check(facts(game)), data) == []  # (a pak gone is not new)
    # a game update asks about everything again
    assert [i.kind for i in precheck.to_ask(precheck.check(facts(game, game_version="v2")), data)] == ["pak_unknown"]


def test_a_changed_mod_is_asked_about_again(game, tmp_path):
    data = tmp_path / "data"
    report = precheck.check(facts(game, [mod("a", material_state="stale")]))
    precheck.remember(data, report.important, "v1")
    assert precheck.to_ask(precheck.check(facts(game, [mod("a", material_state="stale")])), data) == []
    updated = mod("a", material_state="stale", stamp="2026-10-01|2026-10-20")
    assert len(precheck.to_ask(precheck.check(facts(game, [updated])), data)) == 1


def test_a_broken_state_file_asks_again(game, tmp_path, caplog):
    data = tmp_path / "data"
    report = precheck.check(facts(game, [mod("a", material_state="stale")]))
    precheck.remember(data, report.important, "v1")
    (data / precheck.STATE_FILE).write_text("{not json", encoding="utf-8")
    assert len(precheck.to_ask(report, data)) == 1
    precheck.remember(data, report.important, "v1")  # (and it can be written again)
    assert precheck.to_ask(report, data) == []


# ---- the slow part: the last session's records -------------------------------------------------------
def stamp_text(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log_text(game: Path, start: float, body=(), commit=COMMIT) -> str:
    lines = [(0, "REFramework entry"), (0, f"Commit hash: {commit}"), (0, "Tag: v1.5.9.1"),
             (0, f"Current game path: {game}"), *body, (600, "Hooked DirectX 12")]
    return "\r\n".join(f"[{stamp_text(start + dt)}] [REFramework] [info] {msg}" for dt, msg in lines) + "\r\n"


def session(tmp_path, game, mods, faulty=(), body=(), start=None, game_version="v1", commit=COMMIT, placed=None,
            dirty=False):
    """Collect one game session (its log and faulty list in the game folder) with the mods as they
    were then: [(id, name, enabled, stamp)]; ``placed``: the mods with files in the game folder then
    (by default the enabled ones; None in the record: an older one that did not say). REFramework's
    settings are copied with it (loose file loading on, unless a test wrote them)."""
    start = start if start is not None else time.time() - 2 * DAY
    write(game / crashlog.LOG_FILE, log_text(game, start, body, commit))
    os.utime(game / crashlog.LOG_FILE, (start + 600, start + 600))
    if faulty:
        write(game / crashlog.FAULTY_FILE, "".join(f'"error" 2 "Invalid file" "{p}"\r\n' for p in faulty))
        os.utime(game / crashlog.FAULTY_FILE, (start + 60, start + 60))
    if not (game / crashlog.CONFIG_FILE).exists():
        config(game, f"{LOOSE}=true\n")
    context = {"game_version": game_version,
               "mods": [{"id": i, "name": n, "enabled": on, "stamp": s} for i, n, on, s in mods],
               "placed": [i for i, _n, on, _s in mods if on] if placed is None else placed, "dirty": dirty}
    if placed is False:  # (a record made before the manager kept these)
        del context["placed"], context["dirty"]
    crash_dir = tmp_path / "data" / "crashes"
    (found,) = crashes.collect(crash_dir, game, context, appdata="")
    return crash_dir, found


@pytest.fixture
def armor(game):
    """A loose armor mod in the game folder whose .user file REFramework could not load."""
    rel = f"natives/STM/{AVP}.3"
    write(game / rel, "old user file")
    return mod("a", "방어구 A", kinds=["loose"], files=[rel]), manifest(entry(rel, "a"))


def test_files_an_enabled_mod_has_that_reframework_could_not_load(game, tmp_path, armor):
    armor_mod, deployed = armor
    config(game, f"{LOOSE}=true\n")
    vanilla = "GameDesign/Equip/_Prefab/Armor/Male/009/000/009_000_avp.user"
    crash_dir, s = session(tmp_path, game, [("a", "방어구 A", True, armor_mod.stamp)], [AVP, vanilla])
    f = facts(game, [armor_mod], deployed)
    bg = precheck.background(f, crash_dir, tmp_path / "cache", materials=False)
    assert bg.session == s.id and bg.faulty[0]["entries"] == [["Invalid file", AVP]]
    assert bg.faulty[0]["paths"] == [f"natives/STM/{AVP}.3"] and bg.faulty[0]["on"] is True
    found = issue(precheck.check(f, bg), "faulty")
    assert found.level == "high" and found.action == "matfix" and found.mods == ["a"]
    assert found.text == "지난번 게임에서 REFramework가 읽지 못한 파일: '방어구 A'의 003_001_avp.user → 재질 복구를 해 보세요"
    assert found.detail == [f'"Invalid file" "{AVP}"']
    # the game's own file that no mod has is not blamed on anything (REFramework lists some without mods)
    assert [i.kind for i in precheck.check(f, bg).issues if i.kind.startswith("faulty")] == ["faulty"]
    # kept in the cache folder
    assert precheck.load_background(tmp_path / "cache").faulty == bg.faulty


def test_faulty_files_no_longer_hold(game, tmp_path, armor):
    armor_mod, deployed = armor
    crash_dir, _s = session(tmp_path, game, [("a", "방어구 A", True, armor_mod.stamp)], [AVP])
    f = facts(game, [armor_mod], deployed)
    bg = precheck.background(f, crash_dir, materials=False)
    assert "faulty" in kinds(precheck.check(f, bg))
    off = mod("a", "방어구 A", enabled=False, kinds=["loose"], files=armor_mod.files)
    assert "faulty" not in kinds(precheck.check(facts(game, [off], deployed), bg))
    updated = mod("a", "방어구 A", kinds=["loose"], files=armor_mod.files, stamp="2026-10-01|2026-10-21")
    assert "faulty" not in kinds(precheck.check(facts(game, [updated], deployed), bg))
    repaired = manifest(entry(f"natives/STM/{AVP}.3", "a", src_mtime=time.time()))  # (matfix\ holds a new copy)
    assert "faulty" not in kinds(precheck.check(facts(game, [armor_mod], repaired), bg))


def test_faulty_files_of_a_mod_that_was_off_then(game, tmp_path, armor):
    armor_mod, deployed = armor
    crash_dir, _s = session(tmp_path, game, [("a", "방어구 A", False, armor_mod.stamp)], [AVP])
    f = facts(game, [armor_mod], deployed)
    bg = precheck.background(f, crash_dir, materials=False)
    assert bg.faulty == [] and "faulty" not in kinds(precheck.check(f, bg))  # (then it was the game's own file)
    # installed after that session: not in its list at all
    crash_dir, _s = session(tmp_path / "other", game, [], [AVP])
    assert precheck.background(f, crash_dir, materials=False).faulty == []


def test_faulty_files_inside_a_mod_pak(game, tmp_path):
    write_pak([(path_hash(f"natives/STM/{AVP}.3"), b"x", 1, 0)], write(game / "pak_mods/001_Armor.pak", ""))
    deployed = manifest(entry("pak_mods/001_Armor.pak", "p", pak=True))
    pak_mod = mod("p", "갑옷 팩", kinds=["pak"], files=["Armor.pak"])
    crash_dir, _s = session(tmp_path, game, [("p", "갑옷 팩", True, pak_mod.stamp)], [AVP])
    f = facts(game, [pak_mod], deployed)
    found = issue(precheck.check(f, precheck.background(f, crash_dir, materials=False)), "faulty")
    assert found.mods == ["p"] and "'갑옷 팩'의 003_001_avp.user" in found.text


def test_faulty_files_no_mod_placed(game, tmp_path):
    write(game / f"natives/STM/{AVP}.3", "by hand")
    other = "GameDesign/Equip/_Prefab/Armor/Male/004/000/004_000_avp.user"
    write_pak([(path_hash(f"natives/STM/{other}.3"), b"x", 1, 0)], write(game / "pak_mods/hand.pak", ""))
    crash_dir, _s = session(tmp_path, game, [], [AVP, other])
    f = facts(game)
    bg = precheck.background(f, crash_dir, materials=False)
    report = precheck.check(f, bg)
    loose = issue(report, "faulty_loose")
    assert loose.level == "medium" and loose.detail == [f"natives/STM/{AVP}.3"]
    assert loose.folder == f"natives/STM/{AVP}.3".rsplit("/", 1)[0]
    pak = issue(report, "faulty_pak")
    assert pak.level == "medium" and "매니저가 넣지 않은 PAK 'pak_mods/hand.pak'에 있습니다" in pak.text
    (game / f"natives/STM/{AVP}.3").unlink()  # taken out since
    assert "faulty_loose" not in kinds(precheck.check(f, bg))


def test_the_copy_in_the_game_folder_then_is_the_one_blamed(game, tmp_path, armor):
    armor_a, deployed = armor  # (A's copy is in the game folder now)
    rel = armor_a.files[0]
    # both were on and B came later: B's copy was read; B is off now (and applied): nothing to ask about
    armor_b = mod("b", "방어구 B", enabled=False, kinds=["loose"], files=[rel])
    crash_dir, _s = session(tmp_path, game, [("a", "방어구 A", True, armor_a.stamp), ("b", "방어구 B", True, armor_b.stamp)],
                            [AVP])
    f = facts(game, [armor_a, armor_b], deployed)
    bg = precheck.background(f, crash_dir, materials=False)
    assert [r["mod"] for r in bg.faulty] == [] and "faulty" not in kinds(precheck.check(f, bg))
    # B is on again: it is the one
    on = mod("b", "방어구 B", kinds=["loose"], files=[rel])
    f = facts(game, [armor_a, on], manifest(entry(rel, "b")))
    assert issue(precheck.check(f, precheck.background(f, crash_dir, materials=False)), "faulty").mods == ["b"]


def test_a_mod_turned_on_but_not_applied_had_no_file_in_that_game(game, tmp_path, armor):
    armor_mod, deployed = armor
    crash_dir, _s = session(tmp_path / "1", game, [("a", "방어구 A", True, armor_mod.stamp)], [AVP], placed=[],
                            dirty=True)
    f = facts(game, [armor_mod], deployed)  # (applied since, from a source older than that game)
    bg = precheck.background(f, crash_dir, materials=False)
    assert bg.faulty == [] and "faulty" not in kinds(precheck.check(f, bg))
    # a record that does not say (made before the manager kept this): asked about, but not as certain
    crash_dir, _s = session(tmp_path / "2", game, [("a", "방어구 A", True, armor_mod.stamp)], [AVP], placed=False)
    found = issue(precheck.check(f, precheck.background(f, crash_dir, materials=False)), "faulty")
    assert found.level == "medium" and found.mods == ["a"] and found.action == "matfix"
    assert found.text.startswith("지난번 게임에서 REFramework가 읽지 못한 파일 003_001_avp.user은(는) '방어구 A'의 파일일 수 "
                                 "있습니다(그때 게임 폴더에 있던 것이 이 모드의 파일인지는 확실하지 않음)")
    # the game ran with changes not applied: what was in the game folder may differ from the mod now
    crash_dir, _s = session(tmp_path / "3", game, [("a", "방어구 A", True, armor_mod.stamp)], [AVP], dirty=True)
    assert issue(precheck.check(f, precheck.background(f, crash_dir, materials=False)), "faulty").level == "medium"


def test_of_two_paks_with_the_file_the_later_one_is_blamed(game, tmp_path):
    for rel in ("pak_mods/001_Old Armor.pak", "pak_mods/002_New Armor.pak"):
        write_pak([(path_hash(f"natives/STM/{AVP}.3"), b"x", 1, 0)], write(game / rel, ""))
    deployed = manifest(entry("pak_mods/001_Old Armor.pak", "old", pak=True),
                        entry("pak_mods/002_New Armor.pak", "new", pak=True))
    mods = [mod("old", "Old Armor", kinds=["pak"], files=["Old.pak"]), mod("new", "New Armor", kinds=["pak"],
                                                                           files=["New.pak"])]
    crash_dir, _s = session(tmp_path, game, [("old", "Old Armor", True, mods[0].stamp),
                                             ("new", "New Armor", True, mods[1].stamp)], [AVP])
    f = facts(game, mods, deployed)
    report = precheck.check(f, precheck.background(f, crash_dir, materials=False))
    assert [i.mods for i in report.issues if i.kind == "faulty"] == [["new"]]


def test_a_loose_file_comes_before_a_pak_and_only_when_loose_files_are_read(game, tmp_path):
    write_pak([(path_hash(f"natives/STM/{AVP}.3"), b"x", 1, 0)], write(game / "pak_mods/001_Armor.pak", ""))
    write(game / f"natives/STM/{AVP}.3", "by hand")
    deployed = manifest(entry("pak_mods/001_Armor.pak", "p", pak=True))
    pak_mod = mod("p", "갑옷 팩", kinds=["pak"], files=["Armor.pak"])
    crash_dir, _s = session(tmp_path / "1", game, [("p", "갑옷 팩", True, pak_mod.stamp)], [AVP])
    f = facts(game, [pak_mod], deployed)
    bg = precheck.background(f, crash_dir, materials=False)
    assert bg.session_loose is True
    report = precheck.check(f, bg)
    assert "faulty" not in kinds(report) and issue(report, "faulty_loose").detail == [f"natives/STM/{AVP}.3"]
    # loose file loading off in that game: the file by hand was not read (nor a loose mod's), the pak was
    config(game, f"{LOOSE}=false\n")
    loose_mod = mod("a", "방어구 A", kinds=["loose"], files=[f"natives/STM/{AVP}.3"])
    crash_dir, _s = session(tmp_path / "2", game, [("a", "방어구 A", True, loose_mod.stamp),
                                                   ("p", "갑옷 팩", True, pak_mod.stamp)], [AVP])
    f = facts(game, [loose_mod, pak_mod], deployed)
    bg = precheck.background(f, crash_dir, materials=False)
    assert bg.session_loose is False
    report = precheck.check(f, bg)
    assert issue(report, "faulty").mods == ["p"] and "faulty_loose" not in kinds(report)
    # without the settings copied with the record: as they are now
    (crash_dir / _s.id / crashlog.CONFIG_FILE).unlink()
    config(game, f"{LOOSE}=true\n")
    assert precheck.background(f, crash_dir, materials=False).session_loose is True


def test_files_the_material_check_found_fitting_are_not_said_to_need_repair(game, tmp_path, armor):
    armor_mod, deployed = armor
    crash_dir, _s = session(tmp_path, game, [("a", "방어구 A", True, armor_mod.stamp)], [AVP])
    bg = precheck.background(facts(game, [armor_mod], deployed), crash_dir, materials=False)
    current = mod("a", "방어구 A", kinds=["loose"], files=armor_mod.files, material_state="current")
    found = issue(precheck.check(facts(game, [current], deployed), bg), "faulty")
    assert found.level == "low" and found.action == "crash_record" and "재질 점검에서는 현재 게임과 맞는 파일입니다" in found.text
    assert "재질 복구를 해 보세요" not in found.text
    repaired = mod("a", "방어구 A", kinds=["loose"], files=armor_mod.files, material_state="repaired")
    found = issue(precheck.check(facts(game, [repaired], deployed), bg), "faulty")  # (repaired before that game)
    assert found.level == "high" and found.action == "crash_record" and "재질 복구를 한 파일인데도 기록되었습니다" in found.text


LOOSE_FAILED = "[LooseFileLoader] Failed to find path_to_hash candidate, cannot continue"


def test_reframework_that_failed_to_start_and_was_not_replaced(game, tmp_path):
    config(game, f"{LOOSE}=true\n")
    crash_dir, _s = session(tmp_path, game, [], body=[(5, LOOSE_FAILED)], game_version="v0")
    f = facts(game, [mod("a", kinds=["loose"])])
    bg = precheck.background(f, crash_dir, materials=False)
    assert bg.refw_failed == [["loose", LOOSE_FAILED]] and bg.refw_same is True
    found = issue(precheck.check(f, bg), "refw_failed")
    assert found.level == "high" and found.action == "update_refw" and "(루스 파일 읽기를 켜지 못함)" in found.text
    assert found.detail == [LOOSE_FAILED]
    # (that session ran on the game before its update: said so, never "in this game version")
    assert found.text.startswith("게임 업데이트 전 지난번 게임에서 REFramework가 제대로 시작하지 못했고")
    assert "이 게임 버전에서" not in found.text
    same = facts(game, [mod("a", kinds=["loose"])], game_version="v0")
    assert issue(precheck.check(same, bg), "refw_failed").text.startswith("지난번 게임에서 REFramework가 제대로")
    # no loose mods: it did not matter
    assert "refw_failed" not in kinds(precheck.check(facts(game, [mod("p", kinds=["pak"])]), bg))
    # REFramework was replaced since (another build, or the same file touched): not the same any more
    write(game / "dinput8.dll", "MZ another build")
    assert "refw_failed" not in kinds(precheck.check(f, bg))
    bg = precheck.background(f, crash_dir, materials=False)
    assert bg.refw_same is False and "refw_failed" not in kinds(precheck.check(f, bg))


FALLBACK = "Failed to write to current directory, falling back to appdata folder"


def test_reframework_in_appdata_does_not_read_the_game_folders_scripts(game, tmp_path):
    data = tmp_path / "data"
    crash_dir, _s = session(tmp_path, game, [], body=[(1, FALLBACK)])
    f = facts(game, [mod("s", kinds=["script"])])
    report = precheck.check(f, precheck.background(f, crash_dir, materials=False))
    found = issue(report, "refw_appdata")
    assert found.level == "medium" and found.mods == ["s"] and found.items == ["s"]
    assert "설정도 그 폴더의 것을 쓰고" in found.text
    precheck.remember(data, report.important, report.game_version)
    # the next game falls back again: the same thing, not asked again
    crash_dir, _s = session(tmp_path, game, [], body=[(1, FALLBACK)], start=time.time() - DAY)
    assert precheck.to_ask(precheck.check(f, precheck.background(f, crash_dir, materials=False)), data) == []
    # another script mod would not be read either: asked
    more = facts(game, [mod("s", kinds=["script"]), mod("t", kinds=["script"])])
    assert [i.kind for i in precheck.to_ask(precheck.check(more, precheck.background(more, crash_dir,
                                                                                     materials=False)), data)] == [
        "refw_appdata"]
    # only loose or PAK mods: worth knowing (their settings come from there)
    loose = facts(game, [mod("a", kinds=["loose"])])
    assert issue(precheck.check(loose, precheck.background(loose, crash_dir, materials=False)),
                 "refw_appdata").level == "low"


def test_settings_are_read_where_reframework_wrote_its_last_log(game, tmp_path):
    appdata = tmp_path / "Roaming"
    folder = appdata / "REFramework" / "MonsterHunterWilds"
    config(game, f"{LOOSE}=true\n")  # (left from before: REFramework does not read it now)
    write(folder / crashlog.CONFIG_FILE, f"FontSize=16\r\n{LOOSE}=false\r\n")
    start = time.time() - DAY
    write(folder / crashlog.LOG_FILE, log_text(game, start, [(1, FALLBACK)]))
    crash_dir = tmp_path / "data" / "crashes"
    crashes.collect(crash_dir, game, {"mods": []}, appdata=str(appdata))
    f = facts(game, [mod("a", kinds=["loose"])], appdata=str(appdata))
    bg = precheck.background(f, crash_dir, materials=False)
    assert bg.appdata and bg.session_log
    found = issue(precheck.check(f, bg), "config_loose")
    assert found.config == str(folder / crashlog.CONFIG_FILE)
    assert crashes.set_refw_option(game, *found.fix, path=found.config)
    assert (folder / crashlog.CONFIG_FILE).read_bytes() == f"FontSize=16\r\n{LOOSE}=true\r\n".encode()
    assert "config_loose" not in kinds(precheck.check(f, bg))
    # not there yet (REFramework did not get as far as writing it): its defaults, and the fix makes it there
    (folder / crashlog.CONFIG_FILE).unlink()
    found = issue(precheck.check(f, bg), "config_loose")
    assert found.config == str(folder / crashlog.CONFIG_FILE) and found.detail[-1].endswith("(아직 없음)")
    # without a log that says: the file that is there
    assert "config_loose" not in kinds(precheck.check(f, Background(game_version="v1")))


def test_no_records_no_findings(game, tmp_path):
    f = facts(game, [mod("a", kinds=["loose"])])
    bg = precheck.background(f, tmp_path / "data" / "crashes", materials=False)
    assert bg.session == "" and bg.faulty == [] and bg.refw_failed == []


# ---- the slow part: loose files the manager did not place --------------------------------------------
def test_loose_files_no_mod_placed_are_counted(game, tmp_path):
    deployed = manifest(entry("natives/STM/mine.tex.1", "a"))
    for rel in ("natives/STM/mine.tex.1", "natives/STM/Art/b.tex.1", "natives/STM/Art/a.tex.1",
                "natives/x.tmp.mhwmm-tmp"):
        write(game / rel, "t")
    f = facts(game, [mod("a", kinds=["loose"])], deployed)
    bg = precheck.background(f, None, tmp_path / "cache", materials=False)
    assert (bg.natives_total, bg.natives_sample) == (2, ["natives/STM/Art/a.tex.1", "natives/STM/Art/b.tex.1"])
    found = issue(precheck.check(f, bg), "natives_unmanaged")
    assert found.level == "low" and "루스 파일(natives) 2개가 있습니다('a.tex.1', 'b.tex.1')" in found.text


@pytest.fixture
def fake_matfix(monkeypatch):
    calls = []

    def check(game_dir, files, cache_dir=None, game=None, progress=None):
        calls.append([rel for rel, _p in files])
        return {rel: ("fixable" if "old" in rel else "current", "mdf2") for rel, _p in files}

    monkeypatch.setattr(precheck, "check_materials", check)
    import mhwmm.matfix as matfix

    monkeypatch.setattr(matfix, "available", lambda: True)
    return calls


def test_loose_files_no_mod_placed_that_no_longer_fit(game, tmp_path, fake_matfix):
    old = "natives/STM/Art/old_body.mdf2.45"
    write(game / old, "m")
    write(game / "natives/STM/Art/new_body.mdf2.45", "m")
    write(game / "natives/STM/Art/body.tex.241106027", "t")
    config(game, f"{LOOSE}=true\n")
    f = facts(game, [mod("a", kinds=["loose"])])
    cache = tmp_path / "cache"
    bg = precheck.background(f, None, cache)
    assert fake_matfix == [["natives/STM/Art/new_body.mdf2.45", old]]  # (only material files are checked)
    found = issue(precheck.check(f, bg), "natives_stale")
    assert found.level == "high" and found.detail == [old] and "루스 파일 1개('old_body.mdf2.45')가 현재 게임과" in found.text
    assert issue(precheck.check(f, bg), "natives_unmanaged").text.startswith("매니저가 넣지 않은 루스 파일(natives) 3개")
    # the next run checks only what changed
    write(game / "natives/STM/Art/another_old.mdf2.45", "m")
    precheck.background(f, None, cache)
    assert fake_matfix[-1] == ["natives/STM/Art/another_old.mdf2.45"]
    # loose files are not read: worth knowing only
    config(game, f"{LOOSE}=false\n")
    assert issue(precheck.check(f, bg), "natives_stale").level == "low"
    # changed or gone since: no longer said
    write(game / old, "changed")
    os.utime(game / old, (time.time() + 10, time.time() + 10))
    assert "natives_stale" not in kinds(precheck.check(f, bg))
    # another game version: the results are of the old one
    later = facts(game, [mod("a", kinds=["loose"])], game_version="v2")
    report = precheck.check(later, precheck.background(f, None, cache))
    assert report.background == "outdated" and "natives_stale" not in kinds(report)


def test_quick_runs_keep_what_was_found_under_natives(game, tmp_path, fake_matfix):
    old = "natives/STM/Art/old_body.mdf2.45"
    write(game / old, "m")
    config(game, f"{LOOSE}=true\n")
    f = facts(game, [mod("a", kinds=["loose"])])
    cache = tmp_path / "cache"
    full = precheck.background(f, None, cache)
    assert fake_matfix == [[old]] and not precheck.background_due(f, full)
    # right before a launch: only the records (natives\ as found before, nothing checked again)
    write(game / "natives/STM/Art/new_old.mdf2.45", "m")
    quick = precheck.background(f, None, cache, natives=False)
    assert len(fake_matfix) == 1 and quick.natives_made == full.natives_made and quick.natives_total == 1
    assert issue(precheck.check(f, quick), "natives_stale").detail == [old]
    # looking without checking: new files wait for a full run, unchanged ones keep their result
    seen = precheck.background(f, None, cache, materials=False)
    assert len(fake_matfix) == 1 and seen.natives_total == 2 and list(seen.materials) == [old]
    # a quick run without a full one before it: the full one is still due
    fresh = precheck.background(f, None, tmp_path / "other", natives=False)
    assert fresh.natives_made == 0 and precheck.background_due(f, fresh)


def test_verdicts_of_older_check_rules_are_not_used(game, tmp_path, fake_matfix, monkeypatch):
    """After an update that changes the material rules (manager.MATFIX_FORMAT), a natives\\ file the old rules
    called stale is not reported, a quick run does not carry its verdict over under the new rules, and the
    next full run checks it again."""
    old = "natives/STM/Art/old_body.mdf2.45"
    write(game / old, "m")
    config(game, f"{LOOSE}=true\n")
    f = facts(game, [mod("a", kinds=["loose"])])
    cache = tmp_path / "cache"
    monkeypatch.setattr(precheck, "_matfix_format", lambda: 3)
    full = precheck.background(f, None, cache)
    assert issue(precheck.check(f, full), "natives_stale").detail == [old]

    monkeypatch.setattr(precheck, "_matfix_format", lambda: 4)  # the update
    assert "natives_stale" not in kinds(precheck.check(f, precheck.load_background(cache)))
    quick = precheck.background(f, None, cache, natives=False)  # right before a launch
    assert quick.matfix_format == 4 and quick.materials == {} and precheck.background_due(f, quick)
    assert "natives_stale" not in kinds(precheck.check(f, quick)) and len(fake_matfix) == 1
    again = precheck.background(f, None, cache)
    assert fake_matfix[-1] == [old] and issue(precheck.check(f, again), "natives_stale").detail == [old]


def test_scan_results_of_the_previous_format_are_dropped(tmp_path):
    cache = tmp_path / "cache"
    precheck.save_background(cache, precheck.Background(game_version="v1", made=1.0))
    data = json.loads((cache / precheck.BACKGROUND_FILE).read_text(encoding="utf-8"))
    assert data["format"] == precheck.BACKGROUND_FORMAT == 3
    data["format"] = 2  # made by v2.2.8 (material verdicts of the rules before 4)
    (cache / precheck.BACKGROUND_FILE).write_text(json.dumps(data), encoding="utf-8")
    assert precheck.load_background(cache) is None


def test_the_game_index_is_made_only_when_needed(game, tmp_path, monkeypatch):
    import mhwmm.matfix as matfix

    monkeypatch.setattr(matfix, "available", lambda: True)
    asked = []

    def index():
        asked.append(1)
        raise matfix.MatfixError("게임 폴더에서 re_chunk_000.pak을 찾지 못했습니다.")

    f = facts(game)
    write(game / "natives/STM/Art/body.tex.241106027", "t")
    precheck.background(f, None, tmp_path / "cache", game=index)
    assert asked == []  # (nothing to check)
    write(game / "natives/STM/Art/body.mdf2.45", "m")
    bg = precheck.background(f, None, tmp_path / "cache", game=index)
    assert asked == [1] and "re_chunk_000.pak" in bg.matfix and bg.materials == {} and bg.natives_total == 2


def test_material_check_without_zstd(game, tmp_path, monkeypatch):
    import mhwmm.matfix as matfix

    monkeypatch.setattr(matfix, "available", lambda: False)
    write(game / "natives/STM/Art/body.mdf2.45", "m")
    bg = precheck.background(facts(game), None, tmp_path / "cache")
    assert "zstd" in bg.matfix and bg.materials == {} and bg.natives_total == 1


def test_material_check_of_a_real_stale_file(tmp_path):
    from mhwmm import matfix

    if not matfix.available():
        pytest.skip("zstd 모듈이 없습니다")
    import test_matfix as M

    game = tmp_path / "MonsterHunterWilds"
    game.mkdir()
    write(game / "MonsterHunterWilds.exe", "exe")
    M.write_game_pak(game / "re_chunk_000.pak", {M.BODY + ".mdf2.45": M.assemble_mdf2(M.current_body(), 45),
                                                   M.LEGS + ".mdf2.45": M.assemble_mdf2(M.current_body()[:1], 45)})
    write(game / f"{M.BODY}.mdf2.45", M.assemble_mdf2(M.old_body_mod(), 45))  # put in by hand, before the update
    write(game / f"{M.LEGS}.mdf2.45", M.assemble_mdf2(M.current_body()[:1], 45))
    config(game, f"{LOOSE}=true\n")
    f = facts(game, [mod("a", kinds=["loose"])])
    bg = precheck.background(f, None, tmp_path / "cache")
    assert bg.matfix == "" and {rel: r[2] for rel, r in bg.materials.items()} == {
        f"{M.BODY}.mdf2.45": "fixable", f"{M.LEGS}.mdf2.45": "current"}
    assert issue(precheck.check(f, bg), "natives_stale").detail == [f"{M.BODY}.mdf2.45"]


# ---- the slow part's result ---------------------------------------------------------------------------
def test_background_is_kept_and_read_back_tolerantly(tmp_path):
    bg = Background(game_version="v1", made=1.5, session="20261019-101010", refw_same=True,
                    faulty=[{"mod": "a"}], materials={"natives/x.mdf2.45": [1, 2, "fixable", "mdf2"]})
    precheck.save_background(tmp_path, bg)
    assert precheck.load_background(tmp_path) == bg
    data = bg.to_dict()
    data.update(made="yesterday", refw_same="yes", faulty={"not": "a list"}, session_end=3)
    assert precheck.Background.from_dict(data) == Background(game_version="v1", session="20261019-101010",
                                                             materials=bg.materials, session_end=3)
    assert precheck.Background.from_dict({**data, "format": 99}) is None
    (tmp_path / precheck.BACKGROUND_FILE).write_text("[", encoding="utf-8")
    assert precheck.load_background(tmp_path) is None
    assert precheck.load_background(None) is None


def test_when_the_slow_part_should_run_again(game, tmp_path):
    f = facts(game)
    crash_dir = tmp_path / "data" / "crashes"
    assert precheck.background_due(f, None, crash_dir)
    bg = precheck.background(f, crash_dir, materials=False)
    assert not precheck.background_due(f, bg, crash_dir)
    assert precheck.background_due(facts(game, game_version="v2"), bg, crash_dir)
    assert precheck.background_due(f, bg, crash_dir, now=time.time() + 2 * DAY)
    crash_dir, s = session(tmp_path, game, [])  # a session collected since
    assert precheck.background_due(f, bg, crash_dir)
    bg = precheck.background(f, crash_dir, materials=False)
    assert not precheck.background_due(f, bg, crash_dir)
    write(game / crashlog.FAULTY_FILE, f'"error" 2 "Invalid file" "{AVP}"\r\n')  # the same session's list, later
    crashes.collect(crash_dir, game, {}, appdata="")
    assert precheck.background_due(f, bg, crash_dir)


def test_background_never_raises(game, tmp_path, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise OSError("disk gone")

    monkeypatch.setattr(crashes, "sessions", broken)
    monkeypatch.setattr(precheck, "_walk", broken)
    bg = precheck.background(facts(game), tmp_path / "crashes", tmp_path / "cache")
    assert bg.session == "" and bg.natives_total == 0
    assert "지난 게임 기록을 점검하지 못했습니다" in caplog.text


# ---- from a ModManager ---------------------------------------------------------------------------------
def install(manager, tmp_path, name, files):
    with manager.prepare_install(make_zip(tmp_path / "zips" / f"{name}.zip", files)) as s:
        return manager.install_variant(s, s.variants[0])


def test_facts_and_report_of_a_manager(manager, tmp_path, game_dir):
    refw = install(manager, tmp_path, "REFramework", {"dinput8.dll": "MZ"})
    armor = install(manager, tmp_path, "Armor", {"natives/STM/a.tex": "a"})
    pak = install(manager, tmp_path, "PakMod", {"PakMod.pak": "p"})
    app = install(manager, tmp_path, "App", {"reframework/autorun/app.lua": 'require("_CatLib")'})
    with manager.prepare_install(make_zip(tmp_path / "zips" / "Opts.zip", {
            "Opts/One/natives/STM/o.tex": "1", "Opts/Two/natives/STM/o.tex": "2"})) as s:
        opts = manager.install_package(s, s.variants, None, [0])
    manager.set_enabled(opts.id, True)
    manager.set_options(opts.id, [])
    manager.deploy()
    write(game_dir / "pak_mods/hand.pak", "p")
    f = precheck.gather(manager, appdata="")
    assert f.game_dir == game_dir and f.game_version == manager.game_version() and f.verify == []
    states = {m.id: m for m in f.mods}
    assert states[refw.id].refw and states[armor.id].kinds == {"loose"} and states[pak.id].kinds == {"pak"}
    assert states[app.id].missing == ["_CatLib"] and states[opts.id].needs_option
    assert states[armor.id].stamp == f"{armor.installed_at}|{armor.updated_at}"
    assert set(f.deployed) == {"dinput8.dll", "natives/stm/a.tex", "pak_mods/001_pakmod.pak",
                               "reframework/autorun/app.lua"}
    report = precheck.check(f)
    assert issue(report, "config_loose").mods == [armor.id]  # (no settings file yet: off at first)
    assert issue(report, "pak_unknown").detail == ["pak_mods/hand.pak"]
    assert issue(report, "requirement").mods == [app.id]
    assert report.problems == {app.id: ["필요한 모드가 없거나 꺼져 있습니다: _CatLib"],
                               opts.id: ["옵션을 하나도 켜지 않아 아무 파일도 적용되지 않습니다"]}
    # what the main list shows, from the states alone (no folder scans)
    assert precheck.mod_problems(precheck.mod_states(manager)) == report.problems
    # the slow part with the manager's records
    bg = precheck.background(f, manager.crash_dir, manager.data_dir / "cache", materials=False)
    assert (manager.data_dir / "cache" / precheck.BACKGROUND_FILE).is_file() and bg.game_version == f.game_version
    # a deployed file that went missing shows up as the deploy check's problem
    (game_dir / "natives/STM/a.tex").unlink()
    assert issue(precheck.check(precheck.gather(manager, appdata="")), "verify").action == "apply"


def test_the_managers_problems_and_checks(manager, tmp_path, game_dir, monkeypatch):
    monkeypatch.setenv("APPDATA", "")
    install(manager, tmp_path, "REFramework", {"dinput8.dll": "MZ"})
    app = install(manager, tmp_path, "App", {"reframework/autorun/app.lua": 'require("_CatLib")'})
    manager.deploy()
    # the main list's ⚠ marks (also what the GUI shows) come from here
    assert manager.problems() == {app.id: ["필요한 모드가 없거나 꺼져 있습니다: _CatLib"]}
    report = manager.prelaunch_check()
    assert issue(report, "requirement").mods == [app.id] and report.background == "none"
    bg = manager.precheck_background(precheck.gather(manager), materials=False)
    assert precheck.load_background(manager.data_dir / "cache").game_version == bg.game_version == report.game_version
    assert manager.prelaunch_check().background == "current"


def test_cli_check_and_fix(manager, tmp_path, game_dir, capsys, monkeypatch):
    import mhwmm.manager
    from mhwmm.cli import main

    monkeypatch.setenv("APPDATA", "")
    install(manager, tmp_path, "REFramework", {"dinput8.dll": "MZ"})
    install(manager, tmp_path, "Armor", {"natives/STM/a.tex": "a"})
    manager.deploy()
    settings = game_dir / crashlog.CONFIG_FILE
    write(settings, "FontSize=16\r\nLooseFileLoader_Enabled=false\r\n")
    write(game_dir / "reframework/autorun/hand.lua", "x = 1")
    data = str(manager.data_dir)

    assert main(["--data-dir", data, "check", "--quick"]) == 1  # a high issue
    out = capsys.readouterr().out
    assert out.startswith("실행 전 점검: 확인할 것 1개\n• [높음] REFramework 설정에서 루스 파일 읽기(LooseFileLoader)가 꺼져")
    assert "• [낮음] 매니저가 넣지 않은 Lua 스크립트 1개가 있습니다('hand.lua')" in out and "check --fix" in out
    assert (manager.data_dir / "cache" / precheck.BACKGROUND_FILE).is_file()
    # --fix: not while the game runs (REFramework writes its settings back when it closes)
    monkeypatch.setattr(mhwmm.manager, "is_game_running", lambda: True)
    assert main(["--data-dir", data, "check", "--fix"]) == 1
    assert "게임이 실행 중입니다" in capsys.readouterr().err
    assert settings.read_bytes() == b"FontSize=16\r\nLooseFileLoader_Enabled=false\r\n"
    monkeypatch.setattr(mhwmm.manager, "is_game_running", lambda: False)
    assert main(["--data-dir", data, "check", "--fix"]) == 0  # only a low one left
    out = capsys.readouterr().out
    assert "REFramework 설정을 바꿨습니다: LooseFileLoader_Enabled=true" in out
    assert "실행 전 점검: 확인할 것이 없습니다." in out and "check --fix" not in out
    assert settings.read_bytes() == b"FontSize=16\r\nLooseFileLoader_Enabled=true\r\n"


def test_blame_follows_what_was_in_the_game_folder(manager, tmp_path, game_dir, monkeypatch):
    """With a real manager: the conflict's winner is blamed (not the mod placed after it was turned off),
    and a mod turned on without applying is not blamed for a game that ran without it."""
    monkeypatch.setenv("APPDATA", "")
    rel = f"natives/STM/{AVP}.3"
    config(game_dir, f"{LOOSE}=true\n")
    a = install(manager, tmp_path, "Armor A", {rel: "a", "natives/STM/Art/a_only.tex.241106027": "t"})
    b = install(manager, tmp_path, "Armor B", {rel: "b"})
    old = time.time() - 7200
    for path in manager.mods_dir.rglob("*"):  # (installed before the games below)
        os.utime(path, (old, old))
    manager.deploy()

    def play():
        manager.note_launch()
        start = time.time() + 1
        write(game_dir / crashlog.LOG_FILE, log_text(game_dir, start))
        write(game_dir / crashlog.FAULTY_FILE, f'"error" 2 "Invalid file" "{AVP}"\r\n')
        os.utime(game_dir / crashlog.FAULTY_FILE, (start + 60, start + 60))
        manager.collect_crash_evidence()

    def blamed():
        f = precheck.gather(manager, appdata="")
        report = precheck.check(f, precheck.background(f, manager.crash_dir, materials=False))
        return [(i.mods, i.level) for i in report.issues if i.kind == "faulty"]

    play()  # B is later in load order: its copy was read
    assert blamed() == [([b.id], "high")]
    manager.set_enabled(b.id, False)
    manager.deploy()  # A's copy now
    assert blamed() == []
    # C turned on but not applied when the game ran; applied afterwards
    c = install(manager, tmp_path, "Armor C", {rel: "c"})
    for path in manager.mods_dir.rglob("*"):
        os.utime(path, (old, old))
    manager.set_enabled(a.id, False)
    manager.set_enabled(c.id, False)
    manager.deploy()
    manager.set_enabled(c.id, True)
    assert manager.dirty and not (game_dir / rel).exists()
    play()
    assert blamed() == []
    manager.deploy()
    assert (game_dir / rel).read_text() == "c" and blamed() == []


def test_the_manager_writes_only_reframeworks_settings_files(manager, tmp_path, game_dir, monkeypatch):
    import mhwmm.manager

    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(mhwmm.manager, "is_game_running", lambda: False)
    install(manager, tmp_path, "REFramework", {"dinput8.dll": "MZ"})
    manager.deploy()
    with pytest.raises(ValueError):
        manager.set_refw_option(LOOSE, True, path=tmp_path / "elsewhere.txt")
    assert not (tmp_path / "elsewhere.txt").exists()
    target = tmp_path / "Roaming" / "REFramework" / "MonsterHunterWilds" / crashlog.CONFIG_FILE
    assert manager.set_refw_option(LOOSE, True, path=target)  # (REFramework used %APPDATA% the last time)
    assert target.read_bytes() == f"{LOOSE}=true\r\n".encode() and not (game_dir / crashlog.CONFIG_FILE).exists()


def test_edit_issues_are_a_mod_problem(manager, tmp_path):
    """What of a 모드 수정 does not reach the game is one of the mod's ⚠ problems (enabled mods only)."""
    states = [ModState("a", "A", True, edit_issues=["확인할 값 2개", "다시 적용 대기 1개"]),
              ModState("b", "B", False, edit_issues=["확인할 값 1개"])]
    assert precheck.mod_problems(states) == {
        "a": ["모드 수정 내용 일부가 적용되지 않았습니다: 확인할 값 2개 (모드 수정 → 수정 내용)"]}

    mod = install(manager, tmp_path, "Edited", {"natives/STM/a.tex": "a"})
    (state,) = precheck.mod_states(manager)
    assert state.edit_issues == [] and state.stamp == f"{mod.installed_at}|{mod.updated_at}"
    write(manager.mod_dir(mod.id) / "edits.json", "{not json")  # an unreadable record is never used
    manager._invalidate(mod.id)
    mod.edited_at = "2026-10-28T10:00:00"
    (state,) = precheck.mod_states(manager)
    assert state.stamp == mod.stamp == f"{mod.installed_at}|{mod.updated_at}|2026-10-28T10:00:00"
    assert state.edit_issues[0].startswith("모드 수정 기록을 읽지 못해 쓰지 않고 있습니다: 기록이 손상되었습니다")
    assert manager.problems()[mod.id] == [f"모드 수정 내용 일부가 적용되지 않았습니다: {state.edit_issues[0]} "
                                          "(모드 수정 → 수정 내용)"]
    manager.set_enabled(mod.id, False)
    assert precheck.mod_states(manager)[0].edit_issues == [] and manager.problems() == {}
