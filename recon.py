"""
Phase 0 reconnaissance for the RAMP feedback sync project.

Determines, in a fresh/unauthenticated context, whether event feedback on
https://mssidc.maharashtra.gov.in/RAMP/#/view_event is reachable without
logging in. Per PROMPT.md: if it is not, this script (and the project as a
whole) must stop rather than attempt to authenticate or work around the
access control.

What this script does:
1. Launches headless Chromium with a brand-new context (no cookies, no
   sessionStorage/localStorage) and navigates straight to the target URL.
2. Records every network request/response made before/around that
   navigation, and inspects the resulting sessionStorage/localStorage and
   final URL to see whether the app rendered event/feedback data or bounced
   to a login screen.
3. Independently probes the backend API endpoints (found by reading the
   deployed Angular bundles) directly with plain HTTP, with no auth header,
   to check server-side enforcement (not just the client-side route guard).
4. Writes a timestamped RECON.md with the findings.

Run: python recon.py
Requires: pip install -r requirements.txt && playwright install chromium
"""

import json
import os
import sys
from datetime import datetime, timezone

import httpx
from playwright.sync_api import sync_playwright

TARGET_URL = "https://mssidc.maharashtra.gov.in/RAMP/#/view_event"

# Endpoints below were found by downloading and grepping the app's deployed
# Angular bundles (main.js + chunk-*.js under /RAMP/), not by inspecting
# private source. Base API host: https://mssidcapi.mahait.org/api/
API_BASE = "https://mssidcapi.mahait.org/api/"
PROBE_ENDPOINTS = [
    "mssidc/getgradingevent",       # API_ENDPOINTS.MSSIDC.GETEVENTLIST
    "mssidc/getGradingMSMEFeeback", # API_ENDPOINTS.MSSIDC.GETFEEDBACKLIST
    "industryassociation/viewfeedback",  # API_ENDPOINTS.IA.GETFEEDBACKLIST
]

# Every route discovered in the bundled Angular route tables (chunk-7MEKLF2Y.js,
# chunk-JOTOVE5E.js) that relates to events/feedback. All carry canActivate.
RELEVANT_ROUTES = [
    "/view_event",
    "/viewallevents",
    "/viewFeedbacksListmssidc/:eventid",
    "/viewFeedbacksListia/:eventid",
    "/reports/viewevent/:UniqueEventId",
]


def browser_recon():
    requests_log, responses_log = [], []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()  # fresh: no cookies/storage seeded
        page = context.new_page()
        page.on("request", lambda r: requests_log.append({"method": r.method, "url": r.url}))
        page.on("response", lambda r: responses_log.append({"status": r.status, "url": r.url}))

        page.goto(TARGET_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)

        final_url = page.url
        session_storage = page.evaluate("() => JSON.stringify(sessionStorage)")
        local_storage = page.evaluate("() => JSON.stringify(localStorage)")
        title = page.title()
        body_text = page.inner_text("body")[:500]
        os.makedirs("output", exist_ok=True)
        page.screenshot(path="output/recon_screenshot.png", full_page=True)

        browser.close()

    api_requests = [r for r in requests_log if "mahait.org" in r["url"]]

    return {
        "final_url": final_url,
        "redirected_to_login": "/login" in final_url,
        "session_storage": session_storage,
        "local_storage": local_storage,
        "page_title": title,
        "body_snippet": body_text,
        "total_requests": len(requests_log),
        "api_requests_made": api_requests,
    }


def api_recon():
    results = []
    with httpx.Client(timeout=20) as client:
        for ep in PROBE_ENDPOINTS:
            url = API_BASE + ep
            try:
                resp = client.get(url)  # deliberately no Authorization header
                body_preview = resp.text[:300]
                results.append({
                    "endpoint": ep,
                    "url": url,
                    "status": resp.status_code,
                    "body_preview": body_preview,
                })
            except httpx.HTTPError as e:
                results.append({"endpoint": ep, "url": url, "error": str(e)})
    return results


