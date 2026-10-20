import io
import json

import pytest

from mhwmm import nexus
from mhwmm.nexus import CheckItem, NexusClient, NexusError, find_update, parse_mod_ref, parse_source_name

from conftest import make_zip


def files_data():
    """Mod with a main file updated twice (100 -> 101 -> 102) and an untouched optional file."""
    return {
        "files": [
            {"file_id": 100, "name": "Main", "version": "1.0", "category_name": "OLD_VERSION", "uploaded_timestamp": 1740000000},
            {"file_id": 101, "name": "Main", "version": "1.1", "category_name": "OLD_VERSION", "uploaded_timestamp": 1745000000},
            {"file_id": 102, "name": "Main", "version": "1.2", "category_name": "MAIN", "uploaded_timestamp": 1750000000},
            {"file_id": 200, "name": "Optional", "version": "1.0", "category_name": "OPTIONAL", "uploaded_timestamp": 1741000000},
        ],
        "file_updates": [
            {"old_file_id": 100, "new_file_id": 101, "new_file_name": "Main-1-1", "uploaded_timestamp": 1745000000},
            {"old_file_id": 101, "new_file_id": 102, "new_file_name": "Main-1-2", "uploaded_timestamp": 1750000000},
        ],
    }


def test_parse_names_and_links():
    assert parse_source_name("Better Armor-1234-1-2-1740000000.zip") == nexus.SourceRef("Better Armor", 1234, "1.2", 1740000000, True)
    iso = parse_source_name("Ardelia 3664 1.3 2026-08-05T20-52Z replacer.zip")
    assert (iso.name, iso.mod_id, iso.version, iso.exact) == ("Ardelia replacer", 3664, "1.3", False)
    assert parse_source_name("_CatLib.zip") is None
    # versions starting with "v" or letters, no version, underscores, and browser duplicates
    for name, version in (("Mod Name-787-v1-0-1740000000.zip", "v1.0"), ("Mod Name-787-1740000000.zip", ""),
                          ("Mod Name-787-TU4-1740000000.7z", "TU4"), ("Mod Name-787-1_2-1740000000.rar", "1.2"),
                          ("Mod Name-787-1-0-1740000000 (1).zip", "1.0")):
        ref = parse_source_name(name)
        assert (ref.name, ref.mod_id, ref.version, ref.uploaded) == ("Mod Name", 787, version, 1740000000), name
    assert parse_source_name("Re-Texture-55-2-0-1740000000.zip").mod_id == 55
    assert parse_source_name("Mod 2025-10-01.zip") is None
    assert parse_mod_ref("https://www.nexusmods.com/monsterhunterwilds/mods/3664?tab=files") == 3664
    assert parse_mod_ref("https://next.nexusmods.com/monsterhunterwilds/mods/12") == 12
    assert parse_mod_ref(" 77 ") == 77
    with pytest.raises(ValueError):
        parse_mod_ref("https://www.nexusmods.com/skyrimspecialedition/mods/12")
    with pytest.raises(ValueError):
        parse_mod_ref("아무 글자")


def test_find_update_follows_the_file_chain():
    update = find_update(files_data(), 1740000000, "1.0")
    assert update["version"] == "1.2" and update["file"] == "Main-1-2" and update["file_id"] == 102
    assert find_update(files_data(), 1750000000, "1.2") == {}  # newest main file
    assert find_update(files_data(), 1741000000, "1.0") == {}  # optional file never replaced
    # minute-precision names still find their file
    assert find_update(files_data(), 1745000000 - 30, "1.1", exact=False)["version"] == "1.2"


def test_find_update_without_a_known_file():
    assert find_update(files_data(), 0, "1.0")["version"] == "1.2"  # compared by version
    assert find_update(files_data(), 0, "1.2") == {}
    assert find_update(files_data(), 0, "") == {}  # nothing to compare with
    assert find_update(files_data(), 1700000000, "special")["version"] == "1.2"  # compared by upload time


class FakeResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data).encode())
        self.headers = {"x-rl-hourly-remaining": "99", "x-rl-daily-remaining": "2400"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_client_requests_and_errors(monkeypatch):
    seen = []

    def fake_urlopen(request, timeout=0):
        seen.append((request.full_url, request.get_header("Apikey")))
        if request.full_url.endswith("/mods/404/files.json"):
            raise nexus.urllib.error.HTTPError(request.full_url, 404, "nf", {}, None)
        if "validate" in request.full_url:
            return FakeResponse({"name": "hunter", "is_premium": False})
        return FakeResponse(files_data())

    monkeypatch.setattr(nexus.urllib.request, "urlopen", fake_urlopen)
    client = NexusClient("secret")
    assert client.validate()["name"] == "hunter"
    items = [CheckItem("a", 1, 1740000000, True, "1.0"), CheckItem("b", 1, 1750000000, True, "1.2"),
             CheckItem("c", 404, 0, True, "1.0")]
    results, problems = nexus.check_mods(client, items)
    assert results == {"a": find_update(files_data(), 1740000000, "1.0"), "b": {}}
    assert len(problems) == 1 and "404" in problems[0]
    assert sum(1 for url, _ in seen if url.endswith("/mods/1/files.json")) == 1  # cached per Nexus mod
    assert all(key == "secret" for _, key in seen) and client.hourly_remaining == "99"

    monkeypatch.setattr(nexus.urllib.request, "urlopen",
                        lambda r, timeout=0: (_ for _ in ()).throw(nexus.urllib.error.HTTPError(r.full_url, 401, "", {}, None)))
    with pytest.raises(NexusError, match="API 키"):
        NexusClient("bad").validate()
    with pytest.raises(NexusError):
        NexusClient("")


def test_manager_tracks_nexus_files(manager, tmp_path):
    archive = make_zip(tmp_path / "Better Armor-1234-1-0-1740000000.zip", {"natives/STM/a.tex": "a"})
    with manager.prepare_install(archive) as session:
        mod = manager.install_variant(session, session.variants[0])
    assert (mod.name, mod.version, mod.nexus_id, mod.nexus_file_time) == ("Better Armor", "1.0", 1234, 1740000000)
    assert mod.nexus_url.endswith("/monsterhunterwilds/mods/1234")

    # older installs are recognised from their original file name
    mod.nexus_id, mod.nexus_file_time = 0, 0
    with manager.prepare_install(make_zip(tmp_path / "_CatLib.zip", {"reframework/autorun/c.lua": "-"})) as session:
        lib = manager.install_variant(session, session.variants[0])
    items = manager.nexus_items()
    assert [(i.key, i.nexus_id, i.uploaded) for i in items] == [(mod.id, 1234, 1740000000)]

    manager.link_nexus(lib.id, "https://www.nexusmods.com/monsterhunterwilds/mods/55")
    assert lib.nexus_id == 55 and len(manager.nexus_items()) == 2

    manager.set_update_results({mod.id: {"version": "1.2", "file": "Main-1-2", "uploaded": "2025-06-15"}, lib.id: {}})
    assert mod.update["version"] == "1.2" and lib.update == {} and mod.nexus_checked
    reloaded = type(manager)(manager.data_dir)
    assert reloaded.get(mod.id).update["version"] == "1.2" and reloaded.get(lib.id).nexus_id == 55

    # installing the new file clears the notice
    newer = make_zip(tmp_path / "v2" / "Better Armor-1234-1-2-1750000000.zip", {"natives/STM/a.tex": "b"})
    with manager.prepare_install(newer) as session:
        updated = manager.install_variant(session, session.variants[0], replace_id=mod.id)
    assert updated.update == {} and updated.nexus_file_time == 1750000000 and updated.version == "1.2"


class FakeClient:
    def __init__(self, recent=None, fail_after=None):
        self.recent, self.fail_after, self.calls = recent, fail_after, []

    def updated(self, period):
        self.calls.append(("updated", period))
        if self.recent is None:
            raise NexusError("no list")
        return self.recent

    def files(self, mod_id):
        if self.fail_after is not None and len([c for c in self.calls if c[0] == "files"]) >= self.fail_after:
            raise NexusError("Nexus 요청 한도를 넘었습니다.")
        self.calls.append(("files", mod_id))
        return files_data()


def test_check_skips_mods_without_new_files(monkeypatch):
    now = 1760000000
    monkeypatch.setattr(nexus.time, "time", lambda: now)
    recent_check = now - 86400
    items = [CheckItem("old", 1, 1740000000, True, "1.0", recent_check),     # nothing new on Nexus
             CheckItem("new", 2, 1740000000, True, "1.0", recent_check),     # Nexus lists a new file
             CheckItem("never", 3, 1740000000, True, "1.0", 0)]              # never checked
    client = FakeClient(recent={2: now - 3600})
    results, problems = nexus.check_mods(client, items)
    assert results["old"] is None and results["new"]["version"] == "1.2" and results["never"]["version"] == "1.2"
    assert [c for c in client.calls if c[0] == "files"] == [("files", 2), ("files", 3)]

    # without the list (or for mods checked long ago) every mod is looked up
    client = FakeClient(recent=None)
    results, _ = nexus.check_mods(client, items)
    assert all(results[k] for k in ("old", "new", "never"))
    client = FakeClient(recent={})
    nexus.check_mods(client, [CheckItem("x", 1, 0, True, "1.0", now - 40 * 86400)])
    assert client.calls == [("files", 1)]

    # hitting the request limit keeps what was found so far
    results, problems = nexus.check_mods(FakeClient(fail_after=1), [CheckItem("a", 1, 0, True, "1.0"),
                                                                    CheckItem("b", 2, 0, True, "1.0")])
    assert list(results) == ["a"] and "1개 모드는 확인하지 못했습니다" in problems[0]
    with pytest.raises(NexusError):
        nexus.check_mods(FakeClient(fail_after=0), [CheckItem("a", 1, 0, True, "1.0")])


def test_manager_keeps_results_and_finds_renamed_mods(manager, tmp_path):
    archive = make_zip(tmp_path / "Better Armor-1234-1-0-1740000000.zip", {"natives/STM/a.tex": "a"})
    with manager.prepare_install(archive) as session:
        mod = manager.install_variant(session, session.variants[0])
    manager.set_update_results({mod.id: {"version": "1.2"}})
    first = mod.nexus_checked
    manager.set_update_results({mod.id: None})  # nothing new since: keep the notice
    assert mod.update == {"version": "1.2"} and mod.nexus_checked >= first
    assert manager.nexus_items()[0].checked == mod.nexus_checked

    manager.rename(mod.id, "내 갑옷")
    assert manager.find_existing("Better Armor", "Better Armor-1234-1-2-1750000000.zip") is mod
    assert manager.find_existing("Better Armor", "Better Armor.zip") is None
    assert manager.find_existing("내 갑옷") is mod


def test_other_files_of_one_nexus_page_are_other_mods(manager, tmp_path):
    archive = make_zip(tmp_path / "Typhoeus - LBS-4926-1-0-1-1754000000.zip", {"natives/STM/t.tex": "t"})
    with manager.prepare_install(archive) as session:
        typhoeus = manager.install_variant(session, session.variants[0])
    assert typhoeus.nexus_id == 4926
    # another mod offered on the same page: not an update of the installed one
    assert manager.find_existing("Eternal Hunt", "Eternal Hunt-4926-1-0-1755000000.zip") is None
    assert manager.find_existing("Typhoeus - LBS", "Eternal Hunt-4926-1-0-1755000000.zip") is None  # same mod name
    # a new version of the same file is, also after renaming the mod (the file's name decides)
    assert manager.find_existing("Typhoeus - LBS", "Typhoeus - LBS-4926-1-1-1756000000.zip") is typhoeus
    manager.rename(typhoeus.id, "티폰")
    assert manager.find_existing("Typhoeus LBS", "Typhoeus_LBS-4926-1-1-1756000000 (1).zip") is typhoeus
    # with both installed, each one's update finds its own mod
    hunt_zip = make_zip(tmp_path / "Eternal Hunt-4926-1-0-1755000000.zip", {"natives/STM/e.tex": "e"})
    with manager.prepare_install(hunt_zip) as session:
        hunt = manager.install_variant(session, session.variants[0])
    assert manager.find_existing("Eternal Hunt", "Eternal Hunt-4926-1-1-1757000000.zip") is hunt
    assert manager.find_existing("x", "Typhoeus - LBS-4926-2-0-1757000000.zip") is typhoeus
    # downloads named with the upload time (a downloader's random suffix is not part of the file's name)
    assert not nexus.other_file("Ardelia 3664 1.3 2026-08-05T20-52Z m2gvlwD60.zip",
                                "Ardelia 3664 1.4 2026-09-01T10-00Z AbCdEfGh1.zip")
    assert nexus.other_file("Ardelia 3664 1.3 2026-08-05T20-52Z m2gvlwD60.zip", "Ardelia Alt 3664 1.0 2026-09-01T10-00Z x.zip")
    assert not nexus.other_file("A-1-1-1754000000.zip", "B-2-1-1754000000.zip")  # other pages
    assert not nexus.other_file("Typhoeus - LBS-4926-1-0-1-1754000000.zip", "Typhoeus - LBS.zip")  # unknown


def test_cli_nexus(manager, tmp_path, monkeypatch, capsys):
    from mhwmm.cli import main

    data = str(manager.data_dir)
    archive = make_zip(tmp_path / "Better Armor-1234-1-0-1740000000.zip", {"natives/STM/a.tex": "a"})
    assert main(["--data-dir", data, "install", str(archive), "--no-deploy"]) == 0
    assert main(["--data-dir", data, "nexus", "check"]) == 1  # no key yet
    assert "API 키" in capsys.readouterr().err

    monkeypatch.setattr(nexus.NexusClient, "validate", lambda self: {"name": "hunter", "is_premium": True})
    monkeypatch.setattr(nexus.NexusClient, "files", lambda self, mod_id: files_data())
    assert main(["--data-dir", data, "nexus", "key", "secret"]) == 0
    assert "hunter" in capsys.readouterr().out
    assert main(["--data-dir", data, "nexus", "check"]) == 0
    out = capsys.readouterr().out
    assert "새 버전: Better Armor  1.0 -> 1.2" in out and "mods/1234?tab=files" in out
    assert main(["--data-dir", data, "list"]) == 0
    assert "(새 버전 1.2)" in capsys.readouterr().out
    assert main(["--data-dir", data, "nexus", "link", "1", "https://www.nexusmods.com/skyrim/mods/1"]) == 1
    assert main(["--data-dir", data, "nexus", "unlink", "1"]) == 0
    assert main(["--data-dir", data, "nexus", "check"]) == 0
    assert "연결된 모드가 없습니다" in capsys.readouterr().out
