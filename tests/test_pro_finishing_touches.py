"""Tesztek a 4 befejező profi funkcióhoz: sáv-előnézet, Toast+hang, Minőség-ellenőrző és Játék-kímélő mód."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import imageio_ffmpeg

from plex_subtitler import notifier
from plex_subtitler.app1_extractor import audio_extractor, file_processor, hardware_manager
from plex_subtitler.app2_translator import translator_daemon
from plex_subtitler.app2_translator.srt_parser import SrtBlock
from plex_subtitler.app2_translator.sync_verifier import evaluate_subtitle_quality
from plex_subtitler.config import app1_config, app2_config


def _make_multitrack_mkv(target: Path) -> Path:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    sub_en = target.parent / "_en.srt"
    sub_es = target.parent / "_es.srt"
    sub_en.write_text("1\n00:00:00,000 --> 00:00:00,800\nEnglish line\n", encoding="utf-8")
    sub_es.write_text("1\n00:00:00,000 --> 00:00:00,800\nLinea en espanol\n", encoding="utf-8")

    res = subprocess.run(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "color=c=black:s=160x120:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=660:duration=1",
            "-i", str(sub_en),
            "-i", str(sub_es),
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:0", "-map", "4:0",
            "-c:v", "libx264", "-c:a", "aac", "-c:s", "srt",
            "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=Main Audio",
            "-metadata:s:a:1", "language=eng", "-metadata:s:a:1", "title=Director Commentary",
            "-metadata:s:s:0", "language=eng",
            "-metadata:s:s:1", "language=spa",
            str(target),
        ],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stderr
    return target


def test_stream_summary_and_explicit_stream_override(monkeypatch, tmp_path: Path) -> None:
    mkv = _make_multitrack_mkv(tmp_path / "multi_track.mkv")
    streams = audio_extractor.probe_streams(mkv)
    summary = audio_extractor.format_stream_summary(streams)

    assert len(summary["audio"]) == 2
    assert len(summary["subtitle"]) == 2
    assert "[#0]" in summary["audio"][0]["label"]
    assert "⚠️ (kommentár)" in summary["audio"][1]["label"]

    # Explicit feliratsáv #1 (spanyol) kinyerése
    out_es = audio_extractor.extract_embedded_subtitle(
        mkv, output_srt_path=tmp_path / "explicit_es.srt", stream_index=1
    )
    assert out_es is not None
    assert "Linea en espanol" in out_es.read_text(encoding="utf-8")

    # subtitle_stream_index=-1 esetén szándékosan kihagyja a beágyazott feliratot és Whispert hív
    whisper_called: list[Path] = []
    monkeypatch.setattr(file_processor, "transcribe_to_srt", lambda audio_path, **kw: (whisper_called.append(audio_path) or audio_path.with_suffix(".srt")))
    (tmp_path / "multi_track.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(
        file_processor,
        "extract_audio",
        lambda video_path, output_dir, **kw: tmp_path / "multi_track.wav",
    )

    orig_log = app1_config.processed_log
    try:
        app1_config.processed_log = str(tmp_path / "proc.log")
        res = file_processor.FileProcessor().process(
            mkv, subtitle_stream_index=-1, audio_stream_index=0
        )
        assert res is not None and len(whisper_called) == 1
    finally:
        app1_config.processed_log = orig_log


def test_subtitle_quality_checker_metrics() -> None:
    src = [
        SrtBlock(1, "00:00:01,000 --> 00:00:02,000", "Hello world today"),
        SrtBlock(2, "00:00:03,000 --> 00:00:04,000", "Darth Vader"),
        SrtBlock(3, "00:00:05,000 --> 00:00:06,000", "Short sentence here."),
    ]
    good = [
        SrtBlock(1, "00:00:01,000 --> 00:00:02,000", "Sziasztok, szép napot!"),
        SrtBlock(2, "00:00:03,000 --> 00:00:04,000", "Darth Vader"),
        SrtBlock(3, "00:00:05,000 --> 00:00:06,000", "Rövid magyar mondat."),
    ]
    q_good = evaluate_subtitle_quality(
        src, good, max_chars_per_line=42, speech_alignment_ratio=0.95, glossary={"Darth Vader": "Darth Vader"}
    )
    assert q_good.untranslated_count == 0
    assert q_good.over_limit_lines == 0
    assert q_good.score_pct == 100
    assert "95%" in q_good.summary_line

    bad = [
        SrtBlock(1, "00:00:01,000 --> 00:00:02,000", "Hello world today"),  # lefordítatlan!
        SrtBlock(2, "00:00:03,000 --> 00:00:04,000", "Darth Vader"),
        SrtBlock(3, "00:00:05,000 --> 00:00:06,000", "Ez egy rettentően hosszú mondat, ami bőven túllóg a negyvenkettő karakteren."),
    ]
    q_bad = evaluate_subtitle_quality(src, bad, max_chars_per_line=42, glossary={"Darth Vader": "Darth Vader"})
    assert q_bad.untranslated_count == 1
    assert q_bad.over_limit_lines == 1
    assert q_bad.max_line_chars > 42
    assert q_bad.score_pct < 85


def test_notifier_sound_and_toast(monkeypatch, tmp_path: Path) -> None:
    events: list[str] = []
    monkeypatch.setattr(notifier, "play_subtle_chime", lambda: (events.append("beep") or True))
    monkeypatch.setattr(notifier, "show_windows_toast", lambda title, msg: (events.append(f"{title}|{msg}") or True))

    orig_notif, orig_sound = app2_config.desktop_notifications, app2_config.notification_sound
    try:
        app2_config.desktop_notifications = True
        app2_config.notification_sound = True
        notifier.notify_translation_complete("Star Wars", tmp_path / "sw.hu.srt", quality_summary="📊 100%")
        # Várjuk meg a daemon toast szálat röviden
        import time

        for _ in range(20):
            if len(events) >= 2:
                break
            time.sleep(0.05)
        assert "beep" in events
        assert any("Star Wars" in e and "sw.hu.srt" in e for e in events)
    finally:
        app2_config.desktop_notifications = orig_notif
        app2_config.notification_sound = orig_sound


def test_gaming_pause_guard_and_translator_quality_toast(monkeypatch, tmp_path: Path) -> None:
    calls = {"n": 0}

    def _fake_game_active():
        calls["n"] += 1
        return (calls["n"] == 1, "Teljes képernyős 3D játék fut")

    orig_pause = app1_config.pause_when_gaming
    monkeypatch.setattr(hardware_manager, "is_fullscreen_game_active", _fake_game_active)
    try:
        app1_config.pause_when_gaming = True
        ev = threading.Event()
        hardware_manager.wait_while_gaming(stop_event=ev, poll_sec=0.05)
        assert calls["n"] == 2
    finally:
        app1_config.pause_when_gaming = orig_pause

    # Translator teljes lánc quality log + toast ellenőrzés
    notified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        translator_daemon,
        "notify_translation_complete",
        lambda title, path, quality_summary="": notified.append((title, quality_summary)),
    )
    monkeypatch.setattr(
        translator_daemon,
        "get_llm_client",
        lambda: SimpleNamespace(
            set_movie_context=lambda _c: None,
            set_recent_context=lambda _c: None,
            analyze_movie_context=lambda _t, _e: "Akciófilm",
            translate_chunk=lambda _txt: "[1] Fordított sor.",
        ),
    )

    srt = tmp_path / "demo_movie.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,500\nEnglish source line.\n", encoding="utf-8")

    orig_ctx, orig_lang = app2_config.auto_movie_context, app2_config.target_language
    try:
        app2_config.auto_movie_context = False
        app2_config.target_language = "Hungarian"
        out = translator_daemon.process_single_srt(srt, resume=False)
        assert out is not None and out.exists()
        assert len(notified) == 1
        assert "100%" in notified[0][1]
    finally:
        app2_config.auto_movie_context = orig_ctx
        app2_config.target_language = orig_lang
