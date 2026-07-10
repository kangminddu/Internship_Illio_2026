import time
import threading
import pymysql
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

from crawler.lib.youtube_parser import get_session, extract_yt_initial_data, parse_l2_videos, parse_l2_shorts

from config import DB, L2A_WORKERS, L2A_DELAY
BATCH_LIMIT = None
STOP_ON_429 = 5

lock = threading.Lock()
counter = {"done": 0, "ok": 0, "fail": 0, "rate_limited": 0}
stop_flag = threading.Event()

RECENT_MONTHS = 6
MIN_VIDEOS = 10
DORMANT_DAYS = 365


def classify_activity(videos, now=None):
    if now is None:
        now = datetime.now()
    if not videos:
        return "inactive"
    dated = sorted([v["published_at_approx"] for v in videos if v.get("published_at_approx")], reverse=True)
    if not dated:
        return "unknown"
    latest = dated[0]
    if (now - latest).days > DORMANT_DAYS:
        return "dormant"
    if len(dated) >= MIN_VIDEOS:
        tenth = dated[MIN_VIDEOS - 1]
        if (now - tenth).days <= 180:
            return "active"
        if (now - tenth).days <= 365:
            return "low_active"
        return "inactive"
    else:
        return "inactive"


def fetch_videos(crawl_url):
    base = crawl_url.rstrip("/")
    url = base + "/videos?hl=ko&gl=KR"
    resp = get_session().get(url, timeout=20)
    if resp.status_code != 200:
        return None, resp.status_code
    data = extract_yt_initial_data(resp.text)
    if data is None:
        return None, resp.status_code
    return parse_l2_videos(data), 200


def fetch_shorts(crawl_url):
    base = crawl_url.rstrip("/")
    url = base + "/shorts?hl=ko&gl=KR"
    resp = get_session().get(url, timeout=20)
    if resp.status_code != 200:
        return None, resp.status_code
    data = extract_yt_initial_data(resp.text)
    if data is None:
        return None, resp.status_code
    return parse_l2_shorts(data), 200