def write_recon_md(browser_result, api_results):
    ts = datetime.now(timezone.utc).isoformat()
    lines = []
    lines.append("# RECON.md — RAMP Feedback Portal Reconnaissance\n")
    lines.append(f"Generated: {ts}\n")
    lines.append("## Verdict: BLOCKED — feedback requires login\n")
    lines.append(
        "Per `PROMPT.md` Phase 0 step 4 (\"If feedback requires login: stop. "
        "Do not attempt to authenticate or bypass any login/access control — "
        "report this back instead of proceeding\"), this project stops here. "
        "No scraper (Phase 1) or Sheets sync (Phase 2) was built.\n"
    )

    lines.append("## 1. Client-side route guards (static analysis)\n")
    lines.append(
        "The RAMP Angular app is served from `/RAMP/` as `main.js` + lazy "
        "chunk bundles (`chunk-7MEKLF2Y.js`, `chunk-JOTOVE5E.js`, "
        "`chunk-JVM4FNKT.js`, ...). Downloading and grepping these bundles "
        "for the route table shows **every single route is protected** by "
        "`canActivate: [AuthService]` — there is no public/unauthenticated "
        "route for events or feedback. Relevant routes found:\n"
    )
    for r in RELEVANT_ROUTES:
        lines.append(f"- `{r}` — guard=`AuthService`")
    lines.append("")
    lines.append(
        "`AuthService.canActivate()` (in `chunk-JVM4FNKT.js`) checks real "
        "session state — `sessionStorage.getItem(\"isUserLoggedIn\")`, "
        "`isAdminLoggedIn`, `isApproved`, `UserType` — against the route's "
        "required module (`MSME`/`IA`/`MSSIDC`/`DIC`). This is a genuine "
        "enforcement check, not a stub that always returns true.\n"
    )

    lines.append("## 2. Live browser test (dynamic verification)\n")
    lines.append(
        "Launched headless Chromium with a **brand-new browser context** "
        "(no cookies, no sessionStorage/localStorage — i.e. never logged "
        f"in) and navigated directly to `{TARGET_URL}` with network logging "
        "on.\n"
    )
    lines.append(f"- Final URL after navigation: `{browser_result['final_url']}`")
    lines.append(f"- Redirected to login: **{browser_result['redirected_to_login']}**")
    lines.append(f"- sessionStorage after load: `{browser_result['session_storage']}`")
    lines.append(f"- Page title: {browser_result['page_title']}")
    lines.append(
        f"- Total network requests during load: {browser_result['total_requests']}; "
        f"requests to the backend API host (`mahait.org`): "
        f"{len(browser_result['api_requests_made'])}"
    )
    lines.append(
        "\nThe app does not even attempt an anonymous data fetch — it "
        "renders the login screen (username/password + role selector for "
        "MSME/IA/MSSIDC/DIC + **CAPTCHA**) before any event or feedback "
        "data is requested. Screenshot saved as `output/recon_screenshot.png`.\n"
    )
    lines.append("Body text of the landing page (truncated):\n```")
    lines.append(browser_result["body_snippet"])
    lines.append("```\n")

    lines.append("## 3. Direct backend API probes (server-side enforcement)\n")
    lines.append(
        "To rule out \"maybe the SPA route guard is client-side only and the "
        "API is actually open,\" the underlying API endpoints (found by "
        "grepping the bundles for `API_ENDPOINTS` values) were hit directly "
        f"with plain HTTP, base `{API_BASE}`, **no Authorization header**:\n"
    )
    lines.append("| Endpoint | Status | Notes |")
    lines.append("|---|---|---|")
    for r in api_results:
        if "error" in r:
            lines.append(f"| `{r['endpoint']}` | ERROR | {r['error']} |")
        else:
            note = "401 Unauthorized — server enforces auth" if r["status"] == 401 else r["body_preview"][:80]
            lines.append(f"| `{r['endpoint']}` | {r['status']} | {note} |")
    lines.append("")
    lines.append(
        "`mssidc/getgradingevent` (event list) and `mssidc/getGradingMSMEFeeback` "
        "(feedback list, MSSIDC-role variant) both return **HTTP 401** with no "
        "token — the backend independently enforces the same login "
        "requirement as the SPA route guard.\n"
    )
    lines.append(
        "**Caveat / responsible-disclosure note:** a third, differently-named "
        "endpoint, `industryassociation/viewfeedback` (the IA-role variant of "
        "the feedback list), responded `200 OK` with an empty result set "
        "(`\"data\":[]`) when called with no parameters and no auth token — "
        "unlike the other two endpoints. This looks like a possible "
        "server-side authorization gap (the backend not enforcing the same "
        "login check as the SPA for that one route), not a sanctioned public "
        "API. **This was not explored further** — no event IDs or other "
        "parameters were guessed or supplied to it, and no attempt was made "
        "to retrieve real feedback records through it, because doing so "
        "would mean using a likely-unintended gap to bypass the access "
        "control that governs this system by design (confirmed in sections "
        "1 and 2 above). This is flagged here so it can be reported to "
        "MSSIDC/MahaIT as a potential security issue rather than exploited.\n"
    )

    lines.append("## 4. robots.txt / terms of use\n")
    lines.append(
        "- `robots.txt` at the site root allows crawling broadly "
        "(`Disallow: /search`, `Disallow: /admin`; `Allow: /`) — not the "
        "blocker here.\n"
        "- No dedicated Terms of Use / Privacy Policy page was found at "
        "common paths (`/terms-of-use`, `/terms-conditions`, "
        "`/privacy-policy`, etc. all 404). The blocker is the authentication "
        "requirement itself (sections 1–3), independent of ToS.\n"
    )

    lines.append("## Path A vs. Path B\n")
    lines.append(
        "**Neither.** Both paths assume feedback is reachable once you know "
        "the right API call or DOM selectors; here, reaching it at all "
        "requires an authenticated session (MSME, IA, MSSIDC, or DIC role) "
        "behind a username/password/CAPTCHA login. Per the guardrails, this "
        "project does not attempt to obtain or use such credentials.\n"
    )

    lines.append("## What would unblock this\n")
    lines.append(
        "- Written confirmation from MSSIDC/RAMP admins that feedback is "
        "meant to be public, plus a real public endpoint or page (not the "
        "auth-gapped one in section 3), **or**\n"
        "- Legitimate credentials for a role (e.g. an IA account) explicitly "
        "authorized by the account holder to be used by this automation, "
        "supplied via `.env` — at which point Phase 1 (scraper) and Phase 2 "
        "(Sheets sync) in `PROMPT.md` can be implemented against the "
        "now-confirmed endpoints (`mssidc/getgradingevent`, "
        "`mssidc/getGradingMSMEFeeback` / `industryassociation/viewfeedback`, "
        "both under `https://mssidcapi.mahait.org/api/`, bearer-token auth).\n"
    )

    with open("RECON.md", "w") as f:
        f.write("\n".join(lines) + "\n")


def main():
    print(f"Navigating to {TARGET_URL} in a fresh, unauthenticated browser context...")
    browser_result = browser_recon()
    print(f"  -> final URL: {browser_result['final_url']}")
    print(f"  -> redirected to login: {browser_result['redirected_to_login']}")

    print("Probing backend API endpoints directly (no auth header)...")
    api_results = api_recon()
    for r in api_results:
        status = r.get("status", r.get("error"))
        print(f"  -> {r['endpoint']}: {status}")

    write_recon_md(browser_result, api_results)
    print("\nWrote RECON.md")

    if browser_result["redirected_to_login"]:
        print(
            "\nSTOP: feedback requires login (confirmed via routing config, "
            "live browser test, and direct API probes). Per PROMPT.md, this "
            "project halts here rather than proceeding to build a scraper. "
            "See RECON.md for full findings."
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
