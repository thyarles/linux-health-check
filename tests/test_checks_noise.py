"""Regression tests for the daily-CAUTION noise.

Each test here corresponds to something that used to turn the report yellow
every single day on a healthy machine. They are written as "this input must
NOT raise an alert" so that a future tightening of a threshold has to be a
deliberate act rather than an accident.
"""

import os

import pytest

from hc.checks import (
    _etc_ignore_args, _is_loopback, _port_key, _split_hostport,
    _status_age_hours, check_cpu, check_disk, check_docker,
    check_etc_changes, check_fail2ban, check_log_patterns, check_ports,
    check_processes, check_rootkit, check_services, check_system_info,
    check_tools, check_updates, check_users,
)
from hc.models import CAUTION, INFO, OK, UNHEALTHY

from .conftest import alert_messages


def _status_of(section, label: str) -> str:
    for row in section.rows:
        if row.label == label:
            return str(row.status)
    raise AssertionError(f"no row labelled {label!r} in {[r.label for r in section.rows]}")


# ─────────────────────────────────────────────────────────────────────────────
# Docker: containers stopped weeks ago are not today's problem
# ─────────────────────────────────────────────────────────────────────────────

DOCKER_CMD = "docker ps -a"


@pytest.mark.parametrize("status,expected", [
    ("Up 3 days",                       OK),
    ("Up 3 days (healthy)",             OK),
    ("Up 2 hours (unhealthy)",          CAUTION),   # docker's own healthcheck
    ("Restarting (1) 5 seconds ago",    CAUTION),   # crash loop
    ("Exited (1) 24 seconds ago",       CAUTION),   # fresh crash
    ("Exited (255) 6 weeks ago",        INFO),      # long dead, deliberate
    ("Exited (0) About an hour ago",    INFO),      # clean one-shot / k8s init
    ("Exited (0) 6 weeks ago",          INFO),
    ("Created",                         INFO),
])
def test_docker_status_classification(cfg, shell, tools, status, expected):
    tools.add("docker")
    shell.expect(DOCKER_CMD, f"c1\t{status}\timage:latest")
    section = check_docker(cfg)
    assert _status_of(section, "c1") == expected


def test_docker_old_stopped_containers_raise_no_alert(cfg, shell, tools):
    """The exact machine state that produced '19 stopped containers' daily."""
    tools.add("docker")
    shell.expect(DOCKER_CMD, "\n".join([
        "eleicoes-web-1\tExited (255) 6 weeks ago\tweb:latest",
        "eleicoes-api-1\tExited (255) 6 weeks ago\tapi:latest",
        "k8s_POD_helm-install\tExited (0) About an hour ago\tpause:3.6",
        "monitora87-db\tExited (0) 6 weeks ago\tmysql:8.0",
    ]))
    section = check_docker(cfg)
    assert alert_messages(section) == []
    assert section.status != CAUTION


def test_docker_fresh_crash_still_alerts(cfg, shell, tools):
    tools.add("docker")
    shell.expect(DOCKER_CMD, "\n".join([
        "old-stack\tExited (255) 6 weeks ago\tweb:latest",
        "kutt-server-1\tExited (1) 24 seconds ago\tkutt-server",
    ]))
    section = check_docker(cfg)
    assert len(alert_messages(section)) == 1
    assert "kutt-server-1" in alert_messages(section)[0]
    assert "old-stack" not in alert_messages(section)[0]


def test_docker_recent_window_is_configurable(cfg, shell, tools):
    tools.add("docker")
    cfg.set("thresholds", "docker_recent_hours", "1")
    shell.expect(DOCKER_CMD, "c1\tExited (1) 3 hours ago\timage")
    assert alert_messages(check_docker(cfg)) == []


@pytest.mark.parametrize("status,hours", [
    ("Exited (1) 30 seconds ago",   30 / 3600),
    ("Exited (1) 5 minutes ago",    5 / 60),
    ("Exited (1) About an hour ago", 1),
    ("Exited (1) 3 days ago",       72),
    ("Exited (1) 6 weeks ago",      6 * 168),
    # "a second"/"an hour" have no digit — the parser reads the article as 1.
    ("Exited (1) Less than a second ago", 1 / 3600),
])
def test_status_age_parsing(status, hours):
    assert _status_age_hours(status) == pytest.approx(hours, rel=0.01)


