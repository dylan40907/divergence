import os
import subprocess

import pytest
from conftest import history
from typer.testing import CliRunner

from divergence.cli import app
from divergence.engine import Search
from divergence.models import load_scenario, load_trace
from divergence.revisions import git


@pytest.mark.integration
def test_generated_command_scenario_validates_and_verifies_without_running_during_init(
    tmp_path, monkeypatch
):
    repo, _ = history(tmp_path, count=1)
    output = tmp_path / "scenarios" / "door.yaml"
    command = ["{python}", "app.py", "two words", 'embedded"quote', "$(not-a-shell)", ""]
    runner = CliRunner()
    name = "Door: check\npolicy: learned"
    with monkeypatch.context() as guard:
        guard.setattr(
            subprocess, "Popen", lambda *args, **kwargs: pytest.fail("init executed code")
        )
        result = runner.invoke(
            app,
            [
                "init",
                "--output",
                str(output),
                "--name",
                name,
                "--duration-ms",
                "200",
                "--sample-ms",
                "100",
                "--",
                *command,
            ],
        )
    assert result.exit_code == 0, result.output
    assert "Repeatability only" in result.output
    scenario = load_scenario(output)
    assert scenario.name == name
    assert scenario.command == command
    assert not scenario.assertions
    assert scenario.policy.mode == "difference"
    assert runner.invoke(app, ["validate", str(output)]).exit_code == 0
    result = runner.invoke(
        app,
        [
            "verify",
            "--repo",
            str(repo),
            "--scenario",
            str(output),
            "--output",
            str(tmp_path / "run"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "No behavioral assertions were enforced" in " ".join(result.output.split())


def test_init_never_overwrites_existing_scenario(tmp_path):
    output = tmp_path / "scenario.yaml"
    output.write_bytes(b"precious existing configuration\n")
    result = CliRunner().invoke(app, ["init", "--adapter", "browser", "--output", str(output)])
    assert result.exit_code == 2
    assert "already exists" in result.output
    assert output.read_bytes() == b"precious existing configuration\n"


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--adapter", "unknown"],
        ["--adapter", "browser", "--entry", "../outside.html"],
        ["--adapter", "browser", "--entry", "https://example.invalid/app.html"],
        ["--adapter", "browser", "--", "unexpected-command"],
        ["--entry", "page.html", "--", "{python}", "app.py"],
        ["--adapter", "browser", "--duration-ms", "999", "--sample-ms", "250"],
    ],
)
def test_invalid_starters_do_not_leave_an_unusable_file(tmp_path, arguments):
    output = tmp_path / "scenario.yaml"
    result = CliRunner().invoke(app, ["init", "--output", str(output), *arguments])
    assert result.exit_code == 2, result.output
    assert not output.exists()


@pytest.mark.browser
@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RUN_BROWSER_TESTS") != "1", reason="requires Chromium")
def test_browser_starter_replays_plain_html_without_application_hooks(tmp_path):
    pytest.importorskip("playwright")
    repo, _ = history(tmp_path, count=1)
    (repo / "pages").mkdir()
    (repo / "pages" / "index.html").write_text(
        "<!doctype html><title>Starter fixture</title><h1>Ready to connect</h1><p>Local content</p>"
    )
    git(repo, "add", "pages")
    git(repo, "commit", "-m", "Add a plain HTML fixture")
    output = tmp_path / "browser.yaml"
    result = CliRunner().invoke(
        app,
        [
            "init",
            "--adapter",
            "browser",
            "--entry",
            "pages/index.html",
            "--output",
            str(output),
            "--duration-ms",
            "100",
            "--sample-ms",
            "100",
        ],
    )
    assert result.exit_code == 0, result.output
    scenario = load_scenario(output)
    run_dir = tmp_path / "run"
    run = Search.for_verification(repo, "HEAD", scenario, run_dir).run()
    assert run["status"] == "VERIFIED"
    trial = next(iter(run["trials"].values()))
    assert len(trial["captures"]) == 2
    trace = load_trace(run_dir / trial["captures"][1]["path"] / "trace.json")
    assert trace.samples[0].state["title"] == "Starter fixture"
    assert "Ready to connect" in trace.samples[0].state["text"]
    assert trace.samples[0].frame
    assert not scenario.assertions and not scenario.browser.actions
    assert scenario.browser.allow_network is False
