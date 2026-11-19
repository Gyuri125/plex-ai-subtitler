"""Tesztek a sorozat-cache-hez, Drag&Drop alapokhoz, szinkron-ellenőrzőhöz, szószedethez és OpenSubtitles-hez."""

from __future__ import annotations

import itertools
import subprocess
import time
from pathlib import Path
from queue import PriorityQueue
from types import SimpleNamespace
from unittest.mock import patch

import imageio_ffmpeg

from plex_subtitler.app1_extractor import audio_extractor
from plex_subtitler.app1_extractor.gui import (
    _HAS_DND,
    _format_glossary_text,
    _parse_glossary_text,
    run_batch_one_click,
)
from plex_subtitler.app1_extractor.opensubtitles_client import (
    compute_opensubtitles_hash,
    download_opensubtitles_srt,
)
from plex_subtitler.app1_extractor.series_cache import SeriesCacheManager
from plex_subtitler.app1_extractor.watcher import VideoEventHandler
from plex_subtitler.app2_translator.llm_client import LLMClientBase
from plex_subtitler.app2_translator.srt_formatter import shift_srt_file, shift_srt_timestamps
from plex_subtitler.app2_translator.srt_parser import SrtBlock, SrtParser
from plex_subtitler.app2_translator.sync_verifier import verify_subtitle_sync
from plex_subtitler.config import APP_ICON_PATH, app1_config, app2_config