def test_status_age_of_unparseable_status_is_treated_as_ancient():
    assert _status_age_hours("Created") > 1e6


# ─────────────────────────────────────────────────────────────────────────────
# Updates: the security count was double-counted, and is not an incident
# ─────────────────────────────────────────────────────────────────────────────

def test_apt_security_count_ignores_conf_lines(cfg, shell, monkeypatch):
    """`grep -ci security` matched both the Inst and Conf line of each package,
    reporting exactly twice the real number (90 instead of 45)."""
    import hc.checks
    monkeypatch.setattr(hc.checks, "pkg_manager", lambda: "apt-get")
    shell.expect("grep -c '^Inst .*-security'", "45")
    shell.expect("grep -c '^Inst '", "82")
    section = check_updates(cfg)
    assert _status_of(section, "Security Updates") == INFO
    assert [r.value for r in section.rows if r.label == "Security Updates"] == ["45"]


def test_pending_updates_do_not_alert_by_default(cfg, shell, monkeypatch):
    import hc.checks
    monkeypatch.setattr(hc.checks, "pkg_manager", lambda: "apt-get")
    shell.expect("grep -c '^Inst .*-security'", "45")
    shell.expect("grep -c '^Inst '", "82")
    section = check_updates(cfg)
    assert alert_messages(section) == []
    assert section.status == INFO


def test_pending_updates_alert_once_a_threshold_is_configured(cfg, shell, monkeypatch):
    import hc.checks
    monkeypatch.setattr(hc.checks, "pkg_manager", lambda: "apt-get")
    cfg.set("thresholds", "updates_caution", "50")
    cfg.set("thresholds", "security_updates_caution", "10")
    shell.expect("grep -c '^Inst .*-security'", "45")
    shell.expect("grep -c '^Inst '", "82")
    section = check_updates(cfg)
    assert len(alert_messages(section)) == 2


@pytest.mark.parametrize("pm", ["dnf", "yum"])
def test_rpm_pending_updates_do_not_alert_by_default(cfg, shell, monkeypatch, pm):
    import hc.checks
    monkeypatch.setattr(hc.checks, "pkg_manager", lambda: pm)
    shell.expect(f"{pm} check-update", "\n".join(f"pkg{i}.x86_64 1.0 base" for i in range(40)))
    section = check_updates(cfg)
    assert alert_messages(section) == []
    assert _status_of(section, "Pending Updates") == INFO


@pytest.mark.parametrize("pm", ["dnf", "yum"])
def test_rpm_pending_updates_alert_once_configured(cfg, shell, monkeypatch, pm):
    import hc.checks
    monkeypatch.setattr(hc.checks, "pkg_manager", lambda: pm)
    cfg.set("thresholds", "updates_caution", "30")
    shell.expect(f"{pm} check-update", "\n".join(f"pkg{i}.x86_64 1.0 base" for i in range(40)))
    section = check_updates(cfg)
    assert alert_messages(section) == ["40 pending package updates"]


def test_updates_with_no_package_manager_is_informational(cfg, shell, monkeypatch):
    import hc.checks
    monkeypatch.setattr(hc.checks, "pkg_manager", lambda: "")
    section = check_updates(cfg)
    assert section.status == INFO
    assert alert_messages(section) == []


# ─────────────────────────────────────────────────────────────────────────────
# Root logins: `last root` returns history going back weeks
# ─────────────────────────────────────────────────────────────────────────────

def test_historic_root_logins_are_informational(shell):
    shell.expect("last -n 5 root", "\n".join([
        "root     pts/3        Thu Jul 30 16:46 - crash  (22:52)",
        "root     pts/3        Wed Jul 29 13:22 - crash (1+00:23)",
    ]))
    section = check_users()
    assert alert_messages(section) == []
    assert all(r.status != CAUTION for r in section.rows)


