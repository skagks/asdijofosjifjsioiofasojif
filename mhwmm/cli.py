"""Command line interface. Without a command the GUI starts."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import math
import re
import sys
import time
import unicodedata
from pathlib import Path

from . import __version__, bisect, crashlog, fsutil, precheck, requirements, steam
from .analyzer import classify, list_files, package_name
from .constants import REFW_ORIGIN
from .deploy import DeployError, DeployReport, is_root_pak, path_key
from .equip.selftest import DEFAULT_ITEMS as SELFTEST_ITEMS
from .manager import InstallError, ModManager, acquire_instance_lock, prepare_data_dir, setup_logging
from .matfix import MatfixError
from .modedit import BAD_SUFFIX, EDITS_RECORD, EditError, GameCopyChanged
from .modedit import engines as edit_engines
from .modedit import model as edit_model

KIND_NAMES = {
    "reframework": "REFramework", "pak": "PAK", "loose": "루스", "script": "스크립트",
    "dll": "DLL", "other": "기타",
}


def _kinds(kinds: set[str]) -> str:
    return "+".join(KIND_NAMES[k] for k in KIND_NAMES if k in kinds) or "-"


def _print_report(report: DeployReport) -> None:
    print(report.summary())
    for target, mod_name, src in report.paks:
        print(f"  PAK {target}  <-  {mod_name} ({src})")
    for w in report.warnings:
        print(f"경고: {w}")
    for e in report.errors:
        print(f"오류: {e}", file=sys.stderr)


def _resolve(manager: ModManager, refs: list[str]):
    mods = []
    for ref in refs:
        mod = manager.find(ref)
        if mod is None:
            raise SystemExit(f"모드를 찾을 수 없습니다: {ref}")
        mods.append(mod)
    return mods


def _print_options(manager: ModManager, mod) -> None:
    for i, opt in enumerate(mod.options, 1):
        files = manager.option_files(mod.id, opt["id"])
        flag = "[x]" if opt["id"] in mod.selected else "[ ]"
        print(f"  {i}. {flag} {opt['name']}  ({_kinds(classify(files))}, {len(files)}개 파일)")
    if mod.needs_option:
        print(f"  * 옵션을 활성화해 주세요: options {mod.id} --set <번호>")


def _print_missing(manager: ModManager, mod, indent: str = "  ") -> None:
    for req in manager.missing_requirements(mod.id):
        state = "꺼져 있음" if req["state"] == "off" else "없음"
        kind = {"lua": " (Lua 라이브러리)", "shader": " (재질이 쓰는 셰이더 파일)"}.get(req["kind"], "")
        if req["kind"] == "shader":
            kind += f" - {precheck.shader_state(req)}"
        print(f"{indent}필요한 모드 {state}: {req['name']}{kind}  {req['url']}".rstrip())


def _fetch_requirements(manager: ModManager, nexus_ids: list[int]) -> None:
    from . import nexus

    if not nexus_ids:
        return
    try:
        manager.set_requirements(nexus.fetch_requirements(nexus_ids, manager.settings.nexus_api_key))
    except nexus.NexusError as exc:
        print(f"경고: Nexus에서 필요한 모드 정보를 받지 못했습니다: {exc}", file=sys.stderr)


def cmd_conflicts(manager: ModManager, args) -> int:
    conflicts = manager.conflicts()
    pairs = conflicts.pairs()
    if not pairs:
        print("같은 파일을 바꾸는 켜진 모드가 없습니다.")
        return 0
    names = {m.id: f"{i}. {m.name}" for i, m in enumerate(manager.mods, 1)}
    for winner, loser, paths in pairs:
        print(f"{names[winner]}  >  {names[loser]}  (파일 {len(paths)}개)")
        for path in paths[: None if args.files else 3]:
            print(f"    {path}")
        if not args.files and len(paths) > 3:
            print(f"    … 외 {len(paths) - 3}개 (--files로 모두 보기)")
    hidden = manager.hidden_mods(conflicts)
    if hidden:
        print("\n적용되지 않는 모드 (파일이 모두 밀림): " + ", ".join(names[m.id] for m in hidden))
    return 0


def cmd_list(manager: ModManager, args) -> int:
    print(f"게임 폴더: {manager.game_dir or '(설정 안 됨)'}")
    print(f"REFramework: {'설치됨' if manager.refw_installed() else '없음'}")
    print(f"프로필: {manager.profile}")
    if not manager.mods:
        print("설치된 모드가 없습니다.")
        return 0
    conflicts = manager.conflicts()
    for i, m in enumerate(manager.mods, 1):
        wins, losses = conflicts.counts(m.id)
        flag = "[x]" if m.enabled else "[ ]"
        extra = f"  충돌 +{wins}/-{losses}" if wins or losses else ""
        version = f" v{m.version}" if m.version else ""
        if m.update:
            version += f" (새 버전 {m.update.get('version') or '있음'})"
        if m.options:
            extra += "  옵션 선택 필요" if m.needs_option else f"  옵션 {len(m.selected)}/{len(m.options)}"
        print(f"{i:3d}. {flag} {m.name}{version}  ({_kinds(manager.mod_kinds(m.id))}, id={m.id}){extra}")
        if m.enabled:
            _print_missing(manager, m, "       ")
    if manager.dirty:
        print("\n* 적용되지 않은 변경사항이 있습니다. 'deploy'를 실행하세요.")
    return 0


def cmd_install(manager: ModManager, args) -> int:
    with manager.prepare_install(args.path) as session:
        variants = session.variants
        if args.raw or not variants:
            if not args.raw:
                print("모드 구조를 인식하지 못했습니다. --raw 옵션으로 압축 내용을 그대로 설치할 수 있습니다.")
                return 1
            variants = [session.raw_variant()]
        if args.option and any(not 1 <= i <= len(variants) for i in args.option):
            raise SystemExit("잘못된 옵션 번호입니다.")
        selected = None
        if args.all:
            selected = list(range(len(variants)))
        elif args.option:
            selected = [i - 1 for i in args.option]
        name = package_name(variants) if len(variants) > 1 else variants[0].name
        existing = manager.find_existing(name, session.source.name)
        replace_id = existing.id if existing and args.replace else None
        mod = manager.install_package(session, variants, replace_id, selected)
        if args.disabled and mod.enabled:
            manager.set_enabled(mod.id, False)
        print(f"설치: {mod.name}  ({_kinds(manager.mod_kinds(mod.id))}, {len(manager.mod_files(mod.id))}개 파일)")
        if replace_id:
            _print_carry(manager, mod)
        if mod.options:
            _print_options(manager, mod)
    if mod.nexus_id:
        _fetch_requirements(manager, [mod.nexus_id])
    _print_missing(manager, mod)
    return _maybe_deploy(manager, args)


def cmd_profile(manager: ModManager, args) -> int:
    try:
        if not args.name:
            for name in manager.profiles:
                print(f"{'*' if name == manager.profile else ' '} {name}")
            return 0
        if args.delete:
            manager.delete_profile(args.name)
            print(f"프로필 삭제: {args.name} (현재: {manager.profile})")
        elif args.rename:
            new = manager.rename_profile(args.name, args.rename)
            print(f"프로필 이름 변경: {args.name} -> {new}")
            return 0
        elif args.create:
            manager.create_profile(args.name, copy_current=not args.empty)
            print(f"프로필 만들기: {manager.profile}")
        else:
            manager.switch_profile(args.name)
            print(f"프로필 전환: {manager.profile}")
    except KeyError:
        print(f"프로필이 없습니다: {args.name}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    return _maybe_deploy(manager, args)


def cmd_refw(manager: ModManager, args) -> int:
    from . import refw
    from .updater import UpdateError

    if not manager.game_dir:
        print("게임 폴더를 먼저 설정하세요: game <경로>", file=sys.stderr)
        return 1
    try:
        status = refw.check(manager.game_dir)
    except UpdateError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    latest = status.latest
    print(f"설치된 버전: {refw.describe_installed(status)}")
    print(f"최신 버전: {latest.label}" + (f" ({latest.published})" if latest.published else ""))
    if not args.install:
        return 0
    if status.up_to_date and not args.force:
        print("이미 최신 버전입니다. 다시 설치하려면 --force를 붙이세요.")
        return 0
    manager.temp_root.mkdir(parents=True, exist_ok=True)
    archive = manager.temp_root / f"REFramework-{latest.number}.zip"
    try:
        refw.download(latest, archive)
        mod = manager.install_refw(archive, latest.label, f"{refw.ASSET_NAME} ({latest.tag})")
    except UpdateError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    finally:
        fsutil.remove_file(archive)
    print(f"설치: {mod.name} {mod.version}")
    return _maybe_deploy(manager, args)


def cmd_nexus(manager: ModManager, args) -> int:
    from . import nexus

    try:
        if args.action == "key":
            if not args.value:
                key = manager.settings.nexus_api_key
                print(f"API 키: {'설정됨 (' + key[:4] + '…)' if key else '없음'}")
                return 0
            user = nexus.NexusClient(args.value[0]).validate()
            manager.update_settings(nexus_api_key=args.value[0].strip())
            print(f"API 키 저장: {user.get('name') or '이름 없음'} ({'프리미엄' if user.get('is_premium') else '일반'})")
            return 0
        if args.action in ("link", "unlink"):
            if len(args.value) != (2 if args.action == "link" else 1):
                raise SystemExit("사용법: nexus link <모드> <Nexus 주소 또는 번호> / nexus unlink <모드>")
            (mod,) = _resolve(manager, args.value[:1])
            if args.action == "unlink":
                manager.unlink_nexus(mod.id)
                print(f"Nexus 연결 해제: {mod.name}")
            else:
                print(f"Nexus 연결: {mod.name} -> {nexus.MOD_URL.format(manager.link_nexus(mod.id, args.value[1]))}")
            return 0
        # check
        items = manager.nexus_items()
        if not items:
            print("Nexus와 연결된 모드가 없습니다. 'nexus link <모드> <주소>'로 연결하세요.")
            return 0
        client = nexus.NexusClient(manager.settings.nexus_api_key)
        results, problems = nexus.check_mods(client, items, lambda done, total, msg: print(msg, file=sys.stderr))
        _fetch_requirements(manager, manager.requirement_targets())
    except nexus.NexusError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    manager.set_update_results(results)
    for problem in problems:
        print(f"경고: {problem}", file=sys.stderr)
    updates = [m for m in manager.mods if m.update]
    for m in updates:
        print(f"새 버전: {m.name}  {m.version or '?'} -> {m.update.get('version') or '새 파일'}  "
              f"{nexus.FILES_URL.format(m.nexus_id)}")
    if not updates:
        print(f"Nexus와 연결된 모드 {len(items)}개가 모두 최신입니다.")
    return 0


def cmd_options(manager: ModManager, args) -> int:
    (mod,) = _resolve(manager, [args.mod])
    if not mod.options:
        print(f"'{mod.name}'에는 선택할 옵션이 없습니다.")
        return 1
    if args.set is None:
        print(f"{mod.name} 옵션:")
        _print_options(manager, mod)
        return 0
    if any(not 1 <= i <= len(mod.options) for i in args.set):
        raise SystemExit("잘못된 옵션 번호입니다.")
    manager.set_options(mod.id, [mod.options[i - 1]["id"] for i in args.set])
    print(f"{mod.name} 옵션:")
    _print_options(manager, mod)
    return _maybe_deploy(manager, args)


def _maybe_deploy(manager: ModManager, args) -> int:
    if getattr(args, "no_deploy", False) or not manager.game_dir:
        return 0
    report = manager.deploy()
    _print_report(report)
    return 1 if report.errors else 0


def cmd_toggle(manager: ModManager, args) -> int:
    for mod in _resolve(manager, args.mods):
        manager.set_enabled(mod.id, args.cmd == "enable")
        print(f"{'활성화' if args.cmd == 'enable' else '비활성화'}: {mod.name}")
    return _maybe_deploy(manager, args)


def cmd_remove(manager: ModManager, args) -> int:
    for mod in _resolve(manager, args.mods):
        manager.remove(mod.id)
        print(f"삭제: {mod.name}")
    return _maybe_deploy(manager, args)


def cmd_move(manager: ModManager, args) -> int:
    (mod,) = _resolve(manager, [args.mod])
    manager.move_to(mod.id, args.position - 1)
    print(f"{mod.name} -> {args.position}번째")
    return _maybe_deploy(manager, args)


def cmd_deploy(manager: ModManager, args) -> int:
    report = manager.deploy()
    _print_report(report)
    return 1 if report.errors else 0


def cmd_purge(manager: ModManager, args) -> int:
    report = manager.purge()
    _print_report(report)
    return 1 if report.errors else 0


def cmd_verify(manager: ModManager, args) -> int:
    problems = manager.verify()
    for p in problems:
        print(p)
    if not problems:
        print("문제가 없습니다.")
    return 1 if problems else 0


MATERIAL_STATES = {"unchecked": "점검 전", "current": "현재 게임과 맞음", "stale": "복구 필요",
                   "repaired": "복구됨", "declined": "복구하지 않음 (원래 파일 사용)"}


def cmd_materials(manager: ModManager, args) -> int:
    """Check, repair or undo the repair of materials that no longer fit the game."""
    mods = _resolve(manager, args.mods) if args.mods else None
    if args.action == "report":
        if not mods or len(mods) != 1:
            raise SystemExit("진단 파일을 만들 모드 하나를 지정하세요: materials report <모드> [--out 파일.zip]")
        (job,) = manager.material_jobs([mods[0].id]) or [None]
        if job is None:
            print("이 모드에는 재질이나 .user·.pfb 파일이 없습니다.")
            return 0
        out = Path(args.out or f"{mods[0].id}_matfix_report.zip")
        count = manager.export_material_report(job, out)
        print(f"저장했습니다: {out} (.user·.pfb 파일 {count}개)")
        return 0
    if args.action == "restore":
        if not mods:
            raise SystemExit("되돌릴 모드를 지정하세요: materials restore <모드>")
        restored = []
        for mod in mods:
            changed = manager.restore_materials(mod.id)
            print(f"{'원래 재질 파일로 되돌림' if changed else '복구한 재질이 없음'}: {mod.name}")
            if changed:
                restored.append(mod.id)
        _reapply_edits(manager, restored)
        return _maybe_deploy(manager, args)
    jobs = manager.material_jobs([m.id for m in mods] if mods else None)
    if not jobs:
        print("재질(.mdf2)이나 PAK 파일이 있는 모드가 없습니다.")
        return 0
    repair = args.action == "repair"
    shown = [0.0]

    def progress(done, total, msg):
        if time.time() - shown[0] > 1.0:  # the first check reads thousands of game files
            shown[0] = time.time()
            print(msg, file=sys.stderr)

    run = manager.run_materials(jobs, repair=repair, progress=progress)
    manager.finish_materials(run, jobs)
    failed = 0
    position = {m.id: i for i, m in enumerate(manager.mods, 1)}
    for job in jobs:
        if job.mod_id in run.errors:
            print(f"{position.get(job.mod_id, '?')}. {job.name}: 점검하지 못함 ({run.errors[job.mod_id]})")
            failed += 1
            continue
        state = manager.material_state(job.mod_id)
        print(f"{position.get(job.mod_id, '?')}. {job.name}: {MATERIAL_STATES.get(state, state)}")
        for r in run.results.get(job.mod_id, []):
            if r.state == "error":
                failed += 1
                print(f"     {r.rel}: 오류 - {r.error}")
            elif r.state == "fixable" and (r.fixed or r.kind == "mdf2"):
                moved = f"  ->  {r.new_rel}" if r.new_rel != r.rel and not r.rsz else ""
                print(f"     {r.rel}: 재질 {', '.join(r.fixed) or '버전 번호'}{moved}")
            for item in r.rsz:
                if item.get("reason"):
                    failed += 1
                    print(f"     {item['label']}: {item['kind']} 복구 불가 - {item['reason']}")
                    if item.get("detail"):
                        print(f"       ({item['detail']})")
                elif r.state != "error":
                    moved = f"  ->  {r.new_rel}" if r.kind != "pak" and r.new_rel != r.rel else ""
                    print(f"     {item['label']}: {item['kind']} {'복구' if repair else '복구 필요'} ({item['method']}){moved}")
            foreign = {name for name, _mmtr in r.shaders}
            plain = [name for name in r.unresolved if name not in foreign]
            if plain:
                print(f"     {r.rel}: 같은 셰이더를 쓰는 재질을 찾지 못해 그대로 둔 재질 - {', '.join(plain)}")
            if foreign:
                shaders = sorted({mmtr.rsplit("/", 1)[-1] for _name, mmtr in r.shaders}, key=str.casefold)
                print(f"     {r.rel}: 게임에 없는 셰이더({', '.join(shaders)})를 쓰는 재질이라 그대로 둠 - "
                      f"{', '.join(n for n in r.unresolved if n in foreign)}")
        try:
            shaders = manager.shader_requirements(job.mod_id)
        except Exception as exc:  # (only a help)
            print(f"     재질이 쓰는 셰이더를 확인하지 못했습니다: {exc}")
            shaders = []
        for req in shaders:
            where = precheck.shader_state(req) + (" (셰이더 모드가 필요할 수 있음)" if req["state"] == "missing" else "")
            print(f"     재질이 쓰는 {req['name']}: {where}")
    if not repair and any(manager.material_state(j.mod_id) == "stale" for j in jobs):
        print("\n복구하려면: materials repair [모드]")
    _reapply_edits(manager, [j.mod_id for j in jobs])  # (a repair made, given up or no longer needed: a new base)
    code = _maybe_deploy(manager, args) if repair else 0
    return 1 if failed else code


def cmd_equipment(manager: ModManager, args) -> int:
    """Which in-game equipment each mod changes the look of, with the game's own names."""
    from .equip import overview
    from .equip.model import KIND_LABELS, PART_LABELS

    shown = [0.0]

    def progress(done, total, msg):
        if time.time() - shown[0] > 1.0:  # the first run reads the game's data tables
            shown[0] = time.time()
            print(msg, file=sys.stderr)

    if args.icons:
        if manager.save_equipment_icon_sheet(Path(args.icons), progress):
            print(f"저장했습니다: {args.icons}")
            return 0
        print("게임에서 아이콘 그림을 읽지 못했습니다.", file=sys.stderr)
        return 1
    mods = _resolve(manager, args.mods) if args.mods else None
    jobs = manager.equipment_jobs()
    if mods:
        wanted = {m.id for m in mods}
        jobs = [j for j in jobs if j.mod_id in wanted]
    catalog = manager.equipment_catalog(progress)
    scans = manager.equipment_scans(jobs, progress)
    result = overview.build([(j.mod_id, j.name, j.enabled, j.order, scans[j.mod_id]) for j in jobs], catalog)
    if catalog is None:
        print("게임 데이터를 읽지 못해 장비를 번호로 보여 줍니다.\n")
    for job in jobs:
        items = [i for i in result.items if any(u.mod_id == job.mod_id for u in i.uses)]
        if not items and not mods:
            continue
        print(f"{job.order + 1}. {job.name}{'' if job.enabled else ' (꺼짐)'}")
        if not items:
            print("     바꾸는 장비를 찾지 못했습니다")
        for item in items:
            use = next(u for u in item.uses if u.mod_id == job.mod_id)
            parts = [PART_LABELS.get(p, p) for p in (*item.parts, *use.parts) if p and p in use.parts]
            parts = list(dict.fromkeys(parts))
            rarity = f" · {item.rarity_text}" if item.rarity else ""
            where = f" [{', '.join(parts)}]" if parts else ""
            print(f"     {KIND_LABELS.get(item.target.kind, '')} {item.name} ({item.subtitle}{rarity}){where}"
                  f"  {item.target.kind}/{item.target.key}")
            for part, users in item.conflicts().items():
                if any(u.mod_id == job.mod_id for u in users):
                    others = ", ".join(f"'{u.name}'" for u in users if u.mod_id != job.mod_id)
                    print(f"       겹침: {PART_LABELS.get(part, '') or '같은 장비'} - {others}도 바꿈"
                          f" (같은 파일은 아래쪽 '{users[-1].name}' 우선)")
            for note in item.notes:
                print(f"       참고: {note}")
    for problem in result.problems:
        print(f"경고: {problem}", file=sys.stderr)
    if catalog is not None:
        for problem in catalog.problems:
            print(f"경고: {problem}", file=sys.stderr)
    return 0


