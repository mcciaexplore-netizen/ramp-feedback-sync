"""
One-time migration: re-scrapes every event's feedback fresh and writes it
in the newline-separated Feedback format (one "Question: Answer" line per
question inside a single Feedback cell — see sheets_sync.SCHEMA_COLUMNS).

Two modes, chosen by --in-place:

- Default (safe): writes into NEW "<Month> (v2)" worksheets, leaving the
  existing month tabs (old semicolon-flattened Feedback/Rating format)
  completely untouched. Review the (v2) tabs, then delete the old ones
  yourself (and drop the suffix if you want) once satisfied.
- --in-place (destructive): clears each existing month tab and rewrites
  it directly with the new format. No new tabs, no undo — this sheet is
  shared (MCCIA staff already reference it), so this mode asks for a
  typed confirmation before touching anything.

Why a fresh re-scrape either way rather than reformatting what's already
in the sheet: the per-question breakdown was never stored anywhere — the
old pipeline flattened every question's answer into one "; "-joined
string the moment it was scraped and discarded the structured form, so
there's nothing to reformat in place. This has to go back to the live
RAMP API.

This ignores checkpoint.json on purpose — that file tracks the ongoing
incremental sync (main.py / weekly_report.py) and has nothing to do with
this one-time migration, so it's neither read nor written here.

Run once:
    python migrate_feedback_format.py
Or against a handful of events first, to sanity-check the output:
    python migrate_feedback_format.py --limit-events 5
Or to replace the existing tabs directly instead of writing new ones:
    python migrate_feedback_format.py --in-place
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


def confirm_in_place() -> bool:
    print(
        "\n--in-place will CLEAR and REWRITE every existing month tab in the "
        "live sheet with freshly re-scraped data. This cannot be undone from "
        "within the sheet itself (the source data on RAMP is unaffected, but "
        "whatever is currently in those tabs will be gone)."
    )
    answer = input("Type 'yes' to proceed: ").strip().lower()
    return answer == "yes"


def main():
    parser = argparse.ArgumentParser(description="Migrate feedback to the newline-separated Feedback format")
    parser.add_argument("--limit-events", type=int, default=None, help="Only process the first N events (sanity check)")
    parser.add_argument(
        "--in-place", action="store_true",
        help="Clear and rewrite the existing month tabs directly instead of writing new \"(v2)\" tabs",
    )
    args = parser.parse_args()

    if args.in_place and not confirm_in_place():
        print("Aborted — nothing was changed.")
        sys.exit(0)

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
        print(f"Re-scraping {len(events)} event(s) for the Feedback-format migration...")

        rows_by_month = defaultdict(list)
        for i, event in enumerate(events, start=1):
            event_id = event.get("eventId") or event.get("uniqueEventId") or "?"
            rows = scrape_event(client, event, errors)
            for row in rows:
                month = row["Month"] or "Unknown"
                rows_by_month[month].append(row)
            elapsed = time.monotonic() - start
            remaining = (elapsed / i) * (len(events) - i)
            print(f"  [{i}/{len(events)}] {event_id}: {len(rows)} row(s) — ~{remaining/60:.1f} min remaining")
    finally:
        client.close()

    total_rows = sum(len(r) for r in rows_by_month.values())
    if args.in_place:
        print(f"\nReplacing {len(rows_by_month)} existing tab(s) with {total_rows} freshly-scraped row(s)...")
        total_written = 0
        for month, rows in rows_by_month.items():
            written = sync.replace_month(month, rows)
            total_written += written
            print(f"  {month}: {written} row(s)")
    else:
        print(f"\nWriting {total_rows} row(s) across {len(rows_by_month)} new tab(s)...")
        total_written = 0
        for month, rows in rows_by_month.items():
            target_month = month + MONTH_SUFFIX
            for row in rows:
                row["Month"] = target_month
            written = sync.sync(rows)
            total_written += written
            print(f"  {target_month}: {written} row(s)")

    elapsed_total = time.monotonic() - start
    print(f"\nDone in {elapsed_total/60:.1f} min. {total_written} row(s) written, {len(errors)} error(s).")
    if errors:
        os.makedirs("output", exist_ok=True)
        with open("output/run.log", "a") as f:
            f.write(f"\n=== Feedback-format migration ({len(errors)} errors) ===\n")
            for e in errors:
                f.write(f"{e}\n")
        print(f"{len(errors)} error(s) — details appended to output/run.log")

    if args.in_place:
        print("\nExisting month tabs have been replaced with the new Feedback format.")
    else:
        print(
            "\nReview the new \"<Month> (v2)\" tabs in the sheet. Once you're happy "
            "with them, delete the old flattened-format tabs yourself (and rename "
            "the (v2) tabs if you want to drop the suffix), or re-run with "
            "--in-place to replace them directly."
        )


if __name__ == "__main__":
    main()
