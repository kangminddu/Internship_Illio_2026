import warnings
warnings.filterwarnings("ignore", category=UserWarning)
import pymysql
from datetime import datetime
from dateutil.relativedelta import relativedelta

from youtube.config import DB

# ─────────────────────────────────────────────────────────
# [v2 수정 요약]
# 1) fetch_rows: 조회수는 "최신 스냅샷", 좋아요/댓글수는 "좋아요가 기록된
#    최신 스냅샷"에서 분리 조회. 주간 갱신에서 L2a 스냅샷(like=NULL)이
#    최신이 되어도 ER/Loyalty가 0으로 붕괴하지 않는다.
#    like/comment가 한 번도 수집 안 된 콘텐츠는 0이 아니라 None으로 두어
#    평균에서 제외한다 (기존: COALESCE 0 → 평균을 끌어내림).
# 2) 대상 채널: L2b 성공(status='success') 채널만.
# 3) ER/Loyalty: like/comment 표본이 아예 없으면 0이 아니라 None.
# ※ Loyalty의 ×100 스케일은 가이드라인(×100 없음)과 다르지만
#    기존 산출물과의 연속성을 위해 유지 — 변경은 별도 결정 후.
# ─────────────────────────────────────────────────────────

def trimmed_mean(values, trim_ratio=0.1, min_sample=3):
    """상하위 trim_ratio 제거 평균. 표본이 min_sample 미만이면 단순평균.
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


def avg_like_of(rows):
    return trimmed_mean([r[4] for r in rows])


def avg_comment_of(rows):
    return trimmed_mean([r[5] for r in rows])


def avg_view_of(rows):
    views = [r[3] for r in rows if r[3] is not None]
    if not views:
        return None, 0
    return trimmed_mean(views), len(views)


def er_of(rows):
    """ER = (평균좋아요 + 평균댓글) / 평균조회수 * 100.
    like/comment 표본이 전무하면 None (0 아님)."""
    v = trimmed_mean([r[3] for r in rows])
    l = trimmed_mean([r[4] for r in rows])
    c = trimmed_mean([r[5] for r in rows])
    if not v:
        return None
    if l is None and c is None:
        return None
    return (((l or 0) + (c or 0)) / v) * 100


conn = pymysql.connect(**DB, autocommit=True)

today_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
cutoff_3m = today_midnight - relativedelta(months=3)
cutoff_6m = today_midnight - relativedelta(months=6)
cutoff_12m = today_midnight - relativedelta(months=12)

with conn.cursor() as cur:
    print("기존 Youtube channel_metrics 삭제")
    cur.execute("""
        DELETE cm
        FROM channel_metrics cm
        JOIN channels ch
            ON cm.channel_id = ch.channel_id
        WHERE ch.platform = 'youtube'
    """)
    # [수정 2] L2b '성공' 채널만 대상 (failed 로그만 있는 채널 제외)
    cur.execute("""
        SELECT DISTINCT ch.channel_id
        FROM channels ch
        JOIN crawl_logs cl
            ON ch.channel_id = cl.channel_id
           AND cl.layer = 'L2b'
           AND cl.status = 'success'
        WHERE ch.platform = 'youtube'
        AND ch.channel_activity_status IN ('active','low_active')
    """)
    channel_ids = [r[0] for r in cur.fetchall()]
    print(f"대상 채널 : {len(channel_ids)}개")

    def fetch_rows(channel_id, cutoff_date):
        """[수정 1] 조회수 = 최신 스냅샷 / 좋아요·댓글수 = like가 기록된 최신 스냅샷.
        engagement가 한 번도 수집 안 된 콘텐츠는 like/comment가 None으로 남아
        평균 계산에서 제외된다."""
        cur.execute("""
            SELECT c.content_id, c.content_type, c.is_paid_promotion,
                   sv.view_count,
                   se.like_count,
                   se.comment_count
            FROM contents c
            JOIN content_snapshots sv ON sv.snapshot_id = (
                SELECT snapshot_id FROM content_snapshots
                WHERE content_id = c.content_id
                ORDER BY captured_at DESC, snapshot_id DESC
                LIMIT 1
            )
            LEFT JOIN content_snapshots se ON se.snapshot_id = (
                SELECT snapshot_id FROM content_snapshots
                WHERE content_id = c.content_id
                  AND like_count IS NOT NULL
                ORDER BY captured_at DESC, snapshot_id DESC
                LIMIT 1
            )
            WHERE c.channel_id = %s
              AND c.published_at IS NOT NULL
              AND c.published_at >= %s
              AND sv.view_count IS NOT NULL
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
        if avg_view_3m and not (avg_like_3m is None and avg_comment_3m is None):
            er_3m = (((avg_like_3m or 0) + (avg_comment_3m or 0)) / avg_view_3m) * 100
        else:
            er_3m = None
            if not avg_view_3m:
                avg_view_3m = avg_like_3m = avg_comment_3m = None

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

        # ── 통합 지표 ──
        avg_view = trimmed_mean([r[3] for r in rows])
        avg_like = trimmed_mean([r[4] for r in rows])
        avg_comment = trimmed_mean([r[5] for r in rows])
        if avg_view is None or avg_view == 0:
            continue
        avg_view = float(avg_view)

        # [수정 3] engagement 표본이 전무하면 ER/Loyalty도 None (0 아님)
        if avg_like is None and avg_comment is None:
            er = loyalty_score = like_ratio = comment_ratio = None
        else:
            al = float(avg_like or 0)
            ac = float(avg_comment or 0)
            er = ((al + ac) / avg_view) * 100
            loyalty_score = ((ac * 10 + al * 1) / avg_view) * 100
            like_ratio = (al / avg_view) * 100
            comment_ratio = (ac / avg_view) * 100

        vpf = (avg_view / followers) * 100 if followers and followers > 0 else None
        upload_freq = (videos_6m / 26) if period_weeks == 26 else (videos_12m / 52)

        # ── 롱폼/쇼츠 분리 ──
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

        # ── 광고/일반 × 롱폼/쇼츠 ──
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
             longform_avg_view, longform_avg_like, longform_avg_comment, longform_er,
             shorts_avg_view, shorts_avg_like, shorts_avg_comment, shorts_er,
             ad_longform_avg_view, ad_longform_avg_like,
             ad_longform_avg_comment, ad_longform_er,
             normal_longform_avg_view, normal_longform_avg_like,
             normal_longform_avg_comment, normal_longform_er,
             ad_shorts_avg_view, ad_shorts_avg_like,
             ad_shorts_avg_comment, ad_shorts_er,
             normal_shorts_avg_view, normal_shorts_avg_like,
             normal_shorts_avg_comment, normal_shorts_er,
             longform_sample, shorts_sample,
             ad_longform_sample, normal_longform_sample,
             ad_shorts_sample, normal_shorts_sample)
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
            longform_avg_view, longform_avg_like, longform_avg_comment, longform_er,
            shorts_avg_view, shorts_avg_like, shorts_avg_comment, shorts_er,
            ad_longform_avg_view, ad_longform_avg_like,
            ad_longform_avg_comment, ad_longform_er,
            normal_longform_avg_view, normal_longform_avg_like,
            normal_longform_avg_comment, normal_longform_er,
            ad_shorts_avg_view, ad_shorts_avg_like,
            ad_shorts_avg_comment, ad_shorts_er,
            normal_shorts_avg_view, normal_shorts_avg_like,
            normal_shorts_avg_comment, normal_shorts_er,
            longform_sample, shorts_sample,
            ad_longform_sample, normal_longform_sample,
            ad_shorts_sample, normal_shorts_sample,
        ))

        er3m_str = f"{er_3m:.2f}%" if er_3m is not None else "N/A"
        er_str = f"{er:.2f}%" if er is not None else "N/A"
        loy_str = f"{loyalty_score:.2f}" if loyalty_score is not None else "N/A"
        vpf_str = f"{vpf:.2f}%" if vpf is not None else "N/A"
        lf_str = f"{longform_avg_view:.0f}" if longform_avg_view else "-"
        sf_str = f"{shorts_avg_view:.0f}" if shorts_avg_view else "-"
        print(f"ch={channel_id} | 3M:{videos_3m} ER3M:{er3m_str} | sample={sample_count} "
              f"agg={aggregation_method} | 전체조회:{avg_view:.0f} ER:{er_str} "
              f"Loyalty:{loy_str} VPF:{vpf_str} "
              f"| 롱폼:{lf_str}({longform_sample}) 쇼츠:{sf_str}({shorts_sample})")

conn.close()
print("\n완료")