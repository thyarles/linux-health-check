"""Integration-style tests for healthcheck.py CLI entry point."""

import configparser
import json
import sys

import pytest

import healthcheck
from hc.models import OK, CAUTION, UNHEALTHY, Section
from hc.report import generate_plain, generate_text


def make_cfg(daily="ops@example.com", alert="mgr@example.com"):
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "smtp": {"host": "relay", "port": "25", "use_tls": "false", "use_starttls": "false"},
        "email": {"daily_recipients": daily, "alert_recipients": alert},
        "thresholds": {},
        "crontab": {},
    })
    return cfg


def make_sections(cfg=None):
    s = Section("CPU Load")
    s.add("Load Average", "9.00", UNHEALTHY)
    s.alert("Load avg 9.00 exceeds 8.0")
    return [s], UNHEALTHY, ["Load avg 9.00 exceeds 8.0"]


@pytest.fixture
def stub_checks(monkeypatch):
    monkeypatch.setattr(healthcheck, "load_config", make_cfg)
    monkeypatch.setattr(healthcheck, "validate_config", lambda cfg: [])
    monkeypatch.setattr(healthcheck, "run_all_checks", make_sections)


def test_version_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["healthcheck.py", "--version"])
    healthcheck.main()
    out = capsys.readouterr().out
    assert out.strip() == "Linux Health Check v2.1.0"


def test_version_short_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["healthcheck.py", "-V"])
    healthcheck.main()
    assert "v2.1.0" in capsys.readouterr().out


def test_unknown_mode_exits(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["healthcheck.py", "frobnicate"])
    with pytest.raises(SystemExit):
        healthcheck.main()


def test_report_mode_prints_html(stub_checks, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["healthcheck.py", "report"])
    healthcheck.main()
    out = capsys.readouterr().out
    assert out.startswith("<!DOCTYPE html>")
    assert "CPU Load" in out


def test_text_mode_prints_terminal_report(stub_checks, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["healthcheck.py", "text"])
    healthcheck.main()
    out = capsys.readouterr().out
    assert "Overall Status:" in out
    assert "Load Average" in out


def test_json_mode_prints_parseable_json(stub_checks, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["healthcheck.py", "json"])
    healthcheck.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["overall"] == "unhealthy"
    assert payload["sections"][0]["title"] == "CPU Load"
    assert payload["sections"][0]["alerts"]


def test_run_mode_email_uses_plain_body_not_terminal_text(stub_checks, monkeypatch, capsys):
    """Regression test: the email body must be the compact plain report,
    not the terminal-styled text (which carries status symbols)."""
    captured = {}
    rcpts = []
    monkeypatch.setattr(healthcheck, "send_email",
                        lambda cfg, subject, html, plain, rcpt: (
                            captured.update({"subject": subject, "html": html, "plain": plain}),
                            rcpts.append(rcpt)))
    monkeypatch.setattr(sys, "argv", ["healthcheck.py", "run"])
    healthcheck.main()

    sections, overall, alerts = make_sections()
    assert captured["plain"] == generate_plain(sections, overall)
    assert captured["plain"] != generate_text(sections, overall)
    assert "✓" not in captured["plain"]
    assert ["ops@example.com"] in rcpts
    assert "Load avg 9.00 exceeds 8.0" in captured["subject"]


def test_run_mode_alerts_managers_on_unhealthy(stub_checks, monkeypatch, capsys):
    rcpts = []
    monkeypatch.setattr(healthcheck, "send_email",
                        lambda cfg, subject, html, plain, rcpt: rcpts.append(rcpt))
    monkeypatch.setattr(sys, "argv", ["healthcheck.py", "run"])
    healthcheck.main()
    # daily email first, then alert email to managers
    assert rcpts == [["ops@example.com"], ["mgr@example.com"]]


def test_run_mode_no_alert_email_when_ok(monkeypatch, capsys):
    monkeypatch.setattr(healthcheck, "load_config", make_cfg)
    monkeypatch.setattr(healthcheck, "validate_config", lambda cfg: [])

    def ok_checks(cfg):
        s = Section("Disk Usage")
        s.add("/", "50%", OK)
        return [s], OK, []

    monkeypatch.setattr(healthcheck, "run_all_checks", ok_checks)
    rcpts = []
    monkeypatch.setattr(healthcheck, "send_email",
                        lambda cfg, subject, html, plain, rcpt: rcpts.append(rcpt))
    monkeypatch.setattr(sys, "argv", ["healthcheck.py", "run"])
    healthcheck.main()
    assert rcpts == [["ops@example.com"]]   # only the daily email


def test_run_mode_no_alert_email_on_caution(monkeypatch, capsys):
    """Alert email fires only on UNHEALTHY — CAUTION gets the daily report only."""
    monkeypatch.setattr(healthcheck, "load_config", make_cfg)
    monkeypatch.setattr(healthcheck, "validate_config", lambda cfg: [])

    def caution_checks(cfg):
        s = Section("Disk Usage")
        s.add("/", "92%", CAUTION)
        return [s], CAUTION, ["Disk / at 92%"]

    monkeypatch.setattr(healthcheck, "run_all_checks", caution_checks)
    rcpts = []
    monkeypatch.setattr(healthcheck, "send_email",
                        lambda cfg, subject, html, plain, rcpt: rcpts.append(rcpt))
    monkeypatch.setattr(sys, "argv", ["healthcheck.py", "run"])
    healthcheck.main()
    assert rcpts == [["ops@example.com"]]   # managers NOT alerted on CAUTION


def test_config_warnings_printed_to_stderr(stub_checks, monkeypatch, capsys):
    monkeypatch.setattr(healthcheck, "validate_config",
                        lambda cfg: ["[email] daily_recipients is empty — no daily report email will be sent"])
    monkeypatch.setattr(sys, "argv", ["healthcheck.py", "json"])
    healthcheck.main()
    err = capsys.readouterr().err
    assert "⚠ config:" in err
    assert "daily_recipients is empty" in err


def test_run_mode_warns_when_no_daily_recipients(monkeypatch, capsys):
    monkeypatch.setattr(healthcheck, "load_config", lambda: make_cfg(daily=""))
    monkeypatch.setattr(healthcheck, "validate_config", lambda cfg: [])
    monkeypatch.setattr(healthcheck, "run_all_checks", make_sections)
    monkeypatch.setattr(healthcheck, "send_email", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["healthcheck.py", "run"])
    healthcheck.main()
    err = capsys.readouterr().err
    assert "No daily_recipients configured" in err


def test_build_subject_with_alerts():
    subject = healthcheck._build_subject(UNHEALTHY, ["Disk / at 96%", "5 zombie processes"], "web1")
    assert "UNHEALTHY" in subject
    assert "web1" in subject
    assert "Disk / at 96%" in subject
    assert "zombie" not in subject.split("—")[0] or "zombie" in subject


def test_build_subject_no_alerts():
    subject = healthcheck._build_subject(CAUTION, [], "web1")
    assert "CAUTION" in subject
    assert "web1" in subject
    assert "—" not in subject
