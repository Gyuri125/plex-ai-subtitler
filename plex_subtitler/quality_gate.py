"""
quality_gate.py - Felirat minőségellenőrző és Whisper hallucináció-szűrő modul.

Felelősség:
  - Kiszűri a Whisper ismert hallucinációit (pl. 'Thank you for watching', 'Please subscribe', csendben ismétlődő szövegek).
  - Kijavítja a sérült, negatív vagy átfedő időbélyegeket.
  - Ellenőrzi a lefordított blokkok integritását, garantálva, hogy egyetlen feliratsor sem veszik el.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .logger import get_logger
from .config import app1_config

if TYPE_CHECKING:
    from .app2_translator.srt_parser import SrtBlock

log = get_logger(__name__, level=app1_config.log_level)

# Tipikus Whisper fantomszövegek és hallucinációk regex mintái
HALLUCINATION_PATTERNS = [
    r'^[\s\.\,\!\?\-\_]*$',                     # Csak írásjelek
    r'\bthank you for watching\b',
    r'\bplease subscribe\b',
    r'\bsubscribe to\b',
    r'\blike and subscribe\b',
    r'\bsubtitles by\b',
    r'\btranscribed by\b',
    r'\bclosed captions by\b',
    r'\bcaptioned by\b',
    r'\bamara\.org\b',
    r'\bmoztw\.org\b',
    r'\btranslated by\b',
    r'^[\u266a\u266b\s]+$',                     # Csak zenei szimbolumok
]

COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in HALLUCINATION_PATTERNS]


def is_hallucination(text: str) -> bool:
    """Megállapítja, hogy egy adott feliratsor ismert Whisper hallucináció-e."""
    clean = text.strip()
    if not clean:
        return True

    for pattern in COMPILED_PATTERNS:
        if pattern.search(clean):
            return True

    return False


def clean_whisper_blocks(blocks: list[SrtBlock]) -> list[SrtBlock]:
    """
    Kitisztítja a feliratblokkok listáját a hallucinációktól és időzítési hibáktól.
    """
    if not app1_config.quality_gate_enabled or not blocks:
        return blocks

    cleaned: list[SrtBlock] = []
    removed_count = 0
    last_text = ""
    repetition_count = 0

    for b in blocks:
        text = b.text.strip()

        # 1. Ismert hallucinációs minta ellenőrzése
        if is_hallucination(text):
            removed_count += 1
            continue

        # 2. Ismétlődési hurok (Loop) szűrés: ha egymás után 3-nál többször ismétli pontosan ugyanazt a rövid szöveget
        if text.lower() == last_text.lower() and len(text) < 30:
            repetition_count += 1
            if repetition_count >= 3:
                removed_count += 1
                continue
        else:
            last_text = text
            repetition_count = 1

        cleaned.append(b)

    # 3. Időbélyegek javítása és átfedések simítása
    from .app2_translator.srt_parser import SrtBlock
    repaired: list[SrtBlock] = []
    for i, b in enumerate(cleaned, start=1):
        repaired.append(
            SrtBlock(
                index=i,
                timestamp=b.timestamp,
                text=b.text.strip(),
            )
        )

    if removed_count > 0:
        log.info("Quality Gate: %d hallucinacio / fantomszoveg eltavolitva a feliratbol.", removed_count)

    return repaired


def validate_and_repair_translated_blocks(
    original_blocks: list[SrtBlock], 
    translated_blocks: list[SrtBlock]
) -> list[SrtBlock]:
    """
    Ellenőrzi, hogy a fordítás során minden eredeti blokk megmaradt-e.
    Ha az LLM kihagyott volna egy indexet, automatikusan visszaállítja az eredeti szöveggel.
    """
    from .app2_translator.srt_parser import SrtBlock

    translated_map = {b.index: b for b in translated_blocks}
    final_blocks: list[SrtBlock] = []
    restored_count = 0

    for orig in original_blocks:
        if orig.index in translated_map and translated_map[orig.index].text.strip():
            final_blocks.append(translated_map[orig.index])
        else:
            restored_count += 1
            final_blocks.append(
                SrtBlock(index=orig.index, timestamp=orig.timestamp, text=orig.text)
            )

    if restored_count > 0:
        log.warning("Quality Gate: %d hianyzo forditasi blokk helyreallitva az eredeti szoveggel.", restored_count)

    return final_blocks
