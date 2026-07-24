"""
crawler_l2a.py (v2) — 영상/쇼츠 목록 수집 + 활동 분류

기존 버전에서 수정한 문제:
  1) [데이터 오염] fetch 실패(네트워크/404/파싱실패)를 '콘텐츠 없음'과 구분 못 해
     활성 채널을 inactive로 박제 + success 기록하던 버그
     → 실패는 failed로 기록하고 활동 상태는 건드리지 않음.
       inactive 판정은 "두 페이지 모두 200 + 정말 콘텐츠 0개"일 때만.
  2) [데이터 오염] 재실행 시 L2b가 채운 정밀 published_at을 근사값으로
     덮어쓰던 버그 → published_is_approx=1인 행만 근사값 갱신 허용.
  3) shorts fetch의 429가 무시되던 버그 → videos와 동일하게 백오프 처리.
  4) 스레드별 sleep → 전역 RateController (간격/백오프/휴식/slow start).
  5) 채널 단위 트랜잭션 + 워커 예외 격리 + Ctrl+C 정상 종료.

config.py 권장 추가:
  L2A_MIN_INTERVAL = 1.2
  L2_REST_EVERY    = 2000    # 요청 N회마다 휴식 (채널당 2회 요청 = 1000채널)
  L2_REST_SECONDS  = 2100    # 35분
"""
import time
import threading
import pymysql
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from youtube.crawler.lib.youtube_parser import (
    get_session, extract_yt_initial_data, parse_l2_videos, parse_l2_shorts)
from youtube.crawler.lib.rate_control import RateController
from youtube.config import DB, L2A_WORKERS, BATCH_LIMIT, STOP_ON_429

try:
    from youtube.config import L2A_MIN_INTERVAL
except ImportError:
    L2A_MIN_INTERVAL = 1.2
try:
    from youtube.config import L2_REST_EVERY
except ImportError:
    L2_REST_EVERY = 2000
try:
    from youtube.config import L2_REST_SECONDS
except ImportError:
    L2_REST_SECONDS = 2100
try:
    from youtube.config import L1_BACKOFF_BASE as BACKOFF_BASE
except ImportError:
    BACKOFF_BASE = 60
MAX_429_RETRY = 3

lock = threading.Lock()
counter = {"done": 0, "ok": 0, "fail": 0, "rate_limited": 0, "db_error": 0}
stop_flag = threading.Event()
rc = RateController(L2A_MIN_INTERVAL, BACKOFF_BASE,
                    rest_every=L2_REST_EVERY, rest_seconds=L2_REST_SECONDS,
                    name="L2a")


