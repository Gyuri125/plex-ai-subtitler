"""
subtitle_extractor.py - Beágyazott feliratok vizsgálata és gyorskinyerése.

Felelősség:
  - FFmpeg segítségével ellenőrzi, hogy a konténer (.mkv, .mp4) tartalmaz-e
    beágyazott szöveges feliratsávot (SRT, SubRip, ASS/SSA, MOV_TEXT).
  - Ha talál angol feliratot, másodpercek alatt kinyeri .srt formátumba,
    kiváltva a hosszú és számításigényes Whisper STT folyamatot.
  - Ha már van beágyazott magyar felirat, közvetlenül kinyeri .hu.srt néven,
    így még LLM fordításra sincs szükség!
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
import imageio_ffmpeg

from ..logger import get_logger
from ..config import app1_config

log = get_logger(__name__, level=app1_config.log_level)

# Támogatott szöveg alapú feliratformátumok, amiket az FFmpeg srt-re tud konvertálni
TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}


def probe_subtitle_streams(video_path: Path) -> list[dict]:
    """
    Megvizsgálja a videofájlban található feliratsávokat az FFmpeg segítségével.

    Returns:
        List of dicts: [
            {"index": 2, "lang": "eng", "codec": "subrip", "title": "..."},
            ...
        ]
    """
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ffmpeg_exe, "-hide_banner", "-i", str(video_path)]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except Exception as exc:
        log.warning("Nem sikerült megvizsgálni a videó feliratait ('%s'): %s", video_path.name, exc)
        return []

    output = proc.stderr
    streams = []

    # Minta keresése: Stream #0:2(eng): Subtitle: subrip (default)
    # vagy: Stream #0:3: Subtitle: mov_text
    pattern = re.compile(
        r"Stream #0:(\d+)(?:\(([\w\-]+)\))?.*?: Subtitle:\s*(\w+)",
        re.IGNORECASE,
    )

    for line in output.splitlines():
        match = pattern.search(line)
        if match:
            idx = int(match.group(1))
            lang = (match.group(2) or "und").lower()
            codec = match.group(3).lower()
            streams.append({
                "stream_index": idx,
                "lang": lang,
                "codec": codec,
                "is_text": codec in TEXT_SUBTITLE_CODECS,
            })

    log.debug("Talált feliratsávok ('%s'): %s", video_path.name, streams)
    return streams


def extract_single_subtitle_stream(video_path: Path, stream_index: int, output_srt: Path) -> bool:
    """
    Kinyeri az adott indexű feliratsávot SRT formátumban.
    """
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    output_srt.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg_exe,
        "-y",
        "-i", str(video_path),
        "-map", f"0:{stream_index}",
        "-c:s", "srt",
        str(output_srt),
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        if result.returncode == 0 and output_srt.exists() and output_srt.stat().st_size > 100:
            return True
    except Exception as exc:
        log.warning("Hiba a feliratsáv (Stream 0:%d) kinyerésekor: %s", stream_index, exc)

    if output_srt.exists() and output_srt.stat().st_size <= 100:
        output_srt.unlink(missing_ok=True)
    return False


def try_extract_embedded_subtitles(video_path: Path) -> tuple[Path | None, bool]:
    """
    Megkísérli a beágyazott feliratok kinyerését.

    Returns:
        tuple (srt_path, is_already_hungarian):
          - (Path, True)  -> Közvetlenül magyar felirat lett kinyerve (kész a folyamat!)
          - (Path, False) -> Angol felirat lett kinyerve (csak fordításra vár!)
          - (None, False) -> Nem található alkalmas beágyazott felirat (Whisper szükséges)
    """
    streams = probe_subtitle_streams(video_path)
    if not streams:
        return None, False

    parent = video_path.parent
    stem = video_path.stem

    # 1. Van-e már kész magyar szöveges felirat a videóban?
    hu_targets = ["hun", "hu", "hungarian"]
    for s in streams:
        if s["is_text"] and any(s["lang"].startswith(h) for h in hu_targets):
            hu_path = parent / f"{stem}.hu.srt"
            log.info("🎯 Beágyazott MAGYAR feliratsáv észlelve (Stream 0:%d)!", s["stream_index"])
            if extract_single_subtitle_stream(video_path, s["stream_index"], hu_path):
                log.info("⚡ Sikeresen kinyerve a magyar felirat: '%s' (0.5 mp alatt!)", hu_path.name)
                return hu_path, True

    # 2. Van-e beágyazott angol (vagy forrásnyelvű) szöveges felirat?
    en_targets = [lang.lower() for lang in app1_config.embedded_sub_languages]
    for s in streams:
        if s["is_text"] and any(s["lang"].startswith(e) for e in en_targets):
            en_path = parent / f"{stem}.srt"
            log.info("⚡ Beágyazott forrásnyelvű (%s) feliratsáv észlelve (Stream 0:%d)!", s["lang"], s["stream_index"])
            if extract_single_subtitle_stream(video_path, s["stream_index"], en_path):
                log.info("✅ Beágyazott felirat kinyerve: '%s'. Whisper STT kihagyva!", en_path.name)
                return en_path, False

    # 3. Fallback: Ha van olyan szöveges feliratsáv, aminek a nyelve ismeretlen (und), és csak 1 van
    text_streams = [s for s in streams if s["is_text"]]
    if len(text_streams) == 1 and text_streams[0]["lang"] == "und":
        s = text_streams[0]
        en_path = parent / f"{stem}.srt"
        log.info("⚡ Ismeretlen nyelvű (und), egyetlen beágyazott felirat kinyerése (Stream 0:%d)...", s["stream_index"])
        if extract_single_subtitle_stream(video_path, s["stream_index"], en_path):
            log.info("✅ Beágyazott felirat kinyerve: '%s'. Whisper STT kihagyva!", en_path.name)
            return en_path, False

    return None, False
