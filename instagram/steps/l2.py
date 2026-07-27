# -*- coding: utf-8 -*-
"""
steps/l2.py  (Instagram L2 — 게시물 목록 수집)

설계 원칙 (L1 계승 + 프로젝트 문서 기준)
- L1 과 동일하게 Playwright 단일 세션 + 브라우저 goto + 리스너 캡처.
  request 직접 호출(찍어내기)은 인스타에서 429 유발 → 사용 안 함.
- 게시물 목록 GraphQL 캡처. ⚠️ friendly-name 은 계정에 따라 여러 종류가 온다.
  (config.L2_POSTS_QUERY_NAMES 참고. 단일 이름만 잡다가 소형 계정 193개를
   empty 로 놓쳤던 이력이 있음.)
- 최근 L2_POST_LIMIT(10)개 무조건 수집. 기간 필터 없음.
  (3/6개월 확장·활동성 분류·파생 지표는 전부 metrics 단계. raw→derived 원칙.)
- 저장 3층: HTML(raw) / 목록 GraphQL(raw) / 정규화(DB).
  DB 는 fandom_crm 공유 스키마:
    channels(L1이 채움) → contents(게시물) → content_snapshots(좋아요/댓글 시점값)
- 좋아요/댓글 수는 시점마다 바뀌므로 contents 가 아니라 content_snapshots 에 저장.
- 연속 차단(CHALLENGE/로그인/429) STOP_ON_BLOCK 회 → 세션 보호 위해 중단.

L1 과의 차이(틱톡 L2 대비):
- async → sync (L1 이 sync 라 통일)
- TikTok CAPTCHA 로직 제거 (인스타는 CHALLENGE/checkpoint 를 URL 로 판별)
- 딜레이 8~15초 (틱톡 1.5초 아님 — 인스타는 antibot 모듈 없이 딜레이로 버팀)

디버그:
- 환경변수 IG_DEBUG_GQL=1 로 실행하면 오가는 friendly-name 을 전부 출력.
    IG_DEBUG_GQL=1 python -m instagram.steps.l2
  새 쿼리 이름이 보이면 config.L2_POSTS_QUERY_NAMES 에 추가할 것.
"""
from zoneinfo import ZoneInfo
import os
import json
import re
import time
import random
import logging
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout
from playwright_stealth import Stealth
_stealth = Stealth()
# ── config 연동 (실행 방식에 따라 import 경로 대응) ──
try:
    from instagram.config import (
        SESSION_FILE, OUTPUT_DIR, L2_HTML_DIR, L2_GRAPHQL_DIR,
        LOG_DIR, L2_LOG_FILE,  HEADLESS, DB, PLATFORM,
        L2_GRAPHQL_URL_PART, L2_POSTS_QUERY_NAMES, L2_POST_LIMIT,
        L2_MAX_SCROLLS, L2_SCROLL_STALL, L2_GOTO_TIMEOUT_MS,
        L2_GRAPHQL_GRACE_MS, L2_RENDER_WAIT_MS, L2_SCROLL_DELAY, L2_CHANNEL_GAP,
        BATCH_LIMIT, STOP_ON_BLOCK, L2_REELS_QUERY_NAMES, L2_COLLECT_REELS, L2_REELS_LIMIT, context_kwargs
    )
except Exception:
    from config import (
        SESSION_FILE, OUTPUT_DIR, L2_HTML_DIR, L2_GRAPHQL_DIR,
        LOG_DIR, L2_LOG_FILE,  HEADLESS, DB, PLATFORM,
        L2_GRAPHQL_URL_PART, L2_POSTS_QUERY_NAMES, L2_POST_LIMIT,
        L2_MAX_SCROLLS, L2_SCROLL_STALL, L2_GOTO_TIMEOUT_MS,
        L2_GRAPHQL_GRACE_MS, L2_RENDER_WAIT_MS, L2_SCROLL_DELAY, L2_CHANNEL_GAP,
        BATCH_LIMIT, STOP_ON_BLOCK,L2_REELS_QUERY_NAMES, L2_COLLECT_REELS,L2_REELS_LIMIT,context_kwargs
    )

try:
    from instagram.lib.posts_parser import parse_posts
except Exception:
    from lib.posts_parser import parse_posts

try:
    import pymysql
except ImportError:
    pymysql = None


# =========================================================
# 상태 상수 / 디버그 플래그
# =========================================================
STATUS_OK = "success"
STATUS_FAILED = "failed"

