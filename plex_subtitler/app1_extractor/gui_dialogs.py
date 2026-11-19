"""Megosztott GUI párbeszédablakok, widget-segédek, Drag & Drop alap és tutorial szövegek."""

from __future__ import annotations

import logging
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from typing import Callable

import customtkinter as ctk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _HAS_DND = True
except ImportError:
    DND_FILES = None
    TkinterDnD = None
    _HAS_DND = False

from plex_subtitler.app1_extractor.audio_extractor import format_stream_summary, probe_streams
from plex_subtitler.app1_extractor.file_processor import SUPPORTED_EXTENSIONS
from plex_subtitler.config import APP_ICON_PATH, app1_config
from plex_subtitler.error_helper import (
    HealthCheckItem,
    build_diagnostic_clipboard_report,
    check_system_health,
    execute_quick_fix,
    explain_error,
    export_diagnostic_zip,
    load_history_entries,
)
from plex_subtitler.logger import flush_all_logs, get_log_file_path, get_logger

_gui_crash_log = get_logger("plex_subtitler.gui_crash")


def _apply_window_icon(win: tk.Misc) -> None:
    try:
        if APP_ICON_PATH.exists():
            win.iconbitmap(str(APP_ICON_PATH))
    except Exception:
        pass


def _handle_tk_callback_exception(win: tk.Misc, exc: type[BaseException], val: BaseException, tb) -> None:
    """Biztosítja, hogy a Tkinter/CustomTkinter callback hibák --windowed módban is naplózásra kerüljenek."""
    if issubclass(exc, KeyboardInterrupt):
        return
    _gui_crash_log.critical("Kezeletlen GUI esemény-kivétel:", exc_info=(exc, val, tb))
    flush_all_logs()
    try:
        _show_friendly_error_dialog(win, val)
    except Exception:
        pass


if _HAS_DND and TkinterDnD is not None:

    class _BaseCTk(ctk.CTk, TkinterDnD.DnDWrapper):  # type: ignore[misc]
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)

        def report_callback_exception(self, exc, val, tb) -> None:  # type: ignore[override]
            _handle_tk_callback_exception(self, exc, val, tb)

else:

    class _BaseCTk(ctk.CTk):  # type: ignore[no-redef]
        def report_callback_exception(self, exc, val, tb) -> None:  # type: ignore[override]
            _handle_tk_callback_exception(self, exc, val, tb)


