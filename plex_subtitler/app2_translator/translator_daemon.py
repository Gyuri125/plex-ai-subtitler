"""Watchdog daemon az SRT feliratok figyelésére, checkpoint-folytatásra és fordítására."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Callable

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from plex_subtitler.app1_extractor.hardware_manager import wait_while_gaming
from plex_subtitler.app1_extractor.transcriber import unload_model
from plex_subtitler.app2_translator.llm_client import get_llm_client, translate_blocks_with_retry
from plex_subtitler.app2_translator.plex_client import refresh_plex_library_for_file
from plex_subtitler.app2_translator.srt_formatter import format_srt_blocks
from plex_subtitler.app2_translator.srt_parser import SrtBlock, SrtParser
from plex_subtitler.app2_translator.sync_verifier import (
    evaluate_subtitle_quality,
    find_untranslated_block_indices,
)
from plex_subtitler.config import (
    KNOWN_TRANSLATED_SUFFIXES,
    LOGS_DIR,
    app1_config,
    app2_config,
    get_language_code,
)
from plex_subtitler.logger import get_logger
from plex_subtitler.notifier import notify_translation_complete

log = get_logger(__name__, level=app2_config.log_level)

_STABILITY_CHECK_INTERVAL = 2.0
_STABILITY_REQUIRED_CHECKS = 2


def get_target_srt_path(srt_path: Path, target_language: str | None = None) -> Path:
    """Előállítja a célnyelvi SRT útvonalat (pl. film.hu.srt, film.de.srt, film.es.srt)."""
    lang = target_language or app2_config.target_language
    code = get_language_code(lang)
    return srt_path.parent / f"{srt_path.stem}.{code}.srt"


def is_already_translated_filename(path: Path) -> bool:
    """Igazat ad, ha a fájlnév már lefordított nyelvi kiterjesztést visel (pl. .hu.srt, .de.srt)."""
    lower_name = path.name.lower()
    return any(lower_name.endswith(suffix) for suffix in KNOWN_TRANSLATED_SUFFIXES)


def is_translation_complete(src_srt: Path, target_srt: Path) -> bool:
    """Ellenőrzi, hogy a cél SRT létezik-e és minden forrásblokkot lefed-e."""
    if not target_srt.exists():
        return False
    try:
        src_blocks = SrtParser(src_srt).blocks
        if not src_blocks:
            return True
        target_indices = {b.index for b in SrtParser(target_srt).blocks}
        return all(b.index in target_indices for b in src_blocks)
    except Exception:
        return False


def _wait_for_file_stable(
    path: Path,
    stop_event: threading.Event | None = None,
) -> bool:
    """Megvárja, amíg a fájl mérete nem változik."""
    stable_count = 0
    last_size = -1
    while stable_count < _STABILITY_REQUIRED_CHECKS:
        if stop_event and stop_event.is_set():
            return False
        try:
            current_size = path.stat().st_size
        except FileNotFoundError:
            return False

        if current_size == last_size and current_size > 0:
            stable_count += 1
        else:
            stable_count = 0
            last_size = current_size

        if stable_count < _STABILITY_REQUIRED_CHECKS:
            time.sleep(_STABILITY_CHECK_INTERVAL)
    return True


def _build_sample_excerpt(blocks: list[SrtBlock]) -> str:
    """Összeállít egy rövid szövegkivonatot a film elejéről és közepéről az auto-kontextushoz."""
    sample = blocks[:40]
    if len(blocks) > 80:
        mid = len(blocks) // 2
        sample = sample + blocks[mid : mid + 15]
    return "\n".join(b.text.replace("\n", " ") for b in sample if b.text.strip())


def _record_movie_history(
    src_srt: Path,
    target_srt: Path,
    total_blocks: int,
    elapsed_sec: float,
    status: str,
) -> None:
    """Rögzíti a feldolgozott film összegzését a logs/processed_movies.log fájlba."""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        history_file = LOGS_DIR / "processed_movies.log"
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = (
            f"{stamp} | {status} | Film: {src_srt.stem} | "
            f"Cél: {target_srt.name} | Blokkok: {total_blocks} | "
            f"Modell: {app2_config.llm_provider}/{app2_config.llm_model} | "
            f"Idő: {elapsed_sec:.1f} mp\n"
        )
        with open(history_file, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def process_single_srt(
    srt_path: Path,
    progress_callback: Callable[[int, int, float, float], None] | None = None,
    stop_event: threading.Event | None = None,
    resume: bool = True,
) -> Path | None:
    """Egyedi SRT fájl lefordítása időbélyeg-megőrzéssel és checkpoint/resume támogatással."""
    srt_path = Path(srt_path)
    target_path = get_target_srt_path(srt_path)

    log.info("============================================================")
    log.info("🎬 Fordítás kezdete: '%s' → '%s'", srt_path.name, target_path.name)

    parser = SrtParser(srt_path)
    total_blocks = len(parser.blocks)
    if total_blocks == 0:
        log.warning("Üres vagy értelmezhetetlen SRT fájl: '%s'", srt_path.name)
        return None

    translated_map: dict[int, SrtBlock] = {}
    if resume and target_path.exists():
        try:
            existing_blocks = SrtParser(target_path).blocks
            for eb in existing_blocks:
                translated_map[eb.index] = eb
        except Exception as exc:
            log.warning("Korábbi rész-SRT beolvasása sikertelen ('%s'): %s", target_path.name, exc)

    remaining_blocks = [
        b for b in parser.blocks if b.index not in translated_map
    ]
    already_done_count = total_blocks - len(remaining_blocks)

    if not remaining_blocks:
        log.info("✅ A fordítás már teljes volt (%d/%d blokk): '%s'", total_blocks, total_blocks, target_path.name)
        if progress_callback:
            progress_callback(total_blocks, total_blocks, 0.0, 0.0)
        return target_path

    if already_done_count > 0:
        log.info(
            "⏩ Folytatás checkpointtól: %d/%d blokk már kész ('%s'), hátravan: %d",
            already_done_count,
            total_blocks,
            target_path.name,
            len(remaining_blocks),
        )
        if progress_callback:
            progress_callback(already_done_count, total_blocks, 0.0, 0.0)

    if (
        app1_config.unload_whisper_before_translate
        and app1_config.whisper_device.lower() == "cuda"
        and app2_config.llm_provider.lower().strip() in ("local", "ollama")
    ):
        unload_model()

    llm_client = get_llm_client()

    # Auto-kontextus (auto-prompt) generálása a film stílusáról és szereplőiről
    if app2_config.auto_movie_context and len(parser.blocks) >= 3:
        excerpt = _build_sample_excerpt(parser.blocks)
        if excerpt and not (stop_event and stop_event.is_set()):
            try:
                log.info("🧠 Film-kontextus elemzése (%s)...", srt_path.stem)
                movie_ctx = llm_client.analyze_movie_context(srt_path.stem, excerpt)
                if movie_ctx and movie_ctx.strip():
                    llm_client.set_movie_context(movie_ctx)
                    log.info("🎭 Felismert film-kontextus (auto-prompt):\n%s", movie_ctx.strip())
            except Exception as exc:
                log.warning("Auto-kontextus elemzés kihagyva (%s), normál fordítással folytatjuk.", exc)

    batch_size = max(1, int(app2_config.translation_batch_size))
    start_time = time.time()
    newly_done = 0
    recent_tail_lines: list[str] = []

    for i in range(0, len(remaining_blocks), batch_size):
        chunk = remaining_blocks[i : i + batch_size]
        if stop_event and stop_event.is_set():
            log.warning("Fordítás megszakítva (leállítás).")
            break

        llm_client.set_recent_context("\n".join(recent_tail_lines[-2:]))
        chunk_text = SrtParser.blocks_to_text_format(chunk)
        result_text = translate_blocks_with_retry(
            llm_client,
            chunk_text,
            stop_event=stop_event,
        )

        if stop_event and stop_event.is_set() and not result_text:
            log.warning("Fordítás megszakítva (leállítás).")
            break

        if not result_text:
            log.error("Üres válasz érkezett az API-tól, a blokk eredetiben marad.")
            t_blocks = chunk
        else:
            t_blocks = SrtParser.apply_translation_to_blocks(chunk, result_text)

        for tb in t_blocks:
            translated_map[tb.index] = tb

        if chunk and t_blocks:
            last_orig = chunk[-1].text.replace("\n", " ")
            last_tr = t_blocks[-1].text.replace("\n", " ")
            recent_tail_lines.append(f"{last_orig} -> {last_tr}")
            first_orig = chunk[0].text.replace("\n", " ")[:60]
            first_tr = t_blocks[0].text.replace("\n", " ")[:70]
            log.info("💬 Közbeni fordítás [%d]: \"%s\" → \"%s\"", t_blocks[0].index, first_orig, first_tr)

        # Checkpoint mentése minden blokkcsomag után (TV/Plex formázással)
        ordered_checkpoint = [
            translated_map[b.index]
            for b in parser.blocks
            if b.index in translated_map
        ]
        if app2_config.format_tv_subtitles:
            ordered_checkpoint = format_srt_blocks(
                ordered_checkpoint,
                max_chars=app2_config.max_chars_per_line,
                max_lines=app2_config.max_lines_per_block,
            )
        SrtParser.save_srt(ordered_checkpoint, target_path)

        newly_done += len(chunk)
        processed_count = already_done_count + newly_done
        elapsed = time.time() - start_time
        rate = newly_done / elapsed if elapsed > 0 else 0.0
        eta = (len(remaining_blocks) - newly_done) / rate if rate > 0 else 0.0
        log.info("Fordítás folyamatban: %d/%d blokk kész...", processed_count, total_blocks)

        if progress_callback:
            progress_callback(processed_count, total_blocks, elapsed, eta)

    if not translated_map:
        return None

    total_elapsed = time.time() - start_time
    if stop_event and stop_event.is_set():
        log.warning("⚠️ Fordítás megszakítva. Részeredmény elmentve: '%s'", target_path.name)
        _record_movie_history(srt_path, target_path, len(translated_map), total_elapsed, "RÉSZLEGES")
    else:
        # Célzott automatikus javítókör az esetlegesen eredeti nyelven maradt sorokra
        pre_blocks = [translated_map[b.index] for b in parser.blocks if b.index in translated_map]
        untranslated_ids = find_untranslated_block_indices(
            parser.blocks, pre_blocks, glossary=app2_config.glossary
        )
        # Akkor futtatjuk, ha van lefordítatlan gyanús sor, de nem az egész film azonos nyelvű
        too_many_same_lang = total_blocks >= 20 and (len(untranslated_ids) / total_blocks) > 0.5
        if untranslated_ids and not too_many_same_lang:
            repair_ids = untranslated_ids[:30]
            src_by_idx = {b.index: b for b in parser.blocks}
            repair_blocks = [src_by_idx[idx] for idx in repair_ids if idx in src_by_idx]
            log.info(
                "🔧 Minőség-ellenőrző javítókör: %d eredeti nyelven maradt sor célzott újrafordítása...",
                len(repair_blocks),
            )
            llm_client.set_recent_context(
                f"IMPORTANT: These lines were accidentally left untranslated. "
                f"Translate EVERY single line into natural {app2_config.target_language} and keep the exact [ID] prefix."
            )
            repaired_count = 0
            for j in range(0, len(repair_blocks), 5):
                if stop_event and stop_event.is_set():
                    break
                r_chunk = repair_blocks[j : j + 5]
                r_text = SrtParser.blocks_to_text_format(r_chunk)
                r_out = translate_blocks_with_retry(llm_client, r_text, stop_event=stop_event)
                if not r_out:
                    continue
                fixed_blocks = SrtParser.apply_translation_to_blocks(r_chunk, r_out)
                for orig_b, fixed_b in zip(r_chunk, fixed_blocks):
                    if " ".join(fixed_b.text.split()).lower() != " ".join(orig_b.text.split()).lower():
                        translated_map[fixed_b.index] = fixed_b
                        repaired_count += 1
            if repaired_count > 0:
                repaired_ordered = [
                    translated_map[b.index] for b in parser.blocks if b.index in translated_map
                ]
                if app2_config.format_tv_subtitles:
                    repaired_ordered = format_srt_blocks(
                        repaired_ordered,
                        max_chars=app2_config.max_chars_per_line,
                        max_lines=app2_config.max_lines_per_block,
                    )
                SrtParser.save_srt(repaired_ordered, target_path)
                log.info(
                    "✅ Automatikus javítókör kész: %d/%d sor sikeresen kijavítva.",
                    repaired_count,
                    len(repair_blocks),
                )
            else:
                log.info("ℹ️ Javítókör lefutott, a megjelölt sorok változatlanok maradtak.")

        total_elapsed = time.time() - start_time
        log.info("============================================================")
        log.info(
            "🎉 BEFEJEZÉSI ÖSSZEGZÉS | Film: '%s' | Blokkok: %d/%d | Modell: %s/%s | Idő: %.1f mp | Kimenet: '%s'",
            srt_path.stem,
            len(translated_map),
            total_blocks,
            app2_config.llm_provider,
            app2_config.llm_model,
            total_elapsed,
            target_path.name,
        )
        final_blocks = [translated_map[b.index] for b in parser.blocks if b.index in translated_map]
        qa = evaluate_subtitle_quality(
            parser.blocks,
            final_blocks,
            max_chars_per_line=app2_config.max_chars_per_line,
            glossary=app2_config.glossary,
        )
        log.info("%s", qa.summary_line)
        log.info("============================================================")
        _record_movie_history(
            srt_path,
            target_path,
            len(translated_map),
            total_elapsed,
            f"KÉSZ (Minőség: {qa.score_pct}%)",
        )
        if app2_config.plex_refresh_enabled and app2_config.plex_token.strip():
            refresh_plex_library_for_file(target_path)
        notify_translation_complete(srt_path.stem, target_path, quality_summary=qa.summary_line)
    return target_path


class SrtEventHandler(FileSystemEventHandler):
    """Eseménykezelő új vagy áthelyezett SRT fájlok észlelésére."""

    def __init__(self, work_queue: Queue[Path]) -> None:
        super().__init__()
        self._queue = work_queue

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
        if not event.is_directory:
            self._enqueue(Path(str(event.src_path)))

    def on_moved(self, event: FileMovedEvent) -> None:  # type: ignore[override]
        if not event.is_directory:
            self._enqueue(Path(str(event.dest_path)))

    def _enqueue(self, path: Path) -> None:
        if path.suffix.lower() == ".srt" and not is_already_translated_filename(path):
            log.info("📂 Új SRT észlelve, sorba helyezve fordításra: '%s'", path.name)
            self._queue.put(path)


class TranslatorDaemon:
    """Főszervező osztály az SRT fordítás daemonjához."""

    def __init__(
        self,
        progress_callback: Callable[[int, int, float, float], None] | None = None,
        finished_callback: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._queue: Queue[Path] = Queue()
        self._observer = Observer()
        self._stop_event = threading.Event()
        self.progress_callback = progress_callback
        self.finished_callback = finished_callback
        self._on_error = on_error

    def start(self) -> None:
        """Elindítja a watchert és a fordító munkaszálat."""
        if not self._setup_observers():
            msg = "Egyetlen érvényes figyelt mappa sem érhető el a fordításhoz!"
            log.error(msg)
            if self._on_error:
                self._on_error(msg)
            return

        worker_thread = threading.Thread(
            target=self._worker_loop,
            name="TranslatorWorker",
            daemon=True,
        )
        worker_thread.start()
        self._observer.start()
        log.info("🚀 Fordító mappafigyelő elindult (célnyelv: %s).", app2_config.target_language)

        try:
            while not self._stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            self._stop_event.set()
        finally:
            self._shutdown(worker_thread)

    def stop(self) -> None:
        """Jelzi a leállást a daemonnak."""
        self._stop_event.set()

    def scan_existing(self) -> None:
        """Végignézi a meglévő SRT-ket, amelyekhez még nincs teljes célnyelvi fordítás."""
        log.info("Meglévő SRT fájlok átvizsgálása fordításhoz...")
        queued = 0
        for watch_dir_str in app1_config.watch_dirs:
            watch_dir = Path(watch_dir_str.strip())
            if not watch_dir.is_dir():
                continue
            for srt_file in watch_dir.rglob("*.srt"):
                if is_already_translated_filename(srt_file):
                    continue
                target_path = get_target_srt_path(srt_file)
                if not is_translation_complete(srt_file, target_path):
                    self._queue.put(srt_file)
                    queued += 1
        log.info("Fordításra / folytatásra váró SRT fájlok sorba helyezve (%d db).", queued)

    def _setup_observers(self) -> bool:
        handler = SrtEventHandler(self._queue)
        registered = 0
        for watch_dir_str in app1_config.watch_dirs:
            watch_dir = Path(watch_dir_str.strip())
            if not watch_dir.is_dir():
                log.warning("Figyelt mappa nem található (fordító), kihagyjuk: '%s'", watch_dir)
                continue
            self._observer.schedule(handler, str(watch_dir), recursive=True)
            registered += 1
        return registered > 0

    def _worker_loop(self) -> None:
        log.debug("Fordító munkaszál elindult.")
        while not self._stop_event.is_set():
            try:
                srt_path = self._queue.get(timeout=1.0)
            except Empty:
                continue

            try:
                wait_while_gaming(stop_event=self._stop_event)
                if self._stop_event.is_set():
                    break
                if _wait_for_file_stable(srt_path, stop_event=self._stop_event):
                    target_path = get_target_srt_path(srt_path)
                    if is_translation_complete(srt_path, target_path):
                        log.info("A lefordított SRT már teljes, kihagyjuk: %s", target_path.name)
                    else:
                        process_single_srt(
                            srt_path,
                            progress_callback=self.progress_callback,
                            stop_event=self._stop_event,
                            resume=True,
                        )
                        if self.finished_callback:
                            self.finished_callback()
            except Exception as exc:
                log.exception("Váratlan hiba fordítás közben '%s': %s", srt_path.name, exc)
            finally:
                self._queue.task_done()

        log.debug("Fordító munkaszál leállt.")

    def _shutdown(self, worker_thread: threading.Thread) -> None:
        log.info("Fordító Observer leállítása...")
        self._observer.stop()
        self._observer.join(timeout=10)
        worker_thread.join(timeout=60)
        log.info("✅ Fordító Mappafigyelő leállítva.")
