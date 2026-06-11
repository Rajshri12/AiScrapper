"""Application pattern analyzer.

Reads the jobs DB and surfaces rejection/success patterns:
  - score distribution vs outcome
  - best-converting sources (site/strategy)
  - top job titles that got responses
  - application volume over time
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _week_label(iso_date: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return dt.strftime("%Y-W%W")
    except Exception:
        return "unknown"


def run_analysis(conn) -> dict:
    """Analyse the jobs database and return structured stats."""
    rows = conn.execute(
        """SELECT url, title, site, strategy, fit_score, apply_status,
                  applied_at, discovered_at, liveness, legitimacy_score
           FROM jobs"""
    ).fetchall()

    if not rows:
        return {"total": 0}

    total = len(rows)
    applied = [r for r in rows if r[6]]  # applied_at set
    scored = [r for r in rows if r[4] is not None]

    # Score distribution
    score_buckets: dict[str, int] = defaultdict(int)
    for r in scored:
        score = int(r[4])
        if score >= 80:
            score_buckets["80-100"] += 1
        elif score >= 60:
            score_buckets["60-79"] += 1
        elif score >= 40:
            score_buckets["40-59"] += 1
        else:
            score_buckets["<40"] += 1

    # Source breakdown
    source_counts: dict[str, dict] = defaultdict(lambda: {"discovered": 0, "applied": 0})
    for r in rows:
        site = r[2] or r[3] or "unknown"
        source_counts[site]["discovered"] += 1
    for r in applied:
        site = r[2] or r[3] or "unknown"
        source_counts[site]["applied"] += 1

    # Weekly volume
    weekly: dict[str, int] = defaultdict(int)
    for r in rows:
        if r[8]:  # discovered_at
            weekly[_week_label(r[8])] += 1

    # Status breakdown
    status_counts: dict[str, int] = defaultdict(int)
    for r in applied:
        status_counts[r[5] or "applied"] += 1

    # Liveness distribution
    liveness_counts: dict[str, int] = defaultdict(int)
    for r in rows:
        liveness_counts[r[8] or "unknown"] += 1

    return {
        "total": total,
        "scored": len(scored),
        "applied": len(applied),
        "score_distribution": dict(score_buckets),
        "by_source": {k: dict(v) for k, v in source_counts.items()},
        "weekly_discovery": dict(sorted(weekly.items())),
        "apply_status": dict(status_counts),
    }
