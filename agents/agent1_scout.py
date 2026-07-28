"""
AGENT 1 — JOB SCOUT (11-source pipeline)

Sources:
  FREE (no key):
    1. Greenhouse ATS   — 15 top-tier companies, full descriptions
    2. Ashby ATS        — 15 modern startups (Linear, Notion, Supabase…)
    3. Remotive         — 2,000+ remote software jobs
    4. RemoteOK         — 95+ remote jobs, well-tagged
    5. HN Who Is Hiring — monthly thread, founder-posted roles
    6. Himalayas        — 100K+ remote jobs with full descriptions
    7. Jobicy           — Ireland/EU-filtered roles
    8. Workable         — Dublin + Ireland startup board
    9. CareerJet        — 500+ Dublin tech jobs aggregated from all boards

  FREE (API key required — register once):
   10. Adzuna IE        — 50K+ Irish jobs (developer.adzuna.com — free 250/mo)
   11. Reed.co.uk       — UK+IE jobs (reed.co.uk/developers — free 500/mo)
   12. Jooble           — global aggregator (jooble.org/api — free)
   13. The Muse         — US/global roles (themuse.com/developers — free)

  PAID (Apify, ~$0.001–$0.005/result):
   14. LinkedIn (Apify) — full depth, EU remote
   15. Indeed (Apify)   — Dublin + Ireland
   16. IrishJobs.ie     — Ireland-specific job board
   17. Glassdoor        — salary + company insight
   18. Google Jobs      — aggregates everything
   19. Wellfound        — startup equity roles

  PAID (RapidAPI LinkedIn):
   20. RapidAPI LinkedIn — LinkedIn search API (reset monthly)

Daily budget target: <$0.25 Apify + <$0.05 Claude = ~$0.30/day
"""

import asyncio, base64, hashlib, html, json, logging, os, random, re
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
import httpx
import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

from config.profile import PROFILE, SEARCH_QUERIES, GREENHOUSE_SLUGS, LEVER_SLUGS, ASHBY_SLUGS
from core.gmail_token import get_gmail_token

log = logging.getLogger("agent1_scout")
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [SCOUT] %(message)s",
    handlers=[logging.FileHandler("logs/agent1.log"), logging.StreamHandler()])

RAPIDAPI_KEY  = os.getenv("RAPIDAPI_KEY",  "")
APIFY_TOKEN   = os.getenv("APIFY_API_TOKEN", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY= os.getenv("ADZUNA_APP_KEY", "")
REED_API_KEY  = os.getenv("REED_API_KEY", "")
JOOBLE_API_KEY= os.getenv("JOOBLE_API_KEY", "")
MUSE_API_KEY  = os.getenv("MUSE_API_KEY", "")   # optional, improves filtering
claude        = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── Cost controls ─────────────────────────────────────────────────────────────
RAPIDAPI_CALLS_PER_DAY = 20
APIFY_MAX_RESULTS      = 50
CLAUDE_SCORE_BATCH     = 50      # score top 50 with Claude; rest get keyword-score

# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Job:
    id:          str
    title:       str
    company:     str
    location:    str
    url:         str
    posted_at:   str
    description: str
    source:      str
    score:       int  = 0
    verdict:     str  = "pending"
    strengths:   list = field(default_factory=list)
    gaps:        list = field(default_factory=list)
    reason:      str  = ""
    date_found:  str  = field(default_factory=lambda: datetime.now().isoformat())

# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 1 — GREENHOUSE + ASHBY + LEVER (FREE, always run first)
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_greenhouse(slug: str) -> list[tuple]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                params={"content": "true"}
            )
            if r.status_code != 200:
                return []
            jobs = r.json().get("jobs", [])
            for j in jobs:
                j["_company_slug"] = slug  # inject so normaliser uses real company name
            return [(j, "greenhouse") for j in jobs if isinstance(j, dict)]
    except Exception as e:
        log.warning(f"Greenhouse [{slug}]: {e}"); return []

async def fetch_ashby(slug: str) -> list[tuple]:
    """Ashby ATS — used by Linear, Notion, Supabase, Sentry, Grafana, etc."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            if r.status_code != 200:
                return []
            jobs = r.json().get("jobs", [])
            # Inject company slug so normaliser can use it
            for j in jobs:
                j["_company_slug"] = slug
            return [(j, "ashby") for j in jobs if isinstance(j, dict)]
    except Exception as e:
        log.warning(f"Ashby [{slug}]: {e}"); return []

async def fetch_lever(slug: str) -> list[tuple]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://api.lever.co/v0/postings/{slug}",
                            params={"mode": "json"})
            if r.status_code != 200:
                return []
            data = r.json()
            if not isinstance(data, list):
                return []
            return [(j, "lever") for j in data if isinstance(j, dict)]
    except Exception as e:
        log.warning(f"Lever [{slug}]: {e}"); return []

async def fetch_all_ats() -> list[tuple]:
    """Fetch all Greenhouse + Ashby + Lever — completely free."""
    tasks = (
        [fetch_greenhouse(s) for s in GREENHOUSE_SLUGS] +
        [fetch_ashby(s)      for s in ASHBY_SLUGS]      +
        [fetch_lever(s)      for s in LEVER_SLUGS]
    )
    results = await asyncio.gather(*tasks)
    flat = [item for batch in results for item in batch]
    log.info(f"ATS (Greenhouse+Ashby+Lever): {len(flat)} raw listings")
    return flat

# ─────────────────────────────────────────────────────────────────────────────
# FREE SOURCES — NO API KEY REQUIRED
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_remotive() -> list[tuple]:
    """Remotive — 2,000+ curated remote software jobs. Free, no key."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://remotive.com/api/remote-jobs",
                            params={"category": "software-dev"})
            if r.status_code != 200:
                return []
            jobs = r.json().get("jobs", [])
            log.info(f"Remotive: {len(jobs)} jobs")
            return [(j, "remotive") for j in jobs if isinstance(j, dict)]
    except Exception as e:
        log.warning(f"Remotive failed: {e}"); return []

