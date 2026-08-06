"""Unit tests for hc.report — HTML/text/plain/JSON generation."""

import json

from hc.models import OK, CAUTION, UNHEALTHY, Section
from hc.report import (
    _COLORS, generate_html, generate_plain, generate_text, generate_json,
)


def make_sections():
    s1 = Section("System Information")
    s1.add("Hostname", "host.example.com")
    s1.add("── Header ──", "", "info")
    s1.add("Kernel", "6.1.0", UNHEALTHY)

    s2 = Section("CPU Load")
    s2.add("CPU 0", "95.0% used", UNHEALTHY)
    s2.add("Load Average", "4.00", CAUTION, detail="Thresholds: caution >= 4.0")
    s2.alert("Load avg 4.00 exceeds 4.0")
    s2.need_tool("mpstat", rhel_pkg="sysstat", deb_pkg="sysstat")
    return [s1, s2]


def test_colors_cover_all_statuses():
    assert set(_COLORS) == {"ok", "info", "caution", "unhealthy"}
    for info in _COLORS.values():
        assert info["fg"] and info["bg"] and info["label"] and info["sym"]


def test_html_contains_overall_and_escaping():
    s = Section("XSS")
    s.add("<script>alert(1)</script>", "<b>value</b>", CAUTION)
    out = generate_html([s], UNHEALTHY)
    assert "<!DOCTYPE html>" in out
    assert "Overall Status:" in out
    assert "✖ UNHEALTHY" in out
    assert "&lt;script&gt;" in out
    assert "&lt;b&gt;value&lt;/b&gt;" in out
    assert "<script>alert" not in out


def test_html_multi_section_and_version():
    out = generate_html(make_sections(), CAUTION)
    assert "System Information" in out
    assert "CPU Load" in out
    assert "Linux Health Check v" in out


def test_text_report_readable():
    out = generate_text(make_sections(), UNHEALTHY)
    assert "Overall Status:" in out
    assert "✗" in out                      # UNHEALTHY symbol
    assert "System Information" in out
    assert "Kernel" in out
    assert "<" not in out


def test_plain_report_compact():
    out = generate_plain(make_sections(), CAUTION)
    assert out.startswith("Linux Health Check v")
    assert "Overall Status : CAUTION" in out
    assert "[UNHEALTHY]" in out
    assert "Kernel" in out


def test_json_structure():
    out = generate_json(make_sections(), UNHEALTHY)
    payload = json.loads(out)
    assert payload["tool"] == "linux-health-check"
    assert payload["overall"] == "unhealthy"
    assert payload["version"]
    assert payload["hostname"]
    assert payload["timestamp"]
    assert len(payload["sections"]) == 2

    cpu = payload["sections"][1]
    assert cpu["title"] == "CPU Load"
    assert cpu["status"] == "unhealthy"
    assert cpu["alerts"] == ["Load avg 4.00 exceeds 4.0"]
    assert cpu["missing_tools"] == [
        {"tool": "mpstat", "rhel_pkg": "sysstat", "deb_pkg": "sysstat", "optional": False}
    ]
    rows = {r["label"]: r for r in cpu["rows"]}
    assert rows["Load Average"]["status"] == "caution"
    assert rows["Load Average"]["detail"] == "Thresholds: caution >= 4.0"


def test_json_excludes_separators():
    out = generate_json(make_sections(), OK)
    payload = json.loads(out)
    sysinfo = payload["sections"][0]
    labels = [r["label"] for r in sysinfo["rows"]]
    assert "── Header ──" not in labels
    assert "Hostname" in labels and "Kernel" in labels