def test_root_login_today_alerts(shell):
    import datetime
    today = datetime.date.today()
    shell.expect("last -n 5 root", "\n".join([
        today.strftime("root     pts/8        %a %b %e 12:34   still logged in"),
        "root     pts/3        Thu Jul 30 16:46 - crash  (22:52)",
    ]))
    section = check_users()
    assert alert_messages(section) == ["1 root login(s) today"]


def test_root_login_today_matches_single_digit_days(shell, monkeypatch):
    """`last` space-pads single-digit days ('Aug  5'); strftime zero-pads."""
    import datetime

    class FixedDate(datetime.date):
        @classmethod
        def today(cls):
            return cls(2026, 8, 5)

    monkeypatch.setattr(datetime, "date", FixedDate)
    shell.expect("last -n 5 root",
                 "root     pts/8        Wed Aug  5 12:34   still logged in")
    assert alert_messages(check_users()) == ["1 root login(s) today"]


# ─────────────────────────────────────────────────────────────────────────────
# Listening ports: a service restart changes the PID, not the port
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ('0.0.0.0:22 users:(("sshd",pid=101,fd=3))',
     '0.0.0.0:22 users:(("sshd",pid=999,fd=3))'),
    ('127.0.0.1:5432 users:(("postgres",pid=7,fd=11))',
     '127.0.0.1:5432 users:(("postgres",pid=8,fd=12))'),
])
def test_port_key_ignores_pid_and_fd(a, b):
    assert _port_key(a) == _port_key(b)


def test_port_key_still_separates_different_ports():
    assert _port_key('0.0.0.0:22 users:(("sshd",pid=1,fd=3))') != \
           _port_key('0.0.0.0:23 users:(("sshd",pid=1,fd=3))')


def test_port_key_notices_a_different_process_on_the_same_port():
    assert _port_key('0.0.0.0:80 users:(("nginx",pid=1,fd=3))') != \
           _port_key('0.0.0.0:80 users:(("evil",pid=1,fd=3))')


def test_restarted_service_is_not_reported_as_a_new_port(cfg, shell, tools, no_state):
    tools.add("ss")
    no_state["ports"] = ['tcp 0.0.0.0:22 users:(("sshd"))']
    shell.expect("ss -tlnp", 'tcp 0.0.0.0:22 users:(("sshd",pid=999,fd=3))')
    section = check_ports(cfg)
    assert alert_messages(section) == []
    assert _status_of(section, "Port Changes") == OK


def test_a_genuinely_new_port_still_alerts(cfg, shell, tools, no_state):
    tools.add("ss")
    no_state["ports"] = ['tcp 0.0.0.0:22 users:(("sshd"))']
    shell.expect("ss -tlnp", "\n".join([
        'tcp 0.0.0.0:22 users:(("sshd",pid=101,fd=3))',
        'tcp 0.0.0.0:4444 users:(("nc",pid=555,fd=3))',
    ]))
    assert len(alert_messages(check_ports(cfg))) == 1


# ─────────────────────────────────────────────────────────────────────────────
# /etc: files rewritten at boot are not security events
# ─────────────────────────────────────────────────────────────────────────────

def test_files_rewritten_at_boot_are_informational(cfg, shell, monkeypatch):
    import hc.checks
    monkeypatch.setattr(hc.checks, "_boot_time", lambda: 1_700_000_000.0)
    shell.expect("find /etc", "/etc/hostname\n/etc/hosts\n/etc/timezone")
    shell.expect("stat -c '%y'", "2026-08-25 12:31:53.000")
    shell.expect("stat -c '%Y'", "1700000010")     # 10s after boot
    section = check_etc_changes(cfg)
    assert alert_messages(section) == []
    assert _status_of(section, "/etc/hostname") == INFO


def test_security_relevant_etc_change_alerts(cfg, shell, monkeypatch):
    import hc.checks
    monkeypatch.setattr(hc.checks, "_boot_time", lambda: 1_700_000_000.0)
    shell.expect("find /etc", "/etc/shadow")
    shell.expect("stat -c '%y'", "2026-08-25 14:00:00.000")
    shell.expect("stat -c '%Y'", "1700086400")     # a day after boot
    section = check_etc_changes(cfg)
    assert len(alert_messages(section)) == 1
    assert "/etc/shadow" in alert_messages(section)[0]
    assert _status_of(section, "/etc/shadow") == CAUTION


