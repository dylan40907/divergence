import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from divergence.engine import run_lock
from divergence.models import Scenario, load_scenario
from divergence.process import ExecutionError, run_command
from divergence.server import make_server


def test_no_shell_evaluation(tmp_path):
    script = tmp_path / "args.py"
    script.write_text("import sys; print(sys.argv[1])")
    log = tmp_path / "log.txt"
    run_command(
        ["{python}", str(script), "$(whoami); `hostname`; & echo surprise"], tmp_path, 5, log
    )
    assert log.read_text().strip() == "$(whoami); `hostname`; & echo surprise"


def test_process_timeout(tmp_path):
    with pytest.raises(ExecutionError, match="timed out"):
        run_command(
            ["{python}", "-c", "import time; time.sleep(10)"], tmp_path, 1, tmp_path / "log"
        )


def test_timeout_stops_descendant_before_it_can_write(tmp_path):
    child = tmp_path / "child.py"
    child.write_text(
        "from pathlib import Path; import time\nPath('ready').write_text('yes')\ntime.sleep(3)\nPath('orphan').write_text('unexpected')\n"
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\nsubprocess.Popen([sys.executable, 'child.py'])\ntime.sleep(30)\n"
    )
    with pytest.raises(ExecutionError, match="timed out"):
        run_command(["{python}", str(parent)], tmp_path, 2, tmp_path / "log")
    assert (tmp_path / "ready").is_file(), (
        "the child must actually start for this test to be meaningful"
    )
    time.sleep(2)
    assert not (tmp_path / "orphan").exists()


def test_short_lived_oversized_log_is_rejected(tmp_path):
    with pytest.raises(ExecutionError, match="exceeded 16 MiB"):
        run_command(
            ["{python}", "-c", "import sys; sys.stdout.write('x' * (17 * 1024 * 1024))"],
            tmp_path,
            5,
            tmp_path / "log",
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill failure path")
def test_denied_tree_cleanup_stops_launcher_and_reports_warning(tmp_path, monkeypatch):
    from divergence.process import stop_process

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, b"Access denied"),
    )
    try:
        warning = stop_process(process)
        assert "cleanup could not be confirmed" in warning
        assert "Access denied" in warning
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_duplicate_run_lock_is_rejected(tmp_path):
    with run_lock(tmp_path):
        with pytest.raises(ValueError, match="another process"):
            with run_lock(tmp_path):
                pass


def test_yaml_is_data_and_unknown_fields_are_errors(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("!!python/object/apply:os.system ['echo bad']")
    with pytest.raises(ValueError, match="invalid scenario YAML"):
        load_scenario(path)
    with pytest.raises(ValueError):
        Scenario(name="bad", command=["echo"], assertionz=[])


def test_server_only_serves_reports_and_rejects_host_header(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / "run.json").write_text(
        json.dumps(
            {
                "scenario": {"name": "<script>bad</script>"},
                "status": "FOUND",
                "created_at": "2026-08-31",
            }
        )
    )
    (folder / "report.html").write_text("<h1>report</h1>")
    (folder / "secret.txt").write_text("secret")
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(url) as response:
            assert b"&lt;script&gt;" in response.read()
        with urllib.request.urlopen(url + "/run/report.html") as response:
            assert response.read() == b"<h1>report</h1>"
        for route in ("/run/secret.txt", "/../secret.txt", "/%2e%2e/report.html"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(url + route)
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(url, headers={"Host": "evil.test"}))
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
