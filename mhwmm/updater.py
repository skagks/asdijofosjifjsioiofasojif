"""Update the program from GitHub Releases.

A running exe cannot overwrite itself, so the new exe is downloaded next to the
current one as ``MHWildsModManager.update.exe`` and started with
``--finish-update <target> <pid>``.  It waits for the old program to exit, copies
itself over the old exe, starts it and quits; the next start deletes the copy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, fsutil
from .constants import EXE_NAME, GITHUB_REPO, RELEASES_URL

log = logging.getLogger(__name__)

API_RELEASES = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=50"
DOWNLOAD_PREFIX = f"https://github.com/{GITHUB_REPO}/releases/download/"
USER_AGENT = f"MHWildsModManager/{__version__}"
STAGED_SUFFIX = ".update.exe"


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    page_url: str
    exe_url: str | None
    exe_size: int
    sums_url: str | None
    published: str = ""  # YYYY-MM-DD
    # Every release newer than the running version, newest first (filled by check_for_update).
    history: list["Release"] = field(default_factory=list)

    @property
    def changes(self) -> str:
        """The "변경 내역" part of the release notes (without the download notes)."""
        head, sep, tail = self.notes.partition("### 변경 내역")
        return (tail if sep else head).strip()

    @property
    def plain_changes(self) -> str:
        """``changes`` as plain text: markdown emphasis removed, wrapped list items joined."""
        lines: list[str] = []
        for raw in self.changes.replace("**", "").replace("`", "").splitlines():
            stripped = raw.strip()
            if lines and lines[-1].strip() and stripped and not stripped.startswith(("-", "*", "#")):
                lines[-1] = lines[-1].rstrip() + " " + stripped
            else:
                lines.append(raw.rstrip())
        return "\n".join(lines).strip()

    @property
    def sort_key(self) -> tuple[int, ...]:
        return parse_version(self.version) or ()


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.match(r"^v?(\d+(?:\.\d+)*)", str(text).strip(), re.IGNORECASE)
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def _open(url: str, timeout: float = 15, api: bool = False):
    headers = {"User-Agent": USER_AGENT}
    if api:
        headers["Accept"] = "application/vnd.github+json"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def parse_release(data: dict) -> Release:
    tag = str(data.get("tag_name") or "")
    assets = {a.get("name"): a for a in data.get("assets") or [] if isinstance(a, dict)}
    exe = assets.get(EXE_NAME) or {}
    sums = assets.get("SHA256SUMS.txt") or {}
    return Release(
        version=tag.lstrip("vV"),
        tag=tag,
        notes=str(data.get("body") or ""),
        page_url=str(data.get("html_url") or RELEASES_URL),
        exe_url=exe.get("browser_download_url"),
        exe_size=int(exe.get("size") or 0),
        sums_url=sums.get("browser_download_url"),
        published=str(data.get("published_at") or "")[:10],
    )


def fetch_releases() -> list[Release]:
    """Published (non-draft, non-prerelease) releases, newest version first."""
    try:
        with _open(API_RELEASES, api=True) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("아직 공개된 릴리즈가 없습니다.") from exc
        if exc.code in (403, 429):
            raise UpdateError("GitHub 요청 한도를 넘었습니다. 잠시 후 다시 시도하세요.") from exc
        raise UpdateError(f"GitHub 응답 오류 (HTTP {exc.code})") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError(f"업데이트 정보를 가져오지 못했습니다: {getattr(exc, 'reason', exc)}") from exc
    if not isinstance(data, list):
        raise UpdateError("GitHub 응답을 이해하지 못했습니다.")
    releases = [parse_release(item) for item in data
                if isinstance(item, dict) and not item.get("draft") and not item.get("prerelease")]
    releases = [r for r in releases if parse_version(r.version)]
    if not releases:
        raise UpdateError("아직 공개된 릴리즈가 없습니다.")
    return sorted(releases, key=lambda r: r.sort_key, reverse=True)


def check_for_update(current: str = __version__) -> Release | None:
    """The newest release if it is newer than ``current``, with every release in
    between in ``history`` so all of their changes can be shown; else None."""
    newer = [r for r in fetch_releases() if is_newer(r.version, current)]
    if not newer:
        return None
    latest = newer[0]
    latest.history = newer
    return latest


def can_self_update() -> bool:
    """Only the packaged Windows exe can replace itself."""
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def current_exe() -> Path:
    return Path(sys.executable).resolve()


def staged_path(exe: Path) -> Path:
    return exe.with_name(exe.stem + STAGED_SUFFIX)


def _check_url(url: str | None) -> str:
    if not url or not url.startswith(DOWNLOAD_PREFIX):
        raise UpdateError("릴리즈에 내려받을 실행 파일이 없습니다.")
    return url


def _expected_sha256(url: str) -> str | None:
    try:
        with _open(_check_url(url)) as resp:
            text = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(f"체크섬 파일을 받지 못했습니다: {getattr(exc, 'reason', exc)}") from exc
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == EXE_NAME and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


def download_file(url: str, dest: Path, size: int = 0, sha256: str | None = None, progress=None,
                  label: str = "다운로드 중...") -> Path:
    """Download ``url`` to ``dest`` through a .part file, checking size and SHA-256 when known."""
    progress = progress or (lambda done, total, msg: None)
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    done = 0
    try:
        with _open(url, timeout=60) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or size or 0)
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                percent = f" {done * 100 // total}%" if total else ""
                progress(done, total or done, f"{label}{percent}")
    except PermissionError:
        fsutil.remove_file(part)
        raise
    except (urllib.error.URLError, OSError) as exc:
        fsutil.remove_file(part)
        raise UpdateError(f"내려받지 못했습니다: {getattr(exc, 'reason', exc)}") from exc
    if size and done != size:
        fsutil.remove_file(part)
        raise UpdateError("내려받은 파일 크기가 맞지 않습니다. 다시 시도하세요.")
    if sha256 and digest.hexdigest() != sha256.lower():
        fsutil.remove_file(part)
        raise UpdateError("내려받은 파일의 SHA-256 값이 맞지 않습니다. 다시 시도하세요.")
    fsutil.replace_file(part, dest)
    return dest


def download(release: Release, dest: Path, progress=None) -> Path:
    """Download and verify the release exe to ``dest``."""
    url = _check_url(release.exe_url)
    expected = _expected_sha256(release.sums_url) if release.sums_url else None
    try:
        download_file(url, dest, release.exe_size, expected, progress, "업데이트 다운로드 중...")
    except PermissionError as exc:
        raise UpdateError(f"프로그램 폴더({Path(dest).parent})에 쓸 수 없습니다. "
                          "MHWildsModManager.exe를 바탕 화면 같은 사용자 폴더로 옮긴 뒤 다시 시도하세요.") from exc
    log.info("업데이트 v%s를 내려받았습니다.", release.version)
    return Path(dest)


def _detached_flags() -> int:
    return getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def launch_finisher(staged: Path, target: Path) -> None:
    """Start the downloaded exe so it replaces ``target`` once this process exits."""
    subprocess.Popen([str(staged), "--finish-update", str(target), str(os.getpid())],
                     cwd=str(target.parent), close_fds=True, creationflags=_detached_flags())


def _wait_for_exit(pid: int, timeout: float) -> None:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if handle:
            kernel32.WaitForSingleObject(handle, int(timeout * 1000))
            kernel32.CloseHandle(handle)
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        time.sleep(0.2)


def _show_error(message: str) -> None:
    log.error(message)
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "업데이트 실패", 0x10)  # type: ignore[attr-defined]
        except Exception:
            pass


def finish_update(target, pid: int, source=None, timeout: float = 90, launch: bool = True) -> int:
    """Run from the downloaded exe: copy it over ``target`` and start ``target``."""
    source = Path(source or sys.executable)
    target = Path(target)
    _wait_for_exit(pid, timeout)
    temp = target.with_name(target.name + ".new")
    deadline = time.time() + timeout
    while True:
        try:
            fsutil.copy_file(source, temp)
            # The old exe stays locked until every process of the old version is gone.
            fsutil.replace_file(temp, target)
            break
        except OSError as exc:
            if time.time() > deadline:
                fsutil.remove_file(temp)
                _show_error(f"업데이트를 적용하지 못했습니다: {exc}\n\n{source} 파일을 직접 {target.name}(으)로 바꿔 주세요.")
                return 1
            time.sleep(0.5)
    if launch:
        subprocess.Popen([str(target)], cwd=str(target.parent), close_fds=True, creationflags=_detached_flags())
    return 0


def cleanup_staged(exe: Path | None = None) -> None:
    """Delete the copy left behind by a finished update."""
    exe = Path(exe) if exe else current_exe()
    staged = staged_path(exe)
    if fsutil.same_path(staged, exe):
        return
    for path in (staged, staged.with_name(staged.name + ".part")):
        try:
            fsutil.remove_file(path)
        except OSError:
            pass  # still exiting; removed on a later start
