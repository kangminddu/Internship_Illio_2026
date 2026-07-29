# tiktok/test.py
import asyncio
from playwright.async_api import async_playwright
from tiktok import config

HANDLE = "dalsia819"   # 성공하는 채널

async def main():
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=config.PROFILE_DIR, headless=True)
        page = await ctx.new_page()
        await page.goto(f"https://www.tiktok.com/@{HANDLE}",
                        wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(20000)
        h = await page.content()
        open(f"ok_{HANDLE}.html","w",encoding="utf-8").write(h)
        print("길이", len(h), "스크립트:", "__UNIVERSAL_DATA_FOR_REHYDRATION__" in h)
        await ctx.close()

asyncio.run(main())