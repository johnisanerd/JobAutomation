"""
System health check — run before launching the full pipeline.
Tests all APIs, tools, and infrastructure.
"""
import asyncio, json, os, shutil, subprocess, sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

import httpx

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
RAPIDAPI_KEY  = os.getenv("RAPIDAPI_KEY", "")
APIFY_TOKEN   = os.getenv("APIFY_API_TOKEN", "")
GMAIL_TOKEN   = os.getenv("GMAIL_ACCESS_TOKEN", "")
HUNTER_KEY    = os.getenv("HUNTER_API_KEY", "")

PASS = "✅ PASS"
FAIL = "❌ FAIL"
SKIP = "⚠️  SKIP"

results = {}

# ── 1. Anthropic API ──────────────────────────────────────────────────────────
def check_anthropic():
    if not ANTHROPIC_KEY or ANTHROPIC_KEY.startswith("sk-ant-..."):
        results["Anthropic API"] = (SKIP, "key not set")
        return
    try:
        import anthropic
        c = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        m = c.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=10,
            messages=[{"role": "user", "content": "say ok"}]
        )
        results["Anthropic API"] = (PASS, f"model={m.model} tokens={m.usage.input_tokens}in")
    except Exception as e:
        results["Anthropic API"] = (FAIL, str(e)[:100])

