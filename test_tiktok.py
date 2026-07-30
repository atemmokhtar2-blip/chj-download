import asyncio
import sys
import os

# Add current directory to path
sys.path.append(os.getcwd())

from services.downloader import analyze_url, download_video

async def test_tiktok(url):
    print(f"\n--- Testing TikTok URL: {url} ---")
    try:
        info = await analyze_url(url)
        if not info:
            print("FAILED: analyze_url returned None")
            return
        
        print(f"SUCCESS: Analyzed {info['platform']} {info['media_type']}")
        print(f"Title: {info['title']}")
        
        if info['media_type'] == 'video':
            print(f"Qualities: {[q['label'] for q in info['qualities']]}")
            if info['qualities']:
                best = info['qualities'][-1]
                print(f"Attempting to download best quality: {best['label']}")
                path = await download_video(url, best['format_id'], best['label'])
                if path:
                    print(f"DOWNLOAD SUCCESS: {path}")
                    if os.path.exists(path):
                        os.remove(path)
                else:
                    print("DOWNLOAD FAILED")
            else:
                # If no qualities, it might be using "best" logic
                print("No qualities found, attempting 'best' format_id...")
                path = await download_video(url, "best", "best")
                if path:
                    print(f"DOWNLOAD SUCCESS (best): {path}")
                    if os.path.exists(path):
                        os.remove(path)
                else:
                    print("DOWNLOAD FAILED (best)")
    except Exception as e:
        print(f"ERROR: {str(e)}")

async def main():
    # Use a real public TikTok URL
    urls = [
        "https://www.tiktok.com/@khaby.lame/video/7326640986751962373"
    ]
    for url in urls:
        await test_tiktok(url)

if __name__ == "__main__":
    asyncio.run(main())
