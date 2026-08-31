"""Validate saved evidence before reusing verdicts or publishing a portable report."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from divergence.artifacts import MIB, evidence_digest
from divergence.models import Comparison, Scenario, load_trace, safe_child


def read_run(directory: Path) -> tuple[dict, Scenario]:
    manifest = directory / "run.json"
    if manifest.stat().st_size > 64 * MIB:
        raise ValueError("run manifest exceeds 64 MiB; narrow the investigation")

    def reject_constant(value):
        raise ValueError(f"nonfinite value in run manifest: {value}")

    state = json.loads(manifest.read_text(encoding="utf-8"), parse_constant=reject_constant)
    try:
        if state["schema_version"] != 1:
            raise ValueError("unsupported run schema")
        scenario = Scenario.model_validate(state["scenario"])
        # Hash the saved representation: older schema-1 defaults used integer 0/1
        # where current validation normalizes typed float fields to 0.0/1.0.
        saved_payload = json.dumps(state["scenario"], sort_keys=True, separators=(",", ":"))
        if (
            state["identity"]["scenario_digest"]
            != hashlib.sha256(saved_payload.encode()).hexdigest()
        ):
            raise ValueError("saved scenario does not match its recorded digest")
        if not isinstance(state["trials"], dict) or not isinstance(state["history"], list):
            raise ValueError("run trials/history have invalid shapes")
        history = [item["sha"] for item in state["history"]]
        if not history or len(set(history)) != len(history):
            raise ValueError("run history must contain unique revisions")
        if state["identity"]["good"] != history[0] or state["identity"]["bad"] != history[-1]:
            raise ValueError("run endpoints do not match its history")
        for sha, trial in state["trials"].items():
            index = trial["index"]
            if type(index) is not int or not 0 <= index < len(history):
                raise ValueError("invalid saved history index")
            if sha != trial["sha"] or history[index] != sha:
                raise ValueError("saved trial does not match its history position")
        if not isinstance(state["limitations"], list) or not all(
            isinstance(item, str) for item in state["limitations"]
        ):
            raise ValueError("invalid run limitations")
        if not all(isinstance(state[key], str) for key in ("status", "created_at", "version")):
            raise ValueError("run status/date/version must be strings")
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError(f"invalid run manifest: {exc}") from exc
    return state, scenario


def validate_attempt(directory: Path, attempt: dict, scenario: Scenario) -> int:
    """Hash every completed capture, including modalities not displayed by the report."""
    try:
        verdict = attempt["verdict"]
        captures = attempt["captures"]
        repeat_verdicts = attempt["repeat_verdicts"]
        if verdict not in {"GOOD", "BAD", "INCONCLUSIVE"}:
            raise ValueError("invalid saved verdict")
        if not isinstance(captures, list) or not isinstance(repeat_verdicts, list):
            raise ValueError("invalid saved repeats")
        if any(value not in {"GOOD", "BAD", "INCONCLUSIVE"} for value in repeat_verdicts):
            raise ValueError("invalid repeated verdict")
        if verdict != "INCONCLUSIVE" and (
            len(captures) != scenario.repeats or repeat_verdicts != [verdict] * scenario.repeats
        ):
            raise ValueError("saved verdict lacks the required consistent repeats")
        Comparison.model_validate(attempt["comparison"])
        seen = set()
        for capture in captures:
            if not isinstance(capture["path"], str) or not re.fullmatch(
                "[0-9a-f]{64}", capture["digest"]
            ):
                raise ValueError("invalid saved capture path or digest")
            folder = safe_child(directory, capture["path"])
            if folder in seen:
                raise ValueError("independent repeats cannot refer to the same capture")
            seen.add(folder)
            digest = evidence_digest(
                folder,
                max_bytes=scenario.capture_limits.max_mb * MIB,
                max_entries=scenario.capture_limits.max_entries,
            )
            if digest != capture["digest"]:
                raise ValueError(
                    f"saved evidence changed: {capture['path']}; do not reuse its verdict"
                )
            load_trace(folder / "trace.json").validate_for(scenario, folder)
        return len(captures)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"invalid saved trial: {exc}") from exc


def validate_saved_trials(directory: Path, state: dict, scenario: Scenario) -> int:
    count = 0
    for trial in state["trials"].values():
        prior = trial.get("previous_attempts", [])
        if not isinstance(prior, list):
            raise ValueError("invalid retained-attempt ledger")
        for attempt in [*prior, trial]:
            count += validate_attempt(directory, attempt, scenario)
    return count
