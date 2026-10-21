"""
transcriber.py - Helyi Whisper-alapú STT (Speech-to-Text) és SRT-generáló modul.

Használt könyvtár: faster-whisper (CTranslate2-alapú, CPU/GPU-barát)
Alternatíva:       openai-whisper (eredeti, lassabb CPU-n)

A faster-whisper ajánlott:
  - ~4× gyorsabb a CPU-n, ~2× GPU-n az eredeti Whisper-hez képest.
  - Alacsonyabb memóriaigény (int8 kvantálással).
  - Azonos pontosság.
"""

from __future__ import annotations

from pathlib import Path
from datetime import timedelta

from ..logger import get_logger
from ..config import app1_config

log = get_logger(__name__, level=app1_config.log_level)


class TranscriptionError(Exception):
    """Transzkripciós hiba."""


def transcribe_to_srt(audio_path: Path, output_dir: Path | None = None) -> Path:
    """
    Átírja az audiofájlt szöveggé és SRT feliratfájlt generál.

    Az SRT fájl a videofájl mellé kerül (output_dir=None esetén),
    vagy a megadott könyvtárba.

    Args:
        audio_path:  Az elkészített audiofájl útvonala.
        output_dir:  Ha None, az audio_path szülőmappájába ír.

    Returns:
        A létrehozott .srt fájl útvonala.

    Raises:
        TranscriptionError: Ha a Whisper-modell betöltése vagy futtatása sikertelen.
        ImportError:        Ha a faster-whisper nincs telepítve.
    """
    try:
        from faster_whisper import WhisperModel  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "A 'faster-whisper' csomag nincs telepítve!\n"
            "Telepítsd: pip install faster-whisper"
        ) from exc

    srt_dir = Path(output_dir) if output_dir else audio_path.parent
    srt_dir.mkdir(parents=True, exist_ok=True)
    srt_path = srt_dir / (audio_path.stem + ".srt")

    log.info(
        "Whisper transzkripció indul | Modell: %s | Eszköz: %s | Fájl: '%s'",
        app1_config.whisper_model,
        app1_config.whisper_device,
        audio_path.name,
    )

    try:
        model = _load_model()
        segments, info = model.transcribe(
            str(audio_path),
            language=app1_config.whisper_language,
            beam_size=5,
            vad_filter=True,       # Hangaktivitás-szűrő (csökkenti a zajt)
            vad_parameters={
                "min_silence_duration_ms": 500,
            },
            word_timestamps=False,  # Elég a szegmens-szintű időbélyeg
        )
    except Exception as exc:
        raise TranscriptionError(
            f"Whisper hiba a(z) '{audio_path.name}' feldolgozásakor: {exc}"
        ) from exc

    log.info(
        "Felismert nyelv: '%s' (valószínűség: %.1f%%)",
        info.language,
        info.language_probability * 100,
    )

    # Szegmensek SRT formátumba írása
    _write_srt(segments, srt_path)

    log.info("SRT felirat elkészült: '%s'", srt_path)
    return srt_path


# ---------------------------------------------------------------------------
# Belső segédfüggvények
# ---------------------------------------------------------------------------

def _load_model():
    """
    Betölti a Whisper modellt a konfigurált beállításokkal.
    A modell az első híváskor letöltődik a Hugging Face cache-be.
    """
    from faster_whisper import WhisperModel  # type: ignore[import]

    log.debug(
        "Whisper modell betöltése: '%s' | compute_type: '%s'",
        app1_config.whisper_model,
        app1_config.whisper_compute_type,
    )

    return WhisperModel(
        app1_config.whisper_model,
        device=app1_config.whisper_device,
        compute_type=app1_config.whisper_compute_type,
    )


def _write_srt(segments, srt_path: Path) -> int:
    """
    Kiírja a Whisper szegmenseket SRT formátumban.

    SRT formátum:
        <sorszám>
        <HH:MM:SS,mmm> --> <HH:MM:SS,mmm>
        <szöveg>
        <üres sor>

    Returns:
        A kiírt szegmensek száma.
    """
    count = 0
    with srt_path.open("w", encoding="utf-8") as f:
        for i, segment in enumerate(segments, start=1):
            start_ts = _seconds_to_srt_timestamp(segment.start)
            end_ts = _seconds_to_srt_timestamp(segment.end)
            text = segment.text.strip()

            if not text:
                continue

            f.write(f"{i}\n")
            f.write(f"{start_ts} --> {end_ts}\n")
            f.write(f"{text}\n")
            f.write("\n")
            count += 1

    log.debug("SRT szegmensek kiírva: %d db", count)
    return count


def _seconds_to_srt_timestamp(seconds: float) -> str:
    """
    Másodperceket SRT időbélyeg formátumra alakít.

    Pl.: 3723.456 → '01:02:03,456'

    Args:
        seconds: Időpont másodpercben (lebegőpontos).

    Returns:
        SRT-kompatibilis időbélyeg sztring.
    """
    td = timedelta(seconds=seconds)
    total_seconds = int(td.total_seconds())
    milliseconds = round((td.total_seconds() - total_seconds) * 1000)

    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"
