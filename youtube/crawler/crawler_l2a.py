"""
youtube/crawler/crawler_l2a.py (v2) — 영상/쇼츠 목록 수집 + 활동 분류

무슨 일을 하는가
------
채널의 /videos 탭과 /shorts 탭을 긁어서 두 가지를 만든다.

  1) contents 테이블 — 이후 모든 단계의 뼈대.
     L2b는 이 목록의 영상을 하나씩 방문하고, L3는 여기서 댓글 대상을 고른다.
  2) 활동성 판정 — 이 채널이 지금도 활발한지.
     L2b의 수집 우선순위와 metric의 대상 선별이 여기서 갈린다.

채널당 정확히 2요청이다.
이게 L2b(영상당 1요청, 채널당 최대 30)와의 결정적 차이다.
요청 2번으로 콘텐츠 15~30개를 얻으니 효율이 8배 좋고,
그래서 L2a는 9시간에 429 0건으로 끝났지만 L2b는 IP 차단을 당했다.

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

실측: 채널 7,219개 → 성공 7,261 / db_error 1 / 429 0건.
      콘텐츠 223,392건 (video 105,042 / shorts 118,350). 약 9시간.

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
from zoneinfo import ZoneInfo

# pymysql은 datetime의 tzinfo를 버리고 숫자만 문자열로 만든다.
# UTC로 넘기면 MySQL(+09:00)이 KST로 해석해 9시간 어긋난다.
KST = ZoneInfo("Asia/Seoul")

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
try:
    from youtube.config import L2_REFRESH_DAYS
except ImportError:
    L2_REFRESH_DAYS = 7

lock = threading.Lock()
counter = {"done": 0, "ok": 0, "fail": 0, "rate_limited": 0, "db_error": 0}
stop_flag = threading.Event()

# L1은 자체 GlobalRateLimiter를 갖고 있지만, 여기서는 모듈화된 것을 쓴다.
# (L1의 패턴을 rate_control.py로 뺐는데 L1은 이관을 안 한 상태)
rc = RateController(L2A_MIN_INTERVAL, BACKOFF_BASE,
                    rest_every=L2_REST_EVERY, rest_seconds=L2_REST_SECONDS,
                    name="L2a")


def classify_activity(conn, channel_id, now=None):
    """DB contents 기준 활동성 판정.

    쇼츠는 목록 페이지에 날짜가 없어 L2a 시점에는 published_at이 NULL이다.
    따라서 이 시점의 판정은 롱폼 기준 '잠정치'이며,
    L2b가 쇼츠 게시일을 채운 뒤 backfill이 재판정해 확정한다.

    ── 왜 2단계 판정인가 (순환 참조) ──
      L2b는 대상 선정에 활동성을 쓴다 (요청량이 8배라 전 채널을 못 돈다)
      활동성 확정에는 L2b가 채운 쇼츠 게시일이 필요하다
      → 서로를 기다리는 구조

      해결: L2a에서 잠정 판정 → L2b 수집 → backfill이 확정 판정
      (main.py PIPELINE에서 backfill이 metric 바로 앞에 있는 이유)

    ── 기준값 근거 (가이드라인) ──
      180일 10건 = "최근 6개월, 영상 10개 이상"
      365일      = "샘플 미달 시 기간을 2배 확장"
      dormant    = "최근 1년 이상 업로드 없음 → 수집 대상 제외"

    ※ 이 함수는 backfill_activity.py가 import해서 재사용한다.
      판정 로직이 한 곳에만 있어야 잠정치와 확정치의 기준이 같아진다.
    """
    now = now or datetime.now()
    with conn.cursor() as cur:
        # WHERE에 published_at IS NOT NULL이 없다.
        # 게시일 미상 쇼츠를 세려면 NULL 행도 조회 대상에 있어야 한다.
        cur.execute("""
            SELECT SUM(published_at >= %s), SUM(published_at >= %s),
                   MAX(published_at),
                   SUM(content_type='shorts' AND published_at IS NULL)
            FROM contents
            WHERE channel_id = %s
        """, (now - timedelta(days=180), now - timedelta(days=365), channel_id))
        cnt_180d, cnt_365d, last_pub, shorts_unknown = cur.fetchone()

    if (cnt_180d or 0) >= 10:
        return 'active'
    if (cnt_365d or 0) >= 10:
        return 'low_active'
    # 게시일 미상 쇼츠가 10개 이상이면 dormant로 단정할 근거가 없다.
    # (쇼츠만 올리는 채널을 '1년째 활동 없음'으로 오분류하는 것을 방지)
    #
    # 실측: dormant 2,220개 중 1,395개가 쇼츠 보유 채널이었다.
    # dormant는 원래 L2b 대상에서 빠져서, 한 번 잘못 찍히면
    # 쇼츠 게시일을 영영 못 받고 재판정도 불가능한 순환에 갇힌다.
    if (shorts_unknown or 0) >= 10:
        return 'inactive'
    if last_pub is not None and last_pub < now - timedelta(days=365):
        return 'dormant'
    # last_pub이 None(콘텐츠 0건)이면 dormant가 아니라 inactive.
    # "안 올린 것"과 "아직 못 걷은 것"을 구분할 수 없기 때문.
    return 'inactive'


def fetch_tab(crawl_url, tab, parser):
    """채널 탭 1개 fetch. 반환 (items|None, http_code|None, error_type|None)
    items=None이면 실패, items=[]는 '정상인데 콘텐츠 없음'.

    ★ 이 반환 규약이 v2 수정 1번의 핵심이다.

    파이썬에서 None과 []은 둘 다 falsy라 `if not items:`로 쓰면 구분이 안 된다.
    명시적으로 `is None`을 검사해야 "실패"와 "빈 채널"을 나눌 수 있다.

    구분 안 하면 벌어지는 일:
      네트워크 오류로 목록을 못 가져옴
        → "이 채널은 영상이 0개구나" → inactive로 마킹
        → success 로그 기록 → resume 때문에 다시 안 봄
        → 활발한 크리에이터가 영구히 사라짐
    """
    url = crawl_url.rstrip("/") + f"/{tab}?hl=ko&gl=KR"
    # hl=ko: 상대시간("3개월 전")을 한국어로 받아야 parse_relative_date가 읽는다.
    # (L1은 hl=en — 밴/삭제 감지 문구가 영어 기준으로 안정적이라서)
    try:
        resp = get_session().get(url, timeout=20)
    except Exception as e:
        return None, None, f"network:{type(e).__name__}"
    if resp.status_code != 200:
        return None, resp.status_code, f"http_{resp.status_code}"
    data = extract_yt_initial_data(resp.text)
    if data is None:
        # JSON을 못 찾음 = 구조 변경 신호일 수 있다.
        # 이 error_type이 대량으로 찍히면 유튜브가 바뀐 것.
        return None, 200, "no_yt_data"
    return parser(data), 200, None


def log_row(cur, channel_id, url, status, http_status,
            error_type=None, error_detail=None, dur_ms=None):
    """crawl_logs 기록.

    ⚠️ layer가 'L2'로 하드코딩돼 있다. L2b는 'L2b'로 기록하므로
    로그만 보면 'L2'가 L2a를 뜻한다는 걸 알 수 없다. (명명 일관성 문제)
    """
    cur.execute("""
        INSERT INTO crawl_logs
          (channel_id, target_url, layer, status, http_status,
           error_type, error_detail, duration_ms)
        VALUES (%s, %s, 'L2', %s, %s, %s, %s, %s)
    """, (channel_id, url, status, http_status,
          error_type, (error_detail or "")[:500] or None, dur_ms))


def save_channel(channel_id, crawl_url, videos, shorts, dur_ms):
    """수집 결과 저장 — 채널 단위 단일 트랜잭션.

    콘텐츠 INSERT + 스냅샷 INSERT + 활동성 UPDATE + 로그 INSERT가
    전부 들어가거나 전부 안 들어간다.
    (L1과 같은 이유 — 로그 없이 데이터만 남으면 resume이 무한 재시도한다)
    """
    conn = pymysql.connect(**DB, autocommit=False)
    try:
        with conn.cursor() as cur:
            now_kst = datetime.now(KST)   # 루프 밖에서 한 번. 같은 배치는 같은 시각.
            for kind, items in (("video", videos), ("shorts", shorts)):
                for v in items:
                    if not v.get("video_id"):
                        continue
                    # 정밀 게시일 보호: 근사값은 published_is_approx=1인 행만 갱신
                    #
                    # ★ v2 수정 2번. L2a는 "3개월 전"을 역산한 근사값을 넣고,
                    #   L2b는 watch 페이지에서 정확한 값을 받아 approx=0으로 바꾼다.
                    #   L2a를 재실행할 때 그냥 덮어쓰면 L2b가 애써 채운
                    #   정확한 값이 근사값으로 되돌아간다.
                    #   → 뒤 단계가 만든 더 정확한 데이터를 앞 단계가 망치지 않게.
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
                    # ON DUPLICATE 경로에서는 lastrowid를 신뢰할 수 없어 직접 조회.
                    cur.execute(
                        "SELECT content_id FROM contents "
                        "WHERE channel_id=%s AND external_id=%s",
                        (channel_id, v["video_id"]))
                    row = cur.fetchone()
                    if not row:
                        continue
                    # L2a는 조회수만 넣는다. 좋아요·댓글수는 목록 페이지에 없어서
                    # L2b가 watch 페이지에서 채운다.
                    cur.execute("""
                        INSERT INTO content_snapshots
                          (content_id, captured_at, view_count)
                        VALUES (%s, %s, %s)
                        ON DUPLICATE KEY UPDATE view_count=VALUES(view_count)
                    """, (row[0], now_kst, v.get("view_count")))

            if videos or shorts:
                activity = classify_activity(conn, channel_id)
            else:
                activity = 'inactive'   # 두 탭 모두 200 + 콘텐츠 0개일 때만 여기 도달
                # ↑ process_one에서 실패 케이스를 이미 걸러냈으므로,
                #   여기 도달했다면 "정말로 빈 채널"이 확정된 상태다.
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
    """일시 실패 기록 — 활동 상태는 절대 건드리지 않는다.

    ★ 함수 이름과 주석이 곧 v2 수정 1번의 결론이다.
    실패했을 때 channels를 UPDATE하지 않는 것이 핵심.
    로그만 남기면 다음 실행에서 자연스럽게 재시도된다.
    """
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
    """채널 1개 = 요청 2번 (videos 탭 + shorts 탭)."""
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
            # ↑ continue가 for attempt 루프로 돌아가므로 채널을 '처음부터'
            #   다시 한다. videos를 이미 받았어도 버린다.
            #   짝이 맞는 데이터로만 판정하기 위해서.

        # ── shorts 탭 (429 동일 처리 — 기존 무시 버그 수정) ──
        # v2 수정 3번. 이전 버전은 videos의 429만 처리하고 shorts는 그냥 넘어갔다.
        # 그러면 shorts가 차단당해도 videos만으로 판정해버린다.
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
        #
        # 롱폼만 받고 쇼츠를 못 받았는데 판정하면 쇼츠 중심 채널이 저평가된다.
        # 절반의 데이터로 내린 판정은 없느니만 못하다.
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
            # 예외 격리: 채널 1건의 DB 문제로 전체 런이 죽지 않게
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
        # 대상 = L1이 'normal'로 확정한 채널 중 최근 7일 내 L2 성공이 없는 것.
        # L1의 판정 결과에 의존한다 (deleted/suspended는 애초에 안 들어옴).
        cur.execute("""
            SELECT channel_id, COALESCE(channel_url_normalized, channel_url_raw)
            FROM channels
            WHERE platform='youtube'
              AND channel_existence_status='normal'
              AND channel_id_status <> 'duplicate'
              AND channel_id NOT IN (
                  SELECT channel_id FROM crawl_logs
                  WHERE layer='L2' AND status='success' AND channel_id IS NOT NULL
                    AND attempted_at >= NOW() - INTERVAL %s DAY
              )
            ORDER BY channel_id
        """, (L2_REFRESH_DAYS,))
        channels = cur.fetchall()
    conn.close()

    if BATCH_LIMIT:
        channels = channels[:BATCH_LIMIT]

    # 채널당 2요청이라 n_req = total * 2. 예상 시간을 미리 알려준다.
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