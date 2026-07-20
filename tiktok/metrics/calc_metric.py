"""
TikTok 파생지표 계산 (L2 기반)

유튜브 calc_metric.py를 TikTok에 맞게 정리:
  - 롱폼/쇼츠/광고 분리 제거 (TikTok은 전부 content_type='tiktok')
  - 6/12개월 확장 제거 (TikTok L2는 3개월치만 수집) → 3개월 단일
  - Loyalty Score 분모: 조회수 → 팔로워수 (TikTok 스크린샷 기준)
  - 대상: channel_activity_status IN ('active','low_active')

계산 지표 (9개 중 L2 담당분):
  1. view_per_follower_ratio  = 평균조회수 / 팔로워 * 100
  2. like_view_ratio          = 평균좋아요 / 평균조회수 * 100
  3. comment_view_ratio       = 평균댓글 / 평균조회수 * 100
  4. engagement_rate (ER)     = (평균좋아요+평균댓글) / 평균조회수 * 100
  5. upload_frequency_weekly  = 3개월 영상수 / 13주
  9. loyalty_score            = (평균댓글*10 + 평균좋아요*1) / 팔로워
  + avg_view (참고용 대표 조회수)

L3 지표(중복률/고정댓글러/댓글길이)는 calc_l3_metric.py가 이 row에 UPDATE로 얹음.
재계산 가능: DELETE 후 INSERT 구조라 언제든 다시 실행 가능.
"""

import warnings
warnings.filterwarnings("ignore", category=UserWarning)

import pymysql
from datetime import datetime
from dateutil.relativedelta import relativedelta

from tiktok import config
DB = config.DB


# -------------------------------------------------------
# 튜닝 파라미터 (여기만 고치면 됨)
# -------------------------------------------------------

MIN_SAMPLE = 10       # 3개월 내 영상이 이 개수 미만이면 지표 계산 skip
                      # (유튜브와 동일 기준. 결과 보고 조정 가능)
TRIM_RATIO = 0.1      # trimmed_mean 상하위 제거 비율
PERIOD_MONTHS = 3     # 지표 계산 기간
PERIOD_WEEKS = 13     # 업로드 빈도 계산용 (3개월 ≈ 13주)


# -------------------------------------------------------
# 집계 함수
# -------------------------------------------------------

def trimmed_mean(values, trim_ratio=TRIM_RATIO, min_sample=3):
    """상하위 trim_ratio 제거 평균. 표본이 min_sample 미만이면 안 자르고 단순평균.
    표본 0이면 None. (조회수 이상치 왜곡 방지)"""
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


# -------------------------------------------------------
# 메인
# -------------------------------------------------------

conn = pymysql.connect(**DB, autocommit=True)

today_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
cutoff = today_midnight - relativedelta(months=PERIOD_MONTHS)

