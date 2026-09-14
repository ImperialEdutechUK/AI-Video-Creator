FROM python:3.11-slim

# ffmpeg/ffprobe = video pipeline, libgl1 = opencv runtime dep,
# tesseract-ocr = the pytesseract watermark-text detector.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Railway sets $PORT at runtime; default to 8000 for local `docker run`.
ENV PORT=8000 \
    WORKERS=1 \
    PIPELINE_PREP_WORKERS=1 \
    UPLOAD_CHUNK_MB=1 \
    MAX_QUEUED_JOBS=20 \
    MAX_JOBS_PER_SESSION=5 \
    MIN_FREE_DISK_MB=1024 \
    DISK_HEADROOM_FACTOR=2.5 \
    UPLOAD_RESERVATION_TTL_SECONDS=3600
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
