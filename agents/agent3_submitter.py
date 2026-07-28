"""
AGENT 3 — SUBMITTER
Reads Agent 2's queue. For each application:
1. Rewrites company-branded URLs to direct ATS embed URLs
2. Loads saved session for companies requiring login (run setup_sessions.py once)
3. Detects the ATS (Greenhouse, Ashby, Lever, generic)
4. Clicks "Apply" button to reveal the form
5. Fills all fields + uploads tailored PDF
6. Submits and logs result to Agent 4

Auth wall handling:
- Companies with saved sessions (sessions/*.json): auto-login ✅
- Companies without sessions: flagged as needs_manual, apply with pre-built PDF
"""

import asyncio, json, logging, os, re
from datetime import datetime
from pathlib import Path
import anthropic
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

from dotenv import load_dotenv
load_dotenv()

from config.profile import PROFILE

log = logging.getLogger("agent3_submitter")
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [SUBMITTER] %(message)s",
    handlers=[logging.FileHandler("logs/agent3.log"), logging.StreamHandler()])

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_KEY")
client        = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

RESUME_PDF_PATH = "outputs/resume_latest.pdf"
SESSIONS_DIR    = Path("sessions")


def _parse_json(text: str, default):
    """
    Robustly extract a JSON array/object from an LLM response, tolerating
    code fences, preamble prose, or trailing text. Returns `default` on failure
    so a bad parse never crashes a submission.
    """
    if not text:
        return default
    s = text.strip()
    # strip code fences properly (not char-by-char like lstrip)
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # fall back: grab the outermost [...] or {...}
    for open_c, close_c in (("[", "]"), ("{", "}")):
        i, j = s.find(open_c), s.rfind(close_c)
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                continue
    return default


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL VERIFICATION CODE READER
# Greenhouse emails an 8-char "security code" to confirm a human is applying.
# We read it from the candidate's own Gmail and enter it to complete submission.
# ─────────────────────────────────────────────────────────────────────────────
import base64, time

def _extract_code(text: str) -> str | None:
    """
    Find the 8-char security code in email text.
    Greenhouse codes are 8 chars, either mixed-case letters (ykvIVaTX) or
    letters+digits (5GDyVj7y). Excludes lowercase dictionary words (security).
    """
    clean = re.sub(r"<[^>]+>", " ", text)
    for tok in re.findall(r"\b[A-Za-z0-9]{8}\b", clean):
        has_upper = bool(re.search(r"[A-Z]", tok))
        has_lower = bool(re.search(r"[a-z]", tok))
        has_digit = bool(re.search(r"\d", tok))
        # real code = mixed case, OR contains a digit (never an all-lowercase word)
        if (has_upper and has_lower) or (has_digit and (has_upper or has_lower)):
            return tok
    return None

def fetch_verification_code(company: str = "", since_epoch: float | None = None,
                            timeout_s: int = 90) -> str | None:
    """
    Poll Gmail for the most recent Greenhouse 'Security code for your application'
    email (optionally matching company) sent after `since_epoch`. Returns the code.
    """
    try:
        from core.gmail_token import get_gmail_token
    except Exception:
        return None
    import httpx

    deadline = time.time() + timeout_s
    query = 'from:greenhouse-mail.io subject:"security code" newer_than:1h'
    while time.time() < deadline:
        try:
            tok = get_gmail_token()
            r = httpx.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                headers={"Authorization": f"Bearer {tok}"},
                params={"q": query, "maxResults": 5}, timeout=15,
            )
            for m in r.json().get("messages", []):
                d = httpx.get(
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}",
                    headers={"Authorization": f"Bearer {tok}"},
                    params={"format": "full"}, timeout=15,
                ).json()
                # internalDate is ms epoch — only accept emails after we submitted
                internal = int(d.get("internalDate", "0")) / 1000.0
                if since_epoch and internal < since_epoch - 2:
                    continue
                hdrs = {h["name"]: h["value"] for h in d.get("payload", {}).get("headers", [])}
                subj = hdrs.get("Subject", "")
                if company and company.lower() not in subj.lower():
                    # subject is "Security code for your application to <Company>"
                    continue

                def body_of(p):
                    if p.get("body", {}).get("data"):
                        return base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", "ignore")
                    for part in p.get("parts", []):
                        t = body_of(part)
                        if t: return t
                    return ""
                code = _extract_code(body_of(d.get("payload", {})))
                if code:
                    log.info(f"📧 Verification code retrieved from Gmail: {code}")
                    return code
        except Exception as e:
            log.warning(f"Code fetch error: {e}")
        time.sleep(6)
    log.warning("No verification code found in time.")
    return None


async def handle_verification_code(page: Page, app: dict, submit_epoch: float) -> bool:
    """
    After submitting, if a 'security code' field appears, read the emailed code
    and enter it, then resubmit. Returns True if a code was entered.
    """
    # Detect the security-code field / prompt
    body = (await page.content()).lower()
    if not any(s in body for s in ["security code", "verification code",
                                   "confirm you're a human", "8-character", "enter the code"]):
        return False

    log.info("🔐 Security-code step detected — fetching code from Gmail…")
    # Run the blocking Gmail poll off the event loop
    code = await asyncio.get_event_loop().run_in_executor(
        None, fetch_verification_code, app.get("company", ""), submit_epoch, 90)
    if not code:
        return False

    # Enter the code — try common selectors and per-char boxes
    entered = False
    field = await page.query_selector(
        "input[name*='code' i], input[id*='code' i], input[placeholder*='code' i], "
        "input[aria-label*='code' i], input[name*='security' i]")
    if field:
        try:
            await field.fill(code, timeout=4000)
            entered = True
        except Exception:
            pass
    if not entered:
        # per-character boxes: fill each input with one char
        boxes = await page.query_selector_all("input[maxlength='1']")
        if len(boxes) >= len(code):
            for b, ch in zip(boxes, code):
                try: await b.fill(ch)
                except Exception: pass
            entered = True

    if entered:
        await page.wait_for_timeout(500)
        btn = await page.query_selector(
            "button:has-text('Submit'), button:has-text('Confirm'), "
            "button:has-text('Verify'), button[type='submit'], input[type='submit']")
        if btn:
            await btn.click()
            await page.wait_for_timeout(3000)
        log.info("✅ Verification code entered and resubmitted.")
        return True
    return False

# ── Domain → session file mapping ────────────────────────────────────────────
# Each entry: if this domain appears in the apply URL, load this session file.
# Sessions are saved by setup_sessions.py (run once per company).
SESSION_MAP = {
    "stripe.com":     "sessions/stripe.json",
    "okta.com":       "sessions/okta.json",
    "airbnb.com":     "sessions/airbnb.json",
    "databricks.com": "sessions/databricks.json",
    "cloudflare.com": "sessions/cloudflare.json",
    "linkedin.com":   "sessions/linkedin.json",
}

def get_session_for(url: str) -> str | None:
    """Return the saved session file for this URL domain, if one exists."""
    url_lower = url.lower()
    for domain, session_file in SESSION_MAP.items():
        if domain in url_lower and Path(session_file).exists():
            return session_file
    return None

