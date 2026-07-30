# tiktok/steps/l3.py
"""
틱톡 L3 — 댓글 수집

무엇을 하는가
------
L2가 수집한 영상마다 페이지를 열고, 댓글 버튼을 눌러
/api/comment/list/ 응답을 가로채 댓글과 작성자를 저장한다.

가이드라인의 '코어 팬덤 모수 측정'을 위한 단계다.
fans 테이블에 작성자를 쌓아야 "같은 사람이 이 채널의 영상 몇 개에
댓글을 달았나"를 셀 수 있고, 그게 중복률/고정댓글러 지표가 된다.

★ 이 파일은 파이프라인에서 가장 정교한 페이싱을 갖고 있다
------
영상 하나당 페이지를 여는 작업이라 요청량이 가장 많고,
브라우저를 오래 띄워두므로 봇 판정을 받기 쉽다. 그래서:

    영상마다 랜덤 대기   4개 밴드를 가중 선택 (3~50초)
    40개마다 긴 휴식     50~80초
    채널 바뀌면 휴식     12~25초
    behavior.random_dwell  매 영상 다른 행동을 다른 순서로
    page 150개마다 재생성  메모리 누수 방지
    DB 커넥션 ping       장시간 실행 중 끊김 대비

유튜브 L3의 rate_control이 '요청 간격'에 집중한다면,
여기는 '사람처럼 보이기'에 집중한다. 틱톡이 행동 패턴까지 보기 때문.
"""
from zoneinfo import ZoneInfo
import asyncio
import random
from time import monotonic
from datetime import datetime, timezone

from playwright.async_api import async_playwright
from tiktok import config, parser
from tiktok.antibot import browser, behavior

try:
    import pymysql
except ImportError:
    pymysql = None


# -------------------------------------------------------
# 예외
# -------------------------------------------------------

class CaptchaDetected(Exception):
    """CAPTCHA 감지 시 사용. 일반 실패와 구분해 처리한다.

    L2와 달리 여기서는 CAPTCHA를 풀면 '같은 영상을 재시도'한다.
    (L2는 채널 단위라 중단하는 편이 나았지만,
     L3는 영상 단위라 하나만 다시 하면 된다)
    """
    pass


# -------------------------------------------------------
# SQL
# -------------------------------------------------------

INSERT_FAN = """
INSERT INTO fans(platform, external_author_id, last_seen_at)
VALUES('tiktok', %s, NOW())
ON DUPLICATE KEY UPDATE
    last_seen_at = NOW(),
    fan_id = LAST_INSERT_ID(fan_id)
"""
# LAST_INSERT_ID(fan_id) 트릭: ON DUPLICATE 경로에서도
# cur.lastrowid로 기존 fan_id를 받기 위함.

SELECT_FAN = """
SELECT fan_id FROM fans
WHERE platform='tiktok' AND external_author_id=%s
"""

INSERT_COMMENT = """
INSERT IGNORE INTO comments(
    content_id, fan_id, external_comment_id,
    author_display_name, comment_text, like_count, published_at
)
VALUES(%s,%s,%s,%s,%s,%s,%s)
"""
# INSERT IGNORE: 같은 댓글을 다시 수집해도 무시한다.
# 유튜브는 ON DUPLICATE로 좋아요 수를 갱신하는데,
# 여기는 한 번 수집하면 끝(가이드라인: L3는 1회 수집).

INSERT_LOG = """
INSERT INTO crawl_logs(
    channel_id, target_url, layer, status,
    error_type, error_detail, attempted_at
)
VALUES(%s,%s,'L3',%s,%s,%s,%s)
"""

