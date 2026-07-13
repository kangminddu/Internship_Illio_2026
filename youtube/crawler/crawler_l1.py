import time
import pymysql
from datetime import datetime, timezone

# 아까 만든 크롤러의 파싱 함수 재사용
from crawler.lib.youtube_parser import fetch_channel_l1, HEADERS, REQUEST_TIMEOUT

DB = dict(host="127.0.0.1", port=3306, user="root", password="",
          database="fandom_crm", charset="utf8mb4")

DELAY = 0.35        # 채널 사이 간격(초). 3라운드: 0.5 → 0.2
STOP_AFTER_429 = 2    # 429/403 연속 3회면 차단 방지 위해 중단


def classify_existence(r):
    """
    실패한 크롤 결과를 존재 축으로 분류.
    원칙: 확신할 때만 deleted, 애매하면 unknown (오분류 방지).
    반환: (existence_status, error_type)
    """
    code = r.http_status
    sig = r.page_signal

    if sig == "channel_banned":
        return "suspended", "channel_banned"
    if sig == "channel_not_exist":
        return "deleted", "channel_deleted"
    if code == 404:
        return "deleted", "http_404"
    if code in (429, 403):
        return "unknown", "rate_limited"
    if code == 200 and not r.had_yt_data:
        return "unknown", "no_yt_data"
    # ★ 변경: about 없으면서 200이면 → 삭제로 본다 (unknown 아님)
    #   정상 채널은 about이 있으므로, 없다 = 삭제/정지가 거의 확실
    if code == 200 and r.had_yt_data:
        return "deleted", "about_missing_assumed_deleted"
    # 6) timeout/network → 일시적 → unknown
    err = (r.error or "").lower()
    if "timeout" in err:
        return "unknown", "timeout"
    if "connection" in err:
        return "unknown", "network_error"

    # 그 외 → unknown (모르면 건드리지 않는다)
    return "unknown", "unknown_failure"


conn = pymysql.connect(**DB, autocommit=False)
try:
    with conn.cursor() as cur:
        # 유튜브 채널 전부 꺼내기 (normalized 우선, 없으면 raw)
        cur.execute("""
            SELECT channel_id, creator_id,
                   COALESCE(channel_url_normalized, channel_url_raw) AS crawl_url,
                   channel_id_status
            FROM channels WHERE platform='youtube' ORDER BY channel_id
        """)
        rows = cur.fetchall()
        print(f"대상 채널 {len(rows)}개  (DELAY={DELAY}s)\n")

        ok_cnt = fail_cnt = 0
        consecutive_429 = 0   # 연속 429/403 카운트
        stopped = False

        for i, (channel_id, creator_id, crawl_url, id_status) in enumerate(rows, 1):
            print(f"[{i}/{len(rows)}] ch={channel_id} [{id_status}] {crawl_url}")

            t0 = time.time()
            r = fetch_channel_l1(crawl_url)          # 성공/실패 모두 ChannelL1 반환
            dur_ms = int((time.time() - t0) * 1000)

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

                # 2) 채널명 갱신 + 존재 축 = normal (살아있음 확인)
                if r.channel_name:
                    cur.execute("UPDATE channels SET channel_name=%s, external_channel_id=COALESCE(external_channel_id,%s), channel_existence_status='normal' WHERE channel_id=%s",
                                (r.channel_name, r.external_channel_id, channel_id))
                    cur.execute("UPDATE creators SET nickname=%s WHERE creator_id=%s AND nickname LIKE 'G\\_%%'",
                                (r.channel_name, creator_id))
                else:
                    cur.execute("UPDATE channels SET channel_existence_status='normal' WHERE channel_id=%s",
                                (channel_id,))

                # 3) 성공 로그
                cur.execute("""
                    INSERT INTO crawl_logs
                      (channel_id, target_url, layer, status, http_status, duration_ms)
                    VALUES (%s, %s, 'L1', 'success', 200, %s)
                """, (channel_id, crawl_url, dur_ms))

                ok_cnt += 1
                consecutive_429 = 0   # 정상 응답 → 카운터 리셋
                print(f"    OK  {r.channel_name} | 구독 {r.subscriber_count} "
                      f"| 조회 {r.total_view_count} | 영상 {r.total_video_count}")
            else:
                # 실패: 존재 축 + error_type 동시 분류
                new_existence, etype = classify_existence(r)
                code = r.http_status
                cur.execute("""
                    INSERT INTO crawl_logs
                      (channel_id, target_url, layer, status, http_status,
                       error_type, error_detail, duration_ms)
                    VALUES (%s, %s, 'L1', 'failed', %s, %s, %s, %s)
                """, (channel_id, crawl_url, code, etype,
                      (r.error or "")[:500], dur_ms))

                # 존재 축 갱신 (deleted 확신할 때만, 나머지는 unknown 유지)
                if new_existence == "deleted":
                    cur.execute("UPDATE channels SET channel_existence_status='deleted' WHERE channel_id=%s",
                                (channel_id,))

                fail_cnt += 1
                print(f"    FAIL [{etype}] code={code} -> existence={new_existence}")

            conn.commit()  # 채널마다 커밋 (중간에 끊겨도 여기까진 저장)

            # ── 차단 전조 감지 → 즉시 중단 (안전장치) ──────────────────
            if r.http_status in (429, 403):
                consecutive_429 += 1
                print(f"    ⚠️  차단 전조! HTTP {r.http_status} (연속 {consecutive_429}/{STOP_AFTER_429})")
                if consecutive_429 >= STOP_AFTER_429:
                    print(f"\n🛑 HTTP {r.http_status} 연속 {consecutive_429}회 — 차단 방지 위해 중단.")
                    print(f"   여기까지 {i}/{len(rows)}개 처리. DELAY를 늘려 재시도하세요.")
                    stopped = True
                    break
                # 429 떴으면 이번엔 딜레이 크게 주고 계속 (백오프)
                print(f"    ↳ 백오프: {DELAY*10}s 대기")
                time.sleep(DELAY * 10)
                continue
            # ────────────────────────────────────────────────────────

            if i < len(rows):
                time.sleep(DELAY)
            if i % 100 == 0:
                print(f"    💤 {i}개 처리, 30초 휴식...")
                time.sleep(30)
    tail = " (차단 전조로 중단됨)" if stopped else ""
    print(f"\n=== 완료: 성공 {ok_cnt} / 실패 {fail_cnt}{tail} ===")
finally:
    conn.close()