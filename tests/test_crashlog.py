"""REFramework records and pasted error text: parsing (synthetic text written here, no real logs)."""

import pytest

from mhwmm import crashlog
from mhwmm.crashlog import (exception_label, game_relative, mask_user, module_place, parse_config, parse_faulty,
                            parse_log, read_error_text, refw_label, resource_key, set_config_value, split_version)

GAME = r"G:\Program Files (x86)\Steam\steamapps\common\MonsterHunterWilds"
STORAGE = GAME.lower() + r"\_storage_"


def rec(time: str, msg: str, level: str = "info", src: str = "") -> str:
    return f"[2026-10-16 {time}] [REFramework] [{level}] " + (f"[{src}] " if src else "") + msg


def header(time: str = "15:39:22.303", prev: bool = True, game_name: bool = True) -> list[str]:
    lines = [rec(time, rf"Previous log preserved as {GAME}\re2_framework_log.prev.txt")] if prev else []
    lines += [rec(time, "REFramework entry"), rec(time, "Commit hash: d052e8fd3fd41d0a1a33394fcf6e6c19be39b1ee"),
              rec(time, "Tag: v1.5.9.1"), rec(time, "Commits past tag: 514"), rec(time, "Build date: 11.10.2026"),
              rec(time, "Build time: 01:52")]
    if game_name:
        lines.append(rec(time, "Game name: mhwilds"))
    return lines + [rec(time, f"Current game path: {GAME}")]


def stack(*frames: tuple[str, str]) -> list[str]:
    """The call-stack record: the handler's own frames, the dispatcher, then ``frames``."""
    out = [rec("15:48:55.434", "", "error")]
    for symbol, location in [("igTreeNode_StrStr0", rf"{STORAGE}\dinput8.dll + 0x5dd998"),
                             ("UnhandledExceptionFilter", r"C:\WINDOWS\System32\KERNELBASE.dll + 0x10f4e3"),
                             ("KiUserExceptionDispatcher", r"C:\WINDOWS\SYSTEM32\ntdll.dll + 0x165dee"), *frames]:
        out += [f" {symbol}", f"  {location}", ""]
    return out + [""]


def crash_block(module: str, *frames: tuple[str, str], code: str = "c0000005", rip_record: bool = True) -> list[str]:
    lines = [rec("15:48:55.424", f"Exception occurred: {code}", "error")]
    if rip_record:
        lines.append(rec("15:48:55.424", "RIP (exception record): 7ffc38e6e0b4", "error"))
    lines += [rec("15:48:55.424", f"{r}: {v}", "error") for r, v in (("RIP", "7ffc38e6e0b4"), ("RSP", "224ffa90"),
                                                                     ("R8", "224ffab8"), ("EFLAGS", "10206"), ("SS", "2b"))]
    lines += [rec("15:48:55.426", "Call stack:", "error"),
              rec("15:48:55.427", "Adding 30099 entries for module 7ffbea0f0000", src="Scan.cpp:2205")]
    lines += stack(*frames)
    lines += [rec("15:48:55.434", f"Module: 7ffc38e60000 {module}" if module else "Module: Unknown", "error"),
              rec("15:48:55.434", rf"Attempting to write dump to {GAME}\reframework_crash.dmp", "error")]
    return lines


def crlf(lines: list[str]) -> str:
    return "\r\n".join(lines) + "\r\n"


