import unittest

from sheets_sync import SheetsSync


class FakeWorksheet:
    def __init__(self, title, values=None):
        self.title = title
        self.values = values or []
        self.appended = []
        self.updated = []

    def get_all_values(self):
        return self.values

    def append_row(self, row, **kwargs):
        self.appended.append(row)

    def update(self, values, **kwargs):
        self.updated.append((values, kwargs))


class FakeSpreadsheet:
    def __init__(self, worksheets, ranges=None):
        self._worksheets = {worksheet.title: worksheet for worksheet in worksheets}
        self.ranges = ranges or {}

    def worksheets(self):
        return list(self._worksheets.values())

    def worksheet(self, title):
        return self._worksheets[title]

    def values_batch_get(self, ranges):
        return {"valueRanges": [{"values": self.ranges.get(item, [])} for item in ranges]}


class SheetsSyncPostEventTest(unittest.TestCase):
    def make_sync(self, spreadsheet):
        sync = SheetsSync.__new__(SheetsSync)
        sync.spreadsheet = spreadsheet
        sync._worksheets = {}
        return sync

    def test_post_event_reader_discovers_reordered_headers(self):
        title = "August 2026"
        range_name = "'August 2026'!A:Z"
        values = [
            ["feedback", "EMAIL ADDRESS", "EventId", "Mobile", "Participant Name"],
            ["Question: Answer", "person@example.com", "EVENT-1", "9000000000", "Person"],
        ]
        spreadsheet = FakeSpreadsheet([FakeWorksheet(title)], {range_name: values})
        sync = self.make_sync(spreadsheet)
        events = [{"eventDate": "08/20/2026 00:00:00"}]

        rows = sync.read_post_event_rows(events)

        self.assertEqual(rows["EVENT-1"][0]["completion_column"], "feedback")
        self.assertEqual(rows["EVENT-1"][0]["completion_value"], "Question: Answer")
        self.assertEqual(rows["EVENT-1"][0]["email"], "person@example.com")

    def test_daily_archive_upserts_same_date(self):
        header = [
            "Timestamp", "Date", "Month", "Events", "Registered", "Attended",
            "Feedback", "Pending Events", "Errors", "Summary JSON",
            "Pending Items JSON", "Email HTML", "Email Text",
        ]
        worksheet = FakeWorksheet("Daily Reports", [header, ["old", "Aug 26, 2026"]])
        sync = self.make_sync(FakeSpreadsheet([worksheet]))

        sync.write_daily_report(
            "Aug 26, 2026", "August 2026", [], [], [], "<html></html>", "text"
        )

        self.assertEqual(worksheet.appended, [])
        self.assertEqual(len(worksheet.updated), 1)
        self.assertEqual(worksheet.updated[0][1]["range_name"], "A2:M2")


if __name__ == "__main__":
    unittest.main()
