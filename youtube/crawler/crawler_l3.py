"""
youtube/crawler/crawler_l3.py (v2) — 댓글 수집 (Playwright)

왜 이 단계만 브라우저를 쓰는가
------
L1/L2는 requests로 충분했다. 유튜브가 ytInitialData JSON을 HTML에
통째로 박아주기 때문이다.

그런데 댓글은 초기 HTML에 없다.
스크롤해야 youtubei/v1/next API가 호출되면서 로드되는 구조라,
실제로 스크롤 이벤트를 발생시켜야 한다. → Playwright 필수.

수집 방식도 다르다. HTML을 파싱하는 게 아니라
page.on("response")로 API 응답을 가로챈다.
브라우저가 알아서 API를 부르고, 우리는 그 응답만 주워 담는 방식.

왜 댓글을 모으는가
------
가이드라인의 '팬덤 깊이 측정' 때문이다.
같은 수치라도 '다수의 1회성 참여'와 '소수의 반복 참여'는 다르다.
댓글 작성자 ID를 fans 테이블에 쌓아야
"이 사람이 이 채널의 영상 몇 개에 댓글을 달았나"를 셀 수 있고,
그게 댓글 작성자 중복률 / 고정 댓글러 수 지표가 된다.

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
                          # (L1/L2의 60초보다 2배. 브라우저까지 차단당했다는 건
                          #  이미 상당히 눈에 띄었다는 뜻이라 더 길게 쉰다)

TEST_CHANNELS = None      # 숫자를 넣으면 그만큼만 처리 (개발용)

counter = {"done": 0, "ok": 0, "fail": 0, "blocked": 0, "comments": 0}
stop_event = asyncio.Event()   # threading.Event가 아니다 — asyncio라서


# ==========================================
# Async 전역 속도 제어 (rate_control.py의 asyncio 버전)
#
# rate_control.RateController와 로직이 거의 같지만 별도로 존재한다.
# threading.Lock은 asyncio에서 이벤트 루프를 블로킹하므로
# asyncio.Lock으로 다시 작성해야 했다.
# → 결과적으로 같은 로직이 세 벌(L1 자체 / rate_control / 여기).
#   통합이 필요한 부분.
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
        self._lock = asyncio.Lock()       # ← threading.Lock이 아님
        self._next_at = 0.0
        self._pause_until = 0.0
        self._backoff_level = 0
        self._warmup_left = 0
        self._acquired = 0

    async def acquire(self):
        """페이지 로드 슬롯 확보. 구조는 RateController.acquire와 동일."""
        while not stop_event.is_set():
            async with self._lock:
                now = time.time()
                wait_pause = self._pause_until - now
                if wait_pause <= 0:
                    wait_slot = self._next_at - now
                    if wait_slot <= 0:
                        interval = self.min_interval
                        if self._warmup_left > 0:
                            interval *= 2          # slow start
                            self._warmup_left -= 1
                        self._next_at = now + interval + random.uniform(
                            0, interval * 0.3)     # jitter
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
            # lock 밖에서 잔다. 5초로 쪼개 stop_event를 주기적으로 확인.
            await asyncio.sleep(min(sleep_for, 5.0))
        return False

    async def report_blocked(self):
        """차단 페이지 감지 → 전 워커 공동 백오프.
        429가 아니라 'Google sorry 페이지'가 트리거라는 점이 L1/L2와 다르다.
        브라우저는 HTTP 상태 코드가 아니라 페이지 내용으로 차단을 알린다."""
        async with self._lock:
            now = time.time()
            if now >= self._pause_until:   # 이미 정지 중이면 레벨 안 올림
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
    """댓글 작성일도 상대시간("3일 전")으로만 온다. 근사 날짜로 역산.

    ⚠️ youtube_parser.py에 같은 함수가 있다. import하지 않고 복사한 상태.
    """
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
    """API 응답 JSON에서 commentEntityPayload를 전부 긁어 모은다.

    youtube_parser.find_first와 달리 '전부' 수집한다(첫 개가 아니라).
    한 응답에 댓글 여러 개가 들어있기 때문.
    여기서도 경로를 하드코딩하지 않고 재귀 탐색을 쓴다 — 구조 변경 대비.
    """
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
    """이미지/영상/폰트 차단. 댓글 JSON만 필요하므로 렌더링 리소스는 버린다.
    영상 파일을 실제로 다운로드하면 대역폭과 시간이 크게 낭비된다."""
    if route.request.resource_type in ["image", "media", "font"]:
        await route.abort()
    else:
        await route.continue_()


def is_blocked_page(url, title):
    """Google sorry/CAPTCHA 차단 페이지 감지.

    ★ v2 수정 1번의 핵심.
    차단당하면 유튜브가 google.com/sorry로 리다이렉트하는데,
    그 페이지에는 댓글이 당연히 없다.
    이걸 감지하지 않으면 '댓글 0개'로 오인해서 빈 데이터를 success로 쌓는다.
    그리고 resume이 그 채널을 영구 제외한다.

    URL과 title 둘 다 보는 이유: 리다이렉트 형태가 지역/시점마다 다르다.
    """
    u = (url or "").lower()
    t = (title or "").lower()
    return ("google.com/sorry" in u or "/sorry/" in u
            or "unusual traffic" in t or "비정상적인 트래픽" in t)


# ==========================================
# 크롤링 코어
# ==========================================
async def scrape_comments(page, video_id):
    """영상 1개 댓글 긁기. 반환 (payload 리스트, 'blocked'|None)

    동작 원리:
      1) response 리스너를 걸어둔다 (youtubei/v1/next 응답을 가로챔)
      2) 페이지를 열고 스크롤한다
      3) 브라우저가 알아서 댓글 API를 호출하고, 리스너가 응답을 모은다
      4) 모인 JSON에서 댓글 payload를 추출

    HTML을 파싱하는 게 아니라 '브라우저가 받은 API 응답'을 훔쳐보는 방식.
    렌더링된 DOM을 긁는 것보다 안정적이다(클래스명이 바뀌어도 무관).
    """
    seen = []
    _bg_tasks = set()

    def on_resp(response):
        if "youtubei/v1/next" in response.url:
            # response.json()이 async라 태스크로 띄운다.
            # ⚠️ 태스크 참조를 set에 보관해야 한다.
            #    asyncio는 실행 중인 태스크에 강한 참조를 유지하지 않아서,
            #    변수에 안 담으면 GC가 중간에 수거할 수 있다.
            #    (파이썬 공식 문서에 명시된 함정)
            t = asyncio.create_task(_grab(response, seen))
            _bg_tasks.add(t)
            t.add_done_callback(_bg_tasks.discard)

    async def _grab(response, seen):
        try:
            seen.append(await response.json())
        except Exception:
            pass   # JSON이 아니거나 이미 소비된 응답. 조용히 무시.

    page.on("response", on_resp)
    try:
        await page.goto(f"https://www.youtube.com/watch?v={video_id}&hl=ko&gl=KR")
        await page.wait_for_timeout(800)

        # ── 차단 페이지 감지 (기존: 댓글 0으로 오인하던 지점) ──
        if is_blocked_page(page.url, await page.title()):
            return [], "blocked"

        # consent 배너가 뜨면 클릭. context에 SOCS 쿠키를 심어뒀지만
        # 그래도 나오는 경우가 있어 fallback으로 남겨둔다.
        try:
            await page.click("button[aria-label*='모두 수락']", timeout=2000)
        except Exception:
            pass

        # 댓글 영역까지 스크롤해야 API가 호출되기 시작한다.
        await page.evaluate("window.scrollTo(0, 600)")
        await page.wait_for_timeout(800)

        comments = {}          # commentId → payload (중복 제거)
        last, stable = -1, 0
        processed = 0          # seen에서 이미 파싱한 위치
        for i in range(MAX_SCROLLS):
            if stop_event.is_set():
                break
            await page.evaluate("window.scrollBy(0, 800)")
            await page.wait_for_timeout(800)

            # 증분 파싱: 새로 도착한 응답만 처리한다.
            # seen 전체를 매번 다시 훑으면 find_payloads(JSON 전체 재귀)가
            # 응답이 쌓일수록 O(n²)로 커진다.
            for data in seen[processed:]:
                for pl in find_payloads(data):
                    cid = pl.get("properties", {}).get("commentId", "")
                    if cid:
                        comments[cid] = pl
            processed = len(seen)

            # ── 종료 조건 3가지 ──
            if len(comments) >= COMMENT_LIMIT:
                break                          # 목표치 도달
            if len(comments) == last and len(comments) > 0:
                stable += 1
                if stable >= 3:
                    break                      # 3회 연속 안 늘어남 = 바닥
            elif len(comments) == 0 and i >= 5:
                break                          # 6회 스크롤해도 0개 = 댓글 없는 영상
            else:
                stable = 0
            last = len(comments)
        return list(comments.values()), None
    finally:
        # 리스너를 반드시 제거한다. page를 재사용하므로
        # 안 떼면 다음 영상의 응답이 이전 seen에 섞인다.
        page.remove_listener("response", on_resp)


# ==========================================
# DB 적재
# ==========================================
def save_comments(conn, content_id, payloads):
    """댓글 저장 + 팬 등록.

    fans 테이블이 핵심이다. 같은 사람이 여러 영상에 댓글을 달았는지
    추적해야 '코어 팬덤'을 잴 수 있다.
    external_author_id(유튜브 채널ID)로 동일인을 식별한다.
    """
    now = datetime.now()   # naive = OS 로컬(KST). pymysql이 그대로 넘긴다.
    saved = 0
    with conn.cursor() as cur:
        for pl in payloads:
            props = pl.get("properties", {})
            author = pl.get("author", {})
            toolbar = pl.get("toolbar", {})
            uc = author.get("channelId")
            if not uc:
                continue        # 작성자 ID가 없으면 팬 식별이 불가능 → 버림
            comment_id = props.get("commentId", "")
            text = props.get("content", {}).get("content", "")
            name = author.get("displayName")
            # "좋아요 1.2천" 같은 문자열에서 숫자만 남긴다
            like_raw = toolbar.get("likeCountNotliked", "0")
            like = int(re.sub(r"[^\d]", "", str(like_raw)) or 0)
            pub = parse_relative_date(props.get("publishedTime"), now)

            # LAST_INSERT_ID(fan_id) 트릭:
            # ON DUPLICATE 경로에서도 cur.lastrowid로 기존 fan_id를 받기 위함.
            # 이게 없으면 중복일 때 lastrowid가 0이라 별도 SELECT가 필요하다.
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
    """채널 1개 = 영상 10개의 댓글 수집.

    Semaphore로 동시 실행 채널 수를 제한한다(L3_WORKERS=3).
    채널마다 브라우저 컨텍스트를 새로 만들기 때문에,
    제한이 없으면 컨텍스트 수백 개가 동시에 떠서 메모리가 터진다.
    (컨텍스트당 150~250MB, 4GB 서버에서 3개가 한계)
    """
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
                # tie-breaker(content_id DESC): 쇼츠는 published_at이 전부 NULL이라
                # 동률이 대량 발생한다. 없으면 실행할 때마다 다른 10개가 뽑힌다.
                cur.execute("""
                    SELECT content_id, external_id, content_type
                    FROM contents
                    WHERE channel_id=%s AND content_type IN ('video','shorts')
                    ORDER BY published_at DESC, content_id DESC
                    LIMIT %s
                """, (channel_id, VIDEOS_PER_CHANNEL))
                videos = cur.fetchall()

            # 콘텐츠 없음 가드. L2a 미완료 상태에서 L3를 돌리면
            # 전 채널이 videos=[] → 아래 판정에서 success로 기록되고
            # resume이 영구 제외한다.
            # 브라우저 컨텍스트를 만들기 '전에' 빠져나가는 것도 중요하다
            # (컨텍스트 생성은 무거운 작업).
            if not videos:
                log_channel(conn, channel_id, safe_url, "failed",
                            "no_contents", "L2a 미완료 또는 콘텐츠 0건")
                counter["fail"] += 1
                print(f"    [진행중] ch={channel_id} ({nickname}) contents 없음 -> skip")
                return
            
            context = await browser.new_context()
            # consent 페이지 자체를 우회 (클릭 fallback은 scrape 안에 유지)
            # v2 수정 5번. 매 페이지마다 배너를 클릭하는 것보다
            # 쿠키를 미리 심는 게 빠르고 안정적이다.
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
                        # 페이지가 이상한 상태로 남을 수 있으니 초기화.
                        # (page를 채널 내내 재사용하므로 다음 영상에 영향)
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

                # ★ sig == "blocked" 검사가 중요하다.
                # scrape_comments는 차단 시 None이 아니라 []를 반환한다.
                # payloads is None만 보면 3회 재시도를 전부 차단당했을 때
                # 빈 리스트가 통과해서 ok_videos가 증가하고,
                # 결국 채널이 success로 기록된다(댓글 0건인 채로 영구 제외).
                if payloads is None or sig == "blocked":
                    err_videos += 1
                    continue
                n = save_comments(conn, content_id, payloads)
                ch_comments += n
                ok_videos += 1
                print(f"  [{nickname}] {video_id} | 댓글 {n}개")

            # ── 정직한 성공 판정: 절반 이상 성공했을 때만 success ──
            #
            # v2 수정 3번. 영상 10개 중 2개만 성공했는데 success로 기록하면
            # 나머지 8개는 영영 수집되지 않는다.
            # 절반을 기준으로 삼은 이유: 일부 영상은 댓글이 꺼져 있거나
            # 원래 0개라서 100%를 요구하면 정상 채널도 계속 재시도된다.
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
            # 컨텍스트를 반드시 닫는다. 안 닫으면 브라우저 프로세스가
            # 계속 쌓여 메모리가 고갈된다.
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
        # L3는 활동성으로 대상을 좁힌다 (active/low_active만).
        # 가장 비싼 단계(채널당 브라우저 페이지 10개)라
        # 지표 계산에 실제로 쓰이는 채널만 처리한다.
        # ※ metric의 대상 조건과 동일하게 맞춰뒀다.
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
            # 가이드라인상 L3는 "영상 업로드 후 1회 수집"이라
            # L1/L2와 달리 갱신 주기(attempted_at) 조건이 없다.
            sql += """
            AND ch.channel_id NOT IN (
                SELECT channel_id FROM crawl_logs
                WHERE channel_id IS NOT NULL AND layer='L3' AND status='success'
            )
            """
        else:
            sql += " AND ch.channel_id=%s"      # --channel: 단일 채널 재수집
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
        # 브라우저는 하나만 띄우고 채널마다 context를 만든다.
        # 브라우저 프로세스를 채널마다 새로 띄우면 너무 무겁고,
        # context 하나를 공유하면 쿠키/세션이 섞인다.
        browser = await p.chromium.launch(headless=True)
        tasks = [process_channel(browser, sem, ch_id, nick, url)
                 for ch_id, nick, url in channels]
        # return_exceptions=True: 개별 태스크 예외가 전체를 취소하지 않게
        #
        # v2 수정 2번. 기본값(False)이면 태스크 하나가 예외를 던지는 순간
        # gather가 나머지를 전부 취소한다. 채널 1건 때문에 전체가 죽는다.
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