# -*- coding: utf-8 -*-
"""
youtube/diag_429.py — 레이트리밋 진단 스크립트

왜 만들었나
------
L1 크롤러(워커 3~4개 병렬)에서 429가 나기 시작했을 때,
원인이 둘 중 무엇인지 알 수 없었다:

  (A) 요청 '간격'이 짧아서   → 간격을 늘리면 해결
  (B) 요청 '총량'이 많아서   → 간격과 무관, 세션을 끊어야 해결
  (C) 병렬(동시성) 자체가 문제 → 워커 수를 줄여야 해결

이 셋은 대응이 완전히 다르다. 그래서 변수를 하나만 남기고
나머지를 제거한 통제 실험을 만들었다.

설계 원칙
------
크롤러와 '조건을 최대한 동일하게' 두되, 병렬만 없앤다.
  - 같은 fetch 함수(fetch_channel_l1)를 쓴다 → 헤더/쿠키/파싱 동일
  - 같은 대상 쿼리를 쓴다 → 실제로 다음에 처리될 채널을 그대로 사용
  - 단일 스레드 + 고정 간격 → 동시성 변수만 제거

이러면 결과 해석이 명확해진다:
  429가 안 나면  → 병렬이 원인 (C)
  429가 나면     → 몇 번째에서 나는지가 곧 '한도 지표'가 된다 (A or B)
                   간격을 바꿔 재실행하면 A인지 B인지도 갈린다

실행:
  python -m youtube.diag_429                      # 20개, 1.2초 간격
  python -m youtube.diag_429 --n 30 --interval 1.2
  python -m youtube.diag_429 --n 30 --interval 3.0  # 간격만 바꿔 A/B 판별
"""
import time
import argparse
import pymysql
from youtube.config import DB
from youtube.crawler.lib.youtube_parser import fetch_channel_l1


def main(n, interval):
    # 크롤러(crawler_l1_parallel.main)와 동일한 대상 쿼리.
    #
    # 굳이 같은 쿼리를 쓰는 이유:
    #   임의의 채널을 고르면 "그 채널이 특이해서 429가 났나?"라는
    #   변수가 생긴다. 실제로 크롤러가 다음에 처리할 채널을 그대로
    #   써야 조건이 같아진다.
    #
    # ⚠️ 이 쿼리는 crawl_logs에 행이 있으면 무조건 제외한다(layer 무관).
    #    진단용이라 문제없지만, 크롤러 본체는 layer='L1' AND status='success'
    #    조건을 추가로 갖고 있다.
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
        r = fetch_channel_l1(url)          # 크롤러와 '완전히 같은' fetch
        ms = int((time.time() - t0) * 1000)

        # 429가 나오면 즉시 중단한다.
        # 계속 때리면 차단이 심해져서 다음 진단이 오염되고,
        # 알고 싶은 것은 "몇 번째에서 처음 걸리는가"이지
        # "총 몇 번 걸리는가"가 아니기 때문.
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
            # 404, 삭제 채널 등. 429가 아니므로 진단 관점에선 '통과'다.
            # (레이트리밋과 무관한 실패라 카운트만 하고 계속 진행)
            fail += 1
            print(f"[{i:3d}] 🟡 {r.http_status}  ch={cid}  {ms}ms  "
                  f"{r.error_type}  {url}")

        # 응답 시간과 무관하게 고정 간격을 유지한다.
        # (실제 크롤러의 RateController는 '다음 슬롯 시각'을 계산해서
        #  응답이 느려도 전체 속도가 일정하게 유지되도록 한다.
        #  여기서는 단순화해서 sleep만 쓴다)
        time.sleep(interval)

    print(f"\n=== 결과: ok={ok} fail={fail} 429={rl} / {len(channels)}개 시도 ===")

    # 결과 해석을 코드에 박아뒀다.
    # 진단 스크립트는 "숫자"가 아니라 "판단"을 내놓아야 쓸모가 있다.
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