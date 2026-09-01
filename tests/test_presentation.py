"""Layout, density and accessibility of the rendered report.

These pin the properties that make the report readable — worst-first ordering,
a bounded line width, colour never carrying meaning alone — rather than exact
wording, which is free to change.
"""

import re

import pytest

from hc.models import CAUTION, INFO, OK, UNHEALTHY, Section
from hc.report import (
    W, _COLORS, _text_meter, _wrap, generate_html, generate_plain,
    generate_text, order_sections,
)


def sec(title, *rows, applicable=True):
    s = Section(title)
    if not applicable:
        s.not_applicable("Not installed")
        return s
    for label, value, status in rows:
        s.add(label, value, status)
    return s


# ── worst first ─────────────────────────────────────────────────────────────

def test_sections_are_ordered_worst_first():
    ok      = sec("Network", ("a", "1", OK))
    caution = sec("Disk",    ("b", "2", CAUTION))
    bad     = sec("Services", ("c", "3", UNHEALTHY))
    info    = sec("Ports",   ("d", "4", INFO))
    assert [s.title for s in order_sections([ok, caution, bad, info])] == \
           ["Services", "Disk", "Ports", "Network"]


def test_ordering_is_stable_within_a_severity_band():
    a, b, c = sec("A", ("x", "1", OK)), sec("B", ("x", "1", OK)), sec("C", ("x", "1", OK))
    assert [s.title for s in order_sections([a, b, c])] == ["A", "B", "C"]


def test_the_worst_section_appears_before_the_others_in_the_output():
    ok  = sec("Network Aardvark", ("a", "1", OK))
    bad = sec("Zebra Services",   ("c", "3", UNHEALTHY))
    text = generate_text([ok, bad], UNHEALTHY)
    # skip the summary block, which lists everything
    body = text.split("Legend:", 1)[1]
    assert body.index("Zebra Services") < body.index("Network Aardvark")


# ── line-width discipline in the text report ────────────────────────────────

LONG_LABEL = "k8s_POD_helm-install-traefik-crd-wzsss_kube-system_429d29a9-55c3-4354-855b"
LONG_VALUE = "Exited (0) About an hour ago  [rancher/mirrored-pause:3.6-with-a-very-long-tag]"


# A hostname long enough to blow the width on its own. Cloud FQDNs really do
# look like this — the CI runner that first caught the header overflow was
# `runnervmgx7h7.llrwocqjdogehnr5ig5nnjbyxf.xx.internal.cloudapp.net`.
LONG_HOST = "srv0123456789.subdomain-that-goes-on.internal.example.cloudapp.net"


@pytest.fixture
def long_hostname(monkeypatch):
    """Pin the hostname so the width tests measure the renderer, not the
    machine they happen to run on. Without this the suite passed on a laptop
    called 'pgtdt65297' and failed on a CI runner, which is the least useful
    way to find out."""
    import hc.report
    monkeypatch.setattr(hc.report.socket, "getfqdn", lambda: LONG_HOST)


@pytest.mark.parametrize("renderer", [generate_text, generate_plain])
def test_no_line_exceeds_the_declared_width(renderer, long_hostname):
    """The old renderer claimed 76 columns and emitted 155."""
    s = sec("Docker", (LONG_LABEL, LONG_VALUE, INFO), ("short", "value", OK))
    for line in renderer([s], INFO).splitlines():
        assert len(line) <= W, f"{len(line)} cols: {line!r}"


@pytest.mark.parametrize("renderer", [generate_text, generate_plain])
def test_a_long_hostname_keeps_the_part_that_identifies_the_machine(renderer,
                                                                    long_hostname):
    """Truncation drops the domain tail, not the leading label — 'srv0123456789'
    is the half that tells you which server you are reading about."""
    out = renderer([sec("Docker", ("a", "1", OK))], OK)
    assert "srv0123456789" in out


@pytest.mark.parametrize("renderer", [generate_text, generate_plain])
def test_a_short_hostname_is_never_truncated(renderer):
    out = renderer([sec("Docker", ("a", "1", OK))], OK)
    assert "…" not in out.splitlines()[0] + out.splitlines()[1]


def test_flagged_rows_wrap_their_value_in_full():
    """Where there is a problem, the reader gets the whole value."""
    s = sec("Docker", ("name", LONG_VALUE, CAUTION))
    lines = [l for l in generate_text([s], CAUTION).splitlines() if l.strip()]
    wrapped = [l for l in lines if l.startswith(" " * 20) and "rancher" in l]
    assert wrapped, "a flagged value should continue on an indented line"


