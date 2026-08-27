"""Post-event completion checks used by the nightly daily digest.

An event is eligible once its portal ``eventDate`` is strictly earlier than
today in Asia/Kolkata. The portal exposes a date and a start-time label, but
no reliable event end timestamp, so same-day events are deliberately checked
the following night. Cancelled events are excluded.

MSME completion is read from the existing Google Sheet schema. The live
workbook currently tracks it in ``Feedback``; ``SheetsSync`` discovers and
validates that header at runtime. Enrolled portal rows are matched to Sheet
rows within the same event by email, then mobile number, then an unambiguous
participant name. A missing row or blank/incomplete tracked value is pending.
The portal enrolled-row ``status`` is intentionally not used: live
verification showed it contains ``RAMP Registration``, not completion.

Once all three checks pass, the event ID is persisted in
``output/post_event_resolved.json`` and no longer rechecked. Unresolved and
failed events remain eligible on the next run, keeping the scan bounded
without hiding transient failures.
"""

import json
import os
import re
from collections import Counter, defaultdict
from datetime import date
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser

from source_client import PersistentFailure, RampClient

IST = ZoneInfo("Asia/Kolkata")

OUTPUT_DIR = "output"
RESOLVED_FILE = os.path.join(OUTPUT_DIR, "post_event_resolved.json")

CANCELLED_STATUSES = {"cancel", "cancelled", "rejected"}
INCOMPLETE_VALUES = {"", "pending", "incomplete", "not entered", "not done"}

# Exact live values are EventImages and EventAttendance. Normalized aliases
# retain compatibility with human-readable labels if presentation changes.
EVENT_PHOTO_TYPES = {"eventimages", "eventimage", "eventphotos", "eventphoto"}
ATTENDANCE_PHOTO_TYPES = {
    "eventattendance",
    "attendancesheet",
    "attendancesheetphotos",
    "attendancephotos",
}


def load_resolved() -> set[str]:
    if not os.path.exists(RESOLVED_FILE):
        return set()
    try:
        with open(RESOLVED_FILE) as f:
            return set(json.load(f))
    except (OSError, json.JSONDecodeError, TypeError):
        # A corrupt local optimization must not prevent a real check. The
        # next successful run rewrites it with valid JSON.
        return set()


def save_resolved(resolved_ids: set[str]):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tmp_path = f"{RESOLVED_FILE}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(sorted(resolved_ids), f)
    os.replace(tmp_path, RESOLVED_FILE)


def parse_event_date(event_date: str) -> date | None:
    """Parse the portal's live ``MM/DD/YYYY HH:MM:SS`` date format."""
    if not event_date:
        return None
    try:
        return dateparser.parse(str(event_date).strip(), dayfirst=False).date()
    except (ValueError, OverflowError, dateparser.ParserError):
        return None


def is_cancelled(event: dict) -> bool:
    return str(event.get("eventStatus", "")).strip().lower() in CANCELLED_STATUSES


def event_is_past(event: dict, today: date) -> bool:
    event_date = parse_event_date(event.get("eventDate", ""))
    return bool(event_date and event_date < today and not is_cancelled(event))


def _normal_email(value) -> str:
    return str(value or "").strip().casefold()


def _normal_mobile(value) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 7 else ""


