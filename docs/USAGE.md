# Usage

## Your own repository

Generate a starting scenario without running code or installing dependencies:

```sh
# An executable that implements the trace protocol:
divergence init --adapter command --output scenarios/open-door.yaml -- "{python}" tools/capture.py
# Or a local static browser app, without custom application hooks:
divergence init --adapter browser --entry index.html --output scenarios/browser.yaml
```

Existing files are never overwritten. Starters use **difference-only repeatability checks**, with
no guessed assertions or actions. Customize the generated YAML for your application: add the actions
to replay, choose meaningful state fields, add behavioral assertions, and set `policy.mode: assertions`.
The browser starter reads the page title and visible body text; review captures for sensitive content.
The command starter requires your own trace-producing integration, not just an arbitrary executable.
[Scenario setup and trace protocol](ADAPTERS.md).

```sh
divergence validate scenarios/open-door.yaml
divergence verify --repo /path/to/repository --revision v1.2.0 \
  --scenario scenarios/open-door.yaml --output .divergence/runs/preflight
divergence bisect --repo /path/to/repository --good v1.2.0 --bad HEAD \
  --scenario scenarios/open-door.yaml --output .divergence/runs/investigation
```

PowerShell users should put commands on one line or use PowerShell's backtick continuation.
Your branch, index, and uncommitted files are not checked out or reset. Each repeat uses a fresh
worktree of a private local clone. Builds run **trusted code with your user permissions**;
disposable checkouts are not a security sandbox.

Git LFS content and recursive submodules are materialized by default. Submodules are checked out at
the commits recorded by the parent revision. If an LFS object cannot be retrieved or verified, the
trial stops instead of running a pointer file. See the [version-control guide](VERSION_CONTROL.md)
for flags and for UVCS/SVN onboarding.

For an UnrealGameSync workspace, Divergence talks to the same Helix Core server through `p4`; it
does not automate or modify the UGS UI. Start with the read-only connection check, create an Unreal
Automation scenario, verify one submitted changelist, then search a known-good/bad range:

```powershell
divergence perforce-check --repo C:\UGSWorkspace
divergence unreal-init --project Game\Game.uproject --test Project.Functional.Door --editor UnrealEditor-Cmd.exe --output C:\DivergenceScenarios\door.yaml
divergence verify --scm perforce --repo C:\UGSWorkspace --revision 12340 --scenario C:\DivergenceScenarios\door.yaml
divergence bisect --scm perforce --repo C:\UGSWorkspace --good 12000 --bad 12340 --scenario C:\DivergenceScenarios\door.yaml
```

The client is discovered from the workspace, or selected with `--p4-client`. A real run clones its
client spec into a brand-neutral temporary `isolated_*` client, syncs only that client, and deletes
it afterward.
The run directory must be outside the complete active client root; the default Windows location is
under `%LOCALAPPDATA%\Divergence\runs`. The active UGS workspace is never synced, reverted, cleaned,
or deleted. Use `--p4-filespec //your-client/Project/...` to narrow both history and historical
materialization. See the complete
[Perforce/UGS and Unreal guide](PERFORCE_UGS.md) before using a production depot.

Start with `verify` before a history search. It builds and replays **one committed revision** at
least twice, validates the captured traces, checks assertions, and measures repeatability under
your configured tolerances. It works with a single-commit repository and needs no known-bad commit.
The report compares independent replays of the same revision, including the replay that exposed
instability. The original working tree is left untouched; commit changes you want to test first.

| Verification outcome | Meaning | Exit code |
| --- | --- | --- |
| `VERIFIED` | Replays agree under the policy and the configured oracle accepts them | 0 |
| `CHECK_FAILED` | Replays are consistent, but the configured oracle rejects the revision | 3 |
| `INCONCLUSIVE` | Execution failed, evidence is incomplete, replays disagree, or the oracle abstains | 3 |
| Configuration / resume error | Invalid input or changed saved evidence; inspect the CLI diagnostic | 2 |

Passing does not prove general application correctness. In `difference` mode, assertions are not
enforced: verification establishes repeatability under the comparison policy, even if the behavior
is consistently wrong. Use explicit assertions for requirements you want checked. No history search
or source attribution occurs during verification. `--resume` and `--retry-inconclusive` work with the
same integrity checks and evidence retention as bisection; use the original `--output` directory.

Windows builds and captures use owned job objects so descendants do not outlive their run.
A command that exits while leaving background processes is stopped and reported as inconclusive.

```sh
# Capture once when raw evidence is all you need (no repeatability or assertion verdict).
divergence capture --repo /path/to/repository --revision HEAD \
  --scenario scenarios/open-door.yaml --output .divergence/capture
# Compare existing captures without executing the application.
divergence compare good/artifacts/trace.json bad/artifacts/trace.json \
  --scenario scenarios/open-door.yaml --output comparison.json
# Resume: same arguments, original output, unchanged scenario/runner/environment.
divergence bisect --repo /path/to/repository --good v1.2.0 --bad HEAD \
  --scenario scenarios/open-door.yaml --output .divergence/runs/investigation --resume
# Retry transient execution failures, retaining and checking earlier evidence.
# Append --retry-inconclusive to the resume command above.
# Slower: test every revision for fixed-and-reintroduced regressions.
divergence bisect --repo /path/to/repository --good v1.2.0 --bad HEAD \
  --scenario scenarios/open-door.yaml --audit
```

