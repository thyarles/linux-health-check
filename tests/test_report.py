"""The triage block — the part that tells a reader whether this needs them."""

from hc.alerts import AlertDecision
from hc.models import CAUTION, OK, Section, UNHEALTHY
from hc.report import generate_html, generate_plain, generate_text


def decision(new=(), escalated=(), reminders=(), ongoing=(), resolved=(), notify_all=False):
    d = AlertDecision()
    d.new, d.escalated, d.reminders = list(new), list(escalated), list(reminders)
    d.ongoing, d.resolved, d.notify_all = list(ongoing), list(resolved), notify_all
    return d


def sections():
    s = Section("Disk Usage")
    s.add("/var", "91% used", CAUTION)
    return [s]


DISK = (CAUTION, "Disk /var at 91%")


def test_html_marks_a_new_finding_as_needing_attention():
    html = generate_html(sections(), CAUTION, decision(new=[DISK], notify_all=True))
    assert "new findings" in html.lower()
    assert "Needs attention" in html
    assert "Disk /var at 91%" in html


def test_html_marks_an_unchanged_finding_as_no_action():
    html = generate_html(sections(), CAUTION, decision(ongoing=[DISK]))
    assert "Routine confirmation" in html
    assert "no action implied" in html


def test_html_says_so_when_there_is_nothing_to_report():
    html = generate_html(sections(), OK, decision())
    assert "Nothing requiring attention" in html


def test_html_lists_resolved_conditions():
    html = generate_html(sections(), OK, decision(resolved=["Disk /var at 91%"]))
    assert "Cleared since last run" in html


def test_html_escapes_alert_text():
    evil = (CAUTION, '<script>alert("x")</script>')
    html = generate_html(sections(), CAUTION, decision(new=[evil], notify_all=True))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_html_without_a_decision_still_renders():
    """report/text modes and older callers pass no decision."""
    html = generate_html(sections(), CAUTION)
    assert "CAUTION" in html          # the overall status chip
    assert "Disk Usage" in html       # the section still renders
    assert "Needs attention" not in html


def test_text_report_leads_with_the_triage_summary():
    text = generate_text(sections(), CAUTION, decision(new=[DISK], notify_all=True))
    head = text.splitlines()[:8]
    assert any("NEW FINDINGS" in line for line in head)
    assert any("Disk /var at 91%" in line for line in text.splitlines())


def test_text_report_marks_a_routine_run():
    text = generate_text(sections(), CAUTION, decision(ongoing=[DISK]))
    assert "ROUTINE CONFIRMATION" in text
    assert "no action implied" in text


def test_plain_report_includes_triage_too():
    plain = generate_plain(sections(), CAUTION, decision(new=[DISK], notify_all=True))
    assert "NEW FINDINGS" in plain


def test_every_group_appears_when_populated():
    d = decision(
        new=[(CAUTION, "a new thing")],
        escalated=[(UNHEALTHY, "a worse thing")],
        reminders=[(CAUTION, "an old thing")],
        ongoing=[(CAUTION, "a known thing")],
        resolved=["a fixed thing"],
        notify_all=True,
    )
    text = generate_text(sections(), UNHEALTHY, d)
    for phrase in ("a new thing", "a worse thing", "an old thing",
                   "a known thing", "a fixed thing"):
        assert phrase in text
