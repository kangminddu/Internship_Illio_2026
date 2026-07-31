# -*- coding: utf-8 -*-
# config.py  (Instagram)
#
# L1 기존 설정 + L2 (게시물 목록) + L3 (댓글) 설정.
# L2/L3 는 L1 과 동일하게 Playwright 단일 세션 + 느린 딜레이로 차단을 피한다.
# request 직접 호출(찍어내기)은 인스타에서 429 유발 → 사용하지 않는다.
#
# ★ 세 플랫폼 중 가장 보수적인 설정이다.
#   유튜브 L1  : 1.2초 간격, 워커 4개
#   틱톡 L1    : 1초 간격, 워커 1~3개
#   인스타 L1  : 8~15초 간격, 단일 세션      ← 10배 이상 느리다
#
#   인스타는 로그인 세션이 필수인데, 차단당하면 계정 자체가 잠긴다.
#   IP를 바꿔도 계정이 죽으면 복구가 어렵다. → 속도보다 세션 보호가 우선.

from pathlib import Path

# ── 경로 (파일 위치 기준, 실행 위치와 무관) ──
# 유튜브 config는 EXPORT_DIR="youtube/output" 상대경로라 실행 위치에 따라
# 결과물이 다른 곳에 생긴다. 틱톡·인스타는 __file__ 기준으로 고쳤다.
INSTA_DIR = Path(__file__).resolve().parent
SESSION_FILE = INSTA_DIR / "session" / "instagram.json"
# ↑ storage_state JSON. sessionid 쿠키가 들어 있어 .gitignore 대상.
#   (틱톡은 프로필 디렉터리 통째로 쓰는데 인스타는 JSON 하나)
OUTPUT_DIR = INSTA_DIR / "output"

# ★ raw 응답을 파일로 남긴다 — 유튜브·틱톡에는 없는 구조.
#
#   인스타는 GraphQL 응답 구조가 자주 바뀌고, 계정 유형(공개/비공개/
#   크리에이터/비즈니스)마다 필드가 다르다. 파싱이 실패했을 때
#   "그때 실제로 뭘 받았는지"가 없으면 원인을 못 찾는다.
#   → 응답 원본을 그대로 저장해두고 나중에 재파싱할 수 있게 했다.
#   (실제로 L2 쿼리 이름 문제를 이 덤프로 찾아냈다 — 아래 참고)
HTML_DIR = OUTPUT_DIR / "raw" / "l1_html"
GRAPHQL_DIR = OUTPUT_DIR / "raw" / "l1_api_json"
RESULTS_FILE = OUTPUT_DIR / "l1_results.jsonl"
# ↑ jsonl(줄 단위 JSON). 크롤링 중 한 줄씩 append할 수 있어
#   중간에 죽어도 앞부분이 살아있다. 일반 JSON은 전체를 다시 써야 한다.
LOG_DIR = OUTPUT_DIR / "logs"
LOG_FILE = LOG_DIR / "l1.log"

# L2 raw 저장 경로 (게시물 목록 GraphQL verbatim)
L2_GRAPHQL_DIR = OUTPUT_DIR / "raw" / "l2_posts_json"
L2_HTML_DIR = OUTPUT_DIR / "raw" / "l2_html"
L2_LOG_FILE = LOG_DIR / "l2.log"

# ── 세션 / API ──
APP_ID = "936619743392459"      # Instagram web app id (공개값)
# GraphQL 요청 헤더(x-ig-app-id)에 필요하다.
# 모든 사용자에게 동일한 공개 상수라 노출돼도 문제없다.

# ── DB (YouTube/TikTok 와 같은 fandom_crm 공유, platform 컬럼으로 구분) ──
DB = dict(host="127.0.0.1", port=3306, user="root", password="",
          database="fandom_crm", charset="utf8mb4")
PLATFORM = "instagram"          # channels.platform 값

