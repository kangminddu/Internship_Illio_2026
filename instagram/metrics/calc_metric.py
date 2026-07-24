import warnings
warnings.filterwarnings("ignore", category=UserWarning)

import pymysql
from datetime import datetime
from dateutil.relativedelta import relativedelta

try:
    from instagram.config import DB
except Exception:
    from config import DB


# =========================================================
# 설정
# =========================================================

TRIM_RATIO = 0.10
MIN_SAMPLE = 10

# ER / Loyalty 분모 기준
#   "follower" : (좋아요+댓글) / 팔로워        ← 인스타 표준, 기본값
#   "view"     : (좋아요+댓글) / 조회수        ← 유튜브 방식
#
# ⚠️ 인스타는 피드/캐러셀에 조회수를 아예 제공하지 않는다(릴스만 play_count).
#    "view" 로 두면 피드 관련 ER 이 전부 NULL 이 되고, 릴스 ER 과
#    분모가 달라 나란히 비교할 수도 없다. 그래서 follower 로 통일한다.
ER_BASIS = "follower"

conn = pymysql.connect(**DB, autocommit=True)


# =========================================================
# 공통 함수
# =========================================================

def trimmed_mean(values, trim_ratio=TRIM_RATIO):

    vals = [float(v) for v in values if v is not None]

    if not vals:
        return None

    vals.sort()

    n = len(vals)

    if n >= MIN_SAMPLE:
        k = int(n * trim_ratio)

        if n > 2 * k:
            vals = vals[k:n-k]

    return sum(vals) / len(vals)


def avg_view(rows):
    """
    ⚠️ 릴스만 값이 있다(play_count).
       피드/캐러셀은 인스타가 조회수를 제공하지 않아 항상 None.
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

today = datetime.now().replace(
    hour=0,
    minute=0,
    second=0,
    microsecond=0,
)

cutoff_3m = today - relativedelta(months=3)
cutoff_6m = today - relativedelta(months=6)


# =========================================================
# Metric 계산
# =========================================================

with conn.cursor() as cur:

    print("기존 instagram channel_metrics 삭제")
    cur.execute("""
        DELETE cm
        FROM channel_metrics cm
        JOIN channels ch
            ON cm.channel_id = ch.channel_id
        WHERE ch.platform = 'instagram'
    """)

    # ---------------------------------------------------------
    # 채널별 최신 팔로워 수 (ER 분모)
    # ---------------------------------------------------------
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

    # Instagram 채널만 대상
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
        span_weeks = max(span_weeks, 1.0)
        return round(cnt / span_weeks, 2)


    for channel_id in channel_ids:

        follower = followers.get(channel_id)

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

        if sample_count < MIN_SAMPLE:
            print(f"(skip) ch={channel_id} sample={sample_count}")
            continue

        aggregation_method = f"trimmed_mean_6m/{ER_BASIS}"

        # -------------------------------
        # 전체 평균
        # -------------------------------

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

        longform, shorts = split_content(rows_6m)

        longform_avg_view = avg_view(longform)
        longform_avg_like = avg_like(longform)
        longform_avg_comment = avg_comment(longform)
        longform_engagement = engagement_rate(longform, follower)

        shorts_avg_view = avg_view(shorts)
        shorts_avg_like = avg_like(shorts)
        shorts_avg_comment = avg_comment(shorts)
        shorts_engagement = engagement_rate(shorts, follower)

        # -------------------------------
        # 광고 / 일반
        # -------------------------------

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
        view_per_follower_ratio = None
        if follower and shorts_avg_view:
            view_per_follower_ratio = shorts_avg_view / follower * 100

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
            today,

            sample_count,
            aggregation_method,

            contents_3m,
            contents_6m,

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

conn.close()
print("완료")