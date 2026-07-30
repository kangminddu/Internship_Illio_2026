# login.py  (프로젝트 루트)
"""
TikTok persistent profile 최초 생성 스크립트.

왜 이 파일이 필요한가
------
유튜브는 로그인 없이 크롤링이 된다. 틱톡은 L2(영상 목록)와
L3(댓글)가 로그인 세션을 요구한다.

그런데 로그인은 자동화할 수 없다.
  - QR 코드 스캔 / SMS 인증 / 소셜 로그인
  - 2차 인증(OTP)
  - CAPTCHA
전부 사람이 해야 한다.

→ 사람이 한 번 로그인하고, 그 결과를 디스크에 남긴다.
  이후 크롤러는 그 프로필을 재사용한다.

★ storage_state(json)가 아니라 persistent profile을 쓴다
------
Playwright에는 세션을 저장하는 방법이 둘 있다:

  storage_state : 쿠키/로컬스토리지를 JSON으로 export
  persistent profile : 크롬 사용자 데이터 디렉터리를 통째로 재사용  ← 채택

틱톡이 쿠키 외에도 IndexedDB, 캐시, 디바이스 지문 등을 함께 보기 때문에
JSON만 옮기면 "다른 브라우저에서 로그인했다"로 판정되어 세션이 무효화된다.
프로필 디렉터리를 통째로 쓰면 그 상태가 전부 유지된다.

대가: 프로필은 프로세스당 하나만 열 수 있다(Chromium SingletonLock).
      login.py와 크롤러를 동시에 실행할 수 없다.

    python login.py

브라우저가 뜨면 QR 로그인 또는 일반 로그인 + 2차 인증까지 완료한 뒤,
터미널로 돌아와 Enter를 누르세요.
이후에는 `python main.py` 만 실행하면 됩니다.
"""

import os
import sys
import asyncio

from playwright.async_api import async_playwright

from tiktok import config
from tiktok.antibot.browser import (
    persistent_launch_kwargs,
    clear_profile_locks,
)

LOGIN_URL = "https://www.tiktok.com/login"
HOME_URL = "https://www.tiktok.com/"

# 이 쿠키가 있으면 로그인된 것으로 본다.
# sessionid_ss는 secure 버전. 둘 중 하나만 있어도 인정한다.
SESSION_COOKIES = ("sessionid", "sessionid_ss")


def _banner():
    """시작 전 현재 상태를 보여준다.

    '기존 프로필 재사용'인지 '신규 생성'인지 알려주는 이유:
    이미 로그인된 프로필이 있는데 모르고 다시 로그인하면
    기존 세션을 덮어쓸 수 있다.
    """
    exists = os.path.isdir(config.PROFILE_DIR) and os.listdir(config.PROFILE_DIR)
    print("=" * 62)
    print(" TikTok 로그인 프로필 생성")
    print("=" * 62)
    print(f" 프로필 경로 : {config.PROFILE_DIR}")
    print(f" 상태        : {'기존 프로필 재사용' if exists else '신규 생성'}")
    print("=" * 62)