def _make_synced_mkv(target_mkv: Path) -> Path:
    """Valódi MKV videót készít (1s csend + 1.2s beszéd-hang + 0.8s csend) és 1.0-2.2s felirattal."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    sub = target_mkv.parent / "_sync_sub.srt"
    sub.write_text("1\n00:00:01,000 --> 00:00:02,200\nHello from synced MKV!\n", encoding="utf-8")

    # 3s audio: csend 0..1s, 440Hz hang 1.0..2.2s, csend 2.2..3.0s
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
            "-i", str(sub),
            "-map", "0:v", "-map", "[aout]", "-map", "1:0",
            "-c:v", "libx264", "-c:a", "aac", "-c:s", "srt",
            str(target_mkv),
        ],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, res.stderr
    return target_mkv


def test_mkv_sync_verification_passes_and_detects_drift(tmp_path: Path) -> None:
    mkv = _make_synced_mkv(tmp_path / "synced_movie.mkv")
    raw_srt = audio_extractor.extract_embedded_subtitle(mkv, output_srt_path=tmp_path / "synced_movie.srt")
    assert raw_srt is not None and raw_srt.exists()

    # Szinkronban van a valódi MKV hanggal és időtartammal
    ok_report = verify_subtitle_sync(mkv, raw_srt, source_srt_path=raw_srt)
    assert ok_report.in_sync is True
    assert ok_report.out_of_bounds_count == 0
    assert ok_report.overlap_count == 0
    assert ok_report.source_max_drift_ms == 0
    assert ok_report.speech_alignment_ratio == 1.0

    # Szándékos +2500 ms elcsúszás -> a szinkron-ellenőrző észreveszi!
    drifted_srt = tmp_path / "drifted.srt"
    shift_srt_file(raw_srt, 2500, output_path=drifted_srt)
    bad_report = verify_subtitle_sync(mkv, drifted_srt, source_srt_path=raw_srt)
    assert bad_report.in_sync is False
    assert bad_report.out_of_bounds_count == 1
    assert bad_report.source_max_drift_ms == 2500

    # Visszaállítás -2500 ms-sel -> újra szinkronban
    shift_srt_file(drifted_srt, -2500)
    fixed_report = verify_subtitle_sync(mkv, drifted_srt, source_srt_path=raw_srt)
    assert fixed_report.in_sync is True


def test_series_cache_skips_done_and_prioritizes_new_episodes(tmp_path: Path) -> None:
    series_dir = tmp_path / "S01"
    series_dir.mkdir()

    # E01: már teljesen kész (forrás + fordított magyar SRT)
    e01 = _make_synced_mkv(series_dir / "Show.S01E01.mkv")
    (series_dir / "Show.S01E01.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nEp1\n", encoding="utf-8")
    (series_dir / "Show.S01E01.hu.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\n1. rész\n", encoding="utf-8")

    # E02: ismert részleges epizód
    e02 = series_dir / "Show.S01E02.mkv"
    e02.write_bytes(e01.read_bytes())

    cache = SeriesCacheManager(cache_path=tmp_path / "series_cache.json")
    cache.mark_episode(e02, "partial")

    # E03: vadonatúj epizód (nincs a cache-ben!)
    time.sleep(0.02)
    e03 = series_dir / "Show.S01E03_NEW.mkv"
    e03.write_bytes(e01.read_bytes())

    done_eps, pending_eps = cache.scan_folder(series_dir, target_language="Hungarian")
    assert done_eps == [e01.resolve()]
    # Az új epizód (E03) az ELSŐ helyre kerül az ismert félkész (E02) elé!
    assert pending_eps == [e03.resolve(), e02.resolve()]

    # Kötegelt futtatás: csak E03 és E02 kerül feldolgozásra, E01-hez hozzá sem nyúl!
    orig1 = (app1_config.processed_log, app1_config.temp_audio_dir, app1_config.extract_embedded_subtitles)
    orig2 = (app2_config.target_language, app2_config.api_request_delay)
    app1_config.processed_log = str(tmp_path / "processed.log")
    app1_config.temp_audio_dir = str(tmp_path / "temp_audio")
    app1_config.extract_embedded_subtitles = True
    app2_config.target_language = "Hungarian"
    app2_config.api_request_delay = 0.0

    class _MockSeriesLLM(LLMClientBase):
        def translate_chunk(self, _chunk_text: str) -> str:
            return "[1] Új magyar felirat"

    try:
        with patch(
            "plex_subtitler.app2_translator.translator_daemon.get_llm_client",
            return_value=_MockSeriesLLM(),
        ):
            outputs = run_batch_one_click([series_dir], cache_manager=cache)

        assert len(outputs) == 2
        assert (series_dir / "Show.S01E03_NEW.hu.srt").exists()
        assert (series_dir / "Show.S01E02.hu.srt").exists()
        assert cache.is_episode_done(e03, "Hungarian") is True
        assert cache.is_episode_done(e02, "Hungarian") is True
    finally:
        app1_config.processed_log, app1_config.temp_audio_dir, app1_config.extract_embedded_subtitles = orig1
        app2_config.target_language, app2_config.api_request_delay = orig2


def test_watcher_priority_queue_puts_new_file_first(tmp_path: Path) -> None:
    pq: PriorityQueue[tuple[int, int, Path]] = PriorityQueue()
    seq = itertools.count()
    pq.put((10, next(seq), tmp_path / "old_ep.mkv"))
    handler = VideoEventHandler(pq, seq)
    handler._enqueue(tmp_path / "brand_new_ep.mkv")

    first = pq.get()[2].name
    second = pq.get()[2].name
    assert first == "brand_new_ep.mkv"
    assert second == "old_ep.mkv"


def test_glossary_injected_into_prompt_and_shift_clamps_at_zero() -> None:
    orig_glossary = dict(app2_config.glossary)
    app2_config.glossary = {"Darth Vader": "Darth Vader", "Lightsaber": "Fénykard"}
    try:
        class _Dummy(LLMClientBase):
            def translate_chunk(self, chunk_text: str) -> str:
                return chunk_text

        c = _Dummy()
        assert "Darth Vader => Darth Vader" in c.system_prompt
        assert "Lightsaber => Fénykard" in c.system_prompt
    finally:
        app2_config.glossary = orig_glossary

    parsed = _parse_glossary_text("A = B\n  Neo = Neo  \ninvalid_line\n")
    assert parsed == {"A": "B", "Neo": "Neo"}
    assert "Neo = Neo" in _format_glossary_text(parsed)

    shifted_neg500 = shift_srt_timestamps([SrtBlock(1, "00:00:01,000 --> 00:00:02,000", "Hi")], -500)
    assert shifted_neg500[0].timestamp == "00:00:00,500 --> 00:00:01,500"
    shifted = shift_srt_timestamps([SrtBlock(1, "00:00:01,000 --> 00:00:02,000", "Hi")], -2000)
    assert shifted[0].timestamp == "00:00:00,000 --> 00:00:00,100"


def test_opensubtitles_hash_and_download_mock(tmp_path: Path) -> None:
    vid = tmp_path / "big_movie.mkv"
    vid.write_bytes(b"\x01\x02\x03\x04" * 50000)  # 200 KB
    h = compute_opensubtitles_hash(vid)
    assert len(h) == 16

    search_resp = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"data": [{"attributes": {"files": [{"file_id": 999}]}}]},
    )
    dl_meta_resp = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"link": "https://dl.example.com/sub.srt"},
    )
    dl_file_resp = SimpleNamespace(
        raise_for_status=lambda: None,
        content=b"1\n00:00:01,000 --> 00:00:02,000\nDownloaded sub\n",
    )

    with (
        patch("requests.get", side_effect=[search_resp, dl_file_resp]),
        patch("requests.post", return_value=dl_meta_resp),
    ):
        out = download_opensubtitles_srt(vid, api_key="test-key")
    assert out == tmp_path / "big_movie.srt"
    assert "Downloaded sub" in out.read_text(encoding="utf-8")


def test_pgs_ocr_graceful_fallback_icon_and_dnd(tmp_path: Path) -> None:
    assert APP_ICON_PATH.exists()
    assert _HAS_DND is True
    mkv = _make_synced_mkv(tmp_path / "no_pgs.mkv")
    assert audio_extractor.extract_bitmap_subtitle_ocr(mkv) is None
