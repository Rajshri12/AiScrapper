"""ApplyPilot HTTP API server.

Replaces CLI-only control so the pipeline can be managed after Render deployment.

Endpoints:
    POST /jobs/enqueue          Submit a job URL → full pipeline → POST to your backend
    GET  /jobs                  List jobs with optional filters
    GET  /jobs/{job_id}         Single job detail (job_id = URL-encoded job URL)
    POST /pipeline/run          Trigger pipeline stages
    GET  /status                Pipeline stats + DB summary

Auth:
    All write endpoints require  Authorization: Bearer <API_SECRET>
    Set API_SECRET in ~/.applypilot/.env

The webhook payload sent to WEBHOOK_URL:
    {
        "job":   { title, company, url, apply_url, location, salary, score, ... },
        "files": { resume_url, cover_letter_url, expires_at },
        "meta":  { notify_type, pipeline_run_at }
    }
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import unquote

log = logging.getLogger(__name__)

# ── Scheduler state (module-level so lifespan can start/stop it) ─────────────
_scheduler = None
_pipeline_lock = threading.Lock()  # prevent overlapping pipeline runs


def _run_full_pipeline() -> None:
    """Discover → enrich → then per-job: tailor → re-score → cover → webhook."""
    if not _pipeline_lock.acquire(blocking=False):
        log.info("[cron] Pipeline already running — skipping this tick")
        return
    try:
        log.info("[cron] Starting scheduled pipeline run...")
        from applypilot.config import load_env
        load_env()

        # Stage 1: Discover new jobs
        log.info("[cron] Stage: discover")
        from applypilot.pipeline import _run_discover
        _run_discover()

        # Stage 2: Enrich (fetch full descriptions for new jobs)
        log.info("[cron] Stage: enrich")
        from applypilot.pipeline import _run_enrich
        _run_enrich()

        # Stage 3: Per-job pipeline for all enriched but not yet tailored jobs
        from applypilot.database import get_connection, init_db
        init_db()
        conn = get_connection()
        pending = conn.execute("""
            SELECT url FROM jobs
            WHERE full_description IS NOT NULL
              AND (tailored_resume_path IS NULL)
              AND COALESCE(tailor_attempts, 0) < 3
            ORDER BY discovered_at DESC
        """).fetchall()

        log.info("[cron] %d jobs to process through per-job pipeline", len(pending))
        for row in pending:
            try:
                _run_single_job_pipeline(row[0])
            except Exception as e:
                log.error("[cron] Per-job pipeline failed for %s: %s", row[0], e, exc_info=True)

        log.info("[cron] Scheduled pipeline run complete")
    except Exception as e:
        log.error("[cron] Pipeline run failed: %s", e, exc_info=True)
    finally:
        _pipeline_lock.release()


@asynccontextmanager
async def _lifespan(app):
    global _scheduler
    from applypilot.config import load_env
    load_env()

    interval_hours = float(os.environ.get("CRON_INTERVAL_HOURS", "6"))
    run_on_startup = os.environ.get("CRON_RUN_ON_STARTUP", "true").lower() == "true"

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        _scheduler = BackgroundScheduler(daemon=True)
        _scheduler.add_job(
            _run_full_pipeline,
            trigger="interval",
            hours=interval_hours,
            id="pipeline_cron",
            max_instances=1,
            coalesce=True,
        )
        _scheduler.start()
        log.info("[cron] Scheduler started — pipeline runs every %.0fh", interval_hours)
    except ImportError:
        log.warning("[cron] apscheduler not installed — scheduled runs disabled")

    if run_on_startup:
        log.info("[cron] Running pipeline immediately on startup...")
        threading.Thread(target=_run_full_pipeline, daemon=True).start()

    yield  # server is live here

    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("[cron] Scheduler stopped")


# ---------------------------------------------------------------------------
# Request/Response models — must be at module level for Pydantic to resolve
# ---------------------------------------------------------------------------

try:
    from pydantic import BaseModel

    class EnqueueRequest(BaseModel):
        url: str
        priority: bool = False

    class PipelineRunRequest(BaseModel):
        stages: list[str] = ["all"]
        min_score: int = 6
        workers: int = 1
        stream: bool = False

except ImportError:
    pass  # fastapi/pydantic not installed yet — create_app() will raise clearly


# ---------------------------------------------------------------------------
# FastAPI app factory (lazy so the module can be imported without fastapi)
# ---------------------------------------------------------------------------

def create_app():
    try:
        from fastapi import FastAPI, HTTPException, Depends
        from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
        from fastapi.middleware.cors import CORSMiddleware
    except ImportError as exc:
        raise ImportError(
            "fastapi and uvicorn not installed. "
            "Run: pip install fastapi uvicorn"
        ) from exc

    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db, get_connection, get_stats

    load_env()
    ensure_dirs()
    init_db()

    app = FastAPI(
        title="ApplyPilot API",
        description="Control the ApplyPilot pipeline via HTTP",
        version="1.0.0",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Auth ────────────────────────────────────────────────────────────────

    _bearer = HTTPBearer(auto_error=False)

    def _require_auth(
        creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    ):
        secret = os.environ.get("API_SECRET", "")
        if not secret:
            return  # no secret set → open (dev mode)
        if not creds or creds.credentials != secret:
            raise HTTPException(status_code=401, detail="Invalid or missing API_SECRET token")

    # ── Background job runner ───────────────────────────────────────────────

    def _run_single_job_pipeline(job_url: str) -> None:
        """Enrich → tailor → re-score tailored resume → if 8+ cover + notify, else drop."""
        import json, re
        from applypilot.database import get_connection, init_db
        from applypilot.config import load_env, RESUME_PATH, TAILORED_DIR, COVER_LETTER_DIR, load_profile

        load_env()
        init_db()

        conn = get_connection()

        def _reload() -> dict:
            row = conn.execute("SELECT * FROM jobs WHERE url = ?", (job_url,)).fetchone()
            return dict(row) if row else {}

        job = _reload()
        if not job:
            log.error("Enqueue: job not found in DB: %s", job_url)
            return

        log.info("Pipeline starting for: %s", job_url)

        # ── Enrich ──────────────────────────────────────────────────────────
        if not job.get("full_description"):
            log.info("  [enrich] fetching description...")
            try:
                from playwright.sync_api import sync_playwright
                from applypilot.enrichment.detail import scrape_detail_page
                now = datetime.now(timezone.utc).isoformat()
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    ).new_page()
                    result = scrape_detail_page(page, job_url)
                    browser.close()
                if result.get("full_description"):
                    conn.execute(
                        "UPDATE jobs SET full_description=?, application_url=?, detail_scraped_at=? WHERE url=?",
                        (result["full_description"], result.get("application_url"), now, job_url),
                    )
                    conn.commit()
                    log.info("  [enrich] ok (tier %s, %d chars)", result.get("tier_used"), len(result["full_description"]))
                else:
                    conn.execute("UPDATE jobs SET detail_scraped_at=?, detail_error=? WHERE url=?",
                                 (now, result.get("error", "no data"), job_url))
                    conn.commit()
                    log.warning("  [enrich] no description: %s", result.get("error"))
            except Exception as e:
                log.error("  [enrich] failed: %s", e, exc_info=True)
        job = _reload()

        if not job.get("full_description"):
            log.warning("  dropping: no description extracted")
            return

        # ── Is this a software role? quick keyword filter ────────────────────
        desc_lower = (job.get("full_description") or "").lower()
        title_lower = (job.get("title") or "").lower()
        swe_keywords = {
            "software", "engineer", "developer", "programming", "frontend", "backend",
            "fullstack", "full stack", "full-stack", "devops", "data", "machine learning",
            "ml", "ai", "python", "javascript", "typescript", "java", "react", "node",
            "api", "cloud", "aws", "gcp", "azure", "swe", "sde", "intern", "web", "mobile",
        }
        if not any(kw in title_lower or kw in desc_lower[:500] for kw in swe_keywords):
            log.info("  dropping: not a software role (%s)", job.get("title"))
            return

        # ── Tailor (unconditionally — no pre-score gate) ─────────────────────
        if not job.get("tailored_resume_path"):
            log.info("  [tailor] tailoring for: %s", job.get("title"))
            try:
                from applypilot.scoring.tailor import tailor_resume
                from applypilot.scoring.pdf import convert_to_pdf
                profile = load_profile()
                resume_text = RESUME_PATH.read_text(encoding="utf-8")
                tailored, report, data = tailor_resume(resume_text, job, profile)
                log.info("  [tailor] status=%s", report.get("status"))

                safe_title = re.sub(r"[^\w\s-]", "", (job.get("title") or ""))[:50].strip().replace(" ", "_")
                safe_site  = re.sub(r"[^\w\s-]", "", (job.get("site")  or ""))[:20].strip().replace(" ", "_")
                prefix = f"{safe_site}_{safe_title}" or "job"
                TAILORED_DIR.mkdir(parents=True, exist_ok=True)

                txt_path = TAILORED_DIR / f"{prefix}.txt"
                txt_path.write_text(tailored, encoding="utf-8")
                (TAILORED_DIR / f"{prefix}_DATA.json").write_text(json.dumps(data, indent=2), encoding="utf-8")

                pdf_path = None
                if report.get("status") in ("approved", "approved_with_judge_warning"):
                    try:
                        pdf_path = str(convert_to_pdf(txt_path, data=data, profile=profile))
                    except Exception as pe:
                        log.warning("  [tailor/pdf] PDF failed: %s", pe)

                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, tailor_attempts=COALESCE(tailor_attempts,0)+1 WHERE url=?",
                    (pdf_path or str(txt_path), now, job_url),
                )
                conn.commit()
                log.info("  [tailor] saved: %s", pdf_path or txt_path)
            except Exception as e:
                log.error("  [tailor] failed: %s", e, exc_info=True)
        job = _reload()

        if not job.get("tailored_resume_path"):
            log.warning("  dropping: tailor failed")
            return

        # ── Re-score using the TAILORED resume text ──────────────────────────
        if not job.get("fit_score"):
            log.info("  [score] scoring tailored resume...")
            try:
                from applypilot.scoring.scorer import score_job
                tailored_path = job["tailored_resume_path"]
                # Read tailored text — prefer .txt sibling of the PDF
                from pathlib import Path as _Path
                txt_candidate = _Path(tailored_path).with_suffix(".txt")
                if txt_candidate.exists():
                    tailored_text = txt_candidate.read_text(encoding="utf-8")
                else:
                    tailored_text = RESUME_PATH.read_text(encoding="utf-8")
                result = score_job(tailored_text, job)
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE jobs SET fit_score=?, score_reasoning=?, scored_at=? WHERE url=?",
                    (result["score"], result.get("reasoning", ""), now, job_url),
                )
                conn.commit()
                log.info("  [score] tailored score=%s", result["score"])
            except Exception as e:
                log.error("  [score] failed: %s", e, exc_info=True)
        job = _reload()

        fit_score = job.get("fit_score") or 0
        if fit_score < 8:
            log.info("  dropping: tailored score=%s (need 8+)", fit_score)
            return

        log.info("  score=%s >= 8 — proceeding to cover + notify", fit_score)

        # ── Cover letter ─────────────────────────────────────────────────────
        if not job.get("cover_letter_path"):
            log.info("  [cover] generating cover letter...")
            try:
                from applypilot.scoring.cover_letter import generate_cover_letter
                from applypilot.scoring.pdf import convert_to_pdf
                profile = load_profile()
                resume_text = RESUME_PATH.read_text(encoding="utf-8")
                letter = generate_cover_letter(resume_text, job, profile)

                safe_title = re.sub(r"[^\w\s-]", "", (job.get("title") or ""))[:50].strip().replace(" ", "_")
                safe_site  = re.sub(r"[^\w\s-]", "", (job.get("site")  or ""))[:20].strip().replace(" ", "_")
                prefix = f"{safe_site}_{safe_title}" or "job"
                COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)

                cl_path = COVER_LETTER_DIR / f"{prefix}_CL.txt"
                cl_path.write_text(letter, encoding="utf-8")

                pdf_path = None
                try:
                    pdf_path = str(convert_to_pdf(cl_path))
                except Exception as pe:
                    log.warning("  [cover/pdf] PDF failed: %s", pe)

                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                    (pdf_path or str(cl_path), now, job_url),
                )
                conn.commit()
                log.info("  [cover] saved: %s", pdf_path or cl_path)
            except Exception as e:
                log.error("  [cover] failed: %s", e, exc_info=True)
        job = _reload()

        # ── Notify: upload to Cloudinary + POST to dashboard ─────────────────
        webhook_url = os.environ.get("WEBHOOK_URL", "")
        if webhook_url and not job.get("notified_at"):
            log.info("  [notify] uploading + posting webhook...")
            try:
                from applypilot.scoring.notify import _call_webhook, _mark_notified
                _call_webhook(job, "enqueued", webhook_url)
                _mark_notified(job_url, "enqueued")
                log.info("  [notify] webhook delivered")
            except Exception as e:
                log.error("  [notify] webhook failed: %s", e)

        # ── Email: if score 9+ send resume + cover letter + JD to NOTIFY_EMAIL ─
        if fit_score >= 9:
            try:
                import smtplib
                from email.mime.multipart import MIMEMultipart
                from email.mime.text import MIMEText
                from email.mime.application import MIMEApplication
                from pathlib import Path as _Path

                smtp_user  = os.environ.get("SMTP_USER", "")
                smtp_pass  = os.environ.get("SMTP_PASS", "")
                notify_email = os.environ.get("NOTIFY_EMAIL", "")

                if smtp_user and smtp_pass and notify_email:
                    title   = job.get("title") or "Role"
                    company = job.get("site") or ""
                    apply_url = job.get("application_url") or job.get("url", "")
                    jd_text = (job.get("full_description") or "")[:3000]

                    html = f"""<html><body style="font-family:sans-serif;max-width:640px;margin:auto;color:#222">
