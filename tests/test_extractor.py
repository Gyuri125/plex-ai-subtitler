"""Tesztek az App 1 (Extractor, FFmpeg, Whisper transcriber, FileProcessor, Watcher) moduljaihoz."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import imageio_ffmpeg
import pytest

from plex_subtitler.app1_extractor import audio_extractor, transcriber
from plex_subtitler.app1_extractor.file_processor import FileProcessor, _resolve_writable_path
from plex_subtitler.app1_extractor.watcher import VideoEventHandler, WatcherDaemon
from plex_subtitler.config import app1_config


def _create_synthetic_mp4(target: Path, with_subtitle: bool = True) -> Path:
    """Valódi szintetikus MP4-et generál hangsávval és opcionálisan beágyazott felirattal."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=160x120:d=1",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
    ]
    if with_subtitle:
        sub = target.parent / "_src_sub.srt"
        sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nEmbedded hello line\n", encoding="utf-8")
        cmd.extend(["-i", str(sub), "-c:s", "mov_text", "-metadata:s:s:0", "language=eng"])
    cmd.extend(["-c:v", "libx264", "-c:a", "aac", str(target)])
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    return target


def test_real_ffmpeg_probe_embedded_subtitle_and_audio_extraction(tmp_path: Path) -> None:
    mp4 = _create_synthetic_mp4(tmp_path / "sample.mp4", with_subtitle=True)

    streams = audio_extractor.probe_streams(mp4)
    assert len(streams["audio"]) == 1
    assert len(streams["subtitle"]) == 1
    assert streams["subtitle"][0]["codec_name"] == "mov_text"

    # 1) Beágyazott felirat valódi kinyerése FFmpeg-gel
    srt_out = audio_extractor.extract_embedded_subtitle(mp4, output_srt_path=tmp_path / "sample.srt")
    assert srt_out is not None and srt_out.exists()
    assert "Embedded hello line" in srt_out.read_text(encoding="utf-8")

    # 2) Valódi hangsáv kinyerése 16 kHz WAV-ba + takarítás
    audio_dir = tmp_path / "temp_audio"
    wav_path = audio_extractor.extract_audio(mp4, audio_dir)
    assert wav_path.exists()
    assert wav_path.suffix == ".wav"
    assert wav_path.stat().st_size > 1000

    audio_extractor.cleanup_audio(wav_path)
    assert not wav_path.exists()


def test_embedded_subtitle_selects_preferred_language_stream(tmp_path: Path) -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    sub_en = tmp_path / "_en.srt"
    sub_es = tmp_path / "_es.srt"
    sub_en.write_text("1\n00:00:00,000 --> 00:00:00,900\nEnglish text\n", encoding="utf-8")
    sub_es.write_text("1\n00:00:00,000 --> 00:00:00,900\nTexto español\n", encoding="utf-8")
    mp4 = tmp_path / "dual_sub.mp4"
    res = subprocess.run(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "color=c=black:s=160x120:d=1",
            "-i", str(sub_en), "-i", str(sub_es),
            "-map", "0:v", "-map", "1:0", "-map", "2:0",
            "-c:v", "libx264", "-c:s", "mov_text",
            "-metadata:s:s:0", "language=eng",
            "-metadata:s:s:1", "language=spa",
            str(mp4),
        ],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stderr

    orig_pref = app1_config.preferred_subtitle_language
    try:
        app1_config.preferred_subtitle_language = "spa"
        out_es = audio_extractor.extract_embedded_subtitle(mp4, output_srt_path=tmp_path / "out_es.srt")
        assert out_es is not None
        assert "Texto español" in out_es.read_text(encoding="utf-8")

        app1_config.preferred_subtitle_language = "eng"
        out_en = audio_extractor.extract_embedded_subtitle(mp4, output_srt_path=tmp_path / "out_en.srt")
        assert out_en is not None
        assert "English text" in out_en.read_text(encoding="utf-8")
    finally:
        app1_config.preferred_subtitle_language = orig_pref


def test_ffmpeg_command_uses_configured_path_and_mp3_option(tmp_path: Path) -> None:
    fake_ffmpeg = tmp_path / "ffmpeg.exe"
    fake_ffmpeg.write_text("bin", encoding="utf-8")
    orig = (app1_config.ffmpeg_path, app1_config.audio_format, app1_config.audio_stream_index)
    app1_config.ffmpeg_path = str(fake_ffmpeg)
    app1_config.audio_format = "mp3"
    app1_config.audio_stream_index = 2
    try:
        cmd = audio_extractor._build_ffmpeg_command(
            tmp_path / "video.mkv", tmp_path / "out.mp3", audio_stream_index=2
        )
        assert cmd[0] == str(fake_ffmpeg)
        assert "0:a:2" in cmd
        assert "libmp3lame" in cmd
    finally:
        app1_config.ffmpeg_path, app1_config.audio_format, app1_config.audio_stream_index = orig


