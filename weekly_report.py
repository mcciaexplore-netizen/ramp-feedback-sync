"""
Weekly entrypoint: one manual login, then everything else is automatic —
scrape+sync any new feedback into Sheets, build a "this week" analysis, and
email it to REPORT_RECIPIENTS. Meant to be fired by the
com.mccia.ramp-weekly-report LaunchAgent every Monday morning; run it by
hand any time with `python weekly_report.py`.

Auth model is unchanged from main.py (see auth.py / RECON.md): a human
completes the RAMP login + CAPTCHA in a real browser window this script
opens. A desktop notification fires when that window opens since this is
now started by a scheduler rather than a person sitting at the terminal.

The Sheets sync half reuses main.py's exact checkpoint-driven scrape loop
(scrape_event / load_checkpoint / save_checkpoint) — a weekly run behaves
like a normal full run there. The report half is independent: it always
fetches fresh feedback + enrolled/attended counts for events whose
eventDate falls in the last REPORT_WINDOW_DAYS, regardless of checkpoint
state, since the email needs this week's current numbers even if an event
was already synced in an earlier run.
"""

import html
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

from dateutil import parser as dateparser
from dotenv import load_dotenv

import auth
import emailer
from main import load_checkpoint, save_checkpoint, scrape_event
from sheets_sync import SheetsSync
from source_client import PersistentFailure, RampClient

REPORT_WINDOW_DAYS = 7


def notify(message: str):
    """Best-effort desktop notification (macOS) so an unattended scheduled
    run doesn't silently sit at the login screen unnoticed. Never allowed
    to break the run it's reporting on."""
    try:
        subprocess.run(
            [
                "osascript", "-e",
                f'display notification "{message}" with title "RAMP Weekly Report" sound name "Glass"',
            ],
            check=False, timeout=5,
        )
    except Exception:
        pass


def parse_event_date(event_date: str):
    if not event_date:
        return None
    try:
        return dateparser.parse(str(event_date).strip(), dayfirst=False).date()
    except (ValueError, OverflowError, dateparser.ParserError):
        return None


def in_report_window(event_date: str, today) -> bool:
    d = parse_event_date(event_date)
    if not d:
        return False
    return today - timedelta(days=REPORT_WINDOW_DAYS) <= d <= today


def build_week_summary(client: RampClient, week_events: list, errors: list):
    """Full feedback + enrolled/attended fetch for this week's events only
    (expected to be a handful), independent of the Sheets checkpoint.

    Returns (summary_events, feedback_rows): summary_events carries the
    per-event enrolled/attended/feedback counts for the stat cards;
    feedback_rows is the flat list of individual respondent rows that
    actually contain feedback — one per person, for the per-feedback
    cards in the email. A respondent who's merely on the feedback roster
    with nothing filled in doesn't count as "gave feedback" and isn't
    listed.
    """
    summary = []
    feedback_rows = []
    for event in week_events:
        event_id = event.get("eventId") or event.get("uniqueEventId") or "?"
        guid = event.get("uniqueEventId")
        rows = scrape_event(client, event, errors)
        given_rows = [r for r in rows if r["Feedback"].strip()]
        feedback_rows.extend(given_rows)

        enrolled = attended = None
        if guid:
            try:
                enrolled = client.get_enrolled_count(guid)
            except PersistentFailure as e:
                errors.append(f"{event_id}: enrolled count failed ({e})")
            try:
                attended = client.get_attended_count(guid)
            except PersistentFailure as e:
                errors.append(f"{event_id}: attended count failed ({e})")

        summary.append({
            "eventId": event_id,
            "component": event.get("componentName", ""),
            "eventName": event.get("eventName", ""),
            "eventDate": event.get("eventDate", ""),
            "enrolled": enrolled,
            "attended": attended,
            "feedbackCount": len(given_rows),
        })
    return summary, feedback_rows


