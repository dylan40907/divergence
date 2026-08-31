import json

import pytest
from conftest import history

from divergence.adapters import CommandAdapter
from divergence.engine import Search
from divergence.revisions import GitProvider, RevisionError, git


@pytest.mark.integration
def test_real_history_finds_bug_and_preserves_dirty_input(scenario, tmp_path):
    repo, commits = history(tmp_path)
    (repo / "precious.txt").write_text("untracked work")
    (repo / "app.py").write_text("user changes must survive")
    before = git(repo, "status", "--porcelain")
    output = tmp_path / "run"
    result = Search(repo, commits[0], commits[-1], scenario, output).run()
    assert result["status"] == "FOUND"
    assert result["culprit"] == commits[5]
    assert len(result["trials"]) <= 5
    assert git(repo, "status", "--porcelain") == before
    assert (repo / "app.py").read_text() == "user changes must survive"
    assert not list((output / "checkouts").iterdir())
    assert result["ranked_changes"][0]["path"] == "app.py"


@pytest.mark.integration
def test_unbuildable_neighbor_returns_candidate_set(scenario, tmp_path):
    repo, commits = history(tmp_path, skip=(4,))
    result = Search(repo, commits[0], commits[-1], scenario, tmp_path / "run").run()
    assert result["status"] == "AMBIGUOUS"
    assert result["candidates"] == commits[4:6]
    assert result["trials"][commits[4]]["verdict"] == "INCONCLUSIVE"


@pytest.mark.integration
def test_bad_baseline_is_rejected(scenario, tmp_path):
    repo, commits = history(tmp_path, bad_at=0)
    result = Search(repo, commits[0], commits[-1], scenario, tmp_path / "run").run()
    assert result["status"] == "INVALID_BASELINE"
    assert len(result["trials"]) == 1


@pytest.mark.integration
def test_good_bad_endpoint_is_rejected(scenario, tmp_path):
    repo, commits = history(tmp_path, bad_at=10)
    result = Search(repo, commits[0], commits[-1], scenario, tmp_path / "run").run()
    assert result["status"] == "INVALID_ENDPOINT"


@pytest.mark.integration
def test_resume_reuses_validated_trials_and_rejects_tampering(scenario, tmp_path):
    repo, commits = history(tmp_path)
    output = tmp_path / "run"

    def interrupt(event):
        if event["kind"] == "tested" and event["index"] not in {0, 7}:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        Search(repo, commits[0], commits[-1], scenario, output, interrupt).run()
    old = json.loads((output / "run.json").read_text())
    tested = []
    result = Search(
        repo,
        commits[0],
        commits[-1],
        scenario,
        output,
        lambda event: tested.append(event) if event["kind"] == "tested" else None,
    ).run(resume=True)
    assert result["culprit"] == commits[5]
    assert all(event["sha"] not in old["trials"] for event in tested)
    capture = result["trials"][commits[0]]["captures"][0]["path"]
    trace = output / capture / "trace.json"
    trace.write_text(trace.read_text() + " ")
    with pytest.raises(ValueError, match="evidence changed"):
        Search(repo, commits[0], commits[-1], scenario, output).run(resume=True)


def test_non_ancestor_is_rejected(tmp_path):
    repo, commits = history(tmp_path, count=3, bad_at=2)
    git(repo, "checkout", "--detach", commits[0])
    (repo / "branch.txt").write_text("side branch")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "side branch")
    side = git(repo, "rev-parse", "HEAD")
    with pytest.raises(RevisionError, match="first-parent"):
        GitProvider(repo, tmp_path / "run").history(side, commits[-1])


