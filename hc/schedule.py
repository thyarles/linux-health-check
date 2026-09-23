"""Spread the scheduled run over a window instead of firing on the hour.

Every host in a fleet installs the same cron time, so at 07:00 every one of
them starts reading /proc, walking /etc, shelling out to df, dnf and kubectl —
on servers that are frequently also running their backup window. The health
check then reports the CPU spike it caused itself, and the alert lands on a
list of people for whom 07:00 CPU is entirely expected.

So the cron entry still fires on the hour and the RUN sleeps a random slice of
the window before touching anything. Same idea as systemd's RandomizedDelaySec.
Two consequences worth knowing:

  * `crontab -l` keeps showing one fixed, readable time — the schedule itself
    never drifts, only this one run does.
  * the delay is re-rolled every night, so a host that lands at 07:05 today is
    not the 07:05 host tomorrow. Spreading a fleet does not need coordination
    between the hosts; independent uniform draws are enough.

Only the unattended run delays. `healthcheck.py run` typed by a human is
immediate — see the --scheduled flag in healthcheck.py.
"""

import datetime
import random
import re
import sys
import time

DEFAULT_WINDOW = "4h"

_UNITS = {"h": 3600, "m": 60, "s": 1}
_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*([hms])", re.I)


def parse_window(value: str) -> int:
    """Window written as 4h, 90m, 2h30m or a bare number of HOURS, in seconds.

    A bare number means hours because that is the unit the schedule is
    discussed in ("run some time in the four hours after 07:00"). Raises
    ValueError on anything it cannot read, so the caller decides what a typo
    in the config should cost.
    """
    text = str(value).strip().lower()
    if not text:
        raise ValueError("empty window")

    try:
        return int(round(float(text) * 3600))
    except ValueError:
        pass

    matches = list(_TOKEN.finditer(text))
    # Reject '4h junk' and '4x': every character must belong to a token.
    if not matches or "".join(m.group(0) for m in matches).replace(" ", "") != text.replace(" ", ""):
        raise ValueError(f"cannot read window {value!r} — use 4h, 90m or 2h30m")

    seconds = sum(float(m.group(1)) * _UNITS[m.group(2)] for m in matches)
    return int(round(seconds))


def fmt_duration(seconds: int) -> str:
    """0 -> '0m', 9420 -> '2h37m'. Minutes, because that is the granularity."""
    minutes = int(seconds) // 60
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def window_seconds(cfg) -> int:
    """The configured window, or the default when the config cannot be read.

    A malformed value deliberately falls back to DEFAULT_WINDOW rather than to
    no delay: `random = true` says the operator wants the fleet spread out, and
    a typo in the window is a poor reason to send every host back to hitting
    07:00 together. The warning goes to the cron log either way.
    """
    raw = str(cfg.get("crontab", "random_window", fallback=DEFAULT_WINDOW)).strip()
    try:
        return parse_window(raw)
    except ValueError as exc:
        print(f"  ! [crontab] random_window: {exc} — falling back to {DEFAULT_WINDOW}",
              file=sys.stderr)
        return parse_window(DEFAULT_WINDOW)


def enabled(cfg) -> bool:
    try:
        return bool(cfg.getboolean("crontab", "random", fallback=True))
    except ValueError:
        print("  ! [crontab] random is not a boolean — treating it as true",
              file=sys.stderr)
        return True


def _draw(window: int, rng: "random.Random | None" = None) -> int:
    """A uniform delay in [0, window], rounded to a whole minute.

    Whole minutes keep the log line ("Random delay 2h37m — checks start at
    09:37") honest against the clock time it prints beside it; nothing here
    needs second precision. The upper bound is inclusive so the tail end of the
    window is reachable — capping it a minute short would leave every host
    bunched into the earlier part of the range.
    """
    if window <= 0:
        return 0
    return (rng or random).randrange(0, window // 60 + 1) * 60


def pick_delay(cfg, rng: "random.Random | None" = None) -> int:
    """Seconds this run should wait, straight from the config."""
    if not enabled(cfg):
        return 0
    return _draw(window_seconds(cfg), rng)


def apply_delay(cfg, sleep=time.sleep, now=datetime.datetime.now) -> int:
    """Announce and serve the delay. Returns the seconds actually waited.

    The announcement is printed BEFORE the sleep and flushed, so `tail -f
    /var/log/healthcheck.log` at 07:30 shows a host that is waiting rather than
    a host that is broken. That matters: without it, a four-hour silence
    between the cron fire and the first check looks exactly like a hang.
    """
    stamp = f"[{now():%Y-%m-%d %H:%M:%S}]"
    if not enabled(cfg):
        return 0

    # Read once: window_seconds() warns about a malformed value, and warning
    # twice about the same typo in the same run just looks like two problems.
    window = window_seconds(cfg)
    delay  = _draw(window)
    if delay <= 0:
        print(f"{stamp} Random delay: starting now (drew 0m)", file=sys.stderr)
        return 0

    target = now() + datetime.timedelta(seconds=delay)
    print(f"{stamp} Random delay {fmt_duration(delay)} of a "
          f"{fmt_duration(window)} window — checks start at "
          f"{target:%H:%M}", file=sys.stderr)
    sys.stderr.flush()
    sleep(delay)
    return delay