# ── 브라우저 컨텍스트 (login.py와 맞출 것) ──
#
# ★ "login.py와 맞출 것"이 핵심 주석이다.
#   인스타는 브라우저 지문(viewport/locale/timezone/UA)을 세션 검증에 쓴다.
#   로그인할 때와 크롤링할 때 값이 다르면 재인증을 요구하거나
#   의심 활동으로 판정한다.
#   → context_kwargs()로 함수화해서 login.py와 크롤러가 같이 쓴다.
#     (틱톡의 browser.persistent_launch_kwargs()와 같은 역할)
LOCALE = "ko-KR"
TIMEZONE_ID = "Asia/Seoul"
VIEWPORT = {"width": 1400, "height": 900}
USER_AGENT = None       # None이면 Playwright 기본값.
                        # login.py가 실제 Chrome 실행파일을 쓰므로
                        # 기본 UA도 자연스럽다.
HEADLESS = False        # 인스타는 headless 탐지가 있어 창을 띄운다.

def context_kwargs(storage_state = None):
    """브라우저 컨텍스트 옵션을 한 곳에서 생성.

    login.py는 storage_state 없이 호출(새 세션을 만드는 중이므로),
    크롤러는 SESSION_FILE을 넘겨 로그인 상태로 시작한다.
    나머지 옵션은 동일하게 유지된다.
    """
    kw = {
        "viewport": VIEWPORT,
        "locale": LOCALE,
        "timezone_id": TIMEZONE_ID,
    }
    if USER_AGENT:
        kw["user_agent"] = USER_AGENT
    if storage_state:
        kw["storage_state"] = str(storage_state)
    return kw

# ── L1 (Playwright 단일 세션, 무거움 → 살살) ──
L1_DELAY_MIN = 8.0
L1_DELAY_MAX = 15.0
# ↑ 계정당 8~15초. 1,881채널이면 4~8시간이다.
#   범위를 두는 이유: 정확히 10초마다 요청하면 기계적 패턴이라 탐지된다.
L1_GOTO_TIMEOUT_MS = 30000
L1_RENDER_WAIT_MS = 1500        # 페이지 로드 후 렌더링 대기

# ── L1 GraphQL 캡처 ──
#
# ★ 인스타 수집의 핵심 구조.
#
#   유튜브 : HTML에 박힌 ytInitialData를 정규식으로 추출
#   틱톡   : HTML(SSR) + XHR(CSR) 병행
#   인스타 : GraphQL 응답을 page.on("response")로 가로채기  ← 유일한 경로
#
#   인스타는 HTML에 데이터를 거의 안 담는다. React가 GraphQL로
#   전부 가져온다. 그래서 응답을 훔쳐보는 방식이 필수다.
L1_GRAPHQL_GRACE_MS = 4000      # 페이지 로드 후 GraphQL 응답을 기다리는 유예
GRAPHQL_URL_PART = "/api/graphql"                     # L1 프로필 쿼리 URL
PROFILE_QUERY_NAME = "PolarisProfilePageContentQuery"  # L1 friendly-name
# ↑ friendly-name: GraphQL 요청 본문의 fb_api_req_friendly_name 필드.
#   같은 URL로 수십 종류의 쿼리가 날아오므로, 이 이름으로 골라내야 한다.

