# -*- coding: utf-8 -*-
# config.py  (Instagram)
#
# L1 기존 설정 + L2 (게시물 목록) 설정 추가.
# L2 는 L1 과 동일하게 Playwright 단일 세션 + 느린 딜레이로 차단을 피한다.
# request 직접 호출(찍어내기)은 인스타에서 429 유발 → 사용하지 않는다.

from pathlib import Path

# ── 경로 (파일 위치 기준, 실행 위치와 무관) ──
INSTA_DIR = Path(__file__).resolve().parent
SESSION_FILE = INSTA_DIR / "session" / "instagram.json"
OUTPUT_DIR = INSTA_DIR / "output"
HTML_DIR = OUTPUT_DIR / "raw" / "l1_html"
GRAPHQL_DIR = OUTPUT_DIR / "raw" / "l1_api_json"
RESULTS_FILE = OUTPUT_DIR / "l1_results.jsonl"
LOG_DIR = OUTPUT_DIR / "logs"
LOG_FILE = LOG_DIR / "l1.log"

# L2 raw 저장 경로 (게시물 목록 GraphQL verbatim)
L2_GRAPHQL_DIR = OUTPUT_DIR / "raw" / "l2_posts_json"
L2_HTML_DIR = OUTPUT_DIR / "raw" / "l2_html"
L2_LOG_FILE = LOG_DIR / "l2.log"

# ── 세션 / API ──
APP_ID = "936619743392459"      # Instagram web app id (공개값)

# ── DB (YouTube/TikTok 와 같은 fandom_crm 공유, platform 컬럼으로 구분) ──
DB = dict(host="127.0.0.1", port=3306, user="root", password="",
          database="fandom_crm", charset="utf8mb4")
PLATFORM = "instagram"          # channels.platform 값

# ── 브라우저 컨텍스트 (login.py와 맞출 것) ──
LOCALE = "ko-KR"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
HEADLESS = False

# ── L1 (Playwright 단일 세션, 무거움 → 살살) ──
L1_DELAY_MIN = 8.0
L1_DELAY_MAX = 15.0
L1_GOTO_TIMEOUT_MS = 30000
L1_RENDER_WAIT_MS = 1500

# ── L1 GraphQL 캡처 ──
L1_GRAPHQL_GRACE_MS = 4000
GRAPHQL_URL_PART = "/api/graphql"                     # L1 프로필 쿼리 URL
PROFILE_QUERY_NAME = "PolarisProfilePageContentQuery"  # L1 friendly-name

# ============================================================
# L2 (게시물 목록) 설정
# ============================================================
# 게시물 목록 GraphQL (실측 확인됨, 2026-07):
#   URL:            /graphql/query   ← L1 의 /api/graphql 과 다름!
#   friendly-name:  PolarisProfilePostsTabContentQuery_connection
#   root_field:     xdt_api__v1__feed__user_timeline_graphql_connection
L2_GRAPHQL_URL_PART = "/graphql/query"
L2_POSTS_QUERY_NAME = "PolarisProfilePostsTabContentQuery_connection"

# 수집 개수: 최근 N개 무조건. 기간 필터 없음(기간/활동성 판단은 metrics 단계).
L2_POST_LIMIT = 10

# 스크롤 설정 (10개는 보통 첫 페이지에서 다 차서 스크롤 거의 안 함)
L2_MAX_SCROLLS = 5
L2_SCROLL_STALL = 2             # 새 게시물 안 늘면 N회 후 종료

# 딜레이 (L1 계승 — 차단 회피용, 절대 짧게 하지 말 것)
L2_GOTO_TIMEOUT_MS = 30000
L2_GRAPHQL_GRACE_MS = 5000      # 목록 GraphQL 대기 유예
L2_RENDER_WAIT_MS = 1500
L2_SCROLL_DELAY = (1200, 2600)  # 스크롤 사이 대기(ms)
L2_CHANNEL_GAP = (8.0, 15.0)    # 계정 간 딜레이(초) — L1 과 동일

# ============================================================
# L3 (댓글 수집) 설정
# ============================================================
# 댓글 GraphQL (실측 확인됨, 2026-07):
#   URL:            /graphql/query
#   friendly-name:  PolarisPostCommentsPaginationQuery
#   root_field:     xdt_api__v1__media__media_id__comments__connection
L3_GRAPHQL_URL_PART = "/graphql/query"
L3_COMMENTS_QUERY_NAME = "PolarisPostCommentsPaginationQuery"

# raw 저장 경로
L3_GRAPHQL_DIR = OUTPUT_DIR / "raw" / "l3_comments_json"
L3_HTML_DIR = OUTPUT_DIR / "raw" / "l3_html"
L3_LOG_FILE = LOG_DIR / "l3.log"

# 수집 개수
# (현재는 첫 페이지 Top Comments만 수집, pagination 미사용)
L3_COMMENT_LIMIT = 50

# 댓글 GraphQL은 게시물 진입 직후 대부분 수신됨
L3_GOTO_TIMEOUT_MS = 30000
L3_GRAPHQL_GRACE_MS = 5000
L3_RENDER_WAIT_MS = 1500

# 댓글은 스크롤하지 않음(현재 정책)
L3_MAX_SCROLLS = 0
L3_SCROLL_STALL = 0
L3_SCROLL_DELAY = (1200, 2600)

# 게시물 간 딜레이 (L2와 동일)
L3_CONTENT_GAP = (8.0, 15.0)

# ── 공통 ──
BATCH_LIMIT = None             # None이면 전체. 숫자면 앞 N개만.
STOP_ON_429 = 3                # 429/CHALLENGE 연속 N회 → 중단 (세션 보호)
STOP_ON_BLOCK = 3              # L2 연속 차단 서킷브레이커

