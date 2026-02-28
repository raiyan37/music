"""
Local audio processing using pydub (backed by ffmpeg).

Pipeline
--------
inject_ad()
  1. Load song and ad audio
  2. Optionally apply stem-separation results (duck instrumental, fade vocals)
  3. Split the song at the insertion point
  4. Apply volume ducking on the tail of part_before and head of part_after
  5. Crossfade the three segments: part_before -> ad -> part_after
  6. Export final audio as MP3

All heavy work runs in a thread-pool executor so it does not block the event loop.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from pydub import AudioSegment
from pydub.effects import normalize

logger = logging.getLogger(__name__)

# One shared executor for blocking pydub calls
_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load(path: Path) -> AudioSegment:
    """Load an audio file; pydub auto-detects format from the extension."""
    return AudioSegment.from_file(str(path))


def _to_mono_stereo(seg: AudioSegment, channels: int = 2) -> AudioSegment:
    return seg.set_channels(channels)


def _match_sample_rate(reference: AudioSegment, target: AudioSegment) -> AudioSegment:
    """Resample *target* to match *reference* sample rate if they differ."""
    if target.frame_rate != reference.frame_rate:
        target = target.set_frame_rate(reference.frame_rate)
    return target


def _duck_tail(seg: AudioSegment, duration_ms: int, target_db: float) -> AudioSegment:
    """
    Gradually reduce the volume over the last *duration_ms* ms of *seg*.

    Applies a linear fade from 0 dB to *target_db* (negative value).
    """
    if duration_ms <= 0 or target_db >= 0:
        return seg

    fade_len = min(duration_ms, len(seg))
    body = seg[:-fade_len]
    tail = seg[-fade_len:]

    steps = 20
    step_ms = fade_len // steps
    if step_ms == 0:
        return seg

    ducked_tail = AudioSegment.empty()
    for i in range(steps):
        chunk = tail[i * step_ms : (i + 1) * step_ms]
        gain = target_db * (i + 1) / steps
        ducked_tail += chunk.apply_gain(gain)

    remainder = tail[steps * step_ms :]
    ducked_tail = ducked_tail + remainder.apply_gain(target_db)
    return body + ducked_tail


def _unduck_head(seg: AudioSegment, duration_ms: int, from_db: float) -> AudioSegment:
    """
    Gradually restore the volume over the first *duration_ms* ms of *seg*.

    Ramps from *from_db* back to 0 dB.
    """
    if duration_ms <= 0 or from_db >= 0:
        return seg

    fade_len = min(duration_ms, len(seg))
    head = seg[:fade_len]
    body = seg[fade_len:]

    steps = 20
    step_ms = fade_len // steps
    if step_ms == 0:
        return seg

    unducked_head = AudioSegment.empty()
    for i in range(steps):
        chunk = head[i * step_ms : (i + 1) * step_ms]
        gain = from_db * (1 - (i + 1) / steps)
        unducked_head += chunk.apply_gain(gain)

    remainder = head[steps * step_ms :]
    unducked_head = unducked_head + remainder
    return unducked_head + body


def _crossfade_join(
    first: AudioSegment,
    second: AudioSegment,
    crossfade_ms: int,
) -> AudioSegment:
    """
    Join two segments with a crossfade.

    pydub's append(..., crossfade=N) overlaps the last N ms of the first
    segment with the first N ms of the second using equal-power crossfade.
    """
    if crossfade_ms <= 0:
        return first + second

    # Clamp crossfade to the shorter segment length
    cf = min(crossfade_ms, len(first), len(second))
    return first.append(second, crossfade=cf)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def inject_ad_sync(
    song_path: Path,
    ad_path: Path,
    output_path: Path,
    insert_at_seconds: float,
    crossfade_ms: int = 500,
    duck_volume_db: float = -8.0,
    duck_duration_ms: int = 2000,
    instrumental_path: Optional[Path] = None,
) -> float:
    """
    Splice *ad_path* into *song_path* at *insert_at_seconds* and save to *output_path*.

    Parameters
    ----------
    song_path : Path
        Original song file.
    ad_path : Path
        Ad audio file to inject.
    output_path : Path
        Destination for the resulting mixed audio.
    insert_at_seconds : float
        Position (in seconds) in the song where the ad starts.
    crossfade_ms : int
        Length of crossfade applied at each splice point.
    duck_volume_db : float
        How many dB to lower the song during the ad (0 = no ducking).
        Must be ≤ 0.
    duck_duration_ms : int
        How many milliseconds before/after the cut to ramp the volume.
    instrumental_path : Path, optional
        If provided (from stem separation), use the separated instrumental
        track for the duck region instead of the full song mix. This creates
        a smoother transition because vocals are already faded out by Suno.

    Returns
    -------
    float
        Duration of the output audio in seconds.
    """
    logger.info(
        "inject_ad_sync: song=%s ad=%s insert_at=%.2fs crossfade=%dms duck=%.1fdB",
        song_path.name,
        ad_path.name,
        insert_at_seconds,
        crossfade_ms,
        duck_volume_db,
    )

    song = _load(song_path)
    ad = _load(ad_path)

    song = _to_mono_stereo(song)
    ad = _to_mono_stereo(ad)
    ad = _match_sample_rate(song, ad)

    insert_ms = int(insert_at_seconds * 1000)
    insert_ms = max(0, min(insert_ms, len(song)))

    part_before = song[:insert_ms]
    part_after = song[insert_ms:]

    # If we have an isolated instrumental track, use it to replace the
    # transition region so the vocal fade is handled by Suno's separation.
    if instrumental_path is not None and instrumental_path.exists():
        inst = _load(instrumental_path)
        inst = _to_mono_stereo(inst)
        inst = _match_sample_rate(song, inst)

        # Splice the instrumental into the duck region of part_before and part_after
        duck_start = max(0, insert_ms - duck_duration_ms)
        inst_tail = inst[duck_start:insert_ms]
        inst_head = inst[insert_ms : insert_ms + duck_duration_ms]

        part_before = part_before[:duck_start] + inst_tail
        part_after = inst_head + part_after[duck_duration_ms:]
        logger.info("Using instrumental track for duck region")

    # Apply volume ducking
    if duck_volume_db < 0:
        part_before = _duck_tail(part_before, duck_duration_ms, duck_volume_db)
        part_after = _unduck_head(part_after, duck_duration_ms, duck_volume_db)

    # Crossfade-join: part_before -> ad -> part_after
    combined = _crossfade_join(part_before, ad, crossfade_ms)
    combined = _crossfade_join(combined, part_after, crossfade_ms)

    # Normalize to avoid clipping
    combined = normalize(combined)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    combined.export(str(output_path), format="mp3", bitrate="192k")

    duration = len(combined) / 1000.0
    logger.info("Output saved to %s (%.1fs)", output_path, duration)
    return duration


async def inject_ad(
    song_path: Path,
    ad_path: Path,
    output_path: Path,
    insert_at_seconds: float,
    crossfade_ms: int = 500,
    duck_volume_db: float = -8.0,
    duck_duration_ms: int = 2000,
    instrumental_path: Optional[Path] = None,
) -> float:
    """
    Async wrapper around inject_ad_sync — runs in a thread-pool executor.

    Returns the duration of the output audio in seconds.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor,
        inject_ad_sync,
        song_path,
        ad_path,
        output_path,
        insert_at_seconds,
        crossfade_ms,
        duck_volume_db,
        duck_duration_ms,
        instrumental_path,
    )


def get_duration(path: Path) -> float:
    """Return audio duration in seconds without loading the entire file."""
    seg = AudioSegment.from_file(str(path))
    return len(seg) / 1000.0
