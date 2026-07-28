"""
SMART RESUME — picks up exactly where the pipeline left off.

Identifies which Agent 2 jobs failed (no application folder),
re-queues only those, then runs agents 2 → 3 → 4 → 5.
Agent 1 is skipped unless --full flag is passed.

Usage:
  python3 resume_pipeline.py            # resume from failure point
  python3 resume_pipeline.py --full     # full pipeline (re-runs Agent 1 too)
  python3 resume_pipeline.py --agent2   # only re-run Agent 2
  python3 resume_pipeline.py --agent3   # only re-run Agent 3 (re-submit)
"""

import asyncio, argparse, json, logging, os, re, sys
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).parent))
os.makedirs("logs", exist_ok=True)

# Configure root logger ONCE — prevents duplicate lines when agent modules
# call basicConfig again (they check if handlers already exist)
root = logging.getLogger()
if not root.handlers:
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(message)s")
    fh  = logging.FileHandler("logs/orchestrator.log", mode="a")
    sh  = logging.StreamHandler()
    fh.setFormatter(fmt); sh.setFormatter(fmt)
    root.addHandler(fh); root.addHandler(sh)

log = logging.getLogger("RESUME")


def find_failed_agent2_jobs() -> list[dict]:
    """Return hot jobs that have no application folder (Agent 2 didn't process them).
    Skips companies on the Agent 3 ignore-list and junk-company entries up front —
    no point spending pdflatex + Claude time tailoring a resume Agent 3 will never
    submit anyway."""
    from config.profile import PROFILE

    skip = [s.lower() for s in PROFILE.get("agent3_skip_companies", [])]

    def is_skipped(company: str) -> bool:
        c = _norm_company(company)
        return any(s in c for s in skip)

    cap = PROFILE.get("max_roles_per_company", 3)

    done_ids = set()
    for app_json in Path("outputs/applications").glob("*/application.json"):
        try:
            j = json.loads(app_json.read_text())
            done_ids.add(j.get("id", ""))
        except Exception:
            pass

    # Gather all real, non-skipped candidates (incl. already-built — they're needed
    # so the per-company cap counts them and we don't exceed `cap` distinct roles).
    candidates: dict[str, dict] = {}
    dropped_skip = 0
    tracker = Path("outputs/tracker_queue.jsonl")
    if tracker.exists():
        for line in tracker.read_text().strip().splitlines():
            if not line.strip():
                continue
            try:
                j = json.loads(line)
                if j.get("score", 0) < PROFILE["score_threshold"]:
                    continue
                co = _clean_company(j.get("company", ""))
                if is_skipped(co) or not _looks_like_real_company(co):
                    dropped_skip += 1
                    continue
                j["company"] = co
                jid = j.get("id")
                if jid and (jid not in candidates or j.get("score", 0) > candidates[jid].get("score", 0)):
                    candidates[jid] = j
            except Exception:
                pass

    # Collapse duplicate roles + keep at most `cap` distinct teams per company,
    # then build only the curated ones we haven't built yet. Mirrors Agent 3 so we
    # never tailor a resume that the per-company cap would discard at submission.
    try:
        from core.dedupe import dedupe_applications
        curated = dedupe_applications(list(candidates.values()), max_per_company=cap)
    except Exception as e:
        log.warning(f"Agent 2 de-dupe skipped: {e}")
        curated = list(candidates.values())

    trimmed_by_cap = len(candidates) - len(curated)
    failed = [j for j in curated if j.get("id") not in done_ids]

    log.info(f"Already done: {len(done_ids)} | Skipped (ignore-list/junk): {dropped_skip} "
             f"| trimmed by cap/dupe: {trimmed_by_cap} | Still need Agent 2: {len(failed)}")
    return failed


def _norm_company(name: str) -> str:
    return (name or "").strip().lower()


# Junk "company" values that leak in from bad upstream parsing (URLs, cover-note
# fragments, recruiter names, emails) — Agent 3 must not try to apply to these.
_BAD_COMPANY_BITS = ("http", "://", "www.", "&#x", "@", "|")
_SENTENCE_BITS = ("i just applied", "i'm a", "i am a", "thanks for", "sent you",
                  "just applied", "hi atul", "hello ", "dear ", "i recently")


def _clean_company(name: str) -> str:
    """Strip a trailing parenthetical (often an appended URL) so 'CoPlane ( http… )'
    becomes 'CoPlane'."""
    return re.sub(r"\s*\(.*$", "", (name or "").strip()).strip()


def _looks_like_real_company(name: str) -> bool:
    c  = (name or "").strip()
    cl = c.lower()
    if not c or cl in ("name", "unknown", "n/a", "none", "?", "company"):
        return False
    if any(b in cl for b in _BAD_COMPANY_BITS):
        return False
    if any(s in cl for s in _SENTENCE_BITS):
        return False
    if len(c) > 45 or len(c.split()) > 6:   # sentences, not company names
        return False
    return True


