"""Unit tests for hc.mailer — SMTP transports, auth, and header encoding."""

import configparser
import types

import hc.mailer as mailer


class FakeSMTP:
    """Records connection params; sendmail captures the serialized message."""

    instances = []
    use_ssl = False

    def __init__(self, host, port, timeout=30):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.tls_started = False
        self.logged_in = None
        self.sent = None
        FakeSMTP.instances.append(self)

    def starttls(self):
        self.tls_started = True

    def login(self, username, password):
        self.logged_in = (username, password)

    def sendmail(self, from_, recipients, msg):
        self.sent = (from_, recipients, msg)

    def quit(self):
        self.quit_called = True


class FakeSMTP_SSL(FakeSMTP):
    use_ssl = True


def make_cfg(**overrides):
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "smtp": {
            "host": "relay.example.com",
            "port": "25",
            "use_tls": "false",
            "use_starttls": "false",
            "username": "",
            "password": "",
            "from": "healthcheck@example.com",
        },
    })
    for key, value in overrides.items():
        cfg.set("smtp", key, value)
    return cfg


def _patch_smtp(monkeypatch):
    FakeSMTP.instances = []
    stub = types.SimpleNamespace(SMTP=FakeSMTP, SMTP_SSL=FakeSMTP_SSL)
    monkeypatch.setattr(mailer, "smtplib", stub)


def test_no_recipients_no_connection(monkeypatch, capsys):
    _patch_smtp(monkeypatch)
    mailer.send_email(make_cfg(), "subj", "<b>html</b>", "plain", [])
    assert FakeSMTP.instances == []


def test_plain_smtp_default(monkeypatch):
    _patch_smtp(monkeypatch)
    mailer.send_email(make_cfg(), "subj", "<b>html</b>", "plain", ["a@example.com"])
    conn = FakeSMTP.instances[0]
    assert conn.host == "relay.example.com"
    assert conn.port == 25
    assert conn.tls_started is False
    assert conn.logged_in is None
    assert conn.sent[1] == ["a@example.com"]


def test_smtps_when_use_tls(monkeypatch):
    _patch_smtp(monkeypatch)
    mailer.send_email(make_cfg(use_tls="true"), "subj", "h", "p", ["a@example.com"])
    conn = FakeSMTP.instances[0]
    assert conn.use_ssl is True
    assert conn.port == 25


def test_starttls_when_use_starttls(monkeypatch):
    _patch_smtp(monkeypatch)
    mailer.send_email(make_cfg(port="587", use_starttls="true"),
                      "subj", "h", "p", ["a@example.com"])
    conn = FakeSMTP.instances[0]
    assert conn.use_ssl is False
    assert conn.port == 587
    assert conn.tls_started is True


def test_login_when_username_set(monkeypatch):
    _patch_smtp(monkeypatch)
    cfg = make_cfg(username="bot", password="secret")
    mailer.send_email(cfg, "subj", "h", "p", ["a@example.com"])
    assert FakeSMTP.instances[0].logged_in == ("bot", "secret")


def test_subject_is_rfc2047_encoded(monkeypatch):
    _patch_smtp(monkeypatch)
    subject = "✓ OK host.example.com · 2026-08-06 — alert: Disk / at 96%"
    mailer.send_email(make_cfg(), subject, "<b>html</b>", "plain", ["a@example.com"])
    _, _, msg = FakeSMTP.instances[0].sent
    header = [l for l in msg.splitlines() if l.lower().startswith("subject:")][0]
    assert "=?utf-8?" in header
    assert "✓" not in header.split(":", 1)[1]
    # MIME structure must be intact (parts are base64-encoded by the email lib)
    assert "Content-Type: text/html" in msg
    assert "Content-Type: text/plain" in msg
    assert "MIME-Version: 1.0" in msg


def test_send_failure_reported_not_raised(monkeypatch, capsys):
    class BoomSMTP(FakeSMTP):
        def sendmail(self, *a):
            raise OSError("connection refused")

    stub = types.SimpleNamespace(SMTP=BoomSMTP, SMTP_SSL=FakeSMTP_SSL)
    monkeypatch.setattr(mailer, "smtplib", stub)
    mailer.send_email(make_cfg(), "subj", "h", "p", ["a@example.com"])
    err = capsys.readouterr().err
    assert "Email failed" in err
    assert "connection refused" in err
