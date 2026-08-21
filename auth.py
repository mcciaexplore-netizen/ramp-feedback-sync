"""
Human-completes-the-login, script-reuses-the-session auth flow.

Per RECON.md, every RAMP route (and the backend API behind it) requires an
authenticated session, and the login form is gated by a CAPTCHA. This
project does not automate credentials or solve the CAPTCHA — see
RECON.md section 3 for why. Instead:

1. A real (visible) Chromium window opens on the actual login page.
2. A human selects a role (this project expects "MSSIDC" — see below),
   types their own username/password, and solves the CAPTCHA themselves,
   exactly as they would in normal manual use.
3. Once the app's own login handler stores an auth token in
   sessionStorage (confirmed in main.js: `sessionStorage.setItem(
   "authToken", res.token)`), this script reads it out and hands it to
   the rest of the pipeline as a plain Bearer token for direct API calls.

No login/CAPTCHA automation happens here — only observation of a session
a human established themselves.
"""

from playwright.sync_api import sync_playwright

LOGIN_URL = "https://mssidc.maharashtra.gov.in/RAMP/#/login"


def interactive_login(timeout_seconds: int = 300) -> dict:
    """Open a real browser, wait for a human to log in, return the session.

    Returns a dict with: token, user_type, name.
    Raises RuntimeError if no login happens within timeout_seconds.
    """
    print(f"Opening {LOGIN_URL} ...")
    print(
        "Please log in yourself in the browser window that just opened: "
        "pick the MSSIDC role, enter your username/password, and solve the "
        "CAPTCHA. This script will detect your login automatically and "
        f"continue (waiting up to {timeout_seconds}s)."
    )

    with sync_playwright() as p:
        # --start-maximized + no_viewport so the window is impossible to
        # miss among other open windows — a default small window here was
        # getting lost and the login timing out unnoticed.
        browser = p.chromium.launch(headless=False, args=["--start-maximized"])
        context = browser.new_context(no_viewport=True)
        page = context.new_page()
        page.goto(LOGIN_URL)
        page.bring_to_front()

        try:
            page.wait_for_function(
                "() => !!sessionStorage.getItem('authToken')",
                timeout=timeout_seconds * 1000,
            )
        except Exception as e:
            browser.close()
            raise RuntimeError(
                f"No login detected within {timeout_seconds}s. Re-run and "
                "log in before the timeout, or raise LOGIN_TIMEOUT_SECONDS "
                "in .env to allow more time."
            ) from e

        token = page.evaluate("() => sessionStorage.getItem('authToken')")
        user_type = page.evaluate("() => sessionStorage.getItem('UserType')")
        name = page.evaluate("() => sessionStorage.getItem('name')")

        print(f"Logged in as UserType={user_type!r}, name={name!r}.")
        browser.close()

    return {"token": token, "user_type": user_type, "name": name}
