"""Felhasználóbarát hibamagyarázó, hibakódok, öngyógyító akciók, ZIP hibacsomag, állapotjelző és előzmények."""

from __future__ import annotations

import json
import platform
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from plex_subtitler import __version__
from plex_subtitler.config import (
    LOGS_DIR,
    SERIES_CACHE_FILE,
    app1_config,
    app2_config,
    settings_manager,
)
from plex_subtitler.logger import get_log_file_path


@dataclass(frozen=True)
class FriendlyError:
    """Érthető, cselekvésorientált hibaösszegzés egyedi hibakóddal és opcionális gyorsjavítással."""

    code: str
    title: str
    summary_hu: str
    action_hu: str
    technical: str
    quick_fix_id: str | None = None
    quick_fix_label: str | None = None

    def format_dialog_text(self) -> str:
        return (
            f"Hibakód: {self.code}\n\n"
            f"{self.summary_hu}\n\n"
            f"👉 Mit tegyél most:\n{self.action_hu}\n\n"
            f"Technikai részlet (hibajelentéshez):\n{self.technical}"
        )


@dataclass(frozen=True)
class HealthCheckItem:
    """Egy rendszerkomponens élő állapotjelző eredménye."""

    name: str
    status: str  # "ok", "warn", "error", "muted"
    label: str
    detail: str


@dataclass(frozen=True)
class HistoryEntry:
    """Feldolgozott vagy félbemaradt film/epizód előzményrekord."""

    timestamp: str
    status: str  # "KÉSZ", "RÉSZLEGES", "HIBA"
    title: str
    detail: str
    file_path: Path | None = None


