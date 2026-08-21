"""
One-off script: gathers everything needed for the monthly digest.

Source of truth for "which events happened, and when" is the live event
list (client.get_all_events()) — the per-month Sheets only contain events
that had at least one feedback submission (33 of 161 events had zero
feedback and never got a row there), so relying on the sheets alone would
silently undercount "how many events happened."  Feedback content/ratings
are overlaid from the sheets by matching Event ID. Enrolled/attended
counts need a fresh live pass over the enrolled/attended endpoints (see
source_client.ENROLLED_ENDPOINT / ATTENDED_ENDPOINT). All three sources
get combined here and written to digest_data.json for the report-building
step to consume.
"""

import json
import os
from collections import Counter
from datetime import datetime, timezone

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

import auth
from sheets_sync import derive_month
from source_client import RampClient, PersistentFailure

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
MONTH_SHEETS = ["May 2026", "June 2026", "July 2026", "August 2026"]


def read_feedback_by_event(sh) -> dict:
    """Returns {eventId: [{"rating": ..., "feedback": ...}, ...]} across
    every month sheet — feedback content only, not event metadata (the
    live event list is the source of truth for that, see module docstring)."""
    feedback_by_event = {}
    for month_name in MONTH_SHEETS:
        ws = sh.worksheet(month_name)
        values = ws.get_all_values()
        header, *rows = values
        idx = {c: header.index(c) for c in header}
        for r in rows:
            event_id = r[idx["Event ID"]]
            if event_id in ("", "DEMO"):
                continue
            feedback_by_event.setdefault(event_id, []).append({
                "rating": r[idx["Rating"]],
                "feedback": r[idx["Feedback"]],
            })
    return feedback_by_event


def main():
    load_dotenv()
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    delay_seconds = float(os.getenv("REQUEST_DELAY_SECONDS", "2"))
    max_retries = int(os.getenv("MAX_RETRIES", "3"))
    login_timeout = int(os.getenv("LOGIN_TIMEOUT_SECONDS", "480"))

    creds = Credentials.from_service_account_file(service_account_json, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)

    print("Reading feedback content from month sheets...")
    feedback_by_event = read_feedback_by_event(sh)
    print(f"  {len(feedback_by_event)} event(s) with at least one feedback row")

    session = auth.interactive_login(timeout_seconds=login_timeout)
    print(f"Logged in as {session['user_type']} ({session['name']})")

    client = RampClient(token=session["token"], delay_seconds=delay_seconds, max_retries=max_retries)
    errors = []
    events = []
    try:
        raw_events = client.get_all_events()
        print(f"\n{len(raw_events)} event(s) total; fetching enrolled/attended counts...")

        for i, event in enumerate(raw_events, start=1):
            event_id = event.get("eventId") or event.get("uniqueEventId") or "?"
            guid = event.get("uniqueEventId")
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
            else:
                errors.append(f"{event_id}: no uniqueEventId in event list, skipping counts")

            events.append({
                "eventId": event_id,
                "component": event.get("componentName", ""),
                "eventName": event.get("eventName", ""),
                "eventDate": event.get("eventDate", ""),
                "month": derive_month(event.get("eventDate", "")),
                "enrolled": enrolled,
                "attended": attended,
            })
            print(f"  [{i}/{len(raw_events)}] {event_id}: enrolled={enrolled}, attended={attended}")
    finally:
        client.close()

    # Combine + compute per-month aggregates.
    #
    # "Similar feedback" is measured two ways: the Rating column (word-scale
    # forms like Excellent/Good/Yes/No only — many forms here actually use a
    # numeric 1-4 scale instead, which Rating doesn't capture), and exact
    # duplicate Feedback text within the same event (works regardless of
    # scale type, since identical answers to the same form produce
    # identical flattened text) — this second one is the more universal
    # signal and drives the headline "X people gave matching feedback"
    # number.
    result = {"months": {}, "errors": errors}
    for month in MONTH_SHEETS:
        month_events = [e for e in events if e["month"] == month]
        month_summary = {
            "totalEvents": len(month_events),
            "totalEnrolled": 0,
            "totalAttended": 0,
            "totalFeedback": 0,
            "totalSimilarFeedbackRespondents": 0,
            "ratingDistribution": Counter(),
            "events": [],
        }
        for ev in sorted(month_events, key=lambda e: e["eventId"]):
            feedback_rows = feedback_by_event.get(ev["eventId"], [])
            feedback_texts = Counter(r["feedback"] for r in feedback_rows if r["feedback"].strip())
            duplicate_groups = sorted(
                ({"text": t, "count": c} for t, c in feedback_texts.items() if c > 1),
                key=lambda d: -d["count"],
            )
            similar_respondents = sum(g["count"] for g in duplicate_groups)

            for r in feedback_rows:
                if r["rating"].strip():
                    month_summary["ratingDistribution"][r["rating"].strip()] += 1

            month_summary["events"].append({
                "eventId": ev["eventId"],
                "component": ev["component"],
                "eventName": ev["eventName"],
                "eventDate": ev["eventDate"],
                "enrolled": ev["enrolled"],
                "attended": ev["attended"],
                "feedbackCount": len(feedback_rows),
                "similarFeedbackRespondents": similar_respondents,
                "topDuplicateFeedback": duplicate_groups[:3],
            })
            if ev["enrolled"] is not None:
                month_summary["totalEnrolled"] += ev["enrolled"]
            if ev["attended"] is not None:
                month_summary["totalAttended"] += ev["attended"]
            month_summary["totalFeedback"] += len(feedback_rows)
            month_summary["totalSimilarFeedbackRespondents"] += similar_respondents

        month_summary["ratingDistribution"] = dict(month_summary["ratingDistribution"])
        result["months"][month] = month_summary

    with open("digest_data.json", "w") as f:
        json.dump(result, f, indent=2)

    print(f"\nWrote digest_data.json. {len(errors)} error(s).")
    if errors:
        with open("run.log", "a") as f:
            f.write(f"\n=== Digest gather at {datetime.now(timezone.utc).isoformat()} ({len(errors)} errors) ===\n")
            for e in errors:
                f.write(f"{e}\n")
        print("Error details appended to run.log")


if __name__ == "__main__":
    main()
