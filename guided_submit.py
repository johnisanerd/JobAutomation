#!/usr/bin/env python3
"""
Guided submit — opens a VISIBLE browser, auto-fills every walled application
(needs_review / needs_manual), and pauses for you to solve the CAPTCHA + click
the final Submit. The automation does all the tedious filling; you only do the
~5-second human part that anti-bot systems legitimately require.

Run:  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 guided_submit.py

Controls at each pause:  [Enter]=I submitted it   s=skip this one   q=quit all
"""
import os, sys, json, asyncio
from pathlib import Path
from collections import defaultdict

os.environ["AGENT3_INTERACTIVE"] = "1"          # tells Agent 3 to run headed + pause
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.profile import PROFILE
from resume_pipeline import _clean_company, _norm_company, _looks_like_real_company

WALLED = {"needs_review", "needs_manual"}        # the stuck-but-built applications
HISTORY = [
    "outputs/agent4_submitted_history.jsonl",
    "outputs/agent4_submitted.bak",
    "outputs/agent4_submitted.jsonl",
]


def latest_status() -> dict:
    st = {}
    for fn in HISTORY:
        p = Path(fn)
        if not p.exists():
            continue
        for line in p.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
                jid = j.get("job_id") or j.get("id")
                if jid:
                    st[jid] = j.get("status", "")
            except Exception:
                pass
    return st


def build_queue() -> list[dict]:
    built = {}
    for aj in sorted(Path("outputs/applications").glob("*/application.json")):
        try:
            j = json.loads(aj.read_text())
        except Exception:
            continue
        if j.get("id"):
            built[j["id"]] = j

    status = latest_status()
    cap = PROFILE.get("max_roles_per_company", 3)
    skip = [s.lower() for s in PROFILE.get("agent3_skip_companies", [])]

    def is_skipped(c: str) -> bool:
        cc = _norm_company(c)
        return any(s in cc for s in skip)

    cands = []
    for jid, j in built.items():
        if status.get(jid) not in WALLED:
            continue
        co = _clean_company(j.get("company", ""))
        if is_skipped(co) or not _looks_like_real_company(co):
            continue
        j["company"] = co
        j["_force_retry"] = True
        cands.append(j)

    # at most `cap` distinct roles per company, highest-scored first
    by_co = defaultdict(list)
    for j in cands:
        by_co[_norm_company(j["company"])].append(j)
    queue = []
    for co, lst in by_co.items():
        lst.sort(key=lambda x: x.get("score", 0), reverse=True)
        queue.extend(lst[:cap])
    return cands, queue


async def main():
    # Optional: limit how many to try this run, e.g.  guided_submit.py 1
    limit = None
    for a in sys.argv[1:]:
        if a.isdigit():
            limit = int(a)

    cands, queue = build_queue()

    # Optional: restrict to specific companies listed in outputs/guided_only.txt
    # (one per line). Lets us run a targeted subset and leave the rest.
    only_file = Path("outputs/guided_only.txt")
    if only_file.exists():
        wanted = [w.strip().lower() for w in only_file.read_text().splitlines() if w.strip()]
        if wanted:
            def match(co):
                c = (co or "").lower()
                return any(w in c or c in w for w in wanted)
            queue = [j for j in queue if match(j.get("company", ""))]
            print(f"  Filtered to {len(queue)} jobs for companies: {', '.join(wanted)}")

    if limit is not None:
        queue = queue[:limit]
    qp = Path("outputs/agent3_queue.jsonl")
    qp.write_text(("\n".join(json.dumps(j) for j in queue) + "\n") if queue else "")

    print("=" * 70)
    print(f"  GUIDED SUBMIT — {len(queue)} applications ready "
          f"(from {len(cands)} walled candidates, capped {PROFILE.get('max_roles_per_company',3)}/company)")
    print("  A browser window will open and auto-fill each one. When it pauses,")
    print("  solve the CAPTCHA + click Submit in that window, then press Enter here.")
    print("=" * 70)
    if not queue:
        print("  Nothing walled to submit. Done.")
        return

    from agents.agent3_submitter import run_from_queue
    try:
        await run_from_queue()
    except KeyboardInterrupt:
        print("\n  Stopped. Re-run any time to continue with what's left.")


if __name__ == "__main__":
    asyncio.run(main())
