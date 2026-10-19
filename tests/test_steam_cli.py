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
