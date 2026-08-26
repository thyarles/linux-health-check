"""Interpreter resolution for the installed cron entry.

Regression guard: the entry used to be hardcoded to `shutil.which("python3")
or "/usr/bin/python3"`, so installing from a private interpreter (miniconda, an
/opt standalone build) wrote a cron line pointing at a system python3 that may
not exist or may be too old — the job then failed silently every night.
"""

import configparser
import sys

import pytest

from hc.crontab import resolve_python

CONDA = "/root/miniconda3/bin/python"


def cfg(python_value=None):
    c = configparser.ConfigParser()
    c.add_section("crontab")
    if python_value is not None:
        c.set("crontab", "python", python_value)
    return c


def test_defaults_to_the_invoking_interpreter(monkeypatch):
    monkeypatch.delenv("HEALTHCHECK_PYTHON", raising=False)
    assert resolve_python(cfg()) == sys.executable


def test_config_key_overrides_the_invoking_interpreter(monkeypatch):
    monkeypatch.delenv("HEALTHCHECK_PYTHON", raising=False)
    assert resolve_python(cfg(CONDA)) == CONDA


def test_env_wins_over_config(monkeypatch):
    monkeypatch.setenv("HEALTHCHECK_PYTHON", "/opt/py/bin/python3")
    assert resolve_python(cfg(CONDA)) == "/opt/py/bin/python3"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_settings_fall_through_rather_than_pinning_an_empty_path(monkeypatch, blank):
    monkeypatch.setenv("HEALTHCHECK_PYTHON", blank)
    assert resolve_python(cfg(blank)) == sys.executable


def test_falls_back_to_path_when_there_is_no_sys_executable(monkeypatch):
    """Frozen/embedded interpreters leave sys.executable empty."""
    monkeypatch.delenv("HEALTHCHECK_PYTHON", raising=False)
    monkeypatch.setattr(sys, "executable", "")
    monkeypatch.setattr("hc.crontab.shutil.which", lambda _: "/usr/bin/python3")
    assert resolve_python(cfg()) == "/usr/bin/python3"
