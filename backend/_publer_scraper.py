"""
Standalone Publer.com scraper script.
Runs as a separate process to avoid Windows asyncio subprocess conflicts with uvicorn.

Usage: python _publer_scraper.py <youtube_url> <output_video_path>

Outputs the download URL to stdout on success, or exits with code 1 on failure.
"""

import sys
import json


def main():
    if len(sys.argv) != 3:
        print(json.dumps({"error": "Usage: python _publer_scraper.py <url> <output_path>"}))
        sys.exit(1)

    url = sys.argv[1]
    output_path = sys.argv[2]

    from playwright.sync_api import sync_playwright
    import httpx

    PUBLER_URL = "https://publer.com/tools/youtube-video-downloader"

    playwright = None
    browser = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        # Navigate to Publer
        page.goto(PUBLER_URL, wait_until="networkidle", timeout=30000)

        # Find and fill the URL input
        input_sel = None
        for sel in [
            'input[type="url"]',
            'input[type="text"]',
            'input[placeholder*="Paste"]',
            'input[placeholder*="paste"]',
            'input[placeholder*="URL"]',
            'input[placeholder*="url"]',
            'input[placeholder*="link"]',
            'input[name="url"]',
            "input.form-control",
            "input",
        ]:
            try:
                el = page.wait_for_selector(sel, timeout=3000)
                if el:
                    input_sel = sel
                    break
            except Exception:
                continue

        if not input_sel:
            print(json.dumps({"error": "Could not find URL input field on Publer page"}))
            sys.exit(1)

        page.fill(input_sel, url)

        # Click submit button
        btn_clicked = False
        for sel in [
            'button[type="submit"]',
            "button.btn-primary",
            "button.download-btn",
            'button:has-text("Download")',
            'button:has-text("Get")',
            'button:has-text("Process")',
            "form button",
            "button",
        ]:
            try:
                btn = page.wait_for_selector(sel, timeout=2000)
                if btn:
                    btn.click()
                    btn_clicked = True
                    break
            except Exception:
                continue

        if not btn_clicked:
            page.press(input_sel, "Enter")

        # Wait for download link
        download_href = None
        for sel in [
            'a[href*=".mp4"]',
            'a[href*="download"]',
            'a[download]',
            'a.download-btn',
            'a:has-text("Download")',
            'a:has-text("download")',
        ]:
            try:
                link = page.wait_for_selector(sel, timeout=60000)
                if link:
                    download_href = link.get_attribute("href")
                    if download_href:
                        break
            except Exception:
                continue

        if not download_href:
            print(json.dumps({"error": "Publer did not produce a download link within 60 seconds."}))
            sys.exit(1)

        # Make relative URLs absolute
        if download_href.startswith("/"):
            download_href = f"https://publer.com{download_href}"

    finally:
        if browser:
            browser.close()
        if playwright:
            playwright.stop()

    # Stream video to disk
    try:
        with httpx.Client(follow_redirects=True, timeout=300) as client:
            with client.stream("GET", download_href) as resp:
                resp.raise_for_status()
                with open(output_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                        f.write(chunk)
    except Exception as e:
        print(json.dumps({"error": f"Failed to download video file: {str(e)}"}))
        sys.exit(1)

    import os
    file_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    if file_size < 1024:
        print(json.dumps({"error": "Downloaded video file is empty or too small"}))
        sys.exit(1)

    # Success
    print(json.dumps({"success": True, "path": output_path, "size": file_size}))
    sys.exit(0)


if __name__ == "__main__":
    main()