def _normal_name(value) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _normal_document_type(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _has_completed_data(value) -> bool:
    return str(value or "").strip().casefold() not in INCOMPLETE_VALUES


def _sheet_indexes(sheet_rows: list[dict]):
    by_email = defaultdict(list)
    by_mobile = defaultdict(list)
    by_name = defaultdict(list)
    for row in sheet_rows:
        email = _normal_email(row.get("email"))
        mobile = _normal_mobile(row.get("mobile"))
        name = _normal_name(row.get("name"))
        if email:
            by_email[email].append(row)
        if mobile:
            by_mobile[mobile].append(row)
        if name:
            by_name[name].append(row)
    return by_email, by_mobile, by_name


def check_enrolled_data_entry(
    client: RampClient,
    unique_event_id: str,
    sheet_rows: list[dict],
) -> list[dict]:
    """Return enrolled MSMEs absent from the Sheet or incomplete there.

    Email and mobile are the stable cross-system identifiers available in
    both live schemas. Name is used only when unique on both sides, avoiding
    false matches for two participants with the same name.
    """
    enrolled_rows = client.get_enrolled_msmes(unique_event_id)
    by_email, by_mobile, by_name = _sheet_indexes(sheet_rows)
    enrolled_name_counts = Counter(_normal_name(row.get("userName")) for row in enrolled_rows)

    missing = []
    for enrolled in enrolled_rows:
        email = _normal_email(enrolled.get("userEmail"))
        mobile = _normal_mobile(enrolled.get("userMobileNo"))
        name = _normal_name(enrolled.get("userName"))

        matches = by_email.get(email, []) if email else []
        if not matches and mobile:
            matches = by_mobile.get(mobile, [])
        if (
            not matches
            and name
            and enrolled_name_counts[name] == 1
            and len(by_name.get(name, [])) == 1
        ):
            matches = by_name[name]

        if not matches:
            reason = "No matching Sheet row"
        elif any(_has_completed_data(row.get("completion_value")) for row in matches):
            continue
        else:
            column = matches[0].get("completion_column") or "tracked field"
            reason = f"{column} is blank or incomplete"

        missing.append({
            "name": enrolled.get("userName") or enrolled.get("nameOfEnterprise") or "",
            "udyam": enrolled.get("udyamNumber", ""),
            "reason": reason,
        })
    return missing


def check_view_documents(client: RampClient, unique_event_id: str) -> dict:
    """Return independent event-photo and attendance-photo presence flags."""
    docs = client.get_event_documents(unique_event_id)
    types = {_normal_document_type(doc.get("documentType")) for doc in docs}
    return {
        "has_event_photos": bool(types & EVENT_PHOTO_TYPES),
        "has_attendance_photos": bool(types & ATTENDANCE_PHOTO_TYPES),
    }


def run_post_event_checks(
    client: RampClient,
    events: list[dict],
    sheet_rows_by_event: dict[str, list[dict]],
    resolved_ids: set[str],
    today: date,
    errors: list[str],
) -> tuple[list[dict], set[str]]:
    """Run both checks per eligible event, isolating failures by event.

    A failed sub-check is represented as ``None`` and shown as "Check failed"
    in the digest. That prevents a transient portal error from becoming a
    false all-clear or false missing-photo finding. Other checks continue.
    """
    resolved_ids = set(resolved_ids)
    pending_items = []
    candidates = [
        event for event in events
        if event_is_past(event, today)
        and str(event.get("eventId") or event.get("uniqueEventId")) not in resolved_ids
    ]
    candidates.sort(key=lambda event: (
        parse_event_date(event.get("eventDate", "")) or date.min,
        str(event.get("eventId") or event.get("uniqueEventId") or ""),
    ))

    for event in candidates:
        event_id = event.get("eventId") or event.get("uniqueEventId") or "?"
        unique_event_id = event.get("uniqueEventId")
        if not unique_event_id:
            errors.append(f"Event {event_id}: missing uniqueEventId, skipping post-event checks")
            continue

        data_error = None
        document_error = None
        missing_msmes = None
        missing_event_photos = None
        missing_attendance_photos = None

        try:
            missing_msmes = check_enrolled_data_entry(
                client,
                unique_event_id,
                sheet_rows_by_event.get(str(event_id), []),
            )
        except PersistentFailure as exc:
            data_error = str(exc)
            errors.append(f"Event {event_id}: enrolled MSME data-entry check failed ({exc})")

        try:
            document_status = check_view_documents(client, unique_event_id)
            missing_event_photos = not document_status["has_event_photos"]
            missing_attendance_photos = not document_status["has_attendance_photos"]
        except PersistentFailure as exc:
            document_error = str(exc)
            errors.append(f"Event {event_id}: View Document check failed ({exc})")

        has_pending_or_unknown = (
            data_error is not None
            or document_error is not None
            or bool(missing_msmes)
            or missing_event_photos is True
            or missing_attendance_photos is True
        )
        if has_pending_or_unknown:
            pending_items.append({
                "eventId": event_id,
                "eventName": event.get("eventName", ""),
                "eventDate": event.get("eventDate", ""),
                "missing_msmes": missing_msmes,
                "missing_event_photos": missing_event_photos,
                "missing_attendance_photos": missing_attendance_photos,
                "data_entry_error": data_error,
                "document_error": document_error,
            })
        else:
            resolved_ids.add(str(event_id))

    return pending_items, resolved_ids
