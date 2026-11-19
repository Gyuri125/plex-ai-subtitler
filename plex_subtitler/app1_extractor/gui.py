"""CustomTkinter alapú fő grafikus vezérlőpult (Egyszerű és Haladó nézet)."""

from __future__ import annotations

import logging
import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from plex_subtitler.app1_extractor.file_processor import SUPPORTED_EXTENSIONS, FileProcessor
from plex_subtitler.app1_extractor.gui_dialogs import (
    DND_FILES,
    _HAS_DND,
    _TUTORIAL_ADVANCED_TEXT,
    _TUTORIAL_SIMPLE_TEXT,
    GuiLogHandler,
    HealthStatusBar,
    ToolTip,
    _apply_window_icon,
    _ask_stream_selection_if_needed,
    _BaseCTk,
    _format_glossary_text,
    _open_hardware_report_window,
    _open_history_window,
    _open_tutorial_window,
    _parse_glossary_text,
    _show_friendly_error_dialog,
)
from plex_subtitler.app1_extractor.gui_quick import QuickSubtitlerWindow, run_quick_mode
from plex_subtitler.app1_extractor.hardware_manager import (
    HardwareManager,
    get_windows_autostart_enabled,
    set_windows_autostart,
)
from plex_subtitler.app1_extractor.pipeline import run_batch_one_click, run_one_click_pipeline
from plex_subtitler.app1_extractor.series_cache import SeriesCacheManager
from plex_subtitler.app1_extractor.watcher import WatcherDaemon
from plex_subtitler.app2_translator.llm_client import get_available_models
from plex_subtitler.app2_translator.plex_client import test_plex_connection
from plex_subtitler.app2_translator.srt_formatter import shift_srt_file
from plex_subtitler.app2_translator.sync_verifier import (
    auto_align_subtitle,
    verify_subtitle_sync,
)
from plex_subtitler.app2_translator.translator_daemon import (
    TranslatorDaemon,
    process_single_srt,
)
from plex_subtitler.config import app1_config, app2_config, settings_manager
from plex_subtitler.error_helper import (
    build_diagnostic_clipboard_report,
    explain_error,
    export_diagnostic_zip,
)
from plex_subtitler.logger import get_log_file_path, get_logger

log = get_logger("GUI", level="INFO")

__all__ = [
    "PlexSubtitlerApp",
    "QuickSubtitlerWindow",
    "ToolTip",
    "GuiLogHandler",
    "run_gui",
    "run_quick_mode",
    "run_one_click_pipeline",
    "run_batch_one_click",
    "_BaseCTk",
    "_HAS_DND",
    "_apply_window_icon",
    "_parse_glossary_text",
    "_format_glossary_text",
]

_SETTINGS_DEBOUNCE_MS = 400


