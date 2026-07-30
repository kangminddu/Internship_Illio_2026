"""
youtube/crawler/lib/youtube_parser.py — 유튜브 공통 파싱 라이브러리

왜 이 파일이 존재하는가
------
크롤러가 3개(L1, L2a, L2b) 있는데 하는 일은 서로 다르다.
그런데 'HTML을 받아 데이터를 꺼내는 방식'은 셋 다 똑같다:

    HTTP 요청 → HTML → ytInitialData JSON 추출 → 필요한 값 찾기

이 공통부를 한 곳에 모았다. 분리하지 않았다면 유튜브가 HTML 구조를
바꿀 때마다 크롤러 3개를 전부 고쳐야 한다. (유튜브는 실제로 자주 바꾼다)

경계
------
이 파일은 DB를 모른다. pymysql import가 없다.
HTTP 요청과 파싱만 하고 결과를 파이썬 자료구조로 반환한다.
저장은 크롤러의 몫이다.

유튜브 크롤링의 핵심 원리
------
유튜브 페이지 HTML 안에는 이런 게 들어 있다:

    <script>var ytInitialData = {"contents":{...}};</script>

화면에 표시될 모든 데이터가 JSON으로 통째로 박혀 있다.
브라우저는 이걸 읽어 화면을 그린다.

따라서 BeautifulSoup으로 DOM을 파싱할 필요가 없다.
정규식으로 이 JSON만 뽑아 json.loads하면 끝이다.
이게 이 파일 전체를 관통하는 아이디어다.

포함 기능
- Channel(L1) 수집  (+ 공개 설명란 이메일 추출)
- Video(L2) 파싱
- Watch Page 파싱
- ytInitialData 추출
- 숫자/날짜 파싱
- URL 정규화

crawler_l1.py, crawler_l2.py, crawler_l2a.py, crawler_l1_parallel.py 에서 공통으로 사용한다.

[수정 이력]
- fetch_channel_l1 내부 429 재시도 제거: 429 처리는 crawler의 전역 백오프가 전담.
  (내부 2초/4초 3연발 재시도가 전역 rate limiter를 우회해 차단을 가속시키던 문제)
- Session에 SOCS consent 쿠키 추가: 쿠키 없는 콜드 요청의 봇 판정 완화.
- parse_joined_date에 영어 날짜 형식 추가: /about?hl=en 페이지에서 개설일이
  항상 None으로 저장되던 버그 수정.
"""
from zoneinfo import ZoneInfo
import re # 정규 표현식
import json
import threading
import requests
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional # 값이 존재하지 않을 수 있는 변수 다룸


# ─────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────
# 기본 User-Agent("python-requests/2.31.0")를 그대로 쓰면 즉시 차단된다.
# 실제 Chrome 헤더를 흉내낸다.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# L1만 영어(hl=en) 페이지를 쓴다. 밴/삭제 감지 문구가 영어 기준으로
# 더 안정적이기 때문. (L2a/L2b는 hl=ko — 상대시간 파싱이 한국어 전제)
#
# ⚠️ 이 선택 때문에 버그가 있었다: parse_joined_date가 한국어 형식만
#    처리해서, 영어 페이지의 "Joined Mar 21, 2008"을 못 읽고
#    channel_opened_at이 전부 None으로 저장되고 있었다.
#    언어 설정과 파서는 짝을 이뤄야 하고, 하나만 바꾸면 조용히 깨진다.
CHANNEL_URL_SUFFIX = "/about?hl=en&gl=US"
REQUEST_TIMEOUT = 20

# 스레드마다 독립 Session (연결 재사용 + 스레드 안전)
_thread_local = threading.local()

