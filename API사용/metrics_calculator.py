"""
파생 지표 계산기 — DB에 쌓인 로우 데이터(L1·L2·L3)로 지표를 산출해
channel_metrics 테이블에 저장한다.

준비:
    export DATABASE_URL="postgresql://kangminsoo@localhost:5432/creator_crm"

실행:
    python metrics_calculator.py                 # 모든 채널 계산
    python metrics_calculator.py <channel_id>    # 특정 채널만

지표 정의 (YouTube 기준)
    구독자 대비 조회수 비율 = 평균 조회수 / 구독자 수 × 100
    공개 참여율(ER)        = (평균 좋아요 + 평균 댓글) / 평균 조회수 × 100
    업로드 빈도           = 영상 수 / 수집 기간(주)
    댓글 작성자 중복률     = 2개+ 영상에 댓글 단 계정 / 전체 고유 댓글 계정 × 100
    고정 댓글러 수        = ceil(영상 수 / 2) 이상 영상에 댓글 단 계정 (채널 주인 제외)
    평균 댓글 길이        = 댓글 길이 합 / 댓글 수
    Loyalty Score        = (평균 댓글 × 10 + 평균 좋아요) / 평균 조회수

활동 상태 분류 (가이드라인 5항: 최소 표본 10개, 기간 2배 확장)
    NO_CONTENT   : 수집된 영상이 0개 (비공개/삭제/수집실패 가능) → 지표 계산 안 함
    ACTIVE       : 최근 6개월 영상 ≥ 10개                        → 6개월 window로 계산
    LOW_ACTIVITY : 6개월 <10, 최근 12개월 영상 ≥ 10개 (기간 확장)  → 12개월 window로 계산
    INACTIVE     : 12개월 내에도 영상 < 10개                      → 지표 계산 안 함(표본 부족)
    → ACTIVE/LOW_ACTIVITY만 팬덤 지표를 산출. 나머지는 상태·표본수만 기록.

엣지케이스
    - 0으로 나누는 경우 → None(NULL) 반환 ('계산 불가'와 '값이 0'을 구분)
    - 중복률·고정 댓글러 → 채널 주인(author_id == 채널 ID) 제외
    - '절반 이상' → 올림 기준 (ceil)
    - 평균 → 단순 평균 (이상치 제거는 대기 항목, TODO 참고)

수집 기간 일관성
    - 영상 집계·댓글 길이·댓글 작성자 집계가 모두 동일한 window(개월)를 공유.
"""

import os
import sys
import math

import psycopg2
import psycopg2.extras

MIN_SAMPLE = 10   # 가이드라인 5항: YouTube 최소 영상 10개


def div(numerator, denominator):
    """0으로 나누기 가드. 분모가 0/None이면 None 반환."""
    if denominator in (0, None) or numerator is None:
        return None
    return numerator / denominator


def classify_activity(conn, channel_id):
    """
    채널의 활동 상태를 4분류로 판정하고, 지표 계산에 쓸 window(개월)를 결정.
    반환: (status, window_months)
        window_months 는 ACTIVE=6, LOW_ACTIVITY=12, 그 외엔 None(계산 안 함).
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT
            count(*)                                                              AS total,
            count(*) FILTER (WHERE published_at >= NOW() - INTERVAL '6 months')   AS v6,
            count(*) FILTER (WHERE published_at >= NOW() - INTERVAL '12 months')  AS v12
        FROM videos
        WHERE channel_id = %s
    """, (channel_id,))
    r = cur.fetchone()

    if r["total"] == 0:
        return "NO_CONTENT", None
    if r["v6"] >= MIN_SAMPLE:
        return "ACTIVE", 6
    if r["v12"] >= MIN_SAMPLE:
        return "LOW_ACTIVITY", 12
    return "INACTIVE", None


def compute_metrics(conn, channel_id, lookback_months):
    """
    실제 팬덤 지표 산출. ACTIVE/LOW_ACTIVITY 채널에만 호출된다.
    영상 집계·댓글 길이·댓글 작성자 집계가 모두 lookback_months 를 공유한다.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # --- L2 영상 집계 (최근 N개월) ---
    cur.execute("""
    SELECT count(*)              AS video_count,
           avg(view_count)       AS avg_views,
           avg(like_count)       AS avg_likes,
           avg(comment_count)    AS avg_comments,
           min(published_at)     AS first_pub,
           max(published_at)     AS last_pub
    FROM videos
    WHERE channel_id = %(cid)s
      AND published_at >= NOW() - (%(months)s || ' months')::interval
