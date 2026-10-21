from mhwmm import steam
from mhwmm.cli import main

from conftest import make_zip, write

LIBRARYFOLDERS = r'''
"libraryfolders"
{
	"0"
	{
		"path"		"%s"
		"apps" { "228980" "1" }
	}
	"1"
	{
		"path"		"%s"
		"apps"
		{
			"2246340"		"75000000000"
		}
	}
}
'''


def test_parse_vdf_escapes():
    data = steam.parse_vdf('"a" { "path" "D:\\\\Steam Library" // comment\n "q" "say \\"hi\\"" }')
    assert data == {"a": {"path": "D:\\Steam Library", "q": 'say "hi"'}}


def test_find_game_in_secondary_library(tmp_path):
    root = tmp_path / "Steam"
    lib = tmp_path / "Library"
    write(root / "steamapps" / "libraryfolders.vdf",
          LIBRARYFOLDERS % (str(root).replace("\\", "\\\\"), str(lib).replace("\\", "\\\\")))
    write(lib / "steamapps" / "appmanifest_2246340.acf", '"AppState" { "appid" "2246340" "installdir" "MonsterHunterWilds" }')
    write(lib / "steamapps" / "common" / "MonsterHunterWilds" / "MonsterHunterWilds.exe", "exe")
    found = steam.find_game_dir([root], use_system=False)
    assert found == lib / "steamapps" / "common" / "MonsterHunterWilds"


def test_cli_end_to_end(tmp_path, game_dir, capsys):
    data = str(tmp_path / "cli-data")
    archive = make_zip(tmp_path / "Opts.zip", {
        "Opts/A/natives/STM/a.tex": "a",
        "Opts/B/natives/STM/a.tex": "b",
    })
    assert main(["--data-dir", data, "game", str(game_dir)]) == 0
    # one mod with two options, all switched off until the user picks one
    assert main(["--data-dir", data, "install", str(archive)]) == 0
    assert "옵션을 활성화해 주세요" in capsys.readouterr().out
    assert not (game_dir / "natives").exists()
    assert main(["--data-dir", data, "options", "1", "--set", "2"]) == 0
    assert (game_dir / "natives/STM/a.tex").read_text() == "b"
    assert main(["--data-dir", data, "options", "Opts", "--set", "1"]) == 0
    assert (game_dir / "natives/STM/a.tex").read_text() == "a"
    assert main(["--data-dir", data, "list"]) == 0
    assert "옵션 1/2" in capsys.readouterr().out
    assert main(["--data-dir", data, "disable", "1"]) == 0
    assert not (game_dir / "natives").exists()
    assert main(["--data-dir", data, "verify"]) == 0


def test_cli_profiles(tmp_path, game_dir, capsys):
    data = str(tmp_path / "cli-data")
    archive = make_zip(tmp_path / "A.zip", {"natives/STM/a.tex": "a"})
    assert main(["--data-dir", data, "game", str(game_dir)]) == 0
    assert main(["--data-dir", data, "install", str(archive)]) == 0
    assert (game_dir / "natives/STM/a.tex").exists()
    assert main(["--data-dir", data, "profile", "바닐라", "--create", "--empty"]) == 0
    assert not (game_dir / "natives").exists()
    assert main(["--data-dir", data, "profile", "기본"]) == 0
    assert (game_dir / "natives/STM/a.tex").exists()
    capsys.readouterr()
    assert main(["--data-dir", data, "profile"]) == 0
    assert capsys.readouterr().out.split() == ["*", "기본", "바닐라"]
    assert main(["--data-dir", data, "profile", "없는것"]) == 1


def test_cli_crash_records(tmp_path, game_dir, capsys, monkeypatch):
    import time
    import zipfile
    from datetime import datetime

    import pytest

    monkeypatch.setenv("APPDATA", "")
    data = str(tmp_path / "cli-data")
    assert main(["--data-dir", data, "game", str(game_dir)]) == 0
    start = time.time() - 900

    def rec(t, msg, level="info"):
        return f"[{datetime.fromtimestamp(t):%Y-%m-%d %H:%M:%S}.000] [REFramework] [{level}] {msg}"

    write(game_dir / "re2_framework_log.txt", "\r\n".join([
        rec(start, "REFramework entry"), rec(start, f"Current game path: {game_dir}"),
        rec(start + 60, "Exception occurred: c0000005", "error"),
        rec(start + 60, f"Module: 7ff600000000 {game_dir / 'MonsterHunterWilds.exe'}", "error")]) + "\r\n")
    capsys.readouterr()
    assert main(["--data-dir", data, "crash", "list"]) == 0
    listed = capsys.readouterr().out
    assert listed.lstrip().startswith("1. ") and "오류 코드 0xC0000005 (메모리 접근 오류)" in listed
    pasted = write(tmp_path / "error.txt", "RE ENGINE - Aborted\nfile looks corrupted: re_chunk_000.pak.patch_009.pak")
    assert main(["--data-dir", data, "crash", "show", "1", "--text", str(pasted)]) == 0
    shown = capsys.readouterr().out
    assert "의심되는 원인:" in shown and "• 게임 본체에서 오류가 났습니다." in shown and "    근거: " in shown
    assert "붙여 넣은 문구에서 찾은 것:" in shown and "손상된 파일로 보았습니다" in shown
    out = tmp_path / "report.zip"
    assert main(["--data-dir", data, "crash", "report", "--out", str(out)]) == 0
    with zipfile.ZipFile(out) as zf:
        assert "report.txt" in zf.namelist() and not any(n.endswith("state.json") for n in zf.namelist())
    with pytest.raises(SystemExit):
        main(["--data-dir", data, "crash", "show", "없는기록"])

    # REFramework's Lua error logging, only with REFramework there
    assert main(["--data-dir", data, "crash", "lua", "on"]) == 1
    write(game_dir / "dinput8.dll", "refw")
    capsys.readouterr()
    assert main(["--data-dir", data, "crash", "lua", "on"]) == 0
    assert "켜짐" in capsys.readouterr().out
    assert (game_dir / "re2_fw_config.txt").read_bytes() == b"ScriptRunner_LogToDisk=true\r\n"


def test_refw_settings_wait_for_the_game_to_close(tmp_path, game_dir, manager, capsys, monkeypatch):
    """REFramework writes its whole config from memory while the game runs: an edit then would be lost."""
    import pytest

    import mhwmm.manager
    from mhwmm.crashlog import LUA_LOG_KEY
    from mhwmm.deploy import DeployError

    write(game_dir / "dinput8.dll", "refw")
    config = write(game_dir / "re2_fw_config.txt", "FontSize=16\r\nScriptRunner_LogToDisk=false\r\n")
    monkeypatch.setattr(mhwmm.manager, "is_game_running", lambda: True)
    with pytest.raises(DeployError, match="게임이 실행 중입니다"):
        manager.set_refw_option(LUA_LOG_KEY, True)
    data = str(tmp_path / "cli-data")
    assert main(["--data-dir", data, "game", str(game_dir)]) == 0
    assert main(["--data-dir", data, "crash", "lua", "on"]) == 1
    assert "게임이 실행 중입니다" in capsys.readouterr().err
    assert config.read_bytes() == b"FontSize=16\r\nScriptRunner_LogToDisk=false\r\n"
    monkeypatch.setattr(mhwmm.manager, "is_game_running", lambda: False)
    assert manager.set_refw_option(LUA_LOG_KEY, True)
    assert config.read_bytes() == b"FontSize=16\r\nScriptRunner_LogToDisk=true\r\n"