def test_transcriber_model_cache_progress_and_stop_event(tmp_path: Path) -> None:
    transcriber._MODEL_CACHE.clear()
    init_calls: list[tuple] = []

    class _FakeWhisperModel:
        def __init__(self, model_name: str, device: str, compute_type: str) -> None:
            init_calls.append((model_name, device, compute_type))

        def transcribe(self, *_args, **_kwargs):
            segs = [
                SimpleNamespace(start=0.0, end=5.0, text=" First line "),
                SimpleNamespace(start=5.0, end=10.0, text=" Second line "),
            ]
            info = SimpleNamespace(language="en", language_probability=0.98, duration=10.0)
            return iter(segs), info

    fake_fw = SimpleNamespace(WhisperModel=_FakeWhisperModel)
    audio_file = tmp_path / "demo.wav"
    audio_file.write_bytes(b"RIFF")

    progress_updates: list[float] = []
    with patch.dict(sys.modules, {"faster_whisper": fake_fw}):
        srt1 = transcriber.transcribe_to_srt(
            audio_file,
            output_dir=tmp_path,
            progress_callback=lambda pct, _el, _eta: progress_updates.append(pct),
        )
        srt2 = transcriber.transcribe_to_srt(audio_file, output_dir=tmp_path)

    assert srt1 == tmp_path / "demo.srt"
    assert srt2.exists()
    # A második hívás a cache-ből használta a modellt (csak 1x példányosított)
    assert len(init_calls) == 1
    assert progress_updates == [0.5, 1.0]
    content = srt1.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:05,000" in content
    assert "Second line" in content

    # Megszakítás stop_event segítségével
    stop_ev = threading.Event()
    stop_ev.set()
    with patch.dict(sys.modules, {"faster_whisper": fake_fw}):
        with pytest.raises(transcriber.TranscriptionError):
            transcriber.transcribe_to_srt(audio_file, output_dir=tmp_path, stop_event=stop_ev)


def test_resolve_writable_path_falls_back_on_permission_error(tmp_path: Path) -> None:
    fallback = tmp_path / "fallback" / "processed.log"
    orig_mkdir = Path.mkdir

    def fake_mkdir(self: Path, *args, **kwargs) -> None:
        if "blocked_dir" in str(self):
            raise PermissionError("Access denied")
        return orig_mkdir(self, *args, **kwargs)

    with patch.object(Path, "mkdir", new=fake_mkdir):
        resolved = _resolve_writable_path(r"C:\blocked_dir\processed.log", fallback, is_dir=False)
    assert resolved == fallback
    assert fallback.parent.exists()


def test_file_processor_pipeline_and_deduplication(tmp_path: Path) -> None:
    orig = (
        app1_config.processed_log,
        app1_config.temp_audio_dir,
        app1_config.extract_embedded_subtitles,
        app1_config.cleanup_audio,
    )
    app1_config.processed_log = str(tmp_path / "processed.log")
    app1_config.temp_audio_dir = str(tmp_path / "temp_audio")
    app1_config.extract_embedded_subtitles = False
    app1_config.cleanup_audio = True
    try:
        # Nem támogatott kiterjesztés
        txt = tmp_path / "notes.txt"
        txt.write_text("hi", encoding="utf-8")
        assert FileProcessor().process(txt) is None

        # Támogatott videó -> audio + Whisper fallback -> cleanup -> on_srt_ready
        mp4 = _create_synthetic_mp4(tmp_path / "clip.mp4", with_subtitle=False)
        ready_list: list[Path] = []

        def fake_transcribe(audio_path: Path, output_dir: Path | None = None, **_kw) -> Path:
            out = (output_dir or audio_path.parent) / f"{audio_path.stem}.srt"
            out.write_text("1\n00:00:00,000 --> 00:00:01,000\nTranscribed\n", encoding="utf-8")
            return out

        with patch("plex_subtitler.app1_extractor.file_processor.transcribe_to_srt", side_effect=fake_transcribe):
            res = FileProcessor().process(mp4, on_srt_ready=ready_list.append)
        assert res == tmp_path / "clip.srt"
        assert ready_list == [tmp_path / "clip.srt"]
        assert not (tmp_path / "temp_audio" / "clip.wav").exists()

        # Második futtatásnál már szerepel a processed.log-ban -> kihagyva
        assert FileProcessor().process(mp4) is None
    finally:
        (
            app1_config.processed_log,
            app1_config.temp_audio_dir,
            app1_config.extract_embedded_subtitles,
            app1_config.cleanup_audio,
        ) = orig


def test_watcher_scan_existing_and_invalid_dir_handling(tmp_path: Path) -> None:
    orig = (app1_config.watch_dirs, app1_config.processed_log, app1_config.temp_audio_dir)
    app1_config.processed_log = str(tmp_path / "processed.log")
    app1_config.temp_audio_dir = str(tmp_path / "temp")
    try:
        # 1) Érvénytelen mappa -> on_error hívódik, nincs sys.exit()
        app1_config.watch_dirs = [str(tmp_path / "missing")]
        errors: list[str] = []
        daemon = WatcherDaemon(on_error=errors.append)
        daemon.start()
        assert len(errors) == 1

        # 2) scan_existing csak a támogatott videófájlokat teszi sorba
        media = tmp_path / "media"
        media.mkdir()
        (media / "a.mkv").write_bytes(b"1")
        (media / "b.mov").write_bytes(b"2")
        (media / "ignore.txt").write_text("no", encoding="utf-8")
        app1_config.watch_dirs = [str(media)]
        daemon2 = WatcherDaemon()
        daemon2.scan_existing()
        queued = []
        while not daemon2._queue.empty():
            queued.append(daemon2._queue.get()[2].name)
        assert sorted(queued) == ["a.mkv", "b.mov"]

        # 3) VideoEventHandler csak támogatott fájlt enged sorba
        import itertools
        from queue import PriorityQueue

        q: PriorityQueue[tuple[int, int, Path]] = PriorityQueue()
        h = VideoEventHandler(q, itertools.count())
        h._enqueue(media / "c.ts")
        h._enqueue(media / "d.jpg")
        assert q.qsize() == 1
        assert q.get()[2].name == "c.ts"
    finally:
        app1_config.watch_dirs, app1_config.processed_log, app1_config.temp_audio_dir = orig
