"""v1.2.0 tesztek: hibakódok, öngyógyító akciók, ZIP hibacsomag, állapotjelző és előzmények."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from plex_subtitler import __version__, error_helper
from plex_subtitler.app1_extractor import transcriber
from plex_subtitler.app2_translator import llm_client
from plex_subtitler.config import app1_config, app2_config, settings_manager


def test_error_codes_and_quick_fix_metadata() -> None:
    assert __version__ == "1.3.0"

    cases = [
        (ConnectionError("localhost:11434 connection refused"), "ERR-OLLAMA-01", "start_ollama"),
        ("404 model 'gemma3:12b' not found", "ERR-MODEL-02", "pull_ollama_model"),
        (RuntimeError("CUDA out of memory"), "ERR-VRAM-03", "free_vram_reduce_batch"),
        ("401 Unauthorized API key", "ERR-KEY-04", "switch_to_ollama"),
        ("429 Quota exceeded", "ERR-QUOTA-05", "switch_to_ollama"),
        (FileNotFoundError("no such file"), "ERR-FILE-06", None),
        (PermissionError("WinError 32 being used by another process"), "ERR-LOCK-07", None),
        ("OSError: No space left on device", "ERR-DISK-08", None),
        ("ffmpeg: Invalid data found when processing input", "ERR-MEDIA-09", None),
        ("ismeretlen ritka hiba", "ERR-GEN-99", None),
    ]
    for raw_err, expected_code, expected_fix in cases:
        fe = error_helper.explain_error(raw_err)
        assert fe.code == expected_code
        assert fe.quick_fix_id == expected_fix
        assert expected_code in fe.format_dialog_text()


def test_execute_quick_fix_vram_and_switch_to_ollama(monkeypatch) -> None:
    unloaded = {"whisper": 0, "ollama": 0}
    monkeypatch.setattr(transcriber, "unload_model", lambda: unloaded.__setitem__("whisper", unloaded["whisper"] + 1))
    monkeypatch.setattr(llm_client, "unload_ollama_model", lambda *_a, **_kw: unloaded.__setitem__("ollama", unloaded["ollama"] + 1))
    monkeypatch.setattr(llm_client, "ensure_ollama_running", lambda *_a, **_kw: True)
    monkeypatch.setattr(settings_manager, "save", lambda: None)

    orig_prov = app2_config.llm_provider
    orig_model = app2_config.llm_model
    orig_url = app2_config.llm_base_url
    orig_batch = app2_config.translation_batch_size
    try:
        app2_config.llm_provider = "local"
        app2_config.translation_batch_size = 30
        ok_vram, msg_vram = error_helper.execute_quick_fix("free_vram_reduce_batch")
        assert ok_vram is True
        assert unloaded["whisper"] == 1
        assert unloaded["ollama"] == 1
        assert app2_config.translation_batch_size == 8
        assert "8" in msg_vram

        app2_config.llm_provider = "google"
        app2_config.llm_model = "gemini-2.5-flash"
        ok_sw, msg_sw = error_helper.execute_quick_fix("switch_to_ollama")
        assert ok_sw is True
        assert app2_config.llm_provider == "local"
        assert app2_config.llm_model == "gemma3:12b"
        assert app2_config.translation_batch_size == 12
        assert "Ollama" in msg_sw
    finally:
        app2_config.llm_provider = orig_prov
        app2_config.llm_model = orig_model
        app2_config.llm_base_url = orig_url
        app2_config.translation_batch_size = orig_batch


def test_export_diagnostic_zip_redacts_all_secrets(tmp_path: Path) -> None:
    orig_llm_key = app2_config.llm_api_key
    orig_osub_key = app1_config.opensubtitles_api_key
    orig_plex_tok = app2_config.plex_token
    try:
        app2_config.llm_api_key = "SECRET_GEMINI_TOKEN_999"
        app1_config.opensubtitles_api_key = "SECRET_OSUB_TOKEN_888"
        app2_config.plex_token = "SECRET_PLEX_TOKEN_777"

        zp = error_helper.export_diagnostic_zip(output_dir=tmp_path, error_text="CUDA out of memory")
        assert zp.is_file()
        with zipfile.ZipFile(zp, "r") as zf:
            names = set(zf.namelist())
            assert "diagnostic_summary.txt" in names
            assert "settings_sanitized.json" in names

            summary_txt = zf.read("diagnostic_summary.txt").decode("utf-8")
            settings_txt = zf.read("settings_sanitized.json").decode("utf-8")
            for secret in ("SECRET_GEMINI_TOKEN_999", "SECRET_OSUB_TOKEN_888", "SECRET_PLEX_TOKEN_777"):
                assert secret not in summary_txt
                assert secret not in settings_txt
            assert "***REDACTED***" in settings_txt
            assert "ERR-VRAM-03" in summary_txt
    finally:
        app2_config.llm_api_key = orig_llm_key
        app1_config.opensubtitles_api_key = orig_osub_key
        app2_config.plex_token = orig_plex_tok


def test_check_system_health_and_load_history_entries(tmp_path: Path) -> None:
    items = error_helper.check_system_health()
    names = [i.name for i in items]
    assert names == ["gpu", "ai", "ffmpeg", "plex"]
    assert next(i for i in items if i.name == "ffmpeg").status == "ok"

    ep_partial = tmp_path / "S01E02.mkv"
    ep_partial.write_bytes(b"00")
    cache_file = tmp_path / "series_cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "episodes": {
                    str(ep_partial): {
                        "status": "partial",
                        "updated_at": 1700000000.0,
                        "translated_srt": "",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    movies_log = tmp_path / "processed_movies.log"
    movies_log.write_text(
        "2026-11-19 14:00:00 | KÉSZ (Minőség: 98%) | Film: Matrix | Cél: Matrix.hu.srt | Blokkok: 120 | Modell: local/gemma3:12b | Idő: 45.0 mp\n",
        encoding="utf-8",
    )

    entries = error_helper.load_history_entries(limit=10, cache_path=cache_file, movies_log_path=movies_log)
    assert len(entries) == 2
    assert entries[0].title == "S01E02"
    assert entries[0].status == "RÉSZLEGES"
    assert entries[0].file_path == ep_partial
    assert entries[1].title == "Matrix"
    assert entries[1].status == "KÉSZ"


def test_gui_health_bar_and_history_window(gui_app, monkeypatch) -> None:
    from plex_subtitler.app1_extractor.gui_dialogs import _open_history_window

    monkeypatch.setattr(settings_manager, "save", lambda: None)
    app = gui_app
    app.update()
    app.health_bar.apply_items(
        [
            error_helper.HealthCheckItem("gpu", "ok", "GPU: RTX 3080", "CUDA OK"),
            error_helper.HealthCheckItem("ai", "ok", "AI: Ollama", "gemma3:12b"),
            error_helper.HealthCheckItem("ffmpeg", "ok", "FFmpeg: OK", "ok"),
            error_helper.HealthCheckItem("plex", "muted", "Plex: Ki", "off"),
        ]
    )
    assert app.health_bar._pills["gpu"].cget("text") == "GPU: RTX 3080"
    _open_history_window(app)
    app.update_idletasks()
