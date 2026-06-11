#!/usr/bin/env bash
# ApplyPilot — Render startup script
# Runs the full pipeline once, then exits (Render Cron Job re-runs on schedule)
# For a Background Worker (always-on), set WORKER_MODE=loop and it loops every LOOP_INTERVAL seconds.
set -euo pipefail

DATA_DIR="${APPLYPILOT_DIR:-/data}"
LOG_FILE="$DATA_DIR/logs/pipeline.log"

# Ensure directories exist
mkdir -p "$DATA_DIR/logs" "$DATA_DIR/tailored_resumes" "$DATA_DIR/cover_letters"

# Load secrets from Render env vars into the .env that applypilot reads
# (Render injects these as real env vars — applypilot's load_env() will pick them up
#  because load_dotenv() with override=False won't overwrite already-set vars)
echo "Starting ApplyPilot at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Run the pipeline: discover → score → tailor → pdf → notify
# auto-apply is included because we now have Chromium in the image
applypilot run --min-score 6 2>&1 | tee -a "$LOG_FILE"

echo "Pipeline complete at $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# If running as a loop (Background Worker mode), sleep and repeat
if [[ "${WORKER_MODE:-}" == "loop" ]]; then
    INTERVAL="${LOOP_INTERVAL:-21600}"  # default 6 hours
    echo "Worker mode: sleeping ${INTERVAL}s before next run..."
    sleep "$INTERVAL"
    exec /render-start.sh  # restart self
fi
