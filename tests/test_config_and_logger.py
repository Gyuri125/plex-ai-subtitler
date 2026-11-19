"""Tesztek a konfigurációhoz (.env + settings.json), naplózóhoz és hardverkezelőhöz."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from plex_subtitler.app1_extractor.hardware_manager import HardwareManager
from plex_subtitler.config import (
    SettingsManager,
    app1_config,
    get_language_code,
    get_preferred_subtitle_languages,
)
from plex_subtitler.logger import get_logger


def test_language_codes_mapping() -> None:
    assert get_language_code("Hungarian") == "hu"
    assert get_language_code("magyar") == "hu"
    assert get_language_code("German") == "de"
    assert get_language_code("Német") == "de"
    assert get_language_code("Spanish") == "es"
    assert get_language_code("Spanyol") == "es"
    assert get_language_code("English") == "en"
    assert get_language_code("fr") == "fr"
    assert get_language_code("unknown-lang") == "hu"
    assert get_preferred_subtitle_languages("spa") == ("spa", "es", "und")
    assert get_preferred_subtitle_languages("deu") == ("deu", "ger", "de", "und")
    assert "eng" in get_preferred_subtitle_languages("auto")


def test_config_loads_env_and_json_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\ufeff"
        "# komment\n"
        'WATCH_DIRS="C:/Movies, D:/Series"\n'
        "WHISPER_MODEL=medium\n"
        "AUDIO_STREAM_INDEX=1\n"
        "EXTRACT_EMBEDDED_SUBTITLES=false\n"
        "AUTO_TRANSLATE_AFTER_EXTRACT=true\n"
        "LLM_PROVIDER=google\n"
        "LLM_API_KEY=env-secret-key\n"
        "SOURCE_LANGUAGE=Spanish\n"
        "TARGET_LANGUAGE=German\n",
        encoding="utf-8",
    )
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        '{"app1": {"whisper_model": "small"}, "app2": {"llm_api_key": ""}}',
        encoding="utf-8",
    )

    mgr = SettingsManager(settings_path=settings_file, env_path=env_file)
    assert mgr.app1.watch_dirs == ["C:/Movies", "D:/Series"]
    assert mgr.app1.whisper_model == "small"
    assert mgr.app1.audio_stream_index == 1
    assert mgr.app1.extract_embedded_subtitles is False
    assert mgr.app1.auto_translate_after_extract is True
    assert mgr.app2.source_language == "Spanish"
    assert mgr.app2.target_language == "German"
    assert mgr.app2.llm_api_key == "env-secret-key"

    mgr.app2.target_language = "Spanish"
    mgr.save()
    mgr2 = SettingsManager(settings_path=settings_file, env_path=env_file)
    assert mgr2.app2.target_language == "Spanish"


def test_logger_creates_console_and_file_handlers_without_duplicates(tmp_path: Path) -> None:
    logger_name = "test.plex_subtitler.unique_logger"
    existing = logging.getLogger(logger_name)
    existing.handlers.clear()

    lgr = get_logger(logger_name, level="DEBUG", log_dir=tmp_path)
    assert len(lgr.handlers) == 2
    lgr.info("Teszt napló üzenet")
    # Második hívás nem duplikálja a handlereket
    lgr2 = get_logger(logger_name, level="DEBUG", log_dir=tmp_path)
    assert len(lgr2.handlers) == 2
    assert (tmp_path / "unique_logger.log").exists()
    for h in list(lgr.handlers):
        h.close()
    lgr.handlers.clear()


def test_hardware_manager_cpu_and_cuda_profiles() -> None:
    # 1) Ha nincs torch (vagy CUDA nem elérhető) -> CPU profil
    with patch.dict(sys.modules, {"torch": None}):
        hw = HardwareManager()
        assert hw.has_cuda is False
        assert hw.get_recommended_profile() == {
            "whisper_device": "cpu",
            "whisper_model": "small",
            "whisper_compute_type": "int8",
        }

    # 2) Mockolt CUDA 12 GB VRAM -> large-v3 / float16
    fake_torch_high = SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: True,
            get_device_name=lambda _idx: "NVIDIA RTX 4080",
            get_device_properties=lambda _idx: SimpleNamespace(total_memory=12 * (1024**3)),
        )
    )
    with patch.dict(sys.modules, {"torch": fake_torch_high}):
        hw = HardwareManager()
        assert hw.has_cuda is True
        assert hw.get_recommended_profile() == {
            "whisper_device": "cuda",
            "whisper_model": "large-v3",
            "whisper_compute_type": "float16",
        }

    # 3) Mockolt CUDA 6 GB VRAM -> medium / float16
    fake_torch_mid = SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: True,
            get_device_name=lambda _idx: "NVIDIA RTX 3060",
            get_device_properties=lambda _idx: SimpleNamespace(total_memory=6 * (1024**3)),
        )
    )
    with patch.dict(sys.modules, {"torch": fake_torch_mid}):
        hw = HardwareManager()
        assert hw.get_recommended_profile()["whisper_model"] == "medium"

    # 4) apply_auto_fallback frissíti a configot
    orig = (app1_config.whisper_device, app1_config.whisper_model, app1_config.whisper_compute_type)
    try:
        with patch.dict(sys.modules, {"torch": fake_torch_high}):
            HardwareManager().apply_auto_fallback(force_save=False)
            assert app1_config.whisper_model == "large-v3"
            assert app1_config.whisper_device == "cuda"
    finally:
        app1_config.whisper_device, app1_config.whisper_model, app1_config.whisper_compute_type = orig
