from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def _retry_windows_access(operation: Callable[[], None]) -> None:
    """Allow brief file-handle contention without weakening permissions or atomicity."""
    for attempt in range(8):
        try:
            operation()
            return
        except PermissionError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 7:
                raise
            # Windows readers may temporarily deny replacement/deletion. Never fall
            # back to overwriting a live manifest; permanent denial still propagates.
            time.sleep(min(0.025 * 2**attempt, 0.2))


@contextmanager
def atomic_writer(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yield stream
        _retry_windows_access(lambda: os.replace(temporary, path))
    finally:
        if Path(temporary).exists():
            _retry_windows_access(lambda: Path(temporary).unlink())


def atomic_json(path: Path, value: Any) -> None:
    with atomic_writer(path) as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def atomic_text(path: Path, value: str) -> None:
    with atomic_writer(path) as stream:
        stream.write(value)