# resume: 이미 성공한 영상 제외 (external_id 기반 매칭).
# 정렬: 채널별로 묶고, 채널 안에서는 최신순.
#       → 같은 채널 영상이 연속 처리되어 "채널 변경 휴식"이 자연스럽게 동작.
#
# ★ 유튜브 L3와 단위가 다르다.
#   유튜브 : 채널 단위 (한 채널의 영상 10개를 처리하고 채널을 success로 기록)
#   틱톡   : 영상 단위 (영상 하나하나가 success/failed)
#   → 틱톡이 더 세밀하다. 채널 중간에 끊겨도 처리한 영상은 안 날아간다.
#
# ⚠️ target_url LIKE CONCAT('%/video/', external_id)는 인덱스를 못 탄다.
#    crawl_logs가 커지면 느려진다. content_id를 로그에 직접 넣는 게 맞다.
SELECT_TARGETS = """
SELECT
    ct.content_id,
    ct.external_id,
    ch.channel_id,
    ch.channel_url_normalized
FROM contents ct
JOIN channels ch
    ON ct.channel_id = ch.channel_id
WHERE ch.platform='tiktok'
  AND ct.content_type='tiktok'
  AND ch.channel_activity_status IN ('active','low_active')
  -- ↑ 활동성으로 대상을 좁힌다. 가장 비싼 단계라
  --   지표 계산에 실제로 쓰이는 채널만 처리한다.
  --   (유튜브 L3에도 같은 필터가 있다)
  AND NOT EXISTS (
        SELECT 1
        FROM crawl_logs l
        WHERE l.layer='L3'
          AND l.status='success'
          AND l.target_url LIKE CONCAT('%/video/', ct.external_id)
  )
ORDER BY
    ch.channel_id,
    ct.published_at DESC,
    ct.content_id DESC
"""


# -------------------------------------------------------
# CAPTCHA 감지 설정 (L2와 동일 — 복사본)
# -------------------------------------------------------

CAPTCHA_SELECTORS = [
    "div.captcha_verify_container",
    ".secsdk-captcha-drag-icon",
    "div[id*='captcha']",
    "iframe[src*='captcha']",
]

CAPTCHA_TEXT_MARKERS = [
    "complete the puzzle",
    "drag the puzzle",
    "verify to continue",
    "슬라이더를 드래그",
    "퍼즐을 맞추",
    "드래그하여 퍼즐",
    "인증하려면",
    "어떤 영상을 시청",
    "보안 확인",
]


# -------------------------------------------------------
# 운영 파라미터 (튜닝용 — 여기만 고치면 됨)
#
# 상수를 한 곳에 모은 이유: 밴 위험을 조절할 때 코드를 뒤지지 않고
# 여기만 보면 된다. 실제로 여러 번 조정했다(주석의 "50→40" 등).
# -------------------------------------------------------

# page / 커넥션
PAGE_RECYCLE_EVERY = 150         # 이 개수마다 page 재생성 (메모리 누수 방지)
                                 # 브라우저 탭을 오래 쓰면 DOM/리스너가 쌓인다.
                                 # 유튜브 L3에는 없는 장치.

# 댓글 API 대기
COMMENT_IDLE_TIMEOUT = 2.0       # 마지막 응답 후 이 시간(초) 조용하면 종료
                                 # 고정 대기 대신 'idle 감지'를 쓴다.
                                 # 응답이 계속 오면 계속 기다리고,
                                 # 멎으면 즉시 넘어간다.
COMMENT_MAX_WAIT = 25.0          # 버튼 클릭 후 최대 대기(초). 무한 대기 방지.

# 재시도
GOTO_RETRY = 3                   # page.goto 재시도 횟수
GOTO_RETRY_SLEEP = (8, 15)       # goto 재시도 사이 대기(초) 범위
NO_RESPONSE_RETRY = 3            # 댓글 API 미수신 시 같은 영상 재시도 횟수
COMMENT_BUTTON_TRIES = 5         # 댓글 버튼 탐색 반복 횟수

# CAPTCHA
CAPTCHA_MAX_WAIT = 1800          # 사람이 CAPTCHA 푸는 대기 한도(초)
CAPTCHA_POLL = 5                 # CAPTCHA 해제 확인 간격(초)