def test_many_sensitive_changes_produce_one_grouped_alert(cfg, shell, monkeypatch):
    """It used to emit one alert line per file, burying the interesting ones."""
    import hc.checks
    monkeypatch.setattr(hc.checks, "_boot_time", lambda: 0.0)
    shell.expect("find /etc", "\n".join(
        ["/etc/passwd", "/etc/shadow", "/etc/group", "/etc/sudoers", "/etc/fstab"]))
    shell.expect("stat -c '%y'", "2026-08-25 14:00:00.000")
    shell.expect("stat -c '%Y'", "1700086400")
    messages = alert_messages(check_etc_changes(cfg))
    assert len(messages) == 1
    assert "+2 more" in messages[0]


def test_ordinary_etc_churn_is_informational(cfg, shell, monkeypatch):
    import hc.checks
    monkeypatch.setattr(hc.checks, "_boot_time", lambda: 0.0)
    shell.expect("find /etc", "/etc/apt/apt.conf.d/01autoremove")
    shell.expect("stat -c '%y'", "2026-08-25 14:00:00.000")
    shell.expect("stat -c '%Y'", "1700086400")
    section = check_etc_changes(cfg)
    assert alert_messages(section) == []
    assert _status_of(section, "Security-Relevant Changes") == OK


# ─────────────────────────────────────────────────────────────────────────────
# Log analysis: scoped to today, with per-pattern escalation counts
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def logs(monkeypatch):
    """Control what the log greps return, keyed by pattern substring."""
    import hc.checks
    counts: dict = {}
    monkeypatch.setattr(hc.checks, "_log_sources", lambda: "/var/log/syslog")
    monkeypatch.setattr(hc.checks, "_find_auth_log", lambda: "/var/log/auth.log")

    def fake_count(sources, pattern):
        for key, n in counts.items():
            if key in pattern:
                return n, [f"sample {key}"] if n else []
        return 0, []

    monkeypatch.setattr(hc.checks, "count_in_log_today", fake_count)
    return counts


def test_a_single_segfault_is_not_an_incident(cfg, logs):
    logs["segfault"] = 1
    section = check_log_patterns(cfg)
    assert alert_messages(section) == []
    assert _status_of(section, "Segmentation Faults") == INFO


def test_a_pile_of_segfaults_escalates(cfg, logs):
    logs["segfault"] = 12
    section = check_log_patterns(cfg)
    assert alert_messages(section) == ["Segmentation Faults: 12 occurrence(s) today"]


def test_a_single_kernel_panic_escalates(cfg, logs):
    logs["kernel panic"] = 1
    section = check_log_patterns(cfg)
    assert _status_of(section, "Kernel Panic") == UNHEALTHY


def test_background_ssh_probing_does_not_alert(cfg, logs):
    """Any internet-facing host sees a constant level of these."""
    logs["BREAK-IN ATTEMPT"] = 20
    section = check_log_patterns(cfg)
    assert alert_messages(section) == []


def test_ssh_probing_above_the_threshold_alerts(cfg, logs):
    logs["BREAK-IN ATTEMPT"] = 500
    section = check_log_patterns(cfg)
    assert len(alert_messages(section)) == 1


def test_quiet_logs_are_all_ok(cfg, logs):
    section = check_log_patterns(cfg)
    assert section.status == OK
    assert alert_messages(section) == []


# ─────────────────────────────────────────────────────────────────────────────
# Hidden processes: the old check was a race, not a detection
# ─────────────────────────────────────────────────────────────────────────────

def test_process_that_exits_mid_check_is_not_reported_as_hidden(shell, tools):
    """A PID present in the first /proc read and gone from the second is a
    process that simply exited — not something hiding from ps.

    Uses a PID that really exists in /proc, so that the two-read comparison is
    the only thing that can reject it. With a fake PID the later existence
    re-check would mask a regression in the comparison itself.
    """
    pid = str(os.getpid())
    shell.expect("ls /proc", f"1\n2\n{pid}", once=True)   # first read
    shell.expect("ls /proc", "1\n2", once=True)           # second read: gone
    shell.expect("ps -eo pid", "1\n2")
    section = check_rootkit()
    assert alert_messages(section) == []
    assert _status_of(section, "Process Visibility") == OK


