"""
WEB DASHBOARD — open http://localhost:8888 in your browser.

A premium, interactive control panel for the 5-agent pipeline:
 - Live per-agent progress (polled, no page reloads, no flicker)
 - Run / Stop EACH agent independently — they can run in parallel
 - Run Full pipeline / Stop All
 - Open or download the Excel tracker
 - Live activity feed + active-agent banner

Run:
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 web_dashboard.py
"""

import json, os, re, signal, subprocess, sys, threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PORT  = 8888
BASE  = Path(__file__).parent
EXCEL = BASE / "outputs" / "Akshit_Applications.xlsx"


AGENT_META = {
    "1": {"name": "Agent 1 · Scout", "short": "Scout", "emoji": "🔍"},
    "2": {"name": "Agent 2 · Writer", "short": "Writer", "emoji": "✍️"},
    "3": {"name": "Agent 3 · Submitter", "short": "Submitter", "emoji": "🚀"},
    "4": {"name": "Agent 4 · Tracker", "short": "Tracker", "emoji": "📊"},
    "5": {"name": "Agent 5 · Recruiter", "short": "Recruiter", "emoji": "📧"},
    "orch": {"name": "Pipeline", "short": "Pipeline", "emoji": "⚡"},
}

LOG_SOURCES = [
    ("orch", "orchestrator.log"),
    ("1", "agent1.log"),
    ("2", "agent2.log"),
    ("3", "agent3.log"),
    ("4", "agent4.log"),
    ("5", "agent5.log"),
]

TAG_TO_AGENT = {
    "agent1_scout": "1", "SCOUT": "1",
    "agent2_writer": "2", "WRITER": "2",
    "agent3_submitter": "3", "SUBMITTER": "3",
    "agent4_tracker": "4", "TRACKER": "4",
    "agent5_recruiter": "5", "RECRUITER": "5",
    "RESUME": "orch", "ORCH": "orch", "orchestrator": "orch",
}


def _tail_lines(path: Path, limit: int = 220) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(errors="ignore").splitlines()[-limit:]
    except Exception:
        return []


def _parse_log_line(line: str, fallback_agent: str) -> dict | None:
    m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})(?:,\d+)?\s+\[([^\]]+)\]\s*(.*)$", line)
    if m:
        date, time_s, tag, msg = m.groups()
        agent = TAG_TO_AGENT.get(tag, fallback_agent)
    else:
        m = re.match(r"^(\d{2}:\d{2}:\d{2})\s+(.*)$", line)
        date, time_s, tag, msg = "", (m.group(1) if m else ""), "", (m.group(2) if m else line)
        agent = fallback_agent

    msg = (msg or "").strip()
    if not msg:
        return None
    if "HTTP Request" in msg or "api.anthropic.com" in msg or "gmail.googleapis.com" in msg:
        return None
    if msg.startswith("Call log:") or msg.startswith("- "):
        return None

    lower = msg.lower()
    severity = "info"
    if any(x in msg for x in ("❌", "Traceback", "Exception")) or any(x in lower for x in ("failed", "error", "timeout")):
        severity = "bad"
    elif any(x in msg for x in ("⚠️", "warning")) or any(x in lower for x in ("needs review", "needs manual", "validation repair", "no verification code")):
        severity = "warn"
    elif any(x in msg for x in ("✅", "complete", "submitted", "updated", "done")):
        severity = "ok"

    stage = "Working"
    if "Writing application" in msg:
        stage = "Writing resume"
    elif "Compil" in msg or "pdf" in lower:
        stage = "Building PDF"
    elif "Submitting" in msg:
        stage = "Submitting form"
    elif "verification code" in lower:
        stage = "Waiting for code"
    elif "Validation repair" in msg:
        stage = "Fixing form"
    elif "Needs review" in msg or "needs manual" in lower:
        stage = "Needs review"
    elif "Tracker updated" in msg or "Excel" in msg:
        stage = "Updating Excel"
    elif "Queued" in msg or "queue" in lower:
        stage = "Queueing"
    elif "Found" in msg or "Scored" in msg or "Sources:" in msg:
        stage = "Finding jobs"
    elif "Recruiter" in msg or "email" in lower or "contact" in lower:
        stage = "Outreach"
    elif "complete" in lower:
        stage = "Complete"

    meta = AGENT_META.get(agent, AGENT_META["orch"])
    return {
        "date": date,
        "t": time_s or "—",
        "sort": f"{date} {time_s}",
        "agent": agent,
        "agent_name": meta["name"],
        "agent_short": meta["short"],
        "emoji": meta["emoji"],
        "stage": stage,
        "severity": severity,
        "m": msg[:180],
    }