# ============================================================
# L2 (게시물 목록) 설정
# ============================================================
# 게시물 목록 GraphQL:
#   URL:        /graphql/query   ← L1 의 /api/graphql 과 다름!
#   root_field: xdt_api__v1__feed__user_timeline_graphql_connection
#
# ⚠️ friendly-name 은 계정에 따라 여러 종류가 온다 (2026-07 실측).
#    - PolarisProfilePostsQuery
#        → 게시물이 적어 첫 화면에 다 들어가는 계정. 페이지네이션 미발생.
#    - PolarisProfilePostsTabContentQuery_connection
#        → 스크롤 페이지네이션이 발생하는 계정.
#    처음엔 _connection 하나만 잡아서 소형 계정 193개를 empty 로 놓쳤음.
#    새로운 이름이 발견되면 여기에 추가만 하면 된다.
#
# ★ 이 주석이 이 파일에서 가장 중요하다.
#
#   "게시물이 0개"로 기록된 계정 193개를 조사해보니 실제로는 게시물이 있었다.
#   게시물이 적어 스크롤이 발생하지 않는 계정에는 인스타가 다른 이름의
#   쿼리를 보냈고, 크롤러가 그걸 못 알아본 것이다.
#
#   → 이 프로젝트 전반의 주제와 같다:
#     "데이터가 없다"와 "가져오지 못했다"를 구분하지 못한 사례.
#     다만 여기서는 파서가 응답을 '보고도 못 알아봤다'는 형태였다.
#
#   set으로 두고 이름을 추가만 하면 되게 만든 것도 이 경험의 결과다.
L2_GRAPHQL_URL_PART = "/graphql/query"
L2_POSTS_QUERY_NAMES = {
    "PolarisProfilePostsQuery",
    "PolarisProfilePostsTabContentQuery",
    "PolarisProfilePostsTabContentQuery_connection",
}

# [deprecated] 단일 이름 시절 상수. 구코드 호환용 — 신규 코드는 NAMES 를 쓸 것.
L2_POSTS_QUERY_NAME = "PolarisProfilePostsTabContentQuery_connection"

# 릴스 탭 (프로필 그리드에 릴스가 안 보이는 계정이 절반 가까이 된다.
#          2026-07 실측: 725채널 중 365개가 그리드에 릴스 0건)
#   URL:  /{username}/reels/
#   name: PolarisProfileReelsTabContentQuery  (SSR 아님, GraphQL 로 옴)
#
# ★ 유튜브의 쇼츠 문제와 같은 구조다.
#   유튜브 : 쇼츠 탭에 날짜가 없어 활동성 판정이 롱폼 편향
#   인스타 : 릴스가 그리드에 안 나와 절반 가까운 계정이 릴스 0건
#   → 짧은 영상 포맷이 별도 탭으로 분리되면서 생긴 공통 문제.
#     둘 다 별도 탭을 추가로 방문해서 해결했다.
L2_REELS_QUERY_NAMES = {
    "PolarisProfileReelsTabContentQuery",
    "PolarisProfileReelsTabContentQuery_connection",
    "PolarisClipsTabDesktopPaginationQuery",
}

# 릴스 탭도 방문할지. False 면 기존처럼 그리드만 수집.
# 요청이 계정당 2배가 되므로 끌 수 있게 해뒀다.
L2_COLLECT_REELS = True

# 수집 개수: 최근 N개 무조건. 기간 필터 없음(기간/활동성 판단은 metrics 단계).
#
# ★ 유튜브·틱톡과 다른 판단이다.
#   유튜브 L2b는 published_at으로 정렬해 최근 15개를 고른다.
#   인스타는 그냥 최근 10개를 받고, 기간 필터는 metrics가 한다.
#   → 수집 단계는 '가져오는 것'만, 판단은 '계산하는 곳'에서.
#     책임이 더 깔끔하지만, 대신 오래된 게시물만 있는 계정도
#     10개를 다 받아온다.
L2_POST_LIMIT = 10
L2_REELS_LIMIT = 10
# 스크롤 설정 (10개는 보통 첫 페이지에서 다 차서 스크롤 거의 안 함)
L2_MAX_SCROLLS = 5
L2_SCROLL_STALL = 2             # 새 게시물 안 늘면 N회 후 종료
                                # 1로 하면 로딩이 잠깐 느린 걸 바닥으로 오판

