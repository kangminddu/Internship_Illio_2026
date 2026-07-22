import warnings
warnings.filterwarnings("ignore", category=UserWarning)

import pymysql
from datetime import datetime
from dateutil.relativedelta import relativedelta

from config import DB


# =========================================================
# 설정
# =========================================================

TRIM_RATIO = 0.10
MIN_SAMPLE = 10

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
    return trimmed_mean([r[3] for r in rows if r[3] is not None])


def avg_like(rows):
    return trimmed_mean([r[4] for r in rows if r[4] is not None])


def avg_comment(rows):
    return trimmed_mean([r[5] for r in rows if r[5] is not None])


def engagement_rate(rows):

    view = avg_view(rows)

    if not view:
        return None

    like = avg_like(rows) or 0
    comment = avg_comment(rows) or 0

    return (like + comment) / view * 100


def loyalty_score(rows):

    view = avg_view(rows)

    if not view:
        return None

    like = avg_like(rows) or 0
    comment = avg_comment(rows) or 0

    return (comment * 10 + like) / view * 100


def split_content(rows):

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
    for channel_id in channel_ids:

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
        
        aggregation_method = "trimmed_mean_6m"

        # -------------------------------
        # 전체 평균
        # -------------------------------

        avg_view_3m = avg_view(rows_3m)
        avg_like_3m = avg_like(rows_3m)
        avg_comment_3m = avg_comment(rows_3m)

        avg_view_6m = avg_view(rows_6m)
        avg_like_6m = avg_like(rows_6m)
        avg_comment_6m = avg_comment(rows_6m)

        engagement = engagement_rate(rows_6m)
        loyalty = loyalty_score(rows_6m)

        # -------------------------------
        # Posts / Reels
        # -------------------------------

        longform, shorts = split_content(rows_6m)

        longform_avg_view = avg_view(longform)
        longform_avg_like = avg_like(longform)
        longform_avg_comment = avg_comment(longform)
        longform_engagement = engagement_rate(longform)

        shorts_avg_view = avg_view(shorts)
        shorts_avg_like = avg_like(shorts)
        shorts_avg_comment = avg_comment(shorts)
        shorts_engagement = engagement_rate(shorts)

        # -------------------------------
        # 광고 / 일반
        # -------------------------------

        organic_rows, ad_rows = split_ad(rows_6m)

        organic_longform, organic_shorts = split_content(organic_rows)
        ad_longform, ad_shorts = split_content(ad_rows)
        
        # -------------------------------
        # 광고 Posts
        # -------------------------------

        ad_longform_avg_view = avg_view(ad_longform)
        ad_longform_avg_like = avg_like(ad_longform)
        ad_longform_avg_comment = avg_comment(ad_longform)
        ad_longform_er = engagement_rate(ad_longform)

        # -------------------------------
        # 일반 Posts
        # -------------------------------

        organic_longform_avg_view = avg_view(organic_longform)
        organic_longform_avg_like = avg_like(organic_longform)
        organic_longform_avg_comment = avg_comment(organic_longform)
        organic_longform_er = engagement_rate(organic_longform)

        # -------------------------------
        # 광고 Reels
        # -------------------------------

        ad_shorts_avg_view = avg_view(ad_shorts)
        ad_shorts_avg_like = avg_like(ad_shorts)
        ad_shorts_avg_comment = avg_comment(ad_shorts)
        ad_shorts_er = engagement_rate(ad_shorts)

        # -------------------------------
        # 일반 Reels
        # -------------------------------

        organic_shorts_avg_view = avg_view(organic_shorts)
        organic_shorts_avg_like = avg_like(organic_shorts)
        organic_shorts_avg_comment = avg_comment(organic_shorts)
        organic_shorts_er = engagement_rate(organic_shorts)
        longform_sample = len(longform)
        shorts_sample = len(shorts)

        ad_longform_sample = len(ad_longform)
        normal_longform_sample = len(organic_longform)

        ad_shorts_sample = len(ad_shorts)
        normal_shorts_sample = len(organic_shorts)
        upload_frequency_weekly = round(contents_6m / 26, 2)
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

            avg_view,

            engagement_rate,
            loyalty_score,

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

            %s,%s,%s,

            %s,

            %s,%s,

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

            avg_view_6m,

            engagement,
            loyalty,

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