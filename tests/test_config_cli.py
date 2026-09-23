"""`healthcheck.py config show|set|init|prune` — the override layer.

`config set` exists so that an install or upgrade line can carry a host's
settings with it (`--set crontab.time=06:30`) instead of an operator opening an
editor on every machine. It edits the one file that is never regenerated, so
the rules are strict: preserve the operator's comments, change nothing but the
named key, and refuse anything that would write a setting the program does not
read.
"""

import configparser
import textwrap

import pytest

import hc.config as cfgmod
import hc.utils
from hc.config import init_config, parse_assignment, set_option, set_options

CONF = textwrap.dedent("""\
    # My notes about this host.

    [smtp]
    # The relay in this datacentre.
    host = relay.example.com
    port = 25

    [email]
    daily_recipients = ops.a@example.com,
                       ops.b@example.com
    """)


@pytest.fixture(autouse=True)
def real_base(monkeypatch):
    """Validation reads the shipped base file; keep it pointed at the real one."""
    monkeypatch.setattr(hc.utils, "BASE_PATH", hc.utils.BASE_PATH)
    return hc.utils.BASE_PATH


def parsed(text: str) -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c.read_string(text)
    return c


# ───────────────────────────────────────── parsing

@pytest.mark.parametrize("text,expected", [
    ("crontab.time=06:30",        ("crontab", "time", "06:30")),
    ("CRONTAB.TIME=06:30",        ("crontab", "time", "06:30")),
    ("crontab.random_window=2h",  ("crontab", "random_window", "2h")),
    ("email.daily_recipients=a@x,b@y", ("email", "daily_recipients", "a@x,b@y")),
    ("crontab.time = 06:30 ",     ("crontab", "time", "06:30")),
])
def test_assignments_people_actually_type(text, expected):
    assert parse_assignment(text) == expected


def test_only_the_first_equals_splits_the_value():
    """Passwords and base64 secrets contain '='; splitting on all of them
    would silently truncate the credential and break the relay login."""
    assert parse_assignment("smtp.password=p@ss=w0rd==") == (
        "smtp", "password", "p@ss=w0rd==")


@pytest.mark.parametrize("junk", ["crontab", "crontab.time", "time=06:30", ""])
def test_malformed_assignments_are_refused(junk):
    with pytest.raises(SystemExit):
        parse_assignment(junk)


# ───────────────────────────────────────── editing text

def test_an_existing_value_is_replaced_in_place():
    out = set_option(CONF, "smtp", "port", "587")
    assert parsed(out).get("smtp", "port") == "587"
    assert "# The relay in this datacentre." in out
    assert "# My notes about this host." in out


def test_a_new_key_lands_inside_its_own_section():
    """Appended at the end of the file it would sit under [email], where
    configparser reads it as an email setting and [crontab] never sees it."""
    out = set_option(CONF, "smtp", "use_tls", "true")
    cfg = parsed(out)
    assert cfg.get("smtp", "use_tls") == "true"
    assert not cfg.has_option("email", "use_tls")


def test_a_missing_section_is_created():
    out = set_option(CONF, "crontab", "time", "06:30")
    assert parsed(out).get("crontab", "time") == "06:30"


def test_replacing_a_wrapped_value_takes_its_continuation_lines_with_it():
    """A wrapped recipient list spans several indented lines. Leaving the tail
    behind attaches ops.b@example.com to whatever key follows it."""
    out = set_option(CONF, "email", "daily_recipients", "only@example.com")
    assert parsed(out).get("email", "daily_recipients") == "only@example.com"
    assert "ops.b@example.com" not in out


def test_setting_a_value_to_empty_is_allowed():
    """'nobody' is a legitimate setting, and the layering reads it as an
    override rather than as an omission."""
    out = set_option(CONF, "email", "daily_recipients", "")
    assert parsed(out).get("email", "daily_recipients") == ""


def test_repeated_sets_do_not_accumulate_duplicate_keys():
    out = CONF
    for value in ("06:00", "06:15", "06:30"):
        out = set_option(out, "crontab", "time", value)
    assert len([l for l in out.splitlines() if l.startswith("time")]) == 1
    assert parsed(out).get("crontab", "time") == "06:30"


def test_the_file_still_parses_after_every_kind_of_edit():
    out = CONF
    out = set_option(out, "smtp", "port", "587")          # replace
    out = set_option(out, "smtp", "username", "hc")       # add to section
    out = set_option(out, "alerts", "notify_all_on", "caution")  # new section
    cfg = parsed(out)
    assert cfg.get("smtp", "port") == "587"
    assert cfg.get("smtp", "username") == "hc"
    assert cfg.get("alerts", "notify_all_on") == "caution"


# ───────────────────────────────────────── the command

@pytest.fixture
def conf(tmp_path, monkeypatch):
    path = tmp_path / "healthcheck.conf"
    path.write_text(CONF)
    path.chmod(0o600)
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", path)
    return path


