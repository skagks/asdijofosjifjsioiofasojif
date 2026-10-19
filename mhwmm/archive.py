"""Archive extraction.

zip is handled by Python itself.  rar and 7z need an external extractor; the first
one found of 7-Zip, Bandizip (반디집), py7zr (7z only) and the tar.exe built into
Windows is used, falling back to the next if it fails.
"""

from __future__ import annotations

import locale
import logging
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from . import fsutil

log = logging.getLogger(__name__)

ARCHIVE_EXTS = (".zip", ".7z", ".rar")


class ArchiveError(Exception):
    pass


def is_archive(path) -> bool:
    return Path(path).suffix.lower() in ARCHIVE_EXTS


def find_7zip() -> str | None:
    for name in ("7z", "7za", "7zz"):
        found = shutil.which(name)
        if found:
            return found
    if os.name != "nt":
        return None
    candidates = []
    try:
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for value in ("Path64", "Path"):
                try:
                    with winreg.OpenKey(hive, r"Software\7-Zip") as k:
                        v, _ = winreg.QueryValueEx(k, value)
                        candidates.append(Path(v) / "7z.exe")
                except OSError:
                    pass
    except ImportError:  # pragma: no cover
        pass
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "7-Zip" / "7z.exe")
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def find_bandizip() -> str | None:
    """Bandizip's console extractor (bz.exe)."""
    if os.name != "nt":
        return None
    found = shutil.which("bz")
    if found:
        return found
    candidates = []
    try:
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\Bandizip.exe") as k:
                    v, _ = winreg.QueryValueEx(k, "")
                    candidates.append(Path(v.strip('"')).parent / "bz.exe")
            except OSError:
                pass
    except ImportError:  # pragma: no cover
        pass
    for env, sub in (("ProgramFiles", "Bandizip"), ("ProgramFiles(x86)", "Bandizip"), ("ProgramW6432", "Bandizip"),
                     ("LOCALAPPDATA", "Bandizip"), ("LOCALAPPDATA", "Programs\\Bandizip")):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / sub / "bz.exe")
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def find_windows_tar() -> str | None:
    """tar.exe that ships with Windows; recent Windows 11 builds read rar and 7z with it."""
    if os.name != "nt":
        return None
    tar = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
    return str(tar) if tar.is_file() else None


