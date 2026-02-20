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
# Step 1 – Download
# ---------------------------------------------------------------------------
def download_video(job_id: str, url: str) -> dict:
    """Download video + audio from YouTube using yt-dlp Python API."""
    import yt_dlp

    update_job(job_id, step="downloading", progress=5, message="Downloading video from YouTube...")

    job_dir = get_job_dir(job_id)
    output_template = str(job_dir / "source.%(ext)s")
    cookies_path = Path(__file__).parent / "cookies.txt"

    # Base options — use simple "best" format to avoid "Requested format is not available"
    base_opts = {
        "format": "best",
        "outtmpl": output_template,
        "noplaylist": True,
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "extractor_args": {"youtube": {"player_client": ["android,web"]}},
    }

    # Build strategies in priority order
    strategies = []

    # Strategy 1 (PRIMARY): cookies.txt + Android client
    # This is the most reliable method on Windows where DPAPI breaks browser cookie reading
    if cookies_path.exists() and cookies_path.stat().st_size > 0:
        strategies.append({
            **base_opts,
            "cookiefile": str(cookies_path),
            "_label": "Downloading with cookies.txt (primary)...",
        })

    # Strategy 2: Impersonate Chrome via curl_cffi + Android client
    try:
        import curl_cffi
        strategies.append({
            **base_opts,
            "impersonate": "chrome",
            "_label": "Impersonating Chrome + Android client...",
        })
    except ImportError:
        pass

    # Strategy 3: Plain guest download with browser user-agent
    strategies.append({
        **base_opts,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        },
        "_label": "Guest download with Android client...",
    })

    def _cleanup_temp_files():
        """Delete .part, .ytdl, and partial source files to prevent FFmpeg muxing conflicts."""
        for f in job_dir.iterdir():
            if f.suffix in (".part", ".ytdl") or f.name.startswith("source"):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass

    last_error = None
    for i, opts in enumerate(strategies):
        label = opts.pop("_label", f"Strategy {i+1}")
        update_job(job_id, message=label)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

            # Check if the file was actually downloaded
            video_file = None
            for f in job_dir.iterdir():
                if f.name.startswith("source") and f.suffix in (".mp4", ".mkv", ".webm"):
                    video_file = f
                    break

            if video_file:
                # Success! Extract audio and return
                audio_file = job_dir / "source_audio.wav"
                ffmpeg.input(str(video_file)).output(
                    str(audio_file), ac=1, ar=16000, format="wav"
                ).overwrite_output().run(quiet=True)

                update_job(job_id, progress=15, message="Download complete")
                return {"video": str(video_file), "audio": str(audio_file)}
            else:
                last_error = "Downloaded file not found on disk"

        except Exception as e:
            last_error = str(e)
            _cleanup_temp_files()
            continue

    raise RuntimeError(f"All download strategies failed. Last error: {last_error}")



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
        paths = download_video(job_id, url)

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
        update_job(
            job_id,
            status="failed",
            message=f"Error: {str(e)}",
            error=str(e),
        )