def test_healthy_rows_get_exactly_one_line():
    """An inventory entry does not earn three lines of container name."""
    s = sec("Docker", ("name", LONG_VALUE, INFO))
    body = generate_text([s], INFO).split("Legend:", 1)[1]
    rows = [l for l in body.splitlines() if "name" in l and "Exited" in l]
    assert len(rows) == 1, "an INFO row should occupy a single line"
    assert rows[0].endswith("…"), "the over-long value should be truncated, not wrapped"


def test_wrap_splits_words_longer_than_the_column():
    out = _wrap("x" * 200, 40, "")
    assert all(len(l) <= 40 for l in out)
    assert "".join(out).count("…") >= 1


def test_wrap_handles_empty_input():
    assert _wrap("", 40, "") == [""]


# ── meters ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pct,filled", [(0, 0), (50, 5), (91, 9), (100, 10)])
def test_text_meter_fills_proportionally(pct, filled):
    bar = _text_meter(pct)
    assert bar.count("#") == filled
    assert len(bar) == 12          # 10 cells plus the brackets


@pytest.mark.parametrize("pct", [-20, 150])
def test_text_meter_clamps_out_of_range_values(pct):
    bar = _text_meter(pct)
    assert 0 <= bar.count("#") <= 10


def test_html_meter_renders_for_rows_that_have_one():
    s = Section("Disk Usage")
    s.add("/var", "91% used", CAUTION, meter=91)
    html = generate_html([s], CAUTION)
    assert "width:91%" in html


def test_rows_without_a_meter_render_no_bar():
    s = Section("Info")
    s.add("Kernel", "6.6.0", OK)
    assert "width:" not in generate_html([s], OK).split("Kernel")[1][:400]


# ── deltas ──────────────────────────────────────────────────────────────────

def test_delta_is_shown_next_to_the_value():
    s = Section("Disk Usage")
    s.add("/var", "91% used", CAUTION, meter=91, delta="+3.0 pts since last run")
    assert "+3.0 pts since last run" in generate_html([s], CAUTION)
    assert "+3.0 pts since last run" in generate_text([s], CAUTION)


# ── not-applicable sections ─────────────────────────────────────────────────

def test_not_applicable_sections_are_collapsed_to_one_line():
    docker = sec("Docker Containers", applicable=False)
    disk   = sec("Disk Usage", ("/", "40%", OK))
    text   = generate_text([disk, docker], OK)
    assert "Not present on this host: Docker Containers" in text
    # and it does not get a panel of its own
    assert text.count("Docker Containers") == 1


def test_not_applicable_sections_are_collapsed_in_html():
    docker = sec("Docker Containers", applicable=False)
    disk   = sec("Disk Usage", ("/", "40%", OK))
    html   = generate_html([disk, docker], OK)
    assert "Not present on this host" in html
    assert html.count("Docker Containers") == 1


# ── summary grid ────────────────────────────────────────────────────────────

def test_summary_lists_every_applicable_section():
    a, b = sec("Disk Usage", ("/", "40%", OK)), sec("Memory Usage", ("Mem", "60%", OK))
    text = generate_text([a, b], OK)
    summary = text.split("AT A GLANCE", 1)[1].split("Legend:", 1)[0]
    assert "Disk Usage" in summary and "Memory Usage" in summary


def test_summary_counts_the_rows_needing_review():
    s = sec("Disk", ("/var", "91%", CAUTION), ("/tmp", "97%", UNHEALTHY), ("/", "10%", OK))
    summary = generate_text([s], UNHEALTHY).split("AT A GLANCE", 1)[1].split("Legend:", 1)[0]
    assert "2 to review" in summary


def test_html_summary_is_present():
    s = sec("Disk", ("/", "40%", OK))
    assert "At a glance" in generate_html([s], OK)


# ── colour never carries meaning alone ──────────────────────────────────────

@pytest.mark.parametrize("status", [OK, INFO, CAUTION, UNHEALTHY])
def test_every_status_has_a_glyph_and_a_word(status):
    c = _COLORS[status]
    assert c["sym"] and c["word"]
    assert c["word"].isupper()


@pytest.mark.parametrize("status", [OK, INFO, CAUTION, UNHEALTHY])
def test_html_status_chips_pair_colour_with_the_word(status):
    s = Section("T")
    s.add("row", "value", status)
    html = generate_html([s], status)
    assert _COLORS[status]["word"] in html


def test_text_report_has_a_legend():
    s = sec("Disk", ("/", "40%", OK))
    text = generate_text([s], OK)
    assert "Legend:" in text
    for status in (OK, INFO, CAUTION, UNHEALTHY):
        assert _COLORS[status]["word"] in text


