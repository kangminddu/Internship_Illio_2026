# tiktok/steps/l2.py
"""
틱톡 L2 — 영상 목록 수집 + 활동성 판정

유튜브 L2a/L2b와 결정적으로 다른 점
------
① 한 단계로 끝난다.
   유튜브는 목록(L2a)과 상세(L2b)를 나눠야 했다. 목록 페이지가
   좋아요·댓글수를 안 주고, 게시일도 "3개월 전" 상대시간뿐이었기 때문.
   틱톡 /api/post/item_list/는 한 응답에 전부 준다:
       createTime(유닉스 초), playCount, diggCount, commentCount, isAd
   → published_is_approx=0으로 확정 저장. L2b가 필요 없다.

② 활동성을 여기서 확정한다.
   유튜브는 쇼츠 게시일이 L2b에서만 나와서 잠정 판정 후 backfill로
   재판정하는 2단계 구조가 됐다. 틱톡은 정확한 시각을 바로 받으므로
   수집하면서 그 자리에서 확정할 수 있다. → backfill 단계가 없다.

③ 수집 방식이 HTML 파싱이 아니라 XHR 가로채기다.
   page.on("response")로 /api/post/item_list/ 응답을 주워 담는다.
   스크롤하면 브라우저가 알아서 API를 부르고, 우리는 결과만 받는다.

④ CAPTCHA를 사람이 푼다.
   감지하면 최대 30분 대기하며 5초마다 해제 여부를 확인한다.
   그래서 HEADLESS=False이고 자리를 지켜야 한다.
   (실제로는 5~15초 만에 자동 해제되는 경우가 많았다)
"""
import time
import random
import asyncio
from time import monotonic
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright
from tiktok.antibot import browser, behavior
from tiktok import config
from tiktok import parser

try:
    import pymysql
except ImportError:
    pymysql = None


# -------------------------------------------------------
# 예외
# -------------------------------------------------------

class CaptchaDetected(Exception):
    """CAPTCHA 감지 시 사용. 일반 실패와 구분.

    별도 예외로 만든 이유: 처리 방식이 완전히 다르다.
      일반 실패 → 로그 남기고 다음 채널로
      CAPTCHA   → 전체 중단 (계속 진행해도 또 막힐 가능성이 크다)
    """
    pass


# ---------- DB ----------

INSERT_CONTENT = (
    "INSERT INTO contents "
    "(channel_id, external_id, content_type, is_paid_promotion, "
    " published_at, published_is_approx, duration_sec, caption_text, "
    " category, collected_at) "
    "VALUES (%s,%s,'tiktok',%s,%s,0,%s,%s,%s,%s) "
    #                              ↑ published_is_approx=0 (확정값)
    #   유튜브 L2a는 1(근사값)로 넣고 L2b가 0으로 바꾼다.
    #   틱톡은 처음부터 정확한 값이라 0이다.
    "ON DUPLICATE KEY UPDATE "
    " caption_text=VALUES(caption_text), "
    " duration_sec=VALUES(duration_sec), "
    " is_paid_promotion=VALUES(is_paid_promotion), "
    " category=VALUES(category), "
    " published_at=VALUES(published_at), "
    " content_id=LAST_INSERT_ID(content_id)"
    #  ↑ ON DUPLICATE일 때도 lastrowid로 기존 content_id를 받기 위한 트릭.
    #    없으면 중복 시 lastrowid가 0이라 별도 SELECT가 필요하다.
)

INSERT_CSNAP = (
    "INSERT IGNORE INTO content_snapshots "
    "(content_id, captured_at, view_count, like_count, comment_count) "
    "VALUES (%s,%s,%s,%s,%s)"
    #  INSERT IGNORE: 같은 (content_id, captured_at)이면 무시한다.
    #  스냅샷은 시계열이므로 덮어쓰지 않고 그냥 넘긴다.
)

INSERT_CHSNAP = (
    "INSERT INTO channel_snapshots (channel_id, captured_at, follower_count) "
    "VALUES (%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE follower_count=VALUES(follower_count)"
    #  팔로워 수를 L2에서도 갱신한다.
    #  영상 목록 응답의 authorStats에 딸려 오므로 추가 요청이 없다.
)

