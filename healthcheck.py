#!/usr/bin/env python3
"""
Linux Health Check — v2.0

Usage:
  healthcheck.py [run]             Run all checks and send emails (default)
  healthcheck.py report            Print HTML report to stdout, no emails sent
  healthcheck.py text              Print formatted text report to stdout
  healthcheck.py bootstrap         Check and install required system tools
  healthcheck.py crontab [HH:MM]   Install/update crontab entry (default 07:00)

Config: healthcheck.conf in the same directory as this script.
Copy healthcheck.conf.example to healthcheck.conf and edit as needed.
"""

import datetime
import socket
import sys

from hc.models    import OK, _worse
from hc.utils     import load_config, REPORT_DIR, freeze_state
from hc.alerts    import evaluate
from hc.checks    import (
    check_system_info, check_cpu, check_memory, check_disk,
    check_processes, check_services, check_docker, check_updates,
    check_users, check_auth_security, check_fail2ban, check_ports,
    check_crontabs, check_suid_files, check_package_changes,
    check_etc_changes, check_log_patterns, check_rootkit,
    check_network_io, check_tools,
)
from hc.report    import generate_html, generate_text, _COLORS
from hc.mailer    import send_email
from hc.bootstrap import bootstrap
from hc.crontab   import install_crontab


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run_all_checks(cfg) -> tuple:
    checks = [
        check_system_info(),
        check_cpu(cfg),
        check_memory(cfg),
        check_disk(cfg),
        check_processes(cfg),
        check_services(),
        check_docker(cfg),
        check_updates(cfg),
        check_users(),
        check_auth_security(cfg),
        check_fail2ban(cfg),
        check_ports(),
        check_crontabs(),
        check_suid_files(),
        check_package_changes(),
        check_etc_changes(),
        check_log_patterns(cfg),
        check_rootkit(),
        check_network_io(),
        check_tools(),
    ]
    overall = OK
    alerts  = []
    for sec in checks:
        overall = _worse(overall, sec.status)
        alerts.extend(sec.alert_lines)
    return checks, overall, alerts


def _build_subject(overall: str, decision, hostname: str, is_alert: bool) -> str:
    """Subject lines that mean different things to the two audiences.

    The status word only carries an [ACTION] tag when there is genuinely
    something new to act on; the heartbeat says "no new issues" outright, so
    a reader can tell from the subject alone whether it needs them.
    """
    date = datetime.date.today().isoformat()
    ov   = _COLORS.get(overall, _COLORS[OK])

    if is_alert:
        top = " | ".join(m for _, m in decision.actionable[:2])
        return f"[ACTION] {ov['label']} · {hostname} · {date} — {top}"

    # The heartbeat still has to describe what is actually in the report. It
    # can carry findings when the broad list is unset, or when notify_all_on is
    # set to "unhealthy" and the findings are only CAUTION.
    if decision.actionable:
        bits = []
        if decision.new:       bits.append(f"{len(decision.new)} new")
        if decision.escalated: bits.append(f"{len(decision.escalated)} worse")
        if decision.reminders: bits.append(f"{len(decision.reminders)} still open")
        return f"[daily] {ov['label']} · {hostname} · {date} — {', '.join(bits)}"
    if decision.ongoing:
        return (f"[daily] {ov['label']} (no new issues) · {hostname} · {date} "
                f"— {len(decision.ongoing)} known, unchanged")
    return f"[daily] {ov['label']} · {hostname} · {date} — all clear"


def plan_delivery(decision, daily: list, broad: list) -> tuple:
    """Decide who receives this run's message, and whether it is an alert.

    Two audiences asking two different questions:
      daily_recipients — "is the check still running?"      → always hears something
      alert_recipients — "is there something for me to do?" → only when there is

    An alert covers both lists in ONE message, so nobody gets the same report
    twice. With no broad list configured the run degrades to a heartbeat rather
    than dropping the findings on the floor.
    """
    if decision.notify_all and broad:
        return broad + [r for r in daily if r not in broad], True
    return list(daily), False


def _recipients(cfg, key: str) -> list:
    raw = cfg.get("email", key, fallback="")
    return [r.strip() for r in raw.split(",") if r.strip()]


def main() -> None:
    args = sys.argv[1:]
    mode = args[0].lower() if args else "run"

    if mode == "bootstrap":
        bootstrap()
        return

    if mode == "crontab":
        install_crontab(args[1] if len(args) > 1 else "")
        return

    if mode not in ("run", "report", "text"):
        print(__doc__)
        sys.exit(1)

    # Previewing a report must not consume the diff baselines (ports, packages,
    # crontabs, SUID, alert history) that the scheduled run depends on.
    if mode in ("report", "text"):
        freeze_state(True)

    cfg      = load_config()
    hostname = socket.getfqdn()

    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] Running health checks on {hostname}...", file=sys.stderr)
    sections, overall, alerts = run_all_checks(cfg)
    print(f"  Overall status: {overall.upper()}", file=sys.stderr)

    decision = evaluate(alerts, overall, cfg)
    print(f"  Alert triage: {decision.summary()}", file=sys.stderr)

    html  = generate_html(sections, overall, decision)
    plain = generate_text(sections, overall, decision)

    if mode == "report":
        sys.stdout.write(html)
        return

    if mode == "text":
        sys.stdout.write(plain)
        sys.stdout.write("\n")
        return

    # Save report to disk
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts    = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    rpath = REPORT_DIR / f"health-{hostname}-{ts}.txt"
    rpath.write_text(plain, encoding="utf-8")
    print(f"  Report saved: {rpath}")

    daily = _recipients(cfg, "daily_recipients")
    broad = _recipients(cfg, "alert_recipients")
    attempted, is_alert = plan_delivery(decision, daily, broad)

    subject = _build_subject(overall, decision, hostname, is_alert)
    print(f"  Subject: {subject}")
    if is_alert:
        print(f"  Alerting broad list — {decision.reason}")
    elif decision.notify_all and not broad:
        print("  ! Findings need attention but no alert_recipients are "
              "configured — heartbeat only", file=sys.stderr)
    else:
        print(f"  No broad alert — {decision.reason}")

    sent = send_email(cfg, subject, html, plain, attempted)

    # Only now mark these conditions as "already notified". If the relay was
    # down, the next run will treat them as new and try again. Nothing was
    # lost when there was no one to send to, so that case commits normally.
    if sent or not attempted:
        decision.commit()
    else:
        print("  ! Delivery failed — alerts left unacknowledged, will retry next run",
              file=sys.stderr)


if __name__ == "__main__":
    main()
