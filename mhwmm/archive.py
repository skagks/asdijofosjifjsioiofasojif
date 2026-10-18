"""Archive extraction (zip natively, 7z/rar through py7zr or 7-Zip)."""

from __future__ import annotations

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


def _extract_py7zr(archive: Path, dest: Path) -> bool:
    try:
        import py7zr  # type: ignore
    except ImportError:
        return False
    try:
        with py7zr.SevenZipFile(archive, mode="r") as z:
            if z.needs_password():
                raise ArchiveError(f"암호가 걸린 압축 파일은 지원하지 않습니다: {archive.name}")
            z.extractall(path=dest)
    except ArchiveError:
        raise
    except Exception as exc:
        raise ArchiveError(f"7z 압축을 풀 수 없습니다: {exc}") from exc
    return True


def _extract_7zip_exe(exe: str, archive: Path, dest: Path) -> None:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    cmd = [exe, "x", "-y", "-bso0", "-bsp0", "-p", f"-o{dest}", str(archive)]
    try:
        proc = subprocess.run(cmd, capture_output=True, creationflags=flags, timeout=3600)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ArchiveError(f"7-Zip 실행에 실패했습니다: {exc}") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip() or proc.stdout.decode("utf-8", "replace").strip()
        raise ArchiveError(f"7-Zip으로 압축을 풀지 못했습니다 (코드 {proc.returncode}): {err[-500:]}")


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
    exe = find_7zip()
    if exe:
        _extract_7zip_exe(exe, archive, dest)
    elif ext == ".7z" and _extract_py7zr(archive, dest):
        pass
    elif ext == ".7z":
        raise ArchiveError("7z 압축을 풀려면 7-Zip을 설치하거나 'pip install py7zr'를 실행하세요.")
    else:
        raise ArchiveError("RAR 압축을 풀려면 7-Zip(https://www.7-zip.org)을 설치하세요.")
    _sanitize_tree(dest)