def test_session_header_and_crash_block_in_a_crlf_log():
    lines = header() + [
        rec("15:39:23.100", "LdrRegisterDllNotification: Loaded: d3d12.dll"),
        rec("15:39:23.560", rf"LdrRegisterDllNotification: DLL loaded from game directory: {GAME}\sl.interposer.dll"),
        rec("15:41:45.145", "Present failed: 87a0001", "error"),  # the window was hidden: noise
        rec("15:41:46.000", "Failed to find function start for 0x14e68312c, cannot resolve instruction", "error",
            "Scan.cpp:3101"),
        rec("15:41:47.000", "[IntegrityCheckBypass]: Could not find second_conditional_jmp for DD2.", "error"),
    ] + crash_block(rf"{GAME}\D3D12\D3D12Core.dll",
                    ("Unknown symbol", rf"{GAME}\D3D12\D3D12Core.dll + 0xe0b4"),
                    ("Ordinal398 [TDB: app.cMotionSequenceFilterManager.addMotionSequenceFilter]",
                     rf"{GAME}\MonsterHunterWilds.exe + 0x1b989"),
                    ("app.PorterCommonSubAction.c2WayPartsLayerMotionPlay..ctor", "0x7ff612340000 + 0x88"),
                    ("Unknown symbol", "0x2c3944f30"))
    info = parse_log(crlf(lines))
    assert info.started == "2026-10-16 15:39:22.303" and info.ended == "2026-10-16 15:48:55.434"
    assert info.previous_kept and not info.appdata
    assert info.header["commit"].startswith("d052e8fd") and info.header["game_name"] == "mhwilds"
    assert info.game_path == GAME
    assert info.game_dlls == [rf"{GAME}\sl.interposer.dll"]
    crash = info.crash
    assert crash.code == "c0000005" and crash.rip == "7ffc38e6e0b4" and not crash.suspicious
    assert crash.module == rf"{GAME}\D3D12\D3D12Core.dll" and crash.module_base == "7ffc38e60000"
    assert crash.dump == rf"{GAME}\reframework_crash.dmp" and crash.place == crash.module
    # the handler's own frames (dinput8.dll, KERNELBASE, ntdll) are left out
    assert [f.describe() for f in crash.frames] == [
        "D3D12Core.dll + 0xe0b4", "MonsterHunterWilds.exe + 0x1b989 [app.cMotionSequenceFilterManager.addMotionSequenceFilter]",
        "? + 0x88 [app.PorterCommonSubAction.c2WayPartsLayerMotionPlay..ctor]", "?"]
    assert crash.frames[1].symbol == "Ordinal398" and crash.frames[3].offset is None
    # registers, call stacks and the known noise are not "other errors"
    assert info.errors == 0 and info.error_lines == []
    assert info.present_errors == []
    assert module_place(crash.place, [info.game_path]) == ("gpu", "d3d12/d3d12core.dll")


def test_older_builds_and_logs_without_a_crash():
    # before 2025-03-01 there is no "RIP (exception record)" line; stable v1.5.9.1 has no Game name
    lines = header(prev=False, game_name=False) + crash_block("", ("Unknown symbol", "0x2c3944f30"), rip_record=False)
    info = parse_log(lines)  # an iterable of lines works too
    assert not info.previous_kept and "game_name" not in info.header
    assert info.crash.rip == "7ffc38e6e0b4" and info.crash.module == "" and info.crash.place == ""
    assert module_place(info.crash.place) == ("unknown", "")
    # "Module: Unknown" is about the crashing address: the plugin that called into that code is not it
    hooked = parse_log(header() + crash_block("", ("Unknown symbol", "0x2c3944f30"),
                                              ("Unknown symbol", rf"{GAME}\reframework\plugins\Helper.dll + 0x1234")))
    assert hooked.crash.module_logged and hooked.crash.place == ""
    assert hooked.crash.frames[1].module.endswith("Helper.dll")
    # no Module: line at all (the handler died): the crashing frame's module, even when it has none
    cut = [line for line in crash_block(rf"{GAME}\MonsterHunterWilds.exe", ("Unknown symbol", "0x2c3944f30"),
                                        ("Unknown symbol", rf"{GAME}\MonsterHunterWilds.exe + 0x10"))
           if "Module: " not in line and "Attempting" not in line]
    assert parse_log(header() + cut).crash.place == ""
    cut = [line for line in crash_block("", ("Unknown symbol", rf"{GAME}\MonsterHunterWilds.exe + 0x10"))
           if "Module: " not in line]
    assert parse_log(header() + cut).crash.place == rf"{GAME}\MonsterHunterWilds.exe"
    quiet = parse_log(crlf(header() + [rec("15:40:00.000", "Hooked DirectX 12"),
                                        rec("15:40:01.000", "Present failed: -7785fffb", "error")]))
    assert quiet.crash is None and quiet.crashes == []
    assert quiet.present_errors == ["-7785fffb"]  # a removed device, not the "window hidden" noise
    assert quiet.last_lines[-1] == "[2026-10-16 15:40:00.000] Hooked DirectX 12"
    assert parse_log("").records == 0 and parse_log("garbage\nmore garbage").crash is None