def explain_error(exc: Exception | str) -> FriendlyError:
    """Lefordít egy nyers Python/hálózati/GPU hibát közérthető magyar útmutatóra és hibakódra."""
    raw = str(exc).strip() or type(exc).__name__ if isinstance(exc, Exception) else str(exc).strip()
    lower = raw.lower()

    if (
        isinstance(exc, ConnectionError)
        or "11434" in lower
        or "connection refused" in lower
        or "nem érhető el az ollama" in lower
        or ("ollama" in lower and ("fut" in lower or "connect" in lower or "timeout" in lower))
    ):
        return FriendlyError(
            code="ERR-OLLAMA-01",
            title="A helyi AI motor (Ollama) nem válaszol",
            summary_hu="A program nem tudott csatlakozni a gépen futó helyi fordítóhoz (Ollama).",
            action_hu=(
                "1. Kattints lent az 'Ollama elindítása most' gombra (vagy nyisd meg a Start menüből az 'Ollama'-t).\n"
                "2. Várj 5 másodpercet, majd indítsd el újra a film feliratozását.\n"
                "3. Ha továbbra sem indul, ments egy Hibacsomag (.zip) fájlt az Asztalra."
            ),
            technical=raw,
            quick_fix_id="start_ollama",
            quick_fix_label="Ollama elindítása most",
        )

    if ("model" in lower and ("not found" in lower or "404" in lower)) or ("pull" in lower and "ollama" in lower):
        return FriendlyError(
            code="ERR-MODEL-02",
            title="Hiányzik a helyi AI nyelvi modell",
            summary_hu=f"A beállított AI modell ({app2_config.llm_model}) még nincs letöltve erre a gépre.",
            action_hu=(
                "1. Ellenőrizd, hogy van-e internetkapcsolat.\n"
                "2. Kattints lent a 'Modell letöltése most' gombra (vagy Haladó nézetben a 'Modellek lekérése' gombra)."
            ),
            technical=raw,
            quick_fix_id="pull_ollama_model",
            quick_fix_label=f"Modell letöltése ({app2_config.llm_model})",
        )

    if "out of memory" in lower or ("cuda" in lower and ("memory" in lower or "cublas" in lower or "cudnn" in lower)):
        return FriendlyError(
            code="ERR-VRAM-03",
            title="A videókártya (VRAM) memóriája megtelt",
            summary_hu="Az RTX videókártya memóriája egyszerre nem bírta a beszédfelismerést és a fordítást.",
            action_hu=(
                "1. Zárd be a háttérben futó videójátékot vagy más 3D programot.\n"
                "2. Kattints lent a 'VRAM ürítése + blokkméret 8-ra' gombra.\n"
                "3. Indítsd el újra — a program pontosan onnan folytatja, ahol abbamaradt!"
            ),
            technical=raw,
            quick_fix_id="free_vram_reduce_batch",
            quick_fix_label="VRAM ürítése + blokkméret csökkentése 8-ra",
        )

    if (
        "401" in lower
        or "403" in lower
        or "api key" in lower
        or "api_key" in lower
        or "unauthorized" in lower
        or ("permission" in lower and "key" in lower)
    ):
        return FriendlyError(
            code="ERR-KEY-04",
            title="Hibás vagy hiányzó AI API kulcs",
            summary_hu="A felhős AI szolgáltató (Gemini / OpenAI) elutasította a hozzáférést.",
            action_hu=(
                "1. Ha internet és API kulcs nélkül szeretnél fordítani, kattints az 'Átváltás helyi Ollama módra' gombra.\n"
                "2. Ha Geminit vagy OpenAI-t használsz, ellenőrizd az API kulcsot a '2. AI Translator' fülön."
            ),
            technical=raw,
            quick_fix_id="switch_to_ollama",
            quick_fix_label="Átváltás helyi Ollama (ingyenes) módra",
        )

    if "429" in lower or "quota" in lower or "rate limit" in lower or "resource_exhausted" in lower:
        return FriendlyError(
            code="ERR-QUOTA-05",
            title="Átmeneti AI terhelési / kvóta korlát",
            summary_hu="Túl sok kérés ment ki rövid idő alatt a felhős AI-hoz (az eddigi sorok el vannak mentve!).",
            action_hu=(
                "1. Várj 1-2 percet, majd indítsd el újra (onnan folytatja, ahol abbamaradt).\n"
                "2. Vagy kattints az 'Átváltás helyi Ollama (ingyenes) módra' gombra, ahol nincs limit."
            ),
            technical=raw,
            quick_fix_id="switch_to_ollama",
            quick_fix_label="Átváltás helyi Ollama (ingyenes) módra",
        )

    if isinstance(exc, FileNotFoundError) or "nem található" in lower or "no such file" in lower:
        return FriendlyError(
            code="ERR-FILE-06",
            title="A film vagy feliratfájl nem található",
            summary_hu="A kiválasztott videófájl vagy mappa közben elérhetetlenné vált.",
            action_hu=(
                "1. Ellenőrizd, hogy csatlakoztatva van-e a külső merevlemez vagy NAS / hálózati meghajtó.\n"
                "2. Húzd be újra a fájlt az ablakba."
            ),
            technical=raw,
        )

    if isinstance(exc, PermissionError) or "winerror 32" in lower or "being used by another process" in lower:
        return FriendlyError(
            code="ERR-LOCK-07",
            title="A fájlt épp használja egy másik program",
            summary_hu="A film vagy felirat zárolva van (pl. épp lejátszás vagy letöltés alatt áll).",
            action_hu=(
                "1. Zárd be a videólejátszót (VLC / Plex / Böngésző) vagy várd meg a fájl másolásának végét.\n"
                "2. Próbáld meg újra 10 másodperc múlva."
            ),
            technical=raw,
        )

    if "no space left" in lower or "winerror 112" in lower or "not enough space" in lower:
        return FriendlyError(
            code="ERR-DISK-08",
            title="Megtelt a meghajtó",
            summary_hu="Nincs elég szabad lemezterület az ideiglenes hangfájl vagy a felirat mentéséhez.",
            action_hu="Szabadíts fel legalább 2-3 GB tárhelyet azon a meghajtón, ahol a filmek vannak, majd próbáld újra.",
            technical=raw,
        )

    if "ffmpeg" in lower or "ffprobe" in lower or "invalid data found" in lower or "moov atom not found" in lower:
        return FriendlyError(
            code="ERR-MEDIA-09",
            title="Nem sikerült beolvasni a videófájlt",
            summary_hu="A videó formátuma hibásnak tűnik, vagy még nem fejeződött be a letöltése.",
            action_hu=(
                "1. Ellenőrizd VLC lejátszóval, hogy végignézhető-e a videófájl.\n"
                "2. Ha épp másolás/letöltés alatt volt, várd meg a végét és húzd be újra."
            ),
            technical=raw,
        )

    return FriendlyError(
        code="ERR-GEN-99",
        title="Váratlan hiba történt a feldolgozás közben",
        summary_hu="A művelet közben egy nem várt hiba lépett fel (a már kész feliratrészeket a program elmentette).",
        action_hu=(
            "1. Próbáld elindítani még egyszer — a program folytatja a félbemaradt részt.\n"
            "2. Ha ismét előjön, kattints a 'Hibacsomag (.zip)' vagy 'Hibajelentés másolása' gombra!"
        ),
        technical=raw,
    )