def _parse_glossary_text(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.splitlines():
        cleaned = line.strip()
        if not cleaned or "=" not in cleaned:
            continue
        k, v = cleaned.split("=", 1)
        if k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def _format_glossary_text(glossary: dict[str, str]) -> str:
    return "\n".join(f"{k} = {v}" for k, v in glossary.items())


_TUTORIAL_SIMPLE_TEXT = (
    "Üdvözlünk a Plex AI Subtitler Egyszerű nézetében!\n"
    "==================================================\n\n"
    "MAGYAR FELIRAT KÉSZÍTÉSE 3 EGYSZERŰ LÉPÉSBEN:\n\n"
    "1. LÉPÉS — Húzd be a filmet vagy a sorozat mappát:\n"
    "   • Fogj egy filmet (.mkv / .mp4) vagy egy egész sorozatévad-mappát (pl. 'S01'),\n"
    "     és dobd rá a kék keretes dobozra — vagy kattints a 'Film / Sorozat kiválasztása' gombra.\n"
    "   • Ha sorozat-mappát adsz meg és be van pipálva a 'Figyelje az új epizódokat',\n"
    "     a később bemásolt új részeket is automatikusan, elsőbbséggel lefordítja.\n\n"
    "2. LÉPÉS — Várj, amíg a zöld csík végigér:\n"
    "   • Ha a filmben már van angol/spanyol/német felirat, 1–2 perc alatt kész a magyar.\n"
    "   • Ha nincs benne felirat, a videókártya (Whisper GPU) hallgatja le a hangot (~3–6 perc).\n"
    "   • Ha közben le kell állítanod ('Megszakítás'), semmi nem vész el:\n"
    "     következő indításkor onnan folytatja, ahol abbamaradt (Előzmények -> Folytatás).\n\n"
    "3. LÉPÉS — Nézd meg Plexen a TV-n:\n"
    "   • A kész 'film_neve.hu.srt' pontosan a film mellé kerül — a Plex magától megtalálja.\n\n"
    "MIT TEGYÉL, HA HIBA TÖRTÉNIK?\n"
    "   • A program rövid hibakóddal (pl. ERR-OLLAMA-01, ERR-VRAM-03) magyarul kiírja az okot.\n"
    "   • A hibaablakban lévő 1-kattintásos javítógombbal az Ollama indítása, modell-letöltés\n"
    "     vagy VRAM-ürítés azonnal megoldható.\n"
    "   • További hibakereséshez kattints a 'Hibacsomag (.zip)' gombra (API kulcsok automatikusan rejtve).\n"
)

_TUTORIAL_ADVANCED_TEXT = (
    "Haladó Útmutató — GPU Profilok, Szinkron & Plex API\n"
    "===================================================\n\n"
    "1. Automatikus Hardver-profilok (Ultimate Telepítő & Hardver-felmérés):\n"
    "   • CUDA_ULTRA (12+ GB VRAM, pl. RTX 3080/4080): Whisper large-v3 (cuda/float16) + Ollama gemma3:12b (batch=12).\n"
    "   • CUDA_MID (8–10 GB VRAM): Whisper large-v3 (cuda/float16) + Ollama gemma3:4b (batch=10).\n"
    "   • CUDA_ENTRY (4–6 GB VRAM): Whisper medium (cuda/float16) + Ollama gemma3:4b (batch=8).\n"
    "   • CPU_ONLY: Whisper small (cpu/int8) + Ollama gemma3:4b vagy gemma3:1b (batch=6).\n"
    "   • 'Beágyazott felirat kinyerése először' + 'PGS / VobSub OCR': tartsd bepipálva, így MKV-nál\n"
    "     átugorja a Whispert.\n\n"
    "2. Szinkron-ellenőrzés és Auto-Align:\n"
    "   • A 'Szinkron ellenőrzése (MKV + SRT)' gomb az FFmpeg valós 8000 Hz WAV hangenergia (RMS)\n"
    "     alapján méri meg a felirat pontosságát.\n"
    "   • Az 'Auto-Align' gomb (vagy az 'Auto-szinkron javítás' pipa) automatikusan kiszámolja\n"
    "     az egyenletes elcsúszást (±3000 ms tartományban) és eltolja a feliratot.\n\n"
    "3. Plex Media Server automatikus könyvtár-frissítés:\n"
    "   • Kapcsold be a 'Plex frissítés kész felirat után' opciót.\n"
    "   • Plex URL: általában http://localhost:32400 (vagy a Plex szerver IP-címe).\n"
    "   • Plex Token ('X-Plex-Token'): Plex Webben nyiss meg egy filmet -> '...' -> Get Info ->\n"
    "     View XML -> az URL végén lévő 'X-Plex-Token=...' érték.\n\n"
    "4. Szószedet (Glossary) & Diagnosztika:\n"
    "   • A Szószedet mezőbe soronként írhatsz névpárokat ('Darth Vader = Darth Vader').\n"
    "   • Parancssorból: 'python main.py --hardware-scan', 'python doctor_gpu.py --live-llm' és 'pytest -v'.\n"
)


def _open_tutorial_window(parent: tk.Misc, default_advanced: bool = False) -> None:
    """Megnyit egy kétfüles (Egyszerű / Haladó) oktató ablakot."""
    win = ctk.CTkToplevel(parent)
    win.title("📖 Plex AI Subtitler — Útmutató (Egyszerű & Haladó)")
    win.geometry("760x560")
    win.transient(parent)
    _apply_window_icon(win)

    tabs = ctk.CTkTabview(win)
    tabs.pack(fill="both", expand=True, padx=12, pady=12)
    tab_s = tabs.add("Egyszerű mód")
    tab_a = tabs.add("Haladó (Beállítás & Tippek)")

    box_s = ctk.CTkTextbox(tab_s, wrap="word", font=ctk.CTkFont(size=13))
    box_s.pack(fill="both", expand=True, padx=6, pady=6)
    box_s.insert("0.0", _TUTORIAL_SIMPLE_TEXT)
    box_s.configure(state="disabled")

    box_a = ctk.CTkTextbox(tab_a, wrap="word", font=ctk.CTkFont(size=13))
    box_a.pack(fill="both", expand=True, padx=6, pady=6)
    box_a.insert("0.0", _TUTORIAL_ADVANCED_TEXT)
    box_a.configure(state="disabled")

    tabs.set("Haladó (Beállítás & Tippek)" if default_advanced else "Egyszerű mód")
    win.focus()


def _open_hardware_report_window(
    parent: tk.Misc,
    on_profile_applied: Callable[[], None] | None = None,
) -> None:
    """Megjeleníti a teljes hardver-felmérést és 1-kattintásos profilalkalmazást kínál."""
    from plex_subtitler.app1_extractor.hardware_manager import HardwareManager

    win = ctk.CTkToplevel(parent)
    win.title("💻 Hardver-felmérés és Ajánlott AI Profil")
    win.geometry("680x460")
    win.transient(parent)
    _apply_window_icon(win)

    ctk.CTkLabel(
        win,
        text="💻 Rendszer- és Hardver-felmérés",
        font=ctk.CTkFont(size=16, weight="bold"),
    ).pack(anchor="w", padx=16, pady=(14, 6))

    box = ctk.CTkTextbox(win, wrap="word", font=ctk.CTkFont(family="Consolas", size=12))
    box.pack(fill="both", expand=True, padx=16, pady=6)
    box.insert("0.0", "Hardver-felmérés folyamatban...")
    box.configure(state="disabled")

    btn_row = ctk.CTkFrame(win, fg_color="transparent")
    btn_row.pack(fill="x", padx=16, pady=(6, 14))

    apply_btn = ctk.CTkButton(
        btn_row,
        text="⚙️ Ajánlott profil alkalmazása",
        width=220,
        fg_color="#2b7a0b",
        hover_color="#1e5408",
        state="disabled",
    )
    apply_btn.pack(side="left", padx=(0, 8))

    ctk.CTkButton(
        btn_row,
        text="Bezárás",
        width=100,
        fg_color="#444444",
        hover_color="#333333",
        command=win.destroy,
    ).pack(side="right")

    def _scan() -> None:
        hw = HardwareManager()
        inv = hw.collect_system_inventory()
        rec = hw.get_full_recommended_profile()
        lines = [
            f"OS:               {inv.os_info}",
            f"CPU:              {inv.cpu_name} ({inv.cpu_cores} mag)",
            f"RAM:              {inv.ram_total_gb:.1f} GB összes / {inv.ram_available_gb:.1f} GB szabad",
            f"Szabad tárhely:   {inv.disk_free_gb:.1f} GB",
            f"GPU (PyTorch):    {inv.gpu_name} ({inv.vram_gb:.1f} GB VRAM, CUDA={'IGEN' if inv.has_cuda else 'NEM'})",
        ]
        if inv.nvidia_smi_gpu:
            lines.append(
                f"GPU (nvidia-smi): {inv.nvidia_smi_gpu} ({inv.nvidia_smi_vram_gb:.1f} GB VRAM, driver {inv.nvidia_driver})"
            )
        lines.extend(
            [
                f"FFmpeg:           {'OK' if inv.ffmpeg_available else 'HIÁNYZIK'}",
                f"Tesseract OCR:    {'OK' if inv.tesseract_available else 'Nincs (Whisper fallback)'}",
                f"Ollama:           {'OK' if inv.ollama_available else 'Nincs telepítve'}",
                "",
                f"--- Ajánlott profil ({rec['profile_tier']}) ---",
                f"Whisper:          {rec['whisper_model']} ({rec['whisper_device']} / {rec['whisper_compute_type']})",
                f"Lokális AI:       {rec['llm_model']} (Blokkméret: {rec['translation_batch_size']})",
            ]
        )
        if inv.warnings:
            lines.append("")
            lines.append("--- Figyelmeztetések ---")
            for w in inv.warnings:
                lines.append(f"! {w}")

        text_out = "\n".join(lines)

        def _apply_now() -> None:
            hw.apply_auto_fallback(force_save=True, apply_llm=True)
            if on_profile_applied is not None:
                on_profile_applied()
            messagebox.showinfo(
                "Profil alkalmazva",
                f"Az ajánlott {rec['profile_tier']} profil elmentve:\n"
                f"Whisper: {rec['whisper_model']} ({rec['whisper_device']})\n"
                f"Ollama: {rec['llm_model']} (batch={rec['translation_batch_size']})",
            )

        def _render() -> None:
            if not win.winfo_exists():
                return
            box.configure(state="normal")
            box.delete("0.0", "end")
            box.insert("0.0", text_out)
            box.configure(state="disabled")
            apply_btn.configure(state="normal", command=_apply_now)

        try:
            win.after(0, _render)
        except Exception:
            pass

    threading.Thread(target=_scan, daemon=True).start()
    win.focus()


def _show_friendly_error_dialog(
    parent: tk.Misc,
    exc: Exception | str,
    on_fix_applied: Callable[[], None] | None = None,
) -> None:
    """Érthető, hibakódos, öngyógyító hibaablak ZIP hibacsomaggal és vágólap-másolóval."""
    friendly = explain_error(exc)
    win = ctk.CTkToplevel(parent)
    win.title(f"⚠️ [{friendly.code}] {friendly.title}")
    win.geometry("680x480")
    win.transient(parent)
    _apply_window_icon(win)

    ctk.CTkLabel(
        win,
        text=f"⚠️ [{friendly.code}] {friendly.title}",
        font=ctk.CTkFont(size=16, weight="bold"),
        text_color="#ff8a65",
        wraplength=640,
    ).pack(anchor="w", padx=16, pady=(14, 6))

    box = ctk.CTkTextbox(win, wrap="word", font=ctk.CTkFont(size=13))
    box.pack(fill="both", expand=True, padx=16, pady=6)
    box.insert("0.0", friendly.format_dialog_text())
    box.configure(state="disabled")

    fix_status_lbl = ctk.CTkLabel(win, text="", font=ctk.CTkFont(size=12, weight="bold"))
    fix_status_lbl.pack(anchor="w", padx=16, pady=(0, 2))

    if friendly.quick_fix_id and friendly.quick_fix_label:
        fix_btn = ctk.CTkButton(
            win,
            text=f"🔧 {friendly.quick_fix_label}",
            height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#2b7a0b",
            hover_color="#1e5408",
        )

        def _run_fix() -> None:
            fix_btn.configure(state="disabled", text="⏳ Javítás folyamatban...")
            fix_status_lbl.configure(text="Öngyógyító művelet fut...", text_color="#ffd54f")

            def _worker() -> None:
                ok, msg = execute_quick_fix(friendly.quick_fix_id or "")

                def _done() -> None:
                    fix_status_lbl.configure(
                        text=("✅ " if ok else "❌ ") + msg,
                        text_color="#81c784" if ok else "#ef9a9a",
                    )
                    fix_btn.configure(
                        state="normal",
                        text=("✅ Kész — újrapróbálhatod!" if ok else f"🔧 {friendly.quick_fix_label}"),
                    )
                    if ok and on_fix_applied is not None:
                        on_fix_applied()

                win.after(0, _done)

            threading.Thread(target=_worker, daemon=True).start()

        fix_btn.configure(command=_run_fix)
        fix_btn.pack(fill="x", padx=16, pady=(2, 6))

    btn_row = ctk.CTkFrame(win, fg_color="transparent")
    btn_row.pack(fill="x", padx=16, pady=(4, 14))

    copy_btn = ctk.CTkButton(
        btn_row,
        text="📋 Hibajelentés másolása",
        fg_color="#1d6fa5",
        hover_color="#15537c",
    )

    def _copy_report() -> None:
        report = build_diagnostic_clipboard_report(friendly.technical)
        win.clipboard_clear()
        win.clipboard_append(report)
        copy_btn.configure(text="✅ Vágólapra másolva!")

    copy_btn.configure(command=_copy_report)
    copy_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))

    zip_btn = ctk.CTkButton(
        btn_row,
        text="📦 Hibacsomag (.zip)",
        width=160,
        fg_color="#3b5998",
        hover_color="#2d4373",
    )

    def _save_zip() -> None:
        try:
            zp = export_diagnostic_zip(error_text=friendly.technical)
            zip_btn.configure(text="✅ ZIP mentve!")
            fix_status_lbl.configure(text=f"📦 Hibacsomag mentve: {zp}", text_color="#90caf9")
        except Exception as zexc:
            messagebox.showerror("ZIP hiba", str(zexc))

    zip_btn.configure(command=_save_zip)
    zip_btn.pack(side="left", padx=(0, 6))

    def _open_log() -> None:
        log_path = get_log_file_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if not log_path.exists():
            log_path.write_text("", encoding="utf-8")
        if hasattr(os, "startfile"):
            os.startfile(str(log_path))

    ctk.CTkButton(
        btn_row,
        text="📄 Napló",
        width=85,
        fg_color="#444444",
        hover_color="#333333",
        command=_open_log,
    ).pack(side="left", padx=(0, 6))

    ctk.CTkButton(btn_row, text="Bezárás", width=85, command=win.destroy).pack(side="left")
    win.grab_set()
    win.focus()


