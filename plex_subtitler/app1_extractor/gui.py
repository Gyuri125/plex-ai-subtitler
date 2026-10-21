"""
gui.py - CustomTkinter alapú grafikus felhasználói felület.

Tartalmazza mindkét alkalmazás (Extractor és Translator) beállításait
és vezérlőit fülekre bontva. A logolás egy közös LogBoxba történik.
"""

import logging
import threading
import tkinter as tk
from tkinter import filedialog
import customtkinter as ctk

from plex_subtitler.logger import get_logger
from plex_subtitler.config import app1_config, app2_config, settings_manager
from plex_subtitler.app1_extractor.watcher import WatcherDaemon
from plex_subtitler.app1_extractor.hardware_manager import HardwareManager
from plex_subtitler.app1_extractor.file_processor import FileProcessor
from plex_subtitler.app2_translator.translator_daemon import TranslatorDaemon
from plex_subtitler.notifier import refresh_plex_library, send_discord_notification, send_telegram_notification

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

log = get_logger("GUI", level="INFO")

# -----------------------------------------------------------------------------
# Tooltip Osztály
# -----------------------------------------------------------------------------
class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tooltip_window = None
        self.widget.bind("<Enter>", self.enter)
        self.widget.bind("<Leave>", self.leave)

    def enter(self, event=None):
        self.schedule_id = self.widget.after(500, self.show_tooltip)

    def leave(self, event=None):
        if hasattr(self, 'schedule_id'):
            self.widget.after_cancel(self.schedule_id)
        self.hide_tooltip()

    def show_tooltip(self):
        if self.tooltip_window or not self.text:
            return
        x, y, cx, cy = self.widget.bbox("insert") if hasattr(self.widget, "bbox") else (0, 0, 0, 0)
        x = x + self.widget.winfo_rootx() + 25
        y = y + cy + self.widget.winfo_rooty() + 25
        
        self.tooltip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        
        label = tk.Label(tw, text=self.text, justify='left',
                         background="#2b2b2b", foreground="#ffffff", 
                         relief='solid', borderwidth=1,
                         font=("Arial", 10, "normal"), padx=5, pady=3)
        label.pack(ipadx=1)

    def hide_tooltip(self):
        tw = self.tooltip_window
        self.tooltip_window = None
        if tw:
            tw.destroy()


