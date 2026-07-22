# -*- coding: utf-8 -*-
"""
steps/l3.py  (Instagram L3 — 댓글 수집 + 미디어 메타 보강)

설계 원칙 (L2 계승 + 프로젝트 문서 기준)
- L2와 동일하게 Playwright 단일 세션 + 브라우저 goto + 리스너 캡처 방식 사용.
- 각 게시물별로 최대 L3_COMMENT_LIMIT개의 댓글을 수집. 기간 필터 없음.
- 저장 3층: HTML(raw) / 댓글 GraphQL(raw) / 정규화(DB).
  DB는 fandom_crm 공유 스키마:
    channels → contents(게시물) → comments(댓글) / fans(작성자)
- 댓글 수집 대상은 프로필이 아니라 게시물(contents)임.
- 연속 차단(CHALLENGE/로그인/429) STOP_ON_BLOCK 회 → 세션 보호 위해 중단.

⚠️ 댓글 수집 경로 (2026-07 실측)
  게시물 페이지(/p/{shortcode}/)에 진입하면 인스타는 첫 댓글 묶음을
  **서버 렌더링(SSR)** 으로 HTML 안에 박아서 내려준다.
  즉 댓글 GraphQL 요청이 아예 발생하지 않는다.
    → response 리스너만으로는 영원히 0건. (쿼리 이름 문제가 아님)
    → HTML 인라인 JSON 을 파싱하는 폴백이 주 경로다.
  GraphQL(PaginationQuery)은 "댓글 더 보기"/댓글 컨테이너 스크롤 시에만 발생.
  두 경로 모두 root_field 가 동일해서 parse_comments 를 공유한다:
    xdt_api__v1__media__media_id__comments__connection

⚠️ 미디어 메타 보강 (L2 릴스 누락분 복구)
  L2 릴스 탭(clips) 응답에는 taken_at / caption / video_duration 이 없다.
  그래서 릴스는 published_at 이 pk 복원값(부정확), caption 은 NULL 로 들어간다.
  L3 는 어차피 게시물 페이지를 개별 방문하므로, 같은 SSR HTML 에서
  미디어 메타를 함께 뽑아 contents 를 UPDATE 한다. 추가 요청 비용 0.
  (L3_BACKFILL_MEDIA=False 로 끄면 댓글만 수집)

디버그:
- 환경변수 IG_DEBUG_GQL=1 로 실행하면 오가는 friendly-name 을 전부 출력.
    IG_DEBUG_GQL=1 python -m instagram.steps.l3
  '*' 표시가 매칭된 쿼리. 새 이름이 보이면 config 에 추가할 것.
"""

import os
import json
import re
import time
import random
import logging
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

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
        L3_COMMENTS_QUERY_NAMES,
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
        L3_COMMENTS_QUERY_NAMES,
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
# 상태 상수 / 플래그
# =========================================================
STATUS_OK = "success"
STATUS_FAILED = "failed"

DEBUG_GQL = os.environ.get("IG_DEBUG_GQL") == "1"

# 미디어 메타(caption/taken_at/duration) 보강 여부.
# L2 릴스 탭이 이 필드들을 안 주기 때문에 기본 ON.
L3_BACKFILL_MEDIA = True

# 관측된 friendly-name 누적 (실행 끝에 요약 출력).
_SEEN_QUERY_NAMES = {}

# SSR / GraphQL 공통 root_field
COMMENTS_ROOT = "xdt_api__v1__media__media_id__comments__connection"

_INLINE_JSON_RE = re.compile(
    r'<script[^>]+type="application/json"[^>]*>(.*?)</script>', re.S
)


# =========================================================
# DB SQL (fandom_crm 공유 스키마)
# =========================================================
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

