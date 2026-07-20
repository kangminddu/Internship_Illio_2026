# tiktok/antibot/browser.py

from pathlib import Path

from tiktok.antibot import stealth
from tiktok.antibot import manager

PROFILE_DIR = str(Path.home() / "tiktok-playwright-profile")

# ----------------------------
# CDP 설정
# ----------------------------
USE_CDP = True
CDP_URL = "http://127.0.0.1:9222"

MAX_PROXY_RETRY = 5

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
]

BLOCK_RESOURCE_TYPES = {"image", "media", "font"}


async def _block_heavy(route):
    try:
        if route.request.resource_type in BLOCK_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()
    except Exception:
        pass


class BrowserSession:
    """
    launch_persistent_context wrapper.
    CDP에서는 사용되지 않지만 기존 코드 호환을 위해 유지.
    """

    def __init__(self, context):
        self.context = context

    async def close(self):
        try:
            await self.context.close()
        except Exception:
            pass


async def create_context(playwright):
    """
    우선순위

    1. CDP (이미 실행 중인 크롬)
    2. 실패하면 기존 Webshare Proxy
    """

    # =====================================================
    # 1. CDP
    # =====================================================
    if USE_CDP:
        try:
            print("[browser] CDP 연결 시도")

            browser = await playwright.chromium.connect_over_cdp(CDP_URL)

            if not browser.contexts:
                raise RuntimeError("CDP context가 없습니다.")

            context = browser.contexts[0]

            await context.route("**/*", _block_heavy)

            await stealth.prepare_context(context)

            print("[browser] CDP 연결 성공")

            return browser, context

        except Exception as e:
            print(f"[browser] CDP 실패 → 기존 launch 사용 ({e})")

    # =====================================================
    # 2. 기존 Webshare 방식
    # =====================================================

    last_error = None

    for attempt in range(1, MAX_PROXY_RETRY + 1):

        proxy = manager.current_proxy()

        print(f"[proxy] {proxy['server'] if proxy else 'None'}")

        context = None

        try:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=False,
                proxy=proxy,
                args=LAUNCH_ARGS,
            )

            await context.route("**/*", _block_heavy)

            await stealth.prepare_context(context)

            return BrowserSession(context), context

        except Exception as e:

            last_error = e

            print(
                f"[proxy] launch fail ({attempt}/{MAX_PROXY_RETRY}) "
                f"| {type(e).__name__}: {e}"
            )

            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass

            manager.fail_and_rotate()

    raise RuntimeError(
        f"사용 가능한 Proxy가 없습니다 "
        f"(시도 {MAX_PROXY_RETRY}회)\n"
        f"마지막 에러 : {type(last_error).__name__}: {last_error}"
    )


async def new_page(context):
    page = await context.new_page()
    await stealth.apply(page)
    return page