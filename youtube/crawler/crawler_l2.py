"""
crawler_l2.py (L2b, v2) — 영상 개별 watch 페이지 정밀 수집

기존 버전에서 수정한 문제:
  1) [침묵 소실] 워커 예외를 f.result()로 소비하지 않아 DB 에러 시 채널이
     로그 없이 증발 → 무한 재시도 루프. 예외 격리 + db_error 기록으로 수정.
  2) [허위 성공] 429/중단으로 영상 일부만 처리해도 success 기록
     → 채널 내 전 영상을 끝냈을 때만 success. 중간 중단은 기록 없이 재시도.
  3) 스레드별 sleep(워커 10개 = 초당 ~12건, 최대 트래픽원이 최고 속도)
     → 전역 RateController (간격/백오프/휴식/slow start).
  4) 파싱 실패 시 category/duration/published_at을 NULL로 덮어쓰던 버그
     → COALESCE 보호.
  5) 429 시 채널 포기 → 백오프 후 같은 영상부터 재개.

config.py 권장 추가:
  L2B_MIN_INTERVAL = 1.0     # watch 페이지는 가볍지만 물량이 최대 → 보수적으로
  (L2_REST_EVERY / L2_REST_SECONDS 는 L2a와 공유)
"""
import time
import threading
import pymysql
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

from youtube.crawler.lib.youtube_parser import parse_watch_page
from youtube.crawler.lib.rate_control import RateController
from youtube.config import DB, L2B_WORKERS, BATCH_LIMIT, STOP_ON_429

try:
    from youtube.config import L2B_MIN_INTERVAL
except ImportError:
    L2B_MIN_INTERVAL = 1.0
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
try:
    from youtube.config import L2_REFRESH_DAYS
except ImportError:
    L2_REFRESH_DAYS = 7
RECENT_N = 15
MAX_429_RETRY = 3        # 영상 1개당 429 재시도 횟수

lock = threading.Lock()
counter = {"done": 0, "ok": 0, "videos": 0, "vfail": 0,
           "rate_limited": 0, "db_error": 0}
stop_flag = threading.Event()
rc = RateController(L2B_MIN_INTERVAL, BACKOFF_BASE,
                    rest_every=L2_REST_EVERY, rest_seconds=L2_REST_SECONDS,
                    name="L2b")


def fetch_one_video(video_id):
    """watch 페이지 1개. 429는 백오프 후 재시도. 반환 (result|None, 'stop'|None)"""
    for _ in range(MAX_429_RETRY + 1):
        if not rc.acquire(stop_flag):
            return None, "stop"
        try:
            result, code = parse_watch_page(video_id)
        except Exception:
            result, code = None, None
        if code == 429:
            with lock:
                counter["rate_limited"] += 1
            if rc.report_429() >= STOP_ON_429:
                print("\n🛑 백오프 후에도 429 지속 — 전체 중단 (resume 자동).")
                stop_flag.set()
                return None, "stop"
            continue                     # 백오프 해제 후 같은 영상 재시도
        rc.report_success()
        return result, None              # result=None이면 이 영상만 실패
    return None, None                    # 재시도 소진


