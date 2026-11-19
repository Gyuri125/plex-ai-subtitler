"""OpenSubtitles REST API kliens előzetes online felirat-kereséshez (moviehash + fájlnév)."""

from __future__ import annotations

import os
import struct
from pathlib import Path

from plex_subtitler.config import app1_config
from plex_subtitler.logger import get_logger

log = get_logger(__name__, level=app1_config.log_level)

_OPENSUBTITLES_API_BASE = "https://api.opensubtitles.com/api/v1"


def compute_opensubtitles_hash(video_path: Path) -> str:
    """Kiszámítja a szabványos 64-bites OpenSubtitles moviehash értéket."""
    longlongformat = "<q"
    bytesize = struct.calcsize(longlongformat)
    filesize = os.path.getsize(video_path)
    hash_val = filesize

    if filesize < 65536 * 2:
        raise ValueError("A fájl túl rövid az OpenSubtitles hash számításhoz.")

    with open(video_path, "rb") as f:
        for _ in range(65536 // bytesize):
            buf = f.read(bytesize)
            (l_value,) = struct.unpack(longlongformat, buf)
            hash_val = (hash_val + l_value) & 0xFFFFFFFFFFFFFFFF

        f.seek(max(0, filesize - 65536), 0)
        for _ in range(65536 // bytesize):
            buf = f.read(bytesize)
            (l_value,) = struct.unpack(longlongformat, buf)
            hash_val = (hash_val + l_value) & 0xFFFFFFFFFFFFFFFF

    return f"{hash_val:016x}"


def download_opensubtitles_srt(
    video_path: Path,
    output_srt_path: Path | None = None,
    languages: tuple[str, ...] = ("en",),
    api_key: str | None = None,
) -> Path | None:
    """Megpróbál meglévő SRT feliratot keresni és letölteni az OpenSubtitles API-ról."""
    key = (api_key or app1_config.opensubtitles_api_key or "").strip()
    if not key:
        log.debug("OpenSubtitles API kulcs nincs megadva, online keresés kihagyva.")
        return None

    import requests

    headers = {
        "Api-Key": key,
        "User-Agent": "PlexSubtitler v1.2",
        "Accept": "application/json",
    }

    params: dict[str, str] = {
        "languages": ",".join(languages),
        "query": video_path.stem,
    }
    try:
        params["moviehash"] = compute_opensubtitles_hash(video_path)
    except Exception:
        pass

    try:
        resp = requests.get(
            f"{_OPENSUBTITLES_API_BASE}/subtitles",
            headers=headers,
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        items = resp.json().get("data", [])
        if not items:
            log.info("🌐 OpenSubtitles: nem található kész felirat ('%s').", video_path.name)
            return None

        files = items[0].get("attributes", {}).get("files", [])
        if not files:
            return None
        file_id = files[0].get("file_id")
        if not file_id:
            return None

        dl_meta = requests.post(
            f"{_OPENSUBTITLES_API_BASE}/download",
            headers={**headers, "Content-Type": "application/json"},
            json={"file_id": file_id},
            timeout=10,
        )
        dl_meta.raise_for_status()
        link = dl_meta.json().get("link")
        if not link:
            return None

        sub_resp = requests.get(link, timeout=20)
        sub_resp.raise_for_status()

        target = output_srt_path or video_path.with_suffix(".srt")
        target.write_bytes(sub_resp.content)
        log.info("🌐 OpenSubtitles felirat letöltve: '%s' → '%s'", video_path.name, target.name)
        return target
    except Exception as exc:
        log.warning("🌐 OpenSubtitles lekérdezés sikertelen ('%s'): %s", video_path.name, exc)
        return None