def classify_activity(conn, channel_id, now=None):
    """DB contents 기준: 180일 내 10개↑ active / 365일 내 10개↑ low_active / 그 외 inactive"""
    now = now or datetime.utcnow()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT SUM(published_at >= %s), SUM(published_at >= %s)
            FROM contents
            WHERE channel_id = %s AND published_at IS NOT NULL
        """, (now - timedelta(days=180), now - timedelta(days=365), channel_id))
        cnt_180d, cnt_365d = cur.fetchone()
    if (cnt_180d or 0) >= 10:
        return 'active'
    if (cnt_365d or 0) >= 10:
        return 'low_active'
    return 'inactive'


def fetch_tab(crawl_url, tab, parser):
    """채널 탭 1개 fetch. 반환 (items|None, http_code|None, error_type|None)
    items=None이면 실패, items=[]는 '정상인데 콘텐츠 없음'."""
    url = crawl_url.rstrip("/") + f"/{tab}?hl=ko&gl=KR"
    try:
        resp = get_session().get(url, timeout=20)
    except Exception as e:
        return None, None, f"network:{type(e).__name__}"
    if resp.status_code != 200:
        return None, resp.status_code, f"http_{resp.status_code}"
    data = extract_yt_initial_data(resp.text)
    if data is None:
        return None, 200, "no_yt_data"
    return parser(data), 200, None


def log_row(cur, channel_id, url, status, http_status,
            error_type=None, error_detail=None, dur_ms=None):
    cur.execute("""
        INSERT INTO crawl_logs
          (channel_id, target_url, layer, status, http_status,
           error_type, error_detail, duration_ms)
        VALUES (%s, %s, 'L2', %s, %s, %s, %s, %s)
    """, (channel_id, url, status, http_status,
          error_type, (error_detail or "")[:500] or None, dur_ms))


def save_channel(channel_id, crawl_url, videos, shorts, dur_ms):
    """수집 결과 저장 — 채널 단위 단일 트랜잭션."""
    conn = pymysql.connect(**DB, autocommit=False)
    try:
        with conn.cursor() as cur:
            now_utc = datetime.now(timezone.utc)
            for kind, items in (("video", videos), ("shorts", shorts)):
                for v in items:
                    if not v.get("video_id"):
                        continue
                    # 정밀 게시일 보호: 근사값은 published_is_approx=1인 행만 갱신
                    cur.execute("""
                        INSERT INTO contents
                          (channel_id, external_id, content_type, published_at,
                           published_relative, published_is_approx,
                           duration_sec, caption_text)
                        VALUES (%s, %s, %s, %s, %s, 1, %s, %s)
                        ON DUPLICATE KEY UPDATE
                          published_at = IF(published_is_approx=1,
                                            VALUES(published_at), published_at),
                          published_relative = VALUES(published_relative),
                          duration_sec = COALESCE(VALUES(duration_sec), duration_sec)
                    """, (channel_id, v["video_id"], kind,
                          v.get("published_at_approx"), v.get("published_relative"),
                          v.get("duration_sec"), (v.get("title") or "")[:2000]))
                    cur.execute(
                        "SELECT content_id FROM contents "
                        "WHERE channel_id=%s AND external_id=%s",
                        (channel_id, v["video_id"]))
                    row = cur.fetchone()
                    if not row:
                        continue
                    cur.execute("""
                        INSERT INTO content_snapshots
                          (content_id, captured_at, view_count)
                        VALUES (%s, %s, %s)
                        ON DUPLICATE KEY UPDATE view_count=VALUES(view_count)
                    """, (row[0], now_utc, v.get("view_count")))

            if videos or shorts:
                activity = classify_activity(conn, channel_id)
            else:
                activity = 'inactive'   # 두 탭 모두 200 + 콘텐츠 0개일 때만 여기 도달
            cur.execute(
                "UPDATE channels SET channel_activity_status=%s WHERE channel_id=%s",
                (activity, channel_id))
            log_row(cur, channel_id, crawl_url, "success", 200, dur_ms=dur_ms)
        conn.commit()
        return activity
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_failure(channel_id, crawl_url, http_status, error_type, dur_ms):
    """일시 실패 기록 — 활동 상태는 절대 건드리지 않는다."""
    try:
        conn = pymysql.connect(**DB, autocommit=True)
        try:
            with conn.cursor() as cur:
                log_row(cur, channel_id, crawl_url, "failed", http_status,
                        error_type, dur_ms=dur_ms)
        finally:
            conn.close()
    except Exception:
        pass
    with lock:
        counter["fail"] += 1


def process_one(channel):
    channel_id, crawl_url = channel

    for attempt in range(MAX_429_RETRY + 1):
        if stop_flag.is_set():
            return

        # ── videos 탭 ──
        if not rc.acquire(stop_flag):
            return
        t0 = time.time()
        videos, v_code, v_err = fetch_tab(crawl_url, "videos", parse_l2_videos)
        if v_code in (429,):
            with lock:
                counter["rate_limited"] += 1
            if rc.report_429() >= STOP_ON_429:
                print("\n🛑 백오프 후에도 429 지속 — 전체 중단 (resume 자동).")
                stop_flag.set()
                return
            continue                      # 백오프 후 채널 재시도

        # ── shorts 탭 (429 동일 처리 — 기존 무시 버그 수정) ──
        if not rc.acquire(stop_flag):
            return
        shorts, s_code, s_err = fetch_tab(crawl_url, "shorts", parse_l2_shorts)
        if s_code in (429,):
            with lock:
                counter["rate_limited"] += 1
            if rc.report_429() >= STOP_ON_429:
                print("\n🛑 백오프 후에도 429 지속 — 전체 중단 (resume 자동).")
                stop_flag.set()
                return
            continue

        dur_ms = int((time.time() - t0) * 1000)
        rc.report_success()

        # ── 실패는 실패로: inactive 마킹 금지, failed 기록 후 종료 ──
        if videos is None and shorts is None:
            record_failure(channel_id, crawl_url, v_code or s_code,
                           v_err or s_err, dur_ms)
            break
        # 한쪽 탭만 실패: 불완전 데이터로 활동 분류하지 않는다.
        # failed로 기록해 다음 런에서 채널 전체를 재시도.
        if videos is None or shorts is None:
            record_failure(channel_id, crawl_url,
                           v_code if videos is None else s_code,
                           v_err if videos is None else s_err, dur_ms)
            break

        # ── 양쪽 모두 성공 → 저장 + 활동 분류 ──
        try:
            activity = save_channel(channel_id, crawl_url, videos, shorts, dur_ms)
            with lock:
                counter["ok"] += 1
        except Exception as e:
            print(f"    ⚠️ DB 오류 ch={channel_id}: {repr(e)[:140]}")
            record_failure(channel_id, crawl_url, None, "db_error", dur_ms)
            with lock:
                counter["db_error"] += 1
        break

    with lock:
        counter["done"] += 1
        d = counter["done"]
    if d % 50 == 0:
        print(f"  [{d}] ok={counter['ok']} fail={counter['fail']} "
              f"429={counter['rate_limited']} dberr={counter['db_error']} "
              f"req={rc.total_requests}")


def main():
    conn = pymysql.connect(**DB)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT channel_id, COALESCE(channel_url_normalized, channel_url_raw)
            FROM channels
            WHERE platform='youtube'
              AND channel_existence_status='normal'
              AND channel_id_status <> 'duplicate'
              AND channel_id NOT IN (
                  SELECT channel_id FROM crawl_logs
                  WHERE layer='L2' AND status='success' AND channel_id IS NOT NULL
              )
            ORDER BY channel_id
        """)
        channels = cur.fetchall()
    conn.close()

    if BATCH_LIMIT:
        channels = channels[:BATCH_LIMIT]

    total = len(channels)
    n_req = total * 2
    n_rests = (n_req - 1) // L2_REST_EVERY if L2_REST_EVERY else 0
    est_h = (n_req * L2A_MIN_INTERVAL + n_rests * L2_REST_SECONDS) / 3600
    print(f"L2a 대상 {total}개 (요청 ≈{n_req:,}) | WORKERS={L2A_WORKERS} "
          f"INTERVAL={L2A_MIN_INTERVAL}s | "
          f"{L2_REST_EVERY}요청당 {L2_REST_SECONDS//60}분 휴식 {n_rests}회 | "
          f"예상 ≥ {est_h:.1f}시간")
    print("무인 실행 팁: caffeinate -i python -m youtube.main --l2a\n")
    if total == 0:
        print("처리할 채널 없음.")
        return

    start = time.time()
    try:
        with ThreadPoolExecutor(max_workers=L2A_WORKERS) as ex:
            list(ex.map(process_one, channels))
    except KeyboardInterrupt:
        print("\n⏹️  중단 요청 — 진행 중인 채널만 마치고 종료합니다...")
        stop_flag.set()

    elapsed = time.time() - start
    print(f"\n=== L2a 완료: ok={counter['ok']} fail={counter['fail']} "
          f"429={counter['rate_limited']} dberr={counter['db_error']} "
          f"| {elapsed:.0f}초 ===")
    if stop_flag.is_set():
        print("⚠️ 중단됨. 성공한 채널은 skip되므로 재실행하면 이어서 갑니다.")


if __name__ == "__main__":
    main()