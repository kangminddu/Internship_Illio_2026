# instagram/metric/calc_metric.py
"""
Instagram 파생지표 산출 (L2 기반)

★ 이 파일은 세 플랫폼 중 유일하게 '활동성 판정'을 함께 한다.
------
    유튜브 : L2a에서 잠정 판정 → backfill이 확정
    틱톡   : L2에서 수집하면서 확정
    인스타 : 여기, 지표 계산 직전에 판정          ← 임시 위치

  원래 있어야 할 자리가 아니다. 지표 계산 대상을 고르려면
  활동성이 필요한데 그걸 매기는 단계가 없어서 여기 넣었다.
  → 세 플랫폼의 판정 시점이 전부 다르다. 리뷰 안건.

  부작용: L2 이후 새로 들어온 채널은 calc_metric을 다시 돌려야
  활동성이 찍힌다. 실제로 unknown 상태로 남은 채널이 269개 있다.

인스타 고유 제약 두 가지
------
  ① 조회수가 없다.
     피드/캐러셀에는 조회수 자체를 제공하지 않는다. 릴스만 play_count가 있다.
     → ER 분모를 팔로워로 잡을 수밖에 없다 (ER_BASIS 참고).
       가이드라인도 Instagram만 '팔로워 수' 분모로 규정한다.

  ② 콘텐츠 유형이 셋이다.
     feed_image / carousel / reels
     DB 컬럼은 유튜브 시절 이름(longform/shorts)을 그대로 쓴다.
       longform = 피드 + 캐러셀
       shorts   = 릴스
"""
import warnings
warnings.filterwarnings("ignore", category=UserWarning)

import pymysql
from datetime import datetime
from dateutil.relativedelta import relativedelta
# relativedelta: timedelta(days=90)은 '3개월'이 아니다.
# 달마다 일수가 달라서 months=3으로 정확히 계산해야 한다.

try:
    from instagram.config import DB
except Exception:
    from config import DB


# =========================================================
# 설정
# =========================================================

TRIM_RATIO = 0.10    # 상하위 10% 절사 (가이드라인)
MIN_SAMPLE = 10      # 이보다 표본이 적으면 지표를 내지 않는다

# ER / Loyalty 분모 기준
#   "follower" : (좋아요+댓글) / 팔로워        ← 인스타 표준, 기본값
#   "view"     : (좋아요+댓글) / 조회수        ← 유튜브 방식
#
# ⚠️ 인스타는 피드/캐러셀에 조회수를 아예 제공하지 않는다(릴스만 play_count).
#    "view" 로 두면 피드 관련 ER 이 전부 NULL 이 되고, 릴스 ER 과
#    분모가 달라 나란히 비교할 수도 없다. 그래서 follower 로 통일한다.
#
# ★ 이 주석이 이 파일의 핵심 판단이다.
#
#   가이드라인도 같은 결론을 내렸다:
#     YouTube  ER 분모 = 평균 조회수
#     TikTok   ER 분모 = 평균 조회수
#     Instagram ER 분모 = 팔로워 수     ← 조회수가 없어서
#
#   상수로 빼둔 이유: 나중에 인스타가 조회수를 공개하거나
#   릴스만 별도 산출하기로 하면 이 값만 바꾸면 된다.
#   그리고 aggregation_method 컬럼에 기록되므로,
#   기준이 바뀐 전후 데이터를 구분할 수 있다.
ER_BASIS = "follower"

conn = pymysql.connect(**DB, autocommit=True)


# =========================================================
# 공통 함수
# =========================================================

