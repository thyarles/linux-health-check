"""Reading, explaining and editing the override layer.

The config is two files, merged weakest-first by utils.load_config():

    builtin_defaults()      in code, the floor — only reached if the base
                            file is missing
    healthcheck.conf.base   shipped defaults, replaced by every upgrade
    healthcheck.conf        this host's overrides, never touched by an upgrade

The point of the split is that a setting added in a new release arrives in the
base file with a working default, so an upgraded host picks it up without
anyone editing — or migrating — the file that holds its SMTP credentials.

What lives here is everything ABOUT that arrangement: which layer an effective
value came from (`config show`), writing one override into healthcheck.conf
without disturbing the rest of it (`config set`), creating the small starter
file a fresh install begins with (`config init`), and dropping the lines in an
older config that merely restate the base and so pin the host to the defaults
it was installed with (`config prune`).

Editing is line-oriented rather than a configparser round-trip on purpose:
ConfigParser.write() discards every comment in the file and reflows what it
keeps, which would quietly throw away the operator's own notes the first time
anyone changed a port.
"""

import configparser
import pathlib
import re
import shutil
import sys

from .utils import BASE_PATH, CONFIG_PATH, builtin_defaults, config_layers

STARTER = """\
# healthcheck.conf — what THIS host does differently.
# ==============================================================
# Only overrides belong here. Every setting, its documentation
# and its default live in healthcheck.conf.base beside this file;
# anything you write here wins over that, and anything you leave
# out follows the base file — including settings added by future
# upgrades, which is why this file stays short.
#
#   healthcheck.py config show      the effective value of every
#                                   setting, and where it came from
#   healthcheck.py config set alerts.notify_all_on=caution
#                                   write one override in here
#
# This file is never modified by an upgrade. It holds SMTP
# credentials, so it is kept mode 600.
# ==============================================================

[smtp]
host = relay.{domain}

[email]
# REQUIRED. Without these the run has nobody to send to and says
# so on every execution.
#
# The small "is the check still alive?" group — mailed on EVERY
# run, including the all-clear.
daily_recipients =

# The broad list — mailed only when something is NEW or got WORSE.
alert_recipients =
"""


# ─────────────────────────────────────────────────────────────────────────────
# READING
# ─────────────────────────────────────────────────────────────────────────────

def _layer_values() -> dict:
    """{(section, key): layer_name} for the topmost layer that sets each key."""
    origin: dict = {}
    for section, options in builtin_defaults().items():
        for key in options:
            origin[(section, key.lower())] = "default"
    for name, path in config_layers():
        if not path.exists():
            continue
        parser = configparser.RawConfigParser()
        try:
            parser.read_string(path.read_text(encoding="utf-8"))
        except configparser.Error as exc:
            sys.exit(f"  ✗ {path.name} will not parse: {exc}")
        for section in parser.sections():
            for key in parser.options(section):
                origin[(section, key.lower())] = name
    return origin


def show(cfg, only_overrides: bool = False) -> int:
    """Print every effective setting and the layer it came from."""
    origin = _layer_values()
    labels = {"conf": CONFIG_PATH.name, "base": BASE_PATH.name, "default": "built-in"}

    print(f"  Layers, weakest first: built-in < {BASE_PATH.name} < {CONFIG_PATH.name}")
    for name, path in config_layers():
        state = "present" if path.exists() else "MISSING"
        print(f"    {labels[name]:<24} {state}")
    if only_overrides:
        print(f"\n  Showing only what {CONFIG_PATH.name} overrides.")

    shown = 0
    for section in cfg.sections():
        rows = []
        for key in cfg.options(section):
            layer = origin.get((section, key.lower()), "default")
            if only_overrides and layer != "conf":
                continue
            value = cfg.get(section, key).replace("\n", " ")
            if len(value) > 46:
                value = value[:43] + "..."
            rows.append((key, value or "(empty)", layer))
        if not rows:
            continue
        print(f"\n  [{section}]")
        for key, value, layer in rows:
            mark = "*" if layer == "conf" else " "
            print(f"  {mark} {key:<26} {value:<48} {labels[layer]}")
        shown += len(rows)

    if only_overrides and not shown:
        print(f"\n  Nothing — this host runs entirely on {BASE_PATH.name} defaults.")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# WRITING
# ─────────────────────────────────────────────────────────────────────────────

# Spaces around the dot and the equals are tolerated: `--set "crontab.time =
# 06:30"` is what someone writes when they are copying the shape of the config
# file itself, and refusing it teaches nothing.
_ASSIGN = re.compile(r"^\s*([A-Za-z0-9_]+)\s*\.\s*([A-Za-z0-9_]+)\s*=(.*)$", re.S)


def parse_assignment(text: str) -> tuple:
    """'crontab.time=06:30' -> ('crontab', 'time', '06:30').

    Split on the FIRST '=' only, so a value may contain one — passwords and
    base64 secrets routinely do.
    """
    m = _ASSIGN.match(text.strip())
    if not m:
        sys.exit(f"  ✗ '{text}' is not section.key=value (e.g. crontab.time=06:30)")
    return m.group(1).lower(), m.group(2).lower(), m.group(3).strip()


