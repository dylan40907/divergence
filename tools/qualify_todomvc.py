"""Validate an independently maintained TodoMVC app, then locate an explicit local mutation.

The upstream checkout and its two static npm dependencies must already exist. This script never
downloads code, modifies the upstream checkout, or claims the injected mutation is an upstream bug.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

UPSTREAM_COMMIT = "ff43b02e59dfa604386bb382034b2cd07c2bcd8a"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def qualify(upstream: Path, output: Path) -> dict:
    if git(upstream, "rev-parse", "HEAD") != UPSTREAM_COMMIT:
        raise ValueError(
            "use the documented upstream commit; review the integration before updating it"
        )
    if output.exists():
        raise ValueError("qualification requires a new output directory")
    app = upstream / "examples/javascript-es5"
    for name in ("todomvc-common", "todomvc-app-css"):
        if not (app / "node_modules" / name / "package.json").exists():
            raise ValueError("install the upstream static dependencies with --ignore-scripts first")
    output.mkdir(parents=True)
    repository = output / "repository"
    repository.mkdir()
    shutil.copy2(app / "index.html", repository / "index.html")
    shutil.copytree(app / "src", repository / "src")
    shutil.copy2(upstream / "license.md", repository / "LICENSE-TodoMVC.md")
    for name in ("todomvc-common", "todomvc-app-css"):
        shutil.copytree(app / "node_modules" / name, repository / "node_modules" / name)
    # The optional framework-information sidebar requests this document. Keep it local.
    shutil.copy2(upstream / "learn.json", repository / "learn.json")
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Divergence compatibility qualification")
    git(repository, "config", "user.email", "qualification@example.invalid")
    controller = repository / "src/controller.js"
    original = controller.read_text(encoding="utf-8")
    old = "Controller.prototype.removeCompletedItems = function () {\n        var self = this;\n        self.model.read({ completed: true }"
    if original.count(old) != 1:
        raise ValueError("upstream clear-completed implementation changed; review the mutation")
    revisions = []
    for number in range(1, 9):
        if number == 6:
            controller.write_text(
                original.replace(old, old.replace("completed: true", "completed: false")),
                encoding="utf-8",
            )
        (repository / "qualification-history.txt").write_text(
            f"Local validation revision {number}\n"
        )
        git(repository, "add", ".")
        git(
            repository,
            "commit",
            "-q",
            "-m",
            "Inject wrong clear-completed filter"
            if number == 6
            else f"Compatibility history {number}",
        )
        revisions.append(git(repository, "rev-parse", "HEAD"))
    recipe = {
        "schema_version": 1,
        "name": "TodoMVC upstream · clear only completed tasks",
        "adapter": "browser",
        "seed": 42,
        "duration_ms": 2500,
        "sample_ms": 250,
        "repeats": 2,
        "browser": {
            "entry": "index.html",
            "reset_expression": "() => {}",
            "state_expression": "({state:{titles:Array.from(document.querySelectorAll('.todo-list li label'), n => n.textContent), remaining:Number(document.querySelector('.todo-count strong')?.textContent || 0), completed:document.querySelectorAll('.todo-list li.completed').length}})",
            "actions": [
                {"at_ms": 250, "kind": "fill", "target": ".new-todo", "value": "Review trace"},
                {"at_ms": 500, "kind": "press", "target": ".new-todo", "value": "Enter"},
                {"at_ms": 750, "kind": "fill", "target": ".new-todo", "value": "Ship release"},
                {"at_ms": 1000, "kind": "press", "target": ".new-todo", "value": "Enter"},
                {"at_ms": 1250, "kind": "click", "target": ".todo-list li:first-child .toggle"},
                {"at_ms": 1500, "kind": "click", "target": '.filters a[href="#/completed"]'},
                {"at_ms": 1750, "kind": "click", "target": ".clear-completed"},
                {"at_ms": 2000, "kind": "click", "target": '.filters a[href="#/"]'},
            ],
        },
        "assertions": [
            {
                "path": "titles",
                "value": ["Ship release"],
                "message": "Clearing completed tasks must retain the unfinished task.",
            },
            {"path": "remaining", "value": 1},
            {"path": "completed", "value": 0},
        ],
        "policy": {"mode": "assertions"},
    }
    scenario = output / "scenario.yaml"
    scenario.write_text(
        yaml.safe_dump(recipe, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    outcomes = {}
    for label, arguments, expected in [
        ("verify-good", ["verify", "--revision", revisions[0]], 0),
        ("verify-bad", ["verify", "--revision", revisions[-1]], 3),
        ("bisect", ["bisect", "--good", revisions[0], "--bad", revisions[-1]], 0),
    ]:
        folder = output / "runs" / label
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "divergence",
                *arguments,
                "--repo",
                str(repository),
                "--scenario",
                str(scenario),
                "--output",
                str(folder),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        (output / f"{label}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode != expected:
            raise RuntimeError(
                f"{label} returned {result.returncode}; see {output / (label + '.log')}"
            )
        state = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        outcomes[label] = {
            "status": state["status"],
            "trials": len(state["trials"]),
            "culprit": state.get("culprit"),
        }
        print(label, state["status"], flush=True)
    if outcomes["bisect"]["culprit"] != revisions[5]:
        raise RuntimeError("search did not locate the injected mutation")
    if (
        outcomes["verify-good"]["status"] != "VERIFIED"
        or outcomes["verify-bad"]["status"] != "CHECK_FAILED"
    ):
        raise RuntimeError("unexpected verification result")
    result = {
        "upstream": "https://github.com/tastejs/todomvc",
        "upstream_commit": UPSTREAM_COMMIT,
        "application": "examples/javascript-es5",
        "license": "MIT",
        "mutations_are_local": True,
        "history": revisions,
        "expected_culprit": revisions[5],
        "outcomes": outcomes,
    }
    (output / "qualification.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(qualify(args.upstream.resolve(), args.output.resolve()), indent=2))
