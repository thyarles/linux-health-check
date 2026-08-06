"""Unit tests for hc.checks — every check_* function with mocked shell calls."""

import configparser

import hc.checks as checks
from hc.models import OK, INFO, CAUTION, UNHEALTHY


def make_run(*results):
    """Fake hc.checks.run returning queued (rc, stdout, stderr) tuples."""
    queue = list(results)

    def fake(cmd, timeout=30):
        if queue:
            return queue.pop(0)
        return (0, "", "")

    return fake


def make_cfg(**overrides):
    cfg = configparser.ConfigParser()
    cfg.read_dict({"thresholds": {
        "cpu_caution": "80", "cpu_unhealthy": "95",
        "disk_caution": "90", "disk_unhealthy": "95",
        "ram_caution": "80", "ram_unhealthy": "95",
        "load_caution_mult": "1.0", "load_unhealthy_mult": "2.0",
    }})
    for key, value in overrides.items():
        cfg.set("thresholds", key, value)
    return cfg


def has_only(*names):
    return lambda cmd: cmd in names


# ── check_system_info ────────────────────────────────────────────────────────

def test_system_info_populates_rows(monkeypatch):
    monkeypatch.setattr(checks, "read_os_release",
                        lambda: {"PRETTY_NAME": "TestOS 1.0"})
    monkeypatch.setattr(checks, "run", make_run(
        (0, "6.1.0-test", ""),          # uname -r
        (0, "x86_64", ""),              # uname -m
        (0, "up 3 days", ""),           # uptime -p
        (0, "2026-08-01 04:00", ""),    # who -b
        (0, "8", ""),                   # nproc
        (0, "Intel(R) Xeon", ""),       # lscpu model
    ))
    s = checks.check_system_info()
    values = {r.label: r.value for r in s.rows}
    assert values["OS"] == "TestOS 1.0"
    assert values["Kernel"] == "6.1.0-test"
    assert values["Architecture"] == "x86_64"
    assert values["Uptime"] == "up 3 days"
    assert values["CPU Cores"] == "8"
    assert values["CPU Model"] == "Intel(R) Xeon"
    assert values["Hostname"]


def test_system_info_uptime_fallback(monkeypatch):
    monkeypatch.setattr(checks, "read_os_release", lambda: {})
    monkeypatch.setattr(checks, "run", make_run(
        (0, "6.1.0", ""),               # uname -r
        (0, "x86_64", ""),              # uname -m
        (1, "", "no such option"),      # uptime -p fails
        (0, "up 5 hours", ""),          # sed fallback
        (0, "2026-08-01 04:00", ""),    # who -b
        (0, "8", ""),                   # nproc
        (0, "", ""),                    # lscpu
    ))
    s = checks.check_system_info()
    values = {r.label: r.value for r in s.rows}
    assert values["Uptime"] == "up 5 hours"
    assert values["Kernel"] == "6.1.0"


# ── check_cpu ────────────────────────────────────────────────────────────────

MPSTAT_HEADER = (
    "04:05:01  CPU    %usr   %nice    %sys %iowait   %irq   %soft  %steal  %guest  %gnice   %idle\n"
)
MPSTAT_ALL = "04:05:01  all    10.00    0.00    5.00    1.00    0.00    0.00    0.00    0.00    0.00   84.00\n"
MPSTAT_CPU0 = "04:05:01    0    50.00    0.00    0.00    0.00    0.00    0.00    0.00    0.00    0.00   50.00\n"


def test_cpu_load_average_caution(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only("mpstat"))
    monkeypatch.setattr(checks, "run", make_run(
        (0, "4", ""),                       # nproc
        (0, "5.5 4.0 3.0", ""),             # loadavg: 5.5 >= 1.0*4 → CAUTION
        (0, MPSTAT_HEADER + MPSTAT_ALL + MPSTAT_CPU0, ""),
    ))
    s = checks.check_cpu(make_cfg())
    values = {r.label: r.value for r in s.rows}
    assert "CAUTION" in s.status or values["Load Average (1/5/15 min)"]
    row = [r for r in s.rows if r.label.startswith("Load Average")][0]
    assert row.status == CAUTION
    assert "5.50" in row.value


