import subprocess
from pathlib import Path

import pytest

from mhwmm import archive
from mhwmm.archive import ArchiveError

from conftest import write


def use_tools(monkeypatch, seven=None, bandizip=None, tar=None):
    monkeypatch.setattr(archive, "find_7zip", lambda: seven)
    monkeypatch.setattr(archive, "find_bandizip", lambda: bandizip)
    monkeypatch.setattr(archive, "find_windows_tar", lambda: tar)
    monkeypatch.setattr(archive, "_has_py7zr", lambda: False)


def output_dir(cmd: list[str]) -> Path:
    for arg in cmd:
        if arg.startswith("-o:"):
            return Path(arg[3:])
        if arg.startswith("-o"):
            return Path(arg[2:])
    return Path(cmd[cmd.index("-C") + 1])


def test_rar_with_bandizip_only(tmp_path, monkeypatch):
    use_tools(monkeypatch, bandizip=r"C:\Program Files\Bandizip\bz.exe")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs["stdin"] is subprocess.DEVNULL
        write(output_dir(cmd) / "natives" / "a.tex", "x")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(archive.subprocess, "run", fake_run)
    rar = write(tmp_path / "mod.rar", "rar")
    out = tmp_path / "out"
    archive.extract(rar, out)
    assert calls == [[r"C:\Program Files\Bandizip\bz.exe", "x", "-y", "-aoa", f"-o:{out}", str(rar)]]
    assert (out / "natives" / "a.tex").exists()


def test_falls_back_to_the_next_tool(tmp_path, monkeypatch):
    use_tools(monkeypatch, seven="7z.exe", bandizip="bz.exe", tar="tar.exe")
    used = []

    def fake_run(cmd, **kwargs):
        used.append(Path(cmd[0]).name)
        out = output_dir(cmd)
        if cmd[0] == "7z.exe":  # leaves a partial file behind and fails
            write(out / "partial.tmp", "junk")
            return subprocess.CompletedProcess(cmd, 2, b"", "지원하지 않는 형식".encode("cp949"))
        write(out / "natives" / "b.tex", "ok")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(archive.subprocess, "run", fake_run)
    out = tmp_path / "out"
    archive.extract(write(tmp_path / "mod.7z", "7z"), out)
    assert used == ["7z.exe", "bz.exe"]
    assert (out / "natives" / "b.tex").exists()
    assert not (out / "partial.tmp").exists()


def test_reports_every_failure(tmp_path, monkeypatch):
    use_tools(monkeypatch, bandizip="bz.exe", tar="tar.exe")
    monkeypatch.setattr(archive.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"bad archive"))
    with pytest.raises(ArchiveError) as info:
        archive.extract(write(tmp_path / "broken.rar", "x"), tmp_path / "out")
    message = str(info.value)
    assert "반디집" in message and "tar" in message and "bad archive" in message


def test_no_extractor_installed(tmp_path, monkeypatch):
    use_tools(monkeypatch)
    with pytest.raises(ArchiveError, match="반디집이나 7-Zip"):
        archive.extract(write(tmp_path / "mod.rar", "x"), tmp_path / "out")


def test_tool_that_extracts_nothing_counts_as_failure(tmp_path, monkeypatch):
    use_tools(monkeypatch, tar="tar.exe")
    monkeypatch.setattr(archive.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, b"", b""))
    with pytest.raises(ArchiveError, match="나온 파일이 없습니다"):
        archive.extract(write(tmp_path / "mod.rar", "x"), tmp_path / "out")
