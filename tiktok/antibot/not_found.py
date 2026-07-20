# tiktok/antibot/not_found.py
"""TikTok Not Found Detector.

역할
------
현재 페이지가 '계정을 찾을 수 없음' 상태인지 감지한다.
DB 업데이트는 l1이 담당한다.

판별 방식 (실측 기반, 가장 안정적인 순서)
------
TikTok은 __UNIVERSAL_DATA_FOR_REHYDRATION__ JSON의 webapp.user-detail 에
계정 상태를 명확히 담는다:
  - 정상 계정 : statusCode == 0, userInfo 존재 (user/stats 포함)
  - 없는 계정 : statusCode == 10221, userInfo 없음
      (@gamnara96 실측: keys=['statusCode','statusMsg','needFix'])

따라서 HTML 문자열 검색(번역 사전 오탐)이나 DOM :visible(렌더 타이밍 의존)
대신, 이 statusCode/userInfo로 판별하는 것이 가장 정확하고 안정적이다.

parser.extract_universal_data가 이미 같은 JSON을 파싱하므로 재활용한다.
"""

from typing import Optional

from tiktok import parser


# user-detail statusCode 중 "계정 없음/불가"로 간주할 값.
# 10221 = user not found (실측). 0 = 정상. 그 외 비정상은 보수적으로 확장 가능.
NOT_FOUND_STATUS_CODES = {10221}

# 리다이렉트 URL 신호 (보조)
URL_KEYWORDS = ("/404", "/notfound")


def _user_detail(html: str) -> Optional[dict]:
    """__UNIVERSAL_DATA__ 에서 webapp.user-detail dict를 꺼낸다. 없으면 None."""
    try:
        data = parser.extract_universal_data(html)
        scope = data.get("__DEFAULT_SCOPE__", {})
        return scope.get("webapp.user-detail")
    except Exception:
        return None


async def reason(page, html: Optional[str] = None):
    """not_found면 원인 문자열, 정상이면 None.

    html이 주어지면 그걸 쓰고(재요청 없음), 없으면 page.content()로 가져온다.
    """
    # 0) URL 리다이렉트 (계정 없음 시 /404 등으로 튕기는 경우)
    try:
        url = page.url.lower()
        for kw in URL_KEYWORDS:
            if kw in url:
                return f"url:{kw}"
    except Exception:
        pass

    # 1) JSON statusCode 기반 판별 (주력)
    if html is None:
        try:
            html = await page.content()
        except Exception:
            return None  # 페이지를 못 읽으면 판단 보류 (not_found로 단정 안 함)

    detail = _user_detail(html)

    # user-detail 자체가 없으면 판단 보류.
    # (JSON 미로딩/구조 변경일 수 있어 not_found로 단정하지 않는다)
    if detail is None:
        return None

    status = detail.get("statusCode")

    # 명시적으로 "계정 없음" 코드면 not_found 확정
    if status in NOT_FOUND_STATUS_CODES:
        return f"status:{status}"

    # statusCode가 0이 아니고 userInfo도 없으면 계정 없음으로 간주
    # (10221 외의 계정 불가 코드 대비. 정상(0)은 절대 여기 안 걸림)
    if status not in (None, 0) and "userInfo" not in detail:
        return f"status:{status}"

    return None


async def detect(page, html=None):
    return (await reason(page, html)) is not None


async def print_status(page, html=None):
    r = await reason(page, html)
    if r is None:
        print("[not_found] PASS")
    else:
        print(f"[not_found] DETECT -> {r}")