def known_keys() -> dict:
    """{section: {keys}} that may be set — the union of the shipped layers.

    A typo like `crontab.tim=06:30` must not be accepted and then silently do
    nothing for the rest of the host's life, which is exactly what writing an
    unvalidated key into an INI file buys you.
    """
    valid: dict = {}
    for section, options in builtin_defaults().items():
        valid[section] = {k.lower() for k in options}
    if BASE_PATH.exists():
        parser = configparser.RawConfigParser()
        try:
            parser.read_string(BASE_PATH.read_text(encoding="utf-8"))
        except configparser.Error:
            return valid
        for section in parser.sections():
            valid.setdefault(section, set()).update(
                k.lower() for k in parser.options(section))
    return valid


def _is_continuation(line: str) -> bool:
    """An indented value line, as in a wrapped recipient list.

    Replacing `daily_recipients` has to take its wrapped remainder with it, or
    the leftover indented addresses attach themselves to whatever key lands
    next and the file stops meaning what it says.
    """
    return bool(line[:1] in (" ", "\t") and line.strip()
                and not line.strip().startswith(("#", ";")))


def set_option(text: str, section: str, key: str, value: str) -> str:
    """Return `text` with section.key set to value, comments left alone."""
    lines = text.splitlines()
    out: list = []
    in_section = False
    replaced = False
    section_end = -1
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section and section_end < 0:
                section_end = len(out)
            in_section = stripped[1:-1].lower() == section.lower()
            out.append(line)
            i += 1
            continue

        if in_section and "=" in line and not stripped.startswith(("#", ";")):
            if line.split("=", 1)[0].strip().lower() == key:
                out.append(f"{key} = {value}".rstrip())
                replaced = True
                i += 1
                while i < len(lines) and _is_continuation(lines[i]):
                    i += 1
                continue

        out.append(line)
        i += 1

    if replaced:
        return "\n".join(out).rstrip("\n") + "\n"

    if in_section or section_end >= 0:
        # Append inside the existing section rather than at the end of the
        # file, where configparser would read it as the LAST section's key.
        at = len(out) if in_section else section_end
        while at > 0 and not out[at - 1].strip():
            at -= 1
        out[at:at] = [f"{key} = {value}".rstrip()]
        return "\n".join(out).rstrip("\n") + "\n"

    if out and out[-1].strip():
        out.append("")
    out.extend([f"[{section}]", f"{key} = {value}".rstrip()])
    return "\n".join(out).rstrip("\n") + "\n"


def set_options(assignments: list, config_path: "pathlib.Path | None" = None,
                create: bool = True, mail_domain: str = "") -> int:
    """Apply `section.key=value` strings to healthcheck.conf."""
    path = config_path or CONFIG_PATH
    if not assignments:
        sys.exit("  ✗ config set needs at least one section.key=value")

    valid = known_keys()
    pairs = []
    for raw in assignments:
        section, key, value = parse_assignment(raw)
        if section not in valid:
            sys.exit(f"  ✗ no [{section}] section. Known: "
                     f"{', '.join(sorted(valid))}")
        if key not in valid[section]:
            sys.exit(f"  ✗ [{section}] has no '{key}'. Known keys: "
                     f"{', '.join(sorted(valid[section]))}")
        pairs.append((section, key, value))

    if not path.exists():
        if not create:
            sys.exit(f"  ✗ {path} does not exist")
        init_config(path, mail_domain)

    text = path.read_text(encoding="utf-8")
    updated = text
    for section, key, value in pairs:
        updated = set_option(updated, section, key, value)

    if updated == text:
        for section, key, value in pairs:
            print(f"  = {section}.{key} is already {value or '(empty)'}")
        return 0

    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(str(path), str(backup))
    path.write_text(updated, encoding="utf-8")
    for section, key, value in pairs:
        print(f"  ✓ {section}.{key} = {value or '(empty)'}")
    print(f"  Written to {path.name}; previous version kept as {backup.name}")
    return 0


def _comment_block(lines: list, at: int) -> int:
    """Index where the comment block immediately above `at` starts."""
    start = at
    while start > 0 and lines[start - 1].strip().startswith(("#", ";")):
        start -= 1
    return start


