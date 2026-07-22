"""
주 1회 배치 — 대상 채널 목록을 순회하며 수집 + 지표 계산을 한 번에.

준비:
    export YOUTUBE_API_KEY="..."
    export DATABASE_URL="postgresql://kangminsoo@localhost:5432/creator_crm"

실행:
    python run_batch.py                       # target_channels.txt 사용
    python run_batch.py my_list.txt           # 다른 목록 파일 지정

설계 포인트
    - 공용 쿼터: 채널 전체가 하루 한도(10,000)를 나눠 쓰도록 하나의 QuotaTracker 공유
    - 에러 격리: 한 채널이 실패해도 나머지는 계속 (try/except per channel)
    - 쿼터 소진 시: 남은 채널은 건너뛰고 다음 실행에서 이어감
    - 로깅: 실행 결과를 batch_log.txt에 timestamp와 함께 기록 (KPI '수집 성공률' 근거)
    - 마지막에 전 채널 지표 재계산
"""

import os
import sys
import datetime as dt

# .env 파일에서 환경변수 자동 로드 (cron 실행 시 export가 안 보이는 문제 해결)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass  # dotenv 없으면 기존 export 방식으로 동작

# 같은 폴더의 수집기·계산기 재사용
import youtube_collector as collector
import metrics_calculator as calculator
from googleapiclient.discovery import build

LOG_FILE = os.path.join(os.path.dirname(__file__), "batch_log.txt")


def log(msg):
    """화면 + 파일에 동시 기록."""
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_targets(path):
    """대상 목록 파일에서 URL만 추출 (주석·빈 줄 제외)."""
    urls = []
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def main():
    list_path = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.join(os.path.dirname(__file__), "target_channels.txt")

    urls = load_targets(list_path)
    log(f"===== 배치 시작 — 대상 {len(urls)}개 채널 =====")

    # 공용 자원 (쿼터를 채널 전체가 누적으로 공유)
    yt = build("youtube", "v3", developerKey=os.environ["YOUTUBE_API_KEY"])
    quota = collector.QuotaTracker(collector.CONFIG["daily_quota_limit"])

    success, failed, skipped = 0, 0, 0
    changed = False
    for i, url in enumerate(urls, 1):
        log(f"--- ({i}/{len(urls)}) {url} ---")
        try:
            result = collector.collect_channel(
                url,
                yt=yt,
                quota=quota
            )

            if result["new_videos"] > 0:
                changed = True

            log(
                f"    ✓ 성공 — 영상 {result['videos']} · "
                f"신규 {result['new_videos']} · "
                f"댓글 {result['comments']} · "
                f"누적 쿼터 {quota.used}/{quota.limit}"
            )
            success += 1
        except collector.QuotaExceeded:
            # 쿼터 소진 → 남은 채널은 다음 실행으로
            log(f"    ⚠ 쿼터 소진 — 남은 {len(urls) - i + 1}개 채널은 다음 실행에서 수집")
            skipped = len(urls) - i + 1
            break
        except Exception as e:
            # 한 채널 실패해도 배치는 계속
            log(f"    ✗ 실패 — {type(e).__name__}: {e}")
            failed += 1
            continue

    # 수집 끝 → 전 채널 지표 재계산
    if changed:
        log("----- 지표 재계산 -----")
        try:
            calculator.main()
            log("    ✓ 지표 재계산 완료")
        except Exception as e:
            log(f"    ✗ 지표 계산 실패 — {type(e).__name__}: {e}")
    else:
        log("----- 변경된 데이터 없음 → 지표 계산 생략 -----")

    # 요약 (KPI 근거)
    total = len(urls)
    rate = (success / total * 100) if total else 0
    log(f"===== 배치 종료 — 성공 {success} · 실패 {failed} · 건너뜀 {skipped} · "
        f"성공률 {rate:.1f}% · 총 쿼터 {quota.used} =====\n")


if __name__ == "__main__":
    main()