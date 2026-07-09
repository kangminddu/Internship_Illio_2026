# tiktok/steps/l1.py
import os
from datetime import datetime

from playwright.async_api import async_playwright

from tiktok import config
from tiktok import parser

try:
    import pymysql
except ImportError:
    pymysql = None


def normalize(channel):
    c = channel.strip()
    if c.startswith("http"):
        url = c.split("?")[0].rstrip("/")
        handle = url.split("/@")[-1].split("/")[0]
        return handle, url
    handle = c.lstrip("@")
    return handle, "https://www.tiktok.com/@" + handle


async def fetch_html(pw_browser, url):
    ctx_kwargs = dict(
        locale="en-US",
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"),
        viewport={"width": 1280, "height": 900},
    )
    if os.path.exists(config.SESSION_PATH):
        ctx_kwargs["storage_state"] = config.SESSION_PATH
    context = await pw_browser.new_context(**ctx_kwargs)
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(int(config.L1_DELAY * 1000))
        return await page.content()
    finally:
        await context.close()


UPDATE_CH = ("UPDATE channels SET channel_name=%s, bio=%s, external_link=%s, "
             "external_channel_id=%s WHERE channel_id=%s")
INSERT_SNAP = (
    "INSERT INTO channel_snapshots "
    "(channel_id, captured_at, follower_count, following_count, "
    " total_video_count, total_like_count) VALUES (%s,%s,%s,%s,%s,%s) "
    "ON DUPLICATE KEY UPDATE follower_count=VALUES(follower_count), "
    "following_count=VALUES(following_count), "
    "total_video_count=VALUES(total_video_count), "
    "total_like_count=VALUES(total_like_count)"
)


def save_l1(conn, channel_id, row):
    ext_link = None
    bio = row.get("bio")
    if bio:
        for tok in bio.split():
            if tok.startswith("http"):
                ext_link = tok[:512]
                break
    with conn.cursor() as cur:
        cur.execute(UPDATE_CH, (row.get("nickname"), bio, ext_link,
                                row.get("sec_uid"), channel_id))
        cur.execute(INSERT_SNAP, (
            channel_id, datetime.now(),
            row.get("follower_count"), row.get("following_count"),
            row.get("video_count"), row.get("heart_count"),
        ))
    conn.commit()


def fetch_targets(conn, limit):
    sql = ("SELECT channel_id, channel_url_normalized FROM channels "
           "WHERE platform='tiktok' AND channel_id_status='handle_only' "
           "AND channel_name IS NULL ORDER BY channel_id")
    if limit:
        sql += " LIMIT %d" % int(limit)
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


async def run(channel=None, limit=None, **_):
    if pymysql is None:
        raise SystemExit("pymysql 필요: pip install pymysql")

    if channel:
        handle, url = normalize(channel)
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=config.HEADLESS)
            html = await fetch_html(b, url)
            await b.close()
        row = parser.parse_l1(html)
        print("[L1] %s ->" % handle, "OK" if row else "DATA NONE")
        if row:
            for k, v in row.items():
                print("   %-16s: %s" % (k, v))
        return

    conn = pymysql.connect(**config.DB)
    try:
        targets = fetch_targets(conn, limit)
        print("[L1] 대상 채널:", len(targets))
        ok = none = err = 0
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=config.HEADLESS)
            try:
                for i, (cid, url) in enumerate(targets, 1):
                    try:
                        html = await fetch_html(b, url)
                        row = parser.parse_l1(html)
                        if row is None:
                            none += 1
                            print("  [%d/%d] NONE  %s" % (i, len(targets), url))
                            continue
                        save_l1(conn, cid, row)
                        ok += 1
                        print("  [%d/%d] OK    %s (f=%s v=%s)"
                              % (i, len(targets), url,
                                 row.get("follower_count"), row.get("video_count")))
                    except Exception as e:
                        err += 1
                        print("  [%d/%d] ERR   %s | %s" % (i, len(targets), url, e))
            finally:
                await b.close()
        print("[L1] 완료: OK=%d NONE=%d ERR=%d" % (ok, none, err))
    finally:
        conn.close()
