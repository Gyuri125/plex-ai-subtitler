"""Videó–felirat szinkron-ellenőrző modul (MKV/MP4 időtartam, paritás és hangenergia-igazítás)."""

from __future__ import annotations

import array
import json
import math
import re
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path

from plex_subtitler.app1_extractor.audio_extractor import (
    resolve_ffmpeg_executable,
    resolve_ffprobe_executable,
)
from plex_subtitler.app2_translator.srt_formatter import parse_srt_timestamp_ms, shift_srt_file
from plex_subtitler.app2_translator.srt_parser import SrtBlock, SrtParser
from plex_subtitler.config import app1_config
from plex_subtitler.logger import get_logger

log = get_logger(__name__, level=app1_config.log_level)

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2})\.(\d{2})")


@dataclass
class SyncReport:
    """A videó és az SRT felirat szinkron-ellenőrzésének részletes eredménye."""

    in_sync: bool
    video_duration_ms: int
    subtitle_count: int
    first_cue_ms: int
    last_cue_end_ms: int
    out_of_bounds_count: int
    overlap_count: int
    negative_or_zero_duration_count: int
    speech_alignment_ratio: float | None = None
    source_max_drift_ms: int | None = None
    issues: list[str] = field(default_factory=list)


def get_media_duration_ms(video_path: Path) -> int:
    """Lekérdezi a videofájl teljes hosszát ezredmásodpercben (ffprobe / ffmpeg -i)."""
    ffprobe_exe = resolve_ffprobe_executable()
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if ffprobe_exe:
        try:
            res = subprocess.run(
                [
                    ffprobe_exe,
                    "-v",
                    "quiet",
                    "-print_format",
                    "json",
                    "-show_format",
                    str(video_path),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=creation_flags,
            )
            if res.returncode == 0:
                fmt = json.loads(res.stdout).get("format", {})
                if "duration" in fmt:
                    return int(round(float(fmt["duration"]) * 1000))
        except Exception:
            pass

    ffmpeg_exe = resolve_ffmpeg_executable()
    res = subprocess.run(
        [ffmpeg_exe, "-hide_banner", "-i", str(video_path)],
        capture_output=True,
        timeout=20,
        creationflags=creation_flags,
    )
    stderr_text = res.stderr.decode("utf-8", errors="replace")
    match = _DURATION_RE.search(stderr_text)
    if not match:
        return 0
    h, m, s, cs = (int(g) for g in match.groups())
    return (((h * 3600) + (m * 60) + s) * 1000) + (cs * 10)


def _extract_pcm_samples(video_path: Path, sample_rate: int = 8000) -> array.array | None:
    """Alacsony mintavételű monó PCM hangminták kinyerése FFmpeg-gel a szinkron-analízishez."""
    ffmpeg_exe = resolve_ffmpeg_executable()
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory() as tmp_dir:
        wav_path = Path(tmp_dir) / "sync_probe.wav"
        res = subprocess.run(
            [
                ffmpeg_exe,
                "-y",
                "-i",
                str(video_path),
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(sample_rate),
                "-ac",
                "1",
                str(wav_path),
            ],
            capture_output=True,
            timeout=120,
            creationflags=creation_flags,
        )
        if res.returncode != 0 or not wav_path.exists():
            return None

        with wave.open(str(wav_path), "rb") as wf:
            raw_bytes = wf.readframes(wf.getnframes())

        samples = array.array("h")
        samples.frombytes(raw_bytes)
        return samples if len(samples) > 0 else None


def _score_offset_alignment(
    samples: array.array,
    cue_windows_ms: list[tuple[int, int]],
    offset_ms: int,
    silence_threshold_rms: float,
    sample_rate: int,
) -> tuple[float | None, float]:
    """Kiszámolja adott offset_ms mellett az aktív hang-arányt és az átlagos RMS energiát."""
    try:
        import numpy as np

        np_samples = np.frombuffer(samples, dtype=np.int16)
    except Exception:
        np = None
        np_samples = None

    total_samples = len(samples)
    active_cues = 0
    checked_cues = 0
    rms_sum = 0.0
    inv_scale_sq = 1.0 / (32768.0 * 32768.0)

    for start_ms, end_ms in cue_windows_ms:
        shifted_start = start_ms + offset_ms
        shifted_end = end_ms + offset_ms
        if shifted_end <= 0:
            continue
        s_idx = max(0, int((max(0, shifted_start) / 1000.0) * sample_rate))
        e_idx = min(total_samples, int((shifted_end / 1000.0) * sample_rate))
        if e_idx <= s_idx:
            continue
        checked_cues += 1
        if np is not None and np_samples is not None:
            seg = np_samples[s_idx:e_idx].astype(np.float64)
            rms = math.sqrt(float(np.mean(seg * seg)) * inv_scale_sq)
        else:
            window = samples[s_idx:e_idx]
            mean_sq = sum((x / 32768.0) ** 2 for x in window) / len(window)
            rms = math.sqrt(mean_sq)
        rms_sum += rms
        if rms >= silence_threshold_rms:
            active_cues += 1

    if checked_cues == 0:
        return None, 0.0
    return active_cues / checked_cues, rms_sum / checked_cues


def measure_audio_cue_alignment(
    video_path: Path,
    blocks: list[SrtBlock],
    silence_threshold_rms: float = 0.002,
    sample_rate: int = 8000,
) -> float | None:
    """Ellenőrzi, hogy a feliratablakok időpontjaiban van-e valódi hangenergia a videóban."""
    if not blocks:
        return None

    cue_windows = [
        times
        for b in blocks
        if (times := parse_srt_timestamp_ms(b.timestamp)) is not None
    ]
    if not cue_windows:
        return None

    samples = _extract_pcm_samples(video_path, sample_rate=sample_rate)
    if samples is None:
        return None

    ratio, _ = _score_offset_alignment(
        samples,
        cue_windows,
        offset_ms=0,
        silence_threshold_rms=silence_threshold_rms,
        sample_rate=sample_rate,
    )
    return ratio


def estimate_subtitle_offset_ms(
    video_path: Path,
    blocks: list[SrtBlock],
    max_shift_ms: int = 3000,
    step_ms: int = 250,
    silence_threshold_rms: float = 0.002,
    sample_rate: int = 8000,
) -> tuple[int, float, float]:
    """Megbecsüli az optimális felirat-eltolást (ms), a 0 ms-os és a legjobb egyezési arányt."""
    cue_windows = [
        times
        for b in blocks
        if (times := parse_srt_timestamp_ms(b.timestamp)) is not None
    ]
    if not cue_windows:
        return 0, 0.0, 0.0

    samples = _extract_pcm_samples(video_path, sample_rate=sample_rate)
    if samples is None:
        return 0, 0.0, 0.0

    # Hosszú filmnél max 120 egyenletesen elosztott blokkon vizsgálunk a villámgyors futásért
    if len(cue_windows) > 120:
        stride = len(cue_windows) / 120.0
        sampled_cues = [cue_windows[int(i * stride)] for i in range(120)]
    else:
        sampled_cues = cue_windows

    base_ratio_opt, base_rms = _score_offset_alignment(
        samples, sampled_cues, 0, silence_threshold_rms, sample_rate
    )
    base_ratio = base_ratio_opt if base_ratio_opt is not None else 0.0

    best_offset = 0
    best_ratio = base_ratio
    best_rms = base_rms

    for candidate in range(-max_shift_ms, max_shift_ms + 1, step_ms):
        if candidate == 0:
            continue
        ratio_opt, mean_rms = _score_offset_alignment(
            samples, sampled_cues, candidate, silence_threshold_rms, sample_rate
        )
        if ratio_opt is None:
            continue
        # Jelentősen jobb arány vagy azonos legjobb arány mellett tisztább beszédenergia és kisebb eltolás nyer
        if ratio_opt > best_ratio + 0.01 or (
            abs(ratio_opt - best_ratio) <= 0.01
            and (mean_rms > best_rms + 0.005 or (abs(mean_rms - best_rms) <= 0.005 and abs(candidate) < abs(best_offset)))
        ):
            best_offset = candidate
            best_ratio = ratio_opt
            best_rms = mean_rms

    return best_offset, base_ratio, best_ratio


def auto_align_subtitle(
    video_path: Path,
    srt_path: Path,
    output_path: Path | None = None,
    max_shift_ms: int = 3000,
    step_ms: int = 250,
    min_improvement: float = 0.15,
) -> tuple[int, Path, SyncReport]:
    """Automatikusan korrigálja a felirat egyenletes elcsúszását a videó hangsávja alapján."""
    video_path = Path(video_path)
    srt_path = Path(srt_path)
    blocks = SrtParser(srt_path).blocks
    target = Path(output_path) if output_path else srt_path

    if not blocks:
        report = verify_subtitle_sync(video_path, srt_path)
        return 0, target, report

    best_offset, base_ratio, best_ratio = estimate_subtitle_offset_ms(
        video_path,
        blocks,
        max_shift_ms=max_shift_ms,
        step_ms=step_ms,
    )

    applied_offset = 0
    if best_offset != 0 and (best_ratio - base_ratio) >= min_improvement:
        target = shift_srt_file(srt_path, best_offset, output_path=target)
        applied_offset = best_offset
        log.info(
            "🔧 Auto-szinkron korrekció: %+d ms alkalmazva '%s' fájlra (hang-egyezés: %.0f%% → %.0f%%).",
            applied_offset,
            target.name,
            base_ratio * 100,
            best_ratio * 100,
        )
    else:
        log.info(
            "✅ Auto-szinkron ellenőrzés: nem volt szükség eltolásra '%s' fájlnál (egyezés: %.0f%%).",
            srt_path.name,
            base_ratio * 100,
        )

    final_report = verify_subtitle_sync(video_path, target)
    return applied_offset, target, final_report


def verify_subtitle_sync(
    video_path: Path,
    srt_path: Path,
    source_srt_path: Path | None = None,
    tolerance_ms: int = 1500,
    check_audio_alignment: bool = True,
    min_audio_alignment_ratio: float = 0.5,
) -> SyncReport:
    """Átfogó szinkron-ellenőrzés videó és SRT felirat között."""
    video_path = Path(video_path)
    srt_path = Path(srt_path)

    issues: list[str] = []
    video_duration_ms = get_media_duration_ms(video_path)
    blocks = SrtParser(srt_path).blocks

    if not blocks:
        return SyncReport(
            in_sync=False,
            video_duration_ms=video_duration_ms,
            subtitle_count=0,
            first_cue_ms=0,
            last_cue_end_ms=0,
            out_of_bounds_count=0,
            overlap_count=0,
            negative_or_zero_duration_count=0,
            issues=["Az SRT fájl üres vagy nem tartalmaz érvényes blokkot."],
        )

    out_of_bounds = 0
    overlaps = 0
    bad_duration = 0
    first_cue_ms = -1
    last_cue_end_ms = 0
    prev_end = -1

    for block in blocks:
        times = parse_srt_timestamp_ms(block.timestamp)
        if times is None:
            bad_duration += 1
            issues.append(f"Érvénytelen időbélyeg a(z) {block.index}. blokknál.")
            continue
        start_ms, end_ms = times
        if first_cue_ms == -1:
            first_cue_ms = start_ms
        last_cue_end_ms = max(last_cue_end_ms, end_ms)

        if end_ms <= start_ms:
            bad_duration += 1
            issues.append(f"Hibás hossz (<=0 mp) a(z) {block.index}. blokknál.")

        if prev_end > start_ms:
            overlaps += 1
            issues.append(f"Időbélyeg-átfedés a(z) {block.index}. blokknál ({prev_end} > {start_ms} ms).")

        if start_ms < 0 or (video_duration_ms > 0 and end_ms > video_duration_ms + tolerance_ms):
            out_of_bounds += 1
            issues.append(
                f"A(z) {block.index}. blokk túllóg a videó hosszán ({end_ms} ms > {video_duration_ms} ms)."
            )

        prev_end = end_ms

    source_max_drift_ms: int | None = None
    if source_srt_path and Path(source_srt_path).exists():
        src_blocks = {b.index: b for b in SrtParser(Path(source_srt_path)).blocks}
        max_drift = 0
        for b in blocks:
            if b.index in src_blocks:
                t_tr = parse_srt_timestamp_ms(b.timestamp)
                t_src = parse_srt_timestamp_ms(src_blocks[b.index].timestamp)
                if t_tr and t_src:
                    drift = max(abs(t_tr[0] - t_src[0]), abs(t_tr[1] - t_src[1]))
                    max_drift = max(max_drift, drift)
        source_max_drift_ms = max_drift
        if max_drift > tolerance_ms:
            issues.append(f"Eltérés az eredeti és fordított SRT időbélyegek között: {max_drift} ms.")

    alignment_ratio: float | None = None
    if check_audio_alignment:
        alignment_ratio = measure_audio_cue_alignment(video_path, blocks)
        if alignment_ratio is not None and alignment_ratio < min_audio_alignment_ratio:
            issues.append(
                f"Alacsony hang–felirat egyezés ({alignment_ratio:.0%} < {min_audio_alignment_ratio:.0%}) — lehetséges elcsúszás!"
            )

    in_sync = (
        out_of_bounds == 0
        and overlaps == 0
        and bad_duration == 0
        and (source_max_drift_ms is None or source_max_drift_ms <= tolerance_ms)
        and (alignment_ratio is None or alignment_ratio >= min_audio_alignment_ratio)
    )

    return SyncReport(
        in_sync=in_sync,
        video_duration_ms=video_duration_ms,
        subtitle_count=len(blocks),
        first_cue_ms=max(0, first_cue_ms),
        last_cue_end_ms=last_cue_end_ms,
        out_of_bounds_count=out_of_bounds,
        overlap_count=overlaps,
        negative_or_zero_duration_count=bad_duration,
        speech_alignment_ratio=alignment_ratio,
        source_max_drift_ms=source_max_drift_ms,
        issues=issues,
    )


@dataclass
class QualityReport:
    """Lefordított felirat minőség-ellenőrző gyorsjelző eredménye."""

    total_blocks: int
    untranslated_count: int
    avg_line_chars: float
    max_line_chars: int
    over_limit_lines: int
    speech_alignment_ratio: float | None
    score_pct: int
    summary_line: str
    untranslated_indices: list[int] = field(default_factory=list)


def find_untranslated_block_indices(
    source_blocks: list[SrtBlock],
    translated_blocks: list[SrtBlock],
    glossary: dict[str, str] | None = None,
) -> list[int]:
    """Visszaadja azon blokkok indexeit, amelyek valószínűleg az eredeti nyelven maradtak."""
    src_by_idx = {b.index: b.text for b in source_blocks}
    same_glossary = {
        k.strip().lower()
        for k, v in (glossary or {}).items()
        if k.strip().lower() == v.strip().lower()
    }
    flagged: list[int] = []
    for blk in translated_blocks:
        tr_clean = " ".join(blk.text.split())
        src_clean = " ".join(src_by_idx.get(blk.index, "").split())
        if (
            src_clean
            and len(tr_clean) >= 6
            and any(ch.isalpha() for ch in tr_clean)
            and tr_clean.lower() == src_clean.lower()
            and tr_clean.lower() not in same_glossary
        ):
            flagged.append(blk.index)
    return flagged


def evaluate_subtitle_quality(
    source_blocks: list[SrtBlock],
    translated_blocks: list[SrtBlock],
    max_chars_per_line: int = 42,
    speech_alignment_ratio: float | None = None,
    glossary: dict[str, str] | None = None,
) -> QualityReport:
    """Összegzi a kész fordítás nyelvi teljességét, sorhosszát és TV-kompatibilitását."""
    if not translated_blocks:
        return QualityReport(
            total_blocks=0,
            untranslated_count=0,
            avg_line_chars=0.0,
            max_line_chars=0,
            over_limit_lines=0,
            speech_alignment_ratio=speech_alignment_ratio,
            score_pct=0,
            summary_line="📊 Minőség: 0% (üres felirat)",
            untranslated_indices=[],
        )

    untranslated_ids = find_untranslated_block_indices(
        source_blocks, translated_blocks, glossary=glossary
    )
    untranslated = len(untranslated_ids)
    line_lengths: list[int] = []
    over_limit = 0

    for blk in translated_blocks:
        for raw_line in blk.text.splitlines():
            line_len = len(raw_line.strip())
            line_lengths.append(line_len)
            if line_len > max_chars_per_line:
                over_limit += 1

    total = len(translated_blocks)
    avg_len = (sum(line_lengths) / len(line_lengths)) if line_lengths else 0.0
    max_len = max(line_lengths) if line_lengths else 0
    total_lines = max(1, len(line_lengths))

    untr_penalty = (untranslated / total) * 60.0
    over_penalty = (over_limit / total_lines) * 25.0
    sync_penalty = 0.0
    if speech_alignment_ratio is not None and speech_alignment_ratio < 0.7:
        sync_penalty = (0.7 - speech_alignment_ratio) * 25.0

    score = max(0, min(100, round(100.0 - untr_penalty - over_penalty - sync_penalty)))
    sync_part = f" | Szinkron: {speech_alignment_ratio:.0%}" if speech_alignment_ratio is not None else ""
    summary = (
        f"📊 Minőség: {score}% | Lefordítatlan gyanús: {untranslated}/{total} | "
        f"Átlag sorhossz: {avg_len:.1f} kar. | Max: {max_len} kar. "
        f"(>{max_chars_per_line}: {over_limit} sor){sync_part}"
    )
    return QualityReport(
        total_blocks=total,
        untranslated_count=untranslated,
        avg_line_chars=avg_len,
        max_line_chars=max_len,
        over_limit_lines=over_limit,
        speech_alignment_ratio=speech_alignment_ratio,
        score_pct=score,
        summary_line=summary,
        untranslated_indices=untranslated_ids,
    )