""", {"cid": channel_id, "months": lookback_months})
    v = cur.fetchone()
    n_videos = v["video_count"]

    avg_views = float(v["avg_views"]) if v["avg_views"] is not None else None
    avg_likes = float(v["avg_likes"]) if v["avg_likes"] is not None else None
    avg_comments = float(v["avg_comments"]) if v["avg_comments"] is not None else None

    # 수집 기간(주) — 가장 오래된~최신 영상 간격
    period_weeks = None
    if v["first_pub"] and v["last_pub"]:
        days = (v["last_pub"] - v["first_pub"]).days
        period_weeks = days / 7 if days > 0 else None

    # --- L1 최신 구독자 수 ---
    cur.execute("""
    SELECT follower_count
    FROM channel_stats
    WHERE channel_id = %s
    ORDER BY snapshot_date DESC
    LIMIT 1
""", (channel_id,))
    row = cur.fetchone()
    subscribers = row["follower_count"] if row else None

    # --- L3 댓글 길이 집계 (최근 N개월 영상의 댓글만) ---
    cur.execute("""
        SELECT avg(text_length) AS avg_len, count(*) AS n_comments
        FROM comments c
        JOIN videos v ON v.id = c.video_id
        WHERE v.channel_id = %(cid)s
          AND v.published_at >= NOW() - (%(months)s || ' months')::interval
    """, {"cid": channel_id, "months": lookback_months})
    row = cur.fetchone()
    avg_comment_length = float(row["avg_len"]) if row["avg_len"] is not None else None

    # --- L3 작성자별 참여 영상 수 (채널 주인 제외, 최근 N개월 영상 한정) ---
    cur.execute("""
        SELECT c.author_id, count(DISTINCT c.video_id) AS vids
        FROM comments c
        JOIN videos v    ON v.id = c.video_id
        JOIN channels ch ON ch.id = v.channel_id
        WHERE v.channel_id = %(cid)s
          AND v.published_at >= NOW() - (%(months)s || ' months')::interval
          AND c.author_id IS NOT NULL
          AND c.author_id IS DISTINCT FROM ch.platform_channel_id   -- 채널 주인 제외
        GROUP BY c.author_id
    """, {"cid": channel_id, "months": lookback_months})
    author_rows = cur.fetchall()

    total_commenters = len(author_rows)
    repeat_commenters = sum(1 for a in author_rows if a["vids"] >= 2)
    core_threshold = math.ceil(n_videos / 2)
    core_commenters = sum(1 for a in author_rows if a["vids"] >= core_threshold)

    # --- 지표 산출 ---
    view_to_subscriber = None
    if avg_views is not None:
        r = div(avg_views, subscribers)
        view_to_subscriber = r * 100 if r is not None else None

    engagement_rate = None
    if avg_likes is not None and avg_comments is not None:
        r = div(avg_likes + avg_comments, avg_views)
        engagement_rate = r * 100 if r is not None else None

    upload_frequency = div(n_videos, period_weeks)

    overlap_rate = None
    r = div(repeat_commenters, total_commenters)
    if r is not None:
        overlap_rate = r * 100

    loyalty_score = None
    if avg_comments is not None and avg_likes is not None:
        loyalty_score = div(avg_comments * 10 + avg_likes * 1, avg_views)

    metrics = {
        "sample_video_count": n_videos,
        "sample_period_start": v["first_pub"].date() if v["first_pub"] else None,
        "sample_period_end": v["last_pub"].date() if v["last_pub"] else None,
        "view_to_subscriber_ratio": view_to_subscriber,
        "engagement_rate": engagement_rate,
        "upload_frequency": upload_frequency,
        "commenter_overlap_rate": overlap_rate,
        "core_commenter_count": core_commenters,
        "avg_comment_length": avg_comment_length,
        "loyalty_score": loyalty_score,
    }
    debug = {
        "구독자": subscribers, "평균조회수": avg_views, "평균좋아요": avg_likes,
        "평균댓글": avg_comments, "전체댓글계정": total_commenters,
        "2개이상": repeat_commenters, "고정댓글러문턱": f"{core_threshold}개 이상",
    }
    return metrics, debug


def empty_metrics(conn, channel_id, lookback_months=12):
    """
    INACTIVE/NO_CONTENT 채널용 — 팬덤 지표는 전부 NULL로 두되,
    표본 수(참고용)만 채운다. 상태 추적을 위해 행 자체는 저장한다.
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT count(*) AS n,
               min(published_at) AS first_pub,
               max(published_at) AS last_pub
        FROM videos
        WHERE channel_id = %(cid)s
          AND published_at >= NOW() - (%(months)s || ' months')::interval
    """, {"cid": channel_id, "months": lookback_months})
    r = cur.fetchone()
    return {
        "sample_video_count": r["n"],
        "sample_period_start": r["first_pub"].date() if r["first_pub"] else None,
        "sample_period_end": r["last_pub"].date() if r["last_pub"] else None,
        "view_to_subscriber_ratio": None,
        "engagement_rate": None,
        "upload_frequency": None,
        "commenter_overlap_rate": None,
        "core_commenter_count": None,
        "avg_comment_length": None,
        "loyalty_score": None,
    }


def compute_channel(conn, channel_id):
    """
    채널 하나를 4분류로 판정 → 상태에 따라 지표 계산 또는 스킵.
    반환: (status, metrics_dict, debug_or_None)
    """
    status, window = classify_activity(conn, channel_id)

    if status in ("ACTIVE", "LOW_ACTIVITY"):
        metrics, debug = compute_metrics(conn, channel_id, window)
        return status, metrics, debug

    # INACTIVE / NO_CONTENT → 지표 스킵, 상태·표본수만 기록
    metrics = empty_metrics(conn, channel_id)
    return status, metrics, None


def save_metrics(conn, channel_id, status, m):
    with conn.cursor() as cur:
        cur.execute("""
    INSERT INTO channel_metrics
        (
            channel_id,
            snapshot_date,
            activity_status,
            sample_video_count,
            sample_period_start,
            sample_period_end,
            view_to_subscriber_ratio,
            engagement_rate,
            upload_frequency,
            commenter_overlap_rate,
            core_commenter_count,
            avg_comment_length,
            loyalty_score
        )
    VALUES (
        %(cid)s,
        CURRENT_DATE,
        %(status)s,
        %(sample_video_count)s,
        %(sample_period_start)s,
        %(sample_period_end)s,
        %(view_to_subscriber_ratio)s,
        %(engagement_rate)s,
        %(upload_frequency)s,
        %(commenter_overlap_rate)s,
        %(core_commenter_count)s,
        %(avg_comment_length)s,
        %(loyalty_score)s
    )
    ON CONFLICT (channel_id, snapshot_date)
    DO UPDATE SET
        activity_status = EXCLUDED.activity_status,
        sample_video_count = EXCLUDED.sample_video_count,
        sample_period_start = EXCLUDED.sample_period_start,
        sample_period_end = EXCLUDED.sample_period_end,
        view_to_subscriber_ratio = EXCLUDED.view_to_subscriber_ratio,
        engagement_rate = EXCLUDED.engagement_rate,
        upload_frequency = EXCLUDED.upload_frequency,
        commenter_overlap_rate = EXCLUDED.commenter_overlap_rate,
        core_commenter_count = EXCLUDED.core_commenter_count,
        avg_comment_length = EXCLUDED.avg_comment_length,
        loyalty_score = EXCLUDED.loyalty_score,
        computed_at = now()
