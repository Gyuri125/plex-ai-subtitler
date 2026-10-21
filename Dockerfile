# =============================================================================
# Plex AI Subtitler - Headless Docker Image (Home Server / NAS / Unraid)
# =============================================================================

FROM python:3.11-slim

# Rendszerszintű függőségek telepítése (FFmpeg & alapcsomagok)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Függőségek másolása és telepítése
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Forráskód másolása
COPY plex_subtitler ./plex_subtitler
COPY settings.json.example ./settings.json.example

# Média és konfig mappák előkészítése
RUN mkdir -p /media /app/plex_subtitler/temp_audio

# Környezeti változók (Python unbuffered logokhoz)
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Konténer indítása headless (CLI) módban
ENTRYPOINT ["python", "-m", "plex_subtitler.app1_extractor.main", "--cli", "--scan-existing"]
