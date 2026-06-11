"""India-specific job board discovery: Naukri and Foundit.

Naukri: Uses the undocumented JSON search API.
Foundit: Uses the public JSON search API (formerly Monster India).

Both return normalized jobs stored with strategy="india_board".
"""

import json
import logging
import re
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, timezone

from applypilot import config
from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

_TIMEOUT = 15
_NAUKRI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Appid": "109",
    "Systemid": "109",
    "Referer": "https://www.naukri.com/",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_json(url: str, headers: dict) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.debug("Fetch failed %s: %s", url, e)
        return None


def _store_jobs(conn: sqlite3.Connection, jobs: list[dict]) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    new = existing = 0
    for j in jobs:
        if not j.get("url") or not j.get("title"):
            continue
        try:
            conn.execute(
                """INSERT INTO jobs (
                    url, title, location, site, strategy,
                    full_description, application_url, detail_scraped_at,
                    discovered_at
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    j["url"],
                    j["title"],
                    j.get("location", ""),
                    j.get("site", "India"),
                    "india_board",
                    j.get("description", ""),
                    j["url"],
                    now,
                    now,
                ),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1
    conn.commit()
    return new, existing


def _build_query_string(search_cfg: dict) -> list[str]:
    queries = []
    for q in search_cfg.get("queries", []):
        raw = q.get("query", "") if isinstance(q, dict) else str(q)
        if raw:
            queries.append(raw)
    return queries or ["Software Engineer", "Backend Engineer", "Full Stack Developer"]


# ---------------------------------------------------------------------------
# Naukri
# ---------------------------------------------------------------------------

def _fetch_naukri(query: str, location: str = "bengaluru") -> list[dict]:
    """Fetch jobs from Naukri's undocumented JSON search API."""
    encoded_query = urllib.parse.quote(query)
    encoded_loc = urllib.parse.quote(location)
    url = (
        f"https://www.naukri.com/jobapi/v3/search"
        f"?noOf=50&urlType=search_by_key_loc"
        f"&searchType=adv&keyword={encoded_query}"
        f"&location={encoded_loc}&experience=0&salary=0"
        f"&industryTypeGid=&functionAreaGid=&educationUG=&educationPG="
        f"&jobAge=7&src=jobsearchDesk&latLong="
    )
    data = _fetch_json(url, _NAUKRI_HEADERS)
    if not data or not isinstance(data, dict):
        return []

    jobs = []
    for j in data.get("jobDetails", []):
        title = j.get("title", "")
        job_id = j.get("jobId", "")
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        job_url = f"https://www.naukri.com/{slug}-{job_id}"
        loc = j.get("placeholders", [{}])[1].get("label", "") if len(j.get("placeholders", [])) > 1 else location
        desc = j.get("jobDescription", "") or ""
        jobs.append({
            "title": title,
            "url": job_url,
            "location": loc,
            "site": "Naukri",
            "description": re.sub(r"<[^>]+>", " ", desc).strip()[:3000],
        })
    return jobs


# ---------------------------------------------------------------------------
# Foundit
# ---------------------------------------------------------------------------

def _fetch_foundit(query: str, location: str = "Bengaluru") -> list[dict]:
    """Fetch jobs from Foundit (formerly Monster India) JSON API."""
    payload = json.dumps({
        "query": query,
        "location": [location],
        "experience": {"min": 0, "max": 5},
        "postedDate": 7,
        "start": 0,
        "rows": 50,
        "sort": "date",
    }).encode("utf-8")

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; ApplyPilot/1.0)",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.foundit.in",
        "Referer": "https://www.foundit.in/",
    }

    try:
        req = urllib.request.Request(
            "https://www.foundit.in/middleware/jobsearch/api/v2/search",
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.debug("Foundit fetch failed: %s", e)
        return []

    jobs = []
    for j in data.get("jobSearchResult", []):
        title = j.get("jobTitle", "")
        job_id = j.get("jobId", "")
        job_url = f"https://www.foundit.in/job/{job_id}"
        loc = j.get("locations", [location])[0] if j.get("locations") else location
        jobs.append({
            "title": title,
            "url": job_url,
            "location": loc,
            "site": "Foundit",
            "description": j.get("jobDescription", "")[:3000],
        })
    return jobs


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_india_discovery(search_cfg: dict | None = None) -> dict:
    """Discover jobs from Naukri and Foundit.

    Args:
        search_cfg: Loaded searches.yaml dict. Loaded from disk if None.

    Returns:
        {"found": int, "new": int, "existing": int}
    """
    if search_cfg is None:
        search_cfg = config.load_search_config()

    if not search_cfg.get("india_boards", {}).get("enabled", True):
        log.info("india_boards disabled in searches.yaml — skipping")
        return {"found": 0, "new": 0, "existing": 0}

    queries = _build_query_string(search_cfg)
    locations = [loc.get("location", "Bengaluru") for loc in search_cfg.get("locations", [{"location": "Bengaluru"}])]
    bengaluru_locs = [l for l in locations if "bengaluru" in l.lower() or "bangalore" in l.lower() or "india" in l.lower()]
    search_locations = bengaluru_locs or ["Bengaluru"]

    conn = get_connection()
    init_db()
    totals = {"found": 0, "new": 0, "existing": 0}

    for query in queries[:5]:  # cap at 5 queries to avoid rate limits
        for loc in search_locations[:2]:
            # Naukri
            try:
                jobs = _fetch_naukri(query, loc)
                n, e = _store_jobs(conn, jobs)
                log.info("[India/Naukri] '%s' @ %s: %d found, %d new", query, loc, len(jobs), n)
                totals["found"] += len(jobs)
                totals["new"] += n
                totals["existing"] += e
            except Exception as exc:
                log.error("[India/Naukri] %s: %s", query, exc)

            # Foundit
            try:
                jobs = _fetch_foundit(query, loc)
                n, e = _store_jobs(conn, jobs)
                log.info("[India/Foundit] '%s' @ %s: %d found, %d new", query, loc, len(jobs), n)
                totals["found"] += len(jobs)
                totals["new"] += n
                totals["existing"] += e
            except Exception as exc:
                log.error("[India/Foundit] %s: %s", query, exc)

    return totals