def find_failed_agent3_jobs() -> list[dict]:
    """Build the Agent 3 to-apply list with hard guard-rails:

    1. Skip companies in PROFILE['agent3_skip_companies'] entirely (already done).
    2. Never exceed `max_roles_per_company` applications PER COMPANY, COUNTING the
       ones already sent out — so a company that already has applications gone gets
       only its remaining slots (e.g. 1 gone, cap 3 → at most 2 new).
    3. Treat submitted / needs_review / needs_manual / failed as already processed
       so login-walled or reviewed jobs aren't retried forever.
    """
    from config.profile import PROFILE
    cap  = PROFILE.get("max_roles_per_company", 3)
    skip = [s.lower() for s in PROFILE.get("agent3_skip_companies", [])]

    def is_skipped(company: str) -> bool:
        c = _norm_company(company)
        return any(s in c for s in skip)

    # ── Already-processed jobs + how many have GONE OUT per company ───────────
    # Read the SAME full history the submitter checks (run_from_queue), so the
    # builder doesn't keep re-proposing jobs that were already submitted. Last
    # line wins per job_id (latest outcome), matching the submitter's ordering.
    latest_record: dict[str, dict] = {}
    for submitted_path in [
        Path("outputs/agent4_submitted_history.jsonl"),
        Path("outputs/agent4_submitted.bak"),
        Path("outputs/agent4_submitted.jsonl"),
    ]:
        if not submitted_path.exists():
            continue
        for line in submitted_path.read_text(errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                j   = json.loads(line)
                jid = j.get("job_id") or j.get("id")
                if jid:
                    latest_record[jid] = j
            except Exception:
                pass

    processed_ids = set(latest_record.keys())
    processed_status = {}
    gone_per_company: dict[str, int] = {}
    for jid, j in latest_record.items():
        processed_status[jid] = j.get("status", "processed")
        comp = _norm_company(j.get("company", ""))
        gone_per_company[comp] = gone_per_company.get(comp, 0) + 1

    # All apps that were written by Agent 2 (drop skipped + junk companies up front)
    all_written = []
    dropped_junk = 0
    for app_json in sorted(Path("outputs/applications").glob("*/application.json")):
        try:
            j = json.loads(app_json.read_text())
        except Exception:
            continue
        co = _clean_company(j.get("company", ""))
        if is_skipped(co):
            continue
        if not _looks_like_real_company(co):
            dropped_junk += 1
            continue
        j["company"] = co            # normalise for dedupe + form filling
        all_written.append(j)
    if dropped_junk:
        log.info(f"Dropped {dropped_junk} junk-company entries (URLs/sentences/emails)")

    # ── Curate: collapse duplicate roles, at most `cap` distinct teams/company ─
    try:
        from core.dedupe import dedupe_applications, summarise
        curated = dedupe_applications(all_written, max_per_company=cap)
        log.info("\n" + summarise(all_written, curated))
    except Exception as e:
        log.warning(f"De-dupe skipped: {e}")
        curated = all_written

    # Candidates = curated, not yet processed (sorted by score desc by dedupe)
    candidates = [j for j in curated if j.get("id") not in processed_ids]

    # ── Enforce per-company total cap, COUNTING applications already gone ──────
    to_submit: list[dict] = []
    new_per_company: dict[str, int] = {}
    capped_out: dict[str, int] = {}
    for j in candidates:
        comp  = _norm_company(j.get("company", ""))
        total = gone_per_company.get(comp, 0) + new_per_company.get(comp, 0)
        if total >= cap:
            capped_out[comp] = capped_out.get(comp, 0) + 1
            continue
        to_submit.append(j)
        new_per_company[comp] = new_per_company.get(comp, 0) + 1

    status_counts = {}
    for status in processed_status.values():
        status_counts[status] = status_counts.get(status, 0) + 1
    status_note = ", ".join(f"{k}: {v}" for k, v in sorted(status_counts.items())) or "none"
    log.info(f"Skip companies: {skip or 'none'} | cap {cap}/company (counts already-sent)")
    log.info(f"Curated {len(curated)} | already gone {len(processed_ids)} ({status_note}) | "
             f"trimmed by cap {sum(capped_out.values())} | need Agent 3: {len(to_submit)}")
    return to_submit


def find_review_jobs() -> list[dict]:
    """Return application packages whose LATEST Agent 3 outcome was needs_review,
    so we can re-attempt them after improving the form filler. Skips blocklisted
    and junk companies. needs_manual (auth wall / CAPTCHA) is intentionally NOT
    retried — those need a saved session, not better field-filling."""
    from config.profile import PROFILE
    skip = [s.lower() for s in PROFILE.get("agent3_skip_companies", [])]

    def is_skipped(company: str) -> bool:
        c = _norm_company(company)
        return any(s in c for s in skip)

    latest: dict[str, str] = {}
    for submitted_path in [
        Path("outputs/agent4_submitted_history.jsonl"),
        Path("outputs/agent4_submitted.bak"),
        Path("outputs/agent4_submitted.jsonl"),
    ]:
        if submitted_path.exists():
            for line in submitted_path.read_text().strip().splitlines():
                if not line.strip():
                    continue
                try:
                    j = json.loads(line)
                    jid = j.get("job_id") or j.get("id")
                    if jid:
                        latest[jid] = j.get("status", "")
                except Exception:
                    pass
    review_ids = {jid for jid, st in latest.items() if st == "needs_review"}

    jobs = []
    for app_json in sorted(Path("outputs/applications").glob("*/application.json")):
        try:
            j = json.loads(app_json.read_text())
        except Exception:
            continue
        if j.get("id") not in review_ids:
            continue
        co = _clean_company(j.get("company", ""))
        if is_skipped(co) or not _looks_like_real_company(co):
            continue
        j["company"] = co
        j["_force_retry"] = True
        jobs.append(j)

    log.info(f"needs_review to re-submit: {len(jobs)} (of {len(review_ids)} flagged)")
    return jobs


async def run_agent2_on_jobs(jobs: list[dict]):
    """Re-queue specific jobs and run Agent 2."""
    if not jobs:
        log.info("No jobs to process for Agent 2.")
        return

    # Write to queue
    queue_path = Path("outputs/agent2_queue.jsonl")
    with open(queue_path, "w") as f:
        for j in jobs:
            f.write(json.dumps(j) + "\n")
    log.info(f"Queued {len(jobs)} jobs for Agent 2")

    from agents.agent2_writer import run_from_queue
    await run_from_queue()


async def run_agent3_on_jobs(jobs: list[dict]):
    """Re-queue specific jobs and run Agent 3."""
    if not jobs:
        log.info("No jobs to process for Agent 3.")
        return

    # Write to queue
    queue_path = Path("outputs/agent3_queue.jsonl")
    with open(queue_path, "w") as f:
        for j in jobs:
            f.write(json.dumps(j) + "\n")
    log.info(f"Queued {len(jobs)} jobs for Agent 3")

    from agents.agent3_submitter import run_from_queue as submitter_queue
    await submitter_queue()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full",   action="store_true", help="Re-run full pipeline including Agent 1")
    parser.add_argument("--agent1", action="store_true", help="Only run Agent 1")
    parser.add_argument("--agent2", action="store_true", help="Only run Agent 2 (failed jobs)")
    parser.add_argument("--agent3", action="store_true", help="Only run Agent 3 (unsubmitted)")
    parser.add_argument("--agent4", action="store_true", help="Only run Agent 4")
    parser.add_argument("--agent5", action="store_true", help="Only run Agent 5")
    parser.add_argument("--retry-review", action="store_true",
                        help="Re-submit jobs previously flagged needs_review (after filler fixes)")
    args = parser.parse_args()

    # ── Retry needs_review jobs (standalone mode) ─────────────────────────────
    if args.retry_review:
        log.info("══════════════════════════════════")
        log.info("  RETRY needs_review — re-submitting with improved form filler")
        log.info("══════════════════════════════════")
        jobs = find_review_jobs()
        if jobs:
            await run_agent3_on_jobs(jobs)
            log.info(f"✅ Retry complete — re-attempted {len(jobs)} submissions")
        else:
            log.info("Nothing flagged needs_review to retry.")
        return

    log.info(f"══════════════════════════════════")
    log.info(f"  RESUME PIPELINE — {datetime.now().strftime('%d %b %Y %H:%M')}")
    log.info(f"══════════════════════════════════")

    run_all = not any([args.full, args.agent1, args.agent2, args.agent3, args.agent4, args.agent5])

    # ── Agent 1 ──────────────────────────────────────────────────────────────
    if args.full or args.agent1:
        log.info("▶ AGENT 1 — Scout")
        from agents.agent1_scout import run_scout
        await run_scout()
        log.info("✅ Agent 1 complete")

    # ── Agent 2 ──────────────────────────────────────────────────────────────
    if run_all or args.full or args.agent2:
        log.info("▶ AGENT 2 — Resume (failed jobs only)")
        failed = find_failed_agent2_jobs()
        if failed:
            await run_agent2_on_jobs(failed)
            log.info(f"✅ Agent 2 complete — processed {len(failed)} jobs")
        else:
            log.info("✅ Agent 2 — nothing to resume (all jobs done)")

    # ── Agent 3 ──────────────────────────────────────────────────────────────
    if run_all or args.full or args.agent3:
        log.info("▶ AGENT 3 — Submit (unsubmitted applications)")
        to_submit = find_failed_agent3_jobs()
        if to_submit:
            await run_agent3_on_jobs(to_submit)
            log.info(f"✅ Agent 3 complete — attempted {len(to_submit)} submissions")
        else:
            log.info("✅ Agent 3 — nothing to resume")

    # ── Agent 4 ──────────────────────────────────────────────────────────────
    if run_all or args.full or args.agent4:
        log.info("▶ AGENT 4 — Tracker")
        from agents.agent4_tracker import run_tracker
        await run_tracker()
        log.info("✅ Agent 4 complete")

    # ── Agent 5 ──────────────────────────────────────────────────────────────
    if run_all or args.full or args.agent5:
        log.info("▶ AGENT 5 — Recruiter")
        from agents.agent5_recruiter import run_from_queue as recruiter_queue
        await recruiter_queue()
        log.info("✅ Agent 5 complete")

    log.info("══ Resume complete ══")


if __name__ == "__main__":
    asyncio.run(main())
