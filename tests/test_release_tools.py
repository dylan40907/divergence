import base64
import json
import re

import pytest
from packaging.version import Version

from tools.audit_dependencies import includes
from tools.package_release import distribution_sample


def test_osv_range_boundaries_and_unordered_events():
    interval = {
        "ranges": [
            {
                "type": "ECOSYSTEM",
                "events": [
                    {"fixed": "2.0"},
                    {"introduced": "1.0"},
                    {"introduced": "3.0"},
                    {"fixed": "3.1"},
                ],
            }
        ]
    }
    for value, expected in [
        ("0.9", False),
        ("1.0", True),
        ("1.9", True),
        ("2.0", False),
        ("3.0", True),
        ("3.1", False),
    ]:
        assert includes(Version(value), interval) is expected
    interval["ranges"][0]["events"] = [{"introduced": "0"}, {"last_affected": "1.0"}]
    assert includes(Version("1.0"), interval)
    assert not includes(Version("1.0.1"), interval)
    interval["ranges"][0]["events"] = [{"introduced": "0"}, {"limit": "2.0"}]
    assert includes(Version("1.0"), interval)
    assert not includes(Version("2.0"), interval)


def test_osv_explicit_versions_and_unknown_coverage():
    assert includes(Version("1.0"), {"versions": ["1.0.0"]})
    assert not includes(Version("2.0"), {"versions": ["1.0"]})
    with pytest.raises(ValueError, match="unsupported range"):
        includes(Version("1.0"), {"ranges": [{"type": "UNKNOWN", "events": []}]})
    with pytest.raises(ValueError, match="no version coverage"):
        includes(Version("1.0"), {})
    with pytest.raises(ValueError, match="no version coverage"):
        includes(Version("1.0"), {"ranges": [{"type": "GIT", "events": []}]})
    assert not includes(
        Version("2.0"), {"versions": ["0.3.2d", "1.0"], "ranges": [{"type": "GIT", "events": []}]}
    )


def test_distribution_sample_removes_only_host_identity_and_rejects_other_paths():
    payload = {
        "run": {
            "scenario": {"name": "Fieldwork · a coupon applies only once", "adapter": "browser"},
            "history": [{"author": "Divergence Demo"}],
            "status": "FOUND",
            "identity": {"repository": "C:/Users/example/demo"},
            "limitations": [],
            "trials": {"sample": {"captures": [{"path": "trials/capture", "digest": "abc"}]}},
        },
        "traces": {"sample": {"samples": [{"state": {"total_cents": 4050}}]}},
        "integrity": {"checked_captures": 2},
    }

    def encode():
        return (
            "<script>atob('"
            + base64.b64encode(json.dumps(payload).encode()).decode()
            + "')</script>"
        ).encode()

    original = encode()
    result = distribution_sample(original)
    exported = json.loads(base64.b64decode(re.search(rb"atob\('([^']+)'\)", result).group(1)))
    assert original == encode()
    assert exported["run"]["identity"]["repository"] == "generated-cart-repository"
    assert exported["traces"] == payload["traces"]
    assert exported["run"]["trials"] == payload["run"]["trials"]
    assert exported["integrity"] == payload["integrity"]
    payload["traces"]["sample"]["diagnostic"] = "/home/private-user/evidence"
    with pytest.raises(ValueError, match="host path"):
        distribution_sample(encode())
    payload["run"]["scenario"]["name"] = "Private customer application"
    with pytest.raises(ValueError, match="synthetic cart demo"):
        distribution_sample(encode())
