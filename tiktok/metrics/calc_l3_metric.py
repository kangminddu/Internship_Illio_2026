"""
TikTok L3 파생지표 계산 (댓글 기반)

유튜브 calc_l3_metric.py를 TikTok에 맞게 정리:
  - 댓글 로직은 플랫폼 무관 (fan_id, content_id로만 계산) → 거의 그대로
  - 대상 채널 쿼리만 수정 (tiktok + active/low_active)

★ 유튜브 버전보다 나은 점 두 가지
------
  ① 대상 쿼리에 platform 필터가 있다.
     유튜브 calc_l3_metrics는 WHERE ch.platform='youtube'가 빠져 있어
     인스타/틱톡 채널까지 대상에 들어올 수 있다.
  ② content_type='tiktok' 조인 조건이 모든 쿼리에 있다.
     같은 채널에 다른 플랫폼 콘텐츠가 섞이지 않는다.

계산 지표 (9개 중 L3 담당분):
  6. commenter_overlap_rate   = 2개+ 영상에 댓글 단 고유계정 / 전체 고유계정 * 100
  7. regular_commenter_count  = 절반 이상 영상에 댓글 단 고유계정 수
  8. avg_comment_length       = 전체 댓글 텍스트 길이 합 / 댓글 수

가이드라인 근거: '팬덤 깊이 측정' 원칙.
"같은 수치라도 '다수의 1회성 참여'와 '소수의 반복 참여'를 구분하여
 코어 팬덤의 실질 모수를 파악한다."

주의: calc_metric.py(L2 지표)가 먼저 실행되어 channel_metrics row가 있어야 함.
      이 스크립트는 기존 row에 UPDATE로 L3 지표만 얹음 (INSERT 안 함).
      row 없는 채널(L2 지표 계산 안 된 채널)은 UPDATE 0건 → skip.

      ← 이 설계 덕분에 대상 조건을 여기서 다시 쓸 필요가 없다.
        "행이 있으면 얹고 없으면 넘긴다" → calc_metric과 조건이 어긋날 여지가 없다.

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
    #
    # comments에서 역으로 올라온다. crawl_logs를 안 보는 이유:
    # "L3가 성공했다"보다 "댓글이 실제로 있다"가 계산 가능 조건이다.
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
        # 주의: '이 채널의 전체 영상 수'가 아니라
        #       '댓글이 실제로 수집된 영상 수'다.
        #       L3가 일부만 성공하면 이 값이 작아지고,
        #       아래 '절반 이상' 기준도 함께 작아진다.
        cur.execute("""
            SELECT COUNT(DISTINCT c.content_id)
            FROM comments c
            JOIN contents ct ON c.content_id = ct.content_id
            WHERE ct.channel_id = %s
              AND ct.content_type = 'tiktok'
        """, (channel_id,))
        content_count = cur.fetchone()[0]

        # 0이면 아래 나눗셈에서 문제가 되므로 미리 걸러낸다.
        # (유튜브 버전에는 이 가드가 없다)
        if content_count == 0:
            skipped += 1
            continue

        # -------------------------
        # 전체 고유 댓글 작성자 수
        # -------------------------
        # fan_id 기준. 같은 사람이 댓글 10개를 달아도 1명으로 센다.
        # (L3의 fans 테이블이 external_author_id로 동일인을 식별해준 덕분)
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
        # 중복률의 분자. "이 채널을 계속 보는 사람"의 최소 기준.
        #
        # COUNT(DISTINCT c.content_id)를 쓰는 이유:
        # 한 영상에 댓글 5개를 달아도 '1개 영상'으로 센다.
        # 반복 참여는 '여러 영상에 걸친' 것이어야 의미가 있다.
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
        # 가이드라인: "수집 영상 중 절반 이상에 댓글을 남긴 고유 계정 수"
        #
        # ⚠️ 표본이 작으면 지표가 무의미해진다.
        #    content_count=1 → half=0.5 → 1회 댓글자가 전부 '고정 댓글러'
        #    content_count=2 → half=1.0 → 마찬가지
        #    즉 L3 수집이 부실할수록 이 지표가 좋아 보인다.
        #    가이드라인에 표본 하한 규정이 없어 그대로 두고 있다.
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
        # CHAR_LENGTH를 쓴다(LENGTH 아님).
        # LENGTH는 바이트 수라 한글 1자가 3, 이모지가 4로 세어진다.
        # '몰입도'를 재려면 글자 수여야 한다.
        #
        # 틱톡은 이모지 단답 댓글이 특히 많아서, 이 지표로
        # "🔥"만 다는 팬덤과 문장을 쓰는 팬덤을 구분할 수 있다.
        cur.execute("""
            SELECT AVG(CHAR_LENGTH(c.comment_text))
            FROM comments c
            JOIN contents ct ON c.content_id = ct.content_id
            WHERE ct.channel_id = %s
              AND ct.content_type = 'tiktok'
              AND c.comment_text IS NOT NULL
        """, (channel_id,))
        avg_len = cur.fetchone()[0] or 0
        # AVG는 대상 행이 없으면 NULL을 반환한다 → 0으로 방어

        # -------------------------
        # 댓글 작성자 중복률
        # -------------------------
        # 가이드라인: 2개 이상 영상에 댓글 단 계정 ÷ 전체 계정 × 100
        #
        # 높으면 = 같은 사람들이 계속 온다 = 코어 팬덤이 두껍다
        # 낮으면 = 매번 다른 사람이 스쳐간다 = 알고리즘 노출 의존
        #
        # 틱톡은 '추천' 피드 비중이 커서 이 값이 유튜브보다 낮게 나오는
        # 경향이 있다. 플랫폼 간 절대값 비교는 주의해야 한다.
        overlap_rate = (
            repeat_fans / total_fans * 100
            if total_fans else 0
        )

        # -------------------------
        # channel_metrics 업데이트 (기존 row에만 L3 지표 얹기)
        #   INSERT 하지 않음 → calc_metric이 만든 조회수 지표를 절대 안 건드림.
        #   calc_metric 대상이 아닌 채널은 row가 없어 UPDATE 0건 → skip.
        # -------------------------
        # ⚠️ WHERE channel_id=%s만 있다.
        #    channel_metrics를 시계열로 쌓기 시작하면 과거 행까지 전부 덮어쓴다.
        #    (지금은 calc_metric이 매번 DELETE 후 재생성해 채널당 1행뿐이라
        #     동작한다. 유튜브도 같은 구조 — 미해결 과제)
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

        # rowcount로 skip을 판별한다.
        # 대상 조건을 여기서 다시 쓰지 않고 "행이 있으면 얹고, 없으면 넘긴다"로
        # 처리하는 것 — calc_metric과 조건이 어긋날 여지를 없앤다.
        if cur.rowcount == 0:
            skipped += 1
            # skip 사유를 출력한다.
            # "왜 이 채널은 L3 지표가 없지?"를 나중에 추적할 수 있다.
            print(
                f"(skip) {nickname} ch={channel_id}"
                f" — channel_metrics row 없음 (calc_metric 대상 아님)"
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
# ⚠️ main() 함수도 if __name__ 가드도 없다.
#    import하면 바로 실행된다. (crawler는 전부 가드가 있는데 metrics만 다르다)