def test_exceptions_refframework_caught_are_not_crashes_and_suspicious_ones_are_marked():
    vm = [rec("15:40:00.000", "VMContext: Caught exception code c0000005", "error"),
          rec("15:40:00.000", "VMContext: Attempting to handle access violation. Attempting to dump callstack...", "error"),
          rec("15:40:00.000", "RIP: 140001000", "error"), rec("15:40:00.000", "Call stack:", "error")] + stack(
        ("Unknown symbol", rf"{GAME}\MonsterHunterWilds.exe + 0x1000"))
    info = parse_log(header() + vm)
    assert info.crash is None and info.vm_exceptions == 1 and info.errors == 1  # the "Attempting to handle" line
    suspicious = [rec("15:48:55.420", "SUSPICIOUS EXCEPTION: _ReturnAddress() == nullptr", "warning")]
    info = parse_log(header() + vm + suspicious + crash_block(rf"{GAME}\MonsterHunterWilds.exe"))
    assert info.vm_exceptions == 1 and info.crash.suspicious and info.crash.module.endswith("MonsterHunterWilds.exe")
    failed = parse_log(header() + crash_block(rf"{GAME}\MonsterHunterWilds.exe")[:-1] + [
        rec("15:48:55.500", "Exception occurred, but could not load dbghelp.dll", "error")])
    assert failed.crash.dump == "" and failed.crash.dump_failed == "load dbghelp.dll"
    # REFramework says where it will write the dump before it fails to create the file
    failed = parse_log(header() + crash_block(rf"{GAME}\MonsterHunterWilds.exe") + [
        rec("15:48:55.500", "Exception occurred, but could not create dump file", "error")])
    assert failed.crash.dump_failed == "create dump file" and failed.errors == 0


def test_a_script_that_was_loading_when_the_game_died():
    autorun = rf"{GAME}\reframework\autorun"
    loading = header() + [rec("15:39:31.680", "[ScriptRunner] Initializing Lua state for the first time..."),
                          rec("15:39:31.686", rf"[ScriptState] Running script {autorun}\ok.lua..."),
                          rec("15:39:31.689", rf"[ScriptState] Running script {autorun}\foo.lua...")]
    # the first load never finished: sure, however long the log went on
    info = parse_log(loading + [rec("15:45:00.000", "Sending rehook request for D3D")])
    assert info.stuck_script == rf"{autorun}\foo.lua" and info.stuck_sure
    assert info.stuck_line.endswith(r"foo.lua...") and info.scripts[-1].endswith("foo.lua")
    # loaded fine, crashed much later
    done = loading + [rec("15:39:31.690", "[ScriptRunner] Lua state initialized.")]
    assert parse_log(done + crash_block(rf"{GAME}\MonsterHunterWilds.exe")).stuck_script == ""
    # scripts run again (reset from the REFramework menu): suspected only when the end follows closely
    reset = done + [rec("15:48:50.000", rf"[ScriptState] Running script {autorun}\bar.lua...")]
    info = parse_log(reset + crash_block(rf"{GAME}\MonsterHunterWilds.exe"))
    assert info.stuck_script.endswith("bar.lua") and not info.stuck_sure
    late = done + [rec("15:40:00.000", rf"[ScriptState] Running script {autorun}\bar.lua...")]
    assert parse_log(late + crash_block(rf"{GAME}\MonsterHunterWilds.exe")).stuck_script == ""


