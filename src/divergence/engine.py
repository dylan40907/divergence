"""Persistent first-parent bisection with endpoint validation and explicit ambiguity."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import platform
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from divergence import __version__
from divergence.adapters import adapter_for
from divergence.artifacts import MIB, evidence_digest
from divergence.evidence import read_run, validate_saved_trials
from divergence.models import Comparison, Scenario, load_trace
from divergence.oracle import compare
from divergence.process import run_command
from divergence.revisions import GitProvider, python_regions, rank_changes
from divergence.storage import atomic_json


def runner_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.rglob("*.py")):
        digest.update(path.relative_to(Path(__file__).parent).as_posix().encode())
        digest.update(path.read_bytes())
    digest.update(platform.platform().encode())
    digest.update(platform.python_version().encode())
    for package in ("numpy", "Pillow", "pydantic", "playwright", "pygame-ce", "scikit-learn"):
        try:
            digest.update(f"{package}={importlib.metadata.version(package)}".encode())
        except importlib.metadata.PackageNotFoundError:
            pass
    return digest.hexdigest()


@contextmanager
def run_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("another process is using this run directory") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Search:
    def __init__(
        self,
        repository: Path,
        good: str,
        bad: str,
        scenario: Scenario,
        output: Path,
        progress: Callable[[dict], None] | None = None,
        *,
        audit: bool = False,
        verify_only: bool = False,
    ):
        if verify_only and (audit or good != bad):
            raise ValueError("verification requires one revision and cannot audit history")
        self.output = output.resolve()
        self.provider = GitProvider(repository, self.output)
        self.history = (
            [self.provider.resolve(good)] if verify_only else self.provider.history(good, bad)
        )
        self.scenario = scenario
        self.progress = progress or (lambda event: None)
        self.reference: Path | None = None
        self.state: dict = {}
        self.audit = audit
        self.verify_only = verify_only
        self.retry_inconclusive = False
        self.retried: set[str] = set()
        self.validated: set[str] = set()

    @classmethod
    def for_verification(
        cls,
        repository: Path,
        revision: str,
        scenario: Scenario,
        output: Path,
        progress: Callable[[dict], None] | None = None,
    ) -> Search:
        """Exercise one committed revision without requiring a regression interval."""
        return cls(repository, revision, revision, scenario, output, progress, verify_only=True)

    def persist(self):
        atomic_json(self.output / "run.json", self.state)

    def capture_digest(self, directory: Path) -> str:
        return evidence_digest(
            directory,
            max_bytes=self.scenario.capture_limits.max_mb * MIB,
            max_entries=self.scenario.capture_limits.max_entries,
        )

    def trial(self, index: int) -> dict:
        revision = self.history[index]
        previous_attempts = []
        if revision in self.state["trials"]:
            saved = self.state["trials"][revision]
            previous_attempts = saved.get("previous_attempts", []) + [
                {key: value for key, value in saved.items() if key != "previous_attempts"}
            ]
            if revision not in self.validated:
                validate_saved_trials(self.output, {"trials": {revision: saved}}, self.scenario)
                self.validated.add(revision)
            if not (
                self.retry_inconclusive
                and saved.get("retryable", False)
                and saved["verdict"] == "INCONCLUSIVE"
                and revision not in self.retried
            ):
                self.progress({"kind": "cached", **saved})
                return saved
            self.retried.add(revision)
        self.progress(
            {"kind": "testing", "sha": revision, "index": index, "total": len(self.history)}
        )
        started = time.monotonic()
        captures = []
        comparisons = []
        errors = []
        diagnostic_paths = []
        execution_failure = False
        semantic_uncertainty = False
        stage = "checkout"
        attempt = uuid.uuid4().hex[:8]
        for repeat in range(self.scenario.repeats):
            source = None
            folder = self.output / "trials" / revision / f"{attempt}-{repeat}"
            try:
                stage = "checkout"
                source = self.provider.checkout(revision, f"{revision[:12]}-{attempt}-{repeat}")
                stage = "build"
                for n, command in enumerate(self.scenario.build):
                    run_command(
                        command,
                        source,
                        self.scenario.timeout_seconds,
                        self.output / "logs" / f"{revision[:12]}-{attempt}-{repeat}-build-{n}.log",
                    )
                stage = "capture"
                trace = adapter_for(self.scenario).capture(source, folder, self.scenario)
                captures.append(
                    {
                        "path": folder.relative_to(self.output).as_posix(),
                        "digest": self.capture_digest(folder),
                    }
                )
                ref_dir = self.reference or (self.output / captures[0]["path"])
                reference = load_trace(ref_dir / "trace.json")
                stage = "compare"
                comparisons.append(compare(reference, trace, self.scenario, ref_dir, folder))
            except (ValueError, RuntimeError, OSError) as exc:
                errors.append(f"{stage}: {exc}")
                if folder.exists():
                    diagnostic_paths.append(folder.relative_to(self.output).as_posix())
                if stage == "build":
                    diagnostic_paths.extend(
                        path.relative_to(self.output).as_posix()
                        for path in (self.output / "logs").glob(
                            f"{revision[:12]}-{attempt}-{repeat}-build-*.log"
                        )
                    )
                execution_failure = stage in {"checkout", "build", "capture"}
                break
            finally:
                if source is not None:
                    try:
                        self.provider.release(source)
                    except RuntimeError as exc:
                        errors.append(f"cleanup: {exc}")
                        semantic_uncertainty = True
        verdicts = {comparison.verdict for comparison in comparisons}
        if len(verdicts) > 1:
            semantic_uncertainty = True
            errors.append("repeated executions disagree on the verdict")
        if errors or len(comparisons) != self.scenario.repeats:
            verdict = "INCONCLUSIVE"
        else:
            verdict = next(iter(verdicts))
        display_index = min(1, len(comparisons) - 1) if self.verify_only and comparisons else 0
        if self.verify_only and len(comparisons) > 1:
            display_index = next(
                (index for index, item in enumerate(comparisons[1:], 1) if item.verdict != "GOOD"),
                1,
            )
        display_comparison = (
            comparisons[display_index] if comparisons else Comparison(verdict="INCONCLUSIVE")
        )
        comparison_basis = "reference" if self.reference else "self"
        if self.verify_only and display_index > 0:
            comparison_basis = "repeat"
        if len(captures) > 1:
            first = self.output / captures[0]["path"]
            for capture_index, capture in enumerate(captures[1:], start=1):
                second = self.output / capture["path"]
                stable = compare(
                    load_trace(first / "trace.json"),
                    load_trace(second / "trace.json"),
                    self.scenario,
                    first,
                    second,
                    stability=True,
                )
                if stable.verdict != "GOOD":
                    verdict = "INCONCLUSIVE"
                    semantic_uncertainty = True
                    errors.append("repeated executions are unstable under the comparison policy")
                    if self.verify_only:
                        display_index = capture_index
                        display_comparison = stable
                        comparison_basis = "repeatability"
                    break
        # A retry must not erase previously observed behavior. Even incomplete
        # attempts retain their captures and must agree with the fresh execution.
        if previous_attempts and captures:
            fresh = self.output / captures[0]["path"]
            for old in previous_attempts:
                if set(old["repeat_verdicts"]) | verdicts not in ({"GOOD"}, {"BAD"}):
                    if old["repeat_verdicts"] and verdicts:
                        semantic_uncertainty = True
                for capture in old["captures"]:
                    prior = self.output / capture["path"]
                    stable = compare(
                        load_trace(prior / "trace.json"),
                        load_trace(fresh / "trace.json"),
                        self.scenario,
                        prior,
                        fresh,
                        stability=True,
                    )
                    if stable.verdict != "GOOD":
                        semantic_uncertainty = True
            if semantic_uncertainty:
                verdict = "INCONCLUSIVE"
                errors.append("retry disagrees with evidence retained from an earlier attempt")
        result = {
            **self.provider.describe(revision),
            "index": index,
            "verdict": verdict,
            "seconds": round(time.monotonic() - started, 3),
            "captures": captures,
            "comparison": display_comparison.model_dump(),
            "comparison_capture_index": display_index,
            "comparison_basis": comparison_basis,
            "repeat_verdicts": [comparison.verdict for comparison in comparisons],
            "errors": errors,
            "diagnostic_paths": diagnostic_paths,
            "retryable": verdict == "INCONCLUSIVE"
            and execution_failure
            and not semantic_uncertainty
            and "INCONCLUSIVE" not in verdicts,
            "previous_attempts": previous_attempts,
        }
        self.state["trials"][revision] = result
        self.persist()
        self.progress({"kind": "tested", **result})
        return result

    def run(self, *, resume: bool = False, retry_inconclusive: bool = False) -> dict:
        if retry_inconclusive and (not resume or not (self.output / "run.json").is_file()):
            raise ValueError("--retry-inconclusive requires --resume and an existing run")
        self.retry_inconclusive = retry_inconclusive
        self.retried.clear()
        self.validated.clear()
        self.reference = None
        with run_lock(self.output):
            return self._run(resume)

    def _run(self, resume: bool) -> dict:
        identity = {
            "scenario_digest": self.scenario.digest,
            "runner_fingerprint": runner_fingerprint(),
            "repository": str(self.provider.repository),
            "good": self.history[0],
            "bad": self.history[-1],
            "audit": self.audit,
            "mode": "verify" if self.verify_only else "bisect",
        }
        manifest = self.output / "run.json"
        if manifest.exists():
            if not resume:
                raise ValueError("output already contains a run; use --resume or a new --output")
            self.state, _ = read_run(self.output)
            if self.state.get("identity") != identity:
                raise ValueError(
                    "scenario, runner, environment, or revisions changed; use a new output directory"
                )
            validate_saved_trials(self.output, self.state, self.scenario)
            self.validated.update(self.state["trials"])
        else:
            if any(path.name != ".lock" for path in self.output.iterdir()):
                raise ValueError("a new run requires an empty output directory")
            self.state = {
                "schema_version": 1,
                "version": __version__,
                "identity": identity,
                "created_at": datetime.now(UTC).isoformat(),
                "scenario": self.scenario.model_dump(),
                "history": [self.provider.describe(sha) for sha in self.history],
                "trials": {},
                "status": "RUNNING",
                "scope": "single-revision" if self.verify_only else "first-parent",
                "candidates": [],
                "limitations": (
                    [
                        "Checks one committed revision, not uncommitted work or regression history.",
                        "Repeatability is measured under configured tolerances and ignores; it is not proof of determinism.",
                        "Passing covers only this scenario and its enforced assertions, not general application correctness.",
                        f"Observations are sampled every {self.scenario.sample_ms} ms.",
                    ]
                    if self.verify_only
                    else [
                        "Assumes the tested regression remains present after introduction.",
                        "Source ranking is correlation, not proof of causation.",
                        f"Observed divergence is sampled every {self.scenario.sample_ms} ms.",
                    ]
                ),
            }
        self.provider.prepare()
        for key in (
            "culprit",
            "diff",
            "ranked_changes",
            "boundary_good",
            "boundary_bad",
            "good_after_bad",
            "message",
        ):
            self.state.pop(key, None)
        self.state["candidates"] = []
        self.state["status"] = "RUNNING"
        self.persist()
        try:
            baseline = self.trial(0)
            if self.verify_only:
                if baseline["verdict"] == "INCONCLUSIVE":
                    return self.finish(
                        "INCONCLUSIVE",
                        "Could not verify the scenario. Review execution diagnostics and repeatability evidence.",
                    )
                if baseline["verdict"] == "BAD":
                    return self.finish(
                        "CHECK_FAILED",
                        "Repeated executions completed consistently, but the configured oracle rejected this revision.",
                    )
                enforced = (
                    bool(self.scenario.assertions) and self.scenario.policy.mode != "difference"
                )
                return self.finish(
                    "VERIFIED",
                    "Repeated executions agree under the configured comparison policy. "
                    + (
                        "Configured assertions passed."
                        if enforced
                        else "No behavioral assertions were enforced."
                    ),
                )
            if baseline["verdict"] != "GOOD":
                return self.finish(
                    "INVALID_BASELINE", "Known-good revision must pass and reproduce consistently."
                )
            self.reference = self.output / baseline["captures"][0]["path"]
            endpoint = self.trial(len(self.history) - 1)
            if endpoint["verdict"] != "BAD":
                return self.finish(
                    "INVALID_ENDPOINT",
                    "Known-bad revision did not reliably reproduce the configured regression.",
                )
            if self.audit:
                saw_bad = False
                reversals = []
                for index in range(len(self.history)):
                    observed = self.trial(index)
                    if observed["verdict"] == "BAD":
                        saw_bad = True
                    elif observed["verdict"] == "GOOD" and saw_bad:
                        reversals.append(observed["sha"])
                if reversals:
                    self.state["good_after_bad"] = reversals
                    return self.finish(
                        "NON_MONOTONIC",
                        "The regression disappears and reappears in this interval. Narrow the range; one binary-search boundary cannot describe it.",
                    )
            low, high = 0, len(self.history) - 1
            while high - low > 1:
                choices = [
                    i for i in range(low + 1, high) if self.history[i] not in self.state["trials"]
                ]
                # Reconstruct bounds from cached trials on resume.
                for i in range(low + 1, high):
                    trial = self.state["trials"].get(self.history[i])
                    if trial:
                        trial = self.trial(i)  # Validate or retry before using the label.
                        if trial["verdict"] == "GOOD":
                            low = max(low, i)
                        elif trial["verdict"] == "BAD":
                            high = min(high, i)
                if low >= high:
                    return self.finish(
                        "INCONSISTENT",
                        "Observed verdicts violate the assumed good-to-bad boundary.",
                    )
                choices = [i for i in choices if low < i < high]
                if not choices:
                    break
                middle = (low + high) / 2
                index = min(choices, key=lambda i: abs(i - middle))
                trial = self.trial(index)
                if trial["verdict"] == "GOOD":
                    low = index
                elif trial["verdict"] == "BAD":
                    high = index
            candidates = self.history[low + 1 : high + 1]
            self.state["candidates"] = candidates
            self.state["boundary_good"] = self.history[low]
            self.state["boundary_bad"] = self.history[high]
            self.state["culprit"] = candidates[0] if len(candidates) == 1 else None
            if len(candidates) == 1:
                diff = self.provider.diff(candidates[0])
                self.state["diff"] = diff
                paths = [
                    d["path"]
                    for d in self.state["trials"][candidates[0]]["comparison"]["differences"]
                ]
                self.state["ranked_changes"] = rank_changes(diff, paths)
                for change in self.state["ranked_changes"]:
                    if change["path"].endswith(".py"):
                        change["functions"] = python_regions(
                            self.provider.read_file(candidates[0], change["path"]),
                            change["changed_lines"],
                        )
            return self.finish(
                "FOUND" if len(candidates) == 1 else "AMBIGUOUS",
                "Located the first bad revision on the searched first-parent history."
                if len(candidates) == 1
                else "Untestable revisions prevent a unique boundary; all possible candidates are listed.",
            )
        except KeyboardInterrupt:
            self.finish("INTERRUPTED", "Interrupted; rerun the same command with --resume.")
            raise
        except Exception as exc:
            self.finish("FAILED", f"Run stopped without a verdict: {exc}")
            raise

    def finish(self, status: str, message: str) -> dict:
        self.state.update(status=status, message=message, updated_at=datetime.now(UTC).isoformat())
        self.persist()
        return self.state
