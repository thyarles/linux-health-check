"""The version must agree in every file that records it.

This exists because it did not. `pyproject.toml` and `hc/utils.py` said 2.0.3
while `uv.lock` still said 2.0.0 — the version was hand-copied into four places
and one of them was missed. `scripts/set-version.sh` moves them together now;
this test is what makes a hand-edit to any single one a red build.

No toml parser: tomllib is 3.11+ and the dev floor is 3.8, so a regex it is.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _search(relpath: str, pattern: str) -> str:
    text = (ROOT / relpath).read_text()
    m = re.search(pattern, text, re.M)
    assert m, f"no version found in {relpath} with {pattern!r}"
    return m.group(1)


def _versions() -> dict:
    return {
        "hc/utils.py":    _search("hc/utils.py", r'^VERSION\s*=\s*"([^"]+)"'),
        "pyproject.toml": _search("pyproject.toml", r'^version = "([^"]+)"'),
        # The [project] entry, not one of the locked dependencies.
        "uv.lock":        _search("uv.lock",
                                  r'^name = "linux-health-check"\nversion = "([^"]+)"'),
        "install.sh":     _search("install.sh", r'^DEFAULT_TAG="v([^"]+)"'),
    }


def test_every_file_records_the_same_version():
    seen = _versions()
    assert len(set(seen.values())) == 1, (
        "version drift — run scripts/set-version.sh to fix: %s" % seen)


def test_the_version_is_a_release_number():
    for where, v in _versions().items():
        assert re.match(r"^\d+\.\d+\.\d+$", v), f"{where} has a non-X.Y.Z version {v!r}"


def test_the_set_version_script_is_executable():
    script = ROOT / "scripts" / "set-version.sh"
    assert script.exists()
    assert script.stat().st_mode & 0o111, "scripts/set-version.sh is not executable"


@pytest.mark.parametrize("path", ["healthcheck.py", "healthcheck.conf.example"])
def test_headers_do_not_carry_a_hardcoded_version(path):
    """These two used to say 'v2.0' and were wrong for three releases. The
    report prints hc.utils.VERSION at runtime; nothing else needs a copy."""
    head = "\n".join((ROOT / path).read_text().splitlines()[:12])
    assert not re.search(r"v\d+\.\d+", head), f"{path} header pins a version again"