def test_process_that_starts_mid_check_is_not_reported_as_hidden(shell, tools):
    pid = str(os.getpid())
    shell.expect("ls /proc", "1\n2", once=True)
    shell.expect("ls /proc", f"1\n2\n{pid}", once=True)
    shell.expect("ps -eo pid", "1\n2")
    assert alert_messages(check_rootkit()) == []


def test_process_ps_missed_once_is_not_reported_as_hidden(shell, tools):
    """`ps` can miss a process that is being scheduled; the second run catches
    it. Only a PID absent from BOTH ps runs counts as hidden."""
    pid = str(os.getpid())
    shell.expect("ls /proc", f"1\n2\n{pid}")
    shell.expect("ps -eo pid", "1\n2", once=True)          # first run misses it
    shell.expect("ps -eo pid", f"1\n2\n{pid}", once=True)  # second run sees it
    assert alert_messages(check_rootkit()) == []


def test_a_persistently_hidden_process_is_reported(shell, tools):
    """Uses this test process's own PID so the /proc existence re-check passes."""
    pid = str(os.getpid())
    shell.expect("ls /proc", f"1\n2\n{pid}")
    shell.expect("ps -eo pid", "1\n2")
    section = check_rootkit()
    assert len(alert_messages(section)) == 1
    assert "hidden process" in alert_messages(section)[0]


def test_pid_that_vanished_before_the_recheck_is_dropped(shell, tools):
    """Stable across both /proc reads but gone by the time we verify it."""
    shell.expect("ls /proc", "1\n2\n999999")
    shell.expect("ps -eo pid", "1\n2")
    assert alert_messages(check_rootkit()) == []


# ─────────────────────────────────────────────────────────────────────────────
# Things that are permanent by nature must never be CAUTION
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_tools_are_informational_not_caution(shell, tools):
    """A tool missing yesterday is missing today for the same reason."""
    section = check_tools()          # `tools` fixture reports nothing installed
    assert section.status == INFO
    assert alert_messages(section) == []


def test_fail2ban_bans_are_the_system_working(cfg, shell, tools):
    tools.add("fail2ban-client")
    shell.expect("fail2ban-client status sshd", "Currently banned:\t14\nTotal banned:\t320")
    shell.expect("fail2ban-client status", "Jail list:\tsshd")
    section = check_fail2ban(cfg)
    assert alert_messages(section) == []
    assert _status_of(section, "Jail: sshd") == INFO


def test_fail2ban_spike_threshold_can_be_enabled(cfg, shell, tools):
    tools.add("fail2ban-client")
    cfg.set("thresholds", "banned_ips_caution", "10")
    shell.expect("fail2ban-client status sshd", "Currently banned:\t14\nTotal banned:\t320")
    shell.expect("fail2ban-client status", "Jail list:\tsshd")
    assert len(alert_messages(check_fail2ban(cfg))) == 1


@pytest.mark.parametrize("count,expected", [
    (0, OK), (1, INFO), (3, INFO), (10, CAUTION), (50, UNHEALTHY),
])
def test_zombie_thresholds(cfg, shell, count, expected):
    shell.expect('awk \'$8=="Z"\'', str(count))
    section = check_processes(cfg)
    assert _status_of(section, "Zombie Processes") == expected


# ─────────────────────────────────────────────────────────────────────────────
# Services: 'activating' needs two consecutive sightings to count as stuck
# ─────────────────────────────────────────────────────────────────────────────

def test_unit_caught_mid_startup_is_not_stuck(shell, tools, no_state):
    tools.add("systemctl")
    shell.expect("--state=activating", "wsl-pro.service loaded activating start")
    section = check_services()
    assert alert_messages(section) == []
    assert _status_of(section, "wsl-pro.service") == INFO


def test_unit_still_activating_on_the_next_run_is_stuck(shell, tools, no_state):
    tools.add("systemctl")
    no_state["activating"] = ["wsl-pro.service"]
    shell.expect("--state=activating", "wsl-pro.service loaded activating start")
    section = check_services()
    assert alert_messages(section) == ["Service wsl-pro.service stuck activating"]
    assert _status_of(section, "wsl-pro.service") == CAUTION