async def fetch_remoteok() -> list[tuple]:
    """RemoteOK — 95+ remote dev jobs, well-tagged. Free, no key."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://remoteok.com/api",
                headers={"User-Agent": "curl/7.88"},
            )
            if r.status_code != 200:
                return []
            data = r.json()
            # First item is a legal notice dict, skip it
            jobs = [j for j in data if isinstance(j, dict) and j.get("position")]
            log.info(f"RemoteOK: {len(jobs)} jobs")
            return [(j, "remoteok") for j in jobs]
    except Exception as e:
        log.warning(f"RemoteOK failed: {e}"); return []

async def fetch_hn_who_is_hiring() -> list[tuple]:
    """
    Hacker News 'Who Is Hiring' monthly thread.
    High signal — founders posting directly, often remote-friendly.
    Parses current month's thread via Algolia API.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            # Find the latest thread
            now = datetime.now()
            month_year = now.strftime("%B %Y")
            r = await c.get(
                "https://hn.algolia.com/api/v1/search",
                params={"tags": "ask_hn",
                        "query": f"Who is hiring {month_year}",
                        "hitsPerPage": 3},
            )
            hits = r.json().get("hits", [])
            if not hits:
                return []
            thread_id = hits[0]["objectID"]

            # Fetch comments (max 200 — free, instant)
            r2 = await c.get(
                "https://hn.algolia.com/api/v1/search",
                params={"tags": f"comment,story_{thread_id}", "hitsPerPage": 200},
            )
            comments = r2.json().get("hits", [])
            jobs = []
            for c_item in comments:
                text = c_item.get("comment_text", "")
                if not text or len(text) < 50:
                    continue
                # Strip HTML
                clean = re.sub(r"<[^>]+>", " ", text).strip()
                # Build a synthetic job dict
                jobs.append({
                    "_raw_text": clean,
                    "_hn_id":   c_item.get("objectID", ""),
                    "_url":     f"https://news.ycombinator.com/item?id={c_item.get('objectID','')}",
                })
            log.info(f"HN Who Is Hiring ({month_year}): {len(jobs)} comments")
            return [(j, "hn_hiring") for j in jobs]
    except Exception as e:
        log.warning(f"HN Who Is Hiring failed: {e}"); return []

async def fetch_himalayas() -> list[tuple]:
    """Himalayas — 100K+ remote jobs with full descriptions. Free, no key."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://himalayas.app/jobs/api",
                params={"q": "software engineer backend frontend fullstack ML",
                        "limit": 100},
            )
            if r.status_code != 200:
                return []
            jobs = r.json().get("jobs", [])
            log.info(f"Himalayas: {len(jobs)} jobs")
            return [(j, "himalayas") for j in jobs if isinstance(j, dict)]
    except Exception as e:
        log.warning(f"Himalayas failed: {e}"); return []

async def fetch_jobicy() -> list[tuple]:
    """Jobicy — Ireland/EU-filtered remote roles. Free, no key."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            results = []
            # Ireland-specific
            r = await c.get("https://jobicy.com/api/v2/remote-jobs",
                            params={"geo": "ireland", "count": 50, "industry": "engineering"})
            if r.status_code == 200:
                results.extend(r.json().get("jobs", []))
            # Remote engineering
            r2 = await c.get("https://jobicy.com/api/v2/remote-jobs",
                             params={"count": 50, "industry": "engineering"})
            if r2.status_code == 200:
                results.extend(r2.json().get("jobs", []))
            log.info(f"Jobicy: {len(results)} jobs")
            return [(j, "jobicy") for j in results if isinstance(j, dict)]
    except Exception as e:
        log.warning(f"Jobicy failed: {e}"); return []

async def fetch_workable() -> list[tuple]:
    """Workable global job board — Dublin + Ireland + Remote. Free, no key."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            results = []
            for query, loc in [
                ("software engineer", "Dublin, Ireland"),
                ("backend engineer",  "Ireland"),
                ("full stack",        "Ireland"),
                ("machine learning",  "Ireland"),
            ]:
                r = await c.get(
                    "https://jobs.workable.com/api/v1/jobs",
                    params={"query": query, "location": loc},
                )
                if r.status_code == 200:
                    results.extend(r.json().get("jobs", []))
            log.info(f"Workable: {len(results)} jobs")
            return [(j, "workable") for j in results if isinstance(j, dict)]
    except Exception as e:
        log.warning(f"Workable failed: {e}"); return []

async def fetch_careerjet() -> list[tuple]:
    """
    CareerJet — aggregates 584+ Dublin tech jobs from all Irish job boards.
    Free, no key required — just needs a Referer header.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            results = []
            searches = [
                ("software engineer dublin ireland", "Dublin"),
                ("backend developer ireland",        "Dublin"),
                ("full stack engineer ireland",      "Dublin"),
                ("machine learning engineer ireland","Dublin"),
            ]
            for keywords, location in searches:
                r = await c.get(
                    "http://public.api.careerjet.net/search",
                    params={
                        "keywords":   keywords,
                        "location":   location,
                        "affid":      "0",
                        "user_ip":    "127.0.0.1",
                        "url":        "https://example.com",
                        "user_agent": "Mozilla/5.0",
                        "pagesize":   25,
                    },
                    headers={"Referer": "https://example.com"},
                )
                if r.status_code == 200:
                    results.extend(r.json().get("jobs", []))
            log.info(f"CareerJet: {len(results)} jobs")
            return [(j, "careerjet") for j in results if isinstance(j, dict)]
    except Exception as e:
        log.warning(f"CareerJet failed: {e}"); return []

# ─────────────────────────────────────────────────────────────────────────────
# FREE SOURCES — API KEY REQUIRED (free tier, register once)
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_adzuna() -> list[tuple]:
    """
    Adzuna Ireland — 50K+ Irish jobs. Free tier: 250 calls/month.
    Register at: https://developer.adzuna.com/
    Add to .env: ADZUNA_APP_ID=... ADZUNA_APP_KEY=...
    """
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            results = []
            # Adzuna gb = UK (no Ireland endpoint in free tier)
            # Use remote + UK to catch roles open to Ireland/EU candidates
            for query in ["software engineer remote", "backend engineer remote",
                          "full stack engineer", "machine learning engineer"]:
                r = await c.get(
                    "https://api.adzuna.com/v1/api/jobs/gb/search/1",
                    params={
                        "app_id":           ADZUNA_APP_ID,
                        "app_key":          ADZUNA_APP_KEY,
                        "results_per_page": 25,
                        "what":             query,
                        "sort_by":          "date",
                        "max_days_old":     7,
                    },
                )
                if r.status_code == 200:
                    results.extend(r.json().get("results", []))
            log.info(f"Adzuna: {len(results)} jobs")
            return [(j, "adzuna") for j in results if isinstance(j, dict)]
    except Exception as e:
        log.warning(f"Adzuna failed: {e}"); return []

