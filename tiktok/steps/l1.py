# tiktok/steps/l1.py
"""
틱톡 L1 — 채널 기본정보 수집 (팔로워, 총영상수, bio, sec_uid)

★ 이 파일이 이번 작업에서 가장 오래 붙잡은 곳이다.

7월 13일에는 354개 채널을 18분에 무결점으로 수집했다.
그런데 16일 뒤 같은 코드가 30~50% 실패하기 시작했다.

원인을 찾으려고 통제 변수를 하나씩 배제했다:
    CPU, 메모리, IP 3종(EC2/가정용/모바일 핫스팟), 로그인 상태,
    브라우저 컨텍스트 재사용, 캐시/쿠키, 동시성,
    요청 간격 3초~30초, 리소스 차단, headless 여부, 브라우저 실행 옵션
전부 무관했다. 완전 기본 설정에 30초 간격으로 요청해도 같은 비율로 실패했다.

성공/실패 HTML을 직접 대조하고서야 원인이 둘로 갈렸다:
  ① 로그인 상태 — 틱톡이 계정 단위로 조회를 제한한다 (→ _new_context 참고)
  ② SSR/CSR 혼재 — 데이터가 HTML이 아니라 XHR로만 올 때가 있다 (→ fetch_row 참고)

유튜브 L1과 결정적으로 다른 점
------
  유튜브 : requests. ytInitialData가 HTML에 항상 박혀 온다.
  틱톡   : Playwright 필수. 그런데 데이터가 HTML에 있을 때도, 없을 때도 있다.
"""
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
    "--disable-blink-features=AutomationControlled",  # navigator.webdriver 숨김
    "--disable-dev-shm-usage",                        # 컨테이너 /dev/shm 부족 대비
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-crash-restore-bubble",       # 비정상 종료 후 '복원하시겠습니까' 배너 억제
    "--disable-session-crashed-bubble",  # (배너가 뜨면 페이지를 가려 클릭이 막힌다)
]

# L1은 HTML에 박힌 프로필 JSON만 필요 → 렌더링용 리소스는 전부 차단
#
# 인기 채널일수록 썸네일이 수십 개라 페이지가 무거워지고,
# 그만큼 데이터 스크립트가 채워지는 시점도 밀린다.
# ⚠️ stylesheet를 넣었다 뺐다. 실패율에 영향이 없어서(60% vs 50%)
#    원래 상태로 되돌렸다.
BLOCK_RESOURCE_TYPES = {"image", "media", "font"}

# URL 키워드 차단은 비워뒀다.
# 틱톡 자체 모니터링 도메인(mon-va, log-va)을 막아봤지만 효과가 없었다.
BLOCK_URL_KEYWORDS = ()

GOTO_RETRY = 2
GOTO_RETRY_WAIT = 1.5

DATA_WAIT_MS = 45000
DATA_RETRY = 5            # 서버 에러가 확률적이라 재시도를 넉넉히 둔다.
SERVER_ERROR_RETRY = 3    # (실패율 15% 기준 6회 시도면 사실상 다 잡힌다)
DATA_RETRY_WAIT = 8.0

SCRIPT_ID = "__UNIVERSAL_DATA_FOR_REHYDRATION__"
SERVER_ERROR_TEXT = "Something went wrong"
# ↑ 실패 HTML이 항상 102KB 안팎이고 본문이 "문제가 발생했습니다 /
#   나중에 다시 시도하세요"였다. 틱톡 공식 문서도 이 화면을
#   '서버 일시 오류'로 안내한다. 우리가 통제할 수 없는 실패.

