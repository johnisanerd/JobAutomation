"""
AGENT 5 — RECRUITER SCOUT
For every submitted application:
1. Finds recruiters / hiring managers at that company via LinkedIn + Hunter.io/Apollo
2. Generates a personalised email for each contact using Claude
   (tone: concise + professional + bold with impact numbers)
3. Sends via Gmail API
4. Logs all outreach to the tracker
"""

import asyncio, json, logging, os, re
from datetime import datetime
from pathlib import Path
import httpx
import anthropic

from dotenv import load_dotenv
load_dotenv()

from config.profile import PROFILE

log = logging.getLogger("agent5_recruiter")
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [RECRUITER] %(message)s",
    handlers=[logging.FileHandler("logs/agent5.log"), logging.StreamHandler()])

ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY",  "YOUR_ANTHROPIC_KEY")
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY",     "YOUR_HUNTER_KEY")    # hunter.io
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY",      "YOUR_APOLLO_KEY")    # apollo.io (alternative)
from core.gmail_token import get_gmail_token
GMAIL_TOKEN = get_gmail_token()
RAPIDAPI_KEY   = os.getenv("RAPIDAPI_KEY",        "YOUR_RAPIDAPI_KEY")

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — FIND CONTACTS
# ─────────────────────────────────────────────────────────────────────────────
async def find_contacts_hunter(company_domain: str, role_title: str) -> list[dict]:
    """Search Hunter.io for recruiter/engineering contacts at the company."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://api.hunter.io/v2/domain-search",
                params={
                    "domain":   company_domain,
                    "api_key":  HUNTER_API_KEY,
                    "limit":    5,
                    "type":     "personal",
                }
            )
            data  = r.json().get("data", {})
            emails = data.get("emails", [])
            contacts = []
            for e in emails:
                dept = (e.get("department") or "").lower()
                pos  = (e.get("position") or "").lower()
                # Target: recruiters, talent, engineering, CTOs, founders
                relevant = any(kw in dept or kw in pos for kw in
                    ["recruit", "talent", "hr", "people", "engineer", "tech",
                     "cto", "founder", "head of eng", "vp eng", "hiring"])
                if relevant or not contacts:  # always include at least one
                    contacts.append({
                        "name":     f"{e.get('first_name','')} {e.get('last_name','')}".strip(),
                        "email":    e.get("value", ""),
                        "position": e.get("position", ""),
                        "linkedin": e.get("linkedin", ""),
                        "source":   "hunter",
                    })
            return contacts[:3]   # max 3 contacts per company
    except Exception as e:
        log.warning(f"Hunter failed [{company_domain}]: {e}"); return []

async def find_contacts_apollo(company_name: str, role_title: str) -> list[dict]:
    """Apollo.io people search as fallback."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                "https://api.apollo.io/v1/mixed_people/search",
                headers={"Content-Type": "application/json",
                         "Cache-Control": "no-cache",
                         "X-Api-Key": APOLLO_API_KEY},
                json={
                    "organization_names": [company_name],
                    "titles": ["recruiter", "talent", "engineering manager",
                               "head of engineering", "cto", "technical recruiter"],
                    "per_page": 3,
                }
            )
            people = r.json().get("people", [])
            return [{
                "name":     p.get("name", ""),
                "email":    p.get("email", ""),
                "position": p.get("title", ""),
                "linkedin": p.get("linkedin_url", ""),
                "source":   "apollo",
            } for p in people if p.get("email")]
    except Exception as e:
        log.warning(f"Apollo failed [{company_name}]: {e}"); return []

async def find_contacts_linkedin(company_name: str) -> list[dict]:
    """RapidAPI LinkedIn people search."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://linkedin-profiles-api.p.rapidapi.com/search-employees",
                headers={"X-RapidAPI-Key": RAPIDAPI_KEY,
                         "X-RapidAPI-Host": "linkedin-profiles-api.p.rapidapi.com"},
                params={"company": company_name,
                        "keywords": "recruiter OR talent OR engineering hiring",
                        "count": 5}
            )
            people = r.json().get("results", [])
            return [{
                "name":     p.get("fullName", ""),
                "email":    p.get("email", ""),
                "position": p.get("title", ""),
                "linkedin": p.get("profileUrl", ""),
                "source":   "linkedin",
            } for p in people]
    except Exception as e:
        log.warning(f"LinkedIn search failed [{company_name}]: {e}"); return []

def get_company_domain(company_name: str) -> str:
    """Best-guess domain from company name."""
    slug = company_name.lower()
    slug = re.sub(r"[^a-z0-9]", "", slug)
    common_overrides = {
        "eezpays": "eezpays.com", "bajajfinserv": "bajajfinserv.in",
        "stripe": "stripe.com", "intercom": "intercom.com",
        "revolut": "revolut.com", "hubspot": "hubspot.com",
        "notion": "notion.so", "figma": "figma.com",
        "vercel": "vercel.com", "supabase": "supabase.com",
    }
    return common_overrides.get(slug, f"{slug}.com")

async def find_contacts(company_name: str, role_title: str) -> list[dict]:
    """Try Hunter → Apollo → LinkedIn in order."""
    domain   = get_company_domain(company_name)
    contacts = await find_contacts_hunter(domain, role_title)
    if not contacts:
        contacts = await find_contacts_apollo(company_name, role_title)
    if not contacts:
        contacts = await find_contacts_linkedin(company_name)
    log.info(f"Found {len(contacts)} contacts at {company_name}")
    return contacts

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — GENERATE EMAIL WITH CLAUDE
# ─────────────────────────────────────────────────────────────────────────────
def generate_outreach_email(contact: dict, job: dict) -> dict:
    """
    Tone: Concise + professional + bold with impact numbers.
    Subject: short, specific, not generic.
    Body: 3 paragraphs max. Lead with a number. End with a clear ask.
    """
    first_name = contact["name"].split()[0] if contact["name"] else "there"

    prompt = f"""Write a cold outreach email from {PROFILE['name']} to {contact['name']} 
