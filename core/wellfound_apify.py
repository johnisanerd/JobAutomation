"""Adapter for johnvc/wellfound-jobs-api dataset rows.

The previous Wellfound actor id 404s. This module maps the live actor's input
and nested company fields onto the scout's existing Job field names.
"""

from __future__ import annotations

from typing import Any

WELLFOUND_ACTOR_ID = "johnvc~wellfound-jobs-api"
WELLFOUND_ACTOR_URL = "https://apify.com/johnvc/wellfound-jobs-api"


def wellfound_run_input(
    *,
    role: str,
    location: str,
    max_items: int,
) -> dict[str, Any]:
    """Build actor input from the scout's existing role, location, and cap.

    Args:
        role: Wellfound role slug such as ``software-engineer``.
        location: Location slug or the word ``Remote``.
        max_items: Maximum jobs to return.

    Returns:
        Input dict for ``johnvc/wellfound-jobs-api``.
    """
    loc = (location or "").strip()
    remote_only = loc.lower() in {"remote", "anywhere"}
    payload: dict[str, Any] = {
        "roles": [role] if role else ["software-engineer"],
        "maxItems": max_items,
        "includeDescription": True,
    }
    if remote_only:
        payload["remoteOnly"] = True
        payload["locations"] = ["remote"]
    elif loc:
        payload["locations"] = [loc.lower().replace(" ", "-")]
    return payload


def _as_text(value: Any) -> str:
    """Return a stripped string, ignoring nested objects."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _company_name(raw: dict[str, Any]) -> str:
    """Read company from a nested object or a flat string field."""
    company = raw.get("company")
    if isinstance(company, dict):
        return _as_text(company.get("name"))
    name = _as_text(company)
    if name:
        return name
    return _as_text(raw.get("companyName"))


def _location(raw: dict[str, Any]) -> str:
    """Prefer locationNames, then remote, then a flat location field."""
    names = raw.get("locationNames")
    if isinstance(names, list):
        joined = ", ".join(_as_text(item) for item in names if _as_text(item))
        if joined:
            return joined
    if raw.get("remote") is True:
        return "Remote"
    ctx = raw.get("searchContext")
    if isinstance(ctx, dict):
        ctx_loc = _as_text(ctx.get("location"))
        if ctx_loc:
            return ctx_loc
    loc = _as_text(raw.get("location"))
    return loc or "Remote"


def _salary(raw: dict[str, Any]) -> str:
    """Prefer the actor's compensation text, then min/max salary."""
    text = _as_text(raw.get("compensationRaw"))
    if text:
        return text
    lo = raw.get("salaryMin")
    hi = raw.get("salaryMax")
    if lo or hi:
        return f"{lo or '?'}-{hi or '?'}"
    return _as_text(raw.get("salary")) or _as_text(raw.get("compensation"))


def normalize_wellfound_row(raw: dict[str, Any]) -> dict[str, str]:
    """Map one actor row onto the scout's title, company, location, url, description, posted_at.

    Args:
        raw: One dataset item from ``johnvc/wellfound-jobs-api``.

    Returns:
        String fields the scout already stores on ``Job``.
    """
    return {
        "title": _as_text(raw.get("title"))
        or _as_text(raw.get("primaryRoleTitle"))
        or _as_text(raw.get("jobTitle")),
        "company": _company_name(raw),
        "location": _location(raw),
        "url": _as_text(raw.get("url")) or _as_text(raw.get("jobUrl")),
        "description": _as_text(raw.get("description"))
        or _as_text(raw.get("jobDescription")),
        "posted_at": _as_text(raw.get("postedAt"))
        or _as_text(raw.get("createdAt"))
        or _as_text(raw.get("date")),
        "salary": _salary(raw),
    }
