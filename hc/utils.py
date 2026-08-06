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
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
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


def load_state(name: str):
    try:
        return json.loads((STATE_DIR / f"{name}.json").read_text())
    except Exception:
        return None


def save_state(name: str, data) -> None:
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


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "smtp": {
            "host":     "relay.mpt.mp.br",
            "port":     "25",
            "use_tls":  "false",
            "username": "",
            "password": "",
            "from":     f"healthcheck@{socket.getfqdn()}",
        },
        "email": {
            "daily_recipients": "",
            "alert_recipients": "",
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
        },
        "crontab": {
            "time": "07:00",
        },
    })
    if CONFIG_PATH.exists():
        cfg.read(str(CONFIG_PATH))
    return cfg
