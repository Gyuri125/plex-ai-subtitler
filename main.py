"""main.py - A Plex Subtitler alkalmazás belépési pontja (GUI, 1-Click, Szinkron-ellenőrző, Hardver-felmérés és CLI)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from pathlib import Path

# Globális crash- és natív faulthandler naplózás legelőször, még a nehéz modulok importja előtt!
from plex_subtitler.logger import (
    flush_all_logs,
    get_log_file_path,
    get_logger,
    install_global_exception_logging,
)

install_global_exception_logging()

from plex_subtitler import __version__  # noqa: E402
from plex_subtitler.app1_extractor.file_processor import FileProcessor  # noqa: E402
from plex_subtitler.app1_extractor.gui import (  # noqa: E402
    run_batch_one_click,
    run_gui,
    run_quick_mode,
)
from plex_subtitler.app1_extractor.hardware_manager import (  # noqa: E402
    HardwareManager,
    run_first_start_hardware_setup,
)
from plex_subtitler.app1_extractor.watcher import WatcherDaemon  # noqa: E402
from plex_subtitler.app2_translator.plex_client import refresh_plex_library_for_file  # noqa: E402
from plex_subtitler.app2_translator.srt_formatter import shift_srt_file  # noqa: E402
from plex_subtitler.app2_translator.sync_verifier import auto_align_subtitle, verify_subtitle_sync  # noqa: E402
from plex_subtitler.app2_translator.translator_daemon import (  # noqa: E402
    TranslatorDaemon,
    process_single_srt,
)
from plex_subtitler.config import app1_config, app2_config  # noqa: E402

log = get_logger("plex_subtitler_main", level=app1_config.log_level)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="plex-subtitler",
        description=(
            "Plex Subtitler - Extractor & AI Translator\n"
            "Videófájlokból vagy sorozat-mappákból feliratot nyer ki, és AI-val lefordítja."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input_paths",
        nargs="*",
        help="Opcionális videó(k), SRT fájl(ok) vagy sorozat mappa (ikonra húzáskor gyors módban indul).",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Indítás az egyszerűsített 1-Click Gyors mód ablakban.",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Futás parancssori (headless) módban GUI nélkül.",
    )
    parser.add_argument(
        "--app",
        choices=("extractor", "translator", "both"),
        default="extractor",
        help="Melyik daemon fusson --cli módban (alapértelmezett: extractor).",
    )
    parser.add_argument(
        "--scan-existing",
        action="store_true",
        help="Induláskor feldolgozza a már létező, feldolgozatlan fájlokat.",
    )
    parser.add_argument(
        "--once",
        metavar="VIDEO_PATH",
        help="Egyetlen videófájl feldolgozása és kilépés.",
    )
    parser.add_argument(
        "--translate-srt",
        metavar="SRT_PATH",
        help="Egyetlen SRT fájl lefordítása és kilépés.",
    )
    parser.add_argument(
        "--shift-srt",
        nargs=2,
        metavar=("SRT_PATH", "OFFSET_MS"),
        help="Eltolja egy SRT fájl időzítését ±OFFSET_MS ezredmásodperccel.",
    )
    parser.add_argument(
        "--verify-sync",
        nargs=2,
        metavar=("VIDEO_PATH", "SRT_PATH"),
        help="Ellenőrzi a videó és az SRT felirat szinkronját.",
    )
    parser.add_argument(
        "--auto-align",
        nargs=2,
        metavar=("VIDEO_PATH", "SRT_PATH"),
        help="Automatikusan kijavítja egy felirat egyenletes elcsúszását a videó hangsávja alapján.",
    )
    parser.add_argument(
        "--refresh-plex",
        metavar="FILE_PATH",
        help="Frissíti a Plex Media Server megfelelő könyvtárát a megadott fájl mappájára.",
    )
    parser.add_argument(
        "--hardware-scan",
        action="store_true",
        help="Előzetes hardver- és függőség-felmérés futtatása JSON kimenettel és kilépés.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args()


def _print_banner() -> None:
    print(
        f"╔══════════════════════════════════════════════════════════╗\n"
        f"║     🎬  Plex Subtitler — Extractor & Translator  🎙️     ║\n"
        f"║     Verzió: {__version__:<44} ║\n"
        f"║     Whisper: {app1_config.whisper_model:<18} Eszköz: {app1_config.whisper_device:<17} ║\n"
        f"║     Fordító: {app2_config.llm_provider:<18} Célnyelv: {app2_config.target_language:<15} ║\n"
        f"╚══════════════════════════════════════════════════════════╝\n"
    )


def _dispatch(args: argparse.Namespace) -> None:
    if args.hardware_scan:
        hw = HardwareManager()
        inv = hw.collect_system_inventory()
        payload = {
            "inventory": inv.to_dict(),
            "recommended_profile": hw.get_full_recommended_profile(),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        log.info("Hardver-felmérés befejeződött: profil=%s", inv.profile_tier)
        return

    # Első indításkor (amikor még nincs settings.json) automatikus hardver-profilozás
    run_first_start_hardware_setup()

    if args.shift_srt:
        srt_path = Path(args.shift_srt[0])
        offset_ms = int(args.shift_srt[1])
        if not srt_path.exists():
            log.error("A megadott SRT fájl nem létezik: '%s'", srt_path)
            sys.exit(1)
        out = shift_srt_file(srt_path, offset_ms)
        log.info("⏱️ Időzítés eltolva (%+d ms): '%s'", offset_ms, out)
        return

    if args.verify_sync:
        vid_path = Path(args.verify_sync[0])
        srt_path = Path(args.verify_sync[1])
        report = verify_subtitle_sync(vid_path, srt_path)
        log.info(
            "🔍 Szinkron: %s | Videó: %.1fs | Blokkok: %d | Túllógó: %d | Átfedő: %d | Hang-egyezés: %s",
            "RENDBEN" if report.in_sync else "HIBA",
            report.video_duration_ms / 1000.0,
            report.subtitle_count,
            report.out_of_bounds_count,
            report.overlap_count,
            f"{report.speech_alignment_ratio:.0%}" if report.speech_alignment_ratio is not None else "N/A",
        )
        if not report.in_sync:
            for issue in report.issues:
                log.error("  - %s", issue)
            sys.exit(1)
        return

    if args.auto_align:
        vid_path = Path(args.auto_align[0])
        srt_path = Path(args.auto_align[1])
        applied_ms, out_path, report = auto_align_subtitle(vid_path, srt_path)
        log.info(
            "🔧 Auto-align eredmény: %+d ms | Szinkron: %s | Kimenet: '%s'",
            applied_ms,
            "RENDBEN" if report.in_sync else "ELTÉRÉS MARADT",
            out_path,
        )
        if not report.in_sync:
            sys.exit(1)
        return

    if args.refresh_plex:
        ok, msg = refresh_plex_library_for_file(Path(args.refresh_plex))
        if ok:
            log.info("✅ %s", msg)
        else:
            log.error("❌ %s", msg)
            sys.exit(1)
        return

    if args.once:
        video_path = Path(args.once)
        if not video_path.exists():
            log.error("A megadott videófájl nem létezik: '%s'", video_path)
            sys.exit(1)
        log.info("Egyszeri videó-feldolgozás: '%s'", video_path.name)
        srt_path = FileProcessor().process(video_path)
        if srt_path:
            log.info("✅ Eredmény: '%s'", srt_path)
            if app1_config.auto_translate_after_extract:
                translated = process_single_srt(srt_path)
                if translated:
                    log.info("✅ Lefordított eredmény: '%s'", translated)
        else:
            log.error("❌ A feldolgozás sikertelen.")
            sys.exit(1)
        return

    if args.translate_srt:
        srt_path = Path(args.translate_srt)
        if not srt_path.exists():
            log.error("A megadott SRT fájl nem létezik: '%s'", srt_path)
            sys.exit(1)
        translated = process_single_srt(srt_path)
        if translated:
            log.info("✅ Lefordított SRT: '%s'", translated)
        else:
            log.error("❌ A fordítás sikertelen.")
            sys.exit(1)
        return

    if args.cli:
        if args.input_paths:
            res = run_batch_one_click([Path(p) for p in args.input_paths])
            if not res:
                log.info("Nem volt új feldolgozandó elem (minden kész vagy üres).")
            return
    else:
        if args.quick or args.input_paths:
            init_paths = [Path(p) for p in args.input_paths] if args.input_paths else None
            run_quick_mode(initial_paths=init_paths)
            return
        run_gui()
        return

    _print_banner()

    if args.app == "extractor":
        daemon = WatcherDaemon()
        if args.scan_existing:
            daemon.scan_existing()
        daemon.start()
    elif args.app == "translator":
        daemon = TranslatorDaemon()
        if args.scan_existing:
            daemon.scan_existing()
        daemon.start()
    else:
        ext = WatcherDaemon()
        tr = TranslatorDaemon()
        if args.scan_existing:
            ext.scan_existing()
            tr.scan_existing()
        tr_thread = threading.Thread(target=tr.start, name="TranslatorDaemon", daemon=True)
        tr_thread.start()
        try:
            ext.start()
        finally:
            tr.stop()
            tr_thread.join(timeout=10)


def _redirect_console_logs_to_stderr() -> None:
    """JSON-kimenetű CLI parancsoknál a konzollogokat stderr-re tereli, hogy a stdout tiszta JSON maradjon."""
    for obj in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(obj, logging.Logger):
            for h in obj.handlers:
                if type(h) is logging.StreamHandler:
                    h.stream = sys.stderr


def main() -> None:
    if "--hardware-scan" in sys.argv:
        _redirect_console_logs_to_stderr()
    log.info(
        "=== Plex Subtitler v%s indítás | argv=%s | Naplófájl='%s' ===",
        __version__,
        sys.argv[1:],
        get_log_file_path(),
    )
    try:
        args = _parse_args()
        _dispatch(args)
    except SystemExit:
        raise
    except Exception:
        log.critical("Végzetes, kezeletlen hiba a fő folyamatban:", exc_info=True)
        flush_all_logs()
        raise
    finally:
        log.info("=== Plex Subtitler v%s leállt ===", __version__)
        flush_all_logs()
        logging.shutdown()


if __name__ == "__main__":
    main()
