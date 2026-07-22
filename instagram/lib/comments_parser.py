# lib/comments_parser.py

from datetime import datetime


def parse_comments(data):
    """
    Instagram GraphQL 댓글 응답 파싱

    Parameters
    ----------
    data : dict
        response.json()

    Returns
    -------
    list[dict]
    """

    results = []

    connection = (
        data.get("data", {})
            .get("xdt_api__v1__media__media_id__comments__connection", {})
    )

    edges = connection.get("edges", [])

    for edge in edges:
        node = edge.get("node", {})
        user = node.get("user", {})

        created_at = node.get("created_at")

        if created_at:
            published_at = datetime.fromtimestamp(created_at)
        else:
            published_at = None

        results.append(
            {
                # 댓글 ID
                "external_comment_id": (
                    str(node.get("pk")) if node.get("pk") else None
                ),

                # 현재는 부모 댓글만 수집
                "parent_comment_id": None,

                # 작성자
                "external_author_id": (
                    str(user.get("pk")) if user.get("pk") else None
                ),
                "author_display_name": user.get("username"),

                # 댓글 내용
                "comment_text": node.get("text", ""),

                # 작성 시각
                "published_at": published_at,

                # 좋아요 수
                "like_count": node.get("comment_like_count", 0),

                # 답글 수 (현재는 참고용)
                "reply_count": node.get("child_comment_count", 0),

                # 기타 메타
                "is_edited": bool(node.get("is_edited")),
                "has_translation": bool(node.get("has_translation")),
            }
        )

    return results