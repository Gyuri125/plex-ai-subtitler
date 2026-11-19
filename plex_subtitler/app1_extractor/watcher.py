"""Watchdog-alapú mappafigyelő daemon prioritásos sorral (új fájlok előre sorolása)."""

from __future__ import annotations

import itertools
import threading
import time
from pathlib import Path
from queue import Empty, PriorityQueue
from typing import Callable

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from plex_subtitler.app1_extractor.file_processor import SUPPORTED_EXTENSIONS, FileProcessor
from plex_subtitler.app1_extractor.hardware_manager import wait_while_gaming
from plex_subtitler.config import app1_config
from plex_subtitler.logger import get_logger

log = get_logger(__name__, level=app1_config.log_level)

_STABILITY_CHECK_INTERVAL = 2.0
_STABILITY_REQUIRED_CHECKS = 3
_PRIORITY_NEW = 1
_PRIORITY_EXISTING = 10


def _wait_for_file_stable(
    path: Path,
    check_interval: float = _STABILITY_CHECK_INTERVAL,
    required_checks: int = _STABILITY_REQUIRED_CHECKS,
    stop_event: threading.Event | None = None,
) -> bool:
    """Megvárja, amíg a fájl mérete nem változik több egymást követő ellenőrzésen át."""
    stable_count = 0
    last_size = -1

    while stable_count < required_checks:
        if stop_event and stop_event.is_set():
            return False
        try:
            current_size = path.stat().st_size
        except FileNotFoundError:
            log.warning("Fájl eltűnt a stabilitásellenőrzés során: '%s'", path.name)
            return False

        if current_size == last_size and current_size > 0:
            stable_count += 1
            log.debug("Stabilitás: %d/%d (méret: %d bájt) '%s'", stable_count, required_checks, current_size, path.name)
        else:
            if last_size != -1:
                log.debug("Méretváltozás észlelve (%d → %d bájt), visszaállítva: '%s'", last_size, current_size, path.name)
            stable_count = 0
            last_size = current_size

        if stable_count < required_checks:
            time.sleep(check_interval)

    return True


class VideoEventHandler(FileSystemEventHandler):
    """Watchdog eseménykezelő: az újonnan érkező videókat magas prioritással teszi sorba."""

    def __init__(self, work_queue: PriorityQueue[tuple[int, int, Path]], seq_counter: itertools.count) -> None:
        super().__init__()
        self._queue = work_queue
        self._seq = seq_counter

    def on_created(self, event: FileCreatedEvent) -> None:  # type: ignore[override]
        if not event.is_directory:
            self._enqueue(Path(str(event.src_path)))

    def on_moved(self, event: FileMovedEvent) -> None:  # type: ignore[override]
        if not event.is_directory:
            self._enqueue(Path(str(event.dest_path)))

    def _enqueue(self, path: Path) -> None:
        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            log.info("⚡ Új fájl észlelve (első helyre sorolva): '%s'", path.name)
            self._queue.put((_PRIORITY_NEW, next(self._seq), path))


