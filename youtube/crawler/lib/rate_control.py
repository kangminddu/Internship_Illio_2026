# -*- coding: utf-8 -*-
"""
youtube/crawler/lib/rate_control.py — 크롤러 공용 트래픽 제어

왜 이 파일이 존재하는가
------
L1을 만들면서 "429를 어떻게 피할 것인가"를 여러 버전에 걸쳐 다듬었고,
그 결과 패턴을 L2a/L2b가 재사용할 수 있게 모듈로 뺐다.

핵심 발견: 유튜브의 차단 기준은 하나가 아니다.
세 가지를 각각 따로 봐야 했고, 대응 장치도 셋이다.

  1) 초당 요청 수      → min_interval + jitter
  2) 차단 후 재시도    → 429 지수 백오프 (전 워커 공동 정지)
  3) 세션 누적 요청량  → 주기적 휴식 + 재개 후 slow start

3번이 특히 반직관적이다. 초당 속도가 아무리 느려도 8시간을 쉬지 않고
긁으면 차단된다. "얼마나 빠른가"가 아니라 "얼마나 오래, 총 몇 번"도 본다.
→ 세션을 잘게 끊어야 한다.

'전역'이라는 점이 이 클래스의 전부다
------
워커마다 time.sleep(1.2)를 걸면, 워커 4개일 때 실제 속도는 초당 3.3건이 된다.
병렬을 늘릴수록 차단 위험이 커지는 구조가 되어버린다.

여기서는 인스턴스 하나를 모든 워커가 공유하고 lock으로 직렬화하므로,
워커를 40개로 늘려도 IP 기준 요청 속도는 그대로다.
워커의 역할은 "A가 파싱/DB 작업하는 동안 B가 요청을 보내는 것"뿐.

⚠️ 알려진 한계
------
report_429()가 "이미 정지 중이면 레벨을 안 올린다". 그리고
report_success()가 레벨을 즉시 0으로 리셋한다. 복귀 후 slow start로
첫 요청이 대체로 성공하므로, 실제로는 백오프 레벨이 2 이상으로
잘 올라가지 않는다. → 지수 백오프(60→120→240)와 STOP_ON_429 중단이
설계 의도대로 동작하지 않을 가능성이 있다.
L1/L2a에서 429가 0건이라 이 경로는 실측 검증이 안 된 상태다.

기능:
  - 전역 rate limiter: 워커 수와 무관하게 IP 기준 요청 간격 고정 (+jitter)
  - 429 백오프: 전 워커 공동 정지, 지수 증가 (base × 2^라운드)
  - 주기적 휴식: N회 요청마다 M초 전 워커 휴식 (연속 세션 누적량 관리)
  - slow start: 휴식/백오프 복귀 후 30회 요청은 2배 간격

사용:
  from youtube.crawler.lib.rate_control import RateController
  rc = RateController(min_interval=1.2, backoff_base=60,
                      rest_every=2000, rest_seconds=2100, name="L2b")
  ...
  if not rc.acquire(stop_flag): return        # 요청 직전마다
  rounds = rc.report_429()                     # 429 받으면
  rc.report_success()                          # 정상 응답이면
"""
import time
import random
import threading


