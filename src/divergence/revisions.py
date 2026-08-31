"""Git history provider. Only the private clone is ever checked out or cleaned."""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path
from typing import Protocol


class RevisionError(RuntimeError):
    pass


def git(path: Path, *args: str, timeout: int = 60) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"})
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
    def history(self, good: str, bad: str) -> list[str]: ...
    def checkout(self, revision: str, name: str) -> Path: ...
    def describe(self, revision: str) -> dict: ...


class GitProvider:
    def __init__(self, repository: Path, run_dir: Path):
        self.repository = Path(git(repository.resolve(), "rev-parse", "--show-toplevel")).resolve()
        self.run_dir = run_dir.resolve()
        self.clone = self.run_dir / "source"
        self.checkouts = self.run_dir / "checkouts"

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
        git(self.clone, "worktree", "add", "--detach", str(target), revision)
        return target

    def release(self, target: Path) -> None:
        target = target.resolve()
        if (
            not target.is_relative_to(self.checkouts.resolve())
            or target == self.checkouts.resolve()
        ):
            raise RevisionError("refusing to remove a checkout outside this run")
        git(self.clone, "worktree", "remove", "--force", str(target))

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