def test_cpu_load_average_unhealthy_alerts(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only("mpstat"))
    monkeypatch.setattr(checks, "run", make_run(
        (0, "4", ""),
        (0, "9.0 4.0 3.0", ""),             # 9.0 >= 2.0*4 → UNHEALTHY
        (0, MPSTAT_HEADER + MPSTAT_ALL, ""),
    ))
    s = checks.check_cpu(make_cfg())
    row = [r for r in s.rows if r.label.startswith("Load Average")][0]
    assert row.status == UNHEALTHY
    assert any("Load avg 9.00" in a for a in s.alert_lines)


def test_cpu_mpstat_per_core_thresholds(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only("mpstat"))
    busy = "04:05:01    3    90.00    0.00    0.00    0.00    0.00    0.00    0.00    0.00    0.00   10.00\n"
    dead = "04:05:01    7    98.00    0.00    0.00    0.00    0.00    0.00    0.00    0.00    0.00    2.00\n"
    monkeypatch.setattr(checks, "run", make_run(
        (0, "8", ""),
        (0, "0.5 0.4 0.3", ""),
        (0, MPSTAT_HEADER + MPSTAT_ALL + busy + dead, ""),
    ))
    s = checks.check_cpu(make_cfg())
    rows = {r.label: r for r in s.rows}
    assert rows["CPU 3"].status == CAUTION       # 90% used
    assert rows["CPU 7"].status == UNHEALTHY     # 98% used
    assert rows["CPU all"].status == OK          # 16% used
    assert any("CPU 7 at 98%" in a for a in s.alert_lines)


def test_cpu_mpstat_comma_decimals(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only("mpstat"))
    line = "04:05:01    2    99.00    0.00    0.00    0.00    0.00    0.00    0.00    0.00    0.00   0,50\n"
    monkeypatch.setattr(checks, "run", make_run(
        (0, "4", ""),
        (0, "0.1 0.1 0.1", ""),
        (0, MPSTAT_HEADER + line, ""),
    ))
    s = checks.check_cpu(make_cfg())
    rows = {r.label: r for r in s.rows}
    assert rows["CPU 2"].status == UNHEALTHY     # 99.5% used


def test_cpu_fallback_proc_stat_and_need_tool(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only())  # no mpstat
    monkeypatch.setattr(checks, "run", make_run(
        (0, "4", ""),
        (0, "0.2 0.1 0.1", ""),
        (0, "cpu  100 0 100 800 0 0 0 0 0 0", ""),   # 20% used
    ))
    s = checks.check_cpu(make_cfg())
    row = [r for r in s.rows if r.label.startswith("CPU (aggregate")][0]
    assert row.status == OK
    assert "20.0%" in row.value
    assert s.missing_tools and s.missing_tools[0]["tool"] == "mpstat"


# ── check_memory ─────────────────────────────────────────────────────────────

FREE_HEADER = "              total        used        free      shared  buff/cache   available\n"
FREE_OK = (
    "Mem:       16777216000  3355443200  13421772800    104857600  104857600 12884901888\n"
    "Swap:       2147483648   536870912  1610612736\n"
)
FREE_CAUTION = (
    "Mem:       16777216000  13421772800  3355443200    104857600  0  0\n"
    "Swap:       2147483648   536870912  1610612736\n"
)
FREE_UNHEALTHY = (
    "Mem:       16777216000  15999999999  777216001    104857600  0  0\n"
    "Swap:       2147483648   2147483648  0\n"
)


def test_memory_ok(monkeypatch):
    monkeypatch.setattr(checks, "run", make_run((0, FREE_HEADER + FREE_OK, "")))
    s = checks.check_memory(make_cfg())
    rows = {r.label: r for r in s.rows}
    assert rows["Mem"].status == OK
    assert "3 GB" in rows["Mem"].value            # 3355443200 B → 3 GB (floor div)
    assert rows["Swap"].status == OK


