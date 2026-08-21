"""
One-time migration: re-scrapes every event's feedback fresh and writes it
into NEW worksheets in the Question/Rating format (one row per respondent
per question — see sheets_sync.SCHEMA_COLUMNS), leaving the existing
month tabs (with the old flattened Feedback/Rating format) untouched.

Why a fresh re-scrape rather than reformatting what's already in the
sheet: the per-question breakdown was never stored anywhere — the old
pipeline flattened every question's answer into one "Feedback" string the
moment it was scraped and discarded the structured form, so there's
nothing to reformat in place. This has to go back to the live RAMP API.

Why new tabs instead of clearing the existing ones: this sheet is shared
(MCCIA staff already reference it), and re-scraping ~160 events is a long
enough run that something could go wrong partway through — safer to write
into "<Month> (v2)" tabs you can review and compare, then delete the old
tabs yourself once satisfied, than to wipe live data with no undo.

This ignores checkpoint.json on purpose — that file tracks the ongoing
incremental sync (main.py / weekly_report.py) and has nothing to do with
this one-time migration, so it's neither read nor written here.

Run once:
    python migrate_to_qna_format.py
Or against a handful of events first, to sanity-check the output:
    python migrate_to_qna_format.py --limit-events 5
"""

import argparse
import os
import sys
import time
from collections import defaultdict

from dotenv import load_dotenv

import auth
from main import scrape_event
from sheets_sync import SheetsSync
from source_client import RampClient

MONTH_SUFFIX = " (v2)"


def main():
    parser = argparse.ArgumentParser(description="Migrate feedback to the Question/Rating format in new tabs")
    parser.add_argument("--limit-events", type=int, default=None, help="Only process the first N events (sanity check)")
    args = parser.parse_args()

    load_dotenv()
    login_timeout = int(os.getenv("LOGIN_TIMEOUT_SECONDS", "480"))
    delay_seconds = float(os.getenv("REQUEST_DELAY_SECONDS", "2"))
    max_retries = int(os.getenv("MAX_RETRIES", "3"))
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sheet_id or not service_account_json:
        print("ERROR: GOOGLE_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON must be set in .env")
        sys.exit(1)

    sync = SheetsSync(service_account_json, sheet_id)

    session = auth.interactive_login(timeout_seconds=login_timeout)
    print(f"Logged in as {session['user_type']} ({session['name']}).")

    client = RampClient(token=session["token"], delay_seconds=delay_seconds, max_retries=max_retries)
    errors = []
    start = time.monotonic()

    try:
        events = client.get_all_events()
        if args.limit_events:
            events = events[: args.limit_events]
        print(f"Re-scraping {len(events)} event(s) for the Question/Rating migration...")

        rows_by_target_month = defaultdict(list)
        for i, event in enumerate(events, start=1):
            event_id = event.get("eventId") or event.get("uniqueEventId") or "?"
            rows = scrape_event(client, event, errors)
            for row in rows:
                target_month = (row["Month"] or "Unknown") + MONTH_SUFFIX
                rows_by_target_month[target_month].append(row)
            elapsed = time.monotonic() - start
            remaining = (elapsed / i) * (len(events) - i)
            print(f"  [{i}/{len(events)}] {event_id}: {len(rows)} row(s) — ~{remaining/60:.1f} min remaining")
    finally:
        client.close()

    print(f"\nWriting {sum(len(r) for r in rows_by_target_month.values())} row(s) across {len(rows_by_target_month)} new tab(s)...")
    total_written = 0
    for target_month, rows in rows_by_target_month.items():
        written = sync.sync(rows)
        total_written += written
        print(f"  {target_month}: {written} row(s)")

    elapsed_total = time.monotonic() - start
    print(f"\nDone in {elapsed_total/60:.1f} min. {total_written} row(s) written, {len(errors)} error(s).")
    if errors:
        os.makedirs("output", exist_ok=True)
        with open("output/run.log", "a") as f:
            f.write(f"\n=== Question/Rating migration ({len(errors)} errors) ===\n")
            for e in errors:
                f.write(f"{e}\n")
        print(f"{len(errors)} error(s) — details appended to output/run.log")

    print(
        "\nReview the new \"<Month> (v2)\" tabs in the sheet. Once you're happy "
        "with them, delete the old flattened-format tabs yourself (and rename "
        "the (v2) tabs if you want to drop the suffix) — nothing here does "
        "that automatically."
    )


if __name__ == "__main__":
    main()
