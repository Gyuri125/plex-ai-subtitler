"""Egységes, színezett konzol- és fájlnaplózási konfiguráció mindkét alkalmazáshoz.

Garanciák:
- 3-szintű írható mappa fallback (<app>/logs -> %LOCALAPPDATA%/PlexSubtitler/logs -> %TEMP%/PlexSubtitler/logs),
  így írásvédett telepítési könyvtár esetén sem marad el soha a fájlnaplózás.
- Globális kivétel-elfogás: sys.excepthook, threading.excepthook és faulthandler (natív C/CUDA összeomlásokhoz).
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import tempfile
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO

if getattr(sys, "frozen", False):
    _DEFAULT_LOG_DIR = Path(sys.executable).resolve().parent / "logs"
else:
    _DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

_ACTIVE_LOG_DIR: Path | None = None
_SHARED_FILE_HANDLER: logging.Handler | None = None
_FAULT_LOG_STREAM: IO[str] | None = None


class _ColorFormatter(logging.Formatter):
    """Szín-kódolt konzolformázó naplózási szintekhez."""

    GREY = "\x1b[38;5;246m"
    CYAN = "\x1b[36m"
    YELLOW = "\x1b[33m"
    RED = "\x1b[31m"
    BOLD_RED = "\x1b[1;31m"
    RESET = "\x1b[0m"

    FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

    LEVEL_COLORS = {
        logging.DEBUG: GREY,
        logging.INFO: CYAN,
        logging.WARNING: YELLOW,
        logging.ERROR: RED,
        logging.CRITICAL: BOLD_RED,
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelno, self.RESET)
        formatter = logging.Formatter(
            f"{color}{self.FORMAT}{self.RESET}",
            datefmt=self.DATE_FORMAT,
        )
        return formatter.format(record)


def _candidate_log_dirs(preferred: Path | None = None) -> list[Path]:
    """Visszaadja a naplómappa-jelölteket prioritási sorrendben."""
    candidates: list[Path] = []
    if preferred is not None:
        candidates.append(Path(preferred))
    candidates.append(_DEFAULT_LOG_DIR)

    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        candidates.append(Path(local_app) / "PlexSubtitler" / "logs")
    else:
        candidates.append(Path.home() / ".plex_subtitler" / "logs")

    candidates.append(Path(tempfile.gettempdir()) / "PlexSubtitler" / "logs")
    return candidates


def resolve_writable_log_dir(preferred: Path | None = None) -> Path:
    """Megkeresi az első valóban írható naplómappát (írásvédett mappa esetén automatikus fallback)."""
    global _ACTIVE_LOG_DIR
    for cand in _candidate_log_dirs(preferred):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            if preferred is None:
                _ACTIVE_LOG_DIR = cand
            return cand
        except OSError:
            continue
    # Végső esetben a rendszer temp könyvtára
    fallback = Path(tempfile.gettempdir())
    if preferred is None:
        _ACTIVE_LOG_DIR = fallback
    return fallback


def get_active_log_dir(log_dir: str | Path | None = None) -> Path:
    """Visszaadja az aktuálisan használt, írható naplókönyvtárat."""
    if log_dir is not None:
        return resolve_writable_log_dir(Path(log_dir))
    if _ACTIVE_LOG_DIR is not None:
        return _ACTIVE_LOG_DIR
    return resolve_writable_log_dir()


def get_log_file_path(log_dir: str | Path | None = None) -> Path:
    """Visszaadja a fő futási naplófájl útvonalát."""
    return get_active_log_dir(log_dir) / "plex_subtitler.log"


def _create_rotating_handler(log_file: Path, max_bytes: int, backup_count: int) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(_ColorFormatter.FORMAT, datefmt=_ColorFormatter.DATE_FORMAT)
    )
    return handler


def _get_shared_file_handler(log_dir: Path | None = None) -> logging.Handler | None:
    global _SHARED_FILE_HANDLER
    if log_dir is not None:
        target_dir = resolve_writable_log_dir(Path(log_dir))
        try:
            return _create_rotating_handler(target_dir / "plex_subtitler.log", 10 * 1024 * 1024, 5)
        except OSError:
            return None

    if _SHARED_FILE_HANDLER is None:
        for cand in _candidate_log_dirs():
            try:
                cand.mkdir(parents=True, exist_ok=True)
                handler = _create_rotating_handler(cand / "plex_subtitler.log", 10 * 1024 * 1024, 5)
                global _ACTIVE_LOG_DIR
                _ACTIVE_LOG_DIR = cand
                _SHARED_FILE_HANDLER = handler
                break
            except OSError:
                continue
    return _SHARED_FILE_HANDLER


def flush_all_logs() -> None:
    """Azonnal lemezre üríti az összes aktív naplókezelő pufferét."""
    if _SHARED_FILE_HANDLER is not None:
        try:
            _SHARED_FILE_HANDLER.flush()
        except Exception:
            pass
    for lgr in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(lgr, logging.Logger):
            for h in list(lgr.handlers):
                try:
                    h.flush()
                except Exception:
                    pass


def get_logger(
    name: str,
    level: str = "INFO",
    log_dir: str | Path | None = None,
) -> logging.Logger:
    """Létrehoz vagy visszaad egy konfigurált Logger példányt (konzol + fájl)."""
    logger = logging.getLogger(name)
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric_level)

    if logger.handlers:
        return logger

    if sys.stdout is not None:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(_ColorFormatter())
        logger.addHandler(stream_handler)

    if log_dir is not None:
        target_dir = resolve_writable_log_dir(Path(log_dir))
        safe_name = name.split(".")[-1] or "plex_subtitler"
        try:
            custom_fh = _create_rotating_handler(target_dir / f"{safe_name}.log", 5 * 1024 * 1024, 3)
            logger.addHandler(custom_fh)
        except OSError:
            pass
    else:
        shared_fh = _get_shared_file_handler()
        if shared_fh is not None:
            logger.addHandler(shared_fh)

    logger.propagate = False
    return logger


def install_global_exception_logging() -> None:
    """Biztosítja, hogy a nem kezelt fő-, háttérszálbeli és natív (C/CUDA) összeomlások is naplózásra kerüljenek."""
    global _FAULT_LOG_STREAM
    crash_log = get_logger("plex_subtitler.crash")

    # Natív C / CUDA segfault naplózás (faulthandler)
    if _FAULT_LOG_STREAM is None:
        try:
            fault_path = get_active_log_dir() / "crash_native.log"
            _FAULT_LOG_STREAM = open(fault_path, "a", encoding="utf-8")
            faulthandler.enable(file=_FAULT_LOG_STREAM, all_threads=True)
        except Exception:
            pass

    def _sys_hook(exc_type, exc_value, exc_traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        crash_log.critical("Kezeletlen kivétel (main):", exc_info=(exc_type, exc_value, exc_traceback))
        flush_all_logs()

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        crash_log.critical(
            "Kezeletlen kivétel (thread=%s):",
            getattr(args.thread, "name", "unknown"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        flush_all_logs()

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
