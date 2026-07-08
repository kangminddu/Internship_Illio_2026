"""
Crawl4AI 테스트 — TikTok을 스텔스 모드로 뚫을 수 있는지 확인.
강민수님이 전에 Playwright로 CAPTCHA에 막혔던 그 지점을,
Crawl4AI의 스텔스+안티봇 기능이 넘는지 본다.

준비:
    pip install crawl4ai
    crawl4ai-setup

실행:
    python crawl4ai_test.py

확인 포인트:
    - HTML/데이터가 실제로 나오는가?
    - CAPTCHA("Drag the slider") / 로그인 벽 / 빈 페이지가 뜨는가?
    - 영상 URL, 팔로워 등 실제 데이터가 잡히는가?
"""

import asyncio
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, BrowserConfig, CacheMode


async def main():
    # 스텔스 모드 + 안티봇 우회 켜기
    browser_config = BrowserConfig(
        headless=True,
        enable_stealth=True,          # 브라우저 지문 위조
    )
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        magic=True,                   # 자동 안티봇 대응 시도
        wait_until="load",
        page_timeout=60000,
    )

    url = "https://www.tiktok.com/@tiktok"

    async with AsyncWebCrawler(config=browser_config) as crawler:
        print(f"긁는 중: {url}\n" + "=" * 50)
        result = await crawler.arun(url=url, config=run_config)

        if not result.success:
            print(f"✗ 실패: {result.error_message}")
            return

        html = result.html or ""
        text = (result.markdown or "")[:1500]

        print(f"성공 여부   : {result.success}")
        print(f"HTML 길이   : {len(html):,} 자")
        print(f"상태 코드   : {result.status_code}\n")

        # 차단/CAPTCHA 신호 탐지
        blocked_signals = [
            "captcha", "slider", "verify", "로그인", "log in", "login",
            "잠시 후 다시", "너무 많은 요청", "robot", "unusual",
        ]
        hits = [s for s in blocked_signals if s.lower() in html.lower()]
        if hits:
            print(f"⚠ 차단/인증 신호 감지: {hits}")
        else:
            print("차단 신호 없음 (겉보기)")

        # 실제 데이터 신호 탐지
        data_signals = ["follower", "video", "aweme", "팔로워", "unique_id"]
        data_hits = [s for s in data_signals if s.lower() in html.lower()]
        print(f"데이터 신호   : {data_hits if data_hits else '없음'}\n")

        print("--- 페이지 텍스트 앞부분 ---")
        print(text)


if __name__ == "__main__":
    asyncio.run(main())