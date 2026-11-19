"""Helyi Whisper-alapú STT (Speech-to-Text) és SRT-generáló modul modell-cache-sel."""

from __future__ import annotations

import gc
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from plex_subtitler.config import app1_config
from plex_subtitler.logger import get_logger

log = get_logger(__name__, level=app1_config.log_level)

_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}
_MODEL_CACHE_LOCK = threading.Lock()


class TranscriptionError(Exception):
    """Transzkripciós hiba."""


def _load_model() -> Any:
    """Betölti vagy cache-ből adja vissza a Whisper modellt."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ImportError(
            "A 'faster-whisper' csomag nincs telepítve!\nTelepítsd: pip install faster-whisper"
        ) from exc

    key = (
        app1_config.whisper_model,
        app1_config.whisper_device,
        app1_config.whisper_compute_type,
    )
    with _MODEL_CACHE_LOCK:
        if key in _MODEL_CACHE:
            log.debug("Whisper modell cache-ből használva: %s", key)
            return _MODEL_CACHE[key]

        log.debug(
            "Whisper modell betöltése: '%s' | eszköz: '%s' | compute_type: '%s'",
            key[0],
            key[1],
            key[2],
        )
        model = WhisperModel(key[0], device=key[1], compute_type=key[2])
        _MODEL_CACHE.clear()
        _MODEL_CACHE[key] = model
        return model


def unload_model() -> bool:
    """Felszabadítja a betöltött Whisper modellt és a CUDA VRAM-ot (pl. helyi LLM előtt)."""
    with _MODEL_CACHE_LOCK:
        had_model = bool(_MODEL_CACHE)
        _MODEL_CACHE.clear()
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            if hasattr(torch.cuda, "ipc_collect"):
                torch.cuda.ipc_collect()
    except ImportError:
        pass
    if had_model:
        log.info("🧹 Whisper modell felszabadítva a memóriából / VRAM-ból.")
    return had_model


def _seconds_to_srt_timestamp(seconds: float) -> str:
    """Másodperceket SRT időbélyeg formátumra alakít (pl. 3723.456 → '01:02:03,456')."""
    total_ms = max(0, int(round(seconds * 1000)))
    td = timedelta(milliseconds=total_ms)
    total_sec = int(td.total_seconds())
    ms = total_ms % 1000
    hours, rem = divmod(total_sec, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _write_srt(
    segments: Any,
    srt_path: Path,
    total_duration: float = 0.0,
    progress_callback: Callable[[float, float, float], None] | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Kiírja a Whisper szegmenseket SRT formátumban, előrehaladás-követéssel."""
    start_time = time.time()
    count = 0
    with open(srt_path, "w", encoding="utf-8") as f:
        for seg in segments:
            if stop_event and stop_event.is_set():
                raise TranscriptionError("A transzkripció felhasználói kérésre megszakítva.")
            txt = seg.text.strip()
            if not txt:
                continue
            count += 1
            start_ts = _seconds_to_srt_timestamp(seg.start)
            end_ts = _seconds_to_srt_timestamp(seg.end)
            f.write(f"{count}\n{start_ts} --> {end_ts}\n{txt}\n\n")

            if count == 1 or count % 20 == 0:
                log.info("🎙️ [%s] Felismert szöveg (#%d): %s", start_ts.split(",")[0], count, txt[:100])

            if progress_callback and total_duration > 0:
                pct = min(1.0, max(0.0, seg.end / total_duration))
                elapsed = time.time() - start_time
                eta = (elapsed / pct) * (1.0 - pct) if pct > 0.01 else 0.0
                progress_callback(pct, elapsed, eta)

    log.debug("SRT szegmensek kiírva: %d db", count)
    return count


def transcribe_to_srt(
    audio_path: Path,
    output_dir: Path | None = None,
    progress_callback: Callable[[float, float, float], None] | None = None,
    stop_event: threading.Event | None = None,
) -> Path:
    """Átírja az audiofájlt szöveggé és SRT feliratfájlt generál."""
    srt_dir = Path(output_dir) if output_dir else audio_path.parent
    srt_dir.mkdir(parents=True, exist_ok=True)
    srt_path = srt_dir / f"{audio_path.stem}.srt"

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
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            word_timestamps=False,
        )
        log.info(
            "Felismert nyelv: '%s' (valószínűség: %.1f%%)",
            info.language,
            info.language_probability * 100,
        )
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        _write_srt(
            segments,
            srt_path,
            total_duration=duration,
            progress_callback=progress_callback,
            stop_event=stop_event,
        )
    except TranscriptionError:
        if srt_path.exists():
            srt_path.unlink(missing_ok=True)
        raise
    except ImportError:
        raise
    except Exception as exc:
        raise TranscriptionError(
            f"Whisper hiba a(z) '{audio_path.name}' feldolgozásakor: {exc}"
        ) from exc

    log.info("SRT felirat elkészült: '%s'", srt_path.name)
    return srt_path
