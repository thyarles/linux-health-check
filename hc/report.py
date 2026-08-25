import datetime
import html as _htmllib
import socket

from .models import OK, INFO, CAUTION, UNHEALTHY, Section, _RANK
from .utils  import VERSION


# ─────────────────────────────────────────────────────────────────────────────
# PALETTE
#
# Status colours are reserved: they mean state, nothing else, and they always
# ship with a glyph AND the status word so meaning never rests on hue alone
# (which also covers colour-blind readers and clients that strip colour).
#
# Hues are the standard status steps — good #0ca30c, warning #fab219,
# critical #d03b3b. Those are mark colours, not text colours: at text size
# amber on white is 1.79:1, far below readable. So each status carries a
# darker `fg` ink for text and a pale `bg` tint for chips; every fg/bg pair
# below was measured at >= 4.9:1, comfortably past WCAG AA.
#
# INFO is deliberately NOT given a hue. It is not a state anyone acts on, and
# colouring it competes with the rows that matter.
# ─────────────────────────────────────────────────────────────────────────────

_COLORS = {
    OK:        {"fg": "#0a7a0a", "bg": "#e8f6e8", "dot": "#0ca30c",
                "label": "✓ OK",        "word": "OK",        "sym": "✓"},
    INFO:      {"fg": "#52514e", "bg": "#f1f1ef", "dot": "#8a8985",
                "label": "· INFO",      "word": "INFO",      "sym": "·"},
    CAUTION:   {"fg": "#8a5a00", "bg": "#fdf3dd", "dot": "#fab219",
                "label": "⚠ CAUTION",   "word": "CAUTION",   "sym": "!"},
    UNHEALTHY: {"fg": "#b02b2b", "bg": "#fbe9e9", "dot": "#d03b3b",
                "label": "✖ UNHEALTHY", "word": "UNHEALTHY", "sym": "✕"},
}

_INK        = "#1a1a19"      # primary text
_INK_MUTED  = "#6b6a66"      # secondary text
_RULE       = "#e3e3e0"      # hairlines
_SURFACE    = "#ffffff"
_SURFACE_2  = "#f7f7f5"


def _c(status: str) -> dict:
    return _COLORS.get(status, _COLORS[OK])


def _now() -> str:
    """Timestamp with the zone, so reports from several hosts can be compared."""
    now = datetime.datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M %Z").strip()


def order_sections(sections: list) -> list:
    """Worst first, original order preserved within a severity band.

    A reader should not have to scroll past nine green panels to reach the one
    that needs them.
    """
    return sorted(sections, key=lambda s: -_RANK.get(s.status, 0))


def _attention_rows(sec: Section) -> list:
    return [r for r in sec.rows
            if not r.is_separator and r.status in (CAUTION, UNHEALTHY)]


def _chip(status: str, extra: str = "") -> str:
    """Status pill: coloured dot + the word. Never colour alone."""
    c = _c(status)
    return (
        f'<span style="display:inline-block;padding:2px 9px 3px;border-radius:11px;'
        f'background:{c["bg"]};color:{c["fg"]};font-size:11px;font-weight:700;'
        f'letter-spacing:.03em;white-space:nowrap">'
        f'<span style="color:{c["dot"]}">&#9679;</span>&nbsp;{c["word"]}{extra}</span>'
    )


def _meter(pct: float, status: str) -> str:
    """Thin usage bar. The fill carries severity; the track is a pale step of it."""
    c = _c(status)
    pct = max(0.0, min(100.0, float(pct)))
    return (
        f'<span style="display:inline-block;width:96px;height:6px;border-radius:3px;'
        f'background:{c["bg"]};vertical-align:middle;margin-right:8px;'
        f'overflow:hidden;line-height:6px;font-size:0">'
        f'<span style="display:inline-block;width:{pct:.0f}%;height:6px;'
        f'border-radius:3px;background:{c["dot"]}"></span></span>'
    )


