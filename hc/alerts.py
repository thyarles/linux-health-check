"""Alert de-duplication.

The report is a snapshot; a notification is an interruption. Those are not
the same thing. A disk that has been at 91% for three weeks is a true CAUTION
in every snapshot, but it is only worth interrupting a broad audience about
once — otherwise the audience learns that CAUTION means "ignore me".

This module keeps a small state file of the conditions that have already been
notified, so the notifier can answer a different question from "what is the
status?": namely "is there anything here that these people have not already
been told?"
"""

import datetime
import re

from .models import OK, CAUTION, UNHEALTHY, _RANK, _worse
from .utils  import load_state, save_state

STATE_NAME = "alerts"
_TS_FMT    = "%Y-%m-%dT%H:%M:%S"

# Numbers inside an alert message change constantly ("83 pending updates" ->
# "84 pending updates") and must not make an ongoing condition look new.
_NUM_RE = re.compile(r"\d+")


def fingerprint(msg: str) -> str:
    """Stable identity for a condition, ignoring volatile numbers."""
    return _NUM_RE.sub("#", msg).strip().lower()


def _parse(ts: str):
    try:
        return datetime.datetime.strptime(ts, _TS_FMT)
    except (ValueError, TypeError):
        return None


def _hours_since(ts: str, now: datetime.datetime) -> float:
    t = _parse(ts)
    if t is None:
        return 1e9
    return float((now - t).total_seconds()) / 3600.0


class AlertDecision:
    """What to send, to whom, and why."""

    def __init__(self):
        self.new: list      = []   # (status, msg) never notified before
        self.escalated: list = []  # (status, msg) known, but worse than before
        self.ongoing: list  = []   # (status, msg) known and unchanged
        self.reminders: list = []  # (status, msg) ongoing past its reminder age
        self.resolved: list = []   # msg strings that cleared since last run
        self.notify_all      = False
        self.reason          = ""
        self._pending: dict  = {}

    def commit(self) -> None:
        """Record this run's conditions as notified.

        Deliberately separate from evaluate(): only call it once the message
        has actually been accepted by the relay, otherwise a failed send would
        mark a brand new alert as already-delivered and nobody would ever
        hear about it.
        """
        save_state(STATE_NAME, self._pending)

    @property
    def actionable(self) -> list:
        return self.new + self.escalated + self.reminders

    def summary(self) -> str:
        bits = []
        if self.new:       bits.append(f"{len(self.new)} new")
        if self.escalated: bits.append(f"{len(self.escalated)} worsened")
        if self.reminders: bits.append(f"{len(self.reminders)} still open")
        if self.ongoing and not bits: bits.append(f"{len(self.ongoing)} ongoing")
        if self.resolved:  bits.append(f"{len(self.resolved)} resolved")
        return ", ".join(bits) or "nothing to report"


def evaluate(alerts: list, overall: str, cfg, now=None) -> AlertDecision:
    """Compare this run's alerts against what has already been notified.

    `alerts` is a list of (status, msg) as produced by Section.alert().
    Returns an AlertDecision; also rewrites the alert state file.
    """
    now = now or datetime.datetime.now()
    d   = AlertDecision()

    threshold  = cfg.get("alerts", "notify_all_on", fallback="caution").strip().lower()
    if threshold not in (CAUTION, UNHEALTHY):
        threshold = CAUTION
    remind_c   = cfg.getfloat("alerts", "remind_caution_hours",   fallback=168.0)
    remind_u   = cfg.getfloat("alerts", "remind_unhealthy_hours", fallback=24.0)
    forget_h   = cfg.getfloat("alerts", "forget_after_hours",     fallback=72.0)

    prev: dict = load_state(STATE_NAME) or {}
    if not isinstance(prev, dict):
        prev = {}

    seen: dict = {}
    for status, msg in alerts:
        fp  = fingerprint(msg)
        old = prev.get(fp)
        # A condition that has been clear longer than the forget window is a
        # genuine recurrence, not a continuation — notify about it again.
        if old is not None and _hours_since(old.get("last_seen", ""), now) >= forget_h:
            old = None
        # Collapse duplicates within a single run (same condition, new number).
        if fp in seen:
            seen[fp]["status"] = _worse(seen[fp]["status"], status)
            continue

        if old is None:
            entry = {"status": status, "msg": msg,
                     "first_seen": now.strftime(_TS_FMT),
                     "last_seen":  now.strftime(_TS_FMT),
                     "notified_at": now.strftime(_TS_FMT),
                     "notified_status": status}
            d.new.append((status, msg))
        else:
            entry = dict(old)
            entry["status"]    = status
            entry["msg"]       = msg
            entry["last_seen"] = now.strftime(_TS_FMT)
            was = entry.get("notified_status", OK)
            age = _hours_since(entry.get("notified_at", ""), now)
            remind_after = remind_u if status == UNHEALTHY else remind_c

            if _RANK.get(status, 0) > _RANK.get(was, 0):
                d.escalated.append((status, msg))
                entry["notified_at"]     = now.strftime(_TS_FMT)
                entry["notified_status"] = status
            elif remind_after > 0 and age >= remind_after:
                d.reminders.append((status, msg))
                entry["notified_at"]     = now.strftime(_TS_FMT)
                entry["notified_status"] = status
            else:
                d.ongoing.append((status, msg))
        seen[fp] = entry

    # Conditions that were present before and are gone now.
    for fp, old in prev.items():
        if fp in seen:
            continue
        if _hours_since(old.get("last_seen", ""), now) < forget_h:
            # Keep it around briefly so a flapping condition does not re-page
            # on every cycle, but mention that it cleared exactly once.
            entry = dict(old)
            if not entry.get("resolved_reported"):
                d.resolved.append(old.get("msg", fp))
                entry["resolved_reported"] = True
            seen[fp] = entry

    d._pending = seen

    # Decide whether the broad list hears about this run.
    worth = [(st, m) for st, m in d.actionable
             if _RANK.get(st, 0) >= _RANK.get(threshold, 2)]
    if worth:
        d.notify_all = True
        parts = []
        if d.new:       parts.append(f"{len(d.new)} new condition(s)")
        if d.escalated: parts.append(f"{len(d.escalated)} worsened")
        if d.reminders: parts.append(f"{len(d.reminders)} still unresolved")
        d.reason = "; ".join(parts)
    elif d.ongoing:
        d.reason = (f"{len(d.ongoing)} known condition(s) unchanged since the "
                    f"last notification — report only, no alert sent")
    else:
        d.reason = "no conditions requiring attention"

    return d
