"""Önellenőrző hardver-, GPU-, Ollama-, Whisper- és FFmpeg-diagnosztikai szkript."""

from __future__ import annotations

import argparse
import sys

from plex_subtitler.app1_extractor.audio_extractor import resolve_ffmpeg_executable
from plex_subtitler.app1_extractor.hardware_manager import HardwareManager
from plex_subtitler.app2_translator.llm_client import ensure_ollama_running, get_available_models
from plex_subtitler.app2_translator.srt_formatter import format_srt_blocks
from plex_subtitler.app2_translator.srt_parser import SrtBlock
from plex_subtitler.config import app1_config, app2_config
from plex_subtitler.logger import get_log_file_path


def run_diagnostics(live_llm: bool = False) -> int:
    failures = 0
    print("=" * 68)
    print("Plex Subtitler — Rendszer-, Hardver- és GPU-diagnosztika")
    print("=" * 68)

    print(f"[INFO] Python: {sys.version.split()[0]} | Naplófájl: {get_log_file_path()}")

    # 1. Teljes hardver-felmérés
    hw = HardwareManager()
    inv = hw.collect_system_inventory()
    rec = hw.get_full_recommended_profile()
    print(f"[INFO] OS: {inv.os_info}")
    print(
        f"[INFO] CPU: {inv.cpu_cores} mag | RAM: {inv.ram_total_gb:.1f} GB (szabad: {inv.ram_available_gb:.1f} GB) "
        f"| Szabad tárhely: {inv.disk_free_gb:.1f} GB"
    )
    if inv.has_cuda:
        print(f"[OK]   NVIDIA CUDA GPU: {inv.gpu_name} ({inv.vram_gb:.1f} GB VRAM) | Profil: {inv.profile_tier}")
    else:
        print(f"[INFO] GPU mód: CPU ({inv.profile_tier})")
        if inv.nvidia_smi_gpu:
            print(
                f"[FIGYELEM] Fizikai NVIDIA kártya található ({inv.nvidia_smi_gpu}, {inv.nvidia_smi_vram_gb:.1f} GB, "
                f"driver {inv.nvidia_driver}), de a PyTorch jelenleg CPU-s!"
            )

    print(
        f"[INFO] Ajánlott profil: Whisper={rec['whisper_model']} ({rec['whisper_device']}/{rec['whisper_compute_type']}), "
        f"Ollama={rec['llm_model']} (batch={rec['translation_batch_size']})"
    )
    print(
        f"[INFO] Eszközök: FFmpeg={'OK' if inv.ffmpeg_available else 'HIÁNYZIK'} | "
        f"Tesseract OCR={'OK' if inv.tesseract_available else 'Nincs (Whisper fallback)'} | "
        f"Ollama={'OK' if inv.ollama_available else 'Nincs'}"
    )
    for warn in inv.warnings:
        print(f"[FIGYELEM] {warn}")

    # 2. FFmpeg
    try:
        ffmpeg_path = resolve_ffmpeg_executable()
        print(f"[OK]   FFmpeg elérhető: {ffmpeg_path}")
    except Exception as exc:
        print(f"[HIBA] FFmpeg nem található: {exc}")
        failures += 1

    # 3. CTranslate2 / faster-whisper
    try:
        import ctranslate2

        cuda_count = ctranslate2.get_cuda_device_count()
        print(f"[OK]   ctranslate2 CUDA eszközök száma: {cuda_count}")
    except Exception as exc:
        print(f"[FIGYELEM] ctranslate2 ellenőrzés hiba: {exc}")

    # 4. SRT formázó teszt
    sample = [
        SrtBlock(
            1,
            "00:00:01,000 --> 00:00:05,000",
            "Ez egy nagyon hosszú magyar mondat, amit a tévé képernyőjére két sorba érdemes törni.",
        ),
        SrtBlock(2, "00:00:04,500 --> 00:00:07,000", "Következő sor"),
    ]
    formatted = format_srt_blocks(sample, max_chars=42, max_lines=2)
    if "\n" in formatted[0].text and "00:00:04,490" in formatted[0].timestamp:
        print("[OK]   TV/Plex SRT sortörő és átfedés-javító működik.")
    else:
        print("[HIBA] SRT formázó teszt sikertelen.")
        failures += 1

    # 5. Ollama ellenőrzés
    print(f"[INFO] Aktív LLM beállítás: provider={app2_config.llm_provider}, model={app2_config.llm_model}")
    if app2_config.llm_provider.lower().strip() in ("local", "ollama"):
        if ensure_ollama_running(app2_config.llm_base_url, timeout_sec=5.0):
            print("[OK]   Ollama szerver elérhető.")
            try:
                models = get_available_models("local", local_url=app2_config.llm_base_url)
                print(f"[INFO] Elérhető Ollama modellek: {models}")
                if any(app2_config.llm_model in m for m in models):
                    print(f"[OK]   A beállított modell ({app2_config.llm_model}) telepítve van.")
                else:
                    print(
                        f"[FIGYELEM] A(z) '{app2_config.llm_model}' modell még nincs lehúzva! "
                        f"Futtasd: ollama pull {app2_config.llm_model}"
                    )
            except Exception as exc:
                print(f"[FIGYELEM] Modell-lista lekérdezés sikertelen: {exc}")
        else:
            print("[FIGYELEM] Ollama szerver nem elérhető (localhost:11434).")

    if live_llm and app2_config.llm_provider.lower().strip() in ("local", "ollama"):
        from plex_subtitler.app2_translator.llm_client import get_llm_client

        client = get_llm_client()
        out = client.translate_chunk("[1] Hello, how are you today?")
        print(f"[OK]   Élő helyi AI próba válasz: {out!r}")

    print(
        f"[INFO] Aktív Whisper profil: {app1_config.whisper_model} / "
        f"{app1_config.whisper_device} / {app1_config.whisper_compute_type}"
    )
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Plex Subtitler Hardver, GPU és Ollama diagnosztika")
    parser.add_argument("--live-llm", action="store_true", help="Futtat egy 1-soros élő próbafordítást is.")
    args = parser.parse_args()
    sys.exit(run_diagnostics(live_llm=args.live_llm))
