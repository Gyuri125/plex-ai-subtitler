# Plex AI Subtitler — Agent & Developer Architecture Guide

> **Célközönség:** Fejlesztő ágensek és szoftvermérnökök.  
> **Tartalom:** Rendszerarchitektúra, modulok felelősségi köre, belső adatfolyamok, szálkezelés, konfigurációs életciklus és biztonsági szabályok.

---

## 1. Rendszerkoncepció és filozófia

A **Plex AI Subtitler** egy moduláris, aszinkron pipeline, amely mozgóképekből (`.mkv`, `.mp4`, `.avi`) teljesen automatikusan készít Plex-kompatibilis, professzionális minőségű magyar feliratot.

### Miért két különálló alkalmazásból áll (App 1 & App 2)?
1. **Hibaizoláció (Fault Isolation):** A GPU/CPU-intenzív neurális beszédhang-felismerés (Whisper) és a hálózati I/O-intenzív LLM API hívások teljesen eltérő hibapontokkal rendelkeznek (CUDA OOM vs. HTTP 429 Rate Limit).
2. **Különálló skálázhatóság:** Az App 1 és App 2 futhat egy gépen egy folyamatban (GUI-n keresztül), vagy független daemonként két különböző szerveren/konténerben (pl. App 1 a GPU-s munkaállomáson, App 2 a felhőben/NAS-on).
3. **Köztes állapot perzisztencia:** Az App 1 azonnal lementi az angol/forrásnyelvű `.srt` fájlt a lemezre. Ha az LLM API éppen nem elérhető vagy elfogy a kvóta, az eredeti felirat akkor is használható marad a Plexben, és az App 2 a kvóta megújulásakor automatikusan folytatja a fordítást.

---

## 2. Projektstruktúra és Fájltérkép

```text
Gemini-AI_plexfordító/
├── .gitignore                   # Szigorú globális kizárási szabályok (Secrets, Cache, Media)
├── settings.json.example        # Mintakonfiguráció (API kulcsok nélkül!)
├── requirements.txt             # Pip függőségek listája
├── LICENSE                      # MIT licenc
├── README.md                    # Nyilvános GitHub dokumentáció (magyar/angol)
├── AGENT_README.md              # ← EZ A DOKUMENTUM: Fejlesztői és ágens útmutató
├── PlexSubtitler.spec           # PyInstaller bináris csomagoló specifikáció
├── build_exe.bat                # Automata Git-pull és Exe fordító/frissítő script
├── inditas.bat                  # Duplakattintásos indító konzolablak nélkül
├── Dockerfile                   # Headless konténer Unraid/NAS szerverekhez
├── docker-compose.yml           # Docker Compose konfiguráció
│
└── plex_subtitler/              # Fő Python csomag
    ├── config.py                # Konfigurációkezelő (SettingsManager singleton, JSON perzisztencia)
    ├── logger.py                # Színezett konzolos és fájl logging
    ├── notifier.py              # Plex Auto-Refresh API hívások és Discord/Telegram értesítések
    ├── settings.json            # [GITIGNORED] Felhasználó helyi beállításai és API kulcsai
    ├── processed_files.log      # [GITIGNORED] MD5 ujjlenyomat napló a duplikáció elkerülésére
    ├── temp_audio/              # [GITIGNORED] Ideiglenes 16kHz mono WAV audio kinyerések
    │
    ├── app1_extractor/          # 1. FÁZIS: Mappafigyelés + FFmpeg + faster-whisper
    │   ├── __init__.py
    │   ├── main.py              # Fő belépési pont (GUI indító vagy CLI argumentum parser)
    │   ├── gui.py               # CustomTkinter Dark Mode GUI (Tray ikon, integrációk)
    │   ├── watcher.py           # Watchdog daemon, fájlméret-stabilitás ellenőrzés
    │   ├── file_processor.py    # Pipeline orkesztrátor, MD5 ellenőrzés, fájltakarítás
    │   ├── subtitle_extractor.py# Beágyazott feliratok szondázása és 0.5s kinyerése
    │   ├── audio_extractor.py   # FFmpeg hívás (imageio-ffmpeg vagy rendszer bináris)
    │   ├── transcriber.py       # faster-whisper STT motor, VAD és SRT időzítés
    │   └── hardware_manager.py  # PyTorch CUDA & VRAM vizsgálat, dinamikus Whisper profilozó
    │
    └── app2_translator/         # 2. FÁZIS: SRT szegmentálás + LLM szinkronfordítás
        ├── __init__.py
        ├── translator_daemon.py # SRT mappafigyelő és fordítási munkaszál koordinátor
        ├── srt_parser.py        # SRT blokk értelmező, 30-as csomagoló és visszamásoló
        └── llm_client.py        # LLM Factory (Gemini, OpenAI, Ollama), Sliding Context, retry logika
```

