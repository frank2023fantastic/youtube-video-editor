"""
Multi-strategy YouTube video downloader (runs as a standalone subprocess).

Strategies (tried in order):
  1. yt-dlp  — most reliable, handles geo-restrictions, auth, etc.
  2. Publer.com via Playwright — headless scrape fallback (if yt-dlp missing)

Usage:
    python _download_scraper.py <youtube_url> <output_video_path> [job_dir]

Outputs JSON to stdout:
    {"success": true, "path": "...", "size": 12345, "strategy": "yt-dlp"}
    {"error": "...", "debug_screenshot": "..."}
"""

import sys
import os
import json
import time
import traceback
from pathlib import Path


# ──────────────────────────────────────────────────────────────────────────────
# Strategy 1 — yt-dlp (best reliability, activeively maintained)
# ──────────────────────────────────────────────────────────────────────────────
def try_ytdlp(url: str, output_path: str, job_dir: str | None) -> dict | None:
    """Try downloading with yt-dlp, returning result dict or None on failure."""
    try:
        import yt_dlp
    except ImportError:
        return {"skipped": True, "reason": "yt-dlp not installed"}

    errors = []
    cookies_path = Path(__file__).parent / "cookies.txt"
    output_template = str(Path(output_path).parent / "source.%(ext)s")

    # Base options shared across all attempts
    base_opts = {
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "sleep_interval": 2,
        "max_sleep_interval": 5,
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "default"]
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
        },
    }

    if cookies_path.exists() and cookies_path.stat().st_size > 0:
        base_opts["cookiefile"] = str(cookies_path)

    # Format strategies from most specific to most permissive
    format_strategies = [
        # 1. Best mp4 video + m4a audio (no merge needed)
        {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
            "label": "best-mp4",
        },
        # 2. Best video+audio with merge to mp4
        {
            "format": "bestvideo+bestaudio/best",
            "label": "best-merge",
        },
        # 3. Simple "best" — single stream, always works
        {
            "format": "best",
            "label": "simple-best",
        },
        # 4. 720p cap — for restricted videos
        {
            "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "label": "720p-cap",
        },
        # 5. Absolute fallback — any format, convert with ffmpeg
        {
            "format": "worst",
            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                }
            ],
            "label": "worst-convert",
        },
    ]

    for strategy in format_strategies:
        label = strategy.pop("label")
        opts = {**base_opts, **strategy}

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

            # Find the downloaded file (may have different extension before merge)
            video_file = _find_video_file(Path(output_path).parent)
            if video_file:
                # Rename to expected output path if needed
                if str(video_file) != output_path:
                    video_file.rename(output_path)
                file_size = os.path.getsize(output_path)
                if file_size > 1024:
                    return {
                        "success": True,
                        "path": output_path,
                        "size": file_size,
                        "strategy": f"yt-dlp/{label}",
                    }
                else:
                    errors.append(f"{label}: file too small ({file_size} bytes)")
            else:
                errors.append(f"{label}: no video file found after download")

        except Exception as e:
            errors.append(f"{label}: {str(e)[:200]}")
            continue

    return {"skipped": False, "errors": errors}


