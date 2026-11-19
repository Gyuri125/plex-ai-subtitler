"""FFmpeg-alapú hangsáv- és beágyazott felirat-kinyerő modul."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from plex_subtitler.config import (
    app1_config,
    get_preferred_audio_languages,
    get_preferred_subtitle_languages,
)
from plex_subtitler.logger import get_logger

log = get_logger(__name__, level=app1_config.log_level)

_FFMPEG_STREAM_RE = re.compile(
    r"Stream\s+#\d+:\d+(?:\[[^\]]+\])?(?:\((?P<lang>[^)]+)\))?:\s*(?P<type>Audio|Subtitle):\s*(?P<codec>[a-zA-Z0-9_]+)",
    re.IGNORECASE,
)

TEXT_SUBTITLE_CODECS = frozenset({
    "subrip",
    "srt",
    "ass",
    "ssa",
    "mov_text",
    "webvtt",
    "text",
})

BITMAP_SUBTITLE_CODECS = frozenset({
    "hdmv_pgs_subtitle",
    "pgssub",
    "dvd_subtitle",
    "dvdsub",
    "vobsub",
})


class AudioExtractionError(Exception):
    """Hangsáv- vagy felirat-kinyerési hiba."""


def resolve_ffmpeg_executable() -> str:
    """Feloldja az FFmpeg bináris útvonalát (config -> PATH -> imageio_ffmpeg)."""
    configured = (app1_config.ffmpeg_path or "").strip()
    if configured and configured.lower() != "ffmpeg":
        cfg_path = Path(configured)
        if cfg_path.is_file():
            return str(cfg_path)
        found_cfg = shutil.which(configured)
        if found_cfg:
            return found_cfg

    found_path = shutil.which("ffmpeg")
    if found_path:
        return found_path

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise FileNotFoundError(
            "Az 'ffmpeg' nem található sem a PATH-ban, sem az imageio-ffmpeg csomagban!"
        ) from exc


def resolve_ffprobe_executable() -> str | None:
    """Megkeresi az ffprobe binárist, ha elérhető a rendszerben."""
    ffmpeg_exe = Path(resolve_ffmpeg_executable())
    sibling = ffmpeg_exe.with_name("ffprobe.exe" if ffmpeg_exe.suffix.lower() == ".exe" else "ffprobe")
    if sibling.is_file():
        return str(sibling)
    return shutil.which("ffprobe")


def _run_cancellable(
    cmd: list[str],
    timeout: int = 3600,
    stop_event: threading.Event | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Futtat egy külső parancsot stop_event és timeout támogatással."""
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creation_flags,
    )
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if stop_event and stop_event.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise AudioExtractionError("A folyamat felhasználói kérésre megszakítva.")
        if time.monotonic() > deadline:
            proc.kill()
            raise AudioExtractionError(f"FFmpeg időtúllépés ({timeout} mp).")
        time.sleep(0.2)

    stdout, stderr = proc.communicate()
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _probe_via_ffmpeg(video_path: Path) -> dict[str, list[dict]]:
    """Fallback stream-felderítés 'ffmpeg -hide_banner -i' kimenetéből (cím metaadattal)."""
    ffmpeg_exe = resolve_ffmpeg_executable()
    cmd = [ffmpeg_exe, "-hide_banner", "-i", str(video_path)]
    try:
        res = _run_cancellable(cmd, timeout=30)
        stderr_text = res.stderr.decode("utf-8", errors="replace")
        audio_list: list[dict] = []
        sub_list: list[dict] = []
        current_entry: dict | None = None
        for raw_line in stderr_text.splitlines():
            match = _FFMPEG_STREAM_RE.search(raw_line)
            if match:
                stype = match.group("type").lower()
                codec = match.group("codec").lower()
                lang = (match.group("lang") or "").lower()
                entry: dict = {"codec_type": stype, "codec_name": codec, "tags": {"language": lang}}
                if "(comment)" in raw_line.lower():
                    entry["disposition"] = {"comment": 1}
                current_entry = entry
                if stype == "audio":
                    audio_list.append(entry)
                elif stype == "subtitle":
                    sub_list.append(entry)
            elif current_entry is not None and raw_line.startswith("    "):
                tmatch = re.match(r"^\s+title\s*:\s*(.+)$", raw_line, re.IGNORECASE)
                if tmatch:
                    current_entry["tags"]["title"] = tmatch.group(1).strip()
            else:
                current_entry = None
        return {"audio": audio_list, "subtitle": sub_list}
    except Exception as exc:
        log.debug("ffmpeg -i stream elemzés sikertelen ('%s'): %s", video_path.name, exc)
        return {"audio": [], "subtitle": []}


