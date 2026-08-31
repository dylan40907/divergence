# Scenario and adapter guide

Scenarios are trusted executable configuration. YAML is safely loaded and rejects unknown fields.
Commands are argv arrays, never shell strings. `{python}` resolves to the current interpreter.
Other executable paths should be absolute or on PATH. Commands run at the checkout root; pipes,
`&&`, `$()`, and shell variables are not evaluated. Use a script for complex build logic.

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

Generate evidence from actual runtime logic. The adapter must not read revision labels or demo
ground truth to classify runs. Divergence owns classification.

## Browser instrumentation

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

The browser clock is explicitly paused at `clock_epoch_ms` (default 2024-01-01 UTC) before navigation;
capture time does not advance JavaScript timers. Native CSS/GPU animation is not fully controlled by
that clock. `disable_animations: true` and `reduced_motion: reduce` are explicit presentation-test
options, disabled by default because they can change application behavior and mask animation bugs.

External network requests are blocked by default. This is not a comprehensive WebSocket/OS network
policy. Static builds and mocked dependencies work well; arbitrary backend lifecycle and cross-origin
navigation are not automated. Browser console errors/warnings join the trace. PNG frames follow
scenario time; WebM follows wall time.

Recording supports a single document. Password fields are excluded, but other typed values can
still be sensitive. Prefer stable IDs or `data-testid`; structural selectors can be fragile. Review
and replay the generated recipe before a search. See `src/divergence/demo_templates/` for complete
Python, browser, and Godot integrations.