@pytest.mark.integration
@pytest.mark.parametrize("changed_on_retry", [False, True])
def test_resume_retries_execution_failure_without_erasing_prior_evidence(
    scenario, tmp_path, monkeypatch, changed_on_retry
):
    from divergence.storage import atomic_json

    repo, commits = history(tmp_path)
    output = tmp_path / "run"
    original_capture = CommandAdapter.capture
    calls = 0

    def fail_second_capture(adapter, source, folder, recipe):
        nonlocal calls
        if "# revision 4" in (source / "app.py").read_text():
            calls += 1
            if calls == 2:
                raise RuntimeError("temporary runtime unavailable")
        return original_capture(adapter, source, folder, recipe)

    monkeypatch.setattr(CommandAdapter, "capture", fail_second_capture)
    first = Search(repo, commits[0], commits[-1], scenario, output).run()
    assert first["status"] == "AMBIGUOUS"
    failed = first["trials"][commits[4]]
    assert failed["retryable"] is True
    assert len(failed["captures"]) == 1

    def retry_capture(adapter, source, folder, recipe):
        trace = original_capture(adapter, source, folder, recipe)
        if changed_on_retry and "# revision 4" in (source / "app.py").read_text():
            for sample in trace.samples:
                sample.state["door"]["open"] = False
            atomic_json(folder / "trace.json", trace.model_dump())
        return trace

    monkeypatch.setattr(CommandAdapter, "capture", retry_capture)
    result = Search(repo, commits[0], commits[-1], scenario, output).run(
        resume=True, retry_inconclusive=True
    )
    retried = result["trials"][commits[4]]
    assert len(retried["previous_attempts"]) == 1
    assert retried["previous_attempts"][0]["captures"] == failed["captures"]
    assert (output / failed["captures"][0]["path"] / "trace.json").is_file()
    assert retried["retryable"] is False
    if changed_on_retry:
        assert result["status"] == "AMBIGUOUS"
        assert retried["verdict"] == "INCONCLUSIVE"
        assert any("earlier attempt" in error for error in retried["errors"])
    else:
        assert result["status"] == "FOUND"
        assert result["culprit"] == commits[5]
        assert retried["verdict"] == "GOOD"

    def no_more_capture(*args):
        pytest.fail(
            "completed or behaviorally inconsistent evidence must not be selectively retried"
        )

    monkeypatch.setattr(CommandAdapter, "capture", no_more_capture)
    repeated = Search(repo, commits[0], commits[-1], scenario, output).run(
        resume=True, retry_inconclusive=True
    )
    assert repeated["status"] == result["status"]


def test_retry_requires_existing_resume(scenario, tmp_path):
    repo, commits = history(tmp_path, count=2, bad_at=1)
    with pytest.raises(ValueError, match="requires --resume"):
        Search(repo, commits[0], commits[-1], scenario, tmp_path / "run").run(
            retry_inconclusive=True
        )


@pytest.mark.integration
@pytest.mark.parametrize("conflict", ["verdict", "state"])
def test_partial_repeats_cannot_hide_flakiness_behind_later_execution_failure(
    scenario, tmp_path, monkeypatch, conflict
):
    from divergence.storage import atomic_json

    scenario.repeats = 4
    repo, commits = history(tmp_path, count=3, bad_at=2)
    original = CommandAdapter.capture
    calls = 0

    def capture(adapter, source, folder, recipe):
        nonlocal calls
        targeted = "# revision 1" in (source / "app.py").read_text()
        if targeted:
            calls += 1
            if calls == 3:
                raise RuntimeError("later execution failure")
        trace = original(adapter, source, folder, recipe)
        if targeted:
            for sample in trace.samples:
                if conflict == "verdict":
                    sample.state["door"]["open"] = calls == 1
                else:
                    sample.state["unstable"] = calls
            atomic_json(folder / "trace.json", trace.model_dump())
        return trace

    monkeypatch.setattr(CommandAdapter, "capture", capture)
    output = tmp_path / "run"
    result = Search(repo, commits[0], commits[-1], scenario, output).run()
    assert result["status"] == "AMBIGUOUS"
    assert result["trials"][commits[1]]["retryable"] is False
    resumed = Search(repo, commits[0], commits[-1], scenario, output).run(
        resume=True, retry_inconclusive=True
    )
    assert resumed["status"] == "AMBIGUOUS"
    assert calls == 3