def test_memory_caution_no_alert(monkeypatch):
    """Memory alerts only fire at UNHEALTHY (by design); CAUTION shows in report."""
    monkeypatch.setattr(checks, "run", make_run((0, FREE_HEADER + FREE_CAUTION, "")))
    s = checks.check_memory(make_cfg())
    rows = {r.label: r for r in s.rows}
    assert rows["Mem"].status == CAUTION          # exactly 80%
    assert s.alert_lines == []


def test_memory_unhealthy(monkeypatch):
    monkeypatch.setattr(checks, "run", make_run((0, FREE_HEADER + FREE_UNHEALTHY, "")))
    s = checks.check_memory(make_cfg())
    rows = {r.label: r for r in s.rows}
    assert rows["Mem"].status == UNHEALTHY
    assert rows["Swap"].status == UNHEALTHY


# ── check_disk ───────────────────────────────────────────────────────────────

DF_OK = "/dev/sda1        104857600  94371840  10485760      90% /\n"
DF_HOME = "/dev/sda2        209715200  104857600 104857600      50% /home\n"
DF_SNAP = "/dev/loop0        209715200  207618048   2097152      99% /snap/core/1234\n"
DF_BAD = "/dev/sdb1  notanumber  x  y  zzz /mnt/broken\n"


def test_disk_thresholds_and_alert(monkeypatch):
    monkeypatch.setattr(checks, "run", make_run((0, DF_OK + DF_HOME + DF_SNAP + DF_BAD, "")))
    s = checks.check_disk(make_cfg())
    rows = {r.label: r for r in s.rows}
    assert rows["/"].status == CAUTION
    assert rows["/home"].status == OK
    assert "/snap/core/1234" not in rows          # snap mounts skipped
    assert "/mnt/broken" not in rows              # unparseable line skipped
    assert any("Disk / at 90%" in a for a in s.alert_lines)


def test_disk_unhealthy(monkeypatch):
    line = "/dev/sda1        104857600  100663296   4194304      96% /\n"
    monkeypatch.setattr(checks, "run", make_run((0, line, "")))
    s = checks.check_disk(make_cfg())
    rows = {r.label: r for r in s.rows}
    assert rows["/"].status == UNHEALTHY


# ── check_processes ──────────────────────────────────────────────────────────

PS_MEM = (
    "user 123 0.5 12.3 100 200 pts/0 S 08:00 0:01 /usr/bin/bigapp --flag\n"
    "user 456 0.2 5.1 100 200 pts/1 S 08:01 0:00 /usr/bin/other\n"
)
PS_CPU = (
    "user 789 45.0 1.0 100 200 pts/2 R 08:02 0:10 /usr/bin/hog\n"
)


def test_processes_parses_rows_and_zombies(monkeypatch):
    monkeypatch.setattr(checks, "run", make_run(
        (0, PS_MEM, ""),
        (0, PS_CPU, ""),
        (0, "3", ""),                              # 3 zombies → CAUTION
    ))
    s = checks.check_processes()
    rows = {r.label: r for r in s.rows}
    assert rows["PID 123"].status == INFO
    assert "12.3% MEM" in rows["PID 123"].value
    assert rows["PID 789"].status == INFO
    assert rows["Zombie Processes"].status == CAUTION


def test_processes_zombie_flood(monkeypatch):
    monkeypatch.setattr(checks, "run", make_run(
        (0, "", ""),
        (0, "", ""),
        (0, "7", ""),                              # 7 zombies → UNHEALTHY + alert
    ))
    s = checks.check_processes()
    rows = {r.label: r for r in s.rows}
    assert rows["Zombie Processes"].status == UNHEALTHY
    assert any("7 zombie processes" in a for a in s.alert_lines)


# ── check_services ───────────────────────────────────────────────────────────

def test_services_failed_units(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only("systemctl"))
    monkeypatch.setattr(checks, "run", make_run(
        (0, "nginx.service loaded failed failed The nginx HTTP server\n", ""),
        (0, "", ""),
    ))
    s = checks.check_services()
    rows = {r.label: r for r in s.rows}
    assert rows["nginx.service"].status == UNHEALTHY
    assert any("Service nginx.service failed" in a for a in s.alert_lines)


