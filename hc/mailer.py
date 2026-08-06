import configparser
import smtplib
import socket
import sys
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def send_email(cfg: configparser.ConfigParser, subject: str,
               html: str, plain: str, recipients: list) -> None:
    if not recipients:
        return

    host          = cfg.get("smtp", "host",         fallback="relay.mpt.mp.br").strip() or "localhost"
    port          = cfg.getint("smtp", "port",      fallback=25)
    use_tls       = cfg.getboolean("smtp", "use_tls", fallback=False)
    use_starttls  = cfg.getboolean("smtp", "use_starttls", fallback=False)
    username      = cfg.get("smtp", "username", fallback="").strip()
    password      = cfg.get("smtp", "password", fallback="").strip()
    from_         = cfg.get("smtp", "from",     fallback=f"healthcheck@{socket.getfqdn()}").strip()

    msg            = MIMEMultipart("alternative")
    # RFC 2047-encode the subject: health check subjects contain symbols
    # (✓ ⚠ ✖ · —) which many MTAs mangle or reject as raw 8-bit headers.
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"]    = from_
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html,  "html",  "utf-8"))

    try:
        if use_tls:
            conn = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            conn = smtplib.SMTP(host, port, timeout=30)
            if use_starttls:
                conn.starttls()
        if username:
            conn.login(username, password)
        conn.sendmail(from_, recipients, msg.as_string())
        conn.quit()
        print(f"  ✓ Email sent → {', '.join(recipients)}")
    except Exception as exc:
        print(f"  ✗ Email failed ({host}:{port}) → {exc}", file=sys.stderr)
