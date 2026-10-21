"""
sub_sync.py - Felirat időzítés-igazító modul (ffsubsync / alass integráció).

Felelősség:
  - SRT felirat időzítésének millimásodperc-pontos hozzáigazítása a videó hangsávjához.
  - Külső vagy eltérő forrásból származó feliratok elcsúszásának (desync) automatikus javítása.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .logger import get_logger
from .config import app1_config

log = get_logger(__name__, level=app1_config.log_level)


def is_sync_tool_available() -> bool:
    """Ellenőrzi, hogy elérhető-e az ffsubsync vagy az alass a rendszerben."""
    if shutil.which("ffsubsync"):
        return True
    try:
        import ffsubsync  # type: ignore[import]
        return True
    except ImportError:
        pass
    return False


def auto_sync_subtitle(video_path: Path, srt_path: Path, output_path: Path | None = None) -> bool:
    """
    Automatikusan szinkronizálja az SRT feliratot a videóban található beszédhanghoz.

    Args:
        video_path: A referencia videófájl útvonala.
        srt_path: A szinkronizálandó SRT feliratfájl útvonala.
        output_path: Opcionális kimeneti útvonal (alapértelmezésben felülírja a bemeneti fájlt).

    Returns:
        bool: True ha a szinkronizálás sikeresen lefutott, különben False.
    """
    if not app1_config.auto_sync_subtitles:
        return False

    if not is_sync_tool_available():
        log.info(
            "Auto-Sync engedelyezve van, de az 'ffsubsync' nincs telepitve a kornyezetben. "
            "Telepiteshez futtasd: pip install ffsubsync"
        )
        return False

    out_file = output_path or srt_path
    temp_synced = srt_path.with_name(f"{srt_path.stem}.synced.tmp.srt")

    log.info("Idozites szinkronizalasa indul (ffsubsync): '%s'...", srt_path.name)

    # Először parancssori eszközként próbáljuk futtatni
    cmd = []
    if shutil.which("ffsubsync"):
        cmd = ["ffsubsync", str(video_path), "-i", str(srt_path), "-o", str(temp_synced)]
    else:
        # Python modulként hívás
        cmd = [sys.executable, "-m", "ffsubsync", str(video_path), "-i", str(srt_path), "-o", str(temp_synced)]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,  # Max 3 perc nagyobb filmekhez
        )

        if result.returncode == 0 and temp_synced.exists() and temp_synced.stat().st_size > 0:
            if out_file == srt_path:
                temp_synced.replace(out_file)
            else:
                shutil.move(str(temp_synced), str(out_file))
            log.info("Sikeres idozites-szinkronizacio: '%s'", out_file.name)
            return True
        else:
            log.warning("ffsubsync hibakoddal tert vissza (%d): %s", result.returncode, result.stderr[:200])
    except subprocess.TimeoutExpired:
        log.warning("ffsubsync idotullepes miatt leallitva (%s).", srt_path.name)
    except Exception as exc:
        log.warning("Nem sikerult szinkronizalni a feliratot (%s): %s", srt_path.name, exc)
    finally:
        if temp_synced.exists():
            try:
                temp_synced.unlink()
            except OSError:
                pass

    return False