def test_services_activating(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only("systemctl"))
    monkeypatch.setattr(checks, "run", make_run(
        (0, "", ""),
        (0, "foo.service\n", ""),
    ))
    s = checks.check_services()
    assert any(r.label == "foo.service" and r.status == CAUTION for r in s.rows)


def test_services_missing_systemd(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only())
    s = checks.check_services()
    assert s.rows[0].value == "Not available on this system"


# ── check_docker ─────────────────────────────────────────────────────────────

def test_docker_not_installed(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only())
    s = checks.check_docker()
    assert s.rows[0].value == "Not installed (optional)"
    assert s.missing_tools[0]["optional"] is True


def test_docker_stopped_containers_alert(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only("docker"))
    monkeypatch.setattr(checks, "run", make_run(
        (0, "web\tUp 3 hours\tnginx:latest\nold\tExited (0) 2 days ago\tubuntu:20.04\n", ""),
        (0, "TYPE            TOTAL     ACTIVE    SIZE    RECLAIMABLE\nImages          3         1        1.2GB   800MB\n", ""),
    ))
    s = checks.check_docker()
    rows = {r.label: r for r in s.rows}
    assert rows["web"].status == OK
    assert rows["old"].status == CAUTION
    assert any("1 stopped Docker container" in a for a in s.alert_lines)
    assert any(r.label == "Images" for r in s.rows)


# ── check_updates ────────────────────────────────────────────────────────────

def test_updates_dnf_ok(monkeypatch):
    monkeypatch.setattr(checks, "pkg_manager", lambda: "dnf")
    monkeypatch.setattr(checks, "run", make_run((0, "", "")))
    s = checks.check_updates()
    rows = {r.label: r for r in s.rows}
    assert rows["Pending Updates"].status == OK
    assert rows["Pending Updates"].value == "0"


def test_updates_dnf_caution(monkeypatch):
    monkeypatch.setattr(checks, "pkg_manager", lambda: "dnf")
    updates = "\n".join(f"pkg{i}.x86_64 1.0-1 repo" for i in range(25)) + "\n"
    monkeypatch.setattr(checks, "run", make_run((0, updates, "")))
    s = checks.check_updates()
    rows = {r.label: r for r in s.rows}
    assert rows["Pending Updates"].status == CAUTION
    assert any("25 pending package updates" in a for a in s.alert_lines)


def test_updates_apt_security(monkeypatch):
    monkeypatch.setattr(checks, "pkg_manager", lambda: "apt-get")
    monkeypatch.setattr(checks, "run", make_run(
        (0, "", ""),      # apt-get update
        (0, "5", ""),     # upgrade count
        (0, "2", ""),     # security count
    ))
    s = checks.check_updates()
    rows = {r.label: r for r in s.rows}
    assert rows["Pending Updates"].status == INFO
    assert rows["Security Updates"].status == CAUTION
    assert any("2 pending security updates" in a for a in s.alert_lines)


def test_updates_unknown_pm(monkeypatch):
    monkeypatch.setattr(checks, "pkg_manager", lambda: "")
    s = checks.check_updates()
    assert s.rows[0].value == "Not detected"


# ── check_users ──────────────────────────────────────────────────────────────

def test_users_sections(monkeypatch):
    monkeypatch.setattr(checks, "run", make_run(
        (0, "alice pts/0 2026-08-06 08:00 (10.0.0.5)", ""),
        (0, "alice pts/0 2026-08-05 22:00 still logged in\n", ""),
        (0, "root pts/1 2026-08-06 07:30 still logged in\n", ""),
    ))
    s = checks.check_users()
    rows = {r.label: r for r in s.rows}
    assert rows["Logged In"].value.startswith("alice")
    assert rows["root"].status == CAUTION


def test_users_no_one_logged_in(monkeypatch):
    monkeypatch.setattr(checks, "run", make_run(
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
    ))
    s = checks.check_users()
    rows = {r.label: r for r in s.rows}
    assert rows["Logged In"].value == "None"


