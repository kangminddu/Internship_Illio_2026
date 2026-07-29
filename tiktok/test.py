# tiktok/test.py
import asyncio
from playwright.async_api import async_playwright
from tiktok import parser
from tiktok.steps.l1 import _block_heavy

HANDLES = ["2.saho", "shu_.kkk", "singingame", "3arbie_nyoung", "itbackwards",
           "hohoeod9", "suntokk2", "songkun1106", "luminia1025", "minetube07"]

WAIT_MS = 10000
GAP_SEC = 3


async def scenario(p, name, block):
    print("=" * 55)
    print(f"{name}")
    print("=" * 55)
    ok = fail = 0
    t0 = asyncio.get_event_loop().time()
    browser = await p.chromium.launch(headless=True)
    ctx = await browser.new_context()
    if block:
        await ctx.route("**/*", _block_heavy)
    try:
        for i, h in enumerate(HANDLES):
            if i:
                await asyncio.sleep(GAP_SEC)
            page = await ctx.new_page()
            try:
                await page.goto(f"https://www.tiktok.com/@{h}",
                                wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(WAIT_MS)
                html = await page.content()
                row = parser.parse_l1(html)
            except Exception as e:
                html, row = "", None
                print(f"    ERR {type(e).__name__}")
            finally:
                await page.close()
            print(f"  {h:18s} {len(html):>8,}  {'OK' if row else 'FAIL'}")
            ok += 1 if row else 0
            fail += 0 if row else 1
    finally:
        await browser.close()
    elapsed = asyncio.get_event_loop().time() - t0
    print(f"  → OK={ok} FAIL={fail} | {elapsed:.0f}초\n")
    return ok, fail, elapsed


async def main():
    async with async_playwright() as p:
        a = await scenario(p, "차단 있음 (현재 방식)", block=True)
        await asyncio.sleep(10)
        b = await scenario(p, "차단 없음 (전부 로드)", block=False)

    print("=" * 55)
    for name, (ok, fail, el) in [("차단 있음", a), ("차단 없음", b)]:
        print(f"  {name:10s} OK={ok:2d} FAIL={fail:2d}  "
              f"성공률 {ok*10:3d}%  {el:.0f}초")


asyncio.run(main())