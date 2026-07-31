"""
instagram/metric/calc_l3_metric.py — 댓글 기반 지표

가이드라인 '팬덤 깊이 측정' 대응:
  "같은 수치라도 '다수의 1회성 참여'와 '소수의 반복 참여'를 구분하여
   코어 팬덤의 실질 모수를 파악한다."

  댓글 작성자 중복률  2개+ 콘텐츠에 댓글 단 계정 ÷ 전체 계정 × 100
  고정 댓글러 수      절반 이상 콘텐츠에 댓글 단 계정 수
  평균 댓글 길이      전체 길이 합 ÷ 댓글 수

★ 세 플랫폼의 같은 파일 중 이게 가장 정확하다.
------
                        표본 하한   platform 필터   l3_content_count 저장
  유튜브 calc_l3_metrics    ❌            ❌                ❌
  틱톡   calc_l3_metric     ❌            ✅                ❌
  인스타 calc_l3_metric     ✅            ✅                ✅

  가장 나중에 만들면서 앞선 두 개의 문제를 반영했다.
  다만 역전파는 안 됐다 — 유튜브에는 여전히 없다.

INSERT 없이 UPDATE만 한다.
calc_metric.py가 만든 channel_metrics 행에 컬럼 4개를 얹는 방식.
→ 대상 조건을 여기서 다시 쓸 필요가 없다.
  행이 있으면 얹고, 없으면 rowcount 0으로 넘어간다.
"""
import warnings
warnings.filterwarnings("ignore", category=UserWarning)

import pymysql

try:
    from instagram.config import DB
except Exception:
    from config import DB


# ⚠️ L3 는 전체 게시물이 아니라 '댓글 1개 이상 + 1년 이내' 만 수집한다.
#    (17,048건 중 6,972건). 따라서 아래 지표의 분모인 content_count 는
#    '댓글이 수집된 콘텐츠 수'이지 '채널의 전체 게시물 수'가 아니다.
#    콘텐츠가 너무 적으면 '고정 댓글러' 판정이 무의미해지므로 하한을 둔다.
#
# ★ 이 주석이 이 파일의 핵심이다.
#
#   L3 대상을 좁힌 이유: 인스타는 계정당 8~15초라 전 게시물을 돌 수 없다.
#   댓글이 0개인 게시물은 방문해도 얻을 게 없고,
#   1년 넘은 게시물은 팬덤 분석에 의미가 적다.
#   → 17,048건 중 6,972건(41%)만 수집한다.
#
#   그 결과 content_count가 채널마다 크게 다르다.
#   어떤 채널은 10개, 어떤 채널은 2개다.
#   2개짜리 채널에서 "절반 이상"은 1개고, 한 번 댓글 단 사람이
#   전부 '고정 댓글러'가 된다. → 수집이 부실할수록 지표가 좋아 보인다.
#
#   하한 3을 둬서 그 왜곡을 막는다.
#   (유튜브·틱톡에는 이 하한이 없다. 가이드라인에 규정이 없어서인데,
#    여기서는 실제 데이터를 보고 필요하다고 판단해 넣었다)
MIN_L3_CONTENTS = 3

conn = pymysql.connect(**DB, autocommit=True)

