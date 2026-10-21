"""
main.py - Az 1. alkalmazás belépési pontja (Audio Extractor & Whisper STT).

Futtatás:
    python -m plex_subtitler.app1_extractor.main
    python -m plex_subtitler.app1_extractor.main --scan-existing

Kapcsolók:
    --scan-existing   Induláskor átvizsgálja a figyelt mappákat, és feldolgozza
                      az összes még nem feldolgozott videofájlt.
    --once PATH       Egyetlen fájlt dolgoz fel és kilép (teszteléshez hasznos).
    --version         Verzióinformáció kiírása.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# A csomag gyökérkönyvtárát hozzáadjuk a sys.path-hoz, hogy
# közvetlen futtatáskor is működjön az importálás.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plex_subtitler.logger import get_logger
from plex_subtitler.config import app1_config
from plex_subtitler.app1_extractor.watcher import WatcherDaemon
from plex_subtitler.app1_extractor.file_processor import FileProcessor
from plex_subtitler.app1_extractor.gui import run_gui

__version__ = "1.0.0"

log = get_logger("app1_main", level=app1_config.log_level)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="plex-subtitler-extractor",
        description=(
            "Plex Subtitler - App 1: Audio Extractor & Whisper STT\n"
            "Figyeli a megadott mappákat, FFmpeg-gel kinyeri az audiot,\n"
            "majd Whisper segítségével SRT feliratot generál."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--cli",
        action="store_true",
        help="Futás parancssori (headless) módban GUI nélkül.",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Beépített webes dashboard indítása böngészős/NAS vezérléshez.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="A webes dashboard portja (alapértelmezett: 8080).",
    )
    parser.add_argument(
        "--scan-existing",
        action="store_true",
        help="Induláskor feldolgozza a már létező, feldolgozatlan fájlokat.",
    )
    parser.add_argument(
        "--once",
        metavar="VIDEO_PATH",
        help="Egyetlen fájl feldolgozása és kilépés (teszteléshez).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    return parser.parse_args()


def _print_banner() -> None:
    banner = f"""
============================================================
       Plex Subtitler - App 1: Extractor & STT
       Verzio: {__version__:<44}
       Whisper modell: {app1_config.whisper_model:<37}
       Eszkoz: {app1_config.whisper_device:<45}
============================================================
"""
    print(banner)


def main() -> None:
    args = _parse_args()
    
    # Opcionális webes dashboard indítása
    if args.web or app1_config.web_dashboard_enabled:
        from plex_subtitler.web import start_web_dashboard_thread
        port = args.port or app1_config.web_dashboard_port
        start_web_dashboard_thread(host=app1_config.web_dashboard_host, port=port)

    # Ha nincs megadva a --cli, --web vagy a --once flag, akkor induljon a GUI
    if not args.cli and not args.once and not args.web:
        run_gui()
        return

    _print_banner()

    # --- Egyszeri feldolgozás mód (--once) ---
    if args.once:
        video_path = Path(args.once)
        if not video_path.exists():
            log.error("A megadott fájl nem létezik: '%s'", video_path)
            sys.exit(1)

        log.info("Egyszeri feldolgozás mód: '%s'", video_path.name)
        processor = FileProcessor()
        result = processor.process(video_path)

        if result:
            log.info("Eredmeny: '%s'", result)
            sys.exit(0)
        else:
            log.error("A feldolgozas sikertelen.")
            sys.exit(1)

    # --- Daemon mód (folyamatos mappafigyelés) ---
    daemon = WatcherDaemon()

    if args.scan_existing:
        daemon.scan_existing()

    daemon.start()


if __name__ == "__main__":
    main()
