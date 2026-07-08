import pymysql
from l1_crawler import get_session, extract_yt_initial_data, parse_l2_videos

DB = dict(host="127.0.0.1", port=3306, user="root", password="",
          database="fandom_crm", charset="utf8mb4")

conn = pymysql.connect(**DB, autocommit=True)
with conn.cursor() as cur:
    cur.execute("""
        SELECT channel_id, COALESCE(channel_url_normalized, channel_url_raw)
        FROM channels
        WHERE platform='youtube' AND channel_existence_status='normal'
        ORDER BY channel_id
    """)
    channels = cur.fetchall()

print(f"대상 {len(channels)}개 채널\n")
for channel_id, url in channels:
    try:
        body = get_session().get(url.rstrip("/") + "/videos?hl=ko&gl=KR", timeout=20).text
        data = extract_yt_initial_data(body)
        videos = parse_l2_videos(data)
    except Exception as e:
        print(f"  ch={channel_id} 실패: {e}")
        continue

    updated = 0
    with conn.cursor() as cur:
        for v in videos:
            if v.get("duration_sec") and v.get("video_id"):
                cur.execute(
                    "UPDATE contents SET duration_sec=%s WHERE channel_id=%s AND external_id=%s",
                    (v["duration_sec"], channel_id, v["video_id"]))
                updated += cur.rowcount
    print(f"  ch={channel_id} | {updated}개 길이 채움")

conn.close()
print("\n완료")