"""The deployment contract: the server runs this with a bare system python3.

`healthcheck.py` and `hc/` are copied to /opt/healthcheck and executed by
whatever python3 the distro ships. There is no venv, no pip, no network. If a
runtime module ever grows a third-party import, this test fails before it can
reach a server and crash the 07:00 cron with ModuleNotFoundError.

The dev toolchain (pytest/mypy/ruff) is exempt because it never ships — the
`tests/` directory is not part of the deployment.
"""

import ast
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# Everything that gets copied to a server.
RUNTIME_FILES = sorted([REPO / "healthcheck.py"] + list((REPO / "hc").glob("*.py")))

# sys.stdlib_module_names landed in 3.10. On the dev machine that is what we
# check against; the list is a superset of what older pythons ship, so an
# import that is stdlib here could still be missing on RHEL 7 — hence the
# explicit floor list below for anything version-sensitive.
STDLIB: "frozenset[str]" = getattr(sys, "stdlib_module_names", frozenset())

# `hc` is this project's own package, shipped alongside healthcheck.py.
FIRST_PARTY = {"hc"}

# Stdlib modules that did NOT exist in Python 3.6 (the oldest target, RHEL 7).
# Importing one of these unconditionally would break those servers.
TOO_NEW_FOR_RHEL7 = {
    "dataclasses",      # 3.7
    "importlib.metadata",  # 3.8
    "zoneinfo",         # 3.9
    "graphlib",         # 3.9
    "tomllib",          # 3.11
}


def _imports(path: pathlib.Path) -> set:
    """Top-level module names imported by a file, including inside functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:          # relative import within the hc package
                continue
            if node.module:
                found.add(node.module.split(".")[0])
    return found


def test_runtime_files_are_discovered():
    """Guard the guard: if the layout changes, this test must be updated."""
    names = {p.name for p in RUNTIME_FILES}
    assert "healthcheck.py" in names
    assert {"checks.py", "alerts.py", "mailer.py", "report.py", "utils.py"} <= names


@pytest.mark.parametrize("path", RUNTIME_FILES, ids=lambda p: p.name)
def test_no_third_party_imports(path):
    assert STDLIB, "sys.stdlib_module_names unavailable — run tests on python 3.10+"
    third_party = sorted(m for m in _imports(path) if m not in STDLIB | FIRST_PARTY)
    assert not third_party, (
        f"{path.relative_to(REPO)} imports {third_party}, which the server "
        f"deployment cannot provide. Runtime code must be stdlib only."
    )


@pytest.mark.parametrize("path", RUNTIME_FILES, ids=lambda p: p.name)
def test_no_imports_too_new_for_oldest_target(path):
    too_new = sorted(_imports(path) & TOO_NEW_FOR_RHEL7)
    assert not too_new, (
        f"{path.relative_to(REPO)} imports {too_new}, which is stdlib only on "
        f"newer pythons than the RHEL 7 (3.6) targets."
    )


def test_runtime_imports_cleanly_without_the_dev_venv():
    """Import the whole package using the plain interpreter, no venv on the path.

    This is the closest thing to actually running it on a fresh server.
    """
    import subprocess
    code = (
        "import sys; sys.path.insert(0, %r);\n"
        "import healthcheck, hc.checks, hc.alerts, hc.report, hc.mailer, hc.bootstrap, hc.crontab\n"
        "print('ok')" % str(REPO)
    )
    proc = subprocess.run(
        [sys.executable, "-S", "-c", code],   # -S: skip site-packages entirely
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"stdlib-only import failed:\n{proc.stderr}"
    assert "ok" in proc.stdout


def test_makefile_deploy_list_covers_everything_the_runtime_needs():
    """The Makefile's RUNTIME list is what `make deploy` copies to a server.

    The README once said to copy `healthcheck.py` alone, which produced an
    install that could not import its own package. Keep the list honest.
    """
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    runtime_line = next(
        (l for l in makefile.splitlines() if l.startswith("RUNTIME")), ""
    )
    assert runtime_line, "Makefile has no RUNTIME definition"
    listed = set(runtime_line.split(":=", 1)[1].split())

    assert "healthcheck.py" in listed
    assert "hc" in listed, (
        "the hc/ package must be deployed — healthcheck.py cannot import itself"
    )
    assert "healthcheck.conf.example" in listed


def test_no_pip_installable_metadata_claims_dependencies():
    """pyproject declares zero runtime dependencies — that is the promise."""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "dependencies = []" in text, (
        "pyproject must declare no runtime dependencies; dev tools belong in "
        "[dependency-groups]."
    )
