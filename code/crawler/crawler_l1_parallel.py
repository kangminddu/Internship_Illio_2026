import time
import threading
import pymysql
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from crawler.lib.youtube_parser import fetch_channel_l1

from config import DB, WORKERS, DELAY, BATCH_LIMIT, STOP_ON_429

lock = threading.Lock()
counter = {"done": 0, "ok": 0, "fail": 0, "rate_limited": 0}
stop_flag = threading.Event()


def classify_existence(r):
    """실패 결과 → 존재 축 분류. error_type(l1_crawler 분류)을 활용."""
    sig = r.page_signal
    code = r.http_status
    et = r.error_type

    if sig == "channel_banned":
        return "suspended", "channel_banned"
    if sig == "channel_not_exist":
        return "deleted", "channel_deleted"
    if code == 404:
        return "deleted", "http_404"
    if code in (429, 403):
        return "unknown", "rate_limited"

    # l1_crawler가 세분화한 error_type 활용
    if et == "no_yt_data":
        return "unknown", "no_yt_data"
    if et == "about_missing":
        return "deleted", "about_missing_assumed_deleted"
    if et in ("retriable_timeout", "retriable_network"):
        return "unknown", et            # 재시도 대상 (삭제 단정 안 함)
    if et in ("parser_broken", "structure_changed"):
        return "unknown", et            # 긴급: 우리 코드 문제, 채널 상태 판단 보류
    return "unknown", et or "unknown_failure"


def process_one(channel):
    """워커 1개가 채널 1개 처리. 스레드마다 독립 DB 커넥션."""
    channel_id, creator_id, crawl_url, id_status = channel

    if stop_flag.is_set():
        return

    t0 = time.time()
    r = fetch_channel_l1(crawl_url)
    dur_ms = int((time.time() - t0) * 1000)

    # 429/403 → 누적 카운트, 임계 넘으면 전체 중단
    if r.http_status in (429, 403):
        with lock:
            counter["rate_limited"] += 1
            rl = counter["rate_limited"]
        print(f"    ⚠️  HTTP {r.http_status} (누적 {rl}/{STOP_ON_429}) ch={channel_id}")
        if rl >= STOP_ON_429:
            print(f"\n🛑 429/403 누적 {rl}회 — 차단 방지 위해 전체 중단.")
            stop_flag.set()
        return

    conn = pymysql.connect(**DB, autocommit=True)
    try:
        with conn.cursor() as cur:
            if r.ok:
                # 1) 스냅샷 append
                cur.execute("""
                    INSERT INTO channel_snapshots
                      (channel_id, captured_at, follower_count,
                       total_view_count, total_video_count)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      follower_count=VALUES(follower_count),
                      total_view_count=VALUES(total_view_count),
                      total_video_count=VALUES(total_video_count)
                """, (channel_id, datetime.now(timezone.utc),
                      r.subscriber_count, r.total_view_count, r.total_video_count))

                # 2) UC 확보되면 normalized를 canonical UC URL로 통일 (핸들 변경 대비)
                if r.external_channel_id:
                    canonical = f"https://www.youtube.com/channel/{r.external_channel_id}"
                    cur.execute(
                        "UPDATE channels SET channel_url_normalized=%s, external_channel_id=%s WHERE channel_id=%s",
                        (canonical, r.external_channel_id, channel_id))

                # 3) 채널명 + 개설일 + existence=normal
                if r.channel_name:
                    cur.execute(
                        "UPDATE channels SET channel_name=%s, channel_opened_at=%s, channel_existence_status='normal' WHERE channel_id=%s",
                        (r.channel_name, r.channel_opened_at, channel_id))
                    cur.execute(
                        "UPDATE creators SET nickname=%s WHERE creator_id=%s AND nickname LIKE 'G\\_%%'",
                        (r.channel_name, creator_id))
                else:
                    cur.execute(
                        "UPDATE channels SET channel_opened_at=%s, channel_existence_status='normal' WHERE channel_id=%s",
                        (r.channel_opened_at, channel_id))

                # 4) 성공 로그
                cur.execute("""
                    INSERT INTO crawl_logs
                      (channel_id, target_url, layer, status, http_status, duration_ms)
                    VALUES (%s, %s, 'L1', 'success', 200, %s)
                """, (channel_id, crawl_url, dur_ms))

                with lock:
                    counter["ok"] += 1
            else:
                new_existence, etype = classify_existence(r)
                cur.execute("""
                    INSERT INTO crawl_logs
                      (channel_id, target_url, layer, status, http_status,
                       error_type, error_detail, duration_ms)
                    VALUES (%s, %s, 'L1', 'failed', %s, %s, %s, %s)
                """, (channel_id, crawl_url, r.http_status, etype,
                      (r.error or "")[:500], dur_ms))

                if new_existence in ("deleted", "suspended"):
                    cur.execute(
                        "UPDATE channels SET channel_existence_status=%s WHERE channel_id=%s",
                        (new_existence, channel_id))

                with lock:
                    counter["fail"] += 1
    finally:
        conn.close()

    with lock:
        counter["done"] += 1
        d = counter["done"]
    if d % 50 == 0:
        print(f"  [{d}] ok={counter['ok']} fail={counter['fail']} rl={counter['rate_limited']}")

    time.sleep(DELAY)


def main():
    # resume: crawl_logs에 이미 있는 채널은 제외
    conn = pymysql.connect(**DB)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT channel_id, creator_id,
                   COALESCE(channel_url_normalized, channel_url_raw) AS crawl_url,
                   channel_id_status
            FROM channels
            WHERE platform='youtube'
              AND channel_id NOT IN (
                  SELECT channel_id FROM crawl_logs WHERE channel_id IS NOT NULL
              )
            ORDER BY channel_id
        """)
        channels = cur.fetchall()
    conn.close()

    if BATCH_LIMIT:
        channels = channels[:BATCH_LIMIT]

    total = len(channels)
    print(f"남은 채널 {total}개 | WORKERS={WORKERS} DELAY={DELAY}\n")
    if total == 0:
        print("처리할 채널 없음 (다 끝남).")
        return

    start = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(process_one, ch) for ch in channels]
        for f in as_completed(futures):
            if stop_flag.is_set():
                break

    elapsed = time.time() - start
    print(f"\n=== 완료: ok={counter['ok']} fail={counter['fail']} "
          f"rate_limited={counter['rate_limited']} | {elapsed:.0f}초 ===")
    if stop_flag.is_set():
        print("⚠️ 429 누적으로 중단됨. WORKERS 줄이거나 DELAY 늘려서 재시도.")


if __name__ == "__main__":
    main()