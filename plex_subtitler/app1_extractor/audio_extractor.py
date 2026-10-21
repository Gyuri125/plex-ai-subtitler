"""
audio_extractor.py - FFmpeg-alapú hangsáv-kinyerő modul.

Felelős:
  - A videofájlból való hangsáv-extrakció FFmpeg segítségével.
  - Az ideiglenes audiofájl kezelése és takarítása.
"""

import subprocess
import shutil
from pathlib import Path
import imageio_ffmpeg

from ..logger import get_logger
from ..config import app1_config

log = get_logger(__name__, level=app1_config.log_level)


class AudioExtractionError(Exception):
    """Hangsáv-kinyerési hiba."""


def extract_audio(video_path: Path, output_dir: Path) -> Path:
    """
    Kinyeri az audio sávot a megadott videofájlból FFmpeg segítségével.

    A Whisper STT modell 16 kHz-es, mono, WAV/MP3 formátumot kedveli,
    ezért ezekre optimalizálva konvertálunk.

    Args:
        video_path: A forrás videofájl (.mkv, .mp4, stb.) útvonala.
        output_dir: A kimeneti audiofájl tárolási mappája.

    Returns:
        A létrehozott audiofájl útvonala.

    Raises:
        AudioExtractionError: Ha az FFmpeg hiba miatt sikertelen.
        FileNotFoundError:    Ha az FFmpeg nem található a PATH-ban.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    audio_filename = video_path.stem + f".{app1_config.audio_format}"
    audio_path = output_dir / audio_filename

    log.info(
        "Audio extrakció indul: '%s' → '%s'",
        video_path.name,
        audio_path.name,
    )

    cmd = _build_ffmpeg_command(video_path, audio_path)
    log.debug("FFmpeg parancs: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3600,  # max. 1 óra
            check=False,   # Manuálisan kezeljük a hibakódot
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioExtractionError(
            f"FFmpeg időtúllépés: '{video_path.name}'"
        ) from exc
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "Az 'ffmpeg' nem található. Telepítsd és add hozzá a PATH-hoz!"
        ) from exc

    if result.returncode != 0:
        stderr_msg = result.stderr.decode("utf-8", errors="replace").strip()
        raise AudioExtractionError(
            f"FFmpeg hiba (kód {result.returncode}) '{video_path.name}' feldolgozásakor:\n"
            f"{stderr_msg}"
        )

    log.info(
        "Audio sikeresen kinyerve: '%s' (%.1f MB)",
        audio_path.name,
        audio_path.stat().st_size / 1024 / 1024,
    )
    return audio_path


def cleanup_audio(audio_path: Path) -> None:
    """
    Törli az ideiglenes audiofájlt a feldolgozás után.

    Args:
        audio_path: A törlendő audiofájl útvonala.
    """
    try:
        audio_path.unlink(missing_ok=True)
        log.debug("Ideiglenes audiofájl törölve: '%s'", audio_path.name)
    except OSError as exc:
        log.warning("Nem sikerült törölni '%s': %s", audio_path, exc)


# ---------------------------------------------------------------------------
# Belső segédfüggvények
# ---------------------------------------------------------------------------

def _build_ffmpeg_command(video_path: Path, audio_path: Path) -> list[str]:
    """
    Összeállítja az FFmpeg parancsot az audio extrakciójához.

    Opciók magyarázata:
      -y              Felülírja a kimeneti fájlt kérdés nélkül.
      -i              Bemeneti fájl.
      -vn             Nem tartalmaz video streamt a kimenetben.
      -acodec pcm_s16le WAV: 16-bit PCM (Whisper-barát). MP3-hoz: libmp3lame.
      -ar             Mintavételezési frekvencia (16000 Hz = Whisper-optimális).
      -ac 1           Mono csatorna (elegendő STT-hez, kisebb fájl).
      -map 0:a:0      Csak az első audio streamt veszi (főhang, nem kommentár).
    """
    audio_format = app1_config.audio_format.lower()
    sample_rate = str(app1_config.audio_sample_rate)

    if audio_format == "wav":
        codec_args = ["-acodec", "pcm_s16le"]
    elif audio_format == "mp3":
        codec_args = ["-acodec", "libmp3lame", "-b:a", "128k"]
    else:
        # Alapértelmezett fallback: WAV
        codec_args = ["-acodec", "pcm_s16le"]

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    return [
        ffmpeg_exe,
        "-y",
        "-i", str(video_path),
        "-vn",
        "-map", "0:a:0",
        "-ar", sample_rate,
        "-ac", "1",
        *codec_args,
        str(audio_path),
    ]
