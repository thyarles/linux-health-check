"""How the report reaches the reader.

The HTML used to be sent as an attachment, so recipients saw a wall of plain
text in the body and had to download a file to get the formatted report. That
is a good way to make sure nobody reads it.
"""

import pytest

import hc.mailer
from hc.mailer import send_email


@pytest.fixture
def sent(monkeypatch, cfg):
    """Capture the assembled message instead of talking to a relay."""
    box = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=30):
            box["host"], box["port"] = host, port

        def login(self, u, p):
            box["login"] = (u, p)

        def sendmail(self, from_, to, msg):
            box["from"], box["to"], box["raw"] = from_, to, msg

        def quit(self):
            box["quit"] = True

    monkeypatch.setattr(hc.mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(hc.mailer.smtplib, "SMTP_SSL", FakeSMTP)
    return box


HTML  = "<html><body><h1>Report</h1></body></html>"
PLAIN = "Report in plain text"


def test_inline_is_the_default(sent, cfg):
    assert send_email(cfg, "S", HTML, PLAIN, ["a@example.com"])
    raw = sent["raw"]
    assert "multipart/alternative" in raw
    assert "attachment" not in raw


def test_inline_puts_plain_text_first_so_clients_prefer_the_html(sent, cfg):
    """RFC 2046: least-preferred alternative first."""
    cfg.set("email", "html_mode", "inline")
    send_email(cfg, "S", HTML, PLAIN, ["a@example.com"])
    raw = sent["raw"]
    assert raw.index("text/plain") < raw.index("text/html")


def test_attachment_mode_keeps_the_previous_behaviour(sent, cfg):
    cfg.set("email", "html_mode", "attachment")
    send_email(cfg, "S", HTML, PLAIN, ["a@example.com"])
    raw = sent["raw"]
    assert "multipart/mixed" in raw
    assert 'filename="health-report.html"' in raw


def test_both_mode_renders_inline_and_attaches(sent, cfg):
    cfg.set("email", "html_mode", "both")
    send_email(cfg, "S", HTML, PLAIN, ["a@example.com"])
    raw = sent["raw"]
    assert "multipart/alternative" in raw
    assert 'filename="health-report.html"' in raw


def test_an_unknown_mode_falls_back_to_inline(sent, cfg):
    cfg.set("email", "html_mode", "banana")
    send_email(cfg, "S", HTML, PLAIN, ["a@example.com"])
    assert "multipart/alternative" in sent["raw"]
    assert "attachment" not in sent["raw"]


def test_headers_are_set(sent, cfg):
    send_email(cfg, "Subject here", HTML, PLAIN, ["a@example.com", "b@example.com"])
    raw = sent["raw"]
    assert "Subject: Subject here" in raw
    assert "a@example.com, b@example.com" in raw
    assert sent["to"] == ["a@example.com", "b@example.com"]


def test_no_recipients_means_no_connection(sent, cfg):
    assert send_email(cfg, "S", HTML, PLAIN, []) is False
    assert "raw" not in sent


def test_a_failed_send_reports_false(monkeypatch, cfg):
    class Boom:
        def __init__(self, *a, **k):
            raise OSError("connection refused")

    monkeypatch.setattr(hc.mailer.smtplib, "SMTP", Boom)
    assert send_email(cfg, "S", HTML, PLAIN, ["a@example.com"]) is False


def test_utf8_content_survives_the_round_trip(sent, cfg):
    send_email(cfg, "S", "<p>disco rígido cheio ✓</p>", "disco rígido ✓", ["a@example.com"])
    import email
    msg = email.message_from_string(sent["raw"])
    payloads = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        raw = part.get_payload(decode=True)
        if isinstance(raw, bytes):
            payloads.append(raw.decode("utf-8"))
    assert any("rígido" in p for p in payloads)
    assert any("✓" in p for p in payloads)
