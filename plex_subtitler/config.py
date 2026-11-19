"""Centralizált konfigurációkezelő (.env + settings.json támogatással)."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

if getattr(sys, "frozen", False):
    ROOT_DIR = Path(sys.executable).resolve().parent
else:
    ROOT_DIR = Path(__file__).resolve().parent.parent

SETTINGS_FILE = ROOT_DIR / "settings.json"
LOGS_DIR = ROOT_DIR / "logs"
SERIES_CACHE_FILE = ROOT_DIR / "series_cache.json"
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
APP_ICON_PATH = (
    (Path(getattr(sys, "_MEIPASS", "")) / "assets" / "app.ico")
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")
    else (ASSETS_DIR / "app.ico")
)

LANGUAGE_CODE_MAP: dict[str, str] = {
    "hungarian": "hu",
    "magyar": "hu",
    "hu": "hu",
    "english": "en",
    "angol": "en",
    "en": "en",
    "german": "de",
    "német": "de",
    "nemet": "de",
    "deutsch": "de",
    "de": "de",
    "spanish": "es",
    "spanyol": "es",
    "español": "es",
    "espanol": "es",
    "es": "es",
    "french": "fr",
    "francia": "fr",
    "fr": "fr",
    "italian": "it",
    "olasz": "it",
    "it": "it",
}

KNOWN_TRANSLATED_SUFFIXES: tuple[str, ...] = tuple(
    f".{code}.srt" for code in sorted(set(LANGUAGE_CODE_MAP.values()))
)

SUBTITLE_PREF_MAP: dict[str, tuple[str, ...]] = {
    "auto": ("eng", "en", "spa", "es", "deu", "ger", "de", "hun", "hu", "und"),
    "eng": ("eng", "en", "und"),
    "en": ("eng", "en", "und"),
    "english": ("eng", "en", "und"),
    "spa": ("spa", "es", "und"),
    "es": ("spa", "es", "und"),
    "spanish": ("spa", "es", "und"),
    "deu": ("deu", "ger", "de", "und"),
    "ger": ("deu", "ger", "de", "und"),
    "de": ("deu", "ger", "de", "und"),
    "german": ("deu", "ger", "de", "und"),
    "hun": ("hun", "hu", "und"),
    "hu": ("hun", "hu", "und"),
    "hungarian": ("hun", "hu", "und"),
    "fre": ("fre", "fra", "fr", "und"),
    "fra": ("fre", "fra", "fr", "und"),
    "fr": ("fre", "fra", "fr", "und"),
    "ita": ("ita", "it", "und"),
    "it": ("ita", "it", "und"),
}


def get_preferred_subtitle_languages(pref: str | None) -> tuple[str, ...]:
    """Visszaadja a beágyazott felirat- vagy hangsávok preferált FFmpeg/ISO nyelvkód sorrendjét."""
    cleaned = (pref or "auto").strip().lower()
    if not cleaned:
        return SUBTITLE_PREF_MAP["auto"]
    if "," in cleaned:
        parts = [p.strip() for p in cleaned.split(",") if p.strip()]
        return tuple(parts) if parts else SUBTITLE_PREF_MAP["auto"]
    return SUBTITLE_PREF_MAP.get(cleaned, (cleaned, "und"))


get_preferred_audio_languages = get_preferred_subtitle_languages


def get_language_code(language_name: str) -> str:
    """ISO 639-1 kétbetűs nyelvkódot ad vissza a célnyelv nevéből (pl. German -> de)."""
    cleaned = (language_name or "").strip().lower()
    if cleaned in LANGUAGE_CODE_MAP:
        return LANGUAGE_CODE_MAP[cleaned]
    if len(cleaned) == 2 and cleaned.isalpha():
        return cleaned
    return "hu"


def _find_env_file() -> Path | None:
    """Megkeresi az első elérhető .env fájlt a gyökér vagy szülő mappákban."""
    candidates = [
        ROOT_DIR / ".env",
        Path.cwd() / ".env",
        *[(p / ".env") for p in ROOT_DIR.parents[:4]],
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _parse_env_file(env_path: Path) -> dict[str, str]:
    """Egyszerű, külső függőség nélküli .env elemző."""
    values: dict[str, str] = {}
    try:
        for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                val = val[1:-1]
            values[key] = val
    except OSError as exc:
        _log.warning("Nem sikerült beolvasni a .env fájlt (%s): %s", env_path, exc)
    return values


def _parse_bool(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "igen", "on")


@dataclass
class App1Config:
    """Konfiguráció az 1. alkalmazáshoz (Audio/Subtitle Extractor & Whisper STT)."""

    watch_dirs: list[str] = field(default_factory=list)
    temp_audio_dir: str = str(ROOT_DIR / "temp_audio")
    ffmpeg_path: str = "ffmpeg"
    audio_format: str = "wav"
    audio_sample_rate: int = 16000
    audio_stream_index: int = 0
    preferred_audio_language: str = "auto"
    extract_embedded_subtitles: bool = True
    enable_pgs_ocr: bool = True
    enable_opensubtitles: bool = False
    opensubtitles_api_key: str = ""
    preferred_subtitle_language: str = "auto"
    confirm_streams_on_drop: bool = False
    pause_when_gaming: bool = True
    auto_translate_after_extract: bool = False
    unload_whisper_before_translate: bool = True
    scan_existing_on_start: bool = True
    whisper_model: str = "small"
    whisper_language: str | None = None
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    watchdog_poll_interval: int = 5
    processed_log: str = str(ROOT_DIR / "processed_files.log")
    cleanup_audio: bool = True
    log_level: str = "INFO"


@dataclass
class App2Config:
    """Konfiguráció a 2. alkalmazáshoz (AI Translator Agent)."""

    llm_provider: str = "google"
    llm_api_key: str = ""
    llm_model: str = "gemini-2.5-flash"
    llm_base_url: str | None = None
    translation_batch_size: int = 30
    source_language: str = "auto"
    target_language: str = "Hungarian"
    translation_style_hint: str = "Use natural, colloquial language as used in movie dubbing."
    glossary: dict[str, str] = field(default_factory=dict)
    api_request_delay: float = 0.5
    format_tv_subtitles: bool = True
    max_chars_per_line: int = 42
    max_lines_per_block: int = 2
    auto_start_ollama: bool = True
    auto_pull_ollama_model: bool = True
    auto_movie_context: bool = True
    ollama_keep_alive: str = "5m"
    plex_refresh_enabled: bool = False
    plex_url: str = "http://localhost:32400"
    plex_token: str = ""
    auto_align_subtitles: bool = False
    desktop_notifications: bool = True
    notification_sound: bool = True
    ui_mode: str = "simple"
    log_level: str = "INFO"


class SettingsManager:
    """Kezeli a .env és a settings.json alapú betöltést és mentést."""

    def __init__(self, settings_path: Path | None = None, env_path: Path | None = None) -> None:
        self.settings_path = settings_path or SETTINGS_FILE
        self.env_path = env_path if env_path is not None else _find_env_file()
        self.settings_existed_on_load: bool = False
        self.app1 = App1Config()
        self.app2 = App2Config()
        self.load()

    def _apply_env_overrides(self) -> None:
        """Alkalmazza a .env fájlból és a környezeti változókból érkező értékeket."""
        env_values: dict[str, str] = {}
        if self.env_path and self.env_path.is_file():
            env_values.update(_parse_env_file(self.env_path))
        for k, v in os.environ.items():
            if k in {
                "WATCH_DIRS", "TEMP_AUDIO_DIR", "FFMPEG_PATH", "AUDIO_FORMAT",
                "AUDIO_SAMPLE_RATE", "AUDIO_STREAM_INDEX", "PREFERRED_AUDIO_LANGUAGE",
                "EXTRACT_EMBEDDED_SUBTITLES", "ENABLE_PGS_OCR", "ENABLE_OPENSUBTITLES",
                "OPENSUBTITLES_API_KEY", "PREFERRED_SUBTITLE_LANGUAGE",
                "CONFIRM_STREAMS_ON_DROP", "PAUSE_WHEN_GAMING",
                "AUTO_TRANSLATE_AFTER_EXTRACT", "UNLOAD_WHISPER_BEFORE_TRANSLATE",
                "SCAN_EXISTING_ON_START", "WHISPER_MODEL", "WHISPER_LANGUAGE",
                "WHISPER_DEVICE", "WHISPER_COMPUTE_TYPE", "WATCHDOG_POLL_INTERVAL",
                "PROCESSED_LOG", "CLEANUP_AUDIO", "APP1_LOG_LEVEL", "LLM_PROVIDER",
                "LLM_API_KEY", "LLM_MODEL", "LLM_BASE_URL", "TRANSLATION_BATCH_SIZE",
                "SOURCE_LANGUAGE", "TARGET_LANGUAGE", "TRANSLATION_STYLE_HINT",
                "API_REQUEST_DELAY", "FORMAT_TV_SUBTITLES", "AUTO_START_OLLAMA",
                "AUTO_PULL_OLLAMA_MODEL", "AUTO_MOVIE_CONTEXT",
                "OLLAMA_KEEP_ALIVE", "PLEX_REFRESH_ENABLED", "PLEX_URL",
                "PLEX_TOKEN", "AUTO_ALIGN_SUBTITLES", "DESKTOP_NOTIFICATIONS",
                "NOTIFICATION_SOUND", "UI_MODE", "APP2_LOG_LEVEL",
            }:
                env_values[k] = v

        if not env_values:
            return

        if "WATCH_DIRS" in env_values:
            raw_dirs = env_values["WATCH_DIRS"].strip()
            self.app1.watch_dirs = [d.strip() for d in raw_dirs.split(",") if d.strip()] if raw_dirs else []
        if env_values.get("TEMP_AUDIO_DIR"):
            self.app1.temp_audio_dir = env_values["TEMP_AUDIO_DIR"]
        if env_values.get("FFMPEG_PATH"):
            self.app1.ffmpeg_path = env_values["FFMPEG_PATH"]
        if env_values.get("AUDIO_FORMAT"):
            self.app1.audio_format = env_values["AUDIO_FORMAT"]
        if env_values.get("AUDIO_SAMPLE_RATE"):
            self.app1.audio_sample_rate = int(env_values["AUDIO_SAMPLE_RATE"])
        if env_values.get("AUDIO_STREAM_INDEX"):
            self.app1.audio_stream_index = int(env_values["AUDIO_STREAM_INDEX"])
        if env_values.get("PREFERRED_AUDIO_LANGUAGE"):
            self.app1.preferred_audio_language = env_values["PREFERRED_AUDIO_LANGUAGE"]
        if "EXTRACT_EMBEDDED_SUBTITLES" in env_values:
            self.app1.extract_embedded_subtitles = _parse_bool(env_values["EXTRACT_EMBEDDED_SUBTITLES"])
        if "ENABLE_PGS_OCR" in env_values:
            self.app1.enable_pgs_ocr = _parse_bool(env_values["ENABLE_PGS_OCR"])
        if "ENABLE_OPENSUBTITLES" in env_values:
            self.app1.enable_opensubtitles = _parse_bool(env_values["ENABLE_OPENSUBTITLES"])
        if env_values.get("OPENSUBTITLES_API_KEY"):
            self.app1.opensubtitles_api_key = env_values["OPENSUBTITLES_API_KEY"]
        if env_values.get("PREFERRED_SUBTITLE_LANGUAGE"):
            self.app1.preferred_subtitle_language = env_values["PREFERRED_SUBTITLE_LANGUAGE"]
        if "CONFIRM_STREAMS_ON_DROP" in env_values:
            self.app1.confirm_streams_on_drop = _parse_bool(env_values["CONFIRM_STREAMS_ON_DROP"])
        if "PAUSE_WHEN_GAMING" in env_values:
            self.app1.pause_when_gaming = _parse_bool(env_values["PAUSE_WHEN_GAMING"])
        if "AUTO_TRANSLATE_AFTER_EXTRACT" in env_values:
            self.app1.auto_translate_after_extract = _parse_bool(env_values["AUTO_TRANSLATE_AFTER_EXTRACT"])
        if "UNLOAD_WHISPER_BEFORE_TRANSLATE" in env_values:
            self.app1.unload_whisper_before_translate = _parse_bool(env_values["UNLOAD_WHISPER_BEFORE_TRANSLATE"])
        if "SCAN_EXISTING_ON_START" in env_values:
            self.app1.scan_existing_on_start = _parse_bool(env_values["SCAN_EXISTING_ON_START"])
        if env_values.get("WHISPER_MODEL"):
            self.app1.whisper_model = env_values["WHISPER_MODEL"]
        if "WHISPER_LANGUAGE" in env_values:
            self.app1.whisper_language = env_values["WHISPER_LANGUAGE"].strip() or None
        if env_values.get("WHISPER_DEVICE"):
            self.app1.whisper_device = env_values["WHISPER_DEVICE"]
        if env_values.get("WHISPER_COMPUTE_TYPE"):
            self.app1.whisper_compute_type = env_values["WHISPER_COMPUTE_TYPE"]
        if env_values.get("WATCHDOG_POLL_INTERVAL"):
            self.app1.watchdog_poll_interval = int(env_values["WATCHDOG_POLL_INTERVAL"])
        if env_values.get("PROCESSED_LOG"):
            self.app1.processed_log = env_values["PROCESSED_LOG"]
        if "CLEANUP_AUDIO" in env_values:
            self.app1.cleanup_audio = _parse_bool(env_values["CLEANUP_AUDIO"])
        if env_values.get("APP1_LOG_LEVEL"):
            self.app1.log_level = env_values["APP1_LOG_LEVEL"]

        if env_values.get("LLM_PROVIDER"):
            self.app2.llm_provider = env_values["LLM_PROVIDER"]
        if env_values.get("LLM_API_KEY"):
            self.app2.llm_api_key = env_values["LLM_API_KEY"]
        if env_values.get("LLM_MODEL"):
            self.app2.llm_model = env_values["LLM_MODEL"]
        if "LLM_BASE_URL" in env_values:
            self.app2.llm_base_url = env_values["LLM_BASE_URL"].strip() or None
        if env_values.get("TRANSLATION_BATCH_SIZE"):
            self.app2.translation_batch_size = int(env_values["TRANSLATION_BATCH_SIZE"])
        if env_values.get("SOURCE_LANGUAGE"):
            self.app2.source_language = env_values["SOURCE_LANGUAGE"]
        if env_values.get("TARGET_LANGUAGE"):
            self.app2.target_language = env_values["TARGET_LANGUAGE"]
        if env_values.get("TRANSLATION_STYLE_HINT"):
            self.app2.translation_style_hint = env_values["TRANSLATION_STYLE_HINT"]
        if env_values.get("API_REQUEST_DELAY"):
            self.app2.api_request_delay = float(env_values["API_REQUEST_DELAY"])
        if "FORMAT_TV_SUBTITLES" in env_values:
            self.app2.format_tv_subtitles = _parse_bool(env_values["FORMAT_TV_SUBTITLES"])
        if "AUTO_START_OLLAMA" in env_values:
            self.app2.auto_start_ollama = _parse_bool(env_values["AUTO_START_OLLAMA"])
        if "AUTO_PULL_OLLAMA_MODEL" in env_values:
            self.app2.auto_pull_ollama_model = _parse_bool(env_values["AUTO_PULL_OLLAMA_MODEL"])
        if "AUTO_MOVIE_CONTEXT" in env_values:
            self.app2.auto_movie_context = _parse_bool(env_values["AUTO_MOVIE_CONTEXT"])
        if env_values.get("OLLAMA_KEEP_ALIVE"):
            self.app2.ollama_keep_alive = env_values["OLLAMA_KEEP_ALIVE"]
        if "PLEX_REFRESH_ENABLED" in env_values:
            self.app2.plex_refresh_enabled = _parse_bool(env_values["PLEX_REFRESH_ENABLED"])
        if env_values.get("PLEX_URL"):
            self.app2.plex_url = env_values["PLEX_URL"]
        if env_values.get("PLEX_TOKEN"):
            self.app2.plex_token = env_values["PLEX_TOKEN"]
        if "AUTO_ALIGN_SUBTITLES" in env_values:
            self.app2.auto_align_subtitles = _parse_bool(env_values["AUTO_ALIGN_SUBTITLES"])
        if "DESKTOP_NOTIFICATIONS" in env_values:
            self.app2.desktop_notifications = _parse_bool(env_values["DESKTOP_NOTIFICATIONS"])
        if "NOTIFICATION_SOUND" in env_values:
            self.app2.notification_sound = _parse_bool(env_values["NOTIFICATION_SOUND"])
        if env_values.get("UI_MODE"):
            mode_val = env_values["UI_MODE"].strip().lower()
            if mode_val in ("simple", "advanced"):
                self.app2.ui_mode = mode_val
        if env_values.get("APP2_LOG_LEVEL"):
            self.app2.log_level = env_values["APP2_LOG_LEVEL"]

    def load(self) -> None:
        """Betölti a beállításokat (előbb .env, majd settings.json felülbírálás)."""
        self._apply_env_overrides()
        if not self.settings_path.exists():
            self.settings_existed_on_load = False
            return
        self.settings_existed_on_load = True
        try:
            with open(self.settings_path, "r", encoding="utf-8-sig") as f:
                data: dict[str, Any] = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("A settings.json gyökéreleme nem JSON objektum.")
            for k, v in data.get("app1", {}).items():
                if hasattr(self.app1, k):
                    setattr(self.app1, k, v)
            for k, v in data.get("app2", {}).items():
                if hasattr(self.app2, k):
                    if k == "llm_api_key" and not v and self.app2.llm_api_key:
                        continue
                    setattr(self.app2, k, v)
        except Exception as exc:
            _log.warning("Hiba a beállítások betöltésekor (%s): %s", self.settings_path, exc)
            try:
                backup = self.settings_path.with_suffix(".json.corrupt.bak")
                self.settings_path.replace(backup)
                _log.warning("A sérült settings.json átnevezve biztonsági másolatként: '%s'", backup)
                self.settings_existed_on_load = False
            except OSError:
                pass

    def save(self) -> None:
        """Atomikusan kimenti az aktuális beállításokat a settings.json fájlba."""
        payload = {
            "app1": asdict(self.app1),
            "app2": asdict(self.app2),
        }
        tmp_path = self.settings_path.with_suffix(".json.tmp")
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.settings_path)
        except Exception as exc:
            _log.error("Hiba a beállítások mentésekor (%s): %s", self.settings_path, exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


settings_manager = SettingsManager()
app1_config = settings_manager.app1
app2_config = settings_manager.app2
