# -*- coding: utf-8 -*-
"""
steps/l2.py  (Instagram L2 — 게시물 목록 수집)

설계 원칙 (L1 계승 + 프로젝트 문서 기준)
- L1 과 동일하게 Playwright 단일 세션 + 브라우저 goto + 리스너 캡처.
  request 직접 호출(찍어내기)은 인스타에서 429 유발 → 사용 안 함.
- 게시물 목록 GraphQL(PolarisProfilePostsTabContentQuery_connection)만 캡처.
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
"""

import json
import re
import time
import random
import logging
from datetime import datetime

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

# ── config 연동 (실행 방식에 따라 import 경로 대응) ──
try:
    from instagram.config import (
        SESSION_FILE, OUTPUT_DIR, L2_HTML_DIR, L2_GRAPHQL_DIR,
        LOG_DIR, L2_LOG_FILE, LOCALE, USER_AGENT, HEADLESS, DB, PLATFORM,
        L2_GRAPHQL_URL_PART, L2_POSTS_QUERY_NAME, L2_POST_LIMIT,
        L2_MAX_SCROLLS, L2_SCROLL_STALL, L2_GOTO_TIMEOUT_MS,
        L2_GRAPHQL_GRACE_MS, L2_RENDER_WAIT_MS, L2_SCROLL_DELAY, L2_CHANNEL_GAP,
        BATCH_LIMIT, STOP_ON_BLOCK,
    )
except Exception:
    from config import (
        SESSION_FILE, OUTPUT_DIR, L2_HTML_DIR, L2_GRAPHQL_DIR,
        LOG_DIR, L2_LOG_FILE, LOCALE, USER_AGENT, HEADLESS, DB, PLATFORM,
        L2_GRAPHQL_URL_PART, L2_POSTS_QUERY_NAME, L2_POST_LIMIT,
        L2_MAX_SCROLLS, L2_SCROLL_STALL, L2_GOTO_TIMEOUT_MS,
        L2_GRAPHQL_GRACE_MS, L2_RENDER_WAIT_MS, L2_SCROLL_DELAY, L2_CHANNEL_GAP,
        BATCH_LIMIT, STOP_ON_BLOCK,
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
# 상태 상수
# =========================================================
STATUS_OK = "success"
STATUS_FAILED = "failed"


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
        return datetime.utcfromtimestamp(int(unix_sec))
    except Exception:
        return None


# =========================================================
# GraphQL 캡처 판별
# =========================================================
def _is_posts_graphql(response):
    """게시물 목록 GraphQL 응답인지 (요청 헤더 friendly-name 기준)."""
    try:
        req = response.request
        if req.method != "POST":
            return False
        if L2_GRAPHQL_URL_PART not in response.url:
            return False
        return req.headers.get("x-fb-friendly-name") == L2_POSTS_QUERY_NAME
    except Exception:
        return False


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
      posts        : 정규화된 게시물 리스트 (최대 L2_POST_LIMIT)
      raw_pages    : 캡처한 목록 GraphQL 응답들 (raw 저장용)
      html         : 프로필 HTML
      final_url    : 최종 URL
      http_status  : goto 응답 코드
      blocked      : 차단 감지 여부
    """
    url = f"https://www.instagram.com/{username}/"
    result = {
        "posts": [], "raw_pages": [], "html": None,
        "final_url": url, "http_status": None, "blocked": False,
    }

    # --- 목록 GraphQL 캡처 리스너 (goto 이전 등록) ---
    captured = {"pages": [], "posts": [], "seen": set()}

    def _on_response(response):
        if len(captured["posts"]) >= L2_POST_LIMIT:
            return
        if not _is_posts_graphql(response):
            return
        data = _read_json(response)
        if data is None:
            return
        captured["pages"].append(data)
        posts, _page_info = parse_posts(data)
        for p in posts:
            eid = p.get("external_id")
            if eid and eid not in captured["seen"]:
                captured["seen"].add(eid)
                captured["posts"].append(p)
                if len(captured["posts"]) >= L2_POST_LIMIT:
                    return

    page.on("response", _on_response)

    try:
        # --- goto ---
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

        # 차단 URL 이면 즉시 종료
        if _looks_blocked(result["final_url"]):
            result["blocked"] = True

        # --- 목록 GraphQL 유예 대기 ---
        if not result["blocked"] and len(captured["posts"]) < L2_POST_LIMIT:
            deadline = time.monotonic() + (L2_GRAPHQL_GRACE_MS / 1000.0)
            while (len(captured["posts"]) < L2_POST_LIMIT
                   and time.monotonic() < deadline):
                page.wait_for_timeout(150)

        # --- 부족하면 스크롤로 추가 로드 ---
        if not result["blocked"] and len(captured["posts"]) < L2_POST_LIMIT:
            prev = len(captured["posts"])
            stall = 0
            for _ in range(L2_MAX_SCROLLS):
                if len(captured["posts"]) >= L2_POST_LIMIT:
                    break
                try:
                    page.mouse.wheel(0, 3000)
                except Exception:
                    pass
                lo, hi = L2_SCROLL_DELAY
                page.wait_for_timeout(random.randint(int(lo), int(hi)))
                cur = len(captured["posts"])
                if cur == prev:
                    stall += 1
                    if stall >= L2_SCROLL_STALL:
                        break
                else:
                    stall = 0
                prev = cur

    finally:
        page.remove_listener("response", _on_response)

    # --- HTML 항상 저장 ---
    try:
        result["html"] = page.content()
    except Exception:
        result["html"] = None

    result["posts"] = captured["posts"][:L2_POST_LIMIT]
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

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                storage_state=str(SESSION_FILE),
                locale=LOCALE,
                user_agent=USER_AGENT,
            )
            page = context.new_page()

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
                            log.error("[L2] 연속 차단 %d회 -> 세션 보호 위해 중단",
                                      STOP_ON_BLOCK)
                            break
                        continue

                    if not r["posts"]:
                        none += 1
                        block_streak = 0
                        log_l2(conn, channel_id, url, STATUS_OK,
                               r["http_status"], "empty", None)
                        log.info("  [%d/%d] NONE  @%s (게시물 0)",
                                 i, len(targets), username)
                        continue

                    n = save_to_db(conn, channel_id, r["posts"])
                    ok += 1
                    block_streak = 0
                    log_l2(conn, channel_id, url, STATUS_OK,
                           r["http_status"], None, None)
                    log.info("  [%d/%d] OK    @%s (posts=%d)",
                             i, len(targets), username, n)

                except Exception as e:
                    err += 1
                    log_l2(conn, channel_id, url, STATUS_FAILED,
                           None, "exception", str(e)[:400])
                    log.info("  [%d/%d] ERR   @%s | %r",
                             i, len(targets), username, e)

                lo, hi = L2_CHANNEL_GAP
                time.sleep(random.uniform(lo, hi))

            context.close()
            browser.close()

        log.info("[L2] 완료: OK=%d NONE=%d BLOCK=%d ERR=%d",
                 ok, none, blocked, err)
    finally:
        conn.close()


if __name__ == "__main__":
    run(limit=BATCH_LIMIT, headless=HEADLESS, resume=True)