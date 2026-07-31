# tiktok/antibot/stealth.py
"""
브라우저 자동화 흔적 제거 (fingerprint 패치).

무엇을 숨기는가
------
Playwright로 띄운 Chromium은 일반 브라우저와 다른 신호를 흘린다:
    navigator.webdriver === true
    navigator.plugins 가 비어 있음
    navigator.hardwareConcurrency, deviceMemory 가 비정상적인 값
    WebGL 벤더 문자열이 SwiftShader (소프트웨어 렌더링)
    Chrome 전용 객체(window.chrome) 부재

봇 탐지 스크립트는 이런 값을 조합해 점수를 매긴다.
playwright_stealth가 대부분을 패치하고, 여기서 몇 개를 더 얹는다.

★ 적용 범위 주의
------
L2/L3는 browser.create_context()를 통해 이 모듈을 쓴다.
L1은 쓰지 않는다 — stealth를 켜니 모든 요청이
ERR_HTTP_RESPONSE_CODE_FAILURE로 실패했다. (버전 호환 문제로 추정)
같은 라이브러리가 단계마다 다르게 동작한 사례.
"""
from playwright_stealth import Stealth

# 전역 Stealth 객체 (한 번만 생성)
# 내부에 주입할 JS 스크립트를 담고 있어, 페이지마다 새로 만들 필요가 없다.
_stealth = Stealth()


async def apply(page):
    """새 page를 만들 때마다 호출."""

    # 1) viewport를 먼저 잡는다.
    #    stealth 스크립트 일부가 screen/window 크기에 의존해 fingerprint를
    #    맞추므로, viewport 설정 후 stealth를 적용하는 편이 일관적이다.
    #
    #    ⚠️ 여기 1920x1080은 browser.py의 VIEWPORT(1440x900)와 다르다.
    #      persistent context가 이미 1440x900으로 열렸는데 페이지에서
    #      1920x1080으로 덮어쓰면 window.screen과 window.innerWidth가
    #      어긋난 조합이 된다. 그 자체가 봇 신호가 될 수 있다.
    #      (지금까지 문제는 없었지만 정리 대상)
    await page.set_viewport_size({
        "width": 1920,
        "height": 1080,
    })

    await _stealth.apply_stealth_async(page)

    # 2) 사람이 쓰는 브라우저처럼 헤더 설정.
    #    Accept 헤더에서 이미지 타입을 빼서, 서버가 이미지를 덜 밀어주도록
    #    유도(대역폭 절약). 실제 이미지 차단은 browser.py의 route가 담당하고,
    #    이건 보조 신호. (L1은 텍스트 데이터만 필요)
    #
    #    ⚠️ 주석은 Accept를 언급하는데 실제로는 Accept-Language만 설정한다.
    #      의도했던 Accept 조작이 빠졌거나, 주석이 낡았다.
    await page.set_extra_http_headers({
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    })

    # 다크모드 에뮬레이션.
    # prefers-color-scheme는 지문 요소 중 하나다.
    # 기본값(light)보다 dark 사용자가 실제로 많아 자연스럽다.
    await page.emulate_media(color_scheme="dark")


async def prepare_context(context):
    """context 단위 설정. 이후 fingerprint 패치도 여기 추가.

    add_init_script는 '페이지가 로드되기 전에' 실행된다.
    틱톡의 탐지 스크립트가 돌기 전에 값을 바꿔놔야 하므로
    page.evaluate가 아니라 이걸 써야 한다.

    context 단위라 이 컨텍스트의 모든 페이지에 자동 적용된다.
    """
    await context.add_init_script("""
// PDF 뷰어 활성화 여부. headless Chromium은 기본이 false인데,
// 일반 데스크톱 Chrome은 true다. → false면 headless 의심 신호.
Object.defineProperty(navigator, 'pdfViewerEnabled', {
    get: () => true,
});

// CPU 코어 수. 서버가 2코어인데 그대로 노출하면
// "가상 환경/서버"로 보인다. 일반 데스크톱 값인 8로 위장.
Object.defineProperty(navigator, 'hardwareConcurrency', {
    get: () => 8,
});

// 메모리(GB). 4GB 서버를 그대로 노출하지 않고 8로.
// hardwareConcurrency와 짝을 맞춰야 자연스럽다
// (8코어인데 메모리 4GB는 흔치 않은 조합).
Object.defineProperty(navigator, 'deviceMemory', {
    get: () => 8,
});
""")