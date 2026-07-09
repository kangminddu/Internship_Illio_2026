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


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
