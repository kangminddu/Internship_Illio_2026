# tiktok/steps/l1.py
import time
import asyncio
from datetime import datetime

from playwright.async_api import async_playwright

from tiktok import config
from tiktok import parser
from tiktok.antibot import not_found
from tiktok.antibot import stealth
from tiktok.parser import parse_user_detail
try:
    import pymysql
except ImportError:
    pymysql = None


LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
]

# L1은 HTML에 박힌 프로필 JSON만 필요 → 렌더링용 리소스는 전부 차단
BLOCK_RESOURCE_TYPES = {"image", "media", "font"}

BLOCK_URL_KEYWORDS = ()
GOTO_RETRY = 2
GOTO_RETRY_WAIT = 1.5

DATA_WAIT_MS = 45000
DATA_RETRY = 5
SERVER_ERROR_RETRY = 3
DATA_RETRY_WAIT = 8.0

SCRIPT_ID = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
SERVER_ERROR_TEXT = "Something went wrong"

# 데이터 스크립트가 채워졌는지 / 서버 에러 페이지인지 판정하는 JS.
# - attached 대기로는 빈 script 태그 상태에서 통과해 parse가 None이 된다.
# - 'Something went wrong'은 아무리 기다려도 안 채워지므로 즉시 반환한다.
# - 길이 200 기준: '계정 없음' 응답은 데이터가 적어 1000자를 못 넘긴다.
READY_JS = """() => {
    const b = document.body;
    if (b && b.innerText.includes('Something went wrong')) return true;
    const el = document.getElementById(
        '__UNIVERSAL_DATA_FOR_REHYDRATION__');
    return el && el.textContent && el.textContent.length > 200;
}"""


async def _block_heavy(route):
    """불필요 리소스 차단.
    abort/continue 중 하나는 반드시 호출해야 한다. 예외를 그냥 삼키면
    그 요청이 영원히 대기 상태로 남아 페이지 로딩이 끝나지 않는다."""
    try:
        req = route.request
        if req.resource_type in BLOCK_RESOURCE_TYPES:
            await route.abort()
            return
        url = req.url
        for kw in BLOCK_URL_KEYWORDS:
            if kw in url:
                await route.abort()
                return
    except Exception:
        pass
    try:
        await route.continue_()
    except Exception:
        pass


def normalize(channel):
    c = channel.strip()
    if c.startswith("http"):
        url = c.split("?")[0].rstrip("/")
        handle = url.split("/@")[-1].split("/")[0]
        return handle, url
    handle = c.lstrip("@")
    return handle, "https://www.tiktok.com/@" + handle


async def _wait_ready(page):
    """데이터 스크립트가 채워질 때까지 대기. 타임아웃 여부 반환."""
    try:
        await page.wait_for_function(READY_JS, timeout=DATA_WAIT_MS)
        return False
    except Exception:
        return True