def execute_quick_fix(fix_id: str) -> tuple[bool, str]:
    """Végrehajtja a hibaablakból kért 1-kattintásos öngyógyító javítást."""
    if fix_id == "start_ollama":
        from plex_subtitler.app2_translator.llm_client import ensure_ollama_running

        ok = ensure_ollama_running(app2_config.llm_base_url or "http://localhost:11434")
        if ok:
            return True, "Az Ollama szerver elindult és válaszol! Most újraindíthatod a filmet."
        return False, "Nem sikerült elindítani az Ollama-t. Ellenőrizd, hogy telepítve van-e."

    if fix_id == "pull_ollama_model":
        from plex_subtitler.app2_translator.llm_client import (
            ensure_ollama_model_pulled,
            ensure_ollama_running,
        )

        base = app2_config.llm_base_url or "http://localhost:11434"
        if not ensure_ollama_running(base):
            return False, "Az Ollama szerver nem érhető el a modell letöltéséhez."
        ok = ensure_ollama_model_pulled(base_url=base, model=app2_config.llm_model)
        if ok:
            return True, f"A(z) '{app2_config.llm_model}' modell sikeresen letöltve és készen áll!"
        return False, f"Nem sikerült letölteni a(z) '{app2_config.llm_model}' modellt (ellenőrizd az internetet)."

    if fix_id == "free_vram_reduce_batch":
        from plex_subtitler.app1_extractor.transcriber import unload_model
        from plex_subtitler.app2_translator.llm_client import unload_ollama_model

        unload_model()
        if app2_config.llm_provider.lower().strip() in ("local", "ollama"):
            unload_ollama_model(app2_config.llm_base_url, app2_config.llm_model)
        app2_config.translation_batch_size = min(8, max(5, int(app2_config.translation_batch_size)))
        settings_manager.save()
        return (
            True,
            f"GPU VRAM kiürítve, és a blokkméret biztonságos {app2_config.translation_batch_size} sorra csökkentve!",
        )

    if fix_id == "switch_to_ollama":
        from plex_subtitler.app2_translator.llm_client import ensure_ollama_running

        app2_config.llm_provider = "local"
        app2_config.llm_model = "gemma3:12b"
        app2_config.llm_base_url = "http://localhost:11434/v1"
        app2_config.translation_batch_size = 12
        settings_manager.save()
        started = ensure_ollama_running(app2_config.llm_base_url)
        if started:
            return True, "Átváltva helyi Ollama (gemma3:12b, batch=12) módra és az Ollama fut!"
        return True, "Beállítások átváltva helyi Ollama (gemma3:12b) módra. Indítsd el az Ollama-t a használathoz."

    return False, f"Ismeretlen gyorsjavítási művelet: {fix_id}"


def _build_sanitized_settings_dict() -> dict:
    """Kulcsmentesített (API-kulcs és Plex-token nélküli) beállítás-export hibakereséshez."""
    a1 = asdict(app1_config)
    a2 = asdict(app2_config)
    if a1.get("opensubtitles_api_key"):
        a1["opensubtitles_api_key"] = "***REDACTED***"
    if a2.get("llm_api_key"):
        a2["llm_api_key"] = "***REDACTED***"
    if a2.get("plex_token"):
        a2["plex_token"] = "***REDACTED***"
    return {"app1": a1, "app2": a2}