def _open_history_window(
    parent: tk.Misc,
    on_resume_path: Callable[[Path], None] | None = None,
) -> None:
    """Megnyitja az 'Előzmények és félbemaradt filmek' áttekintő ablakot."""
    win = ctk.CTkToplevel(parent)
    win.title("📜 Plex Subtitler — Előzmények és félbemaradt filmek")
    win.geometry("780x520")
    win.transient(parent)
    _apply_window_icon(win)

    top = ctk.CTkFrame(win, fg_color="transparent")
    top.pack(fill="x", padx=14, pady=(12, 4))
    ctk.CTkLabel(
        top,
        text="Legutóbbi feldolgozott és félbemaradt filmek / epizódok",
        font=ctk.CTkFont(size=15, weight="bold"),
    ).pack(side="left")

    scroll = ctk.CTkScrollableFrame(win)
    scroll.pack(fill="both", expand=True, padx=14, pady=8)

    def _populate() -> None:
        for child in scroll.winfo_children():
            child.destroy()

        entries = load_history_entries(limit=30)
        if not entries:
            ctk.CTkLabel(
                scroll,
                text="Még nincs rögzített előzmény. Húzz be egy filmet vagy sorozatmappát!",
                text_color="#aaaaaa",
            ).pack(pady=30)
            return

        color_map = {"KÉSZ": "#2e7d32", "RÉSZLEGES": "#c77700", "HIBA": "#b71c1c"}
        for entry in entries:
            row = ctk.CTkFrame(scroll, fg_color="#23272e")
            row.pack(fill="x", pady=3, padx=2)

            badge = ctk.CTkLabel(
                row,
                text=entry.status,
                width=90,
                fg_color=color_map.get(entry.status, "#444444"),
                corner_radius=6,
                font=ctk.CTkFont(size=11, weight="bold"),
            )
            badge.pack(side="left", padx=8, pady=6)

            info_col = ctk.CTkFrame(row, fg_color="transparent")
            info_col.pack(side="left", fill="x", expand=True, padx=4, pady=4)
            ctk.CTkLabel(
                info_col,
                text=f"{entry.title}  ({entry.timestamp})",
                font=ctk.CTkFont(size=13, weight="bold"),
                anchor="w",
            ).pack(fill="x")
            ctk.CTkLabel(
                info_col,
                text=entry.detail,
                font=ctk.CTkFont(size=11),
                text_color="#bbbbbb",
                anchor="w",
            ).pack(fill="x")

            if entry.file_path is not None and entry.file_path.exists():
                if entry.status != "KÉSZ" and on_resume_path is not None:
                    ctk.CTkButton(
                        row,
                        text="▶ Folytatás",
                        width=90,
                        height=28,
                        fg_color="#2b7a0b",
                        hover_color="#1e5408",
                        command=lambda p=entry.file_path: (win.destroy(), on_resume_path(p)),
                    ).pack(side="right", padx=(4, 8))

                ctk.CTkButton(
                    row,
                    text="📂 Mappa",
                    width=82,
                    height=28,
                    fg_color="#3b5998",
                    hover_color="#2d4373",
                    command=lambda p=entry.file_path.parent: os.startfile(str(p)) if hasattr(os, "startfile") else None,
                ).pack(side="right", padx=4)

    ctk.CTkButton(top, text="🔄 Frissítés", width=95, command=_populate).pack(side="right")
    _populate()
    win.focus()