with conn.cursor() as cur:

    # ------------------------------------------------------------
    # Instagram에서 댓글이 수집된 채널
    # ------------------------------------------------------------
    # comments에서 역으로 올라온다. crawl_logs를 안 보는 이유:
    # "L3가 성공했다"보다 "댓글이 실제로 있다"가 계산 가능 조건이다.
    #
    # ★ WHERE ch.platform='instagram'이 있다.
    #   유튜브 calc_l3_metrics에는 이 필터가 없어서, 인스타/틱톡 채널까지
    #   대상에 들어와 서로 덮어쓸 수 있다.
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

    print(f"L3 지표 계산 대상 : {len(channels)}개")
    print(f"최소 콘텐츠 수 : {MIN_L3_CONTENTS}\n")

    updated = 0
    skipped = 0
    too_few = 0     # 표본 부족으로 걸러낸 수. 별도 집계해서 드러낸다.

    for channel_id, nickname in channels:

        # --------------------------------------------------------
        # 댓글이 존재하는 콘텐츠 수
        # --------------------------------------------------------
        # 이 값이 아래 '절반 이상' 기준의 분모가 된다.
        cur.execute("""
            SELECT COUNT(DISTINCT c.content_id)
            FROM comments c
            JOIN contents ct
                ON c.content_id = ct.content_id
            WHERE ct.channel_id=%s
        """, (channel_id,))

        content_count = cur.fetchone()[0]

        # 콘텐츠 1~2개로는 중복률/고정댓글러가 통계적으로 무의미하다.
        # (2개 중 1개에만 댓글 달아도 '절반 이상' 이 되어버린다)
        if content_count < MIN_L3_CONTENTS:
            too_few += 1
            continue
            # 지표를 0으로 넣지 않고 아예 UPDATE를 안 한다.
            # → channel_metrics의 해당 컬럼이 NULL로 남는다.
            #   "값이 0"과 "측정 불가"를 구분하는 것.

        # --------------------------------------------------------
        # 전체 댓글 작성자 수
        # --------------------------------------------------------
        # fan_id 기준. 같은 사람이 댓글 10개를 달아도 1명으로 센다.
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
        # COUNT(DISTINCT c.content_id)를 쓰는 이유:
        # 한 게시물에 댓글 5개를 달아도 '1개 콘텐츠'로 센다.
        # 반복 참여는 '여러 콘텐츠에 걸친' 것이어야 의미가 있다.
        # (한 게시물에 연속으로 여러 댓글 다는 건 흔한 일이다)
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
        # 위 MIN_L3_CONTENTS 가드 덕분에 half >= 1.5가 보장된다.
        # (content_count가 3 이상이므로)
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
        # CHAR_LENGTH를 쓴다(LENGTH 아님).
        # LENGTH는 바이트 수라 한글 1자가 3, 이모지가 4로 세어진다.
        # '몰입도'를 재려면 글자 수여야 한다.
        # (인스타 댓글은 이모지만 있는 경우가 특히 많다)
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
        # 높으면 = 같은 사람들이 계속 온다 = 코어 팬덤이 두껍다
        # 낮으면 = 매번 다른 사람이 스쳐간다 = 알고리즘 노출 의존
        commenter_overlap_rate = (
            repeat_commenters / total_commenters * 100
            if total_commenters else 0
        )

        # --------------------------------------------------------
        # 기존 channel_metrics 업데이트
        # --------------------------------------------------------
        # ★ l3_content_count를 함께 저장한다.
        #   유튜브·틱톡에는 없는 컬럼이다.
        #
        #   왜 필요한가: "고정 댓글러 5명"이 콘텐츠 10개 기준인지
        #   3개 기준인지에 따라 의미가 완전히 다르다.
        #   지표만 있고 표본이 없으면 신뢰도를 판단할 수 없다.
        #   (calc_metric의 *_sample 컬럼과 같은 발상)
        cur.execute("""
            UPDATE channel_metrics
            SET
                commenter_overlap_rate = %s,
                regular_commenter_count = %s,
                avg_comment_length = %s,
                l3_content_count = %s
            WHERE channel_id=%s
        """, (
            round(commenter_overlap_rate, 2),
            regular_commenters,
            round(float(avg_comment_length), 1),
            content_count,
            channel_id
        ))
        # ⚠️ WHERE channel_id=%s만 있다.
        #    channel_metrics를 시계열로 쌓기 시작하면 과거 행까지
        #    전부 덮어쓴다. (지금은 calc_metric이 DELETE 후 재생성해
        #    채널당 1행뿐이라 동작한다 — 세 플랫폼 공통 문제)

        # rowcount로 skip을 판별한다.
        # 대상 조건을 여기서 다시 쓰지 않고 "행이 있으면 얹고,
        # 없으면 넘긴다"로 처리 — calc_metric과 조건이 어긋날 여지를 없앤다.
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

# 세 가지를 구분해서 집계한다.
#   updated : 정상 계산
#   skipped : channel_metrics 행이 없음 (calc_metric 대상 아님)
#   too_few : 표본 부족
# 왜 계산이 안 됐는지 사유가 갈려야 나중에 추적할 수 있다.
print(
    f"\n완료"
    f" | 업데이트 {updated}개"
    f" | 스킵 {skipped}개"
    f" | 콘텐츠부족 {too_few}개"
)
# ⚠️ main() 함수도 if __name__ 가드도 없다.
#    import하면 바로 실행된다. (세 플랫폼 metrics 공통)