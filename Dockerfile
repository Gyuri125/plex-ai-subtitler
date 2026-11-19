# =============================================================================
# Plex AI Subtitler v1.3.0 - Headless Docker Image (Home Server / NAS / Unraid)
# =============================================================================

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tesseract-ocr \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py doctor_gpu.py settings.example.json ./
COPY plex_subtitler ./plex_subtitler
COPY assets ./assets

RUN mkdir -p /media /app/logs /app/temp_audio

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["python", "main.py", "--cli", "--app", "both", "--scan-existing"]
