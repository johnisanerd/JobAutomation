"""
AGENT 4 — TRACKER
Maintains a live Excel spreadsheet + Google Sheet with all applications.
Monitors inbox for replies and updates status automatically.
Runs every 30 minutes to poll for updates.
"""

import asyncio, base64, json, logging, os, re
from datetime import datetime
from pathlib import Path
import httpx
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from dotenv import load_dotenv
load_dotenv()

from config.profile import PROFILE

log = logging.getLogger("agent4_tracker")
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [TRACKER] %(message)s",
    handlers=[logging.FileHandler("logs/agent4.log"), logging.StreamHandler()])

ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_KEY")
from core.gmail_token import get_gmail_token
SHEETS_TOKEN    = os.getenv("GOOGLE_SHEETS_TOKEN", "YOUR_SHEETS_TOKEN")
SPREADSHEET_ID  = os.getenv("SPREADSHEET_ID", "")        # Google Sheet ID (optional)
EXCEL_PATH      = Path("outputs/Akshit_Applications.xlsx")
SUBMITTED_QUEUE = Path("outputs/agent4_submitted.jsonl")
SUBMITTED_HISTORY = Path("outputs/agent4_submitted_history.jsonl")

COLS = [
    "ID", "Date Found", "Date Applied", "Company", "Role", "Location",
    "Source", "ATS", "Score", "Verdict", "Status", "URL", "Cover Letter",
    "Resume Version", "Recruiter Contacted", "Last Update",
    "Interviewer", "Interview Date", "Notes",
]

STATUS_COLOURS = {
    "found":            "FFF9C4",   # yellow
    "ready_to_apply":   "FFF3E0",   # orange-light
    "submitted":        "E8F5E9",   # green-light
    "needs_review":     "FFF3E0",   # amber
    "rejected":         "FFEBEE",   # red-light
    "interview":        "E3F2FD",   # blue-light
    "offer":            "F3E5F5",   # purple-light
    "withdrawn":        "ECEFF1",   # grey
    "failed":           "FFCDD2",   # red
    "needs_manual":     "FFE0B2",   # orange
}

STATUS_PRIORITY = {
    "found": 0,
    "ready_to_apply": 1,
    "failed": 2,
    "needs_review": 2,
    "needs_manual": 2,
    "submitted": 3,
    "rejected": 4,
    "interview": 5,
    "offer": 6,
    "withdrawn": 7,
}

# ─────────────────────────────────────────────────────────────────────────────
# EXCEL HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def init_workbook() -> openpyxl.Workbook:
    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = "Applications"

    # Header row styling
    header_fill   = PatternFill("solid", fgColor="1A1A2E")
    header_font   = Font(bold=True, color="FFFFFF", size=10)
    header_align  = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for col_idx, col_name in enumerate(COLS, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.fill   = header_fill
        cell.font   = header_font
        cell.alignment = header_align

    # Column widths
    widths = {
        "ID": 14, "Date Found": 13, "Date Applied": 13, "Company": 22,
        "Role": 30, "Location": 16, "Source": 12, "ATS": 14,
        "Score": 8, "Verdict": 10, "Status": 16, "URL": 40,
        "Cover Letter": 12, "Resume Version": 16, "Recruiter Contacted": 20,
        "Last Update": 16, "Interviewer": 18, "Interview Date": 16, "Notes": 35,
    }
    for col_idx, col_name in enumerate(COLS, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = widths.get(col_name, 15)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLS))}1"

    # Add a Summary sheet
    ws2 = wb.create_sheet("Summary")
    summary_data = [
        ("Metric", "Count"),
        ("Total Applications", 0),
        ("Submitted",          0),
        ("Needs Review",       0),
        ("Needs Manual",       0),
        ("Failed",             0),
        ("Rejected",           0),
        ("Interview",          0),
        ("Offer",              0),
        ("Pending",            0),
        ("Response Rate",      "0%"),
    ]
    for r_idx, (k, v) in enumerate(summary_data, 1):
        ws2.cell(row=r_idx, column=1, value=k).font = Font(bold=(r_idx == 1))
        ws2.cell(row=r_idx, column=2, value=v)
    ws2.column_dimensions["A"].width = 22
    ws2.column_dimensions["B"].width = 12

    return wb

def load_or_create_wb() -> openpyxl.Workbook:
    if EXCEL_PATH.exists():
        return openpyxl.load_workbook(EXCEL_PATH)
    return init_workbook()

def find_row_by_id(ws, job_id: str) -> int | None:
    for row in ws.iter_rows(min_row=2, values_only=False):
        if row[0].value == job_id:
            return row[0].row
    return None

def non_empty(value):
    return value not in (None, "")

def status_should_update(current: str | None, new: str | None) -> bool:
    if not new:
        return False
    if not current:
        return True
    return STATUS_PRIORITY.get(new, 0) >= STATUS_PRIORITY.get(current, 0)

