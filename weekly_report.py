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
from collections import Counter
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


def build_week_summary(client: RampClient, week_events: list, errors: list) -> list:
    """Full feedback + enrolled/attended fetch for this week's events only
    (expected to be a handful), independent of the Sheets checkpoint."""
    summary = []
    for event in week_events:
        event_id = event.get("eventId") or event.get("uniqueEventId") or "?"
        guid = event.get("uniqueEventId")
        rows = scrape_event(client, event, errors)

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

        feedback_texts = Counter(r["Feedback"] for r in rows if r["Feedback"].strip())
        ratings = Counter(r["Rating"].strip() for r in rows if r["Rating"].strip())
        duplicate_groups = sorted(
            ({"text": t, "count": c} for t, c in feedback_texts.items() if c > 1),
            key=lambda d: -d["count"],
        )

        summary.append({
            "eventId": event_id,
            "component": event.get("componentName", ""),
            "eventName": event.get("eventName", ""),
            "eventDate": event.get("eventDate", ""),
            "enrolled": enrolled,
            "attended": attended,
            "feedbackCount": len(rows),
            "ratings": ratings,
            "topDuplicateFeedback": duplicate_groups[:3],
        })
    return summary


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


def fmt_pct(numer, denom) -> str:
    if not denom:
        return "—"
    return f"{round(100 * numer / denom)}%"


def stat_tile(value: str, label: str) -> str:
    return f"""<td style="padding:14px 10px;text-align:center;border:1px solid {BORDER};background:#fcfcfb;">
      <div style="font-size:22px;font-weight:800;color:{INK};font-family:Arial,Helvetica,sans-serif;">{esc(value)}</div>
      <div style="font-size:11px;color:{INK_2};margin-top:4px;font-family:Arial,Helvetica,sans-serif;">{esc(label)}</div>
    </td>"""


def event_row_html(ev: dict) -> str:
    enrolled, attended, feedback = ev["enrolled"], ev["attended"], ev["feedbackCount"]
    attend_pct = fmt_pct(attended, enrolled) if attended is not None else "—"
    feedback_pct = fmt_pct(feedback, attended) if attended else "—"

    flag, flag_color = "", INK_2
    if enrolled is None:
        flag, flag_color = "no data", MUTED
    elif attended == 0 and enrolled:
        flag, flag_color = "0 attended", "#d03b3b"
    elif feedback == 0 and attended:
        flag, flag_color = "no feedback", WARN

    date_only = esc(ev["eventDate"]).split(" ")[0]
    td = f"padding:10px 12px;border-bottom:1px solid {BORDER};font-family:Arial,Helvetica,sans-serif;font-size:13px;"
    return f"""<tr>
      <td style="{td}">
        <div style="font-weight:600;color:{INK};">{esc(ev['eventName'])}</div>
        <div style="font-size:11px;color:{MUTED};margin-top:2px;">{esc(ev['eventId'])} &middot; {esc(ev['component'])} &middot; {date_only}</div>
      </td>
      <td style="{td}text-align:right;">{fmt_count(enrolled)}</td>
      <td style="{td}text-align:right;">{fmt_count(attended)}</td>
      <td style="{td}text-align:right;">{attend_pct}</td>
      <td style="{td}text-align:right;">{fmt_count(feedback)}</td>
      <td style="{td}text-align:right;">{feedback_pct}</td>
      <td style="{td}color:{flag_color};font-size:11px;">{esc(flag)}</td>
    </tr>"""


