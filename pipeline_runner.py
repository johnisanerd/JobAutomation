"""
RESUMABLE PIPELINE RUNNER
─────────────────────────
Drives each agent's existing per-item functions while tracking per-item
progress in state/agent{N}.json, so any step can be CONTINUED from exactly
where it failed (already-completed items are skipped, failures are retried).

Usage (normally launched by web_dashboard.py, but works standalone too):
    python3 pipeline_runner.py agent3            # continue/run one step
    python3 pipeline_runner.py all               # run agents 1→5 in sequence
    python3 pipeline_runner.py agent3 --restart  # forget progress, start over

State written per step:  state/agentN.json
PID + active step:       state/runner.json
"""

import asyncio, json, logging, os, signal, sys, time, traceback
from datetime import datetime
from pathlib import Path

ROOT  = Path(__file__).resolve().parent
os.chdir(ROOT)
STATE = ROOT / "state"
STATE.mkdir(exist_ok=True)
(ROOT / "logs").mkdir(exist_ok=True)

# ── Step metadata ─────────────────────────────────────────────────────────────
AGENTS = {
    "agent1": {"label": "Scout",     "desc": "Finds & scores jobs",            "logger": "agent1_scout"},
    "agent2": {"label": "Writer",    "desc": "Tailors resume + cover letter",  "logger": "agent2_writer"},
    "agent3": {"label": "Submitter", "desc": "Auto-submits applications",      "logger": "agent3_submitter"},
    "agent4": {"label": "Tracker",   "desc": "Updates the Excel tracker",      "logger": "agent4_tracker"},
    "agent5": {"label": "Recruiter", "desc": "Sends recruiter outreach",       "logger": "agent5_recruiter"},
}
ORDER = ["agent1", "agent2", "agent3", "agent4", "agent5"]

# Item-source queue for each resumable step (agent1 is a single batch step).
SOURCES = {
    "agent2": ("outputs/agent2_queue.jsonl",   None),
    "agent3": ("outputs/agent3_queue.jsonl",   None),
    "agent4": ("outputs/agent4_submitted.jsonl", None),
    "agent5": ("outputs/agent4_submitted.jsonl", lambda d: d.get("status") == "submitted"),
}

_STOP = False  # flipped by SIGTERM so we exit cleanly between items


# ── State helpers ─────────────────────────────────────────────────────────────
def state_path(agent: str) -> Path:
    return STATE / f"{agent}.json"


def load_state(agent: str) -> dict:
    p = state_path(agent)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {
        "agent": agent, "label": AGENTS[agent]["label"], "desc": AGENTS[agent]["desc"],
        "status": "idle", "total": 0, "done_count": 0, "failed_count": 0,
        "current": None, "done_ids": [], "failed": [],
        "started_at": "", "updated_at": "", "message": "",
    }


def save_state(st: dict):
    st["updated_at"] = datetime.now().isoformat(timespec="seconds")
    state_path(st["agent"]).write_text(json.dumps(st, indent=2))