def build_diagnostic_clipboard_report(error_text: str = "") -> str:
    """Biztonságos (API-kulcs mentes) diagnosztikai szöveget készít továbbküldéshez."""
    log_path = get_log_file_path()
    tail_lines: list[str] = []
    if log_path.exists():
        try:
            all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail_lines = all_lines[-25:]
        except OSError:
            tail_lines = ["(A naplófájl nem olvasható.)"]

    code_line = f"Hibakód: {explain_error(error_text).code}" if error_text.strip() else "Hibakód: (nincs aktív hiba)"
    lines = [
        f"=== Plex Subtitler v{__version__} Hibajelentés ===",
        code_line,
        f"Rendszer: {platform.system()} {platform.release()} | Python: {platform.python_version()}",
        f"Whisper: model={app1_config.whisper_model}, device={app1_config.whisper_device}, compute={app1_config.whisper_compute_type}",
        f"LLM: provider={app2_config.llm_provider}, model={app2_config.llm_model}, batch={app2_config.translation_batch_size}",
        f"Extrák: pgs_ocr={app1_config.enable_pgs_ocr}, opensub={app1_config.enable_opensubtitles}, auto_align={app2_config.auto_align_subtitles}, plex={app2_config.plex_refresh_enabled}",
        f"API kulcs megadva: {'IGEN (rejtve)' if bool(app2_config.llm_api_key) else 'NEM'}",
    ]
    if error_text.strip():
        lines.append("")
        lines.append("--- Hibaüzenet ---")
        lines.append(error_text.strip())
    if tail_lines:
        lines.append("")
        lines.append("--- Utolsó 25 naplósor ---")
        lines.extend(tail_lines)
    return "\n".join(lines)


