# -*- coding: utf-8 -*-
"""
l1.py  (리팩터링 + config 연동판)

무엇을 하는가
------
인스타 계정 프로필을 방문해 팔로워/게시물수/bio/user_id를 얻고,
결과를 jsonl 파일에 한 줄씩 append한다.

★ DB를 건드리지 않는다. 유튜브·틱톡과 결정적으로 다른 점이다.
------
    유튜브 : crawl → 바로 DB INSERT
    틱톡   : crawl → 바로 DB INSERT
    인스타 : crawl → jsonl 파일 → (별도) import_l1.py가 DB 적재

  왜 나눴나:
    계정당 8~15초라 1,881개에 4~8시간이 걸린다. 크롤 도중에
    DB 스키마나 적재 규칙을 바꾸고 싶어도 다시 크롤할 수 없다.
    → 원본을 파일로 남기고 적재만 다시 돌린다.
    크롤러는 세션 유지에만 집중하면 된다.

설계 원칙 (프로젝트 문서 기준)
- 원본(raw)은 항상 저장: HTML은 성패 무관 항상, GraphQL JSON은 응답 있을 때만.
- 상태 판별은 GraphQL(ProfilePageContentQuery) 우선, HTML은 보조 수단.
- GraphQL 이 안 오는 것은 오류가 아니라 정상 분기 → HTML 판별로 넘어간다.
- 없는 계정에서 timeout 을 통째로 소비하지 않는다(grace 만 짧게).
- JSONL은 append-only 실행 로그. 최종 dedup은 Export가 담당.
- Resume는 확정 상태(SUCCESS/PRIVATE/NOT_FOUND)만 제외, 나머지는 재시도.

  ↑ 마지막 원칙이 이 프로젝트 전체를 관통하는 주제다.
    "확정 실패"와 "불확실한 실패"를 구분한다.
    TIMEOUT/ERROR/CHALLENGE는 다음 실행에서 재시도되고,
    NOT_FOUND는 영구 제외된다.

crawl_one 흐름
  (1) 페이지 방문: goto '자체' 타임아웃만 담당 (GraphQL 대기와 완전 분리)
  (2) GraphQL 유예 대기: 짧게만. 안 오면 HTML 판별로.
  (3) HTML 항상 저장
  (4) GraphQL 저장 + 파싱 (왔을 때만)
  (5) 상태 판별: GraphQL 우선, 없으면 HTML

★ (1)과 (2)를 분리한 것이 이 파일의 핵심 개선이다.
  초기 버전은 "GraphQL이 올 때까지 goto 타임아웃 안에서 기다리는" 구조였다.
  그러면 없는 계정에서 GraphQL이 영영 안 와서 30초를 통째로 날린다.
  1,881개 중 700개가 없는 계정이면 그것만 6시간이다.
  → goto는 goto만 책임지고, GraphQL은 별도 짧은 유예(4초)로 기다린다.
"""

import json
import logging
import random
import re
import time
from datetime import datetime

from playwright.sync_api import sync_playwright
# ★ sync API를 쓴다. 유튜브 L3와 틱톡은 async인데 여기만 동기.
#   인스타 L1은 계정을 하나씩 순차 처리한다(병렬 없음).
#   단일 세션을 8시간 유지하는 게 목적이라 동시성이 필요 없고,
#   동기 코드가 훨씬 읽기 쉽다.
from playwright.sync_api import TimeoutError as PWTimeout

# ── config 연동 (실행 방식에 따라 import 경로 대응) ──
# 패키지 실행(-m instagram.steps.l1)과 디렉터리 내 직접 실행 둘 다 되게.
# 개발 편의 장치인데, except Exception이 진짜 import 에러(오타 등)도
# 삼켜서 원인을 가리는 부작용이 있다.
try:
    from instagram.config import (
        SESSION_FILE, OUTPUT_DIR, HTML_DIR, GRAPHQL_DIR, RESULTS_FILE,
        LOG_DIR, LOG_FILE, LOCALE, USER_AGENT, HEADLESS,
        L1_DELAY_MIN, L1_DELAY_MAX, L1_GOTO_TIMEOUT_MS, L1_RENDER_WAIT_MS,
        L1_GRAPHQL_GRACE_MS, GRAPHQL_URL_PART, PROFILE_QUERY_NAME,
        BATCH_LIMIT, STOP_ON_429,
    )
