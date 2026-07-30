# tiktok/steps/l2.py
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
    """CAPTCHA 감지 시 사용. 일반 실패와 구분."""
    pass


# ---------- DB ----------

INSERT_CONTENT = (
    "INSERT INTO contents "
    "(channel_id, external_id, content_type, is_paid_promotion, "
    " published_at, published_is_approx, duration_sec, caption_text, "
    " category, collected_at) "
    "VALUES (%s,%s,'tiktok',%s,%s,0,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    " caption_text=VALUES(caption_text), "
    " duration_sec=VALUES(duration_sec), "
    " is_paid_promotion=VALUES(is_paid_promotion), "
    " category=VALUES(category), "
    " published_at=VALUES(published_at), "
    " content_id=LAST_INSERT_ID(content_id)"
)

INSERT_CSNAP = (
    "INSERT IGNORE INTO content_snapshots "
    "(content_id, captured_at, view_count, like_count, comment_count) "
    "VALUES (%s,%s,%s,%s,%s)"
)

INSERT_CHSNAP = (
    "INSERT INTO channel_snapshots (channel_id, captured_at, follower_count) "
    "VALUES (%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE follower_count=VALUES(follower_count)"
)

INSERT_LOG = (
    "INSERT INTO crawl_logs "
    "(channel_id, target_url, layer, status, error_type, error_detail, attempted_at) "
    "VALUES (%s,%s,'L2',%s,%s,%s,%s)"
)


# -------------------------------------------------------
# CAPTCHA 감지 (L3에서 이식 — shadow DOM 관통)
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

# CAPTCHA 대기 설정
CAPTCHA_MAX_WAIT = 1800   # 사람이 푸는 대기 한도(초)
CAPTCHA_POLL = 5          # 해제 확인 간격(초)


def logline(msg):
    ts = datetime.now().strftime("%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


async def detect_captcha(page):
    # 1) 셀렉터 빠른 경로
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
    if not unix_sec:
        return None
    return datetime.fromtimestamp(unix_sec, tz=ZoneInfo("Asia/Seoul")).replace(tzinfo=None)

def classify_activity(conn, channel_id):
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

    cnt90 = cnt90 or 0
    cnt180 = cnt180 or 0

    if cnt90 >= config.L2_MIN_VIDEOS:
        return "active"

    if cnt180 >= config.L2_MIN_VIDEOS:
        return "low_active"

    return "inactive"

def save_l2(conn, channel_id, videos, follower_count, captured_at=None):
    if captured_at is None:
        captured_at = datetime.now()

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
                _dt(v.get("published_at")),
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
    lo, hi = getattr(config, pair, default)
    return random.randint(int(lo), int(hi))


async def collect_channel(page, url):
    """
    반환: (videos, follower, status_ok)
      status_ok=False → blocked/soft-block (재시도 대상)
    captcha가 끝내 안 풀리면 CaptchaDetected 예외를 던진다.
    """
    captured = {"videos": [], "follower": None, "status": None, "blocked": False}
    min_videos = getattr(config, "L2_MIN_VIDEOS", 15)
    max_scrolls = 8
    stall_limit = getattr(config, "L2_SCROLL_STALL", 2)

    async def on_response(resp):
        if "/api/post/item_list/" not in resp.url:
            return
        try:
            ctype = resp.headers.get("content-type", "")
            if "application/json" not in ctype:
                return
            payload = await resp.json()
        except Exception:
            return
        videos, cursor, has_more, follower, status_code = \
            parser.parse_item_list(payload)

        # ⭐ 먼저 status 저장
        captured["status"] = status_code

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
                    return

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

    await behavior.arrive(page)
    await behavior.small_scroll(page)
    await page.wait_for_timeout(_rand_ms("L2_GOTO_DELAY", (1200, 2200)))

    prev_count = 0
    stall = 0
    for _ in range(max_scrolls):
        if captured["blocked"]:
            break

        # 스크롤 루프 각 회차에서 captcha 체크.
        # captcha 때문에 영상이 안 늘어나는데 stall로 오판하는 것을 방지.
        if await detect_captcha(page):
            if not await wait_until_solved(page):
                raise CaptchaDetected()
            # 풀린 후 stall 카운터 초기화 (captcha 대기가 stall로 안 세지게)
            stall = 0
            prev_count = len(captured["videos"])

        vids = captured["videos"]
        if len(vids) >= min_videos:
            break
        # 종료조건 2: 스크롤해도 새 영상이 안 늘어남 (바닥 도달)
        if len(vids) == prev_count:
            stall += 1
            if stall >= stall_limit:
                break
        else:
            stall = 0
        prev_count = len(vids)
        await behavior.feed_scroll(page)
        await page.wait_for_timeout(_rand_ms("L2_SCROLL_DELAY", (1000, 2200)))

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
            pw_browser, context = await browser.create_context(p)

            try:
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
                            if blocked >= getattr(config, "STOP_ON_BLOCK", 3):
                                print("[L2] 연속 차단 감지 -> 중단")
                                break
                            continue
                        if not videos:
                            none += 1
                            log_l2(conn, cid, url, "success", "empty", None)
                            print("  [%d/%d] NONE  %s" % (i, len(targets), url))
                            continue
                        n = save_l2(conn, cid, videos, follower)
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
                        err += 1
                        log_l2(conn, cid, url, "failed", "exception", str(e)[:200])
                        print("  [%d/%d] ERR   %s | %s" % (i, len(targets), url, e))
                    finally:
                        await page.close()

                    lo, hi = getattr(config, "L2_CHANNEL_GAP", (1.5, 3.5))
                    await behavior.human_pause(lo, hi)
            finally:
                try:
                    await pw_browser.close()
                except Exception:
                    pass

        print("[L2] 완료: OK=%d NONE=%d BLOCK=%d ERR=%d CAPTCHA_FAIL=%d"
              % (ok, none, blocked, err, captcha_fail))
    finally:
        conn.close()