def test_lua_errors_plugins_and_paks():
    lines = header() + [
        rec("15:39:40.000", r"sol: runtime error: ...\MonsterHunterWilds\reframework\autorun\foo.lua:12: attempt to "
                            "index a nil value (field 'x')", "error"),
        "stack traceback:", "\t[C]: in ?",
        rec("15:39:41.000", rf"Unknown error when running script {GAME}\reframework\autorun\bar.lua", "error"),
        rec("15:39:41.500", "[ScriptRunner] Error while checking for scene: x.lua:1: odd", "error"),  # REFramework's own
        rec("15:39:25.893", rf"[PluginLoader] Loaded {GAME}\reframework\plugins\Good.dll"),
        rec("15:39:25.894", rf"[PluginLoader] Failed to load {GAME}\reframework\plugins\Broken.dll", "error"),
        rec("15:39:25.895", "[PluginLoader] Plugin Old requires a different major version", "error"),
        rec("15:39:25.896", "[PluginLoader] Plugin Newer requires a newer minor version", "error"),
        rec("15:39:25.897", "[PluginLoader] Plugin RE4Thing is for a different game RE4", "error"),
        rec("15:39:25.898", "[PluginLoader] Failed to initialize Shy", "error"),
        rec("15:39:25.899", "[PluginLoader] Failed to initialize plugins: whatever", "error"),
        rec("15:39:25.900", "[PluginLoader] Boom has an exception in reframework_plugin_initialize, skipping...", "error"),
        rec("15:39:25.201", rf"[IntegrityCheckBypass]: Cached custom pak with name: 001_A.pak at path: {GAME}\pak_mods\001_A.pak"),
        rec("15:39:25.202", "[IntegrityCheckBypass]: Finished caching custom pak paths. Total count: 2"),
        rec("15:39:26.000", rf"[IntegrityCheckBypass]: injecting {GAME}\pak_mods\001_A.pak as native-form "
                            "'re_chunk_000.pak.sub_000.pak.patch_016.pak' (a6=16, slot=31, auto-assigned)"),
        rec("15:39:26.001", rf"[IntegrityCheckBypass]: pak_load_check_function_hook returned false for string: "
                            rf"{GAME}\pak_mods\002_B.pak", "warning"),
        rec("15:39:26.002", "[IntegrityCheckBypass]: pak_load_check_function_hook returned false for string: "
                            "re_chunk_000.pak.sub_001.pak.patch_002.pak", "warning"),  # every session: noise
        rec("15:39:26.003", "[IntegrityCheckBypass]: slot 32 for '003_C.pak' is already occupied by a real pak, skipping.",
            "error"),
        rec("15:39:26.004", "[IntegrityCheckBypass]: 2 custom pak(s) were cached but NONE were injected - no archive "
                            "family matched the auto-assign suffix. Families seen this run:", "error"),
        rec("15:39:27.000", "[HookManager] Cannot add nullptr function", "error"),
        rec("15:39:28.000", "[VMContext] We're going to crash, could not locate functions we need.", "error"),
        rec("15:39:29.000", "[LooseFileLoader] Failed to find path_to_hash candidate, cannot continue", "error"),
    ]
    info = parse_log(crlf(lines))
    assert [(e.path, e.line, e.message) for e in info.lua_errors] == [
        (r"...\MonsterHunterWilds\reframework\autorun\foo.lua", 12, "attempt to index a nil value (field 'x')"),
        (rf"{GAME}\reframework\autorun\bar.lua", 0, "알 수 없는 오류")]
    assert info.plugins == [rf"{GAME}\reframework\plugins\Good.dll"]
    assert [(e.name, e.reason) for e in info.plugin_errors] == [
        (rf"{GAME}\reframework\plugins\Broken.dll", "load"), ("Old", "major"), ("Newer", "minor"), ("RE4Thing", "game"),
        ("Shy", "init"), ("Boom", "exception")]
    assert info.paks_cached == [rf"{GAME}\pak_mods\001_A.pak"] and info.pak_count == 2
    assert info.paks_injected == {"re_chunk_000.pak.sub_000.pak.patch_016.pak": rf"{GAME}\pak_mods\001_A.pak"}
    assert info.paks_failed == [rf"{GAME}\pak_mods\002_B.pak"] and info.paks_slot_taken == ["003_C.pak"]
    assert info.paks_none == 2 and not info.paks_disabled
    assert info.hook_errors == 1 and len(info.init_errors) == 1 and info.loose_failed.startswith("[LooseFileLoader]")
    old = parse_log([rec("15:39:26.000", rf"[IntegrityCheckBypass]: Redirecting load of re_chunk_000.pak.patch_005.pak "
                                         rf"to custom pak at path: {GAME}\pak_mods\x.pak"),
                     rec("15:39:26.000", "[IntegrityCheckBypass]: Pak directory loading is disabled, skipping it.")])
    assert old.paks_injected == {"re_chunk_000.pak.patch_005.pak": rf"{GAME}\pak_mods\x.pak"} and old.paks_disabled


