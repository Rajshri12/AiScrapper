"""Follow-up cadence tracker for applied jobs.

Tracks applications and generates follow-up drafts at day 7 and day 14.
Urgency buckets: urgent (company replied), overdue (>14d), due_soon (7-14d),
waiting (<7d), cold (>30d).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Literal

log = logging.getLogger(__name__)

Urgency = Literal["urgent", "overdue", "due_soon", "waiting", "cold"]


def _days_since(iso_date: str) -> float:
    """Return days elapsed since an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except Exception:
        return 0.0


def _urgency(days: float, has_reply: bool, follow_up_sent_at: str | None) -> Urgency:
    if has_reply:
        return "urgent"
    if days > 30:
        return "cold"
    if days > 14:
        return "overdue"
    if days >= 7:
        return "due_soon"
    return "waiting"


def get_followup_jobs(conn) -> list[dict]:
    """Fetch applied jobs and classify follow-up urgency."""
    rows = conn.execute(
        """SELECT url, title, site, applied_at, apply_status, follow_up_sent_at
           FROM jobs
           WHERE applied_at IS NOT NULL
           ORDER BY applied_at DESC"""
    ).fetchall()

    jobs = []
    for url, title, site, applied_at, apply_status, follow_up_sent_at in rows:
        if not applied_at:
            continue
        days = _days_since(applied_at)
        has_reply = bool(apply_status and "reply" in (apply_status or "").lower())
        urgency = _urgency(days, has_reply, follow_up_sent_at)
        jobs.append({
            "url": url,
            "title": title or "Unknown",
            "site": site or "",
            "applied_at": applied_at,
            "days_since": round(days, 1),
            "apply_status": apply_status or "",
            "follow_up_sent_at": follow_up_sent_at or "",
            "urgency": urgency,
        })
    return jobs


def generate_followup_draft(job: dict, profile: dict) -> str:
    """Generate a follow-up email draft via LLM."""
    from applypilot.llm import call_llm

    name = profile.get("personal", {}).get("full_name", "Applicant")
    days = job["days_since"]
    title = job["title"]
    company = job.get("site", "the company")

    prompt = f"""Write a brief, professional follow-up email for a job application.

Role applied for: {title}
Company: {company}
Days since application: {days:.0f}
Applicant name: {name}

Keep it under 100 words. Polite, professional, not desperate.
Subject line + body only. No placeholders like [Your Name] — use the actual name.
"""
    return call_llm(prompt)
