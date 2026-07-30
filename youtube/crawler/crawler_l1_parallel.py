"""
youtube/crawler/crawler_l1_parallel.py (v3) — 채널 기본정보 수집

무슨 일을 하는가
------
seed가 넣어둔 'URL만 있는 껍데기' 채널 행을 하나씩 방문해서,
그 채널이 무엇인지 확정한다. 구체적으로 세 가지다:

  1) UC ID 확보  — 유튜브의 진짜 채널 식별자.
                   핸들(@name)은 바뀔 수 있지만 UC는 영구하다.
                   같은 채널이 /@name, /c/name 두 형태로 시드에 들어와도
                   UC를 비교하면 중복을 판별할 수 있다.
  2) 생존 여부   — 삭제/정지된 채널을 L2·L3에서 계속 긁으면 낭비다.
  3) 구독자 수 등 — 나중에 VPF(구독자 대비 조회율) 계산의 분모.

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

실측: 채널 9,043개 → 성공 7,246 / 404 1,021 / 삭제 508 / 중복 214 / 밴 51.
      429는 0건.
"""
import time
import random
import threading
import pymysql
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# pymysql은 datetime을 DB로 보낼 때 tzinfo를 버리고 '숫자만' 문자열로 만든다.
# UTC로 만든 시각을 넘기면 MySQL(default-time-zone=+09:00)이 그 숫자를
# KST로 해석해 9시간 어긋난다. 반드시 KST로 만들어 넘겨야 한다.
KST = ZoneInfo("Asia/Seoul")

from youtube.crawler.lib.youtube_parser import fetch_channel_l1
from youtube.config import DB, L1_WORKERS, BATCH_LIMIT, STOP_ON_429

# config에 값이 없어도 돌아가도록 폴백을 둔다.
# (초기 개발 중 config가 계속 바뀌던 흔적. 지금은 전부 config에 있어
#  except 블록은 실제로 도달하지 않는다 — 정리 대상)
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

# 워커 여러 개가 공유하는 상태. 반드시 lock으로 감싸고 갱신한다.
lock = threading.Lock()
counter = {"done": 0, "ok": 0, "fail": 0, "rate_limited": 0,
           "http403": 0, "duplicate": 0, "db_error": 0}

# 협조적 종료(cooperative shutdown) 신호.
# 강제로 스레드를 죽이면 진행 중인 트랜잭션이 반쪽으로 남는다.
# 각 워커가 요청 직전에 이 플래그를 확인하고 스스로 빠져나간다.
stop_flag = threading.Event()