# 미디어 메타 보강. COALESCE 로 추출 실패(NULL)시 기존 값을 보존한다.
UPDATE_CONTENT_META = (
    "UPDATE contents SET "
    " caption_text = COALESCE(%s, caption_text), "
    " published_at = COALESCE(%s, published_at), "
    " duration_sec = COALESCE(%s, duration_sec) "
    "WHERE content_id = %s"
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
def _safe_name(name):
    return re.sub(r"[^\w.-]", "_", name or "unknown")


def save_l3_html(external_id, html):
    L3_HTML_DIR.mkdir(parents=True, exist_ok=True)
    path = L3_HTML_DIR / f"{_safe_name(external_id)}.html"
    path.write_text(html, encoding="utf-8")
    return path


def save_l3_graphql(external_id, pages):
    """pages: 캡처한 댓글 payload 들의 리스트 (GraphQL 응답 또는 SSR 추출본)."""
    L3_GRAPHQL_DIR.mkdir(parents=True, exist_ok=True)
    path = L3_GRAPHQL_DIR / f"{_safe_name(external_id)}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=2)
    return path


def _dt(value):
    """
    unix초 / datetime / 문자열을 datetime 으로 정규화.
    parse_comments 가 어떤 형태를 주든 받아넘긴다.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).replace(tzinfo=None)
    except Exception:
        return value  # 문자열 datetime 등은 DB 가 파싱하도록 그대로


# =========================================================
# GraphQL 캡처 판별
# =========================================================
def _friendly_name(response):
    """요청 헤더의 x-fb-friendly-name. GraphQL POST 가 아니면 None."""
    try:
        req = response.request
        if req.method != "POST":
            return None
        if L3_GRAPHQL_URL_PART not in response.url:
            return None
        return req.headers.get("x-fb-friendly-name")
    except Exception:
        return None


def _is_comments_graphql(response):
    """
    댓글 GraphQL 응답인지 판별.
    ⚠️ 단일 이름 비교(==) 금지 — 상황에 따라 다른 쿼리명이 온다.
    """
    name = _friendly_name(response)
    if name is None:
        return False
    return name in L3_COMMENTS_QUERY_NAMES


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
# SSR HTML 파싱 — 댓글
# =========================================================
def _find_graphql_payload(obj):
    """
    SSR 인라인 JSON 은 댓글 데이터를
      require → RelayPrefetchedStreamCache → __bbox.result.data → xdt_api__...
    처럼 깊이 묻어둔다.
    중첩 어디에 있든 {"data": {"xdt_api__...": ...}} 형태를 찾아 반환한다.
    → parse_comments 가 GraphQL 응답과 동일한 모양으로 처리할 수 있다.
    """
    if isinstance(obj, dict):
        d = obj.get("data")
        if isinstance(d, dict) and COMMENTS_ROOT in d:
            return {"data": d}
        for v in obj.values():
            r = _find_graphql_payload(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_graphql_payload(v)
            if r:
                return r
    return None


def extract_comments_payload_from_html(html):
    """SSR HTML 에서 댓글 payload 를 추출. 없으면 None."""
    if not html:
        return None
    for m in _INLINE_JSON_RE.finditer(html):
        s = m.group(1)
        if COMMENTS_ROOT not in s:
            continue
        try:
            blob = json.loads(s)
        except Exception:
            continue
        payload = _find_graphql_payload(blob)
        if payload:
            return payload
    return None


# =========================================================
# SSR HTML 파싱 — 미디어 메타 (L2 릴스 누락분 보강)
# =========================================================
def _find_media_node(obj, code):
    """
    SSR JSON 에서 이 게시물의 미디어 노드를 찾는다.
    root_field 이름이 버전마다 달라질 수 있으므로 이름이 아니라
    '내용'으로 식별한다: code 가 일치하고 taken_at 또는 caption 을 가진 dict.
    """
    if isinstance(obj, dict):
        if obj.get("code") == code and (
            "taken_at" in obj or "caption" in obj or "video_duration" in obj
        ):
            return obj
        for v in obj.values():
            r = _find_media_node(v, code)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_media_node(v, code)
            if r:
                return r
    return None


def extract_media_meta_from_html(html, code):
    """
    SSR HTML 에서 caption_text / taken_at / duration_sec 을 추출.
    L2 릴스 탭 응답에 없는 필드들이라 여기서 보강한다.
    반환: dict 또는 None (하나도 못 뽑으면 None)
    """
    if not html or not code:
        return None

    for m in _INLINE_JSON_RE.finditer(html):
        s = m.group(1)
        # 값싼 사전 필터 — code 가 없는 블록은 파싱조차 하지 않는다
        if code not in s:
            continue
        if "taken_at" not in s and "caption" not in s:
            continue
        try:
            blob = json.loads(s)
        except Exception:
            continue

        node = _find_media_node(blob, code)
        if not node:
            continue

        caption = node.get("caption")
        cap_text = caption.get("text") if isinstance(caption, dict) else None
        dur = node.get("video_duration")
        meta = {
            "caption_text": cap_text,
            "taken_at": node.get("taken_at"),
            "duration_sec": int(dur) if dur else None,
        }
        if any(v is not None for v in meta.values()):
            return meta
    return None


# =========================================================
# 단일 게시물 수집
# =========================================================
def collect_comments(page, external_id):
    """
    반환 dict:
      comments     : 정규화된 댓글 리스트 (최대 L3_COMMENT_LIMIT)
      raw_pages    : 캡처한 댓글 payload 들 (raw 저장용)
      html         : 게시물 HTML
      final_url    : 최종 URL
      http_status  : goto 응답 코드
      blocked      : 차단 감지 여부
      from_html    : SSR 폴백으로 댓글을 수집했는지 (진단용)
      media_meta   : SSR 에서 뽑은 미디어 메타 (없으면 None)
      seen_names   : 이 게시물에서 관측한 friendly-name 집합 (진단용)
    """
    url = f"https://www.instagram.com/p/{external_id}/"
    result = {
        "comments": [], "raw_pages": [], "html": None,
        "final_url": url, "http_status": None, "blocked": False,
        "from_html": False, "media_meta": None, "seen_names": set(),
    }

    # --- 댓글 GraphQL 캡처 리스너 (goto 이전 등록) ---
    # SSR 이 주 경로지만, 스크롤/더보기로 GraphQL 이 오는 경우도 있어 유지.
    captured = {"pages": [], "comments": [], "seen": set()}

    def _add_comments(comments):
        """중복 제거하며 담는다. 한도 도달 시 True 반환."""
        for c in comments or []:
            eid = c.get("external_comment_id")
            if eid and eid not in captured["seen"]:
                captured["seen"].add(eid)
                captured["comments"].append(c)
                if len(captured["comments"]) >= L3_COMMENT_LIMIT:
                    return True
        return False

    def _on_response(response):
        # friendly-name 관측 (매칭 여부와 무관하게 기록 — 새 이름 발견용)
        name = _friendly_name(response)
        if name:
            result["seen_names"].add(name)
            _SEEN_QUERY_NAMES[name] = _SEEN_QUERY_NAMES.get(name, 0) + 1
            if DEBUG_GQL:
                hit = "*" if name in L3_COMMENTS_QUERY_NAMES else " "
                print(f"  [gql]{hit} {name}", flush=True)

        if len(captured["comments"]) >= L3_COMMENT_LIMIT:
            return
        if not _is_comments_graphql(response):
            return
        data = _read_json(response)
        if data is None:
            return
        captured["pages"].append(data)
        try:
            comments = parse_comments(data)
        except Exception as e:
            if DEBUG_GQL:
                print(f"  [parse_comments 실패] {name}: {e!r}", flush=True)
            return
        _add_comments(comments)

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

        # --- 렌더 대기 ---
        # SSR 이 주 경로라 GraphQL 을 오래 기다릴 필요는 없지만,
        # DOM 이 자리잡아야 page.content() 에 인라인 JSON 이 온전히 담긴다.
        if not result["blocked"]:
            page.wait_for_timeout(L3_RENDER_WAIT_MS)

        # --- 혹시 오는 GraphQL 유예 대기 ---
        if not result["blocked"] and len(captured["comments"]) < L3_COMMENT_LIMIT:
            deadline = time.monotonic() + (L3_GRAPHQL_GRACE_MS / 1000.0)
            while (len(captured["comments"]) < L3_COMMENT_LIMIT
                   and time.monotonic() < deadline):
                page.wait_for_timeout(150)

        # --- 부족하면 스크롤로 추가 로드 ---
        # ⚠️ 기본 config 는 L3_MAX_SCROLLS=0 이라 이 블록은 돌지 않는다.
        #    인스타 댓글은 본문이 아니라 별도 스크롤 컨테이너에 있어서
        #    mouse.wheel 로는 페이지네이션이 안 터질 수 있음.
        #    더 모으려면 컨테이너 직접 스크롤 구현이 필요하다.
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
        # ⚠️ page 는 전체 게시물에서 재사용된다. 리스너를 반드시 해제할 것.
        #    (해제 안 하면 게시물 수만큼 핸들러가 쌓여 뒤로 갈수록 느려지고,
        #     이전 게시물의 captured 에 계속 append 되어 메모리도 샌다)
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    # --- HTML 항상 저장 ---
    try:
        result["html"] = page.content()
    except Exception:
        result["html"] = None

    # --- SSR HTML 폴백: 댓글 (실질적인 주 경로) ---
    if not captured["comments"] and not result["blocked"]:
        payload = extract_comments_payload_from_html(result["html"])
        if payload:
            result["from_html"] = True
            try:
                comments = parse_comments(payload)
            except Exception as e:
                if DEBUG_GQL:
                    print(f"  [parse_comments(html) 실패] {e!r}", flush=True)
                comments = []
            _add_comments(comments)
            if captured["comments"]:
                captured["pages"].append(payload)

    # --- SSR HTML: 미디어 메타 (L2 릴스 누락분 보강) ---
    if L3_BACKFILL_MEDIA and not result["blocked"]:
        try:
            result["media_meta"] = extract_media_meta_from_html(
                result["html"], external_id
            )
        except Exception as e:
            if DEBUG_GQL:
                print(f"  [media_meta 추출 실패] {e!r}", flush=True)

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
                    "SELECT fan_id FROM fans "
                    "WHERE platform=%s AND external_author_id=%s",
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
                c.get("parent_comment_id"),
                c.get("author_display_name"),
                c.get("comment_text"),
                _dt(c.get("published_at")),
                c.get("like_count", 0),
            ))
            saved += 1

    conn.commit()
    return saved


def update_content_meta(conn, content_id, meta):
    """
    contents 의 caption_text / published_at / duration_sec 보강.
    L2 릴스 탭이 못 주는 필드들이고, published_at 은 pk 복원값이라
    여기서 정확한 값으로 덮어쓴다.
    반환: 실제로 갱신했으면 True
    """
    if not meta:
        return False
    caption = meta.get("caption_text")
    published = _dt(meta.get("taken_at"))
    duration = meta.get("duration_sec")
    if caption is None and published is None and duration is None:
        return False

    with conn.cursor() as cur:
        cur.execute(UPDATE_CONTENT_META,
                    (caption, published, duration, content_id))
    conn.commit()
    return True


def log_l3(conn, channel_id, url, status, http_status=None,
           err_type=None, err_detail=None):
    with conn.cursor() as cur:
        cur.execute(INSERT_LOG, (channel_id, url, status, http_status,
                                 err_type, err_detail, datetime.now()))
    conn.commit()


# =========================================================
# 대상 조회 (contents 에서 인스타 게시물)
# =========================================================
def fetch_targets(conn, limit=None, resume=True):
    """
    L3 대상: 댓글을 수집할 게시물(contents).
    resume: 이미 L3 success/empty 인 게시물 제외.

    ⚠️ empty 도 '처리 완료'로 간주한다 (L2 와 동일한 의미론).
       파서/수집경로를 고친 뒤 재수집하려면 empty 로그를 먼저 지울 것:
         DELETE FROM crawl_logs WHERE layer='L3' AND error_type='empty';
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
              AND l.target_url = CONCAT('https://www.instagram.com/p/',
                                        ct.external_id, '/')
              AND l.layer='L3'
              AND (l.status='success' OR l.error_type='empty')
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
        from_html = 0
        meta_filled = 0
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

                    # 미디어 메타 보강 — 댓글 유무와 무관하게 항상 시도.
                    # (댓글 0개인 릴스도 caption/published_at 은 채워야 한다)
                    meta_ok = False
                    if L3_BACKFILL_MEDIA:
                        try:
                            meta_ok = update_content_meta(
                                conn, content_id, r["media_meta"]
                            )
                        except Exception as e:
                            log.warning("  [%d/%d] META 실패 %s | %r",
                                        i, len(targets), external_id, e)
                    if meta_ok:
                        meta_filled += 1

                    if not r["comments"]:
                        none += 1
                        block_streak = 0
                        log_l3(conn, channel_id, url, STATUS_OK,
                               r["http_status"], "empty", None)
                        log.info("  [%d/%d] NONE  %s (댓글 0%s)",
                                 i, len(targets), external_id,
                                 " meta" if meta_ok else "")
                        continue

                    n = save_to_db(conn, content_id, r["comments"])
                    ok += 1
                    if r["from_html"]:
                        from_html += 1
                    block_streak = 0
                    log_l3(conn, channel_id, url, STATUS_OK,
                           r["http_status"], None, None)
                    log.info("  [%d/%d] OK    %s (comments=%d%s%s)",
                             i, len(targets), external_id, n,
                             " ssr" if r["from_html"] else "",
                             " meta" if meta_ok else "")

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

        log.info("[L3] 완료: OK=%d (SSR=%d) NONE=%d BLOCK=%d ERR=%d | META보강=%d",
                 ok, from_html, none, blocked, err, meta_filled)

        # 관측된 friendly-name 요약 — 미등록 이름이 있으면 config 확인
        if _SEEN_QUERY_NAMES:
            unknown = {k: v for k, v in _SEEN_QUERY_NAMES.items()
                       if k not in L3_COMMENTS_QUERY_NAMES}
            if unknown:
                log.info("[L3] 미등록 friendly-name (참고용):")
                for k, v in sorted(unknown.items(), key=lambda x: -x[1])[:15]:
                    log.info("      %6d회  %s", v, k)
    finally:
        conn.close()


if __name__ == "__main__":
    run(limit=BATCH_LIMIT, headless=HEADLESS, resume=True)