def get_session():
    """스레드별 requests.Session 반환.

    Session을 쓰는 이유: TCP 연결을 재사용한다.
    매번 requests.get()을 하면 요청마다 TCP 핸드셰이크 + TLS 협상을
    새로 한다. 수천 번 요청하는 크롤러에서는 차이가 크다.

    전역 Session 하나를 공유하지 않는 이유: requests.Session은
    스레드 안전을 보장하지 않는다. 여러 스레드가 동시에 쓰면
    쿠키 저장소나 커넥션 풀에서 경쟁 상태가 생길 수 있다.

    threading.local()은 '스레드마다 독립적인 저장소'다.
    같은 get_session()을 불러도 스레드 A와 B는 다른 Session을 받는다.
    → 워커 4개면 Session 4개. 안전하면서 연결 재사용 이점은 유지.
    """
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update(HEADERS)
        # consent 페이지 우회 + 쿠키 없는 콜드 요청의 봇 판정 완화
        #
        # 쿠키가 하나도 없으면 EU 쿠키 동의 페이지로 리다이렉트될 수 있고,
        # 그러면 원하는 데이터 대신 동의 페이지 HTML을 받는다.
        # SOCS=CAI는 "동의 처리됨" 상태를 나타낸다.
        # 부수 효과로, 쿠키가 전혀 없는 요청은 봇처럼 보인다는 문제도 완화된다.
        s.cookies.set("SOCS", "CAI", domain=".youtube.com")
        _thread_local.session = s
    return _thread_local.session


# ─────────────────────────────────────────────────────────
# 이메일 추출 (공개 설명란에 크리에이터가 직접 적어둔 것만)
#
# 유튜브의 '비즈니스 문의' 이메일은 CAPTCHA 뒤에 있어 접근하지 않는다.
# 크리에이터가 설명란에 직접 노출한 것만 가져온다.
# ─────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')

def extract_emails(text):
    if not text:
        return []
    # set으로 중복 제거 + 소문자 정규화 (같은 주소가 대소문자만 달리 적힌 경우)
    return list({e.lower() for e in EMAIL_RE.findall(text)})


# ─────────────────────────────────────────────────────────
# 결과 구조
# ─────────────────────────────────────────────────────────
@dataclass
class ChannelL1:
    """L1 수집 결과.

    dict가 아니라 dataclass인 이유:
    이 객체는 '성공한 데이터'만 담지 않는다. 실패했을 때
    왜 실패했는지도 구조화해서 크롤러에 전달해야 한다.

    crawler_l1_parallel.classify_existence()가 아래 세 필드를 보고
    채널 상태(deleted/suspended/unknown)를 판정한다:
        page_signal  — 페이지 본문에서 감지한 밴/삭제 신호
        http_status  — 404 등
        error_type   — rate_limited / no_yt_data / retriable_timeout ...

    dict로 하면 키 오타가 나도 런타임까지 모르지만,
    dataclass는 필드가 고정돼 있어 즉시 드러난다.
    """
    external_channel_id: Optional[str]
    channel_name: Optional[str]
    channel_opened_at: Optional[str]
    subscriber_count: Optional[int]
    total_view_count: Optional[int]
    total_video_count: Optional[int]
    source_url: str
    captured_at: str
    ok: bool
    error: Optional[str] = None
    http_status: Optional[int] = None
    had_yt_data: bool = False
    page_signal: Optional[str] = None
    error_type: Optional[str] = None
    description: Optional[str] = None
    emails: Optional[list] = None


# ─────────────────────────────────────────────────────────
# URL 정규화
# ─────────────────────────────────────────────────────────
def normalize_channel_url(url: str) -> str:
    """다양한 형태의 채널 URL을 표준형으로.

    순서에 의미가 있다:
      1) UC ID를 최우선 추출 — 문자열 어디에 있든. 가장 확실한 식별자다.
      2) 쿼리스트링 제거
      3) 뒤 슬래시 먼저 제거 → 그다음 탭 꼬리 제거
         (순서가 반대면 "/videos/" 케이스에서 슬래시가 남아 매칭 실패)
    """
    url = url.strip()

    # 문자열 어디에 있든 UC 채널ID를 최우선 추출
    m = re.search(r"(UC[\w-]{22})", url) # 영문자, 숫자, 밑줄 하이픈이 합쳐진 문자가 정확히 22개 반복 여부 확인
    if m:
        return f"https://www.youtube.com/channel/{m.group(1)}"

    if not url.startswith("http"):
        url = "https://" + url

    # 쿼리스트링 제거
    url = re.sub(r"\?.*$", "", url)

    # 뒤 슬래시 먼저 제거 → 탭 꼬리 제거 순서 보장 (/videos/ 케이스)
    url = url.rstrip("/")
    for suffix in ("/featured", "/videos", "/about", "/discussion",
                   "/community", "/playlists", "/streams", "/shorts"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]

    return url.rstrip("/")


