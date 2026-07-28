"""
Apply the per-company de-dupe to the EXISTING written applications.

Builds outputs/agent3_queue.jsonl with only the best N distinct roles per company,
so Agent 3 submits the curated set (e.g. 199 → 56) instead of every variant.
Already-written resumes/PDFs for the skipped roles are kept on disk (not deleted) —
they're simply not submitted.

Run:
  python3 apply_dedupe.py            # preview + write agent3 queue
  python3 apply_dedupe.py --max 2    # override roles-per-company
"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config.profile import PROFILE
from core.dedupe import dedupe_applications, summarise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=PROFILE.get("max_roles_per_company", 3))
    ap.add_argument("--write", action="store_true", help="write the agent3 queue (default: preview only)")
    args = ap.parse_args()

    # Load all written applications (these have resume_pdf + cover_letter ready)
    apps = []
    for p in sorted(Path("outputs/applications").glob("*/application.json")):
        try:
            apps.append(json.loads(p.read_text()))
        except Exception:
            pass

    if not apps:
        print("No written applications found in outputs/applications/.")
        return

    kept = dedupe_applications(apps, max_per_company=args.max)
    print(summarise(apps, kept))
    print(f"\nMax roles/company: {args.max}")
    print(f"Curated set: {len(kept)} applications ready to submit.")

    if args.write or True:   # always write the curated queue
        q = Path("outputs/agent3_queue.jsonl")
        q.write_text("\n".join(json.dumps(k) for k in kept) + "\n")
        print(f"\n✅ Wrote {len(kept)} applications → {q}")
        print("   Run Agent 3 to submit just these:")
        print("   /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 resume_pipeline.py --agent3")


if __name__ == "__main__":
    main()
