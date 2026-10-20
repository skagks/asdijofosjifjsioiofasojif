"""Nexus Mods: recognise downloaded files and check installed mods for updates.

Uses the public v1 API with the user's personal API key (Nexus account settings ->
API Keys).  Files downloaded from Nexus are named
``<name>-<mod id>-<version>-<upload time>``; the upload time identifies the exact
file, and the mod's ``file_updates`` list says which newer file replaced it.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from . import __version__

log = logging.getLogger(__name__)

GAME = "monsterhunterwilds"
API = "https://api.nexusmods.com/v1"
GRAPHQL = "https://api.nexusmods.com/v2/graphql"  # public: works without an API key
MOD_URL = f"https://www.nexusmods.com/{GAME}/mods/{{}}"
FILES_URL = MOD_URL + "?tab=files"
API_KEY_URL = "https://next.nexusmods.com/settings/api-keys"
APP_NAME = "MHWildsModManager"
ISO_TOLERANCE = 90  # seconds; some names only carry the upload minute
RECENT_PERIOD = ("1m", 28 * 86400)  # updated.json period and how far back it reaches (a bit less than a month)
CLOCK_SLACK = 3600  # our clock and Nexus' may differ

_ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar", ".pak")
# Better Armor-1234-1-2-1740000000
_CLASSIC = re.compile(r"^(?P<name>.+?)-(?P<id>\d+)-(?P<ver>\d[0-9A-Za-z]*(?:-[0-9A-Za-z]+)*)-(?P<ts>\d{9,11})$")
# Ardelia 3664 1.3 2026-08-05T20-52Z ...
_ISO = re.compile(r"^(?P<name>.+?)[ _-]+(?P<id>\d+)[ _-]+v?(?P<ver>\d[0-9A-Za-z.]*)[ _-]+"
                  r"(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}(?:-\d{2})?Z)(?P<rest>.*)$")
_URL = re.compile(r"nexusmods\.com/(?P<game>[A-Za-z0-9_]+)/mods/(?P<id>\d+)", re.IGNORECASE)


class NexusError(Exception):
    pass


class NexusNotFound(NexusError):
    pass


@dataclass
class SourceRef:
    name: str
    mod_id: int
    version: str
    uploaded: int  # unix time of the file's upload (0 if unknown)
    exact: bool  # uploaded is to the second


def parse_source_name(name: str) -> SourceRef | None:
    """Mod id, version and upload time from a Nexus download's file name."""
    stem = name.strip()
    if stem.lower().endswith(_ARCHIVE_SUFFIXES):
        stem = stem.rsplit(".", 1)[0]
    m = _CLASSIC.match(stem)
    if m:
        return SourceRef(m["name"].strip(" _-") or stem, int(m["id"]), m["ver"].replace("-", "."), int(m["ts"]), True)
    m = _ISO.match(stem)
    if m:
        parts = [int(x) for x in re.findall(r"\d+", m["iso"])]
        try:
            when = datetime(*parts[:5], *(parts[5:6] or [0]), tzinfo=timezone.utc)
        except ValueError:
            return None
        name = " ".join(x for x in (m["name"].strip(" _-"), m["rest"].strip(" _-")) if x)
        return SourceRef(name or stem, int(m["id"]), m["ver"], int(when.timestamp()), False)
    return None


def parse_mod_ref(text: str) -> int:
    """Mod id from a Nexus mod page link or a bare number."""
    text = text.strip()
    if text.isdigit() and int(text) > 0:
        return int(text)
    m = _URL.search(text)
    if m:
        if m["game"].lower() != GAME:
            raise ValueError("몬스터 헌터 와일즈 모드 링크가 아닙니다.")
        return int(m["id"])
    raise ValueError("Nexus 모드 페이지 주소나 모드 번호를 알아볼 수 없습니다.\n예: https://www.nexusmods.com/monsterhunterwilds/mods/1234")