def trimmed_mean(values, trim_ratio=TRIM_RATIO):
    """상하위 10% 절사 평균. 표본이 MIN_SAMPLE 미만이면 안 자른다.

    가이드라인: "평균값 (상·하위 10% 이상치 제거 후)"

    왜 절사하나: 인스타도 바이럴 편차가 크다. 100만 좋아요 게시물
    하나가 나머지의 평균을 통째로 왜곡한다.

    None을 걸러내는 게 중요하다. 특히 인스타는 피드에 view_count가
    아예 없어서 None이 대량으로 들어온다. 0으로 치면 평균이 붕괴한다.
    → '값이 0'과 '값이 없음'은 다르다.
    """
    vals = [float(v) for v in values if v is not None]

    if not vals:
        return None      # 0이 아니라 None. 호출자가 '표본 없음'을 알 수 있게.

    vals.sort()

    n = len(vals)

    if n >= MIN_SAMPLE:
        k = int(n * trim_ratio)

        if n > 2 * k:    # 잘라내고도 남는 게 있을 때만
            vals = vals[k:n-k]

    return sum(vals) / len(vals)


# fetch_rows가 반환하는 행 구조:
#   r[0] content_id  r[1] content_type  r[2] is_paid_promotion
#   r[3] view_count  r[4] like_count    r[5] comment_count
# 아래 헬퍼들이 이 인덱스를 공유한다.

def avg_view(rows):
    """
    ⚠️ 릴스만 값이 있다(play_count).
       피드/캐러셀은 인스타가 조회수를 제공하지 않아 항상 None.

    → longform(피드+캐러셀)에 이 함수를 쓰면 언제나 None이 나온다.
      그래도 호출하는 이유는 컬럼 구조를 세 플랫폼이 공유하기 때문.
      엑셀에서 'N/A'로 표시되어 "조회수가 없는 게 정상"임을 보여준다.
    """
    return trimmed_mean([r[3] for r in rows if r[3] is not None])


def avg_like(rows):
    return trimmed_mean([r[4] for r in rows if r[4] is not None])


def avg_comment(rows):
    return trimmed_mean([r[5] for r in rows if r[5] is not None])


def engagement_rate(rows, follower=None):
    """
    ER = (평균좋아요 + 평균댓글) / 분모 * 100

    분모는 ER_BASIS 에 따라 팔로워 또는 조회수.
    follower 가 없거나 0 이면 조회수로 폴백하고, 그것도 없으면 None.

    ★ 3단 폴백 구조:
        ① ER_BASIS='follower'이고 팔로워가 있다 → 팔로워 분모 (정상 경로)
        ② 팔로워가 없다 → 조회수로 시도 (릴스만 가능)
        ③ 그것도 없는데 팔로워는 있다 → 팔로워 (①과 중복이지만
                                        ER_BASIS='view'일 때의 폴백)
        ④ 전부 없다 → None

      ②③이 있는 이유: L1이 실패해서 팔로워를 못 받은 채널도
      릴스 조회수는 있을 수 있다. 지표를 아예 못 내는 것보다
      가능한 것으로라도 내되, aggregation_method에 기준을 남긴다.

    ⚠️ like/comment를 `or 0`으로 처리한다.
      좋아요는 있는데 댓글이 없는 게시물이 흔해서 이 편이 낫지만,
      둘 다 None일 때도 0으로 계산해 ER이 0%가 된다.
      "참여 0%"와 "참여를 측정 못함"이 구분되지 않는 지점.
      (유튜브 calc_metrics는 둘 다 None이면 지표도 None으로 둔다)
    """
    if not rows:
        return None

    like = avg_like(rows) or 0
    comment = avg_comment(rows) or 0

    if ER_BASIS == "follower" and follower:
        return (like + comment) / follower * 100

    view = avg_view(rows)
    if view:
        return (like + comment) / view * 100

    # follower 기준인데 팔로워를 못 구한 경우의 마지막 폴백
    if follower:
        return (like + comment) / follower * 100

    return None


