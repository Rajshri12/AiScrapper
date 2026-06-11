"""Remote job board discovery via direct JSON/RSS APIs.

Scrapes public endpoints on remote-focused boards directly:
  - RemoteOK       — JSON API (remoteok.com/api)
  - Remotive       — JSON API (remotive.com/api/remote-jobs)
  - Himalayas      — JSON API (himalayas.app/jobs/api)
  - WeWorkRemotely — RSS feed (weworkremotely.com/remote-jobs.rss)

No browser, no LLM, no API keys needed. Filters by role keywords from
searches.yaml.
"""

import json
import logging
import re
import sqlite3
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from applypilot import config
from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

_TIMEOUT = 15
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ApplyPilot/1.0)",
    "Accept": "application/json, text/plain, */*",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_json(url: str) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.debug("Fetch failed %s: %s", url, e)
        return None


def _fetch_text(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug("Fetch failed %s: %s", url, e)
        return None


def _build_keywords(search_cfg: dict) -> list[str]:
    """Extract role keywords from queries in searches.yaml."""
    keywords = set()
    for q in search_cfg.get("queries", []):
        raw = q.get("query", "") if isinstance(q, dict) else str(q)
        for word in re.findall(r"[A-Za-z][A-Za-z\s]{2,}", raw):
            keywords.add(word.strip().lower())
    # Always include broad software terms
    keywords.update(["software", "backend", "full stack", "engineer", "developer", "ai", "python", "java"])
    return list(keywords)


def _title_matches(title: str, keywords: list[str]) -> bool:
    t = title.lower()
    return any(k in t for k in keywords)


def _store_jobs(conn: sqlite3.Connection, jobs: list[dict], site: str) -> tuple[int, int]:
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
                    j.get("location", "Remote"),
                    site,
                    "websearch_api",
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


# ---------------------------------------------------------------------------
# RemoteOK
# ---------------------------------------------------------------------------

def _fetch_remoteok(keywords: list[str]) -> list[dict]:
    """Fetch jobs from RemoteOK JSON API."""
    data = _fetch_json("https://remoteok.com/api")
    if not isinstance(data, list):
        return []
    jobs = []
    for j in data:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        title = j.get("position", "")
        if not _title_matches(title, keywords):
            continue
        jobs.append({
            "title": title,
            "url": j.get("url", f"https://remoteok.com/l/{j.get('id', '')}"),
            "location": "Remote",
            "description": re.sub(r"<[^>]+>", " ", j.get("description", "")).strip(),
        })
    return jobs


# ---------------------------------------------------------------------------
# Remotive
# ---------------------------------------------------------------------------

def _fetch_remotive(keywords: list[str]) -> list[dict]:
    """Fetch jobs from Remotive JSON API."""
    categories = ["software-dev", "devops-sysadmin", "data", "all-other"]
    jobs = []
    for cat in categories:
        data = _fetch_json(f"https://remotive.com/api/remote-jobs?category={cat}&limit=100")
        if not isinstance(data, dict):
            continue
        for j in data.get("jobs", []):
            title = j.get("title", "")
            if not _title_matches(title, keywords):
                continue
            jobs.append({
                "title": title,
                "url": j.get("url", ""),
                "location": j.get("candidate_required_location", "Remote"),
                "description": re.sub(r"<[^>]+>", " ", j.get("description", "")).strip()[:3000],
            })
    return jobs


# ---------------------------------------------------------------------------
# Himalayas
# ---------------------------------------------------------------------------

def _fetch_himalayas(keywords: list[str]) -> list[dict]:
    """Fetch jobs from Himalayas JSON API."""
    # Himalayas has a public jobs listing endpoint
    data = _fetch_json("https://himalayas.app/jobs/api?quantity=100")
    if not isinstance(data, dict):
        return []
    jobs = []
    for j in data.get("jobs", []):
        title = j.get("title", "")
        if not _title_matches(title, keywords):
            continue
        jobs.append({
            "title": title,
            "url": j.get("applicationLink", j.get("url", "")),
            "location": j.get("location", "Remote"),
            "description": j.get("description", "")[:3000],
        })
    return jobs


# ---------------------------------------------------------------------------
# WeWorkRemotely
# ---------------------------------------------------------------------------

def _fetch_weworkremotely(keywords: list[str]) -> list[dict]:
    """Fetch jobs from WeWorkRemotely RSS feed."""
    feeds = [
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    ]
    jobs = []
    for feed_url in feeds:
        text = _fetch_text(feed_url)
        if not text:
            continue
        try:
            root = ET.fromstring(text)
            for item in root.iter("item"):
                title_el = item.find("title")
                link_el = item.find("link")
                desc_el = item.find("description")
                if title_el is None or link_el is None:
                    continue
                title = title_el.text or ""
                # WWR title format: "Company: Job Title"
                if ": " in title:
                    title = title.split(": ", 1)[1]
                if not _title_matches(title, keywords):
                    continue
                url = link_el.text or ""
                if not url.startswith("http"):
                    # link text is sometimes empty; text is in next sibling
                    url = ""
                    for child in item:
                        if child.tag == "link" and child.tail:
                            url = child.tail.strip()
                            break
                desc = re.sub(r"<[^>]+>", " ", desc_el.text or "").strip()[:2000] if desc_el is not None else ""
                if url:
                    jobs.append({
                        "title": title,
                        "url": url,
                        "location": "Remote",
                        "description": desc,
                    })
        except ET.ParseError as e:
            log.debug("WWR RSS parse error %s: %s", feed_url, e)
    return jobs


# ---------------------------------------------------------------------------
# Jobicy
# ---------------------------------------------------------------------------

def _fetch_jobicy(keywords: list[str]) -> list[dict]:
    """Fetch jobs from Jobicy public JSON API."""
    jobs = []
    for industry in ["engineering", "programming"]:
        data = _fetch_json(f"https://jobicy.com/api/v2/remote-jobs?count=50&industry={industry}")
        if not isinstance(data, dict):
            continue
        for j in data.get("jobs", []):
            title = j.get("jobTitle", "")
            if not _title_matches(title, keywords):
                continue
            jobs.append({
                "title": title,
                "url": j.get("url", ""),
                "location": j.get("jobGeo", "Remote"),
                "description": re.sub(r"<[^>]+>", " ", j.get("jobDescription", "")).strip()[:3000],
            })
    return jobs


# ---------------------------------------------------------------------------
# Dynamite Jobs (RSS)
# ---------------------------------------------------------------------------

def _fetch_dynamitejobs(keywords: list[str]) -> list[dict]:
    """Fetch jobs from Dynamite Jobs RSS feed."""
    text = _fetch_text("https://dynamitejobs.com/feed")
    if not text:
        return []
    jobs = []
    try:
        root = ET.fromstring(text)
        for item in root.iter("item"):
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")
            if title_el is None:
                continue
            title = title_el.text or ""
            if not _title_matches(title, keywords):
                continue
            url = ""
            if link_el is not None:
                url = link_el.text or (link_el.tail or "").strip()
            desc = re.sub(r"<[^>]+>", " ", desc_el.text or "").strip()[:2000] if desc_el is not None else ""
            if url:
                jobs.append({"title": title, "url": url, "location": "Remote", "description": desc})
    except ET.ParseError as e:
        log.debug("DynamiteJobs RSS parse error: %s", e)
    return jobs


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_websearch_discovery(search_cfg: dict | None = None) -> dict:
    """Discover jobs from remote-focused boards via their public APIs.

    Args:
        search_cfg: Loaded searches.yaml dict. Loaded from disk if None.

    Returns:
        {"found": int, "new": int, "existing": int}
    """
    if search_cfg is None:
        search_cfg = config.load_search_config()

    if not search_cfg.get("websearch", {}).get("enabled", True):
        log.info("websearch disabled in searches.yaml — skipping")
        return {"found": 0, "new": 0, "existing": 0}

    keywords = _build_keywords(search_cfg)
    conn = get_connection()
    init_db()

    totals = {"found": 0, "new": 0, "existing": 0}

    boards = [
        ("RemoteOK", _fetch_remoteok),
        ("Remotive", _fetch_remotive),
        ("Himalayas", _fetch_himalayas),
        ("WeWorkRemotely", _fetch_weworkremotely),
        ("Jobicy", _fetch_jobicy),
        ("DynamiteJobs", _fetch_dynamitejobs),
    ]

    for site_name, fetch_fn in boards:
        try:
            jobs = fetch_fn(keywords)
            n, e = _store_jobs(conn, jobs, site_name)
            log.info("[WebSearch] %s: %d found, %d new, %d existing", site_name, len(jobs), n, e)
            totals["found"] += len(jobs)
            totals["new"] += n
            totals["existing"] += e
        except Exception as exc:
            log.error("[WebSearch] %s failed: %s", site_name, exc)

    return totals
