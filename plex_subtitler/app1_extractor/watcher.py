"""
watcher.py - Watchdog-alapú mappafigyelő daemon.

Működése:
  - Regisztrálja az összes konfigurált WATCH_DIRS mappát.
  - Figyeli az új fájlok megjelenését és a másolás befejezését.
  - A stabilizált fájlokat átadja a FileProcessor pipeline-nak.
  - Graceful shutdown: SIGINT / SIGTERM (Ctrl+C) esetén tisztán leáll.

Fontos: Nagy fájloknál a 'created' esemény akkor is tüzelhet, mikor a fájl
még másolás alatt áll. Ezért file_size_stable() megvárja, míg a méret
nem változik két egymást követő ellenőrzés között.
"""

from __future__ import annotations

import time
import sys
import threading
from pathlib import Path
from queue import Queue, Empty

from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler,
    FileCreatedEvent,
    FileMovedEvent,
)

from ..logger import get_logger
from ..config import app1_config
from .file_processor import FileProcessor, SUPPORTED_EXTENSIONS

log = get_logger(__name__, level=app1_config.log_level)

# Fájlméret-stabilitás ellenőrzési paraméterek
_STABILITY_CHECK_INTERVAL = 2.0   # másodperc ellenőrzések között
_STABILITY_REQUIRED_CHECKS = 3    # ennyi egymást követő egyező méret kell


class VideoEventHandler(FileSystemEventHandler):
    """
    Watchdog eseménykezelő: új / áthelyezett videofájlok észlelése.

    Az észlelt fájlokat egy Thread-safe Queue-ba tolja,
    amelyet egy külön munkaszál dolgoz fel.
    """

    def __init__(self, work_queue: Queue) -> None:
        super().__init__()
        self._queue = work_queue

    def on_created(self, event: FileCreatedEvent) -> None:
        """Új fájl létrehozásakor hívódik meg."""
        if not event.is_directory:
            self._enqueue(Path(event.src_path))

    def on_moved(self, event: FileMovedEvent) -> None:
        """Fájl áthelyezésekor / átnevezésekor hívódik meg (pl. torrent kész)."""
        if not event.is_directory:
            self._enqueue(Path(event.dest_path))

    def _enqueue(self, path: Path) -> None:
        """Ellenőrzi a kiterjesztést, majd beteszi a sorba."""
        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            log.info("📂 Új fájl észlelve, sorba helyezve: '%s'", path.name)
            self._queue.put(path)


class WatcherDaemon:
    """
    Főszervező osztály: kezeli az Observer(eke)t és a feldolgozási munkaszálat.

    Indítás:  WatcherDaemon().start()
    Leállás:  Ctrl+C (SIGINT) vagy SIGTERM jel.
    """

    def __init__(self) -> None:
        self._queue: Queue[Path] = Queue()
        self._processor = FileProcessor()
        self._observer = Observer()
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Publikus API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Elindítja a watchert és a feldolgozó munkaszálat, majd blokkolja a hívót."""
        self._setup_observers()

        worker_thread = threading.Thread(
            target=self._worker_loop,
            name="FileProcessorWorker",
            daemon=True,
        )
        worker_thread.start()

        self._observer.start()
        log.info("🚀 Mappafigyelő elindult. Állítsd le: Ctrl+C")
        log.info("Figyelt mappák:")
        for d in app1_config.watch_dirs:
            log.info("  → %s", d)

        try:
            while not self._stop_event.is_set():
                time.sleep(1)
        finally:
            self._shutdown(worker_thread)

    def scan_existing(self) -> None:
        """
        Induláskor feldolgozza a már létező (korábban feldolgozatlan) fájlokat.
        Hasznos, ha a daemon offline volt, mikor egy fájl megérkezett.
        """
        log.info("Már meglévő fájlok átvizsgálása...")
        for watch_dir_str in app1_config.watch_dirs:
            watch_dir = Path(watch_dir_str.strip())
            if not watch_dir.is_dir():
                log.warning("A mappa nem létezik, kihagyjuk: '%s'", watch_dir)
                continue
            for ext in SUPPORTED_EXTENSIONS:
                for video_file in watch_dir.rglob(f"*{ext}"):
                    self._queue.put(video_file)
        log.info("Meglévő fájlok sorba helyezve a feldolgozáshoz.")

    # ------------------------------------------------------------------
    # Belső metódusok
    # ------------------------------------------------------------------

    def _setup_observers(self) -> None:
        """Beregisztrálja a figyelt mappákat az Observer-be."""
        handler = VideoEventHandler(self._queue)
        registered = 0

        for watch_dir_str in app1_config.watch_dirs:
            watch_dir = Path(watch_dir_str.strip())

            if not watch_dir_str.strip():
                continue

            if not watch_dir.is_dir():
                log.warning(
                    "Figyelt mappa nem található, kihagyjuk: '%s'",
                    watch_dir,
                )
                continue

            self._observer.schedule(handler, str(watch_dir), recursive=True)
            registered += 1
            log.debug("Mappa regisztrálva: '%s'", watch_dir)

        if registered == 0:
            log.error(
                "Egyetlen érvényes WATCH_DIRS mappa sem lett regisztrálva! "
                "Ellenőrizd a .env fájlt."
            )
            sys.exit(1)

    def _worker_loop(self) -> None:
        """
        Háttér-munkaszál: kiveszi a fájlokat a sorból és feldolgozza őket.
        Megvárja, amíg a fájl teljesen bemásolódik (méret-stabilizáció).
        """
        log.debug("Feldolgozó munkaszál elindult.")
        while not self._stop_event.is_set():
            try:
                video_path = self._queue.get(timeout=1.0)
            except Empty:
                continue

            try:
                log.debug("Fájl-méret stabilitás ellenőrzése: '%s'", video_path.name)
                _wait_for_file_stable(video_path)
                self._processor.process(video_path)
            except Exception as exc:
                log.exception(
                    "Váratlan hiba a munkaszálban '%s': %s",
                    video_path.name,
                    exc,
                )
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


# ---------------------------------------------------------------------------
# Segédfüggvény: fájlméret-stabilitás
# ---------------------------------------------------------------------------

def _wait_for_file_stable(
    path: Path,
    check_interval: float = _STABILITY_CHECK_INTERVAL,
    required_checks: int = _STABILITY_REQUIRED_CHECKS,
) -> None:
    """
    Megvárja, amíg a fájl mérete nem változik több egymást követő ellenőrzésen át.
    Megakadályozza, hogy félig másolt fájlon kezdjen dolgozni a pipeline.

    Args:
        path:            Az ellenőrizendő fájl útvonala.
        check_interval:  Ellenőrzések közötti várakozás (mp).
        required_checks: Ennyi egyező méretet vár egymás után.
    """
    stable_count = 0
    last_size = -1

    while stable_count < required_checks:
        try:
            current_size = path.stat().st_size
        except FileNotFoundError:
            log.warning("Fájl eltűnt a stabilitásellenőrzés során: '%s'", path.name)
            return

        if current_size == last_size:
            stable_count += 1
            log.debug(
                "Stabilitás: %d/%d (méret: %d bájt) '%s'",
                stable_count,
                required_checks,
                current_size,
                path.name,
            )
        else:
            stable_count = 0
            log.debug(
                "Méretváltozás észlelve (%d → %d bájt), visszaállítva: '%s'",
                last_size,
                current_size,
                path.name,
            )

        last_size = current_size
        time.sleep(check_interval)
