# tiktok/antibot/not_found.py
"""TikTok Not Found Detector.

역할
------
현재 페이지가 '계정을 찾을 수 없음' 상태인지 감지한다.
DB 업데이트는 l1이 담당한다.

★ 책임 분리가 명확하다.
  이 모듈은 '판별'만 하고 DB를 모른다.
  l1.py가 결과를 받아 channel_id_status='not_found'로 마킹한다.
  판별 로직만 따로 테스트할 수 있고, 다른 단계에서도 재사용 가능하다.

★ 이 판정은 되돌릴 수 없다.
  not_found로 마킹되면 channel_id_status가 바뀌어 이후 모든 단계에서
  영구 제외된다. blocked/server_error는 재시도되지만 이건 아니다.
  → 오탐이 나면 멀쩡한 채널이 사라진다. 그래서 아래처럼
    "확신이 없으면 판단 보류"를 원칙으로 삼았다.

판별 방식 (실측 기반, 가장 안정적인 순서)
------
TikTok은 __UNIVERSAL_DATA_FOR_REHYDRATION__ JSON의 webapp.user-detail 에
계정 상태를 명확히 담는다:
  - 정상 계정 : statusCode == 0, userInfo 존재 (user/stats 포함)
  - 없는 계정 : statusCode == 10221, userInfo 없음
      (@gamnara96 실측: keys=['statusCode','statusMsg','needFix'])

따라서 HTML 문자열 검색(번역 사전 오탐)이나 DOM :visible(렌더 타이밍 의존)
대신, 이 statusCode/userInfo로 판별하는 것이 가장 정확하고 안정적이다.

  ── 왜 문자열 검색을 안 쓰나 ──
  "이 계정을 찾지 못했습니다"로 검색하면:
    · 언어 설정에 따라 문구가 달라진다 (영어/일본어 접속 시 실패)
    · 틱톡이 문구를 바꾸면 조용히 깨진다
    · 정상 채널의 bio에 그 문구가 있으면 오탐
  statusCode는 숫자라 이런 문제가 없다.

  ── 왜 DOM 검사를 안 쓰나 ──
  "계정 없음" 화면이 렌더링되기 전에 검사하면 놓친다.
  JSON은 HTML에 이미 박혀 있어 렌더링 타이밍과 무관하다.

parser.extract_universal_data가 이미 같은 JSON을 파싱하므로 재활용한다.
"""

from typing import Optional

from tiktok import parser


# user-detail statusCode 중 "계정 없음/불가"로 간주할 값.
# 10221 = user not found (실측). 0 = 정상. 그 외 비정상은 보수적으로 확장 가능.
#
# set으로 둔 이유: 나중에 '정지된 계정' 같은 다른 코드를 발견하면
# 여기 숫자만 추가하면 된다. 로직을 안 고쳐도 된다.
NOT_FOUND_STATUS_CODES = {10221}

# 리다이렉트 URL 신호 (보조)
# 계정이 없으면 /404로 튕기는 경우가 있다. JSON을 파싱하기 전에
# 빠르게 걸러낼 수 있어 먼저 검사한다.
URL_KEYWORDS = ("/404", "/notfound")


def _user_detail(html: str) -> Optional[dict]:
    """__UNIVERSAL_DATA__ 에서 webapp.user-detail dict를 꺼낸다. 없으면 None.

    parser의 함수를 재사용한다. 같은 JSON을 두 번 파싱하는 셈이지만
    (l1의 parse_l1도 같은 걸 파싱한다) 정규식+json.loads가 밀리초 단위라
    네트워크 대기 시간에 비하면 무시할 수 있다.
    → 중복 파싱보다 '파싱 로직이 한 곳에만 있는 것'이 중요하다.
    """
    try:
        data = parser.extract_universal_data(html)
        scope = data.get("__DEFAULT_SCOPE__", {})
        return scope.get("webapp.user-detail")
    except Exception:
        return None


async def reason(page, html: Optional[str] = None):
    """not_found면 원인 문자열, 정상이면 None.

    bool이 아니라 '사유 문자열'을 반환하는 이유:
    "status:10221"처럼 근거가 남아야 나중에 오탐을 추적할 수 있다.
    detect()는 이걸 bool로 감싼 얇은 래퍼다.

    html이 주어지면 그걸 쓰고(재요청 없음), 없으면 page.content()로 가져온다.
    → l1은 이미 받아둔 html을 넘겨서 중복 요청을 피한다.
    """
    # 0) URL 리다이렉트 (계정 없음 시 /404 등으로 튕기는 경우)
    #    가장 싼 검사라 먼저 한다. JSON 파싱이 필요 없다.
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
            # ★ 여기가 중요하다. 페이지를 못 읽은 걸 '계정 없음'으로
            #   판정하면 네트워크 오류 때문에 멀쩡한 채널이 영구 제외된다.

    detail = _user_detail(html)

    # user-detail 자체가 없으면 판단 보류.
    # (JSON 미로딩/구조 변경일 수 있어 not_found로 단정하지 않는다)
    #
    # ★ 실제로 이 경우가 많다. 틱톡이 CSR로 응답하면 HTML에
    #   __UNIVERSAL_DATA__ 자체가 없어서 여기 걸린다.
    #   그때 not_found로 판정했다면 수백 개 채널이 잘못 제외됐을 것이다.
    #   l1.py가 이 경우를 'blocked/server_error'로 따로 분류해 재시도한다.
    if detail is None:
        return None

    status = detail.get("statusCode")

    # 명시적으로 "계정 없음" 코드면 not_found 확정
    if status in NOT_FOUND_STATUS_CODES:
        return f"status:{status}"

    # statusCode가 0이 아니고 userInfo도 없으면 계정 없음으로 간주
    # (10221 외의 계정 불가 코드 대비. 정상(0)은 절대 여기 안 걸림)
    #
    # 조건이 두 겹인 이유: statusCode만 보면 우리가 모르는 코드에
    # 과잉 반응할 수 있다. userInfo까지 없어야 확정한다.
    # (정상 계정은 statusCode=0이라 앞 조건에서 이미 걸러진다)
    if status not in (None, 0) and "userInfo" not in detail:
        return f"status:{status}"

    return None


async def detect(page, html=None):
    """bool 래퍼. l1.py가 이걸 쓴다."""
    return (await reason(page, html)) is not None


async def print_status(page, html=None):
    """디버깅용. 판정 결과와 근거를 화면에 출력한다.

    새로운 실패 유형을 만났을 때 이걸로 statusCode를 확인하고
    NOT_FOUND_STATUS_CODES에 추가할지 판단한다.
    """
    r = await reason(page, html)
    if r is None:
        print("[not_found] PASS")
    else:
        print(f"[not_found] DETECT -> {r}")