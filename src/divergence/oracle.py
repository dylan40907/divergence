"""Inspectable assertions and multimodal evidence; scores are not probabilities."""

from __future__ import annotations

import fnmatch
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from divergence.models import Comparison, Difference, Scenario, Trace, safe_child

MISSING = "<missing>"


def flatten(value: dict, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict) and item:
            result.update(flatten(item, path))
        else:
            result[path] = item
    return result


def numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def failures(trace: Trace, scenario: Scenario) -> list[Difference]:
    result = []
    for assertion in scenario.assertions:
        events: set[str] = set()
        for i, sample in enumerate(trace.samples):
            if sample.t_ms < assertion.from_ms:
                continue
            events.update(sample.events)
            if assertion.at == "end" and i != len(trace.samples) - 1:
                continue
            state = flatten(sample.state)
            exists = assertion.path in state
            actual = state.get(assertion.path, MISSING)
            expected = assertion.value
            if assertion.op == "event":
                actual, expected = assertion.path in events, True
                passes = actual
            elif assertion.op == "exists":
                actual, expected = exists, assertion.value is not False
                passes = actual == expected
            elif not exists:
                passes = False
            elif assertion.op == "eq":
                passes = actual == expected and (
                    isinstance(actual, bool) == isinstance(expected, bool)
                )
            elif assertion.op == "ne":
                passes = actual != expected or (
                    isinstance(actual, bool) != isinstance(expected, bool)
                )
            elif numeric(actual) and numeric(expected):
                passes = {
                    "gt": actual > expected,
                    "gte": actual >= expected,
                    "lt": actual < expected,
                    "lte": actual <= expected,
                }[assertion.op]
            else:
                passes = False
            if not passes:
                result.append(
                    Difference(
                        t_ms=sample.t_ms,
                        channel="assertion",
                        path=assertion.path,
                        expected=expected,
                        actual=actual,
                        message=assertion.message
                        or f"{assertion.path} must satisfy {assertion.op}",
                    )
                )
    return result


def visual_descriptor(path: Path) -> np.ndarray:
    with Image.open(path) as frame:
        if frame.width * frame.height > 16_000_000:
            raise ValueError("frame exceeds 16 megapixels")
        return np.asarray(frame.convert("RGB").resize((64, 36)), dtype=np.float32) / 255.0


def sequence_distance(a: list[str], b: list[str]) -> float:
    if a == b:
        return 0.0
    difference = sum((Counter(a) - Counter(b)).values()) + sum((Counter(b) - Counter(a)).values())
    # Preserve order changes even when counts match.
    return max(1, difference) / max(1, len(a) + len(b))


