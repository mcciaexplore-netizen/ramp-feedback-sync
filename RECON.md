# RECON.md — RAMP Feedback Portal Reconnaissance

Generated: 2026-08-20T06:52:44.331037+00:00

## Verdict: BLOCKED — feedback requires login

Per `PROMPT.md` Phase 0 step 4 ("If feedback requires login: stop. Do not attempt to authenticate or bypass any login/access control — report this back instead of proceeding"), this project stops here. No scraper (Phase 1) or Sheets sync (Phase 2) was built.

## 1. Client-side route guards (static analysis)

The RAMP Angular app is served from `/RAMP/` as `main.js` + lazy chunk bundles (`chunk-7MEKLF2Y.js`, `chunk-JOTOVE5E.js`, `chunk-JVM4FNKT.js`, ...). Downloading and grepping these bundles for the route table shows **every single route is protected** by `canActivate: [AuthService]` — there is no public/unauthenticated route for events or feedback. Relevant routes found:

- `/view_event` — guard=`AuthService`
- `/viewallevents` — guard=`AuthService`
- `/viewFeedbacksListmssidc/:eventid` — guard=`AuthService`
- `/viewFeedbacksListia/:eventid` — guard=`AuthService`
- `/reports/viewevent/:UniqueEventId` — guard=`AuthService`

`AuthService.canActivate()` (in `chunk-JVM4FNKT.js`) checks real session state — `sessionStorage.getItem("isUserLoggedIn")`, `isAdminLoggedIn`, `isApproved`, `UserType` — against the route's required module (`MSME`/`IA`/`MSSIDC`/`DIC`). This is a genuine enforcement check, not a stub that always returns true.

## 2. Live browser test (dynamic verification)

Launched headless Chromium with a **brand-new browser context** (no cookies, no sessionStorage/localStorage — i.e. never logged in) and navigated directly to `https://mssidc.maharashtra.gov.in/RAMP/#/view_event` with network logging on.

- Final URL after navigation: `https://mssidc.maharashtra.gov.in/RAMP/#/login`
- Redirected to login: **True**
- sessionStorage after load: `{}`
- Page title: MSSIDC
- Total network requests during load: 24; requests to the backend API host (`mahait.org`): 0

The app does not even attempt an anonymous data fetch — it renders the login screen (username/password + role selector for MSME/IA/MSSIDC/DIC + **CAPTCHA**) before any event or feedback data is requested. Screenshot saved as `recon_screenshot.png`.

Body text of the landing page (truncated):
```
Maharashtra Small Scale Industries Development Corporation Ltd.

A Government Of Maharashtra Undertaking
CIN No. U74999MH1962SGC012501

Raising & Accelerating MSME Performance (RAMP)
New to RAMP? Register here
Already registered? Log in as
MSME
IA
MSSIDC
DIC
USERNAME
PASSWORD
Please enter the text below:
LoginForgot Password?
EOI Form
EOI Exhibition
IA User Manual
MSME User Manual
Copyright © 2024. All rights reserved to Maharashtra Small Scale Industries Development Corporation Ltd
Designed by 
```

## 3. Direct backend API probes (server-side enforcement)

To rule out "maybe the SPA route guard is client-side only and the API is actually open," the underlying API endpoints (found by grepping the bundles for `API_ENDPOINTS` values) were hit directly with plain HTTP, base `https://mssidcapi.mahait.org/api/`, **no Authorization header**:

