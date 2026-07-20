import asyncio
import json
from playwright.async_api import async_playwright


VIDEO_URL = "https://www.tiktok.com/@bini_kong/video/7650088841105132808"


async def main():

    async with async_playwright() as p:

        browser = await p.chromium.connect_over_cdp(
            "http://127.0.0.1:9222"
        )

        context = browser.contexts[0]

        # Playwright가 새 탭 생성
        page = await context.new_page()

        got = False

        async def on_response(response):
            nonlocal got

            if got:
                return

            url = response.url

            if "/api/comment/list/" not in url:
                return

            got = True

            print("=" * 80)
            print("댓글 API 발견")
            print(url)

            try:
                data = await response.json()

                print("status :", data.get("status_code"))
                print("total  :", data.get("total"))
                print("cursor :", data.get("cursor"))
                print("has_more :", data.get("has_more"))

                comments = data.get("comments", [])

                print("댓글수 :", len(comments))

                for c in comments[:5]:
                    u = c.get("user", {})
                    print()
                    print("닉네임 :", u.get("nickname"))
                    print("아이디 :", u.get("unique_id"))
                    print("댓글 :", c.get("text"))

                with open(
                    "tiktok/comment_sample.json",
                    "w",
                    encoding="utf-8"
                ) as f:
                    json.dump(
                        data,
                        f,
                        ensure_ascii=False,
                        indent=4
                    )

                print("\n저장 완료")

            except Exception as e:
                print(e)

        page.on("response", on_response)

        print("영상 이동")
        await page.goto(
            VIDEO_URL,
            wait_until="domcontentloaded"
        )

        await page.wait_for_timeout(5000)

        print("버튼 찾는중")

        buttons = page.locator("button")

        cnt = await buttons.count()

        print("button =", cnt)

        for i in range(cnt):

            try:
                txt = (await buttons.nth(i).inner_text()).strip()

                if txt == "댓글":

                    print("댓글 버튼", i)

                    await buttons.nth(i).hover()

                    await page.wait_for_timeout(500)

                    await buttons.nth(i).click(delay=150)

                    break

            except:
                pass

        print("10초 대기")

        await page.wait_for_timeout(10000)

        input("엔터 종료")


asyncio.run(main())