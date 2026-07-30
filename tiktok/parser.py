# tiktok/parser.py
"""
틱톡 응답 파싱 라이브러리.

유튜브 파서와 결정적으로 다른 점
------
유튜브는 HTML에 박힌 ytInitialData 하나만 보면 됐다.
틱톡은 데이터가 오는 경로가 넷이다:

    parse_l1           HTML의 __UNIVERSAL_DATA__     (SSR)
    parse_user_detail  /api/user/detail/  XHR        (CSR)
    parse_item_list    /api/post/item_list/ XHR      (L2 영상 목록)
    parse_comment_list /api/comment/list/ XHR        (L3 댓글)

L1이 두 경로를 다 가진 이유가 이 파일의 핵심 이야기다.
틱톡은 같은 URL이라도 요청마다 SSR/CSR을 다르게 내려준다.
HTML 파싱만 하면 확률적으로 실패한다. (아래 parse_user_detail 참고)

그리고 API 응답 필드명이 카멜/스네이크로 섞여 있다.
    item_list  : createTime, playCount, diggCount   (카멜)
    comment_list: create_time, digg_count, has_more (스네이크)
같은 회사 API인데 엔드포인트마다 규칙이 다르다. 그대로 맞춰 읽는다.
"""
import json
import re

UNIVERSAL_DATA_RE = re.compile(
    r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def extract_universal_data(html):
    """HTML에서 __UNIVERSAL_DATA__ JSON을 뽑는다.

    빈 dict를 반환하는 경우가 셋이다:
      1) script 태그 자체가 없음        → CSR 응답이거나 차단/에러 페이지
      2) 태그는 있는데 내용이 비어 있음 → JS 실행 전에 HTML을 가져간 경우
      3) JSON 파싱 실패                → 잘린 응답

    2번이 실제로 문제가 됐다. 틱톡은 빈 script 태그를 먼저 DOM에 넣고
    JS가 나중에 내용을 채우는데, l1.py가 wait_for_selector(state="attached")로
    기다리다 보니 '태그는 붙었지만 내용은 빈' 상태에서 통과했다.
    → l1.py의 대기 조건을 textContent 길이로 바꿔서 해결.
    """
    m = UNIVERSAL_DATA_RE.search(html)
    if not m:
        return {}
    raw = m.group(1).strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _user_row(info):
    """userInfo dict → L1 저장용 형태.

    SSR(HTML의 __UNIVERSAL_DATA__)과 CSR(/api/user/detail/ XHR)의
    userInfo 구조가 동일하므로 두 경로가 이 함수를 공유한다.

    ← 이게 중요하다. 구조가 같다는 걸 확인했기 때문에
      변환 로직을 한 벌만 두고 두 경로가 나눠 쓸 수 있다.
      복사해뒀다면 한쪽만 고쳐서 결과가 갈렸을 것이다.
    """
    if not info:
        return None
    user = info.get("user", {})
    # statsV2는 큰 숫자를 문자열로 주는 신버전 필드.
    # 두 형태가 시점/지역에 따라 섞여 와서 둘 다 대비한다.
    stats = info.get("stats", {}) or info.get("statsV2", {})
    return {
        "handle": user.get("uniqueId"),        # @핸들
        "nickname": user.get("nickname"),      # 표시 이름
        "sec_uid": user.get("secUid"),         # 틱톡 내부 영구 ID.
                                               # 핸들은 바뀌어도 이건 안 바뀐다.
                                               # → channels.external_channel_id에 저장
        "user_id": user.get("id"),
        "bio": user.get("signature"),
        "verified": user.get("verified"),
        "private_account": bool(user.get("privateAccount")),
        # ↑ 비공개 계정은 프로필은 보이지만 영상 목록이 막힌다.
        #   L2가 실패했을 때 '수집 실패'인지 '원래 못 보는 계정'인지
        #   구분하려고 함께 저장한다.
        "follower_count": _to_int(stats.get("followerCount")),
        "following_count": _to_int(stats.get("followingCount")),
        # heartCount(신) / heart(구) 둘 다 대비. 누적 좋아요.
        "heart_count": _to_int(stats.get("heartCount") or stats.get("heart")),
        "video_count": _to_int(stats.get("videoCount")),
    }


def parse_l1(html):
    """SSR 경로: HTML에 박힌 __UNIVERSAL_DATA__에서 프로필 파싱.

    틱톡이 CSR로 내려주면 이 스크립트가 없으므로 None이 반환된다.
    그 경우 parse_user_detail()로 XHR 응답을 파싱해야 한다.
    """
    data = extract_universal_data(html)
    scope = data.get("__DEFAULT_SCOPE__", {})
    detail = scope.get("webapp.user-detail", {})
    return _user_row(detail.get("userInfo"))
    # 유튜브의 find_first(재귀 탐색)와 달리 경로를 직접 쓴다.
    # 틱톡 구조가 2단계로 얕고 안정적이라 그래도 된다.


def parse_user_detail(payload):
    """CSR 경로: /api/user/detail/ XHR 응답(dict)에서 프로필 파싱.

    틱톡은 같은 URL이라도 요청마다 SSR/CSR을 다르게 내려준다.
    CSR 응답에서는 HTML이 껍데기이고 데이터가 이 XHR로만 오므로,
    HTML 파싱만으로는 확률적으로 실패한다.

    ── 어떻게 알아냈나 ──
    L1 실패율이 30~50%였는데 원인을 못 찾아 통제 변수를 하나씩 배제했다.
    CPU, 메모리, IP 3종(EC2/가정용/모바일), 로그인 여부,
    브라우저 컨텍스트 재사용, 캐시/쿠키, 동시성,
    요청 간격 3초~30초, 리소스 차단, headless 여부 — 전부 무관했다.

    성공/실패 HTML을 대조하고서야 보였다.
      성공: 384KB, __UNIVERSAL_DATA__ 있음, 프로필이 HTML에 렌더링됨
      실패: 102KB, 스크립트 없음, "문제가 발생했습니다"

    그리고 네트워크 로그를 보니 실패 케이스에서도
    /api/user/detail/이 200으로 응답하고 있었다.
    → 데이터는 왔는데 파서가 HTML만 보고 있었던 것.

    payload: response.json() 으로 받은 dict
    """
    if not isinstance(payload, dict):
        return None
    return _user_row(payload.get("userInfo"))


def parse_item_list(payload):
    """
    /api/post/item_list/ XHR 응답(dict)에서 영상 목록 파싱.
    payload: response.json() 로 받은 dict.
    반환: (videos, cursor, has_more, follower_count, status_code)
      - videos: 영상 dict 리스트
      - cursor: 다음 페이지 요청용 문자열 (없으면 None)
      - has_more: bool
      - follower_count: authorStats.followerCount (첫 영상 기준, 없으면 None)
      - status_code: 0 이면 정상

    ★ 유튜브와 결정적으로 다른 점: createTime이 유닉스 타임스탬프다.

    유튜브는 목록 페이지에 "3개월 전" 상대시간만 있고 쇼츠는 날짜조차 없어서,
    근사값으로 넣고 L2b가 나중에 정확한 값으로 덮어써야 했다.
    (그래서 활동성 판정이 2단계가 되고, backfill 단계가 필요해졌다)

    틱톡은 정확한 시각을 바로 주므로 published_is_approx=0으로 확정 저장한다.
    → L2가 수집하면서 그 자리에서 활동성을 확정 판정할 수 있고,
      backfill 같은 재판정 단계가 필요 없다.
    """
    if not isinstance(payload, dict):
        return [], None, False, None, None

    # 엔드포인트마다 키 이름이 다르다. 둘 다 시도.
    status_code = payload.get("statusCode", payload.get("status_code"))
    item_list = payload.get("itemList") or []
    cursor = payload.get("cursor")
    has_more = bool(payload.get("hasMore"))

    follower_count = None
    videos = []
    for item in item_list:
        stats = item.get("stats") or item.get("statsV2") or {}
        video_info = item.get("video") or {}

        # 팔로워: 매 영상 authorStats 에 들어있음. 첫 유효값 사용.
        #
        # 영상 목록 응답에 작성자 정보가 딸려 온다.
        # L2가 이 값으로 channel_snapshots를 갱신하므로,
        # 팔로워 수를 얻으려고 프로필 페이지를 따로 요청할 필요가 없다.
        if follower_count is None:
            astats = item.get("authorStats") or {}
            follower_count = _to_int(astats.get("followerCount"))

        videos.append({
            "external_id": str(item.get("id")) if item.get("id") else None,
            "caption_text": item.get("desc", ""),
            "published_at": _to_int(item.get("createTime")),   # 유닉스 초
            # ↑ 호출자(l2.py의 _dt)가 KST datetime으로 변환한다.
            #   파서는 원본 그대로 넘기고 변환은 저장 계층이 담당.
            "duration_sec": _to_int(video_info.get("duration")),
            "view_count": _to_int(stats.get("playCount")),
            "like_count": _to_int(stats.get("diggCount")),     # digg = 틱톡 용어
            "comment_count": _to_int(stats.get("commentCount")),
            "is_paid_promotion": 1 if item.get("isAd") else 0,
            # ↑ 유튜브는 watch 페이지의 오버레이 HTML로 판별해야 했는데
            #   틱톡은 API가 boolean으로 직접 준다.
            "category": _to_int(item.get("CategoryType")),
            "is_pinned": bool(item.get("isPinnedItem")),
            # ↑ 고정 영상은 최신순 정렬을 흐트러뜨린다.
            #   (프로필 맨 앞에 오지만 오래된 영상일 수 있음)
        })

    return videos, cursor, has_more, follower_count, status_code


def parse_comment_list(payload):
    """
    /api/comment/list/ 응답 파싱

    이 엔드포인트는 스네이크 케이스를 쓴다 (item_list는 카멜).
        create_time, digg_count, has_more, status_code, aweme_id
    같은 회사 API인데 규칙이 다르다. 그대로 맞춰 읽는다.

    반환
    -------
    comments : list
    cursor : int
    has_more : bool
    total : int
    status_code : int
    """
    if not isinstance(payload, dict):
        return [], None, False, 0, None

    status_code = payload.get("status_code")
    comments = payload.get("comments") or []
    cursor = payload.get("cursor")
    has_more = bool(payload.get("has_more"))
    total = _to_int(payload.get("total"))

    parsed = []
    for c in comments:
        user = c.get("user") or {}
        parsed.append({
            "comment_id": str(c.get("cid")) if c.get("cid") else None,
            "external_content_id": (str(c.get("aweme_id"))   # aweme = 틱톡 내부 영상 용어
                                    if c.get("aweme_id") else None),
            "author_id": user.get("uid"),
            # ↑ 팬 식별의 기준. fans.external_author_id로 저장되어
            #   "같은 사람이 여러 영상에 댓글을 달았나"를 추적한다.
            #   (가이드라인의 '코어 팬덤 모수' 측정에 필수)
            "unique_id": user.get("unique_id"),
            "nickname": user.get("nickname"),
            "text": c.get("text"),
            "like_count": _to_int(c.get("digg_count")),
            "published_at": _to_int(c.get("create_time")),   # 유닉스 초
            "reply_count": _to_int(c.get("reply_comment_total")),
            "author_pin": bool(c.get("author_pin")),
            "author_liked": bool(c.get("is_author_digged")),
            # ↑ 크리에이터가 하트를 눌렀는지. 참여 깊이 지표로 쓸 수 있다.
            "is_creator": bool(c.get("label_list")),
        })

    return parsed, cursor, has_more, total, status_code


def _to_int(v):
    """None/빈문자열/문자열숫자를 안전하게 int로.

    틱톡 API가 같은 필드를 숫자로도, 문자열로도 준다
    (특히 statsV2는 큰 수를 문자열로 준다).
    실패하면 0이 아니라 None을 반환하는 게 중요하다 —
    '값이 0'과 '값을 모름'은 지표 계산에서 다르게 취급해야 한다.
    """
    try:
        return int(v)
    except (TypeError, ValueError):
        return None