import configparser
import html as _htmllib
import pathlib
import re
import socket

from .models import OK, INFO, CAUTION, UNHEALTHY, Section
from .utils  import run, has, _fmt_bytes, read_os_release, pkg_manager, install_cmd
from .utils  import load_state, save_state, today_date_re, count_in_log


def check_system_info() -> Section:
    s = Section("System Information")
    osi = read_os_release()
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
    s.add("Last Boot",     boot or "unknown")
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
            if st == UNHEALTHY:
                s.alert(f"CPU {cpu_id} at {used:.0f}%")
            s.add(f"CPU {cpu_id}", f"{used:.1f}% used", st)
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
        s.add(label,
              f"{pct:.1f}% used  ({_fmt_bytes(used)} of {_fmt_bytes(total)}, {_fmt_bytes(free)} free)",
              st)
    return s


def check_disk(cfg: configparser.ConfigParser) -> Section:
    s = Section("Disk Usage")
    thr_c = cfg.getfloat("thresholds", "disk_caution",   fallback=90.0)
    thr_u = cfg.getfloat("thresholds", "disk_unhealthy", fallback=95.0)

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
        s.add(mount,
              f"{pct:.1f}% used  ({_fmt_bytes(used)} of {_fmt_bytes(size)}, {_fmt_bytes(avail)} free)",
              st)
    return s


def check_processes() -> Section:
    s = Section("Top Processes")

    def _ps_rows(sort_col: str, label_col: int, val_col: int, val_suffix: str, limit: int = 10):
        _, out, _ = run(f"ps aux --sort=-{sort_col} 2>/dev/null | awk 'NR>1' | head -{limit}")
        rows = []
        for line in out.splitlines():
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            pid  = parts[1]
            val  = parts[val_col]
            cmd  = parts[10][:60]
            rows.append((pid, val, cmd))
        return rows

    s.add("── Top 10 by Memory ──", "", INFO)
    for pid, pct, cmd in _ps_rows("%mem", 1, 3, "%MEM"):
        s.add(f"PID {pid}", f"{pct}% MEM  {cmd}", INFO)

    s.add("── Top 10 by CPU ──", "", INFO)
    for pid, pct, cmd in _ps_rows("%cpu", 1, 2, "%CPU"):
        s.add(f"PID {pid}", f"{pct}% CPU  {cmd}", INFO)

    _, zombie, _ = run("ps aux 2>/dev/null | awk '$8==\"Z\"' | wc -l")
    try:
        z = int(zombie)
        st = UNHEALTHY if z >= 5 else (CAUTION if z > 0 else OK)
        s.add("Zombie Processes", str(z), st)
        if z >= 5:
            s.alert(f"{z} zombie processes")
    except ValueError:
        pass

    return s


def check_services() -> Section:
    s = Section("System Services")

    if not has("systemctl"):
        s.add("systemd", "Not available on this system", INFO)
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

    _, out2, _ = run("systemctl list-units --state=activating --no-legend --no-pager 2>/dev/null | head -5")
    if out2:
        for line in out2.splitlines():
            parts = line.split(None, 1)
            if parts:
                s.add(parts[0], "stuck in activating state", CAUTION)

    return s


def check_docker() -> Section:
    s = Section("Docker Containers")

    if not has("docker"):
        s.add("Docker", "Not installed (optional)", INFO)
        s.need_tool("docker", rhel_pkg="docker-ce", deb_pkg="docker.io", optional=True)
        return s

    _, out, _ = run(
        "docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Image}}' 2>/dev/null",
        timeout=15,
    )
    if not out:
        s.add("Containers", "None", INFO)
        return s

    stopped = 0
    for line in out.splitlines():
        parts  = line.split("\t")
        name   = parts[0] if parts else "?"
        status = parts[1] if len(parts) > 1 else "?"
        image  = parts[2] if len(parts) > 2 else "?"
        is_up  = status.lower().startswith("up")
        if not is_up:
            stopped += 1
        s.add(_htmllib.escape(name), f"{_htmllib.escape(status)}  [{_htmllib.escape(image)}]",
              OK if is_up else CAUTION)

    if stopped > 0:
        s.alert(f"{stopped} stopped Docker container(s)", CAUTION)

    _, disk_out, _ = run("docker system df 2>/dev/null")
    if disk_out:
        s.add("── Docker Disk Usage ──", "", INFO)
        for line in disk_out.splitlines()[1:5]:
            parts = line.split(None, 1)
            if len(parts) == 2:
                s.add(parts[0], parts[1], INFO)

    return s