async def _wait_enter(prompt):
    """이벤트 루프를 막지 않고 Enter를 기다린다.

    input()은 블로킹 함수다. async 함수 안에서 그냥 호출하면
    이벤트 루프 전체가 멈춰서 브라우저가 응답하지 않는다.
    (사용자가 로그인하려는데 페이지가 얼어붙는다)

    → run_in_executor로 별도 스레드에 넘긴다.
      브라우저는 계속 살아있고, 스레드만 입력을 기다린다.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, input, prompt)


async def _has_session(context):
    """세션 쿠키 존재 여부로 로그인 성공을 판정한다.

    화면을 보고 판단하지 않는 이유: DOM 셀렉터는 UI가 바뀌면 깨진다.
    쿠키는 훨씬 안정적인 신호다.
    """
    names = {c.get("name") for c in await context.cookies()}
    return any(n in names for n in SESSION_COOKIES)


async def main():
    os.makedirs(config.PROFILE_DIR, exist_ok=True)
    _banner()
    # 비정상 종료(강제 kill 등)로 남은 락 파일을 제거한다.
    # 안 지우면 "프로필이 이미 사용 중"이라며 브라우저가 안 뜬다.
    clear_profile_locks()

    ok = False

    async with async_playwright() as p:
        # 크롤러와 동일한 옵션을 사용해야 세션이 무효화되지 않는다.
        #
        # ★ 이게 핵심이다. 브라우저 옵션(viewport, locale, timezone,
        #   user_agent, launch args)이 로그인할 때와 크롤링할 때 다르면
        #   틱톡이 '다른 환경'으로 감지해 재인증을 요구한다.
        #
        #   그래서 옵션을 browser.persistent_launch_kwargs() 한 곳에 모으고
        #   login.py와 크롤러가 같은 함수를 호출한다.
        #   headless만 여기서 False로 덮어쓴다(사람이 봐야 하므로).
        kwargs = persistent_launch_kwargs(headless=False)

        context = await p.chromium.launch_persistent_context(**kwargs)

        try:
            # persistent context는 기본 페이지를 하나 갖고 시작한다.
            # 없으면(드물게) 새로 만든다.
            page = context.pages[0] if context.pages else await context.new_page()

            # 이미 로그인돼 있으면 로그인 페이지 대신 홈으로 간다.
            # (로그인 페이지로 가면 이미 로그인된 사용자가 혼란스럽다)
            already = await _has_session(context)
            try:
                await page.goto(
                    HOME_URL if already else LOGIN_URL,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            except Exception as e:
                # 페이지 로드에 실패해도 브라우저는 살아있다.
                # 사람이 직접 주소를 칠 수 있으므로 중단하지 않는다.
                print(f"[warn] 페이지 로드 실패: {type(e).__name__}: {e}")
                print("       브라우저에서 직접 tiktok.com 으로 이동해도 됩니다.")

            print()
            print("-" * 62)
            print(" 브라우저에서 직접 로그인하세요.")
            print("   · QR / 이메일 / 전화번호 / 소셜 로그인 모두 가능합니다.")
            print("   · 2차 인증(OTP)까지 전부 완료하세요.")
            print("   · captcha 가 뜨면 여기서 미리 풀어두면 좋습니다.")
            #      ↑ 여기서 풀어두면 그 결과가 프로필에 남아
            #        크롤링 중 CAPTCHA 빈도가 줄어든다.
            print("   · 우측 상단에 프로필 아이콘이 보이면 완료입니다.")
            print("-" * 62)
            print(" ⚠️  브라우저 창을 직접 닫지 마세요. 터미널에서 Enter를 누르세요.")
            print("     (강제 종료하면 쿠키가 저장되지 않습니다)")
            #      ↑ 실제로 겪은 문제. 창을 X로 닫으면 Chromium이
            #        메모리의 쿠키를 디스크에 flush하기 전에 죽는다.
            #        로그인은 성공했는데 프로필은 비어 있는 상태가 된다.
            print()

            await _wait_enter(">>> 로그인을 마쳤으면 Enter: ")

            # 사람이 Enter를 눌렀다고 로그인이 됐다는 보장은 없다.
            # 쿠키로 검증하고, 실패하면 한 번 더 기회를 준다.
            ok = await _has_session(context)
            if not ok:
                print("\n[!] 세션 쿠키를 찾지 못했습니다. 로그인이 덜 됐을 수 있습니다.")
                await _wait_enter(">>> 확인 후 다시 Enter (그래도 저장은 진행): ")
                ok = await _has_session(context)

            if ok:
                print("\n[OK] 세션 쿠키 확인됨.")

        finally:
            # close()로 정상 종료해야 쿠키/로컬스토리지가 디스크에 flush 된다.
            #
            # finally에 두는 이유: 예외가 나거나 사용자가 Ctrl+C를 눌러도
            # 여기까지 온 세션은 저장되어야 한다.
            await context.close()

    print()
    print("=" * 62)
    print(f" 프로필 저장 완료 : {config.PROFILE_DIR}")
    print(" 이제부터는 `python main.py` 만 실행하면 됩니다.")
    if not ok:
        print(" ⚠️ 세션 쿠키 미확인 — 크롤링이 실패하면 login.py를 다시 실행하세요.")
    print("=" * 62)
    # 종료 코드를 남긴다. 스크립트로 자동화할 때 성공/실패를 감지할 수 있다.
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[취소] 중단됨. 프로필이 저장되지 않았을 수 있습니다.")
        sys.exit(130)   # 130 = SIGINT로 종료됐음을 뜻하는 관례적 코드