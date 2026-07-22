# tiktok/steps/l1.py
import time
import asyncio
from datetime import datetime

from playwright.async_api import async_playwright

from tiktok import config
from tiktok import parser
from tiktok.antibot import not_found
from tiktok.antibot import stealth

try:
    import pymysql
except ImportError:
    pymysql = None


# 프록시 없이 L1 전용으로 띄우는 브라우저 설정.
# (프록시/rotate/session이 필요 없어 browser.py 대신 여기서 직접 관리)


LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
]

# 대역폭 절약: L1은 HTML의 JSON만 필요 → 이미지/비디오/폰트 차단
BLOCK_RESOURCE_TYPES = {"image", "media", "font"}

# goto 재시도 (일시적 네트워크 오류 대비). 프록시가 없어 rotate는 없다.
GOTO_RETRY = 2
GOTO_RETRY_WAIT = 1.5


async def _block_heavy(route):
    try:
        if route.request.resource_type in BLOCK_RESOURCE_TYPES:
            await route.abort()
        else:
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


async def fetch_html(page, url):
    """goto (일시 오류 시 재시도) → 데이터 로딩 대기 → HTML 반환."""
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

    # 핵심: __UNIVERSAL_DATA__ script가 실제로 DOM에 들어올 때까지 기다린다.
    # (고정 대기로는 worker 병렬 시 느린 페이지의 JSON을 놓칠 수 있음)
    try:
        await page.wait_for_selector(
            "script#__UNIVERSAL_DATA_FOR_REHYDRATION__",
            state="attached",
            timeout=15000,
        )
    except Exception:
        pass  # 그래도 없으면 아래에서 parse가 None → not_found/none 처리

    return await page.content()


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
           "AND channel_name IS NULL ORDER BY channel_id")
    if limit:
        sql += " LIMIT %d" % int(limit)
    with worker_conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


async def _new_context(playwright):
    """프록시 없이 persistent context 생성 + 리소스 차단 + stealth."""
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=config.PROFILE_DIR,   # ← 변경된 부분
        headless=False,
        args=LAUNCH_ARGS,
    )
    await context.route("**/*", _block_heavy)
    await stealth.prepare_context(context)
    return context


async def _new_page(context):
    page = await context.new_page()
    await stealth.apply(page)
    return page


async def run(channel=None, limit=None, **_):
    if pymysql is None:
        raise SystemExit("pymysql 필요: pip install pymysql")
    
    from tiktok.antibot.browser import profile_ready, clear_profile_locks
    if not profile_ready():
        raise SystemExit(f"프로필 없음: {config.PROFILE_DIR}\n먼저 `python login.py` 실행")
    clear_profile_locks()

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
                    html = await fetch_html(page, url)
                finally:
                    await page.close()
            finally:
                await context.close()

        row = parser.parse_l1(html)
        if row is None:
            # 데이터 없음 → 삭제 계정인지 확인
            # (단일 테스트에선 page가 닫혔으므로 html 기반 statusCode 판별)
            from tiktok.antibot.not_found import _user_detail
            detail = _user_detail(html)
            status = detail.get("statusCode") if detail else None
            if status in (10221,) or (status not in (None, 0) and detail and "userInfo" not in detail):
                print(f"[L1] {handle} -> NOT_FOUND (status={status})")
            else:
                print(f"[L1] {handle} -> DATA NONE")
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

    queue = asyncio.Queue()
    for idx, target in enumerate(targets, 1):
        queue.put_nowait((idx, target))

    stats = {"ok": 0, "none": 0, "not_found": 0, "err": 0, "dup": 0}
    stat_lock = asyncio.Lock()
    # 페이지 동시 생성 레이스 방지 (context.new_page 직렬화)
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

                        html = await fetch_html(page, url)
                        
                        # captcha 여부와 무관하게 파싱.
                        # (captcha가 떠도 페이지 JSON에 데이터가 있으므로)
                        row = parser.parse_l1(html)

                        if row is None:
                            # 데이터 없음 → 삭제 계정인지 statusCode로 확인
                            if await not_found.detect(page=page, html=html):
                                mark_not_found(conn, cid)
                                async with stat_lock:
                                    stats["not_found"] += 1
                                print(f"{tag} NOT_FOUND {url}")
                            else:
                                async with stat_lock:
                                    stats["none"] += 1
                                print(f"{tag} NONE {url}")
                            continue

                        try:
                            save_l1(conn, cid, row)
                        except pymysql.err.IntegrityError as e:
                            if e.args and e.args[0] == 1062:
                                mark_duplicate(conn, cid)
                                async with stat_lock:
                                    stats["dup"] += 1
                                print(f"{tag} DUP {url} → duplicate 마킹")
                                continue
                            async with stat_lock:
                                stats["err"] += 1
                            print(f"{tag} ERR(save) {url} | {e}")
                            continue

                        async with stat_lock:
                            stats["ok"] += 1
                        print(f"{tag} OK {url} "
                              f"(f={row.get('follower_count')} "
                              f"v={row.get('video_count')})")

                    except Exception as e:
                        async with stat_lock:
                            stats["err"] += 1
                        print(f"{tag} ERR {url} | {type(e).__name__}: {e}")

                    finally:
                        if page is not None:
                            try:
                                await page.close()
                            except Exception:
                                pass
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
    print("[L1] 완료: OK=%d NOT_FOUND=%d NONE=%d ERR=%d DUP=%d | %.0f초"
          % (stats["ok"], stats["not_found"], stats["none"],
             stats["err"], stats["dup"], elapsed))