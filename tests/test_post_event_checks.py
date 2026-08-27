import unittest
from datetime import date

from daily_report import render_email
from post_event_checks import event_is_past, run_post_event_checks
from source_client import PersistentFailure


class FakeClient:
    def __init__(self, enrolled=None, documents=None, document_failures=None):
        self.enrolled = enrolled or {}
        self.documents = documents or {}
        self.document_failures = set(document_failures or [])

    def get_enrolled_msmes(self, unique_event_id):
        return self.enrolled.get(unique_event_id, [])

    def get_event_documents(self, unique_event_id):
        if unique_event_id in self.document_failures:
            raise PersistentFailure("document page unavailable")
        return self.documents.get(unique_event_id, [])


def event(event_id, event_date="08/20/2026 00:00:00", status="Completed"):
    return {
        "eventId": event_id,
        "uniqueEventId": f"guid-{event_id}",
        "eventName": f"Event {event_id}",
        "eventDate": event_date,
        "eventStatus": status,
    }


def enrolled(name, email, mobile, udyam):
    return {
        "userName": name,
        "userEmail": email,
        "userMobileNo": mobile,
        "udyamNumber": udyam,
        "status": "RAMP Registration",
    }


def sheet_row(name, email, mobile, feedback):
    return {
        "name": name,
        "email": email,
        "mobile": mobile,
        "completion_column": "Feedback",
        "completion_value": feedback,
    }


class PostEventChecksTest(unittest.TestCase):
    def test_complete_and_incomplete_events(self):
        complete = event("COMPLETE")
        incomplete = event("INCOMPLETE", "08/21/2026 00:00:00")
        client = FakeClient(
            enrolled={
                "guid-COMPLETE": [enrolled("Complete Person", "done@example.com", "9000000001", "UDYAM-1")],
                "guid-INCOMPLETE": [
                    enrolled("Blank Person", "blank@example.com", "9000000002", "UDYAM-2"),
                    enrolled("Absent Person", "absent@example.com", "9000000003", "UDYAM-3"),
                ],
            },
            documents={
                "guid-COMPLETE": [
                    {"documentType": "EventImages"},
                    {"documentType": "EventAttendance"},
                ],
                "guid-INCOMPLETE": [{"documentType": "EventImages"}],
            },
        )
        sheet_rows = {
            "COMPLETE": [sheet_row("Complete Person", "done@example.com", "9000000001", "Question: Answer")],
            "INCOMPLETE": [sheet_row("Blank Person", "blank@example.com", "9000000002", "   ")],
        }
        errors = []

        pending, resolved = run_post_event_checks(
            client,
            [complete, incomplete],
            sheet_rows,
            set(),
            date(2026, 8, 26),
            errors,
        )

        self.assertEqual(errors, [])
        self.assertEqual(resolved, {"COMPLETE"})
        self.assertEqual([item["eventId"] for item in pending], ["INCOMPLETE"])
        self.assertEqual(len(pending[0]["missing_msmes"]), 2)
        self.assertFalse(pending[0]["missing_event_photos"])
        self.assertTrue(pending[0]["missing_attendance_photos"])

        html_body, plain_body = render_email(
            "Aug 19 – Aug 26, 2026", "August 2026", [], [], pending, [], 0
        )
        self.assertIn("Pending Post-Event Items", html_body)
        self.assertIn("Event INCOMPLETE", html_body)
        self.assertNotIn("Event COMPLETE", html_body)
        self.assertIn("Attendance sheet photos: Missing", plain_body)
        self.assertIn("Feedback is blank or incomplete", plain_body)

    def test_document_failure_is_logged_and_next_event_continues(self):
        failed = event("FAILED")
        complete = event("NEXT")
        client = FakeClient(
            enrolled={"guid-FAILED": [], "guid-NEXT": []},
            documents={
                "guid-NEXT": [
                    {"documentType": "EventImages"},
                    {"documentType": "EventAttendance"},
                ]
            },
            document_failures={"guid-FAILED"},
        )
        errors = []

        pending, resolved = run_post_event_checks(
            client, [failed, complete], {}, set(), date(2026, 8, 26), errors
        )

        self.assertEqual(resolved, {"NEXT"})
        self.assertEqual(pending[0]["eventId"], "FAILED")
        self.assertIsNone(pending[0]["missing_event_photos"])
        self.assertIsNotNone(pending[0]["document_error"])
        self.assertIn("View Document check failed", errors[0])

    def test_same_day_future_and_cancelled_events_are_not_eligible(self):
        today = date(2026, 8, 26)
        self.assertFalse(event_is_past(event("TODAY", "08/26/2026 00:00:00"), today))
        self.assertFalse(event_is_past(event("FUTURE", "08/27/2026 00:00:00"), today))
        self.assertFalse(event_is_past(event("CANCELLED", status="Cancel"), today))
        self.assertTrue(event_is_past(event("PAST"), today))


if __name__ == "__main__":
    unittest.main()
