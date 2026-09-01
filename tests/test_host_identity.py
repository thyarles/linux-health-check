"""Which name a host calls itself.

Written from a real incident: three RKE2 nodes (mpt-kpm01/02/03) sit behind the
shared name rancher-mgmt.mpt.mp.br. `socket.getfqdn()` reverse-resolves the
kernel hostname and returns the *first* name it finds, which on those nodes came
back as `rancher-mgmt.mpt.mp.br` — identical report headers, identical subject
lines, colliding report filenames, and three machines' alerts reading as one
host flapping.

It is also not stable. Asked again on mpt-kpm03 it answered
`MPT-KPM03.mpt.mp.br`, upper-cased, on a host whose own name is lowercase. A
resolver's answer depends on which node currently holds the VIP, on DNS, and on
cache state; an identity that drifts with any of those is worse than a wrong
one, because the same machine reports under different names on different days.
"""

import pytest

import hc.utils as u
from hc.utils import host_label, host_mail_domain


@pytest.fixture(autouse=True)
def no_override(monkeypatch):
    """Most tests here exercise the resolution, not the config escape hatch."""
    monkeypatch.setattr(u, "_HOST_OVERRIDE", "")


def _names(monkeypatch, kernel, resolved):
    monkeypatch.setattr(u.socket, "gethostname", lambda: kernel)
    monkeypatch.setattr(u.socket, "getfqdn", lambda *a: resolved)


# ─────────────────────────────────────────────────────────────────────────────
# The incident
# ─────────────────────────────────────────────────────────────────────────────

def test_a_shared_vip_name_never_replaces_the_machine_name(monkeypatch):
    _names(monkeypatch, "mpt-kpm03", "rancher-mgmt.mpt.mp.br")
    assert host_label() == "mpt-kpm03"


@pytest.mark.parametrize("kernel", ["mpt-kpm01", "mpt-kpm02", "mpt-kpm03"])
def test_each_node_behind_one_vip_reports_a_distinct_name(monkeypatch, kernel):
    """The point of the fix: three reports, three identities."""
    _names(monkeypatch, kernel, "rancher-mgmt.mpt.mp.br")
    assert host_label() == kernel


# ─────────────────────────────────────────────────────────────────────────────
# ...without throwing away a legitimate FQDN
# ─────────────────────────────────────────────────────────────────────────────

def test_a_matching_fqdn_is_kept_because_it_only_adds_a_domain(monkeypatch):
    _names(monkeypatch, "web01", "web01.example.com")
    assert host_label() == "web01.example.com"


def test_the_match_is_case_insensitive(monkeypatch):
    """Resolvers are free to mangle case; a bare kernel name still gets its
    domain from a differently-cased FQDN for the same machine."""
    _names(monkeypatch, "web01", "WEB01.example.com")
    assert host_label() == "WEB01.example.com"


def test_an_already_qualified_kernel_name_is_used_verbatim(monkeypatch):
    """Real output from mpt-kpm03: `hostname` is already fully qualified and
    getfqdn() answers with the same name UPPER-CASED. The kernel's spelling is
    the one the operator sees, so nothing is borrowed from the resolver."""
    _names(monkeypatch, "mpt-kpm03.mpt.mp.br", "MPT-KPM03.mpt.mp.br")
    assert host_label() == "mpt-kpm03.mpt.mp.br"


def test_a_qualified_kernel_name_ignores_the_resolver_entirely(monkeypatch):
    """getfqdn() reverse-resolves, so on a node that sometimes holds a floating
    VIP its answer changes with the VIP. An identity that drifts is useless, so
    a qualified kernel name never consults it."""
    _names(monkeypatch, "mpt-kpm03.mpt.mp.br", "rancher-mgmt.mpt.mp.br")
    assert host_label() == "mpt-kpm03.mpt.mp.br"


def test_the_same_host_reports_the_same_name_whoever_holds_the_vip(monkeypatch):
    _names(monkeypatch, "mpt-kpm03.mpt.mp.br", "rancher-mgmt.mpt.mp.br")
    holding_vip = host_label()
    _names(monkeypatch, "mpt-kpm03.mpt.mp.br", "MPT-KPM03.mpt.mp.br")
    assert host_label() == holding_vip


