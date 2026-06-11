"""Email notification stage for ApplyPilot.

Sends two types of emails:
  - Referral targets (score >= referral_threshold): high-fit jobs where getting a
    referral before applying dramatically increases interview chances.
  - Blocked/manual ATS jobs: jobs that can't be auto-applied (blocked sites,
    CAPTCHA-heavy ATS) but have tailored materials ready for manual submission.

Both email types attach the tailored resume + cover letter so the user has
everything they need without any extra prep.

SMTP config (add to ~/.applypilot/.env):
  SMTP_USER   your Gmail address
  SMTP_PASS   Gmail App Password (myaccount.google.com/apppasswords)
  NOTIFY_EMAIL  where to send (defaults to profile.json personal.email)
"""

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import httpx

from applypilot import config
from applypilot.database import get_connection

log = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 465


# ---------------------------------------------------------------------------
# Email builders
# ---------------------------------------------------------------------------

def _html_referral(job: dict) -> str:
    title = job.get("title", "Unknown Role")
    site = job.get("site", "")
    score = job.get("fit_score", "?")
    url = job.get("application_url") or job.get("url", "#")
    location = job.get("location", "")
    salary = job.get("salary", "")
    reasoning = (job.get("score_reasoning") or "")[:400]

    salary_line = f"<p><strong>Salary:</strong> {salary}</p>" if salary else ""
    location_line = f"<p><strong>Location:</strong> {location}</p>" if location else ""
    reasoning_block = (
        f'<blockquote style="border-left:3px solid #4CAF50;padding-left:12px;color:#555">'
        f'{reasoning}…</blockquote>'
        if reasoning else ""
    )

    score_pct = int(score) * 10 if isinstance(score, int) else 0
    bar_color = "#4CAF50" if score_pct >= 90 else "#FFC107"

    return f"""
<html><body style="font-family:sans-serif;max-width:640px;margin:auto;color:#222">
<h2 style="color:#4CAF50">&#127919; Referral Opportunity</h2>
<h3>{title}</h3>
<p style="color:#666">{site}</p>

<div style="background:#f5f5f5;border-radius:8px;padding:16px;margin:16px 0">
  <strong>Fit Score: {score}/10</strong>
  <div style="background:#ddd;border-radius:4px;height:10px;margin-top:6px">
    <div style="background:{bar_color};width:{score_pct}%;height:10px;border-radius:4px"></div>
  </div>
</div>

{reasoning_block}

{location_line}
{salary_line}

<p><strong>Job link:</strong> <a href="{url}">{url}</a></p>

<hr style="border:none;border-top:1px solid #eee;margin:24px 0">

<h3 style="color:#1565C0">Why seek a referral?</h3>
<p>This is a <strong>very strong match</strong>. Referred candidates are up to
<strong>10x more likely</strong> to advance past the initial screening round.</p>

<ol>
  <li>Find a current employee at <strong>{site}</strong> on LinkedIn.</li>
  <li>Send a short message with your attached resume and this job link.</li>
  <li>Ask if they'd be willing to submit a referral on your behalf.</li>
</ol>

<p>Your tailored resume and cover letter are attached — ready to share.</p>

<p style="color:#888;font-size:12px;margin-top:32px">Sent by ApplyPilot</p>
</body></html>
"""


def _html_captcha(job: dict) -> str:
    title = job.get("title", "Unknown Role")
    site = job.get("site", "")
    score = job.get("fit_score", "?")
    url = job.get("application_url") or job.get("url", "#")
    location = job.get("location", "")
    salary = job.get("salary", "")
    reasoning = (job.get("score_reasoning") or "")[:400]

    salary_line = f"<p><strong>Salary:</strong> {salary}</p>" if salary else ""
    location_line = f"<p><strong>Location:</strong> {location}</p>" if location else ""
    reasoning_block = (
        f'<blockquote style="border-left:3px solid #FF9800;padding-left:12px;color:#555">'
        f'{reasoning}…</blockquote>'
        if reasoning else ""
    )

    return f"""
<html><body style="font-family:sans-serif;max-width:640px;margin:auto;color:#222">
<h2 style="color:#FF9800">&#128274; CAPTCHA Blocked — Apply Manually</h2>
<h3>{title}</h3>
<p style="color:#666">{site} — auto-apply was blocked by a CAPTCHA</p>

<p><strong>Fit Score: {score}/10</strong></p>
{reasoning_block}

{location_line}
{salary_line}

<p><strong>Job link:</strong> <a href="{url}">{url}</a></p>

<hr style="border:none;border-top:1px solid #eee;margin:24px 0">

<p>Your tailored resume and cover letter are attached.
<strong>Everything is ready — just open the link above and submit manually.</strong></p>

<p style="color:#888;font-size:12px;margin-top:32px">Sent by ApplyPilot</p>
</body></html>
"""