def _get_best_resume(app: dict) -> str | None:
    """Return the tailored PDF for this job, falling back to any available PDF."""
    # First: job-specific tailored PDF
    specific = app.get("resume_pdf", "")
    if specific and Path(specific).exists():
        return specific
    # Second: any PDF in the application folder
    app_dir = Path(app.get("resume_tex", "")).parent if app.get("resume_tex") else None
    if app_dir and app_dir.exists():
        pdfs = list(app_dir.glob("*.pdf"))
        if pdfs:
            return str(pdfs[0])
    # Third: latest PDF in all applications
    all_pdfs = sorted(Path("outputs/applications").glob("**/*.pdf"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    return str(all_pdfs[0]) if all_pdfs else None

# ─────────────────────────────────────────────────────────────────────────────
# URL NORMALISER — rewrite company-hosted pages to direct ATS embed URLs
# ─────────────────────────────────────────────────────────────────────────────
# Companies that host Greenhouse behind their own domain (login wall).
# Map: domain_fragment → greenhouse_slug
_GREENHOUSE_DOMAIN_MAP = {
    "stripe.com":         "stripe",
    "airbnb.com":         "airbnb",
    "brex.com":           "brex",
    "intercom.com":       "intercom",
    "hubspot.com":        "hubspot",
    "figma.com":          "figma",
    "coinbase.com":       "coinbase",
    "squarespace.com":    "squarespace",
    "vercel.com":         "vercel",
    "openai.com":         "openai",
    "anthropic.com":      "anthropic",
    "databricks.com":     "databricks",
    "mongodb.com":        "mongodb",
    "cloudflare.com":     "cloudflare",
    "okta.com":           "okta",
    "datadoghq.com":      "datadog",
    "elastic.co":         "elastic",
}

def rewrite_to_direct_ats_url(url: str) -> str:
    """
    Rewrite company-branded job URLs to the STANDALONE Greenhouse embed form.

    Company pages (stripe.com, careers.airbnb.com) load the Greenhouse form
    inside an iframe behind a "Sign in" nav — which breaks automation. The
    embed endpoint serves the full application form directly, no login, no iframe:

      stripe.com/jobs/search?gh_jid=6176758
      → https://job-boards.greenhouse.io/embed/job_app?for=stripe&token=6176758
    """
    from urllib.parse import urlparse, parse_qs

    # Already a Greenhouse board URL → convert to embed form too
    m = re.search(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_app\?for=([\w-]+)&token=(\d+)|([\w-]+)/jobs/(\d+))", url)
    if m:
        if m.group(1):  # already embed form
            return url
        slug, job_id = m.group(3), m.group(4)
        return f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={job_id}"

    for domain, slug in _GREENHOUSE_DOMAIN_MAP.items():
        if domain in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            job_id = (params.get("gh_jid") or [""])[0]
            if not job_id:
                for part in reversed(parsed.path.rstrip("/").split("/")):
                    if part.isdigit():
                        job_id = part
                        break
            if job_id:
                return f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={job_id}"
    return url

# ─────────────────────────────────────────────────────────────────────────────
# ATS DETECTOR
# ─────────────────────────────────────────────────────────────────────────────
def detect_ats(url: str) -> str:
    url_lower = url.lower()
    if "greenhouse.io"    in url_lower: return "greenhouse"
    if "ashbyhq.com"      in url_lower: return "ashby"
    if "lever.co"         in url_lower: return "lever"
    if "jobs.workable.com" in url_lower: return "workable"
    if "workday.com"      in url_lower: return "workday"
    if "taleo.net"        in url_lower: return "taleo"
    if "smartrecruiters"  in url_lower: return "smartrecruiters"
    if "ashbyhq.com"      in url_lower: return "ashby"
    if "jobvite.com"      in url_lower: return "jobvite"
    if "myworkdayjobs"    in url_lower: return "workday"
    # After rewriting, stripe.com → boards.greenhouse.io (caught above)
    # But detect from original URL for company-branded Greenhouse pages
    for domain in _GREENHOUSE_DOMAIN_MAP:
        if domain in url_lower:
            return "greenhouse"
    return "generic"


# Job aggregators that only LIST roles — their "Apply" button redirects out to the
# real employer/ATS, so the listing page itself has no submittable form.
_AGGREGATOR_DOMAINS = (
    "jobicy.com", "remotive.com", "remoteok.com", "weworkremotely.com",
    "himalayas.app", "jooble.org", "adzuna", "careerjet", "reed.co.uk",
    "themuse.com", "glassdoor.", "indeed.com",
)


def is_aggregator_url(url: str) -> bool:
    u = (url or "").lower()
    return any(d in u for d in _AGGREGATOR_DOMAINS)


async def follow_aggregator_apply(page) -> str | None:
    """On an aggregator listing page, find the outbound 'Apply' link and navigate
    to the real employer/ATS. Returns the resolved off-aggregator URL, or None.
    Only follows genuine apply links — never privacy/terms/social/footer links."""
    # Links whose href clearly points at an application/ATS, OR whose visible text
    # is an apply CTA. NEVER the generic 'any external link' (that grabbed Cloudflare
    # privacy-policy by mistake).
    sel = ("a:has-text('Apply for this'), a:has-text('Apply Now'), "
           "a:has-text('Apply now'), a:has-text('Apply'), a.apply_job_link, "
           "a[href*='greenhouse'], a[href*='lever.co'], a[href*='ashbyhq'], "
           "a[href*='workable'], a[href*='workday'], a[href*='/apply'], "
           "a[href*='/careers'], a[href*='/jobs']")
    _BAD = ("privacy", "policy", "terms", "cookie", "legal", "gdpr", "/about",
            "/contact", "facebook.", "twitter.", "linkedin.com/company",
            "instagram.", "youtube.", "mailto:", "login", "sign-in", "signin")
    ATS = ("greenhouse", "lever.co", "ashbyhq", "workable", "workday",
           "smartrecruiters", "/apply", "/careers", "/jobs")
    try:
        for a in (await page.query_selector_all(sel))[:12]:
            href = ((await a.get_attribute("href")) or "").strip()
            hl = href.lower()
            if not hl.startswith("http") or is_aggregator_url(hl):
                continue
            if any(b in hl for b in _BAD):
                continue
            txt = ((await a.inner_text()) or "").strip().lower()
            looks_apply = ("apply" in txt) or any(k in hl for k in ATS)
            if not looks_apply:
                continue
            try:
                await page.goto(href, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(800)
            except Exception:
                return href
            return page.url
    except Exception:
        pass
    return None


def _write_agent4_result(result: dict):
    """Append an Agent 3 outcome exactly once to the Agent 4 handoff log."""
    with open("outputs/agent4_submitted.jsonl", "a") as f:
        f.write(json.dumps(result) + "\n")

# ─────────────────────────────────────────────────────────────────────────────
# CUSTOM QUESTION ANSWERER — handles work-auth, "why us", dropdowns, EEO
# ─────────────────────────────────────────────────────────────────────────────
# Pre-set answers for common questions (no API call needed)
_KNOWN_ANSWERS = {
    "authorized to work":        "Yes",
    "authorised to work":        "Yes",
    "legally allowed to work":   "Yes",
    "legally authorized":        "Yes",
    "legally authorised":        "Yes",
    "allowed to work":           "Yes",
    "permitted to work":         "Yes",
    "eligible to work":          "Yes",
    "right to work":             "Yes",
    "work authorization":        "Yes, I hold Stamp 1G — full work rights in Ireland, no sponsorship needed",
    "require sponsorship":       "No",
    "need sponsorship":          "No",
    "visa sponsorship":          "No - I have Stamp 1G work authorization",
    "require visa":              "No",
    "notice period":             "Immediately available",
    "years of experience":       "3",
    "salary expectation":        "€60,000 - €75,000",
    "expected salary":           "€60,000 - €75,000",
    "how did you hear":          "Company careers page",
    "employee id":               "N/A",
    "eid":                       "N/A",
    "capital one":               "N/A",
    "if you're not authorized":   "N/A",
    "if you are not authorized":  "N/A",
    "sponsorship would you require": "N/A",
    "willing to relocate":       "Yes",
    "relocation":                "Yes",
    "hybrid":                    "Yes",
    "in-office":                 "Yes",
    "in office":                 "Yes",
    "english level":             "Advanced",
    "english proficiency":       "Advanced",
    "currently located":         PROFILE["location"],
    "where are you based":       PROFILE["location"],
    "country are you based":     "Ireland",
    "country where you currently reside": "Ireland",
    "linkedin":                  f"https://{PROFILE['linkedin']}",
    "github":                    f"https://{PROFILE['github']}",
    "website":                   f"https://{PROFILE['github']}",
    "portfolio":                 f"https://{PROFILE['github']}",
    "pronouns":                  "He/Him/His",
    "gender identity":           "Male",
    "gender":                    "Male",
    "veteran":                   "No",
    "disability":                "No",
    "disabled":                  "No",
    "sexual orientation":        "Straight",
    "orientation":               "Straight",
    "race":                      "Asian",
    "ethnicity":                 "Asian",
    "hispanic":                  "No",
    "latino":                    "No",
    "why ":                      None,   # → Claude
    "what interests you":        None,   # → Claude
    "tell us":                   None,   # → Claude
}


_TECHNICAL_SKILL_TERMS = [
    "aws", "gcp", "google cloud", "azure", "cloud",
    "python", "javascript", "typescript", "node", "node.js", "react",
    "next.js", "fastapi", "django", "flask", "postgres", "postgresql",
    "mysql", "mongodb", "redis", "docker", "kubernetes", "terraform",
    "ci/cd", "cicd", "github actions", "gitlab", "linux", "api",
    "rest", "graphql", "microservices", "backend", "full stack",
    "full-stack", "machine learning", "ml", "ai", "rag", "llm",
]


def _is_technical_skill_question(label: str) -> bool:
    ll = label.lower()
    def has_term(term: str) -> bool:
        # Short skills like "AI" must not match ordinary words like "Capital".
        return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", ll) is not None

    if not any(has_term(term) for term in _TECHNICAL_SKILL_TERMS):
        return False
    return any(k in ll for k in [
        "experience", "worked with", "worked on", "familiar", "proficient",
        "knowledge", "skill", "used", "using", "have you", "do you have",
        "are you comfortable", "comfortable with", "required", "requirement",
        "expertise", "stack", "technologies", "tools", "cloud platform",
    ])


def _build_skill_answer(question: str, app: dict | None = None) -> str:
    """Return a concise skill answer tuned to the question/job context."""
    app = app or {}
    context = " ".join([
        question or "",
        app.get("title", ""),
        app.get("description", ""),
        app.get("tailored_summary", ""),
    ]).lower()

    profile_skills = []
    for values in PROFILE.get("skills", {}).values():
        profile_skills.extend(values)

    cloud_aliases = {
        "aws": "AWS",
        "gcp": "Google Cloud",
        "google cloud": "Google Cloud",
        "azure": "Azure",
    }
    selected: list[str] = []

    def add(skill: str):
        if skill and skill not in selected:
            selected.append(skill)

    for needle, skill in cloud_aliases.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", context):
            add(skill)

    for skill in profile_skills:
        pattern = rf"(?<![a-z0-9]){re.escape(skill.lower())}(?![a-z0-9])"
        if re.search(pattern, context):
            add(skill)

    if not selected:
        for skill in [
            "Python", "JavaScript", "TypeScript", "React", "Next.js",
            "Node.js", "FastAPI", "PostgreSQL", "MongoDB", "Redis",
            "Docker", "Kubernetes", "Azure", "Google Cloud", "AWS", "CI/CD",
        ]:
            add(skill)

    return "Yes - strongest relevant skills: " + ", ".join(selected[:14]) + "."


def _known_answer(label: str, app: dict | None = None) -> str | None:
    if _is_technical_skill_question(label):
        return _build_skill_answer(label, app)
    ll = label.lower()
    for key, val in _KNOWN_ANSWERS.items():
        if key in ll and val is not None:
            return val
    return None


async def _safe_check(cb) -> bool:
    """Tick a checkbox WITHOUT any click that could land on an embedded link.
    GDPR consent labels often wrap a 'privacy policy' <a>, so a coordinate click /
    label click can navigate the page away (the Cloudflare redirect bug). We try
    Playwright's targeted check on the input, then fall back to setting the checked
    state via JS + input/change events — never a click that can follow a link."""
    try:
        if await cb.is_checked():
            return True
    except Exception:
        pass
    try:
        await cb.check(timeout=2000)
        if await cb.is_checked():
            return True
    except Exception:
        pass
    try:
        await cb.evaluate("""c => {
            if (!c.checked) {
                c.checked = true;
                c.dispatchEvent(new Event('input',  {bubbles:true}));
                c.dispatchEvent(new Event('change', {bubbles:true}));
            }
        }""")
        return await cb.is_checked()
    except Exception:
        return False


async def answer_custom_questions(page: Page, app: dict):
    """
    Fill every remaining required field that the standard filler missed:
      - text inputs / textareas → known answer or Claude-generated
      - select dropdowns → pick best matching option (Yes for work-auth, etc.)
    """
    # ── 1. Text/textarea required fields still empty ──────────────────────────
    empties = []
    for el in await page.query_selector_all(
        "input[type='text'], input[type='url'], input:not([type]), textarea"
    ):
        try:
            val = await el.input_value()
            if val.strip():
                continue   # already filled
            # Find label text
            label = (await el.get_attribute("aria-label")
                     or await el.get_attribute("placeholder")
                     or await el.get_attribute("name") or "")
            # Try associated <label>
            el_id = await el.get_attribute("id")
            if el_id and not label:
                lbl = await page.query_selector(f"label[for='{el_id}']")
                if lbl:
                    label = (await lbl.text_content() or "").strip()
            if not label:
                continue
            known = _known_answer(label, app)
            if known:
                await el.fill(known, timeout=3000)
            else:
                empties.append((el, label))
        except Exception:
            pass

    # ── 2. Open-ended questions → batch one Claude call ───────────────────────
    if empties and client:
        questions = [lbl for _, lbl in empties]
        try:
            answers = _claude_answer_questions(questions, app)
            for (el, _), ans in zip(empties, answers):
                try: await el.fill(ans, timeout=3000)
                except Exception: pass
        except Exception as e:
            log.warning(f"Claude Q&A failed: {e}")

    # ── 3. Native <select> dropdowns ──────────────────────────────────────────
    for sel in await page.query_selector_all("select"):
        try:
            label = await _rich_label(sel)
            options = await sel.query_selector_all("option")
            opt_texts = [( (await o.text_content() or "").strip(), await o.get_attribute("value") )
                         for o in options]
            chosen = _pick_option(label, [t for t, _ in opt_texts])
            if chosen:
                val = next((v for t, v in opt_texts if t == chosen), None)
                if val: await sel.select_option(value=val)
        except Exception:
            pass

    # ── 4. React-Select widgets (Greenhouse modern custom questions) ──────────
    await _fill_greenhouse_select_containers(page)
    await _force_greenhouse_known_selects(page)
    await _fill_react_selects(page)

    # ── 4b. Radio-button groups (Yes/No work-auth, sponsorship, skills, etc.) ──
    await _answer_radio_groups(page)

    # ── 5. Checkbox groups ───────────────────────────────────────────────────
    # Check Ireland for location groups and required consent/terms boxes. Avoid
    # optional marketing/newsletter opt-ins.
    try:
        for cb in await page.query_selector_all("input[type='checkbox']"):
            if await cb.is_checked():
                continue
            # label text for this checkbox
            lbl = await cb.evaluate("""c => {
                if (c.id) { const l = document.querySelector(`label[for="${c.id}"]`); if (l) return l.textContent; }
                const p = c.closest('label'); if (p) return p.textContent;
                return (c.nextElementSibling && c.nextElementSibling.textContent) || '';
            }""")
            lt = (lbl or "").strip().lower()
            required = bool(await cb.get_attribute("required")) or (
                (await cb.get_attribute("aria-required") or "").lower() == "true"
            )
            opt_out = any(k in lt for k in [
                "newsletter", "marketing", "promotional", "future job alerts",
                "talent community", "receive updates"
            ])
            consentish = any(k in lt for k in [
                "i agree", "i consent", "consent to", "accept the terms",
                "terms", "privacy", "privacy notice", "data survey",
                "collecting, storing", "processing my responses", "acknowledge",
                "i certify", "true and correct", "accurate and complete",
                "to the best of my knowledge", "i confirm"
            ])
            if lt == "ireland" or lt.startswith("ireland"):
                await _safe_check(cb)
            elif lt in {"linkedin", "careers website", "company careers page", "company website"}:
                await _safe_check(cb)
            elif lt in {"asian", "indian", "south asian", "asian (not hispanic or latino)"}:
                await _safe_check(cb)
            elif any(k in lt for k in ["none of the above", "not applicable", "none of these apply"]):
                await _safe_check(cb)
            elif (required or consentish) and not opt_out:
                await _safe_check(cb)
    except Exception:
        pass


async def _rich_label(el) -> str:
    """Best-effort question text for a field: aria-label + name + associated
    <label for> + the nearest label/legend in its container. Lower-cased."""
    try:
        txt = await el.evaluate("""el => {
            const parts = [el.getAttribute('aria-label')||'', el.getAttribute('name')||'', el.id||''];
            if (el.id){ const l=document.querySelector(`label[for="${el.id}"]`); if(l) parts.push(l.textContent); }
            let n = el;
            for (let i = 0; i < 8 && n; i++, n = n.parentElement) {
                const q = n.querySelector('label,legend,h3,h4,[class*=label],[class*=question]');
                if (q && q.textContent && q.textContent.trim()) parts.push(q.textContent);
                if (n.previousElementSibling && n.previousElementSibling.textContent) {
                    const prev = n.previousElementSibling.textContent.trim();
                    if (prev && prev.length < 400) parts.push(prev);
                }
            }
            return parts.join(' ');
        }""")
        return (txt or "").strip().lower()
    except Exception:
        try:
            return ((await el.get_attribute("aria-label")) or (await el.get_attribute("name")) or "").lower()
        except Exception:
            return ""


async def _radio_option_label(el) -> str:
    """Text identifying a single radio option (its own label / value)."""
    try:
        txt = await el.evaluate("""el => {
            if (el.id){ const l=document.querySelector(`label[for="${el.id}"]`); if(l) return l.textContent; }
            const p = el.closest('label'); if(p) return p.textContent;
            const s = el.nextElementSibling; if(s) return s.textContent;
            return el.value||'';
        }""")
        return (txt or "").strip().lower()
    except Exception:
        try:
            return ((await el.get_attribute("value")) or "").lower()

        except Exception:
            return ""


async def _answer_radio_groups(page: Page):
    """Answer unanswered radio-button questions — the common recurring blocker.

    Work-authorisation / right-to-work / eligibility → Yes.
    Visa-sponsorship required → No (truthful: Stamp 1G needs none).
    Technical-skill 'do you have experience' → Yes.
    Genuinely-unknown questions are left alone (flag review, never guess wrong)."""
    radios = await page.query_selector_all("input[type='radio']")
    groups: dict[str, list] = {}
    for r in radios:
        try:
            name = await r.get_attribute("name") or f"__{id(r)}"
        except Exception:
            name = f"__{id(r)}"
        groups.setdefault(name, []).append(r)

    for name, group in groups.items():
        try:
            if any([await r.is_checked() for r in group]):
                continue  # already answered
        except Exception:
            pass

        q = await _rich_label(group[0])
        want = None
        if any(k in q for k in ["currently live in the united states", "live in the united states",
                                "currently reside in the united states", "reside in the united states"]):
            want = "no"
        elif ("sponsor" in q) or ("visa" in q and any(k in q for k in ["require", "need"])):
            want = "no"
        elif any(k in q for k in ["currently work", "previously worked", "previously employed",
                                  "have you been employed", "been employed", "former employee",
                                  "worked at", "employed by", "blood relative", "relatives",
                                  "relative", "family member", "under consideration"]):
            want = "no"
        elif any(k in q for k in ["government official", "public official", "elected official",
                                  "politically exposed", "public office", "government employee",
                                  "hold public office", "senior political"]):
            want = "no"
        elif "veteran" in q or "disab" in q:
            want = "no"
        elif any(k in q for k in ["18 years", "at least 18", "older", "of legal age",
                                  "legally old enough", "minimum age"]):
            want = "yes"
        elif any(k in q for k in ["author", "right to work", "eligible", "legally", "permit",
                                   "allowed to work", "permitted to work", "lawfully", "work rights"]):
            want = "yes"
        elif any(k in q for k in ["relocat", "in-person", "in person", "onsite", "on-site",
                                  "hybrid", "office", "travel", "commutable"]):
            want = "yes"
        elif _is_technical_skill_question(q):
            want = "yes"
        if want is None:
            # English proficiency radios are usually level options, not Yes/No.
            if any(k in q for k in ["english level", "english proficiency", "proficiency in english"]):
                for r in group:
                    ol = await _radio_option_label(r)
                    if any(k in ol for k in ["advanced", "fluent", "professional", "c1", "c2"]):
                        try:
                            await r.check(timeout=2500)
                            log.info(f"Radio answered (advanced) → {q[:50]}")
                        except Exception:
                            try: await r.click(timeout=2500)
                            except Exception: pass
                        break
            continue  # don't guess on unknown required questions

        target = None
        for r in group:
            ol = await _radio_option_label(r)
            if want == "yes" and (ol in ("yes", "true") or "yes" in ol or "i am" in ol or "i have" in ol):
                target = r; break
            if want == "no" and (ol in ("no", "false") or ol.startswith("no") or "not " in ol or "don't" in ol):
                target = r; break
        if target:
            try:
                await target.check(timeout=2500)
                log.info(f"Radio answered ({want}) → {q[:50]}")
            except Exception:
                try: await target.click(timeout=2500)
                except Exception: pass


def _pick_option(label: str, options: list[str]) -> str | None:
    """Choose the best option text for a labelled dropdown."""
    ll = label.lower()
    opts_l = [(o, o.lower()) for o in options if o.strip() and "select" not in o.lower()[:8]]
    if not opts_l:
        return None

    def find(*keywords):
        normalized = [k.lower().strip() for k in keywords]
        # Prefer exact/word-prefix matches first so "male" does not match
        # "female", and "he" does not match "she/her".
        for orig, low in opts_l:
            clean = re.sub(r"\s+", " ", low).strip()
            for k in normalized:
                if (clean == k or clean.startswith(k + " ") or clean.startswith(k + "/")
                        or clean.startswith(k + "-") or clean.startswith(k + "(")):
                    return orig
        for orig, low in opts_l:
            for k in normalized:
                if len(k) <= 3:
                    if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", low):
                        return orig
                elif k in low:
                    return orig
        return None

    years_match = re.search(r"(\d+)\s*\+?\s*years?", ll)
    if years_match and "experience" in ll:
        required_years = int(years_match.group(1))
        return find("yes") if required_years <= 3 else find("no")

    if _is_technical_skill_question(label):
        return find("yes", "true", "experienced", "proficient", "comfortable")

    # Location / eligibility questions where the truthful answer is explicit.
    if any(k in ll for k in ["currently live in the united states", "live in the united states",
                             "currently reside in the united states", "reside in the united states"]):
        return find("no")
    if any(k in ll for k in ["english level", "english proficiency", "proficiency in english"]):
        return (find("advanced")
                or find("fluent", "professional", "c1", "c2")
                or find("upper", "business"))
    if any(k in ll for k in ["commutable distance", "hybrid in-office", "hybrid in office",
                             "in-office expectation", "in office expectation",
                             "office 1 to 3 days", "office 1-3 days", "participate in this team rotation"]):
        return find("yes", "agree", "understand", "willing", "able")

    # Work-authorisation / sponsorship questions are frequently phrased with the
    # word "country" ("legally allowed to work in this country") — resolve them
    # BEFORE the residence branch so they don't get mistaken for a location field.
    _auth_q    = any(k in ll for k in ["author", "right to work", "eligible to work",
                                       "eligible", "legally", "permit", "allowed to work",
                                       "permitted to work", "lawfully", "work rights"])
    _sponsor_q = ("sponsor" in ll) or ("visa" in ll and any(k in ll for k in ["require", "need"]))

    if any(k in ll for k in ["cuba", "iran", "north korea", "syria", "crimea", "sanction", "export control"]):
        return find("no", "none", "not applicable")

    # Country of residence / location → Ireland (candidate is Dublin-based).
    # Skip when it's really an authorisation/sponsorship question.
    if not (_auth_q or _sponsor_q):
        location_is_relocation_question = any(k in ll for k in ["relocat", "open to", "willing to"])
        if not location_is_relocation_question:
            if ("country" in ll or "based in" in ll or "currently based" in ll
                    or ("where" in ll and ("reside" in ll or "locat" in ll or "live" in ll or "based" in ll))):
                choice = find("ireland") or find("dublin")
                if choice:
                    return choice
            if any(k in ll for k in ["reside", "currently located", "based in", "currently based",
                                      "where do you live", "country are you based"]):
                choice = find("ireland") or find("dublin")
                if choice:
                    return choice
    if any(k in ll for k in ["united states", "u.s.", "usa", "h-1b", "h1b"]):
        if any(k in ll for k in ["sponsor", "visa", "h-1b", "h1b"]):
            return find("yes")
        if any(k in ll for k in ["author", "eligible", "legally", "right to work"]):
            return find("no")
    if _sponsor_q:
        return find("no")
    if any(k in ll for k in ["currently work", "previously worked", "previously employed",
                              "have you been employed", "been employed", "former employee",
                              "worked at", "employed by", "blood relative", "relatives",
                              "relative", "family member", "under consideration"]):
        return find("no")
    if any(k in ll for k in ["government official", "public official", "elected official",
                              "politically exposed", "public office", "government employee",
                              "hold public office", "senior political"]):
        return find("no")
    if any(k in ll for k in ["18 years", "at least 18", "of legal age", "legally old enough",
                              "minimum age", "older"]):
        return find("yes", "i am", "true")
    if any(k in ll for k in ["i certify", "certify that", "true and correct", "accurate and complete",
                              "information provided", "to the best of my knowledge", "i confirm"]):
        return find("yes", "i certify", "i confirm", "i agree", "true", "agree")
    if _auth_q:
        return find("yes", "i am", "i have", "authorized", "authorised", "true")
    if any(k in ll for k in ["relocat", "in-person", "in person", "onsite", "on-site", "hybrid", "office", "travel"]):
        return find("yes", "open", "willing")
    if "hear" in ll and ("job" in ll or "role" in ll or "about" in ll):
        return find("company", "career", "linkedin", "other")
    # EEO / demographic answers requested by candidate.
    if "pronoun" in ll:
        return find("he/him/his", "he/him", "he / him", "he")
    if "gender" in ll:
        return find("male", "man")
    if "sexual" in ll or "orientation" in ll:
        return (find("straight", "heterosexual", "hetero")
                or "Straight")
    if any(k in ll for k in ["race", "ethnic"]):
        for orig, low in opts_l:
            clean = re.sub(r"\s+", " ", low).strip()
            if (clean == "asian" or clean.startswith("asian ")
                    or "south asian" in clean or "asian indian" in clean):
                return orig
        return "Asian"
    if "hispanic" in ll or "latino" in ll:
        return find("no", "not hispanic", "not latino")
    if "veteran" in ll:
        return (find("no", "not a veteran", "i am not a veteran",
                     "i am not", "not protected veteran")
                or find("prefer not", "decline", "not to answer"))
    if "disab" in ll:
        return (find("no", "i do not have", "i don't have", "not disabled",
                     "i am not disabled", "no disability")
                or find("prefer not", "decline", "not to answer"))
    if "policy" in ll or "acknowledge" in ll or "agree" in ll or "confirm" in ll:
        return find("yes", "agree", "acknowledge", "confirm", "understand") or (opts_l[0][0] if opts_l else None)
    # Never blindly choose the first Yes/No option for an unknown question.
    # That caused wrong answers such as "previously employed" = Yes.
    if {low for _, low in opts_l}.issubset({"yes", "no", "true", "false"}):
        return None
    # Default: first real option only for non-binary dropdowns where a required
    # neutral/category choice is safer than leaving the form blocked.
    return opts_l[0][0] if opts_l else None


def _answer_terms_for_select(label: str, answer: str) -> list[str]:
    """Allowed exact/alias terms for answers that must be selected correctly."""
    ll = (label or "").lower()
    ans = (answer or "").strip()
    if "sexual" in ll or "orientation" in ll:
        return ["Straight", "Heterosexual"]
    if any(k in ll for k in ["race", "ethnic"]):
        return ["Asian", "South Asian", "Asian Indian"]
    return [ans] if ans else []


def _option_matches_answer(label: str, answer: str, option_text: str) -> bool:
    low = re.sub(r"\s+", " ", (option_text or "").strip().lower())
    if not low:
        return False
    for term in _answer_terms_for_select(label, answer):
        t = term.lower().strip()
        if low == t or low.startswith(t + " ") or t in low:
            return True
    return False


async def _fill_react_selects(page: Page):
    """
    Handle react-select widgets (.select-shell). Re-queries fresh each pass so
    DOM re-renders never leave us holding stale element handles (which caused
    wrong answers, e.g. visa sponsorship). Processes the FIRST still-empty,
    non-phone dropdown each pass until none remain.
    """
    SHELL_SEL = ".select-shell"   # Greenhouse: exactly one wrapper per dropdown
    max_passes = 24   # safety cap (≥ number of dropdowns)

    for _ in range(max_passes):
        # Re-query fresh every pass — handles stay valid for this single iteration
        shells = await page.query_selector_all(SHELL_SEL)
        target = None
        for shell in shells:
            try:
                # skip phone country picker
                is_phone = await shell.evaluate("""s => {
                    if (/\\+\\d{1,3}/.test(s.textContent)) {
                        const blk = s.closest('[class*=phone], [class*=iti], .field') || s.parentElement;
                        if (blk && blk.querySelector('input[type=tel]')) return true;
                        if (s.className.includes('iti')) return true;
                    }
                    return false;
                }""")
                if is_phone:
                    continue
                # skip ones already processed in a prior pass
                if await shell.get_attribute("data-autofill-done"):
                    continue
                # skip already filled
                val_el = await shell.query_selector("[class*='singleValue'], [class*='single-value']")
                if val_el and (await val_el.text_content() or "").strip():
                    continue
                target = shell
                break
            except Exception:
                continue

        if target is None:
            break   # all dropdowns filled (or only phone left)

        shell = target

        async def _mark_done():
            # Tag so future passes skip this dropdown even if it stayed empty
            try:
                await shell.evaluate("s => s.setAttribute('data-autofill-done','1')")
            except Exception:
                pass

        try:
            # Label — walk up to the field block and prefer nearby question text.
            label = await shell.evaluate("""s => {
                const parts = [];
                const add = t => {
                    t = (t || '').replace(/\\s+/g, ' ').trim();
                    if (t && t.length < 500 && !parts.includes(t)) parts.push(t);
                };
                let n = s;
                for (let i=0;i<8 && n;i++, n=n.parentElement){
                    const own = n.querySelector(':scope > label,:scope > legend,:scope > h3,:scope > h4,:scope > [class*=label],:scope > [class*=question]');
                    if (own) add(own.textContent);
                    if (n.previousElementSibling) add(n.previousElementSibling.textContent);
                    const any = n.querySelector('label,legend,h3,h4,[class*=label],[class*=question]');
                    if (any) add(any.textContent);
                }
                return parts.join(' | ');
            }""")

            # Open and read the real visible options
            await shell.click(timeout=3000)
            await page.wait_for_timeout(450)
            visible = []
            for o in await page.query_selector_all(
                    "div[class*='option'], [role='option'], li[class*='option']"):
                try:
                    if await o.is_visible():
                        t = (await o.text_content() or "").strip()
                        if t and len(t) < 60:
                            visible.append(t)
                except Exception:
                    pass
            if not visible:
                await page.keyboard.press("Escape")
                await _mark_done()
                continue

            # Searchable country/residence picker: Ireland may not be in the
            # initially-visible options, so force it (the keyboard-type filters to it).
            ll = label.lower()
            is_auth_or_sponsor = any(k in ll for k in [
                "author", "right to work", "eligible", "legally", "permit",
                "allowed to work", "permitted to work", "lawfully", "work rights",
                "sponsor", "visa"
            ])
            is_sanctions_question = any(k in ll for k in [
                "cuba", "iran", "north korea", "syria", "crimea", "sanction", "export control"
            ])
            is_country = ("country" in ll or "reside" in ll or "located" in ll
                          or "where do you" in ll or "based" in ll) \
                         and not (is_auth_or_sponsor or is_sanctions_question)
            if is_country:
                want = "Ireland"
            else:
                picked = _pick_option(label, visible)
                visible_l = {v.strip().lower() for v in visible if v.strip()}
                if not picked and visible_l and visible_l.issubset({"yes", "no", "true", "false"}):
                    log.info(f"Select skipped unsafe binary guess → {label[:80] or 'unlabelled'}")
                    await page.keyboard.press("Escape")
                    await _mark_done()
                    continue
                want = picked or visible[0]

            async def _click_visible_option(answer: str) -> bool:
                target = answer.lower().strip()
                for o in await page.query_selector_all(
                        "div[class*='option'], [role='option'], li[class*='option']"):
                    try:
                        if not await o.is_visible():
                            continue
                        text = ((await o.text_content()) or "").strip()
                        low = text.lower()
                        if (low == target or target in low or low in target
                                or _option_matches_answer(label, answer, text)):
                            await o.click(timeout=2500)
                            await page.wait_for_timeout(300)
                            return True
                    except Exception:
                        pass
                return False

            async def _set_and_verify(answer: str) -> bool:
                try:
                    if is_country:
                        await page.keyboard.type(answer, delay=40)
                        await page.wait_for_timeout(400)
                        clicked = await _click_visible_option(answer)
                        if not clicked:
                            await page.keyboard.press("Enter")
                    else:
                        clicked = await _click_visible_option(answer)
                        if not clicked:
                            for term in _answer_terms_for_select(label, answer):
                                await page.keyboard.type(term, delay=40)
                                await page.wait_for_timeout(400)
                                clicked = await _click_visible_option(answer)
                                if clicked:
                                    break
                                await page.keyboard.press("Meta+A")
                                await page.keyboard.press("Backspace")
                        if not clicked:
                            return False
                    await page.wait_for_timeout(300)
                    val = await shell.evaluate(
                        "s=>{const v=s.querySelector('[class*=singleValue],[class*=single-value]');return v?v.textContent.trim():'';}")
                    if not val:
                        return False
                    if answer.lower() == "no":
                        return val.strip().lower().startswith("no")
                    if answer.lower() == "yes":
                        return val.strip().lower().startswith("yes")
                    return answer.lower() in val.lower() or _option_matches_answer(label, answer, val)
                except Exception:
                    return False

            ok = await _set_and_verify(want)
            if not ok:
                try:
                    await shell.click(timeout=2000); await page.wait_for_timeout(400)
                    ok = await _set_and_verify(want)
                except Exception:
                    pass
            if not ok:
                try: await page.keyboard.press("Escape")
                except Exception: pass
            await _mark_done()   # processed (filled or safely skipped) — don't revisit
        except Exception:
            try: await page.keyboard.press("Escape")
            except Exception: pass
            await _mark_done()


async def _fill_greenhouse_select_containers(page: Page):
    """Fill Greenhouse `.select` containers using their own label.

    This is more precise than walking the whole form from `.select-shell` and
    fixes required compliance questions such as Airbnb work authorization and
    sponsorship.
    """
    for _ in range(30):
        selects = await page.query_selector_all(".select")
        target = None
        label = ""
        for sel in selects:
            try:
                if await sel.get_attribute("data-autofill-done"):
                    continue
                shell = await sel.query_selector(".select-shell")
                if not shell or not await shell.is_visible():
                    await sel.evaluate("el => el.setAttribute('data-autofill-done','1')")
                    continue
                label = await sel.evaluate("""el => {
                    const l = el.querySelector('label.select__label,label,legend');
                    return (l && l.textContent || '').replace(/\\s+/g, ' ').trim();
                }""") or ""
                low_label = label.lower()
                if any(k in low_label for k in ["phone", "country code", "dial code"]):
                    await sel.evaluate("el => el.setAttribute('data-autofill-done','1')")
                    continue
                value = await sel.evaluate("""el => {
                    const v = el.querySelector("[class*='singleValue'],[class*='single-value']");
                    if (v && v.textContent && v.textContent.trim()) return v.textContent.trim();
                    const ph = el.querySelector("[class*='placeholder']");
                    return ph && ph.textContent ? ph.textContent.trim() : '';
                }""") or ""
                if value and "select" not in value.lower():
                    await sel.evaluate("el => el.setAttribute('data-autofill-done','1')")
                    continue
                target = sel
                break
            except Exception:
                continue
        if target is None:
            break

        try:
            shell = (await target.query_selector(".select__control")
                     or await target.query_selector(".select__input")
                     or await target.query_selector(".select-shell"))
            await shell.scroll_into_view_if_needed(timeout=2000)
            await shell.click(timeout=3000)
            await page.wait_for_timeout(400)
            visible = []
            for opt in await page.query_selector_all("[role='option'], div[class*='option'], li[class*='option']"):
                try:
                    if not await opt.is_visible():
                        continue
                    text = re.sub(r"\s+", " ", (await opt.inner_text() or "").strip())
                    if text and len(text) <= 100:
                        visible.append(text)
                except Exception:
                    pass
            visible = list(dict.fromkeys(visible))
            low_label = label.lower()
            is_auth_or_sponsor = any(k in low_label for k in [
                "author", "right to work", "eligible", "legally", "permit",
                "allowed to work", "permitted to work", "lawfully", "work rights",
                "sponsor", "visa"
            ])
            is_sanctions_question = any(k in low_label for k in [
                "cuba", "iran", "north korea", "syria", "crimea", "sanction", "export control"
            ])
            is_country = ("country" in low_label or "reside" in low_label
                          or "where are you based" in low_label
                          or "country are you based" in low_label) and not (is_auth_or_sponsor or is_sanctions_question)
            want = "Ireland" if is_country else _pick_option(label, visible)
            if not want:
                visible_l = {v.lower() for v in visible}
                if visible_l and visible_l.issubset({"yes", "no", "true", "false"}):
                    log.info(f"Greenhouse select skipped unsafe binary guess → {label[:80] or 'unlabelled'}")
                    await page.keyboard.press("Escape")
                    await target.evaluate("el => el.setAttribute('data-autofill-done','1')")
                    continue
                want = visible[0] if visible else None
            if not want:
                await page.keyboard.press("Escape")
                await target.evaluate("el => el.setAttribute('data-autofill-done','1')")
                continue

            picked = False
            want_l = want.lower().strip()
            if is_country:
                try:
                    inp = await target.query_selector(".select__input input, input.select__input, input[aria-autocomplete='list']")
                    if inp:
                        await inp.fill("", timeout=1500)
                        await inp.type("Ireland", delay=35)
                        await page.wait_for_timeout(550)
                except Exception:
                    pass
            for opt in await page.query_selector_all("[role='option'], div[class*='option'], li[class*='option']"):
                try:
                    if not await opt.is_visible():
                        continue
                    text = re.sub(r"\s+", " ", (await opt.inner_text() or "").strip())
                    low = text.lower()
                    if low == want_l or want_l in low or low in want_l:
                        await opt.click(timeout=2500)
                        await page.wait_for_timeout(250)
                        picked = True
                        log.info(f"Greenhouse select answered ({want}) → {label[:70]}")
                        break
                except Exception:
                    pass
            if not picked:
                try:
                    inp = await target.query_selector(".select__input input, input.select__input, input[aria-autocomplete='list']")
                    if inp:
                        search_terms = _answer_terms_for_select(label, want)
                        if want_l.startswith("no"):
                            search_terms = ["No"]
                        elif want_l.startswith("yes"):
                            search_terms = ["Yes"]
                        for term in search_terms:
                            await inp.fill("", timeout=1500)
                            await inp.type(term, delay=35)
                            await page.wait_for_timeout(550)
                            for opt in await page.query_selector_all("[role='option'], div[class*='option'], li[class*='option']"):
                                try:
                                    if not await opt.is_visible():
                                        continue
                                    text = re.sub(r"\s+", " ", (await opt.inner_text() or "").strip())
                                    low = text.lower()
                                    if (low == want_l or want_l in low or low in want_l
                                            or _option_matches_answer(label, want, text)):
                                        await opt.click(timeout=2500)
                                        await page.wait_for_timeout(300)
                                        value = await target.evaluate("""el => {
                                            const v = el.querySelector("[class*='singleValue'],[class*='single-value']");
                                            return v && v.textContent ? v.textContent.trim() : '';
                                        }""")
                                        if (want_l in value.lower()
                                                or _option_matches_answer(label, want, value)):
                                            picked = True
                                            log.info(f"Greenhouse select typed+answered ({value or want}) → {label[:70]}")
                                        break
                                except Exception:
                                    pass
                            if picked:
                                break
                except Exception:
                    pass
            if not picked:
                try: await page.keyboard.press("Escape")
                except Exception: pass
            await target.evaluate("el => el.setAttribute('data-autofill-done','1')")
        except Exception:
            try: await page.keyboard.press("Escape")
            except Exception: pass
            try: await target.evaluate("el => el.setAttribute('data-autofill-done','1')")
            except Exception: pass


async def _force_greenhouse_known_selects(page: Page):
    """Directly answer stubborn Greenhouse compliance selects by label text."""
    forced = [
        (["company sponsorship", "work authorization"], "no"),
        (["immigration sponsorship", "work authorization"], "no"),
        (["legally authorized", "country where the job is located"], "yes"),
    ]

    for keywords, answer_prefix in forced:
        try:
            opened = await page.evaluate("""({keywords}) => {
                const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const sel = [...document.querySelectorAll('.select')].find(el => {
                    const txt = norm(el.textContent);
                    if (!keywords.every(k => txt.includes(k))) return false;
                    const val = el.querySelector("[class*='singleValue'],[class*='single-value']");
                    return !(val && norm(val.textContent) && !norm(val.textContent).includes('select'));
                });
                if (!sel) return false;
                sel.scrollIntoView({block:'center', inline:'nearest'});
                const click = sel.querySelector('.select__control') || sel.querySelector('.select-shell') || sel;
                for (const ev of ['mousedown', 'mouseup', 'click']) {
                    click.dispatchEvent(new MouseEvent(ev, {bubbles:true, cancelable:true, view:window}));
                }
                return true;
            }""", {"keywords": keywords})
            if not opened:
                continue
            await page.wait_for_timeout(500)
            picked = await page.evaluate("""({answerPrefix}) => {
                const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const opts = [...document.querySelectorAll("[role='option'], div[class*='option'], li[class*='option']")];
                const opt = opts.find(o => {
                    const r = o.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && norm(o.textContent).startsWith(answerPrefix);
                });
                if (!opt) return '';
                const text = (opt.textContent || '').replace(/\\s+/g, ' ').trim();
                for (const ev of ['mousedown', 'mouseup', 'click']) {
                    opt.dispatchEvent(new MouseEvent(ev, {bubbles:true, cancelable:true, view:window}));
                }
                return text;
            }""", {"answerPrefix": answer_prefix})
            if picked:
                log.info(f"Greenhouse forced select answered ({picked[:70]}) → {' / '.join(keywords)}")
            else:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
        except Exception:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass


async def _fill_aria_dropdowns(page: Page):
    """Handle custom ARIA/listbox dropdowns used by some Greenhouse embeds.

    Airbnb-style required questions render as clickable combobox/listbox blocks,
    not native selects and not `.select-shell`, so the React-select pass never
    sees them. This pass opens only empty visible dropdowns and applies the same
    deterministic answer rules.
    """
    COMBO_SEL = (
        "[role='combobox'], [aria-haspopup='listbox'], "
        "button[aria-haspopup='listbox'], div[aria-haspopup='listbox']"
    )

    async def _label_for(combo) -> str:
        try:
            return await combo.evaluate("""el => {
                const parts = [];
                const add = t => {
                    t = (t || '').replace(/\\s+/g, ' ').trim();
                    if (t && t.length < 500 && !parts.includes(t)) parts.push(t);
                };
                if (el.getAttribute('aria-label')) add(el.getAttribute('aria-label'));
                if (el.getAttribute('aria-labelledby')) {
                    for (const id of el.getAttribute('aria-labelledby').split(/\\s+/)) {
                        const n = document.getElementById(id);
                        if (n) add(n.textContent);
                    }
                }
                let n = el;
                for (let i = 0; i < 8 && n; i++, n = n.parentElement) {
                    const own = n.querySelector(':scope > label,:scope > legend,:scope > h3,:scope > h4,:scope > [class*=label],:scope > [class*=question]');
                    if (own) add(own.textContent);
                    if (n.previousElementSibling) add(n.previousElementSibling.textContent);
                    const any = n.querySelector('label,legend,h3,h4,[class*=label],[class*=question]');
                    if (any) add(any.textContent);
                }
                return parts.join(' | ');
            }""") or ""
        except Exception:
            return ""

    def _looks_empty(text: str) -> bool:
        low = re.sub(r"\s+", " ", (text or "").strip().lower())
        if not low:
            return True
        return "select" in low or low in {"choose", "please choose", "dropdown"}

    for _ in range(20):
        combos = await page.query_selector_all(COMBO_SEL)
        target = None
        label = ""
        for combo in combos:
            try:
                if await combo.get_attribute("data-autofill-done"):
                    continue
                if not await combo.is_visible():
                    continue
                # Greenhouse React selects are handled precisely by
                # _fill_greenhouse_select_containers. Do not reopen them here:
                # the page-level option menu can contain unrelated choices.
                in_greenhouse_select = await combo.evaluate(
                    "el => Boolean(el.closest('.select,.select__container,.select-shell'))"
                )
                if in_greenhouse_select:
                    await combo.evaluate("el => el.setAttribute('data-autofill-done','1')")
                    continue
                txt = (await combo.inner_text() or "").strip()
                # Skip already-filled controls such as "No" from earlier passes.
                if not _looks_empty(txt):
                    await combo.evaluate("el => el.setAttribute('data-autofill-done','1')")
                    continue
                label = await _label_for(combo)
                # Skip phone country or app chrome dropdowns.
                low_label = label.lower()
                if any(k in low_label for k in ["phone", "country code", "dial code"]):
                    await combo.evaluate("el => el.setAttribute('data-autofill-done','1')")
                    continue
                target = combo
                break
            except Exception:
                continue
        if target is None:
            break

        try:
            await target.click(timeout=3000)
            await page.wait_for_timeout(450)
            visible = []
            for opt in await page.query_selector_all("[role='option'], [role='menuitem'], li, div[class*='option']"):
                try:
                    if not await opt.is_visible():
                        continue
                    t = re.sub(r"\s+", " ", (await opt.inner_text() or "").strip())
                    if t and len(t) <= 80 and t.lower() not in {"select", "select..."}:
                        visible.append(t)
                except Exception:
                    pass
            # De-duplicate without changing option order.
            visible = list(dict.fromkeys(visible))
            want = _pick_option(label, visible)
            if not want:
                visible_l = {v.lower() for v in visible}
                if visible_l and visible_l.issubset({"yes", "no", "true", "false"}):
                    log.info(f"ARIA select skipped unsafe binary guess → {label[:80] or 'unlabelled'}")
                    await page.keyboard.press("Escape")
                    await target.evaluate("el => el.setAttribute('data-autofill-done','1')")
                    continue
                want = visible[0] if visible else None
            if not want:
                await page.keyboard.press("Escape")
                await target.evaluate("el => el.setAttribute('data-autofill-done','1')")
                continue

            picked = False
            target_low = want.lower().strip()
            for opt in await page.query_selector_all("[role='option'], [role='menuitem'], li, div[class*='option']"):
                try:
                    if not await opt.is_visible():
                        continue
                    text = re.sub(r"\s+", " ", (await opt.inner_text() or "").strip())
                    low = text.lower()
                    if (low == target_low or target_low in low or low in target_low
                            or _option_matches_answer(label, want, text)):
                        await opt.click(timeout=2500)
                        picked = True
                        await page.wait_for_timeout(250)
                        log.info(f"ARIA select answered ({want}) → {label[:70]}")
                        break
                except Exception:
                    pass
            if not picked:
                for term in _answer_terms_for_select(label, want):
                    try:
                        await page.keyboard.type(term, delay=35)
                        await page.wait_for_timeout(500)
                        for opt in await page.query_selector_all("[role='option'], [role='menuitem'], li, div[class*='option']"):
                            try:
                                if not await opt.is_visible():
                                    continue
                                text = re.sub(r"\s+", " ", (await opt.inner_text() or "").strip())
                                if _option_matches_answer(label, want, text):
                                    await opt.click(timeout=2500)
                                    picked = True
                                    await page.wait_for_timeout(250)
                                    log.info(f"ARIA select typed+answered ({text}) → {label[:70]}")
                                    break
                            except Exception:
                                pass
                        if picked:
                            break
                        await page.keyboard.press("Escape")
                        await target.click(timeout=2000)
                        await page.wait_for_timeout(250)
                    except Exception:
                        pass
            if not picked:
                try: await page.keyboard.press("Escape")
                except Exception: pass
            await target.evaluate("el => el.setAttribute('data-autofill-done','1')")
        except Exception:
            try: await page.keyboard.press("Escape")
            except Exception: pass
            try: await target.evaluate("el => el.setAttribute('data-autofill-done','1')")
            except Exception: pass


# Circuit breaker — once Claude is unavailable (no credits / API down), stop
# hammering it for the rest of the run and use safe fallback answers instead.
_claude_disabled = False

def _is_quota_error(e: Exception) -> bool:
    m = str(e).lower()
    return any(k in m for k in ("credit", "billing", "balance", "quota", "rate limit", "429"))

def _fallback_answer(question: str) -> str:
    """A safe, honest answer when Claude is unavailable."""
    q = question.lower()
    if _is_technical_skill_question(question):
        return _build_skill_answer(question)
    if "why" in q or "interest" in q or "excited" in q or "motivat" in q:
        return (f"I'm drawn to this role because it aligns with my full-stack and "
                f"backend experience building production systems, and I'm eager to "
                f"contribute impactful, well-engineered software. I'm based in Ireland "
                f"on a Stamp 1G visa with full work rights.")
    if "experience" in q or "tell us" in q or "describe" in q or "background" in q:
        return ("Founding Software Engineer at a fintech startup and 2 years at Bajaj "
                "Finserv, with an MSc in Computer Science (Distinction) from Trinity "
                "College Dublin. Strong in Python, JavaScript/TypeScript, React, Node, "
                "PostgreSQL and Docker.")
    if "salary" in q or "compensation" in q or "expectation" in q:
        return "€60,000–€75,000, flexible based on the overall package."
    if "notice" in q or "available" in q or "start" in q:
        return "Immediately available."
    return ""   # leave blank → flags needs_review rather than a wrong answer

def _claude_answer_questions(questions: list[str], app: dict) -> list[str]:
    """
    One Claude call to answer open-ended application questions.
    Never raises — on any API/credit failure it returns safe fallback answers
    and disables Claude for the rest of the run (circuit breaker).
    """
    global _claude_disabled
    if _claude_disabled or client is None:
        return [_fallback_answer(q) for q in questions]

    qlist = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    prompt = f"""Answer these job application questions AS the candidate. Be concise, specific, professional.

CANDIDATE: {PROFILE['name']} — {PROFILE['summary']}
APPLYING FOR: {app.get('title','')} at {app.get('company','')}
KEY FACTS: Stamp 1G (works in Ireland, no sponsorship), MSc CS Distinction Trinity College Dublin,
  Founding Engineer at fintech startup, 2yr at Bajaj Finserv, skills: Python/JS/React/Node/PostgreSQL/Docker,
  cloud/devops: AWS/GCP/Azure required-skill gates should be answered yes when asked.

QUESTIONS:
{qlist}

Return a JSON array of {len(questions)} short answer strings, in order. Each answer 1-3 sentences max.
For "why this company" type questions, be genuine and specific. No markdown, array only."""
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        answers = _parse_json(msg.content[0].text, [])
    except Exception as e:
        if _is_quota_error(e):
            _claude_disabled = True
            log.warning("Claude unavailable (credits/quota) — using fallback answers for the rest of this run.")
        else:
            log.warning(f"Claude Q&A error: {str(e)[:80]} — using fallbacks.")
        return [_fallback_answer(q) for q in questions]

    if not isinstance(answers, list):
        answers = []
    # Pad with fallbacks if Claude returned fewer
    while len(answers) < len(questions):
        answers.append(_fallback_answer(questions[len(answers)]))
    return answers


async def fill_phone_with_country(page: Page):
    """
    Greenhouse phone field has a separate country picker (defaults to US +1).
    Set it to Ireland (+353) then fill the national number, else validation
    fails with 'Phone number is too short'.
    """
    raw = PROFILE.get("phone", "")          # "+353 89 419 2749"
    national = raw.replace("+353", "").replace("+", "").strip()
    national = re.sub(r"\s+", "", national)  # "894192749"

    tel = await page.query_selector("input[type='tel'], #phone, input[autocomplete='tel']")
    if not tel:
        return

    # ── intl-tel-input (Greenhouse standard): .iti__selected-country picker ────
    try:
        selected = await page.query_selector(".iti__selected-country, .iti__flag-container, .iti__selected-flag")
        if selected:
            await selected.click(timeout=2500)
            await page.wait_for_timeout(400)
            # A search box appears — type Ireland to filter
            search = await page.query_selector(".iti__search-input")
            if search:
                await search.fill("Ireland", timeout=2500)
                await page.wait_for_timeout(500)
            # Click the Ireland list item (by data-country-code or text)
            ie = await page.query_selector(
                "li.iti__country[data-country-code='ie'], "
                ".iti__country:has-text('Ireland'), "
                "li:has-text('Ireland (+353)'), li:has-text('Ireland')"
            )
            if ie:
                await ie.click(timeout=2500)
                await page.wait_for_timeout(300)
    except Exception:
        pass

    # ── Fallback: react-select style country picker ───────────────────────────
    try:
        cur = await page.query_selector(".iti__selected-dial-code")
        dial = (await cur.text_content() or "") if cur else ""
        if "+353" not in dial:
            shell = await page.query_selector(".select-shell")
            if shell and "+1" in ((await shell.text_content()) or ""):
                await shell.click(timeout=2000)
                await page.wait_for_timeout(400)
                opt = await page.query_selector(
                    "[class*='option']:has-text('Ireland'), li:has-text('Ireland')")
                if opt:
                    await opt.click(timeout=2000)
                    await page.wait_for_timeout(300)
    except Exception:
        pass

    # ── Fill the national number ──────────────────────────────────────────────
    try:
        await tel.fill("")
        await tel.fill(national, timeout=3000)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# FIELD FILLERS PER ATS
# ─────────────────────────────────────────────────────────────────────────────
_MONTHS = {"jan": "January", "feb": "February", "mar": "March", "apr": "April",
           "may": "May", "jun": "June", "jul": "July", "aug": "August",
           "sep": "September", "oct": "October", "nov": "November", "dec": "December"}


def _parse_exp_dates(dates: str):
    """'Oct 2025 – Present' → ('October','2025','','',True);
       'Jun 2022 – Aug 2024' → ('June','2022','August','2024',False)."""
    d = dates or ""
    is_current = ("present" in d.lower() or "current" in d.lower())
    parts = re.split(r"\s*(?:–|—|-|\bto\b)\s*", d, maxsplit=1, flags=re.I) if d else [""]

    def one(tok):
        t = (tok or "").strip().lower()
        m = re.search(r"([a-z]{3,9})\.?\s*(\d{4})", t)
        if m:
            return _MONTHS.get(m.group(1)[:3], ""), m.group(2)
        y = re.search(r"(\d{4})", t)
        return "", (y.group(1) if y else "")

    sm, sy = one(parts[0]) if parts else ("", "")
    em, ey = ("", "")
    if len(parts) > 1 and not is_current:
        em, ey = one(parts[1])
    return sm, sy, em, ey, is_current


async def _field_label(el) -> str:
    """Best-effort label text for any input/select (Greenhouse wraps each field)."""
    try:
        t = await el.evaluate("""e => {
            if (e.id) { const l = document.querySelector(`label[for="${e.id}"]`); if (l && l.textContent) return l.textContent; }
            let n = e, h = 0;
            while (n && h < 5) {
                const l = n.querySelector && n.querySelector('label,legend');
                if (l && l.textContent) return l.textContent;
                n = n.parentElement; h++;
            }
            return e.getAttribute('aria-label') || e.getAttribute('name') || '';
        }""")
        return (t or "").replace("*", "").strip().lower()
    except Exception:
        return ""


def _score_option(text: str, match: list[str], prefer: list[str], avoid: list[str]) -> float:
    """Score a dropdown option against what we want. Must contain a `match` term to
    qualify; `prefer` terms add points, `avoid` terms (wrong specialisations like
    MBA/JD) subtract; shorter/more-generic options win ties."""
    t = (text or "").lower().strip()
    if not t or not any(m in t for m in match):
        return -1.0
    s = 100.0
    if any(a in t for a in avoid):
        s -= 60.0
    if any(p in t for p in prefer):
        s += 25.0
    s -= len(t) * 0.1          # prefer concise, generic labels
    return s


async def _pick_dropdown_by_keyword(page: Page, label_keys, match: list[str],
                                    prefer: list[str] | None = None,
                                    avoid: list[str] | None = None) -> bool:
    """Open a labelled dropdown (native <select> OR combobox), read its ACTUAL
    options, score them, and select the best match — so it adapts to whatever
    wording a given form uses ('Master's Degree', 'Master of Science', …) instead
    of blindly grabbing the first option."""
    keys = [label_keys] if isinstance(label_keys, str) else list(label_keys)
    m  = [x.lower() for x in match]
    pf = [x.lower() for x in (prefer or [])]
    av = [x.lower() for x in (avoid or [])]

    for sel in await page.query_selector_all("select"):
        try:
            lbl = await _field_label(sel)
            if not any(k in lbl for k in keys):
                continue
            opts = await sel.evaluate("el => Array.from(el.options).map(o => o.text)")
            best, best_s = None, 0.0
            for o in opts:
                sc = _score_option(o, m, pf, av)
                if sc > best_s:
                    best, best_s = o, sc
            if best:
                await sel.select_option(label=best, timeout=2000)
                log.info(f"Dropdown {keys[0]} → {best}")
                return True
        except Exception:
            pass

    for trg in await page.query_selector_all("[role='combobox'], [aria-haspopup='listbox'], div[class*='select__control']"):
        try:
            ctx = await trg.evaluate("el => { let n=el,h=0,t=''; while(n&&h<4){t+=' '+(n.textContent||'');n=n.parentElement;h++;} return t.toLowerCase(); }") or ""
            if not any(k in ctx for k in keys):
                continue
            await trg.click(timeout=2000)
            await page.wait_for_timeout(350)
            best_el, best_s = None, 0.0
            for o in await page.query_selector_all("[role='option'], li[class*='option'], div[class*='option']"):
                try:
                    if not await o.is_visible():
                        continue
                    sc = _score_option(await o.inner_text(), m, pf, av)
                    if sc > best_s:
                        best_el, best_s = o, sc
                except Exception:
                    pass
            if best_el:
                txt = (await best_el.inner_text() or "").strip()
                await best_el.click(timeout=2000)
                log.info(f"Dropdown {keys[0]} → {txt}")
                return True
            await page.keyboard.press("Escape")
        except Exception:
            pass
    return False


async def _set_month_dropdown(page: Page, label_key: str, month: str):
    """Select a month in a start/end-date dropdown (native <select> or combobox)."""
    if not month:
        return
    for sel in await page.query_selector_all("select"):
        try:
            if label_key in (await _field_label(sel)):
                await sel.select_option(label=month, timeout=2000)
                return
        except Exception:
            pass
    for trg in await page.query_selector_all("[role='combobox'], [aria-haspopup='listbox'], div[class*='select__control']"):
        try:
            ctx = await trg.evaluate("el => { let n=el,h=0,t=''; while(n&&h<4){t+=' '+(n.textContent||'');n=n.parentElement;h++;} return t.toLowerCase(); }") or ""
            if label_key not in ctx:
                continue
            await trg.click(timeout=2000)
            await page.wait_for_timeout(300)
            for o in await page.query_selector_all("[role='option'], li[class*='option'], div[class*='option']"):
                if await o.is_visible() and (await o.inner_text() or "").strip().lower() == month.lower():
                    await o.click(timeout=2000)
                    return
            await page.keyboard.press("Escape")
        except Exception:
            pass


async def fill_education_history(page: Page):
    """Fill a structured Education block (school, degree, discipline, dates) from
    PROFILE['education'][0] — most recent. Maps the degree to the right dropdown
    option (Master's/Bachelor's/Doctorate) instead of grabbing the first option."""
    edu = PROFILE.get("education") or []
    if not edu:
        return
    e = edu[0]
    school = e.get("school", "")
    degree = (e.get("degree", "") or "").lower()
    sm, sy, em, ey, current = _parse_exp_dates(e.get("dates", ""))

    # School — usually an autocomplete; type + select suggestion.
    if school:
        for sf in await page.query_selector_all("input[type='text'], input:not([type])"):
            try:
                lbl = await _field_label(sf)
                if any(k in lbl for k in ["school", "university", "institution", "college"]):
                    await sf.click(timeout=2000)
                    await sf.fill("", timeout=1500)
                    await sf.type(school, delay=40)
                    await page.wait_for_timeout(900)
                    picked = False
                    for sugg in ["#education_autocomplete-items > div", "ul.ui-autocomplete li",
                                 "[class*='autocomplete'] li", "[role='option']"]:
                        for o in await page.query_selector_all(sugg):
                            try:
                                if await o.is_visible():
                                    await o.click(timeout=2000)
                                    picked = True
                                    break
                            except Exception:
                                pass
                        if picked:
                            break
                    if not picked:
                        try:
                            await sf.press("ArrowDown")
                            await page.wait_for_timeout(200)
                            await sf.press("Enter")
                        except Exception:
                            pass
                    break
            except Exception:
                pass

    # Degree dropdown — score the form's ACTUAL options against the degree level,
    # avoiding wrong specialisations (MBA / JD / MFA / MEd …).
    deg_keys = ["degree", "education level", "level of education", "highest level", "degree type"]
    if any(k in degree for k in ["phd", "ph.d", "doctor", "dphil"]):
        match, prefer, avoid = (["doctor", "ph.d", "phd", "doctorate", "dphil"],
                                ["philosophy", "doctorate"],
                                ["master", "bachelor", "associate", "high school"])
    elif any(k in degree for k in ["msc", "m.sc", "m.s", "master", "mtech", "m.tech", "m.eng", "mba", "ma ", "m.a"]):
        match  = ["master", "m.s", "msc", "m.sc", "graduate", "post-grad", "postgrad", "mtech", "m.eng"]
        prefer = ["science", "master's degree", "master of science", "master’s degree"]
        avoid  = ["business", "administration", "mba", "fine art", "law", "juris",
                  "nursing", "divinity", "public", "education", "bachelor", "associate",
                  "high school", "doctor"]
    elif any(k in degree for k in ["bsc", "b.sc", "btech", "b.tech", "b.e", "bachelor", "ba ", "b.a"]):
        match  = ["bachelor", "b.s", "bsc", "b.sc", "undergrad"]
        prefer = ["science", "bachelor's degree", "bachelor of science"]
        avoid  = ["master", "associate", "high school", "doctor", "fine art", "business administration"]
    else:
        match, prefer, avoid = (["degree"], [], [])
    if match:
        await _pick_dropdown_by_keyword(page, deg_keys, match, prefer, avoid)

    # Discipline / field of study → Computer Science (best-matching option).
    for key in ("discipline", "field of study", "major", "area of study"):
        if await _pick_dropdown_by_keyword(
                page, key, ["computer science", "computer", "computing", "software"],
                prefer=["computer science"], avoid=["engineering technology"]):
            break

    # Dates (education blocks use the same month/year layout).
    await _set_month_dropdown(page, "start date month", sm)
    for inp in await page.query_selector_all("input[type='text'], input[type='number'], input:not([type])"):
        try:
            lbl = await _field_label(inp)
            if "start date year" in lbl and sy:
                await inp.fill(sy, timeout=2000)
            elif "end date year" in lbl and ey:
                await inp.fill(ey, timeout=2000)
        except Exception:
            pass
    if not current:
        await _set_month_dropdown(page, "end date month", em)


async def fill_location_autocomplete(page: Page, location: str = "Dublin, Ireland"):
    """Greenhouse/Coinbase 'Location (City)' is a Google-style autocomplete: typing
    text isn't enough ('Please enter your location') — a suggestion must be picked.
    Type, then click the first suggestion (or ArrowDown+Enter)."""
    el = await page.query_selector(
        "#job_application_location, input[id*='location' i], "
        "input[name*='location' i], input[aria-label*='location' i]")
    if not el:
        return
    try:
        await el.scroll_into_view_if_needed(timeout=1500)
        await el.click(timeout=2000)
        await el.fill("", timeout=1500)
        await el.type(location, delay=45)
        await page.wait_for_timeout(1000)
        for sugg in ["#location_autocomplete-items > div", "ul.ui-autocomplete li a",
                     "ul.ui-autocomplete li", "[class*='autocomplete'] li",
                     "[class*='autocomplete'] [role='option']", "[role='option']"]:
            for o in await page.query_selector_all(sugg):
                try:
                    if await o.is_visible():
                        await o.click(timeout=2000)
                        log.info("Location autocomplete → suggestion selected")
                        return
                except Exception:
                    pass
        await el.press("ArrowDown")
        await page.wait_for_timeout(300)
        await el.press("Enter")
    except Exception:
        pass


async def fill_employment_history(page: Page):
    """Fill a structured Employment block (company, title, start/end month+year,
    'Current role') from PROFILE['experience'][0] — the most recent role. These
    are the required fields that otherwise read 'Company name is required' etc."""
    exp = PROFILE.get("experience") or []
    if not exp:
        return
    e = exp[0]
    company, title = e.get("company", ""), e.get("title", "")
    sm, sy, em, ey, current = _parse_exp_dates(e.get("dates", ""))

    # Text fields — overwrite (existing logic mis-fills these, e.g. year='Imme').
    for inp in await page.query_selector_all("input[type='text'], input[type='number'], input:not([type])"):
        try:
            lbl = await _field_label(inp)
            if "company name" in lbl and company:
                await inp.fill(company, timeout=2500)
            elif lbl in ("title", "job title") and title:
                await inp.fill(title, timeout=2500)
            elif "start date year" in lbl and sy:
                await inp.fill(sy, timeout=2500)
            elif "end date year" in lbl and ey:
                await inp.fill(ey, timeout=2500)
        except Exception:
            pass

    # Month dropdowns (native <select> or combobox) for start/end.
    await _set_month_dropdown(page, "start date month", sm)
    if current:
        for cb in await page.query_selector_all("input[type='checkbox']"):
            if "current" in (await _field_label(cb)):
                await _safe_check(cb)
                break
    else:
        await _set_month_dropdown(page, "end date month", em)


async def fill_country_fields(page: Page, country: str = "Ireland"):
    """Best-effort: set any required 'what country are you based in' dropdown to the
    given country — across native <select>, react-select, and aria comboboxes. The
    older .select__ logic misses the newer Greenhouse layout (e.g. Brex), leaving the
    required country field blank so the form won't submit. Tightly gated to fields
    whose label/context mentions 'country' so it can't disturb other dropdowns."""
    cl = country.lower()

    # 1) Native <select> elements labelled "country"
    for sel in await page.query_selector_all("select"):
        try:
            lbl = await sel.evaluate("""el => {
                let t = '';
                if (el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) t = l.textContent || ''; }
                if (!t && el.closest('label')) t = el.closest('label').textContent || '';
                if (!t) t = el.getAttribute('aria-label') || el.getAttribute('name') || '';
                return t.toLowerCase();
            }""") or ""
            if "country" not in lbl:
                continue
            cur = await sel.evaluate("el => ((el.options[el.selectedIndex]||{}).text || '')")
            if cur and "select" not in cur.lower() and cur.strip():
                continue
            picked = await sel.evaluate("""(el, c) => {
                for (const o of el.options) {
                    if (o.text && o.text.toLowerCase().includes(c)) {
                        el.value = o.value;
                        el.dispatchEvent(new Event('input',  {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        return o.text;
                    }
                }
                return '';
            }""", cl)
            if picked:
                log.info(f"Country (native select) → {picked}")
        except Exception:
            pass

    # 2) Custom comboboxes (newer Greenhouse / react) near a "country" label
    triggers = await page.query_selector_all(
        "[role='combobox'], [aria-haspopup='listbox'], "
        "div[class*='select__control'], button[class*='select__control']"
    )
    for trg in triggers:
        try:
            ctx = await trg.evaluate("""el => {
                let n = el, hops = 0, t = '';
                while (n && hops < 4) { t += ' ' + (n.textContent || ''); n = n.parentElement; hops++; }
                return t.toLowerCase();
            }""") or ""
            if "country" not in ctx:
                continue
            cur = (await trg.inner_text() or "").strip().lower()
            if cur and "select" not in cur and "ireland" not in cur and len(cur) < 40:
                continue  # already has a value
            if "ireland" in cur:
                continue
            await trg.scroll_into_view_if_needed(timeout=1500)
            await trg.click(timeout=2500)
            await page.wait_for_timeout(350)
            inp = await page.query_selector(
                ".select__input input, input[aria-autocomplete='list'], input[role='combobox']")
            if inp:
                try:
                    await inp.type(country, delay=30)
                    await page.wait_for_timeout(450)
                except Exception:
                    pass
            done = False
            for opt in await page.query_selector_all("[role='option'], li[class*='option'], div[class*='option']"):
                try:
                    if not await opt.is_visible():
                        continue
                    if cl in (await opt.inner_text() or "").lower():
                        await opt.click(timeout=2000)
                        done = True
                        log.info(f"Country (combobox) → {country}")
                        break
                except Exception:
                    pass
            if not done:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
        except Exception:
            pass


async def fill_greenhouse(page: Page, app: dict):
    """
    Greenhouse boards.greenhouse.io/company/jobs/ID structure:
      1. Page loads with job description + "Apply for this Job" button
      2. Click Apply → form appears OR redirects to /apply sub-page
      3. Fill form fields and submit
    """
    # Step 1: Wait for page to load (job description page)
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(1500)

    # Step 2: Click "Apply" button if form not immediately visible
    if not await page.query_selector("#application_form"):
        apply_btn = await page.query_selector(
            "a[href*='/apply'], button:has-text('Apply'), "
            "a:has-text('Apply for this Job'), a:has-text('Apply Now'), "
            ".apply-button, #apply-button"
        )
        if apply_btn:
            await apply_btn.click()
            try:
                await page.wait_for_selector("#application_form", timeout=8000)
            except Exception:
                pass  # form might be inline or different selector

    async def fill(selector, value):
        try:
            el = await page.query_selector(selector)
            if el:
                await el.fill(value)
        except Exception: pass

    await fill("#first_name",         PROFILE["name"].split()[0])
    await fill("#last_name",          PROFILE["name"].split()[-1])
    await fill("#email",              PROFILE["email"])
    await fill("#job_application_location", PROFILE["location"])

    # Phone needs special handling: set Ireland country code first (else "too short")
    await fill_phone_with_country(page)

    # LinkedIn + GitHub — check aria-label, placeholder, and nearby label text
    for field in await page.query_selector_all("input[type='text'], input[type='url']"):
        aria  = (await field.get_attribute("aria-label") or "").lower()
        ph    = (await field.get_attribute("placeholder") or "").lower()
        name  = (await field.get_attribute("name") or "").lower()
        combo = aria + ph + name
        if "linkedin"  in combo: await field.fill(f"https://{PROFILE['linkedin']}")
        elif "github"  in combo: await field.fill(f"https://{PROFILE['github']}")
        elif "website" in combo or "portfolio" in combo:
            await field.fill(f"https://{PROFILE['github']}")

    # Cover letter
    for ta in await page.query_selector_all("textarea"):
        ph        = (await ta.get_attribute("placeholder") or "").lower()
        aria_lbl  = (await ta.get_attribute("aria-label") or "").lower()
        if "cover" in ph or "cover" in aria_lbl or "letter" in ph:
            await ta.fill(app.get("cover_letter", ""))
            break

    # Resume upload
    upload = await page.query_selector("input[type='file']")
    resume = _get_best_resume(app)
    if upload and resume:
        await upload.set_input_files(resume, timeout=5000)

    # ── Answer custom/required questions (work auth, "why us", dropdowns) ──────
    await answer_custom_questions(page, app)

    # ── Required "what country are you based in?" dropdown (newer GH layout) ────
    await fill_country_fields(page)

    # ── Structured fields: location autocomplete + employment/education blocks ─
    await fill_location_autocomplete(page)
    await fill_employment_history(page)
    await fill_education_history(page)

    # ── Re-set phone LAST (custom-question handling can disturb the picker) ────
    await fill_phone_with_country(page)

    # Submit
    btn = await page.query_selector("input[type='submit'], button[type='submit']")
    if btn:
        await btn.click()
        await page.wait_for_timeout(3000)

async def fill_lever(page: Page, app: dict):
    await page.wait_for_selector(".application-form", timeout=10000)

    async def safe_fill(selector, value):
        try: await page.fill(selector, value)
        except Exception: pass

    await safe_fill("input[name='name']",      PROFILE["name"])
    await safe_fill("input[name='email']",     PROFILE["email"])
    await safe_fill("input[name='phone']",     PROFILE["phone"])
    await safe_fill("input[name='org']",       "EEZPAYS")
    await safe_fill("input[name='urls[LinkedIn]']", f"https://{PROFILE['linkedin']}")
    await safe_fill("input[name='urls[GitHub]']",   f"https://{PROFILE['github']}")

    # Cover letter
    cl = await page.query_selector("textarea[name='comments']")
    if cl: await cl.fill(app["cover_letter"])

    # Resume
    upload = await page.query_selector("input[type='file']")
    if upload and Path(RESUME_PDF_PATH).exists():
        await upload.set_input_files(RESUME_PDF_PATH)

    btn = await page.query_selector("button[type='submit'], .template-btn-submit")
    if btn:
        await btn.click()
        await page.wait_for_timeout(3000)

async def fill_ashby(page: Page, app: dict):
    """
    Ashby flow:
      1. Job description page → click 'Apply for this Job'
      2. Redirects to /application page with the actual form
      3. Fill name, email, phone, LinkedIn, GitHub, upload PDF, submit
    """
    # Step 1: Click Apply button if we're on the job description page
    if "/application" not in page.url:
        apply = await page.query_selector(
            "a:has-text('Apply for this Job'), a:has-text('Apply Now'), "
            "button:has-text('Apply for this Job'), button:has-text('Apply')"
        )
        if apply:
            href = await apply.get_attribute("href")
            if href:
                await page.goto(f"https://jobs.ashbyhq.com{href}" if href.startswith("/") else href,
                                wait_until="domcontentloaded", timeout=12000)
            else:
                await apply.click()
                await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(1500)

    # Step 2: Fill by field name (Ashby uses system field names)
    async def safe_fill(selector, value):
        try:
            el = await page.query_selector(selector)
            if el: await el.fill(value, timeout=4000)
        except Exception: pass

    await safe_fill("input[name='_systemfield_name']",  PROFILE["name"])
    await safe_fill("input[name='_systemfield_email']", PROFILE["email"])
    await safe_fill("input[name='_systemfield_phone']", PROFILE["phone"])

    # Additional text fields — check labels
    for label_text, value in {
        "First name":  PROFILE["name"].split()[0],
        "Last name":   PROFILE["name"].split()[-1],
        "LinkedIn":    f"https://{PROFILE['linkedin']}",
        "GitHub":      f"https://{PROFILE['github']}",
        "Website":     f"https://{PROFILE['github']}",
    }.items():
        try:
            label = await page.query_selector(f"label:has-text('{label_text}')")
            if label:
                inp_id = await label.get_attribute("for")
                if inp_id:
                    await page.fill(f"#{inp_id}", value, timeout=3000)
        except Exception: pass

    # Cover letter — any textarea
    for ta in await page.query_selector_all("textarea"):
        ph = (await ta.get_attribute("placeholder") or "").lower()
        if "cover" in ph or "letter" in ph or "additional" in ph:
            await ta.fill(app.get("cover_letter", ""))
            break

    # Resume upload — Ashby has two file inputs (resume + cover letter)
    resume = _get_best_resume(app)
    uploads = await page.query_selector_all("input[type='file']")
    if uploads and resume:
        await uploads[0].set_input_files(resume, timeout=5000)

    await answer_custom_questions(page, app)
    await fill_country_fields(page)

    # Ashby's "Submit Application" button is a JS-driven <button> with no
    # type='submit', so the narrow selector below misses it and the form is
    # never actually submitted. Fall back to the text-based click_first_submit.
    btn = await page.query_selector(
        "button[type='submit'], button:has-text('Submit Application'), "
        "button:has-text('Submit application'), button:has-text('Submit')"
    )
    clicked = False
    if btn:
        try:
            await btn.click(timeout=3500)
            clicked = True
        except Exception:
            clicked = False
    if not clicked:
        await click_first_submit(page)
    await page.wait_for_timeout(3000)


async def click_first_submit(page: Page) -> bool:
    """Click the first viable submit/apply button with short fallbacks."""
    selectors = (
        "button[type='submit']", "input[type='submit']",
        "button:has-text('Submit application')", "button:has-text('Submit Application')",
        "button:has-text('Submit')", "button:has-text('Send application')",
        "button:has-text('Apply for this position')", "a:has-text('Apply for this position')",
        "button:has-text('Apply now')", "a:has-text('Apply now')",
        "button:has-text('Apply')", "a:has-text('Apply')",
    )
    for selector in selectors:
        loc = page.locator(selector)
        try:
            count = min(await loc.count(), 8)
        except Exception:
            continue
        for i in range(count):
            item = loc.nth(i)
            try:
                if not await item.is_visible(timeout=400):
                    continue
                href = (await item.get_attribute("href")) or ""
                if any(bad in href.lower() for bad in ["/apply?batch", "ycombinator.com/apply"]):
                    continue
                try:
                    await item.click(timeout=3500)
                except Exception:
                    handle = await item.element_handle(timeout=1000)
                    if handle:
                        await handle.evaluate("el => el.click()")
                    else:
                        continue
                await page.wait_for_timeout(1800)
                return True
            except Exception:
                continue
    return False


async def deterministic_generic_fill(page: Page, app: dict):
    """Best-effort form fill that does not rely on Claude selectors."""
    async def label_for(el) -> str:
        try:
            return await el.evaluate("""el => {
                const parts = [el.getAttribute('aria-label')||'', el.getAttribute('placeholder')||'', el.name||'', el.id||''];
                if (el.id) {
                    const l = document.querySelector(`label[for="${el.id}"]`);
                    if (l) parts.push(l.textContent || '');
                }
                const ownLabel = el.closest('label');
                if (ownLabel) parts.push(ownLabel.textContent || '');
                let n = el;
                for (let i=0; i<6 && n; i++, n=n.parentElement) {
                    const q = n.querySelector('label,legend,h3,h4,[class*=label],[class*=question]');
                    if (q) parts.push(q.textContent || '');
                    if (n.previousElementSibling) parts.push(n.previousElementSibling.textContent || '');
                }
                return parts.join(' ').replace(/\\s+/g, ' ').trim();
            }""") or ""
        except Exception:
            return ""

    resume = _get_best_resume(app)
    for upload in await page.query_selector_all("input[type='file']"):
        if resume:
            try:
                await upload.set_input_files(resume, timeout=5000)
            except Exception:
                pass

    for el in await page.query_selector_all("input, textarea"):
        try:
            tag = (await el.evaluate("el => el.tagName.toLowerCase()")) or ""
            typ = ((await el.get_attribute("type")) or "").lower()
            if typ in {"hidden", "submit", "button", "file", "checkbox", "radio"}:
                continue
            if typ in {"email"}:
                val = PROFILE["email"]
            elif typ in {"tel", "phone"}:
                val = PROFILE["phone"]
            else:
                label = await label_for(el)
                low = label.lower()
                if "first" in low and "name" in low:
                    val = PROFILE["name"].split()[0]
                elif "last" in low and "name" in low:
                    val = PROFILE["name"].split()[-1]
                elif "full" in low and "name" in low or low.strip() == "name":
                    val = PROFILE["name"]
                elif "linkedin" in low:
                    val = f"https://{PROFILE['linkedin']}"
                elif "github" in low or "portfolio" in low or "website" in low:
                    val = f"https://{PROFILE['github']}"
                elif "cover" in low or "letter" in low or tag == "textarea":
                    val = app.get("cover_letter", "") or _build_skill_answer(label, app)
                elif _is_technical_skill_question(label):
                    val = _build_skill_answer(label, app)
                elif "location" in low or "city" in low or "based" in low:
                    val = PROFILE["location"]
                else:
                    continue
            current = await el.input_value()
            if not current.strip():
                await el.fill(str(val), timeout=3000)
        except Exception:
            pass

    # Skill/required checkboxes are common on generic boards such as Remotive.
    for cb in await page.query_selector_all("input[type='checkbox']"):
        try:
            if await cb.is_checked():
                continue
            label = (await label_for(cb)).lower()
            required = bool(await cb.get_attribute("required")) or "required" in label
            skillish = _is_technical_skill_question(label) or any(k in label for k in [
                "aws", "gcp", "google cloud", "azure", "required technical skills",
                "required skills", "i confirm", "i agree", "terms", "privacy",
            ])
            if required or skillish:
                await _safe_check(cb)
        except Exception:
            pass

async def fill_generic(page: Page, app: dict):
    """Claude-assisted generic form filler — analyses the page HTML."""
    # Reveal common single-page application forms before asking Claude to map
    # fields. Many job pages show only a job description until Apply is clicked.
    for selector in (
        "a:has-text('Apply for this position')", "button:has-text('Apply for this position')",
        "a:has-text('Apply now')", "button:has-text('Apply now')",
        "a:has-text('Apply')", "button:has-text('Apply')",
        "a:has-text('Start application')", "button:has-text('Start application')",
    ):
        try:
            el = page.locator(selector).first
            if await el.count() and await el.is_visible(timeout=1000):
                href = await el.get_attribute("href")
                if href and any(bad in href.lower() for bad in ["/apply?batch", "ycombinator.com/apply"]):
                    continue
                if href and not href.startswith("mailto:"):
                    from urllib.parse import urljoin
                    await page.goto(urljoin(page.url, href), wait_until="domcontentloaded", timeout=15000)
                else:
                    try:
                        await el.click(timeout=3000)
                    except Exception:
                        handle = await el.element_handle(timeout=1000)
                        if handle:
                            await handle.evaluate("el => el.click()")
                await page.wait_for_timeout(1500)
                await dismiss_cookie_banners(page)
                break
        except Exception:
            pass

    html = ""
    for _ in range(3):
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
            html = await page.content()
            break
        except Exception:
            await page.wait_for_timeout(1200)
    if not html:
        log.warning("Generic form analysis skipped: page kept navigating.")
        return
    prompt = f"""Given this job application form HTML, return a JSON list of fill actions.
Each action: {{"selector": "css_selector", "action": "fill|click|upload", "value": "..."}}

Available values:
- first_name: {PROFILE['name'].split()[0]}
- last_name: {PROFILE['name'].split()[-1]}
- full_name: {PROFILE['name']}
- email: {PROFILE['email']}
- phone: {PROFILE['phone']}
- location: {PROFILE['location']}
- linkedin: https://{PROFILE['linkedin']}
- github: https://{PROFILE['github']}
- cover_letter: [USE_COVER_LETTER]

HTML (trimmed): {html[:4000]}

Return JSON array only."""

    global _claude_disabled
    if _claude_disabled or client is None:
        actions = []
    else:
        try:
            msg = client.messages.create(
                model="claude-sonnet-4-6", max_tokens=800,
                messages=[{"role": "user", "content": prompt}]
            )
            actions = _parse_json(msg.content[0].text, [])
        except Exception as e:
            if _is_quota_error(e):
                _claude_disabled = True
                log.warning("Claude unavailable (credits/quota) — skipping AI form-fill for the rest of this run.")
            else:
                log.warning(f"Generic form analysis failed: {str(e)[:80]}")
            actions = []
    if not isinstance(actions, list):
        actions = []

    async def first_visible(selector: str):
        loc = page.locator(selector)
        try:
            count = min(await loc.count(), 12)
        except Exception:
            return None
        for i in range(count):
            item = loc.nth(i)
            try:
                if await item.is_visible(timeout=400) and await item.is_enabled(timeout=400):
                    return item
            except Exception:
                continue
        return None

    for act in actions:
        try:
            if not isinstance(act, dict):
                continue
            sel = act.get("selector")
            val = act.get("value")
            if not sel or val is None:
                continue
            if val == "[USE_COVER_LETTER]": val = app.get("cover_letter", "")
            action = act.get("action")
            target = await first_visible(sel)
            if target is None:
                continue
            if action == "fill":
                await target.fill(str(val), timeout=4000)
            elif action == "click":
                await target.click(timeout=4000)
            elif action == "upload":
                resume = _get_best_resume(app)
                if resume:
                    await target.set_input_files(resume, timeout=5000)
            await page.wait_for_timeout(300)
        except Exception as e:
            log.warning(f"Generic fill action failed [{sel}]: {e}")

    await deterministic_generic_fill(page, app)

    # Generic forms still benefit from the deterministic custom-question pass.
    try:
        await answer_custom_questions(page, app)
        await fill_country_fields(page)
        await fill_location_autocomplete(page)
        await fill_employment_history(page)
        await fill_education_history(page)
    except Exception:
        pass

    await deterministic_generic_fill(page, app)
    await click_first_submit(page)

# ─────────────────────────────────────────────────────────────────────────────
# ANTI-BOT HELPERS
# ─────────────────────────────────────────────────────────────────────────────
async def human_delay(min_ms=500, max_ms=1800):
    import random
    await asyncio.sleep(random.uniform(min_ms, max_ms) / 1000)


async def dismiss_cookie_banners(page: Page):
    """Dismiss common cookie banners that intercept form clicks."""
    for selector in (
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('I accept')",
        "button:has-text('Agree')",
        "button:has-text('Allow all')",
        "button:has-text('Got it')",
    ):
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible(timeout=600):
                await loc.click(timeout=2000)
                await page.wait_for_timeout(500)
                return
        except Exception:
            pass

async def setup_stealth_browser(playwright, session_file: str | None = None):
    # Guided/interactive mode opens a VISIBLE browser so the human can solve the
    # CAPTCHA and click the final submit on forms that block automation.
    interactive = os.environ.get("AGENT3_INTERACTIVE") == "1"
    browser = await playwright.chromium.launch(
        headless=not interactive,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-extensions",
        ]
    )
    ctx_kwargs = dict(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="en-IE",
    )
    if session_file and Path(session_file).exists():
        ctx_kwargs["storage_state"] = session_file
        log.info(f"Loaded session: {session_file}")

    context = await browser.new_context(**ctx_kwargs)
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, context


async def collect_visible_errors(page: Page) -> list[str]:
    """Return short visible validation/blocker messages from the current page."""
    selectors = [
        ".error", ".field-error", ".error-message", ".validation-error",
        "[role='alert']", "[aria-live='assertive']", "[class*='error']",
        "text=/required|please accept|invalid|captcha|recaptcha|verify/i",
    ]
    found: list[str] = []
    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = min(await loc.count(), 12)
            for i in range(count):
                item = loc.nth(i)
                if not await item.is_visible(timeout=300):
                    continue
                txt = re.sub(r"\s+", " ", (await item.inner_text(timeout=700) or "")).strip()
                if txt and len(txt) <= 220 and txt not in found:
                    found.append(txt)
        except Exception:
            continue
    return found[:8]


def has_captcha_blocker(page_text: str, errors: list[str]) -> bool:
    combined = " ".join([page_text, *errors]).lower()
    return any(k in combined for k in [
        "recaptcha", "captcha", "i'm not a robot", "i am not a robot",
        "verify you are human", "human verification"
    ])


async def has_captcha_widget(page) -> bool:
    """Detect CAPTCHA widgets that render inside a (cross-origin) iframe —
    Cloudflare Turnstile, Google reCAPTCHA, hCaptcha. Their challenge text lives
    in the frame, so page.inner_text('body') never sees it and has_captcha_blocker
    misses them, leaving a hard-blocked form wrongly stuck in needs_review."""
    selectors = [
        "iframe[src*='challenges.cloudflare.com']",   # Cloudflare Turnstile
        "iframe[src*='recaptcha']",                   # Google reCAPTCHA
        "iframe[src*='hcaptcha.com']",                # hCaptcha
        "iframe[title*='captcha' i]",
        "iframe[title*='cloudflare' i]",
        ".cf-turnstile", "[class*='turnstile']",
        ".g-recaptcha", ".h-captcha", "[data-sitekey]",
    ]
    for sel in selectors:
        try:
            if await page.query_selector(sel):
                return True
        except Exception:
            pass
    return False


def classify_non_form_page(url: str, page_text: str) -> tuple[str, str] | None:
    """Classify pages that should not stay in needs_review."""
    u = (url or "").lower()
    t = (page_text or "").lower()
    closed_bits = [
        "this job is not available anymore",
        "this job is no longer available",
        "position has been filled",
        "no longer accepting applications",
        "job has expired",
        "application deadline has passed",
    ]
    if any(bit in t for bit in closed_bits):
        return "failed", "Job is closed/unavailable; no retry needed."
    global_apply_bits = [
        "apply to y combinator",
        "accepting applications for the fall",
        "batch funding cycle",
    ]
    if "ycombinator.com/apply" in u or any(bit in t for bit in global_apply_bits):
        return "needs_manual", "Generic apply link led to YC/company-level application, not this job."
    if "news.ycombinator.com" in u:
        return "needs_manual", "Hacker News listing requires manual follow-up; no application form detected."
    return None

# ─────────────────────────────────────────────────────────────────────────────
# MAIN SUBMITTER
# ─────────────────────────────────────────────────────────────────────────────
async def interactive_resolve(page, app: dict, success_signals: list[str]) -> dict:
    """Guided mode: the form is already auto-filled in a VISIBLE browser. Hand it
    to the human to solve the CAPTCHA + click the final Submit, then verify.
    Returns {'action': 'submitted'|'skip'|'recheck', 'text': <page_text>}."""
    bar = "=" * 70
    print(f"\n{bar}")
    print(f"  👉  {app.get('company','?')} — {str(app.get('title',''))[:55]}")
    print(f"  Everything is already filled in. In the browser window, you ONLY need to:")
    print(f"     ▸ tick the 'I'm not a robot' / 'Verify you are human' box (if one shows)")
    print(f"  Then press ENTER here — I'll click Submit and confirm it for you.")
    print(f"  ( s = skip this one   ·   q = quit all )")
    print(bar)
    loop = asyncio.get_event_loop()
    while True:
        ans = (await loop.run_in_executor(None, input, "  ↳ ticked it? press Enter ")).strip().lower()
        if ans == "q":
            raise KeyboardInterrupt("user stopped guided submit")
        if ans == "s":
            return {"action": "skip", "text": ""}
        # Human has ticked the box — now WE click submit + verify (their only job
        # is the tick). Re-run the verification-code step too in case it reappears.
        try:
            await click_first_submit(page)
            await page.wait_for_timeout(3000)
        except Exception:
            pass
        try:
            txt = (await page.inner_text("body")).lower()
        except Exception:
            txt = ""
        if any(s in txt for s in success_signals):
            return {"action": "submitted", "text": txt}
        print("  ⚠️  No confirmation yet — the box may need re-ticking, or there's one more step.")
        conf = (await loop.run_in_executor(
            None, input, "      Re-tick + Enter to retry  ·  y = it DID submit  ·  s = skip: ")
        ).strip().lower()
        if conf == "y":
            return {"action": "submitted", "text": txt}
        if conf == "s":
            return {"action": "skip", "text": ""}
        # otherwise loop and try clicking Submit again


async def submit_application(app: dict) -> dict:
    # 1. Rewrite company-branded URLs → direct ATS embed
    apply_url = rewrite_to_direct_ats_url(app["url"])
    if apply_url != app["url"]:
        log.info(f"URL rewritten: {app['url'][:60]} → {apply_url[:60]}")

    # 2. Look for a saved login session for this domain
    session_file = get_session_for(apply_url) or get_session_for(app["url"])

    ats = detect_ats(apply_url)
    log.info(f"Submitting [{ats}]{' [session]' if session_file else ''}: {app['title']} @ {app['company']}")

    result = {
        "job_id":     app["id"],
        "title":      app["title"],
        "company":    app["company"],
        "url":        app["url"],
        "ats":        ats,
        "status":     "attempting",
        "submitted_at": "",
        "notes":      "",
    }

    async with async_playwright() as pw:
        browser, context = await setup_stealth_browser(pw, session_file=session_file)
        page = await context.new_page()

        # Track redirects to detect auth walls early
        final_url = {"value": apply_url}
        page.on("response", lambda r: final_url.update({"value": r.url}) if r.status in (200,) else None)

        try:
            await page.goto(apply_url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(800)
            await dismiss_cookie_banners(page)
            initial_text = (await page.inner_text("body")).lower()
            non_form = classify_non_form_page(page.url, initial_text)
            if non_form:
                status, note = non_form
                screenshot = f"outputs/screenshots/{app['id']}_{'auth' if status == 'needs_manual' else 'check'}.png"
                Path("outputs/screenshots").mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=screenshot)
                result["status"] = status
                result["notes"] = f"{note} Screenshot: {screenshot}"
                log.warning(f"{'🔐 Needs manual' if status == 'needs_manual' else '⚠️  Not retrying'}: {app['company']} — {note}")
                _write_agent4_result(result)
                return result

            # Aggregator listing (jobicy etc.) → the page has no form; follow its
            # outbound "Apply" link to the real ATS. If it can't be resolved to a
            # direct form, flag for manual rather than crashing on the redirect.
            if is_aggregator_url(page.url):
                real = await follow_aggregator_apply(page)
                if real and not is_aggregator_url(real):
                    apply_url = real
                    ats = detect_ats(apply_url)
                    await dismiss_cookie_banners(page)
                    log.info(f"Aggregator → real apply: {real[:65]} [{ats}]")
                else:
                    result["status"] = "needs_manual"
                    result["notes"]  = f"Aggregator listing — no resolvable direct form. Apply at: {app['url']}"
                    log.warning(f"🔗 Aggregator (flagged manual): {app['company']} — apply directly")
                    _write_agent4_result(result)
                    return result

            # Auth-wall detection by GROUND TRUTH, not nav text:
            # A real login wall has a password field AND no application form/apply path.
            # ("Sign in" nav links exist on almost every page — never use them alone.)
            has_password = await page.query_selector("input[type='password']")
            has_form     = await page.query_selector(
                "#application_form, input[name='first_name'], input[id='first_name'], "
                "input[name='email'], input[type='file']")
            has_apply    = await page.query_selector(
                "a[href*='apply'], button:has-text('Apply'), a:has-text('Apply for this Job')")
            cf_challenge = "just a moment" in (await page.content()).lower() and not has_form

            real_login_wall = (has_password is not None and has_form is None and has_apply is None) or cf_challenge
            if real_login_wall:
                screenshot = f"outputs/screenshots/{app['id']}_auth.png"
                Path("outputs/screenshots").mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=screenshot)
                result["status"] = "needs_manual"
                result["notes"]  = f"Genuine login/anti-bot wall. Screenshot: {screenshot}"
                log.warning(f"🔐 Needs manual: {app['company']} — genuine login wall")
                _write_agent4_result(result)
                return result

            import time as _t
            submit_epoch = _t.time()
            if   ats == "greenhouse":   await fill_greenhouse(page, app)
            elif ats == "lever":        await fill_lever(page, app)
            elif ats == "ashby":        await fill_ashby(page, app)
            elif ats == "workable":     await fill_generic(page, app)
            else:                       await fill_generic(page, app)

            await human_delay(1000, 2000)

            success_signals = ["thank you", "application submitted", "we'll be in touch",
                               "successfully applied", "received your application",
                               "application received", "you've applied",
                               "thanks for taking the time to apply",
                               "thanks for applying", "thank you for applying"]

            # If a security-code step appeared, read code from Gmail + resubmit
            page_text = (await page.inner_text("body")).lower()
            non_form = classify_non_form_page(page.url, page_text)
            if non_form:
                status, note = non_form
                result["status"] = status
                result["notes"] = note
                log.warning(f"{'🔐 Needs manual' if status == 'needs_manual' else '⚠️  Not retrying'}: {app['company']} — {note}")
            if result["status"] not in {"failed", "needs_manual"} and ats == "greenhouse" and not any(s in page_text for s in success_signals):
                did_code = await handle_verification_code(page, app, submit_epoch)
                if did_code:
                    page_text = (await page.inner_text("body")).lower()

            # If the first submit exposed required-field validation, make one
            # repair pass over the now-visible errored fields and submit again.
            if result["status"] not in {"failed", "needs_manual"} and not any(s in page_text for s in success_signals):
                errors = await collect_visible_errors(page)
                if errors and ats in {"greenhouse", "ashby", "generic", "workable"}:
                    log.info(f"Validation repair pass for {app['company']}: {' | '.join(errors[:3])}")
                    try:
                        await page.evaluate(
                            "document.querySelectorAll('[data-autofill-done]').forEach(e => e.removeAttribute('data-autofill-done'))"
                        )
                        if ats in {"generic", "workable"}:
                            await deterministic_generic_fill(page, app)
                        await answer_custom_questions(page, app)
                        if ats == "greenhouse":
                            await fill_phone_with_country(page)
                        btn = await page.query_selector(
                            "input[type='submit'], button[type='submit'], "
                            "button:has-text('Submit application'), button:has-text('Submit')"
                        )
                        if btn:
                            try:
                                await btn.click(timeout=3500)
                            except Exception:
                                await click_first_submit(page)
                            await page.wait_for_timeout(3000)
                            did_code = False
                            if ats == "greenhouse":
                                did_code = await handle_verification_code(page, app, submit_epoch)
                            if did_code:
                                await page.wait_for_timeout(1000)
                            page_text = (await page.inner_text("body")).lower()
                    except Exception as e:
                        log.warning(f"Validation repair failed: {str(e)[:120]}")

            if result["status"] in {"failed", "needs_manual"}:
                pass
            elif any(s in page_text for s in success_signals):
                result["status"]       = "submitted"
                result["submitted_at"] = datetime.now().isoformat()
                result["notes"]        = "Confirmed via page text"
                log.info(f"✅ Submitted: {app['title']} @ {app['company']}")
            elif os.environ.get("AGENT3_INTERACTIVE") == "1":
                # Guided mode: form is filled in a visible browser — let the human
                # solve the CAPTCHA + final submit instead of writing it off.
                outcome = await interactive_resolve(page, app, success_signals)
                if outcome["action"] == "submitted":
                    result["status"]       = "submitted"
                    result["submitted_at"] = datetime.now().isoformat()
                    result["notes"]        = "Confirmed via guided submit"
                    log.info(f"✅ Submitted (guided): {app['title']} @ {app['company']}")
                else:
                    result["status"] = "needs_manual"
                    result["notes"]  = "Skipped in guided mode — submit manually later."
                    log.warning(f"⏭  Skipped (guided): {app['company']}")
            else:
                screenshot = f"outputs/screenshots/{app['id']}_check.png"
                Path("outputs/screenshots").mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=screenshot)
                errors = await collect_visible_errors(page)
                if has_captcha_blocker(page_text, errors) or await has_captcha_widget(page):
                    result["status"] = "needs_manual"
                    result["notes"]  = f"CAPTCHA/human verification requires browser session. Screenshot: {screenshot}"
                    log.warning(f"🔐 Needs manual: {app['company']} — CAPTCHA/human verification")
                elif any("password" in e.lower() for e in errors) or "please enter a password" in page_text:
                    result["status"] = "needs_manual"
                    result["notes"]  = f"Account/password required. Screenshot: {screenshot}"
                    log.warning(f"🔐 Needs manual: {app['company']} — account/password required")
                else:
                    result["status"] = "needs_review"
                    detail = f" Visible errors: {' | '.join(errors)}" if errors else ""
                    result["notes"]  = f"No confirmation detected.{detail} Screenshot: {screenshot}"
                    log.warning(f"⚠️  Needs review: {app['company']} — screenshot saved")

        except PWTimeout:
            result["status"] = "needs_manual"
            result["notes"]  = "Page load timeout — likely auth wall or slow network. Apply manually."
            log.warning(f"⏱  Timeout (flagged manual): {app['company']} — {apply_url[:60]}")
        except Exception as e:
            msg = str(e)
            nav_markers = ("execution context was destroyed", "cannot find context",
                           "target closed", "frame was detached", "navigating and changing")
            if any(m in msg.lower() for m in nav_markers):
                # The page redirected mid-fill (common on aggregator listings that
                # bounce to an external apply site). Not a hard failure — flag for
                # review/manual rather than a dead 'failed'.
                result["status"] = "needs_review"
                result["notes"]  = ("Page navigated/redirected mid-submit (likely an "
                                    "aggregator → external apply hop). Apply directly.")
                log.warning(f"↪  Redirected mid-submit (flagged review): {app['company']} — {apply_url[:60]}")
            else:
                result["status"] = "failed"
                result["notes"] = msg
                log.error(f"Submit error [{app['company']}]: {e}")
        finally:
            await browser.close()

    # Pass to Agent 4
    _write_agent4_result(result)

    return result