def _safe_member_path(name: str) -> str | None:
    """Normalise an archive member name; None if it would escape the destination."""
    name = name.replace("\\", "/").lstrip("/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or ":" in parts[0]:
        return None
    return "/".join(parts)


def _zip_member_name(info: zipfile.ZipInfo) -> str:
    """Zip names without the UTF-8 flag are decoded as cp437 by Python; most mods
    made on Korean/Japanese Windows actually use the local code page."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for enc in ("utf-8", "cp949", "shift_jis", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def _extract_zip(archive: Path, dest: Path) -> None:
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"손상되었거나 ZIP 형식이 아닌 파일입니다: {archive.name}") from exc
    with zf:
        for info in zf.infolist():
            rel = _safe_member_path(_zip_member_name(info))
            if rel is None:
                log.warning("안전하지 않은 경로를 건너뜁니다: %s", info.filename)
                continue
            target = dest / rel
            if info.is_dir():
                fsutil.makedirs(target)
                continue
            fsutil.makedirs(target.parent)
            try:
                with zf.open(info) as src, open(fsutil.longpath(target), "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
            except RuntimeError as exc:  # encrypted member
                raise ArchiveError(f"암호가 걸린 압축 파일은 지원하지 않습니다: {archive.name}") from exc
            except (zipfile.BadZipFile, NotImplementedError) as exc:
                raise ArchiveError(f"압축을 풀 수 없습니다 ({info.filename}): {exc}") from exc


def _has_py7zr() -> bool:
    try:
        import py7zr  # type: ignore  # noqa: F401
    except ImportError:
        return False
    return True


def _extract_py7zr(archive: Path, dest: Path) -> None:
    import py7zr  # type: ignore

    try:
        with py7zr.SevenZipFile(archive, mode="r") as z:
            if z.needs_password():
                raise ArchiveError(f"암호가 걸린 압축 파일은 지원하지 않습니다: {archive.name}")
            z.extractall(path=dest)
    except ArchiveError:
        raise
    except Exception as exc:
        raise ArchiveError(f"py7zr로 압축을 풀지 못했습니다: {exc}") from exc


def _decode(data: bytes) -> str:
    for enc in ("utf-8", locale.getpreferredencoding(False), "cp949"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _run_tool(name: str, cmd: list[str]) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        # stdin is closed so a password prompt fails instead of waiting forever
        proc = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, creationflags=flags, timeout=3600)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArchiveError(f"{name} 실행에 실패했습니다: {exc}") from exc
    if proc.returncode != 0:
        err = _decode(proc.stderr).strip() or _decode(proc.stdout).strip()
        raise ArchiveError(f"{name}(으)로 압축을 풀지 못했습니다 (코드 {proc.returncode}): {err[-300:]}")


def _extractors(archive: Path, dest: Path) -> list[tuple[str, object]]:
    tools: list[tuple[str, object]] = []
    seven = find_7zip()
    if seven:
        tools.append(("7-Zip", lambda: _run_tool(
            "7-Zip", [seven, "x", "-y", "-bso0", "-bsp0", "-p", f"-o{dest}", str(archive)])))
    bandizip = find_bandizip()
    if bandizip:
        tools.append(("반디집", lambda: _run_tool(
            "반디집", [bandizip, "x", "-y", "-aoa", f"-o:{dest}", str(archive)])))
    if archive.suffix.lower() == ".7z" and _has_py7zr():
        tools.append(("py7zr", lambda: _extract_py7zr(archive, dest)))
    tar = find_windows_tar()
    if tar:
        tools.append(("Windows 기본 압축 풀기", lambda: _run_tool(
            "Windows 기본 압축 풀기(tar)", [tar, "-xf", str(archive), "-C", str(dest)])))
    return tools


def _is_empty(dest: Path) -> bool:
    for _dirpath, _dirnames, filenames in os.walk(fsutil.longpath(dest)):
        if filenames:
            return False
    return True


def _sanitize_tree(dest: Path) -> None:
    """Drop symlinks an archive tool may have created."""
    for dirpath, dirnames, filenames in os.walk(fsutil.longpath(dest)):
        for name in list(dirnames) + filenames:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                log.warning("심볼릭 링크를 제거합니다: %s", name)
                os.unlink(p)
                if name in dirnames:
                    dirnames.remove(name)


def extract(archive, dest) -> None:
    """Extract ``archive`` into the (new or empty) directory ``dest``."""
    archive = Path(archive)
    dest = Path(dest)
    fsutil.makedirs(dest)
    ext = archive.suffix.lower()
    if ext == ".zip":
        _extract_zip(archive, dest)
        return
    if ext not in ARCHIVE_EXTS:
        raise ArchiveError(f"지원하지 않는 압축 형식입니다: {archive.suffix}")
    tools = _extractors(archive, dest)
    if not tools:
        raise ArchiveError(f"{ext[1:].upper()} 압축을 풀 프로그램이 없습니다. 반디집이나 7-Zip을 설치하세요.")
    errors = []
    for i, (name, run) in enumerate(tools):
        if i:
            fsutil.rmtree(dest)  # drop whatever the failed tool left behind
            fsutil.makedirs(dest)
        try:
            run()
        except ArchiveError as exc:
            log.info("%s", exc)
            errors.append(str(exc))
            continue
        if _is_empty(dest):
            errors.append(f"{name}(으)로 풀었지만 나온 파일이 없습니다.")
            continue
        _sanitize_tree(dest)
        log.info("%s(으)로 압축을 풀었습니다: %s", name, archive.name)
        return
    raise ArchiveError(f"'{archive.name}'의 압축을 풀지 못했습니다. 파일이 손상되었거나 암호가 걸려 있을 수 있습니다.\n\n"
                       + "\n".join(errors))
