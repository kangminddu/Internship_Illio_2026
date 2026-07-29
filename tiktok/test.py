# tiktok/test.py
import asyncio
from playwright.async_api import async_playwright
from tiktok import parser

HANDLES = ["2.saho", "shu_.kkk", "singingame", "3arbie_nyoung", "itbackwards",
           "hohoeod9", "suntokk2", "songkun1106", "luminia1025", "minetube07"]

WAIT_MS = 10000          # 페이지당 대기
GAP_SEC = 3              # 채널 간 간격


async def probe(page, handle):
    """한 채널 조회 → (성공여부, HTML길이)"""
    url = f"https://www.tiktok.com/@{handle}"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(WAIT_MS)
        html = await page.content()
    except Exception as e:
        print(f"    ERR {handle}: {type(e).__name__}")
        return False, 0
    row = parser.parse_l1(html)
    return row is not None, len(html)


async def scenario(p, name, *, new_browser, new_context, clear_cache):
    """
    new_browser  : 채널마다 브라우저 새로 띄움
    new_context  : 채널마다 컨텍스트 새로 만듦
    clear_cache  : 채널마다 쿠키/스토리지 비움
    """
    print("=" * 60)
    print(f"{name}")
    print(f"  브라우저재생성={new_browser} 컨텍스트재생성={new_context} "
          f"캐시삭제={clear_cache}")
    print("=" * 60)

    ok = fail = 0
    browser = ctx = None
    try:
        if not new_browser:
            browser = await p.chromium.launch(headless=True)
        if not new_browser and not new_context:
            ctx = await browser.new_context()

        for i, h in enumerate(HANDLES):
            if i:
                await asyncio.sleep(GAP_SEC)

            if new_browser:
                browser = await p.chromium.launch(headless=True)
                ctx = await browser.new_context()
            elif new_context:
                ctx = await browser.new_context()
            elif clear_cache:
                await ctx.clear_cookies()
                try:
                    await ctx.clear_permissions()
                except Exception:
                    pass

            page = await ctx.new_page()
            try:
                good, size = await probe(page, h)
            finally:
                await page.close()

            print(f"  {h:18s} {size:>8,}  {'OK' if good else 'FAIL'}")
            ok += 1 if good else 0
            fail += 0 if good else 1

            if new_browser:
                await browser.close()
                browser = ctx = None
            elif new_context:
                await ctx.close()
                ctx = None
    finally:
        try:
            if ctx:
                await ctx.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass

    print(f"  → OK={ok} FAIL={fail}\n")
    return ok, fail


async def main():
    results = {}
    async with async_playwright() as p:
        results["A 완전공유"] = await scenario(
            p, "A. 브라우저·컨텍스트 재사용 (현재 l1.py 방식)",
            new_browser=False, new_context=False, clear_cache=False)
        await asyncio.sleep(10)

        results["B 캐시삭제"] = await scenario(
            p, "B. 컨텍스트 재사용 + 매번 쿠키/권한 삭제",
            new_browser=False, new_context=False, clear_cache=True)
        await asyncio.sleep(10)

        results["C 컨텍스트새로"] = await scenario(
            p, "C. 브라우저 유지 + 매번 새 컨텍스트 (캐시 완전 분리)",
            new_browser=False, new_context=True, clear_cache=False)
        await asyncio.sleep(10)

        results["D 브라우저새로"] = await scenario(
            p, "D. 매번 새 브라우저 (단독 실행과 동일)",
            new_browser=True, new_context=True, clear_cache=False)

    print("=" * 60)
    print("최종 비교")
    print("=" * 60)
    for k, (ok, fail) in results.items():
        rate = ok / (ok + fail) * 100 if ok + fail else 0
        print(f"  {k:16s} OK={ok:2d} FAIL={fail:2d}  성공률 {rate:5.1f}%")


asyncio.run(main())