class HealthStatusBar(ctk.CTkFrame):
    """Élő, nem-blokkoló rendszer-állapotjelző sáv (GPU, AI, FFmpeg, Plex)."""

    _STATUS_COLORS = {
        "ok": "#2e7d32",
        "warn": "#c77700",
        "error": "#b71c1c",
        "muted": "#4a4a4a",
    }

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, fg_color="#1b2028", corner_radius=8)
        self._closed = False
        self._pills: dict[str, ctk.CTkLabel] = {}
        self._tooltips: dict[str, ToolTip] = {}

        for key, default_txt in (
            ("gpu", "GPU: ellenőrzés..."),
            ("ai", "AI: ellenőrzés..."),
            ("ffmpeg", "FFmpeg: ..."),
            ("plex", "Plex: ..."),
        ):
            lbl = ctk.CTkLabel(
                self,
                text=default_txt,
                fg_color="#374151",
                corner_radius=6,
                padx=8,
                pady=3,
                font=ctk.CTkFont(size=11, weight="bold"),
            )
            lbl.pack(side="left", padx=4, pady=4)
            self._pills[key] = lbl
            self._tooltips[key] = ToolTip(lbl, "")

        self.refresh_btn = ctk.CTkButton(
            self,
            text="🔄 Állapot",
            width=82,
            height=24,
            font=ctk.CTkFont(size=11),
            fg_color="#374151",
            hover_color="#4b5563",
            command=self.refresh_async,
        )
        self.refresh_btn.pack(side="right", padx=6, pady=4)

    def apply_items(self, items: list[HealthCheckItem]) -> None:
        for item in items:
            lbl = self._pills.get(item.name)
            if lbl is None:
                continue
            lbl.configure(
                text=item.label,
                fg_color=self._STATUS_COLORS.get(item.status, "#4a4a4a"),
            )
            tip = self._tooltips.get(item.name)
            if tip is not None:
                tip.text = item.detail

    def destroy(self) -> None:
        self._closed = True
        super().destroy()

    def refresh_async(self) -> None:
        if self._closed:
            return
        try:
            self.refresh_btn.configure(state="disabled")
        except Exception:
            return

        def _worker() -> None:
            items = check_system_health()
            if self._closed:
                return

            def _ui() -> None:
                if self._closed or not self.winfo_exists():
                    return
                self.apply_items(items)
                self.refresh_btn.configure(state="normal")

            try:
                if not self._closed:
                    self.after(0, _ui)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()