## Adapters and fixtures

| Runtime | Support | Optional setup |
| --- | --- | --- |
| Instrumented executable | JSON trace protocol, any language/engine | Provide an argv command |
| Browser | Playwright, controlled clock, recording/replay, PNGs and WebM | `pip install -e ".[browser]"`; `python -m playwright install chromium` |
| Python/Pygame | Deterministic example through command protocol; Pygame surface output | `pip install -e ".[pygame]"` |
| Godot 4 | Headless GDScript fixture, CPU diagnostic images | Portable Godot executable |
| Unreal Automation | `index.json` results become strict state/log evidence | A runnable `UnrealEditor-Cmd` and committed `.uproject` |
| Perforce / UGS workspace | Submitted-changelist search using a disposable cloned client | Logged-in `p4`; UGS itself is optional |
| Unity Version Control / Plastic SCM | Branch-changeset search using an owned static workspace | Configured `cm` CLI and existing workspace |
| Subversion | Path-revision search using disposable checkouts | `svn` CLI and existing working copy |

```sh
divergence demo --adapter browser --output browser-demo --commits 16 --bug-at 11 --run
divergence demo --adapter browser --fixture cart --output cart-demo --commits 8 --bug-at 6 --run
divergence demo --adapter pygame --output pygame-demo --commits 16 --bug-at 11 --run
divergence demo --adapter godot --godot /path/to/godot --output godot-demo --commits 16 --bug-at 11 --run
divergence doctor
divergence record --repo browser-demo/repository --scenario browser-demo/scenario.yaml --output recorded.yaml
```

Recording uses an existing browser scenario as its instrumentation/assertion template. It opens
the application for the configured duration and records clicks, text input, and selected keys on
scenario ticks. Review selectors and expected behavior, then replay before bisection. Input values
are saved; do not enter secrets. It does not record arbitrary OS input, drags, or multi-page sessions.

The cart fixture uses ordinary DOM selectors, without application-specific capture or clock hooks.
It changes the quantity and applies a coupon twice; the regression stacks the discount. This tests
the browser adapter against a different application structure and an explicit business invariant.

## How verdicts are decided

- Validate both endpoints and replay every tested revision at least twice.
- Default to explicit behavioral assertions; generic difference detection is opt-in.
- Disagreement, missing evidence, build failures, and timeouts are **INCONCLUSIVE**, not BAD.
- Report all possible boundary commits when skipped revisions prevent a unique answer.
- Keep hashes of evidence and a runner/scenario/environment fingerprint for resume.
- Validate all completed evidence before report export, including undisplayed captures and retries.
  Changed or missing files are rejected; an existing report is preserved if export fails.
- Bound each capture's size and entry count; stream hashes of traces, frames, videos, and logs.
- Keep original traces/images alongside portable reports and plain JSON outcomes.
- Opt-in retries only rerun execution failures, once per resume. Earlier captures are retained,
  integrity checked, and compared with fresh ones. Conflicting behavior stays inconclusive;
  flaky verdicts and learned-model abstentions cannot be selectively retried away.

Visual comparison uses downsampled RGB descriptors, local patch pooling, and motion differences.
It does **not** yet use pretrained semantic video embeddings or optical flow.

## Experimental ML

Optional learned fusion uses actual captured features, mutation-family holdout evaluation,
state/visual/rule baselines, and plain JSON weights. It is not enabled by default, not a calibrated
probability, and not claimed superior to rules. Initial evaluation exposed generalization gaps.
[Evaluation protocol and results](EVALUATION.md).

```sh
python -m pip install -e ".[ml]"
divergence benchmark --output .divergence/benchmark
divergence train .divergence/benchmark/dataset.json --output .divergence/model
```

## Scope and limitations

- Git searches use **first-parent** histories. Perforce searches use visible submitted changelists
  affecting one filespec. UVCS searches one branch; SVN searches revisions affecting one working-copy
  path. No arbitrary Git DAG search or remote build farm.
- Binary search assumes the regression stays bad after introduction. `--audit` checks every
  revision at extra cost; skipped revisions still leave uncertainty.
- First divergence means first relevant **observed sample**, not exact causal execution time.
  Raw earlier differences are stored separately. Alignment does not warp away behavioral delays.
- Determinism needs instrumentation. GPU rendering, external services, physics, and performance
  measurements can be unstable; repeated evidence must agree under the chosen tolerances.
- Source ranking matches runtime terms against changed text and hunk locations. It is a navigation
  aid, not proof of a responsible function or an automatically generated fix.
- Portable reports display about 300 regular frames per trial plus divergence samples and neighbors.
  Full traces remain on disk. Portable payloads are bounded to 128 MiB; run manifests to 64 MiB. WebM
  uses wall time; the report's synchronized frames use scenario time.
- The Pygame/Godot fixtures and Unreal Automation bridge prove their integrations; they are not
  general engine recorders. Unreal state/video beyond test results needs project instrumentation.
- No public hosting, multi-user execution server, automatic account creation, or paid model.