def prune(text: str, base_text: str) -> tuple:
    """Drop keys whose value already matches the base layer. -> (text, removed).

    A config installed before this layering existed is a full copy of the old
    example, so every default in it is pinned at the version it was installed
    from and no later improvement can reach it. Removing the lines that merely
    restate the base turns it back into what it is supposed to be: the list of
    things this host does differently.

    Only comment lines that appear verbatim in the base file are removed with
    the key — those were copied. Anything the operator wrote themselves stays,
    even when the setting beneath it goes.
    """
    base = configparser.RawConfigParser()
    try:
        base.read_string(base_text)
    except configparser.Error:
        return text, []

    lines = text.splitlines()
    keep = [True] * len(lines)
    removed: list = []
    section = ""
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            i += 1
            continue

        if (section and "=" in lines[i] and not stripped.startswith(("#", ";"))
                and not _is_continuation(lines[i])):
            key = lines[i].split("=", 1)[0].strip()
            value = lines[i].split("=", 1)[1].strip()
            end = i + 1
            while end < len(lines) and _is_continuation(lines[end]):
                value += " " + lines[end].strip()
                end += 1

            if base.has_option(section, key):
                mine = " ".join(value.split())
                theirs = " ".join(base.get(section, key).split())
                if mine == theirs:
                    start = _comment_block(lines, i)
                    # Only take the comment along if the base ships it too.
                    while start < i and lines[start] not in base_text.splitlines():
                        start += 1
                    for n in range(start, end):
                        keep[n] = False
                    removed.append(f"[{section}] {key}")
            i = end
            continue
        i += 1

    if not removed:
        return text, []

    # A section every one of whose keys was redundant is now a bare header, and
    # the prose introducing it describes settings that are no longer there. Both
    # go — but only the prose that came from the shipped file. Anything the
    # operator wrote stays, even orphaned: their note is the one thing in this
    # file that nothing else records.
    base_lines = set(base_text.splitlines())
    header = -1
    for i, line in enumerate(lines + ["[end]"]):
        stripped = line.strip()
        if not (stripped.startswith("[") and stripped.endswith("]")):
            continue
        if header >= 0:
            body = range(header, min(i, len(lines)))
            if not any(keep[n] and "=" in lines[n] and
                       not lines[n].strip().startswith(("#", ";")) for n in body):
                keep[header] = False
                for n in body:
                    if lines[n] in base_lines or not lines[n].strip():
                        keep[n] = False
        header = i

    out = [line for line, k in zip(lines, keep) if k]
    # Collapse the runs of blank lines the removals leave behind.
    tidy: list = []
    for line in out:
        if not line.strip() and tidy and not tidy[-1].strip():
            continue
        tidy.append(line)
    return "\n".join(tidy).rstrip("\n") + "\n", removed


def prune_config(config_path: "pathlib.Path | None" = None,
                 apply: bool = False) -> list:
    """Report — or with apply=True, remove — settings that restate the base."""
    path = config_path or CONFIG_PATH
    if not path.exists():
        print(f"  No {path.name} — nothing to prune.")
        return []
    if not BASE_PATH.exists():
        sys.exit(f"  ✗ {BASE_PATH.name} is missing — cannot tell what is redundant.")

    original = path.read_text(encoding="utf-8")
    updated, removed = prune(original, BASE_PATH.read_text(encoding="utf-8"))
    removed = list(removed)

    if not removed:
        print(f"  ✓ {path.name} holds only real overrides already.")
        return []

    print(f"  {len(removed)} setting(s) in {path.name} only restate "
          f"{BASE_PATH.name}:")
    for item in removed:
        print(f"    - {item}")

    if not apply:
        print("\n  Nothing written. These pin this host to the values it was "
              "installed with,\n  so a better default in a later release cannot "
              "reach it. Remove them with:\n      healthcheck.py config prune --apply")
        return removed

    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(str(path), str(backup))
    path.write_text(updated, encoding="utf-8")
    print(f"  ✓ Removed. Previous version kept as {backup.name}")
    return removed


def init_config(config_path: "pathlib.Path | None" = None, mail_domain: str = "",
                force: bool = False) -> int:
    """Create the starter healthcheck.conf. Never overwrites without --force."""
    path = config_path or CONFIG_PATH
    if path.exists() and not force:
        print(f"  {path.name} already exists — left untouched.")
        return 0

    path.write_text(STARTER.format(domain=mail_domain or "domain.com"),
                    encoding="utf-8")
    path.chmod(0o600)          # it will hold SMTP credentials
    print(f"  ✓ Created {path.name} (mode 600). Set the recipients in it, or:")
    print("      healthcheck.py config set email.daily_recipients=ops@example.com")
    return 0


def main(args: list) -> int:
    """`healthcheck.py config <show|set|init>`."""
    from .utils import load_config

    sub = args[0].lower() if args else "show"
    rest = args[1:]

    if sub == "show":
        return show(load_config(), only_overrides="--diff" in rest)
    if sub == "set":
        return set_options([a for a in rest if not a.startswith("--")])
    if sub == "init":
        domain = next((a.split("=", 1)[1] for a in rest
                       if a.startswith("--mail-domain=")), "")
        return init_config(mail_domain=domain, force="--force" in rest)
    if sub == "prune":
        prune_config(apply="--apply" in rest)
        return 0

    sys.exit("  ✗ config takes: show [--diff] | set section.key=value ... | "
             "init [--mail-domain=example.com] [--force] | prune [--apply]")
