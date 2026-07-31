# -*- coding: utf-8 -*-
"""프록시 경유 시 유튜브 429가 풀리는지 확인하는 진단 스크립트.

주의: 공개 프록시는 트래픽이 노출된다.
      로그인 세션(인스타/틱톡)은 절대 이 경로로 보내지 말 것.
      유튜브 공개 페이지 조회 확인 용도로만 사용한다.
"""
import requests

PROXY = "http://219.249.37.107:8197"
proxies = {"http": PROXY, "https": PROXY}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}


def check(label, url, use_proxy):
    kw = {"headers": HEADERS, "timeout": 15}
    if use_proxy:
        kw["proxies"] = proxies
    try:
        r = requests.get(url, **kw)
        body = r.text
        has_data = "ytInitialData" in body
        sorry = "/sorry/" in r.url or "비정상적인 트래픽" in body
        print(f"[{label}] status={r.status_code} len={len(body):,} "
              f"ytInitialData={'O' if has_data else 'X'} "
              f"{'⛔sorry' if sorry else ''}")
        return r.status_code, has_data
    except Exception as e:
        print(f"[{label}] 실패: {type(e).__name__}: {e}")
        return None, False


print("=" * 60)
# 1) 프록시가 살아있는지 + 어떤 IP로 나가는지
check("IP(직접)", "https://api.ipify.org", use_proxy=False)
check("IP(프록시)", "https://api.ipify.org", use_proxy=True)

print("-" * 60)
# 2) 유튜브 watch 페이지 (L2b가 쓰는 그것)
WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&hl=ko&gl=KR"
check("watch(직접)", WATCH, use_proxy=False)
check("watch(프록시)", WATCH, use_proxy=True)
print("=" * 60)