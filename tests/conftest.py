from pathlib import Path

import pytest

from divergence.models import Scenario
from divergence.revisions import git


@pytest.fixture
def scenario():
    return Scenario(
        name="test",
        command=["{python}", "app.py"],
        duration_ms=200,
        sample_ms=100,
        assertions=[{"path": "door.open", "value": True}],
    )


def history(root: Path, count=8, bad_at=5, skip=()):
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    commits = []
    for index in range(count):
        source = """import os, json
from pathlib import Path
cfg=json.loads(Path(os.environ["DIVERGENCE_SCENARIO"]).read_text())
out=Path(os.environ["DIVERGENCE_OUTPUT"])
samples=[{"t_ms":t,"state":{"door":{"open":OPEN}}} for t in range(0,cfg["duration_ms"]+1,cfg["sample_ms"])]
(out/"trace.json").write_text(json.dumps({"seed":cfg["seed"],"samples":samples}))
""".replace("OPEN", repr(index < bad_at))
        if index in skip:
            source = "raise SystemExit(12)\n"
        (repo / "app.py").write_text(source + f"\n# revision {index}\n", encoding="utf-8")
        git(repo, "add", "app.py")
        git(repo, "commit", "-q", "-m", f"Revision {index}")
        commits.append(git(repo, "rev-parse", "HEAD"))
    return repo, commits