# ─────────────────────────────────────────────────────────
# 숫자 파싱 (한국어 억/만/천 + 영어 K/M/B)
# ─────────────────────────────────────────────────────────
def parse_count(raw) -> Optional[int]:
    """"구독자 12.3만명" → 123000

    ⚠️ 정밀도 손실이 있다.
    유튜브는 정확한 숫자를 주지 않고 반올림해서 표시한다.
    실제 구독자가 123,456명이어도 "12.3만"으로 표시되고,
    파싱하면 123,000이 된다. 오차 456명.
    구독자가 많을수록 오차도 커진다.

    이건 버그가 아니라 데이터 소스의 한계다.
    공식 API를 썼다면 정확한 값이 나오지만, API를 안 쓰기로 한 이상
    감수해야 한다. → VPF(구독자 대비 조회율) 지표의 소수점 이하는
    신뢰하면 안 된다.

    단위 텍스트를 먼저 제거하는 이유: hl=en으로 요청해도
    실제로는 한국어가 섞여 오는 경우가 있어 양쪽을 다 처리한다.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip()
    s = re.sub(r"(구독자|조회수|동영상|회|개|명|subscribers?|views?|videos?)", "", s)
    s = s.strip().lower().replace(",", "")

    m = re.search(r"([\d.]+)\s*억", s)
    if m:
        return int(float(m.group(1)) * 100_000_000)
    m = re.search(r"([\d.]+)\s*만", s)
    if m:
        return int(float(m.group(1)) * 10_000)
    m = re.search(r"([\d.]+)\s*천", s)
    if m:
        return int(float(m.group(1)) * 1_000)
    m = re.match(r"^([\d.]+)\s*([kmb])$", s)
    if m:
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[m.group(2)]
        return int(float(m.group(1)) * mult)

    # 단위가 없으면 숫자만 남긴다 ("1,234회" → 1234)
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None

# 동영상 길이
def parse_duration(text):
    """'16:31' → 991, '1:02:03' → 3723"""
    if not text:
        return None
    parts = text.strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 2:
        return parts[0]*60 + parts[1]
    if len(parts) == 3:
        return parts[0]*3600 + parts[1]*60 + parts[2]
    if len(parts) == 1:
        return parts[0]
    return None


# ─────────────────────────────────────────────────────────
# ytInitialData 추출 + 파싱
# ─────────────────────────────────────────────────────────
def extract_yt_initial_data(html: str) -> Optional[dict]:
    """HTML에 박힌 ytInitialData JSON을 뽑아낸다.

    패턴을 3개 두는 이유: 유튜브가 이 스크립트를 심는 방식이
    페이지 종류나 배포 버전에 따라 조금씩 다르다.
    하나가 실패하면 다음 패턴을 시도한다.

    json.loads가 실패하면 다음 패턴으로 넘어가는 것도 의도된 것.
    정규식이 잘못 잘라서 JSON이 깨진 경우일 수 있다.
    """
    for pat in (
        r"var ytInitialData\s*=\s*({.+?})\s*;</script>",
        r'ytInitialData"\]\s*=\s*({.+?})\s*;',
        r"ytInitialData\s*=\s*({.+?})\s*;",
    ):
        m = re.search(pat, html, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
    return None


def find_first(obj, key):
    """중첩 dict/list에서 key를 DFS로 처음 만나는 값 반환.

    왜 경로를 하드코딩하지 않는가:

    ytInitialData의 실제 구조는 이렇게 생겼다.
        contents
         └ twoColumnBrowseResultsRenderer
             └ tabs[0] → tabRenderer → content
                 └ sectionListRenderer → contents[0] → ...
                     └ aboutChannelViewModel

    경로대로 쓰면 data["contents"]["twoColumnBrowseResultsRenderer"]... 인데,
    유튜브가 UI를 조금만 손봐도 중간에 래퍼가 하나 끼거나 탭 순서가 바뀐다.
    그러면 KeyError / IndexError로 터진다.

    find_first는 경로를 몰라도 찾는다.
    "aboutChannelViewModel이라는 키가 어디 있든 가져와" 하는 방식.
    → 구조 변경에 강하다.

    대가는 성능이다. JSON 트리 전체를 순회하니 O(n)이고,
    여러 번 호출하면 매번 처음부터 훑는다.
    하지만 크롤러의 병목은 네트워크(요청당 1.2초)라 파싱 몇 ms는 무의미하다.
    → 안정성을 성능보다 우선한 선택.
    """
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_first(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_first(v, key)
            if r is not None:
                return r
    return None


_MONTHS_EN = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

def parse_joined_date(text):
    """
    '가입일: 2008. 3. 21.'  → '2008-03-21'  (한국어)
    'Joined Mar 21, 2008'   → '2008-03-21'  (영어; /about?hl=en 페이지)

    ⚠️ 영어 형식은 나중에 추가했다.
    CHANNEL_URL_SUFFIX가 hl=en인데 한국어 패턴만 있어서,
    channel_opened_at이 전부 None으로 저장되고 있었다.
    에러가 안 나고 조용히 NULL이 쌓이던 버그.
    """
    if not text:
        return None
    if isinstance(text, dict):
        text = text.get("content", "")
    text = str(text)

    # 한국어: 2008. 3. 21.
    m = re.search(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # 영어: Mar 21, 2008 / March 21, 2008
    m = re.search(r"([A-Za-z]{3})[A-Za-z]*\s+(\d{1,2}),\s*(\d{4})", text)
    if m and m.group(1).lower() in _MONTHS_EN:
        return f"{m.group(3)}-{_MONTHS_EN[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"

    return None


def parse_l1(data: dict) -> dict:
    """ytInitialData → 채널 정보 dict.

    유튜브는 같은 정보를 두 군데에 담는다:
      channelMetadataRenderer  — 메타 태그용. 채널명, 설명 등 기본.
      aboutChannelViewModel    — 정보 탭 내용. 구독자 수, 개설일, 외부 링크.

    about이 없는 경우가 있어(구버전 레이아웃, 일부 채널) meta를 폴백으로 쓴다.
    """
    result = {
        "external_channel_id": None,
        "channel_name": None,
        "channel_opened_at": None,
        "subscriber_count": None,
        "total_view_count": None,
        "total_video_count": None,
        "description": None,
        "emails": [],
    }

    meta = find_first(data, "channelMetadataRenderer") or {}
    about = find_first(data, "aboutChannelViewModel") or {}

    result["channel_name"] = meta.get("title")

    # ── description 본문 ──
    # about 쪽이 더 완전하지만 형태가 dict일 수도 str일 수도 있다.
    # 없으면 meta의 것을 쓴다.
    desc = None
    if about:
        d = about.get("description")
        if isinstance(d, dict):
            desc = d.get("content")
        elif isinstance(d, str):
            desc = d
    if not desc:
        desc = meta.get("description")

    # ── links 섹션 추출 (치지직 등 외부 링크) ──
    #
    # 설명란 본문뿐 아니라 '링크' 섹션도 긁는 이유:
    # 유튜브가 링크를 별도 UI로 분리해서, 본문만 보면 치지직 주소를 놓친다.
    # email 단계(chzzk_email.py)가 여기서 치지직 링크를 찾아
    # 치지직 API로 연락처를 보강한다.
    link_texts = []
    if about:
        for link in about.get("links", []) or []:
            lvm = link.get("channelExternalLinkViewModel") or {}
            l = lvm.get("link")
            if isinstance(l, dict):
                url = l.get("content")
                if url:
                    link_texts.append(url)
            t = lvm.get("title")
            if isinstance(t, dict):
                tt = t.get("content")
                if tt:
                    link_texts.append(tt)

    # 본문 + 링크를 합쳐서 저장. 이메일도 이 합본에서 찾는다.
    combined = "\n".join(filter(None, [desc] + link_texts))
    result["description"] = combined or None
    result["emails"] = extract_emails(combined)

    if about:
        result["subscriber_count"] = parse_count(about.get("subscriberCountText"))
        result["total_view_count"] = parse_count(about.get("viewCountText"))
        result["total_video_count"] = parse_count(about.get("videoCountText"))
        result["external_channel_id"] = about.get("channelId") or meta.get("externalId")
        result["channel_opened_at"] = parse_joined_date(about.get("joinedDateText"))
    else:
        # about 블록이 없으면 UC ID라도 건진다.
        # (호출자가 subscriber_count/channel_name이 둘 다 None인 것을 보고
        #  'about_missing'으로 판정한다)
        result["external_channel_id"] = meta.get("externalId")

    return result


# ─────────────────────────────────────────────────────────
# 채널 1개 수집
# ─────────────────────────────────────────────────────────
def fetch_channel_l1(channel_url: str, debug_dump: bool = False, max_retries: int = 2) -> ChannelL1:
    """
    채널 1개 L1 수집.

    재시도 정책:
      - timeout / ConnectionError 만 내부 재시도 (일시적 네트워크 장애, 백오프 2→4초)
      - 429 는 즉시 반환 → 호출자(crawler_l1_parallel)의 전역 백오프가 전담.
        (내부에서 짧은 간격으로 재시도하면 전역 rate limiter를 우회해 차단을 가속시킴)
      - 404, 200(성공/삭제) — 확정된 결과라 재시도 안 함.

    ※ 이 정책이 이 파일에서 가장 중요한 설계 판단이다.
      이전 버전은 429도 내부에서 2초/4초 간격으로 3번 재시도했는데,
      그러면 이런 일이 벌어진다:

          크롤러의 rate limiter : 1.2초에 한 번만 통과
          파서 내부             : 429 → 2초 후 재시도 → 또 429 → 4초 후 재시도
          결과                  : limiter를 우회해 3번 더 때림

      차단당했는데 오히려 더 때리는 꼴이다.
      → 문제의 '범위'에 따라 책임자를 나눴다:
          429 (전역, 모든 워커에 영향)      → 크롤러의 전역 백오프
          timeout (국소, 이 요청만의 문제)  → 파서 내부 재시도
          404/200 (확정)                    → 재시도 없음
    """
    base_url = normalize_channel_url(channel_url)
    url = base_url + CHANNEL_URL_SUFFIX
    now = datetime.now(timezone.utc).isoformat()   # ※ 이 값은 DB에 저장되지 않음(미사용 필드)

    def fail(error, http_status=None, had_yt_data=False, page_signal=None, error_type=None):
        """실패 결과 생성 헬퍼. 실패해도 '왜'를 구조화해서 반환한다."""
        return ChannelL1(
            external_channel_id=None, channel_name=None, channel_opened_at=None,
            subscriber_count=None, total_view_count=None, total_video_count=None,
            source_url=url, captured_at=now, ok=False, error=error,
            http_status=http_status, had_yt_data=had_yt_data,
            page_signal=page_signal, error_type=error_type,
        )

    for attempt in range(max_retries + 1):        # 0 = 최초, 1~2 = 네트워크 장애 재시도
        try:
            resp = get_session().get(url, timeout=REQUEST_TIMEOUT)
            code = resp.status_code

            # 429 → 즉시 반환. 백오프/재시도는 crawler의 전역 limiter가 처리.
            if code == 429:
                ra = resp.headers.get("Retry-After")
                # 레이트리밋 관련 헤더를 error에 담아둔다.
                # 나중에 crawl_logs에서 "유튜브가 뭐라고 했는지" 확인 가능.
                rl_headers = {k: v for k, v in resp.headers.items()
                              if "ratelimit" in k.lower() or k.lower() == "retry-after"}
                return fail(f"HTTP 429 headers={rl_headers}", http_status=429, error_type="rate_limited")
            # 그 외 non-200 → 확정 결과, 재시도 안 함
            if code != 200:
                return fail(f"HTTP {code}", http_status=code, error_type="http_error")

            # ── 200: 정상 처리 ──
            # 주의: 200이어도 '삭제된 채널' 안내 페이지일 수 있다.
            # 유튜브는 없는 채널에 404가 아니라 200 + 안내문을 주는 경우가 많다.
            body = resp.text
            page_signal = None

            # 1) 밴/정지/해지 (긴 문구로 오탐 방지, hl=en 페이지이므로 영어 시그널이 주로 작동)
            #
            # 짧은 단어("terminated" 하나)로 검사하면 정상 채널의 설명문이나
            # JS 번들에 우연히 포함돼 오탐이 난다. 긴 문구를 쓰는 이유.
            ban_signals = [
                "약관을 위반하여 계정이 해지",
                "커뮤니티 가이드를 위반했기 때문에",
                "저작권 침해에 대한 제3자 신고",
                "violated YouTube's Community Guidelines",
                "terminated",
            ]
            for sig in ban_signals:
                if sig in body:
                    page_signal = "channel_banned"
                    break

            # 2) 일반 삭제/사용불가 (밴 아닐 때만)
            #
            # 순서가 중요하다. 밴 페이지에도 "사용할 수 없음" 문구가 있을 수 있어
            # 순서를 뒤집으면 밴이 단순 삭제로 잘못 분류된다.
            # (밴과 삭제는 CRM에서 의미가 다르다 — 밴은 복구 가능성이 없다)
            if page_signal is None:
                deleted_signals = [
                    "존재하지 않",
                    "채널은 사용할 수 없",
                    "does not exist",
                    "isn't available",
                    "This channel does not",
                ]
                for sig in deleted_signals:
                    if sig in body:
                        page_signal = "channel_not_exist"
                        break

            data = extract_yt_initial_data(body)
            if data is None:
                # 이 error_type이 대량으로 찍히면 = 유튜브가 구조를 바꿨다는 신호.
                # crawl_logs를 집계하면 조기에 감지할 수 있다.
                return fail("ytInitialData not found", http_status=code,
                            had_yt_data=False, page_signal=page_signal,
                            error_type="no_yt_data")

            if debug_dump:
                # 구조가 바뀌었을 때 실제 JSON을 눈으로 보기 위한 디버그 옵션.
                # 파일 맨 아래 단독 실행 블록에서 사용.
                with open("ytinitialdata_dump.json", "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print("  ↳ ytinitialdata_dump.json 저장됨")

            fields = parse_l1(data)

            # JSON은 받았는데 알맹이가 없는 경우.
            # 삭제 채널의 안내 페이지에도 ytInitialData는 들어있어서,
            # 이 검사가 없으면 '빈 성공'으로 저장된다.
            if fields.get("subscriber_count") is None and fields.get("channel_name") is None:
                return fail("about block missing", http_status=code, had_yt_data=True,
                            page_signal=page_signal, error_type="about_missing")

            return ChannelL1(
                **fields, source_url=url, captured_at=now, ok=True,
                http_status=code, had_yt_data=True, page_signal=page_signal,
            )

        except (requests.Timeout, requests.ConnectionError) as e:
            # 일시적 네트워크 장애 → 백오프 후 재시도
            # 이건 '이 요청만의 문제'라 파서가 직접 처리해도 안전하다.
            if attempt < max_retries:
                time.sleep(2 * (attempt + 1))
                continue
            # retriable_* 로 분류 → classify_existence가 'unknown'으로 남겨
            # 다음 실행에서 재시도된다. (deleted로 확정하면 안 된다)
            et = "retriable_timeout" if isinstance(e, requests.Timeout) else "retriable_network"
            return fail(f"{et}: {e!r}"[:200], error_type=et)

        except Exception as e:
            return fail(repr(e)[:200], error_type="unknown_failure")

    return fail("unexpected loop exit", error_type="unknown_failure")


# ─────────────────────────────────────────────────────────
# L2 — 영상 목록 파싱
# ─────────────────────────────────────────────────────────
def parse_relative_date(text, now=None):
    """'3일 전', '2주 전', '5개월 전', '1년 전' → 근사 날짜. 활성분류용.

    ⚠️ 근사값이다. "3개월 전"을 30일×3으로 계산하므로
    같은 달에 올라온 영상들이 완전히 동일한 timestamp를 갖는다.
    → contents.published_is_approx=1로 표시하고, L2b가 watch 페이지에서
      정확한 값을 받아 덮어쓴다.
    → 이 근사값은 '활동성 분류'용으로만 신뢰할 수 있다.
      (6개월 내 10건인지 판단하는 데는 며칠 오차가 무의미하다)
    """
    if now is None:
        now = datetime.now()
    if not text:
        return None
    m = re.search(r"(\d+)\s*(초|분|시간|일|주|개월|년)", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days_map = {"초": 0, "분": 0, "시간": 0, "일": 1, "주": 7, "개월": 30, "년": 365}
        return now - timedelta(days=n * days_map.get(unit, 0))
    return None


def parse_l2_videos(data):
    """richGridRenderer에서 영상 목록 파싱 (채널의 /videos 탭).

    한 항목에서 뽑는 것: video_id, 제목, 조회수, 상대시간, 영상 길이
    """
    grid = find_first(data, "richGridRenderer")
    if not grid:
        return []
    items = grid.get("contents", [])
    videos = []
    now = datetime.now()   # 루프 밖에서 한 번만. 항목마다 기준 시각이 달라지지 않게.

    for item in items:
        lockup = find_first(item, "lockupViewModel")
        if not lockup:
            continue

        video_id = lockup.get("contentId")

        title = None
        title_obj = find_first(lockup, "lockupMetadataViewModel")
        if title_obj:
            t = title_obj.get("title", {})
            title = t.get("content") if isinstance(t, dict) else None

        # 조회수와 날짜가 같은 metadataRows에 섞여 있어서
        # 텍스트 내용으로 구분한다. ("조회수 1.2만회" / "3개월 전")
        view_text = None
        date_text = None
        meta = find_first(lockup, "contentMetadataViewModel")
        if meta:
            for row in meta.get("metadataRows", []):
                for part in row.get("metadataParts", []):
                    txt = part.get("text", {}).get("content", "")
                    if "조회수" in txt or "views" in txt.lower():
                        view_text = txt
                    elif "전" in txt or "ago" in txt.lower():
                        date_text = txt

        # 영상 길이는 썸네일 우하단 배지에 있다. ":"가 들어간 것을 찾는다.
        duration = None
        badges = find_first(lockup, "thumbnailBottomOverlayViewModel")
        if badges:
            for b in badges.get("badges", []):
                bt = find_first(b, "text")
                if isinstance(bt, str) and ":" in bt:
                    duration = bt
                    break

        videos.append({
            "video_id": video_id,
            "title": title,
            "view_count": parse_count(view_text),
            "published_relative": date_text,                      # 원문 보존
            "published_at_approx": parse_relative_date(date_text, now),
            "duration": duration,
            "duration_sec": parse_duration(duration),
        })
    return videos


def parse_l2_shorts(data):
    """richGridRenderer에서 shorts 목록 파싱 (shortsLockupViewModel 구조).

    ⚠️ 이 프로젝트에서 가장 중요한 제약이 여기 있다.

    쇼츠 탭에는 '업로드 날짜가 표시되지 않는다.'
    유튜브 UI에서 쇼츠 목록을 보면 조회수만 나오고 날짜는 없다.
    화면에 없는 걸 크롤러가 뽑을 수는 없다.

    → published_at_approx = None (아래)
    → contents.published_at이 NULL로 들어간다 (실측 118,350건 전부)
    → classify_activity()가 published_at IS NOT NULL만 세므로
      활동성 판정이 '롱폼만' 반영된다
    → 쇼츠만 올리는 채널이 저평가된다

    쇼츠 게시일은 L2b(watch 페이지)에서만 알 수 있는데,
    활동성 판정은 L2a에서 한다. 순서 의존성이 생긴 것.
    (대응: L2a에서 '게시일 미상 쇼츠가 많으면 dormant 판정 보류' +
           L2b가 dormant 채널의 쇼츠도 수집 + backfill이 재판정)
    """
    grid = find_first(data, "richGridRenderer")
    if not grid:
        return []
    items = grid.get("contents", [])
    videos = []

    for item in items:
        slvm = find_first(item, "shortsLockupViewModel")
        if not slvm:
            continue

        # video_id 추출 경로가 두 가지다. 유튜브 버전에 따라 다르게 온다.
        video_id = None
        on_tap = slvm.get("onTap", {})
        rwe = find_first(on_tap, "reelWatchEndpoint")
        if isinstance(rwe, dict):
            video_id = rwe.get("videoId")
        if not video_id:
            # 폴백: entityId가 "shorts-shelf-item-{videoId}" 형태
            eid = slvm.get("entityId", "")
            if eid.startswith("shorts-shelf-item-"):
                video_id = eid.replace("shorts-shelf-item-", "", 1)
        if not video_id:
            continue

        title = None
        view_text = None
        om = slvm.get("overlayMetadata", {})
        if isinstance(om, dict):
            pt = om.get("primaryText")
            if isinstance(pt, dict):
                title = pt.get("content")
            st = om.get("secondaryText")
            if isinstance(st, dict):
                view_text = st.get("content")

        videos.append({
            "video_id": video_id,
            "title": title,
            "view_count": parse_count(view_text),
            "published_relative": None,      # ← 쇼츠 탭에 날짜가 없다
            "published_at_approx": None,     # ← L2b가 채운다
            "duration": None,                # ← 쇼츠 탭에 길이도 없다
            "duration_sec": None,
        })
    return videos


def parse_watch_page(video_id):
    """영상 개별 페이지 → 정확한 게시일·조회수·좋아요·댓글수·카테고리·영상길이.

    L2b가 쓴다. 목록 페이지가 안 주는 것들을 여기서 얻는다:
      - 정확한 게시일 (목록은 "3개월 전" 상대시간뿐)
      - 좋아요 수, 댓글 수 (목록에 아예 없음 → ER 계산에 필수)
      - 카테고리, 유료광고 여부

    이 정보들 때문에 영상 하나당 요청 1번이 필요하고,
    그게 L2a 대비 8배 요청량과 IP 차단의 원인이 됐다.

    정규식과 find_first를 섞어 쓰는 이유:
    단순 문자열은 정규식이 빠르고(category, publishDate),
    중첩 구조는 find_first가 안전하다(likeButton, viewCount).
    """
    url = f"https://www.youtube.com/watch?v={video_id}&hl=ko&gl=KR"
    resp = get_session().get(url, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        return None, resp.status_code      # 호출자가 429를 구분할 수 있게 코드 반환
    body = resp.text
    data = extract_yt_initial_data(body)
    if data is None:
        return None, resp.status_code

    result = {
        "video_id": video_id,
        "published_at": None, "view_count": None,
        "like_count": None, "comment_count": None, "category": None,
        "duration_sec": None,
        "is_paid_promotion": False,
    }

    m = re.search(r'"category":"([^"]+)"', body)
    if m:
        result["category"] = m.group(1).replace("\\u0026", "&")   # JSON 이스케이프 복원

    m = re.search(r'"publishDate":"([^"]+)"', body)
    if m:
        try:
            dt = datetime.fromisoformat(m.group(1))
            # publishDate 에 오프셋(-07:00, Z 등)이 붙어 오는 경우가 있다.
            # strftime 은 tz 정보를 버리므로, 오프셋이 있으면 KST 로 정규화한다.
            #
            # 이 처리가 없으면: -07:00 시각의 숫자가 그대로 문자열이 되고
            # MySQL(+09:00)이 그걸 KST로 해석해 16시간 어긋난다.
            if dt.tzinfo is not None:
                dt = dt.astimezone(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
            result["published_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            result["published_at"] = m.group(1)[:10]   # 파싱 실패 시 날짜만

    m_dur = re.search(r'"approxDurationMs":"(\d+)"', body)
    if m_dur:
        result["duration_sec"] = int(m_dur.group(1)) // 1000

    like = find_first(data, "likeButtonViewModel")
    if like:
        title = find_first(like, "title")
        if isinstance(title, str):
            result["like_count"] = parse_count(title)

    # 댓글 수는 '댓글' 패널의 부가정보에 들어있다.
    panel = find_first(data, "engagementPanelSectionListRenderer")
    if panel:
        ci = find_first(panel, "contextualInfo")
        if ci:
            runs = ci.get("runs", [])
            if runs:
                result["comment_count"] = parse_count(runs[0].get("text"))

    vc = find_first(data, "viewCount")
    if vc:
        # simpleText / content 두 형태로 온다
        vtext = find_first(vc, "simpleText") or find_first(vc, "content")
        if vtext:
            result["view_count"] = parse_count(vtext)

    # 유료광고 표시 여부. 가이드라인이 '광고/일반 분리 지표'를 요구한다.
    # 공식 API의 hasPaidPromotion 대신 watch 페이지의 오버레이로 판별.
    if re.search(r'"paidContentOverlayRenderer"\s*:\s*\{\s*"text"', body):
        result["is_paid_promotion"] = True
    return result, 200


# ─────────────────────────────────────────────────────────
# 단독 실행 테스트
#
# 크롤러를 돌리지 않고 파서만 검증할 때 쓴다.
#   python -m youtube.crawler.lib.youtube_parser https://youtube.com/@침착맨
# debug_dump=True라 ytinitialdata_dump.json이 저장되어,
# 유튜브가 구조를 바꿨을 때 실제 JSON을 눈으로 확인할 수 있다.
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        test_url = sys.argv[1]
        r = fetch_channel_l1(test_url, debug_dump=True)
        print("ok:", r.ok)
        print("channel_name:", r.channel_name)
        print("channel_opened_at:", r.channel_opened_at)
        print("description:", (r.description or "")[:300])
        print("emails:", r.emails)
        print("→ ytinitialdata_dump.json 에서 aboutChannelViewModel 확인 가능")