def process_one(channel):
    channel_id, crawl_url = channel
    if stop_flag.is_set():
        return

    t0 = time.time()
    try:
        videos, code = fetch_videos(crawl_url)
    except Exception:
        videos, code = None, None
    dur_ms = int((time.time() - t0) * 1000)

    if code in (429, 403):
        with lock:
            counter["rate_limited"] += 1
            rl = counter["rate_limited"]
        print(f"    ⚠️ HTTP {code} (누적 {rl}) ch={channel_id}")
        if rl >= STOP_ON_429:
            stop_flag.set()
        return

    conn = pymysql.connect(**DB, autocommit=True)
    try:
        with conn.cursor() as cur:
            if videos:
                now = datetime.now()
                for v in videos:
                    if not v.get("video_id"):
                        continue
                    cur.execute("""
                        INSERT INTO contents
                          (channel_id, external_id, content_type, published_at,
                           published_relative, published_is_approx, duration_sec, caption_text)
                        VALUES (%s, %s, 'video', %s, %s, 1, %s, %s)
                        ON DUPLICATE KEY UPDATE
                          published_at=VALUES(published_at),
                          published_relative=VALUES(published_relative),
                          duration_sec=VALUES(duration_sec)
                    """, (channel_id, v["video_id"],
                          v["published_at_approx"], v["published_relative"],
                          v.get("duration_sec"),
                          (v.get("title") or "")[:2000]))

                    cur.execute("SELECT content_id FROM contents WHERE channel_id=%s AND external_id=%s",
                                (channel_id, v["video_id"]))
                    content_id = cur.fetchone()[0]
                    cur.execute("""
                        INSERT INTO content_snapshots (content_id, captured_at, view_count)
                        VALUES (%s, %s, %s)
                        ON DUPLICATE KEY UPDATE view_count=VALUES(view_count)
                    """, (content_id, datetime.now(timezone.utc), v.get("view_count")))
                # -----------------------------
                # Shorts 수집
                # -----------------------------
                try:
                    shorts, s_code = fetch_shorts(crawl_url)
                except Exception:
                    shorts, s_code = None, None
                if shorts:
                    for sv in shorts:
                        if not sv.get("video_id"):
                            continue
                        cur.execute("""
                                    INSERT INTO contents
                                    (channel_id, external_id, content_type, published_at,
                                    published_relative, published_is_approx, duration_sec, caption_text)
                                    VALUES (%s, %s, 'shorts', %s, %s, 1, %s, %s)
                                    ON DUPLICATE KEY UPDATE
                                    published_relative = VALUES(published_relative),
                                    duration_sec = VALUES(duration_sec)
                                    """, (
                                        channel_id,
                                        sv["video_id"],
                                        sv.get("published_at_approx"),
                                        sv.get("published_relative"),
                                        sv.get("duration_sec"),
                                        (sv.get("title") or "")[:2000],
                                    ))
                        cur.execute(
                            """"
                            SELECT content_id
                            FROM contents
                            WHERE channel_id=%s
                            AND external_id=%s
                            """,
                            (channel_id, sv["video_id"]),
                            )
                        row = cur.fetchone()
                        if not row:
                            continue
                        content_id = row[0]
                        cur.execute("""
                                    INSERT INTO content_snapshots
                                    (content_id, captured_at, view_count)
                                    VALUES(%s, %s, %s)
                                    ON DUPLICATE KEY UPDATE
                                    view_count = VALUES(view_count)
                                    """, (
                                        content_id,
                                        datetime.now(timezone.utc),
                                        sv.get("view_count"),
                                    ))
                    print(f"        + shorts {len(shorts)}개")
                activity = classify_activity(videos, now)
                cur.execute("UPDATE channels SET channel_activity_status=%s WHERE channel_id=%s",
                            (activity, channel_id))
                cur.execute("""
                    INSERT INTO crawl_logs (channel_id, target_url, layer, status, http_status, duration_ms)
                    VALUES (%s, %s, 'L2', 'success', 200, %s)
                """, (channel_id, crawl_url, dur_ms))

                with lock:
                    counter["ok"] += 1
                print(f"    OK ch={channel_id} | 영상 {len(videos)}개 | {activity}")
            else:
                cur.execute("UPDATE channels SET channel_activity_status='inactive' WHERE channel_id=%s",
                            (channel_id,))
                cur.execute("""
                    INSERT INTO crawl_logs (channel_id, target_url, layer, status, http_status, error_type, duration_ms)
                    VALUES (%s, %s, 'L2', 'success', %s, 'no_regular_videos', %s)
                """, (channel_id, crawl_url, code, dur_ms))
                with lock:
                    counter["ok"] += 1
                print(f"    OK ch={channel_id} | 영상 없음 → inactive")
    finally:
        conn.close()

    with lock:
        counter["done"] += 1
    time.sleep(L2A_DELAY)


def main():
    conn = pymysql.connect(**DB)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT channel_id, COALESCE(channel_url_normalized, channel_url_raw)
            FROM channels
            WHERE platform='youtube'
              AND channel_existence_status='normal'
              AND channel_id NOT IN (
                  SELECT channel_id FROM crawl_logs WHERE layer='L2' AND channel_id IS NOT NULL
              )
            ORDER BY channel_id
        """)
        channels = cur.fetchall()
    conn.close()

    if BATCH_LIMIT:
        channels = channels[:BATCH_LIMIT]

    total = len(channels)
    print(f"L2a 대상 {total}개 | WORKERS={L2A_WORKERS}\n")
    if total == 0:
        print("처리할 채널 없음.")
        return

    start = time.time()
    with ThreadPoolExecutor(max_workers=L2A_WORKERS) as ex:
        futures = [ex.submit(process_one, ch) for ch in channels]
        for f in as_completed(futures):
            if stop_flag.is_set():
                break

    elapsed = time.time() - start
    print(f"\n=== L2a 완료: ok={counter['ok']} fail={counter['fail']} "
          f"rate_limited={counter['rate_limited']} | {elapsed:.0f}초 ===")


if __name__ == "__main__":
    main()