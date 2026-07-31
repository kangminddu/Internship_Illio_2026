# tiktok/antibot/proxy.py
"""프록시 목록 로더.

이 모듈은 proxies.txt를 읽어 목록을 제공하는 역할만 한다.
"현재 프록시가 무엇인지 / 회전 / dead 관리"는 전부 manager.py 담당.

★ 현재 미사용이다.
------
proxies.txt가 레포에 없어서 _PROXIES가 항상 빈 목록이다.
그리고 이 모듈을 부르는 manager.py, 그 manager를 쓰는 session.py도
L1/L2/L3 어디서도 호출되지 않는다.
(세 단계 모두 browser.create_context()를 직접 부른다)

왜 만들었나 — 그리고 왜 안 썼나
------
크롤링 규모가 커지면 단일 IP로는 한계가 온다는 판단으로 준비했다.
실제로 유튜브는 L2b에서 서버 IP가 통째로 차단됐다.

그런데 틱톡 실패 원인을 조사해보니 IP가 변수가 아니었다.
EC2·가정용·모바일 핫스팟 세 종류로 테스트했는데 실패율이 같았다.
틱톡 서버가 SSR 응답을 확률적으로 거부하는 것이라, 프록시를 돌려도
같은 비율로 실패한다. → 도입 우선순위가 밀렸다.

유료 프록시 비용 문제도 있었다. "유료 대신 직접 만들어보라"는
방향과도 맞지 않았고.

리뷰에서는 미사용 코드로 남아 있다는 걸 먼저 밝히는 게 낫다.
"준비했지만 원인 분석 결과 IP가 변수가 아니어서 도입하지 않았다"까지가
정확한 설명이다.
"""

from pathlib import Path


# 파일 위치 기준으로 프로젝트 루트를 찾는다.
#   __file__ = .../tiktok/antibot/proxy.py
#   parents[0] = antibot, parents[1] = tiktok, parents[2] = 프로젝트 루트
# 실행 디렉터리와 무관하게 같은 곳을 가리킨다.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROXY_FILE = PROJECT_ROOT / "proxies.txt"
# ⚠️ 이 파일은 .gitignore 대상이어야 한다.
#    프록시 계정 정보(user:pass)가 들어가기 때문.
#    현재는 파일 자체가 없어 문제가 드러나지 않았다.


def load_proxies(path=PROXY_FILE):
    """proxies.txt 로드.

    형식 (Webshare 기본 export):
        host:port:user:pass
    빈 줄과 #주석은 무시. 형식이 깨진 줄은 경고 출력 후 건너뜀.

    깨진 줄에서 예외를 던지지 않고 건너뛰는 이유:
    프록시 100개 중 1줄이 잘못됐다고 크롤러 전체가 못 뜨면 곤란하다.
    경고만 남기고 나머지로 계속 간다.
    (크롤러들의 '개별 실패 격리' 원칙과 같은 발상)
    """
    path = Path(path)

    if not path.exists():
        # 파일이 없어도 예외를 안 던진다. 프록시는 선택 기능이라
        # 없으면 그냥 직접 연결로 동작해야 한다.
        print(f"[proxy] 파일 없음: {path} → 프록시 없이 동작")
        return []

    proxies = []

    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            # 비밀번호에 ':'가 포함될 수 있으므로 최대 3번만 분리
            #
            # ★ split(":")로 하면 비밀번호에 콜론이 있을 때 5조각이 나서
            #   len(parts) != 4에 걸려 멀쩡한 줄이 버려진다.
            #   maxsplit=3이면 앞 세 개만 자르고 나머지는 통째로 남는다.
            #     "1.2.3.4:8080:user:pa:ss" → ['1.2.3.4','8080','user','pa:ss']
            parts = line.split(":", 3)

            if len(parts) != 4:
                # 어느 줄이 잘못됐는지 줄 번호로 알려준다.
                # 내용은 40자만 잘라서 출력 — 비밀번호가 통째로
                # 로그에 남는 것을 줄이려는 의도.
                print(f"[proxy] {path.name}:{lineno} 형식 오류, 건너뜀: {line[:40]}")
                continue

            host, port, user, password = parts

            # 포트가 숫자인지 검증.
            # host와 port 순서가 바뀐 줄을 잡아낸다.
            if not port.isdigit():
                print(f"[proxy] {path.name}:{lineno} 포트 오류, 건너뜀: {line[:40]}")
                continue

            # Playwright의 proxy 인자 형식에 맞춘다.
            #   {"server": "http://host:port", "username": ..., "password": ...}
            # 여기서 변환해두면 호출부는 그대로 넘기기만 하면 된다.
            proxies.append({
                "server": f"http://{host}:{port}",
                "username": user,
                "password": password,
            })

    return proxies


# ★ 모듈 로드 시점에 한 번만 읽는다.
#   프록시 목록은 실행 중 바뀌지 않으므로 매번 파일을 읽을 이유가 없다.
#
#   다만 import 부작용이 있다. 이 모듈을 import하는 것만으로
#   파일을 읽고 "[proxy] 파일 없음" 메시지가 출력된다.
#   (지금은 아무도 import하지 않아 드러나지 않는다)
_PROXIES = load_proxies()


def get_proxy(index):
    """인덱스로 proxy 조회. 범위 밖이면 None.

    manager.py가 인덱스를 증가시키며 회전시키는 구조.
    이 모듈은 '무엇이 몇 번인지'만 알고, '지금 몇 번을 쓸지'는 모른다.
    → 목록 관리와 상태 관리를 분리한 것.
    """
    if 0 <= index < len(_PROXIES):
        return _PROXIES[index]
    return None


def proxy_count():
    return len(_PROXIES)


def mask(p):
    """비밀번호를 가린 출력용 표현.

    로그나 디버그 출력에 프록시 정보를 찍을 때 쓴다.
    비밀번호가 터미널 히스토리나 로그 파일에 남으면 곤란하다.
    """
    if p is None:
        return None
    return {**p, "password": "****"}   # 원본을 안 건드리고 사본을 만든다


if __name__ == "__main__":
    # 단독 실행하면 로드 결과를 확인할 수 있다.
    #   python -m tiktok.antibot.proxy
    # proxies.txt를 새로 넣었을 때 형식이 맞는지 검증하는 용도.
    print("Proxy 개수 :", proxy_count())
    for i in range(proxy_count()):
        print(f"  [{i}] {mask(get_proxy(i))}")