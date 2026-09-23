"""The random start delay that keeps a fleet from scanning all at once.

The bug this answers: every host installed `0 7 * * *`, so at 07:00 every one
of them began a full scan — on servers that were also running backups. CPU hit
100%, and the check that caused it reported it, every morning, to people for
whom 07:00 CPU is normal.
"""

import configparser
import datetime
import random

import pytest

from hc import schedule
from hc.crontab import install_crontab


def cfg(**crontab) -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c.add_section("crontab")
    for k, v in crontab.items():
        c.set("crontab", k, v)
    return c


# ───────────────────────────────────────── window parsing

@pytest.mark.parametrize("text,seconds", [
    ("4h",     4 * 3600),
    ("90m",    90 * 60),
    ("2h30m",  2 * 3600 + 30 * 60),
    ("2h 30m", 2 * 3600 + 30 * 60),
    ("45s",    45),
    ("0",      0),
    ("  3h  ", 3 * 3600),
    ("4H",     4 * 3600),
])
def test_windows_people_actually_write(text, seconds):
    assert schedule.parse_window(text) == seconds


def test_a_bare_number_means_hours():
    """The schedule is discussed in hours ('two hours after 07:00'), so a bare
    number must not quietly mean seconds — that would be a 4-second window."""
    assert schedule.parse_window("4") == 4 * 3600
    assert schedule.parse_window("0.5") == 30 * 60


@pytest.mark.parametrize("junk", ["", "   ", "soon", "4x", "4h junk", "h"])
def test_unreadable_windows_are_rejected(junk):
    with pytest.raises(ValueError):
        schedule.parse_window(junk)


# ───────────────────────────────────────── config handling

def test_a_typo_in_the_window_falls_back_to_the_default_not_to_zero(capsys):
    """`random = true` says the operator wants the fleet spread out. A typo in
    the window is a bad reason to send every host back to hitting 07:00
    together, so the default window stands in — loudly."""
    assert schedule.window_seconds(cfg(random_window="4 hours!")) == 4 * 3600
    assert "random_window" in capsys.readouterr().err


def test_the_delay_is_on_by_default_for_a_config_that_never_heard_of_it():
    """An upgraded host whose healthcheck.conf predates the setting."""
    assert schedule.enabled(configparser.ConfigParser()) is True
    assert schedule.window_seconds(configparser.ConfigParser()) == 4 * 3600


def test_disabling_it_means_no_delay_at_all():
    assert schedule.pick_delay(cfg(random="false")) == 0


def test_a_zero_window_is_the_same_as_off():
    assert schedule.pick_delay(cfg(random="true", random_window="0")) == 0


def test_a_non_boolean_random_setting_keeps_the_spreading(capsys):
    assert schedule.enabled(cfg(random="yes please")) is True
    assert "not a boolean" in capsys.readouterr().err


# ───────────────────────────────────────── the draw

def test_every_draw_lands_inside_the_window_and_on_a_whole_minute():
    c = cfg(random="true", random_window="2h")
    for seed in range(200):
        delay = schedule.pick_delay(c, random.Random(seed))
        assert 0 <= delay <= 2 * 3600
        assert delay % 60 == 0


def test_the_whole_window_is_reachable():
    """A fleet is only spread if the draw actually uses the range — an
    off-by-one that capped it at 07:59 would leave the tail of the window
    empty and still bunch the hosts."""
    c = cfg(random="true", random_window="1h")
    drawn = {schedule.pick_delay(c, random.Random(s)) for s in range(500)}
    assert min(drawn) == 0
    assert max(drawn) == 3600


def test_two_hosts_drawing_independently_do_not_agree():
    c = cfg(random="true", random_window="4h")
    assert len({schedule.pick_delay(c) for _ in range(20)}) > 1


@pytest.mark.parametrize("seconds,text", [
    (0, "0m"), (60, "1m"), (9420, "2h37m"), (3600, "1h00m"), (4 * 3600, "4h00m"),
])
def test_durations_read_like_a_clock(seconds, text):
    assert schedule.fmt_duration(seconds) == text


# ───────────────────────────────────────── the run

def test_the_wait_is_announced_before_it_starts_not_after(capsys):
    """Four silent hours between the cron fire and the first check is
    indistinguishable from a hang. The log must say it is waiting, and say
    when it will start, before it sleeps."""
    order: list = []

    def fake_sleep(seconds):
        order.append(("slept", seconds))

    fixed = datetime.datetime(2026, 9, 21, 7, 0, 0)
    delay = schedule.apply_delay(cfg(random="true", random_window="2h"),
                                 sleep=fake_sleep, now=lambda: fixed)
    err = capsys.readouterr().err

    assert order == [("slept", delay)] or delay == 0
    if delay:
        assert schedule.fmt_duration(delay) in err
        expected = (fixed + datetime.timedelta(seconds=delay)).strftime("%H:%M")
        assert f"checks start at {expected}" in err


def test_a_disabled_delay_sleeps_for_nothing():
    calls: list = []
    waited = schedule.apply_delay(cfg(random="false"), sleep=calls.append)
    assert waited == 0
    assert calls == []


# ───────────────────────────────────────── the cron entry

def test_the_installed_entry_marks_the_run_as_scheduled(monkeypatch, capsys):
    """The delay must apply to the cron run and NOT to `healthcheck.py run`
    typed at a prompt, and the only thing that can tell them apart is the flag
    written into the entry."""
    written: dict = {}

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(*args, **kwargs):
        written["input"] = kwargs["input"]
        return Result()

    monkeypatch.setattr("hc.crontab.load_config", lambda: cfg(time="07:00"))
    monkeypatch.setattr("hc.crontab.run", lambda cmd: (0, "", ""))
    monkeypatch.setattr("hc.crontab.subprocess.run", fake_run)

    install_crontab("07:00")
    entry = next(l for l in written["input"].splitlines()
                 if "linux-healthcheck-managed" in l)
    assert " run --scheduled " in entry
    assert entry.startswith("00 07 * * * ")
    # The time in the entry is still the fixed one an operator can read.
    assert "random" not in entry
