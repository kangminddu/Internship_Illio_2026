"""
TikTok 파생지표 계산 (L2 기반)

유튜브 calc_metric.py를 TikTok에 맞게 정리:
  - 롱폼/쇼츠/광고 분리 제거 (TikTok은 전부 content_type='tiktok')
  - 6/12개월 확장 제거 (TikTok L2는 3개월치만 수집) → 3개월 단일
  - Loyalty Score 분모: 조회수 → 팔로워수 (TikTok 스크린샷 기준)
  - 대상: channel_activity_status IN ('active','low_active')

★ 가이드라인 대조 결과, 이 파일이 유튜브보다 정확하다.
------
  Loyalty Score 분모   가이드라인: 팔로워 수   → 여기는 맞음
                                                  유튜브는 조회수 + ×100 (초과)
  ×100 여부            가이드라인: 없음         → 여기는 맞음
  수집 기간            가이드라인: 최근 3개월   → 여기는 맞음
  최소 샘플            가이드라인: 15개         → ⚠️ 여기는 10개 (MIN_SAMPLE)

  ⚠️ 가이드라인은 TikTok 집계를 '중간값(Median)' 권장한다.
     "바이럴 이상치 영상에 의한 평균 왜곡이 심하므로"
     현재는 유튜브와 같은 절사평균(trimmed_mean)을 쓴다. 미반영 사항.

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
# relativedelta: timedelta(days=90)은 '3개월'이 아니다.
# 달마다 일수가 다르므로 months=3으로 정확히 계산한다.

from tiktok import config
DB = config.DB


# -------------------------------------------------------
# 튜닝 파라미터 (여기만 고치면 됨)
#
# 상수를 파일 상단에 모은 이유: 기준을 바꿀 때 로직을 뒤지지 않아도 된다.
# -------------------------------------------------------

MIN_SAMPLE = 10       # 3개월 내 영상이 이 개수 미만이면 지표 계산 skip
                      # (유튜브와 동일 기준. 결과 보고 조정 가능)
                      # ⚠️ 가이드라인 TikTok 기준은 15개다.
TRIM_RATIO = 0.1      # trimmed_mean 상하위 제거 비율
PERIOD_MONTHS = 3     # 지표 계산 기간
PERIOD_WEEKS = 13     # 업로드 빈도 계산용 (3개월 ≈ 13주)


# -------------------------------------------------------
# 집계 함수
# -------------------------------------------------------

def trimmed_mean(values, trim_ratio=TRIM_RATIO, min_sample=3):
    """상하위 trim_ratio 제거 평균. 표본이 min_sample 미만이면 안 자르고 단순평균.
    표본 0이면 None. (조회수 이상치 왜곡 방지)

    틱톡은 바이럴 편차가 유튜브보다 훨씬 심하다.
    1000만 조회 영상 하나가 나머지 14개(각 1만)의 평균을 통째로 왜곡한다.
    """
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    if n >= min_sample:
        k = int(n * trim_ratio)
        if n > 2 * k:      # 잘라내고도 남는 게 있을 때만
            vals = vals[k:n - k]
    return sum(vals) / len(vals)


# -------------------------------------------------------
# 메인
# -------------------------------------------------------

conn = pymysql.connect(**DB, autocommit=True)

# 기준 시각을 자정으로 고정. 실행 시각마다 cutoff가 달라지면
# 같은 날 두 번 돌렸을 때 결과가 미묘하게 달라진다.
today_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
cutoff = today_midnight - relativedelta(months=PERIOD_MONTHS)

with conn.cursor() as cur:

    print("기존 TikTok channel_metrics 삭제")
    # TikTok 채널의 metric만 삭제 (유튜브 metric 보존)
    #
    # 세 플랫폼이 channel_metrics를 공유하므로 platform 조인이 필수다.
    # 이게 없으면 유튜브 지표까지 날아간다.
    cur.execute("""
        DELETE cm FROM channel_metrics cm
        JOIN channels ch ON cm.channel_id = ch.channel_id
        WHERE ch.platform = 'tiktok'
    """)

    # 대상 채널: 활동 채널(active/low_active) 중 tiktok 영상이 있는 채널
    #
    # 유튜브와 달리 crawl_logs를 안 본다.
    # "L2b 성공 로그가 있나"보다 "영상이 실제로 있나"가 더 직접적인 조건이다.
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
        """영상당 최신 snapshot 1개 + 기간 필터. (조회수/좋아요/댓글)

        ★ 유튜브와 다르게 스냅샷을 하나만 본다.

        유튜브는 L2a(조회수만)와 L2b(좋아요 포함)가 나뉘어 있어서
        "조회수는 최신 스냅샷, 좋아요는 like가 기록된 최신 스냅샷"으로
        분리 조회해야 했다. 안 그러면 주간 갱신에서 ER이 0으로 붕괴한다.

        틱톡은 L2 한 번에 세 값을 다 받으므로 그런 문제가 없다.
        COALESCE(…, 0)으로 단순 처리한다.
        """
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
        """업로드 수. 스냅샷 조인이 없다 —
        '몇 개 올렸나'는 조회수 유무와 무관하다."""
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
        # VPF와 Loyalty의 분모. L1/L2가 넣은 값 중 최신을 쓴다.
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
        # ★ 유튜브는 조용히 continue해서 "왜 지표가 없지?"를 추적할 수 없는데,
        #   여기는 사유를 출력한다.
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
            # 조회수가 0이면 모든 비율 지표의 분모가 0이 된다.
            skipped += 1
            print(f"(skip) ch={channel_id} — 평균 조회수 0 또는 None")
            continue

        avg_view = float(avg_view)
        avg_like = float(avg_like)
        avg_comment = float(avg_comment)

        # ── 지표 산출 ──
        # 조회수 대비 지표 — 알고리즘 노출 중 실제 반응 비율
        like_ratio = (avg_like / avg_view) * 100
        comment_ratio = (avg_comment / avg_view) * 100
        er = ((avg_like + avg_comment) / avg_view) * 100

        # 팔로워 대비 지표 (팔로워 없으면 None)
        if followers and followers > 0:
            vpf = (avg_view / followers) * 100
            # ★ Loyalty 분모가 팔로워다 (유튜브는 조회수).
            #   가이드라인이 플랫폼마다 다르게 정했다.
            #   그리고 ×100이 없어서 값이 매우 작다(0.0001 단위).
            #   → export에서 소수점 4자리로 표시하고
            #     "타 플랫폼과 비교 불가"를 명시한다.
            #   댓글에 가중치 10을 주는 이유: 좋아요는 클릭 한 번이지만
            #   댓글은 글을 써야 한다. 참여 깊이가 다르다.
            loyalty_score = (avg_comment * 10 + avg_like * 1) / followers
        else:
            vpf = None
            loyalty_score = None
            # 0이 아니라 None. "팔로워가 0"과 "팔로워를 모름"은 다르다.

        # 업로드 빈도 (주당)
        upload_freq = videos_3m / PERIOD_WEEKS

        # ── INSERT (L2 지표만. L3 지표는 calc_l3_metric이 UPDATE로 얹음) ──
        #
        # 유튜브는 컬럼이 48개인데 여기는 16개다.
        # 롱폼/쇼츠 × 광고/일반 4분할이 없기 때문 —
        # 틱톡은 콘텐츠 유형이 하나뿐이다.
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
            #                                          ↑ 집계 방식을 기록한다.
            #   나중에 median으로 바꾸면 "trimmed_mean_3m"과 "median_3m"이
            #   섞이는데, 이 값이 있어야 사과와 오렌지를 구분할 수 있다.
            videos_3m,
            avg_view, avg_like, avg_comment, er,
            vpf, er, loyalty_score,
            #    ↑ engagement_rate_3m과 engagement_rate에 같은 값을 넣는다.
            #      유튜브는 3개월/6개월을 따로 계산하는데 틱톡은 3개월 단일이라
            #      두 컬럼이 같아진다. (스키마를 공유하기 때문)
            like_ratio, comment_ratio, upload_freq,
            avg_view,
        ))

        calculated += 1

        # None 방어 후 출력. f-string의 :.2f는 None에서 TypeError가 난다.
        vpf_str = f"{vpf:.2f}%" if vpf is not None else "N/A"
        loy_str = f"{loyalty_score:.4f}" if loyalty_score is not None else "N/A"
        print(f"ch={channel_id} | 표본={sample_count} f={followers} "
              f"| 조회={avg_view:.0f} 좋아요={avg_like:.0f} 댓글={avg_comment:.1f} "
              f"| ER={er:.2f}% VPF={vpf_str} Loyalty={loy_str}")

conn.close()
print(f"\n완료 | 계산 {calculated}개 | 스킵 {skipped}개")
# ⚠️ main() 함수도 if __name__ 가드도 없다.
#    import하면 바로 실행된다. (crawler는 전부 가드가 있는데 metrics만 다르다)