def test_set_writes_the_value_and_keeps_a_backup(conf):
    set_options(["smtp.port=587"], config_path=conf)
    assert parsed(conf.read_text()).get("smtp", "port") == "587"
    backup = conf.with_suffix(".conf.bak")
    assert parsed(backup.read_text()).get("smtp", "port") == "25"


def test_several_settings_apply_in_one_call(conf):
    set_options(["smtp.port=587", "smtp.use_tls=true", "crontab.time=06:30"],
                config_path=conf)
    cfg = parsed(conf.read_text())
    assert (cfg.get("smtp", "port"), cfg.get("smtp", "use_tls")) == ("587", "true")
    assert cfg.get("crontab", "time") == "06:30"


def test_an_unknown_key_is_refused_rather_than_written(conf):
    """`crontab.tim=06:30` accepted and written is a setting that does nothing
    for the rest of that host's life, with no way to notice."""
    before = conf.read_text()
    with pytest.raises(SystemExit) as exc:
        set_options(["crontab.tim=06:30"], config_path=conf)
    assert "time" in str(exc.value)          # the error names the real keys
    assert conf.read_text() == before


def test_an_unknown_section_is_refused(conf):
    with pytest.raises(SystemExit):
        set_options(["crontabs.time=06:30"], config_path=conf)


def test_nothing_is_written_when_any_assignment_is_bad(conf):
    """All-or-nothing: a typo in the third --set must not leave the first two
    applied and the install half-configured."""
    before = conf.read_text()
    with pytest.raises(SystemExit):
        set_options(["smtp.port=587", "smtp.nope=x"], config_path=conf)
    assert conf.read_text() == before


def test_setting_a_value_it_already_has_leaves_the_file_alone(conf):
    before = conf.read_text()
    set_options(["smtp.port=25"], config_path=conf)
    assert conf.read_text() == before
    assert not conf.with_suffix(".conf.bak").exists()


def test_set_creates_the_conf_when_a_host_has_none(tmp_path, monkeypatch):
    path = tmp_path / "healthcheck.conf"
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", path)
    set_options(["crontab.time=06:30"], config_path=path, mail_domain="example.com")
    assert parsed(path.read_text()).get("crontab", "time") == "06:30"
    assert path.stat().st_mode & 0o077 == 0


# ───────────────────────────────────────── init

def test_init_writes_a_starter_that_only_holds_overrides(tmp_path):
    path = tmp_path / "healthcheck.conf"
    init_config(path, mail_domain="mpt.mp.br")
    text = path.read_text()
    cfg = parsed(text)

    assert cfg.get("smtp", "host") == "relay.mpt.mp.br"
    assert cfg.get("email", "daily_recipients") == ""
    # Short by design: everything else comes from the base layer.
    assert len(cfg.sections()) <= 3
    assert "healthcheck.conf.base" in text


def test_init_is_mode_600_because_it_will_hold_smtp_credentials(tmp_path):
    path = tmp_path / "healthcheck.conf"
    init_config(path)
    assert path.stat().st_mode & 0o077 == 0


def test_init_never_clobbers_an_existing_config(tmp_path):
    path = tmp_path / "healthcheck.conf"
    path.write_text(CONF)
    init_config(path, mail_domain="example.com")
    assert path.read_text() == CONF


def test_init_overwrites_only_when_forced(tmp_path):
    path = tmp_path / "healthcheck.conf"
    path.write_text(CONF)
    init_config(path, mail_domain="example.com", force=True)
    assert path.read_text() != CONF


# ───────────────────────────────────────── show

def test_show_names_the_layer_each_value_came_from(conf, monkeypatch, capsys):
    monkeypatch.setattr(hc.utils, "CONFIG_PATH", conf)
    cfgmod.show(hc.utils.load_config())
    out = capsys.readouterr().out

    assert "healthcheck.conf.base" in out
    # smtp.host is set in this host's conf, crontab.time only in the base.
    host_line = next(l for l in out.splitlines() if "host " in l)
    assert host_line.strip().startswith("*")
    assert "healthcheck.conf" in host_line


def test_show_diff_lists_only_what_this_host_overrides(conf, monkeypatch, capsys):
    monkeypatch.setattr(hc.utils, "CONFIG_PATH", conf)
    cfgmod.show(hc.utils.load_config(), only_overrides=True)
    out = capsys.readouterr().out

    assert "host" in out and "daily_recipients" in out
    assert "k8s_max_pods" not in out          # a base default, not an override


# ───────────────────────────────────────── prune

BASE = textwrap.dedent("""\
    [smtp]
    # The relay this reaches.
    host = relay.domain.com
    port = 25

    [email]
    daily_recipients =
    html_mode = inline
    """)


def test_prune_removes_only_what_restates_the_base():
    """A config installed before the layering existed is a full copy of the old
    example: every default in it is pinned at the version it was installed
    from, so no later improvement can reach that host."""
    conf = textwrap.dedent("""\
        [smtp]
        host = mail.mine.example
        port = 25

        [email]
        html_mode = inline
        """)
    out, removed = cfgmod.prune(conf, BASE)
    cfg = parsed(out)

    assert cfg.get("smtp", "host") == "mail.mine.example"   # a real override
    assert not cfg.has_option("smtp", "port")               # restates the base
    assert not cfg.has_option("email", "html_mode")
    assert removed == ["[smtp] port", "[email] html_mode"]


