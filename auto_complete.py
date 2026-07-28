"""
AUTO-COMPLETE — runs the pipeline to 100% completion, unattended.

What it does, in a loop until everything is done:
  1. Checks Anthropic credits. If exhausted, waits and retries (handles limit resets).
  2. Resumes Agent 2 for any unwritten jobs.
  3. Runs Agent 3 to submit everything written.
  4. Runs Agent 4 (Excel) + Agent 5 (recruiter).
  5. Stops when all hot jobs have applications AND the tracker is up to date.

Safe to leave running. It will pause (not crash) when credits run out,
re-check every few minutes, and pick up exactly where it left off.

Run:
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 auto_complete.py
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 auto_complete.py --loop   # keep retrying on credit limits
"""

import asyncio, argparse, json, logging, os, sys, time
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)
sys.path.insert(0, str(Path(__file__).parent))
os.makedirs("logs", exist_ok=True)

root = logging.getLogger()
if not root.handlers:
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(message)s")
    for h in (logging.FileHandler("logs/orchestrator.log", mode="a"), logging.StreamHandler()):
        h.setFormatter(fmt); root.addHandler(h)
log = logging.getLogger("AUTO")

CREDIT_WAIT_SECONDS = 300   # re-check credits every 5 min when exhausted


def credits_ok() -> bool:
    """Cheap probe: 1-token call. Returns False on credit/balance errors."""
    try:
        import anthropic
        anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY")).messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=2,
            messages=[{"role": "user", "content": "hi"}],
        )
        return True
    except Exception as e:
        msg = str(e).lower()
        if "credit" in msg or "billing" in msg or "balance" in msg:
            return False
        # Other errors (rate limit, network) — treat as temporarily not-ok
        log.warning(f"Credit probe error: {str(e)[:80]}")
        return False


def count_state():
    """Return (written, total_hot, submitted)."""
    from config.profile import PROFILE
    threshold = PROFILE["score_threshold"]
    done_ids = {json.loads(p.read_text()).get("id")
                for p in Path("outputs/applications").glob("*/application.json")}
    hot = []
    tq = Path("outputs/tracker_queue.jsonl")
    if tq.exists():
        for line in tq.read_text().strip().splitlines():
            if not line: continue
            j = json.loads(line)
            if j.get("score", 0) >= threshold:
                hot.append(j)
    hot_ids = {j["id"] for j in hot}
    remaining = [j for j in hot if j["id"] not in done_ids]
    submitted = 0
    sf = Path("outputs/agent4_submitted.jsonl")
    if sf.exists():
        for line in sf.read_text().strip().splitlines():
            if line and json.loads(line).get("status") == "submitted":
                submitted += 1
    return len(done_ids & hot_ids), len(hot), len(remaining), submitted


async def wait_for_credits():
    while not credits_ok():
        log.warning(f"⏸  Credits exhausted — waiting {CREDIT_WAIT_SECONDS//60} min then retrying…")
        await asyncio.sleep(CREDIT_WAIT_SECONDS)
    log.info("✅ Credits available — resuming.")


async def run_agent2_remaining():
    from config.profile import PROFILE
    threshold = PROFILE["score_threshold"]
    done_ids = {json.loads(p.read_text()).get("id")
                for p in Path("outputs/applications").glob("*/application.json")}
    remaining = []
    for line in Path("outputs/tracker_queue.jsonl").read_text().strip().splitlines():
        if not line: continue
        j = json.loads(line)
        if j.get("score", 0) >= threshold and j.get("id") not in done_ids:
            remaining.append(j)
    if not remaining:
        log.info("Agent 2: nothing remaining ✅")
        return
    log.info(f"Agent 2: writing {len(remaining)} remaining applications…")
    Path("outputs/agent2_queue.jsonl").write_text(
        "\n".join(json.dumps(j) for j in remaining) + "\n")
    from agents.agent2_writer import run_from_queue
    await run_from_queue()


async def run_agent3_all():
    """Submit every written application not yet successfully submitted."""
    submitted_ids = set()
    sf = Path("outputs/agent4_submitted.jsonl")
    if sf.exists():
        for line in sf.read_text().strip().splitlines():
            if not line: continue
            j = json.loads(line)
            if j.get("status") == "submitted":
                submitted_ids.add(j.get("job_id"))
    to_submit = []
    for p in sorted(Path("outputs/applications").glob("*/application.json")):
        j = json.loads(p.read_text())
        if j.get("id") not in submitted_ids:
            to_submit.append(j)
    if not to_submit:
        log.info("Agent 3: nothing to submit ✅")
        return
    log.info(f"Agent 3: attempting {len(to_submit)} submissions…")
    Path("outputs/agent3_queue.jsonl").write_text(
        "\n".join(json.dumps(j) for j in to_submit) + "\n")
    from agents.agent3_submitter import run_from_queue as submit_queue
    await submit_queue()


async def run_agent4_5():
    from agents.agent4_tracker import run_tracker
    await run_tracker()
    log.info("Agent 4: tracker updated ✅")
    from agents.agent5_recruiter import run_from_queue as recruiter_queue
    await recruiter_queue()
    log.info("Agent 5: recruiter done ✅")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true",
                        help="Keep retrying through credit limits until 100% done")
    args = parser.parse_args()

    log.info("══════════════════════════════════════")
    log.info(f"  AUTO-COMPLETE — {datetime.now():%d %b %H:%M}")
    log.info("══════════════════════════════════════")

    while True:
        written, total, remaining, submitted = count_state()
        log.info(f"State: {written}/{total} written · {remaining} remaining · {submitted} submitted")

        if remaining == 0 and written >= total:
            log.info("🎉 All applications written.")
            # Still run 3/4/5 to submit + track, then done
            await wait_for_credits()
            await run_agent3_all()
            await run_agent4_5()
            log.info("🎉 AUTO-COMPLETE finished — everything up to date.")
            break

        await wait_for_credits()
        try:
            await run_agent2_remaining()
            await run_agent3_all()
            await run_agent4_5()
        except Exception as e:
            msg = str(e).lower()
            if "credit" in msg or "billing" in msg or "balance" in msg:
                log.warning("Hit credit limit mid-run — pausing.")
                if not args.loop:
                    log.info("Run with --loop to auto-wait for reset. Stopping for now.")
                    break
                continue
            log.error(f"Unexpected error: {e}", exc_info=True)
            break

        if not args.loop:
            break

    log.info("══ AUTO-COMPLETE done ══")


if __name__ == "__main__":
    asyncio.run(main())
