"""What 원인 모드 찾기 starts from: the prior weights, shares and pairs from the crash record, material state,
REFramework's age and recent installs, and earlier runs as observations (synthetic records written here; no
real user data)."""

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from mhwmm import accessed, crashes, evidence, fsutil, gamewatch, precheck
from mhwmm.analyzer import classify
from mhwmm.crashes import ModIndex, ModRef
from mhwmm.crashlog import ACCESSED_FILE, FAULTY_FILE, LOG_FILE
from mhwmm.evidence import ModInfo

from conftest import make_zip, write

T0 = datetime(2026, 10, 20, 21, 30, 0).timestamp()  # the session's start (local time, as REFramework writes it)
CRASH = T0 + 600
NOW = T0 + 3600
VERSION = "v1"


def stamp(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def rec(t: float, msg: str, level: str = "info") -> str:
    return f"[{stamp(t)}] [REFramework] [{level}] {msg}"


def log_text(game: Path, body=(), crash: str | None = None, frames=(), start: float = T0, end: float = 600,
             rip: str = "") -> str:
    """A REFramework log: ``body`` records (offset, message, level), then the fatal block with the crashing
    module ``crash`` ("" for code in no module) ``end`` seconds in (at ``rip``: the module is at 7ff600000000),
    or (None) a normal last line."""
    lines = [rec(start, "REFramework entry"), rec(start, f"Current game path: {game}")]
    lines += [rec(start + dt, msg, level) for dt, msg, level in body]
    if crash is not None:
        t = start + end
        lines += [rec(t, "Exception occurred: c0000005", "error")]
        lines += [rec(t, f"RIP (exception record): {rip}", "error")] if rip else []
        lines += [rec(t, "Call stack:", "error"), rec(t, "", "error"),
                  " KiUserExceptionDispatcher", r"  C:\WINDOWS\SYSTEM32\ntdll.dll + 0x165dee", ""]
        for frame in [crash, *frames]:
            lines += [" Unknown symbol", f"  {frame} + 0x1234" if frame else "  0x2c3944f30", ""]
        lines.append(rec(t, f"Module: 7ff600000000 {crash}" if crash else "Module: Unknown", "error"))
    else:
        lines.append(rec(start + end, "Hooked DirectX 12"))
    return "\r\n".join(lines) + "\r\n"


def access_text(entries, constructed: float = T0 + 2) -> str:
    lines = [f"[{stamp(constructed)}] [LooseFileLoader] [info] {accessed.CONSTRUCTED}"]
    lines += [f"[{stamp(t)}] [LooseFileLoader] [info] {p}" for t, p in entries]
    return "\n".join(lines) + "\n"


def put(path: Path, data, mtime: float) -> Path:
    write(path, data)
    os.utime(path, (mtime, mtime))
    return path


class World:
    """A game folder, installed mods (all applied) and their crash records."""

    def __init__(self, tmp_path: Path):
        self.game = tmp_path / "MonsterHunterWilds"
        write(self.game / "MonsterHunterWilds.exe", "exe")
        write(self.game / "dinput8.dll", "MZ")
        self.data = tmp_path / "data"
        self.crash_dir = self.data / "crashes"
        self.mods: list[ModInfo] = []
        self.deployed: dict = {}
        self.material: dict[str, str] = {}
        self.stuck: dict[str, list[str]] = {}

    def add(self, mod_id: str, files, enabled: bool = True, stamp_: str = "2026-10-01T10:00:00|", refw: bool = False,
            changed_at: float | None = None, updated: bool = False, digest: str = "", edited: bool = False) -> ModInfo:
        info = ModInfo(mod_id, f"모드 {mod_id}", enabled, stamp_, digest, frozenset(classify(files)), tuple(files), refw,
                       changed_at, updated, edited)
        self.mods.append(info)
        for f in files if enabled else ():
            if f.lower().endswith(".pak") and "/" not in f:
                rel = f"pak_mods/{len(self.mods):03d}_{f}"
                self.deployed[rel.lower()] = {"path": rel, "mod": mod_id, "pak": True}
            else:
                self.deployed[f.lower()] = {"path": f, "mod": mod_id}
        return info

    def context(self, mods=None) -> dict:
        mods = self.mods if mods is None else mods
        return {"game_version": VERSION,
                "mods": [{"id": m.id, "name": m.name, "enabled": m.enabled, "stamp": m.stamp, "options": []} for m in mods],
                "placed": sorted({e["mod"] for e in self.deployed.values()}),
                "owners": {e["path"]: e["mod"] for k, e in self.deployed.items() if not k.startswith("natives/")}}

    def record(self, text: str, start: float = T0, faulty: str | None = None, access: str | None = None,
               context: dict | None = None) -> crashes.Session:
        crashes.note_launch(self.crash_dir, self.context() if context is None else context, now=start - 10)
        put(self.game / LOG_FILE, text, start + 600)
        if faulty is not None:
            put(self.game / FAULTY_FILE, faulty, start + 30)
        if access is not None:
            put(self.game / ACCESSED_FILE, access, start + 600)
        crashes.collect(self.crash_dir, self.game, {}, appdata="")
        return next(s for s in crashes.sessions(self.crash_dir) if s.started == stamp(start))

    def inputs(self, record_id: str = "", last_good=None, facts=None, owners=None) -> evidence.Inputs:
        refs = [ModRef(m.id, m.name, m.enabled, files=list(m.files), material_state=self.material.get(m.id, ""),
                       stuck=self.stuck.get(m.id, ())) for m in self.mods]
        return evidence.Inputs(self.crash_dir, self.game, self.data, VERSION, record_id, list(self.mods),
                               ModIndex(self.game, self.deployed, refs), owners or {}, facts, last_good)

    def build(self, equipment=None, **kw) -> evidence.Evidence:
        return evidence.build(self.inputs(**kw), equipment, now=NOW)


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


PLUGIN = "reframework/plugins/ArtianQoL.dll"
TEX = "natives/STM/Art/Model/armor/a_ALBD.tex.241106027"
MESH = "natives/STM/Art/Model/armor/b.mesh.241111606"
SCRIPT = "reframework/autorun/Foo.lua"


def standard(world: World) -> None:
    """A plugin, a texture-only mod, a mesh mod and a script."""
    world.add("P", [PLUGIN])
    world.add("T", [TEX])
    world.add("M", [MESH])
    world.add("S", [SCRIPT])


# ---- strong pointers (shares) ------------------------------------------------------------------------------
def test_a_crash_inside_a_mod_s_plugin(world):
    standard(world)
    s = world.record(log_text(world.game, crash=str(world.game / PLUGIN)))
    ev = world.build()
    assert ev.record == s.id and ev.record_title == s.title(NOW)
    assert ev.shares == {"P": 0.5}
    assert ev.reasons["P"] == ["튕긴 곳이 이 모드의 플러그인입니다 (ArtianQoL.dll + 0x1234)"]
    assert ev.weights == {"P": 2.0, "T": 0.6, "S": 1.3}  # (what kind of mod it is: no reason line)
    assert ev.none_scale == 1.0 and ev.or_scale == 1.0 and "T" not in ev.reasons and ev.informative


def test_a_crash_in_a_file_a_mod_put_into_the_game_folder(world):
    world.add("R", ["dxgi.dll"])
    world.add("M", [MESH])
    world.record(log_text(world.game, crash=str(world.game / "_storage_" / "dxgi.dll")))
    ev = world.build()
    assert ev.shares == {"R": 0.5} and ev.reasons["R"] == ["튕긴 곳이 이 모드가 넣은 파일 'dxgi.dll'입니다"]


def test_a_script_that_was_loading_when_the_game_stopped(world):
    world.add("S", [SCRIPT])
    world.add("M", [MESH])
    autorun = world.game / "reframework" / "autorun"
    sure = world.record(log_text(world.game, [(5, "[ScriptRunner] Initializing Lua state for the first time...", "info"),
                                              (6, f"[ScriptState] Running script {autorun / 'Foo.lua'}...", "info")]))
    ev = world.build(record_id=sure.id)  # (no fatal block: the user chose this record)
    assert ev.shares == {"S": 0.5} and ev.reasons["S"] == ["이 모드의 스크립트 'Foo.lua'를 읽다가 멈췄습니다"]
    # the first load had finished: only near the end of the log, not sure
    later = world.record(log_text(world.game, [(5, "[ScriptRunner] Initializing Lua state for the first time...", "info"),
                                               (6, "[ScriptRunner] Lua state initialized.", "info"),
                                               (590, f"[ScriptState] Running script {autorun / 'Foo.lua'}...", "info")],
                                  start=T0 + 1000), start=T0 + 1000)
    ev = world.build(record_id=later.id)
    assert ev.shares == {} and ev.weights["S"] == pytest.approx(3.0 * 1.3)
    assert ev.reasons["S"] == ["이 모드의 스크립트 'Foo.lua'를 읽다가 멈췄습니다 (확실하지 않음)"]


def access_world(world: World) -> None:
    world.add("L1", ["natives/STM/Art/l1/a.mesh.241111606", "natives/STM/Art/l1/b.mesh.241111606"])
    world.add("L2", ["natives/STM/Art/l2/c.mesh.241111606"])
    world.add("L3", ["natives/STM/Art/l3/d.mesh.241111606"])
    world.add("P", [PLUGIN])


def test_the_files_the_game_read_right_before_the_crash(world):
    access_world(world)
    s = world.record(log_text(world.game, crash=str(world.game / "MonsterHunterWilds.exe")), access=access_text([
        (T0 + 10, "natives/STM/GUI/game.tex.241106027"), (T0 + 200, "natives/STM/Art/l3/d.mesh.241111606"),
        (T0 + 595, "natives/STM/Art/l1/a.mesh.241111606"), (T0 + 597, "natives/STM/Art/l1/b.mesh.241111606")]))
    ev = world.build()
    assert ev.weights["L1"] == 5.0 and "L1" not in ev.shares  # (a time window: a weaker pointer than a crash place)
    assert ev.reasons["L1"] == ["튕기기 직전 10초 동안 게임이 읽은 모드 파일 2개가 모두 이 모드의 것입니다"]
    report = ev.passive[0]
    assert report["source"] == "report" and report["unexposed"] == ["L2"]  # L2's files were never asked for
    assert report["on"] == ["L1", "L2", "L3", "P"] and report["seconds"] == 600.0 and report["e"] == 0.005
    assert ev.notes[0] == f"튕김 기록 {datetime.fromtimestamp(CRASH):%H:%M} · 파일 기록 사용(모드 파일 3개)"
    found = {f.kind: f for f in crashes.explain(s, world.inputs().index).findings}
    assert found["accessed"].level == "high" and found["accessed"].mods == ["L1"]
    assert found["accessed"].text == ("튕기기 직전 10초 동안 게임이 읽은 모드 파일 2개가 모두 '모드 L1'의 것입니다 → 의심되는 모드 "
                                      "'모드 L1'")


def test_several_mods_read_right_before_the_crash(world):
    access_world(world)
    s = world.record(log_text(world.game, crash=str(world.game / "MonsterHunterWilds.exe")), access=access_text([
        (T0 + 10, "natives/STM/GUI/game.tex.241106027"), (T0 + 595, "natives/STM/Art/l1/a.mesh.241111606"),
        (T0 + 596, "natives/STM/Art/l2/c.mesh.241111606")]))
    ev = world.build()
    assert ev.weights["L1"] == ev.weights["L2"] == 3.0  # 1 + 6 / (2 + 1)
    assert ev.reasons["L2"] == ["튕기기 직전에 게임이 이 모드의 파일 1개를 읽었습니다"]
    found = {f.kind: f for f in crashes.explain(s, world.inputs().index).findings}
    assert found["accessed_some"].level == "low" and found["accessed_some"].mods == ["L1", "L2"]
    assert found["accessed_some"].text == "튕기기 직전 10초 동안 게임이 여러 모드의 파일을 읽었습니다: '모드 L1', '모드 L2'"


def test_one_file_of_one_mod_right_before_the_crash_says_little(world):
    # the record logs the first request of each file only: one new file of the player's armor near a crash is
    # common - a little more suspicion, far from the x5 of several files that are all one mod's
    access_world(world)
    world.record(log_text(world.game, crash=str(world.game / "MonsterHunterWilds.exe")), access=access_text([
        (T0 + 10, "natives/STM/GUI/game.tex.241106027"), (T0 + 596, "natives/STM/Art/l2/c.mesh.241111606")]))
    ev = world.build()
    assert ev.weights["L2"] == 1.5 and ev.reasons["L2"] == ["튕기기 직전에 게임이 이 모드의 파일 1개를 읽었습니다"]


# ---- weights from the record -------------------------------------------------------------------------------
def test_files_the_game_could_not_load(world, tmp_path):
    world.add("D", ["natives/STM/GameDesign/x.user.3"])
    world.add("E", ["natives/STM/Art/e.tex.241106027"])
    faulty = '"error" 2 "Invalid file" "GameDesign/x.user"\r\n"error" 2 "Invalid file" "Art/e.tex"\r\n'
    world.record(log_text(world.game, crash=str(world.game / "MonsterHunterWilds.exe")), faulty=faulty)
    ev = world.build()
    assert ev.weights == {"D": pytest.approx(6.0 * 1.5), "E": pytest.approx(5.0 * 0.6)}
    assert ev.reasons["D"] == ["REFramework가 이 모드의 파일 x.user을(를) 읽지 못했습니다"]
    # the material check says the file fits this game version: much weaker
    world.material["D"] = "current"
    ev = world.build()
    assert ev.weights["D"] == pytest.approx(1.5 * 1.5)
    assert ev.reasons["D"] == ["REFramework가 이 모드의 파일 x.user을(를) 읽지 못한 파일로 기록했지만 재질 점검에서는 맞는 파일입니다"]


def test_the_call_stack_lua_errors_and_plugins_that_did_not_start(world):
    world.add("H", ["reframework/plugins/Helper.dll"])
    world.add("U", ["reframework/autorun/lib/util.lua"])
    world.add("O", ["reframework/plugins/Old.dll"])
    world.add("X", ["reframework/plugins/Boom.dll"])
    world.add("K", ["Armor.pak"])
    world.deployed["pak_mods/002_armor.pak"] = {"path": "pak_mods/002_Armor.pak", "mod": "K", "pak": True}
    body = [(7, r"sol: runtime error: ...ers\x\MonsterHunterWilds\reframework\autorun\lib\util.lua:3: boom", "error"),
            (9, "[PluginLoader] Plugin Old requires a different major version", "error"),
            (9, "[PluginLoader] Boom has an exception in reframework_plugin_initialize, skipping...", "error"),
            (10, f"[IntegrityCheckBypass]: pak_load_check_function_hook returned false for string: "
                 f"{world.game / 'pak_mods' / '002_Armor.pak'}", "warning"),
            (15, "[HookManager] Cannot add nullptr function", "error")]
    world.record(log_text(world.game, body, crash=str(world.game / "MonsterHunterWilds.exe"),
                          frames=[str(world.game / "reframework" / "plugins" / "Helper.dll")]))
    ev = world.build()
    assert ev.weights["H"] == pytest.approx(2.0 * 2.0)  # stack, plugin
    assert ev.reasons["H"] == ["튕긴 호출 기록에 이 모드의 플러그인이 있습니다"]
    assert ev.weights["U"] == pytest.approx(1.5 * 1.5 * 1.3)  # Lua error, hooks, script
    assert ev.reasons["U"] == ["이 모드의 스크립트에서 오류가 기록되었습니다", "Lua 스크립트가 게임에 없는 함수를 고치려 했습니다"]
    assert ev.weights["O"] == pytest.approx(0.3 * 2.0)
    assert ev.reasons["O"] == ["이 모드의 플러그인은 시작하지 못해 실행되지 않았습니다"]
    assert ev.weights["X"] == pytest.approx(1.5 * 2.0)
    assert ev.reasons["X"] == ["이 모드의 플러그인이 시작하다가 오류를 냈습니다"]
    assert ev.weights["K"] == pytest.approx(0.3) and ev.reasons["K"] == ["이 모드의 PAK은 게임에 적용되지 않았습니다"]


@pytest.mark.parametrize("module, none, weights, none_reason", [
    ("_storage_/dinput8.dll", 1.0, {"refw": 6.0, "S": 1.5 * 1.3, "P": 2.0, "T": 0.6}, ""),
    # (a crash in the graphics runtime is no hint that it is not the mods: render data, models and textures alike, up)
    ("D3D12/D3D12Core.dll", 1.0, {"P": 2.0, "T": 1.5, "M": 1.5, "S": 0.8 * 1.3}, ""),
    ("", 1.0, {"P": 1.5 * 2.0, "T": 0.6, "S": 1.5 * 1.3}, ""),
    (r"C:\WINDOWS\SYSTEM32\ntdll.dll", 1.5, {"P": 2.0, "T": 0.6, "S": 1.3}, "Windows 시스템 파일에서 튕겼습니다"),
    (r"C:\Program Files\Avast\aswhook.dll", 3.0, {"P": 2.0, "T": 0.6, "S": 1.3}, "튕긴 곳이 매니저가 넣지 않은 파일입니다"),
    ("_storage_/something_new.dll", 3.0, {"P": 2.0, "T": 0.6, "S": 1.3}, "튕긴 곳이 매니저가 넣지 않은 파일입니다"),
])
def test_where_it_crashed(world, module, none, weights, none_reason):
    standard(world)
    world.add("refw", ["dinput8.dll"], refw=True)
    path = module if ":" in module or not module else str(world.game / module)
    world.record(log_text(world.game, crash=path))
    ev = world.build()
    assert ev.none_scale == pytest.approx(none) and ev.none_reasons == ([none_reason] if none_reason else [])
    assert ev.weights == pytest.approx(weights)
    if module.endswith("dinput8.dll"):
        assert ev.reasons["refw"] == ["튕긴 곳이 REFramework(dinput8.dll) 안입니다"]
    if module == "":
        assert ev.reasons["P"] == ["튕긴 곳이 어느 파일에도 속하지 않는 코드입니다 (플러그인·스크립트가 바꾼 게임 코드일 수 있음)"]
    if module.startswith("D3D12"):
        assert ev.reasons["M"] == ["그래픽(D3D12) 쪽에서 튕겼고 이 모드에 모델 파일이 있습니다"]
        assert ev.reasons["T"] == ["그래픽(D3D12) 쪽에서 튕겼고 이 모드에 텍스처 파일이 있습니다"]


ARMOR_MESH = "natives/STM/Art/Model/Character/ch03/k/k.mesh.241111606"
ARMOR_CLOTH = "natives/STM/Art/Model/Character/ch03/f/f.gpuc.241111606"
ARMOR_FUR = "natives/STM/Art/Model/Character/ch03/f/f.sfur.241111606"
ARMOR_CHAIN = "natives/STM/Art/Model/Character/ch03/f/f.chain2.241111606"
HUNG = "Fatal D3D error (24, DXGI_ERROR_DEVICE_REMOVED, 0x887a0005) DeviceRemovedReason(0x887a0006)"
DRIVER = "Fatal D3D error (24, DXGI_ERROR_DEVICE_REMOVED, 0x887a0005) DeviceRemovedReason(0x887a0020)"


def graphics_record(world: World, message: str) -> crashes.Session:
    """The game's own report of a graphics error 590 s in (REFramework saw nothing, as in a real DEVICE_HUNG)."""
    from test_crashes import game_report_text, report_zip

    report_zip(world.game / "CrashReport" / datetime.fromtimestamp(CRASH).strftime("%Y_%m%d_%H%M.zip"),
               game_report_text(world.game, message), CRASH - 10, CRASH + 40, started=T0 - 1)
    return world.record(log_text(world.game))


@pytest.mark.parametrize("message, error, render, stale, pak", [
    (HUNG, "DEVICE_HUNG", 2.5, 3.0, 1.6),
    (HUNG.replace("0x887a0006", "0x887a0007"), "DEVICE_RESET", 2.5, 3.0, 1.6),
    (DRIVER, "DRIVER_INTERNAL_ERROR", 1.5, 2.0, 1.2),  # (the driver's own failure: render data up less, still not "not mods")
])
def test_a_graphics_error_points_at_render_data_not_away_from_the_mods(world, monkeypatch, message, error, render,
                                                                       stale, pak):
    # the graphics card could not finish the work the game gave it ("badly formed commands", Microsoft's DXGI
    # documentation): with mods that work is often a mod's model, texture, material or shader, or a graphics plugin.
    # So no "not the mods" weight at all; those mods up (more with files that do not fit this game version, a pak
    # mod whose files are not known a little), Lua scripts and data files a little down
    from mhwmm.matfix.pak import path_hash, write_pak

    standard(world)  # P plugin, T texture, M mesh, S script
    world.add("X", ["natives/STM/Art/Model/armor/c.mesh.241111606", "natives/STM/Art/Model/armor/c.mdf2.45"])
    world.material["X"] = "stale"
    world.add("D", ["natives/STM/GameDesign/z.user.3"])
    world.add("R", ["dxgi.dll", "reshade-shaders/Shaders/Bloom.fx"])
    world.add("K", ["Armor.pak"])  # (its entries named by the list of the game's paths: a model)
    world.add("U", ["Unknown.pak"])  # (not in the game folder: its files are not known)
    # what the graphics card works on itself: GPU particle effects, GPU cloth and fur (loose files and a pak's entries)
    world.add("E", ["natives/STM/Art/Effect/Wp/x.efx.4064419"])
    world.add("C", ["natives/STM/Art/Model/armor/c.gpuc.241111606", "natives/STM/Art/Model/armor/c.chain2.13",
                    "natives/STM/GameDesign/c.user.3"])
    world.add("B", ["natives/STM/Art/Model/armor/b.gpbf.241111606"])
    world.add("F", ["Fur.pak"])  # (named entries only GPU cloth, fur and a chain)
    for mod, entries in (("K", [ARMOR_MESH]), ("F", [ARMOR_CLOTH, ARMOR_FUR, ARMOR_CHAIN])):
        placed = world.game / next(e["path"] for e in world.deployed.values() if e["mod"] == mod)
        placed.parent.mkdir(parents=True, exist_ok=True)
        write_pak([(path_hash(name), b"x", 1, 0) for name in entries], placed)
    names = {path_hash(name): name.lower() for name in (ARMOR_MESH, ARMOR_CLOTH, ARMOR_FUR, ARMOR_CHAIN)}
    monkeypatch.setattr(accessed, "PakNames", lambda cache_dir=None: names.get)
    s = graphics_record(world, message)
    assert s.summary["device_error"] == error and s.summary["category"] == "gpu"
    ev = world.build()
    assert ev.record == s.id and ev.device_error == error
    assert ev.none_scale == 1.0 and ev.none_reasons == []
    assert ev.weights == pytest.approx({"M": render, "T": render, "X": stale * 3.0, "R": render, "K": render, "U": pak,
                                        "E": render, "C": 1.5 * render, "B": render, "F": render,
                                        "P": 2.0, "S": 1.3 * 0.8, "D": 1.5 * 0.8})
    assert min(ev.weights[x] for x in "MTXRKECBF") > ev.weights["S"]  # (a mesh or texture mod over a Lua script)
    lead = f"게임이 그래픽 오류({error})로 멈췄고 "
    assert ev.reasons["M"] == [lead + "이 모드에 모델 파일이 있습니다"]
    assert ev.reasons["T"] == [lead + "이 모드에 텍스처 파일이 있습니다"]
    assert lead + "이 모드에 모델·재질 파일이 있습니다" in ev.reasons["X"]
    assert "이 게임 버전과 맞지 않는 재질·데이터 파일이 있습니다" in ev.reasons["X"]
    assert ev.reasons["R"] == [lead + "이 모드는 그래픽 플러그인(ReShade·업스케일러·프레임 생성 등)입니다"]
    assert ev.reasons["K"] == [lead + "이 모드에 모델 파일이 있습니다"]
    assert ev.reasons["U"] == [lead + "이 모드의 PAK에 모델·텍스처가 들어 있을 수 있습니다"]
    assert ev.reasons["E"] == [lead + "이 모드에 이펙트 파일이 있습니다"]
    assert ev.reasons["C"] == [lead + "이 모드에 GPU 옷감 파일이 있습니다"]
    assert ev.reasons["B"] == [lead + "이 모드에 GPU 버퍼 파일이 있습니다"]
    assert ev.reasons["F"] == [lead + "이 모드에 GPU 옷감·털 파일이 있습니다"]
    assert "S" not in ev.reasons and "D" not in ev.reasons and "P" not in ev.reasons


def test_a_crash_that_is_no_graphics_crash_leaves_render_data_alone(world):
    # (the texture-only mod keeps its lower weight: plugins crash the game more often than textures)
    standard(world)
    graphics_record(world, "Some other error")
    ev = world.build()
    assert ev.device_error == "" and ev.none_scale == 1.0
    assert ev.weights == pytest.approx({"P": 2.0, "T": 0.6, "S": 1.3}) and not ev.reasons


def test_files_no_mod_placed_that_refw_could_not_load(world):
    world.add("M", [MESH])
    write(world.game / "natives" / "STM" / "GameDesign" / "y.user.3", "by hand")
    world.record(log_text(world.game, crash=str(world.game / "MonsterHunterWilds.exe")),
                 faulty='"error" 2 "Invalid file" "GameDesign/y.user"\r\n')
    ev = world.build()
    assert ev.none_scale == 1.5 and ev.none_reasons == ["매니저가 넣지 않은 파일을 REFramework가 읽지 못했습니다"]


def test_material_state(world):
    world.add("A", [MESH])
    world.add("B", ["natives/STM/GameDesign/z.user.3"])
    world.add("C", [TEX], enabled=False)
    world.material.update({"A": "stale", "C": "stale"})
    world.stuck["B"] = ["z.user", "w.user"]
    ev = world.build()
    assert ev.weights == {"A": 3.0, "B": pytest.approx(4.0 * 1.5)}  # (C is off)
    assert ev.reasons == {"A": ["이 게임 버전과 맞지 않는 재질·데이터 파일이 있습니다"], "B": ["이 게임 버전에 맞게 고치지 못한 파일이 2개 있습니다"]}
    assert ev.record == "" and ev.passive == [] and ev.notes == ["재질 점검: 이 게임 버전과 맞지 않는 파일이 있는 모드 2개"]


def test_refw_that_failed_to_start_or_is_older_than_the_game(world):
    world.add("refw", ["dinput8.dll"], refw=True)
    world.add("M", [MESH])
    world.record(log_text(world.game, [(14, "Timed out waiting for VM to initialize.", "error")],
                          crash=str(world.game / "MonsterHunterWilds.exe")))
    ev = world.build()
    assert ev.weights == {"refw": 8.0} and ev.reasons["refw"] == ["REFramework가 이 게임 버전에서 제대로 시작하지 못했습니다"]
    assert ev.or_scale == 1.0
    # the game was updated after REFramework was put in
    dll = world.game / "dinput8.dll"
    os.utime(world.game / "MonsterHunterWilds.exe", (dll.stat().st_mtime + 7200,) * 2)
    states = [precheck.ModState(m.id, m.name, m.enabled, m.stamp, m.kinds, list(m.files), refw=m.refw) for m in world.mods]
    facts = precheck.Facts(world.game, states, world.deployed, game_version=VERSION)
    ev = world.build(facts=facts)
    assert ev.weights == {"refw": 8.0} and ev.or_scale == 2.0  # (the row counts once)
    assert ev.reasons["refw"] == ["REFramework가 이 게임 버전에서 제대로 시작하지 못했습니다",
                                  "게임 업데이트 뒤로 REFramework가 업데이트되지 않았습니다"]
    assert "게임 업데이트 뒤로 REFramework가 업데이트되지 않았습니다" in ev.notes


def test_mods_installed_or_updated_shortly_before_the_crash(world):
    world.add("A", [MESH], changed_at=CRASH - 5 * 3600)
    world.add("B", ["natives/STM/Art/x.mesh.241111606"], changed_at=CRASH - 3 * 86400, updated=True)
    world.add("C", ["natives/STM/Art/y.mesh.241111606"], changed_at=CRASH - 30 * 86400)
    world.add("D", ["natives/STM/Art/z.mesh.241111606"], changed_at=CRASH + 600)  # (after the crash)
    world.add("E", ["natives/STM/Art/w.mesh.241111606"], changed_at=CRASH - 2 * 3600, updated=True, edited=True)
    world.record(log_text(world.game, crash=str(world.game / "MonsterHunterWilds.exe")))
    ev = world.build()
    assert ev.weights == {"A": 2.0, "B": 1.3, "E": 2.0}
    assert ev.reasons == {"A": ["튕기기 5시간 전에 설치했습니다"], "B": ["튕기기 3일 전에 업데이트했습니다"],
                          "E": ["튕기기 2시간 전에 모드 수정에서 바꿨습니다"]}
    assert "최근 설치·업데이트·수정한 모드 3개" in ev.notes
    # a last normal exit says exactly what changed: no guess from install times
    assert world.build(last_good={"time": T0 - 86400, "context": {"mods": []}}).weights == {}


def test_misleading_records_stay_within_bounds(world):
    world.add("P", [PLUGIN])
    world.add("F", [SCRIPT])
    world.add("D", ["natives/STM/GameDesign/x.user.3"], changed_at=CRASH - 3600)
    world.stuck["D"] = ["x.user"]
    autorun = world.game / "reframework" / "autorun"
    world.record(log_text(world.game, [(5, "[ScriptRunner] Initializing Lua state for the first time...", "info"),
                                       (6, f"[ScriptState] Running script {autorun / 'Foo.lua'}...", "info")],
                          crash=str(world.game / PLUGIN)),
                 faulty='"error" 2 "Invalid file" "GameDesign/x.user"\r\n')
    ev = world.build()
    assert ev.shares == pytest.approx({"P": 0.425, "F": 0.425})  # (two pointers: their sum is cut to 0.85)
    assert ev.weights["D"] == evidence.WEIGHT_MAX  # 6 * 4 * 1.5 * 2, cut
    assert all(evidence.WEIGHT_MIN <= w <= evidence.WEIGHT_MAX for w in ev.weights.values())
    assert len(ev.reasons["D"]) == evidence.REASONS_MAX
    assert ev.reasons["D"][0] == "REFramework가 이 모드의 파일 x.user을(를) 읽지 못했습니다"  # (the strongest first)


# ---- pairs ---------------------------------------------------------------------------------------------------
def test_pairs_of_mods_that_may_crash_only_together(world, monkeypatch):
    world.add("P1", ["reframework/plugins/a.dll"])
    world.add("P2", ["reframework/plugins/b.dll"])
    world.add("S1", ["reframework/autorun/a.lua"])
    world.add("S2", ["reframework/autorun/b.lua"])
    world.add("A", ["natives/STM/Art/armor/x.mesh.241111606"])
    world.add("B", ["natives/STM/Art/armor/x_ALBD.tex.241106027"])
    world.add("C", ["natives/STM/Art/armor/x.mesh.241111606"])  # C's mesh covers A's
    world.add("Off", ["natives/STM/Art/armor/y.mesh.241111606"], enabled=False)
    owners = {"natives/stm/art/armor/x.mesh.241111606": ["A", "C"]}
    many = [f"N{i}" for i in range(9)]
    for i in many:
        world.add(i, [f"natives/STM/Art/body/{i}.tex.241106027"])
    equipment = [("armor:f/003/001", "레더 헬름 (여성)", ["A", "B", "C", "Off"]),
                 ("armor:f/000/000", "기본 몸", many)]  # (changed by more than 8 mods: says little)
    ev = world.build(equipment, owners=owners)
    assert ev.equipment == equipment
    assert ev.pairs == [("A", "B", 10.0), ("B", "C", 10.0), ("P1", "P2", 3.0), ("S1", "S2", 2.0)]
    monkeypatch.setattr(evidence, "MAX_PAIRS", 2)
    assert world.build(equipment, owners=owners).pairs == [("A", "B", 10.0), ("B", "C", 10.0)]


# ---- the record and earlier runs as observations ---------------------------------------------------------------
def test_the_record_as_an_observation(world):
    world.add("A", [MESH])
    world.add("B", ["natives/STM/Art/x.mesh.241111606"])
    context = world.context()
    world.add("New", ["natives/STM/Art/n.mesh.241111606"])  # installed since: not part of the record
    world.mods[1] = ModInfo(**{**world.mods[1].__dict__, "stamp": "2026-10-01T10:00:00|2026-10-20T23:00:00"})
    world.record(log_text(world.game, crash=str(world.game / "MonsterHunterWilds.exe"), end=95), context=context)
    ev = world.build()
    (report,) = ev.passive
    assert report == {"source": "report", "on": ["A", "B"], "crashed": True, "rho": 1.0, "seconds": 95.0,
                      "unknown": ["B"], "unexposed": [], "e": 0.005,
                      "label": f"튕김 기록 {datetime.fromtimestamp(T0 + 95):%H:%M}"}
    assert ev.crash_times == [95.0] and ev.when_hint == "start" and ev.minutes_hint is None


@pytest.mark.parametrize("how", ["removed", "off"])
def test_a_record_with_a_mod_that_is_gone_now_is_not_a_launch(world, how):
    # the user crashed with Z, removed (or turned off) Z, installed X and crashes again: the old record would read
    # "crashed with X off" and clear the culprit
    world.add("A", [MESH])
    z = world.add("Z", ["natives/STM/Art/z.mesh.241111606"])
    context = world.context()
    if how == "removed":
        world.mods.remove(z)
    else:
        world.mods[1] = ModInfo(**{**z.__dict__, "enabled": False})
    world.add("X", ["natives/STM/Art/x.mesh.241111606"])
    world.record(log_text(world.game, crash=str(world.game / "MonsterHunterWilds.exe"), end=95), context=context)
    ev = world.build()
    assert ev.record and not any(p["source"] == "report" for p in ev.passive) and ev.crash_times == []
    assert any("그때 켜져 있던 모드 '모드 Z'이(가) 지금은 꺼져 있거나 없어 실행 결과로는 쓰지 않음" in n for n in ev.notes)
    # a record the manager does not know the mods of says nothing as an observation
    world2 = World(world.game.parent / "other")
    world2.add("A", [MESH])
    world2.record(log_text(world2.game, crash=str(world2.game / "MonsterHunterWilds.exe")), context={})
    ev = world2.build()
    assert ev.record and ev.passive == [] and "켜져 있던 모드를 몰라" in ev.notes[0]


def test_an_old_or_other_version_crash_is_not_used_unless_chosen(world):
    world.add("A", [MESH])
    s = world.record(log_text(world.game, crash=str(world.game / "MonsterHunterWilds.exe")))
    assert evidence.build(world.inputs(), now=NOW + 4 * 86400).record == ""
    inputs = world.inputs()
    inputs.game_version = "v2"
    assert evidence.build(inputs, now=NOW).record == ""
    assert evidence.build(world.inputs(record_id=s.id), now=NOW + 30 * 86400).record == s.id


def row(t, outcome, mods, seconds=600.0, code="", category="", version=VERSION, bisect=False, record="", where=""):
    return {"time": t, "started": t - seconds, "seconds": seconds, "outcome": outcome, "code": code,
            "category": category, "game_version": version, "bisect": bisect, "record": record, "where": where,
            "mods": [[i, "2026-10-01T10:00:00|", ""] for i in mods]}


def test_earlier_runs_of_the_same_setup(world):
    world.add("A", [MESH])
    world.add("B", ["natives/STM/Art/x.mesh.241111606"])
    s = world.record(log_text(world.game, crash=str(world.game / PLUGIN), rip="7ff600001000"))
    assert crashes.crash_where(s) == "artianqol.dll+0x1000"
    good = NOW - 7200
    here = "artianqol.dll+0x1000"
    rows = [row(good, "normal", ["A"]),  # the last normal exit: the search takes it itself
            row(NOW - 7000, "normal", ["A", "B"], version="v0"),  # another game version
            row(NOW - 6900, "normal", ["A", "B"], bisect=True),  # a step of an earlier search
            row(NOW - 6800, "crash", ["A", "B"], code="c0000005", category="plugin", record=s.id),  # the record itself
            row(NOW - 6700, "unknown", ["A", "B"]),
            row(NOW - 6600, "crash", ["A", "B"], code="c0000409", category="plugin"),  # another crash
            row(NOW - 6550, "crash", ["A", "B"], code="c0000005", where="artianqol.dll+0x2000"),  # elsewhere in it
            row(NOW - 6540, "crash", ["A", "B"], code="c0000005", where="other.dll+0x1000"),  # another module
            row(NOW - 6520, "crash", ["A", "B", "Gone"], code="c0000005", where=here),  # with a mod not here now
            row(NOW - 6510, "crash", ["A", "B"], seconds=300.0, code="c0000005"),  # the place not known: weak
            row(NOW - 6500, "crash", ["A", "B"], seconds=420.0, code="c0000005", category="", where=here),  # the same
            row(NOW - 6400, "normal", ["A"], seconds=1500.0)]
    rows[-1]["mods"][0][1] = "2026-09-01T10:00:00|"  # A was installed again since
    rows += [row(NOW - 10 * 86400 - i, "normal", ["B"]) for i in range(30)]
    fsutil.atomic_write_json(world.crash_dir / gamewatch.LAUNCHES_FILE, {"format": 1, "rows": rows})
    ev = world.build(last_good={"time": good, "context": {"mods": [], "game_version": VERSION}})
    history = [p for p in ev.passive if p["source"] == "history"]
    assert len(history) == evidence.HISTORY_ROWS
    assert history[0] == {"source": "history", "on": ["A"], "crashed": False, "rho": 0.6, "seconds": 1500.0,
                          "unknown": ["A"], "unexposed": [], "e": None,
                          "label": f"실행 기록 {datetime.fromtimestamp(NOW - 6400):%H:%M} (정상 종료 25분)"}
    assert history[1] == {"source": "history", "on": ["A", "B"], "crashed": True, "rho": 1.0, "seconds": 420.0,
                          "unknown": [], "unexposed": [], "e": evidence.E_HISTORY_EXACT,
                          "label": f"실행 기록 {datetime.fromtimestamp(NOW - 6500):%H:%M} (튕김 7분)"}
    assert history[2]["crashed"] and history[2]["seconds"] == 300.0 and history[2]["e"] == evidence.E_HISTORY_LOOSE
    assert all(p["on"] == ["B"] and not p["crashed"] for p in history[3:])
    assert ev.crash_times == [600.0, 420.0] and ev.when_hint == "time" and ev.minutes_hint == 8.5  # (the same place only)
    assert "이전 실행 기록 20개 (그중 같은 방식으로 튕김 2번)" in ev.notes
    # without a crash record no earlier crash is taken (nothing to compare it with)
    for p in crashes.sessions(world.crash_dir):
        fsutil.rmtree(p.folder)
    ev = world.build()
    assert not any(p["crashed"] for p in ev.passive) and ev.when_hint == ""


def test_when_the_game_crashes(world):
    world.add("A", [MESH])
    world.record(log_text(world.game, crash=str(world.game / "MonsterHunterWilds.exe"), end=40, rip="7ff600001000"))
    here = "monsterhunterwilds.exe+0x1000"
    fsutil.atomic_write_json(world.crash_dir / gamewatch.LAUNCHES_FILE, {"format": 1, "rows": [
        row(NOW - 3 * 86400, "crash", ["A"], seconds=120.0, code="c0000005", where=here),
        row(NOW - 4 * 86400, "crash", ["A"], seconds=2000.0, code="c0000005"),  # (the place not known: not "when")
        row(NOW - 20 * 86400, "crash", ["A"], seconds=3000.0, code="c0000005", where=here)]})  # (too old to say when)
    ev = world.build()
    assert ev.when_hint == "start" and ev.minutes_hint is None and ev.crash_times == [40.0, 120.0, 3000.0]


# ---- the equipment picker --------------------------------------------------------------------------------------
class FakeCatalog:
    def resolve(self, target):
        return target

    def entry(self, target):
        return None

    def names(self, target):
        return {"f/003/001": ["레더 헬름"]}.get(target.key, [])

    def rarity_range(self, target):
        return None, None

    def rarities(self, target):
        return []

    def weapon_type_name(self, wtype):
        return "랜스"


def test_the_equipment_the_mods_change():
    from mhwmm.equip.model import Target
    from mhwmm.equip.scan import ModScan

    helm = ModScan({Target("armor", "f/003/001", "helm"): 2})
    both = ModScan({Target("armor", "f/003/001", "body"): 1, Target("weapon", "02/005"): 3})
    mods = [SimpleNamespace(id=i, name=f"모드 {i}", enabled=True) for i in ("A", "B", "C")]
    scans = {"A": ("sa", helm), "B": ("sb", both), "C": ("old", helm)}
    manager = SimpleNamespace(mods=mods, _equip_lock=threading.RLock(), _load_equip_scans=lambda: scans,
                              equipment_catalog=lambda build=True: FakeCatalog(),
                              equipment_job=lambda i: SimpleNamespace(stamp={"A": "sa", "B": "sb", "C": "sc"}[i]))
    choices = evidence.equipment_choices(manager, ["A", "B", "C"])
    assert choices[0] == ("armor:f/003/001", "레더 헬름 (여성)", ["A", "B"])  # (C changed since its scan: not known)
    assert [c[2] for c in choices[1:]] == [["B"]]
    assert evidence.equipment_choices(manager, ["C"]) == []
    manager.equipment_catalog = lambda build=True: None  # the equipment window never ran for this game version
    assert evidence.equipment_choices(manager, ["A", "B"]) == []
    manager.equipment_catalog = lambda build=True: 1 / 0
    assert evidence.equipment_choices(manager, ["A"]) == []


# ---- never in the way ------------------------------------------------------------------------------------------
def test_a_broken_record_folder_never_stops_the_search(world):
    world.add("A", [MESH])
    s = world.record(log_text(world.game, crash=str(world.game / "MonsterHunterWilds.exe")),
                     access=access_text([(T0 + 10, MESH)]))
    (s.folder / crashes.ACCESSED_COPY).write_bytes(b"not gzip")
    (s.folder / LOG_FILE).write_bytes(b"\xff\xfe\x00garbage")
    ev = world.build()
    assert isinstance(ev, evidence.Evidence) and ev.record == s.id
    inputs = world.inputs()
    inputs.index = None  # (every source that needs it fails on its own)
    ev = evidence.build(inputs, now=NOW)
    assert isinstance(ev, evidence.Evidence) and ev.record == s.id
    assert evidence.build(SimpleNamespace(), now=NOW) == evidence.Evidence()


def test_gathered_from_a_manager(manager, tmp_path, game_dir):
    def install(name, files):
        with manager.prepare_install(make_zip(tmp_path / "zips" / f"{name}.zip", files)) as s:
            return manager.install_variant(s, s.variants[0])

    refw = install("REFramework", {"dinput8.dll": "MZ"})
    tex = install("Armor", {"natives/STM/Art/a.tex.241106027": "a"})
    manager.set_enabled(refw.id, True)
    manager.set_enabled(tex.id, True)
    inputs = evidence.gather(manager, "20261020-213000")
    assert inputs.record_id == "20261020-213000" and inputs.crash_dir == manager.crash_dir
    assert inputs.game_dir == game_dir and inputs.game_version == manager.game_version()
    by_id = {m.id: m for m in inputs.mods}
    assert by_id[refw.id].refw and not by_id[tex.id].refw and by_id[tex.id].kinds == {"loose"}
    assert by_id[tex.id].files == ("natives/STM/Art/a.tex.241106027",) and by_id[tex.id].digest == ""
    assert by_id[tex.id].changed_at == pytest.approx(datetime.fromisoformat(tex.installed_at).timestamp())
    assert isinstance(inputs.index, ModIndex) and isinstance(inputs.precheck, precheck.Facts)
    assert inputs.last_good is None and inputs.owners == {}
    assert not evidence.build(inputs, now=time.time() + 30 * 86400).informative  # (only the kinds of the mods)
    ev = evidence.build(inputs)  # (both installed just now, and no normal exit since)
    assert ev.weights == {refw.id: 2.0, tex.id: pytest.approx(0.6 * 2.0)} and ev.record == ""
    assert ev.reasons[tex.id] == ["1분 전에 설치했습니다"]


def test_options_digest():
    assert evidence.options_digest([]) == "" and evidence.options_digest(None) == ""
    assert evidence.options_digest(["B", "A"]) == evidence.options_digest(["A", "B"]) != evidence.options_digest(["A"])
    assert len(evidence.options_digest(["빨강"])) == 8