def _html_blocked(job: dict) -> str:
    title = job.get("title", "Unknown Role")
    site = job.get("site", "")
    score = job.get("fit_score", "?")
    url = job.get("application_url") or job.get("url", "#")
    location = job.get("location", "")
    salary = job.get("salary", "")
    reasoning = (job.get("score_reasoning") or "")[:400]

    salary_line = f"<p><strong>Salary:</strong> {salary}</p>" if salary else ""
    location_line = f"<p><strong>Location:</strong> {location}</p>" if location else ""
    reasoning_block = (
        f'<blockquote style="border-left:3px solid #2196F3;padding-left:12px;color:#555">'
        f'{reasoning}…</blockquote>'
        if reasoning else ""
    )

    return f"""
<html><body style="font-family:sans-serif;max-width:640px;margin:auto;color:#222">
<h2 style="color:#2196F3">&#128203; Manual Apply Needed</h2>
<h3>{title}</h3>
<p style="color:#666">{site} — skipped by auto-apply (site requires manual submission)</p>

<p><strong>Fit Score: {score}/10</strong></p>
{reasoning_block}

{location_line}
{salary_line}

<p><strong>Job link:</strong> <a href="{url}">{url}</a></p>

<hr style="border:none;border-top:1px solid #eee;margin:24px 0">

<p>Your tailored resume and cover letter are attached.
<strong>Everything is ready — just open the link above and submit.</strong></p>

<p style="color:#888;font-size:12px;margin-top:32px">Sent by ApplyPilot</p>
</body></html>
"""


# ---------------------------------------------------------------------------
# Attachment helper
# ---------------------------------------------------------------------------

def _attach_file(msg: MIMEMultipart, path_str: str | None, label: str) -> bool:
    """Attach a file (PDF preferred, .txt fallback). Returns True if attached."""
    if not path_str:
        return False

    p = Path(path_str)
    # Prefer PDF sibling if the stored path is .txt
    pdf = p.with_suffix(".pdf")
    target = pdf if pdf.exists() else p

    if not target.exists():
        log.warning("Attachment not found: %s", target)
        return False

    data = target.read_bytes()
    mime_type = "application/pdf" if target.suffix == ".pdf" else "text/plain"
    part = MIMEApplication(data, Name=target.name)
    part["Content-Disposition"] = f'attachment; filename="{target.name}"'
    msg.attach(part)
    log.debug("Attached %s (%s)", label, target.name)
    return True


# ---------------------------------------------------------------------------
# SMTP send
# ---------------------------------------------------------------------------

def _send_email(msg: MIMEMultipart, smtp_user: str, smtp_pass: str) -> None:
    with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT) as server:
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)


# ---------------------------------------------------------------------------
# Core notification logic
# ---------------------------------------------------------------------------

