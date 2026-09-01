"""Install a built wheel in a fresh core-only environment and exercise its public CLI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def smoke(wheel: Path, output: Path) -> dict:
    if output.exists():
        raise ValueError("installed smoke requires a new output directory")
    output.mkdir(parents=True)
    environment = output / "environment"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, timeout=120)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # Explicitly clear import overrides: this check must use the installed package.
    child_env = {
        key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    steps = []

    def run(label, args, expected=0):
        result = subprocess.run(
            [str(python), *map(str, args)],
            cwd=output,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        (output / f"{label}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode != expected:
            raise RuntimeError(
                f"{label}: exit {result.returncode}; see {output / (label + '.log')}"
            )
        steps.append({"step": label, "exit_code": result.returncode})
        print(f"PASS {label}", flush=True)
        return result.stdout

    run(
        "installer",
        ["-m", "pip", "install", "--upgrade", "--only-binary=:all:", "pip>=26.2", "setuptools>=83"],
    )
    run("install", ["-m", "pip", "install", "--only-binary=:all:", wheel])
    run("dependency-check", ["-m", "pip", "check"])
    details = json.loads(
        run(
            "imports",
            [
                "-c",
                "import divergence,divergence.plastic_provider,divergence.svn_provider,importlib.util,json,platform; print(json.dumps({'python':platform.python_version(),'platform':platform.platform(),'module':divergence.__file__,'version':divergence.__version__,'revision_providers':['git','perforce','plastic','svn'],'optional_absent':all(importlib.util.find_spec(n) is None for n in ('playwright','pygame','sklearn'))}))",
            ],
        )
    )
    if (
        not Path(details["module"]).resolve().is_relative_to(environment)
        or not details["optional_absent"]
    ):
        raise RuntimeError("smoke environment is not an isolated core-only wheel installation")
    prefix = ["-m", "divergence"]
    run("doctor", [*prefix, "doctor"])
    run("provider-help", [*prefix, "bisect", "--help"])
    provider_help = (output / "provider-help.log").read_text(encoding="utf-8")
    if not all(flag in provider_help for flag in ("--git-lfs", "--git-submodules", "--cm-executable", "--svn-executable")):
        raise RuntimeError("installed CLI is missing version-control provider options")
    run(
        "demo", [*prefix, "demo", "--output", "fixture", "--commits", "8", "--bug-at", "6", "--run"]
    )
    run(
        "init",
        [
            *prefix,
            "init",
            "--output",
            "starter.yaml",
            "--duration-ms",
            "10000",
            "--sample-ms",
            "250",
            "--",
            "{python}",
            "app.py",
        ],
    )
    run("validate", [*prefix, "validate", "starter.yaml"])
    common = [
        *prefix,
        "verify",
        "--repo",
        "fixture/repository",
        "--scenario",
        "fixture/scenario.yaml",
    ]
    run("verify-good", [*common, "--revision", "known-good", "--output", "good"])
    before = json.loads((output / "good/run.json").read_text(encoding="utf-8"))["trials"]
    run("resume", [*common, "--revision", "known-good", "--output", "good", "--resume"])
    after = json.loads((output / "good/run.json").read_text(encoding="utf-8"))["trials"]
    if before != after:
        raise RuntimeError("unchanged installed resume altered completed trials")
    run("verify-bad", [*common, "--revision", "known-bad", "--output", "bad"], expected=3)
    run("export", [*prefix, "report", "good", "--output", "portable.html"])
    result = {**details, "steps": steps, "wheel": wheel.name, "resume_preserved_trials": True}
    (output / "installed-smoke.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    smoke(args.wheel.resolve(), args.output.resolve())