# ── 2. RapidAPI LinkedIn ──────────────────────────────────────────────────────
async def check_rapidapi():
    if not RAPIDAPI_KEY:
        results["RapidAPI LinkedIn"] = (SKIP, "key not set")
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://linkedin-job-search-api.p.rapidapi.com/active-jb-7d",
                headers={"X-RapidAPI-Key": RAPIDAPI_KEY,
                         "X-RapidAPI-Host": "linkedin-job-search-api.p.rapidapi.com"},
                params={"title_filter": "Software Engineer", "location_filter": "Dublin, Ireland", "count": "3"},
            )
            if r.status_code == 200:
                jobs = r.json() if isinstance(r.json(), list) else r.json().get("jobs", [])
                results["RapidAPI LinkedIn"] = (PASS, f"{len(jobs)} jobs returned (linkedin-job-search-api)")
            else:
                results["RapidAPI LinkedIn"] = (FAIL, f"HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        results["RapidAPI LinkedIn"] = (FAIL, str(e)[:100])

# ── 3. Apify ──────────────────────────────────────────────────────────────────
async def check_apify():
    if not APIFY_TOKEN:
        results["Apify"] = (SKIP, "token not set")
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://api.apify.com/v2/users/me",
                headers={"Authorization": f"Bearer {APIFY_TOKEN}"},
            )
            if r.status_code == 200:
                data = r.json()
                plan = data.get("data", {}).get("plan", {}).get("id", "unknown")
                credits = data.get("data", {}).get("monthlyUsage", {}).get("totalCostUsd", "?")
                results["Apify"] = (PASS, f"plan={plan} monthly_used=${credits}")
            else:
                results["Apify"] = (FAIL, f"HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        results["Apify"] = (FAIL, str(e)[:100])

# ── 4. Gmail ──────────────────────────────────────────────────────────────────
async def check_gmail():
    token = GMAIL_TOKEN
    # Try refreshing from token.json if placeholder
    if not token or token.startswith("ya29...."):
        if Path("token.json").exists():
            try:
                from google.oauth2.credentials import Credentials
                from google.auth.transport.requests import Request
                creds = Credentials.from_authorized_user_file("token.json")
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                token = creds.token
            except Exception as e:
                results["Gmail API"] = (SKIP, f"token.json refresh failed: {e}")
                return
        else:
            results["Gmail API"] = (SKIP, "run gmail_auth.py first")
            return

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                headers={"Authorization": f"Bearer {token}"},
                params={"maxResults": 3, "labelIds": "INBOX"},
            )
            if r.status_code == 200:
                msgs = r.json().get("messages", [])
                subjects = []
                for m in msgs[:3]:
                    d = await c.get(
                        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}",
                        headers={"Authorization": f"Bearer {token}"},
                        params={"format": "metadata", "metadataHeaders": "Subject"},
                    )
                    hdrs = {h["name"]: h["value"] for h in d.json().get("payload", {}).get("headers", [])}
                    subjects.append(hdrs.get("Subject", "(no subject)")[:50])
                results["Gmail API"] = (PASS, f"inbox last 3: {' | '.join(subjects)}")
            else:
                results["Gmail API"] = (FAIL, f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        results["Gmail API"] = (FAIL, str(e)[:100])

# ── 5. Hunter.io ──────────────────────────────────────────────────────────────
async def check_hunter():
    if not HUNTER_KEY:
        results["Hunter.io"] = (SKIP, "key not set")
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": "stripe.com", "api_key": HUNTER_KEY, "limit": 2},
            )
            if r.status_code == 200:
                data = r.json()
                found = data.get("data", {}).get("total", 0)
                quota = data.get("meta", {}).get("calls_remaining", "?")
                results["Hunter.io"] = (PASS, f"stripe.com: {found} emails found | calls_remaining={quota}")
            else:
                results["Hunter.io"] = (FAIL, f"HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        results["Hunter.io"] = (FAIL, str(e)[:100])

# ── 6. pdflatex ───────────────────────────────────────────────────────────────
def check_pdflatex():
    path = shutil.which("xelatex") or shutil.which("pdflatex") or (Path.home() / "Library/TinyTeX/bin/universal-darwin/xelatex").exists()
    if not path:
        results["pdflatex"] = (SKIP, "not installed — run: brew install --cask basictex")
        return
    try:
        out = subprocess.check_output([str(Path.home() / "Library/TinyTeX/bin/universal-darwin/xelatex"), "--version"], text=True).splitlines()[0]
        results["pdflatex"] = (PASS, out)
    except Exception as e:
        results["pdflatex"] = (FAIL, str(e)[:100])

# ── 7. resume/main.tex ────────────────────────────────────────────────────────
def check_resume():
    tex = Path("resume/main.tex")
    if not tex.exists():
        results["resume/main.tex"] = (FAIL, "file not found — copy your .tex file to resume/main.tex")
        return
    text = tex.read_text(encoding="utf-8", errors="ignore")
    if r"\documentclass" in text or r"\begin{document}" in text:
        size = tex.stat().st_size
        results["resume/main.tex"] = (PASS, f"{size:,} bytes, valid LaTeX")
    else:
        results["resume/main.tex"] = (FAIL, "file exists but may not be valid LaTeX (no \\documentclass found)")

# ── 8. Playwright ─────────────────────────────────────────────────────────────
async def check_playwright():
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page    = await browser.new_page()
            resp    = await page.goto("https://www.google.com", timeout=15000)
            title   = await page.title()
            await browser.close()
            results["Playwright"] = (PASS, f"Chromium headless OK | {title!r}")
    except Exception as e:
        results["Playwright"] = (FAIL, str(e)[:150])

# ── 9. Output queues clean ────────────────────────────────────────────────────
def check_queues():
    files = [
        "outputs/tracker_queue.jsonl",
        "outputs/agent2_queue.jsonl",
        "outputs/agent3_queue.jsonl",
    ]
    non_empty = []
    for f in files:
        p = Path(f)
        if p.exists() and p.stat().st_size > 0:
            lines = len(p.read_text().strip().splitlines())
            non_empty.append(f"{Path(f).name}({lines} rows)")
    if non_empty:
        results["Queue files"] = (FAIL, f"Not empty: {', '.join(non_empty)} — run Step 5 to clear")
    else:
        results["Queue files"] = (PASS, "all queues empty")


async def main():
    print("=" * 65)
    print("  JOB AUTOMATION — SYSTEM HEALTH CHECK")
    print("=" * 65)

    # Run checks
    check_anthropic()
    check_pdflatex()
    check_resume()
    check_queues()
    await asyncio.gather(
        check_rapidapi(),
        check_apify(),
        check_gmail(),
        check_hunter(),
        check_playwright(),
    )

    # Print results
    print()
    all_pass = True
    for name, (status, detail) in results.items():
        icon = status.split()[0]
        print(f"  {status}  {name:<22}  {detail}")
        if "FAIL" in status:
            all_pass = False

    print()
    if all_pass:
        print("  All checks passed — ready to run the full pipeline!")
    else:
        print("  Some checks failed or were skipped — see above.")
    print("=" * 65)

if __name__ == "__main__":
    asyncio.run(main())
