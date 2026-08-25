"""The de-duplication state machine.

This is the piece that decides whether a finding interrupts the broad audience
or just appears in the report, so it carries the regression the whole change
was about: the same unfixed problem must not notify everyone every day.
"""

import datetime

import pytest

from hc.alerts import AlertDecision, evaluate, fingerprint
from hc.models import CAUTION, OK, UNHEALTHY


@pytest.fixture
def store(monkeypatch) -> dict:
    """In-memory replacement for state/alerts.json."""
    import hc.alerts
    data: dict = {}
    monkeypatch.setattr(hc.alerts, "load_state", lambda name: data.get(name))
    monkeypatch.setattr(hc.alerts, "save_state", lambda name, d: data.__setitem__(name, d))
    return data


DAY0 = datetime.datetime(2026, 1, 5, 7, 0)


def at(days: float = 0, hours: float = 0) -> datetime.datetime:
    return DAY0 + datetime.timedelta(days=days, hours=hours)


def run(cfg, alerts, when, overall=CAUTION, commit=True) -> AlertDecision:
    d = evaluate(list(alerts), overall, cfg, now=when)
    if commit:
        d.commit()
    return d


DISK = (CAUTION, "Disk /var at 91%")


# ── fingerprinting ──────────────────────────────────────────────────────────

def test_fingerprint_ignores_drifting_numbers():
    assert fingerprint("83 pending updates") == fingerprint("84 pending updates")
    assert fingerprint("Disk /var at 91%") == fingerprint("Disk /var at 92%")


def test_fingerprint_separates_genuinely_different_conditions():
    assert fingerprint("Disk /var at 91%") != fingerprint("Disk /home at 91%")


def test_fingerprint_is_case_insensitive_and_trimmed():
    assert fingerprint("  Disk Full  ") == fingerprint("disk full")


# ── the core regression ─────────────────────────────────────────────────────

def test_unchanged_problem_notifies_once_not_every_day(cfg, store):
    """The bug that started all this: everyone got CAUTION every single day."""
    notified_on = [
        day for day in range(1, 8)
        if run(cfg, [DISK], at(days=day)).notify_all
    ]
    assert notified_on == [1], (
        "an unfixed problem must interrupt the broad list once, not daily"
    )


def test_still_open_problem_gets_a_weekly_reminder(cfg, store):
    notified_on = [
        day for day in range(1, 22)
        if run(cfg, [DISK], at(days=day)).notify_all
    ]
    # Discovery on day 1, then a nudge every 168h.
    assert notified_on == [1, 8, 15], notified_on


def test_reminder_can_be_disabled(cfg, store):
    cfg.set("alerts", "remind_caution_hours", "0")
    notified_on = [
        day for day in range(1, 22)
        if run(cfg, [DISK], at(days=day)).notify_all
    ]
    assert notified_on == [1]


def test_unhealthy_is_chased_daily(cfg, store):
    bad = [(UNHEALTHY, "Disk /var at 99%")]
    notified_on = [
        day for day in range(1, 6)
        if run(cfg, bad, at(days=day), overall=UNHEALTHY).notify_all
    ]
    assert notified_on == [1, 2, 3, 4, 5]


# ── transitions ─────────────────────────────────────────────────────────────

def test_new_condition_notifies(cfg, store):
    d = run(cfg, [DISK], at(days=1))
    assert d.notify_all
    assert [m for _, m in d.new] == ["Disk /var at 91%"]
    assert d.ongoing == []


def test_second_run_reports_ongoing_without_notifying(cfg, store):
    run(cfg, [DISK], at(days=1))
    d = run(cfg, [DISK], at(days=2))
    assert not d.notify_all
    assert [m for _, m in d.ongoing] == ["Disk /var at 91%"]
    assert d.new == []


def test_escalation_from_caution_to_unhealthy_notifies_immediately(cfg, store):
    run(cfg, [DISK], at(days=1))
    d = run(cfg, [(UNHEALTHY, "Disk /var at 97%")], at(days=2), overall=UNHEALTHY)
    assert d.notify_all
    assert [m for _, m in d.escalated] == ["Disk /var at 97%"]