def _summary_html(sections: list) -> str:
    """One screen that answers 'what is the state of this machine?'.

    Without it a reader has to scroll the whole report to find out whether
    anything is wrong at all.
    """
    cells = []
    for sec in sections:
        if not sec.applicable:
            continue
        n = len(_attention_rows(sec))
        note = (f'<span style="color:{_INK_MUTED};font-size:11px">&nbsp;{n}</span>'
                if n else "")
        cells.append(
            f'<tr class="hc-row" style="background:{_SURFACE}">'
            f'<td class="hc-ink" style="padding:5px 10px 5px 0;font-size:12px;'
            f'color:{_INK};background:{_SURFACE};'
            f'border-bottom:1px solid {_RULE}">{_htmllib.escape(sec.title)}</td>'
            f'<td style="padding:5px 0;text-align:right;white-space:nowrap;'
            f'background:{_SURFACE};'
            f'border-bottom:1px solid {_RULE}">{_chip(sec.status)}{note}</td>'
            f'</tr>'
        )
    skipped = [s.title for s in sections if not s.applicable]
    foot = ""
    if skipped:
        foot = (f'<div style="margin-top:10px;font-size:11px;color:{_INK_MUTED}">'
                f'Not present on this host: {_htmllib.escape(", ".join(skipped))}</div>')

    half = (len(cells) + 1) // 2
    col1, col2 = "".join(cells[:half]), "".join(cells[half:])
    return (
        f'<div style="margin-bottom:22px">'
        f'<div class="hc-ink" style="font-size:13px;font-weight:700;color:{_INK};'
        f'margin-bottom:8px">At a glance</div>'
        f'<table role="presentation" style="width:100%;border-collapse:collapse">'
        f'<tr>'
        f'<td style="vertical-align:top;width:50%;padding-right:16px">'
        f'<table role="presentation" style="width:100%;border-collapse:collapse">{col1}</table></td>'
        f'<td style="vertical-align:top;width:50%">'
        f'<table role="presentation" style="width:100%;border-collapse:collapse">{col2}</table></td>'
        f'</tr></table>{foot}</div>'
    )


def _legend_html() -> str:
    items = "".join(
        f'<span style="margin-right:14px">{_chip(st)}</span>'
        for st in (OK, INFO, CAUTION, UNHEALTHY)
    )
    return (f'<div class="hc-muted" style="margin:0 0 18px;padding:10px 0;'
            f'border-top:1px solid {_RULE};'
            f'font-size:11px;color:{_INK_MUTED}">{items}</div>')


def _section_html(sec: Section) -> str:
    rows_html = []

    for row in sec.rows:
        if row.is_separator:
            rows_html.append(
                f'<tr><td colspan="2" style="background:{_SURFACE_2};padding:5px 10px;'
                f'font-size:10px;font-weight:700;letter-spacing:.06em;'
                f'text-transform:uppercase;color:{_INK_MUTED}">'
                f'{_htmllib.escape(row.label.strip("─ "))}</td></tr>'
            )
            continue

        rc      = _c(row.status)
        flagged = row.status in (CAUTION, UNHEALTHY)
        # Colour only where it means something. Painting all 163 OK rows green
        # is what made the four rows that mattered invisible.
        row_bg  = rc["bg"] if flagged else _SURFACE
        val_ink = rc["fg"] if flagged else _INK
        dot     = (f'<span style="color:{rc["dot"]};font-weight:700">&#9679;</span>&nbsp;'
                   if flagged else "")

        meter  = _meter(row.meter, row.status) if row.meter is not None else ""
        delta  = (f'<span style="color:{_INK_MUTED};font-size:11px">'
                  f'&nbsp;&nbsp;{_htmllib.escape(row.delta)}</span>') if row.delta else ""
        detail = (f'<br><span style="font-size:10px;color:{_INK_MUTED}">'
                  f'{_htmllib.escape(row.detail)}</span>') if row.detail else ""

        rows_html.append(
            f'<tr class="{"hc-row" if not flagged else "hc-flag"}" '
            f'style="background:{row_bg}">'
            f'<td class="hc-muted" style="padding:6px 10px;font-size:12px;'
            f'color:{_INK_MUTED};background:{row_bg};'
            f'vertical-align:top;width:34%;word-break:break-word">'
            f'{dot}{_htmllib.escape(row.label)}</td>'
            f'<td class="{"" if flagged else "hc-ink"}" '
            f'style="padding:6px 10px;font-size:12px;color:{val_ink};'
            f'background:{row_bg};'
            f'vertical-align:top;word-break:break-word">'
            f'{meter}{_htmllib.escape(row.value)}{delta}{detail}</td>'
            f'</tr>'
        )

    body = "".join(rows_html) or (
        f'<tr><td style="padding:8px 10px;font-size:12px;color:{_INK_MUTED}">'
        f'No data</td></tr>')

    return (
        f'<div style="margin-bottom:18px;border:1px solid {_RULE};border-radius:6px;'
        f'overflow:hidden">'
        f'<div style="padding:9px 10px;background:{_SURFACE_2};'
        f'border-bottom:1px solid {_RULE}">'
        f'<table role="presentation" style="width:100%;border-collapse:collapse"><tr>'
        f'<td class="hc-ink" style="font-size:13px;font-weight:700;color:{_INK}">'
        f'{_htmllib.escape(sec.title)}</td>'
        f'<td style="text-align:right;white-space:nowrap">{_chip(sec.status)}</td>'
        f'</tr></table></div>'
        f'<table role="presentation" style="width:100%;border-collapse:collapse">{body}</table>'
        f'</div>'
    )


