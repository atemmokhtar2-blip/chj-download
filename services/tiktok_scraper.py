import re
import requests
import logging
from typing import Optional

logger = logging.getLogger(__name__)

def scrape_tiktok(url: str) -> Optional[dict]:
    """
    Advanced TikTok scraper that handles:
    1. Short URL expansion (vt.tiktok.com, tiktok.com/t/)
    2. User-Agent rotation (Mobile/Desktop)
    3. Metadata extraction from HTML/JSON
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
    }

    session = requests.Session()
    try:
        # 1. Expand short URL if needed
        response = session.get(url, headers=headers, timeout=15, allow_redirects=True)
        final_url = response.url
        html = response.text
        
        # If redirected to home page, we were blocked or link is dead
        if final_url.strip("/") == "https://www.tiktok.com":
             # Try one more time with different headers
             headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
             response = session.get(url, headers=headers, timeout=15, allow_redirects=True)
             final_url = response.url
             html = response.text

        video_id_match = re.search(r'video/(\d+)', final_url)
        if not video_id_match:
            video_id_match = re.search(r'v/(\d+)', final_url)
            
        if not video_id_match:
            logger.error(f"TikTok Scraper: Could not find video ID in {final_url}")
            # Even if we can't find ID, if we have HTML, try to get meta
            if '<meta property="og:title"' not in html:
                return None

        # 2. Extract Metadata (Title, Author, Thumbnail)
        title = "TikTok Video"
        title_match = re.search(r'<meta property="og:title" content="(.*?)"', html)
        if title_match:
            title = title_match.group(1)

        author = "TikTok User"
        author_match = re.search(r'"authorName":"(.*?)"', html)
        if author_match:
            author = author_match.group(1)

        thumb = ""
        thumb_match = re.search(r'<meta property="og:image" content="(.*?)"', html)
        if thumb_match:
            thumb = thumb_match.group(1)

        # 3. Try to find the no-watermark video URL in JSON
        # This is harder now as TikTok encrypts/hides it, but we can return the webpage_url 
        # and let yt-dlp try again with the expanded URL and better headers.
        
        return {
            "title": title,
            "uploader": author,
            "duration": "Unknown",
            "thumbnail": thumb,
            "platform": "TikTok",
            "media_type": "video",
            "url": url,
            "webpage_url": final_url,
            "qualities": [{"label": "Best", "format_id": "best"}],
        }

    except Exception as e:
        logger.error(f"TikTok Scraper Error: {e}")
        return None
