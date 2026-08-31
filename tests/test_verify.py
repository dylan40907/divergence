import base64
import json
import re

import pytest
import yaml
from conftest import history
from typer.testing import CliRunner

from divergence.adapters import CommandAdapter
from divergence.cli import app
from divergence.engine import Search
from divergence.report import render_report
from divergence.revisions import git
from divergence.storage import atomic_json


def report_payload(output):
    html = render_report(output).read_text(encoding="utf-8")
    encoded = re.search(r"atob\('([A-Za-z0-9+/=]+)'\)", html).group(1)
    return json.loads(base64.b64decode(encoded))


@pytest.mark.integration
def test_verify_single_commit_preserves_work_and_displays_independent_replays(
    scenario, tmp_path, monkeypatch
):
    repo, commits = history(tmp_path, count=1)
    (repo / "app.py").write_text("uncommitted work must survive")
    (repo / "precious.txt").write_text("untracked work")
    before = git(repo, "status", "--porcelain")
    original = CommandAdapter.capture
    calls = 0

    def capture(adapter, source, folder, recipe):
        nonlocal calls
        calls += 1
        trace = original(adapter, source, folder, recipe)
        trace.metadata["replay"] = calls
        atomic_json(folder / "trace.json", trace.model_dump())
        return trace

    monkeypatch.setattr(CommandAdapter, "capture", capture)
    output = tmp_path / "run"
    result = Search.for_verification(repo, "HEAD", scenario, output).run()
    assert result["status"] == "VERIFIED"
    assert len(result["history"]) == 1
    assert not result["candidates"]
    assert "culprit" not in result
    assert git(repo, "status", "--porcelain") == before
    assert (repo / "app.py").read_text() == "uncommitted work must survive"
    assert not list((output / "checkouts").iterdir())
    payload = report_payload(output)
    assert payload["reference_trace"]["metadata"]["replay"] == 1
    assert payload["traces"][commits[0]]["metadata"]["replay"] == 2
    Search.for_verification(repo, "HEAD", scenario, output).run(resume=True)
    assert calls == 2, "resume must validate the evidence, not execute the app again"
    directory = output / result["trials"][commits[0]]["captures"][1]["path"]
    with (directory / "trace.json").open("a") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="evidence changed"):
        Search.for_verification(repo, "HEAD", scenario, output).run(resume=True)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("bad_at", "skip", "status", "exit_code"),
    [(2, (), "VERIFIED", 0), (0, (), "CHECK_FAILED", 3), (2, (0,), "INCONCLUSIVE", 3)],
)
def test_verify_cli_distinguishes_checks_from_execution_failures(
    scenario, tmp_path, bad_at, skip, status, exit_code
):
    repo, commits = history(tmp_path, count=1, bad_at=bad_at, skip=skip)
    recipe = tmp_path / "scenario.yaml"
    recipe.write_text(yaml.safe_dump(scenario.model_dump()))
    output = tmp_path / "run"
    runner = CliRunner()
    arguments = ["verify", "--repo", str(repo), "--scenario", str(recipe), "--output", str(output)]
    result = runner.invoke(app, arguments)
    assert result.exit_code == exit_code, result.output
    assert status in result.output
    payload = report_payload(output)
    trial = payload["run"]["trials"][commits[0]]
    assert payload["run"]["status"] == status
    if status == "CHECK_FAILED":
        assert trial["comparison"]["assertion_failures"][0]["actual"] is False
        assert trial["comparison"]["first_divergence_ms"] == 200
        assert trial["repeat_verdicts"] == ["BAD", "BAD"]
    elif status == "INCONCLUSIVE":
        assert trial["errors"] and trial["retryable"]
        assert not payload["traces"] and payload["reference_trace"] is None
    result = runner.invoke(app, arguments)
    assert result.exit_code == 2
    assert "already contains a run" in result.output
    invalid = runner.invoke(app, ["verify", "--scenario", str(recipe), "--resume"])
    assert invalid.exit_code == 2
    assert "requires the original --output" in invalid.output