def parse_target(text: str):
    """``<kind>/<key>[/<part>]`` (e.g. ``weapon/09/000021/main``, ``armor/f/004/001/helm``; the part may also
    be its Korean name) -> Target; None when it is not one."""
    from .equip.model import KINDS, PART_LABELS, PART_ORDER, Target

    kind, _, rest = text.strip().replace("\\", "/").strip("/").partition("/")
    kind = kind.lower()
    if kind not in KINDS or not rest:
        return None
    head, _, last = rest.rpartition("/")
    names = {label: part for part, label in PART_LABELS.items()}
    part = names.get(last, last.lower())
    if head and part in PART_ORDER.get(kind, ()):
        return Target(kind, head, part)
    return Target(kind, rest)


def cmd_equip_preview(manager: ModManager, args) -> int:
    """A 3D picture of one equipment part with a mod's files (or the game's own look), or the self-test."""
    import shutil

    from .equip.look import COLOR_LABELS

    if args.self_test is not None:
        def progress(done, total, msg):
            print(msg, file=sys.stderr)

        print(manager.equipment_self_test(args.self_test, progress=progress), end="")
        return 0
    if not (args.mod and args.target and args.out):
        raise SystemExit("사용법: equip-preview <모드> <장비> 파일.png [--before] [--size N]  "
                         "(장비 예: weapon/09/000021/main, armor/f/004/001/helm · 모드 '-'는 게임 그대로)\n"
                         "       equip-preview --self-test [N]")
    target = parse_target(args.target)
    if target is None:
        raise SystemExit(f"장비를 알 수 없습니다: {args.target} (예: weapon/09/000021/main, armor/f/004/001/helm)")
    if args.mod == "-":
        job, name = None, "게임"
    else:
        (mod,) = _resolve(manager, [args.mod])
        job, name = manager.equipment_job(mod.id), mod.name
    p = manager.equipment_preview(job, target, args.size, compare=args.before and job is not None)
    out = Path(args.out)
    where = f"{target.kind}/{target.key}" + (f"/{target.part}" if target.part else "")
    print(f"{name} · {where}: {p.text or '정상'}" + (f" (삼각형 {p.triangles:,}개)" if p.triangles else ""))
    saved = False
    if p.after is not None:
        shutil.copyfile(p.after, out)
        print(f"저장했습니다: {out}")
        saved = True
    if args.before:
        if p.before is not None:
            before = out.with_name(f"{out.stem}-before{out.suffix or '.png'}")
            shutil.copyfile(p.before, before)
            print(f"원래 모습: {before}")
        elif job is not None:
            print("원래 모습은 그리지 못했습니다")
    for note in p.notes:
        print(f"  참고: {note}")
    for swatch in p.swatches:
        print(f"  텍스처: {swatch.name}" + (" (새 텍스처)" if swatch.new else ""))
    for chip in p.chips:
        old, new = chip.hex(chip.before) or "?", chip.hex(chip.after) or "?"
        off = "" if chip.after_on else " (꺼짐)"
        print(f"  색: {chip.material} · {COLOR_LABELS.get(chip.name, chip.name)} {old} -> {new}{off}")
    return 0 if saved else 1


def cmd_retarget(manager: ModManager, args) -> int:
    """다른 장비로 옮기기: a new mod that changes another armor design or weapon model the way a mod changes
    its own (the original is switched off unless --keep-original). ``--to`` more than once: the one without
    a part for every part, ``part=equipment`` for that part (armor parts to different designs)."""
    from .equip.model import BODIES, PART_LABELS, PART_ORDER, Target, fallback_label
    from .equip.retarget import Slot, parts_on

    example = "(예: armor/f/087/000, weapon/03/000029)"
    labels = {label: part for part, label in PART_LABELS.items()}

    def body(t) -> str:
        head = t.key.partition("/")[0]
        return head if t.kind == "armor" and head in BODIES else ""

    def with_body(t, b: str):  # armor/004/000 -> armor/f/004/000 (both bodies move together anyway)
        return Target(t.kind, f"{b}/{t.key}") if t.kind == "armor" and b and not body(t) else t

    def equipment(text: str):
        found = parse_target(text)
        if found is None:
            raise SystemExit(f"장비를 알 수 없습니다: {text} {example}")
        return found.item

    (mod,) = _resolve(manager, [args.mod])
    default, chosen = None, {}  # --to without a part (every part), part=... (that part)
    for value in args.to:
        head, sep, rest = value.partition("=")
        if not sep:
            if default is not None:
                raise SystemExit("부위를 적지 않은 --to(모든 부위에 입힐 장비)는 하나만 적으세요")
            default = equipment(value)
            continue
        part = labels.get(head.strip(), head.strip().lower())
        if part in chosen:
            raise SystemExit(f"한 부위에 --to를 두 번 적었습니다: {head.strip()}")
        chosen[part] = equipment(rest)
    reference = default if default is not None else next(iter(chosen.values()))  # (whose body to take)

    def pick(found):  # the one of the body of --to
        return next((t for t in found if body(t) == body(reference)), found[0])

    if args.source:
        source = parse_target(args.source)
        if source is None:
            raise SystemExit(f"장비를 알 수 없습니다: {args.source} {example}")
        source = source.item
        if source.kind == "armor" and not body(source):  # the body the mod has that design in
            slot = Slot("armor", source.key)
            found = [t for t in manager.retarget_sources(mod.id) if Slot.of(t) == slot]
            source = pick(found) if found else with_body(source, body(reference))
    else:
        sources = manager.retarget_sources(mod.id)
        if not sources:
            print(f"'{mod.name}'은(는) 다른 장비로 옮길 수 있는 방어구나 무기를 바꾸지 않습니다.", file=sys.stderr)
            return 1
        if len({Slot.of(t) for t in sources}) > 1:  # (a design's two bodies are one: they move together)
            raise SystemExit("원래 장비가 여러 개입니다. --from으로 고르세요: " +
                             ", ".join(f"{t.kind}/{t.key}" for t in sources))
        source = pick(sources)
    parts = None
    if args.parts is not None:
        parts = [labels.get(p.strip(), p.strip().lower()) for p in args.parts.split(",") if p.strip()]
    if not chosen:  # one piece of equipment for every part (or those of --parts)
        target = with_body(default, body(source))
    else:  # each part its own: the parts named, and with a --to without a part every other one
        def fit(t):  # (both bodies of a design move together: named with the original's body)
            if t.kind != "armor" or not body(source):
                return t
            return Target("armor", f"{body(source)}/{t.key.partition('/')[2] if body(t) else t.key}")

        def named(names) -> str:
            return ", ".join(PART_LABELS.get(p, p) for p in names)

        order = PART_ORDER.get(source.kind, ())
        if [p for p in chosen if p not in order]:
            raise SystemExit(f"알 수 없는 부위입니다: {named(p for p in chosen if p not in order)}")
        if parts is not None and [p for p in chosen if p not in parts]:
            raise SystemExit(f"--parts에 없는 부위의 --to입니다: {named(p for p in chosen if p not in parts)}")
        if parts is not None:
            wanted = parts
        elif default is not None and source.kind == "armor":  # the parts the mod changes (and those named)
            scan = manager.equipment_scans([manager.equipment_job(mod.id)]).get(mod.id)
            changed = parts_on(scan.targets, source) if scan is not None else []
            wanted = [*changed, *chosen] if changed else list(order)
        else:
            wanted = list(order) if default is not None else list(chosen)
        missing = [p for p in wanted if p not in chosen and default is None]
        if missing:
            raise SystemExit(f"옮길 장비를 고르지 않은 부위입니다: {named(missing)} (--to 부위=장비, 또는 모든 부위에 "
                             "입힐 장비를 부위 없이 --to로)")
        wanted = sorted(dict.fromkeys(wanted), key=lambda p: order.index(p) if p in order else len(order))
        target = {p: fit(chosen.get(p, default)) for p in wanted}
        # every part to one: a move to it (as without parts; a weapon's main and sub only together)
        if len(set(target.values())) == 1 and (source.kind == "armor" or set(target) == set(order)):
            target, parts = next(iter(target.values())), list(target)
    shown = [0.0]

    def progress(done, total, msg):
        if time.time() - shown[0] > 1.0:  # the first run reads the game's data tables
            shown[0] = time.time()
            print(msg, file=sys.stderr)

    try:
        catalog = manager.equipment_catalog(progress)
    except Exception:  # names are only a help
        catalog = None

    def label(t) -> str:
        return f"{(catalog.name(t) if catalog is not None else '') or fallback_label(t)} ({t.kind}/{t.key})"

    plan = manager.plan_retarget(mod.id, source, target, parts)
    if isinstance(target, dict):
        print(f"{mod.name}: {label(source)} → 부위마다 다른 장비")
        for part, t in target.items():
            print(f"  {PART_LABELS.get(part, part)} → {label(t)}")
    else:
        print(f"{mod.name}: {label(source)} → {label(target)}")
        if plan.source.kind == "armor" and plan.parts:
            print(f"  부위: {', '.join(PART_LABELS.get(p, p) for p in plan.parts)}")
    fills = sum(1 for move in plan.moves if move.how == "fill")
    print(f"  옮길 파일 {len(plan.moves) - fills}개 · " + (f"게임에서 가져올 파일 {fills}개 · " if fills else "") +
          f"그대로 넣을 파일 {len(plan.kept)}개 · 넣지 않을 파일 {len(plan.dropped)}개")
    for note in plan.notes:
        print(f"  참고: {note}")
    for error in plan.errors:
        print(f"오류: {error}", file=sys.stderr)
    if not plan.ok:
        return 1
    if args.dry_run:
        print("(--dry-run: 새 모드는 만들지 않았습니다)")
        return 0
    was_on = mod.enabled
    new = manager.create_retarget(mod.id, source, target, parts, disable_original=not args.keep_original,
                                  progress=progress)
    print(f"새 모드: {new.name}  ({len(manager.mod_files(new.id))}개 파일, {'켜짐' if new.enabled else '꺼짐'})")
    if was_on and not mod.enabled:
        print(f"원래 모드를 껐습니다: {mod.name}")
    return _maybe_deploy(manager, args)


# ---- 모드 수정 (edit) ---------------------------------------------------------------------------------------------
EDIT_ACTIONS = ("status", "files", "exclude", "include", "use", "values", "set", "scale", "revert", "resolve",
                "reapply", "clear-record")
EDIT_WRITES = ("exclude", "include", "use", "set", "scale", "revert", "resolve", "reapply", "clear-record")
EDIT_HELP = {
    "status": "이 모드에 한 수정: 뺀 파일, 바꾼 값, 업데이트 뒤 확인할 값과 다시 적용하지 못한 파일",
    "files": "모드의 파일과 그 상태 (넣음 · 뺌 · 덮임 · 옵션 꺼짐)",
    "exclude": "파일을 게임에 넣지 않기 (텍스처·모델의 스트리밍 사본도 함께)",
    "include": "뺀 파일을 다시 넣기",
    "use": "켠 옵션 여럿에 같은 파일이 있을 때 쓸 옵션 고르기 (다른 켠 옵션의 같은 파일을 뺌)",
    "values": "값을 바꿀 수 있는 파일 목록, 또는 한 파일의 값 (데이터 파일은 게임 값과 함께)",
    "set": "값 바꾸기 (주소=값, 여러 개)",
    "scale": "여러 값을 한꺼번에: 값 정하기 · 게임 값의 몇 배 · 모드가 바꾼 만큼의 몇 배",
    "revert": "되돌리기: 값, 파일 하나, 또는 --all로 모든 수정",
    "resolve": "업데이트 뒤 작성자가 바꾼 값: 내 값 유지(--keep) 또는 작성자 값 쓰기(--take)",
    "reapply": "다시 적용하지 못했거나 기다리는 수정을 다시 적용 (확인할 값은 그대로)",
    "clear-record": "읽지 못한 수정 기록을 옆으로 옮겨 두기 (edits.json.bad-<시각>)",
}
EDIT_TAGS = {"exclude": "[뺌]", "data": "[수치]", "config": "[설정]"}
EDIT_FILE_STATES = {"in": "넣음", "excluded": "뺌", "overridden": "덮임", "option_off": "옵션 꺼짐",
                    "conflict": "확인 필요", "needs_option": "옵션 고르기 전"}
