"""Tesztek az App 2 (SRT parser, LLM kliens, Translator daemon) moduljaihoz."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from plex_subtitler.app2_translator.llm_client import (
    GeminiClient,
    LLMClientBase,
    OpenAIClient,
    _is_text_translation_model,
    _normalize_openai_base_url,
    get_available_models,
    get_llm_client,
    translate_blocks_with_retry,
)
from plex_subtitler.app2_translator.srt_parser import SrtBlock, SrtParser
from plex_subtitler.app2_translator.translator_daemon import (
    TranslatorDaemon,
    get_target_srt_path,
    is_already_translated_filename,
    is_translation_complete,
    process_single_srt,
)
from plex_subtitler.config import app1_config, app2_config


def test_srt_parser_handles_bom_crlf_invalid_blocks_and_multiline_llm(tmp_path: Path) -> None:
    srt_file = tmp_path / "sample.srt"
    raw = (
        "\ufeff1\r\n"
        "00:00:01,000 --> 00:00:03,000\r\n"
        "Hello world!\r\n"
        "Second line.\r\n\r\n"
        "BROKEN_BLOCK\r\n"
        "not_a_timestamp\r\n\r\n"
        "2\r\n"
        "00:00:04,000 --> 00:00:06,000\r\n"
        "How are you?\r\n"
    )
    srt_file.write_bytes(raw.encode("utf-8"))

    parser = SrtParser(srt_file)
    assert len(parser.blocks) == 2
    assert parser.blocks[0].index == 1
    assert parser.blocks[0].text == "Hello world!\nSecond line."

    chunks = list(parser.get_chunks(batch_size=1))
    assert len(chunks) == 2

    compact = SrtParser.blocks_to_text_format(parser.blocks)
    assert "[1] Hello world! \\n Second line." in compact

    llm_output = "[1] Szia világ!\nMásodik sor.\n[2] Hogy vagy?"
    updated = SrtParser.apply_translation_to_blocks(parser.blocks, llm_output)
    assert updated[0].timestamp == "00:00:01,000 --> 00:00:03,000"
    assert updated[0].text == "Szia világ!\nMásodik sor."
    assert updated[1].text == "Hogy vagy?"

    saved = tmp_path / "out.srt"
    SrtParser.save_srt(updated, saved)
    assert SrtParser(saved).blocks[0].text == "Szia világ!\nMásodik sor."


def test_srt_parser_missing_id_keeps_original() -> None:
    blocks = [
        SrtBlock(1, "00:00:01,000 --> 00:00:02,000", "Original 1"),
        SrtBlock(2, "00:00:02,500 --> 00:00:04,000", "Original 2"),
    ]
    updated = SrtParser.apply_translation_to_blocks(blocks, "[1] Fordított 1")
    assert updated[0].text == "Fordított 1"
    assert updated[1].text == "Original 2"


def test_llm_client_prompt_factory_retry_and_models() -> None:
    orig = (
        app2_config.llm_provider,
        app2_config.llm_api_key,
        app2_config.llm_base_url,
        app2_config.source_language,
        app2_config.target_language,
        app2_config.api_request_delay,
    )
    try:
        app2_config.source_language = "Spanish"
        app2_config.target_language = "Hungarian"
        app2_config.api_request_delay = 0.0

        class _TestClient(LLMClientBase):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def translate_chunk(self, chunk_text: str) -> str:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("429 Too Many Requests, try again in 0s")
                return chunk_text.replace("Hola", "Szia")

        c = _TestClient()
        assert "from Spanish into Hungarian" in c.system_prompt

        with patch("plex_subtitler.app2_translator.llm_client.time.sleep"):
            res = translate_blocks_with_retry(c, "[1] Hola", retries=3)
        assert res == "[1] Szia"
        assert c.calls == 2

        # Stop event azonnali kilépést okoz
        ev = threading.Event()
        ev.set()
        assert translate_blocks_with_retry(c, "[1] Hola", retries=3, stop_event=ev) == ""

        # Factory + Gemini üres kulcs hiba
        app2_config.llm_provider = "google"
        app2_config.llm_api_key = ""
        with pytest.raises(ValueError):
            GeminiClient()

        app2_config.llm_provider = "local"
        app2_config.llm_base_url = "http://localhost:11434"
        client = get_llm_client()
        assert isinstance(client, OpenAIClient)

        assert _normalize_openai_base_url(None, "local") == "http://localhost:11434/v1"
        assert _normalize_openai_base_url("http://localhost:11434", "local") == "http://localhost:11434/v1"
        assert _normalize_openai_base_url(None, "openai") is None

        # Ollama modell lekérés mockolt requests-szel
        fake_resp = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"models": [{"name": "llama3"}, {"name": "mistral"}]},
        )
        with patch("requests.get", return_value=fake_resp):
            assert get_available_models("local", local_url="http://localhost:11434/v1") == ["llama3", "mistral"]

        assert _is_text_translation_model("gemini-2.5-flash") is True
        assert _is_text_translation_model("gemini-2.5-flash-image") is False
        assert _is_text_translation_model("gemini-2.5-flash-preview-tts") is False
        assert _is_text_translation_model("gemini-3.5-transcribe") is False
        assert _is_text_translation_model("gemini-2.5-computer-use-preview-10-2025") is False
    finally:
        (
            app2_config.llm_provider,
            app2_config.llm_api_key,
            app2_config.llm_base_url,
            app2_config.source_language,
            app2_config.target_language,
            app2_config.api_request_delay,
        ) = orig


def test_translator_daemon_dynamic_lang_partial_save_and_scan(tmp_path: Path) -> None:
    src = tmp_path / "episode.srt"
    src.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nFirst line\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nSecond line\n",
        encoding="utf-8",
    )

    assert get_target_srt_path(src, "German") == tmp_path / "episode.de.srt"
    assert get_target_srt_path(src, "Spanish") == tmp_path / "episode.es.srt"
    assert is_already_translated_filename(tmp_path / "episode.de.srt")
    assert is_already_translated_filename(tmp_path / "episode.hu.srt")
    assert not is_already_translated_filename(src)

    orig_lang = app2_config.target_language
    orig_batch = app2_config.translation_batch_size
    orig_delay = app2_config.api_request_delay
    orig_dirs = app1_config.watch_dirs
    app2_config.target_language = "Spanish"
    app2_config.translation_batch_size = 1
    app2_config.api_request_delay = 0.0
    try:
        stop_ev = threading.Event()

        class _PartialLLM(LLMClientBase):
            def translate_chunk(self, chunk_text: str) -> str:
                if "[1]" in chunk_text:
                    stop_ev.set()
                    return "[1] Primera línea"
                return "[2] Segunda línea"

        progress_log: list[tuple[int, int]] = []
        with patch(
            "plex_subtitler.app2_translator.translator_daemon.get_llm_client",
            return_value=_PartialLLM(),
        ):
            out = process_single_srt(
                src,
                progress_callback=lambda cur, tot, _el, _eta: progress_log.append((cur, tot)),
                stop_event=stop_ev,
            )

        assert out == tmp_path / "episode.es.srt"
        assert progress_log == [(1, 2)]
        assert "Primera línea" in out.read_text(encoding="utf-8")

        # scan_existing kihagyja episode.srt-t, ha teljes, de felveszi, mert itt részleges (1/2)!
        assert is_translation_complete(src, out) is False
        app1_config.watch_dirs = [str(tmp_path)]
        td = TranslatorDaemon()
        td.scan_existing()
        queued = []
        while not td._queue.empty():
            queued.append(td._queue.get().name)
        assert queued == ["episode.srt"]

        # Checkpoint / Resume teszt: folytatásnál csak a hiányzó [2] blokkot küldi az LLM-nek!
        sent_chunks: list[str] = []

        class _ResumeLLM(LLMClientBase):
            def translate_chunk(self, chunk_text: str) -> str:
                sent_chunks.append(chunk_text)
                return "[2] Segunda línea"

        resumed_progress: list[tuple[int, int]] = []
        with patch(
            "plex_subtitler.app2_translator.translator_daemon.get_llm_client",
            return_value=_ResumeLLM(),
        ):
            out2 = process_single_srt(
                src,
                progress_callback=lambda cur, tot, _el, _eta: resumed_progress.append((cur, tot)),
            )

        assert out2 == out
        assert len(sent_chunks) == 1
        assert "[1]" not in sent_chunks[0]
        assert "[2]" in sent_chunks[0]
        assert is_translation_complete(src, out) is True
        final_text = out.read_text(encoding="utf-8")
        assert "Primera línea" in final_text
        assert "Segunda línea" in final_text
        assert resumed_progress[-1] == (2, 2)

        # Teljes fordítás után scan_existing már nem teszi sorba
        td2 = TranslatorDaemon()
        td2.scan_existing()
        assert td2._queue.empty()
    finally:
        app2_config.target_language = orig_lang
        app2_config.translation_batch_size = orig_batch
        app2_config.api_request_delay = orig_delay
        app1_config.watch_dirs = orig_dirs
