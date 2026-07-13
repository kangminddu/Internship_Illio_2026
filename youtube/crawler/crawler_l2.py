import time
import threading
import pymysql
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# 모듈 import 확인 필요
from crawler.lib.youtube_parser import parse_watch_page

# ==========================================
# 환경 설정
# ==========================================
from config import DB, L2B_WORKERS, L2B_DELAY, L2_RECENT_MONTHS

RECENT_N = 15            # 채널당 최근 영상 15개
BATCH_LIMIT = None       # 테스트 필요시 숫자 입력
STOP_ON_429 = 5

lock = threading.Lock()
counter = {"videos": 0, "ok": 0, "fail": 0, "rate_limited": 0}
stop_flag = threading.Event()

def process_channel(channel):
    channel_id, = channel
    if stop_flag.is_set():
        return

    conn = pymysql.connect(**DB, autocommit=True)
    try:
        with conn.cursor() as cur:
            # 최근 15개 영상 조회
            cur.execute("""
            SELECT content_id, external_id, content_type
            FROM contents
            WHERE channel_id=%s
            AND content_type='video'
            ORDER BY published_at DESC
            LIMIT %s
            """, (channel_id, RECENT_N))

            videos = list(cur.fetchall())

            cur.execute("""
            SELECT content_id, external_id, content_type
            FROM contents
            WHERE channel_id=%s
            AND content_type='shorts'
            AND published_at IS NULL
            ORDER BY content_id DESC
            """, (channel_id,))

            videos.extend(cur.fetchall())

            video_cnt = 0
            shorts_cnt = 0
            for content_id, video_id, content_type in videos:
                if content_type == "video":
                    video_cnt += 1
                else:
                    shorts_cnt += 1
                if stop_flag.is_set():
                    break
                try:
                    result, code = parse_watch_page(video_id)
                except Exception:
                    result, code = None, None

                # 429(Too Many Requests) 대응
                if code in (429, 403):
                    with lock:
                        counter["rate_limited"] += 1
                        if counter["rate_limited"] >= STOP_ON_429:
                            stop_flag.set()
                    return

                if result:
                    # [최적화] contents 테이블 업데이트
                    cur.execute("""
                        UPDATE contents SET category=%s,
                          published_at=COALESCE(%s, published_at),
                          published_is_approx=IF(%s IS NULL, published_is_approx, 0),
                          duration_sec = %s,
                          is_paid_promotion = %s
                        WHERE content_id=%s
                    """, (result["category"], result["published_at"],
                          result["published_at"], result.get("duration_sec"),
                          result.get("is_paid_promotion", False), content_id))

                    # [최적화] content_snapshots 테이블 UPSERT (중복 방지)
                    cur.execute("""
                        INSERT INTO content_snapshots
                          (content_id, captured_at, view_count, like_count, comment_count)
                        VALUES (%s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                          captured_at = VALUES(captured_at),
                          view_count = VALUES(view_count),
                          like_count = VALUES(like_count),
                          comment_count = VALUES(comment_count)
                    """, (content_id, datetime.now(timezone.utc),
                          result["view_count"], result["like_count"], result["comment_count"]))

                    with lock:
                        counter["videos"] += 1
                else:
                    with lock:
                        counter["fail"] += 1
                time.sleep(L2B_DELAY)

            # 성공 로그 기록
            cur.execute("""
                INSERT INTO crawl_logs (channel_id, target_url, layer, status, http_status)
                VALUES (%s, %s, 'L2b', 'success', 200)
            """, (channel_id, f"channel_{channel_id}"))
            
            with lock:
                counter["ok"] += 1
            print(
                f"OK ch={channel_id} | "
                f"영상 {video_cnt}개 | "
                f"쇼츠 {shorts_cnt}개 | "
                f"총 {len(videos)}개"
            )
    finally:
        conn.close()

def main():
    conn = pymysql.connect(**DB)
    with conn.cursor() as cur:
        # 이미 L2b 작업이 완료된 채널은 제외하고 수집
        cur.execute("""
            SELECT channel_id FROM channels
            WHERE platform='youtube'
              AND channel_activity_status IN ('active','low_active', 'inactive')
              AND channel_id NOT IN (
                  SELECT channel_id FROM crawl_logs WHERE layer='L2b' AND channel_id IS NOT NULL
              )
            ORDER BY channel_id
        """)
        channels = cur.fetchall()
    conn.close()

    if BATCH_LIMIT:
        channels = channels[:BATCH_LIMIT]

    total = len(channels)
    print(f"L2b 대상 채널 {total}개 | WORKERS={L2B_WORKERS}\n")
    
    with ThreadPoolExecutor(max_workers=L2B_WORKERS) as ex:
        futures = [ex.submit(process_channel, ch) for ch in channels]
        for f in as_completed(futures):
            if stop_flag.is_set(): break

    print(f"\n=== L2b 완료: 채널 {counter['ok']} | 영상 {counter['videos']} | 실패 {counter['fail']} ===")

if __name__ == "__main__":
    main()