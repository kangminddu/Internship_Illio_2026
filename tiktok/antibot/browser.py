# tiktok/antibot/browser.py
import os

from tiktok import config
from tiktok.antibot import stealth

# ── 기존 상수는 그대로 두세요 (아래는 예시) ──────────────────
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
]

USER_AGENT = getattr(config, "USER_AGENT", None)
VIEWPORT = getattr(config, "VIEWPORT", {"width": 1440, "height": 900})
LOCALE = getattr(config, "LOCALE", "ko-KR")
TIMEZONE = getattr(config, "TIMEZONE", "Asia/Seoul")
CHANNEL = getattr(config, "BROWSER_CHANNEL", None)   # "chrome" 또는 None


# ── 프로필 유틸 ────────────────────────────────────────────
def profile_ready() -> bool:
    """login.py가 한 번이라도 정상 실행됐는지 확인."""
    return os.path.isfile(
        os.path.join(config.PROFILE_DIR, "Default", "Cookies")
    )


def clear_profile_locks():
    """비정상 종료(kill -9 등)로 남은 락 제거. 없으면 조용히 통과."""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = os.path.join(config.PROFILE_DIR, name)
        try:
            if os.path.islink(p) or os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass


def persistent_launch_kwargs(headless=None, proxy=None):
    """
    login.py와 크롤러가 '완전히 동일한' 옵션으로 프로필을 열도록
    공유하는 단일 진입점. 지문이 어긋나면 세션이 무효화될 수 있다.
    """
    kwargs = dict(
        user_data_dir=config.PROFILE_DIR,
        headless=getattr(config, "HEADLESS", False) if headless is None else headless,
        args=LAUNCH_ARGS,
        viewport=VIEWPORT,
        locale=LOCALE,
        timezone_id=TIMEZONE,
        accept_downloads=False,
    )
    if USER_AGENT:
        kwargs["user_agent"] = USER_AGENT
    if CHANNEL:
        kwargs["channel"] = CHANNEL

    # 기존 프록시 로직이 있다면 그대로 연결하세요.
    p = proxy if proxy is not None else getattr(config, "PROXY", None)
    if p:
        kwargs["proxy"] = p

    return kwargs


class _PersistentBrowser:
    """
    launch_persistent_context()는 Browser 객체를 반환하지 않는다.
    기존 `pw_browser, context = await create_context(p)` 인터페이스를
    유지하기 위한 shim. close()는 context를 닫아 쿠키를 flush한다.
    """
    __slots__ = ("_context",)

    def __init__(self, context):
        self._context = context

    async def close(self):
        try:
            await self._context.close()
        except Exception:
            pass

    @property
    def contexts(self):
        return [self._context]

    def is_connected(self):
        return True


async def create_context(playwright, **overrides):
    """
    ⚠️ 인터페이스 변경 없음: (browser_like, context) 튜플 반환.
    내부만 persistent profile 기반으로 변경됨.
    """
    if not profile_ready():
        raise RuntimeError(
            f"\n로그인 프로필이 없습니다: {config.PROFILE_DIR}\n"
            f"먼저 `python login.py` 를 실행해 TikTok 로그인을 완료하세요.\n"
        )

    clear_profile_locks()

    kwargs = persistent_launch_kwargs()
    kwargs.update(overrides)

    try:
        context = await playwright.chromium.launch_persistent_context(**kwargs)
    except Exception as e:
        raise RuntimeError(
            f"프로필을 열지 못했습니다: {config.PROFILE_DIR}\n"
            f"login.py 브라우저나 다른 크롤러가 같은 프로필을 "
            f"사용 중인지 확인하세요. (프로필은 프로세스당 1개만 열 수 있습니다)\n"
            f"원인: {type(e).__name__}: {e}"
        ) from e

    # 기존에 set_default_timeout 등을 걸어뒀다면 여기서 그대로 유지
    await stealth.prepare_context(context)

    return _PersistentBrowser(context), context


async def new_page(context):
    """변경 없음."""
    page = await context.new_page()
    await stealth.apply(page)
    return page