except Exception:
    from config import (
        SESSION_FILE, OUTPUT_DIR, HTML_DIR, GRAPHQL_DIR, RESULTS_FILE,
        LOG_DIR, LOG_FILE, LOCALE, USER_AGENT, HEADLESS,
        L1_DELAY_MIN, L1_DELAY_MAX, L1_GOTO_TIMEOUT_MS, L1_RENDER_WAIT_MS,
        L1_GRAPHQL_GRACE_MS, GRAPHQL_URL_PART, PROFILE_QUERY_NAME,
        BATCH_LIMIT, STOP_ON_429,
    )

try:
    from instagram.lib.graphql_parser import parse_profile
except Exception:
    from lib.graphql_parser import parse_profile                # steps/ 안에서 직접 실행


# =========================================================
# 상태 상수
# =========================================================
# ★ 9가지 상태로 세분화한다.
#   유튜브 classify_existence는 3가지(deleted/suspended/unknown),
#   틱톡은 4가지(ok/not_found/blocked/server_error)인데 여기는 9개다.
#
#   인스타가 실패 유형이 많아서다:
#     - 로그인 세션 만료 → 로그인 페이지로 리다이렉트
#     - 본인확인 요구(challenge) → 계정이 잠길 수 있는 위험 신호
#     - rate limit → 잠시 쉬어야 함
#   이걸 뭉뚱그리면 "세션을 재발급해야 하는지" 판단할 수 없다.
STATUS_SUCCESS = "SUCCESS"
STATUS_PRIVATE = "PRIVATE"              # 계정은 있는데 게시물이 안 보임
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_LOGIN_REQUIRED = "LOGIN_REQUIRED"  # 세션 만료 or 로그아웃됨
STATUS_CHALLENGE = "CHALLENGE"            # 인스타가 본인확인 요구 ← 가장 위험
STATUS_RATE_LIMITED = "RATE_LIMITED"
STATUS_NETWORK_ERROR = "NETWORK_ERROR"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_ERROR = "ERROR"

# ★ 확정 상태만 resume에서 제외한다.
#   나머지(LOGIN_REQUIRED/CHALLENGE/TIMEOUT/ERROR 등)는 다음 실행에서 재시도.
#   이 튜플 하나가 "실패를 어떻게 다룰 것인가"의 답이다.
CONFIRMED_STATUSES = (STATUS_SUCCESS, STATUS_PRIVATE, STATUS_NOT_FOUND)

ALL_STATUSES = [
    STATUS_SUCCESS, STATUS_PRIVATE, STATUS_NOT_FOUND, STATUS_LOGIN_REQUIRED,
    STATUS_CHALLENGE, STATUS_RATE_LIMITED, STATUS_NETWORK_ERROR,
    STATUS_TIMEOUT, STATUS_ERROR,
]


# =========================================================
# 판별용 텍스트 패턴 (HTML 보조 판별 전용)
# =========================================================
# ⚠️ 문자열 검색은 언어 설정에 따라 실패한다.
#    그래서 '보조 수단'이다. 주력은 GraphQL의 구조적 판별.
#    (틱톡 not_found.py가 문자열 대신 statusCode를 쓰는 것과 같은 이유)
#    한국어/영어 둘 다 넣어둔 건 세션 locale이 어긋날 때 대비.
NOT_FOUND_TEXTS = [
    "Sorry, this page isn",
    "isn't available",
    "페이지를 사용할 수 없습니다",
    "죄송합니다. 이 페이지를 사용할 수 없습니다",
]

PRIVATE_TEXTS = [
    "This Account is Private",
    "This account is private",
    "Account is Private",
    "비공개 계정입니다",
    "비공개 계정",
]