INSERT_LOG = (
    "INSERT INTO crawl_logs "
    "(channel_id, target_url, layer, status, error_type, error_detail, attempted_at) "
    "VALUES (%s,%s,'L2',%s,%s,%s,%s)"
)


# -------------------------------------------------------
# CAPTCHA 감지 (L3에서 이식 — shadow DOM 관통)
#
# ⚠️ antibot/captcha.py에 더 정교한 감지기가 있는데 안 쓴다.
#    L2/L3가 각자 복사본을 갖고 있다. (통합 대상)
#    다만 한국어 마커는 이쪽에만 있다.
# -------------------------------------------------------

CAPTCHA_SELECTORS = [
    "div.captcha_verify_container",
    ".secsdk-captcha-drag-icon",     # secsdk = 틱톡 보안 SDK
    "div[id*='captcha']",
    "iframe[src*='captcha']",
]

CAPTCHA_TEXT_MARKERS = [
    "complete the puzzle",
    "drag the puzzle",
    "verify to continue",
    "슬라이더를 드래그",      # ← 실측으로 확인한 한국어 문구
    "퍼즐을 맞추",
    "드래그하여 퍼즐",
    "인증하려면",
    "어떤 영상을 시청",
    "보안 확인",
]

# CAPTCHA 대기 설정
CAPTCHA_MAX_WAIT = 1800   # 사람이 푸는 대기 한도(초) = 30분
CAPTCHA_POLL = 5          # 해제 확인 간격(초)


def logline(msg):
    """타임스탬프 붙은 로그. flush=True로 즉시 출력.
    (파일 리다이렉트 시 버퍼링되면 진행 상황을 실시간으로 못 본다)"""
    ts = datetime.now().strftime("%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


async def detect_captcha(page):
    """CAPTCHA 감지. 세 단계로 시도한다 (빠르고 확실한 것부터).

    ★ shadow DOM 탐색이 필요한 이유:
      틱톡 CAPTCHA는 iframe이 아니라 메인 문서의 shadow DOM에 뜬다.
      page.content()나 문자열 검색으로는 shadow root 내부를 볼 수 없다.
      → JS로 재귀 탐색해야 한다.

    그리고 '존재'가 아니라 '보이는지'를 검사한다.
    컨테이너가 숨겨진 채 미리 마운트되는 경우가 있어 오탐이 난다.
    """
    # 1) 셀렉터 빠른 경로 (Playwright locator는 open shadow root를 관통한다)
    for sel in CAPTCHA_SELECTORS:
        try:
            if await page.locator(sel).first.is_visible():
                return True
        except Exception:
            pass

    # 2) shadow DOM / overlay 재귀 탐색 — 셀렉터 목록이 낡았을 때의 안전망
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
                        // ↑ 크기가 0이면 숨겨진 것. 보이는 것만 인정.
                    }
                    if (el.shadowRoot) walk(el.shadowRoot);   // 재귀
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

    # 3) 폴백: body 텍스트 (shadow 내부는 안 보이지만 최후의 안전망)
    try:
        text = (await page.locator("body").inner_text()).lower()
    except Exception:
        return False
    return any(k.lower() in text for k in CAPTCHA_TEXT_MARKERS)


async def wait_until_solved(page, poll=CAPTCHA_POLL, max_wait=CAPTCHA_MAX_WAIT):
    """CAPTCHA가 사라질 때까지 대기. 사람이 풀기를 기다린다.

    자동 해결을 시도하지 않는 이유: 슬라이드 퍼즐을 프로그램으로 풀면
    그 자체가 봇 신호가 되고, 실패하면 더 강한 차단으로 이어진다.

    monotonic()을 쓰는 이유: 시스템 시각이 바뀌어도(NTP 동기화 등)
    경과 시간 측정이 어긋나지 않는다.
    """
    logline("🚨 CAPTCHA 감지 — 브라우저에서 직접 풀어주세요. (일시정지)")
    start = monotonic()
    while monotonic() - start < max_wait:
        await asyncio.sleep(poll)
        if not await detect_captcha(page):
            waited = int(monotonic() - start)
            logline(f"✅ CAPTCHA 해결 확인 (대기 {waited}초) — 재개")
            return True
    logline("⏱️ CAPTCHA 대기 초과 — 이 채널 실패 처리. 다음 실행 때 Resume.")
    return False


