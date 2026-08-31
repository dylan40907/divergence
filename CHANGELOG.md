# Changelog

## 0.3.0a1

- Git checkouts materialize Git LFS objects and recursive submodules at their recorded commits. Missing or corrupt LFS objects stop the trial.
- Unity Version Control (formerly Plastic SCM) provider: branch changesets, read-only connection check, exact changeset loading in a disposable static workspace.
- Subversion provider: path-scoped revision history, exact disposable checkouts, externals off by default.
- Perforce operation allowlist: depot-mutating commands are refused; sync and revert run only against the run-owned temporary client.
- `--p4-filespec` limits both history discovery and historical sync.
- Temporary Perforce and UVCS workspace names are neutral and do not name the tool.

## 0.2.0a1

- Perforce provider for submitted changelists, with filespec-scoped history, exact-change sync, stream support, changelist metadata and diffs.
- UGS-safe isolation: each run clones the selected client spec into a temporary client. The active workspace is never synced or cleaned.
- `perforce-check` (read-only), `--scm perforce` for `verify` and `bisect`.
- `unreal-init` and an Unreal Automation report bridge.

## 0.1.0a1

- Git first-parent bisection with endpoint validation, repeated captures, skips, ambiguity detection, a full-history audit and resume.
- Command trace protocol, deterministic browser capture and recording, and demo apps for the command, browser, Pygame and Godot adapters.
- Scenario starters, validation, single-revision verification, and portable HTML reports with synchronized playback and source correlation.
- State, event, log, image, motion and metric comparison; explicit assertions by default; optional learned fusion (scikit-learn) with held-out evaluation.
- Windows job objects for build and capture processes, capture size limits, link rejection, and atomic report writes.
- CI on Linux, Windows and macOS with Python 3.11 and 3.12.
