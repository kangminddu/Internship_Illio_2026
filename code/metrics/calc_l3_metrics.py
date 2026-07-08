import pymysql
from datetime import datetime, timezone

from config import DB

conn = pymysql.connect(**DB, autocommit=True)

with conn.cursor() as cur:
    # L3 수집된 채널 (comments가 있는 채널)
    cur.execute("""
        SELECT DISTINCT ch.channel_id, cr.nickname
        FROM comments c
        JOIN contents ct ON c.content_id = ct.content_id
        JOIN channels ch ON ct.channel_id = ch.channel_id
        JOIN creators cr ON ch.creator_id = cr.creator_id
    """)
    channels = cur.fetchall()
    print(f"L3 지표 계산 대상: {len(channels)}개 채널\n")

    for channel_id, nickname in channels:
        # 이 채널에서 댓글 수집된 영상 수
        cur.execute("""
            SELECT COUNT(DISTINCT c.content_id)
            FROM comments c
            JOIN contents ct ON c.content_id = ct.content_id
            WHERE ct.channel_id = %s
        """, (channel_id,))
        video_count = cur.fetchone()[0]

        # 전체 고유 댓글 작성자 수
        cur.execute("""
            SELECT COUNT(DISTINCT c.fan_id)
            FROM comments c
            JOIN contents ct ON c.content_id = ct.content_id
            WHERE ct.channel_id = %s
        """, (channel_id,))
        total_fans = cur.fetchone()[0]

        # 2개 이상 영상에 댓글 단 계정 수 (중복률 분자)
        cur.execute("""
            SELECT COUNT(*) FROM (
                SELECT c.fan_id
                FROM comments c
                JOIN contents ct ON c.content_id = ct.content_id
                WHERE ct.channel_id = %s
                GROUP BY c.fan_id
                HAVING COUNT(DISTINCT c.content_id) >= 2
            ) t
        """, (channel_id,))
        repeat_fans = cur.fetchone()[0]

        # 절반 이상 영상에 댓글 단 계정 수 (고정 댓글러)
        half = video_count / 2
        cur.execute("""
            SELECT COUNT(*) FROM (
                SELECT c.fan_id
                FROM comments c
                JOIN contents ct ON c.content_id = ct.content_id
                WHERE ct.channel_id = %s
                GROUP BY c.fan_id
                HAVING COUNT(DISTINCT c.content_id) >= %s
            ) t
        """, (channel_id, half))
        regular_commenters = cur.fetchone()[0]

        # 평균 댓글 길이
        cur.execute("""
            SELECT AVG(CHAR_LENGTH(c.comment_text))
            FROM comments c
            JOIN contents ct ON c.content_id = ct.content_id
            WHERE ct.channel_id = %s AND c.comment_text IS NOT NULL
        """, (channel_id,))
        avg_len = cur.fetchone()[0] or 0

        # 중복률
        overlap_rate = (repeat_fans / total_fans * 100) if total_fans else 0

        # Loyalty Score = (평균댓글수 × 10 + 평균좋아요 × 1) / 평균조회수
        # 평균댓글수, 평균좋아요, 평균조회수 (L2-b 스냅샷에서)
        cur.execute("""
            SELECT AVG(s.comment_count), AVG(s.like_count), AVG(s.view_count)
            FROM content_snapshots s
            JOIN contents ct ON s.content_id = ct.content_id
            WHERE ct.channel_id = %s AND s.like_count IS NOT NULL
        """, (channel_id,))
        avg_comment, avg_like, avg_view = cur.fetchone()
        if avg_view and avg_view > 0:
            loyalty = (float(avg_comment or 0) * 10 + float(avg_like or 0) * 1) / float(avg_view)
        else:
            loyalty = None

        # channel_metrics 업데이트 (기존 행에 L3 지표 추가)
        cur.execute("""
        UPDATE channel_metrics
        SET
            commenter_overlap_rate=%s,
            regular_commenter_count=%s,
            avg_comment_length=%s,
            loyalty_score=%s,
            calculated_at=%s
        WHERE channel_id=%s
        """,
        (
            round(float(overlap_rate),2),
            int(regular_commenters),
            round(float(avg_len),1),
            round(float(loyalty),4) if loyalty is not None else None,
            datetime.now(),
            channel_id
        ))

        print(f"{nickname}: 영상{video_count} 팬{total_fans} "
              f"| 중복률{overlap_rate:.1f}% 고정{regular_commenters}명 "
              f"| 평균길이{avg_len:.0f}자 Loyalty{loyalty:.4f}" if loyalty else 
              f"{nickname}: 팬{total_fans} 중복률{overlap_rate:.1f}%")

conn.close()
print("\n완료")