import base64
import json
import re

import pytest
from conftest import history

from divergence.adapters import CommandAdapter
from divergence.engine import Search
from divergence.models import Scenario
from divergence.report import portable_trace, render_report
from divergence.storage import atomic_json


def test_scenario_defaults_have_a_stable_round_trip_digest(scenario):
    restored = Scenario.model_validate_json(scenario.model_dump_json())
    assert scenario.digest == restored.digest


@pytest.mark.integration
@pytest.mark.parametrize(
    "tamper", ["undisplayed-repeat", "video", "scenario", "missing", "duplicates"]
)
def test_reports_reject_changed_evidence_without_replacing_last_good_report(
    tmp_path, scenario, tamper
):
    repo, _ = history(tmp_path, count=1)
    output = tmp_path / "run"
    state = Search.for_verification(repo, "HEAD", scenario, output).run()
    report = render_report(output)
    original_report = report.read_bytes()
    trial = next(iter(state["trials"].values()))
    first = output / trial["captures"][0]["path"]
    if tamper == "undisplayed-repeat":
        (first / "trace.json").write_text((first / "trace.json").read_text() + " ")
    elif tamper == "video":
        (first / "extra.webm").write_bytes(b"modified non-displayed evidence")
    elif tamper == "missing":
        (first / "trace.json").unlink()
    elif tamper == "duplicates":
        trial["captures"][1] = trial["captures"][0]
        atomic_json(output / "run.json", state)
    else:
        state["scenario"]["seed"] += 1
        atomic_json(output / "run.json", state)
    with pytest.raises((ValueError, OSError)):
        render_report(output)
    assert report.read_bytes() == original_report


@pytest.mark.integration
def test_report_and_resume_validate_retained_retry_evidence(tmp_path, scenario, monkeypatch):
    repo, _ = history(tmp_path, count=1)
    original = CommandAdapter.capture
    calls = 0

    def capture(adapter, source, folder, recipe):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("temporary execution failure")
        return original(adapter, source, folder, recipe)

    monkeypatch.setattr(CommandAdapter, "capture", capture)
    output = tmp_path / "run"
    Search.for_verification(repo, "HEAD", scenario, output).run()
    state = Search.for_verification(repo, "HEAD", scenario, output).run(
        resume=True, retry_inconclusive=True
    )
    trial = next(iter(state["trials"].values()))
    report = render_report(output).read_text(encoding="utf-8")
    data = json.loads(base64.b64decode(re.search(r"atob\('([A-Za-z0-9+/=]+)'\)", report).group(1)))
    assert data["integrity"]["checked_captures"] == 3
    prior = output / trial["previous_attempts"][0]["captures"][0]["path"]
    (prior / "process.log").write_text("tampered prior log")
    with pytest.raises(ValueError, match="evidence changed"):
        render_report(output)
    with pytest.raises(ValueError, match="evidence changed"):
        Search.for_verification(repo, "HEAD", scenario, output).run(resume=True)


@pytest.mark.integration
def test_report_destination_and_size_guard_preserve_original_run(tmp_path, scenario, monkeypatch):
    import divergence.report as reports

    repo, _ = history(tmp_path, count=1)
    output = tmp_path / "run"
    state = Search.for_verification(repo, "HEAD", scenario, output).run()
    manifest = (output / "run.json").read_bytes()
    capture = output / next(iter(state["trials"].values()))["captures"][0]["path"]
    with pytest.raises(ValueError, match="original run data"):
        render_report(output, output / "run.json")
    with pytest.raises(ValueError, match="cannot overwrite evidence"):
        render_report(output, capture / "inside.html")
    assert (output / "run.json").read_bytes() == manifest
    assert not (capture / "inside.html").exists()
    report = render_report(output)
    original = report.read_bytes()
    monkeypatch.setattr(reports, "MAX_REPORT_BYTES", 100)
    with pytest.raises(ValueError, match="portable report exceeds"):
        render_report(output)
    assert report.read_bytes() == original


def test_subsampled_report_keeps_exact_onset_and_neighbor_samples(tmp_path, scenario):
    scenario.duration_ms = 100000
    trace = {
        "seed": scenario.seed,
        "samples": [
            {"t_ms": t, "state": {"door": {"open": t < 12300}}} for t in range(0, 100001, 100)
        ],
    }
    atomic_json(tmp_path / "trace.json", trace)
    displayed = portable_trace(tmp_path, scenario, [12300])
    times = {sample["t_ms"] for sample in displayed["samples"]}
    assert {12200, 12300, 12400}.issubset(times)
    assert displayed["display_stride"] > 1
    assert len(times) < len(trace["samples"])
