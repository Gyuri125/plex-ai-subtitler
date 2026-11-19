"""End-to-end és CLI tesztek (main.py --once, --translate-srt, teljes videó->SRT->fordítás lánc)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import imageio_ffmpeg
import pytest

import main as app_main
from plex_subtitler.app2_translator.llm_client import LLMClientBase
from plex_subtitler.config import app1_config, app2_config


def test_e2e_cli_once_extracts_embedded_srt_and_auto_translates(tmp_path: Path) -> None:
    sub = tmp_path / "_embedded.srt"
    sub.write_text(
        "1\n00:00:00,000 --> 00:00:00,900\nGood morning, friend!\n",
        encoding="utf-8",
    )
    video = tmp_path / "movie.mp4"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    res = subprocess.run(
        [
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
            "-i",
            str(sub),
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-c:s",
            "mov_text",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr

    orig1 = (
        app1_config.processed_log,
        app1_config.temp_audio_dir,
        app1_config.extract_embedded_subtitles,
        app1_config.auto_translate_after_extract,
    )
    orig2 = (app2_config.target_language, app2_config.api_request_delay)

    app1_config.processed_log = str(tmp_path / "processed.log")
    app1_config.temp_audio_dir = str(tmp_path / "temp_audio")
    app1_config.extract_embedded_subtitles = True
    app1_config.auto_translate_after_extract = True
    app2_config.target_language = "Hungarian"
    app2_config.api_request_delay = 0.0

    class _MockHungarianLLM(LLMClientBase):
        def translate_chunk(self, chunk_text: str) -> str:
            return "[1] Jó reggelt, barátom!"

    try:
        with (
            patch.object(sys, "argv", ["main.py", "--once", str(video)]),
            patch(
                "plex_subtitler.app2_translator.translator_daemon.get_llm_client",
                return_value=_MockHungarianLLM(),
            ),
        ):
            app_main.main()

        raw_srt = tmp_path / "movie.srt"
        hu_srt = tmp_path / "movie.hu.srt"
        assert raw_srt.exists()
        assert hu_srt.exists()
        assert "Good morning, friend!" in raw_srt.read_text(encoding="utf-8")
        hu_text = hu_srt.read_text(encoding="utf-8")
        assert "00:00:00,000 --> 00:00:00,900" in hu_text
        assert "Jó reggelt, barátom!" in hu_text
    finally:
        (
            app1_config.processed_log,
            app1_config.temp_audio_dir,
            app1_config.extract_embedded_subtitles,
            app1_config.auto_translate_after_extract,
        ) = orig1
        app2_config.target_language, app2_config.api_request_delay = orig2


def test_cli_translate_srt_and_missing_file_exit(tmp_path: Path) -> None:
    src = tmp_path / "clip.srt"
    src.write_text("1\n00:00:01,000 --> 00:00:02,000\nTest line\n", encoding="utf-8")

    orig_lang = app2_config.target_language
    orig_delay = app2_config.api_request_delay
    app2_config.target_language = "German"
    app2_config.api_request_delay = 0.0

    class _MockGermanLLM(LLMClientBase):
        def translate_chunk(self, _chunk_text: str) -> str:
            return "[1] Testzeile"

    try:
        with (
            patch.object(sys, "argv", ["main.py", "--translate-srt", str(src)]),
            patch(
                "plex_subtitler.app2_translator.translator_daemon.get_llm_client",
                return_value=_MockGermanLLM(),
            ),
        ):
            app_main.main()
        assert (tmp_path / "clip.de.srt").exists()

        with patch.object(sys, "argv", ["main.py", "--once", str(tmp_path / "missing.mp4")]):
            with pytest.raises(SystemExit) as exc:
                app_main.main()
            assert exc.value.code == 1
    finally:
        app2_config.target_language = orig_lang
        app2_config.api_request_delay = orig_delay
