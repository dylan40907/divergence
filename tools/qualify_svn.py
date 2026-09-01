"""Build an isolated SVN repository and qualify the real provider end to end."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import yaml

from divergence.engine import Search
from divergence.models import load_scenario
from divergence.report import render_report
from divergence.svn_provider import SubversionProvider


def command(executable: str, *args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        [executable, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def application(revision: int) -> str:
    return """import json, os
from pathlib import Path
cfg=json.loads(Path(os.environ["DIVERGENCE_SCENARIO"]).read_text())
out=Path(os.environ["DIVERGENCE_OUTPUT"])
samples=[{"t_ms":t,"state":{"door":{"open":OPEN}}} for t in range(0,cfg["duration_ms"]+1,cfg["sample_ms"])]
(out/"trace.json").write_text(json.dumps({"seed":cfg["seed"],"samples":samples}))
""".replace("OPEN", repr(revision < 6)) + f"\n# SVN revision {revision}\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--svn", required=True)
    parser.add_argument("--svnadmin", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"refusing to overwrite {output}")
    output.mkdir(parents=True)
    repository = output / "repository"
    active = output / "active-working-copy"
    run = output / "run"
    command(args.svnadmin, "create", str(repository))
    url = repository.as_uri()
    command(args.svn, "mkdir", f"{url}/trunk", "-m", "Create trunk", "--non-interactive")
    active.mkdir()
    command(args.svn, "checkout", f"{url}/trunk", str(active), "--non-interactive")
    revisions = []
    for expected in range(2, 9):
        (active / "app.py").write_text(application(expected), encoding="utf-8")
        if expected == 2:
            command(args.svn, "add", "app.py", cwd=active)
        result = command(
            args.svn,
            "commit",
            "-m",
            f"Fixture revision {expected}",
            "--non-interactive",
            cwd=active,
        )
        actual = int(result.rsplit(" ", 1)[-1].rstrip("."))
        if actual != expected:
            raise RuntimeError(f"expected SVN revision {expected}, got {actual}")
        revisions.append(str(actual))
    sentinel = active / "KEEP.txt"
    sentinel.write_text("uncommitted active workspace data", encoding="utf-8")
    scenario_path = output / "scenario.yaml"
    scenario_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "name": "SVN qualification",
                "adapter": "command",
                "command": ["{python}", "app.py"],
                "build": [],
                "seed": 42,
                "duration_ms": 100,
                "sample_ms": 100,
                "timeout_seconds": 30,
                "repeats": 2,
                "assertions": [{"path": "door.open", "op": "eq", "value": True}],
                "policy": {"mode": "assertions"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    scenario = load_scenario(scenario_path)
    provider = SubversionProvider(active, run, executable=args.svn)
    result = Search(active, revisions[0], revisions[-1], scenario, run, provider=provider).run()
    if result["status"] != "FOUND" or result["culprit"] != "6":
        raise RuntimeError(f"unexpected SVN search result: {result['status']} {result.get('culprit')}")
    report = render_report(run)
    captures = sum(len(trial.get("captures", [])) for trial in result["trials"].values())
    trial_count = len(result["trials"])
    provider = SubversionProvider(active, run, executable=args.svn)
    resumed = Search(active, revisions[0], revisions[-1], scenario, run, provider=provider).run(
        resume=True
    )
    resumed_captures = sum(
        len(trial.get("captures", [])) for trial in resumed["trials"].values()
    )
    if resumed_captures != captures:
        raise RuntimeError("SVN resume unexpectedly created new captures")
    if sentinel.read_text(encoding="utf-8") != "uncommitted active workspace data":
        raise RuntimeError("active SVN working copy changed")
    if list((run / "svn-checkouts").iterdir()):
        raise RuntimeError("temporary SVN checkouts remain")
    summary = {
        "provider": "svn",
        "svn_version": command(args.svn, "--version", "--quiet"),
        "history": revisions,
        "planted_revision": "6",
        "found_revision": result["culprit"],
        "trials": trial_count,
        "captures": captures,
        "resume_reused_evidence": True,
        "active_workspace_preserved": True,
        "temporary_checkouts_removed": True,
        "report": str(report),
    }
    (output / "qualification.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
