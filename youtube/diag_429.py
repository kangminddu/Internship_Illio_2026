# -*- coding: utf-8 -*-
"""
429 진단 스크립트: 크롤러와 동일한 조건(같은 fetch, 같은 간격)으로
단일 스레드 순차 요청을 보내 "몇 번째 요청에서, 어떤 URL에서" 429가 나는지 측정.

실행:  python -m youtube.diag_429
        python -m youtube.diag_429 --n 30 --interval 1.2
"""
import time
import argparse
import pymysql
from youtube.config import DB
from youtube.crawler.lib.youtube_parser import fetch_channel_l1


def main(n, interval):
    # 크롤러와 동일한 쿼리로 "다음에 처리될" 채널들을 그대로 가져옴
    conn = pymysql.connect(**DB)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT channel_id,
                   COALESCE(channel_url_normalized, channel_url_raw) AS crawl_url
            FROM channels
            WHERE platform='youtube'
              AND channel_id NOT IN (
                  SELECT channel_id FROM crawl_logs WHERE channel_id IS NOT NULL
              )
            ORDER BY channel_id
            LIMIT %s
        """, (n,))
        channels = cur.fetchall()
    conn.close()

    print(f"진단 시작: {len(channels)}개 채널, 간격 {interval}초, 단일 스레드\n")
    ok = fail = rl = 0

    for i, (cid, url) in enumerate(channels, 1):
        t0 = time.time()
        r = fetch_channel_l1(url)
        ms = int((time.time() - t0) * 1000)

        if r.http_status == 429:
            rl += 1
            print(f"[{i:3d}] 🔴 429  ch={cid}  {ms}ms  {url}")
            print(f"\n>>> {i}번째 요청에서 첫 429 발생. 여기서 중단.")
            print(f">>> 이전까지 ok={ok} fail={fail}")
            break
        elif r.ok:
            ok += 1
            print(f"[{i:3d}] 🟢 200  ch={cid}  {ms}ms  {r.channel_name}")
        else:
            fail += 1
            print(f"[{i:3d}] 🟡 {r.http_status}  ch={cid}  {ms}ms  "
                  f"{r.error_type}  {url}")

        time.sleep(interval)

    print(f"\n=== 결과: ok={ok} fail={fail} 429={rl} / {len(channels)}개 시도 ===")
    if rl == 0:
        print("→ 순차+간격으로는 429 없음. 크롤러(3스레드)와의 차이 조사 필요.")
    else:
        print("→ 지속 요청 패턴 자체가 현재 IP에서 차단됨 (몇 번째인지가 한도 지표).")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=20, help="시도할 채널 수 (기본 20)")
    p.add_argument("--interval", type=float, default=1.2, help="요청 간격 초 (기본 1.2)")
    a = p.parse_args()
    main(a.n, a.interval)