def loyalty_score(rows, follower=None):
    """
    댓글에 가중치(x10)를 준 충성도 지표. 분모 기준은 ER 과 동일.

    댓글에 10배를 주는 이유(가이드라인):
    좋아요는 클릭 한 번이지만 댓글은 글을 써야 한다.
    같은 1건이어도 참여 깊이가 다르다.

    ⚠️ 여기에 × 100이 붙어 있다.
      가이드라인 원문에는 ×100이 없다:
        Instagram Loyalty = (평균댓글×10 + 평균좋아요×1) ÷ 팔로워 수

      유튜브도 ×100이 붙어 있어 같은 문제를 갖는다.
      틱톡만 ×100 없이 정확하다.
      → 세 플랫폼 중 둘이 가이드라인을 초과했고, 값의 스케일이 달라
        플랫폼 간 직접 비교가 불가능하다. 리뷰에서 결정받을 항목.
    """
    if not rows:
        return None

    like = avg_like(rows) or 0
    comment = avg_comment(rows) or 0

    if ER_BASIS == "follower" and follower:
        return (comment * 10 + like) / follower * 100

    view = avg_view(rows)
    if view:
        return (comment * 10 + like) / view * 100

    if follower:
        return (comment * 10 + like) / follower * 100

    return None


def split_content(rows):
    """
    DB 컬럼명은 유튜브 시절 그대로 longform/shorts 를 쓴다.
      longform = 피드(feed_image) + 캐러셀(carousel)
      shorts   = 릴스(reels)

    ★ 스키마를 세 플랫폼이 공유하기 때문에 생긴 타협이다.

      유튜브 : longform=일반영상, shorts=쇼츠
      틱톡   : 콘텐츠 유형이 하나뿐이라 분리 자체가 없음
      인스타 : longform=피드+캐러셀, shorts=릴스

      컬럼명이 실제 의미와 어긋나 엑셀을 받는 쪽이 혼란스러울 수 있다.
      export가 '피드/릴스'로 라벨을 바꿔 출력해서 완화한다.

      캐러셀(여러 장 묶음)을 피드에 합친 이유:
      둘 다 정지 이미지 기반이고 조회수가 없다는 점이 같다.
      릴스만 성격(동영상, 조회수 있음, 알고리즘 노출 큼)이 다르다.
    """
    longform = [
        r for r in rows
        if r[1] in ("feed_image", "carousel")
    ]

    shorts = [
        r for r in rows
        if r[1] == "reels"
    ]

    return longform, shorts


def split_ad(rows):
    """광고/일반 분리.

    섭외 판단의 핵심 지표다.
    "이 크리에이터는 광고 게시물에서도 성과가 유지되는가?"
    일반 게시물만 잘 나오고 광고에서 급락하면 섭외 가치가 낮다.

    is_paid_promotion은 L2가 GraphQL 응답의 협찬 표시로 판별한다.
    """
    organic = [
        r for r in rows
        if not r[2]
    ]

    ad = [
        r for r in rows
        if r[2]
    ]

    return organic, ad


# =========================================================
# 날짜 기준
# =========================================================

# 자정으로 고정한다. 실행 시각마다 cutoff가 달라지면
# 같은 날 두 번 돌렸을 때 결과가 미묘하게 달라진다.
today = datetime.now().replace(
    hour=0,
    minute=0,
    second=0,
    microsecond=0,
)

cutoff_3m = today - relativedelta(months=3)
cutoff_6m = today - relativedelta(months=6)
# 유튜브처럼 '6개월 미달 시 12개월 확장'을 하지 않는다.
# 인스타는 L2 수집 상한이 계정당 20개(피드10+릴스10)라
# 기간을 늘려도 데이터가 늘지 않는다.


# =========================================================
# Metric 계산
# =========================================================

