from typing import Dict, List, Tuple

OK        = "ok"
INFO      = "info"
CAUTION   = "caution"
UNHEALTHY = "unhealthy"
_RANK     = {OK: 0, INFO: 1, CAUTION: 2, UNHEALTHY: 3}


def _worse(a: str, b: str) -> str:
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


class Row:
    __slots__ = ("label", "value", "status", "detail", "is_separator",
                 "meter", "delta")

    def __init__(self, label: str, value: str, status: str = OK,
                 detail: str = "", is_separator: bool = False,
                 meter=None, delta: str = ""):
        self.label        = str(label)
        self.value        = str(value)
        self.status       = status
        self.detail       = str(detail)
        self.is_separator = is_separator
        # 0-100 — renders a usage bar beside the value. Only set it where a
        # percentage is the point of the row (disk, memory).
        self.meter        = meter
        # Signed change since the previous run, e.g. "+3 since yesterday".
        self.delta        = str(delta)


class Section:
    def __init__(self, title: str):
        self.title                     = title
        self.status                    = OK
        # False when the subject of the check does not exist on this host
        # (no Docker, no fail2ban). Such sections are collapsed into a single
        # "not applicable" line rather than each taking a whole panel.
        self.applicable                = True
        self.rows: "List[Row]"         = []
        # (status, message) pairs — see alert() below.
        self.alert_lines: "List[Tuple[str, str]]" = []
        self.missing_tools: "List[Dict[str, object]]" = []

    def add(self, label: str, value: str, status: str = OK, detail: str = "",
            meter=None, delta: str = "") -> None:
        sep = label.startswith("──")
        self.rows.append(Row(label, value, status, detail, sep, meter, delta))
        if not sep:
            self.status = _worse(self.status, status)

    def not_applicable(self, reason: str) -> None:
        """Mark this check as having nothing to inspect on this host."""
        self.applicable = False
        self.add(self.title, reason, INFO)

    def alert(self, msg: str, status: str = UNHEALTHY) -> None:
        """Record a condition worth notifying a human about.

        Stored as (status, msg) so the notifier can decide who to wake:
        CAUTION reaches the broad list only when it is new, UNHEALTHY
        is always escalated.
        """
        self.alert_lines.append((status, msg))
        self.status = _worse(self.status, status)

    def need_tool(self, tool: str, rhel_pkg: str = "", deb_pkg: str = "",
                  optional: bool = False) -> None:
        self.missing_tools.append({
            "tool":     tool,
            "rhel_pkg": rhel_pkg or tool,
            "deb_pkg":  deb_pkg or tool,
            "optional": optional,
        })