class GlobalRateLimiter:
    """워커 수와 무관하게 'IP 기준' 요청 속도를 고정하는 장치.

    유튜브는 세 가지를 본다고 가정하고 각각에 대응했다:
      1) 초당 요청 수      → min_interval + jitter
      2) 차단 후 재시도    → 429 지수 백오프 (전 워커 공동 정지)
      3) 세션 누적 요청량  → 주기적 휴식 + 재개 후 slow start

    핵심은 '전역'이라는 점이다. 워커마다 sleep을 걸면 워커 수만큼
    속도가 빨라져서, 병렬을 늘릴수록 차단 위험이 커진다.
    여기서는 워커를 40개로 늘려도 처리량이 그대로다.
    워커의 역할은 "A가 파싱하는 동안 B가 요청을 보내는 것"뿐.
    """

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_at = 0.0        # 다음 요청 가능 시각
        self._pause_until = 0.0    # 전 워커 정지 종료 시각 (429 or 휴식)
        self._backoff_level = 0    # 연속 429 라운드 수
        self._warmup_left = 0      # 휴식 재개 후 천천히 시작할 요청 수

    def acquire(self):
        """요청 슬롯을 확보할 때까지 대기. stop_flag가 서면 False."""
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
                        # jitter를 섞는 이유: 정확히 1.2초마다 요청하면
                        # 기계처럼 규칙적이라 봇으로 탐지되기 쉽다.
                        jitter = random.uniform(0, interval * 0.3)
                        self._next_at = now + interval + jitter
                        return True
                    sleep_for = wait_slot
                else:
                    sleep_for = min(wait_pause, 5.0)
            # lock을 놓고 잔다. 5초로 쪼개는 이유는 긴 정지 중에도
            # stop_flag를 주기적으로 확인해 Ctrl+C에 반응하기 위함.
            time.sleep(min(sleep_for, 5.0))
        return False

    def report_429(self):
        """429 발생 → 전 워커 공동 백오프. 연속 라운드 수를 반환."""
        with self._lock:
            now = time.time()
            # 이미 정지 중이면 레벨을 올리지 않는다.
            # (워커 4개가 동시에 429를 받아도 백오프를 4단계 건너뛰지 않게)
            if now >= self._pause_until:
                self._backoff_level += 1
                pause = L1_BACKOFF_BASE * (2 ** (self._backoff_level - 1))  # 60→120→240
                self._pause_until = now + pause
                print(f"\n⏸️  HTTP 429 → 전 워커 {pause}초 일시정지 "
                      f"(백오프 라운드 {self._backoff_level}/{STOP_ON_429})")
            return self._backoff_level

    def rest(self, seconds):
        """주기적 휴식: 전 워커 공동 정지 + 재개 후 워밍업 예약.

        초당 속도가 아무리 느려도, 8시간을 쉬지 않고 긁으면 차단된다.
        세션을 잘게 끊어 '연속 요청량'을 관리하는 장치.
        """
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
    """수집 실패를 두 가지 정보로 분류한다.

    반환 (existence, error_type)
      existence  → channels 테이블에 저장. '다음 실행 대상 선정'에 쓰인다.
      error_type → crawl_logs에 저장. 나중에 원인 분석용.

    핵심은 'deleted/suspended'와 'unknown'의 구분이다.

      deleted/suspended = 확정. 채널이 없어졌다 → 영구 제외
      unknown           = 미확정. 일시적일 수 있다 → 다음 실행에서 재시도

    timeout이나 네트워크 오류를 deleted로 판정하면 멀쩡한 채널이
    영영 수집 대상에서 빠진다. 그래서 확신이 없으면 unknown으로 남긴다.

    ※ 이 원칙("데이터 없음"과 "가져오지 못함"의 구분)이 이 프로젝트
       전체를 관통한다. L2a는 fetch 실패를 '콘텐츠 0개'로 오인했고,
       L3는 차단 페이지를 '댓글 0개'로 오인해서 각각 고쳤다.
    """
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
        return "unknown", "http_403"          # 일시 차단일 수 있음
    if et == "no_yt_data":
        return "unknown", "no_yt_data"        # 유튜브 구조 변경 신호일 수도
    if et == "about_missing":
        return "deleted", "about_missing_assumed_deleted"
    if et in ("retriable_timeout", "retriable_network"):
        return "unknown", et                  # 명백한 일시 오류
    if et in ("parser_broken", "structure_changed"):
        return "unknown", et
    return "unknown", et or "unknown_failure"


def log_row(cur, channel_id, crawl_url, status, http_status,
            error_type=None, error_detail=None, dur_ms=None):
    """crawl_logs 기록. 이 로그가 곧 resume의 기준이 된다."""
    cur.execute("""
        INSERT INTO crawl_logs
          (channel_id, target_url, layer, status, http_status,
           error_type, error_detail, duration_ms)
        VALUES (%s, %s, 'L1', %s, %s, %s, %s, %s)
    """, (channel_id, crawl_url, status, http_status,
          error_type, (error_detail or "")[:500] or None, dur_ms))


