"""Sorozat-mappa gyorsítótár (cache) és új epizód prioritás-kezelő."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from plex_subtitler.app1_extractor.file_processor import SUPPORTED_EXTENSIONS
from plex_subtitler.app2_translator.translator_daemon import (
    get_target_srt_path,
    is_translation_complete,
)
from plex_subtitler.config import SERIES_CACHE_FILE, app1_config, app2_config
from plex_subtitler.logger import get_logger

log = get_logger(__name__, level=app1_config.log_level)


class SeriesCacheManager:
    """Nyilvántartja a sorozat-mappákat és epizódokat, és előre sorolja az új részeket."""

    def __init__(self, cache_path: Path | None = None) -> None:
        self.cache_path = cache_path or SERIES_CACHE_FILE
        self.data: dict[str, Any] = {"watched_folders": [], "episodes": {}}
        self.load()

    def load(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            loaded = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data["watched_folders"] = loaded.get("watched_folders", [])
                self.data["episodes"] = loaded.get("episodes", {})
        except Exception as exc:
            log.warning("Nem sikerült betölteni a sorozat cache-t: %s", exc)

    def save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self.data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("Nem sikerült menteni a sorozat cache-t: %s", exc)

    def register_folder(self, folder: Path) -> None:
        resolved = str(Path(folder).resolve())
        if resolved not in self.data["watched_folders"]:
            self.data["watched_folders"].append(resolved)
            self.save()

    def is_episode_done(self, video_path: Path, target_language: str | None = None) -> bool:
        """Ellenőrzi cache és meglévő lefordított SRT alapján, hogy az epizód kész van-e."""
        resolved = str(video_path.resolve())
        lang = target_language or app2_config.target_language
        src_srt = video_path.with_suffix(".srt")
        target_srt = get_target_srt_path(src_srt, lang)

        if src_srt.exists() and is_translation_complete(src_srt, target_srt):
            return True

        entry = self.data["episodes"].get(resolved)
        if not entry:
            return False
        if entry.get("status") == "done" and entry.get("target_language") == lang:
            if target_srt.exists() and target_srt.stat().st_size > 0:
                return True
        return False

    def mark_episode(
        self,
        video_path: Path,
        status: str,
        src_srt: Path | None = None,
        translated_srt: Path | None = None,
        target_language: str | None = None,
    ) -> None:
        resolved = str(video_path.resolve())
        try:
            st = video_path.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0

        self.data["episodes"][resolved] = {
            "name": video_path.name,
            "size": size,
            "mtime": mtime,
            "status": status,
            "target_language": target_language or app2_config.target_language,
            "src_srt": str(src_srt) if src_srt else "",
            "translated_srt": str(translated_srt) if translated_srt else "",
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save()

    def scan_folder(
        self,
        folder: Path,
        target_language: str | None = None,
    ) -> tuple[list[Path], list[Path]]:
        """Átnézi a mappát: (már kész epizódok, feldolgozandó epizódok — újak elöl!)."""
        folder = Path(folder).resolve()
        self.register_folder(folder)

        videos = [
            p for p in folder.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        ]

        done_list: list[Path] = []
        brand_new: list[Path] = []
        known_pending: list[Path] = []

        for video in videos:
            if self.is_episode_done(video, target_language=target_language):
                if str(video.resolve()) not in self.data["episodes"]:
                    src_srt = video.with_suffix(".srt")
                    tr_srt = get_target_srt_path(src_srt, target_language)
                    self.mark_episode(video, "done", src_srt=src_srt, translated_srt=tr_srt, target_language=target_language)
                done_list.append(video)
            else:
                if str(video.resolve()) not in self.data["episodes"]:
                    brand_new.append(video)
                else:
                    known_pending.append(video)

        # Az újonnan bekerült fájlok legfrissebb módosítási idővel előre kerülnek
        brand_new.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        known_pending.sort(key=lambda p: p.name.lower())
        pending = brand_new + known_pending

        log.info(
            "📚 Sorozat mappa szkennelve ('%s'): %d kész (cache), %d új/folytatandó",
            folder.name,
            len(done_list),
            len(pending),
        )
        return sorted(done_list, key=lambda p: p.name.lower()), pending
