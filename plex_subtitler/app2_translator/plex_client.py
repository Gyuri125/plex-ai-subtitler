"""Plex Media Server API kliens a felirat-könyvtárak automatikus frissítéséhez."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import requests

from plex_subtitler.config import app2_config
from plex_subtitler.logger import get_logger

log = get_logger(__name__, level=app2_config.log_level)


def _normalize_path_for_compare(path_str: str) -> str:
    cleaned = os.path.normpath(path_str.strip()).rstrip("\\/")
    return cleaned.lower()


def _find_matching_section(
    sections: list[dict],
    target_dir: Path,
) -> tuple[str | None, str | None]:
    """Kikeresi azt a Plex szekciót (kulcs, cím), amelynek elérési útja tartalmazza a célmappát."""
    norm_target = _normalize_path_for_compare(str(target_dir.resolve()))
    best_key: str | None = None
    best_title: str | None = None
    best_len = -1

    for sec in sections:
        key = str(sec.get("key", "")).strip()
        title = str(sec.get("title", key))
        for loc in sec.get("Location", []) or []:
            loc_path = str(loc.get("path", "")).strip()
            if not loc_path:
                continue
            norm_loc = _normalize_path_for_compare(loc_path)
            if norm_target == norm_loc or norm_target.startswith(norm_loc + os.sep.lower()):
                if len(norm_loc) > best_len:
                    best_len = len(norm_loc)
                    best_key = key
                    best_title = title
    return best_key, best_title


def fetch_plex_sections(
    plex_url: str,
    plex_token: str,
    timeout: float = 8.0,
) -> list[dict]:
    """Lekéri a Plex Media Server összes könyvtárszekcióját."""
    base = plex_url.strip().rstrip("/")
    headers = {"Accept": "application/json", "X-Plex-Token": plex_token.strip()}
    resp = requests.get(f"{base}/library/sections", headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return list(data.get("MediaContainer", {}).get("Directory", []) or [])


def test_plex_connection(plex_url: str, plex_token: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Ellenőrzi a Plex szerver elérhetőségét és listázza a megtalált könyvtárakat."""
    if not plex_url.strip() or not plex_token.strip():
        return False, "Add meg a Plex URL-t és a Plex Tokent is!"
    try:
        sections = fetch_plex_sections(plex_url, plex_token, timeout=timeout)
        names = [str(s.get("title", s.get("key", "?"))) for s in sections]
        if names:
            return True, f"Plex kapcsolat OK! Könyvtárak: {', '.join(names)}"
        return True, "Plex kapcsolat OK, de nem található könyvtárszekció."
    except Exception as exc:
        return False, f"Plex kapcsolat sikertelen: {exc}"


def refresh_plex_library_for_file(
    file_path: Path,
    plex_url: str | None = None,
    plex_token: str | None = None,
    timeout: float = 8.0,
) -> tuple[bool, str]:
    """Frissíti a Plex Media Server megfelelő könyvtárát a megadott felirat mappájára."""
    url = (plex_url if plex_url is not None else app2_config.plex_url).strip().rstrip("/")
    token = (plex_token if plex_token is not None else app2_config.plex_token).strip()
    if not url or not token:
        return False, "Plex URL vagy Token hiányzik."

    file_path = Path(file_path)
    target_dir = file_path.parent
    headers = {"Accept": "application/json", "X-Plex-Token": token}

    try:
        sections = fetch_plex_sections(url, token, timeout=timeout)
        sec_key, sec_title = _find_matching_section(sections, target_dir)

        if sec_key:
            encoded_dir = quote(str(target_dir.resolve()), safe="")
            refresh_url = f"{url}/library/sections/{sec_key}/refresh?path={encoded_dir}"
            resp = requests.get(refresh_url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            msg = f"Plex könyvtár frissítve ('{sec_title}', mappa: '{target_dir.name}')."
            log.info("📺 %s", msg)
            return True, msg

        # Ha az útvonal pl. Docker/NAS eltérő mount miatt nem egyezett, frissítsük a movie/show szekciókat
        refreshed: list[str] = []
        for sec in sections:
            if sec.get("type") in ("movie", "show"):
                key = str(sec.get("key", "")).strip()
                if key:
                    resp = requests.get(
                        f"{url}/library/sections/{key}/refresh",
                        headers=headers,
                        timeout=timeout,
                    )
                    resp.raise_for_status()
                    refreshed.append(str(sec.get("title", key)))
        if refreshed:
            msg = f"Plex szekciók frissítve ({', '.join(refreshed)})."
            log.info("📺 %s", msg)
            return True, msg
        return False, "Nem található frissíthető film/sorozat Plex szekció."
    except Exception as exc:
        msg = f"Plex frissítés sikertelen ({exc})"
        log.warning("⚠️ %s", msg)
        return False, msg