def test_de_escalation_does_not_notify(cfg, store):
    run(cfg, [(UNHEALTHY, "Disk /var at 97%")], at(days=1), overall=UNHEALTHY)
    d = run(cfg, [DISK], at(days=1, hours=2))
    assert not d.notify_all
    assert [m for _, m in d.ongoing] == ["Disk /var at 91%"]


def test_a_genuinely_new_condition_notifies_even_while_another_is_ongoing(cfg, store):
    run(cfg, [DISK], at(days=1))
    d = run(cfg, [DISK, (CAUTION, "3 root login(s) today")], at(days=2))
    assert d.notify_all
    assert [m for _, m in d.new] == ["3 root login(s) today"]
    assert [m for _, m in d.ongoing] == ["Disk /var at 91%"]


def test_cleared_condition_is_reported_once_then_stays_quiet(cfg, store):
    run(cfg, [DISK], at(days=1))
    cleared = run(cfg, [], at(days=2), overall=OK)
    assert cleared.resolved == ["Disk /var at 91%"]
    assert not cleared.notify_all

    quiet = run(cfg, [], at(days=3), overall=OK)
    assert quiet.resolved == [], "a resolved condition must not be re-announced daily"


def test_recurrence_after_the_forget_window_counts_as_new(cfg, store):
    run(cfg, [DISK], at(days=1))
    run(cfg, [], at(days=2), overall=OK)          # clears
    run(cfg, [], at(days=3), overall=OK)          # still clear
    run(cfg, [], at(days=4), overall=OK)          # forgotten (> 72h)
    d = run(cfg, [DISK], at(days=10))
    assert d.notify_all
    assert [m for _, m in d.new] == ["Disk /var at 91%"]


def test_drifting_number_does_not_look_like_a_new_condition(cfg, store):
    run(cfg, [(CAUTION, "83 pending updates")], at(days=1))
    d = run(cfg, [(CAUTION, "84 pending updates")], at(days=2))
    assert not d.notify_all
    assert d.new == []


def test_duplicate_conditions_within_one_run_are_collapsed(cfg, store):
    d = run(cfg, [DISK, (CAUTION, "Disk /var at 92%")], at(days=1))
    assert len(d.new) == 1


# ── notify_all_on threshold ─────────────────────────────────────────────────

def test_threshold_unhealthy_suppresses_caution_alerts(cfg, store):
    cfg.set("alerts", "notify_all_on", "unhealthy")
    assert not run(cfg, [DISK], at(days=1)).notify_all


def test_threshold_unhealthy_still_alerts_on_unhealthy(cfg, store):
    cfg.set("alerts", "notify_all_on", "unhealthy")
    d = run(cfg, [(UNHEALTHY, "RAM at 99%")], at(days=1), overall=UNHEALTHY)
    assert d.notify_all


def test_invalid_threshold_falls_back_to_caution(cfg, store):
    cfg.set("alerts", "notify_all_on", "banana")
    assert run(cfg, [DISK], at(days=1)).notify_all


# ── delivery failure ────────────────────────────────────────────────────────

def test_uncommitted_alert_is_retried_next_run(cfg, store):
    """A failed SMTP send must not mark the alert as delivered."""
    first = evaluate([DISK], CAUTION, cfg, now=at(days=1))
    assert first.notify_all
    # send fails → no commit

    second = evaluate([DISK], CAUTION, cfg, now=at(days=2))
    assert second.notify_all, "alert was swallowed by a failed delivery"
    assert [m for _, m in second.new] == ["Disk /var at 91%"]
    second.commit()

    third = evaluate([DISK], CAUTION, cfg, now=at(days=3))
    assert not third.notify_all


def test_evaluate_does_not_write_state_until_commit(cfg, store):
    evaluate([DISK], CAUTION, cfg, now=at(days=1))
    assert "alerts" not in store


# ── reporting helpers ───────────────────────────────────────────────────────

def test_summary_describes_a_quiet_run(cfg, store):
    assert run(cfg, [], at(days=1), overall=OK).summary() == "nothing to report"


def test_reason_explains_why_no_alert_was_sent(cfg, store):
    run(cfg, [DISK], at(days=1))
    d = run(cfg, [DISK], at(days=2))
    assert "unchanged" in d.reason and "no alert sent" in d.reason
