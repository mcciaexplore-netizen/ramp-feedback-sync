"""
RAMP portal API client (Path A: direct API calls via httpx).

Endpoint contract below was reverse-engineered from the RAMP app's own
deployed Angular bundles (main.js / chunk-*.js under /RAMP/), primarily by
reading each screen's own `exportToExcel()` mapping (ground truth for field
names) and its `createDynamicForm()` / `patchFeedbackAnswer()` logic (ground
truth for the per-respondent answer JSON shape). See RECON.md.

Confidence levels:
- Events list (`mssidc/gettotalEventlist`) and per-event respondent list
  (`industryassociation/viewfeedback`): HIGH — field names confirmed via
  each screen's own Excel-export code, which maps `item.<field>` directly.
- Per-respondent feedback detail (`msme/getmsmecustomfeedbackquestion`):
  MEDIUM — the question/answer JSON shape is confirmed from
  createDynamicForm/patchFeedbackAnswer, but this was never exercised
  against a live response (no test account was available). Run
  `main.py --dry-run` against 2-3 events first and sanity-check the output
  before trusting a full run.
- Enrolled/attended counts (`industryassociation/enrolledmsmelistforia` /
  `attendedmsmelistforia`): MEDIUM — endpoint, DTO shape, and response
  shape (`{status, data, totalRecords}`) confirmed via
  VeiwenrolledmsmeComponent / ViewattendedmsmeComponent's own loadData()
  in the app bundle, but never exercised against a live response.

All calls require the Bearer token from a human-completed login (auth.py).
This client does not attempt to authenticate on its own.
"""

import time
from dataclasses import dataclass, field

import httpx

API_BASE = "https://mssidcapi.mahait.org/api/"

EVENTS_ENDPOINT = "mssidc/getialist"  # the actual endpoint behind the IA "/view_event" screen
RESPONDENTS_ENDPOINT = "industryassociation/viewfeedback"
FEEDBACK_DETAIL_ENDPOINT = "msme/getmsmecustomfeedbackquestion"
ENROLLED_ENDPOINT = "industryassociation/enrolledmsmelistforia"  # behind /viewEnrolledParticipants/:eventid
ATTENDED_ENDPOINT = "industryassociation/attendedmsmelistforia"  # behind /viewAttendedParticipants/:eventid


class PersistentFailure(Exception):
    """Raised when a request fails after all retries are exhausted."""