def check_updates() -> Section:
    s = Section("Pending Updates")
    pm = pkg_manager()

    if pm in ("dnf", "yum"):
        _, out, _ = run(
            f"{pm} check-update -q 2>/dev/null | grep -Ev '^$|^Last|^Loaded|^Loading|^Obsoleting'",
            timeout=90,
        )
        count = len([l for l in out.splitlines() if l.strip()])
        st = CAUTION if count > 20 else (INFO if count > 0 else OK)
        if count > 20:
            s.alert(f"{count} pending package updates", CAUTION)
        s.add("Pending Updates", str(count), st)

    elif pm == "apt-get":
        run("apt-get update -qq 2>/dev/null", timeout=90)
        _, out, _ = run("apt-get -s upgrade 2>/dev/null | grep -c '^Inst '")
        count = int(out) if out.isdigit() else 0
        st = CAUTION if count > 20 else (INFO if count > 0 else OK)
        if count > 20:
            s.alert(f"{count} pending package updates", CAUTION)
        s.add("Pending Updates", str(count), st)
        _, sec_out, _ = run("apt-get -s upgrade 2>/dev/null | grep -ci 'security'")
        sec = int(sec_out) if sec_out.isdigit() else 0
        if sec > 0:
            s.add("Security Updates", str(sec), CAUTION)
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

    s.add("── Recent Logins (last -20) ──", "", INFO)
    _, last_out, _ = run("last -n 20 2>/dev/null | head -20")
    for line in last_out.splitlines():
        if line.strip() and not line.startswith("wtmp") and not line.startswith("reboot"):
            s.add("", line[:90], INFO)

    _, root_out, _ = run("last -n 5 root 2>/dev/null | grep -v '^$\\|wtmp'")
    if root_out:
        s.add("── Recent Root Logins ──", "", INFO)
        for line in root_out.splitlines():
            if line.strip():
                s.add("root", line[:90], CAUTION)

    return s


def _find_auth_log() -> str:
    for p in ("/var/log/auth.log", "/var/log/secure"):
        if pathlib.Path(p).exists():
            return p
    return ""


def check_auth_security() -> Section:
    s = Section("SSH & Authentication")

    auth_log = _find_auth_log()

    n_failed = count_in_log(auth_log, "Failed password")
    st = UNHEALTHY if n_failed > 100 else (CAUTION if n_failed > 10 else (INFO if n_failed > 0 else OK))
    s.add("Failed SSH Attempts (today)", str(n_failed), st)
    if n_failed > 10:
        s.alert(f"{n_failed} failed SSH attempts today")

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
                          CAUTION if cnt > 10 else INFO)

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
        s.add("Auth Log", "Not found (/var/log/auth.log or /var/log/secure)", CAUTION)

    return s


def check_fail2ban() -> Section:
    s = Section("fail2ban")

    if not has("fail2ban-client"):
        s.add("fail2ban", "Not installed", INFO)
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
              CAUTION if cur > 0 else OK)

    if total_banned > 0:
        s.alert(f"fail2ban: {total_banned} IP(s) currently banned", CAUTION)

    return s


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

    current = sorted(set(raw.splitlines()))
    prev    = load_state("ports")

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

    s.add("── All Listening Ports ──", "", INFO)
    for line in current:
        s.add("", line, INFO)

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
                s.add("INSTALLED", p, CAUTION)
            if len(new_pkgs) > 20:
                s.add("...", f"and {len(new_pkgs) - 20} more", INFO)
            s.alert(f"{len(new_pkgs)} new package(s) installed", CAUTION)
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


