# tiktok/parser.py
import json
import re

UNIVERSAL_DATA_RE = re.compile(
    r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def extract_universal_data(html):
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


def parse_l1(html):
    data = extract_universal_data(html)
    scope = data.get("__DEFAULT_SCOPE__", {})
    detail = scope.get("webapp.user-detail", {})
    info = detail.get("userInfo")
    if not info:
        return None
    user = info.get("user", {})
    stats = info.get("stats", {}) or info.get("statsV2", {})
    return {
        "handle": user.get("uniqueId"),
        "nickname": user.get("nickname"),
        "sec_uid": user.get("secUid"),
        "user_id": user.get("id"),
        "bio": user.get("signature"),
        "verified": user.get("verified"),
        "follower_count": _to_int(stats.get("followerCount")),
        "following_count": _to_int(stats.get("followingCount")),
        "heart_count": _to_int(stats.get("heartCount") or stats.get("heart")),
        "video_count": _to_int(stats.get("videoCount")),
    }

# 기존 tiktok/parser.py 맨 아래에 이어서 추가

# tiktok/parser.py 맨 아래 (기존 parse_l2 초안은 지우고 이걸로)

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
    """
    if not isinstance(payload, dict):
        return [], None, False, None, None

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
        if follower_count is None:
            astats = item.get("authorStats") or {}
            follower_count = _to_int(astats.get("followerCount"))

        videos.append({
            "external_id": str(item.get("id")) if item.get("id") else None,
            "caption_text": item.get("desc", ""),
            "published_at": _to_int(item.get("createTime")),   # 유닉스 초
            "duration_sec": _to_int(video_info.get("duration")),
            "view_count": _to_int(stats.get("playCount")),
            "like_count": _to_int(stats.get("diggCount")),
            "comment_count": _to_int(stats.get("commentCount")),
            "is_paid_promotion": 1 if item.get("isAd") else 0,
            "category": _to_int(item.get("CategoryType")),
            "is_pinned": bool(item.get("isPinnedItem")),
        })

    return videos, cursor, has_more, follower_count, status_code



def parse_comment_list(payload):
    """
    /api/comment/list/ 응답 파싱

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

            "external_content_id": str(c.get("aweme_id")) if c.get("aweme_id") else None,

            "author_id": user.get("uid"),

            "unique_id": user.get("unique_id"),

            "nickname": user.get("nickname"),

            "text": c.get("text"),

            "like_count": _to_int(c.get("digg_count")),

            "published_at": _to_int(c.get("create_time")),

            "reply_count": _to_int(c.get("reply_comment_total")),

            "author_pin": bool(c.get("author_pin")),

            "author_liked": bool(c.get("is_author_digged")),

            "is_creator": bool(c.get("label_list")),

        })

    return (
        parsed,
        cursor,
        has_more,
        total,
        status_code,
    )

def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


