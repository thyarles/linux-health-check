# Linux Health Check

Daily server health and security monitoring script. Runs all checks, saves a report, and mails **two audiences that are asked two different questions**:

- **`daily_recipients`** — a small selected group. Gets a message on every run, including "all clear". This is the *the monitoring itself is alive* signal.
- **`alert_recipients`** — the broad list. Gets a message **only when the run finds something new or something that got worse**. Subject is tagged `[ACTION]`.

A condition that has already been reported does not notify the broad list again until it clears, worsens, or hits the reminder interval. That is what keeps the word CAUTION meaningful: if it reaches everyone, something actually changed.

Requires only **Python 3** (stdlib — no pip). Supports **RHEL 7+** (yum), **RHEL 8+** (dnf), and **Debian/Ubuntu** (apt-get).

On old hosts with no usable `python3` at all — RHEL 7.3 ships Python 2.7.5 and
nothing else — [`install.sh`](install.sh) sets up a private interpreter and the
whole thing in one command. See [Quick Install](#quick-install).

---

## Files

| File | Description | Ships to server |
|------|-------------|-----------------|
| `healthcheck.py` | Entry point | yes |
| `hc/` | The check, report, alert and mail modules | yes |
| `healthcheck.conf.example` | Config template — copy to `healthcheck.conf` and edit | yes |
| `install.sh` | One-command installer: private Python + tagged release + cron | run on server |
| `pyproject.toml`, `tests/`, `uv.lock`, `Makefile` | Dev toolchain only | **no** |
| `.github/workflows/`, `scripts/set-version.sh` | CI and the release/version automation | **no** |

When deployed, the script creates two subdirectories next to itself:

```
/opt/healthcheck/
├── healthcheck.py
├── hc/                       ← the package healthcheck.py imports
├── healthcheck.conf          ← per-server config (not versioned)
├── .installed-version        ← tag installed by install.sh
├── reports/                  ← saved HTML reports (one per run)
└── state/                    ← JSON snapshots for change detection
    ├── packages.json
    ├── ports.json
    ├── crontabs.json
    ├── suid_files.json
    ├── activating.json
    ├── k8s_pod_restarts.json  ← Kubernetes, when the host is a cluster node
    ├── k8s_degraded_pods.json
    ├── k8s_cordoned.json
    ├── k8s_pending_pvcs.json
    └── alerts.json            ← which conditions have already been notified
```

---

## Quick Install

For a server that has no usable `python3`, or any server where you just want the
current release running. Needs no `git` and no repositories — it installs a
private Miniconda under `/root`, so the system Python that `yum` depends on is
never touched.

```bash
# the current release
curl -fsSL https://raw.githubusercontent.com/thyarles/linux-health-check/main/install.sh | bash

# any other tag
curl -fsSL https://raw.githubusercontent.com/thyarles/linux-health-check/main/install.sh | bash -s -- v2.0.1
```

It installs Miniconda to `/root/miniconda3`, downloads the tagged release
tarball to `/root/linux-health-check`, writes `healthcheck.conf` from the
example, installs the cron entry, and then **verifies that the entry it just
wrote actually points at the private interpreter** before reporting success.

With no tag argument it installs the release pinned as `DEFAULT_TAG` at the top
of `install.sh`. Because `install.sh` is always fetched from `main`, that one
line is what "current" means. It is bumped for you: the
[release workflow](#releases) rewrites it and commits it to `main` as part of
cutting each tag, so `main` and the newest release can never disagree.

It is pinned rather than resolved from the GitHub API on purpose: the
unauthenticated API allows 60 calls per hour *per IP*, so one shared office NAT
exhausts the quota and every install behind it fails with `HTTP 403: rate limit
exceeded`. A pinned tag has no such failure mode.

**Re-running it upgrades in place.** `healthcheck.conf`, `state/` and `reports/`
are never in the release archive, so they survive untouched — your SMTP
credentials and the change-detection baselines are safe. `hc/` is purged before
the new copy so a module deleted upstream cannot linger and get imported. The
installed tag is recorded in `.installed-version`.

| Env var | Default | Purpose |
|---------|---------|---------|
| `REPO_TAG` | `DEFAULT_TAG` in `install.sh` | Version to install (or pass as the first argument) |
| `APP_DIR` | `/root/linux-health-check` | Where the code lands |
| `CONDA_PREFIX_DIR` | `/root/miniconda3` | Where the private Python lands |
| `MAIL_DOMAIN` | `domain.com` | Replaces `domain.com` in the generated config |
| `CRON_TIME` | `07:00` | Daily run time |
| `REPO_SLUG` | `thyarles/linux-health-check` | Source repo |
| `DOWNLOADER` | auto | Force `curl` or `wget` when the other is broken |

Needs `curl` or `wget` and `tar` — no `git`, and no GitHub API. Tags before
`v2.0.1` hardcode `/usr/bin/python3` and will be rejected by the verification
step.

On very old hosts curl may exist but be unusable: builds that offer only TLS 1.0
fail against GitHub with `curl: (35) Peer reports incompatible or unsupported
protocol version`. The installer treats a curl failure as a reason to retry with
wget rather than to abort, so those hosts install unattended. `DOWNLOADER=wget`
skips curl entirely.

---

## Deploy to a Server

If you prefer a custom layout, or the server already has a working `python3`,
the manual route still applies.

```bash
# 1. Copy the runtime files to the server (healthcheck.py needs the hc/ package).
#    `make deploy HOST=root@yourserver` does this same step for you.
ssh root@yourserver mkdir -p /opt/healthcheck
scp -r healthcheck.py hc healthcheck.conf.example \
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
python3 healthcheck.py text             Print text report to stdout, no emails
python3 healthcheck.py bootstrap        Check/install system tools
python3 healthcheck.py crontab [HH:MM]  Install/update crontab entry
```

`report` and `text` are read-only: they never send email and never touch the
state snapshots, so previewing a report cannot consume the change-detection
baselines the scheduled run depends on.

### Which Python the cron entry runs

`crontab` writes the interpreter path into the cron line. It resolves, in order:

1. `HEALTHCHECK_PYTHON` in the environment
2. `python` under `[crontab]` in `healthcheck.conf`
3. **`sys.executable`** — whichever Python you invoked `healthcheck.py` with
4. `python3` on `PATH`

Rule 3 is the one that matters: `/root/miniconda3/bin/python healthcheck.py
crontab` installs a line that runs under *that* interpreter, with no
configuration. Cron runs with a minimal environment and often no useful `PATH`,
so a hardcoded `python3` is exactly the kind of entry that fails silently every
night on a host where `/usr/bin/python3` does not exist.

---

## Email Groups

| Group | Key in `healthcheck.conf` | When it receives email | Subject looks like |
|-------|--------------------------|------------------------|--------------------|
| Selected group (ops) | `daily_recipients` | **Every run**, whatever the status | `[daily] ✓ OK · host · date — all clear`<br>`[daily] ⚠ CAUTION (no new issues) · host · date — 3 known, unchanged` |
| Everyone | `alert_recipients` | **Only on new or worsened findings** | `[ACTION] ⚠ CAUTION · host · date — Disk /var at 91%` |

When an alert goes out it is sent as **one** message addressed to both lists, so
nobody receives the same report twice.

### Why a report can be CAUTION without anyone being alerted

The report is a *snapshot*; a notification is an *interruption*. A disk that has
been at 91% for three weeks is a true CAUTION in every snapshot, but it is only
worth interrupting a broad audience about once. `state/alerts.json` remembers
which conditions have already been notified, so each run answers a second
question beyond "what is the status?": *"is there anything here these people
have not already been told?"*

Every report opens with a triage block splitting the findings into **new**,
**got worse**, **still open (reminder)**, **known/unchanged — no action implied**,
and **cleared since last run**, followed by an **At a glance** grid giving every
section's status in one screen.

```ini
[alerts]
# caution | unhealthy
notify_all_on          = caution
# one nudge a week while a CAUTION stays open
remind_caution_hours   = 168
# UNHEALTHY is chased daily
remind_unhealthy_hours = 24
# clear this long and a recurrence counts as new again
forget_after_hours     = 72
```

**`forget_after_hours` must be comfortably longer than your run interval.** With
a daily cron the default 72h is fine; if you run the check weekly, raise it, or
every run will look like a brand new problem and page everyone.

Alerts are only recorded as notified **after the relay accepts the message**. If
the SMTP send fails, the next run treats them as new and tries again.

---

## Report Layout

The report is built around the fact that on a healthy host **roughly 1% of its
rows are things anyone should act on**. Everything below serves finding that 1%:

- **Worst first.** Sections are ordered by severity, so a problem is never on
  page six behind nine green panels.
- **Colour only where it means something.** OK and INFO rows are neutral; only
  CAUTION and UNHEALTHY rows are tinted. Painting all 163 healthy rows green is
  what made the four amber ones invisible.
- **Status is never colour alone.** Every status ships as a chip carrying a
  coloured dot *and* the word, which also covers colour-blind readers and
  clients that strip colour. Status text is a darker ink than the status hue so
  every label clears WCAG AA (amber at text size on white is 1.79:1 — the pale
  chip plus dark ink gets it to 5.4:1).
- **Bounded width.** The text report fits in 78 columns, values wrapping under
  themselves. It previously declared 76 and emitted 155 whenever a Kubernetes
  container name appeared, so mail clients re-wrapped it into a mess.
- **Summaries instead of inventories.** Per-core CPU is a summary plus only the
  cores over threshold — on a 96-core host that listing alone was 29% of the
  report. Purely informational lists are capped with an "and N more" line. The
  one exception is the listening-socket inventory, which is always listed in
  full: it is the security-relevant record of what the host exposes, and
  loopback filtering already keeps it short.
- **Meters and deltas.** Disk and memory rows carry a usage bar and their change
  since the previous run (`+3.0 pts since last run`), because a bare percentage
  does not tell you whether you have days or hours.
- **Absent subsystems collapse.** No Docker or fail2ban on the host means one
  line at the end, not an empty panel each.
- **A reboot is called out** — it explains a burst of new PIDs, rewritten `/etc`
  files and reset counters all at once.

Both renderings carry a legend. The HTML is **light-only on purpose**: every
element sets its own background inline, and the document declares
`color-scheme: only light` to opt out of client-side auto-darkening (Chrome
auto-dark, iOS Mail, Outlook).

An earlier version did ship a `prefers-color-scheme: dark` block. It darkened
the surfaces but could not reach the colours set inline on every row, so on a
dark-mode client the section names kept their dark ink on a now-dark background
and became unreadable. Half a dark theme is worse than none.

---

## What Is Monitored

### System Health
- **System info** — hostname, OS, kernel, architecture, uptime, last boot, CPU model
- **CPU load** — per-core usage via `mpstat` (falls back to `/proc/stat`), load average with thresholds relative to CPU count
- **Memory** — RAM and swap usage %
- **Disk** — all mount points, usage %; CAUTION/UNHEALTHY thresholds configurable
- **Top processes** — top 10 by memory, top 10 by CPU, zombie count (thresholds configurable; a few zombies are routine)
- **System services** — `systemctl` failed units; a unit is only "stuck activating" if it was still activating on the previous run
- **Docker** — containers; a *fresh* non-zero exit or a restart loop is CAUTION, a container stopped long ago is INFO (assumed deliberate), and `Up (unhealthy)` is flagged
- **Kubernetes** — appears only on a host that has **both** `kubectl` and a readable kubeconfig, so a machine that will never run Kubernetes shows nothing. Node Ready/cordon state and pressure conditions, unhealthy pods, unbound PVCs, aggregated Warning events, and a container image count via `crictl`. Everything is filtered for the noise a cluster generates by design: `Completed` helm/CronJob pods, month-old `Evicted` objects, PVCs that are `Pending` because their storage class is `WaitForFirstConsumer`, and pods that happened to be restarting when cron fired are all INFO. A pod's restart *count* is ignored — only growth **since the last run** raises CAUTION. Discovery searches absolute paths (`/var/lib/rancher/rke2/bin`, `/usr/local/bin`, …) as well as `$PATH`, because cron runs with `PATH=/usr/bin:/bin` and never sees the `export PATH=...` that makes `kubectl` work in your shell
- **Pending updates** — package count via yum/dnf/apt, plus a correct Debian security count. Reported as INFO by default: patching backlog is planned maintenance, not an incident. Set `updates_caution` / `security_updates_caution` to make it escalate.
- **Network I/O** — RX/TX bytes per interface, active connection summary

### Security
- **SSH brute force** — failed login attempts today, top attacking IPs (thresholds configurable; internet-facing hosts see a constant background level)
- **Successful SSH logins** — count for today
- **sudo usage** — commands executed today
- **fail2ban** — banned IPs per jail, reported as INFO because a ban is fail2ban *working*; set `banned_ips_caution` to flag an unusual spike
- **Listening sockets** — **TCP and UDP**, with **change detection** comparing protocol + address + process name only, so a service restart (new PID) is not mistaken for a new port. Sockets bound to loopback (`127.x`, `::1`, including a `%lo` zone id) are hidden by default: nothing off the host can reach them, and applications churn through random high ports on `127.0.0.1` all day. A wildcard bind (`*`, `0.0.0.0`, `::`) is treated as reachable, not local. Set `list_local_ports = true` to list them too
- **Crontab changes** — scans `/etc/crontab`, `/etc/cron.d`, `/var/spool/cron`; alerts on new/removed entries
- **SUID files** — full scan via `find`; alerts on new SUID files since last run
- **Package changes** — installed/removed packages since last run, as an audit trail (INFO — routine on any host with unattended-upgrades)
- **/etc modifications** — files changed in the last 24 hours. Only *security-relevant* paths (`passwd`, `shadow`, `sudoers`, `ssh`, `pam.d`, `fstab`, …) raise CAUTION, and as one grouped alert rather than one per file. Files rewritten within a minute of boot are recognised as boot artefacts. Backup agents that write timestamped files into `/etc` are skipped via `etc_ignore` — CommVault drops a new `.zst` into its registry backup directory every 90 minutes, and each one looked like a fresh configuration change.
- **Log patterns** — OOM kills, disk I/O errors, kernel panics, segfaults, CPU machine checks, filesystem errors, SSH brute force markers — **scoped to today**, each with its own escalation count (one segfault is not an incident; one kernel panic is)
- **Rootkit indicators** — `rkhunter` output (if installed), known suspicious file paths, and hidden-process detection that brackets `ps` with two `/proc` reads so that processes merely starting or exiting are not reported as hidden

### Tooling Status
- Every report includes a **System Tools Status** section listing the tools it relies on, with the exact install command for the detected package manager. Reported as INFO: a tool that was missing yesterday is missing today for the same reason, so it could only ever be permanent noise. Act on it with `healthcheck.py bootstrap`.
- **Optional** tools (`docker`, `kubectl`, `crictl`, `fail2ban`, `rkhunter`) are listed **only when they are installed**. A plain web server has no Docker and never will, so telling it to `dnf install docker-ce` every morning — or to install `kubectl` — is advice that can only ever be noise. Required tools (`mpstat`, `ss`, `postfix`) still show their install command, because those genuinely reduce coverage of checks the host *is* running.

---

## Check Options

```ini
[checks]
# List loopback-bound sockets too. Off by default: nothing off the host can
# reach 127.x / ::1, and applications churn through random high ports there.
# Whatever is listed is listed in full — the socket inventory is never truncated.
list_local_ports = false

# Extra path patterns to skip in the /etc scan (comma-separated shell globs).
# For backup agents that write timestamped files into /etc.
etc_ignore = /etc/CommVaultRegistryBackups/*

# Extra mount globs to skip in the disk check, ON TOP OF a built-in list that
# already covers kubelet, containerd, docker and snap. Usually left empty.
disk_ignore =

# Explicit kubeconfig. Blank auto-discovers rke2, k3s, kubeadm and ~/.kube.
kubeconfig =
```

Patterns are shell-quoted before reaching `find`, so a space or a semicolon in
one stays part of the pattern instead of becoming a separate command.

### Why `disk_ignore` has built-in defaults

A Kubernetes node is not forty disks. Two things happen on one:

- Every pod gets a 64 MB `shm` tmpfs under
  `/run/k3s/containerd/.../sandboxes/<id>/shm`. Always 0% used, and it gets a
  new identity every time the pod restarts, so the day-over-day delta churned
  forever. These slipped through the old filter because it matches the *device*
  column against `tmpfs`, and a sandbox shm mount's device is literally `shm`.
- Worse, a pod's `subPath` mounts and its `local-path` PVCs are **bind mounts of
  the root filesystem**, so `df` reports each one with `/dev/sda1` and the
  root's numbers. Forty such pods meant forty identical rows — and, the moment
  `/` crossed 90%, forty identical CAUTION alerts, each with its own
  fingerprint in the de-duplication state.

So the disk check does two things: it skips a built-in list of container-runtime
mount globs (`disk_ignore` **adds** to that list rather than replacing it), and
it then keeps only one row per distinct `(device, size, used, free)` — the
shortest mount point, so `/` always wins. Anything hidden that way is summarised
in a single `Hidden Mounts` row. The trailing `/*` in each glob is deliberate:
`/var/lib/docker/*` skips the per-container mounts while a *dedicated
filesystem* mounted at `/var/lib/docker` is still reported.

### Kubernetes options

```ini
[kubernetes]
# auto    — the whole cluster, plus a per-node pod summary on a multi-node
#           cluster so the same crash loop is not emailed once per node
# cluster — always the whole cluster
# node    — only the pods scheduled on this host
# off     — no Kubernetes section at all
scope = auto

# Individual signals, so one noisy source can be silenced without losing
# the rest of the section.
nodes  = true
pods   = true
pvcs   = true
events = true
images = true

# Images churn on every deploy, so the default is a count only.
list_images = false
```

The section appears only when the host has **both** a `kubectl` and a readable
kubeconfig — a `kubectl` that cannot reach a cluster has nothing to say.
Discovery checks `$KUBECONFIG`, `/etc/rancher/rke2/rke2.yaml`,
`/etc/rancher/k3s/k3s.yaml`, `/etc/kubernetes/admin.conf` and `~/.kube/config`,
and finds the binary by absolute path as well as `$PATH`. That last part
matters: the cron entry runs with `PATH=/usr/bin:/bin` and reads no login
profile, so the `export PATH=/var/lib/rancher/rke2/bin:$PATH` that makes
`kubectl` work in your shell does not exist during the nightly run.

One number is deliberately **not** reported: a "warning events in the last 24
hours" count. The API server's `--event-ttl` defaults to **one hour** and both
k3s and RKE2 ship that default, so a 24-hour window is unobtainable — and a row
claiming one would make a cluster look quiet 90 minutes after an incident. The
row labels the window from the oldest event actually returned instead.

---

## Host Identity

```ini
[general]
hostname =        # blank = the kernel hostname (`hostname` on the command line)
```

The report header, the email subject and the saved report filenames all name the
host. That name comes from the **kernel hostname**, not `socket.getfqdn()`.

`getfqdn()` takes the kernel hostname, reverse-resolves it, and returns the
*first* name it finds — which on a clustered host is routinely a shared VIP
rather than the machine. Three RKE2 nodes behind `xlp-mgmt.example.com` each
reported themselves as `xlp-mgmt.example.com`: identical headers, identical
subject lines, colliding report filenames, and three machines' alerts arriving as
though one host were flapping.

It is also **not stable**. Asked again, the same node answered
`example-03.domain.com` — upper-cased, on a host whose own name is lowercase. The
answer depends on which node currently holds the VIP, on DNS, and on cache state.
An identity that drifts with any of those is worse than a wrong one, because the
same machine files reports under different names on different days.

So the kernel hostname wins, and it is usually already qualified. `getfqdn()` is
consulted for one narrow purpose — supplying a domain the kernel name *lacks* —
and only when it is talking about the same machine, so `web01` +
`web01.example.com` still renders fully qualified. When the resolved name
disagrees, the report says so explicitly in System Information:

```
Hostname        example-03
Resolved name   xlp-mgmt.domain.com  (shared/VIP name — not used as this host's identity)
```

Set `hostname` only if you want something other than the kernel name. The
default `From:` address follows the same identity, borrowing the domain from the
resolved name when the kernel one is bare — so `example-03` on
`xlp-mgmt.domain.com` sends as `healthcheck@example-03.domain.com`.

---

## SMTP Configuration

```ini
[smtp]
host = relay.domain.com             # leave blank to use localhost (Postfix)
port = 25
use_tls = false
username =                          # leave blank if no authentication required
password =
#from = healthcheck@domain.com      # commented out: defaults to healthcheck@<FQDN>
```

`from` ships commented out on purpose. Left alone it becomes
`healthcheck@<this host's FQDN>`, which is right on almost every host — whereas
setting it in the example meant `install.sh`'s `MAIL_DOMAIN` substitution
rewrote it into a sender that had nothing to do with the sending machine.
Uncomment it only when the relay demands a fixed address.

## Thresholds (defaults)

| Metric | CAUTION | UNHEALTHY |
|--------|---------|-----------|
| CPU per core | 80% | 95% |
| Disk per mount | 90% | 95% |
| RAM | 80% | 95% |
| Load average | 1.0 × CPU count | 2.0 × CPU count |
| Zombie processes | 10 | 50 |
| Failed SSH attempts (today) | 50 | 500 |
| Pending updates | off | — |
| Pending security updates | off | — |
| fail2ban banned IPs | off | — |
| Pod Pending / ContainerCreating | 15 min | — |
| Pod restarts **gained since the last run** | 3 | — |
| Evicted pod, newer than | 24 h | — |

All thresholds are configurable in `healthcheck.conf` under `[thresholds]`.
The ones marked *off* are reported as INFO until you give them a number — they
describe backlog or normal defensive activity rather than an incident.

---

## First-Run Behaviour

State snapshots (ports, packages, SUID files, crontabs) are created on the first run. Change detection only activates from the **second run** onwards — no false positives on initial deployment.

---

## Development

The server runs this with a bare `python3` — the system one, or the private
Miniconda that `install.sh` provisions — and **no third-party packages**.
The dev toolchain is therefore kept strictly separate: it lives in
`pyproject.toml` under `[dependency-groups]`, never in `dependencies`, and is
never copied to a server.

There is a `Makefile` for the common tasks — run `make` on its own to list them:

```bash
make install       # uv sync: install pytest / mypy / ruff into .venv
make check         # tests + lint + types — the one to remember
make test          # 252 tests, ~1s, no root and no network needed
make deploy-check  # prove the shipping file set runs on a bare system python3
make text          # preview the report in the terminal, sends no email
```

The Makefile is a workstation convenience and is never copied to a server. It
wraps `uv` directly if you prefer:

```bash
uv sync
uv run pytest
uv run mypy .
uv run ruff check .
```

`tests/test_stdlib_only.py` enforces the deployment contract two ways: it walks
the AST of every runtime module rejecting non-stdlib imports, and it re-imports
the whole package in a subprocess started with `python -S` (site-packages
disabled) — the closest thing to a fresh server without leaving the laptop.

The rest of the suite pins the alerting behaviour: `test_alerts.py` drives the
de-duplication state machine day by day, `test_routing.py` covers who receives
what, and `test_checks_noise.py` holds one regression test per source of the
old daily-CAUTION noise, each written as "this input must **not** raise an
alert" so that re-tightening a threshold has to be deliberate.

Note for contributors: `ruff` deliberately does **not** enable the `UP`
(pyupgrade) rules, and no `python_version` is pinned for mypy. The runtime code
must stay parseable by the Python 3.6 on the RHEL 7 targets — no walrus, no
dataclasses, no `match`.

---

## Releases

Cutting a release is not a manual checklist any more. **Merging to `main`
releases.**

`.github/workflows/01-check.yml` runs tests, lint, types and `make deploy-check`
on every pull request. `.github/workflows/02-tag.yml` runs on a push to `main`,
calls that same check workflow as its gate, and only then:

1. derives the next version from the commit messages —
   `feat:` → minor, a `!:` breaking marker → major, anything else → patch;
2. runs `scripts/set-version.sh`, which writes the version into `hc/utils.py`,
   `pyproject.toml` and `install.sh` and refreshes `uv.lock`;
3. commits that to `main`, tags **the commit it just made**, and creates the
   GitHub release.

The order in step 3 is the point: tagging after the rewrite is what guarantees
the release tarball contains a matching `VERSION` and a `DEFAULT_TAG` pointing
at the very tag being cut. The bot's own commit is pushed with the default
`GITHUB_TOKEN`, which does not trigger workflows, so it cannot re-fire the
release.

To move the version by hand — you rarely should — use the same script:

```bash
scripts/set-version.sh 2.1.0
```

`tests/test_version_consistency.py` fails the build if the four files ever
disagree. It exists because they did: `pyproject.toml` said `2.0.3` while
`uv.lock` still said `2.0.0`.

> **Repo setting to check once:** if `main` is a protected branch, the
> `github-actions[bot]` push in step 3 is rejected and the workflow fails after
> the tests pass. Allow it to bypass the rule, or give the workflow a PAT.

---

## Requirements

- Python 3.6+ (stdlib only — no `pip install` required). No system `python3`?
  [`install.sh`](install.sh) provisions a private one; see [Quick Install](#quick-install).
- For `install.sh` only: `curl` or `wget`, and `tar`. No `git` needed.
- `sysstat` package for `mpstat` (per-core CPU stats; falls back gracefully if absent)
- A working MTA on localhost
- Root or sudo for `bootstrap` auto-install and reading protected log files
