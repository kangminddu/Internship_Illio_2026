# youtube/config.py
#
# 유튜브 파이프라인 전용 설정.
#
# ⚠️ 이 파일은 .gitignore에 있다. DB 비밀번호가 들어가기 때문.
#    새 환경에 배포할 때는 직접 만들어야 한다.
#    (실무에서는 환경변수로 빼는 게 맞다 — 리뷰 안건)

import os

EXPORT_DIR = "youtube/output"
os.makedirs(EXPORT_DIR, exist_ok=True)

DB = dict(host="127.0.0.1", port=3306, user="root", password="",
          database="fandom_crm", charset="utf8mb4")
# charset=utf8mb4: 이모지(4바이트)를 담아야 한다.
# 채널명·댓글에 이모지가 흔해서 utf8(3바이트)로는 저장이 깨진다.


# ── L1 (requests) ──
# 속도는 워커 수가 아니라 L1_MIN_INTERVAL(전역 요청 간격)이 결정한다.
# 워커는 파싱/DB 시간을 겹치는 용도 → 3이면 충분.
# 중요
# GlobalRateLimiter가 IP 기준으로 요청을 직렬화하므로,
# 워커를 40개로 늘려도 처리량은 그대로다.
L1_REFRESH_DAYS = 7      # L1 재수집 주기. 가이드라인 "L1 주 1회 갱신" 대응.
                         #   이게 없으면 한 번 성공한 채널은 영원히 skip되어
                         #   구독자 시계열이 더 이상 쌓이지 않는다.
L1_WORKERS      = 4
L1_MIN_INTERVAL = 1.2    # 전역 요청 간 최소 간격(초). 안정되면 0.8까지 낮춰볼 것
L1_BACKOFF_BASE = 60     # 429 시 첫 대기(초). 이후 2배씩: 60→120→240...
L1_MAX_RETRY    = 3      # 채널당 429 재시도 횟수 (백오프 라운드와 연동)
L1_DELAY        = 0.35   # (구버전 호환용, 새 크롤러는 사용 안 함)
L1_REST_EVERY   = 1000   # 채널 N개 처리마다 휴식.
L1_REST_SECONDS = 2100   #   초당 속도가 느려도 8시간 연속이면 차단된다.
                         #   세션을 잘게 끊어 '누적 요청량'을 관리하는 장치.

# ── L2a (requests) ── 채널당 2요청 (videos 탭 + shorts 탭)
L2A_WORKERS = 4
L2A_DELAY   = 0.4        # (미사용)
L2A_MIN_INTERVAL = 1.2
L2_REFRESH_DAYS = 7

# ── L2b (requests, watch page) ── 채널당 최대 30요청 (영상당 1회)
#
# ⚠️ L2a와 L2b의 요청량 차이가 8배다.
#    L2a는 요청 2번으로 콘텐츠 15개를 얻는데, L2b는 1:1이다.
#    실제로 L2a 완료 직후 L2b를 0.5초 간격으로 시작했다가
#    5초 만에 429를 7번 받고 서버 IP가 구글에 차단됐다.
#    → "한 단계에서 검증된 안전 마진을 다른 단계에 그대로 쓸 수 없다"
L2B_WORKERS = 10         # ⚠️ 위 L1 주석의 원칙("3이면 충분")과 어긋난다.
                         #    전역 리미터가 있어 처리량은 같고 DB 커넥션만 10개 점유.
L2B_DELAY   = 0.3        # (미사용)
L2_RECENT_MONTHS = 6     # (미사용 — crawler_l2.py는 RECENT_N=15로 '개수' 제한)
L2B_MIN_INTERVAL = 1.0
L2_REST_EVERY = 2000     # L2a/L2b가 공유
L2_REST_SECONDS = 2100 # 35분

# ── L3 (Playwright) ──
# 유일하게 브라우저를 쓰는 단계.
# 유튜브 댓글은 초기 HTML에 없고, 스크롤해야 youtubei/v1/next API가
# 호출되는 구조라 실제 스크롤 이벤트를 발생시켜야 한다.
L3_VIDEOS_PER_CHANNEL = 10   # 가이드라인: 영상별 댓글 수집
L3_MAX_SCROLLS = 20
L3_COMMENT_LIMIT = 30
L3_WORKERS = 3               # 브라우저 컨텍스트 3개 = 메모리 ~750MB.
                             # 4GB 서버에서 MySQL과 공존 가능한 상한.
L3_MIN_INTERVAL = 1.5        # 브라우저는 페이지 로드가 무거워 더 보수적으로
L3_REST_EVERY = 400
L3_REST_SECONDS = 1200       # 20분

# ── 공통 ──
BATCH_LIMIT = None       # None이면 전체. 숫자를 넣으면 표본만 (테스트/부분 실행)
STOP_ON_429 = 4   # 의미 변경: '429 응답 건수'가 아니라 '전역 백오프 라운드 수'.
                  # 백오프 : 429 시 재시도
                  # 60+120+240초 백오프 후에도 429가 계속되면 중단.
                  #
                  # 건수 기준이면 워커 4개가 동시에 429를 받았을 때
                  # 순식간에 한도에 닿아버린다. 라운드 기준이어야
                  # "물러섰는데도 여전히 막힌다"를 판단할 수 있다.

# -- EMAIL (치지직 보강 수집) --
# 한국 크리에이터가 유튜브 설명란에 치지직 링크만 걸어두고
# 연락처는 치지직에 적어둔 경우가 많아 추가한 단계.
CHZZK_USER_AGENT = "Mozilla/5.0"
CHZZK_TIMEOUT = 15
CHZZK_DELAY = 1.0