# 딜레이 (L1 계승 — 차단 회피용, 절대 짧게 하지 말 것)
# ↑ "절대 짧게 하지 말 것"이 경고로 남아 있다.
#   인스타는 차단당하면 IP가 아니라 계정이 잠긴다.
L2_GOTO_TIMEOUT_MS = 30000
L2_GRAPHQL_GRACE_MS = 5000      # 목록 GraphQL 대기 유예
L2_RENDER_WAIT_MS = 1500
L2_SCROLL_DELAY = (1200, 2600)  # 스크롤 사이 대기(ms)
L2_CHANNEL_GAP = (8.0, 15.0)    # 계정 간 딜레이(초) — L1 과 동일

# ============================================================
# L3 (댓글 수집) 설정
# ============================================================
# 댓글 GraphQL:
#   URL:        /graphql/query
#   root_field: xdt_api__v1__media__media_id__comments__connection
#
# ⚠️ L2 와 동일한 문제가 예상된다.
#    PaginationQuery 는 "댓글 더 보기"가 발생할 때만 날아오므로,
#    댓글이 적은 게시물은 다른 이름으로 첫 묶음이 온다.
#    아래는 후보 목록 — 디버그 리스너로 실제 이름을 확인한 뒤 확정할 것.
#
# ★ L2에서 겪은 문제를 L3에 미리 반영했다.
#   "이런 문제가 있을 것 같다"를 주석으로 남기고 후보를 넓게 잡아둔 것.
#   한 번 데인 패턴을 다음 단계에 선제 적용한 사례.
L3_GRAPHQL_URL_PART = "/graphql/query"
L3_COMMENTS_QUERY_NAMES = {
    "PolarisPostCommentsPaginationQuery",
    "PolarisPostCommentsQuery",
    "PolarisPostRootQuery",
}

# [deprecated] 단일 이름 시절 상수. 구코드 호환용.
L3_COMMENTS_QUERY_NAME = "PolarisPostCommentsPaginationQuery"

# raw 저장 경로
L3_GRAPHQL_DIR = OUTPUT_DIR / "raw" / "l3_comments_json"
L3_HTML_DIR = OUTPUT_DIR / "raw" / "l3_html"
L3_LOG_FILE = LOG_DIR / "l3.log"

# 수집 개수
# (현재는 첫 페이지 Top Comments만 수집, pagination 미사용)
#
# ★ 한계를 명시해뒀다. 인스타는 기본이 '인기 댓글' 정렬이라
#   첫 페이지가 시간순 최신이 아니다. 최신 댓글을 보려면
#   pagination을 돌려야 하는데 요청량이 크게 늘어 미구현.
L3_COMMENT_LIMIT = 50
L3_MIN_COMMENTS = 3         # 이보다 적으면 지표 계산에서 제외할 하한
L3_MAX_AGE_MONTHS = 12      # 너무 오래된 게시물은 댓글 수집 대상에서 제외
# 댓글 GraphQL은 게시물 진입 직후 대부분 수신됨
L3_GOTO_TIMEOUT_MS = 30000
L3_GRAPHQL_GRACE_MS = 5000
L3_RENDER_WAIT_MS = 1500

# 댓글은 스크롤하지 않음(현재 정책)
# 게시물 진입만으로 첫 묶음(약 12~24개)이 오므로,
# 스크롤 없이 그걸로 끝낸다. 차단 위험 대비 수확이 적다는 판단.
L3_MAX_SCROLLS = 0
L3_SCROLL_STALL = 0
L3_SCROLL_DELAY = (1200, 2600)

# 게시물 간 딜레이 (L2와 동일)
L3_CONTENT_GAP = (8.0, 15.0)

# ── 공통 ──
BATCH_LIMIT = None             # None이면 전체. 숫자면 앞 N개만.
STOP_ON_429 = 3                # 429/CHALLENGE 연속 N회 → 중단 (세션 보호)
                               # ↑ CHALLENGE = 인스타의 본인확인 요구.
                               #   여기 걸리면 계정이 잠길 수 있어 즉시 멈춘다.
STOP_ON_BLOCK = 3              # L2/L3 연속 차단 서킷브레이커