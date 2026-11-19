"""Önálló 1-Click Gyors mód ablak natív Drag & Drop és sorozat-figyelő támogatással."""

from __future__ import annotations

import logging
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from plex_subtitler.app1_extractor.file_processor import SUPPORTED_EXTENSIONS
from plex_subtitler.app1_extractor.gui_dialogs import (
    DND_FILES,
    _HAS_DND,
    GuiLogHandler,
    HealthStatusBar,
    _apply_window_icon,
    _ask_stream_selection_if_needed,
    _BaseCTk,
    _open_history_window,
    _open_tutorial_window,
    _show_friendly_error_dialog,
)
from plex_subtitler.app1_extractor.pipeline import run_batch_one_click
from plex_subtitler.app1_extractor.series_cache import SeriesCacheManager
from plex_subtitler.app1_extractor.watcher import WatcherDaemon
from plex_subtitler.app2_translator.translator_daemon import process_single_srt
from plex_subtitler.config import app1_config, app2_config, settings_manager
from plex_subtitler.error_helper import explain_error, export_diagnostic_zip
from plex_subtitler.logger import get_log_file_path, get_logger

log = get_logger("GUI", level="INFO")


class QuickSubtitlerWindow(_BaseCTk):
    """Egyszerűsített 1-Click Gyors mód ablak natív Drag & Drop és sorozat-figyelő támogatással."""

    def __init__(self, initial_paths: list[Path] | None = None) -> None:
        super().__init__()
        self.title("Plex AI Subtitler — 1-Click Gyors Feliratozó (Drag & Drop)")
        self.geometry("700x640")
        self.resizable(True, True)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        _apply_window_icon(self)

        self._stop_event: threading.Event | None = None
        self._series_watcher: WatcherDaemon | None = None
        self._series_cache = SeriesCacheManager()
        self._health_after_id: str | None = None
        self._gui_log_handler: GuiLogHandler | None = None
        self._gui_loggers: list[logging.Logger] = []
        self._build_quick_ui()
        self._setup_quick_logger()
        self._register_drag_and_drop()
        self._health_after_id = self.after(250, self.health_bar.refresh_async)
        log.info(
            "⚡ Gyors Feliratozó (1-Click) elindult | Whisper=%s (%s) | AI=%s (%s) | Napló='%s'",
            app1_config.whisper_model,
            app1_config.whisper_device,
            app2_config.llm_provider,
            app2_config.llm_model,
            get_log_file_path(),
        )

        if initial_paths:
            self.after(200, lambda: self._start_paths(initial_paths))

    def destroy(self) -> None:
        if hasattr(self, "health_bar"):
            self.health_bar._closed = True
        if self._health_after_id is not None:
            try:
                self.after_cancel(self._health_after_id)
            except Exception:
                pass
            self._health_after_id = None
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
            self.dnd_bind("<<Drop>>", self._on_drop_files)
        except Exception as exc:
            log.debug("Drag & Drop regisztráció kihagyva: %s", exc)

    def _on_drop_files(self, event: tk.Event) -> None:
        raw = getattr(event, "data", "")
        if not raw:
            return
        try:
            dropped = [Path(p) for p in self.tk.splitlist(raw) if p]
        except Exception:
            dropped = [Path(raw)]
        if dropped:
            log.info("🖱️ Behúzva %d elem: %s", len(dropped), ", ".join(p.name for p in dropped[:5]))
            self._start_paths(dropped)

    def _setup_quick_logger(self) -> None:
        handler = GuiLogHandler(self.quick_log_box)
        self._gui_log_handler = handler
        self._gui_loggers = []
        for logger_name in (
            "GUI",
            "plex_subtitler.app1_extractor.audio_extractor",
            "plex_subtitler.app1_extractor.file_processor",
            "plex_subtitler.app1_extractor.opensubtitles_client",
            "plex_subtitler.app1_extractor.series_cache",
            "plex_subtitler.app1_extractor.hardware_manager",
            "plex_subtitler.app1_extractor.transcriber",
            "plex_subtitler.app1_extractor.watcher",
            "plex_subtitler.app2_translator.llm_client",
            "plex_subtitler.app2_translator.plex_client",
            "plex_subtitler.app2_translator.sync_verifier",
            "plex_subtitler.app2_translator.translator_daemon",
        ):
            lgr = logging.getLogger(logger_name)
            lgr.addHandler(handler)
            self._gui_loggers.append(lgr)

    def _build_quick_ui(self) -> None:
        frame = ctk.CTkFrame(self)
        frame.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            frame,
            text="🎬 Film / Sorozat → Magyar Felirat (1-Click)",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(pady=(8, 4))

        ctk.CTkLabel(
            frame,
            text=(
                f"Motor: {app2_config.llm_provider} ({app2_config.llm_model}) | "
                f"Whisper: {app1_config.whisper_model} ({app1_config.whisper_device})"
            ),
            font=ctk.CTkFont(size=12),
            text_color="#aaaaaa",
        ).pack(pady=(0, 8))

        self.health_bar = HealthStatusBar(frame)
        self.health_bar.pack(fill="x", padx=12, pady=(0, 6))

        drop_banner = ctk.CTkFrame(frame, fg_color="#1d2d44", border_width=2, border_color="#3b82f6")
        drop_banner.pack(fill="x", padx=12, pady=6)
        ctk.CTkLabel(
            drop_banner,
            text="🖱️ Húzz ide filmet (.mkv / .mp4), több epizódot, vagy egész sorozat-mappát!",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#e2e8f0",
        ).pack(pady=14)

        pick_row = ctk.CTkFrame(frame, fg_color="transparent")
        pick_row.pack(fill="x", padx=12, pady=4)

        self.btn_pick = ctk.CTkButton(
            pick_row,
            text="📂 Film(ek) kiválasztása",
            height=44,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#2b7a0b",
            hover_color="#1e5408",
            command=self._pick_file,
        )
        self.btn_pick.pack(side="left", expand=True, fill="x", padx=(0, 6))

        self.btn_pick_folder = ctk.CTkButton(
            pick_row,
            text="📁 Sorozat mappa kiválasztása",
            height=44,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#1d6fa5",
            hover_color="#15537c",
            command=self._pick_folder,
        )
        self.btn_pick_folder.pack(side="left", expand=True, fill="x")

        self.chk_watch_series = ctk.CTkCheckBox(
            frame,
            text="👁️ Figyelje az új epizódokat a behúzott sorozat-mappában (újak az első helyre!)",
        )
        self.chk_watch_series.select()
        self.chk_watch_series.pack(anchor="w", padx=14, pady=(4, 4))

        self.status_label = ctk.CTkLabel(
            frame,
            text="Készen áll — válassz vagy húzz be filmet / sorozat mappát!",
            font=ctk.CTkFont(size=13),
            wraplength=620,
        )
        self.status_label.pack(pady=(6, 4))

        self.progress = ctk.CTkProgressBar(frame)
        self.progress.pack(fill="x", padx=12, pady=4)
        self.progress.set(0)

        bottom = ctk.CTkFrame(frame, fg_color="transparent")
        bottom.pack(fill="x", padx=12, pady=(8, 6))

        self.btn_stop = ctk.CTkButton(
            bottom,
            text="⏹ Megszakítás",
            fg_color="#8a2020",
            hover_color="#661414",
            state="disabled",
            command=self._stop_work,
        )
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=(0, 6))

        ctk.CTkButton(
            bottom,
            text="📜 Előzmények",
            width=105,
            fg_color="#4b5563",
            hover_color="#374151",
            command=lambda: _open_history_window(
                self, on_resume_path=lambda p: self._start_paths([p])
            ),
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            bottom,
            text="📖 Tutorial",
            width=88,
            fg_color="#1d6fa5",
            hover_color="#15537c",
            command=lambda: _open_tutorial_window(self, default_advanced=False),
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            bottom,
            text="📦 ZIP",
            width=66,
            fg_color="#444444",
            hover_color="#333333",
            command=self._export_zip,
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            bottom,
            text="⚙️ Haladó",
            fg_color="#3b5998",
            hover_color="#2d4373",
            command=self._open_full_gui,
        ).pack(side="left", expand=True, fill="x")

        ctk.CTkLabel(
            frame,
            text="Élő napló (közbeni szövegek, sorozat cache és összegzés):",
            font=ctk.CTkFont(size=11, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(6, 0))

        self.quick_log_box = ctk.CTkTextbox(
            frame,
            height=180,
            state="disabled",
            fg_color="#1e1e1e",
        )
        self.quick_log_box.pack(fill="both", expand=True, padx=12, pady=(4, 8))

    def _export_zip(self) -> None:
        try:
            zp = export_diagnostic_zip()
            messagebox.showinfo("Hibacsomag mentve", f"A kulcsmentesített hibacsomag elmentve:\n{zp}")
        except Exception as exc:
            _show_friendly_error_dialog(self, exc, on_fix_applied=self.health_bar.refresh_async)

    def _open_log_file(self) -> None:
        log_path = get_log_file_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not log_path.exists():
            log_path.write_text("", encoding="utf-8")
        if hasattr(os, "startfile"):
            os.startfile(str(log_path))

    def _pick_file(self) -> None:
        selected = filedialog.askopenfilenames(
            title="Válassz egy vagy több filmet / SRT feliratot",
            filetypes=[("Videó és felirat", "*.mkv *.mp4 *.avi *.mov *.m4v *.ts *.srt")],
        )
        if selected:
            self._start_paths([Path(s) for s in selected])

    def _pick_folder(self) -> None:
        selected = filedialog.askdirectory(title="Válassz sorozat vagy film mappát")
        if selected:
            self._start_paths([Path(selected)])

    def _start_paths(self, paths: list[Path]) -> None:
        if self._series_watcher:
            self._series_watcher.stop()
            self._series_watcher = None

        stream_overrides: dict[Path, tuple[int | None, int | None]] = {}
        if app1_config.confirm_streams_on_drop:
            for p in paths:
                resolved = Path(p).resolve()
                if resolved.is_file() and resolved.suffix.lower() in SUPPORTED_EXTENSIONS:
                    ok, sub_idx, audio_idx = _ask_stream_selection_if_needed(self, resolved)
                    if not ok:
                        return
                    stream_overrides[resolved] = (sub_idx, audio_idx)

        self._stop_event = threading.Event()
        self.btn_pick.configure(state="disabled")
        self.btn_pick_folder.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress.set(0)
        self.status_label.configure(text=f"Előkészítés ({len(paths)} elem)...")

        folders = [str(p.resolve()) for p in paths if p.is_dir()]
        watch_after = bool(self.chk_watch_series.get()) and bool(folders)

        def _worker() -> None:
            results: list[Path] = []
            caught_exc: Exception | None = None
            try:
                results = run_batch_one_click(
                    paths,
                    cache_manager=self._series_cache,
                    status_callback=lambda msg: self.after(0, lambda m=msg: self.status_label.configure(text=m)),
                    progress_callback=lambda val: self.after(0, lambda v=val: self.progress.set(v)),
                    stop_event=self._stop_event,
                    stream_overrides=stream_overrides,
                )
            except Exception as exc:
                caught_exc = exc
                log.exception("Hiba a gyors módban: %s", exc)

            def _done() -> None:
                if caught_exc is not None:
                    self.btn_pick.configure(state="normal")
                    self.btn_pick_folder.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    friendly = explain_error(caught_exc)
                    self.status_label.configure(text=f"❌ [{friendly.code}] {friendly.title}")
                    _show_friendly_error_dialog(self, caught_exc, on_fix_applied=self.health_bar.refresh_async)
                    return

                if self._stop_event and self._stop_event.is_set():
                    self.btn_pick.configure(state="normal")
                    self.btn_pick_folder.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.status_label.configure(text="⏹ Megszakítva (részeredmény elmentve).")
                    return

                self.progress.set(1.0)
                if watch_after:
                    self.status_label.configure(
                        text=f"✅ Kész ({len(results)} új felirat). Figyelés aktív az új epizódokra!"
                    )
                    self._start_series_watcher(folders)
                else:
                    self.btn_pick.configure(state="normal")
                    self.btn_pick_folder.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    if results:
                        self.status_label.configure(text=f"✅ Kész! {len(results)} felirat elkészült.")
                    else:
                        self.status_label.configure(text="✅ Minden epizód már le volt fordítva (cache).")

            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    def _start_series_watcher(self, folders: list[str]) -> None:
        def _on_new_srt(srt_path: Path) -> None:
            self.after(0, lambda: self.status_label.configure(text=f"⚡ Új epizód fordítása: {srt_path.name}"))
            out = process_single_srt(srt_path, stop_event=self._stop_event, resume=True)
            if out:
                for ext in SUPPORTED_EXTENSIONS:
                    vid = srt_path.with_suffix(ext)
                    if vid.exists():
                        self._series_cache.mark_episode(vid, "done", src_srt=srt_path, translated_srt=out)
                        break
                self.after(0, lambda: self.status_label.configure(text=f"✅ Új epizód kész: {out.name}"))

        self._series_watcher = WatcherDaemon(on_srt_ready=_on_new_srt, extra_dirs=folders)
        threading.Thread(target=self._series_watcher.start, daemon=True).start()

    def _stop_work(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._series_watcher:
            self._series_watcher.stop()
            self._series_watcher = None
        self.btn_stop.configure(state="disabled")
        self.btn_pick.configure(state="normal")
        self.btn_pick_folder.configure(state="normal")

    def _open_full_gui(self) -> None:
        if self._series_watcher:
            self._series_watcher.stop()
        app2_config.ui_mode = "advanced"
        settings_manager.save()
        self.destroy()
        from plex_subtitler.app1_extractor.gui import run_gui

        run_gui()


def run_quick_mode(initial_paths: list[Path] | Path | None = None) -> None:
    """Elindítja a 1-Click Gyors módot."""
    if isinstance(initial_paths, Path):
        paths: list[Path] | None = [initial_paths]
    else:
        paths = initial_paths
    win = QuickSubtitlerWindow(initial_paths=paths)
    win.mainloop()