def set_runner(active: str | None, status: str):
    (STATE / "runner.json").write_text(json.dumps({
        "pid": os.getpid(), "active": active, "status": status,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))


# ── Item loading ──────────────────────────────────────────────────────────────
def item_id(it: dict) -> str:
    return str(it.get("id") or it.get("job_id") or "")


def load_items(agent: str) -> list[dict]:
    path, filt = SOURCES[agent]
    p = ROOT / path
    if not p.exists():
        return []
    out: dict[str, dict] = {}  # dedupe by id, keep last occurrence
    for line in p.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if filt and not filt(d):
            continue
        out[item_id(d)] = d
    return list(out.values())


# ── Per-item ERROR capture (for agents whose item-fn swallows exceptions) ──────
class CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records: list[tuple[int, str]] = []

    def emit(self, record):
        try:
            self.records.append((record.levelno, record.getMessage()))
        except Exception:
            pass

    def clear(self):
        self.records = []

    def errors(self):
        return [m for lvl, m in self.records if lvl >= logging.ERROR]

    def has(self, needle):
        return any(needle in m for _, m in self.records)


def attach_capture(logger_name: str) -> CaptureHandler:
    cap = CaptureHandler()
    logging.getLogger(logger_name).addHandler(cap)
    return cap


# ── Failure record helper ─────────────────────────────────────────────────────
def fail_entry(it: dict, reason: str) -> dict:
    return {
        "id": item_id(it),
        "title": it.get("title", ""),
        "company": it.get("company", ""),
        "reason": (reason or "unknown error").strip()[:500],
        "ts": datetime.now().isoformat(timespec="seconds"),
    }


# ── The resumable item loop (agents 2-5) ──────────────────────────────────────
def run_item_step(agent: str):
    meta  = AGENTS[agent]
    items = load_items(agent)
    st    = load_state(agent)

    done = set(st.get("done_ids", []))
    st.update(status="running", total=len(items), done_count=len(done),
              failed=[], failed_count=0, message="",
              started_at=datetime.now().isoformat(timespec="seconds"))
    save_state(st)
    set_runner(agent, "running")

    if not items:
        st.update(status="done", current=None, message="No items in queue.")
        save_state(st)
        return

    # Import the agent module so its named logger exists, then capture from it.
    if   agent == "agent2": from agents.agent2_writer    import process_job as fn
    elif agent == "agent3": from agents.agent3_submitter import submit_application as fn
    elif agent == "agent4": from agents.agent4_tracker   import log_jobs as fn
    elif agent == "agent5": from agents.agent5_recruiter import process_job as fn
    cap = attach_capture(meta["logger"])

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for it in items:
        if _STOP:
            st.update(status="stopped", current=None, message="Stopped by user.")
            save_state(st)
            loop.close()
            return

        iid = item_id(it)
        if iid in done:          # already completed in a previous run — skip
            continue

        st["current"] = {"id": iid, "title": it.get("title", ""),
                         "company": it.get("company", "")}
        save_state(st)
        cap.clear()

        try:
            if agent == "agent3":
                res    = loop.run_until_complete(fn(it))
                status = (res or {}).get("status", "failed")
                notes  = (res or {}).get("notes", "")
                if status == "submitted":
                    done.add(iid)
                else:
                    st["failed"].append(fail_entry(it, f"{status}: {notes}"))

            elif agent == "agent4":
                loop.run_until_complete(fn([it]))     # batch-of-one into Excel
                done.add(iid)

            elif agent == "agent2":
                loop.run_until_complete(fn(it))
                if cap.has("✅ Done"):
                    done.add(iid)
                elif cap.errors():
                    st["failed"].append(fail_entry(it, cap.errors()[-1]))
                else:                                  # no clear signal → trust output
                    done.add(iid)

            else:  # agent5
                loop.run_until_complete(fn(it))
                if cap.errors():
                    st["failed"].append(fail_entry(it, cap.errors()[-1]))
                else:
                    done.add(iid)

        except Exception as e:
            st["failed"].append(fail_entry(it, f"{e}\n{traceback.format_exc()[-400:]}"))

        st["done_ids"]     = sorted(done)
        st["done_count"]   = len([i for i in (item_id(x) for x in items) if i in done])
        st["failed_count"] = len(st["failed"])
        save_state(st)

    st.update(status="done", current=None,
              message=f"Completed — {st['done_count']}/{st['total']} done, "
                      f"{st['failed_count']} need attention.")
    save_state(st)
    loop.close()


# ── Agent 1 — single batch step (not item-resumable) ──────────────────────────
def run_agent1():
    st = load_state("agent1")
    st.update(status="running", total=1, done_count=0, failed=[], failed_count=0,
              current={"id": "scout", "title": "Fetching & scoring jobs", "company": ""},
              message="", started_at=datetime.now().isoformat(timespec="seconds"))
    save_state(st)
    set_runner("agent1", "running")

    from agents.agent1_scout import run_scout
    cap  = attach_capture("agent1_scout")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_scout())
        st.update(status="done", done_count=1, current=None, message="Scout complete.")
    except Exception as e:
        st["failed"] = [fail_entry({"id": "scout", "title": "Scout run"}, str(e))]
        st.update(status="error", failed_count=1, current=None, message=str(e)[:200])
    finally:
        if cap.errors() and st["status"] != "error":
            st["message"] = "Completed with warnings — see logs."
        save_state(st)
        loop.close()


def run_step(agent: str):
    if agent == "agent1":
        run_agent1()
    else:
        run_item_step(agent)


# ── Entrypoint ────────────────────────────────────────────────────────────────
def _handle_term(*_):
    global _STOP
    _STOP = True


def main():
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    args    = sys.argv[1:]
    restart = "--restart" in args
    targets = [a for a in args if not a.startswith("--")]
    if not targets:
        print("usage: pipeline_runner.py <agent1..agent5|all> [--restart]")
        sys.exit(1)

    steps = ORDER if targets[0] == "all" else targets

    if restart:                       # forget prior progress for these steps
        for a in steps:
            st = load_state(a)
            st.update(status="idle", done_ids=[], done_count=0, failed=[],
                      failed_count=0, current=None, total=0, message="Reset.")
            save_state(st)

    set_runner(steps[0], "running")
    try:
        for a in steps:
            if _STOP:
                break
            run_step(a)
            if _STOP:
                break
            # Stop an "all" run if a step ended in hard error.
            if load_state(a).get("status") == "error":
                break
    finally:
        set_runner(None, "stopped" if _STOP else "idle")


if __name__ == "__main__":
    main()
