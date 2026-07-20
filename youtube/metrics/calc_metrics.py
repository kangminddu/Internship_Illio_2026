import warnings
warnings.filterwarnings("ignore", category=UserWarning)
import pymysql
from datetime import datetime
from dateutil.relativedelta import relativedelta

from config import DB

def avg_like_of(rows):
    likes = [r[4] for r in rows if r[4] is not None]
    if not likes:
        return None
    return trimmed_mean(likes)

def avg_comment_of(rows):
    comments = [r[5] for r in rows if r[5] is not None]
    if not comments:
        return None
    return trimmed_mean(comments)

def trimmed_mean(values, trim_ratio=0.1, min_sample=3):
    """상하위 trim_ratio 제거 평균. 표본이 min_sample 미만이면 자르지 않고 단순평균.
    표본 0이면 None."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    if n >= min_sample:
        k = int(n * trim_ratio)
        if n > 2 * k:
            vals = vals[k:n - k]
    return sum(vals) / len(vals)


def avg_view_of(rows):
    """rows: (content_id, content_type, is_paid, view, like, comment) 리스트.
    평균 조회수(trimmed)와 표본 수 반환."""
    views = [r[3] for r in rows if r[3] is not None]
    if not views:
        return None, 0
    return trimmed_mean(views), len(views)


def er_of(rows):
    """ER = (평균좋아요 + 평균댓글) / 평균조회수 * 100."""
    v = trimmed_mean([r[3] for r in rows])
    l = trimmed_mean([r[4] for r in rows])
    c = trimmed_mean([r[5] for r in rows])
    if not v:  # None 또는 0
        return None
    return ((l + c) / v) * 100


conn = pymysql.connect(**DB, autocommit=True)

today_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
cutoff_3m = today_midnight - relativedelta(months=3)
cutoff_6m = today_midnight - relativedelta(months=6)
cutoff_12m = today_midnight - relativedelta(months=12)

with conn.cursor() as cur:
    print("기존 channel_metrics 삭제")
    cur.execute("DELETE FROM channel_metrics")

    cur.execute("""
        SELECT DISTINCT ch.channel_id
        FROM channels ch
        JOIN crawl_logs cl
            ON ch.channel_id = cl.channel_id
           AND cl.layer = 'L2b'
        WHERE ch.channel_activity_status IN ('active','low_active')
    """)
    channel_ids = [r[0] for r in cur.fetchall()]
    print(f"대상 채널 : {len(channel_ids)}개")

    def fetch_rows(channel_id, cutoff_date):
        """content_type + is_paid + 최신 snapshot을 한 번에. cutoff별 1회 쿼리."""
        cur.execute("""
            SELECT c.content_id, c.content_type, c.is_paid_promotion,
                   s.view_count,
                   COALESCE(s.like_count, 0),
                   COALESCE(s.comment_count, 0)
            FROM contents c
            JOIN content_snapshots s ON s.snapshot_id = (
                SELECT snapshot_id FROM content_snapshots
                WHERE content_id = c.content_id
                ORDER BY captured_at DESC, snapshot_id DESC
                LIMIT 1
            )
            WHERE c.channel_id = %s
              AND c.published_at IS NOT NULL
              AND c.published_at >= %s
              AND s.view_count IS NOT NULL
        """, (channel_id, cutoff_date))
        return cur.fetchall()

    def count_since(channel_id, cutoff):
        cur.execute("""
            SELECT COUNT(*) FROM contents
            WHERE channel_id = %s AND published_at IS NOT NULL AND published_at >= %s
        """, (channel_id, cutoff))
        return cur.fetchone()[0]

    for channel_id in channel_ids:
        # ── 팔로워 ──
        cur.execute("""
            SELECT follower_count FROM channel_snapshots
            WHERE channel_id = %s ORDER BY captured_at DESC LIMIT 1
        """, (channel_id,))
        row = cur.fetchone()
        followers = row[0] if row else None

        # ── 업로드 수 (3/6/12m) ──
        videos_3m = count_since(channel_id, cutoff_3m)
        videos_6m = count_since(channel_id, cutoff_6m)
        videos_12m = count_since(channel_id, cutoff_12m)

        # ── 3개월 지표 (통합) ──
        rows_3m = fetch_rows(channel_id, cutoff_3m)
        avg_view_3m = trimmed_mean([r[3] for r in rows_3m])
        avg_like_3m = trimmed_mean([r[4] for r in rows_3m])
        avg_comment_3m = trimmed_mean([r[5] for r in rows_3m])
        if avg_view_3m:
            er_3m = ((avg_like_3m + avg_comment_3m) / avg_view_3m) * 100
        else:
            avg_view_3m = avg_like_3m = avg_comment_3m = er_3m = None

        # ── 6개월 데이터, 부족하면 12개월 확장 ──
        rows = fetch_rows(channel_id, cutoff_6m)
        period_weeks = 26
        aggregation_method = "trimmed_mean_6m"
        if len(rows) < 10:
            rows = fetch_rows(channel_id, cutoff_12m)
            period_weeks = 52
            aggregation_method = "trimmed_mean_12m"
        if len(rows) < 10:
            continue

        sample_count = len({r[0] for r in rows})

        # ── 통합 지표 (전체 = 롱폼+쇼츠 합친 평균) ──
        avg_view = trimmed_mean([r[3] for r in rows])
        avg_like = trimmed_mean([r[4] for r in rows])
        avg_comment = trimmed_mean([r[5] for r in rows])
        if avg_view is None or avg_view == 0:
            continue
        avg_view = float(avg_view); avg_like = float(avg_like); avg_comment = float(avg_comment)

        er = ((avg_like + avg_comment) / avg_view) * 100
        loyalty_score = ((avg_comment * 10 + avg_like * 1) / avg_view) * 100
        like_ratio = (avg_like / avg_view) * 100
        comment_ratio = (avg_comment / avg_view) * 100

        vpf = (avg_view / followers) * 100 if followers and followers > 0 else None

        upload_freq = (videos_6m / 26) if period_weeks == 26 else (videos_12m / 52)

        # ── 롱폼/쇼츠 분리 (6개월 rows 기준) ──
        longform = [r for r in rows if r[1] == 'video']
        shorts   = [r for r in rows if r[1] == 'shorts']

        longform_avg_view, longform_sample = avg_view_of(longform)
        shorts_avg_view,   shorts_sample   = avg_view_of(shorts)
        longform_er = er_of(longform)
        shorts_er   = er_of(shorts)
        
        longform_avg_like = avg_like_of(longform)
        longform_avg_comment = avg_comment_of(longform)
        
        shorts_avg_like = avg_like_of(shorts)
        shorts_avg_comment = avg_comment_of(shorts)

        # ── 광고/일반 × 롱폼/쇼츠 (조회수만) ──
        ad_longform     = [r for r in longform if r[2] == 1]
        normal_longform = [r for r in longform if r[2] == 0]
        ad_shorts       = [r for r in shorts if r[2] == 1]
        normal_shorts   = [r for r in shorts if r[2] == 0]

        ad_longform_avg_view,     ad_longform_sample     = avg_view_of(ad_longform)
        ad_longform_avg_like = avg_like_of(ad_longform)
        ad_longform_avg_comment = avg_comment_of(ad_longform)
        ad_longform_er = er_of(ad_longform)
        normal_longform_avg_view, normal_longform_sample = avg_view_of(normal_longform)
        normal_longform_avg_like = avg_like_of(normal_longform)
        normal_longform_avg_comment = avg_comment_of(normal_longform)
        normal_longform_er = er_of(normal_longform)
        ad_shorts_avg_view,       ad_shorts_sample       = avg_view_of(ad_shorts)
        ad_shorts_avg_like = avg_like_of(ad_shorts)
        ad_shorts_avg_comment = avg_comment_of(ad_shorts)
        ad_shorts_er = er_of(ad_shorts)
        normal_shorts_avg_view,   normal_shorts_sample   = avg_view_of(normal_shorts)
        normal_shorts_avg_like = avg_like_of(normal_shorts)
        normal_shorts_avg_comment = avg_comment_of(normal_shorts)
        normal_shorts_er = er_of(normal_shorts)

        # ── INSERT ──
        cur.execute("""
            INSERT INTO channel_metrics
            (channel_id, calculated_at, sample_content_count, aggregation_method,
             videos_3m, videos_6m, videos_12m,
             avg_view_3m, avg_like_3m, avg_comment_3m, engagement_rate_3m,
             view_per_follower_ratio, engagement_rate, loyalty_score,
             like_view_ratio, comment_view_ratio, upload_frequency_weekly,
             avg_view,

             longform_avg_view,
             longform_avg_like,
             longform_avg_comment,
             longform_er,

             shorts_avg_view,
             shorts_avg_like,
             shorts_avg_comment,
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
             normal_shorts_sample)

            VALUES
            (%s,%s,%s,%s,
             %s,%s,%s,
             %s,%s,%s,%s,
             %s,%s,%s,
             %s,%s,%s,
             %s,

             %s,%s,%s,%s,

             %s,%s,%s,%s,

             %s,%s,%s,%s,

             %s,%s,%s,%s,

             %s,%s,%s,%s,

             %s,%s,%s,%s,

             %s,%s,%s,%s,%s,%s)
        """, (
            channel_id, datetime.now(), sample_count, aggregation_method,
            videos_3m, videos_6m, videos_12m,
            avg_view_3m, avg_like_3m, avg_comment_3m, er_3m,
            vpf, er, loyalty_score,
            like_ratio, comment_ratio, upload_freq,
            avg_view,

            longform_avg_view,
            longform_avg_like,
            longform_avg_comment,
            longform_er,

            shorts_avg_view,
            shorts_avg_like,
            shorts_avg_comment,
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
            normal_shorts_sample,
        ))

        er3m_str = f"{er_3m:.2f}%" if er_3m is not None else "N/A"
        vpf_str = f"{vpf:.2f}%" if vpf is not None else "N/A"
        lf_str = f"{longform_avg_view:.0f}" if longform_avg_view else "-"
        sf_str = f"{shorts_avg_view:.0f}" if shorts_avg_view else "-"
        print(f"ch={channel_id} | 3M:{videos_3m} ER3M:{er3m_str} | sample={sample_count} "
              f"agg={aggregation_method} | 전체조회:{avg_view:.0f} ER:{er:.2f}% Loyalty:{loyalty_score:.2f} VPF:{vpf_str} "
              f"| 롱폼:{lf_str}({longform_sample}) 쇼츠:{sf_str}({shorts_sample})")

conn.close()
print("\n완료")