({contact['position']}) at {job['company']}.

PURPOSE: Follow up after applying for {job['title']} — build a human connection with the recruiter/hiring team.

TONE RULES (follow strictly):
- Concise: max 120 words in the body
- Professional but warm — not stiff
- Bold: open with a specific impact number or achievement
- End with ONE clear ask (15-min call or reply)
- Never say "I hope this email finds you well" or "I am writing to"
- No bullet points in the email itself

CANDIDATE FACTS TO USE (pick the 2 most relevant):
- Delivered full fintech MVP in under 5 months as sole technical owner
- 35+ production APIs, sub-200ms response times
- Trinity MSc Computer Science — Distinction (1:1)
- 16% throughput boost across 8 platforms at Bajaj Finserv
- GPT/ML projects, distributed systems on GCP
- Stamp 1G — no sponsorship needed

JOB CONTEXT:
Role: {job['title']} at {job['company']}
Job score/fit: {job.get('score', 'N/A')}/100
Why it fits: {job.get('reason', 'strong technical match')}

Return JSON only:
{{
  "subject": "short compelling subject line (max 8 words)",
  "body": "full email body — greeting to sign-off, plain text"
}}"""

    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = msg.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(raw)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — SEND VIA GMAIL
# ─────────────────────────────────────────────────────────────────────────────
async def send_gmail(to_email: str, subject: str, body: str) -> bool:
    """Send email via Gmail API."""
    import email.mime.text, email.mime.multipart
    msg = email.mime.multipart.MIMEMultipart()
    msg["To"]      = to_email
    msg["From"]    = PROFILE["email"]
    msg["Subject"] = subject
    msg.attach(email.mime.text.MIMEText(body, "plain"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                headers={"Authorization": f"Bearer {GMAIL_TOKEN}",
                         "Content-Type": "application/json"},
                json={"raw": raw}
            )
            if r.status_code == 200:
                log.info(f"Email sent → {to_email}")
                return True
            else:
                log.warning(f"Gmail send failed [{r.status_code}]: {r.text}")
                return False
    except Exception as e:
        log.error(f"Gmail error: {e}"); return False

import base64   # needed for send_gmail, import here to avoid top-level issue

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — LOG OUTREACH
# ─────────────────────────────────────────────────────────────────────────────
def log_outreach(job: dict, contact: dict, email_data: dict, sent: bool):
    record = {
        "job_id":      job["id"],
        "company":     job["company"],
        "role":        job["title"],
        "contact_name":  contact["name"],
        "contact_email": contact["email"],
        "contact_title": contact["position"],
        "contact_li":    contact.get("linkedin",""),
        "subject":     email_data.get("subject",""),
        "sent":        sent,
        "sent_at":     datetime.now().isoformat() if sent else "",
        "source":      contact.get("source",""),
    }
    with open("outputs/outreach_log.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")

    # Flag recruiter contacted in tracker
    with open("outputs/tracker_recruiter_update.jsonl", "a") as f:
        f.write(json.dumps({"job_id": job["id"], "recruiter_emailed": sent}) + "\n")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN PROCESSOR
# ─────────────────────────────────────────────────────────────────────────────
async def process_job(job: dict):
    log.info(f"Scouting contacts: {job['title']} @ {job['company']}")

    contacts = await find_contacts(job["company"], job["title"])
    if not contacts:
        log.warning(f"No contacts found for {job['company']}")
        return

    for contact in contacts:
        if not contact.get("email"):
            log.info(f"Skipping {contact['name']} — no email found")
            continue
        try:
            email_data = generate_outreach_email(contact, job)
            log.info(f"Email drafted for {contact['name']} ({contact['position']})")
            log.info(f"Subject: {email_data['subject']}")

            sent = await send_gmail(
                contact["email"],
                email_data["subject"],
                email_data["body"]
            )
            log_outreach(job, contact, email_data, sent)
            await asyncio.sleep(3)   # polite delay between sends

        except Exception as e:
            log.error(f"Outreach failed [{contact['name']}]: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# QUEUE RUNNER
# ─────────────────────────────────────────────────────────────────────────────
async def run_from_queue():
    """Process submitted jobs from Agent 3's output."""
    queue_path = Path("outputs/agent4_submitted.jsonl")
    if not queue_path.exists():
        log.info("No submitted jobs yet."); return

    lines = queue_path.read_text().strip().splitlines()
    submitted = [json.loads(l) for l in lines if json.loads(l).get("status") == "submitted"]

    if not submitted:
        log.info("No successfully submitted applications to scout."); return

    log.info(f"Scouting contacts for {len(submitted)} submitted applications...")
    for job in submitted:
        await process_job(job)
        await asyncio.sleep(5)

    log.info("Agent 5 complete")

if __name__ == "__main__":
    asyncio.run(run_from_queue())