def test_failed_units_always_alert(shell, tools, no_state):
    tools.add("systemctl")
    shell.expect("--state=failed", "nginx.service loaded failed failed A high performance web server")
    section = check_services()
    assert alert_messages(section) == ["Service nginx.service failed"]


# ─────────────────────────────────────────────────────────────────────────────
# Density: the report has to stay readable on a big machine
# ─────────────────────────────────────────────────────────────────────────────

def test_per_core_cpu_is_summarised_not_listed(cfg, shell, tools):
    """96 rows saying '0.0% used' were 29% of the whole report."""
    tools.add("mpstat")
    cores = "\n".join(
        f"Average:  {i}  1.0  0.0  1.0  0.0  0.0  0.0  0.0  0.0  0.0  99.0"
        for i in range(96)
    )
    shell.expect("mpstat", "Average:  all  1.0  0.0  1.0  0.0  0.0  0.0  0.0  0.0  0.0  99.0\n" + cores)
    shell.expect("nproc", "96")
    shell.expect("/proc/loadavg", "1.0 1.0 1.0")
    section = check_cpu(cfg)
    labels = [r.label for r in section.rows]
    assert "CPU (all cores)" in labels
    assert "Cores" in labels
    # idle cores must not each get a row
    assert len([l for l in labels if l.startswith("CPU ") and l != "CPU (all cores)"]) == 0
    assert len(section.rows) < 10


def test_busy_cores_are_still_named(cfg, shell, tools):
    tools.add("mpstat")
    rows = ["Average:  all  1.0  0.0  1.0  0.0  0.0  0.0  0.0  0.0  0.0  50.0"]
    rows += [f"Average:  {i}  1.0  0.0  1.0  0.0  0.0  0.0  0.0  0.0  0.0  99.0" for i in range(8)]
    rows.append("Average:  9  1.0  0.0  1.0  0.0  0.0  0.0  0.0  0.0  0.0  2.0")  # 98% busy
    shell.expect("mpstat", "\n".join(rows))
    shell.expect("nproc", "10")
    shell.expect("/proc/loadavg", "1.0 1.0 1.0")
    section = check_cpu(cfg)
    assert "CPU 9" in [r.label for r in section.rows]
    assert "98.0% used" in [r.value for r in section.rows if r.label == "CPU 9"][0]


def test_disk_rows_carry_a_meter(cfg, shell, no_state):
    shell.expect("df -Pk", "/dev/sda1 1000000 910000 90000 91% /var")
    section = check_disk(cfg)
    row = [r for r in section.rows if r.label == "/var"][0]
    assert row.meter == pytest.approx(91.0)


def test_disk_delta_appears_on_the_second_run(cfg, shell, no_state):
    shell.expect("df -Pk", "/dev/sda1 1000000 880000 120000 88% /var")
    check_disk(cfg)                                    # records 88.0
    shell.rules.clear()
    shell.expect("df -Pk", "/dev/sda1 1000000 910000 90000 91% /var")
    section = check_disk(cfg)
    row = [r for r in section.rows if r.label == "/var"][0]
    assert "+3.0" in row.delta and "since last run" in row.delta


def test_first_ever_run_shows_no_delta(cfg, shell, no_state):
    shell.expect("df -Pk", "/dev/sda1 1000000 910000 90000 91% /var")
    section = check_disk(cfg)
    assert [r for r in section.rows if r.label == "/var"][0].delta == ""


def test_an_unchanged_disk_shows_no_delta_at_all(cfg, shell, no_state):
    """Silence is the message. "no change since last run" on every mount cost a
    line each and told the reader nothing."""
    shell.expect("df -Pk", "/dev/sda1 1000000 910000 90000 91% /var")
    check_disk(cfg)
    section = check_disk(cfg)
    assert [r for r in section.rows if r.label == "/var"][0].delta == ""