def _call_webhook(job: dict, reason: str, webhook_url: str) -> None:
    """Upload resume + cover letter to Cloudinary, then POST structured payload to webhook."""
    from applypilot.storage import upload_job_files

    files = upload_job_files(job)

    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
    headers = {}
    if webhook_secret:
        headers["Authorization"] = f"Bearer {webhook_secret}"

    job_url = job.get("url", "")
    fallback_title = job.get("title") or job_url.split("/")[-1] or "Untitled"
    fallback_company = job.get("site") or ""
    if not fallback_company and job_url:
        from urllib.parse import urlparse
        fallback_company = urlparse(job_url).netloc.replace("www.", "")

    payload = {
        "job": {
            "title": fallback_title,
            "company": fallback_company,
            "url": job_url,
            "apply_url": job.get("application_url") or job_url,
            "location": job.get("location", "") or "",
            "salary": job.get("salary", "") or "",
            "score": job.get("fit_score"),
            "score_reasoning": job.get("score_reasoning", "") or "",
            "legitimacy": job.get("legitimacy_score"),
            "discovered_at": job.get("discovered_at", "") or "",
        },
        "files": {
            "resume_url": files.get("resume_url"),
            "cover_letter_url": files.get("cover_letter_url"),
            "expires_at": files.get("expires_at"),
        },
        "meta": {
            "notify_type": reason,
        },
    }
    resp = httpx.post(webhook_url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    log.debug("Webhook delivered: %s -> %s", reason, resp.status_code)


def _mark_notified(url: str, notify_type: str) -> None:
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE jobs SET notified_at = ?, notify_type = ? WHERE url = ?",
        (now, notify_type, url),
    )
    conn.commit()


