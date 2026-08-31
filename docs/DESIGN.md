# Product design

Divergence is a local application, not a cloud build service. The release target is a trustworthy
regression search whose decisions can be inspected, reproduced, and disputed.

## Boundaries

- Git provider: first-parent ancestry only, explicitly reported. The searched property must remain
  bad after its introduction. Fixed-and-reintroduced bugs require a narrower range or a linear audit.
- A private clone protects the input repository from checkout/build mutations. This is **not a
  security sandbox**. Build commands, browser expressions, and executable adapters are trusted code.
- No automatic dependency installation from arbitrary revisions; users provide build commands.
- Commands use argv lists, not a shell. Process timeouts and process-tree cleanup are required.
- The local report server is read-only and binds to loopback. No remote execution API.
- Evidence is local and may contain sensitive screenshots or logs. Publishing is a user decision.

## Contracts

A versioned YAML scenario contains adapter configuration, reproducible seed, duration/sample rate,
build steps, assertions and comparison policy. Adapters emit the same versioned trace contract:
ordered samples on scenario time, state, events, logs, metrics, and optional relative image paths.
An assertion says which property makes a run bad; generic differences are used only when explicitly
enabled. Appearance differences alone do not prove semantic failure.

Adapters: command protocol for any instrumented runtime; Playwright for instrumented web apps;
Pygame and Godot examples use the command protocol. Future Perforce work belongs behind the revision
provider contract, not inside an engine adapter.

## Oracle

Compare on scenario time with bounded alignment tolerance. Retain timing differences rather than
warping away delays. Measure numeric state drift, categorical changes, event/log differences,
metric deterioration, and visual descriptors. Thresholds and ignored state paths are explicit.
Repeated runs must agree; unstable reference runs or inconsistent candidate labels are inconclusive.
First divergence is the earliest *observed sample* satisfying the configured oracle, not a claim
of exact sub-frame causal timing. Source ranking is correlation, not proof of responsible lines.

## Search and persistence

Validate good and bad endpoints before searching. Maintain a good lower bound and bad upper bound.
Skip unusable revisions, search the remaining interval, and report all possible boundary commits
if skips prevent a unique answer. Persist run metadata and trial results atomically. Cache keys
include scenario digest, source identity, runner version, and reference; never reuse partial trials.

## ML

Optional learned fusion must have a reproducible training/evaluation command, explicit feature
schema, data provenance, held-out bug families, and comparisons to rules/state-only/visual-only.
The generated demonstration is not a generalization benchmark. ML must not override a failed
explicit safety/behavior assertion. No model downloads or API calls by default.
