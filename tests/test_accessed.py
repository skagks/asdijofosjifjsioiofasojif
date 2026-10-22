"""REFramework's record of the files the game asked for: reading it, mapping requests to mods, and the
setting's consent and restore (synthetic records, paks and settings written here; no real user data)."""

import gzip
import random
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from mhwmm import accessed, crashes, crashlog
from mhwmm.accessed import CONSTRUCTED, LOG_KEY, LOOSE_KEY
from mhwmm.crashes import ModIndex, ModRef
from mhwmm.gamewatch import GameExit
from mhwmm.matfix.pak import path_hash, string_hash, write_pak

from conftest import write

T0 = datetime(2026, 10, 21, 20, 0, 0).timestamp()  # local time, as REFramework writes it


def stamp(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def line(t: float, msg: str) -> str:
    return f"[{stamp(t)}] [LooseFileLoader] [info] {msg}"


def record(entries, constructed: float | None = T0, newline: str = "\n") -> str:
    """A record: the CONSTRUCTED line at ``constructed`` (None: cut off), then (time, path) lines."""
    lines = [line(constructed, CONSTRUCTED)] if constructed is not None else []
    lines += [line(t, p) for t, p in entries]
    return newline.join(lines) + newline


# ---- reading -----------------------------------------------------------------------------------------
def test_default_lines_and_the_forms_a_path_may_take():
    text = record([
        (T0 + 9.0, "natives/STM/GUI/ui_texture/tex_000.tex.241106027"),
        (T0 + 9.5, r"natives\STM\Art\Model\Character\ch03\004\001\2\ch03_004_0012.mesh.241111606"),
        (T0 + 10.0, "@natives/STM/GameDesign/Common/Param.user.3"),
        (T0 + 10.25, '"natives/STM/Art/Item/it0300_0009_0.mdf2.45"'),
        (T0 + 11.0, "G:/Steam/steamapps/common/MonsterHunterWilds/natives/STM/Stage/st101/a.scn.21"),
        (T0 + 11.5, r"C:\Program Files (x86)\Steam\steamapps\common\MonsterHunterWilds\natives\STM\b.tex.241106027"),
        (T0 + 12.0, "Art/Model/Item/it03/00/0009/it0300_0009_0.mesh.241111606"),  # no prefix: kept as is
        (T0 + 13.0, "something else entirely"),
    ])
    log = accessed.parse(text)
    assert log.started == T0 and log.logging and log.lines == 9
    assert list(log.paths) == [
        "natives/STM/GUI/ui_texture/tex_000.tex.241106027",
        "natives/STM/Art/Model/Character/ch03/004/001/2/ch03_004_0012.mesh.241111606",
        "natives/STM/GameDesign/Common/Param.user.3",
        "natives/STM/Art/Item/it0300_0009_0.mdf2.45",
        "natives/STM/Stage/st101/a.scn.21",
        "natives/STM/b.tex.241106027",
        "Art/Model/Item/it03/00/0009/it0300_0009_0.mesh.241111606",
    ]
    assert log.paths["natives/STM/Art/Item/it0300_0009_0.mdf2.45"] == pytest.approx(T0 + 10.25)
    assert log.first_path == pytest.approx(T0 + 9.0) and log.last_time == pytest.approx(T0 + 13.0)
    assert log.other == ["something else entirely"] and not log.truncated


def test_a_bom_crlf_and_untimed_lines():
    text = "\ufeff" + record([(T0 + 5, "natives/STM/a.tex.1"), (T0 + 7, "natives/STM/c.tex.1")], newline="\r\n")
    text = text.replace(line(T0 + 7, "natives/STM/c.tex.1"), "natives/STM/b.tex.1\r\n" + line(T0 + 7, "natives/STM/c.tex.1"))
    log = accessed.parse(text)
    assert log.started == T0
    assert log.paths == {"natives/STM/a.tex.1": pytest.approx(T0 + 5), "natives/STM/b.tex.1": pytest.approx(T0 + 5),
                         "natives/STM/c.tex.1": pytest.approx(T0 + 7)}  # (untimed: the previous line's time)
    assert list(log.paths) == ["natives/STM/a.tex.1", "natives/STM/b.tex.1", "natives/STM/c.tex.1"]


def test_a_cut_first_line_only_constructed_and_duplicates():
    cut = accessed.parse(record([(T0 + 9, "natives/STM/a.user.3"), (T0 + 20, "natives/STM/b.user.3")], constructed=None))
    assert cut.started == pytest.approx(T0 + 9) and len(cut.paths) == 2  # the earliest time seen

    off = accessed.parse(record([]))
    assert off.started == T0 and not off.logging and off.first_path is None and off.lines == 1

    twice = accessed.parse(record([(T0 + 3, "natives/STM/a.user.3"), (T0 + 4, "natives/STM/b.user.3"),
                                   (T0 + 8, "natives/STM/a.user.3")]))  # another thread asks again
    assert twice.paths["natives/STM/a.user.3"] == pytest.approx(T0 + 3) and len(twice.paths) == 2


def test_more_paths_than_kept(monkeypatch):
    monkeypatch.setattr(accessed, "MAX_PATHS", 5)
    log = accessed.parse(record([(T0 + i, f"natives/STM/f{i}.tex.1") for i in range(8)]))
    assert len(log.paths) == 5 and log.truncated and log.lines == 9


def test_a_compressed_copy_and_a_huge_file_read_from_its_end(tmp_path):
    text = record([(T0 + 9, "natives/STM/a/b.user.3"), (T0 + 20, "natives/STM/a/c.user.3")])
    gz = tmp_path / accessed.COPY_NAME
    with gzip.open(gz, "wt", encoding="utf-8", newline="") as f:
        f.write(text)
    assert accessed.read(gz) == accessed.parse(text)
    gz.write_bytes(gz.read_bytes()[:30])  # a damaged copy: nothing, never an error
    assert accessed.read(gz) == accessed.AccessLog()
    assert accessed.read(tmp_path / "missing.txt") == accessed.AccessLog()

    # 70 MB: the first line dates it, the last 64 MB are read
    big = tmp_path / accessed.ACCESSED_FILE
    filler = ("x" * (1024 * 1024 - 1) + "\n").encode()
    with open(big, "wb") as f:
        f.write(record([(T0 + 1 + i * 0.001, f"natives/STM/early/f{i:04d}.tex.1") for i in range(500)]).encode())
        for _ in range(70):
            f.write(filler)
        f.write(record([(T0 + 900, "natives/STM/late/a.tex.1"), (T0 + 901, "natives/STM/late/b.tex.1")],
                       constructed=None).encode())
    assert big.stat().st_size > 70 * 1024 * 1024
    log = accessed.read(big)
    assert log.truncated and log.started == T0
    assert list(log.paths) == ["natives/STM/late/a.tex.1", "natives/STM/late/b.tex.1"]
    assert len(log.other) == accessed.MAX_OTHER and all(len(x) <= 300 for x in log.other)


def test_which_session_a_record_belongs_to():
    log = accessed.parse(record([(T0 + 9, "natives/STM/a.tex.1")]))
    assert accessed.belongs(log, T0 + 5) and not accessed.belongs(log, T0 + 5.01)
    assert accessed.belongs(log, T0 - 180) and not accessed.belongs(log, T0 - 180.01)
    assert not accessed.belongs(log, None) and not accessed.belongs(accessed.AccessLog(), T0)


def test_the_pak_hash_is_matfix_s():
    rng = random.Random(4)
    names = ["natives/stm/art/model/character/ch03/004/001/2/ch03_004_0012.mesh.241111606", "a", "ab", "abc", "/",
             "natives/stm/gui/한글/아이콘.tex.241106027", "natives/stm/x/" + "y" * 101 + ".tex.1"]
    names += ["/".join("".join(rng.choice("abc_/0") for _ in range(rng.randint(1, 9))) for _ in range(rng.randint(1, 6)))
              for _ in range(300)]
    hasher = accessed._Hasher()
    for name in names + names:  # (twice: from the kept folder states too)
        assert hasher(name) == string_hash(name), name


# ---- mapping requests to mods --------------------------------------------------------------------------
LOOSE_MESH = "natives/STM/Art/Model/armor/a.mesh.241111606"
LOOSE_TEX = "natives/STM/Art/Model/armor/a_ALBD.tex.241106027"
SHARED = "natives/STM/Art/Model/weapon/w.mesh.241111606"
ONLY_B = "natives/STM/Art/Model/weapon/w_ALBD.tex.241106027"
FREE = "natives/STM/Stage/st101/rock.mesh.241111606"


@pytest.fixture
def game(tmp_path) -> Path:
    game = tmp_path / "MonsterHunterWilds"
    write(game / "MonsterHunterWilds.exe", "exe")
    write(game / "re_chunk_000.pak.sub_000.pak.patch_001.pak", "official")
    return game


def pak(game: Path, name: str, paths) -> str:
    rel = f"pak_mods/{name}"
    (game / "pak_mods").mkdir(parents=True, exist_ok=True)
    write_pak([(path_hash(p), b"x", 1, 0) for p in paths], game / rel)
    return rel


def setup_index(game: Path) -> ModIndex:
    """Loose mod L (a mesh, a texture), pak mods A and B (B's patch number is higher, both have SHARED), a pak no
    mod placed with FREE and SHARED (the highest number), the script mod S and pak mod Q (never asked for)."""
    write(game / LOOSE_MESH, "m")
    write(game / LOOSE_TEX, "t")
    deployed = {LOOSE_MESH.lower(): {"path": LOOSE_MESH, "mod": "L"}, LOOSE_TEX.lower(): {"path": LOOSE_TEX, "mod": "L"},
                "reframework/autorun/s.lua": {"path": "reframework/autorun/s.lua", "mod": "S"}}
    for mod, name, paths in (("A", "001_A.pak", [SHARED, LOOSE_MESH]), ("B", "002_B.pak", [SHARED, ONLY_B]),
                             ("Q", "003_Q.pak", ["natives/STM/never/q.mesh.241111606"])):
        rel = pak(game, name, paths)
        deployed[rel.lower()] = {"path": rel, "mod": mod, "pak": True}
    pak(game, "zz_by_hand.pak", [FREE])
    mods = [ModRef("L", "Loose", files=[LOOSE_MESH, LOOSE_TEX]), ModRef("A", "Pak A", files=["a.pak"]),
            ModRef("B", "Pak B", files=["b.pak"]), ModRef("Q", "Pak Q", files=["q.pak"]),
            ModRef("S", "Script", files=["reframework/autorun/s.lua"])]
    return ModIndex(game, deployed, mods)


ALL = ["L", "A", "B", "Q", "S"]
CONTENT = ["L", "A", "B", "Q"]
QMESH = "natives/STM/never/q.mesh.241111606"
NAMES = {path_hash(p): p.lower() for p in (SHARED, LOOSE_MESH, ONLY_B, FREE, QMESH)}.get  # (accessed.PakNames)
LATER = (T0 + 400, "natives/STM/GUI/later.tex.241106027")  # (logging went on for minutes)


def test_a_loose_file_beats_any_pak_and_the_highest_pak_wins(game):
    index = setup_index(game)
    log = accessed.parse(record([(T0 + 70, LOOSE_MESH), (T0 + 71, SHARED), (T0 + 72, ONLY_B), (T0 + 73, FREE),
                                 (T0 + 74, "natives/STM/GUI/game_only.tex.241106027"), LATER]))
    found = accessed.exposure(log, index, ALL, CONTENT, names=NAMES)
    assert found.requested == {"L": 1, "B": 2} and found.files == 3  # (FREE: a pak no mod placed wins it)
    assert found.log and found.unexposed == ["A", "Q"] and found.note == ""
    # without the names of a pak's files (their types) pak mods are not judged
    assert accessed.exposure(log, index, ALL, CONTENT).unexposed == []
    # B off: A's copy of the shared file is the one read
    found = accessed.exposure(log, index, ["L", "A", "Q", "S"], CONTENT)
    assert found.requested == {"L": 1, "A": 1}
    # requests in another form (an absolute path, backslashes, no natives/STM/) still map
    other = accessed.parse(record([(T0 + 70, "D:\\Games\\MHWilds\\" + LOOSE_TEX.replace("/", "\\")),
                                   (T0 + 71, ONLY_B[len("natives/STM/"):])]))
    assert accessed.exposure(other, index, ALL, CONTENT).requested == {"L": 1, "B": 1}


def test_never_requested_only_where_the_record_can_tell(game):
    index = setup_index(game)
    loose_only = accessed.parse(record([(T0 + 70, LOOSE_MESH), (T0 + 71, LOOSE_TEX), LATER]))
    found = accessed.exposure(loose_only, index, ALL, CONTENT, names=NAMES)
    assert found.requested == {"L": 2} and found.unexposed == []  # no pak request mapped: pak mods are not judged
    nothing = accessed.parse(record([(T0 + 70, "natives/STM/GUI/game_only.tex.241106027")]))
    found = accessed.exposure(nothing, index, ALL, CONTENT)
    assert not found.log and found.unexposed == [] and found.note == accessed.NOTE_NO_HITS
    off = accessed.exposure(accessed.parse(record([])), index, ALL, CONTENT)
    assert not off.log and off.note == accessed.NOTE_OFF
    # a loose mod with a file type the record never shows (perhaps not logged) is not judged either
    write(game / "natives/STM/data/x.user.3", "u")
    index.mods["U"] = ModRef("U", "Data", files=["natives/STM/data/x.user.3"])
    mixed = accessed.parse(record([(T0 + 70, LOOSE_MESH), (T0 + 71, ONLY_B), (T0 + 400, SHARED)]))
    assert accessed.exposure(mixed, index, ALL + ["U"], CONTENT + ["U"], names=NAMES).unexposed == ["A", "Q"]
    seen = accessed.parse(record([(T0 + 70, LOOSE_MESH), (T0 + 71, ONLY_B), (T0 + 72, "natives/STM/y.user.3"),
                                  (T0 + 400, SHARED)]))
    assert accessed.exposure(seen, index, ALL + ["U"], CONTENT + ["U"], names=NAMES).unexposed == ["A", "Q", "U"]
    # the code mod S is never judged, even when listed as content by mistake
    assert "S" not in accessed.exposure(seen, index, ALL, ALL, names=NAMES).unexposed
    # a record of less than a minute (a freeze ended by hand, no crash detected) is too short to tell
    short = accessed.parse(record([(T0 + 70, LOOSE_MESH), (T0 + 71, ONLY_B), (T0 + 90, SHARED)]))
    found = accessed.exposure(short, index, ALL, CONTENT, names=NAMES)
    assert found.log and found.unexposed == [] and found.note == accessed.NOTE_COVER


def test_a_crash_soon_after_logging_started_and_the_last_seconds(game):
    index = setup_index(game)
    log = accessed.parse(record([(T0 + 5, LOOSE_MESH), (T0 + 30, ONLY_B), (T0 + 31, SHARED), (T0 + 400, FREE)]))
    early = accessed.exposure(log, index, ALL, CONTENT, crash=T0 + 40)
    assert early.log and early.unexposed == [] and early.note == accessed.NOTE_COVER
    assert early.tail == {"B": 2} and early.exclusive == "B"
    assert [(round(s, 1), mod) for s, _p, mod in early.tail_files] == [(9.0, "B"), (10.0, "B")]
    late = accessed.exposure(log, index, ALL, CONTENT, crash=T0 + 120, names=NAMES)
    assert late.unexposed == ["A", "Q"] and late.tail == {} and late.exclusive == ""
    # several mods in the last seconds: none is "the only one"
    both = accessed.exposure(accessed.parse(record([(T0 + 100, LOOSE_MESH), (T0 + 105, ONLY_B)])), index, ALL,
                             CONTENT, crash=T0 + 106)
    assert both.tail == {"L": 1, "B": 1} and both.exclusive == ""
    one = accessed.exposure(accessed.parse(record([(T0 + 100, LOOSE_MESH)])), index, ALL, CONTENT, crash=T0 + 101)
    assert one.tail == {"L": 1} and one.exclusive == ""  # (one file is not enough)
    # only the last seconds (the crash window's explain): the same tail, nothing else worked out
    quick = accessed.exposure(log, index, ALL, CONTENT, crash=T0 + 40, tail_only=True)
    assert (quick.tail, quick.exclusive, quick.tail_files) == (early.tail, early.exclusive, early.tail_files)
    assert quick.requested == {} and quick.unexposed == [] and not quick.log
    assert accessed.exposure(log, index, ALL, CONTENT, tail_only=True) == accessed.Exposure()


def test_a_truncated_record_gives_no_never_requested(game):
    index = setup_index(game)
    log = accessed.parse(record([(T0 + 70, LOOSE_MESH), (T0 + 71, ONLY_B), (T0 + 600, LOOSE_TEX)]))
    log.truncated = True
    found = accessed.exposure(log, index, ALL, CONTENT, crash=T0 + 605)
    assert not found.log and found.unexposed == [] and found.note == accessed.NOTE_TRUNCATED
    assert found.tail == {"L": 1}  # (the end is still there)


class FakeSession:
    id = "20261021-200000"

    def __init__(self, log, crashed_at=None, on=ALL):
        self.log_, self.crashed_at = log, crashed_at
        self.context = {"mods": [{"id": i, "name": i, "enabled": True} for i in on], "placed": list(on)}

    def accessed(self):
        return self.log_

    def problem(self):
        return "crash" if self.crashed_at else ""

    summary_extra: dict = {}

    @property
    def summary(self):
        return {**({"crash_time": self.crashed_at} if self.crashed_at else {}), **self.summary_extra}


def test_what_a_search_keeps_of_one_launch(game):
    index = setup_index(game)
    log = accessed.parse(record([(T0 + 70, LOOSE_MESH), (T0 + 600, ONLY_B), (T0 + 601, SHARED)]))
    crash = SimpleNamespace(exit=GameExit(7, 0xC0000005, T0 + 602, started=T0 - 2), crashed=True, normal=False,
                            record=None, session=FakeSession(log, T0 + 602))
    run = accessed.step_run(crash, ALL, index, CONTENT, NAMES)
    assert run == {"seconds": 604.0, "log": True, "unexposed": ["A", "Q"], "tail": ["B"],
                   "record": "20261021-200000", "auto": "crash"}
    normal = SimpleNamespace(exit=GameExit(8, 0, T0 + 1800, started=T0 - 2), crashed=False, normal=True, record=None,
                             session=FakeSession(log))
    run = accessed.step_run(normal, ["L", "B", "S"], index, CONTENT)
    assert run["auto"] == "normal" and run["seconds"] == 1802.0 and run["unexposed"] == [] and run["tail"] == []
    # a freeze 20 s after logging started, ended in the task manager (exit code 1: not a crash the manager saw), then
    # answered "crashed": a 20-second record says nothing of the mods not loaded yet
    frozen = accessed.parse(record([(T0 + 5, LOOSE_MESH), (T0 + 20, ONLY_B)]))
    killed = SimpleNamespace(exit=GameExit(9, 1, T0 + 25, started=T0 - 2), crashed=None, normal=False, record=None,
                             session=FakeSession(frozen))
    run = accessed.step_run(killed, ALL, index, CONTENT, NAMES)
    assert run["auto"] == "" and run["log"] and run["unexposed"] == []
    # not watched, no record, or a record that cannot be read: what is known, never an error
    assert accessed.step_run(SimpleNamespace(exit=None, crashed=None, normal=False, record=None, session=None),
                             ALL, index, CONTENT) == {"seconds": None, "log": False, "unexposed": [], "tail": [],
                                                      "record": "", "auto": ""}

    class Broken(FakeSession):
        def accessed(self):
            raise RuntimeError("damaged")

    run = accessed.step_run(SimpleNamespace(exit=None, crashed=True, normal=False, record=None,
                                            session=Broken(log)), ALL, index, CONTENT)
    assert run["auto"] == "crash" and run["record"] == "20261021-200000" and not run["log"]


# ---- consent and restore --------------------------------------------------------------------------------
@pytest.fixture
def refw_game(game) -> Path:
    write(game / "dinput8.dll", "MZ")
    return game


def config(game: Path, text: str) -> Path:
    return write(game / crashlog.CONFIG_FILE, text)


def test_when_the_record_can_be_turned_on(game, refw_game):
    assert accessed.logging_offer(None).text == accessed.LOG_NO_REFW
    (game / "dinput8.dll").unlink()
    assert not accessed.logging_offer(game).available
    write(game / "dinput8.dll", "MZ")
    path = str(game / crashlog.CONFIG_FILE)
    # loose file loading on: only the record
    config(game, "LooseFileLoader_Enabled=true\r\n")
    offer = accessed.logging_offer(game, appdata="")
    assert (offer.available, offer.already, offer.changes, offer.text, offer.path) == (
        True, False, {LOG_KEY: "true"}, accessed.LOG_NOTE, path)
    assert accessed.logging_offer(game, appdata="", running=True) == accessed.Offer(False, False, accessed.LOG_RUNNING,
                                                                                    {}, path)
    # on already (also while the game runs: nothing to change)
    config(game, "LooseFileLoader_Enabled=true\r\nLooseFileLoader_LogAccessedFiles=true\r\n")
    for running in (False, True):
        offer = accessed.logging_offer(game, appdata="", running=running)
        assert offer.available and offer.already and offer.changes == {} and offer.text == accessed.LOG_ALREADY
    # loose file loading off and natives\ empty: both
    config(game, "LooseFileLoader_Enabled=false\r\n")
    offer = accessed.logging_offer(game, appdata="")
    assert offer.changes == {LOOSE_KEY: "true", LOG_KEY: "true"} and offer.text == accessed.LOG_NOTE + accessed.LOG_NOTE_LOOSE
    (game / "natives" / "STM" / "empty").mkdir(parents=True)
    assert accessed.logging_offer(game, appdata="").available  # (folders without files)
    # ... but not when the search may apply loose mods, or natives\ holds a file
    assert accessed.logging_offer(game, appdata="", loose_mods=True).text == accessed.LOG_UNAVAILABLE_LOOSE
    write(game / "natives" / "STM" / "empty" / "x.tex.241106027", "t")
    offer = accessed.logging_offer(game, appdata="")
    assert not offer.available and offer.text == accessed.LOG_UNAVAILABLE_LOOSE and offer.changes == {}


def setter(game):
    return lambda key, value, path=None: crashes.set_refw_option(game, key, value, appdata="", path=path)


def test_turning_it_on_and_back_off(refw_game):
    game = refw_game
    path = config(game, "ScriptRunner_LogToDisk=true\r\nLooseFileLoader_Enabled=false\r\nFont_Size=16\r\n")
    offer = accessed.logging_offer(game, appdata="")
    restore = accessed.enable_logging(setter(game), offer)
    assert restore == {"path": str(path), "values": {LOOSE_KEY: "false", LOG_KEY: None}}
    assert crashlog.parse_config(path.read_text()) == {"ScriptRunner_LogToDisk": "true", LOOSE_KEY: "true",
                                                       "Font_Size": "16", LOG_KEY: "true"}
    accessed.restore_logging(setter(game), restore)
    assert path.read_bytes() == (b"ScriptRunner_LogToDisk=true\r\nLooseFileLoader_Enabled=false\r\nFont_Size=16\r\n"
                                 b"LooseFileLoader_LogAccessedFiles=false\r\n")  # (a missing key comes back "false")
    assert accessed.enable_logging(setter(game), accessed.Offer(True, True, accessed.LOG_ALREADY)) is None
    # a failure on the way undoes what was changed
    calls = []

    def failing(key, value, path=None):
        calls.append((key, value))
        if key == LOG_KEY and value == "true":
            raise OSError("read-only")
        return setter(game)(key, value, path)

    with pytest.raises(OSError):
        accessed.enable_logging(failing, accessed.logging_offer(game, appdata=""))
    assert crashlog.parse_config(path.read_text())[LOOSE_KEY] == "false"
    assert calls == [(LOOSE_KEY, "true"), (LOG_KEY, "true"), (LOOSE_KEY, "false")]


def test_turned_on_in_the_crash_window_and_off_there_again(refw_game, tmp_path):
    # what the 게임 튕김 기록 window turned on is put back when it is turned off there: loose file loading too, unless
    # the game reads loose files now (then mods with natives files would stop working)
    game, data = refw_game, tmp_path / "data"
    data.mkdir()
    path = config(game, "Font_Size=16\r\n")
    restore = accessed.enable_logging(setter(game), accessed.logging_offer(game, appdata=""))
    accessed.keep_enabled(data, restore)
    assert accessed.enabled_here(data) == {"path": str(path), "values": {LOOSE_KEY: None, LOG_KEY: None}}
    assert not accessed.disable_logging(setter(game), data, str(path), loose_mods=False, game_dir=game)
    assert crashlog.parse_config(path.read_text()) == {"Font_Size": "16", LOOSE_KEY: "false", LOG_KEY: "false"}
    assert accessed.enabled_here(data) is None
    accessed.keep_enabled(data, accessed.enable_logging(setter(game), accessed.logging_offer(game, appdata="")))
    write(game / "natives" / "STM" / "a.tex.241106027", "t")  # (placed since)
    assert accessed.disable_logging(setter(game), data, str(path), loose_mods=False, game_dir=game)
    assert crashlog.parse_config(path.read_text()) == {"Font_Size": "16", LOOSE_KEY: "true", LOG_KEY: "false"}
    # on before (by hand, not here): only the record goes off
    config(game, "LooseFileLoader_Enabled=true\r\nLooseFileLoader_LogAccessedFiles=true\r\n")
    assert not accessed.disable_logging(setter(game), data, str(path), loose_mods=False, game_dir=game)
    assert crashlog.parse_config(path.read_text()) == {LOOSE_KEY: "true", LOG_KEY: "false"}
    # a search's pending restore taken over here: its older values win
    accessed.keep_enabled(data, {"path": str(path), "values": {LOG_KEY: "false"}},
                          {"path": str(path), "values": {LOG_KEY: None, LOOSE_KEY: None}})
    assert accessed.enabled_here(data)["values"] == {LOG_KEY: None, LOOSE_KEY: None}
    accessed.keep_enabled(data, None)  # (nothing changed now: what was kept stays)
    assert accessed.enabled_here(data)["values"] == {LOG_KEY: None, LOOSE_KEY: None}


def test_a_restore_waits_for_the_game_to_close(manager, game_dir, monkeypatch):
    import mhwmm.manager

    write(game_dir / "dinput8.dll", "MZ")
    path = config(game_dir, "LooseFileLoader_Enabled=true\r\n")
    restore = accessed.enable_logging(manager.set_refw_option, accessed.logging_offer(game_dir, appdata=""))
    assert restore == {"path": str(path), "values": {LOG_KEY: None}}
    running = [True]
    monkeypatch.setattr(mhwmm.manager, "is_game_running", lambda: running[0])
    assert accessed.finish_restore(manager, restore) == accessed.RESTORE_PENDING
    assert (manager.data_dir / accessed.RESTORE_FILE).is_file() and accessed.pending(manager.data_dir) == restore
    assert crashlog.parse_config(path.read_text())[LOG_KEY] == "true"
    assert accessed.apply_pending(manager) == ""  # (still running)
    running[0] = False
    assert accessed.apply_pending(manager, searching=True) == ""  # (a new search found it on and keeps it)
    assert accessed.apply_pending(manager) == accessed.RESTORED
    assert crashlog.parse_config(path.read_text()) == {LOOSE_KEY: "true", LOG_KEY: "false"}
    assert not (manager.data_dir / accessed.RESTORE_FILE).exists() and accessed.apply_pending(manager) == ""
    # with the game closed it is done at once; nothing to restore says nothing
    restore = accessed.enable_logging(manager.set_refw_option, accessed.logging_offer(game_dir, appdata=""))
    assert accessed.finish_restore(manager, restore) == accessed.RESTORED
    assert crashlog.parse_config(path.read_text())[LOG_KEY] == "false"
    assert accessed.finish_restore(manager, None) == "" and accessed.finish_restore(manager, {"values": {}}) == ""
    # a pending restore the user no longer wants (turned on for good)
    accessed._keep_pending(manager.data_dir, {"path": str(path), "values": {LOG_KEY: None}})
    accessed.drop_pending(manager.data_dir)
    assert accessed.pending(manager.data_dir) is None


def test_a_pak_is_judged_only_by_file_types_the_record_shows(game):
    # texture requests may not reach the record (LooseTextureLoader off, or its hooks failed after an update): a
    # texture-only pak is not "never requested" when the record has no texture at all - like a loose texture mod
    index = setup_index(game)
    tex = "natives/STM/Art/Model/armor/t_ALBD.tex.241106027"
    rel = pak(game, "004_T.pak", [tex])
    index.deployed[rel.lower()] = {"path": rel, "mod": "T", "pak": True}
    index._paks = None
    index.mods["T"] = ModRef("T", "Pak T", files=["t.pak"])
    names = {path_hash(tex): tex.lower(), **{path_hash(p): p.lower() for p in (SHARED, LOOSE_MESH, ONLY_B, QMESH)}}.get
    meshes = accessed.parse(record([(T0 + 70, LOOSE_MESH), (T0 + 71, SHARED), (T0 + 400, FREE)]))
    found = accessed.exposure(meshes, index, ALL + ["T"], CONTENT + ["T"], names=names)
    assert found.log and "T" not in found.unexposed and found.unexposed == ["A", "Q"]
    textures = accessed.parse(record([(T0 + 70, LOOSE_MESH), (T0 + 71, SHARED), (T0 + 72, "natives/STM/GUI/x.tex.1"),
                                      (T0 + 400, FREE)]))
    assert accessed.exposure(textures, index, ALL + ["T"], CONTENT + ["T"], names=names).unexposed == ["A", "Q", "T"]
    # an entry no name is known for: its types are not known, so it is not judged
    unknown = {k: v for k, v in ((path_hash(p), p.lower()) for p in (SHARED, LOOSE_MESH, ONLY_B))}.get
    assert accessed.exposure(textures, index, ALL + ["T"], CONTENT + ["T"], names=unknown).unexposed == ["A"]
    # the streaming copy of a texture is a type of its own
    assert accessed._type("natives/STM/streaming/Art/x.tex.241106027") == "streaming/tex"
    assert accessed._type("natives/STM/Art/x.tex.241106027") == "tex" and accessed._type(QMESH) == "mesh"


def test_loose_requests_map_by_the_exact_path(game):
    # LooseFileLoader serves only a file that is there: the same folder (streaming or not) and version suffix
    old = "natives/STM/Art/Model/armor/x.mesh.230411001"
    base, stream = "natives/STM/Art/tex/y.tex.241106027", "natives/STM/streaming/Art/tex/y.tex.241106027"
    for rel in (old, base, stream):
        write(game / rel, "f")
    deployed = {old.lower(): {"path": old, "mod": "X"}, stream.lower(): {"path": stream, "mod": "A"},
                base.lower(): {"path": base, "mod": "B"}}
    index = ModIndex(game, deployed, [ModRef("X", "Old X", files=[old]), ModRef("A", "Streaming A", files=[stream]),
                                      ModRef("B", "Base B", files=[base])])
    t = T0 + 300
    log = accessed.parse(record([(T0 + 70, "natives/STM/Art/Model/armor/x.mesh.241111606"),
                                 (t - 2, "natives/STM/streaming/Art/Model/armor/x.mesh.241111606"),
                                 (t - 1, stream), (t - 0.5, base)]))
    found = accessed.exposure(log, index, ["X", "A", "B"], ["X", "A", "B"], crash=t)
    # the game asked for another version of X's mesh: not X's file (never read, so X is not "the only one")
    assert found.requested == {"A": 1, "B": 1} and found.tail == {"A": 1, "B": 1} and found.exclusive == ""
    assert found.unexposed == ["X"]  # (meshes were asked for: X's version never was)


def test_a_crash_time_in_whole_seconds_keeps_its_own_second(game):
    # the game's own crash report: the minidump's time in whole seconds; a request 0.4 s into that second is part
    # of what led to the crash, and "never requested" is judged over the whole record
    index = setup_index(game)
    crash = float(int(T0 + 301))  # (not on a whole minute: a minidump's time)
    log = accessed.parse(record([(T0 + 70, LOOSE_MESH), (T0 + 100, SHARED), (crash + 0.4, ONLY_B)]))
    session = FakeSession(log, crash)
    session.summary_extra = {"crash_from": "report"}
    found = accessed.session_exposure(session, index, CONTENT, names=NAMES)
    assert found.tail == {"B": 1} and "B" not in found.unexposed and found.unexposed == ["A", "Q"]
    assert accessed.crash_slack(session) == accessed.REPORT_SLACK
    minute = FakeSession(log, float(int(T0 // 60 * 60)) + 300)
    minute.summary_extra = {"crash_from": "report"}
    assert accessed.crash_slack(minute) == accessed.REPORT_SLACK_MINUTE
    assert accessed.crash_slack(FakeSession(log, crash)) == 0.0  # (REFramework's handler: milliseconds)


def test_paks_reframework_did_not_load_are_not_what_the_game_read(game):
    # REFramework's log of the session says a pak_mods pak never reached the game (pak loading off, none injected -
    # an outdated REFramework after a game update -, the game refused it, its slot was taken, it was not among those
    # injected): the game read its own files for those paths, so they are not that mod's, nor is it judged
    index = setup_index(game)
    crash = T0 + 600
    log = accessed.parse(record([(T0 + 70, LOOSE_MESH), (crash - 4, SHARED), (crash - 3, ONLY_B), LATER]))
    full = accessed.exposure(log, index, ALL, CONTENT, crash=crash, names=NAMES)
    assert full.tail == {"B": 2} and full.exclusive == "B" and full.unexposed == ["A", "Q"]
    assert accessed.exposure(log, index, ALL, CONTENT, crash=crash, names=NAMES, refw=crashlog.LogInfo()) == full
    for info in (crashlog.LogInfo(paks_disabled=True), crashlog.LogInfo(paks_none=3)):
        found = accessed.exposure(log, index, ALL, CONTENT, crash=crash, names=NAMES, refw=info)
        assert found.requested == {"L": 1} and found.tail == {} and found.exclusive == "" and found.unexposed == []
    # B alone not loaded: A's copy of the shared file is the one read, and B is not judged
    pak_b = str(game / "pak_mods" / "002_B.pak").replace("/", "\\")
    for info in (crashlog.LogInfo(paks_failed=[pak_b]), crashlog.LogInfo(paks_slot_taken=["002_B.pak"]),
                 crashlog.LogInfo(paks_injected={"re_chunk_000.pak.sub_000.pak.patch_002.pak":
                                                 str(game / "pak_mods" / "001_A.pak"),
                                                 "re_chunk_000.pak.sub_000.pak.patch_003.pak":
                                                 str(game / "pak_mods" / "003_Q.pak")})):
        found = accessed.exposure(log, index, ALL, CONTENT, crash=crash, names=NAMES, refw=info)
        assert found.requested == {"L": 1, "A": 1} and found.tail == {"A": 1} and found.unexposed == ["Q"], info
    # a kept session and a launch of the search read the session's own REFramework log

    class Logged(FakeSession):
        def log(self):
            return crashlog.LogInfo(paks_disabled=True)

    session = Logged(log, crash)
    found = accessed.session_exposure(session, index, CONTENT, names=NAMES)
    assert found.tail == {} and found.unexposed == []
    run = accessed.step_run(SimpleNamespace(exit=GameExit(7, 0xC0000005, crash, started=T0 - 2), crashed=True,
                                            normal=False, record=session, session=session), ALL, index, CONTENT, NAMES)
    assert run["tail"] == [] and run["unexposed"] == [] and run["log"]
