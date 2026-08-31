import pytest
from conftest import history

from divergence.engine import Search
from divergence.revisions import GitProvider, git, python_regions, rank_changes


@pytest.mark.integration
def test_audit_detects_fixed_and_reintroduced_regression(scenario, tmp_path):
    repo, commits = history(tmp_path, count=4, bad_at=2)
    good_code = git(repo, "show", f"{commits[0]}:app.py")
    bad_code = (repo / "app.py").read_text()
    for label, code in [("fix", good_code), ("reintroduce", bad_code)]:
        (repo / "app.py").write_text(code)
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", label)
    result = Search(repo, commits[0], "HEAD", scenario, tmp_path / "run", audit=True).run()
    assert result["status"] == "NON_MONOTONIC"
    assert len(result["good_after_bad"]) == 1
    assert not result.get("culprit")


def test_merge_diff_is_against_first_parent(tmp_path):
    repo, commits = history(tmp_path, count=3, bad_at=2)
    git(repo, "checkout", "-b", "feature", commits[0])
    (repo / "door.py").write_text("collider_enabled = True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "door bug")
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "feature", "-m", "Merge door bug")
    provider = GitProvider(repo, tmp_path / "run")
    diff = provider.diff(git(repo, "rev-parse", "HEAD"))
    assert "+collider_enabled = True" in diff
    assert "door.py" in diff


def test_existing_output_is_never_repurposed(scenario, tmp_path):
    repo, commits = history(tmp_path, count=3, bad_at=2)
    output = tmp_path / "important"
    output.mkdir()
    (output / "notes.txt").write_text("keep this")
    with pytest.raises(ValueError, match="empty output"):
        Search(repo, commits[0], commits[-1], scenario, output).run()
    assert (output / "notes.txt").read_text() == "keep this"


def test_changed_python_function_uses_added_lines_not_context():
    source = "class Door:\n    def open(self):\n        self.collider = True\n\n    def close(self):\n        self.collider = True\n"
    diff = "diff --git a/door.py b/door.py\n--- a/door.py\n+++ b/door.py\n@@ -1,6 +1,6 @@\n class Door:\n     def open(self):\n-        self.collider = False\n+        self.collider = True\n \n     def close(self):\n         self.collider = True\n"
    ranked = rank_changes(diff, ["door.collider"])
    assert ranked[0]["changed_lines"] == [3]
    regions = python_regions(source, ranked[0]["changed_lines"])
    assert [region["name"] for region in regions] == ["Door.open"]
