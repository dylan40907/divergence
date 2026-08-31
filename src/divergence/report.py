"""Self-contained HTML evidence report; no CDN, telemetry, or executable trace content."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

from PIL import Image

from divergence.artifacts import MIB
from divergence.evidence import read_run, validate_saved_trials
from divergence.models import Scenario, load_trace, safe_child
from divergence.storage import atomic_text

MAX_REPORT_BYTES = 128 * MIB


def image_data(path: Path) -> str:
    with Image.open(path) as image:
        image.thumbnail((960, 540))
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=75)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def portable_trace(directory: Path, scenario: Scenario, focus_ms: list[float]) -> dict:
    trace = load_trace(directory / "trace.json")
    trace.validate_for(scenario, directory)
    # Keep full traces on disk; bound portable-report image count and state payload.
    step = max(1, (len(trace.samples) - 1 + 299) // 300)
    selected = set(range(0, len(trace.samples), step)) | {len(trace.samples) - 1}
    # Preserve the actual onset and its neighbors even when regular display frames
    # are subsampled. The viewer should not jump past the evidence it describes.
    for time_ms in focus_ms:
        index = min(range(len(trace.samples)), key=lambda i: abs(trace.samples[i].t_ms - time_ms))
        selected.update(range(max(0, index - 1), min(len(trace.samples), index + 2)))
    indices = sorted(selected)
    samples = []
    for index in indices:
        sample = trace.samples[index]
        row = sample.model_dump()
        row["image"] = image_data(safe_child(directory, sample.frame)) if sample.frame else None
        samples.append(row)
    return {"samples": samples, "metadata": trace.metadata, "display_stride": step}


def render_report(run_dir: Path, destination: Path | None = None) -> Path:
    run_dir = run_dir.resolve()
    destination = (destination or run_dir / "report.html").resolve()
    if destination.suffix.lower() not in {".html", ".htm"}:
        raise ValueError(
            "report output must end in .html or .htm; original run data is never overwritten"
        )
    if destination.is_relative_to(run_dir) and destination.relative_to(run_dir).parts[
        0
    ].lower() in {
        "source",
        "checkouts",
        "trials",
        "logs",
    }:
        raise ValueError("report output cannot overwrite evidence, logs, or source checkouts")
    state, scenario = read_run(run_dir)
    checked = validate_saved_trials(run_dir, state, scenario)
    verification = state["identity"].get("mode") == "verify"
    payload = {
        "run": state,
        "traces": {},
        "reference_trace": None,
        "integrity": {"checked_captures": checked, "method": "SHA-256"},
    }
    focus = sorted(
        {
            trial["comparison"]["first_divergence_ms"]
            for trial in state["trials"].values()
            if trial["comparison"]["first_divergence_ms"] is not None
        }
    )
    for sha, trial in state["trials"].items():
        if not trial["captures"]:
            continue
        index = trial.get("comparison_capture_index", 0) if verification else 0
        if not isinstance(index, int) or not 0 <= index < len(trial["captures"]):
            raise ValueError("report comparison refers to a missing capture")
        directory = safe_child(run_dir, trial["captures"][index]["path"])
        payload["traces"][sha] = portable_trace(directory, scenario, focus)
        if verification and index > 0:
            first = safe_child(run_dir, trial["captures"][0]["path"])
            payload["reference_trace"] = portable_trace(first, scenario, focus)
    data = bytearray()
    for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(payload):
        encoded_chunk = chunk.encode("utf-8")
        if len(data) + len(encoded_chunk) > MAX_REPORT_BYTES:
            raise ValueError(
                "portable report exceeds 128 MiB; narrow the investigation. Original evidence is retained."
            )
        data.extend(encoded_chunk)
    encoded = base64.b64encode(data).decode()
    template = (Path(__file__).parent / "ui" / "report.html").read_text(encoding="utf-8")
    atomic_text(destination, template.replace("__DIVERGENCE_DATA__", encoded))
    return destination
