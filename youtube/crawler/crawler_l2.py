"""
youtube/crawler/crawler_l2.py (L2b, v2) — 영상 개별 watch 페이지 정밀 수집

무슨 일을 하는가
------
L2a가 만든 영상 목록을 받아, 영상 하나하나의 watch 페이지를 연다.
목록 페이지가 주지 않는 것들을 여기서 얻는다:

  - 정확한 게시일 (목록은 "3개월 전" 상대시간뿐 → published_is_approx=0으로 확정)
  - 좋아요 수, 댓글 수 (목록에 아예 없음 → ER·Loyalty 계산에 필수)
  - 카테고리, 유료광고 여부 (가이드라인의 광고/일반 분리 지표에 필요)
  - 쇼츠 게시일 (쇼츠 탭에는 날짜가 표시되지 않는다)

★ 이 단계가 파이프라인 전체에서 가장 비싸다
------
    L2a : 채널당 2요청 → 콘텐츠 15~30개 확보
    L2b : 영상당 1요청 → 콘텐츠 1개
          = 같은 데이터에 8배의 요청

실제로 L2a가 끝난 직후 L2b를 0.5초 간격으로 시작했다가
5초 만에 429를 7번 받고 서버 IP가 구글에 차단됐다.
브라우저로 접속해도 "비정상적인 트래픽" 페이지가 뜬다.

교훈: 한 단계에서 검증된 안전 마진을 다른 단계에 그대로 적용할 수 없다.
목록 페이지와 개별 영상 페이지는 유튜브가 적용하는 제한도 다르다.

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

KST = ZoneInfo("Asia/Seoul")   # pymysql이 tzinfo를 버리므로 KST로 만들어 넘긴다

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

RECENT_N = 15            # 가이드라인: 롱폼 최대 15개 / 쇼츠 최대 15개
MAX_429_RETRY = 3        # 영상 1개당 429 재시도 횟수

lock = threading.Lock()
counter = {"done": 0, "ok": 0, "videos": 0, "vfail": 0,
           "rate_limited": 0, "db_error": 0}
stop_flag = threading.Event()
rc = RateController(L2B_MIN_INTERVAL, BACKOFF_BASE,
                    rest_every=L2_REST_EVERY, rest_seconds=L2_REST_SECONDS,
                    name="L2b")


def fetch_one_video(video_id):
    """watch 페이지 1개. 429는 백오프 후 재시도. 반환 (result|None, 'stop'|None)

    반환값이 튜플인 이유: 세 가지 결과를 구분해야 한다.
      (result, None)  성공
      (None,   None)  이 영상만 실패 (다음 영상으로)
      (None,  'stop') 전체 중단 신호 (채널 전체 포기)

    v2 수정 5번: 이전에는 429가 나면 채널 전체를 포기했다.
    지금은 백오프가 풀린 뒤 '같은 영상부터' 재개한다.
    (continue가 for 루프를 돌아 같은 video_id를 다시 요청)
    """
    for _ in range(MAX_429_RETRY + 1):
        if not rc.acquire(stop_flag):
            return None, "stop"
        try:
            result, code = parse_watch_page(video_id)
        except Exception:
            # 파서 예외를 여기서 삼킨다. 영상 1개 파싱 실패로
            # 채널 전체를 날리지 않기 위해서.
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
    """채널 1개 = 영상 최대 30개 (롱폼 15 + 쇼츠 15) 처리."""
    channel_id, activity = channel

    # ── dormant 채널 특별 처리 ──
    #
    # 쇼츠는 목록 페이지에 날짜가 없어 L2a 시점에 published_at이 NULL이다.
    # → 활동성이 롱폼만 보고 판정된다
    # → 쇼츠만 올리는 채널이 dormant로 오분류될 수 있다
    # → dormant는 원래 L2b 대상에서 빠져 쇼츠 게시일을 영영 못 받는다
    # → 재판정도 불가능한 순환에 갇힌다
    #
    # 그래서 dormant 채널은 '쇼츠만' 수집한다(롱폼 스킵 = 요청 절반).
    # 로그 레이어를 'L2b_shorts'로 분리하는 이유:
    #   나중에 backfill이 재판정해서 active로 승격되면,
    #   layer='L2b' 성공 기록이 없으므로 대상에 다시 포함되어
    #   롱폼까지 수집된다. 같은 레이어로 기록했다면 7일 갱신 주기에 걸려
    #   못 갔을 것이다.
    is_dormant = (activity == 'dormant')
    layer = 'L2b_shorts' if is_dormant else 'L2b'
    if stop_flag.is_set():
        return

    try:
        conn = pymysql.connect(**DB, autocommit=True)
        try:
            with conn.cursor() as cur:
                # dormant는 쇼츠 게시일 보강만 — 롱폼은 건너뜀
                videos = []
                if not is_dormant:
                    # tie-breaker(content_id DESC)가 필요한 이유:
                    # L2a가 넣은 published_at은 "3개월 전"을 30일×3으로
                    # 역산한 근사값이라, 같은 달 영상들이 완전히 동일한
                    # timestamp를 갖는다. published_at만으로 정렬하면
                    # MySQL이 동률 순서를 보장하지 않아 실행할 때마다
                    # 다른 15개가 뽑힌다. → 재현성이 없어진다.
                    cur.execute("""
                        SELECT content_id, external_id FROM contents
                        WHERE channel_id=%s AND content_type='video'
                        ORDER BY published_at DESC, content_id DESC LIMIT %s
                    """, (channel_id, RECENT_N))
                    videos = list(cur.fetchall())
                # 쇼츠는 published_at IS NULL인 것만 = 아직 게시일을 모르는 것.
                # ⚠️ 한 번 채워지면 재수집 대상에서 빠진다.
                #    가이드라인의 "L2 주 1회 업데이트"와 어긋나지만,
                #    조건을 빼면 요청량이 크게 늘어 현재 유지 중.
                cur.execute("""
                    SELECT content_id, external_id FROM contents
                    WHERE channel_id=%s AND content_type='shorts'
                      AND published_at IS NULL
                    ORDER BY content_id DESC LIMIT %s
                """, (channel_id, RECENT_N))
                videos.extend(cur.fetchall())

                # 콘텐츠 없음 = 수집 실패, success 기록 금지
                #
                # 이 가드가 없으면: L2a를 안 돌린 상태에서 L2b를 실행하면
                # videos가 빈 리스트 → for 루프 0회 → 아래 success 기록.
                # 그리고 resume이 그 채널을 영구 제외한다.
                # (L2a 미완료 상태에서 --l2b를 잘못 치면 전 채널이 이렇게 된다)
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
                    # ↑ 여기서 return하면 아래 counter["done"] 증가를 건너뛰므로
                    #   가드 안에서 직접 올려준다. (이 함수는 finally가 없다)

                for content_id, video_id in videos:
                    if stop_flag.is_set():
                        return               # ← 중간 중단: success 기록 없이 종료
                        # v2 수정 2번. 절반만 처리하고 success를 남기면
                        # 나머지 영상은 영영 수집되지 않는다.
                    result, sig = fetch_one_video(video_id)
                    if sig == "stop":
                        return
                    if not result:
                        with lock:
                            counter["vfail"] += 1
                        continue             # 이 영상만 포기, 다음 영상으로

                    # COALESCE 보호: 파싱 실패 필드가 기존 값을 지우지 않게
                    #
                    # v2 수정 4번. 유튜브가 HTML을 조금 바꾸면 일부 필드만
                    # 파싱에 실패해 None이 온다. 그대로 UPDATE하면
                    # 이전에 잘 받아둔 값이 NULL로 덮인다.
                    #
                    # published_is_approx는 IF로 처리한다.
                    # published_at을 못 읽었으면 approx 플래그도 그대로 둬야
                    # "정확한 값이 있다"고 잘못 표시되지 않는다.
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

                    # L2a는 view_count만 넣었다. 여기서 like/comment까지 채운다.
                    # calc_metrics는 "like가 기록된 최신 스냅샷"을 따로 찾아
                    # ER을 계산한다 (L2a 스냅샷이 최신이면 like가 NULL이므로).
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
                # 루프를 끝까지 돌아야 여기 도달한다.
                # 중간 return(stop_flag, sig=="stop")은 로그를 남기지 않아
                # 다음 실행에서 재시도된다.
                cur.execute("""
                    INSERT INTO crawl_logs
                      (channel_id, target_url, layer, status, http_status, duration_ms)
                    VALUES (%s, %s, %s, 'success', 200, NULL)
                """, (channel_id, f"channel_{channel_id}", layer))
                with lock:
                    counter["ok"] += 1
        finally:
            conn.close()

    except Exception as e:
        # 예외 격리: 채널 1건의 DB 문제로 전체가 죽거나 침묵 소실되지 않게
        #
        # v2 수정 1번. 이전에는 워커 예외를 아무도 소비하지 않아
        # 채널이 로그 없이 사라졌고, resume이 그걸 "아직 안 함"으로 보고
        # 계속 재시도하는 무한 루프가 됐다.
        print(f"    ⚠️ DB 오류 ch={channel_id}: {repr(e)[:140]}")
        try:
            # 별도 커넥션. 위 커넥션은 이미 깨졌을 수 있다.
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
        #
        # ORDER BY FIELD로 활동성 순서를 강제한다.
        # 57시간짜리 작업이라 중간에 끊길 가능성이 높은데,
        # 그때 최소한 active 채널은 확보돼 있어야 한다.
        #
        # NOT EXISTS 안의 IF: dormant는 'L2b_shorts' 로그를,
        # 나머지는 'L2b' 로그를 기준으로 resume한다.
        # (dormant가 재판정으로 active가 되면 'L2b' 기록이 없으므로
        #  자동으로 대상에 다시 들어와 롱폼까지 수집된다)
        cur.execute("""
            SELECT ch.channel_id, ch.channel_activity_status FROM channels ch
            WHERE ch.platform='youtube'
              AND ch.channel_activity_status IN ('active','low_active','inactive','dormant')
              AND ch.channel_id_status <> 'duplicate'
              AND NOT EXISTS (
                  SELECT 1 FROM crawl_logs cl
                  WHERE cl.channel_id = ch.channel_id
                    AND cl.layer = IF(ch.channel_activity_status='dormant',
                                      'L2b_shorts', 'L2b')
                    AND cl.status='success'
                    AND cl.attempted_at >= NOW() - INTERVAL %s DAY
              )
            ORDER BY FIELD(ch.channel_activity_status,
                           'active','low_active','inactive','dormant'), ch.channel_id
        """, (L2_REFRESH_DAYS,))
        channels = cur.fetchall()
    conn.close()

    if BATCH_LIMIT:
        channels = channels[:BATCH_LIMIT]

    total = len(channels)
    est_req = total * 20   # 채널당 대략 video 15 + shorts 몇 개
    n_rests = (est_req - 1) // L2_REST_EVERY if L2_REST_EVERY else 0
    est_h = (est_req * L2B_MIN_INTERVAL + n_rests * L2_REST_SECONDS) / 3600
    # 예상 시간이 57시간쯤 나온다. 시작 전에 이걸 알아야
    # "오늘 안에 안 끝나겠다"는 판단을 할 수 있다.
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