import pytest
from PIL import Image

from divergence.models import Sample, Trace
from divergence.oracle import compare


def trace(opened=True, *, x=1.0, times=(0, 100, 200), **kwargs):
    return Trace(
        seed=42,
        samples=[Sample(t_ms=t, state={"door": {"open": opened}, "x": x}, **kwargs) for t in times],
    )


def test_assertions_find_behavior_and_observed_onset(scenario, tmp_path):
    outcome = compare(trace(), trace(False), scenario, tmp_path, tmp_path)
    assert outcome.verdict == "BAD"
    assert outcome.first_divergence_ms == 0
    assert outcome.assertion_failures[0].t_ms == 200
    assert outcome.differences[0].path == "door.open"


def test_intentional_difference_is_not_a_failed_assertion(scenario, tmp_path):
    assert compare(trace(), trace(x=5), scenario, tmp_path, tmp_path).verdict == "GOOD"
    assert (
        compare(trace(), trace(x=5), scenario, tmp_path, tmp_path, stability=True).verdict == "BAD"
    )


def test_ignored_state_is_not_instability(scenario, tmp_path):
    scenario.policy.ignore_paths = ["x"]
    assert (
        compare(trace(), trace(x=5), scenario, tmp_path, tmp_path, stability=True).verdict == "GOOD"
    )


def test_tolerance_and_boolean_are_not_conflated(scenario, tmp_path):
    assert (
        compare(trace(), trace(x=1.001), scenario, tmp_path, tmp_path, stability=True).verdict
        == "GOOD"
    )
    assert compare(trace(), trace(opened=1), scenario, tmp_path, tmp_path).verdict == "BAD"


def test_alignment_does_not_warp_away_delay(scenario, tmp_path):
    assert (
        compare(trace(), trace(times=(0, 110, 200)), scenario, tmp_path, tmp_path).verdict
        == "INCONCLUSIVE"
    )


def test_event_order_is_evidence(scenario, tmp_path):
    scenario.policy.mode = "difference"
    outcome = compare(
        trace(events=["A", "B"]), trace(events=["B", "A"]), scenario, tmp_path, tmp_path
    )
    assert outcome.verdict == "BAD"
    assert outcome.features["events_max"] > 0


def test_missing_one_time_event_is_not_filtered_as_noise(scenario, tmp_path):
    scenario.policy.mode = "difference"
    reference = trace()
    reference.samples[1].events = ["DoorOpened"]
    outcome = compare(reference, trace(), scenario, tmp_path, tmp_path)
    assert outcome.verdict == "BAD"
    assert outcome.first_divergence_ms == 100


def test_event_assertion_reports_missing_event_time(scenario, tmp_path):
    from divergence.models import Assertion

    scenario.assertions = [Assertion(path="DoorOpened", op="event")]
    reference = trace()
    reference.samples[1].events = ["DoorOpened"]
    result = compare(reference, trace(), scenario, tmp_path, tmp_path)
    assert result.verdict == "BAD"
    assert result.first_divergence_ms == 100


def test_noise_filters_do_not_suppress_explicit_event_contract(scenario, tmp_path):
    from divergence.models import Assertion

    scenario.assertions = [Assertion(path="Required", op="event")]
    scenario.policy.ignore_events = ["*"]
    result = compare(trace(events=["Required"]), trace(events=[]), scenario, tmp_path, tmp_path)
    assert result.verdict == "BAD"


def test_visual_only_and_missing_visual(scenario, tmp_path):
    Image.new("RGB", (32, 32), "white").save(tmp_path / "a.png")
    Image.new("RGB", (32, 32), "black").save(tmp_path / "b.png")
    scenario.policy.mode = "difference"
    outcome = compare(trace(frame="a.png"), trace(frame="b.png"), scenario, tmp_path, tmp_path)
    assert outcome.verdict == "BAD"
    assert outcome.features["visual_max"] == 1
    assert (
        compare(trace(frame="a.png"), trace(), scenario, tmp_path, tmp_path).verdict
        == "INCONCLUSIVE"
    )


def test_performance_direction_and_missing_metric(scenario, tmp_path):
    scenario.policy.mode = "difference"
    scenario.policy.metric_directions = {"fps": "decrease"}
    assert (
        compare(
            trace(metrics={"fps": 60}), trace(metrics={"fps": 20}), scenario, tmp_path, tmp_path
        ).verdict
        == "BAD"
    )
    assert (
        compare(
            trace(metrics={"fps": 60}), trace(metrics={"fps": 90}), scenario, tmp_path, tmp_path
        ).verdict
        == "GOOD"
    )
    assert (
        compare(trace(metrics={"fps": 60}), trace(), scenario, tmp_path, tmp_path).verdict
        == "INCONCLUSIVE"
    )


def test_transient_change_requires_persistence(scenario, tmp_path):
    scenario.policy.mode = "difference"
    candidate = trace()
    candidate.samples[1].state["x"] = 3
    assert compare(trace(), candidate, scenario, tmp_path, tmp_path).verdict == "GOOD"


def test_contract_rejects_incomplete_trace_and_traversal(scenario, tmp_path):
    with pytest.raises(ValueError, match="incomplete"):
        trace(times=(0, 100)).validate_for(scenario, tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        trace(frame="../secret.png").validate_for(scenario, tmp_path)


def test_contract_rejects_nan_and_duplicate_timestamps():
    with pytest.raises(ValueError):
        trace(x=float("nan"))
    with pytest.raises(ValueError):
        trace(times=(0, 100, 100))
