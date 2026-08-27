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
ENV PORT=8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