def compare(
    reference: Trace,
    candidate: Trace,
    scenario: Scenario,
    reference_dir: Path,
    candidate_dir: Path,
    *,
    stability: bool = False,
) -> Comparison:
    policy = scenario.policy
    if len(reference.samples) != len(candidate.samples):
        return Comparison(
            verdict="INCONCLUSIVE", reasons=["Sample counts differ; incomplete evidence."]
        )
    differences: list[Difference] = []
    curve = []
    run_start = None
    consecutive = 0
    first_sustained = None
    previous_a = previous_b = None
    for a, b in zip(reference.samples, candidate.samples, strict=True):
        if abs(a.t_ms - b.t_ms) > policy.alignment_tolerance_ms:
            return Comparison(
                verdict="INCONCLUSIVE",
                reasons=["Scenario timestamps cannot be aligned within tolerance."],
            )
        sa, sb = flatten(a.state), flatten(b.state)
        keys = sorted(
            k
            for k in sa.keys() | sb.keys()
            if not any(fnmatch.fnmatchcase(k, pattern) for pattern in policy.ignore_paths)
        )
        changed = []
        for key in keys:
            av, bv = sa.get(key, MISSING), sb.get(key, MISSING)
            if key not in sa or key not in sb:
                different = True
            elif numeric(av) and numeric(bv):
                different = abs(
                    av - bv
                ) > policy.state_absolute_tolerance + policy.state_tolerance * abs(av)
            else:
                different = av != bv or type(av) is not type(bv)
            if different:
                changed.append(key)
                if len(differences) < 500:
                    differences.append(
                        Difference(t_ms=b.t_ms, channel="state", path=key, expected=av, actual=bv)
                    )
        state = len(changed) / max(1, len(keys))
        ae = [
            event
            for event in a.events
            if not any(fnmatch.fnmatchcase(event, p) for p in policy.ignore_events)
        ]
        be = [
            event
            for event in b.events
            if not any(fnmatch.fnmatchcase(event, p) for p in policy.ignore_events)
        ]
        events = sequence_distance(ae, be)
        al = [
            f"{entry.level}: {entry.message}"
            for entry in a.logs
            if entry.level in {"warning", "error"}
            and not any(fnmatch.fnmatchcase(entry.message, p) for p in policy.ignore_logs)
        ]
        bl = [
            f"{entry.level}: {entry.message}"
            for entry in b.logs
            if entry.level in {"warning", "error"}
            and not any(fnmatch.fnmatchcase(entry.message, p) for p in policy.ignore_logs)
        ]
        logs = sequence_distance(al, bl)
        metric = 0.0
        for key, direction in policy.metric_directions.items():
            if key not in a.metrics or key not in b.metrics:
                return Comparison(
                    verdict="INCONCLUSIVE", reasons=[f"Required metric is missing: {key}"]
                )
            av, bv = a.metrics[key], b.metrics[key]
            signed = (bv - av) if direction == "increase" else (av - bv)
            ratio = max(0.0, signed / max(abs(av), 1e-9))
            metric = max(metric, ratio)
            if ratio > policy.metric_relative_threshold and len(differences) < 500:
                differences.append(
                    Difference(t_ms=b.t_ms, channel="metrics", path=key, expected=av, actual=bv)
                )
        visual = motion = 0.0
        if bool(a.frame) != bool(b.frame):
            return Comparison(
                verdict="INCONCLUSIVE", reasons=["Visual evidence is missing in one execution."]
            )
        if a.frame and b.frame:
            va = visual_descriptor(safe_child(reference_dir, a.frame))
            vb = visual_descriptor(safe_child(candidate_dir, b.frame))
            pixel_distance = np.abs(va - vb)
            patches = pixel_distance.reshape(6, 6, 8, 8, 3).mean(axis=(1, 3, 4))
            # Local pooling preserves a small missing/moving object's evidence instead
            # of diluting it over the entire image. Still appearance, not semantics.
            visual = max(float(pixel_distance.mean()), float(patches.max()) * 0.5)
            if previous_a is not None:
                motion = float(np.abs((va - previous_a) - (vb - previous_b)).mean())
            previous_a, previous_b = va, vb
            if visual > policy.visual_threshold and len(differences) < 500:
                differences.append(
                    Difference(
                        t_ms=b.t_ms,
                        channel="visual",
                        path="frame",
                        expected="reference appearance",
                        actual=round(visual, 5),
                        message="Mean visual descriptor distance exceeds the configured tolerance.",
                    )
                )
        if events and len(differences) < 500:
            differences.append(
                Difference(t_ms=b.t_ms, channel="events", path="events", expected=ae, actual=be)
            )
        if logs and len(differences) < 500:
            differences.append(
                Difference(
                    t_ms=b.t_ms, channel="logs", path="warnings/errors", expected=al, actual=bl
                )
            )
        signals = [
            bool(changed),
            events > 0,
            logs > 0,
            metric > policy.metric_relative_threshold,
            visual > policy.visual_threshold,
        ]
        score = max(
            state, events, logs, min(1.0, metric), min(1.0, visual / policy.visual_threshold)
        )
        curve.append(
            {
                "t_ms": b.t_ms,
                "state": state,
                "visual": visual,
                "events": events,
                "logs": logs,
                "metrics": metric,
                "motion": motion,
                "score": score,
            }
        )
        if any(signals):
            if consecutive == 0:
                run_start = b.t_ms
            consecutive += 1
            if consecutive >= policy.persistence and first_sustained is None:
                first_sustained = run_start
            if (events > 0 or logs > 0) and first_sustained is None:
                # An event is a discrete observation, not a continuous signal that
                # should need to recur on the next frame to count.
                first_sustained = b.t_ms
        else:
            consecutive = 0
    assertion_failures = failures(candidate, scenario)
    detect_differences = stability or policy.mode in {"difference", "hybrid"}
    detect_assertions = not stability and policy.mode in {"assertions", "hybrid", "learned"}
    times = []
    reasons = []
    if detect_assertions and assertion_failures:
        times.append(min(item.t_ms for item in assertion_failures))
        reasons.append(f"{len(assertion_failures)} assertion violation(s).")
    if detect_differences and first_sustained is not None:
        times.append(first_sustained)
        reasons.append(
            f"Discrete event/log mismatch or continuous difference sustained for {policy.persistence} samples."
        )
    features = {}
    for channel in ("state", "visual", "events", "logs", "metrics", "motion"):
        values = [row[channel] for row in curve]
        features[f"{channel}_mean"] = float(np.mean(values))
        features[f"{channel}_max"] = float(max(values))
    # Keep raw difference onset separate from the configured behavior's onset.
    first_observed = min((d.t_ms for d in differences), default=None)
    failed_paths = {d.path for d in assertion_failures}
    relevant_times = [d.t_ms for d in differences if d.path in failed_paths]
    failed_events = {
        assertion.path
        for assertion in scenario.assertions
        if assertion.op == "event" and assertion.path in failed_paths
    }
    relevant_times.extend(
        d.t_ms
        for d in differences
        if d.channel == "events"
        and failed_events.intersection(set(d.expected or []) | set(d.actual or []))
    )
    if times and relevant_times:
        times.append(min(relevant_times))
    verdict = "BAD" if times else "GOOD"
    learned_score = None
    if not stability and policy.mode == "learned":
        from divergence.fusion import predict

        learned_verdict, learned_score = predict(features, policy.fusion)
        reasons.append(
            f"Experimental learned fusion margin: {learned_score:.3f} (not a probability)."
        )
        if not assertion_failures:
            verdict = learned_verdict
            if verdict == "BAD" and first_observed is not None:
                times.append(first_observed)
    return Comparison(
        verdict=verdict,
        first_divergence_ms=min(times) if times else None,
        reasons=reasons or ["Configured behavior passed."],
        differences=differences,
        curve=curve,
        features=features,
        assertion_failures=assertion_failures,
        learned_score=learned_score,
        first_observed_difference_ms=first_observed,
    )
