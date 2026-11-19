"""Tesztek: Auto-Align szinkronjavítás, Plex könyvtár-frissítés, barátságos hibakezelés és Egyszerű/Haladó GUI."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import imageio_ffmpeg

from plex_subtitler.app2_translator import plex_client
from plex_subtitler.app2_translator.sync_verifier import auto_align_subtitle, verify_subtitle_sync
from plex_subtitler.config import app2_config, settings_manager
from plex_subtitler.error_helper import build_diagnostic_clipboard_report, explain_error


def _build_audio_test_mkv(target_mkv: Path) -> Path:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    filter_complex = (
        "aevalsrc=0:d=1[s1];"
        "sine=frequency=440:duration=1.2[tone];"
        "aevalsrc=0:d=0.8[s2];"
        "[s1][tone][s2]concat=n=3:v=0:a=1[aout]"
    )
    res = subprocess.run(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "color=c=black:s=160x120:d=3",
            "-filter_complex", filter_complex,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "libx264", "-c:a", "aac",
            str(target_mkv),
        ],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stderr
    return target_mkv


def test_auto_align_fixes_uniform_drift_and_skips_synced_srt(tmp_path: Path) -> None:
    mkv = _build_audio_test_mkv(tmp_path / "align_test.mkv")

    # 1. Elcsúszott SRT (+1.2s a hanghoz képest, csendbe esik: 2.2s - 2.9s)
    drifted_srt = tmp_path / "drifted.hu.srt"
    drifted_srt.write_text(
        "1\n00:00:02,200 --> 00:00:02,900\nElcsúszott szöveg\n",
        encoding="utf-8",
    )
    before = verify_subtitle_sync(mkv, drifted_srt)
    assert before.in_sync is False

    applied_ms, out_path, after = auto_align_subtitle(
        mkv, drifted_srt, max_shift_ms=2000, step_ms=250
    )
    assert applied_ms < 0
    assert out_path == drifted_srt
    assert after.in_sync is True
    assert after.speech_alignment_ratio is not None and after.speech_alignment_ratio >= 0.9

    # 2. Már szinkronban lévő SRT-hez ne nyúljon hozzá
    synced_srt = tmp_path / "synced.hu.srt"
    synced_srt.write_text(
        "1\n00:00:01,100 --> 00:00:02,000\nPontos szöveg\n",
        encoding="utf-8",
    )
    applied_zero, _, report_zero = auto_align_subtitle(
        mkv, synced_srt, max_shift_ms=2000, step_ms=250
    )
    assert applied_zero == 0
    assert report_zero.in_sync is True


def test_plex_client_matches_section_and_triggers_refresh(monkeypatch, tmp_path: Path) -> None:
    called_urls: list[tuple[str, dict]] = []

    def _fake_get(url: str, headers: dict, timeout: float = 8.0):
        called_urls.append((url, headers))
        if url.endswith("/library/sections"):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "MediaContainer": {
                        "Directory": [
                            {
                                "key": "1",
                                "title": "Filmek",
                                "type": "movie",
                                "Location": [{"path": str(tmp_path / "Movies")}],
                            },
                            {
                                "key": "2",
                                "title": "Sorozatok",
                                "type": "show",
                                "Location": [{"path": str(tmp_path / "Series")}],
                            },
                        ]
                    }
                },
            )
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(plex_client.requests, "get", _fake_get)
    ep_srt = tmp_path / "Series" / "S01" / "ep01.hu.srt"
    ep_srt.parent.mkdir(parents=True, exist_ok=True)
    ep_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nSzia!\n", encoding="utf-8")

    ok, msg = plex_client.refresh_plex_library_for_file(
        ep_srt,
        plex_url="http://192.168.1.50:32400/",
        plex_token="MY_SECRET_PLEX_TOKEN",
    )
    assert ok is True
    assert "Sorozatok" in msg
    assert len(called_urls) == 2
    assert "/library/sections/2/refresh?path=" in called_urls[1][0]
    assert called_urls[1][1]["X-Plex-Token"] == "MY_SECRET_PLEX_TOKEN"

    ok_conn, conn_msg = plex_client.test_plex_connection("http://192.168.1.50:32400", "MY_SECRET_PLEX_TOKEN")
    assert ok_conn is True and "Filmek" in conn_msg and "Sorozatok" in conn_msg


def test_friendly_error_explanations_and_safe_diagnostic_report() -> None:
    err_ollama = explain_error(ConnectionError("HTTPConnectionPool localhost:11434 Connection refused"))
    assert "Ollama" in err_ollama.title
    assert "Start menü" in err_ollama.action_hu

    err_cuda = explain_error(RuntimeError("CUDA out of memory while allocating float16 tensor"))
    assert "VRAM" in err_cuda.title

    err_key = explain_error("401 Unauthorized: invalid API key provided")
    assert "API kulcs" in err_key.title

    err_quota = explain_error("429 ResourceExhausted: Quota exceeded")
    assert "korlát" in err_quota.title.lower()

    orig_key = app2_config.llm_api_key
    try:
        app2_config.llm_api_key = "SUPER_SECRET_GEMINI_KEY_12345"
        report = build_diagnostic_clipboard_report("Test hibaüzenet")
        assert "SUPER_SECRET_GEMINI_KEY_12345" not in report
        assert "Test hibaüzenet" in report
    finally:
        app2_config.llm_api_key = orig_key


def test_gui_simple_and_advanced_view_switch_and_tutorials(gui_app, monkeypatch) -> None:
    from plex_subtitler.logger import get_log_file_path

    orig_mode = app2_config.ui_mode
    monkeypatch.setattr(settings_manager, "save", lambda: None)
    app = gui_app
    try:
        app._on_view_mode_toggle("Egyszerű nézet")
        app.update()
        log_file = get_log_file_path()
        assert log_file.exists() and log_file.stat().st_size > 0
        assert "Plex Subtitler GUI elindult" in app.log_box.get("0.0", "end")
        assert app.view_switch.get() == "Egyszerű nézet"
        assert bool(app.simple_frame.winfo_manager()) is True
        assert bool(app.tabview.winfo_manager()) is False

        app._on_view_mode_toggle("Haladó nézet")
        app.update_idletasks()
        assert app2_config.ui_mode == "advanced"
        assert bool(app.tabview.winfo_manager()) is True
        assert bool(app.simple_frame.winfo_manager()) is False

        app._on_tutorial_tab_changed("Haladó Útmutató (GPU & Plex)")
        adv_content = app.help_textbox.get("0.0", "end")
        assert "X-Plex-Token" in adv_content
        assert "Auto-Align" in adv_content
    finally:
        app2_config.ui_mode = orig_mode


def test_gui_modular_exports_and_debounced_settings_save(gui_app, monkeypatch) -> None:
    from plex_subtitler.app1_extractor import gui, gui_dialogs, gui_quick, pipeline

    assert gui.run_one_click_pipeline is pipeline.run_one_click_pipeline
    assert gui.run_batch_one_click is pipeline.run_batch_one_click
    assert gui.QuickSubtitlerWindow is gui_quick.QuickSubtitlerWindow
    assert gui._parse_glossary_text is gui_dialogs._parse_glossary_text

    save_calls = {"count": 0}
    monkeypatch.setattr(settings_manager, "save", lambda: save_calls.__setitem__("count", save_calls["count"] + 1))

    app = gui_app
    # 5 gyors gépelési esemény ne írja ki azonnal 5-ször a settings.json-t
    for _ in range(5):
        app._on_setting_changed()
    assert save_calls["count"] == 0
    assert app._save_debounce_id is not None

    # Azonnali flush (vagy ablakbezárás) pontosan 1 mentést hajt végre
    app._flush_debounced_save()
    assert save_calls["count"] == 1
    assert app._save_debounce_id is None
