"""
crawler_l1_parallel.py (v3)

v2(전역 rate limiter + 429 백오프)에 더해 다음 문제 수정:
  1) 중복 채널 처리: 같은 실채널(UC)이 DB에 여러 row로 존재할 때
     canonical UPDATE가 uq_platform_url/uq_platform_extid에 걸려 크래시하던 문제
     → 사전검사 + IntegrityError 이중 안전망으로 'duplicate' 마킹 후 계속
  2) 워커 예외 격리: 채널 1건의 DB 에러가 전체 런을 죽이던 문제
     → 실패 기록 후 다음 채널 진행
  3) 트랜잭션화: 스냅샷만 커밋되고 crawl_logs 전에 죽어 무한 크래시 루프가
     되던 문제 → save_result 전체를 단일 트랜잭션으로 (실패 시 롤백)
  4) Ctrl+C 정상 동작: KeyboardInterrupt 시 stop_flag로 전 워커 종료
  5) channel_opened_at 보존: 재수집에서 None이 와도 기존 값 유지 (COALESCE)
  6) duplicate 마킹된 row는 수집 대상에서 제외
  7) 주기적 휴식: L1_REST_EVERY개 처리마다 L1_REST_SECONDS 동안 전 워커 휴식
     (연속 세션 누적량이 IP 한도에 닿지 않게 세션을 잘게 끊음)
     재개 직후 30개는 2배 간격 워밍업. 무인 실행 시:
     caffeinate -i python -m youtube.main --l1
"""
import time
import random
import threading
import pymysql
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
KST = ZoneInfo("Asia/Seoul")
from youtube.crawler.lib.youtube_parser import fetch_channel_l1
from youtube.config import DB, L1_WORKERS, BATCH_LIMIT, STOP_ON_429

try:
    from youtube.config import L1_MIN_INTERVAL
except ImportError:
    L1_MIN_INTERVAL = 1.2
try:
    from youtube.config import L1_BACKOFF_BASE
except ImportError:
    L1_BACKOFF_BASE = 60
try:
    from youtube.config import L1_MAX_RETRY
except ImportError:
    L1_MAX_RETRY = 3
try:
    from youtube.config import L1_REST_EVERY
except ImportError:
    L1_REST_EVERY = 1000
try:
    from youtube.config import L1_REST_SECONDS
except ImportError:
    L1_REST_SECONDS = 2100      # 35분

try:
    from youtube.config import L1_REFRESH_DAYS
except ImportError:
    L1_REFRESH_DAYS = 7

lock = threading.Lock()
counter = {"done": 0, "ok": 0, "fail": 0, "rate_limited": 0,
           "http403": 0, "duplicate": 0, "db_error": 0}
stop_flag = threading.Event()


class GlobalRateLimiter:
    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._pause_until = 0.0
        self._backoff_level = 0
        self._warmup_left = 0          # 휴식 재개 후 천천히 시작할 요청 수

    def acquire(self):
        while not stop_flag.is_set():
            with self._lock:
                now = time.time()
                wait_pause = self._pause_until - now
                if wait_pause <= 0:
                    wait_slot = self._next_at - now
                    if wait_slot <= 0:
                        interval = self.min_interval
                        if self._warmup_left > 0:      # 재개 직후 워밍업: 2배 간격
                            interval *= 2
                            self._warmup_left -= 1
                        jitter = random.uniform(0, interval * 0.3)
                        self._next_at = now + interval + jitter
                        return True
                    sleep_for = wait_slot
                else:
                    sleep_for = min(wait_pause, 5.0)
            time.sleep(min(sleep_for, 5.0))
        return False

    def report_429(self):
        with self._lock:
            now = time.time()
            if now >= self._pause_until:
                self._backoff_level += 1
                pause = L1_BACKOFF_BASE * (2 ** (self._backoff_level - 1))
                self._pause_until = now + pause
                print(f"\n⏸️  HTTP 429 → 전 워커 {pause}초 일시정지 "
                      f"(백오프 라운드 {self._backoff_level}/{STOP_ON_429})")
            return self._backoff_level

    def rest(self, seconds):
        """주기적 휴식: 전 워커 공동 정지 + 재개 후 워밍업 예약."""
        with self._lock:
            now = time.time()
            if now >= self._pause_until:   # 이미 쉬는 중이면 중복 예약 안 함
                self._pause_until = now + seconds
                self._warmup_left = 30
                resume = time.strftime("%H:%M", time.localtime(now + seconds))
                print(f"\n😴 {counter['done']}개 처리 — {seconds//60}분 휴식 "
                      f"(재개 예정 {resume}, 재개 후 30개는 저속 워밍업)")

    def report_success(self):
        with self._lock:
            if self._backoff_level:
                print("✅ 정상 응답 재개 — 백오프 레벨 리셋")
            self._backoff_level = 0


limiter = GlobalRateLimiter(L1_MIN_INTERVAL)


