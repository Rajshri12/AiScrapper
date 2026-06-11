#!/usr/bin/env bash
# Run this ONCE after first deploy to upload your config files to the Render disk.
# Usage: bash render-setup.sh <render-service-id>
#
# Prerequisites:
#   brew install render  (or: pip install render-cli)
#   render login
#
# What it uploads to /data/:
#   ~/.applypilot/profile.json
#   ~/.applypilot/searches.yaml
#   ~/.applypilot/resume.txt
#   ~/.applypilot/.env  (optional — you can use Render env vars instead)

set -euo pipefail

SERVICE_ID="${1:-}"
LOCAL_DIR="${APPLYPILOT_DIR:-$HOME/.applypilot}"

if [[ -z "$SERVICE_ID" ]]; then
    echo "Usage: bash render-setup.sh <render-service-id>"
    echo "Find your service ID in the Render dashboard URL: render.com/services/srv-XXXX"
    exit 1
fi

echo "Uploading ApplyPilot config to Render disk for service $SERVICE_ID..."

for file in profile.json searches.yaml resume.txt; do
    src="$LOCAL_DIR/$file"
    if [[ -f "$src" ]]; then
        echo "  Uploading $file..."
        render ssh "$SERVICE_ID" -- "cat > /data/$file" < "$src"
    else
        echo "  WARNING: $src not found, skipping"
    fi
done

echo ""
echo "Done. Your config files are on the Render disk."
echo ""
echo "Next: Set these secrets in the Render dashboard (Environment tab):"
echo "  OPENAI_API_KEY"
echo "  SMTP_USER"
echo "  SMTP_PASS"
echo "  NOTIFY_EMAIL"
echo ""
echo "Then trigger a manual deploy or wait for the cron schedule."
