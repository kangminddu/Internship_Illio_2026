# test_pw.py
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context()
        await ctx.add_cookies([{"name":"SOCS","value":"CAI",
                                "domain":".youtube.com","path":"/"}])
        page = await ctx.new_page()
        r = await page.goto("https://www.youtube.com/watch?v=dQw4w9WgXcQ&hl=ko&gl=KR")
        print("status:", r.status)
        print("title:", await page.title())
        await b.close()

asyncio.run(main())