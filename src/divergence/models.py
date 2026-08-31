"""Versioned, deliberately strict public scenario and evidence contracts."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Assertion(Contract):
    path: str
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "exists", "event"] = "eq"
    value: Any = None
    at: Literal["always", "end"] = "end"
    from_ms: float = Field(default=0, ge=0)
    message: str = ""


class Action(Contract):
    at_ms: float = Field(ge=0)
    kind: Literal["click", "press", "fill", "evaluate"]
    target: str = ""
    value: str = ""


class BrowserConfig(Contract):
    entry: str = "index.html"
    viewport: tuple[int, int] = (960, 540)
    state_expression: str = "window.divergence.snapshot()"
    reset_expression: str = "seed => window.divergence.reset(seed)"
    # Optional explicit simulation tick. Otherwise use Playwright's virtual clock.
    tick_expression: str | None = None
    actions: list[Action] = Field(default_factory=list)
    allow_network: bool = False
    clock_epoch_ms: int = Field(default=1704067200000, ge=60000)
    reduced_motion: Literal["reduce", "no-preference"] = "no-preference"
    disable_animations: bool = False

    @model_validator(mode="after")
    def check_viewport(self):
        if any(n < 64 or n > 3840 for n in self.viewport):
            raise ValueError("viewport dimensions must be between 64 and 3840")
        return self


class LinearFusion(Contract):
    schema_version: Literal[1] = 1
    feature_version: Literal[2]
    kind: Literal["standardized_logistic"] = "standardized_logistic"
    feature_names: list[str]
    means: list[float]
    scales: list[float]
    weights: list[float]
    intercept: float
    abstain_margin: float = Field(default=0.25, ge=0)
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_dimensions(self):
        n = len(self.feature_names)
        if (
            not n
            or len(set(self.feature_names)) != n
            or any(len(values) != n for values in (self.means, self.scales, self.weights))
        ):
            raise ValueError("fusion model dimensions must match unique feature_names")
        if any(value <= 0 for value in self.scales):
            raise ValueError("fusion scales must be positive")
        return self


class Policy(Contract):
    mode: Literal["assertions", "difference", "hybrid", "learned"] = "assertions"
    fusion: LinearFusion | None = None
    state_tolerance: float = Field(default=0.02, ge=0)
    state_absolute_tolerance: float = Field(default=0.001, ge=0)
    ignore_paths: list[str] = Field(default_factory=list)
    ignore_events: list[str] = Field(default_factory=list)
    ignore_logs: list[str] = Field(default_factory=list)
    visual_threshold: float = Field(default=0.08, gt=0, le=1)
    metric_relative_threshold: float = Field(default=0.25, gt=0)
    metric_directions: dict[str, Literal["increase", "decrease"]] = Field(default_factory=dict)
    alignment_tolerance_ms: float = Field(default=1, ge=0)
    persistence: int = Field(default=2, ge=1, le=100)


class Scenario(Contract):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=120)
    adapter: Literal["command", "browser"] = "command"
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    duration_ms: int = Field(default=10000, ge=100, le=300000)
    sample_ms: int = Field(default=100, ge=16, le=10000)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    repeats: int = Field(default=2, ge=2, le=10)
    build: list[list[str]] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    assertions: list[Assertion] = Field(default_factory=list)
    policy: Policy = Field(default_factory=Policy)

    @model_validator(mode="after")
    def valid_scenario(self):
        if self.duration_ms % self.sample_ms:
            raise ValueError("duration_ms must be divisible by sample_ms")
        if self.duration_ms // self.sample_ms > 10000:
            raise ValueError("a scenario may contain at most 10001 samples")
        if self.adapter == "command" and not self.command:
            raise ValueError("command adapter requires an argv list")
        if any(not command or any(not arg for arg in command) for command in self.build):
            raise ValueError("build commands must be nonempty argv lists")
        if self.policy.mode == "assertions" and not self.assertions:
            raise ValueError("add assertions or explicitly select policy.mode: difference")
        if self.policy.mode == "learned" and self.policy.fusion is None:
            raise ValueError("learned mode requires an embedded fusion model")
        if any(a.at_ms > self.duration_ms for a in self.browser.actions):
            raise ValueError("browser action is outside scenario duration")
        if any(a.at_ms % self.sample_ms for a in self.browser.actions):
            raise ValueError("browser actions must align with sample_ms ticks")
        if any(a.from_ms > self.duration_ms for a in self.assertions):
            raise ValueError("assertion is outside scenario duration")
        return self

    @property
    def digest(self) -> str:
        payload = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class LogEntry(Contract):
    level: Literal["debug", "info", "warning", "error"] = "info"
    message: str


class Sample(Contract):
    t_ms: float = Field(ge=0)
    state: dict[str, Any] = Field(default_factory=dict)
    events: list[str] = Field(default_factory=list)
    logs: list[LogEntry] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    frame: str | None = None


class Trace(Contract):
    schema_version: Literal[1] = 1
    seed: int
    samples: list[Sample] = Field(min_length=2, max_length=10001)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ordered_samples(self):
        if any(b.t_ms <= a.t_ms for a, b in zip(self.samples, self.samples[1:], strict=False)):
            raise ValueError("trace sample times must be strictly increasing")
        # JSON nested inside state/metadata must not carry NaN/Infinity either.
        json.dumps(self.model_dump(), allow_nan=False)
        return self

    def validate_for(self, scenario: Scenario, artifact_dir: Path) -> None:
        if self.seed != scenario.seed:
            raise ValueError("trace seed does not match the scenario")
        expected = list(range(0, scenario.duration_ms + 1, scenario.sample_ms))
        if len(self.samples) != len(expected):
            raise ValueError(f"incomplete trace: expected {len(expected)} samples")
        for t, sample in zip(expected, self.samples, strict=True):
            if not math.isclose(t, sample.t_ms, abs_tol=scenario.policy.alignment_tolerance_ms):
                raise ValueError(f"trace timestamp {sample.t_ms} does not match expected {t}")
            if sample.frame:
                frame = safe_child(artifact_dir, sample.frame)
                if not frame.is_file() or frame.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                    raise ValueError(f"missing or unsupported frame: {sample.frame}")


Verdict = Literal["GOOD", "BAD", "INCONCLUSIVE"]


class Difference(Contract):
    t_ms: float
    channel: str
    path: str
    expected: Any = None
    actual: Any = None
    message: str = ""


class Comparison(Contract):
    verdict: Verdict
    first_divergence_ms: float | None = None
    reasons: list[str] = Field(default_factory=list)
    differences: list[Difference] = Field(default_factory=list)
    curve: list[dict[str, float]] = Field(default_factory=list)
    features: dict[str, float] = Field(default_factory=dict)
    assertion_failures: list[Difference] = Field(default_factory=list)
    learned_score: float | None = None
    first_observed_difference_ms: float | None = None


def safe_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if Path(relative).is_absolute() or not candidate.is_relative_to(root.resolve()):
        raise ValueError(f"artifact path escapes its root: {relative}")
    return candidate


def load_scenario(path: Path) -> Scenario:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("scenario exceeds 1 MiB")
    try:
        return Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid scenario YAML: {exc}") from exc


def load_trace(path: Path) -> Trace:
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("trace exceeds 64 MiB")
    return Trace.model_validate_json(path.read_text(encoding="utf-8"))
