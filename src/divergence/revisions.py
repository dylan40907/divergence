"""Revision providers and source-correlation helpers."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


class RevisionError(RuntimeError):
    pass


def git(
    path: Path,
    *args: str,
    timeout: int = 60,
    extra_env: dict[str, str] | None = None,
) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GCM_INTERACTIVE": "Never",
        }
    )
    environment.update(extra_env or {})
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=", "-c", "commit.gpgsign=false", "-C", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RevisionError(f"Git could not run: {exc}") from exc
    if result.returncode:
        raise RevisionError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout.strip()


class RevisionProvider(Protocol):
    repository: Path
    scope: str

    def resolve(self, ref: str) -> str: ...
    def history(self, good: str, bad: str) -> list[str]: ...
    def prepare(self) -> None: ...
    def checkout(self, revision: str, name: str) -> Path: ...
    def release(self, target: Path) -> None: ...
    def cleanup(self) -> None: ...
    def describe(self, revision: str) -> dict: ...
    def diff(self, revision: str) -> str: ...
    def read_file(self, revision: str, path: str) -> str: ...
    def identity(self) -> dict: ...
    def limitations(self) -> list[str]: ...


class GitProvider:
    scope = "first-parent"

    def __init__(
        self,
        repository: Path,
        run_dir: Path,
        *,
        materialize_lfs: bool = True,
        materialize_submodules: bool = True,
        fetch_lfs: bool = True,
    ):
        self.repository = Path(git(repository.resolve(), "rev-parse", "--show-toplevel")).resolve()
        self.run_dir = run_dir.resolve()
        self.clone = self.run_dir / "source"
        self.checkouts = self.run_dir / "checkouts"
        self.materialize_lfs = materialize_lfs
        self.materialize_submodules = materialize_submodules
        self.fetch_lfs = fetch_lfs

    def resolve(self, ref: str) -> str:
        return git(
            self.repository, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"
        )

    def history(self, good: str, bad: str) -> list[str]:
        good_sha, bad_sha = self.resolve(good), self.resolve(bad)
        chain = git(self.repository, "rev-list", "--first-parent", bad_sha).splitlines()
        if good_sha not in chain:
            raise RevisionError(
                "good must be on bad's first-parent history; merges are not flattened"
            )
        chain = list(reversed(chain[: chain.index(good_sha) + 1]))
        if len(chain) < 2:
            raise RevisionError("good and bad must be different revisions")
        return chain

    def prepare(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if not self.clone.exists():
            git(
                self.run_dir,
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                "--",
                str(self.repository),
                str(self.clone),
                timeout=300,
            )
        origin = Path(git(self.clone, "remote", "get-url", "origin")).resolve()
        if origin != self.repository:
            raise RevisionError("saved clone origin does not match the requested repository")
        self.checkouts.mkdir(exist_ok=True)

    def checkout(self, revision: str, name: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise RevisionError("invalid checkout name")
        target = (self.checkouts / name).resolve()
        if not target.is_relative_to(self.checkouts.resolve()) or target.exists():
            raise RevisionError("checkout target already exists or escapes the run directory")
        git(
            self.clone,
            "-c",
            "filter.lfs.smudge=",
            "-c",
            "filter.lfs.process=",
            "-c",
            "filter.lfs.required=false",
            "worktree",
            "add",
            "--detach",
            str(target),
            revision,
            timeout=300,
            extra_env={"GIT_LFS_SKIP_SMUDGE": "1"},
        )
        try:
            if self.materialize_submodules and (target / ".gitmodules").is_file():
                self._materialize_submodules(target)
            if self.materialize_lfs:
                self._materialize_lfs(target, self.repository, revision)
            return target
        except Exception:
            try:
                self.release(target)
            except RevisionError:
                pass
            raise

    @staticmethod
    def _lfs_pointer(path: Path) -> tuple[str, int] | None:
        if not path.is_file() or path.is_symlink():
            return None
        with path.open("rb") as stream:
            raw = stream.read(1024).decode("ascii", errors="replace").replace("\r\n", "\n")
        match = re.fullmatch(
            r"version https://git-lfs\.github\.com/spec/v1\n"
            r"oid sha256:([0-9a-f]{64})\n"
            r"size ([0-9]+)\n?",
            raw,
        )
        return (match.group(1), int(match.group(2))) if match else None

    @staticmethod
    def _git_common_dir(repository: Path) -> Path:
        common = Path(git(repository, "rev-parse", "--git-common-dir"))
        return common.resolve() if common.is_absolute() else (repository / common).resolve()

    def _copy_local_lfs_objects(
        self, source_repository: Path, target: Path, paths: list[str]
    ) -> None:
        source_root = self._git_common_dir(source_repository) / "lfs" / "objects"
        if not source_root.is_dir():
            return
        target_root = self._git_common_dir(target) / "lfs" / "objects"
        for relative in paths:
            pointer = self._lfs_pointer(target / relative)
            if pointer is None:
                continue
            oid, expected_size = pointer
            source = source_root / oid[:2] / oid[2:4] / oid
            destination = target_root / oid[:2] / oid[2:4] / oid
            if destination.exists():
                continue
            if (
                not source.is_file()
                or source.is_symlink()
                or source.stat().st_size != expected_size
            ):
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.divergence-copy")
            if temporary.exists():
                raise RevisionError("refusing to replace an existing Git LFS cache temporary file")
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)

    def _hydrate_lfs(self, target: Path, paths: list[str]) -> list[str]:
        object_root = self._git_common_dir(target) / "lfs" / "objects"
        missing = []
        for relative in paths:
            destination = (target / relative).resolve()
            if not destination.is_relative_to(target.resolve()):
                raise RevisionError("Git LFS path escapes the isolated checkout")
            pointer = self._lfs_pointer(destination)
            if pointer is None:
                continue
            oid, expected_size = pointer
            source = object_root / oid[:2] / oid[2:4] / oid
            if not source.is_file() or source.is_symlink() or source.stat().st_size != expected_size:
                missing.append(relative)
                continue
            digest = hashlib.sha256()
            temporary = destination.with_name(f".{destination.name}.divergence-lfs")
            if temporary.exists():
                raise RevisionError("refusing to replace an existing Git LFS temporary file")
            try:
                mode = destination.stat().st_mode
                with source.open("rb") as reader, temporary.open("xb") as writer:
                    for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                        digest.update(chunk)
                        writer.write(chunk)
                if digest.hexdigest() != oid:
                    raise RevisionError(f"Git LFS object hash mismatch for {relative}")
                os.chmod(temporary, mode)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return missing

    def _materialize_lfs(
        self, target: Path, source_repository: Path | None, revision: str
    ) -> None:
        attribute_paths = git(
            target, "ls-files", "--", ".gitattributes", "**/.gitattributes"
        ).splitlines()
        if not any(
            "filter=lfs"
            in (target / relative).read_text(encoding="utf-8", errors="replace")
            for relative in attribute_paths
            if (target / relative).is_file()
        ):
            return
        try:
            paths = [
                line.strip()
                for line in git(target, "lfs", "ls-files", "--name-only", timeout=120).splitlines()
                if line.strip()
            ]
        except RevisionError as exc:
            raise RevisionError(
                "this revision uses Git LFS but git-lfs is unavailable; install Git LFS or use --no-git-lfs explicitly"
            ) from exc
        if not paths:
            return
        if source_repository is not None:
            self._copy_local_lfs_objects(source_repository, target, paths)
        missing = self._hydrate_lfs(target, paths)
        if missing and self.fetch_lfs:
            if source_repository is not None:
                try:
                    git(source_repository, "lfs", "fetch", "origin", revision, timeout=1800)
                except RevisionError:
                    pass
                self._copy_local_lfs_objects(source_repository, target, paths)
                missing = self._hydrate_lfs(target, paths)
            if missing and "origin" in git(target, "remote").splitlines():
                try:
                    git(target, "lfs", "fetch", "origin", revision, timeout=1800)
                except RevisionError:
                    pass
                missing = self._hydrate_lfs(target, paths)
        if missing:
            preview = ", ".join(missing[:5])
            raise RevisionError(
                f"Git LFS content is unavailable for {preview}; fetch the objects into the source repository"
            )

    def _materialize_submodules(self, target: Path) -> None:
        common = [
            "-c",
            "protocol.file.allow=always",
            "-c",
            "filter.lfs.smudge=",
            "-c",
            "filter.lfs.process=",
            "-c",
            "filter.lfs.required=false",
        ]
        git(target, *common, "submodule", "sync", "--recursive", timeout=300)
        git(
            target,
            *common,
            "submodule",
            "update",
            "--init",
            "--recursive",
            "--checkout",
            "--force",
            "--jobs",
            "1",
            timeout=1800,
            extra_env={"GIT_LFS_SKIP_SMUDGE": "1"},
        )
        status = git(target, "submodule", "status", "--recursive").splitlines()
        for line in status:
            match = re.match(r"^[ +\-U]?([0-9a-fA-F]{40,64})\s+(.+?)(?:\s+\(.*\))?$", line)
            if not match:
                raise RevisionError("Git returned malformed recursive submodule status")
            revision, relative = match.groups()
            if line.startswith(("-", "+", "U")):
                raise RevisionError(f"submodule was not materialized at its recorded commit: {relative}")
            submodule = (target / relative).resolve()
            if not submodule.is_relative_to(target.resolve()):
                raise RevisionError("submodule path escapes the isolated checkout")
            if self.materialize_lfs:
                origin = git(submodule, "remote", "get-url", "origin")
                source = Path(origin).resolve() if Path(origin).exists() else None
                self._materialize_lfs(submodule, source, revision)

    def release(self, target: Path) -> None:
        target = target.resolve()
        if (
            not target.is_relative_to(self.checkouts.resolve())
            or target == self.checkouts.resolve()
        ):
            raise RevisionError("refusing to remove a checkout outside this run")
        if (target / ".gitmodules").is_file():
            try:
                git(target, "submodule", "deinit", "--force", "--all", timeout=300)
            except RevisionError:
                pass
        git(self.clone, "worktree", "remove", "--force", str(target), timeout=300)

    def describe(self, revision: str) -> dict:
        raw = git(self.repository, "show", "-s", "--format=%H%x00%s%x00%an%x00%aI", revision)
        sha, subject, author, date = raw.split("\x00", 3)
        return {"sha": sha, "subject": subject, "author": author, "date": date}

    def diff(self, revision: str) -> str:
        # For a merge boundary, the searched change is relative to its first parent,
        # not Git's default combined diff (which can omit the introduced code).
        return git(
            self.repository,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--unified=5",
            f"{revision}^1",
            revision,
        )[:200000]

    def read_file(self, revision: str, path: str) -> str:
        return git(self.repository, "show", "--no-ext-diff", "--no-textconv", f"{revision}:{path}")[
            :500000
        ]

    def cleanup(self) -> None:
        """Git worktrees are released after each capture; keep the private clone for resume."""

    def identity(self) -> dict:
        return {
            "kind": "git",
            "repository": str(self.repository),
            "materialize_lfs": self.materialize_lfs,
            "materialize_submodules": self.materialize_submodules,
            "fetch_lfs": self.fetch_lfs,
        }

    def limitations(self) -> list[str]:
        limits = ["Searches a Git first-parent history."]
        if self.materialize_lfs:
            limits.append(
                "Materializes tracked Git LFS objects from the local source cache and then its configured remote; unavailable objects make a trial inconclusive."
            )
        else:
            limits.append("Git LFS materialization was explicitly disabled; pointer files remain.")
        if self.materialize_submodules:
            limits.append(
                "Initializes recursive submodules at recorded commits; source correlation remains scoped to the parent repository."
            )
        else:
            limits.append("Git submodule materialization was explicitly disabled.")
        return limits


P4Runner = Callable[[list[str], Path, str | None, int], subprocess.CompletedProcess[str]]


def _default_p4_runner(
    argv: list[str], cwd: Path, input_text: str | None, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        env=os.environ.copy(),
    )


def _form_fields(form: str) -> dict[str, list[str]]:
    """Parse a P4 form without interpreting values or continuation text."""
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in form.splitlines():
        match = re.match(r"^([A-Za-z][A-Za-z0-9]*):(?:\s*(.*))?$", line)
        if match:
            current = match.group(1)
            value = (match.group(2) or "").strip()
            fields[current] = [value] if value else []
        elif current and (line.startswith("\t") or line.startswith("    ")):
            fields[current].append(line.strip())
    return fields


def _set_form_field(form: str, field: str, values: list[str]) -> str:
    lines = form.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(field + ":")), None)
    replacement = [f"{field}:" + (f"\t{values[0]}" if len(values) == 1 else "")]
    if len(values) != 1:
        replacement.extend(f"\t{value}" for value in values)
    if start is None:
        lines.extend(["", *replacement])
    else:
        end = start + 1
        while end < len(lines) and (not lines[end] or lines[end][0].isspace()):
            end += 1
        lines[start:end] = replacement
    return "\n".join(lines) + "\n"


class PerforceProvider:
    """Submitted-changelist provider using a dedicated clone of a UGS/P4 client view."""

    scope = "submitted-changelists"

    def __init__(
        self,
        repository: Path,
        run_dir: Path,
        *,
        client: str | None = None,
        filespec: str | None = None,
        executable: str = "p4",
        runner: P4Runner | None = None,
    ):
        self.repository = repository.resolve()
        self.run_dir = run_dir.resolve()
        self.executable = executable
        self.runner = runner or _default_p4_runner
        self.template_client = client
        self.filespec = filespec
        self.workspace = (self.run_dir / "perforce-workspace").resolve()
        self._prepared = False
        self._active = False
        info = self._records("info", client=client)[0]
        self.server = str(info.get("serverAddress") or "")
        self.user = str(info.get("userName") or "")
        self.template_client = client or str(info.get("clientName") or "")
        if not self.server or not self.user or not self.template_client or self.template_client == "*unknown*":
            raise RevisionError(
                "Perforce connection needs a logged-in user and mapped client; pass --p4-client if discovery fails"
            )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.template_client):
            raise RevisionError("Perforce client name contains unsupported characters")
        self.template_fields = _form_fields(
            self._invoke("client", "-o", self.template_client, client=self.template_client)
        )
        roots = self.template_fields.get("Root", []) + self.template_fields.get("AltRoots", [])
        resolved_roots = [Path(root).resolve() for root in roots if root and root.lower() != "null"]
        self.active_root = next(
            (root for root in resolved_roots if self.repository.is_relative_to(root)), None
        )
        if self.active_root is None:
            raise RevisionError(
                "the selected Perforce client does not contain the requested workspace path"
            )
        if self.workspace.is_relative_to(self.active_root) or self.active_root.is_relative_to(
            self.workspace
        ):
            raise RevisionError(
                "Perforce run output must be outside the active UGS client root so isolation and cleanup cannot touch it"
            )
        if filespec is None:
            self.filespec = f"//{self.template_client}/..."
        elif not filespec.startswith("//") or "@" in filespec or "#" in filespec:
            raise RevisionError("--p4-filespec must be a depot/client filespec without a revision")
        digest = hashlib.sha256(
            f"{self.server}\0{self.user}\0{self.template_client}\0{self.run_dir}".encode()
        ).hexdigest()[:16]
        self.client = f"divergence_{digest}"
        self._stream = False

    def _require_safe_operation(
        self,
        args: tuple[str, ...],
        *,
        selected_client: str | None,
        input_text: str | None,
    ) -> None:
        """Constrain P4 to history reads and an owned disposable client.

        This is a defense-in-depth boundary: adding a new P4 operation requires an explicit review
        here. In particular, depot mutations such as submit, shelve, edit, add, delete, integrate,
        and reconcile are never accepted.
        """

        def refuse() -> None:
            operation = args[0] if args else "<empty>"
            raise RevisionError(
                f"Perforce operation {operation!r} is outside Divergence's read/isolation allowlist"
            )

        if not args:
            refuse()
        operation = args[0]
        owned_client = getattr(self, "client", None)
        if operation == "info" and len(args) == 1:
            return
        if operation == "changes":
            return
        if operation == "change" and len(args) == 3 and args[1] == "-o":
            return
        if operation == "describe" and len(args) == 3 and args[1] in {"-s", "-du"}:
            return
        if operation == "print" and len(args) == 3 and args[1] == "-q":
            return
        if operation in {"sync", "revert"}:
            if owned_client and selected_client == owned_client:
                return
            refuse()
        if operation != "client":
            refuse()

        tail = args[1:]
        if len(tail) == 2 and tail[0] == "-o" and tail[1] in {
            self.template_client,
            owned_client,
        }:
            return
        if (
            len(tail) == 4
            and tail[0] == "-t"
            and tail[1] == self.template_client
            and tail[2] == "-o"
            and owned_client
            and tail[3] == owned_client
        ):
            return
        if tail == ("-i",) and owned_client and input_text:
            if _form_fields(input_text).get("Client") == [owned_client]:
                return
        if len(tail) == 2 and tail[0] == "-d" and owned_client and tail[1] == owned_client:
            return
        refuse()

    def _invoke(
        self,
        *args: str,
        client: str | None = None,
        json_output: bool = False,
        input_text: str | None = None,
        timeout: int = 120,
    ) -> str:
        argv = [self.executable]
        if json_output:
            argv.extend(["-ztag", "-Mj"])
        argv.extend(["-d", str(self.repository)])
        selected = client if client is not None else self.template_client
        if selected:
            argv.extend(["-c", selected])
        self._require_safe_operation(args, selected_client=selected, input_text=input_text)
        argv.extend(args)
        try:
            result = self.runner(argv, self.repository, input_text, timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RevisionError(f"Perforce could not run: {exc}") from exc
        if result.returncode:
            message = result.stderr.strip() or result.stdout.strip() or f"p4 {args[0]} failed"
            raise RevisionError(message[:2000])
        return result.stdout

    def _records(self, *args: str, client: str | None = None) -> list[dict]:
        raw = self._invoke(*args, client=client, json_output=True)
        records = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RevisionError("Perforce returned malformed JSON output") from exc
            if record.get("code") in {"error", "fatal"}:
                raise RevisionError(str(record.get("data") or "Perforce command failed")[:2000])
            if record.get("code") in {None, "stat"}:
                records.append(record)
        if not records:
            raise RevisionError(f"Perforce returned no records for {args[0]}")
        return records

    def inspect(self) -> dict:
        return {
            "server": self.server,
            "user": self.user,
            "template_client": self.template_client,
            "active_client_root": str(self.active_root),
            "stream": (self.template_fields.get("Stream") or [""])[0] or "classic view",
            "search_filespec": self.filespec,
            "temporary_client": self.client,
            "temporary_root": str(self.workspace),
        }

    def resolve(self, ref: str) -> str:
        if ref == "HEAD":
            records = self._records("changes", "-m", "1", "-s", "submitted", self.filespec)
            ref = str(records[0].get("change", ""))
        if not re.fullmatch(r"[1-9][0-9]*", ref):
            raise RevisionError("Perforce revisions must be submitted numeric changelists")
        record = self._records("change", "-o", ref)[0]
        status = str(record.get("Status") or record.get("status") or "").lower()
        change = str(record.get("Change") or record.get("change") or ref)
        if change != ref or status != "submitted":
            raise RevisionError(f"Perforce changelist {ref} is not submitted or is not visible")
        return ref

    def history(self, good: str, bad: str) -> list[str]:
        first, last = self.resolve(good), self.resolve(bad)
        if int(first) >= int(last):
            raise RevisionError("good must be an older changelist than bad")
        records = self._records(
            "changes", "-s", "submitted", "-r", "-l", f"{self.filespec}@{first},@{last}"
        )
        changes = {str(record["change"]) for record in records if "change" in record}
        changes.update({first, last})
        history = sorted((change for change in changes if int(first) <= int(change) <= int(last)), key=int)
        if len(history) < 2:
            raise RevisionError("Perforce interval contains fewer than two visible revisions")
        return history

    def prepare(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if self._prepared:
            return
        if self.workspace.exists() and any(self.workspace.iterdir()):
            raise RevisionError("Perforce workspace root is not empty; cleanup or use a new output")
        self.workspace.mkdir(parents=True, exist_ok=True)
        client_created = False
        try:
            form = self._invoke(
                "client", "-t", self.template_client, "-o", self.client, client=self.template_client
            )
            fields = _form_fields(form)
            if fields.get("Client") != [self.client]:
                form = _set_form_field(form, "Client", [self.client])
            form = _set_form_field(form, "Root", [str(self.workspace)])
            form = _set_form_field(form, "Host", [])
            form = _set_form_field(
                form,
                "Description",
                ["Temporary Divergence regression workspace; safe to delete after the run."],
            )
            form = _set_form_field(form, "AltRoots", [])
            form = _set_form_field(form, "ChangeView", [])
            form = _set_form_field(form, "StreamAtChange", [])
            parsed = _form_fields(form)
            view = parsed.get("View", [])
            self._stream = bool(parsed.get("Stream"))
            if view and any(
                not line.startswith("-") and f"//{self.client}/" not in line for line in view
            ):
                raise RevisionError(
                    "cloned Perforce client view does not map only to the temporary client"
                )
            self._invoke("client", "-i", client=self.template_client, input_text=form)
            client_created = True
            saved = _form_fields(self._invoke("client", "-o", self.client, client=self.client))
            if saved.get("Root") != [str(self.workspace)]:
                raise RevisionError("Perforce server did not preserve the isolated workspace root")
            self._prepared = True
        except Exception:
            if client_created:
                self._delete_client(best_effort=True)
            try:
                self._remove_workspace()
            except OSError:
                pass
            raise

    def checkout(self, revision: str, name: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise RevisionError("invalid checkout name")
        if not self._prepared or self._active:
            raise RevisionError("Perforce workspace is not ready or is already active")
        revision = self.resolve(revision)
        self.workspace.mkdir(parents=True, exist_ok=True)
        args = ["sync", "-q"]
        if self._stream:
            args.append(f"--use-stream-change={revision}")
        args.append(f"//{self.client}/...@{revision}")
        self._invoke(*args, client=self.client, timeout=3600)
        self._active = True
        return self.workspace

    def _remove_workspace(self) -> None:
        target = self.workspace.resolve()
        expected = (self.run_dir / "perforce-workspace").resolve()
        if target != expected or not target.is_relative_to(self.run_dir):
            raise RevisionError("refusing to remove a Perforce workspace outside this run")
        if not target.exists():
            return

        def writable(function, path, _error):
            os.chmod(path, stat.S_IWRITE)
            function(path)

        shutil.rmtree(target, onerror=writable)

    def release(self, target: Path) -> None:
        if target.resolve() != self.workspace.resolve():
            raise RevisionError("refusing to release an unknown Perforce workspace")
        try:
            try:
                self._invoke("revert", f"//{self.client}/...", client=self.client)
            except RevisionError as exc:
                if "not opened" not in str(exc).lower():
                    raise
            self._invoke("sync", "-q", f"//{self.client}/...#none", client=self.client, timeout=3600)
        finally:
            self._active = False
            self._remove_workspace()

    def _delete_client(self, *, best_effort: bool = False) -> None:
        try:
            self._invoke("client", "-d", self.client, client=self.template_client)
        except RevisionError:
            if not best_effort:
                raise

    def cleanup(self) -> None:
        if not self._prepared:
            return
        try:
            if self._active:
                self.release(self.workspace)
        finally:
            try:
                self._delete_client()
            finally:
                self._prepared = False
                self._active = False
                self._remove_workspace()

    def describe(self, revision: str) -> dict:
        revision = self.resolve(revision)
        record = self._records("describe", "-s", revision)[0]
        description = str(record.get("desc") or record.get("Description") or "").strip()
        timestamp = int(record.get("time") or 0)
        return {
            "sha": revision,
            "subject": description.splitlines()[0][:200] if description else f"Changelist {revision}",
            "author": str(record.get("user") or record.get("User") or "unknown"),
            "date": datetime.fromtimestamp(timestamp, UTC).isoformat() if timestamp else "",
            "revision_kind": "perforce-changelist",
        }

    def diff(self, revision: str) -> str:
        raw = self._invoke("describe", "-du", self.resolve(revision), timeout=300)[:200000]
        blocks = []
        matches = list(re.finditer(r"^==== (//.+?)#\d+ .*?====\s*$", raw, re.MULTILINE))
        for index, match in enumerate(matches):
            depot = match.group(1)
            path = depot.removeprefix("//")
            end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
            body = raw[match.end() : end]
            body = re.sub(r"^--- .*?$", f"--- a/{path}", body, flags=re.MULTILINE)
            body = re.sub(r"^\+\+\+ .*?$", f"+++ b/{path}", body, flags=re.MULTILINE)
            blocks.append(f"diff --git a/{path} b/{path}\n{body.lstrip()}")
        return "\n".join(blocks)[:200000]

    def read_file(self, revision: str, path: str) -> str:
        depot = "//" + path.lstrip("/")
        return self._invoke("print", "-q", f"{depot}@{self.resolve(revision)}", timeout=300)[:500000]

    def identity(self) -> dict:
        return {
            "kind": "perforce",
            "server": self.server,
            "user": self.user,
            "template_client": self.template_client,
            "filespec": self.filespec,
        }

    def limitations(self) -> list[str]:
        return [
            "Searches visible submitted Perforce changelists affecting the configured filespec.",
            "Uses a dedicated temporary client cloned from the selected UGS/Perforce client view; the active workspace is not synced or cleaned.",
            "Perforce permissions, UGS sync filters, stream imports, and available precompiled binaries still determine what can be reproduced.",
        ]


def rank_changes(diff: str, evidence_paths: list[str]) -> list[dict]:
    tokens = {
        token.lower()
        for path in evidence_paths
        for token in re.split(r"[^a-zA-Z0-9]+", path)
        if len(token) > 2 and token not in {"state", "events", "metrics"}
    }
    ranked = []
    for block in re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE):
        filename = re.search(r"^\+\+\+ b/(.+)$", block, re.MULTILINE)
        if not filename:
            continue
        path = filename.group(1)
        added = "\n".join(
            line[1:]
            for line in block.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        matches = sorted(token for token in tokens if token in (path + "\n" + added).lower())
        hunks = [
            {"line": int(m.group(1)), "context": m.group(2).strip()}
            for m in re.finditer(r"^@@ .*? \+(\d+)(?:,\d+)? @@(.*)$", block, re.MULTILINE)
        ]
        changed_lines = []
        current_line = None
        for line in block.splitlines():
            hunk = re.match(r"^@@ .*? \+(\d+)(?:,\d+)? @@", line)
            if hunk:
                current_line = int(hunk.group(1))
            elif current_line is not None and line.startswith("+"):
                changed_lines.append(current_line)
                current_line += 1
            elif current_line is not None and line.startswith(" "):
                current_line += 1
        ranked.append(
            {
                "path": path,
                "matches": matches,
                "relevance": len(matches),
                "hunks": hunks,
                "changed_lines": changed_lines,
                "explanation": "Changed text overlaps runtime evidence terms."
                if matches
                else "Changed in the boundary commit; no matching runtime terms.",
            }
        )
    return sorted(ranked, key=lambda item: (-item["relevance"], item["path"]))


def python_regions(source: str, changed_lines: list[int]) -> list[dict]:
    """Locate changed lines in Python's syntax tree without importing target code."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    regions = []

    def visit(node, prefix=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                overlap = [
                    line
                    for line in changed_lines
                    if child.lineno <= line <= (child.end_lineno or child.lineno)
                ]
                if overlap and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    regions.append(
                        {
                            "name": name,
                            "start": child.lineno,
                            "end": child.end_lineno,
                            "changed_lines": overlap,
                        }
                    )
                visit(child, name + ".")
            else:
                visit(child, prefix)

    visit(tree)
    return regions
