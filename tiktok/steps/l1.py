# tiktok/steps/l1.py
import os

from playwright.async_api import async_playwright

from tiktok import config
from tiktok import parser


def normalize(channel):
    c = channel.strip()
    if c.startswith("http"):
        url = c.split("?")[0].rstrip("/")
        handle = url.split("/@")[-1].split("/")[0]
        return handle, url
    handle = c.lstrip("@")
    return handle, "https://www.tiktok.com/@" + handle


async def fetch_html(url):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=config.HEADLESS)
        ctx_kwargs = dict(
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        if os.path.exists(config.SESSION_PATH):
            ctx_kwargs["storage_state"] = config.SESSION_PATH
        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(int(config.L1_DELAY * 1000))
            html = await page.content()
        finally:
            await context.close()
            await browser.close()
        return html


async def run(channel=None, **_):
    if not channel:
        print("[L1] --channel <handle> 필요 (예: --channel @nba)")
        return
    handle, url = normalize(channel)
    print("[L1] fetch:", url)
    html = await fetch_html(url)
    print("[L1] html length:", len(html))
    row = parser.parse_l1(html)
    if row is None:
        print("[L1] DATA NONE - __UNIVERSAL_DATA_FOR_REHYDRATION__ 비었거나 없음")
        print("     세션 플래그/캡차/로그인 벽 가능성 (헤드풀 창 확인)")
        print("     script 태그 존재:", "__UNIVERSAL_DATA_FOR_REHYDRATION__" in html)
        return
    print("[L1] OK 파싱 성공:")
    for k, v in row.items():
        print("     %-16s: %s" % (k, v))
    return row
