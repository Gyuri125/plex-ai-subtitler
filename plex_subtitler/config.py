"""
config.py - Centralizált konfigurációs fájl JSON alapú tárolással.

Ez a modul lecseréli az .env alapú konfigurációt egy lokális, hordozható
settings.json fájlra, amelyet a GUI is képes írni és olvasni.
"""

import os
import json
import sys
from pathlib import Path
from dataclasses import dataclass, field, asdict

# PyInstaller esetén az aktuális futtatási könyvtár (nem a temp MEIPASS mappa!)
if getattr(sys, 'frozen', False):
    ROOT_DIR = Path(sys.executable).parent
else:
    ROOT_DIR = Path(__file__).resolve().parent

SETTINGS_FILE = ROOT_DIR / "settings.json"


@dataclass
class App1Config:
    """Konfiguráció az 1. alkalmazáshoz (Audio Extractor & Whisper)."""

    # --- Mappák ---
    watch_dirs: list[str] = field(default_factory=list)
    temp_audio_dir: str = str(ROOT_DIR / "temp_audio")

    # --- FFmpeg ---
    ffmpeg_path: str = "ffmpeg"
    audio_format: str = "wav"
    audio_sample_rate: int = 16000

    # --- Whisper ---
    whisper_model: str = "small"   # Alapértelmezés (hardware_manager felülírhatja)
    whisper_language: str | None = None
    whisper_device: str = "cpu"    # Alapértelmezés
    whisper_compute_type: str = "int8"

    # --- Állapot / Naplózás ---
    watchdog_poll_interval: int = 5
    processed_log: str = str(ROOT_DIR / "processed_files.log")
    cleanup_audio: bool = True
    log_level: str = "INFO"


@dataclass
class App2Config:
    """Konfiguráció a 2. alkalmazáshoz (AI Translator Agent)."""
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str | None = None
    translation_batch_size: int = 30
    target_language: str = "Hungarian"
    translation_style_hint: str = "Use natural, colloquial language as used in Hungarian movie dubbing."
    api_request_delay: float = 0.5
    log_level: str = "INFO"


class SettingsManager:
    """
    Kezeli a settings.json fájlból történő betöltést és mentést.
    """
    def __init__(self):
        self.app1 = App1Config()
        self.app2 = App2Config()
        self.load()

    def load(self) -> None:
        """Betölti a beállításokat a JSON fájlból, ha létezik."""
        if not SETTINGS_FILE.exists():
            # Ha nincs beállítás fájl, létrehozzuk az alapértelmezésekkel
            self.save()
            return
            
        try:
            with SETTINGS_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
                
            if "app1" in data:
                for k, v in data["app1"].items():
                    if hasattr(self.app1, k):
                        setattr(self.app1, k, v)
                        
            if "app2" in data:
                for k, v in data["app2"].items():
                    if hasattr(self.app2, k):
                        setattr(self.app2, k, v)
        except Exception as e:
            print(f"Hiba a beállítások betöltésekor: {e}")

    def save(self) -> None:
        """Kimenti az aktuális beállításokat a JSON fájlba."""
        data = {
            "app1": asdict(self.app1),
            "app2": asdict(self.app2)
        }
        try:
            with SETTINGS_FILE.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Hiba a beállítások mentésekor: {e}")


# Singleton globális példány (így visszafelé kompatibilis marad a kód)
settings_manager = SettingsManager()
app1_config = settings_manager.app1
app2_config = settings_manager.app2
