# -*- coding: utf-8 -*-
"""
steps/l3.py  (Instagram L3 — 댓글 수집 + 미디어 메타 보강)

★ 이 파일에는 사고 기록이 두 개 박혀 있다.
------
  ① SSR 발견 — 리스너만으로는 댓글을 영원히 0건으로 받는다
  ② 6,500건 헛돌이 — 레이트 리밋을 '댓글 0개'로 오인해 empty로 박제했다
  둘 다 아래 해당 위치에 상세히 적어둔다.

설계 원칙 (L2 계승 + 프로젝트 문서 기준)
- L2와 동일하게 Playwright 단일 세션 + 브라우저 goto + 리스너 캡처 방식 사용.
- 각 게시물별로 최대 L3_COMMENT_LIMIT개의 댓글을 수집. 기간 필터 없음.
- 저장 3층: HTML(raw) / 댓글 GraphQL(raw) / 정규화(DB).
  DB는 fandom_crm 공유 스키마:
    channels → contents(게시물) → comments(댓글) / fans(작성자)
- 댓글 수집 대상은 프로필이 아니라 게시물(contents)임.

  ↑ L1/L2는 '계정' 단위인데 L3만 '게시물' 단위다.
    그래서 resume도 게시물 단위로 걸린다. 채널 중간에 끊겨도
    처리한 게시물은 안 날아간다.
    (유튜브 L3는 채널 단위라 절반만 하고 죽으면 그 채널 전체를 다시 해야 한다.
     틱톡 L3는 인스타와 같은 영상 단위)

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

  ★ ①번 사고.
    L2에서 겪은 문제(쿼리 이름 미등록)를 예상하고 config에 후보를
    넓게 잡아뒀는데, 원인이 아예 달랐다.
    "이름을 잘못 잡은 게 아니라 요청 자체가 안 나간다."

    L1/L2에서 통했던 '리스너로 GraphQL 가로채기' 패턴이
    L3에서는 통하지 않았다. 같은 사이트인데 페이지 유형마다 다르다.
    → HTML 인라인 JSON 파싱이 주 경로가 됐고,
      리스너는 페이지네이션용 보조가 됐다.
    (코드 구조는 '리스너 우선, HTML 폴백'인데 실제 비중은 반대다.
     from_html 카운터로 실측 비율을 볼 수 있게 해뒀다)

⚠️ 차단 감지 (2026-07-23 사고 기록)
  레이트 리밋에 걸리면 인스타는 빈 페이지를 즉시 돌려준다.
  URL 은 /p/... 그대로고 리다이렉트도 없어서 _looks_blocked 를 통과한다.
  예전 코드는 이걸 '댓글 0'으로 오인해 6,500건을 헛돌며 empty 로 기록했다.
  → 아래 3중 감지로 막는다:
     ① goto 응답 4xx/5xx  ② goto 예외(ERR_HTTP_RESPONSE_CODE_FAILURE/429)
     ③ HTML 에 COMMENTS_ROOT 구조 자체가 없음
  정상 페이지는 댓글이 0개여도 comments__connection 구조는 존재한다.

  ★ ②번 사고. 프로젝트 전체를 관통하는 주제의 가장 큰 사례다.

    "데이터가 없다"와 "가져오지 못했다"를 구분 못 해서
    6,500건이 empty로 기록됐고, resume이 그걸 영구 제외했다.
    로그상으로는 전부 정상(success/empty)이라 며칠간 몰랐다.

    ③번 감지가 특히 중요하다. 인스타가 200 + 빈 페이지를 주니
    HTTP 상태로도, URL로도 구분이 안 된다.
    "정상 페이지라면 반드시 있어야 할 구조"의 유무로 판별한다.
    → 유튜브 L3가 Google sorry 페이지를 감지하는 것과 같은 발상인데,
      인스타는 눈에 보이는 신호가 없어서 더 어려웠다.

⚠️ 미디어 메타 보강 (L2 릴스 누락분 복구)
  L2 릴스 탭(clips) 응답에는 taken_at / caption / video_duration 이 없다.
  그래서 릴스는 published_at 이 pk 복원값(부정확), caption 은 NULL 로 들어간다.
  L3 는 어차피 게시물 페이지를 개별 방문하므로, 같은 SSR HTML 에서
  미디어 메타를 함께 뽑아 contents 를 UPDATE 한다. 추가 요청 비용 0.
  (L3_BACKFILL_MEDIA=False 로 끄면 댓글만 수집)

  ★ 유튜브 L2b가 쇼츠 게시일을 채우는 것과 같은 역할이다.
    다만 유튜브는 그걸 위해 요청을 따로 한다(채널당 최대 15개 추가).
    여기는 어차피 방문하는 페이지에서 덤으로 뽑으므로 비용이 0이다.
    → 같은 문제를 훨씬 싸게 해결했다.

디버그:
- 환경변수 IG_DEBUG_GQL=1 로 실행하면 오가는 friendly-name 을 전부 출력.
    IG_DEBUG_GQL=1 python -m instagram.steps.l3
  '*' 표시가 매칭된 쿼리. 새 이름이 보이면 config 에 추가할 것.
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
# L2와 동일하게 stealth를 쓴다. (L1만 안 쓴다)

# ── config 연동 (실행 방식에 따라 import 경로 대응) ──
try:
    from instagram.config import (
        SESSION_FILE,
        OUTPUT_DIR,
        L3_HTML_DIR,
        L3_GRAPHQL_DIR,
        LOG_DIR,
        L3_LOG_FILE,
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
        context_kwargs,
        L3_MIN_COMMENTS,
        L3_MAX_AGE_MONTHS,
    )
except Exception:
    from config import (
        SESSION_FILE,
        OUTPUT_DIR,
        L3_HTML_DIR,
        L3_GRAPHQL_DIR,
        LOG_DIR,
        L3_LOG_FILE,
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
        context_kwargs,
        L3_MIN_COMMENTS,
        L3_MAX_AGE_MONTHS,
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
#
# 플래그로 뺀 이유: 댓글만 빠르게 재수집하고 싶을 때 끌 수 있게.
# 메타 추출은 인라인 JSON 전체를 재귀 순회하므로 CPU 비용이 있다.
L3_BACKFILL_MEDIA = True

# 관측된 friendly-name 누적 (실행 끝에 요약 출력).
# L2의 193개 사고 이후 만든 진단 장치를 L3에도 이식했다.
_SEEN_QUERY_NAMES = {}

# SSR / GraphQL 공통 root_field
#
# ★ 이 상수 하나가 세 가지 역할을 한다:
#   ① SSR HTML에서 댓글 payload 찾기
#   ② GraphQL 응답 구조 확인
#   ③ 차단 감지 (이 구조가 없으면 정상 페이지가 아니다)
#
#   두 경로의 root_field가 같아서 parse_comments를 공유할 수 있다.
#   인스타가 SSR과 GraphQL에 같은 스키마를 쓴 덕분.
COMMENTS_ROOT = "xdt_api__v1__media__media_id__comments__connection"

# goto 예외 문자열 중 차단으로 간주할 패턴
#
# ⚠️ 예외 메시지 문자열로 판별한다. Playwright가 에러 종류를
#    타입으로 구분해주지 않아서 어쩔 수 없는 방식.
#    Chromium 버전이 바뀌면 메시지가 달라질 수 있다.
_BLOCK_ERR_MARKERS = (
    "ERR_HTTP_RESPONSE_CODE_FAILURE",   # Chromium이 4xx/5xx에서 내는 에러
    "ERR_TOO_MANY_REQUESTS",
    "429",
)

# ★ SSR 데이터를 뽑는 정규식.
#   인스타는 <script type="application/json"> 블록을 수십 개 심는다.
#   그중 댓글이 든 것을 찾아야 한다.
#
#   유튜브의 ytInitialData와 비슷하지만 훨씬 파편화돼 있다.
#   유튜브는 큰 JSON 하나, 인스타는 작은 JSON 여러 개.
_INLINE_JSON_RE = re.compile(
    r'<script[^>]+type="application/json"[^>]*>(.*?)</script>', re.S
)


# =========================================================
# DB SQL (fandom_crm 공유 스키마)
# =========================================================
# fans: 댓글 작성자. 같은 사람이 여러 게시물에 댓글을 달았는지 추적하는 게
# '코어 팬덤 모수' 측정의 핵심이다(가이드라인 원칙 3).
INSERT_FAN = (
    "INSERT INTO fans "
    "(platform, external_author_id, first_seen_at, last_seen_at) "
    "VALUES (%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE "
    "last_seen_at=VALUES(last_seen_at), "
    "updated_at=CURRENT_TIMESTAMP()"
)
# ⚠️ 유튜브·틱톡은 LAST_INSERT_ID(fan_id) 트릭을 써서 중복 시에도
#    lastrowid로 기존 fan_id를 받는데, 여기는 없다.
#    → 아래 save_to_db에 SELECT 폴백이 있다.
#      중복 댓글러가 많으면 SELECT가 그만큼 더 나간다.

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
# parent_comment_id가 있다. 인스타는 대댓글이 흔해서
# 세 플랫폼 중 여기만 스레드 구조를 저장한다.
#
# ON DUPLICATE로 갱신하는 이유: 재수집 시 좋아요 수가 늘었을 수 있다.
# (틱톡은 INSERT IGNORE로 한 번만 저장 — L3를 1회만 돌리는 정책이라)

# 미디어 메타 보강. COALESCE 로 추출 실패(NULL)시 기존 값을 보존한다.
#
# ★ COALESCE가 핵심이다.
#   SSR에서 caption을 못 뽑았다고 기존 값을 NULL로 덮으면,
#   L2가 그리드에서 잘 받아둔 캡션이 사라진다.
#   "새 값이 있으면 쓰고, 없으면 그대로 둔다."
#   (유튜브 crawler_l2가 category/duration을 COALESCE로 지키는 것과 같은 패턴)
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
    """L1/L2와 동일 구조. 로거 이름만 'l3'.
    (같은 이름이면 핸들러가 공유되어 로그 파일이 섞인다)"""
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
    """게시물 HTML 저장. 성패 무관 항상.

    ★ L3에서 이게 특히 중요하다.
      댓글이 SSR로 오므로, HTML이 곧 원본 데이터다.
      파싱 로직을 바꿨을 때 재크롤 없이 이 파일들로 다시 뽑을 수 있다.

      파일명이 external_id(shortcode)다. L1/L2는 username 기준인데
      L3는 게시물 단위라 여기만 다르다.
    """
    L3_HTML_DIR.mkdir(parents=True, exist_ok=True)
    path = L3_HTML_DIR / f"{_safe_name(external_id)}.html"
    path.write_text(html, encoding="utf-8")
    return path


def save_l3_graphql(external_id, pages):
    """pages: 캡처한 댓글 payload 들의 리스트 (GraphQL 응답 또는 SSR 추출본).

    ★ 'GraphQL 또는 SSR'인 게 포인트다.
      두 경로의 payload 형태를 맞춰뒀기 때문에 같은 파일에 섞어 담아도
      나중에 구분 없이 재파싱할 수 있다.
    """
    L3_GRAPHQL_DIR.mkdir(parents=True, exist_ok=True)
    path = L3_GRAPHQL_DIR / f"{_safe_name(external_id)}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=2)
    return path


def _dt(value):
    """
    unix초 / datetime / 문자열을 datetime 으로 정규화.
    parse_comments 가 어떤 형태를 주든 받아넘긴다.

    ★ 세 가지 입력을 받는 이유:
      SSR과 GraphQL이 같은 root_field를 쓰지만 값 형식이 미묘하게 다르다.
      어떤 경로는 unix초, 어떤 경로는 ISO 문자열이 온다.
      파서를 두 벌 만들지 않으려면 여기서 흡수해야 한다.

      마지막 return value가 중요하다. 파싱 실패 시 None으로 버리지 않고
      원본을 그대로 넘긴다 → MySQL이 문자열 datetime을 파싱할 수 있다.
      (버리면 published_at이 NULL이 되어 지표 계산에서 빠진다)

    ZoneInfo("Asia/Seoul") + replace(tzinfo=None):
    pymysql이 tzinfo를 버리므로 KST 벽시계 숫자로 만들어 넘겨야 한다.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromtimestamp(int(value), ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
    except Exception:
        return value  # 문자열 datetime 등은 DB 가 파싱하도록 그대로


# =========================================================
# GraphQL 캡처 판별
# =========================================================
def _friendly_name(response):
    """요청 헤더의 x-fb-friendly-name. GraphQL POST 가 아니면 None.

    L1/L2와 동일. /graphql/query 하나로 수십 종류가 오가므로
    이 헤더가 유일한 식별자다.
    """
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

    ★ 이 경고가 L2의 193개 사고에서 나온 교훈이다.
      L2는 이름 하나만 잡다가 소형 계정을 통째로 놓쳤다.
      L3는 처음부터 set으로 만들고 후보를 넓게 잡았다.

      결과적으로 L3의 진짜 문제는 이름이 아니라 'SSR'이었지만,
      한 번 데인 패턴을 선제 적용한 것 자체는 옳은 판단이었다.
    """
    name = _friendly_name(response)
    if name is None:
        return False
    return name in L3_COMMENTS_QUERY_NAMES


def _read_json(response):
    """2단 폴백. 절대 예외를 던지지 않는다."""
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
    """URL 리다이렉트로 차단 판별.

    ⚠️ L3에서는 이것만으로 부족하다.
      레이트 리밋에 걸리면 URL이 /p/... 그대로고 리다이렉트가 없다.
      → 아래 3중 감지(_is_block_error + no_ssr_payload)가 필요한 이유.
    """
    u = (final_url or "").lower()
    if "/accounts/login" in u or "/accounts/suspended" in u:
        return True
    if "/challenge" in u or "/checkpoint" in u:
        return True
    return False


def _is_block_error(exc):
    """goto 예외가 레이트 리밋/차단성인지.

    3중 감지의 ②번.
    Chromium은 4xx/5xx 응답에서 goto가 예외를 던지는 경우가 있다.
    (특히 응답 본문이 비어 있을 때)
    그 예외 메시지에 ERR_HTTP_RESPONSE_CODE_FAILURE가 들어간다.
    """
    s = str(exc)
    return any(m in s for m in _BLOCK_ERR_MARKERS)


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

    ★ 반환 형태를 GraphQL 응답과 맞추는 게 핵심이다.
      {"data": {...}}로 감싸서 돌려주면 parse_comments가
      "이게 SSR에서 왔는지 XHR에서 왔는지" 몰라도 된다.
      → 파서를 두 벌 만들지 않아도 된다.

    경로를 하드코딩하지 않고 재귀 탐색하는 이유:
    Relay 캐시 구조가 인스타 배포마다 달라진다.
    'data 안에 COMMENTS_ROOT가 있는 dict'라는 특징으로 찾으면
    중간 경로가 바뀌어도 견딘다.
    (유튜브 find_first와 같은 발상)
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
    """SSR HTML 에서 댓글 payload 를 추출. 없으면 None.

    ★ 값싼 사전 필터가 있다:  if COMMENTS_ROOT not in s: continue

      인라인 JSON 블록이 수십 개인데 전부 json.loads하면 느리다.
      게시물 페이지 HTML이 수백 KB고, 6,972건을 처리해야 한다.
      문자열 검색으로 먼저 후보를 좁히고 그것만 파싱한다.

    json.loads 실패 시 continue하는 이유:
    블록 하나가 깨졌다고 전체를 포기할 이유가 없다. 다음 블록을 본다.
    """
    if not html:
        return None
    for m in _INLINE_JSON_RE.finditer(html):
        s = m.group(1)
        if COMMENTS_ROOT not in s:
            continue                # 파싱 전에 문자열로 먼저 거른다
        try:
            blob = json.loads(s)
        except Exception:
            continue                # 이 블록만 버리고 다음으로
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

    ★ '이름'이 아니라 '내용'으로 찾는 게 이 함수의 판단이다.

      댓글은 root_field 이름(COMMENTS_ROOT)이 안정적이라 그걸 쓰는데,
      미디어 노드는 여러 위치에 여러 이름으로 들어온다.
      → "code가 일치하고 미디어 필드를 가진 dict"라는 특징으로 찾는다.

      code까지 대조하는 이유가 중요하다.
      게시물 페이지에는 추천 게시물, 같은 계정의 다른 게시물 등
      다른 미디어도 함께 실린다. 내가 요청한 것만 골라야 한다.
      (대조 없이 첫 미디어를 쓰면 엉뚱한 게시물의 캡션이 저장된다)
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

    ★ 이 함수가 L2의 구조적 결함을 메운다.

      L2 릴스 탭(clips) 응답:  taken_at ❌  caption ❌  duration ❌
      → published_at은 pk 복원값(부정확), caption은 NULL로 저장됨

      L3는 어차피 게시물 페이지를 개별 방문한다.
      같은 HTML에서 정확한 값을 뽑아 덮어쓴다. 추가 요청 0.

      유튜브는 같은 문제(쇼츠 게시일)를 해결하려고 L2b에서
      채널당 최대 15개의 추가 요청을 한다.
      여기는 공짜다 — 이미 열어야 하는 페이지니까.
    """
    if not html or not code:
        return None

    for m in _INLINE_JSON_RE.finditer(html):
        s = m.group(1)
        # 값싼 사전 필터 — code 가 없는 블록은 파싱조차 하지 않는다
        # 조건이 두 개인 이유: code만 있고 미디어 필드가 없는 블록
        # (예: 링크 목록)을 한 번 더 걸러낸다.
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
        # 하나라도 값이 있어야 반환. 전부 None이면 다음 블록을 계속 본다.
        # (빈 dict를 반환하면 update_content_meta가 헛돈다)
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
      block_reason : 차단으로 본 이유 (진단/로그용)
      from_html    : SSR 폴백으로 댓글을 수집했는지 (진단용)
      media_meta   : SSR 에서 뽑은 미디어 메타 (없으면 None)
      seen_names   : 이 게시물에서 관측한 friendly-name 집합 (진단용)

    ⚠️ goto 는 이 함수 전체에서 정확히 1회만 호출한다.
       (두 번 부르면 요청량이 2배가 되어 레이트 리밋을 자초한다)

    ★ 이 경고가 ②번 사고와 연결된다.
      6,500건을 헛돈 뒤 "요청을 아껴야 한다"는 인식이 강해졌다.
      L2는 그리드+릴스로 goto를 2번 하는데, L3는 게시물 수가
      훨씬 많아서(6,972건) 1회로 제한했다.

    block_reason을 따로 두는 이유:
      "차단됐다"만으로는 원인을 모른다.
      http_429인지, goto 예외인지, SSR 구조가 없는 건지에 따라
      대응이 다르다. crawl_logs의 error_detail에 남긴다.
    """
    url = f"https://www.instagram.com/p/{external_id}/"
    result = {
        "comments": [], "raw_pages": [], "html": None,
        "final_url": url, "http_status": None, "blocked": False,
        "block_reason": None, "from_html": False,
        "media_meta": None, "seen_names": set(),
    }

    captured = {"pages": [], "comments": [], "seen": set()}

    def _mark_blocked(reason):
        """차단 표시. 첫 번째 이유만 남긴다.

        여러 감지가 동시에 걸릴 수 있는데(예: 4xx + SSR 없음),
        가장 먼저 확인된 게 근본 원인에 가깝다.
        """
        result["blocked"] = True
        if result["block_reason"] is None:
            result["block_reason"] = reason

    def _add_comments(comments):
        """중복 제거하며 담는다. 한도 도달 시 True 반환.

        SSR과 GraphQL 양쪽에서 같은 댓글이 올 수 있어 dedup이 필요하다.
        L2의 병합 로직과 달리 여기는 그냥 버린다 —
        댓글은 두 경로가 같은 필드를 주므로 병합할 이유가 없다.
        """
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
            return      # 이미 목표치. 더 파싱할 이유 없다.
        if not _is_comments_graphql(response):
            return
        data = _read_json(response)
        if data is None:
            return
        captured["pages"].append(data)
        try:
            comments = parse_comments(data)
        except Exception as e:
            # 파싱 실패로 리스너가 죽으면 안 된다.
            if DEBUG_GQL:
                print(f"  [parse_comments 실패] {name}: {e!r}", flush=True)
            return
        _add_comments(comments)

    # ⚠️ 리스너는 goto 이전에 등록해야 SSR 외 응답도 놓치지 않는다.
    page.on("response", _on_response)

    try:
        # ===== goto (이 함수에서 유일한 네비게이션) =====
        try:
            resp = page.goto(url, wait_until="domcontentloaded",
                             timeout=L3_GOTO_TIMEOUT_MS)
            result["http_status"] = resp.status if resp else None
            # ① 4xx/5xx → 레이트 리밋 또는 삭제된 게시물
            #
            # 3중 감지의 첫 번째. 가장 확실한 신호다.
            # 다만 인스타는 레이트 리밋에도 200을 주는 경우가 있어
            # 이것만으로는 부족하다.
            if resp and resp.status >= 400:
                _mark_blocked(f"http_{resp.status}")
        except PWTimeout:
            pass    # 타임아웃은 차단이 아닐 수 있다. 아래에서 HTML로 판단.
        except Exception as e:
            # ② net::ERR_HTTP_RESPONSE_CODE_FAILURE 등
            #
            # 3중 감지의 두 번째.
            # Chromium이 4xx/5xx + 빈 본문에서 예외를 던지는 경우.
            # resp 객체가 아예 안 와서 ①번으로는 못 잡는다.
            if _is_block_error(e):
                _mark_blocked(f"goto_error:{type(e).__name__}")
            elif DEBUG_GQL:
                print(f"  [goto 실패] {e!r}", flush=True)

        try:
            result["final_url"] = page.url
        except Exception:
            pass

        # 로그인/챌린지 리다이렉트
        if not result["blocked"] and _looks_blocked(result["final_url"]):
            _mark_blocked("redirect_login")

        # --- 렌더 대기 ---
        # SSR 이 주 경로라 GraphQL 을 오래 기다릴 필요는 없지만,
        # DOM 이 자리잡아야 page.content() 에 인라인 JSON 이 온전히 담긴다.
        #
        # ★ 이 주석이 ①번 발견의 결과다.
        #   원래는 GraphQL을 기다리는 게 목적이었는데,
        #   SSR이 주 경로임을 알고 나서 "DOM 안정화"로 목적이 바뀌었다.
        if not result["blocked"]:
            page.wait_for_timeout(L3_RENDER_WAIT_MS)

        # --- 혹시 오는 GraphQL 유예 대기 ---
        # '혹시'라는 단어가 실제 비중을 말해준다.
        # 대부분 SSR로 오고, GraphQL은 예외적으로만 온다.
        if not result["blocked"] and len(captured["comments"]) < L3_COMMENT_LIMIT:
            deadline = time.monotonic() + (L3_GRAPHQL_GRACE_MS / 1000.0)
            while (len(captured["comments"]) < L3_COMMENT_LIMIT
                   and time.monotonic() < deadline):
                page.wait_for_timeout(150)
                # sync API에서 이벤트 루프를 돌려야 리스너가 호출된다.
                # time.sleep()이면 루프가 멈춰 응답을 못 받는다.

        # --- 부족하면 스크롤로 추가 로드 ---
        # ⚠️ 기본 config 는 L3_MAX_SCROLLS=0 이라 이 블록은 돌지 않는다.
        #    인스타 댓글은 본문이 아니라 별도 스크롤 컨테이너에 있어서
        #    mouse.wheel 로는 페이지네이션이 안 터질 수 있음.
        #
        # ★ 시도했다가 안 돼서 꺼둔 코드다. 지우지 않고 남긴 이유:
        #   나중에 컨테이너를 특정해서 스크롤하는 방법을 찾으면
        #   이 구조를 그대로 쓸 수 있다.
        #   현재는 첫 페이지(Top Comments 약 12~24개)만 수집한다.
        #   → config에 "pagination 미사용"이라고 한계를 명시해뒀다.
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
        #
        # 6,972건을 처리하므로 이게 없으면 후반부에 사실상 멈춘다.
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    # --- HTML 항상 저장 ---
    # 차단당했어도 뜬다. 그 HTML이 차단 판별(③번)의 근거가 되고,
    # 나중에 "그때 뭘 받았나"를 확인하는 자료가 된다.
    try:
        result["html"] = page.content()
    except Exception:
        result["html"] = None

    # ③ SSR 구조 자체가 없으면 차단으로 간주.
    #    정상 페이지는 댓글이 0개여도 comments__connection 은 존재한다.
    #    이 검사가 없으면 레이트 리밋을 'empty' 로 오기록해 resume 이 영영 건너뛴다.
    #
    # ★ 3중 감지의 세 번째이자 가장 중요한 것.
    #
    #   ①②는 HTTP 레벨 신호인데, 인스타는 레이트 리밋에도
    #   200 + 빈 페이지를 준다. URL도 그대로다.
    #   → 눈에 보이는 신호가 전혀 없다.
    #
    #   "정상 페이지라면 반드시 있어야 할 구조"의 유무로 판별한다.
    #   댓글이 0개인 게시물도 comments__connection 껍데기는 온다.
    #   그것조차 없으면 페이지를 제대로 못 받은 것이다.
    #
    #   조건에 `not captured["comments"]`가 있는 이유:
    #   댓글을 이미 받았으면 정상이므로 검사할 필요가 없다.
    if not result["blocked"] and not captured["comments"]:
        if not result["html"] or COMMENTS_ROOT not in result["html"]:
            _mark_blocked("no_ssr_payload")

    # --- SSR HTML 폴백: 댓글 (실질적인 주 경로) ---
    #
    # ★ 코드 구조상 '폴백'이지만 주석이 '실질적인 주 경로'라고 밝힌다.
    #   리스너 방식(L1/L2 계승)을 먼저 쓰고 실패 시 SSR로 가는 구조인데,
    #   실제로는 거의 항상 SSR로 온다.
    #   → 구조를 뒤집는 게 정직하지만, from_html 카운터로
    #     실측 비율을 남기는 선에서 두었다.
    if not captured["comments"] and not result["blocked"]:
        payload = extract_comments_payload_from_html(result["html"])
        if payload:
            result["from_html"] = True
            try:
                comments = parse_comments(payload)
                # ↑ GraphQL 응답과 같은 형태라 같은 파서를 쓴다.
            except Exception as e:
                if DEBUG_GQL:
                    print(f"  [parse_comments(html) 실패] {e!r}", flush=True)
                comments = []
            _add_comments(comments)
            if captured["comments"]:
                captured["pages"].append(payload)
                # ↑ 댓글을 실제로 얻었을 때만 raw에 넣는다.
                #   빈 payload를 저장하면 디스크만 낭비.

    # --- SSR HTML: 미디어 메타 (L2 릴스 누락분 보강) ---
    # 댓글 유무와 무관하게 시도한다.
    # 댓글 0개인 릴스도 caption/published_at은 채워야 하기 때문.
    if L3_BACKFILL_MEDIA and not result["blocked"]:
        try:
            result["media_meta"] = extract_media_meta_from_html(
                result["html"], external_id
            )
        except Exception as e:
            # 메타 추출 실패가 댓글 수집을 망치면 안 된다.
            if DEBUG_GQL:
                print(f"  [media_meta 추출 실패] {e!r}", flush=True)

    result["comments"] = captured["comments"][:L3_COMMENT_LIMIT]
    result["raw_pages"] = captured["pages"]
    return result


# =========================================================
# DB 저장
# =========================================================
def save_to_db(conn, content_id, comments):
    """댓글 + 팬 저장.

    fans가 핵심이다. external_author_id로 동일인을 식별해야
    "이 사람이 이 채널의 게시물 몇 개에 댓글을 달았나"를 셀 수 있고,
    그게 calc_l3_metric의 중복률/고정댓글러 지표가 된다.
    """
    saved = 0

    with conn.cursor() as cur:
        for c in comments:
            author_id = c.get("external_author_id")
            if not author_id:
                continue    # 작성자 ID 없으면 팬 식별 불가 → 버림

            now = datetime.now()
            cur.execute(INSERT_FAN, (
                PLATFORM,
                author_id,
                now,        # first_seen_at
                now,        # last_seen_at
            ))
            # ⚠️ first_seen_at도 매번 now를 넣는다.
            #    ON DUPLICATE에서 last_seen_at만 갱신하므로
            #    기존 팬의 first_seen_at은 안 덮인다. 의도대로 동작한다.
            fan_id = cur.lastrowid
            if not fan_id:
                # LAST_INSERT_ID 트릭이 없어서 중복 시 폴백 SELECT가 필요하다.
                # (유튜브·틱톡은 트릭을 써서 이 쿼리가 없다)
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
                c.get("parent_comment_id"),   # 대댓글 스레드. 인스타만 저장한다.
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

    ★ 세 값이 모두 None이면 UPDATE를 아예 안 날린다.
      COALESCE라 실행해도 무해하지만, 6,972건 × 불필요한 쿼리는 낭비다.
      그리고 반환값으로 "실제 보강 건수"를 집계할 수 있다
      (run()의 meta_filled 카운터).
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
    """crawl_logs 기록. target_url이 게시물 URL이다.

    ⚠️ L1/L2는 채널 단위라 channel_id로 resume이 되는데,
       L3는 게시물 단위라 target_url로 매칭해야 한다.
       그래서 fetch_targets의 NOT EXISTS가 문자열 비교를 쓴다.
       (content_id 컬럼이 crawl_logs에 있으면 훨씬 깔끔하다)
    """
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

    범위 제한 (config):
      - cs.comment_count >= L3_MIN_COMMENTS  : 댓글 0개는 방문 낭비
      - published_at > L3_MAX_AGE_MONTHS 이내 : 오래된 건 지표 가치 낮음
    정렬은 댓글 많은 순 — 중간에 끊겨도 값어치 있는 것부터 확보된다.

    ★ 대상을 좁히는 게 이 파일의 현실적 판단이다.

      전체 게시물 17,048건 → 대상 6,972건 (41%)

      계정당 8~15초라 전부 돌면 며칠이 걸린다.
      댓글이 0개인 게시물은 방문해도 얻을 게 없고,
      1년 넘은 게시물은 팬덤 분석 가치가 낮다.

      ⚠️ 부작용: calc_l3_metric의 content_count가 채널마다 크게 다르다.
        어떤 채널은 10개, 어떤 채널은 2개.
        2개짜리 채널에서 "절반 이상"은 1개고, 한 번 댓글 단 사람이
        전부 '고정 댓글러'가 된다.
        → calc_l3_metric에 MIN_L3_CONTENTS=3 하한을 둔 이유가 이것이다.

    ★ ORDER BY comment_count DESC 가 중요하다.
      6,972건을 한 번에 못 끝낼 가능성이 높다(차단으로 중단될 수 있다).
      그때 최소한 댓글이 많은 게시물은 확보돼 있어야 한다.
      → 유튜브 L2b가 active 채널부터 처리하는 것과 같은 발상.

    resume: 이미 L3 success/empty 인 게시물 제외.
    ⚠️ 수집경로를 고친 뒤 재수집하려면 empty 로그를 먼저 지울 것:
         DELETE FROM crawl_logs WHERE layer='L3' AND error_type='empty';

    ★ 이 경고가 ②번 사고의 뒷정리 방법이다.
      6,500건이 empty로 박제됐을 때, 코드를 고쳐도 resume이
      그걸 계속 건너뛰어서 재수집이 안 됐다.
      로그를 지워야 다시 대상에 들어온다.
      → 'empty'를 성공으로 취급하는 게 편하지만 위험하다는 교훈.
    """
    sql = """
    SELECT ct.content_id, ct.channel_id, ct.external_id
    FROM contents ct
    JOIN channels ch
      ON ct.channel_id = ch.channel_id
    -- 콘텐츠별 최신 스냅샷의 댓글 수
    -- (L2가 저장한 comment_count. 이걸로 방문 가치를 판단한다)
    JOIN (
        SELECT cs1.content_id, cs1.comment_count
        FROM content_snapshots cs1
        JOIN (
            SELECT content_id, MAX(captured_at) AS m
            FROM content_snapshots
            GROUP BY content_id
        ) t
          ON cs1.content_id = t.content_id
         AND cs1.captured_at = t.m
    ) cs
      ON cs.content_id = ct.content_id
    WHERE ch.platform=%s
      AND ct.external_id IS NOT NULL
      AND cs.comment_count >= %s
      AND ct.published_at > DATE_SUB(NOW(), INTERVAL %s MONTH)
    """
    params = [PLATFORM, L3_MIN_COMMENTS, L3_MAX_AGE_MONTHS]

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
        # ⚠️ target_url 문자열 비교로 게시물을 매칭한다.
        #    CONCAT이라 인덱스를 못 타서 crawl_logs가 커지면 느려진다.
        #    (틱톡 L3도 LIKE로 같은 문제를 갖는다)
        #    crawl_logs에 content_id 컬럼이 있으면 해결되는데,
        #    스키마가 채널 단위로 설계돼 있어 그대로 뒀다.

    # 댓글 많은 순 → 중단되더라도 가치 높은 것부터 확보
    sql += " ORDER BY cs.comment_count DESC, ct.content_id"

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
        # 세션 없이 시작하면 전부 차단으로 처리되고 시간만 날린다.

    conn = pymysql.connect(**DB)
    try:
        targets = fetch_targets(conn, limit, resume)
        log.info("[L3] 대상 게시물: %d개", len(targets))
        if not targets:
            log.info("처리할 게시물 없음 (다 끝났거나 contents에 인스타 게시물이 없음).")
            return

        ok = none = err = blocked = 0
        from_html = 0      # SSR로 얻은 건수. 주 경로 비중 실측용.
        meta_filled = 0    # 미디어 메타를 실제로 보강한 건수
        block_streak = 0   # 연속 차단. 누적(blocked)과 분리돼 있다.

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            # ⚠️ 컨텍스트 옵션은 login.py / l2.py 와 반드시 동일해야 한다.
            #    UA/viewport/locale 이 어긋나면 인스타는
            #    "같은 쿠키인데 다른 브라우저"로 보고 세션을 의심한다.
            context = browser.new_context(
                **context_kwargs(storage_state=SESSION_FILE)
            )

            # page 는 전 게시물에서 재사용한다. 생성은 딱 1회.
            # 게시물마다 새로 만들면 그 자체가 부자연스럽고 느리다.
            page = context.new_page()
            _stealth.apply_stealth_sync(page)

            try:
                for i, (content_id, channel_id, external_id) in enumerate(targets, 1):
                    url = f"https://www.instagram.com/p/{external_id}/"
                    try:
                        r = collect_comments(page, external_id)

                        # raw 항상 저장
                        # 차단당한 것도 저장한다 — 나중에 "그때 뭘 받았나"를
                        # 확인해야 새로운 차단 유형을 발견할 수 있다.
                        if r["html"]:
                            save_l3_html(external_id, r["html"])
                        if r["raw_pages"]:
                            save_l3_graphql(external_id, r["raw_pages"])

                        if r["blocked"]:
                            blocked += 1
                            block_streak += 1
                            log_l3(conn, channel_id, url, STATUS_FAILED,
                                   r["http_status"], "blocked",
                                   r["block_reason"] or r["final_url"])
                            # ↑ block_reason을 error_detail에 남긴다.
                            #   나중에 "어떤 유형의 차단이 많았나"를 집계할 수 있다.
                            log.warning("  [%d/%d] BLOCK %s (%s) streak=%d",
                                        i, len(targets), external_id,
                                        r["block_reason"], block_streak)
                            if block_streak >= STOP_ON_BLOCK:
                                log.error(
                                    "[L3] 연속 차단 %d회 -> 중단. "
                                    "레이트 리밋일 수 있으니 몇 시간 쉬었다 재개하세요.",
                                    STOP_ON_BLOCK)
                                break
                            continue
                            # ★ failed로 기록하고 continue한다.
                            #   success/empty가 아니므로 resume이 다시 잡는다.
                            #   ②번 사고의 핵심 수정이 이 지점이다.

                        # 미디어 메타 보강 — 댓글 유무와 무관하게 항상 시도.
                        # (댓글 0개인 릴스도 caption/published_at 은 채워야 한다)
                        #
                        # ★ 순서가 중요하다. 아래 "댓글 0개면 continue"보다
                        #   먼저 와야 한다. 안 그러면 댓글 없는 릴스의
                        #   메타가 영영 안 채워진다.
                        meta_ok = False
                        if L3_BACKFILL_MEDIA:
                            try:
                                meta_ok = update_content_meta(
                                    conn, content_id, r["media_meta"]
                                )
                            except Exception as e:
                                # 메타 실패가 댓글 저장을 막으면 안 된다
                                log.warning("  [%d/%d] META 실패 %s | %r",
                                            i, len(targets), external_id, e)
                        if meta_ok:
                            meta_filled += 1

                        if not r["comments"]:
                            none += 1
                            block_streak = 0
                            log_l3(conn, channel_id, url, STATUS_OK,
                                   r["http_status"], "empty", None)
                            # ★ 여기 도달했다는 건 3중 감지를 전부 통과했다는 뜻.
                            #   = SSR 구조는 있는데 댓글이 정말 0개다.
                            #   (댓글이 삭제됐거나 댓글 기능이 꺼진 게시물)
                            #   이제는 안심하고 empty로 기록할 수 있다.
                            log.info("  [%d/%d] NONE  %s (댓글 0%s)",
                                     i, len(targets), external_id,
                                     " meta" if meta_ok else "")
                            continue

                        n = save_to_db(conn, content_id, r["comments"])
                        ok += 1
                        if r["from_html"]:
                            from_html += 1
                        block_streak = 0   # 성공하면 연속 카운터 리셋
                        log_l3(conn, channel_id, url, STATUS_OK,
                               r["http_status"], None, None)
                        log.info("  [%d/%d] OK    %s (comments=%d%s%s)",
                                 i, len(targets), external_id, n,
                                 " ssr" if r["from_html"] else "",
                                 " meta" if meta_ok else "")
                        # ↑ ssr/meta 표시로 한 줄에 세 정보를 담는다.
                        #   "ssr"이 대부분이면 리스너 경로가 무의미하다는 신호.

                    except Exception as e:
                        # 게시물 1건 예외로 전체가 죽지 않게 격리
                        err += 1
                        log_l3(conn, channel_id, url, STATUS_FAILED,
                               None, "exception", str(e)[:400])
                        log.info("  [%d/%d] ERR   %s | %r",
                                 i, len(targets), external_id, e)

                    # 게시물 간 8~15초 랜덤 대기.
                    # L2의 계정 간 간격과 같은 값이다.
                    # 게시물이 6,972건이라 이것만으로 20~29시간이다.
                    lo, hi = L3_CONTENT_GAP
                    time.sleep(random.uniform(lo, hi))

            finally:
                # 크롤링 중 롤링된 쿠키를 다시 저장해 세션 수명을 늘린다.
                # ⚠️ 차단 중단(break)이나 예외 시에도 반드시 실행되도록 finally.
                #
                # ★ 특히 차단으로 중단됐을 때가 중요하다.
                #   그 시점의 쿠키를 저장해두면 다음 실행에서
                #   갱신된 세션으로 시작할 수 있다.
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

        log.info("[L3] 완료: OK=%d (SSR=%d) NONE=%d BLOCK=%d ERR=%d | META보강=%d",
                 ok, from_html, none, blocked, err, meta_filled)
        # ★ SSR 비율과 META 보강 건수를 함께 찍는다.
        #   SSR=OK와 거의 같으면 "리스너 경로는 사실상 안 쓰인다"는 근거고,
        #   META보강 건수는 "L2 릴스 누락을 얼마나 복구했나"를 보여준다.

        # 관측된 friendly-name 요약 — 미등록 이름이 있으면 config 확인
        #
        # L2와 달리 log.warning이 아니라 log.info다.
        # L3는 SSR이 주 경로라 미등록 쿼리가 있어도 문제가 아니기 때문.
        # (L2는 쿼리를 놓치면 데이터를 못 받아서 경고 수준)
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