def test_reboot_since_last_run_is_flagged(shell, no_state):
    import hc.checks
    no_state["boot_time"] = 1_700_000_000.0
    shell.expect("who -b", "2026-08-25 12:31")
    orig = hc.checks._boot_time
    hc.checks._boot_time = lambda: 1_700_500_000.0      # a later boot
    try:
        section = check_system_info()
    finally:
        hc.checks._boot_time = orig
    assert alert_messages(section) == ["Host rebooted since the previous run"]


def test_no_reboot_means_no_alert(shell, no_state):
    import hc.checks
    no_state["boot_time"] = 1_700_000_000.0
    shell.expect("who -b", "2026-08-25 12:31")
    orig = hc.checks._boot_time
    hc.checks._boot_time = lambda: 1_700_000_000.0
    try:
        section = check_system_info()
    finally:
        hc.checks._boot_time = orig
    assert alert_messages(section) == []


def test_port_inventory_is_capped(cfg, shell, tools, no_state):
    tools.add("ss")
    no_state["ports"] = ["tcp 0.0.0.0:1 users:((\"x\"))"]
    shell.expect("ss -tlnp", "\n".join(f"tcp 0.0.0.0:{p} users:((\"svc\",pid=1,fd=3))"
                                       for p in range(1000, 1040)))
    section = check_ports(cfg)
    assert any(r.label == "…" and "more socket" in r.value for r in section.rows)


def test_uninstalled_optional_tools_do_not_get_their_own_section(cfg, shell, tools):
    """Docker/fail2ban absent used to cost a whole panel each."""
    assert check_docker(cfg).applicable is False
    assert check_fail2ban(cfg).applicable is False


# ─────────────────────────────────────────────────────────────────────────────
# Listening sockets: TCP + UDP, and loopback is not reachable from anywhere
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("addr,loopback", [
    ("127.0.0.1:6444",     True),
    ("127.0.0.53%lo:53",   True),    # systemd-resolved, with a zone id
    ("[::1]:323",          True),
    ("10.255.255.254:53",  False),
    ("0.0.0.0:22",         False),
    ("[::]:22",            False),
    ("*:8472",             False),   # a wildcard bind is the opposite of local
])
def test_loopback_classification(addr, loopback):
    assert _is_loopback(addr) is loopback


@pytest.mark.parametrize("addr,host,port", [
    ("127.0.0.1:6444",   "127.0.0.1", "6444"),
    ("[::1]:323",        "::1",       "323"),
    ("[fe80::1%eth0]:80", "fe80::1%eth0", "80"),
    ("*:8472",           "*",         "8472"),
])
def test_hostport_splitting(addr, host, port):
    assert _split_hostport(addr) == (host, port)


def test_udp_sockets_are_included(cfg, shell, tools, no_state):
    tools.add("ss")
    shell.expect("ss -tlnp", 'tcp 0.0.0.0:22 users:(("sshd",pid=1,fd=3))')
    shell.expect("ss -ulnp", 'udp 0.0.0.0:161 users:(("snmpd",pid=2,fd=4))')
    listed = " ".join(r.value for r in check_ports(cfg).rows)
    assert "0.0.0.0:161" in listed, "UDP sockets were previously invisible"
    assert "udp" in listed


def test_loopback_sockets_are_hidden_by_default(cfg, shell, tools, no_state):
    tools.add("ss")
    shell.expect("ss -tlnp", "\n".join([
        'tcp 0.0.0.0:22 users:(("sshd",pid=1,fd=3))',
        'tcp 127.0.0.1:6444 users:(("k3s",pid=2,fd=4))',
    ]))
    shell.expect("ss -ulnp", 'udp 127.0.0.53%lo:53 users:(("resolved",pid=3,fd=5))')
    section = check_ports(cfg)
    listed = " ".join(r.value for r in section.rows if r.label == "")
    assert "0.0.0.0:22" in listed
    assert "6444" not in listed
    assert "127.0.0.53" not in listed
    assert any("2 hidden" in r.value for r in section.rows)


