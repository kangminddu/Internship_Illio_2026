# tiktok/antibot/stealth.py

from playwright_stealth import Stealth

# 전역 Stealth 객체 (한 번만 생성)
_stealth = Stealth()


async def apply(page):
    """새 page를 만들 때마다 호출."""

    # 1) viewport를 먼저 잡는다.
    #    stealth 스크립트 일부가 screen/window 크기에 의존해 fingerprint를
    #    맞추므로, viewport 설정 후 stealth를 적용하는 편이 일관적이다.
    await page.set_viewport_size({
        "width": 1920,
        "height": 1080,
    })

    await _stealth.apply_stealth_async(page)

    # 2) 사람이 쓰는 브라우저처럼 헤더 설정.
    #    Accept 헤더에서 이미지 타입을 빼서, 서버가 이미지를 덜 밀어주도록
    #    유도(대역폭 절약). 실제 이미지 차단은 browser.py의 route가 담당하고,
    #    이건 보조 신호. (L1은 텍스트 데이터만 필요)
    await page.set_extra_http_headers({
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    })

    await page.emulate_media(color_scheme="dark")


async def prepare_context(context):
    """context 단위 설정. 이후 fingerprint 패치도 여기 추가."""
    await context.add_init_script("""
Object.defineProperty(navigator, 'pdfViewerEnabled', {
    get: () => true,
});

Object.defineProperty(navigator, 'hardwareConcurrency', {
    get: () => 8,
});

Object.defineProperty(navigator, 'deviceMemory', {
    get: () => 8,
});
""")