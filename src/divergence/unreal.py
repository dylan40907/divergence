"""Capture an Unreal Automation test report as a strict Divergence trace."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path

from divergence.models import Scenario
from divergence.process import run_command
from divergence.storage import atomic_json


def _inside(root: Path, relative: str, suffix: str | None = None) -> Path:
    if Path(relative).is_absolute():
        raise ValueError("Unreal project/editor paths inside the workspace must be relative")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or (suffix and path.suffix.lower() != suffix):
        raise ValueError("Unreal path escapes the isolated revision workspace or has the wrong type")
    return path


def capture(editor: str, project: str, test: str, extra: list[str], null_rhi: bool) -> None:
    source = Path.cwd().resolve()
    output = Path(os.environ["DIVERGENCE_OUTPUT"]).resolve()
    scenario_path = Path(os.environ["DIVERGENCE_SCENARIO"]).resolve()
    scenario = Scenario.model_validate_json(scenario_path.read_text(encoding="utf-8"))
    if not re.fullmatch(r"[A-Za-z0-9_.:+\-\[\] ]{1,240}", test):
        raise ValueError("Unreal test name contains unsupported command characters")
    project_path = _inside(source, project, ".uproject")
    if not project_path.is_file():
        raise ValueError(f"Unreal project is missing at this revision: {project}")
    editor_path = Path(editor)
    if editor_path.is_absolute():
        executable = str(editor_path.resolve())
    else:
        candidate = _inside(source, editor)
        executable = str(candidate) if candidate.is_file() else (shutil.which(editor) or "")
    if not executable or not Path(executable).is_file():
        raise ValueError("Unreal Editor command executable was not found")
    report = output / "unreal-report"
    report.mkdir()
    argv = [
        executable,
        str(project_path),
        f"-ExecCmds=Automation RunTests {test};Quit",
        f"-ReportExportPath={report}",
        "-unattended",
        "-nop4",
        "-nosplash",
        "-stdout",
        "-FullStdOutLogOutput",
        *(["-nullrhi"] if null_rhi else []),
        *extra,
    ]
    run_command(argv, source, scenario.timeout_seconds, output / "unreal-editor.log")
    result_path = report / "index.json"
    if not result_path.is_file() or result_path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Unreal did not produce a bounded index.json automation report")
    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict) or not isinstance(result.get("tests"), list):
        raise ValueError("Unreal automation report has an unsupported shape")
    counts = {}
    for key in ("succeeded", "succeededWithWarnings", "failed", "notRun", "inProcess"):
        value = result.get(key, 0)
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"Unreal automation report has invalid {key}")
        counts[key] = value
    total = len(result["tests"])
    if total > 10000:
        raise ValueError("Unreal automation report contains too many test records")
    if sum(counts.values()) != total:
        raise ValueError("Unreal automation report counts do not match its test records")
    failures = []
    warnings = []
    for item in result["tests"]:
        if not isinstance(item, dict):
            raise ValueError("Unreal automation report contains an invalid test record")
        name = str(item.get("fullTestPath") or item.get("testDisplayName") or "unnamed")[:500]
        state = str(item.get("state") or "Unknown")
        if state.lower() in {"fail", "failed", "error"}:
            failures.append(name)
        warning_count = item.get("warnings") or 0
        if not isinstance(warning_count, int) or warning_count < 0:
            raise ValueError("Unreal automation report contains an invalid warning count")
        if warning_count:
            warnings.append(name)
    state = {
        "unreal": {
            "total": total,
            "succeeded": counts["succeeded"],
            "succeeded_with_warnings": counts["succeededWithWarnings"],
            "failed": counts["failed"],
            "not_run": counts["notRun"],
            "in_process": counts["inProcess"],
        }
    }
    logs = [
        {"level": "error", "message": f"Unreal automation failed: {name}"}
        for name in failures[:1000]
    ] + [
        {"level": "warning", "message": f"Unreal automation warning: {name}"}
        for name in warnings[:1000]
    ]
    samples = [
        {"t_ms": t, "state": state, "events": [], "logs": logs if t == scenario.duration_ms else [], "metrics": {}}
        for t in range(0, scenario.duration_ms + 1, scenario.sample_ms)
    ]
    atomic_json(
        output / "trace.json",
        {
            "schema_version": 1,
            "seed": scenario.seed,
            "samples": samples,
            "metadata": {
                "engine": "Unreal Automation Framework",
                "test_filter": test,
                "report": "unreal-report/index.json",
            },
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--editor", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--test", required=True)
    parser.add_argument("--extra", action="append", default=[])
    parser.add_argument("--null-rhi", action="store_true")
    options = parser.parse_args()
    capture(options.editor, options.project, options.test, options.extra, options.null_rhi)
