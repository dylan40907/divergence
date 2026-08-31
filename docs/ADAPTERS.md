# Scenario and adapter guide

Scenarios are trusted executable configuration. YAML is safely loaded and rejects unknown fields.
Commands are argv arrays, never shell strings. `{python}` resolves to the current interpreter.
Other executable paths should be absolute or on PATH. Commands run at the checkout root; pipes,
`&&`, `$()`, and shell variables are not evaluated. Use a script for complex build logic.

## Check a scenario before searching

Create a starter with `divergence init`. This does not inspect your repository, execute commands,
install dependencies, or overwrite a file. Initialization can run before a repository exists.

```sh
# Command protocol; everything after -- is preserved as one argv list.
divergence init --adapter command --name "Open the door" --output scenario.yaml -- "{python}" tools/capture.py
# Plain DOM app served from the committed checkout (or from a build step's output).
divergence init --adapter browser --entry dist/index.html --output browser.yaml
```

PowerShell users can use these commands on one line; quote `"{python}"` so PowerShell treats it as a
literal placeholder. Starters default to 5 seconds, 250 ms samples, and two repeats. `--duration-ms`
must be divisible by `--sample-ms`. Browser entries are relative file paths, not remote URLs.
The browser adapter serves files; it does not start a dev server/backend. Build your app into static
files first (or configure trusted `build` commands), and use relative asset URLs in that output.

The initial browser snapshot reads `document.title` and visible body text without assuming a
`window.divergence` hook. You can provide `--state-expression`, edit the YAML for structured fields,
or use `divergence record` to record actions with the starter as a template. An empty action list
only observes the page; it does not explore the UI. Text, screenshots, logs and recorded values may
contain sensitive data. No automatic redaction is promised.

Every starter has `assertions: []` and `policy.mode: difference`. It can check repeatability but
cannot decide whether the repeated behavior is correct. Add your actual expected behavior, for
example an invariant about a total, door collider, or final state, then select `assertions` mode.
Do not simply use an observed value as the expected value unless it expresses the requirement.

1. Run `divergence validate scenario.yaml` to check configuration without executing application code.
2. Run `divergence verify --repo /path/to/repository --revision HEAD --scenario scenario.yaml`.
   This executes the committed revision in fresh private checkouts, once per configured repeat.
   It checks trace shape/cadence, assertion outcomes, and repeated-execution consistency.
3. Inspect the report's Overview, State & events, and Executions tabs. A failing command or missing
   browser selector is a setup diagnostic, not a regression verdict. An unstable replay is inconclusive;
   correct the scenario or deliberately configure tolerances before starting a new verification run.
4. Once a known-good revision verifies, use `bisect` with good/bad revisions. Bisection independently
   validates both endpoints; verification captures are not imported into another run's cache.

`verify` never tests uncommitted source changes. It also does not infer assertions or install missing
dependencies. The portable verification report shows replay 1 versus replay 2, or the later replay
that exposed instability. If only one capture completes, it is shown without a reference; no capture
is presented as an independent comparison with itself. Earlier retry attempts remain in the ledger.
The raw trace/frame files for every completed repeat remain in the run folder.

Verification is conditional on the scenario, seed, ignores, and tolerances. A passing difference-only
check says the sampled behavior repeats under that policy; it says nothing about whether that behavior
is correct. `capture` remains available for one raw execution without a repeated-run verdict.

## Command scenario

```yaml
schema_version: 1
name: Open the door and reach the exit
adapter: command
command: ['{python}', 'tools/capture.py']
build: []
seed: 42
duration_ms: 10000
sample_ms: 250
timeout_seconds: 120
repeats: 2
assertions:
  - path: door.collider_enabled
    op: eq
    value: false
    at: always
    from_ms: 3000
    message: An open door must release its collision barrier.
  - path: player.reached_exit
    op: eq
    value: true
    at: end
policy:
  mode: assertions
  ignore_paths: [clock_ms, debug.*]
```

