OK        = "ok"
INFO      = "info"
CAUTION   = "caution"
UNHEALTHY = "unhealthy"
_RANK     = {OK: 0, INFO: 1, CAUTION: 2, UNHEALTHY: 3}


def _worse(a: str, b: str) -> str:
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


class Row:
    __slots__ = ("label", "value", "status", "detail", "is_separator")

    def __init__(self, label: str, value: str, status: str = OK,
                 detail: str = "", is_separator: bool = False):
        self.label        = str(label)
        self.value        = str(value)
        self.status       = status
        self.detail       = str(detail)
        self.is_separator = is_separator


class Section:
    def __init__(self, title: str):
        self.title         = title
        self.status        = OK
        self.rows: list    = []
        self.alert_lines   = []
        self.missing_tools = []

    def add(self, label: str, value: str, status: str = OK, detail: str = "") -> None:
        sep = label.startswith("──")
        self.rows.append(Row(label, value, status, detail, sep))
        if not sep:
            self.status = _worse(self.status, status)

    def alert(self, msg: str, status: str = UNHEALTHY) -> None:
        self.alert_lines.append(msg)
        self.status = _worse(self.status, status)

    def need_tool(self, tool: str, rhel_pkg: str = "", deb_pkg: str = "",
                  optional: bool = False) -> None:
        self.missing_tools.append({
            "tool":     tool,
            "rhel_pkg": rhel_pkg or tool,
            "deb_pkg":  deb_pkg or tool,
            "optional": optional,
        })
