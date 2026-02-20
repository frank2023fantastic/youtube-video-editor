"""
YouTube Multi-Language Dubbing Pipeline

Orchestrates: Download -> Separate -> Transcribe -> Translate -> TTS -> Mix
"""

import sys
import asyncio
import os
import glob
import subprocess
import time
import traceback
from pathlib import Path

import edge_tts
import ffmpeg
from faster_whisper import WhisperModel
from googletrans import Translator
from pydub import AudioSegment

from utils import get_job_dir, get_language_voice, get_language_code

# ---------------------------------------------------------------------------
# Shared job state (in-memory; for production use Redis / DB)
# ---------------------------------------------------------------------------
jobs: dict = {}


def update_job(job_id: str, **kwargs):
    """Update a job's state dict with the given key-value pairs."""
    if job_id not in jobs:
        jobs[job_id] = {}
    jobs[job_id].update(kwargs)


# ---------------------------------------------------------------------------
# Step 1 – Download via Publer.com (Playwright headless scraper)
# ---------------------------------------------------------------------------
SCRAPER_SCRIPT = Path(__file__).parent / "_publer_scraper.py"


async def download_video(job_id: str, url: str) -> dict:
    """Download video from YouTube by scraping Publer.com with headless Chromium.

    Runs _publer_scraper.py as a separate process to avoid Windows asyncio
    subprocess conflicts with uvicorn's event loop.
    """
    import json

    update_job(job_id, step="downloading", progress=5, message="Launching Publer scraper...")

    job_dir = get_job_dir(job_id)
    video_file = job_dir / "source.mp4"
    audio_file = job_dir / "source_audio.wav"

    update_job(job_id, progress=10, message="Scraping Publer.com for download link (up to 90s)...")

    # Run the scraper as a completely separate Python process
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(SCRAPER_SCRIPT), url, str(video_file)],
            capture_output=True,
            text=True,
            timeout=180,  # 3 min total timeout
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Publer scraper timed out after 3 minutes")
    except Exception as e:
        raise RuntimeError(f"Failed to launch scraper: {e}")

    # Parse the JSON output from the scraper
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode != 0:
        # Try to get error from JSON output
        try:
            data = json.loads(stdout)
            error_msg = data.get("error", "Unknown error")
        except (json.JSONDecodeError, ValueError):
            error_msg = stderr or stdout or "Scraper exited with no output"
        raise RuntimeError(f"Publer download failed: {error_msg}")

    # Verify the file exists
    if not video_file.exists() or video_file.stat().st_size < 1024:
        raise RuntimeError("Downloaded video file is empty or missing")

    update_job(job_id, progress=20, message="Video downloaded, extracting audio...")

    # ── Extract audio track ──────────────────────────────────────────────
    update_job(job_id, progress=25, message="Extracting audio track...")
    ffmpeg.input(str(video_file)).output(
        str(audio_file), ac=1, ar=16000, format="wav"
    ).overwrite_output().run(quiet=True)

    update_job(job_id, progress=30, message="Download complete")
    return {"video": str(video_file), "audio": str(audio_file)}




# ---------------------------------------------------------------------------
# Step 2 – Audio Separation (Demucs)
# ---------------------------------------------------------------------------
def separate_audio(job_id: str, audio_path: str) -> dict:
    """Separate vocals from background using Demucs."""
    update_job(job_id, step="separating", progress=20, message="Separating vocals from background audio...")

    job_dir = get_job_dir(job_id)

    cmd = [
        sys.executable, "-m", "demucs",
        "--two-stems", "vocals",
        "-o", str(job_dir / "separated"),
        "--mp3",  # lighter output
        audio_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"Demucs failed: {result.stderr[:500]}")

    # Demucs outputs to separated/htdemucs/source_audio/
    sep_dir = job_dir / "separated" / "htdemucs" / "source_audio"
    if not sep_dir.exists():
        # Try alternative model names
        for model_dir in (job_dir / "separated").rglob("source_audio"):
            sep_dir = model_dir
            break

    vocals_file = None
    bg_file = None
    for f in sep_dir.iterdir():
        if "vocals" in f.name:
            vocals_file = str(f)
        elif "no_vocals" in f.name:
            bg_file = str(f)

    if not vocals_file or not bg_file:
        raise RuntimeError("Demucs separation output not found")

    update_job(job_id, progress=35, message="Audio separation complete")
    return {"vocals": vocals_file, "background": bg_file}


# ---------------------------------------------------------------------------
# Step 3 – Transcription (Faster-Whisper)
# ---------------------------------------------------------------------------
def transcribe_audio(job_id: str, vocals_path: str) -> list[dict]:
    """Transcribe vocals using faster-whisper with timestamps."""
    update_job(job_id, step="transcribing", progress=40, message="Transcribing speech...")

    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments_iter, info = model.transcribe(vocals_path, beam_size=5)

    segments = []
    for seg in segments_iter:
        segments.append({
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
        })

    if not segments:
        raise RuntimeError("No speech detected in the audio")

    update_job(job_id, progress=55, message=f"Transcribed {len(segments)} segments")
    return segments


