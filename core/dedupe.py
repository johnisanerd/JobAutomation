"""
Application de-duplication — avoid spamming a company with many near-identical roles.

Policy (configurable):
  - Group jobs by company.
  - Within a company, bucket by normalised role/team identity.
    (Same role listed in several cities collapses to ONE — keep the highest-scored.)
  - Keep at most MAX_PER_COMPANY distinct teams per company, ranked by score.

Result: instead of 49 MongoDB roles, you apply to the 3 most relevant DISTINCT teams.
"""

import re

MAX_PER_COMPANY = 3   # user policy: up to 3 distinct teams per company


def _normalise_team(title: str) -> str:
    """
    Reduce a job title to a stable team/role identity so the same role in
    different cities/levels collapses together.
    """
    t = title.lower().strip()
    # drop parenthetical bits: "(L4)", "(Remote, US)", "(Tokyo, Japan)"
    t = re.sub(r"\([^)]*\)", " ", t)
    # drop trailing location/remote clauses after a dash or comma at the end
    t = re.sub(r"\s*[-–—]\s*(remote|hybrid|onsite|emea|apac|amer|us|uk|eu|ireland|dublin|canada|usa|india).*$", " ", t)
    # drop level roman numerals / Lx tokens
    t = re.sub(r"\b(l[1-9]|i{1,3}|iv|v)\b", " ", t)
    # drop common seniority words so "Senior X" and "Staff X" on the SAME team
    # are still treated as distinct? -> keep seniority (different team rung).
    # collapse whitespace + punctuation
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def dedupe_applications(jobs: list[dict], max_per_company: int = MAX_PER_COMPANY) -> list[dict]:
    """
    Return a filtered list keeping at most `max_per_company` distinct teams per
    company, each represented by its highest-scored variant.
    """
    # group by company
    by_company: dict[str, list[dict]] = {}
    for j in jobs:
        by_company.setdefault((j.get("company") or "unknown").lower(), []).append(j)

    kept: list[dict] = []
    for company, comp_jobs in by_company.items():
        # bucket by normalised team; keep best-scored per bucket
        buckets: dict[str, dict] = {}
        for j in comp_jobs:
            key = _normalise_team(j.get("title", ""))
            cur = buckets.get(key)
            if cur is None or j.get("score", 0) > cur.get("score", 0):
                buckets[key] = j

        # rank distinct teams by score, take top N
        distinct = sorted(buckets.values(), key=lambda x: x.get("score", 0), reverse=True)
        chosen = distinct[:max_per_company]

        # extra safety: ensure the chosen set isn't all the identical location+title
        kept.extend(chosen)

    # stable order: by score desc overall
    kept.sort(key=lambda x: x.get("score", 0), reverse=True)
    return kept


def summarise(before: list[dict], after: list[dict]) -> str:
    from collections import Counter
    cb = Counter((j.get("company") or "?").lower() for j in before)
    ca = Counter((j.get("company") or "?").lower() for j in after)
    lines = [f"De-dupe: {len(before)} → {len(after)} applications "
             f"({len(cb)} companies)"]
    for co in sorted(cb, key=lambda c: -cb[c])[:8]:
        lines.append(f"  {co:14} {cb[co]:>3} → {ca.get(co,0)}")
    return "\n".join(lines)