# ── check_auth_security ──────────────────────────────────────────────────────

def test_auth_security_failed_attempts(monkeypatch):
    monkeypatch.setattr(checks, "_find_auth_log", lambda: "/var/log/auth.log")
    monkeypatch.setattr(checks, "count_in_log", lambda log, pat: {
        "Failed password": 15, "Accepted (password|publickey)": 3, r"sudo.*COMMAND": 2,
    }.get(pat, 0))
    monkeypatch.setattr(checks, "run", make_run(
        (0, "11 1.2.3.4\n3 5.6.7.8", ""),       # top attacking IPs
        (0, "alice sudo -u root whoami", ""),    # recent sudo commands
    ))
    s = checks.check_auth_security()
    rows = {r.label: r for r in s.rows}
    assert rows["Failed SSH Attempts (today)"].status == CAUTION
    assert rows["Successful SSH Logins (today)"].value == "3"
    assert rows["1.2.3.4"].status == CAUTION
    assert any("15 failed SSH attempts today" in a for a in s.alert_lines)


def test_auth_security_unhealthy_over_100(monkeypatch):
    monkeypatch.setattr(checks, "_find_auth_log", lambda: "/var/log/auth.log")
    monkeypatch.setattr(checks, "count_in_log", lambda log, pat: 150 if pat == "Failed password" else 0)
    monkeypatch.setattr(checks, "run", make_run(
        (0, "150 203.0.113.9", ""),
        (0, "", ""),
    ))
    s = checks.check_auth_security()
    rows = {r.label: r for r in s.rows}
    assert rows["Failed SSH Attempts (today)"].status == UNHEALTHY


def test_auth_security_no_auth_log(monkeypatch):
    monkeypatch.setattr(checks, "_find_auth_log", lambda: "")
    monkeypatch.setattr(checks, "count_in_log", lambda log, pat: 0)
    s = checks.check_auth_security()
    rows = {r.label: r for r in s.rows}
    assert rows["Failed SSH Attempts (today)"].status == OK
    assert rows["Auth Log"].status == CAUTION


# ── check_fail2ban ───────────────────────────────────────────────────────────

def test_fail2ban_not_installed(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only())
    s = checks.check_fail2ban()
    assert s.rows[0].value == "Not installed"
    assert s.missing_tools[0]["tool"] == "fail2ban-client"


def test_fail2ban_jails(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only("fail2ban-client"))
    monkeypatch.setattr(checks, "run", make_run(
        (0, "Status\n|- Number of jail:\t2\n`- Jail list:\tsshd, dovecot\n", ""),
        (0, "Status for the jail: sshd\n|- Currently banned:\t2\n`- Total banned:\t10\n", ""),
        (0, "Status for the jail: dovecot\n|- Currently banned:\t0\n`- Total banned:\t5\n", ""),
    ))
    s = checks.check_fail2ban()
    rows = {r.label: r for r in s.rows}
    assert rows["Jail: sshd"].status == CAUTION
    assert rows["Jail: dovecot"].status == OK
    assert any("fail2ban: 2 IP(s) currently banned" in a for a in s.alert_lines)


# ── check_ports ──────────────────────────────────────────────────────────────

SS_OUT = "0.0.0.0:22 users:((\"sshd\"))\n0.0.0.0:80 users:((\"nginx\"))\n"

def test_ports_baseline_first_run(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only("ss"))
    monkeypatch.setattr(checks, "run", make_run((0, SS_OUT, "")))
    monkeypatch.setattr(checks, "load_state", lambda name: None)
    saved = {}
    monkeypatch.setattr(checks, "save_state",
                        lambda name, data: saved.update({name: data}))
    s = checks.check_ports()
    assert any("first run" in r.value for r in s.rows)
    assert saved["ports"] == sorted(set(SS_OUT.splitlines()))


