# NotebookLM download task for the AWS deployment (ECS Fargate Spot).
# Build from the REPOSITORY ROOT:
#   docker build -f aws/automation/Dockerfile -t slc-video-aws-automation .
# Pinned to Debian 12: Playwright 1.49's browser dependencies don't install on
# Debian 13, which plain python:3.12-slim now uses.
FROM python:3.12-slim-bookworm

WORKDIR /srv
COPY aws/automation/requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY aws/automation/rules.py aws/automation/notebooklm.py aws/automation/runner.py aws/automation/creator.py aws/automation/creator_rules.py aws/automation/creator_runner.py ./
# Unit podcasts and study guides. podcast_creator.py + fonts/ are an unchanged copy
# of the Podcast Creator tool's rendering code (Podcast-Creator/backend).
COPY aws/automation/extras_rules.py aws/automation/extras_runner.py aws/automation/studyguide.py aws/automation/garamond_widths.py aws/automation/podcast_creator.py ./
COPY aws/automation/fonts/ ./fonts/

ENV PYTHONUNBUFFERED=1
CMD ["python", "runner.py"]
