"""Cloudinary file storage for ApplyPilot.

Uploads PDFs and text files to Cloudinary with a 5-day TTL tag.
Returns a direct public URL usable as a download link.

Required env var:
    CLOUDINARY_URL=cloudinary://api_key:api_secret@cloud_name

Files are tagged with ttl_5days so a scheduled Cloudinary webhook/rule
can auto-delete them. The returned URL is permanent for that 5-day window.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_TTL_DAYS = int(os.environ.get("FILE_TTL_DAYS", "5"))
_TTL_TAG = f"ttl_{_TTL_DAYS}days"


def _cloudinary():
    """Lazy import cloudinary to avoid hard-import at module load."""
    try:
        import cloudinary
        import cloudinary.uploader
        return cloudinary, cloudinary.uploader
    except ImportError as exc:
        raise ImportError(
            "cloudinary package not installed. "
            "Run: pip install cloudinary"
        ) from exc


def _ensure_configured() -> None:
    """Configure Cloudinary from CLOUDINARY_URL if not already set."""
    cloudinary_mod, _ = _cloudinary()
    cfg = cloudinary_mod.config()
    if cfg.cloud_name:
        return  # already configured

    url = os.environ.get("CLOUDINARY_URL", "")
    if not url:
        raise EnvironmentError(
            "CLOUDINARY_URL not set. "
            "Add it to ~/.applypilot/.env as: "
            "CLOUDINARY_URL=cloudinary://api_key:api_secret@cloud_name"
        )
    cloudinary_mod.config(cloudinary_url=url)


def upload_file(path: Path, public_id: str | None = None) -> dict:
    """Upload a file to Cloudinary and return URL + expiry info.

    Args:
        path:      Local path to the file (PDF or text).
        public_id: Optional Cloudinary public_id. Defaults to the stem of
                   the filename so the URL is predictable and deduplicated.

    Returns:
        {
            "url":        "https://res.cloudinary.com/...",
            "public_id":  "Palantir_SWE_resume",
            "expires_at": "2026-06-16T14:30:00Z",   # now + TTL_DAYS
        }

    Raises:
        EnvironmentError: CLOUDINARY_URL not set.
        ImportError:      cloudinary package not installed.
        RuntimeError:     Upload failed.
    """
    _ensure_configured()
    _, uploader = _cloudinary()

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    pid = public_id or path.stem
    resource_type = "raw"  # PDFs and text treated as raw (not image)

    log.debug("Uploading %s to Cloudinary as '%s'", path.name, pid)
    try:
        result = uploader.upload(
            str(path),
            public_id=pid,
            resource_type=resource_type,
            tags=[_TTL_TAG, "applypilot"],
            overwrite=True,
            invalidate=True,
            use_filename=False,
            unique_filename=False,
        )
    except Exception as exc:
        raise RuntimeError(f"Cloudinary upload failed for {path.name}: {exc}") from exc

    url = result.get("secure_url") or result.get("url", "")
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=_TTL_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    log.info("Uploaded %s -> %s (expires %s)", path.name, url, expires_at)
    return {
        "url": url,
        "public_id": result.get("public_id", pid),
        "expires_at": expires_at,
    }


def upload_job_files(job: dict) -> dict:
    """Upload resume + cover letter PDFs for a job. Returns URL dict.

    Looks for PDF siblings of tailored_resume_path and cover_letter_path.
    Falls back to the stored path itself if no PDF found.

    Args:
        job: Job dict from DB (must have tailored_resume_path key at minimum).

    Returns:
        {
            "resume_url":       str | None,
            "cover_letter_url": str | None,
            "expires_at":       str | None,
        }
    """
    result = {"resume_url": None, "cover_letter_url": None, "expires_at": None}

    def _resolve_pdf(path_str: str | None) -> Path | None:
        if not path_str:
            return None
        p = Path(path_str)
        pdf = p.with_suffix(".pdf")
        if pdf.exists():
            return pdf
        if p.exists():
            return p
        return None

    resume_path = _resolve_pdf(job.get("tailored_resume_path"))
    cover_path = _resolve_pdf(job.get("cover_letter_path"))

    expires_at = None

    if resume_path:
        try:
            r = upload_file(resume_path)
            result["resume_url"] = r["url"]
            expires_at = r["expires_at"]
        except Exception as e:
            log.error("Resume upload failed for job %s: %s", job.get("url", "")[:60], e)

    if cover_path:
        try:
            r = upload_file(cover_path)
            result["cover_letter_url"] = r["url"]
            if not expires_at:
                expires_at = r["expires_at"]
        except Exception as e:
            log.error("Cover letter upload failed for job %s: %s", job.get("url", "")[:60], e)

    result["expires_at"] = expires_at
    return result