---

## 3. Komponensek részletes működése

### 3.1. `plex_subtitler.config` (`config.py`)
- **Osztályok:** `App1Config`, `App2Config`, `SettingsManager`.
- **Tárolás:** Egyetlen `settings.json` fájlban tárolódik az összes beállítás.
- **PyInstaller hordozhatóság:**
  ```python
  if getattr(sys, 'frozen', False):
      ROOT_DIR = Path(sys.executable).parent
  else:
      ROOT_DIR = Path(__file__).resolve().parent
  ```
  Ez garantálja, hogy lefordított `.exe` futtatásakor ne az ideiglenes `_MEIxxxxxx` mappába írja a konfigurációt, hanem az exe mellé.
- **Auto-generálás:** Ha a `settings.json` nem létezik, a `SettingsManager.load()` automatikusan inicializál egyet az alapértelmezett értékekkel (`self.save()`).

### 3.2. `app1_extractor` (Audió kinyerés és Whisper STT)

#### A) `main.py`
- CLI kapcsolók:
  - `--cli`: Fej nélküli (headless) futás GUI nélkül.
  - `--scan-existing`: Induláskor átfésüli a figyelt könyvtárakat a már bent lévő videókért.
  - `--once <PATH>`: Csak a megadott egyetlen videót dolgozza fel, majd azonnal kilép (kiváló unit-teszteléshez és hibakereséshez).
  - Ha nincs kapcsoló megadva: elindul a `gui.py` (`run_gui()`).

#### B) `gui.py`
- **Technológia:** `customtkinter` (Modern Dark Mode).
- **Szálbiztonság:** A Tkinter UI nem futhat más szálon, mint a főszál. Ezért a `GuiLogHandler` a `text_widget.after(0, append_log)` metódust használja, ami thread-safe módon küldi be a naplóüzeneteket a felületre.
- **Munkaszálak:** A mappafigyelők (`WatcherDaemon`, `TranslatorDaemon`) külön háttérszálakon (`threading.Thread(daemon=True)`) futnak, így a GUI sosem fagy le.

#### C) `watcher.py` & Fájlméret-stabilitás (`_wait_for_file_stable`)
- **Probléma:** Ha egy torrent éppen töltődik, vagy egy hálózati másolás (SMB/NFS) folyamatban van, az FFmpeg nem olvashatja a csonka fájlt.
- **Megoldás:** A `_wait_for_file_stable(path)` algoritmus 2 másodperces időközönként háromszor ellenőrzi a fájlméretet. Csak akkor engedi tovább a feldolgozást, ha a méret 3 egymást követő vizsgálatban azonos és nem nulla.

#### D) `file_processor.py` & Duplikáció-szűrés
- A feldolgozott fájlok MD5 hash-e bekerül a `processed_files.log` fájlba.
- Ha a fájl már szerepel a naplóban, a rendszer azonnal átugorja.
- **Temp takarítás:** A kinyert `.wav` fájl egy `try ... finally` blokkban törlődik a lemezről a transzkripció végén, így nem fogy el a tárhely.

#### E) `audio_extractor.py`
- Az audió hangsávot `16000 Hz`-es, 1 csatornás (mono) PCM WAV formátumba menti (`-vn -acodec pcm_s16le -ar 16000 -ac 1`).
- Ha a rendszeren nincs telepítve az `ffmpeg`, automatikusan az `imageio-ffmpeg` beépített binárisára támaszkodik.

#### F) `transcriber.py`
- A CTranslate2 alapú `faster-whisper` modellt futtatja.
- Használja a Silero VAD (Voice Activity Detection) szűrőt a csend-szegmensek és a zenei háttérzajok kiszűrésére.
- SRT időbélyegeket generál: `HH:MM:SS,mmm --> HH:MM:SS,mmm`.

#### G) `hardware_manager.py`
- VRAM és CUDA detektálás `torch.cuda`-val.
- Profilajánlások:
  - $\ge$ 8GB VRAM: `large-v3`, `float16`
  - $\ge$ 4GB VRAM: `medium`, `float16`
  - $<$ 4GB VRAM: `small`, `float16`
  - Csak CPU: `small`, `int8` (CTranslate2 int8 kvantálás CPU-n 3-4x gyorsabb a float32-nél).

---

### 3.3. `app2_translator` (AI Feliratfordító)

#### A) `translator_daemon.py`
- Figyeli az új `.srt` fájlokat a mappákban.
- **Fontos szűrés:** Szigorúan kihagyja a `.hu.srt` végű fájlokat, nehogy rekurzív végtelen ciklusba kerüljön a magyar fájlok újrafordításával!
- Ha egy film mellett már létezik `.hu.srt`, átugorja.

