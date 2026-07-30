"""
youtube/metrics/calc_metrics.py — L2 기반 파생지표 산출

무슨 일을 하는가
------
수집한 원본 데이터(contents + content_snapshots)를 가공해
채널별 파생지표를 계산하고 channel_metrics에 넣는다.

가이드라인이 요구하는 2단계 구조의 두 번째 단계다:
    "로우 데이터를 1차 수집한 후, 이를 가공하여 파생 지표를 산출한다.
     로우 데이터 없이 파생 지표를 직접 수집하면 산출 기준이 불일치하거나
     재계산이 불가능해진다."
→ 지표 공식이 바뀌어도 재크롤링 없이 이 파일만 다시 돌리면 된다.

네트워크를 타지 않는다. DB → 계산 → DB.

산출 지표 (가이드라인 3.1 파생 지표 표 대응)
------
  VPF        평균조회수 ÷ 구독자수 × 100      활성 팬덤 비율
  ER         (평균좋아요+평균댓글) ÷ 평균조회수 × 100
  Loyalty    (평균댓글×10 + 평균좋아요) ÷ 평균조회수 × 100  ← ×100은 가이드라인 초과
  업로드빈도  기간 내 영상 수 ÷ 주
  + 롱폼/쇼츠 × 광고/일반 4분할 지표

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
#    (틱톡/인스타는 ×100 없이 팔로워로 나눈다 → 플랫폼 간 직접 비교 불가)
# ─────────────────────────────────────────────────────────
"""
import warnings
warnings.filterwarnings("ignore", category=UserWarning)
# ↑ pandas/pymysql 조합 경고 억제. 동작에는 영향 없음.
import pymysql
from datetime import datetime
from dateutil.relativedelta import relativedelta
# relativedelta를 쓰는 이유: timedelta(days=90)은 '3개월'이 아니다.
# 달마다 일수가 다르므로 months=3으로 정확히 계산해야 한다.

from youtube.config import DB


def trimmed_mean(values, trim_ratio=0.1, min_sample=3):
    """상하위 trim_ratio 제거 평균. 표본이 min_sample 미만이면 단순평균.
    표본 0이면 None.

    가이드라인: "YouTube — 평균값 (상·하위 10% 이상치 제거 후)"

    왜 절사평균인가:
    크리에이터의 영상 조회수는 편차가 극심하다. 100만 조회 영상 하나가
    나머지 14개(각 1만)의 평균을 통째로 왜곡한다.
    상하위 10%를 잘라내면 '평상시 성과'에 가까워진다.

    None을 걸러내는 것도 중요하다.
    like_count가 수집 안 된 콘텐츠를 0으로 치면 평균이 실제보다 낮아진다.
    "값이 0"과 "값을 모름"은 다르다.
    """
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None          # ← 0이 아니라 None. 호출자가 '표본 없음'을 알 수 있게.
    vals.sort()
    n = len(vals)
    if n >= min_sample:
        k = int(n * trim_ratio)
        if n > 2 * k:        # 잘라내고도 남는 게 있을 때만
            vals = vals[k:n - k]
    return sum(vals) / len(vals)


# fetch_rows가 반환하는 행의 구조:
#   r[0] content_id  r[1] content_type  r[2] is_paid_promotion
#   r[3] view_count  r[4] like_count    r[5] comment_count
# 아래 헬퍼들이 이 인덱스를 공유한다.

def avg_like_of(rows):
    return trimmed_mean([r[4] for r in rows])


def avg_comment_of(rows):
    return trimmed_mean([r[5] for r in rows])


def avg_view_of(rows):
    """(평균 조회수, 표본 수) 반환.
    표본 수를 함께 주는 이유: 지표를 해석할 때 근거가 필요하다.
    "쇼츠 ER 5%"가 영상 20개 기준인지 2개 기준인지에 따라 신뢰도가 다르다.
    → channel_metrics의 *_sample 컬럼으로 저장된다."""
    views = [r[3] for r in rows if r[3] is not None]
    if not views:
        return None, 0
    return trimmed_mean(views), len(views)


def er_of(rows):
    """ER = (평균좋아요 + 평균댓글) / 평균조회수 * 100.
    like/comment 표본이 전무하면 None (0 아님).

    롱폼/쇼츠/광고 등 부분집합마다 ER을 따로 구할 때 쓴다.
    l이나 c 중 하나만 없으면 (l or 0)으로 0 취급하지만,
    둘 다 없으면 계산 자체를 포기한다 — 그건 '참여율 0'이 아니라
    '참여율을 모름'이기 때문."""
    v = trimmed_mean([r[3] for r in rows])
    l = trimmed_mean([r[4] for r in rows])
    c = trimmed_mean([r[5] for r in rows])
    if not v:
        return None
    if l is None and c is None:
        return None
    return (((l or 0) + (c or 0)) / v) * 100