def _build_message(
    subject: str, html: str, job: dict,
    smtp_user: str, notify_email: str,
) -> MIMEMultipart:
    msg = MIMEMultipart("related")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = notify_email
    msg.attach(MIMEText(html, "html"))
    _attach_file(msg, job.get("tailored_resume_path"), "resume")
    _attach_file(msg, job.get("cover_letter_path"), "cover letter")
    return msg


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_notifications(min_score: int = 7, referral_threshold: int = 9) -> dict:
    """Send email notifications for referral targets and blocked/manual jobs.

    Args:
        min_score: Minimum fit_score for blocked-job notifications.
        referral_threshold: fit_score >= this triggers a referral email (default 9).

    Returns:
        Dict with keys: referral_sent, blocked_sent, errors.
    """
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    webhook_url = os.environ.get("WEBHOOK_URL", "")

    has_email = smtp_user and smtp_pass
    has_webhook = bool(webhook_url)

    if not has_email and not has_webhook:
        from rich.console import Console
        Console().print(
            "[yellow]Notify stage skipped:[/yellow] No delivery method configured.\n"
            "Set SMTP_USER + SMTP_PASS (email) or WEBHOOK_URL in ~/.applypilot/.env."
        )
        return {"referral_sent": 0, "blocked_sent": 0, "captcha_sent": 0, "errors": 0}

    # Resolve recipient
    notify_email = os.environ.get("NOTIFY_EMAIL", "")
    if not notify_email:
        try:
            profile = config.load_profile()
            notify_email = profile.get("personal", {}).get("email", "")
        except FileNotFoundError:
            pass
    if not notify_email:
        from rich.console import Console
        Console().print(
            "[yellow]Notify stage skipped:[/yellow] No recipient email found.\n"
            "Set NOTIFY_EMAIL in .env or run 'applypilot init' to set up your profile."
        )
        return {"referral_sent": 0, "blocked_sent": 0, "errors": 0}

    conn = get_connection()
    referral_sent = 0
    blocked_sent = 0
    errors = 0

    # ── 1. Referral targets ──────────────────────────────────────────────────
    rows = conn.execute("""
        SELECT url, title, site, application_url, tailored_resume_path,
               cover_letter_path, fit_score, score_reasoning, location, salary
        FROM jobs
        WHERE fit_score >= ?
          AND tailored_resume_path IS NOT NULL
          AND notified_at IS NULL
          AND applied_at IS NULL
          AND (apply_status IS NULL OR apply_status != 'applied')
        ORDER BY fit_score DESC
    """, (referral_threshold,)).fetchall()

    for row in rows:
        job = dict(row)
        score = job.get("fit_score", "?")
        site = job.get("site", "")
        title = job.get("title", "Role")
        subject = f"\U0001f3af Referral opportunity: {title} @ {site} (score: {score}/10)"
        msg = _build_message(subject, _html_referral(job), job, smtp_user, notify_email)
        try:
            if has_email:
                _send_email(msg, smtp_user, smtp_pass)
            if has_webhook:
                _call_webhook(job, "referral", webhook_url)
            _mark_notified(job["url"], "referral")
            referral_sent += 1
            log.info("Referral notification sent: %s @ %s", title[:40], site)
        except Exception as e:
            log.error("Failed to notify referral for %s: %s", job["url"][:60], e)
            errors += 1

    # ── 2. Blocked / manual ATS jobs ─────────────────────────────────────────
    blocked_sites, blocked_patterns = config.load_blocked_sites()
    manual_domains = config.load_sites_config().get("manual_ats", [])

    # Build parameterized WHERE clause for blocked sites
    params: list = [min_score, referral_threshold]
    clauses: list[str] = []

    if blocked_sites:
        placeholders = ",".join("?" * len(blocked_sites))
        clauses.append(f"site IN ({placeholders})")
        params.extend(blocked_sites)

    for pat in blocked_patterns:
        clauses.append("url LIKE ?")
        params.append(pat)

    for domain in manual_domains:
        clauses.append("url LIKE ?")
        params.append(f"%{domain}%")
        clauses.append("application_url LIKE ?")
        params.append(f"%{domain}%")

    if not clauses:
        # No blocked sites configured — nothing to notify
        pass
    else:
        site_filter = " OR ".join(clauses)
        rows = conn.execute(f"""
            SELECT url, title, site, application_url, tailored_resume_path,
                   cover_letter_path, fit_score, score_reasoning, location, salary
            FROM jobs
            WHERE fit_score >= ?
              AND fit_score < ?
              AND tailored_resume_path IS NOT NULL
              AND notified_at IS NULL
              AND ({site_filter})
            ORDER BY fit_score DESC
        """, params).fetchall()

        for row in rows:
            job = dict(row)
            score = job.get("fit_score", "?")
            site = job.get("site", "")
            title = job.get("title", "Role")
            subject = f"\U0001f4cb Manual apply needed: {title} @ {site} (score: {score}/10)"
            msg = _build_message(subject, _html_blocked(job), job, smtp_user, notify_email)
            try:
                if has_email:
                    _send_email(msg, smtp_user, smtp_pass)
                if has_webhook:
                    _call_webhook(job, "blocked", webhook_url)
                _mark_notified(job["url"], "blocked")
                blocked_sent += 1
                log.info("Blocked notification sent: %s @ %s", title[:40], site)
            except Exception as e:
                log.error("Failed to notify blocked for %s: %s", job["url"][:60], e)
                errors += 1

    # ── 3. CAPTCHA-failed jobs ────────────────────────────────────────────────
    captcha_sent = 0
    rows = conn.execute("""
        SELECT url, title, site, application_url, tailored_resume_path,
               cover_letter_path, fit_score, score_reasoning, location, salary
        FROM jobs
        WHERE apply_status = 'captcha'
          AND tailored_resume_path IS NOT NULL
          AND notified_at IS NULL
        ORDER BY fit_score DESC
    """).fetchall()

    for row in rows:
        job = dict(row)
        score = job.get("fit_score", "?")
        site = job.get("site", "")
        title = job.get("title", "Role")
        subject = f"\U0001f512 CAPTCHA blocked: {title} @ {site} (score: {score}/10)"
        msg = _build_message(subject, _html_captcha(job), job, smtp_user, notify_email)
        try:
            if has_email:
                _send_email(msg, smtp_user, smtp_pass)
            if has_webhook:
                _call_webhook(job, "captcha", webhook_url)
            _mark_notified(job["url"], "captcha")
            captcha_sent += 1
            log.info("CAPTCHA notification sent: %s @ %s", title[:40], site)
        except Exception as e:
            log.error("Failed to notify CAPTCHA for %s: %s", job["url"][:60], e)
            errors += 1

    return {
        "referral_sent": referral_sent,
        "blocked_sent": blocked_sent,
        "captcha_sent": captcha_sent,
        "errors": errors,
    }
