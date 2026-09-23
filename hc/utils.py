import configparser
import datetime
import json
import pathlib
import shutil
import socket
import subprocess

VERSION     = "2.2.0"
SCRIPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "healthcheck.py"
SCRIPT_DIR  = SCRIPT_PATH.parent
STATE_DIR   = SCRIPT_DIR / "state"
REPORT_DIR  = SCRIPT_DIR / "reports"
CONFIG_PATH = SCRIPT_DIR / "healthcheck.conf"
# Shipped defaults, replaced wholesale by every upgrade. Read BEFORE
# healthcheck.conf so a setting added in a new release arrives with a working
# value without anyone editing the operator's file — see load_config().
BASE_PATH   = SCRIPT_DIR / "healthcheck.conf.base"


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


# Set from [general] hostname by load_config(), which always runs before
# anything renders. A module global rather than a parameter because the report
# renderers are called from several places and none of them carry the config.
_HOST_OVERRIDE = ""


def host_label() -> str:
    """The name this report is about.

    NOT socket.getfqdn(). That function takes the kernel's hostname, resolves it
    through /etc/hosts and DNS, and returns the FIRST name it finds there — which
    on a clustered host is routinely a shared VIP rather than the machine. Three
    RKE2 nodes sitting behind xlp-mgmt.domain.com each reported themselves as
    `xlp-mgmt.domain.com`, so their reports, their subject lines and their
    saved report files were indistinguishable and their alerts looked like one
    host flapping.

    Worse, getfqdn() is not even stable: it reverse-resolves, so on a node that
    sometimes holds a floating VIP the answer changes with the VIP. A host's
    identity must not drift.

    So the kernel's own hostname wins, and it is usually already qualified
    (`hostname` printing hst-exp03.domain.com). getfqdn() is consulted for one
    narrow purpose — supplying a domain the kernel name lacks — and only when
    it is talking about the same machine. That also avoids adopting getfqdn()'s
    casing, which resolvers are free to mangle: it answers hst-exp03.domain.com
    on a host whose own name is lowercase.
    """
    if _HOST_OVERRIDE:
        return _HOST_OVERRIDE
    name = socket.gethostname()
    if "." in name:
        return name
    fqdn = socket.getfqdn()
    if name and fqdn and fqdn.split(".")[0].lower() == name.lower():
        return fqdn
    return name or fqdn


def host_mail_domain() -> str:
    """Hostname for the default From: address.

    host_label() is usually a bare short name, and a From: with no domain is
    rejected by some relays — so borrow the domain from the resolved FQDN when
    there is one. Distinct per host, which is the whole point.
    """
    label = host_label()
    if "." in label:
        return label
    fqdn = socket.getfqdn()
    return f"{label}.{fqdn.split('.', 1)[1]}" if "." in fqdn else label


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


def builtin_defaults() -> dict:
    """The floor under everything, in code.

    healthcheck.conf.base ships the same values with the documentation attached
    and overrides this, so in a normal install these are never the answer. They
    exist so that the program still runs — and every test still has a complete
    config — when the base file is missing, which is what a hand-copied install
    of just healthcheck.py and hc/ produces. A test keeps the two in step.
    """
    return {
        "general": {
            # Overrides the name this host calls itself in reports, subject
            # lines and saved report files. Blank uses the kernel hostname —
            # see host_label() for why the resolved FQDN is not trusted.
            "hostname": "",
        },
        "smtp": {
            "host":     "relay.domain.com",
            "port":     "25",
            "use_tls":  "false",
            "username": "",
            "password": "",
            "from":     f"healthcheck@{host_mail_domain()}",
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
            # Minimum severity that notifies alert_recipients: caution | unhealthy.
            # This said "caution" in code and "unhealthy" in the shipped
            # config for three releases, so what a host did depended on
            # whether anyone had copied the file. unhealthy is what the
            # installed fleet is actually running.
            "notify_all_on":          "unhealthy",
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
            # Extra mount globs to skip in the disk check, ON TOP OF the
            # built-in container-runtime list. Kubernetes bind-mounts a pod's
            # subPaths and local-path PVCs off the root filesystem and df
            # reports each one with the ROOT device's numbers, so one node at
            # 91% used to emit forty identical rows and forty identical alerts.
            "disk_ignore": "",
            # Explicit kubeconfig. Blank auto-discovers rke2, k3s, kubeadm and
            # ~/.kube/config.
            "kubeconfig": "",
        },
        "kubernetes": {
            # auto    — cluster-wide, plus a per-node pod summary on a
            #           multi-node cluster
            # cluster — always the whole cluster
            # node    — only pods scheduled on this host
            # off     — no Kubernetes section at all
            "scope":  "auto",
            # Individual signals, so a noisy one can be silenced without
            # losing the rest.
            "nodes":  "true",
            "pods":   "true",
            "pvcs":   "true",
            "events": "true",
            "images": "true",
            # Images are inventory and churn on every deploy, so the default is
            # a count. Set true to list the largest ones.
            "list_images": "false",
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
            # A pod Pending or ContainerCreating for less than this is the
            # scheduler working, not a problem.
            "k8s_pending_minutes": "15",
            # A restart COUNT is meaningless — 200 days up with 5 restarts is a
            # healthy pod. The growth since the previous run is the signal.
            "k8s_restart_delta_caution": "3",
            # Evicted pod objects persist until GC. An eviction from last month
            # is not today's news.
            "k8s_evicted_recent_hours": "24",
            # Guard rail so a very large cluster cannot stall the run.
            "k8s_max_pods": "2000",
        },
        "crontab": {
            "time": "00:07",
            # Whole fleets install the same cron time, so at 00:07 every host
            # starts a full scan at once — often on top of the backup window,
            # which the check then reports as the CPU spike it caused itself.
            # The entry still fires on the hour; the run waits a random slice
            # of the window below before touching anything. A run started by
            # hand is never delayed.
            "random":        "true",
            # 8h, 90m, 2h30m — or a bare number, read as hours.
            "random_window": "8h",
        },
    }


# The layers, weakest first. Each one only has to say what it changes.
LAYERS = ("default", "base", "conf")


def config_layers() -> list:
    """(name, path) for the file-backed layers, in the order they are read."""
    return [("base", BASE_PATH), ("conf", CONFIG_PATH)]


def load_config() -> configparser.ConfigParser:
    """Effective config: built-in defaults < healthcheck.conf.base < healthcheck.conf.

    Merged rather than chosen. healthcheck.conf holds only what this host wants
    to be different, so an upgrade that adds a setting delivers it through the
    base file — no migration step, and the operator's own file is never
    rewritten to keep up.

    A key present in healthcheck.conf wins even when its value is empty:
    `daily_recipients =` is a deliberate "nobody", not an omission, and the
    base layer must not put its own value back.
    """
    cfg = configparser.ConfigParser()
    cfg.read_dict(builtin_defaults())
    for _, path in config_layers():
        if path.exists():
            cfg.read(str(path))
    global _HOST_OVERRIDE
    _HOST_OVERRIDE = cfg.get("general", "hostname", fallback="").strip()
    return cfg