| Endpoint | Status | Notes |
|---|---|---|
| `mssidc/getgradingevent` | 401 | 401 Unauthorized — server enforces auth |
| `mssidc/getGradingMSMEFeeback` | 401 | 401 Unauthorized — server enforces auth |
| `industryassociation/viewfeedback` | 200 | {"statusCode":0,"status":true,"message":"Success.","data":[],"totalRecords":0,"r |

`mssidc/getgradingevent` (event list) and `mssidc/getGradingMSMEFeeback` (feedback list, MSSIDC-role variant) both return **HTTP 401** with no token — the backend independently enforces the same login requirement as the SPA route guard.

**Caveat / responsible-disclosure note:** a third, differently-named endpoint, `industryassociation/viewfeedback` (the IA-role variant of the feedback list), responded `200 OK` with an empty result set (`"data":[]`) when called with no parameters and no auth token — unlike the other two endpoints. This looks like a possible server-side authorization gap (the backend not enforcing the same login check as the SPA for that one route), not a sanctioned public API. **This was not explored further** — no event IDs or other parameters were guessed or supplied to it, and no attempt was made to retrieve real feedback records through it, because doing so would mean using a likely-unintended gap to bypass the access control that governs this system by design (confirmed in sections 1 and 2 above). This is flagged here so it can be reported to MSSIDC/MahaIT as a potential security issue rather than exploited.

## 4. robots.txt / terms of use

- `robots.txt` at the site root allows crawling broadly (`Disallow: /search`, `Disallow: /admin`; `Allow: /`) — not the blocker here.
- No dedicated Terms of Use / Privacy Policy page was found at common paths (`/terms-of-use`, `/terms-conditions`, `/privacy-policy`, etc. all 404). The blocker is the authentication requirement itself (sections 1–3), independent of ToS.

## Path A vs. Path B

**Neither.** Both paths assume feedback is reachable once you know the right API call or DOM selectors; here, reaching it at all requires an authenticated session (MSME, IA, MSSIDC, or DIC role) behind a username/password/CAPTCHA login. Per the guardrails, this project does not attempt to obtain or use such credentials.

## What would unblock this

- Written confirmation from MSSIDC/RAMP admins that feedback is meant to be public, plus a real public endpoint or page (not the auth-gapped one in section 3), **or**
- Legitimate credentials for a role (e.g. an IA account) explicitly authorized by the account holder to be used by this automation, supplied via `.env` — at which point Phase 1 (scraper) and Phase 2 (Sheets sync) in `PROMPT.md` can be implemented against the now-confirmed endpoints (`mssidc/getgradingevent`, `mssidc/getGradingMSMEFeeback` / `industryassociation/viewfeedback`, both under `https://mssidcapi.mahait.org/api/`, bearer-token auth).

---

## Addendum — unblocked via human-completed login (2026-08-20)

The account holder confirmed they want to proceed using their own MSSIDC
login, and asked for credential-based scraping. Two things are true at
once here, and the design below reflects both:

- **The login itself is still never automated.** `PROMPT.md`'s guardrail
  ("never attempt to bypass login, CAPTCHA, or any access control") is
  unconditional — it doesn't carve out an exception for valid credentials.
  Separately, the portal's CAPTCHA (confirmed client-side-only, generated
  and checked entirely in-browser by `CaptchaService` in
  `chunk-JOTOVE5E.js` — never sent to or verified by the backend) is still
  a human-verification gate by intent, regardless of how weak its
  implementation is. Automating past it — even trivially — is exactly what
  "bypass CAPTCHA" means. The login form's password is also client-side
  encrypted (`cryptoService.encryptnew(password)`) before it's POSTed,
  which would require reverse-engineering that scheme to script a raw API
  login — more invasive territory this project also avoids.
- **Everything downstream of a legitimately-established session is fair
  game.** Once a human has logged in themselves — same as any normal use
  of the portal — using that session to automate the tedious parts
  (paging through events, pulling feedback, normalizing, deduping, writing
  to Sheets) isn't bypassing anything; it's automating what the logged-in
  human is already authorized to click through by hand.

`auth.py` implements this: it opens a real, visible Chromium window on the
actual login page, a human completes the role selection, username,
password, and CAPTCHA themselves, and the script simply reads the
resulting `sessionStorage.getItem("authToken")` once the app sets it
(confirmed in `main.js`'s MSSIDC login handler:
`sessionStorage.setItem("authToken", res.token)`). That token is then used
as a plain Bearer token for direct `httpx` calls — Path A as PROMPT.md
prefers, no browser needed for the actual scraping.

### Endpoint chain (confirmed via static analysis of the app's own code)

