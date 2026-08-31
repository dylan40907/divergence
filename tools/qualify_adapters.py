"""Run fresh real-history fixture searches through the CLI; fail on any wrong boundary."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--browsers", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    if root.exists():
        raise SystemExit("Qualification requires a new output directory.")
    root.mkdir(parents=True)
    environment = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(args.browsers.resolve())}
    results = []
    for adapter, fixture, commits, bug in [
        ("command", "interlock", 128, 83),
        ("browser", "cart", 12, 8),
        ("pygame", "interlock", 8, 6),
        ("godot", "interlock", 8, 6),
    ]:
        print(f"Qualifying {adapter}: {commits} commits, boundary {bug}", flush=True)
        started = time.monotonic()
        workspace = root / "fixtures" / adapter
        output = root / "runs" / adapter
        with (root / f"{adapter}.log").open("w", encoding="utf-8") as log:

            def run(*arguments):
                subprocess.run(
                    [sys.executable, "-m", "divergence", *map(str, arguments)],
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=600,
                    check=True,
                )

            run(
                "demo",
                "--adapter",
                adapter,
                "--fixture",
                fixture,
                "--commits",
                commits,
                "--bug-at",
                bug,
                "--output",
                workspace,
                "--godot",
                args.godot.resolve(),
            )
            run("validate", workspace / "scenario.yaml")
            run(
                "bisect",
                "--repo",
                workspace / "repository",
                "--scenario",
                workspace / "scenario.yaml",
                "--good",
                "known-good",
                "--bad",
                "known-bad",
                "--output",
                output,
            )
            state = json.loads((output / "run.json").read_text(encoding="utf-8"))
            expected = json.loads((workspace / "expected.json").read_text(encoding="utf-8"))
            if state["status"] != "FOUND" or state["culprit"] != expected["expected_culprit"]:
                raise RuntimeError(f"{adapter}: search did not find the planted boundary")
            before = {sha: trial["captures"] for sha, trial in state["trials"].items()}
            run(
                "bisect",
                "--repo",
                workspace / "repository",
                "--scenario",
                workspace / "scenario.yaml",
                "--good",
                "known-good",
                "--bad",
                "known-bad",
                "--output",
                output,
                "--resume",
            )
            resumed = json.loads((output / "run.json").read_text(encoding="utf-8"))
            if before != {sha: trial["captures"] for sha, trial in resumed["trials"].items()}:
                raise RuntimeError(f"{adapter}: unchanged resume recaptured evidence")
        result = {
            "adapter": adapter,
            "fixture": fixture,
            "commits": commits,
            "bug_at": bug,
            "culprit": state["culprit"],
            "status": state["status"],
            "trials": len(state["trials"]),
            "captures": sum(len(t["captures"]) for t in state["trials"].values()),
            "first_divergence_ms": state["trials"][state["culprit"]]["comparison"][
                "first_divergence_ms"
            ],
            "resume_preserved_captures": True,
            "seconds": round(time.monotonic() - started, 2),
        }
        results.append(result)
        (root / "qualification.json").write_text(
            json.dumps(results, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
