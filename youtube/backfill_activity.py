# backfill_activity.py
"""활동성 소급 재판정.

crawler_l2a.classify_activity()를 그대로 재사용해, 이미 DB에 쌓인
contents만으로 channel_activity_status를 다시 계산한다. 재크롤링이 없다.

왜 이 파일이 필요한가 (탄생 배경)
------
처음에 channel_metrics가 0행인 문제를 조사하다 원인을 둘 찾았다.
  ① UNIQUE KEY(channel_id)가 채널당 1행만 허용 → INSERT가 전부 실패
     (AUTO_INCREMENT는 12,000을 넘었는데 실제 행은 0개 = 실패 흔적)
  ② 활동성이 전부 'unknown'이라 calc_metrics의 대상 필터에 하나도 안 걸림

②를 고치려면 활동성을 다시 매겨야 하는데, L2a를 재실행하면 9시간이 걸린다.
이미 contents가 DB에 쌓여 있으니 그걸로 계산만 다시 하면 되는 상황이었다.
→ DB만 읽고 쓰는 소급 재판정 스크립트를 따로 만들었다.

언제 쓰나:
  1) L2b 완료 후 — L2a 시점에는 쇼츠의 published_at이 NULL이라
     활동성이 롱폼 기준으로만 잠정 판정된다. L2b가 쇼츠 게시일을
     채운 뒤 이 스크립트가 확정 판정을 한다. (PIPELINE에서 metric 앞)
  2) 판정 기준(180일/10건 등)을 변경했을 때 전체 재판정

★ 왜 판정이 2단계인가 (순환 참조)
------
    L2b는 대상 선정에 활동성을 쓴다 (요청량이 8배라 전 채널을 못 돈다)
    활동성 확정에는 L2b가 채운 쇼츠 게시일이 필요하다
                → 서로를 기다린다

    해결: L2a에서 잠정 판정 → L2b 수집 → 여기서 확정 판정
    main.py PIPELINE이 backfill을 metric 바로 앞에 둔 이유이고,
    그 파일 주석에도 "backfill은 반드시 metric 앞"이라고 적혀 있다.

대상 조건:
  contents가 하나도 없는 채널은 제외한다. classify_activity()는
  근거가 없으면 'inactive'를 반환하므로, 아직 수집되지 않은 채널까지
  대상에 넣으면 '수집 실패'가 '비활성'으로 박제된다.

  ↑ 이게 이 파일에서 가장 중요한 조건이다.
    L2a v2가 "실패를 inactive로 박제하지 않는다"를 명시적으로 고쳤는데,
    backfill이 전 채널을 대상으로 돌면 그 오염을 일괄로 되살린다.
    로직을 재사용해도 '전제 조건'까지 함께 오지는 않는다.
"""
import pymysql
from collections import Counter

from youtube.config import DB
# ★ 판정 로직을 import해서 재사용한다.
#   여기에 같은 기준을 복사해두면 L2a와 backfill의 판정이 갈라진다.
#   (잠정치와 확정치가 다른 기준으로 계산되면 재판정 자체가 무의미)
from youtube.crawler.crawler_l2a import classify_activity

PROGRESS_EVERY = 500   # 7천 개를 도는 동안 진행 표시가 없으면
                       # 멈춘 건지 도는 건지 알 수 없다

TARGET_SQL = """
    SELECT ch.channel_id, ch.channel_activity_status
    FROM channels ch
    WHERE ch.platform = 'youtube'
      AND ch.channel_existence_status = 'normal'   -- 삭제/정지 채널 제외
      AND ch.channel_id_status <> 'duplicate'      -- L2a 대상 조건과 동일 기준
      AND EXISTS (
          SELECT 1 FROM contents c WHERE c.channel_id = ch.channel_id
      )
      -- ↑ 판정 근거(contents)가 있는 채널만.
      --   IN (SELECT DISTINCT ...) 대신 EXISTS를 쓰는 이유:
      --   contents가 22만 건이라 IN은 전체를 서브쿼리로 모은다.
      --   EXISTS는 한 건만 찾으면 즉시 빠져나온다.
    ORDER BY ch.channel_id
"""
# 현재 상태(channel_activity_status)를 함께 SELECT하는 이유:
# 변경 전후를 비교해 '상태 전이'를 집계하기 위함.
# 채널마다 별도 SELECT를 날리지 않으므로 쿼리 수도 늘지 않는다.

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
        transitions = Counter()   # "dormant → active" 같은 전이를 센다
        after = Counter()         # 재판정 후 최종 분포

        for i, (cid, old) in enumerate(targets, 1):
            try:
                new = classify_activity(conn, cid)
            except Exception as e:
                # 채널 1건의 판정 실패로 전체가 죽지 않게 격리.
                # (크롤러들과 같은 원칙)
                print(f"  ⚠️ 판정 실패 ch={cid}: {repr(e)[:120]}")
                continue

            after[new] += 1
            # 실제로 바뀐 것만 UPDATE한다.
            # WHERE에 <> 조건을 넣어 DB가 판단하게 할 수도 있지만,
            # 파이썬에서 미리 거르면 불필요한 쿼리 자체가 안 나간다.
            if new != old:
                with conn.cursor() as cur:
                    cur.execute(UPDATE_SQL, (new, cid))
                changed += 1
                transitions[f"{old} → {new}"] += 1

            if i % PROGRESS_EVERY == 0:
                print(f"  [{i:,}/{total:,}] 변경 {changed:,}건")

        print(f"\n=== 완료: {total:,}개 중 {changed:,}개 변경 ===")

        # ★ 상태 전이 집계가 이 스크립트의 결과물이다.
        #   "몇 개 바뀌었다"만으로는 작업이 의미가 있었는지 알 수 없다.
        #
        #   예상 출력:
        #     dormant → low_active   612건
        #     dormant → active       341건
        #   → "쇼츠를 반영하니 953개 채널이 dormant에서 벗어났다"를
        #      숫자로 말할 수 있다. 쇼츠 대응이 효과가 있었는지의 검증이다.
        if transitions:
            print("\n[상태 전이]")
            for k, v in transitions.most_common():
                print(f"  {k:28s} {v:,}건")

        # 재판정 후 분포. 따로 SQL을 치지 않아도 전체 그림이 보인다.
        # 이 값이 곧 다음 단계(metric)의 대상 규모이기도 하다
        # — active + low_active가 지표 산출 대상이다.
        print("\n[재판정 후 분포]")
        for k in ("active", "low_active", "inactive", "dormant"):
            if after[k]:
                print(f"  {k:12s} {after[k]:,}개")

    finally:
        conn.close()


if __name__ == "__main__":
    main()