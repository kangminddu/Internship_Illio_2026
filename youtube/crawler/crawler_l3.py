"""
crawler_l3.py (v2) — 댓글 수집 (Playwright)

기존 버전에서 수정한 문제:
  1) [침묵 오염] Google sorry/CAPTCHA 차단 페이지를 '댓글 0개'로 오인해
     빈 데이터를 success로 쌓던 문제 → 차단 감지 + 전 워커 백오프 + 재시도,
     반복되면 전체 중단.
  2) [전체 사망] gather 기본 동작으로 채널 1건 예외가 전체 런을 죽이던 문제
     → 채널별 예외 격리 + failed 로그.
  3) [허위 성공] 영상 전부 실패해도 success 기록 → 절반 이상 성공했을 때만
     success, 아니면 failed(재시도 대상).
  4) 전역 속도 제어 없음 → AsyncRateController (페이지 로드 간격 + 주기 휴식
     + slow start).
  5) SOCS 쿠키로 consent 페이지 자체를 우회 (클릭 fallback 유지).
  6) duplicate/삭제 채널 제외, Ctrl+C 정상 종료.

config.py 권장 추가:
  L3_MIN_INTERVAL = 1.5     # 페이지 로드 간 전역 최소 간격(초)
  L3_REST_EVERY   = 400     # 영상 페이지 N회마다 휴식
  L3_REST_SECONDS = 1200    # 20분
"""
import argparse
import asyncio
import re
import time
import random
import pymysql
from datetime import datetime, timedelta
from playwright.async_api import async_playwright

from youtube.config import DB
from youtube.config import L3_VIDEOS_PER_CHANNEL as VIDEOS_PER_CHANNEL
from youtube.config import L3_MAX_SCROLLS as MAX_SCROLLS
from youtube.config import L3_COMMENT_LIMIT as COMMENT_LIMIT
from youtube.config import L3_WORKERS

try:
    from youtube.config import L3_MIN_INTERVAL
except ImportError:
    L3_MIN_INTERVAL = 1.5
try:
    from youtube.config import L3_REST_EVERY
except ImportError:
    L3_REST_EVERY = 400
try:
    from youtube.config import L3_REST_SECONDS
except ImportError:
    L3_REST_SECONDS = 1200
try:
    from youtube.config import STOP_ON_429 as STOP_ROUNDS
except ImportError:
    STOP_ROUNDS = 4
BACKOFF_BASE = 120        # 차단 감지 시 첫 대기(초). 브라우저 차단은 무겁게 쉼

TEST_CHANNELS = None

counter = {"done": 0, "ok": 0, "fail": 0, "blocked": 0, "comments": 0}
stop_event = asyncio.Event()


# ==========================================
# Async 전역 속도 제어 (rate_control.py의 asyncio 버전)
# ==========================================
class AsyncRateController:
    def __init__(self, min_interval, backoff_base,
                 rest_every=0, rest_seconds=0, warmup_count=20, name="L3"):
        self.min_interval = min_interval
        self.backoff_base = backoff_base
        self.rest_every = rest_every
        self.rest_seconds = rest_seconds
        self.warmup_count = warmup_count
        self.name = name
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self._pause_until = 0.0
        self._backoff_level = 0
        self._warmup_left = 0
        self._acquired = 0

    async def acquire(self):
        while not stop_event.is_set():
            async with self._lock:
                now = time.time()
                wait_pause = self._pause_until - now
                if wait_pause <= 0:
                    wait_slot = self._next_at - now
                    if wait_slot <= 0:
                        interval = self.min_interval
                        if self._warmup_left > 0:
                            interval *= 2
                            self._warmup_left -= 1
                        self._next_at = now + interval + random.uniform(
                            0, interval * 0.3)
                        self._acquired += 1
                        if (self.rest_every and
                                self._acquired % self.rest_every == 0):
                            self._pause_until = now + self.rest_seconds
                            self._warmup_left = self.warmup_count
                            resume = time.strftime(
                                "%H:%M", time.localtime(now + self.rest_seconds))
                            print(f"\n😴 [{self.name}] 페이지 {self._acquired:,}회 "
                                  f"— {self.rest_seconds // 60}분 휴식 (재개 {resume})")
                        return True
                    sleep_for = wait_slot
                else:
                    sleep_for = min(wait_pause, 5.0)
            await asyncio.sleep(min(sleep_for, 5.0))
        return False

    async def report_blocked(self):
        async with self._lock:
            now = time.time()
            if now >= self._pause_until:
                self._backoff_level += 1
                pause = self.backoff_base * (2 ** (self._backoff_level - 1))
                self._pause_until = now + pause
                self._warmup_left = self.warmup_count
                print(f"\n⛔ [{self.name}] 차단 페이지 감지 → 전 워커 "
                      f"{int(pause)}초 정지 (라운드 {self._backoff_level}/{STOP_ROUNDS})")
            return self._backoff_level

    async def report_success(self):
        async with self._lock:
            if self._backoff_level:
                print(f"✅ [{self.name}] 정상 페이지 재개 — 백오프 리셋")
            self._backoff_level = 0

    @property
    def total(self):
        return self._acquired


