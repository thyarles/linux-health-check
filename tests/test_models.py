"""Unit tests for hc.models — status ranking and Section aggregation."""

from hc.models import OK, INFO, CAUTION, UNHEALTHY, _worse, Section


def test_worse_ranking():
    assert _worse(OK, CAUTION) == CAUTION
    assert _worse(CAUTION, OK) == CAUTION
    assert _worse(UNHEALTHY, CAUTION) == UNHEALTHY
    assert _worse(OK, OK) == OK
    assert _worse(INFO, OK) == INFO
    assert _worse(UNHEALTHY, OK) == UNHEALTHY


def test_worse_unknown_status_falls_back():
    # Unknown statuses rank as 0; ties return the first argument.
    assert _worse("bogus", OK) == "bogus"
    assert _worse(OK, "bogus") == OK
    assert _worse("bogus", UNHEALTHY) == UNHEALTHY


def test_section_starts_ok():
    s = Section("Test")
    assert s.status == OK
    assert s.rows == []
    assert s.alert_lines == []
    assert s.missing_tools == []


def test_section_add_aggregates_worst_status():
    s = Section("Test")
    s.add("a", "1", OK)
    s.add("b", "2", CAUTION)
    assert s.status == CAUTION
    s.add("c", "3", UNHEALTHY)
    assert s.status == UNHEALTHY


def test_section_separator_does_not_affect_status():
    s = Section("Test")
    s.add("── Header ──", "", INFO)
    assert s.status == OK


def test_section_alert_raises_status():
    s = Section("Test")
    s.alert("something bad")
    assert s.status == UNHEALTHY
    assert s.alert_lines == ["something bad"]
    s.alert("cautionary", CAUTION)
    assert s.status == UNHEALTHY


def test_need_tool_records_entry():
    s = Section("Test")
    s.need_tool("mpstat", rhel_pkg="sysstat", deb_pkg="sysstat")
    assert s.missing_tools == [
        {"tool": "mpstat", "rhel_pkg": "sysstat", "deb_pkg": "sysstat", "optional": False}
    ]
    s.need_tool("docker", deb_pkg="docker.io", optional=True)
    assert s.missing_tools[1]["optional"] is True
    assert s.missing_tools[1]["rhel_pkg"] == "docker"  # falls back to tool name


def test_row_is_separator_detection():
    s = Section("Test")
    s.add("── Top 10 ──", "", INFO)
    s.add("PID 1", "x", INFO)
    assert s.rows[0].is_separator is True
    assert s.rows[1].is_separator is False