class RateController:
    def __init__(self, min_interval, backoff_base=60,
                 rest_every=0, rest_seconds=0, warmup_count=30, name=""):
        self.min_interval = min_interval    # 요청 간 최소 간격(초)
        self.backoff_base = backoff_base    # 429 첫 대기(초). 이후 2배씩
        self.rest_every = rest_every        # N회 요청마다 휴식 (0이면 비활성)
        self.rest_seconds = rest_seconds    # 휴식 시간(초)
        self.warmup_count = warmup_count    # 복귀 후 저속으로 갈 요청 수
        self.name = name                    # 로그 구분용 ("L2a" / "L2b")

        # ── 공유 상태. 반드시 lock 안에서만 만진다 ──
        self._lock = threading.Lock()
        self._next_at = 0.0        # 다음 요청 가능 시각 (평상시 간격 제어)
        self._pause_until = 0.0    # 전 워커 정지 종료 시각 (429 백오프 or 주기 휴식)
        self._backoff_level = 0    # 연속 429 라운드 수
        self._warmup_left = 0      # 남은 워밍업 횟수
        self._acquired = 0         # 총 발사 수 (휴식 트리거 기준)

    def acquire(self, stop_flag):
        """요청 슬롯 확보. 백오프/휴식 중이면 풀릴 때까지 대기.
        stop_flag가 서면 False 반환.

        구조가 while + 짧은 sleep 반복인 이유:
          - lock을 쥔 채로 자면 다른 워커가 전부 멈춘다.
            → lock 안에서는 '얼마나 잘지'만 계산하고, 밖에서 잔다.
          - 35분 휴식 중에도 5초마다 깨어나 stop_flag를 확인한다.
            그래야 Ctrl+C에 반응할 수 있다. (안 그러면 35분을 기다려야 함)

        정지 상태가 두 종류인데 한 변수(_pause_until)로 처리한다.
        429 백오프든 주기 휴식이든 "전 워커가 그 시각까지 멈춘다"는
        동작이 같기 때문.
        """
        while not stop_flag.is_set():
            with self._lock:
                now = time.time()
                wait_pause = self._pause_until - now
                if wait_pause <= 0:                 # 정지 상태 아님
                    wait_slot = self._next_at - now
                    if wait_slot <= 0:              # 내 차례가 왔다
                        interval = self.min_interval
                        if self._warmup_left > 0:
                            # slow start: 휴식/백오프 복귀 직후 30회는 2배 간격.
                            # 쉬었다가 갑자기 원래 속도로 몰아치면
                            # 차단 판정을 다시 유발한다.
                            interval *= 2
                            self._warmup_left -= 1
                        # jitter: 정확히 1.2초마다 요청하면 기계처럼 규칙적이라
                        # 봇 탐지에 걸리기 쉽다. 0~30% 랜덤을 섞는다.
                        jitter = random.uniform(0, interval * 0.3)
                        self._next_at = now + interval + jitter
                        self._acquired += 1

                        # 주기적 휴식: N회 발사마다
                        # (요청 수 기준. 채널 수가 아니라 실제 HTTP 요청 수)
                        if (self.rest_every and
                                self._acquired % self.rest_every == 0):
                            self._start_rest_locked(now)
                        return True
                    sleep_for = wait_slot           # 다음 슬롯까지 대기
                else:
                    sleep_for = min(wait_pause, 5.0)  # 정지 해제까지 대기
            # lock 밖에서 잔다. 5초로 쪼개 stop_flag를 주기적으로 확인.
            time.sleep(min(sleep_for, 5.0))
        return False

    def _start_rest_locked(self, now):
        """주기 휴식 시작. 이름에 _locked를 붙인 건 '호출자가 이미 lock을
        쥐고 있어야 한다'는 규약을 드러내기 위함. (여기서 다시 잠그면 데드락)"""
        self._pause_until = now + self.rest_seconds
        self._warmup_left = self.warmup_count
        # 재개 예정 시각을 미리 출력한다. 35분 휴식이면
        # "멈춘 건가 쉬는 건가"를 사람이 판단할 수 있어야 한다.
        resume = time.strftime("%H:%M", time.localtime(now + self.rest_seconds))
        print(f"\n😴 [{self.name}] 요청 {self._acquired:,}회 — "
              f"{self.rest_seconds // 60}분 휴식 (재개 {resume}, "
              f"재개 후 {self.warmup_count}회 저속 워밍업)")

    def report_429(self, retry_after=None):
        """429 발생 → 전 워커 공동 백오프. 연속 라운드 수 반환.
        retry_after(초)가 오면 그 값을 존중 (최소 backoff_base).

        반환값(라운드 수)을 호출자가 STOP_ON_429와 비교해
        "물러섰는데도 계속 막히면 전체 중단"을 판단한다.

        'if now >= self._pause_until' 조건이 중요하다:
          워커 4개가 거의 동시에 429를 받으면 이 함수가 4번 불린다.
          조건이 없으면 레벨이 한 번에 1→4로 뛰어 백오프가 480초가 되고,
          STOP_ON_429에도 즉시 도달한다.
          이미 정지 중이면 "같은 사건"으로 보고 레벨을 안 올린다.

        retry_after: 유튜브가 Retry-After 헤더를 주면 그 값을 쓴다.
          서버가 알려주는 값이 우리 추정보다 정확하다. 단, 너무 짧게
          오는 경우를 대비해 backoff_base를 하한으로 둔다.
        """
        with self._lock:
            now = time.time()
            if now >= self._pause_until:
                self._backoff_level += 1
                pause = self.backoff_base * (2 ** (self._backoff_level - 1))  # 60→120→240
                if retry_after:
                    try:
                        pause = max(float(retry_after), self.backoff_base)
                    except (TypeError, ValueError):
                        pass
                self._pause_until = now + pause
                self._warmup_left = self.warmup_count   # 복귀 후 저속 시작
                print(f"\n⏸️  [{self.name}] HTTP 429 → 전 워커 {int(pause)}초 정지 "
                      f"(백오프 라운드 {self._backoff_level})")
            return self._backoff_level

    def report_success(self):
        """정상 응답 → 백오프 레벨 리셋.

        ⚠️ 즉시 0으로 되돌리는 게 이 클래스의 약점이다.
        복귀 후 slow start 덕에 첫 요청이 대체로 성공하므로,
        429 → 60초 → 성공 → 레벨0 → 429 → 60초 ... 를 반복하며
        지수 백오프가 사실상 작동하지 않을 수 있다.
        (연속 성공 N회 후 리셋하는 방식이 일반적이다)
        """
        with self._lock:
            if self._backoff_level:
                print(f"✅ [{self.name}] 정상 응답 재개 — 백오프 레벨 리셋")
            self._backoff_level = 0

    @property
    def total_requests(self):
        """지금까지 발사한 총 요청 수. 크롤러가 진행 로그에 찍는다."""
        return self._acquired