# -*- coding: utf-8 -*-
"""
rate_control.py — 크롤러 공용 트래픽 제어 (L1 v4에서 검증된 패턴의 모듈화)

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
        self.min_interval = min_interval
        self.backoff_base = backoff_base
        self.rest_every = rest_every
        self.rest_seconds = rest_seconds
        self.warmup_count = warmup_count
        self.name = name
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._pause_until = 0.0
        self._backoff_level = 0
        self._warmup_left = 0
        self._acquired = 0          # 총 발사 수 (휴식 트리거 기준)

    def acquire(self, stop_flag):
        """요청 슬롯 확보. 백오프/휴식 중이면 풀릴 때까지 대기.
        stop_flag가 서면 False 반환."""
        while not stop_flag.is_set():
            with self._lock:
                now = time.time()
                wait_pause = self._pause_until - now
                if wait_pause <= 0:
                    wait_slot = self._next_at - now
                    if wait_slot <= 0:
                        interval = self.min_interval
                        if self._warmup_left > 0:
                            interval *= 2
                            self._warmup_left -= 1
                        jitter = random.uniform(0, interval * 0.3)
                        self._next_at = now + interval + jitter
                        self._acquired += 1
                        # 주기적 휴식: N회 발사마다
                        if (self.rest_every and
                                self._acquired % self.rest_every == 0):
                            self._start_rest_locked(now)
                        return True
                    sleep_for = wait_slot
                else:
                    sleep_for = min(wait_pause, 5.0)
            time.sleep(min(sleep_for, 5.0))
        return False

    def _start_rest_locked(self, now):
        self._pause_until = now + self.rest_seconds
        self._warmup_left = self.warmup_count
        resume = time.strftime("%H:%M", time.localtime(now + self.rest_seconds))
        print(f"\n😴 [{self.name}] 요청 {self._acquired:,}회 — "
              f"{self.rest_seconds // 60}분 휴식 (재개 {resume}, "
              f"재개 후 {self.warmup_count}회 저속 워밍업)")

    def report_429(self, retry_after=None):
        """429 발생 → 전 워커 공동 백오프. 연속 라운드 수 반환.
        retry_after(초)가 오면 그 값을 존중 (최소 backoff_base)."""
        with self._lock:
            now = time.time()
            if now >= self._pause_until:
                self._backoff_level += 1
                pause = self.backoff_base * (2 ** (self._backoff_level - 1))
                if retry_after:
                    try:
                        pause = max(float(retry_after), self.backoff_base)
                    except (TypeError, ValueError):
                        pass
                self._pause_until = now + pause
                self._warmup_left = self.warmup_count
                print(f"\n⏸️  [{self.name}] HTTP 429 → 전 워커 {int(pause)}초 정지 "
                      f"(백오프 라운드 {self._backoff_level})")
            return self._backoff_level

    def report_success(self):
        with self._lock:
            if self._backoff_level:
                print(f"✅ [{self.name}] 정상 응답 재개 — 백오프 레벨 리셋")
            self._backoff_level = 0

    @property
    def total_requests(self):
        return self._acquired