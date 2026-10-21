"""
logger.py - Egységes naplózási konfiguráció mindkét alkalmazáshoz.
Naplóz a konzolra (színezve) és fájlba egyidejűleg.
"""

import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler


# ANSI színkódok a konzolos naplózáshoz (Windows 10+ és Linux/macOS)
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


def get_logger(
    name: str,
    level: str = "INFO",
    log_dir: Path | None = None,
) -> logging.Logger:
    """
    Létrehoz vagy visszaad egy konfigurált Logger példányt.

    Args:
        name:    A naplózó neve (jellemzően __name__).
        level:   Naplózási szint sztringként (DEBUG, INFO, WARNING, ERROR).
        log_dir: Ha megadva, ebbe a mappába ír fájl-naplót is.

    Returns:
        Konfigurált logging.Logger példány.
    """
    logger = logging.getLogger(name)

    # Ne adjunk hozzá több handlert, ha már inicializálva lett
    if logger.handlers:
        return logger

    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric_level)

    # --- Konzol handler (stdout, színezett) ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(_ColorFormatter())
    logger.addHandler(console_handler)

    # --- Fájl handler (opcionális, rotáló) ---
    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{name.split('.')[-1]}.log"

        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(file_handler)

    return logger
