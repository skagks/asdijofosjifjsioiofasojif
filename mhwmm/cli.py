"""Command line interface. Without a command the GUI starts."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import __version__, bisect, crashlog, fsutil, steam
from .analyzer import classify, package_name
from .deploy import DeployError, DeployReport
from .equip.selftest import DEFAULT_ITEMS as SELFTEST_ITEMS
from .manager import InstallError, ModManager, acquire_instance_lock, prepare_data_dir, setup_logging
from .matfix import MatfixError

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
        kind = " (Lua 라이브러리)" if req["kind"] == "lua" else ""
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
        for mod in mods:
            changed = manager.restore_materials(mod.id)
            print(f"{'원래 재질 파일로 되돌림' if changed else '복구한 재질이 없음'}: {mod.name}")
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
            if r.unresolved:
                print(f"     {r.rel}: 같은 셰이더를 찾지 못해 그대로 둔 재질 - {', '.join(r.unresolved)}")
    if not repair and any(manager.material_state(j.mod_id) == "stale" for j in jobs):
        print("\n복구하려면: materials repair [모드]")
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


def _print_plan(plan) -> None:
    print(plan.problem or plan.intro())
    for line in plan.lines():
        print(f"  • {line}")


def _print_bisect(manager: ModManager, session, nxt) -> None:
    """Where a search is: the step to play, or its result and the way back."""
    if isinstance(nxt, bisect.Step):
        print(session.banner(nxt, applied=not manager.dirty))
        print("게임을 실행해 본 뒤 알려 주세요: bisect crashed (튕겼어요) · bisect fine (괜찮았어요) · "
              "bisect retry (잘 모르겠어요, 이 단계 다시)")
        return
    print(bisect.result_title(nxt, session.names))
    refw = manager.refw_mod()
    for line in session.result_lines(nxt, refw.id if refw else None):
        print(f"  • {line}")
    back = "원래 프로필로 돌아가려면: bisect stop"
    if nxt.can_disable:
        back += "  (찾은 모드를 끄고 돌아가려면: bisect stop --disable)"
    print(back)


def cmd_bisect(manager: ModManager, args) -> int:
    """원인 모드 찾기: start a search, answer for a step (the next one is applied), see where it is, stop."""
    from . import crashes, gamewatch
    from .manager import is_game_running

    session = bisect.load(manager.data_dir)
    if args.action == "status":
        if session is not None:
            for line in session.report_lines():
                print(line)
            print()
            _print_bisect(manager, session, session.next())
            return 0
        print("진행 중인 원인 찾기가 없습니다. 'bisect start'로 시작하면:")
        _print_plan(bisect.prepare(manager))
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
        if manager.outdated_paks():  # moving them later would read as a game update: first
            print("예전 방식(패치 번호)으로 넣은 PAK을 먼저 pak_mods 폴더로 옮깁니다.")
            report = manager.deploy()
            _print_report(report)
            if report.errors:
                return 1
        plan = bisect.prepare(manager)
        if plan.problem:
            print(f"오류: {plan.problem}", file=sys.stderr)
            return 1
        _print_plan(plan)
        print()
        try:
            session, nxt = bisect.start(manager, plan, record.id if record else "")
        except bisect.BisectError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 1
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
                print("아직 끌 모드를 찾지 못했습니다. 'bisect stop'으로 그냥 돌아갈 수 있습니다.", file=sys.stderr)
                return 1
            disable = nxt.mods
        bisect.finish(manager, session, disable)
        off = f" ({', '.join(session.name(i) for i in disable)} 끔)" if disable else ""
        print(f"원래 프로필로 돌아왔습니다: {session.original_profile}{off}")
        return _maybe_deploy(manager, args)
    if isinstance(nxt, bisect.Result):  # nothing more to answer
        _print_bisect(manager, session, nxt)
        return 0
    reason = bisect.problem(manager, session)
    if reason:
        _print_bisect(manager, session, session.abort(reason))
        return 1
    detected = ""
    if args.action != "retry":  # the crashed game's records are copied before the next launch overwrites them
        since = (session.current or {}).get("applied")
        detection = gamewatch.detect(manager.crash_dir, since if isinstance(since, (int, float)) else None,
                                     game_dir=manager.game_dir, context=manager.crash_context())
        detected = detection.label
        if detected:
            print(f"게임 기록: {detected}")
    nxt = bisect.answer(manager, session, {"crashed": True, "fine": False}.get(args.action), detected)
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

    p = sub.add_parser("bisect", help="원인 모드 찾기: 모드를 절반씩 끄고 실행해 보며 게임을 튕기게 하는 모드 찾기")
    p.add_argument("action", nargs="?", default="status",
                   choices=("status", "start", "crashed", "fine", "retry", "stop"),
                   help="status: 진행 상황 (기본값) · start: 시작 (임시 프로필에서) · crashed: 튕겼어요 · fine: 괜찮았어요 · "
                        "retry: 잘 모르겠어요 (이 단계 다시) · stop: 원래 프로필로")
    p.add_argument("--record", metavar="번호", help="start: 원인을 찾을 게임 튕김 기록 (crash list의 번호)")
    p.add_argument("--disable", action="store_true", help="stop: 찾은 모드를 원래 프로필에서 끄기")

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
    "bisect": cmd_bisect, "check": cmd_check,
}


def changes_mods(args) -> bool:
    """The command turns mods on or off, installs, removes or reorders them, or changes the profile or
    the game folder: it waits while 원인 모드 찾기 runs (bisect.BLOCKED)."""
    if args.cmd in ("install", "enable", "disable", "remove", "move", "purge", "game"):
        return True
    return (args.cmd == "options" and args.set is not None) or (args.cmd == "profile" and bool(args.name)) \
        or (args.cmd == "refw" and args.install) or (args.cmd in ("materials", "matfix")
                                                       and args.action in ("repair", "restore"))


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


def _stderr_handler() -> logging.Handler:
    h = logging.StreamHandler(sys.stderr)
    h.setLevel(logging.WARNING)
    h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    return h