def _dt(unix_sec):
    """유닉스 초 → KST naive datetime.

    tz를 붙여 변환한 뒤 replace(tzinfo=None)로 떼는 이유:
    pymysql은 datetime의 tzinfo를 버리고 숫자만 문자열로 만든다.
    UTC로 넘기면 MySQL(+09:00)이 KST로 해석해 9시간 어긋난다.
    → KST 벽시계 숫자로 만들어서 넘겨야 한다.
    """
    if not unix_sec:
        return None
    return datetime.fromtimestamp(unix_sec, tz=ZoneInfo("Asia/Seoul")).replace(tzinfo=None)


def classify_activity(conn, channel_id):
    """활동성 판정.

    가이드라인: TikTok 수집 기간 최근 3개월 / 최소 15개
                샘플 미달 시 기간을 2배 확장 → 180일
    → 90일 15건 = active, 180일 15건 = low_active

    유튜브(180일/10건)와 기준값이 다른 건 버그가 아니라
    가이드라인이 플랫폼마다 다르게 정했기 때문이다.

    ⚠️ dormant가 없다. 가이드라인의 "1년 이상 미업로드 → 수집 제외"
       규정이 틱톡에는 미구현. (유튜브에만 넣었다)
    """
    now = datetime.now()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                SUM(published_at >= %s) AS cnt_90d,
                SUM(published_at >= %s) AS cnt_180d
            FROM contents
            WHERE channel_id=%s
              AND content_type='tiktok'
              AND published_at IS NOT NULL
        """, (
            now - timedelta(days=90),
            now - timedelta(days=180),
            channel_id,
        ))

        cnt90, cnt180 = cur.fetchone()

    cnt90 = cnt90 or 0      # SUM은 행이 없으면 NULL을 반환한다
    cnt180 = cnt180 or 0

    if cnt90 >= config.L2_MIN_VIDEOS:
        return "active"

    if cnt180 >= config.L2_MIN_VIDEOS:
        return "low_active"

    return "inactive"


def save_l2(conn, channel_id, videos, follower_count, captured_at=None):
    """수집 결과 저장. 영상 + 스냅샷 + 팔로워."""
    if captured_at is None:
        captured_at = datetime.now()
        # 루프 밖에서 한 번 정한다. 같은 배치의 스냅샷은 같은 시각이어야
        # 나중에 "이 시점의 데이터"로 묶어서 볼 수 있다.

    saved = 0
    with conn.cursor() as cur:
        if follower_count is not None:
            cur.execute(INSERT_CHSNAP, (channel_id, captured_at, follower_count))

        for v in videos:
            if not v.get("external_id"):
                continue
            cur.execute(INSERT_CONTENT, (
                channel_id,
                v["external_id"],
                v.get("is_paid_promotion", 0),
                _dt(v.get("published_at")),      # 유닉스 초 → KST datetime
                v.get("duration_sec"),
                v.get("caption_text", ""),
                v.get("category"),
                captured_at,
            ))
            # lastrowid 폴백 (L3 save_comments 패턴):
            # ON DUPLICATE 시 lastrowid가 0일 수 있어, 그때는 직접 조회
            content_id = cur.lastrowid
            if not content_id:
                cur.execute(
                    "SELECT content_id FROM contents "
                    "WHERE channel_id=%s AND external_id=%s AND content_type='tiktok'",
                    (channel_id, v["external_id"]),
                )
                row = cur.fetchone()
                if not row:
                    continue
                content_id = row[0]

            cur.execute(INSERT_CSNAP, (
                content_id, captured_at,
                v.get("view_count"), v.get("like_count"), v.get("comment_count"),
            ))
            saved += 1
    conn.commit()
    return saved


def log_l2(conn, channel_id, url, status, err_type=None, err_detail=None):
    with conn.cursor() as cur:
        cur.execute(INSERT_LOG, (channel_id, url, status,
                                 err_type, err_detail, datetime.now()))
    conn.commit()


# ---------- 대상 조회 ----------

def fetch_targets(conn, limit, resume=True):
    """대상 = L1이 sec_uid를 확보한 채널 중 아직 L2를 안 한 것.

    external_channel_id IS NOT NULL이 곧 "L1 성공"의 증거다.
    (L1이 sec_uid를 여기 저장한다)

    ★ resume 조건에 error_type='empty'가 포함된 이유:
      영상이 정말 0개인 채널을 매번 다시 확인할 필요가 없다.
      status='success' + error_type='empty'로 기록해두고 제외한다.
      (blocked/captcha/exception은 재시도 대상이라 여기 없다)

    --all을 주면 resume=False가 되어 전체를 다시 수집한다.
    """
    sql = """
    SELECT c.channel_id, c.channel_url_normalized
    FROM channels c
    WHERE c.platform='tiktok'
      AND c.channel_url_normalized IS NOT NULL
      AND c.external_channel_id IS NOT NULL
    """

    if resume:
        sql += """
        AND NOT EXISTS (
            SELECT 1
            FROM crawl_logs l
            WHERE l.channel_id = c.channel_id
              AND l.layer='L2'
              AND (
                    l.status='success'
                 OR l.error_type='empty'
              )
        )
        """

    sql += " ORDER BY c.channel_id"

    if limit:
        sql += " LIMIT %d" % int(limit)

    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


# ---------- 크롤 ----------

def _rand_ms(pair, default):
    """config에서 (lo, hi) 범위를 읽어 랜덤 밀리초 반환.
    고정 대기는 기계적 패턴이라 봇으로 탐지되기 쉽다."""
    lo, hi = getattr(config, pair, default)
    return random.randint(int(lo), int(hi))


async def collect_channel(page, url):
    """
    반환: (videos, follower, status_ok)
      status_ok=False → blocked/soft-block (재시도 대상)
    captcha가 끝내 안 풀리면 CaptchaDetected 예외를 던진다.

    동작 원리:
      1) /api/post/item_list/ 응답 리스너를 건다
      2) 페이지를 열고 사람처럼 스크롤한다
      3) 브라우저가 알아서 API를 호출하고, 리스너가 응답을 모은다
      4) 목표 개수(15개)에 도달하거나 바닥에 닿으면 종료

    HTML을 파싱하지 않는다. 렌더링된 DOM은 클래스명이 바뀌면 깨지지만
    API 응답 구조는 훨씬 안정적이다.
    """
    captured = {"videos": [], "follower": None, "status": None, "blocked": False}
    min_videos = getattr(config, "L2_MIN_VIDEOS", 15)
    max_scrolls = 8
    stall_limit = getattr(config, "L2_SCROLL_STALL", 2)

    async def on_response(resp):
        if "/api/post/item_list/" not in resp.url:
            return
        try:
            # content-type 확인: 리다이렉트나 에러 페이지가 같은 URL로
            # 올 수 있어 JSON인지 먼저 본다.
            ctype = resp.headers.get("content-type", "")
            if "application/json" not in ctype:
                return
            payload = await resp.json()
        except Exception:
            return
        videos, cursor, has_more, follower, status_code = \
            parser.parse_item_list(payload)

        # ⭐ 먼저 status 저장
        # 아래 return으로 빠져나가기 전에 저장해야 한다.
        # (목표 개수 도달 시 중간에 return하므로 순서가 중요)
        captured["status"] = status_code

        # status_code가 0이 아니면 틱톡이 거부한 것.
        # HTTP는 200인데 본문에서 거부하는 soft-block 패턴.
        if status_code not in (0, None):
            captured["blocked"] = True

        if follower is not None and captured["follower"] is None:
            captured["follower"] = follower

        seen = {v["external_id"] for v in captured["videos"]}

        for v in videos:
            if v["external_id"] and v["external_id"] not in seen:
                captured["videos"].append(v)
                seen.add(v["external_id"])   # ⭐ 이것도 먼저

                if len(captured["videos"]) >= min_videos:
                    return   # 목표 도달 → 더 안 담는다

    page.on("response", on_response)

    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        print(f"[goto] status={resp.status if resp else None}")
    except Exception as e:
        print(f"[goto] {type(e).__name__}: {e}")
        raise

    # goto 직후 captcha 체크 — 뜨면 풀릴 때까지 대기
    if await detect_captcha(page):
        if not await wait_until_solved(page):
            raise CaptchaDetected()

    # 사람처럼 페이지에 '도착'하는 행동.
    # 바로 스크롤을 시작하면 기계적 패턴이라 탐지되기 쉽다.
    await behavior.arrive(page)          # 잠시 머무르며 마우스 이동
    await behavior.small_scroll(page)    # 조금 훑어보기
    await page.wait_for_timeout(_rand_ms("L2_GOTO_DELAY", (1200, 2200)))

    prev_count = 0
    stall = 0
    for _ in range(max_scrolls):
        if captured["blocked"]:
            break    # 차단당했으면 더 스크롤해도 소용없다

        # 스크롤 루프 각 회차에서 captcha 체크.
        # captcha 때문에 영상이 안 늘어나는데 stall로 오판하는 것을 방지.
        if await detect_captcha(page):
            if not await wait_until_solved(page):
                raise CaptchaDetected()
            # 풀린 후 stall 카운터 초기화 (captcha 대기가 stall로 안 세지게)
            stall = 0
            prev_count = len(captured["videos"])

        vids = captured["videos"]
        # 종료조건 1: 목표 개수 도달
        if len(vids) >= min_videos:
            break
        # 종료조건 2: 스크롤해도 새 영상이 안 늘어남 (바닥 도달)
        # 2회 연속이어야 종료한다. 1회로 하면 로딩이 잠깐 느린 것을
        # 바닥으로 오판한다.
        if len(vids) == prev_count:
            stall += 1
            if stall >= stall_limit:
                break
        else:
            stall = 0
        prev_count = len(vids)
        await behavior.feed_scroll(page)
        await page.wait_for_timeout(_rand_ms("L2_SCROLL_DELAY", (1000, 2200)))

    # status가 None인 경우도 정상으로 본다.
    # (영상이 0개면 API가 아예 안 불릴 수 있다)
    status_ok = not captured["blocked"] and captured["status"] in (0, None)
    return captured["videos"], captured["follower"], status_ok


async def run(channel=None, limit=None, resume=True, **_):
    if pymysql is None:
        raise SystemExit("pymysql 필요: pip install pymysql")

    conn = pymysql.connect(**config.DB)
    try:
        if channel:
            handle = channel.lstrip("@")
            url = f"https://www.tiktok.com/@{handle}"
            # DB의 실제 channel_id를 찾아야 한다.
            # (핸들 문자열을 그대로 쓰면 channel_id(bigint) INSERT에서 1366 에러)
            #
            # ← 실제로 겪은 버그. --channel 모드에서 targets에 핸들 문자열을
            #   넣었더니 save_l2가 그걸 channel_id 컬럼에 INSERT하려다
            #   "Incorrect integer value: 'dalsia819'"로 죽었다.
            #   L1의 --channel은 DB를 안 써서 문제가 없었는데,
            #   L2는 저장 경로를 그대로 타서 드러났다.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT channel_id FROM channels "
                    "WHERE platform='tiktok' AND channel_url_normalized=%s",
                    (url,))
                row = cur.fetchone()
            if not row:
                raise SystemExit(
                    f"DB에 없는 채널입니다: {url}\n"
                    f"먼저 seed/L1을 실행하세요.")
            targets = [(row[0], url)]
        else:
            targets = fetch_targets(conn, limit, resume)
        print("[L2] 대상 채널:", len(targets))
        ok = none = err = blocked = captcha_fail = 0

        async with async_playwright() as p:
            # browser.create_context()는 persistent profile을 연다.
            # L2는 로그인이 필요하다 (L1과 반대).
            pw_browser, context = await browser.create_context(p)

            try:
                # 순차 처리. L1처럼 워커를 여러 개 두지 않는다.
                # CAPTCHA를 사람이 풀어야 하는데 여러 창이 동시에 뜨면
                # 어느 것을 풀어야 할지 알 수 없다.
                for i, (cid, url) in enumerate(targets, 1):
                    page = await browser.new_page(context)
                    try:
                        videos, follower, status_ok = \
                            await collect_channel(page, url)

                        if not status_ok:
                            blocked += 1
                            log_l2(conn, cid, url, "failed",
                                   "blocked", "empty/soft-block")
                            print("  [%d/%d] BLOCK %s" % (i, len(targets), url))
                            # 서킷 브레이커: 연속 차단이면 중단.
                            # 계속 때리면 차단이 심해진다.
                            if blocked >= getattr(config, "STOP_ON_BLOCK", 3):
                                print("[L2] 연속 차단 감지 -> 중단")
                                break
                            continue

                        if not videos:
                            # ★ 영상 0개를 success로 기록한다.
                            #   status_ok를 이미 확인했으므로 "차단이 아니라
                            #   정말 빈 채널"이 확정된 상태다.
                            #   error_type='empty'로 남겨 resume이 제외한다.
                            #   (차단이면 위에서 failed로 빠졌다)
                            none += 1
                            log_l2(conn, cid, url, "success", "empty", None)
                            print("  [%d/%d] NONE  %s" % (i, len(targets), url))
                            continue

                        n = save_l2(conn, cid, videos, follower)
                        # 수집 직후 활동성 확정 판정.
                        # 유튜브처럼 backfill로 미룰 필요가 없다.
                        activity = classify_activity(conn, cid)

                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE channels
                                SET channel_activity_status=%s
                                WHERE channel_id=%s
                            """, (activity, cid))
                        conn.commit()
                        log_l2(conn, cid, url, "success", None, None)
                        ok += 1
                        print("  [%d/%d] OK    %s (v=%d f=%s)"
                              % (i, len(targets), url, n, follower))
                        blocked = 0
                        # ↑ 성공하면 연속 차단 카운터 리셋.
                        # ⚠️ blocked가 '연속 카운터'와 '누적 통계' 두 용도로
                        #    쓰여서, 마지막이 성공이면 최종 요약의 BLOCK이 0으로
                        #    찍힌다. 서킷 브레이커는 정상 동작하지만 통계는 틀린다.

                    except CaptchaDetected:
                        # captcha를 끝내 못 풀었음 → failed로 기록.
                        # (success,empty 아님! resume이 다시 잡도록)
                        captcha_fail += 1
                        log_l2(conn, cid, url, "failed",
                               "captcha", "captcha not solved")
                        print("  [%d/%d] CAPTCHA-FAIL %s" % (i, len(targets), url))
                        # captcha 못 풀면 계속 진행해도 또 막힐 가능성 큼 → 중단
                        print("[L2] CAPTCHA 미해결 -> 중단 (resume 가능)")
                        break

                    except Exception as e:
                        # 채널 1건 예외로 전체가 죽지 않게 격리
                        err += 1
                        log_l2(conn, cid, url, "failed", "exception", str(e)[:200])
                        print("  [%d/%d] ERR   %s | %s" % (i, len(targets), url, e))
                    finally:
                        await page.close()

                    # 채널 간 랜덤 대기 (1.5~3.5초)
                    lo, hi = getattr(config, "L2_CHANNEL_GAP", (1.5, 3.5))
                    await behavior.human_pause(lo, hi)
            finally:
                try:
                    await pw_browser.close()
                    # close()로 정상 종료해야 쿠키가 디스크에 flush된다.
                except Exception:
                    pass

        print("[L2] 완료: OK=%d NONE=%d BLOCK=%d ERR=%d CAPTCHA_FAIL=%d"
              % (ok, none, blocked, err, captcha_fail))
    finally:
        conn.close()