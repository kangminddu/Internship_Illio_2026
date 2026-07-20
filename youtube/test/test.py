import asyncio
import json

from playwright.async_api import async_playwright

CHANNEL = "https://www.youtube.com/channel/UCr48whguz2qOwQOq99BejnA/about?hl=en&gl=US"


async def main():

    async with async_playwright() as p:

        browser = await p.chromium.connect_over_cdp(
            "http://127.0.0.1:9222"
        )

        context = browser.contexts[0]
        page = await context.new_page()

        async def on_response(resp):
            if "reveal_business_email" in resp.url:
                print("\n========== API ==========")
                print(resp.url)

                try:
                    body = await resp.json()
                    print(json.dumps(body, indent=2, ensure_ascii=False))
                except Exception as e:
                    print("json parse 실패:", e)

        page.on("response", on_response)

        print("접속:", CHANNEL)
        await page.goto(CHANNEL)

        await page.wait_for_timeout(3000)

        # 이메일 주소 보기 버튼 찾기
        texts = [
            "이메일 주소 보기",
            "이메일 보기",
            "View email address",
            "View email",
        ]

        clicked = False

        for text in texts:

            try:
                btn = page.get_by_text(text, exact=False)

                if await btn.count():

                    print("버튼 발견 :", text)

                    await btn.first.click()

                    clicked = True
                    break

            except Exception:
                pass

        if not clicked:
            print("버튼을 못 찾음")

        print("\n120초 대기 (CAPTCHA 풀거나 응답 확인)")
        await page.wait_for_timeout(120000)


asyncio.run(main())