class WatcherDaemon:
    """Kezeli a Watchdog Observer(eke)t és a prioritásos feldolgozási munkaszálat."""

    def __init__(
        self,
        on_srt_ready: Callable[[Path], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        extra_dirs: list[str] | None = None,
    ) -> None:
        self._queue: PriorityQueue[tuple[int, int, Path]] = PriorityQueue()
        self._seq = itertools.count()
        self._processor = FileProcessor()
        self._observer = Observer()
        self._stop_event = threading.Event()
        self._on_srt_ready = on_srt_ready
        self._on_error = on_error
        self._extra_dirs = extra_dirs

    def _get_watch_dirs(self) -> list[str]:
        return self._extra_dirs if self._extra_dirs is not None else app1_config.watch_dirs

    def enqueue_priority(self, path: Path) -> None:
        """Közvetlenül az első helyre sorol egy videófájlt."""
        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            self._queue.put((_PRIORITY_NEW, next(self._seq), path))

    def start(self) -> None:
        """Elindítja a watchert és a feldolgozó munkaszálat, majd blokkolja a hívót."""
        if not self._setup_observers():
            msg = "Egyetlen érvényes WATCH_DIRS mappa sem lett regisztrálva! Ellenőrizd a beállításokat."
            log.error(msg)
            if self._on_error:
                self._on_error(msg)
            return

        worker_thread = threading.Thread(
            target=self._worker_loop,
            name="FileProcessorWorker",
            daemon=True,
        )
        worker_thread.start()
        self._observer.start()

        log.info("🚀 Mappafigyelő elindult.")
        log.info("Figyelt mappák:")
        for d in self._get_watch_dirs():
            log.info("  → %s", d)

        try:
            while not self._stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            log.info("Megszakítás (Ctrl+C) észlelve...")
            self._stop_event.set()
        finally:
            self._shutdown(worker_thread)

    def stop(self) -> None:
        """Jelzi a leállást a daemonnak."""
        self._stop_event.set()

    def scan_existing(self) -> None:
        """Induláskor sorba teszi a már létező, feldolgozatlan videofájlokat."""
        log.info("Már meglévő videofájlok átvizsgálása...")
        queued = 0
        for watch_dir_str in self._get_watch_dirs():
            watch_dir = Path(watch_dir_str.strip())
            if not watch_dir.is_dir():
                log.warning("A mappa nem létezik, kihagyjuk: '%s'", watch_dir)
                continue
            for file_path in watch_dir.rglob("*"):
                if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
                    self._queue.put((_PRIORITY_EXISTING, next(self._seq), file_path))
                    queued += 1
        log.info("Meglévő fájlok sorba helyezve a feldolgozáshoz (%d db).", queued)

    def _setup_observers(self) -> bool:
        """Beregisztrálja a figyelt mappákat az Observer-be."""
        handler = VideoEventHandler(self._queue, self._seq)
        registered = 0
        for watch_dir_str in self._get_watch_dirs():
            watch_dir = Path(watch_dir_str.strip())
            if not watch_dir.is_dir():
                log.warning("Figyelt mappa nem található, kihagyjuk: '%s'", watch_dir)
                continue
            self._observer.schedule(handler, str(watch_dir), recursive=True)
            log.debug("Mappa regisztrálva: '%s'", watch_dir)
            registered += 1
        return registered > 0

    def _worker_loop(self) -> None:
        """Háttér-munkaszál: kiveszi a fájlokat a prioritásos sorból és feldolgozza őket."""
        log.debug("Feldolgozó munkaszál elindult.")
        while not self._stop_event.is_set():
            try:
                _prio, _seq, video_path = self._queue.get(timeout=1.0)
            except Empty:
                continue

            try:
                wait_while_gaming(stop_event=self._stop_event)
                if self._stop_event.is_set():
                    break
                log.debug("Fájl-méret stabilitás ellenőrzése: '%s'", video_path.name)
                if _wait_for_file_stable(video_path, stop_event=self._stop_event):
                    self._processor.process(
                        video_path,
                        stop_event=self._stop_event,
                        on_srt_ready=self._on_srt_ready,
                    )
            except Exception as exc:
                log.exception("Váratlan hiba a munkaszálban '%s': %s", video_path.name, exc)
            finally:
                self._queue.task_done()

        log.debug("Feldolgozó munkaszál leállt.")

    def _shutdown(self, worker_thread: threading.Thread) -> None:
        """Tisztán leállítja az Observer-t és megvárja a munkaszál befejezését."""
        log.info("Observer leállítása...")
        self._observer.stop()
        self._observer.join(timeout=10)
        log.info("Feldolgozó munkaszál megvárása (max. 60 mp)...")
        worker_thread.join(timeout=60)
        log.info("✅ Mappafigyelő leállítva.")