# ---------------------------------------------------------------------------
# Step 4 – Translation
# ---------------------------------------------------------------------------
def translate_segments(job_id: str, segments: list[dict], target_lang: str) -> list[dict]:
    """Translate each segment to the target language."""
    update_job(job_id, step="translating", progress=60, message="Translating text...")

    translator = Translator()
    lang_code = get_language_code(target_lang)

    translated = []
    for i, seg in enumerate(segments):
        if not seg["text"]:
            translated.append({**seg, "translated": ""})
            continue
        try:
            result = translator.translate(seg["text"], dest=lang_code)
            translated.append({**seg, "translated": result.text})
        except Exception:
            # Fallback: keep original text
            translated.append({**seg, "translated": seg["text"]})

        # Update progress incrementally
        pct = 60 + int(15 * (i + 1) / len(segments))
        update_job(job_id, progress=pct, message=f"Translated {i+1}/{len(segments)} segments")

    return translated


# ---------------------------------------------------------------------------
# Step 5 – TTS Synthesis (Edge-TTS)
# ---------------------------------------------------------------------------
async def synthesize_tts(job_id: str, segments: list[dict], target_lang: str) -> str:
    """Generate TTS audio for each translated segment, then concatenate."""
    update_job(job_id, step="synthesizing", progress=75, message="Generating dubbed audio...")

    job_dir = get_job_dir(job_id)
    tts_dir = job_dir / "tts_segments"
    tts_dir.mkdir(exist_ok=True)

    voice = get_language_voice(target_lang)

    # Generate TTS for each segment
    tts_files = []
    for i, seg in enumerate(segments):
        text = seg.get("translated", seg["text"])
        if not text:
            continue

        out_path = tts_dir / f"seg_{i:04d}.mp3"
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(out_path))
        tts_files.append({
            "file": str(out_path),
            "start": seg["start"],
            "end": seg["end"],
        })

    if not tts_files:
        raise RuntimeError("No TTS segments generated")

    # Build a full-length TTS track aligned to original timestamps
    # Load each clip and place it at the correct timestamp
    max_end = max(seg["end"] for seg in segments)
    full_track = AudioSegment.silent(duration=int(max_end * 1000) + 5000)

    for tts in tts_files:
        try:
            clip = AudioSegment.from_file(tts["file"])
            position_ms = int(tts["start"] * 1000)
            # Truncate clip if it's longer than original segment duration
            seg_duration_ms = int((tts["end"] - tts["start"]) * 1000)
            if len(clip) > seg_duration_ms * 1.5:
                clip = clip.speedup(playback_speed=len(clip) / max(seg_duration_ms, 1))
            full_track = full_track.overlay(clip, position=position_ms)
        except Exception:
            continue

    combined_path = str(job_dir / "tts_combined.wav")
    full_track.export(combined_path, format="wav")

    update_job(job_id, progress=85, message="TTS synthesis complete")
    return combined_path


# ---------------------------------------------------------------------------
# Step 6 – Mix & Merge (FFmpeg)
# ---------------------------------------------------------------------------
def mix_audio_video(job_id: str, video_path: str, bg_path: str, tts_path: str) -> str:
    """Merge TTS track + background audio, overlay on original video."""
    update_job(job_id, step="mixing", progress=88, message="Mixing final audio and video...")

    job_dir = get_job_dir(job_id)
    output_path = str(job_dir / "dubbed_output.mp4")

    try:
        # Mix background stem + TTS dubbing track
        mixed_audio_path = str(job_dir / "mixed_audio.wav")

        bg_input = ffmpeg.input(bg_path)
        tts_input = ffmpeg.input(tts_path)

        # Use amix to combine background (lower volume) + TTS (full volume)
        mixed = ffmpeg.filter(
            [bg_input.audio, tts_input.audio],
            "amix",
            inputs=2,
            duration="longest",
            weights="0.3 1.0",
        )
        ffmpeg.output(mixed, mixed_audio_path).overwrite_output().run(quiet=True)

        # Merge mixed audio with original video
        video_input = ffmpeg.input(video_path)
        audio_input = ffmpeg.input(mixed_audio_path)

        ffmpeg.output(
            video_input.video,
            audio_input.audio,
            output_path,
            vcodec="copy",
            acodec="aac",
            audio_bitrate="192k",
        ).overwrite_output().run(quiet=True)

    except ffmpeg.Error as e:
        raise RuntimeError(f"FFmpeg mixing failed: {str(e)[:500]}")

    update_job(job_id, progress=95, message="Final video rendered")
    return output_path


# ---------------------------------------------------------------------------
# Full Pipeline Orchestrator
# ---------------------------------------------------------------------------
async def run_pipeline(job_id: str, url: str, target_language: str):
    """Execute the full dubbing pipeline end-to-end."""
    try:
        update_job(
            job_id,
            status="processing",
            step="starting",
            progress=0,
            message="Starting pipeline...",
            error=None,
            output_file=None,
        )

        # 1. Download
        paths = await download_video(job_id, url)

        # 2. Separate audio
        stems = separate_audio(job_id, paths["audio"])

        # 3. Transcribe
        segments = transcribe_audio(job_id, stems["vocals"])

        # 4. Translate
        translated = translate_segments(job_id, segments, target_language)

        # 5. TTS
        tts_path = await synthesize_tts(job_id, translated, target_language)

        # 6. Mix
        output = mix_audio_video(job_id, paths["video"], stems["background"], tts_path)

        update_job(
            job_id,
            status="completed",
            progress=100,
            message="Dubbing complete! Your file is ready.",
            output_file=output,
        )

    except Exception as e:
        tb = traceback.format_exc()
        error_msg = str(e) if str(e) else repr(e)
        print(f"[PIPELINE ERROR] {tb}", flush=True)
        update_job(
            job_id,
            status="failed",
            message=f"Error: {error_msg}",
            error=error_msg,
        )
