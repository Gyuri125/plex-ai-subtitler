# Plex Subtitler Module

Ez a könyvtár tartalmazza a **Plex AI Subtitler** teljes Python forráskódját.

A projekt fő GitHub dokumentációja a gyökérkönyvtárban található:
- [Fő README.md](../README.md) – Teljes áttekintés, architektúra, Plex konfiguráció, képernyőképek és gyorsútmutató.
- [AGENT_README.md](../AGENT_README.md) – Részletes fejlesztői és belső architektúra leírás.

---

## Modulstruktúra

```text
plex_subtitler/
├── config.py                  # Központi konfigurációkezelő (SettingsManager, JSON perzisztencia)
├── logger.py                  # Színezett konzolos és fájl alapú naplózó
├── notifier.py                # Plex Auto-Refresh és push értesítések kezelője
├── requirements.txt           # Python csomagfüggőségek
├── settings.json.example      # Sablon beállítási fájl
├── .env.example               # Régi környezeti változó sablon
│
├── app1_extractor/            # 1. FÁZIS: Mappafigyelés, beágyazott feliratkinyerés és Whisper STT
│   ├── main.py                # Belépési pont (GUI és CLI indító)
│   ├── gui.py                 # CustomTkinter Dark Mode felhasználói felület (Tray ikonnal)
│   ├── watcher.py             # Watchdog könyvtárfigyelő és fájlméret-stabilizáló
│   ├── file_processor.py      # Fájlkezelő, MD5 duplikátumszűrés, átmeneti fájlok takarítása
│   ├── subtitle_extractor.py  # Beágyazott feliratsávok szondázása és azonnali kinyerése
│   ├── audio_extractor.py     # FFmpeg wrapper (16kHz mono WAV kinyerés)
│   ├── transcriber.py         # faster-whisper neurális transzkripció és SRT formázó
│   └── hardware_manager.py    # Torch CUDA és VRAM detektálás, automatikus profilválasztó
│
└── app2_translator/           # 2. FÁZIS: SRT szegmentálás és LLM szinkronfordítás
    ├── translator_daemon.py   # SRT fájlfigyelő és fordítás-koordinátor daemon
    ├── srt_parser.py          # SRT blokk értelmező, 30-as csomagoló és visszamásoló
    └── llm_client.py          # Google Gemini, OpenAI és Ollama API kliensek, Sliding Context
```