""", {**m, "cid": channel_id, "status": status})


def fmt(x):
    return f"{x:,.2f}" if isinstance(x, float) else ("—" if x is None else str(x))


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        if len(sys.argv) > 1:
            channel_ids = [int(sys.argv[1])]
        else:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM channels ORDER BY id")
                channel_ids = [r[0] for r in cur.fetchall()]

        for cid in channel_ids:
            status, m, debug = compute_channel(conn, cid)
            save_metrics(conn, cid, status, m)

            print(f"\n{'='*52}\n채널 {cid} — [{status}]\n{'='*52}")

            if debug is None:
                # 지표 미산출 채널
                print(f"  표본 부족/없음 — 팬덤 지표 계산 안 함")
                print(f"    표본 영상 수   {fmt(m['sample_video_count'])} 개")
                continue

            print("  [중간값]")
            for k, val in debug.items():
                print(f"    {k:<14} {fmt(val)}")
            print("  [파생 지표]")
            print(f"    구독자 대비 조회수 비율   {fmt(m['view_to_subscriber_ratio'])} %")
            print(f"    공개 참여율 (ER)         {fmt(m['engagement_rate'])} %")
            print(f"    업로드 빈도              {fmt(m['upload_frequency'])} 개/주")
            print(f"    댓글 작성자 중복률        {fmt(m['commenter_overlap_rate'])} %")
            print(f"    고정 댓글러 수           {fmt(m['core_commenter_count'])} 명")
            print(f"    평균 댓글 길이           {fmt(m['avg_comment_length'])} 자")
            print(f"    Loyalty Score           {fmt(m['loyalty_score'])}")

        conn.commit()
        print(f"\n✓ 저장 완료 (channel_metrics)")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# TODO(대표님 확인 후): 평균 조회수를 '상·하위 10% 이상치 제거 후 평균'으로 바꿀지 결정.
#                      현재는 단순 평균. (가이드라인 5항: YouTube는 상·하위 10% 제거 후 평균이 명시 요구사항)
if __name__ == "__main__":
    main()