import configparser
import datetime
import json
import pathlib
import shutil
import socket
import subprocess

VERSION     = "2.0"
SCRIPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "healthcheck.py"
SCRIPT_DIR  = SCRIPT_PATH.parent
STATE_DIR   = SCRIPT_DIR / "state"
REPORT_DIR  = SCRIPT_DIR / "reports"
CONFIG_PATH = SCRIPT_DIR / "healthcheck.conf"


def run(cmd: str, timeout: int = 30) -> tuple:
    """Run a shell command; returns (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, timeout=timeout
        )
        stdout = r.stdout.decode("utf-8", errors="replace").strip()
        stderr = r.stderr.decode("utf-8", errors="replace").strip()
        return r.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as exc:
        return -1, "", str(exc)


def has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}"
        n //= 1024
    return f"{n:.0f} PB"


def read_os_release() -> dict:
    d: dict = {}
    try:
        for line in pathlib.Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                d[k.strip()] = v.strip().strip('"')
    except FileNotFoundError:
        pass
    return d


def pkg_manager() -> str:
    """Return available package manager: dnf, yum, or apt-get."""
    for pm in ("dnf", "yum", "apt-get"):
        if has(pm):
            return pm
    return ""


def install_cmd(tool: str, rhel_pkg: str, deb_pkg: str) -> str:
    pm = pkg_manager()
    if pm in ("dnf", "yum"):
        return f"{pm} install -y {rhel_pkg}"
    elif pm == "apt-get":
        return f"apt-get install -y {deb_pkg}"
    return f"yum install -y {rhel_pkg}  OR  apt-get install -y {deb_pkg}"


# When true, save_state() is a no-op. Set by the read-only modes (report/text)
# so that previewing a report does not consume the diff baselines that the
# scheduled run depends on.
_STATE_FROZEN = False


def freeze_state(frozen: bool = True) -> None:
    global _STATE_FROZEN
    _STATE_FROZEN = frozen


def load_state(name: str):
    try:
        return json.loads((STATE_DIR / f"{name}.json").read_text())
    except Exception:
        return None


def save_state(name: str, data) -> None:
    if _STATE_FROZEN:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{name}.json").write_text(json.dumps(data, indent=2))


def today_date_re() -> str:
    """Regex matching today's date in syslog format: 'Aug  6' or 'Aug 16'."""
    today = datetime.date.today()
    return today.strftime("%b") + r"[ ]+" + str(today.day) + r"[ ]"


def count_in_log(log_file: str, grep_pattern: str) -> int:
    """Count lines matching pattern in a log file, restricted to today."""
    if has("journalctl"):
        _, out, _ = run(
            f"journalctl --since=today --no-pager -q 2>/dev/null | grep -cE '{grep_pattern}' || echo 0",
            timeout=30,
        )
    else:
        if not pathlib.Path(log_file).exists():
            return 0
        date_re = today_date_re()
        _, out, _ = run(
            f"grep -E '{date_re}' {log_file} 2>/dev/null | grep -cE '{grep_pattern}' || echo 0"
        )
    try:
        return int(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def count_in_log_today(sources: str, grep_pattern: str) -> tuple:
    """Count matches for `grep_pattern` limited to TODAY, plus up to 3 samples.

    Greping the whole log file makes every check sticky: one segfault last
    month keeps the report yellow until the log rotates. Everything here is
    scoped to the current day so a cleared condition clears the report.
    """
    if has("journalctl"):
        _, out, _ = run(
            f"journalctl --since=today --no-pager -q 2>/dev/null | grep -iE '{grep_pattern}' | tail -200",
            timeout=60,
        )
        found = [l for l in out.splitlines() if l.strip()]
        return len(found), found[-3:]

    date_re = today_date_re()
    lines: "list" = []
    for src in sources.split():
        if not pathlib.Path(src).exists():
            continue
        _, out, _ = run(
            f"grep -E '{date_re}' {src} 2>/dev/null | grep -iE '{grep_pattern}' | tail -200"
        )
        lines.extend(l for l in out.splitlines() if l.strip())
    return len(lines), lines[-3:]


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "smtp": {
            "host":     "relay.domain.com",
            "port":     "25",
            "use_tls":  "false",
            "username": "",
            "password": "",
            "from":     f"healthcheck@{socket.getfqdn()}",
        },
        "email": {
            # Small "the system is alive" group — always gets a message.
            "daily_recipients": "",
            # Broad list — only hears about NEW problems worth acting on.
            "alert_recipients": "",
            # How the HTML report reaches the reader:
            #   inline     — rendered in the message body (recommended)
            #   attachment — plain text in the body, HTML as a file
            #   both       — inline and attached
            "html_mode": "inline",
        },
        "alerts": {
            # Minimum severity that notifies alert_recipients: caution | unhealthy
            "notify_all_on":          "caution",
            # Re-notify about a condition that is still open after N hours.
            # 0 disables the reminder entirely (notify once, on first sight).
            "remind_caution_hours":   "168",   # 7 days
            "remind_unhealthy_hours": "24",
            # Forget a condition that has been clear for this long, so that a
            # genuine recurrence notifies again instead of being deduplicated.
            "forget_after_hours":     "72",
        },
        "checks": {
            # Listening-port inventory: loopback sockets cannot be reached from
            # off the host and applications churn through random high ports on
            # 127.0.0.1. Set true to list them anyway.
            "list_local_ports": "false",
            # Extra path patterns to skip in the /etc scan (comma separated).
            # Backup agents that write timestamped files into /etc belong here.
            "etc_ignore": "/etc/CommVaultRegistryBackups/*",
        },
        "thresholds": {
            "cpu_caution":         "80",
            "cpu_unhealthy":       "95",
            "disk_caution":        "90",
            "disk_unhealthy":      "95",
            "ram_caution":         "80",
            "ram_unhealthy":       "95",
            "load_caution_mult":   "1.0",
            "load_unhealthy_mult": "2.0",
            # Zombies are normal in small numbers; only a growing pile matters.
            "zombie_caution":      "10",
            "zombie_unhealthy":    "50",
            # Failed SSH attempts today. Any internet-facing host sees a
            # constant background level, so these are deliberately high.
            "failed_ssh_caution":   "50",
            "failed_ssh_unhealthy": "500",
            # Pending updates: count that raises CAUTION (0 = report as INFO only)
            "updates_caution":          "0",
            "security_updates_caution": "0",
            # fail2ban bans are the system WORKING. Only flag an unusual spike.
            "banned_ips_caution":  "0",
            # Docker: exited-non-zero containers within this window are fresh
            # failures; anything older is treated as intentionally stopped.
            "docker_recent_hours": "24",
        },
        "crontab": {
            "time": "07:00",
        },
    })
    if CONFIG_PATH.exists():
        cfg.read(str(CONFIG_PATH))
    return cfg