def _triage_html(decision) -> str:
    """The 'why am I reading this' block, above everything else.

    Without it a CAUTION report looks identical whether it is a brand new
    problem or the same one from three weeks ago.
    """
    if decision is None:
        return ""

    def block(title, items, status):
        if not items:
            return ""
        c = _c(status)
        lis = "".join(
            f'<li class="hc-ink" style="margin:3px 0;color:{_INK}">'
            f'{_htmllib.escape(m)}</li>'
            for m in items
        )
        return (f'<div style="margin:10px 0 0">'
                f'<div style="font-size:11px;font-weight:700;letter-spacing:.04em;'
                f'text-transform:uppercase;color:{c["fg"]}">{title}</div>'
                f'<ul style="margin:5px 0 0 18px;padding:0;font-size:12px">{lis}</ul></div>')

    parts = [
        block("Needs attention — new",  [m for _, m in decision.new],       UNHEALTHY),
        block("Got worse",              [m for _, m in decision.escalated], UNHEALTHY),
        block("Still open — reminder",  [m for _, m in decision.reminders], CAUTION),
        block("Known, unchanged — no action implied",
              [m for _, m in decision.ongoing],  INFO),
        block("Cleared since last run", decision.resolved,                  OK),
    ]
    inner = "".join(p for p in parts if p)
    if not inner:
        inner = (f'<div style="font-size:12px;color:{_c(OK)["fg"]}">'
                 f'Nothing requiring attention. This message confirms the health '
                 f'check ran successfully.</div>')

    headline = ("This report contains new findings — please read"
                if decision.notify_all
                else "Routine confirmation that the health check ran — no new findings")
    accent = _c(UNHEALTHY if decision.notify_all else OK)

    return (f'<div style="border:1px solid {_RULE};border-left:4px solid {accent["dot"]};'
            f'border-radius:6px;padding:14px 16px;margin-bottom:22px;background:{_SURFACE_2}">'
            f'<div class="hc-ink" style="font-size:14px;font-weight:700;color:{_INK}">'
            f'{_htmllib.escape(headline)}</div>'
            f'<div style="font-size:11px;color:{_INK_MUTED};margin-top:3px">'
            f'{_htmllib.escape(decision.summary())}</div>'
            f'{inner}</div>')


# The report is light-only, on purpose.
#
# A previous version shipped a prefers-color-scheme block. It darkened the
# surfaces but could not reach the colours set inline on every row, so on a
# dark-mode client the section names kept their dark ink on a now-dark
# background and became unreadable. Half a dark theme is worse than none.
#
# "only light" is the documented opt-out from client-side auto-darkening
# (Chrome auto-dark, iOS Mail, Outlook). Paired with an explicit background on
# every container and row, it keeps the report legible wherever it lands.
_LIGHT_LOCK = """
  :root { color-scheme: only light; supported-color-schemes: only light; }
  html, body { background: #ffffff !important; }
  .hc-page { background: #f7f7f5 !important; }
  .hc-card, .hc-row { background: #ffffff !important; }
  .hc-row td { background: #ffffff !important; }
  .hc-band { background: #f7f7f5 !important; }
  .hc-ink { color: #1a1a19 !important; }
  .hc-muted { color: #6b6a66 !important; }
"""