# ──────────────────────────────────────────────────────────────────────────────
# Strategy 2 — Publer.com via Playwright (stealth + debug screenshots)
# ──────────────────────────────────────────────────────────────────────────────
def try_publer(url: str, output_path: str, job_dir: str | None) -> dict | None:
    """Try downloading via Publer.com headless scraper with bot-evasion."""
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
        import httpx
    except ImportError as e:
        return {"skipped": True, "reason": f"Missing dependency: {e}"}

    PUBLER_URL = "https://publer.com/tools/youtube-video-downloader"
    debug_dir = Path(job_dir) if job_dir else Path(output_path).parent
    playwright_obj = None
    browser = None

    try:
        playwright_obj = sync_playwright().start()
        browser = playwright_obj.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = context.new_page()

        # Apply stealth to evade bot detection
        Stealth().apply_stealth_sync(page)

        # Navigate to Publer
        page.goto(PUBLER_URL, wait_until="networkidle", timeout=30000)

        # Dismiss cookie consent banner
        try:
            accept_btn = page.wait_for_selector("button.cky-btn-accept", timeout=3000)
            if accept_btn:
                accept_btn.click()
                page.wait_for_timeout(1000)
        except Exception:
            pass

        # Save initial state screenshot
        page.screenshot(path=str(debug_dir / "debug_publer_initial.png"))

        # Fill the URL input (exact selector from DOM probe)
        input_el = page.query_selector('input[placeholder="https://"]')
        if not input_el:
            input_el = page.query_selector('input[type="text"][name="url"]')
        if not input_el:
            page.screenshot(path=str(debug_dir / "debug_timeout.png"))
            return {"skipped": False, "errors": ["Could not find URL input on Publer"]}

        input_el.click()
        input_el.type(url, delay=30)  # Type like a human
        page.wait_for_timeout(500)

        # Click the Download submit button
        submit_btn = page.query_selector('button[type="submit"]')
        if submit_btn:
            submit_btn.click()
        else:
            page.press('input[placeholder="https://"]', "Enter")

        # Wait for download link to appear (polling approach, up to 120s)
        download_href = None
        start_time = time.time()
        timeout_secs = 120

        while time.time() - start_time < timeout_secs:
            # Check if input is re-enabled (processing finished)
            input_disabled = page.evaluate("""() => {
                const inp = document.querySelector('input[placeholder="https://"]');
                return inp ? inp.disabled : null;
            }""")

            # Look for actual download links (not navigation links)
            links = page.evaluate("""() => {
                const results = [];
                document.querySelectorAll('a').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const dl = a.getAttribute('download');
                    const text = a.innerText?.trim() || '';
                    if (href.includes('.mp4') || href.includes('.webm') ||
                        href.includes('googlevideo') || href.includes('videoplayback') ||
                        (dl !== null && href.startsWith('http'))) {
                        results.push({href, download: dl, text: text.substring(0, 50)});
                    }
                });
                return results;
            }""")

            if links:
                download_href = links[0]["href"]
                break

            # Check for video elements with src
            video_src = page.evaluate("""() => {
                const v = document.querySelector('video source, video[src]');
                return v ? (v.getAttribute('src') || '') : '';
            }""")
            if video_src and video_src.startswith("http"):
                download_href = video_src
                break

            page.wait_for_timeout(3000)

        if not download_href:
            # Save debug screenshot on timeout
            screenshot_path = str(debug_dir / "debug_timeout.png")
            try:
                page.screenshot(path=screenshot_path, full_page=True)
            except Exception:
                screenshot_path = None

            # Dump page state for debugging
            page_state = page.evaluate("""() => {
                const form = document.querySelector('form');
                return {
                    formHTML: form ? form.innerHTML.substring(0, 500) : 'no form',
                    iframes: Array.from(document.querySelectorAll('iframe')).map(
                        i => i.src?.substring(0, 100)
                    ),
                    url: window.location.href,
                };
            }""")

            return {
                "skipped": False,
                "errors": [
                    f"Publer did not produce a download link within {timeout_secs}s",
                    f"Page state: {json.dumps(page_state)}",
                ],
                "debug_screenshot": screenshot_path,
            }

        # Make relative URLs absolute
        if download_href.startswith("/"):
            download_href = f"https://publer.com{download_href}"

    except Exception as e:
        # Save debug screenshot on any error
        screenshot_path = None
        if page:
            try:
                screenshot_path = str(debug_dir / "debug_timeout.png")
                page.screenshot(path=screenshot_path, full_page=True)
            except Exception:
                pass
        return {
            "skipped": False,
            "errors": [f"Playwright error: {str(e)}"],
            "debug_screenshot": screenshot_path,
        }

    finally:
        if browser:
            browser.close()
        if playwright_obj:
            playwright_obj.stop()

    # Stream video file to disk using httpx
    try:
        with httpx.Client(follow_redirects=True, timeout=300) as client:
            with client.stream("GET", download_href) as resp:
                resp.raise_for_status()
                with open(output_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=256 * 1024):
                        f.write(chunk)
    except Exception as e:
        return {"skipped": False, "errors": [f"Video download failed: {str(e)}"]}

    file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    if file_size < 1024:
        return {"skipped": False, "errors": [f"Downloaded file too small: {file_size} bytes"]}

    return {"success": True, "path": output_path, "size": file_size, "strategy": "publer"}


# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────
def _find_video_file(directory: Path) -> Path | None:
    """Find the first video file in directory."""
    for ext in ("*.mp4", "*.mkv", "*.webm", "*.avi"):
        files = list(directory.glob(f"source{ext[1:]}"))
        if files:
            return files[0]
    # Broader search
    for ext in ("*.mp4", "*.mkv", "*.webm"):
        files = list(directory.glob(ext))
        if files:
            return files[0]
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 3:
        print(json.dumps({
            "error": "Usage: python _download_scraper.py <url> <output_path> [job_dir]"
        }))
        sys.exit(1)

    url = sys.argv[1]
    output_path = sys.argv[2]
    job_dir = sys.argv[3] if len(sys.argv) > 3 else None

    all_errors = []

    # ── Strategy 1: yt-dlp (preferred) ────────────────────────────────────
    result = try_ytdlp(url, output_path, job_dir)
    if result and result.get("success"):
        print(json.dumps(result))
        sys.exit(0)
    if result:
        if result.get("skipped"):
            all_errors.append(f"yt-dlp: {result.get('reason', 'skipped')}")
        else:
            for err in result.get("errors", []):
                all_errors.append(f"yt-dlp: {err}")

    # ── Strategy 2: Publer.com (fallback) ─────────────────────────────────
    result = try_publer(url, output_path, job_dir)
    if result and result.get("success"):
        print(json.dumps(result))
        sys.exit(0)
    if result:
        if result.get("skipped"):
            all_errors.append(f"publer: {result.get('reason', 'skipped')}")
        else:
            for err in result.get("errors", []):
                all_errors.append(f"publer: {err}")

    # ── All strategies failed ─────────────────────────────────────────────
    error_summary = " | ".join(all_errors) if all_errors else "All strategies failed"
    debug_screenshot = None
    if result and isinstance(result, dict):
        debug_screenshot = result.get("debug_screenshot")

    output = {"error": error_summary}
    if debug_screenshot:
        output["debug_screenshot"] = debug_screenshot

    print(json.dumps(output))
    sys.exit(1)


if __name__ == "__main__":
    main()