async def fetch_html(page, url, debug=False):
    """goto → 데이터 로딩 대기 → HTML 반환."""
    last_exc = None
    for attempt in range(GOTO_RETRY + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            break
        except Exception as e:
            last_exc = e
            if attempt < GOTO_RETRY:
                await asyncio.sleep(GOTO_RETRY_WAIT)
            else:
                raise last_exc

    timed_out = await _wait_ready(page)
    html = await page.content()
    if debug:
        kind = " [서버에러]" if SERVER_ERROR_TEXT in html else (
            " [타임아웃]" if timed_out else "")
        print(f"    [debug] HTML {len(html):,}자, "
              f"스크립트={'있음' if SCRIPT_ID in html else '없음'}{kind}")
    return html


async def fetch_row(page, url, debug=False):
    """HTML(SSR) 파싱 + /api/user/detail/ XHR(CSR) 가로채기 병행.

    틱톡은 같은 URL이라도 요청마다 SSR/CSR을 다르게 내려준다.
    SSR이면 __UNIVERSAL_DATA__ 스크립트에 데이터가 박혀 있지만,
    CSR이면 HTML은 껍데기이고 데이터는 XHR로만 온다.
    HTML만 보면 CSR 응답에서 전부 실패하므로 두 경로를 모두 본다.

    반환 (row|None, html)
    """
    api_data = {}

    def on_resp(resp):
        if "/api/user/detail/" in resp.url:
            asyncio.create_task(_grab(resp))

    async def _grab(resp):
        try:
            api_data["json"] = await resp.json()
        except Exception:
            pass

    page.on("response", on_resp)
    try:
        html = ""
        for attempt in range(DATA_RETRY + 1):
            html = await fetch_html(page, url, debug=debug)

            # 1) SSR 경로
            row = parser.parse_l1(html)
            if row is not None:
                return row, html

            # 2) CSR 경로 — XHR 응답 사용
            await asyncio.sleep(1)          # _grab 태스크 완료 대기
            row = parse_user_detail(api_data.get("json"))
            if row is not None:
                if debug:
                    print("    [debug] XHR /api/user/detail/ 로 파싱 성공")
                return row, html

            if SCRIPT_ID in html:
                return None, html           # 계정 없음/비공개
            if attempt < DATA_RETRY:
                await asyncio.sleep(DATA_RETRY_WAIT)
        return None, html
    finally:
        page.remove_listener("response", on_resp)

UPDATE_CH = ("UPDATE channels SET channel_name=%s, bio=%s, external_link=%s, "
             "external_channel_id=%s WHERE channel_id=%s")
INSERT_SNAP = (
    "INSERT INTO channel_snapshots "
    "(channel_id, captured_at, follower_count, following_count, "
    " total_video_count, total_like_count) VALUES (%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE follower_count=VALUES(follower_count), "
    "following_count=VALUES(following_count), "
    "total_video_count=VALUES(total_video_count), "
    "total_like_count=VALUES(total_like_count)"
)
MARK_DUPLICATE = (
    "UPDATE channels SET channel_id_status='duplicate', "
    "channel_url_normalized=NULL WHERE channel_id=%s"
)
MARK_NOT_FOUND = (
    "UPDATE channels SET channel_id_status='not_found' WHERE channel_id=%s"
)
LOG_ROW = (
    "INSERT INTO crawl_logs "
    "(channel_id, target_url, layer, status, http_status, "
    " error_type, error_detail) "
    "VALUES (%s, %s, 'L1', %s, %s, %s, %s)"
)


def save_l1(worker_conn, channel_id, row):
    ext_link = None
    bio = row.get("bio")
    if bio:
        for tok in bio.split():
            if tok.startswith("http"):
                ext_link = tok[:512]
                break
    with worker_conn.cursor() as cur:
        cur.execute(UPDATE_CH, (row.get("nickname"), bio, ext_link,
                                row.get("sec_uid"), channel_id))
        cur.execute(INSERT_SNAP, (
            channel_id, datetime.now(),
            row.get("follower_count"), row.get("following_count"),
            row.get("video_count"), row.get("heart_count"),
        ))
    worker_conn.commit()


def log_result(worker_conn, channel_id, url, status,
               error_type=None, error_detail=None):
    """수집 결과를 crawl_logs에 기록. 실패 원인을 남겨야 추적이 가능하다."""
    try:
        with worker_conn.cursor() as cur:
            cur.execute(LOG_ROW, (channel_id, url[:512], status, None,
                                  error_type,
                                  (error_detail or "")[:500] or None))
        worker_conn.commit()
    except Exception:
        pass


def mark_duplicate(worker_conn, channel_id):
    with worker_conn.cursor() as cur:
        cur.execute(MARK_DUPLICATE, (channel_id,))
    worker_conn.commit()


def mark_not_found(worker_conn, channel_id):
    with worker_conn.cursor() as cur:
        cur.execute(MARK_NOT_FOUND, (channel_id,))
    worker_conn.commit()


def fetch_targets(worker_conn, limit):
    sql = ("SELECT channel_id, channel_url_normalized FROM channels "
           "WHERE platform='tiktok' AND channel_id_status='handle_only' "
           "AND channel_name IS NULL "
           "AND channel_url_normalized LIKE '%%tiktok.com%%' "
           "ORDER BY channel_id")
    if limit:
        sql += " LIMIT %d" % int(limit)
    with worker_conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


async def _new_context(playwright):
    """L1은 공개 프로필만 읽으므로 로그인이 필요 없다.

    persistent profile(로그인 상태)로 접속하면 틱톡이 계정 단위로
    조회를 제한해, SSR 데이터를 뺀 shell만 내려준다.
    (성공 응답은 프로필이 HTML에 렌더링되지만, 로그인 상태에서는
     로그인 UI만 있고 데이터가 없다 — 덤프 대조로 확인)
    비로그인은 IP 기준으로만 판정되어 훨씬 안정적이다.
    """
    browser = await playwright.chromium.launch(
        headless=True,
        args=LAUNCH_ARGS,
    )
    context = await browser.new_context(
        locale="ko-KR",
        timezone_id="Asia/Seoul",
    )
    await context.route("**/*", _block_heavy)
    return context


async def _new_page(context):
    return await context.new_page()



async def run(channel=None, limit=None, **_):
    if pymysql is None:
        raise SystemExit("pymysql 필요: pip install pymysql")

    # -----------------------------
    # 단일 채널 테스트
    # -----------------------------
    if channel:
        handle, url = normalize(channel)
        async with async_playwright() as p:
            context = await _new_context(p)
            try:
                page = await _new_page(context)
                try:
                    row, html = await fetch_row(page, url, debug=True)
                finally:
                    await page.close()
            finally:
                await context.close()

        if row is None:
            if SCRIPT_ID not in html:
                kind = ("서버에러" if SERVER_ERROR_TEXT in html
                        else "데이터 스크립트 없음")
                print(f"[L1] {handle} -> BLOCKED ({kind}, "
                      f"HTML {len(html):,}자)")
                return
            from tiktok.antibot.not_found import _user_detail
            detail = _user_detail(html)
            status = detail.get("statusCode") if detail else None
            if status in (10221,) or (status not in (None, 0) and detail and "userInfo" not in detail):
                print(f"[L1] {handle} -> NOT_FOUND (status={status})")
            else:
                print(f"[L1] {handle} -> DATA NONE (status={status})")
            return

        print("[L1] %s -> OK" % handle)
        for k, v in row.items():
            print("   %-16s: %s" % (k, v))
        return

    # -----------------------------
    # 전체 크롤링
    # -----------------------------
    main_conn = pymysql.connect(**config.DB)
    try:
        targets = fetch_targets(main_conn, limit)
    finally:
        main_conn.close()

    print("[L1] 대상 채널:", len(targets))
    if not targets:
        return

    workers = getattr(config, "L1_WORKERS", 3)
    print(f"[L1] workers={workers} 데이터대기={DATA_WAIT_MS//1000}s "
          f"reload재시도={DATA_RETRY}회({DATA_RETRY_WAIT:.0f}s 간격) "
          f"차단리소스={sorted(BLOCK_RESOURCE_TYPES)}")

    queue = asyncio.Queue()
    for idx, target in enumerate(targets, 1):
        queue.put_nowait((idx, target))

    stats = {"ok": 0, "none": 0, "not_found": 0, "err": 0,
             "dup": 0, "blocked": 0}
    stat_lock = asyncio.Lock()
    page_create_lock = asyncio.Lock()
    start = time.time()

    async with async_playwright() as p:
        context = await _new_context(p)

        async def worker(worker_id):
            conn = pymysql.connect(**config.DB)
            try:
                while True:
                    try:
                        i, (cid, url) = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    tag = f"[W{worker_id}] [{i}/{len(targets)}]"
                    page = None
                    try:
                        async with page_create_lock:
                            page = await _new_page(context)

                        row, html = await fetch_row(page, url)

                        if row is None:
                            # 데이터 스크립트 자체가 없음 = 서버에러/로딩실패.
                            # '채널 없음'과 구분해 기록해야 재시도가 가능하다.
                            if SCRIPT_ID not in html:
                                etype = ("server_error"
                                         if SERVER_ERROR_TEXT in html
                                         else "blocked")
                                log_result(conn, cid, url, "failed", etype,
                                           f"no data script, html={len(html)}")
                                async with stat_lock:
                                    stats["blocked"] += 1
                                print(f"{tag} BLOCKED({etype}) {url} "
                                      f"(HTML {len(html):,}자)")
                                continue

                            if await not_found.detect(page=page, html=html):
                                mark_not_found(conn, cid)
                                log_result(conn, cid, url, "failed",
                                           "not_found", None)
                                async with stat_lock:
                                    stats["not_found"] += 1
                                print(f"{tag} NOT_FOUND {url}")
                            else:
                                log_result(conn, cid, url, "failed",
                                           "data_none", "parse returned None")
                                async with stat_lock:
                                    stats["none"] += 1
                                print(f"{tag} NONE {url}")
                            continue

                        try:
                            save_l1(conn, cid, row)
                        except pymysql.err.IntegrityError as e:
                            if e.args and e.args[0] == 1062:
                                mark_duplicate(conn, cid)
                                log_result(conn, cid, url, "failed",
                                           "duplicate_channel", repr(e))
                                async with stat_lock:
                                    stats["dup"] += 1
                                print(f"{tag} DUP {url} → duplicate 마킹")
                                continue
                            log_result(conn, cid, url, "failed",
                                       "db_error", repr(e))
                            async with stat_lock:
                                stats["err"] += 1
                            print(f"{tag} ERR(save) {url} | {e}")
                            continue

                        log_result(conn, cid, url, "success")
                        async with stat_lock:
                            stats["ok"] += 1
                        print(f"{tag} OK {url} "
                              f"(f={row.get('follower_count')} "
                              f"v={row.get('video_count')})")

                    except Exception as e:
                        try:
                            log_result(conn, cid, url, "failed",
                                       "worker_error", repr(e))
                        except Exception:
                            pass
                        async with stat_lock:
                            stats["err"] += 1
                        print(f"{tag} ERR {url} | {type(e).__name__}: {e}")

                    finally:
                        if page is not None:
                            try:
                                await page.close()
                            except Exception:
                                pass
                        # 채널 간 간격. 연속 요청은 프로필이 봇으로 마킹되는
                        # 속도를 높인다. (유튜브 rate_control에 해당하는 최소 장치)
                        await asyncio.sleep(1)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        try:
            tasks = [asyncio.create_task(worker(i + 1))
                     for i in range(workers)]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for wid, r in enumerate(results, 1):
                if isinstance(r, Exception):
                    print(f"[L1] W{wid} 비정상 종료: {type(r).__name__}: {r}")
        finally:
            try:
                await context.close()
            except Exception:
                pass

    elapsed = time.time() - start
    print("[L1] 완료: OK=%d NOT_FOUND=%d NONE=%d BLOCKED=%d ERR=%d DUP=%d "
          "| %.0f초"
          % (stats["ok"], stats["not_found"], stats["none"],
             stats["blocked"], stats["err"], stats["dup"], elapsed))