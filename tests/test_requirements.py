import io
import json

from mhwmm import nexus, requirements
from mhwmm.requirements import module_key, names_match, provided_modules, required_modules

from conftest import make_tree, make_zip, write


def test_lua_modules_a_script_needs(tmp_path):
    root = make_tree(tmp_path / "m", {
        "reframework/autorun/main.lua": (
            'local cat = require("_CatLib")\n'
            "local util = require 'mylib.util'\n"
            'local s = require("string")\n'            # standard library
            'local own = require("main_helpers")\n'    # shipped with the mod
            'local ok, opt = pcall(require, "Optional")\n'  # optional: only used if present
            '-- require("Commented")\n'
            '--[[ require("Block") ]]\n'),
        "reframework/autorun/main_helpers/init.lua": "return {}",
    })
    files = {rel: root / rel for rel in ("reframework/autorun/main.lua", "reframework/autorun/main_helpers/init.lua")}
    assert required_modules(files) == ["_CatLib", "mylib.util"]
    assert provided_modules(["reframework/autorun/mylib/util.lua", "natives/STM/x.tex"]) == {"mylib/util", "util"}
    assert module_key("MyLib.Util") == module_key("mylib/util") == "mylib/util"
    assert names_match("Lua Bone System", "LuaBoneSystem v1.2") and not names_match("Lua", "Lua Bone System")
    assert requirements.is_reframework("REFramework (nightly)")


def install(manager, path):
    with manager.prepare_install(path) as s:
        return manager.install_variant(s, s.variants[0])


def test_missing_lua_libraries(manager, tmp_path, game_dir):
    app = install(manager, make_zip(tmp_path / "App.zip", {
        "reframework/autorun/app.lua": 'require("_CatLib")\nrequire("mylib.util")'}))
    assert app.lua_requires == ["_CatLib", "mylib.util"]
    assert [(r["name"], r["state"]) for r in manager.missing_requirements(app.id)] == [
        ("_CatLib", "missing"), ("mylib.util", "missing")]

    lib = install(manager, make_zip(tmp_path / "_CatLib.zip", {"reframework/autorun/_CatLib.lua": "return {}"}))
    write(game_dir / "reframework/autorun/mylib/util.lua", "-- installed by hand")
    assert manager.missing_requirements(app.id) == []
    manager.set_enabled(lib.id, False)
    assert [(r["name"], r["state"], r["by"]) for r in manager.missing_requirements(app.id)] == [
        ("_CatLib", "off", "_CatLib")]

    # mods installed by older versions are scanned when first looked at
    app.lua_requires, app.lua_scanned = [], False
    assert [r["name"] for r in manager.requirement_status(app.id)] == ["_CatLib", "mylib.util"] and app.lua_scanned


def test_nexus_requirements(manager, tmp_path, game_dir):
    mod = install(manager, make_zip(tmp_path / "Chen-3664-1-0-1740000000.zip", {"natives/STM/a.mesh": "m"}))
    assert manager.requirement_targets() == [3664]
    manager.set_requirements({3664: [
        {"name": "REFramework", "url": "", "nexus_id": 93, "notes": "", "external": False},
        {"name": "Lua Bone System", "url": nexus.MOD_URL.format(10), "nexus_id": 10, "notes": "물리용", "external": False},
        {"name": "Some Tool", "url": "https://example.com", "nexus_id": 0, "notes": "", "external": True},
    ]})
    assert manager.requirement_targets() == []
    states = {r["name"]: r["state"] for r in manager.requirement_status(mod.id)}
    assert states == {"REFramework": "missing", "Lua Bone System": "missing", "Some Tool": "unknown"}

    write(game_dir / "dinput8.dll", "MZ")
    bones = install(manager, make_zip(tmp_path / "LuaBoneSystem.zip", {"reframework/autorun/bones.lua": "-"}))
    assert bones.nexus_id == 0  # renamed download: found by its name instead
    assert manager.missing_requirements(mod.id) == []

    # the requirements survive a restart, and go away when the mod is linked to another page
    reloaded = type(manager)(manager.data_dir)
    assert len(reloaded.get(mod.id).requirements) == 3
    manager.link_nexus(mod.id, "77")
    assert mod.requirements == [] and manager.requirement_targets() == [77]


class FakeResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data).encode())
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_requirements_from_nexus(monkeypatch):
    sent = []

    def fake_urlopen(request, timeout=0):
        body = json.loads(request.data)
        sent.append((request.full_url, request.get_header("Apikey"), body))
        if "game(" in body["query"]:
            return FakeResponse({"data": {"game": {"id": 7777}}})
        return FakeResponse({"data": {"mods": {"nodes": [
            {"modId": 3664, "modRequirements": {"nexusRequirements": {"nodes": [
                {"modName": "Lua Bone System", "url": "https://www.nexusmods.com/monsterhunterwilds/mods/10",
                 "notes": "", "externalRequirement": False},
                {"modName": "REFramework", "url": "https://github.com/praydog/REFramework-nightly",
                 "notes": "nightly", "externalRequirement": True}]}}},
            {"modId": 5, "modRequirements": None},
        ]}}})

    monkeypatch.setattr(nexus.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(nexus, "_GAME_ID", {})
    found = nexus.fetch_requirements([3664, 5, 3664], api_key="")
    assert found[5] == []
    assert found[3664] == [
        {"name": "Lua Bone System", "url": "https://www.nexusmods.com/monsterhunterwilds/mods/10", "nexus_id": 10,
         "notes": "", "external": False},
        {"name": "REFramework", "url": "https://github.com/praydog/REFramework-nightly", "nexus_id": 0,
         "notes": "nightly", "external": True}]
    assert all(url == nexus.GRAPHQL and key is None for url, key, _ in sent)  # works without an API key
    flt = sent[1][2]["variables"]["filter"]
    assert flt["op"] == "OR" and [f["modId"][0]["value"] for f in flt["filter"]] == ["5", "3664"]
    assert all(f["gameId"][0]["value"] == "7777" for f in flt["filter"])


def test_cli_conflicts_and_missing(manager, tmp_path, capsys):
    from mhwmm.cli import main

    data = str(manager.data_dir)
    for name, files in (("A.zip", {"natives/STM/x.tex": "a"}), ("B.zip", {"natives/STM/x.tex": "b"}),
                        ("App.zip", {"reframework/autorun/app.lua": 'require("_CatLib")'})):
        assert main(["--data-dir", data, "install", str(make_zip(tmp_path / name, files)), "--no-deploy"]) == 0
    assert "필요한 모드 없음: _CatLib (Lua 라이브러리)" in capsys.readouterr().out
    assert main(["--data-dir", data, "conflicts"]) == 0
    out = capsys.readouterr().out
    assert "2. B  >  1. A  (파일 1개)" in out and "natives/STM/x.tex" in out and "적용되지 않는 모드" in out
    assert main(["--data-dir", data, "list"]) == 0
    assert "필요한 모드 없음: _CatLib" in capsys.readouterr().out