async def fetch_reed() -> list[tuple]:
    """
    Reed.co.uk — UK + Ireland jobs. Free tier: 500 calls/month.
    Register at: https://www.reed.co.uk/developers/jobseeker
    Add to .env: REED_API_KEY=...
    """
    if not REED_API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            results = []
            creds = base64.b64encode(f"{REED_API_KEY}:".encode()).decode()
            for keywords in ["software engineer", "backend developer", "full stack", "machine learning"]:
                r = await c.get(
                    "https://www.reed.co.uk/api/1.0/search",
                    params={
                        "keywords":      keywords,
                        "location":      "dublin",
                        "distancefromlocation": 15,
                        "resultsToTake": 25,
                        "postedByRecruitmentAgency": "false",
                    },
                    headers={"Authorization": f"Basic {creds}"},
                )
                if r.status_code == 200:
                    results.extend(r.json().get("results", []))
            log.info(f"Reed.co.uk: {len(results)} jobs")
            return [(j, "reed") for j in results if isinstance(j, dict)]
    except Exception as e:
        log.warning(f"Reed.co.uk failed: {e}"); return []

async def fetch_jooble() -> list[tuple]:
    """
    Jooble — global job aggregator. Free API key.
    Register at: https://jooble.org/api/about
    Add to .env: JOOBLE_API_KEY=...
    """
    if not JOOBLE_API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            results = []
            for keywords in ["software engineer", "backend developer", "full stack engineer", "ML engineer"]:
                r = await c.post(
                    f"https://jooble.org/api/{JOOBLE_API_KEY}",
                    json={"keywords": keywords, "location": "Dublin, Ireland"},
                    headers={"Content-Type": "application/json"},
                )
                if r.status_code == 200:
                    results.extend(r.json().get("jobs", []))
            log.info(f"Jooble: {len(results)} jobs")
            return [(j, "jooble") for j in results if isinstance(j, dict)]
    except Exception as e:
        log.warning(f"Jooble failed: {e}"); return []

async def fetch_the_muse() -> list[tuple]:
    """
    The Muse — global tech jobs, company culture focus. Free API (key optional).
    Optional key at: https://www.themuse.com/developers/api/v2
    Add to .env: MUSE_API_KEY=...  (improves rate limits, not required)
    """
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            params: dict = {"descending": "true", "page": "0"}
            if MUSE_API_KEY:
                params["api_key"] = MUSE_API_KEY
            r = await c.get("https://www.themuse.com/api/public/jobs", params=params)
            if r.status_code != 200:
                return []
            results = r.json().get("results", [])
            # Post-filter for tech roles since The Muse has no category filter
            tech_kw = ["engineer", "developer", "software", "backend", "frontend",
                       "fullstack", "full-stack", "machine learning", "ml", "devops", "data"]
            tech_jobs = [j for j in results
                         if any(k in j.get("name", "").lower() for k in tech_kw)]
            log.info(f"The Muse: {len(tech_jobs)} tech jobs (from {len(results)} total)")
            return [(j, "the_muse") for j in tech_jobs if isinstance(j, dict)]
    except Exception as e:
        log.warning(f"The Muse failed: {e}"); return []

# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 2 — RAPIDAPI LINKEDIN (paid, use sparingly)
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_rapidapi(query: str, location: str) -> list[tuple]:
    """
    RapidAPI — linkedin-job-search-api.p.rapidapi.com
    Endpoint: /active-jb-7d  (past 7 days, higher volume than 24h)
    """
    if not RAPIDAPI_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                "https://linkedin-job-search-api.p.rapidapi.com/active-jb-7d",
                headers={
                    "X-RapidAPI-Key":  RAPIDAPI_KEY,
                    "X-RapidAPI-Host": "linkedin-job-search-api.p.rapidapi.com",
                },
                params={
                    "title_filter":    query,
                    "location_filter": location,
                    "count":           "15",
                }
            )
            if r.status_code == 429:
                log.warning("RapidAPI: monthly quota exceeded — skipping LinkedIn source this run")
                return []
            if r.status_code != 200:
                log.warning(f"RapidAPI [{query}] HTTP {r.status_code}: {r.text[:100]}")
                return []
            data = r.json()
            jobs = data if isinstance(data, list) else data.get("jobs", [])
            return [(j, "linkedin_rapid") for j in jobs if isinstance(j, dict)]
    except Exception as e:
        log.warning(f"RapidAPI [{query}]: {e}"); return []

async def fetch_rapidapi_top_queries() -> list[tuple]:
    """
    Only run the 4 highest-priority queries through RapidAPI
    to stay well inside the free tier (20 calls/day budget).
    """
    if not RAPIDAPI_KEY:
        log.info("RapidAPI key not set — skipping"); return []

    priority_queries = [
        ("Software Engineer",     "Dublin, Ireland"),
        ("Full Stack Engineer",   "Dublin, Ireland"),
        ("Backend Engineer",      "Dublin, Ireland"),
        ("AI ML Engineer",        "Dublin, Ireland"),
    ]
    tasks   = [fetch_rapidapi(q, l) for q, l in priority_queries]
    results = await asyncio.gather(*tasks)
    flat    = [item for batch in results for item in batch]
    log.info(f"RapidAPI LinkedIn: {len(flat)} listings ({len(priority_queries)} queries)")
    return flat

# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 2.5 — LINKEDIN JOB-ALERT EMAILS (free, captures the *personalised* stream)
# ─────────────────────────────────────────────────────────────────────────────
# Unlike the RapidAPI/Apify LinkedIn sources (anonymous keyword searches against
# the public board), this reads the job-alert / recommended-job emails LinkedIn
# sends to YOUR inbox — i.e. the same notifications you see in-app, tuned to your
# profile and saved searches. We extract job IDs from those emails, then enrich
# each via the logged-out guest endpoint to get a clean title/company/description.
LINKEDIN_EMAIL_SENDERS = [
    "jobalerts-noreply@linkedin.com",   # "Software Engineer at X", digest alerts
    "jobs-noreply@linkedin.com",        # "X is hiring for a Y role"
    "jobs-listings@linkedin.com",       # recommended-jobs listings
]
LINKEDIN_EMAIL_LOOKBACK_DAYS = 7        # match the rest of the pipeline's window
LINKEDIN_EMAIL_MAX_MESSAGES  = 40       # emails to scan per run
LINKEDIN_EMAIL_MAX_JOBS      = 80       # cap detail fetches (politeness + speed)
LINKEDIN_DETAIL_CONCURRENCY  = 3        # concurrent guest-page fetches (avoid 429s)
LINKEDIN_DETAIL_PACING       = 0.4      # seconds to wait before each guest fetch
LINKEDIN_DETAIL_RETRIES      = 2        # retries on HTTP 429 (rate limit)
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def _walk_html_part(payload: dict) -> str | None:
    """Depth-first search of a Gmail message payload for the text/html body."""
    if payload.get("mimeType") == "text/html" and payload.get("body", {}).get("data"):
        return payload["body"]["data"]
    for part in payload.get("parts", []):
        got = _walk_html_part(part)
        if got:
            return got
    return None

