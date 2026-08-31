from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from divergence import __version__
from divergence.models import load_scenario, load_trace

app = typer.Typer(
    no_args_is_help=True,
    help="Find regressions in Git history using reproducible runtime evidence.",
    pretty_exceptions_enable=False,
)
console = Console()


def fresh_output() -> Path:
    return Path(".divergence/runs") / (
        datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    )


def progress(event: dict):
    if event["kind"] == "testing":
        console.print(
            f"Testing {event['sha'][:8]} · revision {event['index'] + 1}/{event['total']}",
            style="cyan",
            markup=False,
        )
    elif event["kind"] == "tested":
        color = {"GOOD": "green", "BAD": "red", "INCONCLUSIVE": "yellow"}[event["verdict"]]
        first = event["comparison"]["first_divergence_ms"]
        when = f" · first observed divergence {first / 1000:.2f}s" if first is not None else ""
        console.print(
            f"  {event['verdict']} · {event['seconds']:.1f}s{when}", style=color, markup=False
        )
        for error in event["errors"]:
            console.print(f"  {error}", style="yellow", markup=False)


def execute_search(
    repository: Path,
    good: str,
    bad: str,
    scenario: Path,
    output: Path,
    resume: bool = False,
    model: Path | None = None,
    audit: bool = False,
    retry_inconclusive: bool = False,
    verify_only: bool = False,
) -> dict:
    from divergence.engine import Search
    from divergence.report import render_report

    config = load_scenario(scenario)
    if model:
        from divergence.models import LinearFusion

        config.policy.fusion = LinearFusion.model_validate_json(model.read_text(encoding="utf-8"))
        config.policy.mode = "learned"
    console.print(
        "Running trusted application code in disposable checkouts. This is not a security sandbox.",
        style="dim",
    )
    search = Search(
        repository, good, bad, config, output, progress, audit=audit, verify_only=verify_only
    )
    result = search.run(resume=resume, retry_inconclusive=retry_inconclusive)
    report = render_report(output)
    console.print(f"\n{result['status']}: {result['message']}", style="bold", markup=False)
    if result.get("culprit"):
        console.print(f"First bad commit: {result['culprit']}", markup=False)
    if result["status"] == "AMBIGUOUS":
        for sha in result["candidates"]:
            console.print(f"  possible: {sha}", markup=False)
    console.print(f"Report: {report.resolve()}", markup=False)
    return result


@app.command()
def doctor():
    """Check local runtimes without installing anything or contacting external services."""
    table = Table("Component", "Status")
    table.add_row("Divergence", __version__)
    table.add_row("Python", platform.python_version())
    table.add_row("Git", shutil.which("git") or "Missing (required)")
    for package in ("playwright", "pygame", "sklearn"):
        table.add_row(
            package,
            "Available" if importlib.util.find_spec(package) else "Not installed (optional)",
        )
    if importlib.util.find_spec("playwright"):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            path = Path(pw.chromium.executable_path)
            table.add_row(
                "Chromium",
                str(path) if path.exists() else "Missing: python -m playwright install chromium",
            )
            if path.exists():
                try:
                    browser = pw.chromium.launch(headless=True)
                    browser.close()
                    table.add_row("Browser launch", "Verified")
                except Exception as exc:
                    table.add_row("Browser launch", f"Unavailable: {str(exc).splitlines()[0]}")
    table.add_row(
        "Godot", shutil.which("godot") or shutil.which("godot4") or "Not installed (optional)"
    )
    console.print(table)


