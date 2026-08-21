"""
CLI entrypoint for the RAMP feedback -> Google Sheets sync.

Auth model (see RECON.md and auth.py): a human logs in themselves in a real
browser window each run; this script never automates credentials or the
CAPTCHA. Everything after that — enumerating events, pulling feedback,
normalizing, deduping, and writing to Sheets — is automated.

Usage:
  python main.py --recon-only              # Phase 0 recon, then stop
  python main.py --dry-run [--limit-events N]   # scrape+normalize, print/save, skip Sheets
  python main.py                           # full run: scrape -> normalize -> dedup -> sync -> log
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time

from dotenv import load_dotenv

import auth
from source_client import RampClient, PersistentFailure, flatten_feedback
from sheets_sync import SheetsSync, normalize_row, SCHEMA_COLUMNS


CHECKPOINT_FILE = "checkpoint.json"


def load_checkpoint() -> set:
    """Event IDs already fully synced in a prior run. Lets a re-run (after
    a crash, or just starting the script again) skip straight past events
    it's already covered instead of re-fetching and re-deduping them —
    dedup alone was correct but wasteful, since it still cost a full
    rate-limited API round-trip per already-done event."""
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            return set(json.load(f))
    return set()


def save_checkpoint(done_ids: set):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(sorted(done_ids), f)


def run_recon():
    print("Running Phase 0 recon (recon.py)...\n")
    result = subprocess.run([sys.executable, "recon.py"])
    return result.returncode


def scrape_event(client: RampClient, event: dict, errors: list) -> list:
    """Fetch every feedback row for one event. Appends to `errors` in place
    and returns whatever rows it managed to build rather than raising, so a
    problem with one event never stops the run."""
    event_id = event.get("eventId") or event.get("uniqueEventId") or "?"
    unique_event_id = event.get("uniqueEventId")
    if not unique_event_id:
        errors.append(f"Event {event_id}: missing uniqueEventId, skipping")
        return []

    try:
        respondents = client.get_feedback_respondents(unique_event_id)
    except PersistentFailure as e:
        errors.append(f"Event {event_id}: failed to fetch respondents ({e})")
        return []

    rows = []
    for resp in respondents:
        enrollment_id = resp.get("enrollmentId") or resp.get("EnrollmentId")
        udyam = resp.get("udyamRegistrationNumber", "")
        resp_event_id = resp.get("eventId", event_id)

        if not enrollment_id:
            # No enrollment id to look up the detailed answers with — still
            # emit what we know rather than dropping the row.
            rows.append(normalize_row(event, resp, "", ""))
            continue

        try:
            detail = client.get_feedback_detail(udyam, resp_event_id, enrollment_id)
            feedback_text, rating = flatten_feedback(detail["questions"], detail["feedback_answer"])
        except PersistentFailure as e:
            errors.append(f"Event {event_id}, respondent {udyam or enrollment_id}: {e}")
            feedback_text, rating = "", ""

        rows.append(normalize_row(event, resp, feedback_text, rating))

    return rows


def write_dry_run_output(rows):
    with open("dry_run_output.json", "w") as f:
        json.dump(rows, f, indent=2)
    with open("dry_run_output.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCHEMA_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nWrote {len(rows)} row(s) to dry_run_output.json and dry_run_output.csv")


def main():
    parser = argparse.ArgumentParser(description="RAMP feedback -> Google Sheets sync")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--recon-only", action="store_true", help="Run Phase 0 recon and stop")
    group.add_argument("--dry-run", action="store_true", help="Scrape+normalize, skip Sheets write")
    parser.add_argument("--limit-events", type=int, default=None, help="Only scan the first N events (useful with --dry-run)")
    args = parser.parse_args()

    if args.recon_only:
        sys.exit(run_recon())

    load_dotenv()
    login_timeout = int(os.getenv("LOGIN_TIMEOUT_SECONDS", "300"))
    delay_seconds = float(os.getenv("REQUEST_DELAY_SECONDS", "2"))
    max_retries = int(os.getenv("MAX_RETRIES", "3"))

    sync = None
    if not args.dry_run:
        sheet_id = os.getenv("GOOGLE_SHEET_ID")
        service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not sheet_id or not service_account_json:
            print(
                "ERROR: GOOGLE_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON must be "
                "set in .env for a full run (see .env.example / README). Use "
                "--dry-run to test without them."
            )
            sys.exit(1)

        sync = SheetsSync(service_account_json, sheet_id)
        sync.write_demo_row()
        print("Wrote a note to the Overview sheet — check it now to confirm the automation started.")

    session = auth.interactive_login(timeout_seconds=login_timeout)
    print(
        f"Logged in as {session['user_type']} ({session['name']}) — scraping "
        "whatever events/feedback this login can see."
    )

    client = RampClient(token=session["token"], delay_seconds=delay_seconds, max_retries=max_retries)
    start = time.monotonic()
    errors = []
    all_rows = []  # only accumulated for --dry-run; full runs sync per-event instead
    total_added = 0

    done_ids = load_checkpoint() if not args.dry_run else set()

    try:
        events = client.get_all_events()
        if args.limit_events:
            events = events[: args.limit_events]

        def checkpoint_key(e):
            return e.get("eventId") or e.get("uniqueEventId")

        pending = [e for e in events if checkpoint_key(e) not in done_ids]
        skipped = len(events) - len(pending)
        print(f"Found {len(events)} event(s); {skipped} already checkpointed, {len(pending)} to scan.")

        for i, event in enumerate(pending, start=1):
            event_id = event.get("eventId") or event.get("uniqueEventId") or "?"
            rows = scrape_event(client, event, errors)
            print(f"  [{i}/{len(pending)}] Event {event_id}: {len(rows)} feedback row(s)")

            if args.dry_run:
                all_rows.extend(rows)
            else:
                if rows:
                    # Sync each event's rows immediately: progress is visible
                    # in the sheet as it happens, and a crash mid-run doesn't
                    # lose what's already been scraped.
                    added = sync.sync(rows)
                    total_added += added
                # Checkpoint after every event (not just ones with rows) so
                # a zero-feedback event isn't re-fetched pointlessly either.
                key = checkpoint_key(event)
                if key:
                    done_ids.add(key)
                    save_checkpoint(done_ids)

            elapsed = time.monotonic() - start
            remaining = (elapsed / i) * (len(pending) - i)
            print(f"    ~{remaining/60:.1f} min remaining ({i}/{len(pending)} events done)")
    finally:
        client.close()

    elapsed_total = time.monotonic() - start
    print(f"\nDone in {elapsed_total/60:.1f} min. {len(errors)} error(s).")

    if args.dry_run:
        write_dry_run_output(all_rows)
        if errors:
            print("\nErrors encountered:")
            for e in errors:
                print(f"  - {e}")
        return

    sync.update_overview()
    sync.write_run_log(events_scanned=len(events), new_rows=total_added, errors=errors)
    print(f"Synced {total_added} new row(s) across the per-month sheets.")
    if errors:
        print(f"{len(errors)} error(s) occurred — a short preview is in the RunLog tab, full details in run.log.")


if __name__ == "__main__":
    main()
