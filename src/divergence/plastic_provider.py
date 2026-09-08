"""Unity Version Control (Plastic SCM) changeset provider."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from divergence.revisions import RevisionError

PlasticRunner = Callable[[list[str], Path, int], subprocess.CompletedProcess[str]]
_FIELD = "|DIVERGENCE-FIELD|"
_RECORD = "|DIVERGENCE-RECORD|"


def _default_runner(argv: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PLASTICEDITOR"] = ""
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        env=environment,
    )


class PlasticProvider:
    """Search one UVCS branch using an owned static workspace per repeat."""

    scope = "uvcs-branch-changesets"

    def __init__(
        self,
        repository: Path,
        run_dir: Path,
        *,
        executable: str = "cm",
        runner: PlasticRunner | None = None,
    ):
        self.repository = repository.resolve()
        self.run_dir = run_dir.resolve()
        self.executable = executable
        self.runner = runner or _default_runner
        workspace = self._invoke(
            "getworkspacefrompath",
            str(self.repository),
            f"--format={{wkname}}{_FIELD}{{wkpath}}{_FIELD}{{owner}}{_FIELD}{{type}}{_FIELD}{{dynamic}}",
            "--extended",
        ).strip()
        fields = workspace.split(_FIELD)
        if len(fields) != 5 or not fields[0] or not fields[1]:
            raise RevisionError("Unity Version Control returned malformed workspace information")
        self.active_workspace, active_root, self.owner, self.workspace_type, self.dynamic = fields
        self.active_root = Path(active_root).resolve()
        if not self.repository.is_relative_to(self.active_root):
            raise RevisionError("the requested path is not inside the discovered UVCS workspace")
        if self.run_dir.is_relative_to(self.active_root) or self.active_root.is_relative_to(
            self.run_dir
        ):
            raise RevisionError("UVCS run output must be outside the active workspace root")
        header = self._invoke("status", str(self.active_root), "--header").strip()
        parsed = self._parse_header(header)
        self.branch, self.repository_spec, self.active_changeset = parsed
        if not re.fullmatch(r"/[A-Za-z0-9._/-]+", self.branch):
            raise RevisionError("UVCS branch name contains unsupported characters")
        if not re.fullmatch(r"[A-Za-z0-9._@:/-]+", self.repository_spec):
            raise RevisionError("UVCS repository specification contains unsupported characters")
        digest = hashlib.sha256(
            f"{self.repository_spec}\0{self.branch}\0{self.run_dir}".encode()
        ).hexdigest()[:16]
        self.workspace_name = f"isolated_{digest}"
        self._workspace_base = Path(tempfile.gettempdir()).resolve()
        self._workspace_leaf = f"isolated-uvcs-{digest}"
        self.workspace = (self._workspace_base / self._workspace_leaf).resolve()
        if self.workspace.is_relative_to(self.active_root) or self.active_root.is_relative_to(
            self.workspace
        ):
            raise RevisionError("UVCS temporary workspace overlaps the active workspace root")
        self._active = False

    def _invoke(self, *args: str, cwd: Path | None = None, timeout: int = 300) -> str:
        argv = [self.executable, *args]
        try:
            result = self.runner(argv, cwd or self.repository, timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RevisionError(f"Unity Version Control could not run: {exc}") from exc
        if result.returncode:
            message = result.stderr.strip() or result.stdout.strip() or f"cm {args[0]} failed"
            raise RevisionError(message[:2000])
        return result.stdout

    @staticmethod
    def _parse_header(raw: str) -> tuple[str, str, str]:
        line = next((line.strip() for line in raw.splitlines() if line.strip()), "")
        match = re.match(
            r"^(?:br:)?(?P<branch>/[^@\s]+)@(?P<repository>[^\s]+)\s+\(cs:(?P<change>[0-9]+)(?:\s+-[^)]*)?\)$",
            line,
        )
        if not match:
            raise RevisionError("Unity Version Control returned an unrecognized workspace header")
        return match.group("branch"), match.group("repository"), match.group("change")

    def _find_changesets(self, query: str, fields: tuple[str, ...]) -> list[dict[str, str]]:
        format_value = _FIELD.join(f"{{{field}}}" for field in fields) + _RECORD
        raw = self._invoke(
            "find",
            "changesets",
            query,
            f"--format={format_value}",
            "--nototal",
            cwd=self.active_root,
        )
        records = []
        for item in raw.split(_RECORD):
            if not item.strip():
                continue
            values = item.strip("\r\n").split(_FIELD)
            if len(values) != len(fields):
                raise RevisionError("Unity Version Control returned malformed changeset data")
            records.append(dict(zip(fields, values, strict=True)))
        return records

    def inspect(self) -> dict:
        return {
            "active_workspace": self.active_workspace,
            "active_workspace_root": str(self.active_root),
            "active_workspace_type": self.workspace_type,
            "active_workspace_dynamic": self.dynamic,
            "repository": self.repository_spec,
            "branch": self.branch,
            "active_changeset": self.active_changeset,
            "latest_branch_changeset": self.resolve("HEAD"),
            "temporary_workspace": self.workspace_name,
            "temporary_root": str(self.workspace),
        }

    def resolve(self, ref: str) -> str:
        if ref == "HEAD":
            raw = self._invoke("status", str(self.active_root), "--head").strip()
            match = re.search(r"\bcs:([0-9]+)", raw)
            if not match:
                raise RevisionError("Unity Version Control did not report a branch head")
            ref = match.group(1)
        if not re.fullmatch(r"[1-9][0-9]*", ref):
            raise RevisionError("UVCS revisions must be positive numeric changesets or HEAD")
        records = self._find_changesets(
            f"where changesetid = {ref}", ("changesetid", "branch")
        )
        if len(records) != 1 or records[0]["changesetid"] != ref:
            raise RevisionError(f"UVCS changeset {ref} is not visible")
        branch = records[0]["branch"].removeprefix("br:")
        if not branch.startswith("/"):
            branch = "/" + branch
        if branch != self.branch:
            raise RevisionError(f"UVCS changeset {ref} does not belong to branch {self.branch}")
        return ref

    def history(self, good: str, bad: str) -> list[str]:
        first, last = self.resolve(good), self.resolve(bad)
        if int(first) >= int(last):
            raise RevisionError("good must be an older UVCS changeset than bad")
        records = self._find_changesets(
            f"where branch = 'br:{self.branch}'", ("changesetid", "branch")
        )
        revisions = {
            record["changesetid"]
            for record in records
            if record["changesetid"].isdigit()
            and int(first) <= int(record["changesetid"]) <= int(last)
        }
        revisions.update({first, last})
        result = sorted(revisions, key=int)
        if len(result) < 2:
            raise RevisionError("UVCS interval contains fewer than two visible changesets")
        return result

    def prepare(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if self.workspace.exists() and any(self.workspace.iterdir()):
            raise RevisionError("UVCS temporary workspace root is not empty")

    def checkout(self, revision: str, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise RevisionError("invalid checkout name")
        if self._active or self.workspace.exists():
            raise RevisionError("UVCS temporary workspace is already active")
        revision = self.resolve(revision)
        created = False
        try:
            self._invoke(
                "workspace",
                "create",
                self.workspace_name,
                str(self.workspace),
                self.repository_spec,
                cwd=self.run_dir,
                timeout=300,
            )
            created = True
            self._invoke(
                "update",
                ".",
                f"--changeset=cs:{revision}",
                "--forced",
                "--dontmerge",
                "--noinput",
                "--silent",
                cwd=self.workspace,
                timeout=3600,
            )
            header = self._parse_header(
                self._invoke("status", str(self.workspace), "--header", cwd=self.workspace)
            )
            if header[1] != self.repository_spec or header[2] != revision:
                raise RevisionError("UVCS did not preserve the requested isolated changeset")
            self._active = True
            return self.workspace
        except Exception:
            if created:
                self._delete_workspace(best_effort=True)
            self._remove_workspace()
            raise

    def _remove_workspace(self) -> None:
        target = self.workspace.resolve()
        expected = (self._workspace_base / self._workspace_leaf).resolve()
        if target != expected or target.parent != self._workspace_base:
            raise RevisionError("refusing to remove an unknown UVCS temporary workspace")
        if not target.exists():
            return

        def writable(function, path, _error):
            os.chmod(path, stat.S_IWRITE)
            function(path)

        shutil.rmtree(target, onerror=writable)

    def _delete_workspace(self, *, best_effort: bool = False) -> None:
        try:
            self._invoke(
                "workspace", "delete", str(self.workspace), cwd=self.run_dir, timeout=300
            )
        except RevisionError:
            if not best_effort:
                raise

    def release(self, target: Path) -> None:
        if target.resolve() != self.workspace:
            raise RevisionError("refusing to release an unknown UVCS workspace")
        error: RevisionError | None = None
        try:
            self._delete_workspace()
        except RevisionError as exc:
            error = exc
        finally:
            self._active = False
            self._remove_workspace()
        if error:
            raise error

    def cleanup(self) -> None:
        try:
            if self._active or self.workspace.exists():
                self._delete_workspace(best_effort=True)
        finally:
            self._active = False
            self._remove_workspace()

    def _changeset_record(self, revision: str) -> dict[str, str]:
        records = self._find_changesets(
            f"where changesetid = {revision}",
            ("changesetid", "branch", "date", "owner", "comment"),
        )
        if len(records) != 1:
            raise RevisionError(f"UVCS changeset {revision} has no visible metadata")
        return records[0]

    def describe(self, revision: str) -> dict:
        revision = self.resolve(revision)
        record = self._changeset_record(revision)
        comment = record["comment"].strip()
        return {
            "sha": revision,
            "subject": comment.splitlines()[0][:200] if comment else f"Changeset {revision}",
            "author": record["owner"] or "unknown",
            "date": record["date"],
            "revision_kind": "uvcs-changeset",
        }

    @staticmethod
    def _normalize_patch(raw: str) -> str:
        if "diff --git " in raw:
            return raw
        blocks = []
        for block in re.split(r"(?=^Index:\s*)", raw, flags=re.MULTILINE):
            match = re.match(r"^Index:\s*(.+?)\s*$", block, re.MULTILINE)
            if not match:
                continue
            path = match.group(1).replace("\\", "/").lstrip("/")
            body = re.sub(r"^--- .*?$", f"--- a/{path}", block, flags=re.MULTILINE)
            body = re.sub(r"^\+\+\+ .*?$", f"+++ b/{path}", body, flags=re.MULTILINE)
            blocks.append(f"diff --git a/{path} b/{path}\n{body}")
        return "\n".join(blocks) if blocks else raw

    def diff(self, revision: str) -> str:
        revision = self.resolve(revision)
        raw = self._invoke(
            "patch", f"cs:{revision}@{self.repository_spec}", cwd=self.active_root, timeout=600
        )
        return self._normalize_patch(raw)[:200000]

    def read_file(self, revision: str, path: str) -> str:
        revision = self.resolve(revision)
        relative = path.replace("\\", "/").lstrip("/")
        if any(part in {"", ".", ".."} for part in relative.split("/")):
            raise RevisionError("invalid UVCS repository path")
        spec = f"serverpath:/{relative}#cs:{revision}@{self.repository_spec}"
        return self._invoke("cat", spec, "--raw", cwd=self.active_root, timeout=300)[:500000]

    def identity(self) -> dict:
        return {
            "kind": "plastic",
            "repository": self.repository_spec,
            "branch": self.branch,
            "active_workspace": self.active_workspace,
        }

    def limitations(self) -> list[str]:
        return [
            "Searches numeric changesets created on one Unity Version Control branch.",
            "Creates and deletes an owned static workspace for every repeat; the active workspace is never updated, switched, reverted, or cleaned.",
            "Xlinks, cloaked rules, permissions, and server-side retention still determine which content can be reproduced.",
            "Source correlation covers the selected repository; changes inside Xlinked repositories are not ranked separately.",
        ]