EDIT_PROFILES = "수정은 모든 프로필에 똑같이 쓰입니다."
EDIT_OPTIONS = "옵션별 수정은 그 옵션을 켠 프로필에서만 게임에 들어갑니다."
EDIT_FILTERS = {"data": "모드가 바꾼 값만", "config": "설정 같은 값만"}
EDIT_MEMBER = re.compile(r"[0-9A-Fa-f]{16}")
EDIT_HINTS = 5  # "게임에 쓰인 값" lines under a list of values
EDIT_THOUSANDS = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?")  # 1,000 / -12,345.5
EDIT_UNKEYED = "게임과 행을 맞추지 못해 건너뛴 값"  # (the window's text: modeditview.UNKEYED)


class _EditArgs(argparse.Action):
    """What follows ``edit <모드> <동작>``, read by that action's own parser (_edit_parser) in any order: the
    values of ``set`` may come before or after its options."""

    def __call__(self, parser, namespace, values, option_string=None):
        action = getattr(namespace, "action", None) or "status"
        sub = _edit_parser(action)
        sub.parse_intermixed_args(list(values), namespace)
        if action == "resolve" and bool(namespace.addresses) == bool(namespace.every):
            sub.error("확인할 값의 주소를 적거나 --every를 붙이세요 (둘 중 하나만)")
        if action == "use" and namespace.nth < 1:
            sub.error("--nth는 1 이상이어야 합니다")


def _edit_parser(action: str) -> argparse.ArgumentParser:
    """The arguments of one ``edit`` action."""
    p = argparse.ArgumentParser(prog=f"mhwmm edit <모드> {action}", description=EDIT_HELP[action])
    option_help = "옵션이 있는 모드에서 그 옵션의 파일 (이름이 같은 옵션이 여럿이면 '이름 (2번째)')"
    file_help = "게임 경로, 겹치지 않는 파일 이름, <pak>#<해시 16자리> 또는 'edit <모드> values'의 번호"

    def writes(dry_run: bool = True, yes: str = "") -> None:
        if yes:
            p.add_argument("--yes", action="store_true", help=yes)
        if dry_run:
            p.add_argument("--dry-run", action="store_true", help="새 파일을 만들어 확인만 하고 저장하지 않음")
        p.add_argument("--no-deploy", action="store_true", help="게임 폴더에 바로 적용하지 않음")

    if action == "files":
        p.add_argument("--option", metavar="이름", help=option_help)
        p.add_argument("--find", metavar="글자", help="경로에 이 글자가 든 파일만")
        p.add_argument("--excluded", action="store_true", help="뺀 파일만")
        p.add_argument("--overlaps", action="store_true", help="켠 옵션 여럿에 있는 파일만 (use로 쓸 옵션을 고를 수 있음)")
    elif action in ("exclude", "include"):
        p.add_argument("paths", nargs="+", metavar="경로", help="게임 경로, 파일 이름 또는 * · ? 패턴 (대소문자 무시)")
        scope = p.add_mutually_exclusive_group()
        scope.add_argument("--option", metavar="이름", help=option_help)
        scope.add_argument("--all-options", action="store_true", help="모든 옵션의 맞는 파일")
        writes()
    elif action == "use":
        p.add_argument("path", metavar="경로", help="게임 경로, 파일 이름 또는 * · ? 패턴")
        p.add_argument("--option", metavar="이름", required=True, help="쓸 옵션 (켜져 있어야 함)")
        p.add_argument("--nth", type=int, default=1, metavar="N", help="이름이 같은 옵션이 여럿일 때 N번째")
        writes()
    elif action == "values":
        p.add_argument("file", nargs="?", metavar="파일", help=file_help + " (생략하면 파일 목록)")
        p.add_argument("--option", metavar="이름", help=option_help)
        p.add_argument("--all", action="store_true", help="모든 값 (기본은 모드가 바꾼 값만, Lua는 설정 같은 값만)")
        p.add_argument("--find", metavar="글자", help="주소나 이름에 이 글자가 든 값만")
        p.add_argument("--limit", type=int, default=50, metavar="N", help="보여 줄 값의 수 (기본 50, 0은 모두)")
    elif action == "set":
        p.add_argument("file", metavar="파일", help=file_help)
        p.add_argument("pairs", nargs="+", metavar="주소=값",
                       help="값은 [...] 밖의 = 뒤부터 (켜기·끄기는 true/false/켬/끔, 벡터는 1,2,3)")
        p.add_argument("--option", metavar="이름", help=option_help)
        p.add_argument("--same", action="store_true",
                       help="데이터 파일: 이 표에서 값이 같은 행 모두 (규칙으로 기록해 업데이트 뒤 새 행에도 씀)")
        writes()
    elif action == "scale":
        p.add_argument("file", metavar="파일", help=file_help)
        how = p.add_mutually_exclusive_group(required=True)
        how.add_argument("--to", metavar="N", help="값 정하기")
        how.add_argument("--times", metavar="K", help="게임 값의 K배")
        how.add_argument("--change", metavar="K", help="모드가 바꾼 만큼의 K배 (게임 값 + K × (모드 값 - 게임 값))")
        p.add_argument("--field", metavar="패턴",
                       help="바꿀 값의 주소·규칙 주소·이름 (* · ? 패턴, 생략하면 모드가 바꾼 숫자 값 모두)")
        p.add_argument("--option", metavar="이름", help=option_help)
        writes()
    elif action == "revert":
        p.add_argument("file", nargs="?", metavar="파일", help=file_help + " (생략하면 --all로 모든 수정)")
        p.add_argument("addresses", nargs="*", metavar="주소", help="되돌릴 값 (생략하면 파일의 수정 모두)")
        p.add_argument("--option", metavar="이름", help=option_help)
        p.add_argument("--all", action="store_true", help="이 모드의 모든 수정 되돌리기")
        writes(yes="게임 폴더에서 바뀐 파일도 되돌리기 (게임 폴더에서 바뀐 내용은 사라짐)")
    elif action == "resolve":
        p.add_argument("file", metavar="파일", help=file_help)
        p.add_argument("addresses", nargs="*", metavar="주소", help="확인할 값의 주소")
        p.add_argument("--every", action="store_true", help="이 파일의 확인할 값 모두")
        which = p.add_mutually_exclusive_group(required=True)
        which.add_argument("--keep", action="store_true", help="내 값 유지")
        which.add_argument("--take", action="store_true", help="작성자 값 쓰기 (내 수정을 지움)")
        p.add_argument("--option", metavar="이름", help=option_help)
        writes()
    elif action == "reapply":
        p.add_argument("files", nargs="*", metavar="파일", help=file_help + " (생략하면 기다리는 것 모두)")
        writes(yes="게임 폴더에서 바뀐 파일도 다시 만들기 (게임 폴더에서 바뀐 내용은 사라짐)")
    elif action == "clear-record":
        p.add_argument("--yes", action="store_true", help="정말 옮기기 (지우지 않고 edits.json.bad-<시각>으로 옮겨 둠)")
    return p


def cmd_edit(manager: ModManager, args) -> int:
    """모드 수정: leave files of a mod out (or choose which selected option's copy of a path is used), change the
    values of its .user data files (loose or in its root PAKs) and of the settings files its scripts ship with,
    see what was changed and undo it. Saved as the window saves (manager.apply_edit_plan: built into a temporary
    folder and checked, then written all or nothing) and applied unless --no-deploy; --dry-run only builds and
    checks. Exits with 1 on an error, and after reapply while values to check or files not applied are left."""
    (mod,) = _resolve(manager, [args.mod])
    if mod.origin == REFW_ORIGIN:
        print("오류: REFramework는 모드 수정으로 고칠 수 없습니다.", file=sys.stderr)
        return 1
    try:
        with _edit_registry(manager, mod, args.action):
            return EDIT_COMMANDS[args.action](manager, mod, args)
    except GameCopyChanged as exc:
        print(f"오류: {exc}", file=sys.stderr)
        if hasattr(args, "yes"):
            print("매니저가 넣은 파일로 되돌리면 게임 폴더에서 바뀐 파일(스크립트가 저장한 설정 등)은 데이터 폴더의 "
                  "edited\\에 따로 보관하고 게임에는 넣지 않습니다. 그래도 하려면 --yes를 붙이세요.", file=sys.stderr)
        return 1
    except EditError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


@contextlib.contextmanager
def _edit_registry(manager: ModManager, mod, action: str):
    """Hold the game's type registry for the whole command when it reads .user data (the window holds it while it
    is open; each read, each listing of a PAK and the build would load it again otherwise)."""
    need = False
    if action in ("values", "set", "scale"):
        need = any(edit_model.is_data_rel(rel) or is_root_pak(rel) for rel in _stored_rels(manager, mod))
    elif action in ("revert", "resolve", "reapply"):
        record = manager.edit_record(mod.id)
        need = not record.error and any(edit_model.engine_tool(m.engine) == "data"
                                        for item in record.items for m in item.members)
    if not need:
        yield
        return
    from .modedit import rszdata

    rszdata.registry()
    try:
        yield
    finally:
        rszdata.release()


def _stored_rels(manager: ModManager, mod) -> list[str]:
    """The game paths of every stored file of a mod, of every option."""
    if mod.options:
        return [rel for o in mod.options for rel in manager.option_files(mod.id, o["id"])]
    return list_files(manager.files_dir(mod.id))


def _progress_printer():
    """Progress lines on stderr, at most one a second and none for the first second (most edits take less)."""
    shown = [time.time()]

    def progress(done, total, msg):
        if time.time() - shown[0] > 1.0:  # (a big PAK is read and written again)
            shown[0] = time.time()
            print(msg, file=sys.stderr)

    return progress


def _q(text: str) -> str:
    """``text`` as the command line takes it back: in double quotes when it has spaces or characters a shell
    reads."""
    if text and not re.search(r"[\s\"'\[\]*?&|<>;()=#$`^%!,]", text):
        return text
    return '"' + text.replace('"', '\\"') + '"'


def _width(text: str) -> int:
    """Columns ``text`` takes in a terminal (Korean letters take two)."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(1, width - _width(text))


def _scope_name(source) -> str:
    """How ``--option`` names a Source's option ('이름 (2번째)' for the second option with that name)."""
    return source.option if not source.n else f"{source.option} ({source.n + 1}번째)"


def _option_test(name: str | None):
    """Which Sources ``--option NAME`` means (any without it)."""
    if name is None:
        return lambda source: True
    name = name.strip()
    nth = re.fullmatch(r"(.+?) \((\d+)번째\)", name)

    def test(source) -> bool:
        if source.option == name:
            return True
        return nth is not None and source.option == nth.group(1) and source.n == int(nth.group(2)) - 1

    return test


def _no_option(mod, name: str) -> EditError:
    if not mod.options:
        return EditError(f"이 모드에는 옵션이 없습니다 (--option {_q(name)})")
    return EditError(f"옵션을 찾지 못했습니다: {name} (옵션: {', '.join(repr(o['name']) for o in mod.options)})")


def _check_option(manager: ModManager, snap, name: str | None) -> None:
    """_no_option when ``--option NAME`` names none of the mod's options."""
    if name is not None and not any(_option_test(name)(edit_model.Source(o.name, o.n)) for o in snap.options):
        raise _no_option(manager.get(snap.mod_id), name)


def _numbered_in(manager: ModManager, snap, target, ref: str, option: str | None):
    """The target numbered ``ref`` when ``--option`` (if given) names its option: a number is of one file, and
    the option given with it has to agree with it."""
    if option is None or _option_test(option)(target.source):
        return target
    _check_option(manager, snap, option)
    where = f"옵션 '{_scope_name(target.source)}'의 파일" if target.source.option else "옵션에 들지 않은 파일"
    raise EditError(f"{ref.strip()}번 파일은 {where}입니다 (옵션 '{option}'의 파일 번호: edit <모드> values "
                    f"--option {_q(option)})")


def _glob(pattern: str):
    """A case-insensitive test against a pattern in which * and ? are wildcards and everything else, brackets too,
    is literal (addresses such as ``_Values[*]._SellPrice`` are written as they are)."""
    rx = re.compile("".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern.strip()),
                    re.IGNORECASE | re.DOTALL)
    return lambda text: rx.fullmatch(text) is not None


def _path_test(pattern: str):
    """경로: a game path or a pattern, case-insensitive; one without a folder also matches file names."""
    pattern = pattern.strip().replace("\\", "/").strip("/")
    test = _glob(pattern)
    names = "/" not in pattern
    return lambda rel: test(rel) or (names and test(rel.rsplit("/", 1)[-1]))


def _path_rank(rel: str, path: str) -> int:
    """How well 파일 names a game path: 2 the whole path, 1 its end (a file name, or more of the path), 0 not."""
    a, b = rel.casefold(), path.casefold()
    return 2 if a == b else 1 if b and a.endswith("/" + b) else 0


def _split_ref(ref: str) -> tuple[str, str]:
    """(path, PAK entry ``#HASH16`` or "") of 파일."""
    ref = ref.strip().replace("\\", "/").strip("/")
    path, sep, tail = ref.rpartition("#")
    if sep and EDIT_MEMBER.fullmatch(tail):
        return path.strip("/"), "#" + tail.upper()
    return ref, ""


def _only_file(found: list, ref: str, option: str | None, many: bool = False) -> list:
    """``found`` (things with a ``source``) when they are one file in one option (or ``many``: in several),
    else EditError saying what to add."""
    if not found:
        raise EditError(f"파일을 찾지 못했습니다: {ref}" + (f" (옵션 '{option}')" if option else ""))
    rels = sorted({e.source.rel.casefold(): e.source.rel for e in found}.values(), key=str.casefold)
    if len(rels) > 1:
        raise EditError("이름이 같은 파일이 여러 개입니다. 경로를 더 적어 주세요: " + ", ".join(rels[:6])
                        + (" …" if len(rels) > 6 else ""))
    scopes = list(dict.fromkeys(e.source for e in found if e.source.option))
    if len(scopes) > 1 and not many:
        names = ", ".join(f"'{_scope_name(s)}'" for s in scopes)
        raise EditError(f"여러 옵션에 있는 파일입니다. --option으로 고르세요: {names}")
    return found


def _targets(manager: ModManager, snap, ref: str | None = None) -> list:
    """The files (and PAK entries) whose values the command line shows and changes, numbered in this order by
    ``values``: 게임 수치's, then 스크립트 설정's. A 파일 that cannot name a PAK entry does not read the PAKs."""
    data = snap
    if ref is not None and not ref.strip().isdigit() and "#" not in ref and ".pak" not in ref.lower():
        data = dataclasses.replace(snap, files=[f for f in snap.files if not is_root_pak(f.rel)])
    return [*manager.edit_targets(data, "data", _progress_printer()), *manager.edit_targets(snap, "config")]


def _numbered(targets: list, ref: str):
    n = int(ref.strip())
    if not 1 <= n <= len(targets):
        raise EditError(f"{n}번 파일이 없습니다 (번호는 edit <모드> values로 확인)")
    return targets[n - 1]


def _target_name(target) -> str:
    """A target as the command line shows it: [option: ]path[#HASH]."""
    return target.source.label() + target.member


