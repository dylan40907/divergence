"""Owned Windows job lifecycle. Also used as an isolated, stdlib-only launch worker.

The worker joins the parent's job before starting application code, then closes
its inherited job handle. The parent keeps the sole lifetime handle. No breakaway
flags are enabled. This controls process lifetime, not filesystem/network access.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes as w
from pathlib import Path


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", w.DWORD),
        ("min_working_set", ctypes.c_size_t),
        ("max_working_set", ctypes.c_size_t),
        ("active_limit", w.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", w.DWORD),
        ("scheduling", w.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io_counters", ctypes.c_ulonglong * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class Accounting(ctypes.Structure):
    _fields_ = [
        ("times", ctypes.c_longlong * 4),
        ("page_faults", w.DWORD),
        ("total_processes", w.DWORD),
        ("active_processes", w.DWORD),
        ("terminated_processes", w.DWORD),
    ]


def kernel_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([w.LPVOID, w.LPCWSTR], w.HANDLE),
        "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL),
        "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
        "GetCurrentProcess": ([], w.HANDLE),
        "DuplicateHandle": (
            [w.HANDLE, w.HANDLE, w.HANDLE, w.LPVOID, w.DWORD, w.BOOL, w.DWORD],
            w.BOOL,
        ),
        "CloseHandle": ([w.HANDLE], w.BOOL),
        "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
        "QueryInformationJobObject": (
            [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPVOID],
            w.BOOL,
        ),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


def checked(success):
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())
    return success


class WindowsJob:
    def __init__(self):
        self.api = kernel_api()
        self.handle = checked(self.api.CreateJobObjectW(None, None))
        try:
            limits = ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            checked(
                self.api.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                )
            )
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            checked(self.api.CloseHandle(handle))

    def launch(self, argv: list[str], **kwargs) -> subprocess.Popen:
        inherited = w.HANDLE()
        current = self.api.GetCurrentProcess()
        checked(
            self.api.DuplicateHandle(
                current, self.handle, current, ctypes.byref(inherited), 0, True, 2
            )
        )
        try:
            startup = subprocess.STARTUPINFO(lpAttributeList={"handle_list": [inherited.value]})
            # Avoid the venv redirector so the returned process handle belongs to
            # our worker. The worker only imports stdlib and uses an absolute path.
            interpreter = getattr(sys, "_base_executable", sys.executable)
            return subprocess.Popen(
                [
                    interpreter,
                    "-I",
                    "-S",
                    str(Path(__file__).resolve()),
                    str(inherited.value),
                    *argv,
                ],
                startupinfo=startup,
                close_fds=True,
                **kwargs,
            )
        finally:
            checked(self.api.CloseHandle(inherited))

    def active_count(self) -> int:
        info = Accounting()
        checked(
            self.api.QueryInformationJobObject(
                self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
            )
        )
        return info.active_processes

    def wait_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.active_count():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def terminate(self):
        checked(self.api.TerminateJobObject(self.handle, 1))
        if not self.wait_empty(5):
            raise OSError("Windows job cleanup did not finish within five seconds")


def worker(argv: list[str]) -> int:
    api = kernel_api()
    handle = int(argv[0])
    try:
        checked(api.AssignProcessToJobObject(handle, api.GetCurrentProcess()))
    finally:
        checked(api.CloseHandle(handle))
    # The lifetime handle is no longer inherited by the actual application.
    process = subprocess.Popen(
        argv[1:],
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
    return process.wait()


if __name__ == "__main__":
    try:
        result = worker(sys.argv[1:])
    except Exception as exc:
        print(f"Divergence process supervisor failed: {exc}", file=sys.stderr, flush=True)
        result = 125
    sys.exit(result)