rc = AsyncRateController(L3_MIN_INTERVAL, BACKOFF_BASE,
                         rest_every=L3_REST_EVERY, rest_seconds=L3_REST_SECONDS)


# ==========================================
# 유틸리티
# ==========================================
def parse_relative_date(text, now=None):
    if now is None:
        now = datetime.now()
    if not text:
        return None
    m = re.search(r"(\d+)\s*(초|분|시간|일|주|개월|년)", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"초": 0, "분": 0, "시간": 0, "일": 1, "주": 7, "개월": 30, "년": 365}
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


async def block_resources(route):
    if route.request.resource_type in ["image", "media", "font"]:
        await route.abort()
    else:
        await route.continue_()


def is_blocked_page(url, title):
    """Google sorry/CAPTCHA 차단 페이지 감지."""
    u = (url or "").lower()
    t = (title or "").lower()
    return ("google.com/sorry" in u or "/sorry/" in u
            or "unusual traffic" in t or "비정상적인 트래픽" in t)


# ==========================================
# 크롤링 코어
# ==========================================
async def scrape_comments(page, video_id):
    """영상 1개 댓글 긁기. 반환 (payload 리스트, 'blocked'|None)"""
    seen = []
    _bg_tasks = set()
    def on_resp(response):
        if "youtubei/v1/next" in response.url:
            t = asyncio.create_task(_grab(response, seen))
            _bg_tasks.add(t)
            t.add_done_callback(_bg_tasks.discard)

    async def _grab(response, seen):
        try:
            seen.append(await response.json())
        except Exception:
            pass

    page.on("response", on_resp)
    try:
        await page.goto(f"https://www.youtube.com/watch?v={video_id}&hl=ko&gl=KR")
        await page.wait_for_timeout(800)

        # ── 차단 페이지 감지 (기존: 댓글 0으로 오인하던 지점) ──
        if is_blocked_page(page.url, await page.title()):
            return [], "blocked"

        try:
            await page.click("button[aria-label*='모두 수락']", timeout=2000)
        except Exception:
            pass

        await page.evaluate("window.scrollTo(0, 600)")
        await page.wait_for_timeout(800)

        comments = {}
        last, stable = -1, 0
        processed = 0
        for i in range(MAX_SCROLLS):
            if stop_event.is_set():
                break
            await page.evaluate("window.scrollBy(0, 800)")
            await page.wait_for_timeout(800)
            for data in seen[processed:]:
                for pl in find_payloads(data):
                    cid = pl.get("properties", {}).get("commentId", "")
                    if cid:
                        comments[cid] = pl
            processed = len(seen)
            if len(comments) >= COMMENT_LIMIT:
                break
            if len(comments) == last and len(comments) > 0:
                stable += 1
                if stable >= 3:
                    break
            elif len(comments) == 0 and i >= 5:
                break
            else:
                stable = 0
            last = len(comments)
        return list(comments.values()), None
    finally:
        page.remove_listener("response", on_resp)


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

            cur.execute("""
                INSERT INTO fans (platform, external_author_id,
                                  first_seen_at, last_seen_at)
                VALUES ('youtube', %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    last_seen_at = VALUES(last_seen_at),
                    fan_id = LAST_INSERT_ID(fan_id)
            """, (uc, now, now))
            fan_id = cur.lastrowid

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