# 데이터 스크립트가 채워졌는지 / 서버 에러 페이지인지 판정하는 JS.
# - attached 대기로는 빈 script 태그 상태에서 통과해 parse가 None이 된다.
#   (틱톡은 빈 태그를 먼저 DOM에 넣고 JS가 나중에 내용을 채운다.
#    서버가 느리면 그 사이에 page.content()를 가져가버린다)
# - 'Something went wrong'은 아무리 기다려도 안 채워지므로 즉시 반환한다.
#   → 45초를 낭비하지 않고 바로 재시도로 넘어간다.
# - 길이 200 기준: '계정 없음' 응답은 데이터가 적어 1000자를 못 넘긴다.
#   처음에 1000으로 뒀다가 없는 계정이 BLOCKED로 오분류돼서 낮췄다.
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
    그 요청이 영원히 대기 상태로 남아 페이지 로딩이 끝나지 않는다.

    ← 원래 try/except로 통째로 감싸고 pass만 하던 코드였다.
      예외가 나면 route를 abort도 continue도 안 해서
      해당 요청이 pending으로 남고 페이지가 완료되지 않았다.
    """
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
        await route.continue_()   # ← 어떤 경로로 오든 반드시 여기 도달
    except Exception:
        pass


def normalize(channel):
    """--channel 인자 정규화. URL이든 핸들이든 (handle, url) 반환."""
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
        # 실패 원인을 한 줄로 구분해서 보여준다.
        # [서버에러] / [타임아웃] / (표시 없음 = 정상)
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

    ── 어떻게 찾았나 ──
    실패 케이스의 네트워크 로그를 보니 /api/user/detail/이 200으로
    응답하고 있었다. 데이터는 왔는데 파서가 HTML만 보고 있었던 것.

    참고: 틱톡 L2는 처음부터 이 방식이었다(/api/post/item_list/ 가로채기).
    L1만 HTML 파싱 방식이라 이 문제가 생겼다.

    반환 (row|None, html)
    """
    api_data = {}

    def on_resp(resp):
        # 응답 리스너는 동기 함수여야 한다. resp.json()은 async라
        # 태스크로 띄워서 백그라운드에서 읽는다.
        if "/api/user/detail/" in resp.url:
            asyncio.create_task(_grab(resp))

    async def _grab(resp):
        try:
            api_data["json"] = await resp.json()
        except Exception:
            pass   # 응답 본문이 비어 있는 경우가 있다(틱톡이 거부한 케이스)

    page.on("response", on_resp)
    try:
        html = ""
        for attempt in range(DATA_RETRY + 1):
            html = await fetch_html(page, url, debug=debug)

            # 1) SSR 경로 — HTML에 데이터가 박혀 있으면 여기서 끝
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

            # 3) 스크립트가 채워졌는데 둘 다 실패 = 계정 없음/비공개.
            #    재시도해도 결과가 같으므로 즉시 반환한다.
            #    (서버 에러는 스크립트 자체가 없어 여기 안 걸린다)
            if SCRIPT_ID in html:
                return None, html           # 계정 없음/비공개
            if attempt < DATA_RETRY:
                await asyncio.sleep(DATA_RETRY_WAIT)
        return None, html
    finally:
        # 리스너를 반드시 제거한다. page를 재사용하므로
        # 안 떼면 다음 채널의 응답이 이전 api_data에 섞인다.
        page.remove_listener("response", on_resp)


UPDATE_CH = ("UPDATE channels SET channel_name=%s, bio=%s, external_link=%s, "
             "external_channel_id=%s WHERE channel_id=%s")
# external_channel_id에 sec_uid를 넣는다.
# 틱톡 핸들은 바뀔 수 있지만 sec_uid는 영구하다.
# (유튜브의 UC ID와 같은 역할)

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
    """수집 결과 저장."""
    # bio에서 첫 URL을 외부 링크로 뽑는다.
    # 틱톡은 프로필 링크 필드를 API로 안 주므로 bio 본문에서 찾는다.
    # (유튜브는 aboutChannelViewModel.links로 별도 제공)
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
            channel_id, datetime.now(),   # naive = OS 로컬(KST). pymysql이 그대로 넘긴다.
            row.get("follower_count"), row.get("following_count"),
            row.get("video_count"), row.get("heart_count"),
        ))
    worker_conn.commit()