def _parse_email_job_cards(html_body: str) -> dict[str, dict]:
    """
    Extract job cards from a LinkedIn alert email's HTML.
    Returns {job_id: {"location": str, "card_text": str}} — fallback metadata
    used only if the guest-page enrichment fails. Job IDs are the reliable bit.
    """
    cards: dict[str, dict] = {}
    # Linearise: replace each job-view anchor's opening tag with a marker so the
    # readable card text (TITLE COMPANY · LOCATION (MODE) ...) follows the marker.
    marked = re.sub(
        r'<a\b[^>]*href="https://www\.linkedin\.com/comm/jobs/view/(\d+)[^"]*"[^>]*>',
        r' @@JOB:\1@@ ', html_body)
    text = html.unescape(re.sub(r'<[^>]+>', ' ', marked))
    for seg in text.split('@@JOB:')[1:]:
        m = re.match(r'(\d+)@@(.*)', seg, re.S)
        if not m:
            continue
        jid, after = m.group(1), re.sub(r'\s+', ' ', m.group(2)).strip()
        if not after:                       # image/logo anchors render empty
            continue
        # Location sits between "·" and the next of (mode), salary, status, "Apply".
        loc = ""
        lm = re.search(r'·\s*([^·]+?)\s*(?:\(|€|\$|£|Actively recruiting|Apply|$)', after)
        if lm:
            loc = lm.group(1).strip()
        prev = cards.get(jid, {})
        # Keep the richest card_text seen for this id.
        if len(after) > len(prev.get("card_text", "")):
            cards[jid] = {"location": loc or prev.get("location", ""), "card_text": after}
        elif jid not in cards:
            cards[jid] = {"location": loc, "card_text": after}
    return cards

async def _fetch_linkedin_job_detail(client: httpx.AsyncClient, jid: str,
                                     sem: asyncio.Semaphore, fallback: dict) -> dict | None:
    """
    Enrich one job ID via LinkedIn's logged-out guest posting endpoint.
    Returns a raw dict ready for normalise(), or None if the posting is gone.
    """
    async with sem:
        try:
            t = ""
            for attempt in range(LINKEDIN_DETAIL_RETRIES + 1):
                # Gentle pacing + jitter so we don't trip LinkedIn's guest rate limit.
                await asyncio.sleep(LINKEDIN_DETAIL_PACING + random.uniform(0, 0.3))
                r = await client.get(
                    f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}",
                    headers={"User-Agent": _BROWSER_UA}, timeout=25, follow_redirects=True,
                )
                if r.status_code == 429 and attempt < LINKEDIN_DETAIL_RETRIES:
                    await asyncio.sleep(1.5 * (attempt + 1))   # exponential backoff
                    continue
                if r.status_code != 200 or not r.text:
                    raise ValueError(f"HTTP {r.status_code}")
                t = r.text
                break
            if not t:
                raise ValueError("no body after retries")
            def grab(pattern):
                mo = re.search(pattern, t, re.S)
                return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', mo.group(1)))).strip() if mo else ""
            title    = grab(r'top-card-layout__title[^>]*>(.*?)</h2>')
            company  = grab(r'topcard__org-name-link[^>]*>(.*?)</a>') or grab(r'topcard__flavor[^>]*>(.*?)</span>')
            location = grab(r'topcard__flavor--bullet[^>]*>(.*?)</span>')
            dm = re.search(r'show-more-less-html__markup(.*?)</div>', t, re.S)
            desc = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', dm.group(1)))).strip() if dm else ""
        except Exception as e:
            log.debug(f"LinkedIn detail [{jid}] failed ({e}); using email fallback")
            title = company = location = desc = ""

    # Fall back to email-card hints when the guest page is unavailable (e.g. 429).
    if not location:
        location = fallback.get("location", "")
    if not title and not company:
        # Card text is "TITLE COMPANY · LOCATION (mode) … Apply"; keep the part
        # before "·" and strip trailing CTA/status noise so scoring sees a title.
        ct = fallback.get("card_text", "").split(" · ")[0]
        ct = re.sub(r'\s*(Actively recruiting|Easy Apply|Apply|Be an early applicant|Promoted).*$',
                    '', ct).strip()
        title = ct[:140]
    if not title and not desc:
        return None   # nothing usable — skip rather than emit a blank job
    return {
        "job_id": jid,
        "url": f"https://www.linkedin.com/jobs/view/{jid}",
        "title": title,
        "company": company,
        "location": location or "Ireland",
        "description": desc or fallback.get("card_text", ""),
    }

async def fetch_linkedin_alert_emails() -> list[tuple]:
    """
    Read LinkedIn job-alert emails from Gmail, extract job IDs, and enrich each
    via the guest posting endpoint. Reuses the same auto-refreshing Gmail token
    as Agents 4/5 — no extra auth.
    """
    try:
        token = get_gmail_token()
    except Exception as e:
        log.info(f"LinkedIn emails: no Gmail token ({e}) — skipping"); return []
    if not token:
        log.info("LinkedIn emails: Gmail token not set — skipping"); return []

    q = ("from:(" + " OR ".join(LINKEDIN_EMAIL_SENDERS) + ") "
         f"newer_than:{LINKEDIN_EMAIL_LOOKBACK_DAYS}d")
    cards: dict[str, dict] = {}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            lr = await c.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": q, "maxResults": LINKEDIN_EMAIL_MAX_MESSAGES},
            )
            if lr.status_code != 200:
                log.warning(f"LinkedIn emails: Gmail list HTTP {lr.status_code}: {lr.text[:120]}")
                return []
            msgs = lr.json().get("messages", [])
            for ref in msgs:
                mr = await c.get(
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{ref['id']}",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"format": "full"},
                )
                if mr.status_code != 200:
                    continue
                data = _walk_html_part(mr.json().get("payload", {}))
                if not data:
                    continue
                body = base64.urlsafe_b64decode(data).decode("utf-8", "replace")
                for jid, meta in _parse_email_job_cards(body).items():
                    if jid not in cards:        # first sighting wins (freshest email first)
                        cards[jid] = meta
    except Exception as e:
        log.warning(f"LinkedIn emails: Gmail read failed ({e})"); return []

    if not cards:
        log.info("LinkedIn emails: no job cards found in inbox"); return []

    job_ids = list(cards)[:LINKEDIN_EMAIL_MAX_JOBS]
    log.info(f"LinkedIn emails: {len(cards)} unique jobs in inbox, enriching {len(job_ids)}")

    sem = asyncio.Semaphore(LINKEDIN_DETAIL_CONCURRENCY)
    async with httpx.AsyncClient(timeout=30) as c:
        results = await asyncio.gather(
            *[_fetch_linkedin_job_detail(c, jid, sem, cards[jid]) for jid in job_ids]
        )
    jobs = [(r, "linkedin_email") for r in results if r]
    log.info(f"LinkedIn emails: {len(jobs)} jobs enriched and queued")
    return jobs

# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 3 — APIFY (paid per result, gap-filler for Indeed + broader LinkedIn)
# ─────────────────────────────────────────────────────────────────────────────
async def run_apify_actor(actor_id: str, run_input: dict) -> list[dict]:
    """
    Run an Apify actor and return results.
    Uses synchronous run endpoint — waits for completion (max 60s).
    """
    if not APIFY_TOKEN:
        return []
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            # Start the run
            r = await c.post(
                f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items",
                headers={"Authorization": f"Bearer {APIFY_TOKEN}"},
                params={"token": APIFY_TOKEN},
                json=run_input,
            )
            if r.status_code == 200:
                return r.json()
            log.warning(f"Apify [{actor_id}] status {r.status_code}: {r.text[:200]}")
            return []
    except Exception as e:
        log.warning(f"Apify [{actor_id}]: {e}"); return []

async def fetch_apify_indeed() -> list[tuple]:
    """
    Apify Indeed scraper — great for Dublin roles not on LinkedIn.
    Actor: kaix/indeed-scraper (Most Comprehensive)
    Cost: ~$0.50 per 1000 results → 50 results = $0.025
    """
    if not APIFY_TOKEN:
        return []

    results = await run_apify_actor(
        "kaix~indeed-scraper",
        {
            "location":  "Dublin, Ireland",
            "query":     "Software Engineer OR Full Stack Engineer OR Backend Engineer OR AI Engineer",
            "maxItems":  APIFY_MAX_RESULTS,
        }
    )
    jobs = [(r, "indeed_apify") for r in results]
    log.info(f"Apify Indeed: {len(jobs)} listings")
    return jobs

async def fetch_apify_linkedin() -> list[tuple]:
    """
    Apify LinkedIn Jobs scraper — EU remote roles not covered by Greenhouse.
    Actor: curious_coder/linkedin-jobs-scraper
    Cost: ~$1 per 1000 results → 50 results = $0.05
    """
    if not APIFY_TOKEN:
        return []

    results = await run_apify_actor(
        "curious_coder~linkedin-jobs-scraper",
        {
            "queries": [
                "Software Engineer Remote Europe",
                "Full Stack Engineer Remote",
                "AI ML Engineer Remote EU",
            ],
            "maxResults": APIFY_MAX_RESULTS,
            "datePosted": "Past Week",
        }
    )
    jobs = [(r, "linkedin_apify") for r in results]
    log.info(f"Apify LinkedIn: {len(jobs)} listings")
    return jobs

async def fetch_apify_glassdoor() -> list[tuple]:
    """
    Apify Glassdoor scraper — catches roles missed by LinkedIn/Indeed.
    Actor: valig/glassdoor-jobs-scraper  (Mon+Thu only to save credits)
    """
    if not APIFY_TOKEN:
        return []
    today = datetime.now().weekday()
    if today not in (0, 3):
        log.info("Glassdoor: skipped (runs Mon+Thu only)"); return []
    results = await run_apify_actor(
        "valig~glassdoor-jobs-scraper",
        {"keyword": "Software Engineer", "location": "Dublin, Ireland", "maxResults": 30}
    )
    jobs = [(r, "glassdoor_apify") for r in results]
    log.info(f"Apify Glassdoor: {len(jobs)} listings")
    return jobs

async def fetch_apify_irishjobs() -> list[tuple]:
    """
    IrishJobs.ie — Ireland's primary job board. ~$0.00145/job.
    Actor: unfenced-group/irishjobs-ie-scraper
    Only run Mon+Wed+Fri to manage costs.
    """
    if not APIFY_TOKEN:
        return []
    today = datetime.now().weekday()
    if today not in (0, 2, 4):
        log.info("IrishJobs: skipped (runs Mon/Wed/Fri only)"); return []
    results = await run_apify_actor(
        "unfenced-group~irishjobs-ie-scraper",
        {
            "searchTerm": "software engineer",
            "location":   "Dublin",
            "maxItems":   APIFY_MAX_RESULTS,
        }
    )
    jobs = [(r, "irishjobs_apify") for r in results if isinstance(r, dict)]
    log.info(f"Apify IrishJobs: {len(jobs)} listings")
    return jobs

async def fetch_apify_google_jobs() -> list[tuple]:
    """
    Google Jobs — aggregates ALL job boards in one query. Best single-query coverage.
    Actor: johnvc/Google-Jobs-Scraper   (~$0.002/result)
    Run daily for maximum coverage.
    """
    if not APIFY_TOKEN:
        return []
    results = await run_apify_actor(
        "johnvc~Google-Jobs-Scraper",
        {
            "query":    "software engineer OR backend developer OR full stack engineer Dublin Ireland",
            "maxItems": APIFY_MAX_RESULTS,
        }
    )
    jobs = [(r, "google_jobs_apify") for r in results if isinstance(r, dict)]
    log.info(f"Apify Google Jobs: {len(jobs)} listings")
    return jobs

async def fetch_apify_wellfound() -> list[tuple]:
    """
    Wellfound (AngelList) — startup equity roles, remote-friendly.
    Actor: crawlerbros/wellfound-scraper   (~$0.003/result)
    Run Mon+Thu to save credits.
    """
    if not APIFY_TOKEN:
        return []
    today = datetime.now().weekday()
    if today not in (0, 3):
        log.info("Wellfound: skipped (runs Mon+Thu only)"); return []
    results = await run_apify_actor(
        "crawlerbros~wellfound-scraper",
        {
            "role":     "software-engineer",
            "location": "Remote",
            "maxItems": APIFY_MAX_RESULTS,
        }
    )
    jobs = [(r, "wellfound_apify") for r in results if isinstance(r, dict)]
    log.info(f"Apify Wellfound: {len(jobs)} listings")
    return jobs

