"""
Live pipeline dashboard — run in a separate terminal:
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 dashboard.py
Refreshes every 5 seconds. Press Ctrl+C to exit.
"""
import os, re, time, json
from pathlib import Path
from datetime import datetime

LOG  = Path("logs/orchestrator.log")
PIPE = Path("/tmp/pipeline_full.txt")

def read_log() -> str:
    src = PIPE if PIPE.exists() else LOG
    try:
        return src.read_text(errors="ignore")
    except:
        return ""

def count(pattern, text):
    return len(re.findall(pattern, text))

def last_line(pattern, text):
    matches = re.findall(pattern, text)
    return matches[-1].strip() if matches else ""

def parse_dashboard():
    text = read_log()
    now  = datetime.now().strftime("%H:%M:%S")

    # ── Agent 1 ───────────────────────────────────────────────────────────────
    a1_done   = "Agent 1 complete" in text or "✅ Agent 1" in text
    raw_match = re.search(r"Total raw: (\d+)", text)
    flt_match = re.search(r"After filter: (\d+)", text)
    hot_match = re.search(r"Hot jobs.*?: (\d+)", text)
    src_match = re.search(r"Sources: (.+)", text)

    a1_raw = raw_match.group(1) if raw_match else "…"
    a1_flt = flt_match.group(1) if flt_match else "…"
    a1_hot = hot_match.group(1) if hot_match else "…"
    a1_src = src_match.group(1)[:80] if src_match else ""

    # ── Agent 2 ───────────────────────────────────────────────────────────────
    a2_started = "AGENT 2" in text or "Application Writer" in text
    a2_done    = "✅ Agent 2 complete" in text
    a2_written = count(r"✅ Done:", text)
    a2_failed  = count(r"❌ Failed", text)
    a2_total   = int(a1_hot) if a1_hot.isdigit() else 0
    a2_current = last_line(r"▶ Writing application: (.+)", text) if not a2_done else "—"
    a2_pdfs    = len(list(Path("outputs/applications").glob("**/*.pdf"))) if Path("outputs/applications").exists() else 0

    # ── Agent 3 ───────────────────────────────────────────────────────────────
    a3_started  = "AGENT 3" in text or "Submitter" in text
    a3_done     = "✅ Agent 3 complete" in text
    a3_attempts = count(r"Submitting \[", text)
    a3_submitted= count(r"✅ Submitted:", text)
    a3_review   = count(r"Needs review:", text)
    a3_current  = last_line(r"Submitting \[.+?\]: (.+)", text) if not a3_done else "—"
    a3_rewrites = count(r"URL rewritten:", text)

    # ── Agent 4 ───────────────────────────────────────────────────────────────
    a4_started = "AGENT 4" in text or "Tracker" in text
    a4_done    = "✅ Agent 4 complete" in text
    xlsx       = Path("outputs/Akshit_Applications.xlsx")
    a4_rows    = 0
    if xlsx.exists():
        try:
            import openpyxl
            wb = openpyxl.load_workbook(xlsx, read_only=True)
            a4_rows = wb["Applications"].max_row - 1
            wb.close()
        except: pass

    # ── Agent 5 ───────────────────────────────────────────────────────────────
    a5_started = "AGENT 5" in text or "Recruiter" in text
    a5_done    = "✅ Agent 5 complete" in text
    a5_found   = count(r"Found contact:", text)
    a5_sent    = count(r"Email sent|✅.*email", text)
    outreach   = Path("outputs/outreach_log.jsonl")
    a5_logged  = len(outreach.read_text().strip().splitlines()) if outreach.exists() and outreach.stat().st_size else 0

    # ── Is pipeline alive? ─────────────────────────────────────────────────────
    pipeline_done = "══ Pipeline complete" in text or "Pipeline complete" in text
    pid_alive = os.popen("pgrep -f orchestrator.py").read().strip()

    # ── RENDER ─────────────────────────────────────────────────────────────────
    W = 72
    print("\033[H\033[J", end="")   # clear screen
    print("═" * W)
    print(f"  JOB AUTOMATION — LIVE PIPELINE DASHBOARD   [{now}]")
    print("═" * W)

    def status_icon(started, done):
        if done:    return "✅"
        if started: return "⏳"
        return "⏸ "

    def bar(done, total, width=30):
        if not total: return "[" + "─"*width + "]"
        filled = int(width * done / total)
        return "[" + "█"*filled + "─"*(width-filled) + f"] {done}/{total}"

    # Agent 1
    s = status_icon(True, a1_done)
    print(f"\n  {s} AGENT 1 — JOB SCOUT")
    print(f"     Raw fetched : {a1_raw:>5}  |  Filtered: {a1_flt:>4}  |  Hot (≥60): {a1_hot}")
    if a1_src:
        pairs = [x.strip() for x in a1_src.split("|")]
        print(f"     Sources     : {', '.join(pairs[:6])}")
        if len(pairs) > 6:
            print(f"                  {', '.join(pairs[6:])}")

    # Agent 2
    s2 = status_icon(a2_started, a2_done)
    pct2 = f" ({int(100*a2_written/a2_total)}%)" if a2_total and not a2_done else ""
    print(f"\n  {s2} AGENT 2 — APPLICATION WRITER")
    if a2_started:
        print(f"     Progress    : {bar(a2_written, a2_total)}{pct2}")
        print(f"     PDFs made   : {a2_pdfs}  |  Failed: {a2_failed}")
        if not a2_done and a2_current:
            print(f"     Now writing : {a2_current[:60]}")
    else:
        print(f"     Status      : waiting for Agent 1")

    # Agent 3
    s3 = status_icon(a3_started, a3_done)
    print(f"\n  {s3} AGENT 3 — SUBMITTER")
    if a3_started:
        print(f"     Attempted   : {a3_attempts}  |  Submitted: {a3_submitted}  |  Needs review: {a3_review}")
        print(f"     URL rewrites: {a3_rewrites} (login-wall bypass)")
        if not a3_done and a3_current:
            print(f"     Now applying: {a3_current[:60]}")
    else:
        print(f"     Status      : waiting for Agent 2")

    # Agent 4
    s4 = status_icon(a4_started, a4_done)
    print(f"\n  {s4} AGENT 4 — TRACKER")
    if a4_started:
        print(f"     Excel rows  : {a4_rows}  |  File: outputs/Akshit_Applications.xlsx")
    else:
        print(f"     Status      : waiting for Agent 3")

    # Agent 5
    s5 = status_icon(a5_started, a5_done)
    print(f"\n  {s5} AGENT 5 — RECRUITER OUTREACH")
    if a5_started:
        print(f"     Contacts    : {a5_found}  |  Emails sent: {a5_sent}  |  Logged: {a5_logged}")
    else:
        print(f"     Status      : waiting for Agent 4")

    # Footer
    print(f"\n{'─'*W}")
    if pipeline_done:
        print(f"  🎉  PIPELINE COMPLETE")
    elif pid_alive:
        print(f"  ▶   Pipeline running  (PID {pid_alive})  — refreshes every 5s")
    else:
        print(f"  ⚠️   Pipeline process not found — may have crashed")

    # Recent log lines
    lines = text.strip().splitlines()
    recent = [l for l in lines[-12:] if "[ORCH]" in l or "[WRITER]" in l]
    if recent:
        print(f"\n  Recent activity:")
        for line in recent[-6:]:
            ts  = re.search(r"\d{2}:\d{2}:\d{2}", line)
            msg = re.sub(r"^\S+ \[.*?\] ", "", line)
            t   = ts.group() if ts else ""
            print(f"    {t}  {msg[:65]}")
    print("═" * W)


if __name__ == "__main__":
    print("Starting dashboard... (Ctrl+C to exit)")
    try:
        while True:
            parse_dashboard()
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
