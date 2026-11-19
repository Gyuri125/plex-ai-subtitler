"""Egységes feldolgozási pipeline egy videofájlhoz."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Callable

from plex_subtitler.app1_extractor.audio_extractor import (
    AudioExtractionError,
    cleanup_audio,
    extract_audio,
    extract_bitmap_subtitle_ocr,
    extract_embedded_subtitle,
)
from plex_subtitler.app1_extractor.opensubtitles_client import download_opensubtitles_srt
from plex_subtitler.app1_extractor.transcriber import (
    TranscriptionError,
    transcribe_to_srt,
    unload_model,
)
from plex_subtitler.config import ROOT_DIR, app1_config, app2_config
from plex_subtitler.logger import get_logger

log = get_logger(__name__, level=app1_config.log_level)

SUPPORTED_EXTENSIONS = frozenset({".mp4", ".mkv", ".avi", ".mov", ".m4v", ".ts"})


def _resolve_writable_path(configured: str, fallback: Path, is_dir: bool) -> Path:
    """Biztosítja, hogy a célmappa létrehozható legyen; hiba esetén fallback útvonalra vált."""
    target = Path(configured)
    dir_to_create = target if is_dir else target.parent
    try:
        dir_to_create.mkdir(parents=True, exist_ok=True)
        return target
    except OSError as exc:
        log.warning(
            "A konfigurált útvonal nem írható (%s: %s), alapértelmezett használata: %s",
            dir_to_create,
            exc,
            fallback,
        )
        fb_dir = fallback if is_dir else fallback.parent
        fb_dir.mkdir(parents=True, exist_ok=True)
        return fallback


class FileProcessor:
    """Koordinálja a felirat-kinyerést, hangextrakciót, transzkripciót és takarítást."""

    def __init__(self) -> None:
        self._processed_log = _resolve_writable_path(
            app1_config.processed_log,
            ROOT_DIR / "processed_files.log",
            is_dir=False,
        )
        self._temp_dir = _resolve_writable_path(
            app1_config.temp_audio_dir,
            ROOT_DIR / "temp_audio",
            is_dir=True,
        )

    def process(
        self,
        video_path: Path | str,
        progress_callback: Callable[[float, float, float], None] | None = None,
        stop_event: threading.Event | None = None,
        on_srt_ready: Callable[[Path], None] | None = None,
        subtitle_stream_index: int | None = None,
        audio_stream_index: int | None = None,
    ) -> Path | None:
        """Feldolgoz egy videofájlt: beágyazott felirat kinyerése vagy audio + Whisper SRT."""
        video_path = Path(video_path).resolve()

        if not self._is_supported(video_path):
            log.debug("Nem támogatott formátum, kihagyjuk: '%s'", video_path.name)
            return None

        if self._is_already_processed(video_path):
            log.info("Már feldolgozva, kihagyjuk: '%s'", video_path.name)
            return None

        if self._srt_exists(video_path):
            log.info("Már létezik SRT felirat '%s' mellé, kihagyjuk.", video_path.name)
            self._mark_as_processed(video_path)
            return None

        log.info("============================================================")
        log.info("🎬 Film feldolgozás kezdete: '%s'", video_path.name)
        t0 = time.time()

        audio_path: Path | None = None
        try:
            srt_path: Path | None = None
            source_mode = "beágyazott felirat (FFmpeg)"

            force_whisper = subtitle_stream_index == -1

            if not force_whisper and app1_config.extract_embedded_subtitles:
                srt_path = extract_embedded_subtitle(
                    video_path,
                    output_srt_path=video_path.with_suffix(".srt"),
                    stop_event=stop_event,
                    stream_index=subtitle_stream_index,
                )

            if not force_whisper and srt_path is None and app1_config.enable_pgs_ocr:
                source_mode = "Blu-ray PGS OCR"
                srt_path = extract_bitmap_subtitle_ocr(
                    video_path,
                    output_srt_path=video_path.with_suffix(".srt"),
                    stop_event=stop_event,
                )

            if not force_whisper and srt_path is None and app1_config.enable_opensubtitles:
                source_mode = "OpenSubtitles API"
                srt_path = download_opensubtitles_srt(
                    video_path,
                    output_srt_path=video_path.with_suffix(".srt"),
                )

            if srt_path is None:
                source_mode = f"Whisper ({app1_config.whisper_model}/{app1_config.whisper_device})"
                audio_path = extract_audio(
                    video_path,
                    self._temp_dir,
                    stop_event=stop_event,
                    stream_index=audio_stream_index,
                )
                srt_path = transcribe_to_srt(
                    audio_path=audio_path,
                    output_dir=video_path.parent,
                    progress_callback=progress_callback,
                    stop_event=stop_event,
                )
                if (
                    app1_config.unload_whisper_before_translate
                    and app1_config.whisper_device.lower() == "cuda"
                    and app2_config.llm_provider.lower().strip() in ("local", "ollama")
                ):
                    unload_model()

            self._mark_as_processed(video_path)
            elapsed = time.time() - t0
            log.info(
                "✅ Sikeres kinyerés | Film: '%s' | Mód: %s | Idő: %.1f mp | Kimenet: '%s'",
                video_path.name,
                source_mode,
                elapsed,
                srt_path.name,
            )
            if on_srt_ready is not None:
                on_srt_ready(srt_path)
            return srt_path

        except (AudioExtractionError, TranscriptionError, FileNotFoundError) as exc:
            log.error("❌ Feldolgozási hiba '%s': %s", video_path.name, exc)
            return None
        except Exception as exc:
            log.exception("💥 Váratlan hiba '%s': %s", video_path.name, exc)
            return None
        finally:
            if audio_path is not None and app1_config.cleanup_audio:
                cleanup_audio(audio_path)

    @staticmethod
    def _is_supported(path: Path) -> bool:
        """Ellenőrzi, hogy a fájlkiterjesztés támogatott-e."""
        return path.suffix.lower() in SUPPORTED_EXTENSIONS

    def _is_already_processed(self, video_path: Path) -> bool:
        """Ellenőrzi, hogy a fájl szerepel-e a feldolgozottak naplójában."""
        if not self._processed_log.exists():
            return False
        file_id = self._file_id(video_path)
        content = self._processed_log.read_text(encoding="utf-8", errors="replace")
        return file_id in content

    def _mark_as_processed(self, video_path: Path) -> None:
        """Felveszi a fájlt a feldolgozottak naplójába."""
        file_id = self._file_id(video_path)
        with open(self._processed_log, "a", encoding="utf-8") as f:
            f.write(f"{file_id}  {video_path}\n")

    @staticmethod
    def _file_id(video_path: Path) -> str:
        """Egyedi azonosító a videofájl útvonala alapján (MD5 hash)."""
        return hashlib.md5(str(video_path).encode("utf-8")).hexdigest()

    @staticmethod
    def _srt_exists(video_path: Path) -> bool:
        """Ellenőrzi, hogy már létezik-e forrásnyelvű SRT a videó mellett."""
        stem = video_path.stem
        parent = video_path.parent
        return (parent / f"{stem}.srt").exists() or (parent / f"{stem}.en.srt").exists()