def update_row_preserving_details(ws, row_idx: int, data: dict):
    """Update an existing row without erasing useful details or downgrading status."""
    updates = {
        2: data.get("date_found", "")[:10] if data.get("date_found") else "",
        3: data.get("submitted_at", "")[:10] if data.get("submitted_at") else "",
        4: data.get("company", ""),
        5: data.get("title", ""),
        6: data.get("location", ""),
        7: data.get("source", ""),
        8: data.get("ats", ""),
        9: data.get("score", None),
        10: data.get("verdict", ""),
        12: data.get("url", ""),
        13: "Yes" if data.get("cover_letter") else "",
        14: data.get("resume_pdf", "") or data.get("tex_url", ""),
        15: "Yes" if data.get("recruiter_emailed") else "",
        17: data.get("interviewer", ""),
        18: data.get("interview_date", ""),
    }
    for col, value in updates.items():
        if non_empty(value):
            ws.cell(row=row_idx, column=col, value=value)

    current_status = ws.cell(row=row_idx, column=11).value
    new_status = data.get("status")
    if status_should_update(current_status, new_status):
        ws.cell(row=row_idx, column=11, value=new_status)

    ws.cell(row=row_idx, column=16, value=datetime.now().strftime("%Y-%m-%d %H:%M"))
    if data.get("notes"):
        existing = ws.cell(row=row_idx, column=19).value or ""
        note = data["notes"]
        if note not in existing:
            ws.cell(row=row_idx, column=19, value=f"{existing} | {note}".strip(" |"))

    status = ws.cell(row=row_idx, column=11).value or "found"
    row_fill = PatternFill("solid", fgColor=STATUS_COLOURS.get(status, "FFFFFF"))
    for col in range(1, len(COLS) + 1):
        ws.cell(row=row_idx, column=col).fill = row_fill

def write_row(ws, row_idx: int, data: dict):
    values = [
        data.get("id", ""),
        data.get("date_found", "")[:10] if data.get("date_found") else "",
        data.get("submitted_at", "")[:10] if data.get("submitted_at") else "",
        data.get("company", ""),
        data.get("title", ""),
        data.get("location", ""),
        data.get("source", ""),
        data.get("ats", ""),
        data.get("score", 0),
        data.get("verdict", ""),
        data.get("status", "found"),
        data.get("url", ""),
        "Yes" if data.get("cover_letter") else "No",
        data.get("tex_url", ""),
        "Yes" if data.get("recruiter_emailed") else "No",
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        data.get("interviewer", ""),
        data.get("interview_date", ""),
        data.get("notes", ""),
    ]
    status = data.get("status", "found")
    fill_colour = STATUS_COLOURS.get(status, "FFFFFF")
    row_fill    = PatternFill("solid", fgColor=fill_colour)
    border_side = Side(style="thin", color="E0E0E0")
    row_border  = Border(bottom=border_side)

    for col_idx, val in enumerate(values, 1):
        cell            = ws.cell(row=row_idx, column=col_idx, value=val)
        cell.fill       = row_fill
        cell.border     = row_border
        cell.alignment  = Alignment(vertical="center", wrap_text=(col_idx in [5, 12, 19]))
        if col_idx == 12 and val:   # URL — add hyperlink
            cell.hyperlink = val
            cell.font = Font(color="1565C0", underline="single")

def update_summary(wb: openpyxl.Workbook):
    ws  = wb["Applications"]
    ws2 = wb["Summary"]
    statuses = [ws.cell(row=r, column=11).value for r in range(2, ws.max_row + 1)
                if ws.cell(row=r, column=1).value]
    total      = len(statuses)
    submitted  = statuses.count("submitted")
    needs_review = statuses.count("needs_review")
    needs_manual = statuses.count("needs_manual")
    failed     = statuses.count("failed")
    rejected   = statuses.count("rejected")
    interview  = statuses.count("interview")
    offer      = statuses.count("offer")
    pending    = statuses.count("found") + statuses.count("ready_to_apply")
    resp_rate  = f"{round((rejected + interview + offer) / max(submitted, 1) * 100)}%"

    summary_rows = [
        ("Total Applications", total),
        ("Submitted", submitted),
        ("Needs Review", needs_review),
        ("Needs Manual", needs_manual),
        ("Failed", failed),
        ("Rejected", rejected),
        ("Interview", interview),
        ("Offer", offer),
        ("Pending", pending),
        ("Response Rate", resp_rate),
    ]
    ws2.cell(row=1, column=1, value="Metric").font = Font(bold=True)
    ws2.cell(row=1, column=2, value="Count").font = Font(bold=True)
    for row, (label, val) in enumerate(summary_rows, 2):
        ws2.cell(row=row, column=1, value=label)
        ws2.cell(row=row, column=2, value=val)

