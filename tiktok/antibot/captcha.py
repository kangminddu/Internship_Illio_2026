# tiktok/antibot/captcha.py
"""TikTok Captcha / Soft-Block Detector.

역할
------
현재 Page가 Captcha 또는 Soft Block 상태인지 감지한다.
이 모듈은 감지만 담당한다.
Proxy 변경 / Browser 재시작은 manager.py와 browser.py에서 수행한다.

TikTok captcha 렌더링 관찰 (실측: iframe 0 / shadow_dom 1 / regular_dom 2)
------
- iframe 방식이 아니라 메인 문서의 일반 DOM + shadow DOM 조합으로 뜬다.
- 컨테이너는 captcha가 안 떠 있어도 숨겨진 채 미리 마운트될 수 있으므로
  "존재"가 아니라 "보이는지(:visible)"를 검사해야 오탐이 없다.
- page.content()와 문구 검사는 shadow DOM 내부를 보지 못한다.
  → shadow root 재귀 탐색(JS)을 별도 단계로 둔다.

감지 순서 (빠르고 확실한 것부터)
------
1. URL       : captcha/verify 경로로 리다이렉트된 경우
2. Selector  : captcha 컨테이너가 실제로 "보이는" 경우 (open shadow 관통)
3. Shadow    : shadow root 내부까지 JS로 재귀 탐색
4. HTML 문구 : 사용자에게 보이는 captcha 안내 문구 (shadow 밖 한정)
5. iframe    : captcha 전용 iframe (관찰상 0개지만 안전망으로 유지)
"""

from typing import Optional


# URL에 포함되면 captcha 가능성이 매우 높음.
# 주의: "challenge"는 넣지 않는다 — TikTok 해시태그 페이지 경로가
# /challenge/... 라서 정상 페이지를 오탐한다.
URL_KEYWORDS = (
    "captcha",
    "verify",
)

# captcha 컨테이너 셀렉터.
# 반드시 :visible과 함께 사용한다 — 숨겨진 채 미리 마운트된 컨테이너 오탐 방지.
CAPTCHA_SELECTORS = (
    "#captcha_container",
    "#captcha-verify-image",
    "div[class*='captcha_verify']",
    "div[class*='captcha-verify']",
)

# 사용자에게 보이는 안내 문구 (구체적인 문장만).
# 주의: "captcha", "verification" 같은 단어 하나짜리는 넣지 않는다 —
# TikTok 정상 페이지의 JS 번들에도 항상 포함되어 있어 전부 오탐된다.
HTML_KEYWORDS = (
    "security verification",
    "verify to continue",
    "drag the slider",
    "rotate the object",
    "complete the security check",
    "confirm you are human",
)

# captcha 전용 iframe src 패턴
IFRAME_KEYWORDS = (
    "captcha",
    "verify",
)

# shadow root 내부까지 재귀 탐색해서 "보이는" captcha 요소를 찾는 JS.
# page.content()가 shadow 내부를 포함하지 않으므로 이 단계가 필요하다.
_SHADOW_SEARCH_JS = """
() => {
    const isVisible = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const walk = (root) => {
        for (const el of root.querySelectorAll("*")) {
            const key = ((el.id || "") + " " + (el.className || "")).toLowerCase();
            if (key.includes("captcha") && isVisible(el)) {
                return el.id || el.className || "captcha-element";
            }
            if (el.shadowRoot) {
                const hit = walk(el.shadowRoot);
                if (hit) return hit;
            }
        }
        return null;
    };
    return walk(document);
}
"""


async def reason(page, html: Optional[str] = None) -> Optional[str]:
    """captcha/soft-block이면 원인 문자열, 정상이면 None.

    Parameters
    ----------
    page : Playwright Page
    html : 이미 확보한 page.content() 결과가 있으면 전달.
           재호출 없이 재사용한다 (l1의 fetch_html 반환값 등).

    Returns
    -------
    None                            정상
    "url:..."    / "selector:..." /
    "shadow:..." / "html:..."     /
    "iframe:..."                    감지됨 (원인:키워드)
    """
    # 1) URL 검사 — 가장 싸고 확실
    url = page.url.lower()
    for keyword in URL_KEYWORDS:
        if keyword in url:
            return f"url:{keyword}"

    # 2) Selector 검사 — 컨테이너가 실제로 "보이는가"
    #    (Playwright locator는 open shadow root를 관통한다)
    try:
        for selector in CAPTCHA_SELECTORS:
            if await page.locator(f"{selector}:visible").count() > 0:
                return f"selector:{selector}"
    except Exception:
        pass

    # 3) Shadow DOM 재귀 탐색 — 셀렉터 목록이 낡았을 때의 안전망
    try:
        hit = await page.evaluate(_SHADOW_SEARCH_JS)
        if hit:
            return f"shadow:{str(hit)[:60]}"
    except Exception:
        pass

    # 4) HTML 문구 검사 (shadow 내부는 포함되지 않음에 유의)
    try:
        if html is None:
            html = await page.content()
        html_lower = html.lower()
        for keyword in HTML_KEYWORDS:
            if keyword in html_lower:
                return f"html:{keyword}"
    except Exception:
        pass

    # 5) iframe 검사 (frames[0]은 메인 프레임이라 1)과 중복이므로 제외.
    #    관찰상 TikTok은 iframe을 안 쓰지만, 지역/버전별 변형 대비 안전망)
    try:
        for frame in page.frames[1:]:
            src = frame.url.lower()
            for keyword in IFRAME_KEYWORDS:
                if keyword in src:
                    return f"iframe:{keyword}"
    except Exception:
        pass

    return None


async def detect(page, html: Optional[str] = None) -> bool:
    """captcha / soft block 여부."""
    return (await reason(page, html)) is not None


async def print_status(page):
    """디버깅용."""
    r = await reason(page)
    if r is None:
        print("[captcha] PASS")
    else:
        print(f"[captcha] DETECT -> {r}")


if __name__ == "__main__":
    print("captcha.py는 Playwright Page 객체에서 호출되는 모듈입니다.")