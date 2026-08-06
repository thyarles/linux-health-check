import re
import shutil
import subprocess
import sys

from .utils import run, load_config, SCRIPT_PATH


def install_crontab(time_str: str = "") -> None:
    cfg = load_config()
    if not time_str:
        time_str = cfg.get("crontab", "time", fallback="07:00")

    m = re.fullmatch(r"(\d{1,2}):(\d{2})", time_str)
    if not m:
        sys.exit(f"Invalid time '{time_str}'. Use HH:MM (e.g. 07:00).")

    hour, minute = m.group(1).zfill(2), m.group(2)
    if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        sys.exit(f"Invalid time '{time_str}'. Hours must be 00-23, minutes 00-59.")

    py3  = shutil.which("python3") or "/usr/bin/python3"
    tag  = "# linux-healthcheck-managed"
    line = f"{minute} {hour} * * * {py3} {SCRIPT_PATH} run >> /var/log/healthcheck.log 2>&1 {tag}"

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