# ─────────────────────────────────────────────────────────────────────────────
# NORMALISER — handles all source formats
# ─────────────────────────────────────────────────────────────────────────────
def normalise(raw: dict, source: str) -> Job:
    if source == "greenhouse":
        title   = raw.get("title", "")
        # Use company slug injected at fetch time, fall back to department name
        company = raw.get("_company_slug", "") or \
                  (raw.get("departments") or [{}])[0].get("name", "Unknown")
        loc     = (raw.get("location") or {}).get("name", "")
        url     = raw.get("absolute_url", "")
        desc    = raw.get("content", "")
        posted  = raw.get("updated_at", "")

    elif source == "ashby":
        title   = raw.get("title", "")
        company = raw.get("_company_slug", "Unknown")
        addr    = raw.get("address") or {}
        loc     = addr.get("postalAddress", {}).get("addressLocality", "") or \
                  raw.get("location", "") or \
                  ("Remote" if raw.get("isRemote") else "")
        url     = raw.get("jobUrl", "") or raw.get("applyUrl", "")
        desc    = raw.get("descriptionPlain", "")
        posted  = raw.get("publishedAt", "")

    elif source == "lever":
        title   = raw.get("text", "")
        company = raw.get("company", "Unknown")
        loc     = (raw.get("categories") or {}).get("location", "")
        url     = raw.get("hostedUrl", "")
        desc    = raw.get("descriptionPlain", "")
        posted  = str(raw.get("createdAt", ""))

    elif source == "linkedin_rapid":
        # linkedin-job-search-api.p.rapidapi.com field names
        title   = raw.get("title", "")
        company = raw.get("organization", "") or raw.get("company", "")
        locs    = raw.get("locations_derived") or []
        loc     = locs[0] if locs else raw.get("location", "")
        url     = raw.get("url", "") or raw.get("external_apply_url", "")
        # No per-job description; use company description + title as proxy for scoring
        desc    = raw.get("linkedin_org_description", "")
        posted  = raw.get("date_posted", "")

    elif source == "linkedin_email":
        # Built by fetch_linkedin_alert_emails(): clean fields from the guest page
        title   = raw.get("title", "")
        company = raw.get("company", "")
        loc     = raw.get("location", "")
        url     = raw.get("url", "")
        desc    = raw.get("description", "")
        posted  = raw.get("posted_at", "")

    elif source == "indeed_apify":
        title   = raw.get("positionName", "") or raw.get("title", "")
        company = raw.get("company", "")
        loc     = raw.get("location", "")
        url     = raw.get("url", "") or raw.get("jobUrl", "")
        desc    = raw.get("description", "") or raw.get("jobDescription", "")
        posted  = raw.get("postedAt", "") or raw.get("datePosted", "")

    elif source in ("linkedin_apify", "glassdoor_apify"):
        title   = raw.get("title", "") or raw.get("jobTitle", "")
        company = raw.get("companyName", "") or raw.get("company", "")
        loc     = raw.get("location", "")
        url     = raw.get("jobUrl", "") or raw.get("url", "")
        desc    = raw.get("description", "") or raw.get("jobDescription", "")
        posted  = raw.get("postedDate", "") or raw.get("publishedAt", "")

    elif source == "irishjobs_apify":
        title   = raw.get("title", "") or raw.get("jobTitle", "")
        company = raw.get("company", "") or raw.get("companyName", "")
        loc     = raw.get("location", "Dublin, Ireland")
        url     = raw.get("url", "") or raw.get("jobUrl", "")
        desc    = raw.get("description", "") or raw.get("summary", "")
        posted  = raw.get("postedDate", "") or raw.get("datePosted", "")

    elif source == "google_jobs_apify":
        title   = raw.get("title", "") or raw.get("job_title", "")
        company = raw.get("company_name", "") or raw.get("company", "")
        loc     = raw.get("location", "") or raw.get("city", "")
        url     = raw.get("apply_link", "") or raw.get("url", "") or raw.get("job_url", "")
        desc    = raw.get("description", "") or raw.get("job_description", "")
        posted  = raw.get("posted_at", "") or raw.get("date_posted", "")

    elif source == "wellfound_apify":
        title   = raw.get("jobTitle", "") or raw.get("title", "")
        company = raw.get("companyName", "") or raw.get("company", "")
        loc     = raw.get("location", "Remote")
        url     = raw.get("jobUrl", "") or raw.get("url", "")
        desc    = raw.get("description", "") or raw.get("jobDescription", "")

    elif source == "remotive":
        title   = raw.get("title", "")
        company = raw.get("company_name", "")
        loc     = raw.get("candidate_required_location", "Remote")
        url     = raw.get("url", "")
        desc    = re.sub(r"<[^>]+>", " ", raw.get("description", ""))
        posted  = raw.get("publication_date", "")

    elif source == "remoteok":
        title   = raw.get("position", "")
        company = raw.get("company", "")
        loc     = raw.get("location", "Remote")
        url     = raw.get("apply_url", "") or raw.get("url", "")
        desc    = re.sub(r"<[^>]+>", " ", raw.get("description", ""))
        posted  = raw.get("date", "")

    elif source == "hn_hiring":
        # Raw text from HN comment — parse first line as title/company
        text  = raw.get("_raw_text", "")
        parts = text.split("|")
        company = parts[0].strip() if parts else "Unknown"
        title   = parts[1].strip() if len(parts) > 1 else text[:80]
        loc_raw = parts[2].strip() if len(parts) > 2 else "Remote"
        loc     = loc_raw
        url     = raw.get("_url", "")
        desc    = text
        posted  = ""

    elif source == "himalayas":
        title   = raw.get("title", "")
        company = raw.get("companyName", "")
        locs    = raw.get("locationRestrictions", [])
        loc     = ", ".join(locs) if locs else "Remote"
        url     = raw.get("applicationLink", "") or raw.get("guid", "")
        desc    = re.sub(r"<[^>]+>", " ", raw.get("description", "") or raw.get("excerpt", ""))
        posted  = raw.get("pubDate", "")

    elif source == "jobicy":
        title   = raw.get("jobTitle", "")
        company = raw.get("companyName", "")
        loc     = raw.get("jobGeo", "Remote")
        url     = raw.get("url", "")
        desc    = re.sub(r"<[^>]+>", " ", raw.get("jobDescription", "") or raw.get("jobExcerpt", ""))
        posted  = raw.get("pubDate", "")

    elif source == "workable":
        title   = raw.get("title", "")
        co      = raw.get("company") or {}
        company = co.get("title", "") if isinstance(co, dict) else str(co)
        loc_d   = raw.get("location") or {}
        loc     = loc_d.get("city", "") if isinstance(loc_d, dict) else str(loc_d)
        url     = raw.get("url", "")
        desc    = re.sub(r"<[^>]+>", " ", raw.get("description", ""))
        posted  = raw.get("created", "")

    elif source == "careerjet":
        title   = raw.get("title", "")
        company = raw.get("company", "")
        locs    = raw.get("locations", [])
        loc     = locs[0] if locs else "Dublin"
        url     = raw.get("url", "")
        desc    = re.sub(r"<[^>]+>", " ", raw.get("description", ""))
        posted  = raw.get("date", "")

    elif source == "adzuna":
        title   = raw.get("title", "")
        company = (raw.get("company") or {}).get("display_name", "")
        loc     = (raw.get("location") or {}).get("display_name", "")
        url     = raw.get("redirect_url", "")
        desc    = raw.get("description", "")
        posted  = raw.get("created", "")

    elif source == "reed":
        title   = raw.get("jobTitle", "")
        company = raw.get("employerName", "")
        loc     = raw.get("locationName", "")
        url     = raw.get("jobUrl", "")
        desc    = raw.get("jobDescription", "") or raw.get("summary", "")
        posted  = raw.get("datePosted", "") or raw.get("expirationDate", "")

    elif source == "jooble":
        title   = raw.get("title", "")
        company = raw.get("company", "")
        loc     = raw.get("location", "")
        url     = raw.get("link", "")
        desc    = raw.get("snippet", "")
        posted  = raw.get("updated", "")

    elif source == "the_muse":
        title   = raw.get("name", "")
        company = raw.get("company", {}).get("name", "") if isinstance(raw.get("company"), dict) else ""
        locs    = raw.get("locations", [])
        loc     = locs[0].get("name", "") if locs else "Remote"
        url     = raw.get("refs", {}).get("landing_page", "")
        desc    = raw.get("contents", "")
        posted  = raw.get("publication_date", "")

    else:
        title = company = loc = url = desc = posted = ""

    uid = hashlib.md5(f"{company}{title}{loc}".lower().encode()).hexdigest()[:12]
    return Job(id=uid, title=title, company=company, location=loc,
               url=url, posted_at=str(posted), description=desc, source=source)