<h2 style="color:#4CAF50">&#127775; Strong Match — {fit_score}/10: {title} @ {company}</h2>

<div style="background:#fff8e1;border-left:4px solid #FFC107;padding:14px 18px;border-radius:4px;margin:16px 0">
  <strong>&#128276; Referral Request</strong><br>
  If you know anyone at <strong>{company}</strong>, please consider passing along my resume and putting in a referral. Referred candidates are significantly more likely to move forward — it would mean a lot!
</div>

<p><strong>Apply link:</strong> <a href="{apply_url}">{apply_url}</a></p>
<p><strong>Fit Score:</strong> {fit_score}/10 — tailored resume and cover letter attached.</p>
<hr style="border:none;border-top:1px solid #eee;margin:16px 0">
<h3>Job Description</h3>
<pre style="white-space:pre-wrap;font-size:13px;color:#444">{jd_text}</pre>
<p style="color:#888;font-size:12px;margin-top:32px">Sent by ApplyPilot</p>
</body></html>"""

                    msg = MIMEMultipart("mixed")
                    msg["Subject"] = f"\U0001f31f {fit_score}/10 — {title} @ {company}"
                    msg["From"]    = smtp_user
                    msg["To"]      = notify_email
                    msg.attach(MIMEText(html, "html"))

                    # Attach resume PDF
                    resume_p = _Path(job.get("tailored_resume_path") or "")
                    if resume_p.exists():
                        part = MIMEApplication(resume_p.read_bytes(), Name=resume_p.name)
                        part["Content-Disposition"] = f'attachment; filename="{resume_p.name}"'
                        msg.attach(part)

                    # Attach cover letter PDF
                    cover_p = _Path(job.get("cover_letter_path") or "")
                    if cover_p.exists():
                        part = MIMEApplication(cover_p.read_bytes(), Name=cover_p.name)
                        part["Content-Disposition"] = f'attachment; filename="{cover_p.name}"'
                        msg.attach(part)

                    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                        server.login(smtp_user, smtp_pass)
                        server.send_message(msg)
                    log.info("  [email] sent to %s (score=%s)", notify_email, fit_score)
                else:
                    log.debug("  [email] skipped — SMTP_USER/SMTP_PASS/NOTIFY_EMAIL not set")
            except Exception as e:
                log.error("  [email] failed: %s", e)

        log.info("Pipeline complete for: %s", job_url)

    def _insert_job_stub(job_url: str) -> bool:
        """Insert a bare job record if the URL isn't already in the DB.

        Returns True if it was a new insertion.
        """
        from applypilot.database import get_connection
        from datetime import datetime, timezone

        conn = get_connection()
        existing = conn.execute(
            "SELECT url FROM jobs WHERE url = ?", (job_url,)
        ).fetchone()
        if existing:
            return False

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR IGNORE INTO jobs (url, title, site, strategy, discovered_at)
               VALUES (?, ?, ?, ?, ?)""",
            (job_url, "", _domain_from_url(job_url), "api_enqueue", now),
        )
        conn.commit()
        return True

    def _domain_from_url(url: str) -> str:
        try:
            from urllib.parse import urlparse
            return urlparse(url).netloc.replace("www.", "")
        except Exception:
            return ""

    # ── Endpoints ───────────────────────────────────────────────────────────

    @app.post("/jobs/enqueue", dependencies=[Depends(_require_auth)])
    def enqueue_job(req: EnqueueRequest):
        """Submit a job URL. Pipeline runs in background, results POSTed to WEBHOOK_URL."""
        import threading
        _insert_job_stub(req.url)
        threading.Thread(target=_run_single_job_pipeline, args=(req.url,), daemon=True).start()
        return {
            "status": "queued",
            "url": req.url,
            "message": "Pipeline started. Results will be sent to WEBHOOK_URL when complete.",
        }

    @app.get("/jobs")
    def list_jobs(
        limit: int = 50,
        offset: int = 0,
        min_score: Optional[int] = None,
        notified: Optional[bool] = None,
        site: Optional[str] = None,
    ):
        """List jobs. Filter by min_score, notified status, or site."""
        conn = get_connection()
        clauses = []
        params: list = []

        if min_score is not None:
            clauses.append("fit_score >= ?")
            params.append(min_score)
        if notified is True:
            clauses.append("notified_at IS NOT NULL")
        elif notified is False:
            clauses.append("notified_at IS NULL")
        if site:
            clauses.append("site LIKE ?")
            params.append(f"%{site}%")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([limit, offset])

        rows = conn.execute(
            f"""SELECT url, title, site, location, salary, fit_score,
                       score_reasoning, tailored_resume_path, cover_letter_path,
                       notified_at, notify_type, discovered_at, applied_at, apply_status
                FROM jobs {where}
                ORDER BY COALESCE(fit_score, 0) DESC
                LIMIT ? OFFSET ?""",
            params,
        ).fetchall()

        total = conn.execute(
            f"SELECT COUNT(*) FROM jobs {where}",
            params[:-2],
        ).fetchone()[0]

        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "jobs": [dict(r) for r in rows],
        }

    @app.get("/jobs/{job_id:path}")
    def get_job(job_id: str):
        """Get a single job by URL (URL-encoded)."""
        url = unquote(job_id)
        conn = get_connection()
        row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Job not found")
        return dict(row)

    @app.post("/pipeline/run", dependencies=[Depends(_require_auth)])
    def pipeline_run(req: PipelineRunRequest):
        """Trigger pipeline stages in background (replaces `applypilot run`)."""
        import threading
        def _run():
            from applypilot.pipeline import run_pipeline
            run_pipeline(stages=req.stages, min_score=req.min_score,
                         workers=req.workers, stream=req.stream)
        threading.Thread(target=_run, daemon=True).start()
        return {
            "status": "started",
            "stages": req.stages,
            "message": f"Running stages: {', '.join(req.stages)}",
        }

    @app.post("/pipeline/trigger", dependencies=[Depends(_require_auth)])
    def pipeline_trigger():
        """Manually trigger a full pipeline run right now (same as the cron job)."""
        if not _pipeline_lock.acquire(blocking=False):
            return {"status": "already_running", "message": "Pipeline is already in progress"}
        _pipeline_lock.release()
        threading.Thread(target=_run_full_pipeline, daemon=True).start()
        return {"status": "started", "message": "Full pipeline triggered manually"}

    @app.get("/status")
    def status():
        """Pipeline statistics, DB summary, and scheduler state."""
        stats = get_stats()
        webhook_url = os.environ.get("WEBHOOK_URL", "")
        cloudinary_url = os.environ.get("CLOUDINARY_URL", "")
        interval_hours = float(os.environ.get("CRON_INTERVAL_HOURS", "6"))

        next_run = None
        if _scheduler and _scheduler.running:
            job = _scheduler.get_job("pipeline_cron")
            if job and job.next_run_time:
                next_run = job.next_run_time.isoformat()

        return {
            "pipeline": stats,
            "scheduler": {
                "running": bool(_scheduler and _scheduler.running),
                "interval_hours": interval_hours,
                "next_run": next_run,
                "pipeline_busy": not _pipeline_lock.acquire(blocking=False) or _pipeline_lock.release() or False,
            },
            "config": {
                "webhook_configured": bool(webhook_url),
                "cloudinary_configured": bool(cloudinary_url),
                "api_secret_set": bool(os.environ.get("API_SECRET", "")),
            },
        }

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


# ---------------------------------------------------------------------------
# ASGI app instance — created once at module import for uvicorn
# ---------------------------------------------------------------------------

app = create_app()