| Step | Endpoint | Confidence | Source |
|---|---|---|---|
| List all events | `GET mssidc/gettotalEventlist` | HIGH | Field names (`EventName`, `EventDate`, `VenueDetails`, ...) confirmed via `ViewalleventsComponent.exportToExcel()`'s own `item.<Field>` mapping in `chunk-7MEKLF2Y.js`. |
| List feedback respondents for one event | `GET industryassociation/viewfeedback` (params: `UniqueEventId` + pagination DTO) | HIGH | Field names (`udyamRegistrationNumber`, `name`, `districtName`, `eventId`, ...) confirmed via `EventfeedbacklistComponent.exportToExcel()` in `chunk-JOTOVE5E.js`. This call returns *who* gave feedback, not the feedback content itself. |
| Get one respondent's actual answers | `GET msme/getmsmecustomfeedbackquestion` (params: `udyamRegistrationNumber`, `eventId`, `EnrollmentId`) | MEDIUM | The question list and the submitted-answers JSON shape are confirmed from `MsmecustomfeedbackComponent`'s `createDynamicForm()` (form control key = `question_{questionId}`) and `patchFeedbackAnswer()` (`JSON.parse(feedbackAnswer)`, keyed by that same control name) in `chunk-JOTOVE5E.js`. **This was never exercised against a live response** — no test account was available in this environment. Verify with `--dry-run --limit-events 3` before trusting a full run. |

The portal's feedback is a multi-question custom form per event (Text /
Radio / Dropdown / Checkbox questions — see `FeedbackSummaryComponent`'s
own aggregate columns: excellent/good/neutral/poor/yes/no), not a single
flat text+rating pair. `source_client.flatten_feedback()` joins every
question/answer into the `Feedback Text` column and pulls the first
rating-scale-looking answer into `Rating`, per PROMPT.md's "if the form
captures one, else blank" allowance.

No confirmed "feedback submitted on" timestamp field was found anywhere in
this chain — dedup keys off Event ID + Feedback By + Feedback instead
(PROMPT.md's suggested fallback), reconstructable purely from the sheet's
own visible columns on any future run.

## Addendum 2 — scoped to IA's own events, and the correct event-list endpoint (2026-08-20)

The account holder confirmed the IA login (not MSSIDC) is intentional —
they only want their own organization's events and feedback, not the
statewide MSSIDC scope PROMPT.md's original objective described. No change
needed there; IA-role login already returns exactly that scope.

They also pointed at the real screen they use
(`https://mssidc.maharashtra.gov.in/RAMP/#/view_event`, ~169 events) and a
live example feedback URL
(`https://mssidc.maharashtra.gov.in/RAMP/#/viewFeedbacksListia/a56f31e4-b126-4b68-a595-2053e5012026`).
Cross-checking that screen's actual component (`VieweventComponent` in
`chunk-JOTOVE5E.js`) against the first live dry-run (which had used the
wrong, MSSIDC-admin event-list endpoint that happened to also respond
under an IA token) surfaced a mismatch — this project now uses the correct
one:

| | Was using | Now uses |
|---|---|---|
| Endpoint | `mssidc/gettotalEventlist` | `mssidc/getialist` (the actual endpoint behind `/view_event`, confirmed via `VieweventComponent.loadData()`) |
| Response shape | `{data: [...], totalRecords}` | `{data: {item1: {totalCount,...}, item2: [...events]}}` — confirmed from the same method |
| Field casing | PascalCase (`EventName`, `VenueDetails`, ...) | camelCase (`eventName`, `componentName`, `eventDate`, `uniqueEventId`, ...) — confirmed via `displayedColumns` and the `onViewFeedback(item.uniqueEventId)` template binding, which also confirms the GUID field powering the feedback-page link is `uniqueEventId` |

The sheet schema was also updated to match what was actually requested:
Event ID, **Component**, Event Name, Event Date, Month, Feedback By,
Feedback, Rating, Scraped At — `District / Venue` (not a field this
endpoint exposes, and not asked for) was dropped in favor of `Component`
(confirmed present, explicitly requested).

