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
from .srt_parser import SrtParser
from .llm_client import get_llm_client, translate_blocks_with_retry

log = get_logger(__name__, level=app2_config.log_level)

# Fájlméret-stabilitás ellenőrzési paraméterek (App 1 mintájára)
_STABILITY_CHECK_INTERVAL = 2.0
_STABILITY_REQUIRED_CHECKS = 3

def process_single_srt(srt_path: Path, progress_callback=None, stop_event=None) -> None:
    """Egyedi SRT fájl lefordítása."""
    import time
    llm_client = get_llm_client()
    batch_size = app2_config.translation_batch_size
    
    target_path = srt_path.parent / f"{srt_path.stem}.hu.srt"
    
    log.info("=" * 60)
    log.info("Fordítás kezdete: '%s'", srt_path.name)
    log.info("=" * 60)

    parser = SrtParser(srt_path)
    total_blocks = len(parser.blocks)
    translated_blocks = []
    
    start_time = time.time()
    processed_count = 0
    
    for chunk in parser.get_chunks(batch_size=batch_size):
        if stop_event and stop_event.is_set():
            log.warning("Fordítás megszakítva (leállítás).")
            break
            
        chunk_text = SrtParser.blocks_to_text_format(chunk)
        result_text = translate_blocks_with_retry(llm_client, chunk_text, stop_event=stop_event)
        
        if not result_text:
            log.error("Üres válasz érkezett az API-tól, a blokk nem lett lefordítva.")
            translated_blocks.extend(chunk)
        else:
            t_blocks = SrtParser.apply_translation_to_blocks(chunk, result_text)
            translated_blocks.extend(t_blocks)
            
        processed_count += len(chunk)
        if progress_callback:
            elapsed = time.time() - start_time
            pct = processed_count / total_blocks if total_blocks > 0 else 0
            eta = (elapsed / pct - elapsed) if pct > 0 else 0
            progress_callback(processed_count, total_blocks, elapsed, eta)
            
        log.info(f"Fordítás folyamatban: {processed_count}/{total_blocks} blokk kész...")
        
    if len(translated_blocks) > 0:
        SrtParser.save_srt(translated_blocks, target_path)
        if stop_event and stop_event.is_set():
            log.info(f"⚠️ Fordítás megszakítva. Részeredmény elmentve: {target_path.name}")
        else:
            log.info(f"✅ Sikeresen lefordítva: {target_path.name}")


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
            log.info("📂 Új SRT észlelve, sorba helyezve fordításra: '%s'", path.name)
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
        log.info("🚀 Fordító mappafigyelő elindult.")

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
        log.info("✅ Fordító Mappafigyelő leállítva.")


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