@pytest.mark.parametrize("path, expected", [
    (rf"{STORAGE}\dinput8.dll", ("reframework", "dinput8.dll")),
    (rf"{GAME}\reframework\plugins\ArtianQoL.dll", ("plugin", "reframework/plugins/artianqol.dll")),
    (rf"{GAME}\MonsterHunterWilds.exe", ("game", "monsterhunterwilds.exe")),
    (rf"{STORAGE}\crashhandler.dll", ("game", "crashhandler.dll")),
    (rf"{STORAGE}\nvngx_dlssg.dll", ("gpu", "nvngx_dlssg.dll")),
    (rf"{GAME}\D3D12\D3D12Core.dll", ("gpu", "d3d12/d3d12core.dll")),
    (rf"{STORAGE}\dxgi.dll", ("root_dll", "dxgi.dll")),  # a proxy DLL (ReShade, OptiScaler...)
    (rf"{GAME}\bin\helper.dll", ("root_dll", "bin/helper.dll")),
    (r"C:\WINDOWS\SYSTEM32\d3d12.dll", ("gpu", "")),
    (r"C:\Windows\System32\DriverStore\FileRepository\nv_dispi.inf_amd64\nvwgf2umx.dll", ("gpu", "")),
    (r"C:\WINDOWS\SYSTEM32\ntdll.dll", ("system", "")),
    (r"C:\Program Files\Norton\aswhook.dll", ("other", "")),
    ("Unknown", ("unknown", "")),
    ("", ("unknown", "")),
    # runtimes a game update may add next to the exe are the graphics side, not files someone put there
    (rf"{STORAGE}\libxess_fg.dll", ("gpu", "libxess_fg.dll")),
    (rf"{STORAGE}\sl.deepdvc.dll", ("gpu", "sl.deepdvc.dll")),
    (rf"{STORAGE}\amd_fidelityfx_dx12.dll", ("gpu", "amd_fidelityfx_dx12.dll")),
    (rf"{STORAGE}\libxell.dll", ("gpu", "libxell.dll")),
    (rf"{STORAGE}\nvapi64.dll", ("root_dll", "nvapi64.dll")),  # fakenvapi
    (rf"{STORAGE}\nvngx.dll", ("root_dll", "nvngx.dll")),  # OptiScaler
    (rf"{STORAGE}\something_new.dll", ("root_dll", "something_new.dll")),
    # a file name alone (an error window)
    ("KERNELBASE.dll", ("system", "")),
    ("ntdll.dll", ("system", "")),
    ("dinput8.dll", ("reframework", "dinput8.dll")),
    ("MonsterHunterWilds.exe", ("game", "monsterhunterwilds.exe")),
    ("CrashHandler.dll", ("game", "crashhandler.dll")),
    ("nvwgf2umx.dll", ("gpu", "")),
    ("aswhook.dll", ("other", "")),
    ("StackHash_1b2c", ("unknown", "")),
    ("Helper.dll", ("named", "")),
])
def test_where_a_module_is(path, expected):
    assert module_place(path, [GAME]) == expected


