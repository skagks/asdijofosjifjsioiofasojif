"""TypeRegistry.hold()/unhold(): 모드 수정 keeps the type registry loaded while the background
material check load()s and release()s it on its own thread, and it is never loaded twice at once."""

import json
import lzma
import threading
import time

import pytest

from mhwmm.matfix import rsz


@pytest.fixture
def opened(tmp_path, monkeypatch):
    """A small registry file in place of the bundled one; the list of times it was opened (each
    open is slow enough that a second thread arrives while the first one is still loading)."""
    path = tmp_path / rsz.REGISTRY_NAME
    with lzma.open(path, "wt", encoding="utf-8") as f:
        json.dump({"meta": {"reasy_date": "2026-01-01"},
                   "types": {"a1": ["1", "app.Test", "[]"]}, "empty": {}, "alt": {}, "old": {}}, f)
    calls = []
    real_open = lzma.open

    def counting(file, *args, **kwargs):
        calls.append(file)
        time.sleep(0.05)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(lzma, "open", counting)
    monkeypatch.setattr(rsz.TypeRegistry, "path", classmethod(lambda cls: path))
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", None)
    monkeypatch.setattr(rsz.TypeRegistry, "_from_file", False)
    monkeypatch.setattr(rsz.TypeRegistry, "_holds", 0)
    monkeypatch.setattr(rsz.TypeRegistry, "_source", None)
    return calls


def _together(*funcs):
    """Run the functions on their own threads, started at the same moment; their results."""
    start = threading.Barrier(len(funcs))
    results = [None] * len(funcs)

    def run(i, func):
        start.wait()
        results[i] = func()

    threads = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(funcs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return results


def test_two_threads_holding_at_once_load_it_once(opened):
    a, b = _together(rsz.TypeRegistry.hold, rsz.TypeRegistry.hold)
    assert len(opened) == 1
    assert a is b and a.name(0xA1) == "app.Test"
    assert rsz.TypeRegistry._holds == 2
    rsz.TypeRegistry.unhold()
    rsz.TypeRegistry.unhold()
    assert rsz.TypeRegistry._loaded is None and rsz.TypeRegistry._holds == 0


def test_load_and_hold_at_once_load_it_once(opened):
    a, b = _together(rsz.TypeRegistry.load, rsz.TypeRegistry.hold)
    assert len(opened) == 1 and a is b


def test_release_is_a_noop_while_held(opened):
    held = rsz.TypeRegistry.hold()
    assert rsz.TypeRegistry.load() is held  # the background check gets the same one
    rsz.TypeRegistry.release()  # ... and its release() leaves it for the window
    assert rsz.TypeRegistry._loaded is held
    assert rsz.TypeRegistry.load() is held and len(opened) == 1
    rsz.TypeRegistry.unhold()
    assert rsz.TypeRegistry._loaded is None


def test_unhold_to_zero_releases(opened):
    first = rsz.TypeRegistry.hold()
    rsz.TypeRegistry.hold()
    rsz.TypeRegistry.unhold()
    assert rsz.TypeRegistry._loaded is first and rsz.TypeRegistry._holds == 1
    rsz.TypeRegistry.unhold()
    assert rsz.TypeRegistry._loaded is None and not rsz.TypeRegistry._from_file
    again = rsz.TypeRegistry.hold()  # loaded again from the file
    assert again is not first and len(opened) == 2
    rsz.TypeRegistry.unhold()


def test_unhold_without_hold_does_nothing(opened):
    loaded = rsz.TypeRegistry.load()
    rsz.TypeRegistry.unhold()
    assert rsz.TypeRegistry._loaded is loaded and rsz.TypeRegistry._holds == 0
    rsz.TypeRegistry.release()
    assert rsz.TypeRegistry._loaded is None


def test_plain_load_and_release_unchanged(opened):
    a = rsz.TypeRegistry.load()
    assert rsz.TypeRegistry.load() is a and len(opened) == 1
    rsz.TypeRegistry.release()
    assert rsz.TypeRegistry._loaded is None
    b = rsz.TypeRegistry.load()
    assert b is not a and len(opened) == 2
    rsz.TypeRegistry.release()


def test_a_registry_not_loaded_from_file_stays(opened, monkeypatch):
    """Tests put their own registry in place (_from_file False): neither release() nor the last
    unhold() drops it."""
    own = rsz.TypeRegistry({"types": {}})
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", own)
    assert rsz.TypeRegistry.hold() is own
    rsz.TypeRegistry.unhold()
    rsz.TypeRegistry.release()
    assert rsz.TypeRegistry._loaded is own and not opened


def test_reload_drops_it_even_while_held(opened, tmp_path, monkeypatch):
    """After a download the next load reads the new file although 모드 수정 holds the old registry (release()
    would leave it); holders keep the one they have, and date_in_use() says which one load() gives."""
    held = rsz.TypeRegistry.hold()
    assert rsz.TypeRegistry.date_in_use() == "2026-01-01"
    newer = tmp_path / "updates" / rsz.REGISTRY_NAME
    newer.parent.mkdir()
    with lzma.open(newer, "wt", encoding="utf-8") as f:
        json.dump({"meta": {"reasy_date": "2099-01-01"}, "types": {"a1": ["2", "app.Test", "[]"]}, "empty": {},
                   "alt": {}, "old": {}}, f)
    monkeypatch.setattr(rsz.TypeRegistry, "path", classmethod(lambda cls: newer))
    assert rsz.TypeRegistry.date_in_use() == "2026-01-01"  # (the held one is what load() gives yet)
    rsz.TypeRegistry.release()
    assert rsz.TypeRegistry.load() is held
    rsz.TypeRegistry.reload()
    fresh = rsz.TypeRegistry.load()
    assert fresh is not held and fresh.crc(0xA1) == 2 and held.crc(0xA1) == 1
    assert rsz.TypeRegistry.date_in_use() == "2099-01-01" and rsz.TypeRegistry._holds == 1
    rsz.TypeRegistry.unhold()  # (the window closes: the last hold drops what is loaded)
    assert rsz.TypeRegistry._loaded is None and rsz.TypeRegistry._holds == 0
    assert rsz.TypeRegistry.date_in_use() == "2099-01-01"


def test_reload_keeps_a_registry_not_loaded_from_file(opened, monkeypatch):
    own = rsz.TypeRegistry({"types": {}})
    monkeypatch.setattr(rsz.TypeRegistry, "_loaded", own)
    rsz.TypeRegistry.reload()  # (tests put their own in place: like release(), reload() leaves it)
    assert rsz.TypeRegistry._loaded is own and rsz.TypeRegistry.load() is own
    assert rsz.TypeRegistry.date_in_use() == "2026-01-01"  # (the file's: the registry has no file of its own)
