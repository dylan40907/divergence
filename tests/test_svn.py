from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from divergence.engine import Search
from divergence.models import Policy, Scenario
from divergence.revisions import RevisionError
from divergence.svn_provider import SubversionProvider


class FakeSvn:
    def __init__(self, active: Path, *, credentials_in_url: bool = False):
        self.active = active.resolve()
        self.credentials_in_url = credentials_in_url
        self.checkouts: list[tuple[str, Path, bool]] = []

    def __call__(self, argv: list[str], cwd: Path, timeout: int):
        args = argv[1:-1]
        command = args[0]
        stdout = ""
        returncode = 0
        stderr = ""
        url = (
            "https://user:secret@example.invalid/svn/game/trunk"
            if self.credentials_in_url
            else "https://example.invalid/svn/game/trunk"
        )
        if command == "info":
            revision = "160" if "HEAD" in args else next(
                (args[index + 1] for index, value in enumerate(args) if value == "-r"), "160"
            )
            stdout = (
                '<?xml version="1.0"?><info><entry kind="dir" path="." '
                f'revision="{revision}"><url>{url}</url><repository>'
                '<root>https://example.invalid/svn/game</root><uuid>fixture-uuid</uuid>'
                f'</repository><wc-info><wcroot-abspath>{self.active}</wcroot-abspath>'
                "</wc-info></entry></info>"
            )
        elif command == "log":
            range_value = args[args.index("-r") + 1]
            if range_value == "100:160":
                entries = [(100, "Good"), (120, "Still good"), (140, "Break door"), (160, "Bad")]
            else:
                revision = int(range_value.split(":", 1)[0])
                entries = [(revision, f"Revision {revision}")]
            stdout = "<?xml version='1.0'?><log>" + "".join(
                f"<logentry revision='{revision}'><author>tester</author>"
                f"<date>2026-09-01T00:00:00.000000Z</date><msg>{message}</msg></logentry>"
                for revision, message in entries
            ) + "</log>"
        elif command == "checkout":
            revision = args[args.index("-r") + 1]
            target = Path(args[-1])
            ignored = "--ignore-externals" in args
            target.mkdir(parents=True)
            source = """import json, os
from pathlib import Path
cfg=json.loads(Path(os.environ['DIVERGENCE_SCENARIO']).read_text())
out=Path(os.environ['DIVERGENCE_OUTPUT'])
value=VALUE
samples=[{'t_ms':t,'state':{'door':{'open':value}}} for t in range(0,cfg['duration_ms']+1,cfg['sample_ms'])]
(out/'trace.json').write_text(json.dumps({'seed':cfg['seed'],'samples':samples}))
""".replace("VALUE", repr(int(revision) < 140))
            (target / "app.py").write_text(source, encoding="utf-8")
            self.checkouts.append((revision, target, ignored))
        elif command == "diff":
            stdout = (
                "diff --git a/trunk/app.py b/trunk/app.py\n"
                "--- a/trunk/app.py\n+++ b/trunk/app.py\n"
                "@@ -1 +1 @@\n-value=True\n+value=False\n"
            )
        elif command == "cat":
            stdout = "value=False\n"
        else:
            returncode = 1
            stderr = f"unexpected svn command: {args}"
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


@pytest.fixture
def svn_scenario():
    return Scenario(
        name="SVN fixture",
        command=["{python}", "app.py"],
        duration_ms=100,
        sample_ms=100,
        assertions=[{"path": "door.open", "value": True}],
        policy=Policy(mode="assertions"),
    )


def test_subversion_search_uses_disposable_checkouts_and_preserves_active_copy(
    tmp_path, svn_scenario
):
    active = tmp_path / "active-svn"
    active.mkdir()
    sentinel = active / "KEEP.txt"
    sentinel.write_text("active edit")
    fake = FakeSvn(active)
    output = tmp_path / "run"
    provider = SubversionProvider(active, output, runner=fake)
    assert provider.history("100", "160") == ["100", "120", "140", "160"]

    result = Search(
        active,
        "100",
        "160",
        svn_scenario,
        output,
        provider=provider,
    ).run()
    assert result["status"] == "FOUND"
    assert result["culprit"] == "140"
    assert result["identity"]["revision_provider"]["kind"] == "svn"
    assert result["ranked_changes"][0]["path"] == "trunk/app.py"
    assert sentinel.read_text() == "active edit"
    assert not list((output / "svn-checkouts").iterdir())
    assert all(ignored for _, _, ignored in fake.checkouts)

    checkout_count = len(fake.checkouts)
    resumed = Search(
        active,
        "100",
        "160",
        svn_scenario,
        output,
        provider=SubversionProvider(active, output, runner=fake),
    ).run(resume=True)
    assert resumed["culprit"] == "140"
    assert len(fake.checkouts) == checkout_count


def test_subversion_safety_and_external_policy(tmp_path):
    active = tmp_path / "active"
    active.mkdir()
    with pytest.raises(RevisionError, match="outside"):
        SubversionProvider(active, active / ".divergence" / "run", runner=FakeSvn(active))

    fake = FakeSvn(active)
    provider = SubversionProvider(
        active, tmp_path / "external-run", include_externals=True, runner=fake
    )
    provider.prepare()
    checkout = provider.checkout("100", "external")
    assert fake.checkouts[-1][2] is False
    provider.release(checkout)


def test_subversion_rejects_embedded_credentials_and_invalid_revisions(tmp_path):
    active = tmp_path / "active"
    active.mkdir()
    with pytest.raises(RevisionError, match="embedded credentials"):
        SubversionProvider(active, tmp_path / "run", runner=FakeSvn(active, credentials_in_url=True))

    provider = SubversionProvider(active, tmp_path / "valid", runner=FakeSvn(active))
    for revision in ("0", "-1", "HEAD~1", "1;delete"):
        with pytest.raises(RevisionError, match="positive integers"):
            provider.resolve(revision)
