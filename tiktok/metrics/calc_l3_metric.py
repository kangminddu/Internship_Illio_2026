"""
TikTok L3 파생지표 계산 (댓글 기반)

유튜브 calc_l3_metric.py를 TikTok에 맞게 정리:
  - 댓글 로직은 플랫폼 무관 (fan_id, content_id로만 계산) → 거의 그대로
  - 대상 채널 쿼리만 수정 (tiktok + active/low_active)

계산 지표 (9개 중 L3 담당분):
  6. commenter_overlap_rate   = 2개+ 영상에 댓글 단 고유계정 / 전체 고유계정 * 100
  7. regular_commenter_count  = 절반 이상 영상에 댓글 단 고유계정 수
  8. avg_comment_length       = 전체 댓글 텍스트 길이 합 / 댓글 수

주의: calc_metric.py(L2 지표)가 먼저 실행되어 channel_metrics row가 있어야 함.
      이 스크립트는 기존 row에 UPDATE로 L3 지표만 얹음 (INSERT 안 함).
      row 없는 채널(L2 지표 계산 안 된 채널)은 UPDATE 0건 → skip.
재계산 가능: UPDATE만 하므로 언제든 다시 실행 가능.
"""

import warnings
warnings.filterwarnings("ignore", category=UserWarning)

import pymysql

from tiktok import config
DB = config.DB


conn = pymysql.connect(**DB, autocommit=True)

with conn.cursor() as cur:

    # L3 댓글이 수집된 tiktok 채널 (active/low_active만)
    cur.execute("""
        SELECT DISTINCT ch.channel_id, cr.nickname
        FROM comments c
        JOIN contents ct
            ON c.content_id = ct.content_id
           AND ct.content_type = 'tiktok'
        JOIN channels ch
            ON ct.channel_id = ch.channel_id
        JOIN creators cr
            ON ch.creator_id = cr.creator_id
        WHERE ch.platform = 'tiktok'
          AND ch.channel_activity_status IN ('active', 'low_active')
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
            JOIN contents ct ON c.content_id = ct.content_id
            WHERE ct.channel_id = %s
              AND ct.content_type = 'tiktok'
        """, (channel_id,))
        content_count = cur.fetchone()[0]

        if content_count == 0:
            skipped += 1
            continue

        # -------------------------
        # 전체 고유 댓글 작성자 수
        # -------------------------
        cur.execute("""
            SELECT COUNT(DISTINCT c.fan_id)
            FROM comments c
            JOIN contents ct ON c.content_id = ct.content_id
            WHERE ct.channel_id = %s
              AND ct.content_type = 'tiktok'
        """, (channel_id,))
        total_fans = cur.fetchone()[0]

        # -------------------------
        # 2개 이상 영상에 댓글 단 고유 계정 (중복 팬)
        # -------------------------
        cur.execute("""
            SELECT COUNT(*)
            FROM (
                SELECT c.fan_id
                FROM comments c
                JOIN contents ct ON c.content_id = ct.content_id
                WHERE ct.channel_id = %s
                  AND ct.content_type = 'tiktok'
                GROUP BY c.fan_id
                HAVING COUNT(DISTINCT c.content_id) >= 2
            ) t
        """, (channel_id,))
        repeat_fans = cur.fetchone()[0]

        # -------------------------
        # 절반 이상 영상에 댓글 단 고유 계정 (고정 댓글러)
        # -------------------------
        half = content_count / 2
        cur.execute("""
            SELECT COUNT(*)
            FROM (
                SELECT c.fan_id
                FROM comments c
                JOIN contents ct ON c.content_id = ct.content_id
                WHERE ct.channel_id = %s
                  AND ct.content_type = 'tiktok'
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
            JOIN contents ct ON c.content_id = ct.content_id
            WHERE ct.channel_id = %s
              AND ct.content_type = 'tiktok'
              AND c.comment_text IS NOT NULL
        """, (channel_id,))
        avg_len = cur.fetchone()[0] or 0

        # -------------------------
        # 댓글 작성자 중복률
        # -------------------------
        overlap_rate = (repeat_fans / total_fans * 100) if total_fans else 0

        # -------------------------
        # channel_metrics 업데이트 (기존 row에만 L3 얹기)
        #   calc_metric이 만든 조회수 지표는 안 건드림.
        #   row 없는 채널(calc_metric 대상 아님)은 UPDATE 0건 → skip.
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
            print(f"(skip) {nickname} ch={channel_id} — channel_metrics row 없음")
            continue

        updated += 1
        print(
            f"{nickname}"
            f" | 콘텐츠 {content_count}개"
            f" | 팬 {total_fans}명"
            f" | 중복팬 {repeat_fans}명"
            f" | 중복률 {overlap_rate:.1f}%"
            f" | 고정댓글러 {regular_commenters}명"
            f" | 평균길이 {avg_len:.1f}자"
        )

conn.close()
print(f"\n완료 | 업데이트 {updated}개 | 스킵 {skipped}개")