def probe_streams(video_path: Path) -> dict[str, list[dict]]:
    """Lekérdezi a videofájl audio és szöveges felirat sávjait (ffprobe vagy ffmpeg fallback)."""
    ffprobe_exe = resolve_ffprobe_executable()
    if not ffprobe_exe:
        return _probe_via_ffmpeg(video_path)

    cmd = [
        ffprobe_exe,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        str(video_path),
    ]
    try:
        res = _run_cancellable(cmd, timeout=30)
        if res.returncode != 0:
            return _probe_via_ffmpeg(video_path)
        data = json.loads(res.stdout.decode("utf-8", errors="replace"))
        streams = data.get("streams", [])
        return {
            "audio": [s for s in streams if s.get("codec_type") == "audio"],
            "subtitle": [s for s in streams if s.get("codec_type") == "subtitle"],
        }
    except Exception as exc:
        log.debug("ffprobe elemzés sikertelen ('%s'): %s", video_path.name, exc)
        return _probe_via_ffmpeg(video_path)


def format_stream_summary(streams: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Emberbarát címkékkel látja el a felismert audio és felirat sávokat (GUI előnézethez)."""
    out_audio: list[dict] = []
    for idx, s in enumerate(streams.get("audio", [])):
        tags = s.get("tags") or {}
        lang = (tags.get("language") or "und").lower()
        codec = (s.get("codec_name") or "?").lower()
        title = (tags.get("title") or "").strip()
        comm = " ⚠️ (kommentár)" if _is_commentary_stream(s) else ""
        title_part = f" — {title}" if title else ""
        out_audio.append(
            {
                "index": idx,
                "language": lang,
                "codec": codec,
                "is_commentary": _is_commentary_stream(s),
                "label": f"[#{idx}] {lang} ({codec}){title_part}{comm}",
            }
        )

    out_subs: list[dict] = []
    for idx, s in enumerate(streams.get("subtitle", [])):
        tags = s.get("tags") or {}
        lang = (tags.get("language") or "und").lower()
        codec = (s.get("codec_name") or "?").lower()
        title = (tags.get("title") or "").strip()
        is_text = codec in TEXT_SUBTITLE_CODECS
        is_bmp = codec in BITMAP_SUBTITLE_CODECS
        kind = "szöveges SRT" if is_text else ("kép alapú PGS OCR" if is_bmp else codec)
        title_part = f" — {title}" if title else ""
        out_subs.append(
            {
                "index": idx,
                "language": lang,
                "codec": codec,
                "is_text": is_text,
                "is_bitmap": is_bmp,
                "label": f"[#{idx}] {lang} ({kind}){title_part}",
            }
        )
    return {"audio": out_audio, "subtitle": out_subs}


def extract_embedded_subtitle(
    video_path: Path,
    output_srt_path: Path | None = None,
    preferred_languages: tuple[str, ...] | None = None,
    stop_event: threading.Event | None = None,
    stream_index: int | None = None,
) -> Path | None:
    """Megkísérli közvetlenül kinyerni a beágyazott szöveges feliratsávot SRT-be."""
    streams = probe_streams(video_path)
    sub_streams = streams.get("subtitle", [])
    text_subs = [
        (idx, s)
        for idx, s in enumerate(sub_streams)
        if (s.get("codec_name") or "").lower() in TEXT_SUBTITLE_CODECS
    ]
    if not text_subs:
        return None

    if stream_index is not None and any(idx == stream_index for idx, _ in text_subs):
        selected_sub_idx = stream_index
        chosen_stream = next(s for idx, s in text_subs if idx == stream_index)
        selected_lang = ((chosen_stream.get("tags") or {}).get("language") or "und").lower()
    else:
        prefs = preferred_languages or get_preferred_subtitle_languages(app1_config.preferred_subtitle_language)
        selected_sub_idx = text_subs[0][0]
        selected_lang = ((text_subs[0][1].get("tags") or {}).get("language") or "und").lower()
        for pref in prefs:
            for idx, s in text_subs:
                lang = ((s.get("tags") or {}).get("language") or "").lower()
                if lang == pref:
                    selected_sub_idx = idx
                    selected_lang = lang or "und"
                    break
            else:
                continue
            break

    srt_path = output_srt_path or video_path.with_suffix(".srt")
    ffmpeg_exe = resolve_ffmpeg_executable()
    cmd = [
        ffmpeg_exe,
        "-y",
        "-i",
        str(video_path),
        "-map",
        f"0:s:{selected_sub_idx}",
        "-c:s",
        "srt",
        str(srt_path),
    ]
    log.info(
        "Beágyazott szöveges felirat kinyerése (sáv #%d, nyelv: '%s'): '%s' → '%s'",
        selected_sub_idx,
        selected_lang,
        video_path.name,
        srt_path.name,
    )
    res = _run_cancellable(cmd, timeout=300, stop_event=stop_event)
    if res.returncode == 0 and srt_path.exists() and srt_path.stat().st_size > 0:
        log.info("Beágyazott felirat sikeresen kinyerve: '%s'", srt_path.name)
        return srt_path

    stderr_msg = res.stderr.decode("utf-8", errors="replace").strip()
    log.warning("Beágyazott felirat kinyerése sikertelen ('%s'): %s", video_path.name, stderr_msg)
    if srt_path.exists() and srt_path.stat().st_size == 0:
        srt_path.unlink(missing_ok=True)
    return None


def _find_tesseract_exe() -> str | None:
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in (
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def extract_bitmap_subtitle_ocr(
    video_path: Path,
    output_srt_path: Path | None = None,
    preferred_languages: tuple[str, ...] | None = None,
    stop_event: threading.Event | None = None,
) -> Path | None:
    """Blu-ray PGS képfelirat kinyerése és OCR-ezése SRT-be (pgsrip + Tesseract segítségével)."""
    streams = probe_streams(video_path)
    sub_streams = streams.get("subtitle", [])
    pgs_subs = [
        (idx, s)
        for idx, s in enumerate(sub_streams)
        if (s.get("codec_name") or "").lower() in ("hdmv_pgs_subtitle", "pgssub")
    ]
    if not pgs_subs:
        return None

    tesseract_exe = _find_tesseract_exe()
    if not tesseract_exe:
        log.info("ℹ️ PGS képfelirat található, de nincs Tesseract OCR telepítve — Whisper módra váltunk.")
        return None

    try:
        from babelfish import Language
        from pgsrip import Options, Pgs, pgsrip
    except ImportError:
        log.info("ℹ️ PGS OCR csomag (pgsrip) nem elérhető — Whisper módra váltunk.")
        return None

    prefs = preferred_languages or get_preferred_subtitle_languages(app1_config.preferred_subtitle_language)
    selected_idx = pgs_subs[0][0]
    selected_lang = ((pgs_subs[0][1].get("tags") or {}).get("language") or "eng").lower()
    for pref in prefs:
        for idx, s in pgs_subs:
            lang = ((s.get("tags") or {}).get("language") or "").lower()
            if lang == pref:
                selected_idx = idx
                selected_lang = lang or "eng"
                break
        else:
            continue
        break

    srt_path = output_srt_path or video_path.with_suffix(".srt")
    sup_path = srt_path.with_suffix(".tmp_pgs.sup")
    try:
        ffmpeg_exe = resolve_ffmpeg_executable()
        cmd = [
            ffmpeg_exe, "-y", "-i", str(video_path),
            "-map", f"0:s:{selected_idx}", "-c:s", "copy", str(sup_path),
        ]
        log.info("🔍 Blu-ray PGS képfelirat kinyerése OCR-hez (sáv #%d, nyelv: '%s')...", selected_idx, selected_lang)
        res = _run_cancellable(cmd, timeout=300, stop_event=stop_event)
        if res.returncode != 0 or not sup_path.exists() or sup_path.stat().st_size == 0:
            return None

        lang_obj = Language.fromalpha3b(selected_lang) if len(selected_lang) == 3 else Language("eng")
        pgs_media = Pgs(str(sup_path), lang_obj)
        pgsrip.rip(pgs_media, Options(languages={lang_obj}, overwrite=True))
        generated_srt = sup_path.with_suffix(".srt")
        if generated_srt.exists() and generated_srt.stat().st_size > 0:
            generated_srt.replace(srt_path)
            log.info("✅ PGS képfelirat OCR sikeres: '%s'", srt_path.name)
            return srt_path
    except Exception as exc:
        log.warning("⚠️ PGS OCR sikertelen ('%s'): %s — Whisper módra váltunk.", video_path.name, exc)
    finally:
        sup_path.unlink(missing_ok=True)
        sup_path.with_suffix(".srt").unlink(missing_ok=True)
    return None


def _build_ffmpeg_command(
    video_path: Path,
    audio_path: Path,
    audio_stream_index: int = 0,
) -> list[str]:
    """Összeállítja az FFmpeg parancsot az audio extrakciójához."""
    ffmpeg_exe = resolve_ffmpeg_executable()
    audio_format = app1_config.audio_format.lower()
    sample_rate = str(app1_config.audio_sample_rate)

    if audio_format == "mp3":
        codec_args = ["-acodec", "libmp3lame", "-b:a", "128k"]
    else:
        codec_args = ["-acodec", "pcm_s16le"]

    return [
        ffmpeg_exe,
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-map",
        f"0:a:{audio_stream_index}",
        *codec_args,
        "-ar",
        sample_rate,
        "-ac",
        "1",
        str(audio_path),
    ]


def _is_commentary_stream(stream: dict) -> bool:
    """Felismeri a kommentár vagy leíró jellegű mellék-hangsávokat."""
    title = ((stream.get("tags") or {}).get("title") or "").lower()
    disp = stream.get("disposition") or {}
    return bool(disp.get("comment")) or any(
        kw in title for kw in ("commentary", "kommentár", "director", "descriptive", "isolated")
    )


def select_audio_stream_index(
    audio_streams: list[dict],
    preferred_language: str | None = None,
    default_index: int = 0,
) -> int:
    """Kiválasztja a legjobb hangsáv-indexet nyelv alapján, elkerülve a kommentárt."""
    if not audio_streams:
        return 0

    pref = (preferred_language if preferred_language is not None else app1_config.preferred_audio_language) or "auto"
    pref_clean = pref.strip().lower()

    # Ha a felhasználó konkrét (>0) indexet kért és auto nyelven van, tartsuk tiszteletben
    if default_index > 0 and pref_clean == "auto" and default_index < len(audio_streams):
        return default_index

    prefs = get_preferred_audio_languages(pref_clean)
    non_commentary = [(idx, s) for idx, s in enumerate(audio_streams) if not _is_commentary_stream(s)]
    candidates = non_commentary or list(enumerate(audio_streams))

    for target_lang in prefs:
        for idx, stream in candidates:
            lang = ((stream.get("tags") or {}).get("language") or "").lower()
            if lang == target_lang:
                return idx

    if default_index < len(audio_streams) and not _is_commentary_stream(audio_streams[default_index]):
        return default_index
    return candidates[0][0]


def extract_audio(
    video_path: Path,
    output_dir: Path,
    stop_event: threading.Event | None = None,
    stream_index: int | None = None,
) -> Path:
    """Kinyeri az audio sávot a megadott videofájlból FFmpeg segítségével."""
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_format = app1_config.audio_format.lower()
    audio_filename = f"{video_path.stem}.{audio_format}"
    audio_path = output_dir / audio_filename

    streams = probe_streams(video_path)
    audio_streams = streams.get("audio", [])
    if stream_index is not None and 0 <= stream_index < max(1, len(audio_streams)):
        stream_idx = stream_index
    else:
        stream_idx = select_audio_stream_index(
            audio_streams,
            preferred_language=app1_config.preferred_audio_language,
            default_index=app1_config.audio_stream_index,
        )
    if audio_streams and stream_idx < len(audio_streams):
        chosen_lang = ((audio_streams[stream_idx].get("tags") or {}).get("language") or "und").lower()
        log.info("Kiválasztott hangsáv: #%d (nyelv: '%s')", stream_idx, chosen_lang)

    cmd = _build_ffmpeg_command(video_path, audio_path, audio_stream_index=stream_idx)
    log.info("Audio extrakció indul: '%s' → '%s'", video_path.name, audio_path.name)
    log.debug("FFmpeg parancs: %s", " ".join(cmd))

    result = _run_cancellable(cmd, timeout=3600, stop_event=stop_event)
    if result.returncode != 0:
        stderr_msg = result.stderr.decode("utf-8", errors="replace").strip()
        raise AudioExtractionError(
            f"FFmpeg hiba (kód {result.returncode}) '{video_path.name}' feldolgozásakor:\n{stderr_msg}"
        )

    size_mb = audio_path.stat().st_size / (1024 * 1024)
    log.info("Audio sikeresen kinyerve: '%s' (%.1f MB)", audio_path.name, size_mb)
    return audio_path


def cleanup_audio(audio_path: Path) -> None:
    """Törli az ideiglenes audiofájlt a feldolgozás után."""
    try:
        audio_path.unlink(missing_ok=True)
        log.debug("Ideiglenes audiofájl törölve: '%s'", audio_path.name)
    except OSError as exc:
        log.warning("Nem sikerült törölni '%s': %s", audio_path.name, exc)
