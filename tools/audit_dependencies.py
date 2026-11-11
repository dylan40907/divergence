"""Match this interpreter's distributions against a local OSV PyPI ZIP. No network I/O.

Download the snapshot first: https://storage.googleapis.com/osv-vulnerabilities/PyPI/all.zip
Unknown range formats fail the audit instead of being reported as clean.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


def includes(version: Version, affected: dict) -> bool:
    for value in affected.get("versions", []):
        try:
            if version == Version(value):
                return True
        except InvalidVersion:
            # Historical Git tags may appear here; an invalid PEP 440 tag cannot
            # equal the valid distribution version being checked.
            continue
    coverage = bool(affected.get("versions"))
    matched = False
    for interval in affected.get("ranges", []):
        if interval["type"] == "GIT":
            # Distribution checks use published version lists/ecosystem ranges,
            # not an inferred mapping from a wheel to an upstream Git commit.
            continue
        if interval["type"] != "ECOSYSTEM":
            raise ValueError(f"unsupported range type: {interval['type']}")
        coverage = True
        events = interval["events"]
        limits = [event["limit"] for event in events if "limit" in event]
        if limits and not any(value == "*" or version < Version(value) for value in limits):
            continue
        changes = []
        for event in events:
            kind, value = next(iter(event.items()))
            if len(event) != 1 or kind not in {"introduced", "fixed", "last_affected", "limit"}:
                raise ValueError("unsupported range event")
            if kind != "limit":
                changes.append((Version("0") if value == "0" else Version(value), kind))
        if not any(kind == "introduced" for _, kind in changes):
            raise ValueError("range lacks introduction")
        vulnerable = False
        for value, kind in sorted(changes):
            if kind == "introduced" and version >= value:
                vulnerable = True
            elif kind == "fixed" and version >= value:
                vulnerable = False
            elif kind == "last_affected" and version > value:
                vulnerable = False
        matched |= vulnerable
    if not coverage:
        raise ValueError("advisory has no version coverage")
    return matched


def audit(snapshot: Path) -> dict:
    installed = {
        canonicalize_name(distribution.metadata["Name"]): distribution.version
        for distribution in importlib.metadata.distributions()
    }
    findings, unresolved = [], []
    total = 0
    with zipfile.ZipFile(snapshot) as archive:
        for member in archive.infolist():
            if not member.filename.endswith(".json"):
                continue
            entry = json.loads(archive.read(member))
            total += 1
            if entry.get("withdrawn"):
                continue
            for affected in entry.get("affected", []):
                package = affected.get("package", {})
                name = canonicalize_name(package.get("name", ""))
                if package.get("ecosystem") != "PyPI" or name not in installed:
                    continue
                record = {"id": entry["id"], "package": name, "version": installed[name]}
                try:
                    if includes(Version(installed[name]), affected):
                        findings.append(
                            {
                                **record,
                                "summary": entry.get("summary", ""),
                                "aliases": entry.get("aliases", []),
                                "ranges": affected.get("ranges", []),
                            }
                        )
                except (ValueError, KeyError, TypeError) as exc:
                    unresolved.append({**record, "reason": str(exc)})
    with snapshot.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "source": "https://storage.googleapis.com/osv-vulnerabilities/PyPI/all.zip",
        "snapshot_sha256": digest,
        "advisories_read": total,
        "installed": dict(sorted(installed.items())),
        "findings": findings,
        "unresolved": unresolved,
        "scope": "Local matching of Python distribution versions against OSV explicit versions and PyPI ecosystem ranges. No upstream Git commit mapping. Excludes native runtimes, future resolutions, and unknown vulnerabilities.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.snapshot)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        f"{len(result['installed'])} distributions; {len(result['findings'])} findings; "
        f"{len(result['unresolved'])} unresolved; {result['advisories_read']} advisories."
    )
    for item in result["findings"] + result["unresolved"]:
        print(json.dumps(item))
    raise SystemExit(1 if result["findings"] or result["unresolved"] else 0)