def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            log.warning(f"Skipping malformed JSONL line in {path}")
    return rows

def build_job_index() -> dict[str, dict]:
    """
    Build a best-effort source-of-truth index from Agent 1 and Agent 2 outputs.
    Agent 3 submissions are intentionally compact, so Agent 4 enriches them here.
    """
    index: dict[str, dict] = {}
    for job in load_jsonl(Path("outputs/tracker_queue.jsonl")):
        if job.get("id"):
            index[job["id"]] = job

    for app_json in Path("outputs/applications").glob("*/application.json"):
        try:
            app = json.loads(app_json.read_text())
            if app.get("id"):
                index[app["id"]] = {**index.get(app["id"], {}), **app}
        except Exception as e:
            log.warning(f"Could not read application package {app_json}: {e}")
    return index

def normalise_submission(raw: dict, job_index: dict[str, dict]) -> dict:
    """Convert Agent 3 output into the full shape expected by the tracker."""
    job_id = raw.get("id") or raw.get("job_id") or ""
    base = dict(job_index.get(job_id, {}))
    merged = {**base, **raw}
    merged["id"] = job_id
    if raw.get("job_id") and not raw.get("id"):
        merged.pop("job_id", None)
    if raw.get("submitted_at"):
        merged["submitted_at"] = raw["submitted_at"]
    if raw.get("status"):
        merged["status"] = raw["status"]
    if raw.get("notes"):
        merged["notes"] = raw["notes"]
    if base.get("resume_pdf"):
        merged["resume_pdf"] = base["resume_pdf"]
    return merged

# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOG FUNCTION (called by Agents 1, 3)
# ─────────────────────────────────────────────────────────────────────────────
async def log_jobs(jobs: list[dict]):
    """Write/update a batch of jobs in the spreadsheet."""
    os.makedirs("outputs", exist_ok=True)
    wb = load_or_create_wb()
    ws = wb["Applications"]

    for job in jobs:
        existing_row = find_row_by_id(ws, job.get("id", ""))
        if existing_row:
            update_row_preserving_details(ws, existing_row, job)
        else:
            next_row = ws.max_row + 1
            write_row(ws, next_row, job)

    update_summary(wb)
    wb.save(EXCEL_PATH)
    log.info(f"Tracker updated: {len(jobs)} jobs → {EXCEL_PATH}")

    # Optionally sync to Google Sheets
    if SPREADSHEET_ID:
        await sync_to_google_sheets(jobs)

async def update_status(job_id: str, status: str, notes: str = ""):
    """Update a single job's status (called after submission/reply)."""
    wb = load_or_create_wb()
    ws = wb["Applications"]
    row = find_row_by_id(ws, job_id)
    if row:
        ws.cell(row=row, column=11, value=status)
        ws.cell(row=row, column=16, value=datetime.now().strftime("%Y-%m-%d %H:%M"))
        if notes:
            existing = ws.cell(row=row, column=19).value or ""
            ws.cell(row=row, column=19, value=f"{existing} | {notes}".strip(" |"))
        row_fill = PatternFill("solid", fgColor=STATUS_COLOURS.get(status, "FFFFFF"))
        for col in range(1, len(COLS) + 1):
            ws.cell(row=row, column=col).fill = row_fill
        update_summary(wb)
        wb.save(EXCEL_PATH)
        log.info(f"Status updated [{job_id}]: {status}")

async def repair_workbook_integrity():
    """Remove legacy corrupt rows and refresh formulas/summary."""
    if not EXCEL_PATH.exists():
        return
    wb = load_or_create_wb()
    ws = wb["Applications"]
    removed = 0
    for row_idx in range(ws.max_row, 1, -1):
        has_any_data = any(ws.cell(row=row_idx, column=c).value for c in range(1, len(COLS) + 1))
        missing_id = not ws.cell(row=row_idx, column=1).value
        if has_any_data and missing_id:
            ws.delete_rows(row_idx, 1)
            removed += 1
    update_summary(wb)
    wb.save(EXCEL_PATH)
    if removed:
        log.info(f"Workbook integrity repair: removed {removed} legacy rows with blank IDs")

# ─────────────────────────────────────────────────────────────────────────────
# GMAIL INBOX POLLER
# ─────────────────────────────────────────────────────────────────────────────
def classify_email(subject: str, sender: str) -> str | None:
    text = f"{subject} {sender}".lower()
    ignore = [
        "security code", "verification code", "confirm you're a human",
        "thanks for applying", "thank you for applying", "application received",
        "received your application", "no-reply@us.greenhouse-mail.io",
    ]
    if any(k in text for k in ignore):
        return None
    if any(k in text for k in [
        "offer", "congratulations", "pleased to offer",
    ]):
        return "offer"
    if any(k in text for k in [
        "interview", "schedule", "availability", "available for a call",
        "next step", "next round", "recruiter screen", "technical screen",
        "phone screen", "chat with", "book a time",
    ]):
        return "interview"
    if any(k in text for k in [
        "rejected", "unsuccessful", "not moving forward", "not selected",
        "other candidates", "decided not to proceed", "will not be proceeding",
        "unfortunately",
    ]):
        return "rejected"
    return None

