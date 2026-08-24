"""
Heads-up email fired 10 minutes before com.mccia.ramp-weekly-report — the
Monday 11:00 job that opens a real browser window and waits (see auth.py)
for a human to complete the RAMP login + CAPTCHA. Without this, whoever's
supposed to log in has no warning the window is about to appear.

Sends to LOGIN_REMINDER_RECIPIENTS (falls back to REPORT_RECIPIENTS if
unset) via the same Zoho SMTP setup as the weekly digest (emailer.py).
"""

import os

from dotenv import load_dotenv

from auth import LOGIN_URL
from emailer import send_report_email

SUBJECT = "RAMP Weekly Report — log in at 11:00"

PLAIN_BODY = (
    "This is a 10-minute heads-up: the weekly RAMP feedback scrape runs "
    "at 11:00 AM.\n\n"
    "A Chromium window will open on the scheduled machine for the RAMP "
    "login. Pick the MSSIDC role, enter your username/password, and "
    "solve the CAPTCHA as soon as it appears — the run waits up to 30 "
    "minutes, but the sooner you log in, the sooner this week's feedback "
    "digest goes out.\n\n"
    f"RAMP login (opens in your own browser, just a shortcut — the actual "
    f"automated login happens in the Chromium window at 11:00): {LOGIN_URL}\n"
)

HTML_BODY = f"""\
<p>This is a 10-minute heads-up: the weekly RAMP feedback scrape runs at
<strong>11:00 AM</strong>.</p>
<p>A Chromium window will open on the scheduled machine for the RAMP
login. Pick the <strong>MSSIDC</strong> role, enter your username/password,
and solve the CAPTCHA as soon as it appears — the run waits up to 30
minutes, but the sooner you log in, the sooner this week's feedback
digest goes out.</p>
<p><a href="{LOGIN_URL}">{LOGIN_URL}</a><br>
<span style="color:#666;font-size:12px;">(shortcut to the RAMP login page —
the actual automated login still happens in the Chromium window at 11:00)</span></p>
"""


def _run():
    load_dotenv()
    recipients_env = "LOGIN_REMINDER_RECIPIENTS" if os.getenv("LOGIN_REMINDER_RECIPIENTS") else "REPORT_RECIPIENTS"
    send_report_email(SUBJECT, HTML_BODY, PLAIN_BODY, recipients_env=recipients_env)
    print("Login reminder sent.")


if __name__ == "__main__":
    _run()
