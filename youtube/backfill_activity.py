# backfill_activity.py
"""활동성 소급 재판정.

crawler_l2a.classify_activity()를 그대로 재사용해, 이미 DB에 쌓인
contents만으로 channel_activity_status를 다시 계산한다. 재크롤링이 없다.

언제 쓰나:
  1) L2b 완료 후 — L2a 시점에는 쇼츠의 published_at이 NULL이라
     활동성이 롱폼 기준으로만 잠정 판정된다. L2b가 쇼츠 게시일을
     채운 뒤 이 스크립트가 확정 판정을 한다. (PIPELINE에서 metric 앞)
  2) 판정 기준(180일/10건 등)을 변경했을 때 전체 재판정

대상 조건:
  contents가 하나도 없는 채널은 제외한다. classify_activity()는
  근거가 없으면 'inactive'를 반환하므로, 아직 수집되지 않은 채널까지
  대상에 넣으면 '수집 실패'가 '비활성'으로 박제된다.
"""
import pymysql
from collections import Counter

from youtube.config import DB
from youtube.crawler.crawler_l2a import classify_activity

PROGRESS_EVERY = 500

TARGET_SQL = """
    SELECT ch.channel_id, ch.channel_activity_status
    FROM channels ch
    WHERE ch.platform = 'youtube'
      AND ch.channel_existence_status = 'normal'
      AND ch.channel_id_status <> 'duplicate'
      AND EXISTS (
          SELECT 1 FROM contents c WHERE c.channel_id = ch.channel_id
      )
    ORDER BY ch.channel_id
"""

UPDATE_SQL = """
    UPDATE channels SET channel_activity_status = %s
    WHERE channel_id = %s
"""


def main():
    conn = pymysql.connect(**DB, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(TARGET_SQL)
            targets = cur.fetchall()

        total = len(targets)
        print(f"[backfill] 대상 채널 {total:,}개 (contents 보유 + normal)")
        if total == 0:
            print("처리할 채널 없음.")
            return

        changed = 0
        transitions = Counter()
        after = Counter()

        for i, (cid, old) in enumerate(targets, 1):
            try:
                new = classify_activity(conn, cid)
            except Exception as e:
                print(f"  ⚠️ 판정 실패 ch={cid}: {repr(e)[:120]}")
                continue

            after[new] += 1
            if new != old:
                with conn.cursor() as cur:
                    cur.execute(UPDATE_SQL, (new, cid))
                changed += 1
                transitions[f"{old} → {new}"] += 1

            if i % PROGRESS_EVERY == 0:
                print(f"  [{i:,}/{total:,}] 변경 {changed:,}건")

        print(f"\n=== 완료: {total:,}개 중 {changed:,}개 변경 ===")

        if transitions:
            print("\n[상태 전이]")
            for k, v in transitions.most_common():
                print(f"  {k:28s} {v:,}건")

        print("\n[재판정 후 분포]")
        for k in ("active", "low_active", "inactive", "dormant"):
            if after[k]:
                print(f"  {k:12s} {after[k]:,}개")

    finally:
        conn.close()


if __name__ == "__main__":
    main()