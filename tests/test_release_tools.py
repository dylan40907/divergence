import pytest
from packaging.version import Version

from tools.audit_dependencies import includes


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
