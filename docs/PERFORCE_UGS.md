# Perforce, UnrealGameSync, and Unreal Automation

Divergence can search submitted Helix Core changelists while an UnrealGameSync workspace remains
open and unchanged. It uses the `p4` command-line client and the client spec UGS already uses. UGS
does not need to be running, and Divergence does not click it, edit its settings, or use its local
sync state as a test checkout.

## What you need

- Python 3.11+ with the Divergence wheel installed.
- A `p4` executable compatible with your server. If it is not on PATH, pass its full path with
  `--p4-executable`.
- A valid P4 login/ticket and permission to read the selected client spec, changelists, file
  contents, and revisions.
- An existing UGS/P4 client whose root contains the path passed to `--repo`.
- A deterministic test command. For Unreal Automation, the selected changelists must contain a
  `.uproject` and have access to a compatible `UnrealEditor-Cmd` executable.

No Gmail, GitHub, cloud account, public repository, or paid service is needed. Divergence does not
store a P4 password or ticket in its run manifest. Normal P4 environment, `P4CONFIG`, ticket, SSO,
and trust behavior remains controlled by your existing Perforce installation.

## 1. Check the connection without changing anything

Run this from PowerShell, replacing the path with any existing directory inside the UGS client:

```powershell
divergence perforce-check --repo C:\UGSWorkspace
```

The command reads `p4 info`, the selected client spec, and the latest visible submitted changelist.
Confirm the displayed server, user, client, client root, stream/classic view, and history filespec.
If auto-detection selects the wrong client:

```powershell
divergence perforce-check --repo C:\UGSWorkspace --p4-client example_machine_Project_Dev
```

To search only the game project rather than every change in the client view:

```powershell
divergence perforce-check --repo C:\UGSWorkspace --p4-filespec //example_machine_Project_Dev/ExampleGame/...
```

This check does not create, sync, change, or delete a client.

No Divergence command submits, shelves, opens, adds, deletes, integrates, or reconciles depot
content. The provider enforces an internal P4 operation allowlist, and permits sync/revert only on
the run-owned temporary client. A real verification or search is nevertheless not strictly
server-read-only: it briefly creates/deletes that client spec and updates its have-list. Review that
metadata behavior with the depot owner before running against a company server. No scenario or test
file needs to be added to the target project; keep scenarios outside the production workspace.

## 2. Create an Unreal Automation scenario

Choose an Automation test that expresses the behavior that must remain correct. Project tests are
usually more useful than a broad engine suite because bisection runs the test at several revisions
and repeats every tested revision at least twice.

```powershell
divergence unreal-init `
  --project ExampleGame\ExampleGame.uproject `
  --test Project.Functional.Door `
  --editor UnrealEditor-Cmd.exe `
  --output C:\DivergenceScenarios\door.yaml

divergence validate C:\DivergenceScenarios\door.yaml
```

`--project` is relative to the P4 client root. `--editor` may be an absolute executable, a PATH
name, or a path relative to the temporary client. Add trusted `build:` argv entries to the YAML if
each changelist must be compiled. Disable the default graphics behavior only when appropriate:
`--null-rhi` is useful for non-rendering tests but can invalidate rendering or screenshot tests.

The bridge starts Unreal with unattended Automation arguments and `-nop4`, exports the official
Automation report, preserves `unreal-report/index.json` and the editor log, and turns these counts
into assertions:

- at least one test ran;
- failed, not-run, and in-process counts are zero;
- two independent executions agree.

The report bridge captures aggregate Automation results and named failure/warning logs. To compare
actor state, transforms, screenshots, performance, or the exact moment behavior changes, emit the
normal Divergence trace protocol from project-side automation instead.

## 3. Verify a single submitted changelist

Use a changelist you expect to pass. Keep output outside the entire UGS client root. Omitting
`--output` uses a safe per-user location outside the workspace on Windows.

```powershell
divergence verify `
  --scm perforce `
  --repo C:\UGSWorkspace `
  --revision 12000 `
  --scenario C:\DivergenceScenarios\door.yaml `
  --p4-client example_machine_Project_Dev
```

Open the emitted `report.html`. `VERIFIED` means the configured test passed twice and the captures
agreed. `CHECK_FAILED` means the behavior consistently failed. `INCONCLUSIVE` means checkout,
build, launch, report parsing, or repeatability failed; inspect the report's execution ledger.

## 4. Search the submitted history

Once a good changelist verifies and a later changelist consistently fails:

```powershell
divergence bisect `
  --scm perforce `
  --repo C:\UGSWorkspace `
  --good 12000 `
  --bad 12340 `
  --scenario C:\DivergenceScenarios\door.yaml `
  --p4-client example_machine_Project_Dev `
  --p4-filespec //example_machine_Project_Dev/ExampleGame/...
```

The endpoints must be numeric, visible, submitted changelists, with good older than bad. `HEAD`
means the latest visible submitted change affecting the filespec. Binary search assumes the failure
remains present after introduction. Use `--audit` to test every visible change when it may disappear
and return later. Resume an unchanged interrupted run with the same arguments, original `--output`,
and `--resume`; add `--retry-inconclusive` only for transient execution failures.

## Isolation behavior

For each investigation, Divergence:

1. reads the selected template client and validates that `--repo` is inside its Root or AltRoots;
2. rejects output inside or above the complete active client root;
3. clones the client spec to a deterministic, brand-neutral `isolated_<digest>` client with a
   neutral OS-temporary root owned by that run;
4. clears Host, AltRoots, ChangeView, and StreamAtChange from the cloned form;
5. validates classic-view mappings point only at the temporary client;
6. syncs only the configured filespec at exact changelists into the temporary root, using
   stream-at-change semantics for streams;
7. reverts and flushes the temporary client, removes its run-owned files, and deletes its spec.

Cleanup also runs after checkout, build, capture, and report failures. Divergence refuses a nonempty
temporary root and validates paths before recursive removal. Application and build commands still
run with your user permissions, so only use trusted source and scenarios.

UGS sync filters, stream imports, server protections, typemap behavior, external build products,
and precompiled-binary availability differ by installation. Cloning the client spec reproduces its
P4 view; it does not reproduce every UGS UI preference or guarantee that binaries for an old change
exist. A restricted change becomes inconclusive rather than being labeled good or bad.

## Test coverage

The Perforce provider was tested against a fresh official Helix Core 2026.1 stream server with seven real
submitted changes. The planted first bad change was found, all completed captures were reused on
resume, the active workspace sentinel was unchanged, the temporary client was deleted, its local
ticket was removed, and the owned server stopped. Fake-protocol tests also cover unsafe client maps,
output nested in an active root, interrupted sync cleanup, invalid changes, and malformed Unreal
reports. It has not been tested against every authentication, edge-server, or build-farm
configuration.

Official behavior references: [P4 changes](https://help.perforce.com/helix-core/server-apps/cmdref/current/Content/CmdRef/p4_changes.html),
[P4 sync](https://help.perforce.com/helix-core/server-apps/cmdref/current/Content/CmdRef/p4_sync.html),
[P4 client](https://help.perforce.com/helix-core/server-apps/cmdref/current/Content/CmdRef/p4_client.html),
[running Unreal Automation tests](https://dev.epicgames.com/documentation/unreal-engine/run-automation-tests-in-unreal-engine),
and [reviewing Unreal test reports](https://dev.epicgames.com/documentation/unreal-engine/review-test-results-in-unreal-engine).
