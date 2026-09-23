"""The config is a merge, not a choice.

    builtin_defaults()      in code, the floor
    healthcheck.conf.base   shipped defaults, replaced by every upgrade
    healthcheck.conf        this host's overrides, never touched by an upgrade

This replaced a migration step that rewrote the operator's file on upgrade to
inject settings added by a release. Merging removes the need: a new setting
arrives in the base layer with a working value and is simply read.

What must hold, forever:
  * an upgrade delivers new settings to a host whose conf predates them
  * it never changes a value that host chose, including an empty one
  * the base file is read by the program, so it must contain no placeholders
"""

import configparser
import pathlib
import textwrap

import pytest

import hc.utils
from hc.utils import builtin_defaults, load_config

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def layers(monkeypatch, tmp_path):
    """Point both file layers at scratch files the test writes."""
    base = tmp_path / "healthcheck.conf.base"
    conf = tmp_path / "healthcheck.conf"

    def write(base_text="", conf_text=""):
        base.write_text(textwrap.dedent(base_text))
        if conf_text:
            conf.write_text(textwrap.dedent(conf_text))
        monkeypatch.setattr(hc.utils, "BASE_PATH", base)
        monkeypatch.setattr(hc.utils, "CONFIG_PATH", conf)
        return load_config()

    write.base = base          # type: ignore[attr-defined]
    write.conf = conf          # type: ignore[attr-defined]
    return write


# ───────────────────────────────────────── merging

def test_the_conf_wins_over_the_base(layers):
    cfg = layers(
        base_text="[crontab]\ntime = 07:00\nrandom_window = 4h\n",
        conf_text="[crontab]\ntime = 06:30\n",
    )
    assert cfg.get("crontab", "time") == "06:30"
    assert cfg.get("crontab", "random_window") == "4h"


def test_a_setting_added_by_an_upgrade_reaches_a_conf_that_predates_it(layers):
    """The whole point. The operator's file is from an older release and says
    nothing about random_window; the upgrade ships it in the base layer."""
    cfg = layers(
        base_text="[crontab]\ntime = 07:00\nrandom = true\nrandom_window = 4h\n",
        conf_text="[crontab]\ntime = 06:30\n",
    )
    assert cfg.get("crontab", "random") == "true"
    assert cfg.get("crontab", "random_window") == "4h"


def test_an_empty_value_in_the_conf_is_an_override_not_an_omission(layers):
    """`alert_recipients =` means "nobody", deliberately. The base layer must
    not put its own value back and start mailing a list the operator emptied."""
    cfg = layers(
        base_text="[email]\nalert_recipients = shipped@example.com\n",
        conf_text="[email]\nalert_recipients =\n",
    )
    assert cfg.get("email", "alert_recipients") == ""


def test_a_section_only_the_base_has_still_arrives(layers):
    cfg = layers(
        base_text="[kubernetes]\nscope = auto\n",
        conf_text="[smtp]\nhost = mail.example.com\n",
    )
    assert cfg.get("kubernetes", "scope") == "auto"
    assert cfg.get("smtp", "host") == "mail.example.com"


def test_a_host_with_no_conf_at_all_runs_on_the_base(layers):
    cfg = layers(base_text="[crontab]\ntime = 05:00\n")
    assert cfg.get("crontab", "time") == "05:00"


def test_a_missing_base_file_falls_through_to_the_built_in_floor(monkeypatch, tmp_path):
    """A hand-copied install of healthcheck.py + hc/ has no base file. It must
    still run, on the defaults compiled into the code."""
    monkeypatch.setattr(hc.utils, "BASE_PATH", tmp_path / "absent.base")
    monkeypatch.setattr(hc.utils, "CONFIG_PATH", tmp_path / "absent.conf")
    cfg = load_config()
    assert cfg.get("crontab", "time") == "07:00"
    assert cfg.get("thresholds", "cpu_caution") == "80"


def test_the_hostname_override_is_read_from_whichever_layer_sets_it(layers):
    layers(base_text="[general]\nhostname =\n",
           conf_text="[general]\nhostname = report-name.example.com\n")
    assert hc.utils.host_label() == "report-name.example.com"


# ───────────────────────────────────────── the shipped base file

def test_the_base_file_and_the_built_in_floor_agree(monkeypatch, tmp_path):
    """Two sources for one default is how notify_all_on came to be `caution` in
    code and `unhealthy` in the shipped config for three releases — what a host
    did depended on whether anyone had copied the file. Never again."""
    monkeypatch.setattr(hc.utils, "CONFIG_PATH", tmp_path / "absent.conf")
    effective = load_config()          # defaults + the real base file

    # [smtp] from is derived from this machine's own name at import time, so
    # it has no fixed literal to compare against. The base file ships it
    # commented out for exactly that reason.
    derived = {("smtp", "from")}

    drift = []
    for section, options in builtin_defaults().items():
        for key, value in options.items():
            if (section, key) in derived:
                continue
            live = effective.get(section, key)
            if live.strip() != value.strip():
                drift.append(f"[{section}] {key}: code={value!r} base={live!r}")
    assert not drift, "healthcheck.conf.base disagrees with builtin_defaults(): " + \
        "; ".join(drift)


def test_every_built_in_default_is_documented_in_the_base_file():
    """A setting that exists only in code is one no operator can discover."""
    base = configparser.ConfigParser()
    base.read_string((REPO / "healthcheck.conf.base").read_text())

    documented = _documented((REPO / "healthcheck.conf.base").read_text())
    missing = [
        f"[{s}] {k}"
        for s, options in builtin_defaults().items()
        for k in options
        if k not in documented.get(s, set())
    ]
    assert not missing, f"not in healthcheck.conf.base: {missing}"


def test_the_base_file_carries_no_placeholder_recipients():
    """The base file is READ by the program, not copied by a human. A leftover
    `ops.a@domain.com` in it is not an example — it is an address a host with
    no configured recipients would genuinely try to mail."""
    base = configparser.ConfigParser()
    base.read_string((REPO / "healthcheck.conf.base").read_text())
    for key in ("daily_recipients", "alert_recipients"):
        assert base.get("email", key).strip() == "", (
            f"[email] {key} must be empty in the base layer; put the example "
            f"in a comment instead")


def test_the_base_file_says_not_to_edit_it():
    """Every upgrade overwrites it, so an operator's edits are lost. The file
    has to say so in the part they will read first."""
    head = (REPO / "healthcheck.conf.base").read_text()[:900].lower()
    assert "do not edit" in head
    assert "healthcheck.conf" in head


def _documented(text: str) -> dict:
    """{section: {keys}} including keys shipped commented out.

    `#from = healthcheck@domain.com` is documentation, not an omission: the
    default is derived from the host, so writing a literal one into the base
    layer would pin every host to one sender.
    """
    import re

    found: dict = {}
    section = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
        elif section:
            m = re.match(r"^[#;]?\s*([A-Za-z0-9_]+)\s*=", s)
            if m:
                found.setdefault(section, set()).add(m.group(1).lower())
    return found
