"""
notifier.py - Plex Media Server integráció és értesítésküldés (Discord, Telegram).

Felelősség:
  - Automatikus könyvtárfrissítés küldése a Plex Media Server felé,
    amint egy új felirat (.hu.srt) elkészül.
  - Opcionális push értesítések küldése Discord Webhookra vagy Telegramra.
"""

from __future__ import annotations

from pathlib import Path
import requests

from .logger import get_logger
from .config import app2_config

log = get_logger(__name__, level=app2_config.log_level)


def refresh_plex_library() -> bool:
    """
    Frissítési kérést küld a megadott Plex Media Servernek.
    """
    if not app2_config.plex_auto_refresh:
        return False

    plex_url = (app2_config.plex_url or "").strip().rstrip("/")
    plex_token = (app2_config.plex_token or "").strip()

    if not plex_url:
        log.warning("Plex Auto-Refresh engedélyezve van, de nincs megadva PLEX_URL.")
        return False

    refresh_url = f"{plex_url}/library/sections/all/refresh"
    headers = {}
    if plex_token:
        headers["X-Plex-Token"] = plex_token

    log.info("Plex konyvtarfrissites inditasa: %s", refresh_url)
    try:
        response = requests.get(refresh_url, headers=headers, timeout=5)
        if response.status_code in [200, 204]:
            log.info("Plex konyvtarfrissites sikeresen elkuldve. A felirat elerheto.")
            return True
        else:
            log.warning("Plex valaszkod: %s (%s)", response.status_code, response.text[:200])
            return False
    except Exception as exc:
        log.warning("Nem sikerult elerni a Plex Media Servert (%s): %s", plex_url, exc)
        return False


def send_discord_notification(title: str, message: str) -> bool:
    """
    Értesítést küld egy Discord Webhook URL-re.
    """
    webhook_url = (app2_config.discord_webhook_url or "").strip()
    if not webhook_url:
        return False

    payload = {
        "embeds": [
            {
                "title": f"Plex Subtitler: {title}",
                "description": message,
                "color": 15049741,  # Plex narancssárga szín (#E5A00D)
                "footer": {"text": "Plex AI Subtitler"},
            }
        ]
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=5)
        return resp.status_code in [200, 204]
    except Exception as exc:
        log.warning("Hiba a Discord értesítés küldésekor: %s", exc)
        return False


def send_telegram_notification(title: str, message: str) -> bool:
    """
    Értesítést küld a megadott Telegram Boton keresztül.
    """
    token = (app2_config.telegram_bot_token or "").strip()
    chat_id = (app2_config.telegram_chat_id or "").strip()
    if not token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = f"*{title}*\n\n{message}"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }

    try:
        resp = requests.post(url, json=payload, timeout=5)
        return resp.status_code == 200
    except Exception as exc:
        log.warning("Hiba a Telegram értesítés küldésekor: %s", exc)
        return False


def notify_translation_completed(srt_path: Path, block_count: int, elapsed_seconds: float) -> None:
    """
    Összesített értesítéskezelő a sikeres feliratfordítás után.
    """
    movie_name = srt_path.stem.replace(".hu", "")
    time_str = f"{elapsed_seconds:.1f} mp" if elapsed_seconds < 60 else f"{elapsed_seconds/60:.1f} perc"

    title = f"Felirat elkészült: {movie_name}"
    message = (
        f"A **{movie_name}** magyar felirata sikeresen lefordítva és elmentve!\n"
        f"- Blokkmennyiség: {block_count} blokk\n"
        f"- Fordítási idő: {time_str}\n"
        f"- Fájl: `{srt_path.name}`"
    )

    # 1. Plex értesítés és refresh
    refresh_plex_library()

    # 2. Push értesítések
    if app2_config.notifications_enabled:
        if app2_config.discord_webhook_url:
            send_discord_notification(title, message)
        if app2_config.telegram_bot_token and app2_config.telegram_chat_id:
            send_telegram_notification(title, message)