def test_ports_change_detection_alerts(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only("ss"))
    monkeypatch.setattr(checks, "run", make_run((0, SS_OUT, "")))
    prev = ["0.0.0.0:22 users:((\"sshd\"))", "0.0.0.0:3306 users:((\"mysqld\"))"]
    monkeypatch.setattr(checks, "load_state", lambda name: prev)
    monkeypatch.setattr(checks, "save_state", lambda name, data: None)
    s = checks.check_ports()
    assert any(r.label == "NEW" and "0.0.0.0:80" in r.value for r in s.rows)
    assert any(r.label == "GONE" and "3306" in r.value for r in s.rows)
    assert any("New listening port: 0.0.0.0:80" in a for a in s.alert_lines)


def test_ports_netstat_fallback(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only("netstat"))
    monkeypatch.setattr(checks, "run", make_run((0, "tcp 0 0 0.0.0.0:22 0.0.0.0:* LISTEN 1234/sshd\n", "")))
    monkeypatch.setattr(checks, "load_state", lambda name: None)
    monkeypatch.setattr(checks, "save_state", lambda name, data: None)
    s = checks.check_ports()
    assert any("0.0.0.0:22" in r.value for r in s.rows)
    assert s.missing_tools and s.missing_tools[0]["tool"] == "ss"


def test_ports_neither_tool(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only())
    s = checks.check_ports()
    assert s.rows[0].value == "Neither available"
    assert s.missing_tools[0]["optional"] is False


# ── check_crontabs ───────────────────────────────────────────────────────────

def test_crontabs_baseline(monkeypatch):
    monkeypatch.setattr(checks, "run", make_run())   # grep returns nothing
    monkeypatch.setattr(checks, "load_state", lambda name: None)
    monkeypatch.setattr(checks, "save_state", lambda name, data: None)
    s = checks.check_crontabs()
    assert any("first run" in r.value for r in s.rows)


def test_crontabs_new_entry_alerts(monkeypatch):
    class CronPath:
        def __init__(self, p):
            self.p = p

        def is_dir(self):
            return False

        def is_file(self):
            return self.p == "/etc/crontab"

        def iterdir(self):
            raise AssertionError("not a directory")

        @property
        def name(self):
            return self.p.rsplit("/", 1)[-1]

    monkeypatch.setattr(checks, "pathlib", type("PL", (), {"Path": CronPath})())

    def always_line(cmd, timeout=30):
        return (0, "0 2 * * * root /usr/local/bin/backup.sh", "")

    monkeypatch.setattr(checks, "run", always_line)
    monkeypatch.setattr(checks, "load_state", lambda name: [])
    monkeypatch.setattr(checks, "save_state", lambda name, data: None)
    s = checks.check_crontabs()
    assert any(r.label == "NEW" for r in s.rows)
    assert any("New crontab entry detected" in a for a in s.alert_lines)


def test_crontabs_removed_entry(monkeypatch):
    monkeypatch.setattr(checks, "run", make_run())
    prev = ["/etc/crontab: 0 1 * * * root /usr/local/bin/old-job.sh"]
    monkeypatch.setattr(checks, "load_state", lambda name: prev)
    monkeypatch.setattr(checks, "save_state", lambda name, data: None)
    s = checks.check_crontabs()
    assert any(r.label == "GONE" for r in s.rows)


# ── check_suid_files ─────────────────────────────────────────────────────────

def test_suid_baseline(monkeypatch):
    monkeypatch.setattr(checks, "run", make_run((0, "/usr/bin/passwd\n/usr/bin/sudo\n", "")))
    monkeypatch.setattr(checks, "load_state", lambda name: None)
    monkeypatch.setattr(checks, "save_state", lambda name, data: None)
    s = checks.check_suid_files()
    assert any("first run" in r.value for r in s.rows)


def test_suid_new_file_unhealthy(monkeypatch):
    monkeypatch.setattr(checks, "run", make_run((0, "/usr/bin/passwd\n/usr/bin/newsuid\n", "")))
    monkeypatch.setattr(checks, "load_state", lambda name: ["/usr/bin/passwd"])
    monkeypatch.setattr(checks, "save_state", lambda name, data: None)
    s = checks.check_suid_files()
    assert any(r.label == "NEW" and r.value == "/usr/bin/newsuid" and r.status == UNHEALTHY
               for r in s.rows)
    assert any("New SUID file: /usr/bin/newsuid" in a for a in s.alert_lines)


