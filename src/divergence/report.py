"""Self-contained HTML evidence report; no CDN, telemetry, or executable trace content."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

from PIL import Image

from divergence.artifacts import MIB, inspect_artifacts
from divergence.models import Scenario, load_trace, safe_child


def image_data(path: Path) -> str:
    with Image.open(path) as image:
        image.thumbnail((960, 540))
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=75)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def render_report(run_dir: Path, destination: Path | None = None) -> Path:
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    scenario = Scenario.model_validate(state["scenario"])
    payload = {"run": state, "traces": {}}
    for sha, trial in state["trials"].items():
        if not trial["captures"]:
            continue
        directory = safe_child(run_dir, trial["captures"][0]["path"])
        inspect_artifacts(
            directory,
            max_bytes=scenario.capture_limits.max_mb * MIB,
            max_entries=scenario.capture_limits.max_entries,
        )
        trace = load_trace(directory / "trace.json")
        # Keep full traces on disk; bound portable-report image count and state payload.
        step = max(1, (len(trace.samples) - 1 + 299) // 300)
        indices = sorted(set(range(0, len(trace.samples), step)) | {len(trace.samples) - 1})
        samples = []
        for index in indices:
            sample = trace.samples[index]
            row = sample.model_dump()
            row["image"] = image_data(safe_child(directory, sample.frame)) if sample.frame else None
            samples.append(row)
        payload["traces"][sha] = {
            "samples": samples,
            "metadata": trace.metadata,
            "display_stride": step,
        }
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    ).decode()
    template = (Path(__file__).parent / "ui" / "report.html").read_text(encoding="utf-8")
    destination = destination or run_dir / "report.html"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(template.replace("__DIVERGENCE_DATA__", encoded), encoding="utf-8")
    return destination