# --- Email rendering -------------------------------------------------------
#
# Fully inline styles, no <style> block, no CSS variables/media queries —
# this goes through real inboxes (Gmail web/app, Outlook desktop's Word
# rendering engine), not a browser, so only the lowest-common-denominator
# subset of CSS is safe.

NAVY = "#14304f"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
BORDER = "#e5e5e0"
ACCENT = "#eb6834"
WARN = "#9a6a00"


def esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


def fmt_count(n) -> str:
    return f"{n:,}" if n is not None else "—"


def stat_card_html(value, label: str) -> str:
    """One card in the top stat row — width is a percentage so 5 cards
    divide the row evenly regardless of email client."""
    return f"""<td width="20%" style="padding:6px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {BORDER};border-radius:8px;background:#ffffff;">
        <tr><td align="center" style="padding:16px 6px;">
          <div style="font-size:19px;font-weight:800;color:{INK};font-family:Arial,Helvetica,sans-serif;line-height:1.2;">{esc(value)}</div>
          <div style="font-size:10px;color:{MUTED};margin-top:5px;font-family:Arial,Helvetica,sans-serif;text-transform:uppercase;letter-spacing:0.03em;">{esc(label)}</div>
        </td></tr>
      </table>
    </td>"""


def event_header_html(ev: dict) -> str:
    """Section header for one event — name + identity, with its own
    enrolled/attended/feedback line, sitting above that event's feedback
    cards so each event is visually its own group."""
    date_only = esc(ev["eventDate"]).split(" ")[0]
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{NAVY};border-radius:8px;margin:0 0 8px;">
      <tr><td style="padding:12px 16px;">
        <div style="font-size:14px;font-weight:700;color:#ffffff;font-family:Arial,Helvetica,sans-serif;">{esc(ev['eventName'])}</div>
        <div style="font-size:11px;color:#ffffff;opacity:0.82;margin-top:3px;font-family:Arial,Helvetica,sans-serif;">{esc(ev['eventId'])} &middot; {date_only} &middot; {fmt_count(ev['enrolled'])} enrolled &middot; {fmt_count(ev['attended'])} attended &middot; {ev['feedbackCount']} feedback</div>
      </td></tr>
    </table>"""


def feedback_card_html(row: dict) -> str:
    """One card per person who actually gave feedback: their name, then
    their Feedback text as-is — same newline-per-question content as the
    sheet's own Feedback cell, so the email and the sheet read the same
    way. white-space:pre-line renders those newlines as real line breaks
    without needing to re-parse "Question: Answer" back apart."""
    name = esc(row.get("Name") or "Anonymous")
    feedback_text = esc(row.get("Feedback") or "(no written feedback)")
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {BORDER};border-radius:8px;background:#fcfcfb;margin-bottom:8px;">
      <tr><td style="padding:12px 16px;">
        <div style="font-size:13px;font-weight:700;color:{INK};font-family:Arial,Helvetica,sans-serif;margin-bottom:6px;">{name}</div>
        <div style="font-size:12px;color:{INK_2};line-height:1.6;font-family:Arial,Helvetica,sans-serif;white-space:pre-line;">{feedback_text}</div>
      </td></tr>
    </table>"""


def no_feedback_note_html() -> str:
    return (
        f'<p style="margin:0 0 8px;padding:12px 16px;color:{MUTED};font-size:12px;'
        f'font-family:Arial,Helvetica,sans-serif;background:#fcfcfb;border:1px solid {BORDER};'
        f'border-radius:8px;">No feedback received for this event yet.</p>'
    )


def event_section_html(ev: dict, rows_for_event: list) -> str:
    cards = "".join(feedback_card_html(r) for r in rows_for_event) or no_feedback_note_html()
    return f'<div style="margin-bottom:20px;">{event_header_html(ev)}{cards}</div>'


