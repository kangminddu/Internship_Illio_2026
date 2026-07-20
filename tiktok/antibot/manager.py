# tiktok/antibot/manager.py
"""프록시 상태 관리자.

- 현재 사용 중인 프록시 인덱스(_CURRENT)
- 실패한 프록시의 쿨다운(_DEAD)
- 회전(rotate) / 실패 처리(fail_and_rotate)
- 예외가 "프록시 문제"인지 판별(is_proxy_error)
목록 자체는 proxy.py(로더)에서 가져온다.
"""

import subprocess
import time
import random
from datetime import datetime, timedelta

from tiktok.antibot import proxy


# 크롤러가 쓰는 브라우저 프로세스만 죽이기 위한 식별 패턴.
# browser.py의 PROFILE_DIR 이름과 일치해야 한다.
CHROME_KILL_PATTERN = "tiktok-playwright-profile"

# 프록시 자체 문제로 간주할 네트워크 에러 (goto/launch 공통).
# Chromium net error 문자열 기준.
PROXY_ERROR_MARKERS = (
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_REFUSED",
    "ERR_TIMED_OUT",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_NO_SUPPORTED_PROXIES",
    "ERR_EMPTY_RESPONSE",
)
AUTH_ERROR_MARKERS = (
    "ERR_INVALID_AUTH_CREDENTIALS",
    "ERR_PROXY_AUTH_REQUESTED",
)


_CURRENT = None          # 현재 프록시 인덱스 (프록시 없으면 None)
_DEAD = {}               # {index: 해제 시각}


def is_proxy_error(exc) -> bool:
    """예외가 프록시/네트워크 계층 문제인지 판별.

    goto 타임아웃(느린 페이지)이나 파서 에러 등 '프록시와 무관한' 실패까지
    rotate시키면 멀쩡한 프록시를 낭비하므로, 네트워크 마커가 있을 때만 True.
    """
    msg = str(exc)
    return any(marker in msg for marker in PROXY_ERROR_MARKERS)

def is_auth_error(exc) -> bool:
    msg = str(exc)
    return any(marker in msg for marker in AUTH_ERROR_MARKERS)

def init():
    """시작 프록시를 랜덤 선택. 프록시가 없으면 None 유지."""
    global _CURRENT
    count = proxy.proxy_count()
    _CURRENT = random.randrange(count) if count else None


def current_proxy():
    """현재 사용할 proxy dict. 프록시가 없으면 None."""
    if _CURRENT is None:
        init()
    if _CURRENT is None:
        return None
    return proxy.get_proxy(_CURRENT)


def current_index():
    """현재 프록시 인덱스 (로깅/세대 추적용)."""
    return _CURRENT


def mark_dead(minutes=10):
    """현재 프록시를 지정 시간 동안 dead 처리."""
    if _CURRENT is None:
        return
    _DEAD[_CURRENT] = datetime.now() + timedelta(minutes=minutes)
    p = proxy.get_proxy(_CURRENT)
    print(f"[proxy] dead ({minutes}분) -> [{_CURRENT}] {p['server'] if p else '?'}")


def is_dead(index):
    """dead 여부. 쿨다운이 지났으면 자동 해제."""
    until = _DEAD.get(index)
    if until is None:
        return False
    if datetime.now() >= until:
        del _DEAD[index]
        return False
    return True


def rotate_proxy():
    """dead가 아닌 프록시 중 랜덤 선택 (가능하면 현재와 다른 것)."""
    global _CURRENT

    count = proxy.proxy_count()
    if count == 0:
        print("[proxy] proxy 없음")
        return None

    candidates = [i for i in range(count)
                  if i != _CURRENT and not is_dead(i)]

    if not candidates:
        print("[proxy] 사용 가능한 proxy 없음 → dead 쿨다운 무시하고 재사용")
        candidates = [i for i in range(count) if i != _CURRENT]

    if not candidates:
        print("[proxy] proxy 1개뿐 → 현재 proxy 유지")
        candidates = [_CURRENT]

    _CURRENT = random.choice(candidates)
    _DEAD.pop(_CURRENT, None)

    p = proxy.get_proxy(_CURRENT)
    print(f"[proxy] rotate -> [{_CURRENT}] {p['server']}")
    return p


def kill_chrome():
    """크롤러가 띄운 Chromium만 종료 (사용자 일반 Chrome은 건드리지 않음)."""
    try:
        subprocess.run(["pkill", "-f", CHROME_KILL_PATTERN], check=False)
    except Exception:
        pass
    time.sleep(2)


def fail_and_rotate(minutes=10):
    """현재 프록시 dead 처리 → 브라우저 종료 → 새 프록시 선택."""
    mark_dead(minutes)
    kill_chrome()
    return rotate_proxy()


def dead_count():
    """현재 dead 상태인 프록시 수."""
    return sum(1 for i in range(proxy.proxy_count()) if is_dead(i))


def available_count():
    """지금 쓸 수 있는(dead 아닌) 프록시 수."""
    return sum(1 for i in range(proxy.proxy_count()) if not is_dead(i))


if __name__ == "__main__":
    init()
    print("현재:", proxy.mask(current_proxy()))
    print("회전:", proxy.mask(rotate_proxy()))
    print("실패 처리 후:", proxy.mask(fail_and_rotate(minutes=1)))
    print("dead 수:", dead_count(), "/ 가용:", available_count())