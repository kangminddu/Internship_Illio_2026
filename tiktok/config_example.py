# tiktok/config.py
# TikTok 파이프라인 전용 설정. code/ 에 의존하지 않는 자립 구조.
#
# ⚠️ .gitignore 대상. DB 비밀번호가 들어간다.
#    새 환경에서는 직접 만들어야 한다. (환경변수로 빼는 게 맞다 — 리뷰 안건)

import os

# ── 경로 ──
# __file__ 기준으로 잡는 이유: 어느 디렉터리에서 실행하든 같은 곳을 가리킨다.
# 유튜브 config는 EXPORT_DIR = "youtube/output"으로 상대경로라
# 실행 위치에 따라 엑셀이 다른 곳에 생긴다. (이쪽이 낫다)
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))   # .../tiktok
EXPORT_DIR = os.path.join(BASE_DIR, "output")
SESSION_DIR = os.path.join(BASE_DIR, "session")
os.makedirs(EXPORT_DIR, exist_ok=True)
os.makedirs(SESSION_DIR, exist_ok=True)

# Playwright persistent profile
#   - login.py 가 최초 1회 생성하고, L1/L2/L3 가 공유한다.
#   - ⚠️ storage_state(json) 방식이 아님. 프로필 디렉터리 자체를 재사용한다.
#     틱톡이 쿠키 외에 IndexedDB·캐시·디바이스 지문도 보기 때문에
#     JSON만 옮기면 "다른 브라우저에서 로그인했다"로 판정된다.
#   - 프로필은 프로세스당 1개만 열 수 있음 (Chromium SingletonLock).
#     → login.py와 크롤러를 동시에 실행할 수 없다.
#
#   ⚠️ 실측 정정: L1은 이 프로필을 쓰지 않는다.
#     로그인 상태로 접근하면 틱톡이 계정 단위로 조회를 제한해
#     SSR 데이터를 안 준다. L1은 비로그인으로 돌린다.
#     (환경변수로 덮어쓸 수 있게 해둔 건, 프로필을 갈아끼우며
#      테스트하려던 흔적)
PROFILE_DIR = os.environ.get(
    "TIKTOK_PROFILE_DIR",
    os.path.join(SESSION_DIR, "profile"),
)

# [deprecated] storage_state 방식 잔재. 현재 미사용 — persistent profile로 대체됨.
SESSION_PATH = os.path.join(SESSION_DIR, "tiktok_state.json")

# ── DB (YouTube와 같은 fandom_crm 공유, platform 컬럼으로 구분) ──
# 세 플랫폼이 같은 스키마를 쓴다. 그래서 크로스 플랫폼 분석
# (같은 크리에이터의 유튜브/틱톡 팔로워 비교)이 가능하다.
# 대신 각 플랫폼 코드가 platform 필터를 빠뜨리면 서로의 데이터를 건드린다.
DB = dict(host="127.0.0.1", port=3306, user="root", password="",
          database="fandom_crm", charset="utf8mb4")

PLATFORM = "tiktok"   # DB 저장 시 platform 컬럼 값

# ── 수집 스펙 (가이드라인 기준) ──
# 유튜브(6개월/10개)와 다른 건 버그가 아니다.
# 가이드라인이 플랫폼마다 다르게 정했다 — TikTok은 3개월/15개.
L2_PERIOD_MONTHS = 3     # 최근 3개월
L2_MIN_VIDEOS    = 15    # 최소 15개 (미달 시 기간 2배 확장 규칙은 로직에서 처리)
                         #  → l2.classify_activity가 90일/180일로 처리
L3_COMMENT_LIMIT = 50    # 영상당 수집할 최대 댓글 수 (초기값, 튜닝 대상)
                         #  ⚠️ l3.py가 이 값을 안 쓴다. idle timeout으로
                         #     "응답이 멎을 때까지" 받는 방식으로 바뀌었다.
L3_MAX_SCROLLS   = 20    # 댓글 로딩 스크롤 상한 (역시 l3.py 미사용)


# ── 크롤링 속도/안정성 (⚠️ YouTube보다 훨씬 보수적으로) ──
# YouTube는 429 0건이었지만 TikTok은 차단 공격적 → 살살.
# 초기엔 병렬 없이 순차(worker=1)로 방식부터 검증하고, 안정화 후 늘린다.
#
# ★ 이 판단이 맞았다. 유튜브는 L2b에서 IP가 통째로 차단됐는데,
#   틱톡은 보수적으로 시작한 덕에 그런 일이 없었다.
L1_WORKERS = 1
L1_DELAY   = 2.0         # 요청 간 최소 대기(초)
                         # ⚠️ l1.py는 이 값을 안 쓴다.
                         #    워커 루프에서 asyncio.sleep(1)로 하드코딩.
                         #    (3초→5초→30초까지 실험했지만 실패율에
                         #     영향이 없어 1초로 되돌림 — 틱톡 서버측
                         #     확률적 거부라 간격으로 통제가 안 된다)

L2_WORKERS = 1           # ⚠️ 미사용. l2.py는 for 루프로 순차 처리한다.
                         #    CAPTCHA를 사람이 풀어야 해서 여러 창이
                         #    동시에 뜨면 안 되기 때문.
L2_DELAY   = 2.0         # 미사용 (L2_CHANNEL_GAP이 대신 쓰인다)
L2_GOTO_DELAY = (1200, 2200)    # 페이지 로드 후 대기(ms) 범위
L2_SCROLL_DELAY = (1000, 2200)  # 스크롤 간 대기(ms) 범위
L2_CHANNEL_GAP = (1.5, 3.5)     # 채널 간 대기(초) 범위
L2_SCROLL_STALL = 2      # 영상이 안 늘어나는 스크롤 N회면 종료
                         # 1로 하면 로딩이 잠깐 느린 것을 바닥으로 오판한다.
# ↑ 전부 (lo, hi) 튜플이다. 고정값이 아니라 범위를 두는 이유:
#   정확히 2초마다 스크롤하면 기계적 패턴이라 탐지된다.

L3_WORKERS = 1           # Playwright라 무거움 + 밴 위험 → 당분간 1 고정
L3_DELAY   = 3.0         # ⚠️ 미사용. l3.py는 PER_VIDEO_SLEEP_BANDS로
                         #    4개 밴드 가중 선택(3~50초)을 쓴다.
                         #    단순 고정 대기보다 분포가 자연스럽다.

# 연속 차단(429/봇감지) 감지 시 중단하는 서킷브레이커
# 계속 때리면 차단이 심해지므로, 일정 횟수 실패하면 스스로 멈춘다.
STOP_ON_BLOCK = 3        # 연속 차단 N회면 해당 단계 중단

# ── 실행 제어 ──
BATCH_LIMIT = None       # None이면 전체, 숫자면 그만큼만 (테스트용)
                         # main.py의 --limit 기본값으로도 쓰인다.
HEADLESS    = False      # persistent profile 사용 시 False 유지 권장.
                         # login.py(False)와 값이 다르면 재인증을 요구할 수 있음.
                         #
                         # ⚠️ L1은 이 값을 무시하고 headless=True로 강제한다.
                         #    L1은 무인 실행이고, L2/L3만 CAPTCHA 수동 해결이
                         #    필요해 창을 띄운다.