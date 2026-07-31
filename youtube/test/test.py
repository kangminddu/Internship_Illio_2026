# test_chrome.py
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(
            executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            headless=False)
        page = await b.new_page()
        r = await page.goto("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        print("status:", r.status)
        print("title:", await page.title())
        await b.close()

asyncio.run(main())