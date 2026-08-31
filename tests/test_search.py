import json

import pytest
from conftest import history

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
