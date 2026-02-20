"""Quick test: does yt-dlp strategy work?"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(__file__))

from _download_scraper import try_ytdlp

with tempfile.TemporaryDirectory() as td:
    output_path = os.path.join(td, "source.mp4")
    # Use a very short video to test
    result = try_ytdlp(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        output_path,
        td,
    )
    print(json.dumps(result, indent=2, default=str))
