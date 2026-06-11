"""URL liveness classifier for job postings.

Checks whether a job listing URL is still live, expired, behind a bot
challenge, or a generic listing page — before wasting LLM tokens on it.

Ported from career-ops liveness-core.mjs (MIT license).
"""

import re
import urllib.request
import urllib.error
from typing import Literal

_TIMEOUT = 10
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ApplyPilot/1.0)",
    "Accept": "text/html,application/xhtml+xml,*/*",
}

# Body text patterns indicating the listing is expired/closed
_EXPIRED_PATTERNS = [
    r"this (job|position|role|posting) (is no longer|has been) (available|filled|closed|removed)",
    r"(job|position|role) (no longer|not) available",
    r"this posting has expired",
    r"this (job|role) has been filled",
    r"no longer accepting applications",
    r"position (has been )?filled",
    r"job listing (has been )?removed",
    r"this opportunity is (now )?closed",
    r"application (period|deadline) (has )?ended",
    r"this vacancy (is|has been) (closed|filled)",
]
_EXPIRED_RE = re.compile("|".join(_EXPIRED_PATTERNS), re.IGNORECASE)

# Bot challenge / access blocked patterns
_BOT_PATTERNS = [
    "just a moment",         # Cloudflare
    "cf-ray",                # Cloudflare header
    "hcaptcha",
    "recaptcha",
    "access denied",
    "403 forbidden",
    "enable javascript",
    "checking your browser",
    "ddos-guard",
    "are you human",
]

# Apply button / form patterns (confirms listing is live)
_APPLY_PATTERNS = [
    r"apply\s*(now|for\s+this\s+job|to\s+this\s+role)?",
    r"submit\s*(your\s+)?application",
    r"apply\s+online",
    r"quick\s+apply",
    r"easy\s+apply",
]
_APPLY_RE = re.compile("|".join(_APPLY_PATTERNS), re.IGNORECASE)

# Generic listing page (index, not a specific job)
_LISTING_PAGE_PATTERNS = [
    r"all\s+open\s+(positions|roles|jobs)",
    r"current\s+(openings|opportunities|positions)",
    r"(jobs|careers)\s+at\s+\w+",
    r"\d+\s+(open\s+)?(jobs|positions|roles)",
]
_LISTING_RE = re.compile("|".join(_LISTING_PAGE_PATTERNS), re.IGNORECASE)

LivenessResult = Literal["live", "expired", "bot_challenge", "listing_page", "unknown", "error"]


def check_liveness(url: str) -> LivenessResult:
    """Check whether a job URL is still live.

    Args:
        url: The job listing URL to check.

    Returns:
        "live"         — apply button found, looks active
        "expired"      — expired/closed text detected
        "bot_challenge"— behind Cloudflare or CAPTCHA
        "listing_page" — generic jobs index, not a specific listing
        "unknown"      — page loaded but couldn't determine status
        "error"        — HTTP error or network failure
    """
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            status = resp.status
            body = resp.read(30_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return "expired"
        return "error"
    except Exception:
        return "error"

    if status in (404, 410):
        return "expired"

    body_lower = body.lower()

    # Bot challenge check (headers / body)
    for pat in _BOT_PATTERNS:
        if pat in body_lower:
            return "bot_challenge"

    # Too short to be a real job posting
    if len(body) < 300:
        return "unknown"

    # Expired signals
    if _EXPIRED_RE.search(body):
        return "expired"

    # Apply button present → live
    if _APPLY_RE.search(body):
        return "live"

    # Generic listing page
    if _LISTING_RE.search(body):
        return "listing_page"

    return "unknown"
