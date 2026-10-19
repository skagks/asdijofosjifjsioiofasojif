from mhwmm.analyzer import analyze, clean_archive_name, package_name, raw_variant

from conftest import make_tree


def mapping(variant):
    return dict(variant.files)


def test_natives_at_root(tmp_path):
    base = make_tree(tmp_path / "x", {
        "natives/STM/Art/Model/a.mesh.2109108288": "m",
        "readme.txt": "hi",
    })
    (v,) = analyze(base, "Cool Armor-123-1-2-1740000000")
    assert v.name == "Cool Armor"
    assert v.version == "1.2"
    assert mapping(v) == {"natives/STM/Art/Model/a.mesh.2109108288": "natives/STM/Art/Model/a.mesh.2109108288"}
    assert v.kinds == {"loose"}
    assert "readme.txt" in v.skipped


def test_wrapper_folder_and_case(tmp_path):
    base = make_tree(tmp_path / "x", {"MyMod/Natives/STM/a.tex": "t", "MyMod/preview.png": "p"})
    (v,) = analyze(base, "MyMod")
    assert mapping(v) == {"MyMod/Natives/STM/a.tex": "natives/STM/a.tex"}
    assert v.screenshot == "MyMod/preview.png"


def test_pak_mod(tmp_path):
    base = make_tree(tmp_path / "x", {"Folder/re_chunk_000.pak.sub_000.pak.patch_00x.pak": "p", "Folder/info.txt": "i"})
    (v,) = analyze(base, "Pak Mod")
    assert mapping(v) == {"Folder/re_chunk_000.pak.sub_000.pak.patch_00x.pak": "re_chunk_000.pak.sub_000.pak.patch_00x.pak"}
    assert v.kinds == {"pak"}


def test_fluffy_variants(tmp_path):
    base = make_tree(tmp_path / "x", {
        "Pack/modinfo.ini": "name=Pack\nauthor=Hunter\nversion=2.0\n",
        "Pack/Red/modinfo.ini": "name=Red Option\nscreenshot=shot.jpg\n",
        "Pack/Red/shot.jpg": "j",
        "Pack/Red/natives/STM/a.tex": "red",
        "Pack/Blue/natives/STM/a.tex": "blue",
    })
    variants = analyze(base, "Pack")
    names = [v.name for v in variants]
    assert names == ["Red Option", "Pack - Blue"] or sorted(names) == ["Pack - Blue", "Red Option"]
    by_name = {v.name: v for v in variants}
    red = by_name["Red Option"]
    assert mapping(red) == {"Pack/Red/natives/STM/a.tex": "natives/STM/a.tex"}
    assert red.screenshot == "Pack/Red/shot.jpg"
    assert red.version == "2.0"  # inherited from parent modinfo
    blue = by_name["Pack - Blue"]
    assert blue.version == "2.0"
    assert (red.option, blue.option) == ("Red Option", "Blue")
    assert package_name(variants) == "Pack"
    assert mapping(blue) == {"Pack/Blue/natives/STM/a.tex": "natives/STM/a.tex"}


def test_main_plus_optional(tmp_path):
    base = make_tree(tmp_path / "x", {
        "Mod/natives/STM/a.tex": "a",
        "Mod/Optional/No Glow/natives/STM/b.tex": "b",
    })
    variants = analyze(base, "Shiny")
    assert [v.name for v in variants] == ["Shiny", "Shiny - Optional / No Glow"]
    assert mapping(variants[0]) == {"Mod/natives/STM/a.tex": "natives/STM/a.tex"}


def test_reframework_scripts_without_prefix(tmp_path):
    base = make_tree(tmp_path / "x", {
        "Script/autorun/cool.lua": "--",
        "Script/autorun/cool/util.lua": "--",
        "Script/data/cool/config.json": "{}",
    })
    (v,) = analyze(base, "Cool Script")
    assert mapping(v) == {
        "Script/autorun/cool.lua": "reframework/autorun/cool.lua",
        "Script/autorun/cool/util.lua": "reframework/autorun/cool/util.lua",
        "Script/data/cool/config.json": "reframework/data/cool/config.json",
    }
    assert v.kinds == {"script"}


def test_reframework_full_paths_and_plugin(tmp_path):
    base = make_tree(tmp_path / "x", {
        "reframework/autorun/a.lua": "--",
        "reframework/plugins/b.dll": "dll",
    })
    (v,) = analyze(base, "x")
    assert sorted(v.targets) == ["reframework/autorun/a.lua", "reframework/plugins/b.dll"]


def test_bare_lua_files(tmp_path):
    base = make_tree(tmp_path / "x", {"Thing/thing.lua": "--", "Thing/thing/lib.lua": "--", "Thing/README.md": "r"})
    (v,) = analyze(base, "Thing")
    assert sorted(v.targets) == ["reframework/autorun/thing.lua", "reframework/autorun/thing/lib.lua"]


def test_reframework_itself(tmp_path):
    base = make_tree(tmp_path / "x", {"dinput8.dll": "dll", "DELETE_OPENVR_API_DLL_IF_YOU_WANT_TO_USE_OPENXR": ""})
    (v,) = analyze(base, "MHWILDS")
    assert v.targets == ["dinput8.dll"]
    assert v.kinds == {"reframework"}


def test_unrecognized_and_raw(tmp_path):
    base = make_tree(tmp_path / "x", {"Wrapper/stuff/file.bin": "b", "Wrapper/other.cfg": "c"})
    assert analyze(base, "Odd") == []
    raw = raw_variant(base, "Odd")
    assert dict(raw.files) == {"Wrapper/stuff/file.bin": "stuff/file.bin", "Wrapper/other.cfg": "other.cfg"}


def test_junk_is_ignored(tmp_path):
    base = make_tree(tmp_path / "x", {
        "__MACOSX/natives/STM/._a.tex": "junk",
        "natives/STM/a.tex": "a",
        "natives/STM/Thumbs.db": "junk",
    })
    (v,) = analyze(base, "x")
    assert v.targets == ["natives/STM/a.tex"]


def test_clean_archive_name():
    assert clean_archive_name("Better Armor-1234-1-0-1740000000") == ("Better Armor", "1.0")
    assert clean_archive_name("plain_name") == ("plain_name", "")