def _ask_stream_selection_if_needed(
    parent: tk.Misc,
    video_path: Path,
) -> tuple[bool, int | None, int | None]:
    """Több hang- vagy feliratsáv esetén opcionális interaktív sávválasztó popupot nyit."""
    if not app1_config.confirm_streams_on_drop or video_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return True, None, None

    summary = format_stream_summary(probe_streams(video_path))
    audios = summary["audio"]
    subs = summary["subtitle"]
    if len(audios) < 2 and len(subs) < 2:
        return True, None, None

    result: dict[str, object] = {"ok": False, "sub": None, "audio": None}
    win = ctk.CTkToplevel(parent)
    win.title(f"🎚️ Sávválasztó előnézet — {video_path.name}")
    win.geometry("560x480")
    win.transient(parent)
    _apply_window_icon(win)

    ctk.CTkLabel(
        win,
        text=f"Több sáv található ebben a fájlban:\n{video_path.name}",
        font=ctk.CTkFont(size=14, weight="bold"),
        wraplength=520,
    ).pack(anchor="w", padx=16, pady=(12, 6))

    scroll = ctk.CTkScrollableFrame(win, height=310)
    scroll.pack(fill="both", expand=True, padx=16, pady=6)

    sub_var = tk.IntVar(value=subs[0]["index"] if any(s["is_text"] or s["is_bitmap"] for s in subs) else -1)
    audio_var = tk.IntVar(value=audios[0]["index"] if audios else 0)

    ctk.CTkLabel(scroll, text="📝 Feliratsáv (ha van, átugorja a Whispert):", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(2, 4))
    for s in subs:
        if s["is_text"] or s["is_bitmap"]:
            ctk.CTkRadioButton(scroll, text=s["label"], variable=sub_var, value=s["index"]).pack(anchor="w", padx=8, pady=2)
    ctk.CTkRadioButton(
        scroll,
        text="🎙️ Ne nyerje ki — Whisper hangátírás használata",
        variable=sub_var,
        value=-1,
    ).pack(anchor="w", padx=8, pady=(2, 10))

    if audios:
        ctk.CTkLabel(scroll, text="🔊 Hangsáv (Whisper átíráshoz):", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(4, 4))
        for a in audios:
            ctk.CTkRadioButton(scroll, text=a["label"], variable=audio_var, value=a["index"]).pack(anchor="w", padx=8, pady=2)

    btn_row = ctk.CTkFrame(win, fg_color="transparent")
    btn_row.pack(fill="x", padx=16, pady=(6, 14))

    def _confirm() -> None:
        result["ok"] = True
        result["sub"] = int(sub_var.get())
        result["audio"] = int(audio_var.get()) if audios else None
        win.destroy()

    ctk.CTkButton(btn_row, text="▶ Indítás a választott sávokkal", fg_color="#2b7a0b", hover_color="#1e5408", command=_confirm).pack(
        side="left", expand=True, fill="x", padx=(0, 6)
    )
    ctk.CTkButton(btn_row, text="Mégse", width=100, fg_color="#666666", command=win.destroy).pack(side="left")

    win.grab_set()
    win.focus()
    win.wait_window()
    return bool(result["ok"]), result["sub"], result["audio"]  # type: ignore[return-value]