def test_game_relative_paths():
    appdata = r"C:\Users\Kim\AppData\Roaming\REFramework\MonsterHunterWilds"
    assert game_relative(rf"{appdata}\reframework\autorun\x.lua") == "reframework/autorun/x.lua"
    assert game_relative(rf"{STORAGE}\dinput8.dll") == "dinput8.dll"  # even without knowing the game folder
    assert game_relative(rf"\\?\{GAME}\pak_mods\001_A.pak", [GAME]) == "pak_mods/001_a.pak"
    assert game_relative(r"D:\Elsewhere\x.dll", [GAME]) is None
    assert crashlog.module_name(r"C:\a\b\X.dll") == "X.dll"


def test_faulty_files_list():
    text = ('\ufeff"error" 2 "Invalid file" "GameDesign/Equip/_Prefab/Armor/Male/003/001/003_001_avp.user"\r\n'
            '"warning" 1 "Missing file" "Art/Model/Character/ch03/001/x.mdf2"\r\n'
            '"info" 0 "Unknown reason" "a/b.tex"\r\n'
            '"error" 2 "Invalid file" "GameDesign/Equip/_Prefab/Armor/Male/003/001/003_001_avp.user"\r\n'  # again
            "[Invalid file] natives/GameDesign/Common/CommonParam/HumanWeaponRangeParam.user.2\r\n"  # a pasted toast
            "some unrelated text\r\n\r\n")
    found = parse_faulty(text)
    assert [(f.level, f.reason, f.label) for f in found] == [
        ("error", 2, "Invalid file"), ("warning", 1, "Missing file"), ("info", 0, "Unknown reason"), ("", 2, "Invalid file")]
    assert found[0].name == "003_001_avp.user" and found[1].missing
    assert found[3].key == "gamedesign/common/commonparam/humanweaponrangeparam.user"
    # one key for every way a path can be written
    keys = {resource_key(p) for p in ("natives/STM/Art/Model/X.tex.241106027", "Art/Model/x.tex",
                                      "natives/stm/streaming/Art/Model/X.tex.241106027", "@Art\\Model\\X.TEX")}
    assert keys == {"art/model/x.tex"}
    assert resource_key("Art/hat.001.mdf2") == "art/hat.001.mdf2"  # a number inside the name is not a version
    assert split_version("a/x.user.3") == ("a/x.user", ".3") and split_version("a/x.user") == ("a/x.user", "")
    assert resource_key("Sound/x.sbnk.1.x64.ja") == "sound/x.sbnk"


def test_config_edits_keep_everything_else(tmp_path):
    path = tmp_path / "re2_fw_config.txt"
    original = b"FontSize=16\r\nScriptRunner_LogToDisk=false\r\nMenuKey_Key=46\r\nWeird line\r\n"
    path.write_bytes(original)
    assert parse_config(path.read_text())["ScriptRunner_LogToDisk"] == "false"
    assert set_config_value(path, "ScriptRunner_LogToDisk", True)
    assert path.read_bytes() == original.replace(b"LogToDisk=false", b"LogToDisk=true")
    assert not set_config_value(path, "ScriptRunner_LogToDisk", True)  # nothing to change
    assert set_config_value(path, "LooseFileLoader_Enabled", True)
    assert path.read_bytes().endswith(b"Weird line\r\nLooseFileLoader_Enabled=true\r\n")
    assert not list(tmp_path.glob("*.mhwmm-part"))
    lf = tmp_path / "lf.txt"
    lf.write_bytes(b"A=1\nB=2")  # LF and no newline at the end
    set_config_value(lf, "C", "3")
    assert lf.read_bytes() == b"A=1\nB=2\nC=3\n"
    new = tmp_path / "new.txt"
    assert set_config_value(new, "ScriptRunner_LogToDisk", True) and new.read_bytes() == b"ScriptRunner_LogToDisk=true\r\n"
    with pytest.raises(ValueError):
        set_config_value(new, "bad=key", True)
    with pytest.raises(ValueError):
        set_config_value(new, "Key", "two\nlines")
    assert crashlog.config_bool("true") is True and crashlog.config_bool("False") is False
    assert crashlog.config_bool(None) is None and crashlog.config_bool("1") is None


