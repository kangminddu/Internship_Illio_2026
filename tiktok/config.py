# tiktok/config.py
# TikTok 파이프라인 전용 설정. code/ 에 의존하지 않는 자립 구조.

import os

# ── 경로 ──
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))   # .../tiktok
EXPORT_DIR = os.path.join(BASE_DIR, "output")
SESSION_DIR = os.path.join(BASE_DIR, "session")
os.makedirs(EXPORT_DIR, exist_ok=True)
os.makedirs(SESSION_DIR, exist_ok=True)

# Playwright 로그인 세션 저장 파일 (L3에서 로그인 필요할 경우 대비)
SESSION_PATH = os.path.join(SESSION_DIR, "tiktok_state.json")

# ── DB (YouTube와 같은 fandom_crm 공유, platform 컬럼으로 구분) ──
DB = dict(host="127.0.0.1", port=3306, user="root", password="",
          database="fandom_crm", charset="utf8mb4")

PLATFORM = "tiktok"   # DB 저장 시 platform 컬럼 값

# ── 수집 스펙 (가이드라인 기준) ──
L2_PERIOD_MONTHS = 3     # 최근 3개월
L2_MIN_VIDEOS    = 15    # 최소 15개 (미달 시 기간 2배 확장 규칙은 로직에서 처리)
L3_COMMENT_LIMIT = 50    # 영상당 수집할 최대 댓글 수 (초기값, 튜닝 대상)
L3_MAX_SCROLLS   = 20    # 댓글 로딩 스크롤 상한


# ── 크롤링 속도/안정성 (⚠️ YouTube보다 훨씬 보수적으로) ──
# YouTube는 429 0건이었지만 TikTok은 차단 공격적 → 살살.
# 초기엔 병렬 없이 순차(worker=1)로 방식부터 검증하고, 안정화 후 늘린다.
L1_WORKERS = 3
L1_DELAY   = 2.0         # 요청 간 최소 대기(초)

L2_WORKERS = 1
L2_DELAY   = 2.0
L2_GOTO_DELAY = (1200,2200)
L2_SCROLL_DELAY = (1000,2200)
L2_CHANNEL_GAP = (1.5, 3.5)
L2_SCROLL_STALL = 2

L3_WORKERS = 1           # Playwright라 무거움 + 밴 위험 → 당분간 1 고정
L3_DELAY   = 3.0

# 연속 차단(429/봇감지) 감지 시 중단하는 서킷브레이커
STOP_ON_BLOCK = 3        # 연속 차단 N회면 해당 단계 중단

# ── 실행 제어 ──
BATCH_LIMIT = None   # None이면 전체, 숫자면 그만큼만 (테스트용)
HEADLESS    = False      # 초기 개발/디버깅엔 브라우저 보이게. 안정화 후 True