def generate_html(sections: list, overall: str, decision=None) -> str:
    now      = _now()
    hostname = socket.getfqdn()
    ov       = _c(overall)
    ordered  = order_sections(sections)

    body = "".join(_section_html(sec) for sec in ordered if sec.applicable)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="only light">
<meta name="supported-color-schemes" content="only light">
<title>Health Check — {_htmllib.escape(hostname)}</title>
<style>{_LIGHT_LOCK}</style>
</head>
<body class="hc-page" style="margin:0;padding:0;background:{_SURFACE_2};
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif">
<div class="hc-card" style="max-width:860px;margin:0 auto;background:{_SURFACE};
     border:1px solid {_RULE}">

  <div class="hc-band" style="padding:18px 22px;border-bottom:1px solid {_RULE}">
    <table role="presentation" style="width:100%;border-collapse:collapse"><tr>
      <td>
        <div class="hc-ink" style="font-size:19px;font-weight:700;color:{_INK}">
          {_htmllib.escape(hostname)}</div>
        <div class="hc-muted" style="font-size:11px;color:{_INK_MUTED};margin-top:3px">
          Linux Health Check v{VERSION} · {now}</div>
      </td>
      <td style="text-align:right;white-space:nowrap">{_chip(overall)}</td>
    </tr></table>
  </div>

  <div style="padding:20px 22px">
    {_triage_html(decision)}
    {_summary_html(ordered)}
    {_legend_html()}
    {body}
  </div>

  <div class="hc-band hc-muted" style="padding:10px 22px;font-size:10px;
       color:{_INK_MUTED};background:{_SURFACE_2};
       border-top:1px solid {_RULE};text-align:center">
    {_htmllib.escape(hostname)} · {now} · overall {ov["word"]}
  </div>

