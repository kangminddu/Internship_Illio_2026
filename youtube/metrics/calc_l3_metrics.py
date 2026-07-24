import warnings
warnings.filterwarnings("ignore", category=UserWarning)

import pymysql
from datetime import datetime

from youtube.config import DB

conn = pymysql.connect(**DB, autocommit=True)

with conn.cursor() as cur:

    # L3가 수집된 채널
    cur.execute("""
        SELECT DISTINCT ch.channel_id, cr.nickname
        FROM comments c
        JOIN contents ct
            ON c.content_id = ct.content_id
        JOIN channels ch
            ON ct.channel_id = ch.channel_id
        JOIN creators cr
            ON ch.creator_id = cr.creator_id
    """)

    channels = cur.fetchall()

    print(f"L3 지표 계산 대상 : {len(channels)}개\n")

    updated = 0
    skipped = 0

    for channel_id, nickname in channels:

        # -------------------------
        # 댓글 수집된 콘텐츠 수
        # -------------------------
        cur.execute("""
            SELECT COUNT(DISTINCT c.content_id)
            FROM comments c
            JOIN contents ct
                ON c.content_id = ct.content_id
            WHERE ct.channel_id=%s
        """, (channel_id,))

        content_count = cur.fetchone()[0]

        # -------------------------
        # 전체 댓글 작성자 수
        # -------------------------
        cur.execute("""
            SELECT COUNT(DISTINCT c.fan_id)
            FROM comments c
            JOIN contents ct
                ON c.content_id = ct.content_id
            WHERE ct.channel_id=%s
        """, (channel_id,))

        total_fans = cur.fetchone()[0]

        # -------------------------
        # 2개 이상 콘텐츠에 댓글
        # -------------------------
        cur.execute("""
            SELECT COUNT(*)
            FROM (
                SELECT c.fan_id
                FROM comments c
                JOIN contents ct
                    ON c.content_id=ct.content_id
                WHERE ct.channel_id=%s
                GROUP BY c.fan_id
                HAVING COUNT(DISTINCT c.content_id)>=2
            ) t
        """, (channel_id,))

        repeat_fans = cur.fetchone()[0]

        # -------------------------
        # 절반 이상 콘텐츠 댓글러
        # -------------------------
        half = content_count / 2

        cur.execute("""
            SELECT COUNT(*)
            FROM (
                SELECT c.fan_id
                FROM comments c
                JOIN contents ct
                    ON c.content_id=ct.content_id
                WHERE ct.channel_id=%s
                GROUP BY c.fan_id
                HAVING COUNT(DISTINCT c.content_id) >= %s
            ) t
        """, (channel_id, half))

        regular_commenters = cur.fetchone()[0]

        # -------------------------
        # 평균 댓글 길이
        # -------------------------
        cur.execute("""
            SELECT AVG(CHAR_LENGTH(c.comment_text))
            FROM comments c
            JOIN contents ct
                ON c.content_id=ct.content_id
            WHERE ct.channel_id=%s
              AND c.comment_text IS NOT NULL
        """, (channel_id,))

        avg_len = cur.fetchone()[0] or 0

        # -------------------------
        # 댓글 작성자 중복률
        # -------------------------
        overlap_rate = (
            repeat_fans / total_fans * 100
            if total_fans else 0
        )

        # -------------------------
        # channel_metrics 업데이트 (기존 row에만 L3 얹기)
        #   INSERT 하지 않음 → calc_metrics가 만든 조회수 지표를 절대 안 건드림.
        #   calc_metrics 대상이 아닌 채널(inactive 등)은 row가 없어 UPDATE 0건 → skip.
        # -------------------------
        cur.execute("""
            UPDATE channel_metrics
            SET commenter_overlap_rate = %s,
                regular_commenter_count = %s,
                avg_comment_length = %s
            WHERE channel_id = %s
        """, (
            round(float(overlap_rate), 2),
            int(regular_commenters),
            round(float(avg_len), 1),
            channel_id,
        ))

        if cur.rowcount == 0:
            skipped += 1
            print(
                f"(skip) {nickname} ch={channel_id}"
                f" — channel_metrics row 없음 (calc_metrics 대상 아님)"
            )
            continue

        updated += 1
        print(
            f"{nickname}"
            f" | 콘텐츠 {content_count}개"
            f" | 팬 {total_fans}명"
            f" | 중복팬 {repeat_fans}명"
            f" | 중복률 {overlap_rate:.1f}%"
            f" | 고정댓글러 {regular_commenters}명"
            f" | 평균댓글길이 {avg_len:.1f}자"
        )

conn.close()

print(f"\n완료 | 업데이트 {updated}개 | 스킵 {skipped}개")