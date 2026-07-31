# tiktok/antibot/browser.py
"""
브라우저/프로필 관리. L2·L3와 login.py가 공유하는 단일 진입점.

★ 이 파일의 존재 이유는 '옵션 일치'다.
------
틱톡은 브라우저 지문(viewport, locale, timezone, user_agent, launch args)을
세션 검증에 쓴다. login.py가 A 옵션으로 로그인했는데 크롤러가 B 옵션으로
프로필을 열면 "다른 환경에서 접속했다"로 판정해 재인증을 요구한다.

→ 옵션을 persistent_launch_kwargs() 한 곳에 모으고
  login.py와 L2/L3가 같은 함수를 호출한다.
  (login.py는 headless만 False로 덮어쓴다)

L1은 이 파일을 쓰지 않는다. L1은 공개 프로필만 읽으므로 로그인이 불필요하고,
오히려 로그인 상태로 접근하면 틱톡이 계정 단위로 조회를 제한한다.
→ l1.py가 자체 _new_context()로 비로그인 브라우저를 띄운다.
"""
import os

from tiktok import config
from tiktok.antibot import stealth

# ── 기존 상수는 그대로 두세요 (아래는 예시) ──────────────────
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",  # navigator.webdriver 숨김
    "--disable-dev-shm-usage",                        # 컨테이너 /dev/shm 부족 대비
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-crash-restore-bubble",       # 비정상 종료 후 '복원하시겠습니까' 배너 억제.
    "--disable-session-crashed-bubble",  # 배너가 뜨면 페이지를 가려 클릭이 막힌다.
]

# getattr(config, ..., 기본값) 패턴을 쓰는 이유:
# config에 값이 없어도 돌아가게 하려는 것. 나중에 지문을 세밀하게
# 조정할 때 config에 추가만 하면 코드를 안 고쳐도 된다.
USER_AGENT = getattr(config, "USER_AGENT", None)
VIEWPORT = getattr(config, "VIEWPORT", {"width": 1440, "height": 900})
LOCALE = getattr(config, "LOCALE", "ko-KR")
TIMEZONE = getattr(config, "TIMEZONE", "Asia/Seoul")
CHANNEL = getattr(config, "BROWSER_CHANNEL", None)   # "chrome" 또는 None
# ↑ channel="chrome"으로 두면 번들 Chromium 대신 실제 설치된 Chrome을 쓴다.
#   지문이 더 자연스럽지만 환경에 Chrome이 있어야 해서 기본값은 None.


# ── 프로필 유틸 ────────────────────────────────────────────
def profile_ready() -> bool:
    """로그인 프로필이 존재하는지. 없으면 create_context가 안내 메시지와 함께 중단."""
    return os.path.isdir(config.PROFILE_DIR) and bool(os.listdir(config.PROFILE_DIR))


def clear_profile_locks():
    """비정상 종료(kill -9 등)로 남은 락 제거. 없으면 조용히 통과.

    Chromium은 프로필을 열 때 SingletonLock 파일을 만들고 종료 시 지운다.
    강제 종료되면 파일이 남아서, 다음 실행에 "프로필이 이미 사용 중"이라며
    브라우저가 안 뜬다. 실제로 여러 번 겪어서 자동 정리를 넣었다.

    ⚠️ 진짜로 다른 프로세스가 쓰고 있어도 지워버린다.
      프로필을 두 프로세스가 동시에 열면 데이터가 깨질 수 있다.
      → 실행 전에 tasklist로 확인하는 습관이 필요하다.
    """
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = os.path.join(config.PROFILE_DIR, name)
        try:
            # islink도 확인한다. 이 파일들은 심볼릭 링크인 경우가 있어
            # exists()만으로는 깨진 링크를 못 잡는다.
            if os.path.islink(p) or os.path.exists(p):
                os.unlink(p)
        except OSError:
            pass