# 페이싱 (부하 관리)
REST_EVERY = 40                  # 이 개수마다 긴 휴식 (50→40, 더 자주 쉼)
REST_SLEEP = (50, 80)            # 긴 휴식 시간(초) 범위
CHANNEL_CHANGE_SLEEP = (12, 25)  # 채널이 바뀔 때 쉬는 시간(초) — 늘림
                                 # 사람은 한 채널을 보다가 다른 채널로
                                 # 넘어갈 때 텀이 생긴다.
PAGE_SETTLE_SLEEP = (2000, 4500) # goto 후 페이지 안정 대기(ms)
# 영상마다 sleep: 밴드를 가중 선택. 대부분 짧게, 가끔 아주 길게(사람처럼).
# 밴드마다 뽑힐 확률을 다르게 줘서 분포를 넓힘.
#
# ★ 단순 uniform(3, 50)과 다르다.
#   uniform은 모든 값이 균등해서 평균 근처가 두꺼운 자연스러운 분포가 안 나온다.
#   실제 사람은 대부분 짧게 보고 가끔 오래 본다.
PER_VIDEO_SLEEP_BANDS = [        # (lo, hi, weight)
    (3, 8, 5),      # 짧게 (자주)
    (6, 15, 4),     # 보통
    (12, 28, 2),    # 길게
    (25, 50, 1),    # 가끔 아주 길게 (딴짓하는 척)
]


# -------------------------------------------------------
# DB 저장
# -------------------------------------------------------

def fetch_targets(conn, limit=None):
    sql = SELECT_TARGETS
    if limit:
        sql += " LIMIT %d" % int(limit)
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def save_comments(conn, content_id, comments):
    """댓글 저장 + 팬 등록.

    fans가 핵심이다. external_author_id(틱톡 uid)로 동일인을 식별해야
    "이 사람이 이 채널의 영상 몇 개에 댓글을 달았나"를 셀 수 있다.
    """
    saved = 0
    with conn.cursor() as cur:
        for c in comments:
            external_author_id = c.get("author_id")
            if not external_author_id:
                continue    # 작성자 ID가 없으면 팬 식별 불가 → 버림

            # fan upsert. lastrowid가 0이면 직접 조회(폴백).
            cur.execute(INSERT_FAN, (external_author_id,))
            fan_id = cur.lastrowid
            if not fan_id:
                cur.execute(SELECT_FAN, (external_author_id,))
                row = cur.fetchone()
                if not row:
                    continue
                fan_id = row[0]

            # 댓글 작성일도 유닉스 초. KST naive로 변환.
            published = None
            if c.get("published_at"):
                published = datetime.fromtimestamp(
                    c["published_at"], tz=ZoneInfo("Asia/Seoul")
                ).replace(tzinfo=None)

            cur.execute(INSERT_COMMENT, (
                content_id,
                fan_id,
                c["comment_id"],
                c.get("nickname"),
                c.get("text"),
                c.get("like_count"),
                published,
            ))
            saved += cur.rowcount
            # ↑ rowcount를 더한다. INSERT IGNORE라 중복이면 0이므로
            #   '실제로 새로 저장된 개수'가 집계된다.

    conn.commit()
    return saved


def log_l3(conn, channel_id, url, status, err_type=None, err_detail=None):
    with conn.cursor() as cur:
        cur.execute(INSERT_LOG, (
            channel_id, url, status, err_type, err_detail, datetime.now(),
        ))
    conn.commit()


# -------------------------------------------------------
# 로그 유틸
# -------------------------------------------------------

