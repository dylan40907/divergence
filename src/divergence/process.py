"""Bounded, non-shell execution. Checkout isolation is not a security sandbox."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class ExecutionError(RuntimeError):
    pass


def stop_process(process: subprocess.Popen) -> None:
    if os.name == "nt":
        # taskkill /T terminates children as well as the launcher.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def run_command(
    argv: list[str],
    cwd: Path,
    timeout: int,
    log: Path,
    extra_env: dict[str, str] | None = None,
) -> None:
    if not argv:
        raise ExecutionError("empty command")
    argv = [sys.executable if arg == "{python}" else arg for arg in argv]
    environment = os.environ.copy()
    # Do not inject the caller's Python project or interactive Git overrides into the child.
    for key in list(environment):
        if key.startswith("GIT_") or key in {"PYTHONPATH", "PYTHONHOME"}:
            environment.pop(key)
    environment.update(extra_env or {})
    environment["PYTHONUTF8"] = "1"
    log.parent.mkdir(parents=True, exist_ok=True)
    flags = (
        subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )
    with log.open("wb") as output:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                shell=False,
                creationflags=flags,
                start_new_session=os.name != "nt",
            )
        except OSError as exc:
            raise ExecutionError(f"could not start {argv[0]}: {exc}") from exc
        try:
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                if log.stat().st_size > 16 * 1024 * 1024:
                    stop_process(process)
                    raise ExecutionError(f"command log exceeded 16 MiB; see {log.name}")
                try:
                    code = process.wait(timeout=min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except subprocess.TimeoutExpired as exc:
            stop_process(process)
            raise ExecutionError(f"command timed out after {timeout}s; see {log.name}") from exc
        except BaseException:
            if process.poll() is None:
                stop_process(process)
            raise
    if code:
        raise ExecutionError(f"command exited {code}; see {log.name}")
    if log.stat().st_size > 16 * 1024 * 1024:
        raise ExecutionError(f"command log exceeded 16 MiB; see {log.name}")