def persistent_launch_kwargs(headless=None, proxy=None):
    """
    login.py와 크롤러가 '완전히 동일한' 옵션으로 프로필을 열도록
    공유하는 단일 진입점. 지문이 어긋나면 세션이 무효화될 수 있다.

    ★ 이게 이 파일의 핵심이다.
      옵션을 각자 하드코딩했다면 login.py를 고칠 때 크롤러를 같이
      안 고쳐서 조용히 재인증이 걸린다. 그런 버그는 원인을 찾기 어렵다.
    """
    kwargs = dict(
        user_data_dir=config.PROFILE_DIR,
        # headless=None이면 config 값을 쓴다.
        # login.py는 False를 명시해 사람이 볼 수 있게 한다.
        headless=getattr(config, "HEADLESS", False) if headless is None else headless,
        args=LAUNCH_ARGS,
        viewport=VIEWPORT,
        locale=LOCALE,
        timezone_id=TIMEZONE,
        accept_downloads=False,   # 크롤링 중 파일 다운로드는 필요 없다
    )
    if USER_AGENT:
        kwargs["user_agent"] = USER_AGENT
    if CHANNEL:
        kwargs["channel"] = CHANNEL

    # 기존 프록시 로직이 있다면 그대로 연결하세요.
    # ⚠️ config.PROXY가 설정된 적이 없어 이 경로는 미사용이다.
    #    antibot/proxy.py도 proxies.txt가 없으면 빈 목록을 반환한다.
    #    프록시 로테이션을 준비하다 만 흔적.
    p = proxy if proxy is not None else getattr(config, "PROXY", None)
    if p:
        kwargs["proxy"] = p

    return kwargs


class _PersistentBrowser:
    """
    launch_persistent_context()는 Browser 객체를 반환하지 않는다.
    기존 `pw_browser, context = await create_context(p)` 인터페이스를
    유지하기 위한 shim. close()는 context를 닫아 쿠키를 flush한다.

    ★ 왜 이런 껍데기가 필요한가:

      Playwright에는 브라우저를 여는 방법이 둘이다.
        launch()                    → (browser, context) 둘 다 얻는다
        launch_persistent_context() → context 하나만 반환한다

      원래 코드는 launch() 방식이라 호출부가 전부
      `pw_browser, context = ...` 형태였다.
      persistent profile로 바꾸면서 반환값이 하나가 됐는데,
      L2/L3의 호출부를 전부 고치는 대신 shim을 하나 만들었다.

      → 어댑터 패턴. 인터페이스를 유지하면서 내부 구현만 바꾼다.
    """
    __slots__ = ("_context",)   # 인스턴스가 많이 만들어지진 않지만 의도를 명시

    def __init__(self, context):
        self._context = context

    async def close(self):
        # 호출부는 pw_browser.close()를 부르지만 실제로는 context를 닫는다.
        # close()로 정상 종료해야 쿠키/로컬스토리지가 디스크에 flush된다.
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
    # 프로필이 없으면 여기서 멈춘다.
    # 없는 채로 진행하면 로그인 안 된 상태로 크롤링해서
    # 전부 실패하고, 원인을 찾느라 시간을 쓴다.
    # → 실행 초입에 명확한 안내와 함께 중단하는 게 낫다.
    if not profile_ready():
        raise RuntimeError(
            f"\n로그인 프로필이 없습니다: {config.PROFILE_DIR}\n"
            f"먼저 `python login.py` 를 실행해 TikTok 로그인을 완료하세요.\n"
        )

    clear_profile_locks()

    kwargs = persistent_launch_kwargs()
    kwargs.update(overrides)   # 호출부가 개별 옵션을 덮어쓸 수 있게

    try:
        context = await playwright.chromium.launch_persistent_context(**kwargs)
    except Exception as e:
        # 원인을 추정해서 알려준다.
        # Playwright의 기본 에러 메시지만으로는 "프로필이 이미 열려 있다"는 걸
        # 알기 어렵다. 실제로 login.py를 띄워둔 채 크롤러를 돌려서 겪었다.
        raise RuntimeError(
            f"프로필을 열지 못했습니다: {config.PROFILE_DIR}\n"
            f"login.py 브라우저나 다른 크롤러가 같은 프로필을 "
            f"사용 중인지 확인하세요. (프로필은 프로세스당 1개만 열 수 있습니다)\n"
            f"원인: {type(e).__name__}: {e}"
        ) from e

    # 기존에 set_default_timeout 등을 걸어뒀다면 여기서 그대로 유지
    await stealth.prepare_context(context)
    # ⚠️ L2/L3는 여기서 stealth를 받는다.
    #    L1에서 stealth를 켰을 때 전 요청이 ERR_HTTP_RESPONSE_CODE_FAILURE로
    #    실패한 적이 있어, L1은 stealth 없이 돌린다.
    #    L2/L3에서는 지금까지 문제가 없었지만 주의가 필요한 지점.

    return _PersistentBrowser(context), context


async def new_page(context):
    """변경 없음."""
    page = await context.new_page()
    await stealth.apply(page)   # 페이지 단위 지문 패치
    return page