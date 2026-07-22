# login.py  (프로젝트 루트)
"""
TikTok persistent profile 최초 생성 스크립트.

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

SESSION_COOKIES = ("sessionid", "sessionid_ss")


def _banner():
    exists = os.path.isdir(config.PROFILE_DIR) and os.listdir(config.PROFILE_DIR)
    print("=" * 62)
    print(" TikTok 로그인 프로필 생성")
    print("=" * 62)
    print(f" 프로필 경로 : {config.PROFILE_DIR}")
    print(f" 상태        : {'기존 프로필 재사용' if exists else '신규 생성'}")
    print("=" * 62)


async def _wait_enter(prompt):
    """이벤트 루프를 막지 않고 Enter를 기다린다."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, input, prompt)


async def _has_session(context):
    names = {c.get("name") for c in await context.cookies()}
    return any(n in names for n in SESSION_COOKIES)


async def main():
    os.makedirs(config.PROFILE_DIR, exist_ok=True)
    _banner()
    clear_profile_locks()

    ok = False

    async with async_playwright() as p:
        # 크롤러와 동일한 옵션을 사용해야 세션이 무효화되지 않는다.
        kwargs = persistent_launch_kwargs(headless=False)

        context = await p.chromium.launch_persistent_context(**kwargs)

        try:
            page = context.pages[0] if context.pages else await context.new_page()

            already = await _has_session(context)
            try:
                await page.goto(
                    HOME_URL if already else LOGIN_URL,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
            except Exception as e:
                print(f"[warn] 페이지 로드 실패: {type(e).__name__}: {e}")
                print("       브라우저에서 직접 tiktok.com 으로 이동해도 됩니다.")

            print()
            print("-" * 62)
            print(" 브라우저에서 직접 로그인하세요.")
            print("   · QR / 이메일 / 전화번호 / 소셜 로그인 모두 가능합니다.")
            print("   · 2차 인증(OTP)까지 전부 완료하세요.")
            print("   · captcha 가 뜨면 여기서 미리 풀어두면 좋습니다.")
            print("   · 우측 상단에 프로필 아이콘이 보이면 완료입니다.")
            print("-" * 62)
            print(" ⚠️  브라우저 창을 직접 닫지 마세요. 터미널에서 Enter를 누르세요.")
            print("     (강제 종료하면 쿠키가 저장되지 않습니다)")
            print()

            await _wait_enter(">>> 로그인을 마쳤으면 Enter: ")

            ok = await _has_session(context)
            if not ok:
                print("\n[!] 세션 쿠키를 찾지 못했습니다. 로그인이 덜 됐을 수 있습니다.")
                await _wait_enter(">>> 확인 후 다시 Enter (그래도 저장은 진행): ")
                ok = await _has_session(context)

            if ok:
                print("\n[OK] 세션 쿠키 확인됨.")

        finally:
            # close()로 정상 종료해야 쿠키/로컬스토리지가 디스크에 flush 된다.
            await context.close()

    print()
    print("=" * 62)
    print(f" 프로필 저장 완료 : {config.PROFILE_DIR}")
    print(" 이제부터는 `python main.py` 만 실행하면 됩니다.")
    if not ok:
        print(" ⚠️ 세션 쿠키 미확인 — 크롤링이 실패하면 login.py를 다시 실행하세요.")
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[취소] 중단됨. 프로필이 저장되지 않았을 수 있습니다.")
        sys.exit(130)