"""Utility functions for the YouTube dubbing pipeline."""

import re
import os
import shutil
import tempfile
from pathlib import Path

# Base directory for temporary job files
JOBS_DIR = Path(tempfile.gettempdir()) / "yt_dubbing_jobs"
JOBS_DIR.mkdir(exist_ok=True)


def validate_youtube_url(url: str) -> bool:
    """Validate that the given URL is a valid YouTube URL."""
    youtube_patterns = [
        r'(https?://)?(www\.)?youtube\.com/watch\?v=[\w-]{11}',
        r'(https?://)?(www\.)?youtube\.com/shorts/[\w-]{11}',
        r'(https?://)?youtu\.be/[\w-]{11}',
        r'(https?://)?(www\.)?youtube\.com/embed/[\w-]{11}',
    ]
    for pattern in youtube_patterns:
        if re.match(pattern, url.strip()):
            return True
    return False


def get_job_dir(job_id: str) -> Path:
    """Get the working directory for a specific job."""
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def cleanup_job(job_id: str):
    """Remove all temporary files for a completed job."""
    job_dir = JOBS_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)


def check_ffmpeg() -> bool:
    """Check if ffmpeg is available on the system PATH."""
    return shutil.which("ffmpeg") is not None


def get_language_voice(language: str) -> str:
    """Map a target language to an Edge-TTS voice name."""
    voice_map = {
        "spanish": "es-ES-AlvaroNeural",
        "french": "fr-FR-HenriNeural",
        "german": "de-DE-ConradNeural",
        "japanese": "ja-JP-KeitaNeural",
        "chinese": "zh-CN-YunxiNeural",
        "korean": "ko-KR-InJoonNeural",
        "portuguese": "pt-BR-AntonioNeural",
        "italian": "it-IT-DiegoNeural",
        "arabic": "ar-SA-HamedNeural",
        "hindi": "hi-IN-MadhurNeural",
        "russian": "ru-RU-DmitryNeural",
        "turkish": "tr-TR-AhmetNeural",
    }
    return voice_map.get(language.lower(), "es-ES-AlvaroNeural")


def get_language_code(language: str) -> str:
    """Map a target language name to its ISO language code for translation."""
    code_map = {
        "spanish": "es",
        "french": "fr",
        "german": "de",
        "japanese": "ja",
        "chinese": "zh-cn",
        "korean": "ko",
        "portuguese": "pt",
        "italian": "it",
        "arabic": "ar",
        "hindi": "hi",
        "russian": "ru",
        "turkish": "tr",
    }
    return code_map.get(language.lower(), "es")
