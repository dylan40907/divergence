# Version-control providers

Divergence executes trusted committed code in disposable revision workspaces. It never switches,
resets, cleans, reverts, or updates the active workspace. The disposable workspace protects local
edits from ordinary checkout operations; it is not a security sandbox.

## Support matrix

| Provider | Search scope | Disposable isolation | Required local client |
| --- | --- | --- | --- |
| Git | First-parent commits between good and bad | Worktree from a private local clone | Git; Git LFS when the revision uses it |
| Perforce / UGS | Submitted changes affecting one client filespec | Cloned temporary client | Logged-in `p4` |
| Unity Version Control / Plastic SCM | Numeric changesets on the active branch | Owned static workspace per repeat | Configured `cm` and an existing workspace |
| Subversion | Numeric revisions affecting the selected working-copy path | Fresh checkout per repeat | `svn` and an existing working copy |

## Git LFS and submodules

Git LFS and recursive submodules are enabled by default. Divergence creates the parent worktree with
LFS smudging disabled, initializes each submodule at the exact commit recorded by that parent, then
materializes and verifies LFS objects in the parent and submodules. Local LFS object caches are used
first. When allowed, missing objects are fetched from the private clone's origin and the source
repository's upstream remote. Git credential prompts are disabled so unattended runs fail clearly.

An LFS object must match the pointer's byte size and SHA-256. Missing or corrupt objects make the
trial inconclusive; pointer text is never passed to the application as if it were the asset. Use
`--no-git-lfs-fetch` for an offline run. `--no-git-lfs` and `--no-git-submodules` are deliberate
escape hatches for applications that do not need those contents. Their values are part of resume
identity, so changing them requires a new run directory.

Submodule URLs and their own dependencies remain repository-controlled input. File-protocol URLs are
allowed only for the isolated initialization command so local submodules can be reproduced. Source
ranking covers the parent repository's boundary diff; it does not separately rank commits inside a
submodule.

## Unity Version Control / Plastic SCM

The command-line name is `plastic`; `uvcs` is accepted as an alias.

```powershell
divergence plastic-check --repo C:\path\inside\workspace
divergence verify --scm plastic --repo C:\path\inside\workspace --revision 1250 --scenario scenario.yaml
divergence bisect --scm uvcs --repo C:\path\inside\workspace --good 1200 --bad 1250 --scenario scenario.yaml
```

The check reads workspace, repository, branch, and head metadata. A run creates a uniquely named
static workspace under the run directory, updates it to one exact changeset with merging and prompts
disabled, verifies the loaded changeset, runs one repeat, deletes the workspace registration, and
removes only that owned directory. Output must be outside the active workspace.

Search is limited to numeric changesets created on the active branch. Xlinks, cloaked rules,
permissions, and server retention determine reproducibility. Content inside Xlinked repositories is
not separately source-ranked. A configured real UVCS workspace is required for production use;
Divergence does not create a Unity organization, repository, subscription, or trial.

Use `--cm-executable PATH` when `cm` is not on `PATH`.

## Subversion

```powershell
divergence svn-check --repo C:\path\inside\working-copy
divergence verify --scm svn --repo C:\path\inside\working-copy --revision 150 --scenario scenario.yaml
divergence bisect --scm svn --repo C:\path\inside\working-copy --good 100 --bad HEAD --scenario scenario.yaml
```

SVN history is scoped to the selected working-copy URL, so unrelated repository revisions are not
tested. Each repeat checks out that URL at the exact numeric revision into the run directory and
validates the resulting working copy. The active working copy is only inspected. Embedded URL
credentials are rejected; use the SVN credential store and its normal server-certificate setup.
All commands are non-interactive.

Externals are ignored by default because they may point at mutable or separately authenticated
repositories. Use `--svn-externals` only after reviewing every external and confirming it is needed;
the option becomes part of resume identity. Use `--svn-executable PATH` for a client outside `PATH`.

## Perforce and UGS

Perforce uses a cloned temporary client and searches submitted changes affecting the selected
filespec. See [Perforce, UnrealGameSync, and Unreal Automation](PERFORCE_UGS.md) for client discovery,
stream behavior, safe output placement, Unreal scenarios, and installation-specific limits.

## Choosing another system

TFVC is not implemented. Its enterprise authentication and workspace semantics add meaningful
maintenance cost, while Git, Perforce, UVCS, and SVN cover the product's current target workflows.
Add TFVC only when an actual adopter requires it and can provide a disposable qualification project.
