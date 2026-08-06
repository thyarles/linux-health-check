# Linux Health Check

Daily server health and security monitoring script. Runs all checks, saves an HTML report, and sends emails to two configurable groups: an **operations team** (daily, always) and a **managers/alerts group** (only on failures or security events).

Requires only **Python 3** (stdlib — no pip). Supports **RHEL 7+** (yum), **RHEL 8+** (dnf), and **Debian/Ubuntu** (apt-get).

---

## Files

| File | Description |
|------|-------------|
| `healthcheck.py` | Main script |
| `healthcheck.conf.example` | Config template — copy to `healthcheck.conf` and edit |

When deployed, the script creates two subdirectories next to itself:

```
/opt/healthcheck/
├── healthcheck.py
├── healthcheck.conf          ← per-server config (not versioned)
├── reports/                  ← saved HTML reports (one per run)
└── state/                    ← JSON snapshots for change detection
    ├── packages.json
    ├── ports.json
    ├── crontabs.json
    └── suid_files.json
```

---

## Deploy to a Server

```bash
# 1. Copy files to the server
mkdir -p /opt/healthcheck
scp scripts/healthcheck.py scripts/healthcheck.conf.example \
    root@yourserver:/opt/healthcheck/

# 2. Bootstrap: install dependencies and create directories
cd /opt/healthcheck
python3 healthcheck.py bootstrap

# 3. Configure: set SMTP, email recipients, and thresholds
cp healthcheck.conf.example healthcheck.conf
nano healthcheck.conf

# 4. Preview the report before any email is sent
python3 healthcheck.py report > /tmp/report.html
# Open /tmp/report.html in a browser to review

# 5. Install the daily cron job (default 07:00, or pass HH:MM)
python3 healthcheck.py crontab 07:00
crontab -l | grep healthcheck   # verify
```

Log output from cron goes to `/var/log/healthcheck.log`.

---

## Modes

```
python3 healthcheck.py [run]            Run checks + send emails (cron default)
python3 healthcheck.py report           Print HTML to stdout, no emails
python3 healthcheck.py bootstrap        Check/install system tools
python3 healthcheck.py crontab [HH:MM]  Install/update crontab entry
```

---

## Email Groups

| Group | Key in `healthcheck.conf` | When it receives email |
|-------|--------------------------|----------------------|
| Operations team | `daily_recipients` | Every day, regardless of status |
| Managers / on-call | `alert_recipients` | Only when overall status is **CAUTION** or **UNHEALTHY**, or when security events are detected |

---

## What Is Monitored

### System Health
- **System info** — hostname, OS, kernel, architecture, uptime, last boot, CPU model
- **CPU load** — per-core usage via `mpstat` (falls back to `/proc/stat`), load average with thresholds relative to CPU count
- **Memory** — RAM and swap usage %
- **Disk** — all mount points, usage %; CAUTION/UNHEALTHY thresholds configurable
- **Top processes** — top 10 by memory, top 10 by CPU, zombie process count
- **System services** — `systemctl` failed and stuck-activating units
- **Docker** — running/stopped containers and images (skipped if not installed)
- **Pending updates** — package count via yum/dnf/apt; security-specific count on Debian
- **Network I/O** — RX/TX bytes per interface, active connection summary

### Security
- **SSH brute force** — failed login attempts today, top attacking IPs
- **Successful SSH logins** — count for today
- **sudo usage** — commands executed today
- **fail2ban** — currently and historically banned IPs per jail
- **Listening ports** — full list; **change detection** alerts on new or removed ports since last run
- **Crontab changes** — scans `/etc/crontab`, `/etc/cron.d`, `/var/spool/cron`; alerts on new/removed entries
- **SUID files** — full scan via `find`; alerts on new SUID files since last run
- **Package changes** — installed/removed packages since last run
- **/etc modifications** — files changed in the last 24 hours (excludes noisy files like `mtab`, `resolv.conf`)
- **Log patterns** — searches system and auth logs for: OOM kills, disk I/O errors, kernel panics, segfaults, CPU machine checks, filesystem errors, SSH brute force markers
- **Rootkit indicators** — `rkhunter` output (if installed), known suspicious file paths, process PID visibility discrepancies

### Tooling Status
- Every report includes a **System Tools Status** section listing which monitoring tools are installed or missing, with the exact install command for the detected package manager.

---

## SMTP Configuration

```ini
[smtp]
host = relay.mpt.mp.br   # leave blank to use localhost (Postfix)
port = 25
use_tls = false
username =               # leave blank if no authentication required
password =
from = healthcheck@yourserver.mpt.mp.br
```

MPT relay (`relay.mpt.mp.br:25`) requires no authentication. If `host` is left blank, the script connects to `localhost:25` and relies on a local Postfix/sendmail installation.

---

## Thresholds (defaults)

| Metric | CAUTION | UNHEALTHY |
|--------|---------|-----------|
| CPU per core | 80% | 95% |
| Disk per mount | 90% | 95% |
| RAM | 80% | 95% |
| Load average | 1.0 × CPU count | 2.0 × CPU count |

All thresholds are configurable in `healthcheck.conf` under `[thresholds]`.

---

## First-Run Behaviour

State snapshots (ports, packages, SUID files, crontabs) are created on the first run. Change detection only activates from the **second run** onwards — no false positives on initial deployment.

---

## Requirements

- Python 3.6+ (stdlib only — no `pip install` required)
- `sysstat` package for `mpstat` (per-core CPU stats; falls back gracefully if absent)
- A working MTA on localhost **or** access to `relay.mpt.mp.br:25`
- Root or sudo for `bootstrap` auto-install and reading protected log files
