# tiktok/antibot/proxy.py
"""프록시 목록 로더.

이 모듈은 proxies.txt를 읽어 목록을 제공하는 역할만 한다.
"현재 프록시가 무엇인지 / 회전 / dead 관리"는 전부 manager.py 담당.
"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_FILE = PROJECT_ROOT / "proxies.txt"


def load_proxies(path=PROXY_FILE):
    """proxies.txt 로드.

    형식 (Webshare 기본 export):
        host:port:user:pass
    빈 줄과 #주석은 무시. 형식이 깨진 줄은 경고 출력 후 건너뜀.
    """
    path = Path(path)

    if not path.exists():
        print(f"[proxy] 파일 없음: {path} → 프록시 없이 동작")
        return []

    proxies = []

    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            # 비밀번호에 ':'가 포함될 수 있으므로 최대 3번만 분리
            parts = line.split(":", 3)

            if len(parts) != 4:
                print(f"[proxy] {path.name}:{lineno} 형식 오류, 건너뜀: {line[:40]}")
                continue

            host, port, user, password = parts

            if not port.isdigit():
                print(f"[proxy] {path.name}:{lineno} 포트 오류, 건너뜀: {line[:40]}")
                continue

            proxies.append({
                "server": f"http://{host}:{port}",
                "username": user,
                "password": password,
            })

    return proxies


_PROXIES = load_proxies()


def get_proxy(index):
    """인덱스로 proxy 조회. 범위 밖이면 None."""
    if 0 <= index < len(_PROXIES):
        return _PROXIES[index]
    return None


def proxy_count():
    return len(_PROXIES)


def mask(p):
    """비밀번호를 가린 출력용 표현."""
    if p is None:
        return None
    return {**p, "password": "****"}


if __name__ == "__main__":
    print("Proxy 개수 :", proxy_count())
    for i in range(proxy_count()):
        print(f"  [{i}] {mask(get_proxy(i))}")