"""
youtube/metrics/calc_l3_metrics.py — 댓글 기반 지표 산출

무슨 일을 하는가
------
L3가 수집한 comments/fans로 '팬덤 깊이' 지표 3개를 계산한다.

가이드라인 원칙 3: "같은 수치라도 '다수의 1회성 참여'와
'소수의 반복 참여'를 구분하여 코어 팬덤의 실질 모수를 파악한다."

  댓글 작성자 중복률  2개 이상 영상에 댓글 단 계정 ÷ 전체 댓글 계정 × 100
  고정 댓글러 수      수집 영상 중 절반 이상에 댓글을 남긴 계정 수
  평균 댓글 길이      전체 댓글 길이 합 ÷ 댓글 수
                      (이모지 단답 vs 문장형을 구분 → 몰입도 지표)

★ 이 파일은 INSERT를 하지 않는다. UPDATE만 한다.
------
calc_metrics.py가 만든 channel_metrics 행에 세 컬럼을 얹는 방식이다.
그래서 PIPELINE에서 metric 단계가 두 모듈을 순서대로 실행한다:

    "metric": [("youtube.metrics.calc_metrics", False),      ← 행을 만든다
               ("youtube.metrics.calc_l3_metrics", False)]   ← 행에 얹는다

이렇게 나눈 이유:
  - L2 지표와 L3 지표는 데이터 출처도, 수집 시점도 다르다
  - L3만 다시 돌려도 조회수 지표를 건드리지 않는다
  - calc_metrics 대상이 아닌 채널(inactive 등)은 행이 없어
    rowcount 0으로 자연스럽게 skip된다 → 대상 조건을 중복 작성할 필요 없음
"""
import warnings
warnings.filterwarnings("ignore", category=UserWarning)
import pymysql
from datetime import datetime

from youtube.config import DB

conn = pymysql.connect(**DB, autocommit=True)

with conn.cursor() as cur:

    # L3가 수집된 채널
    #
    # comments 테이블에서 역으로 올라온다. crawl_logs를 보지 않는 이유:
    # "L3가 성공했다"보다 "댓글이 실제로 있다"가 계산 가능 조건이기 때문.
    #
    # ⚠️ WHERE ch.platform='youtube'가 없다.
    #    comments에 인스타/틱톡 댓글이 있으면 그 채널도 대상에 들어와
    #    각 플랫폼의 calc_l3_metric이 서로 덮어쓸 수 있다.
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
        # 주의: '이 채널의 전체 영상 수'가 아니라
        #       '댓글이 실제로 수집된 영상 수'다.
        #       L3가 차단당해 일부만 성공하면 이 값이 작아지고,
        #       아래 '절반 이상' 기준도 함께 작아진다.
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
        # fan_id 기준. 같은 사람이 댓글 10개를 달아도 1명으로 센다.
        # (fans 테이블이 external_author_id로 동일인을 식별해준 덕분)
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
        # 가이드라인: "수집 영상 중 절반 이상에 댓글을 남긴 고유 계정 수"
        #
        # ⚠️ 표본이 작으면 지표가 무의미해진다.
        #    content_count=1 → half=0.5 → 1회 댓글자가 전부 '고정 댓글러'
        #    content_count=2 → half=1.0 → 마찬가지
        #    즉 L3가 부실하게 수집된 채널일수록 이 지표가 좋아 보인다.
        #    (가이드라인에 표본 하한 규정이 없어 그대로 두고 있음)
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
        # CHAR_LENGTH를 쓴다(LENGTH 아님).
        # LENGTH는 바이트 수라 한글 1자가 3, 이모지가 4로 세어진다.
        # "몰입도"를 재려면 글자 수여야 한다.
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
        # 가이드라인: 2개 이상 영상에 댓글 단 계정 ÷ 전체 계정 × 100
        #
        # 이 값이 높으면 = 같은 사람들이 계속 온다 = 코어 팬덤이 두껍다
        # 낮으면 = 매번 다른 사람이 스쳐간다 = 알고리즘 노출 의존
        overlap_rate = (
            repeat_fans / total_fans * 100
            if total_fans else 0
        )

        # -------------------------
        # channel_metrics 업데이트 (기존 row에만 L3 얹기)
        #   INSERT 하지 않음 → calc_metrics가 만든 조회수 지표를 절대 안 건드림.
        #   calc_metrics 대상이 아닌 채널(inactive 등)은 row가 없어 UPDATE 0건 → skip.
        # -------------------------
        # ⚠️ WHERE channel_id=%s만 있다.
        #    channel_metrics를 시계열로 쌓기 시작하면 과거 행까지 전부 덮어쓴다.
        #    (지금은 calc_metrics가 매번 DELETE 후 재생성해 1행뿐이라 동작함)
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
        # 처리하는 것 — calc_metrics와 조건이 어긋날 여지를 없앤다.
        if cur.rowcount == 0:
            skipped += 1
            # ★ skip 사유를 출력한다.
            #   calc_metrics는 표본 부족 채널을 조용히 continue해서
            #   "왜 지표가 없지?"를 추적할 수 없는데, 여기는 이유가 남는다.
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