"""
Crawl4AI 테스트 2 — TikTok 개별 영상 페이지에서 '댓글 + 작성자'가 나오는지.
프로필(L1)은 지난 테스트에서 됐음. 이번엔 진짜 관문인 L3(댓글 작성자).

실행:
    python crawl4ai_test_video.py

확인 포인트:
    - 댓글 텍스트가 HTML에 있는가?
    - 댓글 작성자 username이 있는가?  ← 우리 핵심
    - 아니면 로그인/CAPTCHA 벽인가?
"""

import asyncio
import re
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, BrowserConfig, CacheMode

VIDEO_URL = "https://www.tiktok.com/@tiktok/video/7657279285719207198"


async def main():
    browser_config = BrowserConfig(headless=True, enable_stealth=True)
    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        magic=True,
        wait_until="networkidle",   # 댓글이 늦게 로드되므로 네트워크 잠잠할 때까지 대기
        page_timeout=90000,
        delay_before_return_html=5.0,  # 댓글 JS 렌더링 여유
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        print(f"긁는 중: {VIDEO_URL}\n" + "=" * 55)
        result = await crawler.arun(url=VIDEO_URL, config=run_config)

        if not result.success:
            print(f"✗ 실패: {result.error_message}")
            return

        html = result.html or ""
        print(f"성공 : {result.success} | 상태 {result.status_code} | "
              f"HTML {len(html):,}자\n")

        # --- L2: 영상 지표 신호 ---
        l2 = [s for s in ["digg_count", "play_count", "comment_count",
                          "좋아요", "share"] if s.lower() in html.lower()]
        print(f"[L2 영상지표 신호] {l2 if l2 else '없음'}")

        # --- L3: 댓글 관련 신호 ★ 핵심 ★ ---
        l3 = [s for s in ["comment", "reply", "unique_id", "user_id",
                          "댓글", "authorstats"] if s.lower() in html.lower()]
        print(f"[L3 댓글 신호]    {l3 if l3 else '없음'}")

        # 실제 댓글 작성자 username 패턴이 잡히나 (@아이디)
        handles = set(re.findall(r'"unique_id":"([^"]+)"', html))
        print(f"[작성자 unique_id 추출] {len(handles)}개 "
              f"{list(handles)[:5] if handles else ''}")

        # --- 차단 신호 ---
        blocked = [s for s in ["captcha", "slider", "verify",
                               "log in to", "로그인하여", "too many"]
                   if s.lower() in html.lower()]
        print(f"[차단/인증 신호]   {blocked if blocked else '없음'}\n")

        # --- 판정 ---
        if handles:
            print("✓✓ 댓글 작성자 ID 실제 추출됨 → L3 뚫림! (예상 뒤집힘)")
        elif l3:
            print("△ 댓글 관련 구조는 있으나 작성자 ID는 안 잡힘")
        else:
            print("✗ 댓글/작성자 없음 → L3 막힘 (프로필 겉면만 가능)")

        print("\n--- 텍스트 앞부분 ---")
        print((result.markdown or "")[:1200])


if __name__ == "__main__":
    asyncio.run(main())