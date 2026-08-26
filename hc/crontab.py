import configparser
import os
import re
import shutil
import subprocess
import sys

from .utils import run, load_config, SCRIPT_PATH


def resolve_python(cfg: "configparser.ConfigParser | None" = None) -> str:
    """Absolute path to the interpreter the cron entry should use.

    Order: HEALTHCHECK_PYTHON env > [crontab] python in the config >
    sys.executable (the interpreter running us now) > python3 on PATH.

    Defaulting to sys.executable is what makes `/opt/py/bin/python3
    healthcheck.py crontab` install a cron line that runs under that same
    interpreter, rather than silently pinning the entry to a system python3
    that may not exist or may lack the stdlib version this code needs.
    """
    env = os.environ.get("HEALTHCHECK_PYTHON", "").strip()
    if env:
        return env
    if cfg is not None:
        cfgpy = str(cfg.get("crontab", "python", fallback="")).strip()
        if cfgpy:
            return cfgpy
    if sys.executable:
        return sys.executable
    return shutil.which("python3") or "/usr/bin/python3"


def install_crontab(time_str: str = "") -> None:
    cfg = load_config()
    if not time_str:
        time_str = cfg.get("crontab", "time", fallback="07:00")

    m = re.fullmatch(r"(\d{1,2}):(\d{2})", time_str)
    if not m:
        sys.exit(f"Invalid time '{time_str}'. Use HH:MM (e.g. 07:00).")

    hour, minute = m.group(1).zfill(2), m.group(2)
    py3  = resolve_python(cfg)
    tag  = "# linux-healthcheck-managed"
    line = f"{minute} {hour} * * * {py3} {SCRIPT_PATH} run >> /var/log/healthcheck.log 2>&1  {tag}"

    rc, current, _ = run("crontab -l 2>/dev/null || true")
    kept = [l for l in current.splitlines() if tag not in l]
    kept.append(line)
    new_crontab = "\n".join(kept) + "\n"

    try:
        r = subprocess.run(["crontab", "-"], input=new_crontab,
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(f"  ✓ Crontab updated: runs daily at {hour}:{minute}")
            print(f"  Entry: {line}")
        else:
            sys.exit(f"  ✗ crontab update failed: {r.stderr}")
    except FileNotFoundError:
        sys.exit("  ✗ crontab command not found.")