def _find_targets(manager: ModManager, snap, ref: str, option: str | None, targets=None) -> list:
    """The targets 파일 names: one file (with every PAK entry when only the PAK is named)."""
    targets = targets if targets is not None else _targets(manager, snap, ref)
    if ref.strip().isdigit():
        return [_numbered_in(manager, snap, _numbered(targets, ref), ref, option)]
    path, member = _split_ref(ref)
    wanted = _option_test(option)
    ranked = [(t, _path_rank(t.source.rel, path)) for t in targets
              if wanted(t.source) and (not member or t.member == member)]
    best = max((r for _t, r in ranked), default=0)
    found = [t for t, r in ranked if r and r == best]
    if not found:
        _check_option(manager, snap, option)
        raise EditError(f"값을 바꿀 수 있는 파일 가운데 맞는 것이 없습니다: {ref}"
                        + (f" (옵션 '{option}')" if option else "") + " (목록: edit <모드> values)")
    return _only_file(found, ref, option)


def _one_target(manager: ModManager, snap, ref: str, option: str | None):
    found = _find_targets(manager, snap, ref, option)
    if len(found) > 1:
        raise EditError("PAK 안에 값을 바꿀 수 있는 파일이 여러 개입니다. #해시까지 적어 주세요: "
                        + ", ".join(f"{t.source.rel}{t.member}" for t in found))
    return found[0]


def _record_items(manager: ModManager, snap, record, ref: str, option: str | None, many: bool = False) -> list:
    """(record item, PAK entry or "") for 파일 among the files the record has edits of (also those an update took
    away). ``many``: every option the file is in."""
    if ref.strip().isdigit():
        target = _numbered_in(manager, snap, _numbered(_targets(manager, snap), ref), ref, option)
        item = record.item(target.source)
        if item is None or (target.member and item.member(target.member) is None):
            raise EditError(f"{ref}번 파일에는 수정한 값이 없습니다: {_target_name(target)}")
        return [(item, target.member)]
    path, member = _split_ref(ref)
    wanted = _option_test(option)
    ranked = [(i, _path_rank(i.source.rel, path)) for i in record.items if wanted(i.source)]
    best = max((r for _i, r in ranked), default=0)
    if not best:
        _check_option(manager, snap, option)
        raise EditError(f"수정한 파일 가운데 맞는 것이 없습니다: {ref}" + (f" (옵션 '{option}')" if option else "")
                        + " (수정 내용: edit <모드>)")
    found = _only_file([i for i, r in ranked if r == best], ref, option, many)
    for item in found:
        if member and item.member(member) is None:
            raise EditError(f"이 PAK 안 파일에는 수정한 값이 없습니다: {item.source.rel}{member}")
    return [(item, member) for item in found]


def _ref_args(items: list, source, member: str = "") -> str:
    """파일 (and --option) that names ``source`` among the record's items again: its file name when no other
    edited file has that name."""
    name = source.rel.rsplit("/", 1)[-1]
    same = {i.source.rel.casefold() for i in items if _path_rank(i.source.rel, name)}
    out = _q((name if len(same) <= 1 else source.rel) + (member or ""))
    if source.option:
        out += f" --option {_q(_scope_name(source))}"
    return out


def _refuse_bad(record) -> None:
    if record.error:
        raise EditError(f"모드 수정 기록을 읽지 못해 고칠 수 없습니다 ({record.error}). 기록을 옆으로 옮기려면 "
                        "clear-record --yes, 모든 수정을 되돌리려면 revert --all")


def _left_text(result) -> str:
    parts = []
    if result.conflicts:
        parts.append(f"확인할 값 {result.conflicts}개")
    if result.pending:
        parts.append(f"다시 적용하지 못한 파일 {result.pending}개")
    return " · ".join(parts)


def _save_edits(manager: ModManager, mod, snap, pending, args, what: str, force: bool = False):
    """Save what ``pending`` asks for (apply_edit_plan) and apply it: (exit code, EditResult or None)."""
    plan = pending.plan(mod.id, snap.stamp, snap.rev)
    if plan.empty():
        print("바뀐 것이 없습니다.")
        return 0, None
    result = manager.apply_edit_plan(plan, force=force, dry_run=getattr(args, "dry_run", False),
                                     progress=_progress_printer())
    for note in result.notes:
        print(f"  참고: {note}")
    if getattr(args, "dry_run", False):
        print("(--dry-run: 저장하지 않았습니다)")
        return 0, result
    if not result.changed:
        print("바뀐 것이 없습니다.")
        return 0, result
    print(f"모드 수정을 저장했습니다: {mod.name} ({what})")
    left = _left_text(result)
    if left:
        print(f"{left} (확인: edit {_q(mod.name)})")
    if result.materials:
        print(f"뺀 파일이 바뀌어 재질 점검 결과는 다시 점검할 때까지 '점검 전'입니다: materials check {_q(mod.name)}")
    return _maybe_deploy(manager, args), result


# -- reading
def _edit_status(manager: ModManager, mod, args) -> int:
    summary = manager.edit_summary(mod.id)
    record = manager.edit_record(mod.id)
    name = _q(mod.name)
    print(f"{mod.name}: {summary.text()}")
    if record.error:
        for line in manager.edit_issues(mod.id):
            print(f"  {line}")
        print(f"기록을 옆으로 옮기려면: edit {name} clear-record --yes · 모든 수정을 되돌리려면: edit {name} revert --all")
        return 0
    notice = record.notice
    if notice.get("kind") == "update":
        if notice.get("error"):
            print(f"지난 업데이트: {notice['error']}")
        else:
            report = edit_model.CarryReport.from_notice(notice)
            print(f"지난 업데이트: {report.text()}")
            for label in report.dropped:
                print(f"  없어진 파일 (빼 두었던 것): {label}")
    for view in manager.edit_items(mod.id):
        _print_edit_item(view, record, name)
    if not summary.empty:
        print(EDIT_PROFILES)
        if any(i.source.option for i in record.items):
            print(EDIT_OPTIONS)
    if notice:
        try:  # (shown once, as the window's 수정 내용 shows it; what is left to check stays in the list above)
            manager.drop_edit_notice(mod.id)
        except OSError as exc:
            logging.getLogger(__name__).info("업데이트 알림을 지우지 못했습니다: %s", exc)
    return 0


def _print_edit_item(view, record, name: str) -> None:
    """One line of 수정 내용 with its values, what is wrong with it and the command that settles it."""
    ref = _ref_args(record.items, view.source, view.member)
    state = {"inactive_option": " (옵션이 꺼져 있어 지금은 게임에 들어가지 않습니다)",
             "inactive_excluded": " (뺀 파일이라 지금은 게임에 들어가지 않습니다)",
             "pending": f" (다시 적용하지 못했습니다: {view.why})", "waiting": " (다시 적용 대기)",
             "item_conflict": f" ✕ {view.why}"}.get(view.state, "")
    print(f"  {_pad(EDIT_TAGS.get(view.kind, '[수치]'), 8)}{view.source.label()}{view.member}{state}")
    for c in view.changes:
        print(f"          {c.at}: {c.before} → {c.after}" + (f" (행 {c.count}개)" if c.rule else ""))
    for x in view.conflicts:
        print(f"        ✕ {x.change.at}: {x.text()}")
    if view.state == "item_conflict":
        print(f"          (지우기: edit {name} revert {ref})")
    elif len(view.conflicts) == 1:
        print(f"          (edit {name} resolve {ref} {_q(view.conflicts[0].change.at)} --keep|--take)")
    elif view.conflicts:
        print(f"          (edit {name} resolve {ref} --every --keep|--take, 값마다 고르려면 --every 대신 주소)")
    if view.state in ("pending", "waiting"):
        print(f"          (다시 적용: edit {name} reapply {ref.split(' --option ')[0]})")


def _edit_files(manager: ModManager, mod, args) -> int:
    snap = manager.edit_snapshot(mod.id)
    wanted = _option_test(args.option)
    rows = [f for f in snap.files if wanted(f.source)]
    if args.option is not None and not rows:
        raise _no_option(mod, args.option)
    if args.find:
        rows = [f for f in rows if args.find.casefold() in f.rel.casefold()]
    if args.excluded:
        rows = [f for f in rows if f.state == "excluded"]
    if args.overlaps:
        rows = [f for f in rows if len(f.overlap) > 1]
    head = f"{mod.name}: 파일 {len(snap.files)}개"
    if snap.options:
        head += " · 켠 옵션: " + (", ".join(_scope_name(edit_model.Source(o.name, o.n)) for o in snap.options
                                          if o.selected) or "없음")
    excluded = sum(1 for f in snap.files if f.state == "excluded")
    if excluded:
        head += f" · 뺀 파일 {excluded}개"
    print(head)
    width = max((_width(f"[{EDIT_FILE_STATES.get(f.state, f.state)}]") for f in rows), default=0) + 2
    for f in rows:
        extras = [f.kind]
        if f.values:
            extras.append(f"바꾼 값 {f.values}개")
        if f.key in snap.pending:
            extras.append("다시 적용 대기")
        if f.repaired:
            extras.append("재질 복구본")
        if f.placed in ("changed", "stash"):
            extras.append("게임 폴더에서 바뀜")
        if f.overridden_by:
            extras.append(f"아래쪽 옵션 '{f.overridden_by}'의 파일이 덮어씀")
        if len(f.overlap) > 1:
            extras.append("켠 옵션 " + ", ".join(f.overlap) + "에 있음")
        other = snap.overlaps.get(path_key(f.rel)) if f.state == "in" else None
        if other:
            extras.append(f"다른 모드 '{other}'이(가) 덮어씀")
        print(f"  {_pad('[' + EDIT_FILE_STATES.get(f.state, f.state) + ']', width)}{f.source.label()}  "
              f"({' · '.join(extras)})")
    if not rows:
        print("  (맞는 파일이 없습니다)")
    elif args.overlaps:
        print(f"쓸 옵션을 고르려면: edit {_q(mod.name)} use <경로> --option <이름>")
    return 0


def _member_what(target) -> str:
    """What a PAK entry is, from its label ("TestItemData", "ItemData (ItemData.user.3)"), without the hash its
    name already shows."""
    what = target.label.removeprefix(f"{target.source.rel} · ").split(" (옵션 ")[0]
    return re.sub(r" \(#[0-9A-Fa-f]+\)$", "", what)


def _list_targets(mod, targets: list, shown: list, none: str = "") -> None:
    """The numbered list of ``values`` (``shown``: the targets to list, numbered by their place in ``targets``;
    ``none``: what to say when that is none of them)."""
    if not targets:
        print(f"{mod.name}: 값을 바꿀 수 있는 파일이 없습니다 (natives 폴더의 .user 데이터 파일, 모드 PAK 안의 데이터 "
              "파일, reframework 폴더의 스크립트 설정 파일만 바꿀 수 있습니다).")
        return
    print(f"{mod.name}: 값을 바꿀 수 있는 파일 {len(targets)}개")
    if not shown and none:
        print(f"  ({none})")
    numbers = {id(t): n for n, t in enumerate(targets, 1)}
    for t in shown:
        kind = "[데이터]" if t.tool == "data" else "[설정]"
        what = _member_what(t) if t.member else ""
        extra = (f" · {what}" if what else "") + (" (수정함)" if t.edited else "")
        print(f"  {numbers[id(t)]:3d}. {_pad(kind, 9)}{_target_name(t)}{extra}")
        if t.readonly:
            print(f"        바꿀 수 없음: {t.readonly}")
        if t.note:
            print(f"        참고: {t.note}")
    print(f"값을 보려면: edit {_q(mod.name)} values <번호 또는 파일>")


def _differs(view, engine) -> list[bool]:
    """Whether each row's mod value differs from the game's (or the game has no such row): 모드가 바꾼 값. A value
    without a game value in a row the game has was not paired (row numbers, arrays of another length): not."""
    out = []
    for row in view.rows:
        if not view.compared:
            out.append(False)
        elif row.game is None:
            out.append(row.value.group in view.added)
        else:
            out.append(row.game != row.base and not engine.same(row.value, row.base, row.game))
    return out


def _current(row) -> str:
    """The value the game gets now: the user's, a rule's or the mod's."""
    if row.mine is not None:
        return row.mine.after
    if row.rule is not None:
        return row.rule.after
    return row.base


def _hint_values(hint: str) -> list[str]:
    """The values of a "게임에 쓰인 값: 0, 1, 2" hint."""
    _head, sep, rest = hint.partition(": ")
    return [x for x in rest.split(", ") if x] if sep else []


def _row_text(row, compared: bool, added=frozenset()) -> str:
    v = row.value
    game = row.game if row.game is not None else "없음" if v.group in added else "알 수 없음"
    parts = [f"게임 {game}", f"모드 {row.base}"] if compared else [row.base]
    if row.mine is not None:
        parts.append(row.mine.after)
    elif row.rule is not None:
        parts.append(f"{row.rule.after} (규칙: 값이 {row.rule.before}인 행 모두)")
    text = " → ".join(parts)
    if row.conflict is not None:
        text += f"  ✕ {row.conflict.text()}"
    if not v.editable:
        text += f"  (바꿀 수 없음: {v.why})" if v.why else "  (바꿀 수 없음)"
    if v.warn:
        text += " ⚠"
    return text


def _edit_values(manager: ModManager, mod, args) -> int:
    snap = manager.edit_snapshot(mod.id)
    if args.file is None:
        targets = _targets(manager, snap)
        wanted = _option_test(args.option)
        shown = [t for t in targets if wanted(t.source)]
        if args.option is not None and not shown:
            _check_option(manager, snap, args.option)
        _list_targets(mod, targets, shown,
                      f"옵션 '{args.option}'에는 값을 바꿀 수 있는 파일이 없습니다" if args.option is not None else "")
        return 0
    targets = _targets(manager, snap, args.file)
    found = _find_targets(manager, snap, args.file, args.option, targets)
    if len(found) > 1:  # a PAK with several data files (named without "#": every PAK was read, numbers hold)
        print("PAK 안에 값을 바꿀 수 있는 파일이 여러 개입니다. #해시까지 적거나 번호로 고르세요.")
        _list_targets(mod, targets, found)
        return 0
    target = found[0]
    view = manager.open_values(snap, target, _progress_printer())
    engine = edit_engines.get(target.engine)
    data = target.tool == "data"
    differs = _differs(view, engine)
    title = _target_name(target)
    if data and not view.readonly:
        title += f" ({_root_type(manager, target, view)})"
    parts = [f"값 {view.total:,}개"]
    if data and view.compared:
        parts.append(f"모드가 바꾼 값 {sum(differs):,}개")
    elif not data and target.engine == "lua":
        parts.append(f"설정 같은 값 {sum(1 for r in view.rows if r.value.settings):,}개")
    print(f"{title} · {' · '.join(parts)}")
    if view.readonly:
        print(f"바꿀 수 없음: {view.readonly}")
        return 0
    for note in [target.note, *view.notes]:
        if note:
            print(f"참고: {note}")

    def edited(row) -> bool:
        return row.mine is not None or row.conflict is not None or row.rule is not None

    if args.all or (data and not view.compared):
        rows = list(view.rows)
    elif data:
        rows = [r for r, d in zip(view.rows, differs) if d or edited(r)]
    else:
        rows = [r for r in view.rows if r.value.settings or edited(r)]
    filtered = len(rows) < len(view.rows)
    if args.find:
        key = args.find.casefold()
        rows = [r for r in rows if key in r.value.at.casefold() or key in r.value.label.casefold()]
    total = len(rows)
    if args.limit > 0:
        rows = rows[:args.limit]
    width = min(max((_width(r.value.at) for r in rows), default=0), 60)
    kinds = max((len(r.value.kind) for r in rows), default=0)
    for r in rows:
        print(f"  {_pad(r.value.at, width + 2)}{r.value.kind:<{kinds}}   {_row_text(r, view.compared, view.added)}")
    if not rows:
        print("  (맞는 값이 없습니다)")
    if total > len(rows):
        print(f"  … {total:,}개 중 {len(rows):,}개 (--limit으로 더 보기)")
    if filtered:
        print(f"({EDIT_FILTERS[target.tool]} 보여 줍니다. 모두 보기: --all)")
    hints = list(dict.fromkeys((r.value.rule_at, r.hint) for r in rows if r.hint and r.value.rule_at))
    for rule_at, hint in hints[:EDIT_HINTS]:
        print(f"  {hint} ({rule_at})")
    return 0


