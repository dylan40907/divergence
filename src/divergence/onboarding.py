"""Non-executing scenario starters with explicit, limited behavioral coverage."""

from pathlib import Path, PurePosixPath

import yaml

from divergence.models import Scenario

DOM_SNAPSHOT = "({state: {title: document.title, text: document.body?.innerText ?? ''}})"


def create_scenario(
    destination: Path,
    *,
    adapter: str,
    command: list[str] | None = None,
    name: str = "My scenario",
    entry: str = "index.html",
    state_expression: str | None = None,
    duration_ms: int = 5000,
    sample_ms: int = 250,
) -> Scenario:
    """Write a valid starter once. Never run commands, inspect an app, or infer assertions."""
    if adapter not in {"command", "browser"}:
        raise ValueError("adapter must be command or browser")
    if adapter == "command":
        if not command or not command[0].strip():
            raise ValueError("command adapter needs a trace-producing argv after --")
        if entry != "index.html" or state_expression is not None:
            raise ValueError("--entry and --state-expression are only for browser scenarios")
        options = {"command": command}
        guide = (
            "# The command must implement the Divergence trace protocol; an arbitrary app will not.\n"
            "# It runs at the committed checkout root. Commands are argv lists, not shell strings.\n"
            "# {python} uses the active interpreter. No commands run during initialization.\n"
            "# Write trace.json to DIVERGENCE_OUTPUT using the timing in DIVERGENCE_SCENARIO.\n"
        )
    else:
        if command:
            raise ValueError(
                "browser scenarios do not accept command arguments; configure build in YAML"
            )
        normalized = entry.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized.strip()
            or path.is_absolute()
            or ".." in path.parts
            or any(character in normalized for character in (":", "?", "#"))
            or normalized in {".", "./"}
        ):
            raise ValueError(
                "browser entry must be a file path relative to the checkout, without .. or URL components"
            )
        if state_expression is not None and not state_expression.strip():
            raise ValueError("state expression cannot be blank")
        options = {
            "browser": {
                "entry": path.as_posix(),
                "reset_expression": "() => {}",
                "state_expression": state_expression or DOM_SNAPSHOT,
                "actions": [],
                "allow_network": False,
            }
        }
        guide = (
            "# Browser starter for local static files; add build commands if files must be compiled.\n"
            "# Use relative asset URLs. This does not start a backend or a development server.\n"
            "# No application-specific hooks are required; each replay opens a fresh browser.\n"
            "# The default snapshot reads page title and visible body text. Customize it for your app.\n"
            "# Screenshots, text, input values, and logs can contain secrets; review before sharing.\n"
            "# Add actions or use divergence record; an empty action list only observes the page.\n"
        )
    scenario = Scenario(
        name=name,
        adapter=adapter,
        duration_ms=duration_ms,
        sample_ms=sample_ms,
        policy={"mode": "difference"},
        **options,
    )
    payload = {
        "schema_version": 1,
        "name": scenario.name,
        "adapter": scenario.adapter,
        "seed": scenario.seed,
        "duration_ms": scenario.duration_ms,
        "sample_ms": scenario.sample_ms,
        "repeats": scenario.repeats,
        "timeout_seconds": scenario.timeout_seconds,
        "build": [],
        **options,
        "assertions": [],
        "policy": {
            "mode": "difference",
            "ignore_paths": [],
            "ignore_events": [],
            "ignore_logs": [],
        },
    }
    header = (
        "# Divergence scenario starter. Initialization does not execute code or install dependencies.\n"
        "# This starter checks REPEATABILITY ONLY, not application correctness.\n"
        "# Add assertions with your own path / op / value, then set policy.mode to assertions.\n"
        "# Review this file, run divergence validate, then divergence verify on committed source.\n"
        + guide
        + "\n"
    )
    content = header + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100)
    if len(content.encode("utf-8")) > 1024 * 1024:
        raise ValueError("scenario exceeds 1 MiB")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    except FileExistsError as exc:
        raise ValueError("scenario output already exists; choose a new --output path") from exc
    return scenario
