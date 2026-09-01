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
import yaml
from rich.console import Console
from rich.table import Table

from divergence import __version__
from divergence.models import load_scenario, load_trace

app = typer.Typer(
    no_args_is_help=True,
    help="Find regressions in Git or Perforce history using reproducible runtime evidence.",
    pretty_exceptions_enable=False,
)
console = Console()


def fresh_output(scm: str = "git") -> Path:
    if scm == "perforce":
        state_root = Path(
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("XDG_STATE_HOME")
            or (Path.home() / ".local/state")
        )
        root = state_root / "Divergence/runs"
    else:
        root = Path(".divergence/runs")
    return root / (
        datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    )


def progress(event: dict):
    if event["kind"] == "testing":
        noun = "changelist" if event.get("provider") == "perforce" else "revision"
        console.print(
            f"Testing {event['sha'][:12]} · {noun} {event['index'] + 1}/{event['total']}",
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
    scm: str = "git",
    p4_client: str | None = None,
    p4_filespec: str | None = None,
    p4_executable: str = "p4",
) -> dict:
    from divergence.engine import Search
    from divergence.report import render_report
    from divergence.revisions import PerforceProvider

    config = load_scenario(scenario)
    if model:
        from divergence.models import LinearFusion

        config.policy.fusion = LinearFusion.model_validate_json(model.read_text(encoding="utf-8"))
        config.policy.mode = "learned"
    if scm not in {"git", "perforce"}:
        raise ValueError("--scm must be git or perforce")
    if scm == "git" and (p4_client or p4_filespec or p4_executable != "p4"):
        raise ValueError("Perforce options require --scm perforce")
    provider = (
        PerforceProvider(
            repository,
            output,
            client=p4_client,
            filespec=p4_filespec,
            executable=p4_executable,
        )
        if scm == "perforce"
        else None
    )
    console.print(
        "Running trusted application code in an isolated revision workspace. This is not a security sandbox.",
        style="dim",
    )
    search = Search(
        repository,
        good,
        bad,
        config,
        output,
        progress,
        audit=audit,
        verify_only=verify_only,
        provider=provider,
    )
    result = search.run(resume=resume, retry_inconclusive=retry_inconclusive)
    report = render_report(output)
    console.print(f"\n{result['status']}: {result['message']}", style="bold", markup=False)
    if result.get("culprit"):
        noun = "changelist" if scm == "perforce" else "commit"
        console.print(f"First bad {noun}: {result['culprit']}", markup=False)
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
    table.add_row("Perforce", shutil.which("p4") or "Not installed (required only for Perforce/UGS)")
    table.add_row("UnrealGameSync", shutil.which("UnrealGameSync") or "Not on PATH (optional)")
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


