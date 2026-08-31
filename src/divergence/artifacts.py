"""Bounded artifact inspection for trusted captures; not filesystem isolation."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

MIB = 1024 * 1024


class ArtifactLimitError(ValueError):
    pass


def inspect_artifacts(
    directory: Path, *, max_bytes: int = 512 * MIB, max_entries: int = 20000
) -> list[tuple[Path, int]]:
    """Count all entries/bytes without following links or reading file contents."""
    pending = [directory]
    files = []
    entries = total = 0
    root = directory.lstat()
    if not stat.S_ISDIR(root.st_mode) or (
        getattr(root, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ):
        raise ArtifactLimitError("capture root must be a plain directory")
    while pending:
        folder = pending.pop()
        with os.scandir(folder) as children:
            for child in children:
                entries += 1
                if entries > max_entries:
                    raise ArtifactLimitError(f"capture exceeded {max_entries} filesystem entries")
                try:
                    info = child.stat(follow_symlinks=False)
                except FileNotFoundError:
                    # Capture workers may atomically replace temporary files.
                    continue
                if stat.S_ISLNK(info.st_mode) or (
                    getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                ):
                    raise ArtifactLimitError(f"capture links are not supported: {child.name}")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(child.path))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if total > max_bytes:
                        raise ArtifactLimitError(f"capture exceeded {max_bytes // MIB} MiB")
                    files.append((Path(child.path), info.st_size))
                else:
                    raise ArtifactLimitError(f"unsupported capture entry: {child.name}")
    return files


def evidence_digest(
    directory: Path, *, max_bytes: int = 512 * MIB, max_entries: int = 20000
) -> str:
    files = inspect_artifacts(directory, max_bytes=max_bytes, max_entries=max_entries)
    digest = hashlib.sha256()
    for path, expected_size in sorted(files):
        # Include videos and logs as well as traces and frames. Length prefixes
        # distinguish filenames from contents without concatenation ambiguities.
        name = path.relative_to(directory).as_posix().encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(expected_size.to_bytes(8, "big"))
        read = 0
        with path.open("rb") as stream:
            while chunk := stream.read(MIB):
                read += len(chunk)
                if read > expected_size:
                    raise ArtifactLimitError(
                        "capture changed while hashing; evidence is not stable"
                    )
                digest.update(chunk)
        if read != expected_size:
            raise ArtifactLimitError("capture changed while hashing; evidence is not stable")
    return digest.hexdigest()
