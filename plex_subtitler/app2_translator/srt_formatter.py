"""TV és Plex lejátszó-barát SRT utóformázó (sortörés és időbélyeg-átfedés javítása)."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

from plex_subtitler.app2_translator.srt_parser import SrtBlock, SrtParser

_TS_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _hms_to_ms(h: int, m: int, s: int, ms: int) -> int:
    return ((h * 3600) + (m * 60) + s) * 1000 + ms


def _ms_to_hms(total_ms: int) -> str:
    clamped = max(0, total_ms)
    ms = clamped % 1000
    total_sec = clamped // 1000
    hours, rem = divmod(total_sec, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def parse_srt_timestamp_ms(timestamp: str) -> tuple[int, int] | None:
    """Kiolvas egy SRT időbélyeg sort ezredmásodperc párként."""
    match = _TS_RE.search(timestamp.strip())
    if not match:
        return None
    h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(g) for g in match.groups())
    return _hms_to_ms(h1, m1, s1, ms1), _hms_to_ms(h2, m2, s2, ms2)


def format_srt_timestamp_ms(start_ms: int, end_ms: int) -> str:
    """Visszaalakít egy kezdő és vég időpontot szabványos SRT időbélyeggé."""
    return f"{_ms_to_hms(start_ms)} --> {_ms_to_hms(end_ms)}"


def _balanced_two_line_split(single_line: str, max_chars: int) -> str:
    """Kiegyensúlyozottan két sorba tördel egy mondatot szóhatáron."""
    words = single_line.split()
    if len(words) <= 1:
        return single_line
    target = len(single_line) / 2
    best_idx = 1
    best_diff = float("inf")
    cur_len = 0
    for idx in range(1, len(words)):
        cur_len += len(words[idx - 1]) + (1 if idx > 1 else 0)
        line1 = " ".join(words[:idx])
        line2 = " ".join(words[idx:])
        if len(line1) <= max_chars and len(line2) <= max_chars:
            diff = abs(cur_len - target)
            if diff < best_diff:
                best_diff = diff
                best_idx = idx
    if best_diff < float("inf"):
        return " ".join(words[:best_idx]) + "\n" + " ".join(words[best_idx:])

    wrapped = textwrap.wrap(single_line, width=max_chars, break_long_words=False, break_on_hyphens=False)
    if len(wrapped) <= 2:
        return "\n".join(wrapped)
    return wrapped[0] + "\n" + " ".join(wrapped[1:])


def wrap_subtitle_text(text: str, max_chars: int = 42, max_lines: int = 2) -> str:
    """TV/Plex-barát sortörést alkalmaz a felirat szövegére."""
    cleaned_lines = [" ".join(part.split()) for part in text.splitlines() if part.strip()]
    if not cleaned_lines:
        return ""

    # Ha párbeszédes kötőjelek vannak (- Szia! / - Hello!), tartsuk meg külön sorban
    if len(cleaned_lines) == 2 and all(len(l) <= max_chars for l in cleaned_lines):
        return "\n".join(cleaned_lines)

    if len(cleaned_lines) >= 2 and cleaned_lines[0].startswith("-") and cleaned_lines[1].startswith("-"):
        lines = cleaned_lines[:max_lines]
        return "\n".join(lines)

    single_line = " ".join(cleaned_lines)
    if len(single_line) <= max_chars:
        return single_line

    if max_lines == 2:
        return _balanced_two_line_split(single_line, max_chars=max_chars)

    wrapped = textwrap.wrap(single_line, width=max_chars, break_long_words=False)
    return "\n".join(wrapped[:max_lines])


def fix_overlapping_timestamps(
    blocks: list[SrtBlock],
    min_gap_ms: int = 10,
) -> list[SrtBlock]:
    """Megszünteti az egymásba csúszó vagy fordított időbélyegeket."""
    if not blocks:
        return []

    parsed: list[tuple[SrtBlock, int, int] | tuple[SrtBlock, None, None]] = []
    for block in blocks:
        times = parse_srt_timestamp_ms(block.timestamp)
        if times is None:
            parsed.append((block, None, None))
        else:
            start_ms, end_ms = times
            if end_ms <= start_ms:
                end_ms = start_ms + 500
            parsed.append((block, start_ms, end_ms))

    adjusted: list[SrtBlock] = []
    for i, item in enumerate(parsed):
        block, start_ms, end_ms = item
        if start_ms is None or end_ms is None:
            adjusted.append(block)
            continue

        if i + 1 < len(parsed):
            _, next_start, _ = parsed[i + 1]
            if next_start is not None and end_ms > next_start - min_gap_ms:
                clamped_end = max(start_ms + 200, next_start - min_gap_ms)
                if clamped_end <= next_start:
                    end_ms = clamped_end

        adjusted.append(
            SrtBlock(
                index=block.index,
                timestamp=format_srt_timestamp_ms(start_ms, end_ms),
                text=block.text,
            )
        )
    return adjusted


def format_srt_blocks(
    blocks: list[SrtBlock],
    max_chars: int = 42,
    max_lines: int = 2,
) -> list[SrtBlock]:
    """Alkalmazza a TV/Plex sortörést és az időbélyeg-igazítást a teljes feliratra."""
    wrapped_blocks = [
        SrtBlock(
            index=b.index,
            timestamp=b.timestamp,
            text=wrap_subtitle_text(b.text, max_chars=max_chars, max_lines=max_lines),
        )
        for b in blocks
    ]
    return fix_overlapping_timestamps(wrapped_blocks)


def shift_srt_timestamps(blocks: list[SrtBlock], offset_ms: int) -> list[SrtBlock]:
    """Eltolja az összes feliratblokk időbélyegét a megadott ezredmásodperccel (±ms)."""
    if offset_ms == 0:
        return list(blocks)

    shifted: list[SrtBlock] = []
    for block in blocks:
        times = parse_srt_timestamp_ms(block.timestamp)
        if times is None:
            shifted.append(block)
            continue
        start_ms, end_ms = times
        duration = max(100, end_ms - start_ms)
        new_start = max(0, start_ms + offset_ms)
        new_end = max(new_start + 100, start_ms + offset_ms + duration)
        shifted.append(
            SrtBlock(
                index=block.index,
                timestamp=format_srt_timestamp_ms(new_start, new_end),
                text=block.text,
            )
        )
    return shifted


def shift_srt_file(
    srt_path: Path,
    offset_ms: int,
    output_path: Path | None = None,
) -> Path:
    """Betölt egy SRT fájlt, eltolja az időbélyegeit ±offset_ms értékkel és elmenti."""
    parser = SrtParser(srt_path)
    shifted = shift_srt_timestamps(parser.blocks, offset_ms)
    target = Path(output_path) if output_path else Path(srt_path)
    SrtParser.save_srt(shifted, target)
    return target