DEBUG_GQL = os.environ.get("IG_DEBUG_GQL") == "1"

# 미지의 friendly-name 수집용 (실행 끝에 요약 출력).
# 새 이름이 보이면 config.L2_POSTS_QUERY_NAMES 에 추가하면 된다.
_SEEN_QUERY_NAMES = {}


# =========================================================
# DB SQL (fandom_crm 공유 스키마)
# =========================================================
# contents: 게시물 본체 (좋아요/댓글수는 여기 넣지 않음 — 스냅샷으로)
INSERT_CONTENT = (
    "INSERT INTO contents "
    "(channel_id, external_id, content_type, is_paid_promotion, "
    " published_at, duration_sec, caption_text, collected_at) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    " caption_text=VALUES(caption_text), "
    " content_type=VALUES(content_type), "
    " is_paid_promotion=VALUES(is_paid_promotion), "
    " published_at=VALUES(published_at), "
    " content_id=LAST_INSERT_ID(content_id)"
)

# content_snapshots: 그 시점의 좋아요/댓글/조회수 (INSERT IGNORE = 같은 시각 중복 방지)
INSERT_CSNAP = (
    "INSERT IGNORE INTO content_snapshots "
    "(content_id, captured_at, view_count, like_count, comment_count) "
    "VALUES (%s,%s,%s,%s,%s)"
)

INSERT_LOG = (
    "INSERT INTO crawl_logs "
    "(channel_id, target_url, layer, status, http_status, "
    " error_type, error_detail, attempted_at) "
    "VALUES (%s,%s,'L2',%s,%s,%s,%s,%s)"
)

CLIPS_ROOT = "xdt_api__v1__clips__user__connection_v2"

# 인스타 media pk(Snowflake)의 에포크 오프셋
_IG_EPOCH = 1314220021


def _ts_from_pk(pk):
    """
    media pk 상위 비트에서 생성 시각(unix초) 복원.
    릴스 탭 응답에는 taken_at 이 아예 없어 정렬/저장용 키가 필요하다.
    ⚠️ 경험적 방법이라 오차 가능. L3 가 개별 방문 시 정확한 값으로 덮어쓴다.
    """
    try:
        return (int(pk) >> 23) // 1000 + _IG_EPOCH
    except Exception:
        return None


def _parse_reels(data):
    """
    릴스 탭(clips) 응답 파서. 그리드(timeline)와 스키마가 달라 parse_posts 불가.
      - root_field : xdt_api__v1__clips__user__connection_v2
      - 노드 경로  : edges[].node.media   (timeline 보다 한 겹 깊다)
      - 조회수     : play_count (view_count 는 null)
      - ❌ 없음    : taken_at / caption / video_duration
                     → taken_at 은 pk 로 복원, 나머지는 L3 에서 보강
    반환 형태는 parse_posts 와 동일하게 맞춘다.
    """
    try:
        conn = (data or {}).get("data", {}).get(CLIPS_ROOT)
    except Exception:
        return []
    if not isinstance(conn, dict):
        return []

    out = []
    for edge in conn.get("edges") or []:
        m = (edge or {}).get("node", {}).get("media")
        if not isinstance(m, dict):
            continue
        code = m.get("code")
        if not code:
            continue

        caption = m.get("caption")
        cap_text = caption.get("text") if isinstance(caption, dict) else None
        dur = m.get("video_duration")

        out.append({
            "external_id": code,
            "content_type": "reels",
            "taken_at": m.get("taken_at") or _ts_from_pk(m.get("pk")),
            "caption_text": cap_text,
            "like_count": m.get("like_count"),
            "comment_count": m.get("comment_count"),
            "view_count": m.get("play_count") or m.get("view_count"),
            "duration_sec": int(dur) if dur else None,
            "is_paid_promotion": 1 if m.get("is_paid_partnership") else 0,
        })
    return out


def _is_posts_graphql(response):
    """그리드/릴스 목록 GraphQL 응답인지 판별."""
    name = _friendly_name(response)
    if name is None:
        return False
    return name in L2_POSTS_QUERY_NAMES or name in L2_REELS_QUERY_NAMES


