"""Windows asztali Toast értesítés és csendes hangjelzés kész feliratoknál."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from plex_subtitler.config import app2_config
from plex_subtitler.logger import get_logger

log = get_logger(__name__, level=app2_config.log_level)


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def play_subtle_chime() -> bool:
    """Csendes Windows rendszerhangjelzés (hibatűrő, sosem akad meg)."""
    if sys.platform != "win32":
        return False
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_OK)
        return True
    except Exception:
        return False


def show_windows_toast(title: str, message: str, app_id: str = "Plex Subtitler") -> bool:
    """Megjelenít egy nem-blokkoló Windows 10/11 Toast értesítést WinRT-n keresztül."""
    if sys.platform != "win32":
        return False

    t_esc = _xml_escape(title)
    m_esc = _xml_escape(message)
    a_esc = _xml_escape(app_id)
    ps_script = (
        "$ErrorActionPreference = 'SilentlyContinue'; "
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null; "
        f"$xml = [Windows.Data.Xml.Dom.XmlDocument]::new(); "
        f"$xml.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>{t_esc}</text><text>{m_esc}</text></binding></visual></toast>'); "
        f"$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{a_esc}').Show($toast)"
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_script],
            capture_output=True,
            timeout=6,
            creationflags=creation_flags,
            check=False,
        )
        return res.returncode == 0
    except Exception as exc:
        log.debug("Windows Toast értesítés sikertelen: %s", exc)
        return False


def notify_translation_complete(
    movie_title: str,
    target_srt: Path,
    quality_summary: str = "",
) -> None:
    """Aszinkron értesítést küld kész felirat esetén (ha engedélyezve van)."""
    if not app2_config.desktop_notifications and not app2_config.notification_sound:
        return

    if app2_config.notification_sound:
        play_subtle_chime()

    if not app2_config.desktop_notifications:
        return

    body = f"A(z) {movie_title} ({target_srt.name}) kész — a Plexen hamarosan nézhető!"
    if quality_summary:
        body = f"{body}\n{quality_summary}"

    threading.Thread(
        target=show_windows_toast,
        args=("🎬 Plex Subtitler — Felirat elkészült!", body),
        daemon=True,
    ).start()
