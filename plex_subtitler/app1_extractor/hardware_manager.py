"""
hardware_manager.py - Dinamikus hardver-felismerés és Whisper profil beállítás.

Felelősség:
- Felméri a rendelkezésre álló GPU (CUDA) hardvert és memóriát.
- Olyan Whisper paramétereket (modell méret, eszköz, kvantálás) ajánl/állít be,
  amelyek stabilan futnak a gépen, elkerülve az OOM (Out Of Memory) hibákat.
"""

from __future__ import annotations
import math

from ..logger import get_logger
from ..config import app1_config, settings_manager

log = get_logger(__name__, level=app1_config.log_level)

class HardwareManager:
    """
    Hardver-képességeket vizsgáló és optimalizáló osztály.
    """
    def __init__(self):
        self.has_cuda = False
        self.vram_gb = 0.0
        self.gpu_name = "N/A"
        
        self._detect_hardware()

    def _detect_hardware(self) -> None:
        """Felméri a rendelkezésre álló hardvert a torch segítségével."""
        try:
            import torch
            
            self.has_cuda = torch.cuda.is_available()
            
            if self.has_cuda:
                self.gpu_name = torch.cuda.get_device_name(0)
                # get_device_properties() memóriája bájtokban van
                total_memory_bytes = torch.cuda.get_device_properties(0).total_memory
                self.vram_gb = total_memory_bytes / (1024 ** 3)
                log.info("GPU detektálva: %s (VRAM: %.1f GB)", self.gpu_name, self.vram_gb)
            else:
                log.info("Csak CPU detektálva (vagy nincs CUDA telepítve).")
                
        except ImportError:
            log.warning("A 'torch' csomag nem található. Hardver-detektálás CPU módra áll vissza.")
            self.has_cuda = False
        except Exception as e:
            log.warning("Hiba a hardver-detektálás során: %s. CPU módra áll vissza.", e)
            self.has_cuda = False

    def get_recommended_profile(self) -> dict[str, str]:
        """
        Visszaadja a gép specifikációihoz ajánlott Whisper profilt.
        """
        if self.has_cuda:
            if self.vram_gb >= 8.0:
                # Erős GPU (pl. RTX 3080, 4070)
                return {
                    "whisper_device": "cuda",
                    "whisper_model": "large-v3",
                    "whisper_compute_type": "float16"
                }
            elif self.vram_gb >= 4.0:
                # Közepes GPU
                return {
                    "whisper_device": "cuda",
                    "whisper_model": "medium",
                    "whisper_compute_type": "float16"
                }
            else:
                # Gyenge GPU (kiszorítósdi)
                return {
                    "whisper_device": "cuda",
                    "whisper_model": "small",
                    "whisper_compute_type": "float16"
                }
        else:
            # Csak CPU
            # A 'small' modell CPU-n még vállalható sebességgel fut.
            return {
                "whisper_device": "cpu",
                "whisper_model": "small",
                "whisper_compute_type": "int8"
            }

    def apply_auto_fallback(self, force_save: bool = True) -> dict[str, str]:
        """
        Alkalmazza a javasolt hardver-profilt az aktuális konfigurációra,
        majd opcionálisan elmenti a settings.json-be.
        """
        profile = self.get_recommended_profile()
        
        log.info(
            "Auto-Fallback alkalmazása: Eszköz='%s', Modell='%s', Típus='%s'",
            profile["whisper_device"],
            profile["whisper_model"],
            profile["whisper_compute_type"]
        )
        
        app1_config.whisper_device = profile["whisper_device"]
        app1_config.whisper_model = profile["whisper_model"]
        app1_config.whisper_compute_type = profile["whisper_compute_type"]
        
        if force_save:
            settings_manager.save()
            
        return profile
