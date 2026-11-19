"""SRT feliratfájlok olvasása, blokkosítása és időbélyeg-biztos visszaírása."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from plex_subtitler.config import app2_config
from plex_subtitler.logger import get_logger

log = get_logger(__name__, level=app2_config.log_level)

_ID_LINE_RE = re.compile(r"^\[(\d+)\]\s*(.*)$")


@dataclass
class SrtBlock:
    """Egyetlen SRT bejegyzés reprezentációja."""

    index: int
    timestamp: str
    text: str

    def to_string(self) -> str:
        """Visszaalakítja szabványos SRT sztringgé."""
        return f"{self.index}\n{self.timestamp}\n{self.text}\n"


class SrtParser:
    """Kezeli az SRT fájlok beolvasását, blokkosítását és kiírását."""

    def __init__(self, filepath: Path) -> None:
        self.filepath = Path(filepath)
        self.blocks: list[SrtBlock] = self._parse_file()

    def _parse_file(self) -> list[SrtBlock]:
        """Beolvassa az SRT fájlt (UTF-8 BOM támogatással) és SrtBlock objektumokra bontja."""
        if not self.filepath.exists():
            raise FileNotFoundError(f"Az SRT fájl nem található: '{self.filepath}'")

        content = self.filepath.read_text(encoding="utf-8-sig", errors="replace")
        content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not content:
            return []

        raw_blocks = re.split(r"\n\s*\n", content)
        blocks: list[SrtBlock] = []
        for raw_block in raw_blocks:
            lines = [line for line in raw_block.strip().splitlines() if line.strip() != ""]
            if len(lines) < 2:
                continue
            try:
                index = int(lines[0].strip())
                timestamp = lines[1].strip()
                text = "\n".join(lines[2:]).strip() if len(lines) > 2 else ""
                if "-->" not in timestamp:
                    raise ValueError(f"Érvénytelen időbélyeg: {timestamp}")
                blocks.append(SrtBlock(index=index, timestamp=timestamp, text=text))
            except ValueError as exc:
                log.warning("Hiba a blokk értelmezésekor (kihagyva): %s | %s", raw_block[:40], exc)

        log.info("Sikeresen beolvasva %d blokk: '%s'", len(blocks), self.filepath.name)
        return blocks

    def get_chunks(self, batch_size: int = 30) -> Generator[list[SrtBlock], None, None]:
        """Generátor, ami 'batch_size' méretű darabokban adja vissza a blokkokat."""
        size = max(1, batch_size)
        for i in range(0, len(self.blocks), size):
            yield self.blocks[i : i + size]

    @staticmethod
    def save_srt(blocks: list[SrtBlock], output_path: Path) -> None:
        """Lementi az SrtBlock listát egy szabványos UTF-8 SRT fájlba."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            for block in blocks:
                f.write(block.to_string() + "\n")
        log.info("Fordított SRT mentve: '%s'", output_path.name)

    @staticmethod
    def blocks_to_text_format(blocks: list[SrtBlock]) -> str:
        """Az LLM prompt számára egy kompakt '[ID] Szöveg' formátumot generál."""
        lines = []
        for block in blocks:
            clean_text = block.text.replace("\n", " \\n ")
            lines.append(f"[{block.index}] {clean_text}")
        return "\n".join(lines)

    @staticmethod
    def apply_translation_to_blocks(
        original_blocks: list[SrtBlock],
        translated_text: str,
    ) -> list[SrtBlock]:
        """Visszailleszti a lefordított szövegeket az eredeti időbélyeges blokkokba."""
        translations: dict[int, str] = {}
        current_id: int | None = None
        current_lines: list[str] = []

        def _flush() -> None:
            if current_id is not None:
                joined = "\n".join(current_lines).replace(" \\n ", "\n").replace("\\n", "\n").strip()
                translations[current_id] = joined

        for raw_line in translated_text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            match = _ID_LINE_RE.match(stripped)
            if match:
                _flush()
                current_id = int(match.group(1))
                current_lines = [match.group(2).strip()] if match.group(2).strip() else []
            elif current_id is not None:
                current_lines.append(stripped)
        _flush()

        translated_blocks: list[SrtBlock] = []
        for orig in original_blocks:
            if orig.index in translations and translations[orig.index]:
                new_text = translations[orig.index]
            else:
                log.warning("Hiányzó fordítás a %d. blokkhoz! Az eredetit tartjuk meg.", orig.index)
                new_text = orig.text
            translated_blocks.append(
                SrtBlock(index=orig.index, timestamp=orig.timestamp, text=new_text)
            )
        return translated_blocks
