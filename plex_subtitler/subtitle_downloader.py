"""
subtitle_downloader.py - Online feliratletöltő modul (OpenSubtitles REST API / Hash).

Felelősség:
  - Videófájl 64-bites OSdb hash-ének kiszámítása.
  - Elérhető magyar vagy angol felirat lekérése és közvetlen letöltése a Whisper előtt.
  - Ha van kész magyar felirat (.hu.srt): a transzkripció és fordítás is átugorható.
  - Ha van angol felirat (.srt): a Whisper átugorható, azonnal mehet az LLM fordítás.
"""

from __future__ import annotations

import struct
import re
from pathlib import Path
import requests

from .logger import get_logger
from .config import app1_config

log = get_logger(__name__, level=app1_config.log_level)

OPENSUBTITLES_API_URL = "https://api.opensubtitles.com/api/v1"
DEFAULT_USER_AGENT = "PlexSubtitler v2.0"


def compute_movie_hash(file_path: Path) -> tuple[str, int]:
    """
    Kiszámítja a videófájl szabványos OpenSubtitles 64-bites ellenőrzőösszegét (moviehash).
    A fájl első és utolsó 64 kilobájtját olvassa be, és összeadja a 64-bites egészeket.
    """
    longlongformat = '<q'  # little-endian 64-bit signed int
    bytesize = struct.calcsize(longlongformat)

    try:
        filesize = file_path.stat().st_size
        if filesize < 65536 * 2:
            return "", filesize

        hash_val = filesize

        with open(file_path, "rb") as f:
            for _ in range(65536 // bytesize):
                buf = f.read(bytesize)
                (val,) = struct.unpack(longlongformat, buf)
                hash_val = (hash_val + val) & 0xFFFFFFFFFFFFFFFF

            f.seek(max(0, filesize - 65536), 0)
            for _ in range(65536 // bytesize):
                buf = f.read(bytesize)
                (val,) = struct.unpack(longlongformat, buf)
                hash_val = (hash_val + val) & 0xFFFFFFFFFFFFFFFF

        return f"{hash_val:016x}", filesize
    except Exception as exc:
        log.warning("Nem sikerült kiszámítani a moviehash-t (%s): %s", file_path.name, exc)
        return "", 0


def clean_title_from_filename(filename: str) -> str:
    """
    Eltávolítja a tipikus release tag-eket és kiterjesztést a fájlnévből a kereséshez.
    """
    stem = Path(filename).stem
    # Pontok, aláhúzások cseréje szóközre
    clean = re.sub(r'[\._]', ' ', stem)
    # Gyakori release szavak és minőségjelzők eltávolítása
    clean = re.sub(
        r'\b(1080p|720p|2160p|4k|bluray|bdrip|web-dl|webrip|dvdrip|x264|x265|hevc|aac|dts|remux)\b.*$',
        '',
        clean,
        flags=re.IGNORECASE
    )
    return clean.strip()


def try_download_subtitles(video_path: Path) -> tuple[Path | None, bool]:
    """
    Megkísérli letölteni a feliratot az OpenSubtitles REST API-ról.

    Returns:
        tuple (letöltött_srt_path vagy None, is_hungarian: bool)
    """
    if not app1_config.download_online_subtitles:
        return None, False

    movie_hash, filesize = compute_movie_hash(video_path)
    clean_title = clean_title_from_filename(video_path.name)

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json",
    }
    if app1_config.opensubtitles_api_key:
        headers["Api-Key"] = app1_config.opensubtitles_api_key

    # 1. Keresés magyar feliratra
    hu_target = video_path.parent / f"{video_path.stem}.hu.srt"
    if not hu_target.exists():
        log.info("Online felirat keresese (magyar): '%s'...", video_path.name)
        hu_srt = _search_and_fetch(headers, movie_hash, clean_title, "hu", hu_target)
        if hu_srt:
            log.info("Kesz magyar felirat letoltve az internetrol: '%s'. Whisper es forditas atugorva.", hu_srt.name)
            return hu_srt, True

    # 2. Keresés angol feliratra (ha magyar nem volt)
    en_target = video_path.parent / f"{video_path.stem}.srt"
    if not en_target.exists():
        log.info("Online felirat keresese (angol): '%s'...", video_path.name)
        en_srt = _search_and_fetch(headers, movie_hash, clean_title, "en", en_target)
        if en_srt:
            log.info("Angol felirat letoltve az internetrol: '%s'. Whisper atugorva, indul a forditas.", en_srt.name)
            return en_srt, False

    log.debug("Nem talalhato letoltheto felirat a neten a(z) '%s' fajlhoz.", video_path.name)
    return None, False


def _search_and_fetch(headers: dict, movie_hash: str, query: str, lang: str, output_path: Path) -> Path | None:
    """Segédfüggvény felirat keresésére és letöltésére."""
    url = f"{OPENSUBTITLES_API_URL}/subtitles"
    params: dict[str, str | int] = {"languages": lang}

    if movie_hash:
        params["moviehash"] = movie_hash
    elif query:
        params["query"] = query
    else:
        return None

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=6)
        if resp.status_code != 200:
            return None

        data = resp.json()
        items = data.get("data", [])
        if not items:
            # Ha hash-sel nem volt találat, próbáljuk címmel
            if "moviehash" in params and query:
                params.pop("moviehash")
                params["query"] = query
                resp = requests.get(url, headers=headers, params=params, timeout=6)
                if resp.status_code == 200:
                    items = resp.json().get("data", [])

        if not items:
            return None

        # Válasszuk az első vagy legmagasabb letöltésszámú feliratot
        best_item = items[0]
        files = best_item.get("attributes", {}).get("files", [])
        if not files:
            return None

        file_id = files[0].get("file_id")
        if not file_id:
            return None

        # Letöltési link kérése
        dl_url = f"{OPENSUBTITLES_API_URL}/download"
        dl_resp = requests.post(dl_url, headers=headers, json={"file_id": file_id}, timeout=6)
        if dl_resp.status_code == 200:
            link = dl_resp.json().get("link")
            if link:
                content_resp = requests.get(link, timeout=10)
                if content_resp.status_code == 200:
                    with open(output_path, "wb") as f:
                        f.write(content_resp.content)
                    return output_path

    except Exception as exc:
        log.debug("Online keresesi hiba (%s, %s): %s", query, lang, exc)

    return None
