"""Egykattintásos és kötegelt (sorozat-mappás) feliratozási pipeline."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from plex_subtitler.app1_extractor.file_processor import SUPPORTED_EXTENSIONS, FileProcessor
from plex_subtitler.app1_extractor.series_cache import SeriesCacheManager
from plex_subtitler.app2_translator.sync_verifier import auto_align_subtitle
from plex_subtitler.app2_translator.translator_daemon import process_single_srt
from plex_subtitler.config import app2_config
from plex_subtitler.logger import get_logger

log = get_logger("GUI", level="INFO")


def run_one_click_pipeline(
    input_path: Path,
    status_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[float], None] | None = None,
    stop_event: threading.Event | None = None,
    subtitle_stream_index: int | None = None,
    audio_stream_index: int | None = None,
) -> Path | None:
    """Teljes 1-kattintásos film -> magyar felirat lánc (GUI és CLI támogatással)."""
    input_path = Path(input_path).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"A fájl nem található: '{input_path}'")

    if input_path.suffix.lower() == ".srt":
        srt_path = input_path
    elif input_path.suffix.lower() in SUPPORTED_EXTENSIONS:
        candidate_srt = input_path.with_suffix(".srt")
        if candidate_srt.exists() and candidate_srt.stat().st_size > 0 and subtitle_stream_index is None:
            if status_callback:
                status_callback("Meglévő .srt megtalálva, ugrás a fordításra...")
            srt_path = candidate_srt
        else:
            if status_callback:
                status_callback("1/2 Felirat kinyerése / Whisper beszédfelismerés...")
            srt_path = FileProcessor().process(
                input_path,
                progress_callback=(
                    (lambda pct, _el, _eta: progress_callback(pct * 0.4))
                    if progress_callback
                    else None
                ),
                stop_event=stop_event,
                subtitle_stream_index=subtitle_stream_index,
                audio_stream_index=audio_stream_index,
            )
            if not srt_path:
                return None
    else:
        raise ValueError(f"Nem támogatott fájltípus: '{input_path.suffix}'")

    if stop_event and stop_event.is_set():
        return None

    if status_callback:
        status_callback(f"2/2 AI fordítás ({app2_config.target_language})...")

    def _tr_progress(cur: int, tot: int, _el: float, _eta: float) -> None:
        if progress_callback and tot > 0:
            progress_callback(0.4 + 0.6 * (cur / tot))

    translated = process_single_srt(
        srt_path,
        progress_callback=_tr_progress,
        stop_event=stop_event,
        resume=True,
    )
    if (
        translated
        and app2_config.auto_align_subtitles
        and input_path.suffix.lower() in SUPPORTED_EXTENSIONS
        and not (stop_event and stop_event.is_set())
    ):
        if status_callback:
            status_callback("Szinkron ellenőrzése / automatikus finomhangolás...")
        _, translated, _ = auto_align_subtitle(input_path, translated, output_path=translated)
    if translated and status_callback:
        status_callback(f"✅ Kész: {translated.name}")
    return translated


def run_batch_one_click(
    paths: list[Path],
    cache_manager: SeriesCacheManager | None = None,
    status_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[float], None] | None = None,
    stop_event: threading.Event | None = None,
    stream_overrides: dict[Path, tuple[int | None, int | None]] | None = None,
) -> list[Path]:
    """Fájlok és/vagy sorozat-mappák kötegelt 1-Click feldolgozása cache-sel és új-epizód prioritással."""
    cache = cache_manager or SeriesCacheManager()
    queue_items: list[Path] = []
    skipped_done = 0

    for raw_p in paths:
        p = Path(raw_p).resolve()
        if p.is_dir():
            done_eps, pending_eps = cache.scan_folder(p)
            skipped_done += len(done_eps)
            queue_items.extend(pending_eps)
        elif p.is_file():
            if p.suffix.lower() in SUPPORTED_EXTENSIONS and cache.is_episode_done(p):
                skipped_done += 1
                log.info("✅ Már kész (cache, kihagyva): '%s'", p.name)
            else:
                queue_items.append(p)

    if not queue_items:
        if status_callback:
            status_callback(f"✅ Minden epizód már le van fordítva ({skipped_done} db cache-elve).")
        return []

    results: list[Path] = []
    total_items = len(queue_items)
    for idx, item in enumerate(queue_items, start=1):
        if stop_event and stop_event.is_set():
            break
        if status_callback:
            status_callback(f"[{idx}/{total_items}] Feldolgozás: {item.name}...")

        def _item_progress(val: float, i: int = idx) -> None:
            if progress_callback:
                progress_callback(((i - 1) + val) / total_items)

        sub_ov, aud_ov = (stream_overrides or {}).get(item, (None, None))
        out = run_one_click_pipeline(
            item,
            status_callback=lambda msg, i=idx: status_callback(f"[{i}/{total_items}] {msg}") if status_callback else None,
            progress_callback=_item_progress,
            stop_event=stop_event,
            subtitle_stream_index=sub_ov,
            audio_stream_index=aud_ov,
        )
        if out:
            results.append(out)
            if item.suffix.lower() in SUPPORTED_EXTENSIONS:
                cache.mark_episode(
                    item,
                    status="done",
                    src_srt=item.with_suffix(".srt"),
                    translated_srt=out,
                )
        elif item.suffix.lower() in SUPPORTED_EXTENSIONS:
            cache.mark_episode(item, status="partial", src_srt=item.with_suffix(".srt"))

    return results
