import configparser
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .utils import host_mail_domain


def send_email(cfg: configparser.ConfigParser, subject: str,
               html: str, plain: str, recipients: list) -> bool:
    """Send the report. Returns True if the relay accepted the message.

    The caller uses this to decide whether to record the alerts as
    "notified" — marking them delivered when the relay was down would
    silently swallow the alert forever.
    """
    if not recipients:
        return False

    host     = cfg.get("smtp", "host",     fallback="relay.domain.com").strip() or "localhost"
    port     = cfg.getint("smtp", "port",  fallback=25)
    use_tls  = cfg.getboolean("smtp", "use_tls", fallback=False)
    username = cfg.get("smtp", "username", fallback="").strip()
    password = cfg.get("smtp", "password", fallback="").strip()
    from_    = cfg.get("smtp", "from",     fallback=f"healthcheck@{host_mail_domain()}").strip()

    # inline      — multipart/alternative: the client renders the HTML in the
    #               body and falls back to plain text if it cannot. This is what
    #               makes people actually read the report.
    # attachment  — plain text in the body, HTML as a downloadable file.
    # both        — inline HTML *and* the file, for archiving.
    mode = cfg.get("email", "html_mode", fallback="inline").strip().lower()
    if mode not in ("inline", "attachment", "both"):
        mode = "inline"

    text_part = MIMEText(plain, "plain", "utf-8")
    html_part = MIMEText(html, "html", "utf-8")

    if mode == "attachment":
        msg = MIMEMultipart("mixed")
        msg.attach(text_part)
        attach = MIMEText(html, "html", "utf-8")
        attach.add_header("Content-Disposition", "attachment",
                          filename="health-report.html")
        msg.attach(attach)
    else:
        body = MIMEMultipart("alternative")
        body.attach(text_part)      # least-preferred first, per RFC 2046
        body.attach(html_part)
        if mode == "both":
            msg = MIMEMultipart("mixed")
            msg.attach(body)
            attach = MIMEText(html, "html", "utf-8")
            attach.add_header("Content-Disposition", "attachment",
                              filename="health-report.html")
            msg.attach(attach)
        else:
            msg = body

    msg["Subject"] = subject
    msg["From"]    = from_
    msg["To"]      = ", ".join(recipients)

    try:
        conn = smtplib.SMTP_SSL(host, port, timeout=30) if use_tls else smtplib.SMTP(host, port, timeout=30)
        if username:
            conn.login(username, password)
        conn.sendmail(from_, recipients, msg.as_string())
        conn.quit()
        print(f"  ✓ Email sent → {', '.join(recipients)}")
        return True
    except Exception as exc:
        print(f"  ✗ Email failed ({host}:{port}) → {exc}", file=sys.stderr)
        return False
