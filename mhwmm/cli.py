"""Command line interface. Without a command the GUI starts."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__, steam
from .analyzer import classify, package_name
from .deploy import DeployError, DeployReport
from .manager import InstallError, ModManager, acquire_instance_lock, prepare_data_dir, setup_logging

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


def cmd_list(manager: ModManager, args) -> int:
    print(f"게임 폴더: {manager.game_dir or '(설정 안 됨)'}")
    print(f"REFramework: {'설치됨' if manager.refw_installed() else '없음'}")
    if not manager.mods:
        print("설치된 모드가 없습니다.")
        return 0
    conflicts = manager.conflicts()
    for i, m in enumerate(manager.mods, 1):
        wins, losses = conflicts.counts(m.id)
        flag = "[x]" if m.enabled else "[ ]"
        extra = f"  충돌 +{wins}/-{losses}" if wins or losses else ""
        version = f" v{m.version}" if m.version else ""
        if m.options:
            extra += "  옵션 선택 필요" if m.needs_option else f"  옵션 {len(m.selected)}/{len(m.options)}"
        print(f"{i:3d}. {flag} {m.name}{version}  ({_kinds(manager.mod_kinds(m.id))}, id={m.id}){extra}")
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
        existing = manager.find_by_name(name)
        replace_id = existing.id if existing and args.replace else None
        mod = manager.install_package(session, variants, replace_id, selected)
        if args.disabled and mod.enabled:
            manager.set_enabled(mod.id, False)
        print(f"설치: {mod.name}  ({_kinds(manager.mod_kinds(mod.id))}, {len(manager.mod_files(mod.id))}개 파일)")
        if mod.options:
            _print_options(manager, mod)
    return _maybe_deploy(manager, args)


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

    sub.add_parser("deploy", help="활성화된 모드를 게임 폴더에 적용")
    sub.add_parser("purge", help="게임 폴더에서 모든 모드 파일 제거")
    sub.add_parser("verify", help="적용된 파일 점검")

    p = sub.add_parser("game", help="게임 폴더 설정 (경로 생략 시 자동 감지)")
    p.add_argument("path", nargs="?")
    p.add_argument("--force", action="store_true")
    return parser


READ_ONLY_COMMANDS = ("list", "verify")
COMMANDS = {
    "list": cmd_list, "install": cmd_install, "options": cmd_options, "enable": cmd_toggle, "disable": cmd_toggle,
    "remove": cmd_remove, "move": cmd_move, "deploy": cmd_deploy, "purge": cmd_purge,
    "verify": cmd_verify, "game": cmd_game,
}


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
        return COMMANDS[args.cmd](manager, args)
    except (DeployError, InstallError) as exc:
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