class ToolTip:
    """Egyszerű felugró súgó buborék widgetek fölé."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tooltip_window: tk.Toplevel | None = None
        self.schedule_id: str | None = None
        self.widget.bind("<Enter>", self.enter)
        self.widget.bind("<Leave>", self.leave)

    def enter(self, _event: tk.Event | None = None) -> None:
        self.schedule_id = self.widget.after(500, self.show_tooltip)

    def leave(self, _event: tk.Event | None = None) -> None:
        if self.schedule_id is not None:
            self.widget.after_cancel(self.schedule_id)
            self.schedule_id = None
        self.hide_tooltip()

    def show_tooltip(self) -> None:
        if self.tooltip_window or not self.text:
            return
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + 25
        self.tooltip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#2b2b2b",
            foreground="#ffffff",
            relief="solid",
            borderwidth=1,
            font=("Arial", 10, "normal"),
            padx=8,
            pady=5,
        )
        label.pack(ipadx=1)

    def hide_tooltip(self) -> None:
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None


class GuiLogHandler(logging.Handler):
    """Thread-safe logging handler a közös GUI LogBox számára."""

    def __init__(self, text_widget: ctk.CTkTextbox) -> None:
        super().__init__()
        self.text_widget = text_widget
        self.setFormatter(
            logging.Formatter("%(asctime)s | %(name)s | %(message)s", datefmt="%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)

        def append_log() -> None:
            self.text_widget.configure(state="normal")
            self.text_widget.insert("end", msg + "\n")
            self.text_widget.see("end")
            self.text_widget.configure(state="disabled")

        self.text_widget.after(0, append_log)
