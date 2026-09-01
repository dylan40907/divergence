# Product design

Divergence is a local application, not a cloud build service. The release target is a trustworthy
regression search whose decisions can be inspected, reproduced, and disputed.

## Boundaries

- Git provider: first-parent ancestry only. Perforce provider: visible submitted changes affecting
  one filespec, using a disposable clone of the active client spec. Both scopes are explicit. The
  searched property must remain bad after introduction; fixed-and-reintroduced bugs need an audit.
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

Adapters: command protocol for any instrumented runtime; Playwright for DOM-based or instrumented
web apps; Pygame and Godot examples use the command protocol. The Unreal Automation bridge also uses
that protocol. Git and Perforce remain revision providers, separate from engine capture behavior.

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

Manifest writes replace a complete temporary JSON file atomically. Windows access/sharing conflicts
receive up to eight attempts with bounded backoff (less than one second of total waiting per operation).
Persistent denial still raises an error; the writer never falls back to changing a live manifest in
place, adjusts permissions, or disables file locks. Tests hold a real Windows reader without delete
sharing to reproduce blocked replacement and verify both recovery and preservation of the prior JSON.
[Microsoft documents that delete sharing also governs rename access](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew).

Opt-in recovery retries only execution failures. All completed evidence from previous attempts is
retained, integrity checked, and compared with fresh captures. A later runtime failure cannot erase
disagreement in an incomplete set of repeats. Once conflicting behavior is observed, repeated
selection of favorable runs cannot turn that same investigation into a confident boundary.

## ML

Optional learned fusion must have a reproducible training/evaluation command, explicit feature
schema, data provenance, held-out bug families, and comparisons to rules/state-only/visual-only.
The generated demonstration is not a generalization benchmark. ML must not override a failed
explicit safety/behavior assertion. No model downloads or API calls by default.
