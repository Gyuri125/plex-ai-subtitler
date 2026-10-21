"""
series_memory.py - Sorozat-memória és karakter-szótár modul (Series Glossary).

Felelősség:
  - Automatikusan észleli, ha a fordítandó felirat egy sorozat epizódja (pl. S01E02).
  - Kezeli a könyvtárban található 'series_context.json' fájlt, amely eltárolja:
    - Fő karaktereket és megszólítási viszonyukat (tegezés / magázás).
    - Visszatérő szakkifejezéseket, helyszíneket és fordítási szabályokat.
  - Generál egy kontextus-blokkot az LLM prompt számára, megőrizve a sorozat teljes évadának nyelvi egységét.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from plex_subtitler.logger import get_logger
from plex_subtitler.config import app2_config

log = get_logger(__name__, level=app2_config.log_level)

SERIES_CONTEXT_FILENAME = "series_context.json"
SERIES_REGEX = re.compile(r'^(.*?)[._ -]+[Ss](\d{1,2})[Ee](\d{1,2})', re.IGNORECASE)
ALT_SERIES_REGEX = re.compile(r'^(.*?)[._ -]+(\d{1,2})x(\d{1,2})', re.IGNORECASE)


def detect_series_info(file_path: Path) -> dict | None:
    """
    Megvizsgálja a fájlnevet és megállapítja, hogy sorozatrészről van-e szó.

    Returns:
        dict: {"title": str, "season": int, "episode": int} vagy None
    """
    stem = file_path.stem
    # Eltávolítjuk a .hu utótagot ha van
    if stem.endswith(".hu"):
        stem = stem[:-3]

    m = SERIES_REGEX.search(stem)
    if not m:
        m = ALT_SERIES_REGEX.search(stem)

    if m:
        raw_title = m.group(1).replace(".", " ").replace("_", " ").strip()
        season = int(m.group(2))
        episode = int(m.group(3))
        return {
            "title": raw_title or file_path.parent.name,
            "season": season,
            "episode": episode,
        }
    return None


def get_series_context_path(file_path: Path) -> Path:
    """
    Megkeresi vagy kijelöli a series_context.json helyét.
    Ha a szülőmappa pl. 'Season 01', a nagyszülő (sorozat) mappába helyezi.
    """
    parent = file_path.parent
    if re.search(r'season\s*\d+', parent.name, re.IGNORECASE) or re.search(r'évad\s*\d+', parent.name, re.IGNORECASE):
        target_dir = parent.parent
    else:
        target_dir = parent

    return target_dir / SERIES_CONTEXT_FILENAME


def load_series_context(file_path: Path) -> dict:
    """
    Betölti a sorozat kontextusát, vagy létrehoz egy alapértelmezett sablont.
    """
    ctx_path = get_series_context_path(file_path)
    series_info = detect_series_info(file_path)
    title = series_info["title"] if series_info else file_path.parent.name

    default_data = {
        "series_title": title,
        "characters": {},
        "terms": {},
        "notes": "Hasznalj kovetkezetes megszolitasokat (tegezes/onozes) az epizodok kozott."
    }

    if not ctx_path.exists():
        try:
            ctx_path.parent.mkdir(parents=True, exist_ok=True)
            with open(ctx_path, "w", encoding="utf-8") as f:
                json.dump(default_data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            log.debug("Nem sikerult letrehozni a series_context.json fajlt: %s", exc)
        return default_data

    try:
        with open(ctx_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.warning("Hiba a series_context.json olvasasakor (%s): %s", ctx_path.name, exc)
        return default_data


def get_series_prompt_injection(file_path: Path) -> str:
    """
    Generál egy beilleszthető kontextus-utasítást a nyelvi modell (LLM) számára.
    """
    if not app2_config.series_memory_enabled:
        return ""

    series_info = detect_series_info(file_path)
    if not series_info:
        return ""

    ctx = load_series_context(file_path)
    parts = []
    parts.append(f"[SERIES CONTEXT MEMORY]:")
    parts.append(f"Title: {ctx.get('series_title', series_info['title'])} (Season {series_info['season']} Episode {series_info['episode']})")

    characters = ctx.get("characters", {})
    if characters:
        char_list = [f"{name}: {role_and_form}" for name, role_and_form in characters.items()]
        parts.append("Known Characters & Address Tone (Tegez/Magaz): " + "; ".join(char_list))

    terms = ctx.get("terms", {})
    if terms:
        terms_list = [f"{src} -> {tgt}" for src, tgt in terms.items()]
        parts.append("Consistent Glossary: " + "; ".join(terms_list))

    notes = ctx.get("notes")
    if notes:
        parts.append(f"Style notes: {notes}")

    return "\n".join(parts)
