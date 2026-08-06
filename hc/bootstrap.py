import os
import shutil
import sys

from .utils import run, has, pkg_manager, read_os_release
from .utils import SCRIPT_PATH, SCRIPT_DIR, STATE_DIR, REPORT_DIR, CONFIG_PATH


_BOOTSTRAP_PKGS = {
    "dnf":     [("mpstat",          "sysstat"),
                ("ss",              "iproute"),
                ("fail2ban-client", "fail2ban"),
                ("postfix",         "postfix"),
                ("python3",         "python3")],
    "yum":     [("mpstat",          "sysstat"),
                ("ss",              "iproute"),
                ("fail2ban-client", "fail2ban"),
                ("postfix",         "postfix"),
                ("python3",         "python3")],
    "apt-get": [("mpstat",          "sysstat"),
                ("ss",              "iproute2"),
                ("fail2ban",        "fail2ban"),
                ("postfix",         "postfix"),
                ("python3",         "python3")],
}


def bootstrap() -> None:
    print("\n══════════════ Linux Health Check — Bootstrap ══════════════")
    osi = read_os_release()
    pm  = pkg_manager()

    print(f"\n  OS         : {osi.get('PRETTY_NAME', 'unknown')}")
    print(f"  Pkg Mgr    : {pm or '(not detected)'}")
    print(f"  Python     : {sys.version.split()[0]}")
    print(f"  Script     : {SCRIPT_PATH}")
    print(f"  Config     : {CONFIG_PATH} "
          f"({'exists' if CONFIG_PATH.exists() else '*** MISSING — copy healthcheck.conf.example ***'})")

    is_root = (os.geteuid() == 0)

    if not pm:
        print("\n  ERROR: No supported package manager found (dnf / yum / apt-get).")
        return

    pkgs = _BOOTSTRAP_PKGS.get(pm, [])
    missing = [(tool, pkg) for tool, pkg in pkgs if not has(tool)]

    print(f"\n  Checking {len(pkgs)} required tools:")
    for tool, pkg in pkgs:
        mark = "✓" if has(tool) else "✗"
        hint = f"  → sudo {pm} install -y {pkg}" if not has(tool) else ""
        print(f"    {mark}  {tool:<25}{hint}")

    if not missing:
        print("\n  All required tools are already installed.")
    elif is_root:
        print(f"\n  Installing {len(missing)} missing tool(s)...")
        for tool, pkg in missing:
            print(f"    → {pm} install -y {pkg} ... ", end="", flush=True)
            rc, _, err = run(f"{pm} install -y {pkg}", timeout=180)
            print("OK" if rc == 0 else f"FAILED ({err[:60]})")
    else:
        print("\n  ⚠ Not running as root — run these commands manually:\n")
        for tool, pkg in missing:
            print(f"    sudo {pm} install -y {pkg}")

    print()
    for d in (STATE_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
        print(f"  Directory : {d} ✓")

    example = SCRIPT_DIR / "healthcheck.conf.example"
    if not CONFIG_PATH.exists():
        if example.exists():
            shutil.copy(str(example), str(CONFIG_PATH))
            print(f"\n  Created {CONFIG_PATH} from example.")
            print("  *** Edit it to configure email recipients and SMTP. ***")
        else:
            print(f"\n  No config found at {CONFIG_PATH}.")
            print("  Create it manually (see healthcheck.conf.example).")

    print("\n═══════════════════════════════════════════════════════════\n")
