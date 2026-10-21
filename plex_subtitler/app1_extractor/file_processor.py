"""
file_processor.py - Egységes feldolgozási pipeline egy videofájlhoz.

Ez a modul koordinálja:
  1. Audio extrakció (FFmpeg)
  2. Transzkripció (Whisper → SRT)
  3. Takarítás (ideiglenes fájlok törlése)
  4. Feldolgozottság naplózása (duplikátum-szűrés)
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..logger import get_logger
from ..config import app1_config
from .audio_extractor import extract_audio, cleanup_audio, AudioExtractionError
from .transcriber import transcribe_to_srt, TranscriptionError
from .subtitle_extractor import try_extract_embedded_subtitles

log = get_logger(__name__, level=app1_config.log_level)

# Videofájl kiterjesztések, amiket figyelünk
SUPPORTED_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts"}


class FileProcessor:
    """
    Egyetlen videofájl teljes feldolgozási ciklusát végzi.

    Felelős:
    - Duplikátum-szűrés (feldolgozottság-napló alapján).
    - Audio extrakció → Whisper transzkripció pipeline futtatása.
    - Ideiglenes fájlok takarítása.
    - Eredmény (SRT) elhelyezése a videó mellé.
    """

    def __init__(self) -> None:
        self._processed_log = Path(app1_config.processed_log)
        self._temp_dir = Path(app1_config.temp_audio_dir)
        self._processed_log.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Publikus API
    # ------------------------------------------------------------------

    def process(self, video_path: Path) -> Path | None:
        """
        Feldolgoz egy videofájlt: audio kinyerés + SRT generálás.

        Args:
            video_path: A feldolgozandó videofájl útvonala.

        Returns:
            A létrehozott .srt fájl útvonala, vagy None hiba esetén.
        """
        video_path = Path(video_path).resolve()

        # --- Előfeltételek ellenőrzése ---
        if not self._is_supported(video_path):
            log.debug("Nem támogatott formátum, kihagyjuk: '%s'", video_path.name)
            return None

        if self._is_already_processed(video_path):
            log.info("Már feldolgozva, kihagyjuk: '%s'", video_path.name)
            return None

        if self._srt_exists(video_path):
            log.info(
                "Már létezik SRT felirat '%s' mellé, kihagyjuk.",
                video_path.name,
            )
            self._mark_as_processed(video_path)
            return None

        # --- Pipeline futtatása ---
        log.info("=" * 60)
        log.info("Feldolgozás kezdete: '%s'", video_path.name)
        log.info("=" * 60)

        # 0. lépés: Beágyazott feliratok vizsgálata és gyorskinyerése (100x gyorsulás)
        if app1_config.extract_embedded_subs:
            try:
                emb_srt, is_hu = try_extract_embedded_subtitles(video_path)
                if emb_srt:
                    self._mark_as_processed(video_path)
                    if is_hu:
                        log.info("Beagyazott MAGYAR felirat kinyerve: '%s'. A video kesz!", emb_srt.name)
                    else:
                        log.info("Beagyazott felirat kinyerve: '%s'. Whisper kihagyva, kesz a forditasra.", emb_srt.name)
                    return emb_srt
            except Exception as exc:
                log.warning("Hiba a beagyazott felirat vizsgalatatkor, fallback Whisperre: %s", exc)

        # 0.5. lépés: Online felirat keresése és letöltése az internetről (OpenSubtitles)
        if app1_config.download_online_subtitles:
            try:
                from plex_subtitler.subtitle_downloader import try_download_subtitles
                online_srt, is_hu = try_download_subtitles(video_path)
                if online_srt:
                    self._mark_as_processed(video_path)
                    if app1_config.auto_sync_subtitles:
                        from plex_subtitler.sub_sync import auto_sync_subtitle
                        auto_sync_subtitle(video_path, online_srt)
                    return online_srt
            except Exception as exc:
                log.warning("Hiba az online feliratletoltes soran, fallback Whisperre: %s", exc)

        audio_path: Path | None = None
        try:
            # 1. lépés: Audio extrakció
            audio_path = extract_audio(video_path, self._temp_dir)

            # 2. lépés: Whisper transzkripció
            # Az SRT-t a videofájl mellé helyezzük
            srt_path = transcribe_to_srt(
                audio_path=audio_path,
                output_dir=video_path.parent,
            )

            # 2.5. lépés: Quality Gate hallucináció-szűrés és Auto-Sync
            if app1_config.quality_gate_enabled:
                try:
                    from plex_subtitler.app2_translator.srt_parser import SrtParser
                    from plex_subtitler.quality_gate import clean_whisper_blocks
                    parser = SrtParser(srt_path)
                    cleaned = clean_whisper_blocks(parser.blocks)
                    SrtParser.save_srt(cleaned, srt_path)
                except Exception as qg_err:
                    log.warning("Quality Gate figyelmeztetes: %s", qg_err)

            if app1_config.auto_sync_subtitles:
                try:
                    from plex_subtitler.sub_sync import auto_sync_subtitle
                    auto_sync_subtitle(video_path, srt_path)
                except Exception as sync_err:
                    log.warning("Auto-sync figyelmeztetes: %s", sync_err)

            # 3. lépés: Siker naplózása
            self._mark_as_processed(video_path)
            log.info("Sikeresen feldolgozva: '%s' -> '%s'", video_path.name, srt_path.name)
            return srt_path

        except (AudioExtractionError, TranscriptionError, FileNotFoundError) as exc:
            log.error("Feldolgozasi hiba '%s': %s", video_path.name, exc)
            return None

        except Exception as exc:
            log.exception("Varatlan hiba '%s': %s", video_path.name, exc)
            return None

        finally:
            # 4. lépés: Takarítás (mindig lefut, még hiba esetén is)
            if audio_path and app1_config.cleanup_audio:
                cleanup_audio(audio_path)

    # ------------------------------------------------------------------
    # Belső segédmetódusok
    # ------------------------------------------------------------------

    @staticmethod
    def _is_supported(path: Path) -> bool:
        """Ellenőrzi, hogy a fájlkiterjesztés támogatott-e."""
        return path.suffix.lower() in SUPPORTED_EXTENSIONS

    def _is_already_processed(self, video_path: Path) -> bool:
        """
        Ellenőrzi, hogy a fájl szerepel-e a feldolgozottak naplójában.
        A fájl azonosítása az abszolút útvonal MD5 hash-e alapján történik
        (útvonal-változásra érzékeny, de könnyű).
        """
        file_id = self._file_id(video_path)
        if not self._processed_log.exists():
            return False
        return file_id in self._processed_log.read_text(encoding="utf-8")

    def _mark_as_processed(self, video_path: Path) -> None:
        """Felveszi a fájlt a feldolgozottak naplójába."""
        file_id = self._file_id(video_path)
        with self._processed_log.open("a", encoding="utf-8") as f:
            f.write(f"{file_id}  {video_path}\n")

    @staticmethod
    def _file_id(video_path: Path) -> str:
        """Egyedi azonosító a videofájl útvonala alapján (MD5 hash)."""
        return hashlib.md5(str(video_path).encode()).hexdigest()

    @staticmethod
    def _srt_exists(video_path: Path) -> bool:
        """
        Ellenőrzi, hogy már létezik-e angol SRT a videó mellé.
        Ellenőrzött minták: film.srt, film.en.srt
        """
        stem = video_path.stem
        parent = video_path.parent
        return (parent / f"{stem}.srt").exists() or (parent / f"{stem}.en.srt").exists()
