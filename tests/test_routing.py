"""Who receives which message, and what the subject line tells them.

The original defect was social as much as technical: the broad list got a
CAUTION-flagged mail every day, so the word stopped meaning anything. These
tests pin down the two-audience contract.
"""

import pytest

from healthcheck import _build_subject, plan_delivery
from hc.alerts import AlertDecision
from hc.models import CAUTION, OK, UNHEALTHY

OPS   = ["ops@example.com"]
ALL   = ["team@example.com", "manager@example.com"]
HOST  = "srv01.example.com"


def decision(new=(), escalated=(), reminders=(), ongoing=(), resolved=(), notify_all=False):
    d = AlertDecision()
    d.new        = list(new)
    d.escalated  = list(escalated)
    d.reminders  = list(reminders)
    d.ongoing    = list(ongoing)
    d.resolved   = list(resolved)
    d.notify_all = notify_all
    return d


DISK = (CAUTION, "Disk /var at 91%")


# ── delivery planning ───────────────────────────────────────────────────────

def test_quiet_run_goes_only_to_the_selected_group():
    recipients, is_alert = plan_delivery(decision(), OPS, ALL)
    assert recipients == OPS
    assert not is_alert


def test_known_unchanged_problem_goes_only_to_the_selected_group():
    """The whole point: an ongoing CAUTION must not reach everyone again."""
    recipients, is_alert = plan_delivery(decision(ongoing=[DISK]), OPS, ALL)
    assert recipients == OPS
    assert not is_alert


def test_new_finding_reaches_everyone():
    recipients, is_alert = plan_delivery(
        decision(new=[DISK], notify_all=True), OPS, ALL)
    assert is_alert
    assert set(recipients) == set(ALL) | set(OPS)


def test_alert_is_a_single_message_with_no_duplicate_recipients():
    overlap = ["ops@example.com", "manager@example.com"]
    recipients, _ = plan_delivery(decision(new=[DISK], notify_all=True), overlap, ALL)
    assert len(recipients) == len(set(recipients)), "someone would get two copies"


def test_selected_group_is_never_dropped_from_an_alert():
    recipients, _ = plan_delivery(decision(new=[DISK], notify_all=True), OPS, ALL)
    assert set(OPS) <= set(recipients)


def test_findings_degrade_to_a_heartbeat_when_no_broad_list_is_configured():
    """Better the ops team sees it than nobody does."""
    recipients, is_alert = plan_delivery(decision(new=[DISK], notify_all=True), OPS, [])
    assert recipients == OPS
    assert not is_alert


def test_nothing_is_sent_when_no_recipients_are_configured():
    recipients, is_alert = plan_delivery(decision(new=[DISK], notify_all=True), [], [])
    assert recipients == []
    assert not is_alert


def test_plan_does_not_mutate_the_caller_lists():
    daily, broad = list(OPS), list(ALL)
    plan_delivery(decision(new=[DISK], notify_all=True), daily, broad)
    assert daily == OPS and broad == ALL


# ── subject lines ───────────────────────────────────────────────────────────

def test_alert_subject_is_tagged_for_action():
    subject = _build_subject(CAUTION, decision(new=[DISK], notify_all=True), HOST, True)
    assert subject.startswith("[ACTION]")
    assert "CAUTION" in subject
    assert "Disk /var at 91%" in subject


def test_heartbeat_subject_says_all_clear():
    subject = _build_subject(OK, decision(), HOST, False)
    assert subject.startswith("[daily]")
    assert "all clear" in subject


def test_heartbeat_subject_disclaims_ongoing_findings():
    """A CAUTION heartbeat must say outright that nothing is new."""
    subject = _build_subject(CAUTION, decision(ongoing=[DISK]), HOST, False)
    assert subject.startswith("[daily]")
    assert "no new issues" in subject
    assert "1 known, unchanged" in subject


def test_heartbeat_subject_still_reports_findings_it_could_not_escalate():
    """With no broad list, the heartbeat must not claim 'all clear'."""
    subject = _build_subject(CAUTION, decision(new=[DISK], notify_all=True), HOST, False)
    assert "all clear" not in subject
    assert "1 new" in subject


def test_subject_distinguishes_new_from_worsened_from_reminders():
    d = decision(new=[DISK], escalated=[(UNHEALTHY, "RAM at 99%")],
                 reminders=[(CAUTION, "8 zombies")])
    subject = _build_subject(UNHEALTHY, d, HOST, False)
    assert "1 new" in subject and "1 worse" in subject and "1 still open" in subject


@pytest.mark.parametrize("is_alert", [True, False])
def test_subject_always_identifies_the_host_and_date(is_alert):
    d = decision(new=[DISK], notify_all=True)
    assert HOST in _build_subject(CAUTION, d, HOST, is_alert)
