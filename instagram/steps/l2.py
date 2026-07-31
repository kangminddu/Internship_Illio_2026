# -*- coding: utf-8 -*-
"""
steps/l2.py  (Instagram L2 — 게시물 목록 수집)

★ 이 파일에는 두 개의 '실패에서 배운 흔적'이 박혀 있다.
------
  ① friendly-name을 하나만 잡다가 소형 계정 193개를 '게시물 0개'로 놓쳤다
  ② 릴스가 그리드에 안 나오는 계정이 절반(725개 중 365개)이었다
  둘 다 "데이터가 없다"와 "가져오지 못했다"를 구분 못 한 사례다.
  아래 관련 코드에 상세히 적어둔다.

설계 원칙 (L1 계승 + 프로젝트 문서 기준)
- L1 과 동일하게 Playwright 단일 세션 + 브라우저 goto + 리스너 캡처.
  request 직접 호출(찍어내기)은 인스타에서 429 유발 → 사용 안 함.

  ↑ 유튜브 L2b가 requests로 영상 페이지를 찍어내다가 IP 차단당한 것과 대조.
    인스타는 처음부터 브라우저만 쓴다.

- 게시물 목록 GraphQL 캡처. ⚠️ friendly-name 은 계정에 따라 여러 종류가 온다.
  (config.L2_POSTS_QUERY_NAMES 참고. 단일 이름만 잡다가 소형 계정 193개를
   empty 로 놓쳤던 이력이 있음.)
- 최근 L2_POST_LIMIT(10)개 무조건 수집. 기간 필터 없음.
  (3/6개월 확장·활동성 분류·파생 지표는 전부 metrics 단계. raw→derived 원칙.)

  ↑ 가이드라인의 2단계 구조. 수집은 '가져오기'만, 판단은 '계산하는 곳'에서.
    유튜브 L2b는 published_at으로 정렬해 최근 15개를 고르는데,
    여기는 그냥 최신 10개를 받고 기간 판단을 metrics에 넘긴다.

- 저장 3층: HTML(raw) / 목록 GraphQL(raw) / 정규화(DB).
  DB 는 fandom_crm 공유 스키마:
    channels(L1이 채움) → contents(게시물) → content_snapshots(좋아요/댓글 시점값)
- 좋아요/댓글 수는 시점마다 바뀌므로 contents 가 아니라 content_snapshots 에 저장.
- 연속 차단(CHALLENGE/로그인/429) STOP_ON_BLOCK 회 → 세션 보호 위해 중단.

L1 과의 차이(틱톡 L2 대비):
- async → sync (L1 이 sync 라 통일)
- TikTok CAPTCHA 로직 제거 (인스타는 CHALLENGE/checkpoint 를 URL 로 판별)
- 딜레이 8~15초 (틱톡 1.5초 아님 — 인스타는 antibot 모듈 없이 딜레이로 버틴다)

디버그:
- 환경변수 IG_DEBUG_GQL=1 로 실행하면 오가는 friendly-name 을 전부 출력.
    IG_DEBUG_GQL=1 python -m instagram.steps.l2
  새 쿼리 이름이 보이면 config.L2_POSTS_QUERY_NAMES 에 추가할 것.

  ↑ ①번 사고 이후 만든 진단 장치. 같은 문제가 또 나면 즉시 찾을 수 있다.
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
# ⚠️ L1은 stealth를 안 쓰는데 L2만 쓴다.
#    L2는 스크롤·탭 이동 같은 조작이 있어 자동화 흔적이 더 드러나서로 보인다.
#    (틱톡 L1에서 stealth가 전 요청을 죽인 적이 있어 주의가 필요한 라이브러리)

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
# L1은 상태가 9개인데 여기는 2개다.
# L2는 crawl_logs에 기록하고 error_type으로 세분화하므로
# 상태 자체는 success/failed면 충분하다.
STATUS_OK = "success"
STATUS_FAILED = "failed"

DEBUG_GQL = os.environ.get("IG_DEBUG_GQL") == "1"

# 미지의 friendly-name 수집용 (실행 끝에 요약 출력).
# 새 이름이 보이면 config.L2_POSTS_QUERY_NAMES 에 추가하면 된다.
#
# ★ 모듈 전역이라 실행 내내 누적된다.
#   ①번 사고(193개 오분류)의 재발 방지 장치.
#   인스타가 새 쿼리 이름을 도입하면 실행 끝에 경고로 뜬다.
_SEEN_QUERY_NAMES = {}


# =========================================================
# DB SQL (fandom_crm 공유 스키마)
# =========================================================
# contents: 게시물 본체 (좋아요/댓글수는 여기 넣지 않음 — 스냅샷으로)
#
# ★ 원본/시계열 분리. 게시물 캡션은 안 바뀌지만 좋아요 수는 계속 변한다.
#   같은 테이블에 두면 "언제 기준 좋아요인지"를 알 수 없다.
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
    #  ↑ ON DUPLICATE에서도 lastrowid로 기존 content_id를 받는 트릭.
    #    없으면 중복 시 별도 SELECT가 필요하다.
    #    (아래 save_to_db에 폴백 SELECT가 남아 있긴 하다)
)

# content_snapshots: 그 시점의 좋아요/댓글/조회수 (INSERT IGNORE = 같은 시각 중복 방지)
# 시계열이므로 덮어쓰지 않는다. 같은 (content_id, captured_at)이면 그냥 무시.
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

    ★ 유튜브 쇼츠 문제와 정확히 같은 구조다.

      유튜브 : 쇼츠 탭에 날짜가 없다 → published_at NULL → 활동성 판정 왜곡
      인스타 : 릴스 탭에 taken_at이 없다 → 여기서 pk로 복원

      다만 대응이 다르다. 유튜브는 NULL로 두고 L2b가 채우게 했는데,
      여기는 pk에서 역산해 값을 만든다.
      → 정렬과 기간 필터가 즉시 가능해진다.

      Snowflake ID: 상위 41비트가 밀리초 타임스탬프다.
      >> 23으로 하위 비트(워커ID+시퀀스)를 버리고,
      //1000으로 초 단위로 만든 뒤 인스타 에포크를 더한다.

      '경험적'이라고 적은 이유: 인스타가 공식 문서로 밝힌 게 아니라
      관찰로 알아낸 방식이다. 인스타가 ID 체계를 바꾸면 깨진다.
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

    ★ 같은 게시물인데 탭마다 다른 데이터를 준다.

        그리드(timeline) : taken_at ✅  caption ✅  조회수 ❌
        릴스(clips)      : taken_at ❌  caption ❌  조회수 ✅

      이 비대칭이 아래 _on_response의 '중복 병합' 로직을 필요하게 만든다.

    반환 형태를 parse_posts와 맞추는 게 중요하다.
    그래야 호출부가 어느 탭에서 왔는지 신경 쓰지 않아도 된다.
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
        # ↑ timeline은 node가 곧 media인데, clips는 node.media로 한 겹 더 깊다.
        if not isinstance(m, dict):
            continue
        code = m.get("code")
        if not code:
            continue

        caption = m.get("caption")
        cap_text = caption.get("text") if isinstance(caption, dict) else None
        dur = m.get("video_duration")

        out.append({
            "external_id": code,        # 인스타 shortcode. URL에 쓰이는 값
            "content_type": "reels",
            "taken_at": m.get("taken_at") or _ts_from_pk(m.get("pk")),
            #  ↑ 혹시 있으면 그걸 쓰고, 없으면 pk에서 복원
            "caption_text": cap_text,
            "like_count": m.get("like_count"),
            "comment_count": m.get("comment_count"),
            "view_count": m.get("play_count") or m.get("view_count"),
            #  ↑ 릴스는 play_count. view_count는 항상 null이라 폴백만 남겨둠
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
    # ↑ set 두 개를 확인한다. config에 이름을 추가만 하면
    #   코드를 안 고쳐도 새 쿼리를 잡을 수 있다.


# =========================================================
# 로깅
# =========================================================
def setup_logging():
    """L1과 동일 구조. 로거 이름만 'l2'로 다르다.
    (같은 이름이면 핸들러가 공유되어 L1 로그 파일에 섞인다)"""
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
    """프로필 HTML 저장. 파싱 실패 시 원인 추적용."""
    L2_HTML_DIR.mkdir(parents=True, exist_ok=True)
    path = L2_HTML_DIR / f"{_safe_name(username)}.html"
    path.write_text(html, encoding="utf-8")
    return path


def save_l2_graphql(username, pages):
    """pages: 캡처한 목록 GraphQL 응답들의 리스트 (페이지별 verbatim).

    ★ 리스트로 저장한다. 스크롤하면 응답이 여러 번 오기 때문.
      '왜 게시물이 3개만 잡혔지?'를 조사할 때 각 페이지 응답을
      순서대로 볼 수 있어야 한다.

      실제로 ①번 사고(193개)를 이 덤프로 찾아냈다.
      raw_pages가 비어 있는데 seen_names에는 모르는 이름이 있었다.
    """
    L2_GRAPHQL_DIR.mkdir(parents=True, exist_ok=True)
    path = L2_GRAPHQL_DIR / f"{_safe_name(username)}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=2)
    return path


def _dt(unix_sec):
    """유닉스 초 → KST naive datetime.

    tz를 붙여 변환한 뒤 replace(tzinfo=None)로 떼는 이유:
    pymysql이 tzinfo를 버리고 숫자만 문자열로 만든다.
    UTC로 넘기면 MySQL(+09:00)이 KST로 해석해 9시간 어긋난다.
    → KST 벽시계 숫자로 만들어 넘겨야 한다.
    (세 플랫폼 전체를 KST로 통일하면서 고친 부분)
    """
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
    """요청 헤더의 x-fb-friendly-name. GraphQL POST 가 아니면 None.

    ★ URL만으로는 구분이 안 된다.
      /graphql/query 하나로 수십 종류의 쿼리가 오간다.
      게시물 목록, 스토리, 추천, 알림... 전부 같은 경로다.
      요청 헤더의 이 값이 유일한 식별자다.

      response가 아니라 response.request의 헤더를 본다.
      (응답에는 쿼리 이름이 없다)
    """
    try:
        req = response.request
        if req.method != "POST":
            return None      # GraphQL은 항상 POST. GET을 먼저 걸러 비용 절약
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
    """2단 폴백. 절대 예외를 던지지 않는다.
    응답 파싱 실패로 크롤 전체가 죽으면 안 된다."""
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
    """URL 리다이렉트로 차단을 판별한다.

    ★ 틱톡은 CAPTCHA를 shadow DOM까지 뒤져 찾아야 했는데,
      인스타는 URL만 보면 된다. 훨씬 단순하다.

      /challenge, /checkpoint = 본인확인 요구 (계정 잠김 위험)
      /accounts/login         = 세션 만료
      /accounts/suspended     = 계정 정지
    """
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

    ★ ②번 사고. 725채널 중 365개가 그리드에 릴스 0건이었다.
      즉 절반 가까운 계정에서 릴스를 통째로 놓치고 있었다.

      인스타가 계정 설정이나 A/B 테스트에 따라 그리드 구성을 다르게 준다.
      → 릴스 탭을 항상 별도로 방문하는 수밖에 없다.
      → 계정당 페이지 로드가 2번이 되어 시간이 2배가 됐지만,
        데이터의 절반을 놓치는 것보다 낫다는 판단.

      유튜브도 /videos와 /shorts 탭을 각각 방문한다(L2a).
      짧은 영상 포맷이 별도 탭으로 분리되면서 생긴 공통 문제.
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
    #
    # 왜 버킷을 나누나: 한도가 탭별로 독립이어야 한다.
    # 하나로 합치면 그리드 10개가 먼저 차서 릴스를 하나도 못 받는다.
    #
    # by_id는 external_id → post dict 참조. 아래 중복 병합에 쓴다.
    captured = {"pages": [], "posts": [], "reels": [], "seen": set(), "by_id": {}}

    def _on_response(response):
        name = _friendly_name(response)
        if name:
            # 목록 쿼리가 아니어도 이름은 기록한다.
            # 새 쿼리 이름을 발견하는 게 목적이다.
            result["seen_names"].add(name)
            _SEEN_QUERY_NAMES[name] = _SEEN_QUERY_NAMES.get(name, 0) + 1
            if DEBUG_GQL:
                known = (name in L2_POSTS_QUERY_NAMES
                         or name in L2_REELS_QUERY_NAMES)
                print(f"  [gql]{'*' if known else ' '} {name}", flush=True)
                # ↑ *가 붙은 게 우리가 잡는 쿼리. 안 붙은 건 무시하는 것.
                #   새 이름이 * 없이 계속 보이면 config에 추가 검토.

        if not _is_posts_graphql(response):
            return

        is_reels = name in L2_REELS_QUERY_NAMES
        bucket = captured["reels"] if is_reels else captured["posts"]
        limit = L2_REELS_LIMIT if is_reels else L2_POST_LIMIT
        if len(bucket) >= limit:
            return      # 이미 목표치 도달. 더 파싱할 이유가 없다.

        data = _read_json(response)
        if data is None:
            return
        captured["pages"].append(data)   # raw 저장용. 파싱 성패와 무관하게 보관

        try:
            if is_reels:
                posts = _parse_reels(data)      # clips 스키마
            else:
                posts, _page_info = parse_posts(data)   # timeline 스키마
        except Exception as e:
            # 파싱 실패해도 리스너가 죽으면 안 된다.
            # 이 응답만 버리고 다음을 기다린다.
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
                #
                # ★ 이 병합이 이 파일에서 가장 섬세한 부분이다.
                #
                #   같은 릴스가 그리드와 릴스 탭 양쪽에서 온다:
                #     그리드 → taken_at ✅ caption ✅ view_count ❌
                #     릴스탭 → taken_at ❌ caption ❌ view_count ✅
                #
                #   단순 dedup이면 먼저 온 쪽만 남아 절반의 정보를 잃는다.
                #   → None인 필드만 채운다. 이미 값이 있으면 안 덮는다.
                #     (먼저 온 값이 더 정확하다는 전제. 특히 taken_at은
                #      그리드의 실제값이 pk 복원값보다 정확하다)
                prev = captured["by_id"].get(eid)
                if prev is not None:
                    for k, v in p.items():
                        if v is not None and prev.get(k) is None:
                            prev[k] = v
                continue

            captured["seen"].add(eid)
            captured["by_id"][eid] = p   # bucket 과 같은 객체를 참조
            # ↑ 같은 dict를 가리키므로 by_id로 수정하면 bucket에도 반영된다.
            #   복사본을 만들면 병합 결과가 최종 결과에 안 들어간다.
            bucket.append(p)
            if len(bucket) >= limit:
                return

    def _wait_grace(bucket_key, limit):
        """목표 개수를 채울 때까지 짧게 기다린다.

        L1과 같은 패턴. sync API에서 이벤트를 받으려면
        wait_for_timeout으로 이벤트 루프를 돌려야 한다.
        (time.sleep은 루프를 멈춰서 리스너가 호출되지 않는다)

        150ms씩 쪼개는 이유: 목표를 채우면 즉시 빠져나가려고.
        """
        deadline = time.monotonic() + (L2_GRAPHQL_GRACE_MS / 1000.0)
        while (len(captured[bucket_key]) < limit
               and time.monotonic() < deadline):
            page.wait_for_timeout(150)

    def _scroll_more(bucket_key, limit):
        """유예로 부족하면 스크롤해서 다음 페이지를 유발한다.

        인스타는 스크롤해야 다음 GraphQL 요청이 나간다.
        스크롤 → 응답 도착 → 리스너가 잡음 → bucket 증가.

        종료 조건 두 가지:
          ① 목표 개수 도달
          ② 스크롤해도 안 늘어남 (STALL 2회) = 바닥

        STALL을 2회로 둔 이유: 1회면 로딩이 잠깐 느린 것을
        바닥으로 오판한다.

        L2_SCROLL_DELAY가 (1200, 2600) 범위인 것도 의도다.
        고정 간격이면 기계적 패턴이라 탐지된다.
        """
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
        # ↑ goto 실패해도 계속 진행. 부분 로드된 상태에서
        #   GraphQL이 이미 왔을 수 있다.

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
        # ★ 순서가 중요하다. 릴스 탭으로 이동하면 page.content()가
        #   릴스 페이지 HTML을 반환한다. 프로필 HTML을 원하면 지금 떠야 한다.
        try:
            result["html"] = page.content()
        except Exception:
            result["html"] = None

        # ===== ② 릴스 탭 (항상 방문) =====
        # ⚠️ 리스너가 살아있는 이 블록 안에서 방문해야 캡처된다.
        #    finally에서 리스너를 떼므로, try 블록 밖으로 나가면 늦다.
        if L2_COLLECT_REELS and not result["blocked"]:
            try:
                page.goto(reels_url, wait_until="domcontentloaded",
                          timeout=L2_GOTO_TIMEOUT_MS)
                result["reels_visited"] = True

                if _looks_blocked(page.url):
                    result["blocked"] = True
                    # ↑ 그리드는 됐는데 릴스에서 차단당하는 경우가 있다.
                    #   여기서 잡아야 그리드 결과만 저장하고 넘어간다.
                else:
                    _wait_grace("reels", L2_REELS_LIMIT)
                    if len(captured["reels"]) < L2_REELS_LIMIT:
                        _scroll_more("reels", L2_REELS_LIMIT)
            except PWTimeout:
                pass
            except Exception as e:
                # 릴스 탭 실패는 치명적이지 않다. 그리드 결과는 살아 있다.
                if DEBUG_GQL:
                    print(f"  [reels 탭 실패] {e!r}", flush=True)

    finally:
        # ⚠️ page 는 전 계정에서 재사용된다. 리스너를 반드시 해제할 것.
        #    안 떼면 계정마다 누적되어 후반부에 O(n²)로 느려지고,
        #    이전 계정의 응답이 현재 captured에 섞인다.
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    # 그리드에서 HTML을 못 떴으면 지금이라도 (릴스 페이지 HTML이지만 없는 것보단 낫다)
    if result["html"] is None:
        try:
            result["html"] = page.content()
        except Exception:
            result["html"] = None

    # 탭별 한도만큼 취해 합치고 최신순 정렬.
    # 릴스의 taken_at 은 pk 복원값이라 정밀도가 낮다(L3 에서 보정).
    result["n_grid"] = len(captured["posts"])
    result["n_reels"] = len(captured["reels"])
    # ↑ 탭별 개수를 따로 남긴다. 진단용.
    #   "n_reels=0인 계정이 몇 개인가"로 ②번 문제를 발견했다.
    merged = (captured["posts"][:L2_POST_LIMIT]
              + captured["reels"][:L2_REELS_LIMIT])
    merged.sort(key=lambda p: p.get("taken_at") or 0, reverse=True)
    # ↑ taken_at이 None이면 0으로 취급해 맨 뒤로 보낸다.
    #   (None끼리 비교하면 TypeError)

    result["posts"] = merged
    result["raw_pages"] = captured["pages"]
    return result


# =========================================================
# DB 저장
# =========================================================
def save_to_db(conn, channel_id, posts, captured_at=None):
    """게시물 + 스냅샷 저장."""
    if captured_at is None:
        captured_at = datetime.now()
        # 루프 밖에서 한 번 정한다. 같은 배치의 스냅샷은 같은 시각이어야
        # 나중에 "이 시점의 데이터"로 묶어 볼 수 있다.
    saved = 0
    with conn.cursor() as cur:
        for p in posts:
            if not p.get("external_id"):
                continue
            cur.execute(INSERT_CONTENT, (
                channel_id,
                p["external_id"],
                p.get("content_type", "feed_image"),
                #  ↑ 기본값이 feed_image. parse_posts가 타입을 못 정하면
                #    가장 흔한 유형으로 둔다.
                p.get("is_paid_promotion", 0),
                _dt(p.get("taken_at")),
                p.get("duration_sec"),
                p.get("caption_text"),
                captured_at,
            ))
            content_id = cur.lastrowid
            if not content_id:
                # LAST_INSERT_ID 트릭이 있어도 0이 나오는 경우 폴백
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
                #  ↑ view_count는 릴스만 값이 있다. 피드는 None.
                #    calc_metric의 fetch_rows가 "view_count OR like_count"로
                #    조건을 잡는 이유가 이것이다.
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

    ★ TODO가 남아 있다. import_l1이 PRIVATE 계정을
      channel_existence_status='private'로 적재하는데,
      아래 조건은 ('normal','unknown')만 받으므로 실제로는 제외된다.
      의도한 동작이지만 주석이 낡았다.

    external_channel_id IS NOT NULL이 곧 "L1 성공"의 증거다.
    (import_l1이 user_id를 여기 저장한다)

    'unknown'을 포함하는 이유: import_l1을 안 거치고 seed만 돈
    채널도 시도해보려는 것. 다만 그런 채널은 external_channel_id가
    없어서 첫 조건에서 걸린다. 사실상 무의미한 조건.
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
        # ★ error_type='empty'도 제외한다.
        #   게시물이 정말 0개인 계정을 매번 다시 확인할 이유가 없다.
        #
        #   ⚠️ 그런데 ①번 사고가 바로 이 지점이었다.
        #     쿼리 이름을 못 잡아서 'empty'로 기록된 193개가
        #     이 조건 때문에 재시도되지 않았다.
        #     config에 이름을 추가한 뒤 그 로그를 지워야 다시 수집된다.
        #     → 'empty'를 성공으로 취급하는 게 편하지만 위험하다.

    sql += " ORDER BY c.channel_id"
    if limit:
        sql += " LIMIT %d" % int(limit)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _username_from_row(url):
    """channel_url_normalized 에서 username 추출.

    seed/import_l1이 만든 URL 형식(https://www.instagram.com/{username}/)에
    의존한다. 형식이 바뀌면 여기도 고쳐야 한다.
    """
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
        # ↑ 세션 없이 시작하면 전부 차단으로 처리되고 시간만 날린다.

    conn = pymysql.connect(**DB)
    try:
        targets = fetch_targets(conn, limit, resume)
        log.info("[L2] 대상 계정: %d개", len(targets))
        if not targets:
            log.info("처리할 계정 없음 (다 끝났거나 channels 에 인스타 행이 없음).")
            return

        ok = none = err = blocked = 0
        block_streak = 0
        # ★ blocked(누적)와 block_streak(연속)을 분리했다.
        #   틱톡 l2.py는 한 변수를 두 용도로 써서 최종 통계가 틀린다.
        #   여기는 제대로 나눠져 있다.

        # 목록 수집에 쓰이는 쿼리 이름 전체 (그리드 + 릴스).
        # 미등록 경고 판정에 사용 — 릴스 쿼리를 빼먹으면 오탐이 난다.
        known_names = set(L2_POSTS_QUERY_NAMES) | set(L2_REELS_QUERY_NAMES)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            # ⚠️ 컨텍스트 옵션은 login.py 와 반드시 동일해야 한다.
            #    UA/viewport/locale 이 어긋나면 인스타는
            #    "같은 쿠키인데 다른 브라우저"로 보고 세션을 의심한다.
            #
            # ★ context_kwargs()를 쓴다. L1은 옵션을 직접 조립해서
            #   viewport와 timezone_id가 빠져 있었는데, 여기는 제대로 됐다.
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
                        # 성공/실패 무관. 나중에 원인을 봐야 할 수 있다.
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
                            # ★ 목록 쿼리를 하나도 못 잡았으면 '진짜 빈 계정'이
                            #   아니라 friendly-name 미등록일 수 있다.
                            #
                            #   ①번 사고(193개)의 재발 방지 장치다.
                            #   raw_pages가 비어 있는데(= 목록 쿼리를 하나도
                            #   못 잡음) seen_names에 모르는 이름이 있으면
                            #   경고를 띄운다.
                            #
                            #   "게시물 0개"로 조용히 넘어가지 않고
                            #   "이건 우리가 못 알아본 것일 수 있다"를 알려준다.
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
                        block_streak = 0   # 성공하면 연속 카운터 리셋
                        log_l2(conn, channel_id, url, STATUS_OK,
                               r["http_status"], None, None)
                        log.info("  [%d/%d] OK    @%s (posts=%d g=%d r=%d)",
                                 i, len(targets), username, n,
                                 r["n_grid"], r["n_reels"])
                        # ↑ 그리드/릴스 개수를 따로 찍는다.
                        #   "g=10 r=0"이 계속 나오면 릴스 수집에 문제가 있다는 신호.

                    except Exception as e:
                        # 계정 1건 예외로 전체가 죽지 않게 격리
                        err += 1
                        log_l2(conn, channel_id, url, STATUS_FAILED,
                               None, "exception", str(e)[:400])
                        log.info("  [%d/%d] ERR   @%s | %r",
                                 i, len(targets), username, e)

                    # 계정 간 8~15초 랜덤 대기
                    lo, hi = L2_CHANNEL_GAP
                    time.sleep(random.uniform(lo, hi))

            finally:
                # 크롤링 중 롤링된 쿠키를 다시 저장해 세션 수명을 늘린다.
                # ⚠️ 예외/중단 시에도 반드시 실행되도록 finally 에 둔다.
                #
                # ★ L1에는 없는 처리다.
                #   인스타는 요청할 때마다 쿠키를 조금씩 갱신한다(rolling).
                #   그걸 안 저장하면 다음 실행 때 오래된 쿠키를 쓰게 되고,
                #   세션이 빨리 만료된다.
                #   → 크롤이 끝날 때마다 세션 파일을 갱신해 수명을 늘린다.
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
        #
        # ★ ①번 사고의 최종 방어선.
        #   실행 내내 본 쿼리 이름 중 config에 없는 것을 빈도순으로 출력한다.
        #   인스타가 새 쿼리를 도입하면 여기서 즉시 드러난다.
        #   → "왜 게시물이 0개지?"를 며칠 뒤에 발견하는 대신
        #     실행 직후에 알 수 있다.
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