# ── check_package_changes ────────────────────────────────────────────────────

def test_package_changes_dnf_installed(monkeypatch):
    monkeypatch.setattr(checks, "pkg_manager", lambda: "dnf")
    monkeypatch.setattr(checks, "run", make_run(
        (0, "vim-8.2-1.el9\nbash-5.1-8.el9\n", ""),    # rpm -qa
        (0, "ID     | Action(s) | Date\n", ""),         # dnf history
    ))
    monkeypatch.setattr(checks, "load_state", lambda name: ["bash-5.1-8.el9"])
    monkeypatch.setattr(checks, "save_state", lambda name, data: None)
    s = checks.check_package_changes()
    assert any(r.label == "INSTALLED" and r.value == "vim-8.2-1.el9" for r in s.rows)
    assert any("1 new package(s) installed" in a for a in s.alert_lines)


def test_package_changes_apt_removed(monkeypatch):
    monkeypatch.setattr(checks, "pkg_manager", lambda: "apt-get")
    monkeypatch.setattr(checks, "run", make_run(
        (0, "bash 5.1-6ubuntu1\ncoreutils 8.32-4.1ubuntu1\n", ""),
    ))
    monkeypatch.setattr(checks, "load_state", lambda name: ["bash 5.1-6ubuntu1", "oldpkg 1.0-1"])
    monkeypatch.setattr(checks, "save_state", lambda name, data: None)
    s = checks.check_package_changes()
    assert any(r.label == "REMOVED" and r.value == "oldpkg 1.0-1" for r in s.rows)


def test_package_changes_unknown_pm(monkeypatch):
    monkeypatch.setattr(checks, "pkg_manager", lambda: "")
    s = checks.check_package_changes()
    assert s.rows[0].value == "Not detected"


# ── check_etc_changes ────────────────────────────────────────────────────────

def test_etc_changes_alert(monkeypatch):
    monkeypatch.setattr(checks, "run", make_run(
        (0, "/etc/ssh/sshd_config\n", ""),     # find output
        (0, "2026-08-06 03:14:00.000000000 +0200", ""),   # stat mtime
    ))
    s = checks.check_etc_changes()
    rows = {r.label: r for r in s.rows}
    assert rows["/etc/ssh/sshd_config"].status == CAUTION
    assert any("/etc change: /etc/ssh/sshd_config" in a for a in s.alert_lines)


def test_etc_no_changes(monkeypatch):
    monkeypatch.setattr(checks, "run", make_run((0, "", "")))
    s = checks.check_etc_changes()
    assert any("No files modified" in r.value for r in s.rows)


# ── check_log_patterns ───────────────────────────────────────────────────────

class FakePath:
    def __init__(self, p):
        self.p = p

    def exists(self):
        return self.p in ("/var/log/syslog", "/var/log/auth.log")


class FakePathlib:
    Path = FakePath


def test_log_patterns_counts_and_alerts(monkeypatch):
    monkeypatch.setattr(checks, "pathlib", FakePathlib())
    monkeypatch.setattr(checks, "_find_auth_log", lambda: "/var/log/auth.log")

    def smart_run(cmd, timeout=30):
        if "grep -ciE" in cmd:
            return (0, "3" if "Out of memory" in cmd else "0", "")
        if "tail -3" in cmd:
            return (0, "kernel: Out of memory: Killed process 1234\n", "")
        return (0, "", "")

    monkeypatch.setattr(checks, "run", smart_run)
    s = checks.check_log_patterns()
    rows = {r.label: r for r in s.rows}
    assert rows["OOM Killer"].status == CAUTION
    assert "3" in rows["OOM Killer"].value
    assert rows["Disk I/O Errors"].status == OK
    assert any("OOM Killer: 3 occurrence(s)" in a for a in s.alert_lines)
    assert any("Out of memory" in r.value for r in s.rows)


