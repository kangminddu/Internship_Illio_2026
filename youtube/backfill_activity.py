# backfill_activity.py
import pymysql
from youtube.config import DB
from youtube.crawler.crawler_l2a import classify_activity  # 경로는 네 구조에 맞게

def main():
    conn = pymysql.connect(**DB, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                    SELECT channel_id FROM channels
                    WHERE platform='youtube'
                    AND channel_existence_status='normal'
                    AND channel_id_status <> 'duplicate'
                    AND channel_id IN (SELECT DISTINCT channel_id FROM contents)
                """)
            ids = [r[0] for r in cur.fetchall()]

        changed = 0
        for cid in ids:
            new_status = classify_activity(conn, cid)
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE channels SET channel_activity_status=%s
                    WHERE channel_id=%s AND channel_activity_status <> %s
                """, (new_status, cid, new_status))
                if cur.rowcount:
                    changed += 1
        print(f"{len(ids)}개 중 {changed}개 status 변경됨")
    finally:
        conn.close()

if __name__ == "__main__":
    main()