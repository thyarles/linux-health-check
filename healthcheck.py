#!/usr/bin/env python3
"""
Linux Health Check — v2.1.0

Usage:
  healthcheck.py [run]             Run all checks and send emails (default)
  healthcheck.py report            Print HTML report to stdout, no emails sent
  healthcheck.py text              Print formatted text report to stdout
  healthcheck.py json              Print machine-readable JSON report to stdout
  healthcheck.py bootstrap         Check and install required system tools
  healthcheck.py crontab [HH:MM]   Install/update crontab entry (default 07:00)
  healthcheck.py --version         Print version and exit

Config: healthcheck.conf in the same directory as this script.
Copy healthcheck.conf.example to healthcheck.conf and edit as needed.
"""

import datetime
import socket
import sys

from hc import __version__
from hc.models    import OK, CAUTION, UNHEALTHY, _worse
from hc.utils     import load_config, validate_config, REPORT_DIR
from hc.checks    import (
    check_system_info, check_cpu, check_memory, check_disk,
    check_processes, check_services, check_docker, check_updates,
    check_users, check_auth_security, check_fail2ban, check_ports,
    check_crontabs, check_suid_files, check_package_changes,
    check_etc_changes, check_log_patterns, check_rootkit,
    check_network_io, check_tools,
)
from hc.report    import generate_html, generate_plain, generate_text, generate_json, _COLORS
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
        check_processes(),
        check_services(),
        check_docker(),
        check_updates(),
        check_users(),
        check_auth_security(),
        check_fail2ban(),
        check_ports(),
        check_crontabs(),
        check_suid_files(),
        check_package_changes(),
        check_etc_changes(),
        check_log_patterns(),
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


def _build_subject(overall: str, alerts: list, hostname: str) -> str:
    date = datetime.date.today().isoformat()
    ov   = _COLORS.get(overall, _COLORS[OK])
    top  = " | ".join(alerts[:2]) if alerts else ""
    tag  = f"[{ov['label']}]"
    base = f"{tag} {hostname} · {date}"
    return f"{base} — {top}" if top else base


def main() -> None:
    args = sys.argv[1:]
    mode = args[0].lower() if args else "run"

    if mode in ("--version", "-v", "version"):
        print(f"Linux Health Check v{__version__}")
        return

    if mode == "bootstrap":
        bootstrap()
        return

    if mode == "crontab":
        install_crontab(args[1] if len(args) > 1 else "")
        return

    if mode not in ("run", "report", "text", "json"):
        print(__doc__)
        sys.exit(1)

    cfg      = load_config()
    hostname = socket.getfqdn()

    for warning in validate_config(cfg):
        print(f"  ⚠ config: {warning}", file=sys.stderr)

    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] Running health checks on {hostname}...", file=sys.stderr)
    sections, overall, alerts = run_all_checks(cfg)
    print(f"  Overall status: {overall.upper()}", file=sys.stderr)

    html  = generate_html(sections, overall)
    plain = generate_plain(sections, overall)

    if mode == "report":
        sys.stdout.write(html)
        return

    if mode == "text":
        sys.stdout.write(generate_text(sections, overall))
        sys.stdout.write("\n")
        return

    if mode == "json":
        sys.stdout.write(generate_json(sections, overall))
        sys.stdout.write("\n")
        return

    # Save report to disk
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts    = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    rpath = REPORT_DIR / f"health-{hostname}-{ts}.html"
    rpath.write_text(html, encoding="utf-8")
    print(f"  Report saved: {rpath}")

    subject = _build_subject(overall, alerts, hostname)
    print(f"  Subject: {subject}")

    # Daily email → operational team (always)
    daily = [r.strip() for r in cfg.get("email", "daily_recipients", fallback="").split(",") if r.strip()]
    if not daily:
        print("  ⚠ No daily_recipients configured — no daily email sent.", file=sys.stderr)
    send_email(cfg, subject, html, plain, daily)

    # Alert email → managers only when system needs immediate attention
    if overall == UNHEALTHY:
        alert_rcpt = [r.strip() for r in cfg.get("email", "alert_recipients", fallback="").split(",") if r.strip()]
        if alert_rcpt:
            send_email(cfg, subject, html, plain, alert_rcpt)


if __name__ == "__main__":
    main()