</div>
</body>
</html>"""


def _triage_text(decision) -> list:
    if decision is None:
        return []
    lines = ["", "  " + ("NEW FINDINGS — PLEASE READ" if decision.notify_all
                         else "ROUTINE CONFIRMATION — no new findings"),
             f"  ({decision.summary()})", ""]
    groups = [
        ("Needs attention — new", [m for _, m in decision.new]),
        ("Got worse",             [m for _, m in decision.escalated]),
        ("Still open — reminder", [m for _, m in decision.reminders]),
        ("Known, unchanged — no action implied", [m for _, m in decision.ongoing]),
        ("Cleared since last run", decision.resolved),
    ]
    for title, items in groups:
        if not items:
            continue
        lines.append(f"  {title}:")
        lines += [f"    - {m}" for m in items]
        lines.append("")
    return lines


W = 78          # every line the text report emits fits inside this
_LABEL_W = 30   # gutter for the label column


def _text_meter(pct, width: int = 10) -> str:
    """A coarse bar; enough to read magnitude at a glance in a fixed-width body."""
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round(pct / 100.0 * width))
    return "[" + "#" * filled + "." * (width - filled) + "]"


def _wrap(text: str, width: int, indent: str) -> list:
    """Hard-wrap without breaking the layout, splitting over-long words.

    The old renderer declared a 76-column report and then emitted 155-column
    lines whenever a k8s container name showed up, so every mail client
    re-wrapped it and the alignment fell apart.
    """
    words, lines, cur = text.split(), [], ""
    for word in words:
        while len(word) > width:
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:width - 1] + "…")
            word = word[width - 1:]
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= width:
            cur += " " + word
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return [(indent if i else "") + l for i, l in enumerate(lines)] or [""]


def _row_lines(row) -> list:
    """Render one row inside W columns.

    A row that is fine gets exactly one line: an inventory entry does not earn
    three lines of wrapped Kubernetes container name. Rows that are flagged wrap
    in full and keep their detail, because that is where a reader is going to
    look.
    """
    sym     = _c(row.status)["sym"]
    flagged = row.status in (CAUTION, UNHEALTHY)
    label   = row.label if len(row.label) <= _LABEL_W else row.label[:_LABEL_W - 1] + "…"
    value   = row.value
    if row.meter is not None:
        value = f"{_text_meter(row.meter)} {value}"

    # Inventory rows carry no label; padding 30 blank columns before the value
    # just leaves a hole in the middle of the report.
    prefix = (f"  {sym}  {label:<{_LABEL_W}} " if label
              else f"  {sym}  ")
    indent = " " * len(prefix)
    val_w  = W - len(prefix)

    if not flagged:
        one = value if len(value) <= val_w else value[:val_w - 1] + "…"
        out = [prefix + one]
        if row.delta:
            out += [indent + c for c in _wrap(row.delta, val_w, "")]
        return out

    out = []
    for i, chunk in enumerate(_wrap(value, val_w, indent)):
        out.append((prefix if i == 0 else "") + chunk)
    # Delta and detail get their own lines. Appending them to the value let the
    # wrapper split them mid-phrase ("...since last / run").
    for extra in (row.delta, f"({row.detail})" if row.detail else ""):
        if extra:
            out += [indent + c for c in _wrap(extra, val_w, "")]
    return out


def _legend_text() -> list:
    marks = "   ".join(f"{_c(st)['sym']} {_c(st)['word']}"
                       for st in (OK, INFO, CAUTION, UNHEALTHY))
    return [f"  Legend: {marks}"]


def _summary_text(sections: list) -> list:
    lines = ["", "  AT A GLANCE", "  " + "─" * (W - 4)]
    for sec in sections:
        if not sec.applicable:
            continue
        c = _c(sec.status)
        n = len(_attention_rows(sec))
        flag = f"  ({n} to review)" if n else ""
        lines.append(f"  {c['sym']}  {sec.title:<{_LABEL_W}} {c['word']}{flag}")
    skipped = [s.title for s in sections if not s.applicable]
    if skipped:
        lines += ["  " + l for l in
                  _wrap(f"Not present on this host: {', '.join(skipped)}", W - 4, "")]
    return lines + [""]


def generate_plain(sections: list, overall: str, decision=None) -> str:
    """Compact plain text — used for the email body."""
    hostname = socket.getfqdn()
    ordered  = order_sections(sections)
    lines    = [
        f"Linux Health Check v{VERSION} — {hostname} — {_now()}",
        f"Overall Status : {_c(overall)['word']}",
        "=" * W,
    ]
    lines += _triage_text(decision)
    lines += _summary_text(ordered)
    lines += _legend_text()
    for sec in ordered:
        if not sec.applicable:
            continue
        lines += ["", "─" * W, f"  {sec.title}  [{_c(sec.status)['word']}]", "─" * W]
        for row in sec.rows:
            if row.is_separator:
                lines.append(f"  {row.label}")
            else:
                lines += _row_lines(row)
    lines += ["", "=" * W, f"Linux Health Check v{VERSION} · {hostname}"]
    return "\n".join(lines)


def generate_text(sections: list, overall: str, decision=None) -> str:
    """Human-readable terminal report with status symbols and aligned columns."""
    hostname = socket.getfqdn()
    ov       = _c(overall)
    ordered  = order_sections(sections)

    lines = [
        "=" * W,
        f"  Linux Health Check v{VERSION}  ·  {hostname}",
        f"  {_now()}",
        f"  Overall Status: {ov['sym']}  {ov['word']}",
        "=" * W,
    ]
    lines += _triage_text(decision)
    lines += _summary_text(ordered)
    lines += _legend_text()

    for sec in ordered:
        if not sec.applicable:
            continue
        sc = _c(sec.status)
        lines.append("")
        lines.append(f"  {sc['sym']}  {sec.title}  [{sc['word']}]")
        lines.append("  " + "─" * (W - 4))
        for row in sec.rows:
            if row.is_separator:
                lines.append(f"     {row.label.strip('─ ')}")
                continue
            lines += _row_lines(row)

    lines += ["", "=" * W]
    return "\n".join(lines)
