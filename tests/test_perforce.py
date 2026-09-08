import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from divergence.cli import app
from divergence.engine import Search
from divergence.models import Assertion, Policy, Scenario, load_trace
from divergence.report import render_report
from divergence.revisions import PerforceProvider, RevisionError


class FakePerforce:
    def __init__(self, active: Path, *, unsafe_view=False, fail_sync=False, wrong_saved_root=False):
        self.active = active
        self.unsafe_view = unsafe_view
        self.fail_sync = fail_sync
        self.wrong_saved_root = wrong_saved_root
        self.clients = {}
        self.deleted = []
        self.syncs = []
        self.changes = ["100", "110", "120", "130", "140", "150", "160"]
        self.snapshots = {
            change: {
                "app.py": self.app_source(int(change) >= 140),
                "Game.uproject": "{}\n",
            }
            for change in self.changes
        }

    @staticmethod
    def app_source(bad):
        return """import json, os
from pathlib import Path
config=json.loads(Path(os.environ['DIVERGENCE_SCENARIO']).read_text())
out=Path(os.environ['DIVERGENCE_OUTPUT'])
samples=[]
for t in range(0, config['duration_ms']+1, config['sample_ms']):
    samples.append({'t_ms':t,'state':{'door':{'collider_enabled':BAD}},'events':[],'logs':[],'metrics':{}})
(out/'trace.json').write_text(json.dumps({'schema_version':1,'seed':config['seed'],'samples':samples}))
""".replace("BAD", repr(bad))

    @staticmethod
    def completed(argv, code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    def __call__(self, argv, cwd, input_text, timeout):
        json_mode = "-Mj" in argv
        client = argv[argv.index("-c") + 1] if "-c" in argv else None
        command_names = {"info", "change", "changes", "client", "sync", "revert", "describe", "print"}
        index = next(i for i, value in enumerate(argv) if value in command_names)
        args = argv[index:]

        def records(*items):
            return "".join(json.dumps({"code": "stat", **item}) + "\n" for item in items)

        if args[0] == "info":
            return self.completed(
                argv,
                stdout=records(
                    {"serverAddress": "ssl:perforce.example:1666", "userName": "tester", "clientName": "ugs_main"}
                ),
            )
        if args[:2] == ["change", "-o"]:
            change = args[2]
            status = "submitted" if change in self.changes else "pending"
            return self.completed(argv, stdout=records({"Change": change, "Status": status}))
        if args[0] == "changes":
            selected = list(self.changes)
            if "-m" in args:
                selected = [selected[-1]]
            return self.completed(
                argv,
                stdout=records(
                    *[
                        {"change": change, "user": "tester", "time": str(1700000000 + int(change)), "desc": f"Change {change}"}
                        for change in selected
                    ]
                ),
            )
        if args[0] == "client" and "-t" in args and "-o" in args:
            name = args[-1]
            mapped = "ugs_main" if self.unsafe_view else name
            form = (
                f"Client:\t{name}\nOwner:\ttester\nRoot:\t{self.active}\nHost:\ttest-host\n"
                "Description:\n\tUGS workspace\nOptions:\tnoallwrite clobber nocompress unlocked nomodtime rmdir\n"
                f"View:\n\t//depot/main/... //{mapped}/...\n"
            )
            return self.completed(argv, stdout=form)
        if args[:2] == ["client", "-i"]:
            fields = {}
            current = None
            for line in input_text.splitlines():
                if line and not line[0].isspace() and ":" in line:
                    current, value = line.split(":", 1)
                    fields[current] = [value.strip()] if value.strip() else []
                elif current and line.strip():
                    fields[current].append(line.strip())
            self.clients[fields["Client"][0]] = (input_text, Path(fields["Root"][0]))
            return self.completed(argv, stdout="Client saved.\n")
        if args[:2] == ["client", "-o"]:
            if args[2] == "ugs_main":
                return self.completed(
                    argv,
                    stdout=(
                        f"Client:\tugs_main\nOwner:\ttester\nRoot:\t{self.active}\n"
                        "Stream:\t//depot/main\nDescription:\n\tUGS workspace\n"
                    ),
                )
            form, _ = self.clients[args[2]]
            if self.wrong_saved_root:
                form = form.replace(str(self.clients[args[2]][1]), str(self.active))
            return self.completed(argv, stdout=form)
        if args[:2] == ["client", "-d"]:
            self.deleted.append(args[2])
            self.clients.pop(args[2], None)
            return self.completed(argv, stdout="Client deleted.\n")
        if args[0] == "sync":
            spec = args[-1]
            self.syncs.append((client, spec))
            if spec.endswith("#none"):
                return self.completed(argv)
            if self.fail_sync:
                root = self.clients[client][1]
                root.mkdir(parents=True, exist_ok=True)
                (root / "partial.tmp").write_text("partial")
                return self.completed(argv, code=1, stderr="simulated interrupted sync")
            change = spec.rsplit("@", 1)[1]
            root = self.clients[client][1]
            root.mkdir(parents=True, exist_ok=True)
            for name, content in self.snapshots[change].items():
                (root / name).write_text(content, encoding="utf-8")
            return self.completed(argv)
        if args[0] == "revert":
            return self.completed(argv, code=1, stderr="file(s) not opened on this client.\n")
        if args[:2] == ["describe", "-s"]:
            change = args[2]
            return self.completed(
                argv,
                stdout=records(
                    {"change": change, "user": "tester", "time": str(1700000000 + int(change)), "desc": f"Change {change}\nDetails"}
                ),
            )
        if args[:2] == ["describe", "-du"]:
            change = args[2]
            before = self.app_source(int(change) - 1 >= 140).splitlines()[0]
            after = self.app_source(int(change) >= 140).splitlines()[0]
            raw = (
                f"Change {change} by tester\n\n==== //depot/main/app.py#5 (text) ====\n"
                "--- //depot/main/app.py\n+++ //depot/main/app.py\n@@ -1,1 +1,1 @@\n"
                f"-{before}\n+{after}\n"
            )
            return self.completed(argv, stdout=raw)
        if args[0] == "print":
            spec = args[-1]
            change = spec.rsplit("@", 1)[1]
            path = spec[2:].split("@", 1)[0].removeprefix("depot/main/")
            return self.completed(argv, stdout=self.snapshots[change][path])
        raise AssertionError((json_mode, client, args, timeout))


@pytest.fixture
def p4_scenario():
    return Scenario(
        name="UGS door collision",
        adapter="command",
        command=["{python}", "app.py"],
        duration_ms=200,
        sample_ms=100,
        repeats=2,
        assertions=[Assertion(path="door.collider_enabled", op="eq", value=False)],
        policy=Policy(mode="assertions"),
    )


def test_perforce_search_uses_disposable_client_and_preserves_ugs_workspace(
    tmp_path, p4_scenario
):
    active = tmp_path / "active-ugs"
    active.mkdir()
    sentinel = active / "KEEP.txt"
    sentinel.write_text("active workspace")
    fake = FakePerforce(active)
    output = tmp_path / "run"
    provider = PerforceProvider(active, output, runner=fake)
    assert provider.inspect()["template_client"] == "ugs_main"
    assert provider.history("100", "160") == fake.changes

    result = Search(
        active,
        "100",
        "160",
        p4_scenario,
        output,
        provider=provider,
    ).run()
    assert result["status"] == "FOUND"
    assert result["culprit"] == "140"
    assert result["scope"] == "submitted-changelists"
    assert result["identity"]["revision_provider"]["kind"] == "perforce"
    assert "depot/main/app.py" in result["diff"]
    assert result["ranked_changes"][0]["path"] == "depot/main/app.py"
    assert sentinel.read_text() == "active workspace"
    assert not (output / "perforce-workspace").exists()
    assert fake.deleted == [provider.client]
    assert all(client == provider.client for client, _ in fake.syncs)
    assert all("ugs_main" not in spec for _, spec in fake.syncs)
    report = render_report(output)
    assert report.is_file()

    sync_count = len(fake.syncs)
    resumed_provider = PerforceProvider(active, output, runner=fake)
    resumed = Search(
        active,
        "100",
        "160",
        p4_scenario,
        output,
        provider=resumed_provider,
    ).run(resume=True)
    assert resumed["culprit"] == "140"
    assert len(fake.syncs) == sync_count
    assert sentinel.read_text() == "active workspace"


def test_perforce_refuses_template_view_that_targets_active_client(tmp_path):
    active = tmp_path / "active"
    active.mkdir()
    fake = FakePerforce(active, unsafe_view=True)
    provider = PerforceProvider(active, tmp_path / "run", runner=fake)
    with pytest.raises(RevisionError, match="does not map only"):
        provider.prepare()
    assert not fake.clients
    assert not fake.syncs
    assert not provider.workspace.exists()


def test_perforce_refuses_run_workspace_nested_in_active_ugs_workspace(tmp_path):
    active = tmp_path / "active"
    active.mkdir()
    with pytest.raises(RevisionError, match="must be outside"):
        PerforceProvider(active, active / ".divergence/run", runner=FakePerforce(active))

    project = active / "Project"
    project.mkdir()
    with pytest.raises(RevisionError, match="client root"):
        PerforceProvider(project, active / "other-run", runner=FakePerforce(active))


def test_perforce_partial_sync_and_failed_client_validation_are_cleaned(tmp_path):
    active = tmp_path / "active"
    active.mkdir()
    sentinel = active / "KEEP.txt"
    sentinel.write_text("preserved")

    fake = FakePerforce(active, fail_sync=True)
    provider = PerforceProvider(active, tmp_path / "run", runner=fake)
    provider.prepare()
    with pytest.raises(RevisionError, match="interrupted sync"):
        provider.checkout("100", "trial")
    provider.cleanup()
    assert not provider.workspace.exists()
    assert fake.deleted == [provider.client]
    assert sentinel.read_text() == "preserved"

    wrong = FakePerforce(active, wrong_saved_root=True)
    invalid = PerforceProvider(active, tmp_path / "wrong-run", runner=wrong)
    with pytest.raises(RevisionError, match="did not preserve"):
        invalid.prepare()
    assert wrong.deleted == [invalid.client]
    assert not invalid.workspace.exists()


def test_perforce_rejects_invalid_or_non_submitted_revisions(tmp_path):
    active = tmp_path / "active"
    active.mkdir()
    provider = PerforceProvider(active, tmp_path / "run", runner=FakePerforce(active))
    for ref in ("0", "-1", "123;sync", "HEAD~1"):
        with pytest.raises(RevisionError, match="numeric"):
            provider.resolve(ref)
    with pytest.raises(RevisionError, match="not submitted"):
        provider.resolve("999")


def test_perforce_refuses_depot_mutations_and_template_client_sync(tmp_path):
    active = tmp_path / "active"
    active.mkdir()
    fake = FakePerforce(active)
    provider = PerforceProvider(active, tmp_path / "run", runner=fake)
    calls_before = len(fake.syncs)

    for operation in (
        ("submit", "-d", "must never run"),
        ("shelve", "-c", "123"),
        ("edit", "//depot/main/app.py"),
        ("add", "//depot/main/new.py"),
        ("delete", "//depot/main/app.py"),
        ("reconcile", "//depot/main/..."),
        ("change", "-i"),
    ):
        with pytest.raises(RevisionError, match="read/isolation allowlist"):
            provider._invoke(*operation)

    with pytest.raises(RevisionError, match="read/isolation allowlist"):
        provider._invoke("sync", "//ugs_main/...@100", client=provider.template_client)
    assert len(fake.syncs) == calls_before


def test_unreal_init_and_report_bridge(tmp_path, monkeypatch):
    recipe = tmp_path / "unreal.yaml"
    result = CliRunner().invoke(
        app,
        [
            "unreal-init",
            "--project",
            "Game/Game.uproject",
            "--test",
            "Project.Functional.Door",
            "--editor",
            sys.executable,
            "--output",
            str(recipe),
        ],
    )
    assert result.exit_code == 0, result.output
    saved = yaml.safe_load(recipe.read_text())
    assert saved["policy"]["mode"] == "assertions"
    assert {item["path"] for item in saved["assertions"]} == {
        "unreal.total",
        "unreal.failed",
        "unreal.not_run",
        "unreal.in_process",
    }

    source = tmp_path / "source"
    (source / "Game").mkdir(parents=True)
    (source / "Game/Game.uproject").write_text("{}")
    output = tmp_path / "capture"
    output.mkdir()
    config = output / "scenario.json"
    config.write_text(json.dumps(saved))
    monkeypatch.chdir(source)
    monkeypatch.setenv("DIVERGENCE_OUTPUT", str(output))
    monkeypatch.setenv("DIVERGENCE_SCENARIO", str(config))

    def fake_editor(argv, cwd, timeout, log):
        report_arg = next(arg for arg in argv if arg.startswith("-ReportExportPath="))
        report = Path(report_arg.split("=", 1)[1])
        report.mkdir(exist_ok=True)
        (report / "index.json").write_text(
            json.dumps(
                {
                    "succeeded": 1,
                    "succeededWithWarnings": 0,
                    "failed": 0,
                    "notRun": 0,
                    "inProcess": 0,
                    "tests": [
                        {
                            "fullTestPath": "Project.Functional.Door",
                            "state": "Success",
                            "warnings": 0,
                        }
                    ],
                }
            )
        )
        log.write_text("automation complete")

    monkeypatch.setattr("divergence.unreal.run_command", fake_editor)
    from divergence.unreal import capture

    capture(sys.executable, "Game/Game.uproject", "Project.Functional.Door", [], False)
    trace = load_trace(output / "trace.json")
    assert trace.samples[-1].state["unreal"] == {
        "total": 1,
        "succeeded": 1,
        "succeeded_with_warnings": 0,
        "failed": 0,
        "not_run": 0,
        "in_process": 0,
    }
    assert (output / "unreal-report/index.json").is_file()


def test_unreal_init_rejects_command_injection(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "unreal-init",
            "--project",
            "Game.uproject",
            "--test",
            "Safe;Quit",
            "--output",
            str(tmp_path / "bad.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert not (tmp_path / "bad.yaml").exists()


def test_unreal_report_rejects_inconsistent_counts(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "Game.uproject").write_text("{}")
    output = tmp_path / "capture"
    output.mkdir()
    scenario = Scenario(
        name="Unreal",
        command=["ignored"],
        duration_ms=100,
        sample_ms=100,
        policy=Policy(mode="difference"),
    )
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(scenario.model_dump_json())
    monkeypatch.chdir(source)
    monkeypatch.setenv("DIVERGENCE_OUTPUT", str(output))
    monkeypatch.setenv("DIVERGENCE_SCENARIO", str(scenario_path))

    def fake_editor(argv, cwd, timeout, log):
        report = Path(next(arg for arg in argv if arg.startswith("-ReportExportPath=")).split("=", 1)[1])
        report.mkdir(exist_ok=True)
        (report / "index.json").write_text(
            json.dumps(
                {
                    "succeeded": 1,
                    "failed": 1,
                    "notRun": 0,
                    "inProcess": 0,
                    "tests": [{"fullTestPath": "Project.Bad", "state": "Fail"}],
                }
            )
        )

    monkeypatch.setattr("divergence.unreal.run_command", fake_editor)
    from divergence.unreal import capture

    with pytest.raises(ValueError, match="counts do not match"):
        capture(sys.executable, "Game.uproject", "Project.Bad", [], False)
    assert not (output / "trace.json").exists()