conn = pymysql.connect(**DB, autocommit=True)

# 기준 시각을 자정으로 고정한다.
# 실행 시각마다 cutoff가 달라지면 같은 날 두 번 돌렸을 때
# 결과가 미묘하게 달라진다.
today_midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
cutoff_3m = today_midnight - relativedelta(months=3)
cutoff_6m = today_midnight - relativedelta(months=6)
cutoff_12m = today_midnight - relativedelta(months=12)

with conn.cursor() as cur:
    # ⚠️ DELETE 후 재생성 패턴.
    #
    # channel_metrics는 calculated_at 컬럼이 있는 시계열 설계인데,
    # 매번 지우고 새로 넣으니 채널당 1행만 남는다.
    # (원래 UNIQUE KEY(channel_id)가 채널당 1행만 허용해서 지표가
    #  하나도 안 쌓이던 문제를 발견하고 인덱스를 지웠는데,
    #  코드는 여전히 단일 행을 가정하는 상태 — 미해결 과제)
    #
    # 그리고 autocommit=True라 트랜잭션이 없다. DELETE 직후 중간에
    # 죽으면 기존 지표는 사라지고 새 지표는 일부만 남는다.
    print("기존 Youtube channel_metrics 삭제")
    cur.execute("""
        DELETE cm
        FROM channel_metrics cm
        JOIN channels ch
            ON cm.channel_id = ch.channel_id
        WHERE ch.platform = 'youtube'
    """)

    # [수정 2] L2b '성공' 채널만 대상 (failed 로그만 있는 채널 제외)
    #
    # 두 조건의 의미가 다르다:
    #   L2b success  — 좋아요/댓글수가 실제로 수집됐는가 (데이터 유무)
    #   active/low_active — 지표를 낼 가치가 있는 채널인가 (가치 판단)
    #
    # 활동성은 backfill이 확정한 값이다. 그래서 PIPELINE에서
    # backfill이 metric 바로 앞에 있어야 한다.
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
        평균 계산에서 제외된다.

        ★ 이 파일에서 가장 중요한 설계다. 왜 스냅샷을 둘로 나눠 조회하는가:

        L2a는 view_count만 넣는다 (목록 페이지에 좋아요가 없으므로).
        L2b가 나중에 like_count/comment_count까지 채운다.

        주간 갱신을 하면 이런 순서가 된다:
            1주차: L2b 실행 → 스냅샷 A (view + like + comment)
            2주차: L2a 실행 → 스냅샷 B (view만, like=NULL)  ← 이게 최신

        단순히 "최신 스냅샷"만 쓰면 2주차에 like가 NULL이 되어
        ER과 Loyalty가 0으로 붕괴한다.

        → sv: 최신 스냅샷에서 조회수 (최신값이 정확하니까)
          se: like가 기록된 최신 스냅샷에서 좋아요/댓글 (조금 오래됐어도 있는 값)
        두 스냅샷이 서로 다른 시점일 수 있지만, NULL보다는 낫다는 판단.

        LEFT JOIN인 이유: like가 한 번도 수집 안 된 콘텐츠도
        조회수 지표에는 포함시켜야 한다. (se가 NULL이면 like/comment가 None)
        """
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
                  AND like_count IS NOT NULL          -- ← 여기가 핵심
                ORDER BY captured_at DESC, snapshot_id DESC
                LIMIT 1
            )
            WHERE c.channel_id = %s
              AND c.published_at IS NOT NULL
              -- ↑ ⚠️ 쇼츠는 L2b가 게시일을 채우기 전까지 NULL이라
              --   여기서 통째로 빠진다 → shorts_* 지표가 전부 NULL이 된다
              AND c.published_at >= %s
              AND sv.view_count IS NOT NULL
        """, (channel_id, cutoff_date))
        return cur.fetchall()

    def count_since(channel_id, cutoff):
        """업로드 수. fetch_rows와 달리 스냅샷 조인이 없다.
        '몇 개 올렸나'는 조회수 유무와 무관하기 때문."""
        cur.execute("""
            SELECT COUNT(*) FROM contents
            WHERE channel_id = %s AND published_at IS NOT NULL AND published_at >= %s
        """, (channel_id, cutoff))
        return cur.fetchone()[0]

    for channel_id in channel_ids:
        # ── 팔로워 ──
        # VPF(구독자 대비 조회율)의 분모. L1이 넣은 최신 스냅샷을 쓴다.
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
        # 6개월 지표와 별도로 낸다. 최근 추세를 보려는 용도.
        rows_3m = fetch_rows(channel_id, cutoff_3m)
        avg_view_3m = trimmed_mean([r[3] for r in rows_3m])
        avg_like_3m = trimmed_mean([r[4] for r in rows_3m])
        avg_comment_3m = trimmed_mean([r[5] for r in rows_3m])
        if avg_view_3m and not (avg_like_3m is None and avg_comment_3m is None):
            er_3m = (((avg_like_3m or 0) + (avg_comment_3m or 0)) / avg_view_3m) * 100
        else:
            er_3m = None
            # 조회수조차 없으면 좋아요/댓글 평균도 의미가 없다 → 전부 None
            if not avg_view_3m:
                avg_view_3m = avg_like_3m = avg_comment_3m = None

        # ── 6개월 데이터, 부족하면 12개월 확장 ──
        #
        # 가이드라인: "권장 기간 내 샘플 수 미달 → 기간을 2배 확장하여 재수집"
        #             "확장 후에도 최소 샘플 미달 → 비활성 크리에이터로 분류"
        #
        # aggregation_method를 기록하는 이유:
        # 6개월 기준 지표와 12개월 기준 지표를 나란히 놓고 비교하면
        # 사과와 오렌지를 섞는 셈이다. 어느 기준인지 남겨야 한다.
        rows = fetch_rows(channel_id, cutoff_6m)
        period_weeks = 26
        aggregation_method = "trimmed_mean_6m"
        if len(rows) < 10:
            rows = fetch_rows(channel_id, cutoff_12m)
            period_weeks = 52
            aggregation_method = "trimmed_mean_12m"
        if len(rows) < 10:
            continue
            # ⚠️ 조용히 스킵한다. 로그가 없어서 "왜 이 채널은 지표가 없지?"를
            #    나중에 추적할 수 없다. print 한 줄이면 해결될 부분.

        sample_count = len({r[0] for r in rows})   # content_id 기준 유니크 수

        # ── 통합 지표 ──
        avg_view = trimmed_mean([r[3] for r in rows])
        avg_like = trimmed_mean([r[4] for r in rows])
        avg_comment = trimmed_mean([r[5] for r in rows])
        if avg_view is None or avg_view == 0:
            continue      # 조회수가 0이면 모든 비율 지표의 분모가 0 → 계산 불가
        avg_view = float(avg_view)

        # [수정 3] engagement 표본이 전무하면 ER/Loyalty도 None (0 아님)
        #
        # ER 0%는 "아무도 반응 안 함"이고, None은 "반응을 측정 못 함"이다.
        # 0으로 넣으면 엑셀에서 정렬할 때 최하위로 밀려 오해를 부른다.
        if avg_like is None and avg_comment is None:
            er = loyalty_score = like_ratio = comment_ratio = None
        else:
            al = float(avg_like or 0)
            ac = float(avg_comment or 0)
            er = ((al + ac) / avg_view) * 100
            # 댓글에 가중치 10을 주는 이유(가이드라인):
            # 좋아요는 클릭 한 번이지만 댓글은 글을 써야 한다.
            # 같은 1건이어도 참여 깊이가 다르다.
            # ⚠️ 뒤의 × 100은 가이드라인에 없다. 기존 산출물 호환용.
            loyalty_score = ((ac * 10 + al * 1) / avg_view) * 100
            like_ratio = (al / avg_view) * 100
            comment_ratio = (ac / avg_view) * 100

        vpf = (avg_view / followers) * 100 if followers and followers > 0 else None
        # 업로드 빈도는 실제 사용한 기간에 맞춰 나눈다.
        # 12개월로 확장했으면 12개월치를 52주로 나눠야 주당 빈도가 맞다.
        upload_freq = (videos_6m / 26) if period_weeks == 26 else (videos_12m / 52)

        # ── 롱폼/쇼츠 분리 ──
        # 가이드라인이 포맷별 지표를 따로 요구한다.
        # 쇼츠는 조회수가 크고 참여율이 낮은 경향이라 섞으면 왜곡된다.
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
        #
        # 섭외 판단의 핵심 지표다.
        # "이 크리에이터는 광고 영상에서도 성과가 유지되는가?"
        # 일반 영상만 잘 나오고 광고에서 급락하면 섭외 가치가 낮다.
        # is_paid_promotion은 L2b가 watch 페이지의 유료광고 오버레이로 판별한다.
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
        # 컬럼이 48개다. 롱폼/쇼츠 × 광고/일반 조합이 많아서인데,
        # 각 조합마다 조회수/좋아요/댓글/ER 4개 + 표본 수를 저장한다.
        # 표본 수(*_sample)를 함께 넣는 게 중요하다 —
        # 지표만 있고 표본이 없으면 신뢰도를 판단할 수 없다.
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

        # 진행 로그. None이 섞이므로 포맷 전에 전부 방어한다.
        # (f-string의 :.2f는 None에서 TypeError가 난다)
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
# ⚠️ 이 파일에는 main() 함수도 if __name__ 가드도 없다.
#    모듈 최상위에서 바로 실행되므로 import하면 파이프라인이 돌아버린다.
#    crawler/*.py는 전부 main() + 가드 패턴인데 metrics/export만 다르다.