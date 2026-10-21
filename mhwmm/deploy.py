"""Deploy enabled mods into the game folder and keep track of every file placed there.

Each run computes the files the game folder should contain, compares that with the
manifest of the previous run and only performs the difference:

* Loose files: later mods in the load order override earlier ones.
* PAK files go to ``pak_mods/`` as ``NNN_<mod>.pak``, numbered in load order.  The game
  only reads its own patch numbers, so a PAK renamed into the patch chain past them is
  ignored; REFramework mounts every ``.pak`` in ``pak_mods`` into a free patch slot in
  name order, so later files take priority.
* Game files that a mod overwrites are backed up and restored when no longer needed.
* A manifest entry whose file changed on disk is re-checked before touching it.
* A mod file changed in the game folder (settings a script saved, an edited .ini) is not
  thrown away when its mod goes off: it is kept in ``edited/`` of the data folder and put back
  when the same file of the same mod is placed again (an update of the mod drops it).
* Older versions renamed PAKs into the patch chain in the game folder.  The next run
  moves those to ``pak_mods``; one that a game update overwrote with its own patch is
  left alone as a game file.
"""

from __future__ import annotations

import logging
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import fsutil
from .constants import PAK_MODS_DIR, PAK_PATCH_RE

log = logging.getLogger(__name__)

ProgressFn = Callable[[int, int, str], None]
MANIFEST_VERSION = 1
TMP_SUFFIX = ".mhwmm-tmp"
EDITED_DIR = "edited"  # in the data folder: mod files the user changed, while their mod is off


def path_key(rel: str) -> str:
    return rel.replace("\\", "/").casefold()


def is_root_pak(rel: str) -> bool:
    return "/" not in rel and rel.lower().endswith(".pak")


def _pak_sort_key(name: str):
    m = PAK_PATCH_RE.match(name)
    return (0, int(m.group("num")), name.lower()) if m else (1, 0, name.lower())


_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f.]+')


def pak_mods_name(number: int, *parts: str) -> str:
    """``pak_mods/NNN_<label>.pak``: no dots inside, so REFramework never reads it as a patch number."""
    label = _UNSAFE_NAME.sub("_", "_".join(p for p in parts if p)).strip(" _")[:60].rstrip(" _") or "mod"
    return f"{PAK_MODS_DIR}/{number:03d}_{label}.pak"


def is_chain_pak(entry: dict) -> bool:
    """A PAK an older version renamed into the patch chain in the game folder."""
    return bool(entry.get("pak")) and "/" not in entry["path"]


@dataclass
class ModSource:
    mod_id: str
    name: str
    files_dir: Path
    files: list[str]  # game-relative posix paths
    # Optional per-file (source file, source key); the default is files_dir/rel and rel.
    # Mods with options keep each option in its own folder, so their keys name the option.
    sources: dict[str, tuple[Path, str]] | None = None

    def source(self, rel: str) -> tuple[Path, str]:
        if self.sources is not None and rel in self.sources:
            return self.sources[rel]
        return self.files_dir / rel, rel


@dataclass
class DeployReport:
    copied: int = 0
    renamed: int = 0
    removed: int = 0
    kept: int = 0
    backed_up: int = 0
    restored: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    paks: list[tuple[str, str, str]] = field(default_factory=list)  # (target, mod name, source file)

    @property
    def changed(self) -> bool:
        return bool(self.copied or self.renamed or self.removed or self.restored)

    def summary(self) -> str:
        parts = [f"복사 {self.copied}", f"이름 변경 {self.renamed}", f"삭제 {self.removed}", f"유지 {self.kept}"]
        if self.backed_up:
            parts.append(f"백업 {self.backed_up}")
        if self.restored:
            parts.append(f"복원 {self.restored}")
        text = " · ".join(parts)
        if self.errors:
            text += f" · 오류 {len(self.errors)}"
        return text


class DeployError(Exception):
    pass


