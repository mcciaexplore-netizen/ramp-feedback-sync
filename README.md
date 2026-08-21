# RAMP Event Feedback → Google Sheets

Pulls all MSME event feedback from the MSSIDC RAMP portal
(`https://mssidc.maharashtra.gov.in/RAMP/#/view_event`) and syncs it into
the Google Sheet "Feedback MSSIDC"
(`16LzFOcdDMjbf9vikoyuzPzGOx29vRq85WpOhKMFBti0`).

## Read this first: how login works here

Every event/feedback route on RAMP requires an authenticated MSSIDC/IA/
MSME/DIC login, gated by a CAPTCHA — confirmed in [RECON.md](./RECON.md).
This project **does not automate that login or its CAPTCHA**, even though
the CAPTCHA turned out to be weak (client-side only). Instead:

- Running `main.py` (any mode except `--recon-only`) opens a real, visible
  browser window on the actual RAMP login page.
- **You log in yourself** — pick your role, type your username and
  password, and solve the CAPTCHA, exactly like normal manual use.
- Once the app logs you in, the script reads the session token it stores
  and reuses it for the rest of the run (paging through events, pulling
  feedback, normalizing, deduping, writing to Sheets) via direct API calls
  — no browser needed after that point.
- You'll need to repeat the manual login step each time you run the
  script (sessions expire), which is the tradeoff for never scripting
  around the login/CAPTCHA. See RECON.md's addendum for the full reasoning.

**Use the IA role** — this is scoped to one Industry Association's own
events and feedback (confirmed working: logging in as IA and pulling from
`/view_event` + `/viewFeedbacksListia/:id`). An MSSIDC login would see all
events statewide instead, if that's ever needed.

## Project layout

```
ramp-feedback-sync/
├── recon.py          # Phase 0 recon — re-run any time to re-verify the login requirement
├── auth.py           # opens the login page, waits for you to log in, captures the session token
├── source_client.py  # httpx client: events -> per-event respondents -> per-respondent Q&A
├── sheets_sync.py     # normalize to the fixed schema, dedup, batch write to Sheets + RunLog tab
├── main.py            # CLI entrypoint wiring the above together
├── weekly_report.py   # weekly entrypoint: sync + "this week" analysis + email
├── emailer.py         # Gmail SMTP send (App Password auth)
├── run_weekly.sh                        # shell wrapper the LaunchAgent calls
├── com.mccia.ramp-weekly-report.plist   # macOS LaunchAgent: fires weekly_report.py every Monday 11am
├── requirements.txt
├── .env.example
├── RECON.md            # Phase 0 findings + the endpoint chain this implementation uses
└── README.md
```

## Setup