def _version_key(text: str) -> tuple[int, ...] | None:
    m = re.match(r"^v?(\d+(?:\.\d+)*)", str(text or "").strip(), re.IGNORECASE)
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _newer_version(candidate: str, current: str) -> bool:
    a, b = _version_key(candidate), _version_key(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def _date(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def find_update(files_data: dict, uploaded: int, version: str, exact: bool = True) -> dict:
    """What replaces the installed file: {"version", "file", "uploaded", "file_id"} or {} if up to date."""
    files = [f for f in files_data.get("files") or [] if isinstance(f, dict)]
    by_id = {f.get("file_id"): f for f in files}
    current = None
    if uploaded:
        tolerance = 1 if exact else ISO_TOLERANCE
        matches = [f for f in files if abs(int(f.get("uploaded_timestamp") or 0) - uploaded) <= tolerance]
        current = matches[0] if len(matches) == 1 else None
    if current is not None:
        replaced_by = {u.get("old_file_id"): u for u in files_data.get("file_updates") or [] if isinstance(u, dict)}
        file_id, newest, seen = current.get("file_id"), None, set()
        while file_id in replaced_by and file_id not in seen:
            seen.add(file_id)
            newest = replaced_by[file_id]
            file_id = newest.get("new_file_id")
        if newest is None:
            return {}
        target = by_id.get(file_id, {})
        return {
            "version": str(target.get("version") or target.get("mod_version") or ""),
            "file": str(newest.get("new_file_name") or target.get("name") or ""),
            "uploaded": _date(newest.get("uploaded_timestamp") or target.get("uploaded_timestamp")),
            "file_id": file_id,
        }
    # The installed file is unknown: compare with the newest main file instead.
    main = [f for f in files if str(f.get("category_name") or "").upper() == "MAIN"] or \
        [f for f in files if str(f.get("category_name") or "").upper() not in ("OLD_VERSION", "ARCHIVED", "DELETED")]
    if not main:
        return {}
    latest = max(main, key=lambda f: int(f.get("uploaded_timestamp") or 0))
    latest_version = str(latest.get("version") or latest.get("mod_version") or "")
    newer = _newer_version(latest_version, version) if version else False
    if not version or _version_key(version) is None or _version_key(latest_version) is None:
        newer = bool(uploaded) and int(latest.get("uploaded_timestamp") or 0) > uploaded + ISO_TOLERANCE
    if not newer:
        return {}
    return {"version": latest_version, "file": str(latest.get("name") or ""),
            "uploaded": _date(latest.get("uploaded_timestamp")), "file_id": latest.get("file_id")}


class NexusClient:
    def __init__(self, api_key: str):
        self.api_key = (api_key or "").strip()
        if not self.api_key:
            raise NexusError("Nexus API 키가 없습니다. 설정에서 키를 넣어 주세요.")
        self.hourly_remaining: str | None = None
        self.daily_remaining: str | None = None

    def _get(self, path: str):
        data, headers = _request(API + path, self.api_key)
        self.hourly_remaining = headers.get("x-rl-hourly-remaining")
        self.daily_remaining = headers.get("x-rl-daily-remaining")
        return data

    def validate(self) -> dict:
        data = self._get("/users/validate.json")
        return data if isinstance(data, dict) else {}

    def files(self, mod_id: int) -> dict:
        data = self._get(f"/games/{GAME}/mods/{int(mod_id)}/files.json")
        return data if isinstance(data, dict) else {}

    def updated(self, period: str) -> dict[int, int]:
        """{Nexus mod id: last file upload} for mods with new files in the period (1d, 1w, 1m)."""
        data = self._get(f"/games/{GAME}/mods/updated.json?period={period}")
        result = {}
        for entry in data if isinstance(data, list) else []:
            try:
                result[int(entry["mod_id"])] = int(entry.get("latest_file_update") or 0)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return result


def _request(url: str, api_key: str = "", body: dict | None = None):
    """JSON GET (or POST of ``body``). Returns (data, response headers)."""
    headers = {
        "Application-Name": APP_NAME,
        "Application-Version": __version__,
        "User-Agent": f"{APP_NAME}/{__version__}",
        "Accept": "application/json",
    }
    if api_key:
        headers["apikey"] = api_key
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            return json.load(resp), resp.headers
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise NexusError("Nexus API 키가 올바르지 않습니다. 설정에서 키를 다시 확인하세요.") from exc
        if exc.code == 404:
            raise NexusNotFound("Nexus에서 찾을 수 없습니다 (삭제되었거나 숨겨진 모드일 수 있습니다).") from exc
        if exc.code == 429:
            raise NexusError("Nexus 요청 한도를 넘었습니다. 한 시간쯤 뒤에 다시 시도하세요.") from exc
        raise NexusError(f"Nexus 응답 오류 (HTTP {exc.code})") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise NexusError(f"Nexus에 연결하지 못했습니다: {getattr(exc, 'reason', exc)}") from exc


def graphql(query: str, variables: dict, api_key: str = "") -> dict:
    data, _ = _request(GRAPHQL, api_key, {"query": query, "variables": variables})
    if not isinstance(data, dict):
        raise NexusError("Nexus 응답을 읽지 못했습니다.")
    if data.get("errors") and not data.get("data"):
        message = "; ".join(str(e.get("message")) for e in data["errors"] if isinstance(e, dict))
        raise NexusError(f"Nexus 응답 오류: {message}")
    return data.get("data") or {}


_GAME_ID: dict[str, int] = {}
_REQUIREMENTS_QUERY = """
query ($filter: ModsFilter, $count: Int) {
  mods(filter: $filter, count: $count) {
    nodes { modId modRequirements { nexusRequirements { nodes { modName url notes externalRequirement } } } }
  }
}"""
REQUIREMENTS_BATCH = 50


def game_id(api_key: str = "") -> int:
    if GAME not in _GAME_ID:
        data = graphql("query ($d: String!) { game(domainName: $d) { id } }", {"d": GAME}, api_key)
        try:
            _GAME_ID[GAME] = int(data["game"]["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NexusError("Nexus에서 몬스터 헌터 와일즈를 찾지 못했습니다.") from exc
    return _GAME_ID[GAME]


def requirement_from_node(node: dict) -> dict | None:
    name = str(node.get("modName") or "").strip()
    url = str(node.get("url") or "").strip()
    if not name and not url:
        return None
    m = _URL.search(url)
    same_game = bool(m) and m["game"].lower() == GAME
    return {
        "name": name or url,
        "url": url,
        "nexus_id": int(m["id"]) if same_game else 0,
        "notes": str(node.get("notes") or "").strip(),
        "external": bool(node.get("externalRequirement")) or not same_game,
    }


def fetch_requirements(mod_ids: list[int], api_key: str = "") -> dict[int, list[dict]]:
    """Requirements listed on the Nexus pages of ``mod_ids`` ({mod id: [requirement]})."""
    ids = sorted({int(i) for i in mod_ids if int(i) > 0})
    result: dict[int, list[dict]] = {}
    if not ids:
        return result
    gid = game_id(api_key)
    for start in range(0, len(ids), REQUIREMENTS_BATCH):
        chunk = ids[start:start + REQUIREMENTS_BATCH]
        flt = {"op": "OR", "filter": [{"modId": [{"value": str(i), "op": "EQUALS"}],
                                       "gameId": [{"value": str(gid), "op": "EQUALS"}]} for i in chunk]}
        data = graphql(_REQUIREMENTS_QUERY, {"filter": flt, "count": len(chunk)}, api_key)
        for node in ((data.get("mods") or {}).get("nodes") or []):
            if not isinstance(node, dict):
                continue
            try:
                mod_id = int(node.get("modId"))
            except (TypeError, ValueError):
                continue
            reqs = (((node.get("modRequirements") or {}).get("nexusRequirements") or {}).get("nodes") or [])
            result[mod_id] = [r for r in (requirement_from_node(n) for n in reqs if isinstance(n, dict)) if r]
    return result


@dataclass
class CheckItem:
    key: str  # our mod id
    nexus_id: int
    uploaded: int
    exact: bool
    version: str
    checked: int = 0  # last successful check (0 = never)


def check_mods(client: NexusClient, items: list[CheckItem], progress=None) -> tuple[dict[str, dict | None], list[str]]:
    """Look each item up on Nexus.

    Returns ({mod key: update, {} if up to date, None if no new files since the last check}, problems).
    Mods checked within the last month are only looked up when Nexus lists them as updated since.
    """
    progress = progress or (lambda done, total, msg: None)
    results: dict[str, dict | None] = {}
    problems: list[str] = []
    cache: dict[int, dict] = {}
    recent: dict[int, int] | None = None
    period, reach = RECENT_PERIOD
    since = time.time() - reach
    if any(item.checked > since for item in items):
        try:
            recent = client.updated(period)
        except NexusError as exc:
            log.debug("최근 업데이트 목록을 받지 못했습니다: %s", exc)
    for i, item in enumerate(items):
        if recent is not None and item.checked > since and recent.get(item.nexus_id, 0) < item.checked - CLOCK_SLACK:
            results[item.key] = None
            continue
        progress(i, len(items), f"모드 업데이트 확인 중... {i + 1}/{len(items)}")
        try:
            if item.nexus_id not in cache:
                cache[item.nexus_id] = client.files(item.nexus_id)
        except NexusNotFound as exc:
            problems.append(f"Nexus 모드 {item.nexus_id}: {exc}")
            continue
        except NexusError as exc:
            if not results:
                raise
            problems.append(f"{exc} ({len(items) - i}개 모드는 확인하지 못했습니다)")
            break
        results[item.key] = find_update(cache[item.nexus_id], item.uploaded, item.version, item.exact)
    progress(len(items), len(items), "모드 업데이트 확인 완료")
    return results, problems