def deduplicate(jobs: list[Job]) -> list[Job]:
    seen = {}
    for j in jobs:
        if j.id not in seen:
            seen[j.id] = j
    return list(seen.values())

def quick_filter(job: Job) -> bool:
    """Pre-filter before Claude — saves API cost."""
    if not job.title or not job.url:
        return False
    t = (job.title or "").lower()
    c = (job.company or "").lower()
    d = (job.description or "").lower()
    if job.source == "hn_hiring":
        junk_phrases = [
            "i just applied", "i applied", "sent you an email", "linkedin note",
            "thanks for sharing", "thank you for sharing", "quick linkedin",
            "please email me", "dm me", "i'm interested"
        ]
        if any(p in d for p in junk_phrases) or any(p in c for p in junk_phrases):
            return False
        if len(job.company) > 80 or len(job.title) > 140:
            return False
    keep = ["engineer", "developer", "ml", "machine learning", "full stack",
            "fullstack", "backend", "frontend", "founding", "software", "ai "]
    drop = ["director", "vp ", "vice president", "chief", "head of product",
            "manager", "principal", "staff engineer", "senior staff",
            "data entry", "qa ", "quality assurance", "sales", "marketing"]
    return any(k in t for k in keep) and not any(d in t for d in drop)

# ─────────────────────────────────────────────────────────────────────────────
# CLAUDE SCORER — batched to minimise API calls
# ─────────────────────────────────────────────────────────────────────────────
def score_job_batch(jobs: list[Job]) -> list[dict]:
    """
    Score multiple jobs in ONE Claude API call.
    Dramatically cheaper than one call per job.
    """
    jobs_text = "\n\n".join(
        f"JOB_{i+1}:\nTitle: {j.title}\nCompany: {j.company}\n"
        f"Location: {j.location}\nDesc: {j.description[:800]}"
        for i, j in enumerate(jobs)
    )

    skills = (PROFILE["skills"]["languages"] +
              PROFILE["skills"]["frameworks"][:5] +
              PROFILE["skills"]["ml_ai"][:4] +
              PROFILE["skills"]["devops"][:3])

    prompt = f"""Score these {len(jobs)} jobs for the candidate. Return a JSON array only.

CANDIDATE:
Name: {PROFILE['name']} | {PROFILE['location']} | {PROFILE['visa']}
Targets: {', '.join(PROFILE['target_roles'])}
Salary: €{PROFILE['salary_min_eur']:,}–€{PROFILE['salary_max_eur']:,}
Skills: {', '.join(skills)}
Experience: Founding Engineer at fintech startup (full-stack, 35+ APIs, sub-200ms);
  2yr Software Developer at Bajaj Finserv (16% throughput boost, Azure, Docker);
  MSc CS Distinction (1:1) Trinity College Dublin

{jobs_text}

Return JSON array with one object per job, in order:
[
  {{"job": "JOB_1", "score": 0-100, "verdict": "apply|maybe|skip",
    "strengths": ["max 2"], "gaps": ["max 1"], "reason": "one sentence"}},
  ...
]
No markdown, no preamble. Array only."""

    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",   # Haiku = cheapest, fast, great for scoring
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(raw)

def score_all_jobs(jobs: list[Job]) -> list[Job]:
    """
    Score in batches of 10 — balances cost vs context window.
    Uses claude-haiku (cheapest model) for scoring only.
    Agents 2+3+4+5 continue to use claude-sonnet for quality.
    """
    batch_size = 10
    for i in range(0, min(len(jobs), CLAUDE_SCORE_BATCH), batch_size):
        batch = jobs[i:i + batch_size]
        try:
            scores = score_job_batch(batch)
            for job, s in zip(batch, scores):
                job.score     = s.get("score", 0)
                job.verdict   = s.get("verdict", "skip")
                job.strengths = s.get("strengths", [])
                job.gaps      = s.get("gaps", [])
                job.reason    = s.get("reason", "")
        except Exception as e:
            log.warning(f"Batch score failed (jobs {i}–{i+batch_size}): {e}")
            for job in batch:
                job.score = 0; job.verdict = "skip"

    # Jobs beyond CLAUDE_SCORE_BATCH get a quick keyword score (free)
    for job in jobs[CLAUDE_SCORE_BATCH:]:
        job.score   = keyword_score(job)
        job.verdict = "maybe" if job.score >= 50 else "skip"
        job.reason  = "keyword-scored (beyond daily Claude batch)"

    return jobs

def keyword_score(job: Job) -> int:
    """
    Free fallback scorer — keyword matching only.
    Used for jobs beyond the daily Claude batch limit.
    """
    desc  = ((job.description or "") + " " + (job.title or "")).lower()
    loc   = (job.location or "").lower()
    score = 40   # base
    high_value = ["next.js", "nestjs", "node.js", "react", "postgresql",
                  "typescript", "python", "fastapi", "docker", "aws", "gcp",
                  "machine learning", "pytorch", "tensorflow"]
    for kw in high_value:
        if kw in desc: score += 4
    if "fintech" in desc or "startup" in desc: score += 8
    if "dublin" in loc:                        score += 5
    if "remote" in loc:                        score += 3
    return min(score, 85)

# ─────────────────────────────────────────────────────────────────────────────
# DISPATCH + DIGEST
# ─────────────────────────────────────────────────────────────────────────────
async def dispatch_to_agent2(jobs: list[Job]):
    Path("outputs").mkdir(exist_ok=True)
    with open("outputs/agent2_queue.jsonl", "a") as f:
        for job in jobs:
            f.write(json.dumps(asdict(job)) + "\n")
    log.info(f"→ Agent 2 queue: {len(jobs)} jobs")

