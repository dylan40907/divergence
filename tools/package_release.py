"""Build a local distribution ZIP from an explicit allowlist. Never uploads anything."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def distribution_sample(report: bytes) -> bytes:
    """Create a labeled copy of the synthetic cart report without the host repository path.

    This is deliberately not a general-purpose redactor for private application evidence.
    Original captures, recorded digests, and the original report are never modified.
    """
    html = report.decode("utf-8")
    marker = re.search(r"atob\('([A-Za-z0-9+/=]+)'\)", html)
    if not marker:
        raise ValueError("sample is not a rendered report")
    payload = json.loads(base64.b64decode(marker.group(1)))
    run = payload["run"]
    if (
        run["scenario"]["name"] != "Fieldwork · a coupon applies only once"
        or run["scenario"]["adapter"] != "browser"
        or any(row["author"] != "Divergence Demo" for row in run["history"])
        or run["status"] != "FOUND"
    ):
        raise ValueError(
            "distribution sample must be the qualified synthetic cart demo; review real reports separately"
        )
    run["identity"]["repository"] = "generated-cart-repository"
    run["limitations"].append(
        "Distribution sample: host repository path removed. Captured content and evidence digests are unchanged."
    )
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    if re.search(r"\b[A-Za-z]:[\\/]|/(?:Users|home|tmp|private|var|mnt|Volumes)/", serialized):
        raise ValueError("sample still contains a host path; review before distributing")
    encoded = base64.b64encode(serialized.encode()).decode()
    return (html[: marker.start(1)] + encoded + html[marker.end(1) :]).encode("utf-8")


def package(root: Path, sample_report: Path) -> Path:
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = metadata["version"]
    wheel = root / "dist" / f"divergence_hunter-{version}-py3-none-any.whl"
    source = root / "dist" / f"divergence_hunter-{version}.tar.gz"
    payload = {wheel.name: wheel.read_bytes(), source.name: source.read_bytes()}
    with zipfile.ZipFile(io.BytesIO(payload[wheel.name])) as archive:
        for path in (root / "src/divergence").rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                member = "divergence/" + path.relative_to(root / "src/divergence").as_posix()
                if archive.read(member) != path.read_bytes():
                    raise ValueError(f"wheel differs from source: {member}; rebuild it")
        if any("__pycache__" in name or name.endswith(".pyc") for name in archive.namelist()):
            raise ValueError("wheel contains compiled local cache files")
    with tarfile.open(fileobj=io.BytesIO(payload[source.name]), mode="r:gz") as archive:
        for item in archive.getmembers():
            path = PurePosixPath(item.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or any(
                    part in {".git", ".divergence", ".venv", ".tools", ".env", "__pycache__"}
                    for part in path.parts
                )
                or item.issym()
                or item.islnk()
            ):
                raise ValueError(f"unintended source archive member: {item.name}")
            checkout_file = root.joinpath(*path.parts[1:])
            if item.isfile() and checkout_file.is_file():
                stream = archive.extractfile(item)
                if stream is None or stream.read() != checkout_file.read_bytes():
                    raise ValueError(f"source archive is stale: {item.name}; rebuild it")
    for name in [
        "README.md",
        "QUICKSTART.md",
        "CHANGELOG.md",
        "LICENSE",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "requirements-dev-py312.lock",
        "requirements-dev-py311.lock",
        "docs/ADAPTERS.md",
        "docs/DESIGN.md",
        "docs/EVALUATION.md",
        "docs/STATUS.md",
        "docs/RELEASE_CHECKLIST.md",
        "docs/RELEASING.md",
        "docs/DEPENDENCIES.md",
        "docs/EXTERNAL_VALIDATION.md",
        "docs/PUBLIC_RELEASE.md",
        "docs/BACKUP.md",
        "docs/PERFORCE_UGS.md",
        "docs/VERSION_CONTROL.md",
        "docs/assets/report.png",
    ]:
        payload[name] = (root / name).read_bytes()
    payload["examples/cart-report.html"] = distribution_sample(sample_report.read_bytes())
    payload["SHA256SUMS.txt"] = "".join(
        f"{sha256(data)}  {name}\n" for name, data in sorted(payload.items())
    ).encode()
    destination = root / "dist" / f"Divergence-{version}-local.zip"
    temporary = destination.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(payload.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("release archive CRC verification failed")
            for line in archive.read("SHA256SUMS.txt").decode().splitlines():
                digest, name = line.split("  ", 1)
                if sha256(archive.read(name)) != digest:
                    raise ValueError(f"release checksum mismatch: {name}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    checksum = destination.with_suffix(".zip.sha256")
    checksum.write_text(
        f"{sha256(destination.read_bytes())}  {destination.name}\n", encoding="utf-8"
    )
    print(f"Verified {len(payload)} bundle members: {destination}")
    print(checksum.read_text(encoding="utf-8"), end="")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-report", type=Path, required=True)
    args = parser.parse_args()
    package(Path(__file__).resolve().parents[1], args.sample_report)
