# crawler/chzzk_email.py
"""
치지직 이메일 보강 수집기 (B 방식: DB의 description 재사용).

왜 이 단계가 있는가
------
섭외 CRM에서 이메일은 핵심 필드다. 그런데 유튜브의 '비즈니스 문의'
이메일은 CAPTCHA 뒤에 있어 접근할 수 없다(가이드라인 공개 데이터 원칙).

한국 크리에이터는 유튜브 설명란에 치지직 링크만 걸어두고
연락처는 치지직 소개글에 적어두는 경우가 많다.
→ 유튜브에서 못 얻는 이메일을 치지직에서 얻는 우회로.

'B 방식'인 이유 (설계 판단)
------
A 방식: 유튜브 채널 페이지를 다시 요청해서 설명란을 읽는다
B 방식: L1이 이미 저장해둔 channels.description을 재사용한다  ← 채택

B를 택한 이유는 유튜브 요청이 0회이기 때문이다.
L1이 9천 개 채널을 이미 긁었는데 같은 페이지를 또 요청하면
레이트리밋 예산을 두 배로 쓰게 된다.
치지직 API만 호출하므로 유튜브 IP 차단과 무관하게 언제든 실행 가능하다.
(실제로 유튜브 L2b가 IP 차단된 상태에서도 이 단계는 돌릴 수 있다)

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


# 이메일 추출 로직은 youtube_parser와 공유한다.
# 같은 정규식을 두 벌 두면 한쪽만 고쳐서 결과가 갈린다.
from youtube.crawler.lib.youtube_parser import extract_emails
from youtube.config import DB, CHZZK_USER_AGENT, CHZZK_TIMEOUT, CHZZK_DELAY

# 치지직 API는 User-Agent가 없으면 거부한다. 반드시 설정.
HEADERS = {"User-Agent": CHZZK_USER_AGENT}
REQUEST_TIMEOUT = CHZZK_TIMEOUT
REQUEST_DELAY = CHZZK_DELAY        # 치지직 API 호출 간 딜레이(초)

# https:// 있어도 없어도 잡히게, chzzk.me / chzzk.naver.com 둘 다
#
# 두 형태를 다 처리해야 하는 이유:
#   chzzk.naver.com/{32자리 hex}  — 정식 URL. ID가 그대로 들어있다.
#   chzzk.me/{짧은코드}           — 단축링크. 리다이렉트를 따라가야 ID를 안다.
# 설명란에는 짧아서 chzzk.me를 쓰는 경우가 더 많다.
CHZZK_LINK_RE = re.compile(
    r'(?:https?://)?(?:chzzk\.me/\S+|chzzk\.naver\.com/[0-9a-f]{32})',
    re.IGNORECASE,
)
CHZZK_ID_RE = re.compile(r'chzzk\.naver\.com/([0-9a-f]{32})', re.IGNORECASE)


def find_chzzk_channel_id(description):
    """설명란에서 치지직 채널 ID(32자리 hex)를 찾는다. 없으면 None."""
    if not description:
        return None

    m = CHZZK_LINK_RE.search(description)
    if not m:
        return None
    link = m.group(0)

    # 정식 URL이면 ID 바로 추출 (네트워크 요청 불필요)
    id_m = CHZZK_ID_RE.search(link)
    if id_m:
        return id_m.group(1)

    # 스키마 없으면 붙여줌 (chzzk.me/xxx → https://chzzk.me/xxx)
    if not link.startswith("http"):
        link = "https://" + link

    # chzzk.me 단축링크면 리다이렉트 따라가서 최종 URL의 ID 확보
    #
    # HEAD를 쓰는 이유: 본문이 필요 없고 최종 URL만 알면 된다.
    # GET이면 페이지 전체를 받아 대역폭을 낭비한다.
    try:
        resp = requests.head(
            link, headers=HEADERS, allow_redirects=True, timeout=REQUEST_TIMEOUT
        )
        id_m = CHZZK_ID_RE.search(resp.url)   # 리다이렉트된 최종 URL
        if id_m:
            return id_m.group(1)
    except Exception as e:
        # 단축링크가 만료됐거나 서비스가 죽었을 수 있다.
        # 이 채널만 포기하고 다음으로 (전체를 죽이지 않는다)
        print(f"    단축링크 해석 실패: {link} ({e})")
    return None


def fetch_chzzk_description(channel_id):
    """치지직 API로 channelDescription 텍스트 확보. (User-Agent 필수)

    치지직은 공개 API가 있어서 HTML 파싱이 필요 없다.
    유튜브(ytInitialData 정규식 추출)와 대조되는 부분.
    """
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
    """creator_emails 저장. 중복(creator+email)은 무시/갱신.

    source_platform을 남기는 이유:
    같은 크리에이터의 이메일이 유튜브 설명란에도 있고 치지직에도 있을 수 있다.
    어디서 얻었는지를 기록해두면 나중에 "치지직 보강이 얼마나 기여했나"를
    집계할 수 있고, 출처별 신뢰도도 다르게 볼 수 있다.
    """
    n = 0
    for em in emails:
        # 정규식이 잡아낸 것 중 명백한 오검출을 걸러낸다.
        # 설명란에는 "@태그"나 이상한 문자열이 많아 정규식만으로는 부족하다.
        #   - 255자 초과: DB 컬럼 길이 + 정상 이메일일 리 없음
        #   - TLD에 점이 없음: "user@localhost" 같은 형태 배제
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
    #
    # NOT EXISTS가 곧 resume 역할을 한다.
    # 이미 이메일을 찾은 크리에이터는 다시 시도하지 않는다.
    # (crawl_logs가 아니라 결과 테이블 자체를 기준으로 삼는 방식)
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
        #
        # 방어적 중복이다. L1의 ChannelL1에도 emails 필드가 있지만,
        # 정규식이 개선되거나 description 저장 로직이 바뀌었을 때를 대비해
        # 여기서 한 번 더 시도한다. 네트워크 요청이 없으니 비용도 0.
        yt_emails = extract_emails(description)
        if yt_emails:
            with conn.cursor() as cur:
                save_emails(cur, creator_id, channel_id, yt_emails, 'youtube')
            stat["youtube"] += 1
            print(f"[{i}/{len(targets)}] ch={channel_id} 유튜브 이메일 {yt_emails}")
            continue    # ← 치지직 API 호출 안 함 = 딜레이도 없음

        # 2) 치지직 링크 → channelId → API → 이메일
        cz_id = find_chzzk_channel_id(description)
        if not cz_id:
            # 치지직 링크조차 없으면 여기서 얻을 방법이 없다.
            # '수동 대상'으로 분류해 사람이 직접 확인하도록 넘긴다.
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

        # 치지직 API 호출한 경우만 딜레이.
        # continue로 빠진 경로(유튜브 이메일 발견, 링크 없음)는 네트워크를
        # 안 썼으니 쉴 이유가 없다. → 전체 소요 시간이 크게 줄어든다.
        time.sleep(REQUEST_DELAY)

    # 출처별 집계.
    # "치지직 보강이 실제로 몇 건을 더 건졌나"를 숫자로 보여준다.
    # manual 건수는 사람이 처리해야 할 작업량이기도 하다.
    print(
        f"\n[완료] 유튜브 {stat['youtube']} / 치지직 {stat['chzzk']} / "
        f"수동대상 {stat['manual']} (전체 {len(targets)})"
    )


if __name__ == "__main__":
    main()