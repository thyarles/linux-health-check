import configparser
import datetime
import html as _htmllib
import pathlib
import re
import socket

from .models import OK, INFO, CAUTION, UNHEALTHY, Section
from .utils  import run, has, _fmt_bytes, read_os_release, pkg_manager, install_cmd
from .utils  import load_state, save_state, today_date_re, count_in_log
from .utils  import count_in_log_today


# How many rows a purely-informational inventory is allowed to print.
_LIST_CAP = 10


def _delta_note(prev: dict, key: str, now_pct: float, min_move: float = 0.5) -> str:
    """Signed change since the previous run.

    A percentage on its own does not say whether you have days or hours. Noise
    below `min_move` is suppressed so the column stays empty unless it means
    something.
    """
    try:
        before = float(prev[key])
    except (KeyError, TypeError, ValueError):
        return ""
    diff = now_pct - before
    if abs(diff) < min_move:
        return ""          # silence is the message: nothing moved
    return f"{diff:+.1f} pts since last run"


def check_system_info() -> Section:
    s = Section("System Information")
    osi = read_os_release()
    s.add("Date",          datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    s.add("Hostname",      socket.getfqdn())
    s.add("OS",            osi.get("PRETTY_NAME", "unknown"))
    _, k, _  = run("uname -r")
    s.add("Kernel",        k or "unknown")
    _, arch, _ = run("uname -m")
    s.add("Architecture",  arch or "unknown")
    rc, up, _ = run("uptime -p 2>/dev/null")
    if rc != 0 or not up:
        _, up, _ = run(r"uptime | sed 's/.*up \([^,]*\),.*/\1/'")
    s.add("Uptime", up or "unknown")
    _, boot, _ = run("who -b | awk '{print $3, $4}'")
    # A reboot explains new PIDs, rewritten /etc files and reset counters all
    # at once, so say it plainly instead of leaving the reader to infer it.
    btime    = _boot_time()
    prev_bt  = load_state("boot_time")
    rebooted = bool(prev_bt) and btime and abs(float(prev_bt) - btime) > 60
    s.add("Last Boot", boot or "unknown",
          CAUTION if rebooted else OK,
          detail="host rebooted since the previous run" if rebooted else "")
    if rebooted:
        s.alert("Host rebooted since the previous run", CAUTION)
    if btime:
        save_state("boot_time", btime)
    _, ncpu, _ = run("nproc")
    s.add("CPU Cores",     ncpu or "unknown")
    _, model, _ = run("lscpu 2>/dev/null | grep 'Model name' | cut -d: -f2 | xargs")
    if model:
        s.add("CPU Model", model)
    return s


def check_cpu(cfg: configparser.ConfigParser) -> Section:
    s = Section("CPU Load")
    thr_c  = cfg.getfloat("thresholds", "cpu_caution",         fallback=80.0)
    thr_u  = cfg.getfloat("thresholds", "cpu_unhealthy",       fallback=95.0)
    mult_c = cfg.getfloat("thresholds", "load_caution_mult",   fallback=1.0)
    mult_u = cfg.getfloat("thresholds", "load_unhealthy_mult", fallback=2.0)

    _, nproc, _ = run("nproc")
    ncpus = int(nproc) if nproc.isdigit() else 1

    _, la, _ = run("awk '{print $1,$2,$3}' /proc/loadavg")
    if la:
        parts = la.split()
        try:
            l1, l5, l15 = float(parts[0]), float(parts[1]), float(parts[2])
            t_c, t_u = mult_c * ncpus, mult_u * ncpus
            st = UNHEALTHY if l1 >= t_u else (CAUTION if l1 >= t_c else OK)
            if st == UNHEALTHY:
                s.alert(f"Load avg {l1:.2f} exceeds {t_u:.1f} ({ncpus} CPUs × {mult_u})")
            s.add("Load Average (1/5/15 min)",
                  f"{l1:.2f} / {l5:.2f} / {l15:.2f}", st,
                  detail=f"Thresholds: caution ≥ {t_c:.1f}, unhealthy ≥ {t_u:.1f}")
        except (ValueError, IndexError):
            s.add("Load Average", la, INFO)

    if has("mpstat"):
        _, out, _ = run("mpstat -P ALL 1 1 2>/dev/null")
        # mpstat emits both a live snapshot and "Average:" lines; use a dict so
        # each cpu_id is recorded only once (last write = the Average: row).
        cpu_rows: dict = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            cpu_id = None
            for tok in parts[:4]:
                if tok == "all" or (tok.isdigit() and len(tok) <= 3):
                    cpu_id = tok
                    break
            if cpu_id is None:
                continue
            try:
                idle = float(parts[-1].replace(",", "."))
                if not (0.0 <= idle <= 100.0):
                    continue
            except ValueError:
                continue
            used = 100.0 - idle
            st = UNHEALTHY if used >= thr_u else (CAUTION if used >= thr_c else OK)
            cpu_rows[cpu_id] = (used, st)
        # On a 96-core box the per-core listing was 97 rows — a third of the
        # whole report — and 96 of them said "0.0% used". Summarise the fleet
        # of cores and name only the ones actually worth looking at.
        cores = {k: v for k, v in cpu_rows.items() if k != "all"}
        if "all" in cpu_rows:
            used, st = cpu_rows["all"]
            if st == UNHEALTHY:
                s.alert(f"CPU at {used:.0f}%")
            s.add("CPU (all cores)", f"{used:.1f}% used", st, meter=used)

        if cores:
            vals = sorted((v[0] for v in cores.values()), reverse=True)
            busy = [(cid, u, st) for cid, (u, st) in cores.items()
                    if st in (CAUTION, UNHEALTHY)]
            s.add("Cores", f"{len(cores)} total · busiest {vals[0]:.1f}% · "
                           f"median {vals[len(vals) // 2]:.1f}%", OK,
                  detail=f"{len(busy)} core(s) above the caution threshold"
                         if busy else "no core above the caution threshold")
            for cid, used, st in sorted(busy, key=lambda x: -x[1])[:10]:
                if st == UNHEALTHY:
                    s.alert(f"CPU {cid} at {used:.0f}%")
                s.add(f"CPU {cid}", f"{used:.1f}% used", st, meter=used)
            if len(busy) > 10:
                s.add("…", f"and {len(busy) - 10} more core(s) above threshold", INFO)
    else:
        _, stat, _ = run("grep '^cpu ' /proc/stat")
        if stat:
            vals = [int(x) for x in stat.split()[1:] if x.isdigit()]
            if len(vals) >= 4:
                idle  = vals[3]
                total = sum(vals)
                used  = (1 - idle / total) * 100 if total else 0
                st = UNHEALTHY if used >= thr_u else (CAUTION if used >= thr_c else OK)
                s.add("CPU (aggregate, approx.)", f"{used:.1f}% used", st,
                      detail="Install sysstat for per-core stats")
        s.need_tool("mpstat", rhel_pkg="sysstat", deb_pkg="sysstat")

    return s


def check_memory(cfg: configparser.ConfigParser) -> Section:
    s = Section("Memory Usage")
    thr_c = cfg.getfloat("thresholds", "ram_caution",   fallback=80.0)
    thr_u = cfg.getfloat("thresholds", "ram_unhealthy", fallback=95.0)
    prev    = load_state("memory_pct") or {}
    current: dict = {}

    _, out, _ = run("free -b")
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        label = parts[0].rstrip(":")
        if label not in ("Mem", "Swap"):
            continue
        try:
            total = int(parts[1])
            used  = int(parts[2])
            free  = int(parts[3]) if len(parts) > 3 else total - used
        except (IndexError, ValueError):
            continue
        pct = (used / total * 100) if total > 0 else 0.0
        st = UNHEALTHY if pct >= thr_u else (CAUTION if pct >= thr_c else OK)
        if st == UNHEALTHY and label == "Mem":
            s.alert(f"RAM at {pct:.0f}%")
        current[label] = round(pct, 1)
        s.add(label,
              f"{pct:.1f}% used  ({_fmt_bytes(used)} of {_fmt_bytes(total)}, {_fmt_bytes(free)} free)",
              st, meter=pct, delta=_delta_note(prev, label, pct))

    save_state("memory_pct", current)
    return s


def check_disk(cfg: configparser.ConfigParser) -> Section:
    s = Section("Disk Usage")
    thr_c = cfg.getfloat("thresholds", "disk_caution",   fallback=90.0)
    thr_u = cfg.getfloat("thresholds", "disk_unhealthy", fallback=95.0)
    prev    = load_state("disk_pct") or {}
    current: dict = {}

    _, out, _ = run("df -Pk | grep -Ev '^Filesystem|tmpfs|udev|devtmpfs|overlay|squashfs'")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        mount   = parts[5]
        if mount.startswith('/snap/') or mount.startswith('/var/snap/'):
            continue
        pct_str = parts[4].rstrip("%")
        try:
            pct   = float(pct_str)
            size  = int(parts[1]) * 1024
            used  = int(parts[2]) * 1024
            avail = int(parts[3]) * 1024
        except (ValueError, IndexError):
            continue
        st = UNHEALTHY if pct >= thr_u else (CAUTION if pct >= thr_c else OK)
        if st in (CAUTION, UNHEALTHY):
            s.alert(f"Disk {mount} at {pct:.0f}%", status=st)
        current[mount] = round(pct, 1)
        s.add(mount,
              f"{pct:.1f}% used  ({_fmt_bytes(used)} of {_fmt_bytes(size)}, {_fmt_bytes(avail)} free)",
              st, meter=pct, delta=_delta_note(prev, mount, pct))

    save_state("disk_pct", current)
    return s


def check_processes(cfg: configparser.ConfigParser) -> Section:
    s = Section("Top Processes")

    def _ps_rows(sort_col: str, label_col: int, val_col: int, val_suffix: str, limit: int = 5):
        _, out, _ = run(f"ps aux --sort=-{sort_col} 2>/dev/null | awk 'NR>1' | head -{limit}")
        rows = []
        for line in out.splitlines():
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            pid  = parts[1]
            val  = parts[val_col]
            cmd  = parts[10][:40]
            rows.append((pid, val, cmd))
        return rows

    s.add("── Top 5 by Memory ──", "", INFO)
    for pid, pct, cmd in _ps_rows("%mem", 1, 3, "%MEM"):
        s.add(f"PID {pid}", f"{pct}% MEM  {cmd}", INFO)

    s.add("── Top 5 by CPU ──", "", INFO)
    for pid, pct, cmd in _ps_rows("%cpu", 1, 2, "%CPU"):
        s.add(f"PID {pid}", f"{pct}% CPU  {cmd}", INFO)

    z_c = cfg.getint("thresholds", "zombie_caution",   fallback=10)
    z_u = cfg.getint("thresholds", "zombie_unhealthy", fallback=50)
    _, zombie, _ = run("ps aux 2>/dev/null | awk '$8==\"Z\"' | wc -l")
    try:
        z = int(zombie)
        # A handful of zombies is routine on a busy host and clears itself;
        # only a pile that a human must go clean up is worth a notification.
        st = UNHEALTHY if z >= z_u else (CAUTION if z >= z_c else (INFO if z else OK))
        s.add("Zombie Processes", str(z), st,
              detail=f"Thresholds: caution ≥ {z_c}, unhealthy ≥ {z_u}")
        if st in (CAUTION, UNHEALTHY):
            s.alert(f"{z} zombie processes", st)
    except ValueError:
        pass

    return s


def check_services() -> Section:
    s = Section("System Services")

    if not has("systemctl"):
        s.not_applicable("systemd not available on this system")
        return s

    _, out, _ = run("systemctl list-units --state=failed --no-legend --no-pager 2>/dev/null")
    failed = [l.split(None, 4) for l in out.splitlines() if l.strip()]
    if failed:
        for parts in failed:
            unit = parts[0] if parts else "unknown"
            desc = parts[4] if len(parts) > 4 else "failed"
            s.add(unit, desc, UNHEALTHY)
            s.alert(f"Service {unit} failed")
    else:
        s.add("Failed Services", "None", OK)

    # A unit caught mid-start is normal — the report just happened to run
    # during its startup. It is only "stuck" if it was still activating on the
    # previous run too, so compare against the last snapshot.
    _, out2, _ = run("systemctl list-units --state=activating --no-legend --no-pager 2>/dev/null | head -5")
    activating = [l.split(None, 1)[0] for l in out2.splitlines() if l.split()]
    was_activating = set(load_state("activating") or [])
    for unit in activating:
        if unit in was_activating:
            s.add(unit, "still activating since the previous run", CAUTION)
            s.alert(f"Service {unit} stuck activating", CAUTION)
        else:
            s.add(unit, "activating (starting up)", INFO)
    save_state("activating", activating)

    return s


_AGO_RE   = re.compile(r"(\d+|an?)\s+(second|minute|hour|day|week|month|year)s?\s+ago")
_AGO_HOURS = {"second": 1 / 3600, "minute": 1 / 60, "hour": 1, "day": 24,
              "week": 168, "month": 730, "year": 8760}


def _status_age_hours(status: str) -> float:
    """Age in hours from a Docker humanised status ('Exited (1) 3 days ago')."""
    m = _AGO_RE.search(status)
    if not m:
        return 0.0 if "less than" in status.lower() else 1e9
    qty = 1.0 if m.group(1) in ("a", "an") else float(m.group(1))
    return qty * _AGO_HOURS.get(m.group(2), 1.0)


def check_docker(cfg: configparser.ConfigParser) -> Section:
    s = Section("Docker Containers")

    if not has("docker"):
        s.not_applicable("Not installed (optional)")
        s.need_tool("docker", rhel_pkg="docker-ce", deb_pkg="docker.io", optional=True)
        return s

    _, out, _ = run(
        "docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}' 2>/dev/null",
        timeout=15,
    )
    if not out:
        s.add("Containers", "None", INFO)
        return s

    recent_h = cfg.getfloat("thresholds", "docker_recent_hours", fallback=24.0)
    problems, healthy, stopped = [], [], 0

    for line in out.splitlines():
        parts  = line.split("\t")
        name   = parts[0] if parts else "?"
        status = parts[1] if len(parts) > 1 else "?"
        image  = parts[2] if len(parts) > 2 else "?"
        low    = status.lower()

        # A container that exited cleanly, or that has been down for weeks, was
        # almost certainly stopped on purpose — batch jobs, k8s init pods, old
        # project stacks. Flagging those daily is what trains people to ignore
        # the report. Only a *fresh crash* or a *restart loop* needs attention.
        exit_m   = re.search(r"exited \((\d+)\)", low)
        exit_code = int(exit_m.group(1)) if exit_m else None

        if low.startswith("up"):
            st, note = (CAUTION, "container reports unhealthy") if "(unhealthy)" in low else (OK, "")
        elif low.startswith("restarting"):
            st, note = CAUTION, "restart loop"
        elif exit_code is not None and exit_code != 0:
            stopped += 1
            if _status_age_hours(status) <= recent_h:
                st, note = CAUTION, f"crashed within the last {recent_h:.0f}h"
            else:
                st, note = INFO, "stopped (long ago — assumed intentional)"
        else:            # exited(0), created, paused
            stopped += 1
            st, note = INFO, ""

        if st == CAUTION:
            problems.append(f"{name} ({note})")
            s.add(_htmllib.escape(name),
                  f"{_htmllib.escape(status)}  [{_htmllib.escape(image)}]", st,
                  detail=note)
        else:
            healthy.append((name, status, image, st))

    # Containers that are fine are a count, not a wall. On a host running
    # Kubernetes this section was 54 lines of pod names nobody reads.
    running = [h for h in healthy if h[3] == OK]
    s.add("Running", f"{len(running)} container(s) up", OK)
    for name, status, image, st in healthy[:_LIST_CAP]:
        s.add(_htmllib.escape(name),
              f"{_htmllib.escape(status)}  [{_htmllib.escape(image)}]", st)
    if len(healthy) > _LIST_CAP:
        s.add("…", f"and {len(healthy) - _LIST_CAP} more container(s)", INFO)
    s.add("Stopped/Exited Containers", str(stopped), INFO)
    if problems:
        s.alert("Docker: " + "; ".join(problems[:3])
                + (f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""),
                CAUTION)

    _, disk_out, _ = run("docker system df 2>/dev/null")
    if disk_out:
        s.add("── Docker Disk Usage ──", "", INFO)
        for line in disk_out.splitlines()[1:5]:
            parts = line.split(None, 1)
            if len(parts) == 2:
                s.add(parts[0], parts[1], INFO)

    return s


def check_updates(cfg: configparser.ConfigParser) -> Section:
    s = Section("Pending Updates")
    pm = pkg_manager()
    # Pending updates are planned maintenance, not an incident. Default both
    # thresholds to 0 (= report as INFO); set them in healthcheck.conf if you
    # want patching backlog to actually page people.
    thr_all = cfg.getint("thresholds", "updates_caution",          fallback=0)
    thr_sec = cfg.getint("thresholds", "security_updates_caution", fallback=0)

    if pm in ("dnf", "yum"):
        _, out, _ = run(
            f"{pm} check-update -q 2>/dev/null | grep -Ev '^$|^Last|^Loaded|^Loading|^Obsoleting'",
            timeout=90,
        )
        count = len([l for l in out.splitlines() if l.strip()])
        st = CAUTION if (thr_all and count >= thr_all) else (INFO if count > 0 else OK)
        if st == CAUTION:
            s.alert(f"{count} pending package updates", CAUTION)
        s.add("Pending Updates", str(count), st)

    elif pm == "apt-get":
        run("apt-get update -qq 2>/dev/null", timeout=90)
        _, out, _ = run("apt-get -s upgrade 2>/dev/null | grep -c '^Inst '")
        count = int(out) if out.isdigit() else 0
        st = CAUTION if (thr_all and count >= thr_all) else (INFO if count > 0 else OK)
        if st == CAUTION:
            s.alert(f"{count} pending package updates", CAUTION)
        s.add("Pending Updates", str(count), st)

        # The old query was `grep -ci security` over the whole simulation
        # output, which matched both the "Inst" and the "Conf" line of every
        # package — reporting exactly double the real number. Count the
        # Inst lines only, and only those coming from a -security archive.
        _, sec_out, _ = run(
            "apt-get -s upgrade 2>/dev/null | grep -c '^Inst .*-security'"
        )
        sec = int(sec_out) if sec_out.isdigit() else 0
        if sec > 0:
            sec_st = CAUTION if (thr_sec and sec >= thr_sec) else INFO
            s.add("Security Updates", str(sec), sec_st)
            if sec_st == CAUTION:
                s.alert(f"{sec} pending security updates", CAUTION)
    else:
        s.add("Package Manager", "Not detected", INFO)

    return s


def check_users() -> Section:
    s = Section("Users & Logins")

    _, who_out, _ = run("who")
    if who_out:
        for line in who_out.splitlines():
            s.add("Logged In", line.strip(), INFO)
    else:
        s.add("Logged In", "None", OK)

    s.add("── Recent Logins ──", "", INFO)
    _, last_out, _ = run("last -n 5 2>/dev/null | head -5")
    for line in last_out.splitlines():
        if line.strip() and not line.startswith("wtmp") and not line.startswith("reboot"):
            s.add("", line[:90], INFO)

    # `last root` returns history going back weeks. Marking all of it CAUTION
    # meant one root login in July kept the report yellow every day since.
    # Only a root session from today is current news.
    _, root_out, _ = run("last -n 5 root 2>/dev/null | grep -v '^$\\|wtmp'")
    if root_out:
        # `last` pads single-digit days with two spaces ("Aug  5"), so compare
        # on whitespace-normalised text rather than the raw strftime output.
        def _norm(t: str) -> str:
            return re.sub(r"\s+", " ", t).strip()

        today_tag = _norm(datetime.date.today().strftime("%b %d"))
        alt_tag   = _norm(datetime.date.today().strftime("%b ") +
                          str(datetime.date.today().day))
        todays = []
        s.add("── Recent Root Logins ──", "", INFO)
        for line in root_out.splitlines():
            if not line.strip():
                continue
            norm_line = _norm(line)
            is_today = today_tag in norm_line or alt_tag in norm_line
            s.add("root", line[:90], CAUTION if is_today else INFO)
            if is_today:
                todays.append(line.split()[1] if len(line.split()) > 1 else "?")
        if todays:
            s.alert(f"{len(todays)} root login(s) today", CAUTION)

    return s


def _find_auth_log() -> str:
    for p in ("/var/log/auth.log", "/var/log/secure"):
        if pathlib.Path(p).exists():
            return p
    return ""


def check_auth_security(cfg: configparser.ConfigParser) -> Section:
    s = Section("SSH & Authentication")

    auth_log = _find_auth_log()
    thr_c = cfg.getint("thresholds", "failed_ssh_caution",   fallback=50)
    thr_u = cfg.getint("thresholds", "failed_ssh_unhealthy", fallback=500)

    n_failed = count_in_log(auth_log, "Failed password")
    st = UNHEALTHY if n_failed >= thr_u else (CAUTION if n_failed >= thr_c else (INFO if n_failed > 0 else OK))
    s.add("Failed SSH Attempts (today)", str(n_failed), st,
          detail=f"Thresholds: caution ≥ {thr_c}, unhealthy ≥ {thr_u}")
    if st in (CAUTION, UNHEALTHY):
        s.alert(f"{n_failed} failed SSH attempts today", st)

    if auth_log and n_failed > 0:
        _, ips, _ = run(
            f"grep 'Failed password' {auth_log} 2>/dev/null | tail -1000 | "
            r"grep -oE 'from [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | "
            r"awk '{print $2}' | sort | uniq -c | sort -rn | head -5"
        )
        if ips:
            s.add("── Top Attacking IPs ──", "", INFO)
            for line in ips.splitlines():
                parts = line.split()
                if len(parts) == 2:
                    count_str, ip = parts
                    try:
                        cnt = int(count_str)
                    except ValueError:
                        cnt = 0
                    s.add(_htmllib.escape(ip), f"{cnt} attempts",
                          CAUTION if cnt >= thr_c else INFO)

    n_ok = count_in_log(auth_log, r"Accepted (password|publickey)")
    s.add("Successful SSH Logins (today)", str(n_ok), INFO)

    n_sudo = count_in_log(auth_log, r"sudo.*COMMAND")
    s.add("Sudo Commands (today)", str(n_sudo), INFO)
    if n_sudo > 0 and auth_log:
        _, sudo_detail, _ = run(
            f"grep -E 'sudo.*COMMAND' {auth_log} 2>/dev/null | "
            f"grep -E '{today_date_re()}' | tail -5"
        )
        if sudo_detail:
            s.add("── Recent sudo Commands ──", "", INFO)
            for line in sudo_detail.splitlines():
                s.add("", line[:100], INFO)

    if not auth_log:
        # Not a fault on journald-only systems, where journalctl is the source.
        s.add("Auth Log", "Not found — using journalctl" if has("journalctl")
              else "Not found (/var/log/auth.log or /var/log/secure)",
              INFO if has("journalctl") else CAUTION)

    return s


def check_fail2ban(cfg: configparser.ConfigParser) -> Section:
    s = Section("fail2ban")
    # A ban is fail2ban doing its job, not a problem to escalate. Only an
    # unusual spike is worth mentioning; 0 disables the check entirely.
    spike = cfg.getint("thresholds", "banned_ips_caution", fallback=0)

    if not has("fail2ban-client"):
        s.not_applicable("Not installed")
        s.need_tool("fail2ban-client", rhel_pkg="fail2ban", deb_pkg="fail2ban")
        return s

    _, out, _ = run("fail2ban-client status 2>/dev/null", timeout=10)
    if "Jail list" not in out:
        s.add("fail2ban", "Service not running or no output", CAUTION)
        return s

    m = re.search(r"Jail list:\s+(.+)", out)
    if not m:
        s.add("fail2ban", out[:120], INFO)
        return s

    jails        = [j.strip() for j in m.group(1).split(",") if j.strip()]
    total_banned = 0
    for jail in jails:
        _, jout, _ = run(f"fail2ban-client status {jail} 2>/dev/null", timeout=10)
        cur_m   = re.search(r"Currently banned:\s+(\d+)", jout)
        total_m = re.search(r"Total banned:\s+(\d+)",     jout)
        cur     = int(cur_m.group(1))   if cur_m   else 0
        total   = int(total_m.group(1)) if total_m else 0
        total_banned += cur
        s.add(f"Jail: {jail}", f"{cur} currently banned / {total} total",
              INFO if cur > 0 else OK)

    if spike and total_banned >= spike:
        s.add("Ban Volume", f"{total_banned} IPs banned (spike threshold {spike})", CAUTION)
        s.alert(f"fail2ban: unusual ban volume — {total_banned} IP(s) currently banned", CAUTION)

    return s


_PID_RE = re.compile(r",?\s*(pid|fd)=\d+")


def _port_key(line: str) -> str:
    """Normalise an ss/netstat line to address + process, dropping pid/fd."""
    line = _PID_RE.sub("", line)
    line = re.sub(r"\s+", " ", line).strip()
    return line.rstrip(",")


def check_ports() -> Section:
    s = Section("Listening Ports")

    if has("ss"):
        _, raw, _ = run("ss -tlnp 2>/dev/null | awk 'NR>1 {print $4, $6}'")
    elif has("netstat"):
        _, raw, _ = run("netstat -tlnp 2>/dev/null | awk 'NR>2 {print $4, $7}'")
        s.need_tool("ss", rhel_pkg="iproute", deb_pkg="iproute2", optional=True)
    else:
        s.add("ss / netstat", "Neither available", INFO)
        s.need_tool("ss", rhel_pkg="iproute", deb_pkg="iproute2")
        return s

    # The raw ss/netstat line carries pid= and fd=, which change every time a
    # service restarts. Comparing those made a routine restart look like a new
    # listening port every single day. Compare address + process name only.
    current = sorted({_port_key(l) for l in raw.splitlines() if l.strip()})
    prev    = load_state("ports")
    if prev is not None:
        prev = sorted({_port_key(l) for l in prev if str(l).strip()})

    if prev is not None:
        new_ports = [p for p in current if p not in prev]
        del_ports = [p for p in prev    if p not in current]
        if new_ports:
            s.add("── New Ports Since Last Run ──", "", INFO)
            for p in new_ports:
                s.add("NEW", p, CAUTION)
                s.alert(f"New listening port: {p.split()[0]}", CAUTION)
        if del_ports:
            s.add("── Ports No Longer Listening ──", "", INFO)
            for p in del_ports:
                s.add("GONE", p, INFO)
        if not new_ports and not del_ports:
            s.add("Port Changes", "None since last run", OK)
    else:
        s.add("Baseline", f"{len(current)} ports recorded (first run)", INFO)

    # The full inventory was 20+ identical rows every day. Changes are what
    # matter and they are listed above; keep the inventory as a count.
    s.add("── All Listening Ports ──", "", INFO)
    for line in current[:_LIST_CAP]:
        s.add("", line, INFO)
    if len(current) > _LIST_CAP:
        s.add("…", f"and {len(current) - _LIST_CAP} more listening socket(s)", INFO)

    save_state("ports", current)
    return s


def check_crontabs() -> Section:
    s = Section("Crontab Changes")

    lines = []
    cron_sources = [
        "/etc/crontab",
        "/etc/cron.d",
        "/var/spool/cron",
        "/var/spool/cron/crontabs",
    ]
    for src in cron_sources:
        p = pathlib.Path(src)
        try:
            targets = list(p.iterdir()) if p.is_dir() else ([p] if p.is_file() else [])
        except PermissionError:
            continue
        for t in targets:
            if t.is_file() and not t.name.startswith("."):
                _, ct, _ = run(f"grep -Ev '^#|^$' {t} 2>/dev/null")
                for l in ct.splitlines():
                    lines.append(f"{t}: {l.strip()}")

    current = sorted(set(lines))
    prev    = load_state("crontabs")

    if prev is not None:
        new_ct = [l for l in current if l not in prev]
        del_ct = [l for l in prev    if l not in current]
        if new_ct:
            s.add("── New Crontab Entries ──", "", INFO)
            for l in new_ct:
                s.add("NEW", l[:100], CAUTION)
                s.alert("New crontab entry detected", CAUTION)
        if del_ct:
            s.add("── Removed Crontab Entries ──", "", INFO)
            for l in del_ct:
                s.add("GONE", l[:100], INFO)
        if not new_ct and not del_ct:
            s.add("Crontab Changes", f"None ({len(current)} entries)", OK)
    else:
        s.add("Baseline", f"{len(current)} crontab entries recorded (first run)", INFO)

    save_state("crontabs", current)
    return s


def check_suid_files() -> Section:
    s = Section("SUID Files")

    _, out, _ = run("find / -xdev -perm /4000 -type f 2>/dev/null | sort", timeout=120)
    current = sorted(out.splitlines())
    prev    = load_state("suid_files")

    if prev is not None:
        new_suid = [f for f in current if f not in prev]
        del_suid = [f for f in prev    if f not in current]
        if new_suid:
            s.add("── New SUID Files ──", "", INFO)
            for f in new_suid:
                s.add("NEW", f, UNHEALTHY)
                s.alert(f"New SUID file: {f}")
        if del_suid:
            s.add("── Removed SUID Files ──", "", INFO)
            for f in del_suid:
                s.add("GONE", f, INFO)
        if not new_suid and not del_suid:
            s.add("SUID Changes", f"None ({len(current)} files, unchanged)", OK)
    else:
        s.add("Baseline", f"{len(current)} SUID files recorded (first run)", INFO)

    save_state("suid_files", current)
    return s


def check_package_changes() -> Section:
    s = Section("Package Changes")
    pm = pkg_manager()

    if pm in ("dnf", "yum"):
        _, pkg_list, _ = run(
            "rpm -qa --qf '%{NAME}-%{VERSION}-%{RELEASE}\n' 2>/dev/null | sort"
        )
    elif pm == "apt-get":
        _, pkg_list, _ = run(
            "dpkg -l 2>/dev/null | awk '/^ii/{print $2\"-\"$3}' | sort"
        )
    else:
        s.add("Package Manager", "Not detected", INFO)
        return s

    current = sorted(set(pkg_list.splitlines())) if pkg_list else []
    prev    = load_state("packages")

    if prev is not None:
        new_pkgs = [p for p in current if p not in prev]
        del_pkgs = [p for p in prev    if p not in current]
        if new_pkgs:
            s.add("── Newly Installed ──", "", INFO)
            for p in new_pkgs[:20]:
                s.add("INSTALLED", p, INFO)
            if len(new_pkgs) > 20:
                s.add("...", f"and {len(new_pkgs) - 20} more", INFO)
            # Package churn is expected on any host with unattended-upgrades.
            # Keep it in the report as an audit trail, but do not page anyone.
        if del_pkgs:
            s.add("── Removed ──", "", INFO)
            for p in del_pkgs[:20]:
                s.add("REMOVED", p, INFO)
            if len(del_pkgs) > 20:
                s.add("...", f"and {len(del_pkgs) - 20} more", INFO)
        if not new_pkgs and not del_pkgs:
            s.add("Package Changes", f"None ({len(current)} packages)", OK)
    else:
        s.add("Baseline", f"{len(current)} packages recorded (first run)", INFO)

    save_state("packages", current)

    if pm in ("dnf", "yum"):
        _, hist, _ = run(f"{pm} history list 2>/dev/null | head -6")
        if hist:
            s.add("── Recent Transaction History ──", "", INFO)
            for line in hist.splitlines()[:5]:
                if line.strip():
                    s.add("", line[:100], INFO)

    return s


# Files under /etc whose modification actually means something for security.
# Everything else in /etc changes constantly through normal package activity.
_SENSITIVE_ETC = (
    "/etc/passwd", "/etc/shadow", "/etc/group", "/etc/gshadow",
    "/etc/sudoers", "/etc/sudoers.d/", "/etc/ssh/sshd_config",
    "/etc/ssh/ssh_config", "/etc/pam.d/", "/etc/crontab", "/etc/cron.",
    "/etc/fstab", "/etc/hosts.allow", "/etc/hosts.deny",
    "/etc/sysctl.conf", "/etc/selinux/config", "/etc/security/",
)


def _boot_time() -> float:
    """Epoch seconds of the last boot, or 0 if unavailable."""
    try:
        for line in pathlib.Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except Exception:
        pass
    return 0.0


def check_etc_changes() -> Section:
    s = Section("/etc Modifications (last 24h)")

    exclude = (
        "-not -name '*.swp' "
        "-not -name '*.tmp' "
        "-not -name '*.dpkg-*' "
        "-not -name '*.rpmnew' "
        "-not -name '*.rpmsave' "
        "-not -path '*/mtab' "
        "-not -path '*/adjtime' "
        "-not -path '*/ld.so.cache' "
        "-not -path '*/resolv.conf' "
        "-not -path '*/machine-id' "
        "-not -path '*/.pwd.lock' "
        "-not -path '*/blkid.tab*' "
        "-not -path '*/network/interfaces.d/*' "
    )
    _, out, _ = run(
        f"find /etc -maxdepth 3 -type f -mmin -1440 {exclude} 2>/dev/null | sort | head -40",
        timeout=30,
    )
    if not out:
        s.add("Changes", "No files modified in the last 24 hours", OK)
        return s

    # Files rewritten by the init/cloud-init/WSL machinery within a minute of
    # boot (hostname, hosts, timezone…) are a side effect of rebooting, not an
    # edit someone made. Treating them as CAUTION made every reboot look like
    # a security event.
    btime     = _boot_time()
    sensitive = []
    for f in out.splitlines():
        f = f.strip()
        if not f:
            continue
        _, mtime, _   = run(f"stat -c '%y' {f} 2>/dev/null")
        _, mepoch, _  = run(f"stat -c '%Y' {f} 2>/dev/null")
        try:
            at_boot = btime > 0 and abs(float(mepoch) - btime) <= 60
        except ValueError:
            at_boot = False

        is_sensitive = any(f == pfx or f.startswith(pfx) for pfx in _SENSITIVE_ETC)
        if is_sensitive:
            st, note = CAUTION, "security-relevant file"
            sensitive.append(f)
        elif at_boot:
            st, note = INFO, "rewritten at boot"
        else:
            st, note = INFO, ""
        s.add(_htmllib.escape(f), mtime[:19] if mtime else "", st, detail=note)

    if sensitive:
        # One alert for the whole set — not one per file, which used to bury
        # the genuinely interesting lines under a wall of routine ones.
        s.alert("Security-relevant /etc change: " + ", ".join(sensitive[:3])
                + (f" (+{len(sensitive) - 3} more)" if len(sensitive) > 3 else ""),
                CAUTION)
    else:
        s.add("Security-Relevant Changes", "None", OK)

    return s


def _log_sources() -> str:
    """Space-separated list of the system log files present on this host."""
    return " ".join(
        f for f in ("/var/log/syslog", "/var/log/messages",
                    "/var/log/kern.log", "/var/log/dmesg")
        if pathlib.Path(f).exists()
    )


def check_log_patterns(cfg: configparser.ConfigParser) -> Section:
    s = Section("Log Analysis (today)")
    log_files = _log_sources()
    auth_log  = _find_auth_log()

    ssh_thr = cfg.getint("thresholds", "failed_ssh_caution", fallback=50)

    # (label, pattern, sources, severity, min_count_to_escalate)
    # min_count exists because a single segfault of a desktop helper is not an
    # incident, while a single kernel panic obviously is.
    checks = [
        ("OOM Killer",          r"oom.kill|Out of memory",           log_files, CAUTION,   1),
        ("Disk I/O Errors",     r"I/O error|blk_update_request",     log_files, CAUTION,   1),
        ("Kernel Panic",        r"kernel panic",                     log_files, UNHEALTHY, 1),
        ("Segmentation Faults", r"segfault|SIGSEGV",                 log_files, CAUTION,  10),
        ("CPU Machine Check",   r"machine check|MCE.*Bank",          log_files, CAUTION,   1),
        ("Filesystem Errors",   r"EXT[234]-fs error|XFS.*error",     log_files, CAUTION,   1),
        ("SSH Brute Force",     r"BREAK-IN ATTEMPT|Invalid user",    auth_log,  CAUTION, ssh_thr),
        ("USB Device Added",    r"New USB device found",             log_files, INFO,      0),
        ("sudo Escalation",     r"sudo.*COMMAND",                    auth_log,  INFO,      0),
    ]

    for label, pattern, sources, severity, min_count in checks:
        if not sources:
            continue
        # Scoped to today. The previous version grepped the entire log file, so
        # one bad night kept the report yellow until logrotate ran — and the
        # counts silently reset when it did.
        count, sample_lines = count_in_log_today(sources, pattern)

        escalate = (severity in (CAUTION, UNHEALTHY)
                    and min_count > 0 and count >= min_count)
        st = severity if escalate else (INFO if count > 0 else OK)
        detail = (f"escalates at ≥ {min_count} today"
                  if severity in (CAUTION, UNHEALTHY) and min_count > 0 else "")
        s.add(label, f"{count} occurrence(s) today", st, detail=detail)

        if escalate:
            s.alert(f"{label}: {count} occurrence(s) today", severity)
            if sample_lines:
                s.add("── Recent entries ──", "", INFO)
                for line in sample_lines:
                    s.add("", line[:120], severity)

    return s


def check_rootkit() -> Section:
    s = Section("Rootkit Indicators")

    if has("rkhunter"):
        _, out, _ = run(
            "rkhunter --check --sk --rwo 2>/dev/null | head -30", timeout=120
        )
        warnings = [l.strip() for l in out.splitlines() if re.search(r"warn|Warning", l, re.I)]
        if warnings:
            for w in warnings[:10]:
                s.add("rkhunter", w[:100], CAUTION)
                s.alert("rkhunter warning", CAUTION)
        else:
            s.add("rkhunter", "No warnings", OK)
    else:
        s.add("rkhunter", "Not installed (optional)", INFO)
        s.need_tool("rkhunter", rhel_pkg="rkhunter", deb_pkg="rkhunter", optional=True)

    suspicious = [
        "/usr/lib/.libc.so", "/usr/lib/.so", "/lib/.so",
        "/usr/bin/.sniffer",  "/usr/bin/bsd-port",
        "/usr/bin/sshd1",     "/usr/bin/rsyncd",
        "/dev/.hdd",          "/dev/.udev",
        "/tmp/.ICE-unix/.X0-lock",
    ]
    found = [p for p in suspicious if pathlib.Path(p).exists()]
    if found:
        for p in found:
            s.add("Suspicious Path", p, UNHEALTHY)
            s.alert(f"Suspicious file/dir found: {p}")
    else:
        s.add("Known Rootkit Paths", "None found", OK)

    # Comparing a single /proc listing against a single `ps` run is a race:
    # any process that starts or exits between the two commands looks hidden.
    # On a busy host that produced dozens of phantom "hidden processes" every
    # day. Bracket the ps call with two /proc reads and keep only PIDs that
    # were present in BOTH — a real hidden process persists, a race artifact
    # does not. Survivors are then re-checked individually.
    _, proc_before, _ = run("ls /proc 2>/dev/null | grep -E '^[0-9]+$'")
    _, ps_pids,     _ = run("ps -eo pid --no-headers 2>/dev/null")
    _, proc_after,  _ = run("ls /proc 2>/dev/null | grep -E '^[0-9]+$'")

    stable = set(proc_before.split()) & set(proc_after.split())
    hidden = {p for p in stable - set(ps_pids.split()) if p.isdigit() and int(p) > 2}
    if hidden:
        _, ps_again, _ = run("ps -eo pid --no-headers 2>/dev/null")
        hidden -= set(ps_again.split())
        hidden = {p for p in hidden if pathlib.Path(f"/proc/{p}").exists()}

    if hidden:
        s.add("Hidden Processes",
              f"{len(hidden)} PID(s) in /proc not visible in ps: "
              f"{', '.join(sorted(hidden)[:10])}", CAUTION)
        s.alert(f"{len(hidden)} potentially hidden process(es)", CAUTION)
    else:
        s.add("Process Visibility", "OK", OK)

    return s


def check_network_io() -> Section:
    s = Section("Network I/O")

    _, out, _ = run("cat /proc/net/dev 2>/dev/null")
    for line in out.splitlines()[2:]:  # skip 2-line header
        parts = line.split()
        if len(parts) < 10:
            continue
        iface = parts[0].rstrip(":")
        if iface == "lo":
            continue
        try:
            rx = int(parts[1])
            tx = int(parts[9])
        except (ValueError, IndexError):
            continue
        s.add(iface, f"RX {_fmt_bytes(rx)}  TX {_fmt_bytes(tx)}", INFO)

    if has("ss"):
        _, conn, _ = run("ss -s 2>/dev/null | head -4")
        if conn:
            s.add("── Connection Summary ──", "", INFO)
            for line in conn.splitlines():
                if line.strip():
                    s.add("", line.strip(), INFO)

    return s


_TOOLS = [
    ("mpstat",          "sysstat",   "sysstat",   False),
    ("ss",              "iproute",   "iproute2",  False),
    ("fail2ban-client", "fail2ban",  "fail2ban",  True),
    ("rkhunter",        "rkhunter",  "rkhunter",  True),
    ("docker",          "docker-ce", "docker.io", True),
    ("postfix",         "postfix",   "postfix",   False),
]


def check_tools() -> Section:
    s = Section("System Tools Status")

    for tool, rhel_pkg, deb_pkg, optional in _TOOLS:
        installed = has(tool)
        tag  = "[optional] " if optional else ""
        if installed:
            s.add(f"{tag}{tool}", "Installed", OK)
        else:
            cmd = install_cmd(tool, rhel_pkg, deb_pkg)
            # Never CAUTION: a tool that was missing yesterday is missing today
            # for the same reason, so it can only ever be permanent noise.
            # `healthcheck.py bootstrap` is the way to act on this.
            s.add(f"{tag}{tool}", f"Not installed — run: {cmd}", INFO,
                  detail="Reduces coverage of some checks" if not optional else "")

    return s