with conn.cursor() as cur:

    print("기존 instagram channel_metrics 삭제")
    # ★ 유튜브·틱톡과 다르다. '오늘 계산분'만 지운다.
    #
    #     유튜브 : DELETE ... WHERE platform='youtube'        (전체 삭제)
    #     틱톡   : DELETE ... WHERE platform='tiktok'         (전체 삭제)
    #     인스타 : DELETE ... AND DATE(calculated_at)=CURDATE()  ← 오늘만
    #
    #   시계열이 실제로 쌓인다. 어제 계산분이 남아 있어
    #   "지난주 대비 ER 변화"를 볼 수 있다.
    #
    #   문서 5-B에서 UNIQUE KEY(channel_id)를 지운 게 이걸 가능하게 했는데,
    #   그 의도를 실제로 살린 건 세 플랫폼 중 여기뿐이다.
    #
    #   ⚠️ 다만 calc_l3_metric의 UPDATE는 WHERE channel_id만 봐서
    #     과거 행까지 전부 덮어쓴다. 절반만 시계열인 상태.
    cur.execute("""
        DELETE cm
        FROM channel_metrics cm
        JOIN channels ch
            ON cm.channel_id = ch.channel_id
        WHERE ch.platform = 'instagram'
        AND DATE(cm.calculated_at) = CURDATE()
    """)

    # ---------------------------------------------------------
    # 채널별 최신 팔로워 수 (ER 분모)
    # ---------------------------------------------------------
    # 채널마다 SELECT를 날리지 않고 한 번에 dict로 읽는다.
    # ER의 분모라 모든 채널에서 필요하므로, 수백 번 왕복할 이유가 없다.
    #
    # follower_count가 NULL/0인 채널은 dict에서 아예 제외한다(if r[1]).
    # → followers.get()이 None을 반환하고, engagement_rate가
    #   폴백 경로를 타거나 None을 낸다.
    cur.execute("""
        SELECT s.channel_id, s.follower_count
        FROM channel_snapshots s
        JOIN channels ch
            ON ch.channel_id = s.channel_id
        JOIN (
            SELECT channel_id, MAX(captured_at) AS m
            FROM channel_snapshots
            GROUP BY channel_id
        ) t
          ON t.channel_id = s.channel_id
         AND t.m = s.captured_at
        WHERE ch.platform='instagram'
    """)
    followers = {r[0]: r[1] for r in cur.fetchall() if r[1]}
    print(f"팔로워 확보 채널 : {len(followers)}")

    # ---------------------------------------------------------
    # 활동성 판정  ← 인스타에만 여기 있다
    # ---------------------------------------------------------
    print("채널 활동성 판정")
    # ★ SQL 한 방으로 전 채널을 갱신한다.
    #
    #   유튜브·틱톡은 파이썬에서 채널마다 classify_activity()를 부르는데,
    #   여기는 UPDATE ... JOIN (SELECT ... GROUP BY) 하나로 끝낸다.
    #   1,881채널이면 파이썬 루프는 쿼리 수천 번, 이건 한 번이다.
    #
    #   대신 로직이 SQL에 묻혀 있어 재사용이 안 된다.
    #   유튜브는 classify_activity를 backfill_activity.py가 import해서
    #   쓰는데, 여기는 그럴 수 없다.
    #
    #   기준값 90일/180일/15건은 틱톡과 같다.
    #   ⚠️ 가이드라인의 Instagram 기준을 따로 확인하지 않고
    #     틱톡 기준을 빌려 쓴 상태다. 검증이 필요한 부분.
    #     (유튜브는 180일/10건 — 가이드라인이 플랫폼마다 다르게 정했다)
    cur.execute("""
        UPDATE channels ch
        JOIN (
            SELECT channel_id,
                   SUM(published_at >= DATE_SUB(NOW(), INTERVAL 90 DAY))  AS cnt_90d,
                   SUM(published_at >= DATE_SUB(NOW(), INTERVAL 180 DAY)) AS cnt_180d
            FROM contents
            WHERE published_at IS NOT NULL
              AND content_type IN ('feed_image','carousel','reels')
            GROUP BY channel_id
        ) t ON t.channel_id = ch.channel_id
        SET ch.channel_activity_status =
            CASE
              WHEN t.cnt_90d  >= 15 THEN 'active'
              WHEN t.cnt_180d >= 15 THEN 'low_active'
              ELSE 'inactive'
            END
        WHERE ch.platform = 'instagram'
    """)
    # ★ JOIN이라 contents가 없는 채널은 갱신되지 않는다.
    #   → 수집 실패한 채널이 'inactive'로 박제되지 않는다.
    #     (유튜브 backfill이 EXISTS 조건으로 하는 것과 같은 효과를
    #      JOIN이 자연스럽게 만들어준다)
    #     'unknown'으로 남아 다음 실행에 다시 판정된다.
    #
    # ⚠️ dormant가 없다. 가이드라인의 "1년 이상 미업로드 → 수집 제외"
    #    규정은 유튜브에만 구현했다.
    print(f"  활동성 갱신: {cur.rowcount}건")

    # 판정 직후 분포를 출력한다. 따로 SQL을 안 쳐도
    # "지금 몇 개가 지표 대상인지" 바로 알 수 있다.
    cur.execute("""
        SELECT channel_activity_status, COUNT(*)
        FROM channels WHERE platform='instagram'
        GROUP BY channel_activity_status
        """)
    print("     활동성 분포: " + ", ".join(f"{k}={v}" for k, v in cur.fetchall()))

    # 지표 대상: active/low_active + 콘텐츠 보유
    # EXISTS로 콘텐츠 유무를 확인하는 이유: 활동성이 active여도
    # (과거 판정이 남아 있는 등) 실제 콘텐츠가 없으면 계산할 게 없다.
    cur.execute("""
        SELECT c.channel_id
        FROM channels c
        WHERE c.platform='instagram'
        AND c.channel_activity_status IN ('active', 'low_active')
          AND EXISTS (
                SELECT 1
                FROM contents ct
                WHERE ct.channel_id=c.channel_id
          )
    """)

    channel_ids = [r[0] for r in cur.fetchall()]

    print(f"대상 채널 : {len(channel_ids)}")
    print(f"ER 분모 기준 : {ER_BASIS}")


    def fetch_rows(channel_id, cutoff):
        """콘텐츠별 최신 스냅샷 1개 + 기간 필터.

        ★ 유튜브와 다르게 스냅샷을 하나만 본다.

          유튜브는 L2a(조회수만)와 L2b(좋아요 포함)가 나뉘어 있어
          "조회수는 최신, 좋아요는 like가 기록된 최신"으로 분리 조회해야
          ER이 0으로 붕괴하지 않았다.

          인스타 L2는 GraphQL 응답 한 번에 좋아요·댓글수를 다 받으므로
          그런 문제가 없다.

        WHERE의 마지막 조건이 중요하다:
            (s.view_count IS NOT NULL OR s.like_count IS NOT NULL)

          유튜브는 `s.view_count IS NOT NULL`만 본다. 인스타에서 그러면
          피드/캐러셀이 통째로 빠진다(조회수가 아예 없으므로).
          → OR로 바꿔서 "조회수든 좋아요든 하나라도 있으면 포함"한다.
            플랫폼 특성에 맞춰 조건을 고친 사례.
        """
        cur.execute("""
            SELECT
                c.content_id,
                c.content_type,
                c.is_paid_promotion,

                s.view_count,
                COALESCE(s.like_count,0),
                COALESCE(s.comment_count,0)

            FROM contents c

            JOIN content_snapshots s
              ON s.snapshot_id=(
                    SELECT snapshot_id
                    FROM content_snapshots
                    WHERE content_id=c.content_id
                    ORDER BY captured_at DESC,
                             snapshot_id DESC
                    LIMIT 1
              )

            WHERE c.channel_id=%s
            AND c.content_type IN ('feed_image', 'carousel', 'reels')
              AND c.published_at IS NOT NULL
              AND c.published_at >= %s
              AND (
                    s.view_count IS NOT NULL
                    OR s.like_count IS NOT NULL
              )
        """, (channel_id, cutoff))

        return cur.fetchall()


    def count_since(channel_id, cutoff):
        """업로드 수. 스냅샷 조인이 없다 —
        '몇 개 올렸나'는 좋아요 수집 여부와 무관하다."""
        cur.execute("""
            SELECT COUNT(*)

            FROM contents c

            WHERE c.channel_id=%s
            AND c.content_type IN ('feed_image', 'carousel', 'reels')
              AND c.published_at IS NOT NULL
              AND c.published_at >= %s
        """, (channel_id, cutoff))

        return cur.fetchone()[0]


    def upload_freq_weekly(channel_id, cutoff):
        """
        주당 업로드 횟수.

        ⚠️ contents 는 L2 수집 상한(채널당 20~30개)에 잘린 값이다.
           단순히 26주로 나누면 자주 올리는 채널이 과소평가되므로,
           실제 수집된 구간(최신~최오래된 게시물)의 주 수로 나눈다.

        ★ 세 플랫폼 중 여기만 이렇게 계산한다.

          유튜브 : videos_6m / 26      (고정 26주로 나눔)
          틱톡   : videos_3m / 13      (고정 13주로 나눔)
          인스타 : cnt / 실제_수집구간_주수

          왜 다른가:
            하루에 3개씩 올리는 채널이 있다고 하자.
            6개월이면 540개인데 L2는 20개만 가져온다.
            그 20개가 최근 일주일치라면, 26주로 나누면
            "주당 0.77회"가 되어 실제(주당 21회)와 완전히 다르다.

            수집된 게시물의 최초~최신 간격으로 나누면
            "1주 동안 20개 = 주당 20회"가 되어 실제에 가까워진다.

          유튜브·틱톡도 같은 문제가 있는데 인스타에서만 고쳤다.
          (수집 상한이 상대적으로 작아 왜곡이 먼저 드러났다)
        """
        cur.execute("""
            SELECT MIN(published_at), MAX(published_at), COUNT(*)
            FROM contents
            WHERE channel_id=%s
              AND content_type IN ('feed_image','carousel','reels')
              AND published_at IS NOT NULL
              AND published_at >= %s
        """, (channel_id, cutoff))

        mn, mx, cnt = cur.fetchone()
        if not (mn and mx and cnt):
            return 0.0

        span_weeks = (mx - mn).days / 7.0
        # 구간이 1주 미만이면 1주로 본다(0 나눗셈 방지 + 과대추정 억제)
        # ↑ 게시물 20개가 하루에 몰려 있으면 span_weeks가 0이 되어
        #   ZeroDivisionError가 나거나 "주당 무한대"가 된다.
        span_weeks = max(span_weeks, 1.0)
        return round(cnt / span_weeks, 2)


    for channel_id in channel_ids:

        follower = followers.get(channel_id)   # 없으면 None

        # -------------------------------
        # 콘텐츠 개수
        # -------------------------------

        contents_3m = count_since(channel_id, cutoff_3m)
        contents_6m = count_since(channel_id, cutoff_6m)

        # -------------------------------
        # 데이터 조회
        # -------------------------------

        rows_3m = fetch_rows(channel_id, cutoff_3m)
        rows_6m = fetch_rows(channel_id, cutoff_6m)

        sample_count = len(rows_6m)

        # 표본 부족 → skip.
        # ★ 사유를 출력한다. 유튜브 calc_metrics는 조용히 continue해서
        #   "왜 이 채널은 지표가 없지?"를 추적할 수 없다.
        if sample_count < MIN_SAMPLE:
            print(f"(skip) ch={channel_id} sample={sample_count}")
            continue

        # 집계 방식을 기록한다. 나중에 ER_BASIS를 바꾸면
        # "trimmed_mean_6m/follower"와 "trimmed_mean_6m/view"가 섞이는데,
        # 이 값이 있어야 사과와 오렌지를 구분할 수 있다.
        aggregation_method = f"trimmed_mean_6m/{ER_BASIS}"

        # -------------------------------
        # 전체 평균
        # -------------------------------
        # 3개월과 6개월을 따로 낸다. 최근 추세를 보려는 용도.

        avg_view_3m = avg_view(rows_3m)
        avg_like_3m = avg_like(rows_3m)
        avg_comment_3m = avg_comment(rows_3m)

        avg_view_6m = avg_view(rows_6m)
        avg_like_6m = avg_like(rows_6m)
        avg_comment_6m = avg_comment(rows_6m)

        engagement = engagement_rate(rows_6m, follower)
        engagement_3m = engagement_rate(rows_3m, follower)
        loyalty = loyalty_score(rows_6m, follower)

        # -------------------------------
        # 피드 / 릴스
        # -------------------------------
        # 포맷별로 성격이 완전히 다르다.
        # 릴스는 알고리즘 노출이 커서 팔로워 밖 사람도 많이 보고,
        # 피드는 주로 팔로워가 본다. 섞으면 둘 다 왜곡된다.
        # (가이드라인도 "릴스/피드 포맷 분리 후 각각 평균"을 요구한다)

        longform, shorts = split_content(rows_6m)

        longform_avg_view = avg_view(longform)     # ← 항상 None (조회수 없음)
        longform_avg_like = avg_like(longform)
        longform_avg_comment = avg_comment(longform)
        longform_engagement = engagement_rate(longform, follower)

        shorts_avg_view = avg_view(shorts)         # ← 릴스만 값이 있다
        shorts_avg_like = avg_like(shorts)
        shorts_avg_comment = avg_comment(shorts)
        shorts_engagement = engagement_rate(shorts, follower)

        # -------------------------------
        # 광고 / 일반
        # -------------------------------
        # 2×2로 나눈다: (광고/일반) × (피드/릴스)
        # "광고 릴스는 잘 나오는데 광고 피드는 급락한다" 같은
        # 세밀한 판단이 가능해진다.

        organic_rows, ad_rows = split_ad(rows_6m)

        organic_longform, organic_shorts = split_content(organic_rows)
        ad_longform, ad_shorts = split_content(ad_rows)

        # -------------------------------
        # 광고 피드
        # -------------------------------

        ad_longform_avg_view = avg_view(ad_longform)
        ad_longform_avg_like = avg_like(ad_longform)
        ad_longform_avg_comment = avg_comment(ad_longform)
        ad_longform_er = engagement_rate(ad_longform, follower)

        # -------------------------------
        # 일반 피드
        # -------------------------------

        organic_longform_avg_view = avg_view(organic_longform)
        organic_longform_avg_like = avg_like(organic_longform)
        organic_longform_avg_comment = avg_comment(organic_longform)
        organic_longform_er = engagement_rate(organic_longform, follower)

        # -------------------------------
        # 광고 릴스
        # -------------------------------

        ad_shorts_avg_view = avg_view(ad_shorts)
        ad_shorts_avg_like = avg_like(ad_shorts)
        ad_shorts_avg_comment = avg_comment(ad_shorts)
        ad_shorts_er = engagement_rate(ad_shorts, follower)

        # -------------------------------
        # 일반 릴스
        # -------------------------------

        organic_shorts_avg_view = avg_view(organic_shorts)
        organic_shorts_avg_like = avg_like(organic_shorts)
        organic_shorts_avg_comment = avg_comment(organic_shorts)
        organic_shorts_er = engagement_rate(organic_shorts, follower)

        # 표본 수를 함께 저장한다.
        # "광고 릴스 ER 3%"가 릴스 10개 기준인지 1개 기준인지에 따라
        # 신뢰도가 완전히 다르다. 지표만 있고 표본이 없으면
        # 해석할 수가 없다.
        longform_sample = len(longform)
        shorts_sample = len(shorts)

        ad_longform_sample = len(ad_longform)
        normal_longform_sample = len(organic_longform)

        ad_shorts_sample = len(ad_shorts)
        normal_shorts_sample = len(organic_shorts)

        upload_frequency_weekly = upload_freq_weekly(channel_id, cutoff_6m)

        # -------------------------------
        # 조회수/팔로워 비율 (릴스에만 조회수가 있으므로 릴스 기준)
        # -------------------------------
        # ★ VPF(구독자 대비 조회율)를 릴스로만 계산한다.
        #
        #   유튜브·틱톡은 전체 평균 조회수로 계산하는데,
        #   인스타는 피드에 조회수가 없어 릴스밖에 쓸 게 없다.
        #   → 같은 컬럼이지만 의미가 다르다.
        #     "릴스 조회수 ÷ 팔로워"로 읽어야 한다.
        #     export에서 라벨로 구분해주는 게 맞다.
        view_per_follower_ratio = None
        if follower and shorts_avg_view:
            view_per_follower_ratio = shorts_avg_view / follower * 100

        # 컬럼 46개. 광고/일반 × 피드/릴스 조합마다
        # 조회수·좋아요·댓글·ER 4개 + 표본 수를 저장하기 때문.
        cur.execute("""
        INSERT INTO channel_metrics (

            channel_id,
            calculated_at,

            sample_content_count,
            aggregation_method,
            videos_3m,
            videos_6m,

            avg_view_3m,
            avg_like_3m,
            avg_comment_3m,
            engagement_rate_3m,

            avg_view,

            engagement_rate,
            loyalty_score,
            view_per_follower_ratio,

            upload_frequency_weekly,

            longform_avg_view,
            longform_avg_like,
            longform_avg_comment,

            shorts_avg_view,
            shorts_avg_like,
            shorts_avg_comment,

            longform_er,
            shorts_er,

            ad_longform_avg_view,
            ad_longform_avg_like,
            ad_longform_avg_comment,
            ad_longform_er,

            normal_longform_avg_view,
            normal_longform_avg_like,
            normal_longform_avg_comment,
            normal_longform_er,

            ad_shorts_avg_view,
            ad_shorts_avg_like,
            ad_shorts_avg_comment,
            ad_shorts_er,

            normal_shorts_avg_view,
            normal_shorts_avg_like,
            normal_shorts_avg_comment,
            normal_shorts_er,

            longform_sample,
            shorts_sample,

            ad_longform_sample,
            normal_longform_sample,

            ad_shorts_sample,
            normal_shorts_sample
        )
        VALUES (

            %s,%s,

            %s,

            %s,%s,%s,

            %s,%s,%s,%s,

            %s,

            %s,%s,%s,

            %s,

            %s,%s,%s,

            %s,%s,%s,

            %s,%s,

            %s,%s,%s,%s,

            %s,%s,%s,%s,

            %s,%s,%s,%s,

            %s,%s,%s,%s,

            %s,%s,

            %s,%s,

            %s,%s

        )
        """, (
            channel_id,
            today,      # 자정 기준. DATE(calculated_at)=CURDATE() 삭제 조건과 맞춘다.

            sample_count,
            aggregation_method,

            contents_3m,
            contents_6m,
            # ↑ 컬럼명은 videos_3m/videos_6m이지만 인스타는 사진도 포함한다.
            #   스키마를 유튜브 기준으로 만들어서 생긴 이름 불일치.

            avg_view_3m,
            avg_like_3m,
            avg_comment_3m,
            engagement_3m,

            avg_view_6m,

            engagement,
            loyalty,
            view_per_follower_ratio,

            upload_frequency_weekly,

            longform_avg_view,
            longform_avg_like,
            longform_avg_comment,

            shorts_avg_view,
            shorts_avg_like,
            shorts_avg_comment,

            longform_engagement,
            shorts_engagement,

            ad_longform_avg_view,
            ad_longform_avg_like,
            ad_longform_avg_comment,
            ad_longform_er,

            organic_longform_avg_view,
            organic_longform_avg_like,
            organic_longform_avg_comment,
            organic_longform_er,
            # ↑ 변수는 organic_*인데 컬럼은 normal_*이다.
            #   유튜브가 normal을 쓰고 인스타 코드가 organic을 쓴다.
            #   같은 개념의 이름이 두 개.

            ad_shorts_avg_view,
            ad_shorts_avg_like,
            ad_shorts_avg_comment,
            ad_shorts_er,

            organic_shorts_avg_view,
            organic_shorts_avg_like,
            organic_shorts_avg_comment,
            organic_shorts_er,

            longform_sample,
            shorts_sample,

            ad_longform_sample,
            normal_longform_sample,

            ad_shorts_sample,
            normal_shorts_sample
        ))
        # ⚠️ 진행 로그가 없다. 대상이 200~300채널이라 몇 초면 끝나지만,
        #    유튜브·틱톡은 채널마다 지표를 한 줄씩 찍어서
        #    이상값을 즉시 눈으로 확인할 수 있다.

conn.close()
print("완료")
# ⚠️ main() 함수도 if __name__ 가드도 없다.
#    import하면 바로 실행된다. (세 플랫폼 metrics 공통 문제)