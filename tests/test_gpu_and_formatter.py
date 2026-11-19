"""Tesztek az RTX 3080 VRAM-stafétához, hangsáv-választáshoz, SRT formázóhoz és 1-Click módhoz."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import imageio_ffmpeg

import doctor_gpu
from plex_subtitler.app1_extractor import audio_extractor, transcriber
from plex_subtitler.app1_extractor.gui import run_one_click_pipeline
from plex_subtitler.app2_translator.llm_client import (
    LLMClientBase,
    ensure_ollama_model_pulled,
    ensure_ollama_running,
)
from plex_subtitler.logger import get_log_file_path, get_logger
from plex_subtitler.app2_translator.srt_formatter import (
    fix_overlapping_timestamps,
    format_srt_blocks,
    wrap_subtitle_text,
)
from plex_subtitler.app2_translator.srt_parser import SrtBlock, SrtParser
from plex_subtitler.app2_translator.translator_daemon import process_single_srt
from plex_subtitler.config import app1_config, app2_config


def test_tv_srt_wrapper_and_overlap_fixer() -> None:
    long_line = "Ez egy nagyon hosszú magyar mondat, amely biztosan nem fér ki egyetlen tévés sorba."
    wrapped = wrap_subtitle_text(long_line, max_chars=42, max_lines=2)
    lines = wrapped.splitlines()
    assert len(lines) == 2
    assert all(len(l) <= 42 for l in lines)

    # Párbeszédes kötőjel megmarad két külön sorban
    dialogue = "- Merre tartasz?\n- Csak haza."
    assert wrap_subtitle_text(dialogue, max_chars=42, max_lines=2) == dialogue

    # Átfedő időbélyeg javítása
    blocks = [
        SrtBlock(1, "00:00:01,000 --> 00:00:05,000", "Első mondat"),
        SrtBlock(2, "00:00:04,500 --> 00:00:07,000", "Második mondat"),
    ]
    fixed = fix_overlapping_timestamps(blocks, min_gap_ms=10)
    assert fixed[0].timestamp == "00:00:01,000 --> 00:00:04,490"
    assert fixed[1].timestamp == "00:00:04,500 --> 00:00:07,000"


def test_checkpoint_resume_works_even_with_timestamp_clamp(tmp_path: Path) -> None:
    src = tmp_path / "overlap.srt"
    src.write_text(
        "1\n00:00:01,000 --> 00:00:05,000\nLine one\n\n"
        "2\n00:00:04,500 --> 00:00:07,000\nLine two\n",
        encoding="utf-8",
    )
    orig = (
        app2_config.target_language,
        app2_config.translation_batch_size,
        app2_config.api_request_delay,
        app2_config.format_tv_subtitles,
    )
    app2_config.target_language = "Hungarian"
    app2_config.translation_batch_size = 1
    app2_config.api_request_delay = 0.0
    app2_config.format_tv_subtitles = True
    try:
        # 1. részleges futás (csak 1. blokk) -> módosított végű .hu.srt jön létre
        out = tmp_path / "overlap.hu.srt"
        out.write_text("1\n00:00:01,000 --> 00:00:04,490\nElső sor\n\n", encoding="utf-8")

        sent: list[str] = []

        class _ResumeLLM(LLMClientBase):
            def translate_chunk(self, chunk_text: str) -> str:
                sent.append(chunk_text)
                return "[2] Második sor"

        with patch(
            "plex_subtitler.app2_translator.translator_daemon.get_llm_client",
            return_value=_ResumeLLM(),
        ):
            process_single_srt(src, resume=True)

        assert len(sent) == 1
        assert "[1]" not in sent[0]
        assert "[2]" in sent[0]
        final_blocks = SrtParser(out).blocks
        assert [b.index for b in final_blocks] == [1, 2]
    finally:
        (
            app2_config.target_language,
            app2_config.translation_batch_size,
            app2_config.api_request_delay,
            app2_config.format_tv_subtitles,
        ) = orig


def test_audio_stream_selection_by_language_avoids_commentary() -> None:
    streams = [
        {"codec_type": "audio", "tags": {"language": "eng", "title": "Director Commentary"}, "disposition": {"comment": 1}},
        {"codec_type": "audio", "tags": {"language": "hun", "title": "Hungarian Dub"}},
        {"codec_type": "audio", "tags": {"language": "eng", "title": "Main English 5.1"}},
        {"codec_type": "audio", "tags": {"language": "spa", "title": "Spanish"}},
    ]
    assert audio_extractor.select_audio_stream_index(streams, preferred_language="eng") == 2
    assert audio_extractor.select_audio_stream_index(streams, preferred_language="spa") == 3
    assert audio_extractor.select_audio_stream_index(streams, preferred_language="hun") == 1


def test_transcriber_unload_model_clears_cache_and_cuda() -> None:
    transcriber._MODEL_CACHE[("large-v3", "cuda", "float16")] = object()
    empty_cache_mock = MagicMock()
    ipc_collect_mock = MagicMock()
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: True,
            empty_cache=empty_cache_mock,
            ipc_collect=ipc_collect_mock,
        )
    )
    with patch.dict(sys.modules, {"torch": fake_torch}):
        unloaded = transcriber.unload_model()

    assert unloaded is True
    assert len(transcriber._MODEL_CACHE) == 0
    empty_cache_mock.assert_called_once()
    ipc_collect_mock.assert_called_once()


def test_ensure_ollama_running_autostarts_when_offline() -> None:
    orig_auto = app2_config.auto_start_ollama
    app2_config.auto_start_ollama = True
    calls = {"n": 0}

    def fake_get(_url: str, timeout: float = 2):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("offline")
        return SimpleNamespace(status_code=200)

    try:
        with (
            patch("requests.get", side_effect=fake_get),
            patch("shutil.which", return_value="C:/Ollama/ollama.exe"),
            patch("subprocess.Popen") as mock_popen,
            patch("plex_subtitler.app2_translator.llm_client.time.sleep"),
        ):
            ok = ensure_ollama_running("http://localhost:11434/v1", timeout_sec=3.0)
        assert ok is True
        mock_popen.assert_called_once()
    finally:
        app2_config.auto_start_ollama = orig_auto


def test_one_click_pipeline_real_mp4_to_formatted_hu_srt(tmp_path: Path) -> None:
    sub = tmp_path / "_src.srt"
    sub.write_text(
        "1\n00:00:00,000 --> 00:00:00,900\nThis is a very long movie dialogue line that should wrap cleanly onto two TV lines.\n",
        encoding="utf-8",
    )
    mp4 = tmp_path / "sample_movie.mp4"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    res = subprocess.run(
        [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", "color=c=black:s=160x120:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-i", str(sub),
            "-c:v", "libx264", "-c:a", "aac", "-c:s", "mov_text",
            str(mp4),
        ],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stderr

    orig1 = (app1_config.processed_log, app1_config.temp_audio_dir, app1_config.extract_embedded_subtitles)
    orig2 = (app2_config.target_language, app2_config.api_request_delay, app2_config.format_tv_subtitles)
    app1_config.processed_log = str(tmp_path / "processed.log")
    app1_config.temp_audio_dir = str(tmp_path / "temp_audio")
    app1_config.extract_embedded_subtitles = True
    app2_config.target_language = "Hungarian"
    app2_config.api_request_delay = 0.0
    app2_config.format_tv_subtitles = True

    class _MockLocalLLM(LLMClientBase):
        def translate_chunk(self, _chunk_text: str) -> str:
            return "[1] Ez egy nagyon hosszú filmes párbeszéd, amely szépen két tévés sorba fog törni."

    statuses: list[str] = []
    progress_vals: list[float] = []
    try:
        with patch(
            "plex_subtitler.app2_translator.translator_daemon.get_llm_client",
            return_value=_MockLocalLLM(),
        ):
            out = run_one_click_pipeline(
                mp4,
                status_callback=statuses.append,
                progress_callback=progress_vals.append,
            )

        assert out == tmp_path / "sample_movie.hu.srt"
        assert out.exists()
        blocks = SrtParser(out).blocks
        assert len(blocks) == 1
        assert "\n" in blocks[0].text
        assert all(len(l) <= 42 for l in blocks[0].text.splitlines())
        assert progress_vals[-1] == 1.0
        assert any("Kész" in s for s in statuses)
    finally:
        app1_config.processed_log, app1_config.temp_audio_dir, app1_config.extract_embedded_subtitles = orig1
        app2_config.target_language, app2_config.api_request_delay, app2_config.format_tv_subtitles = orig2


def test_doctor_gpu_runs_cleanly() -> None:
    assert doctor_gpu.run_diagnostics(live_llm=False) == 0


def test_ensure_ollama_model_pulled_auto_pulls_when_missing() -> None:
    orig_pull = app2_config.auto_pull_ollama_model
    app2_config.auto_pull_ollama_model = True
    try:
        # 1) Ha már megvan a modell -> nem hív /api/pull-t
        tags_resp = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"models": [{"name": "gemma3:12b"}]},
        )
        with (
            patch("requests.get", return_value=tags_resp),
            patch("requests.post") as mock_post,
        ):
            assert ensure_ollama_model_pulled("http://localhost:11434/v1", "gemma3:12b") is True
            mock_post.assert_not_called()

        # 2) Ha hiányzik -> automatikusan lehúzza /api/pull végpontról
        empty_tags = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"models": []})

        class _FakePullStream:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode: bool = True):
                return iter([
                    '{"status":"downloading","total":100,"completed":50}',
                    '{"status":"success","total":100,"completed":100}',
                ])

        with (
            patch("requests.get", return_value=empty_tags),
            patch("requests.post", return_value=_FakePullStream()) as mock_post2,
        ):
            assert ensure_ollama_model_pulled("http://localhost:11434/v1", "gemma3:12b") is True
            mock_post2.assert_called_once()
    finally:
        app2_config.auto_pull_ollama_model = orig_pull


def test_auto_movie_context_rolling_prompt_and_logs(tmp_path: Path, caplog) -> None:
    src = tmp_path / "Matrix_1999.srt"
    src.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nWake up, Neo.\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nThe Matrix has you.\n\n"
        "3\n00:00:05,000 --> 00:00:06,000\nFollow the white rabbit.\n",
        encoding="utf-8",
    )
    orig = (
        app2_config.target_language,
        app2_config.translation_batch_size,
        app2_config.api_request_delay,
        app2_config.auto_movie_context,
    )
    app2_config.target_language = "Hungarian"
    app2_config.translation_batch_size = 2
    app2_config.api_request_delay = 0.0
    app2_config.auto_movie_context = True

    seen_prompts: list[str] = []

    class _ContextAwareLLM(LLMClientBase):
        def analyze_movie_context(self, movie_title: str, sample_text: str) -> str:
            assert "Matrix_1999" in movie_title
            return "- Sci-fi action thriller\n- Keep 'Neo' and 'Matrix' untranslated\n- Informal tone (tegezes)"

        def translate_chunk(self, chunk_text: str) -> str:
            seen_prompts.append(self.system_prompt)
            if "[1]" in chunk_text:
                return "[1] Ébredj, Neo.\n[2] A Matrix fogva tart."
            return "[3] Kövesd a fehér nyulat."

    try:
        with (
            patch("plex_subtitler.app2_translator.translator_daemon.get_llm_client", return_value=_ContextAwareLLM()),
            patch("plex_subtitler.app2_translator.translator_daemon.LOGS_DIR", tmp_path / "logs"),
        ):
            out = process_single_srt(src)

        assert out == tmp_path / "Matrix_1999.hu.srt"
        assert len(seen_prompts) == 2
        assert "Keep 'Neo' and 'Matrix' untranslated" in seen_prompts[0]
        # Második csomagban már benne van a gördülő előző párbeszéd-kontextus is
        assert "The Matrix has you." in seen_prompts[1]
        history_file = tmp_path / "logs" / "processed_movies.log"
        assert history_file.exists()
        assert "Matrix_1999" in history_file.read_text(encoding="utf-8")
        assert get_log_file_path(tmp_path / "logs") == tmp_path / "logs" / "plex_subtitler.log"
        lgr = get_logger("test.file.log.check", log_dir=tmp_path / "logs")
        lgr.info("Teszt napló bejegyzés")
        assert (tmp_path / "logs" / "check.log").exists()
        for h in list(lgr.handlers):
            h.close()
        lgr.handlers.clear()
    finally:
        (
            app2_config.target_language,
            app2_config.translation_batch_size,
            app2_config.api_request_delay,
            app2_config.auto_movie_context,
        ) = orig