def _root_type(manager: ModManager, target, view) -> str:
    """The data file's (or PAK entry's) type (app.user_data.ItemData)."""
    from .modedit import pakedit, rszdata

    try:
        if target.member:
            data = pakedit.read_member(Path(view.base.path), pakedit.member_hash(target.member))
        else:
            data = Path(view.base.path).read_bytes()
        reg = rszdata.registry()
        try:
            return rszdata.root_type(data, reg) or "데이터"
        finally:
            rszdata.release()
    except (OSError, KeyError, EditError):
        return "데이터"


# -- writing
def _edit_exclude(manager: ModManager, mod, args) -> int:
    flag = args.action == "exclude"
    snap = manager.edit_snapshot(mod.id)
    _refuse_bad(snap.record)
    wanted = _option_test(args.option)
    scope = [f for f in snap.files if wanted(f.source)]
    if args.option is not None and not scope:
        raise _no_option(mod, args.option)
    chosen: dict[str, object] = {}
    for pattern in args.paths:
        test = _path_test(pattern)
        hits = [f for f in scope if test(f.rel)]
        if not hits:
            raise EditError(f"맞는 파일이 없습니다: {pattern}" + (f" (옵션 '{args.option}')" if args.option else ""))
        scopes = list(dict.fromkeys(f.source.option and _scope_name(f.source) for f in hits))
        if len(scopes) > 1 and args.option is None and not args.all_options:
            raise EditError(f"여러 옵션에 있는 파일입니다: {pattern}. --option으로 고르거나, 모든 옵션에서 "
                            f"{'빼' if flag else '넣'}려면 --all-options를 붙이세요 (옵션: "
                            + ", ".join(f"'{s}'" for s in scopes) + ")")
        for f in hits:
            chosen.setdefault(f.key, f)
    by_rel = {(f.source.option, f.source.n, f.rel): f for f in snap.files}
    twins = set()
    for f in list(chosen.values()):
        twin = by_rel.get((f.source.option, f.source.n, f.twin)) if f.twin else None
        if twin is not None and twin.key not in chosen:
            chosen[twin.key] = twin
            twins.add(twin.key)
    pending = edit_model.Pending(snap.record)
    count = 0
    for f in chosen.values():
        if pending.excluded(f.source) == flag:
            if f.key not in twins:  # (a twin that already follows says nothing)
                print(f"  {'이미 뺀' if flag else '빼지 않은'} 파일입니다: {f.source.label()}")
            continue
        pending.exclude(f.source, flag)
        count += 1
        twin = f" (스트리밍 사본도 함께 {'뺍니다' if flag else '넣습니다'})" if f.key in twins else ""
        print(f"  {'뺌' if flag else '다시 넣음'}: {f.source.label()}{twin}")
    for line in _exclude_notes(manager, mod, snap, pending, list(chosen.values()), flag):
        print(line)
    code, _result = _save_edits(manager, mod, snap, pending, args,
                                f"{'뺀' if flag else '다시 넣은'} 파일 {count}개")
    return code


def _exclude_notes(manager: ModManager, mod, snap, pending, chosen: list, flag: bool) -> list[str]:
    """What the window would point out about the files left out or put back (shown, never enforced)."""
    out = []
    after = {f.key: pending.excluded(f.source) for f in snap.files}
    live = [f for f in snap.files if not f.option_id or f.option_id in mod.selected] or snap.files
    if flag and all(after[f.key] for f in live):
        out.append("경고: 모든 파일을 빼면 이 모드는 게임에 아무것도 넣지 않습니다.")
    for f in chosen:
        if f.state == "option_off" and flag:
            out.append(f"참고: {f.source.label()}: 이 옵션은 지금 꺼져 있어 빼도 게임에는 달라지는 것이 없습니다.")
        elif f.state == "overridden":
            out.append(f"참고: {f.source.label()}: 아래쪽 옵션 '{f.overridden_by}'의 파일이 이 파일을 덮어씁니다. 그 옵션의 "
                       "파일을 빼면 이 파일이 들어갑니다.")
        other = snap.overlaps.get(path_key(f.rel))
        if other and not flag:
            out.append(f"참고: {f.source.label()}: 다른 모드 '{other}'이(가) 이 파일을 덮어씁니다.")
    groups: dict[tuple, list] = {}  # (scope, folder, stem) -> files: .sbnk/.spck, .mesh/.mdf2 go together
    for f in snap.files:
        folder, _, name = f.rel.rpartition("/")
        groups.setdefault((f.source.option, f.source.n, folder.casefold(), name.split(".")[0].casefold()), []).append(f)
    seen = set()
    for f in chosen:
        folder, _, name = f.rel.rpartition("/")
        key = (f.source.option, f.source.n, folder.casefold(), name.split(".")[0].casefold())
        if key in seen:
            continue
        seen.add(key)
        group = groups.get(key, [])
        kept = [g.rel.rsplit("/", 1)[-1] for g in group if not after[g.key]]
        left = [g.rel.rsplit("/", 1)[-1] for g in group if after[g.key]]
        if kept and left:
            stem = name.split(".")[0]
            again = f"edit {_q(mod.name)} exclude {_q((folder + '/' if folder else '') + stem + '.*')}" + (
                f" --option {_q(_scope_name(f.source))}" if f.source.option else "")
            out.append(f"참고: 같은 이름의 파일 가운데 일부만 뺐습니다: {stem} ({', '.join(kept)}은 넣고 "
                       f"{', '.join(left)}은 뺌). 함께 빼야 할 수 있습니다. (함께 빼기: {again})")
    if flag:
        scripts = [f for f in chosen if f.rel.lower().endswith(".lua")]
        users = [g for g in snap.files if g.rel.lower().endswith(".lua") and g.state == "in" and not after[g.key]]
        for f in scripts:
            provided = requirements.provided_modules([f.rel])
            for g in users:
                if any(requirements.module_key(n) in provided for n in requirements.required_modules({g.rel: g.path})):
                    out.append(f"참고: {f.source.label()}: 다른 스크립트({g.rel.rsplit('/', 1)[-1]})가 이 파일을 "
                               "불러옵니다.")
                    break
    return out


def _edit_use(manager: ModManager, mod, args) -> int:
    snap = manager.edit_snapshot(mod.id)
    _refuse_bad(snap.record)
    if not snap.options:
        raise EditError("옵션이 없는 모드입니다.")
    name, n = args.option.strip(), args.nth - 1
    nth = re.fullmatch(r"(.+?) \((\d+)번째\)", name)
    if nth is not None and not any(o.name == name for o in snap.options):
        name, n = nth.group(1), int(nth.group(2)) - 1
    ref = next((o for o in snap.options if o.name == name and o.n == n), None)
    if ref is None:
        raise _no_option(mod, name if not n else f"{name} ({n + 1}번째)")
    label = _scope_name(edit_model.Source(ref.name, ref.n))
    if not ref.selected:
        raise EditError(f"옵션 '{label}'이(가) 꺼져 있습니다. 켠 옵션 가운데에서만 고를 수 있습니다 (켜기: options "
                        f"{_q(mod.name)} --set <번호>).")
    selected = {o.id for o in snap.options if o.selected}
    test = _path_test(args.path)
    mine = [f for f in snap.files if f.option_id == ref.id and test(f.rel)]
    if not mine:
        raise EditError(f"옵션 '{label}'에 맞는 파일이 없습니다: {args.path}")
    by_rel = {(f.source.option, f.source.n, f.rel): f for f in snap.files}
    pending = edit_model.Pending(snap.record)
    count, offered = 0, 0
    for f in mine:
        others = [g for g in snap.files if g.option_id in selected and g.option_id != ref.id
                  and path_key(g.rel) == path_key(f.rel)]
        if not others:
            print(f"  켠 옵션 가운데 '{label}'에만 있는 파일이라 그대로 둡니다: {f.rel}")
            continue
        offered += 1
        wanted = [(f, False)] + [(g, True) for g in others]  # (the chosen copy goes in, the others' are left out)
        wanted += [(by_rel[(g.source.option, g.source.n, g.twin)], flag) for g, flag in list(wanted)
                   if g.twin and (g.source.option, g.source.n, g.twin) in by_rel]
        changed = False
        for g, flag in wanted:
            if pending.excluded(g.source) != flag:
                pending.exclude(g.source, flag)
                count += flag
                changed = True
        if changed:
            print(f"  {f.rel}: 옵션 '{label}'의 파일을 씁니다 (뺌: "
                  + ", ".join(f"'{_scope_name(g.source)}'" for g in others) + ")")
        else:
            print(f"  {f.rel}: 이미 옵션 '{label}'의 파일을 쓰고 있습니다")
    if not offered:
        raise EditError(f"다른 켠 옵션에 같은 파일이 없어 고를 것이 없습니다: {args.path}")
    print(f"(자동으로 아래쪽 옵션의 파일을 쓰려면: edit {_q(mod.name)} include {_q(args.path)} --all-options)")
    code, _result = _save_edits(manager, mod, snap, pending, args, f"옵션 '{label}'의 파일 쓰기 · 뺀 파일 {count}개")
    return code


def _split_pair(pair: str, known) -> tuple[str, str]:
    """(address, value) of 주소=값: the value starts after an = outside [...] (and outside quotes in them); of
    several, the one after an address the file has."""
    cuts, depth, quoted, escaped = [], 0, False, False
    for i, ch in enumerate(pair):
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"' and depth:
            quoted = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == "=" and not depth:
            cuts.append(i)
    if not cuts:
        raise EditError(f"'주소=값' 꼴로 적어 주세요: {pair}")
    for i in cuts:
        if pair[:i].strip() in known:
            return pair[:i].strip(), pair[i + 1:]
    raise EditError(f"이 파일에 없는 주소입니다: {pair[:cuts[-1]].strip()} (주소 확인: edit <모드> values <파일> --all)")


def _typed(v, text: str) -> str:
    """켬/끔 for a bool of any engine (the config engines take true/false)."""
    if v.kind in edit_engines.BOOL_KINDS:
        return {"켬": "true", "끔": "false"}.get(text.strip(), text)
    return text


def _print_target_note(target) -> None:
    """Why the values of the file do not reach the game now (another option's copy, its option off, the file
    left out) or what saving a big PAK takes: the window shows it on its values page."""
    if target.note:
        print(f"참고: {target.note}")


def _value_view(manager: ModManager, mod, snap, args):
    """(target, its ValueView, engine) of 파일, refused when its values cannot be changed."""
    _refuse_bad(snap.record)
    target = _one_target(manager, snap, args.file, args.option)
    if target.readonly:
        raise EditError(f"{_target_name(target)}: {target.readonly}")
    view = manager.open_values(snap, target, _progress_printer())
    if view.readonly:
        raise EditError(f"{_target_name(target)}: {view.readonly}")
    return target, view, edit_engines.get(target.engine)


def _edit_set(manager: ModManager, mod, args) -> int:
    from .modedit import rszdata

    snap = manager.edit_snapshot(mod.id)
    target, view, engine = _value_view(manager, mod, snap, args)
    data = target.tool == "data"
    if args.same and not data:
        raise EditError("--same은 데이터 파일(.user)에서만 쓸 수 있습니다.")
    rows = {r.value.at: r for r in view.rows}
    pending = edit_model.Pending(snap.record)
    count, ints = 0, False
    for pair in args.pairs:
        at, text = _split_pair(pair, rows)
        row = rows[at]
        v = row.value
        if not v.editable:
            raise EditError(f"바꿀 수 없는 값입니다: {at}" + (f" ({v.why})" if v.why else ""))
        after = engine.parse(v, _typed(v, text))
        if args.same:
            if not v.rule_at:
                raise EditError(f"표의 행 값이 아니라 --same을 쓸 수 없습니다: {at}")
            n = sum(1 for r in view.rows if r.value.rule_at == v.rule_at and r.value.editable
                    and engine.same(r.value, r.value.text, v.text))
            pending.set_value(target.source, target.member, target.engine, target.label, at=v.rule_at, kind=v.kind,
                              before=v.text, after=after, rule=True, name=v.label)
            print(f"  {v.rule_at} (값이 {v.text}인 행 {n}개): {v.text} → {after}")
        else:
            if row.rule is not None and row.mine is None and engine.same(v, after, v.text):
                raise EditError(f"{at}: 이 값은 '값이 {row.rule.before}인 행 모두 → {row.rule.after}' 규칙으로 바뀝니다. "
                                f"원래 값으로 하려면 규칙을 되돌리세요: edit {_q(mod.name)} revert "
                                f"{_ref_args(snap.record.items, target.source, target.member)} {_q(row.rule.at)}")
            pending.set_value(target.source, target.member, target.engine, target.label, at=v.at, kind=v.kind,
                              before=v.text, after=after, raw=v.raw, name=v.label)
            back = " (원래 값)" if engine.same(v, after, v.text) and row.mine is not None else ""
            print(f"  {at}: {_current(row)} → {after}{back}")
        if v.warn:
            print(f"    참고: {v.warn}")
        if data and v.kind in rszdata.INTS:
            ints = True
            used = _hint_values(row.hint)
            if used and not any(engine.same(v, after, x) for x in used):
                print(f"    경고: {rszdata.NOT_IN_HINT} ({row.hint})")
        count += 1
    if data:
        if ints:
            print(f"참고: {rszdata.INT_CAUTION}")
        print(f"참고: {rszdata.ONLINE_CAUTION}")
    _print_target_note(target)
    code, _result = _save_edits(manager, mod, snap, pending, args, f"바꾼 값 {count}개")
    return code


def _plain_number(text: str, flag: str) -> str:
    """K of --times/--change (or N of --to for number values) with its thousands separators taken out (1,000);
    any other comma is refused, not dropped: a decimal comma (0,5) would read as 5."""
    text = text.strip()
    if "," not in text:
        return text
    if not EDIT_THOUSANDS.fullmatch(text):
        raise EditError(f"--{flag}에는 숫자를 적어 주세요: {text} (소수점은 .으로)")
    return text.replace(",", "")


def _number_like(text: str) -> bool:
    """Whether the number values read ``text`` as a number once its commas are dropped (engines.scaled)."""
    text = text.strip().replace(",", "")
    try:
        if text.lower().lstrip("+-").startswith("0x"):
            int(text, 16)
        else:
            float(text)
    except ValueError:
        return False
    return True


def _skip_text(reason: str, n: int) -> str:
    """Values scale skipped for one reason: a reason that names the values ("게임 값이 없어 건너뛴 값") with the count
    after it, a sentence (an engine's why: "켜기·끄기 값은 ...") in brackets after "건너뛴 값 n개"."""
    if reason.endswith("값"):
        return f"{reason} {n:,}개"
    return f"건너뛴 값 {n:,}개 ({reason.rstrip('. ')})"


