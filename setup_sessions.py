"""
SESSION SETUP — saves browser login state for companies with auth walls.

Two modes:
  1. MANUAL (default) — opens a visible browser, you log in, session saved on close
  2. AUTO             — uses credentials from .env to log in automatically

Usage:
  python3 setup_sessions.py              # interactive menu (manual login)
  python3 setup_sessions.py --auto       # auto-login all companies with credentials in .env
  python3 setup_sessions.py --auto stripe linkedin   # auto-login specific companies

Add credentials to .env:
  STRIPE_EMAIL=your@email.com
  STRIPE_PASSWORD=yourpassword
  LINKEDIN_EMAIL=your@email.com
  LINKEDIN_PASSWORD=yourpassword
  (etc.)
"""

import asyncio, argparse, json, os, sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from playwright.async_api import async_playwright, Page

SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

# ── Company definitions ────────────────────────────────────────────────────────
COMPANIES = {
    "stripe": {
        "name":       "Stripe",
        "login_url":  "https://stripe.com/login",
        "session":    "sessions/stripe.json",
        "email_sel":  "input[name='email'], input[id='email'], input[type='email']",
        "pass_sel":   "input[name='password'], input[id='password'], input[type='password']",
        "submit_sel": "button[type='submit']",
        "success":    "dashboard.stripe.com",
        "env_email":  "STRIPE_EMAIL",
        "env_pass":   "STRIPE_PASSWORD",
    },
    "okta": {
        "name":       "Okta",
        "login_url":  "https://www.okta.com/company/careers/",
        "session":    "sessions/okta.json",
        "email_sel":  "input[name='username'], input[type='email']",
        "pass_sel":   "input[name='password'], input[type='password']",
        "submit_sel": "input[type='submit'], button[type='submit']",
        "success":    "okta.com",
        "env_email":  "OKTA_EMAIL",
        "env_pass":   "OKTA_PASSWORD",
    },
    "airbnb": {
        "name":       "Airbnb",
        "login_url":  "https://www.airbnb.com/login",
        "session":    "sessions/airbnb.json",
        "email_sel":  "input[name='email'], input[id='email']",
        "pass_sel":   "input[name='password'], input[id='password']",
        "submit_sel": "button[type='submit']",
        "success":    "airbnb.com/account",
        "env_email":  "AIRBNB_EMAIL",
        "env_pass":   "AIRBNB_PASSWORD",
    },
    "databricks": {
        "name":       "Databricks",
        "login_url":  "https://accounts.cloud.databricks.com/login",
        "session":    "sessions/databricks.json",
        "email_sel":  "input[name='username'], input[type='email']",
        "pass_sel":   "input[name='password'], input[type='password']",
        "submit_sel": "button[type='submit']",
        "success":    "accounts.cloud.databricks.com",
        "env_email":  "DATABRICKS_EMAIL",
        "env_pass":   "DATABRICKS_PASSWORD",
    },
    "linkedin": {
        "name":       "LinkedIn",
        "login_url":  "https://www.linkedin.com/login",
        "session":    "sessions/linkedin.json",
        "email_sel":  "input#username, input[name='session_key']",
        "pass_sel":   "input#password, input[name='session_password']",
        "submit_sel": "button[type='submit'], button[data-litms-control-urn*='login']",
        "success":    "linkedin.com/feed",
        "env_email":  "LINKEDIN_EMAIL",
        "env_pass":   "LINKEDIN_PASSWORD",
    },
}


def check_status():
    print("\nCurrent session status:")
    print(f"  {'Company':<14} {'Session':<10} {'Cookies'}")
    print("  " + "─" * 40)
    for key, co in COMPANIES.items():
        path = Path(co["session"])
        if path.exists():
            try:
                d = json.loads(path.read_text())
                c = len(d.get("cookies", []))
                print(f"  {co['name']:<14} ✅ saved   {c} cookies")
            except Exception:
                print(f"  {co['name']:<14} ⚠️  corrupt")
        else:
            print(f"  {co['name']:<14} ❌ not set")


async def manual_setup(co: dict):
    """Open visible browser, wait for user to log in, save session on close."""
    print(f"\n  Opening browser at {co['login_url']}")
    print(f"  >> Log in to {co['name']}, then CLOSE THE BROWSER TAB/WINDOW.")
    print(f"  >> Session will be saved automatically when you close.\n")

    async with async_playwright() as pw:
        browser = await pw.webkit.launch(headless=False)
        ctx  = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        await page.goto(co["login_url"])

        # Wait until the browser is closed or navigated away from login
        print("  Waiting for you to complete login...")
        try:
            # Give up to 5 minutes for manual login
            await page.wait_for_url(f"**{co['success']}**", timeout=300000)
            print(f"  ✅ Logged in to {co['name']}!")
        except Exception:
            # Just save whatever state we have
            pass

        path = Path(co["session"])
        path.parent.mkdir(exist_ok=True)
        await ctx.storage_state(path=str(path))
        await browser.close()

    d = json.loads(path.read_text())
    print(f"  ✅ Session saved → {co['session']} ({len(d.get('cookies', []))} cookies)")


