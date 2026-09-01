"""Subversion revision provider using disposable checkouts."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

from divergence.revisions import RevisionError

SvnRunner = Callable[[list[str], Path, int], subprocess.CompletedProcess[str]]


def _default_runner(argv: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        env=os.environ.copy(),
    )


def _public_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        raise RevisionError(
            "Subversion URLs with embedded credentials are unsupported; use the client's credential store"
        )
    host = parsed.hostname or ""
    if parsed.port:
        host += f":{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


class SubversionProvider:
    """Search repository revisions affecting one SVN working-copy path."""

    scope = "subversion-path-revisions"

    def __init__(
        self,
        repository: Path,
        run_dir: Path,
        *,
        executable: str = "svn",
        include_externals: bool = False,
        runner: SvnRunner | None = None,
    ):
        self.repository = repository.resolve()
        self.run_dir = run_dir.resolve()
        self.executable = executable
        self.include_externals = include_externals
        self.runner = runner or _default_runner
        self.checkouts = (self.run_dir / "svn-checkouts").resolve()
        info = self._info(str(self.repository))
        self.url = _public_url(info["url"])
        self.repository_root = _public_url(info["repository_root"])
        self.repository_uuid = info["repository_uuid"]
        self.active_root = Path(info["wc_root"]).resolve()
        if not self.repository.is_relative_to(self.active_root):
            raise RevisionError("the requested path is not inside the discovered SVN working copy")
        if self.run_dir.is_relative_to(self.active_root) or self.active_root.is_relative_to(
            self.run_dir
        ):
            raise RevisionError(
                "Subversion run output must be outside the active working-copy root"
            )
        self.repository_path = self.url.removeprefix(self.repository_root).strip("/")

    def _invoke(
        self,
        *args: str,
        cwd: Path | None = None,
        timeout: int = 300,
    ) -> str:
        argv = [self.executable, *args, "--non-interactive"]
        try:
            result = self.runner(argv, cwd or self.repository, timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RevisionError(f"Subversion could not run: {exc}") from exc
        if result.returncode:
            message = result.stderr.strip() or result.stdout.strip() or f"svn {args[0]} failed"
            raise RevisionError(message[:2000])
        return result.stdout

    def _xml(self, *args: str, cwd: Path | None = None, timeout: int = 300):
        raw = self._invoke(*args, cwd=cwd, timeout=timeout)
        try:
            return ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            raise RevisionError("Subversion returned malformed XML output") from exc

    def _info(self, target: str, revision: str | None = None) -> dict[str, str]:
        args = ["info", "--xml"]
        if revision is not None:
            args.extend(["-r", revision])
        args.append(target)
        root = self._xml(*args)
        entry = root.find("entry")
        if entry is None:
            raise RevisionError("Subversion returned no working-copy information")
        url = entry.findtext("url") or ""
        repository = entry.find("repository")
        wc_info = entry.find("wc-info")
        repository_root = repository.findtext("root") if repository is not None else ""
        repository_uuid = repository.findtext("uuid") if repository is not None else ""
        wc_root = wc_info.findtext("wcroot-abspath") if wc_info is not None else ""
        if not url or not repository_root or not repository_uuid:
            raise RevisionError("Subversion information is missing URL or repository identity")
        if not wc_root:
            wc_root = str(self.repository)
        return {
            "revision": str(entry.get("revision") or ""),
            "url": url,
            "repository_root": repository_root,
            "repository_uuid": repository_uuid,
            "wc_root": wc_root,
        }

    def inspect(self) -> dict:
        return {
            "url": self.url,
            "repository_root": self.repository_root,
            "repository_uuid": self.repository_uuid,
            "active_working_copy_root": str(self.active_root),
            "search_path": self.repository_path or "/",
            "latest_visible_revision": self.resolve("HEAD"),
            "externals": "included" if self.include_externals else "ignored",
            "temporary_root": str(self.checkouts),
        }

    def resolve(self, ref: str) -> str:
        if ref == "HEAD":
            ref = self._info(self.url, "HEAD")["revision"]
        if not re.fullmatch(r"[1-9][0-9]*", ref):
            raise RevisionError("Subversion revisions must be positive integers or HEAD")
        info = self._info(self.url, ref)
        if not info["revision"]:
            raise RevisionError(f"Subversion revision {ref} is not visible for this path")
        return ref

    def history(self, good: str, bad: str) -> list[str]:
        first, last = self.resolve(good), self.resolve(bad)
        if int(first) >= int(last):
            raise RevisionError("good must be an older Subversion revision than bad")
        root = self._xml("log", "--xml", "--quiet", "-r", f"{first}:{last}", self.url)
        revisions = {
            str(entry.get("revision"))
            for entry in root.findall("logentry")
            if entry.get("revision") and int(first) <= int(entry.get("revision", "0")) <= int(last)
        }
        revisions.update({first, last})
        result = sorted(revisions, key=int)
        if len(result) < 2:
            raise RevisionError("Subversion interval contains fewer than two visible revisions")
        return result

    def prepare(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.checkouts.mkdir(exist_ok=True)

    def checkout(self, revision: str, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise RevisionError("invalid checkout name")
        revision = self.resolve(revision)
        target = (self.checkouts / name).resolve()
        if not target.is_relative_to(self.checkouts) or target.exists():
            raise RevisionError("checkout target already exists or escapes the run directory")
        args = ["checkout", "--quiet", "-r", revision]
        if not self.include_externals:
            args.append("--ignore-externals")
        args.extend([self.url, str(target)])
        try:
            self._invoke(*args, cwd=self.run_dir, timeout=3600)
        except Exception:
            self._remove_checkout(target)
            raise
        return target

    def _remove_checkout(self, target: Path) -> None:
        target = target.resolve()
        if target == self.checkouts or not target.is_relative_to(self.checkouts):
            raise RevisionError("refusing to remove an SVN checkout outside this run")
        if not target.exists():
            return

        def writable(function, path, _error):
            os.chmod(path, stat.S_IWRITE)
            function(path)

        shutil.rmtree(target, onerror=writable)

    def release(self, target: Path) -> None:
        self._remove_checkout(target)

    def cleanup(self) -> None:
        if not self.checkouts.exists():
            return
        for child in self.checkouts.iterdir():
            self._remove_checkout(child)

    def _log_entry(self, revision: str):
        root = self._xml(
            "log", "--xml", "--quiet", "-r", f"{revision}:{revision}", self.repository_root
        )
        entry = root.find("logentry")
        if entry is None or entry.get("revision") != revision:
            raise RevisionError(f"Subversion revision {revision} has no visible log entry")
        return entry

    def describe(self, revision: str) -> dict:
        revision = self.resolve(revision)
        entry = self._log_entry(revision)
        message = (entry.findtext("msg") or "").strip()
        return {
            "sha": revision,
            "subject": message.splitlines()[0][:200] if message else f"Revision {revision}",
            "author": entry.findtext("author") or "unknown",
            "date": entry.findtext("date") or "",
            "revision_kind": "subversion-revision",
        }

    def diff(self, revision: str) -> str:
        return self._invoke(
            "diff",
            "--git",
            "--internal-diff",
            "-c",
            self.resolve(revision),
            self.url,
            timeout=600,
        )[:200000]

    def read_file(self, revision: str, path: str) -> str:
        revision = self.resolve(revision)
        relative = path.replace("\\", "/").lstrip("/")
        if any(part in {"", ".", ".."} for part in relative.split("/")):
            raise RevisionError("invalid Subversion repository path")
        prefix = self.repository_path.rstrip("/")
        if prefix and relative.startswith(prefix + "/"):
            url = f"{self.repository_root}/{relative}"
        else:
            url = f"{self.url.rstrip('/')}/{relative}"
        return self._invoke("cat", "-r", revision, f"{url}@{revision}", timeout=300)[:500000]

    def identity(self) -> dict:
        return {
            "kind": "svn",
            "url": self.url,
            "repository_root": self.repository_root,
            "repository_uuid": self.repository_uuid,
            "include_externals": self.include_externals,
        }

    def limitations(self) -> list[str]:
        result = [
            "Searches numeric Subversion revisions affecting the selected working-copy path.",
            "Creates a new checkout for every repeat; the active working copy is never updated or cleaned.",
        ]
        if self.include_externals:
            result.append(
                "svn:externals are enabled and may reduce reproducibility if their definitions are not pinned."
            )
        else:
            result.append("svn:externals are ignored unless explicitly enabled.")
        return result
