"""Unit tests for hc.utils — formatting, config, state, and validation."""

import configparser
import datetime
import re

import pytest

from hc import utils


def test_fmt_bytes():
    assert utils._fmt_bytes(0) == "0 B"
    assert utils._fmt_bytes(512) == "512 B"
    assert utils._fmt_bytes(1023) == "1023 B"
    assert utils._fmt_bytes(1024) == "1 KB"
    assert utils._fmt_bytes(2048) == "2 KB"
    assert utils._fmt_bytes(1048576) == "1 MB"
    assert utils._fmt_bytes(1073741824) == "1 GB"
    assert utils._fmt_bytes(-512) == "-512 B"


def test_today_date_re_matches_syslog_style(monkeypatch):
    today = datetime.date.today()
    fake = type("FakeDate", (), {"today": staticmethod(lambda: today)})
    monkeypatch.setattr(utils.datetime, "date", fake)
    pattern = utils.today_date_re()
    # Single-digit days are space-padded in syslog: "Aug  6" (two spaces)
    padded = f"{today.strftime('%b')}  {today.day} 10:00:00 host sshd[1]: x"
    single = f"{today.strftime('%b')} {today.day} 10:00:00 host sshd[1]: x"
    assert re.search(pattern, padded)
    assert re.search(pattern, single)


def test_pkg_manager_detection(monkeypatch):
    monkeypatch.setattr(utils, "has", lambda c: c in ("dnf",))
    assert utils.pkg_manager() == "dnf"
    monkeypatch.setattr(utils, "has", lambda c: c in ("yum",))
    assert utils.pkg_manager() == "yum"
    monkeypatch.setattr(utils, "has", lambda c: c in ("apt-get",))
    assert utils.pkg_manager() == "apt-get"
    monkeypatch.setattr(utils, "has", lambda c: False)
    assert utils.pkg_manager() == ""


def test_install_cmd(monkeypatch):
    monkeypatch.setattr(utils, "pkg_manager", lambda: "dnf")
    assert utils.install_cmd("ss", "iproute", "iproute2") == "dnf install -y iproute"
    monkeypatch.setattr(utils, "pkg_manager", lambda: "yum")
    assert utils.install_cmd("ss", "iproute", "iproute2") == "yum install -y iproute"
    monkeypatch.setattr(utils, "pkg_manager", lambda: "apt-get")
    assert utils.install_cmd("ss", "iproute", "iproute2") == "apt-get install -y iproute2"
    monkeypatch.setattr(utils, "pkg_manager", lambda: "")
    cmd = utils.install_cmd("ss", "iproute", "iproute2")
    assert "yum install -y iproute" in cmd and "apt-get install -y iproute2" in cmd


def test_load_config_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "CONFIG_PATH", tmp_path / "nope.conf")
    cfg = utils.load_config()
    assert cfg.get("smtp", "host") == "relay.mpt.mp.br"
    assert cfg.getint("smtp", "port") == 25
    assert cfg.getboolean("smtp", "use_tls") is False
    assert cfg.getboolean("smtp", "use_starttls") is False
    assert cfg.getfloat("thresholds", "cpu_caution") == 80.0
    assert cfg.getfloat("thresholds", "disk_unhealthy") == 95.0
    assert cfg.get("crontab", "time") == "07:00"
    assert cfg.get("email", "daily_recipients") == ""


def test_load_config_reads_file(monkeypatch, tmp_path):
    conf = tmp_path / "healthcheck.conf"
    conf.write_text(
        "[smtp]\nhost = mx.example.com\nport = 587\nuse_starttls = true\n"
        "[thresholds]\nram_caution = 70\n[crontab]\ntime = 06:30\n"
    )
    monkeypatch.setattr(utils, "CONFIG_PATH", conf)
    cfg = utils.load_config()
    assert cfg.get("smtp", "host") == "mx.example.com"
    assert cfg.getint("smtp", "port") == 587
    assert cfg.getboolean("smtp", "use_starttls") is True
    assert cfg.getfloat("thresholds", "ram_caution") == 70.0
    assert cfg.get("crontab", "time") == "06:30"


def test_validate_config_clean():
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "smtp": {"host": "relay", "port": "25", "use_tls": "false", "use_starttls": "false"},
        "email": {"daily_recipients": "a@example.com", "alert_recipients": ""},
        "thresholds": {"cpu_caution": "80", "cpu_unhealthy": "95"},
    })
    assert utils.validate_config(cfg) == []


def test_validate_config_catches_bad_thresholds():
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "smtp": {"host": "relay", "port": "25"},
        "email": {"daily_recipients": "a@example.com"},
        "thresholds": {"cpu_caution": "150", "cpu_unhealthy": "-5", "load_caution_mult": "-1"},
    })
    warnings = utils.validate_config(cfg)
    assert any("cpu_caution" in w and "0-100" in w for w in warnings)
    assert any("cpu_unhealthy" in w for w in warnings)
    assert any("load_caution_mult" in w and "negative" in w for w in warnings)


def test_validate_config_catches_bad_port():
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "smtp": {"port": "99999"},
        "email": {"daily_recipients": "a@example.com"},
    })
    warnings = utils.validate_config(cfg)
    assert any("port" in w and "1-65535" in w for w in warnings)


def test_validate_config_catches_tls_conflict():
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "smtp": {"use_tls": "true", "use_starttls": "true"},
        "email": {"daily_recipients": "a@example.com"},
    })
    warnings = utils.validate_config(cfg)
    assert any("use_tls and use_starttls" in w for w in warnings)


def test_validate_config_catches_empty_recipients():
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "smtp": {},
        "email": {"daily_recipients": "  "},
        "thresholds": {},
    })
    warnings = utils.validate_config(cfg)
    assert any("daily_recipients is empty" in w for w in warnings)


def test_state_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "STATE_DIR", tmp_path)
    utils.save_state("ports", ["0.0.0.0:22", "0.0.0.0:80"])
    assert utils.load_state("ports") == ["0.0.0.0:22", "0.0.0.0:80"]


def test_load_state_missing_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "STATE_DIR", tmp_path)
    assert utils.load_state("does-not-exist") is None


def test_load_state_corrupt_returns_none(monkeypatch, tmp_path):
    (tmp_path / "ports.json").write_text("{not json")
    monkeypatch.setattr(utils, "STATE_DIR", tmp_path)
    assert utils.load_state("ports") is None


def test_version_single_source():
    import hc
    assert utils.VERSION == hc.__version__ == "2.1.0"