def test_user_names_are_masked():
    text = (r"C:\Users\김철수\AppData\Roaming\x  c:/users/John Doe/Desktop  \\?\D:\USERS\bob\Games"
            "  C:\\\\Users\\\\esc\\\\x  C:\\Users\\Public\\Documents")
    masked = mask_user(text)
    assert "김철수" not in masked and "John Doe" not in masked and "bob" not in masked and "esc" not in masked
    assert r"C:\Users\<사용자>\AppData" in masked and "c:/users/<사용자>/Desktop" in masked
    assert r"C:\Users\Public" in masked
    assert mask_user("/home/someone/x") == "/home/<사용자>/x"
    # Wine / Proton paths
    assert mask_user(r"Z:\home\alice\.local\x") == r"Z:\home\<사용자>\.local\x"
    assert mask_user("Z:/home/dan/x") == "Z:/home/<사용자>/x"
    assert mask_user(r"D:\Games\Home\MonsterHunterWilds") == r"D:\Games\Home\MonsterHunterWilds"
    assert mask_user(r"경로C:\Users\john\x") == r"경로C:\Users\<사용자>\x"  # right after other text


def test_exception_codes_and_build_labels():
    assert exception_label("c0000005") == "0xC0000005 (메모리 접근 오류)"
    assert exception_label("0xE06D7363") == "0xE06D7363 (C++ 예외)"
    assert exception_label("80000003") == "0x80000003 (중단점)" and exception_label("1234") == "0x00001234"
    assert exception_label("nope") == "" and crashlog.normalize_code(0xC0000409) == "c0000409"
    head = {"commit": "d052e8fd3fd41d0a1a33394fcf6e6c19be39b1ee", "tag": "v1.5.9.1", "commits_past": "514",
            "build_date": "11.10.2026"}
    assert refw_label(head) == "v1.5.9.1+514 (2026-10-11 빌드)"

    class Build:
        commit, label = "d052e8f", "nightly 01432"

    assert refw_label(head, [Build()]) == "nightly 01432"
    assert refw_label({"commit": "abcdef1234"}) == "abcdef1"


