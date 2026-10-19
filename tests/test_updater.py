import hashlib
import io
import json
import subprocess
import sys

import pytest

from mhwmm import updater
from mhwmm.constants import EXE_NAME, GITHUB_REPO
from mhwmm.updater import Release, UpdateError

from conftest import write

BASE = f"https://github.com/{GITHUB_REPO}/releases/download/v9.0.0/"


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def serve(monkeypatch, files: dict):
    def fake_open(url, timeout=15, api=False):
        if url not in files:
            raise updater.urllib.error.URLError("not found")
        return FakeResponse(files[url])
    monkeypatch.setattr(updater, "_open", fake_open)


def release(exe: bytes, sums: bool = True) -> Release:
    return Release(version="9.0.0", tag="v9.0.0", notes="", page_url="", exe_url=BASE + EXE_NAME,
                   exe_size=len(exe), sums_url=BASE + "SHA256SUMS.txt" if sums else None)


def test_version_compare():
    assert updater.is_newer("v1.2.0", "1.1.1")
    assert updater.is_newer("1.10.0", "1.9.9")
    assert not updater.is_newer("v1.1", "1.1.0")
    assert not updater.is_newer("v1.0.9", "1.1.0")
    assert not updater.is_newer("nightly", "1.0.0")


def test_parse_release_and_check(monkeypatch):
    data = {
        "tag_name": "v9.0.0",
        "html_url": "https://github.com/x/y/releases/tag/v9.0.0",
        "body": "### 다운로드\n\n받으세요\n\n### 변경 내역\n\n- 좋아짐\n",
        "assets": [{"name": EXE_NAME, "browser_download_url": BASE + EXE_NAME, "size": 10},
                   {"name": "SHA256SUMS.txt", "browser_download_url": BASE + "SHA256SUMS.txt", "size": 80}],
    }
    serve(monkeypatch, {updater.API_LATEST: json.dumps(data).encode()})
    found = updater.check_for_update()
    assert found.version == "9.0.0" and found.exe_size == 10
    assert found.changes == "- 좋아짐"

    data["tag_name"] = "v0.0.1"
    serve(monkeypatch, {updater.API_LATEST: json.dumps(data).encode()})
    assert updater.check_for_update() is None


def test_download_verifies_checksum(tmp_path, monkeypatch):
    exe = b"MZ new version"
    good = f"{hashlib.sha256(exe).hexdigest()}  {EXE_NAME}\n".encode()
    serve(monkeypatch, {BASE + EXE_NAME: exe, BASE + "SHA256SUMS.txt": good})
    dest = tmp_path / "MHWildsModManager.update.exe"
    seen = []
    updater.download(release(exe), dest, lambda done, total, msg: seen.append(done))
    assert dest.read_bytes() == exe and seen[-1] == len(exe)

    dest.unlink()
    serve(monkeypatch, {BASE + EXE_NAME: exe, BASE + "SHA256SUMS.txt": ("0" * 64 + f"  {EXE_NAME}").encode()})
    with pytest.raises(UpdateError, match="SHA-256"):
        updater.download(release(exe), dest)
    assert not dest.exists() and not (tmp_path / (dest.name + ".part")).exists()


def test_download_rejects_foreign_urls(tmp_path):
    bad = Release("9.0.0", "v9.0.0", "", "", "https://evil.example/x.exe", 1, None)
    with pytest.raises(UpdateError):
        updater.download(bad, tmp_path / "x.exe")


def test_finish_update_replaces_and_restarts(tmp_path, monkeypatch):
    new = write(tmp_path / "MHWildsModManager.update.exe", "new")
    target = write(tmp_path / "MHWildsModManager.exe", "old")
    old_process = subprocess.Popen([sys.executable, "-c", "pass"])
    old_process.wait()  # the old program has already exited
    started = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    real_replace = updater.fsutil.replace_file
    attempts = []

    def flaky_replace(src, dst):  # the old exe is still locked for a moment
        attempts.append(dst)
        if len(attempts) < 3:
            raise PermissionError("in use")
        real_replace(src, dst)

    monkeypatch.setattr(updater.fsutil, "replace_file", flaky_replace)
    monkeypatch.setattr(updater.time, "sleep", lambda s: None)
    assert updater.finish_update(target, old_process.pid, source=new, timeout=5) == 0
    assert target.read_text() == "new"
    assert started == [[str(target)]]
    assert not (tmp_path / "MHWildsModManager.exe.new").exists()

    updater.cleanup_staged(target)
    assert not new.exists() and target.exists()


def test_cli_routes_finish_update(monkeypatch):
    from mhwmm import cli

    calls = []
    monkeypatch.setattr(updater, "finish_update", lambda target, pid: calls.append((target, pid)) or 0)
    assert cli.main(["--finish-update", "C:/x/MHWildsModManager.exe", "1234"]) == 0
    assert calls == [("C:/x/MHWildsModManager.exe", 1234)]
