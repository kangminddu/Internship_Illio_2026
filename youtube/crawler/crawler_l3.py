import argparse
import asyncio
import json
import re
import pymysql
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright

# ==========================================
# 환경 설정
# ==========================================
from config import DB
from config import L3_VIDEOS_PER_CHANNEL as VIDEOS_PER_CHANNEL
from config import L3_MAX_SCROLLS as MAX_SCROLLS
from config import L3_COMMENT_LIMIT as COMMENT_LIMIT
from config import L3_WORKERS
# 테스트용 설정: 먼저 2~3개 채널만 돌려서 팬 추적 로직을 검증하세요.
# 전체 33개 채널을 실전으로 수집하려면 이 값을 None으로 변경하세요.
TEST_CHANNELS = None      


# ==========================================
# 유틸리티 함수
# ==========================================
def parse_relative_date(text, now=None):
    if now is None:
        now = datetime.now()
    if not text:
        return None
    m = re.search(r"(\d+)\s*(초|분|시간|일|주|개월|년)", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"초":0,"분":0,"시간":0,"일":1,"주":7,"개월":30,"년":365}
        return now - timedelta(days=n * days.get(unit, 0))
    return None


def find_payloads(obj, results=None):
    if results is None:
        results = []
    if isinstance(obj, dict):
        if "commentEntityPayload" in obj:
            results.append(obj["commentEntityPayload"])
        for v in obj.values():
            find_payloads(v, results)
    elif isinstance(obj, list):
        for v in obj:
            find_payloads(v, results)
    return results

# [핵심 최적화 1] 브라우저 렌더링 리소스 차단 (속도 대폭 향상)
async def block_resources(route):
    if route.request.resource_type in ["image", "media", "font"]:
        await route.abort()
    else:
        await route.continue_()

# ==========================================
# 크롤링 코어
# ==========================================
async def scrape_comments(page, video_id):
    """영상 1개 댓글 긁기. (댓글 리스트, 응답수) 반환."""
    seen = []
    def on_resp(response):
        if "youtubei/v1/next" in response.url:
            asyncio.create_task(_grab(response, seen))
    async def _grab(response, seen):
        try:
            seen.append(await response.json())
        except Exception:
            pass

    page.on("response", on_resp)
    await page.goto(f"https://www.youtube.com/watch?v={video_id}&hl=ko&gl=KR")
    
    # [핵심 최적화 2] 대기 시간 소폭 단축 (안전성 유지 선에서 타협)
    await page.wait_for_timeout(800) 
    try:
        await page.click("button[aria-label*='모두 수락']", timeout=2000)
    except Exception:
        pass
    
    await page.evaluate("window.scrollTo(0, 600)")
    await page.wait_for_timeout(800)

    comments = {}
    last, stable = -1, 0
    for i in range(MAX_SCROLLS):
        await page.evaluate("window.scrollBy(0, 800)")
        await page.wait_for_timeout(800) # 기존 1800 -> 1500으로 단축
        
        for data in seen:
            for pl in find_payloads(data):
                cid = pl.get("properties", {}).get("commentId", "")
                if cid:
                    comments[cid] = pl

        # 댓글 상한 도달 → 중단
        if len(comments) >= COMMENT_LIMIT:
            break

        if len(comments) == last and len(comments) > 0:
            stable += 1
            if stable >= 3:
                break
        elif len(comments) == 0 and i >= 5:
            break   # 5번 스크롤해도 댓글 0 → 댓글 없는 영상
        else:
            stable = 0
        last = len(comments)

    page.remove_listener("response", on_resp)
    return list(comments.values())

# ==========================================
# DB 적재
# ==========================================
def save_comments(conn, content_id, payloads):
    now = datetime.now()
    saved = 0
    with conn.cursor() as cur:
        for pl in payloads:
            props = pl.get("properties", {})
            author = pl.get("author", {})
            toolbar = pl.get("toolbar", {})

            uc = author.get("channelId")
            if not uc:
                continue
            comment_id = props.get("commentId", "")
            text = props.get("content", {}).get("content", "")
            name = author.get("displayName")
            like_raw = toolbar.get("likeCountNotliked", "0")
            like = int(re.sub(r"[^\d]", "", str(like_raw)) or 0)
            pub = parse_relative_date(props.get("publishedTime"), now)

            # [DB 최적화 1] fan UPSERT (LAST_INSERT_ID 활용)
            cur.execute("""
                INSERT INTO fans (platform, external_author_id, first_seen_at, last_seen_at)
                VALUES ('youtube', %s, %s, %s)
                ON DUPLICATE KEY UPDATE 
                    last_seen_at = VALUES(last_seen_at),
                    fan_id = LAST_INSERT_ID(fan_id)
            """, (uc, now, now))
            fan_id = cur.lastrowid 

            # [DB 최적화 2] comments 중복 방지 방어 코드 적용
            cur.execute("""
                INSERT INTO comments
                  (content_id, fan_id, external_comment_id, author_display_name,
                   comment_text, like_count, published_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  like_count = VALUES(like_count),
                  author_display_name = VALUES(author_display_name),
                  comment_text = VALUES(comment_text)
            """, (content_id, fan_id, comment_id, name, text, like, pub))
            saved += 1
    return saved