@pytest.mark.integration
def test_verify_selects_actual_unstable_replay_not_a_self_comparison(
    scenario, tmp_path, monkeypatch
):
    scenario.repeats = 3
    repo, commits = history(tmp_path, count=1)
    original = CommandAdapter.capture
    calls = 0

    def capture(adapter, source, folder, recipe):
        nonlocal calls
        calls += 1
        trace = original(adapter, source, folder, recipe)
        for sample in trace.samples:
            sample.state["unexpected"] = 100 if calls == 3 else 0
        atomic_json(folder / "trace.json", trace.model_dump())
        return trace

    monkeypatch.setattr(CommandAdapter, "capture", capture)
    output = tmp_path / "run"
    result = Search.for_verification(repo, "HEAD", scenario, output).run()
    assert result["status"] == "INCONCLUSIVE"
    trial = result["trials"][commits[0]]
    assert trial["repeat_verdicts"] == ["GOOD"] * 3  # assertions alone cannot catch this
    assert trial["comparison_capture_index"] == 2
    assert trial["comparison_basis"] == "repeatability"
    assert trial["comparison"]["first_divergence_ms"] == 0
    assert not trial["retryable"]
    payload = report_payload(output)
    assert payload["reference_trace"]["samples"][0]["state"]["unexpected"] == 0
    assert payload["traces"][commits[0]]["samples"][0]["state"]["unexpected"] == 100
    Search.for_verification(repo, "HEAD", scenario, output).run(
        resume=True, retry_inconclusive=True
    )
    assert calls == 3, "flakiness must not be retried until it happens to pass"


@pytest.mark.integration
def test_verify_partial_failure_retries_and_retains_prior_capture(scenario, tmp_path, monkeypatch):
    repo, commits = history(tmp_path, count=1)
    original = CommandAdapter.capture
    calls = 0

    def capture(adapter, source, folder, recipe):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("temporary unavailable runtime")
        return original(adapter, source, folder, recipe)

    monkeypatch.setattr(CommandAdapter, "capture", capture)
    output = tmp_path / "run"
    failed = Search.for_verification(repo, "HEAD", scenario, output).run()
    assert failed["status"] == "INCONCLUSIVE"
    payload = report_payload(output)
    assert payload["reference_trace"] is None, "do not display a capture compared with itself"
    assert commits[0] in payload["traces"]
    result = Search.for_verification(repo, "HEAD", scenario, output).run(
        resume=True, retry_inconclusive=True
    )
    assert result["status"] == "VERIFIED"
    trial = result["trials"][commits[0]]
    assert calls == 4
    assert trial["previous_attempts"][0]["captures"] == failed["trials"][commits[0]]["captures"]
    assert report_payload(output)["reference_trace"] is not None


@pytest.mark.integration
def test_verify_difference_only_does_not_claim_assertions_passed(scenario, tmp_path):
    scenario.policy.mode = "difference"
    repo, _ = history(tmp_path, count=1, bad_at=0)
    result = Search.for_verification(repo, "HEAD", scenario, tmp_path / "run").run()
    assert result["status"] == "VERIFIED"  # stable, even though the unused assertion would fail
    assert "No behavioral assertions were enforced" in result["message"]


@pytest.mark.integration
def test_verification_cannot_resume_a_search_or_changed_scenario(scenario, tmp_path):
    repo, commits = history(tmp_path, count=2, bad_at=1)
    output = tmp_path / "run"
    Search(repo, commits[0], commits[1], scenario, output).run()
    with pytest.raises(ValueError, match="changed"):
        Search.for_verification(repo, commits[0], scenario, output).run(resume=True)
    verify_output = tmp_path / "verify"
    Search.for_verification(repo, commits[0], scenario, verify_output).run()
    scenario.seed += 1
    with pytest.raises(ValueError, match="changed"):
        Search.for_verification(repo, commits[0], scenario, verify_output).run(resume=True)
