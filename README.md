<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name.

# ApplyPilot

**Fully autonomous job application pipeline. Runs every 6 hours. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Pickle-Pixel/ApplyPilot?style=social)](https://github.com/Pickle-Pixel/ApplyPilot)

---

## What It Does

ApplyPilot discovers jobs across 10+ sources, scores them against your resume with AI, tailors your resume per job, generates cover letters, and emails you the best matches — with PDFs attached and a referral request — all hands-free on a 6-hour cron cycle.

**Sources checked every run:**
- JobSpy: LinkedIn, Indeed, Glassdoor, ZipRecruiter
- ATS APIs: 70+ named companies (Anthropic, Stripe, Airbnb, Figma, Meesho, CRED, PhonePe, and more) via Greenhouse / Ashby / Lever / Workable / SmartRecruiters
- Remote boards: RemoteOK, Remotive, Himalayas, WeWorkRemotely, Jobicy, DynamiteJobs
- India boards: Naukri, Foundit
- Workday corporate portals (configurable)

**Per-job pipeline:**
1. Score — AI rates fit 1–10
2. Tailor — rewrites your resume for the JD (bullets reframed, skills rebuilt from real content + JD)
3. Cover letter — targeted, generated per job
4. Webhook — POST structured payload with Cloudinary-hosted PDF links to your backend
5. Email — if score ≥ 9, sends you an email with resume PDF + cover letter PDF attached, JD in body, and a referral request

---

## Quick Start

### 1. Install

```bash
pip install -r requirements.txt
# python-jobspy must be installed separately (pins exact numpy — works fine at runtime)
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
```

### 2. First-time setup

```bash
applypilot init        # creates ~/.applypilot/ with profile.json, searches.yaml, .env
applypilot doctor      # verify everything is wired up
```

### 3. Configure `~/.applypilot/.env`

```env
# LLM — at least one required
OPENAI_API_KEY=sk-...
# GEMINI_API_KEY=AIza...        # free tier: 15 RPM / 1M tokens/day
LLM_MODEL=gpt-4o-mini           # or gemini-2.0-flash, gpt-5.4-nano, etc.

# Email notifications (Gmail App Password)
SMTP_USER=you@gmail.com
SMTP_PASS=xxxx xxxx xxxx xxxx   # from myaccount.google.com/apppasswords
NOTIFY_EMAIL=you@gmail.com      # where to receive alerts (comma-separate for multiple)

# Webhook (optional — POST resume/cover letter links to your backend)
WEBHOOK_URL=https://your-backend.com/api/webhook
WEBHOOK_SECRET=your-secret
CLOUDINARY_URL=cloudinary://api_key:api_secret@cloud_name

# Cron interval (default: 6 hours)
CRON_INTERVAL_HOURS=6
CRON_RUN_ON_STARTUP=true        # run immediately on server start
```

### 4. Run

**Option A — CLI (one-shot, all stages):**
```bash
applypilot run
applypilot run -w 4              # 4 parallel workers for discovery/enrichment
applypilot run --min-score 8     # only tailor jobs scoring 8+
applypilot run --stream          # concurrent stages (faster on large backlogs)
```

**Option B — API server (starts cron automatically):**
```bash
applypilot serve                 # starts FastAPI on port 8000, cron fires every 6h
```
The cron runs `discover → enrich → tailor → re-score → cover → webhook → email` automatically. No manual trigger needed after first start.

**Option C — Docker:**
```bash
docker build -t applypilot .
docker run -d \
  -v ~/.applypilot:/root/.applypilot \
  -p 8000:8000 \
  applypilot
```

---

## Pipeline Stages

| Stage | What Happens |
|-------|-------------|
| **Discover** | Hits all sources above, deduplicates by URL |
| **Enrich** | Fetches full JD via JSON-LD → CSS selectors → AI extraction |
| **Score** | AI rates fit 1–10 against your profile and preferences |
| **Tailor** | Rewrites resume per JD: bullets reframed with senior-level language, skills rebuilt from your real content + JD keywords |
| **Cover letter** | Targeted letter per job, referencing role and company |
| **Notify** | Score ≥ 8 → webhook; Score ≥ 9 → email with PDFs + referral request |

---

## Cron Schedule

The API server (`applypilot serve`) uses APScheduler to run the full pipeline automatically:

```
Every 6 hours (configurable via CRON_INTERVAL_HOURS in .env)
```

To change the interval:
```env
CRON_INTERVAL_HOURS=3    # every 3 hours
CRON_INTERVAL_HOURS=12   # twice daily
```

To disable auto-run on startup (first tick fires after first interval):
```env
CRON_RUN_ON_STARTUP=false
```

---

## Configuration Files

All created by `applypilot init` in `~/.applypilot/`:

| File | Purpose |
|------|---------|
| `profile.json` | Your info: contact, work auth, compensation, experience, skills, preserved resume facts |
| `searches.yaml` | Job titles, locations, boards to search, score thresholds |
| `.env` | API keys, SMTP, webhook, cron config |

**Package configs** (shipped, edit as needed):
| File | Purpose |
|------|---------|
| `src/applypilot/config/portals.yaml` | 70+ company ATS slugs — add your target companies |
| `src/applypilot/config/employers.yaml` | Workday employer registry |
| `src/applypilot/config/sites.yaml` | Blocked sites, manual ATS domains |

---

## Adding Target Companies

To add a company to the direct ATS polling list, edit [src/applypilot/config/portals.yaml](src/applypilot/config/portals.yaml):

```yaml
- name: YourTargetCompany
  provider: greenhouse    # greenhouse | ashby | lever | workable | smartrecruiters
  slug: company-ats-slug  # the slug from their jobs URL, e.g. boards.greenhouse.io/slug
  enabled: true
```

---

## API Endpoints (when running `applypilot serve`)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/jobs/enqueue` | Submit a job URL to run through the full pipeline |
| `GET` | `/jobs` | List jobs (filter by score, status) |
| `GET` | `/jobs/{job_id}` | Single job detail |
| `POST` | `/pipeline/run` | Manually trigger pipeline stages |
| `GET` | `/status` | DB stats and pipeline state |

All write endpoints require `Authorization: Bearer <API_SECRET>`.

---

## Email Alerts

For jobs scoring **9 or 10**, you receive an email with:
- Subject: `🌟 9/10 — Job Title @ Company | Referral Appreciated`
- Highlighted referral request box
- Tailored resume PDF attached
- Cover letter PDF attached
- Full job description in body

Set `SMTP_USER`, `SMTP_PASS` (Gmail App Password), and `NOTIFY_EMAIL` in `.env`. Multiple recipients supported: `NOTIFY_EMAIL=a@gmail.com,b@gmail.com`.

---

## CLI Reference

```
applypilot init                    # First-time setup wizard
applypilot doctor                  # Verify setup, check all requirements
applypilot run [stages...]         # Run pipeline stages (or 'all')
applypilot run -w 4                # 4 parallel workers
applypilot run --stream            # Concurrent stages
applypilot run --min-score 8       # Override score threshold
applypilot run --dry-run           # Preview without executing
applypilot serve                   # Start API server + 6h cron
applypilot status                  # DB stats
applypilot dashboard               # Open HTML results dashboard
applypilot followup                # Show pending follow-up actions (day 7, day 14)
applypilot followup --draft        # Generate follow-up email drafts
applypilot analyze                 # Rejection pattern analysis (needs 50+ jobs)
applypilot apply                   # Auto-apply via browser (requires Claude Code + Node.js)
applypilot apply -w 3              # 3 parallel browser workers
applypilot apply --dry-run         # Fill forms without submitting
```

---

## Requirements

| Component | Required For | Notes |
|-----------|-------------|-------|
| Python 3.11+ | Everything | Core runtime |
| OpenAI / Gemini API key | LLM stages | Gemini free tier works fine |
| Gmail App Password | Email alerts | `myaccount.google.com/apppasswords` |
| Cloudinary account | Webhook file hosting | Free tier sufficient |
| Node.js 18+ | Auto-apply only | For Playwright MCP server |
| Claude Code CLI | Auto-apply only | [claude.ai/code](https://claude.ai/code) |

---

## License

[GNU Affero General Public License v3.0](LICENSE). If you deploy a modified version as a service, you must release your source code under the same license.