def _edit_scale(manager: ModManager, mod, args) -> int:
    from .modedit import rszdata

    snap = manager.edit_snapshot(mod.id)
    target, view, engine = _value_view(manager, mod, snap, args)
    mode, k = ("set", args.to) if args.to is not None else ("times", args.times) if args.times is not None \
        else ("change", args.change)
    if mode != "set":
        k = _plain_number(k, mode)
        try:
            factor = float(k)
        except ValueError:
            factor = float("nan")
        if not math.isfinite(factor):
            raise EditError(f"--{mode}에는 숫자를 적어 주세요: {k}")
        if not view.compared:
            raise EditError("게임 파일과 비교하지 못해 --times/--change를 쓸 수 없습니다")
    elif not view.compared and not args.field:
        raise EditError("게임 파일과 비교하지 못해 모드가 바꾼 값을 알 수 없습니다. 바꿀 값을 --field로 고르세요")
    field = _glob(args.field) if args.field else None
    numeric = set(edit_engines.INT_RANGES) | set(edit_engines.FLOAT_KINDS)
    rows = []
    for row, differs in zip(view.rows, _differs(view, engine)):
        v = row.value
        if not v.editable:
            continue
        if field is not None and not (field(v.at) or (v.rule_at and field(v.rule_at)) or field(v.label)):
            continue
        if field is None and v.kind not in numeric:
            continue
        if view.compared and (row.game is None or not differs):
            continue  # (values paired with the game's that the mod changed)
        rows.append(row)
    if not rows:
        which = ("모드가 바꾼 값 가운데 --field에 맞는 값이 없습니다" if view.compared else "--field에 맞는 값이 없습니다") \
            if args.field else "모드가 바꾼 숫자 값이 없습니다"
        raise EditError(f"바꿀 값이 없습니다: {which} (값 하나는 set으로 바꿀 수 있습니다)")
    if mode == "set" and _number_like(k) and any(r.value.kind in numeric for r in rows):
        k = _plain_number(k, "to")  # (a number for the number values: 1,000 is 1000, 0,5 is refused)
    pending = edit_model.Pending(snap.record)
    count, skipped, ints = 0, {}, False
    for row in rows:
        v = row.value
        text, note = edit_engines.scaled(engine, v, mode, k, row.base, row.game)
        if text is None:
            skipped[note] = skipped.get(note, 0) + 1
            continue
        if mode != "set" and v.at not in view.keyed:  # (its game value may be another row's: rows paired by place)
            skipped[EDIT_UNKEYED] = skipped.get(EDIT_UNKEYED, 0) + 1
            continue
        if row.rule is not None and row.mine is None and engine.same(v, text, v.text):
            skipped["규칙으로 바뀌는 값이라 건너뛴 값"] = skipped.get("규칙으로 바뀌는 값이라 건너뛴 값", 0) + 1
            continue
        if engine.same(v, text, _current(row)):
            skipped["이미 그 값이라 건너뛴 값"] = skipped.get("이미 그 값이라 건너뛴 값", 0) + 1
            continue
        how = {"mode": mode, "k": k} if mode == "set" else {"mode": mode, "k": factor, "game": row.game}
        pending.set_value(target.source, target.member, target.engine, target.label, at=v.at, kind=v.kind,
                          before=v.text, after=text, how=how, raw=v.raw, name=v.label)
        print(f"  {v.at}: {_current(row)} → {text}" + (f" ({note})" if note else ""))
        ints = ints or v.kind in rszdata.INTS
        count += 1
    for note, n in skipped.items():
        print(f"  {_skip_text(note, n)}")
    if EDIT_UNKEYED in skipped:
        print("  (게임과 행을 맞추지 못한 값은 --to나 set으로 바꿀 수 있습니다)")
    if target.tool == "data":
        if ints:
            print(f"참고: {rszdata.INT_CAUTION}")
        print(f"참고: {rszdata.ONLINE_CAUTION}")
    _print_target_note(target)
    code, _result = _save_edits(manager, mod, snap, pending, args, f"바꾼 값 {count}개")
    return code


def _rule_of_row(manager: ModManager, snap, source, member: str, at: str):
    """The rule of the record that covers the row ``at`` (None when none does), read in the file."""
    target = next((t for t in _targets(manager, snap, source.rel + member)
                   if t.source == source and t.member == member), None)
    if target is None:
        return None
    view = manager.open_values(snap, target, _progress_printer())
    return next((r.rule for r in view.rows if r.value.at == at), None)


def _twin_out(snap, pending, source):
    """The streaming copy of a stored file (or the file a streaming copy is of), in the same option, when it is
    left out too: put back with it."""
    f = next((g for g in snap.files if g.source == source), None)
    if f is None or not f.twin:
        return None
    twin = next((g for g in snap.files if g.option_id == f.option_id and path_key(g.rel) == path_key(f.twin)), None)
    return twin if twin is not None and twin.state != "conflict" and pending.excluded(twin.source) else None


def _option_revert_error(manager: ModManager, mod, snap, option: str, everything: bool) -> EditError:
    """revert without 파일 but with ``--option``: --all is every option's edits (as 모든 수정 되돌리기), so
    an option's edits are reverted file by file; the files the record has edits of in that option."""
    _check_option(manager, snap, option)
    _refuse_bad(snap.record)
    name, items = _q(mod.name), snap.record.items
    mine = [i for i in items if _option_test(option)(i.source)]
    head = ("--all은 모든 옵션의 수정을 되돌리므로 --option과 함께 쓸 수 없습니다. " if everything
            else "되돌릴 파일을 적어 주세요. ")
    if not mine:
        tail = f"옵션 '{option.strip()}'에는 수정한 파일이 없습니다"
    else:
        tail = (f"옵션 '{option.strip()}'의 수정은 파일마다 되돌립니다: "
                + " · ".join(f"edit {name} revert {_ref_args(items, i.source)}" for i in mine[:3])
                + (" …" if len(mine) > 3 else ""))
    if not everything:
        tail += f" (모든 옵션의 수정을 되돌리려면: edit {name} revert --all)"
    return EditError(head + tail)


def _edit_revert(manager: ModManager, mod, args) -> int:
    snap = manager.edit_snapshot(mod.id)
    record = snap.record
    pending = edit_model.Pending(record)
    if args.file is None:
        if args.option is not None:  # (refused, not dropped: --all would revert the other options' edits too)
            raise _option_revert_error(manager, mod, snap, args.option, args.all)
        if not args.all:
            raise EditError(f"모든 수정을 되돌리려면 --all을 붙이세요: edit {_q(mod.name)} revert --all")
        summary = manager.edit_summary(mod.id)
        if summary.empty and not record.error:
            print("되돌릴 수정이 없습니다.")
            return 0
        pending.reset_all()
        print(f"  모든 수정을 되돌립니다: {summary.text()}")
        if record.error:
            print(f"  (읽지 못한 기록은 {EDITS_RECORD}{BAD_SUFFIX}<시각>으로 옮겨 둡니다)")
        code, _result = _save_edits(manager, mod, snap, pending, args, "모든 수정 되돌리기", force=args.yes)
        return code
    _refuse_bad(record)
    ((item, member),) = _record_items(manager, snap, record, args.file, args.option)
    where = item.source.label()
    count = 0
    if not args.addresses:
        if member:  # one entry of a PAK
            m = item.member(member)
            for c in [*m.changes, *(x.change for x in m.conflicts)]:
                pending.revert_value(item.source, member, c.at, c.before if c.rule else None)
            count = len(m.changes) + len(m.conflicts)
            print(f"  되돌림: {where}{member} (값 {count}개)")
        else:
            pending.drop(item.source)
            what = [w for w in ("뺀 파일" if item.exclude else "", f"바꾼 값 {item.changes()}개" if item.changes() else "",
                                f"확인할 값 {item.conflicts()}개" if item.conflicts() else "") if w]
            count = 1
            print(f"  되돌림: {where} ({' · '.join(what) or '수정 기록'})")
            twin = _twin_out(snap, pending, item.source) if item.exclude else None
            if twin is not None:  # (a texture or model and its streaming copy go in together, as include puts them)
                pending.exclude(twin.source, False)
                count += 1
                print(f"  다시 넣음: {twin.source.label()} (스트리밍 사본도 함께 넣습니다)")
    else:
        names = [m.member for m in item.members]
        if not names:
            raise EditError(f"이 파일에는 바꾼 값이 없습니다: {where} (파일을 다시 넣으려면 include)")
        if not member:
            if len(names) != 1:
                raise EditError("PAK 안 파일을 #해시까지 적어 주세요: "
                                + ", ".join(f"{item.source.rel}{n}" for n in names))
            member = names[0]
        m = item.member(member)
        for at in args.addresses:
            at = at.strip()
            mine = [(c, None) for c in m.changes if c.at == at] + [(x.change, x) for x in m.conflicts
                                                                    if x.change.at == at]
            if mine:
                pending.revert_value(item.source, member, at)
                for c, x in mine:
                    if c.rule:
                        print(f"  {at}: 규칙 '값이 {c.before}인 행 모두 → {c.after}'를 되돌립니다")
                    elif x is not None:  # (never applied: the game gets what the file has now, as resolve --take)
                        print(f"  {at}: 작성자 값 쓰기" + (f" ({x.now})" if x.now is not None else " (내 수정을 지움)"))
                    else:
                        print(f"  {at}: {c.after} → {c.before} (원래 값)")
                count += 1
                continue
            rule = None
            if edit_model.engine_tool(m.engine) == "data" and any(c.rule for c in m.changes):
                rule = _rule_of_row(manager, snap, item.source, member, at)
            if rule is None:
                raise EditError(f"수정한 값이 아닙니다: {at}")
            pending.revert_value(item.source, member, at, rule.before)
            print(f"  {at}: 이 값은 '값이 {rule.before}인 행 모두 → {rule.after}' 규칙으로 바뀌었습니다. 원래 값으로 하면 "
                  f"규칙 전체를 되돌립니다 (행 {rule.count}개).")
            count += 1
    code, _result = _save_edits(manager, mod, snap, pending, args, f"되돌린 수정 {count}개", force=args.yes)
    return code


def _edit_resolve(manager: ModManager, mod, args) -> int:
    snap = manager.edit_snapshot(mod.id)
    record = snap.record
    _refuse_bad(record)
    ((item, member),) = _record_items(manager, snap, record, args.file, args.option)
    members = [m for m in item.members if m.conflicts and (not member or m.member == member)]
    if not members:
        raise EditError(f"확인할 값이 없습니다: {item.source.label()}{member}")
    if args.addresses and len(members) > 1:
        raise EditError("PAK 안 파일을 #해시까지 적어 주세요: "
                        + ", ".join(f"{item.source.rel}{m.member}" for m in members))
    pending = edit_model.Pending(record)
    count = 0
    for m in members:
        if args.every:
            chosen = list(m.conflicts)
        else:
            chosen = []
            for at in args.addresses:
                hits = [x for x in m.conflicts if x.change.at == at.strip()]
                if not hits:
                    raise EditError(f"확인할 값이 아닙니다: {at}")
                chosen += hits
        for x in chosen:
            at, before = x.change.at, x.change.before if x.change.rule else None
            if args.keep:
                if x.why != "changed" or x.now is None:
                    if args.every:
                        print(f"  {at}: {x.text()} - 내 값을 유지할 수 없어 그대로 둡니다 (--take로 지우기)")
                        continue
                pending.keep_mine(item.source, m.member, at, before)
                print(f"  {at}: 내 값 유지 ({x.now} → {x.change.after})")
            else:
                pending.take_author(item.source, m.member, at, before)
                print(f"  {at}: 작성자 값 쓰기" + (f" ({x.now})" if x.now is not None else " (내 수정을 지움)"))
            count += 1
    code, _result = _save_edits(manager, mod, snap, pending, args, f"확인한 값 {count}개")
    return code


def _edit_reapply(manager: ModManager, mod, args) -> int:
    snap = manager.edit_snapshot(mod.id)
    record = snap.record
    _refuse_bad(record)
    sources = []
    if args.files:
        for ref in args.files:
            sources += [item.source for item, _m in _record_items(manager, snap, record, ref, None, many=True)]
    else:
        sources = [v.source for v in manager.edit_items(mod.id) if v.state in ("pending", "waiting")]
    sources = list(dict.fromkeys(sources))
    if not sources:
        print("다시 적용할 수정이 없습니다.")
        summary = manager.edit_summary(mod.id)
        left = [x for x in (f"확인할 값 {summary.conflicts}개" if summary.conflicts else "",
                            f"다시 적용하지 못한 파일 {summary.pending}개" if summary.pending else "") if x]
        if left:
            print(f"{' · '.join(left)} (확인: edit {_q(mod.name)})")
            return 1
        return 0
    pending = edit_model.Pending(record)
    for source in sources:
        pending.reapply(source)
        print(f"  다시 적용: {source.label()}")
    code, result = _save_edits(manager, mod, snap, pending, args, f"다시 적용한 파일 {len(sources)}개",
                               force=args.yes)
    if result is not None and getattr(args, "dry_run", False) and _left_text(result):
        print(f"{_left_text(result)} (확인: edit {_q(mod.name)})")
    return 1 if result is not None and (result.conflicts or result.pending) else code


def _edit_clear(manager: ModManager, mod, args) -> int:
    record = manager.edit_record(mod.id)
    if not record.error:
        raise EditError("수정 기록을 읽을 수 있어 옮길 필요가 없습니다. 모든 수정을 되돌리려면: "
                        f"edit {_q(mod.name)} revert --all")
    if not args.yes:
        raise EditError(f"읽지 못한 수정 기록({record.error})을 {EDITS_RECORD}{BAD_SUFFIX}<시각>으로 옮기려면 "
                        "--yes를 붙이세요 (그 기록의 수정은 쓰지 않게 됩니다).")
    snap = manager.edit_snapshot(mod.id)
    pending = edit_model.Pending(record)
    pending.clear_bad()
    code, result = _save_edits(manager, mod, snap, pending, args, "읽지 못한 기록을 옆으로 옮김")
    if result is not None and result.changed:
        print(f"읽지 못한 기록은 모드 폴더의 {EDITS_RECORD}{BAD_SUFFIX}<시각>으로 옮겨 두었습니다.")
    return code


EDIT_COMMANDS = {
    "status": _edit_status, "files": _edit_files, "exclude": _edit_exclude, "include": _edit_exclude,
    "use": _edit_use, "values": _edit_values, "set": _edit_set, "scale": _edit_scale, "revert": _edit_revert,
    "resolve": _edit_resolve, "reapply": _edit_reapply, "clear-record": _edit_clear,
}


def _print_carry(manager: ModManager, mod) -> None:
    """What an update did to the mod's 모드 수정 (the record's notice; the window shows it once more)."""
    record = manager.edit_record(mod.id)
    notice = {} if record.error else record.notice
    if notice.get("kind") != "update":
        return
    if notice.get("error"):
        print(f"  모드 수정: {notice['error']}")
        return
    report = edit_model.CarryReport.from_notice(notice)
    print(f"  모드 수정: {report.text()}")
    for label in report.dropped:
        print(f"    없어진 파일 (빼 두었던 것): {label}")
    if report.problems:
        print(f"  확인하려면: edit {_q(mod.name)}")


def _reapply_edits(manager: ModManager, mod_ids) -> None:
    """Build again the 모드 수정 whose base a material check, repair or restore changed (the window does it when
    idle); not while 원인 모드 찾기 runs."""
    wanted = set(mod_ids)
    waiting = [i for i in manager.edits_pending() if i in wanted]
    if not waiting:
        return
    names = ", ".join(manager.get(i).name for i in waiting)
    if bisect.active(manager.data_dir):
        print(f"원인 모드 찾기 중이라 모드 수정은 다시 적용하지 않았습니다: {names} (마친 뒤: edit <모드> reapply)")
        return
    for result in manager.reapply_edits(waiting, _progress_printer()):
        if result.changed:
            left = _left_text(result)
            print(f"모드 수정을 다시 적용했습니다: {manager.get(result.mod_id).name}" + (f" · {left}" if left else ""))
    still = [manager.get(i).name for i in manager.edits_pending() if i in wanted]
    if still:
        print(f"모드 수정을 다시 적용하지 못했습니다: {', '.join(still)} (확인: edit <모드>)")


def _print_findings(findings) -> None:
    for f in findings:
        print(f"• {f.text}")
        for line in f.evidence[:3]:
            print(f"    근거: {line}")