def render_email(week_label: str, month_label: str, summary_events: list, feedback_rows: list, errors: list, total_synced: int):
    total_events = len(summary_events)
    total_enrolled = sum(e["enrolled"] for e in summary_events if e["enrolled"] is not None)
    total_attended = sum(e["attended"] for e in summary_events if e["attended"] is not None)

    total_feedback = len(feedback_rows)

    cards_html = "".join([
        stat_card_html(month_label, "Month"),
        stat_card_html(total_events, "Events Happened"),
        stat_card_html(fmt_count(total_enrolled), "Enrolled"),
        stat_card_html(fmt_count(total_attended), "Attended"),
        stat_card_html(fmt_count(total_feedback), "Gave Feedback"),
    ])

    rows_by_event = {}
    for r in feedback_rows:
        rows_by_event.setdefault(r.get("Event ID", ""), []).append(r)
    for rows in rows_by_event.values():
        rows.sort(key=lambda r: r.get("Name", ""))

    events_sorted = sorted(summary_events, key=lambda e: e["eventDate"])
    events_html = "".join(
        event_section_html(ev, rows_by_event.get(ev["eventId"], [])) for ev in events_sorted
    ) or (
        f'<p style="padding:20px;text-align:center;color:{MUTED};'
        f'font-family:Arial,Helvetica,sans-serif;font-size:13px;margin:0;border:1px solid {BORDER};'
        f'border-radius:8px;background:#fcfcfb;">No events fell in this window.</p>'
    )

    error_html = ""
    if errors:
        error_html = (
            f'<p style="margin:18px 0 0;font-size:12px;color:{WARN};'
            f'font-family:Arial,Helvetica,sans-serif;">{len(errors)} issue(s) occurred while collecting '
            f'counts/feedback this run &mdash; see run.log for details.</p>'
        )

    html_body = f"""<html><body style="margin:0;padding:0;background:#f2f2ee;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f2ee;padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="700" cellpadding="0" cellspacing="0" style="background:#ffffff;border:1px solid {BORDER};">
  <tr><td style="background:{NAVY};padding:20px 24px;">
    <div style="color:#ffffff;font-size:18px;font-weight:800;font-family:Arial,Helvetica,sans-serif;">MCCIA RAMP Feedback &mdash; Weekly Report</div>
    <div style="color:#ffffff;opacity:0.8;font-size:12px;margin-top:4px;font-family:Arial,Helvetica,sans-serif;">Week of {esc(week_label)} &middot; Industry Association scope</div>
  </td></tr>
  <tr><td style="padding:20px 24px 4px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr>{cards_html}</tr>
    </table>
  </td></tr>
  <tr><td style="padding:14px 24px 24px;">
    <div style="font-size:14px;font-weight:700;color:{INK};font-family:Arial,Helvetica,sans-serif;margin-bottom:12px;">Events &amp; Feedback This Week</div>
    {events_html}
    {error_html}
    <p style="margin:20px 0 0;font-size:12px;color:{MUTED};font-family:Arial,Helvetica,sans-serif;">
      {total_synced} new feedback row(s) were synced to the
      <a href="https://docs.google.com/spreadsheets/d/{esc(os.getenv('GOOGLE_SHEET_ID', ''))}" style="color:{ACCENT};">Feedback MSSIDC</a>
      sheet this run.
    </p>
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""

    plain_lines = [
        f"MCCIA RAMP Feedback — Weekly Report — Week of {week_label} ({month_label})",
        "",
        f"Events Happened: {total_events}",
        f"Enrolled: {fmt_count(total_enrolled)}",
        f"Attended: {fmt_count(total_attended)}",
        f"Gave Feedback: {fmt_count(total_feedback)}",
        "",
        "Events & Feedback This Week:",
    ]
    for ev in events_sorted:
        date_only = str(ev["eventDate"]).split(" ")[0]
        plain_lines.append("")
        plain_lines.append(
            f"== {ev['eventName']} ({ev['eventId']}) — {date_only} — "
            f"{fmt_count(ev['enrolled'])} enrolled, {fmt_count(ev['attended'])} attended, "
            f"{ev['feedbackCount']} feedback =="
        )
        for r in rows_by_event.get(ev["eventId"], []):
            plain_lines.append(f"  - {r.get('Name') or 'Anonymous'}:")
            for line in (r.get("Feedback") or "").split("\n"):
                plain_lines.append(f"      {line}")
        if not rows_by_event.get(ev["eventId"]):
            plain_lines.append("  (no feedback received for this event yet)")
    if not events_sorted:
        plain_lines.append("No events fell in this window.")
    plain_lines.append("")
    plain_lines.append(f"{total_synced} new feedback row(s) synced to the Feedback MSSIDC sheet this run.")
    if errors:
        plain_lines.append(f"{len(errors)} issue(s) occurred — see run.log for details.")

    return html_body, "\n".join(plain_lines)


# --- Orchestration -----------------------------------------------------


def _run():
    load_dotenv()
    login_timeout = int(os.getenv("LOGIN_TIMEOUT_SECONDS", "1800"))
    delay_seconds = float(os.getenv("REQUEST_DELAY_SECONDS", "2"))
    max_retries = int(os.getenv("MAX_RETRIES", "3"))

    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sheet_id or not service_account_json:
        print("ERROR: GOOGLE_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON must be set in .env")
        sys.exit(1)

    sync = SheetsSync(service_account_json, sheet_id)

    notify("Log in to RAMP now to run this week's feedback sync + report.")
    session = auth.interactive_login(timeout_seconds=login_timeout)
    print(f"Logged in as {session['user_type']} ({session['name']}).")

    client = RampClient(token=session["token"], delay_seconds=delay_seconds, max_retries=max_retries)
    start = time.monotonic()
    errors = []
    today = datetime.now().date()
    total_added = 0

    try:
        events = client.get_all_events()
        print(f"Found {len(events)} event(s) total.")

        week_events = [e for e in events if in_report_window(e.get("eventDate", ""), today)]
        print(f"{len(week_events)} event(s) fall in the report window (last {REPORT_WINDOW_DAYS} days).")
        summary_events, feedback_rows = build_week_summary(client, week_events, errors)

        done_ids = load_checkpoint()

        def checkpoint_key(e):
            return e.get("eventId") or e.get("uniqueEventId")

        pending = [e for e in events if checkpoint_key(e) not in done_ids]
        print(f"{len(pending)} event(s) not yet checkpointed; syncing to Sheets...")
        for i, event in enumerate(pending, start=1):
            rows = scrape_event(client, event, errors)
            if rows:
                total_added += sync.sync(rows)
            key = checkpoint_key(event)
            if key:
                done_ids.add(key)
                save_checkpoint(done_ids)
            print(f"  [{i}/{len(pending)}] synced")
    finally:
        client.close()

    sync.update_overview()
    sync.write_run_log(events_scanned=len(events), new_rows=total_added, errors=errors)

    today_label = today.strftime("%b %d, %Y")
    week_start_label = (today - timedelta(days=REPORT_WINDOW_DAYS)).strftime("%b %d")
    week_label = f"{week_start_label} – {today_label}"
    month_label = today.strftime("%B %Y")

    html_body, plain_body = render_email(week_label, month_label, summary_events, feedback_rows, errors, total_added)
    emailer.send_report_email(
        subject=f"RAMP Feedback — Weekly Report ({week_label})",
        html_body=html_body,
        plain_fallback=plain_body,
    )
    print("Weekly report emailed.")
    notify("Weekly RAMP report sent.")

    elapsed = time.monotonic() - start
    print(f"Done in {elapsed/60:.1f} min. {len(errors)} error(s), {total_added} new row(s) synced.")


def main():
    try:
        _run()
    except Exception as e:
        notify(f"RAMP weekly run FAILED: {e}"[:200])
        raise


if __name__ == "__main__":
    main()
