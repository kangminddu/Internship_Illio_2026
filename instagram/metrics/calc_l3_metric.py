import warnings
warnings.filterwarnings("ignore", category=UserWarning)

import pymysql

from config import DB

conn = pymysql.connect(**DB, autocommit=True)

with conn.cursor() as cur:

    # ------------------------------------------------------------
    # Instagram에서 댓글이 수집된 채널
    # ------------------------------------------------------------
    cur.execute("""
        SELECT DISTINCT
            ch.channel_id,
            cr.nickname
        FROM comments c
        JOIN contents ct
            ON c.content_id = ct.content_id
        JOIN channels ch
            ON ct.channel_id = ch.channel_id
        JOIN creators cr
            ON ch.creator_id = cr.creator_id
        WHERE ch.platform='instagram'
    """)

    channels = cur.fetchall()

    print(f"L3 지표 계산 대상 : {len(channels)}개\n")

    updated = 0
    skipped = 0

    for channel_id, nickname in channels:

        # --------------------------------------------------------
        # 댓글이 존재하는 콘텐츠 수
        # --------------------------------------------------------
        cur.execute("""
            SELECT COUNT(DISTINCT c.content_id)
            FROM comments c
            JOIN contents ct
                ON c.content_id = ct.content_id
            WHERE ct.channel_id=%s
        """, (channel_id,))

        content_count = cur.fetchone()[0]

        if content_count == 0:
            skipped += 1
            continue

        # --------------------------------------------------------
        # 전체 댓글 작성자 수
        # --------------------------------------------------------
        cur.execute("""
            SELECT COUNT(DISTINCT fan_id)
            FROM comments c
            JOIN contents ct
                ON c.content_id = ct.content_id
            WHERE ct.channel_id=%s
        """, (channel_id,))

        total_commenters = cur.fetchone()[0]

        # --------------------------------------------------------
        # 2개 이상 콘텐츠에 댓글 남긴 팬
        # --------------------------------------------------------
        cur.execute("""
            SELECT COUNT(*)
            FROM (
                SELECT c.fan_id
                FROM comments c
                JOIN contents ct
                    ON c.content_id = ct.content_id
                WHERE ct.channel_id=%s
                GROUP BY c.fan_id
                HAVING COUNT(DISTINCT c.content_id) >= 2
            ) t
        """, (channel_id,))

        repeat_commenters = cur.fetchone()[0]

        # --------------------------------------------------------
        # 절반 이상 콘텐츠에 댓글 남긴 팬
        # --------------------------------------------------------
        half = content_count / 2

        cur.execute("""
            SELECT COUNT(*)
            FROM (
                SELECT c.fan_id
                FROM comments c
                JOIN contents ct
                    ON c.content_id = ct.content_id
                WHERE ct.channel_id=%s
                GROUP BY c.fan_id
                HAVING COUNT(DISTINCT c.content_id) >= %s
            ) t
        """, (channel_id, half))

        regular_commenters = cur.fetchone()[0]

        # --------------------------------------------------------
        # 평균 댓글 길이
        # --------------------------------------------------------
        cur.execute("""
            SELECT AVG(CHAR_LENGTH(comment_text))
            FROM comments c
            JOIN contents ct
                ON c.content_id = ct.content_id
            WHERE ct.channel_id=%s
              AND comment_text IS NOT NULL
        """, (channel_id,))

        avg_comment_length = cur.fetchone()[0] or 0

        # --------------------------------------------------------
        # 댓글 작성자 중복률
        # --------------------------------------------------------
        commenter_overlap_rate = (
            repeat_commenters / total_commenters * 100
            if total_commenters else 0
        )

        # --------------------------------------------------------
        # 기존 channel_metrics 업데이트
        # --------------------------------------------------------
        cur.execute("""
            UPDATE channel_metrics
            SET
                commenter_overlap_rate = %s,
                regular_commenter_count = %s,
                avg_comment_length = %s
            WHERE channel_id=%s
        """, (
            round(commenter_overlap_rate, 2),
            regular_commenters,
            round(float(avg_comment_length), 1),
            channel_id
        ))

        if cur.rowcount == 0:
            skipped += 1
            print(
                f"(skip) {nickname}"
                f" | channel_id={channel_id}"
                f" | channel_metrics 없음"
            )
            continue

        updated += 1

        print(
            f"{nickname}"
            f" | 콘텐츠 {content_count}개"
            f" | 댓글러 {total_commenters}명"
            f" | 중복댓글러 {repeat_commenters}명"
            f" | 중복률 {commenter_overlap_rate:.1f}%"
            f" | 고정댓글러 {regular_commenters}명"
            f" | 평균댓글길이 {avg_comment_length:.1f}자"
        )

conn.close()

print(
    f"\n완료"
    f" | 업데이트 {updated}개"
    f" | 스킵 {skipped}개"
)