def render_email(week_label: str, summary_events: list, errors: list, total_synced: int):
    total_events = len(summary_events)
    total_enrolled = sum(e["enrolled"] for e in summary_events if e["enrolled"] is not None)
    total_attended = sum(e["attended"] for e in summary_events if e["attended"] is not None)
    total_feedback = sum(e["feedbackCount"] for e in summary_events)

    all_ratings = Counter()
    all_dupes = []
    for e in summary_events:
        all_ratings.update(e["ratings"])
        all_dupes.extend(e["topDuplicateFeedback"])
    top_dupe = max(all_dupes, key=lambda d: d["count"], default=None)

    events_sorted = sorted(summary_events, key=lambda e: e["eventDate"])
    rows_html = "".join(event_row_html(e) for e in events_sorted) or (
        f'<tr><td colspan="7" style="padding:20px;text-align:center;color:{MUTED};'
        f'font-family:Arial,Helvetica,sans-serif;font-size:13px;">No events fell in this window.</td></tr>'
    )

    spotlight_html = ""
    if top_dupe:
        snippet = esc(top_dupe["text"][:220]) + ("…" if len(top_dupe["text"]) > 220 else "")
        spotlight_html += (
            f'<p style="margin:16px 0 4px;font-weight:700;font-size:13px;color:{INK};'
            f'font-family:Arial,Helvetica,sans-serif;">Most repeated response ({top_dupe["count"]}&times;)</p>'
            f'<p style="margin:0;font-style:italic;color:{INK_2};font-size:13px;'
            f'font-family:Arial,Helvetica,sans-serif;">&ldquo;{snippet}&rdquo;</p>'
        )
    if all_ratings:
        items = "".join(
            f'<li style="margin-bottom:2px;">{esc(k)}: {v}</li>'
            for k, v in sorted(all_ratings.items(), key=lambda kv: -kv[1])
        )
        spotlight_html += (
            f'<p style="margin:16px 0 4px;font-weight:700;font-size:13px;color:{INK};'
            f'font-family:Arial,Helvetica,sans-serif;">Word-scale ratings this week</p>'
            f'<ul style="margin:0;padding-left:18px;color:{INK_2};font-size:13px;'
            f'font-family:Arial,Helvetica,sans-serif;">{items}</ul>'
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
    <table role="presentation" width="100%" cellpadding="0" cellspacing="6">
      <tr>
        {stat_tile(total_events, "Events this week")}
        {stat_tile(fmt_count(total_enrolled), "Enrolled")}
        {stat_tile(fmt_count(total_attended), "Attended")}
        {stat_tile(fmt_count(total_feedback), "Gave feedback")}
      </tr>
    </table>
  </td></tr>
  <tr><td style="padding:8px 24px 20px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {BORDER};">
      <thead>
        <tr style="background:#fcfcfb;">
          <th align="left" style="padding:10px 12px;font-size:11px;text-transform:uppercase;color:{MUTED};font-family:Arial,Helvetica,sans-serif;border-bottom:1px solid {BORDER};">Event</th>
          <th align="right" style="padding:10px 12px;font-size:11px;text-transform:uppercase;color:{MUTED};font-family:Arial,Helvetica,sans-serif;border-bottom:1px solid {BORDER};">Enrolled</th>
          <th align="right" style="padding:10px 12px;font-size:11px;text-transform:uppercase;color:{MUTED};font-family:Arial,Helvetica,sans-serif;border-bottom:1px solid {BORDER};">Attended</th>
          <th align="right" style="padding:10px 12px;font-size:11px;text-transform:uppercase;color:{MUTED};font-family:Arial,Helvetica,sans-serif;border-bottom:1px solid {BORDER};">Attend %</th>
          <th align="right" style="padding:10px 12px;font-size:11px;text-transform:uppercase;color:{MUTED};font-family:Arial,Helvetica,sans-serif;border-bottom:1px solid {BORDER};">Feedback</th>
          <th align="right" style="padding:10px 12px;font-size:11px;text-transform:uppercase;color:{MUTED};font-family:Arial,Helvetica,sans-serif;border-bottom:1px solid {BORDER};">Feedback %</th>
          <th style="padding:10px 12px;border-bottom:1px solid {BORDER};"></th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
    {spotlight_html}
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
        f"MCCIA RAMP Feedback — Weekly Report — Week of {week_label}",
        "",
        f"Events this week: {total_events}",
        f"Enrolled: {fmt_count(total_enrolled)}",
        f"Attended: {fmt_count(total_attended)}",
        f"Gave feedback: {fmt_count(total_feedback)}",
        "",
    ]
    for e in events_sorted:
        plain_lines.append(
            f"- {e['eventName']} ({e['eventId']}): enrolled={fmt_count(e['enrolled'])}, "
            f"attended={fmt_count(e['attended'])}, feedback={e['feedbackCount']}"
        )
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
        summary_events = build_week_summary(client, week_events, errors)

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

    html_body, plain_body = render_email(week_label, summary_events, errors, total_added)
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
