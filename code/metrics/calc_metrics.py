import pymysql
from datetime import datetime
from dateutil.relativedelta import relativedelta

from config import DB

def trimmed_mean(values, trim_ratio=0.1):
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    k = int(n * trim_ratio)
    if n > 2 * k:
        vals = vals[k:n-k]
    return sum(vals) / len(vals)

conn = pymysql.connect(**DB, autocommit=True)

today_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
cutoff_6m = today_midnight - relativedelta(months=6)
cutoff_12m = today_midnight - relativedelta(months=12)

with conn.cursor() as cur:
    print("기존 channel_metrics 삭제")
    cur.execute("DELETE FROM channel_metrics")

    cur.execute("""
        SELECT DISTINCT ch.channel_id
        FROM channels ch
        JOIN crawl_logs cl
            ON ch.channel_id=cl.channel_id
           AND cl.layer='L2b'
        WHERE ch.channel_activity_status IN ('active','low_active')
    """)
    channel_ids = [r[0] for r in cur.fetchall()]
    print(f"대상 채널 : {len(channel_ids)}개")

    for channel_id in channel_ids:
        cur.execute("""
            SELECT follower_count
            FROM channel_snapshots
            WHERE channel_id=%s
            ORDER BY captured_at DESC
            LIMIT 1
        """, (channel_id,))
        row = cur.fetchone()
        followers = row[0] if row else None

        cur.execute("""
            SELECT COUNT(*) FROM contents
            WHERE channel_id=%s AND published_at IS NOT NULL AND published_at >= %s
        """, (channel_id, cutoff_6m))
        videos_6m = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM contents
            WHERE channel_id=%s AND published_at IS NOT NULL AND published_at >= %s
        """, (channel_id, cutoff_12m))
        videos_12m = cur.fetchone()[0]

        def fetch_snapshot_rows(cutoff_date):
            cur.execute("""
                SELECT
                    c.content_id,
                    s.view_count,
                    COALESCE(s.like_count,0),
                    COALESCE(s.comment_count,0)
                FROM contents c
                JOIN content_snapshots s
                ON s.snapshot_id = (
                        SELECT snapshot_id
                        FROM content_snapshots
                        WHERE content_id = c.content_id
                        ORDER BY captured_at DESC, snapshot_id DESC
                        LIMIT 1
                )
                WHERE c.channel_id=%s
                AND c.published_at IS NOT NULL
                AND c.published_at >= %s
                AND s.view_count IS NOT NULL
            """, (channel_id, cutoff_date))
            return cur.fetchall()

        # 최근 6개월 데이터 수집
        rows = fetch_snapshot_rows(cutoff_6m)
        period_weeks = 26
        aggregation_method = "trimmed_mean_6m"

        # 샘플 부족하면 12개월까지 확장
        if len(rows) < 10:
            rows = fetch_snapshot_rows(cutoff_12m)
            period_weeks = 52
            aggregation_method = "trimmed_mean_12m"

        # 12개월까지 봐도 샘플 부족하면 제외
        if len(rows) < 10:
            continue

        sample_count = len({r[0] for r in rows})
        avg_view = trimmed_mean([r[1] for r in rows])
        avg_like = trimmed_mean([r[2] for r in rows])
        avg_comment = trimmed_mean([r[3] for r in rows])

        if avg_view is None or avg_view == 0:
            continue
        
        avg_view = float(avg_view)
        avg_like = float(avg_like)
        avg_comment = float(avg_comment)

        er = ((avg_like + avg_comment) / avg_view) * 100
        loyalty_score = ((avg_comment * 10 + avg_like * 1) / avg_view) * 100

        vpf = None
        if followers and followers > 0:
            vpf = (avg_view / followers) * 100

        like_ratio = (avg_like / avg_view) * 100
        comment_ratio = (avg_comment / avg_view) * 100

        # 업로드 빈도 = 실제 업로드 영상 수 / 기간(주)
        if period_weeks == 26:
            upload_freq = videos_6m / 26
        else:
            upload_freq = videos_12m / 52

        cur.execute("""
            INSERT INTO channel_metrics
            (
                channel_id,
                calculated_at,
                sample_content_count,
                aggregation_method,

                videos_6m,
                videos_12m,

                view_per_follower_ratio,
                engagement_rate,
                loyalty_score,

                like_view_ratio,
                comment_view_ratio,

                upload_frequency_weekly
            )
            VALUES
            (
                %s, %s, %s, %s,

                %s, %s,

                %s, %s, %s,

                %s, %s, %s
            )
        """,
        (
            channel_id,
            datetime.now(),
            sample_count,
            aggregation_method,

            videos_6m,
            videos_12m,

            vpf,
            er,
            loyalty_score,

            like_ratio,
            comment_ratio,

            upload_freq,
        ))

        print(
            f"ch={channel_id} "
            f"| sample={sample_count} "
            f"| agg={aggregation_method} "
            f"| ER={er:.2f}% "
            f"| Loyalty={loyalty_score:.2f} "
            f"| VPF={vpf:.2f}%"
            if vpf is not None
            else f"ch={channel_id} | agg={aggregation_method} | Loyalty={loyalty_score:.2f}"
        )

conn.close()
print("\n완료")