def logline(msg):
    ts = datetime.now().strftime("%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# -------------------------------------------------------
# CAPTCHA (L2와 동일 로직)
# -------------------------------------------------------

async def detect_captcha(page):
    # 1) 셀렉터 빠른 경로 (open shadow root 자동 관통)
    for sel in CAPTCHA_SELECTORS:
        try:
            if await page.locator(sel).first.is_visible():
                return True
        except Exception:
            pass

    # 2) shadow DOM / overlay 재귀 탐색
    try:
        found = await page.evaluate(r"""
        () => {
            const markers = [
                '슬라이더를 드래그', '퍼즐을 맞추', '드래그하여 퍼즐',
                '어떤 영상을 시청', '보안 확인',
                'complete the puzzle', 'drag the puzzle', 'verify to continue'
            ];
            let hit = false;
            const walk = (root) => {
                if (hit || !root) return;
                const t = root.textContent || '';
                if (markers.some(m => t.includes(m))) { hit = true; return; }
                const els = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of els) {
                    const cn = (typeof el.className === 'string') ? el.className : '';
                    if ((cn.includes('captcha') || cn.includes('verify'))) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0) { hit = true; return; }
                    }
                    if (el.shadowRoot) walk(el.shadowRoot);
                    if (hit) return;
                }
            };
            walk(document);
            return hit;
        }
        """)
        if found:
            return True
    except Exception:
        pass

    # 3) 폴백: body 텍스트
    try:
        text = (await page.locator("body").inner_text()).lower()
    except Exception:
        return False
    return any(k.lower() in text for k in CAPTCHA_TEXT_MARKERS)


async def wait_until_solved(page, poll=CAPTCHA_POLL, max_wait=CAPTCHA_MAX_WAIT):
    logline("🚨 CAPTCHA 감지 — 브라우저에서 직접 퍼즐을 풀어주세요. (일시정지)")
    start = monotonic()
    while monotonic() - start < max_wait:
        await asyncio.sleep(poll)
        if not await detect_captcha(page):
            waited = int(monotonic() - start)
            logline(f"✅ CAPTCHA 해결 확인 (대기 {waited}초) — 재개")
            return True
    logline("⏱️ CAPTCHA 대기 초과 — 안전 종료. 다음 실행 때 Resume.")
    return False


# -------------------------------------------------------
# 댓글 패널 열기 (언어 무관 + 다중 방식)
# -------------------------------------------------------

async def open_comments(page):
    """댓글 패널을 여는 요소를 여러 방식으로 찾아 클릭.
    TikTok UI는 언어(댓글/Comments)·레이아웃(버튼/탭/아이콘)이
    상황마다 달라서, 순서대로 시도한다. 반환: 클릭 성공 여부.

    ★ 왜 이렇게 복잡한가:
      틱톡 UI가 A/B 테스트로 계속 바뀐다. 같은 계정으로 접속해도
      영상마다 레이아웃이 다른 경우가 있다.
      셀렉터 하나에 의존하면 어느 날 갑자기 전부 실패한다.
      → 안정적인 것부터 순서대로 시도하고, 최후에는 전수 탐색.
    """

    async def _click(loc):
        """요소를 사람처럼 클릭한다.

        바로 el.click()을 부르지 않고 마우스를 먼저 움직이는 이유:
        틱톡이 클릭 직전 마우스 궤적을 본다. 커서가 순간이동하면
        봇 신호가 된다. steps로 여러 단계에 나눠 이동시키고,
        중심에서 살짝 어긋난 좌표를 쓴다.
        """
        try:
            if await loc.count() == 0:
                return False
            el = loc.first
            if not await el.is_visible():
                return False
            box = await el.bounding_box()
            if box:
                await page.mouse.move(
                    box["x"] + box["width"] / 2 + random.uniform(-4, 4),
                    box["y"] + box["height"] / 2 + random.uniform(-4, 4),
                    steps=random.randint(8, 18),
                )
            await asyncio.sleep(random.uniform(0.2, 0.8))
            await el.click(timeout=5000)
            return True
        except Exception:
            return False

    # 1) aria-label / data-e2e 기반 (언어 무관도 높음)
    #    data-e2e는 틱톡이 테스트용으로 넣은 속성이라 클래스명보다 안정적이다.
    for sel in [
        "[aria-label*='댓글']",
        "[aria-label*='comment' i]",     # i = 대소문자 무시
        "[data-e2e='comment-icon']",
        "[data-e2e='browse-comment']",
        "[data-e2e*='comment']",
    ]:
        if await _click(page.locator(sel)):
            return True

    # 2) 역할/텍스트 부분매칭 ("댓글", "Comments" + 숫자 붙어도)
    #    get_by_role은 접근성 트리를 보므로 DOM 구조 변경에 덜 민감하다.
    for loc in [
        page.get_by_role("tab", name="Comments"),
        page.get_by_role("tab", name="댓글"),
        page.get_by_text("Comments", exact=False),
        page.get_by_text("댓글", exact=False),
    ]:
        if await _click(loc):
            return True

    # 3) 최후: 모든 button/a/tab 순회하며 텍스트 포함 검사
    #    느리지만 위 방식이 전부 실패했을 때의 안전망.
    try:
        els = await page.locator("button, a, div[role='tab']").all()
        for el in els:
            try:
                t = (await el.inner_text()).strip().lower()
            except Exception:
                continue
            if "댓글" in t or "comment" in t:
                if await _click(el):
                    return True
    except Exception:
        pass

    return False


# -------------------------------------------------------
# 댓글 수집
# -------------------------------------------------------

async def collect_comments(page, video_url):
    """반환: (captured, api_seen)
      api_seen=False → 응답 자체를 못 받음 → 재시도 대상

    ★ api_seen을 따로 반환하는 이유:
      "댓글이 0개"와 "API 응답을 못 받음"은 완전히 다르다.

        댓글 0개    → 정상. 댓글이 꺼져 있거나 원래 없는 영상.
        응답 미수신 → 실패. 버튼을 못 눌렀거나 네트워크 문제.

      구분하지 않으면 실패한 영상이 '댓글 0개'로 success 기록되고
      resume이 영구 제외한다. (이 프로젝트 전반의 원칙)
    """
    captured = []
    state = {"last_response": None, "api_seen": False}

    async def on_response(resp):
        if "/api/comment/list/" not in resp.url:
            return
        try:
            ctype = resp.headers.get("content-type", "")
            if "application/json" not in ctype:
                return
            payload = await resp.json()
        except Exception:
            return

        state["api_seen"] = True             # 응답을 받았다는 증거
        state["last_response"] = monotonic() # idle 감지용 타임스탬프

        comments, cursor, has_more, total, status = \
            parser.parse_comment_list(payload)

        # 중복 제거. 스크롤/재요청으로 같은 댓글이 여러 번 올 수 있다.
        seen = {c["comment_id"] for c in captured}
        for c in comments:
            if c["comment_id"] not in seen:
                captured.append(c)
                seen.add(c["comment_id"])

    page.on("response", on_response)

    try:
        # 이전 영상 상태 섞임 방지 초기화
        # page를 재사용하므로 명시적으로 비워야 한다.
        captured.clear()
        state["api_seen"] = False
        state["last_response"] = None

        # 페이지 이동 (재시도)
        for attempt in range(GOTO_RETRY):
            try:
                await page.goto(
                    video_url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                break
            except Exception:
                if attempt == GOTO_RETRY - 1:
                    raise
                logline(f"goto retry ({attempt + 1}/{GOTO_RETRY})")
                await asyncio.sleep(random.uniform(*GOTO_RETRY_SLEEP))

        await page.wait_for_timeout(random.randint(*PAGE_SETTLE_SLEEP))

        # CAPTCHA 확인
        if await detect_captcha(page):
            raise CaptchaDetected()

        # 사람처럼 어슬렁거리기 — 매 영상마다 다른 행동을 다른 순서로.
        # (기존: 마우스1번+휠 반복으로 항상 동일 → 봇 패턴.
        #  변경: behavior.random_dwell이 확률적으로 조합)
        await behavior.random_dwell(page)

        # 댓글 열기 (언어 무관 다중 방식) — 재시도
        # 중간중간 CAPTCHA를 확인한다. 버튼을 못 찾는 이유가
        # CAPTCHA가 화면을 덮고 있어서일 수 있다.
        clicked = False
        for _ in range(COMMENT_BUTTON_TRIES):
            if await open_comments(page):
                clicked = True
                break
            if await detect_captcha(page):
                raise CaptchaDetected()
            await page.wait_for_timeout(1000)

        if not clicked:
            if await detect_captcha(page):
                raise CaptchaDetected()
            raise RuntimeError("comment button not found")

        # 댓글 API idle timeout 대기
        #
        # 고정 시간이 아니라 '응답이 멎을 때까지' 기다린다.
        # 댓글이 많으면 응답이 계속 오므로 계속 기다리고,
        # 다 왔으면 2초 만에 넘어간다. → 평균 대기 시간이 크게 준다.
        deadline = monotonic() + COMMENT_MAX_WAIT
        if state["last_response"] is None:
            state["last_response"] = monotonic()

        while True:
            await asyncio.sleep(0.3)
            now = monotonic()
            if now - state["last_response"] >= COMMENT_IDLE_TIMEOUT:
                break        # 응답이 멎었다 = 다 받았다
            if now >= deadline:
                break        # 최대 대기 초과 (무한 대기 방지)

        return captured, state["api_seen"]

    finally:
        # 리스너 제거. page를 재사용하므로 안 떼면 다음 영상의 응답이
        # 이전 captured에 섞인다.
        page.remove_listener("response", on_response)


# -------------------------------------------------------
# 메인 실행
# -------------------------------------------------------

async def run(channel=None, limit=None, resume=True, **_):

    if pymysql is None:
        raise SystemExit("pymysql 필요: pip install pymysql")

    conn = pymysql.connect(**config.DB)

    ok = 0
    err = 0
    page = None
    interrupted = False

    try:
        if limit is None:
            limit = getattr(config, "BATCH_LIMIT", None)
        targets = fetch_targets(conn, limit)
        logline(f"[L3] 대상 영상 : {len(targets)}")

        async with async_playwright() as p:

            pw_browser, context = await browser.create_context(p)
            page = await browser.new_page(context)
            prev_channel = None   # 채널 변경 감지용 (루프 밖에서 초기화)

            try:
                for i, (content_id,
                        external_id,
                        channel_id,
                        channel_url) in enumerate(targets, 1):

                    # DB 커넥션 살아있는지 확인 (죽었으면 재연결)
                    #
                    # 수천 개 영상을 몇 시간에 걸쳐 처리한다.
                    # MySQL의 wait_timeout(기본 8시간)이나 네트워크 문제로
                    # 커넥션이 끊길 수 있다. ping으로 확인하고 재연결한다.
                    # (유튜브 L3에는 없는 장치)
                    try:
                        conn.ping()
                    except Exception:
                        try:
                            conn = pymysql.connect(**config.DB)
                            logline("DB 재연결 완료")
                        except Exception as e:
                            logline(f"DB 재연결 실패: {e}")

                    # page 주기적 재생성 (메모리 관리)
                    # 같은 탭으로 수백 페이지를 열면 DOM/리스너/캐시가 쌓여
                    # 브라우저 메모리가 계속 늘어난다.
                    if i > 1 and (i - 1) % PAGE_RECYCLE_EVERY == 0:
                        try:
                            await page.close()
                        except Exception:
                            pass
                        page = await browser.new_page(context)
                        logline(f"♻️ page 재생성 (i={i})")

                    # 채널 변경 휴식 (매 영상마다 체크 — page 재생성과 독립)
                    #
                    # SELECT_TARGETS가 채널별로 정렬해두었기 때문에
                    # 같은 채널 영상이 연속으로 오고, 채널이 바뀌는 지점을
                    # 이렇게 감지할 수 있다.
                    if prev_channel is not None and prev_channel != channel_id:
                        wait = random.uniform(*CHANNEL_CHANGE_SLEEP)
                        logline(f"↪️ 채널 변경 휴식 {wait:.1f}초")
                        await asyncio.sleep(wait)
                    prev_channel = channel_id

                    video_url = f"{channel_url}/video/{external_id}"

                    stop_all = False
                    while True:  # 현재 영상 처리 루프 (CAPTCHA 해결 시 재시도)
                        try:
                            comments = []
                            api_seen = False
                            # API 미수신이면 같은 영상을 다시 시도한다.
                            # 버튼 클릭이 씹히거나 응답이 늦는 경우가 있다.
                            for attempt in range(1, NO_RESPONSE_RETRY + 1):
                                comments, api_seen = await collect_comments(
                                    page, video_url
                                )
                                if api_seen:
                                    if attempt > 1:
                                        logline(
                                            f"[{i}/{len(targets)}] "
                                            f"미수신 재시도 성공 (attempt {attempt})"
                                        )
                                    break
                                logline(
                                    f"[{i}/{len(targets)}] "
                                    f"API 미수신 → 재시도 {attempt}/{NO_RESPONSE_RETRY}"
                                )

                            if not comments and not api_seen:
                                # 3회 모두 응답 없음 → 재시도 대상으로 남김
                                #
                                # ★ failed로 기록한다. success가 아니다.
                                #   댓글 0개가 아니라 '못 받은 것'이므로
                                #   다음 실행에서 다시 시도해야 한다.
                                err += 1
                                log_l3(
                                    conn, channel_id, video_url,
                                    "failed", "no_response",
                                    "comment api not received",
                                )
                                logline(
                                    f"[{i}/{len(targets)}] "
                                    f"MISS (API {NO_RESPONSE_RETRY}회 미수신)"
                                )
                            else:
                                # api_seen=True면 댓글이 0개여도 성공이다.
                                # (댓글이 꺼진 영상이거나 원래 없는 경우)
                                n = save_comments(conn, content_id, comments)
                                log_l3(conn, channel_id, video_url, "success")
                                ok += 1
                                logline(f"[{i}/{len(targets)}] OK comments={n}")

                            break  # 다음 영상

                        except CaptchaDetected:
                            # L2와 다르게 여기서는 풀면 '같은 영상'을 재시도한다.
                            # while True 루프의 continue가 그 역할.
                            solved = await wait_until_solved(page)
                            if solved:
                                logline(
                                    f"[{i}/{len(targets)}] "
                                    f"CAPTCHA 해결 → 같은 영상 재시도"
                                )
                                continue
                            else:
                                stop_all = True    # 못 풀면 전체 중단
                                break

                        except Exception as e:
                            err += 1
                            log_l3(
                                conn, channel_id, video_url,
                                "failed", "exception", str(e)[:200],
                            )
                            logline(f"[{i}/{len(targets)}] ERR {e}")
                            break

                    if stop_all:
                        break

                    # 50개마다 긴 휴식  (상수는 REST_EVERY=40)
                    if i % REST_EVERY == 0:
                        rest = random.uniform(*REST_SLEEP)
                        logline(f"💤 {REST_EVERY}개 처리 — 휴식 {int(rest)}초")
                        await asyncio.sleep(rest)

                    # 영상마다 랜덤 sleep — 밴드를 가중치로 골라 분포를 넓힘
                    # random.choices로 밴드를 뽑고, 그 안에서 다시 uniform.
                    # → 대부분 3~8초, 가끔 25~50초가 나온다.
                    lo, hi, _w = random.choices(
                        PER_VIDEO_SLEEP_BANDS,
                        weights=[b[2] for b in PER_VIDEO_SLEEP_BANDS],
                    )[0]
                    await asyncio.sleep(random.uniform(lo, hi))

            except KeyboardInterrupt:
                interrupted = True
                logline("[L3] KeyboardInterrupt — 안전 종료 중...")

            finally:
                # 브라우저를 반드시 닫는다. 안 닫으면 Chromium 프로세스가
                # 남아서 다음 실행 때 프로필 락에 걸린다.
                try:
                    if page is not None:
                        await page.close()
                except Exception:
                    pass
                try:
                    await pw_browser.close()
                except Exception:
                    pass

    except KeyboardInterrupt:
        # 상위에서 한 번 더 잡는다. async 컨텍스트 밖에서 눌린 경우 대비.
        interrupted = True
        logline("[L3] KeyboardInterrupt (상위) — 종료")

    finally:
        try:
            conn.close()
        except Exception:
            pass

    tag = " (중단됨)" if interrupted else ""
    logline(f"[L3] 완료 OK={ok} ERR={err}{tag}")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n종료되었습니다.")