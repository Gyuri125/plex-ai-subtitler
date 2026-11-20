"""v1.3.0 tesztek: előzetes hardver-felmérés, dinamikus profilok, golyóálló naplózás és Ultimate telepítő."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from plex_subtitler import logger as app_logger
from plex_subtitler.app1_extractor import hardware_manager
from plex_subtitler.config import SettingsManager


def test_hardware_inventory_and_dynamic_tier_profiles(monkeypatch) -> None:
    hw = hardware_manager.HardwareManager()

    # 1. CUDA_ULTRA (pl. RTX 3080 12 GB)
    hw.has_cuda = True
    hw.vram_gb = 12.0
    hw.ram_total_gb = 32.0
    p_ultra = hw.get_full_recommended_profile()
    assert p_ultra["profile_tier"] == "CUDA_ULTRA"
    assert p_ultra["whisper_model"] == "large-v3"
    assert p_ultra["llm_model"] == "gemma3:12b"
    assert p_ultra["translation_batch_size"] == 12

    # 2. CUDA_MID (8 GB)
    hw.vram_gb = 8.0
    p_mid = hw.get_full_recommended_profile()
    assert p_mid["profile_tier"] == "CUDA_MID"
    assert p_mid["llm_model"] == "gemma3:4b"
    assert p_mid["translation_batch_size"] == 10

    # 3. CUDA_ENTRY (6 GB)
    hw.vram_gb = 6.0
    p_entry = hw.get_full_recommended_profile()
    assert p_entry["profile_tier"] == "CUDA_ENTRY"
    assert p_entry["whisper_model"] == "medium"
    assert p_entry["translation_batch_size"] == 8

    # 4. CPU_ONLY (kevés RAM vs sok RAM)
    hw.has_cuda = False
    hw.vram_gb = 0.0
    hw.ram_total_gb = 16.0
    assert hw.get_full_recommended_profile()["llm_model"] == "gemma3:4b"
    hw.ram_total_gb = 8.0
    assert hw.get_full_recommended_profile()["llm_model"] == "gemma3:1b"

    # 5. Fizikai NVIDIA GPU jelen van, de CPU-s PyTorch -> figyelmeztetés
    monkeypatch.setattr(hardware_manager, "_probe_nvidia_smi", lambda: ("NVIDIA GeForce RTX 3080", 12.0, "566.14"))
    inv = hw.collect_system_inventory()
    assert inv.nvidia_smi_gpu == "NVIDIA GeForce RTX 3080"
    assert any("PyTorch nem CUDA-képes" in w for w in inv.warnings)


def test_logger_writable_fallback_and_fault_handler(tmp_path: Path, monkeypatch) -> None:
    blocked = tmp_path / "readonly_target"
    fallback_dir = tmp_path / "fallback_logs"

    monkeypatch.setattr(app_logger, "_candidate_log_dirs", lambda _pref=None: [blocked, fallback_dir])

    orig_write_text = Path.write_text

    def _fail_on_blocked(self: Path, *args, **kwargs):
        if self.parent == blocked:
            raise PermissionError("Read-only directory")
        return orig_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _fail_on_blocked)

    resolved = app_logger.resolve_writable_log_dir(blocked)
    assert resolved == fallback_dir
    assert fallback_dir.is_dir()

    app_logger.install_global_exception_logging()
    assert (app_logger.get_active_log_dir() / "crash_native.log").exists()


def test_config_atomic_save_and_corrupt_json_recovery(tmp_path: Path) -> None:
    cfg_path = tmp_path / "settings.json"
    cfg_path.write_text("{BROKEN JSON!!!", encoding="utf-8")

    mgr = SettingsManager(settings_path=cfg_path, env_path=tmp_path / "missing.env")
    assert mgr.settings_existed_on_load is False
    assert (tmp_path / "settings.json.corrupt.bak").is_file()

    mgr.app1.whisper_model = "medium"
    mgr.save()
    assert cfg_path.is_file()
    assert not (tmp_path / "settings.json.tmp").exists()
    reloaded = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert reloaded["app1"]["whisper_model"] == "medium"


def test_cli_hardware_scan_and_tk_callback_logging(monkeypatch) -> None:
    main_py = Path(__file__).resolve().parent.parent / "main.py"
    res = subprocess.run(
        [sys.executable, str(main_py), "--hardware-scan"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert "inventory" in data and "recommended_profile" in data
    assert data["inventory"]["cpu_cores"] >= 1
    assert data["recommended_profile"]["profile_tier"] in ("CUDA_ULTRA", "CUDA_MID", "CUDA_ENTRY", "CPU_ONLY")

    from plex_subtitler.app1_extractor import gui_dialogs

    shown: list[str] = []
    monkeypatch.setattr(gui_dialogs, "_show_friendly_error_dialog", lambda _w, err: shown.append(str(err)))
    try:
        raise RuntimeError("Szimulált GUI gombnyomás hiba")
    except RuntimeError as exc:
        gui_dialogs._handle_tk_callback_exception(None, type(exc), exc, exc.__traceback__)  # type: ignore[arg-type]
    assert shown == ["Szimulált GUI gombnyomás hiba"]


def test_ultimate_installer_files_exist() -> None:
    src_dir = Path(__file__).resolve().parent.parent
    assert (src_dir / "TELEPITO_ULTIMATE.bat").is_file()
    assert (src_dir / "setup_ultimate.ps1").is_file()

    bat_txt = (src_dir / "TELEPITO_ULTIMATE.bat").read_text(encoding="utf-8")
    assert "install_bat_" in bat_txt
    assert "setup_ultimate.ps1" in bat_txt
    ps_txt = (src_dir / "setup_ultimate.ps1").read_text(encoding="utf-8")
    assert "hardware_report.json" in ps_txt
    assert "Microsoft.VCRedist" in ps_txt
    assert "Python.Python.3.12" in ps_txt
    assert "Gyan.FFmpeg" in ps_txt
    assert "Tls12" in ps_txt
    assert "WindowsApps" in ps_txt
    assert "/VERYSILENT" in ps_txt
    assert "download_model" in ps_txt
    assert "OLLAMA_FLASH_ATTENTION" in ps_txt
    assert "--quick" in ps_txt
