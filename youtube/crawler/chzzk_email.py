# crawler/chzzk_email.py
"""
치지직 이메일 보강 수집기 (B 방식: DB의 description 재사용).

흐름:
  L1이 channels.description에 저장해둔 유튜브 채널 설명을 읽음
  → 그 설명에 이메일 있으면 저장 (source='youtube')
  → 없으면 설명 속 치지직 링크 추출
  → chzzk.me 단축링크면 리다이렉트로 실제 channelId 확보
  → 치지직 API로 channelDescription 받기
  → 이메일 추출 → creator_emails 저장 (source_platform='chzzk')
  → 그래도 없으면 '수동 대상'으로 출력 (직접 유튜브 확인 필요)

전제:
  - crawler_l1_parallel.py 가 먼저 돌아서 channels.description 이 채워져 있어야 함
  - DB의 description을 읽으므로 유튜브 재요청 없음 (치지직 API만 호출)
"""

import re
import time
import requests
import pymysql
from datetime import datetime


from crawler.lib.youtube_parser import extract_emails
from config import DB, CHZZK_USER_AGENT, CHZZK_TIMEOUT, CHZZK_DELAY

HEADERS = {"User-Agent": CHZZK_USER_AGENT}
REQUEST_TIMEOUT = CHZZK_TIMEOUT
REQUEST_DELAY = CHZZK_DELAY        # 치지직 API 호출 간 딜레이(초)

# https:// 있어도 없어도 잡히게, chzzk.me / chzzk.naver.com 둘 다
CHZZK_LINK_RE = re.compile(
    r'(?:https?://)?(?:chzzk\.me/\S+|chzzk\.naver\.com/[0-9a-f]{32})',
    re.IGNORECASE,
)
CHZZK_ID_RE = re.compile(r'chzzk\.naver\.com/([0-9a-f]{32})', re.IGNORECASE)


def find_chzzk_channel_id(description):
    if not description:
        return None

    m = CHZZK_LINK_RE.search(description)
    if not m:
        return None
    link = m.group(0)

    # 정식 URL이면 ID 바로 추출
    id_m = CHZZK_ID_RE.search(link)
    if id_m:
        return id_m.group(1)

    # 스키마 없으면 붙여줌 (chzzk.me/xxx → https://chzzk.me/xxx)
    if not link.startswith("http"):
        link = "https://" + link

    # chzzk.me 단축링크면 리다이렉트 따라가서 최종 URL의 ID 확보
    try:
        resp = requests.head(
            link, headers=HEADERS, allow_redirects=True, timeout=REQUEST_TIMEOUT
        )
        id_m = CHZZK_ID_RE.search(resp.url)
        if id_m:
            return id_m.group(1)
    except Exception as e:
        print(f"    단축링크 해석 실패: {link} ({e})")
    return None


def fetch_chzzk_description(channel_id):
    """치지직 API로 channelDescription 텍스트 확보. (User-Agent 필수)"""
    url = f"https://api.chzzk.naver.com/service/v1/channels/{channel_id}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        content = data.get("content") or {}
        return content.get("channelDescription")
    except Exception as e:
        print(f"    치지직 API 실패: {channel_id} ({e})")
        return None


def save_emails(cur, creator_id, channel_id, emails, source):
    """creator_emails 저장. 중복(creator+email)은 무시/갱신."""
    n = 0
    for em in emails:
        if len(em) > 255 or "." not in em.split("@")[-1]:
            continue
        cur.execute("""
            INSERT INTO creator_emails
              (creator_id, channel_id, email, source_platform, collected_at)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              collected_at = VALUES(collected_at)
        """, (creator_id, channel_id, em, source, datetime.now()))
        n += 1
    return n


def main():
    conn = pymysql.connect(**DB, autocommit=True)

    # 유튜브 채널 중, 아직 creator_emails에 이메일이 없고
    # L1이 description을 채워둔 채널만 대상 (재요청 없음)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ch.channel_id, ch.creator_id, ch.description
            FROM channels ch
            WHERE ch.platform = 'youtube'
              AND ch.channel_existence_status = 'normal'
              AND ch.description IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM creator_emails ce
                  WHERE ce.creator_id = ch.creator_id
              )
            ORDER BY ch.channel_id
        """)
        targets = cur.fetchall()

    print(f"[치지직 보강] 대상(이메일 없는 유튜브 채널): {len(targets)}개\n")

    stat = {"youtube": 0, "chzzk": 0, "manual": 0, "no_desc": 0}

    for i, (channel_id, creator_id, description) in enumerate(targets, 1):

        # 1) DB의 description에 이메일 있으면 그걸로 저장하고 끝
        #    (L1 저장 이후 새로 매칭되는 경우 대비 — 보통 L1에서 이미 저장됐을 것)
        yt_emails = extract_emails(description)
        if yt_emails:
            with conn.cursor() as cur:
                save_emails(cur, creator_id, channel_id, yt_emails, 'youtube')
            stat["youtube"] += 1
            print(f"[{i}/{len(targets)}] ch={channel_id} 유튜브 이메일 {yt_emails}")
            continue

        # 2) 치지직 링크 → channelId → API → 이메일
        cz_id = find_chzzk_channel_id(description)
        if not cz_id:
            stat["manual"] += 1
            print(f"[{i}/{len(targets)}] ch={channel_id} 치지직 링크 없음 → 수동 대상")
            continue

        cz_desc = fetch_chzzk_description(cz_id)
        cz_emails = extract_emails(cz_desc)
        if cz_emails:
            with conn.cursor() as cur:
                save_emails(cur, creator_id, channel_id, cz_emails, 'chzzk')
            stat["chzzk"] += 1
            print(f"[{i}/{len(targets)}] ch={channel_id} 치지직 이메일 {cz_emails}")
        else:
            stat["manual"] += 1
            print(f"[{i}/{len(targets)}] ch={channel_id} 치지직도 이메일 없음 → 수동 대상")

        time.sleep(REQUEST_DELAY)  # 치지직 API 호출한 경우만 딜레이

    print(
        f"\n[완료] 유튜브 {stat['youtube']} / 치지직 {stat['chzzk']} / "
        f"수동대상 {stat['manual']} (전체 {len(targets)})"
    )


if __name__ == "__main__":
    main()