def classify_existence(r):
    sig = r.page_signal
    code = r.http_status
    et = r.error_type
    if sig == "channel_banned":
        return "suspended", "channel_banned"
    if sig == "channel_not_exist":
        return "deleted", "channel_deleted"
    if code == 404:
        return "deleted", "http_404"
    if code == 403:
        return "unknown", "http_403"
    if et == "no_yt_data":
        return "unknown", "no_yt_data"
    if et == "about_missing":
        return "deleted", "about_missing_assumed_deleted"
    if et in ("retriable_timeout", "retriable_network"):
        return "unknown", et
    if et in ("parser_broken", "structure_changed"):
        return "unknown", et
    return "unknown", et or "unknown_failure"


def log_row(cur, channel_id, crawl_url, status, http_status,
            error_type=None, error_detail=None, dur_ms=None):
    cur.execute("""
        INSERT INTO crawl_logs
          (channel_id, target_url, layer, status, http_status,
           error_type, error_detail, duration_ms)
        VALUES (%s, %s, 'L1', %s, %s, %s, %s, %s)
    """, (channel_id, crawl_url, status, http_status,
          error_type, (error_detail or "")[:500] or None, dur_ms))


def mark_duplicate(cur, channel_id, crawl_url, other_id, uc, dur_ms):
    """같은 실채널을 가리키는 row가 이미 있음 → 이 row는 duplicate로 마킹."""
    cur.execute(
        "UPDATE channels SET channel_id_status='duplicate' WHERE channel_id=%s",
        (channel_id,))
    log_row(cur, channel_id, crawl_url, "failed", 200,
            "duplicate_channel", f"same channel as channel_id={other_id} ({uc})",
            dur_ms)
    with lock:
        counter["duplicate"] += 1
        counter["fail"] += 1


def save_result(channel_id, creator_id, crawl_url, r, dur_ms):
    """수집 결과 저장. 전체가 단일 트랜잭션 — 실패 시 롤백되어
    '스냅샷만 커밋되고 로그 없는' 반쪽 상태가 남지 않는다."""
    conn = pymysql.connect(**DB, autocommit=False)
    try:
        with conn.cursor() as cur:
            if r.ok:
                uc = r.external_channel_id

                # ── 중복 사전검사: 같은 UC를 이미 가진 다른 row가 있는가 ──
                if uc:
                    canonical = f"https://www.youtube.com/channel/{uc}"
                    cur.execute(
                        "SELECT channel_id FROM channels "
                        "WHERE platform='youtube' AND channel_id<>%s "
                        "AND (external_channel_id=%s OR channel_url_normalized=%s)",
                        (channel_id, uc, canonical))
                    dup = cur.fetchone()
                    if dup:
                        mark_duplicate(cur, channel_id, crawl_url, dup[0], uc, dur_ms)
                        conn.commit()
                        return

                cur.execute("""
                    INSERT INTO channel_snapshots
                      (channel_id, captured_at, follower_count,
                       total_view_count, total_video_count)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      follower_count=VALUES(follower_count),
                      total_view_count=VALUES(total_view_count),
                      total_video_count=VALUES(total_video_count)
                """, (channel_id, datetime.now(KST),
                      r.subscriber_count, r.total_view_count, r.total_video_count))

                if uc:
                    cur.execute(
                        "UPDATE channels SET channel_url_normalized=%s, "
                        "external_channel_id=%s, channel_id_status='resolved' "
                        "WHERE channel_id=%s",
                        (canonical, uc, channel_id))

                # 개설일은 None이 와도 기존 값을 지우지 않음 (COALESCE)
                if r.channel_name:
                    cur.execute(
                        "UPDATE channels SET channel_name=%s, "
                        "channel_opened_at=COALESCE(%s, channel_opened_at), "
                        "channel_existence_status='normal' WHERE channel_id=%s",
                        (r.channel_name, r.channel_opened_at, channel_id))
                    cur.execute(
                        "UPDATE creators SET nickname=%s "
                        "WHERE creator_id=%s AND nickname LIKE 'G\\_%%'",
                        (r.channel_name, creator_id))
                else:
                    cur.execute(
                        "UPDATE channels SET "
                        "channel_opened_at=COALESCE(%s, channel_opened_at), "
                        "channel_existence_status='normal' WHERE channel_id=%s",
                        (r.channel_opened_at, channel_id))

                if r.description:
                    cur.execute(
                        "UPDATE channels SET description=%s WHERE channel_id=%s",
                        (r.description, channel_id))

                log_row(cur, channel_id, crawl_url, "success", 200, dur_ms=dur_ms)
                with lock:
                    counter["ok"] += 1
            else:
                new_existence, etype = classify_existence(r)
                log_row(cur, channel_id, crawl_url, "failed", r.http_status,
                        etype, r.error, dur_ms)
                if new_existence in ("deleted", "suspended"):
                    cur.execute(
                        "UPDATE channels SET channel_existence_status=%s "
                        "WHERE channel_id=%s",
                        (new_existence, channel_id))
                with lock:
                    counter["fail"] += 1
        conn.commit()

    except pymysql.err.IntegrityError as e:
        # 레이스 안전망: 사전검사 이후 다른 워커가 같은 UC를 먼저 canonical로
        # 만든 경우 UPDATE가 1062로 실패할 수 있다 → 롤백 후 duplicate로 기록
        conn.rollback()
        if e.args and e.args[0] == 1062 and r.ok and r.external_channel_id:
            with conn.cursor() as cur:
                mark_duplicate(cur, channel_id, crawl_url, "?",
                               r.external_channel_id, dur_ms)
            conn.commit()
        else:
            raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_db_error(channel_id, crawl_url, err, dur_ms):
    """save_result 실패 시 최소한 실패 로그라도 남겨 무한 재시도 루프를 끊는다."""
    try:
        conn = pymysql.connect(**DB, autocommit=True)
        try:
            with conn.cursor() as cur:
                log_row(cur, channel_id, crawl_url, "failed", None,
                        "db_error", repr(err), dur_ms)
        finally:
            conn.close()
    except Exception:
        pass  # 로그조차 실패하면 다음 런에서 재시도되는 것을 허용


