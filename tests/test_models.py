"""Status ranking and how a Section accumulates it."""

import pytest

from hc.models import CAUTION, INFO, OK, UNHEALTHY, Section, _worse


@pytest.mark.parametrize("a,b,expected", [
    (OK, INFO, INFO),
    (INFO, CAUTION, CAUTION),
    (CAUTION, UNHEALTHY, UNHEALTHY),
    (UNHEALTHY, OK, UNHEALTHY),
    (OK, OK, OK),
    (CAUTION, CAUTION, CAUTION),
])
def test_worse_picks_the_higher_severity(a, b, expected):
    assert _worse(a, b) == expected
    assert _worse(b, a) == expected


def test_worse_treats_unknown_status_as_lowest():
    assert _worse("nonsense", CAUTION) == CAUTION


def test_section_starts_ok_and_absorbs_the_worst_row():
    s = Section("t")
    assert s.status == OK
    s.add("a", "1", INFO)
    s.add("b", "2", CAUTION)
    s.add("c", "3", OK)
    assert s.status == CAUTION


def test_separator_rows_do_not_affect_section_status():
    s = Section("t")
    s.add("── Heading ──", "", UNHEALTHY)
    assert s.status == OK
    assert s.rows[0].is_separator


def test_alert_stores_status_alongside_the_message():
    s = Section("t")
    s.alert("something broke", CAUTION)
    assert s.alert_lines == [(CAUTION, "something broke")]


def test_alert_defaults_to_unhealthy():
    s = Section("t")
    s.alert("very bad")
    assert s.alert_lines == [(UNHEALTHY, "very bad")]
    assert s.status == UNHEALTHY


def test_alert_raises_section_status():
    s = Section("t")
    s.add("a", "1", OK)
    s.alert("caution please", CAUTION)
    assert s.status == CAUTION


def test_need_tool_records_both_package_names():
    s = Section("t")
    s.need_tool("mpstat", rhel_pkg="sysstat", deb_pkg="sysstat", optional=True)
    assert s.missing_tools == [
        {"tool": "mpstat", "rhel_pkg": "sysstat", "deb_pkg": "sysstat", "optional": True}
    ]


def test_need_tool_defaults_package_names_to_the_tool_name():
    s = Section("t")
    s.need_tool("rkhunter")
    assert s.missing_tools[0]["rhel_pkg"] == "rkhunter"
    assert s.missing_tools[0]["deb_pkg"] == "rkhunter"