def log_result(worker_conn, channel_id, url, status,
               error_type=None, error_detail=None):
    """수집 결과를 crawl_logs에 기록. 실패 원인을 남겨야 추적이 가능하다.

    ← 원래 L1에는 이 함수가 없었다. 실패를 화면에만 출력하고
      DB에는 아무것도 안 남겼다. 그래서 501개를 돌려도
      crawl_logs가 비어 있어서 "몇 개가 왜 실패했나"를 알 수 없었다.
      (유튜브에서 이미 지키던 원칙이 틱톡에는 없었던 것)
    """
    try:
        with worker_conn.cursor() as cur:
            cur.execute(LOG_ROW, (channel_id, url[:512], status, None,
                                  error_type,
                                  (error_detail or "")[:500] or None))
        worker_conn.commit()
    except Exception:
        pass   # 로그 실패로 수집 자체가 멈추지 않게


def mark_duplicate(worker_conn, channel_id):
    with worker_conn.cursor() as cur:
        cur.execute(MARK_DUPLICATE, (channel_id,))
    worker_conn.commit()


def mark_not_found(worker_conn, channel_id):
    with worker_conn.cursor() as cur:
        cur.execute(MARK_NOT_FOUND, (channel_id,))
    worker_conn.commit()


def fetch_targets(worker_conn, limit):
    """대상 선정 = resume 로직.

    유튜브와 방식이 다르다.
      유튜브 : crawl_logs의 L1 success 기록이 없는 채널
      틱톡   : channel_name IS NULL인 채널  ← 결과 테이블 자체가 기준

    성공하면 channel_name이 채워져 다음 실행에서 제외된다.
    단순하지만 '언제 수집했나'를 알 수 없어 주기적 갱신이 불가능하다.

    LIKE '%%tiktok.com%%': 시드 엑셀에 platform='tiktok'인데
    유튜브 URL이 들어간 행이 1건 있었다. 도메인까지 검증한다.
    """
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

    ── 대조 결과 ──
      비로그인 성공 : 384KB, 프로필 데이터가 HTML에 렌더링됨,
                      title = "TikTok - Make Your Day"
      로그인 실패   : 102KB, 로그인 UI(업로드/알림 19개)만 있고 데이터 없음,
                      title = "(19)"  ← 알림 개수만, 채널명 없음

    ★ 인증이 항상 유리한 것은 아니다.
      L1은 공개 정보만 읽는데, L2/L3와 브라우저 프로필을 공유하면서
      불필요한 로그인 상태를 끌고 들어간 게 문제였다.
      (L2/L3는 영상 목록·댓글이라 로그인이 실제로 필요하다)
    """
    browser = await playwright.chromium.launch(
        headless=True,     # L1은 무인 실행. CAPTCHA 수동 해결이 필요한 L2/L3와 다르다.
        args=LAUNCH_ARGS,
    )
    context = await browser.new_context(
        locale="ko-KR",
        timezone_id="Asia/Seoul",
    )
    await context.route("**/*", _block_heavy)
    # stealth는 적용하지 않는다.
    # playwright_stealth를 붙였더니 모든 요청이
    # ERR_HTTP_RESPONSE_CODE_FAILURE로 실패했다. (버전 호환 문제로 추정)
    return context


async def _new_page(context):
    return await context.new_page()


async def run(channel=None, limit=None, **_):
    if pymysql is None:
        raise SystemExit("pymysql 필요: pip install pymysql")

    # -----------------------------
    # 단일 채널 테스트
    # -----------------------------
    # DB를 건드리지 않는다. 파싱 결과만 화면에 출력.
    # (틱톡 L2의 --channel은 DB에 쓰기 때문에 channel_id를 조회해야 했다)
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
            # 실패를 세 갈래로 구분해서 보여준다.
            if SCRIPT_ID not in html:
                kind = ("서버에러" if SERVER_ERROR_TEXT in html
                        else "데이터 스크립트 없음")
                print(f"[L1] {handle} -> BLOCKED ({kind}, "
                      f"HTML {len(html):,}자)")
                return
            from tiktok.antibot.not_found import _user_detail
            detail = _user_detail(html)
            status = detail.get("statusCode") if detail else None
            # 10221 = 틱톡의 '계정 없음' 코드 (실측)
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
        main_conn.close()   # 대상만 읽고 바로 닫는다. 워커가 각자 연결한다.

    print("[L1] 대상 채널:", len(targets))
    if not targets:
        return

    workers = getattr(config, "L1_WORKERS", 3)
    # 실행 설정을 먼저 출력한다. 몇 시간짜리 작업을 걸어두고
    # 나중에 로그를 보면 "어떤 설정으로 돌린 거지?"를 알 수 없다.
    print(f"[L1] workers={workers} 데이터대기={DATA_WAIT_MS//1000}s "
          f"reload재시도={DATA_RETRY}회({DATA_RETRY_WAIT:.0f}s 간격) "
          f"차단리소스={sorted(BLOCK_RESOURCE_TYPES)}")

    # asyncio.Queue로 작업을 분배한다.
    # 워커마다 리스트를 나눠주면 어떤 워커는 빨리 끝나고 놀게 된다.
    # 큐에서 하나씩 꺼내가면 자연스럽게 균형이 맞는다.
    queue = asyncio.Queue()
    for idx, target in enumerate(targets, 1):
        queue.put_nowait((idx, target))

    stats = {"ok": 0, "none": 0, "not_found": 0, "err": 0,
             "dup": 0, "blocked": 0}
    stat_lock = asyncio.Lock()
    # 여러 워커가 동시에 context.new_page()를 호출하면
    # Playwright 내부에서 레이스가 나는 경우가 있어 직렬화한다.
    page_create_lock = asyncio.Lock()
    start = time.time()

    async with async_playwright() as p:
        # 컨텍스트 하나를 모든 워커가 공유한다.
        # A/B 테스트 결과 컨텍스트를 매번 새로 만드는 쪽(20%)이
        # 재사용하는 쪽(50%)보다 오히려 성공률이 낮았다.
        # 쿠키가 전혀 없는 콜드 요청이 더 의심받는 것으로 보인다.
        context = await _new_context(p)

        async def worker(worker_id):
            conn = pymysql.connect(**config.DB)   # 워커마다 독립 커넥션
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
                            #
                            # ★ 이 구분이 핵심이다.
                            #   blocked/server_error → channels를 안 건드림
                            #                          → 다음 실행에 재시도됨
                            #   not_found            → channel_id_status 변경
                            #                          → 영구 제외
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

                            # statusCode 기반 판별 (HTML 문자열 검색이 아니라).
                            # 안내 문구는 지역/언어에 따라 달라져 오탐이 난다.
                            if await not_found.detect(page=page, html=html):
                                mark_not_found(conn, cid)
                                log_result(conn, cid, url, "failed",
                                           "not_found", None)
                                async with stat_lock:
                                    stats["not_found"] += 1
                                print(f"{tag} NOT_FOUND {url}")
                            else:
                                # 스크립트도 있고 계정도 있는데 파싱 실패.
                                # 드문 경우라 원인을 남겨둔다.
                                log_result(conn, cid, url, "failed",
                                           "data_none", "parse returned None")
                                async with stat_lock:
                                    stats["none"] += 1
                                print(f"{tag} NONE {url}")
                            continue

                        try:
                            save_l1(conn, cid, row)
                        except pymysql.err.IntegrityError as e:
                            # 1062 = UNIQUE 위반. 같은 sec_uid를 가진 채널이
                            # 이미 있다 = 시드에 같은 계정이 두 번 들어온 것.
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
                        # 워커 예외 격리. 채널 1건 때문에 전체가 죽지 않게.
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
                        #
                        # ⚠️ 3초→5초→30초까지 늘려봤지만 실패율에 영향이 없었다.
                        #    틱톡 서버 측 확률적 거부라 간격으로는 통제가 안 된다.
                        await asyncio.sleep(1)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        try:
            tasks = [asyncio.create_task(worker(i + 1))
                     for i in range(workers)]
            # return_exceptions=True: 워커 하나가 죽어도 나머지는 계속
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