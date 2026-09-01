from __future__ import annotations

import hashlib
import shutil

import pytest

from divergence.engine import Search
from divergence.models import Policy, Scenario
from divergence.revisions import GitProvider, RevisionError, git


def _commit_identity(repository):
    git(repository, "config", "user.name", "Dependency Test")
    git(repository, "config", "user.email", "dependency@example.invalid")


@pytest.mark.integration
def test_git_provider_materializes_recursive_submodules_without_touching_source(tmp_path):
    leaf = tmp_path / "leaf"
    leaf.mkdir()
    git(leaf, "init", "-b", "main")
    _commit_identity(leaf)
    (leaf / "value.txt").write_text("recorded dependency", encoding="utf-8")
    git(leaf, "add", "value.txt")
    git(leaf, "commit", "-m", "Add dependency")

    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    _commit_identity(repository)
    (repository / "main.txt").write_text("parent", encoding="utf-8")
    git(repository, "add", "main.txt")
    git(repository, "commit", "-m", "Parent")
    git(
        repository,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(leaf),
        "Dependencies/Leaf",
        timeout=300,
    )
    git(repository, "commit", "-am", "Record submodule")
    revision = git(repository, "rev-parse", "HEAD")
    active_dependency = repository / "Dependencies" / "Leaf" / "value.txt"
    active_dependency.write_text("uncommitted active edit", encoding="utf-8")

    provider = GitProvider(repository, tmp_path / "run")
    provider.prepare()
    checkout = provider.checkout(revision, "submodules")
    assert (checkout / "Dependencies" / "Leaf" / "value.txt").read_text() == "recorded dependency"
    assert active_dependency.read_text() == "uncommitted active edit"
    provider.release(checkout)
    assert not checkout.exists()


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("git-lfs") is None, reason="Git LFS is not installed")
def test_git_provider_materializes_lfs_from_source_object_cache(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    _commit_identity(repository)
    git(repository, "lfs", "install", "--local")
    git(repository, "lfs", "track", "*.bin")
    payload = bytes(range(256)) * 16
    (repository / "Content.bin").write_bytes(payload)
    git(repository, "add", ".gitattributes", "Content.bin")
    git(repository, "commit", "-m", "Add large content")
    revision = git(repository, "rev-parse", "HEAD")
    unrelated = b"not referenced by the tested revision"
    unrelated_oid = hashlib.sha256(unrelated).hexdigest()
    unrelated_object = (
        repository / ".git" / "lfs" / "objects" / unrelated_oid[:2] / unrelated_oid[2:4] / unrelated_oid
    )
    unrelated_object.parent.mkdir(parents=True, exist_ok=True)
    unrelated_object.write_bytes(unrelated)

    provider = GitProvider(repository, tmp_path / "run")
    provider.prepare()
    checkout = provider.checkout(revision, "lfs")
    assert (checkout / "Content.bin").read_bytes() == payload
    assert provider.identity()["materialize_lfs"] is True
    clone_unrelated = (
        provider._git_common_dir(provider.clone)
        / "lfs"
        / "objects"
        / unrelated_oid[:2]
        / unrelated_oid[2:4]
        / unrelated_oid
    )
    assert not clone_unrelated.exists()
    provider.release(checkout)


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("git-lfs") is None, reason="Git LFS is not installed")
def test_git_provider_rejects_missing_lfs_objects_instead_of_running_pointer(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    _commit_identity(repository)
    git(repository, "lfs", "install", "--local")
    (repository / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    (repository / "missing.bin").write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
        "size 100\n"
    )
    git(
        repository,
        "-c",
        "filter.lfs.clean=",
        "-c",
        "filter.lfs.required=false",
        "add",
        ".gitattributes",
        "missing.bin",
    )
    git(repository, "commit", "-m", "Record unavailable pointer")
    revision = git(repository, "rev-parse", "HEAD")

    provider = GitProvider(repository, tmp_path / "run", fetch_lfs=False)
    provider.prepare()
    with pytest.raises(RevisionError, match="content is unavailable"):
        provider.checkout(revision, "missing")
    assert not (tmp_path / "run" / "checkouts" / "missing").exists()


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("git-lfs") is None, reason="Git LFS is not installed")
def test_search_executes_materialized_lfs_content_through_recorded_submodule(tmp_path):
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    git(dependency, "init", "-b", "main")
    _commit_identity(dependency)
    (dependency / "helper.py").write_text(
        "import json\nfrom pathlib import Path\n\n"
        "def door_open():\n    return json.loads(Path('Content.bin').read_bytes())['open']\n",
        encoding="utf-8",
    )
    git(dependency, "add", "helper.py")
    git(dependency, "commit", "-m", "Add dependency API")

    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    _commit_identity(repository)
    git(repository, "lfs", "install", "--local")
    git(repository, "lfs", "track", "*.bin")
    git(
        repository,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(dependency),
        "Dependencies/Runtime",
        timeout=300,
    )
    (repository / "app.py").write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, 'Dependencies/Runtime')\nfrom helper import door_open\n"
        "cfg=json.loads(Path(os.environ['DIVERGENCE_SCENARIO']).read_text())\n"
        "out=Path(os.environ['DIVERGENCE_OUTPUT'])\n"
        "samples=[{'t_ms':t,'state':{'door':{'open':door_open()}}} "
        "for t in range(0,cfg['duration_ms']+1,cfg['sample_ms'])]\n"
        "(out/'trace.json').write_text(json.dumps({'seed':cfg['seed'],'samples':samples}))\n",
        encoding="utf-8",
    )
    commits = []
    for index, is_open in enumerate((True, True, False, False)):
        (repository / "Content.bin").write_bytes(
            ("{\"open\":" + str(is_open).lower() + ",\"revision\":" + str(index) + "}").encode()
        )
        git(repository, "add", ".gitattributes", ".gitmodules", "Dependencies/Runtime", "app.py", "Content.bin")
        git(repository, "commit", "-m", f"Revision {index}")
        commits.append(git(repository, "rev-parse", "HEAD"))

    scenario = Scenario(
        name="Git dependencies",
        command=["{python}", "app.py"],
        duration_ms=100,
        sample_ms=100,
        assertions=[{"path": "door.open", "value": True}],
        policy=Policy(mode="assertions"),
    )
    output = tmp_path / "run"
    result = Search(repository, commits[0], commits[-1], scenario, output).run()
    assert result["status"] == "FOUND"
    assert result["culprit"] == commits[2]
    assert result["identity"]["revision_provider"]["materialize_lfs"] is True
    assert result["identity"]["revision_provider"]["materialize_submodules"] is True
    assert not list((output / "checkouts").iterdir())
