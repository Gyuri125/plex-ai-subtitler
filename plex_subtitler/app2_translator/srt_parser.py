"""
srt_parser.py - SRT feliratfájlok olvasása, blokkosítása és írása.

Ez a modul felelős azért, hogy az SRT fájlt logikai egységekre (chunk) bontsa.
Szigorú elvárás, hogy az időbélyegek és az indexek érintetlenek maradjanak
a fordítás során, ezért csak a "text" (szöveg) részt küldjük el kontextussal együtt,
vagy ha az egész blokkot elküldjük, akkor biztosítjuk a formátum megtartását.
"""

from __future__ import annotations
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Generator

from plex_subtitler.logger import get_logger
from plex_subtitler.config import app2_config

log = get_logger(__name__, level=app2_config.log_level)

@dataclass
class SrtBlock:
    """Egyetlen SRT bejegyzés reprezentációja."""
    index: int
    timestamp: str  # Pl.: "00:02:15,300 --> 00:02:18,100"
    text: str

    def to_string(self) -> str:
        """Visszaalakítja szabványos SRT sztringgé."""
        return f"{self.index}\n{self.timestamp}\n{self.text}\n"

class SrtParser:
    """
    Kezeli az SRT fájlok beolvasását, blokkosítását és kiírását.
    """
    
    def __init__(self, filepath: Path):
        self.filepath = Path(filepath)
        self.blocks: list[SrtBlock] = []
        self._parse_file()
        
    def _parse_file(self) -> None:
        """Beolvassa az SRT fájlt és SrtBlock objektumokra bontja."""
        if not self.filepath.exists():
            raise FileNotFoundError(f"Az SRT fájl nem található: {self.filepath}")
            
        with self.filepath.open('r', encoding='utf-8', errors='replace') as f:
            content = f.read()
            
        # Dupla sortörések mentén szétvágjuk a blokkokra (figyelve a Windows \r\n-re is)
        raw_blocks = re.split(r'\n\s*\n', content.strip())
        
        for raw_block in raw_blocks:
            lines = raw_block.strip().split('\n')
            if len(lines) >= 3:
                try:
                    index = int(lines[0].strip())
                    timestamp = lines[1].strip()
                    text = "\n".join(lines[2:]).strip()
                    self.blocks.append(SrtBlock(index=index, timestamp=timestamp, text=text))
                except ValueError:
                    log.warning(f"Hiba a blokk értelmezésekor (kihagyva): {lines[0]}")
                    
        log.info(f"Sikeresen beolvasva {len(self.blocks)} blokk: {self.filepath.name}")

    def get_chunks(self, batch_size: int = 30) -> Generator[list[SrtBlock], None, None]:
        """
        Generátor, ami 'batch_size' méretű darabokban (chunk) adja vissza a blokkokat.
        Ez kritikus a kontextusfüggő LLM fordításhoz.
        """
        for i in range(0, len(self.blocks), batch_size):
            yield self.blocks[i:i + batch_size]

    @staticmethod
    def save_srt(blocks: list[SrtBlock], output_path: Path) -> None:
        """
        Lementi az SrtBlock listát egy szabványos SRT fájlba.
        """
        with open(output_path, 'w', encoding='utf-8') as f:
            for block in blocks:
                f.write(block.to_string())
                f.write("\n")
        log.info(f"💾 Fordított SRT mentve: {output_path.name}")

    @staticmethod
    def blocks_to_text_format(blocks: list[SrtBlock]) -> str:
        """
        Az LLM prompt számára egy kompakt stringet generál a blokkokból.
        A formátum:
        [ID] Szöveg
        Ez csökkenti a tokenfelhasználást és az LLM nehezebben rontja el az időbélyeget (mert nem kapja meg).
        A visszatérő formátumból aztán könnyű rekonstruálni az eredeti blokkokat.
        """
        result = []
        for b in blocks:
            # Sortöréseket kicseréljük egy egyedi szeparátorra vagy szóközre (szóköz jobb, LLM okos)
            # De a feliratoknál jobb megtartani a sortörést, így \n-t használunk.
            text = b.text.replace("\n", " \\n ")
            result.append(f"[{b.index}] {text}")
        return "\n".join(result)
        
    @staticmethod
    def apply_translation_to_blocks(original_blocks: list[SrtBlock], translated_text: str) -> list[SrtBlock]:
        """
        Az LLM által visszaadott szövegből (amiben [ID] azonosítók vannak)
        frissíti az eredeti blokkokat a lefordított szöveggel.
        Szigorúan ellenőrzi, hogy az ID-k egyeznek-e.
        """
        translated_blocks = []
        
        # Regex az [ID] Szöveg kinyerésére
        pattern = re.compile(r'\[(\d+)\]\s*(.*)')
        
        # Létrehozunk egy szótárat a könnyebb kereséshez
        translations = {}
        for line in translated_text.split('\n'):
            line = line.strip()
            if not line:
                continue
            match = pattern.match(line)
            if match:
                idx = int(match.group(1))
                text = match.group(2).replace(" \\n ", "\n") # Visszaállítjuk a sortöréseket
                translations[idx] = text
                
        # Alkalmazzuk az eredeti blokkokra
        for block in original_blocks:
            if block.index in translations:
                translated_blocks.append(
                    SrtBlock(index=block.index, timestamp=block.timestamp, text=translations[block.index])
                )
            else:
                log.warning(f"⚠️ Hiányzó fordítás a {block.index}. blokkhoz! Az eredetit tartjuk meg.")
                translated_blocks.append(block)
                
        return translated_blocks