def log_channel(conn, channel_id, url, status, error_type=None, detail=None):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO crawl_logs
              (channel_id, target_url, layer, status, http_status,
               error_type, error_detail)
            VALUES (%s, %s, 'L3', %s, %s, %s, %s)
        """, (channel_id, url, status, 200 if status == "success" else None,
              error_type, (detail or "")[:500] or None))


# ==========================================
# 채널 1개 처리
# ==========================================
async def process_channel(browser, sem, channel_id, nickname, channel_url):
    if stop_event.is_set():
        return
    async with sem:
        if stop_event.is_set():
            return
        conn = pymysql.connect(**DB, autocommit=True)
        context = None
        safe_url = channel_url or f"channel_{channel_id}"
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT content_id, external_id, content_type
                    FROM contents
                    WHERE channel_id=%s AND content_type IN ('video','shorts')
                    ORDER BY published_at DESC, content_id DESC
                    LIMIT %s
                """, (channel_id, VIDEOS_PER_CHANNEL))
                videos = cur.fetchall()
            if not videos:
                log_channel(conn, channel_id, safe_url, "failed",
                            "no_contents", "L2a 미완료 또는 콘텐츠 0건")
                counter["fail"] += 1
                print(f"    [진행중] ch={channel_id} ({nickname}) contents 없음 -> skip")
                return
            
            context = await browser.new_context()
            # consent 페이지 자체를 우회 (클릭 fallback은 scrape 안에 유지)
            await context.add_cookies([{
                "name": "SOCS", "value": "CAI",
                "domain": ".youtube.com", "path": "/"}])
            page = await context.new_page()
            await page.route("**/*", block_resources)

            ch_comments, ok_videos, err_videos = 0, 0, 0
            print(f"\n=== {nickname} (ch={channel_id}) | {len(videos)}개 ===")

            for content_id, video_id, content_type in videos:
                if stop_event.is_set():
                    return                       # 중간 중단 → 기록 없이 재시도 대상

                # ── 차단 백오프 포함 재시도 루프 ──
                payloads = None
                for _ in range(3):
                    if not await rc.acquire():
                        return
                    try:
                        payloads, sig = await scrape_comments(page, video_id)
                    except Exception as e:
                        payloads, sig = None, None
                        print(f"  [{nickname}] {video_id} 에러: {repr(e)[:100]}")
                        try:
                            await page.goto("about:blank")
                        except Exception:
                            pass
                        break                    # 페이지 에러는 이 영상만 실패
                    if sig == "blocked":
                        counter["blocked"] += 1
                        if await rc.report_blocked() >= STOP_ROUNDS:
                            print("\n🛑 차단 지속 — 전체 중단. 수 시간 뒤 재실행 "
                                  "권장 (완주 채널은 자동 skip).")
                            stop_event.set()
                            return
                        continue                 # 백오프 후 같은 영상 재시도
                    await rc.report_success()
                    break

                if payloads is None or sig == "blocked":
                    err_videos += 1
                    continue
                n = save_comments(conn, content_id, payloads)
                ch_comments += n
                ok_videos += 1
                print(f"  [{nickname}] {video_id} | 댓글 {n}개")

            # ── 정직한 성공 판정: 절반 이상 성공했을 때만 success ──
            if videos and ok_videos * 2 < len(videos):
                log_channel(conn, channel_id, safe_url, "failed",
                            "too_many_video_errors",
                            f"ok={ok_videos}/{len(videos)}")
                counter["fail"] += 1
                print(f"  → [실패 처리] {nickname} ok={ok_videos}/{len(videos)}")
            else:
                log_channel(conn, channel_id, safe_url, "success")
                counter["ok"] += 1
                counter["comments"] += ch_comments
                print(f"  → [완료] {nickname} | 영상 {ok_videos}/{len(videos)} "
                      f"| 댓글 {ch_comments}개")

        except Exception as e:
            # 채널 단위 예외 격리 — 전체 런을 죽이지 않는다
            print(f"  ⚠️ 채널 오류 ch={channel_id}: {repr(e)[:140]}")
            try:
                log_channel(conn, channel_id, safe_url, "failed",
                            "worker_error", repr(e))
            except Exception:
                pass
            counter["fail"] += 1
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass
            conn.close()
            counter["done"] += 1
            if counter["done"] % 20 == 0:
                print(f"  [{counter['done']}] ok={counter['ok']} "
                      f"fail={counter['fail']} blocked={counter['blocked']} "
                      f"comments={counter['comments']:,} pages={rc.total:,}")


# ==========================================
# 메인
# ==========================================
async def main(channel_id=None):
    conn = pymysql.connect(**DB, autocommit=True)
    with conn.cursor() as cur:
        sql = """
        SELECT ch.channel_id, cr.nickname, ch.channel_url_normalized
        FROM channels ch
        JOIN creators cr ON ch.creator_id = cr.creator_id
        WHERE ch.platform='youtube'
          AND ch.channel_existence_status='normal'
          AND ch.channel_id_status <> 'duplicate'
          AND ch.channel_activity_status IN ('active', 'low_active')
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

    est_pages = len(channels) * VIDEOS_PER_CHANNEL
    print(f"L3 대상 채널 {len(channels)}개 (페이지 ≈{est_pages:,}) | "
          f"병렬 {L3_WORKERS} | INTERVAL={L3_MIN_INTERVAL}s | "
          f"{L3_REST_EVERY}페이지당 {L3_REST_SECONDS//60}분 휴식")
    print("무인 실행 팁: caffeinate -i python -m youtube.main --l3\n")

    sem = asyncio.Semaphore(L3_WORKERS)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        tasks = [process_channel(browser, sem, ch_id, nick, url)
                 for ch_id, nick, url in channels]
        # return_exceptions=True: 개별 태스크 예외가 전체를 취소하지 않게
        await asyncio.gather(*tasks, return_exceptions=True)
        await browser.close()

    print(f"\n=== L3 완료: ok={counter['ok']} fail={counter['fail']} "
          f"blocked={counter['blocked']} comments={counter['comments']:,} ===")
    if stop_event.is_set():
        print("⚠️ 차단으로 중단됨. 완주 채널은 skip되므로 재실행하면 이어서 갑니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", type=int, help="Run L3 for one channel only")
    args = parser.parse_args()
    try:
        asyncio.run(main(channel_id=args.channel))
    except KeyboardInterrupt:
        print("\n⏹️  중단됨. 완주 채널은 skip되므로 재실행하면 이어서 갑니다.")