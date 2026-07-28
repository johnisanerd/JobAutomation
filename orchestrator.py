"""
MASTER ORCHESTRATOR
Runs all 5 agents in the correct sequence with a single command.
  python orchestrator.py          — run full pipeline now
  python orchestrator.py --schedule  — run on daily schedule
"""

import asyncio, argparse, logging, os
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [ORCH] %(message)s",
    handlers=[logging.FileHandler("logs/orchestrator.log"), logging.StreamHandler()])
log = logging.getLogger("orchestrator")

os.makedirs("logs", exist_ok=True)
os.makedirs("outputs/applications", exist_ok=True)
os.makedirs("outputs/screenshots", exist_ok=True)

async def run_pipeline():
    log.info("══════════════════════════════════════════")
    log.info(f"  JOB AUTOMATION PIPELINE — {datetime.now().strftime('%d %b %Y %H:%M')}")
    log.info("══════════════════════════════════════════")

    # ── Agent 1: Scout ────────────────────────────────────────────────────────
    log.info("▶ AGENT 1 — Job Scout")
    from agents.agent1_scout import run_scout
    await run_scout()
    log.info("✅ Agent 1 complete\n")

    # ── Agent 2: Writer ───────────────────────────────────────────────────────
    log.info("▶ AGENT 2 — Application Writer")
    from agents.agent2_writer import run_from_queue as writer_queue
    await writer_queue()
    log.info("✅ Agent 2 complete\n")

    # ── Agent 3: Submitter ────────────────────────────────────────────────────
    log.info("▶ AGENT 3 — Submitter")
    from agents.agent3_submitter import run_from_queue as submitter_queue
    await submitter_queue()
    log.info("✅ Agent 3 complete\n")

    # ── Agent 4: Tracker ──────────────────────────────────────────────────────
    log.info("▶ AGENT 4 — Tracker")
    from agents.agent4_tracker import run_tracker
    await run_tracker()
    log.info("✅ Agent 4 complete\n")

    # ── Agent 5: Recruiter Scout ──────────────────────────────────────────────
    log.info("▶ AGENT 5 — Recruiter Scout")
    from agents.agent5_recruiter import run_from_queue as recruiter_queue
    await recruiter_queue()
    log.info("✅ Agent 5 complete\n")

    log.info("══ Pipeline complete ══")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--schedule", action="store_true",
                        help="Run on daily schedule instead of once")
    args = parser.parse_args()

    if args.schedule:
        scheduler = AsyncIOScheduler(timezone="Europe/Dublin")
        scheduler.add_job(run_pipeline, "cron", hour=7, minute=30)
        scheduler.start()
        log.info("Scheduled: pipeline runs daily at 07:30 Dublin. Ctrl+C to stop.")
        try:
            asyncio.get_event_loop().run_forever()
        except (KeyboardInterrupt, SystemExit):
            pass
    else:
        asyncio.run(run_pipeline())