def _as_int(value, default: int) -> int:
    """The backend serializes some count fields as strings — coerce
    defensively rather than assuming a JSON number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class RampClient:
    token: str
    delay_seconds: float = 2.0
    max_retries: int = 3
    timeout: float = 30.0
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self):
        self._client = httpx.Client(
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )

    def close(self):
        self._client.close()

    def _get(self, path: str, params: dict) -> dict:
        """GET with config-driven rate limiting and retry/backoff.

        Raises PersistentFailure if all retries are exhausted; the caller
        decides whether to log-and-continue (per event/respondent) or
        propagate.
        """
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            time.sleep(self.delay_seconds)
            try:
                resp = self._client.get(path, params=params)
                if resp.status_code == 401:
                    raise PersistentFailure(
                        "401 Unauthorized — the login session has expired. "
                        "Re-run main.py to log in again."
                    )
                resp.raise_for_status()
                return resp.json()
            except PersistentFailure:
                raise
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as e:
                last_error = e
                backoff = self.delay_seconds * (2 ** (attempt - 1))
                if attempt < self.max_retries:
                    time.sleep(backoff)
        raise PersistentFailure(f"{path} failed after {self.max_retries} attempts: {last_error}")

    def get_all_events(self, page_size: int = 100) -> list[dict]:
        """Enumerate every event via mssidc/getialist, paginating.

        Response shape is {status, data: {item1: {totalCount, ...}, item2:
        [events]}} — confirmed from VieweventComponent.loadData() in the
        app's own bundle, distinct from the flat {data: [...], totalRecords}
        shape other list endpoints use.
        """
        events = []
        page = 1
        while True:
            dto = {
                "PageNumber": page,
                "PageSize": page_size,
                "SortColumn": "",
                "SortDirection": "",
                "SearchTerm": "",
                "SearchCol": "",
                "TodayTommarow": "",
                "FromDate": "",
                "ToDate": "",
            }
            result = self._get(EVENTS_ENDPOINT, dto)
            data = result.get("data") or {}
            page_data = data.get("item2") or []
            events.extend(page_data)
            # The backend serializes totalCount as a string on this
            # endpoint (confirmed live) — coerce defensively.
            total = _as_int((data.get("item1") or {}).get("totalCount"), default=len(events))
            if len(page_data) < page_size or len(events) >= total:
                break
            page += 1
        return events

    def get_feedback_respondents(self, unique_event_id: str, page_size: int = 100) -> list[dict]:
        """Enumerate every feedback respondent for one event, paginating."""
        respondents = []
        page = 1
        while True:
            dto = {
                "UniqueEventId": unique_event_id,
                "PageNumber": page,
                "PageSize": page_size,
                "SortColumn": "",
                "SortDirection": "",
                "SearchTerm": "",
                "SearchCol": "",
            }
            result = self._get(RESPONDENTS_ENDPOINT, dto)
            page_data = result.get("data") or []
            respondents.extend(page_data)
            total = _as_int(result.get("totalRecords"), default=len(respondents))
            if len(page_data) < page_size or len(respondents) >= total:
                break
            page += 1
        return respondents

    def get_enrolled_count(self, unique_event_id: str) -> int:
        """How many MSMEs enrolled for one event — behind the IA
        "/viewEnrolledParticipants/:eventid" screen. PageSize=1 since only
        `totalRecords` is needed, not the actual list."""
        dto = {
            "UniqueEventId": unique_event_id,
            "PageNumber": 1,
            "PageSize": 1,
            "SortColumn": "",
            "SortDirection": "",
            "SearchTerm": "",
            "SearchCol": "",
        }
        result = self._get(ENROLLED_ENDPOINT, dto)
        return _as_int(result.get("totalRecords"), default=0)

    def get_attended_count(self, unique_event_id: str) -> int:
        """How many enrolled MSMEs actually attended one event — behind the
        IA "/viewAttendedParticipants/:eventid" screen."""
        dto = {
            "UniqueEventId": unique_event_id,
            "PageNumber": 1,
            "PageSize": 1,
            "SortColumn": "",
            "SortDirection": "",
            "SearchTerm": "",
            "SearchCol": "",
        }
        result = self._get(ATTENDED_ENDPOINT, dto)
        return _as_int(result.get("totalRecords"), default=0)

    def get_feedback_detail(self, udyam_registration_number: str, event_id: str, enrollment_id: str) -> dict:
        """Fetch one respondent's question set + submitted answers.

        Returns {"questions": [...], "feedback_answer": {...} | None}.
        `feedback_answer` is None if the respondent hasn't actually
        submitted (result.data[4][0]["feedbackAnswer"] was null in the app).
        """
        params = {
            "udyamRegistrationNumber": udyam_registration_number,
            "eventId": event_id,
            "EnrollmentId": enrollment_id,
        }
        result = self._get(FEEDBACK_DETAIL_ENDPOINT, params)
        if not result.get("status"):
            raise PersistentFailure(result.get("message", "feedback detail call returned status=false"))

        data = result.get("data") or []
        questions_raw = data[4] if len(data) > 4 else []
        feedback_answer_raw = None
        if questions_raw and isinstance(questions_raw, list) and questions_raw:
            feedback_answer_raw = questions_raw[0].get("feedbackAnswer")

        questions = []
        for item in questions_raw:
            options = [o.strip() for o in item["options"].split(",")] if item.get("options") else None
            questions.append({
                "questionId": item.get("questionId"),
                "questionText": item.get("questionText"),
                "answerType": item.get("answerType"),
                "options": options,
            })

        feedback_answer = None
        if feedback_answer_raw:
            import json
            feedback_answer = json.loads(feedback_answer_raw)

        return {"questions": questions, "feedback_answer": feedback_answer}


def answers_by_question(questions: list[dict], feedback_answer: dict | None) -> list[tuple[str, str]]:
    """One (question_text, answer_text) pair per question actually
    answered, in the form's own question order.

    The portal's feedback forms are multi-question (Text/Radio/Dropdown/
    Checkbox per createDynamicForm in the app bundle) — this used to be
    joined into one flattened text blob per respondent, which read as an
    unreadable wall of "Question: answer; Question: answer; ..." in a
    single Sheets cell, and only captured a "Rating" when a form happened
    to use the word-scale (Excellent/Good/...) vocabulary rather than the
    numeric 1-5 scale most of these forms actually use. Returning each
    question separately instead lets the caller emit one Question/Rating
    row per question, matching the sheet's own columns.
    """
    if not feedback_answer:
        return []

    pairs = []
    for q in questions:
        key = f"question_{q['questionId']}"
        raw_answer = feedback_answer.get(key)
        if raw_answer is None:
            continue

        if q["answerType"] == "Checkbox" and isinstance(raw_answer, list) and q.get("options"):
            selected = [opt for opt, is_checked in zip(q["options"], raw_answer) if is_checked]
            answer_text = ", ".join(selected)
        else:
            answer_text = str(raw_answer)

        if answer_text:
            pairs.append((q["questionText"], answer_text))
    return pairs