def collect_activity() -> dict:
    entries = []
    seen = set()
    for agent, filename in LOG_SOURCES:
        for line in _tail_lines(BASE / "logs" / filename):
            item = _parse_log_line(line, agent)
            if not item:
                continue
            key = (item["agent"], item["sort"], item["m"])
            if key in seen:
                continue
            seen.add(key)
            entries.append(item)

    entries.sort(key=lambda x: x["sort"])
    by_agent = {a: [] for a in ("1", "2", "3", "4", "5")}
    last = {}
    for item in entries:
        if item["agent"] in by_agent:
            by_agent[item["agent"]].append(item)
            last[item["agent"]] = item
    for agent in by_agent:
        by_agent[agent] = by_agent[agent][-5:]

    recent = entries[-40:]
    return {
        "recent": recent,
        "by_agent": by_agent,
        "last": last,
    }


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    try:
        for line in path.read_text(errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    return rows


def read_excel_snapshot() -> dict:
    snap = {"exists": EXCEL.exists(), "rows": 0, "status_counts": {}, "processed_ids": set()}
    if not EXCEL.exists():
        return snap
    try:
        import openpyxl
        wb = openpyxl.load_workbook(EXCEL, read_only=True)
        ws = wb["Applications"]
        headers = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
        id_idx = headers.index("ID") if "ID" in headers else None
        status_idx = headers.index("Status") if "Status" in headers else None
        for row in ws.iter_rows(min_row=2, values_only=True):
            snap["rows"] += 1
            status = (row[status_idx] if status_idx is not None else "") or "blank"
            snap["status_counts"][status] = snap["status_counts"].get(status, 0) + 1
            job_id = row[id_idx] if id_idx is not None else ""
            if job_id and status not in ("ready_to_apply", "found", "blank"):
                snap["processed_ids"].add(str(job_id))
        wb.close()
    except Exception:
        pass
    return snap

# ─────────────────────────────────────────────────────────────────────────────
# DATA COLLECTION  (unchanged — ground truth from logs + filesystem)
# ─────────────────────────────────────────────────────────────────────────────
def collect_stats() -> dict:
    # Read all logs, deduplicate lines (duplicate logging from multi-agent imports)
    seen_lines = set()
    log_lines  = []
    for name in ["orchestrator.log", "agent1.log", "agent2.log",
                 "agent3.log", "agent4.log", "agent5.log"]:
        p = BASE / "logs" / name
        if not p.exists(): continue
        try:
            for line in p.read_text(errors="ignore").splitlines():
                if line not in seen_lines:
                    seen_lines.add(line)
                    log_lines.append(line)
        except: pass
    log_text = "\n".join(log_lines)

    def cnt(pat): return len(re.findall(pat, log_text))
    def last(pat):
        m = re.findall(pat, log_text)
        return m[-1].strip() if m else ""

    excel = read_excel_snapshot()

    # ── Agent 1 — use tracker_queue.jsonl as ground truth ────────────────────
    tracker_q = BASE / "outputs" / "tracker_queue.jsonl"
    total_scored, hot_count = 0, 0
    try:
        from config.profile import PROFILE
        threshold = PROFILE.get("score_threshold", 60)
        for line in tracker_q.read_text().strip().splitlines():
            if not line: continue
            j = json.loads(line)
            total_scored += 1
            if j.get("score", 0) >= threshold:
                hot_count += 1
    except: pass

    raw_m = re.search(r"Total raw: (\d+)", log_text)
    flt_m = re.search(r"After filter: (\d+)", log_text)
    src_m = re.search(r"Sources: (.+)", log_text)
    a1 = {
        "done":     "Agent 1 complete" in log_text or "✅ Agent 1" in log_text,
        "raw":      raw_m.group(1) if raw_m else ("—" if not total_scored else str(total_scored)),
        "filtered": flt_m.group(1) if flt_m else str(total_scored),
        "hot":      str(hot_count) if hot_count else (re.search(r"Hot jobs.*?: (\d+)", log_text) or type("", (), {"group": lambda *_: "—"})()).group(1),
        "sources":  src_m.group(1)[:120] if src_m else "",
    }

    # ── Agent 2 — ground truth from filesystem ────────────────────────────────
    app_dirs  = list((BASE / "outputs" / "applications").glob("*/application.json"))
    apps_done = len(app_dirs)
    pdfs_done = len(list((BASE / "outputs" / "applications").glob("**/*.pdf")))
    a2_total  = hot_count or int(a1["hot"]) if str(a1["hot"]).isdigit() else apps_done
    remaining = max(0, a2_total - apps_done)
    pct2      = min(100, int(100 * apps_done / a2_total)) if a2_total else 0
    a2_cur    = last(r"▶ Writing application: (.+)")
    a2_writing= bool(a2_cur) and ("✅ Agent 2 complete" not in log_text)
    a2 = {
        "started":  apps_done > 0 or "Application Writer" in log_text,
        "done":     "✅ Agent 2 complete" in log_text and remaining == 0,
        "writing":  a2_writing,
        "written":  apps_done,
        "pdfs":     pdfs_done,
        "failed":   cnt(r"❌ Failed \["),
        "total":    a2_total,
        "remaining":remaining,
        "pct":      pct2,
        "current":  a2_cur,
    }

    # ── Agent 3 — final truth from Excel; handoff JSONL is only current-cycle.
    submitted_file = BASE / "outputs" / "agent4_submitted.jsonl"
    submitted_history = BASE / "outputs" / "agent4_submitted_history.jsonl"
    current_handoff = load_jsonl(submitted_file)
    history_rows = load_jsonl(submitted_history)

    handoff_status = {}
    handoff_ids = set()
    manual_list = []
    for j in current_handoff:
        st = j.get("status", "unknown")
        handoff_status[st] = handoff_status.get(st, 0) + 1
        jid = j.get("job_id") or j.get("id")
        if jid:
            handoff_ids.add(jid)
        if st in ("needs_manual", "needs_review"):
            manual_list.append(f"{j.get('title','?')[:40]} @ {j.get('company','?')}")

    history_ids = {j.get("id") or j.get("job_id") for j in history_rows if (j.get("id") or j.get("job_id"))}
    processed_ids = set(excel["processed_ids"]) | history_ids | handoff_ids

    # Also count from logs. Keep this separate from the JSONL handoff file:
    # logs answer "was a success ever observed?", JSONL answers "is it confirmed in
    # the current tracker handoff file?"
    log_submitted_items = set(re.findall(r"✅ Submitted: (.+)", log_text))
    log_submitted = len(log_submitted_items)
    done_summaries = re.findall(
        r"Done — Submitted: (\d+) \| Review: (\d+) \| Failed: (\d+)", log_text
    )
    latest_submitted = latest_review = latest_failed = 0
    if done_summaries:
        latest_submitted, latest_review, latest_failed = map(int, done_summaries[-1])
    log_attempted  = cnt(r"Submitting \[")
    screenshots   = len(list((BASE / "outputs" / "screenshots").glob("*.png")))
    orch_tail = "\n".join(_tail_lines(BASE / "logs" / "orchestrator.log", 500))
    current_submission = ""
    current_matches = re.findall(r"Submitting \[[^\]]+\](?: \[session\])?: (.+)", orch_tail)
    if current_matches:
        current_submission = current_matches[-1].strip()
    agent3_queue = BASE / "outputs" / "agent3_queue.jsonl"
    queue_total = pending_count = 0
    pending_by_company = {}
    if agent3_queue.exists():
        for line in agent3_queue.read_text(errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                j = json.loads(line)
                queue_total += 1
                if j.get("id") not in processed_ids:
                    pending_count += 1
                    company = (j.get("company") or "?").strip().lower()
                    pending_by_company[company] = pending_by_company.get(company, 0) + 1
            except Exception:
                pass
    durable_submitted = excel["status_counts"].get("submitted", 0)
    durable_review = excel["status_counts"].get("needs_review", 0)
    durable_manual = excel["status_counts"].get("needs_manual", 0)
    durable_failed = excel["status_counts"].get("failed", 0)
    durable_ready = excel["status_counts"].get("ready_to_apply", 0)
    handoff_total = len(current_handoff)
    a3 = {
        "started":    durable_submitted > 0 or durable_review > 0 or handoff_total > 0 or log_attempted > 0,
        "done":       "✅ Agent 3 complete" in log_text,
        "attempts":   log_attempted,
        "queue_total": queue_total,
        "pending":    pending_count,
        "pending_top": sorted(pending_by_company.items(), key=lambda x: -x[1])[:8],
        "processed":  max(0, queue_total - pending_count),
        "submitted":  durable_submitted,
        "review":     durable_review,
        "manual":     durable_manual,
        "failed":     durable_failed,
        "ready":      durable_ready,
        "handoff":    handoff_total,
        "handoff_submitted": handoff_status.get("submitted", 0),
        "handoff_review": handoff_status.get("needs_review", 0),
        "handoff_manual": handoff_status.get("needs_manual", 0),
        "handoff_failed": handoff_status.get("failed", 0),
        "log_submitted": log_submitted,
        "latest_submitted": latest_submitted,
        "latest_review": latest_review,
        "latest_failed": latest_failed,
        "screenshots":screenshots,
        "current":    current_submission,
        "status_summary": f"{pending_count} pending · {durable_submitted} submitted · {durable_review} review · {handoff_total} syncing",
        "manual_list":manual_list[:8],
    }

    # ── Agent 4 — Excel is the ground truth ───────────────────────────────────
    excel_rows = excel["rows"]
    a4 = {
        "started": excel_rows > 0 or "Tracker" in log_text,
        "done":    "✅ Agent 4 complete" in log_text,
        "rows":    excel_rows,
        "exists":  excel["exists"],
        "submitted": excel["status_counts"].get("submitted", 0),
        "review": excel["status_counts"].get("needs_review", 0),
        "failed": excel["status_counts"].get("failed", 0),
        "ready": excel["status_counts"].get("ready_to_apply", 0),
    }

    # ── Agent 5 ────────────────────────────────────────────────────────────────
    outreach = BASE / "outputs" / "outreach_log.jsonl"
    emails_sent = 0
    if outreach.exists():
        emails_sent = len([l for l in outreach.read_text().strip().splitlines() if l])
    a5 = {
        "started": emails_sent > 0 or "Recruiter" in log_text,
        "done":    "✅ Agent 5 complete" in log_text,
        "sent":    emails_sent,
    }

    procs = running_state()
    external = external_activity_state()
    if not procs.get("any") and external.get("any"):
        procs = external

    # ── Pipeline state — managed process state takes priority; pgrep is best-effort.
    try:
        pid = subprocess.run(
            ["pgrep", "-f", r"orchestrator\.py|resume_pipeline\.py"],
            capture_output=True, text=True
        ).stdout.strip()
    except Exception:
        pid = ""
    if pid and not procs.get("any"):
        orch_tail_for_pid = "\n".join(_tail_lines(BASE / "logs" / "orchestrator.log", 80))
        if "agent3_submitter" in orch_tail_for_pid or "Submitting [" in orch_tail_for_pid:
            procs["3"] = True
            procs["any"] = True
    # Only mark done if process is not running AND log says done
    is_done = (not pid) and (not procs.get("any")) and (
        "Pipeline complete" in log_text or "Resume complete" in log_text
    )

    # ── Structured live activity ──────────────────────────────────────────────
    activity = collect_activity()
    now_t = datetime.now().strftime("%H:%M:%S")
    fallback_activity = {
        "1": ("Finding jobs", f"{a1['filtered']} scored roles · {a1['hot']} hot matches", "ok" if a1["done"] else "info"),
        "2": ("Writing resume", f"{a2['written']} applications written · {a2['pdfs']} PDFs · {a2['remaining']} left", "warn" if a2["remaining"] else "ok"),
        "3": ("Submitting", f"{a3['pending']} pending · {a3['submitted']} submitted · {a3['review']} review · {a3['handoff']} syncing", "warn" if a3["pending"] or a3["review"] or a3["handoff"] else "ok"),
        "4": ("Updating Excel", f"{a4['rows']} rows · {a4['submitted']} submitted · {a4['review']} review · {a4['ready']} ready", "ok" if a4["exists"] else "warn"),
        "5": ("Outreach", f"{a5['sent']} recruiter emails logged", "ok" if a5["sent"] else "info"),
    }
    for agent, (stage, msg, severity) in fallback_activity.items():
        if activity["by_agent"].get(agent):
            continue
        meta = AGENT_META[agent]
        item = {
            "date": "",
            "t": now_t,
            "sort": f"derived {agent}",
            "agent": agent,
            "agent_name": meta["name"],
            "agent_short": meta["short"],
            "emoji": meta["emoji"],
            "stage": stage,
            "severity": severity,
            "m": msg,
        }
        activity["by_agent"][agent] = [item]
        activity["last"][agent] = item

    active_agent, active_msg = "", ""
    if procs.get("any") or pid:
        for agent in ("1", "2", "3", "4", "5"):
            if procs.get(agent) and activity["last"].get(agent):
                latest = activity["last"][agent]
                active_agent = latest["agent_name"]
                active_msg = latest["m"][:110]
                break
        if not active_agent and activity["recent"]:
            latest = activity["recent"][-1]
            active_agent = latest["agent_name"]
            active_msg = latest["m"][:110]

    return {
        "ts": datetime.now().strftime("%H:%M:%S"),
        "pid": pid,
        "done": is_done,
        "active_agent": active_agent,
        "active_msg": active_msg,
        "a1": a1, "a2": a2, "a3": a3, "a4": a4, "a5": a5,
        "recent": activity["recent"],
        "activity": activity,
        "procs": procs,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PER-AGENT PROCESS MANAGER  — each agent is its own process; run/stop in parallel
# ─────────────────────────────────────────────────────────────────────────────
_procs: dict[str, subprocess.Popen] = {}
_LOCK  = threading.Lock()
AGENT_FLAG = {"1": "--agent1", "2": "--agent2", "3": "--agent3",
              "4": "--agent4", "5": "--agent5", "all": None, "full": "--full"}
AGENT_KILL_PATTERNS = {
    "1": r"resume_pipeline\.py --agent1|agent1_scout\.py",
    "2": r"resume_pipeline\.py --agent2|agent2_writer\.py",
    "3": r"resume_pipeline\.py --agent3|agent3_submitter\.py",
    "4": r"resume_pipeline\.py --agent4|agent4_tracker\.py",
    "5": r"resume_pipeline\.py --agent5|agent5_recruiter\.py",
}


def _alive(p) -> bool:
    return p is not None and p.poll() is None


def running_state() -> dict:
    """Return {agent_id: bool}. Reaps dead processes. A full/all run marks every
    agent busy so the UI shows them as part of the pipeline."""
    with _LOCK:
        for k in list(_procs):
            if not _alive(_procs[k]):
                _procs.pop(k, None)
        live = set(_procs)
    pipeline = ("all" in live) or ("full" in live)
    state = {a: (a in live) or pipeline for a in ("1", "2", "3", "4", "5")}
    state["all"]  = pipeline
    state["any"]  = bool(live)
    return state


def external_activity_state(max_age_seconds: int = 180) -> dict:
    """Best-effort recovery after dashboard restart while an agent keeps running.

    Managed Popen state is lost when the dashboard restarts, but the agents keep
    appending logs. Use fresh log mtimes as a display hint so the UI still says
    "running" instead of going stale. Stop buttons still use pkill fallback below.
    """
    state = {a: False for a in ("1", "2", "3", "4", "5")}
    now = datetime.now().timestamp()
    for agent, filename in [("1", "agent1.log"), ("2", "agent2.log"), ("3", "orchestrator.log"),
                            ("4", "agent4.log"), ("5", "agent5.log")]:
        p = BASE / "logs" / filename
        try:
            if p.exists() and now - p.stat().st_mtime <= max_age_seconds:
                if agent == "3":
                    tail = "\n".join(_tail_lines(p, 30))
                    if "agent3_submitter" in tail or "Submitting [" in tail:
                        state[agent] = True
                else:
                    state[agent] = True
        except Exception:
            pass
    state["all"] = False
    state["any"] = any(state.values())
    return state


def launch_resume(agent: str):
    with _LOCK:
        if _alive(_procs.get(agent)):
            return False, f"{_name(agent)} is already running"
        live = {k for k, p in _procs.items() if _alive(p)}
        if agent in ("all", "full") and live:
            return False, "Stop running agents before a full pipeline run"
        if agent not in ("all", "full") and (live & {"all", "full"}):
            return False, "A full pipeline run is already in progress"

        flag = AGENT_FLAG.get(agent)
        cmd  = [sys.executable, str(BASE / "resume_pipeline.py")]
        if flag:
            cmd.append(flag)
        log_f = open(BASE / "logs" / "orchestrator.log", "a")
        # start_new_session → own process group, so Stop kills its browser children too
        _procs[agent] = subprocess.Popen(
            cmd, cwd=str(BASE), stdout=log_f, stderr=subprocess.STDOUT,
            start_new_session=True)
    return True, f"{_name(agent)} started"


def _kill(p) -> bool:
    if not _alive(p):
        return False
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        try:
            p.wait(timeout=8)
        except Exception:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        return True
    except Exception:
        try:
            p.terminate(); return True
        except Exception:
            return False


def stop_resume(agent: str = "all"):
    killed = False
    with _LOCK:
        if agent == "all":
            for k, p in list(_procs.items()):
                killed |= _kill(p)
                _procs.pop(k, None)
        else:
            killed |= _kill(_procs.get(agent))
            _procs.pop(agent, None)
            # A full/all run contains this agent — stopping it means stopping that run.
            for k in ("all", "full"):
                if _alive(_procs.get(k)):
                    killed |= _kill(_procs[k])
                    _procs.pop(k, None)
    if agent == "all":
        r = subprocess.run(["pkill", "-TERM", "-f", r"resume_pipeline\.py|orchestrator\.py"],
                           capture_output=True)
        killed |= (r.returncode == 0)
    elif agent in AGENT_KILL_PATTERNS:
        r = subprocess.run(["pkill", "-TERM", "-f", AGENT_KILL_PATTERNS[agent]],
                           capture_output=True)
        killed |= (r.returncode == 0)
    return killed, ("Stopped" if killed else "Nothing was running")


def _name(agent: str) -> str:
    return {"all": "Full pipeline", "full": "Full pipeline"}.get(
        agent, f"Agent {agent}")


# ─────────────────────────────────────────────────────────────────────────────
# FRONT-END  — static shell; all data rendered client-side from /stats
# ─────────────────────────────────────────────────────────────────────────────
PAGE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Automation · Control Center</title>
<style>
:root{
  --bg:#080a12; --bg2:#0d1020; --card:rgba(22,27,46,.72); --card-bd:rgba(99,102,241,.14);
  --txt:#e7ebf5; --mut:#8b94ad; --dim:#566079;
  --acc:#38bdf8; --acc2:#34d399; --ok:#34d399; --warn:#fbbf24; --bad:#fb7185; --info:#60a5fa;
  --ring:rgba(56,189,248,.45);
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Roboto,sans-serif;
  color:var(--txt);min-height:100vh;line-height:1.5;
  background:
    radial-gradient(1100px 600px at 12% -8%,rgba(56,189,248,.14),transparent 60%),
    radial-gradient(900px 600px at 95% 0%,rgba(52,211,153,.10),transparent 55%),
    var(--bg);
  -webkit-font-smoothing:antialiased;padding:22px clamp(14px,4vw,40px) 60px}
a{color:inherit;text-decoration:none}
.wrap{max-width:1180px;margin:0 auto}

/* Header */
.top{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:20px}
.brand{display:flex;align-items:center;gap:12px;font-weight:800;font-size:1.18rem;letter-spacing:-.2px}
.logo{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;font-size:1.15rem;
  background:linear-gradient(135deg,var(--acc),var(--acc2));box-shadow:0 6px 22px rgba(56,189,248,.30)}
.brand small{display:block;font-size:.66rem;font-weight:600;color:var(--mut);letter-spacing:.3px;margin-top:1px}
.chip{display:inline-flex;align-items:center;gap:7px;padding:6px 13px;border-radius:999px;
  font-size:.74rem;font-weight:700;letter-spacing:.3px;border:1px solid var(--card-bd);background:var(--card);backdrop-filter:blur(8px)}
.chip .pdot{width:8px;height:8px;border-radius:50%;background:var(--mut)}
.chip.live{color:var(--ok);border-color:rgba(52,211,153,.4)} .chip.live .pdot{background:var(--ok);animation:pulse 1.4s infinite}
.chip.done{color:var(--info);border-color:rgba(56,189,248,.4)} .chip.done .pdot{background:var(--info)}
.chip.idle{color:var(--mut)}
.spacer{flex:1}
.clock{font-size:.74rem;color:var(--mut);font-variant-numeric:tabular-nums}
.conn{font-size:.7rem;color:var(--mut);display:flex;align-items:center;gap:6px}
.conn i{width:7px;height:7px;border-radius:50%;background:var(--ok);display:inline-block}
.conn.bad i{background:var(--bad)}

/* Global action bar */
.gbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.btn{display:inline-flex;align-items:center;gap:7px;padding:9px 15px;border-radius:11px;cursor:pointer;
  font-size:.78rem;font-weight:700;border:1px solid transparent;transition:transform .12s,box-shadow .2s,background .2s,opacity .2s;user-select:none}
.btn:active{transform:translateY(1px) scale(.99)}
.btn:disabled{opacity:.4;cursor:not-allowed;transform:none!important}
.btn.primary{color:#051018;background:linear-gradient(135deg,var(--acc),var(--acc2));box-shadow:0 6px 20px rgba(56,189,248,.28)}
.btn.primary:hover:not(:disabled){box-shadow:0 9px 26px rgba(56,189,248,.42)}
.btn.ghost{background:rgba(56,189,248,.08);color:#b9e8ff;border-color:rgba(56,189,248,.18)}
.btn.ghost:hover:not(:disabled){background:rgba(56,189,248,.15)}
.btn.danger{background:rgba(251,113,133,.12);color:var(--bad);border-color:rgba(251,113,133,.32)}
.btn.danger:hover:not(:disabled){background:rgba(251,113,133,.22)}
.btn.sm{padding:7px 12px;font-size:.74rem;border-radius:9px}

/* Summary pills */
.pills{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.pcard{background:var(--card);border:1px solid var(--card-bd);border-radius:14px;padding:13px 16px;backdrop-filter:blur(10px);
  display:flex;align-items:center;gap:12px}
.pcard .pi{width:36px;height:36px;border-radius:10px;display:grid;place-items:center;font-size:1.05rem}
.pcard .pv{font-size:1.34rem;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}
.pcard .pl{font-size:.66rem;color:var(--mut);text-transform:uppercase;letter-spacing:.7px;margin-top:3px}
.i-hot{background:rgba(56,189,248,.14);color:#9de7ff}.i-pdf{background:rgba(96,165,250,.14);color:#bcd7ff}
.i-sub{background:rgba(52,211,153,.16);color:#7ef0c4}.i-xls{background:rgba(56,189,248,.16);color:#8fdcff}

/* Active banner */
.active{display:none;align-items:center;gap:14px;margin-bottom:18px;padding:13px 18px;border-radius:14px;
  background:linear-gradient(90deg,rgba(56,189,248,.16),rgba(52,211,153,.05));border:1px solid rgba(56,189,248,.30)}
.active.show{display:flex;animation:slidein .35s ease}
.active .dot{width:11px;height:11px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 0 rgba(52,211,153,.6);animation:pulse 1.5s infinite;flex:none}
.active .who{font-weight:800;color:#b9e8ff;white-space:nowrap}
.active .doing{color:var(--mut);font-size:.83rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}

/* Agent grid */
.main{display:grid;grid-template-columns:minmax(0,1fr) 390px;gap:16px;align-items:start}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:16px;margin-bottom:18px}
.card{position:relative;background:var(--card);border:1px solid var(--card-bd);border-radius:18px;padding:18px;
  backdrop-filter:blur(12px);transition:transform .18s,border-color .2s,box-shadow .25s;overflow:hidden}
.card::before{content:"";position:absolute;inset:0 0 auto 0;height:3px;background:linear-gradient(90deg,transparent,transparent)}
.card:hover{transform:translateY(-3px);border-color:rgba(124,131,255,.34);box-shadow:0 16px 40px rgba(0,0,0,.4)}
.card.busy{border-color:rgba(52,211,153,.45);box-shadow:0 0 0 1px rgba(52,211,153,.25),0 16px 40px rgba(0,0,0,.4)}
.card.busy::before{background:linear-gradient(90deg,var(--ok),var(--acc));animation:flow 1.4s linear infinite;background-size:200% 100%}
.chead{display:flex;align-items:center;gap:11px;margin-bottom:14px}
.cemoji{width:40px;height:40px;border-radius:12px;display:grid;place-items:center;font-size:1.2rem;background:rgba(124,131,255,.12)}
.cname{font-weight:800;font-size:.98rem}
.cdesc{font-size:.7rem;color:var(--mut)}
.sdot{margin-left:auto;width:11px;height:11px;border-radius:50%;background:#39415c;flex:none;transition:.3s}
.sdot.run{background:var(--ok);box-shadow:0 0 0 4px rgba(52,211,153,.18);animation:pulse 1.4s infinite}
.sdot.done{background:var(--info);box-shadow:0 0 0 4px rgba(56,189,248,.14)}
.sdot.warn{background:var(--warn)}
.stat-word{font-size:.66rem;color:var(--mut);margin-left:8px;text-transform:uppercase;letter-spacing:.5px}

.tiles{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.tile{flex:1;min-width:64px;background:rgba(255,255,255,.025);border:1px solid rgba(255,255,255,.05);
  border-radius:11px;padding:9px 8px;text-align:center}
.tile .tv{font-size:1.32rem;font-weight:800;line-height:1.05;font-variant-numeric:tabular-nums;transition:color .3s}
.tile .tl{font-size:.6rem;color:var(--mut);text-transform:uppercase;letter-spacing:.5px;margin-top:3px}
.g{color:var(--ok)}.b{color:var(--info)}.a{color:var(--warn)}.r{color:var(--bad)}.v{color:var(--acc2)}

.bar{height:8px;border-radius:7px;background:rgba(255,255,255,.06);overflow:hidden;margin:4px 0 10px}
.bar i{display:block;height:100%;border-radius:7px;width:0;
  background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .6s cubic-bezier(.4,0,.2,1)}
.now{font-size:.71rem;color:var(--mut);margin-bottom:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:1.1em}
.now b{color:#aeb6cf;font-weight:600}
.lastact{display:flex;align-items:center;gap:8px;margin:2px 0 10px;padding:8px 10px;border-radius:11px;
  background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.055);font-size:.72rem;color:#aeb6cf}
.lastact .la-dot{width:7px;height:7px;border-radius:50%;background:var(--dim);flex:none}
.lastact.ok .la-dot{background:var(--ok)}.lastact.warn .la-dot{background:var(--warn)}.lastact.bad .la-dot{background:var(--bad)}
.lastact b{font-size:.62rem;text-transform:uppercase;letter-spacing:.6px;color:var(--mut);white-space:nowrap}
.lastact span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mini-feed{display:flex;flex-direction:column;gap:4px;margin-bottom:12px;max-height:128px;overflow:auto}
.mini-row{display:grid;grid-template-columns:48px 1fr;gap:8px;align-items:start;padding:5px 7px;border-radius:8px;
  background:rgba(255,255,255,.022);font-size:.68rem;color:#9fa8bf}
.mini-row .mt{font-family:ui-monospace,Menlo,monospace;color:var(--dim);font-size:.64rem}
.mini-row .mm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mini-row.bad{border-left:2px solid var(--bad)}.mini-row.warn{border-left:2px solid var(--warn)}.mini-row.ok{border-left:2px solid var(--ok)}
.mlist{margin:-2px 0 10px;display:flex;flex-direction:column;gap:3px;max-height:96px;overflow:auto}
.mi{font-size:.69rem;color:var(--warn);padding:2px 0;border-bottom:1px solid rgba(255,255,255,.04)}
.srcs{font-size:.68rem;color:var(--mut);line-height:1.7;margin-bottom:10px}
.srcs b{color:#8d96ae}
.acts{display:flex;gap:8px;flex-wrap:wrap}

/* Activity log */
.logwrap{background:var(--card);border:1px solid var(--card-bd);border-radius:18px;padding:16px 18px;backdrop-filter:blur(12px);
  position:sticky;top:18px;max-height:calc(100vh - 36px);overflow:hidden}
.logwrap h3{font-size:.7rem;color:var(--mut);text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.logwrap h3 .lz{width:7px;height:7px;border-radius:50%;background:var(--ok);animation:pulse 1.5s infinite}
.feed{display:flex;flex-direction:column;gap:1px;max-height:300px;overflow:auto;font-size:.75rem}
.le{display:grid;grid-template-columns:58px 1fr;gap:4px 10px;align-items:center;padding:7px 8px;border-radius:9px;transition:background .15s}
.le:hover{background:rgba(255,255,255,.03)}
.lt{color:var(--acc);font-family:ui-monospace,Menlo,monospace;white-space:nowrap;min-width:60px;opacity:.85}
.lag{display:inline-flex;align-items:center;gap:5px;padding:3px 8px;border-radius:999px;background:rgba(56,189,248,.08);
  border:1px solid rgba(56,189,248,.18);color:#b9e8ff;font-size:.66rem;font-weight:800;white-space:nowrap;justify-self:start}
.lst{grid-column:1/2;font-size:.58rem;text-transform:uppercase;letter-spacing:.3px;color:var(--mut);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:58px}
.lm{grid-column:2/3;color:#aeb6cf;overflow:hidden;text-overflow:ellipsis;white-space:normal;line-height:1.35}
.le.ok .lst{color:var(--ok)}.le.warn .lst{color:var(--warn)}.le.bad .lst{color:var(--bad)}
.empty{color:var(--dim);font-size:.78rem;padding:10px 4px}

.foot{text-align:center;font-size:.68rem;color:var(--dim);margin-top:20px}
.foot a{color:var(--acc)}

/* Toasts */
.toasts{position:fixed;right:18px;bottom:18px;display:flex;flex-direction:column;gap:9px;z-index:99}
.toast{padding:11px 15px;border-radius:11px;font-size:.78rem;font-weight:600;color:#fff;background:rgba(30,36,60,.95);
  border:1px solid var(--card-bd);box-shadow:0 12px 32px rgba(0,0,0,.5);backdrop-filter:blur(10px);
  display:flex;align-items:center;gap:9px;animation:toastin .3s ease;max-width:330px}
.toast.ok{border-color:rgba(52,211,153,.45)} .toast.err{border-color:rgba(251,113,133,.5)}
.toast.out{animation:toastout .3s ease forwards}

.spin{width:13px;height:13px;border-radius:50%;border:2px solid rgba(255,255,255,.25);border-top-color:#fff;display:inline-block;animation:spin .7s linear infinite}

@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(52,211,153,.55)}70%{box-shadow:0 0 0 9px rgba(52,211,153,0)}100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}
@keyframes flow{0%{background-position:0% 0}100%{background-position:200% 0}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes slidein{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
@keyframes toastin{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:none}}
@keyframes toastout{to{opacity:0;transform:translateX(20px)}}
@media(max-width:1080px){
  .main{grid-template-columns:1fr}
  .logwrap{position:relative;top:auto;max-height:none}
}
@media(max-width:760px){
  .grid{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div class="wrap">

  <div class="top">
    <div class="brand"><div class="logo">⚡</div>
      <div>Job Automation<small>Pipeline Control Center</small></div>
    </div>
    <div id="statusChip" class="chip idle"><span class="pdot"></span><span id="statusText">CONNECTING…</span></div>
    <div class="spacer"></div>
    <div class="conn" id="conn"><i></i><span id="connText">live</span></div>
    <div class="clock" id="clock">--:--:--</div>
  </div>

  <div class="gbar">
    <button class="btn primary" id="runAll" onclick="run('all')">🚀 Run Full Pipeline</button>
    <button class="btn danger"  id="stopAll" onclick="stop('all')">■ Stop All</button>
    <a class="btn ghost" href="/setup_sessions">🔐 Sessions</a>
    <button class="btn ghost" onclick="openExcel()">📂 Open Excel</button>
    <a class="btn ghost" href="/excel">⬇ Download Excel</a>
  </div>

  <div class="pills">
    <div class="pcard"><div class="pi i-hot">🔍</div><div><div class="pv" id="pHot">—</div><div class="pl">Hot Jobs</div></div></div>
    <div class="pcard"><div class="pi i-pdf">📄</div><div><div class="pv" id="pPdf">—</div><div class="pl">PDFs Built</div></div></div>
    <div class="pcard"><div class="pi i-sub">✅</div><div><div class="pv" id="pSub">—</div><div class="pl">Submitted</div></div></div>
    <div class="pcard"><div class="pi i-xls">📊</div><div><div class="pv" id="pXls">—</div><div class="pl">In Excel</div></div></div>
  </div>

  <div class="active" id="activeBanner">
    <div class="dot"></div>
    <div class="who" id="activeWho"></div>
    <div class="doing" id="activeMsg"></div>
  </div>

  <div class="main">
    <div class="grid" id="grid"></div>

    <div class="logwrap">
      <h3><span class="lz"></span> Live Activity</h3>
      <div class="feed" id="feed"><div class="empty">Connecting…</div></div>
    </div>
  </div>

  <div class="foot">Auto-refreshing · <a href="/setup_sessions">Setup login sessions</a> · each agent runs as its own process</div>
</div>

<div class="toasts" id="toasts"></div>

<script>
const CONF = [
  {id:"1",key:"a1",name:"Scout",emoji:"🔍",desc:"Finds & scores jobs",
   tiles:[{f:"raw",l:"Raw",c:"b"},{f:"filtered",l:"Filtered",c:""},{f:"hot",l:"Hot ≥60",c:"g"}],
   bar:null, srcs:true, run:"🔄 Re-run Scout"},
  {id:"2",key:"a2",name:"Writer",emoji:"✍️",desc:"Tailors resume + cover letter",
   tiles:[{f:"written",l:"Written",c:"g"},{f:"pdfs",l:"PDFs",c:"b"},{f:"remaining",l:"Left",c:"a"},{f:"failed",l:"Failed",c:"r"}],
   bar:"pct", now:"current", run:"▶ Resume Writer"},
  {id:"3",key:"a3",name:"Submitter",emoji:"🚀",desc:"Auto-submits applications",
   tiles:[{f:"pending",l:"Pending",c:"a"},{f:"submitted",l:"Submitted",c:"g"},{f:"review",l:"Review",c:"a"},{f:"handoff",l:"Syncing",c:"v"}],
   bar:null, now:"current", run:"▶ Resume Submitter"},
  {id:"4",key:"a4",name:"Tracker",emoji:"📊",desc:"Updates the Excel tracker",
   tiles:[{f:"rows",l:"Rows",c:"b"},{f:"submitted",l:"Submitted",c:"g"},{f:"review",l:"Review",c:"a"},{f:"ready",l:"Ready",c:""}], bar:null, excel:true, run:"🔄 Refresh Excel"},
  {id:"5",key:"a5",name:"Recruiter",emoji:"📧",desc:"Sends recruiter outreach",
   tiles:[{f:"sent",l:"Emails sent",c:"g"}], bar:null, run:"📧 Run Recruiter"},
];

function esc(s){return String(s==null?"":s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

function buildCards(){
  const g = document.getElementById('grid');
  g.innerHTML = CONF.map(c=>{
    const tiles = c.tiles.map(t=>
      `<div class="tile"><div class="tv ${t.c}" id="v_${c.id}_${t.f}">—</div><div class="tl">${t.l}</div></div>`).join('');
    const bar  = c.bar ? `<div class="bar"><i id="bar_${c.id}"></i></div>` : '';
    const srcs = c.srcs ? `<div class="srcs" id="srcs_${c.id}"></div>` : '';
    const now  = c.now ? `<div class="now" id="now_${c.id}"></div>` : '';
    const ml   = c.mlist ? `<div class="mlist" id="ml_${c.id}"></div>` : '';
    const ex   = c.excel ? `<a class="btn ghost sm" href="/excel">⬇ Excel</a>` : '';
    return `<div class="card" id="card_${c.id}">
      <div class="chead">
        <div class="cemoji">${c.emoji}</div>
        <div><div class="cname">Agent ${c.id} · ${c.name}</div><div class="cdesc">${c.desc}</div></div>
        <span class="sdot" id="dot_${c.id}"></span><span class="stat-word" id="sw_${c.id}"></span>
      </div>
      <div class="tiles">${tiles}</div>
      ${bar}${srcs}${ml}${now}
      <div class="lastact" id="la_${c.id}"><span class="la-dot"></span><b>Last</b><span>No recent activity</span></div>
      <div class="mini-feed" id="mf_${c.id}"></div>
      <div class="acts">
        <button class="btn primary sm" id="run_${c.id}" onclick="run('${c.id}')">${c.run}</button>
        <button class="btn danger sm"  id="stop_${c.id}" onclick="stop('${c.id}')">■ Stop</button>
        ${ex}
      </div>
    </div>`;
  }).join('');
}

function setText(id,val){const e=document.getElementById(id);if(e&&e.textContent!=String(val))e.textContent=val;}

function update(s){
  // header status
  const chip=document.getElementById('statusChip'), st=document.getElementById('statusText');
  if(s.procs && s.procs.any){chip.className='chip live';st.textContent='RUNNING';}
  else if(s.done){chip.className='chip done';st.textContent='COMPLETE';}
  else {chip.className='chip idle';st.textContent='IDLE';}
  setText('clock', s.ts);

  // pills
  setText('pHot', s.a1.hot); setText('pPdf', s.a2.pdfs);
  setText('pSub', s.a3.submitted); setText('pXls', s.a4.rows);

  // active banner
  const ab=document.getElementById('activeBanner');
  if(s.active_agent){ab.classList.add('show');setText('activeWho','▶ '+s.active_agent);setText('activeMsg',s.active_msg||'');}
  else ab.classList.remove('show');

  // global buttons
  document.getElementById('runAll').disabled  = !!(s.procs && s.procs.any);
  document.getElementById('stopAll').disabled = !(s.procs && s.procs.any);

  // cards
  for(const c of CONF){
    const d=s[c.key]||{}, busy=!!(s.procs && s.procs[c.id]);
    for(const t of c.tiles) setText(`v_${c.id}_${t.f}`, d[t.f]==null?'—':d[t.f]);
    if(c.bar){const b=document.getElementById('bar_'+c.id); if(b) b.style.width=(d.pct||0)+'%';}
    if(c.now){const e=document.getElementById('now_'+c.id);
      const showNow = d.current && (busy || c.id !== '3');
      if(e) e.innerHTML = showNow ? '<b>Now:</b> '+esc(d.current) : '';}
    if(c.srcs){const e=document.getElementById('srcs_'+c.id);
      if(e) e.innerHTML = (d.sources||'').split('|').filter(x=>x.includes(':')).slice(0,8)
            .map(p=>{const [k,v]=p.split(':');return '<b>'+esc(k.trim())+'</b>:'+esc((v||'').trim());}).join(' · ');}
    if(c.mlist){const e=document.getElementById('ml_'+c.id);
      if(e) e.innerHTML = (d.manual_list||[]).map(x=>'<div class="mi">🔐 '+esc(x)+'</div>').join('');}

    const act = (s.activity && s.activity.by_agent && s.activity.by_agent[c.id]) || [];
    let latest = act.length ? act[act.length-1] : null;
    if(c.id === '3'){
      latest = {
        severity: (d.pending || d.review || d.handoff) ? 'warn' : 'ok',
        stage: 'Status',
        m: d.status_summary || ''
      };
    }
    const la = document.getElementById('la_'+c.id);
    if(la){
      la.className = 'lastact ' + (latest ? latest.severity : '');
      la.innerHTML = `<span class="la-dot"></span><b>${latest ? esc(latest.stage) : 'Last'}</b><span>${latest ? esc(latest.m) : 'No recent activity'}</span>`;
    }
    const mf = document.getElementById('mf_'+c.id);
    if(mf){
      mf.innerHTML = act.slice(-2).reverse().map(x =>
        `<div class="mini-row ${esc(x.severity)}"><span class="mt">${esc(x.t)}</span><span class="mm">${esc(x.m)}</span></div>`
      ).join('');
    }

    const card=document.getElementById('card_'+c.id);
    const dot=document.getElementById('dot_'+c.id), sw=document.getElementById('sw_'+c.id);
    card.classList.toggle('busy', busy);
    if(busy){dot.className='sdot run'; sw.textContent='running';}
    else if(c.id==='3' && d.handoff > 0){dot.className='sdot warn'; sw.textContent='syncing';}
    else if(c.id==='3' && d.pending > 0){dot.className='sdot warn'; sw.textContent='pending';}
    else if(d.done){dot.className='sdot done'; sw.textContent='done';}
    else if(d.started){dot.className='sdot warn'; sw.textContent='partial';}
    else {dot.className='sdot'; sw.textContent='idle';}

    const rb=document.getElementById('run_'+c.id), stb=document.getElementById('stop_'+c.id);
    rb.disabled = busy || !!(s.procs && s.procs.all);
    if(busy && !rb.dataset.label){rb.dataset.label=rb.innerHTML; rb.innerHTML='<span class="spin"></span> Running';}
    else if(!busy && rb.dataset.label){rb.innerHTML=rb.dataset.label; delete rb.dataset.label;}
    stb.disabled = !busy;
  }

  // feed
  const feed=document.getElementById('feed');
  if(s.recent && s.recent.length){
    feed.innerHTML = s.recent.slice().reverse().map(e=>`<div class="le ${esc(e.severity)}">
      <span class="lt">${esc(e.t)}</span>
      <span class="lag">${esc(e.emoji||'•')} ${esc(e.agent_short||e.agent_name||'Agent')}</span>
      <span class="lst">${esc(e.stage||'Working')}</span>
      <span class="lm">${esc(e.m)}</span>
    </div>`).join('');
  } else feed.innerHTML='<div class="empty">No activity yet — start an agent above.</div>';
}

let failCount=0;
async function refresh(){
  try{
    const r=await fetch('/stats',{cache:'no-store'});
    const s=await r.json();
    failCount=0; setConn(true);
    update(s);
  }catch(e){ failCount++; if(failCount>1) setConn(false); }
}
function setConn(ok){
  const c=document.getElementById('conn');
  c.className='conn'+(ok?'':' bad');
  document.getElementById('connText').textContent = ok?'live':'reconnecting…';
}

async function run(a){
  const r=await act('/resume?agent='+a);
  toast(r.msg||'Started', r.ok);
}
async function stop(a){
  const r=await act('/stop?agent='+a);
  toast(r.msg||'Stopped', r.ok!==false);
}
async function openExcel(){
  const r=await act('/open_excel');
  toast(r.msg||'Opening…', r.ok);
}
async function act(url){
  try{ const r=await fetch(url,{method:'POST'}); const j=await r.json(); refresh(); setTimeout(refresh,600); return j; }
  catch(e){ return {ok:false,msg:'Request failed'}; }
}

let toastN=0;
function toast(msg, ok){
  const wrap=document.getElementById('toasts');
  const id='t'+(toastN++);
  const el=document.createElement('div');
  el.className='toast '+(ok?'ok':(ok===false?'err':''));
  el.innerHTML=(ok?'✅ ':(ok===false?'⚠️ ':'• '))+esc(msg);
  el.id=id; wrap.appendChild(el);
  setTimeout(()=>{el.classList.add('out'); setTimeout(()=>el.remove(),300);}, 3200);
}

buildCards();
refresh();
setInterval(refresh, 2000);
</script>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# SESSION SETUP PAGE  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────
SESSION_COMPANIES = {
    "stripe":     ("Stripe",      "sessions/stripe.json",     "https://stripe.com/login"),
    "okta":       ("Okta",        "sessions/okta.json",       "https://www.okta.com/company/careers/"),
    "airbnb":     ("Airbnb",      "sessions/airbnb.json",     "https://careers.airbnb.com/"),
    "databricks": ("Databricks",  "sessions/databricks.json", "https://www.databricks.com/company/careers"),
    "linkedin":   ("LinkedIn",    "sessions/linkedin.json",   "https://www.linkedin.com/login"),
}

SETUP_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Sessions</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,sans-serif;background:#080a12;color:#cbd5e1;padding:28px;max-width:640px;margin:0 auto}
h1{color:#f1f5f9;font-size:1.2rem;margin-bottom:6px}
.sub{color:#64748b;font-size:.82rem;margin-bottom:22px}
.co{background:#12172a;border:1px solid #1e293b;border-radius:10px;padding:16px;margin:10px 0}
.co h3{color:#94a3b8;font-size:.78rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
.st{font-size:.76rem;color:#475569;margin-bottom:10px}
.st.ok{color:#4ade80}
.btn{display:inline-block;padding:8px 15px;background:#6366f1;color:#fff;border-radius:8px;
     font-size:.78rem;font-weight:600;cursor:pointer;border:none;margin-right:8px}
.btn:hover{background:#4f46e5}
.note{font-size:.72rem;color:#475569;margin-top:6px}
.back{display:inline-block;margin-top:20px;padding:8px 15px;background:#1e293b;color:#818cf8;
      border-radius:8px;font-size:.78rem;font-weight:600}
</style></head><body>
<h1>🔐 Session Setup</h1>
<div class="sub">Save a login session once per company. Agent 3 auto-reuses it — no manual login needed on future runs.</div>
{BLOCKS}
<a class="back" href="/">← Back to Dashboard</a>
</body></html>"""


def render_setup() -> str:
    blocks = ""
    for key, (name, sf, url) in SESSION_COMPANIES.items():
        exists = (BASE / sf).exists()
        st_cls  = "st ok" if exists else "st"
        st_txt  = f"✅ Saved ({sf})" if exists else "❌ Not set up"
        lbl     = "🔄 Refresh Session" if exists else "🔐 Open Login Browser"
        blocks += f"""<div class="co">
  <h3>{name}</h3>
  <div class="{st_cls}">{st_txt}</div>
  <a class="btn" href="/launch_session?co={key}">{lbl}</a>
  <div class="note">Opens a visible Chrome window at {url}<br>
  Log in → come back here → your session is saved automatically when you close the browser.</div>
</div>"""
    return SETUP_HTML.replace("{BLOCKS}", blocks)


def launch_session_setup(key: str):
    """Open a visible browser for the user to log in."""
    if key not in SESSION_COMPANIES:
        return
    name, sf, url = SESSION_COMPANIES[key]
    script = f"""
import asyncio
from playwright.async_api import async_playwright
from pathlib import Path
import time

async def run():
    async with async_playwright() as pw:
        browser = await pw.webkit.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto("{url}")
        print("Log in to {name} in the browser window.")
        print("The session will be saved when you close the browser.")
        while True:
            try:
                await page.title()
                await asyncio.sleep(2)
            except:
                break
        Path("{sf}").parent.mkdir(exist_ok=True)
        await ctx.storage_state(path="{sf}")
        print("Session saved to {sf}")

asyncio.run(run())
"""
    threading.Thread(
        target=lambda: subprocess.run([sys.executable, "-c", script], cwd=str(BASE)),
        daemon=True
    ).start()


# ─────────────────────────────────────────────────────────────────────────────
# HTTP HANDLER
# ─────────────────────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)

        if p.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode())

        elif p.path == "/stats":
            self._send(200, "application/json", json.dumps(collect_stats()).encode())

        elif p.path == "/excel":
            if not EXCEL.exists():
                return self._send(404, "text/plain", b"Excel not created yet")
            data = EXCEL.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition",
                'attachment; filename="Akshit_Applications.xlsx"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        elif p.path == "/setup_sessions":
            self._send(200, "text/html", render_setup().encode())

        elif p.path == "/launch_session":
            launch_session_setup(q.get("co", [""])[0])
            self._send(200, "text/html",
                ("<meta http-equiv='refresh' content='2;url=/setup_sessions'>"
                 "<body style='font-family:sans-serif;background:#080a12;color:#cbd5e1;padding:30px'>"
                 "Opening browser… <a href='/setup_sessions' style='color:#818cf8'>back</a></body>"
                ).encode())

        # Allow GET fallbacks for these (handy for direct links/testing)
        elif p.path in ("/resume", "/stop", "/open_excel"):
            self._action(p.path, q)

        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)
        if p.path in ("/resume", "/stop", "/open_excel"):
            self._action(p.path, q)
        else:
            self.send_response(404); self.end_headers()

    def _action(self, path, q):
        agent = q.get("agent", ["all"])[0]
        if path == "/resume":
            ok, msg = launch_resume(agent)
        elif path == "/stop":
            ok, msg = stop_resume(agent)
        else:  # /open_excel
            ok, msg = self._open_excel()
        self._send(200, "application/json", json.dumps({"ok": ok, "msg": msg}).encode())

    def _open_excel(self):
        if not EXCEL.exists():
            return False, "Excel not created yet"
        try:
            subprocess.Popen(["open", str(EXCEL)])
            return True, "Opening Excel…"
        except Exception as e:
            return False, str(e)

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    os.chdir(str(BASE))
    Path("logs").mkdir(exist_ok=True)
    Path("sessions").mkdir(exist_ok=True)
    server = ThreadingHTTPServer(("localhost", PORT), H)
    print(f"\n  ⚡ Job Automation dashboard → http://localhost:{PORT}")
    print(f"  Open that URL in your browser. Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
