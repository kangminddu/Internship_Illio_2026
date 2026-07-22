# -*- coding: utf-8 -*-
"""
steps/l3.py  (Instagram L3 — 댓글 수집)

설계 원칙 (L2 계승 + 프로젝트 문서 기준)
- L2와 동일하게 Playwright 단일 세션 + 브라우저 goto + 리스너 캡처 방식 사용.
- 게시물의 댓글 GraphQL(PolarisPostCommentsPaginationQuery)만 캡처.
- 각 게시물별로 최대 L3_COMMENT_LIMIT개의 댓글을 수집. 기간 필터 없음.
- 저장 3층: HTML(raw) / 댓글 GraphQL(raw) / 정규화(DB).
  DB는 fandom_crm 공유 스키마:
    channels → contents(게시물) → comments(댓글) / fans(작성자)
- 댓글 수집 대상은 프로필이 아니라 게시물(contents)임.
- 연속 차단(CHALLENGE/로그인/429) STOP_ON_BLOCK 회 → 세션 보호 위해 중단.
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
# ── config 연동 (실행 방식에 따라 import 경로 대응) ──
try:
    from instagram.config import (
        SESSION_FILE,
        OUTPUT_DIR,
        L3_HTML_DIR,
        L3_GRAPHQL_DIR,
        LOG_DIR,
        L3_LOG_FILE,
        LOCALE,
        USER_AGENT,
        HEADLESS,
        DB,
        PLATFORM,

        L3_GRAPHQL_URL_PART,
        L3_COMMENTS_QUERY_NAME,
        L3_COMMENT_LIMIT,

        L3_MAX_SCROLLS,
        L3_SCROLL_STALL,

        L3_GOTO_TIMEOUT_MS,
        L3_GRAPHQL_GRACE_MS,
        L3_RENDER_WAIT_MS,

        L3_SCROLL_DELAY,
        L3_CONTENT_GAP,

        BATCH_LIMIT,
        STOP_ON_BLOCK,
    )
except Exception:
    from config import (
        SESSION_FILE,
        OUTPUT_DIR,
        L3_HTML_DIR,
        L3_GRAPHQL_DIR,
        LOG_DIR,
        L3_LOG_FILE,
        LOCALE,
        USER_AGENT,
        HEADLESS,
        DB,
        PLATFORM,

        L3_GRAPHQL_URL_PART,
        L3_COMMENTS_QUERY_NAME,
        L3_COMMENT_LIMIT,

        L3_MAX_SCROLLS,
        L3_SCROLL_STALL,

        L3_GOTO_TIMEOUT_MS,
        L3_GRAPHQL_GRACE_MS,
        L3_RENDER_WAIT_MS,

        L3_SCROLL_DELAY,
        L3_CONTENT_GAP,

        BATCH_LIMIT,
        STOP_ON_BLOCK,
    )

try:
    from instagram.lib.comments_parser import parse_comments
except Exception:
    from lib.comments_parser import parse_comments

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
# L3: 팬(fans) / 댓글(comments) 저장 SQL
INSERT_FAN = (
    "INSERT INTO fans "
    "(platform, external_author_id, first_seen_at, last_seen_at) "
    "VALUES (%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "last_seen_at=VALUES(last_seen_at), "
    "updated_at=CURRENT_TIMESTAMP()"
)

INSERT_COMMENT = (
    "INSERT INTO comments "
    "(content_id, fan_id, external_comment_id, parent_comment_id, "
    " author_display_name, comment_text, published_at, like_count) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "author_display_name=VALUES(author_display_name), "
    "comment_text=VALUES(comment_text), "
    "like_count=VALUES(like_count), "
    "published_at=VALUES(published_at), "
    "collected_at=CURRENT_TIMESTAMP()"
)
INSERT_LOG = (
    "INSERT INTO crawl_logs "
    "(channel_id, target_url, layer, status, http_status, "
    " error_type, error_detail, attempted_at) "
    "VALUES (%s,%s,'L3',%s,%s,%s,%s,%s)"
)


# =========================================================
# 로깅
# =========================================================
def setup_logging():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("l3")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    fh = logging.FileHandler(L3_LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


# =========================================================
# 저장 헬퍼 (raw)
# =========================================================
def _safe_name(username):
    return re.sub(r"[^\w.-]", "_", username or "unknown")


def save_l3_html(external_id, html):
    L3_HTML_DIR.mkdir(parents=True, exist_ok=True)
    path = L3_HTML_DIR / f"{_safe_name(external_id)}.html"
    path.write_text(html, encoding="utf-8")
    return path


def save_l3_graphql(external_id, pages):
    """pages: 캡처한 목록 GraphQL 응답들의 리스트 (페이지별 verbatim)."""
    L3_GRAPHQL_DIR.mkdir(parents=True, exist_ok=True)
    path = L3_GRAPHQL_DIR / f"{_safe_name(external_id)}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=2)
    return path


# =========================================================
# GraphQL 캡처 판별
# =========================================================
def _is_comments_graphql(response):
    """게시물 목록 GraphQL 응답인지 (요청 헤더 friendly-name 기준)."""
    try:
        req = response.request
        if req.method != "POST":
            return False
        if L3_GRAPHQL_URL_PART not in response.url:
            return False
        return req.headers.get("x-fb-friendly-name") == L3_COMMENTS_QUERY_NAME
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
def collect_comments(page, external_id):
    """
    반환 dict:
      comments     : 정규화된 댓글 리스트 (최대 L3_COMMENT_LIMIT)
      raw_pages    : 캡처한 댓글 GraphQL 응답들 (raw 저장용)
      html         : 게시물 HTML
      final_url    : 최종 URL
      http_status  : goto 응답 코드
      blocked      : 차단 감지 여부
    """
    url = f"https://www.instagram.com/p/{external_id}/"
    result = {
        "comments": [], "raw_pages": [], "html": None,
        "final_url": url, "http_status": None, "blocked": False,
    }

    # --- 목록 GraphQL 캡처 리스너 (goto 이전 등록) ---
    captured = {"pages": [], "comments": [], "seen": set()}

    def _on_response(response):
        if len(captured["comments"]) >= L3_COMMENT_LIMIT:
            return
        if not _is_comments_graphql(response):
            return
        data = _read_json(response)
        if data is None:
            return
        captured["pages"].append(data)
        comments = parse_comments(data)
        for p in comments:
            eid = p.get("external_comment_id")
            if eid and eid not in captured["seen"]:
                captured["seen"].add(eid)
                captured["comments"].append(p)
                if len(captured["comments"]) >= L3_COMMENT_LIMIT:
                    return

    page.on("response", _on_response)

    try:
        # --- goto ---
        try:
            resp = page.goto(url, wait_until="domcontentloaded",
                             timeout=L3_GOTO_TIMEOUT_MS)
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
        if not result["blocked"] and len(captured["comments"]) < L3_COMMENT_LIMIT:
            deadline = time.monotonic() + (L3_GRAPHQL_GRACE_MS / 1000.0)
            while (len(captured["comments"]) < L3_COMMENT_LIMIT
                   and time.monotonic() < deadline):
                page.wait_for_timeout(150)

        # --- 부족하면 스크롤로 추가 로드 ---
        if not result["blocked"] and len(captured["comments"]) < L3_COMMENT_LIMIT:
            prev = len(captured["comments"])
            stall = 0
            for _ in range(L3_MAX_SCROLLS):
                if len(captured["comments"]) >= L3_COMMENT_LIMIT:
                    break
                try:
                    page.mouse.wheel(0, 3000)
                except Exception:
                    pass
                lo, hi = L3_SCROLL_DELAY
                page.wait_for_timeout(random.randint(int(lo), int(hi)))
                cur = len(captured["comments"])
                if cur == prev:
                    stall += 1
                    if stall >= L3_SCROLL_STALL:
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

    result["comments"] = captured["comments"][:L3_COMMENT_LIMIT]
    result["raw_pages"] = captured["pages"]
    return result


# =========================================================
# DB 저장
# =========================================================
def save_to_db(conn, content_id, comments):
    saved = 0

    with conn.cursor() as cur:
        for c in comments:
            author_id = c.get("external_author_id")
            if not author_id:
                continue

            now = datetime.now()
            cur.execute(INSERT_FAN, (
                PLATFORM,
                author_id,
                now,
                now,
            ))
            fan_id = cur.lastrowid
            if not fan_id:
                cur.execute(
                    "SELECT fan_id FROM fans WHERE platform=%s AND external_author_id=%s",
                    (PLATFORM, author_id),
                )
                row = cur.fetchone()
                if not row:
                    continue
                fan_id = row[0]

            cur.execute(INSERT_COMMENT, (
                content_id,
                fan_id,
                c.get("external_comment_id"),
                None,
                c.get("author_display_name"),
                c.get("comment_text"),
                c.get("published_at"),
                c.get("like_count", 0),
            ))
            saved += 1

    conn.commit()
    return saved


def log_l3(conn, channel_id, url, status, http_status=None,
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
    L3 대상: 댓글을 수집할 게시물(contents).
    이미 L3 성공한 게시물은 resume=True일 때 제외.
    """
    sql = """
    SELECT ct.content_id, ct.channel_id, ct.external_id
    FROM contents ct
    JOIN channels ch
      ON ct.channel_id = ch.channel_id
    WHERE ch.platform=%s
      AND ct.external_id IS NOT NULL
    """
    params = [PLATFORM]

    if resume:
        sql += """
        AND NOT EXISTS (
            SELECT 1
            FROM crawl_logs l
            WHERE l.channel_id = ct.channel_id
              AND l.target_url = CONCAT('https://www.instagram.com/p/', ct.external_id, '/')
              AND l.layer='L3'
              AND l.status='success'
        )
        """

    sql += " ORDER BY ct.content_id"

    if limit:
        sql += " LIMIT %d" % int(limit)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()




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
        log.info("[L3] 대상 게시물: %d개", len(targets))
        if not targets:
            log.info("처리할 게시물 없음 (다 끝났거나 contents에 인스타 게시물이 없음).")
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

            for i, (content_id, channel_id, external_id) in enumerate(targets, 1):
                url = f"https://www.instagram.com/p/{external_id}/"
                try:
                    r = collect_comments(page, external_id)

                    # raw 항상 저장
                    if r["html"]:
                        save_l3_html(external_id, r["html"])
                    if r["raw_pages"]:
                        save_l3_graphql(external_id, r["raw_pages"])

                    if r["blocked"]:
                        blocked += 1
                        block_streak += 1
                        log_l3(conn, channel_id, url, STATUS_FAILED,
                               r["http_status"], "blocked", r["final_url"])
                        log.warning("  [%d/%d] BLOCK %s -> %s",
                                    i, len(targets), external_id, r["final_url"])
                        if block_streak >= STOP_ON_BLOCK:
                            log.error("[L3] 연속 차단 %d회 -> 세션 보호 위해 중단",
                                      STOP_ON_BLOCK)
                            break
                        continue

                    if not r["comments"]:
                        none += 1
                        block_streak = 0
                        log_l3(conn, channel_id, url, STATUS_OK,
                               r["http_status"], "empty", None)
                        log.info("  [%d/%d] NONE  %s (댓글 0)",
                                 i, len(targets), external_id)
                        continue

                    n = save_to_db(conn, content_id, r["comments"])
                    ok += 1
                    block_streak = 0
                    log_l3(conn, channel_id, url, STATUS_OK,
                           r["http_status"], None, None)
                    log.info("  [%d/%d] OK    %s (comments=%d)",
                             i, len(targets), external_id, n)

                except Exception as e:
                    err += 1
                    log_l3(conn, channel_id, url, STATUS_FAILED,
                           None, "exception", str(e)[:400])
                    log.info("  [%d/%d] ERR   %s | %r",
                             i, len(targets), external_id, e)

                lo, hi = L3_CONTENT_GAP
                time.sleep(random.uniform(lo, hi))

            context.close()
            browser.close()

        log.info("[L3] 완료: OK=%d NONE=%d BLOCK=%d ERR=%d",
                 ok, none, blocked, err)
    finally:
        conn.close()


if __name__ == "__main__":
    run(limit=BATCH_LIMIT, headless=HEADLESS, resume=True)