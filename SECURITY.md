# Security and privacy

Only run trusted repositories and scenarios. Builds, applications, native engines, and browser
expressions run with your user's permissions. Private clones isolate checkout changes, **not
filesystem or network access**. They do not protect credentials from malicious target code.

- Use a disposable VM for unknown applications. Container execution is not implemented.
- Commands avoid shell interpolation, but the executed commands themselves remain trusted code.
- Perforce never submits, shelves, opens, adds, deletes, integrates, or reconciles depot content.
  An internal operation allowlist rejects those commands before invoking `p4`; sync and revert are
  accepted only for the exact run-owned temporary client. Perforce runs clone the selected client
  spec into a run-owned, brand-neutral `isolated_*` client whose root is a neutral OS-temporary
  path. Its description is also brand-neutral. That client creation/deletion and its have-list are
  temporary server metadata, so an execution run is not strictly server-read-only even though depot
  content remains read-only. The `perforce-check` command is metadata-read-only. Divergence
  validates the active Root/AltRoots boundary, temporary root, and classic-view right-hand mappings;
  it never intentionally syncs, reverts, or cleans the active UGS client. A compromised `p4`
  executable/server or trusted build still runs with your permissions. P4 authentication stays
  in the existing environment/ticket store and is excluded from manifests; reports retain server,
  user, client, changelist, diff, and local-path metadata.
- Evidence can contain sensitive screenshots, logs, source diffs, author metadata and local paths.
  Nothing uploads automatically. Review portable reports before sharing; there is no automatic
  redaction. Browser recording saves non-password input values too.
- The read-only report server binds to loopback, rejects unexpected Host headers, disables framing,
  and exposes only reports. Other local users/processes can access it. There is no execution API.
- Report values are inserted as text; embedded JSON is base64 encoded. Reports load no CDNs and
  send no telemetry. Local model files are validated JSON coefficients, never executable pickle.
- Resume checks scenario/runner/environment identity and evidence hashes. It is recovery support,
  not a cryptographically authenticated chain of custody.
- Report export checks the saved scenario digest and every completed capture, including previous
  attempts, logs, and videos not embedded in the viewer. It rejects missing or modified evidence
  before replacing a report. Failed-attempt diagnostic folders are labeled separately and are not
  represented as completed, hash-validated captures. Reports cannot overwrite run evidence folders.
- Timeouts and size limits are guardrails, not complete quotas against hostile resource use.
  Capture limits are polled and can be overshot; they do not cover builds or total disk usage.
  Artifact inspection rejects links and junctions but is not a defense against hostile concurrent
  filesystem replacement. Run untrusted applications in a separate security boundary.
- Unreal Automation is launched with `-nop4`, but project/editor code can still access the network,
  filesystem, credentials, and source-control tools on its own. Use synthetic test data and inspect
  editor logs and Automation reports before sharing.
- Windows application processes join an owned job before application code starts. Closing the
  runner's lifetime handle terminates that job; errors establishing it prevent execution. This
  is lifecycle control, not an OS security sandbox. POSIX process groups cannot contain work that
  deliberately creates another session or runs through an external service.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. Do not post credentials or private captures in public issues.
