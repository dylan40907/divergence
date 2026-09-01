from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from divergence.engine import Search
from divergence.models import Policy, Scenario
from divergence.plastic_provider import PlasticProvider
from divergence.revisions import RevisionError

FIELD = "|DIVERGENCE-FIELD|"
RECORD = "|DIVERGENCE-RECORD|"


class FakePlastic:
    changes = ["100", "120", "140", "160"]

    def __init__(self, active: Path, *, fail_update: bool = False, wrong_revision: bool = False):
        self.active = active.resolve()
        self.fail_update = fail_update
        self.wrong_revision = wrong_revision
        self.created: list[Path] = []
        self.deleted: list[Path] = []
        self.updated: list[str] = []
        self.workspace_revision = "100"

    @staticmethod
    def _formatted(fields: dict[str, str], format_value: str) -> str:
        output = format_value
        for name, value in fields.items():
            output = output.replace("{" + name + "}", value)
        return output

    def __call__(self, argv: list[str], cwd: Path, timeout: int):
        args = argv[1:]
        stdout = ""
        stderr = ""
        returncode = 0
        if args[0] == "getworkspacefrompath":
            assert "--extended" in args
            stdout = FIELD.join(["artist_workspace", str(self.active), "artist", "regular", "False"])
        elif args[0] == "status" and "--head" in args:
            stdout = "cs:160@game@cloud"
        elif args[0] == "status" and "--header" in args:
            path = Path(args[1]).resolve()
            revision = (
                "999"
                if path != self.active and self.wrong_revision
                else self.workspace_revision if path != self.active else "100"
            )
            stdout = f"/main@game@cloud (cs:{revision} - head)"
        elif args[:2] == ["find", "changesets"]:
            query = args[2]
            if "where branch" in query:
                assert "br:/main" in query
            format_value = next(value.removeprefix("--format=") for value in args if value.startswith("--format="))
            exact = re.search(r"changesetid = ([0-9]+)", query)
            revisions = [exact.group(1)] if exact else self.changes
            rows = []
            for revision in revisions:
                rows.append(
                    self._formatted(
                        {
                            "changesetid": revision,
                            "parent": str(max(0, int(revision) - 20)),
                            "branch": "/main",
                            "date": "2026-09-01T00:00:00Z",
                            "owner": "artist",
                            "comment": "Break door" if revision == "140" else f"Changeset {revision}",
                        },
                        format_value,
                    )
                )
            stdout = "".join(rows)
        elif args[:2] == ["workspace", "create"]:
            path = Path(args[3])
            path.mkdir(parents=True)
            self.created.append(path.resolve())
        elif args[0] == "update":
            revision = next(value.split("cs:", 1)[1] for value in args if value.startswith("--changeset=cs:"))
            self.updated.append(revision)
            self.workspace_revision = revision
            if self.fail_update:
                returncode = 1
                stderr = "interrupted UVCS update"
            else:
                source = """import json, os
from pathlib import Path
cfg=json.loads(Path(os.environ['DIVERGENCE_SCENARIO']).read_text())
out=Path(os.environ['DIVERGENCE_OUTPUT'])
value=VALUE
samples=[{'t_ms':t,'state':{'door':{'open':value}}} for t in range(0,cfg['duration_ms']+1,cfg['sample_ms'])]
(out/'trace.json').write_text(json.dumps({'seed':cfg['seed'],'samples':samples}))
""".replace("VALUE", repr(int(revision) < 140))
                (cwd / "app.py").write_text(source, encoding="utf-8")
        elif args[:2] == ["workspace", "delete"]:
            self.deleted.append(Path(args[2]).resolve())
        elif args[0] == "patch":
            stdout = (
                "Index: Assets/app.py\n"
                "--- old/Assets/app.py\n+++ new/Assets/app.py\n"
                "@@ -1 +1 @@\n-value=True\n+value=False\n"
            )
        elif args[0] == "cat":
            stdout = "value=False\n"
        else:
            returncode = 1
            stderr = f"unexpected cm command: {args}"
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


@pytest.fixture
def plastic_scenario():
    return Scenario(
        name="UVCS fixture",
        command=["{python}", "app.py"],
        duration_ms=100,
        sample_ms=100,
        assertions=[{"path": "door.open", "value": True}],
        policy=Policy(mode="assertions"),
    )


def test_plastic_search_uses_disposable_workspace_and_preserves_active_workspace(
    tmp_path, plastic_scenario
):
    active = tmp_path / "active-plastic"
    active.mkdir()
    sentinel = active / "KEEP.txt"
    sentinel.write_text("active edit")
    fake = FakePlastic(active)
    output = tmp_path / "run"
    provider = PlasticProvider(active, output, runner=fake)
    assert provider.history("100", "160") == fake.changes
    assert provider.inspect()["branch"] == "/main"

    result = Search(
        active,
        "100",
        "160",
        plastic_scenario,
        output,
        provider=provider,
    ).run()
    assert result["status"] == "FOUND"
    assert result["culprit"] == "140"
    assert result["identity"]["revision_provider"]["kind"] == "plastic"
    assert result["ranked_changes"][0]["path"] == "Assets/app.py"
    assert sentinel.read_text() == "active edit"
    assert not provider.workspace.exists()
    assert len(fake.created) == len(fake.deleted)

    update_count = len(fake.updated)
    resumed = Search(
        active,
        "100",
        "160",
        plastic_scenario,
        output,
        provider=PlasticProvider(active, output, runner=fake),
    ).run(resume=True)
    assert resumed["culprit"] == "140"
    assert len(fake.updated) == update_count


def test_plastic_partial_update_and_wrong_revision_are_cleaned(tmp_path):
    active = tmp_path / "active"
    active.mkdir()
    failed = FakePlastic(active, fail_update=True)
    provider = PlasticProvider(active, tmp_path / "failed-run", runner=failed)
    provider.prepare()
    with pytest.raises(RevisionError, match="interrupted"):
        provider.checkout("100", "trial")
    assert failed.created == failed.deleted
    assert not provider.workspace.exists()

    wrong = FakePlastic(active, wrong_revision=True)
    provider = PlasticProvider(active, tmp_path / "wrong-run", runner=wrong)
    provider.prepare()
    with pytest.raises(RevisionError, match="requested isolated changeset"):
        provider.checkout("100", "trial")
    assert wrong.created == wrong.deleted
    assert not provider.workspace.exists()


def test_plastic_refuses_active_workspace_output_and_invalid_revisions(tmp_path):
    active = tmp_path / "active"
    active.mkdir()
    with pytest.raises(RevisionError, match="outside"):
        PlasticProvider(active, active / ".divergence" / "run", runner=FakePlastic(active))

    provider = PlasticProvider(active, tmp_path / "run", runner=FakePlastic(active))
    for revision in ("0", "-1", "HEAD~1", "2;delete"):
        with pytest.raises(RevisionError, match="positive numeric"):
            provider.resolve(revision)
