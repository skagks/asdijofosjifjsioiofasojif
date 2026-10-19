import hashlib
import io
import json
import zipfile

import pytest

from mhwmm import refw, updater
from mhwmm.cli import main
from mhwmm.updater import UpdateError

from conftest import make_zip, write

COMMITS = {1432: "d052e8fd3fd41d0a1a33394fcf6e6c19be39b1ee", 1431: "08d6e3f698daeaa066e240ee14cf33c54b91ff4b",
           1420: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}


def fake_dll(commit: str) -> bytes:
    return b"MZ\x90\x00" + b"\x00" * 64 + commit.encode() + b"\x00" * 64


def ref_zip(commit: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("dinput8.dll", fake_dll(commit))
        zf.writestr("reframework_revision.txt", commit + "\r\n")
    return buf.getvalue()


def api_release(number: int, payload: bytes | None = None, asset=refw.ASSET_NAME):
    tag = f"nightly-{number:05d}-{COMMITS[number]}"
    url = f"{refw.DOWNLOAD_PREFIX}{tag}/{asset}"
    digest = f"sha256:{hashlib.sha256(payload).hexdigest()}" if payload else None
    return {"tag_name": tag, "html_url": f"https://github.com/{refw.REFW_REPO}/releases/tag/{tag}",
            "published_at": "2026-10-11T01:59:00Z",
            "assets": [{"name": asset, "size": len(payload or b"x"), "browser_download_url": url, "digest": digest}]}


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def github(monkeypatch):
    """Serve the newest build (1432) plus two older ones from a fake GitHub."""
    latest_zip = ref_zip(COMMITS[1432])
    releases = [api_release(1431), api_release(1432, latest_zip), api_release(1420),
                {"tag_name": "v1.99-36a9a93", "assets": []}]
    files = {refw.API_RELEASES: json.dumps(releases).encode(),
             f"{refw.DOWNLOAD_PREFIX}nightly-01432-{COMMITS[1432]}/{refw.ASSET_NAME}": latest_zip}

    def fake_open(url, timeout=15, api=False):
        if url not in files:
            raise updater.urllib.error.URLError("not found")
        return FakeResponse(files[url])

    monkeypatch.setattr(refw, "_open", fake_open)
    monkeypatch.setattr(updater, "_open", fake_open)
    return files


def test_builds_and_installed_version(github, game_dir):
    builds = refw.fetch_builds()
    assert [b.number for b in builds] == [1432, 1431, 1420]
    assert builds[0].label == "nightly 01432" and builds[0].sha256 and builds[0].published == "2026-10-11"

    status = refw.check(game_dir)
    assert not status.installed and status.needs_action
    assert refw.describe_installed(status) == "설치되어 있지 않음"

    write(game_dir / "dinput8.dll", fake_dll(COMMITS[1420]))
    status = refw.check(game_dir)
    assert status.current.number == 1420 and status.behind == 12 and not status.up_to_date
    assert "12개 이전" in refw.describe_installed(status)

    write(game_dir / "dinput8.dll", b"MZ some other proxy dll")
    assert refw.check(game_dir).current is None


def test_install_and_update_refw(github, manager, game_dir, tmp_path):
    write(game_dir / "dinput8.dll", fake_dll(COMMITS[1420]))  # put there by hand
    with manager.prepare_install(make_zip(tmp_path / "A.zip", {"natives/STM/a.tex": "a"})) as s:
        manager.install_variant(s, s.variants[0])

    status = refw.check(game_dir)
    archive = tmp_path / "REFramework.zip"
    refw.download(status.latest, archive)
    mod = manager.install_refw(archive, status.latest.label, "REFramework.zip")
    assert mod.origin == "reframework" and mod.name == "REFramework" and mod.version == "nightly 01432"
    assert manager.mods[0].id == mod.id and mod.enabled
    assert manager.mod_files(mod.id) == ["dinput8.dll"]  # only the DLL, as advised without VR
    report = manager.deploy()
    assert report.backed_up == 1  # the hand-installed DLL is kept
    assert refw.check(game_dir).up_to_date

    # installing again replaces the same mod
    again = manager.install_refw(archive, "nightly 01432", "REFramework.zip")
    assert again.id == mod.id and len(manager.mods) == 2

    manager.set_enabled(mod.id, False)
    manager.deploy()
    assert refw.check(game_dir).current.number == 1420  # the original came back


def test_refw_download_checks_digest(github, tmp_path):
    build = refw.fetch_builds()[0]
    build.sha256 = "0" * 64
    with pytest.raises(UpdateError, match="SHA-256"):
        refw.download(build, tmp_path / "r.zip")


def test_cli_refw(github, tmp_path, game_dir, capsys):
    data = str(tmp_path / "cli-data")
    assert main(["--data-dir", data, "game", str(game_dir)]) == 0
    assert main(["--data-dir", data, "refw"]) == 0
    assert "설치되어 있지 않음" in capsys.readouterr().out
    assert main(["--data-dir", data, "refw", "--install"]) == 0
    assert (game_dir / "dinput8.dll").read_bytes() == fake_dll(COMMITS[1432])
    capsys.readouterr()
    assert main(["--data-dir", data, "refw", "--install"]) == 0
    assert "이미 최신" in capsys.readouterr().out

