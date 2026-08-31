"""Generate a real local history with a bug in application code, never oracle labels."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from divergence.revisions import git
from divergence.storage import atomic_json


def create_demo(
    output: Path,
    *,
    commits: int = 128,
    bug_at: int = 83,
    adapter: str = "command",
    godot: str = "godot",
) -> dict:
    if not 2 <= bug_at <= commits <= 512:
        raise ValueError("require 2 <= bug_at <= commits <= 512")
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("demo output must be new or empty; existing files are never overwritten")
    output.mkdir(parents=True, exist_ok=True)
    repository = output / "repository"
    template = adapter if adapter in {"browser", "godot"} else "python"
    shutil.copytree(
        Path(__file__).parent / "demo_templates" / template,
        repository,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Divergence Demo")
    git(repository, "config", "user.email", "demo@example.invalid")
    (repository / ".gitignore").write_text("__pycache__/\n*.pyc\n.godot/\n", encoding="utf-8")
    (repository / ".gitattributes").write_text(
        "* text=auto\n*.py text eol=lf\n*.js text eol=lf\n*.gd text eol=lf\n", encoding="utf-8"
    )
    culprit = good = ""
    for number in range(1, commits + 1):
        (repository / "evolution.txt").write_text(
            f"Interlock simulation revision {number}\n", encoding="utf-8"
        )
        if number == bug_at:
            door = repository / (
                "door.js"
                if adapter == "browser"
                else "door.gd"
                if adapter == "godot"
                else "door.py"
            )
            code = door.read_text(encoding="utf-8")
            before, after = (
                ("this.collider_enabled = false", "this.collider_enabled = true")
                if adapter == "browser"
                else ("self.collider_enabled = False", "self.collider_enabled = True")
            )
            if adapter == "godot":
                before, after = "collider_enabled = false", "collider_enabled = true"
            door.write_text(code.replace(before, after), encoding="utf-8")
        git(repository, "add", ".")
        message = (
            "Refactor door collision handling"
            if number == bug_at
            else f"Tune interlock lab iteration {number:03d}"
        )
        git(repository, "commit", "-q", "-m", message)
        if number == 1:
            good = git(repository, "rev-parse", "HEAD")
            git(repository, "tag", "known-good")
        if number == bug_at:
            culprit = git(repository, "rev-parse", "HEAD")
    git(repository, "tag", "known-bad")
    scenario = {
        "schema_version": 1,
        "name": "Interlock · open door and reach exit",
        "adapter": "browser" if adapter == "browser" else "command",
        "seed": 42,
        "duration_ms": 10000,
        "sample_ms": 250,
        "repeats": 2,
        "assertions": [
            {
                "path": "door.collider_enabled",
                "op": "eq",
                "value": False,
                "at": "always",
                "from_ms": 3000,
                "message": "An open door must release its collision barrier.",
            },
            {
                "path": "player.reached_exit",
                "op": "eq",
                "value": True,
                "at": "end",
                "message": "The player must reach the exit after activating the switch.",
            },
        ],
        "policy": {"mode": "assertions", "ignore_paths": ["clock_ms"]},
    }
    if adapter == "browser":
        scenario["browser"] = {
            "entry": "index.html",
            "tick_expression": "ms => window.divergence.step(ms)",
            "actions": [{"at_ms": 3000, "kind": "click", "target": "#switch"}],
        }
    elif adapter == "godot":
        scenario["command"] = [godot, "--headless", "--path", ".", "--script", "capture.gd"]
    else:
        scenario["command"] = ["{python}", "app.py"] + (["--pygame"] if adapter == "pygame" else [])
    scenario_path = output / "scenario.yaml"
    scenario_path.write_text(
        yaml.safe_dump(scenario, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    result = {
        "repository": str(repository),
        "scenario": str(scenario_path),
        "good": good,
        "bad": git(repository, "rev-parse", "HEAD"),
        "expected_culprit": culprit,
        "bug_at": bug_at,
        "commits": commits,
        "adapter": adapter,
    }
    # Ground truth is outside the repository and is never read by the search engine.
    atomic_json(output / "expected.json", result)
    return result
