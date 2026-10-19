"""REFramework updates from the REFramework-nightly GitHub releases.

Each nightly release (tag ``nightly-01432-<commit>``) ships ``REFramework.zip`` with
one ``dinput8.dll`` for every supported game.  The DLL contains its commit hash as
plain text, so the installed build is found by looking for the commits of recent
releases inside the game's ``dinput8.dll``.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path

from .constants import REFRAMEWORK_DLL, REFW_ORIGIN
from .updater import UpdateError, _open, download_file

log = logging.getLogger(__name__)

REFW_REPO = "praydog/REFramework-nightly"
API_RELEASES = f"https://api.github.com/repos/{REFW_REPO}/releases?per_page=30"
DOWNLOAD_PREFIX = f"https://github.com/{REFW_REPO}/releases/download/"
RELEASES_PAGE = f"https://github.com/{REFW_REPO}/releases"
ASSET_NAME = "REFramework.zip"
REVISION_FILE = "reframework_revision.txt"
ORIGIN = REFW_ORIGIN
TAG_RE = re.compile(r"^nightly-(\d+)-([0-9a-f]{7,40})$", re.IGNORECASE)


@dataclass
class Build:
    tag: str
    number: int
    commit: str
    url: str
    size: int
    sha256: str | None
    page_url: str
    published: str = ""

    @property
    def label(self) -> str:
        return f"nightly {self.number:05d}"


@dataclass
class Status:
    installed: bool  # a dinput8.dll is in the game folder
    latest: Build
    current: Build | None = None  # the installed build, when it is one of the recent ones
    builds: list[Build] = field(default_factory=list)

    @property
    def up_to_date(self) -> bool:
        return self.current is not None and self.current.number >= self.latest.number

    @property
    def behind(self) -> int | None:
        return self.latest.number - self.current.number if self.current else None

    @property
    def needs_action(self) -> bool:
        return not self.up_to_date


def describe_installed(status: Status) -> str:
    if not status.installed:
        return "설치되어 있지 않음"
    if status.current is None:
        return "알 수 없음 (오래된 빌드이거나 REFramework가 아닌 dinput8.dll)"
    if status.up_to_date:
        return f"{status.current.label} (최신)"
    return f"{status.current.label} (최신보다 {status.behind}개 이전 빌드)"


def parse_build(data: dict) -> Build | None:
    m = TAG_RE.match(str(data.get("tag_name") or ""))
    if not m:
        return None
    asset = next((a for a in data.get("assets") or [] if isinstance(a, dict) and a.get("name") == ASSET_NAME), None)
    if not asset:
        return None
    url = str(asset.get("browser_download_url") or "")
    if not url.startswith(DOWNLOAD_PREFIX):
        return None
    digest = str(asset.get("digest") or "")
    return Build(
        tag=m.group(0),
        number=int(m.group(1)),
        commit=m.group(2).lower(),
        url=url,
        size=int(asset.get("size") or 0),
        sha256=digest[7:].lower() if digest.lower().startswith("sha256:") else None,
        page_url=str(data.get("html_url") or RELEASES_PAGE),
        published=str(data.get("published_at") or "")[:10],
    )


def fetch_builds() -> list[Build]:
    """Recent nightly builds that ship REFramework.zip, newest first."""
    try:
        with _open(API_RELEASES, api=True) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise UpdateError("GitHub 요청 한도를 넘었습니다. 잠시 후 다시 시도하세요.") from exc
        raise UpdateError(f"REFramework 릴리즈를 확인하지 못했습니다 (HTTP {exc.code})") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError(f"REFramework 릴리즈를 확인하지 못했습니다: {getattr(exc, 'reason', exc)}") from exc
    builds = [b for b in (parse_build(item) for item in data if isinstance(item, dict)) if b] \
        if isinstance(data, list) else []
    if not builds:
        raise UpdateError("내려받을 수 있는 REFramework 빌드를 찾지 못했습니다.")
    return sorted(builds, key=lambda b: b.number, reverse=True)


def identify(game_dir: Path, builds: list[Build]) -> Build | None:
    """The build installed in ``game_dir`` (None if not installed or older than ``builds``)."""
    dll = Path(game_dir) / REFRAMEWORK_DLL
    try:
        data = dll.read_bytes()
    except OSError:
        return None
    for build in builds:
        if build.commit.encode("ascii") in data:
            return build
    return None


def check(game_dir: Path, builds: list[Build] | None = None) -> Status:
    builds = builds if builds is not None else fetch_builds()
    installed = (Path(game_dir) / REFRAMEWORK_DLL).is_file()
    return Status(installed=installed, latest=builds[0], current=identify(game_dir, builds) if installed else None,
                  builds=builds)


def download(build: Build, dest: Path, progress=None) -> Path:
    if not build.url.startswith(DOWNLOAD_PREFIX):
        raise UpdateError("REFramework 다운로드 주소가 올바르지 않습니다.")
    return download_file(build.url, dest, build.size, build.sha256, progress, "REFramework 내려받는 중...")
