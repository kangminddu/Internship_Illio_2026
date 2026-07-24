"""
YouTube 공통 파싱 라이브러리

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

import re
import json
import threading
import requests
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional


# ─────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
CHANNEL_URL_SUFFIX = "/about?hl=en&gl=US"
REQUEST_TIMEOUT = 20

# 스레드마다 독립 Session (연결 재사용 + 스레드 안전)
_thread_local = threading.local()

def get_session():
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update(HEADERS)
        # consent 페이지 우회 + 쿠키 없는 콜드 요청의 봇 판정 완화
        s.cookies.set("SOCS", "CAI", domain=".youtube.com")
        _thread_local.session = s
    return _thread_local.session


# ─────────────────────────────────────────────────────────
# 이메일 추출 (공개 설명란에 크리에이터가 직접 적어둔 것만)
# ─────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')

def extract_emails(text):
    if not text:
        return []
    return list({e.lower() for e in EMAIL_RE.findall(text)})


# ─────────────────────────────────────────────────────────
# 결과 구조
# ─────────────────────────────────────────────────────────
@dataclass
class ChannelL1:
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
    url = url.strip()

    # 문자열 어디에 있든 UC 채널ID를 최우선 추출
    m = re.search(r"(UC[\w-]{22})", url)
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

    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None

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
    """중첩 dict/list에서 key를 DFS로 처음 만나는 값 반환."""
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
    """
    base_url = normalize_channel_url(channel_url)
    url = base_url + CHANNEL_URL_SUFFIX
    now = datetime.now(timezone.utc).isoformat()

    def fail(error, http_status=None, had_yt_data=False, page_signal=None, error_type=None):
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
                rl_headers = {k: v for k, v in resp.headers.items()
                              if "ratelimit" in k.lower() or k.lower() == "retry-after"}
                return fail(f"HTTP 429 headers={rl_headers}", http_status=429, error_type="rate_limited")
            # 그 외 non-200 → 확정 결과, 재시도 안 함
            if code != 200:
                return fail(f"HTTP {code}", http_status=code, error_type="http_error")

            # ── 200: 정상 처리 ──
            body = resp.text
            page_signal = None

            # 1) 밴/정지/해지 (긴 문구로 오탐 방지, hl=en 페이지이므로 영어 시그널이 주로 작동)
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
                return fail("ytInitialData not found", http_status=code,
                            had_yt_data=False, page_signal=page_signal,
                            error_type="no_yt_data")

            if debug_dump:
                with open("ytinitialdata_dump.json", "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print("  ↳ ytinitialdata_dump.json 저장됨")

            fields = parse_l1(data)

            if fields.get("subscriber_count") is None and fields.get("channel_name") is None:
                return fail("about block missing", http_status=code, had_yt_data=True,
                            page_signal=page_signal, error_type="about_missing")

            return ChannelL1(
                **fields, source_url=url, captured_at=now, ok=True,
                http_status=code, had_yt_data=True, page_signal=page_signal,
            )

        except (requests.Timeout, requests.ConnectionError) as e:
            # 일시적 네트워크 장애 → 백오프 후 재시도
            if attempt < max_retries:
                time.sleep(2 * (attempt + 1))
                continue
            et = "retriable_timeout" if isinstance(e, requests.Timeout) else "retriable_network"
            return fail(f"{et}: {e!r}"[:200], error_type=et)

        except Exception as e:
            return fail(repr(e)[:200], error_type="unknown_failure")

    return fail("unexpected loop exit", error_type="unknown_failure")


# ─────────────────────────────────────────────────────────
# L2 — 영상 목록 파싱
# ─────────────────────────────────────────────────────────
def parse_relative_date(text, now=None):
    """'3일 전', '2주 전', '5개월 전', '1년 전' → 근사 날짜. 활성분류용."""
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
    """richGridRenderer에서 영상 목록 파싱."""
    grid = find_first(data, "richGridRenderer")
    if not grid:
        return []
    items = grid.get("contents", [])
    videos = []
    now = datetime.now()

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
            "published_relative": date_text,
            "published_at_approx": parse_relative_date(date_text, now),
            "duration": duration,
            "duration_sec": parse_duration(duration),
        })
    return videos


def parse_l2_shorts(data):
    """richGridRenderer에서 shorts 목록 파싱 (shortsLockupViewModel 구조)."""
    grid = find_first(data, "richGridRenderer")
    if not grid:
        return []
    items = grid.get("contents", [])
    videos = []

    for item in items:
        slvm = find_first(item, "shortsLockupViewModel")
        if not slvm:
            continue

        video_id = None
        on_tap = slvm.get("onTap", {})
        rwe = find_first(on_tap, "reelWatchEndpoint")
        if isinstance(rwe, dict):
            video_id = rwe.get("videoId")
        if not video_id:
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
            "published_relative": None,
            "published_at_approx": None,
            "duration": None,
            "duration_sec": None,
        })
    return videos


def parse_watch_page(video_id):
    """영상 개별 페이지 → 정확한 게시일·조회수·좋아요·댓글수·카테고리·영상길이."""
    url = f"https://www.youtube.com/watch?v={video_id}&hl=ko&gl=KR"
    resp = get_session().get(url, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        return None, resp.status_code
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
        result["category"] = m.group(1).replace("\\u0026", "&")

    m = re.search(r'"publishDate":"([^"]+)"', body)
    if m:
        try:
            dt = datetime.fromisoformat(m.group(1))
            result["published_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            result["published_at"] = m.group(1)[:10]

    m_dur = re.search(r'"approxDurationMs":"(\d+)"', body)
    if m_dur:
        result["duration_sec"] = int(m_dur.group(1)) // 1000

    like = find_first(data, "likeButtonViewModel")
    if like:
        title = find_first(like, "title")
        if isinstance(title, str):
            result["like_count"] = parse_count(title)

    panel = find_first(data, "engagementPanelSectionListRenderer")
    if panel:
        ci = find_first(panel, "contextualInfo")
        if ci:
            runs = ci.get("runs", [])
            if runs:
                result["comment_count"] = parse_count(runs[0].get("text"))

    vc = find_first(data, "viewCount")
    if vc:
        vtext = find_first(vc, "simpleText") or find_first(vc, "content")
        if vtext:
            result["view_count"] = parse_count(vtext)

    if re.search(r'"paidContentOverlayRenderer"\s*:\s*\{\s*"text"', body):
        result["is_paid_promotion"] = True
    return result, 200


# ─────────────────────────────────────────────────────────
# 단독 실행 테스트
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