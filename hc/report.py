import datetime
import html as _htmllib
import socket

from .models import OK, INFO, CAUTION, UNHEALTHY, Section
from .utils  import VERSION


_COLORS = {
    OK:        {"fg": "#1a7f37", "bg": "#d4edda", "label": "✓ OK",        "sym": "✓"},
    INFO:      {"fg": "#0c5460", "bg": "#d1ecf1", "label": "ℹ INFO",      "sym": "–"},
    CAUTION:   {"fg": "#856404", "bg": "#fff3cd", "label": "⚠ CAUTION",   "sym": "!"},
    UNHEALTHY: {"fg": "#721c24", "bg": "#f8d7da", "label": "✖ UNHEALTHY", "sym": "✗"},
}


def _section_html(sec: Section) -> str:
    c = _COLORS.get(sec.status, _COLORS[OK])
    rows_html = []

    for row in sec.rows:
        if row.is_separator:
            rows_html.append(
                f'<tr>'
                f'<td style="background:#eee;padding:0;width:28px"></td>'
                f'<td colspan="2" style="background:#eee;padding:4px 8px;'
                f'font-size:11px;color:#555;font-weight:bold">'
                f'{_htmllib.escape(row.label)}</td>'
                f'</tr>'
            )
            continue
        rc = _COLORS.get(row.status, _COLORS[OK])
        row_bg = rc["bg"] if row.status != OK else "transparent"
        detail = (f'<br><span style="font-size:10px;color:#666">'
                  f'{_htmllib.escape(row.detail)}</span>') if row.detail else ""
        rows_html.append(
            f'<tr style="background:{row_bg}">'
            f'<td style="padding:5px 6px;width:28px;text-align:center;'
            f'font-size:13px;color:{rc["fg"]};font-weight:bold">{rc["sym"]}</td>'
            f'<td style="padding:5px 8px;font-size:12px;color:#444;white-space:nowrap;min-width:180px">'
            f'{_htmllib.escape(row.label)}</td>'
            f'<td style="padding:5px 8px;font-size:12px;font-family:monospace;'
            f'color:{rc["fg"]};word-break:break-all">'
            f'{_htmllib.escape(row.value)}{detail}</td>'
            f'</tr>'
        )

    rows_content = "".join(rows_html) or '<tr><td colspan="3" style="padding:8px;color:#888">No data</td></tr>'

    return (
        f'<div style="margin-bottom:20px">'
        f'<div style="background:{c["fg"]};color:#fff;padding:8px 14px;'
        f'border-radius:4px 4px 0 0;font-size:13px;font-weight:bold;'
        f'display:flex;justify-content:space-between;align-items:center">'
        f'<span>{_htmllib.escape(sec.title)}</span>'
        f'<span style="opacity:.9;font-size:16px">{c["sym"]}</span>'
        f'</div>'
        f'<table style="width:100%;border-collapse:collapse;'
        f'border:1px solid {c["fg"]};border-top:none">'
        f'{rows_content}'
        f'</table>'
        f'</div>'
    )


def generate_html(sections: list, overall: str) -> str:
    now      = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    hostname = socket.getfqdn()
    ov       = _COLORS.get(overall, _COLORS[OK])

    body = "".join(_section_html(sec) for sec in sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Health Check — {_htmllib.escape(hostname)}</title>
</head>
<body style="font-family:Arial,Helvetica,sans-serif;margin:0;padding:0;background:#f0f0f0">
<div style="max-width:860px;margin:20px auto;background:#fff;border:1px solid #ccc;border-radius:4px;overflow:hidden">

  <div style="background:#2c3e50;color:#fff;padding:18px 24px">
    <div style="font-size:10px;opacity:.65;text-transform:uppercase;letter-spacing:.8px">
      Ministério Público do Trabalho · SPAI
    </div>
    <div style="font-size:20px;font-weight:bold;margin:4px 0 2px">Linux Health Check</div>
    <div style="font-size:13px;opacity:.8">{_htmllib.escape(hostname)} · {now}</div>
  </div>

  <div style="background:{ov["bg"]};border-left:5px solid {ov["fg"]};padding:10px 24px;
              font-size:14px;font-weight:bold;color:{ov["fg"]}">
    Overall Status: {ov["label"]}
  </div>

  <div style="padding:20px 24px">
    {body}
  </div>

  <div style="background:#f8f8f8;padding:8px 24px;font-size:10px;color:#999;
              border-top:1px solid #eee;text-align:center">
    Linux Health Check v{VERSION} · {_htmllib.escape(hostname)} · {now}
  </div>

</div>
</body>
</html>"""


def generate_plain(sections: list, overall: str) -> str:
    now      = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    hostname = socket.getfqdn()
    lines    = [
        f"Linux Health Check v{VERSION} — {hostname} — {now}",
        f"Overall Status : {overall.upper()}",
        "=" * 72,
    ]
    for sec in sections:
        lines += [f"\n{'─' * 40}", f"  {sec.title}  [{sec.status.upper()}]", f"{'─' * 40}"]
        for row in sec.rows:
            if row.is_separator:
                lines.append(f"  {row.label}")
            else:
                lines.append(f"  {row.label:<32} {row.value}")
    lines += ["", "=" * 72, f"Linux Health Check v{VERSION}"]
    return "\n".join(lines)