def test_list_local_ports_flag_shows_them(cfg, shell, tools, no_state):
    cfg.set("checks", "list_local_ports", "true")
    tools.add("ss")
    shell.expect("ss -tlnp", "\n".join([
        'tcp 0.0.0.0:22 users:(("sshd",pid=1,fd=3))',
        'tcp 127.0.0.1:6444 users:(("k3s",pid=2,fd=4))',
    ]))
    listed = " ".join(r.value for r in check_ports(cfg).rows if r.label == "")
    assert "0.0.0.0:22" in listed and "6444" in listed


def test_churning_loopback_ports_do_not_raise_alerts(cfg, shell, tools, no_state):
    """Applications open random high ports on 127.0.0.1 constantly."""
    tools.add("ss")
    no_state["ports"] = ['tcp 0.0.0.0:22 users:(("sshd"))']
    shell.expect("ss -tlnp", "\n".join([
        'tcp 0.0.0.0:22 users:(("sshd",pid=1,fd=3))',
        'tcp 127.0.0.1:46051 users:(("MainThread",pid=1481,fd=22))',
        'tcp 127.0.0.1:39775 users:(("MainThread",pid=1482,fd=23))',
    ]))
    assert alert_messages(check_ports(cfg)) == []


def test_a_new_exposed_port_still_alerts_with_its_protocol(cfg, shell, tools, no_state):
    tools.add("ss")
    no_state["ports"] = ['tcp 0.0.0.0:22 users:(("sshd"))']
    shell.expect("ss -tlnp", 'tcp 0.0.0.0:22 users:(("sshd",pid=1,fd=3))')
    shell.expect("ss -ulnp", 'udp 0.0.0.0:4444 users:(("nc",pid=9,fd=3))')
    messages = alert_messages(check_ports(cfg))
    assert len(messages) == 1
    assert "udp" in messages[0] and "4444" in messages[0]


def test_old_state_without_a_protocol_prefix_rebaselines_quietly(cfg, shell, tools, no_state):
    """Upgrading must not report every socket on the host as brand new."""
    tools.add("ss")
    no_state["ports"] = ['0.0.0.0:22 users:(("sshd",pid=101,fd=3))']   # pre-UDP format
    shell.expect("ss -tlnp", "\n".join([
        'tcp 0.0.0.0:22 users:(("sshd",pid=1,fd=3))',
        'tcp 0.0.0.0:443 users:(("nginx",pid=2,fd=4))',
    ]))
    section = check_ports(cfg)
    assert alert_messages(section) == []
    assert any("re-recorded" in r.value for r in section.rows)


# ─────────────────────────────────────────────────────────────────────────────
# /etc: backup agents write timestamped files on a schedule
# ─────────────────────────────────────────────────────────────────────────────

def test_commvault_registry_backups_are_ignored_by_default(cfg):
    """Observed on a real server: a new .zst every 90 minutes."""
    args = _etc_ignore_args(cfg)
    assert "/etc/CommVaultRegistryBackups/*" in args


def test_extra_ignore_patterns_come_from_the_config(cfg):
    cfg.set("checks", "etc_ignore", "/etc/foo/*, /etc/bar/*.bak")
    args = _etc_ignore_args(cfg)
    assert "-not -path '/etc/foo/*'" in args
    assert "-not -path '/etc/bar/*.bak'" in args


def test_ignore_patterns_are_shell_quoted(cfg):
    """healthcheck.conf is admin-owned, but a pattern must stay one argument.

    Re-parse the way the shell will: a space or a semicolon inside a pattern
    must not split it into extra words.
    """
    import shlex
    cfg.set("checks", "etc_ignore", "/etc/x y/*; rm -rf /")
    tokens = shlex.split(_etc_ignore_args(cfg))
    assert "/etc/x y/*; rm -rf /" in tokens, tokens
    assert "rm" not in tokens and ";" not in tokens
    assert tokens.count("-not") == 9


def test_the_ignore_list_reaches_the_find_command(cfg, shell, monkeypatch):
    import hc.checks
    monkeypatch.setattr(hc.checks, "_boot_time", lambda: 0.0)
    cfg.set("checks", "etc_ignore", "/etc/CommVaultRegistryBackups/*")
    shell.expect("find /etc", "")
    check_etc_changes(cfg)
    find_cmd = [c for c in shell.calls if "find /etc" in c][0]
    assert "CommVaultRegistryBackups" in find_cmd