def test_ok_rows_are_not_painted_with_a_status_colour():
    """163 green rows are what made the 4 amber ones invisible."""
    s = Section("T")
    s.add("fine", "all good", OK)
    html = generate_html([s], OK)
    row = html.split("fine")[1][:300]
    assert _COLORS[OK]["bg"] not in row, "OK rows should not carry a tinted background"


def test_flagged_rows_are_tinted():
    s = Section("T")
    s.add("bad", "91% used", CAUTION)
    html = generate_html([s], CAUTION)
    assert _COLORS[CAUTION]["bg"] in html


# ── accessibility of the palette itself ─────────────────────────────────────

def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    channels = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("status", [OK, INFO, CAUTION, UNHEALTHY])
def test_status_text_meets_wcag_aa_on_its_own_chip(status):
    """Amber on white is 1.79:1 — unreadable. Each status carries a darker ink."""
    c = _COLORS[status]
    assert _contrast(c["fg"], c["bg"]) >= 4.5


@pytest.mark.parametrize("status", [OK, INFO, CAUTION, UNHEALTHY])
def test_status_text_meets_wcag_aa_on_the_page(status):
    assert _contrast(_COLORS[status]["fg"], "#ffffff") >= 4.5


def test_every_colour_token_is_a_valid_hex():
    for c in _COLORS.values():
        for key in ("fg", "bg", "dot"):
            assert re.fullmatch(r"#[0-9a-f]{6}", c[key]), (key, c[key])


# ── html hygiene ────────────────────────────────────────────────────────────

def test_html_opts_out_of_client_side_auto_darkening():
    """A half-dark theme is worse than none.

    The previous version darkened the surfaces via prefers-color-scheme but
    could not reach the colours set inline on every row, so section names kept
    their dark ink on a now-dark background and vanished.
    """
    html = generate_html([sec("T", ("a", "1", OK))], OK)
    assert 'name="color-scheme" content="only light"' in html
    assert "color-scheme: only light" in html
    assert "prefers-color-scheme" not in html, "no partial dark theme"


def test_no_element_carries_text_colour_without_a_background():
    """An inherited background is what disappears when a client force-inverts."""
    import re
    s = Section("Disk Usage")
    s.add("/var", "91% used", CAUTION, meter=91)
    s.add("/", "40% used", OK)
    html = generate_html([s], CAUTION)
    body = html.split("<body", 1)[1]
    naked = []
    for m in re.finditer(r'<(td|tr|div)\b([^>]*)>', body):
        tag, attrs = m.group(1), m.group(2)
        style = re.search(r'style="([^"]*)"', attrs)
        if not style or not re.search(r'(^|;)\s*color:', style.group(1)):
            continue
        has_bg    = "background" in style.group(1)
        has_class = "hc-ink" in attrs or "hc-muted" in attrs or "hc-row" in attrs
        if not has_bg and not has_class:
            naked.append(f"<{tag}> {style.group(1)[:60]}")
    assert not naked, "these would vanish on a force-inverting client: " + str(naked)


def test_unflagged_rows_are_pinned_white_but_flagged_rows_keep_their_tint():
    s = Section("T")
    s.add("fine", "ok", OK)
    s.add("bad", "91%", CAUTION)
    html = generate_html([s], CAUTION)
    assert 'class="hc-row"' in html          # pinned to white by the light lock
    assert 'class="hc-flag"' in html         # keeps its amber tint
    assert _COLORS[CAUTION]["bg"] in html


def test_html_sets_every_colour_inline_so_style_stripping_clients_still_work():
    """Many mail clients drop <style>; the inline light design must stand alone."""
    html = generate_html([sec("T", ("a", "1", CAUTION))], CAUTION)
    body = html.split("<body", 1)[1]
    assert "style=" in body
    assert _COLORS[CAUTION]["bg"] in body


def test_unlabelled_rows_do_not_pad_a_hole_before_the_value():
    """Inventory rows (ports, log samples) have no label."""
    s = Section("Listening Sockets")
    s.add("", "tcp *:443", INFO)
    line = [l for l in generate_text([s], INFO).splitlines() if "tcp *:443" in l][0]
    assert line == "  ·  tcp *:443", repr(line)


def test_labelled_rows_still_align_in_a_column():
    s = Section("T")
    s.add("Kernel", "6.6.0", OK)
    s.add("Uptime", "3 hours", OK)
    lines = [l for l in generate_text([s], OK).splitlines()
             if "6.6.0" in l or "3 hours" in l]
    assert len({l.index(v) for l, v in zip(lines, ("6.6.0", "3 hours"))}) == 1
