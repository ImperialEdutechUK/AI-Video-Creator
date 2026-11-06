# Video worker for the AWS deployment (ECS Fargate Spot).
#
# Build from the REPOSITORY ROOT so the image uses the exact same
# backend/app/pipeline.py, assets and fonts as the Railway service:
#
#   docker build -f aws/worker/Dockerfile -t slc-video-aws-worker .
#
# Same base image and system packages as backend/Dockerfile.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY aws/worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Unmodified pipeline package (pipeline.py + assets + fonts).
COPY backend/app ./app
COPY aws/worker/worker.py ./worker.py

ENV PYTHONUNBUFFERED=1 \
    PIPELINE_PREP_WORKERS=1 \
    IDLE_EXIT_SECONDS=120

CMD ["python", "worker.py"]
