"""Adapters produce the same Trace contract, independently of revision control."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Protocol

from divergence.models import Scenario, Trace, load_trace
from divergence.process import ExecutionError, run_command
from divergence.storage import atomic_json


class Adapter(Protocol):
    def capture(self, source: Path, output: Path, scenario: Scenario) -> Trace: ...


class CommandAdapter:
    def capture(self, source: Path, output: Path, scenario: Scenario) -> Trace:
        output.mkdir(parents=True, exist_ok=False)
        config = output / "scenario.json"
        atomic_json(config, scenario.model_dump())
        environment = {
            "DIVERGENCE_OUTPUT": str(output.resolve()),
            "DIVERGENCE_SCENARIO": str(config.resolve()),
            "DIVERGENCE_SEED": str(scenario.seed),
            "PYTHONHASHSEED": str(scenario.seed),
        }
        run_command(
            scenario.command,
            source,
            scenario.timeout_seconds,
            output / "process.log",
            environment,
            capture_dir=output,
            capture_max_mb=scenario.capture_limits.max_mb,
            capture_max_entries=scenario.capture_limits.max_entries,
        )
        trace = load_trace(output / "trace.json")
        trace.validate_for(scenario, output)
        return trace


class BrowserAdapter:
    def capture(self, source: Path, output: Path, scenario: Scenario) -> Trace:
        output.mkdir(parents=True, exist_ok=False)
        config = output / "scenario.json"
        atomic_json(config, scenario.model_dump())
        # A child process puts browser startup, JS, and teardown under a hard timeout.
        try:
            run_command(
                [
                    sys.executable,
                    "-m",
                    "divergence.adapters.browser",
                    str(source.resolve()),
                    str(output.resolve()),
                    str(config.resolve()),
                ],
                source,
                scenario.timeout_seconds,
                output / "process.log",
                capture_dir=output,
                capture_max_mb=scenario.capture_limits.max_mb,
                capture_max_entries=scenario.capture_limits.max_entries,
            )
        except ExecutionError as exc:
            diagnostic = output / "diagnostic.json"
            if diagnostic.is_file() and diagnostic.stat().st_size <= 32768:
                try:
                    message = json.loads(diagnostic.read_text(encoding="utf-8")).get("message")
                except (ValueError, OSError, AttributeError):
                    message = None
                if isinstance(message, str) and message:
                    raise ExecutionError(
                        f"{message}\nWorker log: {output / 'process.log'}"
                    ) from exc
            raise
        trace = load_trace(output / "trace.json")
        trace.validate_for(scenario, output)
        return trace


def adapter_for(scenario: Scenario) -> Adapter:
    return BrowserAdapter() if scenario.adapter == "browser" else CommandAdapter()
