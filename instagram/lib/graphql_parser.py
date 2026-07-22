def _pick_count(user, flat_key, edge_key):
    """follower_count(내부 API 형태)와 edge_followed_by.count(웹 GraphQL 형태) 모두 대응."""
    v = user.get(flat_key)
    if v is not None:
        return v
    edge = user.get(edge_key)
    if isinstance(edge, dict):
        return edge.get("count")
    return None


def parse_profile(data):
    # {"data": {"user": null}} 같은 응답에서 안전하게 None 반환 -> caller 가 HTML 판별로 폴백
    user = (data or {}).get("data", {}).get("user")
    if not user:
        return None

    return {
        "user_id": user.get("id"),
        "pk": user.get("pk") or user.get("id"),
        "username": user.get("username"),
        "nickname": user.get("full_name"),
        "biography": user.get("biography"),

        "followers": _pick_count(user, "follower_count", "edge_followed_by"),
        "following": _pick_count(user, "following_count", "edge_follow"),
        "posts": _pick_count(user, "media_count", "edge_owner_to_timeline_media"),

        "profile_pic_url": user.get("profile_pic_url"),
        "external_url": user.get("external_url"),

        "category_name": user.get("category") or user.get("category_name"),
        "account_type": user.get("account_type"),

        "is_private": user.get("is_private"),
        "is_verified": user.get("is_verified"),
    }