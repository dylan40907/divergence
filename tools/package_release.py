"""Build a local distribution ZIP from an explicit allowlist. Never uploads anything."""

from __future__ import annotations

import argparse
import hashlib
import io
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
        "docs/ADAPTERS.md",
        "docs/DESIGN.md",
        "docs/EVALUATION.md",
        "docs/STATUS.md",
        "docs/RELEASE_CHECKLIST.md",
        "docs/RELEASING.md",
        "docs/DEPENDENCIES.md",
        "docs/assets/report.png",
    ]:
        payload[name] = (root / name).read_bytes()
    report = sample_report.read_bytes()
    if b"__DIVERGENCE_DATA__" in report or b"atob('" not in report:
        raise ValueError("sample is not a rendered report")
    payload["examples/cart-report.html"] = report
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