def check_etc_changes() -> Section:
    s = Section("/etc Modifications (last 24h)")

    exclude = (
        "-not -name '*.swp' "
        "-not -name '*.tmp' "
        "-not -path '*/mtab' "
        "-not -path '*/adjtime' "
        "-not -path '*/ld.so.cache' "
        "-not -path '*/resolv.conf' "
    )
    _, out, _ = run(
        f"find /etc -maxdepth 3 -type f -mmin -1440 {exclude} 2>/dev/null | sort | head -40",
        timeout=30,
    )
    if out:
        for f in out.splitlines():
            f = f.strip()
            if not f:
                continue
            _, mtime, _ = run(f"stat -c '%y' {f} 2>/dev/null")
            s.add(_htmllib.escape(f), mtime[:19] if mtime else "", CAUTION)
            s.alert(f"/etc change: {f}", CAUTION)
    else:
        s.add("Changes", "No files modified in the last 24 hours", OK)

    return s


def check_log_patterns() -> Section:
    s = Section("Log Analysis")
    log_files = " ".join(
        f for f in ("/var/log/syslog", "/var/log/messages",
                    "/var/log/kern.log", "/var/log/dmesg")
        if pathlib.Path(f).exists()
    )
    auth_log = _find_auth_log()

    checks = [
        ("OOM Killer",          r"oom.kill|Out of memory",         log_files,  UNHEALTHY),
        ("Disk I/O Errors",     r"I/O error|EIO|blk_update_request", log_files, UNHEALTHY),
        ("Kernel Panic",        r"kernel panic|Kernel panic",      log_files,  UNHEALTHY),
        ("Segmentation Faults", r"segfault|SIGSEGV",               log_files,  CAUTION),
        ("CPU Machine Check",   r"machine check|MCE.*Bank",        log_files,  UNHEALTHY),
        ("Filesystem Errors",   r"EXT[234]-fs error|XFS.*error",   log_files,  UNHEALTHY),
        ("SSH Brute Force",     r"BREAK-IN ATTEMPT|Invalid user",  auth_log,   CAUTION),
        ("USB Device Added",    r"New USB device found",           log_files,  INFO),
        ("sudo Escalation",     r"sudo.*COMMAND",                  auth_log,   INFO),
    ]

    for label, pattern, sources, severity in checks:
        if not sources:
            continue
        count = 0
        for src in sources.split():
            if pathlib.Path(src).exists():
                _, cnt, _ = run(f"grep -ciE '{pattern}' {src} 2>/dev/null || echo 0")
                try:
                    count += int(cnt.splitlines()[0])
                except (ValueError, IndexError):
                    pass
        st = severity if count > 0 else OK
        s.add(label, f"{count} occurrence(s) in logs", st)
        if count > 0 and severity in (CAUTION, UNHEALTHY):
            s.alert(f"{label}: {count} occurrence(s)", severity)

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

    _, ps_pids,   _ = run("ps -eo pid --no-headers 2>/dev/null | sort -n")
    _, proc_pids, _ = run("ls /proc 2>/dev/null | grep -E '^[0-9]+$' | sort -n")
    hidden = set(proc_pids.splitlines()) - set(ps_pids.splitlines())
    hidden = {p for p in hidden if p.isdigit() and int(p) > 2}
    if len(hidden) > 5:
        s.add("Hidden Processes",
              f"{len(hidden)} PIDs in /proc not visible in ps", CAUTION)
        s.alert(f"{len(hidden)} potentially hidden processes", CAUTION)
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
            s.add(f"{tag}{tool}",
                  f"Not installed — run: {cmd}",
                  INFO if optional else CAUTION)

    return s
