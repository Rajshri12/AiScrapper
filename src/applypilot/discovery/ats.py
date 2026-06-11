"""ATS API discovery: Greenhouse, Ashby, Lever, Workable, SmartRecruiters.

Hits the public JSON endpoints exposed by each ATS provider directly —
no browser, no LLM, zero token cost. Company list loaded from
config/portals.yaml.

Every job is stored with full_description + application_url + detail_scraped_at
pre-filled so the enrichment stage is skipped automatically.
"""

import logging
import re
import sqlite3
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import yaml

from applypilot import config
from applypilot.config import CONFIG_DIR
from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ApplyPilot/1.0)",
    "Accept": "application/json, text/plain, */*",
}
_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self._parts)).strip()


def _strip_html(html: str) -> str:
    p = _HTMLStripper()
    p.feed(html)
    return p.get_text()


def _fetch_json(url: str) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            import json
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.debug("Fetch failed %s: %s", url, e)
        return None


def _fetch_text(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read().decode("utf-8")
    except Exception as e:
        log.debug("Fetch failed %s: %s", url, e)
        return None


def _load_location_filter(search_cfg: dict | None = None):
    if search_cfg is None:
        search_cfg = config.load_search_config()
    accept = search_cfg.get("location_accept", [])
    reject = search_cfg.get("location_reject_non_remote", [])
    return accept, reject


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    if not location:
        return True
    loc = location.lower()
    if any(r in loc for r in ("remote", "anywhere", "work from home", "wfh", "distributed")):
        return True
    for r in reject:
        if r.lower() in loc:
            return False
    if not accept:
        return True
    for a in accept:
        if a.lower() in loc:
            return True
    return False


# ---------------------------------------------------------------------------
# Portal registry
# ---------------------------------------------------------------------------

def load_portals() -> list[dict]:
    """Load company ATS registry from config/portals.yaml."""
    path = CONFIG_DIR / "portals.yaml"
    if not path.exists():
        log.warning("portals.yaml not found at %s", path)
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [p for p in data.get("portals", []) if p.get("enabled", True)]


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

def _fetch_greenhouse(slug: str, company: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    data = _fetch_json(url)
    if not data or not isinstance(data, dict):
        return []
    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("location", {}).get("name", "") if isinstance(j.get("location"), dict) else str(j.get("location", ""))
        jobs.append({
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "company": company,
            "location": loc,
            "description": "",
            "posted_at": j.get("first_published") or j.get("updated_at", ""),
        })
    return jobs


def _fetch_ashby(slug: str, company: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    data = _fetch_json(url)
    if not data or not isinstance(data, dict):
        return []
    jobs = []
    for j in data.get("jobPostings", []):
        loc = j.get("location", "") or j.get("locationName", "")
        if isinstance(loc, dict):
            loc = loc.get("name", "")
        jobs.append({
            "title": j.get("title", ""),
            "url": j.get("jobUrl", ""),
            "company": company,
            "location": str(loc),
            "description": _strip_html(j.get("descriptionHtml", "") or j.get("description", "")),
            "posted_at": j.get("publishedAt", ""),
        })
    return jobs


def _fetch_lever(slug: str, company: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}"
    data = _fetch_json(url)
    if not isinstance(data, list):
        return []
    jobs = []
    for j in data:
        cats = j.get("categories", {})
        loc = cats.get("location", "") if isinstance(cats, dict) else ""
        desc_html = ""
        if isinstance(j.get("descriptionBody"), str):
            desc_html = j["descriptionBody"]
        elif isinstance(j.get("description"), str):
            desc_html = j["description"]
        jobs.append({
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
            "company": company,
            "location": str(loc),
            "description": _strip_html(desc_html),
            "posted_at": str(j.get("createdAt", "")),
        })
    return jobs


def _fetch_workable(slug: str, company: str) -> list[dict]:
    url = f"https://apply.workable.com/{slug}/jobs.md"
    text = _fetch_text(url)
    if not text:
        return []
    jobs = []
    for line in text.splitlines():
        # Markdown table rows: | [Title](url) | ... | Location |
        m = re.search(r"\[([^\]]+)\]\((https://apply\.workable\.com/[^\)]+)\)", line)
        if not m:
            continue
        title = m.group(1)
        job_url = m.group(2).rstrip("/")
        parts = [p.strip() for p in line.split("|") if p.strip()]
        location = parts[-1] if len(parts) >= 3 else ""
        jobs.append({
            "title": title,
            "url": job_url,
            "company": company,
            "location": location,
            "description": "",
            "posted_at": "",
        })
    return jobs


def _fetch_smartrecruiters(slug: str, company: str) -> list[dict]:
    jobs = []
    offset = 0
    limit = 100
    for _ in range(20):  # max 2000 jobs
        url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit={limit}&offset={offset}&status=PUBLIC"
        data = _fetch_json(url)
        if not data or not isinstance(data, dict):
            break
        items = data.get("content", [])
        if not items:
            break
        for j in items:
            loc_obj = j.get("location", {}) or {}
            city = loc_obj.get("city", "")
            country = loc_obj.get("country", "")
            remote = loc_obj.get("remote", False)
            if remote:
                loc = "Remote"
            elif city and country:
                loc = f"{city}, {country}"
            else:
                loc = city or country or ""
            job_url = f"https://jobs.smartrecruiters.com/{slug}/{j.get('id', '')}"
            jobs.append({
                "title": j.get("name", ""),
                "url": job_url,
                "company": company,
                "location": loc,
                "description": "",
                "posted_at": j.get("releasedDate", ""),
            })
        offset += limit
        if offset >= data.get("totalFound", 0):
            break
    return jobs


_PROVIDERS = {
    "greenhouse": _fetch_greenhouse,
    "ashby": _fetch_ashby,
    "lever": _fetch_lever,
    "workable": _fetch_workable,
    "smartrecruiters": _fetch_smartrecruiters,
}


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _store_jobs(conn: sqlite3.Connection, jobs: list[dict], portal: dict) -> tuple[int, int]:
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
                    portal["name"],
                    "ats_api",
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
# Public entry point
# ---------------------------------------------------------------------------

def run_ats_discovery(search_cfg: dict | None = None) -> dict:
    """Discover jobs from all enabled ATS portals.

    Args:
        search_cfg: Loaded searches.yaml dict. Loaded from disk if None.

    Returns:
        {"found": int, "new": int, "existing": int, "errors": int}
    """
    if search_cfg is None:
        search_cfg = config.load_search_config()

    portals = load_portals()
    if not portals:
        log.info("No portals configured — skipping ATS discovery")
        return {"found": 0, "new": 0, "existing": 0, "errors": 0}

    accept_locs, reject_locs = _load_location_filter(search_cfg)
    init_db()

    totals = {"found": 0, "new": 0, "existing": 0, "errors": 0}

    def _scan_portal(portal: dict) -> tuple[int, int, int]:
        provider_id = portal.get("provider", "")
        slug = portal.get("slug", "")
        name = portal.get("name", slug)
        fn = _PROVIDERS.get(provider_id)
        if not fn:
            log.warning("Unknown provider '%s' for %s", provider_id, name)
            return 0, 0, 1

        try:
            jobs = fn(slug, name)
            filtered = [j for j in jobs if _location_ok(j.get("location"), accept_locs, reject_locs)]
            # Each thread needs its own connection (SQLite is not thread-safe across threads)
            thread_conn = get_connection()
            n, e = _store_jobs(thread_conn, filtered, portal)
            log.info("[ATS] %s (%s): %d found, %d new, %d existing",
                     name, provider_id, len(filtered), n, e)
            return len(filtered), n, e
        except Exception as exc:
            log.error("[ATS] %s: %s", name, exc)
            return 0, 0, 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_scan_portal, p): p for p in portals}
        for fut in as_completed(futures):
            found, new, existing = fut.result()
            totals["found"] += found
            totals["new"] += new
            totals["existing"] += existing
            if existing == 0 and found == 0:
                totals["errors"] += 1

    log.info("[ATS] Total: %d found, %d new, %d existing",
             totals["found"], totals["new"], totals["existing"])
    return totals