async def auto_setup(co: dict) -> bool:
    """Attempt automated login using credentials from .env."""
    email = os.getenv(co["env_email"], "")
    passw = os.getenv(co["env_pass"], "")

    if not email or not passw:
        print(f"  ⚠️  {co['name']}: No credentials in .env ({co['env_email']} / {co['env_pass']})")
        print(f"      Add them to .env or use manual mode.")
        return False

    print(f"  Attempting auto-login to {co['name']} as {email}...")

    async with async_playwright() as pw:
        browser = await pw.webkit.launch(headless=True)
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
            viewport={"width": 1280, "height": 900},
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await ctx.new_page()

        try:
            await page.goto(co["login_url"], timeout=20000)
            await page.wait_for_timeout(1500)

            # Fill email
            email_el = await page.query_selector(co["email_sel"])
            if not email_el:
                print(f"  ❌ Could not find email field on {co['name']} login page.")
                await browser.close()
                return False
            await email_el.fill(email)
            await page.wait_for_timeout(500)

            # Some login flows have a "Continue" button before password
            continue_btn = await page.query_selector(
                "button:has-text('Continue'), button:has-text('Next'), "
                "input[type='submit']:not([value='Sign in'])"
            )
            if continue_btn:
                await continue_btn.click()
                await page.wait_for_timeout(1500)

            # Fill password
            pass_el = await page.query_selector(co["pass_sel"])
            if pass_el:
                await pass_el.fill(passw)
                await page.wait_for_timeout(300)

            # Submit
            submit = await page.query_selector(co["submit_sel"])
            if submit:
                await submit.click()

            # Wait for success URL
            try:
                await page.wait_for_url(f"**{co['success']}**", timeout=15000)
                success = True
            except Exception:
                # Check if we're on a 2FA page
                body = (await page.content()).lower()
                if "verify" in body or "two-factor" in body or "2fa" in body or "code" in body:
                    print(f"  ⚠️  {co['name']} requires 2FA — switching to manual mode...")
                    await browser.close()
                    await manual_setup(co)
                    return True
                success = co["success"].split("/")[0] in page.url

            if success:
                path = Path(co["session"])
                path.parent.mkdir(exist_ok=True)
                await ctx.storage_state(path=str(path))
                await browser.close()
                d = json.loads(path.read_text())
                print(f"  ✅ Auto-login success! Session saved → {co['session']} ({len(d.get('cookies',[]))} cookies)")
                return True
            else:
                print(f"  ❌ Auto-login failed (wrong credentials or CAPTCHA). Try manual mode.")
                await browser.close()
                return False

        except Exception as e:
            print(f"  ❌ Auto-login error: {e}")
            await browser.close()
            return False


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto",   action="store_true", help="Auto-login using .env credentials")
    parser.add_argument("companies", nargs="*", help="Specific companies (default: all)")
    args = parser.parse_args()

    keys = args.companies if args.companies else list(COMPANIES.keys())
    # Filter to valid keys only
    keys = [k for k in keys if k in COMPANIES]
    if not keys:
        keys = list(COMPANIES.keys())

    print("\n" + "="*55)
    print("  JOB AUTOMATION — SESSION SETUP")
    print("="*55)
    check_status()

    if args.auto:
        print(f"\n  AUTO MODE — using .env credentials for: {', '.join(keys)}")
        for key in keys:
            await auto_setup(COMPANIES[key])
    else:
        print(f"\n  MANUAL MODE — interactive browser login")
        print(f"  Companies: {', '.join(keys)}")
        print("\n  Options:")
        for i, key in enumerate(keys, 1):
            co = COMPANIES[key]
            exists = "✅" if Path(co["session"]).exists() else "❌"
            print(f"    [{i}] {co['name']} {exists}")
        print(f"    [a] All")
        print(f"    [q] Quit")

        choice = input("\n  Choose: ").strip().lower()
        if choice == "q":
            return
        elif choice == "a":
            selected = keys
        elif choice.isdigit() and 1 <= int(choice) <= len(keys):
            selected = [keys[int(choice) - 1]]
        else:
            print("  Invalid choice.")
            return

        for key in selected:
            co = COMPANIES[key]
            print(f"\n{'─'*55}")
            print(f"  Setting up: {co['name']}")
            print(f"{'─'*55}")
            await manual_setup(co)

    print("\n  Final status:")
    check_status()
    print("\n  Done! Agent 3 will now auto-login to these companies.")
    print("  Run: python3 resume_pipeline.py  to submit applications.\n")


if __name__ == "__main__":
    asyncio.run(main())
