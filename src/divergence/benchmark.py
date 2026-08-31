"""Capture real fixture executions for repeatable, explicitly synthetic evaluation."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from divergence.adapters import CommandAdapter
from divergence.models import Scenario
from divergence.oracle import compare
from divergence.storage import atomic_json


def generate(output: Path, variants: int = 4, progress: Callable[[str], None] = print) -> Path:
    if output.exists() and any(output.iterdir()):
        raise ValueError("benchmark directory must be new or empty")
    if not 2 <= variants <= 20:
        raise ValueError("variants must be between 2 and 20")
    output.mkdir(parents=True, exist_ok=True)
    template = Path(__file__).parent / "demo_templates" / "python"
    original = (template / "app.py").read_text(encoding="utf-8")
    door_source = (template / "door.py").read_text(encoding="utf-8")
    families = [
        "collision",
        "slow_motion",
        "late_switch",
        "missing_event",
        "error_log",
        "visual_loss",
    ]
    examples = []
    for family in families:
        for variant in range(variants):
            identifier = f"{family}-{variant}"
            progress(f"Capturing benchmark {identifier}")
            root = output / identifier
            source = root / "source"
            shutil.copytree(template, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            config = Scenario(
                name=identifier,
                command=["{python}", "app.py"],
                duration_ms=10000,
                sample_ms=500,
                seed=42 + variant,
                policy={"mode": "difference", "ignore_paths": ["clock_ms"]},
            )
            # Vary initial state consistently across reference/control/mutation.
            base = original.replace("x = 140.0", f"x = {130.0 + variant * 7}")
            (source / "app.py").write_text(base, encoding="utf-8")
            reference_dir = root / "reference"
            reference = CommandAdapter().capture(source, reference_dir, config)
            # A benign visual skin change is allowed by fixture ground truth.
            control = base.replace('"#778ca7"', f'"#{120 + variant:02x}8ca7"')
            (source / "app.py").write_text(control, encoding="utf-8")
            control_dir = root / "control"
            benign = CommandAdapter().capture(source, control_dir, config)
            clean_result = compare(reference, benign, config, reference_dir, control_dir)
            mutation = base
            if family == "collision":
                (source / "door.py").write_text(
                    door_source.replace(
                        "self.collider_enabled = False", "self.collider_enabled = True"
                    ),
                    encoding="utf-8",
                )
            elif family == "slow_motion":
                mutation = base.replace("* 0.11", f"* {0.025 + variant * 0.008}")
            elif family == "late_switch":
                mutation = base.replace("t == 3000", f"t == {6000 + variant * 500}")
            elif family == "missing_event":
                mutation = base.replace('events.append("DoorOpened")', "events.extend([])")
            elif family == "error_log":
                mutation = base.replace(
                    '"logs": []',
                    '"logs": ([{"level":"error","message":"Navigation invariant violated"}] if t >= 3000 else [])',
                )
            elif family == "visual_loss":
                mutation = base.replace(
                    'fill="#8f9bff", outline="#c4caff"', 'fill="#182334", outline="#182334"'
                )
            (source / "app.py").write_text(mutation, encoding="utf-8")
            # Prevent same-second Python bytecode cache reuse between source mutations.
            cache = source / "__pycache__"
            if cache.exists():
                for item in cache.glob("*.pyc"):
                    item.unlink()
            mutated_dir = root / "mutation"
            mutated = CommandAdapter().capture(source, mutated_dir, config)
            result = compare(reference, mutated, config, reference_dir, mutated_dir)
            for label, kind, comparison in [(0, "control", clean_result), (1, "mutation", result)]:
                examples.append(
                    {
                        "id": f"{identifier}-{kind}",
                        "group": family,
                        "label": label,
                        "features": comparison.features,
                        "rule_verdict": comparison.verdict,
                        "reference": reference_dir.relative_to(output).as_posix(),
                        "candidate": (root / kind).relative_to(output).as_posix(),
                    }
                )
    dataset = output / "dataset.json"
    atomic_json(
        dataset,
        {
            "schema_version": 1,
            "feature_version": 2,
            "provenance": "Real executions of generated, labeled application mutations. Synthetic dataset.",
            "examples": examples,
        },
    )
    return dataset