Assertion operations: `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `exists`, `event`. `event` requires the
named event since `from_ms`; `at: end` checks the eligible window. State paths address nested
dictionaries using dots; avoid literal dots in dictionary keys. Numeric comparison tolerances
do not apply to explicit equality assertions: use inequalities for allowed ranges. `exists`
requires a path unless `value: false` requests absence. `from_ms` is inclusive.

Modes: `assertions` (default), `difference` (generic changes), `hybrid` (both must pass), and
`learned` (experimental JSON model, normally passed as CLI `--model`). Explicit failed assertions
still force BAD in learned mode. Full model contents become part of the saved scenario/fingerprint.

`state_tolerance` is relative to the reference, plus `state_absolute_tolerance`. `ignore_paths`
accepts case-sensitive globs. `visual_threshold` controls RGB/local-patch distance. `persistence`
applies to continuous signals, not discrete event/log mismatches. Only warning/error logs affect
the generic oracle. Events/logs must be per-sample deltas, not cumulative histories.

`ignore_events` and `ignore_logs` accept glob patterns for known benign event names/log messages.
They affect generic comparison and stability checks, never explicit assertions.

Metrics affect the generic verdict only when explicitly selected:

```yaml
policy:
  mode: hybrid
  metric_relative_threshold: 0.25
  metric_directions: {frame_ms: increase, fps: decrease}
```

Relative thresholds near a zero reference are sensitive. Use meaningful nonzero measurements and
sufficient repeats. The Python fixture labels its synthetic step metric; it is not hardware evidence.

## Trace protocol

The executable receives `DIVERGENCE_SCENARIO` (absolute JSON path), `DIVERGENCE_OUTPUT` (absolute
artifact directory), and `DIVERGENCE_SEED`. Exit zero and write `trace.json`:

```json
{
  "schema_version": 1,
  "seed": 42,
  "samples": [
    {"t_ms": 0, "state": {"door": {"open": false}}, "events": [], "logs": [], "metrics": {}, "frame": "frames/00000.png"},
    {"t_ms": 250, "state": {"door": {"open": true}}, "events": ["DoorOpened"], "logs": [], "metrics": {}, "frame": "frames/00001.png"}
  ],
  "metadata": {"engine": "your runtime", "clock": "scenario"}
}
```

This abbreviated trace is for a 250 ms scenario. Real traces contain every configured sample,
including zero and the final timestamp. Times increase strictly, seeds match, finite numbers are
required, and image paths must stay within the capture folder. PNG/JPEG frames are optional, but
both compared executions must offer the same modality. Adapter-owned metadata can add provenance.

Guardrails: 1 MiB YAML, 64 MiB trace JSON, 10,001 samples, 16-megapixel decoded frames, 16 MiB process
logs, configured command timeouts. These are not a hostile-code sandbox. Build steps run once per
fresh repeat checkout with individual timeouts. Capture timeout covers browser startup/teardown.
Checkouts are removed after trials; logs, frames, traces and the private clone remain.

Capture storage is bounded separately for each repeat:

```yaml
capture_limits:
  max_mb: 512
  max_entries: 20000
```

Sizes use MiB (1,048,576 bytes). All files count, including video, logs, and temporary files;
directories count toward the entry limit. Command and browser workers are checked approximately
every half second and again after exit, so fast-exiting writers are checked too. These are polling
guardrails, not hard disk quotas: a burst of writes can exceed the threshold before termination.
Limits apply per capture, not to the whole investigation, build directory, or machine. Completed
captures are checked again for comparison, resume, and report generation. Links, junctions, and
non-regular files are rejected rather than followed. No captured files are silently deleted.

Evidence hashes include all capture files, including logs and video, using 1 MiB read chunks.
Changing these limits changes the scenario fingerprint; start a new investigation after a change.

Windows build/capture commands run inside an owned job object. A small isolated Python worker joins the job
before starting application code, then releases its inherited job handle. The runner retains the
lifetime handle; closing it also stops descendants, including after the runner exits unexpectedly.
Timeouts terminate the job and wait for its process count to reach zero. Normal launcher exit is
also checked: remaining background processes are stopped and reported as an execution failure.
Failure to establish the job stops execution rather than launching without lifecycle control.
This supervision covers build/capture subprocesses; Git operations and interactive recording retain
their separate lifecycles.

POSIX uses a process group, including a check for descendants after launcher exit. Processes that
deliberately create a separate session, system services, and externally launched work are outside
that contract. Job objects/process groups control lifetime, not filesystem, credentials, or network
access. Restricted host policies can still reject process creation or cleanup; do not ignore errors.

Implementation references: [Windows job objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
and [Python handle inheritance](https://docs.python.org/3/library/subprocess.html#subprocess.STARTUPINFO).

Generate evidence from actual runtime logic. The adapter must not read revision labels or demo
ground truth to classify runs. Divergence owns classification.

## Browser scenarios

Serve a static entry from the checkout and expose these hooks, or supply equivalent expressions:

```js
window.divergence = {
  reset(seed) { /* reset your application */ },
  snapshot() { return {state: {door: {open: false}}, events: [], logs: [], metrics: {}}; },
  step(milliseconds) { /* optional deterministic simulation step */ }
};
```

```yaml
adapter: browser
browser:
  entry: index.html
  reset_expression: 'seed => window.divergence.reset(seed)'
  state_expression: 'window.divergence.snapshot()'
  tick_expression: 'ms => window.divergence.step(ms)'
  viewport: [960, 540]
  allow_network: false
  actions:
    - {at_ms: 3000, kind: click, target: '#switch'}