# =========================================================
# 로깅
# =========================================================
def setup_logging():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("l2")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    fh = logging.FileHandler(L2_LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


# =========================================================
# 저장 헬퍼 (raw)
# =========================================================
def _safe_name(username):
    return re.sub(r"[^\w.-]", "_", username or "unknown")


def save_l2_html(username, html):
    L2_HTML_DIR.mkdir(parents=True, exist_ok=True)
    path = L2_HTML_DIR / f"{_safe_name(username)}.html"
    path.write_text(html, encoding="utf-8")
    return path


def save_l2_graphql(username, pages):
    """pages: 캡처한 목록 GraphQL 응답들의 리스트 (페이지별 verbatim)."""
    L2_GRAPHQL_DIR.mkdir(parents=True, exist_ok=True)
    path = L2_GRAPHQL_DIR / f"{_safe_name(username)}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=2)
    return path


def _dt(unix_sec):
    if not unix_sec:
        return None
    try:
        return datetime.fromtimestamp(int(unix_sec), ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    except Exception:
        return None


# =========================================================
# GraphQL 캡처 판별
# =========================================================
def _friendly_name(response):
    """요청 헤더의 x-fb-friendly-name. GraphQL POST 가 아니면 None."""
    try:
        req = response.request
        if req.method != "POST":
            return None
        if L2_GRAPHQL_URL_PART not in response.url:
            return None
        return req.headers.get("x-fb-friendly-name")
    except Exception:
        return None


def _is_posts_graphql(response):
    name = _friendly_name(response)
    if name is None:
        return False
    return name in L2_POSTS_QUERY_NAMES or name in L2_REELS_QUERY_NAMES


def _read_json(response):
    if response is None:
        return None
    try:
        return response.json()
    except Exception:
        try:
            txt = response.text()
            return json.loads(txt) if txt else None
        except Exception:
            return None


def _looks_blocked(final_url):
    u = (final_url or "").lower()
    if "/accounts/login" in u or "/accounts/suspended" in u:
        return True
    if "/challenge" in u or "/checkpoint" in u:
        return True
    return False



# =========================================================
# 단일 계정 수집
# =========================================================
def collect_posts(page, username):
    """
    반환 dict:
      posts        : 그리드 + 릴스 통합 (최신순). 최대 POST_LIMIT + REELS_LIMIT
      raw_pages    : 캡처한 목록 GraphQL 응답들 (raw 저장용)
      html         : 프로필 HTML (그리드 탭 기준)
      final_url    : 최종 URL
      http_status  : goto 응답 코드
      blocked      : 차단 감지 여부
      n_grid/n_reels: 탭별 수집 개수 (진단용)
      reels_visited : 릴스 탭 방문 여부 (진단용)
      seen_names   : 관측한 friendly-name 집합 (진단용)

    ⚠️ 수집 경로가 둘이다 (2026-07 실측)
      ① 그리드 탭  /{username}/       → PolarisProfilePosts*Query
      ② 릴스 탭    /{username}/reels/ → PolarisProfileReelsTabContentQuery
      계정에 따라 그리드에 릴스가 전혀 노출되지 않아 릴스 탭을 항상 방문한다.
      한도는 탭별로 독립이되 seen 은 공유해 중복을 제거한다.
    """
    url = f"https://www.instagram.com/{username}/"
    reels_url = f"https://www.instagram.com/{username}/reels/"
    result = {
        "posts": [], "raw_pages": [], "html": None,
        "final_url": url, "http_status": None, "blocked": False,
        "n_grid": 0, "n_reels": 0,
        "reels_visited": False, "seen_names": set(),
    }

    # 탭별 버킷. seen 은 공유 → 그리드에 이미 잡힌 릴스는 중복 저장 안 됨.
    captured = {"pages": [], "posts": [], "reels": [], "seen": set(), "by_id": {}}

    def _on_response(response):
        name = _friendly_name(response)
        if name:
            result["seen_names"].add(name)
            _SEEN_QUERY_NAMES[name] = _SEEN_QUERY_NAMES.get(name, 0) + 1
            if DEBUG_GQL:
                known = (name in L2_POSTS_QUERY_NAMES
                         or name in L2_REELS_QUERY_NAMES)
                print(f"  [gql]{'*' if known else ' '} {name}", flush=True)

        if not _is_posts_graphql(response):
            return

        is_reels = name in L2_REELS_QUERY_NAMES
        bucket = captured["reels"] if is_reels else captured["posts"]
        limit = L2_REELS_LIMIT if is_reels else L2_POST_LIMIT
        if len(bucket) >= limit:
            return

        data = _read_json(response)
        if data is None:
            return
        captured["pages"].append(data)

        try:
            if is_reels:
                posts = _parse_reels(data)
            else:
                posts, _page_info = parse_posts(data)
        except Exception as e:
            if DEBUG_GQL:
                print(f"  [parse 실패] {name}: {e!r}", flush=True)
            return

        for p in posts or []:
            eid = p.get("external_id")
            if not eid:
                continue

            if eid in captured["seen"]:
                # ⚠️ 중복이라고 버리면 안 된다.
                #    그리드(timeline) 응답에는 조회수 필드가 아예 없고,
                #    릴스(clips) 응답에는 caption/taken_at 이 없다.
                #    같은 게시물이 양쪽에서 오면 서로의 빈 칸을 채워준다.
                #    (버리면 먼저 잡힌 그리드 버전이 이겨 조회수가 영영 NULL)
                prev = captured["by_id"].get(eid)
                if prev is not None:
                    for k, v in p.items():
                        if v is not None and prev.get(k) is None:
                            prev[k] = v
                continue

            captured["seen"].add(eid)
            captured["by_id"][eid] = p   # bucket 과 같은 객체를 참조
            bucket.append(p)
            if len(bucket) >= limit:
                return

    def _wait_grace(bucket_key, limit):
        deadline = time.monotonic() + (L2_GRAPHQL_GRACE_MS / 1000.0)
        while (len(captured[bucket_key]) < limit
               and time.monotonic() < deadline):
            page.wait_for_timeout(150)

    def _scroll_more(bucket_key, limit):
        prev = len(captured[bucket_key])
        stall = 0
        for _ in range(L2_MAX_SCROLLS):
            if len(captured[bucket_key]) >= limit:
                break
            try:
                page.mouse.wheel(0, 3000)
            except Exception:
                pass
            lo, hi = L2_SCROLL_DELAY
            page.wait_for_timeout(random.randint(int(lo), int(hi)))
            cur = len(captured[bucket_key])
            if cur == prev:
                stall += 1
                if stall >= L2_SCROLL_STALL:
                    break
            else:
                stall = 0
            prev = cur

    page.on("response", _on_response)

    try:
        # ===== ① 그리드 탭 =====
        try:
            resp = page.goto(url, wait_until="domcontentloaded",
                             timeout=L2_GOTO_TIMEOUT_MS)
            result["http_status"] = resp.status if resp else None
        except PWTimeout:
            pass
        except Exception:
            pass

        try:
            result["final_url"] = page.url
        except Exception:
            pass

        if _looks_blocked(result["final_url"]):
            result["blocked"] = True

        if not result["blocked"]:
            _wait_grace("posts", L2_POST_LIMIT)
            if len(captured["posts"]) < L2_POST_LIMIT:
                _scroll_more("posts", L2_POST_LIMIT)

        # 프로필 HTML 은 릴스 탭으로 넘어가기 전에 떠야 한다
        try:
            result["html"] = page.content()
        except Exception:
            result["html"] = None

        # ===== ② 릴스 탭 (항상 방문) =====
        # ⚠️ 리스너가 살아있는 이 블록 안에서 방문해야 캡처된다.
        if L2_COLLECT_REELS and not result["blocked"]:
            try:
                page.goto(reels_url, wait_until="domcontentloaded",
                          timeout=L2_GOTO_TIMEOUT_MS)
                result["reels_visited"] = True

                if _looks_blocked(page.url):
                    result["blocked"] = True
                else:
                    _wait_grace("reels", L2_REELS_LIMIT)
                    if len(captured["reels"]) < L2_REELS_LIMIT:
                        _scroll_more("reels", L2_REELS_LIMIT)
            except PWTimeout:
                pass
            except Exception as e:
                if DEBUG_GQL:
                    print(f"  [reels 탭 실패] {e!r}", flush=True)

    finally:
        # ⚠️ page 는 전 계정에서 재사용된다. 리스너를 반드시 해제할 것.
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    if result["html"] is None:
        try:
            result["html"] = page.content()
        except Exception:
            result["html"] = None

    # 탭별 한도만큼 취해 합치고 최신순 정렬.
    # 릴스의 taken_at 은 pk 복원값이라 정밀도가 낮다(L3 에서 보정).
    result["n_grid"] = len(captured["posts"])
    result["n_reels"] = len(captured["reels"])
    merged = (captured["posts"][:L2_POST_LIMIT]
              + captured["reels"][:L2_REELS_LIMIT])
    merged.sort(key=lambda p: p.get("taken_at") or 0, reverse=True)

    result["posts"] = merged
    result["raw_pages"] = captured["pages"]
    return result


# =========================================================
# DB 저장
# =========================================================
def save_to_db(conn, channel_id, posts, captured_at=None):
    if captured_at is None:
        captured_at = datetime.now()
    saved = 0
    with conn.cursor() as cur:
        for p in posts:
            if not p.get("external_id"):
                continue
            cur.execute(INSERT_CONTENT, (
                channel_id,
                p["external_id"],
                p.get("content_type", "feed_image"),
                p.get("is_paid_promotion", 0),
                _dt(p.get("taken_at")),
                p.get("duration_sec"),
                p.get("caption_text"),
                captured_at,
            ))
            content_id = cur.lastrowid
            if not content_id:
                cur.execute(
                    "SELECT content_id FROM contents "
                    "WHERE channel_id=%s AND external_id=%s",
                    (channel_id, p["external_id"]),
                )
                row = cur.fetchone()
                if not row:
                    continue
                content_id = row[0]

            cur.execute(INSERT_CSNAP, (
                content_id, captured_at,
                p.get("view_count"), p.get("like_count"), p.get("comment_count"),
            ))
            saved += 1
    conn.commit()
    return saved


def log_l2(conn, channel_id, url, status, http_status=None,
           err_type=None, err_detail=None):
    with conn.cursor() as cur:
        cur.execute(INSERT_LOG, (channel_id, url, status, http_status,
                                 err_type, err_detail, datetime.now()))
    conn.commit()


# =========================================================
# 대상 조회 (channels 에서 인스타 SUCCESS 계정)
# =========================================================
def fetch_targets(conn, limit=None, resume=True):
    """
    L1 이 channels 에 인스타 계정을 적재했다는 전제.
    resume: 이미 L2 success/empty 인 채널 제외.

    ⚠️ TODO: L1 이 channels 를 어떻게 채우는지 확정되면
       - channel_existence_status='normal' (공개 계정) 필터 등 조건 조정.
       - is_private 계정 제외 조건 확인.
    """
    sql = """
    SELECT c.channel_id, c.channel_name, c.channel_url_normalized
    FROM channels c
    WHERE c.platform=%s
      AND c.external_channel_id IS NOT NULL
      AND c.channel_existence_status IN ('normal','unknown')
    """
    params = [PLATFORM]

    if resume:
        sql += """
        AND NOT EXISTS (
            SELECT 1 FROM crawl_logs l
            WHERE l.channel_id=c.channel_id
              AND l.layer='L2'
              AND (l.status='success' OR l.error_type='empty')
        )
        """

    sql += " ORDER BY c.channel_id"
    if limit:
        sql += " LIMIT %d" % int(limit)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _username_from_row(url):
    """channel_url_normalized 에서 username 추출."""
    if not url:
        return None

    m = re.search(r"instagram\.com/([^/?#]+)", url)
    if m:
        return m.group(1)

    return None


# =========================================================
# 메인
# =========================================================
def run(limit=BATCH_LIMIT, headless=HEADLESS, resume=True):
    log = setup_logging()

    if pymysql is None:
        log.error("pymysql 필요: pip install pymysql")
        return
    if not SESSION_FILE.exists():
        log.error("세션 파일 없음: %s (login.py 먼저 실행)", SESSION_FILE)
        return

    conn = pymysql.connect(**DB)
    try:
        targets = fetch_targets(conn, limit, resume)
        log.info("[L2] 대상 계정: %d개", len(targets))
        if not targets:
            log.info("처리할 계정 없음 (다 끝났거나 channels 에 인스타 행이 없음).")
            return

        ok = none = err = blocked = 0
        block_streak = 0

        # 목록 수집에 쓰이는 쿼리 이름 전체 (그리드 + 릴스).
        # 미등록 경고 판정에 사용 — 릴스 쿼리를 빼먹으면 오탐이 난다.
        known_names = set(L2_POSTS_QUERY_NAMES) | set(L2_REELS_QUERY_NAMES)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            # ⚠️ 컨텍스트 옵션은 login.py 와 반드시 동일해야 한다.
            #    UA/viewport/locale 이 어긋나면 인스타는
            #    "같은 쿠키인데 다른 브라우저"로 보고 세션을 의심한다.
            context = browser.new_context(
                **context_kwargs(storage_state=SESSION_FILE)
            )

            # page 는 전 계정에서 재사용한다. 생성은 딱 1회.
            page = context.new_page()
            _stealth.apply_stealth_sync(page)

            try:
                for i, (channel_id, channel_name, url_norm) in enumerate(targets, 1):
                    username = _username_from_row(url_norm)
                    if not username:
                        err += 1
                        log_l2(conn, channel_id, url_norm or "", STATUS_FAILED,
                               None, "no_username", None)
                        log.info("  [%d/%d] SKIP (username 없음) ch=%s",
                                 i, len(targets), channel_id)
                        continue

                    url = f"https://www.instagram.com/{username}/"
                    try:
                        r = collect_posts(page, username)

                        # raw 항상 저장
                        if r["html"]:
                            save_l2_html(username, r["html"])
                        if r["raw_pages"]:
                            save_l2_graphql(username, r["raw_pages"])

                        if r["blocked"]:
                            blocked += 1
                            block_streak += 1
                            log_l2(conn, channel_id, url, STATUS_FAILED,
                                   r["http_status"], "blocked", r["final_url"])
                            log.warning("  [%d/%d] BLOCK @%s -> %s",
                                        i, len(targets), username, r["final_url"])
                            if block_streak >= STOP_ON_BLOCK:
                                log.error(
                                    "[L2] 연속 차단 %d회 -> 중단. "
                                    "레이트 리밋일 수 있으니 쉬었다 재개하세요.",
                                    STOP_ON_BLOCK)
                                break
                            continue

                        if not r["posts"]:
                            none += 1
                            block_streak = 0
                            log_l2(conn, channel_id, url, STATUS_OK,
                                   r["http_status"], "empty", None)
                            # 목록 쿼리를 하나도 못 잡았으면 '진짜 빈 계정'이
                            # 아니라 friendly-name 미등록일 수 있다.
                            unmatched = r["seen_names"] - known_names
                            if not r["raw_pages"] and unmatched:
                                log.warning(
                                    "  [%d/%d] NONE  @%s (게시물 0) "
                                    "⚠️ 미매칭 쿼리: %s",
                                    i, len(targets), username,
                                    ", ".join(sorted(unmatched)),
                                )
                            else:
                                log.info("  [%d/%d] NONE  @%s (게시물 0)",
                                         i, len(targets), username)
                            continue

                        n = save_to_db(conn, channel_id, r["posts"])
                        ok += 1
                        block_streak = 0
                        log_l2(conn, channel_id, url, STATUS_OK,
                               r["http_status"], None, None)
                        log.info("  [%d/%d] OK    @%s (posts=%d g=%d r=%d)",
                                 i, len(targets), username, n,
                                 r["n_grid"], r["n_reels"])

                    except Exception as e:
                        err += 1
                        log_l2(conn, channel_id, url, STATUS_FAILED,
                               None, "exception", str(e)[:400])
                        log.info("  [%d/%d] ERR   @%s | %r",
                                 i, len(targets), username, e)

                    lo, hi = L2_CHANNEL_GAP
                    time.sleep(random.uniform(lo, hi))

            finally:
                # 크롤링 중 롤링된 쿠키를 다시 저장해 세션 수명을 늘린다.
                # ⚠️ 예외/중단 시에도 반드시 실행되도록 finally 에 둔다.
                try:
                    context.storage_state(path=str(SESSION_FILE))
                except Exception as e:
                    log.warning("세션 저장 실패: %r", e)
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

        log.info("[L2] 완료: OK=%d NONE=%d BLOCK=%d ERR=%d",
                 ok, none, blocked, err)

        # 관측된 friendly-name 요약 — 미등록 이름이 있으면 config 에 추가할 것
        if _SEEN_QUERY_NAMES:
            unknown = {k: v for k, v in _SEEN_QUERY_NAMES.items()
                       if k not in known_names}
            if unknown:
                log.warning("[L2] 미등록 friendly-name (config 확인 필요):")
                for k, v in sorted(unknown.items(), key=lambda x: -x[1])[:15]:
                    log.warning("      %6d회  %s", v, k)
    finally:
        conn.close()

if __name__ == "__main__":
    run(limit=BATCH_LIMIT, headless=HEADLESS, resume=True)