def test_an_unresolvable_host_falls_back_to_the_kernel_name(monkeypatch):
    """getfqdn() returns the input unchanged when nothing resolves."""
    _names(monkeypatch, "isolated-box", "isolated-box")
    assert host_label() == "isolated-box"


def test_an_empty_kernel_hostname_falls_back_to_the_resolved_one(monkeypatch):
    _names(monkeypatch, "", "somehow.example.com")
    assert host_label() == "somehow.example.com"


# ─────────────────────────────────────────────────────────────────────────────
# Config override
# ─────────────────────────────────────────────────────────────────────────────

def test_the_config_override_wins(monkeypatch):
    _names(monkeypatch, "mpt-kpm03", "rancher-mgmt.mpt.mp.br")
    monkeypatch.setattr(u, "_HOST_OVERRIDE", "kpm03.prod.example.com")
    assert host_label() == "kpm03.prod.example.com"


def test_load_config_publishes_the_override(monkeypatch, tmp_path):
    conf = tmp_path / "healthcheck.conf"
    conf.write_text("[general]\nhostname = named-by-hand\n")
    monkeypatch.setattr(u, "CONFIG_PATH", conf)
    u.load_config()
    try:
        assert host_label() == "named-by-hand"
    finally:
        u._HOST_OVERRIDE = ""


def test_load_config_clears_a_stale_override(monkeypatch, tmp_path):
    monkeypatch.setattr(u, "_HOST_OVERRIDE", "left-over")
    monkeypatch.setattr(u, "CONFIG_PATH", tmp_path / "nonexistent.conf")
    u.load_config()
    assert u._HOST_OVERRIDE == ""


# ─────────────────────────────────────────────────────────────────────────────
# The default From: address
# ─────────────────────────────────────────────────────────────────────────────

def test_the_mail_host_borrows_a_domain_from_the_resolved_name(monkeypatch):
    """A From: with no domain is rejected by some relays, so a bare kernel
    name gets the resolved name's domain — still distinct per host."""
    _names(monkeypatch, "mpt-kpm03", "rancher-mgmt.mpt.mp.br")
    assert host_mail_domain() == "mpt-kpm03.mpt.mp.br"


def test_the_mail_host_leaves_an_already_qualified_name_alone(monkeypatch):
    _names(monkeypatch, "web01", "web01.example.com")
    assert host_mail_domain() == "web01.example.com"


def test_the_mail_host_copes_with_no_domain_anywhere(monkeypatch):
    _names(monkeypatch, "isolated-box", "isolated-box")
    assert host_mail_domain() == "isolated-box"


# ─────────────────────────────────────────────────────────────────────────────
# It reaches the places that identify the host
# ─────────────────────────────────────────────────────────────────────────────

def test_the_report_header_uses_the_machine_name(monkeypatch):
    from hc.models import OK, Section
    from hc.report import generate_html, generate_text
    monkeypatch.setattr("hc.report.host_label", lambda: "mpt-kpm03")
    s = Section("Disk Usage")
    s.add("/", "40% used", OK)
    for out in (generate_text([s], OK), generate_html([s], OK)):
        assert "mpt-kpm03" in out
        assert "rancher-mgmt" not in out


def test_system_info_shows_the_resolved_name_when_it_disagrees(monkeypatch, shell):
    """The mismatch is invisible until something prints both, so the report
    says which shared name the host resolves to and that it is not its identity."""
    import hc.checks
    monkeypatch.setattr(hc.checks, "host_label", lambda: "mpt-kpm03")
    monkeypatch.setattr(hc.checks.socket, "getfqdn", lambda *a: "rancher-mgmt.mpt.mp.br")
    monkeypatch.setattr(hc.checks.socket, "gethostname", lambda: "mpt-kpm03")
    rows = {r.label: r.value for r in hc.checks.check_system_info().rows}
    assert rows["Hostname"] == "mpt-kpm03"
    assert "rancher-mgmt.mpt.mp.br" in rows["Resolved name"]


def test_system_info_stays_quiet_when_the_names_agree(monkeypatch, shell):
    import hc.checks
    monkeypatch.setattr(hc.checks, "host_label", lambda: "web01.example.com")
    monkeypatch.setattr(hc.checks.socket, "getfqdn", lambda *a: "web01.example.com")
    monkeypatch.setattr(hc.checks.socket, "gethostname", lambda: "web01")
    labels = [r.label for r in hc.checks.check_system_info().rows]
    assert "Resolved name" not in labels
