import asyncio
from playwright.async_api import async_playwright

VIDEO_ID = "hLJ9SAo90cQ"

seen = []

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        async def on_response(response):
            if "youtubei/v1/next" in response.url:
                print("\n=== youtubei/v1/next ===")
                print(response.url)

                try:
                    data = await response.json()
                    seen.append(data)
                    print("response 저장 완료")
                except Exception as e:
                    print("json 실패:", e)

        page.on("response", on_response)

        url = f"https://www.youtube.com/watch?v={VIDEO_ID}&hl=ko&gl=KR"

        print("접속:", url)
        await page.goto(url)

        print("현재 URL:", page.url)

        await page.wait_for_timeout(1000)

        try:
            await page.click("button[aria-label*='모두 수락']", timeout=3000)
        except:
            pass

        await page.evaluate("window.scrollTo(0, 800)")

        for i in range(10):
            await page.evaluate("window.scrollBy(0, 1000)")
            await page.wait_for_timeout(1000)
            print(f"scroll {i+1}")

        print("\n==============================")
        print("youtubei/v1/next 응답 수:", len(seen))

        payloads = 0

        def walk(obj):
            nonlocal payloads

            if isinstance(obj, dict):
                if "commentEntityPayload" in obj:
                    payloads += 1
                for v in obj.values():
                    walk(v)

            elif isinstance(obj, list):
                for v in obj:
                    walk(v)

        for d in seen:
            walk(d)

        print("commentEntityPayload 수:", payloads)

        input("\n엔터 누르면 종료")

        await browser.close()

asyncio.run(main())