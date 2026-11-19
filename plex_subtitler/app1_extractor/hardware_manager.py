"""Dinamikus előzetes hardver-felmérés, profil-optimalizálás és Windows rendszer-segédek."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from plex_subtitler.config import ROOT_DIR, app1_config, app2_config, settings_manager
from plex_subtitler.logger import get_logger

log = get_logger(__name__, level=app1_config.log_level)

_AUTOSTART_TASK_NAME = "PlexSubtitlerAutoWatcher"
_QUNS_RUNNING_D3D_FULL_SCREEN = 3


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


@dataclass
class SystemInventory:
    """Teljes hardver- és környezet-felmérési pillanatkép."""

    os_info: str
    cpu_name: str
    cpu_cores: int
    ram_total_gb: float
    ram_available_gb: float
    disk_free_gb: float
    has_cuda: bool
    gpu_name: str
    vram_gb: float
    nvidia_smi_gpu: str = ""
    nvidia_smi_vram_gb: float = 0.0
    nvidia_driver: str = ""
    ffmpeg_available: bool = False
    tesseract_available: bool = False
    ollama_available: bool = False
    profile_tier: str = "CPU_ONLY"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _get_ram_gb() -> tuple[float, float]:
    """Visszaadja a (teljes_GB, szabad_GB) RAM méretet külső csomag nélkül."""
    if sys.platform == "win32":
        try:
            stat = _MemoryStatusEx()
            stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                gb = 1024.0**3
                return round(stat.ullTotalPhys / gb, 1), round(stat.ullAvailPhys / gb, 1)
        except Exception:
            pass
    try:
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
        vals: dict[str, int] = {}
        for line in meminfo.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                parts = v.strip().split()
                if parts and parts[0].isdigit():
                    vals[k.strip()] = int(parts[0])
        total = round(vals.get("MemTotal", 0) / (1024.0**2), 1)
        avail = round(vals.get("MemAvailable", vals.get("MemFree", 0)) / (1024.0**2), 1)
        return total, avail
    except Exception:
        return 0.0, 0.0


def _probe_nvidia_smi() -> tuple[str, float, str]:
    """Lekérdezi az nvidia-smi-t (GPU név, VRAM GB, driver verzió) CUDA-töltés nélkül is."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        win_default = Path(r"C:\Windows\System32\nvidia-smi.exe")
        if win_default.is_file():
            smi = str(win_default)
    if not smi:
        return "", 0.0, ""
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        res = subprocess.run(
            [smi, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=creation_flags,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            first_line = res.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first_line.split(",")]
            if len(parts) >= 2:
                gpu = parts[0]
                vram_gb = round(float(parts[1]) / 1024.0, 1)
                driver = parts[2] if len(parts) >= 3 else ""
                return gpu, vram_gb, driver
    except Exception:
        pass
    return "", 0.0, ""


def is_fullscreen_game_active() -> tuple[bool, str]:
    """Ellenőrzi Windows-on, hogy fut-e teljes képernyős Direct3D/Vulkan játék."""
    if sys.platform != "win32":
        return False, ""
    try:
        state = ctypes.c_int(0)
        hr = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        if hr == 0 and state.value == _QUNS_RUNNING_D3D_FULL_SCREEN:
            return True, "Teljes képernyős 3D játék fut (D3D Fullscreen)"
    except Exception:
        pass
    return False, ""


def wait_while_gaming(
    stop_event: threading.Event | None = None,
    poll_sec: float = 10.0,
) -> None:
    """Háttérdaemonoknál szünetelteti az AI/Whisper feldolgozást játék közben."""
    if not app1_config.pause_when_gaming:
        return
    logged_pause = False
    while not (stop_event and stop_event.is_set()):
        active, reason = is_fullscreen_game_active()
        if not active:
            if logged_pause:
                log.info("▶️ Játék befejeződött — háttérfeldolgozás folytatódik.")
            return
        if not logged_pause:
            log.info("🎮 %s — háttér-AI szüneteltetve a játék végéig...", reason)
            logged_pause = True
        if stop_event:
            stop_event.wait(poll_sec)
        else:
            time.sleep(poll_sec)


def get_windows_autostart_enabled() -> bool:
    """Ellenőrzi, hogy létezik-e a Windows Task Scheduler automatikus figyelő feladat."""
    if sys.platform != "win32":
        return False
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        res = subprocess.run(
            ["schtasks", "/Query", "/TN", _AUTOSTART_TASK_NAME],
            capture_output=True,
            timeout=5,
            creationflags=creation_flags,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def set_windows_autostart(enabled: bool) -> tuple[bool, str]:
    """Létrehozza vagy törli a bejelentkezéskori háttér-figyelő feladatot (Task Scheduler)."""
    if sys.platform != "win32":
        return False, "Csak Windows rendszeren támogatott."
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    if not enabled:
        try:
            res = subprocess.run(
                ["schtasks", "/Delete", "/TN", _AUTOSTART_TASK_NAME, "/F"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=creation_flags,
                check=False,
            )
            if res.returncode == 0 or not get_windows_autostart_enabled():
                return True, "Automatikus háttér-indítás kikapcsolva."
            return False, (res.stderr or res.stdout).strip()
        except Exception as exc:
            return False, str(exc)

    if getattr(sys, "frozen", False):
        exe = str(Path(sys.executable).resolve())
        arg = "--cli --app both --scan-existing"
        work_dir = str(Path(exe).parent)
    else:
        py = str(Path(sys.executable).resolve())
        pyw = Path(py).with_name("pythonw.exe")
        exe = str(pyw if pyw.is_file() else Path(py))
        main_py = str((ROOT_DIR / "main.py").resolve())
        arg = f'"{main_py}" --cli --app both --scan-existing'
        work_dir = str(ROOT_DIR.resolve())

    ps_cmd = (
        f"$action = New-ScheduledTaskAction -Execute '{exe}' -Argument '{arg}' -WorkingDirectory '{work_dir}'; "
        f"$trigger = New-ScheduledTaskTrigger -AtLogOn; "
        f"$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 0); "
        f"Register-ScheduledTask -TaskName '{_AUTOSTART_TASK_NAME}' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=creation_flags,
            check=False,
        )
        if res.returncode == 0:
            return True, "Windows bejelentkezéskori háttér-figyelő bekapcsolva (játék közben automatikusan szünetel!)."
        return False, (res.stderr or res.stdout).strip()
    except Exception as exc:
        return False, str(exc)


class HardwareManager:
    """Hardver-képességeket vizsgáló és teljes Whisper+AI profilt optimalizáló osztály."""

    def __init__(self) -> None:
        self.has_cuda: bool = False
        self.vram_gb: float = 0.0
        self.gpu_name: str = "N/A"
        self.ram_total_gb: float = 0.0
        self.ram_available_gb: float = 0.0
        self.cpu_cores: int = os.cpu_count() or 1
        self.disk_free_gb: float = 0.0
        self._detect_hardware()

    def _detect_hardware(self) -> None:
        """Felméri a GPU-t, RAM-ot és szabad tárhelyet."""
        self.ram_total_gb, self.ram_available_gb = _get_ram_gb()
        try:
            usage = shutil.disk_usage(ROOT_DIR)
            self.disk_free_gb = round(usage.free / (1024.0**3), 1)
        except OSError:
            self.disk_free_gb = 0.0

        try:
            import torch

            if torch.cuda.is_available():
                self.has_cuda = True
                self.gpu_name = torch.cuda.get_device_name(0)
                total_mem = torch.cuda.get_device_properties(0).total_memory
                self.vram_gb = total_mem / (1024**3)
                log.info(
                    "💻 Hardver: GPU=%s (%.1f GB VRAM) | RAM=%.1f GB | CPU=%d mag | Szabad hely=%.1f GB",
                    self.gpu_name,
                    self.vram_gb,
                    self.ram_total_gb,
                    self.cpu_cores,
                    self.disk_free_gb,
                )
            else:
                log.info(
                    "🖥️ Hardver: CPU mód (nincs aktív CUDA) | RAM=%.1f GB | CPU=%d mag | Szabad hely=%.1f GB",
                    self.ram_total_gb,
                    self.cpu_cores,
                    self.disk_free_gb,
                )
        except ImportError:
            log.warning("⚠️ A 'torch' csomag nem található. Hardver-detektálás CPU módra áll vissza.")
        except Exception as exc:
            log.warning("⚠️ Hiba a hardver-detektálás során: %s. CPU módra áll vissza.", exc)
            self.has_cuda = False
            self.vram_gb = 0.0

    def get_profile_tier(self) -> str:
        """Besorolja a gépet 4 teljesítmény-kategóriába a mért GPU/VRAM alapján."""
        if self.has_cuda:
            if self.vram_gb >= 11.0:
                return "CUDA_ULTRA"
            if self.vram_gb >= 7.5:
                return "CUDA_MID"
            if self.vram_gb >= 4.0:
                return "CUDA_ENTRY"
        return "CPU_ONLY"

    def get_recommended_profile(self) -> dict[str, str]:
        """Visszaadja a gép specifikációihoz ajánlott Whisper profilt."""
        if self.has_cuda:
            if self.vram_gb >= 8.0:
                return {
                    "whisper_device": "cuda",
                    "whisper_model": "large-v3",
                    "whisper_compute_type": "float16",
                }
            if self.vram_gb >= 4.0:
                return {
                    "whisper_device": "cuda",
                    "whisper_model": "medium",
                    "whisper_compute_type": "float16",
                }
            return {
                "whisper_device": "cuda",
                "whisper_model": "small",
                "whisper_compute_type": "float16",
            }
        return {
            "whisper_device": "cpu",
            "whisper_model": "small",
            "whisper_compute_type": "int8",
        }

    def get_full_recommended_profile(self) -> dict[str, Any]:
        """Teljes Whisper + lokális Ollama AI profil ajánlása a mért hardverhez."""
        whisper = self.get_recommended_profile()
        tier = self.get_profile_tier()
        if tier == "CUDA_ULTRA":
            llm_model, batch = "gemma3:12b", 12
        elif tier == "CUDA_MID":
            llm_model, batch = "gemma3:4b", 10
        elif tier == "CUDA_ENTRY":
            llm_model, batch = "gemma3:4b", 8
        else:
            llm_model = "gemma3:4b" if self.ram_total_gb >= 12.0 else "gemma3:1b"
            batch = 6
        return {
            **whisper,
            "profile_tier": tier,
            "llm_model": llm_model,
            "translation_batch_size": batch,
            "unload_whisper_before_translate": True,
        }

    def collect_system_inventory(self) -> SystemInventory:
        """Teljes előzetes hardver- és függőség-felmérés (CPU, RAM, GPU, tárhely, FFmpeg, OCR, Ollama)."""
        smi_gpu, smi_vram, smi_driver = _probe_nvidia_smi()

        ffmpeg_ok = bool(shutil.which("ffmpeg"))
        if not ffmpeg_ok:
            try:
                import imageio_ffmpeg

                ffmpeg_ok = bool(imageio_ffmpeg.get_ffmpeg_exe())
            except Exception:
                ffmpeg_ok = False

        tesseract_ok = bool(shutil.which("tesseract")) or Path(
            r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        ).is_file()
        ollama_ok = bool(shutil.which("ollama")) or (
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        ).is_file()

        warnings: list[str] = []
        if smi_gpu and not self.has_cuda:
            warnings.append(
                f"NVIDIA GPU ({smi_gpu}, {smi_vram:.1f} GB) jelen van, de a telepített PyTorch nem CUDA-képes! "
                "Futtasd a TELEPITO_ULTIMATE.bat telepítőt a GPU-gyorsítás aktiválásához."
            )
        if self.disk_free_gb > 0 and self.disk_free_gb < 15.0:
            warnings.append(
                f"Kevés szabad tárhely ({self.disk_free_gb:.1f} GB). A nagy AI modellekhez legalább 15–20 GB ajánlott."
            )
        if self.ram_total_gb > 0 and self.ram_total_gb < 8.0:
            warnings.append(
                f"Alacsony rendszermemória ({self.ram_total_gb:.1f} GB RAM). Kisebb modell (gemma3:1b / small) ajánlott."
            )
        if not ffmpeg_ok:
            warnings.append("FFmpeg nem található a rendszerben.")
        if not tesseract_ok:
            warnings.append("Tesseract OCR nincs telepítve (Blu-ray PGS képfeliratokhoz ajánlott; nélküle Whisper fut).")

        return SystemInventory(
            os_info=f"{platform.system()} {platform.release()} ({platform.machine()})",
            cpu_name=platform.processor() or "Ismeretlen CPU",
            cpu_cores=self.cpu_cores,
            ram_total_gb=self.ram_total_gb,
            ram_available_gb=self.ram_available_gb,
            disk_free_gb=self.disk_free_gb,
            has_cuda=self.has_cuda,
            gpu_name=self.gpu_name,
            vram_gb=round(self.vram_gb, 1),
            nvidia_smi_gpu=smi_gpu,
            nvidia_smi_vram_gb=smi_vram,
            nvidia_driver=smi_driver,
            ffmpeg_available=ffmpeg_ok,
            tesseract_available=tesseract_ok,
            ollama_available=ollama_ok,
            profile_tier=self.get_profile_tier(),
            warnings=warnings,
        )

    def apply_auto_fallback(self, force_save: bool = True, apply_llm: bool = False) -> dict[str, Any]:
        """Alkalmazza a javasolt profilt az aktuális konfigurációra."""
        profile = self.get_full_recommended_profile() if apply_llm else self.get_recommended_profile()
        log.info(
            "⚙️ Auto-Fallback alkalmazása: Eszköz='%s', Modell='%s', Típus='%s'%s",
            profile["whisper_device"],
            profile["whisper_model"],
            profile["whisper_compute_type"],
            f", LLM='{profile['llm_model']}' (batch={profile['translation_batch_size']})" if apply_llm else "",
        )
        app1_config.whisper_device = str(profile["whisper_device"])
        app1_config.whisper_model = str(profile["whisper_model"])
        app1_config.whisper_compute_type = str(profile["whisper_compute_type"])
        if apply_llm:
            app1_config.unload_whisper_before_translate = True
            if not app2_config.llm_api_key:
                app2_config.llm_provider = "local"
                app2_config.llm_model = str(profile["llm_model"])
                app2_config.llm_base_url = app2_config.llm_base_url or "http://localhost:11434/v1"
                app2_config.translation_batch_size = int(profile["translation_batch_size"])
        if force_save:
            settings_manager.save()
        return profile


def run_first_start_hardware_setup() -> SystemInventory | None:
    """Első indításkor (amikor még nincs settings.json) felméri a gépet és elmenti az optimális profilt."""
    if settings_manager.settings_existed_on_load:
        return None
    hw = HardwareManager()
    inv = hw.collect_system_inventory()
    hw.apply_auto_fallback(force_save=True, apply_llm=True)
    log.info(
        "🧭 Első indítási hardver-felmérés kész | Profil=%s | GPU=%s (%.1f GB) | RAM=%.1f GB | Szabad hely=%.1f GB",
        inv.profile_tier,
        inv.gpu_name,
        inv.vram_gb,
        inv.ram_total_gb,
        inv.disk_free_gb,
    )
    for w in inv.warnings:
        log.warning("⚠️ Hardver figyelmeztetés: %s", w)
    return inv
