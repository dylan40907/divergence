"""Bounded, non-shell execution. Checkout isolation is not a security sandbox."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from divergence.artifacts import MIB, inspect_artifacts


class ExecutionError(RuntimeError):
    pass


def stop_process(process: subprocess.Popen) -> str | None:
    warnings = []
    if os.name == "nt":
        # taskkill /T terminates children as well as the launcher.
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode:
                diagnostic = result.stdout.decode(errors="replace").strip()[:500]
                warnings.append(
                    f"process-tree cleanup could not be confirmed: {diagnostic or 'taskkill failed'}"
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            warnings.append(f"process-tree cleanup could not be confirmed: {exc}")
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            warnings.append(f"process-group cleanup failed: {exc}")
    # Use our existing process handle if enumeration/termination of the whole
    # tree is denied. This only guarantees stopping the launcher; say so.
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        warnings.append(f"launcher cleanup failed: {exc}")
    return "; ".join(warnings) or None


def run_command(
    argv: list[str],
    cwd: Path,
    timeout: int,
    log: Path,
    extra_env: dict[str, str] | None = None,
    *,
    capture_dir: Path | None = None,
    capture_max_mb: int = 512,
    capture_max_entries: int = 20000,
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

    def check_capture():
        if capture_dir is not None:
            inspect_artifacts(
                capture_dir, max_bytes=capture_max_mb * MIB, max_entries=capture_max_entries
            )

    check_capture()
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
            next_capture_check = 0.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                if log.stat().st_size > 16 * 1024 * 1024:
                    raise ExecutionError(f"command log exceeded 16 MiB; see {log.name}")
                if time.monotonic() >= next_capture_check:
                    check_capture()
                    next_capture_check = time.monotonic() + 0.5
                try:
                    code = process.wait(timeout=min(0.25, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except subprocess.TimeoutExpired as exc:
            warning = stop_process(process)
            message = f"command timed out after {timeout}s; see {log.name}"
            raise ExecutionError(message + (f"; {warning}" if warning else "")) from exc
        except BaseException as exc:
            if process.poll() is None:
                warning = stop_process(process)
                if warning and isinstance(exc, Exception):
                    raise ExecutionError(f"{exc}; {warning}") from exc
            raise
    if code:
        raise ExecutionError(f"command exited {code}; see {log.name}")
    if log.stat().st_size > 16 * 1024 * 1024:
        raise ExecutionError(f"command log exceeded 16 MiB; see {log.name}")
    check_capture()