def test_prune_keeps_a_setting_the_base_does_not_have():
    conf = "[crontab]\ntime = 07:00\n"
    out, removed = cfgmod.prune(conf, BASE)
    assert parsed(out).get("crontab", "time") == "07:00"
    assert removed == []


def test_prune_keeps_the_operators_own_comments():
    """Their notes are the one thing in that file nothing else records."""
    conf = textwrap.dedent("""\
        [smtp]
        # Ticket INFRA-4471 says to leave this alone.
        port = 25
        """)
    out, _ = cfgmod.prune(conf, BASE)
    assert "INFRA-4471" in out


def test_prune_takes_copied_comments_with_the_setting():
    """A comment that came from the shipped file is not the operator's note —
    leaving it behind orphans it above an unrelated key."""
    conf = textwrap.dedent("""\
        [smtp]
        # The relay this reaches.
        host = relay.domain.com
        """)
    out, removed = cfgmod.prune(conf, BASE)
    assert removed == ["[smtp] host"]
    assert "The relay this reaches." not in out


def test_prune_compares_wrapped_values_by_content_not_layout():
    base = "[email]\ndaily_recipients = a@x, b@x\n"
    conf = "[email]\ndaily_recipients = a@x,\n                   b@x\n"
    _, removed = cfgmod.prune(conf, base)
    assert removed == ["[email] daily_recipients"]


def test_prune_leaves_an_empty_override_of_a_set_default_alone():
    """`alert_recipients =` against a base that names someone is a deliberate
    'nobody' — the strongest possible override, not a redundancy."""
    base = "[email]\nalert_recipients = shipped@example.com\n"
    conf = "[email]\nalert_recipients =\n"
    out, removed = cfgmod.prune(conf, base)
    assert removed == []
    assert parsed(out).get("email", "alert_recipients") == ""


def test_prune_reports_without_writing_unless_applied(conf, monkeypatch, capsys):
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", conf)
    before = conf.read_text()

    cfgmod.prune_config(config_path=conf)
    assert conf.read_text() == before
    assert "--apply" in capsys.readouterr().out

    cfgmod.prune_config(config_path=conf, apply=True)
    assert conf.read_text() != before
    assert conf.with_suffix(".conf.bak").read_text() == before


def test_a_pruned_config_still_produces_the_same_effective_settings(
        tmp_path, monkeypatch):
    """The point of pruning is that NOTHING changes about what the host does."""
    base = tmp_path / "healthcheck.conf.base"
    base.write_text(BASE)
    conf = tmp_path / "healthcheck.conf"
    conf.write_text("[smtp]\nhost = mail.mine.example\nport = 25\n"
                    "\n[email]\nhtml_mode = inline\n")
    monkeypatch.setattr(hc.utils, "BASE_PATH", base)
    monkeypatch.setattr(hc.utils, "CONFIG_PATH", conf)
    monkeypatch.setattr(cfgmod, "BASE_PATH", base)

    before = {(s, k): hc.utils.load_config().get(s, k)
              for s in hc.utils.load_config().sections()
              for k in hc.utils.load_config().options(s)}

    cfgmod.prune_config(config_path=conf, apply=True)

    after = {(s, k): hc.utils.load_config().get(s, k)
             for s in hc.utils.load_config().sections()
             for k in hc.utils.load_config().options(s)}
    assert before == after


def test_prune_drops_a_section_left_with_no_settings():
    """Every key under [email] restated the base, so the header and the prose
    introducing it now describe nothing."""
    base = textwrap.dedent("""\
        [smtp]
        host = relay.domain.com

        [email]
        # Two lists, two different purposes.
        html_mode = inline
        """)
    conf = textwrap.dedent("""\
        [smtp]
        host = mail.mine.example

        [email]
        # Two lists, two different purposes.
        html_mode = inline
        """)
    out, _ = cfgmod.prune(conf, base)
    assert "[email]" not in out
    assert "Two lists" not in out
    assert parsed(out).get("smtp", "host") == "mail.mine.example"


def test_prune_keeps_a_section_that_still_holds_an_override():
    base = "[smtp]\nhost = relay.domain.com\nport = 25\n"
    conf = "[smtp]\nhost = mail.mine.example\nport = 25\n"
    out, _ = cfgmod.prune(conf, base)
    assert "[smtp]" in out
    assert parsed(out).get("smtp", "host") == "mail.mine.example"


def test_prune_keeps_an_operators_note_in_an_emptied_section():
    base = "[email]\nhtml_mode = inline\n"
    conf = "[email]\n# Ask INFRA before changing any of this.\nhtml_mode = inline\n"
    out, _ = cfgmod.prune(conf, base)
    assert "Ask INFRA" in out
