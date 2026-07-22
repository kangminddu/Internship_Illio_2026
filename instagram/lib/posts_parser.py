# -*- coding: utf-8 -*-
"""
lib/posts_parser.py

인스타 게시물 목록 GraphQL(PolarisProfilePostsTabContentQuery_connection) 응답 파서.

설계 원칙 (L1 graphql_parser 와 동일):
- Instagram 이 필드 경로를 바꿔도 이 파일 한 곳만 고치면 되도록 격리.
- 실패해도 예외를 던지지 않고 빈 결과/None 을 돌려줌 (수집 파이프라인이 안 끊기게).
- 파생 지표는 절대 계산하지 않음 (raw→derived 원칙). 원값만 추출.

응답 구조 (2026-07 기준, 실측):
  data
    └ xdt_api__v1__feed__user_timeline_graphql_connection
        ├ edges[] → node (게시물)
        └ page_info { end_cursor, has_next_page }
"""

# GraphQL 응답의 최상위 connection 키 (root_field_name 과 동일)
CONNECTION_KEY = "xdt_api__v1__feed__user_timeline_graphql_connection"


# media_type / product_type → contents.content_type enum 매핑
#   contents.content_type enum: 'video','shorts','reels','feed_image','carousel','tiktok'
def _content_type(node):
    mt = node.get("media_type")
    pt = (node.get("product_type") or "").lower()

    # 캐러셀: media_type 8 또는 product_type carousel_container
    if mt == 8 or pt == "carousel_container":
        return "carousel"
    # 릴스/영상: product_type clips, 또는 media_type 2(video)
    if pt == "clips" or mt == 2:
        return "reels"
    # 단일 이미지
    if mt == 1 or pt == "feed":
        return "feed_image"
    # 알 수 없으면 이미지로 폴백 (일단 저장은 되게)
    return "feed_image"


def _caption_text(node):
    cap = node.get("caption")
    if isinstance(cap, dict):
        return cap.get("text")
    return None


def _thumbnail_url(node):
    iv = node.get("image_versions2")
    if isinstance(iv, dict):
        cands = iv.get("candidates")
        if isinstance(cands, list) and cands:
            # candidates[0] 이 원본(가장 큰) 해상도
            first = cands[0]
            if isinstance(first, dict):
                return first.get("url")
    return None


def _pick(node, *keys):
    """여러 후보 키 중 첫 non-None. IG 스키마 변형 대비."""
    for k in keys:
        v = node.get(k)
        if v is not None:
            return v
    return None


def parse_post_node(node):
    """단일 게시물 node → 정규화 dict. 실패 시 None (external_id 없으면 버림)."""
    if not isinstance(node, dict):
        return None

    # external_id: shortcode(code)를 우선 사용
    # 예외적으로 code가 없으면 pk -> id 순으로 폴백
    external_id = node.get("code")

    # 예외적으로 code가 없으면 pk 사용
    if not external_id:
        external_id = node.get("pk")

    # 그것도 없으면 id(pk_userid 형태)에서 pk 부분 사용
    if not external_id:
        full_id = node.get("id")
        if full_id and "_" in full_id:
            external_id = full_id.split("_", 1)[0]
        else:
            external_id = full_id

    if not external_id:
        return None

    return {
        "external_id": str(external_id),
        "content_type": _content_type(node),
        "taken_at": node.get("taken_at"),                 # 유닉스 초 (필수)
        "caption_text": _caption_text(node),
        "like_count": _pick(node, "like_count"),
        "comment_count": _pick(node, "comment_count"),
        "view_count": _pick(node, "view_count", "play_count"),  # 이미지는 None
        "is_paid_promotion": 1 if node.get("is_paid_partnership") else 0,
        "carousel_media_count": node.get("carousel_media_count"),
        "thumbnail_url": _thumbnail_url(node),
        # 목록 단계에서 duration 은 안 옴 → None (L2b/상세에서 채울 자리)
        "duration_sec": None,
    }


def parse_posts(data):
    """
    전체 GraphQL 응답 → (posts, page_info)
      posts: 정규화된 게시물 dict 리스트
      page_info: {"end_cursor":..., "has_next_page":bool}
    실패해도 ([], {}) 반환. 절대 예외 안 던짐.
    """
    try:
        conn = (data or {}).get("data", {}).get(CONNECTION_KEY)
        if not isinstance(conn, dict):
            return [], {}

        posts = []
        for edge in conn.get("edges", []) or []:
            node = edge.get("node") if isinstance(edge, dict) else None
            p = parse_post_node(node)
            if p:
                posts.append(p)

        pi = conn.get("page_info") or {}
        page_info = {
            "end_cursor": pi.get("end_cursor"),
            "has_next_page": bool(pi.get("has_next_page")),
        }
        return posts, page_info
    except Exception:
        return [], {}