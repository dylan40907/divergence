"""Qualify Perforce bisection against a new isolated local Helix Core server."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import socket
import stat
import subprocess
import time
from pathlib import Path

from divergence.engine import Search
from divergence.models import Assertion, Policy, Scenario
from divergence.report import render_report
from divergence.revisions import PerforceProvider
from divergence.storage import atomic_json


def run(
    argv: list[str], cwd: Path, env: dict[str, str], input_text: str | None = None
) -> str:
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def app_source(index: int, bad_at: int) -> str:
    bad = index >= bad_at
    return f"""import json, os
from pathlib import Path
REVISION={index}
config=json.loads(Path(os.environ['DIVERGENCE_SCENARIO']).read_text())
out=Path(os.environ['DIVERGENCE_OUTPUT'])
samples=[]
for t in range(0, config['duration_ms']+1, config['sample_ms']):
    state={{'door':{{'collider_enabled': {bad!r}}}, 'revision': REVISION}}
    samples.append({{'t_ms':t,'state':state,'events':[],'logs':[],'metrics':{{}}}})
(out/'trace.json').write_text(json.dumps({{'schema_version':1,'seed':config['seed'],'samples':samples}}))
"""


def qualify(p4: Path, output: Path) -> dict:
    original_p4_environment = {
        key: os.environ.get(key)
        for key in ("P4PORT", "P4USER", "P4CLIENT", "P4PASSWD", "P4TICKETS")
    }
    if output.exists():
        raise ValueError("qualification output must be new")
    output.mkdir(parents=True)
    active = output / "active-ugs"
    active.mkdir()
    env = os.environ.copy()
    # A fresh Helix Core server grants its operating-system account the initial
    # superuser identity. No password, ticket, or external P4 configuration is used.
    user = os.environ.get("USERNAME") or getpass.getuser()
    password = f"Aa1-{secrets.token_urlsafe(18)}"  # throwaway server, new each run
    client = "active-ugs"
    p4d = p4.with_name("p4d.exe" if os.name == "nt" else "p4d")
    if not p4d.is_file():
        raise ValueError("p4d must be next to p4 for real-server qualification")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    address = f"127.0.0.1:{port}"
    tickets = output / "qualification.tickets"
    env.update(P4PORT=address, P4USER=user, P4CLIENT=client, P4TICKETS=str(tickets))
    server_root = output / "server-root"
    server_root.mkdir()
    server_log = (output / "p4d.log").open("w", encoding="utf-8")
    server = subprocess.Popen(
        [
            str(p4d),
            "-r",
            str(server_root),
            "-p",
            address,
            "-L",
            str(output / "server.log"),
            "-J",
            str(output / "journal"),
        ],
        cwd=output,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=server_log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    def p4run(*args: str, input_text: str | None = None, selected=client) -> str:
        return run(
            [str(p4), "-p", address, "-u", user, "-c", selected, "-d", str(active), *args],
            output,
            env,
            input_text,
        )

    try:
        for _ in range(50):
            try:
                p4run("info")
                break
            except RuntimeError as exc:
                if server.poll() is not None:
                    raise RuntimeError("local p4d exited during startup") from exc
                time.sleep(0.1)
        else:
            raise RuntimeError("local p4d did not start")
        p4run("passwd", input_text=f"{password}\n{password}\n")
        p4run("login", input_text=f"{password}\n")
        p4run(
            "depot",
            "-i",
            input_text=f"Depot:\tstream\nOwner:\t{user}\nDescription:\n\tFixture stream depot\nType:\tstream\nStreamDepth:\t1\nMap:\tstream/...\n",
        )
        p4run(
            "stream",
            "-i",
            input_text=f"Stream:\t//stream/main\nOwner:\t{user}\nName:\tmain\nParent:\tnone\nType:\tmainline\nDescription:\n\tFixture mainline\nOptions:\tallsubmit unlocked toparent fromparent mergedown\nParentView:\tinherit\nPaths:\n\tshare ...\n",
        )
        p4run(
            "client",
            "-i",
            input_text=f"Client:\t{client}\nOwner:\t{user}\nDescription:\n\tActive UGS fixture; Divergence must not modify this workspace.\nRoot:\t{active}\nOptions:\tallwrite noclobber nocompress unlocked nomodtime rmdir\nSubmitOptions:\trevertunchanged\nLineEnd:\tlocal\nStream:\t//stream/main\n",
        )
        changes = []
        bad_at = 5
        for index in range(1, 8):
            if index > 1:
                p4run("edit", str(active / "app.py"))
            (active / "app.py").write_text(app_source(index, bad_at), encoding="utf-8")
            if index == 1:
                (active / "Game.uproject").write_text('{"FileVersion": 3}\n', encoding="utf-8")
                p4run("add", str(active / "app.py"), str(active / "Game.uproject"))
            submitted = p4run("submit", "-d", f"Fixture revision {index}")
            marker = next(
                line
                for line in submitted.splitlines()
                if line.startswith("Change ") and "submitted" in line
            )
            changes.append(marker.split()[1])
        sentinel = active / "LOCAL_SENTINEL.txt"
        sentinel.write_text("active UGS workspace remains untouched", encoding="utf-8")
        scenario = Scenario(
            name="Real Perforce door collision",
            adapter="command",
            command=["{python}", "app.py"],
            duration_ms=200,
            sample_ms=100,
            repeats=2,
            assertions=[Assertion(path="door.collider_enabled", op="eq", value=False)],
            policy=Policy(mode="assertions", ignore_paths=["revision"]),
        )
        scenario_path = output / "scenario.yaml"
        import yaml

        scenario_path.write_text(
            yaml.safe_dump(scenario.model_dump(), sort_keys=False), encoding="utf-8"
        )
        os.environ.update(
            P4PORT=address, P4USER=user, P4CLIENT=client, P4TICKETS=str(tickets)
        )
        run_dir = output / "run"
        provider = PerforceProvider(active, run_dir, client=client, executable=str(p4))
        result = Search(
            active, changes[0], changes[-1], scenario, run_dir, provider=provider
        ).run()
        if result["status"] != "FOUND" or result["culprit"] != changes[bad_at - 1]:
            raise RuntimeError("real Perforce qualification did not locate the planted changelist")
        captures_before = json.loads((run_dir / "run.json").read_text())["trials"]
        resumed = Search(
            active,
            changes[0],
            changes[-1],
            scenario,
            run_dir,
            provider=PerforceProvider(
                active, run_dir, client=client, executable=str(p4)
            ),
        ).run(resume=True)
        captures_after = json.loads((run_dir / "run.json").read_text())["trials"]
        if captures_before != captures_after or resumed["culprit"] != result["culprit"]:
            raise RuntimeError("resume changed completed Perforce evidence")
        if sentinel.read_text(encoding="utf-8") != "active UGS workspace remains untouched":
            raise RuntimeError("active UGS workspace changed")
        clients = p4run("clients", "-e", "divergence_*")
        if any(line.startswith("Client divergence_") for line in clients.splitlines()):
            raise RuntimeError("temporary Perforce client was not deleted")
        report = render_report(run_dir)
        summary = {
            "status": result["status"],
            "culprit": result["culprit"],
            "expected_culprit": changes[bad_at - 1],
            "changes": changes,
            "trials": len(result["trials"]),
            "captures": sum(
                len(trial["captures"]) for trial in result["trials"].values()
            ),
            "resume_preserved_evidence": True,
            "active_workspace_preserved": True,
            "temporary_clients_removed": True,
            "p4_version": run([str(p4), "-V"], output, env).splitlines()[0],
            "p4d_version": run([str(p4d), "-V"], output, env).splitlines()[0],
            "report": str(report),
        }
        atomic_json(output / "qualification.json", summary)
        return summary
    finally:
        for key, value in original_p4_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            run([str(p4), "-p", address, "-u", user, "admin", "stop"], output, env)
        except RuntimeError:
            if server.poll() is None:
                server.terminate()
        server.wait(timeout=10)
        server_log.close()
        if tickets.exists():
            tickets.chmod(stat.S_IWRITE)
            tickets.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--p4", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args()
    print(json.dumps(qualify(options.p4.resolve(), options.output.resolve()), indent=2))
