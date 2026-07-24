import pymysql

from youtube.config import DB
from youtube.crawler.lib.youtube_parser import fetch_channel_l1

conn = pymysql.connect(**DB)

with conn.cursor() as cur:
    cur.execute("""
        SELECT
            ch.channel_id,
            ch.channel_url_normalized,
            cl.http_status
        FROM crawl_logs cl
        JOIN channels ch
          ON ch.channel_id = cl.channel_id
        WHERE cl.http_status IN (403, 429)
        ORDER BY cl.log_id DESC
        LIMIT 10
    """)

    rows = cur.fetchall()

for channel_id, url, old_status in rows:
    print("=" * 80)
    print(f"channel_id : {channel_id}")
    print(f"old status : {old_status}")
    print(f"url        : {url}")

    r = fetch_channel_l1(url)

    print(f"new ok     : {r.ok}")
    print(f"new status : {r.http_status}")
    print(f"error      : {r.error}")
    print(f"name       : {r.channel_name}")
    print()

conn.close()