# -----------------------------------------------------------------------------
# GUI Logger Handler
# -----------------------------------------------------------------------------
class GuiLogHandler(logging.Handler):
    """Egyedi handler, ami a GUI LogBox-ba írja a logokat thread-safe módon."""
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget
        self.setFormatter(logging.Formatter("%(asctime)s | %(name)s | %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        msg = self.format(record)
        def append_log():
            self.text_widget.configure(state="normal")
            self.text_widget.insert("end", msg + "\n")
            self.text_widget.see("end")
            self.text_widget.configure(state="disabled")
        # Biztosítjuk, hogy a GUI szálon fusson le
        self.text_widget.after(0, append_log)


# -----------------------------------------------------------------------------
# Fő GUI Osztály
# -----------------------------------------------------------------------------
class PlexSubtitlerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Plex Subtitler - Extractor & Translator")
        self.geometry("740x840")
        self.resizable(False, False)
        
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        self.extractor_daemon = None
        self.translator_daemon = None
        self.tray_icon = None
        
        self._build_ui()
        self._load_settings_to_ui()
        self._setup_gui_logger()
        self._init_tray()
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

    def _setup_gui_logger(self):
        gui_handler = GuiLogHandler(self.log_box)
        # Root loggerhez adjuk, hogy mindent lássunk (Info szinten)
        logging.getLogger().addHandler(gui_handler)

    def _build_ui(self):
        self.main_frame = ctk.CTkFrame(self)
        self.main_frame.pack(fill="both", expand=True, padx=15, pady=15)
        
        # Cím
        self.title_label = ctk.CTkLabel(
            self.main_frame, 
            text="Plex Subtitler", 
            font=ctk.CTkFont(size=22, weight="bold")
        )
        self.title_label.pack(pady=(5, 10))
        
        # Fülek (Tabs)
        self.tabview = ctk.CTkTabview(self.main_frame, height=390)
        self.tabview.pack(fill="x", padx=10, pady=5)
        
        self.tab_extractor = self.tabview.add("1. Audio Extractor")
        self.tab_translator = self.tabview.add("2. AI Translator")
        self.tab_integrations = self.tabview.add("3. Integrációk")
        self.tab_help = self.tabview.add("4. Súgó")
        
        self._build_extractor_tab()
        self._build_translator_tab()
        self._build_integrations_tab()
        self._build_help_tab()
        
        # Közös LogBox Szekció
        log_label = ctk.CTkLabel(self.main_frame, text="Rendszernapló:", font=ctk.CTkFont(weight="bold"))
        log_label.pack(anchor="w", padx=10, pady=(15, 0))
        
        self.log_box = ctk.CTkTextbox(self.main_frame, height=200, state="disabled", fg_color="#1e1e1e")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(5, 10))

    def _build_extractor_tab(self):
        # Mappa beállítások
        dir_frame = ctk.CTkFrame(self.tab_extractor, fg_color="transparent")
        dir_frame.pack(fill="x", pady=5)
        
        ctk.CTkLabel(dir_frame, text="Figyelt mappák (vesszővel):").pack(anchor="w")
        
        row_frame = ctk.CTkFrame(dir_frame, fg_color="transparent")
        row_frame.pack(fill="x", pady=(0, 5))
        
        self.dir_entry = ctk.CTkEntry(row_frame)
        self.dir_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ToolTip(self.dir_entry, "Több mappa esetén vesszővel válaszd el.")
        
        ctk.CTkButton(row_frame, text="Tallózás", width=80, command=self._browse_directory).pack(side="left")
        self.btn_single_file = ctk.CTkButton(row_frame, text="Egyedi fájl kiválasztása", width=140, command=self._browse_single_file)
        self.btn_single_file.pack(side="left", padx=5)
        
        # Whisper beállítások
        wh_frame = ctk.CTkFrame(self.tab_extractor)
        wh_frame.pack(fill="x", pady=15, ipadx=5, ipady=5)
        
        ctk.CTkLabel(wh_frame, text="Modell Méret:").grid(row=0, column=0, padx=10, pady=5, sticky="w")
        self.model_combo = ctk.CTkComboBox(wh_frame, values=["tiny", "base", "small", "medium", "large-v2", "large-v3"])
        self.model_combo.grid(row=0, column=1, padx=10, pady=5)
        
        ctk.CTkLabel(wh_frame, text="Eszköz:").grid(row=0, column=2, padx=10, pady=5, sticky="w")
        self.device_combo = ctk.CTkComboBox(wh_frame, values=["cpu", "cuda"])
        self.device_combo.grid(row=0, column=3, padx=10, pady=5)
        
        ctk.CTkLabel(wh_frame, text="Precizitás:").grid(row=1, column=0, padx=10, pady=5, sticky="w")
        self.compute_combo = ctk.CTkComboBox(wh_frame, values=["int8", "float16", "float32"])
        self.compute_combo.grid(row=1, column=1, padx=10, pady=5)
        
        self.auto_hw_btn = ctk.CTkButton(wh_frame, text="Hardver Auto-Detektálás", 
                                         fg_color="#2b7a0b", hover_color="#1e5408", command=self._run_hardware_detection)
        self.auto_hw_btn.grid(row=1, column=2, columnspan=2, padx=10, pady=5)
        
        ctk.CTkLabel(wh_frame, text="Nagyobb modell = pontosabb felirat, de több memóriát (VRAM) igényel.", font=ctk.CTkFont(size=11), text_color="gray").grid(row=2, column=0, columnspan=2, padx=10, pady=(0, 5), sticky="w")
        ctk.CTkLabel(wh_frame, text="int8: Gyengébb gépekre / fp16: Dedikált GPU-ra.", font=ctk.CTkFont(size=11), text_color="gray").grid(row=2, column=2, columnspan=2, padx=10, pady=(0, 5), sticky="w")
        
        # Extra opciók (Beágyazott felirat, Online felirat, Auto-sync, Quality Gate)
        extra_opt_frame = ctk.CTkFrame(self.tab_extractor, fg_color="transparent")
        extra_opt_frame.pack(fill="x", padx=5, pady=(0, 2))
        
        self.chk_embedded_subs = ctk.CTkCheckBox(
            extra_opt_frame, 
            text="Beágyazott feliratok kinyerése (100x gyorsulás)", 
            command=self._on_setting_changed
        )
        self.chk_embedded_subs.pack(side="left", padx=5)
        ToolTip(self.chk_embedded_subs, "Ha a letöltött videóban már van angol felirat, 0.5 mp alatt kinyeri és kihagyja a Whispert.")

        self.chk_online_subs = ctk.CTkCheckBox(
            extra_opt_frame,
            text="Online feliratkeresés (OpenSubtitles)",
            command=self._on_setting_changed
        )
        self.chk_online_subs.pack(side="left", padx=10)
        ToolTip(self.chk_online_subs, "Hash és cím alapján kész feliratot keres a netről a Whisper előtt.")
        
        self.chk_minimize_tray = ctk.CTkCheckBox(
            extra_opt_frame, 
            text="Tálcára rejtés bezáráskor", 
            command=self._on_setting_changed
        )
        self.chk_minimize_tray.pack(side="right", padx=5)
        ToolTip(self.chk_minimize_tray, "Az ablak bezárásakor a program a háttérben fut tovább a Windows tálcaikonján.")

        extra_opt_frame2 = ctk.CTkFrame(self.tab_extractor, fg_color="transparent")
        extra_opt_frame2.pack(fill="x", padx=5, pady=(2, 5))

        self.chk_auto_sync = ctk.CTkCheckBox(
            extra_opt_frame2,
            text="Auto-Sync időzítés-igazítás (ffsubsync)",
            command=self._on_setting_changed
        )
        self.chk_auto_sync.pack(side="left", padx=5)
        ToolTip(self.chk_auto_sync, "A felirat időbélyegeit automatikusan a videó beszédhangjához igazítja.")

        self.chk_quality_gate = ctk.CTkCheckBox(
            extra_opt_frame2,
            text="Quality Gate (Hallucináció-szűrés)",
            command=self._on_setting_changed
        )
        self.chk_quality_gate.pack(side="left", padx=10)
        ToolTip(self.chk_quality_gate, "Kiszűri a Whisper fantomszövegeit és ellenőrzi a felirat integritását.")

        # Vezérlő gombok
        ctrl_frame = ctk.CTkFrame(self.tab_extractor, fg_color="transparent")
        ctrl_frame.pack(fill="x", pady=10)
        
        self.ext_start_btn = ctk.CTkButton(ctrl_frame, text="▶ Extractor Indítása", command=self._start_extractor)
        self.ext_start_btn.pack(side="left", expand=True, padx=5)
        
        self.ext_stop_btn = ctk.CTkButton(ctrl_frame, text="⏹ Extractor Leállítása", fg_color="#a83232", hover_color="#7a2424", state="disabled", command=self._stop_extractor)
        self.ext_stop_btn.pack(side="right", expand=True, padx=5)
        
        self.ext_progress_bar = ctk.CTkProgressBar(self.tab_extractor, mode="indeterminate")
        self.ext_progress_bar.pack(fill="x", padx=10, pady=(0, 10))
        self.ext_progress_bar.set(0)
        
    def _build_translator_tab(self):
        # API Provider
        api_frame = ctk.CTkFrame(self.tab_translator)
        api_frame.pack(fill="x", pady=10, ipadx=5, ipady=5)
        
        ctk.CTkLabel(api_frame, text="API Szolgáltató:").grid(row=0, column=0, padx=10, pady=5, sticky="w")
        self.api_provider_combo = ctk.CTkComboBox(api_frame, values=["google", "openai", "local"], command=self._on_provider_changed)
        self.api_provider_combo.grid(row=0, column=1, padx=10, pady=5, sticky="we")
        ToolTip(self.api_provider_combo, "Google Gemini vagy OpenAI (kompatibilis Ollama-val is).")
        
        ctk.CTkLabel(api_frame, text="Modell Név:").grid(row=1, column=0, padx=10, pady=5, sticky="w")
        
        # Készítünk egy belső frame-et a comboboxnak és a frissítés gombnak
        model_frame = ctk.CTkFrame(api_frame, fg_color="transparent")
        model_frame.grid(row=1, column=1, padx=10, pady=5, sticky="we")
        model_frame.columnconfigure(0, weight=1)
        
        self.api_model_entry = ctk.CTkComboBox(model_frame, command=self._on_setting_changed)
        self.api_model_entry.grid(row=0, column=0, sticky="we")
        self.api_model_entry.bind("<KeyRelease>", self._on_setting_changed)
        ToolTip(self.api_model_entry, "Pl: gemini-1.5-pro, gpt-4o-mini, llama3")
        
        self.btn_fetch_models = ctk.CTkButton(model_frame, text="Modellek lekérése", width=140, command=self._fetch_models_from_api)
        self.btn_fetch_models.grid(row=0, column=1, padx=(10, 0))

        ctk.CTkLabel(api_frame, text="API Kulcs:").grid(row=2, column=0, padx=10, pady=5, sticky="w")
        self.api_key_entry = ctk.CTkEntry(api_frame, show="*")
        self.api_key_entry.grid(row=2, column=1, padx=10, pady=5, sticky="we")
        self.api_key_entry.bind("<KeyRelease>", self._on_setting_changed)
        ToolTip(self.api_key_entry, "A kulcs csillagokkal van rejtve, és gépeléskor azonnal elmentődik a settings.json-be.")
        
        api_frame.columnconfigure(1, weight=1)

        # Fordítás beállítások
        trans_frame = ctk.CTkFrame(self.tab_translator)
        trans_frame.pack(fill="x", pady=10, ipadx=5, ipady=5)
        
        ctk.CTkLabel(trans_frame, text="Célnyelv:").grid(row=0, column=0, padx=10, pady=5, sticky="w")
        self.target_lang_entry = ctk.CTkOptionMenu(trans_frame, values=["Hungarian", "English", "German", "Spanish"], command=self._on_setting_changed)
        self.target_lang_entry.grid(row=0, column=1, padx=10, pady=5, sticky="we")
        
        ctk.CTkLabel(trans_frame, text="Blokkméret (sor):").grid(row=1, column=0, padx=10, pady=5, sticky="w")
        
        slider_frame = ctk.CTkFrame(trans_frame, fg_color="transparent")
        slider_frame.grid(row=1, column=1, padx=10, pady=5, sticky="we")
        slider_frame.columnconfigure(0, weight=1)
        
        self.batch_size_slider = ctk.CTkSlider(slider_frame, from_=10, to=100, number_of_steps=9, command=self._on_slider_changed)
        self.batch_size_slider.grid(row=0, column=0, sticky="we", pady=5)
        
        self.batch_size_label = ctk.CTkLabel(slider_frame, text="30", width=30)
        self.batch_size_label.grid(row=0, column=1, padx=(5, 0), sticky="e")
        ToolTip(self.batch_size_slider, "Hány sor menjen egyszerre az API-nak. Túl nagy = elcsúszhat, Túl kicsi = nincs kontextus. (Ajánlott: 30)")
        
        self.chk_sliding_context = ctk.CTkCheckBox(
            trans_frame,
            text="Gördülő kontextus-ablak (Sliding Context)",
            command=self._on_setting_changed
        )
        self.chk_sliding_context.grid(row=2, column=0, padx=10, pady=(5, 5), sticky="w")
        ToolTip(self.chk_sliding_context, "Átadja az előző blokkok lefordított sorait az LLM-nek a konzisztens szinkronhangnem és tegezés/önözés érdekében.")

        self.chk_series_memory = ctk.CTkCheckBox(
            trans_frame,
            text="Sorozat-memória és karakter szótár (Series Glossary)",
            command=self._on_setting_changed
        )
        self.chk_series_memory.grid(row=2, column=1, padx=10, pady=(5, 5), sticky="w")
        ToolTip(self.chk_series_memory, "Sorozatok esetén megőrzi a tegezés/magázás szabályait és a szakkifejezéseket az epizódok között.")

        self.chk_parallel = ctk.CTkCheckBox(
            trans_frame,
            text="Párhuzamosított fordítás (Gyorsítás 2 szálon)",
            command=self._on_setting_changed
        )
        self.chk_parallel.grid(row=3, column=0, columnspan=2, padx=10, pady=(5, 5), sticky="w")
        ToolTip(self.chk_parallel, "Többszálú konkurens LLM kérések a fordítási idő csökkentéséhez.")
        
        trans_frame.columnconfigure(1, weight=1)

        # Vezérlő gombok
        file_frame = ctk.CTkFrame(self.tab_translator, fg_color="transparent")
        file_frame.pack(fill="x", pady=(10, 0))
        self.btn_single_srt = ctk.CTkButton(file_frame, text="Egyedi SRT fájl kiválasztása", command=self._browse_single_srt)
        self.btn_single_srt.pack(side="left", padx=5)
        
        self.btn_single_stop = ctk.CTkButton(file_frame, text="⏹ Leállítás", fg_color="#a83232", hover_color="#7a2424", state="disabled", command=self._stop_single_srt)
        self.btn_single_stop.pack(side="left", padx=5)

        ctrl_frame = ctk.CTkFrame(self.tab_translator, fg_color="transparent")
        ctrl_frame.pack(fill="x", pady=10)
        
        self.tr_start_btn = ctk.CTkButton(ctrl_frame, text="▶ Translator Indítása", command=self._start_translator)
        self.tr_start_btn.pack(side="left", expand=True, padx=5)
        
        self.tr_stop_btn = ctk.CTkButton(ctrl_frame, text="⏹ Translator Leállítása", fg_color="#a83232", hover_color="#7a2424", state="disabled", command=self._stop_translator)
        self.tr_stop_btn.pack(side="right", expand=True, padx=5)
        
        self.tr_progress_frame = ctk.CTkFrame(self.tab_translator, fg_color="transparent")
        self.tr_progress_frame.pack(fill="x", padx=10, pady=(0, 10))
        
        self.tr_progress_label = ctk.CTkLabel(self.tr_progress_frame, text="Készen áll a fordításra.")
        self.tr_progress_label.pack(anchor="w")
        
        self.tr_progress_bar = ctk.CTkProgressBar(self.tr_progress_frame, mode="determinate")
        self.tr_progress_bar.pack(fill="x", pady=(5, 0))
        self.tr_progress_bar.set(0)

    def _build_integrations_tab(self):
        # Plex Szerver integráció
        plex_frame = ctk.CTkFrame(self.tab_integrations)
        plex_frame.pack(fill="x", padx=10, pady=10, ipadx=5, ipady=5)

        self.chk_plex_refresh = ctk.CTkCheckBox(
            plex_frame, 
            text="Automatikus Plex könyvtárfrissítés (Auto-Refresh)", 
            command=self._on_setting_changed
        )
        self.chk_plex_refresh.pack(anchor="w", padx=10, pady=(5, 5))
        ToolTip(self.chk_plex_refresh, "Ha be van kapcsolva, a .hu.srt elkészültekor a Plex azonnal észleli a feliratot.")

        p_row1 = ctk.CTkFrame(plex_frame, fg_color="transparent")
        p_row1.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(p_row1, text="Plex URL:", width=110, anchor="w").pack(side="left")
        self.plex_url_entry = ctk.CTkEntry(p_row1, placeholder_text="http://localhost:32400")
        self.plex_url_entry.pack(side="left", fill="x", expand=True)
        self.plex_url_entry.bind("<KeyRelease>", self._on_setting_changed)

        p_row2 = ctk.CTkFrame(plex_frame, fg_color="transparent")
        p_row2.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(p_row2, text="Plex Token:", width=110, anchor="w").pack(side="left")
        self.plex_token_entry = ctk.CTkEntry(p_row2, show="*", placeholder_text="X-Plex-Token (opcionális)")
        self.plex_token_entry.pack(side="left", fill="x", expand=True)
        self.plex_token_entry.bind("<KeyRelease>", self._on_setting_changed)

        self.btn_test_plex = ctk.CTkButton(plex_frame, text="Plex kapcsolat tesztelése", command=self._test_plex)
        self.btn_test_plex.pack(padx=10, pady=(5, 5))

        # Értesítések szekció
        notif_frame = ctk.CTkFrame(self.tab_integrations)
        notif_frame.pack(fill="x", padx=10, pady=10, ipadx=5, ipady=5)

        self.chk_notifications = ctk.CTkCheckBox(
            notif_frame,
            text="Push értesítések küldése (Discord / Telegram)",
            command=self._on_setting_changed
        )
        self.chk_notifications.pack(anchor="w", padx=10, pady=(5, 5))

        d_row = ctk.CTkFrame(notif_frame, fg_color="transparent")
        d_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(d_row, text="Discord Webhook:", width=130, anchor="w").pack(side="left")
        self.discord_url_entry = ctk.CTkEntry(d_row, placeholder_text="https://discord.com/api/webhooks/...")
        self.discord_url_entry.pack(side="left", fill="x", expand=True)
        self.discord_url_entry.bind("<KeyRelease>", self._on_setting_changed)

        tg_row = ctk.CTkFrame(notif_frame, fg_color="transparent")
        tg_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(tg_row, text="Telegram Token:", width=130, anchor="w").pack(side="left")
        self.tg_token_entry = ctk.CTkEntry(tg_row, show="*", placeholder_text="Bot Token")
        self.tg_token_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.tg_token_entry.bind("<KeyRelease>", self._on_setting_changed)

        ctk.CTkLabel(tg_row, text="Chat ID:", width=60, anchor="w").pack(side="left")
        self.tg_chat_entry = ctk.CTkEntry(tg_row, placeholder_text="Chat ID", width=110)
        self.tg_chat_entry.pack(side="left")
        self.tg_chat_entry.bind("<KeyRelease>", self._on_setting_changed)

        self.btn_test_notif = ctk.CTkButton(notif_frame, text="Teszt értesítés küldése", command=self._test_notifications)
        self.btn_test_notif.pack(padx=10, pady=(5, 5))

        # Web Dashboard szekció
        web_frame = ctk.CTkFrame(self.tab_integrations)
        web_frame.pack(fill="x", padx=10, pady=10, ipadx=5, ipady=5)

        self.chk_web_dashboard = ctk.CTkCheckBox(
            web_frame,
            text="Beépített Web Dashboard engedélyezése (Böngészős / NAS hozzáférés)",
            command=self._on_setting_changed
        )
        self.chk_web_dashboard.pack(anchor="w", padx=10, pady=(5, 5))

        w_row = ctk.CTkFrame(web_frame, fg_color="transparent")
        w_row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(w_row, text="Web Port:", width=130, anchor="w").pack(side="left")
        self.web_port_entry = ctk.CTkEntry(w_row, placeholder_text="8080", width=80)
        self.web_port_entry.pack(side="left")
        self.web_port_entry.bind("<KeyRelease>", self._on_setting_changed)

        self.btn_open_web = ctk.CTkButton(w_row, text="Dashboard megnyitása", width=150, command=self._open_web_browser)
        self.btn_open_web.pack(side="left", padx=10)

    def _load_settings_to_ui(self):
        # App 1
        self.dir_entry.insert(0, ",".join(app1_config.watch_dirs))
        self.model_combo.set(app1_config.whisper_model)
        self.device_combo.set(app1_config.whisper_device)
        self.compute_combo.set(app1_config.whisper_compute_type)
        if app1_config.extract_embedded_subs:
            self.chk_embedded_subs.select()
        else:
            self.chk_embedded_subs.deselect()
        if app1_config.download_online_subtitles:
            self.chk_online_subs.select()
        else:
            self.chk_online_subs.deselect()
        if app1_config.auto_sync_subtitles:
            self.chk_auto_sync.select()
        else:
            self.chk_auto_sync.deselect()
        if app1_config.quality_gate_enabled:
            self.chk_quality_gate.select()
        else:
            self.chk_quality_gate.deselect()
        if app1_config.minimize_to_tray:
            self.chk_minimize_tray.select()
        else:
            self.chk_minimize_tray.deselect()
        
        # App 2
        self.api_provider_combo.set(app2_config.llm_provider)
        
        provider = app2_config.llm_provider.lower().strip()
        if provider == "google":
            self.api_model_entry.configure(values=["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash"])
        elif provider == "openai":
            self.api_model_entry.configure(values=["gpt-4o-mini", "gpt-4o"])
        else:
            self.api_model_entry.configure(values=["llama3", "mistral", "phi3"])
            
        self.api_model_entry.set(app2_config.llm_model)
        
        self.api_key_entry.delete(0, "end")
        self.api_key_entry.insert(0, app2_config.llm_api_key)
        self.target_lang_entry.set(app2_config.target_language or "Hungarian")
        self.batch_size_slider.set(app2_config.translation_batch_size)
        self.batch_size_label.configure(text=str(int(self.batch_size_slider.get())))
        if app2_config.use_sliding_context:
            self.chk_sliding_context.select()
        else:
            self.chk_sliding_context.deselect()
        if app2_config.series_memory_enabled:
            self.chk_series_memory.select()
        else:
            self.chk_series_memory.deselect()
        if app2_config.parallel_translation_workers > 1:
            self.chk_parallel.select()
        else:
            self.chk_parallel.deselect()

        # Integrációk
        if app2_config.plex_auto_refresh:
            self.chk_plex_refresh.select()
        else:
            self.chk_plex_refresh.deselect()
        self.plex_url_entry.delete(0, "end")
        self.plex_url_entry.insert(0, app2_config.plex_url or "")
        self.plex_token_entry.delete(0, "end")
        self.plex_token_entry.insert(0, app2_config.plex_token or "")

        if app2_config.notifications_enabled:
            self.chk_notifications.select()
        else:
            self.chk_notifications.deselect()
        self.discord_url_entry.delete(0, "end")
        self.discord_url_entry.insert(0, app2_config.discord_webhook_url or "")
        self.tg_token_entry.delete(0, "end")
        self.tg_token_entry.insert(0, app2_config.telegram_bot_token or "")
        self.tg_chat_entry.delete(0, "end")
        self.tg_chat_entry.insert(0, app2_config.telegram_chat_id or "")

        if app1_config.web_dashboard_enabled:
            self.chk_web_dashboard.select()
        else:
            self.chk_web_dashboard.deselect()
        self.web_port_entry.delete(0, "end")
        self.web_port_entry.insert(0, str(app1_config.web_dashboard_port or 8080))

    def _save_settings_from_ui(self):
        # App 1
        dirs_str = self.dir_entry.get().strip()
        app1_config.watch_dirs = [d.strip() for d in dirs_str.split(",")] if dirs_str else []
        app1_config.whisper_model = self.model_combo.get()
        app1_config.whisper_device = self.device_combo.get()
        app1_config.whisper_compute_type = self.compute_combo.get()
        app1_config.extract_embedded_subs = bool(self.chk_embedded_subs.get())
        app1_config.download_online_subtitles = bool(self.chk_online_subs.get())
        app1_config.auto_sync_subtitles = bool(self.chk_auto_sync.get())
        app1_config.quality_gate_enabled = bool(self.chk_quality_gate.get())
        app1_config.minimize_to_tray = bool(self.chk_minimize_tray.get())
        
        # App 2
        app2_config.llm_provider = self.api_provider_combo.get()
        app2_config.llm_model = self.api_model_entry.get().strip()
        app2_config.llm_api_key = self.api_key_entry.get().strip()
        app2_config.target_language = self.target_lang_entry.get().strip()
        app2_config.translation_batch_size = int(self.batch_size_slider.get())
        app2_config.use_sliding_context = bool(self.chk_sliding_context.get())
        app2_config.series_memory_enabled = bool(self.chk_series_memory.get())
        app2_config.parallel_translation_workers = 2 if self.chk_parallel.get() else 1

        # Integrációk
        app2_config.plex_auto_refresh = bool(self.chk_plex_refresh.get())
        app2_config.plex_url = self.plex_url_entry.get().strip()
        app2_config.plex_token = self.plex_token_entry.get().strip()
        app2_config.notifications_enabled = bool(self.chk_notifications.get())
        app2_config.discord_webhook_url = self.discord_url_entry.get().strip()
        app2_config.telegram_bot_token = self.tg_token_entry.get().strip()
        app2_config.telegram_chat_id = self.tg_chat_entry.get().strip()

        app1_config.web_dashboard_enabled = bool(self.chk_web_dashboard.get())
        try:
            app1_config.web_dashboard_port = int(self.web_port_entry.get().strip() or "8080")
        except ValueError:
            app1_config.web_dashboard_port = 8080
            
        settings_manager.save()

    def _open_web_browser(self):
        import webbrowser
        self._save_settings_from_ui()
        port = app1_config.web_dashboard_port or 8080
        from plex_subtitler.web import start_web_dashboard_thread
        start_web_dashboard_thread(host=app1_config.web_dashboard_host, port=port)
        webbrowser.open(f"http://localhost:{port}")

    def _on_provider_changed(self, choice):
        # Frissítsük az alapértelmezett beépített listákat, ha a user vált, de a fetch modellt is használhatja
        if choice == "google":
            self.api_model_entry.configure(values=["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash"])
            self.api_model_entry.set("gemini-1.5-flash")
        elif choice == "openai":
            self.api_model_entry.configure(values=["gpt-4o-mini", "gpt-4o"])
            self.api_model_entry.set("gpt-4o-mini")
        else:
            self.api_model_entry.configure(values=["llama3", "mistral", "phi3"])
        self._save_settings_from_ui()

    def _fetch_models_from_api(self):
        """Dinamikus modell-lekérdező, ami a kiválasztott provider API-jához csatlakozik."""
        provider = self.api_provider_combo.get().lower().strip()
        api_key = self.api_key_entry.get().strip()
        
        if provider in ["google", "openai"] and not api_key:
            from tkinter import messagebox
            messagebox.showerror("Hiba", f"API kulcs szükséges a(z) {provider} modellek lekéréséhez!")
            return
            
        orig_text = self.btn_fetch_models.cget("text")
        self.btn_fetch_models.configure(text="Lekérdezés...", state="disabled")
        
        def _worker():
            try:
                from plex_subtitler.app2_translator.llm_client import get_available_models
                from plex_subtitler.config import app2_config
                
                local_url = app2_config.llm_base_url or "http://localhost:11434"
                models = get_available_models(provider, api_key=api_key, local_url=local_url)
                
                def _update_ui():
                    if models:
                        self.api_model_entry.configure(values=models)
                        self.api_model_entry.set(models[0])
                        self._save_settings_from_ui()
                    self.btn_fetch_models.configure(text=orig_text, state="normal")
                    
                self.after(0, _update_ui)
                
            except Exception as e:
                def _show_error():
                    from tkinter import messagebox
                    messagebox.showerror("Hálózati Hiba", str(e))
                    self.btn_fetch_models.configure(text=orig_text, state="normal")
                self.after(0, _show_error)
                
        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _on_setting_changed(self, event=None):
        """Eseménykezelő: gépeléskor azonnal mentjük a beállításokat (pl. API kulcsot)."""
        self._save_settings_from_ui()

    def _on_slider_changed(self, value):
        self.batch_size_label.configure(text=str(int(value)))
        self._save_settings_from_ui()

    def _browse_single_srt(self):
        from pathlib import Path
        file_path = filedialog.askopenfilename(
            title="Válassz egy feliratfájlt",
            filetypes=[("SRT Subtitles", "*.srt")]
        )
        if file_path:
            self._save_settings_from_ui()
            log.info("▶ Egyedi SRT fordítása indul: %s", file_path)
            threading.Thread(target=self._process_single_srt, args=(Path(file_path),), daemon=True).start()

    def _stop_single_srt(self):
        if hasattr(self, '_single_srt_stop_event') and self._single_srt_stop_event:
            self._single_srt_stop_event.set()
            self.btn_single_stop.configure(state="disabled", text="Leállítás...")

    def _process_single_srt(self, file_path):
        import time
        self._single_srt_stop_event = threading.Event()
        
        orig_text = "Egyedi SRT fájl kiválasztása"
        orig_fg = self.btn_single_srt.cget("fg_color")
        orig_hover = self.btn_single_srt.cget("hover_color")

        def _start_ui():
            self.btn_single_srt.configure(text="Feldolgozás...", fg_color="#cc7a00", hover_color="#a36200", state="disabled")
            self.btn_single_stop.configure(state="normal", text="⏹ Leállítás")
            self.tr_start_btn.configure(state="disabled")

        def _end_ui():
            if self._single_srt_stop_event.is_set():
                self.tr_progress_label.configure(text="Megszakítva, részeredmény mentve.")
            else:
                self.btn_single_srt.configure(text="Kész!", fg_color="#2b7a0b", hover_color="#1e5408")
                self.tr_progress_bar.set(1.0)
                self.tr_progress_label.configure(text="Fordítás kész!")
                
            self.btn_single_stop.configure(state="disabled", text="⏹ Leállítás")
            
            def _revert():
                self.btn_single_srt.configure(text=orig_text, fg_color=orig_fg, hover_color=orig_hover, state="normal")
                self.tr_start_btn.configure(state="normal")
                self.tr_progress_bar.set(0)
                if not self._single_srt_stop_event.is_set():
                    self.tr_progress_label.configure(text="Készen áll a fordításra.")
            self.after(3000, _revert)

        self.after(0, _start_ui)
        
        try:
            from plex_subtitler.app2_translator.translator_daemon import process_single_srt
            
            def _update_progress(current, total, elapsed, eta):
                def _ui_update():
                    if total > 0:
                        pct = current / total
                        eta_str = time.strftime("%H:%M:%S", time.gmtime(eta))
                        elapsed_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
                        
                        self.tr_progress_bar.set(pct)
                        self.tr_progress_label.configure(
                            text=f"Fordítás folyamatban: {int(pct*100)}% | Eltelt: {elapsed_str} | ETA: {eta_str}"
                        )
                self.after(0, _ui_update)
                
            process_single_srt(file_path, progress_callback=_update_progress, stop_event=self._single_srt_stop_event)
            
            log.info("Egyedi SRT fájl feldolgozása befejeződött.")
        except Exception as e:
            log.exception("Hiba történt az SRT fordítása során: %s", e)
        finally:
            self.after(0, _end_ui)

    def _browse_directory(self):
        folder = filedialog.askdirectory(title="Válassz figyelt mappát")
        if folder:
            current = self.dir_entry.get().strip()
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, f"{current},{folder}" if current else folder)
            self._save_settings_from_ui()

    def _browse_single_file(self):
        from pathlib import Path
        file_path = filedialog.askopenfilename(
            title="Válassz egy videófájlt",
            filetypes=[("Video files", "*.mkv *.mp4 *.avi")]
        )
        if file_path:
            self._save_settings_from_ui()
            log.info("▶ Egyedi fájl feldolgozása indul: %s", file_path)
            threading.Thread(target=self._process_single_file, args=(Path(file_path),), daemon=True).start()

    def _process_single_file(self, file_path):
        orig_text = "Egyedi fájl kiválasztása"
        orig_fg = self.btn_single_file.cget("fg_color")
        orig_hover = self.btn_single_file.cget("hover_color")

        def _start_ui():
            self.btn_single_file.configure(text="Feldolgozás...", fg_color="#cc7a00", hover_color="#a36200", state="disabled")
            self.ext_start_btn.configure(state="disabled")
            self.ext_progress_bar.start()

        def _end_ui():
            self.ext_progress_bar.stop()
            self.btn_single_file.configure(text="Kész!", fg_color="#2b7a0b", hover_color="#1e5408")
            
            def _revert():
                self.btn_single_file.configure(text=orig_text, fg_color=orig_fg, hover_color=orig_hover, state="normal")
                self.ext_start_btn.configure(state="normal")
            self.after(3000, _revert)

        self.after(0, _start_ui)
        
        try:
            processor = FileProcessor()
            processor.process(file_path)
            log.info("Egyedi fajl feldolgozasa befejezodott.")
        except Exception as e:
            log.exception("Hiba történt a fájl feldolgozása során: %s", e)
        finally:
            self.after(0, _end_ui)

    def _run_hardware_detection(self):
        hw = HardwareManager()
        profile = hw.apply_auto_fallback(force_save=False)
        self.model_combo.set(profile["whisper_model"])
        self.device_combo.set(profile["whisper_device"])
        self.compute_combo.set(profile["whisper_compute_type"])
        self._save_settings_from_ui()

    # --- Extractor Vezérlés ---
    def _start_extractor(self):
        self._save_settings_from_ui()
        if not app1_config.watch_dirs:
            log.error("Nincs megadva figyelt mappa!")
            return
            
        self.orig_ext_btn_text = self.ext_start_btn.cget("text")
        self.orig_ext_btn_fg = self.ext_start_btn.cget("fg_color")
        self.orig_ext_btn_hover = self.ext_start_btn.cget("hover_color")
        
        self.ext_start_btn.configure(text="Figyelés...", fg_color="#cc7a00", hover_color="#a36200", state="disabled")
        self.ext_stop_btn.configure(state="normal")
        self.ext_progress_bar.start()
        
        self.extractor_daemon = WatcherDaemon()
        threading.Thread(target=self.extractor_daemon.start, daemon=True).start()

    def _stop_extractor(self):
        self.ext_stop_btn.configure(state="disabled")
        if self.extractor_daemon:
            self.extractor_daemon._stop_event.set()
        
        self.ext_progress_bar.stop()
        self.ext_start_btn.configure(text="Kész!", fg_color="#2b7a0b", hover_color="#1e5408")
        def _revert():
            self.ext_start_btn.configure(text=self.orig_ext_btn_text, fg_color=self.orig_ext_btn_fg, hover_color=self.orig_ext_btn_hover, state="normal")
        self.after(3000, _revert)

    # --- Translator Vezérlés ---
    def _start_translator(self):
        self._save_settings_from_ui()
        if not app1_config.watch_dirs:
            log.error("Nincs megadva figyelt mappa!")
            return
        if not app2_config.llm_api_key and app2_config.llm_provider != "local":
            log.warning("Figyelem: Nincs megadva API kulcs, a hívások elbukhatnak!")
            
        self.orig_tr_btn_text = self.tr_start_btn.cget("text")
        self.orig_tr_btn_fg = self.tr_start_btn.cget("fg_color")
        self.orig_tr_btn_hover = self.tr_start_btn.cget("hover_color")
        
        self.tr_start_btn.configure(text="Fordító fut...", fg_color="#cc7a00", hover_color="#a36200", state="disabled")
        self.tr_stop_btn.configure(state="normal")
        
        self.translator_daemon = TranslatorDaemon()
        
        def _update_translator_progress(current, total, elapsed=0, eta=0):
            def _ui_update():
                if total > 0:
                    import time
                    pct = current / total
                    eta_str = time.strftime("%H:%M:%S", time.gmtime(eta))
                    elapsed_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
                    
                    self.tr_progress_bar.set(pct)
                    self.tr_progress_label.configure(
                        text=f"Fordítás folyamatban: {int(pct*100)}% | Eltelt: {elapsed_str} | ETA: {eta_str}"
                    )
            self.after(0, _ui_update)
            
        def _translation_finished():
            def _ui_update():
                self.tr_progress_bar.set(1.0)
                self.tr_progress_label.configure(text="Fordítás kész!")
                def _reset():
                    self.tr_progress_bar.set(0)
                    self.tr_progress_label.configure(text="Készen áll a fordításra.")
                self.after(3000, _reset)
            self.after(0, _ui_update)

        self.translator_daemon.progress_callback = _update_progress
        self.translator_daemon.finished_callback = _translation_finished
        
        # Átvizsgálja a meglévő SRT-ket
        threading.Thread(target=self.translator_daemon.scan_existing, daemon=True).start()
        # Elindítja a watchert
        threading.Thread(target=self.translator_daemon.start, daemon=True).start()

    def _stop_translator(self):
        self.tr_stop_btn.configure(state="disabled")
        if self.translator_daemon:
            self.translator_daemon._stop_event.set()
            
        self.tr_start_btn.configure(text="Kész!", fg_color="#2b7a0b", hover_color="#1e5408")
        def _revert():
            self.tr_start_btn.configure(text=self.orig_tr_btn_text, fg_color=self.orig_tr_btn_fg, hover_color=self.orig_tr_btn_hover, state="normal")
        self.after(3000, _revert)

    # --- Integrációk és Tesztek ---
    def _test_plex(self):
        self._save_settings_from_ui()
        from plex_subtitler.notifier import refresh_plex_library
        from tkinter import messagebox
        ok = refresh_plex_library()
        if ok:
            messagebox.showinfo("Plex Siker", "Sikeres kapcsolat a Plex Media Serverrel! A könyvtárfrissítés lefutott.")
        else:
            messagebox.showwarning("Plex Figyelmeztetés", "Nem sikerült elérni a Plex Media Servert. Ellenőrizd a címet (pl. http://localhost:32400) és az X-Plex-Token-t.")

    def _test_notifications(self):
        self._save_settings_from_ui()
        from plex_subtitler.notifier import send_discord_notification, send_telegram_notification
        from tkinter import messagebox
        sent = False
        if app2_config.discord_webhook_url:
            send_discord_notification("Plex Subtitler Teszt", "Ez egy tesztértesítés a Plex Subtitlertől! A Discord webhook működik.")
            sent = True
        if app2_config.telegram_bot_token and app2_config.telegram_chat_id:
            send_telegram_notification("Plex Subtitler Teszt", "Ez egy tesztértesítés a Plex Subtitlertől! A Telegram kapcsolat működik.")
            sent = True

        if sent:
            messagebox.showinfo("Értesítés", "A tesztüzenet sikeresen elküldve!")
        else:
            messagebox.showwarning("Hiányzó adatok", "Adj meg legalább egy Discord Webhookot vagy Telegram adatokat a teszthez!")

    # --- Rendszertálca (System Tray) Kezelés ---
    def _create_tray_image(self):
        if not HAS_TRAY:
            return None
        try:
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse((4, 4, 60, 60), fill="#E5A00D")
            draw.polygon([(26, 18), (26, 46), (46, 32)], fill="#1E1E1E")
            return img
        except Exception:
            return None

    def _init_tray(self):
        if not HAS_TRAY:
            return
        try:
            img = self._create_tray_image()
            if not img:
                return

            menu = pystray.Menu(
                pystray.MenuItem("Megnyitas", self._tray_open, default=True),
                pystray.MenuItem("Kilepes", self._tray_exit),
            )
            self.tray_icon = pystray.Icon("PlexSubtitler", img, "Plex AI Subtitler", menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception as e:
            log.warning("Nem sikerült elindítani a tálcaikont: %s", e)
            self.tray_icon = None

    def _on_window_close(self):
        self._save_settings_from_ui()
        if app1_config.minimize_to_tray and HAS_TRAY and self.tray_icon:
            self.withdraw()
            log.info("Alkalmazas a rendszer talcara rejtve. Kattints a talcaikonra a visszanyitashoz.")
        else:
            self._tray_exit()

    def _tray_open(self, icon=None, item=None):
        def _show():
            self.deiconify()
            self.lift()
            self.focus_force()
        self.after(0, _show)

    def _tray_exit(self, icon=None, item=None):
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self._stop_extractor()
        self._stop_translator()
        self.after(0, self.destroy)

    def _build_help_tab(self):
        help_text = """Plex Subtitler - Hasznalati Utmutato

A program két fő részből áll:

1. Audio Extractor (Hang kinyerése és feliratozás)
• Célja: Videófájlokból (mkv, mp4) kinyeri a hangot, majd az OpenAI Whisper modell segítségével angol (vagy eredeti) nyelvű feliratot (SRT) generál.
• Működése: 
  - Mappafigyelés (Watchdog): Ha megadsz egy vagy több mappát, a program figyeli őket, és az új videókhoz automatikusan legyártja a feliratot.
  - Egyedi fájl: Ha csak egy konkrét videót szeretnél feldolgozni, használd az 'Egyedi fájl kiválasztása' gombot.
• Modellek: A 'tiny' és 'base' gyors, de pontatlanabb. A 'small' és 'medium' jó egyensúly. A 'large' modellek nagyon pontosak, de sok VRAM-ot igényelnek.
• Precizitás: fp16 ajánlott dedikált videókártyához (GPU). Ha gyengébb a gép, próbáld meg az int8 opciót.

2. AI Translator (Mesterséges Intelligencia Fordító)
• Célja: A meglévő SRT fájlokat (pl. amit az Extractor generált) lefordítja a kívánt nyelvre (pl. magyarra) LLM (Nagy Nyelvi Modell) segítségével.
• Működése: Automatikusan figyeli a mappákat angol SRT-ket keresve, és ha talál olyat, aminek még nincs meg a .hu.srt párja, azt lefordítja.
• API Kulcsok: 
  - Google Gemini API: Ingyenesen igényelhető a Google AI Studio oldalon.
  - OpenAI API: Fizetős (pl. gpt-4o-mini).
  - A kulcsot elég egyszer megadni, azonnal elmentődik a beállítások közé.
• Blokkméret: Ha a fordítás elcsúszik vagy hibás, érdemes a blokkmérettel (egyszerre küldött sorok száma) kísérletezni. Ajánlott érték: 30.

Fontos: A folyamat állapota a Rendszernaplóban követhető. Nagy fájlok feldolgozása vagy fordítása (különösen CPU-n) hosszú perceket vehet igénybe!"""

        self.help_textbox = ctk.CTkTextbox(self.tab_help, wrap="word", font=ctk.CTkFont(size=13))
        self.help_textbox.pack(fill="both", expand=True, padx=10, pady=10)
        self.help_textbox.insert("0.0", help_text)
        self.help_textbox.configure(state="disabled")


def run_gui():
    app = PlexSubtitlerApp()
    app.mainloop()

if __name__ == "__main__":
    run_gui()
