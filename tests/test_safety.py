import json
import threading
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
