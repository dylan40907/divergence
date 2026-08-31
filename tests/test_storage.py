import ctypes
import json
import os
import threading
import time
from ctypes import wintypes as w

import pytest

from divergence.storage import atomic_json


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing semantics")
@pytest.mark.parametrize("release_lock", [True, False])
def test_atomic_manifest_handles_transient_and_persistent_windows_readers(
    tmp_path, monkeypatch, release_lock
):
    path = tmp_path / "run.json"
    atomic_json(path, {"status": "previous complete result"})
    original = path.read_bytes()
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
    api.CreateFileW.restype = w.HANDLE
    api.CloseHandle.argtypes = [w.HANDLE]
    api.CloseHandle.restype = w.BOOL
    # Read/write sharing without FILE_SHARE_DELETE prevents replacing this file.
    handle = api.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0, None)
    assert handle != w.HANDLE(-1).value, ctypes.WinError(ctypes.get_last_error())
    blocked = threading.Event()
    closed = threading.Event()
    replace = os.replace
    observed_denials = []

    def observe_replace(source, destination):
        try:
            replace(source, destination)
        except PermissionError as exc:
            observed_denials.append(exc.winerror)
            assert path.read_bytes() == original, "the old manifest must remain intact"
            blocked.set()
            raise

    def reader():
        if blocked.wait(5):
            time.sleep(0.05)
        api.CloseHandle(handle)
        closed.set()

    worker = threading.Thread(target=reader) if release_lock else None
    monkeypatch.setattr(os, "replace", observe_replace)
    if worker:
        worker.start()
    started = time.monotonic()
    try:
        if release_lock:
            atomic_json(path, {"status": "new complete result"})
            assert json.loads(path.read_text())["status"] == "new complete result"
        else:
            with pytest.raises(PermissionError):
                atomic_json(path, {"status": "must not overwrite"})
            assert path.read_bytes() == original
        assert observed_denials, "the reader must actually block an atomic replacement"
        assert time.monotonic() - started < 4, "permanent denial must not block indefinitely"
        assert not list(tmp_path.glob(".run.json-*"))
    finally:
        if worker:
            worker.join(timeout=6)
        if not closed.is_set():
            api.CloseHandle(handle)


def test_unrelated_replace_failure_is_not_retried_or_written_in_place(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    atomic_json(path, {"preserve": True})
    original = path.read_bytes()
    calls = 0

    def denied(*args):
        nonlocal calls
        calls += 1
        raise OSError("disk unavailable")

    monkeypatch.setattr(os, "replace", denied)
    with pytest.raises(OSError, match="disk unavailable"):
        atomic_json(path, {"new": True})
    assert calls == 1
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".run.json-*"))


def test_invalid_json_cannot_replace_a_complete_manifest(tmp_path):
    path = tmp_path / "run.json"
    atomic_json(path, {"preserve": True})
    original = path.read_bytes()
    with pytest.raises(ValueError):
        atomic_json(path, {"invalid": float("nan")})
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".run.json-*"))