with conn.cursor() as cur:

    print("기존 TikTok channel_metrics 삭제")
    # TikTok 채널의 metric만 삭제 (유튜브 metric 보존)
    cur.execute("""
        DELETE cm FROM channel_metrics cm
        JOIN channels ch ON cm.channel_id = ch.channel_id
        WHERE ch.platform = 'tiktok'
    """)

    # 대상 채널: 활동 채널(active/low_active) 중 tiktok 영상이 있는 채널
    cur.execute("""
        SELECT DISTINCT ch.channel_id
        FROM channels ch
        JOIN contents ct
            ON ct.channel_id = ch.channel_id
           AND ct.content_type = 'tiktok'
        WHERE ch.platform = 'tiktok'
          AND ch.channel_activity_status IN ('active', 'low_active')
    """)
    channel_ids = [r[0] for r in cur.fetchall()]
    print(f"대상 채널 : {len(channel_ids)}개\n")

    def fetch_rows(channel_id, cutoff_date):
        """영상당 최신 snapshot 1개 + 기간 필터. (조회수/좋아요/댓글)"""
        cur.execute("""
            SELECT c.content_id,
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
              AND c.content_type = 'tiktok'
              AND c.published_at IS NOT NULL
              AND c.published_at >= %s
              AND s.view_count IS NOT NULL
        """, (channel_id, cutoff_date))
        return cur.fetchall()

    def count_since(channel_id, cutoff_date):
        cur.execute("""
            SELECT COUNT(*) FROM contents
            WHERE channel_id = %s
              AND content_type = 'tiktok'
              AND published_at IS NOT NULL
              AND published_at >= %s
        """, (channel_id, cutoff_date))
        return cur.fetchone()[0]

    calculated = 0
    skipped = 0

    for channel_id in channel_ids:

        # ── 팔로워 (최신 스냅샷) ──
        cur.execute("""
            SELECT follower_count FROM channel_snapshots
            WHERE channel_id = %s
            ORDER BY captured_at DESC LIMIT 1
        """, (channel_id,))
        row = cur.fetchone()
        followers = row[0] if row else None

        # ── 3개월 영상 데이터 ──
        rows = fetch_rows(channel_id, cutoff)

        # 최소 표본 미달 → skip
        if len(rows) < MIN_SAMPLE:
            skipped += 1
            print(f"(skip) ch={channel_id} — 3개월 영상 {len(rows)}개 (< {MIN_SAMPLE})")
            continue

        sample_count = len(rows)
        videos_3m = count_since(channel_id, cutoff)

        # ── 대표값 (trimmed_mean) ──
        avg_view = trimmed_mean([r[1] for r in rows])
        avg_like = trimmed_mean([r[2] for r in rows])
        avg_comment = trimmed_mean([r[3] for r in rows])

        if avg_view is None or avg_view == 0:
            skipped += 1
            print(f"(skip) ch={channel_id} — 평균 조회수 0 또는 None")
            continue

        avg_view = float(avg_view)
        avg_like = float(avg_like)
        avg_comment = float(avg_comment)

        # ── 지표 산출 ──
        # 조회수 대비 지표
        like_ratio = (avg_like / avg_view) * 100
        comment_ratio = (avg_comment / avg_view) * 100
        er = ((avg_like + avg_comment) / avg_view) * 100

        # 팔로워 대비 지표 (팔로워 없으면 None)
        if followers and followers > 0:
            vpf = (avg_view / followers) * 100
            loyalty_score = (avg_comment * 10 + avg_like * 1) / followers
        else:
            vpf = None
            loyalty_score = None

        # 업로드 빈도 (주당)
        upload_freq = videos_3m / PERIOD_WEEKS

        # ── INSERT (L2 지표만. L3 지표는 calc_l3_metric이 UPDATE로 얹음) ──
        cur.execute("""
            INSERT INTO channel_metrics
            (channel_id, calculated_at, sample_content_count, aggregation_method,
             videos_3m,
             avg_view_3m, avg_like_3m, avg_comment_3m, engagement_rate_3m,
             view_per_follower_ratio, engagement_rate, loyalty_score,
             like_view_ratio, comment_view_ratio, upload_frequency_weekly,
             avg_view)
            VALUES
            (%s,%s,%s,%s,
             %s,
             %s,%s,%s,%s,
             %s,%s,%s,
             %s,%s,%s,
             %s)
        """, (
            channel_id, datetime.now(), sample_count, "trimmed_mean_3m",
            videos_3m,
            avg_view, avg_like, avg_comment, er,
            vpf, er, loyalty_score,
            like_ratio, comment_ratio, upload_freq,
            avg_view,
        ))

        calculated += 1

        vpf_str = f"{vpf:.2f}%" if vpf is not None else "N/A"
        loy_str = f"{loyalty_score:.4f}" if loyalty_score is not None else "N/A"
        print(f"ch={channel_id} | 표본={sample_count} f={followers} "
              f"| 조회={avg_view:.0f} 좋아요={avg_like:.0f} 댓글={avg_comment:.1f} "
              f"| ER={er:.2f}% VPF={vpf_str} Loyalty={loy_str}")

conn.close()
print(f"\n완료 | 계산 {calculated}개 | 스킵 {skipped}개")