# ─────────────────────────────────────────────────────────────────────────────
# QUEUE RUNNER
# ─────────────────────────────────────────────────────────────────────────────
async def run_from_queue():
    queue_path = Path("outputs/agent3_queue.jsonl")
    if not queue_path.exists():
        log.info("No queue yet."); return

    lines = queue_path.read_text().strip().splitlines()
    if not lines:
        log.info("Queue empty."); return

    # Only skip jobs that ALREADY SUCCEEDED — re-submitting a 'submitted' job would
    # create a duplicate real application. needs_review / needs_manual / failed are
    # allowed to be re-attempted (e.g. after a form-filler improvement).
    done_status = {}   # job_id -> latest status (last line wins)
    for submitted_path in [
        Path("outputs/agent4_submitted_history.jsonl"),
        Path("outputs/agent4_submitted.bak"),
        Path("outputs/agent4_submitted.jsonl"),
    ]:
        if submitted_path.exists():
            for done_line in submitted_path.read_text(errors="ignore").splitlines():
                if not done_line.strip():
                    continue
                try:
                    done = json.loads(done_line)
                    jid = done.get("job_id") or done.get("id")
                    if jid:
                        done_status[jid] = done.get("status", "")
                except Exception:
                    pass

    log.info(f"Processing {len(lines)} applications...")
    results = []
    for line in lines:
        app = json.loads(line)
        if done_status.get(app.get("id")) == "submitted" and not app.get("_force_retry"):
            log.info(f"Skipping (already submitted): {app.get('title')} @ {app.get('company')}")
            continue
        result = await submit_application(app)
        results.append(result)
        await asyncio.sleep(5)   # polite delay between submissions

    # Summary
    submitted = [r for r in results if r["status"] == "submitted"]
    review    = [r for r in results if r["status"] == "needs_review"]
    failed    = [r for r in results if r["status"] == "failed"]
    log.info(f"Done — Submitted: {len(submitted)} | Review: {len(review)} | Failed: {len(failed)}")

    # Clear queue
    queue_path.write_text("")

if __name__ == "__main__":
    asyncio.run(run_from_queue())