def mark_duplicate(cur, channel_id, crawl_url, other_id, uc, dur_ms):
    """같은 실채널을 가리키는 row가 이미 있음 → 이 row는 duplicate로 마킹.

    시드 엑셀에 @침착맨과 youtube.com/c/chimchakman이 따로 들어오면
    별개 행이 되지만, L1이 열어보면 UC가 같다.
    나중에 들어온 쪽을 duplicate로 찍어 이후 단계에서 제외한다.
    """
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
    '스냅샷만 커밋되고 로그 없는' 반쪽 상태가 남지 않는다.

    왜 단일 트랜잭션인가 (v3 수정 3번):
      resume이 crawl_logs 기준이다. 스냅샷만 커밋되고 로그를 남기기 전에
      죽으면, 다음 실행에서 "이 채널 아직 안 했네" 하고 또 시도한다.
      또 죽으면 또 시도 → 무한 루프. 데이터는 쌓이는데 진행은 안 된다.
      전부 들어가거나 전부 안 들어가게 해야 이 상태가 안 생긴다.
    """
    conn = pymysql.connect(**DB, autocommit=False)   # ← 명시적 트랜잭션
    try:
        with conn.cursor() as cur:
            if r.ok:
                uc = r.external_channel_id

                # ── 중복 사전검사: 같은 UC를 이미 가진 다른 row가 있는가 ──
                # 이걸 안 하면 아래 canonical UPDATE가 UNIQUE 제약에 걸려
                # 1062 에러로 크래시한다.
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

                # 구독자/조회수/영상수는 시점마다 변한다 → 스냅샷으로 쌓는다.
                # uq_snap_channel_time(channel_id, captured_at) UNIQUE라
                # 시각이 다르면 새 행, 같으면 갱신된다.
                cur.execute("""
                    INSERT INTO channel_snapshots
                      (channel_id, captured_at, follower_count,
                       total_view_count, total_video_count)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      follower_count=VALUES(follower_count),
                      total_view_count=VALUES(total_view_count),
                      total_video_count=VALUES(total_video_count)
                """, (channel_id, datetime.now(KST),      # ← UTC 아님. 위 KST 주석 참고
                      r.subscriber_count, r.total_view_count, r.total_video_count))

                # UC를 찾았으면 URL을 표준형으로 바꾸고 resolved로 승격.
                # 다음 실행부터는 이 canonical URL로 요청한다.
                if uc:
                    cur.execute(
                        "UPDATE channels SET channel_url_normalized=%s, "
                        "external_channel_id=%s, channel_id_status='resolved' "
                        "WHERE channel_id=%s",
                        (canonical, uc, channel_id))

                # 개설일은 None이 와도 기존 값을 지우지 않음 (COALESCE)
                # 재수집에서 파싱이 실패하면 None이 오는데, 그걸로 덮으면
                # 이전에 잘 받아둔 값이 사라진다.
                if r.channel_name:
                    cur.execute(
                        "UPDATE channels SET channel_name=%s, "
                        "channel_opened_at=COALESCE(%s, channel_opened_at), "
                        "channel_existence_status='normal' WHERE channel_id=%s",
                        (r.channel_name, r.channel_opened_at, channel_id))
                    # seed가 임시로 넣은 닉네임(G_숫자)만 실제 채널명으로 교체.
                    # 사람이 정성껏 적어둔 닉네임은 건드리지 않는다.
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

                # description은 email 단계가 재사용한다.
                # (치지직 링크를 여기서 뽑아 API로 이메일을 찾는다 — 유튜브 재요청 없음)
                if r.description:
                    cur.execute(
                        "UPDATE channels SET description=%s WHERE channel_id=%s",
                        (r.description, channel_id))

                log_row(cur, channel_id, crawl_url, "success", 200, dur_ms=dur_ms)
                with lock:
                    counter["ok"] += 1
            else:
                # 실패 경로. 로그는 항상 남기고,
                # '확정' 실패일 때만 channels 상태를 바꾼다.
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
        # 레이스 안전망 (TOCTOU 대응).
        #
        # 위 사전검사(SELECT)를 통과해도 1062가 날 수 있다:
        #   워커 A: SELECT → 중복 없음
        #   워커 B: SELECT → 중복 없음   (A가 아직 커밋 안 함)
        #   워커 A: UPDATE → 성공
        #   워커 B: UPDATE → 1062 에러
        #
        # 검사(check)와 사용(use) 사이에 상태가 바뀌는 문제라,
        # 사전검사만으로는 절대 막을 수 없다.
        # 원칙: 사전검사는 최적화일 뿐이고, 실제 보증은 DB 제약이 한다.
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
    """save_result 실패 시 최소한 실패 로그라도 남겨 무한 재시도 루프를 끊는다.

    별도 커넥션을 쓰는 이유: save_result의 커넥션은 이미 롤백됐거나
    깨진 상태일 수 있어서, 그걸로 로그를 쓰려 하면 또 실패한다.
    """
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
    """채널 1개 처리. 워커 스레드가 이 함수를 반복 호출한다.

    흐름: 속도 슬롯 확보 → HTTP 요청 → DB 저장
    세 단계의 책임이 명확히 분리돼 있다.
      속도 제어 → GlobalRateLimiter
      요청/파싱 → youtube_parser.fetch_channel_l1 (L2a·L2b도 같은 파일 사용)
      저장      → save_result
    """
    channel_id, creator_id, crawl_url, id_status = channel

    for attempt in range(L1_MAX_RETRY + 1):
        if stop_flag.is_set():
            return
        if not limiter.acquire():   # 전역 속도 제어 통과 대기
            return

        t0 = time.time()
        r = fetch_channel_l1(crawl_url)
        dur_ms = int((time.time() - t0) * 1000)

        # 429는 파서가 즉시 반환한다(내부 재시도 없음).
        # 파서가 자체적으로 재시도하면 전역 limiter를 우회해서
        # 차단당한 상태에서 오히려 더 때리는 꼴이 된다.
        # → 전역 백오프가 전담하도록 책임을 분리했다.
        if r.http_status == 429:
            with lock:
                counter["rate_limited"] += 1
            level = limiter.report_429()
            if level >= STOP_ON_429:
                print(f"\n[STOP] 백오프 {level}라운드 후에도 429 지속 — 전체 중단. "
                      f"수 시간 뒤 재실행 권장 (resume은 crawl_logs 기준 자동).")
                stop_flag.set()
                return
            continue      # 백오프가 풀린 뒤 같은 채널 재시도

        if r.http_status == 403:
            with lock:
                counter["http403"] += 1

        limiter.report_success()

        # ── 예외 격리: 채널 1건의 DB 문제로 전체 런이 죽지 않게 ──
        # (v3 수정 2번. 9천 개를 8시간 돌리는데 1건 때문에 처음부터
        #  다시 시작하는 건 말이 안 된다)
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
    # 1,000개마다 35분 휴식. 세션을 잘게 끊어 누적 요청량을 관리한다.
    if L1_REST_EVERY and d > 0 and d % L1_REST_EVERY == 0:
        limiter.rest(L1_REST_SECONDS)
    if d % 50 == 0:
        print(f"  [{d}] ok={counter['ok']} fail={counter['fail']} "
              f"429={counter['rate_limited']} 403={counter['http403']} "
              f"dup={counter['duplicate']} dberr={counter['db_error']}")


def main():
    conn = pymysql.connect(**DB)
    with conn.cursor() as cur:
        # ── 대상 선정 = resume 로직 ──
        #
        # 이 SQL 하나가 곧 '이어서 실행' 기능이다.
        # 상태 파일도 체크포인트도 없다. crawl_logs만 보면 된다.
        #
        #   duplicate 제외        : 같은 채널을 두 번 긁지 않음
        #   deleted/suspended 제외: 없어진 채널을 매주 재요청하지 않음
        #   L1 success 로그 없음  : 아직 안 했거나, 7일이 지나 갱신할 때가 됨
        #
        # attempted_at 조건이 있는 이유: 가이드라인이 L1을 '주 1회' 갱신하라고
        # 규정한다. 이 조건이 없으면 한 번 성공한 채널은 영원히 건너뛰어
        # 구독자 시계열이 더 이상 쌓이지 않는다.
        #
        # COALESCE: 첫 실행에는 normalized가 비어 있어 raw URL을 쓴다.
        # L1이 UC를 찾아 normalized를 채우면 다음부터는 그걸 쓴다.
        # (자기가 만든 결과를 다음 실행의 입력으로 쓰는 구조)
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
        channels = channels[:BATCH_LIMIT]   # 테스트용 표본 제한

    # 8시간짜리 작업을 시작하기 전에 8시간 걸린다는 걸 알려준다.
    # 사소해 보이지만 "지금 시작하면 오늘 안에 끝나나"를 판단할 수 있다.
    total = len(channels)
    n_rests = (total - 1) // L1_REST_EVERY if L1_REST_EVERY else 0
    est_h = (total * L1_MIN_INTERVAL + n_rests * L1_REST_SECONDS) / 3600
    print(f"남은 채널 {total}개 | WORKERS={L1_WORKERS} "
          f"MIN_INTERVAL={L1_MIN_INTERVAL}s | "
          f"{L1_REST_EVERY}개당 {L1_REST_SECONDS//60}분 휴식 {n_rests}회 | "
          f"예상 소요 ≥ {est_h:.1f}시간")
    if total == 0:
        print("처리할 채널 없음 (다 끝남).")
        return

    start = time.time()
    try:
        # 크롤링은 I/O 바운드다. HTTP 응답을 기다리는 동안 GIL이 풀리므로
        # 프로세스가 아니라 스레드로 충분하다.
        # list()로 감싸는 이유: ex.map은 lazy 이터레이터라
        # 소비하지 않으면 워커 예외가 드러나지 않는다.
        with ThreadPoolExecutor(max_workers=L1_WORKERS) as ex:
            list(ex.map(process_one, channels))
    except KeyboardInterrupt:
        # 강제 종료가 아니라 협조적 종료.
        # 진행 중인 채널은 마치게 두어 반쪽 데이터가 남지 않게 한다.
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