def process_channel(channel):
    channel_id, = channel
    if stop_flag.is_set():
        return

    try:
        conn = pymysql.connect(**DB, autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT content_id, external_id FROM contents
                    WHERE channel_id=%s AND content_type='video'
                    ORDER BY published_at DESC, content_id DESC LIMIT %s
                """, (channel_id, RECENT_N))
                videos = list(cur.fetchall())
                cur.execute("""
                    SELECT content_id, external_id FROM contents
                    WHERE channel_id=%s AND content_type='shorts'
                      AND published_at IS NULL
                    ORDER BY content_id DESC LIMIT %s
                """, (channel_id, RECENT_N))
                videos.extend(cur.fetchall())
                # 콘텐츠 없음 = 수집 실패, success 기록 금지
                if not videos:
                    cur.execute("""
                        INSERT INTO crawl_logs
                        (channel_id, target_url, layer, status, http_status,
                        error_type, error_detail)
                        VALUES (%s, %s, 'L2b', 'failed', NULL, %s, %s)        
                        """, (channel_id, f"channel_{channel_id}",
                              "no_contents", "L2a 미완료 또는 콘텐츠 0건"))
                    with lock:
                        counter["done"] += 1
                    return
                for content_id, video_id in videos:
                    if stop_flag.is_set():
                        return               # ← 중간 중단: success 기록 없이 종료
                    result, sig = fetch_one_video(video_id)
                    if sig == "stop":
                        return
                    if not result:
                        with lock:
                            counter["vfail"] += 1
                        continue

                    # COALESCE 보호: 파싱 실패 필드가 기존 값을 지우지 않게
                    cur.execute("""
                        UPDATE contents SET
                          category = COALESCE(%s, category),
                          published_at = COALESCE(%s, published_at),
                          published_is_approx =
                              IF(%s IS NULL, published_is_approx, 0),
                          duration_sec = COALESCE(%s, duration_sec),
                          is_paid_promotion = %s
                        WHERE content_id=%s
                    """, (result.get("category"), result.get("published_at"),
                          result.get("published_at"), result.get("duration_sec"),
                          result.get("is_paid_promotion", False), content_id))

                    cur.execute("""
                        INSERT INTO content_snapshots
                          (content_id, captured_at, view_count,
                           like_count, comment_count)
                        VALUES (%s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                          view_count=VALUES(view_count),
                          like_count=VALUES(like_count),
                          comment_count=VALUES(comment_count)
                    """, (content_id, datetime.now(KST),
                          result.get("view_count"), result.get("like_count"),
                          result.get("comment_count")))
                    with lock:
                        counter["videos"] += 1

                # ── 전 영상 완주했을 때만 success ──
                cur.execute("""
                    INSERT INTO crawl_logs
                      (channel_id, target_url, layer, status, http_status, duration_ms)
                    VALUES (%s, %s, 'L2b', 'success', 200, NULL)
                """, (channel_id, f"channel_{channel_id}"))
                with lock:
                    counter["ok"] += 1
        finally:
            conn.close()

    except Exception as e:
        # 예외 격리: 채널 1건의 DB 문제로 전체가 죽거나 침묵 소실되지 않게
        print(f"    ⚠️ DB 오류 ch={channel_id}: {repr(e)[:140]}")
        try:
            conn2 = pymysql.connect(**DB, autocommit=True)
            try:
                with conn2.cursor() as cur:
                    cur.execute("""
                        INSERT INTO crawl_logs
                          (channel_id, target_url, layer, status, http_status,
                           error_type, error_detail)
                        VALUES (%s, %s, 'L2b', 'failed', NULL, 'db_error', %s)
                    """, (channel_id, f"channel_{channel_id}", repr(e)[:500]))
            finally:
                conn2.close()
        except Exception:
            pass
        with lock:
            counter["db_error"] += 1

    with lock:
        counter["done"] += 1
        d = counter["done"]
    if d % 20 == 0:
        print(f"  [{d}] ch_ok={counter['ok']} videos={counter['videos']} "
              f"vfail={counter['vfail']} 429={counter['rate_limited']} "
              f"dberr={counter['db_error']} req={rc.total_requests:,}")


def main():
    conn = pymysql.connect(**DB)
    with conn.cursor() as cur:
        # active 우선 처리 → 가치 높은 데이터부터 확보
        cur.execute("""
            SELECT channel_id FROM channels
            WHERE platform='youtube'
              AND channel_activity_status IN ('active','low_active','inactive')
              AND channel_id_status <> 'duplicate'
              AND channel_id NOT IN (
                  SELECT channel_id FROM crawl_logs
                  WHERE layer='L2b' AND status='success' AND channel_id IS NOT NULL
                    AND attempted_at >= NOW() - INTERVAL %s DAY
              )
            ORDER BY FIELD(channel_activity_status,
                           'active','low_active','inactive'), channel_id
        """, (L2_REFRESH_DAYS))
        channels = cur.fetchall()
    conn.close()

    if BATCH_LIMIT:
        channels = channels[:BATCH_LIMIT]

    total = len(channels)
    est_req = total * 20   # 채널당 대략 video 15 + shorts 몇 개
    n_rests = (est_req - 1) // L2_REST_EVERY if L2_REST_EVERY else 0
    est_h = (est_req * L2B_MIN_INTERVAL + n_rests * L2_REST_SECONDS) / 3600
    print(f"L2b 대상 채널 {total}개 (요청 대략 {est_req:,}) | "
          f"WORKERS={L2B_WORKERS} INTERVAL={L2B_MIN_INTERVAL}s | "
          f"{L2_REST_EVERY}요청당 {L2_REST_SECONDS//60}분 휴식 | "
          f"예상 ≥ {est_h:.1f}시간")
    print("무인 실행 팁: caffeinate -i python -m youtube.main --l2b\n")
    if total == 0:
        print("처리할 채널 없음.")
        return

    start = time.time()
    try:
        with ThreadPoolExecutor(max_workers=L2B_WORKERS) as ex:
            list(ex.map(process_channel, channels))
    except KeyboardInterrupt:
        print("\n⏹️  중단 요청 — 진행 중인 채널만 마치고 종료합니다...")
        stop_flag.set()

    elapsed = time.time() - start
    print(f"\n=== L2b 완료: 채널 {counter['ok']} | 영상 {counter['videos']} "
          f"| 영상실패 {counter['vfail']} | 429={counter['rate_limited']} "
          f"dberr={counter['db_error']} | {elapsed:.0f}초 ===")
    if stop_flag.is_set():
        print("⚠️ 중단됨. 완주한 채널만 skip되므로 재실행하면 이어서 갑니다.")


if __name__ == "__main__":
    main()