class PlexSubtitlerApp(_BaseCTk):
    """Fő grafikus alkalmazásablak."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Plex Subtitler - Extractor & Translator")
        self.geometry("880x980")
        self.resizable(True, True)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        _apply_window_icon(self)

        self.extractor_daemon: WatcherDaemon | None = None
        self.translator_daemon: TranslatorDaemon | None = None
        self._single_file_stop_event: threading.Event | None = None
        self._single_srt_stop_event: threading.Event | None = None
        self._simple_stop_event: threading.Event | None = None
        self._simple_series_watcher: WatcherDaemon | None = None
        self._simple_cache = SeriesCacheManager()
        self._last_error_text: str = ""
        self._save_debounce_id: str | None = None
        self._health_after_id: str | None = None
        self._gui_log_handler: GuiLogHandler | None = None
        self._gui_loggers: list[logging.Logger] = []

        self._build_ui()
        self._load_settings_to_ui()
        self._setup_gui_logger()
        self._register_drag_and_drop()
        self._health_after_id = self.after(250, self.health_bar.refresh_async)
        log.info(
            "🚀 Plex Subtitler GUI elindult | Nézet=%s | Whisper=%s (%s) | AI=%s (%s) | Napló='%s'",
            app2_config.ui_mode,
            app1_config.whisper_model,
            app1_config.whisper_device,
            app2_config.llm_provider,
            app2_config.llm_model,
            get_log_file_path(),
        )

    def destroy(self) -> None:
        if hasattr(self, "health_bar"):
            self.health_bar._closed = True
        if self._health_after_id is not None:
            try:
                self.after_cancel(self._health_after_id)
            except Exception:
                pass
            self._health_after_id = None
        if self._save_debounce_id is not None:
            try:
                self.after_cancel(self._save_debounce_id)
            except Exception:
                pass
            self._save_debounce_id = None
            try:
                self._save_settings_from_ui()
            except Exception:
                pass
        if self._gui_log_handler is not None:
            for lgr in self._gui_loggers:
                try:
                    lgr.removeHandler(self._gui_log_handler)
                except Exception:
                    pass
            self._gui_log_handler = None
            self._gui_loggers.clear()
        super().destroy()

    def _register_drag_and_drop(self) -> None:
        if not _HAS_DND or DND_FILES is None:
            return
        try:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_main_drop)
        except Exception as exc:
            log.debug("Főablak Drag & Drop regisztráció kihagyva: %s", exc)

    def _on_main_drop(self, event: tk.Event) -> None:
        raw = getattr(event, "data", "")
        if not raw:
            return
        try:
            dropped = [Path(p) for p in self.tk.splitlist(raw) if p]
        except Exception:
            dropped = [Path(raw)]
        if dropped:
            log.info("🖱️ Behúzva %d elem: %s", len(dropped), ", ".join(p.name for p in dropped[:5]))
            self._start_simple_paths(dropped)

    def _setup_gui_logger(self) -> None:
        handler = GuiLogHandler(self.log_box)
        self._gui_log_handler = handler
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        self._gui_loggers = [root_logger]
        for logger_name in (
            "GUI",
            "plex_subtitler.app1_extractor.audio_extractor",
            "plex_subtitler.app1_extractor.file_processor",
            "plex_subtitler.app1_extractor.opensubtitles_client",
            "plex_subtitler.app1_extractor.series_cache",
            "plex_subtitler.app1_extractor.transcriber",
            "plex_subtitler.app1_extractor.watcher",
            "plex_subtitler.app1_extractor.hardware_manager",
            "plex_subtitler.app2_translator.srt_parser",
            "plex_subtitler.app2_translator.llm_client",
            "plex_subtitler.app2_translator.plex_client",
            "plex_subtitler.app2_translator.sync_verifier",
            "plex_subtitler.app2_translator.translator_daemon",
        ):
            lgr = logging.getLogger(logger_name)
            lgr.addHandler(handler)
            lgr.propagate = False
            self._gui_loggers.append(lgr)

    def _build_ui(self) -> None:
        self.main_frame = ctk.CTkFrame(self)
        self.main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        header_row = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        header_row.pack(fill="x", padx=10, pady=(5, 8))

        self.title_label = ctk.CTkLabel(
            header_row,
            text="🎬 Plex AI Subtitler",
            font=ctk.CTkFont(size=22, weight="bold"),
        )
        self.title_label.pack(side="left", padx=4)

        self.view_switch = ctk.CTkSegmentedButton(
            header_row,
            values=("Egyszerű nézet", "Haladó nézet"),
            command=self._on_view_mode_toggle,
        )
        self.view_switch.pack(side="left", padx=14)

        ctk.CTkButton(
            header_row,
            text="⚡ 1-Click",
            width=88,
            fg_color="#2b7a0b",
            hover_color="#1e5408",
            command=self._switch_to_quick_mode,
        ).pack(side="right", padx=(4, 0))

        ctk.CTkButton(
            header_row,
            text="📖 Útmutató",
            width=96,
            fg_color="#3b5998",
            hover_color="#2d4373",
            command=lambda: _open_tutorial_window(
                self, default_advanced=(app2_config.ui_mode == "advanced")
            ),
        ).pack(side="right", padx=4)

        ctk.CTkButton(
            header_row,
            text="📜 Előzmények",
            width=110,
            fg_color="#4b5563",
            hover_color="#374151",
            command=lambda: _open_history_window(
                self, on_resume_path=lambda p: self._start_simple_paths([p])
            ),
        ).pack(side="right", padx=4)

        ctk.CTkButton(
            header_row,
            text="💻 Hardver",
            width=94,
            fg_color="#374151",
            hover_color="#4b5563",
            command=lambda: _open_hardware_report_window(
                self, on_profile_applied=self._on_quick_fix_applied
            ),
        ).pack(side="right", padx=4)

        self.health_bar = HealthStatusBar(self.main_frame)
        self.health_bar.pack(fill="x", padx=12, pady=(0, 6))

        self.content_container = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.content_container.pack(fill="x", padx=10, pady=2)

        self.simple_frame = ctk.CTkFrame(self.content_container)
        self._build_simple_view()

        self.tabview = ctk.CTkTabview(self.content_container, height=580)
        self.tab_extractor = self.tabview.add("1. Audio Extractor")
        self.tab_translator = self.tabview.add("2. AI Translator")
        self.tab_help = self.tabview.add("3. Súgó & Tutorial")

        self._build_extractor_tab()
        self._build_translator_tab()
        self._build_help_tab()

        log_header = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        log_header.pack(fill="x", padx=15, pady=(8, 0))

        log_label = ctk.CTkLabel(
            log_header,
            text="Rendszernapló (élő szövegek, hibák és összegzés):",
            font=ctk.CTkFont(weight="bold"),
        )
        log_label.pack(side="left")

        self.btn_export_zip = ctk.CTkButton(
            log_header,
            text="📦 Hibacsomag (.zip)",
            width=145,
            height=26,
            fg_color="#4b5563",
            hover_color="#374151",
            command=self._export_diagnostic_zip,
        )
        self.btn_export_zip.pack(side="right", padx=(6, 0))

        self.btn_copy_diag = ctk.CTkButton(
            log_header,
            text="📋 Hibajelentés másolása",
            width=165,
            height=26,
            fg_color="#1d6fa5",
            hover_color="#15537c",
            command=self._copy_diagnostic_report,
        )
        self.btn_copy_diag.pack(side="right", padx=(6, 0))

        ctk.CTkButton(
            log_header,
            text="📄 Naplófájl megnyitása",
            width=165,
            height=26,
            fg_color="#3b5998",
            hover_color="#2d4373",
            command=self._open_log_file,
        ).pack(side="right")

        self.log_box = ctk.CTkTextbox(
            self.main_frame,
            height=210,
            state="disabled",
            fg_color="#1e1e1e",
        )
        self.log_box.pack(fill="both", expand=True, padx=10, pady=8)

    def _build_simple_view(self) -> None:
        ctk.CTkLabel(
            self.simple_frame,
            text="👋 Húzd ide a filmet vagy sorozat mappát — minden más automatikus!",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(pady=(14, 4))

        self.simple_engine_label = ctk.CTkLabel(
            self.simple_frame,
            text=f"AI motor: {app2_config.llm_provider} ({app2_config.llm_model}) | Whisper: {app1_config.whisper_model}",
            font=ctk.CTkFont(size=12),
            text_color="#aaaaaa",
        )
        self.simple_engine_label.pack(pady=(0, 10))

        drop_box = ctk.CTkFrame(self.simple_frame, fg_color="#1d2d44", border_width=2, border_color="#3b82f6")
        drop_box.pack(fill="x", padx=18, pady=6)
        ctk.CTkLabel(
            drop_box,
            text="🖱️ DOBD IDE A FILMET (.mkv / .mp4) VAGY A SOROZAT MAPPÁT (S01)",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#e2e8f0",
        ).pack(pady=24)

        btn_row = ctk.CTkFrame(self.simple_frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=18, pady=8)

        self.simple_btn_pick_file = ctk.CTkButton(
            btn_row,
            text="📂 Film(ek) kiválasztása",
            height=48,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color="#2b7a0b",
            hover_color="#1e5408",
            command=self._simple_pick_files,
        )
        self.simple_btn_pick_file.pack(side="left", expand=True, fill="x", padx=(0, 8))

        self.simple_btn_pick_folder = ctk.CTkButton(
            btn_row,
            text="📁 Sorozat mappa kiválasztása",
            height=48,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color="#1d6fa5",
            hover_color="#15537c",
            command=self._simple_pick_folder,
        )
        self.simple_btn_pick_folder.pack(side="left", expand=True, fill="x", padx=(0, 8))

        self.simple_btn_stop = ctk.CTkButton(
            btn_row,
            text="⏹ Megszakítás",
            height=48,
            width=140,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#8a2020",
            hover_color="#661414",
            state="disabled",
            command=self._simple_stop,
        )
        self.simple_btn_stop.pack(side="left")

        opt_row = ctk.CTkFrame(self.simple_frame, fg_color="transparent")
        opt_row.pack(fill="x", padx=18, pady=4)

        ctk.CTkLabel(opt_row, text="Célnyelv:", font=ctk.CTkFont(size=13, weight="bold")).pack(side="left", padx=(0, 6))
        self.simple_target_lang = ctk.CTkOptionMenu(
            opt_row,
            values=("Hungarian", "English", "German", "Spanish"),
            command=self._on_simple_lang_changed,
        )
        self.simple_target_lang.pack(side="left", padx=(0, 16))

        self.simple_chk_watch = ctk.CTkCheckBox(
            opt_row,
            text="👁️ Figyelje az új epizódokat a sorozatmappában (újak az első helyre!)",
        )
        self.simple_chk_watch.select()
        self.simple_chk_watch.pack(side="left")

        self.simple_status_label = ctk.CTkLabel(
            self.simple_frame,
            text="Készen áll — húzz be egy filmet vagy sorozatmappát!",
            font=ctk.CTkFont(size=14, weight="bold"),
            wraplength=780,
        )
        self.simple_status_label.pack(pady=(10, 4))

        self.simple_progress = ctk.CTkProgressBar(self.simple_frame)
        self.simple_progress.pack(fill="x", padx=18, pady=(2, 10))
        self.simple_progress.set(0)

        self.simple_error_card = ctk.CTkFrame(
            self.simple_frame, fg_color="#3b1d1d", border_width=1, border_color="#ff7043"
        )
        self.simple_error_label = ctk.CTkLabel(
            self.simple_error_card,
            text="",
            justify="left",
            wraplength=740,
            text_color="#ffccbc",
            font=ctk.CTkFont(size=12),
        )
        self.simple_error_label.pack(fill="x", padx=12, pady=8)

    def _build_extractor_tab(self) -> None:
        dir_frame = ctk.CTkFrame(self.tab_extractor, fg_color="transparent")
        dir_frame.pack(fill="x", pady=4)

        ctk.CTkLabel(dir_frame, text="Figyelt mappák (vesszővel):").pack(anchor="w")
        row_dir = ctk.CTkFrame(dir_frame, fg_color="transparent")
        row_dir.pack(fill="x", pady=2)

        self.dir_entry = ctk.CTkEntry(row_dir)
        self.dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.dir_entry.bind("<KeyRelease>", self._on_setting_changed)
        ToolTip(self.dir_entry, "Több mappa esetén vesszővel válaszd el.")

        ctk.CTkButton(row_dir, text="Tallózás", width=85, command=self._browse_directory).pack(side="left")

        single_row = ctk.CTkFrame(self.tab_extractor, fg_color="transparent")
        single_row.pack(fill="x", pady=4)

        self.btn_single_file = ctk.CTkButton(
            single_row,
            text="Egyedi videófájl feldolgozása",
            command=self._browse_single_file,
        )
        self.btn_single_file.pack(side="left", expand=True, fill="x", padx=(0, 5))

        self.btn_single_file_stop = ctk.CTkButton(
            single_row,
            text="⏹ Leállítás",
            width=110,
            fg_color="#8a2020",
            hover_color="#661414",
            state="disabled",
            command=self._stop_single_file,
        )
        self.btn_single_file_stop.pack(side="left")

        grid_frame = ctk.CTkFrame(self.tab_extractor)
        grid_frame.pack(fill="x", pady=6, ipadx=6, ipady=6)

        ctk.CTkLabel(grid_frame, text="Modell Méret:").grid(row=0, column=0, padx=8, pady=5, sticky="w")
        self.model_combo = ctk.CTkComboBox(
            grid_frame,
            values=("tiny", "base", "small", "medium", "large-v2", "large-v3"),
            command=lambda _v: self._save_settings_from_ui(),
        )
        self.model_combo.grid(row=0, column=1, padx=8, pady=5)

        ctk.CTkLabel(grid_frame, text="Eszköz:").grid(row=0, column=2, padx=8, pady=5, sticky="w")
        self.device_combo = ctk.CTkComboBox(
            grid_frame,
            values=("cpu", "cuda"),
            command=lambda _v: self._save_settings_from_ui(),
        )
        self.device_combo.grid(row=0, column=3, padx=8, pady=5)

        ctk.CTkLabel(grid_frame, text="Precizitás:").grid(row=1, column=0, padx=8, pady=5, sticky="w")
        self.compute_combo = ctk.CTkComboBox(
            grid_frame,
            values=("int8", "float16", "float32"),
            command=lambda _v: self._save_settings_from_ui(),
        )
        self.compute_combo.grid(row=1, column=1, padx=8, pady=5)

        ctk.CTkLabel(grid_frame, text="Hangsáv index:").grid(row=1, column=2, padx=8, pady=5, sticky="w")
        self.audio_stream_combo = ctk.CTkComboBox(
            grid_frame,
            values=("0", "1", "2", "3"),
            command=lambda _v: self._save_settings_from_ui(),
        )
        self.audio_stream_combo.grid(row=1, column=3, padx=8, pady=5)

        ctk.CTkLabel(grid_frame, text="Whisper nyelv:").grid(row=2, column=0, padx=8, pady=5, sticky="w")
        self.whisper_lang_combo = ctk.CTkComboBox(
            grid_frame,
            values=("auto", "en", "es", "de", "hu", "fr", "it"),
            command=lambda _v: self._save_settings_from_ui(),
        )
        self.whisper_lang_combo.grid(row=2, column=1, padx=8, pady=5)
        ToolTip(self.whisper_lang_combo, "A videó beszédnyelve (auto = automatikus felismerés, pl. es, de, en).")

        ctk.CTkLabel(grid_frame, text="Beágyazott felirat:").grid(row=2, column=2, padx=8, pady=5, sticky="w")
        self.embedded_sub_lang_combo = ctk.CTkComboBox(
            grid_frame,
            values=("auto", "eng", "spa", "deu", "hun", "fre", "ita"),
            command=lambda _v: self._save_settings_from_ui(),
        )
        self.embedded_sub_lang_combo.grid(row=2, column=3, padx=8, pady=5)
        ToolTip(self.embedded_sub_lang_combo, "Ha az MKV/MP4 több szöveges feliratot tartalmaz, melyik nyelvet válassza elsőként.")

        ctk.CTkLabel(grid_frame, text="Preferált hangsáv:").grid(row=3, column=0, padx=8, pady=5, sticky="w")
        self.audio_lang_combo = ctk.CTkComboBox(
            grid_frame,
            values=("auto", "eng", "spa", "deu", "hun", "fre", "ita"),
            command=lambda _v: self._save_settings_from_ui(),
        )
        self.audio_lang_combo.grid(row=3, column=1, padx=8, pady=5)
        ToolTip(self.audio_lang_combo, "Nyelv szerinti hangsáv-választás (elkerüli a kommentár sávokat).")

        self.auto_hw_btn = ctk.CTkButton(
            grid_frame,
            text="⚙️ Auto Hardver Optimalizálás",
            fg_color="#3b5998",
            hover_color="#2d4373",
            command=self._run_hardware_detection,
        )
        self.auto_hw_btn.grid(row=3, column=2, columnspan=2, padx=8, pady=6, sticky="ew")

        opts_frame = ctk.CTkFrame(self.tab_extractor, fg_color="transparent")
        opts_frame.pack(fill="x", pady=4)

        self.chk_scan_existing = ctk.CTkCheckBox(
            opts_frame,
            text="Meglévő videók beolvasása induláskor",
            command=self._save_settings_from_ui,
        )
        self.chk_scan_existing.pack(side="left", padx=(0, 12))

        self.chk_embedded_subs = ctk.CTkCheckBox(
            opts_frame,
            text="Beágyazott felirat kinyerése először",
            command=self._save_settings_from_ui,
        )
        self.chk_embedded_subs.pack(side="left", padx=(0, 12))

        self.chk_auto_translate = ctk.CTkCheckBox(
            opts_frame,
            text="Automatikus fordítás SRT után",
            command=self._save_settings_from_ui,
        )
        self.chk_auto_translate.pack(side="left")

        opts_frame2 = ctk.CTkFrame(self.tab_extractor, fg_color="transparent")
        opts_frame2.pack(fill="x", pady=2)

        self.chk_pgs_ocr = ctk.CTkCheckBox(
            opts_frame2,
            text="Blu-ray PGS OCR",
            command=self._save_settings_from_ui,
        )
        self.chk_pgs_ocr.pack(side="left", padx=(0, 12))
        ToolTip(self.chk_pgs_ocr, "Ha a videóban PGS képfelirat van és telepítve van a Tesseract OCR, ebből nyer SRT-t.")

        self.chk_opensubtitles = ctk.CTkCheckBox(
            opts_frame2,
            text="OpenSubtitles online",
            command=self._save_settings_from_ui,
        )
        self.chk_opensubtitles.pack(side="left", padx=(0, 8))

        self.opensub_key_entry = ctk.CTkEntry(
            opts_frame2,
            placeholder_text="OpenSubtitles API kulcs (opcionális)",
            show="*",
            width=260,
        )
        self.opensub_key_entry.pack(side="left", fill="x", expand=True)
        self.opensub_key_entry.bind("<KeyRelease>", self._on_setting_changed)

        opts_frame3 = ctk.CTkFrame(self.tab_extractor, fg_color="transparent")
        opts_frame3.pack(fill="x", pady=2)

        self.chk_confirm_streams = ctk.CTkCheckBox(
            opts_frame3,
            text="🎚️ Sávválasztó popup MKV-nál",
            command=self._save_settings_from_ui,
        )
        self.chk_confirm_streams.pack(side="left", padx=(0, 12))
        ToolTip(self.chk_confirm_streams, "Ha több audio/felirat sáv van egy MKV-ban, rákérdez indítás előtt.")

        self.chk_pause_gaming = ctk.CTkCheckBox(
            opts_frame3,
            text="🎮 Játék közben szünetel",
            command=self._save_settings_from_ui,
        )
        self.chk_pause_gaming.pack(side="left", padx=(0, 12))
        ToolTip(self.chk_pause_gaming, "Teljes képernyős 3D játék futásakor szünetelteti a háttér-AI-t.")

        self.chk_win_autostart = ctk.CTkCheckBox(
            opts_frame3,
            text="🚀 Háttér-figyelő Windows induláskor",
            command=self._on_autostart_toggled,
        )
        self.chk_win_autostart.pack(side="left")
        ToolTip(self.chk_win_autostart, "Bejelentkezéskor elindítja a háttér-figyelőt (játék közben megáll).")

        btn_row = ctk.CTkFrame(self.tab_extractor, fg_color="transparent")
        btn_row.pack(fill="x", pady=6)

        self.ext_start_btn = ctk.CTkButton(
            btn_row,
            text="▶ Mappafigyelés indítása",
            fg_color="#2b7a0b",
            hover_color="#1e5408",
            command=self._start_extractor,
        )
        self.ext_start_btn.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.orig_ext_btn_text = self.ext_start_btn.cget("text")
        self.orig_ext_btn_fg = self.ext_start_btn.cget("fg_color")
        self.orig_ext_btn_hover = self.ext_start_btn.cget("hover_color")

        self.ext_stop_btn = ctk.CTkButton(
            btn_row,
            text="⏹ Leállítás",
            fg_color="#8a2020",
            hover_color="#661414",
            state="disabled",
            command=self._stop_extractor,
        )
        self.ext_stop_btn.pack(side="left", expand=True, fill="x")

        self.ext_progress_label = ctk.CTkLabel(
            self.tab_extractor,
            text="Készen áll a kinyerésre / átírásra.",
            font=ctk.CTkFont(size=12),
        )
        self.ext_progress_label.pack(anchor="w", padx=4)

        self.ext_progress_bar = ctk.CTkProgressBar(self.tab_extractor)
        self.ext_progress_bar.pack(fill="x", pady=4)
        self.ext_progress_bar.set(0)

    def _build_translator_tab(self) -> None:
        grid = ctk.CTkFrame(self.tab_translator)
        grid.pack(fill="x", pady=4, ipadx=6, ipady=6)
        grid.columnconfigure(1, weight=1)

        ctk.CTkLabel(grid, text="API Szolgáltató:").grid(row=0, column=0, padx=8, pady=4, sticky="w")
        self.api_provider_combo = ctk.CTkComboBox(
            grid,
            values=("google", "openai", "local"),
            command=self._on_provider_changed,
        )
        self.api_provider_combo.grid(row=0, column=1, padx=8, pady=4, sticky="ew")
        ToolTip(self.api_provider_combo, "Google Gemini, OpenAI vagy lokális Ollama/LMStudio.")

        ctk.CTkLabel(grid, text="Modell Név:").grid(row=1, column=0, padx=8, pady=4, sticky="w")
        model_row = ctk.CTkFrame(grid, fg_color="transparent")
        model_row.grid(row=1, column=1, padx=8, pady=4, sticky="ew")
        model_row.columnconfigure(0, weight=1)

        self.api_model_entry = ctk.CTkComboBox(
            model_row,
            values=("gemini-2.5-flash", "gemini-2.5-pro", "gemini-flash-lite-latest"),
            command=lambda _v: self._save_settings_from_ui(),
        )
        self.api_model_entry.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.api_model_entry.bind("<KeyRelease>", self._on_setting_changed)

        self.btn_fetch_models = ctk.CTkButton(
            model_row,
            text="🔄 Modellek lekérése",
            width=145,
            command=self._fetch_models_from_api,
        )
        self.btn_fetch_models.grid(row=0, column=1)

        ctk.CTkLabel(grid, text="API Kulcs:").grid(row=2, column=0, padx=8, pady=4, sticky="w")
        self.api_key_entry = ctk.CTkEntry(grid, show="*")
        self.api_key_entry.grid(row=2, column=1, padx=8, pady=4, sticky="ew")
        self.api_key_entry.bind("<KeyRelease>", self._on_setting_changed)
        ToolTip(
            self.api_key_entry,
            "A kulcs csillagokkal van rejtve, és gépeléskor azonnal elmentődik a settings.json-be.",
        )

        ctk.CTkLabel(grid, text="Base URL (local):").grid(row=3, column=0, padx=8, pady=4, sticky="w")
        self.base_url_entry = ctk.CTkEntry(grid, placeholder_text="http://localhost:11434/v1")
        self.base_url_entry.grid(row=3, column=1, padx=8, pady=4, sticky="ew")
        self.base_url_entry.bind("<KeyRelease>", self._on_setting_changed)

        lang_row = ctk.CTkFrame(grid, fg_color="transparent")
        lang_row.grid(row=4, column=0, columnspan=2, padx=8, pady=4, sticky="ew")

        ctk.CTkLabel(lang_row, text="Forrásnyelv:").pack(side="left", padx=(0, 6))
        self.source_lang_entry = ctk.CTkOptionMenu(
            lang_row,
            values=("auto", "English", "German", "Spanish", "Hungarian"),
            command=lambda _v: self._save_settings_from_ui(),
        )
        self.source_lang_entry.pack(side="left", padx=(0, 14))

        ctk.CTkLabel(lang_row, text="Célnyelv:").pack(side="left", padx=(0, 6))
        self.target_lang_entry = ctk.CTkOptionMenu(
            lang_row,
            values=("Hungarian", "English", "German", "Spanish"),
            command=lambda _v: self._save_settings_from_ui(),
        )
        self.target_lang_entry.pack(side="left", padx=(0, 14))

        self.chk_format_tv = ctk.CTkCheckBox(
            lang_row,
            text="TV/Plex sortörés (2×42 kar.)",
            command=self._save_settings_from_ui,
        )
        self.chk_format_tv.pack(side="left", padx=(0, 12))

        self.chk_auto_context = ctk.CTkCheckBox(
            lang_row,
            text="Auto film-kontextus",
            command=self._save_settings_from_ui,
        )
        self.chk_auto_context.pack(side="left")
        ToolTip(self.chk_auto_context, "Fordítás előtt elemzi a film stílusát, szereplőit és hangnemét.")

        ctk.CTkLabel(grid, text="Blokkméret (sor):").grid(row=5, column=0, padx=8, pady=4, sticky="w")
        slider_row = ctk.CTkFrame(grid, fg_color="transparent")
        slider_row.grid(row=5, column=1, padx=8, pady=4, sticky="ew")
        slider_row.columnconfigure(0, weight=1)

        self.batch_size_slider = ctk.CTkSlider(
            slider_row,
            from_=5,
            to=80,
            number_of_steps=15,
            command=self._on_slider_changed,
        )
        self.batch_size_slider.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.batch_size_label = ctk.CTkLabel(slider_row, text="30", width=35)
        self.batch_size_label.grid(row=0, column=1)

        ctk.CTkLabel(grid, text="Fordítási stílus:").grid(row=6, column=0, padx=8, pady=4, sticky="w")
        self.style_hint_entry = ctk.CTkEntry(
            grid,
            placeholder_text="Pl.: Use natural, colloquial language as used in Hungarian movie dubbing.",
        )
        self.style_hint_entry.grid(row=6, column=1, padx=8, pady=4, sticky="ew")
        self.style_hint_entry.bind("<KeyRelease>", self._on_setting_changed)
        ToolTip(self.style_hint_entry, "Egyedi stílus-utasítás az AI fordító számára.")

        ctk.CTkLabel(grid, text="Szószedet (Eredeti = Cél):").grid(row=7, column=0, padx=8, pady=4, sticky="nw")
        self.glossary_box = ctk.CTkTextbox(grid, height=52)
        self.glossary_box.grid(row=7, column=1, padx=8, pady=4, sticky="ew")
        self.glossary_box.bind("<KeyRelease>", self._on_setting_changed)
        ToolTip(self.glossary_box, "Soronként: Karakter vagy kifejezés = Fordítás (pl. Darth Vader = Darth Vader).")

        tools_row = ctk.CTkFrame(self.tab_translator, fg_color="transparent")
        tools_row.pack(fill="x", pady=2)

        ctk.CTkLabel(tools_row, text="Időzítés (ms):").pack(side="left", padx=(0, 6))
        self.offset_ms_entry = ctk.CTkEntry(tools_row, width=80)
        self.offset_ms_entry.insert(0, "500")
        self.offset_ms_entry.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            tools_row,
            text="-500 ms",
            width=75,
            command=lambda: self._shift_srt_dialog(-abs(self._get_offset_ms())),
        ).pack(side="left", padx=(0, 4))

        ctk.CTkButton(
            tools_row,
            text="+500 ms",
            width=75,
            command=lambda: self._shift_srt_dialog(abs(self._get_offset_ms())),
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            tools_row,
            text="🔍 Szinkron ellenőrzése",
            width=160,
            fg_color="#3b5998",
            hover_color="#2d4373",
            command=self._verify_sync_dialog,
        ).pack(side="left", expand=True, fill="x", padx=(0, 4))

        ctk.CTkButton(
            tools_row,
            text="🔧 Auto-Align",
            width=110,
            fg_color="#1d6fa5",
            hover_color="#15537c",
            command=self._auto_align_dialog,
        ).pack(side="left", padx=(0, 6))

        self.chk_auto_align = ctk.CTkCheckBox(
            tools_row,
            text="Auto-szinkron javítás",
            command=self._save_settings_from_ui,
        )
        self.chk_auto_align.pack(side="left")

        plex_row = ctk.CTkFrame(self.tab_translator)
        plex_row.pack(fill="x", pady=3, ipadx=4, ipady=3)

        self.chk_plex_refresh = ctk.CTkCheckBox(
            plex_row,
            text="📺 Plex frissítés:",
            command=self._save_settings_from_ui,
        )
        self.chk_plex_refresh.pack(side="left", padx=(6, 6))

        self.plex_url_entry = ctk.CTkEntry(plex_row, width=180, placeholder_text="http://localhost:32400")
        self.plex_url_entry.pack(side="left", padx=(0, 6))
        self.plex_url_entry.bind("<KeyRelease>", self._on_setting_changed)

        self.plex_token_entry = ctk.CTkEntry(plex_row, width=170, show="*", placeholder_text="X-Plex-Token")
        self.plex_token_entry.pack(side="left", expand=True, fill="x", padx=(0, 6))
        self.plex_token_entry.bind("<KeyRelease>", self._on_setting_changed)
        ToolTip(self.plex_token_entry, "Plex Web -> ... -> Get Info -> View XML -> X-Plex-Token értéke.")

        ctk.CTkButton(
            plex_row,
            text="🔌 Teszt",
            width=75,
            fg_color="#444444",
            hover_color="#333333",
            command=self._test_plex_dialog,
        ).pack(side="left", padx=(0, 8))

        self.chk_desktop_notif = ctk.CTkCheckBox(
            plex_row,
            text="🔔 Toast + hang",
            command=self._save_settings_from_ui,
        )
        self.chk_desktop_notif.pack(side="left", padx=(0, 4))
        ToolTip(self.chk_desktop_notif, "Windows értesítés és csendes hangjelzés kész feliratnál.")

        single_srt_row = ctk.CTkFrame(self.tab_translator, fg_color="transparent")
        single_srt_row.pack(fill="x", pady=4)

        self.btn_single_srt = ctk.CTkButton(
            single_srt_row,
            text="Egyedi SRT fájl kiválasztása és fordítása",
            command=self._browse_single_srt,
        )
        self.btn_single_srt.pack(side="left", expand=True, fill="x", padx=(0, 5))

        self.btn_single_stop = ctk.CTkButton(
            single_srt_row,
            text="⏹ Leállítás",
            width=110,
            fg_color="#8a2020",
            hover_color="#661414",
            state="disabled",
            command=self._stop_single_srt,
        )
        self.btn_single_stop.pack(side="left")

        daemon_row = ctk.CTkFrame(self.tab_translator, fg_color="transparent")
        daemon_row.pack(fill="x", pady=4)

        self.tr_start_btn = ctk.CTkButton(
            daemon_row,
            text="▶ Fordító mappafigyelés indítása",
            fg_color="#2b7a0b",
            hover_color="#1e5408",
            command=self._start_translator,
        )
        self.tr_start_btn.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.orig_tr_btn_text = self.tr_start_btn.cget("text")
        self.orig_tr_btn_fg = self.tr_start_btn.cget("fg_color")
        self.orig_tr_btn_hover = self.tr_start_btn.cget("hover_color")

        self.tr_stop_btn = ctk.CTkButton(
            daemon_row,
            text="⏹ Leállítás",
            fg_color="#8a2020",
            hover_color="#661414",
            state="disabled",
            command=self._stop_translator,
        )
        self.tr_stop_btn.pack(side="left", expand=True, fill="x")

        self.tr_progress_frame = ctk.CTkFrame(self.tab_translator, fg_color="transparent")
        self.tr_progress_frame.pack(fill="x", pady=4)

        self.tr_progress_label = ctk.CTkLabel(
            self.tr_progress_frame,
            text="Készen áll a fordításra.",
            font=ctk.CTkFont(size=12),
        )
        self.tr_progress_label.pack(anchor="w")

        self.tr_progress_bar = ctk.CTkProgressBar(self.tr_progress_frame)
        self.tr_progress_bar.pack(fill="x", pady=2)
        self.tr_progress_bar.set(0)

    def _build_help_tab(self) -> None:
        top_row = ctk.CTkFrame(self.tab_help, fg_color="transparent")
        top_row.pack(fill="x", padx=8, pady=(6, 2))

        self.tutorial_selector = ctk.CTkSegmentedButton(
            top_row,
            values=("Egyszerű Útmutató", "Haladó Útmutató (GPU & Plex)"),
            command=self._on_tutorial_tab_changed,
        )
        self.tutorial_selector.pack(side="left")
        self.tutorial_selector.set("Egyszerű Útmutató")

        ctk.CTkButton(
            top_row,
            text="📋 Hibajelentés vágólapra",
            width=185,
            fg_color="#1d6fa5",
            hover_color="#15537c",
            command=self._copy_diagnostic_report,
        ).pack(side="right")

        self.help_textbox = ctk.CTkTextbox(self.tab_help, wrap="word", font=ctk.CTkFont(size=13))
        self.help_textbox.pack(fill="both", expand=True, padx=8, pady=6)
        self.help_textbox.insert("0.0", _TUTORIAL_SIMPLE_TEXT)
        self.help_textbox.configure(state="disabled")

    def _on_tutorial_tab_changed(self, choice: str) -> None:
        txt = _TUTORIAL_ADVANCED_TEXT if "Haladó" in choice else _TUTORIAL_SIMPLE_TEXT
        self.help_textbox.configure(state="normal")
        self.help_textbox.delete("0.0", "end")
        self.help_textbox.insert("0.0", txt)
        self.help_textbox.configure(state="disabled")

    def _load_settings_to_ui(self) -> None:
        self.dir_entry.delete(0, "end")
        self.dir_entry.insert(0, ", ".join(app1_config.watch_dirs))
        self.model_combo.set(app1_config.whisper_model)
        self.device_combo.set(app1_config.whisper_device)
        self.compute_combo.set(app1_config.whisper_compute_type)
        self.audio_stream_combo.set(str(app1_config.audio_stream_index))
        self.whisper_lang_combo.set(app1_config.whisper_language or "auto")
        self.embedded_sub_lang_combo.set(app1_config.preferred_subtitle_language or "auto")
        self.audio_lang_combo.set(app1_config.preferred_audio_language or "auto")

        if app1_config.scan_existing_on_start:
            self.chk_scan_existing.select()
        else:
            self.chk_scan_existing.deselect()

        if app1_config.extract_embedded_subtitles:
            self.chk_embedded_subs.select()
        else:
            self.chk_embedded_subs.deselect()

        if app1_config.auto_translate_after_extract:
            self.chk_auto_translate.select()
        else:
            self.chk_auto_translate.deselect()

        if app1_config.enable_pgs_ocr:
            self.chk_pgs_ocr.select()
        else:
            self.chk_pgs_ocr.deselect()

        if app1_config.enable_opensubtitles:
            self.chk_opensubtitles.select()
        else:
            self.chk_opensubtitles.deselect()

        if app1_config.confirm_streams_on_drop:
            self.chk_confirm_streams.select()
        else:
            self.chk_confirm_streams.deselect()

        if app1_config.pause_when_gaming:
            self.chk_pause_gaming.select()
        else:
            self.chk_pause_gaming.deselect()

        if get_windows_autostart_enabled():
            self.chk_win_autostart.select()
        else:
            self.chk_win_autostart.deselect()

        self.opensub_key_entry.delete(0, "end")
        if app1_config.opensubtitles_api_key:
            self.opensub_key_entry.insert(0, app1_config.opensubtitles_api_key)

        provider = app2_config.llm_provider.lower().strip()
        self.api_provider_combo.set(provider or "google")
        self._apply_provider_model_suggestions(provider or "google")
        self.api_model_entry.set(app2_config.llm_model)

        self.api_key_entry.delete(0, "end")
        self.api_key_entry.insert(0, app2_config.llm_api_key or "")

        self.base_url_entry.delete(0, "end")
        if app2_config.llm_base_url:
            self.base_url_entry.insert(0, app2_config.llm_base_url)

        self.source_lang_entry.set(app2_config.source_language or "auto")
        self.target_lang_entry.set(app2_config.target_language or "Hungarian")
        self.batch_size_slider.set(app2_config.translation_batch_size)
        self.batch_size_label.configure(text=str(int(app2_config.translation_batch_size)))
        self.style_hint_entry.delete(0, "end")
        self.style_hint_entry.insert(0, app2_config.translation_style_hint or "")
        if app2_config.format_tv_subtitles:
            self.chk_format_tv.select()
        else:
            self.chk_format_tv.deselect()
        if app2_config.auto_movie_context:
            self.chk_auto_context.select()
        else:
            self.chk_auto_context.deselect()

        self.glossary_box.delete("1.0", "end")
        if app2_config.glossary:
            self.glossary_box.insert("1.0", _format_glossary_text(app2_config.glossary))

        if app2_config.auto_align_subtitles:
            self.chk_auto_align.select()
        else:
            self.chk_auto_align.deselect()

        if app2_config.plex_refresh_enabled:
            self.chk_plex_refresh.select()
        else:
            self.chk_plex_refresh.deselect()

        if app2_config.desktop_notifications:
            self.chk_desktop_notif.select()
        else:
            self.chk_desktop_notif.deselect()

        self.plex_url_entry.delete(0, "end")
        self.plex_url_entry.insert(0, app2_config.plex_url or "http://localhost:32400")
        self.plex_token_entry.delete(0, "end")
        if app2_config.plex_token:
            self.plex_token_entry.insert(0, app2_config.plex_token)

        self.simple_target_lang.set(app2_config.target_language or "Hungarian")
        self._apply_ui_mode(app2_config.ui_mode or "simple")

    def _save_settings_from_ui(self) -> None:
        raw_dirs = self.dir_entry.get().strip()
        app1_config.watch_dirs = [d.strip() for d in raw_dirs.split(",") if d.strip()] if raw_dirs else []
        app1_config.whisper_model = self.model_combo.get().strip()
        app1_config.whisper_device = self.device_combo.get().strip()
        app1_config.whisper_compute_type = self.compute_combo.get().strip()
        try:
            app1_config.audio_stream_index = int(self.audio_stream_combo.get().strip())
        except ValueError:
            app1_config.audio_stream_index = 0
        raw_w_lang = self.whisper_lang_combo.get().strip()
        app1_config.whisper_language = None if not raw_w_lang or raw_w_lang.lower() == "auto" else raw_w_lang
        app1_config.preferred_subtitle_language = self.embedded_sub_lang_combo.get().strip() or "auto"
        app1_config.preferred_audio_language = self.audio_lang_combo.get().strip() or "auto"
        app1_config.scan_existing_on_start = bool(self.chk_scan_existing.get())
        app1_config.extract_embedded_subtitles = bool(self.chk_embedded_subs.get())
        app1_config.enable_pgs_ocr = bool(self.chk_pgs_ocr.get())
        app1_config.enable_opensubtitles = bool(self.chk_opensubtitles.get())
        app1_config.opensubtitles_api_key = self.opensub_key_entry.get().strip()
        app1_config.confirm_streams_on_drop = bool(self.chk_confirm_streams.get())
        app1_config.pause_when_gaming = bool(self.chk_pause_gaming.get())
        app1_config.auto_translate_after_extract = bool(self.chk_auto_translate.get())

        app2_config.llm_provider = self.api_provider_combo.get().strip()
        app2_config.llm_model = self.api_model_entry.get().strip()
        app2_config.llm_api_key = self.api_key_entry.get().strip()
        app2_config.llm_base_url = self.base_url_entry.get().strip() or None
        app2_config.source_language = self.source_lang_entry.get().strip()
        app2_config.target_language = self.target_lang_entry.get().strip()
        app2_config.translation_batch_size = int(self.batch_size_slider.get())
        app2_config.translation_style_hint = self.style_hint_entry.get().strip()
        app2_config.format_tv_subtitles = bool(self.chk_format_tv.get())
        app2_config.auto_movie_context = bool(self.chk_auto_context.get())
        app2_config.glossary = _parse_glossary_text(self.glossary_box.get("1.0", "end"))
        app2_config.auto_align_subtitles = bool(self.chk_auto_align.get())
        app2_config.desktop_notifications = bool(self.chk_desktop_notif.get())
        app2_config.notification_sound = bool(self.chk_desktop_notif.get())
        app2_config.plex_refresh_enabled = bool(self.chk_plex_refresh.get())
        app2_config.plex_url = self.plex_url_entry.get().strip() or "http://localhost:32400"
        app2_config.plex_token = self.plex_token_entry.get().strip()

        self.simple_target_lang.set(app2_config.target_language or "Hungarian")
        if hasattr(self, "simple_engine_label"):
            self.simple_engine_label.configure(
                text=f"AI motor: {app2_config.llm_provider} ({app2_config.llm_model}) | Whisper: {app1_config.whisper_model}"
            )

        if self._save_debounce_id is not None:
            try:
                self.after_cancel(self._save_debounce_id)
            except Exception:
                pass
            self._save_debounce_id = None

        settings_manager.save()

    def _apply_provider_model_suggestions(self, provider: str) -> None:
        if provider == "google":
            self.api_model_entry.configure(
                values=("gemini-2.5-flash", "gemini-2.5-pro", "gemini-flash-lite-latest")
            )
        elif provider == "openai":
            self.api_model_entry.configure(values=("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"))
        else:
            self.api_model_entry.configure(values=("gemma3:12b", "gemma2:9b", "qwen2.5:14b", "llama3"))

    def _on_provider_changed(self, choice: str) -> None:
        prov = choice.lower().strip()
        self._apply_provider_model_suggestions(prov)
        if prov == "google":
            self.api_model_entry.set("gemini-2.5-flash")
        elif prov == "openai":
            self.api_model_entry.set("gpt-4o-mini")
        else:
            self.api_model_entry.set("gemma3:12b")
            if not self.base_url_entry.get().strip():
                self.base_url_entry.insert(0, "http://localhost:11434/v1")
            self.batch_size_slider.set(12)
            self.batch_size_label.configure(text="12")
        self._save_settings_from_ui()

    def _fetch_models_from_api(self) -> None:
        provider = self.api_provider_combo.get().lower().strip()
        api_key = self.api_key_entry.get().strip()
        local_url = self.base_url_entry.get().strip() or "http://localhost:11434"
        if not api_key and provider in ("google", "openai"):
            messagebox.showerror("Hiba", f"API kulcs szükséges a(z) {provider} modellek lekéréséhez!")
            return

        self.btn_fetch_models.configure(text="Lekérdezés...", state="disabled")

        def _worker() -> None:
            try:
                models = get_available_models(provider=provider, api_key=api_key, local_url=local_url)

                def _update_ui() -> None:
                    if models:
                        current = self.api_model_entry.get().strip()
                        self.api_model_entry.configure(values=models)
                        if current in models:
                            self.api_model_entry.set(current)
                        elif "gemini-2.5-flash" in models:
                            self.api_model_entry.set("gemini-2.5-flash")
                        else:
                            self.api_model_entry.set(models[0])
                        self._save_settings_from_ui()
                    self.btn_fetch_models.configure(text="🔄 Modellek lekérése", state="normal")

                self.after(0, _update_ui)
            except Exception as exc:
                err_text = str(exc)

                def _show_error() -> None:
                    _show_friendly_error_dialog(self, err_text, on_fix_applied=self._on_quick_fix_applied)
                    self.btn_fetch_models.configure(text="🔄 Modellek lekérése", state="normal")

                self.after(0, _show_error)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_setting_changed(self, _event: tk.Event | None = None) -> None:
        if self._save_debounce_id is not None:
            try:
                self.after_cancel(self._save_debounce_id)
            except Exception:
                pass
        self._save_debounce_id = self.after(_SETTINGS_DEBOUNCE_MS, self._flush_debounced_save)

    def _flush_debounced_save(self) -> None:
        self._save_debounce_id = None
        self._save_settings_from_ui()

    def _on_slider_changed(self, value: float) -> None:
        self.batch_size_label.configure(text=str(int(value)))
        self._on_setting_changed()

    def _browse_directory(self) -> None:
        selected = filedialog.askdirectory(title="Válassz figyelt mappát")
        if selected:
            current = self.dir_entry.get().strip()
            new_val = f"{current}, {selected}" if current else selected
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, new_val)
            self._save_settings_from_ui()

    def _browse_single_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="Válassz egy videófájlt",
            filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.m4v *.ts")],
        )
        if selected:
            self._save_settings_from_ui()
            video_path = Path(selected)
            ok, sub_idx, audio_idx = _ask_stream_selection_if_needed(self, video_path)
            if not ok:
                return
            log.info("▶ Egyedi fájl feldolgozása indul: %s", video_path.name)
            threading.Thread(
                target=self._process_single_file,
                args=(video_path, sub_idx, audio_idx),
                daemon=True,
            ).start()

    def _on_autostart_toggled(self) -> None:
        desired = bool(self.chk_win_autostart.get())
        ok, msg = set_windows_autostart(desired)
        if ok:
            log.info("🚀 %s", msg)
            messagebox.showinfo("Windows indítás", msg)
        else:
            if desired:
                self.chk_win_autostart.deselect()
            else:
                self.chk_win_autostart.select()
            _show_friendly_error_dialog(self, msg, on_fix_applied=self._on_quick_fix_applied)

    def _stop_single_file(self) -> None:
        if self._single_file_stop_event:
            self._single_file_stop_event.set()
            self.btn_single_file_stop.configure(state="disabled", text="Leállítás...")

    def _process_single_file(
        self,
        file_path: Path,
        subtitle_stream_index: int | None = None,
        audio_stream_index: int | None = None,
    ) -> None:
        self._single_file_stop_event = threading.Event()
        orig_text = "Egyedi videófájl feldolgozása"
        orig_fg = self.btn_single_file.cget("fg_color")
        orig_hover = self.btn_single_file.cget("hover_color")

        def _start_ui() -> None:
            self.btn_single_file.configure(
                text="Feldolgozás...", fg_color="#cc7a00", hover_color="#a36200", state="disabled"
            )
            self.btn_single_file_stop.configure(state="normal", text="⏹ Leállítás")
            self.ext_start_btn.configure(state="disabled")
            self.ext_progress_bar.set(0)
            self.ext_progress_label.configure(text="Feldolgozás folyamatban...")

        def _progress(pct: float, elapsed: float, eta: float) -> None:
            def _ui() -> None:
                self.ext_progress_bar.set(pct)
                e_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
                eta_str = time.strftime("%H:%M:%S", time.gmtime(eta))
                self.ext_progress_label.configure(
                    text=f"Átírás: {int(pct * 100)}% | Eltelt: {e_str} | ETA: {eta_str}"
                )

            self.after(0, _ui)

        def _end_ui(success: bool) -> None:
            self.ext_progress_bar.set(1.0 if success else 0.0)
            self.btn_single_file_stop.configure(state="disabled", text="⏹ Leállítás")
            if self._single_file_stop_event and self._single_file_stop_event.is_set():
                self.ext_progress_label.configure(text="Megszakítva.")
            elif success:
                self.btn_single_file.configure(text="Kész!", fg_color="#2b7a0b", hover_color="#1e5408")
                self.ext_progress_label.configure(text="Feldolgozás kész!")
            else:
                self.ext_progress_label.configure(text="A feldolgozás sikertelen vagy kihagyva.")

            def _revert() -> None:
                self.btn_single_file.configure(
                    text=orig_text, fg_color=orig_fg, hover_color=orig_hover, state="normal"
                )
                self.ext_start_btn.configure(state="normal")

            self.after(3000, _revert)

        self.after(0, _start_ui)
        srt_path: Path | None = None
        try:
            processor = FileProcessor()
            srt_path = processor.process(
                file_path,
                progress_callback=_progress,
                stop_event=self._single_file_stop_event,
                subtitle_stream_index=subtitle_stream_index,
                audio_stream_index=audio_stream_index,
            )
            if srt_path and app1_config.auto_translate_after_extract:
                log.info("Automatikus fordítás indul: %s", srt_path.name)
                process_single_srt(srt_path, stop_event=self._single_file_stop_event)
            log.info("✅ Egyedi fájl feldolgozása befejeződött.")
        except Exception as exc:
            log.exception("Hiba történt a fájl feldolgozása során: %s", exc)
        finally:
            self.after(0, lambda: _end_ui(srt_path is not None))

    def _browse_single_srt(self) -> None:
        selected = filedialog.askopenfilename(
            title="Válassz egy feliratfájlt",
            filetypes=[("SRT Subtitles", "*.srt")],
        )
        if selected:
            self._save_settings_from_ui()
            srt_path = Path(selected)
            log.info("▶ Egyedi SRT fordítása indul: %s", srt_path.name)
            threading.Thread(target=self._process_single_srt, args=(srt_path,), daemon=True).start()

    def _stop_single_srt(self) -> None:
        if self._single_srt_stop_event:
            self._single_srt_stop_event.set()
            self.btn_single_stop.configure(state="disabled", text="Leállítás...")

    def _process_single_srt(self, file_path: Path) -> None:
        self._single_srt_stop_event = threading.Event()
        orig_text = "Egyedi SRT fájl kiválasztása és fordítása"
        orig_fg = self.btn_single_srt.cget("fg_color")
        orig_hover = self.btn_single_srt.cget("hover_color")

        def _start_ui() -> None:
            self.btn_single_srt.configure(
                text="Feldolgozás...", fg_color="#cc7a00", hover_color="#a36200", state="disabled"
            )
            self.btn_single_stop.configure(state="normal", text="⏹ Leállítás")
            self.tr_start_btn.configure(state="disabled")
            self.tr_progress_bar.set(0)

        def _update_progress(current: int, total: int, elapsed: float, eta: float) -> None:
            def _ui_update() -> None:
                pct = current / total if total > 0 else 0.0
                self.tr_progress_bar.set(pct)
                e_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
                eta_str = time.strftime("%H:%M:%S", time.gmtime(eta))
                self.tr_progress_label.configure(
                    text=f"Fordítás folyamatban: {int(pct * 100)}% | Eltelt: {e_str} | ETA: {eta_str}"
                )

            self.after(0, _ui_update)

        def _end_ui() -> None:
            if self._single_srt_stop_event and self._single_srt_stop_event.is_set():
                self.tr_progress_label.configure(text="Megszakítva, részeredmény mentve.")
            else:
                self.btn_single_srt.configure(text="Kész!", fg_color="#2b7a0b", hover_color="#1e5408")
                self.tr_progress_bar.set(1.0)
                self.tr_progress_label.configure(text="Fordítás kész!")
            self.btn_single_stop.configure(state="disabled", text="⏹ Leállítás")

            def _revert() -> None:
                self.btn_single_srt.configure(
                    text=orig_text, fg_color=orig_fg, hover_color=orig_hover, state="normal"
                )
                self.tr_start_btn.configure(state="normal")
                self.tr_progress_bar.set(0)
                self.tr_progress_label.configure(text="Készen áll a fordításra.")

            self.after(3000, _revert)

        self.after(0, _start_ui)
        try:
            process_single_srt(
                file_path,
                progress_callback=_update_progress,
                stop_event=self._single_srt_stop_event,
            )
            log.info("Egyedi SRT fájl feldolgozása befejeződött.")
        except Exception as exc:
            log.exception("Hiba történt az SRT fordítása során: %s", exc)
            self.after(0, lambda e=exc: _show_friendly_error_dialog(self, e, on_fix_applied=self._on_quick_fix_applied))
        finally:
            self.after(0, _end_ui)

    def _run_hardware_detection(self) -> None:
        profile = HardwareManager().apply_auto_fallback(force_save=False)
        self.model_combo.set(profile["whisper_model"])
        self.device_combo.set(profile["whisper_device"])
        self.compute_combo.set(profile["whisper_compute_type"])
        self._save_settings_from_ui()

    def _start_extractor(self) -> None:
        self._save_settings_from_ui()
        if not app1_config.watch_dirs:
            log.error("Nincs megadva figyelt mappa!")
            return

        self.ext_start_btn.configure(
            text="Figyelés...", fg_color="#cc7a00", hover_color="#a36200", state="disabled"
        )
        self.ext_stop_btn.configure(state="normal")
        self.ext_progress_bar.configure(mode="indeterminate")
        self.ext_progress_bar.start()

        def _on_srt_ready(srt_path: Path) -> None:
            if app1_config.auto_translate_after_extract:
                process_single_srt(srt_path)

        def _on_daemon_error(msg: str) -> None:
            def _reset_ui() -> None:
                self.ext_progress_bar.stop()
                self.ext_progress_bar.configure(mode="determinate")
                self.ext_progress_bar.set(0)
                self.ext_stop_btn.configure(state="disabled")
                self.ext_start_btn.configure(
                    text=self.orig_ext_btn_text,
                    fg_color=self.orig_ext_btn_fg,
                    hover_color=self.orig_ext_btn_hover,
                    state="normal",
                )
                _show_friendly_error_dialog(self, msg, on_fix_applied=self._on_quick_fix_applied)

            self.after(0, _reset_ui)

        self.extractor_daemon = WatcherDaemon(on_srt_ready=_on_srt_ready, on_error=_on_daemon_error)
        if app1_config.scan_existing_on_start:
            self.extractor_daemon.scan_existing()
        threading.Thread(target=self.extractor_daemon.start, daemon=True).start()

    def _stop_extractor(self) -> None:
        self.ext_stop_btn.configure(state="disabled")
        if self.extractor_daemon:
            self.extractor_daemon.stop()
        self.ext_progress_bar.stop()
        self.ext_progress_bar.configure(mode="determinate")
        self.ext_progress_bar.set(0)
        self.ext_start_btn.configure(text="Kész!", fg_color="#2b7a0b", hover_color="#1e5408")

        def _revert() -> None:
            self.ext_start_btn.configure(
                text=self.orig_ext_btn_text,
                fg_color=self.orig_ext_btn_fg,
                hover_color=self.orig_ext_btn_hover,
                state="normal",
            )

        self.after(3000, _revert)

    def _start_translator(self) -> None:
        self._save_settings_from_ui()
        if not app1_config.watch_dirs:
            log.error("Nincs megadva figyelt mappa!")
            return
        if not app2_config.llm_api_key and app2_config.llm_provider.lower() != "local":
            log.warning("Figyelem: Nincs megadva API kulcs, a hívások elbukhatnak!")

        self.tr_start_btn.configure(
            text="Fordító fut...", fg_color="#cc7a00", hover_color="#a36200", state="disabled"
        )
        self.tr_stop_btn.configure(state="normal")

        def _update_progress(current: int, total: int, elapsed: float, eta: float) -> None:
            def _ui_update() -> None:
                pct = current / total if total > 0 else 0.0
                self.tr_progress_bar.set(pct)
                e_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
                eta_str = time.strftime("%H:%M:%S", time.gmtime(eta))
                self.tr_progress_label.configure(
                    text=f"Fordítás folyamatban: {int(pct * 100)}% | Eltelt: {e_str} | ETA: {eta_str}"
                )

            self.after(0, _ui_update)

        def _finished() -> None:
            def _ui_update() -> None:
                self.tr_progress_bar.set(1.0)
                self.tr_progress_label.configure(text="Fordítás kész!")

                def _reset() -> None:
                    self.tr_progress_bar.set(0)
                    self.tr_progress_label.configure(text="Készen áll a fordításra.")

                self.after(3000, _reset)

            self.after(0, _ui_update)

        def _on_daemon_error(msg: str) -> None:
            def _reset_ui() -> None:
                self.tr_stop_btn.configure(state="disabled")
                self.tr_start_btn.configure(
                    text=self.orig_tr_btn_text,
                    fg_color=self.orig_tr_btn_fg,
                    hover_color=self.orig_tr_btn_hover,
                    state="normal",
                )
                _show_friendly_error_dialog(self, msg, on_fix_applied=self._on_quick_fix_applied)

            self.after(0, _reset_ui)

        self.translator_daemon = TranslatorDaemon(
            progress_callback=_update_progress,
            finished_callback=_finished,
            on_error=_on_daemon_error,
        )
        self.translator_daemon.scan_existing()
        threading.Thread(target=self.translator_daemon.start, daemon=True).start()

    def _stop_translator(self) -> None:
        self.tr_stop_btn.configure(state="disabled")
        if self.translator_daemon:
            self.translator_daemon.stop()
        self.tr_start_btn.configure(text="Kész!", fg_color="#2b7a0b", hover_color="#1e5408")

        def _revert() -> None:
            self.tr_start_btn.configure(
                text=self.orig_tr_btn_text,
                fg_color=self.orig_tr_btn_fg,
                hover_color=self.orig_tr_btn_hover,
                state="normal",
            )

        self.after(3000, _revert)


    def _get_offset_ms(self) -> int:
        try:
            return int(self.offset_ms_entry.get().strip())
        except ValueError:
            return 500

    def _shift_srt_dialog(self, offset_ms: int) -> None:
        selected = filedialog.askopenfilename(
            title="Válassz SRT fájlt időzítés-eltoláshoz",
            filetypes=[("SRT Subtitles", "*.srt")],
        )
        if not selected:
            return
        srt_path = Path(selected)
        shift_srt_file(srt_path, offset_ms)
        log.info("⏱️ Időzítés eltolva (%+d ms): '%s'", offset_ms, srt_path.name)
        messagebox.showinfo("Kész", f"Az SRT időzítése eltolva ({offset_ms:+d} ms):\n{srt_path.name}")

    def _verify_sync_dialog(self) -> None:
        video_sel = filedialog.askopenfilename(
            title="1/2 Válassz videófájlt (MKV/MP4)",
            filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.m4v *.ts")],
        )
        if not video_sel:
            return
        srt_sel = filedialog.askopenfilename(
            title="2/2 Válassz ellenőrzendő SRT fájlt",
            initialdir=str(Path(video_sel).parent),
            filetypes=[("SRT Subtitles", "*.srt")],
        )
        if not srt_sel:
            return

        def _worker() -> None:
            report = verify_subtitle_sync(Path(video_sel), Path(srt_sel))
            align_str = (
                f"{report.speech_alignment_ratio:.0%}"
                if report.speech_alignment_ratio is not None
                else "N/A"
            )
            summary = (
                f"Szinkron rendben: {'IGEN ✅' if report.in_sync else 'NEM ⚠️'}\n"
                f"Videó hossza: {report.video_duration_ms / 1000:.1f} mp | Blokkok: {report.subtitle_count}\n"
                f"Túllógó: {report.out_of_bounds_count} | Átfedő: {report.overlap_count} | Hang–felirat egyezés: {align_str}"
            )
            if report.issues:
                summary += "\n\nProblémák:\n- " + "\n- ".join(report.issues[:5])
            log.info("🔍 Szinkron-ellenőrzés (%s):\n%s", Path(srt_sel).name, summary)
            self.after(0, lambda: messagebox.showinfo("Szinkron-ellenőrzés eredménye", summary))

        threading.Thread(target=_worker, daemon=True).start()

    def _auto_align_dialog(self) -> None:
        video_sel = filedialog.askopenfilename(
            title="1/2 Válassz videófájlt (MKV/MP4)",
            filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.m4v *.ts")],
        )
        if not video_sel:
            return
        srt_sel = filedialog.askopenfilename(
            title="2/2 Válassz igazítandó SRT fájlt",
            initialdir=str(Path(video_sel).parent),
            filetypes=[("SRT Subtitles", "*.srt")],
        )
        if not srt_sel:
            return

        def _worker() -> None:
            try:
                applied_ms, out_path, report = auto_align_subtitle(Path(video_sel), Path(srt_sel))
                if applied_ms != 0:
                    msg = (
                        f"🔧 Auto-szinkron sikeres!\n\n"
                        f"Alkalmazott eltolás: {applied_ms:+d} ms\n"
                        f"Fájl: {out_path.name}\n"
                        f"Új hang–felirat egyezés: {report.speech_alignment_ratio:.0%}"
                        if report.speech_alignment_ratio is not None
                        else f"🔧 Elmentve ({applied_ms:+d} ms): {out_path.name}"
                    )
                else:
                    msg = (
                        f"✅ A felirat már eleve szinkronban volt ({out_path.name}).\n"
                        "Nem volt szükség eltolásra."
                    )
                self.after(0, lambda: messagebox.showinfo("Auto-Align eredmény", msg))
            except Exception as exc:
                self.after(0, lambda e=exc: _show_friendly_error_dialog(self, e, on_fix_applied=self._on_quick_fix_applied))

        threading.Thread(target=_worker, daemon=True).start()

    def _test_plex_dialog(self) -> None:
        self._save_settings_from_ui()
        url = self.plex_url_entry.get().strip() or "http://localhost:32400"
        token = self.plex_token_entry.get().strip()

        def _worker() -> None:
            ok, msg = test_plex_connection(url, token)
            if ok:
                self.after(0, lambda: messagebox.showinfo("Plex Kapcsolat", msg))
            else:
                self.after(0, lambda: _show_friendly_error_dialog(self, msg, on_fix_applied=self._on_quick_fix_applied))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_view_mode_toggle(self, choice: str) -> None:
        mode = "simple" if "Egyszerű" in choice else "advanced"
        app2_config.ui_mode = mode
        if self._save_debounce_id is not None:
            try:
                self.after_cancel(self._save_debounce_id)
            except Exception:
                pass
            self._save_debounce_id = None

        settings_manager.save()
        self._apply_ui_mode(mode)

    def _apply_ui_mode(self, mode: str) -> None:
        norm = "simple" if str(mode).strip().lower() == "simple" else "advanced"
        if norm == "simple":
            self.tabview.pack_forget()
            self.simple_frame.pack(fill="x", padx=4, pady=4)
            self.view_switch.set("Egyszerű nézet")
        else:
            self.simple_frame.pack_forget()
            self.tabview.pack(fill="x", padx=2, pady=4)
            self.view_switch.set("Haladó nézet")

    def _on_simple_lang_changed(self, lang: str) -> None:
        self.target_lang_entry.set(lang)
        self._save_settings_from_ui()

    def _simple_pick_files(self) -> None:
        selected = filedialog.askopenfilenames(
            title="Válassz egy vagy több filmet / SRT feliratot",
            filetypes=[("Videó és felirat", "*.mkv *.mp4 *.avi *.mov *.m4v *.ts *.srt")],
        )
        if selected:
            self._start_simple_paths([Path(s) for s in selected])

    def _simple_pick_folder(self) -> None:
        selected = filedialog.askdirectory(title="Válassz sorozat vagy film mappát")
        if selected:
            self._start_simple_paths([Path(selected)])

    def _start_simple_paths(self, paths: list[Path]) -> None:
        self._save_settings_from_ui()
        if self._simple_series_watcher:
            self._simple_series_watcher.stop()
            self._simple_series_watcher = None

        stream_overrides: dict[Path, tuple[int | None, int | None]] = {}
        if app1_config.confirm_streams_on_drop:
            for p in paths:
                resolved = Path(p).resolve()
                if resolved.is_file() and resolved.suffix.lower() in SUPPORTED_EXTENSIONS:
                    ok, sub_idx, audio_idx = _ask_stream_selection_if_needed(self, resolved)
                    if not ok:
                        return
                    stream_overrides[resolved] = (sub_idx, audio_idx)

        self.simple_error_card.pack_forget()
        self._last_error_text = ""
        self._simple_stop_event = threading.Event()
        self.simple_btn_pick_file.configure(state="disabled")
        self.simple_btn_pick_folder.configure(state="disabled")
        self.simple_btn_stop.configure(state="normal")
        self.simple_progress.set(0)
        self.simple_status_label.configure(text=f"Előkészítés ({len(paths)} elem)...")

        folders = [str(p.resolve()) for p in paths if p.is_dir()]
        watch_after = bool(self.simple_chk_watch.get()) and bool(folders)

        def _worker() -> None:
            results: list[Path] = []
            caught_exc: Exception | None = None
            try:
                results = run_batch_one_click(
                    paths,
                    cache_manager=self._simple_cache,
                    status_callback=lambda msg: self.after(0, lambda m=msg: self.simple_status_label.configure(text=m)),
                    progress_callback=lambda val: self.after(0, lambda v=val: self.simple_progress.set(v)),
                    stop_event=self._simple_stop_event,
                    stream_overrides=stream_overrides,
                )
            except Exception as exc:
                caught_exc = exc
                log.exception("Hiba az Egyszerű nézetben: %s", exc)

            def _done() -> None:
                if caught_exc is not None:
                    self.simple_btn_pick_file.configure(state="normal")
                    self.simple_btn_pick_folder.configure(state="normal")
                    self.simple_btn_stop.configure(state="disabled")
                    friendly = explain_error(caught_exc)
                    self._last_error_text = friendly.technical
                    self.simple_status_label.configure(text=f"❌ [{friendly.code}] {friendly.title}")
                    self.simple_error_label.configure(text=friendly.format_dialog_text())
                    self.simple_error_card.pack(fill="x", padx=18, pady=(0, 10))
                    _show_friendly_error_dialog(self, caught_exc, on_fix_applied=self._on_quick_fix_applied)
                    return

                if self._simple_stop_event and self._simple_stop_event.is_set():
                    self.simple_btn_pick_file.configure(state="normal")
                    self.simple_btn_pick_folder.configure(state="normal")
                    self.simple_btn_stop.configure(state="disabled")
                    self.simple_status_label.configure(text="⏹ Megszakítva (részeredmény elmentve).")
                    return

                self.simple_progress.set(1.0)
                if watch_after:
                    self.simple_status_label.configure(
                        text=f"✅ Kész ({len(results)} új felirat). Figyelés aktív az új epizódokra!"
                    )
                    self._start_simple_series_watcher(folders)
                else:
                    self.simple_btn_pick_file.configure(state="normal")
                    self.simple_btn_pick_folder.configure(state="normal")
                    self.simple_btn_stop.configure(state="disabled")
                    if results:
                        self.simple_status_label.configure(
                            text=f"✅ Kész! {len(results)} felirat elkészült (a Plexen hamarosan megjelenik)."
                        )
                    else:
                        self.simple_status_label.configure(text="✅ Minden epizód már le volt fordítva (cache).")

            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _start_simple_series_watcher(self, folders: list[str]) -> None:
        def _on_new_srt(srt_path: Path) -> None:
            self.after(0, lambda: self.simple_status_label.configure(text=f"⚡ Új epizód fordítása: {srt_path.name}"))
            out = process_single_srt(srt_path, stop_event=self._simple_stop_event, resume=True)
            if out:
                for ext in SUPPORTED_EXTENSIONS:
                    vid = srt_path.with_suffix(ext)
                    if vid.exists():
                        self._simple_cache.mark_episode(vid, "done", src_srt=srt_path, translated_srt=out)
                        break
                self.after(0, lambda: self.simple_status_label.configure(text=f"✅ Új epizód kész: {out.name}"))

        self._simple_series_watcher = WatcherDaemon(on_srt_ready=_on_new_srt, extra_dirs=folders)
        threading.Thread(target=self._simple_series_watcher.start, daemon=True).start()

    def _simple_stop(self) -> None:
        if self._simple_stop_event:
            self._simple_stop_event.set()
        if self._simple_series_watcher:
            self._simple_series_watcher.stop()
            self._simple_series_watcher = None
        self.simple_btn_stop.configure(state="disabled")
        self.simple_btn_pick_file.configure(state="normal")
        self.simple_btn_pick_folder.configure(state="normal")

    def _copy_diagnostic_report(self) -> None:
        report = build_diagnostic_clipboard_report(self._last_error_text)
        self.clipboard_clear()
        self.clipboard_append(report)
        orig = self.btn_copy_diag.cget("text")
        self.btn_copy_diag.configure(text="✅ Vágólapra másolva!")
        self.after(2500, lambda: self.btn_copy_diag.configure(text=orig))

    def _export_diagnostic_zip(self) -> None:
        try:
            zp = export_diagnostic_zip(error_text=self._last_error_text)
            log.info("📦 Hibacsomag elmentve: '%s'", zp)
            messagebox.showinfo(
                "Hibacsomag elkészült",
                f"A kulcsmentesített hibacsomag elmentve:\n{zp}",
            )
        except Exception as exc:
            _show_friendly_error_dialog(self, exc, on_fix_applied=self._on_quick_fix_applied)

    def _on_quick_fix_applied(self) -> None:
        self._load_settings_to_ui()
        self.health_bar.refresh_async()

    def _open_log_file(self) -> None:
        log_path = get_log_file_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not log_path.exists():
            log_path.write_text("", encoding="utf-8")
        if hasattr(os, "startfile"):
            os.startfile(str(log_path))

    def _switch_to_quick_mode(self) -> None:
        if self._simple_series_watcher:
            self._simple_series_watcher.stop()
        self.destroy()
        run_quick_mode()


def run_gui() -> None:
    """Elindítja a teljes grafikus alkalmazást."""
    app = PlexSubtitlerApp()
    app.mainloop()