```

Without `tick_expression`, Playwright advances virtual browser timers. With it, the simulation owns
time and browser timers are not advanced separately. Actions run after their tick and before the
snapshot, and must align to `sample_ms`. Actions: `click`, `fill`, `press`, trusted JS `evaluate`.
Snapshots return state/events/logs/metrics; the adapter supplies `t_ms` and `frame`.

For an ordinary DOM application, hooks are optional. Each execution opens a fresh browser context.
Use a no-op reset when a fresh page is sufficient and read visible application state:

```yaml
browser:
  entry: index.html
  reset_expression: '() => {}'
  state_expression: '({state: {total_cents: Number(document.querySelector("#total").dataset.cents)}})'
  actions:
    - {at_ms: 250, kind: fill, target: '#quantity', value: '2'}
    - {at_ms: 1000, kind: click, target: '#apply-coupon'}
    - {at_ms: 1500, kind: click, target: '#apply-coupon'}
```

The cart demo generates a complete scenario, independent DOM application, and actual Git history:
`divergence demo --adapter browser --fixture cart --output cart-demo --commits 8 --bug-at 6 --run`.

The browser clock is explicitly paused at `clock_epoch_ms` (default 2024-01-01 UTC) before navigation;
capture time does not advance JavaScript timers. Native CSS/GPU animation is not fully controlled by
that clock. `disable_animations: true` and `reduced_motion: reduce` are explicit presentation-test
options, disabled by default because they can change application behavior and mask animation bugs.

External network requests are blocked by default. This is not a comprehensive WebSocket/OS network
policy. Static builds and mocked dependencies work well; arbitrary backend lifecycle and cross-origin
navigation are not automated. Browser console errors/warnings join the trace. PNG frames follow
scenario time; WebM follows wall time.

Browser setup failures identify the stage (`reset_expression`, `state_expression`, snapshot, action,
navigation, or launch) and scenario time where applicable. A bounded `diagnostic.json` and the full
`process.log` remain in the failed capture directory. The CLI/report surfaces that diagnostic; it
does not treat a missing hook, invalid snapshot, or missing selector as a behavioral regression.

Recording supports a single document, ordinary text/number inputs, textarea, clicks, and selected
keys. Enter/Space button activation is captured once; text spaces are represented by the resulting
fill value. Adjacent fills within one sample are coalesced, so this is not keystroke-exact recording.
Contenteditable, selects, shadow DOM, drag/drop, and arbitrary key combinations are not supported.

Password fields, `autocomplete` password/payment/one-time-code fields, and elements inside
`data-divergence-private` regions are excluded from recorded actions. This does not redact custom
snapshot expressions, application logs, or screenshots. Other typed values may also be sensitive;
use synthetic data and review the YAML before sharing. Stable unique IDs or `data-testid` are best;
duplicate names/IDs fall back to structural selectors, which can break as a page changes. Review
and replay the recipe before a search. See `src/divergence/demo_templates/` for complete examples.

## Resuming and retrying

Resume requires the original repository, resolved revisions, scenario, runner code, and environment
fingerprint. Captured evidence is hashed and validated before reuse. `--resume --retry-inconclusive`
retries checkout/build/capture failures once per invocation; it does not retry unstable behavior or
classifier abstentions. Successful labels remain cached. A retry retains each prior attempt and
compares its completed captures with the fresh execution. If behavior changes between attempts,
the result remains inconclusive, even when the latest repeats agree with each other.

Prior attempts appear in `run.json` and the report's execution ledger. The frame viewer shows the
latest attempt; original files remain in their recorded paths. Fix a scenario or runtime version by
starting a new run, rather than bypassing the fingerprint or editing stored evidence.