#### B) `srt_parser.py`
- Regexszel bontja szét az SRT-t `SrtBlock` objektumokra (`index`, `start_time`, `end_time`, `lines`).
- **Batching (Csomagolás):** 30 feliratblokkonként készít egy összefüggő promptot:
  ```text
  [1] Hello, how are you?
  [2] I'm doing fine, thanks. \n And you?
  ```
  A belső sortöréseket `\n` jellel reprezentálja, megőrizve a képernyőn látható 2 soros tördelést.
- **Reconstruction:** A modell válaszából visszafejti az ID-kat (`[1]`, `[2]`), és újraépíti a formázott `.srt` fájlt a pontos eredeti időkódokkal.

#### C) `llm_client.py`
- **Támogatott szolgáltatók:**
  - `Google Gemini` (`google-genai` SDK): Alapértelmezettként a `gemini-1.5-flash` vagy `gemini-2.0-flash` ajánlott.
  - `OpenAI` (`openai` SDK): pl. `gpt-4o-mini`.
  - `Local Ollama` (`openai` kompatibilis végpont vagy helyi REST): pl. `llama3`, `mistral`.
- **Szinkronfordítási rendszerprompt (`SYSTEM_PROMPT`):**
  - Megköveteli a szinkron-stílusú magyar nyelvhasználatot (nem tükörfordítás!).
  - Kifejezett tiltás a csevegő válaszokra ("Íme a fordítás:" stb. szigorúan tilos).
  - Pontos ID-tartás megkövetelése.
- **Hiba- és kvótatűrés (`translate_blocks_with_retry`):**
  - Ha a szerver `429 Too Many Requests` vagy `Resource Exhausted` hibát dob, kinyeri a javasolt várakozási időt (`try again in Xs`), és alszik (alapból min. 60 másodpercet).
  - Minden másodpercben ellenőrzi a `stop_event`-et, így a GUI leállító gombjára azonnal és tisztán reagál.
  - Részeredmény-mentés: Ha megszakad a fordítás, az addig elkészült blokkok nem vesznek el, elmenti a részleges `.hu.srt`-t.

---

## 4. Plex Media Server kompatibilitási szabályok

A Plex külső feliratfelismerő motorja a fájlnevek struktúrájára támaszkodik:

| Eredeti videófájl | App 1 generált felirat (Forrás) | App 2 generált felirat (Magyar cél) | Plex észlelés |
| :--- | :--- | :--- | :--- |
| `Dune (2021).mkv` | `Dune (2021).srt` | `Dune (2021).hu.srt` | `English (External)` & `Hungarian (External)` |
| `Dark.S01E01.mp4` | `Dark.S01E01.srt` | `Dark.S01E01.hu.srt` | `Unknown/Source` & `Magyar` |

> **Fontos:** A feliratfájlnak közvetlenül a videofájl mellett, azonos könyvtárban kell elhelyezkednie!

---

## 5. Szabályok és Biztonsági Útmutató Ágenseknek (Guardrails)

Ha módosítod vagy bővíted a kódbázist, kövesd az alábbi szigorú szabályokat:

1. **Soha ne commitolj személyes adatot vagy titkot:**
   - A `settings.json`, `.env`, `processed_files.log` és a médiafájlok mindig maradjanak a `.gitignore`-ban.
   - Ha új beállítást adsz hozzá a `config.py`-hoz, frissítsd a `settings.json.example`-t is!
2. **Tartsd tiszteletben a szálbiztonságot:**
   - GUI műveleteket tilos háttérszálból közvetlenül hívni; mindig használd a `.after(0, ...)` mechanizmust.
3. **Karakterkódolás:**
   - Minden feliratfájl beolvasása és mentése szigorúan `utf-8` kódolással történjen (`encoding="utf-8"`).
4. **Elérési utak kezelése:**
   - Mindig a `pathlib.Path` modult használd az operációs rendszerek közötti kompatibilitás érdekében (Windows duplaper vs. Unix perjel).
5. **PyInstaller kompatibilitás:**
   - Ne használj olyan relatív fájlelérési utakat (`__file__`), amelyek nem veszik figyelembe a `sys.frozen` állapotot! Használd a `config.ROOT_DIR`-t.

---

## 6. Gyakori tesztelési és fejlesztői parancsok

```powershell
# 1. Virtuális környezet aktiválása (Windows PowerShell)
.venv\Scripts\Activate.ps1

# 2. Egyetlen fájl tesztelése parancssorból
python -m plex_subtitler.app1_extractor.main --once "C:\Media\Test\sample.mkv"

# 3. Fej nélküli futtatás meglévő fájlok beolvasásával
python -m plex_subtitler.app1_extractor.main --cli --scan-existing

# 4. GUI indítása
python -m plex_subtitler.app1_extractor.main

# 5. Standalone .exe építése PyInstallerrel
pyinstaller PlexSubtitler.spec
```
