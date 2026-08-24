"""
Sends the weekly report by email via SMTP — aistudio@mcciapune.com is a
Zoho Mail account, so this defaults to Zoho's SMTP (smtp.zoho.in:465,
India cluster — see .env.example if that turns out to be the wrong
regional cluster for this account). Both are overridable via SMTP_HOST /
SMTP_PORT in .env in case the account moves elsewhere.

Auth is a Zoho application-specific password (SMTP_APP_PASSWORD in .env)
tied to SMTP_USER — never the real account password, and never hardcoded.
Generate one at https://accounts.zoho.in/home#security/apppassword (or the
.com equivalent if the account isn't on the India cluster) — requires
2FA/TFA to be enabled on the account first. Recipients come from
REPORT_RECIPIENTS as a comma-separated list.
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


DEFAULT_SMTP_HOST = "smtp.zoho.in"
DEFAULT_SMTP_PORT = 465


class EmailConfigError(Exception):
    """Raised when required SMTP env vars are missing."""


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise EmailConfigError(
            f"{name} is not set in .env — see .env.example for the SMTP setup."
        )
    return value


def send_report_email(subject: str, html_body: str, plain_fallback: str, recipients_env: str = "REPORT_RECIPIENTS") -> None:
    """Send one HTML email (with a plain-text fallback part) to every
    address in the env var named by recipients_env (REPORT_RECIPIENTS by
    default), authenticated as SMTP_USER via an App Password. Raises
    EmailConfigError if the env isn't configured, or smtplib's own
    exceptions on a send failure — callers should let a send failure
    surface loudly rather than silently skip the report.
    """
    sender = _require_env("SMTP_USER")
    app_password = _require_env("SMTP_APP_PASSWORD")
    recipients_raw = _require_env(recipients_env)
    recipients = [addr.strip() for addr in recipients_raw.split(",") if addr.strip()]
    if not recipients:
        raise EmailConfigError("REPORT_RECIPIENTS is set but contains no valid addresses.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain_fallback, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    host = os.getenv("SMTP_HOST", DEFAULT_SMTP_HOST)
    port = int(os.getenv("SMTP_PORT", str(DEFAULT_SMTP_PORT)))

    with smtplib.SMTP_SSL(host, port) as server:
        server.login(sender, app_password)
        server.sendmail(sender, recipients, msg.as_string())