@app.command("perforce-check")
def perforce_check(
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, help="Path inside the mapped UGS workspace."),
    ] = Path("."),
    p4_client: Annotated[
        str | None, typer.Option(help="UGS/Perforce client; auto-detected by default.")
    ] = None,
    p4_filespec: Annotated[
        str | None, typer.Option(help="History scope; defaults to the complete selected client view.")
    ] = None,
    p4_executable: Annotated[str, typer.Option(help="Perforce CLI executable.")] = "p4",
):
    """Read Perforce connection/view metadata without syncing or changing a workspace."""
    from divergence.revisions import PerforceProvider

    try:
        provider = PerforceProvider(
            repo,
            fresh_output("perforce").parent / "connection-probe",
            client=p4_client,
            filespec=p4_filespec,
            executable=p4_executable,
        )
        details = provider.inspect()
        details["latest_visible_change"] = provider.resolve("HEAD")
    except (ValueError, RuntimeError, OSError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(2) from exc
    table = Table("Perforce setting", "Value")
    for key, value in details.items():
        table.add_row(key.replace("_", " ").title(), str(value))
    console.print(table)
    console.print(
        "Read-only check complete. A real run creates a separate temporary client under its output directory.",
        style="green",
    )


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
def init(
    command: Annotated[
        list[str] | None,
        typer.Argument(help="For command adapters, pass a trace-producing argv after --."),
    ] = None,
    adapter: Annotated[str, typer.Option(help="command or browser")] = "command",
    output: Annotated[
        Path, typer.Option(help="New scenario YAML path; never overwrites an existing file.")
    ] = Path("divergence.yaml"),
    name: Annotated[str, typer.Option(help="Name shown in reports.")] = "My scenario",
    entry: Annotated[
        str, typer.Option(help="Browser entry file relative to the committed checkout.")
    ] = "index.html",
    state_expression: Annotated[
        str | None,
        typer.Option(help="Browser snapshot expression; defaults to title and visible text."),
    ] = None,
    duration_ms: Annotated[int, typer.Option(min=100, max=300000)] = 5000,
    sample_ms: Annotated[int, typer.Option(min=16, max=10000)] = 250,
):
    """Write a scenario starter without executing code or installing dependencies."""
    from divergence.onboarding import create_scenario

    try:
        create_scenario(
            output,
            adapter=adapter,
            command=command,
            name=name,
            entry=entry,
            state_expression=state_expression,
            duration_ms=duration_ms,
            sample_ms=sample_ms,
        )
    except (ValueError, OSError) as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(2) from exc
    console.print(f"Created: {output.resolve()}", style="green", markup=False)
    console.print(
        "Repeatability only: no behavioral assertions or replay actions were inferred. "
        "Add the behavior you want checked before using this to judge regressions.",
        style="yellow",
    )
    if adapter == "command":
        console.print(
            "The command must write a Divergence trace; wrapping an arbitrary app is not enough."
        )
    else:
        console.print(
            "The browser serves local static files. Customize snapshots/actions; captured text and images may contain secrets."
        )
    console.print(
        "Next: review the YAML, validate it, then verify a committed revision. Nothing was executed."
    )


@app.command("unreal-init")
def unreal_init(
    project: Annotated[
        str, typer.Option(help=".uproject path relative to the UGS workspace root.")
    ],
    test: Annotated[str, typer.Option(help="Unreal Automation test path/filter.")],
    editor: Annotated[
        str,
        typer.Option(help="Absolute Editor-Cmd executable, PATH name, or workspace-relative path."),
    ] = "UnrealEditor-Cmd.exe",
    output: Annotated[
        Path, typer.Option(help="New scenario YAML path; never overwrites an existing file.")
    ] = Path("divergence-unreal.yaml"),
    name: Annotated[str, typer.Option(help="Scenario name shown in reports.")] = "Unreal automation",
    timeout_seconds: Annotated[int, typer.Option(min=1, max=3600)] = 900,
    null_rhi: Annotated[
        bool, typer.Option(help="Run with -nullrhi; disable for screenshot/rendering tests.")
    ] = False,
):
    """Create a Perforce-ready Unreal Automation scenario without running the project."""
    if output.exists():
        console.print("scenario output already exists; choose a new path", style="red")
        raise typer.Exit(2)
    if Path(project).is_absolute() or Path(project).suffix.lower() != ".uproject":
        console.print("--project must be a workspace-relative .uproject path", style="red")
        raise typer.Exit(2)
    if not test or any(character in test for character in (';', '"', "'", "\n", "\r")):
        console.print("--test contains unsupported Unreal command characters", style="red")
        raise typer.Exit(2)
    command = [
        "{python}",
        "-m",
        "divergence.unreal",
        "--editor",
        editor,
        "--project",
        project,
        "--test",
        test,
        *(["--null-rhi"] if null_rhi else []),
    ]
    scenario = {
        "schema_version": 1,
        "name": name,
        "adapter": "command",
        "command": command,
        "build": [],
        "seed": 42,
        "duration_ms": 1000,
        "sample_ms": 100,
        "timeout_seconds": timeout_seconds,
        "repeats": 2,
        "assertions": [
            {"path": "unreal.total", "op": "gt", "value": 0, "at": "end"},
            {"path": "unreal.failed", "op": "eq", "value": 0, "at": "end"},
            {"path": "unreal.not_run", "op": "eq", "value": 0, "at": "end"},
            {"path": "unreal.in_process", "op": "eq", "value": 0, "at": "end"},
        ],
        "policy": {"mode": "assertions"},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")
    console.print(f"Created: {output.resolve()}", style="green", markup=False)
    console.print(
        "No project or Perforce data was changed. Add trusted build commands if the selected client view does not provide runnable binaries, then validate and verify a submitted changelist.",
        style="yellow",
    )


@app.command()
def verify(
    scenario: Annotated[
        Path, typer.Option(exists=True, dir_okay=False, help="Trusted scenario YAML.")
    ],
    revision: Annotated[
        str, typer.Option(help="Git revision or submitted Perforce changelist to replay.")
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
    scm: Annotated[str, typer.Option(help="Revision provider: git or perforce.")] = "git",
    p4_client: Annotated[
        str | None, typer.Option(help="UGS/Perforce client view to clone; auto-detected by default.")
    ] = None,
    p4_filespec: Annotated[
        str | None,
        typer.Option(help="Perforce history scope, for example //client/Project/...."),
    ] = None,
    p4_executable: Annotated[str, typer.Option(help="Perforce CLI executable.")] = "p4",
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
            output or fresh_output(scm),
            resume=resume,
            retry_inconclusive=retry_inconclusive,
            verify_only=True,
            scm=scm,
            p4_client=p4_client,
            p4_filespec=p4_filespec,
            p4_executable=p4_executable,
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
    good: Annotated[str, typer.Option(help="Known-good Git revision or Perforce changelist.")],
    bad: Annotated[str, typer.Option(help="Known-bad Git revision or Perforce changelist.")] = "HEAD",
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
    scm: Annotated[str, typer.Option(help="Revision provider: git or perforce.")] = "git",
    p4_client: Annotated[
        str | None, typer.Option(help="UGS/Perforce client view to clone; auto-detected by default.")
    ] = None,
    p4_filespec: Annotated[
        str | None,
        typer.Option(help="Perforce history scope, for example //client/Project/...."),
    ] = None,
    p4_executable: Annotated[str, typer.Option(help="Perforce CLI executable.")] = "p4",
):
    """Locate the first bad revision in a Git or Perforce history. Executes trusted code."""
    if resume and output is None:
        console.print("--resume requires the original --output directory", style="red")
        raise typer.Exit(2)
    try:
        result = execute_search(
            repo,
            good,
            bad,
            scenario,
            output or fresh_output(scm),
            resume,
            model,
            audit,
            retry_inconclusive,
            scm=scm,
            p4_client=p4_client,
            p4_filespec=p4_filespec,
            p4_executable=p4_executable,
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