def normalise_match_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())

def match_email_to_job(ws, subject: str, sender: str) -> str | None:
    haystack = normalise_match_text(f"{subject} {sender}")
    best = None
    best_len = 0
    for row in ws.iter_rows(min_row=2, values_only=False):
        job_id = row[0].value
        company = str(row[3].value or "")
        if not job_id or not company:
            continue
        needle = normalise_match_text(company)
        if len(needle) >= 3 and needle in haystack and len(needle) > best_len:
            best = job_id
            best_len = len(needle)
    return best

async def poll_gmail_for_replies():
    """Check inbox for application replies and update tracker."""
    try:
        gmail_token = get_gmail_token()
        async with httpx.AsyncClient() as c:
            r = await c.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                headers={"Authorization": f"Bearer {gmail_token}"},
                params={"q": "(application OR interview OR opportunity OR position OR recruiter OR schedule) newer_than:14d",
                        "maxResults": 50}
            )
            msgs = r.json().get("messages", [])
            for msg_ref in msgs:
                msg_r = await c.get(
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_ref['id']}",
                    headers={"Authorization": f"Bearer {gmail_token}"},
                    params={"format": "metadata", "metadataHeaders": ["Subject", "From"]}
                )
                headers = {h["name"]: h["value"] for h in msg_r.json().get("payload", {}).get("headers", [])}
                subject = headers.get("Subject", "")
                sender  = headers.get("From", "")

                status = classify_email(subject, sender)
                if not status:
                    continue

                wb = load_or_create_wb()
                ws = wb["Applications"]
                job_id = match_email_to_job(ws, subject, sender)
                if job_id:
                    await update_status(job_id, status,
                                        f"Auto-detected from email: {subject}")
                    log.info(f"Email detected → {job_id}: {status}")

    except Exception as e:
        log.warning(f"Gmail poll failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# GOOGLE SHEETS SYNC (optional)
# ─────────────────────────────────────────────────────────────────────────────
async def sync_to_google_sheets(jobs: list[dict]):
    if not SPREADSHEET_ID or not SHEETS_TOKEN:
        return
    try:
        rows = []
        for job in jobs:
            rows.append([
                job.get("id",""), job.get("company",""), job.get("title",""),
                job.get("location",""), job.get("score",0), job.get("verdict",""),
                job.get("status","found"), job.get("url",""),
                datetime.now().strftime("%Y-%m-%d %H:%M"),
            ])
        async with httpx.AsyncClient() as c:
            await c.post(
                f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/Sheet1!A1:I{len(rows)+1}:append",
                headers={"Authorization": f"Bearer {SHEETS_TOKEN}"},
                params={"valueInputOption": "USER_ENTERED"},
                json={"values": rows}
            )
        log.info(f"Synced {len(jobs)} rows to Google Sheets")
    except Exception as e:
        log.warning(f"Sheets sync failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# PROCESS SUBMITTED QUEUE (from Agent 3)
# ─────────────────────────────────────────────────────────────────────────────
async def process_submitted_queue():
    if not SUBMITTED_QUEUE.exists(): return
    lines = [l for l in SUBMITTED_QUEUE.read_text().splitlines() if l.strip()]
    if not lines: return

    job_index = build_job_index()
    jobs = []
    for line in lines:
        try:
            jobs.append(normalise_submission(json.loads(line), job_index))
        except Exception as e:
            log.warning(f"Skipping bad submitted queue entry: {e}")
    if not jobs:
        return

    with SUBMITTED_HISTORY.open("a") as f:
        for job in jobs:
            f.write(json.dumps(job) + "\n")

    await log_jobs(jobs)
    SUBMITTED_QUEUE.write_text("")
    log.info(f"Processed {len(jobs)} submitted entries from Agent 3")

# ─────────────────────────────────────────────────────────────────────────────
# PERIODIC RUN
# ─────────────────────────────────────────────────────────────────────────────
async def run_tracker():
    log.info("── Tracker cycle ──")
    await repair_workbook_integrity()
    await process_submitted_queue()
    await poll_gmail_for_replies()

if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    os.makedirs("outputs", exist_ok=True)
    scheduler = AsyncIOScheduler(timezone="Europe/Dublin")
    scheduler.add_job(run_tracker, "interval", minutes=30)
    scheduler.start()
    log.info("Agent 4 running — polls every 30 min")
    try:
        asyncio.get_event_loop().run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