async def dispatch_to_tracker(jobs: list[Job]):
    Path("outputs").mkdir(exist_ok=True)
    with open("outputs/tracker_queue.jsonl", "a") as f:
        for job in jobs:
            f.write(json.dumps(asdict(job)) + "\n")
    log.info(f"→ Agent 4 tracker: {len(jobs)} jobs")

async def send_digest(hot: list[Job], total: int, sources: dict):
    lines = [
        f"Job Scout — {datetime.now().strftime('%d %b %Y')}",
        f"Total scanned: {total} | Strong matches: {len(hot)}",
        f"Sources: " + " | ".join(f"{k}: {v}" for k, v in sources.items()),
        "",
    ]
    for j in sorted(hot, key=lambda x: x.score, reverse=True)[:15]:
        lines.append(f"[{j.score}/100] {j.title} @ {j.company} ({j.location})")
        lines.append(f"  {j.reason}")
        lines.append(f"  {j.url}\n")
    digest = "\n".join(lines)
    log.info("\n" + digest)
    Path("outputs").mkdir(exist_ok=True)
    with open(f"outputs/digest_{datetime.now().strftime('%Y%m%d')}.txt", "w") as f:
        f.write(digest)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN RUN
# ─────────────────────────────────────────────────────────────────────────────
async def run_scout():
    log.info("════ Agent 1: Job Scout (20-source) ════")
    Path("logs").mkdir(exist_ok=True)

    # ── Tier 1: Free ATS APIs (Greenhouse + Ashby + Lever) ───────────────────
    ats_raw = await fetch_all_ats()

    # ── Tier 2: Free job boards — run all concurrently ───────────────────────
    (remotive_raw, remoteok_raw, hn_raw, himalayas_raw,
     jobicy_raw, workable_raw, careerjet_raw) = await asyncio.gather(
        fetch_remotive(),
        fetch_remoteok(),
        fetch_hn_who_is_hiring(),
        fetch_himalayas(),
        fetch_jobicy(),
        fetch_workable(),
        fetch_careerjet(),
    )

    # ── Tier 3: Free job boards with API keys ────────────────────────────────
    (adzuna_raw, reed_raw, jooble_raw, muse_raw) = await asyncio.gather(
        fetch_adzuna(),
        fetch_reed(),
        fetch_jooble(),
        fetch_the_muse(),
    )

    # ── Tier 4: RapidAPI LinkedIn (paid, use sparingly) ──────────────────────
    rapid_raw = await fetch_rapidapi_top_queries()

    # ── Tier 4.5: LinkedIn job-alert emails (free, personalised stream) ───────
    li_email_raw = await fetch_linkedin_alert_emails()

    # ── Tier 5: Apify scrapers (paid per result) ─────────────────────────────
    (indeed_raw, linkedin_raw, glassdoor_raw,
     irishjobs_raw, google_raw, wellfound_raw) = await asyncio.gather(
        fetch_apify_indeed(),
        fetch_apify_linkedin(),
        fetch_apify_glassdoor(),
        fetch_apify_irishjobs(),
        fetch_apify_google_jobs(),
        fetch_apify_wellfound(),
    )

    all_raw = (ats_raw + remotive_raw + remoteok_raw + hn_raw +
               himalayas_raw + jobicy_raw + workable_raw + careerjet_raw +
               adzuna_raw + reed_raw + jooble_raw + muse_raw +
               rapid_raw + li_email_raw + indeed_raw + linkedin_raw + glassdoor_raw +
               irishjobs_raw + google_raw + wellfound_raw)

    sources = {
        "ATS":         len(ats_raw),
        "Remotive":    len(remotive_raw),
        "RemoteOK":    len(remoteok_raw),
        "HN":          len(hn_raw),
        "Himalayas":   len(himalayas_raw),
        "Jobicy":      len(jobicy_raw),
        "Workable":    len(workable_raw),
        "CareerJet":   len(careerjet_raw),
        "Adzuna":      len(adzuna_raw),
        "Reed":        len(reed_raw),
        "Jooble":      len(jooble_raw),
        "Muse":        len(muse_raw),
        "RapidAPI":    len(rapid_raw),
        "LinkedInMail":len(li_email_raw),
        "ApifyIndeed": len(indeed_raw),
        "ApifyLI":     len(linkedin_raw),
        "ApifyGD":     len(glassdoor_raw),
        "IrishJobs":   len(irishjobs_raw),
        "GoogleJobs":  len(google_raw),
        "Wellfound":   len(wellfound_raw),
    }
    log.info(f"Total raw: {len(all_raw)} | " + " | ".join(f"{k}:{v}" for k,v in sources.items()))

    # 2. Normalise + deduplicate
    jobs = deduplicate([normalise(r, src) for r, src in all_raw])
    log.info(f"After dedup: {len(jobs)}")

    # 3. Quick filter (free — no API calls)
    jobs = [j for j in jobs if quick_filter(j)]
    log.info(f"After filter: {len(jobs)}")

    # 4. Score with Claude (batched — cost optimised)
    jobs = score_all_jobs(jobs)

    # 5. Split and dispatch
    hot = [j for j in jobs if j.score >= PROFILE["score_threshold"]]
    log.info(f"Hot jobs (≥{PROFILE['score_threshold']}): {len(hot)}")

    # ── De-dupe per company: at most N distinct teams, best-scored variant ─────
    # Avoids spamming a company with many near-identical roles.
    try:
        from core.dedupe import dedupe_applications, summarise
        hot_dicts = [asdict(j) for j in hot]
        kept_dicts = dedupe_applications(hot_dicts,
                                         max_per_company=PROFILE.get("max_roles_per_company", 3))
        kept_ids = {k["id"] for k in kept_dicts}
        deduped = [j for j in hot if j.id in kept_ids]
        log.info("\n" + summarise(hot_dicts, kept_dicts))
        hot = deduped
        log.info(f"After per-company de-dupe: {len(hot)} applications")
    except Exception as e:
        log.warning(f"De-dupe skipped: {e}")

    await dispatch_to_agent2(hot)        # only the de-duped best roles get resumes
    await dispatch_to_tracker(jobs)      # tracker still logs everything scanned
    await send_digest(hot, len(jobs), sources)
    log.info("════ Agent 1 complete ════")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    if args.once:
        asyncio.run(run_scout())
    else:
        scheduler = AsyncIOScheduler(timezone="Europe/Dublin")
        scheduler.add_job(run_scout, "cron", hour=7, minute=0)
        scheduler.start()
        log.info("Agent 1 scheduled — 07:00 Dublin daily")
        try:
            asyncio.get_event_loop().run_forever()
        except (KeyboardInterrupt, SystemExit):
            pass