@app.command()
def validate(scenario: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]):
    """Validate a scenario without executing its commands."""
    try:
        config = load_scenario(scenario)
    except (ValueError, OSError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(2) from exc
    console.print(f"Valid: {config.name}\nScenario digest: {config.digest}", markup=False)


@app.command()
def verify(
    scenario: Annotated[
        Path, typer.Option(exists=True, dir_okay=False, help="Trusted scenario YAML.")
    ],
    revision: Annotated[
        str, typer.Option(help="Committed Git revision to replay; excludes uncommitted work.")
    ] = "HEAD",
    repo: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path("."),
    output: Annotated[
        Path | None, typer.Option(help="Run directory (new unless --resume).")
    ] = None,
    resume: Annotated[
        bool, typer.Option(help="Reuse validated completed evidence in --output.")
    ] = False,
    retry_inconclusive: Annotated[
        bool,
        typer.Option(
            help="With --resume, retry execution failures once, retaining prior evidence."
        ),
    ] = False,
):
    """Check replay, assertions, and repeatability at one revision. Executes trusted code."""
    if resume and output is None:
        console.print("--resume requires the original --output directory", style="red")
        raise typer.Exit(2)
    try:
        result = execute_search(
            repo,
            revision,
            revision,
            scenario,
            output or fresh_output(),
            resume=resume,
            retry_inconclusive=retry_inconclusive,
            verify_only=True,
        )
    except (ValueError, RuntimeError, OSError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(2) from exc
    raise typer.Exit(0 if result["status"] == "VERIFIED" else 3)


@app.command()
def bisect(
    scenario: Annotated[
        Path, typer.Option(exists=True, dir_okay=False, help="Trusted scenario YAML.")
    ],
    good: Annotated[str, typer.Option(help="Known-good Git revision.")],
    bad: Annotated[str, typer.Option(help="Known-bad Git revision.")] = "HEAD",
    repo: Annotated[Path, typer.Option(exists=True, file_okay=False)] = Path("."),
    output: Annotated[
        Path | None, typer.Option(help="Run directory (new unless --resume).")
    ] = None,
    resume: Annotated[
        bool, typer.Option(help="Reuse validated completed trials in --output.")
    ] = False,
    retry_inconclusive: Annotated[
        bool,
        typer.Option(
            help="With --resume, retry execution failures once, retaining prior evidence. Unstable results are not retried."
        ),
    ] = False,
    model: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            dir_okay=False,
            help="Experimental learned fusion JSON. Explicit assertions still take precedence.",
        ),
    ] = None,
    audit: Annotated[
        bool,
        typer.Option(
            help="Test every revision to check for fixed-and-reintroduced regressions (slower)."
        ),
    ] = False,
):
    """Locate the first bad commit on a first-parent history. Executes trusted code."""
    if resume and output is None:
        console.print("--resume requires the original --output directory", style="red")
        raise typer.Exit(2)
    try:
        result = execute_search(
            repo,
            good,
            bad,
            scenario,
            output or fresh_output(),
            resume,
            model,
            audit,
            retry_inconclusive,
        )
    except (ValueError, RuntimeError, OSError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(2) from exc
    raise typer.Exit(0 if result["status"] == "FOUND" else 3)


@app.command()
def demo(
    output: Annotated[
        Path, typer.Option(help="New directory for the generated demo repository.")
    ] = Path("demo-workspace"),
    commits: Annotated[int, typer.Option(min=2, max=512)] = 128,
    bug_at: Annotated[int, typer.Option(min=2, help="1-based bug introduction commit.")] = 83,
    adapter: Annotated[str, typer.Option(help="command, browser, pygame, or godot")] = "command",
    fixture: Annotated[str, typer.Option(help="interlock or cart (browser only)")] = "interlock",
    godot: Annotated[
        str, typer.Option(help="Godot executable path for --adapter godot.")
    ] = "godot",
    run: Annotated[
        bool, typer.Option("--run", help="Immediately bisect the generated history.")
    ] = False,
):
    """Create a local fixture with a real, intentionally regressed Git history."""
    from divergence.demo import create_demo

    if adapter not in {"command", "browser", "pygame", "godot"}:
        console.print("adapter must be command, browser, pygame, or godot", style="red")
        raise typer.Exit(2)
    try:
        console.print(f"Creating {commits} real commits; bug introduced at revision {bug_at}.")
        if adapter == "godot" and Path(godot).is_file():
            godot = str(Path(godot).resolve())
        result = create_demo(
            output, commits=commits, bug_at=bug_at, adapter=adapter, godot=godot, fixture=fixture
        )
        console.print(
            f"Demo ready: {result['repository']}\nScenario: {result['scenario']}", markup=False
        )
        if run:
            outcome = execute_search(
                Path(result["repository"]),
                "known-good",
                "known-bad",
                Path(result["scenario"]),
                fresh_output(),
            )
            if outcome.get("culprit") != result["expected_culprit"]:
                raise RuntimeError(
                    "demo did not locate its expected culprit; inspect the report diagnostics"
                )
    except (ValueError, RuntimeError, OSError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(2) from exc


@app.command("compare")
def compare_traces(
    reference: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    candidate: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    scenario: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    output: Annotated[
        Path | None, typer.Option(help="Write machine-readable comparison JSON.")
    ] = None,
):
    """Compare two captured trace.json files without running application code."""
    from divergence.oracle import compare
    from divergence.storage import atomic_json

    try:
        config = load_scenario(scenario)
        a, b = load_trace(reference), load_trace(candidate)
        a.validate_for(config, reference.parent)
        b.validate_for(config, candidate.parent)
        result = compare(a, b, config, reference.parent, candidate.parent)
        if output:
            atomic_json(output, result.model_dump())
        console.print_json(result.model_dump_json())
        raise typer.Exit({"GOOD": 0, "BAD": 1, "INCONCLUSIVE": 3}[result.verdict])
    except (ValueError, OSError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(2) from exc


@app.command()
def report(
    run_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    output: Annotated[Path | None, typer.Option()] = None,
):
    """Regenerate a portable HTML report from local run evidence."""
    from divergence.report import render_report

    try:
        console.print(str(render_report(run_dir, output).resolve()), markup=False)
    except (ValueError, OSError, KeyError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(2) from exc


@app.command()
def benchmark(
    output: Annotated[Path, typer.Option()] = Path(".divergence/benchmark"),
    variants: Annotated[int, typer.Option(min=2, max=20)] = 4,
):
    """Capture generated application mutations for reproducible synthetic ML evaluation."""
    from divergence.benchmark import generate

    try:
        dataset = generate(output, variants, lambda message: console.print(message, markup=False))
        console.print(f"Dataset: {dataset.resolve()}", markup=False)
    except (ValueError, RuntimeError, OSError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(2) from exc


@app.command("record")
def record_scenario(
    repo: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    scenario: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    output: Annotated[
        Path, typer.Option(help="New YAML path; existing files are never overwritten.")
    ],
):
    """Open a local browser and record actions on the configured scenario clock."""
    from divergence.recorder import record

    try:
        config = load_scenario(scenario)
        console.print(
            f"Recording for {config.duration_ms / 1000:g} seconds. Interact with the opened application."
        )
        console.print(
            "Input values are saved in the scenario. Do not enter secrets. Password fields are excluded.",
            style="yellow",
        )
        recorded = record(repo, config, output)
        console.print(
            f"Saved {len(recorded.browser.actions)} actions to {output.resolve()}", markup=False
        )
        console.print(
            "Review selectors and assertions, then validate and replay the recording before bisection."
        )
    except (ValueError, RuntimeError, OSError, ImportError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(2) from exc


@app.command("capture")
def capture_revision(
    repo: Annotated[Path, typer.Option(exists=True, file_okay=False)],
    scenario: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option(help="New capture directory.")],
    revision: Annotated[str, typer.Option()] = "HEAD",
):
    """Capture one Git revision in an isolated checkout before configuring a search."""
    from divergence.adapters import adapter_for
    from divergence.engine import run_lock
    from divergence.process import run_command
    from divergence.revisions import GitProvider

    try:
        config = load_scenario(scenario)
        output = output.resolve()
        with run_lock(output):
            if any(path.name != ".lock" for path in output.iterdir()):
                raise ValueError("capture requires an empty output directory")
            provider = GitProvider(repo, output)
            sha = provider.resolve(revision)
            provider.prepare()
            source = provider.checkout(sha, "capture")
            try:
                for index, command in enumerate(config.build):
                    run_command(
                        command, source, config.timeout_seconds, output / f"build-{index}.log"
                    )
                trace = adapter_for(config).capture(source, output / "artifacts", config)
            finally:
                provider.release(source)
        console.print(
            f"Captured {len(trace.samples)} samples at {sha[:8]}\nTrace: {output / 'artifacts/trace.json'}",
            markup=False,
        )
    except (ValueError, RuntimeError, OSError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(2) from exc


@app.command("train")
def train_model(
    dataset: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option()] = Path(".divergence/model"),
):
    """Evaluate with held-out mutation families and export an inspectable JSON fusion model."""
    from divergence.fusion import train

    try:
        evaluation = train(dataset, output)
        console.print_json(data=evaluation)
        console.print(f"Model: {(output / 'model.json').resolve()}", markup=False)
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(2) from exc


@app.command()
def serve(
    root: Annotated[Path, typer.Option()] = Path(".divergence/runs"),
    port: Annotated[int, typer.Option(min=1024, max=65535)] = 8741,
    open_browser: Annotated[bool, typer.Option("--open")] = False,
):
    """Browse saved reports on loopback. Read-only; no remote execution endpoints."""
    from divergence.server import make_server

    try:
        server = make_server(root, port)
    except OSError as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(2) from exc
    url = f"http://127.0.0.1:{server.server_port}"
    console.print(f"Divergence reports: {url}\nCtrl+C to stop.", markup=False)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@app.callback()
def configure():
    # Optional project-local browser install, without changing the global environment.
    local_browsers = Path(".tools/browsers")
    if local_browsers.is_dir() and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browsers.resolve())