def process_one(channel):
    channel_id, creator_id, crawl_url, id_status = channel

    for attempt in range(L1_MAX_RETRY + 1):
        if stop_flag.is_set():
            return
        if not limiter.acquire():
            return

        t0 = time.time()
        r = fetch_channel_l1(crawl_url)
        dur_ms = int((time.time() - t0) * 1000)

        if r.http_status == 429:
            with lock:
                counter["rate_limited"] += 1
            level = limiter.report_429()
            if level >= STOP_ON_429:
                print(f"\n[STOP] 백오프 {level}라운드 후에도 429 지속 — 전체 중단. "
                      f"수 시간 뒤 재실행 권장 (resume은 crawl_logs 기준 자동).")
                stop_flag.set()
                return
            continue

        if r.http_status == 403:
            with lock:
                counter["http403"] += 1

        limiter.report_success()

        # ── 예외 격리: 채널 1건의 DB 문제로 전체 런이 죽지 않게 ──
        try:
            save_result(channel_id, creator_id, crawl_url, r, dur_ms)
        except Exception as e:
            print(f"    ⚠️ DB 오류 ch={channel_id}: {repr(e)[:140]}")
            record_db_error(channel_id, crawl_url, e, dur_ms)
            with lock:
                counter["db_error"] += 1
                counter["fail"] += 1
        break

    with lock:
        counter["done"] += 1
        d = counter["done"]
    if L1_REST_EVERY and d > 0 and d % L1_REST_EVERY == 0:
        limiter.rest(L1_REST_SECONDS)
    if d % 50 == 0:
        print(f"  [{d}] ok={counter['ok']} fail={counter['fail']} "
              f"429={counter['rate_limited']} 403={counter['http403']} "
              f"dup={counter['duplicate']} dberr={counter['db_error']}")


def main():
    conn = pymysql.connect(**DB)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT channel_id, creator_id,
                   COALESCE(channel_url_normalized, channel_url_raw) AS crawl_url,
                   channel_id_status
            FROM channels
            WHERE platform='youtube'
              AND channel_id_status <> 'duplicate'
              AND channel_existence_status NOT IN ('deleted','suspended')
              AND channel_id NOT IN (
                  SELECT channel_id FROM crawl_logs
                  WHERE layer='L1' AND status='success'
                    AND channel_id IS NOT NULL
                    AND attempted_at >= NOW() - INTERVAL %s DAY
              )
            ORDER BY channel_id
        """, (L1_REFRESH_DAYS,))
        channels = cur.fetchall()
    conn.close()

    if BATCH_LIMIT:
        channels = channels[:BATCH_LIMIT]

    total = len(channels)
    n_rests = (total - 1) // L1_REST_EVERY if L1_REST_EVERY else 0
    est_h = (total * L1_MIN_INTERVAL + n_rests * L1_REST_SECONDS) / 3600
    print(f"남은 채널 {total}개 | WORKERS={L1_WORKERS} "
          f"MIN_INTERVAL={L1_MIN_INTERVAL}s | "
          f"{L1_REST_EVERY}개당 {L1_REST_SECONDS//60}분 휴식 {n_rests}회 | "
          f"예상 소요 ≥ {est_h:.1f}시간")
    print("무인 실행 팁: caffeinate -i python -m youtube.main --l1 (맥 절전 방지)\n")
    if total == 0:
        print("처리할 채널 없음 (다 끝남).")
        return

    start = time.time()
    try:
        with ThreadPoolExecutor(max_workers=L1_WORKERS) as ex:
            list(ex.map(process_one, channels))
    except KeyboardInterrupt:
        print("\n[STOP]  중단 요청 — 진행 중인 채널만 마치고 종료합니다...")
        stop_flag.set()

    elapsed = time.time() - start
    print("\n" + "=" * 60)
    print(
        f"[DONE] "
        f"ok={counter['ok']} "
        f"fail={counter['fail']} "
        f"429={counter['rate_limited']} "
        f"403={counter['http403']} "
        f"dup={counter['duplicate']} "
        f"dberr={counter['db_error']} "
        f"time={elapsed:.0f}s"
    )
    print("=" * 60)

    if stop_flag.is_set():
        print("[INFO] 중단되었습니다. 다시 실행하면 이어서 진행합니다.")


if __name__ == "__main__":
    main()