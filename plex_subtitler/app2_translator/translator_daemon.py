"""
translator_daemon.py - Watchdog daemon az angol SRT fájlok figyelésére.

Működése:
  - Figyeli a megadott mappákban az új .srt fájlokat.
  - Stabilizáció után (fájl másolás/generálás vége) elkezdi a fordítást.
  - Kizárja a már lefordított (.hu.srt) fájlokat.
"""

from __future__ import annotations

import time
import threading
from pathlib import Path
from queue import Queue, Empty

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileMovedEvent

from plex_subtitler.logger import get_logger
from plex_subtitler.config import app1_config, app2_config
from plex_subtitler.notifier import notify_translation_completed
from .srt_parser import SrtParser
from .llm_client import get_llm_client, translate_blocks_with_retry

log = get_logger(__name__, level=app2_config.log_level)

# Fájlméret-stabilitás ellenőrzési paraméterek (App 1 mintájára)
_STABILITY_CHECK_INTERVAL = 2.0
_STABILITY_REQUIRED_CHECKS = 3

def process_single_srt(srt_path: Path, progress_callback=None, stop_event=None) -> None:
    """Egyedi SRT fájl lefordítása (párhuzamosított feldolgozással és sorozat-memóriával)."""
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .series_memory import get_series_prompt_injection
    from plex_subtitler.quality_gate import validate_and_repair_translated_blocks
    from plex_subtitler.sub_sync import auto_sync_subtitle

    llm_client = get_llm_client()
    batch_size = app2_config.translation_batch_size
    workers = max(1, app2_config.parallel_translation_workers)
    
    target_path = srt_path.parent / f"{srt_path.stem}.hu.srt"
    
    log.info("=" * 60)
    log.info("Fordítás kezdete: '%s' (szálak száma: %d)", srt_path.name, workers)
    log.info("=" * 60)

    parser = SrtParser(srt_path)
    total_blocks = len(parser.blocks)
    if total_blocks == 0:
        log.warning("Az SRT fájl nem tartalmaz feldolgozható blokkokat: '%s'", srt_path.name)
        return

    chunks = list(parser.get_chunks(batch_size=batch_size))
    total_chunks = len(chunks)
    series_context = get_series_prompt_injection(srt_path)
    if series_context:
        log.info("Sorozat-kontextus aktív: '%s'", srt_path.name)

    start_time = time.time()
    processed_count = 0
    progress_lock = threading.Lock()

    def update_progress(delta_count: int):
        nonlocal processed_count
        with progress_lock:
            processed_count += delta_count
            if progress_callback:
                elapsed = time.time() - start_time
                pct = processed_count / total_blocks if total_blocks > 0 else 0
                eta = (elapsed / pct - elapsed) if pct > 0 else 0
                progress_callback(processed_count, total_blocks, elapsed, eta)
            log.info("Fordítás folyamatban: %d/%d blokk kész...", processed_count, total_blocks)

    def process_chunk_list(chunk_list: list[list]) -> list:
        partition_results = []
        sliding_history: list[str] = []

        for chunk in chunk_list:
            if stop_event and stop_event.is_set():
                break

            context_parts = []
            if series_context:
                context_parts.append(series_context)

            if app2_config.use_sliding_context and sliding_history:
                recent = sliding_history[-app2_config.sliding_context_size:]
                context_parts.append("\n".join(recent))

            context_text = "\n\n".join(context_parts)
            chunk_text = SrtParser.blocks_to_text_format(chunk)

            result_text = translate_blocks_with_retry(
                llm_client,
                chunk_text,
                context_text=context_text,
                stop_event=stop_event,
            )

            if not result_text:
                log.error("Üres válasz érkezett az API-tól, az eredeti blokkot tartjuk meg.")
                partition_results.extend(chunk)
            else:
                t_blocks = SrtParser.apply_translation_to_blocks(chunk, result_text)
                partition_results.extend(t_blocks)
                for b in t_blocks:
                    clean_text = b.text.replace("\n", " ").strip()
                    sliding_history.append(f"[{b.index}] {clean_text}")

            update_progress(len(chunk))

        return partition_results

    translated_blocks: list = []

    # Ha 1 worker vagy kevés chunk van, egyszerű szekvenciális futás
    if workers <= 1 or total_chunks <= 1:
        translated_blocks = process_chunk_list(chunks)
    else:
        # Párhuzamos partíciókra bontás
        num_partitions = min(workers, total_chunks)
        part_size = (total_chunks + num_partitions - 1) // num_partitions
        partitions = [chunks[i:i + part_size] for i in range(0, total_chunks, part_size)]

        partition_results_ordered = [None] * len(partitions)
        with ThreadPoolExecutor(max_workers=len(partitions)) as executor:
            future_to_idx = {
                executor.submit(process_chunk_list, part): idx 
                for idx, part in enumerate(partitions)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    partition_results_ordered[idx] = future.result()
                except Exception as exc:
                    log.error("Hiba a partíció fordításakor (szál %d): %s", idx, exc)
                    # Fallback az eredeti chunkokra
                    fallback_chunk_blocks = [b for chk in partitions[idx] for b in chk]
                    partition_results_ordered[idx] = fallback_chunk_blocks

        for part_res in partition_results_ordered:
            if part_res:
                translated_blocks.extend(part_res)

    if len(translated_blocks) > 0:
        # Quality Gate ellenőrzés és javítás
        final_blocks = validate_and_repair_translated_blocks(parser.blocks, translated_blocks)
        SrtParser.save_srt(final_blocks, target_path)
        total_time = time.time() - start_time

        # Opcionális automatikus időzítés-szinkronizálás, ha elérhető a referencia videó
        if app1_config.auto_sync_subtitles:
            for ext in [".mkv", ".mp4", ".avi", ".mov"]:
                candidate_video = srt_path.with_suffix(ext)
                if candidate_video.exists():
                    auto_sync_subtitle(candidate_video, target_path)
                    break

        if stop_event and stop_event.is_set():
            log.info(f"Forditas megszakitva. Reszeredmeny elmentve: {target_path.name}")
        else:
            log.info(f"Sikeresen leforditva: {target_path.name} ({total_time:.1f} mp)")
            try:
                notify_translation_completed(target_path, len(final_blocks), total_time)
            except Exception as notify_err:
                log.warning("Hiba az értesítés küldésekor: %s", notify_err)


class SrtEventHandler(FileSystemEventHandler):
    """Eseménykezelő új vagy áthelyezett SRT fájlok észlelésére."""
    def __init__(self, work_queue: Queue) -> None:
        super().__init__()
        self._queue = work_queue

    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory:
            self._enqueue(Path(event.src_path))

    def on_moved(self, event: FileMovedEvent) -> None:
        if not event.is_directory:
            self._enqueue(Path(event.dest_path))

    def _enqueue(self, path: Path) -> None:
        if path.suffix.lower() == ".srt":
            # Ne vegyük sorba a már magyar fájlokat
            if path.name.endswith(".hu.srt"):
                return
            log.info("Uj SRT eszlelve, sorba helyezve forditasra: '%s'", path.name)
            self._queue.put(path)


class TranslatorDaemon:
    """Főszervező osztály az SRT fordítás daemonjához."""

    def __init__(self) -> None:
        self._queue: Queue[Path] = Queue()
        self._observer = Observer()
        self._stop_event = threading.Event()
        self.progress_callback = None
        self.finished_callback = None

    def start(self) -> None:
        """Elindítja a watchert és a fordító munkaszálat."""
        self._setup_observers()

        worker_thread = threading.Thread(
            target=self._worker_loop,
            name="TranslatorWorker",
            daemon=True,
        )
        worker_thread.start()

        self._observer.start()
        log.info("Fordito mappafigyelo elindult.")

        try:
            while not self._stop_event.is_set():
                time.sleep(1)
        finally:
            self._shutdown(worker_thread)

    def scan_existing(self) -> None:
        """Végignézi a meglévő angol SRT-ket, amihez nincs még magyar."""
        log.info("Meglévő SRT fájlok átvizsgálása fordításhoz...")
        for watch_dir_str in app1_config.watch_dirs:
            watch_dir = Path(watch_dir_str.strip())
            if not watch_dir.is_dir():
                continue
            
            for srt_file in watch_dir.rglob("*.srt"):
                if srt_file.name.endswith(".hu.srt"):
                    continue
                # Ha már van mellette .hu.srt, kihagyjuk
                target_path = srt_file.parent / f"{srt_file.stem}.hu.srt"
                if not target_path.exists():
                    self._queue.put(srt_file)

    def _setup_observers(self) -> None:
        handler = SrtEventHandler(self._queue)
        for watch_dir_str in app1_config.watch_dirs:
            watch_dir = Path(watch_dir_str.strip())
            if watch_dir.is_dir():
                self._observer.schedule(handler, str(watch_dir), recursive=True)

    def _worker_loop(self) -> None:
        log.debug("Fordító munkaszál elindult.")
        
        while not self._stop_event.is_set():
            try:
                srt_path = self._queue.get(timeout=1.0)
            except Empty:
                continue

            try:
                # 1. Stabilitás ellenőrzés
                _wait_for_file_stable(srt_path)
                
                # 2. Célfájl ellenőrzése
                target_path = srt_path.parent / f"{srt_path.stem}.hu.srt"
                if target_path.exists():
                    log.info("A magyar SRT már létezik, kihagyjuk: %s", target_path.name)
                    continue

                # 3. Fájl betöltése és feldolgozása
                process_single_srt(srt_path, self.progress_callback, self._stop_event)
                
                # 4. Eredmény mentése callback
                if self.finished_callback and not self._stop_event.is_set():
                    self.finished_callback()

            except Exception as exc:
                log.exception("Váratlan hiba fordítás közben '%s': %s", srt_path.name, exc)
            finally:
                self._queue.task_done()

        log.debug("Fordító munkaszál leállt.")

    def _shutdown(self, worker_thread: threading.Thread) -> None:
        log.info("Fordító Observer leállítása...")
        self._observer.stop()
        self._observer.join(timeout=5)
        worker_thread.join(timeout=10)
        log.info("Fordito Mappafigyelo leallitva.")


def _wait_for_file_stable(path: Path) -> None:
    """Megvárja, amíg a fájlméret nem változik."""
    stable_count = 0
    last_size = -1

    while stable_count < _STABILITY_REQUIRED_CHECKS:
        try:
            current_size = path.stat().st_size
        except FileNotFoundError:
            return

        if current_size == last_size:
            stable_count += 1
        else:
            stable_count = 0
            last_size = current_size
        time.sleep(_STABILITY_CHECK_INTERVAL)