def cmd_crash(manager: ModManager, args) -> int:
    """게임 튕김 기록: copy REFramework's records of the last game sessions, list them, show what one points
    at, save the support zip, or switch REFramework's Lua error logging."""
    from . import crashes
    from .crashlog import LUA_LOG_KEY

    if args.action == "lua":
        if args.ref not in (None, "on", "off"):
            raise SystemExit("사용법: crash lua [on|off]")
        if not manager.game_dir:
            print("게임 폴더를 먼저 설정하세요: game <경로>", file=sys.stderr)
            return 1
        if args.ref is not None:
            try:
                changed = manager.set_refw_option(LUA_LOG_KEY, args.ref == "on")
            except ValueError as exc:
                print(f"오류: {exc}", file=sys.stderr)
                return 1
            print("REFramework 설정을 바꿨습니다." if changed else "이미 그렇게 설정되어 있습니다.")
        on = crashes.lua_logging(manager.game_dir)
        print(f"Lua 스크립트 오류 기록 ({LUA_LOG_KEY}): {'켜짐' if on else '꺼짐'}")
        return 0
    manager.collect_crash_evidence()
    found = manager.crash_sessions()
    if args.action == "list":
        if not found:
            print("모은 기록이 없습니다.")
        for i, s in enumerate(found, 1):
            print(f"{i:2d}. {s.id}  {s.title()}")
        return 0
    session = crashes.find_session(manager.crash_dir, args.ref) if args.ref else (found[0] if found else None)
    if args.ref and session is None:
        raise SystemExit(f"기록을 찾을 수 없습니다: {args.ref} (crash list로 번호를 확인하세요)")
    index = manager.crash_index()
    explanation = crashes.explain(session, index)
    if args.action == "show":
        if session is not None:
            print(f"기록: {session.id}")
        for line in explanation.lines():
            print(line)
        if args.text:
            text = sys.stdin.read() if args.text == "-" else Path(args.text).read_text(encoding="utf-8",
                                                                                       errors="replace")
            pasted = crashes.explain_text(text, index, session)
            print("\n붙여 넣은 문구에서 찾은 것:")
            _print_findings(pasted)
            if not pasted:
                print("• 알아볼 수 있는 내용이 없습니다.")
        return 0
    out = Path(args.out or f"crash_report_{session.id if session else time.strftime('%Y%m%d-%H%M%S')}.zip")
    if args.dump and (session is None or session.dump_path is None):
        print("이 기록에는 크래시 덤프가 없습니다.", file=sys.stderr)
    manager.export_crash_report(out, session, explanation, manager.crash_report_mods(_kinds),
                                include_dump=args.dump)
    print(f"저장했습니다: {out}")
    return 0


def _print_plan(plan, when: str | None = None, minutes: float | None = None, started: bool = False) -> None:
    """The start window as text: the introduction, the suspects with their 근거, details
    (``when``/``minutes``: 언제 튕기나요 as given, else the plan's; ``started``: the search runs already, so it is
    changed by stopping it first)."""
    if plan.problem:
        print(plan.problem)
        return
    print(plan.intro())
    if plan.v2:
        print()
        print(bisect.RANKING_HEADING)
        for label, p, why in [] if plan.uniform else plan.ranking_rows():
            print(f"  {'약 ' + bisect.percent(p):<9}{label}")
            for line in why:
                print(f"           근거: {line}")
        note = plan.ranking_note()
        if note:
            print(f"  {note}")
        when = when or plan.when
        minutes = minutes if minutes is not None else plan.minutes
        print(f"언제 튕기나요: {bisect.WHEN_TEXT.get(when, when)}"
              + (f" · 약 {round(minutes)}분" if when == "time" and minutes else "")
              + f" (바꾸려면: {'bisect stop 후 ' if started else ''}bisect start --when start|same|time|rare [--minutes N])")
    for line in plan.lines():
        print(f"  • {line}")


def _print_ranking(step, names: dict) -> None:
    """The suspects of a step, most likely first (what the records and the answers so far give)."""
    rows = [s for s in step.ranking if s.p >= 0.01][:5]
    if not rows:
        return
    print("의심 순위 (기록과 지금까지의 답으로 매긴 것이고, 확실하다는 뜻은 아닙니다):")
    for s in rows:
        print(f"  {'약 ' + bisect.percent(s.p):<9}{bisect.suspect_label(s, names)}")


def _print_bisect(manager: ModManager, session, nxt) -> None:
    """Where a search is: the step to play, or its result (with its 근거) and the way back."""
    if isinstance(nxt, bisect.Step):
        print(session.banner(nxt, applied=not manager.dirty))
        if nxt.engine:
            print(bisect.progress_text(nxt, session.names))
        print("게임을 실행해 본 뒤 알려 주세요: bisect crashed (튕겼어요) · bisect fine (괜찮았어요) · "
              "bisect retry (잘 모르겠어요, 같은 구성으로 다시)"
              + (" · 게임을 지켜보지 못했다면 --minutes N으로 플레이한 시간(분)을 알려 주세요" if nxt.engine else ""))
        return
    print(bisect.result_title(nxt, session.names))
    if nxt.evidence:
        print("지금까지 가장 의심되는 것:" if nxt.kind == "unsure" else "근거:")
        for line in nxt.evidence:
            print(f"  - {line}")
    refw = manager.refw_mod()
    for line in session.result_lines(nxt, refw.id if refw else None):
        print(f"  • {line}")
    more = ""
    if nxt.kind == "unsure":
        more = f"{bisect.RESULT_MORE}: bisect more ({bisect.MORE_LAUNCHES}번 더)"
    elif nxt.can_confirm and nxt.kind in bisect.NAMED:
        more = f"{bisect.RESULT_CONFIRM}: bisect more"
    if more:
        print(more)
    back = "원래 프로필로 돌아가려면: bisect stop"
    if nxt.can_disable:
        what = "가장 의심되는 모드" if nxt.kind == "unsure" else "찾은 모드"
        back += f"  ({what}를 끄고 돌아가려면: bisect stop --disable)"
    print(back)


def _bisect_evidence(manager: ModManager, record_id: str, owners: dict):
    """The evidence a search starts from (evidence.build: the crash record, older launches, materials ...), with
    REFramework's records copied first (as the GUI does at startup)."""
    from . import evidence

    try:
        manager.collect_crash_evidence()
    except Exception as exc:  # then the records kept so far
        logging.getLogger(__name__).warning("게임 기록을 모으지 못했습니다: %s", exc)
    inputs = evidence.gather(manager, record_id, owners)
    units = [m.id for m in manager.mods if m.enabled]
    return evidence.build(inputs, evidence.equipment_choices(manager, units))


def cmd_bisect(manager: ModManager, args) -> int:
    """원인 모드 찾기: start a search, answer for a step (the next one is applied), see where it is, go on
    (계속 찾기 / 한 번 더 확인), stop."""
    from . import accessed, crashes, gamewatch
    from .manager import is_game_running

    session = bisect.load(manager.data_dir)
    names = session.names if session is not None else {m.id: m.name for m in manager.mods}
    if args.action == "status":
        if session is not None:
            for line in session.report_lines():
                print(line)
            print()
            nxt = session.next()
            if isinstance(nxt, bisect.Step) and nxt.engine:
                _print_ranking(nxt, names)
            _print_bisect(manager, session, nxt)
            return 0
        print("진행 중인 원인 찾기가 없습니다. 'bisect start'로 시작하면:")
        owners = bisect.conflict_owners(manager)
        _print_plan(bisect.prepare(manager, evidence=_bisect_evidence(manager, "", owners), owners=owners))
        return 0
    if args.action == "start":
        if session is not None:
            print("이미 원인 찾기가 진행 중입니다 ('bisect'로 확인, 'bisect stop'으로 그만두기).", file=sys.stderr)
            return 1
        if not manager.game_dir:
            print("게임 폴더를 먼저 설정하세요: game <경로>", file=sys.stderr)
            return 1
        if is_game_running():
            print("게임이 실행 중입니다. 게임을 끈 뒤 다시 시작하세요.", file=sys.stderr)
            return 1
        record = crashes.find_session(manager.crash_dir, args.record) if args.record else None
        if args.record and record is None:
            raise SystemExit(f"기록을 찾을 수 없습니다: {args.record} (crash list로 번호를 확인하세요)")
        if args.minutes is not None and args.minutes <= 0:
            raise SystemExit("--minutes는 0보다 커야 합니다.")
        if manager.outdated_paks():  # moving them later would read as a game update: first
            print("예전 방식(패치 번호)으로 넣은 PAK을 먼저 pak_mods 폴더로 옮깁니다.")
            report = manager.deploy()
            _print_report(report)
            if report.errors:
                return 1
        offer = None
        if args.log_files:  # (checked before anything changes)
            offer = accessed.manager_offer(manager, running=False)
            if not offer.available:
                print(f"오류: {offer.text}", file=sys.stderr)
                return 1
        owners = bisect.conflict_owners(manager)
        ev = _bisect_evidence(manager, record.id if record else "", owners)
        plan = bisect.prepare(manager, evidence=ev, owners=owners)
        if plan.problem:
            print(f"오류: {plan.problem}", file=sys.stderr)
            return 1
        when = args.when or plan.when
        minutes = args.minutes if args.minutes is not None else (plan.minutes if when == "time" else None)
        if when == "time" and minutes is None:
            minutes = 10.0
        restore = None
        if offer is not None and not offer.already:
            try:
                restore = accessed.enable_logging(manager.set_refw_option, offer)
            except (DeployError, ValueError, OSError) as exc:
                print(f"오류: 게임이 읽는 파일 기록을 켜지 못했습니다: {exc}", file=sys.stderr)
                return 1
            print("게임이 읽는 파일 기록을 켰습니다 (원인 찾기를 마치면 원래대로 되돌립니다).")
        elif offer is not None:
            print(f"게임이 읽는 파일 기록: {offer.text}")
        try:
            session, nxt = bisect.start(manager, plan, record.id if record else str(ev.record or ""),
                                        when=when, minutes=minutes, refw_restore=restore)
        except bisect.BisectError as exc:
            if restore:
                accessed.restore_logging(manager.set_refw_option, restore)
            print(f"오류: {exc}", file=sys.stderr)
            return 1
        _print_plan(plan, when, minutes, started=True)
        print()
        code = _maybe_deploy(manager, args)
        _print_bisect(manager, session, nxt)
        return code
    if session is None:
        print("진행 중인 원인 찾기가 없습니다. 'bisect start'로 시작하세요.", file=sys.stderr)
        return 1
    nxt = session.next()
    if args.action == "stop":
        disable = []
        if args.disable:
            if not isinstance(nxt, bisect.Result) or not nxt.can_disable:
                why = ("아직 하나로 좁히지 못해 끌 모드가 없습니다 (가장 의심되는 모드가 50% 이상일 때만 끕니다)."
                       if isinstance(nxt, bisect.Result) and nxt.kind == "unsure" else "아직 끌 모드를 찾지 못했습니다.")
                print(f"{why} 'bisect stop'으로 그냥 돌아갈 수 있습니다.", file=sys.stderr)
                return 1
            disable = nxt.mods
        note = session.interim_note() if isinstance(nxt, bisect.Step) else ""
        restore = session.refw_restore
        bisect.finish(manager, session, disable)
        off = f" ({', '.join(session.name(i) for i in disable)} 끔)" if disable else ""
        print(f"원래 프로필로 돌아왔습니다: {session.original_profile}{off}")
        if note:
            print(note.strip())
        restored = accessed.finish_restore(manager, restore)
        if restored:
            print(restored)
        return _maybe_deploy(manager, args)
    if args.action == "more":
        if not isinstance(nxt, bisect.Result) or session.legacy or not (
                nxt.kind == "unsure" or (nxt.can_confirm and nxt.kind in bisect.NAMED)):
            print("지금은 더 찾을 것이 없습니다. 'bisect more'는 결과가 '아직 하나로 좁히지 못했습니다'일 때(계속 찾기)나 "
                  "한 번 더 확인할 수 있는 결과일 때 씁니다.", file=sys.stderr)
            return 1
        reason = bisect.problem(manager, session)
        if reason:
            _print_bisect(manager, session, session.abort(reason))
            return 1
        print(f"{bisect.RESULT_MORE if nxt.kind == 'unsure' else bisect.RESULT_CONFIRM}: 원인 찾기를 이어 갑니다.")
        nxt = session.more()
        if isinstance(nxt, bisect.Step):
            nxt = bisect.apply_step(manager, session)
        code = _maybe_deploy(manager, args) if isinstance(nxt, bisect.Step) else 0
        _print_bisect(manager, session, nxt)
        return code
    if isinstance(nxt, bisect.Result):  # nothing more to answer
        _print_bisect(manager, session, nxt)
        return 0
    reason = bisect.problem(manager, session)
    if reason:
        _print_bisect(manager, session, session.abort(reason))
        return 1
    detected, run = "", None
    if args.action != "retry":  # the crashed game's records are copied before the next launch overwrites them
        since = (session.current or {}).get("launched") or (session.current or {}).get("applied")
        detection = gamewatch.detect(manager.crash_dir, since if isinstance(since, (int, float)) else None,
                                     game_dir=manager.game_dir, context=manager.crash_context())
        detected = detection.label
        if detected:
            print(f"게임 기록: {detected}")
        if not session.legacy:
            run = accessed.step_run(detection, nxt.on, manager.crash_index(), session.content,
                                    accessed.PakNames(manager.data_dir / "cache"))
            if run.get("seconds") is None and args.minutes is not None and args.minutes >= 0:
                run["seconds"] = 60.0 * args.minutes  # (the game was not watched: the play time the user gave)
            if args.action == "fine":  # (the answer is taken at once: what to do next time, not now)
                for note in (bisect.run_note(nxt, run.get("seconds"), bisect.CLI_RETRY),
                             bisect.exposure_note(nxt, run["unexposed"], names, bisect.CLI_RETRY)):
                    if note:
                        print(note)
    nxt = bisect.answer(manager, session, {"crashed": True, "fine": False}.get(args.action), detected, run)
    change = session.last_change()
    if change:
        print(change)
    code = _maybe_deploy(manager, args) if isinstance(nxt, bisect.Step) else 0
    _print_bisect(manager, session, nxt)
    return code


def cmd_check(manager: ModManager, args) -> int:
    """실행 전 점검: every check (low ones too), with REFramework's records copied first and the slow part
    (the last game's records, the loose files no mod placed) done again; ``--fix`` changes the REFramework
    settings it names. Exits with 1 while there are high or medium issues."""
    from . import precheck

    shown = [0.0]

    def progress(done, total, msg):
        if time.time() - shown[0] > 1.0:  # the first material check reads the game's file list
            shown[0] = time.time()
            print(msg, file=sys.stderr)

    if manager.game_dir:
        manager.collect_crash_evidence()
    facts = precheck.gather(manager)
    background = None
    if facts.game_dir and Path(facts.game_dir).is_dir():
        background = manager.precheck_background(facts, materials=not args.quick, progress=progress)
    report = precheck.check(facts, background)
    fixes = [issue for issue in report.issues if issue.fix]
    if args.fix and fixes:
        for issue in fixes:
            key, value = issue.fix
            try:
                changed = manager.set_refw_option(key, value, path=issue.config or None)
            except (DeployError, ValueError) as exc:
                print(f"오류: {exc}", file=sys.stderr)
                return 1
            where = f" ({crashlog.mask_user(issue.config)})" if issue.config else ""
            print(f"REFramework 설정을 바꿨습니다: {key}={value}{where}" if changed
                  else f"이미 그렇게 설정되어 있습니다: {key}={value}{where}")
        print()
        report = precheck.check(facts, background)
    for line in report.lines():
        print(line)
    if background is not None and background.matfix:
        print(f"\n참고: {background.matfix}")
    if not args.fix and any(issue.fix for issue in report.issues):
        print("\nREFramework 설정을 고치려면: check --fix (게임이 꺼져 있을 때만)")
    return 1 if report.important else 0