def test_log_patterns_no_logs(monkeypatch):
    class NoLogs(FakePath):
        def exists(self):
            return False
    monkeypatch.setattr(checks, "pathlib", type("PL", (), {"Path": NoLogs})())
    monkeypatch.setattr(checks, "_find_auth_log", lambda: "")
    s = checks.check_log_patterns()
    assert s.rows == []          # every check skipped, nothing to report


# ── check_rootkit ────────────────────────────────────────────────────────────

def test_rootkit_rkhunter_warnings(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only("rkhunter"))
    monkeypatch.setattr(checks, "run", make_run(
        (0, "Warning: Suspicious file found\nInfo: All ok\n", ""),
        (0, "1\n2\n3\n4\n", ""),
        (0, "1\n2\n3\n4\n", ""),
    ))
    s = checks.check_rootkit()
    rows = {r.label: r for r in s.rows}
    assert rows["rkhunter"].status == CAUTION
    assert rows["Known Rootkit Paths"].value == "None found"
    assert rows["Process Visibility"].status == OK


def test_rootkit_hidden_processes(monkeypatch):
    monkeypatch.setattr(checks, "has", has_only())
    monkeypatch.setattr(checks, "run", make_run(
        (0, "1\n2\n3\n4\n5\n6\n", ""),
        (0, "1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n11\n12\n", ""),
    ))
    s = checks.check_rootkit()
    rows = {r.label: r for r in s.rows}
    assert rows["Hidden Processes"].status == CAUTION
    assert any("6 potentially hidden processes" in a for a in s.alert_lines)


# ── check_network_io ─────────────────────────────────────────────────────────

PROC_NET_DEV = (
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
    "    lo: 100 1 0 0 0 0 0 0 100 1 0 0 0 0 0 0\n"
    "  eth0: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0\n"
)


def test_network_io_parses_interfaces(monkeypatch):
    class NetPath:
        def __init__(self, p):
            pass

        def read_text(self):
            return PROC_NET_DEV

    monkeypatch.setattr(checks, "pathlib", type("PL", (), {"Path": NetPath})())
    monkeypatch.setattr(checks, "has", has_only("ss"))
    monkeypatch.setattr(checks, "run", make_run(
        (0, "Total: 3 (kernel 0)\nTCP: 2 (estab 1, closed 0)\n", ""),
    ))
    s = checks.check_network_io()
    rows = {r.label: r for r in s.rows}
    assert rows["eth0"].value == "RX 1000 B  TX 1 KB"   # 2000 B → 1 KB (floor div)
    assert "lo" not in rows
    assert any("TCP:" in r.value for r in s.rows)


def test_network_io_missing_proc_net_dev(monkeypatch):
    class BrokenPath:
        def __init__(self, p):
            pass

        def read_text(self):
            raise FileNotFoundError

    monkeypatch.setattr(checks, "pathlib", type("PL", (), {"Path": BrokenPath})())
    monkeypatch.setattr(checks, "has", has_only())
    s = checks.check_network_io()
    assert not any(r.label == "eth0" for r in s.rows)


# ── check_tools ──────────────────────────────────────────────────────────────

def test_tools_status(monkeypatch):
    installed = {"mpstat"}
    monkeypatch.setattr(checks, "has", lambda cmd: cmd in installed)
    monkeypatch.setattr(checks, "install_cmd",
                        lambda tool, rhel_pkg, deb_pkg: f"dnf install -y {rhel_pkg}")
    s = checks.check_tools()
    rows = {r.label: r for r in s.rows}
    assert rows["mpstat"].value == "Installed"
    assert rows["mpstat"].status == OK
    assert "dnf install -y iproute" in rows["ss"].value
    assert rows["ss"].status == CAUTION
    assert rows["[optional] fail2ban-client"].status == INFO
    assert rows["[optional] rkhunter"].status == INFO
    assert rows["[optional] docker"].status == INFO
    assert rows["postfix"].status == CAUTION