# =========================================================
# 로깅
# =========================================================
def setup_logging():
    """콘솔 + 파일 이중 출력.

    ★ 세 플랫폼 중 여기만 logging 모듈을 쓴다.
      유튜브·틱톡은 print만 쓰는데, 인스타 L1은 8시간짜리라
      파일로 남겨야 나중에 무슨 일이 있었는지 볼 수 있다.
      (문서 6번 '로그 파일화 습관' 미완료 항목의 유일한 예외)

    if logger.handlers 체크가 중요하다.
    이 모듈이 두 번 import되면 핸들러가 중복 등록되어
    같은 로그가 두 번씩 찍힌다.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("l1")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


# =========================================================
# reader 연동
# =========================================================
def load_rows(limit=None):
    """대상 계정 목록. DB가 아니라 엑셀에서 직접 읽는다.

    ★ 유튜브·틱톡은 DB의 channels에서 대상을 고르는데,
      인스타 L1은 엑셀을 바로 읽는다.

      DB에 아직 채널이 없어도 크롤할 수 있다는 뜻이고,
      seed → L1 순서를 강제하지 않는다.
      (실제로는 import_l1이 creator를 찾아야 하므로 seed가 먼저여야 한다)
    """
    try:
        from instagram.reader import get_instagram_rows
    except ImportError:
        from reader import get_instagram_rows

    rows = get_instagram_rows()
    if limit:
        rows = rows[:limit]
    return rows


# =========================================================
# 저장 헬퍼
# =========================================================
def _safe_name(username):
    """파일명으로 쓸 수 있게 특수문자 치환.
    인스타 username에 점(.)이 흔한데, 그건 파일명에 써도 안전하다."""
    return re.sub(r"[^\w.-]", "_", username)


def save_html(username, html):
    """HTML을 항상 저장한다 (성공/실패 무관).

    ★ 왜 실패한 것까지 저장하나:
      "왜 이 계정이 NOT_FOUND로 나왔지?"를 나중에 확인하려면
      그때 받은 HTML이 있어야 한다.
      실제로 L2의 쿼리 이름 문제(193개 오분류)를 이런 덤프로 찾아냈다.

      대가는 디스크다. 1,881개 × 수백 KB = 수백 MB.
      크롤이 안정화되면 지워도 되는 데이터.
    """
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    path = HTML_DIR / f"{_safe_name(username)}.html"
    path.write_text(html, encoding="utf-8")
    return path


def save_graphql_json(username, data):
    """GraphQL 응답 원본 저장. indent=2로 사람이 읽을 수 있게.

    파싱 로직을 바꿨을 때 재크롤 없이 이 파일들로 다시 파싱할 수 있다.
    """
    GRAPHQL_DIR.mkdir(parents=True, exist_ok=True)
    path = GRAPHQL_DIR / f"{_safe_name(username)}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def append_result(result):
    """jsonl에 한 줄 append.

    ★ append-only인 게 중요하다.
      일반 JSON은 전체를 다시 써야 해서, 중간에 죽으면 파일이 깨진다.
      jsonl은 줄 단위라 8시간 크롤 중 언제 죽어도 앞부분이 온전하다.

      그리고 같은 계정이 여러 번 나올 수 있다(재실행 등).
      dedup은 import_l1이 STATUS_PRIORITY로 처리한다.
      → 크롤러는 '기록'만 하고 '정리'는 다음 단계가 한다.
    """
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")


def load_completed():
    """확정 상태(SUCCESS/PRIVATE/NOT_FOUND)의 username을 모아 Resume에서 제외.

    ★ 이 함수가 곧 resume 로직이다.
      유튜브·틱톡은 crawl_logs 테이블을 보는데, 여기는 jsonl 파일을 읽는다.
      DB 없이도 재개가 되는 구조.

      확정 상태만 제외하는 게 핵심이다.
      TIMEOUT이나 CHALLENGE는 제외하지 않으므로,
      세션을 재발급하고 다시 돌리면 그것들부터 재시도된다.
    """
    completed = set()
    if not RESULTS_FILE.exists():
        return completed
    with RESULTS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue    # 깨진 줄은 무시. 한 줄 때문에 전체를 못 읽으면 곤란.
            if row.get("status") in CONFIRMED_STATUSES:
                completed.add(row.get("username"))
    return completed


# =========================================================
# HTML 보조 판별 헬퍼
# =========================================================
def extract_meta(html, prop):
    """<meta property="og:url" content="..."> 에서 content 추출.

    BeautifulSoup을 안 쓰고 정규식으로 하는 이유:
    의존성을 줄이고, 메타 태그 몇 개만 필요해서 파서를 통째로
    돌릴 이유가 없다. (인스타 HTML은 수백 KB다)

    두 단계로 나눈 이유: <meta ...> 태그를 먼저 다 뽑고
    그 안에서 property를 확인한다. 한 번에 하려면 정규식이
    복잡해지고 속성 순서(content가 property보다 앞에 오는 경우)에
    취약해진다.
    """
    for m in re.finditer(r"<meta\b[^>]*>", html, re.IGNORECASE):
        tag = m.group(0)
        if re.search(r'property\s*=\s*["\']' + re.escape(prop) + r'["\']', tag, re.IGNORECASE):
            cm = re.search(r'content\s*=\s*["\'](.*?)["\']', tag, re.IGNORECASE | re.DOTALL)
            if cm:
                return cm.group(1)
    return None


def extract_canonical(html):
    """<link rel="canonical" href="..."> 추출."""
    for m in re.finditer(r"<link\b[^>]*>", html, re.IGNORECASE):
        tag = m.group(0)
        if re.search(r'rel\s*=\s*["\']canonical["\']', tag, re.IGNORECASE):
            hm = re.search(r'href\s*=\s*["\'](.*?)["\']', tag, re.IGNORECASE)
            if hm:
                return hm.group(1)
    return None


def has_profile_meta(html, username):
    """이 HTML이 '해당 계정의 프로필 페이지'인지 구조적으로 확인.

    ★ 문자열 검색이 아니라 '구조'로 판별한다.

      og:url이나 canonical이 /{username}으로 끝나면
      인스타가 "이 페이지는 그 계정의 프로필이다"라고 선언한 것이다.
      언어 설정과 무관하고, 문구가 바뀌어도 안 깨진다.

      username을 인자로 받아 대조하는 것도 중요하다.
      단순히 "프로필 페이지인가"가 아니라
      "내가 요청한 그 계정의 프로필인가"를 본다.
      → 리다이렉트로 다른 계정에 갔을 때를 잡아낸다.

    og:title 폴백("(@username)" 포함)은 og:url이 없는 경우 대비.
    """
    uname = username.lower().strip("/")
    og_url = (extract_meta(html, "og:url") or "").lower().rstrip("/")
    canonical = (extract_canonical(html) or "").lower().rstrip("/")
    for link in (og_url, canonical):
        if link.endswith("/" + uname):
            return True
    og_title = (extract_meta(html, "og:title") or "").lower()
    if f"(@{uname})" in og_title:
        return True
    return False


def looks_logged_out(html):
    """로그인 폼이 있으면 세션이 만료된 것.

    password 입력 필드나 /accounts/login으로 가는 form이 있으면
    로그인 벽에 막힌 상태다.
    """
    if 'name="password"' in html:
        return True
    if re.search(r'action\s*=\s*["\'][^"\']*/accounts/login', html, re.IGNORECASE):
        return True
    return False


def looks_private(html):
    """비공개 계정 신호.

    "is_private": true 를 먼저 본다 — HTML에 박힌 JSON 조각이라
    문자열 검색보다 신뢰도가 높다.
    그다음 텍스트 폴백.
    """
    if re.search(r'"is_private"\s*:\s*true', html):
        return True
    return any(t in html for t in PRIVATE_TEXTS)


def looks_not_found(html):
    return any(t in html for t in NOT_FOUND_TEXTS)


def classify_by_html(final_url, http_status, html, username):
    """HTML만으로 상태를 판별한다. GraphQL을 못 받았을 때의 폴백.

    ★ 판별 순서에 의미가 있다. 확실한 신호부터 본다.

      1) URL 리다이렉트 — 가장 확실. 로그인/챌린지 페이지로 튕겼다는 건
                          논쟁의 여지가 없다.
      2) HTTP 404      — 확실
      3) 프로필 메타   — 구조적 판별. 있으면 계정이 존재한다.
      4) 로그인 폼     — 세션 문제
      5) 응답 불완전   — HTML이 잘렸다
      6) 나머지        — NOT_FOUND

    ⚠️ 마지막 줄이 위험하다.
      "프로필 메타가 없다"를 곧바로 NOT_FOUND로 단정한다.
      인스타가 응답 구조를 바꾸면 멀쩡한 계정이 전부
      NOT_FOUND로 찍히고, import_l1이 'deleted'로 적재해
      영구 제외된다.

      완충 장치는 있다:
        - 앞의 "len(html) < 2000 or </html> 없음" 체크가
          잘린 응답을 먼저 걸러낸다
        - GraphQL이 오면 이 함수 자체를 안 탄다 (주력 경로)
      그래도 오탐 시 되돌리기 어려운 판정이라, 로그를 봐야 한다.
    """
    url = (final_url or "").lower()

    if "/accounts/login" in url or "/accounts/suspended" in url:
        return STATUS_LOGIN_REQUIRED, f"[HTML] 로그인 리다이렉트 ({final_url})"
    if "/challenge" in url or "/checkpoint" in url:
        return STATUS_CHALLENGE, f"[HTML] 챌린지/체크포인트 ({final_url})"

    if http_status == 404:
        return STATUS_NOT_FOUND, "[HTML] HTTP 404"

    if not html:
        return STATUS_ERROR, "[HTML] HTML 없음"

    if has_profile_meta(html, username):
        if looks_private(html):
            return STATUS_PRIVATE, "[HTML] 프로필 메타 + 비공개 신호"
        return STATUS_SUCCESS, "[HTML] 프로필 메타 확인"

    if looks_logged_out(html):
        return STATUS_LOGIN_REQUIRED, "[HTML] 로그인 폼/월"

    # 응답이 잘렸거나 비정상적으로 짧으면 판별 불가 → ERROR(재시도 대상)
    # 이 체크가 없으면 네트워크 문제로 잘린 응답이 NOT_FOUND가 된다.
    if len(html) < 2000 or "</html>" not in html.lower():
        return STATUS_ERROR, "[HTML] 응답 불완전"

    if looks_not_found(html):
        return STATUS_NOT_FOUND, "[HTML] 프로필 메타 없음(구조적) + 계정없음 텍스트"
    return STATUS_NOT_FOUND, "[HTML] 프로필 메타 없음(구조적)"
    # ↑ 텍스트가 있든 없든 NOT_FOUND지만 reason이 다르다.
    #   나중에 "텍스트 없이 구조만으로 판정한 건 몇 개인가"를
    #   집계해서 오탐 여부를 검토할 수 있다.


# =========================================================
# 결과 레코드
# =========================================================
def _blank_result(row):
    """모든 필드를 None으로 초기화한 결과 레코드.

    ★ 빈 레코드를 먼저 만들고 채워나가는 방식.

      성공 경로에서만 dict를 만들면, 실패했을 때 어떤 필드가
      있는지 없는지가 경로마다 달라진다.
      → jsonl의 스키마가 일정하지 않아 import_l1이 파싱하기 어렵다.

      여기서는 status가 뭐든 필드 구성이 항상 같다.
      (유튜브 ChannelL1 dataclass가 하는 역할을 dict로 한 것)

    ts를 여기서 찍는 이유: import_l1의 is_better()가
    같은 등급일 때 최신 것을 고르는 데 쓴다.
    """
    username = row["username"]
    return {
        "key": row.get("key"),          # seed_key. import_l1이 creator를 찾는 데 쓴다
        "username": username,
        "pk": None,
        "url": f"https://www.instagram.com/{username}/",
        "final_url": None,              # 리다이렉트 후 URL. 상태 판별의 핵심 근거
        "http_status": None,
        "status": STATUS_ERROR,         # 기본값이 ERROR. 성공해야 바뀐다.
        "reason": "",                   # 판별 근거. 오탐 추적용
        "html_path": None,
        "graphql_json_path": None,
        "user_id": None,                # 인스타 내부 ID. username보다 영구적
        "nickname": None,
        "followers": None,
        "following": None,
        "posts": None,
        "biography": None,
        "external_url": None,
        "category_name": None,
        "account_type": None,
        "is_private": None,
        "is_verified": None,
        "profile_pic_url": None,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }


def _join_reason(*parts):
    """빈 문자열을 걸러내고 ' | '로 잇는다."""
    return " | ".join(p for p in parts if p)


# =========================================================
# GraphQL 캡처 헬퍼
# =========================================================
def _is_profile_graphql(response):
    """PolarisProfilePageContentQuery 프로필 GraphQL 응답인지 (요청 헤더 기준).

    ★ URL만으로는 구분할 수 없다.
      /api/graphql 하나로 수십 종류의 쿼리가 오간다.
      요청 헤더의 x-fb-friendly-name으로 골라내야 한다.

      response가 아니라 response.request의 헤더를 본다.
      (응답에는 쿼리 이름이 없다)

    method != POST 체크: GraphQL은 항상 POST다.
    같은 경로로 오는 GET 요청을 미리 걸러 비용을 아낀다.

    try/except로 감싼 이유: 리스너는 크롤 중 수십 번 호출되는데,
    여기서 예외가 나면 페이지 로드 자체가 영향을 받을 수 있다.
    """
    try:
        req = response.request
        if req.method != "POST":
            return False
        if GRAPHQL_URL_PART not in response.url:
            return False
        return req.headers.get("x-fb-friendly-name") == PROFILE_QUERY_NAME
    except Exception:
        return False


def _read_graphql_json(response):
    """GraphQL body -> dict. 실패해도 절대 예외를 던지지 않고 None.

    2단 폴백: response.json()이 실패하면 text()로 받아 직접 파싱.
    응답이 이미 소비됐거나 Content-Type이 이상한 경우 대비.

    "절대 예외를 던지지 않는다"가 중요하다.
    GraphQL 파싱 실패로 크롤 전체가 죽으면 안 된다.
    None이면 HTML 판별로 넘어가면 그만이다.
    """
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


def _looks_terminal(http_status, final_url):
    """더 기다릴 필요 없이 즉시 결론나는 상태 -> grace 를 통째로 건너뛴다.

    ★ 시간 절약의 핵심.

      없는 계정(404)이나 로그인 페이지로 튕긴 경우, GraphQL은
      영영 오지 않는다. 그런데도 4초를 기다리면 순수 낭비다.

      1,881개 중 700개가 NOT_FOUND라면 700 × 4초 = 47분.
      계정당 8~15초 딜레이와 별개로 추가되는 시간이다.

      → 결론이 이미 난 경우 유예를 건너뛴다.
    """
    if http_status in (404, 410):
        return True
    u = (final_url or "").lower()
    if "/accounts/login" in u or "/accounts/suspended" in u:
        return True
    if "/challenge" in u or "/checkpoint" in u:
        return True
    return False


# =========================================================
# 단일 계정 크롤링
# =========================================================
def crawl_one(page, row):
    """계정 하나 크롤. 절대 예외를 밖으로 던지지 않는다.

    모든 실패가 status로 표현되어 jsonl에 기록된다.
    → 메인 루프가 try/except를 갖지 않아도 되고,
      실패 원인이 항상 파일에 남는다.
    """
    username = row["username"]
    result = _blank_result(row)
    url = result["url"]
    notes = []

    # --- GraphQL 캡처 리스너: goto '이전'에 등록해야 goto 중 도착분을 놓치지 않는다 ---
    #
    # ★ 순서가 중요하다. goto 이후에 리스너를 걸면 이미 지나간 응답을 못 잡는다.
    #   인스타는 페이지 로드 중에 GraphQL을 부르므로 대부분 goto 안에서 온다.
    captured = {"response": None}

    def _on_response(response):
        if captured["response"] is not None:
            return      # 첫 개만 쓴다. 같은 쿼리가 여러 번 올 수 있다.
        if _is_profile_graphql(response):
            captured["response"] = response

    page.on("response", _on_response)

    goto_ok = False
    goto_error = None

    try:
        # ---- (1) 네비게이션: goto '자체' 타임아웃만 담당 (GraphQL 대기와 완전 분리) ----
        #
        # ★ 이 분리가 초기 버전 대비 가장 큰 개선이다.
        #   예전엔 GraphQL을 goto 타임아웃 안에서 기다렸는데,
        #   안 오는 계정에서 30초를 통째로 날렸다.
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",   # load까지 안 기다린다. 이미지 등 불필요.
                timeout=L1_GOTO_TIMEOUT_MS,
            )
            goto_ok = True
            result["http_status"] = response.status if response else None
        except PWTimeout:
            goto_error = "timeout"
            notes.append("goto 타임아웃")
        except Exception as e:
            goto_error = "network"
            notes.append(f"goto 예외: {e!r}")
        # ↑ goto가 실패해도 계속 진행한다. 페이지가 부분적으로
        #   로드됐을 수 있고, HTML만 건져도 판별이 가능하다.

        try:
            result["final_url"] = page.url
        except Exception:
            pass

        # ---- (2) GraphQL 유예 대기: '짧게만'. 안 오면 그대로 HTML 판별로 넘어간다 ----
        #      없는 계정(404 등)은 _looks_terminal 로 grace 자체를 건너뛴다.
        if (
            goto_ok
            and captured["response"] is None
            and not _looks_terminal(result["http_status"], result["final_url"])
        ):
            deadline = time.monotonic() + (L1_GRAPHQL_GRACE_MS / 1000.0)
            # ↑ monotonic()을 쓴다. 시스템 시각이 바뀌어도(NTP 동기화)
            #   경과 시간 측정이 어긋나지 않는다.

            # wait_for_timeout 이 이벤트 루프를 pump 하므로 그 사이 리스너가 응답을 잡는다.
            #
            # ★ sync API에서 이벤트를 받으려면 이런 폴링이 필요하다.
            #   time.sleep()을 쓰면 이벤트 루프가 안 돌아 리스너가 호출되지 않는다.
            #   100ms씩 쪼개는 이유: 응답이 오면 빨리 빠져나가려고.
            while captured["response"] is None and time.monotonic() < deadline:
                page.wait_for_timeout(100)

        # HTML 로 판별할 예정이면(=GraphQL 못 잡음) SPA 렌더 잠깐 안정화
        # 인스타는 React라 DOM이 늦게 채워진다. 바로 content()를 뽑으면
        # 메타 태그가 아직 없을 수 있다.
        if goto_ok and captured["response"] is None:
            try:
                page.wait_for_timeout(L1_RENDER_WAIT_MS)
            except Exception:
                pass

    finally:
        # 리스너 반드시 제거. 같은 page 를 루프에서 재사용하므로 안 지우면
        # 계정마다 리스너가 누적되어 후반부로 갈수록 느려지고 오탐이 생긴다.
        #
        # ★ 실제로 겪은 문제로 보인다. 1,881개를 돌면 리스너가 1,881개
        #   쌓이고, 응답 하나마다 그게 전부 호출된다. O(n²)이 된다.
        page.remove_listener("response", _on_response)

    # ---- (3) HTML 항상 저장 (성패 무관) ----
    html = None
    try:
        html = page.content()
        try:
            result["final_url"] = page.url  # 리다이렉트 후 최종 URL 갱신
            # ↑ (1)에서도 찍었지만 그 사이 리다이렉트가 더 일어날 수 있다.
        except Exception:
            pass
        result["html_path"] = str(save_html(username, html))
    except Exception as e:
        notes.append(f"html 확보 실패: {e!r}")

    # ---- (4) GraphQL 저장 + 파싱 (왔을 때만) ----
    graphql = _read_graphql_json(captured["response"])
    profile = None
    if graphql is not None:
        try:
            result["graphql_json_path"] = str(save_graphql_json(username, graphql))
        except Exception as e:
            notes.append(f"GraphQL 저장 실패: {e!r}")
        try:
            profile = parse_profile(graphql)   # user=null 이면 None 반환
        except Exception as e:
            notes.append(f"GraphQL 파싱 예외: {e!r}")
        if profile:
            result.update(profile)   # 파싱 결과를 결과 레코드에 병합

    # ---- (5) 상태 판별: GraphQL 우선, 없으면 HTML ----
    if profile:
        # GraphQL로 프로필을 받았다 = 계정이 확실히 존재한다.
        # is_private만 확인하면 된다.
        if result.get("is_private"):
            status, reason = STATUS_PRIVATE, "GraphQL + private"
        else:
            status, reason = STATUS_SUCCESS, "GraphQL + profile"
    else:
        # GraphQL 이 없는 것은 '오류가 아니라 정상 분기'.
        # 단, HTML 조차 못 건졌고 goto 도 실패했을 때만 진짜 오류로 남긴다.
        #
        # ★ 이 주석이 설계 판단이다.
        #   초기 버전은 GraphQL이 없으면 실패로 처리했는데,
        #   인스타는 캐시나 SSR로 GraphQL 없이 페이지를 주는 경우가 있다.
        #   HTML만으로도 판별이 가능하므로 '정상 분기'로 취급한다.
        if not html:
            if goto_error == "timeout":
                status, reason = STATUS_TIMEOUT, "goto 타임아웃 + HTML 없음"
            elif goto_error == "network":
                status, reason = STATUS_NETWORK_ERROR, "goto 네트워크 예외 + HTML 없음"
            else:
                status, reason = STATUS_ERROR, "HTML 없음"
        else:
            status, reason = classify_by_html(
                result["final_url"],
                result["http_status"],
                html,
                username,
            )
            if goto_error:  # goto 는 실패했지만 HTML 은 건진 경우 흔적만 남김
                notes.append(f"goto_error={goto_error} (HTML 확보됨, HTML 판별)")
                # ↑ status를 바꾸지 않고 notes에만 남긴다.
                #   HTML로 판별이 됐으면 그게 더 정확한 정보다.

    result["status"] = status
    result["reason"] = _join_reason(reason, *notes)
    return result


# =========================================================
# 요약
# =========================================================
def summarize(results, log):
    """상태별 집계 + 비정상 계정 이름 나열.

    ★ SUCCESS를 제외한 상태의 계정 이름을 찍는다.
      "TIMEOUT 12개"만 보면 뭘 해야 할지 모르지만,
      계정 이름이 있으면 몇 개를 직접 열어보고
      "정말 없는 계정인지" 확인할 수 있다.

      30개로 자르는 이유: 700개가 NOT_FOUND면 로그가 넘친다.
    """
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    log.info("=" * 44)
    log.info("결과 요약 (총 %d개)", len(results))
    for status in ALL_STATUSES:
        if counts.get(status):
            log.info("  %-16s %d", status, counts[status])

    for status in ALL_STATUSES:
        if status == STATUS_SUCCESS:
            continue
        names = [r["username"] for r in results if r["status"] == status]
        if names:
            shown = ", ".join(names[:30]) + (" ..." if len(names) > 30 else "")
            log.info("  - %s: %s", status, shown)
    log.info("=" * 44)

# =========================================================
# 메인
# =========================================================
def main(limit=BATCH_LIMIT, headless=HEADLESS, resume=True):
    log = setup_logging()

    # resume=False면 기존 결과를 통째로 지운다.
    # 전체 재수집이 필요할 때 쓴다. (틱톡 main.py의 --all과 같은 역할)
    if not resume and RESULTS_FILE.exists():
        RESULTS_FILE.unlink()
        log.info("resume=False: 기존 결과 파일 삭제")

    # 세션이 없으면 시작조차 안 한다.
    # 없는 채로 진행하면 전부 LOGIN_REQUIRED가 되고 8시간을 날린다.
    if not SESSION_FILE.exists():
        log.error("세션 파일이 없습니다: %s (login.py를 먼저 실행하세요)", SESSION_FILE)
        return

    rows = load_rows(limit)
    log.info("전체 대상 : %d개", len(rows))

    completed = load_completed()
    if completed:
        log.info("확정 상태(제외) : %d개", len(completed))
    rows = [row for row in rows if row["username"] not in completed]
    log.info("신규 수집 대상 : %d개", len(rows))
    if not rows:
        log.info("처리할 계정 없음 (다 끝남).")
        return

    log.info("딜레이 %.1f~%.1f초 | 차단 연속 %d회 중단",
             L1_DELAY_MIN, L1_DELAY_MAX, STOP_ON_429)

    # 차단으로 간주할 상태 (세션 보호용)
    #
    # ★ CHALLENGE가 여기 있는 게 중요하다.
    #   인스타는 의심스러운 활동을 감지하면 본인확인을 요구하는데,
    #   이걸 무시하고 계속 요청하면 계정이 잠긴다.
    #   IP는 바꿀 수 있지만 계정이 죽으면 복구가 어렵다.
    #   → 세 번 연속이면 즉시 멈춘다.
    BLOCK_STATUSES = (STATUS_CHALLENGE, STATUS_LOGIN_REQUIRED, STATUS_RATE_LIMITED)

    results = []
    block_streak = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            storage_state=str(SESSION_FILE),   # 로그인 세션 주입
            locale=LOCALE,
            user_agent=USER_AGENT,
        )
        # ⚠️ config.context_kwargs()를 안 쓰고 직접 조립한다.
        #    login.py는 context_kwargs()를 쓰므로 viewport와 timezone_id가
        #    빠져 있다. 지문이 어긋나 재인증을 유발할 수 있다.
        #    (login.py와 옵션을 맞추라는 config 주석과 어긋난 부분)

        page = context.new_page()
        # ★ page 하나를 1,881개 내내 재사용한다.
        #   계정마다 새로 만들면 그 자체가 부자연스럽고 느리다.
        #   대신 리스너를 매번 제거해야 한다 (crawl_one의 finally).

        for i, row in enumerate(rows, 1):
            r = crawl_one(page, row)   # 예외를 안 던지므로 try 불필요
            results.append(r)
            append_result(r)           # 즉시 파일에 기록. 죽어도 여기까진 남는다.

            log.info(
                "[%d/%d] @%s -> %s (http=%s) :: %s",
                i,
                len(rows),
                r["username"],
                r["status"],
                r["http_status"],
                r["reason"],     # 판별 근거를 매 줄에 남긴다
            )

            # ── 차단 신호 연속 감지 -> 세션 보호를 위해 중단 ──
            if r["status"] in BLOCK_STATUSES:
                block_streak += 1
                log.warning("차단 신호 연속 %d회 (%s @%s)",
                            block_streak, r["status"], r["username"])
                if block_streak >= STOP_ON_429:
                    log.error(
                        "차단 %d회 연속 -> 세션 보호를 위해 중단 "
                        "(딜레이를 늘리거나 세션을 재발급하세요)",
                        STOP_ON_429,
                    )
                    break
            else:
                block_streak = 0  # 정상 결과가 나오면 연속 카운트 리셋
                # ★ '연속'이 기준이다. 누적이면 하루 종일 돌다가
                #   드문드문 3번 걸려도 중단된다.
                #   연속 3회여야 "지금 막히고 있다"는 신호다.

            # 계정 간 8~15초 랜덤 대기.
            # 고정 간격이면 기계적 패턴이라 탐지된다.
            time.sleep(random.uniform(L1_DELAY_MIN, L1_DELAY_MAX))

        context.close()   # storage_state를 갱신하지는 않는다(읽기만)
        browser.close()

    summarize(results, log)


if __name__ == "__main__":
    main(limit=BATCH_LIMIT, headless=HEADLESS, resume=True)