def cmd_game(manager: ModManager, args) -> int:
    path = Path(args.path) if args.path else manager.detect_game_dir()
    if path is None:
        print("게임 폴더를 찾지 못했습니다. 경로를 직접 지정하세요: game <경로>")
        return 1
    if not steam.is_game_dir(path) and not args.force:
        print(f"{path} 에 게임 실행 파일이 없습니다. 그래도 사용하려면 --force 를 붙이세요.")
        return 1
    manager.set_game_dir(path)
    print(f"게임 폴더: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mhwmm", description="몬스터 헌터 와일즈 모드 매니저")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--data-dir", help="모드와 설정을 저장할 폴더")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="GUI 실행 (기본값)")
    sub.add_parser("list", help="설치된 모드 목록")

    p = sub.add_parser("install", help="압축 파일/폴더/PAK에서 모드 설치")
    p.add_argument("path")
    p.add_argument("--option", "--variant", dest="option", type=int, action="append",
                   help="옵션이 여러 개인 모드에서 바로 켤 옵션 번호 (여러 번 지정 가능, 기본값은 모두 꺼짐)")
    p.add_argument("--all", action="store_true", help="모든 옵션을 켜기")
    p.add_argument("--raw", action="store_true", help="구조 분석 없이 그대로 설치")
    p.add_argument("--replace", action="store_true", help="같은 이름의 모드가 있으면 교체")
    p.add_argument("--disabled", action="store_true", help="비활성 상태로 설치")
    p.add_argument("--no-deploy", action="store_true", help="게임 폴더에 바로 적용하지 않음")

    for name, help_text in (("enable", "모드 활성화"), ("disable", "모드 비활성화"), ("remove", "모드 삭제")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("mods", nargs="+", help="모드 id, 번호 또는 이름")
        p.add_argument("--no-deploy", action="store_true")

    p = sub.add_parser("move", help="로드 순서 변경")
    p.add_argument("mod")
    p.add_argument("position", type=int, help="1부터 시작하는 위치 (아래일수록 우선)")
    p.add_argument("--no-deploy", action="store_true")

    p = sub.add_parser("options", help="모드의 옵션 보기/선택")
    p.add_argument("mod", help="모드 id, 번호 또는 이름")
    p.add_argument("--set", type=int, nargs="*", metavar="번호", help="켤 옵션 번호들 (나머지는 꺼짐, 비우면 모두 끔)")
    p.add_argument("--no-deploy", action="store_true")

    p = sub.add_parser("refw", help="REFramework 버전 확인/설치/업데이트")
    p.add_argument("--install", action="store_true", help="최신 빌드 설치 또는 업데이트")
    p.add_argument("--force", action="store_true", help="최신이어도 다시 설치")
    p.add_argument("--no-deploy", action="store_true")

    p = sub.add_parser("nexus", help="Nexus Mods API 키 설정, 모드 연결, 업데이트 확인")
    p.add_argument("action", choices=("check", "key", "link", "unlink"),
                   help="check: 업데이트 확인 · key [키]: API 키 저장 · link <모드> <주소>: 모드 페이지 연결 · unlink <모드>")
    p.add_argument("value", nargs="*")

    p = sub.add_parser("profile", help="프로필 목록/전환/만들기/삭제")
    p.add_argument("name", nargs="?", help="프로필 이름 (생략하면 목록)")
    p.add_argument("--create", action="store_true", help="현재 상태를 복사해 새 프로필 만들기")
    p.add_argument("--empty", action="store_true", help="--create와 함께: 모든 모드가 꺼진 프로필")
    p.add_argument("--rename", metavar="새이름", help="프로필 이름 바꾸기")
    p.add_argument("--delete", action="store_true", help="프로필 삭제")
    p.add_argument("--no-deploy", action="store_true")

    p = sub.add_parser("conflicts", help="같은 파일을 바꾸는 모드들 보기")
    p.add_argument("--files", action="store_true", help="겹치는 파일을 모두 보기")

    sub.add_parser("deploy", help="활성화된 모드를 게임 폴더에 적용")
    sub.add_parser("purge", help="게임 폴더에서 모든 모드 파일 제거")
    sub.add_parser("verify", help="적용된 파일 점검")

    p = sub.add_parser("materials", aliases=["matfix"],
                       help="게임 업데이트로 맞지 않게 된 재질(.mdf2) 점검·복구·되돌리기")
    p.add_argument("action", choices=("check", "repair", "restore", "report"),
                   help="check: 점검 · repair: 복구 (원래 파일은 그대로 둠) · restore: 원래 파일로 되돌리기 · "
                        "report: 진단 파일(zip) 저장")
    p.add_argument("--out", help="report: 저장할 zip 파일")
    p.add_argument("mods", nargs="*", help="모드 id, 번호 또는 이름 (생략하면 모든 모드)")
    p.add_argument("--no-deploy", action="store_true")

    p = sub.add_parser("equipment", aliases=["equip"], help="모드가 게임 속 어떤 장비의 모습을 바꾸는지 보기")
    p.add_argument("mods", nargs="*", help="모드 id, 번호 또는 이름 (생략하면 장비를 바꾸는 모든 모드)")
    p.add_argument("--icons", metavar="파일.png", help="게임의 아이콘 그림을 확인용으로 저장")

    p = sub.add_parser("equip-preview", help="모드를 적용한 장비 부위의 간단한 3D 미리보기 그림 저장")
    p.add_argument("mod", nargs="?", help="모드 id, 번호 또는 이름 ('-': 게임 그대로)")
    p.add_argument("target", nargs="?", help="장비 부위 (예: weapon/09/000021/main, armor/f/004/001/helm)")
    p.add_argument("out", nargs="?", help="저장할 PNG 파일")
    p.add_argument("--before", action="store_true", help="같은 구도로 그린 원래 모습도 <이름>-before.png로 저장")
    p.add_argument("--size", type=int, default=512, help="그림 크기 (픽셀, 32~1024, 기본 512)")
    p.add_argument("--self-test", type=int, nargs="?", const=SELFTEST_ITEMS, metavar="N",
                   help="게임의 장비 N개로 미리보기를 점검해 결과를 글로 출력 (그림은 저장하지 않음)")

    p = sub.add_parser("retarget", help="다른 장비로 옮기기: 모드가 바꾸는 방어구·무기 대신 다른 장비를 바꾸는 새 모드 "
                                        "만들기 (원래 모드는 그대로 두고 끔)")
    p.add_argument("mod", help="모드 id, 번호 또는 이름")
    p.add_argument("--to", required=True, action="append", metavar="[부위=]장비",
                   help="옮길 장비 (예: armor/f/087/000, weapon/03/000029 · 방어구는 같은 체형, 무기는 같은 종류). "
                        "방어구는 부위마다 다른 장비로도: --to helm=armor/f/045/002 (또는 머리=...)를 부위마다 "
                        "적고, 부위 없는 --to는 나머지 모든 부위 (예: --to armor/f/087/000 --to 머리=armor/f/045/002)")
    p.add_argument("--from", dest="source", help="원래 장비 (모드가 여러 장비를 바꿀 때, 예: armor/f/034/001)")
    p.add_argument("--parts", help="방어구에서 옮길 부위 (쉼표로, 예: helm,body 또는 머리,몸통 · 생략하면 모든 부위, "
                                  "부위 없는 --to가 없으면 --to로 고른 부위)")
    p.add_argument("--keep-original", action="store_true", help="원래 모드를 끄지 않음")
    p.add_argument("--dry-run", action="store_true", help="무엇을 옮기는지만 보여 주고 새 모드는 만들지 않음")
    p.add_argument("--no-deploy", action="store_true", help="게임 폴더에 바로 적용하지 않음")

    p = sub.add_parser("edit", aliases=["modedit"],
                       help="모드 수정: 파일 빼기 · 쓸 옵션 고르기 · 데이터 파일과 스크립트 설정의 값 바꾸기 · 되돌리기 "
                            "(모드의 원래 파일은 그대로 두고 수정본을 따로 만듦)")
    p.add_argument("mod", help="모드 id, 번호 또는 이름")
    p.add_argument("action", nargs="?", default="status", choices=EDIT_ACTIONS, metavar="동작",
                   help=" · ".join(f"{a}: {EDIT_HELP[a]}" for a in EDIT_ACTIONS) + " (기본값 status)")
    p.add_argument("rest", nargs=argparse.REMAINDER, action=_EditArgs, metavar="...",
                   help="동작의 인자와 옵션 (동작마다: edit <모드> <동작> --help)")

    p = sub.add_parser("crash", help="게임 튕김 기록: REFramework 기록 모으기 · 의심되는 원인 · 진단 파일")
    p.add_argument("action", choices=("list", "show", "report", "lua"),
                   help="list: 기록 목록 · show [번호]: 의심되는 원인 · report [번호] --out 파일.zip: 진단 파일 저장 · "
                        "lua [on|off]: Lua 스크립트 오류 기록 (REFramework 설정, 게임이 꺼져 있을 때만)")
    p.add_argument("ref", nargs="?", help="기록 번호(1 = 가장 최근) 또는 폴더 이름 · lua: on/off")
    p.add_argument("--out", help="report: 저장할 zip 파일")
    p.add_argument("--dump", action="store_true", help="report: 크래시 덤프 포함 (메모리 일부가 들어 있어 사용자 "
                                                         "이름을 가리지 못함)")
    p.add_argument("--text", metavar="파일", help="show: 게임 오류 창이나 이벤트 뷰어에서 복사한 문구 파일도 분석 "
                                                "('-'는 표준 입력)")

    p = sub.add_parser("bisect", help="원인 모드 찾기: 기록으로 의심되는 정도를 매긴 뒤 모드를 켜고 끄며 실행해 보고 게임을 "
                                      "튕기게 하는 모드 찾기")
    p.add_argument("action", nargs="?", default="status",
                   choices=("status", "start", "crashed", "fine", "retry", "more", "stop"),
                   help="status: 진행 상황 · 의심 순위(%%)와 다음 구성 (기본값) · start: 시작 (임시 프로필에서) · "
                        "crashed: 튕겼어요 · fine: 괜찮았어요 · retry: 잘 모르겠어요 (같은 구성으로 다시) · "
                        "more: 아직 좁히지 못함이면 6번 더(계속 찾기), 결과가 나왔으면 한 번 더 확인 · stop: 원래 프로필로")
    p.add_argument("--record", metavar="번호", help="start: 원인을 찾을 게임 튕김 기록 (crash list의 번호)")
    p.add_argument("--when", choices=("start", "same", "time", "rare"),
                   help="start: 언제 튕기나요 · start: 켜거나 불러오는 중 · same: 같은 장소·행동에서 매번 · "
                        "time: 한동안 하다 보면 (--minutes) · rare: 가끔")
    p.add_argument("--minutes", type=float, metavar="N",
                   help="start --when time: 몇 분쯤 하면 튕기는지 · crashed/fine: 이번에 플레이한 분 "
                        "(게임을 지켜보지 못했을 때)")
    p.add_argument("--log-files", action="store_true",
                   help="start: 게임이 읽는 파일을 기록해 더 정확하게 찾기 (REFramework 설정, 마치면 되돌림)")
    p.add_argument("--disable", action="store_true",
                   help="stop: 찾은 모드를 원래 프로필에서 끄기 (아직 좁히지 못함이면 50%% 이상일 때만)")

    p = sub.add_parser("check", help="실행 전 점검: 게임이 튕기거나 모드가 작동하지 않는 흔한 원인 찾기 "
                                     "(높음·보통 항목이 있으면 종료 코드 1)")
    p.add_argument("--quick", action="store_true", help="매니저가 넣지 않은 루스 파일 중 새 파일의 재질은 점검하지 않음")
    p.add_argument("--fix", action="store_true", help="찾은 REFramework 설정 문제(루스 파일 읽기 · pak_mods 읽기 · 읽지 "
                                                     "못한 파일 기록)를 켬 (게임이 꺼져 있을 때만)")

    p = sub.add_parser("game", help="게임 폴더 설정 (경로 생략 시 자동 감지)")
    p.add_argument("path", nargs="?")
    p.add_argument("--force", action="store_true")
    return parser


READ_ONLY_COMMANDS = ("list", "verify", "conflicts", "equipment", "equip", "equip-preview")
COMMANDS = {
    "list": cmd_list, "install": cmd_install, "options": cmd_options, "profile": cmd_profile, "refw": cmd_refw,
    "nexus": cmd_nexus, "conflicts": cmd_conflicts, "enable": cmd_toggle, "disable": cmd_toggle,
    "remove": cmd_remove, "move": cmd_move, "deploy": cmd_deploy, "purge": cmd_purge,
    "verify": cmd_verify, "game": cmd_game, "materials": cmd_materials, "matfix": cmd_materials,
    "equipment": cmd_equipment, "equip": cmd_equipment, "equip-preview": cmd_equip_preview, "crash": cmd_crash,
    "bisect": cmd_bisect, "check": cmd_check, "retarget": cmd_retarget, "edit": cmd_edit, "modedit": cmd_edit,
}


def changes_mods(args) -> bool:
    """The command turns mods on or off, installs, removes or reorders them, changes what a mod puts into the
    game (모드 수정), or changes the profile or the game folder: it waits while 원인 모드 찾기 runs
    (bisect.BLOCKED)."""
    if args.cmd in ("install", "enable", "disable", "remove", "move", "purge", "game"):
        return True
    return (args.cmd == "options" and args.set is not None) or (args.cmd == "profile" and bool(args.name)) \
        or (args.cmd == "refw" and args.install) or (args.cmd in ("materials", "matfix")
                                                       and args.action in ("repair", "restore")) \
        or (args.cmd == "retarget" and not args.dry_run) \
        or (args.cmd in ("edit", "modedit") and args.action in EDIT_WRITES and not getattr(args, "dry_run", False))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["--finish-update"] and len(argv) == 3:
        # started by the in-app updater from the freshly downloaded exe
        from .updater import finish_update

        return finish_update(argv[1], int(argv[2]))
    args = build_parser().parse_args(argv)
    if args.cmd in (None, "gui"):
        from .gui import main as gui_main

        return gui_main(args.data_dir)

    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    try:
        data_dir, problem = prepare_data_dir(args.data_dir)
        if problem:
            print(f"경고: {problem}", file=sys.stderr)
        lock = acquire_instance_lock(data_dir)
        if lock is None and args.cmd not in READ_ONLY_COMMANDS:
            print("모드 매니저가 이미 실행 중입니다. 종료한 뒤 다시 시도하세요.", file=sys.stderr)
            return 2
        manager = ModManager(data_dir)
    except OSError as exc:
        print(f"데이터 폴더를 열 수 없습니다: {exc}", file=sys.stderr)
        return 2
    setup_logging(manager.data_dir)
    logging.getLogger().addHandler(_stderr_handler())
    if lock is not None:  # (a REFramework setting 원인 모드 찾기 left to put back once the game is closed)
        _apply_pending_restore(manager)
    try:
        if changes_mods(args) and bisect.active(manager.data_dir):
            print(f"오류: {bisect.BLOCKED} (bisect로 확인, bisect stop으로 그만두기)", file=sys.stderr)
            return 1
        return COMMANDS[args.cmd](manager, args)
    except (DeployError, InstallError, MatfixError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # archive errors and other failures
        logging.getLogger(__name__).exception("명령 실패")
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            lock.close()


def _apply_pending_restore(manager: ModManager) -> None:
    """accessed.apply_pending for the command line (the window does it at startup and after each game exit)."""
    from . import accessed

    try:
        message = accessed.apply_pending(manager, searching=bisect.active(manager.data_dir))
    except Exception as exc:  # only a help: the next start tries again
        logging.getLogger(__name__).warning("REFramework 설정을 되돌리지 못했습니다: %s", exc)
        return
    if message:
        print(message)


def _stderr_handler() -> logging.Handler:
    h = logging.StreamHandler(sys.stderr)
    h.setLevel(logging.WARNING)
    h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    return h
