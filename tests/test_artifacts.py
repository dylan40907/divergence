import json
import os
import subprocess
from pathlib import Path

import pytest

from divergence.adapters import CommandAdapter
from divergence.artifacts import MIB, ArtifactLimitError, evidence_digest, inspect_artifacts
from divergence.engine import Search


@pytest.mark.parametrize("wait_after_write", [False, True])
def test_capture_stops_on_size_limit_and_retains_diagnostics(tmp_path, scenario, wait_after_write):
    source = tmp_path / "source"
    source.mkdir()
    # A fast-exiting worker must also be checked, after the polling window.
    (source / "app.py").write_text(
        """import os, time
from pathlib import Path
output=Path(os.environ['DIVERGENCE_OUTPUT'])
(output/'oversized.webm').write_bytes(b'x' * (2 * 1024 * 1024))
"""
        + ("time.sleep(20)\n" if wait_after_write else "")
    )
    scenario.capture_limits.max_mb = 1
    output = tmp_path / "capture"
    with pytest.raises((ArtifactLimitError, RuntimeError), match="capture exceeded 1 MiB"):
        CommandAdapter().capture(source, output, scenario)
    assert (output / "oversized.webm").is_file()
    assert (output / "process.log").is_file()


def test_entry_limit_counts_empty_directories(tmp_path):
    for number in range(5):
        (tmp_path / str(number)).mkdir()
    with pytest.raises(ArtifactLimitError, match="4 filesystem entries"):
        inspect_artifacts(tmp_path, max_entries=4)


def test_digest_streams_files_and_includes_logs_and_video(tmp_path, monkeypatch):
    (tmp_path / "trace.json").write_text("{}")
    video = tmp_path / "capture.webm"
    with video.open("wb") as stream:
        stream.truncate(3 * MIB)
    log = tmp_path / "process.log"
    log.write_text("first")
    original_open = Path.open

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size=-1):
            assert 0 < size <= MIB, "evidence hashing must use bounded reads"
            return self.stream.read(size)

    def bounded_open(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return BoundedReader(stream) if mode == "rb" else stream

    monkeypatch.setattr(Path, "open", bounded_open)
    first = evidence_digest(tmp_path)
    log.write_text("other")
    second = evidence_digest(tmp_path)
    assert first != second
    with original_open(video, "r+b") as stream:
        stream.write(b"changed")
    assert evidence_digest(tmp_path) != second


def test_capture_rejects_directory_link_without_following_it(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("unchanged")
    capture = tmp_path / "capture"
    capture.mkdir()
    link = capture / "escape"
    if os.name == "nt":
        # Junctions need no symlink privilege. Use one native shell and literal paths.
        command = (
            "New-Item -ItemType Junction -Path '"
            + str(link).replace("'", "''")
            + "' -Target '"
            + str(outside).replace("'", "''")
            + "' | Out-Null"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ArtifactLimitError, match="capture links"):
            inspect_artifacts(capture)
        assert (outside / "sentinel").read_text() == "unchanged"
    finally:
        if os.name == "nt":
            link.rmdir()  # Remove the junction itself, never recursively traverse it.
        else:
            link.unlink()


def test_storage_failure_is_inconclusive_in_real_history(tmp_path, scenario):
    from conftest import history

    from divergence.revisions import git

    repo, commits = history(tmp_path, count=2, bad_at=1)
    code = (repo / "app.py").read_text()
    (repo / "app.py").write_text(code + "\n(out/'large.bin').write_bytes(b'x' * (2*1024*1024))\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-q", "-m", "Capture writes too much evidence")
    scenario.capture_limits.max_mb = 1
    output = tmp_path / "run"
    result = Search(repo, commits[0], "HEAD", scenario, output).run()
    assert result["status"] == "INVALID_ENDPOINT"
    endpoint = result["trials"][result["identity"]["bad"]]
    assert endpoint["verdict"] == "INCONCLUSIVE"
    assert not result.get("culprit")
    assert any("capture exceeded 1 MiB" in error for error in endpoint["errors"])
    assert json.loads((output / "run.json").read_text())["status"] == "INVALID_ENDPOINT"
