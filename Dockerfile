# ApplyPilot — Render deployment image
# Stages: base → python deps → chromium → app
FROM python:3.12-slim AS base

# System deps: chromium (headless), pdflatex
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium headless
    chromium \
    chromium-driver \
    # Fonts for Chrome
    fonts-liberation \
    fonts-noto \
    # pdflatex — minimal TeX Live bundle
    texlive-latex-base \
    texlive-latex-recommended \
    texlive-latex-extra \
    texlive-fonts-recommended \
    # Network / DNS + download tools
    ca-certificates \
    curl \
    # Build tools needed by some Python wheels
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Tell Playwright / our chrome.py where Chromium lives
ENV CHROME_PATH=/usr/bin/chromium
ENV CHROMIUM_PATH=/usr/bin/chromium
# Playwright needs this to not try to download its own browser
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
# Chromium needs this to run as root in container
ENV CHROME_DEVEL_SANDBOX=/usr/bin/chromium

# ---- Python deps ----
WORKDIR /app
COPY pyproject.toml ./
# Install app in editable mode so we can COPY src after
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        typer \
        rich \
        httpx \
        beautifulsoup4 \
        "playwright>=1.40" \
        python-dotenv \
        pyyaml \
        pandas \
        python-jobspy \
        lxml \
        openai

# Install playwright chromium (uses system chromium via env var, but still needs browser bindings)
RUN playwright install chromium --with-deps 2>/dev/null || true

# ---- App source ----
COPY src/ ./src/
COPY README.md ./
COPY config/ ./config/
RUN pip install --no-cache-dir -e .

# ---- User config is mounted from Render Disk at /data ----
# Config files (profile.json, searches.yaml, resume.txt, .env) live in /data
# DB lives in /data/applypilot.db
# Tailored resumes / PDFs in /data/tailored_resumes/
ENV APPLYPILOT_DIR=/data

# ---- Entrypoint ----
COPY render-start.sh /render-start.sh
RUN chmod +x /render-start.sh

CMD ["/render-start.sh"]