def test_pasted_error_windows():
    found = read_error_text(
        "RE ENGINE - Aborted\r\nfile looks corrupted: re_chunk_000.pak.sub_000.pak.patch_011.pak\r\n"
        "Fatal D3D error (24, DXGI_ERROR_DEVICE_REMOVED, 0x887a0005) DeviceRemovedReason(0x887A0006)\r\n"
        "unhandled exception 0x140000000 + 0xa4d69d0\r\n"
        "[Invalid file] natives/STM/GameDesign/x.user.3\r\n"
        r"sol: runtime error: ...\reframework\autorun\foo.lua:12: attempt to call a nil value")
    assert found.aborted and found.corrupted_paks == ["re_chunk_000.pak.sub_000.pak.patch_011.pak"]
    assert found.d3d == [(24, "DXGI_ERROR_DEVICE_REMOVED", "0x887a0005", "0x887a0006")]
    assert found.unhandled == ["0x140000000 + 0xa4d69d0"]
    assert [f.key for f in found.faulty] == ["gamedesign/x.user"]
    assert found.scripts == [(r"...\reframework\autorun\foo.lua", 12)]  # cut by Lua: "..." stays
    event = read_error_text("오류 있는 응용 프로그램 이름: MonsterHunterWilds.exe, 버전: 1.42.0.2\n"
                            "오류 있는 모듈 이름: ArtianQoL.dll, 버전: 0.0.0.0\n예외 코드: 0xc0000005\n"
                            r"오류 있는 모듈 경로: G:\Games\MonsterHunterWilds\reframework\plugins\ArtianQoL.dll")
    assert event.codes == ["c0000005"]
    assert event.modules == [r"G:\Games\MonsterHunterWilds\reframework\plugins\ArtianQoL.dll"]  # the path wins
    english = read_error_text("Faulting module name: aswhook.dll, version: 1.0\nException code: 0xc0000409")
    assert english.modules == ["aswhook.dll"] and english.codes == ["c0000409"]
    assert not read_error_text("hello") and not read_error_text("")
    assert read_error_text("Monster Hunter Wilds - GPU Crash").gpu_crash
    # Reliability Monitor (no folder, no 0x) in English and Korean, and WER's event 1001
    for text in ("Source\r\nMonster Hunter Wilds\r\nSummary\r\nStopped working\r\nFaulting Application Path:\tG:\\x.exe\r\n"
                 "Problem signature\r\nProblem Event Name:\tAPPCRASH\r\nFault Module Name:\tKERNELBASE.dll\r\n"
                 "Fault Module Version:\t10.0.26100.1\r\nException Code:\te06d7363\r\nException Offset:\t00000000000c5f2a",
                 "문제 이벤트 이름:\tAPPCRASH\r\n오류 모듈 이름:\tKERNELBASE.dll\r\n오류 모듈 버전:\t10.0.26100.1\r\n"
                 "예외 코드:\te06d7363\r\n예외 오프셋:\t00000000000c5f2a",
                 "Fault bucket 123, type 4\r\nEvent Name: APPCRASH\r\nResponse: Not available\r\n\r\nProblem signature:\r\n"
                 "P1: MonsterHunterWilds.exe\r\nP2: 1.42.0.2\r\nP3: 6720a6b1\r\nP4: KERNELBASE.dll\r\nP5: 10.0.26100.1\r\n"
                 "P6: 2b3c4d5e\r\nP7: e06d7363\r\nP8: 00000000000c5f2a\r\n"):
        found = read_error_text(text)
        assert found.modules == ["KERNELBASE.dll"] and found.codes == ["e06d7363"], text
    bex = read_error_text("Event Name: BEX64\nP1: MonsterHunterWilds.exe\nP4: StackHash_8a2c\nP7: 0000000000000000\n"
                          "P8: c0000409\n")
    assert bex.modules == ["StackHash_8a2c"] and bex.codes == ["c0000409"]


def test_long_lines_are_read_in_linear_time():
    import time

    line = "x: " + "a" * 200_000  # e.g. a script logging a whole table as an error, with no ".lua:N:" in it
    start = time.perf_counter()
    info = parse_log([rec("15:39:22.303", "REFramework entry"), rec("15:39:23.000", line, "error"),
                      rec("15:39:24.000", line + r": C:\x\reframework\autorun\a.lua:3: boom", "error")])
    found = read_error_text(line + "\n" + line)
    assert time.perf_counter() - start < 1.0
    assert info.errors == 1 and [(e.path, e.line, e.message) for e in info.lua_errors] == [
        (r"C:\x\reframework\autorun\a.lua", 3, "boom")]
    assert not found


def test_large_logs_are_read_in_part(tmp_path):
    path = tmp_path / "big.txt"
    first = rec("10:00:00.000", "REFramework entry") + "\r\n"
    last = rec("10:30:00.000", "Exception occurred: c0000005", "error") + "\r\n"
    path.write_bytes(first.encode() + b"x" * 5000 + b"\r\n" + last.encode())
    info = crashlog.read_log(path, max_bytes=2000)
    assert info.started.endswith("10:00:00.000") and info.crash.code == "c0000005"
    assert crashlog.first_time(path) == "2026-10-16 10:00:00.000"
    assert crashlog.first_time(tmp_path / "missing.txt") == ""