class Deployer:
    def __init__(self, game_dir, data_dir):
        self.game_dir = Path(game_dir)
        self.manifest_path = Path(data_dir) / "deployment.json"
        self.backup_dir = Path(data_dir) / "backups"
        self.edited_dir = Path(data_dir) / EDITED_DIR

    # ------------------------------------------------------------------ manifest
    def load_manifest(self) -> dict:
        data = fsutil.read_json(self.manifest_path, None)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("version", MANIFEST_VERSION)
        data.setdefault("game_dir", "")
        if not isinstance(data.get("files"), dict):
            data["files"] = {}
        if not isinstance(data.get("backups"), dict):
            data["backups"] = {}
        if not isinstance(data.get("edited"), dict):
            data["edited"] = {}  # key -> {"path", "backup", "mod", "src", "src_size", "src_mtime"}
        return data

    def _save(self, manifest: dict) -> None:
        manifest["version"] = MANIFEST_VERSION
        manifest["game_dir"] = str(self.game_dir) if (manifest["files"] or manifest["backups"]) else ""
        fsutil.atomic_write_json(self.manifest_path, manifest)

    @staticmethod
    def bound_game_dir(manifest: dict) -> str:
        if manifest.get("files") or manifest.get("backups"):
            return manifest.get("game_dir") or ""
        return ""

    def _check_game_dir(self, manifest: dict) -> None:
        bound = self.bound_game_dir(manifest)
        if bound and not fsutil.same_path(bound, self.game_dir):
            raise DeployError(
                f"다른 게임 폴더({bound})에 적용된 모드가 있습니다. 그 폴더에서 먼저 모드를 해제하세요."
            )
        if not self.game_dir.is_dir():
            raise DeployError(f"게임 폴더가 없습니다: {self.game_dir}")

    # ------------------------------------------------------------------ helpers
    def _target(self, rel: str) -> Path:
        return self.game_dir / rel

    def _entry_state(self, entry: dict) -> str:
        st = fsutil.stat_or_none(self._target(entry["path"]))
        if st is None:
            return "missing"
        if st.st_size == entry.get("size") and st.st_mtime_ns == entry.get("mtime"):
            return "ok"
        return "changed"

    def _backup(self, key: str, rel: str, backups: dict, report: DeployReport) -> None:
        old = backups.pop(key, None)
        if old:
            fsutil.remove_file(self.backup_dir / old["backup"])
        fsutil.move_file(self._target(rel), self.backup_dir / rel)
        backups[key] = {"path": rel, "backup": rel}
        report.backed_up += 1
        log.info("기존 파일 백업: %s", rel)

    @staticmethod
    def _user_edited(entry: dict, state: str | None) -> bool:
        """A placed mod file that is not the mod's own copy any more (not a PAK: those are not edited)."""
        return not entry.get("pak") and (state == "modified" or bool(entry.get("edited")))

    def _stash(self, key: str, entry: dict, edited: dict) -> None:
        """Keep a mod file changed in the game folder aside (instead of deleting it): it comes back
        when the same file of the same mod is placed again."""
        old = edited.pop(key, None)
        if old:
            fsutil.remove_file(self.edited_dir / old["backup"])
        rel = entry["path"]
        fsutil.move_file(self._target(rel), self.edited_dir / rel)
        edited[key] = {"path": rel, "backup": rel, "mod": entry.get("mod"), "src": entry.get("src"),
                       "src_size": entry.get("src_size"), "src_mtime": entry.get("src_mtime")}
        log.info("사용자가 바꾼 모드 파일을 따로 보관합니다 (모드를 다시 켜면 되돌립니다): %s", rel)

    def _drop_stash(self, key: str, edited: dict, why: str) -> None:
        old = edited.pop(key, None)
        if old:
            fsutil.remove_file(self.edited_dir / old["backup"])
            fsutil.prune_empty_dirs((self.edited_dir / old["backup"]).parent, self.edited_dir.parent)
            log.info("보관해 둔 모드 파일을 지웁니다 (%s): %s", why, old.get("path"))

    def verify(self, include_outdated: bool = True) -> list[str]:
        """Problems with deployed files, without changing anything."""
        manifest = self.load_manifest()
        problems = []
        for entry in manifest["files"].values():
            state = self._entry_state(entry)
            if state == "missing":
                problems.append(f"없어진 파일: {entry['path']}")
            elif state == "changed" and is_chain_pak(entry):
                problems.append(f"게임 업데이트 등으로 덮어써진 PAK: {entry['path']}")
        outdated = self.outdated_paks(manifest)
        if include_outdated and outdated:
            problems.append(f"예전 방식(패치 번호)으로 넣은 PAK {len(outdated)}개는 게임이 읽지 않습니다. "
                            f"다시 적용하면 {PAK_MODS_DIR} 폴더로 옮깁니다.")
        return problems

    def outdated_paks(self, manifest: dict | None = None) -> list[str]:
        """Our PAKs still in the patch chain, where the game no longer reads them."""
        manifest = manifest or self.load_manifest()
        return [e["path"] for e in manifest["files"].values()
                if is_chain_pak(e) and self._entry_state(e) == "ok"]

    # ------------------------------------------------------------------ deploy
    def run(self, sources: list[ModSource], progress: ProgressFn | None = None,
            installed: set[str] | None = None) -> DeployReport:
        """Make the game folder match ``sources`` (in load order, last one wins). ``installed``: ids
        of every installed mod (the kept edits of other mods are dropped)."""
        report = DeployReport()
        manifest = self.load_manifest()
        self._check_game_dir(manifest)
        files: dict = manifest["files"]
        backups: dict = manifest["backups"]
        edited: dict = manifest["edited"]
        progress = progress or (lambda done, total, msg: None)

        try:
            if installed is not None:
                for key in [k for k, e in edited.items() if not isinstance(e, dict) or e.get("mod") not in installed]:
                    self._drop_stash(key, edited, "모드를 지움")
            status = self._reconcile(files, backups, report)
            desired = self._desired(sources, files, status, report)
            self._apply(desired, files, backups, edited, status, report, progress, installed)
        finally:
            self._save(manifest)
        return report

    def _reconcile(self, files: dict, backups: dict, report: DeployReport) -> dict[str, str]:
        status: dict[str, str] = {}
        for key, entry in list(files.items()):
            state = self._entry_state(entry)
            if state == "missing":
                del files[key]
                log.info("이미 없어진 파일을 목록에서 제거: %s", entry["path"])
            elif state == "ok":
                status[key] = "ok"
            elif is_chain_pak(entry) or key in backups:
                # Most likely a game update wrote its own file under this name.
                del files[key]
                stale = backups.pop(key, None)
                if stale:
                    fsutil.remove_file(self.backup_dir / stale["backup"])
                report.warnings.append(
                    f"'{entry['path']}' 파일이 외부에서 바뀌었습니다(게임 업데이트?). 게임 파일로 간주하고 건드리지 않습니다."
                )
            else:
                status[key] = "modified"
        return status

    def _desired(self, sources: list[ModSource], files: dict, status: dict, report: DeployReport) -> dict:
        desired: dict[str, dict] = {}
        paks: list[dict] = []
        for src in sources:
            mod_paks = []
            for rel in src.files:
                if is_root_pak(rel):
                    mod_paks.append(rel)
                    continue
                item = self._source_item(src, rel)
                if item:
                    desired[path_key(rel)] = item
            for rel in sorted(mod_paks, key=_pak_sort_key):
                item = self._source_item(src, rel)
                if item:
                    stem = posixpath.splitext(rel)[0] if len(mod_paks) > 1 else ""
                    paks.append((item, stem))
        # Load order decides the number, and the number decides priority (last wins).
        for number, (item, stem) in enumerate(paks, 1):
            target = pak_mods_name(number, item["mod_name"], stem)
            item["path"] = target
            item["pak"] = True
            desired[path_key(target)] = item
            report.paks.append((target, item["mod_name"], item["src"]))
        return desired

    @staticmethod
    def _source_item(src: ModSource, rel: str) -> dict | None:
        path, key = src.source(rel)
        st = fsutil.stat_or_none(path)
        if st is None:
            log.warning("모드 '%s'의 파일이 없습니다: %s", src.name, rel)
            return None
        return {
            "path": rel,
            "mod": src.mod_id,
            "mod_name": src.name,
            "src": key,
            "src_path": path,
            "src_size": st.st_size,
            "src_mtime": st.st_mtime_ns,
            "pak": False,
        }

    @staticmethod
    def _identity(item: dict) -> tuple:
        return (item["mod"], item["src"], item["src_size"], item["src_mtime"])

    @staticmethod
    def _identity_of(kept: dict) -> tuple:
        return (kept.get("mod"), kept.get("src"), kept.get("src_size"), kept.get("src_mtime"))

    def _apply(self, desired: dict, files: dict, backups: dict, edited: dict, status: dict,
               report: DeployReport, progress: ProgressFn, installed: set[str] | None = None) -> None:
        to_place = []
        for key, item in desired.items():
            entry = files.get(key)
            if entry and status.get(key) in ("ok", "modified") and self._identity(entry) == self._identity(item):
                report.kept += 1
            else:
                to_place.append(key)
        to_remove = [k for k in files if k not in desired]

        # PAKs that only change number (or move to pak_mods) are renamed instead of copied again.
        owned_paks = {self._identity(e): k for k, e in files.items() if e.get("pak") and status.get(k) == "ok"}
        relocate: dict[str, str] = {}
        for key in to_place:
            item = desired[key]
            old = owned_paks.pop(self._identity(item), None) if item["pak"] else None
            if old and old != key:
                relocate[key] = old

        total = len(to_place) + len(to_remove) + len(relocate) or 1
        done = 0
        dirs_to_prune: set[Path] = set()

        # 1) move PAKs that will be renumbered out of the way
        for key, old in list(relocate.items()):
            entry = files.pop(old)
            tmp_rel = entry["path"] + TMP_SUFFIX
            try:
                fsutil.replace_file(self._target(entry["path"]), self._target(tmp_rel))
            except OSError as exc:
                files[old] = entry
                del relocate[key]
                report.errors.append(f"{entry['path']} 이름 변경 실패: {exc}")
                continue
            entry["path"] = tmp_rel
            files[path_key(tmp_rel)] = entry
            relocate[key] = path_key(tmp_rel)
            done += 1
            progress(done, total, entry["path"])

        # 2) remove files that are no longer wanted (one changed in the game folder is kept aside)
        for key in to_remove:
            entry = files.get(key)
            if entry is None:
                continue
            try:
                if self._user_edited(entry, status.get(key)) and (installed is None or entry.get("mod") in installed):
                    self._stash(key, entry, edited)
                else:
                    if status.get(key) == "modified":
                        log.info("수정된 모드 파일을 삭제합니다: %s", entry["path"])
                    fsutil.remove_file(self._target(entry["path"]))
            except OSError as exc:
                report.errors.append(f"{entry['path']} 삭제 실패: {exc}")
                continue
            del files[key]
            report.removed += 1
            dirs_to_prune.add(self._target(entry["path"]).parent)
            done += 1
            progress(done, total, entry["path"])

        # 3) place new or changed files (or the changed copy kept aside while the mod was off)
        for key in to_place:
            item = desired[key]
            rel = item["path"]
            target = self._target(rel)
            from_stash = False
            try:
                if key in files:
                    entry = files[key]
                    if entry.get("mod") != item["mod"] and self._user_edited(entry, status.get(key)) and \
                            (installed is None or entry.get("mod") in installed):
                        self._stash(key, entry, edited)  # another mod's file goes there now
                    else:
                        fsutil.remove_file(target)
                    del files[key]
                elif fsutil.exists(target):
                    self._backup(key, rel, backups, report)
                kept = edited.get(key)
                if kept and self._identity_of(kept) == self._identity(item) and \
                        fsutil.exists(self.edited_dir / kept["backup"]):
                    fsutil.move_file(self.edited_dir / kept["backup"], target)
                    del edited[key]
                    fsutil.prune_empty_dirs((self.edited_dir / kept["backup"]).parent, self.edited_dir.parent)
                    log.info("보관해 둔 모드 파일을 되돌립니다: %s", rel)
                    from_stash = True
                    report.copied += 1
                elif key in relocate:
                    entry = files.pop(relocate[key])
                    fsutil.makedirs(target.parent)
                    fsutil.replace_file(self._target(entry["path"]), target)
                    report.renamed += 1
                    dirs_to_prune.add(self._target(entry["path"]).parent)
                    log.info("PAK 이름 변경: %s -> %s", entry["path"].removesuffix(TMP_SUFFIX), rel)
                else:
                    if kept and kept.get("mod") == item["mod"]:  # the mod was updated: its new copy instead
                        self._drop_stash(key, edited, "모드가 바뀜")
                    fsutil.copy_file(item["src_path"], target)
                    report.copied += 1
            except OSError as exc:
                report.errors.append(f"{rel} 적용 실패: {exc}")
                continue
            st = fsutil.stat_or_none(target)
            files[key] = {
                "path": rel,
                "mod": item["mod"],
                "src": item["src"],
                "src_size": item["src_size"],
                "src_mtime": item["src_mtime"],
                "size": st.st_size if st else -1,
                "mtime": st.st_mtime_ns if st else -1,
                "pak": item["pak"],
            }
            if from_stash:
                files[key]["edited"] = True  # not the mod's copy, though it reads as unchanged now
            done += 1
            progress(done, total, rel)

        # 4) put back game files that no mod overrides any more
        for key, b in list(backups.items()):
            if key in files:
                continue
            backup_file = self.backup_dir / b["backup"]
            target = self._target(b["path"])
            if not fsutil.exists(backup_file):
                report.warnings.append(f"백업 파일이 없어 복원하지 못했습니다: {b['path']}")
                del backups[key]
                continue
            if fsutil.exists(target):
                report.warnings.append(
                    f"'{b['path']}' 자리에 다른 파일이 있어 원본을 복원하지 않았습니다. 백업: {backup_file}"
                )
                del backups[key]
                continue
            try:
                fsutil.move_file(backup_file, target)
            except OSError as exc:
                report.errors.append(f"{b['path']} 복원 실패: {exc}")
                continue
            del backups[key]
            report.restored += 1
            fsutil.prune_empty_dirs(backup_file.parent, self.backup_dir)

        for d in sorted(dirs_to_prune, key=lambda p: len(p.parts), reverse=True):
            fsutil.prune_empty_dirs(d, self.game_dir)