def export_diagnostic_zip(output_dir: Path | None = None, error_text: str = "") -> Path:
    """Egykattintásos, kulcsmentesített hibacsomag (.zip) mentése az Asztalra vagy célmappába."""
    if output_dir is not None:
        target_dir = Path(output_dir)
    else:
        desktop = Path.home() / "Desktop"
        target_dir = desktop if desktop.is_dir() else LOGS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    zip_path = target_dir / f"PlexSubtitler_Hibacsomag_{stamp}.zip"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("diagnostic_summary.txt", build_diagnostic_clipboard_report(error_text))
        zf.writestr(
            "settings_sanitized.json",
            json.dumps(_build_sanitized_settings_dict(), indent=2, ensure_ascii=False),
        )
        log_file = get_log_file_path()
        if log_file.is_file():
            zf.write(log_file, arcname="logs/plex_subtitler.log")
        crash_native = log_file.parent / "crash_native.log"
        if crash_native.is_file() and crash_native.stat().st_size > 0:
            zf.write(crash_native, arcname="logs/crash_native.log")
        for extra_dir in {log_file.parent, LOGS_DIR}:
            hw_report = extra_dir / "hardware_report.json"
            if hw_report.is_file() and "logs/hardware_report.json" not in zf.namelist():
                zf.write(hw_report, arcname="logs/hardware_report.json")
            install_logs = sorted(extra_dir.glob("install_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            if install_logs and f"logs/{install_logs[0].name}" not in zf.namelist():
                zf.write(install_logs[0], arcname=f"logs/{install_logs[0].name}")
        movies_log = LOGS_DIR / "processed_movies.log"
        if movies_log.is_file():
            zf.write(movies_log, arcname="logs/processed_movies.log")
        if SERIES_CACHE_FILE.is_file():
            zf.write(SERIES_CACHE_FILE, arcname="series_cache.json")

    return zip_path


def check_system_health() -> list[HealthCheckItem]:
    """Gyors, nem-blokkoló állapotellenőrzés a 4 kritikus komponensre (GPU, AI, FFmpeg, Plex)."""
    items: list[HealthCheckItem] = []

    # 1. GPU / Whisper
    try:
        import torch

        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            items.append(HealthCheckItem("gpu", "ok", f"GPU: {gpu_name}", "CUDA aktív"))
        else:
            items.append(HealthCheckItem("gpu", "warn", "GPU: CPU mód", "Nincs CUDA GPU"))
    except Exception:
        items.append(HealthCheckItem("gpu", "warn", "GPU: CPU mód", "PyTorch CUDA nem elérhető"))

    # 2. AI motor
    provider = app2_config.llm_provider.lower().strip()
    if provider in ("local", "ollama"):
        try:
            import requests
            from plex_subtitler.app2_translator.llm_client import _ollama_root_url

            root = _ollama_root_url(app2_config.llm_base_url)
            resp = requests.get(f"{root}/api/tags", timeout=1.5)
            if resp.status_code == 200:
                names = [str(m.get("name", "")) for m in resp.json().get("models", [])]
                target = app2_config.llm_model.strip()
                has_model = any(n == target or n.startswith(f"{target}:") or target.startswith(f"{n}:") for n in names)
                if has_model:
                    items.append(HealthCheckItem("ai", "ok", f"AI: Ollama ({target})", "Ollama fut, modell kész"))
                else:
                    items.append(HealthCheckItem("ai", "warn", f"AI: Ollama (hiányzó: {target})", "Modell letöltésre vár"))
            else:
                items.append(HealthCheckItem("ai", "error", "AI: Ollama hiba", f"HTTP {resp.status_code}"))
        except Exception:
            items.append(HealthCheckItem("ai", "error", "AI: Ollama offline", "Indítsd el az Ollama-t"))
    else:
        if app2_config.llm_api_key.strip():
            items.append(HealthCheckItem("ai", "ok", f"AI: {provider} ({app2_config.llm_model})", "API kulcs beállítva"))
        else:
            items.append(HealthCheckItem("ai", "error", f"AI: {provider} (nincs kulcs!)", "Add meg az API kulcsot"))

    # 3. FFmpeg
    try:
        from plex_subtitler.app1_extractor.audio_extractor import resolve_ffmpeg_executable

        ff = resolve_ffmpeg_executable()
        items.append(HealthCheckItem("ffmpeg", "ok", "FFmpeg: OK", ff))
    except Exception as exc:
        items.append(HealthCheckItem("ffmpeg", "error", "FFmpeg: HIÁNYZIK", str(exc)))

    # 4. Plex
    if app2_config.plex_refresh_enabled:
        from plex_subtitler.app2_translator.plex_client import test_plex_connection

        ok, msg = test_plex_connection(app2_config.plex_url, app2_config.plex_token, timeout=2.0)
        items.append(
            HealthCheckItem("plex", "ok" if ok else "error", "Plex: OK" if ok else "Plex: Hiba", msg)
        )
    else:
        items.append(HealthCheckItem("plex", "muted", "Plex: Ki", "Opcionális könyvtárfrissítés kikapcsolva"))

    return items


def load_history_entries(
    limit: int = 30,
    cache_path: Path | None = None,
    movies_log_path: Path | None = None,
) -> list[HistoryEntry]:
    """Összegyűjti a feldolgozott és félbemaradt filmek/epizódok listáját az előzmények ablakhoz."""
    entries: list[HistoryEntry] = []
    seen_titles: set[str] = set()

    c_path = cache_path or SERIES_CACHE_FILE
    if c_path.is_file():
        try:
            data = json.loads(c_path.read_text(encoding="utf-8"))
            episodes = data.get("episodes", {}) or {}
            sorted_eps = sorted(
                episodes.items(),
                key=lambda kv: float((kv[1] or {}).get("updated_at", 0.0)),
                reverse=True,
            )
            for raw_video, info in sorted_eps:
                vid_path = Path(raw_video)
                raw_st = str((info or {}).get("status", "unknown")).lower()
                st_label = "KÉSZ" if raw_st == "done" else ("RÉSZLEGES" if raw_st == "partial" else "HIBA")
                ts = float((info or {}).get("updated_at", 0.0))
                ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts > 0 else "-"
                tr_srt = (info or {}).get("translated_srt") or ""
                detail = Path(tr_srt).name if tr_srt else "Folytatható checkpointtól"
                entries.append(
                    HistoryEntry(
                        timestamp=ts_str,
                        status=st_label,
                        title=vid_path.stem,
                        detail=detail,
                        file_path=vid_path,
                    )
                )
                seen_titles.add(vid_path.stem.lower())
        except Exception:
            pass

    m_path = movies_log_path or (LOGS_DIR / "processed_movies.log")
    if m_path.is_file():
        try:
            lines = [ln.strip() for ln in m_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
            for line in reversed(lines):
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 3:
                    continue
                ts_str = parts[0][:16]
                raw_status = parts[1]
                st_label = "KÉSZ" if "KÉSZ" in raw_status else ("RÉSZLEGES" if "RÉSZLEGES" in raw_status else "HIBA")
                title = parts[2].replace("Film:", "").strip()
                if title.lower() in seen_titles and st_label == "KÉSZ":
                    continue
                detail = " | ".join([raw_status, *parts[3:]])
                entries.append(
                    HistoryEntry(
                        timestamp=ts_str,
                        status=st_label,
                        title=title,
                        detail=detail,
                        file_path=None,
                    )
                )
        except Exception:
            pass

    return entries[:limit]