```bash
cd ramp-feedback-sync
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

Then fill in `.env`:
- `GOOGLE_SHEET_ID` — already set to the target sheet.
- `GOOGLE_SERVICE_ACCOUNT_JSON` — path to a service-account key JSON with
  edit access to that sheet (share the sheet with the service account's
  email). **Never commit this file** — it's in `.gitignore`.

There is no username/password field in `.env` — see above.

## Running

**First time — sanity-check the shape of the data:**
```bash
python main.py --dry-run --limit-events 3
```
Log in when the browser window opens. This scrapes just 3 events, prints a
summary, and writes `dry_run_output.json` / `dry_run_output.csv` locally —
no Sheets write happens. Check that Event Name/Date/Venue and Feedback
Text/Rating look right before trusting a full run. The per-respondent
feedback-detail endpoint (`msme/getmsmecustomfeedbackquestion`) was
reverse-engineered from the app's own code but never tested against a live
response (see RECON.md) — this is the step most likely to need a tweak, so
check its output first.

**Full run:**
```bash
python main.py
```
Logs in the same way, then scrapes every event, dedupes against what's
already in the sheet, batch-appends new rows, and writes a summary to the
`RunLog` tab.

**Re-verify the login-requirement finding at any time:**
```bash
python main.py --recon-only
```

## Weekly automated report

`weekly_report.py` is a separate entrypoint, fired automatically every
**Monday at 11:00 AM** by a macOS LaunchAgent. In one run it: opens the
login browser, syncs any new feedback into the sheet (same logic as
`main.py`), pulls fresh enrolled/attended counts for the past 7 days of
events, and **emails an HTML summary** to the configured recipients.

**The login step is still manual** — that part can't be automated (see
above). What changes is *when* it asks: a desktop notification fires the
moment the browser window opens, and the script waits up to
`LOGIN_TIMEOUT_SECONDS` (30 min by default for this entrypoint) for you to
log in. If nothing happens within that window the run fails and you get a
"RAMP weekly run FAILED" notification instead of a silent no-op. **Your Mac
needs to be on and awake around 11:00 AM Monday** for this to fire —
launchd does not wake a sleeping machine for a missed calendar job.

### One-time setup: Zoho application-specific password

The report is sent via SMTP from `aistudio@mcciapune.com`, a **Zoho Mail**
account (not Gmail). Generate an application-specific password for it
(requires 2FA to be enabled on the account) at
https://accounts.zoho.in/home#security/apppassword — use the `.com`
equivalent instead if this account isn't on Zoho's India cluster — then set
in `.env`:

```
SMTP_USER=aistudio@mcciapune.com
SMTP_APP_PASSWORD=<the application-specific password>
SMTP_HOST=smtp.zoho.in
SMTP_PORT=465
REPORT_RECIPIENTS=gunjan.fellow@mcciapune.com,neerajt@mcciapune.com,data@mcciapune.com,ismail.fellow@mcciapune.com
```

`SMTP_HOST=smtp.zoho.in` is a guess based on this being an India-based
account — if `weekly_report.py` fails to send, check which regional
cluster `aistudio@mcciapune.com` actually lives on (Zoho Mail → Settings →
look at the account's data center) and correct `SMTP_HOST` accordingly
(`smtp.zoho.com` for the global/US cluster, `smtp.zoho.eu` for EU, etc).

Never the real account password, and never commit `.env`.

**Test it manually first, before relying on the schedule:**
```bash
python weekly_report.py
```

### Installing the weekly schedule

```bash
cp com.mccia.ramp-weekly-report.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.mccia.ramp-weekly-report.plist
```

Logs from each scheduled run go to `weekly_report_output.log` (script
output) and `launchd_stdout.log` / `launchd_stderr.log` (launchd-level
output, should normally be empty).

**To stop the weekly schedule:**
```bash
launchctl unload ~/Library/LaunchAgents/com.mccia.ramp-weekly-report.plist
rm ~/Library/LaunchAgents/com.mccia.ramp-weekly-report.plist
```

**To change the day/time**, edit the `StartCalendarInterval` block in
`com.mccia.ramp-weekly-report.plist` (`Weekday`: 0=Sunday..6=Saturday,
1=Monday) then re-copy and reload it as above.

## Schema written to Sheet1

| Column | Notes |
|---|---|
| Event ID | |
| Component | e.g. "Skill Development" |
| Event Name | |
| Event Date | raw date string from the portal |
| Month | derived "Month YYYY" from Event Date, for easy filtering/pivoting — see RECON.md re: date-format confidence |
| Enrollment ID | the MSME's enrollment for this specific event — also the dedup key |
| Name | respondent name (or Udyam registration number if name absent) |
| Mobile Number | |
| Email | |
| District | |
| Feedback | all questions/answers for that submission, flattened — see RECON.md addendum |
| Rating | first rating-scale answer found (Excellent/Good/Neutral/Poor/Yes/No), else blank |
| Scraped At | ISO timestamp of the run that wrote the row |

A demo row (`Event ID = DEMO`) is written immediately when a full run
starts, before the (slower) scrape begins, so you have an instant signal
in the sheet that the automation is running — delete it once real rows
show up.

Dedup key: Event ID + Enrollment ID (a genuinely unique identifier for one
person's one submission to one event) — falls back to a hash of Name +
Feedback when Enrollment ID is missing. Reconstructed by re-reading the
sheet, so re-running never duplicates rows.

The sheet is formatted on first write: frozen bold header row, sized
columns, wrapped text in the Feedback column, and banded (alternating)
rows so it reads as a report rather than a raw dump.

## Rate limiting & error handling

- `REQUEST_DELAY_SECONDS` (default 2s) between every API call — this is a
  government server; the client never parallelizes requests.
- `MAX_RETRIES` (default 3) with exponential backoff on timeouts/connection
  errors.
- A failure fetching one event's respondents, or one respondent's detail,
  is logged and skipped — it doesn't stop the whole run. All errors are
  collected and written to the `RunLog` tab (and printed) at the end.
- A `401` mid-run means your session expired — the run stops with a clear
  message; just re-run `main.py` and log in again.

## Known gaps / things to verify on your first real run

- **Month** is derived from `Event Date` by best-effort parsing (ISO,
  common `dd-mm-yyyy`/`mm-dd-yyyy` formats, and the ASP.NET `/Date(...)/`
  wrapper). The exact format the API returns wasn't confirmed live — check
  a few rows after your first `--dry-run` and adjust `derive_month()` in
  `sheets_sync.py` if it comes back blank.
- **Rating extraction** picks the *first* question whose answer matches a
  known rating word (Excellent/Good/Neutral/Poor/Yes/No). If an event's
  form asks multiple such questions, only the first one lands in the
  Rating column — the rest are still captured in Feedback Text.
- If a respondent record has no `enrollmentId`, the script emits a row
  with blank Feedback Text/Rating rather than dropping it — check
  `dry_run_output.json` for any of these and decide if that's the right
  call for your reporting needs.