# ==========================================
# 메인 실행부
# ==========================================
# ==========================================
# 채널 1개 처리 (병렬 워커)
# ==========================================
async def process_channel(browser, sem, channel_id, nickname, channel_url):
    """채널 하나를 독립 컨텍스트 + 독립 DB 커넥션으로 처리."""
    async with sem:  # 동시 실행 수 제한
        # 채널마다 독립 DB 커넥션 (공유하면 병렬에서 충돌)
        conn = pymysql.connect(**DB, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                SELECT content_id, external_id, content_type
                FROM contents
                WHERE channel_id=%s
                AND content_type IN ('video','shorts')
                ORDER BY published_at DESC
                LIMIT %s
                """, (
                    channel_id,
                    VIDEOS_PER_CHANNEL,
                ))

                videos = cur.fetchall()
            video_cnt = sum(1 for _, _, t in videos if t == "video")
            shorts_cnt = sum(1 for _, _, t in videos if t == "shorts")
            # 채널마다 독립 컨텍스트 + 페이지 (브라우저는 공유, 컨텍스트는 분리)
            context = await browser.new_context()
            page = await context.new_page()
            await page.route("**/*", block_resources)

            print(
            f"\n=== {nickname} (ch={channel_id}) | "
            f"총 {len(videos)}개 "
            f"(영상 {video_cnt}, 쇼츠 {shorts_cnt}) ==="
            )
            ch_comments = 0

            for content_id, video_id, content_type in videos:

                kind = "쇼츠" if content_type == "shorts" else "영상"

                try:
                    payloads = await scrape_comments(page, video_id)
                    n = save_comments(conn, content_id, payloads)
                    ch_comments += n

                    print(
                        f"  [{nickname}] "
                        f"{kind} | {video_id} | 댓글 {n}개"
                    )

                except Exception as e:
                    print(
                        f"  [{nickname}] "
                        f"{kind} | {video_id} | 에러: {e}"
                    )

                    try:
                        await page.goto("about:blank")
                    except Exception:
                        pass

            await context.close()  # 컨텍스트 닫아 메모리 회수

            # crawl_logs 기록
            with conn.cursor() as cur:
                safe_url = channel_url or f"https://www.youtube.com/channel/{channel_id}"
                cur.execute("""
                    INSERT INTO crawl_logs
                    (channel_id, target_url, layer, status, http_status)
                    VALUES (%s, %s, 'L3', 'success', 200)
                """, (channel_id, safe_url))

            print(
                f"  → [완료] {nickname} | "
                f"총 {len(videos)}개 "
                f"(영상 {video_cnt}, 쇼츠 {shorts_cnt}) | "
                f"댓글 {ch_comments}개"
            )
        finally:
            conn.close()


# ==========================================
# 메인 실행부 (병렬)
# ==========================================
async def main(channel_id=None):
    conn = pymysql.connect(**DB, autocommit=True)
    with conn.cursor() as cur:
        sql = """
        SELECT ch.channel_id, cr.nickname, ch.channel_url_normalized
        FROM channels ch
        JOIN creators cr ON ch.creator_id = cr.creator_id
        WHERE ch.platform='youtube'
        """
        params = []
        if channel_id is None:
            sql += """
            AND ch.channel_id NOT IN (
                SELECT channel_id FROM crawl_logs
                WHERE channel_id IS NOT NULL AND layer='L3' AND status='success'
            )
            """
        else:
            sql += " AND ch.channel_id=%s"
            params.append(channel_id)

        if TEST_CHANNELS is None:
            cur.execute(sql, params)
        else:
            cur.execute(sql + " LIMIT %s", params + [TEST_CHANNELS])
        channels = cur.fetchall()
    conn.close()

    mode = "테스트 모드" if TEST_CHANNELS else "전체 실행 모드"
    print(f"[{mode}] 수집 대상 남은 채널: {len(channels)}개 (병렬 {L3_WORKERS}개)")

    sem = asyncio.Semaphore(L3_WORKERS)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        tasks = [
            process_channel(browser, sem, ch_id, nick, url)
            for ch_id, nick, url in channels
        ]
        await asyncio.gather(*tasks)
        await browser.close()

    print("\n모든 작업이 완료되었습니다.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--channel",
        type=int,
        help="Run L3 for one channel only"
    )
    args = parser.parse_args()
    asyncio.run(main(channel_id=args.channel))